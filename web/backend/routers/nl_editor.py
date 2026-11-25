"""자연어 RenderSpec 편집 엔드포인트 (Phase 7, #315).

엔드포인트:
- `POST /api/projects/{project_id}/nl-edit` — 자연어 명령 → patch + 요약 (미리보기).
- `POST /api/projects/{project_id}/nl-edit/apply` — 사용자 승인 후 patch 실제 적용.
- `POST /api/projects/{project_id}/nl-edit/undo` — 직전 snapshot 으로 롤백.

저장 구조
--------
- 현재 RenderSpec: `web/data/{id}/cache/render_spec.json`
  · 본 라우터는 RenderSpec 캐시가 없을 때 SceneCard 로부터 *그때그때* 빌드한다.
  · apply 시점에 새 RenderSpec 을 위 경로에 덮어쓴다.
- snapshot stack: `web/data/{id}/nl_edit/snapshots/{ISO타임스탬프}.json`
  · 적용 직전의 RenderSpec 을 timestamp 파일명으로 저장 → undo 시 가장 최근 파일을 pop.

동시성
------
같은 프로젝트에 동시 다발 요청이 들어오면 후자는 409 ConflictError 로 거부한다. 단순
asyncio.Lock 기반 구현이며, 다중 워커 환경에서는 동일 프로세스 안에서만 보장된다.
다중 프로세스 대응(파일 락 등)은 별도 이슈로 미룬다 — 본 이슈 비범위.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

from src.nl_editor import (
    JsonPatchOp,
    NlEditApplyRequest,
    NlEditApplyResponse,
    NlEditRequest,
    NlEditResponse,
    NlEditUndoResponse,
    PatchApplyError,
    apply_patch_to_specs,
)
from src.nl_editor.gemini_client import generate_nl_edit, release_project_cache
from src.nl_editor.intent_classifier import classify_intent
from src.render_spec.builder import build_render_spec
from src.render_spec.effects import EFFECT_REGISTRY
from src.render_spec.scene_card_sync import sync_scene_cards_from_render_spec

from .. import state as store
from ..cost_tracker import _load_estimator, _save_estimator

router = APIRouter(prefix="/api/projects", tags=["nl_editor"])

# ── 동시성 — project_id 단위 락 ───────────────────────────────────────────

_project_locks: dict[str, asyncio.Lock] = {}


def _get_lock(project_id: str) -> asyncio.Lock:
    lock = _project_locks.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _project_locks[project_id] = lock
    return lock


# ── 저장 경로 헬퍼 ────────────────────────────────────────────────────────

def _project_cache_dir(project_id: str) -> Path:
    d = store.DATA_DIR / project_id / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_spec_path(project_id: str) -> Path:
    return _project_cache_dir(project_id) / "render_spec.json"


def _snapshots_dir(project_id: str) -> Path:
    d = store.DATA_DIR / project_id / "nl_edit" / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── RenderSpec 로드/저장 ──────────────────────────────────────────────────

def _load_render_spec_payload(project_id: str) -> dict[str, Any]:
    """현재 RenderSpec 을 `{"scenes": [...]}` dict 로 로드.

    캐시 파일이 있으면 그대로 읽고, 없으면 Project.scenes 로부터 *그때그때* 빌드해서
    돌려준다(빌드 결과는 저장하지 않음 — apply 시점에 저장).
    """
    cache = _render_spec_path(project_id)
    if cache.exists() and cache.stat().st_size > 0:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "scenes" in data:
                return data
            if isinstance(data, list):
                return {"scenes": data}
        except Exception:
            pass  # 손상된 캐시면 무시하고 다시 빌드

    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, f"프로젝트를 찾을 수 없습니다: {project_id}")

    scenes_dicts: list[dict] = []
    for sc in p.scenes:
        try:
            spec = build_render_spec(sc, p)
        except Exception as e:
            # 빌더가 실패하면 nl-edit 의 의미가 사라지므로 즉시 명시적 에러로 통보.
            raise HTTPException(
                500,
                f"RenderSpec 빌드 실패 (씬 {getattr(sc, 'scene_id', '?')}): {e}",
            )
        scenes_dicts.append(json.loads(spec.model_dump_json()))
    return {"scenes": scenes_dicts}


def _save_render_spec_payload(project_id: str, payload: dict) -> None:
    """`{"scenes": [...]}` dict 를 캐시에 저장. 호출자가 검증을 마친 후 호출한다."""
    cache = _render_spec_path(project_id)
    cache.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _save_snapshot(project_id: str, payload: dict) -> str:
    """현재 RenderSpec 을 snapshot 디렉토리에 timestamp 파일로 저장.

    반환: snapshot_id (= 파일명에서 확장자 제거).
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    # 파일명 안전: `2026-05-29T12-34-56-123456`
    snapshot_id = now.strftime("%Y-%m-%dT%H-%M-%S-%f")
    target = _snapshots_dir(project_id) / f"{snapshot_id}.json"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return snapshot_id


