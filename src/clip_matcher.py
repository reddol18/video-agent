"""Phase 3 — 클립 매칭

ScenePlan × FootageIndex → 씬별 최적 클립 선정 (LLM-as-Ranker)
결과를 cache/clip_selection.json에 저장한다.
"""

import json
import os
import sys
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz
from rich.console import Console
from rich.table import Table

from .utils.file_cache import load_json, save_json
from .utils.gemini_client import find_keyword_timestamp, find_scene_in_video, generate_json, get_video_duration
from .clip_editor import DISSOLVE_SEC

console = Console()

SELECTION_PATH = "cache/clip_selection.json"
MIN_QUALITY = float(os.getenv("MIN_QUALITY_SCORE", "0.5"))
CANDIDATE_LIMIT = 20

MATCH_PROMPT_TEMPLATE = """다음 씬에 가장 어울리는 클립을 후보 목록에서 순서대로 3개 골라주세요.

[씬 정보]
- 제목: {title}
- 설명: {description}
- 분위기: {mood}
- 키워드: {keywords}
- 필요 길이: {duration_hint_sec}초

[후보 클립 목록]
각 클립의 '원본 파일명'은 장면 내용을 추론하는 중요한 힌트입니다.
파일명의 의미(촬영 장소, 상황, 피사체 등)와 씬 시나리오의 연관성을 종합적으로 고려하여 match_score를 산정해주세요.
{candidates}

아래 JSON 배열 형식으로만 반환하세요 (설명 없이 순수 JSON):
[
  {{
    "chunk_id": string,
    "match_score": float,  // 0.0~1.0 — 파일명 의미, 설명, 키워드, 분위기를 종합 고려
    "match_reason": string // 선택 근거 (한국어 1문장)
  }},
  ...
]"""

BATCH_MATCH_PROMPT_TEMPLATE = """다음 여러 씬에 대해 각각 가장 어울리는 클립을 후보 목록에서 순서대로 3개씩 골라주세요.

[씬 목록]
{scenes_block}

[후보 클립 목록 (전체 공유)]
각 클립의 '원본 파일명'은 장면 내용을 추론하는 중요한 힌트입니다.
파일명의 의미(촬영 장소, 상황, 피사체 등)와 씬 시나리오의 연관성을 종합적으로 고려하여 match_score를 산정해주세요.
{candidates}

아래 JSON 객체 형식으로만 반환하세요 (설명 없이 순수 JSON).
키는 씬 번호(정수를 문자열로), 값은 해당 씬의 랭킹 배열입니다.
예시 (씬 번호가 3, 7인 경우):
{{
  "3": [
    {{
      "chunk_id": "abc-001",
      "match_score": 0.9,
      "match_reason": "이유"
    }},
    ...
  ],
  "7": [...],
  ...
}}"""

BATCH_SIZE_MIN = 5
BATCH_SIZE_MAX = 10


def _filename_keyword_hit(filename: str, scene_keywords: list[str]) -> bool:
    """파일명(확장자 제외)에 씬 키워드가 하나라도 포함되어 있으면 True."""
    stem = Path(filename).stem.lower()
    return any(kw.lower() in stem for kw in scene_keywords if kw)


def _prefilter(
    chunks: list,
    scene: dict,
    used_ids: set,
    footage_order: dict | None = None,
    current_min_order: int = -1,
) -> list:
    """LLM 호출 전 후보 클립 사전 필터링.

    정렬 기준 (내림차순 우선순위):
    1. 씬 키워드가 footage 파일명에 포함된 경우 +10 가중치 (대폭 상향)
    2. ScenePlan keywords와 chunk keywords의 문자열 교집합 크기

    footage_order가 주어진 경우:
    - footage_orders.txt에 등록된 파일은 순서 인덱스 >= current_min_order 인 경우만 포함
    - footage_orders.txt에 없는 파일은 제약 없이 포함
    """
    def _order_ok(c: dict) -> bool:
        if footage_order is None:
            return True
        stem = unicodedata.normalize('NFC', Path(c["source_file"]).stem)
        name = Path(c["source_file"]).name
        order_idx = footage_order.get(stem) if footage_order.get(stem) is not None else footage_order.get(name)
        if order_idx is None:
            return True  # footage_orders.txt에 없는 파일 — 제약 없음
        return order_idx >= current_min_order

    candidates = [
        c for c in chunks
        if c["quality_score"] >= MIN_QUALITY
        and c["chunk_id"] not in used_ids
        and _order_ok(c)
    ]

    scene_kw_list = scene.get("keywords", [])
    scene_query = " ".join(scene_kw_list + [scene.get("title", ""), scene.get("description", "")])

    def sort_key(c):
        filename = Path(c["source_file"]).name
        # 파일명 키워드 히트: 가중치 10
        filename_bonus = 10 if _filename_keyword_hit(filename, scene_kw_list) else 0

        # 청크 키워드 + description을 씬 쿼리와 rapidfuzz로 비교 (0~10 범위로 정규화)
        chunk_text = " ".join(c.get("keywords", []) + [c.get("description", ""), c.get("mood", "")])
        fuzzy_score = fuzz.token_set_ratio(scene_query, chunk_text) / 10.0

        return filename_bonus + fuzzy_score

    candidates.sort(key=sort_key, reverse=True)
    return candidates[:CANDIDATE_LIMIT]


