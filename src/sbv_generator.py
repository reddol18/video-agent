"""Phase 4-B — SBV 자막 생성기

narration_timeline.json을 읽어 output/final_output.sbv를 생성한다.
"""

import re
import sys
from pathlib import Path

from rich.console import Console

from .utils.file_cache import load_json

console = Console()

SUBTITLES_PATH = "output/final_output.sbv"
MIN_BLOCK_CHARS = 20  # 이 이하면 다음 문장과 병합


def _fmt_time(sec: float) -> str:
    """초 → SBV 타임코드 (H:MM:SS.mmm)"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:06.3f}"


def _split_sentences(text: str) -> list[str]:
    """문장 부호 기준으로 텍스트 분리. 짧은 문장은 다음과 병합."""
    parts = re.split(r"(?<=[.?!—])\s*", text.strip())
    parts = [p.strip() for p in parts if p.strip()]

    merged = []
    buf = ""
    for p in parts:
        buf = (buf + " " + p).strip() if buf else p
        if len(buf) >= MIN_BLOCK_CHARS:
            merged.append(buf)
            buf = ""
    if buf:
        if merged:
            merged[-1] += " " + buf
        else:
            merged.append(buf)
    return merged


def run(force: bool = False) -> str:
    """SBV 자막 파일 생성. 저장 경로 반환."""
    narr_path = Path("output/narration/narration_timeline.json")
    sbv_path = Path(SUBTITLES_PATH)
    if (
        not force
        and sbv_path.exists()
        and narr_path.exists()
        and sbv_path.stat().st_mtime >= narr_path.stat().st_mtime
    ):
        console.print("[dim]  Phase 4-B: 자막 캐시 사용[/dim]")
        return SUBTITLES_PATH

    entries_data = load_json("output/narration/narration_timeline.json")
    if not entries_data:
        raise RuntimeError("output/narration/narration_timeline.json 이 없어요. Phase 4-A를 먼저 실행해주세요.")
    entries = entries_data["entries"]

    console.print(f"\n[bold cyan]Phase 4-B[/bold cyan] 자막 생성 중...")

    blocks: list[tuple[float, float, str]] = []
    for entry in entries:
        # #382 — segment 단위 자막 지원. entry["segments"] 가 있으면 segment 별로 자막 블록을 만든다.
        scene_start = float(entry.get("start_sec", 0.0))
        scene_dur = float(entry.get("duration_sec", 0.0))
        # 씬 시각 길이(=overflow 클램프 상한). 누락 시 entry duration 으로 폴백.
        scene_visual = float(entry.get("scene_visual_duration_sec", scene_dur))
        segments = entry.get("segments") or []

        if segments:
            # 각 segment 의 (offset_sec_in_scene, end_sec_in_scene) 을 영상 타임라인 기준으로 변환.
            seg_blocks: list[tuple[float, float, str]] = []
            for seg in sorted(segments, key=lambda s: float(s.get("offset_sec", 0.0))):
                seg_offset = float(seg.get("offset_sec", 0.0))
                seg_end_in_scene = float(seg.get("end_sec_in_scene", seg_offset))
                # 씬 시각 길이를 넘어가는 segment 는 씬 경계에서 자름 (이슈 본문 명시).
                if seg_end_in_scene > scene_visual:
                    seg_end_in_scene = scene_visual
                if seg_end_in_scene <= seg_offset:
                    continue
                abs_start = scene_start + seg_offset
                abs_end = scene_start + seg_end_in_scene
                seg_text = seg.get("sbv_text") or seg.get("text") or ""
                if not seg_text.strip():
                    continue
                # 문장 분할로 가독성 향상
                sentences = _split_sentences(seg_text)
                total_chars = sum(len(s) for s in sentences)
                total_dur = abs_end - abs_start
                cursor = abs_start
                for sentence in sentences:
                    ratio = len(sentence) / total_chars if total_chars > 0 else 1 / len(sentences)
                    dur = total_dur * ratio
                    seg_blocks.append((cursor, cursor + dur, sentence))
                    cursor += dur
            # SBV 규격상 시작 시각 오름차순 보장 — 겹치는 segment 는 그대로 두되 정렬만.
            seg_blocks.sort(key=lambda b: b[0])
            blocks.extend(seg_blocks)
            continue

        # ── 옛 동작 폴백 (entry["segments"] 없음) — 회귀 안전 ─────────────────
        subtitle_text = entry.get("subtitle_text") or entry.get("narration_text") or ""
        sentences = _split_sentences(subtitle_text)
        total_chars = sum(len(s) for s in sentences)
        total_dur = entry["duration_sec"]
        cursor = entry["start_sec"]

        for sentence in sentences:
            ratio = len(sentence) / total_chars if total_chars > 0 else 1 / len(sentences)
            dur = total_dur * ratio
            blocks.append((cursor, cursor + dur, sentence))
            cursor += dur

    Path(SUBTITLES_PATH).parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for start, end, text in blocks:
        lines.append(f"{_fmt_time(start)},{_fmt_time(end)}")
        lines.append(text)
        lines.append("")

    with open(SUBTITLES_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    console.print(f"  {len(blocks)}개 자막 블록 생성 완료")
    return SUBTITLES_PATH


def print_summary(path: str, project_root: str) -> None:
    sbv_abs = str(Path(project_root) / path)
    console.print()
    console.rule("[bold green]✅ Phase 4-B 완료 — 자막 생성[/bold green]")
    console.print(f"  📄 자막 파일 → [link=cursor://file/{sbv_abs}]cursor://file/{sbv_abs}[/link]")
    console.print("  YouTube, Vimeo 등에 직접 업로드 가능한 SBV 형식이에요.")


if __name__ == "__main__":
    project_root = str(Path(__file__).parent.parent)
    result = run()
    print_summary(result, project_root)
