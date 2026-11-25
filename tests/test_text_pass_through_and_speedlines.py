"""텍스트 오버레이 관통(pass-through) 효과 + 텍스트 마스킹 집중선 테스트.

- 관통: 한쪽 화면 밖에서 들어와 "반대편" 화면 밖으로 빠져나간다.
- 집중선: loop_animation="speedlines" 일 때 글자 알파로 마스킹된 집중선 레이어.
"""

import numpy as np
import pytest

from src.clip_editor import (
    _PASS_THROUGH_EFFECTS,
    _build_pass_through_exprs,
    _build_text_alpha_mask,
    _build_text_drawtext_parts,
    _expand_text_speedlines_overlays,
    _get_overlay_effect_config,
    _render_text_masked_speed_lines_layer,
)


# ── 관통(pass-through) ────────────────────────────────────────────────────


def test_pass_through_auto_pairs_exit_direction():
    """enter 에만 관통을 지정하면 exit 도 같은 관통으로 자동 연결된다."""
    ov = {"enter_effect": "pass_left_to_right", "exit_effect": "fade"}
    enter, exit_, _, _ = _get_overlay_effect_config(ov, text_supported=True)
    assert enter == "pass_left_to_right"
    assert exit_ == "pass_left_to_right"


def test_pass_through_is_valid_for_text():
    """관통 효과가 텍스트에서 fade 로 폴백되지 않는다 (회귀: 예전엔 이미지 전용)."""
    for eff in _PASS_THROUGH_EFFECTS:
        enter, _, _, _ = _get_overlay_effect_config(
            {"enter_effect": eff}, text_supported=True
        )
        assert enter == eff


def test_pass_through_expr_enters_from_one_side_exits_other():
    """x 표현식이 진입은 음(-) 오프셋, 퇴장은 양(+) 오프셋을 갖는다 (왼→오)."""
    ov = {
        "start_sec": 1.0,
        "end_sec": 4.0,
        "enter_effect": "pass_left_to_right",
        "enter_duration_sec": 0.5,
        "exit_duration_sec": 0.5,
    }
    x_expr, y_expr = _build_pass_through_exprs(ov, "BX", "BY", 5.0, "pass_left_to_right")
    # 진입: 왼쪽(-), 퇴장: 오른쪽(+)
    assert "-1.0000*(w*1.1)" in x_expr
    assert "+1.0000*(w*1.1)" in x_expr
    # 세로축은 건드리지 않는다.
    assert y_expr == "BY"


def test_pass_through_vertical_uses_height_axis():
    ov = {"start_sec": 0.0, "end_sec": 3.0, "enter_effect": "pass_top_to_bottom"}
    x_expr, y_expr = _build_pass_through_exprs(ov, "BX", "BY", 3.0, "pass_top_to_bottom")
    assert x_expr == "BX"
    assert "(h*1.1)" in y_expr


def test_pass_through_drawtext_quotes_expression():
    """drawtext x/y 는 따옴표로 감싸야 한다 — 표현식의 콤마가 필터 구분자로 오인되지 않도록."""
    ov = {
        "text": "테스트",
        "start_sec": 0.5,
        "end_sec": 2.5,
        "enter_effect": "pass_right_to_left",
        "font_size": 40,
    }
    parts = _build_text_drawtext_parts(ov, 3.0)
    assert len(parts) == 1
    assert ":x='" in parts[0] and ":y='" in parts[0]


# ── 텍스트 마스킹 집중선 ──────────────────────────────────────────────────


def _text_ov(**kw):
    ov = {
        "text": "나온다",
        "font_size": 120,
        "x": 0.5,
        "y": 0.5,
        "color": "#ffdd00",
        "font_family": "BMDOHYEON",
        "loop_animation": "speedlines",
    }
    ov.update(kw)
    return ov


def test_speed_lines_never_leak_outside_glyphs():
    """집중선은 글자 알파 밖으로 단 1픽셀도 새지 않아야 한다 (마스킹의 핵심)."""
    W, H = 480, 320
    ov = _text_ov()
    built = _build_text_alpha_mask(ov, W, H)
    assert built is not None
    mask = np.array(built[0])

    layer = _render_text_masked_speed_lines_layer(ov, W, H, 255, phase_bucket=None)
    assert layer is not None
    alpha = np.array(layer)[:, :, 3]
    # 마스크가 완전히 0 인 픽셀에는 집중선이 있으면 안 된다.
    assert int((alpha[mask == 0] > 0).sum()) == 0


