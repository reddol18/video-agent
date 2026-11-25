from __future__ import annotations
import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import json
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Form, Response, Header, Request
from fastapi.responses import FileResponse

# 프로젝트 루트를 sys.path에 추가해 기존 src/ 모듈 임포트
_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

from ..models import (
    CreateProjectRequest, ParseScenarioRequest, IndexFootageRequest,
    Project, SceneCard, FootageItem, FootageChunk, TextOverlay as TextOverlayModel,
)
from .. import state as store
from .. import keyed_locks as _keyed_locks
from ..ws.progress import create_task_id, push_sync, register_bg_task
from ..footage_paths import (
    SUFFIX_PAREN,
    invalidate_footage_caches,
    normalize_policy,
    resolve_footage_name,
)

router = APIRouter(prefix="/api/projects", tags=["project"])


# ── CRUD ───────────────────────────────────────────────────────────────────

@router.post("", response_model=Project)
def create_project(req: CreateProjectRequest):
    return store.create_project(req.name, req.footage_dir, req.scenario_text, req.auto_footage_dir)


@router.post("/{project_id}/duplicate", response_model=Project)
def duplicate_project(project_id: str):
    """프로젝트 복제. footage_dir은 원본과 동일 경로를 공유(파일 미복사)."""
    dup = store.duplicate_project(project_id)
    if not dup:
        raise HTTPException(404, "원본 프로젝트를 찾을 수 없습니다")
    return dup


@router.get("", response_model=list[Project])
def list_projects():
    return store.list_projects()


