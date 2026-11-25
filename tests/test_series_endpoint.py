"""시리즈 목록 라우터(/api/series) 테스트.

검증 흐름:
1. 생성 → GET 목록에 나타난다. `_series/series.json` 에 저장된다.
2. 같은 이름 재생성 → 멱등(기존 항목 반환, 목록 개수 그대로).
3. 삭제 → 그 시리즈에 속한 프로젝트의 series 가 빈 문자열(미편성)로 되돌아간다.
4. 이름 변경 → 소속 프로젝트의 series 값도 새 이름으로 일괄 갱신된다.
5. `_series` 폴더가 state.load_all() 에서 프로젝트로 오인 로드되지 않는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_and_store(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from web.backend import state as store

    data_root = tmp_path_factory.mktemp("series_data")
    store.DATA_DIR = data_root
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()

    from fastapi.testclient import TestClient
    from web.backend.main import app

    return TestClient(app), data_root, store


def _make_project(store, name="테스트", series=""):
    p = store.create_project(name=name)
    if series:
        store.update_project(p.project_id, series=series)
        p = store.get_project(p.project_id)
    store._dump(p)
    return p


def test_create_list_and_idempotent(app_and_store):
    client, data_root, _store = app_and_store

    r = client.post("/api/series", json={"name": "여행"})
    assert r.status_code == 200
    created = r.json()
    assert created["name"] == "여행"
    assert created["id"]

    # 저장 파일 확인
    series_file = data_root / "_series" / "series.json"
    assert series_file.exists()
    saved = json.loads(series_file.read_text(encoding="utf-8"))
    assert len(saved["series"]) == 1

    # 목록
    lst = client.get("/api/series").json()
    assert [s["name"] for s in lst] == ["여행"]

    # 같은 이름 재생성 → 멱등(같은 id 반환, 개수 유지)
    r2 = client.post("/api/series", json={"name": "여행"})
    assert r2.status_code == 200
    assert r2.json()["id"] == created["id"]
    assert len(client.get("/api/series").json()) == 1


def test_empty_name_rejected(app_and_store):
    client, _data_root, _store = app_and_store
    r = client.post("/api/series", json={"name": "   "})
    assert r.status_code == 400


def test_delete_resets_project_series(app_and_store):
    client, _data_root, store = app_and_store
    created = client.post("/api/series", json={"name": "요리"}).json()
    p = _make_project(store, name="김치찌개", series="요리")

    r = client.delete(f"/api/series/{created['id']}")
    assert r.status_code == 200

    # 목록에서 사라짐
    assert client.get("/api/series").json() == []
    # 프로젝트 series 는 미편성(빈 문자열)으로 되돌아감(프로젝트 자체는 유지)
    reloaded = store.get_project(p.project_id)
    assert reloaded is not None
    assert (reloaded.series or "") == ""


def test_rename_updates_member_projects(app_and_store):
    client, _data_root, store = app_and_store
    created = client.post("/api/series", json={"name": "여행"}).json()
    p1 = _make_project(store, name="제주", series="여행")
    p2 = _make_project(store, name="부산", series="여행")
    other = _make_project(store, name="독립", series="")

    r = client.patch(f"/api/series/{created['id']}", json={"name": "국내여행"})
    assert r.status_code == 200
    assert r.json()["name"] == "국내여행"

    assert store.get_project(p1.project_id).series == "국내여행"
    assert store.get_project(p2.project_id).series == "국내여행"
    assert (store.get_project(other.project_id).series or "") == ""


def test_series_folder_not_loaded_as_project(app_and_store):
    client, _data_root, store = app_and_store
    # 시리즈를 만들어 _series/series.json 이 생기게 한다.
    client.post("/api/series", json={"name": "여행"})
    # 실제 프로젝트 1개도 만든다.
    p = _make_project(store, name="진짜프로젝트")

    # state 를 비우고 다시 로드 → _series 폴더는 프로젝트로 로드되면 안 된다.
    store._projects.clear()
    store.load_all()

    ids = {pp.project_id for pp in [] } | {pp.project_id for pp in store.list_projects()}
    assert p.project_id in ids
    # _series 폴더는 로드되지 않으므로 프로젝트는 정확히 1개.
    assert len(store.list_projects()) == 1
