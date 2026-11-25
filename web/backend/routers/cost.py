"""모의 클라우드 비용 조회 API (#301).

- GET /api/projects/{id}/cost — 누적 비용
- GET /api/projects/{id}/cost/preview — 현재 프로젝트 상태로 렌더 시 예상 비용
- GET /api/cost/pricing — 현재 적용 중인 단가표
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException

from src.cost_estimator import load_pricing, preview_video_cost
from .. import state as store
from ..cost_tracker import get_cost_summary


router = APIRouter(tags=["cost"])


@router.get("/api/projects/{project_id}/cost")
def get_cost(project_id: str):
    """누적 비용 조회. 아직 기록이 없으면 빈 객체.

    #355 — 응답에 `by_model` (Gemini 모델 ID → 누적 USD) 요약을 함께 포함한다.
    기존 phases 구조는 그대로 유지하며, 모델 정보가 없는 구버전 항목은
    by_model 키 `""`(빈 문자열)로 합산된다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    summary = get_cost_summary(project_id) or {
        "provider": load_pricing().get("provider", "hetzner_r2"),
        "currency": "USD",
        "total_usd": 0,
        "phases": [],
    }
    # by_model 집계 — phases 의 각 항목에서 (gemini_model, total_usd) 를 모은다.
    # 모델 필드가 없는 구버전 항목은 키 "" 로 들어가며, UI 는 이를 "기타/구버전" 으로 표시.
    by_model: dict[str, float] = {}
    for ph in summary.get("phases", []) or []:
        model_key = (ph.get("gemini_model") or "") if isinstance(ph, dict) else ""
        # api_usd 만 모델별로 의미가 있으므로 그것만 집계.
        # (compute/storage/egress 는 모델과 무관하다)
        api_usd = float(ph.get("api_usd", 0) or 0) if isinstance(ph, dict) else 0.0
        if api_usd == 0:
            continue
        by_model[model_key] = by_model.get(model_key, 0.0) + api_usd
    summary["by_model"] = by_model
    return summary


@router.get("/api/projects/{project_id}/cost/preview")
def preview_cost(project_id: str):
    """현재 프로젝트 상태로 렌더링했을 때의 예상 비용."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    scenes = len(p.scenes)
    if scenes == 0:
        return {"total_usd": 0, "phases": [], "note": "씬이 없습니다"}

    # 평균 나레이션 글자수
    narration_chars = [len(s.narration_text or "") for s in p.scenes]
    avg_narration = int(sum(narration_chars) / scenes) if scenes else 0

    # 영상 길이 추정 (TTS duration 합)
    total_duration = 0.0
    for s in p.scenes:
        if s.tts and s.tts.duration_sec:
            total_duration += s.tts.duration_sec
        else:
            # TTS 미생성 — duration_hint 기반
            total_duration += s.duration_hint_sec or 5.0

    # footage 청크 수 (footage_index.json 기준)
    cache_dir = store.get_cache_dir(project_id)
    idx_path = cache_dir / "footage_index.json"
    footage_chunks = 0
    footage_total_sec = 0.0
    if idx_path.exists():
        try:
            import json as _json
            idx = _json.loads(idx_path.read_text(encoding="utf-8"))
            for ch in idx.get("chunks", []):
                footage_chunks += 1
                footage_total_sec += max(0.0, float(ch.get("end_sec", 0)) - float(ch.get("start_sec", 0)))
        except Exception:
            pass

    # TTS provider 판별 — 세그먼트별 voice override 우선, 없으면 프로젝트 tts_voice.
    # 하나라도 typecast(tc_/uc_) voice 가 있으면 typecast 로 간주해 크레딧 예상을 표시.
    from src.tts import resolve_provider
    voices: list[str] = []
    for s in p.scenes:
        for seg in (getattr(s, "narration_segments", None) or []):
            if getattr(seg, "voice_name", None):
                voices.append(seg.voice_name)
    if getattr(p, "tts_voice", None):
        voices.append(p.tts_voice)
    tts_provider = "typecast" if any(resolve_provider(v) == "typecast" for v in voices) else "google"

    return preview_video_cost(
        scenes=scenes,
        avg_narration_chars_per_scene=avg_narration,
        avg_output_duration_sec=total_duration,
        footage_total_duration_sec=footage_total_sec,
        footage_chunks=footage_chunks,
        tts_provider=tts_provider,
        # Gemini 단계(파싱·인덱싱·매칭)는 Claude Code 로 대체되어 예상 비용에서 제외.
        include_gemini=False,
    )


@router.get("/api/cost/pricing")
def get_pricing():
    """현재 적용 중인 단가표."""
    return load_pricing(force_reload=True)
