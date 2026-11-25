"""흑백(1채널) 이미지로도 Ken Burns 가 동작하는지 검증 (#grayscale-kenburns).

실제 사고: 씬 53 의 소스가 흑백 PNG(mode=L)였는데
`cv2.imread(IMREAD_UNCHANGED)` 가 이를 2차원 배열(H, W)로 읽어오면서
채널 수를 참조하는 `img.shape[2]` 가 "tuple index out of range" 로 터졌다.

결과가 조용하지 않았다 — Ken Burns 가 실패하면서 해당 씬의 클립이 아예
생성되지 않았고, 씬 53 이 영상에서 통째로 빠지며 뒤 씬들의 TTS 타이밍이
밀려 나레이션이 겹쳤다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")

from src.utils.ken_burns import generate_moviepy_clip  # noqa: E402

VIDEO_SIZE = (640, 360)


def _duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(r.stdout.strip() or 0.0)


def _render(img: Path, out: Path, duration: float = 2.0) -> bool:
    return generate_moviepy_clip(
        str(img), str(out), duration,
        1.0, 1.2, VIDEO_SIZE, "cover", 0.5, 0.5,
    )


def test_grayscale_png_renders(tmp_path):
    """핵심 회귀: 흑백 PNG 가 조용히 실패하면 씬이 통째로 사라진다."""
    img = tmp_path / "gray.png"
    # mode=L 로 저장되도록 2차원 배열을 그대로 기록한다.
    cv2.imwrite(str(img), np.full((360, 640), 128, dtype=np.uint8))
    assert cv2.imread(str(img), cv2.IMREAD_UNCHANGED).ndim == 2, "흑백 재현 실패"

    out = tmp_path / "out.mp4"
    assert _render(img, out), "흑백 이미지에서 Ken Burns 가 실패했다"
    assert out.exists() and out.stat().st_size > 0
    assert _duration(out) == pytest.approx(2.0, abs=0.3)


def test_color_png_still_renders(tmp_path):
    """일반 컬러 이미지는 기존 동작 그대로."""
    img = tmp_path / "color.png"
    cv2.imwrite(str(img), np.full((360, 640, 3), 90, dtype=np.uint8))
    out = tmp_path / "out.mp4"
    assert _render(img, out)
    assert _duration(out) == pytest.approx(2.0, abs=0.3)


def test_rgba_png_still_renders(tmp_path):
    """알파 채널 PNG 도 기존 동작 그대로(투명 → 검정 합성)."""
    img = tmp_path / "rgba.png"
    rgba = np.full((360, 640, 4), 200, dtype=np.uint8)
    rgba[:, :, 3] = 128
    cv2.imwrite(str(img), rgba)
    out = tmp_path / "out.mp4"
    assert _render(img, out)
    assert _duration(out) == pytest.approx(2.0, abs=0.3)


def _mean_rgb(video: Path, at_sec: float = 1.0) -> tuple[float, float, float]:
    """영상 한 프레임의 평균 RGB. ffmpeg → rawvideo 로 읽어 numpy 로 평균."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at_sec), "-i", str(video),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=60,
    )
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    w, h = VIDEO_SIZE
    buf = buf[: w * h * 3].reshape(h, w, 3)
    return tuple(float(buf[:, :, c].mean()) for c in range(3))  # type: ignore[return-value]


def test_16bit_png_keeps_colors(tmp_path):
    """#16bit-png 회귀: 16비트 PNG 의 색이 그대로 보존되어야 한다.

    실제 사고: 16비트 PNG 는 cv2 가 0~65535 uint16 으로 읽는데, 알파 합성이
    255 로 나누는 것을 전제해 alpha=257 이 되어 색이 폭주했다. uint8 캐스팅에서
    래핑되며 흰색이 청록/분홍 노이즈로 깨져 나왔다(자막·표가 읽을 수 없게 됨).
    """
    img = tmp_path / "deep.png"
    # 16비트 흰색(65535) + 불투명 알파. 8비트로 정규화하면 255 여야 한다.
    rgba16 = np.full((360, 640, 4), 65535, dtype=np.uint16)
    cv2.imwrite(str(img), rgba16)
    assert cv2.imread(str(img), cv2.IMREAD_UNCHANGED).dtype == np.uint16, "16비트 재현 실패"

    out = tmp_path / "out16.mp4"
    assert _render(img, out), "16비트 PNG 에서 Ken Burns 가 실패했다"
    r, g, b = _mean_rgb(out)
    # 흰색이 유지되어야 한다 — 깨지면 채널이 제각각 낮은 값으로 흩어진다.
    assert min(r, g, b) > 230, f"16비트 흰색이 깨졌다: RGB=({r:.0f},{g:.0f},{b:.0f})"
    assert max(r, g, b) - min(r, g, b) < 12, (
        f"채널 간 편차가 크다(색 틀어짐): RGB=({r:.0f},{g:.0f},{b:.0f})"
    )
