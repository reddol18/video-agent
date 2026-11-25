"""그래픽 오버레이용 라이브러리 PNG 자산 로더 (#71).

기존 ``src/clip_editor.py`` 의 ``_draw_reaction_icon_shape`` / ``_build_smell_cloud_image``
/ ``_render_alert_png`` / ``_render_target_png`` 가 직접 도형을 그리던 코드의 **상위 옵션**
으로 동작한다. 자산이 있으면 PIL 이미지를 그대로 반환하고, 없으면 ``None`` 을 돌려 호출
측이 기존 벡터 그리기 폴백을 타게 한다.

자산 위치: ``assets/graphics/{category}/{name}.png`` (Twemoji 72×72, CC-BY 4.0).
파일이 추가/교체되면 별도 코드 변경 없이 적용된다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# 리포 루트 기준 자산 디렉토리. ``src/clip_editor.py`` 와 같은 작업 디렉토리 가정.
_ASSETS_ROOT = Path(__file__).resolve().parent.parent / "assets" / "graphics"


# 카테고리/이름 → 파일명. 새로운 자산을 추가하려면 카테고리 dict 에 한 줄 더한다.
# 카테고리 키는 clip_editor 분기에서 그대로 사용한다.
_ASSET_TABLE: dict[str, dict[str, str]] = {
    "reaction": {
        "thunder": "thunder.png",
        "sweat": "sweat.png",
        "question": "question.png",
        "exclamation": "exclamation.png",
    },
    "alert": {
        "warning": "warning.png",
        "info": "info.png",
        "error": "error.png",
    },
    "target": {
        "crosshair": "crosshair.png",
        "arrow": "arrow.png",
    },
    "smell": {
        # 단일 cloud 자산을 후처리(colorize)로 색 변환. green/brown/white 등은
        # 호출 측이 직접 PIL 로 변환한다.
        "cloud": "cloud.png",
    },
}


def get_asset_path(category: str, name: str) -> "str | None":
    """카테고리/이름 → 절대 경로. 미존재 시 None.

    예: ``get_asset_path("reaction", "thunder")`` → ``/.../assets/graphics/reaction/thunder.png``
    """
    files = _ASSET_TABLE.get(category)
    if not files:
        return None
    filename = files.get(name)
    if not filename:
        return None
    p = _ASSETS_ROOT / category / filename
    if not p.exists() or not p.is_file():
        return None
    return str(p)


@lru_cache(maxsize=32)
def _load_asset_cached(category: str, name: str):
    """PIL.RGBA Image 로 자산을 한 번만 디코드해 캐시. 미존재 시 None.

    캐시 키는 (category, name) 만. 사이즈 변환은 호출 측이 ``copy().resize(...)`` 로 진행.
    이 함수가 돌려주는 ``Image`` 는 **읽기 전용 참조** 로 취급할 것 — 직접 paste/mutate
    하지 말고 항상 ``copy()`` 후 가공한다.
    """
    p = get_asset_path(category, name)
    if not p:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        return Image.open(p).convert("RGBA")
    except Exception:
        return None


def load_resized_asset(category: str, name: str, target_px: int):
    """자산을 정사각형 ``target_px`` 로 리사이즈한 RGBA Image 사본을 반환. 미존재 시 None.

    원본 Twemoji 가 정사각형이라 폭/높이 동일하게 두고, 호출 측이 필요하면 캔버스에
    여백을 더해 합성한다. ``target_px`` 는 1 이상의 정수.
    """
    src = _load_asset_cached(category, name)
    if src is None:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    s = max(1, int(target_px))
    return src.copy().resize((s, s), Image.LANCZOS)


def colorize_asset(image, color_rgb: tuple[int, int, int]):
    """RGBA 자산의 RGB 채널을 ``color_rgb`` 로 균일 채색(알파는 보존).

    smell 처럼 단일 자산을 색상별로 재사용할 때 호출. ``image`` 가 None 이면 None 반환.
    호출 측의 원본 캐시를 망가뜨리지 않도록 반드시 ``image.copy()`` 를 받아 호출할 것.
    """
    if image is None:
        return None
    try:
        from PIL import Image
    except ImportError:
        return image
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    r, g, b, a = image.split()
    solid = Image.new("RGBA", image.size, color_rgb + (0,))
    sr, sg, sb, _ = solid.split()
    return Image.merge("RGBA", (sr, sg, sb, a))


# ── 주색상 추출 + 글로우 후처리 (#71 후속) ─────────────────────────────────
# Twemoji 단색 톤이 영상 배경에 묻혀 가독성이 낮은 문제를 보완. 자산의 주색상
# (알파>128 픽셀의 가중 평균)으로 외곽 글로우를 자동 합성한다. 사용자가 색을
# 직접 지정하지 않아도 reaction(노랑/파랑/빨강 등) 별로 적절한 톤이 나오도록.

# 글로우 권장 톤 — 채도 가중 평균이 자산의 인지 색과 어긋날 때 사용 (예: Twemoji ❓
# 는 빨간 배경에 흰 물음표라 평균은 빨강이 우세하지만 "물음표=파랑" 인지 톤이 더
# 적절). 명시 매핑이 있으면 항상 우선, 없으면 dominant_color 폴백.
_PREFERRED_GLOW_RGB: dict[tuple[str, str], tuple[int, int, int]] = {
    ("reaction", "thunder"): (255, 220, 50),    # 옐로우
    ("reaction", "sweat"): (90, 180, 245),       # 라이트블루
    ("reaction", "question"): (90, 160, 255),    # 블루
    ("reaction", "exclamation"): (255, 90, 90),  # 레드
    ("alert", "warning"): (255, 200, 60),
    ("alert", "info"): (70, 150, 230),
    ("alert", "error"): (240, 60, 60),
    ("target", "crosshair"): (255, 80, 80),
    ("target", "arrow"): (70, 160, 230),
    ("smell", "cloud"): (220, 220, 220),
}


def preferred_glow_color(category: str, name: str) -> tuple[int, int, int] | None:
    """카테고리/이름 → 권장 글로우 색. 매핑 없으면 dominant_color 폴백."""
    rgb = _PREFERRED_GLOW_RGB.get((category, name))
    if rgb is not None:
        return rgb
    return dominant_color(category, name)

@lru_cache(maxsize=64)
def _dominant_color_cached(asset_key: tuple[str, str]) -> tuple[int, int, int] | None:
    """카테고리/이름 기준 주색상 1회 계산 후 캐시. 자산 없으면 None."""
    category, name = asset_key
    src = _load_asset_cached(category, name)
    if src is None:
        return None
    return _compute_dominant_color(src)


def _compute_dominant_color(image) -> tuple[int, int, int] | None:
    """RGBA 이미지에서 채도 가중 평균 RGB 를 반환. 알파>128 픽셀만 사용.

    채도 가중을 쓰는 이유: Twemoji 는 흰색 하이라이트가 많아 단순 평균이 회색에
    가까워지는 경향. (max-min) 로 채도를 근사해 곱해주면 ⚡ 노랑/💧 파랑/💢 빨강
    같은 식별색이 살아남는다. 채도 가중 합이 너무 작으면(=거의 무채색) 단순 평균
    으로 폴백.
    """
    if image is None:
        return None
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return None
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    # 작은 썸네일로 다운샘플 — 72x72 자산도 5000+ 픽셀, 32x32 면 충분.
    thumb = image.copy()
    thumb.thumbnail((32, 32))
    px = thumb.load()
    w, h = thumb.size
    sum_r = sum_g = sum_b = 0.0
    sum_w = 0.0
    plain_r = plain_g = plain_b = 0.0
    plain_c = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 128:
                continue
            sat = max(r, g, b) - min(r, g, b)  # 0~255
            plain_r += r; plain_g += g; plain_b += b; plain_c += 1
            if sat <= 0:
                continue
            weight = sat / 255.0
            sum_r += r * weight
            sum_g += g * weight
            sum_b += b * weight
            sum_w += weight
    if sum_w > 1e-6:
        return (
            int(round(sum_r / sum_w)),
            int(round(sum_g / sum_w)),
            int(round(sum_b / sum_w)),
        )
    if plain_c > 0:
        return (
            int(round(plain_r / plain_c)),
            int(round(plain_g / plain_c)),
            int(round(plain_b / plain_c)),
        )
    return None


def dominant_color(category: str, name: str) -> tuple[int, int, int] | None:
    """카테고리/이름 → 주색상 RGB. 자산 미존재 시 None."""
    return _dominant_color_cached((category, name))


def add_outer_glow(
    image,
    color_rgb: tuple[int, int, int] | None = None,
    blur_radius: int | None = None,
    intensity: float = 1.0,
    pad: int | None = None,
    outline_color: tuple[int, int, int] = (0, 0, 0),
    outline_width: int | None = None,
):
    """이미지 외곽에 글로우(어우라) + 외곽선을 합성한 RGBA 사본을 반환한다.

    동작:
      1. 입력보다 ``pad`` 만큼 큰 캔버스에 알파 마스크를 가져와 가우시안 블러로 부풂(글로우).
      2. 같은 알파 마스크를 작은 반경(MaxFilter)으로 살짝 부풀려 외곽선 레이어를 만듬.
      3. 캔버스 위에 글로우 → 외곽선 → 원본 이미지 순으로 alpha_composite.

    파라미터:
      color_rgb: 글로우 색. None 이면 입력 이미지의 주색상으로 자동 선택.
      blur_radius: 가우시안 블러 반경(px). None 이면 입력 크기 기준 자동 (max(short_edge*0.18, 4)).
      intensity: 글로우 알파 multiplier (0.0~2.0 권장). 기본 1.0.
      pad: 캔버스 패딩(px). None 이면 blur_radius*2.
      outline_color: 외곽선 RGB. 기본 검정.
      outline_width: 외곽선 두께(px). None 이면 short_edge*0.025 (대략 자산의 2~3%).

    None 반환: 입력이 None 이거나 PIL 미설치.
    """
    if image is None:
        return None
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return image

    if image.mode != "RGBA":
        image = image.convert("RGBA")

    w, h = image.size
    short_edge = min(w, h)
    if blur_radius is None:
        blur_radius = max(4, int(short_edge * 0.18))
    if pad is None:
        pad = blur_radius * 2
    if outline_width is None:
        outline_width = max(1, int(short_edge * 0.025))

    if color_rgb is None:
        color_rgb = _compute_dominant_color(image) or (255, 255, 255)

    canvas_w = w + pad * 2
    canvas_h = h + pad * 2

    # 알파 마스크를 캔버스에 패딩하여 배치 (글로우/외곽선 공유)
    mask = Image.new("L", (canvas_w, canvas_h), 0)
    src_alpha = image.split()[3]
    mask.paste(src_alpha, (pad, pad))

    # 1) 글로우 — 큰 반경의 가우시안 블러
    blurred = mask.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    if intensity != 1.0:
        blurred = blurred.point(lambda v: min(255, int(v * intensity)))
    glow_layer = Image.new("RGBA", (canvas_w, canvas_h), color_rgb + (0,))
    glow_layer.putalpha(blurred)

    # 2) 외곽선 — 알파 마스크를 MaxFilter 로 ``outline_width`` 만큼 살짝 부풀려 검정 단색 레이어.
    # MaxFilter 는 홀수 size 만 지원하므로 2*w+1 사용. 작은 GaussianBlur 로 살짝 부드럽게.
    if outline_width > 0:
        dilate_size = outline_width * 2 + 1
        # MaxFilter 는 큰 커널일 때 비싸므로 단계 반복으로 대체
        outline_alpha = mask
        remaining = outline_width
        while remaining > 0:
            step = min(remaining, 5)  # 한 번에 최대 5px
            outline_alpha = outline_alpha.filter(ImageFilter.MaxFilter(step * 2 + 1))
            remaining -= step
        # 살짝 부드럽게 (계단현상 제거)
        outline_alpha = outline_alpha.filter(ImageFilter.GaussianBlur(radius=0.7))
        outline_layer = Image.new("RGBA", (canvas_w, canvas_h), outline_color + (0,))
        outline_layer.putalpha(outline_alpha)
    else:
        outline_layer = None

    # 최종 합성 캔버스: 글로우 → 외곽선 → 원본
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    canvas.alpha_composite(glow_layer)
    if outline_layer is not None:
        canvas.alpha_composite(outline_layer)
    canvas.alpha_composite(image, dest=(pad, pad))
    return canvas


__all__ = [
    "get_asset_path",
    "load_resized_asset",
    "colorize_asset",
    "dominant_color",
    "preferred_glow_color",
    "add_outer_glow",
]