def _latest_snapshot(project_id: str) -> tuple[str, dict] | None:
    """가장 최근 snapshot 을 (id, payload) 로 반환. 없으면 None."""
    d = _snapshots_dir(project_id)
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    f = files[-1]
    try:
        return f.stem, json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pop_latest_snapshot(project_id: str) -> tuple[str, dict, bool] | None:
    """가장 최근 snapshot 을 가져온 후 *파일을 삭제*.

    반환: (id, payload, has_more) — has_more 는 삭제 이후 남은 snapshot 이 있는지.
    """
    d = _snapshots_dir(project_id)
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    f = files[-1]
    try:
        payload = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    f.unlink(missing_ok=True)
    has_more = len(list(d.glob("*.json"))) > 0
    return f.stem, payload, has_more


# ── #355 — 프로젝트별 Gemini 모델 조회 ──────────────────────────────────

def _project_gemini_model(project_id: str) -> str | None:
    """Project.gemini_model 을 반환. 빈 문자열·None 이면 None 으로 정규화해
    호출자가 pricing.json default_model 로 폴백할 수 있게 한다.
    """
    try:
        p = store.get_project(project_id)
    except Exception:
        return None
    if not p:
        return None
    m = getattr(p, "gemini_model", "") or ""
    return m.strip() or None


# ── 비용 누적 ────────────────────────────────────────────────────────────

def _record_intent_classify_cost(project_id: str, meta: dict) -> float:
    """의도 분류 1회 호출 비용을 `7_nl_edit_classify` phase 로 누적.

    반환: 이번 호출의 api_usd 추정값(로그에 기록할 용도). 실패 시 0.0.
    """
    try:
        est = _load_estimator(project_id)
        pc = est.record_intent_classify(
            input_chars=meta.get("input_chars", 0) or 0,
            output_chars=meta.get("output_chars", 0) or 0,
            cached_input_tokens=meta.get("cached_input_tokens", 0) or 0,
            regular_input_tokens=meta.get("regular_input_tokens", 0) or 0,
            output_tokens=meta.get("output_tokens", 0) or 0,
            model=meta.get("model") or None,
        )
        _save_estimator(project_id, est)
        return float(getattr(pc, "api_usd", 0.0) or 0.0)
    except Exception:
        # 비용 기록 실패가 본 기능을 막아서는 안 됨.
        return 0.0


def _intent_log_path(project_id: str) -> Path:
    """`web/data/<pid>/cache/intent_log.jsonl` 경로 반환 (디렉토리 보장)."""
    d = _project_cache_dir(project_id)
    return d / "intent_log.jsonl"


# 로그 회전 임계 — 1MB 초과 시 날짜별 파일로 회전.
_INTENT_LOG_ROTATE_BYTES = 1 * 1024 * 1024


