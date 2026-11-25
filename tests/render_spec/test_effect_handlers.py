"""이펙트 백엔드 핸들러 레지스트리 회귀 테스트 (#322).

#306 1단계에서 도입된 매니페스트(`EFFECT_REGISTRY`) 가 *메타데이터* 만 다루는 반면,
2단계(#322)에서 도입된 **핸들러 레지스트리**(`PY_EFFECT_HANDLERS_*`) 는 백엔드
ffmpeg/PIL 합성 진입점을 카테고리 단위로 통합한다. 본 테스트는 다음을 보장한다.

1. **6개 카테고리 모두 핸들러가 등록되어 있다** — 누락 시 builder.py / project.py 의
   단일 루프가 해당 카테고리를 *조용히 스킵* 해 미리보기/RenderSpec 회귀가 발생한다.
2. **카테고리 키가 EFFECT_REGISTRY 의 카테고리 집합과 일치한다** — 매니페스트와 핸들러가
   하나라도 어긋나면 신규 효과 추가 시 자동 등장 약속이 깨진다.
3. **builder.py 가 RenderSpec 빌드 시 핸들러 레지스트리를 *실제로 호출* 한다** — 단위
   테스트가 빌드 결과 layers 의 구성을 직접 검증한다.

회귀 안전성 검증: 본 PR 의 일원화는 *호출만* 한 곳으로 모았으므로, ``build_render_spec``
출력 RenderSpec 의 layer 수/순서/필드값은 직전 버전과 동일해야 한다.
"""

from __future__ import annotations

import pytest

from src.render_spec import (
    EFFECT_REGISTRY,
    PY_EFFECT_HANDLERS_PREVIEW,
    PY_EFFECT_HANDLERS_RENDER_SPEC,
    build_render_spec,
    get_preview_handler,
    get_render_spec_handler,
)
from src.render_spec.models import (
    FilterLayer,
    GraphicLayer,
    ImageLayer,
    MosaicLayer,
    MotionLayer,
    ShapeLayer,
    Size,
    TextLayer,
    VideoLayer,
)


# ── 1) 6개 카테고리 모두 등록되었는가 ──────────────────────────────────────

CATEGORIES = ("text", "shape", "graphic", "filter", "motion", "mosaic")


def test_render_spec_handlers_cover_all_six_categories():
    """6개 카테고리 모두 RenderSpec 변환 핸들러가 등록되어 있어야 한다."""
    missing = [c for c in CATEGORIES if c not in PY_EFFECT_HANDLERS_RENDER_SPEC]
    assert not missing, (
        f"RenderSpec 핸들러 누락 카테고리: {missing} — "
        "효과 매니페스트 파일에서 register_render_spec_handler 등록을 확인하세요."
    )


def test_preview_handlers_cover_all_six_categories():
    """6개 카테고리 모두 미리보기 합성 핸들러가 등록되어 있어야 한다."""
    missing = [c for c in CATEGORIES if c not in PY_EFFECT_HANDLERS_PREVIEW]
    assert not missing, (
        f"Preview 핸들러 누락 카테고리: {missing} — "
        "효과 매니페스트 파일에서 register_preview_handler 등록을 확인하세요."
    )


def test_handler_keys_match_effect_registry_categories():
    """핸들러 카테고리 집합이 EFFECT_REGISTRY 의 카테고리 집합과 정확히 일치한다.

    한쪽에만 카테고리가 있으면 신규 효과 추가 시 매니페스트 1개 수정으로 끝나는 약속이
    깨진다. (예: 핸들러는 등록됐는데 EFFECT_REGISTRY 에는 효과가 없거나, 반대)
    """
    registry_cats = {eff.category for eff in EFFECT_REGISTRY.values()}
    render_handler_cats = set(PY_EFFECT_HANDLERS_RENDER_SPEC.keys())
    preview_handler_cats = set(PY_EFFECT_HANDLERS_PREVIEW.keys())
    # 집중선(speed_lines)은 EFFECT_REGISTRY(=EffectAddPanel 버튼)에 넣지 않는 별도 오버레이
    # 타입이며 미리보기 핸들러만 등록한다. 렌더/미리보기 동일성은
    # tests/test_speed_lines.py 및 apply_speed_lines 경로로 보장된다.
    _PREVIEW_ONLY_STANDALONE = {"speed_lines"}
    assert registry_cats == render_handler_cats
    assert registry_cats == (preview_handler_cats - _PREVIEW_ONLY_STANDALONE)


def test_get_render_spec_handler_returns_callable():
    """get_render_spec_handler 헬퍼는 6개 카테고리 모두에 대해 callable 을 반환한다."""
    for c in CATEGORIES:
        h = get_render_spec_handler(c)
        assert callable(h), f"카테고리 {c} 의 RenderSpec 핸들러가 callable 이 아님"


def test_get_preview_handler_returns_callable():
    for c in CATEGORIES:
        h = get_preview_handler(c)
        assert callable(h), f"카테고리 {c} 의 Preview 핸들러가 callable 이 아님"


def test_get_handler_returns_none_for_unknown_category():
    assert get_render_spec_handler("nonexistent") is None  # type: ignore[arg-type]
    assert get_preview_handler("nonexistent") is None  # type: ignore[arg-type]