def _format_candidates(candidates: list) -> str:
    lines = []
    for c in candidates:
        filename = Path(c["source_file"]).name
        lines.append(
            f"- chunk_id: {c['chunk_id']}\n"
            f"  원본 파일명: {filename}\n"
            f"  설명: {c['description']}\n"
            f"  분위기: {c['mood']}\n"
            f"  키워드: {', '.join(c.get('keywords', []))}\n"
            f"  품질: {c['quality_score']}\n"
            f"  길이: {c['end_sec'] - c['start_sec']:.1f}초"
        )
    return "\n".join(lines)


def _format_scenes_block(scenes: list) -> str:
    """배치 프롬프트용 씬 목록 포맷."""
    blocks = []
    for s in scenes:
        blocks.append(
            f"[씬 {s['scene_id']}]\n"
            f"- 제목: {s['title']}\n"
            f"- 설명: {s['description']}\n"
            f"- 분위기: {s['mood']}\n"
            f"- 키워드: {', '.join(s.get('keywords', []))}\n"
            f"- 필요 길이: {s['duration_hint_sec']}초"
        )
    return "\n\n".join(blocks)


def _pick_clips_batch(
    scenes: list,
    chunks: list,
    used_primary: set,
    tts_dur_map: dict,
    footage_order: dict | None = None,
    current_min_order: int = -1,
) -> list[dict | None]:
    """씬 여러 개에 대해 _prefilter 점수 1위 클립을 선정 (LLM 호출 없음).

    반환값: scenes 순서에 맞춘 ClipSelection(또는 None) 리스트.
    """
    results: list[dict | None] = []
    local_min_order = current_min_order  # 배치 내에서도 순서 갱신
    for scene in scenes:
        sid = scene["scene_id"]
        tts_dur = float(tts_dur_map.get(sid) or scene["duration_hint_sec"])

        cands = _prefilter(chunks, scene, used_primary, footage_order, local_min_order)
        is_reused = False
        if not cands:
            cands = _prefilter(chunks, scene, set(), footage_order, local_min_order)
            is_reused = True

        if not cands:
            results.append(None)
            continue

        top = cands[0]
        chunk_id = top["chunk_id"]
        trim_start = top["start_sec"]
        trim_end = top["end_sec"]

        # 배치 내 순서 제약 갱신
        if footage_order is not None:
            src_stem = unicodedata.normalize('NFC', Path(top["source_file"]).stem)
            src_name = Path(top["source_file"]).name
            chosen_order = footage_order.get(src_stem)
            if chosen_order is None:
                chosen_order = footage_order.get(src_name)
            if chosen_order is not None and chosen_order > local_min_order:
                local_min_order = chosen_order

        results.append({
            "scene_id": sid,
            "chunk_id": chunk_id,
            "source_file": top["source_file"],
            "trim_start": trim_start,
            "trim_end": trim_end,
            "clip_duration_sec": round(trim_end - trim_start, 3),
            "tts_duration_sec": round(tts_dur, 3),
            "match_score": 0.0,
            "match_reason": "keyword+filename score",
            "is_reused": is_reused,
        })

    return results


