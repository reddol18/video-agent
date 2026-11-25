"""프로젝트 전역 배경색(letterbox 여백) 기능 테스트.

검증 항목:
1. `#RRGGBB` → `0xRRGGBB` 변환 유틸(normalize_bg_color) 단위 테스트.
2. bg_color 미지정(=black) 시 RenderSpec pad 필터 문자열이 기존과 동일함(회귀 방지).
3. bg_color 지정 시 pad 에 색이 반영됨.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# src 임포트 경로 보장
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.ffmpeg_utils import normalize_bg_color
from src.render_spec.models import Canvas, CropBox
from src.render_spec.ffmpeg_mapper import _pad_filter


# ── (1) 변환 유틸 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("#1a2b3c", "0x1A2B3C"),
        ("#FFFFFF", "0xFFFFFF"),
        ("#000000", "0x000000"),
        ("  #12ab34  ", "0x12AB34"),  # 공백 트림
        ("black", "black"),
        ("BLACK", "black"),
        ("white", "white"),
        ("red", "red"),
        (None, "black"),          # 미지정 → 기본 black
        ("", "black"),            # 빈 문자열 → 기본 black
        ("#12ab", "black"),       # 잘못된 길이 → black 폴백
        ("nonsense123", "black"), # 알 수 없는 형식 → black 폴백
    ],
)
def test_normalize_bg_color(raw, expected):
    assert normalize_bg_color(raw) == expected


# ── (2) 기본값(black) 회귀: pad 문자열이 기존과 동일 ─────────────────────────

def test_pad_filter_default_black_unchanged():
    """bg_color 미지정(Canvas 기본 black)이면 pad 필터가 기존 `:black` 형식 그대로."""
    canvas = Canvas(width=1920, height=1080, fps=30)  # bg_color 미지정 → "black"
    pad = CropBox(x=0, y=140, w=1920, h=800)
    result = _pad_filter(pad, canvas)
    assert result == "pad=1920:1080:0:140:black"


def test_pad_filter_explicit_black_same_as_default():
    """명시적 black 도 동일 문자열이어야 회귀 없음."""
    canvas = Canvas(width=1920, height=1080, fps=30, bg_color="black")
    pad = CropBox(x=0, y=140, w=1920, h=800)
    assert _pad_filter(pad, canvas) == "pad=1920:1080:0:140:black"


# ── (3) 지정 시 pad 에 색 반영 ───────────────────────────────────────────────

def test_pad_filter_custom_color_reflected():
    """정규화된 0xRRGGBB 색이 pad 필터에 반영된다."""
    canvas = Canvas(width=1920, height=1080, fps=30, bg_color=normalize_bg_color("#1a2b3c"))
    pad = CropBox(x=0, y=140, w=1920, h=800)
    assert _pad_filter(pad, canvas) == "pad=1920:1080:0:140:0x1A2B3C"


def test_pad_filter_none_pad_returns_empty():
    """pad 이 None(cover)이면 빈 문자열 — 색과 무관하게 기존 동작 유지."""
    canvas = Canvas(width=1920, height=1080, fps=30, bg_color="0xFF0000")
    assert _pad_filter(None, canvas) == ""
