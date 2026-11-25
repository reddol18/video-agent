"""이펙트 카탈로그 매니페스트 — 공통 모델 정의 (#306).

설계 의도
---------
지금까지 효과(텍스트/도형/그래픽/필터/모션/모자이크)는

- 백엔드 ffmpeg/PIL 합성 코드
- 프론트 미리보기 표시 코드
- 프론트 효과 추가 패널(EffectAddPanel) 버튼 메타데이터

의 3곳에 *각각 따로* 코드와 라벨이 흩어져 있었다. 새 효과(예: smell)를 추가할 때 한쪽만
구현하고 다른 쪽을 깜빡하면 "백엔드는 새 type을 모르는데 프론트만 버튼이 뜨는" 같은
사고가 났다.

이 모듈은 그 세 위치를 한 매니페스트(`EffectDefinition`)로 통합하기 위한 *공통 모델*이다.
각 효과는 자신의 매니페스트 파일(`text.py`/`shape.py`/...) 하나에서

1. **메타데이터**(이름, 표시 라벨, 카테고리, 색상)
2. **파라미터 명세**(`params: dict[str, ParamSpec]`) — 타입/범위/기본값
3. **프리뷰 지원 여부**(`preview_supported`) — 프론트가 즉시 미리보기를 그릴 수 있는가
4. (선택) **백엔드 ffmpeg 매핑 hint**(`backend_layer_type`) — RenderSpec.Layer.type 과 매칭

만 선언하면 된다. 양쪽 코드가 이 매니페스트를 *읽어* 동작하므로 한쪽만 바꾸는 사고가 사라진다.

본 모듈은 의도적으로 *데이터만* 정의한다. 실제 ffmpeg/PIL 합성 진입점 콜백은
``src/render_spec/effects/_handlers.py`` 의 ``PY_EFFECT_HANDLERS_*`` 레지스트리에
파이썬 dict 로 별도 보관한다 — Pydantic 모델에 직접 callable 을 두면 JSON 직렬화·TS
자동 생성이 깨지기 때문이다. 각 매니페스트 파일(``text.py``/``shape.py``/...)이 자신의
변환·미리보기 핸들러를 함께 등록한다.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── 파라미터 명세 ─────────────────────────────────────────────────────────

class ParamSpec(BaseModel):
    """효과 파라미터 1개의 타입/범위/기본값 명세.

    Pydantic v2 모델이므로 `pydantic2ts` 가 자동으로 TS 타입으로 변환한다.
    값 자체는 `default` 한 필드로 일원화한다(`int|float|str|bool` 유니온).
    프론트는 이 정보를 읽어 입력 위젯(슬라이더/체크박스/셀렉트)을 만들 수 있다.
    """

    # 파라미터의 논리 타입. 실제 값은 default/min/max 필드에 들어간다.
    kind: Literal["int", "float", "str", "bool", "enum"]
    # 표시 라벨(필수). 영어 키와 별도로 한글 라벨을 유지한다.
    label: str
    # 기본값. 타입은 `kind` 에 따라 결정 — TS 측에서는 union 으로 노출.
    default: int | float | str | bool
    # 수치형(int/float)에 한해 범위. 그 외 kind에서는 None.
    min: Optional[float] = None
    max: Optional[float] = None
    # enum 옵션 (kind="enum"에서만 사용)
    options: Optional[list[str]] = None
    # 보조 설명(툴팁)
    description: str = ""


# ── 효과 매니페스트 본체 ──────────────────────────────────────────────────

EffectCategory = Literal[
    "text",
    "shape",
    "graphic",
    "filter",
    "motion",
    "mosaic",
    "image",
]


class EffectDefinition(BaseModel):
    """효과 1종의 매니페스트.

    각 카테고리(text/shape/graphic/filter/motion/mosaic) 내부에서 효과 1개를 식별한다.

    - `name`: 카테고리 내부의 식별자(예: "rect", "ellipse", "spotlight", "shake").
      카테고리가 다르면 같은 name이 있어도 무방하다(`EFFECT_REGISTRY` 키는 `category:name` 조합).
    - `display_label`: UI 버튼에 표시되는 한글 라벨. "+" 같은 prefix는 EffectAddPanel 측에서 붙인다.
    - `category`: 6개 카테고리 중 하나.
    - `params`: 파라미터 명세. 빈 dict면 별도 옵션 없는 효과(예: + 사각형 버튼).
    - `preview_supported`: 프론트가 즉시 미리보기를 그릴 수 있는가. 백엔드 렌더만 가능하면 False.
    - `backend_layer_type`: 이 효과가 RenderSpec 상에서 어떤 Layer 로 매핑되는지 hint.
      ("video"/"image"는 footage이므로 여기서는 등장하지 않는다.)
    - `tooltip`: UI 툴팁(있으면).
    - `notes`: 개발자용 비고(있으면). UI에는 표시되지 않는다.

    본 모델에는 *데이터만* 둔다. 실제 변환·합성 콜백(``register_render_spec_handler`` /
    ``register_preview_handler`` 로 등록)은 ``_handlers.PY_EFFECT_HANDLERS_*`` dict 가
    카테고리 단위로 보관한다 — Pydantic 모델에 callable 을 직접 두면 JSON 직렬화 / TS
    자동 생성이 깨지기 때문에 분리한 것이다.
    """

    name: str
    display_label: str
    category: EffectCategory
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    preview_supported: bool = False
    backend_layer_type: Optional[
        Literal["text", "shape", "graphic", "mosaic"]
    ] = None
    tooltip: str = ""
    notes: str = ""


__all__ = [
    "EffectCategory",
    "EffectDefinition",
    "ParamSpec",
]