def _pick_clip(
    scene: dict,
    chunks: list,
    used_primary: set,
    tts_dur: float,
    footage_order: dict | None = None,
    current_min_order: int = -1,
) -> dict | None:
    """씬 하나에 대해 최적 클립 선정.

    클립 길이 채택 기준:
    - 클립이 TTS 음성보다 길면 → 클립 전체 길이 사용 (영상이 나레이션보다 길어도 됨)
    - 클립이 TTS 음성보다 짧으면 → 클립 전체 길이 사용 (어쩔 수 없음)
    즉, 항상 클립 원본 길이를 그대로 채택한다.
    """
    candidates = _prefilter(chunks, scene, used_primary, footage_order, current_min_order)
    if not candidates:
        # 중복 허용 폴백
        candidates = _prefilter(chunks, scene, set(), footage_order, current_min_order)
        is_reused = True
    else:
        is_reused = False

    if not candidates:
        return None

    prompt = MATCH_PROMPT_TEMPLATE.format(
        title=scene["title"],
        description=scene["description"],
        mood=scene["mood"],
        keywords=", ".join(scene.get("keywords", [])),
        duration_hint_sec=scene["duration_hint_sec"],
        candidates=_format_candidates(candidates),
    )

    raw = generate_json(prompt)
    rankings = json.loads(raw)

    if not rankings:
        return None

    top = rankings[0]
    chunk_id = top["chunk_id"]

    # chunk_id로 원본 청크 찾기
    chunk_map = {c["chunk_id"]: c for c in chunks}
    chunk = chunk_map.get(chunk_id)
    if not chunk:
        return None

    # 클립 원본 길이를 그대로 채택 (TTS보다 길면 영상이 더 길어짐 — 의도된 동작)
    trim_start = chunk["start_sec"]
    trim_end = chunk["end_sec"]

    return {
        "scene_id": scene["scene_id"],
        "chunk_id": chunk_id,
        "source_file": chunk["source_file"],
        "trim_start": trim_start,
        "trim_end": trim_end,
        "clip_duration_sec": round(trim_end - trim_start, 3),
        "tts_duration_sec": round(tts_dur, 3),
        "match_score": float(top.get("match_score", 0.0)),
        "match_reason": top.get("match_reason", ""),
        "is_reused": is_reused,
    }


