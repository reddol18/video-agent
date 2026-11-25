"""그래픽 오버레이 효과 매니페스트 (#306, #322).

씬에 그래픽 마커(스팟라이트/타겟/알림/리액션/스멜/이모지)를 1개 추가.
백엔드는 `GraphicOverlay` → `GraphicLayer` 로 변환된다.

UI: EffectAddPanel의 "그래픽 오버레이" 섹션 — 6종 버튼.

매니페스트의 핵심 효용: 신규 그래픽 타입을 1종 추가하려면
- 이 파일에 EffectDefinition 1개를 더하면 EffectAddPanel 버튼이 자동 등장하고
- 백엔드 GraphicLayer.graphic_type 의 Literal 범위만 늘려주면 된다.

(파라미터 패널은 후속 사이클.)

#322 — RenderSpec 변환·미리보기 합성 핸들러를 본 파일에서 등록한다.
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import EffectDefinition
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)


EFFECT_GRAPHIC_SPOTLIGHT = EffectDefinition(
    name="spotlight",
    display_label="Spotlight",
    category="graphic",
    params={},
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="집중조명 — 중앙 원형만 밝게",
)

EFFECT_GRAPHIC_ALERT = EffectDefinition(
    name="alert",
    display_label="Alert",
    category="graphic",
    params={},
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="알림 팝업",
)

EFFECT_GRAPHIC_TARGET = EffectDefinition(
    name="target",
    display_label="Target",
    category="graphic",
    params={},
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="타겟 마커 (십자선/화살표)",
)

EFFECT_GRAPHIC_REACTION = EffectDefinition(
    name="reaction",
    display_label="Reaction",
    category="graphic",
    params={},
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="리액션 아이콘 (번개/땀/물음표/느낌표)",
)

EFFECT_GRAPHIC_SMELL = EffectDefinition(
    name="smell",
    display_label="Smell",
    category="graphic",
    params={},
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="냄새/연기 효과",
)

EFFECT_GRAPHIC_EMOJI = EffectDefinition(
    name="emoji",
    display_label="Emoji",
    category="graphic",
    params={},
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="이모지 아이콘 (❓ 🔍 🚫 💩)",
    notes="Apple Color Emoji 기반 (#288)",
)

EFFECT_GRAPHIC_DASH = EffectDefinition(
    name="dash",
    display_label="Dash",
    category="graphic",
    params={},
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="질주 표시 (=3=3=3) — 마크가 흘러가는 애니메이션",
    notes="만화식 이동/질주 연출. 항상 애니메이션(동적 layer)으로 렌더된다.",
)


# ── RenderSpec 변환 핸들러 ─────────────────────────────────────────────────
@register_render_spec_handler("graphic")
def _render_spec_handler(scene, canvas, duration_sec) -> Iterable[object]:
    """SceneCard.graphic_overlays → GraphicLayer 목록."""
    from src.render_spec.builder import _build_graphic_layer

    out: list[object] = []
    for ov in (getattr(scene, "graphic_overlays", None) or []):
        out.append(_build_graphic_layer(ov, canvas, duration_sec))
    return out


# ── 미리보기 합성 핸들러 ───────────────────────────────────────────────────
@register_preview_handler("graphic")
def _preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """그래픽 오버레이 미리보기 합성 진입점.

    ``src/clip_editor.py::apply_graphic_overlays`` 를 그대로 호출한다.
    """
    from src.clip_editor import apply_graphic_overlays

    try:
        return bool(apply_graphic_overlays(
            src_path,
            dst_path,
            overlays_data,
            video_width,
            video_height,
        ))
    except Exception:
        return False


__all__ = [
    "EFFECT_GRAPHIC_SPOTLIGHT",
    "EFFECT_GRAPHIC_ALERT",
    "EFFECT_GRAPHIC_TARGET",
    "EFFECT_GRAPHIC_REACTION",
    "EFFECT_GRAPHIC_SMELL",
    "EFFECT_GRAPHIC_EMOJI",
    "EFFECT_GRAPHIC_DASH",
]
