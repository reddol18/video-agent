"""BGM 편집 화면이 옛 영상을 보여주지 않는지 (#bgm-preview-cache).

사고: 영상을 다시 렌더한 뒤 BGM 편집에 들어가면 미리보기에 이전 버전이 나왔다.

원인 — `render/output` 은 항상 같은 URL 이고 서버가 cache-control 을 주지 않는다.
브라우저는 etag/last-modified 만 보고 캐시된 mp4 를 계속 재사용한다.

해결 — `render/output-info` 가 파일 mtime 을 돌려주고, 프론트가 그 값을
`?v=` 쿼리로 붙인다. 다시 렌더하면 mtime 이 바뀌므로 URL 이 달라져 새로 받는다.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def client(tmp_path_factory):
    from web.backend import state as store
    store.DATA_DIR = tmp_path_factory.mktemp("data")
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()
    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app), store


def _project(client):
    c, store = client
    pid = c.post("/api/projects", json={"name": "bgm-cache"}).json()["project_id"]
    out = store.DATA_DIR / pid / "output"
    out.mkdir(parents=True, exist_ok=True)
    return c, pid, out


def test_reports_mtime_when_video_exists(client):
    c, pid, out = _project(client)
    (out / "final_output.mp4").write_bytes(b"x" * 2048)
    info = c.get(f"/api/projects/{pid}/render/output-info").json()
    assert info["has_final_mp4"] is True
    assert info["mtime"] > 0
    assert info["size"] == 2048


def test_mtime_zero_when_missing(client):
    c, pid, out = _project(client)
    info = c.get(f"/api/projects/{pid}/render/output-info").json()
    assert info["has_final_mp4"] is False
    assert info["mtime"] == 0


def test_mtime_changes_after_rerender(client):
    """핵심 회귀: 다시 렌더하면 값이 바뀌어야 URL 이 달라지고 캐시가 깨진다."""
    c, pid, out = _project(client)
    f = out / "final_output.mp4"
    f.write_bytes(b"old")
    first = c.get(f"/api/projects/{pid}/render/output-info").json()["mtime"]

    import os
    time.sleep(0.01)
    f.write_bytes(b"new-and-longer")
    future = time.time() + 5
    os.utime(f, (future, future))          # 재렌더 = 새 mtime

    second = c.get(f"/api/projects/{pid}/render/output-info").json()["mtime"]
    assert second != first, "재렌더 후에도 버전이 같으면 브라우저가 옛 영상을 계속 쓴다"


def test_output_endpoint_accepts_version_query(client):
    """`?v=` 를 붙여도 영상이 정상 응답해야 한다."""
    c, pid, out = _project(client)
    (out / "final_output.mp4").write_bytes(b"y" * 4096)
    r = c.get(f"/api/projects/{pid}/render/output?v=12345")
    assert r.status_code == 200
    assert len(r.content) == 4096
