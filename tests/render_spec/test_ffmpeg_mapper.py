"""RenderSpec → ffmpeg 매퍼 회귀 테스트 (#303).

검증 원칙:
1. **순수 매퍼**: 같은 RenderSpec 입력에 같은 vf/af 문자열이 나온다.
2. **시간/공간 분리**: footage_speed가 바뀌어도 vf 안의 scale/crop/pad 부분은 동일.
   playback_rate는 vf의 마지막 setpts 한 부분과 af의 atempo에만 영향.
3. **fit × footage_speed × aspect 18조합**: fit(3) × speed(3) × aspect(2)에서
   매퍼가 만든 vf/af가 깨지지 않고, 핵심 불변식(공간 부분이 speed와 무관)이 유지된다.
4. **추상 키워드 부재**: 매퍼 결과 문자열에 "cover"/"contain"/"stretch"가 등장하지 않는다.
5. **Ken Burns 경로 분리**: ImageLayer는 zoompan 식만, VideoLayer는 setpts/scale 식만.

본 테스트는 ffmpeg를 *실행하지 않는다* — 명령 문자열 동등성과 패턴만 검사한다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.render_spec import (
    Canvas,
    ImageLayer,
    Size,
    VideoLayer,
    build_render_spec,
    spec_to_video_ffmpeg_args,
    video_layer_to_atempo,
    video_layer_to_vf,
)


# ── 헬퍼: 가짜 SceneCard / Project ────────────────────────────────────────

def make_clip_footage(file="footage.mp4", fit="contain", rotation=0,
                       x_offset=0.0, y_offset=0.0):
    return SimpleNamespace(
        type="clip", file=file, fit=fit, rotation=rotation,
        x_offset=x_offset, y_offset=y_offset,
        clip_start_sec=None, clip_end_sec=None,
    )


def make_kenburns_footage(image="image.jpg", fit="cover"):
    return SimpleNamespace(type="kenburns", image=image, fit=fit)


def make_scene(scene_id=1, footage_speed=1.0, footage=None, duration_hint_sec=5.0):
    return SimpleNamespace(
        scene_id=scene_id, footage=footage, footage_speed=footage_speed,
        duration_hint_sec=duration_hint_sec,
        text_overlays=[], shape_overlays=[], graphic_overlays=[], mosaic_overlays=[],
    )


def make_project(width=1920, height=1080):
    return SimpleNamespace(output_width=width, output_height=height)


# ── 1. 회전 매핑 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("rot,expected_prefix", [
    (0, ""),
    (90, "transpose=1"),
    (180, "hflip,vflip"),
    (270, "transpose=2"),
])
def test_rotation_filter_mapping(rot, expected_prefix):
    """rotation_deg가 ffmpeg transpose/hflip+vflip으로 정확히 매핑된다."""
    layer = VideoLayer(
        source="x.mp4",
        source_size=Size(width=1920, height=1080),
        scale_x=1.0, scale_y=1.0,
        rotation_deg=rot,
        playback_rate=1.0,
        start_sec=0.0, end_sec=5.0,
    )
    canvas = Canvas(width=1920, height=1080, fps=30)
    vf = video_layer_to_vf(layer, canvas)
    if expected_prefix:
        assert vf.startswith(expected_prefix + ","), f"vf={vf}"
    else:
        assert not vf.startswith("transpose"), f"vf={vf}"
        assert not vf.startswith("hflip"), f"vf={vf}"


# ── 2. playback_rate가 vf의 setpts와 af의 atempo만 바꾼다 ────────────────

def test_playback_rate_does_not_affect_scale_or_crop():
    """playback_rate가 바뀌어도 scale/crop/pad 부분은 그대로다 (시간/공간 분리)."""
    base_layer_kwargs = dict(
        source="x.mp4",
        source_size=Size(width=1920, height=1080),
        scale_x=0.5, scale_y=0.5,
        rotation_deg=0,
        start_sec=0.0, end_sec=5.0,
    )
    canvas = Canvas(width=1920, height=1080, fps=30)
    vf_normal = video_layer_to_vf(VideoLayer(**base_layer_kwargs, playback_rate=1.0), canvas)
    vf_slow = video_layer_to_vf(VideoLayer(**base_layer_kwargs, playback_rate=0.5), canvas)
    vf_fast = video_layer_to_vf(VideoLayer(**base_layer_kwargs, playback_rate=2.0), canvas)

    # rate 1.0은 setpts가 안 붙음
    assert "setpts" not in vf_normal
    # 다른 rate는 setpts가 *맨 끝에만* 붙음 → 나머지 prefix는 동일
    assert vf_slow.startswith(vf_normal + ",setpts=")
    assert vf_fast.startswith(vf_normal + ",setpts=")
    # setpts 인자는 rate별로 다름
    assert "setpts=2.000000" in vf_slow  # 1/0.5
    assert "setpts=0.500000" in vf_fast  # 1/2.0


@pytest.mark.parametrize("rate,expected", [
    (1.0, ""),
    (0.5, "atempo=0.500000"),
    (2.0, "atempo=2.000000"),
    (4.0, "atempo=2.0,atempo=2.000000"),   # 4.0 > 2.0 → 2.0 × 2.0
    (0.25, "atempo=0.5,atempo=0.500000"),  # 0.25 < 0.5 → 0.5 × 0.5
])
def test_atempo_chain_for_rate_range(rate, expected):
    """atempo 0.5~2.0 범위 밖은 체이닝, 1.0은 빈 문자열."""
    layer = VideoLayer(
        source="x.mp4",
        source_size=Size(width=1920, height=1080),
        scale_x=1.0, scale_y=1.0,
        playback_rate=rate,
        start_sec=0.0, end_sec=5.0,
    )
    assert video_layer_to_atempo(layer) == expected


# ── 3. fit × footage_speed × aspect 18조합 (build → mapper 통합) ──────────

# fit(3) × footage_speed(3) × aspect(2) = 18 케이스.
FITS = ["contain", "cover", "stretch"]
SPEEDS = [0.5, 1.0, 2.0]
ASPECTS = [
    # (canvas_w, canvas_h, source_w, source_h, label)
    (1920, 1080, 1920, 1080, "16:9_canvas_16:9_source"),
    (1080, 1920, 1920, 1080, "9:16_canvas_16:9_source"),
]


@pytest.mark.parametrize("fit", FITS)
@pytest.mark.parametrize("speed", SPEEDS)
@pytest.mark.parametrize("cw,ch,sw,sh,label", ASPECTS)
def test_fit_speed_aspect_combinations(fit, speed, cw, ch, sw, sh, label):
    """18조합 전체에서 매퍼가 안 깨지고, 시간/공간 분리 불변식이 유지된다."""
    scene = make_scene(
        footage_speed=speed,
        footage=make_clip_footage(fit=fit),
    )
    project = make_project(width=cw, height=ch)
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=sw, height=sh),
        footage_duration_sec=10.0,
    )
    args = spec_to_video_ffmpeg_args(spec)
    vf = args["vf"]
    af = args["af"]

    assert vf is not None and len(vf) > 0, f"{fit}/{speed}/{label}: vf 없음"
    assert args["layer_type"] == "video"

    # 추상 키워드 부재
    for kw in ("contain", "cover", "stretch"):
        assert kw not in vf, f"{fit}/{speed}/{label}: vf에 {kw} 등장: {vf}"
        if af:
            assert kw not in af

    # scale 명령이 반드시 존재
    assert "scale=" in vf
    # setsar=1,fps=30 마지막 직전에 들어가 있어야 함
    assert "setsar=1,fps=30" in vf

    # 시간/공간 분리 검증: speed != 1.0이면 setpts가 *마지막* 토큰
    tokens = vf.split(",")
    if speed != 1.0:
        assert tokens[-1].startswith("setpts="), f"setpts가 마지막이 아님: {vf}"
        # 공간 부분(=setpts 제외)을 같은 fit/aspect에서 speed=1.0과 비교하면 동일해야 함
        scene_ref = make_scene(footage_speed=1.0, footage=make_clip_footage(fit=fit))
        spec_ref = build_render_spec(
            scene_ref, project,
            footage_size=Size(width=sw, height=sh),
            footage_duration_sec=10.0,
        )
        vf_ref = spec_to_video_ffmpeg_args(spec_ref)["vf"]
        # ref의 vf 전체가 speed!=1.0 vf의 setpts 직전까지와 같아야 한다
        assert vf.startswith(vf_ref + ",setpts="), (
            f"공간 부분이 speed 분기로 바뀜 — speed={speed}, fit={fit}, label={label}\n"
            f"vf_ref={vf_ref}\nvf={vf}"
        )

    # 오디오 atempo: speed != 1.0이면 비어있지 않아야 함
    if speed != 1.0:
        assert af, f"speed={speed}인데 af 비어있음"
    else:
        assert af is None


# ── 4. fit별 핵심 필터 존재 검증 ──────────────────────────────────────────

def test_contain_uses_pad_not_crop():
    """contain은 pad만 사용. crop이 들어가면 안 됨."""
    scene = make_scene(footage=make_clip_footage(fit="contain"))
    project = make_project(width=1080, height=1920)  # 세로 캔버스
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=5.0)
    vf = spec_to_video_ffmpeg_args(spec)["vf"]
    assert "pad=" in vf, f"contain인데 pad 없음: {vf}"
    assert "crop=" not in vf, f"contain인데 crop 등장: {vf}"


def test_cover_uses_crop_not_pad():
    """cover는 crop만 사용. pad가 들어가면 안 됨."""
    scene = make_scene(footage=make_clip_footage(fit="cover"))
    project = make_project(width=1080, height=1920)
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=5.0)
    vf = spec_to_video_ffmpeg_args(spec)["vf"]
    assert "crop=" in vf, f"cover인데 crop 없음: {vf}"
    assert "pad=" not in vf, f"cover인데 pad 등장: {vf}"


def test_stretch_uses_neither_crop_nor_pad():
    """stretch는 scale만. crop/pad 모두 없어야 함."""
    scene = make_scene(footage=make_clip_footage(fit="stretch"))
    project = make_project(width=1080, height=1920)
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=5.0)
    vf = spec_to_video_ffmpeg_args(spec)["vf"]
    assert "crop=" not in vf, f"stretch인데 crop 등장: {vf}"
    assert "pad=" not in vf, f"stretch인데 pad 등장: {vf}"
    # scale=1080:1920 형태로 들어가야 함 (스트레치)
    assert "scale=1080:1920" in vf


# ── 5. Ken Burns(이미지) 경로 분리 ────────────────────────────────────────

def test_kenburns_uses_zoompan_not_setpts():
    """ImageLayer는 zoompan을 만들고 setpts 시간축 변환은 안 한다."""
    scene = make_scene(footage_speed=1.0, footage=make_kenburns_footage(fit="cover"))
    project = make_project(width=1920, height=1080)
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=5.0)
    args = spec_to_video_ffmpeg_args(spec, image_total_frames=150)
    assert args["layer_type"] == "image"
    vf = args["vf"]
    assert "zoompan=" in vf
    assert "setpts=" not in vf  # 이미지 경로엔 setpts 없음
    # zoom 식: zoom_from(1.0) → zoom_to(1.15)
    assert "1.000000+0.150000*on/149" in vf
    # 오디오 없음
    assert args["af"] is None


# ── 6. footage 없는 spec ──────────────────────────────────────────────────

def test_no_footage_returns_none_args():
    scene = make_scene(footage=None)
    project = make_project(width=1920, height=1080)
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=5.0)
    args = spec_to_video_ffmpeg_args(spec)
    assert args["vf"] is None
    assert args["af"] is None
    assert args["layer_type"] is None


# ── 7. 회귀 방지: 기존 cut_clip의 contain 출력과 같은 vf 구조 ─────────────

def test_contain_vf_matches_legacy_cut_clip_structure():
    """contain 모드에서 매퍼가 만드는 vf가 기존 cut_clip의 vf와 같은 구조다.

    기존 cut_clip(contain, x_offset=0, y_offset=0, rotation=0)의 vf 형태:
        scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(W-iw)/2:(H-ih)/2:black,...

    매퍼는 builder가 미리 pad 좌표를 픽셀로 계산해두므로 표현식이 아닌 절대 픽셀이지만,
    동일한 *시각적 결과*를 만들어야 한다. 본 테스트는 의미 단위(scale 후 pad 존재)와
    캔버스 크기로 정확히 패딩되는지만 검증한다.
    """
    scene = make_scene(footage=make_clip_footage(fit="contain"))
    project = make_project(width=1080, height=1920)  # 세로 캔버스, 가로 source
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=5.0)
    vf = spec_to_video_ffmpeg_args(spec)["vf"]

    # contain: pad 크기는 캔버스 크기와 같아야 함
    assert "pad=1080:1920:" in vf, f"pad가 캔버스 크기로 안 됨: {vf}"


def test_cover_vf_crop_matches_canvas():
    """cover 모드에서 crop 크기가 캔버스 크기와 같아야 한다."""
    scene = make_scene(footage=make_clip_footage(fit="cover"))
    project = make_project(width=1080, height=1920)
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=5.0)
    vf = spec_to_video_ffmpeg_args(spec)["vf"]
    assert "crop=1080:1920:" in vf, f"crop이 캔버스 크기로 안 됨: {vf}"


# ── 8. 결정적 출력 (같은 입력 → 같은 vf) ─────────────────────────────────

def test_mapper_is_deterministic():
    """같은 RenderSpec을 두 번 통과시키면 정확히 같은 vf/af가 나와야 한다."""
    scene = make_scene(footage_speed=0.5, footage=make_clip_footage(fit="cover", rotation=90))
    project = make_project(width=1920, height=1080)
    spec = build_render_spec(scene, project,
                             footage_size=Size(width=1920, height=1080),
                             footage_duration_sec=10.0)
    args1 = spec_to_video_ffmpeg_args(spec)
    args2 = spec_to_video_ffmpeg_args(spec)
    assert args1 == args2
