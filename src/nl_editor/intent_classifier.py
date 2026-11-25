"""자연어 명령 의도 분류기 — LLM 기반 (#359, 사용자 직접 지시).

설계 의도
--------
기존 키워드 분류(`is_overlay_intent`, `is_split_intent`)는 어휘 사전이 좁아
`"여기 잘라줘"`, `"별 그려"`, `"끊어"` 같은 변형 명령을 patch 경로로 흘려보낸다.
patch 경로 진입 후에는 영상 첨부가 없어 분류 자체에는 비용 부담이 적지만,
사용자 의도가 split/overlay 인데도 영상 분석이 발동하지 않으면 결과 품질이 떨어진다.

핵심 가치: **영상 첨부 호출(split/overlay detector)을 의도가 명확할 때만 발생시켜
비디오 토큰 비용 최소화.** 의도 분류는 텍스트만 → 호출당 매우 저렴
(Flash-Lite 기준 약 $0.00002 ≈ 0.03원).

폴백 체인
--------
1. LLM 호출 성공 + JSON 파싱 성공 + intent 값이 유효(split/overlay/patch) → 그대로 사용.
2. LLM 호출 실패(HTTP 에러, JSON 파싱 실패, intent 값 오류) → 키워드 분류 폴백.
   meta 에 `fallback_used=True` 표시.
3. 모호한 경우 LLM은 `patch` 를 반환하도록 시스템 프롬프트에서 안전 폴백 명시.

캐싱
----
시스템 프롬프트가 매 호출 동일하므로 Gemini implicit caching 의 이점을 자연스럽게
받는다. Flash-Lite 는 explicit cached_content 의 최소 토큰 요건(약 1,024 토큰)을
시스템 프롬프트 단독으로는 채우지 못할 가능성이 높으므로, 본 모듈은 explicit 캐시를
*시도*하되 실패하면 조용히 일반 호출로 폴백한다. 토큰 카운트가 `cached_input_tokens` 로
돌아오면 implicit/explicit 어느 쪽이든 캐시가 적용된 것이다.

모델 폴백 체인
-------------
인자 `model` → 프로젝트의 모델 → `gemini-2.5-flash-lite` (분류는 가장 싼 모델이 기본).
호출자가 명시적으로 다른 모델을 주입하면 그대로 사용한다.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

logger = logging.getLogger(__name__)


# 분류 결과 유형
IntentValue = Literal["split", "overlay", "patch"]
_VALID_INTENTS: set[str] = {"split", "overlay", "patch"}

# 분류 전용 기본 모델 — Flash-Lite. 분류는 텍스트 100바이트 수준이라 가장 싼 모델로 충분.
_DEFAULT_CLASSIFIER_MODEL = "gemini-2.5-flash-lite"

# 시스템 프롬프트 버전 — 변경 시 캐시 무효화 키로 사용.
INTENT_PROMPT_VERSION = "v1-2026-06-03-llm-intent"

# explicit 캐시 TTL — 5분(짧게 잡아 저장 비용 위험 최소화).
_INTENT_CACHE_TTL_SEC = 300


# ── 시스템 프롬프트 ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = """당신은 비디오 에디터의 명령 의도 분류기입니다.
사용자의 자연어 명령을 보고 다음 3개 분기 중 하나로 분류하세요.

분기 정의:
1. "split" — 영상 안의 특정 이벤트/장면/순간을 찾아 그 지점에서 영상을 자르거나 나누는 의도.
   - 예: "여기 잘라줘", "주인공이 웃는 순간마다 끊어", "충돌하는 부분에서 나눠", "장면이 바뀌는 데서 쪼개"
   - 핵심 신호: 자르기/나누기/분할 + 시각적 이벤트 또는 시간 지점

2. "overlay" — 영상의 특정 이벤트 시점에 텍스트/자막/효과를 *자동으로* 얹는 의도.
   - 예: "고양이 보일 때마다 텍스트 띄워", "찌를 때 '아얏!' 표시해줘", "별 그려", "이 장면에서 효과음 깔아"
   - 핵심 신호: 이벤트 감지 + 텍스트/효과/SFX/그림 추가

