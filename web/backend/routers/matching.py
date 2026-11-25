from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks

_ROOT = Path(__file__).parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

from ..models import (
    MatchRequest, ManualMatchRequest, SceneCard, ClipEffect, MatchMeta,
    SaveKeywordMatchRequest,
)
from .. import state as store
from ..ws.progress import create_task_id, push_sync, register_bg_task

router = APIRouter(prefix="/api/projects", tags=["matching"])


# ── #389 — match.trim_start/trim_end → footage.clip_*_sec 자동 적용 ────────
# 매칭 결과는 MatchMeta(trim_start/trim_end) 에만 저장되어 있었고 footage.clip_*_sec
# 는 null 인 채로 남아 있었다. 그 결과 인스펙터/TimelineBar/렌더링이 footage 원본
# 길이를 사용해 사용자가 인식하는 "매칭 길이" 와 실제 영상 길이가 어긋난다.
#
# 정책:
#   1. footage 가 clip 이고 clip_start_sec/clip_end_sec 가 모두 null 이며
#      match.trim_end > match.trim_start 이면 trim 값으로 자동 채움.
#   2. 사용자가 명시적으로 clip_*_sec 를 설정한 씬은 건드리지 않는다 (둘 중 하나라도
#      값이 있으면 신뢰).
#   3. match.source_file basename 과 footage.file basename 이 일치하지 않으면 적용
#      하지 않음 — 오래된/잘못된 match 데이터로 인한 회귀 방지 (matchTrustworthy 가드).

def _apply_match_trim_to_footage(footage, match) -> bool:
    """match.trim_start/trim_end 를 footage.clip_*_sec 에 채워준다.

    Returns:
        True 면 footage 가 변경됨, False 면 변경 사항 없음.
    """
    if footage is None or match is None:
        return False
    # clip 타입만 적용 (이미지/sync/multiscreen 등은 trim 개념이 다름).
    if getattr(footage, "type", None) != "clip":
        return False
    trim_start = getattr(match, "trim_start", None) or 0.0
    trim_end = getattr(match, "trim_end", None) or 0.0
    if not (trim_end > trim_start):
        return False
    # 이미 사용자가 설정해둔 값이 있으면 보존.
    if footage.clip_start_sec is not None or footage.clip_end_sec is not None:
        return False
    # match.source_file basename vs footage.file basename 일치 검사 — 회귀 안전.
    match_basename = Path(str(getattr(match, "source_file", "") or "")).name
    footage_basename = Path(str(getattr(footage, "file", "") or "")).name
    if match_basename and footage_basename and match_basename != footage_basename:
        return False
    footage.clip_start_sec = float(trim_start)
    footage.clip_end_sec = float(trim_end)
    return True


@router.post("/{project_id}/match")
async def match_footage(project_id: str, req: MatchRequest, background_tasks: BackgroundTasks):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    if not p.scenes:
        raise HTTPException(400, "먼저 시나리오를 파싱해주세요")

    # #355 후속 — Gemini 매칭일 때 사용할 모델 결정.
    # 우선순위: 요청 본문 model > 프로젝트 gemini_model > None(라이브러리 폴백)
    model = req.model or (getattr(p, "gemini_model", None) or None)

    task_id = create_task_id()

    async def _run():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_match,
                                   project_id, req.method, req.scene_ids, task_id, model)

    task = asyncio.create_task(_run())
    register_bg_task(task_id, task)
    return {"task_id": task_id}