def test_speed_lines_actually_fill_glyphs():
    """글자 안쪽에는 집중선이 실제로 그려져야 한다 (inner_ratio 로 텅 비면 회귀)."""
    W, H = 480, 320
    ov = _text_ov()
    mask = np.array(_build_text_alpha_mask(ov, W, H)[0])
    layer = _render_text_masked_speed_lines_layer(ov, W, H, 255, phase_bucket=None)
    alpha = np.array(layer)[:, :, 3]
    inside = alpha[mask > 128]
    assert inside.size > 0
    assert (inside > 0).sum() > 0


def test_speed_lines_cover_full_text_width_when_wide():
    """가로로 긴 문구도 양끝까지 집중선이 들어가야 한다.

    회귀 방지: 한 점에서 방사하면 양끝 글자가 거의 수평선이 되어 뭉개졌다. 외곽선 기반
    으로 바꾼 뒤에는 좌·중·우 세 구간 모두에 선이 고르게 들어간다.
    """
    W, H = 720, 480
    ov = _text_ov(text="대박사건이다!!!", font_size=110)
    mask = np.array(_build_text_alpha_mask(ov, W, H)[0])
    layer = _render_text_masked_speed_lines_layer(ov, W, H, 255, phase_bucket=None)
    alpha = np.array(layer)[:, :, 3]

    xs = np.nonzero(mask.max(axis=0) > 128)[0]
    x0, x1 = int(xs.min()), int(xs.max())
    thirds = np.linspace(x0, x1, 4).astype(int)
    for a, b in zip(thirds[:-1], thirds[1:]):
        seg_mask = mask[:, a:b] > 128
        seg_alpha = alpha[:, a:b]
        # 각 구간에 선이 "들어가 있기는" 해야 한다. 비율 하한을 낮게 잡는 이유:
        # 예전엔 30% 이상을 요구했는데, 그 수준으로 채우면 획이 갉여 글자가 깨져
        # 보인다는 실사용 피드백이 있었다(사용자: "글자가 그냥 깨진 것처럼 보인다").
        # 집중선은 가장자리 질감이지 글자를 채우는 무늬가 아니다.
        assert seg_mask.sum() > 0
        assert (seg_alpha[seg_mask] > 0).sum() / seg_mask.sum() > 0.03


def test_speed_lines_do_not_shred_glyphs():
    """집중선이 글자를 삼켜서는 안 된다 — 획 대부분은 원래 색으로 남아야 한다.

    회귀 방지: 획 안쪽으로 깊이 파고들면 반대편 선과 가운데서 만나 글자가 깨진 것처럼
    읽힌다. 선이 덮는 비율에 상한을 둬서 "글자가 먼저 읽히는" 상태를 보장한다.
    """
    W, H = 1080, 1920
    ov = _text_ov(text="궁예전", font_size=200)
    mask = np.array(_build_text_alpha_mask(ov, W, H)[0])
    alpha = np.array(_render_text_masked_speed_lines_layer(ov, W, H, 255, phase_bucket=None))[:, :, 3]
    inside = mask > 128
    covered = (alpha[inside] > 0).sum() / inside.sum()
    assert covered < 0.35, f"집중선이 글자의 {covered:.0%} 를 덮었다 — 글자가 깨져 보인다"


def test_speed_lines_buckets_differ():
    """버킷마다 다른 선이 그려져야 애니메이션(깜빡임)이 생긴다."""
    W, H = 1080, 1920
    ov = _text_ov(text="궁예전", font_size=200)
    a0 = np.array(_render_text_masked_speed_lines_layer(ov, W, H, 255, phase_bucket=0))[:, :, 3]
    a1 = np.array(_render_text_masked_speed_lines_layer(ov, W, H, 255, phase_bucket=1))[:, :, 3]
    assert not np.array_equal(a0, a1)


# ── 진행 방향 꼬리(tail) ──────────────────────────────────────────────────


def test_speed_tail_never_covers_glyphs():
    """꼬리는 글자 '밖'에만 그려야 한다 — 획을 덮으면 가독성이 무너진다."""
    from src.clip_editor import _render_text_speed_tail_layer

    W, H = 1080, 1920
    ov = _text_ov(text="궁예전", font_size=200)
    mask = np.array(_build_text_alpha_mask(ov, W, H)[0])
    for direction in [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)]:
        layer = _render_text_speed_tail_layer(ov, W, H, 255, direction=direction)
        assert layer is not None
        alpha = np.array(layer)[:, :, 3]
        assert int((alpha[mask > 128] > 0).sum()) == 0, f"{direction} 에서 글자를 덮었다"
        assert int((alpha > 0).sum()) > 0, f"{direction} 에서 꼬리가 안 그려졌다"