3. "patch" — 위 2개에 해당하지 않는 모든 일반 편집 명령. RenderSpec 의 속성을 직접 변경.
   - 예: "마지막 씬 좀 더 밝게", "BGM 작게", "씬 3에 페이드인 추가", "텍스트 색을 빨강으로",
     "타이틀 위로 올려", "씬 순서 바꿔", "1번 씬 길이 줄여"
   - 핵심 신호: 명시적 속성/값 변경, 자동 이벤트 감지가 필요 없는 단순 편집

분류 규칙:
- 사용자가 "여기" "이 부분" "이 장면" 등으로 *현재 위치*를 가리키며 자르라고 하면 → split
- "그려" "넣어" "표시해" 같은 동사가 시각 이벤트와 함께 나오면 → overlay
- 모호하거나 어느 분기인지 판단이 어려우면 → patch (안전 폴백)
- 사용자가 *직접* 텍스트 내용·좌표·시각을 지정하면 (예: "5초에 '안녕' 텍스트 추가") → patch
  (자동 감지가 필요 없으므로 RenderSpec 직접 수정)

응답 형식 (JSON 만, 다른 설명 금지):
{"intent": "split|overlay|patch", "reasoning": "한 줄 이유"}
"""


# ── 메타 dict 형식 ───────────────────────────────────────────────────────

def _empty_meta() -> dict[str, Any]:
    return {
        "model": "",
        "input_chars": 0,
        "output_chars": 0,
        "reasoning": "",
        "fallback_used": False,
        "cached_input_tokens": 0,
        "regular_input_tokens": 0,
        "output_tokens": 0,
        "duration_ms": 0,
    }


# ── 캐시 매니저 (모듈 전역, 시스템 프롬프트 단일 — explicit 캐시 시도용) ─────

@dataclass
class _IntentCacheEntry:
    name: str
    token_count: int
    created_at: float
    ttl_seconds: int
    version: str
    model: str

    @property
    def expired(self) -> bool:
        return time.time() >= self.created_at + max(0, self.ttl_seconds - 30)


class _IntentCacheManager:
    """모듈 전역 explicit 캐시 매니저.

    NlEditorCacheManager 와 달리 시스템 프롬프트가 *단일*(프로젝트와 무관)이므로
    프로젝트별 분리 불필요. 다만 모델별로는 분리한다.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _IntentCacheEntry] = {}
        self._lock = threading.Lock()
        # 실패 이력 — Flash-Lite 캐시 최소 토큰 미달로 한 번 실패하면 같은 모델로는
        # 더 이상 재시도하지 않아 호출 지연/로그 누적을 막는다.
        self._failed_models: set[str] = set()

    def ensure(
        self,
        model: str,
        create_fn: Callable[[str, int, Optional[str]], tuple[str, int]],
    ) -> Optional[_IntentCacheEntry]:
        with self._lock:
            if model in self._failed_models:
                return None
            entry = self._entries.get(model)
            if (
                entry is not None
                and not entry.expired
                and entry.version == INTENT_PROMPT_VERSION
                and entry.model == model
            ):
                return entry
            # 만료/버전 변경 시 폐기
            if entry is not None:
                self._entries.pop(model, None)
        # 락 밖에서 외부 호출(타임 비용 큼) — race 가능하나 idempotent 라 무해.
        try:
            name, tokens = create_fn(_SYSTEM_PROMPT, _INTENT_CACHE_TTL_SEC, model)
        except Exception as e:
            logger.info(
                "[intent_classifier.cache] explicit 캐시 생성 실패 (소형 프롬프트일 가능성) "
                "model=%s err=%s — implicit 캐시에 의존",
                model, e,
            )
            with self._lock:
                self._failed_models.add(model)
            return None
        new_entry = _IntentCacheEntry(
            name=name,
            token_count=tokens,
            created_at=time.time(),
            ttl_seconds=_INTENT_CACHE_TTL_SEC,
            version=INTENT_PROMPT_VERSION,
            model=model,
        )
        with self._lock:
            self._entries[model] = new_entry
        logger.info(
            "[intent_classifier.cache] explicit 캐시 생성 model=%s name=%s tokens=%d",
            model, name, tokens,
        )
        return new_entry


_singleton_lock = threading.Lock()
_singleton: Optional[_IntentCacheManager] = None


def _get_cache_manager() -> _IntentCacheManager:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = _IntentCacheManager()
    return _singleton