def _append_intent_log(
    project_id: str,
    instruction: str,
    intent: str,
    meta: dict,
    api_usd: float,
) -> None:
    """분류 결과 1건을 JSONL 한 줄로 append.

    파일 누적이 1MB 를 넘으면 같은 디렉토리에서 `intent_log.YYYYMMDD.jsonl` 로 회전한다.
    회전 실패해도 분류 자체는 막지 않는다.
    """
    try:
        path = _intent_log_path(project_id)
        # 회전 (선택적) — 파일 크기 1MB 초과 시 날짜 스탬프로 rename.
        try:
            if path.exists() and path.stat().st_size > _INTENT_LOG_ROTATE_BYTES:
                stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
                rotated = path.parent / f"intent_log.{stamp}.jsonl"
                # 같은 날 여러 번 회전될 수 있으므로 suffix 부여.
                if rotated.exists():
                    idx = 1
                    while True:
                        cand = path.parent / f"intent_log.{stamp}.{idx}.jsonl"
                        if not cand.exists():
                            rotated = cand
                            break
                        idx += 1
                path.rename(rotated)
        except Exception:
            # 회전 실패는 무시(연속 append 우선).
            pass

        line = json.dumps(
            {
                "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "instruction": instruction,
                "intent": intent,
                "reasoning": meta.get("reasoning", ""),
                "model": meta.get("model", ""),
                "fallback_used": bool(meta.get("fallback_used", False)),
                "input_chars": int(meta.get("input_chars", 0) or 0),
                "output_chars": int(meta.get("output_chars", 0) or 0),
                "cached_input_tokens": int(meta.get("cached_input_tokens", 0) or 0),
                "regular_input_tokens": int(meta.get("regular_input_tokens", 0) or 0),
                "output_tokens": int(meta.get("output_tokens", 0) or 0),
                "api_usd": float(api_usd or 0.0),
                "duration_ms": int(meta.get("duration_ms", 0) or 0),
            },
            ensure_ascii=False,
        )
        # append-only — 한 줄씩 추가.
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # 로깅 실패가 기능을 막지 않도록 조용히 패스.
        pass


def _record_nl_edit_cost(project_id: str, meta: dict) -> None:
    """`cost_estimator.record_nl_edit` 호출 후 project.cost_estimate 에 저장.

    #317 — meta 의 캐시/일반 토큰 분리를 그대로 전달한다. 토큰 정보가 없으면 글자수로
    폴백 계산된다(estimator 측에서 처리).

    캐시 저장 비용: 한 번의 호출이 캐시를 "점유"한 시간을 호출 간격으로 가정한다.
    여기서는 보수적으로 호출 1회당 캐시 age 만큼 누적 — TTL 만료 시 자동 정리되므로
    과대 청구 위험은 낮다.
    """
    try:
        est = _load_estimator(project_id)
        est.record_nl_edit(
            input_chars=meta.get("input_chars", 0) or 0,
            output_chars=meta.get("output_chars", 0) or 0,
            cached_input_tokens=meta.get("cached_input_tokens", 0) or 0,
            regular_input_tokens=meta.get("regular_input_tokens", 0) or 0,
            output_tokens=meta.get("output_tokens", 0) or 0,
            cache_storage_tokens=meta.get("cache_storage_tokens", 0) or 0,
            # 호출 간격을 모르므로 1초로 가정 — 저장 비용을 과소 평가하지 않기 위해
            # 캐시가 활성일 때만 보수적으로 +1초씩 누적한다. 정확한 저장 비용은
            # TTL × 토큰수로 계산되지만, 여기서는 사용 빈도로 점진 누적.
            cache_storage_seconds=1.0 if (meta.get("cache_storage_tokens", 0) or 0) > 0 else 0.0,
            # 자동 씬 분할/자동 오버레이 검출 시 비디오 inline 입력 토큰(295 tok/s)을 누적.
            # detector 들이 meta 에 video_duration_sec 를 채워 보낸다.
            video_duration_sec=meta.get("video_duration_sec", 0.0) or 0.0,
            # #355 — meta 에 detector/generate_nl_edit 이 채워준 모델 ID 전달.
            # 누락 시 estimator 가 default_model 로 폴백한다.
            model=meta.get("model") or None,
        )
        _save_estimator(project_id, est)
    except Exception:
        # 비용 기록 실패가 본 기능을 막아서는 안 됨 — 조용히 패스.
        pass


# ── 엔드포인트 ────────────────────────────────────────────────────────────

