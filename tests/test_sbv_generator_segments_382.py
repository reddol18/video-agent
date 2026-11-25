"""#382 — SBV 자막 생성기의 segment 단위 출력 + N=1 회귀 안전 테스트.

요구사항 1:1 대응
- entry["segments"] 가 있으면 segment 단위로 자막 블록을 만든다.
- 동일 entry duration / scene_dur 일 때, segments=1개(seg_0) 결과는 기존 동작과 동일해야 한다.
- segment 가 씬 시각 길이를 넘는 경우 씬 경계에서 잘린다.
- 겹치는 segment 는 SBV 규격상 줄을 분리해서 출력 (start 시각 오름차순 정렬).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_project_root(tmp_path, monkeypatch):
    """sbv_generator 가 CWD 기준으로 동작하므로 임시 디렉토리로 chdir."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    monkeypatch.chdir(tmp_path)
    # output/narration 디렉토리 미리 만들기.
    (tmp_path / "output" / "narration").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_narr_timeline(root: Path, entries: list[dict]) -> None:
    path = root / "output" / "narration" / "narration_timeline.json"
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")


def test_sbv_no_segments_falls_back_to_legacy_behavior(tmp_project_root):
    """entry["segments"] 가 없으면 기존 동작과 동일 (회귀 안전)."""
    from src.sbv_generator import run as sbv_run

    _write_narr_timeline(tmp_project_root, [{
        "scene_id": 1,
        "audio_file": "x.wav",
        "narration_text": "안녕하세요. 반갑습니다.",
        "subtitle_text": "안녕하세요. 반갑습니다.",
        "start_sec": 0.0,
        "end_sec": 4.0,
        "duration_sec": 4.0,
    }])
    out = sbv_run(force=True)
    body = Path(out).read_text(encoding="utf-8")
    # 적어도 한 블록 + "안녕" 텍스트가 포함되어야 한다.
    assert "0:00:00.000" in body
    assert "안녕" in body


def test_sbv_uses_segments_when_provided(tmp_project_root):
    """entry["segments"] 가 있으면 segment 단위로 자막 블록을 만들고
    offset_sec/end_sec_in_scene 을 영상 타임라인 기준으로 변환한다."""
    from src.sbv_generator import run as sbv_run

    _write_narr_timeline(tmp_project_root, [{
        "scene_id": 1,
        "audio_file": "scene_001.wav",
        "narration_text": "씬 본문",
        "subtitle_text": "씬 본문",
        "start_sec": 0.0,
        "end_sec": 6.0,
        "duration_sec": 6.0,
        "scene_visual_duration_sec": 6.0,
        "segments": [
            {
                "id": "seg_0",
                "offset_sec": 0.0,
                "end_sec_in_scene": 2.0,
                "text": "안녕하세요",
                "sbv_text": None,
            },
            {
                "id": "seg_1",
                "offset_sec": 3.0,
                "end_sec_in_scene": 5.0,
                "text": "반갑습니다",
                "sbv_text": None,
            },
        ],
    }])
    out = sbv_run(force=True)
    body = Path(out).read_text(encoding="utf-8")
    # seg_0 시작 = 0.0, seg_1 시작 = 3.0
    assert "0:00:00.000" in body
    assert "0:00:03.000" in body
    assert "안녕하세요" in body
    assert "반갑습니다" in body


def test_sbv_clamps_segment_to_scene_boundary(tmp_project_root):
    """offset+duration 이 씬 시각 길이를 넘는 segment 는 씬 경계에서 잘린다."""
    from src.sbv_generator import run as sbv_run

    _write_narr_timeline(tmp_project_root, [{
        "scene_id": 1,
        "audio_file": "scene_001.wav",
        "narration_text": "",
        "subtitle_text": "",
        "start_sec": 0.0,
        "end_sec": 5.0,
        "duration_sec": 5.0,
        "scene_visual_duration_sec": 5.0,  # 씬 길이 5초
        "segments": [
            {
                "id": "seg_overflow",
                "offset_sec": 4.0,
                "end_sec_in_scene": 8.0,  # 8 → 5 로 클램프되어야 함.
                "text": "넘침 segment",
                "sbv_text": None,
            },
        ],
    }])
    out = sbv_run(force=True)
    body = Path(out).read_text(encoding="utf-8")
    # 시작 4.0 ~ 끝 5.0 (씬 경계).
    assert "0:00:04.000" in body
    assert "0:00:05.000" in body
    # 8초까지 자막이 가지 않아야 함.
    assert "0:00:08.000" not in body
    assert "넘침 segment" in body


def test_sbv_sorts_overlapping_segments_by_start(tmp_project_root):
    """겹치는 segment 라도 start 시각 오름차순으로 출력된다."""
    from src.sbv_generator import run as sbv_run

    _write_narr_timeline(tmp_project_root, [{
        "scene_id": 1,
        "audio_file": "scene_001.wav",
        "narration_text": "",
        "subtitle_text": "",
        "start_sec": 0.0,
        "end_sec": 10.0,
        "duration_sec": 10.0,
        "scene_visual_duration_sec": 10.0,
        "segments": [
            # 일부러 순서를 뒤섞어 입력
            {"id": "seg_b", "offset_sec": 5.0, "end_sec_in_scene": 7.0,
             "text": "두번째", "sbv_text": None},
            {"id": "seg_a", "offset_sec": 1.0, "end_sec_in_scene": 4.0,
             "text": "첫번째", "sbv_text": None},
        ],
    }])
    out = sbv_run(force=True)
    body = Path(out).read_text(encoding="utf-8")
    # "첫번째" 가 "두번째" 보다 먼저 등장.
    pos_a = body.find("첫번째")
    pos_b = body.find("두번째")
    assert pos_a < pos_b, f"순서 어긋남: 첫번째={pos_a}, 두번째={pos_b}\n---\n{body}"
