"""2026-09-16 — PUT /register 씬 전멸 사고 재발 방지 가드.

사고: 프로젝트 궁예전 4부에서 브라우저가 빈 씬 목록을 보내 74씬이 한 번에 전멸
(감사 로그 scenes_before=74 → scenes_after=0, danger=true).

요구사항 1:1 대응
1. 서버 N씬 + 클라 빈 목록 → 409 거부, 기존 씬 보존.
2. 명시적 allow_bulk_scene_delete 플래그가 있으면 허용.
3. 정상 삭제(1개)는 계속 동작.
4. `scenes` 키 없음/None → 씬 동기화 스킵(삭제 아님), 기존 씬 보존.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_and_store(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from web.backend import state as store
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store._projects.clear()
    store._dump_locks.clear()

    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app), store


def _create_project(client, name: str = "bulk-delete-guard") -> str:
    resp = client.post(
        "/api/projects",
        json={"name": name, "footage_dir": "", "scenario_text": ""},
    )
    assert resp.status_code == 200
    return resp.json()["project_id"]


def _register_74_scenes(client, pid: str) -> None:
    scenes = [
        {"scene_id": i, "narration_text": f"씬 {i} 나레이션"}
        for i in range(1, 75)
    ]
    resp = client.put(f"/api/projects/{pid}/register", json={"scenes": scenes})
    assert resp.status_code == 200
    detail = client.get(f"/api/projects/{pid}").json()
    assert len(detail["scenes"]) == 74


def test_empty_scene_list_is_rejected_and_scenes_preserved(app_and_store):
    """요구사항 1 — 서버 74씬인데 클라가 [] 를 보내면 409 거부 + 74씬 그대로."""
    client, store = app_and_store
    pid = _create_project(client)
    _register_74_scenes(client, pid)

    resp = client.put(f"/api/projects/{pid}/register", json={"scenes": []})
    assert resp.status_code == 409

    body = resp.json()["detail"]
    assert body["error"] == "scene_bulk_delete_blocked"
    assert body["server_scene_count"] == 74
    assert body["client_scene_count"] == 0
    assert body["required_flag"] == store.SCENE_BULK_DELETE_FLAG

    # 씬이 하나도 사라지지 않아야 한다.
    detail = client.get(f"/api/projects/{pid}").json()
    assert len(detail["scenes"]) == 74
    assert {s["scene_id"] for s in detail["scenes"]} == set(range(1, 75))


def test_explicit_flag_allows_full_delete(app_and_store):
    """요구사항 2 — 명시적 전체삭제 플래그가 있으면 실제로 0개가 된다."""
    client, store = app_and_store
    pid = _create_project(client)
    _register_74_scenes(client, pid)

    resp = client.put(
        f"/api/projects/{pid}/register",
        json={"scenes": [], store.SCENE_BULK_DELETE_FLAG: True},
    )
    assert resp.status_code == 200

    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["scenes"] == []


def test_normal_single_scene_delete_still_works(app_and_store):
    """요구사항 3 — 74씬 → 73씬(1개 삭제)은 플래그 없이도 정상 허용(회귀 방지)."""
    client, _store = app_and_store
    pid = _create_project(client)
    _register_74_scenes(client, pid)

    scenes = [
        {"scene_id": i, "narration_text": f"씬 {i} 나레이션"}
        for i in range(1, 75)
        if i != 40
    ]
    resp = client.put(f"/api/projects/{pid}/register", json={"scenes": scenes})
    assert resp.status_code == 200

    detail = client.get(f"/api/projects/{pid}").json()
    assert len(detail["scenes"]) == 73
    assert 40 not in {s["scene_id"] for s in detail["scenes"]}


def test_missing_scenes_key_skips_sync(app_and_store):
    """요구사항 4 — scenes 키 없음/None 은 삭제가 아니라 '동기화 스킵'."""
    client, _store = app_and_store
    pid = _create_project(client)
    _register_74_scenes(client, pid)

    # 키 아예 없음 (Editor.tsx 의 registerProject(pid, "", undefined, ...) 경로)
    resp = client.put(f"/api/projects/{pid}/register", json={"name": "이름만 변경"})
    assert resp.status_code == 200
    detail = client.get(f"/api/projects/{pid}").json()
    assert len(detail["scenes"]) == 74

    # 명시적 null 도 동일하게 스킵 (빈 배열과 혼동 금지)
    resp = client.put(f"/api/projects/{pid}/register", json={"scenes": None})
    assert resp.status_code == 200
    detail = client.get(f"/api/projects/{pid}").json()
    assert len(detail["scenes"]) == 74


def test_new_project_with_few_scenes_is_not_blocked(app_and_store):
    """신규/소규모 프로젝트(씬 5개 미만)는 비율 판정 대상이 아니다 — 등록이 막히면 안 된다."""
    client, _store = app_and_store
    pid = _create_project(client)

    # 0씬 프로젝트에 빈 배열 등록 (신규 프로젝트 정상 등록 경로)
    resp = client.put(f"/api/projects/{pid}/register", json={"scenes": []})
    assert resp.status_code == 200

    # 2씬 → 0씬 도 소규모라 허용 (사용자가 직접 지운 정상 편집)
    resp = client.put(
        f"/api/projects/{pid}/register",
        json={"scenes": [{"scene_id": 1}, {"scene_id": 2}]},
    )
    assert resp.status_code == 200
    resp = client.put(f"/api/projects/{pid}/register", json={"scenes": []})
    assert resp.status_code == 200
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["scenes"] == []