def _do_match(
    project_id: str,
    method: str,
    scene_ids: list[int] | None,
    task_id: str,
    model: str | None = None,
):
    p = store.get_project(project_id)
    if not p:
        push_sync(task_id, {"type": "error", "task_id": task_id,
                            "code": "NOT_FOUND", "message": "프로젝트 없음"})
        return

    target_scenes = [s for s in p.scenes if scene_ids is None or s.scene_id in scene_ids]
    total = len(target_scenes)

    push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "matching",
                        "current": 0, "total": total, "message": f"매칭 시작 ({method})", "item": ""})

    cache_dir = store.get_cache_dir(project_id)
    index_path = cache_dir / "footage_index.json"

    if method in ("gemini_filename", "gemini_content", "keyword_filename"):
        if not index_path.exists():
            push_sync(task_id, {"type": "error", "task_id": task_id,
                                "code": "NO_INDEX", "message": "footage 인덱스가 없습니다. 먼저 footage를 인덱싱하세요."})
            return

        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        chunks = index_data.get("chunks", [])

    # studio_assets 는 label 매칭뿐 아니라 Gemini 매칭에서도 활용한다.
    # 카메라 자동생성 파일명(VID_20260531_090622.mp4)에는 의미 정보가 없으므로,
    # footage 스튜디오에서 사용자가 붙인 한국어 레이블이 매칭 품질을 좌우한다.
    # 레이블이 빈 자산만 있거나 studio_assets 자체가 비어 있으면 두 Gemini 함수는
    # 자동으로 기존 파일명 기반 동작으로 폴백한다 (회귀 안전).
    studio_assets = p.studio_assets if method in ("label", "gemini_filename", "gemini_content") else []

    success = 0
    failed = 0

    for i, scene in enumerate(target_scenes):
        try:
            if method == "keyword_filename":
                result = _match_keyword(scene, p.footage_dir, chunks)
            elif method == "gemini_filename":
                result = _match_gemini_filename(scene, p.footage_dir, chunks, studio_assets, model=model)
            elif method == "gemini_content":
                result = _match_gemini_content(scene, p.footage_dir, chunks, studio_assets, model=model)
            elif method == "label":
                result = _match_label(scene, p.footage_dir, studio_assets)
            else:
                continue

            if result:
                # #389 — 매칭 직후 자동으로 trim 을 footage.clip_*_sec 에 채움.
                _apply_match_trim_to_footage(result["footage"], result["match"])
                matched = store.update_scene(project_id, scene.scene_id,
                                             footage=result["footage"],
                                             match=result["match"])
                push_sync(task_id, {"type": "match_done", "task_id": task_id,
                                    "scene_id": scene.scene_id,
                                    "matched_scene": matched.model_dump()})
                success += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            push_sync(task_id, {"type": "error", "task_id": task_id,
                                "code": "MATCH_FAILED", "message": str(e),
                                "scene_id": scene.scene_id})

        push_sync(task_id, {"type": "progress", "task_id": task_id, "phase": "matching",
                            "current": i + 1, "total": total,
                            "message": f"씬 {scene.scene_id} 매칭 완료", "item": scene.title})

    # #301 — 모의 비용 기록: Phase 3 매칭 (Gemini 기반일 때만 의미 있음)
    # #355 후속 — 선택한 model 을 record_matching 에 그대로 전파해야 by_model
    # 분해가 모델별로 누적된다. method 별 평균 글자수도 분리 — content 가 약 4배.
    if method in ("gemini_filename", "gemini_content"):
        try:
            from ..cost_tracker import measure_phase
            with measure_phase(project_id) as _est:
                # 매칭당 평균 글자수 — 실제 프롬프트 구성과 일치시킨다.
                # gemini_filename: 후보 5개 (레이블 ~25자 + 파일명) + 씬 설명·키워드 ≈ 350자, output ≈ 30자
                #   (레이블 길이가 파일명 stem 과 비슷해 평균 토큰 증가는 무시할 수준)
                # gemini_content : 후보 10개 청크 desc/keywords + 레이블 + 씬 설명 ≈ 1700자, output ≈ 30자
                #   (청크당 레이블 ~30자 × 10 = +300자 가산)
                if method == "gemini_filename":
                    per_scene_input, per_scene_output = 350, 30
                else:
                    per_scene_input, per_scene_output = 1700, 30
                _est.record_matching(
                    input_chars=per_scene_input * total,
                    output_chars=per_scene_output * total,
                    cpu_seconds=0,
                    model=model,
                )
        except Exception as _cost_err:
            print(f"[cost] matching 기록 실패: {_cost_err}")

    push_sync(task_id, {"type": "complete", "task_id": task_id,
                        "summary": {"total": total, "success": success, "failed": failed}})


