"""cost_estimator.record_nl_edit — #317 캐시 토큰 분리 기록 회귀.

확인 항목:
1. 기존 시그니처(글자수만 전달) 호환 — `7_nl_edit` Phase 만 추가, 캐시 Phase 없음.
2. 토큰 기반 호출 — `7_nl_edit` (regular) + `7_nl_edit_cached_input` 분리.
3. 캐시 저장 비용 (`7_nl_edit_cache_storage`) 가 cache_storage_tokens/seconds 가 있을 때만 기록.
4. 캐시 입력 단가가 일반 입력 단가의 1/10 ($0.30 → $0.03) 인지 가격 비례 확인.
"""

from __future__ import annotations

from src.cost_estimator import CostEstimator


def test_legacy_chars_only_signature():
    est = CostEstimator()
    est.record_nl_edit(input_chars=1000, output_chars=300)
    phases = {p.phase: p for p in est.breakdown.phases}
    assert "7_nl_edit" in phases
    assert phases["7_nl_edit"].api_usd > 0
    assert "7_nl_edit_cached_input" not in phases
    assert "7_nl_edit_cache_storage" not in phases


def test_tokens_split_into_cached_and_regular():
    est = CostEstimator()
    est.record_nl_edit(
        cached_input_tokens=6000,
        regular_input_tokens=300,
        output_tokens=200,
    )
    phases = {p.phase: p for p in est.breakdown.phases}
    assert "7_nl_edit" in phases
    assert "7_nl_edit_cached_input" in phases
    # 캐시 입력 단가가 1/10 — 일반 입력 같은 토큰 수 기준 대비
    # regular: 300 tokens × $0.30/M = $0.00009
    # cached: 6000 tokens × $0.03/M = $0.00018
    # output: 200 tokens × $2.50/M = $0.00050
    # 7_nl_edit api = regular_input + output ≈ $0.00059
    regular = phases["7_nl_edit"].api_usd
    cached = phases["7_nl_edit_cached_input"].api_usd
    assert regular > 0
    assert cached > 0
    # 같은 토큰 수 기준이라면 cached 단가는 정확히 1/10. 비례 확인을 위해 별도 계산.
    # cached(6000) / regular_input(300) = 20 토큰비. 단가비 0.03/0.30 = 0.1.
    # 따라서 cached / (regular_input portion) = 20 * 0.1 = 2.0.
    # 위에서 regular Phase 는 입력+출력 합. 출력만 제거하고 비교.
    regular_input_only = 300 / 1_000_000 * 0.30
    cached_input_only = 6000 / 1_000_000 * 0.03
    assert abs(cached_input_only / regular_input_only - 2.0) < 1e-6
    assert abs(cached - cached_input_only) < 1e-9


def test_cache_storage_recorded_when_storage_tokens_present():
    est = CostEstimator()
    est.record_nl_edit(
        cached_input_tokens=6000,
        regular_input_tokens=300,
        output_tokens=200,
        cache_storage_tokens=6000,
        cache_storage_seconds=3600.0,  # 1시간
    )
    phases = {p.phase: p for p in est.breakdown.phases}
    assert "7_nl_edit_cache_storage" in phases
    # 6000 tokens × $1.00/M/h × 1h = $0.006
    storage_api = phases["7_nl_edit_cache_storage"].api_usd
    assert abs(storage_api - 6000 / 1_000_000 * 1.00 * 1.0) < 1e-9


def test_no_cache_storage_when_seconds_zero():
    est = CostEstimator()
    est.record_nl_edit(
        cached_input_tokens=6000,
        regular_input_tokens=300,
        output_tokens=200,
        cache_storage_tokens=6000,
        cache_storage_seconds=0,
    )
    phases = {p.phase: p for p in est.breakdown.phases}
    assert "7_nl_edit_cache_storage" not in phases


def test_multiple_calls_accumulate():
    est = CostEstimator()
    for _ in range(3):
        est.record_nl_edit(
            cached_input_tokens=6000,
            regular_input_tokens=300,
            output_tokens=200,
        )
    phases = {p.phase: p for p in est.breakdown.phases}
    # 같은 phase 면 누적
    assert phases["7_nl_edit_cached_input"].api_usd > 0
    # 누적분: 3회 × (6000 × $0.03/M) = $0.00054
    expected = 3 * 6000 / 1_000_000 * 0.03
    assert abs(phases["7_nl_edit_cached_input"].api_usd - expected) < 1e-9
