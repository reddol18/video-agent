from __future__ import annotations
from typing import Any, Literal, Optional, Union
from pydantic import AliasChoices, BaseModel, ConfigDict, Field


# ── Overlay enter/exit effects (#287) ──────────────────────────────────────
# 모든 오버레이(텍스트/도형/그래픽)는 등장/사라짐 효과를 가진다.
#   공통: "none"(효과 없음), "fade"(알파 페이드)
#   텍스트 전용 추가: "typing"(부분 문자열 누적), "zoom"(폰트크기 보간)
# 도형/그래픽은 typing/zoom을 데이터로 받아도 백엔드는 fade로 폴백한다 (회귀 안전).
# 기본값은 fade + 0.3초로 기존 데이터(필드 없음)와 호환되도록 한다.

# pass_* 는 "관통" — 한쪽 화면 밖에서 날아 들어와 원위치를 지나 반대편 화면 밖으로 빠져나간다.
# enter_effect 에만 지정하면 exit 방향은 렌더러가 자동으로 반대편으로 연결한다.
TextEnterExitEffect = Literal[
    "none",
    "fade",
    "typing",
    "zoom",
    "pass_left_to_right",
    "pass_right_to_left",
    "pass_top_to_bottom",
    "pass_bottom_to_top",
]
# 이미지 오버레이 등 non-text 오버레이의 등장/사라짐 효과.
#   기본: "none", "fade"
#   이미지 오버레이 확장: slide_in_left/right/top/bottom (방향별 슬라이드), zoom(작게→원래), pop(크게→원래+bounce)
# 도형(ShapeOverlay)/그래픽(GraphicOverlay) 는 이 Literal 을 공유하되 slide/zoom/pop 값을 데이터로 받아도
# clip_editor 렌더러가 fade 로 폴백 처리한다 (이미지 오버레이 전용 렌더 로직).
NonTextEnterExitEffect = Literal[
    "none",
    "fade",
    "slide_in_left",
    "slide_in_right",
    "slide_in_top",
    "slide_in_bottom",
    "zoom",
    "pop",
    "pass_left_to_right",
    "pass_right_to_left",
    "pass_top_to_bottom",
    "pass_bottom_to_top",
]

# ── Text Loop Animation ───────────────────────────────────────────────────
# 텍스트가 표시되는 동안(enter/exit 사이) 반복적으로 적용되는 모션 효과.
# 'none'이면 정적. 진폭/주기는 모두 정규화 단위로 표현하여 폰트 크기/캔버스 해상도에 따라 자연스러운 값으로 변환된다.
# speedlines 는 집중선이 "글자가 차지하는 영역 안에만" 마스킹되어 나타나는 효과.
# 화면 전체 집중선(SpeedLines)과 같은 생성기/캐시를 쓰되 글자 알파로 잘라낸다.
TextLoopAnimation = Literal["none", "bounce", "shake", "pulse", "wave", "speedlines"]


class TextSpeedLinesConfig(BaseModel):
    """텍스트 마스킹 집중선(loop_animation="speedlines") 파라미터.

    모두 선택값이며, 생략 시 렌더러가 씬 집중선과 동일한 기본값을 쓴다.
    color 를 비우면 글자색을, center_x/center_y 를 비우면 글자 위치를 따라간다.
    """
    color: str | None = None
    density: int | None = None
    center_x: float | None = None
    center_y: float | None = None
    # 선이 글자 외곽에서 안쪽으로 파고드는 깊이 비율(0~1). 기본 0.72.
    depth: float | None = None
    # ── 표현 모드 ───────────────────────────────────────────────────────────
    # "tail"(기본) — 글자가 날아온 방향으로 집중선 스타일 꼬리를 글자 *밖*에 그린다.
    #                획을 건드리지 않아 글자가 온전히 읽힌다. 관통(pass_*) 효과 필요.
    # "inner"      — 글자 획 *안쪽*을 집중선으로 갉아낸다. 아주 큰 글자에만 어울린다.
    mode: Literal["tail", "inner"] | None = None
    # 꼬리 길이(글자 크기 대비 배수). 기본 0.55.
    tail_length: float | None = None
    # 꼬리 불투명도(0~1). 기본 0.85.
    tail_opacity: float | None = None
    # 꼬리 줄무늬 주기(px). 작을수록 선이 촘촘하다. 기본 9.
    tail_stripe: int | None = None
    inner_ratio: float | None = None
    thickness: float | None = None
    width_variance: float | None = None
    jitter: float | None = None
    seed: int | None = None
    supersample: int | None = None
    num_buckets: int | None = None
    animate: bool | None = None
    anim_speed: float | None = None
    anim_period_sec: float | None = None


# ── Text Overlay ───────────────────────────────────────────────────────────

class GradientStop(BaseModel):
    color: str
    position: float  # 0.0 ~ 1.0


class GradientConfig(BaseModel):
    angle: float = 0.0
    stops: list[GradientStop] = Field(default_factory=list)


class TextOverlay(BaseModel):
    id: str
    text: str
    x: float = 0.5        # 0.0 ~ 1.0
    y: float = 0.1        # 0.0 ~ 1.0
    font_size: int = 40
    font_family: str = "NotoSansKR"
    color: str = "#ffffff"
    gradient: Optional[GradientConfig] = None
    bold: bool = False
    italic: bool = False
    outline_color: str = "#000000"
    outline_width: int = 2
    glow_color: str = "#000000"
    glow_radius: int = 0
    opacity: float = 1.0
    align: Literal["left", "center", "right"] = "center"
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    # ── SFX (#290) — 오버레이별 1개 SFX, enter 시점에 트리거 ────────────────
    # sfx_asset_id가 None이면 SFX 없음. studio_assets에서 audio 타입 자산을 참조.
    # sfx_volume은 0.0~1.0 (기본 0.7). TTS 자동 더킹은 최종 렌더 단계에서 적용.
    sfx_asset_id: Optional[str] = None
    sfx_volume: float = 0.7
    # ── 등장/사라짐 효과 (#287) ────────────────────────────────────────────
    # 기존 데이터(필드 없음)는 fade + 0.3초로 회귀 안전.
    enter_effect: TextEnterExitEffect = "fade"
    exit_effect: TextEnterExitEffect = "fade"
    enter_duration_sec: float = 0.3
    exit_duration_sec: float = 0.3
    # ── 표시 중 반복 애니메이션 ─────────────────────────────────────────────
    # bounce(위아래 통통), shake(좌우 빠른 흔들림), pulse(폰트 크기 숨쉬기), wave(좌우 부드럽게 떨림)
    # loop_amplitude는 효과별로 의미가 다르다:
    #   - bounce/wave: 캔버스 높이 또는 폭 대비 비율 (기본 0.02 → 1080p에서 약 22px)
    #   - shake: 폰트 크기 대비 비율 (기본 0.05 → fs=40이면 ±2px)
    #   - pulse: 1.0 기준 스케일 진폭 (기본 0.1 → 0.9~1.1배)
    # loop_period_sec: 한 사이클 길이. shake는 빠른 진동이라 이 값과 별개로 내부 빈도 고정.
    # 기존 데이터(필드 없음)는 'none' + 기본값으로 회귀 안전.
    loop_animation: TextLoopAnimation = "none"
    loop_amplitude: float = 0.02
    loop_period_sec: float = 0.8
    # loop_animation="speedlines" 일 때만 쓰이는 집중선 파라미터. None 이면 전부 기본값.
    text_speed_lines: Optional[TextSpeedLinesConfig] = None
    # ── 카운트다운 (N초부터 1씩 줄어드는 숫자) ───────────────────────────────
    # countdown_from > 0 이면 이 오버레이는 카운트다운으로 렌더된다. `text` 는 무시되고
    # start_sec 부터 1초(countdown_interval_sec)마다 N, N-1 … countdown_to 로 바뀐다.
    # 폰트/색/외곽선/위치/애니메이션 등 나머지 스타일 필드는 일반 텍스트와 완전히 동일하게
    # 적용된다(같은 drawtext 경로를 재사용). 0 이면 기존 동작(일반 텍스트) — 회귀 안전.
    countdown_from: int = 0
    # 카운트다운이 끝나는 숫자(포함). 기본 1 — "3,2,1". 0 으로 두면 "3,2,1,0".
    countdown_to: int = 1
    # 숫자 한 개가 머무는 시간(초). 기본 1.0 = 1초에 하나씩.
    countdown_interval_sec: float = 1.0
    # 카운트다운이 끝난 뒤 표시할 문구(예: "시작!"). 빈 문자열이면 아무것도 안 띄운다.
    countdown_end_text: str = ""


