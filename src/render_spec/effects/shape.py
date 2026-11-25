"""도형 오버레이 효과 매니페스트 (#306, #322).

씬에 사각형/타원 도형 1개를 추가. 백엔드는 `ShapeOverlay` → `ShapeLayer` 로 변환된다.

UI: EffectAddPanel의 "도형" 섹션 — `+ 사각형`, `+ 타원` 2개 버튼.

#322 — RenderSpec 변환·미리보기 합성 핸들러를 본 파일에서 등록한다.
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import EffectDefinition
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)


EFFECT_SHAPE_RECT = EffectDefinition(
    name="rect",
    display_label="사각형",
    category="shape",
    params={},
    preview_supported=True,
    backend_layer_type="shape",
    tooltip="사각형 도형 오버레이",
)

EFFECT_SHAPE_ELLIPSE = EffectDefinition(
    name="ellipse",
    display_label="타원",
    category="shape",
    params={},
    preview_supported=True,
    backend_layer_type="shape",
    tooltip="타원 도형 오버레이",
)


# ── RenderSpec 변환 핸들러 ─────────────────────────────────────────────────
@register_render_spec_handler("shape")
def _render_spec_handler(scene, canvas, duration_sec) -> Iterable[object]:
    """SceneCard.shape_overlays → ShapeLayer 목록."""
    from src.render_spec.builder import _build_shape_layer

    out: list[object] = []
    for ov in (getattr(scene, "shape_overlays", None) or []):
        out.append(_build_shape_layer(ov, canvas, duration_sec))
    return out


# ── 미리보기 합성 핸들러 ───────────────────────────────────────────────────
@register_preview_handler("shape")
def _preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """도형 오버레이 미리보기 합성 진입점.

    ``web/backend/routers/project.py::_apply_shape_overlays_preview`` 를 그대로 호출한다.
    """
    from web.backend.routers.project import _apply_shape_overlays_preview

    try:
        _apply_shape_overlays_preview(
            src_path,
            dst_path,
            overlays_data,
            video_width,
            video_height,
        )
        return True
    except Exception:
        return False


__all__ = [
    "EFFECT_SHAPE_RECT",
    "EFFECT_SHAPE_ELLIPSE",
]
