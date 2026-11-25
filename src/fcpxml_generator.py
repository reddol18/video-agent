"""Phase 6 이후 FCPXML v1.11 타임라인 내보내기 생성기.

cache/clip_selection.json + output/narration/narration_timeline.json →
output/final_output.fcpxml (Final Cut Pro / DaVinci Resolve에서 열기 가능)
"""

import math
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from rich.console import Console

from .utils.file_cache import load_json
from .utils.ffmpeg_utils import get_duration

console = Console()
PROJECT_ROOT = str(Path(__file__).parent.parent)

# FCPXML 기본 프레임레이트: 24000/1001 ≈ 23.976 fps (FCP 기본값)
# 분수 타임값 분모는 frameRate × 1000 배수. 24fps 기준으로 24000 사용.
_TIMEBASE = 24000  # 분모 (s 단위 분수의 분모)
_FPS = 24          # 프레임 레이트


def secs_to_fcp(seconds: float) -> str:
    """FFmpeg 초 단위(float) → FCPXML 분수형 타임값 문자열.

    FCPXML에서 시간은 "N/Ds" 형식 (N: 정수 분자, D: 정수 분모).
    정확한 프레임 정렬을 위해 분모는 _TIMEBASE(24000)를 사용하고
    분자는 반올림하여 정수화한다.

    예: 1.5초 → "36000/24000s" → FCP에서 1.5초로 해석됨.
    0초는 "0s"로 단순화한다.
    """
    if seconds == 0.0:
        return "0s"
    numerator = round(seconds * _TIMEBASE)
    # 기약분수 처리
    g = math.gcd(numerator, _TIMEBASE)
    n = numerator // g
    d = _TIMEBASE // g
    if d == 1:
        return f"{n}s"
    return f"{n}/{d}s"


def _asset_id(idx: int) -> str:
    return f"r{idx + 1}"


def _format_id() -> str:
    return "r0"


def _abs_path(rel_path: str) -> str:
    """프로젝트 루트 기준 상대 경로 → file:// 절대 URL."""
    abs_p = Path(PROJECT_ROOT) / rel_path
    return abs_p.resolve().as_uri()


def _footage_src_uri(source_file: str) -> str:
    """clip_selection.json의 source_file → footage/ 폴더 기준 file:// URI.

    source_file 형태별 처리:
    - 파일명만 있을 때 (예: final_video_ch2.mp4)
      → footage/{filename} 경로로 변환
    - footage/ 또는 cache/ken_burns/ 등 상대경로일 때
      → PROJECT_ROOT 기준으로 변환
    - 절대경로일 때
      → footage/ 폴더 내 동일한 파일명 파일이 있으면 그것을 우선 사용,
         없으면 절대경로 그대로 file:// URI 변환
    """
    p = Path(source_file)

    # 절대경로인 경우
    if p.is_absolute():
        footage_candidate = Path(PROJECT_ROOT) / "footage" / p.name
        if footage_candidate.exists():
            return footage_candidate.resolve().as_uri()
        return p.resolve().as_uri()

    # 상대경로인 경우 — footage/ 또는 cache/ 등 슬래시 포함 여부로 판단
    if "/" in source_file or "\\" in source_file:
        abs_p = Path(PROJECT_ROOT) / source_file
        if abs_p.exists():
            return abs_p.resolve().as_uri()
        # 경로가 실제로 없으면 footage/ 폴더에서 파일명으로 재시도
        footage_candidate = Path(PROJECT_ROOT) / "footage" / p.name
        if footage_candidate.exists():
            return footage_candidate.resolve().as_uri()
        return abs_p.resolve().as_uri()

    # 파일명만 있는 경우 → footage/ 폴더에서 찾기
    footage_candidate = Path(PROJECT_ROOT) / "footage" / source_file
    if footage_candidate.exists():
        return footage_candidate.resolve().as_uri()
    # footage/ 에 없으면 PROJECT_ROOT 기준 상대경로로 폴백
    return (Path(PROJECT_ROOT) / source_file).resolve().as_uri()


