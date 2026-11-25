"""#404 — 씬 재번호/삽입/삭제로 어긋난 narration WAV 잔재 차단.

요구사항 1:1 대응
- 재번호 시 wav 잔재가 다음 register 호출로 즉시 삭제되어야 한다 (text 변경 감지).
- 신규 segment 가 들어왔는데 같은 이름의 wav 가 이미 디스크에 있으면(이전 씬 잔재 가능성)
  synced=False 로 들어온 경우 차단(삭제)해야 한다.
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


def _create_project(client, name: str = "stale404") -> str:
    resp = client.post(
        "/api/projects",
        json={"name": name, "footage_dir": "", "scenario_text": ""},
    )
    assert resp.status_code == 200
    return resp.json()["project_id"]


def _write_fake_wav(narr_dir: Path, filename: str, data: bytes = b"FAKEWAV") -> Path:
    narr_dir.mkdir(parents=True, exist_ok=True)
    path = narr_dir / filename
    path.write_bytes(data)
    return path


def test_text_change_purges_stale_segment_wav(app_and_store):
    """segment.text 가 register 호출 시 바뀌면 그 segment 의 wav 파일이 즉시 삭제되고
    새 segment 의 synced 가 False 로 보정되어야 한다 (#404 핵심 시나리오)."""
    client, store = app_and_store
    pid = _create_project(client)

    # 1) 초기 등록 — 씬 16번에 segment seg_0 = "아저씨를 파티원으로 영입합시다."
    initial = [
        {
            "scene_id": 16,
            "narration_segments": [
                {
                    "id": "seg_0",
                    "text": "아저씨를 파티원으로 영입합시다.",
                    "offset_sec": 0.0,
                    "duration_sec": 1.5,
                    "audio_url": f"/api/projects/{pid}/tts/segment/seg_0/audio",
                    "status": "ready",
                    "synced": True,
                },
            ],
        }
    ]
    r = client.put(f"/api/projects/{pid}/register", json={"scenes": initial})
    assert r.status_code == 200

    # 2) 디스크 wav 파일을 흉내냄 (TTS 실생성 대신 fake 바이트).
    narr_dir = store.get_output_dir(pid) / "narration"
    wav_path = _write_fake_wav(narr_dir, "scene_016_seg_seg_0.wav")
    assert wav_path.exists()

    # 3) 사용자가 씬을 삽입해 옛 16번이 17번이 되고 새 16번이 들어옴 → 16번 text 가 바뀜.
    after_insert = [
        {
            "scene_id": 16,
            "narration_segments": [
                {
                    "id": "seg_0",
                    "text": "새로운 16번 씬 텍스트",  # text 변경 — stale 신호
                    "offset_sec": 0.0,
                    "duration_sec": 1.0,
                    "audio_url": f"/api/projects/{pid}/tts/segment/seg_0/audio",
                    "status": "ready",
                    "synced": True,  # 클라이언트는 잘못 알고 있음
                },
            ],
        }
    ]
    r2 = client.put(f"/api/projects/{pid}/register", json={"scenes": after_insert})
    assert r2.status_code == 200

    # 4) 검증: 옛 wav 파일은 삭제되어야 한다 (잘못된 음성 재생 차단).
    assert not wav_path.exists(), (
        "text 가 변경됐는데 stale wav 가 그대로 남아있다 — #404 회귀"
    )

    # 5) 검증: 저장된 segment 의 synced 가 False 로 강제되어야 한다.
    p = store.get_project(pid)
    assert p is not None
    scene16 = next(s for s in p.scenes if s.scene_id == 16)
    assert scene16.narration_segments, "segments 가 보존되어야 함"
    seg0 = scene16.narration_segments[0]
    assert seg0.synced is False, "stale 감지 시 synced=False 로 강제되어야 함"
    assert seg0.status == "idle", "stale 감지 시 status=idle 로 리셋되어야 함"


def test_new_segment_with_existing_wav_is_purged_when_unsynced(app_and_store):
    """기존에 없던 segment_id 가 들어왔는데 같은 이름의 wav 파일이 디스크에 잔재로
    남아있고 클라이언트가 synced=False 로 보내면 잔재 wav 가 삭제되어야 한다."""
    client, store = app_and_store
    pid = _create_project(client)

    # 1) 우선 빈 씬 등록.
    r = client.put(
        f"/api/projects/{pid}/register",
        json={"scenes": [{"scene_id": 5, "narration_segments": []}]},
    )
    assert r.status_code == 200

    # 2) 디스크에 옛 wav 잔재 (이전 동명 segment 가 있었던 흔적).
    narr_dir = store.get_output_dir(pid) / "narration"
    wav_path = _write_fake_wav(narr_dir, "scene_005_seg_seg_0.wav")
    assert wav_path.exists()

    # 3) 새 segment 등록 — synced=False (아직 TTS 생성 전).
    r2 = client.put(
        f"/api/projects/{pid}/register",
        json={
            "scenes": [
                {
                    "scene_id": 5,
                    "narration_segments": [
                        {
                            "id": "seg_0",
                            "text": "완전히 새로운 텍스트",
                            "offset_sec": 0.0,
                            "duration_sec": 0.0,
                            "audio_url": "",
                            "status": "idle",
                            "synced": False,
                        }
                    ],
                }
            ]
        },
    )
    assert r2.status_code == 200

    # 4) 신규 segment 인데 동명 wav 잔재가 있었음 → 차단(삭제) 되어야 한다.
    assert not wav_path.exists(), (
        "신규 segment 등록 시 동명 wav 잔재가 보존됐다 — 잘못된 음성 매핑 위험"
    )
