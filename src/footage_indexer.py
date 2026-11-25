"""Phase 2 — 재료 영상 인덱서

footage/ 폴더의 영상들을 청크로 분할하고 Gemini로 분석하여
cache/footage_index.json을 저장한다.

이미지 파일(.jpg, .jpeg, .png, .bmp, .webp)이 footage/ 폴더에 있으면
Ken Burns 효과를 적용하여 동적 mp4 클립으로 변환한 뒤 동일한 파이프라인에 투입한다.
변환된 mp4는 cache/ken_burns/ 에 보관되며, source_file은 변환된 mp4(cache/ken_burns/{stem}.mp4) 경로를 가리킨다.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from .utils.file_cache import file_fingerprint, load_json, save_json
from .utils.ffmpeg_utils import get_duration, has_audio, transcode_for_gemini
from .utils.gemini_client import generate_json_with_video

console = Console()

INDEX_PATH = "cache/footage_index.json"
SUPPORTED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# Ken Burns 기본 파라미터
KEN_BURNS_DEFAULTS = {
    "duration": float(os.getenv("KEN_BURNS_DURATION_SEC", "5")),
    "start_scale": float(os.getenv("KEN_BURNS_START_SCALE", "1.0")),
    "end_scale": float(os.getenv("KEN_BURNS_END_SCALE", "1.15")),
    "width": int(os.getenv("KEN_BURNS_WIDTH", "1280")),
    "height": int(os.getenv("KEN_BURNS_HEIGHT", "720")),
    "fit_mode": os.getenv("KEN_BURNS_FIT_MODE", "cover"),
    "anchor_x": float(os.getenv("KEN_BURNS_ANCHOR_X", "0.5")),
    "anchor_y": float(os.getenv("KEN_BURNS_ANCHOR_Y", "0.5")),
    "fps": int(os.getenv("KEN_BURNS_FPS", "30")),
}

KEN_BURNS_CACHE_DIR = "cache/ken_burns"

## DEPRECATED ── 이전 슬라이딩 윈도우 청크 분할 상수 (사용자 결정으로 무력화).
## 호환을 위해 정의만 유지하지만 _chunk_ranges 는 이제 "파일 1개 = 청크 1개" 정책으로
## 항상 단일 튜플 [(0.0, duration)] 만 반환한다.
CHUNK_SEC = int(os.getenv("CHUNK_DURATION_SEC", "10"))
OVERLAP_SEC = int(os.getenv("CHUNK_OVERLAP_SEC", "2"))
MIN_QUALITY = float(os.getenv("MIN_QUALITY_SCORE", "0.5"))

# #355 후속 (사용자 결정 2026-06): Gemini Flash 무료 티어 RPM(~15) 보호용 고정 인터벌.
# 사용자 결정(2026-06-05 재조정): 4초 → 10초.
#   - Tier 1 Flash-Lite RPM 한도(4,000)는 충분하지만 호출당 토큰량(영상 60s = ~17.7K input tok)
#     에 따라 TPM 한도가 진짜 병목으로 추정됨. 4초 간격(분당 15회) × 17.7K = ~265K TPM 초과 위험.
#   - 10초 간격(분당 6회) × 17.7K = ~106K TPM 으로 안전선 확보.
_RPM_INTERVAL_SEC = float(os.getenv("GEMINI_INDEX_RPM_INTERVAL_SEC", "10"))

ANALYSIS_PROMPT = """이 영상 클립을 분석하여 아래 JSON 형식으로 반환하세요.

{
  "description": string,    // 장면을 한국어 2문장으로 묘사. 인물, 배경, 행동 포함
  "mood": string,           // 분위기 형용사 3개 이하 (예: "고요함, 차분함, 새벽")
  "keywords": [string],     // 검색용 명사 5개 이하
  "quality_score": float,   // 0.0~1.0. 1.0=흔들림없음/노출적절/피사체명확, 0.5=약간흔들림, 0.0=심한흔들림/블러
  "has_audio": bool         // 의미있는 현장음 또는 대화 포함 여부
}

