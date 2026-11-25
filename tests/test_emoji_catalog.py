"""이모지 카탈로그 정합성 (#emoji-catalog).

이모지는 백엔드(`EMOJI_ICON_TABLE`, 실제 렌더)와 프론트
(`web/frontend/src/lib/emojiCatalog.ts`, 선택 UI + 미리보기) 두 곳에 있다.
한쪽만 늘리면 "편집기에는 보이는데 렌더하면 안 나온다"는 회귀가 생긴다.

또 하나의 함정: 폰트에 글리프가 없으면 렌더 결과가 두부(□)가 된다.
목록에 넣기 전에 실제로 그려지는지 확인한다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.clip_editor import EMOJI_ICON_TABLE, _find_emoji_font_path  # noqa: E402

CATALOG_TS = ROOT / "web/frontend/src/lib/emojiCatalog.ts"


def _frontend_catalog() -> dict[str, str]:
    text = CATALOG_TS.read_text(encoding="utf-8")
    return dict(re.findall(r'\{ key: "([a-z_]+)", char: "([^"]+)" \}', text))


# ── 원래 있던 4종은 절대 사라지면 안 된다(기존 프로젝트 저장값) ──────────

@pytest.mark.parametrize("key,char", [
    ("question", "❓"), ("lens", "🔍"), ("prohibit", "🚫"), ("poo", "💩"),
])
def test_legacy_keys_preserved(key, char):
    assert EMOJI_ICON_TABLE.get(key) == char


def test_catalog_grew(): 
    assert len(EMOJI_ICON_TABLE) >= 90, f"이모지 {len(EMOJI_ICON_TABLE)}개뿐"


# ── 프론트/백엔드 1:1 ──────────────────────────────────────────────────

def test_frontend_and_backend_match():
    """핵심 회귀: 한쪽만 늘리면 편집기와 렌더 결과가 어긋난다."""
    fe = _frontend_catalog()
    assert fe, "프론트 카탈로그를 파싱하지 못했다"
    assert set(fe) == set(EMOJI_ICON_TABLE), (
        f"프론트에만: {sorted(set(fe) - set(EMOJI_ICON_TABLE))} / "
        f"백엔드에만: {sorted(set(EMOJI_ICON_TABLE) - set(fe))}"
    )
    mismatch = {k: (fe[k], EMOJI_ICON_TABLE[k])
                for k in fe if fe[k] != EMOJI_ICON_TABLE[k]}
    assert not mismatch, f"같은 키인데 글리프가 다르다: {mismatch}"


def test_no_duplicate_glyphs():
    """같은 이모지가 두 키에 들어가면 선택 UI 에서 헷갈린다."""
    seen: dict[str, str] = {}
    dup = []
    for k, ch in EMOJI_ICON_TABLE.items():
        if ch in seen:
            dup.append((seen[ch], k, ch))
        seen[ch] = k
    assert not dup, f"중복 글리프: {dup}"


# ── 실제 렌더 가능 여부(두부 방지) ─────────────────────────────────────

def test_all_glyphs_render():
    """모든 이모지가 폰트로 실제 그려져야 한다(빈 렌더 = 두부)."""
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_emoji_font_path()
    if not font_path:
        pytest.skip("이모지 폰트를 찾을 수 없는 환경")
    font = ImageFont.truetype(font_path, 160)

    blank = []
    for key, ch in EMOJI_ICON_TABLE.items():
        img = Image.new("RGBA", (220, 220), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((10, 10), ch, font=font, embedded_color=True)
        if img.split()[-1].getbbox() is None:
            blank.append((key, ch))
    assert not blank, f"폰트에 글리프가 없어 두부로 렌더됨: {blank}"
