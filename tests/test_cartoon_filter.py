"""만화(cartoon) 필터 vf 체인 생성 + 기존 필터 무회귀 테스트."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from src.clip_editor import (
    _build_cartoon_core_vf,
    _build_filter_vf,
    _detect_content_region,
    _wrap_cartoon_vf_with_content_crop,
    apply_scene_filters,
)


def test_cartoon_returns_vf_chain():
    """cartoon 은 median + eq + edgedetect 3요소를 포함한 vf 체인을 반환한다."""
    vf = _build_filter_vf("cartoon", 1.0)
    assert vf is not None
    assert "median=radius=" in vf
    assert "eq=saturation=" in vf
    assert "edgedetect" in vf


def test_cartoon_intensity_scales():
    """intensity 가 클수록 median radius 와 채도가 커진다 (저조도 색 유지 위해 채도>=1.4)."""
    low = _build_filter_vf("cartoon", 0.0)
    high = _build_filter_vf("cartoon", 1.0)
    assert "median=radius=1" in low
    assert "median=radius=3" in high
    # 채도는 항상 1.4 이상 유지 (저조도 색 죽음 방지)
    assert "saturation=1.4" in low


def test_cartoon_intensity_clamped():
    """범위 밖 intensity 는 0~1 로 클램프되어 예외 없이 반환된다."""
    assert _build_filter_vf("cartoon", -5.0) is not None
    assert _build_filter_vf("cartoon", 99.0) is not None


def test_existing_filters_unchanged():
    """기존 7종 필터 무회귀 — cartoon 추가가 기존 반환을 건드리지 않는다."""
    assert _build_filter_vf("flashback", 1.0) is not None
    assert _build_filter_vf("horror", 1.0) is not None
    assert _build_filter_vf("vivid", 1.0) is not None
    assert _build_filter_vf("nightvision", 1.0) is not None
    assert _build_filter_vf("warm", 1.0) is not None
    assert _build_filter_vf("cool", 1.0) is not None
    # pov 는 vf 로 표현 불가 → None (호출자가 PNG composite 로 처리)
    assert _build_filter_vf("pov", 1.0) is None
    # 알 수 없는 타입 → None
    assert _build_filter_vf("unknown_xyz", 1.0) is None


# ── #letterbox-edge: 콘텐츠 영역 crop 래핑 로직 테스트 ─────────────────────

def test_wrap_no_region_returns_core_unchanged():
    """여백 없음(region=None)이면 코어 vf 를 그대로 반환 — fit=cover 무회귀."""
    core = _build_cartoon_core_vf(1.0)
    wrapped = _wrap_cartoon_vf_with_content_crop(core, None, 1080, 1920)
    assert wrapped == core
    assert "crop=" not in wrapped
    assert "pad=" not in wrapped


def test_wrap_with_region_adds_crop_and_pad():
    """레터박스 있으면 crop→코어→pad 로 감싼다 (여백 경계를 edgedetect 가 못 보게)."""
    core = _build_cartoon_core_vf(1.0)
    # 세로 1080x1920, 콘텐츠가 1080x608 로 상하 레터박스 (y=656).
    region = (1080, 608, 0, 656)
    wrapped = _wrap_cartoon_vf_with_content_crop(core, region, 1080, 1920)
    assert wrapped.startswith("crop=1080:608:0:656,")
    assert core in wrapped
    assert wrapped.endswith("pad=1080:1920:0:656:color=black")


# ── #letterbox-edge: 실제 렌더 픽셀 검증 스모크 ───────────────────────────

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe 필요")
def test_cartoon_no_edge_on_letterbox_boundary(tmp_path):
    """fit=contain 세로 영상(상하 레터박스)에 cartoon 적용 시,
    레터박스 경계 y좌표와 프레임 최외곽 행이 순수 검정(엣지선 없음)이어야 한다.
    """
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")

    W, H = 1080, 1920
    content_h = 608                      # round=2 짝수
    pad_top = (H - content_h) // 2       # 656 (짝수)
    # 콘텐츠 영역만 유채색(격자 무늬로 엣지 유발), 나머지는 검정 레터박스.
    src = tmp_path / "src.mp4"
    # 컬러 콘텐츠(testsrc) 를 상하 검정으로 pad → fit=contain letterbox 재현.
    build = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={W}x{content_h}:rate=15:duration=1",
            "-vf", f"pad={W}:{H}:0:{pad_top}:color=black",
            "-pix_fmt", "yuv420p", "-frames:v", "15", str(src),
        ],
        capture_output=True,
    )
    assert build.returncode == 0, build.stderr.decode(errors="ignore")[:400]

    dst = tmp_path / "out.mp4"
    ok = apply_scene_filters(
        str(src), str(dst),
        [{"filter_type": "cartoon", "intensity": 1.0}],
        W, H,
    )
    assert ok and dst.exists() and dst.stat().st_size > 0

    # 출력 첫 프레임을 PNG 로 추출해 픽셀 검사.
    frame = tmp_path / "frame.png"
    ext = subprocess.run(
        ["ffmpeg", "-y", "-i", str(dst), "-frames:v", "1", str(frame)],
        capture_output=True,
    )
    assert ext.returncode == 0, ext.stderr.decode(errors="ignore")[:400]
    arr = np.asarray(Image.open(str(frame)).convert("RGB"))

    # 레터박스 여백(콘텐츠 위/아래)은 순수 검정이어야 한다 — edgedetect 선 없음.
    # yuv420p 왕복 오차 허용치.
    THRESH = 12
    # 상단 여백 중간 행 (경계 바로 위쪽 여러 행)
    for yy in range(4, pad_top - 4):
        row = arr[yy]
        assert row.max() <= THRESH, (
            f"상단 레터박스 y={yy} 에 비검정 픽셀(max={row.max()}) — edgedetect 엣지 잔존"
        )
    # 하단 여백
    for yy in range(pad_top + content_h + 4, H - 4):
        row = arr[yy]
        assert row.max() <= THRESH, (
            f"하단 레터박스 y={yy} 에 비검정 픽셀(max={row.max()}) — edgedetect 엣지 잔존"
        )
    # 레터박스 경계 바로 그 행(콘텐츠 시작 직전/직후 여백)도 검정.
    assert arr[pad_top - 1].max() <= THRESH, "상단 경계선 y 에 엣지 잔존"
    assert arr[pad_top + content_h].max() <= THRESH, "하단 경계선 y 에 엣지 잔존"


# ── #cartoon-halfcrop: 어두운 콘텐츠를 여백으로 오인해 절반만 crop 하는 회귀 방지 ──

@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe 필요")
def test_detect_region_rejects_asymmetric_dark_content(tmp_path):
    """콘텐츠 오른쪽 절반이 검정(비대칭)이면 레터박스로 오인하지 않고 None 반환.

    회귀 재현: 오른쪽이 검정인 콘텐츠 → cropdetect 는 crop=540:1920:0:0(왼쪽 절반)을
    뱉는다. 대칭성 검증이 없으면 crop→pad 로 오른쪽 절반이 검정 출력된다.
    """
    W, H = 1080, 1920
    src = tmp_path / "halfdark.mp4"
    build = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=white:s={W // 2}x{H}:d=1",
            "-f", "lavfi", "-i", f"color=c=black:s={W // 2}x{H}:d=1",
            "-filter_complex", "[0][1]hstack=inputs=2[v]",
            "-map", "[v]", "-pix_fmt", "yuv420p", "-frames:v", "12", str(src),
        ],
        capture_output=True,
    )
    assert build.returncode == 0, build.stderr.decode(errors="ignore")[:400]
    # 비대칭 여백 → 레터박스 아님 → None(전체 프레임, crop 안 함).
    assert _detect_content_region(str(src), W, H) is None


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe 필요")
def test_detect_region_accepts_symmetric_letterbox(tmp_path):
    """상하 대칭 레터박스는 정상적으로 콘텐츠 영역을 crop 대상으로 감지한다."""
    W, H = 1080, 1920
    content_h = 608
    pad_top = (H - content_h) // 2  # 656
    src = tmp_path / "letterbox.mp4"
    build = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={W}x{content_h}:rate=15:duration=1",
            "-vf", f"pad={W}:{H}:0:{pad_top}:color=black",
            "-pix_fmt", "yuv420p", "-frames:v", "12", str(src),
        ],
        capture_output=True,
    )
    assert build.returncode == 0, build.stderr.decode(errors="ignore")[:400]
    region = _detect_content_region(str(src), W, H)
    assert region is not None, "대칭 레터박스인데 감지 실패"
    w, h, x, y = region
    # 폭은 전체, 높이는 콘텐츠 근사, 상하 대칭.
    assert x == 0 and w == W
    assert abs(y - pad_top) <= 8 and abs(h - content_h) <= 8


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe 필요")
def test_cartoon_full_frame_cover_still_applies(tmp_path):
    """fit=cover(여백 없음) 영상은 crop 없이 전체 프레임에 cartoon 이 정상 적용된다."""
    pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    np = pytest.importorskip("numpy")

    W, H = 640, 640
    src = tmp_path / "src.mp4"
    build = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={W}x{H}:rate=15:duration=1",
            "-pix_fmt", "yuv420p", "-frames:v", "15", str(src),
        ],
        capture_output=True,
    )
    assert build.returncode == 0

    dst = tmp_path / "out.mp4"
    ok = apply_scene_filters(
        str(src), str(dst),
        [{"filter_type": "cartoon", "intensity": 1.0}],
        W, H,
    )
    assert ok and dst.exists() and dst.stat().st_size > 0

    frame = tmp_path / "frame.png"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(dst), "-frames:v", "1", str(frame)],
        capture_output=True,
    )
    arr = np.asarray(Image.open(str(frame)).convert("RGB"))
    # 전체 프레임이 검정으로 뭉개지지 않고 색이 살아있어야 한다 (cartoon 적용 확인).
    assert arr.max() > 40, "cover 영상에 cartoon 이 적용되지 않았거나 전체가 검정"