def reset_cache_for_tests() -> None:
    """테스트 전용 — 캐시 매니저 초기화."""
    global _singleton
    with _singleton_lock:
        _singleton = None


# ── 모델 결정 ────────────────────────────────────────────────────────────

def _resolve_classifier_model(
    explicit: Optional[str], project_id: Optional[str]
) -> str:
    """분류 호출에 사용할 모델 ID.

    우선순위:
      1) 명시 인자 `explicit`
      2) 프로젝트의 gemini_model (있고 비어있지 않으면)
      3) `gemini-2.5-flash-lite` (분류는 가장 싼 모델이 기본)
    """
    if explicit and explicit.strip():
        return explicit.strip()
    if project_id:
        try:
            from web.backend import state as _store
            p = _store.get_project(project_id)
            m = getattr(p, "gemini_model", None) if p else None
            if m and m.strip():
                return m.strip()
        except Exception:
            # web.backend import 실패(CLI 컨텍스트) 시 조용히 폴백
            pass
    return _DEFAULT_CLASSIFIER_MODEL


# ── 키워드 폴백 ──────────────────────────────────────────────────────────

def _fallback_classify(instruction: str) -> IntentValue:
    """LLM 실패 시 키워드 기반 분류 — overlay 우선, 그다음 split, 둘 다 아니면 patch.

    핵심: 기존 `is_overlay_intent`, `is_split_intent` 와 우선순위(overlay → split → patch)를
    그대로 유지하여 라우터의 기존 분기 동작과 호환.
    """
    # 지연 import — 모듈 로딩 순환 회피.
    from src.nl_editor.overlay_detector import is_overlay_intent
    from src.nl_editor.split_detector import is_split_intent
    if is_overlay_intent(instruction):
        return "overlay"
    if is_split_intent(instruction):
        return "split"
    return "patch"


# ── LLM 응답 파싱 ────────────────────────────────────────────────────────

def _parse_intent_response(raw: str) -> tuple[Optional[str], str]:
    """LLM raw 응답 → (intent, reasoning).

    intent 가 유효 범위 밖이면 None 반환 (호출자가 폴백 결정).
    """
    if not raw:
        return None, ""
    text = raw.strip()
    # ```json fence 제거
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 부분 JSON 추출 시도
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None, ""
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, ""
    if not isinstance(data, dict):
        return None, ""
    intent = str(data.get("intent") or "").strip().lower()
    reasoning = str(data.get("reasoning") or "")
    if intent not in _VALID_INTENTS:
        return None, reasoning
    return intent, reasoning


# ── 공개 API ─────────────────────────────────────────────────────────────

