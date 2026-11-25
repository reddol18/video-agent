"""업로드 엔드포인트의 client_meta 통합 회귀 테스트 (#308).

요구사항 1:1 대응
- 푸티지 업로드: `client_meta` 동봉 시 `client_meta_trusted=True` 응답
- 푸티지 업로드: `client_meta` 누락/비정상 시 `client_meta_trusted=False`, 파일 저장 정상
- TTS 업로드: WAV + `client_meta` 동봉 시 client_meta 의 duration 신뢰
- TTS 업로드: WAV 단독 시 `wave.open` 측정값 fallback
- studio audio-upload: `label` 쿼리 파라미터가 multipart 추가 후에도 그대로 작동

WAV 더미 데이터는 22050Hz mono short(0초~0.5초)를 직접 생성한다.
백엔드 코드를 mock 없이 호출해 sanity check + 파일 저장이 일관되게 작동하는지 확인.
"""

from __future__ import annotations

import io
import json
import sys
import wave
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def app_and_root(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # state 디렉토리를 임시 폴더로 격리 — 다른 프로젝트 상태 영향 차단.
    from web.backend import state as store
    data_root = tmp_path_factory.mktemp("data")
    store.DATA_DIR = data_root
    store.DATA_DIR.mkdir(exist_ok=True)
    # in-memory 캐시 초기화
    store._projects.clear()

    from fastapi.testclient import TestClient
    from web.backend.main import app
    return TestClient(app), data_root, store


@pytest.fixture()
def project(app_and_root):
    """각 테스트마다 신규 프로젝트 생성. 씬 1~9 미리 등록(tts_upload 가 갱신할 대상)."""
    client, data_root, store = app_and_root
    resp = client.post(
        "/api/projects",
        json={"name": "client-meta-test", "footage_dir": "", "scenario_text": "테스트"},
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["project_id"]

    # tts_upload 는 기존 씬에만 update_scene 가능 → 미리 빈 씬을 등록한다.
    # SceneCard 의 필수 필드(scene_id) 만 채우고 나머지는 기본값.
    scenes_payload = [
        {"scene_id": i, "title": f"테스트 씬 {i}"}
        for i in range(1, 10)
    ]
    resp_reg = client.put(
        f"/api/projects/{pid}/register",
        json={"name": "client-meta-test", "scenes": scenes_payload},
    )
    assert resp_reg.status_code == 200, resp_reg.text

    yield client, pid


def _make_wav_blob(duration_sec: float = 0.5, sample_rate: int = 22050) -> bytes:
    """순수 silence WAV 바이트를 만든다 — wave.open이 정상 길이를 인식하도록."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        n_frames = int(duration_sec * sample_rate)
        w.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


# ── 1) footage_upload: client_meta 있음 → trusted ─────────────────────────

def test_footage_upload_with_client_meta_returns_trusted(project):
    client, pid = project
    # 작은 더미 파일(실제 mp4 디코더가 호출되지 않으므로 내용은 임의)
    fake_video = b"\x00" * 1024
    meta = json.dumps({"width": 1920, "height": 1080, "duration": 5.0, "source": "client"})

    resp = client.post(
        f"/api/projects/{pid}/scenes/1/footage-upload",
        files={"file": ("test.mp4", fake_video, "video/mp4")},
        data={"client_meta": meta},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["client_meta_trusted"] is True
    # 파일이 실제로 저장됐는지
    assert Path(body["path"]).exists()


# ── 2) footage_upload: client_meta 없음 → fallback ────────────────────────

def test_footage_upload_without_client_meta_returns_untrusted(project):
    client, pid = project
    fake_video = b"\x00" * 1024
    resp = client.post(
        f"/api/projects/{pid}/scenes/2/footage-upload",
        files={"file": ("test.mp4", fake_video, "video/mp4")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["client_meta_trusted"] is False


# ── 3) footage_upload: 비정상 client_meta → fallback ─────────────────────

def test_footage_upload_with_invalid_client_meta_returns_untrusted(project):
    client, pid = project
    fake_video = b"\x00" * 1024
    # width 범위 이탈
    bad_meta = json.dumps({"width": 5, "height": 1080, "duration": 5.0})

    resp = client.post(
        f"/api/projects/{pid}/scenes/3/footage-upload",
        files={"file": ("test.mp4", fake_video, "video/mp4")},
        data={"client_meta": bad_meta},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_meta_trusted"] is False


# ── 4) footage_upload: 깨진 JSON → fallback (서버 에러 안 남) ────────────

def test_footage_upload_with_broken_json_does_not_500(project):
    client, pid = project
    fake_video = b"\x00" * 1024
    resp = client.post(
        f"/api/projects/{pid}/scenes/4/footage-upload",
        files={"file": ("test.mp4", fake_video, "video/mp4")},
        data={"client_meta": "{ not json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["client_meta_trusted"] is False


# ── 5) tts_upload: WAV + client_meta → trusted (duration 신뢰) ───────────

def test_tts_upload_wav_with_client_meta_returns_trusted(project):
    client, pid = project
    wav = _make_wav_blob(duration_sec=2.5)
    # 의도적으로 client_meta 의 duration 을 wave 측정값과 다르게 지정해
    # 응답에 어느 쪽 값이 사용되었는지 검증한다.
    meta = json.dumps({"duration": 9.99, "source": "client"})

    resp = client.post(
        f"/api/projects/{pid}/scenes/5/tts-upload",
        files={"file": ("scene_005.wav", wav, "audio/wav")},
        data={"client_meta": meta},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["client_meta_trusted"] is True

    # 씬 state 에 기록된 tts.duration_sec 가 client_meta(9.99) 값인지 확인.
    proj = client.get(f"/api/projects/{pid}").json()
    scene_5 = next(s for s in proj["scenes"] if s["scene_id"] == 5)
    assert scene_5["tts"] is not None
    # client_meta 가 신뢰됐다면 duration_sec ≈ 9.99
    assert abs(scene_5["tts"]["duration_sec"] - 9.99) < 0.01


# ── 6) tts_upload: WAV 단독 → fallback (wave.open 측정값) ────────────────

def test_tts_upload_wav_without_client_meta_uses_wave_fallback(project):
    client, pid = project
    wav = _make_wav_blob(duration_sec=1.2)

    resp = client.post(
        f"/api/projects/{pid}/scenes/6/tts-upload",
        files={"file": ("scene_006.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_meta_trusted"] is False

    proj = client.get(f"/api/projects/{pid}").json()
    scene_6 = next(s for s in proj["scenes"] if s["scene_id"] == 6)
    # wave 측정값 ≈ 1.2초 (silence 22050 frames * 1.2 ≈ 26460)
    assert abs(scene_6["tts"]["duration_sec"] - 1.2) < 0.05


# ── 6b) tts_upload: stale seg_0 세그먼트 WAV 삭제, seg_1+ 보존 ────────────

def test_tts_upload_unlinks_stale_seg0_but_preserves_seg1(project, app_and_root):
    """"전체 재생성"(tts_upload)이 레거시 WAV 를 갱신할 때, 옛 seg_0 세그먼트
    WAV 를 제거해 build_merged_wav 가 새 레거시 WAV 로 폴백되게 한다.
    seg_1 이상(사용자가 추가한 나레이션)은 절대 삭제하지 않는다.
    """
    client, pid = project
    _, _, store = app_and_root

    # tts_upload 는 narration_text 가 비어 있으면 400 으로 거부한다(잔재 누수 차단).
    store.update_scene(pid, 7, narration_text="씬 7 나레이션")

    narration_dir = store.get_output_dir(pid) / "narration"
    narration_dir.mkdir(parents=True, exist_ok=True)

    # 씬 7 에 대한 옛 세그먼트 WAV 들을 미리 배치.
    stale_seg0 = narration_dir / "scene_007_seg_seg_0.wav"
    keep_seg1 = narration_dir / "scene_007_seg_seg_1.wav"
    stale_seg0.write_bytes(_make_wav_blob(duration_sec=3.0))
    keep_seg1.write_bytes(_make_wav_blob(duration_sec=1.0))
    assert stale_seg0.exists() and keep_seg1.exists()

    # 새 목소리 업로드(레거시 경로에만 저장됨).
    wav = _make_wav_blob(duration_sec=2.0)
    resp = client.post(
        f"/api/projects/{pid}/scenes/7/tts-upload",
        files={"file": ("scene_007.wav", wav, "audio/wav")},
    )
    assert resp.status_code == 200, resp.text

    # 레거시 WAV 는 새로 쓰였고, stale seg_0 은 제거, seg_1 은 보존되어야 한다.
    assert (narration_dir / "narration_scene_007.wav").exists()
    assert not stale_seg0.exists(), "stale seg_0 세그먼트 WAV 가 제거되지 않았다"
    assert keep_seg1.exists(), "seg_1 세그먼트 WAV 가 삭제되면 안 된다"


# ── 7) studio audio-upload: label 쿼리 파라미터 회귀 방지 ────────────────

def test_studio_audio_upload_label_query_param_still_works(project, tmp_path):
    """`label` 은 query param. multipart `client_meta` 추가 후에도 정상 작동해야 한다."""
    client, pid = project
    # studio audio-upload 는 p.footage_dir 가 필요 — 임시로 설정
    from web.backend import state as store
    store.update_project(pid, footage_dir=str(tmp_path))

    wav = _make_wav_blob(duration_sec=0.5)
    meta = json.dumps({"duration": 0.5, "source": "client"})

    resp = client.post(
        f"/api/projects/{pid}/studio/audio-upload",
        params={"label": "테스트라벨"},
        files={"file": ("bgm.wav", wav, "audio/wav")},
        data={"client_meta": meta},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    asset = body["asset"]
    assert asset["label"] == "테스트라벨"
    assert asset["asset_type"] == "audio"
    # #312 — client_meta 동봉 시 응답에서 신뢰 모드 진입 여부를 확인 가능해야 한다.
    assert body["client_meta_trusted"] is True


# ── 8) studio audio-upload: client_meta 누락 시 untrusted (#312) ─────────

def test_studio_audio_upload_without_client_meta_returns_untrusted(project, tmp_path):
    """client_meta 미동봉 시 응답 `client_meta_trusted=False`. asset 저장은 그대로."""
    client, pid = project
    from web.backend import state as store
    store.update_project(pid, footage_dir=str(tmp_path))

    wav = _make_wav_blob(duration_sec=0.5)

    resp = client.post(
        f"/api/projects/{pid}/studio/audio-upload",
        params={"label": "라벨없음"},
        files={"file": ("bgm2.wav", wav, "audio/wav")},
        # data 에 client_meta 미동봉
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_meta_trusted"] is False
    assert body["asset"]["asset_type"] == "audio"


# ── 9) studio audio-upload: sample_rate/channels 포함 메타 (#312) ────────

def test_studio_audio_upload_with_sample_rate_returns_trusted(project, tmp_path):
    """duration + sample_rate + channels 동봉 시도 정상 처리.

    `_parse_client_audio_meta` 가 옵셔널 필드까지 보존하지만 응답에는 trusted 만
    실리므로, 이 테스트는 옵셔널 필드 동봉이 신뢰 모드를 깨뜨리지 않음을 보장.
    """
    client, pid = project
    from web.backend import state as store
    store.update_project(pid, footage_dir=str(tmp_path))

    wav = _make_wav_blob(duration_sec=0.5)
    meta = json.dumps(
        {"duration": 0.5, "sample_rate": 48000, "channels": 2, "source": "client"}
    )

    resp = client.post(
        f"/api/projects/{pid}/studio/audio-upload",
        params={"label": "스테레오"},
        files={"file": ("bgm3.wav", wav, "audio/wav")},
        data={"client_meta": meta},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_meta_trusted"] is True
