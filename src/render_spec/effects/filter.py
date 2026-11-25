"""씬 필터 효과 매니페스트 (#306, #322).

씬 전체 색감/무드 필터(Flashback/Horror/Vivid/NightVision/POV). 백엔드는 `SceneFilter` 를
ffmpeg 필터 체인으로 변환한다(RenderSpec 상에는 footage 레이어의 후처리로 들어간다).

UI: EffectAddPanel의 "필터" 섹션 — 5종 필터 버튼. 같은 타입을 두 번 추가하지 못한다.

#322 — RenderSpec 변환·미리보기 합성 핸들러를 본 파일에서 등록한다.
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import EffectDefinition, ParamSpec
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)


# intensity 는 5개 필터 공통이므로 헬퍼로 만들어 재사용한다.
def _intensity_param() -> dict[str, ParamSpec]:
    return {
        "intensity": ParamSpec(
            kind="float",
            label="강도",
            default=1.0,
            min=0.0,
            max=1.0,
            description="필터 적용 강도 (0.0 ~ 1.0)",
        ),
    }


EFFECT_FILTER_FLASHBACK = EffectDefinition(
    name="flashback",
    display_label="Flashback",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="회상 분위기 — 흐릿하고 따뜻한 톤",
)

EFFECT_FILTER_HORROR = EffectDefinition(
    name="horror",
    display_label="Horror",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="공포 분위기 — 채도 저하/그린 틴트",
)

EFFECT_FILTER_VIVID = EffectDefinition(
    name="vivid",
    display_label="Vivid",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="선명한 색감 강화",
)

EFFECT_FILTER_NIGHTVISION = EffectDefinition(
    name="nightvision",
    display_label="NightVision",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="야간 투시경 분위기 — 그린 모노톤",
)

EFFECT_FILTER_POV = EffectDefinition(
    name="pov",
    display_label="POV",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="1인칭 시점 분위기",
)

EFFECT_FILTER_WARM = EffectDefinition(
    name="warm",
    display_label="Warm",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="따뜻한 분위기 — 노란/주황 톤 + 부드러운 비네팅 (황혼/카페)",
)

EFFECT_FILTER_COOL = EffectDefinition(
    name="cool",
    display_label="Cool",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="시원한 분위기 — 청록/파란 톤 + 살짝 저채도 (얼음/에어컨/여름)",
)

EFFECT_FILTER_CARTOON = EffectDefinition(
    name="cartoon",
    display_label="Cartoon",
    category="filter",
    params=_intensity_param(),
    preview_supported=True,
    tooltip="만화/웹툰 필터 — 색 평탄화 + 채도/대비 강화 + 검은 윤곽선",
)


# ── RenderSpec 변환 핸들러 ─────────────────────────────────────────────────
@register_render_spec_handler("filter")
def _render_spec_handler(scene, canvas, duration_sec) -> Iterable[object]:
    """SceneCard.filters → FilterLayer 목록.

    ``_build_filter_layer`` 가 미지원/알 수 없는 ``filter_type`` 에 대해 None 을 반환할 수
    있으므로 None 결과는 스킵한다 (#343 참고).
    """
    from src.render_spec.builder import _build_filter_layer

    out: list[object] = []
    for ov in (getattr(scene, "filters", None) or []):
        layer = _build_filter_layer(ov, duration_sec)
        if layer is not None:
            out.append(layer)
    return out


# ── 미리보기 합성 핸들러 ───────────────────────────────────────────────────
@register_preview_handler("filter")
def _preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """씬 필터 미리보기 합성 진입점.

    ``src/clip_editor.py::apply_scene_filters`` 를 그대로 호출한다.
    """
    from src.clip_editor import apply_scene_filters

    try:
        return bool(apply_scene_filters(
            src_path,
            dst_path,
            overlays_data,
            video_width,
            video_height,
        ))
    except Exception:
        return False


__all__ = [
    "EFFECT_FILTER_FLASHBACK",
    "EFFECT_FILTER_HORROR",
    "EFFECT_FILTER_VIVID",
    "EFFECT_FILTER_NIGHTVISION",
    "EFFECT_FILTER_POV",
    "EFFECT_FILTER_WARM",
    "EFFECT_FILTER_COOL",
    "EFFECT_FILTER_CARTOON",
]
