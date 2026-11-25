"""전체 TTS 재생성 — 세그먼트 voice_name 정책 검증.

요구사항 1:1 대응
- (1) 세그먼트에 개별 voice_name 이 지정되어 있으면 그 목소리로 재합성 대상이 선택된다.
- (2) voice_name 이 None 인 세그먼트는 프로젝트 tts_voice 로 재합성 대상이 선택된다.
- 전체 재생성이 씬의 seg_0 뿐 아니라 seg_1 등 모든 활성(텍스트 있는) 세그먼트를 재합성한다.

실제 Google TTS 호출은 monkeypatch 로 대체하고, 각 세그먼트가 어떤 voice_name 으로
합성 요청되는지만 기록해 정책을 검증한다.
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
    client, data_root, store = app_and_root
    resp = client.post(
        "/api/projects",
        json={"name": "regen-tts-test", "footage_dir": "", "scenario_text": ""},
    )
    assert resp.status_code == 200
    pid = resp.json()["project_id"]

    # 프로젝트 기본 목소리 설정.
    r_voice = client.patch(f"/api/projects/{pid}/tts/voice", json={"voice_name": "ko-KR-PROJECT"})
    assert r_voice.status_code == 200

    # 씬 1: seg_0(voice 미지정) + seg_1(voice 지정) — 둘 다 재합성 대상.
    scenes_payload = [
        {
            "scene_id": 1,
            "narration_segments": [
                {"id": "seg_0", "text": "기본 목소리 구간", "offset_sec": 0.0,
                 "voice_name": None, "status": "idle", "synced": False},
                {"id": "seg_1", "text": "지정 목소리 구간", "offset_sec": 2.0,
                 "voice_name": "ko-KR-CUSTOM", "status": "idle", "synced": False},
            ],
        },
    ]
    resp_reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "regen-tts-test", "scenes": scenes_payload},
    )
    assert resp_reg.status_code == 200, resp_reg.text
    yield client, pid, store


def test_regenerate_all_respects_segment_voice_policy(project, monkeypatch):
    """전체 재생성 시 각 세그먼트가 규약대로 목소리를 선택하는지 검증.

    - seg_0(voice_name=None) → 프로젝트 목소리 ko-KR-PROJECT
    - seg_1(voice_name=ko-KR-CUSTOM) → 지정 목소리 존중
    """
    client, pid, store = project

    calls: list[tuple[str, str]] = []  # (text, voice_name)

    def fake_tts(text, out_path, voice_name=None, emotion=None):
        calls.append((text, voice_name))
        # 합성 산출물 흉내 — WAV 파일을 실제로 생성해 이후 존재 검사를 통과시킨다.
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"RIFF....WAVEfake")
        return 1.5  # duration_sec

    import src.narration_generator as ng
    monkeypatch.setattr(ng, "_tts", fake_tts)

    from web.backend.routers import tts as tts_router

    # voice_name 미전달(None) → 서버가 프로젝트 tts_voice 를 effective_voice 로 사용.
    tts_router._do_generate_tts(pid, [1], force=True, task_id="test-task", voice_name=None)

    # 두 세그먼트 모두 재합성 대상이어야 한다(모든 활성 세그먼트 재합성).
    assert len(calls) == 2, f"모든 활성 세그먼트가 재합성되어야 함: {calls}"

    voices = {text: voice for text, voice in calls}
    # (1) 개별 지정 세그먼트는 지정 목소리 존중.
    assert voices["지정 목소리 구간"] == "ko-KR-CUSTOM"
    # (2) 미지정(None) 세그먼트는 프로젝트 목소리.
    assert voices["기본 목소리 구간"] == "ko-KR-PROJECT"


def test_regenerate_all_updates_segment_meta(project, monkeypatch):
    """재생성 후 각 세그먼트 duration_sec/synced/status 가 서버 store 에 갱신된다."""
    client, pid, store = project

    def fake_tts(text, out_path, voice_name=None, emotion=None):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"RIFF....WAVEfake")
        return 2.25

    import src.narration_generator as ng
    monkeypatch.setattr(ng, "_tts", fake_tts)

    from web.backend.routers import tts as tts_router
    tts_router._do_generate_tts(pid, [1], force=True, task_id="test-task-2", voice_name=None)

    proj = client.get(f"/api/projects/{pid}").json()
    scene = next(s for s in proj["scenes"] if s["scene_id"] == 1)
    segs = {s["id"]: s for s in scene["narration_segments"]}
    for sid in ("seg_0", "seg_1"):
        assert segs[sid]["status"] == "ready", segs[sid]
        assert segs[sid]["synced"] is True
        assert segs[sid]["duration_sec"] == pytest.approx(2.25)
