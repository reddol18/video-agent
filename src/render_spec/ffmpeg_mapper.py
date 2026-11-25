"""RenderSpec → ffmpeg 파라미터 순수 매퍼 (#303).

설계 원칙
---------
1. **계산 금지** — 이 모듈은 *해석/계산*을 하지 않는다. RenderSpec에 이미 절대 픽셀/배율로
   풀어져 있는 값을 그대로 ffmpeg 필터 문자열에 옮긴다. cover/contain/stretch 같은 추상
   키워드를 보는 분기 자체가 없으며, fit-vs-speed 같은 결합도 만들 수 없는 구조다.
2. **공간(VideoLayer) 매핑이 본 이슈 범위** — Phase 1.
   - VideoLayer는 `scale_x/scale_y + crop + pad + rotation_deg + playback_rate`를 보고
     ffmpeg `vf` 체인 한 줄(또는 vf+af 한 쌍)을 만든다.
   - 그 외 레이어(text/shape/graphic/mosaic)는 회귀 방지를 위해 *고수준 헬퍼*를
     기존 `src/clip_editor.py` 함수에 그대로 위임하는 진입점만 제공한다.
   - ImageLayer(Ken Burns)는 별도 분기로 매핑 — RenderSpec 매퍼는 zoom_from/zoom_to만
     읽고 zoompan 표현식을 만든다.
3. **함수는 모두 "값 → 문자열"** — 사이드이펙트 없음. 같은 입력에 항상 같은 출력.
   회귀 테스트가 문자열 동등성만으로 검증할 수 있도록.
4. **출력 인터페이스 2종**
   - `video_layer_to_vf(layer, canvas) -> str` — VideoLayer 1개에 대한 vf 체인 한 줄.
   - `video_layer_to_atempo(layer) -> str` — playback_rate에 대응하는 atempo 체인.
   - `image_layer_to_kenburns_vf(layer, canvas, total_frames, fps) -> str` — ImageLayer 1개.
   - `spec_to_video_ffmpeg_args(spec, src_input_label="0:v") -> list[str]` — 전체 spec을
     단일 vf 인자(`["-vf", "..."]`)로 묶어 반환.

본 모듈은 RenderSpec.layers 중 *맨 아래 footage 레이어 1개*만 ffmpeg 명령으로 매핑한다.
오버레이(text/shape/graphic/mosaic)는 별도 패스에서 PNG 오버레이로 합성되므로 vf chain
한 줄로 표현할 수 없는 별도 단계다 — 이 부분은 기존 헬퍼를 그대로 사용한다.
"""

from __future__ import annotations

from typing import Optional

from src.render_spec.models import (
    Canvas,
    CropBox,
    ImageLayer,
    Layer,
    RenderSpec,
    Size,
    VideoLayer,
)


# ── 회전 ──────────────────────────────────────────────────────────────────

def _rotate_filter(rotation_deg: int) -> str:
    """0/90/180/270 회전을 ffmpeg 필터로 변환. 그 외 값은 빈 문자열."""
    if rotation_deg == 90:
        return "transpose=1"
    if rotation_deg == 180:
        return "hflip,vflip"
    if rotation_deg == 270:
        return "transpose=2"
    return ""


# ── 공간 매핑 ──────────────────────────────────────────────────────────────

def _scale_filter(layer: VideoLayer | ImageLayer) -> str:
    """source_size × (scale_x, scale_y) → 절대 픽셀 scale 명령.

    RenderSpec.builder에서 이미 cover/contain/stretch가 모두 (scale_x, scale_y)로
    풀어진 상태로 들어온다. 매퍼는 그 결과 픽셀 크기만 ffmpeg에 옮긴다.
    """
    target_w = max(1, int(round(layer.source_size.width * layer.scale_x)))
    target_h = max(1, int(round(layer.source_size.height * layer.scale_y)))
    return f"scale={target_w}:{target_h}"


def _crop_filter(crop: Optional[CropBox]) -> str:
    """cover에서 사용. scale 결과에서 (x, y) 시작점 + (w, h) 영역으로 잘라낸다."""
    if crop is None:
        return ""
    return f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y}"