def _match_keyword(scene: SceneCard, footage_dir: str, chunks: list) -> dict | None:
    """비AI: 파일명 토큰 vs 씬 설명/키워드 rapidfuzz 매칭."""
    from rapidfuzz import fuzz

    text = f"{scene.title} {scene.description} {' '.join(scene.keywords)}"

    best_score = -1.0
    best_chunk = None

    seen_files: set[str] = set()
    for chunk in chunks:
        fn = Path(chunk.get("source_file", "")).stem
        if fn in seen_files:
            continue
        seen_files.add(fn)
        score = fuzz.token_set_ratio(text, fn.replace("_", " ").replace("-", " "))
        if score > best_score:
            best_score = score
            best_chunk = chunk

    if best_chunk is None:
        # footage_dir 파일명 직접 스캔 (인덱스 없는 파일 포함)
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        for f in Path(footage_dir).iterdir():
            if f.suffix.lower() not in video_exts | image_exts:
                continue
            score = fuzz.token_set_ratio(text, f.stem.replace("_", " "))
            if score > best_score:
                best_score = score
                best_chunk = {"source_file": str(f), "chunk_id": f.stem,
                              "start_sec": 0.0, "end_sec": 0.0}

    if not best_chunk:
        return None

    src_file = best_chunk.get("source_file", "")
    ext = Path(src_file).suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    if ext in image_exts:
        from ..models import KenburnsEffect
        footage = KenburnsEffect(image=Path(src_file).name)
    else:
        footage = ClipEffect(file=Path(src_file).name)

    match = MatchMeta(
        chunk_id=best_chunk.get("chunk_id", ""),
        source_file=src_file,
        trim_start=best_chunk.get("start_sec", 0.0),
        trim_end=best_chunk.get("end_sec", 0.0),
        match_score=best_score,
        match_reason="keyword_filename",
    )
    return {"footage": footage, "match": match}


def _match_gemini_filename(
    scene: SceneCard,
    footage_dir: str,
    chunks: list,
    studio_assets: list | None = None,
    model: str | None = None,
) -> dict | None:
    """Gemini · 레이블 매칭: footage 스튜디오에서 사용자가 단 레이블을 우선 활용한다.

    카메라 자동생성 파일명(VID_20260531_090622.mp4)은 의미가 없어 파일명만으로는
    매칭이 사실상 불가능하다는 사용자 피드백을 반영해, studio_assets.label 을
    사전 필터·Gemini 프롬프트 양쪽에 반영한다.

    동작:
      1. 사전 필터 — 레이블이 있는 청크는 `token_set_ratio(씬, 레이블)`,
         레이블이 없는 청크는 기존 파일명 기반 점수로 폴백.
      2. 같은 source_file 안에서는 최고점 청크 1개만 보존 (기존과 동일).
      3. 상위 5개를 Gemini 에 전달. 프롬프트에는 레이블이 있으면 레이블을, 없으면
         "(레이블 없음 — 파일명: ...)" 표시. 응답 스키마는 `{"chosen":"<파일명>"}` 유지.
      4. studio_assets 가 비어 있거나 모든 청크에 레이블이 없으면 기존 파일명 기반
         동작과 동일하게 회귀 안전 폴백.
    """
    from rapidfuzz import fuzz

    studio_assets = studio_assets or []
    # 파일명 → 레이블 매핑. 빈 레이블은 제외해 폴백 분기를 정확히 타게 한다.
    label_by_filename: dict[str, str] = {
        a.filename: a.label for a in studio_assets if getattr(a, "label", "")
    }

    text = f"{scene.title} {scene.description} {' '.join(scene.keywords)}"

    # 파일별 대표 점수 계산 — 레이블 우선, 없으면 파일명 폴백.
    file_scores: dict[str, tuple[float, dict, str]] = {}
    labeled_count = 0
    unlabeled_count = 0
    for chunk in chunks:
        src = chunk.get("source_file", "")
        if not src:
            continue
        fname = Path(src).name
        label = label_by_filename.get(fname, "")
        if label:
            score = fuzz.token_set_ratio(text, label)
            labeled_count += 1
        else:
            fn_stem = Path(src).stem
            score = fuzz.token_set_ratio(text, fn_stem.replace("_", " ").replace("-", " "))
            unlabeled_count += 1
        # 같은 source_file 안에서는 최고점 청크만 보존.
        if src not in file_scores or score > file_scores[src][0]:
            file_scores[src] = (score, chunk, label)

    print(
        f"[match:gemini_filename] scene={scene.scene_id} "
        f"레이블있는청크={labeled_count} 없는청크={unlabeled_count}"
    )

    if not file_scores:
        return None

    # 상위 5개만 Gemini 에게.
    top5 = sorted(file_scores.items(), key=lambda x: -x[1][0])[:5]

    from src.utils import gemini_client as _gc

    candidate_lines = []
    for i, (src, (_score, _chunk, label)) in enumerate(top5, start=1):
        fname = Path(src).name
        if label:
            candidate_lines.append(f"[{i}] 레이블: {label} / 파일: {fname}")
        else:
            candidate_lines.append(f"[{i}] (레이블 없음 — 파일: {fname})")
    candidate_block = "\n".join(candidate_lines)

    prompt = (
        f"다음 씬에 가장 어울리는 후보를 하나만 골라주세요. 레이블은 촬영자가 직접 단 한국어 메모입니다.\n"
        f"씬 제목: {scene.title}\n"
        f"씬 설명: {scene.description}\n"
        f"키워드: {', '.join(scene.keywords)}\n\n"
        f"후보:\n{candidate_block}\n\n"
        f"선택한 후보의 파일명을 chosen 에 넣어 JSON 으로만 반환하세요 "
        f"(설명·코드펜스 없이 순수 JSON):\n"
        f'{{"chosen": "<선택한 후보의 파일명>"}}'
    )
    try:
        raw = _gc.generate_json(prompt, model=model)
        data = json.loads(raw)
        answer = str(data.get("chosen", "")).strip()
        chosen = next((src for src, _ in top5 if Path(src).name == answer), top5[0][0])
    except Exception as e:
        # generate_json 호출 실패 또는 JSON 파싱 실패 → fuzz top1 로 안전 폴백.
        print(f"[matching] gemini_filename 폴백(top1): {e}")
        chosen = top5[0][0]

    best_chunk = file_scores[chosen][1]
    ext = Path(chosen).suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if ext in image_exts:
        from ..models import KenburnsEffect
        footage = KenburnsEffect(image=Path(chosen).name)
    else:
        footage = ClipEffect(file=Path(chosen).name)

    match = MatchMeta(
        chunk_id=best_chunk.get("chunk_id", ""),
        source_file=chosen,
        trim_start=best_chunk.get("start_sec", 0.0),
        trim_end=best_chunk.get("end_sec", 0.0),
        match_score=file_scores[chosen][0],
        match_reason="gemini_filename",
    )
    return {"footage": footage, "match": match}


