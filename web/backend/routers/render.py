from __future__ import annotations
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

from ..models import BgmRenderRequest, RenderRequest
from .. import state as store
from ..speakers import resolve_segment_voice
from ..ws.progress import create_task_id, push_sync, register_bg_task

router = APIRouter(prefix="/api/projects", tags=["render"])

_render_status: dict[str, dict] = {}
# BGM 후처리 렌더 상태 (final_output → final_with_bgm). #289
_bgm_render_status: dict[str, dict] = {}


def _wav_duration(path: Path) -> float:
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


@router.post("/{project_id}/render")
async def render(project_id: str, req: RenderRequest):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)

    # footage가 있는 씬이 하나도 없으면 렌더 불가
    matched = [s for s in p.scenes if s.footage is not None]
    if not matched:
        raise HTTPException(400, "footage가 있는 씬이 없습니다")

    task_id = create_task_id()
    _render_status[project_id] = {"task_id": task_id, "phase": 0, "status": "running"}
    skip_subtitle = bool(getattr(req, "skip_subtitle", False))
    watermark = bool(getattr(req, "watermark", False))
    if watermark:
        # 워터마크 환경이 없으면 몇 분 렌더 후 마지막에 실패하지 않도록 시작 전에 거절한다.
        from src.watermark import available as _wm_available
        ok, why = _wm_available()
        if not ok:
            _render_status.pop(project_id, None)
            raise HTTPException(400, f"워터마크를 쓸 수 없습니다: {why}")

    async def _run():
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _do_render, project_id, task_id, skip_subtitle, watermark)
        except Exception as e:
            # 백그라운드 실행 중 예외는 silently 사라지지 않도록 status에 기록.
            import traceback
            tb = traceback.format_exc()
            print(f"[render] 예외로 종료: {e}\n{tb}", flush=True)
            _render_status[project_id] = {
                "task_id": task_id, "status": "error",
                "error": f"렌더링 중 예외: {e}",
            }
            try:
                push_sync(task_id, {"type": "error", "task_id": task_id,
                                    "code": "RENDER_EXCEPTION",
                                    "message": f"렌더링 중 예외: {e}"})
            except Exception:
                pass

    task = asyncio.create_task(_run())
    register_bg_task(task_id, task)
    return {"task_id": task_id}


@router.get("/{project_id}/render/status")
def render_status(project_id: str):
    s = dict(_render_status.get(project_id, {"task_id": None, "phase": 0, "status": "idle"}))
    # SBV 파일 존재 여부 — 프론트가 자막 다운로드 링크 노출을 결정.
    sbv_path = store.DATA_DIR / project_id / "output" / "final_output.sbv"
    s["has_subtitle"] = sbv_path.exists() and sbv_path.stat().st_size > 0
    return s


@router.get("/{project_id}/render/output-info")
def render_output_info(project_id: str):
    """현재 디스크에 final_output.mp4가 있는지, 인메모리 status가 어떤지 메타만 반환.
    프론트가 BGM 진입 가능 여부를 결정할 때 사용."""
    disk_path = store.DATA_DIR / project_id / "output" / "final_output.mp4"
    in_memory = _render_status.get(project_id, {})
    exists = disk_path.exists() and disk_path.stat().st_size > 0
    # #bgm-preview-cache — 브라우저가 옛 영상을 캐시해 BGM 편집 화면에 이전 버전이
    # 나오던 문제(사용자 보고). 프론트가 이 값을 URL 쿼리로 붙여 캐시를 깬다.
    return {
        "has_final_mp4": exists,
        "status": in_memory.get("status", "idle"),
        "mtime": int(disk_path.stat().st_mtime) if exists else 0,
        "size": disk_path.stat().st_size if exists else 0,
    }


@router.post("/{project_id}/render/output-upload")
async def render_output_upload(project_id: str, file: UploadFile = File(...)):
    """사용자가 직접 mp4를 업로드해 BGM 입히기 입력으로 사용.

    저장 경로: web/data/{id}/output/final_output.mp4 (기존 렌더 결과를 덮어쓴다).
    """
    out_dir = store.DATA_DIR / project_id / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "final_output.mp4"
    content = await file.read()
    if not content:
        raise HTTPException(400, "빈 파일입니다")
    dst.write_bytes(content)
    # 인메모리 status도 동기화해 다른 API에서 done 취급되도록.
    _render_status[project_id] = {
        "task_id": None,
        "phase": 6,
        "status": "done",
        "output_file": str(dst),
    }
    # #301 — 사용자 업로드 mp4 저장 비용
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_transcode(
                cpu_seconds=0,
                output_bytes=dst.stat().st_size,
                phase_label="15_output_upload",
            )
    except Exception as _err:
        print(f"[cost] output_upload 기록 실패: {_err}")
    return {"ok": True, "size": dst.stat().st_size, "path": str(dst)}


@router.get("/{project_id}/render/output")
def render_output(project_id: str):
    """렌더링 결과 mp4 스트리밍."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    # 1) 인메모리 status
    s = _render_status.get(project_id, {})
    if s.get("status") == "done":
        output_file = Path(s.get("output_file", ""))
        if output_file.exists() and output_file.stat().st_size > 0:
            _record_egress(project_id, output_file.stat().st_size)
            return FileResponse(str(output_file), media_type="video/mp4", filename="final_output.mp4")
    # 2) 디스크 fallback
    disk_path = store.DATA_DIR / project_id / "output" / "final_output.mp4"
    if disk_path.exists() and disk_path.stat().st_size > 0:
        _record_egress(project_id, disk_path.stat().st_size)
        return FileResponse(str(disk_path), media_type="video/mp4", filename="final_output.mp4")
    raise HTTPException(404, "렌더링된 영상이 없습니다")


def _record_egress(project_id: str, bytes_size: int) -> None:
    """#301 — egress 비용 기록 헬퍼. R2면 $0이지만 사용량은 추적."""
    if bytes_size <= 0:
        return
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_egress(bytes_size=bytes_size)
    except Exception as _err:
        print(f"[cost] egress 기록 실패: {_err}")


