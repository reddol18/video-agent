"""#393 — 씬 단위 narration segments 합성(merged WAV) 공통 유틸.

배경: #382 도입으로 한 씬에 여러 narration segment 가 존재할 수 있게 되었으나,
`render.py` 의 `_build_merged_wav` 는 render 라우터 내부 함수라 미리보기
(`_make_scene_preview`) 등 다른 경로에서 재사용할 수 없었다. 그 결과 사용자가
segment 를 추가해도 미리보기 영상에는 seg_0 만 들리는 회귀(#393)가 발생.

본 모듈은 segment WAV 경로 계산 + adelay/amix 기반 merged WAV 합성을 한 곳에
두어 final render 와 preview 양쪽이 동일한 산출물을 사용하도록 한다.

설계 고정점:
- merged WAV 의 길이는 `_scene_visual_length(scene)` 로 atrim 클램프한다.
  preview/final render 모두 같은 정책으로 잘려야 일관성이 유지된다.
- segment 가 0개 → 0.0 반환 (호출자가 폴백 처리).
- segment 1개 → adelay 만 적용 후 atrim (amix 없이 — ffmpeg 가 amix 입력 1개에
  대해 비효율적이고 일부 빌드에서 에러를 내는 사례가 있음).
- segment N개(N>=2) → adelay 각각 + amix=normalize=0 으로 합치고 atrim.

ffmpeg 출력 포맷은 final_composer 호환성을 위해 24kHz mono PCM WAV 로 고정한다.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Any

import sys as _sys_ffpath
_ROOT_FFPATH = Path(__file__).parent.parent.parent
if str(_ROOT_FFPATH) not in _sys_ffpath.path:
    _sys_ffpath.path.insert(0, str(_ROOT_FFPATH))
from src.utils.ffmpeg_utils import ffmpeg_bin


def segment_wav_path(narration_dir: Path, scene_id: int, seg_id: str) -> Path:
    """씬+segment 식별자로부터 WAV 경로를 결정한다.

    seg_id 에 영문/숫자/`_`/`-` 외 문자가 들어와도 안전한 파일명을 만들기 위해
    필터를 둔다. 비정상 입력은 `seg` 단일 prefix 로 폴백.
    """
    safe = "".join(c for c in seg_id if c.isalnum() or c in ("_", "-")) or "seg"
    return narration_dir / f"scene_{scene_id:03d}_seg_{safe}.wav"


def scene_visual_length(scene: Any) -> float:
    """씬의 시각 길이(초). merged WAV atrim 클램프 + 회귀(#382/#395) 정책 기준.

    우선순위: max(
        seg_0.duration_sec,
        scene.duration_hint_sec,
        모든 활성 segment 의 (offset_sec + duration_sec) 끝점 최대값,  # #395
        1.0,
    )

    #395 — seg_0 / hint 만 보고 결정하면 사용자가 추가한 seg_1+ 가 atrim 에서 잘림.
    예: seg_0(dur=3.096) + seg_1(offset=7.65, dur=2.28) → 끝점 9.93s.
    hint=5.0 만으로 클램프하면 seg_1 통째로 사라진다. 모든 segment 의 끝점을
    포괄해야 atrim 이 잘라내지 않는다.

    필터 정책: `build_merged_wav` 가 합성 입력으로 채택하는 조건(`text.strip()`)과
    동일하게 적용한다. status 가 ready 가 아닌 segment 도 텍스트가 있으면 끝점
    포함 — 디스크 wav 가 없으면 어차피 ffmpeg 입력에서 빠지므로 atrim end 만
    약간 보수적으로 커지는 정도(빈 구간 추가)로 무해하다.

    클립 트림(footage.clip_*_sec) 길이는 여기서 고려하지 않는다 — segment 합성은
    "씬 길이" 라는 보존된 의미값(=hint, TTS) 안에서 끝나야 하며, footage 트림은
    이후 video 합성 단계에서 별도로 처리된다.

    tts_speed: 씬의 TTS 재생 속도(1.0=원본). speed>1.0 이면 오디오가 짧아지고
    speed<1.0 이면 늘어난다. seg_0/segment 끝점 계산에도 반영해야 atrim 이
    speed 로 늘어난 오디오를 자르지 않는다. hint 는 사용자가 지정한 시각 길이라
    speed 와 무관하게 그대로 사용.
    """
    segs = scene.narration_segments or []
    tts_speed = float(getattr(scene, "tts_speed", 1.0) or 1.0)
    if tts_speed <= 0:
        tts_speed = 1.0
    seg_0 = next((s for s in segs if s.id == "seg_0"), None)
    tts0 = (float(seg_0.duration_sec or 0.0) / tts_speed) if seg_0 else 0.0
    base = float(getattr(scene, "duration_hint_sec", 0) or 0)
    # #395 — 모든 활성 segment 의 offset+duration 끝점을 포함.
    # tts_speed 반영: 오디오 duration 이 1/tts_speed 배로 스케일링되고,
    # segment offset 은 씬 시작 기준 절대 오프셋이라 speed 에 무관.
    seg_max_end = 0.0
    for s in segs:
        text = getattr(s, "text", None)
        if not (isinstance(text, str) and text.strip()):
            continue
        dur = float(s.duration_sec or 0.0)
        if dur <= 0:
            continue
        end_sec = float(s.offset_sec or 0.0) + (dur / tts_speed)
        if end_sec > seg_max_end:
            seg_max_end = end_sec
    return max(tts0, base, seg_max_end, 1.0)


def _atempo_chain(speed: float) -> str:
    """ffmpeg atempo 필터 문자열을 생성한다.

    atempo 는 한 인스턴스당 0.5~2.0 범위만 지원하므로 범위 밖은 체이닝한다.
    speed 가 1.0 근처(1e-6 오차)면 빈 문자열 반환 → 호출자가 원본 그대로 사용.
    """
    if speed is None or abs(speed - 1.0) < 1e-6:
        return ""
    s = float(speed)
    if s <= 0:
        return ""
    if 0.5 <= s <= 2.0:
        return f"atempo={s:.6f}"
    if s > 2.0:
        # 2.0 로 한 단계 처리 후 나머지 비율 적용 (같은 방식으로 재귀 확장 가능).
        inner = _atempo_chain(s / 2.0)
        return f"atempo=2.0,{inner}" if inner else "atempo=2.0"
    # s < 0.5
    inner = _atempo_chain(s / 0.5)
    return f"atempo=0.5,{inner}" if inner else "atempo=0.5"


def wav_duration_sec(path: Path) -> float:
    """WAV 파일 길이(초). 손상/미존재 시 0.0."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.0


def build_merged_wav(
    scene: Any,
    narration_dir: Path,
    merged_path: Path,
    log: Any = None,
) -> float:
    """씬의 모든 segment 를 offset_sec 에 따라 합성해 단일 wav 로 저장.

    Args:
        scene: SceneCard. narration_segments(list[NarrationSegment]) 사용.
            scene.tts_speed(float, 기본 1.0) 이 1.0 이 아니면 각 segment 에
            atempo 를 먼저 적용한 뒤 adelay/amix 를 수행한다.
        narration_dir: segment WAV 파일들이 위치한 폴더 (출력 폴더의 narration/).
        merged_path: 합성 결과 WAV 출력 경로.
        log: 선택적 로깅 콜러블. 실패 시 호출되며 None 이면 print 로 폴백.

    Returns:
        합성된 wav 의 길이(초). 씬 시각 길이로 atrim 클램프 적용.
        합성 대상 segment 가 없거나 ffmpeg 실패 시 0.0.

    회귀 안전:
        - segments=[seg_0] (N=1) 케이스: adelay(0) + atrim 만 적용되어 결과가
          seg_0 원본 WAV 와 사실상 동일 (다만 atrim 클램프로 씬 길이 초과분은 잘림).
        - segments 가 비었거나 모두 빈 텍스트 → 0.0 반환, merged_path 생성 안 함.
        - tts_speed 필드가 없거나 1.0 인 경우 필터 체인은 이전과 동일 (atempo 삽입 없음).
    """
    segs = [
        seg for seg in (scene.narration_segments or [])
        if getattr(seg, "text", None) and seg.text.strip()
    ]
    scene_dur = scene_visual_length(scene)
    if not segs:
        return 0.0

    # tts_speed — 필드 없거나 유효하지 않으면 1.0 폴백. 1.0 이면 atempo 삽입 안 함.
    tts_speed = float(getattr(scene, "tts_speed", 1.0) or 1.0)
    if tts_speed <= 0:
        tts_speed = 1.0
    atempo_str = _atempo_chain(tts_speed)  # "" 이면 원본 속도

    inputs: list[str] = []
    filter_parts: list[str] = []
    labels: list[str] = []
    used = 0
    # #410 — 실제 wav 들의 끝점 max 도 atrim 클램프에 반영.
    # scene_visual_length 는 seg.duration_sec 메타에 의존하는데, #409 가 #406 시점에
    # status=idle 로 만들면서 duration_sec=0.0 로 리셋함. 그 후 사용자가 generate 안
    # 트리거하고 클라이언트 TTS 만 만들어 tts-upload 로 legacy wav 가 디스크에 5.69s
    # 짜리로 생겼는데, scene_visual_length 는 메타만 보고 hint=5.0 반환 → atrim 이
    # 5.0s 에서 잘라 wav 끝부분(0.69s, "...는데요." 부분)이 사라지는 회귀.
    #
    # 해결: 합성 입력으로 채택된 wav 의 실제 길이 + offset 끝점도 max 후보로 추가.
    actual_max_end = 0.0
    # #406-followup — seg_0 wav 가 없으면 legacy narration_scene_NNN.wav 폴백.
    # 프론트의 tts-upload 라우터는 legacy 경로에만 wav 를 저장하고 segment 경로
    # (`scene_NNN_seg_seg_0.wav`)는 안 만듦 → seg_0 영역이 무음이 되는 회귀.
    # seg_0 의 의도(기본 나레이션) 와 legacy wav 의 의도가 동일하므로 안전한 폴백.
    legacy_wav = narration_dir / f"narration_scene_{scene.scene_id:03d}.wav"
    for seg in segs:
        wav_seg = segment_wav_path(narration_dir, scene.scene_id, seg.id)
        if (not wav_seg.exists() or wav_seg.stat().st_size == 0):
            # seg_0 만 legacy 폴백 허용 (다른 segment 는 자체 wav 필수).
            if seg.id == "seg_0" and legacy_wav.exists() and legacy_wav.stat().st_size > 0:
                wav_seg = legacy_wav
            else:
                continue
        delay_ms = max(0, int(float(seg.offset_sec or 0.0) * 1000))
        inputs += ["-i", str(wav_seg)]
        lbl = f"s{used}"
        # tts_speed 적용: atempo 를 adelay 앞단에 삽입해 오디오 길이 자체를 스케일링.
        # atempo=1.0 근사 시 atempo_str 이 "" 라 filter chain 은 이전과 동일.
        if atempo_str:
            filter_parts.append(
                f"[{used}:a]{atempo_str},adelay={delay_ms}|{delay_ms}[{lbl}]"
            )
        else:
            filter_parts.append(
                f"[{used}:a]adelay={delay_ms}|{delay_ms}[{lbl}]"
            )
        labels.append(f"[{lbl}]")
        used += 1
        # 실제 wav 길이 측정 — atrim 클램프가 자르지 않도록 actual_max_end 갱신.
        # tts_speed 반영: 오디오 길이는 실제로 (원본길이/tts_speed) 로 변환된다.
        actual_dur = wav_duration_sec(wav_seg)
        if actual_dur > 0:
            actual_end = float(seg.offset_sec or 0.0) + (actual_dur / tts_speed)
            if actual_end > actual_max_end:
                actual_max_end = actual_end
    if used == 0:
        return 0.0

    # #410 — scene_visual_length 메타 기반 길이와 actual wav 끝점 중 큰 값 채택.
    # 메타 stale 로 atrim 이 wav 끝을 자르는 회귀(1번 씬 "는데요" 잘림 사례) 차단.
    if actual_max_end > scene_dur:
        scene_dur = actual_max_end

    mix_in = "".join(labels)
    if used == 1:
        fc = (
            filter_parts[0]
            + f";{mix_in}atrim=end={scene_dur:.3f},asetpts=PTS-STARTPTS[aout]"
        )
    else:
        fc = (
            ";".join(filter_parts)
            + f";{mix_in}amix=inputs={used}:normalize=0:duration=longest"
            + f",atrim=end={scene_dur:.3f},asetpts=PTS-STARTPTS[aout]"
        )

    merged_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [
            ffmpeg_bin(), "-y", *inputs,
            "-filter_complex", fc,
            "-map", "[aout]", "-ar", "24000", "-ac", "1",
            str(merged_path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0 or not merged_path.exists():
        msg = (
            f"[narration_merge] merged narration wav 합성 실패 scene="
            f"{scene.scene_id}: {r.stderr[-500:]}"
        )
        if log is not None:
            try:
                log(msg)
            except Exception:
                print(msg, flush=True)
        else:
            print(msg, flush=True)
        return 0.0
    return wav_duration_sec(merged_path)
