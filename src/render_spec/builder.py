"""SceneCard → RenderSpec 변환기 (#302).

핵심 약속:
- `fit`(공간 변환)과 `footage_speed`(시간 변환)는 *완전히 독립된 분기*에서 계산한다.
  과거 회귀 버그(2026-05-27 d97386e 시리즈)의 패턴이었던 "speed 분기 안에서 fit이
  contain으로 강제 덮어쓰기"를 함수 구조로 차단한다.
- 변환 결과에 추상 키워드가 남지 않는다. cover/contain/stretch는 모두 `scale_x/scale_y +
  crop + pad` 픽셀로 풀어낸다.

공개 함수:
- `build_render_spec(scene, project, footage_size, footage_duration_sec) -> RenderSpec`
- `save_render_spec(project_id, specs, cache_dir=Path("cache")) -> Path`
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from src.render_spec.models import (
    Canvas,
    CropBox,
    FilterLayer,
    GraphicLayer,
    ImageLayer,
    Layer,
    MosaicLayer,
    MotionLayer,
    RenderSpec,
    ShapeLayer,
    Size,
    TextLayer,
    VideoLayer,
)


# #322 — 효과 카테고리 적용 순서(footage 위에 쌓는 순서)는 픽셀 합성 결과에 영향을 준다.
# 이 순서는 `src/clip_editor.py` 의 효과 적용 순서(cut_clip → filters → motions →
# mosaic → text/shape/graphic) 와 일치해야 한다. 핸들러 dict 의 등록 순서에 의존하지
# 않도록 본 모듈에서 *명시적*으로 선언한다.
_EFFECT_CATEGORY_ORDER: tuple[str, ...] = (
    "filter",
    "motion",
    "mosaic",
    "text",
    "shape",
    "graphic",
)


RENDER_SPEC_VERSION = 1

# 색상 이름 → hex 매핑 (가장 자주 쓰이는 것만). 이외는 #RRGGBB 형식이 아닐 경우 그대로
# 통과시킨다(다운스트림에서 추가 검증할 수 있도록).
_NAMED_COLORS: dict[str, str] = {
    "black": "#000000",
    "white": "#FFFFFF",
    "red": "#FF0000",
    "green": "#008000",
    "blue": "#0000FF",
    "yellow": "#FFFF00",
    "cyan": "#00FFFF",
    "magenta": "#FF00FF",
    "gray": "#808080",
    "grey": "#808080",
}

_HEX3_RE = re.compile(r"^#([0-9a-fA-F]{3})$")
_HEX6_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
_RGB_RE = re.compile(r"^rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$")


def _normalize_color(value: Optional[str], default: str = "#FFFFFF") -> str:
    """다양한 색 표기를 `#RRGGBB`(대문자)로 통일한다."""
    if not value:
        return default
    s = value.strip()
    if not s:
        return default
    low = s.lower()
    if low in _NAMED_COLORS:
        return _NAMED_COLORS[low]
    m = _HEX6_RE.match(s)
    if m:
        return "#" + m.group(1).upper()
    m = _HEX3_RE.match(s)
    if m:
        # #abc → #aabbcc
        a, b, c = m.group(1)
        return f"#{a}{a}{b}{b}{c}{c}".upper()
    m = _RGB_RE.match(s)
    if m:
        r, g, b = (max(0, min(255, int(x))) for x in m.groups())
        return f"#{r:02X}{g:02X}{b:02X}"
    # 알 수 없는 표기 → 그대로 두되 다운스트림에서 처리하도록 디폴트로 폴백
    return default


def _ratio_to_px(ratio: float, span: int) -> int:
    """0~1 비율을 캔버스 길이(px)로 환산. 음수/1초과도 허용(오프셋 용도)."""
    return int(round(float(ratio) * span))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── 공간 변환: fit → (scale_x, scale_y, crop, pad) ─────────────────────────

def _compute_fit_geometry(
    canvas: Canvas,
    source_size: Size,
    fit: str,
) -> tuple[float, float, Optional[CropBox], Optional[CropBox]]:
    """fit 키워드를 (scale_x, scale_y, crop, pad) 절대 픽셀로 환산한다.

    반환 의미:
    - `scale_x/scale_y`: source_size에 곱했을 때 얻고 싶은 *스케일된 가시 영역* 크기.
    - `crop`: 스케일 결과(픽셀 좌표계)에서 잘라낼 영역. None이면 자르지 않음.
    - `pad`: 캔버스 좌표계 내부에서 스케일 결과를 그릴 영역. None이면 캔버스 가득.

    contain: scale로 캔버스 안에 통째로 들어가게 줄이고, 남는 영역은 pad.
    cover  : scale로 캔버스를 완전히 덮을 만큼 키우고, 넘치는 영역은 crop.
    stretch: x/y를 각각 캔버스 크기에 맞게 늘림(비율 무시). crop/pad 없음.
    """
    cw, ch = canvas.width, canvas.height
    sw, sh = source_size.width, source_size.height
    fit_norm = fit if fit in ("contain", "cover", "stretch") else "contain"

    if fit_norm == "stretch":
        return (cw / sw, ch / sh, None, None)

    if fit_norm == "cover":
        # source의 어느 축이든 캔버스를 *덮어야* 하므로 더 큰 스케일을 선택.
        s = max(cw / sw, ch / sh)
        scaled_w = sw * s
        scaled_h = sh * s
        # 가운데 정렬로 crop. 픽셀 단위에서 안전하게 반올림.
        crop_x = max(0, int(round((scaled_w - cw) / 2)))
        crop_y = max(0, int(round((scaled_h - ch) / 2)))
        crop = CropBox(x=crop_x, y=crop_y, w=cw, h=ch)
        return (s, s, crop, None)

    # contain (기본)
    s = min(cw / sw, ch / sh)
    scaled_w = sw * s
    scaled_h = sh * s
    pad_x = max(0, int(round((cw - scaled_w) / 2)))
    pad_y = max(0, int(round((ch - scaled_h) / 2)))
    # contain은 스케일 결과를 그대로 그리고 캔버스 안에서 가운데 배치.
    pad = CropBox(
        x=pad_x,
        y=pad_y,
        w=max(1, int(round(scaled_w))),
        h=max(1, int(round(scaled_h))),
    )
    return (s, s, None, pad)


# ── 회전 보정 ──────────────────────────────────────────────────────────────

def _apply_rotation_to_size(size: Size, rotation_deg: int) -> Size:
    """수동 회전(0/90/180/270)이 적용된 후의 가시 영역 해상도를 반환."""
    if rotation_deg in (90, 270):
        return Size(width=size.height, height=size.width)
    return size


# ── 오버레이 변환 ──────────────────────────────────────────────────────────

def _scene_default_end(duration_sec: float, value: Optional[float]) -> Optional[float]:
    """오버레이의 end_sec이 None이면 씬 길이로 자동 연장한다."""
    if value is None:
        return duration_sec
    return float(value)


def _build_text_layer(ov: Any, canvas: Canvas, duration_sec: float) -> TextLayer:
    return TextLayer(
        text=str(getattr(ov, "text", "") or ""),
        position_x=_ratio_to_px(_safe_float(getattr(ov, "x", 0.5), 0.5), canvas.width),
        position_y=_ratio_to_px(_safe_float(getattr(ov, "y", 0.1), 0.1), canvas.height),
        font_size_px=max(1, _safe_int(getattr(ov, "font_size", 40), 40)),
        font_family=str(getattr(ov, "font_family", "NotoSansKR") or "NotoSansKR"),
        color=_normalize_color(getattr(ov, "color", None), "#FFFFFF"),
        align=getattr(ov, "align", "center") if getattr(ov, "align", "center") in ("left", "center", "right") else "center",
        bold=bool(getattr(ov, "bold", False)),
        italic=bool(getattr(ov, "italic", False)),
        outline_color=_normalize_color(getattr(ov, "outline_color", None), "#000000"),
        outline_width_px=max(0, _safe_int(getattr(ov, "outline_width", 0), 0)),
        opacity=max(0.0, min(1.0, _safe_float(getattr(ov, "opacity", 1.0), 1.0))),
        start_sec=max(0.0, _safe_float(getattr(ov, "start_sec", 0.0), 0.0)),
        end_sec=_scene_default_end(duration_sec, getattr(ov, "end_sec", None)),
        enter_effect=getattr(ov, "enter_effect", "fade"),
        exit_effect=getattr(ov, "exit_effect", "fade"),
        enter_duration_sec=max(0.0, _safe_float(getattr(ov, "enter_duration_sec", 0.3), 0.3)),
        exit_duration_sec=max(0.0, _safe_float(getattr(ov, "exit_duration_sec", 0.3), 0.3)),
        loop_animation=(
            getattr(ov, "loop_animation", "none")
            if getattr(ov, "loop_animation", "none") in ("none", "bounce", "shake", "pulse", "wave")
            else "none"
        ),
        loop_amplitude=max(0.0, _safe_float(getattr(ov, "loop_amplitude", 0.02), 0.02)),
        loop_period_sec=max(0.1, _safe_float(getattr(ov, "loop_period_sec", 0.8), 0.8)),
    )


def _build_shape_layer(ov: Any, canvas: Canvas, duration_sec: float) -> ShapeLayer:
    return ShapeLayer(
        shape=getattr(ov, "shape", "rect") if getattr(ov, "shape", "rect") in ("rect", "ellipse") else "rect",
        x=_ratio_to_px(_safe_float(getattr(ov, "x", 0.0), 0.0), canvas.width),
        y=_ratio_to_px(_safe_float(getattr(ov, "y", 0.0), 0.0), canvas.height),
        width=max(1, _ratio_to_px(_safe_float(getattr(ov, "width", 0.1), 0.1), canvas.width)),
        height=max(1, _ratio_to_px(_safe_float(getattr(ov, "height", 0.1), 0.1), canvas.height)),
        color=_normalize_color(getattr(ov, "color", None), "#FFFFFF"),
        opacity=max(0.0, min(1.0, _safe_float(getattr(ov, "opacity", 1.0), 1.0))),
        start_sec=max(0.0, _safe_float(getattr(ov, "start_sec", 0.0), 0.0)),
        end_sec=_scene_default_end(duration_sec, getattr(ov, "end_sec", None)),
        enter_effect=getattr(ov, "enter_effect", "fade") if getattr(ov, "enter_effect", "fade") in ("none", "fade") else "fade",
        exit_effect=getattr(ov, "exit_effect", "fade") if getattr(ov, "exit_effect", "fade") in ("none", "fade") else "fade",
        enter_duration_sec=max(0.0, _safe_float(getattr(ov, "enter_duration_sec", 0.3), 0.3)),
        exit_duration_sec=max(0.0, _safe_float(getattr(ov, "exit_duration_sec", 0.3), 0.3)),
    )


def _build_graphic_layer(ov: Any, canvas: Canvas, duration_sec: float) -> GraphicLayer:
    return GraphicLayer(
        graphic_type=getattr(ov, "type", "spotlight"),
        center_x=_ratio_to_px(_safe_float(getattr(ov, "x", 0.5), 0.5), canvas.width),
        center_y=_ratio_to_px(_safe_float(getattr(ov, "y", 0.5), 0.5), canvas.height),
        params=dict(getattr(ov, "params", {}) or {}),
        opacity=max(0.0, min(1.0, _safe_float(getattr(ov, "opacity", 1.0), 1.0))),
        start_sec=max(0.0, _safe_float(getattr(ov, "start_sec", 0.0), 0.0)),
        end_sec=_scene_default_end(duration_sec, getattr(ov, "end_sec", None)),
    )


_FILTER_TYPES_VALID = ("flashback", "horror", "vivid", "nightvision", "pov", "warm", "cool", "cartoon")
_MOTION_TYPES_VALID = ("shake", "slowmo", "clip_zoom", "stutter")


def _build_filter_layer(ov: Any, duration_sec: float) -> Optional[FilterLayer]:
    """SceneFilter → FilterLayer 변환 (#343).

    알 수 없는 ``filter_type`` 은 None 으로 스킵 — RenderSpec 에 정의되지 않은 키워드를
    무리하게 layers 에 끼워 넣어 다운스트림 매퍼를 혼란시키지 않기 위함.
    """
    ftype = getattr(ov, "filter_type", None)
    if ftype not in _FILTER_TYPES_VALID:
        return None
    return FilterLayer(
        filter_type=ftype,
        intensity=max(0.0, min(1.0, _safe_float(getattr(ov, "intensity", 1.0), 1.0))),
        params=dict(getattr(ov, "params", {}) or {}),
        start_sec=0.0,
        end_sec=duration_sec,
        opacity=1.0,
    )


def _build_motion_layer(ov: Any, duration_sec: float) -> Optional[MotionLayer]:
    """SceneMotion → MotionLayer 변환 (#343).

    빈 motion_type / 미지원 타입은 None 으로 스킵.
    """
    mtype = getattr(ov, "motion_type", None)
    if mtype not in _MOTION_TYPES_VALID:
        return None
    motion_start = max(0.0, _safe_float(getattr(ov, "start_sec", 0.0), 0.0))
    motion_dur = _safe_float(getattr(ov, "duration_sec", 1.0), 1.0)
    if motion_dur <= 0:
        motion_dur = 1.0
    return MotionLayer(
        motion_type=mtype,
        motion_start_sec=motion_start,
        motion_duration_sec=motion_dur,
        params=dict(getattr(ov, "params", {}) or {}),
        start_sec=0.0,
        end_sec=duration_sec,
        opacity=1.0,
    )


def _build_mosaic_layer(ov: Any, canvas: Canvas, duration_sec: float) -> MosaicLayer:
    return MosaicLayer(
        shape=getattr(ov, "shape", "rect") if getattr(ov, "shape", "rect") in ("rect", "ellipse") else "rect",
        x=_ratio_to_px(_safe_float(getattr(ov, "x", 0.4), 0.4), canvas.width),
        y=_ratio_to_px(_safe_float(getattr(ov, "y", 0.4), 0.4), canvas.height),
        width=max(1, _ratio_to_px(_safe_float(getattr(ov, "width", 0.2), 0.2), canvas.width)),
        height=max(1, _ratio_to_px(_safe_float(getattr(ov, "height", 0.2), 0.2), canvas.height)),
        mode=getattr(ov, "mode", "pixelate") if getattr(ov, "mode", "pixelate") in ("pixelate", "blur") else "pixelate",
        pixel_size=max(1, _safe_int(getattr(ov, "pixel_size", 20), 20)),
        blur_radius=max(0, _safe_int(getattr(ov, "blur_radius", 20), 20)),
        start_sec=max(0.0, _safe_float(getattr(ov, "start_sec", 0.0), 0.0)),
        end_sec=_scene_default_end(duration_sec, getattr(ov, "end_sec", None)),
    )


# ── 핵심: footage(영상/이미지) 레이어 빌더 ────────────────────────────────

def _build_footage_layer(
    scene: Any,
    canvas: Canvas,
    footage_size: Size,
    footage_duration_sec: float,
) -> Optional[Layer]:
    """씬의 footage(EffectConfig)를 VideoLayer 또는 ImageLayer로 환산.

    중요: 이 함수는 **fit과 playback_rate를 한 번도 같은 표현식 안에서 다루지 않는다**.
    먼저 fit으로 공간 geometry(scale/crop/pad)를 결정한 뒤, playback_rate는 별도로 대입한다.
    """
    footage = getattr(scene, "footage", None)
    if footage is None:
        return None

    footage_type = getattr(footage, "type", None)

    # 1) footage_speed → playback_rate (공간 계산과 무관)
    raw_speed = getattr(scene, "footage_speed", 1.0)
    playback_rate = _safe_float(raw_speed, 1.0)
    if playback_rate <= 0:
        playback_rate = 1.0

    # 2) 원본 해상도에 회전 적용 → 공간 계산용 가시 해상도
    raw_rotation = _safe_int(getattr(footage, "rotation", 0), 0)
    rotation = raw_rotation if raw_rotation in (0, 90, 180, 270) else 0
    visible_source = _apply_rotation_to_size(footage_size, rotation)

    # 3) fit → (scale, crop, pad) — playback_rate를 *전혀 참조하지 않는다*
    fit_raw = getattr(footage, "fit", None)
    # 이미지(kenburns)의 디폴트 fit은 "cover", clip의 디폴트는 "contain"
    default_fit = "cover" if footage_type in ("kenburns", "kenburns_copy_img") else "contain"
    fit = fit_raw if fit_raw in ("contain", "cover", "stretch") else default_fit
    scale_x, scale_y, crop, pad = _compute_fit_geometry(canvas, visible_source, fit)

    if footage_type in ("kenburns", "kenburns_copy_img"):
        image_source = getattr(footage, "image", "") or ""
        return ImageLayer(
            source=str(image_source),
            source_size=visible_source,
            scale_x=scale_x,
            scale_y=scale_y,
            crop=crop,
            pad=pad,
            zoom_from=1.0,
            zoom_to=1.15,
            start_sec=0.0,
            end_sec=footage_duration_sec,
        )

    # clip(영상) 외 타입은 1차 범위에서 VideoLayer 매핑이 의미 없으므로 None 반환.
    # (edit/sync/multiscreen/title_gen 등은 후속 이슈에서 별도 빌더 추가)
    if footage_type != "clip":
        return None

    source = getattr(footage, "file", "") or ""
    from src.utils.speed_ramp import scene_speed_params, scene_speed_base
    _sp_mode, _sp_s, _sp_hold, _sp_accel = scene_speed_params(scene)
    _sp_base = scene_speed_base(scene)
    return VideoLayer(
        source=str(source),
        source_size=visible_source,
        scale_x=scale_x,
        scale_y=scale_y,
        crop=crop,
        pad=pad,
        playback_rate=playback_rate,
        speed_mode=_sp_mode,
        speed_ramp_hold=_sp_hold,
        speed_ramp_accel=_sp_accel,
        speed_ramp_base=_sp_base,
        rotation_deg=rotation,  # ffmpeg 매퍼가 transpose로 처리하도록 그대로 전달
        start_sec=0.0,
        end_sec=footage_duration_sec,
    )


# ── 공개 API ──────────────────────────────────────────────────────────────

def build_render_spec(
    scene: Any,
    project: Any,
    footage_size: Optional[Size] = None,
    footage_duration_sec: Optional[float] = None,
    fps: int = 30,
) -> RenderSpec:
    """SceneCard 1개를 RenderSpec으로 변환한다.

    인자:
    - `scene`: `SceneCard` (또는 동일 구조 dict-like 객체)
    - `project`: `Project` (output_width/output_height 사용)
    - `footage_size`: 원본 footage 해상도. 호출자가 미리 ffprobe로 측정해 넘긴다.
      None이면 기본 1920×1080(가정) — 단위 테스트 외에는 None 호출 금지.
    - `footage_duration_sec`: 원본 footage 길이(초). 영상 길이는 별도 분기에서 다루므로
      여기서는 VideoLayer.end_sec 기본값으로만 사용. 이미지면 임의의 양수 사용.
    - `fps`: 캔버스 fps. 기본 30.

    반환: 씬 1개에 대한 `RenderSpec`.
    """
    cw = _safe_int(getattr(project, "output_width", 1920), 1920) or 1920
    ch = _safe_int(getattr(project, "output_height", 1080), 1080) or 1080
    # 프로젝트 전역 배경색(letterbox 여백). ffmpeg pad 토큰으로 정규화.
    # 기본 "black" — 미지정 시 기존과 동일.
    try:
        from src.utils.ffmpeg_utils import normalize_bg_color as _normalize_bg_color
        _bg = _normalize_bg_color(getattr(project, "bg_color", "black"))
    except Exception:
        _bg = "black"
    canvas = Canvas(width=cw, height=ch, fps=fps, bg_color=_bg)

    if footage_size is None:
        footage_size = Size(width=1920, height=1080)
    if footage_duration_sec is None or footage_duration_sec <= 0:
        # duration_hint_sec을 폴백으로 사용. 그것도 없으면 5.0.
        footage_duration_sec = max(0.1, _safe_float(getattr(scene, "duration_hint_sec", 5.0), 5.0))

    # 씬 전체 길이: 일단 duration_hint_sec(또는 footage 길이) 우선. RenderSpec 사용자는
    # 이후 narration_timeline에서 갱신된 값으로 재생성하는 흐름을 권장.
    raw_speed = _safe_float(getattr(scene, "footage_speed", 1.0), 1.0)
    if raw_speed <= 0:
        raw_speed = 1.0
    # 씬 길이는 footage 시간 길이 / playback_rate 로 잡되, 최소 duration_hint_sec 보장.
    # ramp 모드는 출력 길이 공식(src/utils/speed_ramp)으로 계산.
    from src.utils.speed_ramp import scene_output_duration
    base_duration = scene_output_duration(footage_duration_sec, scene)
    duration_sec = max(
        base_duration,
        max(0.1, _safe_float(getattr(scene, "duration_hint_sec", 0.0), 0.0)),
    )

    layers: list[Layer] = []

    # 1) footage 레이어 (가장 아래) — 이 단계는 카테고리 외부의 *기반 레이어*이므로
    # 효과 핸들러 레지스트리와 별개로 별도 빌더가 처리한다.
    footage_layer = _build_footage_layer(scene, canvas, footage_size, duration_sec)
    if footage_layer is not None:
        layers.append(footage_layer)

    # 2) 효과 카테고리 (#322) — 카테고리별 if 분기 대신 핸들러 레지스트리 일괄 호출.
    # 적용 순서는 ``_EFFECT_CATEGORY_ORDER`` 가 결정한다(clip_editor 의 실행 순서와 일치:
    # filters → motions → mosaic → text → shape → graphic). 신규 효과/카테고리는 매니페스트
    # 파일에서 핸들러를 등록하면 자동으로 본 루프에 합류한다.
    #
    # import 를 함수 내부에서 수행하는 이유: 매니페스트 파일들이 builder.py 의 ``_build_*_layer``
    # 헬퍼를 호출하기 위해 builder 를 import 하므로, 모듈 로딩 시점에 effects 패키지를 import 하면
    # 순환 import 가 발생한다. 함수 호출 시점에는 양쪽 모듈이 모두 로딩 완료 상태가 보장된다.
    from src.render_spec.effects._handlers import get_render_spec_handler

    for category in _EFFECT_CATEGORY_ORDER:
        handler = get_render_spec_handler(category)
        if handler is None:
            # 미등록 카테고리는 조용히 스킵 — 회귀 안전(빈 layers 와 동일).
            continue
        for layer in handler(scene, canvas, duration_sec):
            layers.append(layer)

    return RenderSpec(
        version=RENDER_SPEC_VERSION,
        scene_id=_safe_int(getattr(scene, "scene_id", 0), 0),
        canvas=canvas,
        duration_sec=duration_sec,
        layers=layers,
    )


def save_render_spec(
    project_id: str,
    specs: list[RenderSpec],
    cache_dir: Path | str = Path("cache"),
) -> Path:
    """씬별 RenderSpec 리스트를 `cache/render_spec.json`으로 저장한다.

    박테스터 감사용 산출물. 같은 캐시 디렉토리에 프로젝트별로 한 파일만 유지한다
    (`cache/render_spec.json`). 호출자가 프로젝트 디렉토리 단위 캐시 폴더(예:
    `web/data/{project_id}/cache/`)를 넘길 수도 있도록 `cache_dir`를 인자로 받는다.

    반환: 저장된 파일 경로.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / "render_spec.json"
    payload = {
        "version": RENDER_SPEC_VERSION,
        "project_id": project_id,
        "scenes": [json.loads(s.model_dump_json()) for s in specs],
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path
