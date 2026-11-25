"""자연어 편집 — Gemini 컨텍스트 캐시 매니저 (#317).

설계 요지
--------
시스템 프롬프트 = 스키마 힌트 + 효과 카탈로그 + 응답 형식 + few-shot 30종.
#332 확장 이후 약 22~26KB(≈ 6,000 Gemini 토큰)로, 매 호출마다 *동일하게* 청구된다.
이를 Gemini 컨텍스트 캐시(`$0.03/1M`) 로 옮기면 호출당 입력 비용이 약 90% 감소.

손익분기 계산 (issue #317 기준):
- 캐시 저장: 6,000 토큰 × $1.00/M/h = $0.006/h ≈ 8.3원/시간
- 캐시 절감: 6,000 × ($0.30 - $0.03)/M = $0.00162/회 ≈ 2.2원/회
- 손익분기: 시간당 약 4회 호출

핵심 정책
--------
1. **옵트인** — 환경변수 `NL_EDITOR_CACHE_ENABLED=1` 일 때만 캐시 사용.
   기본값(0) 에서는 캐시 미사용 → 저장 비용/사용량 부담 0.
2. **프로젝트 단위 분리** — 캐시 키는 (프로젝트 ID, 정적 프롬프트 버전) 조합.
   다른 프로젝트는 캐시를 공유하지 않는다(현재 분리 정책, 후속에서 검토).
3. **TTL 1시간** — 만료 시 자동 재생성. 만료된 cache_name 으로 호출하면 SDK가
   NOT_FOUND 를 던지므로 `ensure_cache` 가 시간 기반으로도 사전 갱신한다.
4. **fallback 강제** — 캐시 생성/사용이 실패하면 일반 호출로 자동 폴백.
   캐시는 *최적화*일 뿐 NL 편집 기능 자체를 막아서는 안 된다.
5. **세션 종료 시 정리** — `release(project_id)` 로 명시적 삭제 가능. TTL 자동 만료도
   안전망.
6. **동시 접근 락** — `threading.Lock` (FastAPI 워커 단일 프로세스 가정).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from src.nl_editor.prompt import STATIC_PROMPT_VERSION

logger = logging.getLogger(__name__)

# 환경변수
_ENV_ENABLED = "NL_EDITOR_CACHE_ENABLED"
_ENV_TTL_SEC = "NL_EDITOR_CACHE_TTL_SEC"
# 손익분기 — 시간당 호출 수가 이 값 미만이면 캐시 자동 해제/미생성 (#340).
# 빈도는 최근 1시간 윈도 내 호출 수(실측)로 측정 (#342).
_ENV_BREAK_EVEN_CPH = "NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR"
# 통계 신뢰성 확보용 — 표본이 이 값 미만이면 손익분기 판정 보류 (#340).
# 윈도가 1시간이므로 "1시간 내 N건 이상 누적되면 평가 시작" 의 의미. 기본 2 (#342).
_ENV_MIN_SAMPLES = "NL_EDITOR_CACHE_MIN_SAMPLES"

_DEFAULT_TTL_SEC = 3600  # 1시간
_DEFAULT_BREAK_EVEN_CPH = 4  # 시간당 4회 (cache.py 모듈 docstring 손익분기 계산)
_DEFAULT_MIN_SAMPLES = 2  # 표본 2건 이상부터 손익분기 평가 (#342)
# 빈도 측정 윈도 — 시간당 호출 수(cph)를 실측하기 위해 1시간 사용 (#342).
# 과거 구현(#340)은 10분 윈도 × 6 환산식을 사용했으나, 표본 ≥ 5 조건과 환산식이
# 수학적으로 모순(samples ≥ 5 ⟹ cph ≥ 30 ≥ threshold=4)되어 자동 해제 분기가
# 영원히 발동하지 않았다. 윈도를 1시간으로 늘리고 환산을 실측치로 바꾸면
# 임계값의 의미가 명확해지고 저빈도 사용자(1~3 rph) 시나리오가 보호된다.
_CALLS_WINDOW_SEC = 3600  # 1시간


def is_cache_enabled() -> bool:
    """캐시 기능 활성화 여부. 환경변수로 옵트인."""
    val = os.getenv(_ENV_ENABLED, "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def _ttl_seconds() -> int:
    raw = os.getenv(_ENV_TTL_SEC)
    if raw:
        try:
            v = int(raw)
            if 60 <= v <= 86400:
                return v
        except ValueError:
            pass
    return _DEFAULT_TTL_SEC


def _break_even_calls_per_hour() -> int:
    """손익분기 임계값(시간당 호출 수). 환경변수로 조정 가능 (#340).

    음수/0/파싱 실패 시 손익분기 자동 해제 기능을 *비활성*화하기 위해 0 을 반환한다.
    """
    raw = os.getenv(_ENV_BREAK_EVEN_CPH)
    if raw is None:
        return _DEFAULT_BREAK_EVEN_CPH
    try:
        v = int(raw)
        if v < 0:
            return 0
        return v
    except ValueError:
        return _DEFAULT_BREAK_EVEN_CPH


def _min_samples_for_break_even() -> int:
    """손익분기 평가를 시작할 최소 표본 수. 통계 신뢰성 확보용 (#340).

    호출 빈도가 임계값 미만이라도 표본이 이 값 미만이면 캐시를 유지한다
    (예: 옵트인 직후 첫 1~2회 호출).
    """
    raw = os.getenv(_ENV_MIN_SAMPLES)
    if raw is None:
        return _DEFAULT_MIN_SAMPLES
    try:
        v = int(raw)
        if v < 0:
            return 0
        return v
    except ValueError:
        return _DEFAULT_MIN_SAMPLES


@dataclass
class CacheEntry:
    """단일 캐시 상태."""
    name: str  # Gemini caches/abc123 형태의 리소스 이름
    token_count: int  # 저장 비용 산정용
    created_at: float  # epoch seconds
    ttl_seconds: int
    version: str  # STATIC_PROMPT_VERSION 시점값

    @property
    def expired(self) -> bool:
        return time.time() >= self.created_at + max(0, self.ttl_seconds - 60)
        # -60s 안전 마진 — 호출 직전 만료 위험 회피.

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)


class NlEditorCacheManager:
    """프로젝트별 시스템 프롬프트 캐시를 관리한다.

    사용 예 (gemini_client 내부에서):
        mgr = get_cache_manager()
        entry = mgr.ensure(
            project_id="abc",
            system_prompt=static_prompt,
            create_fn=create_cached_content,
        )
        if entry:
            text, usage = generate_json_with_usage(prompt, cached_content=entry.name)
            mgr.record_use(project_id="abc", elapsed_seconds=elapsed)
        else:
            text, usage = generate_json_with_usage(prompt, system=static_prompt)
    """

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        # 손익분기 자동 감지용 — 최근 호출 타임스탬프 큐 (프로젝트별)
        self._recent_calls: dict[str, list[float]] = {}

    # ── 캐시 라이프사이클 ────────────────────────────────────────────────

    def ensure(
        self,
        project_id: str,
        system_prompt: str,
        create_fn: Callable[[str, int], tuple[str, int]],
    ) -> Optional[CacheEntry]:
        """프로젝트 캐시를 보장한다. 캐시 미존재/만료/버전 불일치 시 재생성.

        반환: 유효한 `CacheEntry`. 캐시 생성 실패(소형 프롬프트, 권한 등) 시 None.
        """
        if not is_cache_enabled():
            return None

        with self._lock:
            entry = self._entries.get(project_id)
            if entry is not None and not entry.expired and entry.version == STATIC_PROMPT_VERSION:
                return entry

            # 만료/버전 변경 시 기존 캐시는 폐기 시도 (실패해도 무시)
            if entry is not None:
                self._entries.pop(project_id, None)

            ttl = _ttl_seconds()
            try:
                name, tokens = create_fn(system_prompt, ttl)
            except Exception as e:
                logger.warning(
                    "[nl_editor.cache] 캐시 생성 실패 — fallback to non-cached. project=%s err=%s",
                    project_id, e,
                )
                return None

            new_entry = CacheEntry(
                name=name,
                token_count=tokens,
                created_at=time.time(),
                ttl_seconds=ttl,
                version=STATIC_PROMPT_VERSION,
            )
            self._entries[project_id] = new_entry
            logger.info(
                "[nl_editor.cache] 캐시 생성 project=%s name=%s tokens=%d ttl=%ds",
                project_id, name, tokens, ttl,
            )
            return new_entry

    def invalidate(self, project_id: str, delete_fn: Optional[Callable[[str], None]] = None) -> None:
        """프로젝트 캐시 명시 무효화. `delete_fn` 이 주어지면 원격 삭제도 시도.

        세션 종료, 패널 닫힘 시 호출. TTL 자동 만료도 작동하지만 저장 비용 절감을 위해
        즉시 삭제 권장.
        """
        with self._lock:
            entry = self._entries.pop(project_id, None)
        if entry and delete_fn is not None:
            try:
                delete_fn(entry.name)
                logger.info("[nl_editor.cache] 캐시 명시 삭제 project=%s name=%s", project_id, entry.name)
            except Exception as e:
                logger.warning(
                    "[nl_editor.cache] 캐시 원격 삭제 실패(무시). project=%s err=%s",
                    project_id, e,
                )

    def peek(self, project_id: str) -> Optional[CacheEntry]:
        """현재 캐시 상태 조회 (생성/검증 없이)."""
        with self._lock:
            entry = self._entries.get(project_id)
            if entry is None or entry.expired or entry.version != STATIC_PROMPT_VERSION:
                return None
            return entry

    # ── 호출 통계 (손익분기 자동 감지) ───────────────────────────────────

    def record_call(self, project_id: str) -> None:
        """호출 1회 기록. 1시간 윈도 안의 최근 호출만 유지 (#342).

        과거 #340 구현은 10분 윈도 × 6 환산식을 썼으나 표본 ≥ 5 조건과
        수학적으로 모순됐다. 이제 윈도 = 1시간으로, 표본 = 1시간 내 호출 수
        = 시간당 호출 수(cph) 그 자체로 통일한다.
        """
        now = time.time()
        with self._lock:
            q = self._recent_calls.setdefault(project_id, [])
            q.append(now)
            window = now - _CALLS_WINDOW_SEC
            while q and q[0] < window:
                q.pop(0)

    def calls_per_hour(self, project_id: str) -> float:
        """최근 1시간 내 호출 수 = 시간당 호출 빈도(실측) (#342)."""
        with self._lock:
            q = self._recent_calls.get(project_id, [])
            return float(len(q))

    def sample_count(self, project_id: str) -> int:
        """최근 1시간 윈도 내 누적 호출 수 — 손익분기 평가의 표본 크기 (#342).

        윈도가 1시간이므로 `sample_count == calls_per_hour` 가 성립한다.
        의미적 구분(표본 신뢰성 vs 측정값)을 위해 두 메서드를 유지한다.
        """
        with self._lock:
            return len(self._recent_calls.get(project_id, []))

    def should_skip_for_break_even(
        self,
        project_id: str,
        *,
        threshold_cph: Optional[int] = None,
        min_samples: Optional[int] = None,
    ) -> bool:
        """손익분기 미달로 캐시를 건너뛰어야 하는지 판정 (#340, 환산식 보정 #342).

        - `threshold_cph`: 시간당 호출 임계값. None 이면 환경변수 또는 기본값(4).
        - `min_samples`: 표본 최소 크기. None 이면 환경변수 또는 기본값(2).

        반환 True 조건:
            (a) 임계값 > 0  AND
            (b) 표본 수 >= min_samples  AND
            (c) calls_per_hour < threshold_cph

        임계값 0(또는 음수) 이면 자동 해제 기능을 끈 것으로 간주하여 False.
        표본 부족 시에는 False (캐시 유지/생성) — 첫 호출 손실보다 통계 신뢰성 우선.

        #342 보정: 환산식이 `samples × 6` 이었던 #340 구현은 표본 ≥ 5 AND cph < 4
        를 수학적으로 동시 만족할 수 없어 자동 해제 분기가 영원히 발동하지 않았다.
        이제 윈도 = 1시간이고 cph = 1시간 내 호출 수(실측) 이므로 임계값 의미가
        직접적이다. 예: threshold=4, min_samples=2 → 1시간에 2~3회 호출하면 자동 해제.
        """
        if threshold_cph is None:
            threshold_cph = _break_even_calls_per_hour()
        if min_samples is None:
            min_samples = _min_samples_for_break_even()
        if threshold_cph <= 0:
            return False
        with self._lock:
            q = self._recent_calls.get(project_id, [])
            samples = len(q)
            if samples < min_samples:
                return False
            cph = float(samples)  # 윈도 = 1시간이므로 호출 수 == cph
        return cph < threshold_cph


# ── 싱글톤 ─────────────────────────────────────────────────────────────

_singleton: Optional[NlEditorCacheManager] = None
_singleton_lock = threading.Lock()


def get_cache_manager() -> NlEditorCacheManager:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = NlEditorCacheManager()
    return _singleton


def reset_cache_manager_for_tests() -> None:
    """테스트 전용 — 싱글톤 상태 초기화."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "CacheEntry",
    "NlEditorCacheManager",
    "get_cache_manager",
    "is_cache_enabled",
    "reset_cache_manager_for_tests",
]
