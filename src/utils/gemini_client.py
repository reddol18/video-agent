"""Gemini API 클라이언트 래퍼 (google-genai SDK)

Rate limit 대응(대기 + 지수 백오프)과 JSON 응답 파싱을 담당한다.
"""

import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from .ffmpeg_utils import ffmpeg_bin, ffprobe_bin

load_dotenv()

_INTERVAL = float(os.getenv("GEMINI_REQUEST_INTERVAL_SEC", "4"))
# #355 후속 (사용자 결정 2026-06): 재시도 횟수 5회 (영상 1개당 60s × 5 = 5분 상한).
# 너무 적으면 일시 429에 약하고, 너무 많으면 백오프 누적으로 무한정 멈춤.
_MAX_RETRY = int(os.getenv("GEMINI_MAX_RETRY", "5"))
# #355 후속 (사용자 결정 2026-06): 지수 백오프 상한 (초). 10*2^attempt가 끝없이 커지는 것을 막음.
_MAX_BACKOFF_SEC = float(os.getenv("GEMINI_MAX_BACKOFF_SEC", "60"))
_INLINE_LIMIT = 20 * 1024 * 1024  # 20MB


def _resolve_model(model: str | None = None) -> str:
    """#355 — 호출 함수에서 model 인자를 안 줬을 때의 폴백 체인.

    우선순위:
      1) 명시 인자 `model`
      2) 환경변수 `GEMINI_MODEL`
      3) `pricing.json` 의 `api.gemini.default_model`
      4) 최후 안전망 "gemini-2.5-flash"

    print 로 1줄 로그를 남겨 어떤 모델로 갔는지 추적 가능하게 한다(검증 요구사항).
    """
    if model:
        chosen = model
    else:
        env = os.getenv("GEMINI_MODEL", "").strip()
        if env:
            chosen = env
        else:
            try:
                # 지연 import — load_pricing 만 필요.
                from src.cost_estimator import get_gemini_default_model
                chosen = get_gemini_default_model()
            except Exception:
                chosen = "gemini-2.5-flash"
    # 디버깅용 한 줄 로그 — 어디서 어떤 모델로 호출됐는지 추적.
    print(f"[gemini] using model={chosen}")
    return chosen


def _log_usage(response, model_id: str, tag: str = "") -> None:
    """Gemini 응답의 usage_metadata 를 한 줄 로그로 출력.

    사용자 결정(2026-06-05): TPM 한도 진단을 위해 호출 단위로 실제 토큰 소비량을 가시화.
    response 가 None 이거나 usage_metadata 가 없어도 안전(silently skip).

    출력 예:
        [gemini-usage] model=gemini-2.5-flash-lite in=17712 cached=0 out=156 total=17868
    """
    try:
        if response is None:
            return
        um = getattr(response, "usage_metadata", None)
        if um is None:
            return
        suffix = f" tag={tag}" if tag else ""
        print(
            f"[gemini-usage] model={model_id} "
            f"in={getattr(um, 'prompt_token_count', '?')} "
            f"cached={getattr(um, 'cached_content_token_count', '?')} "
            f"out={getattr(um, 'candidates_token_count', '?')} "
            f"total={getattr(um, 'total_token_count', '?')}"
            f"{suffix}"
        )
    except Exception:
        pass


def _client() -> genai.Client:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY 환경변수가 없어요.\n"
            ".env 파일에 GOOGLE_API_KEY=... 를 추가해주세요."
        )
    return genai.Client(api_key=api_key)


def generate_json(prompt: str, system: str | None = None, model: str | None = None) -> str:
    """텍스트 프롬프트로 Gemini 호출 → JSON 문자열 반환.

    `model` 미지정 시 `_resolve_model()` 폴백 체인을 사용한다.
    """
    client = _client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        system_instruction=system,
    )
    model_id = _resolve_model(model)

    for attempt in range(_MAX_RETRY):
        try:
            time.sleep(_INTERVAL)
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=config,
            )
            _log_usage(response, model_id, tag="generate_json")
            return response.text
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                # #355 후속 (사용자 결정 2026-06): 백오프 상한 _MAX_BACKOFF_SEC(기본 60s).
                wait = min(10 * (2 ** attempt), _MAX_BACKOFF_SEC)
                print(f"  ⏳ API 요청 한도 초과. {wait:.0f}초 후 재시도... ({attempt+1}/{_MAX_RETRY})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini API 최대 재시도 횟수 초과")


# ── #317 — 컨텍스트 캐싱 지원 ────────────────────────────────────────────

