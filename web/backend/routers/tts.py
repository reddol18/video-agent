from __future__ import annotations
import asyncio
import sys
from pathlib import Path

import tempfile

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ..models import (
    CreateNarrationSegmentRequest,
    GenerateNarrationSegmentRequest,
    GenerateTtsRequest,
    NarrationSegment,
    SetSpeakersRequest,
    SetTtsVoiceRequest,
    Speaker,
    TtsMeta,
    UpdateNarrationRequest,
    UpdateNarrationSegmentRequest,
)
from .. import state as store
from ..speakers import (
    extract_speaker_name,
    find_speaker,
    normalize_name,
    resolve_scene_voice,
    resolve_segment_voice,
)
from ..ws.progress import create_task_id, push_sync, register_bg_task

router = APIRouter(prefix="/api/projects", tags=["tts"])

# 프로젝트 무관 TTS 엔드포인트(voice 목록 프록시 등).
voices_router = APIRouter(prefix="/api/tts", tags=["tts"])

# Typecast voice 목록 캐시 — 프로세스 수명 동안 1회 조회 후 재사용(596개, 저빈도 변경).
_TYPECAST_VOICES_CACHE: list[dict] | None = None


@voices_router.get("/voices")
def get_tts_voices(provider: str = "typecast"):
    """provider 별 voice 목록 프록시.

    - ``provider=typecast``: TYPECAST_API_KEY 로 Typecast /v2/voices 조회 후 반환.
      키 미설정이면 400. Google voice 는 프론트가 하드코딩 목록을 쓰므로 여기서
      제공하지 않는다(빈 배열).
    """
    global _TYPECAST_VOICES_CACHE
    if provider != "typecast":
        return {"provider": provider, "voices": []}

    if _TYPECAST_VOICES_CACHE is not None:
        return {"provider": "typecast", "voices": _TYPECAST_VOICES_CACHE,
                "cached": True}

    from src.tts import list_typecast_voices
    from src.tts.typecast_provider import TypecastError
    try:
        voices = list_typecast_voices()
    except TypecastError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # 네트워크 등 예기치 못한 오류
        raise HTTPException(502, f"Typecast voice 조회 실패: {e}")
    _TYPECAST_VOICES_CACHE = voices
    return {"provider": "typecast", "voices": voices, "cached": False}


class TtsPreviewRequest(BaseModel):
    voice_name: str
    # 짧은 샘플 문장. 미지정 시 기본 안내 문장 사용.
    text: str | None = None
    rate: float | None = None


_PREVIEW_SAMPLE_TEXT = "안녕하세요. 이 목소리로 나레이션이 만들어집니다."


@voices_router.post("/preview")
def preview_tts(req: TtsPreviewRequest):
    """공급자 분기(Google/Typecast)를 태우는 서버측 미리듣기 합성.

    프론트 ``synthesizeTts`` 는 Google REST 를 브라우저에서 직접 호출하므로
    Typecast voice(``tc_``/``uc_``) 미리듣기가 불가능하다. 이 엔드포인트는
    ``src.tts.synthesize`` 에 위임해 voice prefix 로 provider 를 자동 선택하고,
    합성 결과 오디오 바이트를 그대로 반환한다.

    - Google voice 도 동작하지만, 프론트는 회귀 방지를 위해 Typecast 일 때만 호출한다.
    - 반환 형식은 provider 원본(Typecast=wav, Google=mp3). Content-Type 을 그에 맞춰 세팅.
    """
    from src.tts import resolve_provider, synthesize
    from src.tts.typecast_provider import TypecastError

    voice = (req.voice_name or "").strip()
    if not voice:
        raise HTTPException(400, "voice_name 이 비어 있어요.")
    text = (req.text or _PREVIEW_SAMPLE_TEXT).strip() or _PREVIEW_SAMPLE_TEXT
    rate = req.rate if req.rate is not None else 1.0
    provider = resolve_provider(voice)
    suffix = ".wav" if provider == "typecast" else ".mp3"
    media_type = "audio/wav" if provider == "typecast" else "audio/mpeg"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        dst = tmp.name
        try:
            synthesize(text, dst, voice=voice, rate=rate)
        except TypecastError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # 키/네트워크/합성 실패
            raise HTTPException(502, f"미리듣기 합성 실패: {e}")
        with open(dst, "rb") as f:
            data = f.read()

    if not data:
        raise HTTPException(502, "미리듣기 오디오가 비어 있어요.")
    return Response(content=data, media_type=media_type)