def _pad_filter(pad: Optional[CropBox], canvas: Canvas) -> str:
    """contain에서 사용. 캔버스 안에 scale 결과를 그릴 자리(pad.x/y/w/h)만큼 letterbox.

    `pad` 박스는 RenderSpec.builder에서 이미 캔버스 좌표계 기준으로 계산되어 들어옴.
    여기서는 그 값을 `pad={canvas_w}:{canvas_h}:{pad.x}:{pad.y}:black` 으로 직역만 한다.
    """
    if pad is None:
        return ""
    return f"pad={canvas.width}:{canvas.height}:{pad.x}:{pad.y}:{getattr(canvas, 'bg_color', None) or 'black'}"


def _setsar_and_fps(fps: int) -> str:
    """fps 고정 + 픽셀 종횡비 1:1. 모든 영상 매핑의 마지막에 붙는다."""
    return f"setsar=1,fps={fps}"


def video_layer_to_vf(layer: VideoLayer, canvas: Canvas) -> str:
    """VideoLayer 1개에 대한 ffmpeg `-vf` 체인 문자열.

    체인 순서(고정): rotation → scale → crop → pad → setsar/fps → setpts.

    - rotation: source_size는 회전 후 가시 해상도 기준이지만, 실제 ffmpeg 입력 픽셀은
      회전 전이므로 transpose/hflip+vflip을 *맨 앞*에 둔다.
    - scale: source_size에 scale_x/scale_y를 곱한 절대 픽셀로 스케일.
    - crop: cover일 때만 존재 (pad=None). scale 결과에서 잘라낸다.
    - pad: contain일 때만 존재 (crop=None). 캔버스 좌표계에 letterbox.
    - setsar/fps: 픽셀 종횡비 1:1, fps 고정.
    - setpts: playback_rate가 1.0이 아니면 `setpts=(1/rate)*PTS`로 시간축 변환.
    """
    parts: list[str] = []
    r = _rotate_filter(layer.rotation_deg)
    if r:
        parts.append(r)
    parts.append(_scale_filter(layer))
    c = _crop_filter(layer.crop)
    if c:
        parts.append(c)
    p = _pad_filter(layer.pad, canvas)
    if p:
        parts.append(p)
    parts.append(_setsar_and_fps(canvas.fps))
    if layer.playback_rate != 1.0:
        # 원본 길이(end_sec)를 모르면 가속 구간을 잡을 수 없으므로 정속 배속으로 폴백.
        if layer.speed_mode in ("ramp", "ramp_down") and layer.end_sec is not None:
            from src.utils.speed_ramp import ramp_setpts_expr
            _L = _layer_source_len(layer)
            parts.append(ramp_setpts_expr(
                _L, float(layer.playback_rate), layer.speed_ramp_hold, layer.speed_ramp_accel,
                down=(layer.speed_mode == "ramp_down"), base=layer.speed_ramp_base,
            ))
        else:
            pts_factor = 1.0 / layer.playback_rate
            parts.append(f"setpts={pts_factor:.6f}*PTS")
    return ",".join(parts)


def _layer_source_len(layer: VideoLayer) -> float:
    """ramp 계산용 원본 길이 L = end_sec - start_sec (없으면 1.0)."""
    if layer.end_sec is None:
        return 1.0
    return max(0.1, float(layer.end_sec) - float(layer.start_sec))


def video_layer_to_atempo(layer: VideoLayer) -> str:
    """playback_rate에 대응하는 ffmpeg `-af` 체인.

    atempo는 0.5~2.0 범위만 단일 적용이 가능하다. 그 밖은 체이닝.
    rate==1.0이면 빈 문자열을 반환(호출자가 `-af` 생략).
    """
    rate = float(layer.playback_rate)
    if rate == 1.0:
        return ""
    if layer.speed_mode in ("ramp", "ramp_down") and layer.end_sec is not None:
        from src.utils.speed_ramp import ramp_audio_filter
        return ramp_audio_filter(
            _layer_source_len(layer), rate, layer.speed_ramp_hold, layer.speed_ramp_accel,
            down=(layer.speed_mode == "ramp_down"), base=layer.speed_ramp_base,
        )
    if 0.5 <= rate <= 2.0:
        return f"atempo={rate:.6f}"
    if rate > 2.0:
        return f"atempo=2.0,atempo={rate / 2.0:.6f}"
    # rate < 0.5
    return f"atempo=0.5,atempo={rate / 0.5:.6f}"


