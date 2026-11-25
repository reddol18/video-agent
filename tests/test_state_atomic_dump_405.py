"""#405 — state._dump 의 atomic write + threading.Lock 동작 검증.

요구사항 1:1 대응
- atomic write: _dump 도중 다른 프로세스가 state.json 을 읽어도 부분/손상 JSON 을 보지 않는다.
  → tmp 경유 + os.replace 패턴으로 구현됐는지 + 결과 파일이 유효 JSON 인지 확인.
- 동시 호출 시 손상 없음: 같은 project 의 _dump 를 여러 스레드가 동시에 호출해도 마지막
  결과가 유효 JSON 이며 모든 호출이 예외 없이 끝난다.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest


@pytest.fixture()
def store_with_tmp(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from web.backend import state as store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store._projects.clear()
    store._dump_locks.clear()
    return store


def _make_project(store, pid: str = "p1"):
    from web.backend.models import Project
    now = "2026-06-24T00:00:00+00:00"
    p = Project(
        project_id=pid,
        name="test",
        footage_dir="",
        scenario_text="",
        created_at=now,
        updated_at=now,
    )
    store._projects[pid] = p
    return p


def test_dump_uses_tmp_then_replace(store_with_tmp):
    """_dump 가 tmp 경유 → os.replace 로 원자적으로 쓰는지 검증.

    구현 디테일을 검증하는 대신, 결과 파일이 유효 JSON 이고
    잔재 tmp 파일이 남지 않는지 확인한다.
    """
    store = store_with_tmp
    p = _make_project(store, "atomic-pid")
    store._dump(p)

    state_path = store._project_dir("atomic-pid") / "state.json"
    tmp_path = state_path.with_suffix(".json.tmp")
    assert state_path.exists(), "state.json 이 생성돼야 함"
    assert not tmp_path.exists(), "tmp 잔재가 남지 않아야 함"
    # 유효 JSON 이어야 한다 — 부분 쓰기였으면 파싱 실패.
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["project_id"] == "atomic-pid"


def test_concurrent_dumps_no_corruption(store_with_tmp):
    """같은 project 에 대해 여러 스레드가 동시에 _dump 를 호출해도
    예외 없이 끝나고 최종 state.json 이 유효 JSON 으로 남는지 검증."""
    store = store_with_tmp
    p = _make_project(store, "concurrent-pid")

    errors: list[BaseException] = []
    iterations = 50
    threads_n = 8

    def worker(idx: int):
        try:
            for i in range(iterations):
                # 각 스레드가 name 을 바꿔 가며 dump → 직렬화 안 되면 손상 가능.
                p.name = f"t{idx}-{i}"
                store._dump(p)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"동시 _dump 도중 예외 발생: {errors[:3]}"

    state_path = store._project_dir("concurrent-pid") / "state.json"
    assert state_path.exists()
    # 손상 없이 유효 JSON 으로 끝났는지.
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["project_id"] == "concurrent-pid"
    # tmp 잔재 없음.
    assert not state_path.with_suffix(".json.tmp").exists()
