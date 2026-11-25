"""텍스트 오버레이 효과 매니페스트 (#306, #322).

씬에 추가하는 텍스트(자막/문구). 백엔드는 `TextOverlay` → `TextLayer` 로 변환되며,
ffmpeg 측에서는 PIL로 PNG를 그려 overlay 한다.

UI 흐름: EffectAddPanel의 "+ 텍스트" 버튼 1개 → Editor.tsx 측 칩 편집기에서 상세 편집.
파라미터는 칩 편집기에서 다루므로 ParamSpec은 비워둔다.

#322 (2단계) 백엔드 매퍼 일원화
-------------------------------
- ``_render_spec_handler`` 가 SceneCard.text_overlays → TextLayer 변환을 담당.
- ``_preview_handler`` 가 미리보기 합성 진입점.
두 핸들러는 ``_handlers.PY_EFFECT_HANDLERS_*`` 레지스트리에 자동 등록되어, builder.py /
project.py 측에서 카테고리별 if 분기 없이 단일 루프로 호출된다.

기존 변환·합성 로직은 *그대로 유지*하고 호출만 한 곳으로 모은다 (회귀 안전 보장).
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import EffectDefinition
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)


EFFECT_TEXT_OVERLAY = EffectDefinition(
    name="text",
    display_label="텍스트",
    category="text",
    params={},  # 칩 편집기에서 상세 편집
    preview_supported=True,
    backend_layer_type="text",
    tooltip="화면에 자막/문구 추가 — 추가 후 칩 클릭으로 편집",
    notes=(
        "추가 후 씬 카드의 칩을 클릭하면 텍스트·스타일·프리셋 편집기가 열립니다."
    ),
)


# ── RenderSpec 변환 핸들러 (builder.py 가 호출) ────────────────────────────
@register_render_spec_handler("text")
def _render_spec_handler(scene, canvas, duration_sec) -> Iterable[object]:
    """SceneCard.text_overlays → TextLayer 목록.

    실제 변환은 ``src/render_spec/builder.py::_build_text_layer`` 가 담당한다
    (헬퍼는 builder 가 보유 — fit/회전 같은 footage 매핑과 한 모듈에 묶이는 것이 의미상
    더 자연스럽기 때문). 본 핸들러는 그 헬퍼를 카테고리 일괄 변환 진입점으로 래핑한다.
    """
    # import 를 핸들러 내부에서 하는 이유: builder.py 가 본 모듈을 import 하므로
    # 모듈 로딩 시점에 builder 를 import 하면 순환 import 가 발생한다. 핸들러가 *호출* 될
    # 시점에는 builder 모듈이 완전히 로딩된 상태가 보장되므로 안전하다.
    from src.render_spec.builder import _build_text_layer

    out: list[object] = []
    for ov in (getattr(scene, "text_overlays", None) or []):
        out.append(_build_text_layer(ov, canvas, duration_sec))
    return out


# ── 미리보기 합성 핸들러 (web/backend/routers/project.py 가 호출) ─────────
@register_preview_handler("text")
def _preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """텍스트 오버레이 미리보기 합성 진입점.

    기존 ``web/backend/routers/project.py::_apply_text_overlays_preview`` 함수를 그대로
    호출한다. 함수 자체는 None 반환이지만, 본 핸들러는 *호출 성공/실패*를 bool 로 반환해
    다른 카테고리 핸들러와 시그니처를 일치시킨다.

    ``output_height`` 는 텍스트의 픽셀 크기를 미리보기 다운스케일 비율에 맞추기 위해
    필요하므로 ``kwargs`` 로 받는다.
    """
    from web.backend.routers.project import _apply_text_overlays_preview

    try:
        _apply_text_overlays_preview(
            src_path,
            dst_path,
            overlays_data,
            video_width,
            video_height,
            output_height=kwargs.get("output_height"),
        )
        return True
    except Exception:
        return False


__all__ = ["EFFECT_TEXT_OVERLAY"]