@router.get("/{project_id}/render/subtitle")
def render_subtitle(project_id: str):
    """렌더링 결과의 final_output.sbv 다운로드. clip_editor가 자동 생성한 파일을 반환."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    sbv_path = store.DATA_DIR / project_id / "output" / "final_output.sbv"
    if not sbv_path.exists() or sbv_path.stat().st_size == 0:
        raise HTTPException(404, "자막 파일이 없습니다 (먼저 렌더링하세요)")
    _record_egress(project_id, sbv_path.stat().st_size)
    return FileResponse(str(sbv_path), media_type="text/plain", filename="final_output.sbv")


@router.post("/{project_id}/render/subtitle")
def generate_subtitle_only(project_id: str):
    """자막만 빠르게 생성 — 영상 렌더링 없이 TTS 길이 누적으로 SBV 생성.

    동작:
      1) 각 씬의 subtitle_text(없으면 narration_text)와 tts.duration_sec을 수집.
      2) TTS 길이를 누적해 임시 timeline 만들고 SBV 작성.
      3) 결과 파일을 반환(다운로드 헤더 포함).
    영상 렌더링 결과(final_output.sbv)와는 별도로 quick_subtitle.sbv 파일로 출력해
    렌더 결과를 덮어쓰지 않는다.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    # #382 — segment 단위로 자막을 만든다. 동일 씬 내 segment 들은 offset_sec 기준으로 정렬.
    # 씬 단위 visual 길이 = max(seg_0.duration, duration_hint_sec, 1.0). 다음 씬 시작은 이전 씬 길이 누적.
    cursor = 0.0
    entries: list[tuple[float, float, str]] = []
    for scene in sorted(p.scenes, key=lambda s: s.scene_id):
        segs_all = list(scene.narration_segments or [])
        # 빈 narration_segments + narration_text 폴백 (마이그레이션 누락 대비).
        if not segs_all and scene.narration_text and scene.narration_text.strip():
            dur = float(scene.tts.duration_sec) if scene.tts and scene.tts.duration_sec else 3.0
            st_raw = getattr(scene, "subtitle_text", None)
            sub_text = st_raw.strip() if isinstance(st_raw, str) and st_raw.strip() else scene.narration_text.strip()
            entries.append((cursor, cursor + dur, sub_text))
            cursor += dur
            continue

        seg_0 = next((s for s in segs_all if s.id == "seg_0"), None)
        tts0 = float(seg_0.duration_sec) if seg_0 else 0.0
        base = float(getattr(scene, "duration_hint_sec", 0) or 0)
        # #412 — 모든 활성 segment 끝점 포함 (자막 잘림 회귀 차단).
        seg_max_end = 0.0
        for _s in segs_all:
            _txt = getattr(_s, "text", None)
            if not (isinstance(_txt, str) and _txt.strip()):
                continue
            _dur = float(_s.duration_sec or 0.0)
            if _dur <= 0:
                continue
            _end = float(_s.offset_sec or 0.0) + _dur
            if _end > seg_max_end:
                seg_max_end = _end
        scene_dur = max(tts0, base, seg_max_end, 1.0)

        active = [s for s in segs_all if s.text and s.text.strip()]
        if not active:
            cursor += scene_dur
            continue
        # segment 단위 자막 entry — offset_sec 정렬, 넘침은 씬 경계에서 자름.
        for seg in sorted(active, key=lambda s: s.offset_sec):
            seg_start = cursor + float(seg.offset_sec or 0.0)
            seg_end = seg_start + float(seg.duration_sec or 0.0)
            scene_end = cursor + scene_dur
            if seg_end > scene_end:
                seg_end = scene_end
            if seg_end <= seg_start:
                continue
            txt = seg.sbv_text if isinstance(seg.sbv_text, str) and seg.sbv_text.strip() else seg.text.strip()
            entries.append((seg_start, seg_end, txt))
        cursor += scene_dur

    if not entries:
        raise HTTPException(400, "자막을 만들 씬이 없습니다 (나레이션을 먼저 입력하세요)")

    def fmt_time(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h}:{m:02d}:{s:06.3f}"

    # SBV 텍스트 작성 — 한 entry 안에서 '.' 기준 줄바꿈으로 가독성 향상.
    lines: list[str] = []
    import re as _re
    for start, end, text in entries:
        # 마침표·물음표·느낌표·말줄임 등 종결 부호 뒤에서 줄바꿈.
        # 부호는 유지(자막에 그대로 보이도록).
        parts = _re.split(r"(?<=[\.!\?]) +", text)
        parts = [p.strip() for p in parts if p.strip()]
        body = "\n".join(parts) if parts else text
        lines.append(f"{fmt_time(start)},{fmt_time(end)}")
        lines.append(body)
        lines.append("")

    out_dir = store.DATA_DIR / project_id / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    sbv_path = out_dir / "quick_subtitle.sbv"
    sbv_path.write_text("\n".join(lines), encoding="utf-8")
    _record_egress(project_id, sbv_path.stat().st_size)
    return FileResponse(str(sbv_path), media_type="text/plain", filename="quick_subtitle.sbv")


# ── #289 — 후처리 BGM 렌더링 ─────────────────────────────────────────────
# 렌더 완료된 final_output.mp4를 입력으로 받아 Project.bgm_tracks를 ffmpeg로 합성한다.
# 출력: output/final_with_bgm.mp4. 기존 영상 오디오(나레이션·SFX)는 그대로 유지.