def _match_gemini_content(
    scene: SceneCard,
    footage_dir: str,
    chunks: list,
    studio_assets: list | None = None,
    model: str | None = None,
) -> dict | None:
    """Gemini · 콘텐츠 매칭: studio_assets 레이블 + chunk 메타(description/keywords) 종합.

    레이블이 있는 청크는 사전 필터에서 `레이블 0.5 + description 0.5` 가중을 적용한다.
    레이블이 없으면 description 1.0 (기존 동작에 가깝게 폴백). Gemini 프롬프트에도
    `레이블=...` 줄을 함께 표시해, 파일명만으로는 잡히지 않던 사용자 의도가 반영되게 한다.

    studio_assets 가 비어 있으면 모든 청크가 무레이블로 간주되어 description 1.0
    가중으로 동작 — 기존 0.3·0.7 보다 description 비중이 더 커진 변화는 있지만,
    파일명에 의미가 없는 카메라 자동 명명 환경에서는 무해하고 오히려 합리적이다.
    """
    from src.utils import gemini_client as _gc
    from rapidfuzz import fuzz

    studio_assets = studio_assets or []
    label_by_filename: dict[str, str] = {
        a.filename: a.label for a in studio_assets if getattr(a, "label", "")
    }

    text = f"{scene.title} {scene.description} {' '.join(scene.keywords)}"

    # 레이블·description 양쪽 점수를 가중 결합해 상위 10개 청크 추림.
    scored: list[tuple[float, dict, str]] = []
    labeled_count = 0
    unlabeled_count = 0
    for chunk in chunks:
        src = chunk.get("source_file", "")
        fname = Path(src).name if src else ""
        label = label_by_filename.get(fname, "")
        desc_score = fuzz.token_set_ratio(text, chunk.get("description", ""))
        if label:
            label_score = fuzz.token_set_ratio(text, label)
            combined = label_score * 0.5 + desc_score * 0.5
            labeled_count += 1
        else:
            combined = desc_score
            unlabeled_count += 1
        scored.append((combined, chunk, label))

    print(
        f"[match:gemini_content] scene={scene.scene_id} "
        f"레이블있는청크={labeled_count} 없는청크={unlabeled_count}"
    )

    scored.sort(key=lambda x: -x[0])
    candidates = scored[:10]

    if not candidates:
        return None

    candidate_text = "\n".join(
        (
            f"- chunk_id={c['chunk_id']} file={Path(c.get('source_file','')).name} "
            f"레이블={label or '(없음)'} desc={c.get('description','')} "
            f"keywords={c.get('keywords','')}"
        )
        for _, c, label in candidates
    )

    prompt = (
        f"다음 씬에 가장 어울리는 클립을 하나 골라주세요. "
        f"레이블은 촬영자가 직접 단 한국어 메모입니다.\n"
        f"씬 제목: {scene.title}\n"
        f"씬 설명: {scene.description}\n"
        f"키워드: {', '.join(scene.keywords)}\n\n"
        f"후보 클립:\n{candidate_text}\n\n"
        f"JSON 형식으로만 반환하세요 (설명·코드펜스 없이 순수 JSON):\n"
        f'{{"chunk_id": "<위 후보 중 하나의 chunk_id>"}}'
    )
    try:
        raw = _gc.generate_json(prompt, model=model)
        data = json.loads(raw)
        answer = str(data.get("chunk_id", "")).strip()
        chosen_chunk = next(
            (c for _, c, _label in candidates if c.get("chunk_id") == answer),
            candidates[0][1],
        )
    except Exception as e:
        print(f"[matching] gemini_content 폴백(top1): {e}")
        chosen_chunk = candidates[0][1]

    src_file = chosen_chunk.get("source_file", "")
    ext = Path(src_file).suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if ext in image_exts:
        from ..models import KenburnsEffect
        footage = KenburnsEffect(image=Path(src_file).name)
    else:
        footage = ClipEffect(file=Path(src_file).name)

    match = MatchMeta(
        chunk_id=chosen_chunk.get("chunk_id", ""),
        source_file=src_file,
        trim_start=chosen_chunk.get("start_sec", 0.0),
        trim_end=chosen_chunk.get("end_sec", 0.0),
        match_score=candidates[0][0],
        match_reason="gemini_content",
    )
    return {"footage": footage, "match": match}


