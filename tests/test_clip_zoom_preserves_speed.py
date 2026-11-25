"""clip_zoom 모션이 footage_speed(슬로모션/배속)를 지우지 않는지 검증 (#zoompan-speed).

실제 사고: 씬 26 에 0.55배 슬로모션 + clip_zoom 을 같이 지정했더니
렌더 결과가 12.66s 가 아니라 6.97s(=원본 길이)로 나왔다.

원인: footage_speed 는 setpts 로 적용되는데 setpts 는 프레임을 새로 만들지 않고
표시 시각만 늘린다(208프레임 그대로). 그 뒤 zoompan(d=1:fps=N) 이 같은 208프레임을
N fps 로 재타이밍하면서 길이가 원본으로 되돌아갔다.

해결: zoompan 앞에 fps 필터를 둬서 늘어난 시간만큼 프레임을 실제로 복제시킨다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.clip_editor import apply_scene_motions  # noqa: E402

# 실제 렌더 파이프라인과 같은 인코더. libx264 로 인코딩하면 ffmpeg 가
# r_frame_rate 를 실제 프레임 간격(15)으로 다시 계산해버려, "메타는 30fps 인데
# 프레임은 듬성듬성" 이라는 버그 재현 조건이 사라진다.
# 씬 26 실측값 1/0.5476566 ≈ 1.826 — 비정수여야 버그가 재현된다.
_PTS_FACTOR = 1.826

_ENCODER = ["-c:v", "h264_videotoolbox", "-q:v", "50", "-b:v", "0", "-pix_fmt", "yuv420p"]

ZOOM = [{
    "motion_type": "clip_zoom",
    "start_sec": 1.0,
    "duration_sec": 2.0,
    "params": {"target_x": 0.5, "target_y": 0.5, "start_scale": 1.0, "end_scale": 1.3},
}]


def _duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(r.stdout.strip() or 0.0)


@pytest.fixture()
def slowed_clip(tmp_path):
    """4초 소스를 0.5476배(=실제 씬 26 값)로 느리게 만든 클립 → 약 7.3초.

    재현 조건이 까다롭다. setpts 배율이 정수(예: 2.0)이면 ffmpeg 가 컨테이너
    r_frame_rate 를 실제 간격(15fps)으로 다시 계산해버려, zoompan 이 길이를
    되돌리지 못한다. 실제 사고처럼 배율이 비정수(1.826…)일 때만
    "메타는 30fps 인데 프레임은 듬성듬성" 상태가 남아 버그가 재현된다.
    """
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", "4",
         "-i", "testsrc=size=640x360:rate=30", str(src)],
        check=True, timeout=120,
    )
    slowed = tmp_path / "slowed.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-vf", f"setpts={_PTS_FACTOR}*PTS", "-an", *_ENCODER, str(slowed)],
        check=True, timeout=120,
    )
    assert _duration(str(slowed)) == pytest.approx(4.0 * _PTS_FACTOR, abs=0.3)
    return slowed


def test_clip_zoom_preserves_slow_motion_length(tmp_path, slowed_clip):
    """핵심 회귀: zoompan 이 슬로모션을 원본 길이로 되돌리면 안 된다."""
    dst = tmp_path / "out.mp4"
    assert apply_scene_motions(str(slowed_clip), str(dst), ZOOM, 640, 360)
    expected = 4.0 * _PTS_FACTOR
    out = _duration(str(dst))
    assert out == pytest.approx(expected, abs=0.4), (
        f"슬로모션이 소실됐다: {expected:.2f}s 입력 → {out:.2f}s 출력 "
        f"(4.0s 근처면 zoompan 이 속도를 지운 것)"
    )


def test_clip_zoom_preserves_speedup_length(tmp_path):
    """배속(빠르게)도 마찬가지로 보존되어야 한다."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", "6",
         "-i", "testsrc=size=640x360:rate=30", str(src)],
        check=True, timeout=120,
    )
    fast = tmp_path / "fast.mp4"
    cfr = tmp_path / "cfr_fast.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-r", "30",
         "-an", str(cfr)],
        check=True, timeout=120,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(cfr),
         "-vf", "setpts=0.5*PTS", "-an",
         *_ENCODER, str(fast)],
        check=True, timeout=120,
    )
    assert _duration(str(fast)) == pytest.approx(3.0, abs=0.3)

    dst = tmp_path / "out.mp4"
    assert apply_scene_motions(str(fast), str(dst), ZOOM, 640, 360)
    assert _duration(str(dst)) == pytest.approx(3.0, abs=0.4)


def test_clip_zoom_without_speed_change_is_unaffected(tmp_path):
    """속도 변경이 없는 일반 클립은 기존 동작 그대로(길이 보존)."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", "5",
         "-i", "testsrc=size=640x360:rate=30", str(src)],
        check=True, timeout=120,
    )
    dst = tmp_path / "out.mp4"
    assert apply_scene_motions(str(src), str(dst), ZOOM, 640, 360)
    assert _duration(str(dst)) == pytest.approx(5.0, abs=0.4)
