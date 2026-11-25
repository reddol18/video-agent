"""#403 — `scene_preview_render` (POST /preview) 가 narration_segments 합성을 반영하는지 검증.

배경: #393 회귀의 진짜 회귀.
프론트엔드가 실제로 호출하는 라우터는 POST `/scenes/{sid}/preview` →
`scene_preview_render` 인데 이 함수는 옛 단일 `narration_scene_NNN.wav` 만 참조해
사용자가 추가한 seg_1+ TTS 가 미리보기에 들리지 않았다. (#393 가 잘못 GET 경로의
`_make_scene_preview` 만 수정한 결과.)

여기서는 ffmpeg 호출을 모두 모킹하여 16번 씬(seg_0+seg_1) 시뮬레이션을 통해
`scene_preview_render` 가 `build_merged_wav` 를 호출하고 merged wav 를 amix 입력으로
사용하는지 확인한다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def patched_render(monkeypatch, tmp_path):
    """`scene_preview_render` 호출 시 ffmpeg/ffprobe 가 실제로 안 돌도록 모킹."""
    from web.backend.routers import project as proj_mod

    calls = SimpleNamespace(
        merged_wav_calls=[],
        ffmpeg_commands=[],
        merged_wav_path=None,
    )

    monkeypatch.setattr(proj_mod, "_ffprobe_duration", lambda _path: 5.0)
    monkeypatch.setattr(proj_mod, "_ffprobe_video_size", lambda _path: (1280, 720))
    monkeypatch.setattr(proj_mod, "_ensure_proxy", lambda _pid, _fname: None)

    # narration_merge.build_merged_wav 모킹.
    from web.backend import narration_merge as merge_mod

    def fake_build_merged_wav(scene, narration_dir, merged_path, log=None):
        calls.merged_wav_calls.append({
            "scene_id": scene.scene_id,
            "segment_ids": [s.id for s in (scene.narration_segments or [])],
            "narration_dir": str(narration_dir),
            "merged_path": str(merged_path),
        })
        calls.merged_wav_path = str(merged_path)
        merged_path.parent.mkdir(parents=True, exist_ok=True)
        # WAV-처럼 보이는 임의 바이트 (size > 0).
        merged_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake_data")
        # 실제 scene_visual_length 호출 — atrim 정책 시뮬레이션.
        return float(merge_mod.scene_visual_length(scene))

    monkeypatch.setattr(merge_mod, "build_merged_wav", fake_build_merged_wav)
    # routers.project 모듈 내부에서 `from ..narration_merge import build_merged_wav` 로
    # 직접 import 하므로, 모듈 attribute 도 함께 패치한다.
    # 단, scene_preview_render 는 `from ..narration_merge import build_merged_wav as _merge_wav`
    # 를 함수 내부에서 호출하므로 narration_merge 모듈 attribute 만 패치하면 충분하다.

    def fake_run(args, *_run_args, **_run_kwargs):
        calls.ffmpeg_commands.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
        if args and len(args) >= 1:
            last = str(args[-1])
            if "ffprobe" in str(args[0]):
                return SimpleNamespace(returncode=0, stdout="audio", stderr="")
            try:
                p = Path(last)
                if p.suffix in {".mp4", ".wav"}:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    # ffmpeg `-y` 동작 모사 — 빈 파일이 있어도 덮어쓰기.
                    p.write_bytes(b"fake_output")
            except Exception:
                pass
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    return proj_mod, calls


def _make_project_with_two_segments(tmp_path, monkeypatch):
    """16번 씬 시뮬레이션 — seg_0 + seg_1 가 모두 ready 상태인 프로젝트 생성.

    `scene_preview_render` 는 `p.footage_dir` 와 `scene.footage.file` 을 참조하므로
    fake footage 파일과 footage_dir 을 실제로 만들어둔다.
    """
    from web.backend import state as store
    from web.backend.models import Project, SceneCard, NarrationSegment, ClipEffect

    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", data_root)
    store._projects.clear()

    # footage_dir 와 file 을 실제로 만든다 — scene_preview_render 가 존재 검사를 수행.
    footage_dir = tmp_path / "footage"
    footage_dir.mkdir(exist_ok=True)
    fake_footage = footage_dir / "fake.mp4"
    fake_footage.write_bytes(b"fake_video")

    project_id = "test-403"
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
        footage=ClipEffect(file="fake.mp4"),
    )
    p = Project(
        project_id=project_id,
        name="t403",
        scenes=[scene],
        footage_dir=str(footage_dir),
    )
    store._projects[project_id] = p

    proj_dir = data_root / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "state.json").write_text("{}")
    (proj_dir / "cache").mkdir(exist_ok=True)
    (proj_dir / "output" / "narration").mkdir(parents=True, exist_ok=True)
    return project_id


def test_scene_preview_render_uses_merged_wav_for_segments(patched_render, tmp_path, monkeypatch):
    """POST /preview (`scene_preview_render`): seg_0 + seg_1 → build_merged_wav 호출 + merged wav 가 amix 에 사용.

    이것이 #403 의 핵심 — 사용자가 실제로 호출하는 라우터가 segments 를 반영해야 한다.
    """
    from fastapi import BackgroundTasks

    proj_mod, calls = patched_render
    project_id = _make_project_with_two_segments(tmp_path, monkeypatch)

    bg = BackgroundTasks()
    resp = proj_mod.scene_preview_render(project_id, 16, bg)
    assert resp is not None, "scene_preview_render 응답 없음"

    # 1) build_merged_wav 가 호출됐는가?
    assert len(calls.merged_wav_calls) == 1, (
        f"build_merged_wav 호출 횟수 불일치(예상 1): {calls.merged_wav_calls}"
    )
    call = calls.merged_wav_calls[0]
    assert call["scene_id"] == 16
    assert "seg_0" in call["segment_ids"] and "seg_1" in call["segment_ids"]

    # 2) merged wav 가 임시 경로(/tmp/...vag_seg_merged_016_...wav)에 만들어졌는가?
    merged_path = Path(call["merged_path"])
    assert merged_path.name.startswith("vag_seg_merged_016_"), (
        f"merged wav 임시 파일명 정책 위반: {merged_path}"
    )
    assert merged_path.suffix == ".wav"

    # 3) merged wav 가 ffmpeg `-i` 입력으로 들어간 호출이 있는가? (amix 단계)
    merged_path_str = str(merged_path)
    used_in_ffmpeg = any(merged_path_str in cmd for cmd in calls.ffmpeg_commands)
    assert used_in_ffmpeg, (
        f"merged wav 가 ffmpeg amix 입력으로 사용되지 않음 (#403 회귀 미수정). "
        f"ffmpeg 호출 수={len(calls.ffmpeg_commands)}"
    )


def test_scene_preview_render_falls_back_to_legacy_when_no_segments(patched_render, tmp_path, monkeypatch):
    """segments 비어있고 옛 narration_scene_NNN.wav 만 있는 레거시 케이스는 그대로 폴백 — 회귀 안전."""
    from fastapi import BackgroundTasks
    from web.backend import state as store
    from web.backend.models import Project, SceneCard, ClipEffect, TtsMeta

    proj_mod, calls = patched_render

    data_root = tmp_path / "data_legacy"
    data_root.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", data_root)
    store._projects.clear()

    footage_dir = tmp_path / "footage_legacy"
    footage_dir.mkdir(exist_ok=True)
    (footage_dir / "fake.mp4").write_bytes(b"fake")

    project_id = "test-403-legacy"
    scene = SceneCard(
        scene_id=1,
        narration_text="옛 단일 narration",
        narration_segments=[],
        tts=TtsMeta(audio_url="u", duration_sec=4.0, status="ready", synced=True),
        footage=ClipEffect(file="fake.mp4"),
    )
    p = Project(
        project_id=project_id, name="t403-legacy",
        scenes=[scene], footage_dir=str(footage_dir),
    )
    store._projects[project_id] = p

    proj_dir = data_root / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "state.json").write_text("{}")
    (proj_dir / "cache").mkdir(exist_ok=True)
    narr_dir = proj_dir / "output" / "narration"
    narr_dir.mkdir(parents=True, exist_ok=True)
    legacy_wav = narr_dir / "narration_scene_001.wav"
    legacy_wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    bg = BackgroundTasks()
    resp = proj_mod.scene_preview_render(project_id, 1, bg)
    assert resp is not None

    # segments 가 없으므로 build_merged_wav 미호출.
    assert len(calls.merged_wav_calls) == 0, (
        f"segments 없는데 build_merged_wav 가 호출됨: {calls.merged_wav_calls}"
    )

    # 옛 legacy wav 가 ffmpeg 입력으로 들어갔는가?
    legacy_path_str = str(legacy_wav)
    used = any(legacy_path_str in cmd for cmd in calls.ffmpeg_commands)
    assert used, (
        f"레거시 narration_scene_001.wav 가 ffmpeg 입력으로 사용되지 않음. "
        f"#403 수정이 레거시 경로를 망가뜨림."
    )