# ── Label match (studio_assets 기반) ──────────────────────────────────────

def _match_label(scene: SceneCard, footage_dir: str, studio_assets: list) -> dict | None:
    """studio_assets의 label과 씬 title/description/keywords를 rapidfuzz로 유사도 비교."""
    if not studio_assets:
        return None

    from rapidfuzz import fuzz
    from ..models import FootageAsset

    text = f"{scene.title} {scene.description} {' '.join(scene.keywords)}"

    best_score = -1.0
    best_asset: FootageAsset | None = None

    for asset in studio_assets:
        if not asset.label:
            continue
        score = fuzz.token_set_ratio(text, asset.label)
        if score > best_score:
            best_score = score
            best_asset = asset

    if best_asset is None:
        return None

    src_file = Path(footage_dir) / best_asset.filename
    ext = src_file.suffix.lower()

    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        from ..models import KenburnsEffect
        footage = KenburnsEffect(image=best_asset.filename)
    else:
        footage = ClipEffect(file=best_asset.filename)

    match = MatchMeta(
        chunk_id=f"{best_asset.asset_id}_label",
        source_file=str(src_file),
        trim_start=0.0,
        trim_end=0.0,
        match_score=best_score,
        match_reason="label",
    )
    return {"footage": footage, "match": match}


# ── Manual match ───────────────────────────────────────────────────────────