def run(force: bool = False) -> list:
    """씬별 클립 매칭 실행. ClipSelection 배열 반환."""
    scenes_data = load_json("cache/scene_plan.json")
    if not scenes_data:
        raise RuntimeError("cache/scene_plan.json 이 없어요. Phase 1을 먼저 실행해주세요.")
    scenes = scenes_data["scenes"]

    # TTS 음성 길이를 씬별로 미리 로드 (Phase 4 먼저 실행됨)
    narr_data = load_json("output/narration/narration_timeline.json")
    if not narr_data:
        raise RuntimeError("output/narration/narration_timeline.json 이 없어요. Phase 4-A를 먼저 실행해주세요.")
    tts_dur_map = {e["scene_id"]: e["duration_sec"] for e in narr_data["entries"]}

    index_data = load_json("cache/footage_index.json")
    if not index_data:
        raise RuntimeError("cache/footage_index.json 이 없어요. Phase 2를 먼저 실행해주세요.")
    chunks = index_data["chunks"]

    if not force:
        cached = load_json(SELECTION_PATH)
        if cached:
            console.print("[dim]  Phase 3: 클립 매칭 캐시 사용[/dim]")
            return cached["selections"]

    # footage_orders.txt 로드 (존재하면 footage 순서 제약 적용)
    footage_order: dict | None = None
    footage_orders_path = Path("footage_orders.txt")
    if footage_orders_path.exists():
        footage_order = {}
        with footage_orders_path.open(encoding="utf-8") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if line:
                    # stem(확장자 제외)과 전체 파일명 모두 등록 (NFC 정규화: macOS NFD 파일명과 불일치 방지)
                    footage_order[unicodedata.normalize('NFC', Path(line).stem)] = idx
                    footage_order[Path(line).name] = idx
        console.print(f"  [dim]footage_orders.txt 로드 완료 — {len(footage_order) // 2}개 파일 순서 제약 적용[/dim]")

    console.print(f"\n[bold cyan]Phase 3[/bold cyan] 씬별 클립 매칭 중... ({len(scenes)}개 씬)")

    selections = []
    used_primary: set = set()

    for scene in scenes:
        sid = scene["scene_id"]
        tts_dur = float(tts_dur_map.get(sid) or scene["duration_hint_sec"])

        # scene.footage 필드로 해당 파일 청크만 필터링
        footage_field = scene.get("footage")
        if not footage_field:
            console.print(f"  [red]씬 {sid}: 'footage' 필드가 없어요. scene_plan.json을 확인해주세요.[/red]")
            continue

        # kenburns 지원: footage가 {"type": "kenburns", "image": "..."} dict이면 영상 검색 건너뜀
        if isinstance(footage_field, dict) and footage_field.get("type") == "kenburns":
            image_file = footage_field.get("image", "")
            console.print(f"  씬 {sid} — [bold green][kenburns][/bold green] {image_file}")
            selections.append({
                "scene_id": sid,
                "chunk_id": f"kenburns_{sid}",
                "source_file": image_file,
                "trim_start": 0.0,
                "trim_end": round(tts_dur, 3),
                "clip_duration_sec": round(tts_dur, 3),
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": 1.0,
                "match_reason": "kenburns 이미지 직접 지정",
                "is_reused": False,
                "effect": "kenburns",
                "kenburns_image": image_file,
            })
            continue

        # title_gen 지원: footage가 {"type": "title_gen", "options": {...}} dict이면 영상 검색 건너뜀
        if isinstance(footage_field, dict) and footage_field.get("type") == "title_gen":
            options = footage_field.get("options", {})
            text_preview = str(options.get("text", ""))[:20]
            console.print(f"  씬 {sid} — [bold blue][title_gen][/bold blue] {text_preview}")
            selections.append({
                "scene_id": sid,
                "chunk_id": f"title_gen_{sid}",
                "source_file": "",
                "trim_start": 0.0,
                "trim_end": round(tts_dur, 3),
                "clip_duration_sec": round(tts_dur, 3),
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": 1.0,
                "match_reason": "title_gen 효과 직접 지정",
                "is_reused": False,
                "effect": "title_gen",
                "title_gen_options": options,
            })
            continue

        # clip 지원: footage가 {"type": "clip", "file": "파일명"} dict이면 영상 검색 건너뜀
        if isinstance(footage_field, dict) and footage_field.get("type") == "clip":
            file_name = footage_field.get("file", "")
            console.print(f"  씬 {sid} — [bold cyan][clip][/bold cyan] {file_name}")
            selections.append({
                "scene_id": sid,
                "chunk_id": f"clip_{sid}",
                "source_file": file_name,
                "trim_start": 0.0,
                "trim_end": round(tts_dur, 3),
                "clip_duration_sec": round(tts_dur, 3),
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": 1.0,
                "match_reason": "clip 영상 직접 지정",
                "is_reused": False,
                "effect": "clip",
            })
            continue

        # kenburns_copy_img 지원: footage가 {"type": "kenburns_copy_img", "copy_img_options": {...}} dict이면 영상 검색 건너뜀
        if isinstance(footage_field, dict) and footage_field.get("type") == "kenburns_copy_img":
            copy_img_options = footage_field.get("copy_img_options", {})
            input_file = copy_img_options.get("input", "")
            console.print(f"  씬 {sid} — [bold yellow][kenburns+copy_img][/bold yellow] {input_file}")
            selections.append({
                "scene_id": sid,
                "chunk_id": f"kenburns_copy_img_{sid}",
                "source_file": input_file,
                "trim_start": 0.0,
                "trim_end": round(tts_dur, 3),
                "clip_duration_sec": round(tts_dur, 3),
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": 1.0,
                "match_reason": "kenburns+copy_img 중첩 효과 직접 지정",
                "is_reused": False,
                "effect": "kenburns_copy_img",
                "copy_img_options": copy_img_options,
            })
            continue

        # edit dict 방어 복원: Gemini 또는 캐시가 footage를 string으로 변환한 경우에도
        # scene_plan의 footage 원본 구조를 유지하도록 scene_plan을 직접 재참조한다.
        # (footage_field가 dict가 아닌 경우 scene_plan에서 footage를 다시 읽어 edit dict 복원 시도)
        if not isinstance(footage_field, dict) and footage_field:
            # scene_plan에서 footage 원본 읽기
            scene_plan_raw = load_json("cache/scene_plan.json")
            if scene_plan_raw:
                for sp_scene in scene_plan_raw.get("scenes", []):
                    if sp_scene.get("scene_id") == sid:
                        raw_footage = sp_scene.get("footage")
                        if isinstance(raw_footage, dict) and raw_footage.get("type") in (
                            "edit", "kenburns", "kenburns_copy_img", "clip", "title_gen", "sync", "multiscreen"
                        ):
                            footage_field = raw_footage
                            scene["footage"] = footage_field
                        break

        # edit 지원: footage가 {"type": "edit", "description": "...", "file": "파일명", "min_duration": N} dict이면
        # Gemini로 영상 내 해당 장면 구간 분석
        if isinstance(footage_field, dict) and footage_field.get("type") == "edit":
            edit_desc = footage_field.get("description", "")
            edit_file = footage_field.get("file", "")
            # min_duration: TTS 길이 + 양쪽 디졸브 구간 + 여유 마진
            # 디졸브는 앞뒤 씬과 각각 DISSOLVE_SEC만큼 겹치므로 2배 확보
            tts_based_min = tts_dur + DISSOLVE_SEC * 2 + 2.0
            scenario_min = float(footage_field.get("min_duration") or 0.0)
            min_duration = max(tts_based_min, scenario_min)

            # footage/ 디렉터리 기준으로 경로 resolve
            video_path = Path("footage") / edit_file
            if not video_path.exists():
                console.print(f"  [red]씬 {sid}: edit 영상 파일 '{edit_file}'을 footage/ 에서 찾을 수 없어요.[/red]")
                continue

            console.print(f"  씬 {sid} — [bold yellow][edit][/bold yellow] {edit_file} / \"{edit_desc[:20]}\"")

            try:
                # Gemini 호출 전에 ffprobe로 영상 길이를 먼저 측정
                total_duration = get_video_duration(str(video_path))

                # 최적화: min_duration >= 원본 전체 길이이면 AI 분석 없이 전체 사용
                if min_duration >= total_duration:
                    trim_start = 0.0
                    trim_end = total_duration
                    match_reason = f"min_duration({min_duration}s) >= 전체 길이({total_duration:.2f}s) — 전체 영상 사용"
                    console.print(f"    [dim]전체 영상 사용 (min_duration >= 전체 길이)[/dim]")
                else:
                    # min_duration < total_duration인 경우에만 Gemini 호출
                    result = find_scene_in_video(str(video_path), edit_desc, min_duration=min_duration)
                    trim_start = result["start_sec"]
                    trim_end = result["end_sec"]
                    match_reason = f"Gemini 분석: {edit_desc[:30]}"

                    # 구간이 min_duration보다 짧으면 앞뒤 확장 보정 (Gemini가 못 지킨 경우 대비)
                    actual_dur = trim_end - trim_start
                    if actual_dur < min_duration:
                        deficit = min_duration - actual_dur
                        extend_before = deficit / 2.0
                        extend_after = deficit / 2.0
                        trim_start = max(0.0, trim_start - extend_before)
                        trim_end = min(total_duration, trim_end + extend_after)
                        # 한쪽이 막혔으면 반대쪽으로 보충
                        if trim_start == 0.0:
                            trim_end = min(total_duration, min_duration)
                        if trim_end == total_duration:
                            trim_start = max(0.0, total_duration - min_duration)
                        console.print(
                            f"    [dim]구간 확장 보정: {trim_start:.2f}s ~ {trim_end:.2f}s "
                            f"(min_duration={min_duration}s)[/dim]"
                        )

            except Exception as e:
                console.print(f"  [yellow]씬 {sid}: 분석 실패 ({e}) — 전체 영상 사용[/yellow]")
                # 실패 시 get_video_duration으로 전체 길이를 재시도하거나 기본값 사용
                try:
                    total_duration = get_video_duration(str(video_path))
                except Exception:
                    total_duration = tts_dur
                trim_start = 0.0
                trim_end = total_duration
                match_reason = "분석 실패 — 전체 영상 사용"

            trim_start = round(trim_start, 3)
            trim_end = round(trim_end, 3)
            selections.append({
                "scene_id": sid,
                "chunk_id": f"edit_{sid}",
                "source_file": f"footage/{edit_file}",
                "trim_start": trim_start,
                "trim_end": trim_end,
                "clip_duration_sec": round(trim_end - trim_start, 3),
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": 1.0,
                "match_reason": match_reason,
                "is_reused": False,
                "effect": "edit",
            })
            continue

        # sync 지원: footage가 {"type": "sync", "word": "...", "inner": {...}} dict이면
        # inner가 edit이면 edit 처리 로직 재활용, inner 없으면 일반 footage 매칭
        # Gemini로 T_video(영상 내 키워드 발생 지점)를 획득하여 sync_video_sec 필드 추가
        if isinstance(footage_field, dict) and footage_field.get("type") == "sync":
            sync_word = footage_field.get("word", "")
            inner = footage_field.get("inner")
            console.print(f"  씬 {sid} — [bold green][sync:{sync_word}][/bold green]")

            # inner가 edit이면 edit 처리 로직으로 클립 구간 확정
            if inner and inner.get("type") == "edit":
                edit_desc = inner.get("description", "")
                edit_file = inner.get("file", "")
                tts_based_min = tts_dur + DISSOLVE_SEC * 2 + 2.0
                scenario_min = float(inner.get("min_duration") or 0.0)
                min_duration = max(tts_based_min, scenario_min)

                video_path = Path("footage") / edit_file
                if not video_path.exists():
                    console.print(f"  [red]씬 {sid}: sync+edit 영상 파일 '{edit_file}'을 footage/ 에서 찾을 수 없어요.[/red]")
                    continue

                try:
                    total_duration = get_video_duration(str(video_path))
                    if min_duration >= total_duration:
                        trim_start = 0.0
                        trim_end = total_duration
                        match_reason = f"sync+edit: min_duration >= 전체 길이 — 전체 영상 사용"
                    else:
                        result = find_scene_in_video(str(video_path), edit_desc, min_duration=min_duration)
                        trim_start = result["start_sec"]
                        trim_end = result["end_sec"]
                        actual_dur = trim_end - trim_start
                        if actual_dur < min_duration:
                            deficit = min_duration - actual_dur
                            trim_start = max(0.0, trim_start - deficit / 2.0)
                            trim_end = min(total_duration, trim_end + deficit / 2.0)
                            if trim_start == 0.0:
                                trim_end = min(total_duration, min_duration)
                            if trim_end == total_duration:
                                trim_start = max(0.0, total_duration - min_duration)
                        match_reason = f"sync+edit Gemini 분석: {edit_desc[:30]}"
                except Exception as e:
                    console.print(f"  [yellow]씬 {sid}: sync+edit 분석 실패 ({e}) — 전체 영상 사용[/yellow]")
                    try:
                        total_duration = get_video_duration(str(video_path))
                    except Exception:
                        total_duration = tts_dur
                    trim_start = 0.0
                    trim_end = total_duration
                    match_reason = "sync+edit 분석 실패 — 전체 영상 사용"

                trim_start = round(trim_start, 3)
                trim_end = round(trim_end, 3)
                source_file = f"footage/{edit_file}"

            else:
                # inner 없음 또는 지원하지 않는 inner 타입 → 일반 footage 매칭 시도
                # footage_index에서 단순히 첫 번째 청크 사용 (sync는 시간 보정이 주 목적)
                if chunks:
                    top = chunks[0]
                    trim_start = top["start_sec"]
                    trim_end = top["end_sec"]
                    source_file = top["source_file"]
                    match_reason = "sync: footage 첫 번째 청크 사용"
                else:
                    console.print(f"  [red]씬 {sid}: sync 일반 매칭용 청크가 없어요.[/red]")
                    continue
                trim_start = round(trim_start, 3)
                trim_end = round(trim_end, 3)

            # T_video 획득: Gemini로 영상 내 sync_word 등장 지점 탐색
            sync_video_sec: float | None = None
            try:
                video_full_path = Path(source_file)
                if not video_full_path.is_absolute() and not video_full_path.exists():
                    video_full_path = Path("footage") / Path(source_file).name
                if video_full_path.exists() and sync_word:
                    kw_result = find_keyword_timestamp(str(video_full_path), sync_word)
                    sync_video_sec = kw_result["timestamp_sec"]
                    console.print(
                        f"    [dim]sync T_video='{sync_word}' @ {sync_video_sec:.3f}s[/dim]"
                    )
            except Exception as e:
                console.print(f"    [yellow]sync T_video 탐색 실패 ({e}) — None 저장[/yellow]")

            sel_entry = {
                "scene_id": sid,
                "chunk_id": f"sync_{sid}",
                "source_file": source_file,
                "trim_start": trim_start,
                "trim_end": trim_end,
                "clip_duration_sec": round(trim_end - trim_start, 3),
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": 1.0,
                "match_reason": match_reason,
                "is_reused": False,
                "effect": "sync",
                "sync_word": sync_word,
            }
            if sync_video_sec is not None:
                sel_entry["sync_video_sec"] = round(sync_video_sec, 3)
            selections.append(sel_entry)
            continue

        # multiscreen dict 지원: footage가 {"type":"multiscreen","files":[...],"layout":"xxx"} 이면
        # files 목록을 footage_list로 변환하여 이하 동일 로직으로 처리
        if isinstance(footage_field, dict) and footage_field.get("type") == "multiscreen":
            footage_layout = footage_field.get("layout", "auto")
            footage_files = footage_field.get("files", [])
            # list 처리 경로로 바로 진입 — 이후 layout을 ClipSelection에 포함
            console.print(
                f"  씬 {sid} — [bold magenta][multiscreen:{footage_layout}][/bold magenta] "
                f"{len(footage_files)}개 영상"
            )
            multiscreen_clips = []
            per_file_used: set = set()
            for footage_name in footage_files:
                footage_name_nfc = unicodedata.normalize("NFC", footage_name)
                file_chunks = [
                    c for c in chunks
                    if unicodedata.normalize("NFC", Path(c["source_file"]).name) == footage_name_nfc
                ]
                if not file_chunks:
                    console.print(f"    [yellow]  '{footage_name}'에 해당하는 청크가 없어요 — 전체 영상 사용[/yellow]")
                    multiscreen_clips.append({
                        "source_file": str(footage_name),
                        "trim_start": 0.0,
                        "trim_end": tts_dur,
                        "clip_duration_sec": round(tts_dur, 3),
                        "tts_duration_sec": round(tts_dur, 3),
                        "match_score": 0.0,
                        "match_reason": "footage 청크 없음 — 전체 사용",
                    })
                    continue

                result = _pick_clips_batch(
                    [scene], file_chunks, per_file_used, tts_dur_map, footage_order, 0
                )[0]

                if result is None:
                    console.print(f"    [yellow]  '{footage_name}' 적합한 클립 없음 — 첫 번째 청크 사용[/yellow]")
                    fallback = file_chunks[0]
                    multiscreen_clips.append({
                        "source_file": fallback["source_file"],
                        "trim_start": fallback["start_sec"],
                        "trim_end": fallback["end_sec"],
                        "clip_duration_sec": round(fallback["end_sec"] - fallback["start_sec"], 3),
                        "tts_duration_sec": round(tts_dur, 3),
                        "match_score": 0.0,
                        "match_reason": "폴백: 첫 번째 청크",
                    })
                    continue

                per_file_used.add(result["chunk_id"])
                multiscreen_clips.append({
                    "source_file": result["source_file"],
                    "trim_start": result["trim_start"],
                    "trim_end": result["trim_end"],
                    "clip_duration_sec": result["clip_duration_sec"],
                    "tts_duration_sec": result["tts_duration_sec"],
                    "match_score": result["match_score"],
                    "match_reason": result["match_reason"],
                })

            if not multiscreen_clips:
                console.print(f"  [red]씬 {sid}: multiscreen 클립을 하나도 찾지 못했어요.[/red]")
                continue

            primary = multiscreen_clips[0]
            sel_entry = {
                "scene_id": sid,
                "chunk_id": f"multiscreen_{sid}",
                "source_file": primary["source_file"],
                "trim_start": primary["trim_start"],
                "trim_end": primary["trim_end"],
                "clip_duration_sec": primary["clip_duration_sec"],
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": primary["match_score"],
                "match_reason": primary["match_reason"],
                "is_reused": False,
                "effect": "multiscreen",
                "multiscreen_clips": multiscreen_clips,
                "layout": footage_layout,
            }
            selections.append(sel_entry)
            continue

        # multiscreen 지원: footage가 배열이면 각 파일에 대해 개별 클립 매칭
        if isinstance(footage_field, list):
            console.print(f"  씬 {sid} — [bold magenta][multiscreen][/bold magenta] {len(footage_field)}개 영상")
            multiscreen_clips = []
            per_file_used: set = set()
            for footage_name in footage_field:
                footage_name_nfc = unicodedata.normalize("NFC", footage_name)
                file_chunks = [
                    c for c in chunks
                    if unicodedata.normalize("NFC", Path(c["source_file"]).name) == footage_name_nfc
                ]
                if not file_chunks:
                    console.print(f"    [yellow]  '{footage_name}'에 해당하는 청크가 없어요 — 전체 영상 사용[/yellow]")
                    # footage_index에 청크가 없으면 footage 디렉터리에서 파일 경로 직접 사용
                    multiscreen_clips.append({
                        "source_file": str(footage_name),
                        "trim_start": 0.0,
                        "trim_end": tts_dur,
                        "clip_duration_sec": round(tts_dur, 3),
                        "tts_duration_sec": round(tts_dur, 3),
                        "match_score": 0.0,
                        "match_reason": "footage 청크 없음 — 전체 사용",
                    })
                    continue

                result = _pick_clips_batch(
                    [scene], file_chunks, per_file_used, tts_dur_map, footage_order, 0
                )[0]

                if result is None:
                    console.print(f"    [yellow]  '{footage_name}' 적합한 클립 없음 — 첫 번째 청크 사용[/yellow]")
                    fallback = file_chunks[0]
                    multiscreen_clips.append({
                        "source_file": fallback["source_file"],
                        "trim_start": fallback["start_sec"],
                        "trim_end": fallback["end_sec"],
                        "clip_duration_sec": round(fallback["end_sec"] - fallback["start_sec"], 3),
                        "tts_duration_sec": round(tts_dur, 3),
                        "match_score": 0.0,
                        "match_reason": "폴백: 첫 번째 청크",
                    })
                    continue

                per_file_used.add(result["chunk_id"])
                multiscreen_clips.append({
                    "source_file": result["source_file"],
                    "trim_start": result["trim_start"],
                    "trim_end": result["trim_end"],
                    "clip_duration_sec": result["clip_duration_sec"],
                    "tts_duration_sec": result["tts_duration_sec"],
                    "match_score": result["match_score"],
                    "match_reason": result["match_reason"],
                })

            if not multiscreen_clips:
                console.print(f"  [red]씬 {sid}: multiscreen 클립을 하나도 찾지 못했어요.[/red]")
                continue

            # 대표 선택 항목 (첫 번째 클립 기준) + multiscreen_clips 배열 포함
            primary = multiscreen_clips[0]
            selections.append({
                "scene_id": sid,
                "chunk_id": f"multiscreen_{sid}",
                "source_file": primary["source_file"],
                "trim_start": primary["trim_start"],
                "trim_end": primary["trim_end"],
                "clip_duration_sec": primary["clip_duration_sec"],
                "tts_duration_sec": round(tts_dur, 3),
                "match_score": primary["match_score"],
                "match_reason": primary["match_reason"],
                "is_reused": False,
                "effect": "multiscreen",
                "multiscreen_clips": multiscreen_clips,
            })
            continue

        # 단일 footage 처리 (기존 로직)
        footage_name = footage_field
        footage_name_nfc = unicodedata.normalize("NFC", footage_name)
        scene_chunks = [
            c for c in chunks
            if unicodedata.normalize("NFC", Path(c["source_file"]).name) == footage_name_nfc
        ]
        if not scene_chunks:
            console.print(f"  [red]씬 {sid}: footage '{footage_name}'에 해당하는 청크가 없어요.[/red]")
            continue

        console.print(f"  씬 {sid} — [dim]{footage_name}[/dim] ({len(scene_chunks)}개 청크 대상)")

        # used_primary 전달 → _prefilter가 이미 사용된 청크 제외 후 최고 점수 선택
        result = _pick_clips_batch(
            [scene], scene_chunks, used_primary, tts_dur_map, footage_order, 0
        )[0]

        if result is None:
            console.print(f"  [red]씬 {sid}: 적합한 클립을 찾지 못했어요.[/red]")
            continue

        used_primary.add(result["chunk_id"])

        selections.append(result)

    save_json(SELECTION_PATH, {"selections": selections})
    return selections