# ── Effect Configs ─────────────────────────────────────────────────────────

class ClipEffect(BaseModel):
    type: Literal["clip"] = "clip"
    file: str
    x_offset: float = 0.0  # 비율 -1.0~1.0 수평 오프셋 (양수: 오른쪽, 음수: 왼쪽)
    y_offset: float = 0.0  # 비율 -1.0~1.0 수직 오프셋 (양수: 아래, 음수: 위)
    # ── #258 footage 종횡비 fit 옵션 ─────────────────────────────────────────
    # footage 영상의 종횡비와 출력 종횡비가 다를 때 처리 방식.
    #   "contain" — 비율 유지하며 letterbox/pillarbox 패딩 (기본, 기존 동작)
    #   "cover"   — 비율 유지하며 출력 영역을 가득 채우고 넘치는 부분은 잘라냄
    #   "stretch" — 비율 무시하고 출력 해상도에 맞게 늘림
    # 기존 프로젝트(필드 없음)는 default="contain"으로 회귀 보장.
    fit: Literal["contain", "cover", "stretch"] = "contain"
    # ── #263 원본 영상 수동 회전 옵션 ───────────────────────────────────────
    # 0/90/180/270 도. 0이 기본(회전 없음). 0이 아니면 자동 회전 휴리스틱(_needs_rotation) 비활성화.
    # 적용 순서: rotation → fit/scale → crop/pad. 회전 후 종횡비 기준으로 fit이 처리되며,
    # x_offset/y_offset도 회전 후 좌표계 기준.
    rotation: Literal[0, 90, 180, 270] = 0
    # 씬 나누기 — 같은 footage를 여러 씬이 공유하면서 각 씬이 다른 구간을 보여줄 때 사용.
    # None이면 0~footage 끝까지로 해석.
    clip_start_sec: Optional[float] = None
    clip_end_sec: Optional[float] = None


class KenburnsEffect(BaseModel):
    type: Literal["kenburns"] = "kenburns"
    image: str
    # 이미지가 출력 비율과 다를 때 처리 방식.
    # cover(기본): 영역 가득 채움 + crop / contain: 여백 / stretch: 찌그러뜨림.
    fit: Literal["cover", "contain", "stretch"] = "cover"


class CopyImgOptions(BaseModel):
    input: str
    output: str
    extra: dict[str, Any] = Field(default_factory=dict)


class KenburnsCopyImgEffect(BaseModel):
    type: Literal["kenburns_copy_img"] = "kenburns_copy_img"
    copy_img_options: CopyImgOptions
    image: str


class EditEffect(BaseModel):
    type: Literal["edit"] = "edit"
    description: str
    file: str
    min_duration: float = 5.0
    preview_placeholder: bool = True


class SyncEffect(BaseModel):
    type: Literal["sync"] = "sync"
    word: str
    inner: Union[EditEffect, ClipEffect, None] = None
    preview_placeholder: bool = True


class GridBorder(BaseModel):
    """그리드 셀 광원 그라데이션 보더 설정.

    animate=True 이면 재생 중 광원(hue)이 회전하며 살아있는 느낌을 준다.
    모든 필드는 옵셔널 기본값 — 보더 미지정 셀은 기존 무보더 동작과 동일하게 렌더된다.
    """
    width_px: int = 0                    # 보더 두께(px). 0이면 보더 없음.
    color_from: str = "#00e5ff"          # 그라데이션 시작 색 (hex)
    color_to: str = "#a855f7"            # 그라데이션 끝 색 (hex)
    animate: bool = True                 # 광원 회전 애니메이션 on/off
    speed: float = 1.0                   # 광원 회전 속도 배수 (1.0 = 1바퀴/1초 기준 계수)


class GridCell(BaseModel):
    """N×M 그리드의 셀 하나. file 이 비면 배경색만 채워진다."""
    file: str = ""
    border: Optional[GridBorder] = None


class MultiscreenEffect(BaseModel):
    type: Literal["multiscreen"] = "multiscreen"
    # ── 기존 동작 (회귀 보존) ──────────────────────────────────────────────
    # files 만 지정하면 기존 xstack 멀티스크린(auto/grid2x2/triptych 등)과 100% 동일.
    files: list[str] = []
    # ── N×M 그리드 비교 확장 (옵셔널) ───────────────────────────────────────
    # rows/cols/cells 가 모두 채워지면 grid_nxm 렌더 경로로 진입한다.
    # 미지정(None)이면 기존 files 기반 동작 유지.
    rows: Optional[int] = None
    cols: Optional[int] = None
    cells: Optional[list[GridCell]] = None


class TitleOptions(BaseModel):
    text: str
    mode: str = "shimmer"
    duration: float = 3.0
    font_path: str | None = None
    font_size: int = 80
    text_color: str = "white"
    rect_color: str | None = None
    bg_color: str = "black"
    dissolve_time: float = 0.5
    spc_word: str | None = None


class TitleGenEffect(BaseModel):
    type: Literal["title_gen"] = "title_gen"
    options: TitleOptions


EffectConfig = Union[
    ClipEffect,
    KenburnsEffect,
    KenburnsCopyImgEffect,
    EditEffect,
    SyncEffect,
    MultiscreenEffect,
    TitleGenEffect,
]


# ── Shape Overlay ──────────────────────────────────────────────────────────

class ShapeOverlay(BaseModel):
    id: str
    shape: Literal["rect", "ellipse"]
    x: float        # 0.0~1.0 비율 x 좌표 (좌상단 기준)
    y: float        # 0.0~1.0 비율 y 좌표 (좌상단 기준)
    width: float    # 0.0~1.0 비율 너비
    height: float   # 0.0~1.0 비율 높이
    color: str      # hex 색상 (예: "#ff0000")
    opacity: float = 1.0  # 0.0 ~ 1.0
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    # ── SFX (#290) — 오버레이별 1개 SFX, enter(start_sec) 시점에 트리거 ────
    sfx_asset_id: Optional[str] = None
    sfx_volume: float = 0.7
    # ── 등장/사라짐 효과 (#287) ────────────────────────────────────────────
    # 도형 오버레이는 fade/none만 지원. typing/zoom은 데이터로 와도 fade로 폴백.
    enter_effect: NonTextEnterExitEffect = "fade"
    exit_effect: NonTextEnterExitEffect = "fade"
    enter_duration_sec: float = 0.3
    exit_duration_sec: float = 0.3


# ── Image Overlay (#image) ─────────────────────────────────────────────────
# footage 스튜디오에 등록된 image 자산을 씬 위에 자유 위치·크기로 합성하는 오버레이.
# 주 용도: "이전 영상 썸네일 + 제목" 카드처럼 이미지 + 캡션을 한 묶음으로 표시.
#
# 좌표 규약: ShapeOverlay/MosaicOverlay와 동일. (x, y) = 좌상단 기준 0~1 비율,
# (width, height) = 캔버스 폭/높이 대비 비율. `height`는 *이미지 영역만* 가리키며,
# caption_text가 비어있지 않으면 이미지 바로 아래에 캡션 박스가 추가로 그려진다
# (이미지 면적은 유지된다 — 사용자가 width/height를 줄여도 이미지가 잘리지 않음).
#
# 적용 순서: filters → motions → mosaic_overlays → image_overlays → text/shape/graphic.
# 이미지 오버레이가 텍스트/도형/그래픽보다 *아래*에 깔리므로, 썸네일 위에 별도 자막
# 텍스트나 도형 장식을 얹는 합성이 자연스럽게 가능하다.

