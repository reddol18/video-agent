"""이펙트 카탈로그 백엔드 핸들러 레지스트리 (#322).

배경
----
#306 1단계에서 `EFFECT_REGISTRY` (메타데이터 매니페스트)가 도입되어 프론트
EffectAddPanel 의 버튼 목록이 동적 갱신되도록 정리되었으나, 실제 백엔드
ffmpeg/PIL 합성 분기는 여전히 카테고리별로 흩어져 있었다:

- `web/backend/routers/project.py` 의 카테고리별 `_apply_*_overlays_preview` /
  `apply_*` 호출 (text/shape/graphic/filter/motion/mosaic)
- `src/render_spec/builder.py` 의 카테고리별 ``_build_*_layer`` 분기
- `src/clip_editor.py` 의 카테고리별 `apply_*` 함수

신규 효과 1종을 추가할 때, 매니페스트(`text.py`/`shape.py`/...) 1개 파일만 손대도
백엔드 합성/RenderSpec 변환이 자동 연결되는 *단일 진입점*을 마련하는 것이 본 모듈의
목적이다.

설계 원칙
---------
1. **Pydantic 모델은 데이터만** — `EffectDefinition` 자체에는 콜백 필드를 넣지 않는다.
   직렬화/TS 자동 생성을 깨지 않기 위해, 콜백은 본 모듈의 **별도 Python dict** 로 관리한다.
2. **카테고리 단위 핸들러** — 효과 1종 단위가 아니라 카테고리(=6종) 단위로 핸들러를
   등록한다. 이는 노팀장이 의도적으로 선택한 *점진적 일원화* 방향이다:
     - 기존 `apply_*` 진입점이 이미 카테고리 단위로 분리되어 있어, 카테고리 단위 핸들러는
       이를 그대로 wrap 할 수 있고 회귀 위험이 최소화된다.
     - 카테고리 내부 효과별 분기(예: filter의 flashback/horror 등)는 현행 `_build_filter_vf`
       체계를 그대로 유지하므로 골든 테스트가 픽셀 단위로 동일하게 유지된다.
     - 효과 1종 단위 핸들러(= 효과별 ffmpeg 표현식 분해)는 별도 후속 이슈로 분리한다.
3. **두 종류의 핸들러 dict**:
     - ``PY_EFFECT_HANDLERS_RENDER_SPEC`` — SceneCard 의 효과 리스트(예: ``scene.text_overlays``)
       → ``RenderSpec.Layer`` 리스트로 변환하는 핸들러. ``builder.py`` 가 사용.
     - ``PY_EFFECT_HANDLERS_PREVIEW`` — 미리보기(`project.py`) 가 카테고리별로 호출하는
       Python 합성 진입점. 입출력 파일 경로 + 효과 리스트(dict) + 캔버스 사이즈를 받아 부울 반환.
4. **회귀 안전** — 본 모듈은 *기존 함수들을 호출만 한다*. 새 ffmpeg 표현식이나 새 PIL
   로직은 추가하지 않는다. 따라서 이번 일원화로 인한 픽셀/문자열 차이가 0임이 보장된다.

레지스트리 키
-------------
카테고리 문자열 (``"text"``/``"shape"``/``"graphic"``/``"filter"``/``"motion"``/``"mosaic"``)을
키로 사용한다. 향후 효과 1종 단위 핸들러로 세분화하면 ``"category:name"`` 형식으로 확장할 수
있도록 키 형식을 한정해 두지 않는다(헬퍼 함수가 key 빌더를 캡슐화).

호출 예
-------
::

    from src.render_spec.effects._handlers import (
        PY_EFFECT_HANDLERS_RENDER_SPEC,
        PY_EFFECT_HANDLERS_PREVIEW,
    )

    # builder.py — RenderSpec 변환
    for category, handler in PY_EFFECT_HANDLERS_RENDER_SPEC.items():
        for layer in handler(scene, canvas, duration_sec):
            layers.append(layer)

    # project.py — 미리보기 합성
    handler = PY_EFFECT_HANDLERS_PREVIEW.get("filter")
    if handler:
        handler(src_path, dst_path, filters_data, out_w, out_h)
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from src.render_spec.effects._base import EffectCategory


# ── 1) RenderSpec 변환 핸들러 ─────────────────────────────────────────────────
#
# 시그니처: (scene: SceneCard-like, canvas: Canvas, duration_sec: float) -> Iterable[Layer]
#
# `scene` 은 SceneCard (또는 동일 구조의 dict-like 객체). 핸들러는 카테고리에 해당하는
# 효과 리스트(`scene.text_overlays`, `scene.shape_overlays`, ...)를 *내부에서* 꺼내
# 0개 이상의 Layer 를 yield/return 한다.
#
# 시그니처를 통일해 builder.py 가 카테고리별 if 분기 없이 단일 루프로 처리할 수 있도록 한다.

RenderSpecHandler = Callable[..., Iterable[object]]
"""SceneCard → RenderSpec Layer 변환 핸들러 시그니처.

