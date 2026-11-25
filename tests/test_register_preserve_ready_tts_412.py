"""#412 — register 의 "퇴행 쓰기" 로부터 서버의 정상 TTS 메타를 보호한다.

사고: 브라우저가 낡은 스냅샷(idle/False/0.0)으로 register 를 쏘면 서버의 정상
narration_segments 메타가 지워지고, 렌더 시 `_tts_stale()` 이 전 씬을 stale 로
판정해 유료 TTS 를 전량 재생성했다.

요구사항 1:1 대응
1. 서버 ready/synced/dur>0 + 클라 idle/False/0.0 + 텍스트 동일 → 서버 값 보존
2. 같은 조건이되 텍스트 변경 → 클라 값 수용(stale 처리 정상 동작)
3. 서버 idle + 클라 ready/synced/dur>0 → 클라 값 수용 (#411 회귀 방지)
4. 서버 ready + 디스크 wav 없음 → status 가 강제 idle 로 리셋되지 않을 것 (#409 회귀 방지)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture()
def app_and_store(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from web.backend import state as store
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store._projects.clear()
    store._dump_locks.clear()

    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app), store


def _create_project(client, name: str = "preserve412") -> str:
    resp = client.post(
        "/api/projects",
        json={"name": name, "footage_dir": "", "scenario_text": ""},
    )
    assert resp.status_code == 200
    return resp.json()["project_id"]


def _seg(text: str, *, status="ready", synced=True, dur=2.5, seg_id="seg_0", audio_url="/audio/ok"):
    return {
        "id": seg_id,
        "text": text,
        "offset_sec": 0.0,
        "duration_sec": dur,
        "audio_url": audio_url,
        "status": status,
        "synced": synced,
    }


def _register(client, pid: str, segs: list, *, narration_text: str):
    resp = client.put(
        f"/api/projects/{pid}/register",
        json={
            "scenes": [
                {
                    "scene_id": 1,
                    "narration_text": narration_text,
                    "narration_segments": segs,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


def _server_seg(store, pid: str, seg_id="seg_0"):
    p = store.get_project(pid)
    scene = next(s for s in p.scenes if s.scene_id == 1)
    return next(s for s in (scene.narration_segments or []) if s.id == seg_id)


TEXT = "궁예가 철원으로 향했다."


def test_1_client_regression_same_text_preserves_server_meta(app_and_store):
    """핵심: 텍스트 동일 + 클라 퇴행 메타 → 서버의 ready/synced/duration 보존."""
    client, store = app_and_store
    pid = _create_project(client)

    _register(client, pid, [_seg(TEXT)], narration_text=TEXT)
    assert _server_seg(store, pid).synced is True

    # 브라우저가 낡은 스냅샷(idle/False/0.0)을 그대로 쏜다.
    _register(
        client, pid,
        [_seg(TEXT, status="idle", synced=False, dur=0.0, audio_url="")],
        narration_text=TEXT,
    )

    seg = _server_seg(store, pid)
    assert seg.status == "ready"
    assert seg.synced is True
    assert seg.duration_sec == pytest.approx(2.5)
    assert seg.audio_url == "/audio/ok"


def test_2_text_change_accepts_client_meta(app_and_store):
    """텍스트가 바뀌면 서버 ready 메타를 되살리지 않는다 → stale 처리 정상."""
    client, store = app_and_store
    pid = _create_project(client)

    _register(client, pid, [_seg(TEXT)], narration_text=TEXT)

    new_text = "궁예가 송악으로 향했다."
    _register(
        client, pid,
        [_seg(new_text, status="idle", synced=False, dur=0.0, audio_url="")],
        narration_text=new_text,
    )

    seg = _server_seg(store, pid)
    assert seg.text == new_text
    assert seg.synced is False
    assert seg.status == "idle"
    assert seg.duration_sec == pytest.approx(0.0)


def test_3_client_newer_meta_wins(app_and_store):
    """#411 회귀 방지 — 서버 idle 이고 클라가 ready/synced/dur>0 이면 클라 값 수용."""
    client, store = app_and_store
    pid = _create_project(client)

    _register(
        client, pid,
        [_seg(TEXT, status="idle", synced=False, dur=0.0, audio_url="")],
        narration_text=TEXT,
    )
    assert _server_seg(store, pid).status == "idle"

    _register(client, pid, [_seg(TEXT, status="ready", synced=True, dur=3.2)], narration_text=TEXT)

    seg = _server_seg(store, pid)
    assert seg.status == "ready"
    assert seg.synced is True
    assert seg.duration_sec == pytest.approx(3.2)


def test_4_no_wav_on_disk_does_not_force_idle(app_and_store):
    """#409 회귀 방지 — 디스크에 wav 가 없어도 클라가 보낸 ready 가 idle 로 리셋되지 않는다."""
    client, store = app_and_store
    pid = _create_project(client)

    narr_dir = store.get_output_dir(pid) / "narration"
    wav = narr_dir / "scene_001_seg_seg_0.wav"
    assert not wav.exists()  # 비동기 generate 중 — wav 아직 없음

    _register(client, pid, [_seg(TEXT, status="ready", synced=True, dur=1.8)], narration_text=TEXT)
    seg = _server_seg(store, pid)
    assert seg.status == "ready"
    assert seg.synced is True

    # 같은 상태로 한 번 더 register 가 들어와도 여전히 ready 유지.
    _register(client, pid, [_seg(TEXT, status="ready", synced=True, dur=1.8)], narration_text=TEXT)
    seg = _server_seg(store, pid)
    assert seg.status == "ready"
    assert seg.synced is True
    assert not wav.exists()
