"""Typecast TTS provider.

Typecast API (https://api.typecast.ai) 로 동기 합성한다.

- 인증: ``X-API-KEY`` 헤더. 키는 환경변수 ``TYPECAST_API_KEY`` 로만 읽는다
  (코드/로그에 절대 하드코딩하지 않는다).
- 합성: ``POST /v1/text-to-speech`` — 응답은 오디오 바이너리(audio/wav) 를 직접 반환.
- speaking_rate → audio_tempo (0.5~2.0) 매핑.
- text 최대 2000자/요청 — 초과 시 잘라내지 않고 명확한 예외를 던져
  사용자가 세그먼트를 분할하도록 유도한다.

리샘플: Typecast WAV 는 16bit/mono/44100Hz 로 오나, pydub 이 렌더 mix 단계에서
샘플레이트 차이를 흡수하므로 저장 시 리샘플하지 않고 원본 그대로 둔다.
(문제 발생 시에만 24000Hz 리샘플로 전환 — review-notes 참조)
"""
from __future__ import annotations

import os
from pathlib import Path

from pydub import AudioSegment

_SYNTH_URL = "https://api.typecast.ai/v1/text-to-speech"
_VOICES_URL = "https://api.typecast.ai/v2/voices"

# Typecast 제약 상수 (spec 확정값).
MAX_TEXT_CHARS = 2000
_DEFAULT_MODEL = "ssfm-v30"
_DEFAULT_LANGUAGE = "kor"
_TEMPO_MIN = 0.5
_TEMPO_MAX = 2.0
_TIMEOUT_SEC = 120


class TypecastError(RuntimeError):
    """Typecast 관련 오류(키 누락, 텍스트 초과, HTTP 실패 등)."""


def _get_api_key() -> str:
    key = (os.getenv("TYPECAST_API_KEY") or "").strip()
    if not key:
        raise TypecastError(
            "TYPECAST_API_KEY 환경변수가 설정되지 않았어요. "
            "설정 화면에서 Typecast API 키를 입력해주세요."
        )
    return key


def _clamp_tempo(rate: float) -> float:
    try:
        r = float(rate)
    except (TypeError, ValueError):
        r = 1.0
    if r < _TEMPO_MIN:
        return _TEMPO_MIN
    if r > _TEMPO_MAX:
        return _TEMPO_MAX
    return r


def synthesize(
    text: str,
    dst: str,
    *,
    voice: str | None,
    rate: float,
    model: str = _DEFAULT_MODEL,
    language: str = _DEFAULT_LANGUAGE,
) -> float:
    """Typecast TTS 로 WAV 생성. 오디오 길이(초) 반환.

    Args:
        text: 합성할 텍스트 (최대 2000자).
        dst: 저장할 WAV 경로.
        voice: Typecast voice_id (``tc_...`` / ``uc_...``).
        rate: speaking_rate → audio_tempo (0.5~2.0 로 clamp).
        model: 합성 모델. 기본 ``ssfm-v30``.
        language: 언어. 기본 ``kor``.

    Raises:
        TypecastError: 키 누락 / voice 누락 / 텍스트 초과 / HTTP 실패.
    """
    import requests  # 지연 임포트 — Google 전용 경로에서 requests 미설치여도 무방.

    voice_id = (voice or "").strip()
    if not voice_id:
        raise TypecastError("Typecast voice_id 가 비어 있어요.")

    if len(text) > MAX_TEXT_CHARS:
        raise TypecastError(
            f"Typecast 는 요청당 최대 {MAX_TEXT_CHARS}자까지 지원해요 "
            f"(현재 {len(text)}자). 나레이션을 여러 세그먼트로 나눠주세요."
        )

    api_key = _get_api_key()
    payload = {
        "voice_id": voice_id,
        "text": text,
        "model": model,
        "language": language,
        "output": {
            "audio_format": "wav",
            "audio_tempo": _clamp_tempo(rate),
        },
    }
    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    resp = requests.post(_SYNTH_URL, json=payload, headers=headers, timeout=_TIMEOUT_SEC)
    if resp.status_code != 200:
        # 응답 본문은 에러 JSON 일 수 있으므로 앞부분만 노출(키는 포함되지 않음).
        detail = resp.text[:300] if resp.text else ""
        raise TypecastError(
            f"Typecast 합성 실패 (HTTP {resp.status_code}): {detail}"
        )

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        f.write(resp.content)

    audio = AudioSegment.from_file(dst)
    return len(audio) / 1000.0  # ms → sec


def list_voices() -> list[dict]:
    """Typecast voice 목록 조회 (GET /v2/voices). voice 배열 반환.

    Raises:
        TypecastError: 키 누락 / HTTP 실패.
    """
    import requests

    api_key = _get_api_key()
    resp = requests.get(_VOICES_URL, headers={"X-API-KEY": api_key}, timeout=_TIMEOUT_SEC)
    if resp.status_code != 200:
        detail = resp.text[:300] if resp.text else ""
        raise TypecastError(
            f"Typecast voice 목록 조회 실패 (HTTP {resp.status_code}): {detail}"
        )
    data = resp.json()
    # 응답이 배열이거나 {"voices": [...]} 형태일 수 있으므로 방어적으로 처리.
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("voices", "data", "result"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []
