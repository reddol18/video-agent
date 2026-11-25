"""cut_clip vf 호환성 회귀 테스트 (#303).

`src/utils/ffmpeg_utils.cut_clip`가 내부에서 RenderSpec 매퍼를 사용하도록 전환된 뒤에도,
실제 시각적 결과가 깨지지 않는다는 약속을 검증한다.

검증 전략:
- ffmpeg를 직접 실행하지 않는다 (CI에서 비용 큼).
- 대신 `subprocess.run`을 monkey-patch하여 cut_clip이 만든 ffmpeg 명령(vf)을 캡처하고,
  그 vf가 다음 불변식을 만족하는지 확인한다:
  1. fit=contain → vf에 pad가 캔버스 크기로 들어간다.
  2. fit=cover   → vf에 crop이 캔버스 크기로 들어간다.
  3. fit=stretch → 비율 무시 scale, crop/pad 없음.
  4. rotation=90/180/270 → vf에 transpose/hflip+vflip가 prepend된다.
  5. target_duration이 다르면 setpts가 마지막 토큰으로 들어간다.
  6. offset==0이면 RenderSpec 매퍼 경로(=추상 키워드 없음, scale 절대 픽셀).
  7. offset!=0이면 레거시 경로(=force_original_aspect_ratio 키워드 존재).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.utils.ffmpeg_utils as ffu


class _CapturedCommand:
    """subprocess.run을 흉내 내며 cut_clip의 ffmpeg argv를 저장한다."""
    def __init__(self):
        self.argv = None

    def __call__(self, cmd, *args, **kwargs):
        self.argv = list(cmd)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return _Result()


def _vf_of(captured: _CapturedCommand) -> str:
    """캡처한 argv에서 -vf 다음 토큰을 꺼낸다."""
    argv = captured.argv or []
    for i, a in enumerate(argv):
        if a == "-vf" and i + 1 < len(argv):
            return argv[i + 1]
    return ""


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """subprocess.run을 stub으로 바꾸고, get_video_dimensions/_needs_rotation도 stub."""
    cap = _CapturedCommand()
    monkeypatch.setattr(ffu.subprocess, "run", cap)
    monkeypatch.setattr(ffu, "get_video_dimensions", lambda src: (1920, 1080))
    monkeypatch.setattr(ffu, "_needs_rotation", lambda src, *a, **kw: False)
    src = tmp_path / "src.mp4"
    src.write_bytes(b"")
    dst = tmp_path / "dst.mp4"
    return cap, str(src), str(dst)


# ── 기본 fit 매핑 ─────────────────────────────────────────────────────────

def test_contain_no_offset_uses_render_spec_mapper(patched):
    cap, src, dst = patched
    ffu.cut_clip(src, 0.0, 5.0, dst, fit="contain", out_w=1080, out_h=1920)
    vf = _vf_of(cap)
    assert "pad=1080:1920:" in vf, f"contain pad 누락: {vf}"
    # RenderSpec 매퍼 경로: 추상 키워드 없음
    assert "force_original_aspect_ratio" not in vf
    # crop은 contain에서 없음
    assert "crop=" not in vf


def test_cover_no_offset_uses_render_spec_mapper(patched):
    cap, src, dst = patched
    ffu.cut_clip(src, 0.0, 5.0, dst, fit="cover", out_w=1080, out_h=1920)
    vf = _vf_of(cap)
    assert "crop=1080:1920:" in vf, f"cover crop 누락: {vf}"
    assert "force_original_aspect_ratio" not in vf
    assert "pad=" not in vf


def test_stretch_no_offset_uses_render_spec_mapper(patched):
    cap, src, dst = patched
    ffu.cut_clip(src, 0.0, 5.0, dst, fit="stretch", out_w=1080, out_h=1920)
    vf = _vf_of(cap)
    assert "scale=1080:1920" in vf
    assert "crop=" not in vf
    assert "pad=" not in vf


# ── rotation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rot,expect", [
    (90, "transpose=1"),
    (180, "hflip,vflip"),
    (270, "transpose=2"),
])
def test_manual_rotation_propagates_to_vf(patched, rot, expect):
    cap, src, dst = patched
    ffu.cut_clip(src, 0.0, 5.0, dst, fit="contain", rotation=rot,
                  out_w=1920, out_h=1080)
    vf = _vf_of(cap)
    assert vf.startswith(expect + ","), f"rotation={rot} 매핑 안 됨: {vf}"


def test_no_auto_rotation_matches_preview(monkeypatch, tmp_path):
    """자동 회전 제거(미리보기≡렌더): rotation=0이면 _needs_rotation이 True를 반환해도
    렌더가 절대 자동 회전(transpose)하지 않는다.

    - 세로 프로젝트(1080×1920)에 가로 footage(1920×1080): 회전 없이 contain 레터박스.
    - 가로 프로젝트(1920×1080)에 세로 footage(1080×1920): 회전 없이 contain 필러박스.
    미리보기(builder._footage_to_layer)가 수동 rotation만 존중하는 것과 일치한다.
    """
    src = tmp_path / "src.mp4"
    src.write_bytes(b"")
    dst = tmp_path / "dst.mp4"

    # _needs_rotation이 True를 반환하더라도 호출되지 않아야 하므로 무시된다.
    monkeypatch.setattr(ffu, "_needs_rotation", lambda s, *a, **kw: True)

    # 세로 프로젝트 + 가로 footage → 자동 회전 없음
    cap = _CapturedCommand()
    monkeypatch.setattr(ffu.subprocess, "run", cap)
    monkeypatch.setattr(ffu, "get_video_dimensions", lambda s: (1920, 1080))
    ffu.cut_clip(str(src), 0.0, 5.0, str(dst), fit="contain", rotation=0,
                  out_w=1080, out_h=1920)
    vf = _vf_of(cap)
    assert not vf.startswith("transpose"), f"가로 footage가 자동 회전됨: {vf}"
    assert "pad=1080:1920:" in vf, f"contain 레터박스 누락: {vf}"

    # 가로 프로젝트 + 세로 footage → 자동 회전 없음 (필러박스)
    cap2 = _CapturedCommand()
    monkeypatch.setattr(ffu.subprocess, "run", cap2)
    monkeypatch.setattr(ffu, "get_video_dimensions", lambda s: (1080, 1920))
    ffu.cut_clip(str(src), 0.0, 5.0, str(dst), fit="contain", rotation=0,
                  out_w=1920, out_h=1080)
    vf2 = _vf_of(cap2)
    assert not vf2.startswith("transpose"), f"세로 footage가 자동 회전됨: {vf2}"
    assert "pad=1920:1080:" in vf2, f"contain 필러박스 누락: {vf2}"


# ── speed (target_duration) ───────────────────────────────────────────────

def test_target_duration_adds_setpts(patched):
    cap, src, dst = patched
    # clip_duration = 10s, target = 5s → speedup × 2 → pts_ratio = 0.5
    ffu.cut_clip(src, 0.0, 10.0, dst, fit="contain", target_duration=5.0,
                  out_w=1920, out_h=1080)
    vf = _vf_of(cap)
    # RenderSpec 매퍼는 setpts를 *맨 끝*에 둔다 (시간/공간 분리)
    assert vf.split(",")[-1].startswith("setpts="), f"setpts 끝에 없음: {vf}"
    # playback_rate = 1 / pts_ratio = 1 / 0.5 = 2.0 → setpts factor = 1/2.0 = 0.5
    assert "setpts=0.500000" in vf


def test_target_duration_does_not_break_fit_geometry(patched):
    """target_duration이 있어도 fit이 정확히 적용된다 (fit-vs-speed 회귀 차단)."""
    cap, src, dst = patched
    # cover + speedup × 2
    ffu.cut_clip(src, 0.0, 10.0, dst, fit="cover", target_duration=5.0,
                  out_w=1080, out_h=1920)
    vf = _vf_of(cap)
    # cover의 crop은 setpts 분기와 *완전히 무관*하게 들어가 있어야 함
    assert "crop=1080:1920:" in vf, f"speed 적용 후 fit 누락: {vf}"


# ── offset != 0은 레거시 경로 ─────────────────────────────────────────────

def test_offset_nonzero_uses_legacy_path(patched):
    cap, src, dst = patched
    ffu.cut_clip(src, 0.0, 5.0, dst, fit="contain",
                  x_offset=0.1, y_offset=-0.05,
                  out_w=1920, out_h=1080)
    vf = _vf_of(cap)
    # 레거시 경로 흔적: force_original_aspect_ratio
    assert "force_original_aspect_ratio=decrease" in vf, f"offset 경로가 레거시가 아님: {vf}"