def _do_bgm_render(project_id: str, task_id: str):
    """final_output.mp4 + bgm_tracks → final_with_bgm.mp4.

    BGM 합성은 final_output.mp4의 비디오 트랙을 `-c:v copy`로 그대로 가져가므로,
    사용자가 신 에디터에서 자막/오버레이를 수정한 뒤 BGM 렌더만 누르면 옛 영상의
    옛 자막/오버레이가 그대로 final_with_bgm.mp4에 박혀 나간다. 이를 막기 위해
    BGM 렌더 진입 시 본 렌더(_do_render)를 먼저 무조건 실행해 final_output.mp4를
    최신 상태로 갱신한 뒤 BGM 합성을 진행한다.
    """
    p = store.get_project(project_id)
    if not p:
        _log("BGM 렌더: 프로젝트를 찾을 수 없음")
        return

    tracks = p.bgm_tracks or []
    if not tracks:
        _bgm_render_status[project_id] = {
            "task_id": task_id, "status": "error",
            "error": "BGM 트랙이 없습니다.",
        }
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "NO_BGM_TRACKS",
                            "message": "BGM 트랙이 없습니다."})
        return

    # ── 본 렌더 선행 실행 — 최신 자막/오버레이가 final_output.mp4에 박히도록 ──
    push_sync(task_id, {"type": "progress", "task_id": task_id,
                        "phase": "bgm_render", "current": 0, "total": 2,
                        "message": "최신 영상 렌더링 중 (BGM 합성 전)...", "item": ""})
    # #bgm-reuse-final — 이미 최신 final_output.mp4 가 있으면 본 렌더를 건너뛴다.
    #
    # 원래는 무조건 _do_render 를 돌려 자막/오버레이 수정분을 반영했다. 그런데
    # 본 렌더는 시작 시 clips/ 와 final_output.mp4 를 통째로 지우므로, 이미 완성된
    # 영상이 있어도 매번 처음부터 다시 만든다. 렌더 경로에 회귀가 있으면 멀쩡하던
    # 결과물까지 같이 망가진다(실사고: 589.7s 완성본 → 467s/139s 로 잘림).
    #
    # `.bgm_reuse_final` 마커가 있으면 기존 final_output.mp4 를 그대로 쓴다.
    # 마커는 BGM 합성 후 삭제하지 않는다 — 씬을 편집하면 프론트가 지우거나,
    # 사용자가 본 렌더를 직접 눌러 갱신하면 된다.
    output_dir_pre = store.get_output_dir(project_id)
    _final_pre = output_dir_pre / "final_output.mp4"
    _reuse_marker = output_dir_pre / ".bgm_reuse_final"
    _reuse = (
        _reuse_marker.exists()
        and _final_pre.exists()
        and _final_pre.stat().st_size > 0
    )
    # #bgm-stale-final — 마커가 있어도 "final_output.mp4 가 최신" 일 때만 재사용한다.
    #
    # 마커만 보고 재사용하면, 그 뒤에 씬을 고쳐 클립을 다시 만들어도 BGM 은 옛 영상
    # 위에 얹힌다(사용자 보고: "가장 마지막에 렌더링한 영상 기준이 아니다").
    # 씬 클립(clips/*.mp4) 중 하나라도 final_output 보다 새로우면 본 렌더를 다시 돌린다.
    if _reuse:
        try:
            _final_mtime = _final_pre.stat().st_mtime
            _clips_dir = output_dir_pre / "clips"
            _newer = [
                c.name for c in _clips_dir.glob("scene_*.mp4")
                if c.stat().st_mtime > _final_mtime + 1.0
            ]
            # 씬 편집(자막/효과/트림)은 클립을 다시 만들지 않아도 state.json 만 바뀐다.
            # 그 편집도 BGM 결과에 반영되어야 하므로 state.json 시각도 함께 본다.
            _state = store.DATA_DIR / project_id / "state.json"
            _state_newer = (
                _state.exists() and _state.stat().st_mtime > _final_mtime + 1.0
            )
            if _newer or _state_newer:
                _reuse = False
                _why = []
                if _newer:
                    _why.append(f"클립 {len(_newer)}개")
                if _state_newer:
                    _why.append("씬 편집(state.json)")
                _log(f"BGM 렌더: final_output.mp4 가 오래됨 — {' / '.join(_why)} 이 더 최신. "
                     f"본 렌더를 다시 실행합니다.")
        except OSError:
            _reuse = False
    if _reuse:
        _log(f"BGM 렌더: 기존 final_output.mp4 재사용 (본 렌더 생략) — {_final_pre}")
        push_sync(task_id, {"type": "progress", "task_id": task_id,
                            "phase": "bgm_render", "current": 1, "total": 2,
                            "message": "기존 영상 재사용 — BGM 합성만 진행합니다",
                            "item": ""})
    else:
        _do_render(project_id, task_id, skip_subtitle=False)

    # 본 렌더가 실패하면 final_output.mp4가 없거나 옛 상태일 수 있다.
    output_dir = store.get_output_dir(project_id)
    final_input = output_dir / "final_output.mp4"
    if not final_input.exists():
        _bgm_render_status[project_id] = {
            "task_id": task_id, "status": "error",
            "error": "본 렌더 실패로 final_output.mp4 가 생성되지 않았습니다.",
        }
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "NO_FINAL_OUTPUT",
                            "message": "본 렌더 실패로 final_output.mp4 가 없습니다."})
        return

    # 본 렌더 직후 store에서 최신 프로젝트 다시 로드 (bgm_tracks 등이 갱신됐을 수 있음).
    p = store.get_project(project_id) or p
    footage_dir = Path(p.footage_dir) if p.footage_dir else None
    asset_by_id = {a.asset_id: a for a in p.studio_assets}

    # ── ffmpeg 입력/필터 구성 ──────────────────────────────────────────
    inputs: list[str] = ["-i", str(final_input)]
    filter_parts: list[str] = []
    mix_labels: list[str] = ["[0:a]"]  # 기존 final_output의 오디오 트랙
    bgm_idx = 1
    used = 0

    push_sync(task_id, {"type": "progress", "task_id": task_id,
                        "phase": "bgm_render", "current": 0, "total": 2,
                        "message": f"BGM {len(tracks)}개 트랙 합성 준비...", "item": ""})

    for tr in tracks:
        # audio_asset_id = studio_assets uuid(프로젝트에 등록한 오디오). footage_dir 필요.
        # (#419 — 외부 공용 BGM 라이브러리 참조는 제거. 기존 트랙은 등록 오디오로 이전 완료.)
        asset = asset_by_id.get(tr.audio_asset_id)
        if not asset or asset.asset_type != "audio" or not footage_dir:
            continue
        src: Path = footage_dir / asset.filename
        if not src.exists():
            continue
        track_duration = max(0.0, float(tr.end_sec) - float(tr.start_sec))
        if track_duration <= 0.05:
            continue

        # ⚠ BGM 원본 길이 측정 — 트랙 길이가 원본을 넘으면 ffmpeg 가 마지막 패킷을 반복하며
        # "버퍼링 같은 이상한 음" 회귀가 발생한다. 원본 길이로 atrim 을 클램프해 자연스럽게 끊는다
        # (트랙 끝까지의 나머지는 무음). 측정 실패 시 트랙 길이 그대로 사용(=옛 동작 폴백).
        try:
            import subprocess as _sp
            _pr = _sp.run(
                [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(src)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            _src_dur_str = (_pr.stdout or "").strip()
            src_dur = float(_src_dur_str) if _src_dur_str and _src_dur_str not in ("N/A",) else None
        except Exception:
            src_dur = None
        effective_duration = (
            min(track_duration, src_dur) if src_dur and src_dur > 0 else track_duration
        )

        delay_ms = max(0, int(float(tr.start_sec) * 1000))
        fade_in = max(0.0, float(tr.fade_in_sec))
        fade_out = max(0.0, float(tr.fade_out_sec))
        # 페이드 길이는 *유효* 트랙 길이를 넘지 않도록
        fade_in = min(fade_in, effective_duration / 2)
        fade_out = min(fade_out, effective_duration / 2)
        fade_out_start = max(0.0, effective_duration - fade_out)
        vol = max(0.0, min(1.0, float(tr.volume)))

        inputs += ["-i", str(src)]
        # atrim 0~effective_duration → fade in/out → volume → adelay
        chain = (
            f"[{bgm_idx}:a]atrim=0:{effective_duration:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade_in:.3f},"
            f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f},"
            f"volume={vol:.3f},adelay={delay_ms}|{delay_ms}[bgm{used}]"
        )
        filter_parts.append(chain)
        mix_labels.append(f"[bgm{used}]")
        used += 1
        bgm_idx += 1

    if used == 0:
        _bgm_render_status[project_id] = {
            "task_id": task_id, "status": "error",
            "error": "유효한 BGM 트랙이 없습니다 (자산 파일 누락 또는 길이 0).",
        }
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "NO_VALID_BGM",
                            "message": "유효한 BGM 트랙이 없습니다."})
        return

    # ── amix 옵션 설명 ──────────────────────────────────────────────────────
    # duration=first      : final_output 오디오 길이로 출력 자름. BGM 이 더 짧아도 OK.
    # normalize=0         : 입력 수로 1/N 자동 감쇠 비활성 — 각 트랙의 volume= 슬라이더가 그대로 반영.
    # dropout_transition=0: ⚠ 트랙이 시작/끝날 때 (=BGM 길이가 영상보다 짧음)
    #                      amix 가 default 2초간 다른 트랙을 일시적으로 증폭하는 동작 차단.
    #                      이 동작이 있으면 BGM 시작/끝 부근에서 본문 오디오까지 볼륨이
    #                      "늘었다 줄었다" 들리는 회귀 원인. 0 으로 명시해 본문 오디오는
    #                      항상 동일 음량 유지.
    mix_inputs = "".join(mix_labels)
    filter_parts.append(
        f"{mix_inputs}amix=inputs={used + 1}:duration=first:normalize=0:dropout_transition=0[aout]"
    )
    filter_complex = ";".join(filter_parts)

    output_path = output_dir / "final_with_bgm.mp4"
    cmd = [
        ffmpeg_bin(), "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]
    push_sync(task_id, {"type": "progress", "task_id": task_id,
                        "phase": "bgm_render", "current": 1, "total": 2,
                        "message": f"ffmpeg 실행 중 ({used}개 트랙)...", "item": ""})
    _log(f"BGM 렌더 실행: {used}개 트랙 → {output_path}")
    import time as _t_bgm
    _bgm_t0 = _t_bgm.monotonic()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    _bgm_cpu = _t_bgm.monotonic() - _bgm_t0
    if r.returncode != 0 or not output_path.exists():
        _log(f"BGM 렌더 실패: {r.stderr[-2000:]}")
        _bgm_render_status[project_id] = {
            "task_id": task_id, "status": "error",
            "error": f"ffmpeg 실패: {r.stderr[-1000:]}",
        }
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "FFMPEG_FAILED",
                            "message": f"ffmpeg 실패: {r.stderr[-500:]}"})
        return

    _bgm_render_status[project_id] = {
        "task_id": task_id, "status": "done",
        "output_file": str(output_path),
    }

    # #301 — BGM 렌더 비용 기록
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_bgm_render(
                cpu_seconds=_bgm_cpu,
                output_bytes=output_path.stat().st_size if output_path.exists() else 0,
            )
    except Exception as _err:
        print(f"[cost] bgm_render 기록 실패: {_err}")

    push_sync(task_id, {"type": "complete", "task_id": task_id,
                        "summary": {"total": used, "success": used, "failed": 0}})