순수 JSON만 반환하세요."""


def _chunk_ranges(duration: float) -> list[tuple[float, float]]:
    """영상 길이에서 청크 구간 목록 생성.

    사용자 결정(2026-06): 파일 1개 = 청크 1개 정책.
      - 기존 10s/2s 슬라이딩 윈도우는 영상 1개당 Gemini 호출 횟수를 폭증시켜
        비용이 부담스러웠다. 단일 청크로 통합하여 호출 횟수를 N→1 로 축소.
      - 영상 전체에 대한 description 한 줄로도 매칭 품질은 충분(=대부분 footage 가
        한 가지 장면).
      - duration <= 0 인 경우 빈 리스트 반환(이전 동작과 동일 — 분석 스킵).

    함수 시그니처는 보존 — 다른 모듈에서 import 해도 영향 없음.
    """
    if duration is None or duration <= 0:
        return []
    return [(0.0, float(duration))]


def _analyze_chunk(source_file: str, start: float, end: float, model: str | None = None) -> dict | None:
    """청크 하나를 트랜스코딩 후 Gemini로 분석. 실패 시 None 반환.

    model 미지정 시 gemini_client 의 _resolve_model() 폴백 체인 사용.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # start/end를 전달하여 청크 범위만 트랜스코딩 (전체 파일 처리 방지)
        transcode_for_gemini(source_file, tmp_path, start=start, end=end)
        raw = generate_json_with_video(tmp_path, ANALYSIS_PROMPT, model=model)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Gemini 응답이 dict가 아님: {type(data)}")
        return data
    except Exception as e:
        console.print(f"  [yellow]⚠️  청크 분석 실패 ({Path(source_file).name} {start:.0f}s~{end:.0f}s): {e}[/yellow]")
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _build_cached_key_set(cached_chunks: list) -> set[str]:
    """기존 캐시에 있는 파일을 파일명 기준으로 집합 반환.

    파일 경로(source_file)에서 파일명(stem+suffix)만 추출하여 저장한다.
    footage/ 디렉토리가 이동하거나 경로가 바뀌어도 파일명이 같으면 캐시 히트로 처리.

    #355 후속 (사용자 결정 2026-06): `_fallback: true` 청크는 캐시 키 집합에 포함하지
    않는다. Gemini API 일시적 429/타임아웃으로 폴백된 청크가 영원히 description 없는
    상태로 남는 문제를 해결하기 위함. 정상 청크(description 있음)는 그대로 캐시 유지되며,
    폴백 청크가 있는 파일은 force=False 로 다시 실행해도 "신규 파일"로 분류되어 자동 재시도된다.
    """
    keys: set[str] = set()
    for chunk in cached_chunks:
        if chunk.get("_fallback"):
            # 폴백 청크는 캐시 무효 — 다음 실행에서 재시도되도록 키 집합에서 제외.
            continue
        src = chunk.get("source_file", "")
        if src:
            # 파일 경로 전체와 파일명 모두 집합에 추가 (양쪽 형식 모두 허용)
            keys.add(src)
            keys.add(Path(src).name)
    return keys


def _convert_image_to_video(img_path: str) -> str | None:
    """이미지 파일에 Ken Burns 효과를 적용하여 mp4 클립으로 변환한다.

    변환 결과는 cache/ken_burns/{stem}.mp4 에 저장되며,
    이미 동일한 파일명의 변환본이 존재하면 재사용한다 (mtime 기준).

    Returns:
        변환된 mp4 파일의 경로, 실패 시 None.
    """
    try:
        from .utils.ken_burns import generate_moviepy_clip
    except ImportError:
        try:
            from src.utils.ken_burns import generate_moviepy_clip
        except ImportError:
            console.print(
                f"  [yellow]⚠️  ken_burns 모듈을 불러올 수 없어요. "
                f"moviepy / opencv-python 설치 여부를 확인해주세요.[/yellow]"
            )
            return None

    img_path_obj = Path(img_path)
    out_dir = Path(KEN_BURNS_CACHE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / f"{img_path_obj.stem}.mp4")

    # 이미 변환본이 존재하고 원본보다 최신이면 재사용
    if Path(out_path).exists():
        img_mtime = img_path_obj.stat().st_mtime
        out_mtime = Path(out_path).stat().st_mtime
        if out_mtime >= img_mtime:
            return out_path

    p = KEN_BURNS_DEFAULTS
    success = generate_moviepy_clip(
        img_path=img_path,
        output_path=out_path,
        duration=p["duration"],
        start_scale=p["start_scale"],
        end_scale=p["end_scale"],
        video_size=(p["width"], p["height"]),
        fit_mode=p["fit_mode"],
        anchor_x=p["anchor_x"],
        anchor_y=p["anchor_y"],
        fps=p["fps"],
    )
    if not success:
        console.print(f"  [red]❌ Ken Burns 변환 실패: {img_path_obj.name}[/red]")
        return None

    return out_path