# ── #382 — narration_segments 헬퍼 ─────────────────────────────────────
# 신규 경로 규약: output/narration/scene_{scene_id:03d}_seg_{seg_id}.wav

def _segment_wav_path(project_id: str, scene_id: int, seg_id: str) -> Path:
    narr_dir = store.get_output_dir(project_id) / "narration"
    narr_dir.mkdir(parents=True, exist_ok=True)
    # seg_id 에 슬래시/공백 등이 섞이지 않게 안전 처리 (segment id 는 영문/숫자/_/-).
    safe_seg = "".join(c for c in seg_id if c.isalnum() or c in ("_", "-")) or "seg"
    return narr_dir / f"scene_{scene_id:03d}_seg_{safe_seg}.wav"


def _segment_audio_url(project_id: str, seg_id: str) -> str:
    return f"/api/projects/{project_id}/tts/segment/{seg_id}/audio"


def _find_scene_and_seg(project_id: str, scene_id: int, seg_id: str):
    """(project, scene, segment_index, segment) 튜플 반환. 없으면 (None, None, -1, None).

    seg_id 매칭은 정확 일치만 허용한다.
    """
    p = store.get_project(project_id)
    if not p:
        return None, None, -1, None
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        return p, None, -1, None
    for idx, seg in enumerate(scene.narration_segments or []):
        if seg.id == seg_id:
            return p, scene, idx, seg
    return p, scene, -1, None


def _next_segment_id(scene) -> str:
    """seg_0, seg_1, ... 형태로 다음 segment id 발급. 기존 id 와 충돌하지 않는 최소 정수."""
    existing = {seg.id for seg in (scene.narration_segments or [])}
    n = 0
    while f"seg_{n}" in existing:
        n += 1
    return f"seg_{n}"


def _persist_scene_segments(project_id: str, scene) -> None:
    """씬의 narration_segments 변경 후 디스크 저장 + narration_text/tts 동기화.

    호환성: seg_0 의 text/duration/synced 를 SceneCard.narration_text/tts 에 그대로 복사해
    아직 마이그레이션되지 않은 호출자(렌더, FCPXML 등)가 옛 필드를 읽어도 일관된
    값을 보도록 유지한다.
    """
    seg_0 = next((s for s in (scene.narration_segments or []) if s.id == "seg_0"), None)
    if seg_0 is not None:
        scene.narration_text = seg_0.text
        scene.tts = TtsMeta(
            audio_url=seg_0.audio_url,
            duration_sec=seg_0.duration_sec,
            status=seg_0.status,
            synced=seg_0.synced,
        ) if seg_0.audio_url or seg_0.duration_sec or seg_0.status != "idle" else None
    store.update_scene(project_id, scene.scene_id,
                       narration_segments=list(scene.narration_segments),
                       narration_text=scene.narration_text,
                       tts=scene.tts)


@router.patch("/{project_id}/tts/voice")
def set_tts_voice(project_id: str, req: SetTtsVoiceRequest):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    store.update_project(project_id, tts_voice=req.voice_name)
    return {"voice_name": req.voice_name}


# ── 다중 화자 사전 ──────────────────────────────────────────────────────────

