"""preview-final-cache — POST /scenes/{id}/preview 최종 결과 캐시 검증.

배경(실측): 프론트가 씬 전환마다 호출하는 `scene_preview_render` 가 매번 ffmpeg
전 구간 재인코딩을 수행했다(1회 3.3~3.5초, CPU 570%). GET 경로만 최종 캐시
(`cache/previews/scene_NNN.mp4`)를 보고 있었고 POST 경로에는 최종 캐시 조회가
아예 없었다. trim/speed 캐시는 중간 산출물이라 합성·인코딩 비용을 못 줄였다.

이 파일이 지키는 계약 2가지:

1. 편집이 없으면 두 번째 요청은 ffmpeg 를 전혀 호출하지 않는다 (캐시 적중).
2. **푸티지 파일이 서버 밖에서 바뀌면(mtime/size 변경) 캐시를 반드시 버린다.**
   써드파티 세션에서 원본을 직접 고치는 경우 서버는 통보를 받지 못하므로,
   캐시 키가 파일 실체(file_signature)를 포함해야만 감지된다. 이 보장이 깨지면
   "사용자가 고쳤는데 옛 영상이 계속 보이는" 최악의 실패 모드가 된다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def patched_render(monkeypatch):
    """ffmpeg/ffprobe 를 모킹하고 ffmpeg 호출 이력을 수집한다."""
    from web.backend.routers import project as proj_mod

    calls = SimpleNamespace(ffmpeg_commands=[])

    monkeypatch.setattr(proj_mod, "_ffprobe_duration", lambda _path: 5.0)
    monkeypatch.setattr(proj_mod, "_ffprobe_video_size", lambda _path: (1280, 720))
    # 프록시 생성을 끔 — 원본 footage 를 그대로 입력으로 쓰게 한다.
    monkeypatch.setattr(proj_mod, "_ensure_proxy", lambda _pid, _fname: None)

    def fake_run(args, *_a, **_kw):
        arg_list = list(args) if isinstance(args, (list, tuple)) else [str(args)]
        if arg_list and "ffprobe" in str(arg_list[0]):
            # ffprobe 는 비용이 거의 없고 캐시 판단과 무관 — 이력에 넣지 않는다.
            return SimpleNamespace(returncode=0, stdout="audio", stderr="")
        calls.ffmpeg_commands.append(arg_list)
        last = Path(str(arg_list[-1]))
        if last.suffix in {".mp4", ".wav"}:
            last.parent.mkdir(parents=True, exist_ok=True)
            last.write_bytes(b"fake_output_video_bytes")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return proj_mod, calls


def _make_project(tmp_path, monkeypatch, project_id: str):
    """footage 1개 + TTS 없는 단순 씬 1개짜리 프로젝트를 만든다."""
    from web.backend import state as store
    from web.backend.models import ClipEffect, Project, SceneCard

    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "DATA_DIR", data_root)
    store._projects.clear()

    footage_dir = tmp_path / "footage"
    footage_dir.mkdir(exist_ok=True)
    footage = footage_dir / "fake.mp4"
    footage.write_bytes(b"original_footage_bytes")

    scene = SceneCard(
        scene_id=1,
        narration_text="",          # TTS 없음 — 단순 복사 경로
        narration_segments=[],
        footage=ClipEffect(file="fake.mp4"),
    )
    store._projects[project_id] = Project(
        project_id=project_id,
        name="final-cache-test",
        scenes=[scene],
        footage_dir=str(footage_dir),
    )

    proj_dir = data_root / project_id
    (proj_dir / "cache").mkdir(parents=True, exist_ok=True)
    (proj_dir / "output" / "narration").mkdir(parents=True, exist_ok=True)
    (proj_dir / "state.json").write_text("{}")
    return footage


def _call_preview(proj_mod, project_id: str, scene_id: int = 1):
    from fastapi import BackgroundTasks

    bg = BackgroundTasks()
    resp = proj_mod.scene_preview_render(project_id, scene_id, bg)
    assert resp is not None
    return resp, bg


def test_second_request_hits_final_cache_and_skips_ffmpeg(patched_render, tmp_path, monkeypatch):
    """편집이 없으면 두 번째 요청은 ffmpeg 를 한 번도 호출하지 않는다."""
    proj_mod, calls = patched_render
    project_id = "final-cache-hit"
    _make_project(tmp_path, monkeypatch, project_id)

    _call_preview(proj_mod, project_id)
    first_round = len(calls.ffmpeg_commands)
    assert first_round > 0, "첫 요청은 실제로 ffmpeg 를 돌려야 한다(캐시 미스)"

    calls.ffmpeg_commands.clear()
    resp2, _ = _call_preview(proj_mod, project_id)

    assert calls.ffmpeg_commands == [], (
        f"두 번째 요청에서 ffmpeg 가 재실행됨 — 최종 캐시 미적용. "
        f"호출={[c[:4] for c in calls.ffmpeg_commands]}"
    )
    # 응답이 캐시 파일(final-*.mp4)을 가리키는지 확인.
    served = Path(resp2.path)
    assert served.name.startswith("final-"), f"캐시 파일이 서빙되지 않음: {served}"
    assert served.exists() and served.stat().st_size > 0


def test_external_footage_edit_invalidates_cache(patched_render, tmp_path, monkeypatch):
    """푸티지를 서버 밖에서 직접 고치면(mtime/size 변경) 캐시가 무효화된다.

    써드파티 세션 편집 프로토콜의 핵심 보장. 웹 API 를 거치지 않았으므로
    서버는 변경을 통보받지 못한다 — 캐시 키의 file_signature 만이 이를 잡는다.
    """
    import os

    proj_mod, calls = patched_render
    project_id = "final-cache-external"
    footage = _make_project(tmp_path, monkeypatch, project_id)

    _call_preview(proj_mod, project_id)
    calls.ffmpeg_commands.clear()

    # 캐시가 실제로 적중하는 상태인지 먼저 확인(대조군).
    _call_preview(proj_mod, project_id)
    assert calls.ffmpeg_commands == [], "대조군: 변경 전에는 캐시가 적중해야 한다"

    # ── 써드파티 세션이 원본 푸티지를 직접 교체 ──
    footage.write_bytes(b"externally_edited_footage_bytes_DIFFERENT_LENGTH")
    st = footage.stat()
    os.utime(footage, (st.st_atime, st.st_mtime + 120))  # mtime 도 명시적으로 전진

    calls.ffmpeg_commands.clear()
    _call_preview(proj_mod, project_id)

    assert calls.ffmpeg_commands, (
        "푸티지 외부 수정 후에도 캐시가 적중함 — 사용자가 고친 내용이 반영되지 않는 "
        "치명적 실패 모드. 캐시 키에 footage file_signature 가 빠졌는지 확인할 것."
    )
