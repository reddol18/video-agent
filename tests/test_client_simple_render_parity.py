"""클라이언트 단순 렌더 ↔ 백엔드 ffmpeg 매퍼 정합성 검증 (#309).

배경
----
백엔드는 `video_layer_to_vf` 로 absolute 픽셀 ffmpeg vf 체인을 만든다.
프론트는 `applySimpleRenderTransform` 으로 동일한 절대 픽셀을 CSS 백분율로 환산한다.

본 테스트는 *같은 RenderSpec 입력* 에 대해 양쪽이 동일한 *기하학적 변환* 을 표현하는지
확인한다. ffmpeg/CSS 둘 다 결국 캔버스의 (x, y, w, h) 영역에 영상을 그리는 동작이며,
그 영역이 일치해야 픽셀 결과가 같아진다.

CSS 결과(좌상단 기준 캔버스 비율) 와 ffmpeg 결과(scale/crop/pad 절대 픽셀) 의 *논리적 등가*
를 다음 식으로 검증한다:

- contain (pad 사용): 영상이 캔버스 안 (pad.x, pad.y, pad.w, pad.h) 픽셀 영역에 그려짐.
- cover  (crop 사용): scaled 영상(폭 sourceW*sx, 높이 sourceH*sy) 에서 (crop.x, crop.y)
  픽셀이 캔버스의 (0, 0) 으로 오는 식으로 그려짐 → 캔버스가 잘라낸 직사각형.
- stretch: 영상이 캔버스 전체를 채움.

이 등가가 깨지면 백엔드/클라가 다른 픽셀을 그리게 되어 fit 회귀가 다시 일어난다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from src.render_spec.builder import _compute_fit_geometry
from src.render_spec.models import Canvas, Size


# ── 백엔드 builder 의 geometry 가 단순 렌더 약속(절대 픽셀) 을 따르는지 검증 ──

@pytest.mark.parametrize(
    "canvas_wh,source_wh,fit",
    [
        # 16:9 캔버스
        ((1920, 1080), (1920, 1080), "contain"),
        ((1920, 1080), (1920, 1080), "cover"),
        ((1920, 1080), (1920, 1080), "stretch"),
        ((1920, 1080), (1080, 1920), "contain"),  # 세로 소스를 가로 캔버스
        ((1920, 1080), (1080, 1920), "cover"),
        # 세로 캔버스 (숏폼)
        ((1080, 1920), (1920, 1080), "contain"),
        ((1080, 1920), (1920, 1080), "cover"),
        # 정사각
        ((1080, 1080), (1920, 1080), "contain"),
        ((1080, 1080), (1920, 1080), "cover"),
        # 작은 소스를 큰 캔버스에
        ((1920, 1080), (640, 360), "contain"),
        ((1920, 1080), (640, 360), "cover"),
    ],
)
def test_geometry_is_absolute_pixels_no_keyword(canvas_wh, source_wh, fit):
    """builder._compute_fit_geometry 는 모든 fit 을 절대 픽셀(scale/crop/pad)로 환산한다.

    클라 렌더러(`applySimpleRenderTransform`) 는 이 절대 픽셀을 CSS 백분율로 그대로 변환하므로
    builder 출력이 곧 클라/서버 픽셀 정합성의 단일 진실이다.
    """
    cw, ch = canvas_wh
    sw, sh = source_wh
    canvas = Canvas(width=cw, height=ch, fps=30)
    source = Size(width=sw, height=sh)

    sx, sy, crop, pad = _compute_fit_geometry(canvas, source, fit)

    # 모든 fit 에서 scale 은 양수.
    assert sx > 0 and sy > 0

    if fit == "stretch":
        # crop/pad 없음, scale 만으로 캔버스 채움.
        assert crop is None
        assert pad is None
        assert pytest.approx(sw * sx) == cw
        assert pytest.approx(sh * sy) == ch
    elif fit == "cover":
        # crop 사용. scale 결과는 캔버스보다 크거나 같다.
        assert pad is None
        assert crop is not None
        scaled_w = sw * sx
        scaled_h = sh * sy
        assert scaled_w >= cw - 0.5  # 부동소수점 여유
        assert scaled_h >= ch - 0.5
        # crop 영역 크기 = 캔버스 크기 (정확히)
        assert crop.w == cw
        assert crop.h == ch
        # crop 시작점은 항상 [0, scaled-cw] 범위 안.
        assert 0 <= crop.x <= scaled_w - cw + 1
        assert 0 <= crop.y <= scaled_h - ch + 1
    else:  # contain
        # pad 사용. scale 결과는 캔버스보다 작거나 같다.
        assert crop is None
        assert pad is not None
        scaled_w = sw * sx
        scaled_h = sh * sy
        assert scaled_w <= cw + 0.5
        assert scaled_h <= ch + 0.5
        # pad 영역 크기 = scaled 크기 (반올림 ±1px)
        assert abs(pad.w - scaled_w) <= 1
        assert abs(pad.h - scaled_h) <= 1
        # pad 위치는 letterbox 가운데 정렬 (반올림 ±1px)
        assert abs(pad.x - (cw - scaled_w) / 2) <= 1
        assert abs(pad.y - (ch - scaled_h) / 2) <= 1


def test_geometry_independent_of_speed():
    """playback_rate(시간 변환) 은 _compute_fit_geometry 입력에 들어가지 않는다.

    fit 과 speed 가 같은 표현식에 묶이면 2026-05-27 회귀가 재발한다.
    """
    canvas = Canvas(width=1920, height=1080, fps=30)
    source = Size(width=1920, height=1080)
    # 함수 시그니처에 playback_rate / speed 인자가 없음을 직접 확인.
    import inspect
    sig = inspect.signature(_compute_fit_geometry)
    assert "speed" not in sig.parameters
    assert "playback_rate" not in sig.parameters
    # 함수 출력은 fit 만으로 결정.
    sx1, sy1, c1, p1 = _compute_fit_geometry(canvas, source, "contain")
    sx2, sy2, c2, p2 = _compute_fit_geometry(canvas, source, "contain")
    assert (sx1, sy1) == (sx2, sy2)
    assert c1 == c2
    assert p1 == p2


def test_render_spec_simple_mode_layers_remain_video_only_after_speed_change():
    """speed 변경은 layers 구성을 바꾸지 않는다 — 단순 모드 판정이 유지되어야 한다.

    프론트 isSimpleRenderSpec 은 layers 1개 + video 타입 + opacity 1.0 + rotation 0 이면
    true. speed 는 playback_rate 필드에만 들어가므로 단순 판정에 영향을 주지 않는다.
    """
    from web.backend.models import SceneCard, ClipEffect, Project as ProjectModel
    from src.render_spec.builder import build_render_spec

    proj = ProjectModel(project_id="p", name="t", output_width=1920, output_height=1080)
    scene = SceneCard(
        scene_id=1,
        title="t",
        description="t",
        narration_text="t",
        duration_hint_sec=5.0,
        footage_speed=1.0,
        footage=ClipEffect(type="clip", file="clip.mp4", fit="contain", rotation=0),
    )
    spec_1x = build_render_spec(scene, proj)
    assert len(spec_1x.layers) == 1
    assert spec_1x.layers[0].type == "video"

    scene.footage_speed = 2.0
    spec_2x = build_render_spec(scene, proj)
    assert len(spec_2x.layers) == 1
    assert spec_2x.layers[0].type == "video"
    assert spec_2x.layers[0].playback_rate == 2.0
    # 공간 변환(scale/crop/pad) 은 speed 와 무관하게 동일해야 한다.
    assert spec_2x.layers[0].scale_x == spec_1x.layers[0].scale_x
    assert spec_2x.layers[0].scale_y == spec_1x.layers[0].scale_y
    assert spec_2x.layers[0].crop == spec_1x.layers[0].crop
    assert spec_2x.layers[0].pad == spec_1x.layers[0].pad


def test_render_spec_kenburns_produces_image_layer_not_simple():
    """kenburns 효과는 ImageLayer → isSimpleRenderSpec 가 false 판정 (백엔드 fallback)."""
    from web.backend.models import SceneCard, KenburnsEffect, Project as ProjectModel
    from src.render_spec.builder import build_render_spec

    proj = ProjectModel(project_id="p", name="t", output_width=1920, output_height=1080)
    scene = SceneCard(
        scene_id=1,
        title="t",
        description="t",
        narration_text="t",
        duration_hint_sec=5.0,
        footage=KenburnsEffect(type="kenburns", image="img.png", fit="cover"),
    )
    spec = build_render_spec(scene, proj)
    assert len(spec.layers) == 1
    assert spec.layers[0].type == "image"  # → 프론트가 단순 모드 제외 (Ken Burns)


# ── #343 회귀 — filters / motions 가 layers 에 표현되어야 단순 모드 판정에서 자동 제외 ──

def _make_scene_with_clip(**extra):
    """공통 시나리오: clip footage 한 개짜리 SceneCard. ``extra`` 로 filters/motions 주입."""
    from web.backend.models import SceneCard, ClipEffect, Project as ProjectModel

    proj = ProjectModel(project_id="p", name="t", output_width=1920, output_height=1080)
    scene = SceneCard(
        scene_id=1,
        title="t",
        description="t",
        narration_text="t",
        duration_hint_sec=5.0,
        footage_speed=1.0,
        footage=ClipEffect(type="clip", file="clip.mp4", fit="contain", rotation=0),
        **extra,
    )
    return scene, proj


def test_render_spec_filter_horror_emits_filter_layer():
    """horror 필터를 가진 씬 → RenderSpec.layers 에 FilterLayer 가 명시적으로 들어와야 한다.

    이전 회귀(#343): builder 가 scene.filters 를 layers 에 추가하지 않아 layers 길이가
    1 그대로 → isSimpleRenderSpec true → 클라 단순 미리보기가 horror 색감을 표현하지
    못함. 본 테스트는 builder 가 FilterLayer 를 만들고 layers 길이가 ≥2 가 되어
    isSimpleRenderSpec(layers.length !== 1 검사) 가 자동으로 false 판정하도록 한다.
    """
    from web.backend.models import SceneFilter
    from src.render_spec.builder import build_render_spec
    from src.render_spec.models import FilterLayer, VideoLayer

    scene, proj = _make_scene_with_clip(
        filters=[SceneFilter(filter_type="horror", intensity=1.0)],
    )
    spec = build_render_spec(scene, proj)
    layer_types = [layer.type for layer in spec.layers]

    # video → filter 순으로 정렬되어야 한다 (clip_editor 의 적용 순서 일치).
    assert layer_types == ["video", "filter"]
    assert isinstance(spec.layers[0], VideoLayer)
    assert isinstance(spec.layers[1], FilterLayer)
    assert spec.layers[1].filter_type == "horror"


def test_render_spec_motion_shake_emits_motion_layer():
    """shake 모션을 가진 씬 → MotionLayer 가 layers 에 들어와야 한다."""
    from web.backend.models import SceneMotion
    from src.render_spec.builder import build_render_spec
    from src.render_spec.models import MotionLayer, VideoLayer

    scene, proj = _make_scene_with_clip(
        motions=[SceneMotion(motion_type="shake", start_sec=0.0, duration_sec=2.0)],
    )
    spec = build_render_spec(scene, proj)
    layer_types = [layer.type for layer in spec.layers]

    assert layer_types == ["video", "motion"]
    assert isinstance(spec.layers[0], VideoLayer)
    assert isinstance(spec.layers[1], MotionLayer)
    assert spec.layers[1].motion_type == "shake"
    assert spec.layers[1].motion_duration_sec == 2.0


def test_render_spec_filter_and_motion_combined_layer_order():
    """filter + motion 동시 적용 시 layers 순서가 cut_clip → filter → motion 으로 유지.

    이 순서는 ``clip_editor`` 의 실제 vf 체인 적용 순서와 같다. 순서가 어긋나면 ffmpeg
    매퍼와 미세하게 다른 결과가 나올 가능성이 있어 회귀 가드를 둔다.
    """
    from web.backend.models import SceneFilter, SceneMotion
    from src.render_spec.builder import build_render_spec

    scene, proj = _make_scene_with_clip(
        filters=[SceneFilter(filter_type="vivid", intensity=0.8)],
        motions=[SceneMotion(motion_type="clip_zoom", start_sec=0.5, duration_sec=1.5)],
    )
    spec = build_render_spec(scene, proj)
    layer_types = [layer.type for layer in spec.layers]

    assert layer_types == ["video", "filter", "motion"]


def test_render_spec_empty_filter_motion_arrays_stay_simple():
    """filters=[] / motions=[] 인 단순 씬은 여전히 video 1개만 — 단순 모드 유지.

    빈 배열이 들어와도 layers 에 끼지 않아야 한다 (불필요한 NoOp 레이어 금지).
    """
    from src.render_spec.builder import build_render_spec

    scene, proj = _make_scene_with_clip(filters=[], motions=[])
    spec = build_render_spec(scene, proj)
    assert [layer.type for layer in spec.layers] == ["video"]


def test_render_spec_unknown_filter_type_is_skipped():
    """지원하지 않는 ``filter_type`` 은 layers 에서 스킵된다 — 다운스트림 매퍼 보호.

    Pydantic Literal 검증을 통과하지 못하므로 SceneFilter 인스턴스 자체를 만들 수 없지만,
    duck-typed 객체로 builder 에 흘러들어올 가능성을 고려해 가드를 둔다.
    """
    from src.render_spec.builder import build_render_spec

    class _FakeFilter:
        filter_type = "unknown_xxx"
        intensity = 1.0
        params: dict = {}

    scene, proj = _make_scene_with_clip()
    # SceneCard.filters 는 Pydantic Literal 검증이 있어 우회 setattr 로 주입.
    object.__setattr__(scene, "filters", [_FakeFilter()])

    spec = build_render_spec(scene, proj)
    # 알 수 없는 filter_type 은 None 으로 스킵되어 video 만 남는다.
    assert [layer.type for layer in spec.layers] == ["video"]


def test_render_spec_filter_motion_compatibility_with_find_footage_layer():
    """find_footage_layer 가 새 layer 타입 추가 후에도 VideoLayer 만 정확히 찾는다.

    ffmpeg 매퍼는 footage 1개만 사용하므로 filter/motion 이 끼어들어도 매퍼 출력이
    바뀌지 않아야 한다 (회귀 방지 가드).
    """
    from web.backend.models import SceneFilter, SceneMotion
    from src.render_spec.builder import build_render_spec
    from src.render_spec.ffmpeg_mapper import find_footage_layer, video_layer_to_vf
    from src.render_spec.models import VideoLayer

    scene_simple, proj = _make_scene_with_clip()
    scene_complex, _ = _make_scene_with_clip(
        filters=[SceneFilter(filter_type="horror", intensity=1.0)],
        motions=[SceneMotion(motion_type="shake", start_sec=0.0, duration_sec=2.0)],
    )

    spec_simple = build_render_spec(scene_simple, proj)
    spec_complex = build_render_spec(scene_complex, proj)

    f_simple = find_footage_layer(spec_simple)
    f_complex = find_footage_layer(spec_complex)
    assert isinstance(f_simple, VideoLayer)
    assert isinstance(f_complex, VideoLayer)
    # footage geometry 는 filter/motion 추가에도 영향받지 않는다.
    assert video_layer_to_vf(f_simple, spec_simple.canvas) == video_layer_to_vf(
        f_complex, spec_complex.canvas
    )
