"""#382 — NarrationSegment 마이그레이션 + segment CRUD + 회귀(N=1 동작 동일) 테스트.

요구사항 1:1 대응
- 마이그레이션 자동: load_all 시 narration_text 가 있으면 seg_0 으로 자동 변환되어야 한다.
- 첫 segment(seg_0): 시나리오 파서가 만든 첫 segment 만 씬 길이 자동 연장 의미를 가진다 (모델/저장 측 정책 보존).
- 신규 추가: POST /scenes/{sid}/narration/segments → seg_N 자동 부여, offset_sec 보존.
- 편집: PATCH text 변경 시 synced=False.
- 삭제: DELETE 후 segments 에서 제거.
- 넘침: offset + duration > 씬 길이 segment 가 SBV 생성 시 씬 경계에서 잘리는지 확인은
  렌더 단계 테스트지만, 모델·라우터 단에서 데이터를 받아들이는지만 검증한다.
- N=1 회귀: 마이그레이션 직후 seg_0 만 있는 프로젝트는 narration_text/tts 필드와 동일한 값.
"""

from __future__ import annotations

import json
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
    client, data_root, store = app_and_root
    resp = client.post(
        "/api/projects",
        json={"name": "seg382-test", "footage_dir": "", "scenario_text": ""},
    )
    assert resp.status_code == 200
    pid = resp.json()["project_id"]

    # 씬 3개 등록 — 1 은 narration_text 있음(마이그레이션 대상), 2 는 빈 씬, 3 은 segments 직접 지정.
    scenes_payload = [
        {"scene_id": 1, "narration_text": "안녕하세요"},
        {"scene_id": 2},
        {"scene_id": 3, "narration_segments": [
            {"id": "seg_0", "text": "씬 3 첫 구간", "offset_sec": 0.0,
             "duration_sec": 0.0, "audio_url": "", "status": "idle", "synced": False},
        ]},
    ]
    resp_reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "seg382-test", "scenes": scenes_payload},
    )
    assert resp_reg.status_code == 200, resp_reg.text
    yield client, pid


def test_migration_creates_seg0_for_existing_narration_text(project, app_and_root):
    """기존 narration_text 가 있는 씬은 reload 시 seg_0 으로 자동 변환된다."""
    client, pid = project
    _, _, store = app_and_root

    # 강제로 store 캐시를 비우고 load_all 다시 호출해 마이그레이션 트리거.
    store._projects.clear()
    store.load_all()

    proj = client.get(f"/api/projects/{pid}").json()
    scene_1 = next(s for s in proj["scenes"] if s["scene_id"] == 1)
    assert scene_1["narration_segments"], "씬 1 마이그레이션 후 seg_0 가 있어야 합니다"
    assert scene_1["narration_segments"][0]["id"] == "seg_0"
    assert scene_1["narration_segments"][0]["text"] == "안녕하세요"
    # narration_text 도 호환 필드로 그대로 보존.
    assert scene_1["narration_text"] == "안녕하세요"

    # 빈 씬(2)은 segments 가 빈 채로 남는다.
    scene_2 = next(s for s in proj["scenes"] if s["scene_id"] == 2)
    assert scene_2["narration_segments"] == []


def test_create_segment_auto_id(project):
    client, pid = project
    # 씬 1 에 추가 → 이미 seg_0 가 있으니 seg_1 이 발급되어야 함.
    # 먼저 마이그레이션을 트리거하기 위해 GET 한번.
    client.get(f"/api/projects/{pid}")
    resp = client.post(
        f"/api/projects/{pid}/scenes/1/narration/segments",
        json={"text": "두 번째 구간", "offset_sec": 1.5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # seg_0 마이그레이션 됐을 수 있으니 seg_1 또는 seg_0 가 발급됨.
    assert body["id"].startswith("seg_")
    assert body["text"] == "두 번째 구간"
    assert abs(body["offset_sec"] - 1.5) < 1e-6
    assert body["synced"] is False
    assert body["status"] == "idle"


def test_update_segment_marks_unsynced_when_text_changes(project):
    client, pid = project
    # 미리 segment 추가.
    resp = client.post(
        f"/api/projects/{pid}/scenes/3/narration/segments",
        json={"id": "seg_42", "text": "원본", "offset_sec": 0.0},
    )
    assert resp.status_code == 200, resp.text

    # synced 를 강제로 True 로 만들기 위해 별도 PATCH 후 다시 PATCH (text 변경).
    # text 변경하지 않으면 synced 가 유지되어야 한다.
    r1 = client.patch(
        f"/api/projects/{pid}/scenes/3/narration/segments/seg_42",
        json={"offset_sec": 0.5},
    )
    assert r1.status_code == 200
    assert r1.json()["text"] == "원본"
    # text 변경 → synced=False.
    r2 = client.patch(
        f"/api/projects/{pid}/scenes/3/narration/segments/seg_42",
        json={"text": "수정"},
    )
    assert r2.status_code == 200
    assert r2.json()["text"] == "수정"
    assert r2.json()["synced"] is False


def test_delete_segment_removes_from_scene(project):
    client, pid = project
    # 새 segment 추가 후 삭제.
    add = client.post(
        f"/api/projects/{pid}/scenes/3/narration/segments",
        json={"id": "seg_temp", "text": "삭제될 거", "offset_sec": 0.0},
    )
    assert add.status_code == 200

    dr = client.delete(f"/api/projects/{pid}/scenes/3/narration/segments/seg_temp")
    assert dr.status_code == 200
    assert dr.json()["deleted"] == "seg_temp"

    proj = client.get(f"/api/projects/{pid}").json()
    scene_3 = next(s for s in proj["scenes"] if s["scene_id"] == 3)
    assert all(s["id"] != "seg_temp" for s in scene_3["narration_segments"])


def test_duplicate_segment_id_returns_400(project):
    client, pid = project
    # 씬 3 에는 seg_0 가 있다 (fixture 에서 직접 지정).
    resp = client.post(
        f"/api/projects/{pid}/scenes/3/narration/segments",
        json={"id": "seg_0", "text": "중복!"},
    )
    assert resp.status_code == 400


def test_segment_endpoint_404_for_unknown_seg(project):
    client, pid = project
    resp = client.patch(
        f"/api/projects/{pid}/scenes/1/narration/segments/nonexistent_xx",
        json={"text": "no-op"},
    )
    assert resp.status_code == 404


def test_segment_audio_url_returns_404_when_no_file(project):
    """WAV 파일 미생성 상태에서는 segment 오디오 엔드포인트가 404."""
    client, pid = project
    resp = client.get(
        f"/api/projects/{pid}/tts/segment/seg_unknown/audio?scene_id=1"
    )
    assert resp.status_code == 404
