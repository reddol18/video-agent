"""이미지 오버레이 효과 매니페스트.

footage 스튜디오에 등록된 이미지 자산을 씬 위에 자유 위치·크기로 합성하는 오버레이.
주 용도는 "이전 영상 썸네일 + 제목" 카드처럼 이미지+캡션을 한 묶음으로 표시하는 것.

UI: EffectAddPanel의 "이미지" 섹션 — `+ 이미지` 버튼 1개.
백엔드 합성: `web.backend.routers.project._apply_image_overlays_preview` (미리보기)
             + `src.clip_editor.apply_image_overlays` (최종 렌더).

RenderSpec 측 매핑(`backend_layer_type`)은 1차 구현에서는 None으로 두고, 후속 사이클에서
ImageLayer 매핑을 도입한다 — RenderSpec 우회 경로(builder.py)는 이미 graphic/filter/motion
등이 일부만 지원되는 패턴이므로 회귀 위험이 없다.
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import EffectDefinition
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)


EFFECT_IMAGE_OVERLAY = EffectDefinition(
    name="image",
    display_label="이미지",
    category="image",
    params={},
    preview_supported=True,
    backend_layer_type=None,
    tooltip="footage 스튜디오의 이미지 자산을 씬 위에 합성 + 선택적 캡션",
)


# ── RenderSpec 변환 핸들러 ─────────────────────────────────────────────────
@register_render_spec_handler("image")
def _render_spec_handler(scene, canvas, duration_sec) -> Iterable[object]:
    """SceneCard.image_overlays → Layer 목록.

    1차 구현에서는 RenderSpec 변환을 비워둔다 (builder.py가 카테고리별로 layer를 수집할 때
    image 카테고리는 0개를 반환). 실제 렌더링은 `src.clip_editor.apply_image_overlays` 와
    `web.backend.routers.project._apply_image_overlays_preview` 의 직접 경로가 담당한다.
    """
    return []


# ── 미리보기 합성 핸들러 ───────────────────────────────────────────────────
@register_preview_handler("image")
def _preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """이미지 오버레이 미리보기 합성 진입점.

    `web/backend/routers/project.py::_apply_image_overlays_preview` 를 그대로 호출한다.
    """
    from web.backend.routers.project import _apply_image_overlays_preview

    try:
        _apply_image_overlays_preview(
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
    "EFFECT_IMAGE_OVERLAY",
]
