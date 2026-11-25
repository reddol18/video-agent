"""TTS provider 추상화 — provider 판별 + 인터페이스.

여러 TTS 공급자(Google Cloud, Typecast)를 voice(id/name) 프리픽스만으로
자동 판별해서 동일한 인터페이스(``synthesize(text, dst, *, voice, rate) -> float``)
로 호출할 수 있게 한다. 사용자는 목소리만 고르면 provider 가 자동 결정된다.
"""
from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

Provider = Literal["google", "typecast"]

# Typecast voice_id 프리픽스. 공식 응답 기준 tc_ / uc_ 로 시작한다.
_TYPECAST_PREFIXES = ("tc_", "uc_")


def resolve_provider(voice: str | None) -> Provider:
    """voice 프리픽스로 provider 를 판별하는 순수 함수.

    - ``tc_`` / ``uc_`` 로 시작 → typecast
    - 그 외(``ko-KR-...`` 등) 또는 빈 값 → google (기본)

    Args:
        voice: voice_id 또는 voice_name. None/빈 문자열이면 google.

    Returns:
        "google" 또는 "typecast".
    """
    v = (voice or "").strip()
    if not v:
        return "google"
    lowered = v.lower()
    if lowered.startswith(_TYPECAST_PREFIXES):
        return "typecast"
    return "google"


@runtime_checkable
class TTSProvider(Protocol):
    """TTS provider 공통 인터페이스.

    구현체는 ``synthesize`` 를 제공한다. 반환값은 생성된 오디오의 길이(초)이며,
    기존 ``narration_generator._tts`` 와 동일하게 WAV 파일을 ``dst`` 에 저장한다.
    """

    def synthesize(self, text: str, dst: str, *, voice: str | None, rate: float) -> float:
        """텍스트 → WAV 파일 저장, 오디오 길이(초) 반환."""
        ...