@router.post("/{project_id}/scenes/{scene_id}/match-manual")
def manual_match(project_id: str, scene_id: int, req: ManualMatchRequest):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)

    footage_path = Path(p.footage_dir) / req.filename
    if not footage_path.exists():
        raise HTTPException(400, f"{req.filename} 파일을 footage 폴더에서 찾을 수 없습니다")

    ext = footage_path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    if ext in image_exts:
        from ..models import KenburnsEffect
        footage = KenburnsEffect(image=req.filename)
    else:
        footage = ClipEffect(file=req.filename)

    import subprocess
    trim_end = req.trim_end
    if trim_end is None:
        try:
            r = subprocess.run(
                [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(footage_path)],
                capture_output=True, text=True,
            )
            trim_end = float(r.stdout.strip())
        except Exception:
            trim_end = 0.0

    match = MatchMeta(
        chunk_id=f"{req.filename}_manual",
        source_file=str(footage_path),
        trim_start=req.trim_start,
        trim_end=trim_end,
        match_score=1.0,
        match_reason="manual",
    )
    # #389 — 수동 매칭에서도 trim 을 footage.clip_*_sec 에 자동 적용 (사용자가
    #         별도로 trim 범위를 지정한 경우 그 값이 곧 씬 길이가 되어야 함).
    _apply_match_trim_to_footage(footage, match)
    updated = store.update_scene(project_id, scene_id, footage=footage, match=match)
    if not updated:
        raise HTTPException(404, "씬을 찾을 수 없습니다")
    return {"scene_id": scene_id, "updated": updated.model_dump()}


# ── #311 — 클라이언트 키워드 매칭 결과 저장 ────────────────────────────────
# 백엔드 _match_keyword 알고리즘을 web/frontend/src/lib/clientKeywordMatching.ts 로
# 포팅했다. 클라이언트가 계산한 결과를 받아 store에 일괄 반영한다.
#
# 이 엔드포인트는 Gemini를 호출하지 않으며 (원본 _match_keyword와 동일) CPU만 사용한다.
# 따라서 cost_tracker.record_matching 도 호출하지 않는다 — 기존 _do_match도
# method == "keyword_filename" 케이스는 비용 기록을 하지 않으므로 정합성 유지.
#
# #327 — 입력 검증: match-manual과 동일한 수준으로 클라가 보낸 필드를 검증한다.
#   1. scene_id가 프로젝트의 실제 씬에 존재해야 한다.
#   2. filename basename이 footage_dir에 실제 파일로 존재해야 한다 (path traversal 차단).
#   3. footage_index.json이 있으면 chunk_id/source_file이 인덱스와 일치해야 한다
#      (chunk_id 빈 문자열은 fallback 매칭으로 간주 — 인덱스 검증 면제).
# 검증 실패 항목은 결과 응답의 failed_scene_ids에 포함하며, 모든 항목이 실패하면 422.

def _load_footage_index_chunks(project_id: str) -> tuple[set[str], set[str]] | None:
    """footage_index.json에서 chunk_id 집합과 source_file 집합(basename)을 추출.

    반환:
      (chunk_ids, source_basenames) — 인덱스가 없으면 None.
    """
    cache_dir = store.get_cache_dir(project_id)
    index_path = cache_dir / "footage_index.json"
    if not index_path.exists():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    chunks = data.get("chunks", [])
    chunk_ids = {str(c.get("chunk_id", "")) for c in chunks if c.get("chunk_id")}
    source_basenames = {
        Path(str(c.get("source_file", ""))).name
        for c in chunks
        if c.get("source_file")
    }
    return chunk_ids, source_basenames


