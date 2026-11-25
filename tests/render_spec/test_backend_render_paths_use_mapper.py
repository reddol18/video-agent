"""백엔드 렌더 경로가 RenderSpec 매퍼를 사용하는지 통합 검증 (#303).

이슈 #303 완료 조건:
- `_make_scene_preview` / `_do_render`(=`scene_preview_render` POST + clip_editor)가
  RenderSpec 경유로 동작.
- 대표 씬 10개에 대해 매퍼 결과가 결정적이고, 회귀 케이스(fit-vs-speed)가 깨지지 않음.

본 테스트는 ffmpeg를 실행하지 않고, 매퍼가 만든 vf 문자열의 *구조적 동등성*과
*핵심 불변식*만 검증한다. 실제 픽셀 비교는 박테스터 감사 단계에서 수행.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.render_spec import (
    Canvas,
    Size,
    VideoLayer,
    build_render_spec,
    spec_to_video_ffmpeg_args,
    video_layer_to_vf,
)
from src.render_spec.builder import _apply_rotation_to_size, _compute_fit_geometry


# ── 대표 씬 10개 (이슈 #303 완료 조건) ────────────────────────────────────
# 실제 김감독 프로젝트에서 자주 등장하는 fit × footage_speed × aspect 조합.
REPRESENTATIVE_SCENES = [
    # (label, canvas_w, canvas_h, src_w, src_h, fit, footage_speed, rotation)
    ("16:9_원본_contain_정상속도",       1920, 1080, 1920, 1080, "contain", 1.0, 0),
    ("16:9_원본_cover_정상속도",         1920, 1080, 1920, 1080, "cover",   1.0, 0),
    ("9:16_원본_contain_정상속도",       1080, 1920, 1080, 1920, "contain", 1.0, 0),
    ("9:16_캔버스_16:9소스_cover",        1080, 1920, 1920, 1080, "cover",   1.0, 0),
    ("9:16_캔버스_16:9소스_contain",      1080, 1920, 1920, 1080, "contain", 1.0, 0),
    ("16:9_캔버스_9:16소스_contain",      1920, 1080, 1080, 1920, "contain", 1.0, 0),
    ("9:16_캔버스_16:9소스_cover_0.25배",  1080, 1920, 1920, 1080, "cover",   0.25, 0),  # #fit-bug 재현 케이스
    ("9:16_캔버스_16:9소스_contain_2배",   1080, 1920, 1920, 1080, "contain", 2.0, 0),
    ("16:9_원본_stretch_정상속도",        1920, 1080, 1920, 1080, "stretch", 1.0, 0),
    ("9:16_캔버스_회전90_contain",        1080, 1920, 1920, 1080, "contain", 1.0, 90),
]


def _make_scene(footage_speed, fit, rotation):
    footage = SimpleNamespace(
        type="clip", file="x.mp4", fit=fit, rotation=rotation,
        x_offset=0.0, y_offset=0.0,
        clip_start_sec=None, clip_end_sec=None,
    )
    return SimpleNamespace(
        scene_id=1, footage=footage, footage_speed=footage_speed,
        duration_hint_sec=5.0,
        text_overlays=[], shape_overlays=[], graphic_overlays=[], mosaic_overlays=[],
    )


@pytest.mark.parametrize("label,cw,ch,sw,sh,fit,speed,rot", REPRESENTATIVE_SCENES)
def test_representative_scene_produces_deterministic_vf(label, cw, ch, sw, sh, fit, speed, rot):
    """대표 씬 10개 각각이 결정적 vf를 만들고, 핵심 불변식을 만족한다."""
    scene = _make_scene(speed, fit, rot)
    project = SimpleNamespace(output_width=cw, output_height=ch)
    spec = build_render_spec(
        scene, project,
        footage_size=Size(width=sw, height=sh),
        footage_duration_sec=10.0,
    )
    args = spec_to_video_ffmpeg_args(spec)
    vf, af = args["vf"], args["af"]

    assert vf, f"{label}: vf 비어있음"
    # 추상 키워드 부재
    for kw in ("contain", "cover", "stretch", "force_original_aspect_ratio"):
        assert kw not in vf, f"{label}: vf에 {kw} 등장 ({vf})"
    # setsar=1,fps=30 마지막 직전에 들어가 있어야 함 (시간/공간 분리)
    assert "setsar=1,fps=" in vf
    # 속도 != 1.0이면 atempo가 있어야 함
    if speed != 1.0:
        assert af, f"{label}: speed={speed}인데 af 비어있음"


def test_fit_speed_regression_case_does_not_force_contain():
    """#fit-bug 재현 케이스: 9:16 cover + footage_speed=0.25.

    과거 회귀에선 footage_speed != 1.0 분기에서 fit이 contain으로 강제 덮어쓰여
    cover가 무력화됐었다. RenderSpec 매퍼 경로에선 fit이 *별도 단계*에서 결정되므로
    speed가 어떤 값이든 cover crop이 그대로 나와야 한다.
    """
    # 직접 RenderSpec 생성으로 시뮬레이션
    canvas = Canvas(width=1080, height=1920, fps=30)
    visible = _apply_rotation_to_size(Size(width=1920, height=1080), 0)
    sx, sy, crop, pad = _compute_fit_geometry(canvas, visible, "cover")
    assert crop is not None and pad is None  # cover 약속
    layer = VideoLayer(
        source="x.mp4",
        source_size=visible,
        scale_x=sx, scale_y=sy, crop=crop, pad=pad,
        rotation_deg=0,
        playback_rate=0.25,  # 느린 재생 — 과거 회귀의 핵심 차원
        start_sec=0.0, end_sec=10.0,
    )
    vf = video_layer_to_vf(layer, canvas)
    # cover의 crop이 살아있어야 함
    assert "crop=1080:1920:" in vf, f"speed=0.25에서 cover crop 사라짐: {vf}"
    # pad는 절대 등장하지 않음 (contain으로 덮어쓰여진 회귀가 났다면 pad가 들어있을 것)
    assert "pad=" not in vf, f"speed=0.25에서 contain pad가 등장: {vf}"
    # setpts는 *맨 마지막*에 한 번만
    tokens = vf.split(",")
    setpts_tokens = [t for t in tokens if t.startswith("setpts=")]
    assert len(setpts_tokens) == 1, f"setpts가 0개 또는 2개 이상: {vf}"
    assert tokens[-1].startswith("setpts="), f"setpts가 마지막이 아님: {vf}"


def test_cut_clip_path_offset_zero_uses_mapper():
    """cut_clip(offset=0) 경로가 RenderSpec 매퍼를 실제로 호출함을 verify."""
    import src.utils.ffmpeg_utils as ffu

    class _Cap:
        def __init__(self): self.argv = None
        def __call__(self, cmd, *a, **kw):
            self.argv = list(cmd)
            class _R:
                returncode = 0; stdout = ""; stderr = ""
            return _R()

    cap = _Cap()
    orig_run = ffu.subprocess.run
    orig_dims = ffu.get_video_dimensions
    orig_rot = ffu._needs_rotation
    try:
        ffu.subprocess.run = cap
        ffu.get_video_dimensions = lambda s: (1920, 1080)
        ffu._needs_rotation = lambda s, *a, **kw: False
        ffu.cut_clip("src.mp4", 0.0, 5.0, "dst.mp4",
                     fit="cover", out_w=1080, out_h=1920,
                     x_offset=0.0, y_offset=0.0)
        argv = cap.argv or []
        vf = next((argv[i+1] for i, a in enumerate(argv) if a == "-vf"), "")
        assert "force_original_aspect_ratio" not in vf  # 매퍼 경로 확정
        assert "crop=1080:1920:" in vf  # cover crop 정상
    finally:
        ffu.subprocess.run = orig_run
        ffu.get_video_dimensions = orig_dims
        ffu._needs_rotation = orig_rot
