"""다중 화자 — 씬 태그 speaker:{name} 으로 TTS 목소리를 고르는 기능.

핵심 회귀 방지 항목:
  - 화자 태그가 없으면 기존과 100% 동일하게 프로젝트 기본 목소리를 쓴다.
  - segment 개별 지정이 화자 태그를 이긴다 (좁은 범위 우선).
  - 미등록 이름은 TTS 를 실패시키지 않고 기본 목소리로 떨어진다.
  - 전체 프로젝트 sync(PUT /register)가 화자 사전을 지우지 않는다.
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
    pid = client.post("/api/projects", json={"name": "speaker-test"}).json()["project_id"]
    client.put(f"/api/projects/{pid}/register", json={
        "name": "speaker-test",
        "tts_voice": "BASE_VOICE",
        "scenes": [
            {"scene_id": 1, "title": "A", "narration_text": "첫 대사"},
            {"scene_id": 2, "title": "B", "narration_text": "둘째 대사"},
        ],
    })
    yield client, pid


# ── 순수 규칙 ───────────────────────────────────────────────────────────────

def test_extract_speaker_name_ignores_effect_tags():
    from web.backend.speakers import extract_speaker_name
    assert extract_speaker_name(["speaker:홍길동", "horror"]) == "홍길동"
    assert extract_speaker_name(["horror", "witty"]) is None
    assert extract_speaker_name(["speaker:"]) is None
    # 대소문자·공백 무시
    assert extract_speaker_name(["Speaker:  철수  "]) == "철수"
    # 씬당 1명 — 첫 번째를 쓴다
    assert extract_speaker_name(["speaker:철수", "speaker:영희"]) == "철수"


def test_resolve_priority_segment_beats_speaker_tag():
    from web.backend.models import NarrationSegment, Project, SceneCard, Speaker
    from web.backend.speakers import resolve_scene_voice, resolve_segment_voice

    p = Project(project_id="x", name="t", tts_voice="BASE",
                speakers=[Speaker(name="홍길동", voice_name="VOICE_A")])
    scene = SceneCard(scene_id=1, tags=["speaker:홍길동"])

    assert resolve_scene_voice(scene, p) == ("VOICE_A", "홍길동")
    # segment 지정이 가장 좁으므로 이긴다
    assert resolve_segment_voice(NarrationSegment(id="seg_0", voice_name="SEG_V"), scene, p) == "SEG_V"
    # 없으면 화자 목소리를 물려받는다
    assert resolve_segment_voice(NarrationSegment(id="seg_0"), scene, p) == "VOICE_A"


def test_unknown_speaker_falls_back_to_default():
    """오타 하나로 TTS 가 통째로 실패하면 안 된다 — 기본 목소리로 떨어지되 이름은 돌려준다."""
    from web.backend.models import Project, SceneCard, Speaker
    from web.backend.speakers import resolve_scene_voice

    p = Project(project_id="x", name="t", tts_voice="BASE",
                speakers=[Speaker(name="홍길동", voice_name="VOICE_A")])
    voice, name = resolve_scene_voice(SceneCard(scene_id=1, tags=["speaker:홍길똥"]), p)
    assert voice == "BASE"
    assert name == "홍길똥"


def test_no_speaker_tag_is_unchanged_behavior():
    from web.backend.models import Project, SceneCard
    from web.backend.speakers import resolve_scene_voice
    p = Project(project_id="x", name="t", tts_voice="BASE")
    assert resolve_scene_voice(SceneCard(scene_id=1, tags=["horror"]), p) == ("BASE", None)


# ── API ─────────────────────────────────────────────────────────────────────

def test_put_and_list_speakers(project):
    client, pid = project
    r = client.put(f"/api/projects/{pid}/speakers", json={"speakers": [
        {"name": "홍길동", "voice_name": "VOICE_A", "note": "주인공"},
    ]})
    assert r.status_code == 200, r.text
    got = client.get(f"/api/projects/{pid}/speakers").json()
    assert [s["name"] for s in got["speakers"]] == ["홍길동"]
    assert got["default_voice"] == "BASE_VOICE"


def test_list_reports_scene_usage_and_unknown_names(project):
    client, pid = project
    client.put(f"/api/projects/{pid}/speakers", json={"speakers": [
        {"name": "홍길동", "voice_name": "VOICE_A"},
    ]})
    client.patch(f"/api/projects/{pid}/scenes/1", json={"tags": ["speaker:홍길동"]})
    client.patch(f"/api/projects/{pid}/scenes/2", json={"tags": ["speaker:오타난이름"]})

    got = client.get(f"/api/projects/{pid}/speakers").json()
    assert got["speakers"][0]["scene_ids"] == [1]
    assert got["unknown_speakers"] == [{"name": "오타난이름", "scene_ids": [2]}]


def test_duplicate_names_rejected(project):
    client, pid = project
    r = client.put(f"/api/projects/{pid}/speakers", json={"speakers": [
        {"name": "철수", "voice_name": "A"},
        {"name": "철수 ", "voice_name": "B"},   # 공백만 다름 → 같은 이름
    ]})
    assert r.status_code == 400
    assert "중복" in r.json()["detail"]


def test_blank_name_or_voice_rejected(project):
    client, pid = project
    assert client.put(f"/api/projects/{pid}/speakers",
                      json={"speakers": [{"name": "  ", "voice_name": "A"}]}).status_code == 400
    assert client.put(f"/api/projects/{pid}/speakers",
                      json={"speakers": [{"name": "철수", "voice_name": " "}]}).status_code == 400


def test_register_sync_preserves_speakers(project):
    """프론트의 주기적 전체 push 가 화자 사전을 지우면 안 된다."""
    client, pid = project
    client.put(f"/api/projects/{pid}/speakers", json={"speakers": [
        {"name": "홍길동", "voice_name": "VOICE_A"},
    ]})
    full = client.get(f"/api/projects/{pid}").json()
    assert client.put(f"/api/projects/{pid}/register", json=full).status_code == 200
    after = client.get(f"/api/projects/{pid}/speakers").json()
    assert [s["name"] for s in after["speakers"]] == ["홍길동"]


def test_speakers_persist_to_state_json(project, app_and_root):
    import json
    client, pid = project
    _, data_root, _ = app_and_root
    client.put(f"/api/projects/{pid}/speakers", json={"speakers": [
        {"name": "영희", "voice_name": "VOICE_B"},
    ]})
    state = json.loads((Path(data_root) / pid / "state.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in state["speakers"]] == ["영희"]