@router.get("/{project_id}/speakers")
def list_speakers(project_id: str) -> dict:
    """화자 목록 + 각 화자가 실제로 쓰이는 씬 번호."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    used: dict[str, list[int]] = {}
    unknown: dict[str, list[int]] = {}
    for s in p.scenes:
        name = extract_speaker_name(getattr(s, "tags", None))
        if not name:
            continue
        bucket = used if find_speaker(p.speakers, name) else unknown
        bucket.setdefault(normalize_name(name), []).append(s.scene_id)

    return {
        "speakers": [
            {**sp.model_dump(), "scene_ids": used.get(normalize_name(sp.name), [])}
            for sp in p.speakers
        ],
        "default_voice": p.tts_voice,
        # 태그에는 있는데 사전에 없는 이름 — 오타 확인용.
        "unknown_speakers": [
            {"name": n, "scene_ids": ids} for n, ids in sorted(unknown.items())
        ],
    }


@router.put("/{project_id}/speakers")
def put_speakers(project_id: str, req: SetSpeakersRequest) -> dict:
    """화자 사전 전체 교체. 이름 중복(대소문자 무시)은 거부한다."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    cleaned: list[Speaker] = []
    seen: set[str] = set()
    for sp in req.speakers:
        name = (sp.name or "").strip()
        if not name:
            raise HTTPException(400, "화자 이름이 비어 있습니다")
        if not (sp.voice_name or "").strip():
            raise HTTPException(400, f"화자 '{name}' 의 목소리가 지정되지 않았습니다")
        key = normalize_name(name)
        if key in seen:
            raise HTTPException(400, f"화자 이름이 중복됩니다: {name}")
        seen.add(key)
        cleaned.append(sp.model_copy(update={"name": name}))

    store.update_project(project_id, speakers=cleaned)
    return {"speakers": [sp.model_dump() for sp in cleaned]}


@router.post("/{project_id}/tts/generate")
async def generate_tts(project_id: str, req: GenerateTtsRequest, background_tasks: BackgroundTasks):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    if not p.scenes:
        raise HTTPException(400, "씬이 없습니다")

    voice_name = req.voice_name or p.tts_voice
    task_id = create_task_id()

    async def _run():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_generate_tts,
                                   project_id, req.scene_ids, req.force, task_id, voice_name)

    task = asyncio.create_task(_run())
    register_bg_task(task_id, task)
    return {"task_id": task_id}


