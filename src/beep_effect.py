"""씬 단위 "삐 소리" 자동 효과 — 타이밍 계산 + 비프 WAV 생성.

기능 개요
─────────
특정 씬에 토글(``SceneCard.beep_on_narration_end``)로 지정하면, 그 씬의 **마지막
TTS narration segment 가 끝나는 시점에 맞춰 비프가 같이 끝나도록** "삐" 비프음을
겹쳐 넣는다. 비프 길이만큼 나레이션 끝부분을 덮으므로, 방송 검열음처럼 마지막
단어가 삐에 가려지는 연출이 된다.

(옛 동작은 TTS 가 끝난 *뒤* 이어붙이는 방식이었다. 그러면 나레이션이 끝나고 남은
 씬 끝 빈 구간에서 비프만 따로 울려 연출이 살지 않는다 — 사용자 지적으로 변경.)

설계 고정점
───────────
- 비프 시작 시각(절대, output 타임라인 기준) =
  ``scene_abs_start`` + (마지막 활성 segment 의 끝점) - (비프 길이).
- segment 길이는 씬의 ``tts_speed`` 로 스케일링된다(atempo 후처리와 동일).
  narration_merge.scene_visual_length / build_merged_wav 가 오디오 실제 길이를
  ``duration_sec / tts_speed`` 로 다루는 것과 일관.
- "마지막 segment" 판정 기준은 끝점(offset + 스케일된 duration) 이 가장 큰 segment.
  offset 기준이 아니라 끝점 기준이어야 여러 segment 가 겹치거나 순서가 어긋나도
  실제로 소리가 끝나는 시점을 잡는다.
- scene_abs_start 는 cache/timeline.json 의 씬 start_sec(=출력 타임라인 기준).
  footage_speed 로 인한 스케일링은 이미 timeline.json 에 반영되어 있으므로,
  segment offset/duration(TTS 기준) 과 scene_abs_start(출력 기준) 을 그대로 더하면
  된다. (TTS 는 footage_speed 의 영향을 받지 않는다.)

비프 사운드
───────────
별도 오디오 파일 없이 ffmpeg ``sine`` 소스로 생성한다. 기본 1kHz, 0.4초.
final_composer 의 기존 SFX 트랙 배치 로직(_build_sfx_track_for_overlay)을 그대로
재사용하기 위해 WAV 파일 하나를 만들어 sfx_entries 에 실어 보낸다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from .utils.ffmpeg_utils import ffmpeg_bin

# 기본 비프 사양 — 1kHz 사인파, 0.4초.
DEFAULT_BEEP_FREQ_HZ = 1000
# 비프 길이. 검열음으로 들리려면 어느 정도 길이가 필요하다.
#   0.4s 는 마지막 한 음절 정도만 걸려 검열음 느낌이 약했다 — 사용자 요청으로 0.8s.
DEFAULT_BEEP_DURATION_SEC = 0.8

# 비프가 나레이션 끝을 덮는 양(초).
#
# 예전에는 이 값이 없고 비프 **길이 전체**(0.8s)만큼 나레이션을 거슬러 올라가 시작했다.
# 그래서 가려야 할 말이 0.8s 보다 짧으면 그 말이 통째로 먹혔다
#   (실측: "이런 십포" 의 "십포" 가 전부 비프에 덮여 들리지 않음 — 사용자 보고).
#
# 이제 비프는 "나레이션 끝을 이만큼만 덮고" 나머지는 나레이션 뒤로 흘러나간다.
# 가려야 할 단어의 끝부분만 걸리므로 말은 들리면서 검열 효과는 남는다.
# 비프가 뒤로 삐져나가는 길이는 (DEFAULT_BEEP_DURATION_SEC - 이 값) 이다.
DEFAULT_BEEP_OVERLAP_SEC = 0.2


def _last_active_segment_end_in_scene(scene: Any) -> Optional[float]:
    """씬 내(씬 시작 기준 상대) 마지막 활성 segment 의 끝점(초)을 반환.

    활성 segment = text 가 비어있지 않고 duration_sec > 0.
    끝점 = offset_sec + duration_sec / tts_speed.
    활성 segment 가 하나도 없으면 None.
    """
    segs = getattr(scene, "narration_segments", None) or []
    tts_speed = float(getattr(scene, "tts_speed", 1.0) or 1.0)
    if tts_speed <= 0:
        tts_speed = 1.0

    best_end: Optional[float] = None
    for s in segs:
        text = getattr(s, "text", None)
        if not (isinstance(text, str) and text.strip()):
            continue
        dur = float(getattr(s, "duration_sec", 0.0) or 0.0)
        if dur <= 0:
            continue
        end = float(getattr(s, "offset_sec", 0.0) or 0.0) + (dur / tts_speed)
        if best_end is None or end > best_end:
            best_end = end
    return best_end


def compute_beep_start_sec(
    scene: Any,
    scene_abs_start: float,
    beep_duration_sec: float = DEFAULT_BEEP_DURATION_SEC,
    overlap_sec: Optional[float] = None,
) -> Optional[float]:
    """씬의 비프 시작 절대 시각(출력 타임라인 기준, 초). 계산 불가 시 None.

    비프는 마지막 TTS 가 끝나기 ``overlap_sec`` 초 전에 시작한다. 즉 가려야 할
    말의 **끝부분만** 삐에 걸리고, 비프의 나머지는 나레이션 뒤로 흘러나간다.
    방송 검열음처럼 마지막 단어가 삐에 가려지되 말 자체는 들리는 연출이다.

    (옛 동작은 비프 **길이 전체**만큼 거슬러 올라가 시작해서, 가릴 말이 비프보다
     짧으면 그 말이 통째로 먹혔다 — "이런 십포" 의 "십포" 가 전부 덮인 사용자 보고.
     그보다 더 옛 동작은 TTS 가 다 끝난 *뒤* 이어붙여 비프만 따로 울렸다.)

    Args:
        scene: SceneCard. narration_segments / tts_speed 참조.
        scene_abs_start: cache/timeline.json 기준 이 씬의 출력 타임라인 시작 시각.
        beep_duration_sec: 비프 길이(초).
        overlap_sec: 나레이션 끝을 덮는 양(초). None 이면
            ``DEFAULT_BEEP_OVERLAP_SEC``. 비프 길이보다 크면 길이로 클램프한다
            (그 이상 덮는 것은 의미가 없다 — 비프가 나레이션 앞으로만 밀린다).

    Returns:
        비프 시작 절대 시각. 활성 narration segment 가 없으면 None
        (붙일 기준점이 없으므로 비프를 배치하지 않는다).
    """
    rel_end = _last_active_segment_end_in_scene(scene)
    if rel_end is None:
        return None
    dur = max(0.0, float(beep_duration_sec))
    ov = DEFAULT_BEEP_OVERLAP_SEC if overlap_sec is None else float(overlap_sec)
    ov = min(max(0.0, ov), dur)
    # 나레이션 시작보다 앞서지 않도록 씬 상대 0 으로 하한을 둔다
    # (겹침이 나레이션 전체보다 길면 전부 덮이되, 앞 씬을 침범하진 않는다).
    rel_start = max(0.0, rel_end - ov)
    start = float(scene_abs_start) + rel_start
    return start if start >= 0 else 0.0


def ensure_beep_wav(
    out_path: Path,
    *,
    freq_hz: int = DEFAULT_BEEP_FREQ_HZ,
    duration_sec: float = DEFAULT_BEEP_DURATION_SEC,
) -> Optional[Path]:
    """비프 WAV 를 생성(캐시)한다. 이미 존재하면 그대로 반환.

    ffmpeg ``sine`` 소스로 44.1kHz 스테레오 WAV 를 만든다. 앞뒤에 짧은
    afade 를 넣어 클릭 노이즈를 줄인다. 실패 시 None.
    """
    out_path = Path(out_path)
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    d = max(0.05, float(duration_sec))
    fade = min(0.02, d / 4)
    fade_out_start = max(0.0, d - fade)
    filt = (
        f"afade=t=in:st=0:d={fade:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}"
    )
    r = subprocess.run(
        [
            ffmpeg_bin(), "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency={int(freq_hz)}:duration={d:.3f}:sample_rate=44100",
            "-af", filt,
            "-ac", "2",
            str(out_path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0 or not out_path.exists():
        return None
    return out_path
