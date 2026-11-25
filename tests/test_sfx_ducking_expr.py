"""SFX 더킹 표현식 회귀 테스트 (#ducking-limit).

씬이 많은 프로젝트에서 최종 합성이 통째로 실패하던 버그를 막는다.
원인: 모든 TTS 구간을 하나의 ffmpeg volume 표현식에 넣어 길이/복잡도
상한(약 1000자)을 넘겼다. → "Error when evaluating the volume expression"
"""
import subprocess

import pytest

from src.final_composer import _build_sfx_track_for_overlay


def _filtergraph(ducks, abs_start=0.5, sfx_path="dummy.wav"):
    parts, out = _build_sfx_track_for_overlay(
        abs_start_sec=abs_start, sfx_path=sfx_path, sfx_volume=0.7,
        ducks=ducks, input_idx=0, label="sfx0",
    )
    return ";".join(parts), out


def _ffmpeg_accepts(filtergraph: str, out_label: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-t", "0.2",
         "-i", "sine=f=1000", "-filter_complex", filtergraph,
         "-map", out_label, "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0, r.stderr


@pytest.mark.parametrize("n_scenes", [1, 31, 71, 200])
def test_ffmpeg_accepts_expression_at_any_scene_count(n_scenes):
    """씬 수가 늘어도 ffmpeg가 표현식을 받아들여야 한다."""
    ducks = [(i * 5.0, i * 5.0 + 2.0) for i in range(n_scenes)]
    fg, out = _filtergraph(ducks)
    ok, err = _ffmpeg_accepts(fg, out)
    assert ok, f"{n_scenes}개 구간에서 ffmpeg 거부:\n{err[-400:]}"


def test_expression_length_does_not_grow_with_scene_count():
    """표현식 길이가 씬 수에 비례해 늘면 결국 상한을 넘는다."""
    short, _ = _filtergraph([(i * 5.0, i * 5.0 + 2.0) for i in range(5)])
    long_, _ = _filtergraph([(i * 5.0, i * 5.0 + 2.0) for i in range(300)])
    assert len(long_) < 1000, f"표현식이 너무 김: {len(long_)}자"
    # 멀리 있는 구간은 걸러지므로 길이가 비슷해야 한다.
    assert len(long_) <= len(short) * 3


def test_only_overlapping_ducks_are_kept():
    """SFX 재생 창에 걸치는 더킹만 남는다."""
    # SFX는 t=100 부근에서만 소리난다. 0~50 구간 더킹은 무의미.
    ducks = [(0.0, 2.0), (10.0, 12.0), (100.0, 102.0)]
    fg, _ = _filtergraph(ducks, abs_start=100.0)
    assert "between(t,100" in fg          # 겹치는 구간은 남고
    assert "between(t,10.000" not in fg   # 먼 구간은 빠진다


def test_no_ducks_passes_through_unchanged():
    """더킹 대상이 없으면 volume envelope를 붙이지 않는다."""
    fg, out = _filtergraph([])
    assert "eval=frame" not in fg
    assert out == "[sfx0_pre]"
