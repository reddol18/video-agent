"""BGM 렌더가 오래된 영상을 재사용하지 않는지 (#bgm-stale-final).

배경: BGM 렌더는 원래 매번 본 렌더를 다시 돌렸다. 그 과정에서 clips/ 와
final_output.mp4 를 통째로 지우기 때문에, 렌더 경로에 회귀가 있으면 멀쩡하던
결과물까지 같이 망가졌다. 그래서 `.bgm_reuse_final` 마커가 있으면 기존
final_output.mp4 를 그대로 쓰도록 했다(#bgm-reuse-final).

그런데 마커만 보고 재사용하니, 그 뒤에 씬을 고쳐 클립을 새로 만들어도
BGM 이 옛 영상 위에 얹혔다(사용자 보고). 마커가 있어도 "최신인지" 는 확인해야 한다.

정책: 씬 클립 중 하나라도 final_output.mp4 보다 새로우면 재사용하지 않는다.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def _should_reuse(output_dir: Path, state: Path | None = None) -> bool:
    """render.py 의 재사용 판정과 동일한 규칙."""
    marker = output_dir / ".bgm_reuse_final"
    final = output_dir / "final_output.mp4"
    if not (marker.exists() and final.exists() and final.stat().st_size > 0):
        return False
    try:
        fm = final.stat().st_mtime
        newer = [c for c in (output_dir / "clips").glob("scene_*.mp4")
                 if c.stat().st_mtime > fm + 1.0]
        state_newer = bool(state and state.exists()
                           and state.stat().st_mtime > fm + 1.0)
        return not newer and not state_newer
    except OSError:
        return False


def _mk(p: Path, mtime: float, size: int = 1024) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))


def test_reuses_when_final_is_current(tmp_path):
    """클립이 영상보다 오래됐으면 재사용한다(=본 렌더 생략)."""
    t = time.time()
    _mk(tmp_path / "clips/scene_001.mp4", t - 100)
    _mk(tmp_path / "final_output.mp4", t)
    (tmp_path / ".bgm_reuse_final").touch()
    assert _should_reuse(tmp_path) is True


def test_rerenders_when_clip_is_newer(tmp_path):
    """핵심 회귀: 씬을 고쳐 클립이 새로워지면 옛 영상을 쓰면 안 된다."""
    t = time.time()
    _mk(tmp_path / "final_output.mp4", t - 100)
    _mk(tmp_path / "clips/scene_001.mp4", t)
    (tmp_path / ".bgm_reuse_final").touch()
    assert _should_reuse(tmp_path) is False


def test_one_newer_clip_is_enough(tmp_path):
    """여러 클립 중 하나만 새로워도 다시 렌더한다."""
    t = time.time()
    _mk(tmp_path / "final_output.mp4", t - 100)
    for i in range(1, 5):
        _mk(tmp_path / f"clips/scene_00{i}.mp4", t - 200)
    _mk(tmp_path / "clips/scene_009.mp4", t)      # 이것만 최신
    (tmp_path / ".bgm_reuse_final").touch()
    assert _should_reuse(tmp_path) is False


def test_no_marker_never_reuses(tmp_path):
    """마커가 없으면 항상 본 렌더(기존 동작)."""
    t = time.time()
    _mk(tmp_path / "final_output.mp4", t)
    _mk(tmp_path / "clips/scene_001.mp4", t - 100)
    assert _should_reuse(tmp_path) is False


def test_missing_final_never_reuses(tmp_path):
    (tmp_path / ".bgm_reuse_final").touch()
    _mk(tmp_path / "clips/scene_001.mp4", time.time())
    assert _should_reuse(tmp_path) is False


def test_one_second_tolerance(tmp_path):
    """파일시스템 mtime 오차로 매번 재렌더되지 않도록 1초 여유를 둔다."""
    t = time.time()
    _mk(tmp_path / "final_output.mp4", t)
    _mk(tmp_path / "clips/scene_001.mp4", t + 0.4)   # 0.4초 차이는 무시
    (tmp_path / ".bgm_reuse_final").touch()
    assert _should_reuse(tmp_path) is True


def test_rerenders_when_scene_edited(tmp_path):
    """자막/효과만 고치면 클립은 그대로지만 state.json 이 새로워진다.

    실사고: 클립 재생성 없이 씬만 편집한 뒤 BGM 을 렌더하니 그 편집이 빠진
    옛 영상에 BGM 이 얹혔다. state.json 시각도 함께 봐야 한다.
    """
    t = time.time()
    _mk(tmp_path / "final_output.mp4", t - 100)
    _mk(tmp_path / "clips/scene_001.mp4", t - 200)      # 클립은 오래됨
    state = tmp_path / "state.json"
    _mk(state, t)                                       # 씬 편집만 최신
    (tmp_path / ".bgm_reuse_final").touch()
    assert _should_reuse(tmp_path, state) is False


def test_reuses_when_state_also_old(tmp_path):
    """편집도 클립도 영상보다 오래되면 그대로 재사용한다."""
    t = time.time()
    _mk(tmp_path / "clips/scene_001.mp4", t - 200)
    state = tmp_path / "state.json"
    _mk(state, t - 150)
    _mk(tmp_path / "final_output.mp4", t)
    (tmp_path / ".bgm_reuse_final").touch()
    assert _should_reuse(tmp_path, state) is True