def run(footage_dir: str, force: bool = False, include_files: set[str] | None = None, model: str | None = None) -> list:
    """footage_dir 내 영상을 분석하여 FootageChunk 배열 반환.

    증분 처리 전략 (source_file 기준 비교):
      - footage/ 폴더의 각 영상 파일을 순회하며, footage_index.json의 chunks 배열에서
        source_file 필드를 수집하여 비교한다.
      - 해당 파일 경로 또는 파일명이 기존 source_file에 없으면 신규 파일로 간주하여
        Gemini API로 분석 후 기존 청크에 병합한다.
      - 이미 source_file에 있으면 스킵하여 재분석하지 않는다.
      - footage_index.json이 없거나 chunks가 비어있으면 모든 파일을 신규로 간주하여
        전체 분석을 수행한다.
      - force=True이면 기존 캐시를 무시하고 전체 재분석을 강제한다.
    """
    footage_path = Path(footage_dir)
    video_files = sorted([f for f in footage_path.iterdir() if f.suffix.lower() in SUPPORTED_EXT])

    # include_files가 지정된 경우 해당 파일만 분석 (edit/sync 씬 전용 파일 제외)
    if include_files is not None:
        video_files = [f for f in video_files if f.name in include_files]

    # 이미지 파일 감지 — Ken Burns 효과 적용 후 video_files에 추가
    image_files = sorted([f for f in footage_path.iterdir() if f.suffix.lower() in IMAGE_EXT])
    if image_files:
        console.print(
            f"\n[bold cyan]Phase 2[/bold cyan] 이미지 파일 {len(image_files)}개 발견 — "
            f"Ken Burns 효과 적용 중..."
        )
        for img_file in image_files:
            converted_path = _convert_image_to_video(str(img_file))
            if converted_path is not None:
                # 변환된 mp4를 video_files에 추가한다.
                # source_file은 converted_path(mp4)를 가리키며, 파이프라인 전반과 호환된다.
                converted = Path(converted_path)
                video_files = sorted(set(video_files) | {converted}, key=lambda p: p.name)
                console.print(
                    f"  [green]✓[/green] {img_file.name} → Ken Burns 클립 준비 완료 "
                    f"({converted.name})"
                )
            else:
                console.print(f"  [yellow]⚠️  {img_file.name} 변환 실패 — 건너뜁니다.[/yellow]")

    if not video_files:
        console.print(f"[red]❌ '{footage_dir}' 폴더에 영상 또는 이미지 파일이 없어요.[/red]")
        console.print("  MP4, MOV, AVI, MKV, JPG, PNG 형식의 파일을 footage/ 폴더에 넣어주세요.")
        return []

    # 기존 캐시 로드
    cached_data = load_json(INDEX_PATH)
    if isinstance(cached_data, dict):
        cached_chunks: list = cached_data.get("chunks", [])
        cached_failed: list = cached_data.get("failed", [])
    else:
        cached_chunks = []
        cached_failed = []

    # source_file 기준 증분 처리 여부 결정:
    #   - force=True: 항상 전체 재분석 (캐시 무시)
    #   - chunks가 비어있음(footage_index.json 없음 또는 빈 배열): 전체 신규 분석
    #   - chunks가 있음: source_file 기준으로 신규 파일만 분석
    has_cached_index = bool(cached_chunks) and not force

    # 기존 청크의 source_file 집합 구성 (경로 전체 및 파일명 양쪽 허용)
    cached_key_set: set[str] = _build_cached_key_set(cached_chunks) if has_cached_index else set()

    # 증분 처리: 기존 청크는 모두 유지하고, source_file에 없는 신규 파일만 분석 대상으로 선별
    # (footage/ 디렉토리에서 삭제된 파일의 청크도 보존 — 클립 매칭에 계속 활용 가능)
    # #355 후속 (사용자 결정 2026-06): 폴백 청크는 보존하지 않는다 — 재시도해서 새 청크로 대체.
    retained_chunks: list[dict] = (
        [c for c in cached_chunks if not c.get("_fallback")] if has_cached_index else []
    )
    fallback_retry_count = len(cached_chunks) - len(retained_chunks) if has_cached_index else 0
    new_files = [
        vf for vf in video_files
        if not has_cached_index
        or (str(vf) not in cached_key_set and vf.name not in cached_key_set)
    ]

    if has_cached_index and not new_files:
        console.print(
            f"\n[bold cyan]Phase 2[/bold cyan] 기존 인덱스 사용 — 신규 파일 없음 "
            f"({len(retained_chunks)}개 청크 유지)"
        )
        return retained_chunks

    if has_cached_index:
        console.print(
            f"\n[bold cyan]Phase 2[/bold cyan] 증분 인덱싱 — "
            f"신규 파일 {len(new_files)}개 분석 (기존 {len(retained_chunks)}개 청크 유지)"
        )
        if fallback_retry_count > 0:
            console.print(f"  🔄 이전 폴백 청크 {fallback_retry_count}개 재시도 대상")
    else:
        console.print(f"\n[bold cyan]Phase 2[/bold cyan] 재료 영상 분석 중... ({len(video_files)}개 파일)")

    console.print(f"  ⏱  rate limit 보호: 호출 사이 {_RPM_INTERVAL_SEC:.0f}초 대기")

    new_chunks: list = []
    failed_chunks: list = list(cached_failed)
    # #355 후속: 호출 카운터 — 첫 호출은 sleep 스킵, 두 번째부터 _RPM_INTERVAL_SEC 대기.
    analyze_call_count = 0

    for video_file in new_files:
        src = str(video_file)
        fp = file_fingerprint(src)

        try:
            duration = get_duration(src)
        except Exception as e:
            console.print(f"  [red]❌ {video_file.name} 읽기 실패: {e}[/red]")
            console.print("  파일이 손상됐거나 지원하지 않는 형식일 수 있어요.")
            continue

        ranges = _chunk_ranges(duration)
        file_chunks = []

        with Progress(
            TextColumn(f"  {video_file.name}"),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            TextColumn("청크"),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("", total=len(ranges))

            for start, end in ranges:
                chunk_id = f"{video_file.stem}_{int(start):04d}"
                # #355 후속 (사용자 결정 2026-06): Gemini Flash 무료 티어 RPM 보호.
                # 첫 호출은 즉시, 이후 호출 사이에 _RPM_INTERVAL_SEC 초 대기.
                if analyze_call_count > 0 and _RPM_INTERVAL_SEC > 0:
                    time.sleep(_RPM_INTERVAL_SEC)
                analyze_call_count += 1
                analysis = _analyze_chunk(src, start, end, model=model)

                if analysis is None:
                    # Gemini 분석 실패 시 파일명 기반 폴백 청크 생성
                    # — 청크를 버리지 않고 index에 포함시켜 다음 실행에서 재시도하지 않도록 함
                    stem = video_file.stem
                    try:
                        audio_present = has_audio(src)
                    except Exception:
                        audio_present = False
                    fallback_chunk = {
                        "chunk_id": chunk_id,
                        "source_file": src,
                        "start_sec": start,
                        "end_sec": end,
                        "description": stem,
                        "mood": "",
                        "keywords": [stem],
                        "quality_score": 0.5,
                        "has_audio": audio_present,
                        "_fallback": True,
                        "_mtime": fp["mtime"],
                        "_size": fp["size"],
                    }
                    file_chunks.append(fallback_chunk)
                    progress.advance(task)
                    continue

                chunk = {
                    "chunk_id": chunk_id,
                    "source_file": src,
                    "start_sec": start,
                    "end_sec": end,
                    "description": analysis.get("description", ""),
                    "mood": analysis.get("mood", ""),
                    "keywords": analysis.get("keywords", []),
                    "quality_score": float(analysis.get("quality_score", 0.5)),
                    "has_audio": bool(analysis.get("has_audio", False)),
                    # 캐시 유효성 판단용 메타데이터
                    "_mtime": fp["mtime"],
                    "_size": fp["size"],
                }
                file_chunks.append(chunk)
                progress.advance(task)

        console.print(f"  [green]✓[/green] {video_file.name}  {len(file_chunks)}개 청크 분석 완료")
        new_chunks.extend(file_chunks)

    # 기존 청크 + 신규 청크 병합하여 저장
    all_chunks = retained_chunks + new_chunks
    save_json(INDEX_PATH, {"chunks": all_chunks, "failed": failed_chunks})

    newly_failed = len(failed_chunks) - len(cached_failed)
    if newly_failed > 0:
        console.print(f"\n  [yellow]⚠️  분석 실패한 청크 {newly_failed}개는 건너뛰었어요.[/yellow]")
    elif failed_chunks:
        console.print(f"\n  [yellow]⚠️  누적 분석 실패 청크 {len(failed_chunks)}개 (이전 실패 포함)[/yellow]")

    return all_chunks


def print_summary(chunks: list, project_root: str) -> None:
    """인덱싱 결과 요약 출력."""
    console.print()
    console.rule("[bold green]✅ Phase 2 완료 — 재료 영상 인덱싱 결과[/bold green]")
    console.print(f"  총 [bold]{len(chunks)}개[/bold] 청크 분석 완료")

    quality_ok = sum(1 for c in chunks if c["quality_score"] >= MIN_QUALITY)
    console.print(f"  매칭에 사용 가능한 청크: [bold]{quality_ok}개[/bold] (품질 점수 {MIN_QUALITY} 이상)")

    index_abs = str(Path(project_root) / INDEX_PATH)
    console.print(f"\n  📄 전체 내용 보기 → [link=cursor://file/{index_abs}]cursor://file/{index_abs}[/link]")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python -m src.footage_indexer <footage_폴더>")
        sys.exit(1)

    project_root = str(Path(__file__).parent.parent)
    result = run(sys.argv[1])
    print_summary(result, project_root)
