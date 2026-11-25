"""Phase 4-A — 나레이션 생성기

Google Cloud Chirp HD TTS로 씬별 나레이션 WAV를 생성하고
output/narration/narration_timeline.json을 저장한다.
"""

import os
import shutil
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv
from pydub import AudioSegment
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

from .utils.file_cache import load_json, save_json

load_dotenv()

console = Console()

NARRATION_DIR = "output/narration"
TIMELINE_PATH = "output/narration/narration_timeline.json"

# TTS 보이스 이름은 .env의 TTS_VOICE_NAME 환경변수로 재정의 가능.
# 기본값은 .env.example 기준값인 ko-KR-Chirp3-HD-Orus (Chirp3 HD, 남성 Orus)와 통일.
# Google Cloud TTS 콘솔에서 사용 가능한 보이스 목록 확인 권장.
VOICE_NAME = os.getenv("TTS_VOICE_NAME", "ko-KR-Chirp3-HD-Orus")
SPEAKING_RATE = float(os.getenv("TTS_SPEAKING_RATE", "1.0"))
NARRATION_PADDING = 0.2  # 씬 시작 후 나레이션 시작까지 여백(초)
SILENCE_FADEOUT_THRESHOLD = 3.0  # 이 이상 침묵이면 fadeout 적용


def _resolve_sound_path(sound_file: str) -> Path | None:
    """사운드 파일 경로를 해석한다.

    - 절대경로 또는 명시적 상대경로(./로 시작)면 그대로 사용
    - 파일명만 지정하면 프로젝트 루트의 sounds/ 폴더에서 탐색
    """
    p = Path(sound_file)
    if p.is_absolute():
        return p
    if p.exists():
        return p
    # sounds/ 폴더 기준 탐색 (프로젝트 루트 기준)
    sounds_path = Path("sounds") / sound_file
    if sounds_path.exists():
        return sounds_path
    return None


def _extract_word_timestamp(wav_path: str, word: str) -> float:
    """TTS WAV 파일에서 특정 단어가 발화되는 타임스탬프를 반환한다.

    faster-whisper를 사용하여 단어별 타임스탬프를 추출하고,
    지정한 단어(부분 문자열 포함)가 처음 등장하는 시점을 반환한다.

    faster-whisper 미설치 시 graceful fallback:
    - 경고 메시지 출력 후 오디오 전체 길이의 절반을 반환

    단어를 찾지 못한 경우에도 오디오 길이의 절반을 반환한다.

    Args:
        wav_path: 분석할 WAV 파일 경로
        word: 찾을 단어 (부분 문자열 매칭)

    Returns:
        단어 발화 시작 시각 (초, float)
    """
    # WAV 전체 길이 계산 (폴백용)
    try:
        audio = AudioSegment.from_wav(wav_path)
        total_duration = len(audio) / 1000.0
    except Exception:
        total_duration = 5.0

    fallback = total_duration / 2.0

    # faster-whisper 임포트 시도
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        warnings.warn(
            "faster-whisper가 설치되어 있지 않아 sync 단어 타임스탬프를 추출할 수 없어요. "
            f"폴백: duration/2 = {fallback:.3f}s 사용. "
            "설치: uv pip install 'video-agent[sync]'",
            stacklevel=2,
        )
        return fallback

    try:
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(
            wav_path,
            language="ko",
            word_timestamps=True,
        )

        for segment in segments:
            if segment.words is None:
                continue
            for w in segment.words:
                segment_word = w.word.strip()
                if word in segment_word:
                    return float(w.start)

        # 단어를 찾지 못한 경우 폴백
        console.print(
            f"  [yellow]faster-whisper: '{word}' 단어를 찾지 못했어요. "
            f"폴백: duration/2 = {fallback:.3f}s 사용[/yellow]"
        )
        return fallback

    except Exception as e:
        console.print(f"  [yellow]faster-whisper 분석 오류 ({e}). 폴백: {fallback:.3f}s 사용[/yellow]")
        return fallback


def _tts(
    text: str,
    dst: str,
    speaking_rate: float = 1.0,
    voice_name: str | None = None,
    emotion: str | None = None,
) -> float:
    """텍스트 → WAV 파일 생성. 오디오 길이(초) 반환.

    provider 추상화(``src/tts``)로 위임하는 shim. voice 프리픽스로 Google /
    Typecast 를 자동 판별한다. 시그니처·반환 계약(WAV 저장 + 길이 반환)은
    호출부(웹 백엔드 tts.py) 호환을 위해 그대로 유지한다.

    Args:
        voice_name: TTS 보이스. None/빈 값이면 환경변수 ``TTS_VOICE_NAME``
            기본값(VOICE_NAME, Google) 사용. ``tc_``/``uc_`` 프리픽스면 Typecast,
            그 외(``ko-KR-...``)면 Google 로 라우팅된다.
    """
    from .tts import synthesize
    effective_voice = (voice_name or "").strip() or VOICE_NAME
    return synthesize(
        text, dst, voice=effective_voice, rate=speaking_rate, emotion=emotion
    )