def run(force: bool = False) -> Optional[str]:
    """FCPXML 파일을 생성하고 출력 경로를 반환한다.

    이미 생성된 경우 force=False이면 재생성 건너뜀.
    """
    output_path = Path(PROJECT_ROOT) / "output" / "final_output.fcpxml"

    if output_path.exists() and not force:
        console.print(f"[dim]  FCPXML 이미 생성됨 → 건너뜀 (재생성: --export-fcp-force)[/dim]")
        return str(output_path)

    # ── 입력 데이터 로드 ─────────────────────────────────────────
    clip_sel_raw = load_json("cache/clip_selection.json")
    if not clip_sel_raw:
        raise RuntimeError("cache/clip_selection.json 이 없어요. Phase 3을 먼저 실행해주세요.")

    narr_raw = load_json("output/narration/narration_timeline.json")
    if not narr_raw:
        raise RuntimeError("output/narration/narration_timeline.json 이 없어요. Phase 4를 먼저 실행해주세요.")

    scene_plan_raw = load_json("cache/scene_plan.json")
    if not scene_plan_raw:
        raise RuntimeError("cache/scene_plan.json 이 없어요. Phase 1을 먼저 실행해주세요.")

    selections: list[dict] = clip_sel_raw.get("selections", [])
    narr_entries: list[dict] = narr_raw.get("entries", narr_raw) if isinstance(narr_raw, dict) else narr_raw
    scenes: list[dict] = scene_plan_raw if isinstance(scene_plan_raw, list) else scene_plan_raw.get("scenes", [])

    if not selections:
        raise RuntimeError("clip_selection.json 에 selections 가 비어 있어요.")

    # scene_id → 데이터 매핑
    narr_map: dict[int, dict] = {e["scene_id"]: e for e in narr_entries}
    scene_map: dict[int, dict] = {s["scene_id"]: s for s in scenes}

    # ── 타임라인 총 길이 계산 ────────────────────────────────────
    # timeline.json 이 있으면 우선 사용, 없으면 narration으로 추정
    timeline_raw = load_json("cache/timeline.json")
    if timeline_raw and "timestamps" in timeline_raw:
        ts_list = timeline_raw["timestamps"]
        total_duration = max(t["end_sec"] for t in ts_list)
    elif narr_entries:
        total_duration = max(e.get("end_sec", 0.0) for e in narr_entries)
    else:
        total_duration = sum(s.get("clip_duration_sec", 0.0) for s in selections)

    # ── XML 트리 구성 ────────────────────────────────────────────
    # FCPXML v1.11 루트
    root = ET.Element("fcpxml", version="1.11")

    # resources
    resources = ET.SubElement(root, "resources")

    # 공용 포맷: 1080×1920 (9:16 숏폼), 24fps
    fmt = ET.SubElement(resources, "format",
                        id=_format_id(),
                        name="FFVideoFormat1080x1920at2398",
                        frameDuration=f"1001/{_FPS * 1000}s",
                        width="1080",
                        height="1920",
                        colorSpace="1-1-1 (Rec. 709)")

    # 비디오 에셋: footage 원본 파일 (중복 제거)
    seen_sources: dict[str, int] = {}  # source_file → asset index
    asset_idx = 0

    for sel in selections:
        src = sel["source_file"]
        # source_file이 비어 있는 씬(title_gen 등 effect 전용)은 비디오 에셋 생성 불필요
        if not src:
            continue
        if src in seen_sources:
            continue
        seen_sources[src] = asset_idx

        # 이슈 #61: 원본 파일의 실제 전체 길이를 ffprobe로 읽어 duration에 사용
        # 이슈 #109: footage/ 폴더 기준으로 실제 파일 경로를 찾아 ffprobe 실행
        footage_uri = _footage_src_uri(src)
        # file:// URI → 로컬 경로로 변환하여 ffprobe 실행
        parsed = urlparse(footage_uri)
        abs_src = unquote(parsed.path)
        try:
            file_duration = get_duration(abs_src)
        except Exception:
            file_duration = sel.get("clip_duration_sec", 10.0)
        duration_str = secs_to_fcp(file_duration)

        ET.SubElement(resources, "asset",
                      id=_asset_id(asset_idx),
                      name=Path(src).stem,
                      uid=f"footage_{asset_idx:03d}",
                      start="0s",
                      duration=duration_str,
                      hasVideo="1",
                      hasAudio="1",
                      audioSources="1",
                      audioChannels="2",
                      src=footage_uri,
                      format=_format_id())
        asset_idx += 1

    # 오디오 에셋: narration WAV 파일
    narr_asset_start_idx = asset_idx
    narr_asset_map: dict[int, int] = {}  # scene_id → asset index

    for entry in narr_entries:
        sid = entry["scene_id"]
        audio_file = entry.get("audio_file", f"output/narration/narration_scene_{sid:03d}.wav")
        dur = entry.get("duration_sec", 1.0)

        narr_asset_map[sid] = asset_idx
        # 이슈 #62: 오디오 전용 asset은 format 속성 생략 (FCPXML 표준)
        ET.SubElement(resources, "asset",
                      id=_asset_id(asset_idx),
                      name=f"narration_scene_{sid:03d}",
                      uid=f"narration_{sid:03d}",
                      start="0s",
                      duration=secs_to_fcp(dur),
                      hasVideo="0",
                      hasAudio="1",
                      audioSources="1",
                      audioChannels="1",
                      src=_abs_path(audio_file))
        asset_idx += 1

    # ── library / event / project ────────────────────────────────
    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="video-agent 자동 편집")
    project = ET.SubElement(event, "project", name="final_output")

    # sequence: 전체 타임라인
    sequence = ET.SubElement(project, "sequence",
                              duration=secs_to_fcp(total_duration),
                              format=_format_id(),
                              tcStart="0s",
                              tcFormat="NDF",
                              audioLayout="stereo",
                              audioRate="48k")

    spine = ET.SubElement(sequence, "spine")

    # ── 씬별 클립 배치 ──────────────────────────────────────────
    for sel in selections:
        sid = sel["scene_id"]
        src = sel["source_file"]
        # source_file이 비어 있는 씬(title_gen 등 effect 전용)은 비디오 클립 배치 불필요
        # seen_sources에 빈 문자열 키가 없으므로 기본값 0이 반환되어 잘못된 asset ref가 생성됨
        if not src:
            continue
        trim_start = sel.get("trim_start", 0.0)
        trim_end = sel.get("trim_end", trim_start + sel.get("clip_duration_sec", 5.0))
        clip_dur = trim_end - trim_start

        # 타임라인상 시작 위치
        if timeline_raw and "timestamps" in timeline_raw:
            ts_entry = next((t for t in timeline_raw["timestamps"] if t["scene_id"] == sid), None)
            tl_start = ts_entry["start_sec"] if ts_entry else 0.0
        else:
            # 대안: narration start_sec 사용
            narr = narr_map.get(sid)
            tl_start = narr["start_sec"] if narr else 0.0

        asset_vidx = seen_sources.get(src, 0)

        # <clip> 요소
        scene_info = scene_map.get(sid, {})
        clip_name = scene_info.get("title", f"씬 {sid}")

        clip_elem = ET.SubElement(spine, "clip",
                                  name=clip_name,
                                  offset=secs_to_fcp(tl_start),
                                  duration=secs_to_fcp(clip_dur),
                                  start=secs_to_fcp(trim_start),
                                  tcFormat="NDF",
                                  format=_format_id())

        # 비디오 참조
        ET.SubElement(clip_elem, "video",
                      ref=_asset_id(asset_vidx),
                      offset="0s",
                      duration=secs_to_fcp(clip_dur),
                      start=secs_to_fcp(trim_start))

        # 이슈 #63: FCPXML 표준 adjust-transform 속성 방식으로 크롭 Transform 표현
        # 9:16 크롭 Transform (1920×1080 → 1080×1920 center crop)
        # 원본이 16:9(1920×1080)이고 타깃이 9:16(1080×1920)일 때
        # 세로 맞춤: scale = 1920/1080 ≈ 1.778 → 세로 꽉 채우기
        # position은 "x y" 형식, scale은 "sx sy" 형식으로 직접 표현
        ET.SubElement(clip_elem, "adjust-transform",
                      position="0 0",
                      scale="1 1.7778")

        # 씬 마커: 제목 + 설명
        desc = scene_info.get("description", "")
        marker = ET.SubElement(clip_elem, "marker",
                               start="0s",
                               duration=secs_to_fcp(clip_dur),
                               value=clip_name,
                               note=desc)

        # narration 오디오 클립 (lane="-1": 비디오 아래 오디오 레인)
        narr = narr_map.get(sid)
        if narr and sid in narr_asset_map:
            narr_dur = narr.get("duration_sec", clip_dur)
            narr_start_in_tl = narr.get("start_sec", tl_start)
            ET.SubElement(clip_elem, "audio",
                          ref=_asset_id(narr_asset_map[sid]),
                          lane="-1",
                          offset=secs_to_fcp(narr_start_in_tl - tl_start),
                          duration=secs_to_fcp(narr_dur),
                          start="0s",
                          role="dialogue")

    # ── 들여쓰기 후 파일 저장 ─────────────────────────────────────
    _indent(root)
    tree = ET.ElementTree(root)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(output_path),
               encoding="UTF-8",
               xml_declaration=True)

    # DTD 선언 삽입 (FCP가 DTD를 요구함)
    _prepend_doctype(str(output_path))

    return str(output_path)


