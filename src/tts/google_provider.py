"""Google Cloud TTS provider.

기존 ``narration_generator._tts`` 의 Google 합성 로직을 그대로 이관한다.
LINEAR16 / 24000Hz / speaking_rate 매핑은 회귀 방지를 위해 동일하게 유지한다.
"""
from __future__ import annotations

import os
from pathlib import Path

from google.cloud import texttospeech
from pydub import AudioSegment

# 기본 보이스 — narration_generator.VOICE_NAME 과 동일 규약.
# voice 인자가 비어 있을 때 폴백으로 사용한다.
_DEFAULT_VOICE = os.getenv("TTS_VOICE_NAME", "ko-KR-Chirp3-HD-Orus")


def synthesize(
    text: str, dst: str, *, voice: str | None, rate: float,
    emotion: str | None = None,
) -> float:
    """Google Cloud TTS 로 WAV 생성. 오디오 길이(초) 반환.

    Args:
        text: 합성할 텍스트.
        dst: 저장할 WAV 경로.
        voice: Google 보이스 이름(``ko-KR-...``). 빈 값이면 환경변수 기본값.
        rate: speaking_rate (Google AudioConfig.speaking_rate 로 그대로 전달).
        emotion: #tts-emotion — 말투/감정 지시문(자연어). 예 "신나고 들뜬 목소리로".
            Chirp3-HD 계열이 지원하는 ``SynthesisInput.prompt`` 로 전달한다.
            빈 값이면 지시 없이 합성(기존 동작).
    """
    client = texttospeech.TextToSpeechClient()

    _emotion = (emotion or "").strip()
    if _emotion:
        # prompt 미지원 보이스/구버전 라이브러리에서 TypeError 가 날 수 있다.
        # 그 경우 감정 지시를 버리고 평소대로 합성해 TTS 자체가 실패하지 않게 한다.
        try:
            synthesis_input = texttospeech.SynthesisInput(text=text, prompt=_emotion)
        except (TypeError, ValueError) as e:
            print(f"[tts] 감정 프롬프트 미지원 — 무시하고 합성: {e}", flush=True)
            synthesis_input = texttospeech.SynthesisInput(text=text)
    else:
        synthesis_input = texttospeech.SynthesisInput(text=text)
    effective_voice = (voice or "").strip() or _DEFAULT_VOICE
    voice_params = texttospeech.VoiceSelectionParams(
        language_code="ko-KR",
        name=effective_voice,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=24000,
        speaking_rate=rate,
    )

    try:
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice_params,
            audio_config=audio_config,
        )
    except Exception as e:
        # #tts-emotion — 서버가 prompt 를 거부하면 감정 지시만 버리고 재시도한다.
        #
        # 실측(2026-08-24): ko-KR-Chirp3-HD-* 는
        #   400 InvalidArgument "Prompt is only supported for Gemini TTS."
        # 를 돌려준다. 필드는 라이브러리에 있지만 보이스가 받지 않는다.
        # 이 경우 그대로 두면 감정을 넣은 씬의 TTS 가 통째로 실패하므로,
        # 말투를 포기하고 음성만이라도 만들어 준다(사용자가 원인을 알 수 있게 로그).
        if not _emotion:
            raise
        print(
            f"[tts] 이 보이스({effective_voice})는 감정 프롬프트를 지원하지 않습니다 "
            f"— 말투 무시하고 합성합니다: {str(e)[:120]}",
            flush=True,
        )
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=voice_params,
            audio_config=audio_config,
        )

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        f.write(response.audio_content)

    audio = AudioSegment.from_wav(dst)
    return len(audio) / 1000.0  # ms → sec