def run(force: bool = False) -> list:
    """씬별 나레이션 생성. NarrationEntry 배열 반환.

    timeline.json 없이 scene_plan.json만으로 TTS를 생성한다.
    start_sec/end_sec는 Phase 5에서 실제 영상 타임라인이 확정된 후 덮어쓴다.
    """
    scenes_data = load_json("cache/scene_plan.json")
    if not scenes_data:
        raise RuntimeError("cache/scene_plan.json 이 없어요. Phase 1을 먼저 실행해주세요.")
    scenes = scenes_data["scenes"]

    if not force:
        cached = load_json(TIMELINE_PATH)
        if cached:
            console.print("[dim]  Phase 4-A: 나레이션 캐시 사용[/dim]")
            return cached["entries"]

    console.print(f"\n[bold cyan]Phase 4-A[/bold cyan] 나레이션 생성 중... ({len(scenes)}개 씬)")

    entries = []
    with Progress(
        TextColumn("  씬 {task.fields[scene_id]}"),
        BarColumn(bar_width=20),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("", total=len(scenes), scene_id="?")

        for scene in scenes:
            scene_id = scene["scene_id"]
            progress.update(task, scene_id=scene_id)

            narr_text = scene["narration_text"]
            dst = str(Path(NARRATION_DIR) / f"narration_scene_{scene_id:03d}.wav")
            sound_effect = scene.get("sound_effect")

            # sound_effect가 있으면 TTS 건너뜀 — 사운드 파일을 narration WAV로 복사
            if sound_effect and sound_effect.get("file"):
                sound_file = sound_effect["file"]
                sound_path = _resolve_sound_path(sound_file)
                if sound_path and sound_path.exists():
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(sound_path), dst)
                    audio = AudioSegment.from_file(dst)
                    audio_dur = len(audio) / 1000.0
                    console.print(f"  [dim]씬 {scene_id}: sound_effect 사용 ({sound_file}, {audio_dur:.2f}s)[/dim]")
                else:
                    console.print(f"  [yellow]씬 {scene_id}: sound_effect 파일을 찾을 수 없어요: {sound_file} — TTS로 대체[/yellow]")
                    audio_dur = _tts(narr_text, dst, speaking_rate=SPEAKING_RATE) if narr_text else 0.0
            else:
                if not narr_text:
                    # 빈 텍스트는 TTS 없이 2초 무음 WAV 저장
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    AudioSegment.silent(duration=2000).export(dst, format="wav")
                    audio_dur = 2.0
                else:
                    audio_dur = _tts(narr_text, dst, speaking_rate=SPEAKING_RATE)

            # sync 타입 씬: TTS 생성 후 faster-whisper로 단어 타임스탬프 추출
            footage_field = scene.get("footage")
            sync_word_sec: float | None = None
            if (
                isinstance(footage_field, dict)
                and footage_field.get("type") == "sync"
                and Path(dst).exists()
                and audio_dur > 0
            ):
                sync_word = footage_field.get("word", "")
                if sync_word:
                    sync_word_sec = _extract_word_timestamp(dst, sync_word)
                    console.print(
                        f"  [dim]씬 {scene_id}: sync 단어 '{sync_word}' "
                        f"T_audio={sync_word_sec:.3f}s[/dim]"
                    )

            # start_sec/end_sec는 Phase 5에서 타임라인 확정 후 갱신됨 (0으로 초기화)
            entry: dict = {
                "scene_id": scene_id,
                "narration_text": narr_text,
                "audio_file": dst,
                "duration_sec": round(audio_dur, 3),
                "start_sec": 0.0,
                "end_sec": round(audio_dur, 3),
                "speaking_rate": SPEAKING_RATE,
            }
            if sync_word_sec is not None:
                entry["sync_word_sec"] = round(sync_word_sec, 3)

            entries.append(entry)
            progress.advance(task)

    save_json(TIMELINE_PATH, {"entries": entries})
    return entries


def print_summary(entries: list, project_root: str) -> None:
    console.print()
    console.rule("[bold green]✅ Phase 4-A 완료 — 나레이션 생성[/bold green]")
    console.print(f"  {len(entries)}개 씬 나레이션 생성 완료")

    tl_abs = str(Path(project_root) / TIMELINE_PATH)
    console.print(f"\n  📄 타이밍 정보 → [link=cursor://file/{tl_abs}]cursor://file/{tl_abs}[/link]")


if __name__ == "__main__":
    project_root = str(Path(__file__).parent.parent)
    result = run()
    print_summary(result, project_root)