class ImageOverlay(BaseModel):
    id: str
    # studio_assets 중 asset_type="image"인 자산의 asset_id 를 참조한다.
    # 렌더링 시 백엔드가 project.studio_assets / footage_dir 로 실제 파일 경로를 해석한다.
    image_asset_id: str
    # 좌상단 기준 비율 좌표 (도형 오버레이와 동일 규약)
    x: float = 0.05
    y: float = 0.05
    width: float = 0.25            # 캔버스 폭 대비 비율 — 이미지 영역만
    height: float = 0.20           # 캔버스 높이 대비 비율 — 이미지 영역만 (캡션은 별도 공간)
    opacity: float = 1.0
    # 회전은 1차에서 데이터만 보관, 렌더링은 후속 (현재는 무시 → 0과 동일 동작).
    rotation_deg: float = 0
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    # ── 캡션 (선택) ────────────────────────────────────────────────────────
    # caption_text가 None 또는 빈 문자열이면 캡션 박스를 그리지 않는다 (이미지만 표시).
    caption_text: Optional[str] = None
    caption_font_size: float = 0.025   # 캔버스 폭 대비 비율
    caption_color: str = "#FFFFFF"
    caption_bg_color: Optional[str] = "#000000"
    caption_bg_opacity: float = 0.6
    caption_padding: float = 0.005     # 캡션 박스 내부 여백 (캔버스 폭 대비)
    # ── 등장/사라짐 효과 — 다른 오버레이와 동일한 fade/none 정책 ───────────
    enter_effect: NonTextEnterExitEffect = "fade"
    exit_effect: NonTextEnterExitEffect = "fade"
    enter_duration_sec: float = 0.3
    exit_duration_sec: float = 0.3
    # ── SFX (#290) — 오버레이별 1개 SFX, enter(start_sec) 시점에 트리거 ────
    sfx_asset_id: Optional[str] = None
    sfx_volume: float = 0.7
    # ── Scene Tag 시스템 (#215) — 다른 오버레이와 통일 ─────────────────────
    source_id: Optional[str] = None


# ── Mosaic Overlay (#264) ──────────────────────────────────────────────────
# 영상의 일부 영역에 모자이크/블러를 입혀 가리는 오버레이.
# 도형 오버레이(ShapeOverlay)와 동일한 좌표·크기 표현(좌상단 기준 0~1 비율)을 사용한다.
# 1차 구현 범위: shape="rect" 정식 렌더링. shape="ellipse"는 데이터만 받고
# 렌더러에서는 사각형으로 폴백한다(후속 사이클에서 Pillow 타원 알파 마스크 적용 예정).
# 적용 순서: filters → motions → mosaic_overlays → image_overlays → text/shape/graphic overlays.

class MosaicOverlay(BaseModel):
    id: str
    shape: Literal["rect", "ellipse"] = "rect"
    # 좌상단 기준 비율 좌표 (도형 오버레이와 동일 규약)
    x: float = 0.4
    y: float = 0.4
    width: float = 0.2
    height: float = 0.2
    # pixelate 격자 한 칸 크기 (px). 클수록 거칠게 모자이크된다.
    pixel_size: int = 20
    # 적용 방식: pixelate=다운/업스케일 픽셀화, blur=가우시안 박스블러
    mode: Literal["pixelate", "blur"] = "pixelate"
    # mode="blur"일 때만 사용. boxblur 반경(px).
    blur_radius: int = 20
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    # ── Scene Tag 시스템 (#215) — 다른 오버레이와 통일 ─────────────────────
    source_id: Optional[str] = None


# ── Graphic Overlay (#205) ─────────────────────────────────────────────────
# UI/감정용 그래픽 오버레이. type별 세부 파라미터는 params(dict)에 자유 형식으로 보관한다.
# 1차 구현 범위: spotlight, alert 2종 실제 렌더링. 나머지 type은 데이터만 받고 렌더링은 후속 사이클.

class GraphicOverlay(BaseModel):
    id: str
    # ── 그래픽 오버레이 type (#205/#216/#288) ──────────────────────────────
    # spotlight/target/alert/reaction/smell: 기존 PIL 렌더 5종
    # emoji: 이모지 아이콘 (#288) — params.icon_type에 question/lens/prohibit/poo
    # dash: 만화식 "=3=3=3" 질주 표시 — 마크가 진행 방향으로 흘러가며 반복(애니메이션).
    #   params: mark(표시 문자, 기본 "=3"), count(마크 개수), size(캔버스 폭 대비 비율),
    #           color(#RRGGBB), direction("left"|"right"), speed(초당 이동량 배수),
    #           spacing(마크 간격, size 대비 배수), trail(꼬리 페이드 on/off)
    type: Literal["spotlight", "target", "alert", "reaction", "smell", "emoji", "dash"]
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    x: float = 0.5  # 0.0~1.0 비율 (중심 기준)
    y: float = 0.5  # 0.0~1.0 비율 (중심 기준)
    params: dict = Field(default_factory=dict)
    # ── Scene Tag 시스템 (#215) ─────────────────────────────────────────────
    # 태그가 자동 추가한 오버레이를 출처 추적 위해 부여하는 옵셔널 ID 식별자.
    # 형식: "tag-{tagname}-graphic" (sceneTags.applySceneTags 부여 규칙).
    # 유저가 수동 추가한 오버레이는 None.
    source_id: Optional[str] = None
    # ── SFX (#290) — 오버레이별 1개 SFX, enter(start_sec) 시점에 트리거 ────
    sfx_asset_id: Optional[str] = None
    sfx_volume: float = 0.7
    # ── 등장/사라짐 효과 (#287) ────────────────────────────────────────────
    # 그래픽 오버레이는 fade/none만 지원. typing/zoom은 데이터로 와도 fade로 폴백.
    enter_effect: NonTextEnterExitEffect = "fade"
    exit_effect: NonTextEnterExitEffect = "fade"
    enter_duration_sec: float = 0.3
    exit_duration_sec: float = 0.3


# ── Speed Lines Overlay (집중선/속도선) ─────────────────────────────────────
# 만화/웹툰 특유의 "집중선" — 화면 중심점에서 사방으로 뻗는 날카로운 쐐기 선들로 박진감을
# 준다. cairosvg/rsvg 가 없으므로 Pillow 로 고해상도 PNG 를 프로시저럴 생성해 합성한다
# (src/speed_lines.py). 파라미터+해상도 해시 기반 캐시로 미리보기 반복 시 재생성 비용을 줄인다.
#
# 적용 순서: 다른 오버레이 위(최상단)에 얹혀 화면 전체에 박진감을 준다.
# start_sec/end_sec, fade in/out 은 다른 오버레이와 동일 규약. footage_speed/clip_zoom 과
# 충돌 없음(오버레이는 최종 합성 단계에서 프레임 위에 얹힘).

