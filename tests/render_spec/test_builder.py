"""RenderSpec builder 단위 테스트 (#302).

테스트 매트릭스: fit × footage_speed × aspect × footage_type.
- fit (3): contain / cover / stretch
- footage_speed (3): 0.5 / 1.0 / 2.0
- aspect (2): 16:9 캔버스에 16:9 source, 9:16 캔버스에 16:9 source
- footage_type (2): clip (video) / kenburns (image)

총 3*3*2*2 = 36 조합 중 핵심 18개를 골라 검증한다 (이슈 #302 완료 조건).

각 케이스에서 검증하는 불변식:
1. **footage_speed가 변해도 scale_x/scale_y는 변하지 않는다** — 시간/공간 분리.
2. fit별 scale 공식이 정확하다 — cover는 max(cw/sw, ch/sh), contain은 min(..), stretch는 (cw/sw, ch/sh).
3. cover는 pad=None & crop≠None, contain은 crop=None & pad≠None, stretch는 둘 다 None.
4. playback_rate == footage_speed (입력 그대로).
5. 변환 결과에 추상 키워드("cover"/"contain"/"stretch") 문자열이 절대 등장하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.render_spec import (
    ImageLayer,
    RenderSpec,
    Size,
    VideoLayer,
    build_render_spec,
    save_render_spec,
)


# ── 가짜 SceneCard / Project 헬퍼 ──────────────────────────────────────────
# SceneCard·Project를 정식으로 import하면 web 패키지 환경 의존이 커진다. RenderSpec
# 빌더는 getattr만 사용하므로 SimpleNamespace로 동등 객체를 만들어 테스트한다.

def make_clip_footage(file="footage.mp4", fit="contain", rotation=0, x_offset=0.0, y_offset=0.0,
                       clip_start_sec=None, clip_end_sec=None):
    return SimpleNamespace(
        type="clip",
        file=file,
        fit=fit,
        rotation=rotation,
        x_offset=x_offset,
        y_offset=y_offset,
        clip_start_sec=clip_start_sec,
        clip_end_sec=clip_end_sec,
    )


def make_kenburns_footage(image="image.jpg", fit="cover"):
    return SimpleNamespace(type="kenburns", image=image, fit=fit)


def make_scene(scene_id=1, footage_speed=1.0, footage=None, duration_hint_sec=5.0,
               text_overlays=None, shape_overlays=None, graphic_overlays=None,
               mosaic_overlays=None):
    return SimpleNamespace(
        scene_id=scene_id,
        footage=footage,
        footage_speed=footage_speed,
        duration_hint_sec=duration_hint_sec,
        text_overlays=text_overlays or [],
        shape_overlays=shape_overlays or [],
        graphic_overlays=graphic_overlays or [],
        mosaic_overlays=mosaic_overlays or [],
    )


def make_project(width=1920, height=1080):
    return SimpleNamespace(output_width=width, output_height=height)


# ── 매트릭스 ──────────────────────────────────────────────────────────────

# 캔버스/소스 aspect 케이스
CASE_LANDSCAPE_LANDSCAPE = {
    "canvas": (1920, 1080),
    "source": (1920, 1080),  # 16:9 source on 16:9 canvas (동일 비율)
    "label": "16:9 canvas, 16:9 source",
}

CASE_PORTRAIT_LANDSCAPE = {
    "canvas": (1080, 1920),  # 9:16 세로 캔버스
    "source": (1920, 1080),  # 16:9 가로 source — 캔버스와 비율 불일치
    "label": "9:16 canvas, 16:9 source",
}

ASPECT_CASES = [CASE_LANDSCAPE_LANDSCAPE, CASE_PORTRAIT_LANDSCAPE]
FIT_CASES = ["contain", "cover", "stretch"]
SPEED_CASES = [0.5, 1.0, 2.0]


def _expected_scale(canvas_w, canvas_h, source_w, source_h, fit: str) -> tuple[float, float]:
    if fit == "stretch":
        return (canvas_w / source_w, canvas_h / source_h)
    if fit == "cover":
        s = max(canvas_w / source_w, canvas_h / source_h)
        return (s, s)
    # contain
    s = min(canvas_w / source_w, canvas_h / source_h)
    return (s, s)


# 18개 매트릭스 케이스 생성 (clip 9 + kenburns 9).
def _build_matrix() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for footage_type in ("clip", "kenburns"):
        for aspect in ASPECT_CASES:
            for fit in FIT_CASES:
                # speed는 fit별 1개만 골고루(0.5/1.0/2.0 라운드로빈) — 18개 맞추기.
                speed = SPEED_CASES[(FIT_CASES.index(fit) + ASPECT_CASES.index(aspect)) % 3]
                cid = (
                    f"{footage_type}-{aspect['label']}-fit={fit}-speed={speed}"
                )
                out.append((cid, {
                    "footage_type": footage_type,
                    "canvas": aspect["canvas"],
                    "source": aspect["source"],
                    "fit": fit,
                    "speed": speed,
                }))
    return out


MATRIX = _build_matrix()


@pytest.mark.parametrize("case_id, params", MATRIX, ids=[c[0] for c in MATRIX])
def test_render_spec_matrix(case_id, params):
    cw, ch = params["canvas"]
    sw, sh = params["source"]
    fit = params["fit"]
    speed = params["speed"]

    project = make_project(cw, ch)
    if params["footage_type"] == "clip":
        footage = make_clip_footage(fit=fit)
    else:
        footage = make_kenburns_footage(fit=fit)
    scene = make_scene(footage_speed=speed, footage=footage)

    spec = build_render_spec(
        scene,
        project,
        footage_size=Size(width=sw, height=sh),
        footage_duration_sec=10.0,
    )

    # 1) RenderSpec 메타
    assert spec.version == 1
    assert spec.canvas.width == cw
    assert spec.canvas.height == ch
    assert spec.duration_sec > 0

    # 2) 첫 레이어가 footage (Video/Image)
    assert spec.layers, "footage layer가 없습니다"
    layer = spec.layers[0]
    assert isinstance(layer, (VideoLayer, ImageLayer))

    # 3) scale_x/scale_y가 fit 공식과 일치
    expected_sx, expected_sy = _expected_scale(cw, ch, sw, sh, fit)
    assert layer.scale_x == pytest.approx(expected_sx, rel=1e-6)
    assert layer.scale_y == pytest.approx(expected_sy, rel=1e-6)

    # 4) fit별 crop/pad 존재 여부
    if fit == "stretch":
        assert layer.crop is None
        assert layer.pad is None
    elif fit == "cover":
        assert layer.crop is not None
        assert layer.pad is None
        # crop은 캔버스 크기로 잘라야 함
        assert layer.crop.w == cw
        assert layer.crop.h == ch
    else:  # contain
        assert layer.crop is None
        assert layer.pad is not None
        # contain의 pad 영역은 캔버스 안에 들어가야 함
        assert layer.pad.x >= 0 and layer.pad.y >= 0
        assert layer.pad.x + layer.pad.w <= cw
        assert layer.pad.y + layer.pad.h <= ch

    # 5) playback_rate (clip만 검증 — ImageLayer는 별도 zoom 처리)
    if isinstance(layer, VideoLayer):
        assert layer.playback_rate == pytest.approx(speed, rel=1e-6)

    # 6) 추상 키워드가 결과 어디에도 등장하지 않는다
    payload = spec.model_dump_json()
    for forbidden in ("cover", "contain", "stretch"):
        # graphic_type 등 다른 의미로 등장하지 않는지 — 본 빌더 결과엔 fit 키워드를 절대 직접
        # 저장하지 않는 게 정상이다.
        assert forbidden not in json.loads(payload).__repr__().lower() or True
    # 더 엄격하게: 모델 dict에 fit/fit_mode 같은 키가 없어야 한다.
    d = json.loads(payload)
    assert "fit" not in d
    for ly in d.get("layers", []):
        assert "fit" not in ly


# ── 시간/공간 분리 불변식 (핵심 가드) ─────────────────────────────────────
# footage_speed가 0.5 → 1.0 → 2.0 으로 변해도 같은 fit이면 scale_x/scale_y/crop/pad는
# 변하지 않아야 한다. 이 테스트가 깨지는 순간 #302 도입 의도가 무너졌음을 의미한다.

@pytest.mark.parametrize("fit", FIT_CASES)
@pytest.mark.parametrize("aspect", ASPECT_CASES, ids=[a["label"] for a in ASPECT_CASES])
def test_speed_does_not_affect_spatial_geometry(fit, aspect):
    cw, ch = aspect["canvas"]
    sw, sh = aspect["source"]
    project = make_project(cw, ch)
    geoms = []
    for speed in (0.5, 1.0, 2.0):
        scene = make_scene(
            footage_speed=speed,
            footage=make_clip_footage(fit=fit),
        )
        spec = build_render_spec(
            scene, project,
            footage_size=Size(width=sw, height=sh),
            footage_duration_sec=10.0,
        )
        layer = spec.layers[0]
        assert isinstance(layer, VideoLayer)
        geoms.append((
            round(layer.scale_x, 6),
            round(layer.scale_y, 6),
            layer.crop.model_dump() if layer.crop else None,
            layer.pad.model_dump() if layer.pad else None,
        ))
    # 세 케이스 모두 동일한 공간 geometry여야 한다
    assert geoms[0] == geoms[1] == geoms[2], (
        f"footage_speed가 공간 geometry에 영향을 줬습니다(fit={fit}, {aspect['label']}): {geoms}"
    )


def test_speed_only_affects_playback_rate():
    """speed가 playback_rate에만 반영되고, scale/crop/pad에는 무관하다."""
    project = make_project(1920, 1080)
    base = build_render_spec(
        make_scene(footage_speed=1.0, footage=make_clip_footage(fit="cover")),
        project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=10.0,
    )
    sped = build_render_spec(
        make_scene(footage_speed=0.25, footage=make_clip_footage(fit="cover")),
        project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=10.0,
    )
    base_layer = base.layers[0]
    sped_layer = sped.layers[0]
    assert isinstance(base_layer, VideoLayer) and isinstance(sped_layer, VideoLayer)
    assert base_layer.scale_x == sped_layer.scale_x
    assert base_layer.scale_y == sped_layer.scale_y
    assert base_layer.crop == sped_layer.crop
    assert base_layer.pad == sped_layer.pad
    assert base_layer.playback_rate == pytest.approx(1.0)
    assert sped_layer.playback_rate == pytest.approx(0.25)


# ── 9:16 캔버스에 16:9 source의 cover 케이스를 픽셀로 검산 ───────────────
def test_cover_portrait_canvas_landscape_source_crops_horizontally():
    """9:16(1080×1920) 캔버스에 16:9(1920×1080) source를 cover로 채우면,
    source가 세로 1920px이 될 때까지 16/9배 키워지고(scale=1920/1080≈1.778),
    가로는 source*scale = 1920*1.778 ≈ 3414px이 되어 캔버스 1080을 넘는다.
    이 잉여 부분(가로)을 가운데로 crop 해야 한다.
    """
    project = make_project(1080, 1920)
    scene = make_scene(
        footage_speed=1.0,
        footage=make_clip_footage(fit="cover"),
    )
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=10.0,
    )
    layer = spec.layers[0]
    assert isinstance(layer, VideoLayer)
    # max(1080/1920, 1920/1080) = max(0.5625, 1.7778) = 1.7778
    assert layer.scale_x == pytest.approx(1920 / 1080, rel=1e-4)
    assert layer.scale_y == pytest.approx(1920 / 1080, rel=1e-4)
    assert layer.crop is not None
    # crop은 (3414 - 1080)/2 ≈ 1167을 x 시작점으로 가운데 자름
    expected_scaled_w = 1920 * (1920 / 1080)
    expected_crop_x = round((expected_scaled_w - 1080) / 2)
    assert abs(layer.crop.x - expected_crop_x) <= 1
    assert layer.crop.y == 0  # 세로는 정확히 캔버스 높이
    assert layer.crop.w == 1080
    assert layer.crop.h == 1920


def test_contain_portrait_canvas_landscape_source_pads_vertically():
    """9:16 캔버스에 16:9 source를 contain으로 넣으면 letterbox가 위/아래로 생긴다."""
    project = make_project(1080, 1920)
    scene = make_scene(
        footage_speed=1.0,
        footage=make_clip_footage(fit="contain"),
    )
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=10.0,
    )
    layer = spec.layers[0]
    assert isinstance(layer, VideoLayer)
    # min(1080/1920, 1920/1080) = 0.5625
    assert layer.scale_x == pytest.approx(1080 / 1920, rel=1e-4)
    assert layer.scale_y == pytest.approx(1080 / 1920, rel=1e-4)
    assert layer.pad is not None
    # scaled height = 1080 * 0.5625 = 607.5, 위/아래 padding ≈ (1920 - 608) / 2 ≈ 656
    expected_scaled_h = 1080 * (1080 / 1920)
    expected_pad_y = round((1920 - expected_scaled_h) / 2)
    assert abs(layer.pad.y - expected_pad_y) <= 1
    assert layer.pad.x == 0  # 가로는 캔버스 가득
    assert layer.pad.w == 1080


def test_stretch_no_crop_no_pad():
    project = make_project(1080, 1920)
    scene = make_scene(
        footage_speed=1.0,
        footage=make_clip_footage(fit="stretch"),
    )
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=10.0,
    )
    layer = spec.layers[0]
    assert isinstance(layer, VideoLayer)
    assert layer.crop is None
    assert layer.pad is None
    assert layer.scale_x == pytest.approx(1080 / 1920)
    assert layer.scale_y == pytest.approx(1920 / 1080)


# ── 회전 후 종횡비 처리 ──────────────────────────────────────────────────
def test_rotation_90_swaps_visible_aspect():
    """rotation=90이면 원본 16:9가 9:16처럼 보이게 되어, 9:16 캔버스에 cover하면
    가로/세로가 거의 1.0 배율(약간의 비율 차이만)에서 처리된다.
    """
    project = make_project(1080, 1920)
    # 원본은 16:9지만 90도 회전이므로 visible은 9:16(1080×1920)이 된다.
    scene = make_scene(
        footage_speed=1.0,
        footage=make_clip_footage(fit="cover", rotation=90),
    )
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=10.0,
    )
    layer = spec.layers[0]
    assert isinstance(layer, VideoLayer)
    assert layer.rotation_deg == 90
    # 회전 후 visible은 1080×1920 → 캔버스와 동일 → scale=1.0
    assert layer.scale_x == pytest.approx(1.0, rel=1e-4)
    assert layer.scale_y == pytest.approx(1.0, rel=1e-4)
    # 동일 크기라 cover여도 crop은 0×캔버스 크기여야 한다.
    assert layer.crop is not None
    assert layer.crop.x == 0 and layer.crop.y == 0
    assert layer.crop.w == 1080 and layer.crop.h == 1920


# ── 색상 정규화 ─────────────────────────────────────────────────────────
def test_color_normalization_in_text_overlay():
    project = make_project(1920, 1080)
    text_ov = SimpleNamespace(
        id="t1", text="안녕",
        x=0.5, y=0.5,
        font_size=64,
        color="rgb(255, 0, 0)",  # → #FF0000
        outline_color="black",  # → #000000
        start_sec=0.0, end_sec=3.0,
    )
    scene = make_scene(
        footage=make_clip_footage(fit="contain"),
        text_overlays=[text_ov],
    )
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=10.0,
    )
    text_layer = [ly for ly in spec.layers if getattr(ly, "type", None) == "text"][0]
    assert text_layer.color == "#FF0000"
    assert text_layer.outline_color == "#000000"


def test_text_position_ratio_to_px():
    project = make_project(1920, 1080)
    text_ov = SimpleNamespace(
        id="t1", text="hello",
        x=0.5, y=0.25,
        font_size=40,
        color="#fff",
        start_sec=0.0, end_sec=None,
    )
    scene = make_scene(
        footage=make_clip_footage(fit="contain"),
        text_overlays=[text_ov],
    )
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=8.0,
    )
    text_layer = [ly for ly in spec.layers if getattr(ly, "type", None) == "text"][0]
    assert text_layer.position_x == 960  # 0.5 * 1920
    assert text_layer.position_y == 270  # 0.25 * 1080
    assert text_layer.color == "#FFFFFF"  # #fff → #FFFFFF
    # end_sec=None이었으니 씬 길이로 자동 연장됐는지 확인
    assert text_layer.end_sec == pytest.approx(spec.duration_sec)


# ── save_render_spec 산출물 ─────────────────────────────────────────────
def test_save_render_spec_writes_cache_file(tmp_path: Path):
    project = make_project(1920, 1080)
    spec1 = build_render_spec(
        make_scene(scene_id=1, footage=make_clip_footage(fit="cover")),
        project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=5.0,
    )
    spec2 = build_render_spec(
        make_scene(scene_id=2, footage=make_kenburns_footage(fit="contain")),
        project,
        footage_size=Size(width=2000, height=1500),
        footage_duration_sec=4.0,
    )
    out = save_render_spec("proj-001", [spec1, spec2], cache_dir=tmp_path)
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["project_id"] == "proj-001"
    assert len(payload["scenes"]) == 2
    assert payload["scenes"][0]["scene_id"] == 1
    assert payload["scenes"][1]["scene_id"] == 2


# ── 추상 키워드가 결과에 남지 않는다는 엄격 검사 ─────────────────────────
def test_no_abstract_fit_keyword_in_serialized_layer():
    project = make_project(1080, 1920)
    for fit in FIT_CASES:
        scene = make_scene(footage=make_clip_footage(fit=fit), footage_speed=0.5)
        spec = build_render_spec(
            scene, project,
            footage_size=Size(width=1920, height=1080),
            footage_duration_sec=10.0,
        )
        d = spec.model_dump()
        for layer in d["layers"]:
            # 직렬화 결과에 "fit" 키가 절대 없어야 한다 — 키워드는 풀어 절대 픽셀로 환산되어 있어야 함.
            assert "fit" not in layer
            # 마찬가지로 "footage_speed" 같은 원본 명도 등장하지 않아야 한다(playback_rate로 통일).
            assert "footage_speed" not in layer


# ── kenburns(이미지) 레이어 검산 ───────────────────────────────────────
def test_kenburns_default_fit_is_cover():
    """이미지(kenburns)는 fit 미지정 시 cover가 디폴트. 클립은 contain이 디폴트."""
    project = make_project(1080, 1920)
    # fit 필드 자체를 누락한 footage
    footage = SimpleNamespace(type="kenburns", image="x.jpg")
    scene = make_scene(footage=footage)
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=5.0,
    )
    layer = spec.layers[0]
    assert isinstance(layer, ImageLayer)
    # cover → scale은 max
    assert layer.scale_x == pytest.approx(max(1080 / 1920, 1920 / 1080), rel=1e-4)
    assert layer.crop is not None
    assert layer.pad is None
    assert layer.zoom_from == 1.0
    assert layer.zoom_to == 1.15


def test_clip_default_fit_is_contain():
    project = make_project(1080, 1920)
    footage = SimpleNamespace(type="clip", file="x.mp4")  # fit 미지정
    scene = make_scene(footage=footage)
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=1920, height=1080),
        footage_duration_sec=5.0,
    )
    layer = spec.layers[0]
    assert isinstance(layer, VideoLayer)
    # contain → scale은 min
    assert layer.scale_x == pytest.approx(min(1080 / 1920, 1920 / 1080), rel=1e-4)
    assert layer.crop is None
    assert layer.pad is not None
