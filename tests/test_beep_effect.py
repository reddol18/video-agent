"""삐 소리 씬 단위 자동 효과 — 타이밍 계산 단위 테스트.

대상 함수:
- `src.beep_effect.compute_beep_start_sec(scene, scene_abs_start) -> float | None`

설계 의도 검증:
- 비프는 마지막 TTS 끝에 *맞춰 같이 끝난다* — 시작 = (마지막 segment 끝) - (비프 길이).
  즉 비프 길이만큼 나레이션 끝부분을 덮는다(방송 검열음 연출).
- "마지막 segment" 는 offset 순서가 아니라 끝점(offset+duration) 최대 기준.
- 활성 segment 가 없으면 None.
- tts_speed 로 segment 길이가 스케일링되어 끝점에 반영.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.beep_effect import compute_beep_start_sec, ensure_beep_wav


def _make_scene(segs: list, tts_speed: float = 1.0):
    from web.backend.models import SceneCard, NarrationSegment
    return SceneCard(
        scene_id=1,
        tts_speed=tts_speed,
        narration_segments=[NarrationSegment(**s) for s in segs],
        beep_on_narration_end=True,
    )


# 타이밍 계산 자체를 검증하므로 임의 값(0.4)을 명시해 기본값 변경과 분리한다.
BEEP = 0.4


def test_single_segment_beep_start():
    # seg_0: offset 0, dur 3.0 → 끝점 3.0. scene_abs_start=10.0.
    # 비프는 13.0 에 *끝나야* 하므로 시작 = 13.0 - 0.4 = 12.6.
    scene = _make_scene([{"id": "seg_0", "text": "안녕", "offset_sec": 0.0, "duration_sec": 3.0}])
    assert compute_beep_start_sec(scene, 10.0, BEEP) == pytest.approx(12.6)


def test_beep_ends_with_narration():
    """핵심 회귀: 비프 끝 == 나레이션 끝. (씬 끝에서 따로 울리면 연출이 죽는다.)"""
    scene = _make_scene([{"id": "seg_0", "text": "a", "offset_sec": 0.0, "duration_sec": 5.0}])
    start = compute_beep_start_sec(scene, 2.0, BEEP)
    narration_end = 2.0 + 5.0
    assert start + BEEP == pytest.approx(narration_end)
    assert start < narration_end, "비프가 나레이션과 겹쳐야 한다"


def test_last_segment_by_endpoint_not_offset():
    # seg_1 이 offset 은 더 작지만(1.0) 끝점(1.0+5.0=6.0)이 seg_0(0+2=2) 보다 크다.
    # 끝점 기준으로 6.0 이 마지막 → 비프 시작 = 6.0 - 0.4.
    scene = _make_scene([
        {"id": "seg_0", "text": "a", "offset_sec": 0.0, "duration_sec": 2.0},
        {"id": "seg_1", "text": "b", "offset_sec": 1.0, "duration_sec": 5.0},
    ])
    assert compute_beep_start_sec(scene, 0.0, BEEP) == pytest.approx(5.6)


def test_tts_speed_scales_duration():
    # dur 4.0 이 speed 2.0 이면 실제 오디오 2.0s → 끝점 2.0. 비프 시작 = 5.0+2.0-0.4.
    scene = _make_scene(
        [{"id": "seg_0", "text": "a", "offset_sec": 0.0, "duration_sec": 4.0}],
        tts_speed=2.0,
    )
    assert compute_beep_start_sec(scene, 5.0, BEEP) == pytest.approx(6.6)


def test_empty_text_and_zero_duration_ignored():
    # 빈 텍스트 / duration 0 은 활성 아님. 남은 활성 segment 만으로 끝점 계산.
    scene = _make_scene([
        {"id": "seg_0", "text": "   ", "offset_sec": 0.0, "duration_sec": 3.0},
        {"id": "seg_1", "text": "b", "offset_sec": 2.0, "duration_sec": 0.0},
        {"id": "seg_2", "text": "c", "offset_sec": 1.0, "duration_sec": 2.0},
    ])
    # 활성: seg_2 만 (끝점 3.0). 비프 시작 = 3.0 - 0.4.
    assert compute_beep_start_sec(scene, 0.0, BEEP) == pytest.approx(2.6)


def test_short_narration_does_not_precede_scene():
    """TTS 가 비프보다 짧아도 앞 씬을 침범하지 않는다(씬 상대 0 하한)."""
    scene = _make_scene([{"id": "seg_0", "text": "짧", "offset_sec": 0.0, "duration_sec": 0.2}])
    assert compute_beep_start_sec(scene, 10.0, BEEP) == pytest.approx(10.0)


def test_no_active_segment_returns_none():
    scene = _make_scene([{"id": "seg_0", "text": "", "offset_sec": 0.0, "duration_sec": 0.0}])
    assert compute_beep_start_sec(scene, 5.0) is None


def test_ensure_beep_wav_caches(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, *_a, **_kw):
        calls.append(list(args))
        p = Path(args[-1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = tmp_path / "_beep.wav"
    r1 = ensure_beep_wav(out, freq_hz=1000, duration_sec=0.4)
    assert r1 == out and out.exists()
    # sine 소스가 ffmpeg 인자에 포함되어야 한다.
    joined = " ".join(calls[0])
    assert "sine=frequency=1000" in joined
    # 두 번째 호출은 캐시 사용 → ffmpeg 재호출 없음.
    r2 = ensure_beep_wav(out, freq_hz=1000, duration_sec=0.4)
    assert r2 == out
    assert len(calls) == 1


def test_default_duration_covers_a_meaningful_tail():
    """기본 비프 길이가 '마지막 말을 가릴 만큼' 되는지 — 너무 짧으면 검열음 연출이 죽는다.

    0.4s 는 마지막 한 음절 정도만 걸려 약했다(사용자 지적). 기본값을 늘렸고,
    되돌아가는 회귀를 막기 위해 하한을 고정한다.
    """
    from src.beep_effect import DEFAULT_BEEP_DURATION_SEC

    assert DEFAULT_BEEP_DURATION_SEC >= 0.7, (
        f"기본 비프가 너무 짧다({DEFAULT_BEEP_DURATION_SEC}s) — 마지막 말이 안 가려진다"
    )

    scene = _make_scene([{"id": "seg_0", "text": "a", "offset_sec": 0.0, "duration_sec": 5.0}])
    start = compute_beep_start_sec(scene, 0.0, DEFAULT_BEEP_DURATION_SEC)
    # 기본값으로도 "끝이 맞물린다"는 규약은 그대로여야 한다.
    assert start + DEFAULT_BEEP_DURATION_SEC == pytest.approx(5.0)


def test_beep_mute_expression_silences_narration():
    """비프 구간에서 나레이션이 실제로 음소거되는지 — ffmpeg 표현식 검증.

    볼륨만 올리는 방식은 원리상 실패한다: amix(normalize=0)는 두 소리를 더할 뿐이라
    말소리 성분이 그대로 남는다(실측: 비프 0.7→말보다 3dB 작음, 2.0 으로 올려도
    말 성분 -24.1dB 불변). 그래서 가려야 할 구간의 나레이션 자체를 지운다.
    """
    import re
    import tempfile

    ff = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if ff.returncode != 0:  # pragma: no cover
        pytest.skip("ffmpeg 없음")

    d = Path(tempfile.mkdtemp())
    speech, beep, out = d / "sp.wav", d / "bp.wav", d / "o.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=300:duration=3", "-ac", "2", str(speech)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=1000:duration=0.8", "-ac", "2", str(beep)], check=True)

    a, b, ramp = 1.2, 2.0, 0.005
    expr = (f"1-min(1,between(t,{a:.3f},{b:.3f})"
            f"*min(1,min((t-{a:.3f}),({b:.3f}-t))/{ramp}))")
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(speech), "-i", str(beep),
         "-filter_complex",
         f"[0:a]volume=eval=frame:volume='{expr}'[n];"
         f"[1:a]volume=1.0,adelay=1200|1200[s];"
         f"[n][s]amix=inputs=2:normalize=0:duration=longest",
         str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"음소거 표현식이 ffmpeg 에서 실패: {r.stderr[:300]}"

    def speech_level(ss: float, dur: float) -> float:
        o = subprocess.run(
            ["ffmpeg", "-ss", str(ss), "-t", str(dur), "-i", str(out),
             "-af", "bandpass=f=300:width_type=h:w=60,volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        ).stderr
        m = re.search(r"mean_volume:\s*(-?[\d.]+)", o)
        assert m, "volumedetect 파싱 실패"
        return float(m.group(1))

    inside = speech_level(1.3, 0.6)   # 비프 구간 — 말이 지워져야 한다
    outside = speech_level(0.2, 0.7)  # 비프 밖 — 말이 그대로여야 한다
    assert inside < outside - 15, f"비프 구간 말소리가 안 지워졌다 (안 {inside:.1f} / 밖 {outside:.1f} dB)"
    assert outside > -35, f"비프 밖 나레이션까지 줄었다 ({outside:.1f} dB)"
