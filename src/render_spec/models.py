"""RenderSpec Pydantic 모델 정의 (#302).

설계 원칙:
1. **추상 키워드 금지** — `fit="cover"` 같은 키워드는 절대 등장하지 않는다. 변환기에서 모두
   `scale_x/scale_y/crop` 픽셀로 풀어낸 결과만 보관한다. 이렇게 하면 백엔드 ffmpeg 매퍼와
   프론트 미리보기가 같은 값을 보고 기계적으로 그리게 되어 분기 차이로 인한 회귀가 사라진다.
2. **시간축과 공간축 분리** — `playback_rate`(시간)와 `scale_x/scale_y/crop`(공간)는 독립
   필드. 어느 한쪽이 1.0이 아니어도 다른 쪽의 계산 결과가 바뀌지 않는다.
3. **모든 좌표·크기는 절대 픽셀(int)** — `Canvas` 좌표계 기준. 비율 좌표는 변환 단계에서
   픽셀로 환산된다.
4. **시간은 항상 초 단위 `float`** — ms나 frame 인덱스를 섞지 않는다.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field


# ── 기본 자료형 ────────────────────────────────────────────────────────────

class Size(BaseModel):
    """가로/세로 픽셀 크기."""
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)


class CropBox(BaseModel):
    """소스(스케일 적용 *후*)에서 잘라낼 사각 영역. 좌상단 기준 픽셀.

    `scale_x/scale_y`를 곱해 키워진 결과 위에서 `(x, y, w, h)` 만큼 잘라낸다.
    예: 9:16 캔버스에 16:9 원본을 cover로 채우면 scale로 양쪽이 캔버스 높이만큼 늘어나고
    그 위에서 가운데 9:16 영역을 잘라내게 된다.
    """
    x: int = Field(..., ge=0)
    y: int = Field(..., ge=0)
    w: int = Field(..., ge=1)
    h: int = Field(..., ge=1)


class Canvas(BaseModel):
    """최종 렌더 캔버스(=프로젝트 출력 해상도)."""
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    fps: int = Field(30, ge=1, le=240)
    # letterbox/pillarbox 여백 배경색. ffmpeg pad 가 받는 토큰(`black` 또는 `0xRRGGBB`).
    # 기본 `black` — 지정하지 않으면 기존과 동일한 검정 여백.
    bg_color: str = "black"


# ── Layer 들 ──────────────────────────────────────────────────────────────

class _LayerBase(BaseModel):
    """모든 레이어 공통 필드."""
    start_sec: float = Field(0.0, ge=0.0)
    end_sec: Optional[float] = None  # None이면 씬 종료까지 자동 연장
    opacity: float = Field(1.0, ge=0.0, le=1.0)


class VideoLayer(_LayerBase):
    """영상(또는 자른 영상) 레이어.

    - `source_size`: 원본 파일 해상도(픽셀). 회전 적용 후 가로/세로가 바뀐 값을 넣는다.
      즉, source_size는 항상 "캔버스에 그릴 직전의" 가시 영역 해상도다.
    - `scale_x/scale_y`: 캔버스를 채우기 위한 단일 배율. cover/contain/stretch가 모두 이
      배율로 환원된다.
    - `crop`: scale 적용 후 캔버스 내부에 그릴 영역. None이면 scale 결과를 그대로 사용.
    - `playback_rate`: footage_speed가 그대로 매핑된다(1.0이면 원속도). fit과 절대로 묶지
      말 것 — 본 필드 분리가 #302 도입 동기.
    - `rotation_deg`: 사용자가 수동으로 지정한 회전. source_size는 이미 회전 후 기준이므로
      파이프라인 매퍼는 이 값을 그대로 ffmpeg `transpose`/CSS `transform` 에 전달만 한다.
    """
    type: Literal["video"] = "video"
    source: str  # footage 파일 경로(또는 footage_dir 기준 상대 경로)
    source_size: Size
    scale_x: float = Field(..., gt=0.0)
    scale_y: float = Field(..., gt=0.0)
    crop: Optional[CropBox] = None
    pad: Optional[CropBox] = None  # 캔버스 내부 배치 영역(contain 시 사용; cover/stretch는 None)
    playback_rate: float = Field(1.0, gt=0.0)
    # 재생 속도 모드 — "ramp" 면 정속(hold) → 선형 가속(accel) → playback_rate 유지.
    # 원본 길이 L 은 (end_sec - start_sec). 기본 constant → 기존 직렬화/매핑 무회귀.
    speed_mode: Literal["constant", "ramp", "ramp_down"] = "constant"
    speed_ramp_hold: float = 0.2
    speed_ramp_accel: float = 0.3
    # ramp 기본 배속(가속의 시작 / 감속의 끝 속도). 기본 1.0.
    speed_ramp_base: float = Field(1.0, gt=0.0)
    rotation_deg: Literal[0, 90, 180, 270] = 0


class ImageLayer(_LayerBase):
    """이미지 + Ken Burns 줌인 레이어."""
    type: Literal["image"] = "image"
    source: str
    source_size: Size
    scale_x: float = Field(..., gt=0.0)
    scale_y: float = Field(..., gt=0.0)
    crop: Optional[CropBox] = None
    pad: Optional[CropBox] = None
    # Ken Burns 줌 비율. (1.0 → 1.15)가 디폴트(기존 _make_scene_preview와 동일).
    zoom_from: float = Field(1.0, gt=0.0)
    zoom_to: float = Field(1.15, gt=0.0)


class TextLayer(_LayerBase):
    """자막/문구 텍스트 오버레이.

    좌표는 캔버스 픽셀 기준. font_size는 px. color는 `#RRGGBB`로 정규화된다.
    `position.x/y`는 텍스트 박스의 *중심* 위치다.
    """
    type: Literal["text"] = "text"
    text: str
    position_x: int
    position_y: int
    font_size_px: int = Field(..., ge=1)
    font_family: str = "NotoSansKR"
    color: str = "#FFFFFF"
    align: Literal["left", "center", "right"] = "center"
    bold: bool = False
    italic: bool = False
    outline_color: str = "#000000"
    outline_width_px: int = 0
    enter_effect: Literal["none", "fade", "typing", "zoom"] = "fade"
    exit_effect: Literal["none", "fade", "typing", "zoom"] = "fade"
    enter_duration_sec: float = 0.3
    exit_duration_sec: float = 0.3
    # 표시 중 반복 애니메이션 — 미리보기/렌더 모두 동일 의미.
    loop_animation: Literal["none", "bounce", "shake", "pulse", "wave"] = "none"
    loop_amplitude: float = 0.02
    loop_period_sec: float = 0.8


class ShapeLayer(_LayerBase):
    """사각형/타원 오버레이. 좌상단 기준 픽셀."""
    type: Literal["shape"] = "shape"
    shape: Literal["rect", "ellipse"] = "rect"
    x: int
    y: int
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    color: str = "#FFFFFF"
    enter_effect: Literal["none", "fade"] = "fade"
    exit_effect: Literal["none", "fade"] = "fade"
    enter_duration_sec: float = 0.3
    exit_duration_sec: float = 0.3


class GraphicLayer(_LayerBase):
    """그래픽 오버레이(spotlight/target/alert/...). 좌표는 캔버스 픽셀(중심 기준)."""
    type: Literal["graphic"] = "graphic"
    graphic_type: Literal["spotlight", "target", "alert", "reaction", "smell", "emoji", "dash"]
    center_x: int
    center_y: int
    params: dict = Field(default_factory=dict)


class MosaicLayer(_LayerBase):
    """영역 모자이크/블러. 좌상단 기준 픽셀."""
    type: Literal["mosaic"] = "mosaic"
    shape: Literal["rect", "ellipse"] = "rect"
    x: int
    y: int
    width: int = Field(..., ge=1)
    height: int = Field(..., ge=1)
    mode: Literal["pixelate", "blur"] = "pixelate"
    pixel_size: int = Field(20, ge=1)
    blur_radius: int = Field(20, ge=0)


class FilterLayer(_LayerBase):
    """씬 전체 색감/무드 필터 (#206 SceneFilter 의 RenderSpec 표현).

    builder 가 ``scene.filters`` 항목을 그대로 보존해 layers 에 끼워 넣는다. 클라이언트는
    이 레이어가 한 개라도 있으면 단순 미리보기를 포기하고 백엔드 렌더로 폴백한다 (#343).

    설계 메모:
    - ``intensity`` / ``params`` 는 백엔드 ``clip_editor.apply_scene_filters`` 가 해석하는
      값과 그대로 매칭되도록 둔다. RenderSpec 단계에서 별도 변환은 하지 않는다 (추상 키워드
      금지 원칙 위배가 아닌가? — 색감 필터는 "키워드 → 픽셀"이 1:1 의미를 갖지 않는 정성적
      효과라 키워드 자체가 단일 진실이다. fit/motion 같은 *기하* 변환만 픽셀 환원 대상).
    - ``filter_type`` 은 ``SceneFilter.filter_type`` Literal 과 동일 집합을 유지한다.
    """
    type: Literal["filter"] = "filter"
    filter_type: Literal["flashback", "horror", "vivid", "nightvision", "pov", "warm", "cool"]
    intensity: float = Field(1.0, ge=0.0, le=1.0)
    params: dict = Field(default_factory=dict)


class MotionLayer(_LayerBase):
    """씬 내 화면 다이나믹 효과 (#207 SceneMotion 의 RenderSpec 표현).

    shake / clip_zoom 등 시간 변화가 있는 효과를 표현한다. 단순 미리보기에서는 해석할 수
    없으므로 ``isSimpleRenderSpec`` 이 자동으로 false 를 돌려준다 (#343).

    - ``motion_start_sec`` / ``motion_duration_sec`` 은 ``SceneMotion`` 의 ``start_sec`` /
      ``duration_sec`` 과 동일. 부모 ``_LayerBase.start_sec`` 는 *레이어 활성 구간*이며
      모션은 씬 전체에 걸쳐 활성이라 0 으로 둔다.
    """
    type: Literal["motion"] = "motion"
    motion_type: Literal["shake", "slowmo", "clip_zoom", "stutter"]
    motion_start_sec: float = Field(0.0, ge=0.0)
    motion_duration_sec: float = Field(1.0, gt=0.0)
    params: dict = Field(default_factory=dict)


Layer = Annotated[
    Union[
        VideoLayer,
        ImageLayer,
        TextLayer,
        ShapeLayer,
        GraphicLayer,
        MosaicLayer,
        FilterLayer,
        MotionLayer,
    ],
    Field(discriminator="type"),
]


# ── 루트 모델 ─────────────────────────────────────────────────────────────

class RenderSpec(BaseModel):
    """씬 1개에 해당하는 정규화된 렌더 명세.

    `scene_id`는 같은 프로젝트 내 씬을 구분하기 위해 보관한다. 캐시 파일에는 씬 리스트로
    저장된다.
    """
    version: int = 1
    scene_id: int
    canvas: Canvas
    duration_sec: float = Field(..., gt=0.0)
    layers: list[Layer] = Field(default_factory=list)