def test_speed_tail_extends_toward_given_direction():
    """꼬리는 지정한 방향(=날아온 쪽)으로 뻗어야 한다."""
    from src.clip_editor import _render_text_speed_tail_layer

    W, H = 1080, 1920
    ov = _text_ov(text="궁예전", font_size=200)
    mask = np.array(_build_text_alpha_mask(ov, W, H)[0])
    gx1 = int(np.nonzero(mask.max(axis=0) > 128)[0].max())
    gx0 = int(np.nonzero(mask.max(axis=0) > 128)[0].min())

    right = np.array(_render_text_speed_tail_layer(ov, W, H, 255, direction=(1.0, 0.0)))[:, :, 3]
    left = np.array(_render_text_speed_tail_layer(ov, W, H, 255, direction=(-1.0, 0.0)))[:, :, 3]
    # 오른쪽 꼬리는 글자 오른쪽 끝 바깥에, 왼쪽 꼬리는 왼쪽 끝 바깥에 픽셀이 있어야 한다.
    assert (right[:, gx1 + 5:] > 0).sum() > 0
    assert (right[:, :gx0 - 5] > 0).sum() == 0
    assert (left[:, :gx0 - 5] > 0).sum() > 0
    assert (left[:, gx1 + 5:] > 0).sum() == 0


def test_tail_mode_is_static_by_default():
    """tail 은 기본이 정적 1장이어야 한다 (렌더 시간 회귀 방지).

    꼬리는 모양이 고정이라 깜빡일 이유가 없다. 그런데 버킷 분할을 타면 표시 구간이
    수십 조각으로 쪼개져 조각마다 풀해상도 PNG 를 새로 그린다(장당 약 0.28초).
    실측: 오버레이 7개 → 292 조각, PNG 생성만 80초. 27초 영상이 15분 넘게 걸렸다.
    """
    ov = _text_ov(
        id="t1", start_sec=0.5, end_sec=2.5,
        enter_effect="pass_left_to_right",
        text_speed_lines={"mode": "tail"},
    )
    out = _expand_text_speedlines_overlays([ov], 5.0)
    assert len(out) == 1, f"tail 이 {len(out)}조각으로 쪼개졌다 — 렌더가 느려진다"
    assert out[0]["_speedlines_bucket"] is None


def test_expand_speedlines_splits_into_buckets():
    # inner 모드는 선이 깜빡여야 하므로 버킷 분할을 유지한다.
    ov = _text_ov(
        id="t1", start_sec=0.5, end_sec=2.5,
        text_speed_lines={"mode": "inner"},
    )
    out = _expand_text_speedlines_overlays([ov], 5.0)
    assert len(out) > 1
    # 버킷이 순환하고, 조각마다 페이드가 켜지지 않는다.
    assert {o["_speedlines_bucket"] for o in out} == set(range(8))
    assert all(o["enter_effect"] == "none" for o in out)
    # 구간이 원본 범위를 벗어나지 않는다.
    assert out[0]["start_sec"] >= 0.5
    assert out[-1]["end_sec"] <= 2.5


def test_expand_speedlines_static_when_animate_false():
    ov = _text_ov(id="t1", start_sec=0.0, end_sec=2.0, text_speed_lines={"animate": False})
    out = _expand_text_speedlines_overlays([ov], 3.0)
    assert len(out) == 1
    assert out[0]["_speedlines_bucket"] is None


def test_expand_speedlines_leaves_other_overlays_untouched():
    plain = {"id": "p", "text": "x", "loop_animation": "bounce"}
    assert _expand_text_speedlines_overlays([plain], 5.0) == [plain]


def test_expand_speedlines_preserves_pass_through_trajectory():
    """조각으로 나뉘어도 관통 궤적은 오버레이 전체 구간 기준으로 보존된다."""
    ov = _text_ov(
        id="t1", start_sec=0.2, end_sec=2.8, enter_effect="pass_left_to_right",
        text_speed_lines={"mode": "inner"},   # 조각으로 나뉘는 모드에서 검증
    )
    out = _expand_text_speedlines_overlays([ov], 3.0)
    assert all(o["_pass_through_effect"] == "pass_left_to_right" for o in out)
    assert all(o["_pass_through_start"] == pytest.approx(0.2) for o in out)
    assert all(o["_pass_through_end"] == pytest.approx(2.8) for o in out)


