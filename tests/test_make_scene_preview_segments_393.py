"""#393 — `_make_scene_preview` 가 narration_segments 합성을 반영하는지 검증.

배경: #382 도입 후에도 미리보기는 옛 단일 `narration_scene_NNN.wav` 만 사용해
사용자가 추가한 segment(seg_1+) 의 TTS 가 미리보기에 들리지 않는 회귀가 있었다.

여기서는 ffmpeg 호출을 모두 모킹하여 다음을 검증한다.
1. 씬에 segments(seg_0+seg_1) 가 있을 때 `_make_scene_preview` 가 `build_merged_wav`
   를 호출하고 그 결과 wav 를 amix 입력으로 사용한다.
2. segments 가 비어있고 옛 `narration_scene_NNN.wav` 만 있는 레거시 케이스는
   그대로 폴백 (회귀 안전).
3. 16번 씬 시뮬레이션 — seg_0+seg_1 가 모두 있는 경우 merged WAV 가 합성되고
   ffmpeg amix 명령에 포함되는지 확인.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def patched_preview(monkeypatch, tmp_path):
    """`_make_scene_preview` 호출 시 ffmpeg/ffprobe 가 실제로 안 돌도록 모킹.

    - ffprobe duration → 5.0s 고정
    - ffprobe size → (1280, 720)
    - subprocess.run → 결과 mp4/wav 파일을 빈 채로 생성하고 returncode=0
    - build_merged_wav → 호출 인자를 캡처하고 merged wav 파일을 빈 채로 생성, 길이 9.93 반환
    """
    from web.backend.routers import project as proj_mod

    calls = SimpleNamespace(
        merged_wav_calls=[],
        ffmpeg_commands=[],
        merged_wav_path=None,
    )

    # ── _ffprobe_* 모킹 ────────────────────────────────────────────────────
    monkeypatch.setattr(proj_mod, "_ffprobe_duration", lambda _path: 5.0)
    monkeypatch.setattr(proj_mod, "_ffprobe_video_size", lambda _path: (1280, 720))

    # _ensure_proxy 는 None 반환 (프록시 비활성).
    monkeypatch.setattr(proj_mod, "_ensure_proxy", lambda _pid, _fname: None)

    # _get_footage_file 은 그냥 가짜 경로 반환.
    fake_footage = tmp_path / "fake_footage.mp4"
    fake_footage.write_bytes(b"fake")
    monkeypatch.setattr(proj_mod, "_get_footage_file", lambda _scene, _dir: str(fake_footage))

    # ── narration_merge.build_merged_wav 모킹 ──────────────────────────────
    # #395 — 옛 모킹은 무조건 9.93s 를 반환해 atrim 회귀(scene_visual_length 가
    # seg_1+ 끝점을 무시하는 버그)를 검출하지 못했다. 이제는 실제
    # `scene_visual_length(scene)` 를 호출해 atrim 클램프 결과를 그대로 반환한다.
    #   - 옛 코드(seg_0/hint 만 봄): max(3.096, 5.0, 1.0) = 5.0 → 5.0s 반환
    #     → 미리보기 main_dur 가 짧아 seg_1 자리 부족 → 회귀 검출 가능.
    #   - 신 코드(seg_1 끝점 포함): max(3.096, 5.0, 9.93, 1.0) = 9.93 → 9.93s 반환
    #     → 미리보기 main_dur 충분 → seg_1 포함.
    from web.backend import narration_merge as merge_mod

    def fake_build_merged_wav(scene, narration_dir, merged_path, log=None):
        calls.merged_wav_calls.append({
            "scene_id": scene.scene_id,
            "segment_ids": [s.id for s in (scene.narration_segments or [])],
            "narration_dir": str(narration_dir),
            "merged_path": str(merged_path),
        })
        calls.merged_wav_path = str(merged_path)
        # 빈 wav 파일 생성 (size > 0).
        merged_path.parent.mkdir(parents=True, exist_ok=True)
        merged_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake_data")
        # 실제 scene_visual_length 를 호출해 atrim 정책을 충실히 시뮬레이션.
        return float(merge_mod.scene_visual_length(scene))

    monkeypatch.setattr(merge_mod, "build_merged_wav", fake_build_merged_wav)

    # ── subprocess.run 모킹 (ffmpeg/ffprobe) ───────────────────────────────
    def fake_run(args, *_run_args, **_run_kwargs):
        calls.ffmpeg_commands.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
        # ffmpeg/ffprobe 출력 파일이 인자 마지막에 있다고 가정 — 빈 파일로 만든다.
        if args and len(args) >= 1:
            last = str(args[-1])
            # ffprobe csv 결과 stdout 처리
            if "ffprobe" in str(args[0]):
                result = SimpleNamespace(returncode=0, stdout="audio", stderr="")
                return result
            # ffmpeg 출력 파일 생성
            try:
                p = Path(last)
                if p.suffix in {".mp4", ".wav"}:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.exists():
                        p.write_bytes(b"fake_output")
            except Exception:
                pass
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    return proj_mod, calls


def _make_project_with_two_segments(tmp_path, monkeypatch):
    """16번 씬 시뮬레이션 — seg_0 + seg_1 가 모두 ready 상태인 프로젝트 생성."""
    from web.backend import state as store
    from web.backend.models import Project, SceneCard, NarrationSegment

    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", data_root)
    store._projects.clear()

    project_id = "test-393"
    scene = SceneCard(
        scene_id=16,
        narration_text="seg_0 텍스트",
        narration_segments=[
            NarrationSegment(
                id="seg_0", text="seg_0 텍스트", offset_sec=0.0,
                duration_sec=3.096, status="ready", synced=True,
            ),
            NarrationSegment(
                id="seg_1", text="seg_1 추가 구간", offset_sec=7.65,
                duration_sec=2.28, status="ready", synced=True,
            ),
        ],
        footage=None,  # _get_footage_file 가 모킹돼 있어 무시됨.
    )
    p = Project(project_id=project_id, name="t393", scenes=[scene])
    store._projects[project_id] = p
    # state.json 도 만들어두기 (preview 캐시 무효화 검사용).
    proj_dir = data_root / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "state.json").write_text("{}")
    (proj_dir / "cache").mkdir(exist_ok=True)
    (proj_dir / "output" / "narration").mkdir(parents=True, exist_ok=True)
    return project_id


def test_preview_uses_merged_wav_for_segments(patched_preview, tmp_path, monkeypatch):
    """16번 씬: seg_0 + seg_1 → `build_merged_wav` 가 호출되고 merged wav 가 amix 에 사용된다."""
    proj_mod, calls = patched_preview
    project_id = _make_project_with_two_segments(tmp_path, monkeypatch)

    out = proj_mod._make_scene_preview(project_id, 16, force=True)
    assert out is not None, "미리보기 생성 실패"

    # build_merged_wav 가 정확히 1회 호출됨 (segment 들에 대해).
    assert len(calls.merged_wav_calls) == 1, \
        f"build_merged_wav 호출 횟수 불일치: {calls.merged_wav_calls}"
    call = calls.merged_wav_calls[0]
    assert call["scene_id"] == 16
    assert "seg_0" in call["segment_ids"]
    assert "seg_1" in call["segment_ids"]

    # merged wav 경로가 preview 폴더 내 _narration_merged_scene_016.wav 형태인지 확인.
    merged_path = Path(call["merged_path"])
    assert merged_path.name == "_narration_merged_scene_016.wav", \
        f"merged wav 파일명 정책 위반: {merged_path}"
    assert "previews" in merged_path.parts, \
        f"merged wav 가 preview 캐시 폴더 안에 생성되어야 함: {merged_path}"

    # ffmpeg 명령 중에 merged wav 가 `-i` 입력으로 들어간 호출이 있어야 한다 (amix 단계).
    merged_path_str = str(merged_path)
    used_in_ffmpeg = any(
        merged_path_str in cmd for cmd in calls.ffmpeg_commands
    )
    assert used_in_ffmpeg, (
        f"merged wav 가 ffmpeg amix 입력으로 사용되지 않음. "
        f"ffmpeg 호출: {calls.ffmpeg_commands}"
    )

    # #395 회귀 검증 — 미리보기 길이가 seg_1 끝점(9.93s)을 포괄해야 한다.
    # 옛 버그: scene_visual_length=5.0 → merged_dur=5.0 → main_dur=max(5.0,5.0+1.0)=6.0
    #   → seg_1(7.65~9.93s) 자리 없음 → seg_1 안 들림.
    # 신: scene_visual_length=9.93 → merged_dur=9.93 → main_dur=max(5.0,9.93+1.0)=10.93
    #   → seg_1 영역 포함.
    # cur clip 생성 ffmpeg 호출 (`-t <main_dur>` 또는 `tpad=stop_duration=<freeze>`)에서
    # 미리보기 영상 길이가 최소 seg_1 끝점(9.93s) 이상인지 확인한다.
    cur_clip_main_dur = None
    for cmd in calls.ffmpeg_commands:
        # tpad 케이스 (main_dur > footage_dur): -vf 인자에 stop_duration 포함, 영상=footage_dur+freeze
        if "-vf" in cmd:
            vf_idx = cmd.index("-vf")
            if vf_idx + 1 < len(cmd) and "tpad=stop_mode=clone:stop_duration=" in cmd[vf_idx + 1]:
                # stop_duration=<freeze> 추출 → main_dur = footage_dur(5.0) + freeze
                import re
                m = re.search(r"stop_duration=([\d.]+)", cmd[vf_idx + 1])
                if m:
                    freeze = float(m.group(1))
                    cur_clip_main_dur = 5.0 + freeze  # footage_dur 모킹값 5.0
                    break
        # -t <main_dur> 케이스 (main_dur <= footage_dur): cur clip 의 -t 인자
        if "-t" in cmd and "cur.mp4" in (cmd[-1] if cmd else ""):
            t_idx = cmd.index("-t")
            if t_idx + 1 < len(cmd):
                cur_clip_main_dur = float(cmd[t_idx + 1])
                break
    assert cur_clip_main_dur is not None, (
        f"cur clip ffmpeg 명령에서 main_dur 추출 실패: {calls.ffmpeg_commands}"
    )
    # seg_1 끝점 9.93s 이상이어야 seg_1 의 오디오가 영상 범위 안에 있다.
    assert cur_clip_main_dur >= 9.93, (
        f"#395 회귀 — 미리보기 cur clip 길이({cur_clip_main_dur:.3f}s)가 "
        f"seg_1 끝점(9.93s)을 포괄하지 못함. atrim 으로 seg_1 잘림."
    )


def test_preview_falls_back_to_legacy_wav_when_no_segments(patched_preview, tmp_path, monkeypatch):
    """segments 비어있고 옛 narration_scene_NNN.wav 만 있는 레거시 케이스는 폴백."""
    proj_mod, calls = patched_preview

    from web.backend import state as store
    from web.backend.models import Project, SceneCard, TtsMeta

    data_root = tmp_path / "data2"
    data_root.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", data_root)
    store._projects.clear()

    project_id = "test-393-legacy"
    scene = SceneCard(
        scene_id=1,
        narration_text="옛 단일 narration",
        narration_segments=[],  # segments 없음 (마이그레이션 전 상태)
        tts=TtsMeta(audio_url="u", duration_sec=4.0, status="ready", synced=True),
    )
    p = Project(project_id=project_id, name="t393-legacy", scenes=[scene])
    store._projects[project_id] = p

    proj_dir = data_root / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "state.json").write_text("{}")
    (proj_dir / "cache").mkdir(exist_ok=True)
    narr_dir = proj_dir / "output" / "narration"
    narr_dir.mkdir(parents=True, exist_ok=True)
    legacy_wav = narr_dir / "narration_scene_001.wav"
    legacy_wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    out = proj_mod._make_scene_preview(project_id, 1, force=True)
    assert out is not None

    # segments 가 없으므로 build_merged_wav 는 호출되지 않아야 한다.
    assert len(calls.merged_wav_calls) == 0, (
        f"segments 없는데 build_merged_wav 가 호출됨: {calls.merged_wav_calls}"
    )

    # 옛 legacy wav 가 ffmpeg amix 입력으로 들어갔는지 확인.
    legacy_path_str = str(legacy_wav)
    used = any(legacy_path_str in cmd for cmd in calls.ffmpeg_commands)
    assert used, (
        f"레거시 narration_scene_001.wav 가 ffmpeg 입력으로 사용되지 않음. "
        f"명령: {calls.ffmpeg_commands}"
    )


def test_preview_no_narration_no_wav_used(patched_preview, tmp_path, monkeypatch):
    """narration_text/segments 모두 비면 wav 가 ffmpeg 입력으로 사용되지 않아야 한다."""
    proj_mod, calls = patched_preview

    from web.backend import state as store
    from web.backend.models import Project, SceneCard

    data_root = tmp_path / "data3"
    data_root.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", data_root)
    store._projects.clear()

    project_id = "test-393-empty"
    scene = SceneCard(scene_id=1, narration_text="", narration_segments=[])
    p = Project(project_id=project_id, name="t393-empty", scenes=[scene])
    store._projects[project_id] = p

    proj_dir = data_root / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "state.json").write_text("{}")
    (proj_dir / "cache").mkdir(exist_ok=True)
    (proj_dir / "output" / "narration").mkdir(parents=True, exist_ok=True)

    out = proj_mod._make_scene_preview(project_id, 1, force=True)
    assert out is not None

    # build_merged_wav 호출 없음.
    assert len(calls.merged_wav_calls) == 0
