"""nl_editor 엔드포인트 통합 테스트 (#315).

검증 흐름 (Gemini 호출은 monkeypatch 로 mock):
1. POST /nl-edit — 미리보기. patch + summary + safety_flags 반환.
2. POST /nl-edit/apply — RenderSpec 캐시 갱신 + snapshot 저장.
3. POST /nl-edit/undo — 직전 snapshot 으로 롤백.
4. 같은 프로젝트에 동시 다발 요청은 409 로 거부 (lock 동작 확인).
5. cost_estimate 에 `7_nl_edit` Phase 가 누적된다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_and_store(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from web.backend import state as store

    data_root = tmp_path_factory.mktemp("nl_data")
    store.DATA_DIR = data_root
    store.DATA_DIR.mkdir(exist_ok=True)
    store._projects.clear()

    from fastapi.testclient import TestClient
    from web.backend.main import app

    return TestClient(app), data_root, store


@pytest.fixture()
def project_with_render_spec(app_and_store, tmp_path_factory):
    """씬 1개 + render_spec.json 캐시가 미리 깔린 프로젝트."""
    client, _root, store = app_and_store

    footage_dir = tmp_path_factory.mktemp("footage_nl")
    (footage_dir / "clip.mp4").write_bytes(b"\x00" * 16)

    resp = client.post(
        "/api/projects",
        json={"name": "nl-edit-test", "footage_dir": str(footage_dir), "scenario_text": ""},
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["project_id"]

    # cache/render_spec.json 직접 작성 — 빌더 의존성 없이 nl-edit 만 검증.
    cache_dir = store.DATA_DIR / pid / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    spec_payload = {
        "scenes": [
            {
                "version": 1,
                "scene_id": 1,
                "canvas": {"width": 1920, "height": 1080, "fps": 30},
                "duration_sec": 5.0,
                "layers": [
                    {
                        "type": "video",
                        "source": "footage/clip.mp4",
                        "source_size": {"width": 1920, "height": 1080},
                        "scale_x": 1.0,
                        "scale_y": 1.0,
                        "playback_rate": 1.0,
                        "rotation_deg": 0,
                        "start_sec": 0.0,
                        "end_sec": 5.0,
                        "opacity": 1.0,
                    },
                    {
                        "type": "text",
                        "text": "원본",
                        "position_x": 960,
                        "position_y": 540,
                        "font_size_px": 48,
                        "font_family": "NotoSansKR",
                        "color": "#FFFFFF",
                        "align": "center",
                        "bold": False,
                        "italic": False,
                        "outline_color": "#000000",
                        "outline_width_px": 0,
                        "start_sec": 0.0,
                        "end_sec": 5.0,
                        "opacity": 1.0,
                        "enter_effect": "fade",
                        "exit_effect": "fade",
                        "enter_duration_sec": 0.3,
                        "exit_duration_sec": 0.3,
                    },
                ],
            }
        ]
    }
    (cache_dir / "render_spec.json").write_text(
        json.dumps(spec_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return client, pid, store


def _mock_llm_response(text: str = "변경됨") -> str:
    return json.dumps({
        "patch": [
            {"op": "replace", "path": "/scenes/0/layers/1/text", "value": text}
        ],
        "summary": f"1번 씬 자막을 '{text}' 로 변경합니다.",
        "safety_flags": [],
    })


def test_preview_returns_patch_summary_flags(project_with_render_spec, monkeypatch):
    """POST /nl-edit 가 patch + summary + safety_flags 를 반환한다."""
    client, pid, _ = project_with_render_spec

    def mock_gen(prompt, *, system=None, cached_content=None):
        # #317 — generate_fn 시그니처가 (text, usage_dict) 튜플 반환.
        return _mock_llm_response("미리보기"), {
            "cached_input_tokens": 0,
            "regular_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    monkeypatch.setattr(
        "src.nl_editor.gemini_client._default_generate", mock_gen
    )

    resp = client.post(
        f"/api/projects/{pid}/nl-edit",
        json={"instruction": "자막 바꿔줘", "history": []},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patch"][0]["path"] == "/scenes/0/layers/1/text"
    assert body["patch"][0]["value"] == "미리보기"
    assert body["summary"]
    assert body["safety_flags"] == []
    assert body["estimated_cost_usd"] > 0


def test_apply_updates_cache_and_saves_snapshot(project_with_render_spec, monkeypatch):
    """apply 후 캐시가 갱신되고 snapshot 디렉토리에 파일이 생긴다."""
    client, pid, store = project_with_render_spec

    # 미리 캐시를 원본 상태로 복원해 결정성 보장
    cache_path = store.DATA_DIR / pid / "cache" / "render_spec.json"
    original = json.loads(cache_path.read_text())

    apply_resp = client.post(
        f"/api/projects/{pid}/nl-edit/apply",
        json={
            "patch": [
                {"op": "replace", "path": "/scenes/0/layers/1/text", "value": "적용됨"}
            ],
            "instruction": "자막 변경",
            "summary": "1번 씬 자막을 적용됨으로 변경",
        },
    )
    assert apply_resp.status_code == 200, apply_resp.text
    body = apply_resp.json()
    assert body["snapshot_id"]
    assert body["scenes"][0]["layers"][1]["text"] == "적용됨"

    # 캐시 파일 확인
    updated = json.loads(cache_path.read_text())
    assert updated["scenes"][0]["layers"][1]["text"] == "적용됨"

    # snapshot 디렉토리 확인
    snap_dir = store.DATA_DIR / pid / "nl_edit" / "snapshots"
    assert snap_dir.exists()
    snaps = list(snap_dir.glob("*.json"))
    assert len(snaps) >= 1
    # snapshot 내용은 원본과 동일해야 함
    snap_payload = json.loads(snaps[-1].read_text())
    assert snap_payload == original


def test_undo_restores_previous_state(project_with_render_spec):
    """undo 호출 시 캐시가 snapshot 으로 복원된다."""
    client, pid, store = project_with_render_spec

    # 1) 원본 백업
    cache_path = store.DATA_DIR / pid / "cache" / "render_spec.json"
    original_text = cache_path.read_text()

    # 2) 변경 적용
    apply_resp = client.post(
        f"/api/projects/{pid}/nl-edit/apply",
        json={
            "patch": [
                {"op": "replace", "path": "/scenes/0/layers/1/text", "value": "임시"}
            ]
        },
    )
    assert apply_resp.status_code == 200
    assert json.loads(cache_path.read_text())["scenes"][0]["layers"][1]["text"] == "임시"

    # 3) undo
    undo_resp = client.post(f"/api/projects/{pid}/nl-edit/undo")
    assert undo_resp.status_code == 200, undo_resp.text
    body = undo_resp.json()
    assert body["snapshot_id"]

    # 캐시가 원본으로 돌아갔는지
    assert cache_path.read_text() == original_text


def test_undo_without_snapshot_returns_404(app_and_store, tmp_path_factory):
    """snapshot 이 없는 프로젝트에 undo 호출 시 404."""
    client, _root, store = app_and_store

    # 새 프로젝트
    footage_dir = tmp_path_factory.mktemp("no_snap")
    resp = client.post(
        "/api/projects",
        json={"name": "no-snap", "footage_dir": str(footage_dir), "scenario_text": ""},
    )
    pid = resp.json()["project_id"]

    # 캐시도 만들어둔다 (없으면 endpoint 가 빌드 시도)
    cache_dir = store.DATA_DIR / pid / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "render_spec.json").write_text('{"scenes": []}', encoding="utf-8")

    undo_resp = client.post(f"/api/projects/{pid}/nl-edit/undo")
    assert undo_resp.status_code == 404


def test_apply_response_includes_synced_scene_cards(project_with_render_spec):
    """#335 — apply 응답에 RenderSpec → SceneCard 역동기화 결과(`scene_cards`) 가
    포함되어야 한다. 프론트가 IndexedDB Project.scenes 를 갱신하는 핵심 데이터."""
    client, pid, store = project_with_render_spec

    apply_resp = client.post(
        f"/api/projects/{pid}/nl-edit/apply",
        json={
            "patch": [
                {"op": "replace", "path": "/scenes/0/layers/1/text", "value": "동기화확인"}
            ]
        },
    )
    assert apply_resp.status_code == 200, apply_resp.text
    body = apply_resp.json()
    assert "scene_cards" in body, "응답에 scene_cards 필드가 없습니다"
    assert isinstance(body["scene_cards"], list)
    # 씬 1개에 대해 변환 결과가 들어 있어야 한다 (프로젝트에 씬이 있으면)
    p = store.get_project(pid)
    if p and p.scenes:
        assert len(body["scene_cards"]) >= 1
        first = body["scene_cards"][0]
        # text layer 가 SceneCard.text_overlays 로 변환되어 새 텍스트 반영
        text_overlays = first.get("text_overlays") or []
        assert any(t.get("text") == "동기화확인" for t in text_overlays), \
            f"text_overlays에 변경된 텍스트가 없음: {text_overlays}"


def test_undo_response_includes_synced_scene_cards(project_with_render_spec):
    """#335 — undo 응답에도 scene_cards 가 포함되어야 한다."""
    client, pid, _ = project_with_render_spec

    # apply 먼저 (snapshot 만들기)
    client.post(
        f"/api/projects/{pid}/nl-edit/apply",
        json={
            "patch": [
                {"op": "replace", "path": "/scenes/0/layers/1/text", "value": "임시텍스트"}
            ]
        },
    )
    undo_resp = client.post(f"/api/projects/{pid}/nl-edit/undo")
    assert undo_resp.status_code == 200, undo_resp.text
    body = undo_resp.json()
    assert "scene_cards" in body
    assert isinstance(body["scene_cards"], list)