def _do_generate_tts(project_id: str, scene_ids: list[int] | None, force: bool, task_id: str,
                     voice_name: str | None = None):
    """#382 — 씬별로 모든 narration_segments 의 TTS 를 생성한다.

    seg_0 만 있는 마이그레이션 직후 프로젝트는 기존 동작과 결과가 동일하다.
    각 segment 는 신규 경로 ``scene_{id:03d}_seg_{seg_id}.wav`` 로 저장된다.
    """
    p = store.get_project(project_id)
    if not p:
        return

    from src.narration_generator import _tts as tts_synthesize

    effective_voice = voice_name or p.tts_voice

    output_dir = store.get_output_dir(project_id) / "narration"
    output_dir.mkdir(parents=True, exist_ok=True)

    targets = [s for s in p.scenes if scene_ids is None or s.scene_id in scene_ids]
    total = len(targets)

    push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "tts",
                        "current": 0, "total": total, "message": "TTS 생성 시작...", "item": ""})

    success = 0
    failed = 0
    tts_chars_generated = 0  # #301 — 실제 TTS 호출된 글자수 누적

    for i, scene in enumerate(targets):
        # 중간 이미지 삽입 씬은 "그림만 보여주는" 씬이라 나레이션을 갖지 않는다.
        # 씬 재번호로 앞 씬의 narration_text 가 흘러들어온 경우 그대로 TTS 를 돌리면
        # 유료 크레딧이 낭비되고 같은 음성이 두 씬에서 나온다. 여기서 원천 차단한다.
        if getattr(scene, "is_insert_image_scene", False):
            push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "tts",
                                "current": i + 1, "total": total,
                                "message": f"씬 {scene.scene_id} 중간 이미지 삽입 씬 — TTS 스킵",
                                "item": scene.title})
            continue

        # 마이그레이션 누락 방어 — narration_segments 가 비어 있어도 narration_text 가
        # 있으면 즉석으로 seg_0 를 만들어 진행한다.
        if not scene.narration_segments and scene.narration_text:
            scene.narration_segments = [NarrationSegment(
                id="seg_0", text=scene.narration_text, offset_sec=0.0,
            )]

        if not scene.narration_segments:
            push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "tts",
                                "current": i + 1, "total": total,
                                "message": f"씬 {scene.scene_id} 나레이션 없음 — 스킵",
                                "item": scene.title})
            continue

        # 씬 태그 speaker:{name} → 그 화자의 목소리. 미등록 이름이면 기본 목소리로
        # 떨어지되 사용자가 알 수 있게 진행 메시지로 알린다.
        _scene_voice, _spk_name = resolve_scene_voice(scene, p, effective_voice)
        if _spk_name and not find_speaker(p.speakers, _spk_name):
            push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "tts",
                                "current": i, "total": total,
                                "message": (
                                    f"씬 {scene.scene_id}: 화자 '{_spk_name}' 가 사전에 없어 "
                                    f"기본 목소리로 생성합니다"
                                ),
                                "item": scene.title})

        any_scene_success = False
        for seg in list(scene.narration_segments):
            seg_voice = seg.voice_name or _scene_voice
            wav_path = _segment_wav_path(project_id, scene.scene_id, seg.id)
            already_synced = (
                wav_path.exists()
                and wav_path.stat().st_size > 0
                and seg.synced
                and seg.status == "ready"
            )
            if already_synced and not force:
                # 상태/URL 만 정리해서 푸시.
                seg.audio_url = _segment_audio_url(project_id, seg.id)
                _persist_scene_segments(project_id, scene)
                push_sync(task_id, {"type": "tts_done", "task_id": task_id,
                                    "scene_id": scene.scene_id,
                                    "audio_url": seg.audio_url,
                                    "duration_sec": seg.duration_sec})
                any_scene_success = True
                continue

            text = seg.text
            if not text or not text.strip():
                # 빈 텍스트는 TTS 없이 스킵.
                continue

            try:
                # 생성 중 상태 반영
                seg.status = "generating"
                seg.synced = False
                seg.error_message = None  # #392 — 재시도 시 옛 에러 메시지 초기화
                _persist_scene_segments(project_id, scene)

                # #tts-emotion — 씬에 지정된 말투 지시문을 함께 전달.
                duration = tts_synthesize(
                    text, str(wav_path), voice_name=seg_voice,
                    emotion=(getattr(scene, "tts_emotion", "") or "").strip() or None,
                )
                tts_chars_generated += len(text)

                seg.audio_url = _segment_audio_url(project_id, seg.id)
                seg.duration_sec = float(duration)
                seg.status = "ready"
                seg.synced = True
                seg.error_message = None  # 성공 시 명시적으로 None 리셋
                _persist_scene_segments(project_id, scene)
                push_sync(task_id, {"type": "tts_done", "task_id": task_id,
                                    "scene_id": scene.scene_id,
                                    "audio_url": seg.audio_url,
                                    "duration_sec": duration})
                any_scene_success = True
            except Exception as e:
                # #392 — TTS 실패 원인 추적: traceback 까지 stderr 로 명시적으로 출력하고,
                #         사용자에게도 status=error + error_message 형태로 노출한다.
                import traceback as _tb
                err_str = f"{type(e).__name__}: {e}"
                print(
                    f"[tts:seg] 실패 scene={scene.scene_id} seg={seg.id} voice={seg_voice!r} "
                    f"err={err_str}",
                    flush=True,
                )
                _tb.print_exc()
                seg.status = "error"
                seg.synced = False
                seg.error_message = err_str
                _persist_scene_segments(project_id, scene)
                push_sync(task_id, {"type": "error", "task_id": task_id,
                                    "code": "TTS_FAILED", "message": err_str,
                                    "scene_id": scene.scene_id})

        if any_scene_success:
            success += 1
        elif scene.narration_segments:
            failed += 1

        push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "tts",
                            "current": i + 1, "total": total,
                            "message": f"씬 {scene.scene_id} TTS 완료", "item": scene.title})

    # #301 — 모의 비용 기록: Phase 4 TTS
    if tts_chars_generated > 0:
        try:
            from ..cost_tracker import measure_phase
            with measure_phase(project_id) as _est:
                _est.record_tts(chars=tts_chars_generated, cpu_seconds=0)
        except Exception as _cost_err:
            print(f"[cost] tts 기록 실패: {_cost_err}")

    push_sync(task_id, {"type": "complete", "task_id": task_id,
                        "summary": {"total": total, "success": success, "failed": failed}})


