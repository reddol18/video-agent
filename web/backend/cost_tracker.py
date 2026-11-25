"""백엔드용 비용 누적 헬퍼.

라우터/태스크에서 Phase 종료 시 호출 — Project.cost_estimate에 누적해서 저장한다.
프론트엔드는 GET /api/projects/{id}/cost 또는 Project 응답으로 받아 표시.
"""
from __future__ import annotations
from contextlib import contextmanager
import time
from typing import Optional

from src.cost_estimator import CostEstimator, CostBreakdown
from . import state as store


def _load_estimator(project_id: str) -> CostEstimator:
    """기존 누적을 불러와서 이어 쓸 수 있는 estimator를 반환."""
    p = store.get_project(project_id)
    existing = None
    if p and p.cost_estimate:
        try:
            existing = CostBreakdown.from_dict(p.cost_estimate)
        except Exception:
            existing = None
    return CostEstimator(existing=existing)


def _save_estimator(project_id: str, est: CostEstimator) -> None:
    store.update_project(project_id, cost_estimate=est.breakdown.to_dict())


@contextmanager
def measure_phase(project_id: str):
    """with 블록의 wall-clock을 cpu_seconds 근사값으로 사용하고,
    블록 내부에서 record_* 호출 결과를 모아 자동 저장한다.

    사용 예:
        with measure_phase(pid) as est:
            est.record_tts(chars=1500, cpu_seconds=0)  # cpu_seconds는 자동 보정
    """
    est = _load_estimator(project_id)
    start = time.monotonic()
    try:
        yield est
    finally:
        elapsed = time.monotonic() - start
        # 마지막에 기록된 phase의 cpu_seconds가 0이면 wall-clock으로 보정.
        if est.breakdown.phases and est.breakdown.phases[-1].cpu_seconds == 0:
            est.breakdown.phases[-1].cpu_seconds = elapsed
        _save_estimator(project_id, est)


def record_phase(project_id: str, record_fn_name: str, **kwargs) -> None:
    """단발성 기록 — record_fn_name에 해당하는 메서드 호출 후 저장."""
    est = _load_estimator(project_id)
    fn = getattr(est, record_fn_name, None)
    if not fn:
        return
    fn(**kwargs)
    _save_estimator(project_id, est)


def get_cost_summary(project_id: str) -> Optional[dict]:
    """프로젝트의 누적 비용 요약."""
    p = store.get_project(project_id)
    if not p:
        return None
    return p.cost_estimate or {}