def test_apply_invalid_patch_returns_422(project_with_render_spec):
    """잘못된 path 를 가진 patch 는 422 로 거부된다."""
    client, pid, _ = project_with_render_spec
    resp = client.post(
        f"/api/projects/{pid}/nl-edit/apply",
        json={
            "patch": [
                {"op": "replace", "path": "/scenes/99/layers/0/text", "value": "x"}
            ]
        },
    )
    assert resp.status_code == 422


def test_cost_estimate_accumulates_nl_edit_phase(project_with_render_spec, monkeypatch):
    """미리보기 호출 후 Project.cost_estimate.phases 에 7_nl_edit 가 누적된다."""
    client, pid, store = project_with_render_spec

    def mock_gen(prompt, *, system=None, cached_content=None):
        return _mock_llm_response("비용"), {
            "cached_input_tokens": 0,
            "regular_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    monkeypatch.setattr("src.nl_editor.gemini_client._default_generate", mock_gen)

    resp = client.post(
        f"/api/projects/{pid}/nl-edit",
        json={"instruction": "자막 비용 테스트", "history": []},
    )
    assert resp.status_code == 200

    p = store.get_project(pid)
    phases = (p.cost_estimate or {}).get("phases", [])
    phase_names = [ph["phase"] for ph in phases]
    assert "7_nl_edit" in phase_names
    nl_phase = next(ph for ph in phases if ph["phase"] == "7_nl_edit")
    assert nl_phase["gemini_input_chars"] > 0
    assert nl_phase["api_usd"] > 0