def print_summary(selections: list, project_root: str) -> None:
    """매칭 결과 요약 출력."""
    console.print()
    console.rule("[bold green]✅ Phase 3 완료 — 씬별 클립 매칭 결과[/bold green]")

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("씬", width=4)
    table.add_column("파일", width=20)
    table.add_column("구간", width=14)
    table.add_column("일치도", width=8)
    table.add_column("비고", width=20)

    for s in selections:
        score = s["match_score"]
        effect = s.get("effect", "")
        if effect == "multiscreen":
            clips = s.get("multiscreen_clips", [])
            src = f"[multiscreen x{len(clips)}]"
            span = f"{s['tts_duration_sec']:.1f}s (동기화)"
        elif effect == "title_gen":
            text_preview = str(s.get("title_gen_options", {}).get("text", ""))[:15]
            src = f"[title_gen] {text_preview}"
            span = f"{s['tts_duration_sec']:.1f}s"
        elif effect == "kenburns_copy_img":
            input_file = s.get("copy_img_options", {}).get("input", "")
            src = f"[kb+cimg] {Path(input_file).name}"
            span = f"{s['tts_duration_sec']:.1f}s"
        elif effect == "clip":
            src = f"[clip] {Path(s.get('source_file', '')).name}"
            span = f"{s['tts_duration_sec']:.1f}s"
        elif effect == "edit":
            src = f"[edit] {Path(s.get('source_file', '')).name}"
            span = f"{s['trim_start']:.1f}s ~ {s['trim_end']:.1f}s"
        elif effect == "sync":
            sync_word = s.get("sync_word", "")
            sync_video_sec = s.get("sync_video_sec")
            src = f"[sync:{sync_word}] {Path(s.get('source_file', '')).name}"
            if sync_video_sec is not None:
                span = f"{s['trim_start']:.1f}s~{s['trim_end']:.1f}s T_v={sync_video_sec:.2f}s"
            else:
                span = f"{s['trim_start']:.1f}s ~ {s['trim_end']:.1f}s"
        else:
            src = Path(s["source_file"]).name if s.get("source_file") else ""
            span = f"{s['trim_start']:.1f}s ~ {s['trim_end']:.1f}s"
        score_str = f"{score * 100:.0f}%"
        warn = ""
        if s.get("is_reused"):
            warn = "♻️ 재사용"
        elif score < 0.7 and effect not in ("multiscreen", "title_gen", "kenburns_copy_img", "clip", "edit"):
            warn = "⚠️ 확인 필요"

        style = "yellow" if score < 0.7 and effect not in ("multiscreen", "title_gen", "kenburns_copy_img", "clip", "edit", "sync") else "green"
        table.add_row(str(s["scene_id"]), src, span, f"[{style}]{score_str}[/{style}]", warn)

    console.print(table)

    low = [s for s in selections if s["match_score"] < 0.7 and s.get("effect") not in ("multiscreen", "title_gen", "kenburns_copy_img", "clip", "edit", "sync")]
    if low:
        console.print(f"\n  [yellow]⚠️  일치도가 낮은 씬이 {len(low)}개 있어요. 확인 후 교체를 고려해보세요.[/yellow]")
        console.print("  편집 모드에서 'preview <씬번호>' 를 입력하면 해당 클립을 바로 미리볼 수 있어요.")

    sel_abs = str(Path(project_root) / SELECTION_PATH)
    console.print(f"\n  📄 전체 내용 보기 → [link=cursor://file/{sel_abs}]cursor://file/{sel_abs}[/link]")


if __name__ == "__main__":
    project_root = str(Path(__file__).parent.parent)
    result = run()
    print_summary(result, project_root)
