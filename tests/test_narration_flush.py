"""새로고침 직전 대본 저장 (#narration-flush).

실제 사고(2일 연속 보고): TTS 대본을 고치고 바로 새로고침하면
"치다 만 텍스트"가 남아 있었다.

원인 — 프론트는 대본 입력을 800ms 디바운스로 PATCH 한다. 그 사이에 페이지가
사라지면 마지막 타건이 서버에 도달하지 못한다. 새로고침 시 화면은 서버를
진실의 원천으로 다시 읽으므로, 서버에 없는 마지막 글자들이 사라진 것처럼 보인다.

해결 — unload 직전 `navigator.sendBeacon` 으로 밀어 넣는다.
beacon 은 POST 만 지원해서 PATCH 와 별도로 이 경로를 둔다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from web.backend import state as store
    store.DATA_DIR = tmp_path_factory.mktemp("data")
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()
    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app)


@pytest.fixture()
def project(client):
    pid = client.post("/api/projects", json={"name": "flush-test"}).json()["project_id"]
    r = client.put(f"/api/projects/{pid}/register", json={
        "name": "flush-test",
        "scenes": [{"scene_id": 1, "narration_text": "원본 대본"}],
    })
    assert r.status_code == 200, r.text
    return client, pid


def _scene(client, pid, sid=1):
    return next(s for s in client.get(f"/api/projects/{pid}").json()["scenes"]
                if s["scene_id"] == sid)


def _flush(client, pid, text, sid=1):
    return client.post(f"/api/projects/{pid}/scenes/{sid}/narration-flush",
                       json={"narration_text": text})


def test_flush_saves_last_keystrokes(project):
    """핵심 회귀: 디바운스가 발화하기 전이라도 마지막 대본이 저장된다."""
    client, pid = project
    r = _flush(client, pid, "칠월엔 그렇게 덥진 않았습니다.")
    assert r.status_code == 200 and r.json()["changed"] is True
    assert _scene(client, pid)["narration_text"] == "칠월엔 그렇게 덥진 않았습니다."


def test_flush_bumps_revision(project):
    """#stale-write — 저장 후 rev 가 올라야 옛 스냅샷 push 를 막을 수 있다."""
    client, pid = project
    before = _scene(client, pid)["narration_rev"]
    _flush(client, pid, "새 대본")
    assert _scene(client, pid)["narration_rev"] == before + 1


def test_blank_flush_does_not_erase(project):
    """빈 값이 날아와도 기존 대본을 지우면 안 된다."""
    client, pid = project
    r = _flush(client, pid, "   ")
    assert r.json()["ok"] is False
    assert _scene(client, pid)["narration_text"] == "원본 대본"


def test_same_text_is_noop(project):
    """같은 값이면 rev 를 올리지 않는다(불필요한 무효화 방지)."""
    client, pid = project
    before = _scene(client, pid)["narration_rev"]
    r = _flush(client, pid, "원본 대본")
    assert r.json()["changed"] is False
    assert _scene(client, pid)["narration_rev"] == before


def test_missing_field_is_rejected(project):
    client, pid = project
    r = client.post(f"/api/projects/{pid}/scenes/1/narration-flush", json={})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_unknown_scene_404(project):
    client, pid = project
    assert _flush(client, pid, "x", sid=999).status_code == 404


def test_flush_survives_reload(project):
    """flush 이후 서버를 다시 읽어도(=새로고침) 마지막 대본이 그대로다."""
    client, pid = project
    _flush(client, pid, "마지막까지 친 글자")
    reloaded = client.get(f"/api/projects/{pid}").json()["scenes"][0]
    assert reloaded["narration_text"] == "마지막까지 친 글자"
