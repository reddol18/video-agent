"""POST /api/projects/{id}/match-keyword-client 입력 검증 회귀 테스트 — 이슈 #327.

요구사항(이슈 #327):
  1. scene_id가 프로젝트의 실제 씬에 존재하는지 확인
  2. source_file/filename이 footage_dir 안에 실제로 존재하는 파일인지 확인
  3. chunk_id가 footage_index.json의 chunk 목록에 존재하는지 확인 (인덱스가 있을 때)
  4. 검증 실패 항목은 failed_scene_ids/failure_reasons에 포함되어야 한다
  5. 모든 항목이 실패하면 422를 반환해야 한다
  6. 정상 페이로드는 기존과 동일하게 200 + updated_scene_ids 반환

본 테스트는 fastapi TestClient로 라우터 전체 흐름을 통과시키며 cache/state를
실제로 갱신해 회귀를 잡는다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_and_store(tmp_path_factory):
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
def project_with_index(app_and_store, tmp_path_factory):
    """씬 3개 + footage_dir에 파일 2개 + footage_index.json 1개 chunks를 가진 프로젝트."""
    client, _data_root, store = app_and_store

    # footage_dir 별도 임시 폴더에 생성 (프로젝트 디렉토리와 분리).
    footage_dir = tmp_path_factory.mktemp("footage")
    (footage_dir / "seoul_night.mp4").write_bytes(b"\x00")
    (footage_dir / "busan_beach.mp4").write_bytes(b"\x00")

    resp = client.post(
        "/api/projects",
        json={
            "name": "match-keyword-client-test",
            "footage_dir": str(footage_dir),
            "scenario_text": "",
        },
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["project_id"]

    # 씬 1~3 등록.
    scenes_payload = [
        {"scene_id": i, "title": f"씬{i}", "narration_text": f"씬 {i} 나레이션"}
        for i in range(1, 4)
    ]
    resp_reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "match-keyword-client-test", "scenes": scenes_payload},
    )
    assert resp_reg.status_code == 200, resp_reg.text

    # footage_index.json 직접 작성 — 인덱스 검증 경로를 발동시키기 위해 chunks 2개 등록.
    cache_dir = store.get_cache_dir(pid)
    index = {
        "chunks": [
            {
                "chunk_id": "seoul_c1",
                "source_file": str(footage_dir / "seoul_night.mp4"),
                "start_sec": 0.0,
                "end_sec": 5.0,
            },
            {
                "chunk_id": "busan_c1",
                "source_file": str(footage_dir / "busan_beach.mp4"),
                "start_sec": 0.0,
                "end_sec": 5.0,
            },
        ]
    }
    (cache_dir / "footage_index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )
    yield client, pid, footage_dir


# ── 정상 페이로드 ───────────────────────────────────────────────────────────

def test_save_keyword_match_result_valid_payload_succeeds(project_with_index):
    client, pid, footage_dir = project_with_index
    payload = {
        "items": [
            {
                "scene_id": 1,
                "source_file": str(footage_dir / "seoul_night.mp4"),
                "filename": "seoul_night.mp4",
                "chunk_id": "seoul_c1",
                "trim_start": 0.0,
                "trim_end": 5.0,
                "match_score": 85.0,
                "is_image": False,
            }
        ]
    }
    resp = client.post(f"/api/projects/{pid}/match-keyword-client", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated_scene_ids"] == [1]
    assert body["failed_scene_ids"] == []
    assert body["method"] == "keyword_filename"
    assert body["computed_on"] == "client"


# ── 검증 1) scene_id 미존재 ──────────────────────────────────────────────────

def test_invalid_scene_id_is_rejected(project_with_index):
    client, pid, footage_dir = project_with_index
    payload = {
        "items": [
            {
                "scene_id": 99,  # 존재하지 않는 씬
                "source_file": str(footage_dir / "seoul_night.mp4"),
                "filename": "seoul_night.mp4",
                "chunk_id": "seoul_c1",
                "trim_start": 0.0,
                "trim_end": 5.0,
                "match_score": 85.0,
                "is_image": False,
            }
        ]
    }
    resp = client.post(f"/api/projects/{pid}/match-keyword-client", json=payload)
    # 모든 항목 실패 → 422
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", {})
    assert 99 in detail.get("failed_scene_ids", [])
    reasons = detail.get("failure_reasons", [])
    assert any(r.get("reason") == "scene_not_found" for r in reasons)


# ── 검증 2) filename이 footage_dir에 없음 ────────────────────────────────────

def test_filename_not_in_footage_dir_is_rejected(project_with_index):
    client, pid, _footage_dir = project_with_index
    payload = {
        "items": [
            {
                "scene_id": 2,
                "source_file": "/some/where/missing.mp4",
                "filename": "missing.mp4",  # footage_dir에 없는 파일
                "chunk_id": "",  # fallback 매칭이라 chunk_id는 빈 문자열
                "trim_start": 0.0,
                "trim_end": 0.0,
                "match_score": 50.0,
                "is_image": False,
            }
        ]
    }
    resp = client.post(f"/api/projects/{pid}/match-keyword-client", json=payload)
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", {})
    reasons = detail.get("failure_reasons", [])
    assert any(r.get("reason") == "footage_not_found" for r in reasons)


# ── 검증 3) chunk_id가 footage_index에 없음 ──────────────────────────────────

def test_chunk_id_not_in_index_is_rejected(project_with_index):
    client, pid, footage_dir = project_with_index
    payload = {
        "items": [
            {
                "scene_id": 2,
                "source_file": str(footage_dir / "seoul_night.mp4"),
                "filename": "seoul_night.mp4",
                "chunk_id": "phantom_chunk_999",  # footage_index에 없음
                "trim_start": 0.0,
                "trim_end": 5.0,
                "match_score": 75.0,
                "is_image": False,
            }
        ]
    }
    resp = client.post(f"/api/projects/{pid}/match-keyword-client", json=payload)
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", {})
    reasons = detail.get("failure_reasons", [])
    assert any(r.get("reason") == "chunk_id_not_in_index" for r in reasons)


# ── 검증 4) path traversal — filename에 슬래시 포함 ─────────────────────────

def test_filename_with_path_traversal_is_rejected(project_with_index):
    client, pid, footage_dir = project_with_index
    payload = {
        "items": [
            {
                "scene_id": 2,
                "source_file": str(footage_dir / "seoul_night.mp4"),
                "filename": "../../etc/passwd",  # path traversal 시도
                "chunk_id": "seoul_c1",
                "trim_start": 0.0,
                "trim_end": 5.0,
                "match_score": 50.0,
                "is_image": False,
            }
        ]
    }
    resp = client.post(f"/api/projects/{pid}/match-keyword-client", json=payload)
    assert resp.status_code == 422, resp.text
    detail = resp.json().get("detail", {})
    reasons = detail.get("failure_reasons", [])
    # filename != basename(filename) → invalid_filename
    assert any(r.get("reason") == "invalid_filename" for r in reasons)


# ── 검증 5) 혼합 — 정상 항목 + 실패 항목 함께 보낼 때 부분 처리 ────────────

def test_partial_failure_returns_200_with_failed_ids(project_with_index):
    client, pid, footage_dir = project_with_index
    payload = {
        "items": [
            # 정상 항목 1
            {
                "scene_id": 1,
                "source_file": str(footage_dir / "seoul_night.mp4"),
                "filename": "seoul_night.mp4",
                "chunk_id": "seoul_c1",
                "trim_start": 0.0,
                "trim_end": 5.0,
                "match_score": 85.0,
                "is_image": False,
            },
            # 실패 항목 (scene_id 없음)
            {
                "scene_id": 99,
                "source_file": str(footage_dir / "seoul_night.mp4"),
                "filename": "seoul_night.mp4",
                "chunk_id": "seoul_c1",
                "trim_start": 0.0,
                "trim_end": 5.0,
                "match_score": 85.0,
                "is_image": False,
            },
        ]
    }
    resp = client.post(f"/api/projects/{pid}/match-keyword-client", json=payload)
    # 일부라도 성공하면 200.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert 1 in body["updated_scene_ids"]
    assert 99 in body["failed_scene_ids"]
    reasons = body["failure_reasons"]
    assert any(
        r.get("scene_id") == 99 and r.get("reason") == "scene_not_found"
        for r in reasons
    )
