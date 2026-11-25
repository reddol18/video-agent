"""Phase 6 — 최종 합성기

muted_output.mp4 + 씬별 나레이션 WAV → final_output.mp4
"""

import subprocess
import sys
from pathlib import Path

from rich.console import Console

from .utils.file_cache import load_json
from .utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

console = Console()

FINAL_OUTPUT = "output/final_output.mp4"

# SFX 길이를 못 재면 이 값으로 가정한다. 더킹 창 계산에만 쓰이며, 넉넉히 잡아도
# 표현식 길이에는 거의 영향이 없다(창에 걸치는 TTS 구간 수만 조금 늘어난다).
_SFX_DURATION_FALLBACK_SEC = 5.0


def _probe_duration(path: str) -> float:
    """오디오 파일 길이(초). 실패 시 폴백값을 돌려준다(예외 없음)."""
    try:
        r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if r.returncode == 0:
            return float(r.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    return _SFX_DURATION_FALLBACK_SEC


def _build_sfx_track_for_overlay(
    abs_start_sec: float,
    sfx_path: str,
    sfx_volume: float,
    ducks: list[tuple[float, float]],
    input_idx: int,
    label: str,
    duck_db: float = 10.0,
    attack_sec: float = 0.1,
    release_sec: float = 0.3,
    max_duration_sec: float | None = None,
) -> tuple[list[str], str]:
    """단일 SFX 트랙용 ffmpeg filter chain 조각을 생성한다 (#290).

    Args:
        abs_start_sec: 영상 타임라인 기준 SFX 트리거 시각(초).
        sfx_path: SFX 오디오 파일 경로.
        sfx_volume: 기본 볼륨(0.0~1.0).
        ducks: TTS 활성 구간 목록 [(start_sec, end_sec), ...] (영상 타임라인 기준).
        input_idx: ffmpeg -i 인덱스 (filter 내에서 `[{idx}:a]`로 참조).
        label: 결과 라벨 (예: "sfx0").
        duck_db: 감쇠 깊이 dB (기본 10dB ≈ 0.32배).
        attack_sec: TTS 시작 직전 빠른 어택.
        release_sec: TTS 종료 직후 자연스러운 릴리즈.

    Returns:
        (filter_parts, output_label)
        - filter_parts: 이 트랙에 필요한 ";" 구분 조각들의 리스트
        - output_label: 다른 mix 라벨에 연결할 최종 라벨 (예: "[sfx0_out]")
    """
    delay_ms = max(0, int(abs_start_sec * 1000))
    duck_gain = 10 ** (-duck_db / 20.0)  # 10dB → 약 0.316
    # 씬 경계 클램프 — max_duration_sec 가 있으면 그 길이에서 자른다(끝 0.3초 페이드아웃).
    # 긴 효과음(장작·박수 등)이 씬이 끝난 뒤 다음 씬까지 깔리는 것을 막는다.
    trim = ""
    sfx_full = None
    if max_duration_sec is not None and max_duration_sec > 0:
        sfx_full = _probe_duration(sfx_path)
        if sfx_full > max_duration_sec + 0.01:
            fade = min(0.3, max_duration_sec / 2)
            trim = (f"atrim=end={max_duration_sec:.3f},"
                    f"afade=t=out:st={max(0.0, max_duration_sec - fade):.3f}:d={fade:.3f},")
    # adelay로 트리거 시각에 배치
    parts = [f"[{input_idx}:a]{trim}adelay={delay_ms}|{delay_ms},volume={sfx_volume}[{label}_pre]"]

    # #ducking-limit — SFX 가 실제로 소리나는 구간에 걸치는 더킹만 남긴다.
    #
    # SFX 는 보통 1초 미만인데, 기존 코드는 영상 전체의 TTS 구간(이 프로젝트는
    # 71개)을 모두 표현식에 넣었다. ffmpeg 표현식은 길이/복잡도 상한(약 1000자)이
    # 있어, 씬이 30개를 넘으면 "Error when evaluating the volume expression" 으로
    # 최종 합성이 통째로 실패한다.
    #
    # SFX 재생 창 밖의 더킹 구간은 어차피 무음 구간에 적용되므로 결과에 영향이
    # 없다. 창에 걸치는 것만 남기면 표현식이 항상 짧게 유지된다.
    if ducks:
        sfx_dur = sfx_full if sfx_full is not None else _probe_duration(sfx_path)
        if max_duration_sec is not None and max_duration_sec > 0:
            sfx_dur = min(sfx_dur, max_duration_sec)
        win_start = abs_start_sec - attack_sec
        win_end = abs_start_sec + sfx_dur + release_sec
        ducks = [
            (ts, te) for (ts, te) in ducks
            if te + release_sec >= win_start and ts - attack_sec <= win_end
        ]

    if not ducks:
        # 더킹 없음 — 그대로 통과
        return parts, f"[{label}_pre]"

    # ducks → 영상 타임라인 기준 활성 구간. volume enable expression 빌드.
    #   기본 1.0(통과), 더킹 구간이면 duck_gain(0.316). 어택/릴리즈는
    #   ffmpeg volume의 enable 값이 step 함수이므로 정확한 envelope은 어렵다.
    #   대신 expression 기반 다단 보간: max(duck_gain, ...) 형태 + between 사용.
    #
    # 단순화: enable=between(t,Start,End) 일 때 duck_gain, 그 외 1.0.
    # 어택은 (Start-attack) 부터 Start 까지 선형 감쇠, 릴리즈는 End 부터 End+release 까지 선형 회복.
    # 이 모든 것을 ffmpeg `volume=eval=frame` 의 expression으로 구현한다.
    #
    # volume=eval=frame:volume='expr'
    # expr 내에서 t (현재 시각, 트랙 기준 — adelay 이후이므로 영상 타임라인과 일치)
    expr_parts = ["1"]  # 기본 1.0
    for (ts, te) in ducks:
        # 어택: ts-attack ~ ts (1.0 → duck_gain), 코어: ts ~ te (duck_gain),
        # 릴리즈: te ~ te+release (duck_gain → 1.0)
        a_start = max(0.0, ts - attack_sec)
        r_end = te + release_sec
        # 어택 구간 진행도 (0 → 1): (t - a_start) / attack
        # 어택 시 게인: 1 - (1 - duck_gain) * progress
        attack_expr = (
            f"if(between(t,{a_start:.3f},{ts:.3f}),"
            f"1-(1-{duck_gain:.4f})*((t-{a_start:.3f})/{max(attack_sec, 0.001):.3f}),1)"
        )
        # 코어 구간
        core_expr = f"if(between(t,{ts:.3f},{te:.3f}),{duck_gain:.4f},1)"
        # 릴리즈: duck_gain + (1 - duck_gain) * progress
        release_expr = (
            f"if(between(t,{te:.3f},{r_end:.3f}),"
            f"{duck_gain:.4f}+(1-{duck_gain:.4f})*((t-{te:.3f})/{max(release_sec, 0.001):.3f}),1)"
        )
        # 한 구간에서는 셋 중 하나가 작아짐. min을 사용해 모든 구간 envelope를 합성.
        expr_parts.append(attack_expr)
        expr_parts.append(core_expr)
        expr_parts.append(release_expr)
    # #ducking-depth — min()을 구간마다 중첩하면 씬이 많을 때 ffmpeg 표현식
    # 파서의 중첩 한계(약 100단)를 넘겨 "Error when evaluating the volume
    # expression" 으로 렌더가 통째로 실패한다(구간 32개 = 97단에서 재현).
    #
    # 더킹 구간은 서로 겹치지 않으므로, 각 구간의 "1로부터의 감쇠량"을 단순히
    # 더한 뒤 1에서 빼면 min 중첩과 같은 envelope를 얻는다. 이 형태는 구간이
    # 몇 개든 중첩 깊이가 일정하다.
    #   envelope = 1 - Σ(1 - gain_i)
    # 겹침이 생기더라도 아래 clip으로 duck_gain 미만으로는 내려가지 않는다.
    drop_terms = [f"(1-{ex})" for ex in expr_parts[1:]]
    if drop_terms:
        total_drop = "+".join(drop_terms)
        # 하한을 duck_gain으로 고정 — 구간이 겹쳐도 과도한 감쇠를 막는다.
        envelope_expr = f"max({duck_gain:.4f},1-({total_drop}))"
    else:
        envelope_expr = expr_parts[0]

    parts.append(
        f"[{label}_pre]volume=eval=frame:volume='{envelope_expr}'[{label}_out]"
    )
    return parts, f"[{label}_out]"


def run(bgm_path: str | None = None, force: bool = False,
        sfx_entries: list[dict] | None = None, watermark: bool = False) -> str:
    """최종 합성 실행. 출력 파일 경로 반환.

    watermark: True 면 마지막 영상 인코딩 직전 프레임에 비가시 워터마크(Video Seal)를 넣는다
        (src/watermark.py). 기본 False — 기존 동작(영상 copy) 그대로.

    Args:
        bgm_path: 단일 BGM (레거시). 후처리 BGM 에디터(#289)는 final_with_bgm를 별도로 만든다.
        force: 캐시 무시 여부.
        sfx_entries: 오버레이 SFX 목록 (#290). 각 항목:
            { "abs_start_sec": float, "path": str, "volume": float }
            None/빈 리스트면 SFX 없이 기존 동작 유지.
    """
    if not force and Path(FINAL_OUTPUT).exists():
        console.print("[dim]  Phase 6: final_output 캐시 사용[/dim]")
        return FINAL_OUTPUT

    entries_data = load_json("output/narration/narration_timeline.json")
    if not entries_data:
        raise RuntimeError("output/narration/narration_timeline.json 이 없어요. Phase 4-A를 먼저 실행해주세요.")
    entries = entries_data["entries"]

    # scene_plan에서 sound_effect offset_sec 정보 로드
    scene_plan_data = load_json("cache/scene_plan.json")
    sound_offset_map: dict[int, float] = {}
    if scene_plan_data:
        for scene in scene_plan_data.get("scenes", []):
            se = scene.get("sound_effect")
            if se and se.get("file"):
                sound_offset_map[scene["scene_id"]] = float(se.get("offset_sec", 0.3))

    # 출력 해상도 읽기 (기본값 1920×1080)
    sel_data = load_json("cache/clip_selection.json")
    output_width = int(sel_data.get("output_width", 1920)) if sel_data else 1920
    output_height = int(sel_data.get("output_height", 1080)) if sel_data else 1080
    # 프로젝트 전역 배경색(letterbox 여백). 기본 "black" — 미지정 시 기존과 동일.
    from .utils.ffmpeg_utils import normalize_bg_color as _normalize_bg_color
    _bg_norm = _normalize_bg_color(sel_data.get("bg_color", "black") if sel_data else "black")
    _bg_pad_suffix = "" if _bg_norm == "black" else f":{_bg_norm}"

    muted = "output/muted_output.mp4"
    if not Path(muted).exists():
        raise RuntimeError(f"{muted} 이 없어요. Phase 5를 먼저 실행해주세요.")

    # timeline.json에서 씬별 src_audio_file 경로 로드 (현장음 믹싱용)
    timeline_data = load_json("cache/timeline.json")
    src_audio_map: dict[int, str] = {}
    if timeline_data:
        for ts in timeline_data.get("timestamps", []):
            saf = ts.get("src_audio_file")
            if saf and Path(saf).exists():
                src_audio_map[ts["scene_id"]] = saf

    console.print(f"\n[bold cyan]Phase 6[/bold cyan] 최종 합성 중...")

    # ── Step A: narration WAV들을 중간 파일 하나로 합치기 ────────────────────
    # 인풋이 많을 때 한 번에 처리하면 메모리 부족이 발생하므로 2단계로 분리한다.
    from .utils.ffmpeg_utils import get_duration as _get_dur

    muted_dur = _get_dur(muted)
    narr_inputs = []
    narr_filter_parts = []
    narr_mix_labels = ""
    idx = 0

    for entry in entries:
        wav = entry["audio_file"]
        if not Path(wav).exists():
            console.print(f"  [yellow]  씬 {entry['scene_id']} 나레이션 파일 없음: {wav}[/yellow]")
            continue
        scene_id = entry["scene_id"]
        if scene_id in sound_offset_map:
            delay_ms = int((entry["start_sec"] + sound_offset_map[scene_id]) * 1000)
        else:
            delay_ms = int(entry["start_sec"] * 1000)
        narr_inputs += ["-i", wav]
        lbl = f"n{idx}"
        narr_filter_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms}[{lbl}]")
        narr_mix_labels += f"[{lbl}]"
        idx += 1

    narr_mixed_wav = "output/narration/_narr_mixed.wav"
    Path(narr_mixed_wav).parent.mkdir(parents=True, exist_ok=True)

    if idx > 0:
        narr_filter = ";".join(narr_filter_parts)
        narr_filter += f";{narr_mix_labels}amix=inputs={idx}:normalize=0:duration=longest[aout]"
        r = subprocess.run(
            [ffmpeg_bin(), "-y", *narr_inputs,
             "-filter_complex", narr_filter,
             "-map", "[aout]", "-ar", "44100", "-ac", "2",
             "-t", str(muted_dur),
             narr_mixed_wav],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            raise RuntimeError(f"나레이션 믹스 실패: {r.stderr}")
    else:
        # 나레이션 없음 — 무음 WAV 생성
        r = subprocess.run(
            [ffmpeg_bin(), "-y", "-f", "lavfi", "-i", f"aevalsrc=0:c=stereo:s=44100:d={muted_dur:.3f}",
             narr_mixed_wav],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            raise RuntimeError(f"무음 WAV 생성 실패: {r.stderr}")

    # ── Step B: 현장음 믹스 (있는 경우) ──────────────────────────────────────
    src_audio_count = 0
    if src_audio_map and timeline_data:
        src_inputs = []
        src_filter_parts = []
        src_mix_labels = ""
        src_idx = 0
        ts_map = {ts["scene_id"]: ts for ts in timeline_data.get("timestamps", [])}
        for scene_id, saf in sorted(src_audio_map.items()):
            ts = ts_map.get(scene_id)
            if ts is None:
                continue
            # 씬별 footage_audio_volume — 0=음소거(트랙 자체 스킵), 0.3=기본, 1.0=풀.
            fav = float(ts.get("footage_audio_volume", 0.3))
            if fav <= 0:
                continue  # 무음이면 입력 추가하지 않음 (CPU/I/O 절감)
            delay_ms = int(ts["start_sec"] * 1000)
            src_inputs += ["-i", saf]
            lbl = f"s{src_idx}"
            src_filter_parts.append(f"[{src_idx}:a]volume={fav},adelay={delay_ms}|{delay_ms}[{lbl}]")
            src_mix_labels += f"[{lbl}]"
            src_idx += 1
            src_audio_count += 1

        if src_audio_count > 0:
            src_mixed_wav = "output/narration/_src_mixed.wav"
            src_filter = ";".join(src_filter_parts)
            src_filter += f";{src_mix_labels}amix=inputs={src_audio_count}:normalize=0:duration=longest[aout]"
            r = subprocess.run(
                [ffmpeg_bin(), "-y", *src_inputs,
                 "-filter_complex", src_filter,
                 "-map", "[aout]", "-ar", "44100", "-ac", "2",
                 "-t", str(muted_dur),
                 src_mixed_wav],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                raise RuntimeError(f"현장음 믹스 실패: {r.stderr}")

    # ── Step C: 최종 합성 ─────────────────────────────────────────────────────
    final_inputs = ["-i", muted, "-i", narr_mixed_wav]

    # "삐 소리" 구간에서는 나레이션을 **음소거**한다 (검열음 연출).
    #   비프 볼륨만 올리는 방식은 원리상 한계가 있다 — amix(normalize=0)는 두 소리를
    #   더할 뿐이라 말소리 자체는 그대로 남고, 비프를 키울수록 전체가 시끄러워지기만 한다
    #   (실측: 비프 0.7 → 말보다 3dB 작음 / 2.0 으로 올려도 말 성분은 -24.1dB 로 불변).
    #   진짜 "가리는" 효과는 가려야 할 구간의 나레이션을 지우는 것이다.
    _beep_mutes = [
        (float(s["abs_start_sec"]), float(s["abs_start_sec"]) + float(s.get("duration_sec") or 0.0))
        for s in (sfx_entries or [])
        if s.get("beep_mute") and s.get("abs_start_sec") is not None
    ]
    if _beep_mutes:
        # 구간 안이면 0, 밖이면 1. 경계에 5ms 램프를 둬 클릭 노이즈를 막는다.
        _R = 0.005
        _terms = [
            f"between(t,{a:.3f},{b:.3f})*min(1,min((t-{a:.3f}),({b:.3f}-t))/{_R})"
            for a, b in _beep_mutes
        ]
        _expr = "1-min(1," + "+".join(_terms) + ")"
        final_filter_parts = [f"[1:a]volume=eval=frame:volume='{_expr}'[narr_vol]"]
        print(f"[beep] 나레이션 음소거 구간 {len(_beep_mutes)}개: "
              + ", ".join(f"{a:.2f}~{b:.2f}s" for a, b in _beep_mutes))
    else:
        final_filter_parts = ["[1:a]volume=1.0[narr_vol]"]

    final_mix_labels = "[narr_vol]"
    final_mix_count = 1
    final_idx = 2

    if src_audio_count > 0:
        # 씬별 footage_audio_volume은 이미 src_mixed_wav 만들 때 적용됨 → 여기선 추가 감쇠 없이 1.0.
        final_inputs += ["-i", src_mixed_wav]
        final_filter_parts.append(f"[{final_idx}:a]volume=1.0[src_vol]")
        final_mix_labels += "[src_vol]"
        final_mix_count += 1
        final_idx += 1

    # ── #290 — 오버레이 SFX 트랙 추가 (TTS 자동 더킹) ──────────────────────
    # TTS 활성 구간(narration_timeline entries) 동안 모든 SFX 트랙의 볼륨을
    # volume eval=frame:expr 방식으로 자동 감쇠시킨다.
    if sfx_entries:
        # TTS 활성 구간 (영상 타임라인 기준)
        duck_intervals: list[tuple[float, float]] = []
        for e in entries:
            try:
                ts = float(e.get("start_sec", 0.0))
                te = float(e.get("end_sec", ts))
                if te > ts:
                    duck_intervals.append((ts, te))
            except (TypeError, ValueError):
                continue

        for sfx_i, sfx in enumerate(sfx_entries):
            sfx_path = sfx.get("path", "")
            if not sfx_path or not Path(sfx_path).exists():
                continue
            abs_start = float(sfx.get("abs_start_sec", 0.0))
            sfx_vol = float(sfx.get("volume", 0.7))
            # "삐 소리" 등 no_duck 엔트리는 TTS 더킹 대상에서 제외한다. 비프는 마지막 TTS
            # 끝부분에 *겹쳐서* 나므로, 더킹을 걸면 정작 가려야 할 구간에서 비프가 작아져
            # 검열음 연출이 죽는다. 나레이션을 덮을 만큼 그대로 재생해야 한다.
            entry_ducks = [] if sfx.get("no_duck") else duck_intervals
            final_inputs += ["-i", sfx_path]
            parts, label = _build_sfx_track_for_overlay(
                abs_start_sec=abs_start,
                sfx_path=sfx_path,
                sfx_volume=sfx_vol,
                ducks=entry_ducks,
                input_idx=final_idx,
                label=f"sfx{sfx_i}",
                max_duration_sec=sfx.get("max_duration_sec"),
            )
            final_filter_parts.extend(parts)
            final_mix_labels += label
            final_mix_count += 1
            final_idx += 1

    if bgm_path and Path(bgm_path).exists():
        final_inputs += ["-i", bgm_path]
        final_filter_parts.append(f"[{final_idx}:a]volume=0.15[bgm]")
        final_mix_labels += "[bgm]"
        final_mix_count += 1
        final_idx += 1

    final_filter = ";".join(final_filter_parts)
    final_filter += f";{final_mix_labels}amix=inputs={final_mix_count}:normalize=0[aout]"

    # muted_output 실제 해상도 확인
    probe_r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", muted],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    muted_w, muted_h = output_width, output_height
    try:
        parts = probe_r.stdout.strip().split(",")
        if len(parts) == 2:
            muted_w, muted_h = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        pass

    needs_scale = (muted_w != output_width or muted_h != output_height)

    Path(FINAL_OUTPUT).parent.mkdir(parents=True, exist_ok=True)

    # ── 비가시 워터마크 (옵션) ────────────────────────────────────────────
    # 해상도 맞춤(scale/pad)도 이 단계 디코딩에서 같이 처리해, 워터마크 뒤에는
    # 영상 재인코딩이 없도록(-c:v copy) 한다.
    wm_record: dict | None = None
    if watermark:
        from . import watermark as _wm
        _wm_vf = None
        if needs_scale:
            _wm_vf = (
                f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,"
                f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2{_bg_pad_suffix},setsar=1"
            )
        wm_path = "output/muted_output_wm.mp4"
        console.print(f"  🔏 비가시 워터마크 삽입 중 ({_wm.model_name()})...")
        wm_record = _wm.embed_video(muted, wm_path, vf=_wm_vf,
                                    out_size=(output_width, output_height) if _wm_vf else None)
        console.print(f"  🔏 워터마크 삽입 완료 ({wm_record['elapsed_sec']}s)")
        final_inputs[1] = wm_path
        needs_scale = False

    if needs_scale:
        # 해상도 불일치 → scale/pad + libx264 재인코딩
        scale_vf = (
            f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2{_bg_pad_suffix},setsar=1"
        )
        # filter_complex에 video scale을 추가
        combined_filter = f"[0:v]{scale_vf}[vscaled];{final_filter}"
        cmd = [
            ffmpeg_bin(), "-y",
            *final_inputs,
            "-filter_complex", combined_filter,
            "-map", "[vscaled]",
            "-map", "[aout]",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            FINAL_OUTPUT,
        ]
    else:
        cmd = [
            ffmpeg_bin(), "-y",
            *final_inputs,
            "-filter_complex", final_filter,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            FINAL_OUTPUT,
        ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"최종 합성 실패: {result.stderr}")

    if wm_record is not None:
        from . import watermark as _wm
        wm_record["final_file"] = FINAL_OUTPUT
        wm_record["final_sha256"] = _wm.sha256_file(FINAL_OUTPUT)
        _wm.append_log(wm_record)
        console.print(f"  🔏 출처 기록 저장: {_wm.LOG_PATH}")

    if src_audio_map:
        console.print(f"  [dim]현장음 믹싱 완료 — {len(src_audio_map)}개 씬 (볼륨 0.3)[/dim]")

    return FINAL_OUTPUT


def print_summary(output_path: str, project_root: str) -> None:
    from .utils.ffmpeg_utils import get_duration
    dur = get_duration(output_path)

    console.print()
    console.rule("[bold green]✅ Phase 6 완료 — 완성본 영상 생성[/bold green]")
    console.print(f"  길이: [bold]{dur:.1f}초[/bold] ({int(dur) // 60}분 {int(dur) % 60}초)")

    out_abs = str(Path(project_root) / output_path)
    console.print(f"\n  🎬 완성본 재생 → [link=cursor://file/{out_abs}]cursor://file/{out_abs}[/link]")
    console.print("  (파일을 직접 열거나, 아래 편집 모드에서 수정 요청을 할 수 있어요.)")


if __name__ == "__main__":
    project_root = str(Path(__file__).parent.parent)
    result = run()
    print_summary(result, project_root)
