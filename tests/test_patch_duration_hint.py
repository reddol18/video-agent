"""PATCH /scenes/{sid} 의 duration_hint_sec 허용 검증.

켄번스/삽입 이미지 씬 길이 편집 UI 가 보내는 duration_hint_sec 이 서버에 실제로 반영되는지 확인.
- 양수 float → 반영
- 0/음수/비수치 → 무시(기존 값 유지)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_and_root(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from web.backend import state as store
    data_root = tmp_path_factory.mktemp("data")
    store.DATA_DIR = data_root
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()

    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app), data_root, store


@pytest.fixture()
def project(app_and_root):
    client, _data_root, _store = app_and_root
    resp = client.post(
        "/api/projects",
        json={"name": "dur-hint-test", "footage_dir": "", "scenario_text": ""},
    )
    assert resp.status_code == 200
    pid = resp.json()["project_id"]
    resp_reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "dur-hint-test", "scenes": [{"scene_id": 1, "duration_hint_sec": 5.0}]},
    )
    assert resp_reg.status_code == 200, resp_reg.text
    yield client, pid


def test_patch_duration_hint_sec_applied(project):
    client, pid = project
    r = client.patch(f"/api/projects/{pid}/scenes/1", json={"duration_hint_sec": 8.5})
    assert r.status_code == 200, r.text
    assert r.json()["duration_hint_sec"] == 8.5


def test_patch_duration_hint_sec_rejects_nonpositive(project):
    client, pid = project
    for bad in (0, -3, "abc"):
        r = client.patch(f"/api/projects/{pid}/scenes/1", json={"duration_hint_sec": bad})
        assert r.status_code == 200, r.text
        # 잘못된 값은 무시 → 기존값(5.0) 유지.
        assert r.json()["duration_hint_sec"] == 5.0