# ── Ken Burns(이미지) 매핑 ────────────────────────────────────────────────

def image_layer_to_kenburns_vf(
    layer: ImageLayer,
    canvas: Canvas,
    total_frames: int,
    fps: int,
    upscale_factor: int = 4,
) -> str:
    """ImageLayer 1개에 대한 zoompan 기반 Ken Burns vf 체인.

    기존 `_make_scene_preview`의 zoompan 식과 동일한 출력을 만들도록 작성. 이번 이슈에서
    Ken Burns 자체는 범위 밖이므로 *식 형태*는 기존과 1:1로 유지하고, RenderSpec의
    `zoom_from/zoom_to`만 받아서 식에 끼워 넣는다.

    체인: rotation → scale → crop → pad → upscale → zoompan(zoom_from→zoom_to).
    """
    parts: list[str] = []
    r = _rotate_filter(0)  # ImageLayer는 rotation 필드가 없음
    if r:
        parts.append(r)
    parts.append(_scale_filter(layer))
    c = _crop_filter(layer.crop)
    if c:
        parts.append(c)
    p = _pad_filter(layer.pad, canvas)
    if p:
        parts.append(p)
    # zoompan jitter 완화용 업스케일 (기존 동작과 동일)
    parts.append(f"scale={canvas.width * upscale_factor}:{canvas.height * upscale_factor}")
    # 선형 보간 zoom (zoom_from → zoom_to)
    span = max(1, total_frames - 1)
    delta = layer.zoom_to - layer.zoom_from
    zp_z = f"{layer.zoom_from:.6f}+{delta:.6f}*on/{span}"
    parts.append(
        f"zoompan=z='{zp_z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={canvas.width}x{canvas.height}:fps={fps}"
    )
    return ",".join(parts)


# ── 통합: 1개 footage 레이어 → ffmpeg argv 파편 ───────────────────────────

def find_footage_layer(spec: RenderSpec) -> Optional[Layer]:
    """RenderSpec에서 footage(VideoLayer 또는 ImageLayer)를 찾는다.

    빌더가 footage 레이어를 맨 앞에 넣으므로 보통 layers[0]이지만, 안전하게 type으로
    탐색한다. 없으면 None.
    """
    for layer in spec.layers:
        if isinstance(layer, (VideoLayer, ImageLayer)):
            return layer
    return None


def spec_to_video_ffmpeg_args(
    spec: RenderSpec,
    *,
    image_total_frames: Optional[int] = None,
) -> dict[str, str | None]:
    """RenderSpec의 footage 레이어 1개에 대한 `-vf`/`-af` 인자.

    반환 형식: `{"vf": str | None, "af": str | None, "layer_type": "video"|"image"|None}`.
    호출자가 `["-vf", vf]`/`["-af", af]`를 ffmpeg argv에 끼워 넣는다.

    - VideoLayer: `vf`=공간+시간(setpts) 체인, `af`=atempo 체인(또는 None).
    - ImageLayer: `vf`=Ken Burns 체인(zoompan 포함), `af`=None. `image_total_frames`가
      None이면 30fps × 5초 = 150 프레임으로 가정.
    - footage 레이어가 없으면 둘 다 None, `layer_type`=None.
    """
    layer = find_footage_layer(spec)
    if layer is None:
        return {"vf": None, "af": None, "layer_type": None}
    if isinstance(layer, VideoLayer):
        vf = video_layer_to_vf(layer, spec.canvas)
        af = video_layer_to_atempo(layer)
        return {"vf": vf, "af": af or None, "layer_type": "video"}
    # ImageLayer
    if image_total_frames is None:
        image_total_frames = max(1, int(spec.duration_sec * spec.canvas.fps))
    vf = image_layer_to_kenburns_vf(
        layer, spec.canvas, image_total_frames, spec.canvas.fps
    )
    return {"vf": vf, "af": None, "layer_type": "image"}


__all__ = [
    "find_footage_layer",
    "image_layer_to_kenburns_vf",
    "spec_to_video_ffmpeg_args",
    "video_layer_to_atempo",
    "video_layer_to_vf",
]