class SpeedLinesOverlay(BaseModel):
    id: str
    # 선 색상 (#RRGGBB). 기본 검정 — 흰 배경 만화 느낌. 흰색/빨강도 자주 씀.
    color: str = "#000000"
    # 선 개수(밀도). 많을수록 촘촘.
    density: int = 80
    # 중심점 (0~1 비율). 세로 영상에선 인물 얼굴 근처(예: 0.5, 0.4)로 두는 경우가 많다.
    center_x: float = 0.5
    center_y: float = 0.5
    # 중앙 빈 타원 영역 반경 (영역 반변 대비 비율). 피사체가 가려지지 않도록 비워둔다.
    # 개선2: area_w != area_h 면 빈 영역이 타원이 된다(x/y 반경이 area 비율대로 다름).
    # 개선1: 상한이 커져(1.5) 큰 빈원을 만들 수 있다.
    inner_ratio: float = 0.28
    # 바깥쪽 선 굵기 배수 (1.0 = 표준).
    thickness: float = 0.9
    # 개선4: 선별 바깥 굵기 랜덤 편차 (0=균일, 0.2=±20%). 자연스러운 손그림 두께 편차.
    width_variance: float = 0.2
    # 각도 랜덤 지터 (0=균등, 1=손그림 느낌).
    jitter: float = 0.6
    # 재현용 시드 — 같은 시드+파라미터면 항상 같은 그림(캐시 재현성).
    seed: int = 0
    # 렌더 캔버스 배율 (2~3 권장). 확대 대비 안티에일리어싱.
    supersample: int = 3
    # ── 표시 영역 크기 (bounding box) ───────────────────────────────────────
    # 집중선이 방사되는 영역의 크기(비율). (center_x, center_y) 를 중심으로 하는
    # 폭 area_w·높이 area_h 의 사각형 안에만 방사선이 그려진다. 기본 1.0=전체 화면
    # (기존 씬 = 필드 없음 → 1.0 로 로드되어 전체 방사, 회귀 안전). 값이 작을수록
    # 좁은 영역에만 집중선이 나타나며 영역 밖은 투명하게 클립된다.
    area_w: float = 1.0
    area_h: float = 1.0
    opacity: float = 1.0
    start_sec: float = 0.0
    end_sec: Optional[float] = None
    # ── 선별 페이드 인/아웃 반복 애니메이션 ──────────────────────────────────────
    # animate=True 면 각 선이 최대 길이로 고정된 채 독립적으로 페이드 인→아웃→(안 보임)
    # →다시 페이드 인을 반복한다. 각 선의 페이드 위상은 seed 기반 완전 랜덤 → 무작위로
    # 깜빡이는 박진감. (과거 "선 길이 성장 anim_t" 방식은 폐기.)
    # 구현: 선을 페이즈 버킷 num_buckets 개로 나눠 버킷별 PNG 를 생성하고, 프레임마다
    # 각 버킷의 페이드 알파(sin^2 왕복)로 합성한다. anim_speed/anim_period_sec 로 페이드
    # 주기를 제어한다. 기존 데이터(필드 없음)는 animate=True 기본 → 자동으로 켜진다.
    animate: bool = True
    # 페이드 버킷 개수 — 선을 이 개수의 위상 버킷에 나눠 배정한다. 클수록 깜빡임 타이밍이
    # 더 세분화(부드러움)되지만 오버레이당 생성 PNG 수가 늘어난다. 기존 데이터 호환(기본 8).
    num_buckets: int = 8
    # (deprecated) 과거 scale 방식의 시작 배율. 현재 방식에서는 사용하지 않으나
    # 기존 데이터 로드 호환을 위해 필드를 유지한다.
    start_scale: float = 1.4
    # 페이드 속도 배수(클수록 빨리 깜빡).
    anim_speed: float = 1.0
    # 한 페이드 사이클의 기준 길이(초). anim_speed 로 나눠 실제 주기를 정한다.
    anim_period_sec: float = 0.7
    # ── 등장/사라짐 효과 — 다른 오버레이와 동일 fade/none 정책 ───────────────
    enter_effect: NonTextEnterExitEffect = "fade"
    exit_effect: NonTextEnterExitEffect = "fade"
    enter_duration_sec: float = 0.3
    exit_duration_sec: float = 0.3
    # ── Scene Tag 시스템 — 다른 오버레이와 통일 ─────────────────────────────
    source_id: Optional[str] = None


# ── Scene Filter (#206) ────────────────────────────────────────────────────
# 씬 전체 색감/무드 필터. 적용 순서는 cut_clip 직후, 텍스트/도형/그래픽 오버레이 *이전*.
# 1차 구현 범위: flashback / horror / vivid / nightvision 4종 실제 렌더링.
# pov는 데이터만 받고 렌더링은 후속 사이클(Pillow radial mask + 키프레임).

class SceneFilter(BaseModel):
    filter_type: Literal["flashback", "horror", "vivid", "nightvision", "pov", "warm", "cool", "cartoon"]
    intensity: float = 1.0  # 0.0 ~ 1.0
    params: dict = Field(default_factory=dict)
    # ── Scene Tag 시스템 (#215) ─────────────────────────────────────────────
    # 태그가 자동 추가한 필터를 출처 추적 위해 부여하는 옵셔널 ID 식별자.
    # 형식: "tag-{tagname}-filter" (sceneTags.applySceneTags 부여 규칙).
    # 유저가 수동 추가한 필터는 None.
    source_id: Optional[str] = None


# ── Scene Motion (#207) ───────────────────────────────────────────────────
# 씬 내 화면 다이나믹 효과 (셰이크 / clip 줌 / 슬로우 모션).
# 적용 순서: cut_clip → filters → motions → text/shape/graphic overlays.
# 1차 구현 범위: shake / clip_zoom / stutter 실제 렌더링 (영상 길이 변경 없음).
# stutter(#347): 끊김/점프 모션. params.intensity(0.0~1.0)로 강도 조절.
# slowmo는 데이터만 받고 렌더링은 후속 사이클 (영상 길이 변경 + narration_timeline 정합성 처리 필요).

class SceneMotion(BaseModel):
    motion_type: Literal["shake", "slowmo", "clip_zoom", "stutter"]
    start_sec: float = 0.0      # 씬 내 상대 시작 시각
    duration_sec: float = 1.0   # 효과 지속 시간
    params: dict = Field(default_factory=dict)
    # ── Scene Tag 시스템 (#215) ─────────────────────────────────────────────
    # 태그가 자동 추가한 모션을 출처 추적 위해 부여하는 옵셔널 ID 식별자.
    # 형식: "tag-{tagname}-motion" (sceneTags.applySceneTags 부여 규칙).
    # 유저가 수동 추가한 모션은 None.
    source_id: Optional[str] = None


# ── Match metadata ─────────────────────────────────────────────────────────

class MatchMeta(BaseModel):
    chunk_id: str
    source_file: str
    trim_start: float
    trim_end: float
    match_score: float = 0.0
    match_reason: str = ""


class TtsMeta(BaseModel):
    audio_url: str
    duration_sec: float
    status: Literal["idle", "generating", "ready", "error"] = "idle"
    synced: bool = False  # 현재 narration_text 기준으로 TTS가 생성됐으면 True


# ── NarrationSegment (#382) ───────────────────────────────────────────────
# 씬 1개에 여러 개의 TTS 구간(N:1)을 가지기 위한 단위.
# - seg_0 (=시나리오 파서가 생성하는 첫 segment)만 TTS 길이에 따라 씬 길이 자동 연장.
# - seg_1 이후 사용자가 TimelineBar 클릭으로 추가한 segment는 씬 길이 자동 연장 없음.
#   `offset_sec + duration_sec > 씬 길이` 이면 UI 빨간 마커 + 렌더 시 씬 경계 atrim.
# - 회귀 안전: SceneCard.narration_text / SceneCard.tts 는 마이그레이션용으로 유지하고
#   새 코드는 SceneCard.narration_segments 만 참조한다.

class NarrationSegment(BaseModel):
    id: str                       # "seg_0", "seg_1" ... 또는 uuid
    text: str = ""                # 자막/TTS 원문
    offset_sec: float = 0.0       # 씬 시작 기준 시작 시점
    audio_url: str = ""           # /api/projects/{pid}/tts/segment/{seg_id}/audio
    duration_sec: float = 0.0     # TTS 실제 길이
    status: Literal["idle", "generating", "ready", "error"] = "idle"
    synced: bool = False           # 현재 text 기준으로 TTS 생성됐는지
    voice_name: Optional[str] = None  # segment별 voice override (None=프로젝트 기본)
    sbv_text: Optional[str] = None    # 자막용 별도 텍스트 (None이면 text 사용)
    # #392 — TTS 생성 실패 시 사용자에게 표시할 에러 메시지.
    # status == "error" 일 때만 의미 있음. 성공 시(ready) 자동으로 None 으로 리셋된다.
    error_message: Optional[str] = None


# ── Scene Comment (편집 지시 코멘트) ─────────────────────────────────────────
# 김감독이 씬에 남기는 편집 지시 메모. 나중에 클로드 에이전트가 읽고 수정한 뒤
# resolve API 로 완료 처리한다. 완료된 코멘트는 삭제하지 않고 이력으로 남긴다.
#
# 상태 전이는 open → done 단방향 (재오픈은 reopen API).
# resolved_* 필드는 status == "done" 일 때만 의미가 있다.