def classify_intent(
    instruction: str,
    project_id: Optional[str] = None,
    model: Optional[str] = None,
    *,
    generate_fn: Optional[Callable[..., tuple[str, dict]]] = None,
    create_cache_fn: Optional[Callable[[str, int, Optional[str]], tuple[str, int]]] = None,
) -> tuple[IntentValue, dict[str, Any]]:
    """자연어 명령의 의도를 분류한다.

    인자:
    - `instruction`: 분류할 자연어 명령.
    - `project_id`: 프로젝트 ID — 프로젝트 설정 모델 조회용. None 이면 기본 모델.
    - `model`: 분류용 모델 ID 명시 지정. None 이면 폴백 체인
      (인자 → 프로젝트 모델 → `gemini-2.5-flash-lite`).
    - `generate_fn`: 테스트용 LLM 호출 함수 주입.
      시그니처: `(prompt, *, system=None, cached_content=None, model=None) -> (text, usage)`.
    - `create_cache_fn`: 테스트용 캐시 생성 함수 주입.
      시그니처: `(system_prompt, ttl_seconds, model) -> (name, tokens)`.

    반환: `(intent, meta)`.

    meta 구조:
        {
          "model": str,                  # 실제 호출에 사용된 모델 ID (폴백일 땐 분류기 기본값)
          "input_chars": int,            # 시스템 + 사용자 명령 글자수
          "output_chars": int,           # LLM 응답 글자수
          "reasoning": str,              # LLM 이 제공한 분류 근거 (폴백 시 자동 메시지)
          "fallback_used": bool,         # True 면 키워드 폴백으로 결정됨
          "cached_input_tokens": int,    # usage_metadata 기반
          "regular_input_tokens": int,
          "output_tokens": int,
          "duration_ms": int,            # 분류 호출 wall-clock
        }
    """
    t0 = time.monotonic()
    meta = _empty_meta()

    # 1) 모델 결정
    used_model = _resolve_classifier_model(model, project_id)
    meta["model"] = used_model

    # 2) 호출 함수 폴백
    if generate_fn is None:
        from src.utils.gemini_client import generate_json_with_usage as _gen
        generate_fn = _gen
    if create_cache_fn is None:
        from src.utils.gemini_client import create_cached_content as _create
        # SDK 시그니처: (system_instruction, ttl_seconds=..., display_name=..., model=...)
        # 캐시 매니저 시그니처: (system_prompt, ttl_seconds, model) → (name, tokens)
        def create_cache_fn(sp: str, ttl: int, m: Optional[str]) -> tuple[str, int]:
            return _create(sp, ttl_seconds=ttl, display_name="nl_intent_classifier", model=m)

    # 3) explicit 캐시 시도 (Flash-Lite 에서는 최소 토큰 미달로 실패 가능 — 자동 폴백).
    cache_entry: Optional[_IntentCacheEntry] = None
    try:
        cache_entry = _get_cache_manager().ensure(used_model, create_cache_fn)
    except Exception as e:
        logger.info("[intent_classifier] 캐시 확보 단계 예외(무시) err=%s", e)
        cache_entry = None

    # 4) LLM 호출
    instruction_safe = (instruction or "").strip()
    if not instruction_safe:
        # 빈 명령 — 즉시 폴백.
        meta["fallback_used"] = True
        meta["reasoning"] = "빈 명령 — patch 로 안전 폴백"
        meta["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return "patch", meta

    user_prompt = f"사용자 명령: \"{instruction_safe}\"\n\n위 명령을 분류하세요."
    meta["input_chars"] = len(_SYSTEM_PROMPT) + len(user_prompt)

    text = ""
    usage: dict[str, Any] = {}
    llm_ok = False
    try:
        if cache_entry is not None:
            text, usage = generate_fn(
                user_prompt,
                cached_content=cache_entry.name,
                model=used_model,
            )
        else:
            text, usage = generate_fn(
                user_prompt,
                system=_SYSTEM_PROMPT,
                model=used_model,
            )
        llm_ok = True
    except Exception as e:
        # 캐시 사용 시 cache_not_found / expired 등 흔한 실패 — 일반 호출 한 번 더 시도.
        if cache_entry is not None:
            logger.warning(
                "[intent_classifier] 캐시 호출 실패 → 일반 호출 폴백. err=%s", e,
            )
            try:
                text, usage = generate_fn(
                    user_prompt,
                    system=_SYSTEM_PROMPT,
                    model=used_model,
                )
                llm_ok = True
            except Exception as e2:
                logger.warning("[intent_classifier] 일반 호출도 실패. err=%s", e2)
                llm_ok = False
        else:
            logger.warning("[intent_classifier] LLM 호출 실패. err=%s", e)
            llm_ok = False

    meta["output_chars"] = len(text or "")
    if usage:
        meta["cached_input_tokens"] = int(usage.get("cached_input_tokens", 0) or 0)
        meta["regular_input_tokens"] = int(usage.get("regular_input_tokens", 0) or 0)
        meta["output_tokens"] = int(usage.get("output_tokens", 0) or 0)

    # 5) 응답 파싱
    if llm_ok:
        intent, reasoning = _parse_intent_response(text)
        if intent is not None:
            meta["reasoning"] = reasoning or "(LLM 이유 미제공)"
            meta["duration_ms"] = int((time.monotonic() - t0) * 1000)
            return intent, meta  # type: ignore[return-value]
        logger.warning(
            "[intent_classifier] LLM 응답 파싱 실패 → 키워드 폴백. raw=%r", (text or "")[:200],
        )

    # 6) 키워드 폴백
    fb_intent = _fallback_classify(instruction_safe)
    meta["fallback_used"] = True
    meta["reasoning"] = (
        f"키워드 폴백 — LLM {'응답 실패' if not llm_ok else '파싱/검증 실패'}, "
        f"분류기 결정: {fb_intent}"
    )
    meta["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return fb_intent, meta


__all__ = [
    "INTENT_PROMPT_VERSION",
    "IntentValue",
    "classify_intent",
    "reset_cache_for_tests",
]