def generate_json_with_usage(
    prompt: str,
    system: str | None = None,
    cached_content: str | None = None,
    model: str | None = None,
) -> tuple[str, dict]:
    """`generate_json` 의 확장판. 응답 텍스트와 함께 토큰 사용량 dict 를 반환한다.

    인자:
    - `prompt`: 사용자 명령 텍스트.
    - `system`: 시스템 프롬프트(=캐시되지 않은 경우). `cached_content` 가 있으면 None 권장.
    - `cached_content`: 컨텍스트 캐시 리소스 이름 (예: "cachedContents/abc123"). 있으면
      해당 캐시를 재사용 → 입력 토큰 단가 90% 인하.

    반환: `(text, usage_dict)`.
    `usage_dict` 구조:
      {
        "cached_input_tokens": int,  # 캐시 적용분
        "regular_input_tokens": int,  # 일반 입력분 (= prompt + system 미캐시분)
        "output_tokens": int,
        "total_tokens": int,
      }

    Gemini SDK가 `usage_metadata` 를 채워주지 않으면 모든 값이 0 으로 반환된다.
    """
    client = _client()
    config_kwargs: dict = {"response_mime_type": "application/json"}
    if cached_content:
        # 캐시 사용 시 system_instruction 은 캐시에 이미 포함되므로 추가하지 않는다.
        config_kwargs["cached_content"] = cached_content
    elif system is not None:
        config_kwargs["system_instruction"] = system
    config = types.GenerateContentConfig(**config_kwargs)
    model_id = _resolve_model(model)

    for attempt in range(_MAX_RETRY):
        try:
            time.sleep(_INTERVAL)
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=config,
            )
            _log_usage(response, model_id, tag="generate_json_with_usage")
            usage = _extract_usage(response)
            return response.text, usage
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                # #355 후속 (사용자 결정 2026-06): 백오프 상한 _MAX_BACKOFF_SEC(기본 60s).
                wait = min(10 * (2 ** attempt), _MAX_BACKOFF_SEC)
                print(f"  ⏳ API 요청 한도 초과. {wait:.0f}초 후 재시도... ({attempt+1}/{_MAX_RETRY})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini API 최대 재시도 횟수 초과")


def _extract_usage(response) -> dict:
    """`response.usage_metadata` 를 우리 표준 dict 로 정규화.

    SDK가 캐시 적용분을 `cached_content_token_count` 로 분리 보고한다. 일반 입력은
    `prompt_token_count - cached_content_token_count` 로 계산한다.
    """
    out = {
        "cached_input_tokens": 0,
        "regular_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return out
    prompt_tokens = getattr(meta, "prompt_token_count", 0) or 0
    cached_tokens = getattr(meta, "cached_content_token_count", 0) or 0
    output_tokens = getattr(meta, "candidates_token_count", 0) or 0
    total_tokens = getattr(meta, "total_token_count", 0) or 0
    out["cached_input_tokens"] = cached_tokens
    # prompt_token_count 는 *캐시 토큰 포함* 누적 수치이므로 빼야 일반 입력만 남는다.
    out["regular_input_tokens"] = max(0, prompt_tokens - cached_tokens)
    out["output_tokens"] = output_tokens
    out["total_tokens"] = total_tokens
    return out


def create_cached_content(
    system_instruction: str,
    ttl_seconds: int = 3600,
    display_name: str = "nl_editor_static_prompt",
    model: str | None = None,
) -> tuple[str, int]:
    """시스템 프롬프트로 컨텍스트 캐시를 생성한다.

    반환: `(cache_name, cached_token_count)`.

    Gemini Flash 의 캐시 최소 크기는 약 1,024 토큰 — 미달이면 API가 INVALID_ARGUMENT 를
    반환한다. 호출자가 이 상황을 잡아 fallback 처리한다.
    """
    client = _client()
    cfg = types.CreateCachedContentConfig(
        system_instruction=system_instruction,
        ttl=f"{int(ttl_seconds)}s",
        display_name=display_name,
        # contents 가 비어있으면 일부 SDK 버전에서 오류 → 빈 dummy 1 토큰 추가.
        # 시스템 프롬프트만 캐시하면 충분하지만 SDK 호환성을 위해 마커를 둔다.
        contents=[types.Content(role="user", parts=[types.Part.from_text(text="시작")])],
    )
    cached = client.caches.create(model=_resolve_model(model), config=cfg)
    name = cached.name
    tokens = 0
    if cached.usage_metadata is not None:
        tokens = getattr(cached.usage_metadata, "total_token_count", 0) or 0
    return name, tokens


def delete_cached_content(name: str) -> None:
    """캐시 명시적 삭제. 실패해도 무시 — TTL 만료가 안전망."""
    try:
        client = _client()
        client.caches.delete(name=name)
    except Exception:
        pass


def generate_json_with_video(
    video_path: str,
    prompt: str,
    system: str | None = None,
    model: str | None = None,
) -> str:
    """영상 파일과 함께 Gemini 호출 → JSON 문자열 반환.

    20MB 미만이면 base64 인라인 전송, 이상이면 File API 사용.
    `model` 미지정 시 `_resolve_model()` 폴백 체인을 사용한다.
    """
    client = _client()
    file_size = Path(video_path).stat().st_size
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        system_instruction=system,
    )
    model_id = _resolve_model(model)

    for attempt in range(_MAX_RETRY):
        try:
            time.sleep(_INTERVAL)
            if file_size < _INLINE_LIMIT:
                with open(video_path, "rb") as f:
                    video_bytes = f.read()
                contents = [
                    types.Part.from_bytes(data=video_bytes, mime_type="video/mp4"),
                    prompt,
                ]
                response = client.models.generate_content(
                    model=model_id,
                    contents=contents,
                    config=config,
                )
            else:
                uploaded = client.files.upload(path=video_path)
                # 파일 처리 완료 대기
                while uploaded.state.name == "PROCESSING":
                    time.sleep(2)
                    uploaded = client.files.get(name=uploaded.name)
                response = client.models.generate_content(
                    model=model_id,
                    contents=[uploaded, prompt],
                    config=config,
                )
                client.files.delete(name=uploaded.name)
            _log_usage(response, model_id, tag="generate_json_with_video")
            return response.text
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                # #355 후속 (사용자 결정 2026-06): 백오프 상한 _MAX_BACKOFF_SEC(기본 60s).
                wait = min(10 * (2 ** attempt), _MAX_BACKOFF_SEC)
                print(f"  ⏳ API 요청 한도 초과. {wait:.0f}초 후 재시도... ({attempt+1}/{_MAX_RETRY})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini API 최대 재시도 횟수 초과")