class CommentImage(BaseModel):
    """편집 코멘트에 붙인 참조 이미지 1건.

    코멘트 본문에는 `[이미지1]` 같은 자리표시자로 등장하고, 실제 파일은
    web/data/{project_id}/comment_images/ 에 저장된다. `index` 가 자리표시자
    번호(1-base)이며, 에이전트는 이 번호로 본문 ↔ 파일을 매칭한다.
    """
    id: str                                  # uuid4 hex
    index: int                               # 본문 [이미지N] 의 N (1-base)
    filename: str                            # 저장된 파일명 (project 내 유일)
    path: str                                # 에이전트가 바로 읽을 수 있는 절대경로
    url: str                                 # 브라우저 표시용 API URL
    mime: str = "image/png"
    size: int = 0                            # bytes
    created_at: str = ""                     # ISO8601 UTC


class SceneComment(BaseModel):
    id: str                                  # uuid4 hex
    body: str                                # 편집 지시 원문 (필수, 공백만이면 거부)
    status: Literal["open", "done"] = "open"
    created_at: str = ""                     # ISO8601 UTC
    author: str = "user"                     # 남긴 주체. 현재는 항상 "user"
    # 붙임 이미지 — 본문의 [이미지N] 자리표시자와 index 로 대응한다.
    images: list[CommentImage] = Field(default_factory=list)
    # ── 완료 처리 기록 ──────────────────────────────────────────────────────
    resolved_at: Optional[str] = None        # ISO8601 UTC
    resolved_by: Optional[str] = None        # "agent" | "user"
    resolution_note: Optional[str] = None    # 에이전트가 무엇을 고쳤는지 한 줄 요약


# ── Scene Card ─────────────────────────────────────────────────────────────

class SceneCard(BaseModel):
    scene_id: int
    title: str = ""
    description: str = ""
    mood: str = ""
    keywords: list[str] = Field(default_factory=list)
    duration_hint_sec: float = 5.0
    narration_text: str = ""
    # ── #stale-write — narration_text 의 서버측 편집 리비전 ──────────────────
    # register(전체 프로젝트 push)가 옛 스냅샷으로 최신 대본을 덮어쓰는 사고를
    # 막기 위한 값. narration_text 가 실제로 바뀔 때마다 서버가 1씩 올린다.
    # 클라이언트는 자기가 읽은 값을 그대로 돌려보내며, 서버는 자신이 가진 값보다
    # 낮은 리비전의 narration_text 쓰기를 거부한다(=stale write 차단).
    narration_rev: int = 0
    # 자막용 텍스트(SBV) 별도 지정. None이면 narration_text를 그대로 사용.
    # 사용 예: TTS 발음 연속성을 위해 narration_text는 "에이치피"로 쓰고
    # 자막에는 "HP"로 표시하고 싶을 때 subtitle_text="HP가 줄어들어..." 처럼 분리.
    subtitle_text: Optional[str] = None
    footage: EffectConfig | None = None
    match: MatchMeta | None = None
    tts: TtsMeta | None = None
    text_overlays: list[TextOverlay] = Field(default_factory=list)
    footage_speed: float = 1.0
    # ── 재생 속도 모드 ───────────────────────────────────────────────────────
    # constant: 전 구간 footage_speed 배속(기존 동작, 기본값 → 기존 프로젝트 무회귀).
    # ramp: 처음 정속(1x) → 선형 가속 → footage_speed 도달 후 끝까지 유지.
    # hold/accel 은 원본 클립 구간 대비 비율(hold+accel<=1 로 클램프). 수학: src/utils/speed_ramp.py
    # ramp_down: 처음 footage_speed 로 빠르게 → 선형 감속 → 정속(1x) 으로 끝까지 (ramp 의 시간 대칭).
    footage_speed_mode: Literal["constant", "ramp", "ramp_down"] = "constant"
    footage_speed_ramp_hold: float = 0.2
    footage_speed_ramp_accel: float = 0.3
    # ramp/ramp_down 의 "기본 배속" — 가속의 시작 속도 / 감속의 끝 속도. 기본 1.0(=기존 동작).
    # 기존 배속 위에 구간 배속을 더 얹을 때 사용 (예: 1.5배속 씬의 앞부분만 4.5배속).
    footage_speed_ramp_base: float = 1.0
    # 씬 footage 원본 오디오 음량 — 0=음소거, 0.3=기본 TTS 더킹, 1.0=풀 볼륨.
    # 기존 하드코딩 0.3 더킹을 이 필드로 치환 → 기본값 유지 시 회귀 안전.
    footage_audio_volume: float = 0.3
    # ── TTS 재생 속도 (씬별 수동 조절) ────────────────────────────────────────
    # Google Chirp3-HD 목소리는 API `speakingRate` 를 무시하므로 TTS 재합성으로는
    # 속도 조절이 불가능하다. 대신 렌더/미리보기 시점에 각 segment WAV 에
    # `atempo` ffmpeg 필터를 적용해 후처리로 속도를 변경한다.
    # 값 규약: 1.0 = 원본 속도(기본), 2.0 = 2배 빠르게, 0.5 = 절반 속도.
    # atempo 안정 범위 밖(<0.5 또는 >2.0)은 build_merged_wav 가 자동 체이닝한다.
    # 회귀 안전: 필드가 없는 기존 프로젝트는 기본값 1.0 으로 로드되어 이전과 동일.
    tts_speed: float = 1.0
    # ── #tts-emotion — 씬별 TTS 감정/말투 지시문 ─────────────────────────────
    # Google Chirp3-HD 는 SynthesisInput.prompt 로 말투를 자연어 지시할 수 있다.
    # 예: "아주 신나고 들뜬 목소리로", "차분하고 낮게 깔린 목소리로".
    # 빈 문자열이면 지시 없이 기본 톤으로 합성한다(기존 동작).
    # 주의: 프롬프트 지원은 보이스/언어에 따라 다르며, 미지원이면 무시될 수 있다.
    tts_emotion: str = ""
    shape_overlays: list[ShapeOverlay] = Field(default_factory=list)
    # ── Graphic Overlay (#205) ──────────────────────────────────────────────
    # 빈 배열이 기본. 기존 씬 카드(graphic_overlays 미존재)와 호환.
    graphic_overlays: list[GraphicOverlay] = Field(default_factory=list)
    # ── Scene Filter (#206) ─────────────────────────────────────────────────
    # 씬 전체 색감/무드 필터. 빈 배열이 기본. 기존 씬 카드와 호환.
    # 적용 순서: cut_clip → filters → text_overlays → shape_overlays → graphic_overlays
    filters: list[SceneFilter] = Field(default_factory=list)
    # ── Scene Motion (#207) ─────────────────────────────────────────────────
    # 씬 내 화면 다이나믹 효과 (셰이크/clip 줌/슬로우 모션). 빈 배열이 기본.
    # 적용 순서: cut_clip → filters → motions → text_overlays → shape_overlays → graphic_overlays
    # 1차 구현: shake / clip_zoom 렌더링. slowmo는 데이터만(후속).
    motions: list[SceneMotion] = Field(default_factory=list)
    # ── Mosaic Overlay (#264) ───────────────────────────────────────────────
    # 영역 모자이크/블러 오버레이. 빈 배열이 기본. 기존 씬 카드(mosaic_overlays 미존재)와 호환.
    # 적용 순서: filters → motions → mosaic_overlays → image_overlays → text/shape/graphic overlays.
    mosaic_overlays: list[MosaicOverlay] = Field(default_factory=list)
    # ── Image Overlay ────────────────────────────────────────────────────────
    # footage 스튜디오 이미지 자산을 씬 위에 자유 위치·크기로 합성하는 오버레이.
    # 빈 배열이 기본. 기존 씬 카드(image_overlays 미존재)와 호환.
    # 적용 순서: filters → motions → mosaic_overlays → image_overlays → text/shape/graphic overlays.
    # image_overlays는 mosaic 위에 깔리지만 text/shape/graphic보다 *아래*에 위치하여,
    # 썸네일 위에 자막 텍스트나 도형 장식을 자연스럽게 얹을 수 있다.
    image_overlays: list[ImageOverlay] = Field(default_factory=list)
    # ── Speed Lines Overlay (집중선) ─────────────────────────────────────────
    # 만화 집중선 오버레이. 빈 배열이 기본. 기존 씬 카드(speed_lines 미존재)와 호환.
    # 적용 순서: 모든 오버레이 중 최상단(그래픽 오버레이 다음)에 얹힌다.
    speed_lines: list[SpeedLinesOverlay] = Field(default_factory=list)
    # ── Scene SFX — 씬 본체에 붙는 1개 SFX ────────────────────────────────────
    # 오버레이별 SFX와 동일한 풀(studio_assets audio + sfx_presets)에서 선택.
    # 트리거 시점 = 씬 영상 시작 + sfx_offset_sec.
    # 기존 데이터(필드 없음)는 sfx_asset_id=None으로 회귀 안전.
    sfx_asset_id: Optional[str] = None
    sfx_volume: float = 0.7
    sfx_offset_sec: float = 0.0
    # ── "삐 소리" 씬 단위 자동 효과 ────────────────────────────────────────────
    # True 면 이 씬의 "마지막 활성 TTS narration segment 가 끝나는 시점"에 비프음을
    # 겹쳐 넣는다 — 비프 끝이 마지막 TTS 끝과 맞물리므로, 비프 길이만큼 나레이션
    # 끝부분을 덮는다. 그 구간의 나레이션은 final_composer 가 음소거하므로 실제로
    # 말이 가려지는 검열음 연출이 된다(볼륨만 올리면 말소리가 그대로 남는다).
    # 비프 시작 시각 계산은 src.beep_effect.compute_beep_start_sec 가 담당하며,
    # 실제 비프음은 ffmpeg sine 소스로 렌더 시점에 생성되어 기존 SFX 트랙 경로로 믹싱된다.
    # 기존 데이터(필드 없음)는 False 로 회귀 안전.
    beep_on_narration_end: bool = False
    # 나레이션(1.0)과 같은 크기. 예전 0.7 은 말소리보다 3dB 작아 비프가 묻혔다.
    beep_volume: float = 1.0
    # ── "중간 이미지 삽입" 씬 마커 ──────────────────────────────────────────────
    # True 면 이 씬은 사용자가 "중간 이미지 삽입" 원클릭 액션으로 자동 생성한
    # 이미지 전용 삽입 씬이다(전체화면 이미지 오버레이 + 짧은 지속시간, narration 없음).
    # 프론트가 복잡한 오버레이 요소 대신 단순 "중간 이미지 씬" 요소로 취급하기 위한 표식.
    # 렌더 파이프라인은 일반 씬과 동일하게 처리한다(별도 렌더 경로 없음).
    # 기존 데이터(필드 없음)는 False 로 회귀 안전.
    is_insert_image_scene: bool = False
    # ── Scene Tag 시스템 (#208) ─────────────────────────────────────────────
    # 씬 카드 태그. 빈 배열이 기본. Phase 1 구현 범위는 필드 보존(직렬화)뿐이며,
    # 백엔드 측 효과 적용 로직은 후속 사이클에서 추가한다.
    tags: list[str] = Field(default_factory=list)
    # ── 씬-TTS N:1 매칭 (#382) ───────────────────────────────────────────────
    # 씬 1개에 여러 segment(TTS 구간)를 가진다. 시나리오 파서가 만든 첫 segment(seg_0)는
    # 씬 길이 자동 연장 정책이 유지되고, 사용자가 TimelineBar 클릭으로 추가한 segment는
    # 자동 연장 없이 offset_sec 기준으로 배치된다.
    #
    # 마이그레이션: state.load_project 단계에서 narration_segments 가 비어 있고
    # narration_text 가 있으면 seg_0 으로 자동 변환한다. 기존 코드 호환을 위해
    # narration_text / tts 필드는 그대로 보존하지만 새 코드는 narration_segments 만 참조한다.
    narration_segments: list[NarrationSegment] = Field(default_factory=list)

    # ── 편집 지시 코멘트 (씬 단위) ──────────────────────────────────────────
    # 빈 배열이 기본. 기존 씬 카드(comments 미존재)와 호환.
    # open 코멘트는 에이전트가 처리할 작업 목록, done 코멘트는 처리 이력이다.
    comments: list[SceneComment] = Field(default_factory=list)