@router.post("/{project_id}/match-keyword-client")
def save_keyword_match_result(project_id: str, req: SaveKeywordMatchRequest):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    # 검증용 사전 자료.
    valid_scene_ids = {s.scene_id for s in p.scenes}
    footage_dir = Path(p.footage_dir) if p.footage_dir else None
    index_info = _load_footage_index_chunks(project_id)

    updated_ids: list[int] = []
    failed_ids: list[int] = []
    # 각 실패 사유를 분리해 디버깅 가능하게 노출 (응답 본문에 포함).
    failure_reasons: list[dict] = []

    for item in req.items:
        # 1) scene_id 검증 — 프로젝트의 실제 씬 ID만 허용.
        if item.scene_id not in valid_scene_ids:
            failed_ids.append(item.scene_id)
            failure_reasons.append({
                "scene_id": item.scene_id,
                "reason": "scene_not_found",
                "detail": f"scene_id={item.scene_id}이 프로젝트에 존재하지 않습니다",
            })
            continue

        # 2) filename 검증 — basename만 추출해 path traversal 차단.
        #    클라가 절대경로를 보내도 백엔드는 basename만 사용한다 (footage 엔티티는 basename 저장).
        filename_basename = Path(item.filename).name
        if not filename_basename or filename_basename != item.filename:
            # 클라가 보낸 filename 필드는 basename이어야 한다. 아니면 거부.
            failed_ids.append(item.scene_id)
            failure_reasons.append({
                "scene_id": item.scene_id,
                "reason": "invalid_filename",
                "detail": f"filename은 basename이어야 합니다: {item.filename!r}",
            })
            continue

        # 3) footage_dir에 실제 파일 존재 검증 (match-manual과 동일 수준).
        if footage_dir is not None and footage_dir.exists():
            footage_path = footage_dir / filename_basename
            if not footage_path.exists():
                failed_ids.append(item.scene_id)
                failure_reasons.append({
                    "scene_id": item.scene_id,
                    "reason": "footage_not_found",
                    "detail": f"{filename_basename} 파일을 footage 폴더에서 찾을 수 없습니다",
                })
                continue

        # 4) chunk_id / source_file 정합성 검증.
        #    footage_index.json이 있을 때만 검증한다. chunk_id가 비어있으면 fallback
        #    매칭(폴더 스캔)이므로 인덱스 검증을 면제 — 백엔드 _match_keyword 동작과 일치.
        if index_info is not None and item.chunk_id:
            chunk_ids, source_basenames = index_info
            if item.chunk_id not in chunk_ids:
                failed_ids.append(item.scene_id)
                failure_reasons.append({
                    "scene_id": item.scene_id,
                    "reason": "chunk_id_not_in_index",
                    "detail": f"chunk_id={item.chunk_id!r}이 footage_index에 없습니다",
                })
                continue
            # source_file basename도 인덱스와 일치해야 한다.
            src_basename = Path(item.source_file).name
            if src_basename and src_basename not in source_basenames:
                failed_ids.append(item.scene_id)
                failure_reasons.append({
                    "scene_id": item.scene_id,
                    "reason": "source_file_not_in_index",
                    "detail": f"source_file={src_basename!r}이 footage_index에 없습니다",
                })
                continue

        # is_image 플래그를 신뢰하되, 미지정 시 확장자로 폴백 (안전망).
        is_image = item.is_image
        if not is_image:
            ext = Path(item.source_file).suffix.lower()
            is_image = ext in image_exts

        if is_image:
            from ..models import KenburnsEffect
            footage = KenburnsEffect(image=filename_basename)
        else:
            footage = ClipEffect(file=filename_basename)

        match = MatchMeta(
            chunk_id=item.chunk_id,
            source_file=item.source_file,
            trim_start=item.trim_start,
            trim_end=item.trim_end,
            match_score=item.match_score,
            # 백엔드 _match_keyword와 정확히 동일한 reason 문자열 사용 — 회귀 방지.
            match_reason="keyword_filename",
        )
        # #389 — 클라이언트 키워드 매칭에서도 trim 을 footage.clip_*_sec 에 자동 적용.
        _apply_match_trim_to_footage(footage, match)
        updated = store.update_scene(project_id, item.scene_id, footage=footage, match=match)
        if updated:
            updated_ids.append(item.scene_id)
        else:
            failed_ids.append(item.scene_id)
            failure_reasons.append({
                "scene_id": item.scene_id,
                "reason": "store_update_failed",
                "detail": "store.update_scene이 None을 반환했습니다",
            })

    # 모든 항목이 검증/저장 실패하면 422로 거부 — 정상 클라가 잘못된 페이로드를 보내는
    # 시나리오를 명확히 차단한다 (성공 0건일 때 200 반환은 사일런트 실패 위험).
    if req.items and not updated_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "모든 매칭 결과 항목이 검증/저장에 실패했습니다",
                "failed_scene_ids": failed_ids,
                "failure_reasons": failure_reasons,
            },
        )

    return {
        "updated_scene_ids": updated_ids,
        "failed_scene_ids": failed_ids,
        "failure_reasons": failure_reasons,
        "method": "keyword_filename",
        "computed_on": "client",
    }


