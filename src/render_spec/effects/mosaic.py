"""모자이크 오버레이 효과 매니페스트 (#306).

씬에 영역 모자이크/블러 1개를 추가. 백엔드는 `MosaicOverlay` → `MosaicLayer` 로 변환된다.

UI: EffectAddPanel의 "모자이크" 섹션 — `+ 사각형`, `+ 타원` 2개 버튼.
1차 구현(#264)에서는 타원도 데이터로 받지만 백엔드 렌더에서 사각형으로 폴백된다.
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import EffectDefinition, ParamSpec
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)


def _common_mode_param() -> dict[str, ParamSpec]:
    return {
        "mode": ParamSpec(
            kind="enum",
            label="모드",
            default="pixelate",
            options=["pixelate", "blur"],
            description="픽셀화(pixelate) / 블러(blur)",
        ),
        "pixel_size": ParamSpec(
            kind="int",
            label="픽셀 크기",
            default=20,
            min=1,
            max=200,
            description="픽셀화 모드의 셀 크기",
        ),
        "blur_radius": ParamSpec(
            kind="int",
            label="블러 반경",
            default=20,
            min=0,
            max=200,
            description="블러 모드의 반경",
        ),
    }


EFFECT_MOSAIC_RECT = EffectDefinition(
    name="rect",
    display_label="사각형",
    category="mosaic",
    params=_common_mode_param(),
    preview_supported=True,
    backend_layer_type="mosaic",
    tooltip="사각형 모자이크 영역",
)

EFFECT_MOSAIC_ELLIPSE = EffectDefinition(
    name="ellipse",
    display_label="타원",
    category="mosaic",
    params=_common_mode_param(),
    preview_supported=True,
    backend_layer_type="mosaic",
    tooltip="타원 모자이크 — 1차 구현에서는 사각형으로 폴백됩니다 (#264 후속)",
)


# ── RenderSpec 변환 핸들러 ─────────────────────────────────────────────────
@register_render_spec_handler("mosaic")
def _render_spec_handler(scene, canvas, duration_sec) -> Iterable[object]:
    """SceneCard.mosaic_overlays → MosaicLayer 목록."""
    from src.render_spec.builder import _build_mosaic_layer

    out: list[object] = []
    for ov in (getattr(scene, "mosaic_overlays", None) or []):
        out.append(_build_mosaic_layer(ov, canvas, duration_sec))
    return out


# ── 미리보기 합성 핸들러 ───────────────────────────────────────────────────
@register_preview_handler("mosaic")
def _preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """모자이크 오버레이 미리보기 합성 진입점.

    ``src/clip_editor.py::apply_mosaic_overlays`` 를 그대로 호출한다.
    """
    from src.clip_editor import apply_mosaic_overlays

    try:
        return bool(apply_mosaic_overlays(
            src_path,
            dst_path,
            overlays_data,
            video_width,
            video_height,
        ))
    except Exception:
        return False


__all__ = [
    "EFFECT_MOSAIC_RECT",
    "EFFECT_MOSAIC_ELLIPSE",
]
