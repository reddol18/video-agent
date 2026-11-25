"""이펙트 카탈로그 매니페스트 (#306).

`EFFECT_REGISTRY` 가 6종 효과(text/shape/graphic/filter/motion/mosaic)의 메타데이터를
한 곳에서 노출한다. 백엔드와 프론트가 이 레지스트리를 *동일하게* 읽음으로써, 새 효과를
추가할 때 한쪽만 구현하고 다른 쪽을 깜빡하는 사고를 구조적으로 방지한다.

레지스트리 키
------------
`(category, name)` 튜플 형식의 *문자열 키* `"category:name"`. 예: `"shape:rect"`,
`"graphic:spotlight"`, `"filter:flashback"`. 카테고리가 다르면 같은 name도 충돌하지 않는다
(예: `"shape:rect"` 와 `"mosaic:rect"`).

조회 헬퍼
--------
- `get_effect(category, name) -> EffectDefinition | None`
- `iter_effects_by_category(category) -> Iterable[EffectDefinition]`

TS 자동 생성
-----------
이 패키지의 Pydantic 모델은 `scripts/_pydantic_models_collector.py` 가 import 하여
`web/frontend/src/types/generated.ts` 에 `EffectDefinition`, `ParamSpec` 으로 노출된다.
런타임 카탈로그 자체는 백엔드 API(`/api/effect_catalog`) 로 프론트에 전달한다.

제공 기능
--------
- 데이터 매니페스트 + 조회 헬퍼 (``EFFECT_REGISTRY`` / ``get_effect`` /
  ``iter_effects_by_category``)
- 백엔드 합성 핸들러 레지스트리 (``PY_EFFECT_HANDLERS_RENDER_SPEC`` /
  ``PY_EFFECT_HANDLERS_PREVIEW``) — builder.py·project.py 가 카테고리별 if 분기 없이
  단일 루프로 효과를 적용할 수 있게 한다.
- 신규 효과 추가는 매니페스트 파일 1개 수정으로 끝나는 워크플로 보장 (``docs/adding-effect.md``).
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import (
    EffectCategory,
    EffectDefinition,
    ParamSpec,
)
from src.render_spec.effects._handlers import (
    PY_EFFECT_HANDLERS_PREVIEW,
    PY_EFFECT_HANDLERS_RENDER_SPEC,
    get_preview_handler,
    get_render_spec_handler,
)
from src.render_spec.effects.filter import (
    EFFECT_FILTER_CARTOON,
    EFFECT_FILTER_COOL,
    EFFECT_FILTER_FLASHBACK,
    EFFECT_FILTER_HORROR,
    EFFECT_FILTER_NIGHTVISION,
    EFFECT_FILTER_POV,
    EFFECT_FILTER_VIVID,
    EFFECT_FILTER_WARM,
)
from src.render_spec.effects.graphic import (
    EFFECT_GRAPHIC_ALERT,
    EFFECT_GRAPHIC_EMOJI,
    EFFECT_GRAPHIC_DASH,
    EFFECT_GRAPHIC_REACTION,
    EFFECT_GRAPHIC_SMELL,
    EFFECT_GRAPHIC_SPOTLIGHT,
    EFFECT_GRAPHIC_TARGET,
)
from src.render_spec.effects.mosaic import (
    EFFECT_MOSAIC_ELLIPSE,
    EFFECT_MOSAIC_RECT,
)
from src.render_spec.effects.motion import (
    EFFECT_MOTION_CLIP_ZOOM,
    EFFECT_MOTION_SHAKE,
    EFFECT_MOTION_SLOWMO,
    EFFECT_MOTION_STUTTER,
)
from src.render_spec.effects.shape import (
    EFFECT_SHAPE_ELLIPSE,
    EFFECT_SHAPE_RECT,
)
from src.render_spec.effects.image import EFFECT_IMAGE_OVERLAY
from src.render_spec.effects.text import EFFECT_TEXT_OVERLAY
# 집중선(속도선) 오버레이 — import 시 미리보기 핸들러("speed_lines")를 등록한다.
# EFFECT_REGISTRY 에는 넣지 않는다(별도 오버레이 타입).
from src.render_spec.effects import speed_lines as _speed_lines_manifest  # noqa: F401


# ── 레지스트리 본체 ───────────────────────────────────────────────────────

def _registry_key(eff: EffectDefinition) -> str:
    """`category:name` 형식의 고유 키. EffectAddPanel/백엔드 모두 같은 키를 본다."""
    return f"{eff.category}:{eff.name}"


_ALL_EFFECTS: list[EffectDefinition] = [
    # text
    EFFECT_TEXT_OVERLAY,
    # shape
    EFFECT_SHAPE_RECT,
    EFFECT_SHAPE_ELLIPSE,
    # graphic
    EFFECT_GRAPHIC_SPOTLIGHT,
    EFFECT_GRAPHIC_ALERT,
    EFFECT_GRAPHIC_TARGET,
    EFFECT_GRAPHIC_REACTION,
    EFFECT_GRAPHIC_SMELL,
    EFFECT_GRAPHIC_EMOJI,
    EFFECT_GRAPHIC_DASH,
    # filter
    EFFECT_FILTER_FLASHBACK,
    EFFECT_FILTER_HORROR,
    EFFECT_FILTER_VIVID,
    EFFECT_FILTER_NIGHTVISION,
    EFFECT_FILTER_POV,
    EFFECT_FILTER_WARM,
    EFFECT_FILTER_COOL,
    EFFECT_FILTER_CARTOON,
    # motion
    EFFECT_MOTION_SHAKE,
    EFFECT_MOTION_CLIP_ZOOM,
    EFFECT_MOTION_STUTTER,
    EFFECT_MOTION_SLOWMO,
    # mosaic
    EFFECT_MOSAIC_RECT,
    EFFECT_MOSAIC_ELLIPSE,
    # image
    EFFECT_IMAGE_OVERLAY,
]


def _build_registry() -> dict[str, EffectDefinition]:
    """매니페스트 리스트 → `category:name` 키 dict. 중복 키는 즉시 에러."""
    out: dict[str, EffectDefinition] = {}
    for eff in _ALL_EFFECTS:
        key = _registry_key(eff)
        if key in out:
            raise RuntimeError(
                f"EFFECT_REGISTRY: 중복 키 '{key}' — 매니페스트 파일을 확인하세요."
            )
        out[key] = eff
    return out


EFFECT_REGISTRY: dict[str, EffectDefinition] = _build_registry()


# ── 조회 헬퍼 ─────────────────────────────────────────────────────────────

def get_effect(category: EffectCategory, name: str) -> EffectDefinition | None:
    """카테고리/이름으로 효과 1개 조회. 없으면 None."""
    return EFFECT_REGISTRY.get(f"{category}:{name}")


def iter_effects_by_category(
    category: EffectCategory,
) -> Iterable[EffectDefinition]:
    """카테고리 단위로 매니페스트 순회. UI 섹션 구성에 사용."""
    for eff in EFFECT_REGISTRY.values():
        if eff.category == category:
            yield eff


__all__ = [
    "EFFECT_REGISTRY",
    "EffectCategory",
    "EffectDefinition",
    "ParamSpec",
    "PY_EFFECT_HANDLERS_PREVIEW",
    "PY_EFFECT_HANDLERS_RENDER_SPEC",
    "get_effect",
    "get_preview_handler",
    "get_render_spec_handler",
    "iter_effects_by_category",
    # 매니페스트 개별 export (테스트/외부 참조 용)
    "EFFECT_TEXT_OVERLAY",
    "EFFECT_SHAPE_RECT",
    "EFFECT_SHAPE_ELLIPSE",
    "EFFECT_GRAPHIC_SPOTLIGHT",
    "EFFECT_GRAPHIC_ALERT",
    "EFFECT_GRAPHIC_TARGET",
    "EFFECT_GRAPHIC_REACTION",
    "EFFECT_GRAPHIC_SMELL",
    "EFFECT_GRAPHIC_EMOJI",
    "EFFECT_GRAPHIC_DASH",
    "EFFECT_FILTER_FLASHBACK",
    "EFFECT_FILTER_HORROR",
    "EFFECT_FILTER_VIVID",
    "EFFECT_FILTER_NIGHTVISION",
    "EFFECT_FILTER_POV",
    "EFFECT_FILTER_WARM",
    "EFFECT_FILTER_COOL",
    "EFFECT_FILTER_CARTOON",
    "EFFECT_MOTION_SHAKE",
    "EFFECT_MOTION_CLIP_ZOOM",
    "EFFECT_MOTION_STUTTER",
    "EFFECT_MOTION_SLOWMO",
    "EFFECT_MOSAIC_RECT",
    "EFFECT_MOSAIC_ELLIPSE",
    "EFFECT_IMAGE_OVERLAY",
]