# ── drawtext 이스케이프 ───────────────────────────────────────────────────


def test_apostrophe_does_not_break_filtergraph():
    """작은따옴표가 든 자막이 필터그래프를 깨뜨리지 않아야 한다.

    회귀: `'` 를 `\'` 로 이스케이프하면 ffmpeg 필터그래프 파서가 그 지점에서 문자열이
    끝난 것으로 읽어, 뒤따르는 `enable='between(t,3.2,3.3)'` 의 콤마를 필터 구분자로
    오인한다("No such filter: '3.3176'"). 그 결과 그 씬의 drawtext 자막이 통째로
    사라졌다. 올바른 관용구는 `'\''`.
    """
    from src.clip_editor import _escape_drawtext

    out = _escape_drawtext("진성왕을 '진성여왕'이라 쓰지 않습니다")
    # 문자열을 닫고-리터럴 따옴표-다시 여는 관용구여야 한다.
    assert "'\\''" in out
    # 파서가 해석하지 않는 백슬래시+따옴표 형태가 남아 있으면 안 된다.
    assert "\\'" not in out.replace("'\\''", "")


def test_typing_overlay_with_apostrophe_keeps_enable_intact():
    """작은따옴표 자막의 typing 청크에서도 enable 구간이 온전해야 한다."""
    ov = {
        "id": "q",
        "text": "'따옴표' 테스트",
        "x": 0.5,
        "y": 0.8,
        "font_size": 30,
        "font_family": "NotoSansKR",
        "color": "#ffffff",
        "enter_effect": "typing",
        "exit_effect": "fade",
        "enter_duration_sec": 0.8,
        "start_sec": 1.0,
        "end_sec": 3.0,
    }
    parts = _build_text_drawtext_parts(ov, 4.0)
    assert parts
    # 모든 청크가 enable 절을 온전히 갖는다 (콤마가 필터 구분자로 새지 않았다는 뜻).
    import re

    for p in parts:
        assert p.count(":enable='between(t,") == 1
        assert re.search(r":enable='between\(t,[\d.]+,[\d.]+\)'$", p), p


def test_transform_supports_text_pass_through():
    """미리보기 경로가 텍스트 관통을 오프셋으로 계산할 수 있어야 한다.

    회귀: 미리보기는 오버레이당 PNG 1장을 시간 구간에 합성하는 구조라, 관통을
    프레임별 오프셋으로 직접 계산하지 않으면 텍스트가 제자리에 붙어 있었다.
    (사용자 보고: "날아오는 효과가 안 된다" — 최종 렌더는 정상, 미리보기만 정지.)
    """
    from src.clip_editor import _compute_image_overlay_transform as cot

    ov = {
        "text": "테스트",
        "start_sec": 0.2,
        "end_sec": 3.0,
        "enter_effect": "pass_left_to_right",
        "exit_effect": "pass_left_to_right",
        "enter_duration_sec": 0.45,
        "exit_duration_sec": 0.45,
    }
    W, H = 1080, 1920
    # 진입 시작 직후엔 왼쪽 화면 밖(음수 오프셋), 정착 후엔 0.
    dx_start, _, _, _ = cot(ov, 0.22, W, H, visual_clip_duration=4.0, text_supported=True)
    dx_settled, _, _, _ = cot(ov, 1.5, W, H, visual_clip_duration=4.0, text_supported=True)
    dx_exit, _, _, _ = cot(ov, 2.9, W, H, visual_clip_duration=4.0, text_supported=True)
    assert dx_start < -100, f"진입 시 왼쪽 밖이어야 하는데 dx={dx_start}"
    assert abs(dx_settled) < 1.0, f"정착 구간은 오프셋 0이어야 하는데 dx={dx_settled}"
    assert dx_exit > 100, f"퇴장 시 오른쪽 밖이어야 하는데 dx={dx_exit}"


# ── 관통 + 루프 애니메이션 동시 지정 (미리보기 회귀) ──────────────────────────