@router.post("/{project_id}/nl-edit", response_model=NlEditResponse)
async def nl_edit_preview(project_id: str, req: NlEditRequest) -> NlEditResponse:
    """자연어 명령 → patch + summary + safety_flags (미리보기, 적용 안 함)."""
    lock = _get_lock(project_id)
    if lock.locked():
        raise HTTPException(409, "같은 프로젝트에 다른 nl-edit 요청이 진행 중입니다.")

    async with lock:
        # ── 의도 분류 — LLM 기반(#359) ─────────────────────────────────
        # 텍스트만 보내므로 매우 저렴(Flash-Lite 기준 ~0.03원). 영상 첨부 호출
        # (split/overlay detector) 은 의도가 명확할 때만 발동시켜 비디오 토큰
        # 비용을 최소화한다. LLM 실패 시 키워드 폴백.
        classifier_model = _project_gemini_model(project_id)
        loop = asyncio.get_event_loop()
        intent, classify_meta = await loop.run_in_executor(
            None,
            lambda: classify_intent(
                req.instruction,
                project_id=project_id,
                model=classifier_model,
            ),
        )
        # 분류 비용 기록 + 로그 기록 (책임 분리: 분류 결과를 받은 *직후* 라우터에서 기록).
        classify_api_usd = _record_intent_classify_cost(project_id, classify_meta)
        _append_intent_log(
            project_id=project_id,
            instruction=req.instruction,
            intent=intent,
            meta=classify_meta,
            api_usd=classify_api_usd,
        )

        # 이벤트 기반 자동 오버레이 의도면 별도 경로(텍스트+SFX).
        if intent == "overlay" and req.target_scene_id is not None:
            try:
                return await _handle_overlay_intent(project_id, req)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(503, f"자동 오버레이 분석 실패: {e}")

        # 이벤트 기반 자동 씬 분할 의도면 별도 경로 — Gemini로 영상 분석 후 cuts 반환.
        if intent == "split" and req.target_scene_id is not None:
            try:
                split_resp = await _handle_split_intent(project_id, req)
                return split_resp
            except HTTPException:
                raise
            except Exception as e:
                # 분할 경로 실패 시 사용자에게 명확히 안내(기존 patch 경로로 폴백하지 않음).
                raise HTTPException(503, f"자동 분할 분석 실패: {e}")

        # target_scene_id 가 없는 경우, split/overlay 로 분류돼도 영상이 없으니
        # patch 경로로 흘려보내야 한다(기존 키워드 분류 동작과 동일).
        payload = _load_render_spec_payload(project_id)
        # #355 — 프로젝트 설정 모델을 LLM 호출에 전달. None 이면 default_model.
        model_id = _project_gemini_model(project_id)
        loop = asyncio.get_event_loop()
        try:
            resp, meta = await loop.run_in_executor(
                None,
                lambda: generate_nl_edit(
                    effect_registry=EFFECT_REGISTRY,
                    render_spec_payload=payload,
                    user_instruction=req.instruction,
                    history=req.history,
                    project_id=project_id,  # #317 — 캐시 키 구성용
                    model=model_id,
                ),
            )
        except PatchApplyError as e:
            raise HTTPException(422, f"LLM 응답을 유효한 RenderSpec patch 로 변환하지 못했습니다: {e}")
        except RuntimeError as e:
            raise HTTPException(503, f"LLM 호출 실패: {e}")

        # 비용 누적 (미리보기도 호출이 발생했으므로 기록)
        _record_nl_edit_cost(project_id, meta)

        return resp


