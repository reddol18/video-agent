"""씬 모션 효과 매니페스트 (#306, #347).

씬 내 화면 다이나믹 효과 (Shake / Clip Zoom / Stutter / Slow Motion).
백엔드는 `SceneMotion` 을 ffmpeg motion 필터로 변환한다.

UI: EffectAddPanel의 "모션" 섹션 — 4종 버튼.

1차 구현 (#207 / #234 / #347)
- shake / clip_zoom / stutter: 실제 렌더링 지원 (영상 길이 보존)
- slowmo: 데이터만 받고 렌더링은 후속 (영상 길이 변경 + narration_timeline 정합성 필요)

slowmo가 데이터만 받는 상태이지만, EffectAddPanel 측에서는 항상 활성화되어 있었으므로
1단계에서는 이 동작을 그대로 유지한다. (회귀 방지)
"""

from __future__ import annotations

from typing import Iterable

from src.render_spec.effects._base import EffectDefinition, ParamSpec
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)


EFFECT_MOTION_SHAKE = EffectDefinition(
    name="shake",
    display_label="Shake",
    category="motion",
    params={
        "intensity": ParamSpec(
            kind="float",
            label="강도",
            default=0.5,
            min=0.0,
            max=1.0,
            description="셰이크 진폭 (0.0 ~ 1.0)",
        ),
    },
    preview_supported=False,
    tooltip="충격 셰이크",
)

EFFECT_MOTION_CLIP_ZOOM = EffectDefinition(
    name="clip_zoom",
    display_label="Zoom",
    category="motion",
    params={
        "zoom_to": ParamSpec(
            kind="float",
            label="확대율",
            default=1.5,
            min=1.0,
            max=3.0,
            description="최종 확대 배율 (1.0=원본)",
        ),
    },
    preview_supported=False,
    tooltip="영역 줌",
)

EFFECT_MOTION_STUTTER = EffectDefinition(
    name="stutter",
    display_label="Stutter",
    category="motion",
    params={
        "intensity": ParamSpec(
            kind="float",
            label="끊김 강도",
            default=0.3,
            min=0.0,
            max=1.0,
            description=(
                "끊김 강도 (0.0=거의 없음 ~ 1.0=글리치풍). "
                "기본 0.3 은 초당 약 8~12프레임 수준의 은은한 영화적 느낌."
            ),
        ),
    },
    preview_supported=False,
    tooltip="이동 촬영 영상에 끊김/점프 모션 (정지 화면에는 효과 미미)",
    notes=(
        "이동 중 footage 전용. fps 다운샘플로 프레임을 홀드→점프시켜 끊기는 느낌을 만든다. "
        "영상 길이는 보존 (shake/clip_zoom 과 동일)."
    ),
)

EFFECT_MOTION_SLOWMO = EffectDefinition(
    name="slowmo",
    display_label="Slow",
    category="motion",
    params={
        "speed_factor": ParamSpec(
            kind="float",
            label="속도 배율",
            default=0.5,
            min=0.1,
            max=1.0,
            description="재생 속도 (1.0=원속, 0.5=절반속도)",
        ),
    },
    preview_supported=False,
    tooltip="슬로우 모션",
    notes="데이터만 보존 — 실제 렌더링은 후속 사이클",
)


# ── RenderSpec 변환 핸들러 ─────────────────────────────────────────────────
@register_render_spec_handler("motion")
def _render_spec_handler(scene, canvas, duration_sec) -> Iterable[object]:
    """SceneCard.motions → MotionLayer 목록.

    ``_build_motion_layer`` 가 미지원/빈 ``motion_type`` 에 대해 None 을 반환할 수 있으므로
    스킵한다 (#343 참고).
    """
    from src.render_spec.builder import _build_motion_layer

    out: list[object] = []
    for ov in (getattr(scene, "motions", None) or []):
        layer = _build_motion_layer(ov, duration_sec)
        if layer is not None:
            out.append(layer)
    return out


# ── 미리보기 합성 핸들러 ───────────────────────────────────────────────────
@register_preview_handler("motion")
def _preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """씬 모션 미리보기 합성 진입점.

    ``src/clip_editor.py::apply_scene_motions`` 를 그대로 호출한다.
    """
    from src.clip_editor import apply_scene_motions

    try:
        return bool(apply_scene_motions(
            src_path,
            dst_path,
            overlays_data,
            video_width,
            video_height,
        ))
    except Exception:
        return False


__all__ = [
    "EFFECT_MOTION_SHAKE",
    "EFFECT_MOTION_CLIP_ZOOM",
    "EFFECT_MOTION_STUTTER",
    "EFFECT_MOTION_SLOWMO",
]
