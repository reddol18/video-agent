"""bg-preview-fix — 프리뷰 speed 단계 배경색 회귀 방지 테스트.

배경: 프로젝트 전역 배경색(bg_color)이 최종 렌더에는 반영되지만, 프리뷰의
footage_speed != 1.0 경로에서 speed 단계 Canvas 에 bg_color 가 전달되지 않아
letterbox 여백이 검정으로 구워지던 회귀. 또 speed 캐시 키에 bg_color 가 없어
배경색을 바꿔도 옛 검정 letterbox 캐시가 재사용되던 문제.

검증 항목:
1. black(기본)일 때 SpeedStageKey payload/hash 가 pre-fix 와 바이트 동일 — 회귀 안전.
2. 배경색이 non-black 이면 캐시 키가 바뀌어 옛 캐시가 무효화된다.
3. Canvas 가 bg_color 를 그대로 보관한다(speed/scale 단계 pad 색 주입 경로).
"""

from src.render_spec.models import Canvas
from src.render_spec.preview_stages import SpeedStageKey

_BASE = dict(
    prev_hash="abc123",
    footage_speed=1.5,
    canvas_w=1080,
    canvas_h=1920,
    fit="contain",
)


def test_black_bg_cache_key_is_regression_stable():
    """black(기본/미지정)이면 payload 에 bg 키가 없고 hash 가 기본 키와 동일."""
    k_black = SpeedStageKey(**_BASE, bg_color="black")
    k_default = SpeedStageKey(**_BASE)  # bg_color 기본값 = "black"

    assert "bg" not in k_black.to_payload()
    assert k_black.to_payload() == k_default.to_payload()
    assert k_black.hash() == k_default.hash()


def test_non_black_bg_changes_cache_key():
    """배경색이 non-black 이면 payload 에 bg 가 들어가고 hash 가 달라진다(캐시 무효화)."""
    k_black = SpeedStageKey(**_BASE, bg_color="black")
    k_blue = SpeedStageKey(**_BASE, bg_color="#0084ff")
    k_red = SpeedStageKey(**_BASE, bg_color="#ff0000")

    assert k_blue.to_payload().get("bg") == "#0084ff"
    assert k_blue.hash() != k_black.hash()
    assert k_red.hash() != k_blue.hash()


def test_canvas_carries_bg_color():
    """Canvas 기본은 black, 명시 시 raw hex 를 그대로 보관(pad 필터로 직역됨)."""
    assert Canvas(width=1080, height=1920, fps=30).bg_color == "black"
    assert Canvas(width=1080, height=1920, fps=30, bg_color="#0084ff").bg_color == "#0084ff"
