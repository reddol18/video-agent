"""소재가 짧은 clip 그룹의 오프셋 배분 검증 (#short-source-group).

실제 사고: 씬 63~67 다섯 개가 5.4초짜리 `rec_R2_choice.mp4` 를 공유하는데,
다섯 씬에 필요한 시간(TTS 합)은 21.0초였다. 오프셋을 TTS 합으로 그냥 누적하니
씬 64~67 의 시작 지점이 각각 5.4 / 8.9 / 13.7 / 18.0초 — 전부 파일 끝을 넘어
"마지막 프레임 사용" 으로 떨어져 0.1초 정지화면이 됐다.
(같은 이유로 그 씬들의 텍스트 오버레이 합성도 실패했다.)

정책: 그룹의 TTS 총합이 원본 길이를 넘으면, 원본을 TTS 비율대로 나눠 배분한다.
각 씬은 자기 몫 구간을 받고 부족한 길이는 이후 단계에서 슬로모션으로 늘어난다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.clip_editor import _build_clip_group_offsets  # noqa: E402


@pytest.fixture()
def short_clip(tmp_path):
    """5.4초짜리 소스 — 실제 사고와 같은 길이."""
    p = tmp_path / "shared.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", "5.4",
         "-i", "testsrc=size=320x180:rate=30", str(p)],
        check=True, timeout=120,
    )
    return p


def _sel(scene_id: int, src: Path, tts: float) -> dict:
    return {
        "scene_id": scene_id, "source_file": str(src), "effect": "clip",
        "tts_duration_sec": tts, "clip_duration_sec": tts,
    }


def test_offsets_stay_inside_short_source(short_clip):
    """핵심 회귀: 모든 오프셋이 원본 길이 안에 있어야 한다."""
    sels = [_sel(sid, short_clip, tts) for sid, tts in
            [(63, 2.92), (64, 3.32), (65, 4.16), (66, 3.20), (67, 2.44)]]
    off = _build_clip_group_offsets(sels)
    assert len(off) == 5
    for sid in (63, 64, 65, 66, 67):
        assert off[sid] < 5.4, f"씬 {sid} 오프셋 {off[sid]:.2f} 가 원본(5.4s)을 넘었다"


def test_offsets_are_ordered_and_proportional(short_clip):
    """씬 순서대로 증가하고, TTS 가 긴 씬이 더 큰 몫을 받는다."""
    sels = [_sel(sid, short_clip, tts) for sid, tts in
            [(63, 2.92), (64, 3.32), (65, 4.16), (66, 3.20), (67, 2.44)]]
    off = _build_clip_group_offsets(sels)
    vals = [off[s] for s in (63, 64, 65, 66, 67)]
    assert vals == sorted(vals)
    assert vals[0] == 0.0
    # 씬 65(TTS 4.16)의 몫이 씬 67(TTS 2.44)의 몫보다 커야 한다.
    share_65 = off[66] - off[65]
    share_67 = 5.4 - off[67]
    assert share_65 > share_67


def test_long_enough_source_keeps_tts_accumulation(tmp_path):
    """원본이 충분히 길면 기존 동작(TTS 누적) 그대로."""
    src = tmp_path / "long.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", "30",
         "-i", "testsrc=size=320x180:rate=30", str(src)],
        check=True, timeout=120,
    )
    sels = [_sel(1, src, 3.0), _sel(2, src, 4.0), _sel(3, src, 5.0)]
    off = _build_clip_group_offsets(sels)
    assert off[1] == pytest.approx(0.0)
    assert off[2] == pytest.approx(3.0)
    assert off[3] == pytest.approx(7.0)


def test_single_scene_group_is_untouched(short_clip):
    """혼자 쓰는 씬은 배분 대상이 아니다(오프셋 0)."""
    off = _build_clip_group_offsets([_sel(10, short_clip, 12.0)])
    assert off[10] == pytest.approx(0.0)