# ── Project ────────────────────────────────────────────────────────────────

class ProjectStatus(str):
    SETUP = "setup"
    MATCHING = "matching"
    TTS_PENDING = "tts_pending"
    READY = "ready"
    EDITING = "editing"


# ── Footage Studio ─────────────────────────────────────────────────────────

class FootageMarker(BaseModel):
    """푸티지 타임라인의 특정 시각에 꽂는 편집 지시 코멘트 (구간 분할 화면)."""
    marker_id: str
    time_sec: float
    comment: str = ""
    created_at: str = ""


class FootageAsset(BaseModel):
    asset_id: str
    filename: str
    label: str = ""
    # ── #289/#290 — audio 타입 자산 (BGM/SFX 공용) 추가 ─────────────────────
    # 기존 video/image 자산과 동일한 풀에 통합 저장하되 asset_type 필드로 구분한다.
    # 분류 기준: 확장자 .mp3 .wav .m4a .ogg .aac .flac → audio.
    asset_type: Literal["video", "image", "audio"]
    source: Literal["recorded", "split", "presentation", "imported"]
    created_at: str = ""
    # ── 분할 출처 추적 ─────────────────────────────────────────────────────
    # split_from : 이 자산이 구간분할 조각이면 잘라낸 원본의 asset_id. 조각이 아니면 "".
    # has_splits : 이 자산을 원본으로 하여 조각이 만들어진 적이 있으면 True.
    # 둘 다 기본값이 있으므로 기존 state.json(필드 없음)도 그대로 로드된다.
    split_from: str = ""
    has_splits: bool = False
    # ── 지점 편집 코멘트 (구간 분할 화면) ──────────────────────────────────────
    # 영상의 특정 시각에 꽂는 편집 지시 메모. 씬 카드의 comments 와 달리 푸티지
    # 자체의 타임라인에 묶인다(예: "이 지점 앞 3초~뒤 5초 잘라서 '…' 대사 넣기").
    # 분할 저장 시 조각 범위 안의 마커는 조각 기준 시각으로 보정되어 함께 넘어간다.
    markers: list[FootageMarker] = Field(default_factory=list)
    # ── 푸티지 전체 코멘트 (구간 분할 화면) ────────────────────────────────────
    # markers 가 특정 시각에 묶인 메모라면, note 는 이 영상 전체에 대한 자유 메모
    # (내용 요약·쓸 곳·주의점 등). 기본값 "" 이라 기존 state.json 도 그대로 로드된다.
    note: str = ""


class SplitPoint(BaseModel):
    start: float
    end: float
    label: str = ""


class BgmTrack(BaseModel):
    """후처리 전용 BGM 트랙 (#289).

    렌더링이 끝난 final_output.mp4 위에 다중 트랙으로 합성된다. 트랙 간 겹침은 허용 (믹스).
    audio_asset_id는 studio_assets 내 asset_type="audio" 항목을 가리킨다.
    """
    id: str
    audio_asset_id: str
    start_sec: float = 0.0       # 영상 타임라인 기준 BGM 시작 시각
    end_sec: float = 0.0         # 영상 타임라인 기준 BGM 종료 시각 (>= start_sec)
    fade_in_sec: float = 0.5
    fade_out_sec: float = 0.5
    volume: float = 0.4          # 0.0~1.0 (기본 0.4 — TTS와 공존 권장)


# ── 다중 화자 ───────────────────────────────────────────────────────────────

class Speaker(BaseModel):
    """화자 1명 — 이름과 그 화자가 쓸 TTS 목소리.

    씬 태그에 `speaker:{name}` 을 달면 그 씬의 TTS 가 이 목소리로 생성된다.
    이름은 태그에서 찾는 키이므로 프로젝트 안에서 유일해야 한다(대소문자 무시).
    """
    name: str                          # 태그에 쓰는 화자명 (예: "홍길동")
    voice_name: str                    # TTS 목소리 id (예: "ko-KR-Chirp3-HD-Orus")
    provider: Optional[str] = None     # 미지정 시 프로젝트 기본 provider
    note: str = ""                     # 메모 (역할 설명 등). 렌더에 영향 없음.


