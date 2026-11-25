"""카운트다운 오버레이의 그라디언트/글로우 렌더 경로 검증.

카운트다운은 시간대별로 텍스트가 바뀌어 PNG 1장으로 표현할 수 없기 때문에,
예전에는 그라디언트/글로우가 설정돼 있어도 무조건 drawtext(simple) 경로로 보내졌고
그 결과 스타일이 조용히 무시되고 흰색 단색으로 렌더됐다.

지금은 그라디언트/글로우가 있는 카운트다운만 숫자별 오버레이로 펼쳐
PNG 합성(complex) 경로로 보낸다. 스타일이 없는 카운트다운은 기존 drawtext 경로 유지.
"""

from src.clip_editor import _expand_countdown_overlay, _build_text_drawtext_parts


GRADIENT = {"stops": [{"color": "#feba01", "pos": 0.0}, {"color": "#ffffff", "pos": 1.0}], "angle": 90}


def _countdown(**extra):
    ov = {
        "text": "",
        "countdown_from": 5,
        "countdown_to": 1,
        "countdown_interval_sec": 1.0,
        "countdown_end_text": "액션!",
        "start_sec": 2.0,
        "end_sec": 8.0,
        "font_size": 80,
    }
    ov.update(extra)
    return ov


def _classify(overlays, clip_duration=10.0):
    """apply_text_overlays 의 분류 로직만 떼어낸 복제본.

    ffmpeg 실행 없이 "어떤 오버레이가 어느 경로로 가는지" 만 검증한다.
    """
    simple, complex_ = [], []
    for ov in overlays:
        gradient = ov.get("gradient")
        glow_radius = int(ov.get("glow_radius", 0))
        is_countdown = int(ov.get("countdown_from", 0) or 0) > 0
        is_pulse = str(ov.get("loop_animation") or "none").lower() == "pulse"
        needs_complex = (gradient and gradient.get("stops")) or glow_radius > 0 or is_pulse
        if not needs_complex:
            simple.append(ov)
        elif is_countdown:
            complex_.extend(_expand_countdown_overlay(ov, clip_duration))
        else:
            complex_.append(ov)
    return simple, complex_


def test_countdown_without_style_stays_on_drawtext():
    """그라디언트/글로우 없는 카운트다운은 기존 drawtext 경로 유지 (PNG 생성 회피)."""
    ov = _countdown()
    simple, complex_ = _classify([ov])
    assert simple == [ov]
    assert complex_ == []
    # drawtext 경로가 숫자 5개 + 종료 문구 1개를 그린다.
    parts = _build_text_drawtext_parts(ov, 10.0)
    assert len(parts) == 6


def test_countdown_with_gradient_expands_to_png_path():
    """그라디언트가 있으면 숫자마다 PNG 오버레이로 펼쳐진다 (종료 문구 포함)."""
    ov = _countdown(gradient=GRADIENT, glow_color="#ffae00", glow_radius=15)
    simple, complex_ = _classify([ov])
    assert simple == []
    # 5,4,3,2,1 + "액션!" = 6건
    assert [c["text"] for c in complex_] == ["5", "4", "3", "2", "1", "액션!"]
    # 각 건이 자기 구간을 갖고, 스타일은 원본과 동일하게 승계된다.
    assert [c["start_sec"] for c in complex_] == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    for c in complex_:
        assert c["gradient"] == GRADIENT
        assert c["glow_radius"] == 15
        assert c["font_size"] == 80
        # 재귀 방지 + 숫자 전환마다 페이드가 겹쳐 깜빡이지 않도록 진입/종료 효과 제거
        assert c["countdown_from"] == 0
        assert c["enter_effect"] == "none"
        assert c["exit_effect"] == "none"
    # 종료 문구는 오버레이 종료 시각까지 유지
    assert complex_[-1]["end_sec"] == 8.0


def test_countdown_with_glow_only_expands():
    """글로우만 있어도 PNG 경로로 펼쳐진다."""
    _, complex_ = _classify([_countdown(glow_radius=15, gradient=None, countdown_end_text="")])
    assert [c["text"] for c in complex_] == ["5", "4", "3", "2", "1"]


def test_expand_respects_number_cap_and_end_sec_clamp():
    """숫자 상한(120) 초과 방지 + end_sec 로 구간이 잘린다."""
    steps = _expand_countdown_overlay(
        {"countdown_from": 500, "countdown_to": 1, "countdown_interval_sec": 1.0,
         "start_sec": 0.0, "end_sec": None, "gradient": GRADIENT},
        clip_duration=600.0,
    )
    assert len(steps) == 120

    # end_sec 이 3.5 면 5→4→3 구간만 남고 마지막은 3.5 에서 잘린다.
    clamped = _expand_countdown_overlay(_countdown(end_sec=3.5, countdown_end_text=""), 10.0)
    assert [c["text"] for c in clamped] == ["5", "4"]
    assert clamped[-1]["end_sec"] == 3.5


def test_non_countdown_overlay_unchanged():
    """카운트다운이 아니면 그대로 통과한다 (회귀 방지)."""
    ov = {"text": "안녕", "gradient": GRADIENT, "start_sec": 0.0, "end_sec": 3.0}
    assert _expand_countdown_overlay(ov, 10.0) == [ov]
    simple, complex_ = _classify([ov])
    assert complex_ == [ov] and simple == []