(scene, canvas, duration_sec) -> Iterable[Layer]
"""


PY_EFFECT_HANDLERS_RENDER_SPEC: dict[str, RenderSpecHandler] = {}
"""카테고리 → SceneCard→Layer 변환 핸들러.

매니페스트 파일(`text.py`/`shape.py`/...) 측에서 import 시 등록한다.
"""


def register_render_spec_handler(
    category: EffectCategory,
) -> Callable[[RenderSpecHandler], RenderSpecHandler]:
    """카테고리 단위 RenderSpec 변환 핸들러 등록 데코레이터.

    이미 같은 카테고리가 등록되어 있으면 즉시 에러 — 매니페스트 파일 간 충돌을 import
    단계에서 잡아낸다. (테스트가 import 시점에 RuntimeError 를 잡도록 한다.)
    """

    def decorator(fn: RenderSpecHandler) -> RenderSpecHandler:
        if category in PY_EFFECT_HANDLERS_RENDER_SPEC:
            raise RuntimeError(
                f"PY_EFFECT_HANDLERS_RENDER_SPEC: 카테고리 '{category}' 중복 등록"
            )
        PY_EFFECT_HANDLERS_RENDER_SPEC[category] = fn
        return fn

    return decorator


def get_render_spec_handler(
    category: EffectCategory,
) -> Optional[RenderSpecHandler]:
    """카테고리 → 등록된 RenderSpec 핸들러 조회. 미등록이면 None.

    builder.py 가 사용. 미등록 카테고리는 *조용히 skip*(빈 layers 반환과 동일)되어
    회귀가 발생하지 않도록 한다.
    """
    return PY_EFFECT_HANDLERS_RENDER_SPEC.get(category)


# ── 2) 미리보기 합성 핸들러 ───────────────────────────────────────────────────
#
# 시그니처: (src_path, dst_path, effect_list_dicts, video_width, video_height, **kwargs) -> bool
#
# 본 핸들러는 `web/backend/routers/project.py` 의 카테고리별 if 블록을 단일 루프로 줄이는
# 용도다. 호출자는 `PY_EFFECT_HANDLERS_PREVIEW[category]` 로 진입점을 가져와, 효과
# 리스트가 비어있지 않으면 호출한다. 진입점은 *기존 `apply_*` / `_apply_*_overlays_preview`
# 함수를 그대로 호출*한다(회귀 안전).

PreviewHandler = Callable[..., bool]
"""미리보기 합성 진입점 시그니처.

(src_path, dst_path, overlays_data, video_width, video_height, **kwargs) -> bool
성공/스킵 시 True, 실패 시 False.
"""


PY_EFFECT_HANDLERS_PREVIEW: dict[str, PreviewHandler] = {}
"""카테고리 → 미리보기 합성 진입점 함수."""


def register_preview_handler(
    category: EffectCategory,
) -> Callable[[PreviewHandler], PreviewHandler]:
    """카테고리 단위 미리보기 핸들러 등록 데코레이터."""

    def decorator(fn: PreviewHandler) -> PreviewHandler:
        if category in PY_EFFECT_HANDLERS_PREVIEW:
            raise RuntimeError(
                f"PY_EFFECT_HANDLERS_PREVIEW: 카테고리 '{category}' 중복 등록"
            )
        PY_EFFECT_HANDLERS_PREVIEW[category] = fn
        return fn

    return decorator


def get_preview_handler(category: EffectCategory) -> Optional[PreviewHandler]:
    """카테고리 → 등록된 미리보기 핸들러 조회. 미등록이면 None."""
    return PY_EFFECT_HANDLERS_PREVIEW.get(category)


__all__ = [
    "PY_EFFECT_HANDLERS_PREVIEW",
    "PY_EFFECT_HANDLERS_RENDER_SPEC",
    "PreviewHandler",
    "RenderSpecHandler",
    "get_preview_handler",
    "get_render_spec_handler",
    "register_preview_handler",
    "register_render_spec_handler",
]
