"""Typecast TTS provider 연동 유닛 테스트.

핵심 검증:
- resolve_provider 프리픽스 판별 (tc_/uc_ → typecast, ko-KR- → google, 빈값 → google)
- estimate_typecast_credits 무료 한도 차감 로직
- typecast_provider.synthesize: 바이너리 응답 → dst 저장 → pydub 길이 측정 (HTTP mock)

실제 HTTP 호출/실제 합성은 하지 않는다(크레딧 소모 방지).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pydub import AudioSegment

from src.tts.base import resolve_provider
from src.cost_estimator import estimate_typecast_credits


# ── resolve_provider ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "voice,expected",
    [
        ("tc_62a8975e695 ", "typecast"),
        ("tc_abc123", "typecast"),
        ("uc_deadbeef", "typecast"),
        ("TC_UPPER", "typecast"),
        ("UC_UPPER", "typecast"),
        ("ko-KR-Chirp3-HD-Orus", "google"),
        ("ko-KR-Neural2-A", "google"),
        ("", "google"),
        (None, "google"),
        ("   ", "google"),
        ("random-voice", "google"),
    ],
)
def test_resolve_provider(voice, expected):
    assert resolve_provider(voice) == expected


# ── estimate_typecast_credits — 무료 한도 차감 ─────────────────────────────

def test_typecast_credits_under_free_limit():
    # 무료 한도(30k) 이하 → 청구 0, 무료 커버.
    info = estimate_typecast_credits(chars=10_000)
    assert info["credits"] == 10_000
    assert info["billable_credits"] == 0
    assert info["free_covered"] is True
    assert info["usd"] == 0.0


def test_typecast_credits_exactly_at_free_limit():
    info = estimate_typecast_credits(chars=30_000)
    assert info["billable_credits"] == 0
    assert info["free_covered"] is True
    assert info["usd"] == 0.0


def test_typecast_credits_over_free_limit():
    # 40k 글자 → 무료 30k 차감 후 10k 청구. 10k/1000 * 0.075 = 0.75
    info = estimate_typecast_credits(chars=40_000)
    assert info["credits"] == 40_000
    assert info["billable_credits"] == 10_000
    assert info["free_covered"] is False
    assert info["usd"] == pytest.approx(0.75)


def test_typecast_credits_with_used_free_this_month():
    # 이미 25k 무료 소진 → 남은 무료 5k. 10k 요청 중 5k 청구.
    info = estimate_typecast_credits(chars=10_000, used_free_this_month=25_000)
    assert info["billable_credits"] == 5_000
    assert info["free_covered"] is False
    assert info["usd"] == pytest.approx(5_000 / 1000 * 0.075)


def test_typecast_credits_zero_chars():
    info = estimate_typecast_credits(chars=0)
    assert info["credits"] == 0
    assert info["billable_credits"] == 0
    assert info["free_covered"] is True
    assert info["usd"] == 0.0


# ── typecast_provider.synthesize — HTTP mock ───────────────────────────────

def _make_wav_bytes(duration_ms: int = 1500) -> bytes:
    seg = AudioSegment.silent(duration=duration_ms, frame_rate=44100)
    buf = io.BytesIO()
    seg.export(buf, format="wav")
    return buf.getvalue()


def test_typecast_synthesize_saves_binary_and_measures_length(tmp_path, monkeypatch):
    from src.tts import typecast_provider

    monkeypatch.setenv("TYPECAST_API_KEY", "dummy-key-for-test")
    wav_bytes = _make_wav_bytes(1500)
    dst = tmp_path / "out.wav"

    fake_resp = mock.Mock()
    fake_resp.status_code = 200
    fake_resp.content = wav_bytes
    fake_resp.text = ""

    with mock.patch("requests.post", return_value=fake_resp) as mpost:
        dur = typecast_provider.synthesize(
            "안녕하세요", str(dst), voice="tc_abc123", rate=1.0
        )

    # dst 에 바이너리가 그대로 저장됐는지
    assert dst.exists()
    assert dst.read_bytes() == wav_bytes
    # pydub 로 잰 길이(초)가 대략 1.5초
    assert dur == pytest.approx(1.5, abs=0.1)

    # 요청 페이로드 검증 — voice_id/audio_tempo/format, X-API-KEY 헤더
    _, kwargs = mpost.call_args
    payload = kwargs["json"]
    assert payload["voice_id"] == "tc_abc123"
    assert payload["output"]["audio_format"] == "wav"
    assert payload["output"]["audio_tempo"] == 1.0
    assert kwargs["headers"]["X-API-KEY"] == "dummy-key-for-test"


def test_typecast_synthesize_missing_key_raises(tmp_path, monkeypatch):
    from src.tts import typecast_provider

    monkeypatch.delenv("TYPECAST_API_KEY", raising=False)
    with pytest.raises(typecast_provider.TypecastError):
        typecast_provider.synthesize("x", str(tmp_path / "o.wav"), voice="tc_x", rate=1.0)


def test_typecast_synthesize_text_over_limit_raises(tmp_path, monkeypatch):
    from src.tts import typecast_provider

    monkeypatch.setenv("TYPECAST_API_KEY", "dummy")
    too_long = "가" * (typecast_provider.MAX_TEXT_CHARS + 1)
    with pytest.raises(typecast_provider.TypecastError):
        typecast_provider.synthesize(too_long, str(tmp_path / "o.wav"), voice="tc_x", rate=1.0)


def test_typecast_synthesize_tempo_clamped(tmp_path, monkeypatch):
    from src.tts import typecast_provider

    monkeypatch.setenv("TYPECAST_API_KEY", "dummy")
    fake_resp = mock.Mock()
    fake_resp.status_code = 200
    fake_resp.content = _make_wav_bytes(500)
    fake_resp.text = ""

    with mock.patch("requests.post", return_value=fake_resp) as mpost:
        # rate 5.0 → 상한 2.0 으로 clamp
        typecast_provider.synthesize("x", str(tmp_path / "o.wav"), voice="tc_x", rate=5.0)
    assert mpost.call_args.kwargs["json"]["output"]["audio_tempo"] == 2.0


def test_typecast_synthesize_http_error_raises(tmp_path, monkeypatch):
    from src.tts import typecast_provider

    monkeypatch.setenv("TYPECAST_API_KEY", "dummy")
    fake_resp = mock.Mock()
    fake_resp.status_code = 400
    fake_resp.text = '{"error":"bad voice"}'

    with mock.patch("requests.post", return_value=fake_resp):
        with pytest.raises(typecast_provider.TypecastError):
            typecast_provider.synthesize("x", str(tmp_path / "o.wav"), voice="tc_x", rate=1.0)


# ── 통합 라우팅: src.tts.synthesize 가 provider 로 분기 ─────────────────────

def test_synthesize_routes_typecast(tmp_path, monkeypatch):
    import src.tts as tts_pkg

    monkeypatch.setenv("TYPECAST_API_KEY", "dummy")
    with mock.patch("src.tts.typecast_provider.synthesize", return_value=1.23) as m:
        dur = tts_pkg.synthesize("x", str(tmp_path / "o.wav"), voice="tc_x", rate=1.0)
    assert dur == 1.23
    m.assert_called_once()


def test_synthesize_routes_google(tmp_path):
    import src.tts as tts_pkg

    with mock.patch("src.tts.google_provider.synthesize", return_value=2.34) as m:
        dur = tts_pkg.synthesize("x", str(tmp_path / "o.wav"), voice="ko-KR-Chirp3-HD-Orus", rate=1.0)
    assert dur == 2.34
    m.assert_called_once()
