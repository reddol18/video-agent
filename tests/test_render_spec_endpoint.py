"""GET /api/projects/{id}/scenes/{scene_id}/render-spec 엔드포인트 테스트 (#309).

검증 흐름:
1. 단순 씬(영상 1개 + fit=contain + speed=1.0) → layers=video 1개, 단순 모드 합치.
2. cover/stretch 도 layers=video 1개 — 단순 판정에 포함.
3. text_overlay 가 추가되면 layers=video+text → 비단순(백엔드 fallback 트리거).
4. 잘못된 scene_id → 404.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_and_store(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from web.backend import state as store

    data_root = tmp_path_factory.mktemp("rs_data")
    store.DATA_DIR = data_root
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()

    from fastapi.testclient import TestClient
    from web.backend.main import app

    return TestClient(app), data_root, store


def _make_project_with_clip_scene(client, footage_dir: Path, fit: str = "contain", speed: float = 1.0):
    """clip 효과만 가진 단순 씬 1개로 프로젝트 생성."""
    (footage_dir / "clip.mp4").write_bytes(b"\x00" * 16)
    resp = client.post(
        "/api/projects",
        json={"name": "rs-test", "footage_dir": str(footage_dir), "scenario_text": ""},
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["project_id"]

    # 씬 1개 등록 — register 로 한 번에 푸시
    scene = {
        "scene_id": 1,
        "title": "테스트",
        "description": "단순 씬",
        "narration_text": "안녕하세요",
        "duration_hint_sec": 5.0,
        "footage_speed": speed,
        "footage": {
            "type": "clip",
            "file": "clip.mp4",
            "fit": fit,
            "rotation": 0,
        },
        "text_overlays": [],
        "shape_overlays": [],
        "graphic_overlays": [],
        "mosaic_overlays": [],
    }
    rr = client.put(f"/api/projects/{pid}/register", json={"name": "rs-test", "scenes": [scene]})
    assert rr.status_code == 200, rr.text
    return pid


def test_render_spec_simple_scene_returns_single_video_layer(app_and_store, tmp_path_factory):
    """단순 씬 — layers 가 video 1개여야 한다."""
    client, _root, _store = app_and_store
    footage_dir = tmp_path_factory.mktemp("footage_rs_simple")
    pid = _make_project_with_clip_scene(client, footage_dir, fit="contain", speed=1.0)

    resp = client.get(f"/api/projects/{pid}/scenes/1/render-spec")
    assert resp.status_code == 200, resp.text
    spec = resp.json()
    assert spec["scene_id"] == 1
    assert spec["canvas"]["width"] > 0
    assert spec["canvas"]["height"] > 0
    assert len(spec["layers"]) == 1
    assert spec["layers"][0]["type"] == "video"
    # 단순 씬 판정 조건 확인
    vl = spec["layers"][0]
    assert vl.get("opacity", 1.0) == 1.0
    assert vl.get("rotation_deg", 0) == 0


def test_render_spec_cover_and_stretch_still_simple(app_and_store, tmp_path_factory):
    """fit=cover/stretch 도 단순 씬 — layers 는 여전히 video 1개."""
    client, _root, _store = app_and_store
    for fit in ("cover", "stretch"):
        footage_dir = tmp_path_factory.mktemp(f"footage_rs_{fit}")
        pid = _make_project_with_clip_scene(client, footage_dir, fit=fit, speed=1.0)
        resp = client.get(f"/api/projects/{pid}/scenes/1/render-spec")
        assert resp.status_code == 200, resp.text
        spec = resp.json()
        assert len(spec["layers"]) == 1
        assert spec["layers"][0]["type"] == "video"


def test_render_spec_speed_preserved_in_playback_rate(app_and_store, tmp_path_factory):
    """footage_speed 가 playback_rate 로 그대로 매핑된다."""
    client, _root, _store = app_and_store
    footage_dir = tmp_path_factory.mktemp("footage_rs_speed")
    pid = _make_project_with_clip_scene(client, footage_dir, fit="contain", speed=2.0)
    resp = client.get(f"/api/projects/{pid}/scenes/1/render-spec")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["layers"][0]["playback_rate"] == 2.0


def test_render_spec_text_overlay_adds_text_layer(app_and_store, tmp_path_factory):
    """text_overlay 가 있으면 layers 가 2개 — 비단순 모드(백엔드 fallback)."""
    client, _root, _store = app_and_store
    footage_dir = tmp_path_factory.mktemp("footage_rs_text")
    (footage_dir / "clip.mp4").write_bytes(b"\x00" * 16)
    resp = client.post(
        "/api/projects",
        json={"name": "rs-text-test", "footage_dir": str(footage_dir), "scenario_text": ""},
    )
    pid = resp.json()["project_id"]
    scene = {
        "scene_id": 1,
        "title": "텍스트 있음",
        "description": "비단순 씬",
        "narration_text": "n",
        "duration_hint_sec": 5.0,
        "footage": {"type": "clip", "file": "clip.mp4", "fit": "contain"},
        "text_overlays": [
            {
                "id": "t1",
                "text": "안녕",
                "x": 0.5,
                "y": 0.1,
                "font_size": 40,
                "color": "#FFFFFF",
                "start_sec": 0.0,
                "end_sec": None,
            }
        ],
        "shape_overlays": [],
        "graphic_overlays": [],
        "mosaic_overlays": [],
    }
    rr = client.put(f"/api/projects/{pid}/register", json={"name": "x", "scenes": [scene]})
    assert rr.status_code == 200, rr.text

    sr = client.get(f"/api/projects/{pid}/scenes/1/render-spec")
    assert sr.status_code == 200
    spec = sr.json()
    # video + text
    assert len(spec["layers"]) >= 2
    types = [layer["type"] for layer in spec["layers"]]
    assert "video" in types
    assert "text" in types


def test_render_spec_404_on_missing_scene(app_and_store, tmp_path_factory):
    """존재하지 않는 scene_id → 404."""
    client, _root, _store = app_and_store
    footage_dir = tmp_path_factory.mktemp("footage_rs_missing")
    pid = _make_project_with_clip_scene(client, footage_dir, fit="contain", speed=1.0)
    resp = client.get(f"/api/projects/{pid}/scenes/999/render-spec")
    assert resp.status_code == 404


def test_render_spec_404_on_missing_project(app_and_store):
    """존재하지 않는 project_id → 404."""
    client, _root, _store = app_and_store
    resp = client.get("/api/projects/does-not-exist/scenes/1/render-spec")
    assert resp.status_code == 404