@router.get("/{project_id}/tts/{scene_id}/audio")
def get_tts_audio(project_id: str, scene_id: int):
    """레거시 엔드포인트 — 씬의 seg_0 WAV 를 우선 반환, 없으면 옛 경로 폴백.

    회귀 안전: 마이그레이션 이전 프로젝트의 narration_scene_NNN.wav 가 그대로 남아 있어도
    seg_0 신규 파일이 없을 때 폴백되어 다운로드가 가능하다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    narr_dir = store.get_output_dir(project_id) / "narration"
    new_path = narr_dir / f"scene_{scene_id:03d}_seg_seg_0.wav"
    legacy_path = narr_dir / f"narration_scene_{scene_id:03d}.wav"
    wav_path = new_path if new_path.exists() else legacy_path
    if not wav_path.exists():
        raise HTTPException(404, "TTS 파일이 없습니다")
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_egress(bytes_size=wav_path.stat().st_size)
    except Exception as _err:
        print(f"[cost] tts_audio 기록 실패: {_err}")
    return FileResponse(str(wav_path), media_type="audio/wav")


# ── #382 — segment 단위 엔드포인트 ────────────────────────────────────────

@router.get("/{project_id}/tts/segment/{seg_id}/audio")
def get_segment_audio(project_id: str, seg_id: str, scene_id: int | None = None):
    """특정 segment 의 WAV 파일을 반환.

    ``scene_id`` 쿼리 파라미터가 지정되면 해당 씬의 wav 파일만 탐색한다(빠른 경로).
    미지정 시 narration 디렉토리에서 ``*_seg_{seg_id}.wav`` 패턴으로 첫 일치를 반환.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    narr_dir = store.get_output_dir(project_id) / "narration"
    wav_path: Path | None = None
    if scene_id is not None:
        cand = _segment_wav_path(project_id, scene_id, seg_id)
        if cand.exists():
            wav_path = cand
    if wav_path is None and narr_dir.exists():
        # seg_id 정규화 (영문/숫자/_/-)
        safe_seg = "".join(c for c in seg_id if c.isalnum() or c in ("_", "-"))
        suffix = f"_seg_{safe_seg}.wav"
        for f in sorted(narr_dir.iterdir()):
            if f.name.endswith(suffix):
                wav_path = f
                break
    if wav_path is None or not wav_path.exists():
        raise HTTPException(404, "segment 오디오 파일이 없습니다")
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_egress(bytes_size=wav_path.stat().st_size)
    except Exception as _err:
        print(f"[cost] segment_audio 기록 실패: {_err}")
    return FileResponse(str(wav_path), media_type="audio/wav")


