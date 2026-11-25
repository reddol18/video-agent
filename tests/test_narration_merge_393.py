"""#393 — `narration_merge` 공통 유틸 단위 테스트.

대상 함수:
- `segment_wav_path(narration_dir, scene_id, seg_id) -> Path`
- `scene_visual_length(scene) -> float`
- `build_merged_wav(scene, narration_dir, merged_path) -> float` (ffmpeg 호출은 모킹)

설계 의도 검증:
- segment_wav_path: 파일명 표준 `scene_{id:03d}_seg_{seg_id}.wav` 보존.
- scene_visual_length: seg_0.duration_sec, duration_hint_sec, 1.0 의 max.
- build_merged_wav: N=1 / N>=2 두 케이스 모두 ffmpeg 인자 구조가 의도대로 생성되는지.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def captured_subprocess(monkeypatch):
    """subprocess.run 을 캡처해 ffmpeg 호출 인자를 검사할 수 있게 한다."""
    calls = []

    def fake_run(args, *_a, **_kw):
        calls.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
        # ffmpeg 출력 파일을 빈 파일로 생성.
        if args and "-y" in args:
            try:
                p = Path(args[-1])
                if p.suffix == ".wav":
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            except Exception:
                pass
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _make_scene(scene_id: int, segs: list, hint: float = 5.0):
    """간소 SceneCard 모킹 (실제 모델 import 없이 duck typing)."""
    from web.backend.models import SceneCard, NarrationSegment
    return SceneCard(
        scene_id=scene_id,
        duration_hint_sec=hint,
        narration_segments=[NarrationSegment(**seg) for seg in segs],
    )


def test_segment_wav_path_format(tmp_path):
    from web.backend.narration_merge import segment_wav_path
    p = segment_wav_path(tmp_path, 16, "seg_1")
    assert p == tmp_path / "scene_016_seg_seg_1.wav"


def test_segment_wav_path_sanitizes_unsafe_chars(tmp_path):
    from web.backend.narration_merge import segment_wav_path
    p = segment_wav_path(tmp_path, 3, "seg_!@#$")
    # 비안전 문자 제거 후 seg_ 로 남음.
    assert p.name.startswith("scene_003_seg_seg_") or p.name == "scene_003_seg_seg.wav"


def test_scene_visual_length_seg0_max():
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(1, [
        {"id": "seg_0", "text": "x", "duration_sec": 7.0},
    ], hint=5.0)
    assert scene_visual_length(scene) == pytest.approx(7.0)


def test_scene_visual_length_hint_max():
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(1, [
        {"id": "seg_0", "text": "x", "duration_sec": 2.0},
    ], hint=6.5)
    assert scene_visual_length(scene) == pytest.approx(6.5)


def test_scene_visual_length_minimum_one_second():
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(1, [], hint=0.0)
    assert scene_visual_length(scene) == pytest.approx(1.0)


def test_scene_visual_length_includes_seg1_end_point_regression_395():
    """#395 — seg_1+ 의 offset_sec + duration_sec 끝점도 scene_visual_length 에 포함되어야.

    회귀 시나리오 (16번 씬):
    - seg_0 (offset=0, dur=3.096) + seg_1 (offset=7.65, dur=2.28)
    - 옛 로직: max(seg_0=3.096, hint=5.0, 1.0) = 5.0 → seg_1(7.65~9.93s) 통째로 잘림
    - 신 로직: max(3.096, 5.0, 9.93, 1.0) = 9.93 → seg_1 포함
    """
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(16, [
        {"id": "seg_0", "text": "안녕", "offset_sec": 0.0, "duration_sec": 3.096,
         "status": "ready", "synced": True},
        {"id": "seg_1", "text": "추가 구간", "offset_sec": 7.65, "duration_sec": 2.28,
         "status": "ready", "synced": True},
    ], hint=5.0)
    # seg_1 끝점 = 7.65 + 2.28 = 9.93
    assert scene_visual_length(scene) == pytest.approx(9.93, abs=1e-3)


def test_scene_visual_length_ignores_empty_text_segments():
    """텍스트 없는 segment 는 끝점 계산에서 제외 (build_merged_wav 의 필터와 일관)."""
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(2, [
        {"id": "seg_0", "text": "안녕", "offset_sec": 0.0, "duration_sec": 2.0},
        {"id": "seg_1", "text": "  ", "offset_sec": 10.0, "duration_sec": 5.0},  # 공백만
        {"id": "seg_2", "text": "", "offset_sec": 20.0, "duration_sec": 3.0},   # 빈 텍스트
    ], hint=4.0)
    # seg_1/seg_2 는 무시 → max(2.0, 4.0, 1.0) = 4.0
    assert scene_visual_length(scene) == pytest.approx(4.0)


def test_scene_visual_length_ignores_zero_duration_segments():
    """duration_sec <= 0 segment 는 끝점 계산에서 제외 (아직 TTS 안 된 신규 segment)."""
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(3, [
        {"id": "seg_0", "text": "x", "offset_sec": 0.0, "duration_sec": 2.0},
        {"id": "seg_1", "text": "y", "offset_sec": 10.0, "duration_sec": 0.0,
         "status": "idle"},  # 아직 TTS 안 됨
    ], hint=3.0)
    # seg_1 무시 → max(2.0, 3.0, 1.0) = 3.0
    assert scene_visual_length(scene) == pytest.approx(3.0)


def test_scene_visual_length_single_seg0_regression_safe():
    """회귀 안전 — seg_0 만 있는 정상 프로젝트는 결과가 옛 로직과 동일."""
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(4, [
        {"id": "seg_0", "text": "x", "offset_sec": 0.0, "duration_sec": 3.5},
    ], hint=5.0)
    # 옛 로직: max(3.5, 5.0, 1.0) = 5.0
    # 신 로직: max(3.5, 5.0, seg_max_end=3.5, 1.0) = 5.0 (동일)
    assert scene_visual_length(scene) == pytest.approx(5.0)


def test_scene_visual_length_all_error_segments_falls_back_to_hint():
    """모든 segment 가 error/duration=0 이면 hint/1.0 폴백."""
    from web.backend.narration_merge import scene_visual_length
    scene = _make_scene(5, [
        {"id": "seg_0", "text": "x", "offset_sec": 0.0, "duration_sec": 0.0,
         "status": "error", "error_message": "tts failed"},
        {"id": "seg_1", "text": "y", "offset_sec": 5.0, "duration_sec": 0.0,
         "status": "error"},
    ], hint=4.0)
    # 모두 duration=0 → seg_max_end = 0 → max(0, 4.0, 1.0) = 4.0
    assert scene_visual_length(scene) == pytest.approx(4.0)


def test_build_merged_wav_empty_segments_returns_zero(tmp_path, captured_subprocess):
    """segments 가 비면 ffmpeg 호출 없이 0.0 반환."""
    from web.backend.narration_merge import build_merged_wav
    scene = _make_scene(1, [], hint=5.0)
    out = tmp_path / "merged.wav"
    result = build_merged_wav(scene, tmp_path, out)
    assert result == 0.0
    assert not out.exists()
    assert len(captured_subprocess) == 0


def test_build_merged_wav_single_segment_uses_adelay_only(tmp_path, captured_subprocess):
    """N=1 케이스: adelay + atrim 만 적용되고 amix 는 안 쓰임 (회귀 안전 — seg_0 만 있는 정상 프로젝트)."""
    from web.backend.narration_merge import build_merged_wav, segment_wav_path

    # seg_0 wav 파일 미리 생성 (실제로 존재해야 ffmpeg 입력으로 포함).
    narration_dir = tmp_path
    seg0_wav = segment_wav_path(narration_dir, 1, "seg_0")
    seg0_wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE_seg0")

    scene = _make_scene(1, [
        {"id": "seg_0", "text": "안녕", "offset_sec": 0.0, "duration_sec": 3.0},
    ], hint=5.0)

    merged = tmp_path / "merged.wav"
    # subprocess 모킹으로 wav_duration 이 wave 라이브러리로 못 읽으므로 0.0 반환되지만,
    # ffmpeg 인자 구성을 검증하는 게 목적이므로 그것만 확인한다.
    build_merged_wav(scene, narration_dir, merged)

    assert len(captured_subprocess) == 1, "ffmpeg 호출 횟수가 1회여야 함"
    cmd = captured_subprocess[0]
    # filter_complex 에 amix 가 없고 adelay + atrim 만 있어야 함.
    fc_idx = cmd.index("-filter_complex")
    fc = cmd[fc_idx + 1]
    assert "amix" not in fc, f"N=1 케이스에서 amix 사용됨: {fc}"
    assert "adelay" in fc, f"adelay 미사용: {fc}"
    assert "atrim" in fc, f"atrim 미사용: {fc}"
    # 씬 길이 = max(seg_0=3.0, hint=5.0, 1.0) = 5.0
    assert "end=5.000" in fc, f"atrim 클램프 값 불일치: {fc}"


def test_build_merged_wav_multi_segments_uses_amix(tmp_path, captured_subprocess):
    """N=2 케이스: adelay 각각 + amix=normalize=0 + atrim 사용."""
    from web.backend.narration_merge import build_merged_wav, segment_wav_path

    narration_dir = tmp_path
    for seg_id in ("seg_0", "seg_1"):
        wav = segment_wav_path(narration_dir, 16, seg_id)
        wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    scene = _make_scene(16, [
        {"id": "seg_0", "text": "안녕", "offset_sec": 0.0, "duration_sec": 3.096},
        {"id": "seg_1", "text": "추가", "offset_sec": 7.65, "duration_sec": 2.28},
    ], hint=5.0)

    merged = tmp_path / "merged_016.wav"
    build_merged_wav(scene, narration_dir, merged)

    assert len(captured_subprocess) == 1
    cmd = captured_subprocess[0]
    # 입력 wav 가 2개 (-i 각 1번씩).
    minus_i_count = sum(1 for x in cmd if x == "-i")
    assert minus_i_count == 2, f"-i 입력이 2개여야 함: {cmd}"

    fc_idx = cmd.index("-filter_complex")
    fc = cmd[fc_idx + 1]
    assert "amix=inputs=2" in fc, f"amix=inputs=2 미사용: {fc}"
    assert "normalize=0" in fc, f"normalize=0 미사용 (TTS 볼륨 감쇠 회귀): {fc}"
    # seg_1.offset 7.65 → adelay 7650.
    assert "adelay=7650|7650" in fc, f"seg_1 adelay 누락: {fc}"
    # #395 — 씬 길이 = max(seg_0=3.096, hint=5.0, seg_1_end=9.93, 1.0) = 9.93
    # → atrim end=9.930 (옛 정책의 5.000 으로 잘리면 seg_1 사라지는 회귀 발생).
    assert "end=9.930" in fc, f"atrim 클램프 값 불일치 (seg_1 끝점 미포함 회귀): {fc}"


def test_build_merged_wav_skips_missing_segment_wav(tmp_path, captured_subprocess):
    """일부 segment wav 가 디스크에 없으면 그 segment 는 ffmpeg 입력에서 빠진다."""
    from web.backend.narration_merge import build_merged_wav, segment_wav_path

    narration_dir = tmp_path
    # seg_0 wav 만 존재, seg_1 wav 는 없음.
    seg0_wav = segment_wav_path(narration_dir, 5, "seg_0")
    seg0_wav.write_bytes(b"RIFF\x00\x00WAVE")

    scene = _make_scene(5, [
        {"id": "seg_0", "text": "x", "offset_sec": 0.0, "duration_sec": 2.0},
        {"id": "seg_1", "text": "y", "offset_sec": 3.0, "duration_sec": 1.5},
    ], hint=5.0)

    merged = tmp_path / "merged_005.wav"
    build_merged_wav(scene, narration_dir, merged)

    assert len(captured_subprocess) == 1
    cmd = captured_subprocess[0]
    # 입력은 seg_0 1개만.
    minus_i_count = sum(1 for x in cmd if x == "-i")
    assert minus_i_count == 1, f"누락된 seg_1 가 입력에 들어감: {cmd}"