# ── 2) 핸들러 중복 등록 시 즉시 에러 ──────────────────────────────────────

def test_duplicate_render_spec_handler_registration_raises():
    from src.render_spec.effects._handlers import register_render_spec_handler

    with pytest.raises(RuntimeError, match="중복 등록"):
        @register_render_spec_handler("text")  # 이미 등록됨
        def _dup(scene, canvas, duration_sec):
            return []


def test_duplicate_preview_handler_registration_raises():
    from src.render_spec.effects._handlers import register_preview_handler

    with pytest.raises(RuntimeError, match="중복 등록"):
        @register_preview_handler("filter")  # 이미 등록됨
        def _dup(*args, **kwargs):
            return True


# ── 3) builder.py 가 핸들러를 통해 layers 를 실제로 채우는지 ──────────────

class _FakeFootage:
    def __init__(self, fit="contain"):
        self.type = "clip"
        self.file = "test.mp4"
        self.fit = fit
        self.rotation = 0


class _FakeProject:
    output_width = 1920
    output_height = 1080


class _FakeTextOverlay:
    text = "안녕"
    x = 0.5
    y = 0.1
    font_size = 40
    color = "#FFFFFF"
    align = "center"


class _FakeShapeOverlay:
    shape = "rect"
    x = 0.1
    y = 0.1
    width = 0.2
    height = 0.2
    color = "#FF0000"


class _FakeGraphicOverlay:
    type = "spotlight"
    x = 0.5
    y = 0.5
    params = {}


class _FakeFilter:
    filter_type = "flashback"
    intensity = 0.5
    params = {}


class _FakeMotion:
    motion_type = "shake"
    start_sec = 0.0
    duration_sec = 1.0
    params = {}


class _FakeMosaic:
    shape = "rect"
    x = 0.4
    y = 0.4
    width = 0.2
    height = 0.2
    mode = "pixelate"
    pixel_size = 20
    blur_radius = 20


class _FakeScene:
    """builder.build_render_spec 에 들어갈 SceneCard-like."""
    scene_id = 1
    duration_hint_sec = 5.0
    footage_speed = 1.0
    footage = _FakeFootage()
    text_overlays = [_FakeTextOverlay()]
    shape_overlays = [_FakeShapeOverlay()]
    graphic_overlays = [_FakeGraphicOverlay()]
    mosaic_overlays = [_FakeMosaic()]
    filters = [_FakeFilter()]
    motions = [_FakeMotion()]


def test_build_render_spec_uses_handler_registry():
    """build_render_spec 결과 layers 가 6개 카테고리 모두에서 Layer 1개씩 + footage 1개를 포함한다.

    카테고리별 적용 순서(filter → motion → mosaic → text → shape → graphic)도 함께 검증한다.
    이는 미리보기와 최종 출력의 합성 결과가 같아야 한다는 일관성 보장이다.
    """
    scene = _FakeScene()
    spec = build_render_spec(
        scene, _FakeProject(),
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=5.0,
    )

    # footage 1 + filter 1 + motion 1 + mosaic 1 + text 1 + shape 1 + graphic 1 = 7
    assert len(spec.layers) == 7

    # 적용 순서 검증 (footage 가장 아래, 그 다음 _EFFECT_CATEGORY_ORDER 순서)
    layer_types = [type(l).__name__ for l in spec.layers]
    assert layer_types == [
        "VideoLayer",
        "FilterLayer",
        "MotionLayer",
        "MosaicLayer",
        "TextLayer",
        "ShapeLayer",
        "GraphicLayer",
    ], f"layer 순서가 기대와 다름: {layer_types}"


def test_build_render_spec_with_no_effects_returns_only_footage():
    """효과가 하나도 없으면 layers 는 footage 1개뿐이어야 한다."""

    class _EmptyScene(_FakeScene):
        text_overlays: list = []
        shape_overlays: list = []
        graphic_overlays: list = []
        mosaic_overlays: list = []
        filters: list = []
        motions: list = []

    spec = build_render_spec(
        _EmptyScene(), _FakeProject(),
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=5.0,
    )
    assert len(spec.layers) == 1
    assert isinstance(spec.layers[0], (VideoLayer, ImageLayer))


def test_build_render_spec_filter_motion_skipped_for_invalid_type():
    """미지원 filter_type/motion_type 은 builder 헬퍼가 None 으로 스킵 — 핸들러도 None 결과를 건너뛴다."""

    class _BadFilter:
        filter_type = "no_such_filter"
        intensity = 1.0
        params = {}

    class _BadMotion:
        motion_type = "no_such_motion"
        start_sec = 0.0
        duration_sec = 1.0
        params = {}

    class _BadScene(_FakeScene):
        text_overlays: list = []
        shape_overlays: list = []
        graphic_overlays: list = []
        mosaic_overlays: list = []
        filters = [_BadFilter()]
        motions = [_BadMotion()]

    spec = build_render_spec(
        _BadScene(), _FakeProject(),
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=5.0,
    )
    # 미지원 효과는 None → 스킵. footage 1개만 남는다.
    assert len(spec.layers) == 1
    assert isinstance(spec.layers[0], (VideoLayer, ImageLayer))
