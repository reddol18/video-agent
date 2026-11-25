"""씬 편집 지시 코멘트 API 검증.

김감독이 씬에 남긴 편집 지시를 저장하고, 클로드 에이전트가 처리 후 완료 이력으로
남기는 전체 수명주기를 확인한다.

핵심 회귀 방지 항목:
  - 전체 프로젝트 sync(PUT /register)가 코멘트를 지우지 않을 것
    (프론트는 IndexedDB-first 라 주기적으로 프로젝트 전체를 push 한다.
     register 의 필드 allow-list 에 comments 가 빠지면 코멘트가 조용히 증발한다.)
  - 완료 처리는 이력을 보존할 것 (삭제가 아니라 status 전이)
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
    resp = client.post("/api/projects", json={"name": "comment-test"})
    assert resp.status_code == 200
    pid = resp.json()["project_id"]
    reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "comment-test", "scenes": [
            {"scene_id": 1, "title": "오프닝", "narration_text": "안녕하세요"},
            {"scene_id": 2, "title": "본론", "narration_text": "본론입니다"},
        ]},
    )
    assert reg.status_code == 200, reg.text
    yield client, pid


def _add(client, pid, scene_id, body):
    r = client.post(f"/api/projects/{pid}/scenes/{scene_id}/comments", json={"body": body})
    assert r.status_code == 200, r.text
    return r.json()


# ── 기본 CRUD ───────────────────────────────────────────────────────────────

def test_create_comment_defaults_to_open(project):
    client, pid = project
    c = _add(client, pid, 1, "컷을 더 짧게 잘라줘")
    assert c["status"] == "open"
    assert c["body"] == "컷을 더 짧게 잘라줘"
    assert c["created_at"]
    assert c["resolved_at"] is None


def test_body_is_trimmed_and_blank_rejected(project):
    client, pid = project
    assert _add(client, pid, 1, "  공백 정리  ")["body"] == "공백 정리"
    r = client.post(f"/api/projects/{pid}/scenes/1/comments", json={"body": "   "})
    assert r.status_code == 400


def test_missing_project_or_scene_404(project):
    client, pid = project
    assert client.post(f"/api/projects/{pid}/scenes/999/comments", json={"body": "x"}).status_code == 404
    assert client.post("/api/projects/nope/scenes/1/comments", json={"body": "x"}).status_code == 404


def test_update_and_delete(project):
    client, pid = project
    c = _add(client, pid, 1, "원문")
    r = client.patch(f"/api/projects/{pid}/scenes/1/comments/{c['id']}", json={"body": "수정본"})
    assert r.status_code == 200 and r.json()["body"] == "수정본"

    assert client.delete(f"/api/projects/{pid}/scenes/1/comments/{c['id']}").status_code == 200
    remaining = client.get(f"/api/projects/{pid}/scenes/1/comments").json()["comments"]
    assert all(x["id"] != c["id"] for x in remaining)


# ── 완료 처리 ───────────────────────────────────────────────────────────────

def test_resolve_records_history_without_deleting(project):
    client, pid = project
    c = _add(client, pid, 1, "자막 폰트 키워줘")
    r = client.post(
        f"/api/projects/{pid}/scenes/1/comments/{c['id']}/resolve",
        json={"resolution_note": "폰트 48 → 64", "resolved_by": "agent"},
    )
    assert r.status_code == 200
    done = r.json()
    assert done["status"] == "done"
    assert done["resolved_by"] == "agent"
    assert done["resolution_note"] == "폰트 48 → 64"
    assert done["resolved_at"]
    # 원문은 이력으로 보존된다 (삭제가 아님)
    assert done["body"] == "자막 폰트 키워줘"
    assert any(x["id"] == c["id"] for x in client.get(f"/api/projects/{pid}/scenes/1/comments").json()["comments"])


def test_double_resolve_conflicts(project):
    client, pid = project
    c = _add(client, pid, 1, "중복 처리 방지")
    assert client.post(f"/api/projects/{pid}/scenes/1/comments/{c['id']}/resolve", json={}).status_code == 200
    # 두 번째 resolve 는 409 — 누가 언제 고쳤는지 이력이 덮이는 것을 막는다
    assert client.post(f"/api/projects/{pid}/scenes/1/comments/{c['id']}/resolve", json={}).status_code == 409


def test_done_comment_is_not_editable(project):
    client, pid = project
    c = _add(client, pid, 1, "완료 후 수정 금지")
    client.post(f"/api/projects/{pid}/scenes/1/comments/{c['id']}/resolve", json={})
    r = client.patch(f"/api/projects/{pid}/scenes/1/comments/{c['id']}", json={"body": "몰래 수정"})
    assert r.status_code == 409


def test_reopen_clears_resolution(project):
    client, pid = project
    c = _add(client, pid, 1, "되돌리기")
    client.post(f"/api/projects/{pid}/scenes/1/comments/{c['id']}/resolve",
                json={"resolution_note": "고침", "resolved_by": "agent"})
    r = client.post(f"/api/projects/{pid}/scenes/1/comments/{c['id']}/reopen")
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    assert r.json()["resolved_at"] is None
    assert r.json()["resolution_note"] is None


# ── 에이전트 소비용 조회 ────────────────────────────────────────────────────

def test_agent_queue_lists_open_with_scene_context(project):
    client, pid = project
    c1 = _add(client, pid, 1, "씬1 지시")
    _add(client, pid, 2, "씬2 지시")
    client.post(f"/api/projects/{pid}/scenes/1/comments/{c1['id']}/resolve", json={})

    q = client.get(f"/api/projects/{pid}/comments/agent-queue").json()
    # 완료된 씬1 코멘트는 큐에서 빠지고, 열린 씬2 만 남는다
    assert q["pending"] == 1
    task = q["tasks"][0]
    assert task["scene_id"] == 2
    assert task["instruction"] == "씬2 지시"
    assert task["scene_context"]["title"] == "본론"
    assert task["resolve_url"].endswith("/resolve")

    # resolve_url 을 그대로 호출하면 완료 처리된다 (에이전트 사용 경로)
    assert client.post(task["resolve_url"], json={"resolution_note": "처리함"}).status_code == 200
    assert client.get(f"/api/projects/{pid}/comments/agent-queue").json()["pending"] == 0


def test_project_comment_list_filters_by_status(project):
    client, pid = project
    c1 = _add(client, pid, 1, "열린 것")
    _add(client, pid, 2, "또 열린 것")
    client.post(f"/api/projects/{pid}/scenes/1/comments/{c1['id']}/resolve", json={})

    assert client.get(f"/api/projects/{pid}/comments", params={"status": "open"}).json()["total"] == 1
    assert client.get(f"/api/projects/{pid}/comments", params={"status": "done"}).json()["total"] == 1
    assert client.get(f"/api/projects/{pid}/comments").json()["total"] == 2


# ── 회귀 방지: 전체 프로젝트 sync ───────────────────────────────────────────

def test_register_without_comments_key_preserves_them(project):
    """코멘트를 모르는 클라이언트가 프로젝트 전체를 push 해도 코멘트가 남아야 한다."""
    client, pid = project
    _add(client, pid, 1, "지워지면 안 됨")
    reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "comment-test", "scenes": [
            {"scene_id": 1, "title": "오프닝", "narration_text": "바뀐 나레이션"},
            {"scene_id": 2, "title": "본론"},
        ]},
    )
    assert reg.status_code == 200
    bodies = [c["body"] for c in client.get(f"/api/projects/{pid}/scenes/1/comments").json()["comments"]]
    assert "지워지면 안 됨" in bodies


def test_register_roundtrips_comments(project):
    """클라이언트가 코멘트를 포함해 push 하면 그대로 유지된다."""
    client, pid = project
    _add(client, pid, 1, "왕복 확인")
    comments = client.get(f"/api/projects/{pid}/scenes/1/comments").json()["comments"]
    reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "comment-test", "scenes": [{"scene_id": 1, "comments": comments}, {"scene_id": 2}]},
    )
    assert reg.status_code == 200
    after = client.get(f"/api/projects/{pid}/scenes/1/comments").json()["comments"]
    assert [c["id"] for c in after] == [c["id"] for c in comments]


def test_comments_persist_to_state_json(project, app_and_root):
    """state.json 에 실제로 기록되어 서버 재기동 후에도 살아남는다."""
    import json
    client, pid = project
    _, data_root, _ = app_and_root
    _add(client, pid, 2, "디스크 확인")
    state = json.loads((Path(data_root) / pid / "state.json").read_text(encoding="utf-8"))
    scene2 = next(s for s in state["scenes"] if s["scene_id"] == 2)
    assert any(c["body"] == "디스크 확인" for c in scene2["comments"])


# ── 붙임 이미지 ─────────────────────────────────────────────────────────────

# 1x1 PNG. 실제 이미지 바이트라야 업로드 경로가 그대로 검증된다.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _upload(client, pid, scene_id, name="shot.png", mime="image/png", data=_PNG_1PX):
    return client.post(
        f"/api/projects/{pid}/scenes/{scene_id}/comment-images",
        files={"file": (name, data, mime)},
    )


def test_upload_assigns_incrementing_index_per_scene(project):
    """자리표시자 번호는 씬 단위로 1부터 증가한다."""
    client, pid = project
    first = _upload(client, pid, 1).json()
    assert first["index"] == 1
    assert first["path"].endswith(first["filename"])

    # 코멘트로 저장돼야 다음 번호 계산에 반영된다.
    client.post(
        f"/api/projects/{pid}/scenes/1/comments",
        json={"body": f"[이미지{first['index']}] 참고", "images": [first]},
    )
    assert _upload(client, pid, 1).json()["index"] == 2


def test_upload_rejects_non_image(project):
    client, pid = project
    r = _upload(client, pid, 1, name="a.json", mime="application/json", data=b"{}")
    assert r.status_code == 400


def test_uploaded_image_is_served_back(project):
    client, pid = project
    img = _upload(client, pid, 1).json()
    got = client.get(img["url"])
    assert got.status_code == 200
    assert got.content == _PNG_1PX


def test_image_file_is_written_to_disk(project, app_and_root):
    client, pid = project
    _, data_root, _ = app_and_root
    img = _upload(client, pid, 1).json()
    on_disk = Path(data_root) / pid / "comment_images" / img["filename"]
    assert on_disk.is_file() and on_disk.read_bytes() == _PNG_1PX


def test_agent_queue_matches_placeholder_to_path(project):
    """에이전트가 [이미지N] → 실제 파일 경로를 바로 얻는다."""
    client, pid = project
    img = _upload(client, pid, 2).json()
    client.post(
        f"/api/projects/{pid}/scenes/2/comments",
        json={"body": f"[이미지{img['index']}] 이 느낌으로", "images": [img]},
    )
    task = next(
        t for t in client.get(f"/api/projects/{pid}/comments/agent-queue").json()["tasks"]
        if t["scene_id"] == 2
    )
    assert task["images"] == [{
        "placeholder": f"[이미지{img['index']}]",
        "index": img["index"],
        "path": img["path"],
        "url": img["url"],
        "mime": "image/png",
    }]
    assert task["missing_image_indexes"] == []


def test_agent_queue_reports_broken_and_unused_references(project):
    """본문만 있고 파일이 없거나, 파일만 있고 본문이 안 쓰는 경우를 구분해 알린다."""
    client, pid = project
    img = _upload(client, pid, 1).json()
    # 본문은 [이미지9] 를 참조하지만 붙임은 index=1 하나뿐.
    client.post(
        f"/api/projects/{pid}/scenes/1/comments",
        json={"body": "[이미지9] 를 참고", "images": [img]},
    )
    task = next(
        t for t in client.get(f"/api/projects/{pid}/comments/agent-queue").json()["tasks"]
        if "[이미지9]" in t["instruction"]
    )
    assert task["missing_image_indexes"] == [9]
    assert [u["index"] for u in task["unreferenced_images"]] == [img["index"]]
    assert task["images"] == []


def test_deleting_comment_removes_its_image_file(project, app_and_root):
    """코멘트를 지우면 임시 폴더에 파일이 남지 않는다."""
    client, pid = project
    _, data_root, _ = app_and_root
    img = _upload(client, pid, 2).json()
    created = client.post(
        f"/api/projects/{pid}/scenes/2/comments",
        json={"body": f"[이미지{img['index']}]", "images": [img]},
    ).json()
    on_disk = Path(data_root) / pid / "comment_images" / img["filename"]
    assert on_disk.is_file()

    client.delete(f"/api/projects/{pid}/scenes/2/comments/{created['id']}")
    assert not on_disk.exists()


def test_update_keeps_images_when_omitted(project):
    """본문만 고칠 때 붙임 이미지가 사라지지 않는다."""
    client, pid = project
    img = _upload(client, pid, 1).json()
    created = client.post(
        f"/api/projects/{pid}/scenes/1/comments",
        json={"body": f"[이미지{img['index']}] 원본", "images": [img]},
    ).json()
    updated = client.patch(
        f"/api/projects/{pid}/scenes/1/comments/{created['id']}",
        json={"body": f"[이미지{img['index']}] 수정본"},
    ).json()
    assert updated["body"].endswith("수정본")
    assert [i["id"] for i in updated["images"]] == [img["id"]]


def test_register_sync_preserves_comment_images(project):
    """프론트의 전체 프로젝트 push(PUT /register)가 붙임 이미지를 지우지 않는다."""
    client, pid = project
    img = _upload(client, pid, 1).json()
    client.post(
        f"/api/projects/{pid}/scenes/1/comments",
        json={"body": f"[이미지{img['index']}] 유지 확인", "images": [img]},
    )
    full = client.get(f"/api/projects/{pid}").json()
    assert client.put(f"/api/projects/{pid}/register", json=full).status_code == 200

    after = client.get(f"/api/projects/{pid}/scenes/1/comments").json()["comments"]
    target = next(c for c in after if "유지 확인" in c["body"])
    assert [i["id"] for i in target["images"]] == [img["id"]]