def test_pass_through_offset_survives_loop_animation_in_preview():
    """관통(pass_*)과 루프 애니메이션을 함께 쓴 오버레이가 미리보기에서 실제로 날아와야 한다.

    회귀 내용: 미리보기 합성 루프(`_apply_text_overlays_preview`)가 관통 오프셋을
    ox/oy 에 더한 직후, loop_animation 분기가 `=` 대입으로 그 값을 덮어썼다.
    그 결과 pulse/shake/wave/bounce 를 같이 지정한 텍스트는 미리보기에서만
    제자리에 붙어 "관통이 적용되지 않는" 것처럼 보였다.
    (최종 렌더는 ffmpeg 표현식을 누적해 정상 동작 — 미리보기 전용 회귀였다.)

    여기서는 미리보기가 쓰는 것과 같은 오프셋 계산 + 같은 누적 규칙을 재현해,
    t=0 에서 글자가 화면 밖(폭의 상당 부분)에 있는지 확인한다.
    """
    import math

    from src.clip_editor import _compute_image_overlay_transform

    W, H = 1280, 720
    clip_dur = 2.5
    ov = {
        "text": "관통 테스트",
        "x": 0.5,
        "y": 0.2,
        "font_size": 107,
        "start_sec": 0.0,
        "end_sec": None,
        "enter_effect": "pass_right_to_left",
        "exit_effect": "pass_left_to_right",
        "enter_duration_sec": 0.35,
        "exit_duration_sec": 0.3,
        "loop_animation": "pulse",
        "loop_amplitude": 0.02,
        "loop_period_sec": 0.8,
    }

    def preview_ox(t: float) -> int:
        """미리보기 합성 루프와 동일한 순서로 ox 를 계산한다."""
        ox = 0
        dx, _dy, _sc, _al = _compute_image_overlay_transform(
            ov, t, W, H, visual_clip_duration=clip_dur, text_supported=True,
        )
        ox += int(round(dx))
        # pulse 는 리사이즈 중심 보정을 "누적"해야 한다 (대입이면 관통이 지워진다).
        amp = float(ov["loop_amplitude"])
        period = float(ov["loop_period_sec"])
        factor = 1.0 + amp * math.sin(t * 2 * math.pi / period)
        if abs(factor - 1.0) > 0.001:
            new_w = max(1, int(W * factor))
            ox += (W - new_w) // 2
        return ox

    # 등장 시작 — 오른쪽 화면 밖에 있어야 한다(폭의 절반 이상 바깥).
    assert preview_ox(0.0) > W * 0.5, "t=0 에서 글자가 화면 밖에서 시작해야 한다"
    # 등장 중 — 안쪽으로 이동하지만 아직 제자리가 아니다.
    assert preview_ox(0.0) > preview_ox(0.1) > preview_ox(0.2), "관통은 단조 감소로 진입해야 한다"
    # 등장 완료 — 거의 제자리(펄스 보정분만 남는다).
    assert abs(preview_ox(0.35)) < W * 0.05, "enter_duration 이후에는 제자리에 있어야 한다"
    assert abs(preview_ox(1.0)) < W * 0.05
    # 퇴장 — 화면 밖으로 빠져나간다.
    # 이 오버레이는 exit_effect 를 pass_left_to_right 로 명시했으므로 오른쪽(+x)으로 나간다.
    # (enter 만 지정했다면 자동 페어링으로 왼쪽(-x)으로 나갔을 것이다.)
    assert preview_ox(2.45) > W * 0.3, "퇴장 시 화면 밖으로 나가야 한다"
    assert abs(preview_ox(2.45)) > abs(preview_ox(2.3)), "퇴장은 갈수록 멀어져야 한다"


def test_pass_through_offset_survives_shake_and_wave():
    """shake/wave 도 관통 오프셋 위에 누적되어야 한다(대입이면 관통이 사라진다)."""
    import math

    from src.clip_editor import _compute_image_overlay_transform

    W, H = 1280, 720
    for anim in ("shake", "wave"):
        ov = {
            "text": "관통",
            "x": 0.5, "y": 0.5, "font_size": 80,
            "start_sec": 0.0, "end_sec": None,
            "enter_effect": "pass_right_to_left",
            "enter_duration_sec": 0.35, "exit_duration_sec": 0.3,
            "loop_animation": anim, "loop_amplitude": 0.02, "loop_period_sec": 0.8,
        }
        dx, _dy, _sc, _al = _compute_image_overlay_transform(
            ov, 0.0, W, H, visual_clip_duration=2.5, text_supported=True,
        )
        ox = int(round(dx))
        # 루프 오프셋은 관통 대비 훨씬 작다 — 누적해도 화면 밖 판정이 유지돼야 한다.
        if anim == "shake":
            ox += int(math.sin(0.0) * (80 * 0.02))
        else:
            ox += int(math.sin(0.0) * (W * 0.02))
        assert ox > W * 0.5, f"{anim}: 관통 오프셋이 유지돼야 한다"