@router.post("/{project_id}/scenes/{scene_id}/narration/segments")
def create_segment(project_id: str, scene_id: int, req: CreateNarrationSegmentRequest):
    """씬에 신규 narration segment 추가 (#382).

    id 미지정 시 seg_N 형식으로 자동 부여. 중복 id 면 400.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, "씬을 찾을 수 없습니다")

    seg_id = req.id or _next_segment_id(scene)
    if any(s.id == seg_id for s in (scene.narration_segments or [])):
        raise HTTPException(400, f"segment id 중복: {seg_id}")

    seg = NarrationSegment(
        id=seg_id,
        text=req.text or "",
        offset_sec=max(0.0, float(req.offset_sec or 0.0)),
        voice_name=req.voice_name,
        sbv_text=req.sbv_text,
    )
    scene.narration_segments = list(scene.narration_segments or []) + [seg]
    _persist_scene_segments(project_id, scene)
    return seg.model_dump()


@router.patch("/{project_id}/scenes/{scene_id}/narration/segments/{seg_id}")
def update_segment(project_id: str, scene_id: int, seg_id: str,
                   req: UpdateNarrationSegmentRequest):
    """기존 segment 부분 편집 (#382).

    text 가 바뀌면 ``synced=False`` 로 자동 표시한다 (사용자가 [재생성] 버튼으로 새 TTS 호출).
    offset_sec / voice_name / sbv_text 변경은 synced 상태를 건드리지 않는다 (텍스트만이 TTS
    재생성 트리거).
    """
    p, scene, idx, seg = _find_scene_and_seg(project_id, scene_id, seg_id)
    if not scene or seg is None:
        raise HTTPException(404, "segment 를 찾을 수 없습니다")

    if req.text is not None and req.text != seg.text:
        seg.text = req.text
        seg.synced = False
    if req.offset_sec is not None:
        seg.offset_sec = max(0.0, float(req.offset_sec))
    if req.voice_name is not None:
        seg.voice_name = req.voice_name or None
    if req.sbv_text is not None:
        seg.sbv_text = req.sbv_text or None

    scene.narration_segments[idx] = seg
    _persist_scene_segments(project_id, scene)
    return seg.model_dump()


@router.delete("/{project_id}/scenes/{scene_id}/narration/segments/{seg_id}")
def delete_segment(project_id: str, scene_id: int, seg_id: str):
    """segment 삭제. seg_0 도 삭제 허용 (전체 segments 가 빈 배열이 되면 narration 없음 상태).

    삭제된 segment 의 WAV 파일도 즉시 정리한다.
    """
    p, scene, idx, seg = _find_scene_and_seg(project_id, scene_id, seg_id)
    if not scene or seg is None:
        raise HTTPException(404, "segment 를 찾을 수 없습니다")

    # WAV 파일 정리
    try:
        wav = _segment_wav_path(project_id, scene_id, seg_id)
        if wav.exists():
            wav.unlink()
    except Exception:
        pass

    scene.narration_segments = [s for s in scene.narration_segments if s.id != seg_id]
    _persist_scene_segments(project_id, scene)
    return {"deleted": seg_id}


@router.post("/{project_id}/scenes/{scene_id}/narration/segments/{seg_id}/generate")
async def generate_segment_tts(project_id: str, scene_id: int, seg_id: str,
                               req: GenerateNarrationSegmentRequest,
                               background_tasks: BackgroundTasks):
    """특정 segment 의 TTS 만 생성/재생성.

    백그라운드에서 ``_do_generate_segment`` 실행. WebSocket ``tts_done`` 메시지로 완료 통지.
    """
    p, scene, idx, seg = _find_scene_and_seg(project_id, scene_id, seg_id)
    if not scene or seg is None:
        raise HTTPException(404, "segment 를 찾을 수 없습니다")

    task_id = create_task_id()
    voice_override = req.voice_name
    force = bool(req.force)

    async def _run():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_generate_segment,
                                   project_id, scene_id, seg_id, force, task_id, voice_override)

    task = asyncio.create_task(_run())
    register_bg_task(task_id, task)
    return {"task_id": task_id, "seg_id": seg_id, "scene_id": scene_id}


def _do_generate_segment(project_id: str, scene_id: int, seg_id: str,
                         force: bool, task_id: str,
                         voice_override: str | None = None):
    """단일 segment TTS 생성. 동기 실행 함수 (백그라운드 executor 에서 호출)."""
    p, scene, idx, seg = _find_scene_and_seg(project_id, scene_id, seg_id)
    if not p or not scene or seg is None:
        return
    # 중간 이미지 삽입 씬은 나레이션을 갖지 않는다 — 유료 크레딧 낭비 방지 (전체 생성 경로와 동일 정책).
    if getattr(scene, "is_insert_image_scene", False):
        print(f"[tts] scene={scene_id} 중간 이미지 삽입 씬 — segment TTS 스킵", flush=True)
        return

    from src.narration_generator import _tts as tts_synthesize

    # 전체 생성 경로와 같은 규칙을 쓴다 — 여기서만 화자 태그를 빼먹으면
    # "미리듣기는 기본 목소리, 렌더는 화자 목소리" 로 어긋난다.
    effective_voice = voice_override or resolve_segment_voice(seg, scene, p)
    wav_path = _segment_wav_path(project_id, scene_id, seg_id)

    push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "tts",
                        "current": 0, "total": 1,
                        "message": f"씬 {scene_id} segment {seg_id} TTS 생성 중...",
                        "item": seg.text[:30]})

    if not seg.text or not seg.text.strip():
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "EMPTY_TEXT",
                            "message": "텍스트가 비어있어 TTS 를 생성할 수 없습니다",
                            "scene_id": scene_id})
        return

    if seg.synced and seg.status == "ready" and wav_path.exists() and wav_path.stat().st_size > 0 and not force:
        seg.audio_url = _segment_audio_url(project_id, seg_id)
        _persist_scene_segments(project_id, scene)
        push_sync(task_id, {"type": "tts_done", "task_id": task_id,
                            "scene_id": scene_id, "audio_url": seg.audio_url,
                            "duration_sec": seg.duration_sec})
        push_sync(task_id, {"type": "complete", "task_id": task_id,
                            "summary": {"total": 1, "success": 1, "failed": 0}})
        return

    try:
        seg.status = "generating"
        seg.synced = False
        seg.error_message = None  # #392 — 재시도 시 옛 에러 메시지 초기화
        _persist_scene_segments(project_id, scene)
        # #tts-emotion — 씬에 지정된 말투 지시문을 함께 전달.
        duration = tts_synthesize(
            seg.text, str(wav_path), voice_name=effective_voice,
            emotion=(getattr(scene, "tts_emotion", "") or "").strip() or None,
        )
        seg.audio_url = _segment_audio_url(project_id, seg_id)
        seg.duration_sec = float(duration)
        seg.status = "ready"
        seg.synced = True
        seg.error_message = None  # 성공 시 None 리셋
        _persist_scene_segments(project_id, scene)
        # #301 비용 기록
        try:
            from ..cost_tracker import measure_phase
            with measure_phase(project_id) as _est:
                _est.record_tts(chars=len(seg.text), cpu_seconds=0)
        except Exception:
            pass
        push_sync(task_id, {"type": "tts_done", "task_id": task_id,
                            "scene_id": scene_id, "audio_url": seg.audio_url,
                            "duration_sec": duration})
        push_sync(task_id, {"type": "complete", "task_id": task_id,
                            "summary": {"total": 1, "success": 1, "failed": 0}})
    except Exception as e:
        # #392 — TTS 실패 원인 추적: traceback stderr 출력 + segment.error_message 저장.
        import traceback as _tb
        err_str = f"{type(e).__name__}: {e}"
        print(
            f"[tts:seg] 실패 scene={scene_id} seg={seg_id} voice={effective_voice!r} "
            f"err={err_str}",
            flush=True,
        )
        _tb.print_exc()
        seg.status = "error"
        seg.synced = False
        seg.error_message = err_str
        _persist_scene_segments(project_id, scene)
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "TTS_FAILED", "message": err_str,
                            "scene_id": scene_id})
        push_sync(task_id, {"type": "complete", "task_id": task_id,
                            "summary": {"total": 1, "success": 0, "failed": 1}})


@router.patch("/{project_id}/scenes/{scene_id}/narration")
async def update_narration(project_id: str, scene_id: int, req: UpdateNarrationRequest,
                           background_tasks: BackgroundTasks):
    """레거시 엔드포인트 — #382 이후 seg_0 편집으로 위임된다.

    seg_0 이 없으면 새로 만든다. narration_text/tts 필드도 호환을 위해 동기화한다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, "씬을 찾을 수 없습니다")

    seg_0 = next((s for s in (scene.narration_segments or []) if s.id == "seg_0"), None)
    if seg_0 is None:
        seg_0 = NarrationSegment(id="seg_0", text=req.narration_text, offset_sec=0.0)
        scene.narration_segments = [seg_0] + list(scene.narration_segments or [])
    else:
        if seg_0.text != req.narration_text:
            seg_0.text = req.narration_text
            seg_0.synced = False
    _persist_scene_segments(project_id, scene)

    # TTS 재생성 (백그라운드) — 씬 전체 segment 재생성 (force=True)
    task_id = create_task_id()

    async def _run():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_generate_tts,
                                   project_id, [scene_id], True, task_id)

    task = asyncio.create_task(_run())
    register_bg_task(task_id, task)

    return {"scene_id": scene_id, "tts_queued": True, "task_id": task_id}


def _wav_duration(path: Path) -> float:
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0