@router.get("/{project_id}", response_model=Project)
def get_project(project_id: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    return p


@router.patch("/{project_id}", response_model=Project)
def update_project(project_id: str, body: dict):
    # #289 — bgm_tracks가 들어오면 BgmTrack 모델로 검증/변환한다.
    # (state.update_project는 setattr만 하므로 검증을 라우터에서 명시적으로 수행.)
    if "bgm_tracks" in body and body["bgm_tracks"] is not None:
        from ..models import BgmTrack
        try:
            body["bgm_tracks"] = [
                BgmTrack(**t) if not isinstance(t, BgmTrack) else t
                for t in body["bgm_tracks"]
            ]
        except Exception as e:
            raise HTTPException(400, f"bgm_tracks 검증 실패: {e}")
    if "studio_assets" in body and body["studio_assets"] is not None:
        from ..models import FootageAsset as FootageAssetModel
        try:
            body["studio_assets"] = [
                FootageAssetModel(**a) if not isinstance(a, FootageAssetModel) else a
                for a in body["studio_assets"]
            ]
        except Exception as e:
            raise HTTPException(400, f"studio_assets 검증 실패: {e}")
    # state.update_project 가 선언 타입으로 검증한다(scenes 등). 실패는 400 으로 변환.
    try:
        p = store.update_project(project_id, **body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    return p


def _purge_narration_wavs_for_empty_scenes(project_id: str, project) -> None:
    """나레이션이 없는 씬 번호에 남아 있는 WAV 를 삭제하고 stale tts 메타를 정리한다.

    WAV 파일명이 scene_id 기반이라, 씬 삽입/삭제로 번호가 밀리면 파일은 옛 번호에
    그대로 남는다. 그 자리에 "나레이션 없는 씬"(중간 이미지 삽입 씬 등)이 오면
    앞 씬의 음성이 그 씬의 것으로 오인되어 미리보기에서 엉뚱한 소리가 난다.

    나레이션(텍스트·세그먼트)이 전혀 없는 씬만 대상이므로, 정상 씬의 음성은
    절대 지우지 않는다. 삭제된 음성은 TTS 재생성으로 언제든 복구 가능하다.
    """
    try:
        narr_dir = store.get_output_dir(project_id) / "narration"
        if not narr_dir.exists():
            return
        changed = False
        for sc in (project.scenes or []):
            has_text = bool((getattr(sc, "narration_text", "") or "").strip())
            has_segs = bool(getattr(sc, "narration_segments", None))
            if has_text or has_segs:
                continue
            sid = sc.scene_id
            for name in (
                f"scene_{sid:03d}_seg_seg_0.wav",
                f"narration_scene_{sid:03d}.wav",
            ):
                f = narr_dir / name
                if f.exists():
                    try:
                        f.unlink()
                        print(f"[purge] scene={sid} 나레이션 없는 씬의 잔재 WAV 삭제: {name}", flush=True)
                    except Exception:
                        pass
            # 나레이션이 없는데 tts 메타만 남아 있으면 UI 가 "음성 있음" 으로 오인한다.
            if getattr(sc, "tts", None) is not None:
                sc.tts = None
                changed = True
        if changed:
            store._dump(project)
    except Exception as e:
        # 정리는 보조 기능이므로 실패해도 register 자체를 깨지 않는다.
        print(f"[purge] 빈 씬 WAV 정리 실패(무시): {e}", flush=True)


def _preserve_ready_narration_meta(
    *,
    scene_id: int,
    existing: "SceneCard | None",
    new_segments: list,
) -> None:
    """#412 — register 의 "퇴행 쓰기" 로부터 서버의 정상 TTS 메타를 보호한다.

    사고 경위: 브라우저가 낡은/빈 스냅샷(status=idle, synced=False, duration_sec=0.0)을
    들고 `PUT /register` 를 쏘면 서버의 정상 메타가 그대로 지워지고, 렌더 시
    `_tts_stale()` 이 전 씬을 stale 로 판정 → 유료 TTS 전량 재생성.

    정책 (텍스트가 동일한 segment 에 한정):
      - 서버가 status=="ready" and synced and duration_sec>0 인데
        클라가 그보다 퇴행한 값을 보냈으면 → 서버의
        status/synced/duration_sec/audio_url 을 유지한다.
      - 텍스트가 바뀐 segment 는 손대지 않는다 → `_purge_stale_narration_wavs` 의
        stale 처리(wav 삭제 + synced=False)가 정상 동작해야 하기 때문.
      - 클라가 더 새로운 정보(ready/synced/duration>0)를 보낸 경우도 손대지 않는다
        → #411 의 "비동기 generate 중 register 로 영원히 idle" 회귀 방지.

    #409 와의 차이: 디스크 wav 존재 여부를 진실로 삼지 않는다. 오직 서버 메타 ↔ 클라
    메타의 방향성만 본다. 따라서 wav 가 아직 없는 상태에서 register 가 들어와도
    status 가 강제로 idle 이 되는 일은 없다.
    """
    if existing is None or not isinstance(new_segments, list):
        return
    try:
        old_by_id = {s.id: s for s in (existing.narration_segments or []) if getattr(s, "id", None)}
        if not old_by_id:
            return
        for seg in new_segments:
            seg_id = getattr(seg, "id", None)
            if not seg_id:
                continue
            old = old_by_id.get(seg_id)
            if old is None:
                continue
            # 텍스트가 바뀌었으면 stale 처리 대상 — 서버 값을 되살리면 안 된다.
            if (getattr(old, "text", "") or "").strip() != (getattr(seg, "text", "") or "").strip():
                continue
            # 서버가 "정상 생성됨" 상태가 아니면 보존할 것이 없다.
            server_ready = (
                getattr(old, "status", None) == "ready"
                and bool(getattr(old, "synced", False))
                and float(getattr(old, "duration_sec", 0.0) or 0.0) > 0.0
            )
            if not server_ready:
                continue
            # 클라가 서버와 같거나 더 새로운 정보를 보냈으면 그대로 수용.
            client_ready = (
                getattr(seg, "status", None) == "ready"
                and bool(getattr(seg, "synced", False))
                and float(getattr(seg, "duration_sec", 0.0) or 0.0) > 0.0
            )
            if client_ready:
                continue
            # 퇴행 쓰기 — 서버 값 유지.
            seg.status = old.status
            seg.synced = True
            seg.duration_sec = float(old.duration_sec or 0.0)
            if getattr(old, "audio_url", ""):
                seg.audio_url = old.audio_url
            if getattr(old, "voice_name", None) is not None and getattr(seg, "voice_name", None) is None:
                seg.voice_name = old.voice_name
            seg.error_message = None
            print(
                f"[register#412] scene={scene_id} seg={seg_id} 클라 퇴행 메타 감지 "
                f"→ 서버 ready 메타 보존 (dur={seg.duration_sec:.2f}s)",
                flush=True,
            )
    except Exception as _err:
        # 보호 로직 실패가 register 자체를 깨뜨리지 않도록.
        print(f"[register#412] ready 메타 보존 실패(무시): {_err}", flush=True)


def _purge_stale_narration_wavs(
    *,
    project_id: str,
    scene_id: int,
    existing: "SceneCard",
    new_narration_text: str | None,
    new_segments: list | None,
) -> None:
    """#404 — register 시점에 stale narration WAV 를 즉시 삭제하고 synced 플래그를 강제 False 로.

    재번호/삽입/삭제로 같은 scene_id 의 텍스트가 디스크 wav 와 어긋나면,
    그 wav 는 다른 씬에서 흘러들어온 잔재일 수 있다. 이 함수는:

      1) legacy `narration_scene_{scene_id:03d}.wav` 에 대해
         existing.narration_text != new_narration_text 이면 파일 삭제.
      2) 신규 `scene_{scene_id:03d}_seg_{seg_id}.wav` 에 대해 segment.id 별로
         existing text 와 new text 가 다르면 파일 삭제 + new_segments[i].synced=False.

    new_*가 None 이면 클라이언트가 해당 필드를 보내지 않은 것이므로 비교 대상이 없다 → skip.
    """
    try:
        narr_dir = store.get_output_dir(project_id) / "narration"

        # 1) legacy 단일 wav (= narration_text 기반)
        # #406 — narration_text 와 seg_0 는 "기본 나레이션" 동일 의미를 두 곳에 저장하므로,
        # narration_text 변경 시 seg_0 의 text/wav/synced 도 함께 갱신해야 한다.
        # 클라가 segments 를 보내지 않은 경우(`new_segments is None`)에도 seg_0 동기화.
        if new_narration_text is not None:
            old_text = (existing.narration_text or "").strip()
            new_text = (new_narration_text or "").strip()
            if old_text != new_text:
                legacy = narr_dir / f"narration_scene_{scene_id:03d}.wav"
                if legacy.exists():
                    try:
                        legacy.unlink()
                        print(
                            f"[register#404] scene={scene_id} narration_text 변경 감지 → "
                            f"legacy wav 삭제: {legacy.name} "
                            f"| old={old_text[:30]!r} | new={new_text[:30]!r}",
                            flush=True,
                        )
                    except Exception:
                        pass
                # legacy 메타(existing.tts) 도 synced=False 로 강제.
                if existing.tts is not None and existing.tts.synced:
                    existing.tts.synced = False
        # #406 — seg_0 와 narration_text 동기화 (항상 검사, 모든 경로).
        # narration_text 와 seg_0 는 "기본 나레이션" 동일 의미를 두 곳에 저장하므로,
        # 둘이 어긋난 상태는 어떤 경로로든 잘못된 상태. register 호출 시마다 미스매치를
        # 감지해 새 narration_text 로 seg_0 를 강제 동기화 + wav 삭제 + synced=False.
        #
        # 중요: 클라가 segments 를 함께 보낸 경우(`new_segments is not None`)에도 동기화한다.
        # 이유: 프론트가 narration_text 만 갱신하고 seg_0.text 는 옛 텍스트 그대로 register
        # 에 보내는 race 가 실제로 발생. patch_scene 의 #406 가 동기화한 결과가 직후의
        # register 호출에서 옛 segments 로 덮어쓰이는 회귀 발견(24번 씬 사례).
        #
        # new_segments 로 들어온 seg_0 객체와 existing.narration_segments 의 seg_0 객체가
        # 다를 수 있으므로, 양쪽 모두 동기화한다.
        new_text_for_seg0 = (new_narration_text or "").strip() if new_narration_text is not None else None
        if new_text_for_seg0 is not None:
            # 양쪽 segments 컬렉션에서 seg_0 찾아 동기화
            seg0_candidates = []
            if existing.narration_segments:
                s = next((x for x in existing.narration_segments if x.id == "seg_0"), None)
                if s is not None:
                    seg0_candidates.append(s)
            if isinstance(new_segments, list):
                s = next((x for x in new_segments if getattr(x, "id", None) == "seg_0"), None)
                if s is not None:
                    seg0_candidates.append(s)

            need_wav_purge = False
            for seg0 in seg0_candidates:
                seg0_text = (getattr(seg0, "text", "") or "").strip()
                if seg0_text != new_text_for_seg0:
                    seg0.text = new_narration_text
                    seg0.synced = False
                    seg0.status = "idle"
                    need_wav_purge = True

            if need_wav_purge:
                seg0_wav = narr_dir / f"scene_{scene_id:03d}_seg_seg_0.wav"
                if seg0_wav.exists():
                    try:
                        seg0_wav.unlink()
                        print(
                            f"[register#406] scene={scene_id} narration_text 와 "
                            f"seg_0.text 미스매치 → seg_0 동기화 + wav 삭제: {seg0_wav.name}",
                            flush=True,
                        )
                    except Exception:
                        pass
                # legacy wav 도 옛 텍스트 잔재 가능성. 같이 삭제.
                legacy_wav = narr_dir / f"narration_scene_{scene_id:03d}.wav"
                if legacy_wav.exists():
                    try:
                        legacy_wav.unlink()
                        print(
                            f"[register#406] scene={scene_id} legacy wav 도 옛 텍스트 "
                            f"잔재로 판단 → 삭제: {legacy_wav.name}",
                            flush=True,
                        )
                    except Exception:
                        pass
                if existing.tts is not None:
                    existing.tts.synced = False

        # 2) 신규 segment 별 wav (= narration_segments 기반)
        if isinstance(new_segments, list):
            old_by_id = {s.id: (s.text or "").strip() for s in (existing.narration_segments or [])}
            for seg in new_segments:
                seg_id = getattr(seg, "id", None)
                if not seg_id:
                    continue
                new_text = (getattr(seg, "text", "") or "").strip()
                old_text = old_by_id.get(seg_id)
                if old_text is None:
                    # 신규 segment_id — 옛 wav 가 같은 이름으로 존재할 수 있으므로
                    # 보수적으로 확인: 디스크에 동일 경로 wav 가 이미 있고 새 seg 가
                    # synced=False 라면 우리가 만든 것이 아니므로 잔재일 가능성 → 삭제.
                    wav = narr_dir / f"scene_{scene_id:03d}_seg_{seg_id}.wav"
                    if wav.exists() and not getattr(seg, "synced", False):
                        try:
                            wav.unlink()
                            print(
                                f"[register#404] scene={scene_id} seg={seg_id} "
                                f"신규 segment 인데 동명 wav 존재 → 잔재로 판단 삭제",
                                flush=True,
                            )
                        except Exception:
                            pass
                    continue
                if old_text != new_text:
                    wav = narr_dir / f"scene_{scene_id:03d}_seg_{seg_id}.wav"
                    if wav.exists():
                        try:
                            wav.unlink()
                            print(
                                f"[register#404] scene={scene_id} seg={seg_id} text "
                                f"변경 감지 → wav 삭제: {wav.name}",
                                flush=True,
                            )
                        except Exception:
                            pass
                    # 새 segment 의 synced 도 False 로 강제 (audio_url 은 보존 — UI 폴백용).
                    try:
                        seg.synced = False
                        seg.status = "idle"
                    except Exception:
                        pass
    except Exception as _err:
        # 정리 실패는 register 자체를 깨지 않는다.
        print(f"[register#404] stale wav 정리 실패(무시): {_err}", flush=True)


@router.put("/{project_id}/register")
def register_project(project_id: str, body: dict):
    """브라우저 전용 프로젝트를 서버에 등록(upsert)한다.

    scenes 배열이 포함되면 각 씬의 narration_text / footage 등을 서버에 동기화한다.
    """
    from datetime import datetime, timezone
    from ..models import TtsMeta

    p = store.get_project(project_id)
    if not p:
        now = datetime.now(timezone.utc).isoformat()
        fd = store.DATA_DIR / project_id / "footage"
        fd.mkdir(parents=True, exist_ok=True)
        p = Project(
            project_id=project_id,
            name=body.get("name", ""),
            footage_dir=str(fd),
            scenario_text=body.get("scenario_text", ""),
            created_at=now,
            updated_at=now,
        )
        store._projects[p.project_id] = p
        store._dump(p)

    # ── 씬 대량 소실 차단 (2026-09-16 사고) ──────────────────────────────────
    # 브라우저가 stale/빈 스토어 상태로 register 를 한 번 쏘면 서버 씬이 통째로
    # 사라지던 사고(74씬 → 0씬)를 여기서 끊는다. 어떤 씬도 건드리기 전에 판정한다.
    #
    # 판정 기준은 state.is_dangerous_scene_loss (감사 로그 danger 판정과 같은 상수).
    # - `scenes` 키가 없거나 None → 씬 동기화 자체를 건너뛰는 경로이므로 검사 대상 아님.
    #   (Editor.tsx 의 registerProject(pid, "", undefined, ...) 가 이 경로를 쓴다.)
    # - 한두 개 삭제 같은 정상 편집은 통과한다. 50% 이상 소실만 막는다.
    # - 진짜 전체 삭제 의도라면 body 에 allow_bulk_scene_delete=true 를 넣어 명시한다.
    _incoming_scenes = body.get("scenes")
    if isinstance(_incoming_scenes, list) and not body.get(store.SCENE_BULK_DELETE_FLAG):
        _client_ids = {
            sd.get("scene_id")
            for sd in _incoming_scenes
            if isinstance(sd, dict) and sd.get("scene_id") is not None
        }
        # 삭제 후 남게 될 씬 수 = (기존 씬 ∩ 클라 목록) + 클라가 새로 추가한 씬
        _kept = len([s for s in p.scenes if s.scene_id in _client_ids])
        _added = len(_client_ids - {s.scene_id for s in p.scenes})
        _reason = store.is_dangerous_scene_loss(len(p.scenes), _kept + _added)
        if _reason:
            print(
                f"[register#bulk-delete] ⛔ {project_id} 씬 대량 삭제 거부: {_reason}",
                flush=True,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "scene_bulk_delete_blocked",
                    "message": (
                        f"씬 대량 삭제가 차단되었습니다. {_reason}. "
                        f"의도한 삭제라면 '{store.SCENE_BULK_DELETE_FLAG}': true 를 함께 보내세요."
                    ),
                    "server_scene_count": len(p.scenes),
                    "client_scene_count": len(_client_ids),
                    "required_flag": store.SCENE_BULK_DELETE_FLAG,
                },
            )

    # 씬 데이터 동기화 — narration_text, footage, tts 메타
    from ..models import ShapeOverlay as ShapeOverlayModel
    from ..models import GraphicOverlay as GraphicOverlayModel

    def _safe_parse_list(model_cls, items, label: str, scene_id):
        """검증 실패한 항목은 건너뛰고 로깅. 한 항목 때문에 register 전체가 깨지지 않도록."""
        out = []
        for it in items:
            if not isinstance(it, dict):
                out.append(it)
                continue
            try:
                out.append(model_cls(**it))
            except Exception as e:
                print(f"[register] scene={scene_id} {label} 항목 검증 실패 (스킵): {e}")
        return out

    # `scenes` 가 None(명시적 null)이면 "씬 동기화 없음" 으로 취급 — 빈 배열과 구분한다.
    scenes_data = body.get("scenes")
    if not isinstance(scenes_data, list):
        scenes_data = []
    for sd in scenes_data:
        scene_id = sd.get("scene_id")
        if scene_id is None:
            continue
        existing = next((s for s in p.scenes if s.scene_id == scene_id), None)
        kwargs: dict = {}
        if "narration_text" in sd:
            # #408 — register 가 옛 스냅샷으로 narration_text 를 덮어쓰는 race 방지.
            # 프론트가 여러 컴포넌트에서 동시에 updateProject 호출 시, 한쪽이 옛 store
            # 상태로 pushProjectState 를 보내 백엔드의 새 값을 덮어쓰는 사고 발생.
            # 정책: register 는 narration_text 의 "삭제/공백화" 를 차단한다.
            # - 새 값이 비어있는데 existing 이 비어있지 않으면 narration_text 변경 skip.
            # - 채워진 텍스트로 덮어쓰는 것은 그대로 허용 (정상 케이스).
            _new_nt = sd["narration_text"]
            _new_nt_str = (_new_nt if isinstance(_new_nt, str) else "").strip()
            _existing_nt = (getattr(existing, "narration_text", None) or "").strip() if existing else ""
            _existing_rev = int(getattr(existing, "narration_rev", 0) or 0) if existing else 0
            # 클라이언트가 "내가 읽었던 리비전". 옛 클라이언트는 이 값을 안 보내는데,
            # 그 경우 리비전 검사를 못 하므로 기존 동작(#408: 공백화만 차단)을 유지한다.
            _client_rev = sd.get("narration_rev")
            _has_rev = isinstance(_client_rev, int)

            if not _new_nt_str and _existing_nt:
                # #408 — 공백으로 지우는 쓰기는 항상 차단.
                print(
                    f"[register#408] scene={scene_id} narration_text 빈 값으로 덮어쓰기 차단 "
                    f"| existing={_existing_nt[:30]!r}",
                    flush=True,
                )
            elif (
                _has_rev
                and _new_nt_str != _existing_nt
                and _client_rev < _existing_rev
            ):
                # #stale-write — 클라이언트가 더 옛 리비전을 들고 있다. 거부한다.
                # 이게 없으면 새로고침/undo 복원 직후의 옛 스냅샷이 주기적 전체 push 로
                # 최신 대본을 덮어써 편집분이 조용히 사라진다(실측 913회/36씬).
                print(
                    f"[register#stale] scene={scene_id} 옛 리비전 쓰기 거부 "
                    f"| client_rev={_client_rev} < server_rev={_existing_rev} "
                    f"| keep={_existing_nt[:24]!r} reject={_new_nt_str[:24]!r}",
                    flush=True,
                )
            else:
                if _existing_nt and _new_nt_str != _existing_nt:
                    print(
                        f"[register#408] scene={scene_id} narration_text 변경 "
                        f"| existing={_existing_nt[:30]!r} → new={_new_nt_str[:30]!r}",
                        flush=True,
                    )
                    # 실제 내용이 바뀌었을 때만 리비전을 올린다.
                    kwargs["narration_rev"] = _existing_rev + 1
                kwargs["narration_text"] = _new_nt
        if "subtitle_text" in sd:
            # null/빈 문자열은 None으로 저장 → SBV에서 narration_text 폴백.
            st = sd["subtitle_text"]
            kwargs["subtitle_text"] = st if isinstance(st, str) and st.strip() else None
        if "title" in sd:
            kwargs["title"] = sd["title"]
        if "footage" in sd and sd["footage"] is not None:
            kwargs["footage"] = _footage_to_effect(sd["footage"], footage_dir=p.footage_dir or None)
        if "footage_speed" in sd:
            kwargs["footage_speed"] = sd["footage_speed"]
        if "footage_speed_mode" in sd:
            kwargs["footage_speed_mode"] = (
                sd["footage_speed_mode"] if sd["footage_speed_mode"] in ("ramp", "ramp_down") else "constant"
            )
        for _rk in ("footage_speed_ramp_hold", "footage_speed_ramp_accel"):
            if _rk in sd:
                try:
                    kwargs[_rk] = float(sd[_rk])
                except (TypeError, ValueError):
                    pass
        if "footage_speed_ramp_base" in sd:
            try:
                _rb = float(sd["footage_speed_ramp_base"])
                if _rb > 0:
                    kwargs["footage_speed_ramp_base"] = _rb
            except (TypeError, ValueError):
                pass
        if "footage_audio_volume" in sd:
            try:
                kwargs["footage_audio_volume"] = float(sd["footage_audio_volume"])
            except (TypeError, ValueError):
                pass
        if "tts_emotion" in sd:
            # #tts-emotion — 씬별 말투 지시문. 문자열만 허용, 공백은 제거.
            _te = sd["tts_emotion"]
            kwargs["tts_emotion"] = _te.strip() if isinstance(_te, str) else ""
        if "tts_speed" in sd:
            # 씬별 TTS 재생 속도 — Chirp3-HD 대상 atempo 후처리 배율.
            # 잘못된 값(음수/0/문자열)은 무시하고 기존 값을 유지한다.
            try:
                _ts = float(sd["tts_speed"])
                if _ts > 0:
                    kwargs["tts_speed"] = _ts
            except (TypeError, ValueError):
                pass
        # "삐 소리" 씬 단위 자동 효과 토글 + 볼륨.
        if "beep_on_narration_end" in sd:
            kwargs["beep_on_narration_end"] = bool(sd["beep_on_narration_end"])
        if "beep_volume" in sd:
            try:
                kwargs["beep_volume"] = max(0.0, min(1.0, float(sd["beep_volume"])))
            except (TypeError, ValueError):
                pass
        # "중간 이미지 삽입" 씬 마커 — 프론트가 단순 요소로 취급하기 위한 표식.
        if "is_insert_image_scene" in sd:
            kwargs["is_insert_image_scene"] = bool(sd["is_insert_image_scene"])
        if "text_overlays" in sd and isinstance(sd["text_overlays"], list):
            from ..models import TextOverlay as TextOverlayModel
            kwargs["text_overlays"] = _safe_parse_list(TextOverlayModel, sd["text_overlays"], "text_overlays", scene_id)
        # tags 동기화 (#212) — 문자열 배열 그대로 보존, 그 외 타입은 무시
        if "tags" in sd and isinstance(sd["tags"], list):
            kwargs["tags"] = [str(t) for t in sd["tags"] if isinstance(t, str)]
        # tts 메타 동기화 (duration_sec 보존)
        # 명시적 null이 들어오면 store에도 None으로 — 옛 메타 잔재 누수 차단.
        # 현재는 status=="ready"가 아닌 모든 경우(None/dirty 등)를 무시해 옛 메타가 보존되는 회귀가 있었다.
        tts_data = sd.get("tts")
        if "tts" in sd and sd["tts"] is None:
            kwargs["tts"] = None
        elif tts_data and tts_data.get("status") == "ready":
            kwargs["tts"] = TtsMeta(
                audio_url=tts_data.get("audio_url", f"/api/projects/{project_id}/tts/{scene_id}/audio"),
                duration_sec=float(tts_data.get("duration_sec", 0)),
                status="ready",
                synced=bool(tts_data.get("synced", False)),
            )
        # shape_overlays 동기화
        shape_overlays_data = sd.get("shape_overlays")
        if shape_overlays_data is not None and isinstance(shape_overlays_data, list):
            kwargs["shape_overlays"] = _safe_parse_list(ShapeOverlayModel, shape_overlays_data, "shape_overlays", scene_id)
        # graphic_overlays 동기화 (#205)
        graphic_overlays_data = sd.get("graphic_overlays")
        if graphic_overlays_data is not None and isinstance(graphic_overlays_data, list):
            kwargs["graphic_overlays"] = _safe_parse_list(GraphicOverlayModel, graphic_overlays_data, "graphic_overlays", scene_id)
        # filters 동기화 (#206) — 씬 전체 색감/무드 필터
        filters_data = sd.get("filters")
        if filters_data is not None and isinstance(filters_data, list):
            from ..models import SceneFilter as SceneFilterModel
            kwargs["filters"] = _safe_parse_list(SceneFilterModel, filters_data, "filters", scene_id)
        # motions 동기화 (#207) — 씬 다이나믹 효과 (shake/clip_zoom/slowmo)
        motions_data = sd.get("motions")
        if motions_data is not None and isinstance(motions_data, list):
            from ..models import SceneMotion as SceneMotionModel
            kwargs["motions"] = _safe_parse_list(SceneMotionModel, motions_data, "motions", scene_id)
        # mosaic_overlays 동기화 (#264) — 영역 모자이크/블러 오버레이
        mosaic_overlays_data = sd.get("mosaic_overlays")
        if mosaic_overlays_data is not None and isinstance(mosaic_overlays_data, list):
            from ..models import MosaicOverlay as MosaicOverlayModel
            kwargs["mosaic_overlays"] = _safe_parse_list(MosaicOverlayModel, mosaic_overlays_data, "mosaic_overlays", scene_id)
        # image_overlays 동기화 — footage 스튜디오의 이미지 자산을 씬 위에 합성하는 오버레이
        image_overlays_data = sd.get("image_overlays")
        if image_overlays_data is not None and isinstance(image_overlays_data, list):
            from ..models import ImageOverlay as ImageOverlayModel
            kwargs["image_overlays"] = _safe_parse_list(ImageOverlayModel, image_overlays_data, "image_overlays", scene_id)
        # speed_lines 동기화 — 집중선(속도선) 오버레이
        speed_lines_data = sd.get("speed_lines")
        if speed_lines_data is not None and isinstance(speed_lines_data, list):
            from ..models import SpeedLinesOverlay as SpeedLinesOverlayModel
            kwargs["speed_lines"] = _safe_parse_list(SpeedLinesOverlayModel, speed_lines_data, "speed_lines", scene_id)
        # 편집 지시 코멘트 동기화 — 전체 프로젝트 sync 가 코멘트를 지우지 않도록 반드시 처리.
        # 명시적 None 은 "전부 삭제" 의도로 보고 빈 배열로 반영한다.
        if "comments" in sd:
            comments_data = sd.get("comments")
            if comments_data is None:
                kwargs["comments"] = []
            elif isinstance(comments_data, list):
                from ..models import SceneComment as SceneCommentModel
                kwargs["comments"] = _safe_parse_list(SceneCommentModel, comments_data, "comments", scene_id)
        # #382 — narration_segments 동기화. 명시적 None 은 빈 배열로 처리하여 사용자가
        # 전체 삭제한 의도를 반영한다.
        # #411 — register 는 클라가 보낸 segments 메타를 옛 백엔드 메타로 덮어쓰지 않는다.
        # (이전 #409 는 wav 디스크 진실로 status 강제했으나 generate 가 비동기라 wav 가
        # 아직 안 만들어진 사이에 register 가 들어오면 status=idle 로 리셋 → 영원히 idle
        # 회귀. 사용자 보고 "로컬 보존이 정답" 의 원인.)
        # 새 정책: 클라가 보낸 status/synced/duration_sec/audio_url 을 그대로 받는다.
        # text 변경에 따른 stale wav 정리는 _purge_stale_narration_wavs 가 별도로 처리.
        #
        # #412 — 단, "퇴행(regression) 쓰기" 는 막는다.
        # 브라우저가 낡은/빈 스냅샷(status=idle, synced=False, duration=0)을 들고 register
        # 를 한 번 쏘면 서버의 정상 TTS 메타가 지워지고, 렌더 시 _tts_stale() 이 전 씬을
        # stale 로 판정해 유료 TTS 를 전량 재생성하는 사고가 실제로 발생했다.
        # 정책: 텍스트가 동일한 segment 에 한해, 서버가 ready/synced/duration>0 인데
        #       클라가 그보다 퇴행한 값을 보내면 서버 값을 유지한다.
        #       텍스트가 바뀐 segment 는 예외(클라 값 수용 → stale 처리가 정상 동작).
        #       클라가 더 새로운 정보(ready/synced/duration>0)를 보낸 경우도 그대로 수용
        #       → #411 의 "비동기 generate 중 register" 회귀는 재발하지 않는다.
        if "narration_segments" in sd:
            seg_data = sd.get("narration_segments")
            if seg_data is None:
                kwargs["narration_segments"] = []
            elif isinstance(seg_data, list):
                from ..models import NarrationSegment as NarrationSegmentModel
                parsed = _safe_parse_list(
                    NarrationSegmentModel, seg_data, "narration_segments", scene_id,
                )
                _preserve_ready_narration_meta(
                    scene_id=scene_id, existing=existing, new_segments=parsed,
                )
                kwargs["narration_segments"] = parsed
        # #404 — stale narration wav 차단.
        # 씬 재번호/삽입/삭제로 같은 scene_id 의 narration_text/segment.text 가
        # 디스크 wav 와 어긋나면, 그 wav 는 다른 씬의 잔재일 수 있다.
        # 정책: existing.narration_text / existing.narration_segments[*].text 와
        #       클라이언트 신규 text 가 다르면 → 해당 wav 파일 즉시 삭제 + 신규
        #       segment 의 synced=False 강제 → 사용자의 다음 미리보기/렌더 때
        #       자동 재생성되도록 한다.
        if existing is not None:
            _purge_stale_narration_wavs(
                project_id=project_id,
                scene_id=scene_id,
                existing=existing,
                new_narration_text=kwargs.get("narration_text"),
                new_segments=kwargs.get("narration_segments"),
            )
        if existing:
            if kwargs:
                store.update_scene(project_id, scene_id, **kwargs)
        else:
            # 씬이 없으면 생성
            new_scene = SceneCard(scene_id=scene_id, **kwargs)
            p.scenes.append(new_scene)
            store._dump(p)

    # 씬 목록 동기화 — 클라이언트가 보낸 scene_id 목록에 없는 기존 씬은 서버에서 제거.
    # 이렇게 해야 클라이언트에서 씬을 삭제했을 때 서버 상태가 진실의 출처와 일치한다.
    # body 에 ``scenes`` 키가 있고 list 인 경우에만 동기화한다 — 빈 배열([]) 도 "모두 삭제"
    # 의도로 처리해야 한다. ``if scenes_data:`` 는 빈 배열을 falsy 로 인식해 전체 삭제 후
    # 서버측 씬이 남아 새로고침 시 되살아나는 회귀를 만들었다.
    # 단, 값이 None(명시적 null)이면 "씬 정보 미포함" 이므로 동기화하지 않는다.
    # (`"scenes" in body` 만 보면 null 이 빈 배열처럼 취급돼 전체 삭제로 이어진다.)
    if isinstance(body.get("scenes"), list):
        client_scene_ids = {sd.get("scene_id") for sd in scenes_data if sd.get("scene_id") is not None}
        p = store.get_project(project_id) or p
        before = len(p.scenes)
        p.scenes = [s for s in p.scenes if s.scene_id in client_scene_ids]
        # 씬 순서 일관성 보장 — 신규 append/제거 후 항상 scene_id 오름차순으로 정리.
        # 렌더링은 list 순서를 그대로 따라가므로 여기서 정렬해두지 않으면
        # 마지막 추가된 씬이 맨 앞에 박히는 등 영상/텍스트 위치가 어긋난다.
        prev_order = [s.scene_id for s in p.scenes]
        p.scenes.sort(key=lambda s: s.scene_id)
        new_order = [s.scene_id for s in p.scenes]
        if len(p.scenes) != before or prev_order != new_order:
            store._dump(p)

        # 씬 삽입/삭제로 번호가 밀리면 디스크의 WAV 는 옛 번호 그대로 남는다.
        # (WAV 파일명이 scene_id 기반 — `scene_{id:03d}_seg_*.wav`, `narration_scene_{id:03d}.wav`)
        # 그 결과 "나레이션이 없는 씬"(예: 중간 이미지 삽입 씬) 자리에 앞 씬의 음성이
        # 그대로 남아 미리보기에서 엉뚱한 소리가 나던 회귀가 있었다.
        # #404 purge 는 "텍스트가 바뀐 경우" 만 다루므로 이 경우를 놓친다 → 여기서 보정한다.
        _purge_narration_wavs_for_empty_scenes(project_id, p)

    # output_width/height 동기화
    out_w = body.get("output_width")
    out_h = body.get("output_height")
    if out_w and out_h:
        store.update_project(project_id, output_width=int(out_w), output_height=int(out_h))

    # 프로젝트 메타 동기화 — name, scenario_text, tts_voice, status
    proj_updates: dict = {}
    # 빈 name("")은 무시 — 미리보기 등에서 name 없이 register를 호출해도
    # 서버에 저장된 기존 이름을 덮어쓰지 않도록 방어한다.
    if "name" in body and isinstance(body["name"], str) and body["name"].strip():
        proj_updates["name"] = body["name"]
    if "scenario_text" in body and isinstance(body["scenario_text"], str):
        proj_updates["scenario_text"] = body["scenario_text"]
    if "tts_voice" in body and isinstance(body["tts_voice"], str):
        proj_updates["tts_voice"] = body["tts_voice"]
    if "status" in body and isinstance(body["status"], str):
        proj_updates["status"] = body["status"]
    # 수동 시리즈 그룹명. 빈 문자열 허용(시리즈 해제 → "시리즈 미편성"). 앞뒤 공백 정리.
    if "series" in body and isinstance(body["series"], str):
        proj_updates["series"] = body["series"].strip()
    # 스플릿 씬 디졸브 강제 적용 옵션 — bool 캐스팅 후 그대로 전달(setattr 패스스루).
    # 옛 키(force_sibling_dissolve)도 호환 처리: 새 키가 없을 때만 옛 키를 새 키로 매핑.
    if "force_split_scene_dissolve" in body:
        proj_updates["force_split_scene_dissolve"] = bool(body["force_split_scene_dissolve"])
    elif "force_sibling_dissolve" in body:
        proj_updates["force_split_scene_dissolve"] = bool(body["force_sibling_dissolve"])
    # #355 — 프로젝트 단위 Gemini 모델 선택. 빈 문자열은 default_model 폴백 의도.
    if "gemini_model" in body and isinstance(body["gemini_model"], str):
        proj_updates["gemini_model"] = body["gemini_model"]
    # 프로젝트 전역 배경색(letterbox 여백). 빈 문자열/None 은 기본 "black" 으로.
    if "bg_color" in body and isinstance(body["bg_color"], str):
        proj_updates["bg_color"] = body["bg_color"].strip() or "black"
    if proj_updates:
        store.update_project(project_id, **proj_updates)

    # studio_assets 동기화 (#복제) — 클라이언트가 IndexedDB에 가진 asset 메타를 서버로 푸시
    studio_assets_data = body.get("studio_assets")
    if isinstance(studio_assets_data, list):
        from ..models import FootageAsset as FootageAssetModel
        store.update_project(project_id, studio_assets=_safe_parse_list(FootageAssetModel, studio_assets_data, "studio_assets", -1))

    # bgm_tracks 동기화
    bgm_tracks_data = body.get("bgm_tracks")
    if isinstance(bgm_tracks_data, list):
        from ..models import BgmTrack as BgmTrackModel
        store.update_project(project_id, bgm_tracks=_safe_parse_list(BgmTrackModel, bgm_tracks_data, "bgm_tracks", -1))

    # 화자 사전 동기화 — 키가 없으면 건드리지 않는다(서버 값 보존).
    # 여기서 빠뜨리면 프론트의 주기적 전체 push 때마다 화자가 조용히 사라진다.
    speakers_data = body.get("speakers")
    if isinstance(speakers_data, list):
        from ..models import Speaker as SpeakerModel
        store.update_project(project_id, speakers=_safe_parse_list(SpeakerModel, speakers_data, "speakers", -1))

    p = store.get_project(project_id) or p

    # 잔재 디스크 정리 — 활성 scene_id 외의 옛 narration WAV / preview 파일 제거.
    # 옛 wav가 남으면 같은 번호로 새 씬이 생긴 뒤 잘못된 음성이 재생되는 회귀 발생.
    # 패턴 매칭은 `narration_scene_NNN.wav` / `scene_NNN.mp4` 로 한정 — `_narr_mixed.wav`
    # 같은 파생 파일은 절대 건드리지 않는다.
    try:
        import re as _re
        _active_ids = {s.scene_id for s in p.scenes}
        _narr_dir = store.get_output_dir(project_id) / "narration"
        _preview_dir = store.get_cache_dir(project_id) / "previews"
        _wav_pat = _re.compile(r"^narration_scene_(\d{3})\.wav$")
        # #382 — segment 단위 신규 경로 ``scene_NNN_seg_{seg_id}.wav`` 도 정리 대상.
        _seg_wav_pat = _re.compile(r"^scene_(\d{3})_seg_[A-Za-z0-9_-]+\.wav$")
        _prev_pat = _re.compile(r"^scene_(\d{3})\.mp4$")
        if _narr_dir.exists():
            for _f in _narr_dir.iterdir():
                _m = _wav_pat.match(_f.name)
                if _m and int(_m.group(1)) not in _active_ids:
                    try:
                        _f.unlink()
                    except Exception:
                        pass
                    continue
                _m_seg = _seg_wav_pat.match(_f.name)
                if _m_seg and int(_m_seg.group(1)) not in _active_ids:
                    try:
                        _f.unlink()
                    except Exception:
                        pass
        if _preview_dir.exists():
            for _f in _preview_dir.iterdir():
                _m = _prev_pat.match(_f.name)
                if _m and int(_m.group(1)) not in _active_ids:
                    try:
                        _f.unlink()
                    except Exception:
                        pass
    except Exception as _err:
        print(f"[register] 잔재 디스크 정리 실패(무시): {_err}", flush=True)

    # footage 파일 존재 여부 맵 — 프론트에서 불필요한 업로드 스킵용
    footage_dir = Path(p.footage_dir) if p.footage_dir else store.DATA_DIR / project_id / "footage"
    footage_exists: dict[int, bool] = {}
    for s in p.scenes:
        fname = getattr(s.footage, "file", None) or getattr(s.footage, "image", None) if s.footage else None
        if fname:
            footage_exists[s.scene_id] = (footage_dir / fname).exists()
    return {**p.dict(), "footage_exists": footage_exists}


@router.post("/{project_id}/scenes/{scene_id}/narration-flush")
async def narration_flush(project_id: str, scene_id: int, request: Request):
    """#narration-flush — 새로고침/탭닫기 직전 대본을 마지막으로 받아 저장한다.

    프론트는 대본 입력을 800ms 디바운스로 PATCH 한다. 그 사이에 페이지가
    사라지면 마지막 타건이 서버에 도달하지 못해, 다시 열었을 때 "치다 만 텍스트"
    가 남는다(사용자 보고, 2일 연속).

    unload 시점에는 일반 fetch 가 취소되므로 프론트가 `navigator.sendBeacon` 을
    쓰는데, beacon 은 POST 만 지원한다. 그래서 PATCH 와 별도로 이 경로를 둔다.

    저장 정책은 PATCH 와 동일:
      · 공백만 있는 값은 무시(기존 대본을 지우지 않는다)
      · 실제로 바뀐 경우에만 narration_rev 를 올린다(#stale-write)
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, "씬을 찾을 수 없습니다")
    try:
        body = json.loads((await request.body()).decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(400, "본문을 읽을 수 없습니다")

    text = body.get("narration_text")
    if not isinstance(text, str):
        return {"ok": False, "reason": "narration_text 없음"}
    cur = (scene.narration_text or "").strip()
    if not text.strip():
        return {"ok": False, "reason": "빈 값 무시"}
    if text.strip() == cur:
        return {"ok": True, "changed": False}

    kwargs = {
        "narration_text": text,
        "narration_rev": int(getattr(scene, "narration_rev", 0) or 0) + 1,
    }
    print(f"[narration-flush] scene={scene_id} 마지막 대본 저장 "
          f"| {cur[:24]!r} → {text.strip()[:24]!r}", flush=True)
    updated = store.update_scene(project_id, scene_id, **kwargs)
    return {"ok": True, "changed": True,
            "narration_rev": getattr(updated, "narration_rev", None)}


@router.patch("/{project_id}/scenes/{scene_id}")
def patch_scene(project_id: str, scene_id: int, body: dict):
    """씬의 허용된 필드를 부분 업데이트한다.

    허용 필드: text_overlays, footage_speed, narration_text, footage, shape_overlays, graphic_overlays, filters, motions, mosaic_overlays, image_overlays, tags, duration_hint_sec
    브라우저 전용 프로젝트에서 서버 preview 전에 씬 데이터를 동기화할 때 사용한다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, "씬을 찾을 수 없습니다")
    from ..models import ShapeOverlay as ShapeOverlayModel
    from ..models import GraphicOverlay as GraphicOverlayModel
    from ..models import SceneFilter as SceneFilterModel
    from ..models import SceneMotion as SceneMotionModel
    from ..models import MosaicOverlay as MosaicOverlayModel
    from ..models import ImageOverlay as ImageOverlayModel
    from ..models import SpeedLinesOverlay as SpeedLinesOverlayModel
    from ..models import MatchMeta as MatchMetaModel
    allowed = {"text_overlays", "footage_speed", "footage_speed_mode", "footage_speed_ramp_hold", "footage_speed_ramp_accel", "footage_speed_ramp_base", "footage_audio_volume", "tts_speed", "narration_text", "subtitle_text", "footage", "shape_overlays", "graphic_overlays", "filters", "motions", "mosaic_overlays", "image_overlays", "speed_lines", "tags", "tts", "match", "sfx_asset_id", "sfx_volume", "sfx_offset_sec", "beep_on_narration_end", "beep_volume", "is_insert_image_scene", "duration_hint_sec", "comments", "tts_emotion"}
    kwargs: dict = {}
    for k, v in body.items():
        if k == "text_overlays" and isinstance(v, list):
            kwargs[k] = [TextOverlayModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "shape_overlays" and isinstance(v, list):
            kwargs[k] = [ShapeOverlayModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "graphic_overlays" and isinstance(v, list):
            kwargs[k] = [GraphicOverlayModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "filters" and isinstance(v, list):
            # 씬 필터 (#206) — flashback/horror/vivid/nightvision/pov
            kwargs[k] = [SceneFilterModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "motions" and isinstance(v, list):
            # 씬 모션 (#207) — shake/clip_zoom/slowmo
            kwargs[k] = [SceneMotionModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "mosaic_overlays" and isinstance(v, list):
            # 모자이크 오버레이 (#264) — pixelate/blur, rect/ellipse
            kwargs[k] = [MosaicOverlayModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "image_overlays" and isinstance(v, list):
            # 이미지 오버레이 — footage 스튜디오 이미지 자산을 씬 위에 합성
            kwargs[k] = [ImageOverlayModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "speed_lines" and isinstance(v, list):
            # 집중선(속도선) 오버레이 — 만화 박진감
            kwargs[k] = [SpeedLinesOverlayModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "comments" and isinstance(v, list):
            # 편집 지시 코멘트 — 씬 단위 메모 + 처리 이력
            from ..models import SceneComment as SceneCommentModel
            kwargs[k] = [SceneCommentModel(**item) if isinstance(item, dict) else item for item in v]
        elif k == "tts_emotion":
            # #tts-emotion — 말투 지시문. register 와 동일하게 문자열만 허용 + trim.
            kwargs[k] = v.strip() if isinstance(v, str) else ""
        elif k == "footage" and isinstance(v, dict):
            kwargs[k] = _footage_to_effect(v, footage_dir=p.footage_dir or None)
        elif k == "tags" and isinstance(v, list):
            # 태그는 문자열 배열만 허용 (#212)
            kwargs[k] = [str(t) for t in v if isinstance(t, str)]
        elif k == "tts":
            # TTS 메타(status/duration_sec/synced/audio_url). dict면 TtsMeta로 검증, None도 허용.
            if v is None:
                kwargs[k] = None
            elif isinstance(v, dict):
                from ..models import TtsMeta
                try:
                    kwargs[k] = TtsMeta(**v)
                except Exception as e:
                    print(f"[patch] tts 검증 실패(스킵): {e}", flush=True)
        elif k == "match":
            # 매칭 메타 — footage 교체/분할 시 클라이언트가 null로 클리어하거나
            # 수동 매칭 패널에서 새 MatchMeta를 dict로 전송한다.
            # 보안: source_file은 footage_dir 내부 basename으로 제한 — 절대경로/`..` 금지.
            if v is None:
                kwargs[k] = None
            elif isinstance(v, dict):
                src = v.get("source_file")
                if isinstance(src, str):
                    # path traversal 방지 — 절대경로, 상위 디렉터리 참조, 디렉터리 구분자 모두 거부.
                    if src.startswith("/") or src.startswith("\\") or ".." in src or "/" in src or "\\" in src:
                        raise HTTPException(400, f"match.source_file must be a basename: got {src!r}")
                try:
                    kwargs[k] = MatchMetaModel(**v)
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(400, f"match 검증 실패: {e}")
            else:
                raise HTTPException(400, "match must be null or a MatchMeta object")
        elif k == "tts_speed":
            # 씬별 TTS 재생 속도 (0.5~2.0 권장, atempo 자동 체이닝으로 그 외도 처리).
            # 잘못된 값은 무시하고 기존 값 유지 — 회귀 안전.
            try:
                _ts = float(v)
                if _ts > 0:
                    kwargs[k] = _ts
            except (TypeError, ValueError):
                pass
        elif k == "footage_speed_mode":
            # 재생 속도 모드 — constant/ramp/ramp_down 외 값은 constant 로 정규화.
            kwargs[k] = v if v in ("ramp", "ramp_down") else "constant"
        elif k in ("footage_speed_ramp_hold", "footage_speed_ramp_accel"):
            # ramp 비율(0~1). 숫자가 아니면 기존 값 유지.
            try:
                kwargs[k] = min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                pass
        elif k == "footage_speed_ramp_base":
            # ramp 기본 배속(양수). 잘못된 값은 기존 값 유지.
            try:
                _rb = float(v)
                if _rb > 0:
                    kwargs[k] = _rb
            except (TypeError, ValueError):
                pass
        elif k == "duration_hint_sec":
            # 씬 화면 표시 길이 힌트 (켄번스/삽입 이미지 씬 길이 편집용).
            # 양수 float 만 허용, 잘못된 값은 무시하고 기존 값 유지 — 회귀 안전.
            try:
                _dh = float(v)
                if _dh > 0:
                    kwargs[k] = _dh
            except (TypeError, ValueError):
                pass
        elif k in allowed and hasattr(scene, k):
            kwargs[k] = v
    # #stale-write — 명시적 단일 씬 편집(PATCH)은 항상 최신으로 간주하고 리비전을 올린다.
    # 이 값이 올라가야, 이후 브라우저가 옛 스냅샷으로 보내는 전체 register 를 거부할 수 있다.
    if "narration_text" in kwargs:
        _new_nt = (kwargs.get("narration_text") or "").strip()
        _cur_nt = (scene.narration_text or "").strip()
        if _new_nt != _cur_nt:
            kwargs["narration_rev"] = int(getattr(scene, "narration_rev", 0) or 0) + 1

    # #406 — narration_text 만 patch 한 경우에도 seg_0 와 동기화.
    # patch_scene 은 register 와 달리 narration_segments 를 받지 않으므로,
    # narration_text 가 바뀌면 seg_0 이 옛 텍스트로 남아 미리보기에 옛 음성이 들리는 회귀.
    # register 의 _purge_stale_narration_wavs 와 동일한 정책을 여기서도 적용한다.
    if "narration_text" in kwargs and scene.narration_segments:
        new_nt = kwargs.get("narration_text") or ""
        new_text = new_nt.strip()
        seg0 = next((s for s in scene.narration_segments if s.id == "seg_0"), None)
        if seg0 is not None and (seg0.text or "").strip() != new_text:
            # 새 narration_text 로 seg_0 동기화 + wav 삭제 + synced=False.
            seg0.text = new_nt
            seg0.synced = False
            seg0.status = "idle"
            narr_dir = store.get_output_dir(project_id) / "narration"
            for wav_name in (
                f"scene_{scene_id:03d}_seg_seg_0.wav",
                f"narration_scene_{scene_id:03d}.wav",
            ):
                wav = narr_dir / wav_name
                if wav.exists():
                    try:
                        wav.unlink()
                        print(
                            f"[patch#406] scene={scene_id} narration_text 변경 → "
                            f"seg_0 동기화 + wav 삭제: {wav_name}",
                            flush=True,
                        )
                    except Exception:
                        pass
            if scene.tts is not None:
                scene.tts.synced = False
            # scene.narration_segments 는 객체 참조라 in-place 변경됨 → kwargs 에 추가 불필요.
            # store.update_scene 호출이 narration_text 만 갱신해도 segments 변경은 이미 반영됨.
            kwargs["narration_segments"] = scene.narration_segments

    if kwargs:
        store.update_scene(project_id, scene_id, **kwargs)
    updated = next((s for s in store.get_project(project_id).scenes if s.scene_id == scene_id), scene)
    return updated


@router.delete("/{project_id}")
def delete_project(project_id: str):
    if not store.delete_project(project_id):
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    return {"deleted": True}


# ── Scenario Parse ─────────────────────────────────────────────────────────

@router.post("/{project_id}/parse-scenario")
def parse_scenario(project_id: str, req: ParseScenarioRequest):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    import tempfile
    from src.scenario_parser import run as parse_run

    # scenario_parser.run()은 파일 경로를 받으므로 임시 파일 사용
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(req.scenario_text)
        tmp_path = f.name

    import os
    # run()이 cache/ 경로를 사용하지 않도록 cwd를 프로젝트 데이터 디렉토리로 변경
    old_cwd = os.getcwd()
    data_dir = str(store.get_cache_dir(project_id).parent)
    os.chdir(data_dir)
    try:
        scenes_raw = parse_run(tmp_path, force=True)
    finally:
        os.chdir(old_cwd)
        Path(tmp_path).unlink(missing_ok=True)

    scenes: list[SceneCard] = []
    for s in scenes_raw:
        footage = s.get("footage")
        effect = _footage_to_effect(footage, footage_dir=p.footage_dir or None)
        scenes.append(SceneCard(
            scene_id=s["scene_id"],
            title=s.get("title", ""),
            description=s.get("description", ""),
            mood=s.get("mood", ""),
            keywords=s.get("keywords", []),
            duration_hint_sec=s.get("duration_hint_sec", 5.0),
            narration_text=s.get("narration_text", ""),
            footage=effect,
        ))

    store.update_project(project_id, scenes=scenes, scenario_text=req.scenario_text)

    # #301 — 모의 비용 기록: Phase 1 시나리오 파싱
    try:
        from ..cost_tracker import measure_phase
        import json as _json
        output_chars = sum(len(_json.dumps(s, ensure_ascii=False)) for s in scenes_raw)
        with measure_phase(project_id) as _est:
            _est.record_scenario_parse(
                scenario_text=req.scenario_text,
                output_chars=output_chars,
                cpu_seconds=0,
            )
    except Exception as _cost_err:
        print(f"[cost] scenario_parse 기록 실패: {_cost_err}")

    return {"scenes": [s.model_dump() for s in scenes]}


def _footage_to_effect(footage: Any, footage_dir: str | None = None) -> Any:
    """씬 footage dict → 적절한 Effect 모델 변환.

    footage_dir가 주어지면 clip 타입의 clip_start_sec/clip_end_sec를 파일 실제 길이로 clamp한다.
    누적 분할 등으로 비정상 값이 들어와도 자동 보정 — 디스크에 잘못된 값이 박히지 않도록 방어.
    """
    if footage is None:
        return None
    if isinstance(footage, dict):
        t = footage.get("type", "clip")
        if t == "edit":
            from ..models import EditEffect
            return EditEffect(
                description=footage.get("description", ""),
                file=footage.get("file", ""),
                min_duration=footage.get("min_duration", 5.0),
            )
        if t == "sync":
            from ..models import SyncEffect
            inner_raw = footage.get("inner")
            inner = _footage_to_effect(inner_raw) if inner_raw else None
            return SyncEffect(word=footage.get("word", ""), inner=inner)
        if t == "kenburns":
            from ..models import KenburnsEffect
            fit_raw = footage.get("fit", "cover") if isinstance(footage, dict) else "cover"
            fit = fit_raw if fit_raw in ("cover", "contain", "stretch") else "cover"
            return KenburnsEffect(image=footage.get("image", ""), fit=fit)  # type: ignore[arg-type]
        if t == "multiscreen":
            from ..models import MultiscreenEffect, GridCell, GridBorder
            cells_raw = footage.get("cells")
            cells = None
            if isinstance(cells_raw, list):
                cells = []
                for c in cells_raw:
                    if not isinstance(c, dict):
                        continue
                    b_raw = c.get("border")
                    border = GridBorder(**b_raw) if isinstance(b_raw, dict) else None
                    cells.append(GridCell(file=c.get("file", ""), border=border))
            return MultiscreenEffect(
                files=footage.get("files", []),
                rows=footage.get("rows"),
                cols=footage.get("cols"),
                cells=cells,
            )
        if t == "title_gen":
            from ..models import TitleGenEffect, TitleOptions
            opts = footage.get("options", {})
            return TitleGenEffect(options=TitleOptions(**opts))
    # 기본: clip
    from ..models import ClipEffect
    file = footage.get("file", "") if isinstance(footage, dict) else ""
    x_offset = float(footage.get("x_offset", 0)) if isinstance(footage, dict) else 0.0
    y_offset = float(footage.get("y_offset", 0)) if isinstance(footage, dict) else 0.0
    # #258 fit 옵션 (contain/cover/stretch). 미지정 또는 잘못된 값이면 contain.
    fit_raw = footage.get("fit", "contain") if isinstance(footage, dict) else "contain"
    fit = fit_raw if fit_raw in ("contain", "cover", "stretch") else "contain"
    # #263 rotation (0/90/180/270). 미지정 또는 잘못된 값이면 0.
    try:
        rot_raw = int(footage.get("rotation", 0)) if isinstance(footage, dict) else 0
    except (TypeError, ValueError):
        rot_raw = 0
    rotation = rot_raw if rot_raw in (0, 90, 180, 270) else 0
    # 씬 나누기 — clip_start_sec / clip_end_sec (둘 다 옵셔널).
    def _opt_float(key: str):
        if not isinstance(footage, dict):
            return None
        v = footage.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    cs = _opt_float("clip_start_sec")
    ce = _opt_float("clip_end_sec")
    # 누적 분할 등으로 비정상 값이 들어오면 footage 실제 길이로 clamp.
    if footage_dir and file:
        try:
            src = Path(footage_dir) / file
            if src.exists():
                dur = _ffprobe_duration(str(src))
                if dur and dur > 0:
                    if ce is not None and ce > dur:
                        ce = round(dur, 3)
                    if cs is not None and cs > dur:
                        cs = round(dur, 3)
                    if cs is not None and ce is not None and cs >= ce:
                        # 시작 >= 끝이면 시작값을 0으로 폴백(완전 잘못된 상태)
                        cs = 0.0
        except Exception:
            pass
    return ClipEffect(  # type: ignore[arg-type]
        file=file, x_offset=x_offset, y_offset=y_offset, fit=fit, rotation=rotation,
        clip_start_sec=cs, clip_end_sec=ce,
    )


# ── Footage Indexing ───────────────────────────────────────────────────────

@router.get("/{project_id}/footage-index-status")
def footage_index_status(project_id: str):
    """footage_index.json 존재 여부와 청크 수를 반환.

    Setup Step 3의 `gemini_content`(Gemini · 내용 분석) 카드 활성화 판정용.
    인덱싱 없이는 청크 description 활용이 0이라 사실상 레이블 매칭(gemini_filename)과
    동일 결과가 나오므로, 인덱싱된 청크가 1개 이상인 경우에만 카드를 활성화한다.

    응답:
        {"indexed": bool, "chunk_count": int}
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    cache_dir = store.get_cache_dir(project_id)
    index_path = cache_dir / "footage_index.json"
    if not index_path.exists():
        return {"indexed": False, "chunk_count": 0}

    try:
        import json as _json
        data = _json.loads(index_path.read_text(encoding="utf-8"))
        chunk_count = len(data.get("chunks", []))
    except Exception:
        # 손상된 파일은 미인덱싱과 동일하게 취급.
        return {"indexed": False, "chunk_count": 0}

    return {"indexed": chunk_count > 0, "chunk_count": chunk_count}


# ── Scene Cut Durations (#378) ─────────────────────────────────────────────
# `cache/timeline.json` 은 Phase 5 (clip_editor.py) 산출물로 각 씬의 실제 cut 길이를
# (start_sec, end_sec) 형태로 담는다. TimelineBar `getSceneDurationSec` 가 ①순위
# 폴백으로 사용할 수 있게 (scene_id, cut_sec) 를 가벼운 JSON 으로 노출한다.
#
# 캐시 친화 설계:
#  - 응답 ETag = `cache/timeline.json` 의 mtime + size 해시 (file 변경 시에만 무효).
#  - 클라이언트가 If-None-Match 헤더를 보내면 304 로 응답.
#  - 파일 부재 시 200 + 빈 배열 — 클라이언트가 폴백 우선순위로 자연스럽게 떨어지게.
#
# WebSocket 알림은 추가하지 않는다(#378 가이드 — 변경 최소화):
#  - 기존 렌더 task 의 `complete` 이벤트는 Editor 가 이미 구독 중.
#  - Editor 진입 시 1회 fetch + 렌더 complete 시 1회 재fetch 만으로 충분히 정합.
#  - HTTP 폴링/lazy fetch 도 가능하지만 위 두 트리거만으로 평상시 0 추가 요청.

@router.get("/{project_id}/scene-cut-durations")
def scene_cut_durations(
    project_id: str,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    """`cache/timeline.json` 의 각 씬 cut 길이를 반환.

    응답 예:
        {"scenes": [{"scene_id": 1, "cut_sec": 4.7}, ...]}

    파일 부재 시: `{"scenes": []}` (200) — 클라이언트가 폴백 우선순위 사용.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    timeline_path = store.get_cache_dir(project_id) / "timeline.json"
    if not timeline_path.exists():
        # ETag 비워두기 — 파일 생성 시 다른 etag 가 부여되어 무효화됨.
        return {"scenes": []}

    try:
        st = timeline_path.stat()
        # mtime 을 ns 단위로 받아 ETag 후보로 사용. size 도 결합해 손상 검출.
        etag = f'W/"tl-{int(st.st_mtime_ns)}-{st.st_size}"'
        # 클라이언트가 보낸 If-None-Match 와 일치 → 304 (본문 없음).
        if if_none_match and if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})

        import json as _json
        data = _json.loads(timeline_path.read_text(encoding="utf-8"))
        timestamps = data.get("timestamps", []) if isinstance(data, dict) else []
        scenes: list[dict] = []
        for ts in timestamps:
            if not isinstance(ts, dict):
                continue
            sid = ts.get("scene_id")
            try:
                start = float(ts.get("start_sec", 0.0))
                end = float(ts.get("end_sec", 0.0))
            except (TypeError, ValueError):
                continue
            cut = end - start
            if not isinstance(sid, int) or cut <= 0:
                # scene_id 가 int 가 아니거나 cut 이 0 이하면 클라이언트 폴백에 맡긴다.
                continue
            scenes.append({"scene_id": sid, "cut_sec": round(cut, 3)})

        response.headers["ETag"] = etag
        # Last-Modified 는 RFC 7232 호환 형식. mtime 초 단위.
        from email.utils import formatdate as _formatdate
        response.headers["Last-Modified"] = _formatdate(st.st_mtime, usegmt=True)
        # 짧은 max-age — Phase 5 직후 자동 재fetch 시 stale 캐시를 피한다.
        response.headers["Cache-Control"] = "private, max-age=2, must-revalidate"
        return {"scenes": scenes}
    except Exception:
        # 손상된 파일은 빈 결과로 폴백 — 클라이언트가 ②~⑥ 우선순위 사용.
        return {"scenes": []}


@router.post("/{project_id}/index-footage")
async def index_footage(project_id: str, req: IndexFootageRequest, background_tasks: BackgroundTasks):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    if not p.footage_dir:
        raise HTTPException(400, "footage_dir이 설정되지 않았습니다")

    task_id = create_task_id()

    async def _run():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_index_footage, project_id, p.footage_dir, task_id, req.force)

    task = asyncio.create_task(_run())
    register_bg_task(task_id, task)
    return {"task_id": task_id}


# ── 인덱싱 사전 비용 추정 (2026-06) ────────────────────────────────────────
# footage_dir 의 영상 파일을 스캔하여 총 duration → Gemini 입력 토큰(295/sec)
# → USD/KRW 환산. 인덱싱 카드에 "예상 약 N원" 라벨을 띄우기 위함.
#
# 단가/모델 분기는 match-cost-estimate(matching.py)의 패턴을 그대로 따른다.
#   - model 미지정 → 프로젝트의 gemini_model → pricing.json default_model 순.
#   - KRW 환산은 매칭 estimate 와 동일한 1380 KRW/USD 사용(상수 복제).
# 사용자 결정(2026-06): 파일 1개 = 청크 1개 정책이므로 file_count 만큼 호출이
# 발생한다. cached_file_count 는 참고 정보로 응답에 포함.

_INDEX_KRW_RATE = 1380
_INDEX_OUTPUT_TOKENS_PER_FILE = 130  # 영상 1개당 응답 JSON ≈ 200자 ÷ 1.5 ≈ 130 tokens
_INDEX_PROMPT_TOKENS_PER_FILE = 350  # ANALYSIS_PROMPT ≈ 530자 ÷ 1.5 ≈ 350 tokens
_INDEX_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


@router.get("/{project_id}/index-cost-estimate")
def index_cost_estimate(project_id: str, model: str | None = None):
    """footage_dir 의 영상 파일들에 대한 인덱싱 예상 비용을 반환.

    응답 예:
        {
          "file_count": 28,
          "cached_file_count": 0,
          "total_duration_sec": 842.5,
          "estimated_input_tokens": 248_887,    # 영상 + 프롬프트
          "estimated_output_tokens": 3640,      # 파일수 × 130
          "usd": 0.0623,
          "krw": 86,
          "model": "gemini-2.5-flash"
        }

    Gemini 매칭이 아닌 모델/방식 분기는 없음 — 항상 비디오 토큰 기반으로 계산.
    `model` 미지정 시 프로젝트의 `gemini_model`, 그래도 없으면 default_model.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    if not p.footage_dir:
        raise HTTPException(400, "footage_dir이 설정되지 않았습니다")

    footage_path = Path(p.footage_dir)
    if not footage_path.exists():
        # 폴더가 없으면 0원으로 응답 — UI 가 깨지지 않도록.
        return {
            "file_count": 0,
            "cached_file_count": 0,
            "total_duration_sec": 0.0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "usd": 0.0,
            "krw": 0,
            "model": (model or getattr(p, "gemini_model", None) or "") or "",
        }

    # 캐시된 파일 집합 — 이미 인덱싱된 파일 수만 카운트(보수적으로 전체를 비용에 포함).
    cache_dir = store.get_cache_dir(project_id)
    index_path = cache_dir / "footage_index.json"
    cached_files: set[str] = set()
    if index_path.exists():
        try:
            import json as _json
            data = _json.loads(index_path.read_text(encoding="utf-8"))
            for ch in data.get("chunks", []):
                src = ch.get("source_file", "")
                if src:
                    cached_files.add(Path(src).name)
        except Exception:
            pass

    # 영상 파일 스캔.
    files = [f for f in footage_path.iterdir() if f.is_file() and f.suffix.lower() in _INDEX_VIDEO_EXTS]
    total_duration_sec = 0.0
    cached_count = 0
    for f in files:
        if f.name in cached_files:
            cached_count += 1
        # duration 측정 — 실패 시 0으로 폴백(=비용 미반영).
        try:
            from src.utils.ffmpeg_utils import get_duration
            total_duration_sec += float(get_duration(str(f)) or 0.0)
        except Exception:
            pass

    chosen_model = (model or "").strip() or (getattr(p, "gemini_model", None) or None)
    from src.cost_estimator.estimator import get_gemini_model_pricing
    g = get_gemini_model_pricing(chosen_model)

    video_tokens = total_duration_sec * g["video_tokens_per_sec"]
    prompt_tokens = len(files) * _INDEX_PROMPT_TOKENS_PER_FILE
    output_tokens = len(files) * _INDEX_OUTPUT_TOKENS_PER_FILE

    total_input = video_tokens + prompt_tokens
    usd = (
        total_input / 1_000_000 * g["input_per_1m_tokens_usd"]
        + output_tokens / 1_000_000 * g["output_per_1m_tokens_usd"]
    )
    krw = round(usd * _INDEX_KRW_RATE)

    return {
        "file_count": len(files),
        "cached_file_count": cached_count,
        "total_duration_sec": round(total_duration_sec, 1),
        "estimated_input_tokens": int(round(total_input)),
        "estimated_output_tokens": int(round(output_tokens)),
        "usd": round(usd, 6),
        "krw": int(krw),
        "model": chosen_model or "",
    }


def _do_index_footage(project_id: str, footage_dir: str, task_id: str, force: bool):
    # src.footage_indexer 는 클래스가 아니라 모듈 함수(run) 를 노출한다.
    # 인덱싱 출력 경로는 모듈 상수 INDEX_PATH("cache/footage_index.json") 를 따르므로
    # 임시로 CWD 를 프로젝트의 데이터 디렉토리로 옮겨 호출한다(레거시 호환).
    import os
    from src import footage_indexer as fi

    push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "indexing",
                        "current": 0, "total": 1, "message": "footage 분석 시작...", "item": ""})

    project_dir = store.get_project_dir(project_id) if hasattr(store, "get_project_dir") else None
    if project_dir is None:
        # 폴백 — DATA_DIR / project_id
        project_dir = store.DATA_DIR / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = store.get_cache_dir(project_id)
    index_path = cache_dir / "footage_index.json"

    # 프로젝트가 선택한 Gemini 모델을 인덱싱 호출에 전파.
    # 미설정 시 None → footage_indexer 내부에서 _resolve_model() 폴백 체인 사용.
    p_for_model = store.get_project(project_id)
    index_model = getattr(p_for_model, "gemini_model", None) if p_for_model else None
    print(f"[index-footage] project={project_id} model={index_model or '(default)'}")

    prev_cwd = os.getcwd()
    try:
        os.chdir(str(project_dir))
        # 모듈 함수 호출. 결과 list 는 모든 청크(기존+신규)를 포함한다.
        result = fi.run(footage_dir, force=force, model=index_model)
        push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "indexing",
                            "current": 1, "total": 1, "message": f"{len(result)}개 파일 인덱싱 완료", "item": ""})

        # #301 — 모의 비용 기록: Phase 2 footage 인덱싱.
        # 2026-06 — 프로젝트의 gemini_model 을 estimator 에 전달하여 by_model 분해에 반영.
        try:
            from ..cost_tracker import measure_phase
            import json as _json
            p = store.get_project(project_id)
            model = getattr(p, "gemini_model", None) if p else None
            with measure_phase(project_id) as _est:
                if index_path.exists():
                    idx_data = _json.loads(index_path.read_text(encoding="utf-8"))
                    chunks = idx_data.get("chunks", [])
                    for ch in chunks:
                        dur = max(0.0, float(ch.get("end_sec", 0)) - float(ch.get("start_sec", 0)))
                        out_chars = len(_json.dumps(ch, ensure_ascii=False))
                        _est.record_footage_index_chunk(
                            video_duration_sec=dur,
                            output_chars=out_chars,
                            prompt_chars=500,
                            cpu_seconds=10.0,
                            model=model,
                        )
        except Exception as _cost_err:
            print(f"[cost] footage_index 기록 실패: {_cost_err}")

        push_sync(task_id, {"type": "complete", "task_id": task_id,
                            "summary": {"total": len(result), "success": len(result), "failed": 0}})
    except Exception as e:
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "INDEX_FAILED", "message": str(e)})
    finally:
        try:
            os.chdir(prev_cwd)
        except Exception:
            pass


# ── Footage Browser ────────────────────────────────────────────────────────

@router.get("/{project_id}/footage", response_model=list[FootageItem])
def get_footage(project_id: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    footage_dir = Path(p.footage_dir)
    if not footage_dir.exists():
        raise HTTPException(400, "footage 폴더가 존재하지 않습니다")

    cache_dir = store.get_cache_dir(project_id)
    index_path = cache_dir / "footage_index.json"

    chunks_by_file: dict[str, list[FootageChunk]] = {}
    if index_path.exists():
        import json
        data = json.loads(index_path.read_text(encoding="utf-8"))
        for chunk in data.get("chunks", []):
            fn = Path(chunk.get("source_file", "")).name
            kw_raw = chunk.get("keywords", []) or []
            # 방어적으로 문자열만 통과시키고 공백/중복 제거.
            keywords_clean: list[str] = []
            seen_kw: set[str] = set()
            for k in kw_raw:
                if not isinstance(k, str):
                    continue
                ks = k.strip()
                if not ks or ks in seen_kw:
                    continue
                seen_kw.add(ks)
                keywords_clean.append(ks)
            chunks_by_file.setdefault(fn, []).append(FootageChunk(
                chunk_id=chunk.get("chunk_id", ""),
                start_sec=chunk.get("start_sec", 0.0),
                end_sec=chunk.get("end_sec", 0.0),
                description=chunk.get("description", ""),
                keywords=keywords_clean,
                quality_score=chunk.get("quality_score", 0.0),
            ))

    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    # ── #289/#290 — 오디오 파일도 footage 폴더에서 자동 인식한다 ────────────
    # BGM/SFX 자산으로 활용. file_type="audio"로 응답.
    audio_exts = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}

    items: list[FootageItem] = []
    for f in sorted(footage_dir.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in video_exts and ext not in image_exts and ext not in audio_exts:
            continue
        if ext in video_exts:
            file_type = "video"
        elif ext in audio_exts:
            file_type = "audio"
        else:
            file_type = "image"
        # 영상·오디오는 길이 측정, 이미지는 0.
        dur = _get_duration(str(f)) if file_type in ("video", "audio") else 0.0
        items.append(FootageItem(
            filename=f.name,
            path=str(f),
            thumbnail_url=f"/api/projects/{project_id}/footage/{f.name}/thumbnail",
            duration_sec=dur,
            file_type=file_type,
            chunks=chunks_by_file.get(f.name, []),
        ))
    return items


# ── Footage 썸네일 캐시 (#256) ─────────────────────────────────────────────
#
# 원본 영상/이미지를 매번 디코드하지 않도록 디스크 캐시 사용.
# - 캐시 폴더: output/footage_thumbnails/ (.gitignore 처리됨)
# - 캐시 키: sha256(absolute_path + mtime)[:16] + ".png"
# - mtime이 바뀌면 키도 바뀌므로 자동 무효화
# - PNG 포맷 (작은 사이즈 320px → 5~30KB 수준)

_THUMBNAIL_CACHE_DIR = _ROOT / "output" / "footage_thumbnails"


def _footage_thumbnail_cache_path(footage_path: Path, width: int = 320) -> Path:
    """sha256(절대경로 + mtime + width)로 캐시 키 산출."""
    import hashlib
    abs_path = str(footage_path.resolve())
    mtime = footage_path.stat().st_mtime
    key_src = f"{abs_path}|{mtime}|{width}".encode("utf-8")
    digest = hashlib.sha256(key_src).hexdigest()[:16]
    return _THUMBNAIL_CACHE_DIR / f"{digest}.png"


def _generate_footage_thumbnail(footage_path: Path, output_path: Path, width: int = 320) -> bool:
    """ffmpeg로 footage에서 PNG 썸네일을 생성한다. 성공 시 True."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ext = footage_path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
    # #289/#290 — 오디오 파일은 showwavespic 파형 썸네일
    audio_exts = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}
    if ext in image_exts:
        # 이미지: ffmpeg로 리사이즈만 (PNG 출력)
        result = subprocess.run(
            [ffmpeg_bin(), "-y", "-i", str(footage_path),
             "-frames:v", "1", "-vf", f"scale={width}:-1",
             str(output_path)],
            capture_output=True,
        )
    elif ext in audio_exts:
        # 오디오: 파형 PNG (높이=width의 9/16, 16:9 호환). 색상은 BgmEditor accent와 동일.
        h = max(80, int(width * 9 / 16))
        result = subprocess.run(
            [ffmpeg_bin(), "-y", "-i", str(footage_path),
             "-filter_complex", f"showwavespic=s={width}x{h}:colors=#a78bfa",
             "-frames:v", "1", str(output_path)],
            capture_output=True,
        )
        # 파형 실패 시 회색 폴백
        if result.returncode != 0 or not output_path.exists():
            subprocess.run(
                [ffmpeg_bin(), "-y", "-f", "lavfi",
                 "-i", f"color=c=#1a1a1a:s={width}x{int(width * 9 / 16)}:d=1",
                 "-frames:v", "1", str(output_path)],
                capture_output=True,
            )
    else:
        # 비디오: 0.5초 시점에서 프레임 1장 추출 (검은 프레임 회피).
        # -ss를 -i 뒤에 두는 이유: input 앞 -ss는 keyframe seek라 vp9 등에서
        # 해당 위치에 키프레임이 없으면 빈 출력이 나온다. input 뒤 -ss는 디코드
        # 후 정확 seek라 어떤 코덱이든 안전하다.
        result = subprocess.run(
            [ffmpeg_bin(), "-y", "-i", str(footage_path),
             "-ss", "0.5", "-frames:v", "1", "-vf", f"scale={width}:-1",
             str(output_path)],
            capture_output=True,
        )
        # 0.5초 시점에서 실패한 경우 (영상이 매우 짧음) 0초로 재시도
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            # 빈 파일이 남아 있으면 제거 (다음 시도에서 -y로 덮어쓰지만 stat 검사 일관성 위해)
            if output_path.exists() and output_path.stat().st_size == 0:
                output_path.unlink(missing_ok=True)
            result = subprocess.run(
                [ffmpeg_bin(), "-y", "-i", str(footage_path),
                 "-frames:v", "1", "-vf", f"scale={width}:-1",
                 str(output_path)],
                capture_output=True,
            )
    return output_path.exists() and output_path.stat().st_size > 0


def _serve_footage_thumbnail(footage_path: Path, width: int = 320,
                              project_id: str | None = None) -> FileResponse:
    """캐시 hit 시 즉시 반환, miss 시 생성 후 반환.

    #301 — project_id가 주어지면 ffmpeg CPU 시간 + egress를 기록.
    """
    import time as _t_thumb
    cache_path = _footage_thumbnail_cache_path(footage_path, width=width)
    cache_hit = cache_path.exists() and cache_path.stat().st_size > 0
    cpu_seconds = 0.0
    if not cache_hit:
        _t0 = _t_thumb.monotonic()
        ok = _generate_footage_thumbnail(footage_path, cache_path, width=width)
        cpu_seconds = _t_thumb.monotonic() - _t0
        if not ok:
            raise HTTPException(404, "썸네일을 생성할 수 없습니다 (파일 디코드 실패)")

    # #301 — 비용 기록 (썸네일은 PNG 수십 KB 수준 — 미미하지만 누적 추적)
    if project_id:
        try:
            from ..cost_tracker import measure_phase
            with measure_phase(project_id) as _est:
                _est.record_scene_preview(
                    cpu_seconds=cpu_seconds,
                    output_bytes=cache_path.stat().st_size if cache_path.exists() else 0,
                    cache_hit=cache_hit,
                )
        except Exception as _err:
            print(f"[cost] thumbnail 기록 실패: {_err}")

    return FileResponse(str(cache_path), media_type="image/png")


@router.get("/{project_id}/footage/thumbnail")
def get_footage_thumbnail_by_path(project_id: str, path: str, width: int = 320):
    """경로 기반 footage 썸네일 — mtime 기반 디스크 캐시 사용 (#256).

    - `path`: footage 파일의 상대 경로(파일명) 또는 절대 경로
    - `width`: 썸네일 가로 폭 (기본 320, 80~1920 범위 제한)
    - 보안: 프로젝트의 footage_dir(또는 data_dir) 안에 있는 파일만 허용
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    # width 범위 제한 (DoS 방지)
    width = max(80, min(1920, int(width)))

    # path 정규화 + 보안 검증
    # 절대 경로 또는 상대 경로(footage_dir 기준) 모두 허용
    req = Path(path)
    if not req.is_absolute():
        # 상대 경로 → footage_dir 기준
        if not p.footage_dir:
            raise HTTPException(400, "footage_dir이 설정되지 않았습니다")
        req = Path(p.footage_dir) / req
    try:
        resolved = req.resolve(strict=False)
    except (OSError, RuntimeError):
        raise HTTPException(400, "잘못된 경로입니다")

    # 허용 디렉토리: footage_dir, 프로젝트 폴더 전체
    allowed_roots: list[Path] = []
    if p.footage_dir:
        try:
            allowed_roots.append(Path(p.footage_dir).resolve())
        except (OSError, RuntimeError):
            pass
    try:
        allowed_roots.append(store._project_dir(project_id).resolve())
    except (OSError, RuntimeError, AttributeError):
        pass

    if not allowed_roots:
        raise HTTPException(400, "허용된 경로가 없습니다")

    ok = False
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            ok = True
            break
        except ValueError:
            continue
    if not ok:
        raise HTTPException(403, "허용되지 않은 경로입니다")

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")

    return _serve_footage_thumbnail(resolved, width=width, project_id=project_id)


@router.get("/{project_id}/footage/{filename}/thumbnail")
def get_thumbnail(project_id: str, filename: str):
    """파일명 기반 footage 썸네일 — mtime 기반 캐시 사용 (#256).

    기존 호환 경로. 내부적으로 `_serve_footage_thumbnail` 사용 → mtime 변경 시 자동 무효화.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)

    if not p.footage_dir:
        raise HTTPException(400, "footage_dir이 설정되지 않았습니다")

    src = Path(p.footage_dir) / filename
    if not src.exists() or not src.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")

    return _serve_footage_thumbnail(src, width=320, project_id=project_id)


@router.get("/{project_id}/footage/{filename}/stream")
def stream_footage(project_id: str, filename: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    src = Path(p.footage_dir) / filename
    if not src.exists():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    # #301 — 영상 스트리밍 egress 기록
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_egress(bytes_size=src.stat().st_size)
    except Exception as _err:
        print(f"[cost] footage_stream 기록 실패: {_err}")
    return FileResponse(str(src))


@router.get("/{project_id}/footage/{filename}/exists")
def footage_exists(project_id: str, filename: str):
    """서버 footage_dir에 파일이 있는지 가벼운 확인. 클라가 업로드 스킵 여부 판단용."""
    p = store.get_project(project_id)
    if not p or not p.footage_dir:
        return {"exists": False}
    src = Path(p.footage_dir) / filename
    return {"exists": src.exists() and src.stat().st_size > 0}


@router.get("/{project_id}/footage/{filename}/stream-proxy")
def stream_footage_proxy(project_id: str, filename: str):
    """미리보기 video 태그용 720p 프록시 스트림. 비디오가 아니면 원본으로 폴백."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    proxy = _ensure_proxy(project_id, filename)
    if proxy and proxy.exists():
        return FileResponse(str(proxy))
    src = Path(p.footage_dir) / filename
    if not src.exists():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return FileResponse(str(src))


# ── Scene preview (전환 포함 미리보기) ────────────────────────────────────

def _get_footage_file(scene: "SceneCard", footage_dir: str) -> str | None:
    if not scene.footage:
        return None
    f = scene.footage
    fname = getattr(f, "file", None) or getattr(f, "image", None)
    if not fname:
        return None
    p = Path(footage_dir) / fname
    return str(p) if p.exists() else None


# 미리보기 인코딩 해상도 상한 (#254)
# 출력 해상도가 이보다 크면 가로세로 비율을 유지한 채 다운스케일하여 미리보기 인코딩 속도를 단축한다.
# Phase 6 최종 렌더링(src/clip_editor.py)에는 영향을 주지 않는다.
PREVIEW_MAX_H = 720


# ── #361 — 미리보기 ffmpeg 하드웨어 가속 (opt-in) ───────────────────────────
# 환경 변수 VIDEO_AGENT_HWACCEL 으로 켠다 (기본 꺼짐).
#   - "videotoolbox" 또는 "1" (macOS) : `-hwaccel videotoolbox` + 인코더 h264_videotoolbox
#   - 그 외/미설정 : 기존 libx264 동작
# 인코더 차이로 인한 미세한 화질/품질 차이는 미리보기 한정이라 사용자 시각 비교에 영향이 작고,
# CPU 인코딩이 가장 큰 병목인 macOS 환경에서 2~5x 빠른 미리보기 생성이 가능하다.
# Phase 6 최종 렌더링(src/clip_editor.py)에는 영향을 주지 않는다 — 미리보기 한정.
import os as _os_hwaccel


def _hwaccel_enabled() -> bool:
    val = (_os_hwaccel.environ.get("VIDEO_AGENT_HWACCEL") or "").strip().lower()
    return val in ("1", "true", "yes", "videotoolbox", "on")


def _ffmpeg_input_hwaccel_args() -> list[str]:
    """입력 디코딩 단계에 적용할 ffmpeg 인자. 미사용이면 빈 리스트."""
    if _hwaccel_enabled():
        return ["-hwaccel", "videotoolbox"]
    return []


def _ffmpeg_video_encoder_args(crf: str = "23", preset: str = "fast") -> list[str]:
    """미리보기용 비디오 인코더 인자.

    libx264 의 `-crf`/`-preset` 은 videotoolbox 에 의미가 없어,
    하드웨어 인코더 사용 시 `-b:v 4M`(미리보기 720p 적정 비트레이트) + `-realtime 1` 로 대체한다.
    """
    if _hwaccel_enabled():
        return ["-c:v", "h264_videotoolbox", "-b:v", "4M", "-realtime", "1"]
    return ["-c:v", "libx264", "-crf", crf, "-preset", preset]


def _compute_preview_size(raw_w: int, raw_h: int) -> tuple[int, int]:
    """미리보기 인코딩용 (width, height)를 계산한다.

    raw_h <= PREVIEW_MAX_H 인 경우 원본 해상도를 그대로 사용한다.
    raw_h > PREVIEW_MAX_H 이면 가로세로 비율을 유지한 채 높이를 PREVIEW_MAX_H 로 맞추며,
    libx264 + yuv420p 호환을 위해 너비도 짝수로 정렬한다.
    """
    if raw_h <= 0 or raw_w <= 0:
        # 비정상 입력 — 기본 1280x720 으로 보정
        return 1280, 720
    if raw_h <= PREVIEW_MAX_H:
        # 너비/높이 모두 짝수 보장 (yuv420p)
        return (raw_w // 2) * 2 or 2, (raw_h // 2) * 2 or 2
    scale = PREVIEW_MAX_H / float(raw_h)
    scaled_w = int(raw_w * scale)
    out_w = (scaled_w // 2) * 2 or 2
    out_h = (PREVIEW_MAX_H // 2) * 2 or 2
    return out_w, out_h


def _ffprobe_video_size(path: str) -> tuple[int, int] | None:
    """영상 파일의 (width, height)를 반환한다. 실패 시 None."""
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        parts = r.stdout.strip().split(",")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


_PROXY_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


def _proxy_path(project_id: str, filename: str) -> Path:
    return store.get_cache_dir(project_id) / "proxies" / (Path(filename).stem + "_proxy720.mp4")


def _proxy_meta_path(project_id: str, filename: str) -> Path:
    """프록시 사이드카 메타 경로 — 프록시를 만든 *원본* 의 file_signature 를 담는다.

    프록시 파일명 자체에 해시를 넣지 않고 사이드카를 쓰는 이유:
    - 파일명이 고정이라 `invalidate_footage_caches()` 가 경로만으로 삭제할 수 있다.
    - 원본이 바뀌어도 프록시/사이드카를 제자리 덮어쓰므로 낡은 프록시가 쌓이지 않는다.
    """
    return _proxy_path(project_id, filename).with_suffix(".json")


def _read_proxy_meta(meta: Path) -> dict | None:
    """사이드카를 읽어 저장된 원본 sig 를 돌려준다. 없거나 깨졌으면 None."""
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None
    sig = data.get("src_sig") if isinstance(data, dict) else None
    return sig if isinstance(sig, dict) else None


# 프록시 생성 직렬화 — 같은 (project_id, filename) 은 한 번에 하나만 만든다.
#
# 배경(실측 버그): 이어보기 프리페치가 여러 씬의 미리보기를 동시에 요청하면, 같은 원본을
# 공유하는 씬들이 `_ensure_proxy()` 를 동시에 호출한다. 예전 구현은 같은 프록시 경로에
# 여러 ffmpeg 가 동시에 쓰고 서로의 사이드카를 지워(`meta.unlink`) 덮어썼다. 결과:
#   - "[proxy] 생성 실패" / "원본 변경 감지 → 재생성" 이 원본이 멀쩡한데도 무더기 발생
#   - 프록시의 file_signature 가 호출마다 달라져 `TrimStageKey.input_sig` 가 흔들리고
#   - trim/final 미리보기 캐시가 영구히 MISS → 매번 전량 재인코딩(씬당 수십 초)
_PROXY_BUILD_LOCK = _keyed_locks.KeyedLock()
# 락 대기 상한. 대용량 원본(수백 MB)의 720p 재인코딩이 분 단위로 걸릴 수 있어
# 넉넉히 잡되, 무한 대기로 워커 스레드가 고갈되는 것은 막는다. 초과 시 원본 폴백.
PROXY_LOCK_TIMEOUT_SEC = 180.0


def _proxy_cache_hit(src: Path, dst: Path, meta: Path, src_sig: dict) -> Path | None:
    """기존 프록시를 그대로 쓸 수 있으면 경로를, 아니면 None 을 돌려준다.

    **읽기 전용** 이다 — 락 없이 호출되므로 여기서 파일을 지우면 안 된다.
    (예전 구현은 이 판정 단계에서 곧바로 `unlink` 했고, 그것이 동시 호출 시
     서로의 프록시/사이드카를 날리는 경쟁의 원인이었다.)

    재사용 조건 두 가지를 모두 만족해야 한다.

    1. 원본이 그대로일 것 — 프록시를 만들 때의 원본 `file_signature`(name+mtime+size)를
       사이드카(`{stem}_proxy720.json`)에 남겨두고, 재사용 전에 현재 원본의 sig 와
       비교한다. 써드파티 세션이나 직접 편집으로 원본이 바뀌면 sig 가 달라지므로
       프록시를 재생성한다. 사이드카가 없거나 깨졌으면(과거 버전이 만든 프록시 포함)
       판정 불가로 보고 보수적으로 재생성한다 — 낡은 것을 쓰느니 다시 만든다.
    2. 프록시가 성한 파일일 것 — ffprobe 로 길이를 한 번 더 검증한다. 과거 한번 깨진
       채로 저장된 프록시(ffmpeg 중단/SIGKILL 등으로 헤더만 있는 파일)는 size>0 만으로
       판단하면 재사용되어 미리보기 500을 유발한다.
    """
    try:
        if not dst.exists() or dst.stat().st_size == 0:
            return None
    except OSError:
        return None
    cached_sig = _read_proxy_meta(meta)
    if cached_sig is None:
        # 사이드카 없음/손상 — 원본 대조 불가. 보수적으로 재생성.
        print(f"[proxy] 원본 대조 불가(메타 없음) → 재생성: {dst.name}", flush=True)
        return None
    if cached_sig != src_sig:
        # 원본이 외부에서 바뀜 — 프록시가 옛 영상이므로 재생성.
        print(f"[proxy] 원본 변경 감지 → 재생성: {dst.name}", flush=True)
        return None
    if _ffprobe_duration(str(dst)) is None:
        # 깨진 캐시 — 재생성 대상.
        print(f"[proxy] 깨진 캐시 감지 → 재생성: {dst.name}", flush=True)
        return None
    return dst


def _ensure_proxy(project_id: str, filename: str) -> Path | None:
    """원본 footage의 720p 프록시를 생성/반환. 비디오만 대상. 실패 시 None(원본 폴백).

    원본이 안 바뀌었으면 반드시 캐시가 적중해야 한다. 프록시 인코딩은 비싸고,
    매번 재생성하면 씬 전환 미리보기 성능이 무너진다.

    동시성 계약:
      - **캐시 적중 판정은 락 밖** 에서 한다. 적중 경로(~0.12초)에 직렬화를 걸면
        이어보기 프리페치가 오히려 느려진다.
      - 미스일 때만 `(project_id, filename)` 단위 락을 잡는다. 서로 다른 파일은
        여전히 병렬로 생성된다.
      - 락을 잡은 뒤 **한 번 더** 적중 판정을 한다(double-checked). 대기 중에 앞선
        호출자가 만들어 놨으면 중복 인코딩 없이 그 결과를 재사용한다.
      - 프록시 mp4 와 사이드카 json 은 임시파일 → `os.replace` 로 원자적 교체.
        부분 기록된 파일이 다른 요청에 보이지 않는다.
      - 사이드카는 프록시 교체 *후* 에 쓴다. 중간에 죽으면 "메타 없음 → 재생성"으로
        보수적으로 기운다(기존 동작 유지).
    """
    if Path(filename).suffix.lower() not in _PROXY_VIDEO_EXTS:
        return None
    p = store.get_project(project_id)
    if not p or not p.footage_dir:
        return None
    src = Path(p.footage_dir) / filename
    if not src.exists() or src.stat().st_size == 0:
        return None

    from src.render_spec import preview_stages as _stages
    src_sig = _stages.file_signature(src)

    dst = _proxy_path(project_id, filename)
    meta = _proxy_meta_path(project_id, filename)

    # ① 락 밖 빠른 경로 — 적중이면 즉시 반환(직렬화 없음).
    hit = _proxy_cache_hit(src, dst, meta, src_sig)
    if hit is not None:
        return hit

    # ② 미스 — 같은 프록시 경로에 대해 한 번에 하나만 생성한다.
    try:
        with _PROXY_BUILD_LOCK.acquire((project_id, filename), PROXY_LOCK_TIMEOUT_SEC):
            # ③ double-check — 대기하는 사이 앞선 호출자가 만들었으면 그대로 재사용.
            hit = _proxy_cache_hit(src, dst, meta, src_sig)
            if hit is not None:
                return hit
            return _build_proxy(src, dst, meta, src_sig, filename)
    except _keyed_locks.LockTimeout:
        # 선행 생성이 지나치게 길다 — 매달리지 말고 원본 폴백.
        print(f"[proxy] 선행 생성 대기 시간 초과 → 원본 폴백: {filename}", flush=True)
        return None


def _build_proxy(src: Path, dst: Path, meta: Path, src_sig: dict, filename: str) -> Path | None:
    """실제 프록시 인코딩. **반드시 `_PROXY_BUILD_LOCK` 을 잡은 상태에서 호출한다.**"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 낡은 사이드카를 먼저 치운다. 인코딩 도중 죽어도 "메타 없음 → 재생성" 으로 기운다.
    meta.unlink(missing_ok=True)
    size = _ffprobe_video_size(str(src))
    vf = ["-vf", f"scale=-2:{PREVIEW_MAX_H}"] if (size and size[1] > PREVIEW_MAX_H) else []
    # 임시 파일명에 pid+tid 를 넣어, 혹시라도 락 밖에서 호출되는 경로가 생기더라도
    # 서로의 tmp 를 덮어쓰지 않게 한다(원자적 교체의 전제).
    tmp = dst.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp.mp4")
    cmd = [ffmpeg_bin(), "-y", "-i", str(src), *vf,
           "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            print(f"[proxy] 생성 실패 {filename}", flush=True)
            tmp.unlink(missing_ok=True)
            return None
        # 원자적 교체 — 부분 기록된 파일이 다른 요청에게 보이지 않는다.
        os.replace(tmp, dst)
        # 사이드카도 임시파일 → replace 로 원자적으로. 프록시 교체 *후* 에 쓴다.
        try:
            meta_tmp = meta.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp.json")
            meta_tmp.write_text(
                json.dumps({"src_sig": src_sig}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(meta_tmp, meta)
        except Exception as e:
            print(f"[proxy] 메타 기록 실패 {filename}: {e}", flush=True)
            meta.unlink(missing_ok=True)
        return dst
    except Exception as e:
        print(f"[proxy] 생성 예외 {filename}: {e}", flush=True)
        tmp.unlink(missing_ok=True)
        return None


def _scene_footage_volume(scene) -> float:
    """씬의 원본 오디오 음량(0~1). 값이 없거나 이상하면 기본 0.3.

    주의: 0 은 "음소거" 라는 유효한 값이므로 falsy 로 취급해 기본값으로 되돌리면 안 된다.
    """
    v = getattr(scene, "footage_audio_volume", 0.3)
    try:
        return float(v) if v is not None else 0.3
    except (TypeError, ValueError):
        return 0.3


def _copy_footage_with_volume(src: str, dst: str, scene) -> None:
    """footage 를 dst 로 복사하되 씬의 `footage_audio_volume` 을 적용한다.

    TTS 가 없는(또는 TTS 길이를 못 읽는) 미리보기 경로는 예전에 `shutil.copy2` 로
    원본을 그대로 복사했다. 그 탓에 음량 설정이 한 번도 적용되지 않아, 원본음량을
    0(음소거)으로 내려도 미리보기에서 소리가 그대로 들리는 버그가 있었다.
    음량 적용이 필요 없거나(1.0) 오디오 트랙이 없으면 기존처럼 단순 복사한다.
    """
    import shutil as _shutil
    fav = _scene_footage_volume(scene)
    has_audio = bool(subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", src],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip())
    if not has_audio or fav == 1.0:
        _shutil.copy2(src, dst)
        return
    if fav <= 0:
        # 완전 음소거 — 오디오 트랙 자체를 제거(-an).
        cmd = [ffmpeg_bin(), "-y", "-i", src, "-map", "0:v", "-c:v", "copy", "-an", dst]
    else:
        cmd = [ffmpeg_bin(), "-y", "-i", src,
               "-filter_complex", f"[0:a]volume={fav}[aout]",
               "-map", "0:v", "-map", "[aout]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", dst]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not Path(dst).exists():
        # 실패 시에도 미리보기 자체는 나와야 하므로 원본 복사로 폴백.
        print(f"[preview] 원본음량 적용 실패 — 복사로 폴백: {r.stderr[:200]}", flush=True)
        _shutil.copy2(src, dst)


def _probe_has_audio(path: str) -> bool:
    """오디오 스트림이 하나라도 있으면 True."""
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return bool(r.stdout.strip())


def _ffprobe_duration(path: str) -> float | None:
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        pass
    # duration 메타데이터가 없는 webm(MediaRecorder 출력) 등 — 전체 디코딩으로 측정
    r2 = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lines = [l.strip() for l in r2.stdout.strip().splitlines() if l.strip()]
    if lines:
        try:
            return float(lines[-1].rstrip(","))
        except ValueError:
            pass
    return None


# ── #308 — 클라이언트 메타 신뢰 모드 ────────────────────────────────────────
#
# 프론트엔드가 `<video>`/`<audio>` 엘리먼트로 추출한 해상도/길이 메타를
# `client_meta` form 필드(JSON 문자열)로 첨부한다. 백엔드는 sanity check
# 통과 시에만 신뢰하고, 통과 못하면 기존 `_ffprobe_*` 폴백으로 안전망 확보.
#
# Sanity 범위(이슈 #308 회귀 방지):
#   - width/height: 16 ~ 16384 (보통 영상 8K 8192 까지)
#   - duration: 0.1 ~ 3600 초 (한 푸티지/TTS 단위, 1시간 상한)
#
# 둘 다 None 반환이 fallback 진입을 의미한다.

_CLIENT_META_MIN_DIM = 16
_CLIENT_META_MAX_DIM = 16384
_CLIENT_META_MIN_DUR = 0.1
_CLIENT_META_MAX_DUR = 3600.0


def _parse_client_video_meta(raw: str | None) -> dict | None:
    """client_meta JSON 문자열을 파싱·검증해 dict 반환. 실패 시 None."""
    if not raw:
        return None
    try:
        import json as _json
        data = _json.loads(raw)
    except Exception as _e:
        print(f"[client_meta] JSON 파싱 실패: {_e}", flush=True)
        return None
    if not isinstance(data, dict):
        return None
    w = data.get("width")
    h = data.get("height")
    d = data.get("duration")
    try:
        w = int(w) if w is not None else None
        h = int(h) if h is not None else None
        d = float(d) if d is not None else None
    except (TypeError, ValueError):
        return None
    if w is None or h is None or d is None:
        return None
    if not (_CLIENT_META_MIN_DIM <= w <= _CLIENT_META_MAX_DIM):
        print(f"[client_meta] width 범위 이탈 ({w}) — fallback", flush=True)
        return None
    if not (_CLIENT_META_MIN_DIM <= h <= _CLIENT_META_MAX_DIM):
        print(f"[client_meta] height 범위 이탈 ({h}) — fallback", flush=True)
        return None
    if not (_CLIENT_META_MIN_DUR <= d <= _CLIENT_META_MAX_DUR):
        print(f"[client_meta] duration 범위 이탈 ({d}) — fallback", flush=True)
        return None
    return {"width": w, "height": h, "duration": d}


def _parse_client_audio_meta(raw: str | None) -> dict | None:
    """오디오 client_meta. duration만 필수, sample_rate/channels는 옵셔널(#312).

    반환 형식:
        {"duration": float, "sample_rate"?: int, "channels"?: int}

    sample_rate / channels 는 본 엔드포인트에서 즉시 사용되지 않더라도
    로그/응답에서 진단·검증에 쓰인다. 클라가 보낸 값이 범위 밖이면
    duration 만 신뢰하고 옵셔널 필드는 누락한다(전체 폴백 방지).
    """
    if not raw:
        return None
    try:
        import json as _json
        data = _json.loads(raw)
    except Exception as _e:
        print(f"[client_meta] audio JSON 파싱 실패: {_e}", flush=True)
        return None
    if not isinstance(data, dict):
        return None
    d = data.get("duration")
    try:
        d = float(d) if d is not None else None
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    if not (_CLIENT_META_MIN_DUR <= d <= _CLIENT_META_MAX_DUR):
        print(f"[client_meta] audio duration 범위 이탈 ({d}) — fallback", flush=True)
        return None
    result: dict = {"duration": d}
    # 옵셔널: sample_rate (8kHz~192kHz 일반 범위)
    sr = data.get("sample_rate")
    if sr is not None:
        try:
            sr_i = int(sr)
            if 8000 <= sr_i <= 192000:
                result["sample_rate"] = sr_i
            else:
                print(
                    f"[client_meta] audio sample_rate 범위 이탈 ({sr_i}) — 누락",
                    flush=True,
                )
        except (TypeError, ValueError):
            pass
    # 옵셔널: channels (1~8 일반 범위, 7.1까지 커버)
    ch = data.get("channels")
    if ch is not None:
        try:
            ch_i = int(ch)
            if 1 <= ch_i <= 8:
                result["channels"] = ch_i
            else:
                print(
                    f"[client_meta] audio channels 범위 이탈 ({ch_i}) — 누락",
                    flush=True,
                )
        except (TypeError, ValueError):
            pass
    return result


def _make_scene_preview(
    project_id: str,
    scene_id: int,
    force: bool = False,
) -> Path | None:
    """씬 미리보기 mp4를 생성하고 경로를 반환한다.

    현재 씬 추출 길이:
    - TTS 생성 완료: max(footage 원본 길이, tts_duration + 패딩 1초)
    - TTS 없음: footage 원본 길이 전체
    전/후 씬: 각 2초, xfade fade 0.6초 전환.
    """
    import tempfile, shutil

    p = store.get_project(project_id)
    if not p:
        return None

    scenes = p.scenes
    idx = next((i for i, s in enumerate(scenes) if s.scene_id == scene_id), None)
    if idx is None:
        return None

    preview_dir = store.get_cache_dir(project_id) / "previews"
    preview_dir.mkdir(exist_ok=True)
    out = preview_dir / f"scene_{scene_id:03d}.mp4"

    if out.exists() and not force:
        # 캐시 해상도가 현재 미리보기 출력 해상도와 다르면 재생성 (#254 다운스케일 반영)
        _cached_size = _ffprobe_video_size(str(out))
        _raw_w = p.output_width if hasattr(p, "output_width") and p.output_width else 1280
        _raw_h = p.output_height if hasattr(p, "output_height") and p.output_height else 720
        _exp_w, _exp_h = _compute_preview_size(_raw_w, _raw_h)
        # state.json이 캐시보다 새로우면 폐기 — 씬 삭제/분할/TTS 갱신 시 옛 오디오가 합성된 캐시 반환 방지
        _state_path = store.get_cache_dir(project_id).parent / "state.json"
        _state_newer = False
        try:
            if _state_path.exists() and _state_path.stat().st_mtime > out.stat().st_mtime:
                _state_newer = True
        except OSError:
            pass
        if _state_newer or (_cached_size and (_cached_size[0] != _exp_w or _cached_size[1] != _exp_h)):
            out.unlink(missing_ok=True)
        else:
            return out

    cur_scene = scenes[idx]
    cur_file = _get_footage_file(cur_scene, p.footage_dir)
    if not cur_file:
        return None

    _cp = _ensure_proxy(project_id, Path(cur_file).name)
    if _cp and _cp.exists():
        cur_file = str(_cp)
    footage_dur = _ffprobe_duration(cur_file)
    if footage_dur is None:
        return None

    SIDE_DUR = 2.0   # 전/후 씬 추출 길이
    DISSOLVE = 0.6   # 전환 길이
    TTS_PAD = 0.5    # TTS보다 짧을 때 앞뒤 공백

    # 현재 씬 미리보기 길이 결정
    # #393 — narration_segments (N>=1) 를 합성한 merged WAV 를 사용한다.
    # 옛 코드는 단일 `narration_scene_NNN.wav` (=seg_0 의 결과물처럼 보이지만 실제로는
    # render 시점에 합성된 파일) 만 참조해 segment 추가분이 미리보기에 안 들렸다.
    # 이제는 preview 시점에서 직접 segment WAV 를 모아 merged 를 만든 뒤 합성에 사용.
    narration_dir = store.get_output_dir(project_id) / "narration"
    legacy_wav_path = narration_dir / f"narration_scene_{scene_id:03d}.wav"

    tts = cur_scene.tts
    # narration_text 가 비면 옛 wav/메타가 남아있어도 TTS 를 무시한다(잔재 누수 차단).
    # 진실의 원천은 narration_text 또는 narration_segments 중 하나라도 의미 있는 텍스트.
    _has_narration_text = bool((cur_scene.narration_text or "").strip())
    _segs_active = [
        seg for seg in (cur_scene.narration_segments or [])
        if getattr(seg, "text", None) and seg.text.strip()
    ]
    _has_narration = _has_narration_text or bool(_segs_active)

    # preview 전용 merged WAV — segment 들로부터 매번 새로 합성한다.
    # render(/api/.../render) 가 만든 narration_scene_NNN.wav 와 충돌하지 않도록 별도 캐시 경로.
    preview_merged_wav = preview_dir / f"_narration_merged_scene_{scene_id:03d}.wav"
    merged_dur = 0.0
    if _segs_active:
        # 새 segment 가 추가됐을 수 있으므로 항상 새로 합성 (기존 캐시 무효화).
        preview_merged_wav.unlink(missing_ok=True)
        try:
            from ..narration_merge import build_merged_wav as _merge_wav
            merged_dur = _merge_wav(cur_scene, narration_dir, preview_merged_wav)
        except Exception as _merge_err:
            print(f"[preview] merged wav 합성 예외 scene={scene_id}: {_merge_err}", flush=True)
            merged_dur = 0.0

    if merged_dur > 0 and preview_merged_wav.exists() and preview_merged_wav.stat().st_size > 0:
        # 모든 segment 끝 시각(merged_dur) 까지 영상을 늘려 잘리지 않도록.
        tts_dur = merged_dur
        wav_path = preview_merged_wav
        main_dur = max(footage_dur, tts_dur + TTS_PAD * 2)
    elif (_has_narration_text and tts and tts.synced and tts.duration_sec > 0
          and legacy_wav_path.exists() and legacy_wav_path.stat().st_size > 0):
        # 폴백: segments 가 비어있고 옛 단일 wav 만 있는 레거시 프로젝트.
        tts_dur = tts.duration_sec
        wav_path = legacy_wav_path
        main_dur = max(footage_dur, tts_dur + TTS_PAD * 2)
    else:
        wav_path = legacy_wav_path  # 실제 사용 안 함 (아래 분기에서 wav_candidate 도 재계산)
        main_dur = footage_dur

    tmp = Path(tempfile.mkdtemp(prefix="vag_preview_"))
    try:
        clips: list[str] = []

        # 미리보기 다운스케일 (#254) — 출력 해상도가 PREVIEW_MAX_H 보다 크면 비율 유지하며 축소
        _raw_sc_w = p.output_width if hasattr(p, "output_width") and p.output_width else 1280
        _raw_sc_h = p.output_height if hasattr(p, "output_height") and p.output_height else 720
        _sc_w, _sc_h = _compute_preview_size(_raw_sc_w, _raw_sc_h)
        SCALE_VF = f"scale={_sc_w}:{_sc_h}:force_original_aspect_ratio=decrease,pad={_sc_w}:{_sc_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"

        def _has_audio(path: str) -> bool:
            r = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            return "audio" in r.stdout

        # #361 — 하드웨어 가속 활성화 시 입력/인코더 인자.
        # 디코딩은 입력 옵션이므로 `-i` 앞에 와야 한다 (ffmpeg 인자 순서 규칙).
        _hw_in = _ffmpeg_input_hwaccel_args()
        _venc = _ffmpeg_video_encoder_args()

        # 이전 씬 clip (마지막 SIDE_DUR초). 오디오는 항상 제거 — 현재 씬을 평가할 때
        # 전/후 씬의 footage 사운드(특히 같은 footage 분할 케이스)가 끼어들어 잔재처럼
        # 들리는 것을 막는다. 시각적 dissolve 컨텍스트만 제공.
        if idx > 0:
            prev_file = _get_footage_file(scenes[idx - 1], p.footage_dir)
            if prev_file:
                prev_total = _ffprobe_duration(prev_file) or SIDE_DUR
                prev_start = max(0.0, prev_total - SIDE_DUR)
                prev_clip = str(tmp / "prev.mp4")
                subprocess.run(
                    [ffmpeg_bin(), "-y", *_hw_in, "-ss", f"{prev_start:.3f}", "-i", prev_file,
                     "-t", f"{SIDE_DUR:.3f}",
                     "-vf", SCALE_VF,
                     *_venc, "-an",
                     prev_clip],
                    capture_output=True,
                )
                if Path(prev_clip).exists():
                    clips.append(prev_clip)

        # 현재 씬 clip (처음부터 main_dur초, 원본 오디오 포함)
        cur_clip = str(tmp / "cur.mp4")
        cur_has_audio = _has_audio(cur_file)
        if main_dur > footage_dur:
            freeze_dur = main_dur - footage_dur
            subprocess.run(
                [ffmpeg_bin(), "-y", *_hw_in, "-i", cur_file,
                 "-vf", f"{SCALE_VF},tpad=stop_mode=clone:stop_duration={freeze_dur:.3f}",
                 *_venc,
                 *([] if cur_has_audio else ["-an"]),
                 *(["-c:a", "aac", "-b:a", "128k"] if cur_has_audio else []),
                 cur_clip],
                capture_output=True,
            )
        else:
            subprocess.run(
                [ffmpeg_bin(), "-y", *_hw_in, "-i", cur_file,
                 "-t", f"{main_dur:.3f}",
                 "-vf", SCALE_VF,
                 *_venc,
                 *([] if cur_has_audio else ["-an"]),
                 *(["-c:a", "aac", "-b:a", "128k"] if cur_has_audio else []),
                 cur_clip],
                capture_output=True,
            )
        if not Path(cur_clip).exists():
            return None
        clips.append(cur_clip)

        # 다음 씬 clip (처음 SIDE_DUR초). 오디오 제거 이유는 prev_clip과 동일.
        if idx < len(scenes) - 1:
            next_file = _get_footage_file(scenes[idx + 1], p.footage_dir)
            if next_file:
                next_clip = str(tmp / "next.mp4")
                subprocess.run(
                    [ffmpeg_bin(), "-y", *_hw_in, "-ss", "0", "-i", next_file,
                     "-t", f"{SIDE_DUR:.3f}",
                     "-vf", SCALE_VF,
                     *_venc, "-an",
                     next_clip],
                    capture_output=True,
                )
                if Path(next_clip).exists():
                    clips.append(next_clip)

        # ── Step 1: 비디오+오디오 xfade+acrossfade 합성 → combined.mp4 ────
        # clips 각각 오디오 유무 파악
        clips_have_audio = [_has_audio(c) for c in clips]
        any_audio = any(clips_have_audio)

        combined = str(tmp / "combined.mp4")
        durations: list[float] = [_ffprobe_duration(c) or SIDE_DUR for c in clips]

        eff_dissolve = DISSOLVE
        min_dur = min(durations)
        if eff_dissolve >= min_dur / 2.0:
            eff_dissolve = min_dur / 4.0

        if len(clips) == 1:
            shutil.copy2(clips[0], combined)
        else:
            inputs_args: list[str] = []
            for c in clips:
                inputs_args += ["-i", c]

            filter_parts: list[str] = []
            acc_offset = 0.0
            prev_v = "0:v"
            for i in range(1, len(clips)):
                acc_offset += durations[i - 1] - eff_dissolve
                offset = max(0.001, acc_offset)
                nv = f"v{i:02d}"
                filter_parts.append(
                    f"[{prev_v}][{i}:v]xfade=transition=fade"
                    f":duration={eff_dissolve:.3f}:offset={offset:.3f}[{nv}]"
                )
                prev_v = nv

            # 오디오: 각 클립 오디오를 silence pad 후 concat
            if any_audio:
                # 오디오 없는 클립은 aevalsrc로 silence 생성
                a_inputs: list[str] = []
                for i, (c, has_a, dur) in enumerate(zip(clips, clips_have_audio, durations)):
                    if has_a:
                        a_inputs.append(f"[{i}:a]apad=whole_dur={dur:.3f}[ap{i}]")
                    else:
                        a_inputs.append(
                            f"aevalsrc=0:channel_layout=stereo:sample_rate=44100:duration={dur:.3f}[ap{i}]"
                        )
                filter_parts += a_inputs

                # acrossfade chain
                acc_a_offset = 0.0
                prev_a = "ap0"
                for i in range(1, len(clips)):
                    na = f"a{i:02d}"
                    filter_parts.append(
                        f"[{prev_a}][ap{i}]acrossfade=d={eff_dissolve:.3f}[{na}]"
                    )
                    prev_a = na

                filter_complex = ";".join(filter_parts)
                r = subprocess.run(
                    [ffmpeg_bin(), "-y", *inputs_args,
                     "-filter_complex", filter_complex,
                     "-map", f"[{prev_v}]", "-map", f"[{prev_a}]",
                     *_venc,
                     "-c:a", "aac", "-b:a", "128k",
                     combined],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
            else:
                filter_complex = ";".join(filter_parts)
                r = subprocess.run(
                    [ffmpeg_bin(), "-y", *inputs_args,
                     "-filter_complex", filter_complex,
                     "-map", f"[{prev_v}]",
                     *_venc, "-an",
                     combined],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
            if r.returncode != 0 or not Path(combined).exists():
                return None

        # ── Step 2: TTS WAV를 원본 오디오에 amix로 오버랩 ────────────────
        # cur_clip의 combined 내 시작 위치 계산
        cur_clip_list_idx = 1 if len(clips) > 1 and clips[0] == str(tmp / "prev.mp4") else 0
        prev_dur_sum = 0.0
        for i in range(cur_clip_list_idx):
            prev_dur_sum += durations[i]
        audio_delay_sec = max(0.0, prev_dur_sum - eff_dissolve * cur_clip_list_idx)

        # #393 — preview 가 위에서 결정한 wav_path 를 그대로 사용.
        #   - merged_dur>0: preview_merged_wav (segments 합성본)
        #   - 폴백: legacy_wav_path (옛 단일 narration_scene_NNN.wav)
        # narration_text/segments 가 비면 wav 무시 — 위 main_dur 결정과 동일한 가드.
        tts_wav: str | None = None
        if merged_dur > 0 and wav_path.exists() and wav_path.stat().st_size > 0:
            tts_wav = str(wav_path)
        elif (_has_narration_text and tts and tts.synced and tts.duration_sec > 0
                and wav_path.exists() and wav_path.stat().st_size > 0):
            tts_wav = str(wav_path)

        if tts_wav:
            total_vid_dur = _ffprobe_duration(combined) or main_dur
            delay_ms = int(audio_delay_sec * 1000)
            combined_has_audio = _has_audio(combined)
            if combined_has_audio:
                # 원본 오디오(씬별 음량) + TTS(볼륨 1.0) amix. footage_audio_volume 0=음소거, 0.3=기본 더킹, 1.0=풀.
                # normalize=0: amix 기본은 입력 개수만큼 자동 감쇠(2개면 ×0.5)라 TTS가 절반으로 줄어
                # "마치 환경음처럼" 들리는 회귀가 있었다. final_composer와 동일하게 normalize 끄고 명시 볼륨만 적용.
                _fav = getattr(cur_scene, "footage_audio_volume", 0.3)
                r = subprocess.run(
                    [ffmpeg_bin(), "-y",
                     "-i", combined,
                     "-i", tts_wav,
                     "-filter_complex",
                     f"[0:a]volume={_fav}[orig];"
                     f"[1:a]adelay={delay_ms}|{delay_ms},"
                     f"apad=whole_dur={total_vid_dur:.3f}[tts];"
                     f"[orig][tts]amix=inputs=2:normalize=0:duration=first[aout]",
                     "-map", "0:v", "-map", "[aout]",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                     str(out)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
            else:
                # 원본 오디오 없음 → TTS만
                r = subprocess.run(
                    [ffmpeg_bin(), "-y",
                     "-i", combined,
                     "-i", tts_wav,
                     "-filter_complex",
                     f"[1:a]adelay={delay_ms}|{delay_ms},"
                     f"apad=whole_dur={total_vid_dur:.3f}[a]",
                     "-map", "0:v", "-map", "[a]",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                     str(out)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
            if r.returncode != 0 or not out.exists():
                shutil.copy2(combined, str(out))
        else:
            # TTS 가 없는 씬 — 예전에는 combined 를 그대로 복사했다. 그 경로에는
            # footage_audio_volume 이 한 번도 적용되지 않아, 원본음량을 0(음소거)으로
            # 설정해도 미리보기에서 원본 소리가 그대로 들리는 버그가 있었다.
            # (음량 적용은 위 `if tts_wav:` 분기 안에만 있었다.)
            _copy_footage_with_volume(combined, str(out), cur_scene)

        return out if out.exists() else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@router.get("/{project_id}/scenes/{scene_id}/render-spec")
def scene_render_spec(project_id: str, scene_id: int) -> dict:
    """씬 1개의 RenderSpec(JSON)을 반환한다. (#309)

    클라이언트 단순 렌더(영상 1개 + fit + speed만 있는 씬)가 백엔드 ffmpeg를 호출하지 않고
    `<video>` + CSS만으로 미리보기를 그릴 수 있도록 RenderSpec을 그대로 노출한다.

    - 빌더는 항상 같은 함수(`build_render_spec`)를 호출하여 클라/서버 결과가 1:1로 정합한다.
    - footage 메타(해상도/길이)는 가능하면 ffprobe로 측정. footage가 없거나 측정 실패면
      빌더 기본값을 사용 — fit 픽셀 계산 결과는 동일하다.
    - 단순 모드 판정(`isSimpleRenderSpec`)은 *프론트 책임*. 본 엔드포인트는 RenderSpec만
      제공하고, "단순한지 여부"는 layers 구성을 보고 클라가 직접 결정한다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, "씬을 찾을 수 없습니다")

    from src.render_spec.builder import build_render_spec
    from src.render_spec.models import Size as _RsSize

    # footage 해상도/길이 측정 — 실패 시 빌더 기본값(1920×1080) 사용.
    footage_size = None
    footage_duration_sec = None
    if scene.footage and p.footage_dir:
        fname = getattr(scene.footage, "file", None) or getattr(scene.footage, "image", None)
        if fname:
            footage_path = Path(p.footage_dir) / fname
            if footage_path.exists():
                size = _ffprobe_video_size(str(footage_path))
                if size:
                    footage_size = _RsSize(width=size[0], height=size[1])
                else:
                    # 이미지일 가능성 — PIL로 측정 시도
                    try:
                        from PIL import Image as _PilImage
                        with _PilImage.open(str(footage_path)) as im:
                            w, h = im.size
                            footage_size = _RsSize(width=int(w), height=int(h))
                    except Exception:
                        footage_size = None
                # 영상이면 duration도 측정
                dur = _ffprobe_duration(str(footage_path))
                if dur and dur > 0:
                    footage_duration_sec = dur

    try:
        spec = build_render_spec(
            scene,
            p,
            footage_size=footage_size,
            footage_duration_sec=footage_duration_sec,
        )
    except Exception as e:
        raise HTTPException(500, f"RenderSpec 빌드 실패: {e}")

    import json as _json
    return _json.loads(spec.model_dump_json())


@router.get("/{project_id}/scenes/{scene_id}/preview")
def scene_preview(project_id: str, scene_id: int, force: bool = False):
    # #301 — 캐시 미스/force 시에만 실제 ffmpeg 비용 발생.
    # 캐시 히트면 wall-clock이 ms 단위라 비용 거의 0이지만 그대로 기록 (egress 작게나마).
    import time as _t_preview
    preview_dir = store.get_cache_dir(project_id) / "previews"
    cache_path = preview_dir / f"scene_{scene_id:03d}.mp4"
    cache_existed = cache_path.exists() and not force

    _t0 = _t_preview.monotonic()
    out = _make_scene_preview(project_id, scene_id, force=force)
    elapsed = _t_preview.monotonic() - _t0

    if not out or not out.exists():
        raise HTTPException(404, "미리보기를 생성할 수 없습니다")

    # 캐시 미스로 실제 ffmpeg가 돌았을 때만 의미 있는 CPU 시간 기록.
    # 캐시 히트면 egress(다운로드 크기)만 누적.
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            preview_bytes = out.stat().st_size if out.exists() else 0
            _est.record_scene_preview(
                cpu_seconds=elapsed,
                output_bytes=preview_bytes,
                cache_hit=cache_existed,
            )
    except Exception as _cost_err:
        print(f"[cost] scene_preview 기록 실패: {_cost_err}")

    return FileResponse(str(out), media_type="video/mp4")


@router.delete("/{project_id}/scenes/{scene_id}/preview-cache")
def delete_preview_cache(project_id: str, scene_id: int):
    """씬 미리보기 캐시를 명시적으로 무효화한다.

    GET 경로의 `previews/scene_NNN.mp4` 와, POST 경로의 최종 캐시(`scene-NNN/final-*.mp4`)를
    함께 지운다. 최종 캐시만 남기면 "캐시 지웠는데 옛 영상이 계속 나온다" 가 되므로
    두 경로를 반드시 같이 비운다.

    중간 단계(trim/speed) 캐시는 입력 파일 실체(mtime+size) 기반이라 잘못된 결과를
    낼 수 없으므로 유지한다 — 재생성 비용만 아끼는 안전한 보존.
    """
    cache_dir = store.get_cache_dir(project_id)
    preview_path = cache_dir / "previews" / f"scene_{scene_id:03d}.mp4"
    preview_path.unlink(missing_ok=True)
    try:
        from src.render_spec import preview_stages as _stages_del
        _stages_del.purge_stage(cache_dir, scene_id, "final")
    except Exception as _purge_err:
        print(f"[preview-cache] final purge 실패(무시): {_purge_err}", flush=True)
    return {"ok": True}


# ── 미리보기 생성 동시성 제어 ───────────────────────────────────────────────
#
# 이어보기 프리페치는 여러 씬의 미리보기를 동시에 요청한다. 제한이 없으면 서버가
# ffmpeg 를 요청 수만큼 띄워 CPU 를 나눠 갖고, 각 인코딩이 전부 느려진다(실측:
# 캐시 미스 씬 1건이 12~72초). 상한을 2 로 둔 근거:
#   - ffmpeg 한 건이 이미 멀티스레드로 CPU 대부분을 쓴다(실측 570% CPU). 병렬도를
#     올려도 총 처리량은 거의 늘지 않고 개별 지연만 길어진다.
#   - 1 로 두면 사용자가 보고 있는 씬이 프리페치 뒤에 줄을 서게 된다. 2 면 프리페치
#     1건이 돌고 있어도 사용자 클릭이 곧바로 슬롯을 잡는다.
# 필요하면 환경변수 VAG_PREVIEW_CONCURRENCY 로 조정한다.
try:
    _PREVIEW_CONCURRENCY = max(1, int(os.environ.get("VAG_PREVIEW_CONCURRENCY", "2")))
except ValueError:
    _PREVIEW_CONCURRENCY = 2
_PREVIEW_SLOTS = _keyed_locks.BoundedSlots(_PREVIEW_CONCURRENCY)
# 슬롯 대기 상한. 초과 시 503 으로 빠르게 돌려보낸다(요청이 매달리지 않게).
PREVIEW_SLOT_TIMEOUT_SEC = 300.0
# 같은 (project_id, scene_id) 동시 요청 합류 — 프리페치와 사용자 클릭이 같은 씬에
# 겹치는 실제 상황에서 ffmpeg 를 두 번 돌리지 않는다.
_PREVIEW_INFLIGHT = _keyed_locks.SingleFlight()


@router.post("/{project_id}/scenes/{scene_id}/preview")
def scene_preview_render(
    project_id: str,
    scene_id: int,
    background_tasks: BackgroundTasks,
):
    """씬 미리보기 렌더 — 동시성 게이트를 씌운 얇은 래퍼.

    실제 생성 로직은 `_scene_preview_render_impl` 에 있다. 여기서 하는 일은 두 가지뿐:
      1. 같은 (project_id, scene_id) 의 동시 요청을 하나로 합친다(single-flight).
      2. 프로젝트 전체 동시 생성 수를 `_PREVIEW_CONCURRENCY` 로 제한한다.

    **합류자는 리더의 응답 객체를 공유하지 않는다.** 미스 경로의 `FileResponse` 는
    요청별 `BackgroundTasks` 로 지워지는 임시 파일을 가리키므로, 두 응답이 같은 객체를
    쓰면 한쪽이 스트리밍하는 도중 다른 쪽 cleanup 이 파일을 지울 수 있다. 대신 합류자는
    리더가 끝나기를 기다렸다가 impl 을 **다시** 호출한다 — 리더가 최종 캐시를 채워둔
    뒤이므로 이 재호출은 캐시 적중(~0.12초)이고, 중복 ffmpeg 는 일어나지 않는다.

    주의: 캐시 적중 판정은 impl 안(그리고 `_ensure_proxy` 안)에서 하므로, 적중 요청도
    슬롯을 한 번 통과한다. 적중은 ~0.12초라 슬롯 점유가 사실상 없고, 반대로 슬롯 밖에서
    적중을 따로 판정하려면 키 계산(씬 상태 해시)을 이중으로 해야 해 득이 없다.
    """
    # 슬롯은 single-flight **안쪽** 에서 잡는다. 반대로 하면 합류자(follower)가
    # 슬롯을 쥔 채 선행 작업을 기다려 슬롯이 고갈된다(리더가 슬롯을 못 잡는 교착).
    def _run():
        with _PREVIEW_SLOTS.slot(PREVIEW_SLOT_TIMEOUT_SEC):
            return _scene_preview_render_impl(project_id, scene_id, background_tasks)

    key = (project_id, scene_id)
    try:
        result, leader = _PREVIEW_INFLIGHT.do(key, _run, PREVIEW_SLOT_TIMEOUT_SEC)
        if leader:
            return result
        # 합류자 — 리더 결과를 재사용하지 않고 캐시 적중을 노려 다시 만든다.
        return _run()
    except _keyed_locks.LockTimeout:
        print(f"[preview] 동시 실행 대기 시간 초과 scene={scene_id}", flush=True)
        raise HTTPException(503, "미리보기 생성이 혼잡합니다. 잠시 후 다시 시도해 주세요")


def _scene_preview_render_impl(
    project_id: str,
    scene_id: int,
    background_tasks: BackgroundTasks,
):
    """TTS 길이에 맞게 footage를 늘리고 TTS 오디오를 합성한 미리보기 mp4를 반환한다.

    - footage 없음: 400
    - footage_dir 없음: 400
    - TTS 파일 없음: footage만 그대로 반환 (오디오 합성 스킵)
    - TTS > footage 길이: Time Warp(0.5 이상) 또는 마지막 프레임 프리즈(0.5 미만)
    """
    import tempfile, shutil, time as _t_prev_render
    _preview_t0 = _t_prev_render.monotonic()

    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, "씬을 찾을 수 없습니다")

    if not scene.footage:
        raise HTTPException(400, "씬에 footage가 배정되지 않았습니다")

    if not p.footage_dir:
        raise HTTPException(400, "footage_dir이 설정되지 않았습니다")

    footage_dir = Path(p.footage_dir)
    if not footage_dir.exists():
        raise HTTPException(400, "footage 폴더가 존재하지 않습니다")

    # footage 파일 경로 결정
    fname = getattr(scene.footage, "file", None) or getattr(scene.footage, "image", None)
    if not fname:
        raise HTTPException(400, "씬 footage에 파일이 지정되지 않았습니다")

    footage_path = footage_dir / fname
    # preview-final-cache — 프록시로 교체되기 *전* 의 원본 경로를 따로 보관한다.
    # `_ensure_proxy` 가 원본 변경 시 프록시를 재생성하긴 하지만, 재생성된 프록시의
    # mtime/size 가 우연히 이전과 같을 수 있고(동일 크기 재인코딩) 프록시를 아예
    # 타지 않는 경로도 있다. 외부 수정 감지를 프록시에 위임하지 않도록 원본 sig 를
    # 반드시 함께 캐시 키에 넣는다.
    _origin_footage_path = footage_path
    _proxy = _ensure_proxy(project_id, fname)
    if _proxy and _proxy.exists():
        footage_path = _proxy
    if not footage_path.exists():
        raise HTTPException(400, f"footage 파일을 찾을 수 없습니다: {fname}")

    # TTS 파일 경로 (없으면 오디오 합성 스킵)
    # 씬 삭제·분할로 scene_id가 재번호되면 옛 wav 파일과 옛 tts 메타가 다음 씬에
    # 잔재로 남는 경우가 있다. 진실의 원천은 narration_text 하나로만 본다 —
    # narration_text가 비면 tts 메타가 살아 있어도 디스크 wav 무시.
    #
    # #403 — narration_segments(N>=1) 합성 결과(merged WAV)를 우선 사용한다.
    # #393 가 GET 경로(`_make_scene_preview`) 만 고쳤기 때문에 사용자가 실제로 호출하는
    # POST 경로(`scene_preview_render`) 가 여전히 옛 단일 wav 만 보고 seg_1+ 의 TTS 가
    # 미리보기에서 들리지 않는 회귀가 남아 있었다. 여기서도 동일한 segment 통합 로직을 둔다.
    narration_dir = store.get_output_dir(project_id) / "narration"
    legacy_tts_path = narration_dir / f"narration_scene_{scene_id:03d}.wav"
    _has_narration_intent = bool((scene.narration_text or "").strip())

    _segs_active = [
        seg for seg in (scene.narration_segments or [])
        if getattr(seg, "text", None) and seg.text.strip()
    ]
    _preview_merged_wav: Path | None = None
    _merged_dur = 0.0
    if _segs_active:
        # 임시 wav 경로 — _cleanup() 에서 응답 후 삭제. mktemp 는 race 위험이 있지만
        # 미리보기 한 호출 안에서 즉시 쓰고 지우므로 충돌 가능성이 낮다.
        _tmp_merged_path = Path(tempfile.mktemp(suffix=".wav", prefix=f"vag_seg_merged_{scene_id:03d}_"))
        try:
            from ..narration_merge import build_merged_wav as _merge_wav
            _merged_dur = _merge_wav(scene, narration_dir, _tmp_merged_path)
        except Exception as _merge_err:
            print(f"[preview-render] merged wav 합성 실패 scene={scene_id}: {_merge_err}", flush=True)
            _merged_dur = 0.0
        if _merged_dur > 0 and _tmp_merged_path.exists() and _tmp_merged_path.stat().st_size > 0:
            _preview_merged_wav = _tmp_merged_path

    if _preview_merged_wav is not None:
        tts_path = _preview_merged_wav
        has_tts = True
    else:
        tts_path = legacy_tts_path
        has_tts = _has_narration_intent and tts_path.exists() and tts_path.stat().st_size > 0

    # 이미지 파일 여부 확인
    _image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    is_image_footage = footage_path.suffix.lower() in _image_exts

    # #270: 씬 간 dissolve 전환 시간 반영
    # 최종 렌더링에서 첫/끝 씬은 한쪽만 dissolve, 중간 씬은 양쪽 dissolve가 적용된다.
    # 미리보기는 dissolve 효과 자체는 표현하지 않지만 footage 길이 계산에는 반영하여
    # 사용자가 실제 최종 결과와 같은 길이감으로 검증할 수 있게 한다.
    from src.clip_editor import DISSOLVE_SEC  # 0.5s
    _scene_idx = next((i for i, s in enumerate(p.scenes) if s.scene_id == scene_id), 0)
    _is_edge = (_scene_idx == 0) or (_scene_idx == len(p.scenes) - 1)
    dissolve_pad = DISSOLVE_SEC * (1 if _is_edge else 2)

    # footage_speed: 1.0 외의 값이면 setpts로 영상 속도 조절
    footage_speed = scene.footage_speed if hasattr(scene, "footage_speed") else 1.0
    if footage_speed is None:
        footage_speed = 1.0
    # 재생 속도 모드(constant/ramp) — 수학은 src/utils/speed_ramp 단일 헬퍼.
    from src.utils.speed_ramp import scene_speed_params as _scene_speed_params, scene_speed_base as _scene_speed_base
    _spd_mode, _, _spd_hold, _spd_accel = _scene_speed_params(scene)
    _spd_base = _scene_speed_base(scene)

    # ── preview-final-cache ──────────────────────────────────────────────────
    # 최종 산출물(Stage 6) 캐시 조회. 적중이면 아래 ffmpeg 파이프라인 전체를 건너뛴다.
    #
    # 도입 배경(실측): 씬 전환 1회당 ffmpeg 전 구간 재인코딩 3.3~3.5초 / CPU 570%.
    # 기존 trim/speed 캐시는 중간 산출물이라 합성·인코딩 비용이 매번 그대로 발생했다.
    #
    # 무효화 프로토콜:
    #   (1) 푸티지를 써드파티 세션에서 직접 고친 경우 — 서버는 통보를 못 받는다.
    #       그래서 캐시 키에 파일 실체(file_signature = name+mtime+size)를 넣는다.
    #       파일이 바뀌면 hash 가 달라져 자동으로 미스가 된다.
    #   (2) 그 외(변경 없음) — 캐시를 그대로 사용해 ffmpeg 를 건너뛴다.
    #
    # 키 구성은 보수적이다: 씬 상태 dict 를 통째로 넣고(모델에 새 효과 필드가 추가돼도
    # 자동 포함) 렌더에 무관한 필드만 명시적으로 제외한다. "편집했는데 옛 영상이
    # 계속 보이는" 실패 모드를 신규 필드 누락으로 재발시키지 않기 위함.
    from src.render_spec import preview_stages as _stages
    from src.utils.ffmpeg_utils import normalize_bg_color as _normalize_bg_color

    _raw_w = p.output_width if getattr(p, "output_width", None) else 1920
    _raw_h = p.output_height if getattr(p, "output_height", None) else 1080
    _final_out_w, _final_out_h = _compute_preview_size(_raw_w, _raw_h)
    _bg_norm = _normalize_bg_color(getattr(p, "bg_color", "black"))
    _project_cache_dir = store.get_cache_dir(project_id)

    _final_hash: str | None = None
    try:
        # narration WAV 시그니처 — TTS 재생성/외부 교체 감지.
        # 참조 가능한 모든 wav(legacy + segment 별)를 훑는다. 없는 파일도 {"missing": True}
        # 로 키에 남겨, "없다가 생긴" 전이도 미스가 되게 한다.
        from ..narration_merge import segment_wav_path as _seg_wav_path
        _narr_sigs: list[dict] = [_stages.file_signature(legacy_tts_path)]
        for _seg in (scene.narration_segments or []):
            _narr_sigs.append(
                _stages.file_signature(_seg_wav_path(narration_dir, scene_id, _seg.id))
            )

        # 이미지 오버레이가 참조하는 자산 파일 시그니처 — 자산 교체 감지.
        _asset_sigs: list[dict] = []
        if scene.image_overlays:
            _asset_by_id = {
                _a.asset_id: _a.filename for _a in (p.studio_assets or [])
            }
            for _ov in scene.image_overlays:
                _fn = _asset_by_id.get(getattr(_ov, "image_asset_id", "") or "")
                _asset_sigs.append(
                    _stages.file_signature(footage_dir / _fn) if _fn else {"missing": True}
                )

        _final_key = _stages.FinalStageKey(
            scene_state=scene.model_dump(mode="json"),
            # 원본 + 실제 입력(프록시일 수 있음) 양쪽의 실체를 모두 잡는다.
            # 원본 sig 가 외부 수정 감지의 핵심이고(프록시는 stale 일 수 있음),
            # 입력 sig 는 프록시가 재생성된 경우를 잡는다.
            footage_sig={
                "origin": _stages.file_signature(_origin_footage_path),
                "input": _stages.file_signature(footage_path),
            },
            narration_sigs=_narr_sigs,
            asset_sigs=_asset_sigs,
            canvas_w=_final_out_w,
            canvas_h=_final_out_h,
            output_h=_raw_h,
            bg_color=_bg_norm,
            dissolve_pad=dissolve_pad,
            scene_index=_scene_idx,
            scene_count=len(p.scenes),
        )
        _final_hash = _final_key.hash()
    except Exception as _fk_err:
        # 키 계산 실패 → 캐시를 쓰지 않고 정상 재생성(보수적 폴백).
        print(f"[preview-cache] final key 계산 실패(캐시 미사용): {_fk_err}", flush=True)
        _final_hash = None

    if _final_hash:
        _final_lookup = _stages.lookup(_project_cache_dir, scene_id, "final", _final_hash)
        if _final_lookup.hit:
            print(_stages.log_line(scene_id, [_final_lookup]), flush=True)
            # 임시 merged wav 는 캐시 적중 시에도 만들어졌을 수 있으므로 정리.
            if _preview_merged_wav is not None:
                background_tasks.add_task(_preview_merged_wav.unlink, True)
            try:
                from ..cost_tracker import measure_phase as _mp_hit
                with _mp_hit(project_id) as _est_hit:
                    _est_hit.record_scene_preview(
                        cpu_seconds=_t_prev_render.monotonic() - _preview_t0,
                        output_bytes=_final_lookup.cached.stat().st_size,
                        cache_hit=True,
                    )
            except Exception as _cost_err:
                print(f"[cost] scene_preview_render(cache hit) 기록 실패: {_cost_err}")
            return FileResponse(
                str(_final_lookup.cached),
                media_type="video/mp4",
                filename=f"preview_scene_{scene_id:03d}.mp4",
            )

    # 씬 나누기 — clip_start_sec/clip_end_sec이 지정되어 있으면 footage를 해당 구간으로 자른 임시 파일로 대체.
    # 이후 모든 처리(footage_dur, speed, kenburns 등)는 자른 결과 기준으로 동작.
    # #370 — 단계별 캐싱: 같은 footage + 같은 (cs, ce) 면 stage1 캐시 적중 시 ffmpeg 호출 생략.
    _trim_tmp_ref: list[str] = []
    # 단계 캐시 적중/미스 추적 (디버깅·효용 측정용).
    _stage_results: list[_stages.StageCacheResult] = []
    # Stage 1 결과의 hash — Stage 2 cache key 계산에 사용. trim 없을 땐 원본 footage file_sig 의 안정 해시.
    _stage1_hash: str | None = None
    if not is_image_footage:
        _cs = getattr(scene.footage, "clip_start_sec", None)
        _ce = getattr(scene.footage, "clip_end_sec", None)
        if (_cs is not None and _cs > 0) or _ce is not None:
            _trim_key = _stages.TrimStageKey(
                input_path=str(footage_path),
                input_sig=_stages.file_signature(footage_path),
                clip_start_sec=float(_cs or 0.0),
                clip_end_sec=float(_ce) if _ce is not None else None,
            )
            _trim_hash = _trim_key.hash()
            _stage1_hash = _trim_hash
            _project_cache = store.get_cache_dir(project_id)
            _trim_lookup = _stages.lookup(_project_cache, scene_id, "trim", _trim_hash)
            _stage_results.append(_trim_lookup)
            if _trim_lookup.hit:
                # 캐시 적중 — ffmpeg 호출 건너뛰고 캐시 파일을 다음 단계 입력으로 사용.
                # cleanup 대상에는 추가하지 않는다(캐시는 유지되어야 한다).
                footage_path = _trim_lookup.cached  # type: ignore[assignment]
            else:
                _trim_fd, _trim_path = tempfile.mkstemp(suffix=".mp4", prefix="vag_trim_")
                import os as _os_t
                _os_t.close(_trim_fd)
                _ss = max(0.0, float(_cs or 0.0))
                _trim_cmd = [ffmpeg_bin(), "-y", "-ss", f"{_ss:.3f}", "-i", str(footage_path)]
                if _ce is not None and float(_ce) > _ss:
                    _trim_cmd += ["-to", f"{float(_ce) - _ss:.3f}"]
                _trim_cmd += ["-c:v", "libx264", "-crf", "23", "-preset", "fast",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", _trim_path]
                _rt = subprocess.run(_trim_cmd, capture_output=True)
                if _rt.returncode == 0 and Path(_trim_path).exists() and Path(_trim_path).stat().st_size > 0:
                    # 단계 캐시에 보관 (= 다음 미리보기 호출에서 재사용 가능).
                    # 저장 실패는 미리보기 본흐름과 무관 — 침묵 폴백.
                    try:
                        _cached = _stages.store(_project_cache, scene_id, "trim", _trim_hash, _trim_path)
                        footage_path = _cached
                        # tmp 원본은 즉시 삭제(이중 보관 회피).
                        Path(_trim_path).unlink(missing_ok=True)
                    except Exception as _ce_err:
                        print(f"[preview-cache] trim store 실패(무시): {_ce_err}", flush=True)
                        footage_path = Path(_trim_path)
                        _trim_tmp_ref.append(_trim_path)

    # ffprobe로 footage 길이 측정 (이미지는 None → TTS 길이로 대체)
    footage_dur = None if is_image_footage else _ffprobe_duration(str(footage_path))
    if footage_dur is None and not is_image_footage:
        raise HTTPException(500, "footage 길이를 측정할 수 없습니다")

    # 임시 파일 생성
    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".mp4", prefix="vag_render_preview_")
    import os as _os
    _os.close(tmp_fd)
    tmp_path = Path(tmp_path_str)

    _speed_tmp_ref: list[str] = []  # 속도 조절 임시 파일 경로 (cleanup용)

    def _cleanup():
        tmp_path.unlink(missing_ok=True)
        for _sp in _speed_tmp_ref:
            Path(_sp).unlink(missing_ok=True)
        for _tp in _trim_tmp_ref:
            Path(_tp).unlink(missing_ok=True)
        # #403 — segment 합성용 임시 merged wav 정리.
        if _preview_merged_wav is not None:
            _preview_merged_wav.unlink(missing_ok=True)

    try:
        # 프로젝트 출력 해상도 읽기 (기본값 1920×1080)
        # preview-final-cache — 위에서 최종 캐시 키 계산에 이미 쓴 값과 *반드시* 같아야
        # 하므로 재계산하지 않고 그대로 재사용한다(키와 실제 인코딩 파라미터의 괴리 방지).
        raw_w, raw_h = _raw_w, _raw_h
        # 미리보기 다운스케일 (#254) — Phase 6 최종 렌더는 영향 없음
        out_w, out_h = _final_out_w, _final_out_h

        # 프로젝트 전역 배경색(letterbox 여백). black(기본)이면 접미사를 붙이지 않아
        # 기존 pad 문자열과 바이트 동일 — 회귀 안전.
        _bg_pad_suffix = "" if _bg_norm == "black" else f":{_bg_norm}"

        # footage_speed 적용: 1.0이 아닌 경우 setpts+atempo로 속도 조절된 임시 파일 생성
        # #370 — Stage 2 캐싱: 직전 단계 산출물(footage_path) sig + speed + canvas + fit 으로 hash.
        #         같은 입력이면 ffmpeg 인코딩을 건너뛰고 캐시 파일로 footage_path 교체.
        _speed_tmp_path: str | None = None
        if not is_image_footage and footage_speed and footage_speed != 1.0:
            import tempfile as _tempfile_speed
            # 단계 캐시 키: stage1 hash 가 있으면 그것을, 없으면 현재 footage_path 의 file_signature 안정 해시.
            _prev_h_for_speed = _stage1_hash if _stage1_hash else _stages.stage_hash(
                "raw", {"sig": _stages.file_signature(footage_path)},
            )
            _spd_fit_for_key = "contain"
            if scene.footage and getattr(scene.footage, "type", None) == "clip":
                _spd_fit_raw_for_key = getattr(scene.footage, "fit", "contain") or "contain"
                _spd_fit_for_key = _spd_fit_raw_for_key if _spd_fit_raw_for_key in ("contain", "cover", "stretch") else "contain"
            # 회전 씬은 이 단계에서 화면 맞춤(fit)을 하지 않고 원본 크기 그대로 속도만 바꾼다.
            # 여기서 회전 전 프레임을 출력 캔버스에 먼저 맞추면, 뒤의 회전 단계가 이미 맞춘
            # 캔버스를 다시 돌려 맞추면서 영상이 이중 축소된다(13번 씬 실측 2026-11-05).
            _spd_rot = 0
            if scene.footage and getattr(scene.footage, "type", None) == "clip":
                try:
                    _spd_rot = int(getattr(scene.footage, "rotation", 0) or 0)
                except (TypeError, ValueError):
                    _spd_rot = 0
            _spd_canvas_wh = (out_w, out_h)
            if _spd_rot in (90, 180, 270):
                _spd_canvas_wh = _ffprobe_video_size(str(footage_path)) or (out_w, out_h)
            _speed_key = _stages.SpeedStageKey(
                prev_hash=_prev_h_for_speed,
                footage_speed=float(footage_speed),
                canvas_w=_spd_canvas_wh[0],
                canvas_h=_spd_canvas_wh[1],
                fit=_spd_fit_for_key,
                # bg-preview-fix — 배경색 변경 시 캐시 무효화. black 이면 키에 미포함(회귀 안전).
                bg_color=_bg_norm,
                speed_mode=_spd_mode,
                ramp_hold=_spd_hold,
                ramp_accel=_spd_accel,
                ramp_base=_spd_base,
            )
            _speed_hash = _speed_key.hash()
            _project_cache = store.get_cache_dir(project_id)
            _speed_lookup = _stages.lookup(_project_cache, scene_id, "speed", _speed_hash)
            _stage_results.append(_speed_lookup)
            if _speed_lookup.hit:
                footage_path = _speed_lookup.cached  # type: ignore[assignment]
                footage_dur = _ffprobe_duration(str(footage_path)) or footage_dur
                # cleanup 대상에 추가하지 않음(캐시 유지).
                # 캐시 적중 시점에 함수 진행 흐름의 후속 footage_speed 분기를 모두 건너뛰기 위해
                # 아래의 진짜 인코딩 블록은 건너뛰는 가짜 가드를 둔다.
            else:
                _sf_fd, _speed_path_str = _tempfile_speed.mkstemp(suffix=".mp4", prefix="vag_speed_")
                _os.close(_sf_fd)
                _speed_tmp_path = _speed_path_str
        # 실제 ffmpeg 인코딩은 캐시 미스일 때만 수행.
        if (not is_image_footage and footage_speed and footage_speed != 1.0
                and _speed_tmp_path is not None):
            _speed_path_str = _speed_tmp_path
            pts_factor = 1.0 / footage_speed
            # atempo 범위: 0.5~2.0; 범위 밖이면 체이닝
            if 0.5 <= footage_speed <= 2.0:
                af = f"atempo={footage_speed:.6f}"
            elif footage_speed > 2.0:
                af = f"atempo=2.0,atempo={footage_speed / 2.0:.6f}"
            else:
                af = f"atempo=0.5,atempo={footage_speed / 0.5:.6f}"
            _pa_spd = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(footage_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            _has_audio_spd = bool(_pa_spd.stdout.strip())
            # #303 — RenderSpec 매퍼 경유로 fit/speed/rotation을 *완전히 분리된* 단계로 처리.
            # 과거(#fit-bug, 2026-05-27): 이 블록은 fit과 setpts를 같은 표현식에 묶고,
            # 더 위쪽에선 "speed != 1.0"이면 fit을 contain으로 강제로 덮어쓰는 회귀가 있었다.
            # RenderSpec 빌더는 fit을 절대 픽셀(scale_x/crop/pad)로 풀고, 매퍼는 그 값을
            # ffmpeg 필터로 직역만 한다. setpts는 매퍼 안에서 *맨 끝 단일 토큰*으로만 붙으며
            # fit 계산과는 함수 호출이 분리되어 있어 다시는 묶일 수 없다.
            _spd_fit = "contain"
            if scene.footage and getattr(scene.footage, "type", None) == "clip":
                _spd_fit_raw = getattr(scene.footage, "fit", "contain") or "contain"
                _spd_fit = _spd_fit_raw if _spd_fit_raw in ("contain", "cover", "stretch") else "contain"
            from src.render_spec.models import Canvas as _Canvas, Size as _Size, VideoLayer as _VideoLayer
            from src.render_spec.builder import _compute_fit_geometry as _compute_fit, _apply_rotation_to_size as _rot_size
            from src.render_spec.ffmpeg_mapper import video_layer_to_vf as _vf_map
            # bg-preview-fix — speed 단계 canvas 에도 프로젝트 배경색 주입.
            # builder.py 경로와 동일하게 normalize_bg_color 결과(_bg_norm)를 그대로 넣어
            # letterbox pad 가 검정으로 구워지는 회귀를 막는다. black 이면 Canvas 기본값과 동일.
            _spd_canvas = _Canvas(width=_spd_canvas_wh[0], height=_spd_canvas_wh[1], fps=30, bg_color=_bg_norm)
            _spd_src_size = _ffprobe_video_size(str(footage_path)) or (1920, 1080)
            _spd_visible = _rot_size(_Size(width=_spd_src_size[0], height=_spd_src_size[1]), 0)
            _sx, _sy, _crop, _pad = _compute_fit(_spd_canvas, _spd_visible, _spd_fit)
            _spd_layer = _VideoLayer(
                source=str(footage_path),
                source_size=_spd_visible,
                scale_x=_sx, scale_y=_sy, crop=_crop, pad=_pad,
                rotation_deg=0,
                playback_rate=float(footage_speed),
                speed_mode=_spd_mode,
                speed_ramp_hold=_spd_hold,
                speed_ramp_accel=_spd_accel,
                speed_ramp_base=_spd_base,
                start_sec=0.0, end_sec=max(0.1, footage_dur or 1.0),
            )
            _speed_vf = _vf_map(_spd_layer, _spd_canvas)
            if _spd_mode in ("ramp", "ramp_down"):
                # ramp 오디오 — 계단식 atempo(영상 setpts 와 총 길이 일치)
                from src.render_spec.ffmpeg_mapper import video_layer_to_atempo as _af_map
                af = _af_map(_spd_layer)
            if _has_audio_spd:
                _r_spd = subprocess.run(
                    [ffmpeg_bin(), "-y", "-i", str(footage_path),
                     "-vf", _speed_vf,
                     "-af", af,
                     "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                     "-c:a", "aac",
                     _speed_path_str],
                    capture_output=True,
                )
            else:
                _r_spd = subprocess.run(
                    [ffmpeg_bin(), "-y", "-i", str(footage_path),
                     "-vf", _speed_vf,
                     "-an",
                     "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                     _speed_path_str],
                    capture_output=True,
                )
            if _r_spd.returncode == 0 and Path(_speed_path_str).exists() and Path(_speed_path_str).stat().st_size > 0:
                # #370 — 캐시에 저장(성공 시). 저장 실패해도 본 흐름은 계속.
                try:
                    _cached_speed = _stages.store(
                        _project_cache, scene_id, "speed", _speed_hash, _speed_path_str,
                    )
                    footage_path = _cached_speed
                    Path(_speed_path_str).unlink(missing_ok=True)
                except Exception as _ce_err:
                    print(f"[preview-cache] speed store 실패(무시): {_ce_err}", flush=True)
                    footage_path = Path(_speed_path_str)
                    _speed_tmp_ref.append(_speed_path_str)
                footage_dur = _ffprobe_duration(str(footage_path)) or footage_dur

        if is_image_footage:
            # 이미지(켄번스) — 기본은 TTS 길이로, footage_speed가 있으면 영상만 짧게(=TTS/속도).
            # TTS 오디오는 원본 속도 그대로 유지(사용자 의도: 영상만 빨리/느리게).
            tts_dur = _ffprobe_duration(str(tts_path)) if has_tts else None
            # TTS 가 없는 이미지 씬은 사용자가 지정한 `duration_hint_sec` 이 곧 화면 길이다.
            # (예전에는 5.0 이 하드코딩돼 있어 "씬 길이" 슬라이더를 2초로 줄여도 미리보기가
            #  계속 5초로 나왔다. 렌더 경로는 duration_hint_sec 을 쓰므로 미리보기만 어긋났다.)
            # duration_hint_sec 은 "씬 길이 하한"이다. TTS 가 있어도 hint 가 더 길면
            # 그만큼 화면이 유지된다(렌더 경로가 그렇게 동작한다). 미리보기가 TTS 길이만
            # 쓰면, 나레이션이 끝난 뒤에 뜨는 자막(예: 펀치라인)이 미리보기에서 잘려
            # "왜 안 나오냐"는 상태가 된다. 렌더와 같은 규칙으로 max 를 취한다.
            try:
                _hint = float(getattr(scene, "duration_hint_sec", 5.0) or 5.0)
            except (TypeError, ValueError):
                _hint = 5.0
            if _hint <= 0:
                _hint = 5.0
            if tts_dur and tts_dur > 0:
                base_dur = max(tts_dur, _hint)
            else:
                base_dur = _hint
            sp = footage_speed if (footage_speed and footage_speed > 0) else 1.0
            from src.utils.speed_ramp import output_duration as _spd_out_dur
            dur = _spd_out_dur(base_dur, _spd_mode, sp, _spd_hold, _spd_accel, base=_spd_base)
            fps = 30
            total_frames = max(1, int(round(dur * fps)))

            # kenburns 효과 — fit 모드 + 천천히 줌인(1.0 → 1.15).
            # fit: cover(가득 채움+crop, 기본) / contain(여백) / stretch(찌그러뜨림)
            _kb_fit_raw = getattr(scene.footage, "fit", None) or "cover"
            kb_fit = _kb_fit_raw if _kb_fit_raw in ("contain", "cover", "stretch") else "cover"

            # zoompan는 입력 프레임당 1프레임 처리 → loop 입력 + fps 지정으로 total_frames 만큼 생성.
            # 베이스 사이즈는 출력 크기 기준으로 fit 모드에 맞게 먼저 정규화한 뒤, zoompan으로 줌인.
            if kb_fit == "stretch":
                base_vf = f"scale={out_w}:{out_h}"
            elif kb_fit == "contain":
                base_vf = (
                    f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                    f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{_bg_pad_suffix}"
                )
            else:  # cover (기본)
                base_vf = (
                    f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                    f"crop={out_w}:{out_h}"
                )

            # zoompan: 매 프레임 1.0 → 1.15 선형 보간 줌인, 중앙 고정.
            # 베이스를 정규화한 뒤 4배 업스케일하여 zoompan의 정수 좌표 떨림(jitter) 완화.
            zp_z = f"1.0+0.15*on/{total_frames - 1 if total_frames > 1 else 1}"
            kb_vf = (
                f"{base_vf},"
                f"scale={out_w * 4}:{out_h * 4},"
                f"zoompan=z='{zp_z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":s={out_w}x{out_h}:fps={fps}"
            )

            cmd = [ffmpeg_bin(), "-y",
                   "-loop", "1", "-i", str(footage_path)]
            if has_tts and tts_dur and tts_dur > 0:
                # TTS 가 씬 길이보다 짧으면(duration_hint_sec 으로 씬을 늘린 경우) 오디오
                # 스트림이 TTS 끝에서 종료돼, 그 뒤에 배치된 SFX 가 통째로 잘린다.
                # apad 로 씬 길이만큼 무음을 채워 오디오 트랙 길이를 영상과 맞춘다.
                cmd += ["-i", str(tts_path),
                        "-af", f"apad=whole_dur={dur:.3f}",
                        "-c:a", "aac"]
            cmd += ["-t", f"{dur:.3f}",
                    "-vf", kb_vf,
                    "-r", str(fps),
                    "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    tmp_path_str]
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
                raise HTTPException(500, f"이미지 미리보기 생성 실패: {r.stderr.decode()[:200]}")
        elif not has_tts:
            # TTS 없음 → footage 복사. 단 원본 음량 설정은 반드시 적용한다.
            # (예전에는 그대로 복사만 해서 원본음량 0(음소거)이 무시됐다.)
            _copy_footage_with_volume(str(footage_path), tmp_path_str, scene)
        else:
            tts_dur = _ffprobe_duration(str(tts_path))
            if tts_dur is None or tts_dur <= 0:
                _copy_footage_with_volume(str(footage_path), tmp_path_str, scene)
                tts_dur = None  # 아래 가지 평가 회피
            # #270: dissolve를 포함한 필요 길이 (TTS + DISSOLVE × 1 또는 × 2)
            # 첫/끝 씬은 ×1, 중간 씬은 ×2. footage가 이 길이보다 짧으면 슬로모션으로 늘려야 한다.
            required_dur = (tts_dur + dissolve_pad) if tts_dur is not None else None

            # clip 효과 파라미터(fit/rotation/offset)를 미리 읽어 합성 단계의 scale 결정에 사용
            # 후속 needs_rescale 분기와의 중복 처리 회피를 위함.
            _clip_x_offset_pre = 0
            _clip_y_offset_pre = 0
            _clip_fit_pre = "contain"
            _clip_rotation_pre = 0
            if scene.footage and scene.footage.type == "clip":
                _x_ratio_pre = float(getattr(scene.footage, "x_offset", 0) or 0)
                _y_ratio_pre = float(getattr(scene.footage, "y_offset", 0) or 0)
                _clip_x_offset_pre = int(_x_ratio_pre * out_w)
                _clip_y_offset_pre = int(_y_ratio_pre * out_h)
                _fit_raw_pre = getattr(scene.footage, "fit", "contain") or "contain"
                _clip_fit_pre = _fit_raw_pre if _fit_raw_pre in ("contain", "cover", "stretch") else "contain"
                try:
                    _rot_raw_pre = int(getattr(scene.footage, "rotation", 0) or 0)
                except (TypeError, ValueError):
                    _rot_raw_pre = 0
                _clip_rotation_pre = _rot_raw_pre if _rot_raw_pre in (0, 90, 180, 270) else 0

            if tts_dur is None:
                pass  # 위에서 복사 처리됨
            elif required_dur <= footage_dur:
                # TTS가 footage보다 짧거나 같음 → footage 원본 길이 유지 + TTS 합성 (#268)
                # 영상은 footage 길이 그대로, TTS는 끝까지 재생되고 이후 구간은
                # footage 원본 오디오(있으면) 또는 무음. duration=first(=footage)로 길이 고정.
                # 비디오 재인코딩 불요 → stream copy로 빠르게 처리.
                _pa = subprocess.run(
                    [ffprobe_bin(), "-v", "error", "-select_streams", "a",
                     "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(footage_path)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                footage_has_audio = bool(_pa.stdout.strip())
                # 다운스케일(#254)을 위해 비디오는 항상 재인코딩.
                # 단, fit이 cover/stretch이거나 rotation/offset이 있으면 후속 needs_rescale
                # 분기가 정확한 vf chain으로 재처리하므로 여기선 단순 scale로만 다운스케일.
                # 그렇지 않으면(기본 contain) 여기서 letterbox/pillarbox까지 한 번에 처리.
                _needs_post_rescale = (
                    _clip_fit_pre != "contain"
                    or _clip_x_offset_pre != 0 or _clip_y_offset_pre != 0
                    or _clip_rotation_pre != 0
                )
                if _needs_post_rescale:
                    # 짝수 보장만 하고 종횡비 처리는 후속 분기에 위임.
                    vf_scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
                else:
                    vf_scale = f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{_bg_pad_suffix},setsar=1"
                if footage_has_audio:
                    # footage 원본 오디오(씬별 음량) + TTS 1.0 amix. 0=음소거, 0.3=기본, 1.0=풀.
                    # normalize=0: amix 자동 감쇠 끔. 명시한 volume 값 그대로 유지.
                    _fav = getattr(scene, "footage_audio_volume", 0.3)
                    audio_filter = (
                        f"[0:a]volume={_fav}[orig];"
                        f"[1:a]volume=1.0,apad[tts];"
                        f"[orig][tts]amix=inputs=2:normalize=0:duration=first[aout]"
                    )
                    r = subprocess.run(
                        [ffmpeg_bin(), "-y",
                         "-i", str(footage_path),
                         "-i", str(tts_path),
                         "-filter_complex", audio_filter,
                         "-map", "0:v:0", "-map", "[aout]",
                         "-vf", vf_scale,
                         "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                         "-c:a", "aac",
                         "-movflags", "+faststart",
                         tmp_path_str],
                        capture_output=True,
                    )
                else:
                    # footage 무음 → TTS를 footage 길이까지 apad로 패딩하여 단일 트랙 사용
                    audio_filter = f"[1:a]apad=whole_dur={footage_dur:.3f}[aout]"
                    r = subprocess.run(
                        [ffmpeg_bin(), "-y",
                         "-i", str(footage_path),
                         "-i", str(tts_path),
                         "-filter_complex", audio_filter,
                         "-map", "0:v:0", "-map", "[aout]",
                         "-vf", vf_scale,
                         "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                         "-c:a", "aac",
                         "-movflags", "+faststart",
                         tmp_path_str],
                        capture_output=True,
                    )
                if r.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
                    raise HTTPException(500, f"미리보기 생성에 실패했습니다: {r.stderr.decode()[:200]}")
            else:
                # required_dur(=TTS+dissolve_pad)가 footage보다 긺 → footage 늘리기 (#270)
                # 최종 렌더링과 같이 양쪽(또는 한쪽) dissolve 여유를 포함한 길이로 늘림.
                speed_ratio = footage_dur / required_dur  # < 1.0

                # Step 1: footage 길이 조정
                adjusted_fd, adjusted_path_str = tempfile.mkstemp(suffix=".mp4", prefix="vag_adj_")
                _os.close(adjusted_fd)

                if speed_ratio >= 0.5:
                    # Time Warp: setpts=PTS/ratio (N>1 → 슬로우모션)
                    pts_factor = 1.0 / speed_ratio  # > 1.0
                    r = subprocess.run(
                        [ffmpeg_bin(), "-y",
                         "-i", str(footage_path),
                         "-vf", f"setpts={pts_factor:.6f}*PTS",
                         "-an",
                         "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                         adjusted_path_str],
                        capture_output=True,
                    )
                else:
                    # 마지막 프레임 프리즈로 부족분 채움
                    extra = required_dur - footage_dur
                    r = subprocess.run(
                        [ffmpeg_bin(), "-y",
                         "-i", str(footage_path),
                         "-vf", f"tpad=stop_mode=clone:stop_duration={extra:.3f}",
                         "-an",
                         "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                         adjusted_path_str],
                        capture_output=True,
                    )

                try:
                    if r.returncode != 0 or not Path(adjusted_path_str).exists() or Path(adjusted_path_str).stat().st_size == 0:
                        raise HTTPException(500, "footage 길이 조정에 실패했습니다")

                    # Step 2: 조정된 footage(오디오 없음) + footage 원본 오디오 + TTS 합성
                    # adjusted는 -an으로 오디오가 제거된 상태이므로
                    # footage 원본 오디오가 있으면 footage/TTS 세 입력으로 amix
                    _pa2 = subprocess.run(
                        [ffprobe_bin(), "-v", "error", "-select_streams", "a",
                         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(footage_path)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                    )
                    if bool(_pa2.stdout.strip()):
                        # 0: adjusted(video only), 1: footage(audio), 2: tts
                        # normalize=0 + footage 볼륨 명시. TTS는 풀 1.0.
                        _fav = getattr(scene, "footage_audio_volume", 0.3)
                        r2 = subprocess.run(
                            [ffmpeg_bin(), "-y",
                             "-i", adjusted_path_str,
                             "-i", str(footage_path),
                             "-i", str(tts_path),
                             "-filter_complex",
                             f"[1:a]volume={_fav}[orig];"
                             f"[orig][2:a]amix=inputs=2:normalize=0:duration=first[aout]",
                             "-map", "0:v:0", "-map", "[aout]",
                             "-c:v", "copy", "-c:a", "aac", "-shortest",
                             tmp_path_str],
                            capture_output=True,
                        )
                    else:
                        r2 = subprocess.run(
                            [ffmpeg_bin(), "-y",
                             "-i", adjusted_path_str,
                             "-i", str(tts_path),
                             "-map", "0:v:0", "-map", "1:a:0",
                             "-c:v", "copy", "-c:a", "aac", "-shortest",
                             tmp_path_str],
                            capture_output=True,
                        )
                    if r2.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
                        raise HTTPException(500, f"오디오 합성에 실패했습니다: {r2.stderr.decode()[:200]}")
                finally:
                    Path(adjusted_path_str).unlink(missing_ok=True)

        # clip 효과의 x_offset/y_offset/fit/rotation 읽기 (#258, #263)
        _clip_x_offset = 0
        _clip_y_offset = 0
        _clip_fit = "contain"
        _clip_rotation = 0
        if scene.footage and scene.footage.type == "clip":
            _x_ratio = float(getattr(scene.footage, "x_offset", 0) or 0)
            _y_ratio = float(getattr(scene.footage, "y_offset", 0) or 0)
            _clip_x_offset = int(_x_ratio * out_w)
            _clip_y_offset = int(_y_ratio * out_h)
            _fit_raw = getattr(scene.footage, "fit", "contain") or "contain"
            _clip_fit = _fit_raw if _fit_raw in ("contain", "cover", "stretch") else "contain"
            # #263 rotation (0/90/180/270). 잘못된 값은 0으로 폴백.
            try:
                _rot_raw = int(getattr(scene.footage, "rotation", 0) or 0)
            except (TypeError, ValueError):
                _rot_raw = 0
            _clip_rotation = _rot_raw if _rot_raw in (0, 90, 180, 270) else 0

        # 출력 해상도로 scale/crop — footage 원본 해상도와 출력 해상도가 다를 때
        # #258 fit 모드별 분기: contain(기본/패딩), cover(영역 채움+crop), stretch(비율 무시).
        # #263 rotation이 0이 아니면 회전 적용이 필요하므로 항상 재처리.
        vid_size = _ffprobe_video_size(tmp_path_str)
        needs_rescale = (
            (vid_size and (vid_size[0] != out_w or vid_size[1] != out_h))
            or (_clip_x_offset != 0 or _clip_y_offset != 0)
            or _clip_fit != "contain"  # cover/stretch는 항상 재처리
            or _clip_rotation != 0  # #263 회전은 항상 재처리
        )
        if needs_rescale:
            import tempfile as _tempfile_scale
            sc_fd, sc_path_str = _tempfile_scale.mkstemp(suffix=".mp4", prefix="vag_scale_")
            import os as _os_sc
            _os_sc.close(sc_fd)

            # #303 — RenderSpec 매퍼 경유로 fit/rotation을 절대 픽셀로 직역.
            # offset == 0이면 매퍼 경로(추상 키워드 없음, 분기 결합 불가능). offset != 0은
            # builder가 아직 offset을 지원하지 않으므로 레거시 표현식 기반 경로를 유지한다.
            if _clip_x_offset == 0 and _clip_y_offset == 0:
                from src.render_spec.models import (
                    Canvas as _RsCanvas, Size as _RsSize, VideoLayer as _RsVideoLayer,
                )
                from src.render_spec.builder import (
                    _compute_fit_geometry as _rs_fit,
                    _apply_rotation_to_size as _rs_rot,
                )
                from src.render_spec.ffmpeg_mapper import video_layer_to_vf as _rs_vf
                # bg-preview-fix — scale/fit 단계 canvas 에도 배경색 주입(contain letterbox pad).
                # black 이면 Canvas 기본값과 동일. 이 단계는 in-place(os.replace)라 별도 캐시 키 없음.
                _rs_canvas = _RsCanvas(width=out_w, height=out_h, fps=30, bg_color=_bg_norm)
                _rs_src_size = _ffprobe_video_size(tmp_path_str) or (1920, 1080)
                _rs_visible = _rs_rot(
                    _RsSize(width=_rs_src_size[0], height=_rs_src_size[1]),
                    _clip_rotation,
                )
                _rs_sx, _rs_sy, _rs_crop, _rs_pad = _rs_fit(_rs_canvas, _rs_visible, _clip_fit)
                _rs_layer = _RsVideoLayer(
                    source=tmp_path_str,
                    source_size=_rs_visible,
                    scale_x=_rs_sx, scale_y=_rs_sy,
                    crop=_rs_crop, pad=_rs_pad,
                    rotation_deg=_clip_rotation,
                    playback_rate=1.0,  # 속도는 이전 단계에서 이미 적용됨
                    start_sec=0.0, end_sec=1.0,
                )
                vf_scale = _rs_vf(_rs_layer, _rs_canvas)
            else:
                # ── 레거시 경로 (offset != 0) ─────────────────────────────
                # rotation prefix
                if _clip_rotation == 90:
                    _rotate_prefix = "transpose=1,"
                elif _clip_rotation == 180:
                    _rotate_prefix = "hflip,vflip,"
                elif _clip_rotation == 270:
                    _rotate_prefix = "transpose=2,"
                else:
                    _rotate_prefix = ""

                if _clip_fit == "stretch":
                    vf_scale = f"{_rotate_prefix}scale={out_w}:{out_h},setsar=1"
                elif _clip_fit == "cover":
                    _crop_x_expr = f"(iw-{out_w})/2+{_clip_x_offset}"
                    _crop_y_expr = f"(ih-{out_h})/2+{_clip_y_offset}"
                    vf_scale = (
                        f"{_rotate_prefix}scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                        f"crop={out_w}:{out_h}:{_crop_x_expr}:{_crop_y_expr},setsar=1"
                    )
                else:
                    _abs_x = abs(_clip_x_offset)
                    _abs_y = abs(_clip_y_offset)
                    _pad_w = out_w + 2 * _abs_x
                    _pad_h = out_h + 2 * _abs_y
                    _pad_x = f"({_pad_w}-iw)/2+{_clip_x_offset}"
                    _pad_y = f"({_pad_h}-ih)/2+{_clip_y_offset}"
                    _crop_x = _abs_x
                    _crop_y = _abs_y
                    vf_scale = (
                        f"{_rotate_prefix}scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                        f"pad={_pad_w}:{_pad_h}:{_pad_x}:{_pad_y}:{_bg_norm},"
                        f"crop={out_w}:{out_h}:{_crop_x}:{_crop_y}"
                    )

            r_sc = subprocess.run(
                [ffmpeg_bin(), "-y", "-i", tmp_path_str,
                 "-vf", vf_scale,
                 "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                 "-c:a", "copy", sc_path_str],
                capture_output=True,
            )
            if r_sc.returncode == 0 and Path(sc_path_str).stat().st_size > 0:
                import os as _os_sc2
                _os_sc2.replace(sc_path_str, tmp_path_str)
            else:
                Path(sc_path_str).unlink(missing_ok=True)

        # 효과 적용 (#322) — 카테고리별 if 분기 6개를 레지스트리 조회 단일 루프로 통합.
        #
        # 통합 전(레퍼런스): 6개 카테고리(filter/motion/mosaic/text/shape/graphic) 각각이
        # ``apply_*`` / ``_apply_*_overlays_preview`` 를 직접 호출하는 if 블록이었다. 신규
        # 효과/카테고리 추가 시 본 라우터를 직접 수정해야 했다.
        #
        # 통합 후: ``PY_EFFECT_HANDLERS_PREVIEW`` 에 카테고리별로 등록된 핸들러를 ``_PREVIEW_CATEGORIES``
        # 메타정보(데이터 추출 방식·임시파일 prefix·추가 인자)로 순회 호출한다.
        # 동작 자체는 *기존 함수 호출 시그니처와 1:1로 같다* — 회귀가 발생하지 않도록
        # 카테고리별 메타에서 차이를 모두 흡수한다.
        #
        # 적용 순서는 메인 흐름(src/clip_editor.py)과 동일: filters → motions → mosaic
        # → text → shape → graphic. 미리보기와 최종 출력의 합성 결과가 동일해야 하므로
        # 이 순서를 임의로 바꾸지 않는다.
        from src.render_spec.effects._handlers import get_preview_handler

        # 카테고리별 메타: scene 의 효과 리스트 속성명, 임시파일 prefix, 추가 kwargs.
        # ``replace_always`` 는 text/shape 처럼 *함수 내부에서 실패시 폴백 복사* 를 보장하는
        # 카테고리에 True (즉 핸들러 반환이 True 이면 dst 가 유효하다고 가정). 그 외는 dst
        # 파일 존재 + 0바이트 아님 검사를 추가로 수행한다 (기존 filter/motion/mosaic/graphic 동작).
        # 적용 순서는 clip_editor 메인 흐름과 동일하게 유지:
        # filter → motion → mosaic → image → text → shape → graphic.
        _PREVIEW_CATEGORIES: list[tuple[str, str, str, dict, bool]] = [
            # (category, scene_attr, tmpfile_prefix, extra_kwargs, replace_always)
            ("filter", "filters", "vag_filter_", {}, False),
            ("motion", "motions", "vag_motion_", {}, False),
            ("mosaic", "mosaic_overlays", "vag_mosaic_", {}, False),
            # 이미지 오버레이는 미리 image_asset_id → 절대경로로 해석한 dict 를 핸들러에 전달.
            ("image", "image_overlays", "vag_image_", {}, False),
            ("text", "text_overlays", "vag_overlay_", {"output_height": raw_h}, True),
            ("shape", "shape_overlays", "vag_shape_", {}, True),
            ("graphic", "graphic_overlays", "vag_graphic_", {}, False),
            # 집중선(속도선) — 모든 오버레이 중 최상단. clip_editor 메인 흐름과 동일 순서.
            ("speed_lines", "speed_lines", "vag_speed_lines_", {}, False),
        ]

        # 이미지 오버레이 미리보기를 위한 asset_id → 절대경로 lookup.
        # studio_assets 중 asset_type="image" 만 후보. footage_dir 부재 시 빈 dict.
        _img_asset_path_by_id: dict[str, str] = {}
        try:
            _p_for_imgs = store.get_project(project_id)
            _footage_root = Path(_p_for_imgs.footage_dir) if (_p_for_imgs and _p_for_imgs.footage_dir) else None
            if _footage_root and _footage_root.exists() and _p_for_imgs is not None:
                for _a_img in (_p_for_imgs.studio_assets or []):
                    if getattr(_a_img, "asset_type", "") == "image":
                        _abs_img = _footage_root / _a_img.filename
                        if _abs_img.exists():
                            _img_asset_path_by_id[_a_img.asset_id] = str(_abs_img)
        except Exception as _img_lookup_err:
            print(f"[preview] 이미지 자산 lookup 실패(무시): {_img_lookup_err}", flush=True)

        import os as _os_eff
        import tempfile as _tempfile_eff
        import traceback as _tb_eff

        for category, scene_attr, prefix, extra_kwargs, replace_always in _PREVIEW_CATEGORIES:
            scene_items = getattr(scene, scene_attr, None) or []
            if not scene_items:
                continue
            data_list = [item.model_dump() for item in scene_items]
            # image 카테고리 한정: image_asset_id 를 절대 경로로 해석해 `_image_path` 주입.
            # 해석 실패(자산 없음)는 해당 overlay 만 스킵 — 다른 효과들은 정상 적용됨.
            if category == "image":
                resolved: list[dict] = []
                for _d in data_list:
                    _aid = _d.get("image_asset_id")
                    _path = _img_asset_path_by_id.get(_aid) if _aid else None
                    if _path:
                        _d["_image_path"] = _path
                        resolved.append(_d)
                data_list = resolved
                if not data_list:
                    # 모두 미해석이면 해당 카테고리 건너뜀.
                    continue
            handler = get_preview_handler(category)
            if handler is None:
                # 매니페스트 로딩에서 핸들러가 등록되지 않았다면 회귀 위험을 막기 위해 스킵.
                print(f"[preview] '{category}' 핸들러 미등록 — 스킵", flush=True)
                continue

            eff_fd, eff_path_str = _tempfile_eff.mkstemp(suffix=".mp4", prefix=prefix)
            _os_eff.close(eff_fd)
            try:
                ok = handler(
                    tmp_path_str, eff_path_str,
                    data_list,
                    out_w, out_h,
                    **extra_kwargs,
                )
                # 미리보기는 TTS 를 이미 섞은 뒤에 모션을 적용한다. slowmo(정지 포함) 모션은
                # 영상 길이가 바뀐다며 오디오 트랙을 버리므로(-an) TTS 까지 사라져 무음이 됐다
                # (챕터2 6번 씬, 2026-11-25). 모션 결과에 오디오가 없으면 직전 오디오를 다시
                # 붙이고 영상 길이까지 무음으로 채운다. 최종 렌더는 TTS 를 따로 얹어 영향 없음.
                if (category == "motion" and ok and Path(eff_path_str).exists()
                        and not _probe_has_audio(eff_path_str) and _probe_has_audio(tmp_path_str)):
                    _ra_fd, _ra_path = _tempfile_eff.mkstemp(suffix=".mp4", prefix="vag_motion_a_")
                    _os_eff.close(_ra_fd)
                    _ra = subprocess.run(
                        [ffmpeg_bin(), "-y", "-i", eff_path_str, "-i", tmp_path_str,
                         "-map", "0:v:0", "-map", "1:a:0", "-af", "apad",
                         "-c:v", "copy", "-c:a", "aac", "-shortest", _ra_path],
                        capture_output=True,
                    )
                    if _ra.returncode == 0 and Path(_ra_path).stat().st_size > 0:
                        _os_eff.replace(_ra_path, eff_path_str)
                    else:
                        Path(_ra_path).unlink(missing_ok=True)
                if replace_always:
                    # text/shape: 함수 자체가 실패 시 src→dst 복사를 보장하므로 그대로 replace.
                    _os_eff.replace(eff_path_str, tmp_path_str)
                elif ok and Path(eff_path_str).exists() and Path(eff_path_str).stat().st_size > 0:
                    _os_eff.replace(eff_path_str, tmp_path_str)
                else:
                    Path(eff_path_str).unlink(missing_ok=True)
            except Exception as _eff_e:
                print(f"[preview] '{category}' 적용 실패: {_eff_e}\n{_tb_eff.format_exc()}", flush=True)
                Path(eff_path_str).unlink(missing_ok=True)

        # preview-final-cache — 완성된 미리보기를 최종 캐시에 보관.
        #
        # store() 는 복사(copy→rename)이므로 tmp_path 는 그대로 응답에 쓸 수 있고,
        # 이어지는 _cleanup 이 tmp 를 지워도 캐시 사본은 남는다.
        # 저장 실패는 본 흐름과 무관 — 다음 요청이 그냥 다시 만들 뿐이라 침묵 폴백.
        _final_stored: Path | None = None
        if _final_hash and tmp_path.exists() and tmp_path.stat().st_size > 0:
            try:
                _final_stored = _stages.store(
                    _project_cache_dir, scene_id, "final", _final_hash, tmp_path_str,
                )
            except Exception as _fs_err:
                print(f"[preview-cache] final store 실패(무시): {_fs_err}", flush=True)
            _stage_results.append(
                _stages.StageCacheResult(stage="final", key_hash=_final_hash, cached=None)
            )

        background_tasks.add_task(_cleanup)

        # #370 — 단계 캐시 효용 로그 + LRU 정리. 로그는 hit/miss 한 줄.
        # LRU 는 응답 본흐름과 분리해 BackgroundTask 로 미룬다(파일 시스템 스캔이 길어질 수 있음).
        if _stage_results:
            try:
                print(_stages.log_line(scene_id, _stage_results), flush=True)
            except Exception:
                pass
        try:
            _project_cache_for_lru = store.get_cache_dir(project_id)
            background_tasks.add_task(_stages.enforce_lru, _project_cache_for_lru)
        except Exception as _lru_err:
            print(f"[preview-cache] LRU 스케줄링 실패(무시): {_lru_err}", flush=True)

        # #301 — 모의 비용 기록: 인터랙티브 미리보기 1회 (캐시 미스 강제)
        try:
            from ..cost_tracker import measure_phase
            _elapsed_prev = _t_prev_render.monotonic() - _preview_t0
            _prev_bytes = Path(tmp_path_str).stat().st_size if Path(tmp_path_str).exists() else 0
            with measure_phase(project_id) as _est:
                _est.record_scene_preview(
                    cpu_seconds=_elapsed_prev,
                    output_bytes=_prev_bytes,
                    cache_hit=False,
                )
        except Exception as _cost_err:
            print(f"[cost] scene_preview_render 기록 실패: {_cost_err}")

        return FileResponse(
            tmp_path_str,
            media_type="video/mp4",
            filename=f"preview_scene_{scene_id:03d}.mp4",
        )
    except HTTPException:
        _cleanup()
        raise
    except Exception as e:
        _cleanup()
        raise HTTPException(500, f"미리보기 생성 오류: {e}") from e


def _apply_text_overlays_preview(
    src_path: str,
    dst_path: str,
    overlays: list[dict],
    video_width: int,
    video_height: int,
    output_height: int | None = None,
) -> None:
    """텍스트 오버레이를 Pillow PNG → ffmpeg overlay 방식으로 적용한다.

    drawtext 필터 없이도 동작하도록 모든 오버레이를 Pillow로 렌더링한다.
    실패 시 src를 dst로 복사한다.

    font_size, glow_radius, outline_width 등 픽셀 단위 값은 사용자가 출력 해상도
    기준으로 설정한 것이다. 미리보기가 다운스케일된 경우 그 비율로 줄여 렌더해야
    최종 렌더(원본 해상도)와 시각적으로 동일해진다.
    """
    import shutil as _sh
    import tempfile

    from src.clip_editor import _render_overlay_png

    # 카운트다운 오버레이는 숫자마다 "텍스트가 다른 1초짜리 오버레이" 로 펼친다.
    # 미리보기는 오버레이당 PNG 1장 + start/end 구간 합성 구조라, 이렇게 펼치면
    # 최종 렌더(drawtext 를 숫자마다 나누는 방식)와 같은 결과가 나온다.
    def _expand_countdown(ov: dict) -> list[dict]:
        cd_from = int(ov.get("countdown_from", 0) or 0)
        if cd_from <= 0:
            return [ov]
        cd_to = int(ov.get("countdown_to", 1) or 0)
        if cd_to > cd_from:
            cd_from, cd_to = cd_to, cd_from
        try:
            interval = float(ov.get("countdown_interval_sec", 1.0) or 1.0)
        except (TypeError, ValueError):
            interval = 1.0
        interval = max(0.05, min(60.0, interval))
        base_start = float(ov.get("start_sec", 0.0) or 0.0)
        end_raw = ov.get("end_sec")
        out: list[dict] = []
        numbers = list(range(cd_from, cd_to - 1, -1))[:120]
        for i, num in enumerate(numbers):
            seg_start = base_start + i * interval
            seg_end = seg_start + interval
            if end_raw is not None:
                seg_end = min(seg_end, float(end_raw))
            if seg_end <= seg_start:
                continue
            step = dict(ov)
            step["text"] = str(num)
            step["start_sec"] = seg_start
            step["end_sec"] = seg_end
            # 숫자마다 등장/사라짐 페이드가 겹치면 깜빡이므로 진입/종료 효과는 끈다.
            step["enter_effect"] = "none"
            step["exit_effect"] = "none"
            step["countdown_from"] = 0  # 재귀 방지
            out.append(step)
        end_text = str(ov.get("countdown_end_text", "") or "")
        if end_text:
            fin_start = base_start + len(numbers) * interval
            fin_end = float(end_raw) if end_raw is not None else None
            if fin_end is None or fin_end > fin_start:
                fin = dict(ov)
                fin["text"] = end_text
                fin["start_sec"] = fin_start
                fin["end_sec"] = fin_end
                fin["countdown_from"] = 0
                out.append(fin)
        return out

    expanded: list[dict] = []
    for _ov in overlays:
        expanded.extend(_expand_countdown(_ov))
    active_overlays = [ov for ov in expanded if ov.get("text")]
    if not active_overlays:
        _sh.copy2(src_path, dst_path)
        return

    tmp_dir = tempfile.mkdtemp(prefix="vag_prev_overlay_")
    try:
        current_src = src_path

        # 실제 입력 영상 크기를 ffprobe로 확인 (scale 결과가 홀수→짝수 반올림될 수 있음)
        actual_size = _ffprobe_video_size(src_path)
        if actual_size:
            video_width, video_height = actual_size

        # 미리보기 다운스케일 비율 — font_size 등 픽셀 값에 곱해 시각적 일관성 확보
        # output_height가 주어지지 않으면 video_height와 같다고 가정(비율 1.0)
        scale_ratio = float(video_height) / float(output_height) if output_height and output_height > 0 else 1.0

        # 모든 오버레이 PNG를 먼저 생성
        # #287 — 원본 ov dict도 함께 저장하여 프레임별 fade 알파 계산.
        overlay_layers: list[tuple] = []  # (png_img, start_sec, end_sec, ov)
        for idx, ov in enumerate(active_overlays):
            # 픽셀 단위 값들을 미리보기 비율로 스케일한 사본을 만든다
            ov_scaled = dict(ov)
            if scale_ratio != 1.0:
                if "font_size" in ov_scaled:
                    ov_scaled["font_size"] = max(1, int(round(int(ov_scaled["font_size"]) * scale_ratio)))
                if "glow_radius" in ov_scaled and ov_scaled["glow_radius"]:
                    ov_scaled["glow_radius"] = max(0, int(round(int(ov_scaled["glow_radius"]) * scale_ratio)))
                if "outline_width" in ov_scaled and ov_scaled["outline_width"]:
                    ov_scaled["outline_width"] = max(0, int(round(int(ov_scaled["outline_width"]) * scale_ratio)))
            png_path = _render_overlay_png(ov_scaled, video_width, video_height, tmp_dir, idx)
            if not png_path:
                continue
            try:
                from PIL import Image as _PILImage
                png_img = _PILImage.open(png_path).convert("RGBA")
            except Exception:
                continue
            # 속도 최적화 — 캔버스 크기 PNG 대신 글자가 있는 영역(알파 bbox)만 보관·합성한다.
            # bbox 밖은 알파 0 이라 합성 결과는 같다.
            _bbox = png_img.getchannel("A").getbbox()
            if _bbox is None:
                continue
            _crop = png_img.crop(_bbox)
            start_sec = float(ov.get("start_sec", 0.0))
            end_sec = ov.get("end_sec")
            overlay_layers.append((_crop, (_bbox[0], _bbox[1]), start_sec, end_sec, ov))

        if not overlay_layers:
            _sh.copy2(current_src, dst_path)
        else:
            # ffmpeg pipe: raw RGB24 프레임 → Pillow alpha_composite → ffmpeg 재인코딩
            from PIL import Image as _PILImage
            import struct

            # 영상 정보
            fps_r = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate,nb_frames",
                 "-of", "csv=p=0", current_src],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            fps_str, nb_frames_str = (fps_r.stdout.strip().split(",") + ["", ""])[:2]
            fps = 30.0
            if "/" in fps_str:
                n, d = fps_str.split("/")
                fps = float(n) / max(float(d), 1)
            elif fps_str:
                fps = float(fps_str)

            audio_check = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", current_src],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            has_audio = bool(audio_check.stdout.strip())

            overlay_out = str(Path(tmp_dir) / "pillow_overlay.mp4")

            # decode: 비디오 스트림만 raw RGB24로 추출 (-an으로 오디오 제외)
            dec = subprocess.Popen(
                [ffmpeg_bin(), "-i", current_src,
                 "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            # 오디오는 별도 추출
            audio_tmp = str(Path(tmp_dir) / "audio_tmp.aac")
            if has_audio:
                subprocess.run(
                    [ffmpeg_bin(), "-y", "-i", current_src, "-vn", "-c:a", "copy", audio_tmp],
                    capture_output=True,
                )
            # encode ← pipe (비디오만) + 오디오 mux
            enc_cmd = [
                ffmpeg_bin(), "-y",
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", f"{video_width}x{video_height}",
                "-r", str(fps),
                "-i", "pipe:0",
            ]
            if has_audio and Path(audio_tmp).exists() and Path(audio_tmp).stat().st_size > 0:
                enc_cmd += ["-i", audio_tmp, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
            else:
                enc_cmd += ["-an"]
            enc_cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast",
                        "-pix_fmt", "yuv420p", overlay_out]
            enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

            # #287 — 클립 길이로 end_sec=None인 fade 종료점 결정
            from src.clip_editor import get_duration as _get_duration, _compute_overlay_alpha_scalar as _alpha_scalar
            try:
                clip_dur = _get_duration(current_src)
            except Exception:
                clip_dur = None

            frame_size = video_width * video_height * 3
            frame_idx = 0
            try:
                import math as _math
                # 미리보기 텍스트 오버레이 페이드 인/아웃 — 최종 렌더와 동일 동작.
                FADE_BASE_PREV = 0.3

                def _with_alpha(img, a):
                    if a >= 0.999:
                        return img
                    r_, g_, b_, a_ = img.split()
                    return _PILImage.merge("RGBA", (r_, g_, b_, a_.point(lambda v, m=a: int(v * m))))

                def _composite_region(base_rgb, piece, x, y):
                    """불투명 RGB 프레임의 (x, y) 영역에만 alpha_composite (화면 밖은 잘라냄)."""
                    pw, ph = piece.size
                    x0, y0 = max(0, x), max(0, y)
                    x1, y1 = min(video_width, x + pw), min(video_height, y + ph)
                    if x1 <= x0 or y1 <= y0:
                        return
                    if (x0, y0, x1, y1) != (x, y, x + pw, y + ph):
                        piece = piece.crop((x0 - x, y0 - y, x1 - x, y1 - y))
                    box = (x0, y0, x1, y1)
                    region = base_rgb.crop(box).convert("RGBA")
                    region.alpha_composite(piece)
                    base_rgb.paste(region.convert("RGB"), box)

                while True:
                    raw = dec.stdout.read(frame_size)
                    if len(raw) < frame_size:
                        break
                    t = frame_idx / fps
                    _active = [
                        lyr for lyr in overlay_layers
                        if t >= lyr[2] and (lyr[3] is None or t <= lyr[3])
                    ]
                    if not _active:
                        # 이 프레임에 보이는 텍스트가 없음 — 합성 없이 그대로 넘긴다.
                        enc.stdin.write(raw)
                        frame_idx += 1
                        continue
                    base = _PILImage.frombytes("RGB", (video_width, video_height), raw)
                    for (png_img, (cx0, cy0), start_sec, end_sec, ov_data) in _active:
                        # #287 — 텍스트 오버레이는 fade만 적용 (typing/zoom→fade 폴백, 복잡 텍스트 한정).
                        alpha_scalar = _alpha_scalar(
                            ov_data, t,
                            visual_clip_duration=clip_dur,
                            text_supported=True,
                        )

                        # 표시 중 반복 애니메이션 — bounce/shake/wave 는 (ox, oy) 오프셋,
                        # pulse 는 PNG 자체 resize. 최종 렌더(ffmpeg) 표현식과 동일한 의미·진폭·주기.
                        loop_anim = str(ov_data.get("loop_animation") or "none").lower()
                        loop_amp = float(ov_data.get("loop_amplitude") or 0.02)
                        loop_period = max(0.1, float(ov_data.get("loop_period_sec") or 0.8))
                        ox = 0
                        oy = 0
                        layer_img = png_img
                        # 관통(pass_*) — 한쪽 화면 밖에서 들어와 반대편으로 빠져나간다.
                        # 최종 렌더는 drawtext x/y 표현식으로 처리하지만 미리보기는 PNG 합성이라
                        # 프레임마다 오프셋을 직접 계산해 줘야 한다. 이게 없으면 미리보기에서만
                        # 텍스트가 제자리에 붙어 있어 "관통이 안 된다"고 보인다.
                        _enter_eff = str(ov_data.get("enter_effect") or "none")
                        _exit_eff = str(ov_data.get("exit_effect") or "none")
                        if _enter_eff.startswith("pass_") or _exit_eff.startswith("pass_"):
                            from src.clip_editor import _compute_image_overlay_transform as _cot
                            _dx, _dy, _sc, _al = _cot(
                                ov_data, t, video_width, video_height,
                                visual_clip_duration=clip_dur, text_supported=True,
                            )
                            ox += int(round(_dx))
                            oy += int(round(_dy))
                        if loop_anim in ("bounce", "shake", "pulse", "wave") and t >= start_sec:
                            rel_t = t - start_sec
                            # 루프 애니메이션 오프셋은 관통(pass_*) 오프셋 "위에" 더한다.
                            # 대입(=)을 쓰면 위에서 계산한 관통 오프셋이 지워져, 관통+루프를
                            # 함께 지정한 오버레이가 미리보기에서 제자리에 붙어 버린다.
                            if loop_anim == "bounce":
                                oy += int(-abs(_math.sin(rel_t * 2 * _math.pi / loop_period)) * (video_height * loop_amp))
                            elif loop_anim == "shake":
                                fs_for_shake = int(ov_data.get("font_size") or 40)
                                ox += int(_math.sin(rel_t * 2 * _math.pi * 12) * (fs_for_shake * loop_amp))
                            elif loop_anim == "wave":
                                ox += int(_math.sin(rel_t * 2 * _math.pi / loop_period) * (video_width * loop_amp))
                            elif loop_anim == "pulse":
                                # 캔버스 크기 PNG 를 (1 + amp*sin) 비율로 리사이즈해 중앙 정렬하던 것과 같은
                                # 결과를, 잘린 조각(crop) 기준으로 계산한다(위치·크기를 같은 비율로 변환).
                                factor = 1.0 + loop_amp * _math.sin(rel_t * 2 * _math.pi / loop_period)
                                if abs(factor - 1.0) > 0.001:
                                    new_w = max(1, int(video_width * factor))
                                    new_h = max(1, int(video_height * factor))
                                    sx, sy = new_w / video_width, new_h / video_height
                                    layer_img = png_img.resize(
                                        (max(1, int(round(png_img.width * sx))), max(1, int(round(png_img.height * sy)))),
                                        _PILImage.LANCZOS,
                                    )
                                    ox += (video_width - new_w) // 2 + int(round(cx0 * sx)) - cx0
                                    oy += (video_height - new_h) // 2 + int(round(cy0 * sy)) - cy0

                        if alpha_scalar <= 0.0:
                            continue
                        # 합성. 오프셋이 없고 원본 조각이면 alpha_composite(기존 fast path),
                        # 아니면 기존처럼 mask paste 로 위치 오프셋과 함께 합성한다.
                        if ox == 0 and oy == 0 and layer_img is png_img:
                            _composite_region(base, _with_alpha(png_img, alpha_scalar), cx0, cy0)
                        else:
                            layer_img = _with_alpha(layer_img, alpha_scalar)
                            base.paste(layer_img, (cx0 + ox, cy0 + oy), layer_img)
                    enc.stdin.write(base.tobytes())
                    frame_idx += 1
            finally:
                dec.stdout.close()
                dec.wait()
                enc.stdin.close()
                enc.wait()

            if Path(overlay_out).exists() and Path(overlay_out).stat().st_size > 0:
                _sh.copy2(overlay_out, dst_path)
            else:
                _sh.copy2(current_src, dst_path)
    except Exception as _e:
        import traceback as _tb
        print(f"[overlay_preview] 오버레이 적용 실패: {_e}\n{_tb.format_exc()}", flush=True, file=__import__('sys').stderr)
        _sh.copy2(src_path, dst_path)
    finally:
        _sh.rmtree(tmp_dir, ignore_errors=True)


def _render_shape_overlay_png(
    shape_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> str | None:
    """Pillow로 도형(rect/circle) PNG를 video_width×video_height 크기로 생성하고 경로를 반환한다.

    반환값: PNG 파일 경로 (실패 시 None)
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    shape = shape_data.get("shape", "rect")
    # 비율(0.0~1.0) → 픽셀 변환
    x = int(float(shape_data.get("x", 0)) * video_width)
    y = int(float(shape_data.get("y", 0)) * video_height)
    w = int(float(shape_data.get("width", 0.1)) * video_width)
    h = int(float(shape_data.get("height", 0.1)) * video_height)
    color_hex = shape_data.get("color", "#ff0000")
    opacity = float(shape_data.get("opacity", 1.0))
    alpha = max(0, min(255, int(opacity * 255)))

    # hex 색상 → RGB 변환
    color_hex = color_hex.lstrip("#")
    try:
        r, g, b = int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 255, 0, 0

    img = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    fill_color = (r, g, b, alpha)
    bbox = [x, y, x + w, y + h]

    if shape == "ellipse":
        draw.ellipse(bbox, fill=fill_color)
    else:
        draw.rectangle(bbox, fill=fill_color)

    png_path = str(Path(tmp_dir) / f"shape_{idx:03d}.png")
    img.save(png_path, "PNG")
    return png_path


def _apply_shape_overlays_preview(
    src_path: str,
    dst_path: str,
    shape_overlays: list[dict],
    video_width: int,
    video_height: int,
) -> None:
    """도형 오버레이를 Pillow PNG → ffmpeg overlay 방식으로 적용한다.

    실패 시 src를 dst로 복사한다.
    """
    import shutil as _sh
    import tempfile

    if not shape_overlays:
        _sh.copy2(src_path, dst_path)
        return

    tmp_dir = tempfile.mkdtemp(prefix="vag_shape_overlay_")
    try:
        # 실제 입력 영상 크기를 ffprobe로 확인
        actual_size = _ffprobe_video_size(src_path)
        if actual_size:
            video_width, video_height = actual_size

        # 도형 PNG 생성
        # #287 — 원본 sh_data dict도 함께 저장하여 프레임별 fade 알파 계산.
        overlay_layers: list[tuple] = []  # (png_img, start_sec, end_sec, sh_data)
        for i, sh_data in enumerate(shape_overlays):
            png_path = _render_shape_overlay_png(sh_data, video_width, video_height, tmp_dir, i)
            if not png_path:
                continue
            try:
                from PIL import Image as _PILImage
                png_img = _PILImage.open(png_path).convert("RGBA")
            except Exception:
                continue
            start_sec = float(sh_data.get("start_sec", 0.0))
            end_sec = sh_data.get("end_sec")
            overlay_layers.append((png_img, start_sec, end_sec, sh_data))

        if not overlay_layers:
            _sh.copy2(src_path, dst_path)
            return

        from PIL import Image as _PILImage

        # 영상 fps 확인
        fps_r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        fps_str = fps_r.stdout.strip()
        fps = 30.0
        if "/" in fps_str:
            n, d = fps_str.split("/")
            fps = float(n) / max(float(d), 1)
        elif fps_str:
            try:
                fps = float(fps_str)
            except ValueError:
                pass

        audio_check = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        has_audio = bool(audio_check.stdout.strip())

        overlay_out = str(Path(tmp_dir) / "shape_overlay.mp4")

        # decode: raw RGB24
        dec = subprocess.Popen(
            [ffmpeg_bin(), "-i", src_path,
             "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        # 오디오 별도 추출
        audio_tmp = str(Path(tmp_dir) / "audio_tmp.aac")
        if has_audio:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-i", src_path, "-vn", "-c:a", "copy", audio_tmp],
                capture_output=True,
            )

        enc_cmd = [
            ffmpeg_bin(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{video_width}x{video_height}",
            "-r", str(fps),
            "-i", "pipe:0",
        ]
        if has_audio and Path(audio_tmp).exists() and Path(audio_tmp).stat().st_size > 0:
            enc_cmd += ["-i", audio_tmp, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
        else:
            enc_cmd += ["-an"]
        enc_cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast",
                    "-pix_fmt", "yuv420p", overlay_out]
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        # #287 — 클립 길이로 end_sec=None인 fade 종료점 결정
        from src.clip_editor import get_duration as _get_duration_sh, _compute_overlay_alpha_scalar as _alpha_scalar_sh
        try:
            clip_dur_sh = _get_duration_sh(src_path)
        except Exception:
            clip_dur_sh = None

        frame_size = video_width * video_height * 3
        frame_idx = 0
        try:
            # 미리보기 도형 오버레이 페이드 인/아웃 — 최종 렌더와 동일 동작.
            FADE_BASE_PREV_SH = 0.3
            while True:
                raw = dec.stdout.read(frame_size)
                if len(raw) < frame_size:
                    break
                t = frame_idx / fps
                base = _PILImage.frombytes("RGB", (video_width, video_height), raw).convert("RGBA")
                for (png_img, start_sec, end_sec, sh_data) in overlay_layers:
                    if t < start_sec:
                        continue
                    if end_sec is not None and t > end_sec:
                        continue
                    # #287 — 도형 오버레이는 fade만 적용 (none이면 1.0)
                    alpha_scalar = _alpha_scalar_sh(
                        sh_data, t,
                        visual_clip_duration=clip_dur_sh,
                        text_supported=False,
                    )
                    if alpha_scalar >= 0.999:
                        base = _PILImage.alpha_composite(base, png_img)
                    elif alpha_scalar > 0.0:
                        r_, g_, b_, a_ = png_img.split()
                        a_scaled = a_.point(lambda v, m=alpha_scalar: int(v * m))
                        faded = _PILImage.merge("RGBA", (r_, g_, b_, a_scaled))
                        base = _PILImage.alpha_composite(base, faded)
                enc.stdin.write(base.convert("RGB").tobytes())
                frame_idx += 1
        finally:
            dec.stdout.close()
            dec.wait()
            enc.stdin.close()
            enc.wait()

        if Path(overlay_out).exists() and Path(overlay_out).stat().st_size > 0:
            _sh.copy2(overlay_out, dst_path)
        else:
            _sh.copy2(src_path, dst_path)

    except Exception as _e:
        import traceback as _tb
        print(f"[shape_overlay] 도형 오버레이 적용 실패: {_e}\n{_tb.format_exc()}", flush=True, file=__import__('sys').stderr)
        _sh.copy2(src_path, dst_path)
    finally:
        _sh.rmtree(tmp_dir, ignore_errors=True)


def _apply_image_overlays_preview(
    src_path: str,
    dst_path: str,
    image_overlays: list[dict],
    video_width: int,
    video_height: int,
) -> None:
    """이미지 오버레이 미리보기 합성 진입점.

    실제 합성은 ``src.clip_editor.apply_image_overlays`` 를 그대로 호출한다 — 최종 렌더와
    미리보기 결과를 픽셀 단위로 동일하게 유지하기 위함. 각 overlay dict 는 호출 측
    (라우터의 `image` 카테고리 분기)이 미리 ``_image_path`` 절대경로를 주입해야 한다.
    실패 시 src 를 dst 로 복사한다 (회귀 안전).
    """
    import shutil as _sh
    try:
        # 해상도 보정은 apply_image_overlays 내부(_resolve_overlay_canvas)가 담당한다.
        #   예전엔 여기서만 ffprobe 보정을 했고 최종 렌더 경로엔 없어서 두 경로가 비대칭이었다.
        #   이제 공통 헬퍼가 양쪽 모두 보정하므로 여기서 따로 덮어쓰지 않는다.
        from src.clip_editor import apply_image_overlays as _apply
        ok = _apply(src_path, dst_path, image_overlays, video_width, video_height)
        if not ok:
            # apply_image_overlays 가 실패해도 src→dst 복사를 보장하지만, 방어적으로 한 번 더 확인.
            if not Path(dst_path).exists() or Path(dst_path).stat().st_size == 0:
                _sh.copy2(src_path, dst_path)
    except Exception as _e:
        import traceback as _tb
        print(f"[image_overlay] 이미지 오버레이 미리보기 실패: {_e}\n{_tb.format_exc()}", flush=True, file=__import__('sys').stderr)
        try:
            _sh.copy2(src_path, dst_path)
        except Exception:
            pass


# ── Footage Upload ─────────────────────────────────────────────────────────

@router.post("/{project_id}/scenes/{scene_id}/footage-upload")
async def footage_upload(
    project_id: str,
    scene_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    client_meta: str | None = Form(None),
    on_conflict: str = Form("skip"),
):
    """프론트엔드 IndexedDB에 저장된 footage Blob을 서버에 업로드한다.

    multipart/form-data로 영상 파일을 수신하여
    web/data/{project_id}/footage/ 디렉토리에 원본 파일명으로 저장한다.
    p.footage_dir이 비어있으면 해당 경로를 footage_dir로 자동 설정한다.

    #308 — `client_meta`(JSON: width/height/duration)가 동봉되면 sanity check
    후 신뢰. 본 엔드포인트는 메타를 직접 사용하지 않지만, 통합/검증을 위해
    파싱은 수행하고 결과를 로그로 남긴다(향후 인덱싱·매칭 단계에서 활용).
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    footage_dir = store.get_output_dir(project_id).parent / "footage"
    footage_dir.mkdir(parents=True, exist_ok=True)

    filename = Path(file.filename or f"footage_scene_{scene_id:03d}.mp4").name
    saved_path = footage_dir / filename

    # 같은 이름이 이미 있을 때의 처리 — on_conflict 로 결정 (기본 skip: 하위호환).
    policy = normalize_policy(on_conflict)
    conflicted = saved_path.exists() and saved_path.stat().st_size > 0
    if conflicted and policy == "skip":
        await file.read()  # multipart body 소비
        return {"ok": True, "path": str(saved_path), "filename": filename,
                "requested_filename": filename, "skipped": True,
                "conflict": True, "applied_policy": "skip"}
    if conflicted and policy == "rename":
        saved_path = resolve_footage_name(footage_dir, filename, SUFFIX_PAREN)
        filename = saved_path.name

    try:
        content = await file.read()
        if not content:
            raise HTTPException(400, "업로드된 파일이 비어 있습니다")
        saved_path.write_bytes(content)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"footage 파일 저장 오류: {e}") from e

    # 덮어쓰기는 프록시·인덱스 캐시를 낡게 만든다 — 반드시 무효화.
    if conflicted and policy == "overwrite":
        invalidate_footage_caches(project_id, filename)

    if not p.footage_dir:
        store.update_project(project_id, footage_dir=str(footage_dir))

    # 씬이 없으면 자동 생성 — 브라우저 전용 프로젝트 preview 지원
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        ext = Path(filename).suffix.lower()
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        if ext in image_exts:
            from ..models import KenburnsEffect
            footage_effect = KenburnsEffect(image=filename)
        else:
            from ..models import ClipEffect
            footage_effect = ClipEffect(file=filename)
        new_scene = SceneCard(scene_id=scene_id, footage=footage_effect)
        p.scenes.append(new_scene)
        store._dump(p)

    # #308 — client_meta 통합 로그(이미지에는 의미 없음). 신뢰 모드 진입 여부만 표기.
    # 본 엔드포인트는 메타 자체를 응답에 포함하지 않지만, 후속 호출(인덱싱 등)이
    # 동일 파일에 대해 ffprobe 를 다시 돌리지 않도록 향후 캐시화 여지를 남긴다.
    _client_meta_obj = _parse_client_video_meta(client_meta)
    if _client_meta_obj is not None:
        print(
            f"[client_meta] footage_upload trust: scene={scene_id} "
            f"{_client_meta_obj['width']}x{_client_meta_obj['height']} "
            f"dur={_client_meta_obj['duration']:.2f}s",
            flush=True,
        )

    # #301 — 업로드된 파일의 저장 비용 기록 (ingest egress는 보통 무료)
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_transcode(
                cpu_seconds=0,
                output_bytes=saved_path.stat().st_size if saved_path.exists() else 0,
                phase_label="12_footage_upload",
            )
    except Exception as _err:
        print(f"[cost] footage_upload 기록 실패: {_err}")

    background_tasks.add_task(_ensure_proxy, project_id, filename)
    return {
        "ok": True,
        "path": str(saved_path),
        "filename": filename,
        "requested_filename": Path(file.filename or filename).name,
        "skipped": False,
        "conflict": conflicted,
        "applied_policy": policy if conflicted else "none",
        "client_meta_trusted": _client_meta_obj is not None,
    }


# ── TTS Upload ─────────────────────────────────────────────────────────────

# NOTE: `GET /{project_id}/tts/{scene_id}/audio` 는 여기 있었으나 제거했다.
#   같은 경로가 `routers/tts.py` 에도 등록돼 있어 **중복 라우트**였고, 먼저 등록되는
#   이쪽이 요청을 가로챘다. 그런데 이 구현은 옛 파일명(`narration_scene_NNN.wav`)만
#   찾아서, 현행 파일명(`scene_NNN_seg_seg_0.wav`)으로 TTS 를 만들어도 **항상 404** 였다.
#   `tts.py` 의 구현은 신규 파일명을 우선 보고 없을 때만 옛 경로로 폴백하므로 그쪽이 맞다.


@router.post("/{project_id}/scenes/{scene_id}/tts-upload")
async def tts_upload(
    project_id: str,
    scene_id: int,
    file: UploadFile = File(...),
    client_meta: str | None = Form(None),
):
    """프론트엔드 IndexedDB에 저장된 TTS Blob을 서버에 업로드한다.

    multipart/form-data로 오디오 파일(wav 또는 mp3)을 수신하여
    output/narration/ 디렉토리에 narration_scene_{scene_id:03d}.wav 패턴으로 저장한다.

    #308 — `client_meta`(JSON: duration)가 동봉되면 sanity check 후 WAV
    재측정(`wave.open` + frames/rate)을 생략한다. 파싱 실패/누락 시 기존 측정 fallback.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    output_dir = store.get_output_dir(project_id) / "narration"
    output_dir.mkdir(parents=True, exist_ok=True)

    wav_path = output_dir / f"narration_scene_{scene_id:03d}.wav"

    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if scene is None:
        raise HTTPException(404, "씬을 찾을 수 없습니다")
    # narration_text가 비어 있는 씬에 옛 IndexedDB blob이 부활하는 잔재 누수 차단.
    if not (scene.narration_text or "").strip():
        raise HTTPException(400, "narration_text가 비어 있어 TTS를 업로드할 수 없습니다")

    # #308 — try 블록 밖에서 client_meta 신뢰 판단에 사용하므로 미리 초기화.
    is_mp3 = False

    try:
        content = await file.read()
        if not content:
            raise HTTPException(400, "업로드된 파일이 비어 있습니다")

        # MP3 감지: 매직 바이트 ID3 또는 0xFF 0xFB/0xFA/0xF3
        is_mp3 = (
            content[:3] == b"ID3"
            or (len(content) >= 2 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0)
        )
        if is_mp3:
            import subprocess, tempfile, time as _t_mp3
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                _mp3_t0 = _t_mp3.monotonic()
                # MP3 → WAV 변환.
                # #tts-truncate 버그 — Google TTS Chirp HD가 만든 MP3는 길이가 잘못
                # 기록된 경우가 있고(평균 비트레이트 추정 헤더가 짧게 표시), 일반 `-i mp3`
                # 디코딩이 첫 몇 프레임만 처리하고 EOF로 판단할 수 있다.
                # 해결: `-f mp3`로 컨테이너 명시 + libmp3lame 디코더 명시 + `-vn`으로
                # 비디오 스트림 무시. 추가로 변환 후 길이를 입력 MP3와 대조해서
                # 50% 이상 짧으면 재시도(다른 디코더 경로).
                _mp3_cmd = [
                    ffmpeg_bin(), "-y",
                    "-f", "mp3",
                    "-i", tmp_path,
                    "-vn",
                    "-ar", "22050", "-ac", "1",
                    str(wav_path),
                ]
                subprocess.run(_mp3_cmd, check=True, capture_output=True)
                # 변환 후 길이 검증
                try:
                    _src_dur_r = subprocess.run(
                        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                         "-of", "csv=p=0", tmp_path],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                    )
                    _src_dur = float(_src_dur_r.stdout.strip() or 0)
                except Exception:
                    _src_dur = 0.0
                try:
                    import wave as _wave_chk
                    with _wave_chk.open(str(wav_path), "rb") as _w_chk:
                        _dst_dur = _w_chk.getnframes() / _w_chk.getframerate()
                except Exception:
                    _dst_dur = 0.0
                # 출력이 입력의 70% 미만이면 잘림으로 간주하고 packet 단위 재디코딩 시도.
                if _src_dur > 0 and _dst_dur > 0 and _dst_dur < _src_dur * 0.7:
                    print(f"[tts-upload] MP3→WAV 잘림 감지 src={_src_dur:.2f}s dst={_dst_dur:.2f}s — 재시도", flush=True)
                    subprocess.run(
                        [ffmpeg_bin(), "-y",
                         "-acodec", "mp3",
                         "-i", tmp_path,
                         "-vn",
                         "-ar", "22050", "-ac", "1",
                         "-fflags", "+genpts",
                         str(wav_path)],
                        check=False, capture_output=True,
                    )
                _mp3_cpu = _t_mp3.monotonic() - _mp3_t0
                try:
                    from ..cost_tracker import measure_phase
                    with measure_phase(project_id) as _est:
                        _est.record_transcode(
                            cpu_seconds=_mp3_cpu,
                            output_bytes=wav_path.stat().st_size,
                            phase_label="13_tts_upload_transcode",
                        )
                except Exception as _err:
                    print(f"[cost] tts_upload 기록 실패: {_err}")
            finally:
                import os as _os
                _os.unlink(tmp_path)
        else:
            wav_path.write_bytes(content)
            try:
                from ..cost_tracker import measure_phase
                with measure_phase(project_id) as _est:
                    _est.record_transcode(
                        cpu_seconds=0,
                        output_bytes=wav_path.stat().st_size,
                        phase_label="13_tts_upload",
                    )
            except Exception as _err:
                print(f"[cost] tts_upload 기록 실패: {_err}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"TTS 파일 저장 오류: {e}") from e

    # 레거시 WAV(seg_0)가 성공적으로 최종 확정된 뒤에만, 짝이 되는 stale seg_0
    # 세그먼트 WAV 를 제거한다. build_merged_wav 는 seg_0 세그먼트 WAV 가 존재하고
    # 0바이트가 아니면 그것을 우선 채택하고, 없을 때만 방금 쓴 레거시 WAV 로
    # 폴백한다(narration_merge.py:186). "전체 재생성"은 레거시 경로만 갱신하므로
    # 옛 세그먼트 WAV 가 남아 있으면 미리보기/렌더가 계속 옛 목소리를 읽는 회귀가
    # 발생한다 → 여기서 unlink 하여 새 레거시 WAV 로 폴백되게 한다.
    # seg_1 이상(사용자가 타임라인에 추가한 나레이션)은 절대 건드리지 않는다 —
    # 정확히 `scene_{id:03d}_seg_seg_0.wav` 파일만 대상.
    try:
        stale_seg0 = output_dir / f"scene_{scene_id:03d}_seg_seg_0.wav"
        stale_seg0.unlink(missing_ok=True)
    except Exception as _err:
        print(f"[tts-upload] stale seg_0 정리 실패(무시): {_err}", flush=True)

    # WAV 길이 측정 후 씬 메타 갱신 — render 시 synced=True여야 재생성 생략.
    # #308 — client_meta(duration)가 있으면 sanity check 후 wave.open 측정 생략.
    # MP3 → WAV 변환 경로에서는 변환 후 길이가 원본과 달라질 수 있으므로
    # 변환 발생 시(is_mp3 == True) 무조건 재측정. 변환 없는 WAV 직저장에서만 신뢰.
    _client_audio_meta = _parse_client_audio_meta(client_meta)
    _used_client_meta = False
    dur = 0.0
    if _client_audio_meta is not None and not is_mp3:
        dur = float(_client_audio_meta["duration"])
        _used_client_meta = True
        print(
            f"[client_meta] tts_upload trust: scene={scene_id} dur={dur:.2f}s",
            flush=True,
        )
    else:
        try:
            import wave as _wave
            with _wave.open(str(wav_path), "rb") as _w:
                dur = _w.getnframes() / _w.getframerate()
        except Exception:
            dur = 0.0
    from ..models import TtsMeta
    store.update_scene(project_id, scene_id, tts=TtsMeta(
        audio_url=f"/api/projects/{project_id}/tts/{scene_id}/audio",
        duration_sec=dur,
        status="ready",
        synced=True,
    ))

    return {"ok": True, "client_meta_trusted": _used_client_meta}


# ── Match status ───────────────────────────────────────────────────────────

@router.get("/{project_id}/match-status")
def match_status(project_id: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    unmatched = [s.scene_id for s in p.scenes if s.footage is None or
                 (hasattr(s.footage, "file") and not s.footage.file and
                  not hasattr(s.footage, "image"))]
    return {
        "matched": len(p.scenes) - len(unmatched),
        "total": len(p.scenes),
        "unmatched_scene_ids": unmatched,
        "all_matched": len(unmatched) == 0,
    }


def _get_duration(path: str) -> float:
    # format duration 우선
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        v = float(r.stdout.strip())
        if v > 0:
            return v
    except Exception:
        pass
    # webm 등 duration=N/A — 마지막 패킷 pts_time으로 측정
    r2 = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lines = [l.strip().rstrip(",") for l in r2.stdout.splitlines() if l.strip()]
    if lines:
        try:
            return float(lines[-1])
        except ValueError:
            pass
    return 0.0