def _extract_scene_clip(proxy_path: Path, clip_start: float | None, clip_end: float | None) -> Path:
    """씬의 clip_start/end 구간만 ffmpeg로 잘라 임시 파일 생성. 둘 다 None이면 원본 그대로 반환.

    Gemini가 보는 영상이 씬과 동일한 구간이어야 시점/위치 추정이 정확해진다.
    분할된 씬의 경우 원본 전체(예: 135초)가 아닌 그 씬의 구간(예: 5초)만 보내야 한다.
    """
    if clip_start is None and clip_end is None:
        return proxy_path
    import subprocess as _sp
    import tempfile as _tmp
    cs = float(clip_start or 0.0)
    fd, tmp_str = _tmp.mkstemp(suffix=".mp4", prefix="vag_nl_clip_")
    import os as _os
    _os.close(fd)
    tmp = Path(tmp_str)
    cmd = [ffmpeg_bin(), "-y", "-ss", f"{cs:.3f}", "-i", str(proxy_path)]
    if clip_end is not None and float(clip_end) > cs:
        cmd += ["-to", f"{float(clip_end) - cs:.3f}"]
    cmd += ["-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(tmp)]
    r = _sp.run(cmd, capture_output=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return proxy_path  # 추출 실패 시 원본 폴백
    return tmp


async def _handle_split_intent(project_id: str, req: NlEditRequest) -> NlEditResponse:
    """이벤트 기반 자동 씬 분할 처리. 720p 프록시 영상을 Gemini에 보내 cuts 검출."""
    from src.nl_editor.split_detector import detect_split_points
    from src.nl_editor.models import SplitDetectResult, JsonPatchOp
    from .. import state as store
    from ..routers.project import _ensure_proxy

    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    scene = next((s for s in p.scenes if s.scene_id == req.target_scene_id), None)
    if not scene:
        raise HTTPException(404, f"씬을 찾을 수 없습니다: scene_id={req.target_scene_id}")
    if not scene.footage:
        raise HTTPException(400, "footage 없는 씬은 분할 불가")
    fname = getattr(scene.footage, "file", None)
    if not fname:
        raise HTTPException(400, "비디오 footage만 자동 분할 가능합니다")

    proxy = _ensure_proxy(project_id, fname)
    if proxy is None:
        raise HTTPException(400, "이 footage의 720p 프록시를 만들 수 없습니다(비디오가 아닐 수 있음)")

    cs = getattr(scene.footage, "clip_start_sec", None)
    ce = getattr(scene.footage, "clip_end_sec", None)
    # 씬이 분할된 경우 그 구간만 추출 — Gemini가 씬과 동일한 영상을 봐야 시점 추정이 정확.
    clip = _extract_scene_clip(proxy, cs, ce)
    # 추출된 클립은 0초 기준이므로 ctx의 clip_start는 0으로 LLM에게 알려 응답도 0 기준이 되게 한다.
    duration = float(ce) - float(cs or 0.0) if ce is not None else None
    ctx = {"clip_start_sec": 0.0, "clip_end_sec": duration, "scene_id": scene.scene_id}

    # #355 — 프로젝트 설정 모델을 detector 호출에 전달.
    model_id = _project_gemini_model(project_id)
    loop = asyncio.get_event_loop()
    cuts_rel_raw, reasoning, meta = await loop.run_in_executor(
        None, lambda: detect_split_points(str(clip), req.instruction, ctx, model=model_id)
    )
    # 임시 클립 정리
    if clip != proxy:
        clip.unlink(missing_ok=True)
    # 응답은 이미 씬(클립) 0초 기준이라 별도 변환 불필요. 범위 밖만 거른다.
    cuts_rel = [round(c, 2) for c in cuts_rel_raw if c > 0.0]
    if duration is not None:
        cuts_rel = [c for c in cuts_rel if c < duration]
    cuts_rel = sorted({c for c in cuts_rel})

    _record_nl_edit_cost(project_id, meta)

    n = len(cuts_rel)
    summary = (
        f"씬 {scene.scene_id}을(를) {n}개 지점에서 분할합니다."
        if n > 0
        else "분할할 이벤트를 찾지 못했습니다."
    )
    return NlEditResponse(
        patch=[],
        summary=summary,
        kind="split",
        split=SplitDetectResult(
            scene_id=scene.scene_id, cuts_sec=cuts_rel, reasoning=reasoning,
            scene_duration_sec=duration,
        ),
    )


async def _handle_overlay_intent(project_id: str, req: NlEditRequest) -> NlEditResponse:
    """이벤트 기반 자동 텍스트+SFX 오버레이 생성. 720p 프록시로 비전 분석."""
    import time as _t
    from src.nl_editor.overlay_detector import detect_overlay_events
    from src.nl_editor.models import OverlayItem, OverlayDetectResult
    from .. import state as store
    from ..routers.project import _ensure_proxy

    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    scene = next((s for s in p.scenes if s.scene_id == req.target_scene_id), None)
    if not scene:
        raise HTTPException(404, f"씬을 찾을 수 없습니다: scene_id={req.target_scene_id}")
    if not scene.footage:
        raise HTTPException(400, "footage 없는 씬은 오버레이 자동 추가 불가")
    fname = getattr(scene.footage, "file", None)
    if not fname:
        raise HTTPException(400, "비디오 footage만 자동 오버레이 가능합니다")

    proxy = _ensure_proxy(project_id, fname)
    if proxy is None:
        raise HTTPException(400, "이 footage의 720p 프록시를 만들 수 없습니다(비디오가 아닐 수 있음)")

    # SFX 후보 수집 (studio_assets 중 audio 타입)
    sfx_candidates = [
        {"asset_id": a.asset_id, "label": a.label or "", "filename": a.filename or ""}
        for a in (p.studio_assets or [])
        if getattr(a, "asset_type", None) == "audio"
    ]

    cs = getattr(scene.footage, "clip_start_sec", None)
    ce = getattr(scene.footage, "clip_end_sec", None)
    # 씬 구간만 추출해 Gemini에 전달 — 분할된 씬은 원본 전체가 아닌 그 구간만 봐야 정확.
    clip = _extract_scene_clip(proxy, cs, ce)
    duration = float(ce) - float(cs or 0.0) if ce is not None else None
    ctx = {"clip_start_sec": 0.0, "clip_end_sec": duration, "scene_id": scene.scene_id}

    # #355 — 프로젝트 설정 모델을 detector 호출에 전달.
    model_id = _project_gemini_model(project_id)
    loop = asyncio.get_event_loop()
    raw_overlays, reasoning, meta = await loop.run_in_executor(
        None, lambda: detect_overlay_events(str(clip), req.instruction, ctx, sfx_candidates, model=model_id)
    )
    if clip != proxy:
        clip.unlink(missing_ok=True)

    # time_sec는 이미 추출된 씬 클립의 0초 기준(_extract_scene_clip 보정 덕). 범위 밖만 거른다.
    items: list[OverlayItem] = []
    ts_now = int(_t.time() * 1000)
    for i, o in enumerate(raw_overlays):
        rel = round(float(o["time_sec"]), 2)
        if rel < 0:
            continue
        if duration is not None and rel >= duration:
            continue
        items.append(OverlayItem(
            id=f"text_ai_{ts_now}_{i}",
            text=o["text"],
            x=o["x"], y=o["y"],
            font_family=o["font_family"],
            color=o["color"],
            start_sec=rel,
            end_sec=rel + 2.0,  # 기본 2초 노출
            sfx_asset_id=o.get("sfx_asset_id"),
            sfx_volume=0.7,
        ))

    _record_nl_edit_cost(project_id, meta)

    n = len(items)
    with_sfx = sum(1 for it in items if it.sfx_asset_id)
    summary = (
        f"씬 {scene.scene_id}에 {n}개 텍스트 오버레이를 추가합니다 (SFX 연결 {with_sfx}개)."
        if n > 0
        else "추가할 이벤트를 찾지 못했습니다."
    )
    return NlEditResponse(
        patch=[],
        summary=summary,
        kind="overlay",
        overlay=OverlayDetectResult(
            scene_id=scene.scene_id, overlays=items, reasoning=reasoning,
        ),
    )


@router.post("/{project_id}/nl-edit/apply", response_model=NlEditApplyResponse)
async def nl_edit_apply(project_id: str, req: NlEditApplyRequest) -> NlEditApplyResponse:
    """사용자가 미리보기를 승인 → patch 를 실제 RenderSpec 에 적용 + snapshot 저장."""
    lock = _get_lock(project_id)
    if lock.locked():
        raise HTTPException(409, "같은 프로젝트에 다른 nl-edit 요청이 진행 중입니다.")

    async with lock:
        payload = _load_render_spec_payload(project_id)
        # 1) snapshot 저장 (현재 상태)
        snapshot_id = _save_snapshot(project_id, payload)

        # 2) patch 적용 + 검증
        try:
            _, new_payload = apply_patch_to_specs(payload, req.patch)
        except PatchApplyError as e:
            # snapshot 만 남고 캐시는 그대로 — 사용자 데이터 보호.
            raise HTTPException(422, f"patch 적용 실패: {e}")

        # 3) 새 payload 캐시 갱신
        _save_render_spec_payload(project_id, new_payload)

        # 4) #335 — RenderSpec → SceneCard 역동기화. 프론트가 IndexedDB Project.scenes
        # 를 갱신할 수 있도록 함께 반환. 변환 불가 항목(filters/motions/narration_text 등)
        # 은 기존 SceneCard 를 그대로 유지한다.
        p = store.get_project(project_id)
        scene_cards = (
            sync_scene_cards_from_render_spec(new_payload, p) if p else []
        )

        applied_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        return NlEditApplyResponse(
            scenes=new_payload.get("scenes", []),
            scene_cards=scene_cards,
            snapshot_id=snapshot_id,
            applied_at=applied_at,
        )


@router.post("/{project_id}/nl-edit/undo", response_model=NlEditUndoResponse)
async def nl_edit_undo(project_id: str) -> NlEditUndoResponse:
    """가장 최근 snapshot 으로 RenderSpec 롤백."""
    lock = _get_lock(project_id)
    if lock.locked():
        raise HTTPException(409, "같은 프로젝트에 다른 nl-edit 요청이 진행 중입니다.")

    async with lock:
        popped = _pop_latest_snapshot(project_id)
        if popped is None:
            raise HTTPException(404, "되돌릴 수 있는 snapshot 이 없습니다.")
        snapshot_id, payload, has_more = popped

        # 현재 캐시를 snapshot 으로 덮어쓴다.
        _save_render_spec_payload(project_id, payload)

        # #335 — undo 시에도 SceneCard 를 함께 롤백할 수 있도록 변환 결과 반환.
        p = store.get_project(project_id)
        scene_cards = (
            sync_scene_cards_from_render_spec(payload, p) if p else []
        )

        return NlEditUndoResponse(
            scenes=payload.get("scenes", []),
            scene_cards=scene_cards,
            snapshot_id=snapshot_id,
            has_more_snapshots=has_more,
            rolled_back_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        )


@router.post("/{project_id}/nl-edit/cache/release")
async def nl_edit_cache_release(project_id: str) -> dict:
    """#317 — 세션 종료(패널 닫힘) 시 컨텍스트 캐시 명시 해제.

    저장 비용 절감을 위해 사용. TTL 자동 만료(1시간)도 안전망으로 작동하므로 본
    엔드포인트가 실패해도 추가 비용은 제한적이다.
    """
    try:
        release_project_cache(project_id)
        return {"released": True}
    except Exception as e:
        # 캐시 해제 실패가 사용자 흐름을 막아서는 안 됨.
        return {"released": False, "error": str(e)}


@router.get("/{project_id}/intent-log")
async def get_intent_log(project_id: str, limit: int = Query(50, ge=1, le=1000)) -> list[dict]:
    """최근 N 개 분류 로그를 JSON 배열로 반환 (#359).

    - 회전 파일(intent_log.YYYYMMDD.jsonl) 까지 모두 합쳐 최신순으로 N개 추림.
    - 파손된 라인은 조용히 건너뛴다.
    - 검증/디버그 도구용. 프론트 UI 는 후속 작업에서 노출.
    """
    cache_dir = _project_cache_dir(project_id)
    # 활성 파일 + 회전 파일 모두 수집. 회전 파일은 날짜·suffix 순으로 정렬.
    files: list[Path] = []
    active = cache_dir / "intent_log.jsonl"
    if active.exists():
        files.append(active)
    for f in sorted(cache_dir.glob("intent_log.*.jsonl")):
        if f != active:
            files.append(f)
    entries: list[dict] = []
    for f in files:
        try:
            for raw in f.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue
    # 최신순(ts 기준 내림차순). ts 가 누락된 항목은 맨 뒤로.
    entries.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return entries[: int(limit)]


__all__ = ["router"]
