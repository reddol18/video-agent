"""세그먼트 연결은 항상 30fps CFR 이어야 한다 (#xfade-vfr-segment).

실제 사고(2회): 최종 영상이 7분 44초에서 잘렸다.

원인 — `_simple_concat` 이 `-c copy` 로 이어붙이면 입력들의 프레임 간격이 그대로
남아 결과가 가변 프레임레이트(VFR)가 된다. 짧은 소재를 슬로모션으로 크게 늘린
클립(프레임이 듬성듬성)이 섞이면 세그먼트 avg_frame_rate 가 21.3fps 같은 값이 되고,
그 세그먼트를 xfade 에 넣으면 뒤쪽 입력이 통째로 사라진다.

ffmpeg 는 오류도 경고도 내지 않는다(rc=0). 그래서 조용히 잘린 영상이 나온다.
실측: 8.87s + 21.17s 를 xfade → 29.53s 가 나와야 하는데 13.70s.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.clip_editor import _simple_concat  # noqa: E402


def _probe(path: Path, field: str) -> str:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={field}", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout.strip()


def _dur(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(r.stdout.strip() or 0)


def _make(path: Path, dur: float, fps: int) -> None:
    """지정 fps 로 클립 생성."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", str(dur),
         "-i", f"testsrc=size=320x180:rate={fps}", "-r", str(fps),
         "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=120,
    )


def test_concat_output_is_30fps(tmp_path):
    """핵심 회귀: 입력 fps 가 섞여도 결과는 30fps 여야 한다."""
    a, b, c = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "out.mp4"
    _make(a, 2.0, 30)
    _make(b, 2.0, 8)          # 슬로모션 클립처럼 프레임이 듬성듬성
    _simple_concat([str(a), str(b)], str(c))
    assert _probe(c, "avg_frame_rate") == "30/1", "VFR 세그먼트는 xfade 를 무너뜨린다"


def test_concat_preserves_duration(tmp_path):
    """길이는 보존된다.

    30fps 로 재타이밍하면서 마지막 프레임 유지분만큼 아주 조금 늘어날 수 있다
    (8fps 입력 기준 최대 프레임 간격 0.125s 수준). 실제 프로젝트 클립
    5개(21.17s)에서는 +0.13s 였다. 잘림만 아니면 된다.
    """
    a, b, c = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "out.mp4"
    _make(a, 2.0, 30)
    _make(b, 3.0, 8)
    _simple_concat([str(a), str(b)], str(c))
    got = _dur(c)
    assert got >= 5.0 - 0.05, f"길이가 줄었다: {got:.2f}s"
    assert got <= 5.0 + 0.5, f"길이가 과하게 늘었다: {got:.2f}s"


def test_xfade_survives_mixed_fps_inputs(tmp_path):
    """정규화된 세그먼트끼리는 xfade 가 길이를 보존한다."""
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    s0, s1 = tmp_path / "s0.mp4", tmp_path / "s1.mp4"
    _make(a, 3.0, 30)
    _make(b, 2.0, 8)
    _simple_concat([str(a)], str(s0))          # 단일도 같은 경로를 타게
    _simple_concat([str(a), str(b)], str(s1))
    d0, d1 = _dur(s0), _dur(s1)
    out = tmp_path / "x.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(s0), "-i", str(s1),
         "-filter_complex",
         f"[0:v][1:v]xfade=transition=fade:duration=0.5:offset={d0 - 0.5:.3f}[v]",
         "-map", "[v]", "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
         "-an", str(out)],
        check=True, timeout=120,
    )
    assert _dur(out) == pytest.approx(d0 + d1 - 0.5, abs=0.5), (
        "xfade 가 뒤쪽 입력을 삼켰다 — 최종 영상이 잘린다"
    )


def test_single_clip_concat_also_normalized(tmp_path):
    """클립이 1개여도 fps 가 정규화되어야 한다(그룹 크기 1인 세그먼트)."""
    a, c = tmp_path / "a.mp4", tmp_path / "out.mp4"
    _make(a, 2.0, 8)
    _simple_concat([str(a)], str(c))
    assert _probe(c, "avg_frame_rate") == "30/1"