class Project(BaseModel):
    # 옛 필드명(force_sibling_dissolve)을 별칭으로 받아 새 필드(force_split_scene_dissolve)에
    # 매핑한다. populate_by_name=True로 새 이름·옛 이름 양쪽 입력 모두 허용.
    model_config = ConfigDict(populate_by_name=True)

    project_id: str
    name: str
    # ── 수동 시리즈 그룹명 (홈 리스트 그룹핑용) ───────────────────────────────
    # 사용자가 자유 입력으로 지정. 빈 문자열(기본)이면 "시리즈 미편성"으로 취급한다.
    # 기존 프로젝트는 이 필드가 없어도 기본값 ""로 로드되어 회귀 안전.
    series: str = ""
    footage_dir: str = ""
    scenario_text: str = ""
    status: str = "setup"
    scenes: list[SceneCard] = Field(default_factory=list)
    tts_voice: str = "ko-KR-Chirp3-HD-Orus"
    # ── 다중 화자 사전 ──────────────────────────────────────────────────────
    # 씬 태그 `speaker:{name}` 이 여기 등록된 화자를 가리키면 그 목소리로 TTS 를 만든다.
    # 비어 있으면 기존과 동일하게 전부 tts_voice 를 쓴다(회귀 안전).
    speakers: list[Speaker] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    studio_assets: list[FootageAsset] = Field(default_factory=list)
    output_width: int = 1920
    output_height: int = 1080
    # ── 프로젝트 전역 배경색 (letterbox/pillarbox 여백) ──────────────────────
    # 프론트 컬러피커의 `#RRGGBB` 또는 named 값. 기본 "black" — 지정 안 하면
    # 기존 검정 여백과 100% 동일(회귀 안전). ffmpeg 전달 시 normalize_bg_color 로 변환.
    bg_color: str = "black"
    # ── 후처리 BGM 트랙 (#289) ──────────────────────────────────────────────
    # 렌더링 완료 후 BgmEditor 페이지에서 편집한다. 빈 배열이 기본 — 기존 프로젝트와 호환.
    bgm_tracks: list[BgmTrack] = Field(default_factory=list)
    # ── 모의 클라우드 비용 누적 (#301) ───────────────────────────────────────
    # 각 Phase 실행 시 cost_estimator.record_*가 누적 기록한다.
    # 형식: src.cost_estimator.CostBreakdown.to_dict() — 자세한 구조는 docs/cost-analysis.md.
    cost_estimate: dict = Field(default_factory=dict)
    # ── 스플릿 씬 디졸브 강제 적용 옵션 ────────────────────────────────────────
    # 같은 source_file을 공유하는 인접 씬(분할된 스플릿 씬)은 잔상 방지를 위해
    # 기본적으로 dissolve를 0초로 처리한다(clip_editor의 안전망). True면 스플릿 씬
    # 사이에도 일반 dissolve를 강제 적용해 의도적 잔상/전환 효과를 허용한다.
    # 기본값 False — 기존 프로젝트와 회귀 안전.
    # 옛 키(force_sibling_dissolve)도 alias로 받아주어 옛 state.json/IndexedDB 데이터와 호환.
    force_split_scene_dissolve: bool = Field(
        default=False,
        validation_alias=AliasChoices("force_split_scene_dissolve", "force_sibling_dissolve"),
        serialization_alias="force_split_scene_dissolve",
    )
    # ── #355 — 프로젝트 단위 Gemini 모델 선택 ───────────────────────────────
    # 자연어 편집(nl_editor) · 자동 씬 분할 · 자동 오버레이 검출 등 Gemini 호출에서
    # 사용할 모델 ID. pricing.json `api.gemini.models` 키와 일치해야 한다.
    # 빈 문자열이면 호출 시점에 pricing.json `default_model` 로 폴백한다
    # (기존 프로젝트 데이터와 회귀 안전).
    gemini_model: str = "gemini-2.5-flash"


# ── Request / Response bodies ──────────────────────────────────────────────

class CreateProjectRequest(BaseModel):
    name: str
    footage_dir: str = ""
    scenario_text: str = ""
    auto_footage_dir: bool = False  # True면 footage_dir을 web/data/{id}/footage/로 자동 설정


class ParseScenarioRequest(BaseModel):
    scenario_text: str
    force: bool = False


class IndexFootageRequest(BaseModel):
    force: bool = False


class MatchRequest(BaseModel):
    method: Literal["gemini_filename", "gemini_content", "keyword_filename", "manual", "label"]
    scene_ids: list[int] | None = None
    # #355 후속 — Gemini 매칭(gemini_filename / gemini_content) 시 사용할 모델 ID.
    # None 이면 라우터에서 프로젝트의 `gemini_model` 로 폴백, 그래도 없으면
    # pricing.json `default_model`. Gemini 호출 함수에 직접 전달되어 어떤 모델로
    # 추론할지 결정하며, `record_matching` 비용 누적의 by_model 분해에도 그대로 반영된다.
    model: str | None = None


class ManualMatchRequest(BaseModel):
    filename: str
    trim_start: float = 0.0
    trim_end: float | None = None


# ── #311 — 키워드(파일명) 매칭 클라이언트 처리 결과 저장용 ──────────────────
# 백엔드 `_match_keyword`를 TypeScript로 포팅한 결과를 저장하는 엔드포인트의 입력.
# 클라이언트가 계산한 결과만 받아 store.update_scene 으로 반영한다.

class KeywordMatchClientItem(BaseModel):
    scene_id: int
    # source_file은 footage 디렉토리 내 파일명(basename) 또는 절대경로 둘 다 허용.
    # 백엔드는 footage_dir에서 해당 파일을 찾을 수 있어야 하며, 절대경로면 그대로 사용한다.
    source_file: str
    filename: str
    chunk_id: str = ""
    trim_start: float = 0.0
    trim_end: float = 0.0
    match_score: float = 0.0
    is_image: bool = False


class SaveKeywordMatchRequest(BaseModel):
    """클라이언트에서 계산한 keyword_filename 매칭 결과 일괄 저장 요청.

    백엔드는 각 항목을 store.update_scene으로 반영하며 별도의 Gemini 호출이 없으므로
    비용 누적은 발생하지 않는다 (기존 _match_keyword 동작과 동일).
    """
    items: list[KeywordMatchClientItem]


class UpdateNarrationRequest(BaseModel):
    narration_text: str


# ── #382 — NarrationSegment API requests ────────────────────────────────
# 모든 요청 본문은 부분 업데이트(PATCH) 시 None=미지정으로 해석한다.

class CreateNarrationSegmentRequest(BaseModel):
    """씬에 신규 narration segment 추가.

    id 가 지정되면 그 id 를 사용하고(중복 시 400), 미지정이면 seg_{n} 형식으로 자동 부여.
    offset_sec/text/voice_name/sbv_text 는 옵셔널이며 미지정 시 기본값 사용.
    """
    id: Optional[str] = None
    text: str = ""
    offset_sec: float = 0.0
    voice_name: Optional[str] = None
    sbv_text: Optional[str] = None


class UpdateNarrationSegmentRequest(BaseModel):
    """기존 segment 부분 편집. None 필드는 변경하지 않는다."""
    text: Optional[str] = None
    offset_sec: Optional[float] = None
    voice_name: Optional[str] = None
    sbv_text: Optional[str] = None


class GenerateNarrationSegmentRequest(BaseModel):
    """특정 segment 의 TTS 생성/재생성. force=True 면 synced 상관없이 재호출."""
    force: bool = False
    voice_name: Optional[str] = None


class GenerateTtsRequest(BaseModel):
    scene_ids: list[int] | None = None
    force: bool = False
    voice_name: str | None = None  # None이면 프로젝트 tts_voice 사용

class SetTtsVoiceRequest(BaseModel):
    voice_name: str


class SetSpeakersRequest(BaseModel):
    """화자 사전 전체 교체 (PUT). 부분 수정 대신 통째로 보낸다."""
    speakers: list[Speaker] = Field(default_factory=list)


