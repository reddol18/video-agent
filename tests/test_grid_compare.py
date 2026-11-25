"""N×M 그리드 비교 + 광원 그라데이션 보더 — 순수 함수 단위 테스트 + FFmpeg 스모크.

대상 (src/utils/ffmpeg_utils.py):
- compute_grid_cells: N×M 셀 좌표/크기 계산 (세로 1080×1920 정확성 포함)
- normalize_gradient_color: hex/named/폴백 색 파싱
- build_grid_cell_filter: 셀 필터 문자열 (보더 유무/animate 분기, 보더 offset)
- build_grid_compare: 실제 FFmpeg 렌더 스모크 (2×1 그리드 + 보더)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.utils.ffmpeg_utils import (
    build_grid_cell_filter,
    build_grid_compare,
    compute_grid_cells,
    normalize_gradient_color,
)


# ── compute_grid_cells ───────────────────────────────────────────────────────

def test_grid_cells_2x1_vertical_exact_cover():
    """세로 1080×1920, 2행×1열 — 셀이 화면 전체를 정확히 덮고 경계가 맞아야."""
    cells = compute_grid_cells(rows=2, cols=1, out_w=1080, out_h=1920)
    assert cells == [
        (0, 0, 1080, 960),
        (0, 960, 1080, 960),
    ]
    # 전체 면적 = 화면 면적
    assert sum(w * h for (_, _, w, h) in cells) == 1080 * 1920


def test_grid_cells_2x2_odd_remainder_absorbed():
    """나머지 픽셀은 마지막 행/열이 흡수하여 빈틈이 없어야 한다."""
    cells = compute_grid_cells(rows=2, cols=2, out_w=1081, out_h=1921)
    # base_w=540, base_h=960 → 마지막 열/행이 +1 흡수
    assert cells == [
        (0, 0, 540, 960),
        (540, 0, 541, 960),
        (0, 960, 540, 961),
        (540, 960, 541, 961),
    ]
    assert sum(w * h for (_, _, w, h) in cells) == 1081 * 1921


def test_grid_cells_3x2_row_major_order():
    """행 우선(row-major) 순서 — 좌상→우, 다음 행."""
    cells = compute_grid_cells(rows=3, cols=2, out_w=1080, out_h=1920)
    assert len(cells) == 6
    # y 좌표가 0,0,640,640,1280,1280 순
    ys = [y for (_, y, _, _) in cells]
    assert ys == [0, 0, 640, 640, 1280, 1280]


def test_grid_cells_invalid_dims_raise():
    with pytest.raises(ValueError):
        compute_grid_cells(rows=0, cols=2, out_w=1080, out_h=1920)


# ── normalize_gradient_color ─────────────────────────────────────────────────

def test_gradient_color_hex():
    assert normalize_gradient_color("#00e5ff") == "0x00E5FF"


def test_gradient_color_named_passthrough():
    assert normalize_gradient_color("cyan") == "cyan"


def test_gradient_color_fallback():
    assert normalize_gradient_color(None) == "0x00E5FF"
    assert normalize_gradient_color("", fallback="0xABCDEF") == "0xABCDEF"
    assert normalize_gradient_color("not-a-color!", fallback="0x111111") == "0x111111"


# ── build_grid_cell_filter ───────────────────────────────────────────────────

def test_cell_filter_no_border_matches_plain_scale():
    """보더 없으면 gradients/overlay 없이 scale+pad 만 (기존 xstack 셀 동등)."""
    parts = build_grid_cell_filter(
        input_idx=0, cell_w=540, cell_h=960, out_label="cell0",
        pts_ratio=1.0, border=None,
    )
    assert len(parts) == 1
    s = parts[0]
    assert "gradients" not in s
    assert "overlay" not in s
    assert "scale=540:960" in s
    assert s.endswith("[cell0]")


def test_cell_filter_border_offset_and_inner_size():
    """보더 있으면 gradients 소스 + 이미지 (width_px 안쪽) overlay."""
    parts = build_grid_cell_filter(
        input_idx=1, cell_w=540, cell_h=960, out_label="cell1",
        pts_ratio=1.0,
        border={"width_px": 20, "color_from": "#00e5ff", "color_to": "#a855f7", "animate": True, "speed": 1.0},
    )
    joined = ";".join(parts)
    # 그라데이션 소스는 셀 크기
    assert "gradients=s=540x960" in joined
    assert "0x00E5FF" in joined and "0xA855F7" in joined
    # 이미지는 (셀 - 2*보더) 크기로 스케일
    assert "scale=500:920" in joined
    # 보더 두께만큼 안쪽에 overlay
    assert "overlay=x=20:y=20" in joined
    # animate=True → hue 회전 포함
    assert "hue=H=2*PI" in joined
    assert parts[-1].endswith("[cell1]")


def test_cell_filter_animate_off_no_hue():
    parts = build_grid_cell_filter(
        input_idx=0, cell_w=400, cell_h=700, out_label="cell0",
        pts_ratio=1.0,
        border={"width_px": 10, "animate": False},
    )
    joined = ";".join(parts)
    assert "gradients=" in joined
    assert "hue=H=2*PI" not in joined


def test_cell_filter_border_clamped_to_half_cell():
    """보더가 셀 절반을 넘으면 콘텐츠가 사라지지 않도록 clamp 되어야."""
    parts = build_grid_cell_filter(
        input_idx=0, cell_w=100, cell_h=100, out_label="cell0",
        pts_ratio=1.0,
        border={"width_px": 999, "animate": False},
    )
    joined = ";".join(parts)
    # inner_w = 100 - 2*bw > 0 이어야 함 (clamp 로 bw <= 49)
    assert "scale=2:2" in joined  # bw=49 → inner=2


# ── FFmpeg 렌더 스모크 (로컬 ffmpeg만, 외부 API/크레딧 없음) ──────────────────

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 미설치")
def test_build_grid_compare_smoke(tmp_path: Path):
    """2×1 그리드 + 보더로 짧은 mp4 를 실제 렌더 → 0바이트 아님 + 해상도 1080×1920."""
    # 테스트 소스 2개 생성 (testsrc, 짧게)
    src0 = tmp_path / "s0.mp4"
    src1 = tmp_path / "s1.mp4"
    for src, pat in ((src0, "testsrc"), (src1, "testsrc2")):
        r = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"{pat}=s=320x240:d=1:r=10",
             "-t", "1", "-c:v", "libx264", "-crf", "30", "-an", str(src)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr

    dst = tmp_path / "grid.mp4"
    build_grid_compare(
        clip_paths=[str(src0), str(src1)],
        dst=str(dst),
        rows=2, cols=1,
        target_duration=1.0,
        borders=[
            {"width_px": 20, "color_from": "#00e5ff", "color_to": "#a855f7", "animate": True, "speed": 1.0},
            None,
        ],
        output_duration=0.5,
        out_w=1080, out_h=1920,
    )
    assert dst.exists()
    assert dst.stat().st_size > 0

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(dst)],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "1080,1920"
