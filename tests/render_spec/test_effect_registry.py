"""이펙트 카탈로그 매니페스트 회귀 테스트 (#306).

검증 원칙
1. **레지스트리 키 무결성**: `category:name` 키가 중복되지 않고, 6개 카테고리가 모두 존재.
2. **기존 UI 정책 보존**: EffectAddPanel 이 기대하던 효과 목록(이름, 카테고리)이 그대로 노출.
3. **모델 직렬화 가능성**: 모든 EffectDefinition이 `.model_dump()` / `.model_dump_json()` 으로
   안전하게 직렬화된다 — 백엔드 라우터(`/api/effect_catalog`)와 TS 자동 생성 둘 다 이 직렬화에
   의존한다.
4. **ParamSpec 일관성**: kind="int"/"float"이면 min/max가 양쪽 다 있거나 양쪽 다 없다.
   enum이면 options가 비어있지 않다.
"""

from __future__ import annotations

import json

import pytest

from src.render_spec import EFFECT_REGISTRY, EffectDefinition, ParamSpec, get_effect


# ── 1) 레지스트리 키 무결성 ────────────────────────────────────────────────

def test_registry_keys_use_category_colon_name():
    """모든 키가 `category:name` 형식이며, 키와 효과 메타가 일치한다."""
    for key, eff in EFFECT_REGISTRY.items():
        assert key == f"{eff.category}:{eff.name}", (
            f"키 {key}와 효과({eff.category}:{eff.name})가 불일치"
        )


def test_registry_no_duplicate_keys():
    """레지스트리는 dict이므로 키 중복 자체는 발생하지 않지만, 매니페스트 리스트와의 길이를
    비교해 *카테고리:name 조합*이 매니페스트 추가 시 중복되지 않았는지 간접 검증한다.
    """
    # 모든 매니페스트가 등록되어 있어야 한다 — 우리가 의도한 6종 × 카운트.
    expected_min = 1 + 2 + 7 + 5 + 3 + 2  # text + shape + graphic + filter + motion + mosaic
    assert len(EFFECT_REGISTRY) >= expected_min


def test_registry_has_all_categories():
    """기본 6개 카테고리 + image 카테고리가 모두 1개 이상 등록되어 있다.

    (image 는 후속 사이클에서 추가됨. 신규 카테고리 추가 시 본 집합을 갱신한다.)
    """
    cats = {eff.category for eff in EFFECT_REGISTRY.values()}
    assert cats == {"text", "shape", "graphic", "filter", "motion", "mosaic", "image"}


# ── 2) 기존 UI 정책 보존 ───────────────────────────────────────────────────

def test_existing_filter_types_preserved():
    """EffectAddPanel.tsx 하드코딩되어 있던 원 5종 필터가 그대로 등록되어 있고,
    후속 추가 필터(warm/cool/cartoon)도 함께 등록된다.

    원 5종 보존은 부분집합으로 검증(신규 필터 추가가 기존 필터를 지우지 않음).
    """
    original5 = {"flashback", "horror", "vivid", "nightvision", "pov"}
    actual = {eff.name for eff in EFFECT_REGISTRY.values() if eff.category == "filter"}
    assert original5 <= actual
    # 후속 추가 필터도 등록되어 있어야 한다.
    assert {"warm", "cool", "cartoon"} <= actual


def test_existing_graphic_types_preserved():
    """GRAPHIC_TYPES 7종(spotlight/alert/target/reaction/smell/emoji/dash)이 그대로."""
    expected = {"spotlight", "alert", "target", "reaction", "smell", "emoji", "dash"}
    actual = {eff.name for eff in EFFECT_REGISTRY.values() if eff.category == "graphic"}
    assert actual == expected


def test_existing_motion_types_preserved():
    """MOTION_TYPES: 기존 3종(shake/clip_zoom/slowmo) + stutter(#347)."""
    expected = {"shake", "clip_zoom", "slowmo", "stutter"}
    actual = {eff.name for eff in EFFECT_REGISTRY.values() if eff.category == "motion"}
    assert actual == expected


def test_existing_shape_and_mosaic_types_preserved():
    """기존 shape/mosaic가 각각 rect/ellipse 2종."""
    shape = {eff.name for eff in EFFECT_REGISTRY.values() if eff.category == "shape"}
    mosaic = {eff.name for eff in EFFECT_REGISTRY.values() if eff.category == "mosaic"}
    assert shape == {"rect", "ellipse"}
    assert mosaic == {"rect", "ellipse"}


# ── 3) 모델 직렬화 가능성 ──────────────────────────────────────────────────

def test_registry_is_json_serializable():
    """모든 EffectDefinition을 dict → json 으로 변환할 수 있다.

    백엔드 라우터(`/api/effect_catalog`)는 FastAPI 가 Pydantic 모델 리스트를 자동
    직렬화하지만, 본 테스트는 직접 검증해 회귀를 막는다.
    """
    payload = [eff.model_dump() for eff in EFFECT_REGISTRY.values()]
    s = json.dumps(payload)
    parsed = json.loads(s)
    assert len(parsed) == len(EFFECT_REGISTRY)
    # category 필드가 등록된 카테고리 중 하나여야 한다.
    for p in parsed:
        assert p["category"] in {
            "text", "shape", "graphic", "filter", "motion", "mosaic", "image",
        }


# ── 4) ParamSpec 일관성 ────────────────────────────────────────────────────

def test_numeric_paramspec_has_min_max_pair():
    """kind=int/float인 ParamSpec은 min과 max를 양쪽 다 가져야 일관된 UI를 그릴 수 있다."""
    for eff in EFFECT_REGISTRY.values():
        for pname, ps in eff.params.items():
            if ps.kind in ("int", "float"):
                assert ps.min is not None and ps.max is not None, (
                    f"{eff.category}:{eff.name}.{pname} — 수치형은 min/max를 모두 명시해야 합니다."
                )
                assert ps.min <= ps.max


def test_enum_paramspec_has_options():
    """kind=enum이면 options가 비어있지 않아야 한다."""
    for eff in EFFECT_REGISTRY.values():
        for pname, ps in eff.params.items():
            if ps.kind == "enum":
                assert ps.options, (
                    f"{eff.category}:{eff.name}.{pname} — enum은 options를 명시해야 합니다."
                )
                # default도 options 안에 있어야 한다.
                assert ps.default in ps.options


# ── 5) get_effect 헬퍼 ────────────────────────────────────────────────────

def test_get_effect_returns_definition():
    e = get_effect("filter", "flashback")
    assert isinstance(e, EffectDefinition)
    assert e.display_label == "Flashback"


def test_get_effect_returns_none_for_unknown():
    assert get_effect("filter", "no_such_filter") is None
    assert get_effect("text", "no_such") is None


def test_paramspec_model_dump_round_trip():
    """ParamSpec 한 개를 dict → 재생성 round trip — TS측에서 JSON으로 받았을 때 다시 쓸 수 있는지."""
    ps = ParamSpec(
        kind="float",
        label="강도",
        default=0.5,
        min=0.0,
        max=1.0,
        description="x",
    )
    restored = ParamSpec(**ps.model_dump())
    assert restored == ps