@router.post("/{project_id}/render-bgm")
async def render_bgm(project_id: str, req: BgmRenderRequest):
    """후처리 BGM 합성 시작 (#289). final_output.mp4 → final_with_bgm.mp4."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    if not (p.bgm_tracks or []):
        raise HTTPException(400, "BGM 트랙이 없습니다")

    task_id = create_task_id()
    _bgm_render_status[project_id] = {"task_id": task_id, "status": "running"}

    async def _run():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_bgm_render, project_id, task_id)

    task = asyncio.create_task(_run())
    register_bg_task(task_id, task)
    return {"task_id": task_id}


@router.get("/{project_id}/render-bgm/status")
def render_bgm_status(project_id: str):
    s = _bgm_render_status.get(project_id, {"task_id": None, "status": "idle"})
    return s


@router.get("/{project_id}/render-bgm/output")
def render_bgm_output(project_id: str):
    """final_with_bgm.mp4 스트리밍."""
    from fastapi.responses import FileResponse
    output_dir = store.get_output_dir(project_id)
    output_path = output_dir / "final_with_bgm.mp4"
    if not output_path.exists():
        raise HTTPException(404, "final_with_bgm.mp4 이 없습니다")
    _record_egress(project_id, output_path.stat().st_size)
    return FileResponse(str(output_path), media_type="video/mp4", filename="final_with_bgm.mp4")


def _log(msg: str) -> None:
    import sys
    print(f"[render] {msg}", flush=True, file=sys.stderr)


def _do_render(project_id: str, task_id: str, skip_subtitle: bool = False, watermark: bool = False):
    """웹 프로젝트 상태를 기반으로 기존 파이프라인 실행.

    TTS WAV가 모든 씬에 대해 이미 존재하면 Phase 4(TTS 생성)를 건너뛰고
    Phase 5(영상 편집)부터 실행한다.
    """
    import json
    import os

    # 자막 생성 스킵 옵션을 환경변수로 clip_editor에 전달.
    # clip_editor가 cwd 변경 후 sbv_generator.run을 부르는 위치에서 분기.
    if skip_subtitle:
        os.environ["VAG_SKIP_SUBTITLE"] = "1"
    else:
        os.environ.pop("VAG_SKIP_SUBTITLE", None)

    _log(f"시작 — project_id={project_id}")
    p = store.get_project(project_id)
    if not p:
        _log("프로젝트를 찾을 수 없음")
        return

    cache_dir = store.get_cache_dir(project_id)
    output_dir = store.get_output_dir(project_id)
    narr_dir = output_dir / "narration"
    narr_dir.mkdir(parents=True, exist_ok=True)

    # 매 렌더링 시작 시 캐시 파일 청소 — 사용자가 다시 ▶ 렌더링을 누르면 새로 만들어져야 한다.
    # clip_editor가 force=True여도 muted_output.mp4가 있으면 어디선가 캐시 검사 분기를 탈 수 있고,
    # 옛 final_output.mp4가 그대로 남아 있으면 다운로드 시점에 옛 결과로 잘못 보일 수 있다.
    # 안전망으로 시작 시 명시 삭제.
    for stale_name in ("muted_output.mp4", "final_output.mp4", "final_output.sbv", "final_with_bgm.mp4"):
        stale = output_dir / stale_name
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass
    # 씬별 cut 캐시도 모두 제거 (overlay/필터 변경 반영).
    clips_dir = output_dir / "clips"
    if clips_dir.exists():
        import shutil as _shutil_clips
        try:
            _shutil_clips.rmtree(clips_dir)
        except OSError:
            pass

    # ── Step 1: clip_selection.json 생성 ──────────────────────────────────
    _log("Step 1: clip_selection.json 생성 중...")
    push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "render",
                        "current": 0, "total": 5, "message": "clip_selection.json 생성 중...", "item": ""})

    footage_dir = Path(p.footage_dir) if p.footage_dir else store.get_output_dir(project_id).parent / "footage"

    def _footage_duration(filename: str) -> float:
        """ffprobe로 영상 길이 조회. 실패 시 0 반환."""
        import subprocess
        from pathlib import Path as _P_dur
        # 절대경로면 그대로, 아니면 footage_dir 기준으로 해석.
        _fp = _P_dur(filename)
        fpath = _fp if _fp.is_absolute() else (footage_dir / filename)
        if not fpath.exists():
            return 0.0
        try:
            r = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "format=duration", "-of", "csv=p=0", str(fpath)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            val = r.stdout.strip()
            if val and val not in ("N/A", ""):
                return float(val)
            # webm 등 duration N/A 폴백
            r2 = subprocess.run(
                [ffprobe_bin(), "-select_streams", "v:0",
                 "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(fpath)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            lines = [l.strip() for l in r2.stdout.strip().splitlines() if l.strip()]
            if lines:
                return float(lines[-1].rstrip(","))
        except Exception:
            pass
        return 0.0

    # studio_assets 에서 asset_id → 파일경로 lookup 테이블 (이미지 오버레이 해석용).
    # 빈 prefix/footage_dir 미존재 환경에서도 안전하게 동작하도록 일관된 빈 dict 폴백.
    _image_asset_by_id: dict[str, str] = {}
    if footage_dir and footage_dir.exists():
        for _a in (p.studio_assets or []):
            if getattr(_a, "asset_type", "") == "image":
                _abs = footage_dir / _a.filename
                if _abs.exists():
                    _image_asset_by_id[_a.asset_id] = str(_abs)

    def _build_image_overlay_payloads(scene) -> list[dict]:
        """씬의 image_overlays 를 clip_editor 가 기대하는 dict 리스트로 변환.

        - 각 항목에 ``_image_path`` 절대 경로 키를 주입 (asset_id 해석 결과).
        - 자산을 찾지 못한 항목은 스킵 (조용히 제거 — 깨진 참조 때문에 렌더가 실패하지 않도록).
        """
        out: list[dict] = []
        for ov in (getattr(scene, "image_overlays", None) or []):
            asset_id = getattr(ov, "image_asset_id", None)
            if not asset_id:
                continue
            img_path = _image_asset_by_id.get(asset_id)
            if not img_path:
                continue
            d = ov.model_dump()
            d["_image_path"] = img_path
            out.append(d)
        return out

    # footage 파일 경로 해석 — basename 만 들어오면 프로젝트 footage_dir 기준으로 절대경로로 변환.
    # 이유: clip_editor 는 CWD(=web/data/{project_id})/footage 만 찾는데, footage_dir 가 다른 프로젝트
    # 폴더를 공유하는 경우(복제 프로젝트) 같은 이름 파일이 없어 거의 모든 씬이 스킵된다 → 최종 영상 깨짐.
    # 절대경로면 그대로 통과시킨다.
    from pathlib import Path as _P_norm
    _footage_root = _P_norm(p.footage_dir).resolve() if getattr(p, "footage_dir", None) else None

    def _resolve_source_file(fname: str | None) -> str | None:
        if not fname:
            return fname
        fp = _P_norm(fname)
        if fp.is_absolute():
            return fname
        if _footage_root is not None:
            cand = _footage_root / fname
            if cand.exists():
                return str(cand)
        return fname

    def _build_sel(scene, source_file: str, trim_start: float, trim_end: float) -> dict:
        """clip_editor.py가 기대하는 형식으로 selection 딕셔너리 생성."""
        # source_file 을 절대경로로 normalize — footage_dir 공유 프로젝트에서도 안전.
        source_file = _resolve_source_file(source_file) or source_file
        # #412 — 씬 길이 계산은 모든 활성 segment 의 끝점(offset + duration) 최대값을 포함.
        #   기존 #382 정책 (seg_0 만 자동 연장) 은 사용자 의도와 달랐음 — 사용자가 추가한 TTS
        #   (seg_1+) 도 영상에 끝까지 들어가야 한다는 의도. 그렇지 않으면 영상이 seg_0 의 짧은
        #   길이에 맞춰 잘림.
        segs = list(scene.narration_segments or [])
        if not segs and scene.narration_text:
            # 마이그레이션 누락 폴백 — narration_text 가 있으면 seg_0 가 있다고 가정한다.
            tts_dur = float(scene.tts.duration_sec) if scene.tts else 0.0
        else:
            # 모든 활성 segment (text 있고 duration > 0) 의 끝점 최대값.
            tts_dur = 0.0
            for _s in segs:
                _txt = getattr(_s, "text", None)
                if not (isinstance(_txt, str) and _txt.strip()):
                    continue
                _dur = float(_s.duration_sec or 0.0)
                if _dur <= 0:
                    continue
                _end = float(_s.offset_sec or 0.0) + _dur
                if _end > tts_dur:
                    tts_dur = _end
        # duration_hint_sec 은 "씬 길이 하한"이다. 나레이션이 끝난 뒤에도 화면이 유지돼야
        # 하는 컷(펀치라인 자막, 문서 스크롤 등)은 hint 로 길이를 늘려 쓴다.
        # 이 값을 빼먹으면 clip_editor 의 `clip_duration = tts_dur or ...` 가 TTS 길이만
        # 보게 되어, 씬이 hint 보다 짧게 잘리고 오디오 트랙도 그만큼 짧아진다.
        # (실측 회귀: 씬3 hint 5.5s / TTS 2.8s → 렌더 결과 3.57s 로 잘림.)
        _hint = float(getattr(scene, "duration_hint_sec", 0) or 0)
        tts_dur = max(tts_dur, _hint)
        clip_dur = max(trim_end - trim_start, tts_dur, 1.0)
        footage_type = scene.footage.type  # type: ignore[union-attr]
        footage_speed = scene.footage_speed if hasattr(scene, "footage_speed") and scene.footage_speed else 1.0
        # 분할 씬 여부 — footage에 clip_start_sec / clip_end_sec이 명시되어 있으면 True.
        # clip_editor가 group_offsets를 무시하고 trim_start/trim_end를 그대로 사용해야 한다.
        cs_explicit = getattr(scene.footage, "clip_start_sec", None)
        ce_explicit = getattr(scene.footage, "clip_end_sec", None)
        has_explicit_trim = (cs_explicit is not None) or (ce_explicit is not None)
        base = {
            "scene_id": scene.scene_id,
            "source_file": source_file,
            "trim_start": trim_start,
            "trim_end": trim_end,
            "explicit_trim": has_explicit_trim,
            "clip_duration_sec": clip_dur,
            "tts_duration_sec": tts_dur,
            "effect": footage_type,
            "text_overlays": [ov.model_dump() for ov in (scene.text_overlays or [])],
            "shape_overlays": [ov.model_dump() for ov in (scene.shape_overlays or [])],
            "graphic_overlays": [
                ov.model_dump() for ov in (getattr(scene, "graphic_overlays", None) or [])
            ],
            # 씬 필터 (#206) — flashback/horror/vivid/nightvision/pov
            "filters": [
                f.model_dump() for f in (getattr(scene, "filters", None) or [])
            ],
            # 씬 모션 (#207) — shake / clip_zoom / slowmo
            # 1차 구현: shake + clip_zoom 렌더링 (영상 길이 변경 없음). slowmo는 후속 사이클.
            "motions": [
                m.model_dump() for m in (getattr(scene, "motions", None) or [])
            ],
            # 모자이크 오버레이 (#264) — rect/ellipse, pixelate/blur. ellipse는 1차 rect 폴백.
            # _build_sel에서 누락되면 clip_editor 메인 흐름에서 sel.get("mosaic_overlays", []) → 빈 배열로 처리되어
            # 모든 모자이크 데이터가 사라지므로 반드시 포함해야 한다 (회귀 방지).
            "mosaic_overlays": [
                mo.model_dump() for mo in (getattr(scene, "mosaic_overlays", None) or [])
            ],
            # 이미지 오버레이 — image_asset_id를 footage_dir 내 실제 파일 경로로 해석하여
            # `_image_path` 키에 주입한다. clip_editor.apply_image_overlays 는 해당 키를
            # 입력으로 PIL 합성에 사용한다. 자산을 찾지 못하면 해당 overlay 는 스킵된다.
            "image_overlays": _build_image_overlay_payloads(scene),
            # 집중선(속도선) 오버레이 — 만화 박진감. clip_editor 가 파라미터+해상도 해시
            # 캐시로 Pillow PNG 를 생성/재사용한다(src.speed_lines). 누락하면 sel.get 폴백으로
            # 빈 배열이 되어 데이터가 사라지므로 반드시 포함(회귀 방지).
            "speed_lines": [
                sl.model_dump() for sl in (getattr(scene, "speed_lines", None) or [])
            ],
            "footage_speed": footage_speed,
            # 재생 속도 모드 — constant(기본)/ramp. clip_editor 가 setpts/atempo 분기에 사용.
            "footage_speed_mode": getattr(scene, "footage_speed_mode", "constant") or "constant",
            "footage_speed_ramp_hold": float(getattr(scene, "footage_speed_ramp_hold", 0.2) or 0.0),
            "footage_speed_ramp_accel": float(getattr(scene, "footage_speed_ramp_accel", 0.3) or 0.0),
            # ramp 기본 배속(가속의 시작 / 감속의 끝 속도). 기본 1.0.
            "footage_speed_ramp_base": float(getattr(scene, "footage_speed_ramp_base", 1.0) or 1.0),
            # 씬 footage 원본 오디오 음량 — 0=음소거, 0.3=기본 더킹, 1.0=풀.
            # 누락하면 clip_editor 가 기본 0.3 으로 폴백해 사용자 음소거(0) 설정이 무시된다.
            "footage_audio_volume": float(
                getattr(scene, "footage_audio_volume", 0.3)
                if getattr(scene, "footage_audio_volume", None) is not None
                else 0.3
            ),
            # TTS 재생 속도 — Chirp3-HD 는 API speakingRate 를 무시하므로 렌더 시점에
            # 각 segment wav 에 atempo 필터로 후처리한다. 실제 처리는
            # `narration_merge.build_merged_wav` 가 담당(scene.tts_speed 참조).
            # 이 값은 파이프라인 하류(clip_editor) 에서 추가 사용 시 참조 가능하도록
            # sel dict 에 함께 실어 두지만, 현재는 merged wav 가 이미 속도 반영된 상태로
            # 넘어가므로 clip_editor 내부에서 별도 처리는 하지 않는다 (회귀 안전).
            "tts_speed": float(
                getattr(scene, "tts_speed", 1.0)
                if getattr(scene, "tts_speed", None) is not None
                else 1.0
            ),
            "x_offset": float(getattr(scene.footage, "x_offset", 0) or 0),
            "y_offset": float(getattr(scene.footage, "y_offset", 0) or 0),
            # #258 fit 모드 (contain/cover/stretch). 기존 프로젝트는 contain.
            "fit": (
                getattr(scene.footage, "fit", "contain") or "contain"
            ) if getattr(scene.footage, "fit", "contain") in ("contain", "cover", "stretch") else "contain",
            # #263 수동 회전 (0/90/180/270). 잘못된 값은 0으로 폴백. 기존 프로젝트는 0.
            "rotation": (
                int(getattr(scene.footage, "rotation", 0) or 0)
                if int(getattr(scene.footage, "rotation", 0) or 0) in (0, 90, 180, 270)
                else 0
            ),
        }
        if footage_type == "multiscreen":
            # N×M 그리드 확장 (#grid-compare) — rows/cols/cells 가 있으면 grid_nxm 경로.
            ms_rows = getattr(scene.footage, "rows", None)
            ms_cols = getattr(scene.footage, "cols", None)
            ms_cells = getattr(scene.footage, "cells", None)
            ms_files = list(getattr(scene.footage, "files", []) or [])
            if ms_rows and ms_cols and ms_cells:
                # 셀 목록 → multiscreen_clips + border 메타. file 이 빈 셀도 슬롯 유지(빈 슬롯).
                clips = []
                for c in ms_cells:
                    cfile = getattr(c, "file", "") or ""
                    resolved = _resolve_source_file(cfile) or cfile if cfile else ""
                    border = getattr(c, "border", None)
                    clips.append({
                        "source_file": resolved,
                        "border": border.model_dump() if border is not None else None,
                    })
                base["multiscreen_clips"] = clips
                base["layout"] = "grid_nxm"
                base["grid_rows"] = int(ms_rows)
                base["grid_cols"] = int(ms_cols)
            else:
                # 기존 files 기반 멀티스크린 (auto/grid2x2/triptych 등) — 회귀 보존.
                clips = []
                for f in ms_files:
                    resolved = _resolve_source_file(f) or f
                    clips.append({"source_file": resolved})
                base["multiscreen_clips"] = clips
                base["layout"] = getattr(scene.footage, "layout", "auto") or "auto"
            return base
        if footage_type in ("kenburns", "kenburns_copy_img"):
            img = getattr(scene.footage, "image", source_file)
            # kenburns 이미지도 footage_dir 기준 절대경로로 변환 — 폴더 공유 프로젝트 대응.
            img = _resolve_source_file(img) or img
            base["kenburns_image"] = img
            if footage_type == "kenburns_copy_img":
                cop = getattr(scene.footage, "copy_img_options", None)
                base["copy_img_options"] = cop.model_dump() if cop else {}
        elif footage_type == "title_gen":
            opts = getattr(scene.footage, "options", None)
            base["title_gen_options"] = opts.model_dump() if opts else {}
        return base

    selections = []
    # scene_id 오름차순 보장 — register/append 흐름에서 순서가 어긋난 인메모리 상태가 있어도
    # 렌더링 자체는 항상 scene_id 순으로 진행되어 영상·텍스트·오디오 정렬이 보장된다.
    scenes_sorted = sorted(p.scenes, key=lambda s: s.scene_id)
    for scene in scenes_sorted:
        if scene.footage is None:
            continue
        # footage/match 동기화 가드 — footage 교체 후 match가 옛 source_file을 가리키면
        # 렌더가 미리보기와 다른 영상을 깔게 된다. 기본 정책: footage.file을 진실의 원천으로 보고
        # 불일치 시 match를 무시하고 footage 기반 폴백(아래 else 분기)으로 떨어진다.
        # (사용자가 다시 매칭을 돌리거나 수동 매칭을 채우면 정상 복구)
        _match_src = scene.match.source_file if scene.match else None
        _footage_file = getattr(scene.footage, "file", None) if scene.footage else None
        if _match_src and _footage_file:
            from pathlib import Path as _P
            if _P(_match_src).name != _P(_footage_file).name:
                print(
                    f"[render WARN] scene={scene.scene_id} footage/match 불일치: "
                    f"footage.file={_footage_file} vs match.source_file={_match_src} "
                    f"— footage 우선, match 무시",
                    flush=True,
                )
                # match를 일시적으로 무력화하여 아래 else 분기(footage 기준 trim)로 떨어뜨림.
                # 인메모리 객체를 영구 변경하지 않도록 분기 후 _eff_match 변수만 사용한다.
                _eff_match = None
            else:
                _eff_match = scene.match
        else:
            _eff_match = scene.match
        if _eff_match is not None:
            # 정식 매칭 결과 사용 — 단, 사용자가 split(footage.clip_start_sec/clip_end_sec)을 적용했으면
            # 분할 범위가 매칭 trim 보다 우선한다. match.trim_* 은 보통 footage 원본 전체를 가리키므로
            # split 정보 없이 사용하면 사용자가 잘라낸 부분이 무시되고 원본 전체가 들어가 영상이 길어진다.
            _cs_split = getattr(scene.footage, "clip_start_sec", None)
            _ce_split = getattr(scene.footage, "clip_end_sec", None)
            _eff_ts = float(_cs_split) if _cs_split is not None else float(_eff_match.trim_start)
            _eff_te = float(_ce_split) if _ce_split is not None else float(_eff_match.trim_end)
            sel = _build_sel(scene, _eff_match.source_file, _eff_ts, _eff_te)
        else:
            # 브라우저 전용 프로젝트: footage 파일을 직접 사용 (매칭 없음)
            fname = getattr(scene.footage, "file", None) or getattr(scene.footage, "image", None)
            if not fname:
                continue
            # 씬 나누기 — clip_start_sec/clip_end_sec이 있으면 그 구간을 trim 범위로 사용.
            full_dur = _footage_duration(fname)
            cs = getattr(scene.footage, "clip_start_sec", None)
            ce = getattr(scene.footage, "clip_end_sec", None)
            ts = float(cs) if cs is not None else 0.0
            te = float(ce) if ce is not None else full_dur
            ts = max(0.0, ts)
            te = min(full_dur, te) if full_dur > 0 else te
            if te <= ts:
                te = full_dur if full_dur > ts else ts + 1.0
            sel = _build_sel(scene, fname, ts, te)
        selections.append(sel)

    clip_sel_path = cache_dir / "clip_selection.json"
    clip_sel_data = {
        "selections": selections,
        "output_width": p.output_width if hasattr(p, "output_width") else 1920,
        "output_height": p.output_height if hasattr(p, "output_height") else 1080,
        # 프로젝트 전역 배경색(letterbox 여백). 기본 "black" — 미지정 시 기존과 동일.
        "bg_color": getattr(p, "bg_color", "black") or "black",
        # 스플릿 씬 디졸브 강제 적용 옵션 — clip_editor가 안전망(같은 source_file 인접 씬 그룹 병합) 분기 여부 판단.
        # 기본 False(안전망 작동 = 스플릿 씬 dissolve 0초). True면 스플릿 씬에도 일반 dissolve 강제.
        "force_split_scene_dissolve": bool(getattr(p, "force_split_scene_dissolve", False)),
    }
    # encoding 명시 필수 — Windows 기본(CP949)으로 저장되면 읽는 쪽(UTF-8)에서 깨진다.
    clip_sel_path.write_text(
        json.dumps(clip_sel_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _log(f"Step 1 완료: {len(selections)}개 씬 선택")

    # ── Step 2: TTS 재생성이 필요한 씬 목록 ──────────────────────────────
    # #382 — segment 1개라도 synced=False 이거나 WAV 가 없으면 재생성 대상.
    # #393 — segment WAV 경로 계산은 narration_merge 공통 유틸로 위임. preview 합성도
    # 동일한 경로 규칙을 따라야 final/preview 간 산출물이 어긋나지 않는다.
    from ..narration_merge import segment_wav_path as _segment_wav_path
    from ..narration_merge import build_merged_wav as _merge_wav

    def _segment_wav(scene_id: int, seg_id: str) -> Path:
        return _segment_wav_path(narr_dir, scene_id, seg_id)

    def _tts_stale(s) -> bool:
        segs = [seg for seg in (s.narration_segments or []) if seg.text and seg.text.strip()]
        if not segs:
            # narration 자체가 없으면 재생성 불필요.
            return False
        for seg in segs:
            if not seg.synced or seg.status != "ready":
                return True
            wav = _segment_wav(s.scene_id, seg.id)
            if not wav.exists() or wav.stat().st_size == 0:
                return True
            expected = float(seg.duration_sec or 0.0)
            if expected > 0:
                actual = _wav_duration(wav)
                if actual > 0 and abs(actual - expected) > 0.05:
                    return True
        return False

    unsynced_scene_ids = [s.scene_id for s in p.scenes if _tts_stale(s)]
    all_synced = len(unsynced_scene_ids) == 0

    # src/main.py의 파이프라인 함수들은 cwd 기준으로 cache/, output/ 경로를 사용하므로
    # 프로젝트 데이터 디렉토리로 cwd를 변경한 뒤 실행한다.
    old_cwd = os.getcwd()
    project_data_dir = str(cache_dir.parent)  # web/data/{project_id}/

    try:
        # cwd 변경 전에 .env 로드 + GOOGLE_APPLICATION_CREDENTIALS 절대경로 정규화.
        # 이유:
        #  - 백엔드(uvicorn) 자체는 .env를 자동 로드하지 않아 import 시점까지 env가 비어 있을 수 있다.
        #  - .env의 GOOGLE_APPLICATION_CREDENTIALS가 상대경로(./gcp_key.json)이면 chdir 후 SDK가 못 찾는다.
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv()
        except Exception as e:
            _log(f"[warn] dotenv 로드 실패(무시 가능): {e}")
        gcp_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if gcp_creds and not os.path.isabs(gcp_creds):
            abs_creds = os.path.abspath(gcp_creds)
            if os.path.isfile(abs_creds):
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = abs_creds
                _log(f"GOOGLE_APPLICATION_CREDENTIALS 절대경로 적용: {abs_creds}")
            else:
                _log(f"[warn] GOOGLE_APPLICATION_CREDENTIALS={gcp_creds} (절대경로={abs_creds}) 파일 없음")
        elif not gcp_creds:
            _log("[warn] GOOGLE_APPLICATION_CREDENTIALS 환경변수가 비어 있음 (TTS 호출 실패 예상)")

        _log(f"cwd 변경: {project_data_dir}")
        os.chdir(project_data_dir)

        # ── Step 3: Phase 4 (TTS) — 동기화 안 된 씬만 재생성 ────────────
        _log(f"Step 2: TTS 확인 — unsynced={unsynced_scene_ids}")
        if all_synced:
            _log("Step 2: 모든 TTS 최신 상태 — 생략")
            push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "render",
                                "current": 1, "total": 5,
                                "message": "모든 TTS 최신 상태 — Phase 4 생략", "item": ""})
        else:
            push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "render",
                                "current": 1, "total": 5,
                                "message": f"TTS 재생성 중: {len(unsynced_scene_ids)}개 씬", "item": ""})
            from src.narration_generator import _tts as tts_synthesize
            from ..models import TtsMeta
            for scene_id in unsynced_scene_ids:
                _log(f"  TTS 생성: 씬 {scene_id}")
                scene = next(s for s in p.scenes if s.scene_id == scene_id)
                # #382 — 씬의 각 segment 마다 WAV 생성. 빈 텍스트는 스킵.
                for seg in list(scene.narration_segments or []):
                    if not seg.text or not seg.text.strip():
                        continue
                    wav_seg = _segment_wav(scene_id, seg.id)
                    # 씬 태그 speaker:{name} 반영 — 편집기에서 들은 목소리와
                    # 최종 렌더의 목소리가 같아야 한다.
                    voice = resolve_segment_voice(seg, scene, p)
                    duration = tts_synthesize(seg.text, str(wav_seg), voice_name=voice)
                    seg.audio_url = f"/api/projects/{project_id}/tts/segment/{seg.id}/audio?scene_id={scene_id}"
                    seg.duration_sec = float(duration)
                    seg.status = "ready"
                    seg.synced = True
                # seg_0 동기화 → SceneCard.tts 호환 필드 갱신
                seg_0 = next((s for s in (scene.narration_segments or []) if s.id == "seg_0"), None)
                tts_meta = None
                if seg_0 is not None:
                    tts_meta = TtsMeta(
                        audio_url=f"/api/projects/{project_id}/tts/{scene_id}/audio",
                        duration_sec=seg_0.duration_sec,
                        status="ready",
                        synced=True,
                    )
                store.update_scene(project_id, scene_id,
                                   narration_segments=list(scene.narration_segments or []),
                                   narration_text=seg_0.text if seg_0 else scene.narration_text,
                                   tts=tts_meta)
        _log("Step 2 완료")

        # ── Step 3.5: narration_timeline.json 생성 + 씬별 merged WAV ────────
        # #382 — 씬 1개에 여러 segment 가 있으므로 final_composer 호환을 위해
        # 씬별 merged WAV (narration_scene_NNN.wav) 를 segment 들로부터 ffmpeg adelay+amix 로 합성한다.
        # seg_0 만 있는 (= N=1) 케이스는 결과적으로 단일 segment WAV 와 동일하게 동작한다.
        # 단 ``atrim=end=씬길이`` 는 ffmpeg 가 amix=duration=longest 이후 적용해 넘침을 자른다.
        p = store.get_project(project_id)  # TTS synced 갱신 반영
        narr_timeline_path = narr_dir / "narration_timeline.json"

        # #393 — 씬 길이 / merged WAV 합성은 narration_merge 공통 유틸로 위임.
        # preview(`_make_scene_preview`) 도 같은 모듈을 호출해 동일한 결과를 얻는다.
        from ..narration_merge import scene_visual_length as _scene_visual_length

        def _build_merged_wav(scene, merged_path: Path) -> float:
            return _merge_wav(scene, narr_dir, merged_path, log=_log)

        entries = []
        for scene in sorted(p.scenes, key=lambda s: s.scene_id):
            segs_active = [seg for seg in (scene.narration_segments or [])
                           if seg.text and seg.text.strip()]
            if not segs_active and not scene.narration_text:
                continue
            merged_path = narr_dir / f"narration_scene_{scene.scene_id:03d}.wav"
            merged_dur = _build_merged_wav(scene, merged_path)
            # 자막용 텍스트 — segment 단위 sbv 생성에 사용. SBV 생성기는 segments
            # 필드를 보면 segment 단위로 자막을 만들고, 없으면 옛 동작(전체 텍스트) 으로 폴백한다.
            scene_dur = _scene_visual_length(scene)
            sbv_segments = []
            for seg in segs_active:
                seg_start = float(seg.offset_sec or 0.0)
                seg_end = seg_start + float(seg.duration_sec or 0.0)
                # 넘침은 씬 경계에서 잘림 (이슈 본문 명시).
                if seg_end > scene_dur:
                    seg_end = scene_dur
                if seg_end <= seg_start:
                    continue
                sbv_text = seg.sbv_text if isinstance(seg.sbv_text, str) and seg.sbv_text.strip() else seg.text
                sbv_segments.append({
                    "id": seg.id,
                    "offset_sec": seg_start,
                    "end_sec_in_scene": seg_end,
                    "text": seg.text,
                    "sbv_text": sbv_text,
                })

            st_raw = getattr(scene, "subtitle_text", None)
            subtitle_text = st_raw.strip() if isinstance(st_raw, str) and st_raw.strip() else (
                scene.narration_text or (segs_active[0].text if segs_active else "")
            )
            # entry duration: 시각 길이(씬 길이) 기준으로 SBV / final_composer 의 동작 일관성 보장.
            # merged_dur 가 0이면 segment 가 없어 wav 생성 실패한 케이스 → scene_dur 폴백.
            audio_dur = merged_dur if merged_dur > 0 else scene_dur
            entries.append({
                "scene_id": scene.scene_id,
                "audio_file": f"output/narration/narration_scene_{scene.scene_id:03d}.wav",
                "narration_text": scene.narration_text or (segs_active[0].text if segs_active else ""),
                "subtitle_text": subtitle_text,
                "start_sec": 0.0,
                "end_sec": audio_dur,
                "duration_sec": audio_dur,
                # #382 — segment 단위 SBV 생성을 위해 segments 메타 첨부.
                "segments": sbv_segments,
                "scene_visual_duration_sec": scene_dur,
            })
        narr_timeline_path.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
            # encoding 을 반드시 명시한다. Windows 의 기본 인코딩은 CP949 라
            # 한글 나레이션이 CP949 로 저장되는데, 읽는 쪽(utils/file_cache.load_json)은
            # UTF-8 로 열어 UnicodeDecodeError 로 렌더가 통째로 실패한다.
            encoding="utf-8",
        )
        _log(f"Step 3 완료: narration_timeline.json ({len(entries)}개 항목)")

        # ── Step 4: Phase 5 (영상 편집) ───────────────────────────────────
        _log("Step 4: clip_editor 시작")
        push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "render",
                            "current": 2, "total": 5, "message": "영상 편집 중 (clip_editor)...", "item": ""})
        from src.clip_editor import run as clip_editor_run
        import time as _time_phase5
        _t5_start = _time_phase5.monotonic()

        # clip_editor 는 렌더에서 가장 오래 걸리는 단계라 씬별 진행을 전달한다.
        # 전체 진행 바(phase="render", total=5)와 일관되게, 이 단계는 구간 [2, 3] 을
        # 채운다: current = 2 + (done/씬수) → 40%~60% 를 씬 단위로 매끄럽게 진행.
        # (기존 total=5 스킴 그대로라 프론트 변경 없이 역행/점프 없이 채워진다.)
        def _clip_editor_progress(done: int, total: int, sid) -> None:
            frac = (done / total) if total > 0 else 0.0
            if sid is not None:
                msg = f"영상 편집 중: 씬 {sid} ({done}/{total})"
            else:
                msg = f"영상 편집 완료 ({done}/{total})"
            push_sync(task_id, {
                "type": "progress", "task_id": task_id, "phase": "render",
                "current": 2 + frac, "total": 5, "message": msg, "item": "",
            })

        clip_editor_run(force=True, progress_cb=_clip_editor_progress)
        _phase5_cpu_sec = _time_phase5.monotonic() - _t5_start
        _log(f"Step 4 완료: clip_editor ({_phase5_cpu_sec:.1f}s)")

        # #301 — 모의 비용 기록: Phase 5 클립 편집
        try:
            from ..cost_tracker import measure_phase as _mp5
            muted_path = output_dir / "muted_output.mp4"
            muted_bytes = muted_path.stat().st_size if muted_path.exists() else 0
            with _mp5(project_id) as _est:
                _est.record_clip_edit(cpu_seconds=_phase5_cpu_sec, output_bytes=muted_bytes)
        except Exception as _cost_err:
            print(f"[cost] clip_edit 기록 실패: {_cost_err}")

        # ── Step 5: Phase 6 (최종 합성) ───────────────────────────────────
        _log("Step 5: final_composer 시작")
        push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "render",
                            "current": 3, "total": 5, "message": "최종 합성 중 (final_composer)...", "item": ""})

        # ── #290 — 오버레이 SFX 엔트리 수집 ───────────────────────────────
        # 각 씬의 텍스트/도형/그래픽 오버레이 중 sfx_asset_id가 지정된 것만 모은다.
        # 절대 트리거 시각 = 씬의 영상 타임라인 start_sec + 오버레이 start_sec.
        # 씬 영상 타임라인은 cache/timeline.json에서 가져온다 (Phase 5가 방금 생성).
        sfx_entries: list[dict] = []
        try:
            tl_path = cache_dir / "timeline.json"
            if tl_path.exists():
                tl_data = json.loads(tl_path.read_text(encoding="utf-8"))
                scene_start_map = {
                    ts["scene_id"]: float(ts.get("start_sec", 0.0))
                    for ts in tl_data.get("timestamps", [])
                }
                # 씬 끝 시각 — 오버레이/씬 SFX 를 씬 경계에서 자르는 데 쓴다(다음 씬으로 번짐 방지).
                scene_end_map = {
                    ts["scene_id"]: float(ts["end_sec"])
                    for ts in tl_data.get("timestamps", [])
                    if ts.get("end_sec") is not None
                }
                # 삐 소리 타이밍용 — narration_timeline 의 (Phase 5 갱신 후) start_sec.
                # 이 값은 clip_editor 가 timeline.start_sec + NARRATION_PADDING(0.6s) 로
                # 갱신한 "실제 나레이션 WAV 배치 시각" 이라, segment offset 을 그 위에 더하면
                # 최종 믹스에서 나레이션 오디오가 실제로 나오는 절대 시각과 일치한다.
                # (timeline.start_sec 만 쓰면 padding 만큼 어긋나 비프가 TTS 끝보다 이르게 붙음.)
                narr_start_map: dict[int, float] = {}
                try:
                    _ntl_path = narr_dir / "narration_timeline.json"
                    if _ntl_path.exists():
                        _ntl = json.loads(_ntl_path.read_text(encoding="utf-8"))
                        for _e in _ntl.get("entries", []):
                            narr_start_map[_e["scene_id"]] = float(_e.get("start_sec", 0.0))
                except Exception:
                    narr_start_map = {}
                p2 = store.get_project(project_id)
                asset_by_id = {
                    a.asset_id: a for a in (p2.studio_assets if p2 else [])
                }
                footage_dir_path = Path(p2.footage_dir) if (p2 and p2.footage_dir) else None

                def _resolve_sfx(asset_id: str) -> str | None:
                    """sfx_asset_id를 파일 경로로 변환. 자산 없거나 파일 부재 시 None.

                    두 가지 ID 체계를 지원한다.
                    1) ``"preset:{preset_key}"`` — 앱 내장 기본 효과음 프리셋(#418).
                       footage_dir 와 무관하게 절대 경로로 해석된다.
                    2) studio_assets 의 ``asset_id``(uuid) — 기존 동작.
                       footage_dir 가 없거나 audio 타입이 아니면 None.
                    """
                    if not asset_id:
                        return None
                    # 1) SFX 프리셋 분기 — footage_dir 의존성 없음.
                    if asset_id.startswith("preset:"):
                        from src.sfx_presets import get_preset_file_path
                        preset_key = asset_id[len("preset:"):]
                        path = get_preset_file_path(preset_key)
                        if not path:
                            return None
                        return path if Path(path).exists() else None
                    # 2) 기존 studio_assets 해석.
                    if not footage_dir_path:
                        return None
                    a = asset_by_id.get(asset_id)
                    if not a or a.asset_type != "audio":
                        return None
                    src = footage_dir_path / a.filename
                    return str(src) if src.exists() else None

                for scene in (p2.scenes if p2 else []):
                    if scene.scene_id not in scene_start_map:
                        continue
                    scene_abs_start = scene_start_map[scene.scene_id]
                    scene_abs_end = scene_end_map.get(scene.scene_id)

                    def _max_dur(abs_start: float) -> float | None:
                        """씬 끝까지 남은 길이. 씬 끝을 모르면 None(자르지 않음)."""
                        if scene_abs_end is None:
                            return None
                        return max(0.05, scene_abs_end - abs_start)

                    def _collect(ovs):
                        for ov in ovs or []:
                            asset_id = getattr(ov, "sfx_asset_id", None)
                            if not asset_id:
                                continue
                            path = _resolve_sfx(asset_id)
                            if not path:
                                continue
                            vol = float(getattr(ov, "sfx_volume", 0.7) or 0.7)
                            ov_start = float(getattr(ov, "start_sec", 0.0) or 0.0)
                            sfx_entries.append({
                                "abs_start_sec": scene_abs_start + ov_start,
                                "path": path,
                                "volume": max(0.0, min(1.0, vol)),
                                "max_duration_sec": _max_dur(scene_abs_start + ov_start),
                            })

                    _collect(getattr(scene, "text_overlays", None))
                    _collect(getattr(scene, "shape_overlays", None))
                    _collect(getattr(scene, "graphic_overlays", None))
                    _collect(getattr(scene, "image_overlays", None))

                    # 씬 본체 SFX — 트리거 시각 = 씬 시작 + sfx_offset_sec.
                    scene_sfx_id = getattr(scene, "sfx_asset_id", None)
                    if scene_sfx_id:
                        scene_sfx_path = _resolve_sfx(scene_sfx_id)
                        if scene_sfx_path:
                            scene_sfx_vol = float(getattr(scene, "sfx_volume", 0.7) or 0.7)
                            scene_sfx_offset = float(getattr(scene, "sfx_offset_sec", 0.0) or 0.0)
                            sfx_entries.append({
                                "abs_start_sec": scene_abs_start + scene_sfx_offset,
                                "path": scene_sfx_path,
                                "volume": max(0.0, min(1.0, scene_sfx_vol)),
                                "max_duration_sec": _max_dur(scene_abs_start + scene_sfx_offset),
                            })

                    # "삐 소리" 씬 단위 자동 효과 — 마지막 활성 TTS segment 끝에 이어붙임.
                    # 비프 시작 시각 계산은 순수 함수(compute_beep_start_sec)에 위임하고,
                    # 비프음 WAV 는 ffmpeg sine 으로 생성해 기존 SFX 트랙 경로로 믹싱한다.
                    if getattr(scene, "beep_on_narration_end", False):
                        from src.beep_effect import (
                            compute_beep_start_sec as _compute_beep_start,
                            ensure_beep_wav as _ensure_beep_wav,
                            DEFAULT_BEEP_DURATION_SEC as _BEEP_DUR,
                            DEFAULT_BEEP_FREQ_HZ as _BEEP_FREQ,
                        )
                        # 비프 기준점 = 나레이션 WAV 의 실제 배치 시각(narr_start_map).
                        # 없으면 timeline 씬 시작(scene_abs_start) 폴백.
                        beep_base = narr_start_map.get(scene.scene_id, scene_abs_start)
                        # 비프 길이만큼 앞당겨 시작 → 나레이션 끝과 비프 끝이 함께 떨어진다.
                        beep_start = _compute_beep_start(scene, beep_base, _BEEP_DUR)
                        if beep_start is not None:
                            beep_wav = _ensure_beep_wav(
                                narr_dir / f"_beep_{_BEEP_FREQ}_{int(_BEEP_DUR * 1000)}ms.wav",
                                freq_hz=_BEEP_FREQ,
                                duration_sec=_BEEP_DUR,
                            )
                            if beep_wav is not None:
                                beep_vol = float(getattr(scene, "beep_volume", 0.7) or 0.7)
                                sfx_entries.append({
                                    "abs_start_sec": beep_start,
                                    "path": str(beep_wav),
                                    "volume": max(0.0, min(1.0, beep_vol)),
                                    # 비프는 나레이션 끝에 겹쳐 나므로 TTS 더킹 제외
                                    # (더킹하면 정작 가려야 할 구간에서 비프가 작아진다).
                                    "no_duck": True,
                                    # 이 구간의 나레이션을 음소거해 실제로 "가려지게" 한다.
                                    # 볼륨만으로는 말소리가 그대로 남아 검열음이 되지 않는다.
                                    "beep_mute": True,
                                    "duration_sec": _BEEP_DUR,
                                })
        except Exception as sfx_err:
            _log(f"SFX 수집 경고 (무시): {sfx_err}")
            sfx_entries = []
        _log(f"SFX 엔트리 수집: {len(sfx_entries)}개")

        from src.final_composer import run as final_run
        import time as _time_phase6
        _t6_start = _time_phase6.monotonic()
        if watermark:
            push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "render",
                                "current": 3, "total": 5,
                                "message": "최종 합성 + 비가시 워터마크 삽입 중 (수 분 더 걸립니다)...", "item": ""})
        final_run(force=True, sfx_entries=sfx_entries, watermark=watermark)
        _phase6_cpu_sec = _time_phase6.monotonic() - _t6_start
        _log(f"Step 5 완료: final_composer ({_phase6_cpu_sec:.1f}s)")

        # #301 — 모의 비용 기록: Phase 6 최종 합성
        try:
            from ..cost_tracker import measure_phase as _mp6
            final_path = output_dir / "final_output.mp4"
            final_bytes = final_path.stat().st_size if final_path.exists() else 0
            with _mp6(project_id) as _est:
                _est.record_final_compose(cpu_seconds=_phase6_cpu_sec, output_bytes=final_bytes)
        except Exception as _cost_err:
            print(f"[cost] final_compose 기록 실패: {_cost_err}")

        # ── Step 6: FCPXML 생성 ───────────────────────────────────────────
        _log("Step 6: FCPXML 생성")
        push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "render",
                            "current": 4, "total": 5, "message": "FCPXML 타임라인 생성 중...", "item": ""})
        try:
            from src import fcpxml_generator
            # fcpxml_generator.run 은 project_root 인자를 받지 않는다 — CWD 기준으로 cache/output 을 읽으므로
            # 이미 위에서 os.chdir 로 project_data_dir 로 옮긴 상태면 그대로 동작한다.
            fcpxml_generator.run(force=True)
            _log("Step 6 완료: FCPXML")
        except Exception as fcp_err:
            import traceback
            _log(f"Step 6 경고: FCPXML 생성 실패 (무시) — {fcp_err}\n{traceback.format_exc()}")

        final_output = str(project_data_dir + "/output/final_output.mp4")
        _log(f"렌더링 완료: {final_output}")
        _render_status[project_id] = {
            "task_id": task_id, "phase": 6, "status": "done",
            "output_file": final_output,
        }
        push_sync(task_id, {"type": "complete", "task_id": task_id,
                            "summary": {"total": 5, "success": 5, "failed": 0}})

    except Exception as e:
        import traceback as _tb
        full = _tb.format_exc()
        print(f"[RENDER ERROR] {e}\n{full}", flush=True)
        _render_status[project_id] = {"task_id": task_id, "phase": -1,
                                      "status": "error", "error": str(e)}
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "RENDER_FAILED", "message": f"{e}\n\n{full}"})
    finally:
        os.chdir(old_cwd)
