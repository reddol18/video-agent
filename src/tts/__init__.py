"""TTS provider 패키지 — voice 프리픽스로 공급자 자동 분기.

공개 API:
    synthesize(text, dst, *, voice, rate) -> float
        voice 프리픽스로 Google / Typecast 를 판별해 해당 provider 로 위임.
    resolve_provider(voice) -> "google" | "typecast"
    list_typecast_voices() -> list[dict]
"""
from __future__ import annotations

from .base import Provider, TTSProvider, resolve_provider


def synthesize(
    text: str, dst: str, *, voice: str | None, rate: float,
    emotion: str | None = None,
) -> float:
    """voice 로 provider 를 판별해 WAV 를 생성하고 길이(초) 를 반환한다.

    - voice 가 ``tc_``/``uc_`` 로 시작 → Typecast. 이때 ``TYPECAST_API_KEY`` 가
      없으면 명확한 에러(TypecastError).
    - 그 외 → Google Cloud TTS.

    기존 ``narration_generator._tts`` 의 계약(WAV 저장 + 길이 반환)을 유지한다.

    #tts-emotion — ``emotion`` 은 말투 지시문(자연어). 현재 Google 경로만 지원하며
    Typecast 는 무시한다(해당 API 는 별도 emotion 파라미터 체계를 쓴다).
    """
    provider = resolve_provider(voice)
    if provider == "typecast":
        from . import typecast_provider
        return typecast_provider.synthesize(text, dst, voice=voice, rate=rate)
    from . import google_provider
    return google_provider.synthesize(
        text, dst, voice=voice, rate=rate, emotion=emotion
    )


def list_typecast_voices() -> list[dict]:
    """Typecast voice 목록 프록시. ``typecast_provider.list_voices`` 위임."""
    from . import typecast_provider
    return typecast_provider.list_voices()


__all__ = [
    "synthesize",
    "resolve_provider",
    "list_typecast_voices",
    "Provider",
    "TTSProvider",
]
