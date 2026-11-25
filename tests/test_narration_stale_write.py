"""나레이션 대본 stale write 차단 검증 (#stale-write).

실제 사고: 편집기를 새로고침하면 브라우저에 남아 있던 옛 스냅샷이 주기적
전체 push(PUT /register)로 서버의 최신 대본을 덮어써, 방금 고친 대본이
조용히 사라졌다. 백엔드 로그 실측 기준 913회 / 36개 씬에서 발생.

기존 방어(#408)는 "빈 값으로 덮어쓰기"만 막았기 때문에, 옛 텍스트(비어있지
않은 값)로 덮어쓰는 이 사고는 그대로 통과했다.

정책:
  - PATCH(단일 씬 편집)는 항상 최신 → narration_rev 를 올린다.
  - register(전체 push)는 자기가 들고 온 rev 가 서버보다 낮으면 대본 쓰기를 거부.
  - rev 를 안 보내는 옛 클라이언트는 기존 동작 유지(하위 호환).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def client_and_store(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from web.backend import state as store
    store.DATA_DIR = tmp_path_factory.mktemp("data")
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()
    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app), store


@pytest.fixture()
def project(client_and_store):
    client, _ = client_and_store
    pid = client.post("/api/projects", json={"name": "stale-test"}).json()["project_id"]
    r = client.put(f"/api/projects/{pid}/register", json={
        "name": "stale-test",
        "scenes": [{"scene_id": 1, "narration_text": "원본 대본"}],
    })
    assert r.status_code == 200, r.text
    return client, pid


def _scene(client, pid, sid=1):
    scenes = client.get(f"/api/projects/{pid}").json()["scenes"]
    return next(s for s in scenes if s["scene_id"] == sid)


def test_patch_bumps_revision(project):
    client, pid = project
    before = _scene(client, pid)["narration_rev"]
    client.patch(f"/api/projects/{pid}/scenes/1", json={"narration_text": "고친 대본"})
    after = _scene(client, pid)
    assert after["narration_text"] == "고친 대본"
    assert after["narration_rev"] == before + 1


def test_stale_register_cannot_revert_edited_text(project):
    """핵심 회귀: 옛 스냅샷이 방금 고친 대본을 되돌리면 안 된다."""
    client, pid = project
    stale_rev = _scene(client, pid)["narration_rev"]      # 브라우저가 읽어간 시점
    client.patch(f"/api/projects/{pid}/scenes/1", json={"narration_text": "고친 대본"})

    # 브라우저가 옛 스냅샷(옛 rev + 옛 텍스트)을 전체 push
    client.put(f"/api/projects/{pid}/register", json={
        "name": "stale-test",
        "scenes": [{"scene_id": 1, "narration_text": "원본 대본",
                    "narration_rev": stale_rev}],
    })
    assert _scene(client, pid)["narration_text"] == "고친 대본"


def test_current_revision_register_still_applies(project):
    """최신 rev 를 들고 온 정상 push 는 반영되어야 한다."""
    client, pid = project
    cur = _scene(client, pid)["narration_rev"]
    client.put(f"/api/projects/{pid}/register", json={
        "name": "stale-test",
        "scenes": [{"scene_id": 1, "narration_text": "새 대본",
                    "narration_rev": cur}],
    })
    assert _scene(client, pid)["narration_text"] == "새 대본"


def test_client_without_rev_keeps_legacy_behavior(project):
    """rev 를 안 보내는 옛 클라이언트는 기존대로 동작(하위 호환)."""
    client, pid = project
    client.put(f"/api/projects/{pid}/register", json={
        "name": "stale-test",
        "scenes": [{"scene_id": 1, "narration_text": "구버전 클라이언트 대본"}],
    })
    assert _scene(client, pid)["narration_text"] == "구버전 클라이언트 대본"


def test_blank_overwrite_still_blocked(project):
    """#408 기존 방어(공백화 차단)는 그대로 유지."""
    client, pid = project
    cur = _scene(client, pid)["narration_rev"]
    client.put(f"/api/projects/{pid}/register", json={
        "name": "stale-test",
        "scenes": [{"scene_id": 1, "narration_text": "", "narration_rev": cur}],
    })
    assert _scene(client, pid)["narration_text"] == "원본 대본"


def test_repeated_stale_pushes_never_win(project):
    """실제 사고 재현: 옛 스냅샷이 반복 push 돼도 끝까지 버텨야 한다."""
    client, pid = project
    stale_rev = _scene(client, pid)["narration_rev"]
    client.patch(f"/api/projects/{pid}/scenes/1", json={"narration_text": "최종 대본"})
    for _ in range(30):
        client.put(f"/api/projects/{pid}/register", json={
            "name": "stale-test",
            "scenes": [{"scene_id": 1, "narration_text": "원본 대본",
                        "narration_rev": stale_rev}],
        })
    assert _scene(client, pid)["narration_text"] == "최종 대본"