class RenderRequest(BaseModel):
    output_name: str | None = None
    # True면 자막(.sbv) 생성을 건너뛴다("영상만" 모드).
    skip_subtitle: bool = False
    # True면 최종 합성 때 비가시 워터마크(Video Seal)를 넣는다. 기본 끔(렌더 시간 증가).
    watermark: bool = False


class BgmRenderRequest(BaseModel):
    """후처리 BGM 렌더링 요청 (#289).

    Project.bgm_tracks와 final_output.mp4를 입력으로 받아 final_with_bgm.mp4를 생성한다.
    """
    # 명시적으로 트랙을 받지 않고 프로젝트 상태(Project.bgm_tracks)에서 읽는다.
    pass


# ── Template (#406/#407) ───────────────────────────────────────────────────
# 영상 구조(씬 배치 + 자막 + 오버레이 + BGM)를 원본 영상 없이 재사용 가능한 형태로 보존.
# 저장 위치: web/data/_templates/{template_id}/template.json
#
# 포함 범위:
#   - 씬 구조(scene_id 순서, duration_hint_sec, narration_text/segments, 자막)
#   - 오버레이/필터/모션/모자이크/이미지 오버레이
#   - BGM 트랙
#   - 프로젝트 메타(output_width/height, tts_voice, gemini_model 등)
#
# 제외 범위 (원본 영상/생성된 산출물):
#   - footage.source_file / clip_start_sec / clip_end_sec (영상 파일 의존 필드)
#   - narration WAV 파일, output/, cache/ 산출물
#   - footage_dir, studio_assets (원본 영상)
#   - match 메타 (footage 의존)
#
# 새 프로젝트 생성 시 (POST /api/projects/from-template):
#   - footage 필드는 None 으로 비워서 사용자가 씬별 업로드하게 함
#   - narration_text/segments 는 보존 → 사용자가 TTS generate 트리거
#   - tts/match 메타는 클리어 → 새 프로젝트에서 재생성


class TemplateMeta(BaseModel):
    """템플릿 목록(GET /api/templates) 응답용 메타 정보."""
    template_id: str
    title: str
    description: str = ""
    created_at: str = ""
    source_project_id: str = ""
    scene_count: int = 0


class TemplateBgmFile(BaseModel):
    """템플릿이 직접 들고 다니는 BGM 오디오 파일 1개 (#419 후속).

    파일 실체는 web/data/_templates/{template_id}/bgm/{filename} 에 있다.
    새 프로젝트 생성 시 이 파일을 footage 폴더로 복사해 studio_assets(audio)로 등록하고,
    bgm_tracks 의 audio_asset_id 를 새 asset_id 로 바꾼다.
    """
    filename: str                 # 템플릿 bgm/ 폴더 안의 파일명
    label: str = ""               # studio_asset label (원래 자산 라벨)
    original_filename: str = ""   # 원본 프로젝트 footage 폴더에서의 파일명 (참고용)


class Template(BaseModel):
    """템플릿 상세 모델. template.json 의 직렬화 스키마.

    scenes 의 각 SceneCard 는 footage=None / match=None / tts=None / narration_segments[*].audio_url=""
    로 정제되어 저장된다. 새 프로젝트 생성 시 그대로 복제하면 안전.
    """
    template_id: str
    title: str
    description: str = ""
    source_project_id: str = ""
    created_at: str = ""
    # 씬 구조 — footage/match/tts 가 클리어된 상태로 저장.
    scenes: list[SceneCard] = Field(default_factory=list)
    # 프로젝트 메타 (해상도/음성/모델/BGM)
    output_width: int = 1920
    output_height: int = 1080
    tts_voice: str = "ko-KR-Chirp3-HD-Orus"
    gemini_model: str = "gemini-2.5-flash"
    bgm_tracks: list[BgmTrack] = Field(default_factory=list)
    # bgm_tracks[*].audio_asset_id → 템플릿 bgm/ 폴더에 보관된 파일 정보.
    # 키가 없는 트랙은 기존처럼 audio_asset_id 를 그대로 보존한다.
    bgm_files: dict[str, TemplateBgmFile] = Field(default_factory=dict)
    force_split_scene_dissolve: bool = False


class CreateTemplateRequest(BaseModel):
    """프로젝트 → 템플릿 변환 요청 본문 (POST /api/templates)."""
    source_project_id: str
    title: str
    description: str = ""


class CreateProjectFromTemplateRequest(BaseModel):
    """템플릿에서 새 프로젝트 생성 요청 본문 (POST /api/projects/from-template)."""
    template_id: str
    title: str = ""


# ── Clip library ───────────────────────────────────────────────────────────
#
# 밈영상/로딩 애니메이션 등 여러 프로젝트에서 재사용하는 짧은 영상의 전역 라이브러리.
# 저장 위치는 web/data/_cliplib/{clip_id}/ (routers/clip_library.py).
#
# 씬 지정은 prefix 참조가 아니라 프로젝트 footage 폴더로의 하드링크(복사 폴백)로
# 이루어진다. 따라서 SceneCard.footage 에는 평범한 basename 만 들어가고,
# 렌더러/매처는 라이브러리의 존재 자체를 알 필요가 없다.


class ClipLibraryEntry(BaseModel):
    """라이브러리 클립 1개의 메타. clip.json 의 직렬화 스키마이자 API 응답 모델."""
    clip_id: str
    title: str
    category: str = ""
    # 클립 디렉토리 내 basename. 경로가 아니다.
    filename: str
    # 등록에 사용한 원본 URL — 재다운로드/출처 표시용.
    source_url: str = ""
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    created_at: str = ""


class ClipLibraryRegisterRequest(BaseModel):
    """URL → 클립 등록 요청 본문 (POST /api/clip-library)."""
    url: str
    title: str = ""
    category: str = ""


class SceneFromClipLibraryRequest(BaseModel):
    """라이브러리 클립을 씬에 지정하는 요청 본문."""
    clip_id: str


class SceneFromClipLibraryResponse(BaseModel):
    """씬 지정 결과.

    `filename` 은 충돌 회피가 적용된 **확정** 파일명이다. 프론트는 반드시 이 값으로
    footage 를 구성해야 한다 (라이브러리의 원본 filename 과 다를 수 있음).
    """
    filename: str
    duration_sec: float = 0.0
    # "link"(하드링크) 또는 "copy"(폴백) — 진단용.
    mode: str = "link"


# ── Footage item ───────────────────────────────────────────────────────────

class FootageChunk(BaseModel):
    chunk_id: str
    start_sec: float
    end_sec: float
    description: str = ""
    # 인덱싱된 청크의 검색 키워드(명사 ≤5). footage_index.json 의 keywords 를 그대로
    # 노출하여 자산 카드에 뱃지로 표시한다. 미인덱싱 청크는 빈 배열.
    keywords: list[str] = Field(default_factory=list)
    quality_score: float = 0.0


class FootageItem(BaseModel):
    filename: str
    path: str
    thumbnail_url: str
    duration_sec: float
    # #289/#290 — audio 파일 타입 추가. 클라이언트는 file_type으로 video/image/audio를 구분한다.
    file_type: Literal["video", "image", "audio"]
    chunks: list[FootageChunk] = Field(default_factory=list)


# ── WebSocket messages ─────────────────────────────────────────────────────

class ProgressMsg(BaseModel):
    type: Literal["progress"] = "progress"
    task_id: str
    phase: str
    current: int
    total: int
    message: str
    item: str = ""


class TtsDoneMsg(BaseModel):
    type: Literal["tts_done"] = "tts_done"
    task_id: str
    scene_id: int
    audio_url: str
    duration_sec: float


class MatchDoneMsg(BaseModel):
    type: Literal["match_done"] = "match_done"
    task_id: str
    scene_id: int
    matched_scene: SceneCard


class TaskCompleteMsg(BaseModel):
    type: Literal["complete"] = "complete"
    task_id: str
    summary: dict[str, int]


class ErrorMsg(BaseModel):
    type: Literal["error"] = "error"
    task_id: str
    code: str
    message: str
    scene_id: int | None = None