def _indent(elem: ET.Element, level: int = 0) -> None:
    """XML 트리를 재귀적으로 들여쓰기 처리 (Python 3.9+ ET.indent 대체)."""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent(child, level + 1)
        # 마지막 자식 닫는 태그 들여쓰기
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent


def _prepend_doctype(filepath: str) -> None:
    """생성된 XML 파일 상단에 FCPXML DTD DOCTYPE 선언을 삽입한다.

    Final Cut Pro는 DOCTYPE 선언이 없으면 파일을 유효한 FCPXML로
    인식하지 못할 수 있다.
    """
    doctype = (
        '<!DOCTYPE fcpxml>\n'
    )
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # <?xml ...?> 선언 다음 줄에 DOCTYPE 삽입
    if "<!DOCTYPE" not in content:
        content = content.replace("?>", "?>\n" + doctype, 1)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)


def print_summary(output_path: str, project_root: str) -> None:
    """FCPXML 생성 완료 요약 출력."""
    abs_path = str(Path(output_path).resolve())
    size_kb = Path(output_path).stat().st_size // 1024 if Path(output_path).exists() else 0

    console.print("\n[bold green]✅ FCPXML 내보내기 완료![/bold green]")
    console.print(f"  파일: [dim]{abs_path}[/dim]  ({size_kb} KB)")
    console.print(f"  Final Cut Pro에서 파일을 열어 원본 소스를 재연결(Relink)하면 편집을 시작할 수 있어요.")
    fcp_uri = f"[link=cursor://file/{abs_path}]cursor://file/{abs_path}[/link]"
    console.print(f"  📄 에디터에서 열기 → {fcp_uri}")