def get_video_duration(video_path: str) -> float:
    """ffprobe로 영상 파일의 전체 길이(초)를 반환한다.

    Gemini API 호출 없이 로컬에서만 실행되므로 비용이 발생하지 않는다.

    Args:
        video_path: 길이를 측정할 영상 파일 경로

    Returns:
        영상 전체 길이 (초 단위, float)

    Raises:
        FileNotFoundError: 파일이 존재하지 않는 경우
        RuntimeError: ffprobe 실행 실패 시
    """
    video_path_obj = Path(video_path)
    if not video_path_obj.exists():
        raise FileNotFoundError(f"영상 파일을 찾을 수 없어요: {video_path}")

    probe_cmd = [
        ffprobe_bin(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    if probe_result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {probe_result.stderr}")
    return float(probe_result.stdout.strip())


def find_keyword_timestamp(video_path: str, keyword: str, model: str | None = None) -> dict:
    """영상에서 키워드(행위/피사체)가 등장하는 타임스탬프를 Gemini로 분석하여 반환한다.

    find_scene_in_video()와 동일한 영상 전처리(480p 트랜스코딩, 20MB 기준 분기)를 재활용한다.

    Args:
        video_path: 분석할 영상 파일 경로
        keyword: 찾을 키워드 (행위 또는 피사체 설명)

    Returns:
        {"timestamp_sec": float, "total_duration": float}
        - timestamp_sec: 키워드가 처음 등장하는 시각 (초, 0-based)
        - total_duration: 원본 영상 전체 길이 (초)
        키워드를 찾지 못하면 total_duration / 2를 timestamp_sec 폴백으로 사용
    """
    video_path_obj = Path(video_path)
    if not video_path_obj.exists():
        raise FileNotFoundError(f"영상 파일을 찾을 수 없어요: {video_path}")

    # ffprobe로 원본 영상 전체 길이 측정
    probe_cmd = [
        ffprobe_bin(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    if probe_result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {probe_result.stderr}")
    total_duration = float(probe_result.stdout.strip())

    fallback_ts = total_duration / 2.0

    prompt = (
        f"이 영상에서 '{keyword}'와 가장 유사한 행위 또는 피사체가 처음 나타나는 타임스탬프를 "
        f"초 단위로 반환하세요.\n\n"
        f"규칙:\n"
        f"- timestamp_sec은 0 이상 {total_duration:.2f} 이하여야 합니다\n"
        f"- 해당 내용을 찾을 수 없으면 {fallback_ts:.2f}를 반환하세요\n\n"
        f"아래 JSON 형식으로만 반환하세요 (설명 없이 순수 JSON):\n"
        f'{{\"timestamp_sec\": float}}'
    )

    # 480p 트랜스코딩 (find_scene_in_video와 동일 전략)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        transcode_cmd = [
            ffmpeg_bin(), "-y",
            "-i", str(video_path),
            "-vf", "scale=854:480:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-b:v", "500k", "-b:a", "64k", "-ac", "1",
            "-c:v", "libx264", "-preset", "fast",
            tmp_path,
        ]
        transcode_result = subprocess.run(transcode_cmd, capture_output=True, text=True)
        if transcode_result.returncode != 0:
            raise RuntimeError(f"ffmpeg 트랜스코딩 실패: {transcode_result.stderr}")

        raw = generate_json_with_video(tmp_path, prompt, model=model)
        data = json.loads(raw)

        timestamp_sec = float(data.get("timestamp_sec", fallback_ts))

        # 범위 보정
        timestamp_sec = max(0.0, min(timestamp_sec, total_duration))

        return {
            "timestamp_sec": round(timestamp_sec, 3),
            "total_duration": round(total_duration, 3),
        }
    except Exception:
        # 실패 시 폴백
        return {
            "timestamp_sec": round(fallback_ts, 3),
            "total_duration": round(total_duration, 3),
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def find_scene_in_video(
    video_path: str,
    description: str,
    min_duration: float | None = None,
    model: str | None = None,
) -> dict:
    """영상에서 장면 설명에 해당하는 구간을 Gemini로 분석하여 반환한다.

    footage_indexer의 전략과 동일하게 480p 트랜스코딩 후 20MB 기준 분기한다.
    transcode 전에 ffprobe로 원본 전체 길이를 측정한다.

    Args:
        video_path: 분석할 영상 파일 경로
        description: Gemini에게 전달할 장면 설명 (자연어)

    Returns:
        {"start_sec": float, "end_sec": float, "total_duration": float}
        total_duration은 원본 영상 전체 길이 (초 단위)
    """
    video_path_obj = Path(video_path)
    if not video_path_obj.exists():
        raise FileNotFoundError(f"영상 파일을 찾을 수 없어요: {video_path}")

    # ffprobe로 원본 영상 전체 길이 측정
    probe_cmd = [
        ffprobe_bin(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    if probe_result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {probe_result.stderr}")
    total_duration = float(probe_result.stdout.strip())

    min_dur_line = ""
    if min_duration is not None and min_duration > 0:
        min_dur_line = (
            f"- 선택 구간의 길이(end_sec - start_sec)는 반드시 {min_duration:.2f}초 이상이어야 합니다\n"
            f"- 장면이 짧더라도 앞뒤 문맥을 포함해 {min_duration:.2f}초 이상 확보하세요\n"
        )

    prompt = (
        f"이 영상에서 다음 장면을 찾아 시작 시간(start_sec)과 끝 시간(end_sec)을 초 단위로 반환하세요.\n"
        f"장면 설명: {description}\n\n"
        f"규칙:\n"
        f"- start_sec와 end_sec는 0 이상 {total_duration:.2f} 이하여야 합니다\n"
        f"{min_dur_line}"
        f"- 해당 장면을 찾을 수 없으면 영상 전체 구간(start_sec=0, end_sec={total_duration:.2f})을 반환하세요\n\n"
        f"아래 JSON 형식으로만 반환하세요 (설명 없이 순수 JSON):\n"
        f'{{\"start_sec\": float, \"end_sec\": float}}'
    )

    # 480p 트랜스코딩 (footage_indexer와 동일 전략)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        transcode_cmd = [
            ffmpeg_bin(), "-y",
            "-i", str(video_path),
            "-vf", "scale=854:480:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-b:v", "500k", "-b:a", "64k", "-ac", "1",
            "-c:v", "libx264", "-preset", "fast",
            tmp_path,
        ]
        transcode_result = subprocess.run(transcode_cmd, capture_output=True, text=True)
        if transcode_result.returncode != 0:
            raise RuntimeError(f"ffmpeg 트랜스코딩 실패: {transcode_result.stderr}")

        raw = generate_json_with_video(tmp_path, prompt, model=model)
        data = json.loads(raw)

        start_sec = float(data.get("start_sec", 0.0))
        end_sec = float(data.get("end_sec", total_duration))

        # 범위 보정 (원본 범위 초과 방지)
        start_sec = max(0.0, min(start_sec, total_duration))
        end_sec = max(0.0, min(end_sec, total_duration))
        if end_sec <= start_sec:
            end_sec = total_duration

        return {
            "start_sec": round(start_sec, 3),
            "end_sec": round(end_sec, 3),
            "total_duration": round(total_duration, 3),
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)