# ── #355 후속 — 매칭 예상 비용 API ─────────────────────────────────────────
# Setup Step 3 의 Gemini 매칭 카드에서 "예상 ~원" 라벨을 즉시 표시하기 위한
# 사전 추정. 실제 호출이 일어나기 전에 (method, model, scene_count) 만으로
# pricing.json 단가표를 직접 조회해 USD/KRW 를 계산한다.
#
# 토큰 추정 근거(코드 상수로 박아둠):
#  - gemini_filename: 후보 5개 파일명 + 씬 설명·키워드 + 프롬프트 문구 ≈ 350자
#    Gemini 한글 1.5 chars/token 가정 → input ≈ 233 tokens, output JSON ≈ 30자/20 tokens.
#  - gemini_content : 후보 10개 청크 desc/keywords + 씬 설명 ≈ 1500자
#    → input ≈ 1000 tokens, output JSON ≈ 30자/20 tokens.
#
# KRW 환산은 프론트(CostBadge, PatchPreview) 와 동일하게 1380원/USD 사용.
# 환율을 코드 한 곳에 모으는 별도 리팩터링은 본 패치 범위 밖이므로 동일 상수 복제.

_MATCH_KRW_RATE = 1380

_MATCH_TOKEN_ESTIMATE = {
    # gemini_filename: 후보 5개 (레이블 ~25자 + 파일명) + 씬 설명/키워드 + 프롬프트 ≈ 350자
    #   → input ≈ 233 tokens (한글 1.5 chars/token). 레이블 길이가 파일명 stem 과 비슷해
    #   평균 토큰 증가량은 무시할 수준이라 기존 233 유지.
    "gemini_filename": {"input": 233, "output": 20},
    # gemini_content: 청크 10개당 레이블 ~30자 추가 (+300자 ≈ +200 tokens) → 1000 → 1200.
    "gemini_content":  {"input": 1200, "output": 20},
}


@router.get("/{project_id}/match-cost-estimate")
def match_cost_estimate(
    project_id: str,
    method: str,
    model: str | None = None,
    scene_count: int | None = None,
):
    """매칭 방식·모델·씬 수로 예상 비용을 즉시 계산해 반환한다.

    Gemini 매칭이 아니면 모두 0원. 프로젝트가 존재하지 않으면 404.
    `model` 미지정 시 프로젝트의 `gemini_model` 로 폴백.
    `scene_count` 미지정 시 프로젝트의 현재 씬 수.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    # 폴백 — 모델은 프로젝트 설정, 씬 수는 현재 씬 수.
    chosen_model = (model or "").strip() or (getattr(p, "gemini_model", None) or None)
    n = int(scene_count) if scene_count is not None else len(p.scenes)
    if n < 0:
        n = 0

    # Gemini 매칭이 아니면 0원 — 프론트는 라벨을 숨기거나 0원 표시.
    if method not in _MATCH_TOKEN_ESTIMATE:
        return {
            "usd": 0.0,
            "krw": 0,
            "per_scene_input_tokens": 0,
            "per_scene_output_tokens": 0,
            "scene_count": n,
            "method": method,
            "model": chosen_model or "",
        }

    # 모델별 단가 조회 — pricing.json `api.gemini.models[model]` 가 진실의 원천.
    from src.cost_estimator.estimator import get_gemini_model_pricing
    g = get_gemini_model_pricing(chosen_model)
    per_input = _MATCH_TOKEN_ESTIMATE[method]["input"]
    per_output = _MATCH_TOKEN_ESTIMATE[method]["output"]
    total_input = per_input * n
    total_output = per_output * n
    usd = (
        total_input / 1_000_000 * g["input_per_1m_tokens_usd"]
        + total_output / 1_000_000 * g["output_per_1m_tokens_usd"]
    )
    krw = round(usd * _MATCH_KRW_RATE)

    return {
        "usd": round(usd, 6),
        "krw": int(krw),
        "per_scene_input_tokens": per_input,
        "per_scene_output_tokens": per_output,
        "scene_count": n,
        "method": method,
        "model": chosen_model or "",
    }
