"""씬별 TTS 말투/감정 지시문 (#tts-emotion).

Chirp3-HD 는 SynthesisInput.prompt 로 말투를 자연어 지시할 수 있다.
씬마다 지시문을 저장하고, TTS 합성 시 provider 까지 전달되는지 검증한다.

회귀 방지 핵심:
  - 전체 프로젝트 sync(PUT /register)가 값을 지우지 않을 것
    (프론트는 IndexedDB-first 라 주기적으로 프로젝트 전체를 push 한다.
     register allow-list 에서 빠지면 조용히 증발한다.)
  - prompt 미지원 환경에서도 TTS 자체는 실패하지 않을 것
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from web.backend import state as store
    store.DATA_DIR = tmp_path_factory.mktemp("data")
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()
    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app)


@pytest.fixture()
def project(client):
    pid = client.post("/api/projects", json={"name": "emotion-test"}).json()["project_id"]
    r = client.put(f"/api/projects/{pid}/register", json={
        "name": "emotion-test",
        "scenes": [{"scene_id": 1, "narration_text": "안녕하세요"}],
    })
    assert r.status_code == 200, r.text
    return client, pid


def _scene(client, pid, sid=1):
    return next(s for s in client.get(f"/api/projects/{pid}").json()["scenes"]
                if s["scene_id"] == sid)


# ── 저장/조회 ──────────────────────────────────────────────────────────────

def test_default_is_empty(project):
    client, pid = project
    assert _scene(client, pid)["tts_emotion"] == ""


def test_patch_saves_emotion(project):
    client, pid = project
    client.patch(f"/api/projects/{pid}/scenes/1",
                 json={"tts_emotion": "아주 신나고 들뜬 목소리로 말해줘"})
    assert _scene(client, pid)["tts_emotion"] == "아주 신나고 들뜬 목소리로 말해줘"


def test_register_roundtrips_emotion(project):
    """전체 push 로도 값이 보존된다."""
    client, pid = project
    client.put(f"/api/projects/{pid}/register", json={
        "name": "emotion-test",
        "scenes": [{"scene_id": 1, "narration_text": "안녕하세요",
                    "tts_emotion": "차분하게 말해줘"}],
    })
    assert _scene(client, pid)["tts_emotion"] == "차분하게 말해줘"


def test_register_without_key_preserves_existing(project):
    """핵심 회귀: tts_emotion 을 안 보낸 push 가 기존 값을 지우면 안 된다."""
    client, pid = project
    client.patch(f"/api/projects/{pid}/scenes/1", json={"tts_emotion": "진지하게"})
    client.put(f"/api/projects/{pid}/register", json={
        "name": "emotion-test",
        "scenes": [{"scene_id": 1, "narration_text": "안녕하세요"}],
    })
    assert _scene(client, pid)["tts_emotion"] == "진지하게"


def test_emotion_is_trimmed(project):
    client, pid = project
    client.patch(f"/api/projects/{pid}/scenes/1", json={"tts_emotion": "   신나게   "})
    assert _scene(client, pid)["tts_emotion"] == "신나게"


# ── provider 전달 ──────────────────────────────────────────────────────────

def _fake_client(monkeypatch):
    """google TTS 클라이언트를 가로채 SynthesisInput 을 관찰한다."""
    from src.tts import google_provider as gp
    captured = {}

    class _Resp:
        audio_content = b"RIFF0000WAVEfmt "

    class _Client:
        def synthesize_speech(self, *, input, voice, audio_config):
            captured["input"] = input
            return _Resp()

    monkeypatch.setattr(gp.texttospeech, "TextToSpeechClient", lambda: _Client())
    monkeypatch.setattr(gp, "AudioSegment", MagicMock(
        from_wav=MagicMock(return_value=[0] * 1000)))
    return gp, captured


def test_emotion_passed_as_prompt(monkeypatch, tmp_path):
    gp, captured = _fake_client(monkeypatch)
    gp.synthesize("안녕", str(tmp_path / "a.wav"),
                  voice="ko-KR-Chirp3-HD-Alnilam", rate=1.0,
                  emotion="신나게 말해줘")
    assert getattr(captured["input"], "prompt", "") == "신나게 말해줘"


def test_no_emotion_sends_no_prompt(monkeypatch, tmp_path):
    gp, captured = _fake_client(monkeypatch)
    gp.synthesize("안녕", str(tmp_path / "a.wav"),
                  voice="ko-KR-Chirp3-HD-Alnilam", rate=1.0)
    assert not getattr(captured["input"], "prompt", "")


def test_unsupported_prompt_falls_back(monkeypatch, tmp_path):
    """prompt 미지원이어도 TTS 가 통째로 실패하면 안 된다."""
    from src.tts import google_provider as gp
    _, captured = _fake_client(monkeypatch)
    real = gp.texttospeech.SynthesisInput

    def _no_prompt(**kw):
        if "prompt" in kw:
            raise TypeError("prompt 미지원")
        return real(**kw)

    monkeypatch.setattr(gp.texttospeech, "SynthesisInput", _no_prompt)
    gp.synthesize("안녕", str(tmp_path / "a.wav"),
                  voice="ko-KR-Chirp3-HD-Alnilam", rate=1.0,
                  emotion="신나게")
    assert not getattr(captured["input"], "prompt", "")


def test_server_rejecting_prompt_falls_back(monkeypatch, tmp_path):
    """실측 회귀: Chirp3-HD 는 prompt 를 400 으로 거부한다.

    google.api_core.exceptions.InvalidArgument:
        400 Prompt is only supported for Gemini TTS.

    이때 TTS 가 통째로 실패하면 감정을 지정한 씬의 음성이 아예 안 만들어진다.
    말투만 버리고 음성은 정상 생성되어야 한다.
    """
    from src.tts import google_provider as gp
    calls = []

    class _Resp:
        audio_content = b"RIFF0000WAVEfmt "

    class _Client:
        def synthesize_speech(self, *, input, voice, audio_config):
            calls.append(input)
            if getattr(input, "prompt", ""):
                raise RuntimeError("400 Prompt is only supported for Gemini TTS.")
            return _Resp()

    monkeypatch.setattr(gp.texttospeech, "TextToSpeechClient", lambda: _Client())
    monkeypatch.setattr(gp, "AudioSegment", MagicMock(
        from_wav=MagicMock(return_value=[0] * 1000)))

    dur = gp.synthesize("안녕", str(tmp_path / "a.wav"),
                        voice="ko-KR-Chirp3-HD-Alnilam", rate=1.0,
                        emotion="신나게 말해줘")
    assert dur > 0, "폴백 후에도 음성이 생성되어야 한다"
    assert len(calls) == 2, "prompt 포함 1회 실패 후 prompt 없이 재시도해야 한다"
    assert getattr(calls[0], "prompt", "") == "신나게 말해줘"
    assert not getattr(calls[1], "prompt", "")


def test_error_without_emotion_still_raises(monkeypatch, tmp_path):
    """감정과 무관한 실패는 삼키지 않고 그대로 올린다."""
    from src.tts import google_provider as gp

    class _Client:
        def synthesize_speech(self, **kw):
            raise RuntimeError("403 API disabled")

    monkeypatch.setattr(gp.texttospeech, "TextToSpeechClient", lambda: _Client())
    with pytest.raises(RuntimeError, match="403"):
        gp.synthesize("안녕", str(tmp_path / "a.wav"),
                      voice="ko-KR-Chirp3-HD-Alnilam", rate=1.0)
