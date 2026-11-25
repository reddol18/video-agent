from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..models import FootageAsset, SplitPoint
from .. import state as store
from ..footage_paths import (
    SUFFIX_PAREN,
    count_scene_references,
    invalidate_footage_caches,
    normalize_policy,
    resolve_footage_name,
)
# #308 — client_meta sanity parser 공용 사용 (project router에 정의).
from .project import _parse_client_video_meta, _parse_client_audio_meta, _ensure_proxy

import sys as _sys_ffpath
_ROOT_FFPATH = Path(__file__).parent.parent.parent.parent
if str(_ROOT_FFPATH) not in _sys_ffpath.path:
    _sys_ffpath.path.insert(0, str(_ROOT_FFPATH))
from src.utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

router = APIRouter(prefix="/api/projects", tags=["studio"])

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
# ── #289/#290 — 오디오 자산 확장자 ─────────────────────────────────────────
# BGM 에디터·오버레이 SFX·향후 후처리 사운드 기능 공용.
# import-existing 및 future asset 분류 시 audio 타입으로 등록된다.
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}


# ── Request bodies ─────────────────────────────────────────────────────────

class UpdateLabelRequest(BaseModel):
    label: str


# ── Asset listing ──────────────────────────────────────────────────────────

@router.get("/{project_id}/studio/assets", response_model=list[FootageAsset])
def get_studio_assets(project_id: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    return p.studio_assets


# ── Label update ───────────────────────────────────────────────────────────

@router.patch("/{project_id}/studio/assets/{asset_id}")
def update_asset_label(project_id: str, asset_id: str, req: UpdateLabelRequest):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    for asset in p.studio_assets:
        if asset.asset_id == asset_id:
            asset.label = req.label
            store.update_project(project_id, studio_assets=p.studio_assets)
            return asset
    raise HTTPException(404, "asset을 찾을 수 없습니다")


# ── Asset delete ───────────────────────────────────────────────────────────

@router.delete("/{project_id}/studio/assets/{asset_id}")
def delete_studio_asset(project_id: str, asset_id: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    original = len(p.studio_assets)
    p.studio_assets = [a for a in p.studio_assets if a.asset_id != asset_id]
    if len(p.studio_assets) == original:
        raise HTTPException(404, "asset을 찾을 수 없습니다")
    store.update_project(project_id, studio_assets=p.studio_assets)
    return {"deleted": True}


# ── Asset bulk delete (다중 선택 삭제) ─────────────────────────────────────

class BulkDeleteAssetsRequest(BaseModel):
    asset_ids: list[str]


@router.post("/{project_id}/studio/assets/bulk-delete")
def bulk_delete_studio_assets(project_id: str, req: BulkDeleteAssetsRequest):
    """선택한 자산들을 studio_assets 목록에서만 제거한다.

    **footage_dir 의 실제 파일은 절대 삭제하지 않는다** (프로덕트 오너 지정 정책).
    목록에서 빠져도 씬이 그 파일을 참조하고 있으면 렌더는 계속 동작하므로,
    참조 중인 건수를 응답에 담아 프론트가 사전 경고할 수 있게 한다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    targets = set(req.asset_ids)
    if not targets:
        return {
            "deleted": 0,
            "not_found": [],
            "scene_referenced_count": 0,
            "scene_referenced_filenames": [],
            "files_kept": True,
        }

    removed = [a for a in p.studio_assets if a.asset_id in targets]
    remaining = [a for a in p.studio_assets if a.asset_id not in targets]
    found_ids = {a.asset_id for a in removed}
    not_found = [aid for aid in req.asset_ids if aid not in found_ids]

    # 씬이 참조 중인 파일 집계 — 파일 자체는 남기므로 경고 목적 정보.
    scenes = getattr(p, "scenes", []) or []
    referenced: list[str] = []
    ref_total = 0
    for a in removed:
        n = count_scene_references(scenes, a.filename)
        if n > 0:
            referenced.append(a.filename)
            ref_total += n

    # ── 분할 원본 플래그 정리 ───────────────────────────────────────────────
    # 조각이 모두 사라진 원본은 has_splits 를 내린다(목록 뱃지가 거짓으로 남지 않게).
    surviving_parents = {a.split_from for a in remaining if a.split_from}
    for a in remaining:
        if a.has_splits and a.asset_id not in surviving_parents:
            a.has_splits = False

    if removed:
        # 파일을 남기는 정책이므로 프록시/인덱스 캐시는 **무효화하지 않는다**.
        # 씬이 계속 그 파일을 참조할 수 있어, 캐시를 지우면 미리보기가 불필요하게
        # 재인코딩되고 AI 인덱스도 날아간다.
        store.update_project(project_id, studio_assets=remaining)

    return {
        "deleted": len(removed),
        "not_found": not_found,
        "scene_referenced_count": ref_total,
        "scene_referenced_filenames": referenced,
        # 호출부가 사용자에게 "파일은 남습니다" 를 확실히 안내할 수 있도록 명시.
        "files_kept": True,
    }


# ── Asset copy (스튜디오 간 footage 복사) ─────────────────────────────────

class CopyAssetRequest(BaseModel):
    # 출발지 프로젝트/자산. 도착지는 경로의 {project_id}.
    source_project_id: str
    source_asset_id: str


@router.post("/{project_id}/studio/assets/copy-from")
def copy_asset_from(project_id: str, req: CopyAssetRequest, background_tasks: BackgroundTasks):
    """다른 프로젝트의 footage 파일을 이 프로젝트로 서버에서 직접 복사한다.

    소스 footage_dir의 실제 파일을 타겟 footage_dir로 shutil.copy2 물리 복사하고,
    새 asset_id/파일명으로 studio_assets에 등록한다. 출발지·도착지가 같은 footage_dir을
    공유하더라도 항상 uuid prefix 새 파일을 만들어 원본을 덮어쓰지 않는다.
    파일은 서버 footage_dir에 저장되므로 에디터 미리보기/렌더링과 일관된다.
    """
    src_p = store.get_project(req.source_project_id)
    if not src_p:
        raise HTTPException(404, "원본 프로젝트를 찾을 수 없습니다")

    # asset_id 우선 조회. 구형 프로젝트는 asset_id가 filename일 수 있어 filename fallback.
    src_asset = next(
        (a for a in src_p.studio_assets if a.asset_id == req.source_asset_id), None
    )
    if src_asset is None:
        src_asset = next(
            (a for a in src_p.studio_assets if a.filename == req.source_asset_id), None
        )
    if src_asset is None:
        raise HTTPException(404, "원본 asset을 찾을 수 없습니다")

    if not src_p.footage_dir:
        raise HTTPException(400, "원본 프로젝트에 footage_dir이 설정되지 않았습니다")
    src_file = Path(src_p.footage_dir) / src_asset.filename
    if not src_file.exists():
        raise HTTPException(404, f"원본 파일을 찾을 수 없습니다: {src_asset.filename}")

    dest_p = store.get_project(project_id)
    if not dest_p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    if dest_p.footage_dir:
        dest_dir = Path(dest_p.footage_dir)
    else:
        dest_dir = store.get_output_dir(project_id).parent / "footage"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 충돌 회피 파일명 (항상 uuid prefix) + 물리 복사
    stem = Path(src_asset.filename).stem
    ext = Path(src_asset.filename).suffix
    unique_name = f"{uuid.uuid4().hex[:8]}_{stem}{ext}"
    dst = dest_dir / unique_name
    try:
        shutil.copy2(src_file, dst)
    except Exception as e:
        raise HTTPException(500, f"footage 파일 복사 오류: {e}") from e

    if not dest_p.footage_dir:
        store.update_project(project_id, footage_dir=str(dest_dir))

    asset = FootageAsset(
        asset_id=str(uuid.uuid4()),
        filename=unique_name,
        label=src_asset.label,
        asset_type=src_asset.asset_type,
        source="imported",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store.update_project(project_id, studio_assets=dest_p.studio_assets + [asset])
    background_tasks.add_task(_ensure_proxy, project_id, unique_name)
    return {"asset": asset.model_dump()}


@router.post("/{project_id}/studio/asset-file-sync")
async def sync_studio_asset_file(
    project_id: str,
    background_tasks: BackgroundTasks,
    filename: str = Form(...),
    file: UploadFile = File(...),
    on_conflict: str = Form("skip"),
):
    """footage 파일을 '원본 파일명 그대로' 서버 footage_dir에 저장한다.

    클라이언트가 saveAsset(로컬 폴더)으로 만든 자산을 서버에도 확보해
    미리보기/렌더가 항상 동작하도록 한다. 녹화·가져오기·구간분할·크롭 공통.
    studio_assets 등록은 클라이언트가 별도로 하므로 여기선 파일만 저장한다.

    같은 이름의 파일이 이미 있을 때의 처리는 `on_conflict` 로 정한다:
      - "skip"      — 저장하지 않고 기존 파일 유지 (기본값, 기존 호출부 하위호환)
      - "overwrite" — 새 파일로 덮어쓰고 프록시·인덱스 캐시를 무효화
      - "rename"    — `이름 (2).mp4` 로 회피 저장

    응답의 `filename` 은 **실제 저장된 최종 파일명**이다. rename 시 요청한 이름과
    달라지므로 클라이언트는 반드시 이 값으로 자산을 등록해야 한다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    footage_dir = Path(p.footage_dir) if p.footage_dir else store.get_output_dir(project_id).parent / "footage"
    footage_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(filename).name  # 경로 조작 방지
    if not safe_name:
        raise HTTPException(400, "잘못된 파일명입니다")
    policy = normalize_policy(on_conflict)
    dst = footage_dir / safe_name
    conflicted = dst.exists() and dst.stat().st_size > 0

    if conflicted and policy == "skip":
        await file.read()  # multipart body 소비
        if not p.footage_dir:
            store.update_project(project_id, footage_dir=str(footage_dir))
        return {"ok": True, "filename": safe_name, "requested_filename": safe_name,
                "skipped": True, "conflict": True, "applied_policy": "skip"}

    if conflicted and policy == "rename":
        dst = resolve_footage_name(footage_dir, safe_name, SUFFIX_PAREN)

    content = await file.read()
    if not content:
        raise HTTPException(400, "빈 파일입니다")
    dst.write_bytes(content)

    # 덮어쓰기는 파일명을 키로 하는 파생 캐시를 전부 낡게 만든다 — 반드시 무효화.
    if conflicted and policy == "overwrite":
        invalidate_footage_caches(project_id, dst.name)

    if not p.footage_dir:
        store.update_project(project_id, footage_dir=str(footage_dir))

    background_tasks.add_task(_ensure_proxy, project_id, dst.name)
    return {"ok": True, "filename": dst.name, "requested_filename": safe_name,
            "skipped": False, "conflict": conflicted,
            "applied_policy": policy if conflicted else "none"}


# ── Footage 충돌 사전 검사 ─────────────────────────────────────────────────

class ConflictCheckRequest(BaseModel):
    filenames: list[str]


@router.post("/{project_id}/studio/footage-conflicts")
def check_footage_conflicts(project_id: str, req: ConflictCheckRequest):
    """가져오려는 파일명들 중 이미 존재하는 것과, 그 파일을 참조하는 씬 수를 반환한다.

    프론트가 업로드 전에 호출해 충돌 해결 다이얼로그를 띄우는 데 쓴다.
    `scene_refs` 는 덮어쓰기 선택 시 "씬 N개가 함께 바뀝니다" 경고에 사용된다.
    `suggested` 는 rename 을 골랐을 때 실제로 저장될 이름의 미리보기다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    footage_dir = Path(p.footage_dir) if p.footage_dir else store.get_output_dir(project_id).parent / "footage"
    conflicts = []
    for raw in req.filenames:
        name = Path(raw).name
        if not name:
            continue
        dst = footage_dir / name
        if dst.exists() and dst.stat().st_size > 0:
            conflicts.append({
                "filename": name,
                "scene_refs": count_scene_references(p.scenes, name),
                "suggested": resolve_footage_name(footage_dir, name, SUFFIX_PAREN).name,
            })
    return {"conflicts": conflicts}


# ── Recording upload ───────────────────────────────────────────────────────

@router.post("/{project_id}/studio/recording")
async def upload_recording(
    project_id: str,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    label: str = Form(""),
    split_points: str = Form(""),  # JSON 문자열
    client_meta: str | None = Form(None),  # #308 — 분할 없는 단일 저장 케이스에만 의미
):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    footage_dir = Path(p.footage_dir)
    footage_dir.mkdir(parents=True, exist_ok=True)

    # 원본 파일 저장
    ext = Path(video.filename or "recording.webm").suffix or ".webm"
    base_name = f"recording_{uuid.uuid4().hex[:8]}{ext}"
    dest = footage_dir / base_name

    content = await video.read()
    dest.write_bytes(content)

    # #308 — 단일 저장(분할 없음) 케이스에서 client_meta 신뢰 로그.
    # FootageAsset 자체는 width/height/duration 필드가 없으므로(인덱싱 단계에서
    # ffprobe로 측정) 현재로서는 통합 로그만 남기고, 향후 asset 메타 캐시화
    # 도입 시 그대로 활용한다.
    _client_meta_obj = _parse_client_video_meta(client_meta)
    if _client_meta_obj is not None:
        print(
            f"[client_meta] studio/recording trust: "
            f"{_client_meta_obj['width']}x{_client_meta_obj['height']} "
            f"dur={_client_meta_obj['duration']:.2f}s",
            flush=True,
        )

    now = datetime.now(timezone.utc).isoformat()

    # split_points 파싱
    splits: list[SplitPoint] = []
    if split_points.strip():
        import json as _json
        try:
            raw = _json.loads(split_points)
            splits = [SplitPoint(**item) for item in raw]
        except Exception:
            pass

    new_assets: list[FootageAsset] = []

    if splits:
        # 구간 분할
        import time as _t_split
        _split_t0 = _t_split.monotonic()
        _split_bytes_out = 0
        stem = Path(base_name).stem
        for i, sp in enumerate(splits):
            part_name = f"{stem}_part_{i + 1}.mp4"
            part_path = footage_dir / part_name
            result = subprocess.run(
                [
                    ffmpeg_bin(), "-y",
                    "-i", str(dest),
                    "-ss", f"{sp.start:.3f}",
                    "-to", f"{sp.end:.3f}",
                    "-c", "copy",
                    str(part_path),
                ],
                capture_output=True,
            )
            if result.returncode != 0 or not part_path.exists():
                continue
            _split_bytes_out += part_path.stat().st_size
            asset = FootageAsset(
                asset_id=str(uuid.uuid4()),
                filename=part_name,
                label=sp.label or label,
                asset_type="video",
                source="split",
                created_at=now,
            )
            new_assets.append(asset)
        # #301 — 녹화 분할 ffmpeg 비용 기록
        try:
            from ..cost_tracker import measure_phase
            with measure_phase(project_id) as _est:
                _est.record_transcode(
                    cpu_seconds=_t_split.monotonic() - _split_t0,
                    output_bytes=_split_bytes_out,
                    phase_label="10_recording_split",
                )
        except Exception as _err:
            print(f"[cost] recording_split 기록 실패: {_err}")
        # 분할 성공 시 원본 유지 (파일은 남김, assets에는 미등록)
    else:
        # 단일 저장
        asset = FootageAsset(
            asset_id=str(uuid.uuid4()),
            filename=base_name,
            label=label,
            asset_type="video",
            source="recorded",
            created_at=now,
        )
        new_assets.append(asset)

    updated = p.studio_assets + new_assets
    store.update_project(project_id, studio_assets=updated)

    for _a in new_assets:
        background_tasks.add_task(_ensure_proxy, project_id, _a.filename)
    return {"assets": [a.model_dump() for a in new_assets]}


# ── Audio asset upload (#bgm) ────────────────────────────────────────────
#
# BGM/SFX 트랙 입력으로 사용할 오디오 파일을 footage_dir에 저장하고 studio_assets에 등록한다.
@router.post("/{project_id}/studio/audio-upload")
async def upload_audio_asset(
    project_id: str,
    file: UploadFile = File(...),
    label: str = "",
    client_meta: str | None = Form(None),  # #308 — 오디오 duration JSON
):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    if not p.footage_dir:
        raise HTTPException(400, "footage_dir이 설정되지 않았습니다")
    footage_dir = Path(p.footage_dir)
    footage_dir.mkdir(parents=True, exist_ok=True)

    # 파일명 안전화 — 동일명 충돌 회피용 prefix.
    orig_name = file.filename or "audio.mp3"
    ext = Path(orig_name).suffix.lower() or ".mp3"
    base_stem = Path(orig_name).stem or "audio"
    unique_name = f"{uuid.uuid4().hex[:8]}_{base_stem}{ext}"
    dst = footage_dir / unique_name
    content = await file.read()
    if not content:
        raise HTTPException(400, "빈 파일입니다")
    dst.write_bytes(content)

    # #308 — 오디오 client_meta 신뢰 로그(duration).
    # #312 — 응답에 `client_meta_trusted` 플래그를 추가하여 검증·디버깅을 돕는다.
    # 본 엔드포인트는 자체로 duration 을 저장하지 않으므로(이후 ffmpeg 합성 단계에서
    # 측정) 신뢰 여부는 로그/응답 표시용이지만, 클라가 자기 client_meta 가 백엔드
    # 에 도달했는지 확인할 수 있다.
    _client_audio_meta = _parse_client_audio_meta(client_meta)
    _audio_meta_trusted = _client_audio_meta is not None
    if _client_audio_meta is not None:
        # sample_rate / channels 는 옵셔널이라 dict.get 으로 안전 추출.
        _sr = _client_audio_meta.get("sample_rate")
        _ch = _client_audio_meta.get("channels")
        _extra = ""
        if _sr is not None:
            _extra += f" sr={_sr}"
        if _ch is not None:
            _extra += f" ch={_ch}"
        print(
            f"[client_meta] studio/audio-upload trust: "
            f"dur={_client_audio_meta['duration']:.2f}s{_extra}",
            flush=True,
        )

    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc).isoformat()
    asset = FootageAsset(
        asset_id=str(uuid.uuid4()),
        filename=unique_name,
        label=label or base_stem,
        asset_type="audio",
        source="recorded",
        created_at=now,
    )
    updated = p.studio_assets + [asset]
    store.update_project(project_id, studio_assets=updated)

    # #301 — 오디오 자산 업로드 저장 비용
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_transcode(
                cpu_seconds=0,
                output_bytes=dst.stat().st_size,
                phase_label="14_audio_upload",
            )
    except Exception as _err:
        print(f"[cost] audio_upload 기록 실패: {_err}")

    return {
        "asset": asset.model_dump(),
        # #312 — 검증·디버깅용. 응답 스키마 호환을 위해 항상 bool 반환.
        "client_meta_trusted": _audio_meta_trusted,
    }


# ── Presentation capture ───────────────────────────────────────────────────

@router.post("/{project_id}/studio/presentation")
async def upload_presentation(
    project_id: str,
    html_file: UploadFile | None = File(default=None),
    # #313 — 클라이언트(pdf.js / html2canvas) 가 미리 캡처한 PNG 페이지들.
    # 존재하면 Playwright 헤드리스 호출을 건너뛴다 (비용 절감 핵심 경로).
    # multipart 에서 `images` 필드를 반복해 동일 키로 여러 파일을 전송한다.
    images: list[UploadFile] | None = File(default=None),
    # #313 — 클라가 캡처한 결과의 메타(원본 파일 stem, 페이지 수 등).
    # 디버깅·로그·파일명 부여용. 미전송이어도 동작은 가능.
    client_capture_meta: str | None = Form(default=None),
):
    """프레젠테이션 → 페이지별 이미지 등록.

    두 가지 입력 모드:
    1) `images` (PNG 다중) — 클라이언트가 pdf.js / html2canvas 로 직접 캡처한
       결과를 그대로 받아 이미지 자산으로 등록. **헤드리스 브라우저 호출 없음**
       (#313 핵심 절감 경로).
    2) `html_file` (HTML) — 클라 캡처가 실패했거나 사용자가 명시적으로 백엔드
       fallback 을 요청한 경우. Playwright 로 페이지 캡처.

    두 입력이 모두 비어 있으면 400.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    footage_dir = Path(p.footage_dir)
    footage_dir.mkdir(parents=True, exist_ok=True)

    # ── 모드 분기 ─────────────────────────────────────────────────────────
    # 클라 캡처 결과(images) 가 1장 이상 도착했으면 클라 모드.
    has_client_images = bool(images) and any(
        (img.filename or "") for img in images
    )

    if not has_client_images and html_file is None:
        raise HTTPException(
            400,
            "프레젠테이션 파일이 비어 있습니다. images(PNG 다중) 또는 html_file 중 하나는 필요합니다.",
        )

    captured: list[Path] = []
    used_client_capture = False

    if has_client_images:
        # ── 클라이언트 캡처 모드 ──────────────────────────────────────────
        used_client_capture = True
        # 파일명 stem 안전화 — 같은 세션에서 여러 번 업로드해도 충돌하지 않게
        # 고유 prefix 를 붙인다.
        prefix = f"presentation_{uuid.uuid4().hex[:8]}"
        for i, img in enumerate(images or []):
            if not img.filename:
                continue
            ext = Path(img.filename).suffix.lower() or ".png"
            # 클라가 보낸 페이지 순서를 그대로 보존 (1-based zero-pad 3).
            out_name = f"{prefix}_page_{i + 1:03d}{ext}"
            out_path = footage_dir / out_name
            data = await img.read()
            if not data:
                # 빈 파일은 건너뛴다 — 클라 측 toBlob 실패 안전망.
                continue
            out_path.write_bytes(data)
            captured.append(out_path)

        if not captured:
            raise HTTPException(
                400,
                "클라이언트 캡처 이미지가 모두 비어 있습니다. html_file 로 다시 시도하세요.",
            )

        # #313 — 클라이언트 캡처 모드의 비용: 거의 저장/네트워크만.
        # Playwright CPU 비용(0)을 명시적으로 0 으로 기록하여 CostBadge 가
        # "캡처 모드 절감액" 을 보여줄 수 있도록 한다.
        try:
            from ..cost_tracker import measure_phase
            _bytes_out = sum(c.stat().st_size for c in captured if c.exists())
            with measure_phase(project_id) as _est:
                _est.record_transcode(
                    cpu_seconds=0,  # 헤드리스 브라우저 사용 안 함
                    output_bytes=_bytes_out,
                    phase_label="11_presentation_capture",
                )
        except Exception as _err:
            print(f"[cost] presentation(client) 기록 실패: {_err}")

        # 메타 로그 — 검증/디버깅용
        if client_capture_meta:
            print(
                f"[client_meta] studio/presentation client-capture: "
                f"pages={len(captured)} meta={client_capture_meta[:200]}",
                flush=True,
            )
    else:
        # ── 백엔드 fallback (Playwright) 모드 ────────────────────────────
        # HTML 임시 저장
        html_name = f"presentation_{uuid.uuid4().hex[:8]}.html"
        html_path = footage_dir / html_name
        content = await html_file.read()  # type: ignore[union-attr]
        html_path.write_bytes(content)

        import time as _t_pres
        _pres_t0 = _t_pres.monotonic()
        try:
            captured = await _capture_html_pages(str(html_path), footage_dir)
        except Exception as e:
            raise HTTPException(500, f"Playwright 캡처 실패: {e}")
        finally:
            html_path.unlink(missing_ok=True)

        # #301 — Playwright 캡처는 Chromium headless로 CPU 비용 발생
        try:
            from ..cost_tracker import measure_phase
            _bytes_out = sum(c.stat().st_size for c in captured if c.exists())
            with measure_phase(project_id) as _est:
                _est.record_transcode(
                    cpu_seconds=_t_pres.monotonic() - _pres_t0,
                    output_bytes=_bytes_out,
                    phase_label="11_presentation_capture",
                )
        except Exception as _err:
            print(f"[cost] presentation 기록 실패: {_err}")

    now = datetime.now(timezone.utc).isoformat()
    new_assets = [
        FootageAsset(
            asset_id=str(uuid.uuid4()),
            filename=img.name,
            label="",
            asset_type="image",
            source="presentation",
            created_at=now,
        )
        for img in captured
    ]

    updated = p.studio_assets + new_assets
    store.update_project(project_id, studio_assets=updated)

    return {
        "assets": [a.model_dump() for a in new_assets],
        # #313 — 클라가 캡처 결과가 백엔드에 도달했는지 확인 가능.
        "client_capture_used": used_client_capture,
    }


async def _capture_html_pages(html_path: str, output_dir: Path) -> list[Path]:
    """Playwright로 HTML 슬라이드 각 페이지를 PNG로 캡처."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright가 설치되지 않았습니다. 'uv add playwright && playwright install chromium'을 실행하세요.")

    stem = Path(html_path).stem
    captured: list[Path] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.goto(f"file://{html_path}")
        await page.wait_for_load_state("networkidle")

        # 슬라이드 수 감지: section 태그, .slide, [data-page] 순으로 시도
        slide_count = 0
        for selector in ["section", ".slide", "[data-page]", ".step"]:
            count = await page.locator(selector).count()
            if count > 1:
                slide_count = count
                break

        if slide_count <= 1:
            # 단일 페이지
            out = output_dir / f"{stem}_page_001.png"
            await page.screenshot(path=str(out), full_page=False)
            captured.append(out)
        else:
            # reveal.js 등: 각 슬라이드로 이동 후 캡처
            # 첫 번째 슬라이드 (이미 로드됨)
            for i in range(slide_count):
                out = output_dir / f"{stem}_page_{i + 1:03d}.png"
                if i > 0:
                    # 오른쪽 화살표로 다음 슬라이드
                    await page.keyboard.press("ArrowRight")
                    await page.wait_for_timeout(300)
                await page.screenshot(path=str(out), full_page=False)
                captured.append(out)

        await browser.close()

    return [p for p in captured if p.exists()]


# ── Import existing footage_dir files ─────────────────────────────────────

@router.post("/{project_id}/studio/import-existing")
def import_existing_footage(project_id: str):
    """footage_dir의 기존 파일들을 'imported' source로 studio_assets에 일괄 등록."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    footage_dir = Path(p.footage_dir)
    if not footage_dir.exists():
        raise HTTPException(400, "footage_dir이 존재하지 않습니다")

    existing_filenames = {a.filename for a in p.studio_assets}
    now = datetime.now(timezone.utc).isoformat()
    new_assets: list[FootageAsset] = []

    for f in sorted(footage_dir.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in VIDEO_EXTS:
            asset_type = "video"
        elif ext in IMAGE_EXTS:
            asset_type = "image"
        elif ext in AUDIO_EXTS:
            # #289/#290 — audio 타입 자산 (BGM/SFX 공용)
            asset_type = "audio"
        else:
            continue
        if f.name in existing_filenames:
            continue
        new_assets.append(FootageAsset(
            asset_id=str(uuid.uuid4()),
            filename=f.name,
            label="",
            asset_type=asset_type,
            source="imported",
            created_at=now,
        ))

    updated = p.studio_assets + new_assets
    store.update_project(project_id, studio_assets=updated)

    return {"imported": len(new_assets), "assets": [a.model_dump() for a in new_assets]}


# ── Asset thumbnail ────────────────────────────────────────────────────────

@router.get("/{project_id}/studio/assets/{asset_id}/thumbnail")
def get_asset_thumbnail(project_id: str, asset_id: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    asset = next((a for a in p.studio_assets if a.asset_id == asset_id), None)
    if not asset:
        raise HTTPException(404, "asset을 찾을 수 없습니다")

    src = Path(p.footage_dir) / asset.filename
    if not src.exists():
        raise HTTPException(404, "파일을 찾을 수 없습니다")

    thumb_dir = store.get_cache_dir(project_id) / "studio_thumbnails"
    thumb_dir.mkdir(exist_ok=True)
    thumb = thumb_dir / (Path(asset.filename).stem + ".jpg")

    import time as _t_studio_thumb
    cache_hit = thumb.exists()
    _t0 = _t_studio_thumb.monotonic()
    if not thumb.exists():
        if asset.asset_type == "image":
            import shutil
            shutil.copy2(src, thumb)
        elif asset.asset_type == "audio":
            # #289/#290 — audio 자산은 영상이 없으므로 ffmpeg showwavespic 파형 이미지로 대체.
            # 파형 생성 실패 시 빈 회색 이미지 폴백.
            r = subprocess.run(
                [ffmpeg_bin(), "-y", "-i", str(src),
                 "-filter_complex", "showwavespic=s=320x180:colors=#4ade80",
                 "-frames:v", "1", str(thumb)],
                capture_output=True,
            )
            if r.returncode != 0 or not thumb.exists():
                # 폴백: 회색 plain 이미지
                subprocess.run(
                    [ffmpeg_bin(), "-y",
                     "-f", "lavfi", "-i", "color=c=#1a1a1a:s=320x180:d=1",
                     "-frames:v", "1", str(thumb)],
                    capture_output=True,
                )
        else:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-ss", "0", "-i", str(src),
                 "-frames:v", "1", "-q:v", "3", "-vf", "scale=320:-1",
                 str(thumb)],
                capture_output=True,
            )

    if not thumb.exists():
        raise HTTPException(500, "썸네일 생성 실패")

    # #301 — 비용 기록
    try:
        from ..cost_tracker import measure_phase
        _elapsed = _t_studio_thumb.monotonic() - _t0
        with measure_phase(project_id) as _est:
            _est.record_scene_preview(
                cpu_seconds=_elapsed,
                output_bytes=thumb.stat().st_size,
                cache_hit=cache_hit,
            )
    except Exception as _err:
        print(f"[cost] studio_thumbnail 기록 실패: {_err}")

    return FileResponse(str(thumb), media_type="image/jpeg")


# ── Asset stream (audio/video 미리듣기·미리보기) ───────────────────────────
# BgmEditor·오버레이 SFX UI에서 audio 자산을 HTMLAudioElement로 재생할 때 사용한다.
@router.get("/{project_id}/studio/assets/{asset_id}/stream")
def stream_studio_asset(project_id: str, asset_id: str):
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404)
    asset = next((a for a in p.studio_assets if a.asset_id == asset_id), None)
    if not asset:
        raise HTTPException(404, "asset을 찾을 수 없습니다")
    src = Path(p.footage_dir) / asset.filename
    if not src.exists():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    # MIME 타입 추정 — audio/video는 확장자로 추측, 미지 타입은 octet-stream.
    ext = src.suffix.lower()
    mime_map = {
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".aac": "audio/aac", ".flac": "audio/flac",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".mkv": "video/x-matroska", ".m4v": "video/mp4", ".avi": "video/x-msvideo",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
    }
    media_type = mime_map.get(ext, "application/octet-stream")
    # #301 — 자산 스트리밍 egress 기록
    try:
        from ..cost_tracker import measure_phase
        with measure_phase(project_id) as _est:
            _est.record_egress(bytes_size=src.stat().st_size)
    except Exception as _err:
        print(f"[cost] asset_stream 기록 실패: {_err}")
    return FileResponse(str(src), media_type=media_type)


# ── Asset filmstrip (프레임 스트립) ────────────────────────────────────────
# 구간 분할 화면에서 "자를 지점"을 눈으로 찾게 돕는 스프라이트 시트.
# interval 초 간격 프레임을 가로 1줄로 이어붙인 **JPEG 1장**으로 반환하므로
# 프론트는 요청 1회 + CSS background-position 으로 N개 썸네일을 그릴 수 있다.

# 타일 1장의 가로 폭(px). 세로는 원본 종횡비에 맞춰 ffmpeg 가 정한다(scale=W:-1).
FILMSTRIP_TILE_WIDTH = 160
# 스프라이트 1장의 최대 타일 수. 초과하면 interval 을 자동으로 키워 맞춘다.
# 160px 타일 × 200장 = 32000px — JPEG 최대 변(65535)과 브라우저 디코딩 모두 안전권.
FILMSTRIP_MAX_TILES = 200
# 프론트 토글이 보내는 값만 허용 — 임의 값으로 캐시가 무한 증식하는 것을 막는다.
FILMSTRIP_ALLOWED_INTERVALS = (1, 2, 5, 10)


def _filmstrip_probe_size(path: Path) -> tuple[int, int] | None:
    """생성된 스프라이트의 실제 width/height 를 ffprobe 로 읽는다.

    타일 수를 공식(round(duration/interval))으로만 추정하면 ffmpeg `fps` 필터의
    반올림 동작과 1장 어긋날 수 있고, 그러면 `tile` 이 빈 칸을 패딩해 넣어
    프론트의 스프라이트 좌표가 전부 밀린다. 그래서 **결과물의 실측 폭**에서
    타일 수를 역산한다.
    """
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    parts = [x for x in r.stdout.strip().split(",") if x.strip()]
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


@router.get("/{project_id}/studio/assets/{asset_id}/filmstrip")
def get_asset_filmstrip(project_id: str, asset_id: str, interval: float = 2, meta: int = 0):
    """프레임 스트립 스프라이트 시트.

    - `?meta=1` → 스프라이트 좌표 계산에 필요한 메타만 JSON 으로 반환(이미지 전송 없음)
    - 그 외   → JPEG 본문 + `X-Filmstrip-*` 헤더에 같은 메타를 동봉

    캐시 키 = 파일 실체(file_signature = name+mtime+size) + interval.
    원본을 덮어쓰면 mtime/size 가 바뀌어 자동 무효화된다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    asset = next((a for a in p.studio_assets if a.asset_id == asset_id), None)
    if not asset:
        raise HTTPException(404, "asset을 찾을 수 없습니다")
    if asset.asset_type != "video":
        raise HTTPException(400, "영상 자산만 프레임 스트립을 만들 수 있습니다")

    src = Path(p.footage_dir) / asset.filename
    if not src.exists():
        raise HTTPException(404, "파일을 찾을 수 없습니다")

    # 요청 interval 정규화 — 허용 목록 중 가장 가까운 값으로 스냅.
    req_interval = min(FILMSTRIP_ALLOWED_INTERVALS, key=lambda v: abs(v - float(interval)))

    from .project import _ffprobe_duration
    duration = _ffprobe_duration(str(src)) or 0.0
    if duration <= 0:
        raise HTTPException(422, "영상 길이를 읽을 수 없어 프레임 스트립을 만들 수 없습니다")

    # 최대 타일 수 제한 — 초과하면 interval 을 키워 맞춘다(허용 목록 밖 값도 허용).
    # 예) 2시간 영상 + interval=1 → 7200장 → 36초로 올려 200장 이내로.
    eff_interval = float(req_interval)
    if duration / eff_interval > FILMSTRIP_MAX_TILES:
        import math as _math
        eff_interval = _math.ceil(duration / FILMSTRIP_MAX_TILES)

    # 캐시 키: file_signature(name+mtime+size) + 적용 interval + 타일 폭.
    from src.render_spec import preview_stages as _stages
    sig = _stages.file_signature(src)
    key = _stages.stage_hash("filmstrip", {
        "sig": sig,
        "interval": eff_interval,
        "tile_w": FILMSTRIP_TILE_WIDTH,
    })

    strip_dir = store.get_cache_dir(project_id) / "studio_filmstrips"
    strip_dir.mkdir(parents=True, exist_ok=True)
    strip = strip_dir / f"{key}.jpg"

    import time as _t_strip
    cache_hit = strip.exists() and strip.stat().st_size > 0
    _t0 = _t_strip.monotonic()

    if not cache_hit:
        # 예상 타일 수 — fps 필터 반올림 때문에 1장 어긋날 수 있으므로 넉넉히 잡고,
        # 생성 후 실측 폭에서 실제 타일 수를 역산한다.
        est_tiles = max(1, round(duration / eff_interval))
        r = subprocess.run(
            [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(src),
             "-vf", f"fps=1/{eff_interval},scale={FILMSTRIP_TILE_WIDTH}:-1,tile={est_tiles}x1",
             "-frames:v", "1", "-q:v", "4", str(strip)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0 or not strip.exists() or strip.stat().st_size == 0:
            strip.unlink(missing_ok=True)
            raise HTTPException(500, f"프레임 스트립 생성 실패: {(r.stderr or '')[:300]}")

    size = _filmstrip_probe_size(strip)
    if not size:
        strip.unlink(missing_ok=True)
        raise HTTPException(500, "프레임 스트립 크기를 읽을 수 없습니다")
    sheet_w, tile_h = size
    # 실측 폭 → 실제 타일 수. tile 필터가 빈 칸을 패딩했더라도 좌표는 정확히 맞는다.
    tile_count = max(1, sheet_w // FILMSTRIP_TILE_WIDTH)

    info = {
        "interval": eff_interval,
        "requested_interval": float(req_interval),
        "tile_count": tile_count,
        "tile_width": FILMSTRIP_TILE_WIDTH,
        "tile_height": tile_h,
        "sheet_width": sheet_w,
        "duration_sec": duration,
        "cache_hit": cache_hit,
    }

    # #301 — 비용 기록 (썸네일 엔드포인트와 동일 방식)
    try:
        from ..cost_tracker import measure_phase
        _elapsed = _t_strip.monotonic() - _t0
        with measure_phase(project_id) as _est:
            _est.record_scene_preview(
                cpu_seconds=_elapsed,
                output_bytes=strip.stat().st_size,
                cache_hit=cache_hit,
            )
    except Exception as _err:
        print(f"[cost] studio_filmstrip 기록 실패: {_err}")

    if meta:
        return info

    return FileResponse(
        str(strip),
        media_type="image/jpeg",
        headers={
            "X-Filmstrip-Interval": str(eff_interval),
            "X-Filmstrip-Tile-Count": str(tile_count),
            "X-Filmstrip-Tile-Width": str(FILMSTRIP_TILE_WIDTH),
            "X-Filmstrip-Tile-Height": str(tile_h),
            "X-Filmstrip-Sheet-Width": str(sheet_w),
            "X-Filmstrip-Duration": str(duration),
            # 프론트가 fetch 로 헤더를 읽을 수 있게 노출(동일 출처라도 명시가 안전).
            "Access-Control-Expose-Headers":
                "X-Filmstrip-Interval,X-Filmstrip-Tile-Count,X-Filmstrip-Tile-Width,"
                "X-Filmstrip-Tile-Height,X-Filmstrip-Sheet-Width,X-Filmstrip-Duration",
        },
    )


# ── Scene index (장면 텍스트 검색) ──────────────────────────────────────────
# `web/data/{project_id}/scene_index.json` 은 외부 도구가 생성한 **읽기 전용**
# 장면 설명 색인이다. 백엔드는 절대 쓰지 않고, 없거나 깨져 있으면
# `available: false` 로만 알려 프론트가 조용히 기능을 끄게 한다.
#
# 구조(요구사항 기준):
#   { "_meta": {...}, "signatures": {filename: {name,mtime,size}}, "segments": [...] }
#   segments[].start_sec / end_sec 는 **해당 클립 내부의 상대 시각**이다.

SCENE_INDEX_FILENAME = "scene_index.json"


@router.get("/{project_id}/studio/scene-index")
def get_studio_scene_index(project_id: str, asset_id: str | None = None):
    """장면 설명 색인 조회.

    - 파일이 없거나 JSON 파싱 실패 → 404/500 이 아니라
      `{"available": False, "segments": []}`. 색인은 어디까지나 **보조 기능**이고,
      이 응답이 실패하면 구간 분할 화면 전체가 망가지므로 의도적으로 성공 응답을 준다.
    - `asset_id` 지정 시 그 자산의 filename 에 해당하는 segments 만 돌려준다.
    - 각 파일의 현재 실체를 `file_signature` 로 재계산해 색인 생성 당시와 다르면
      `stale: True`, 원본이 사라졌으면 `missing: True` 를 붙인다.
    """
    import json as _json

    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    index_path = store.DATA_DIR / project_id / SCENE_INDEX_FILENAME
    if not index_path.exists():
        return {"available": False, "segments": []}

    try:
        data = _json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("최상위가 object 가 아님")
        raw_segments = data.get("segments") or []
        if not isinstance(raw_segments, list):
            raise ValueError("segments 가 배열이 아님")
        raw_signatures = data.get("signatures") or {}
        if not isinstance(raw_signatures, dict):
            raw_signatures = {}
    except Exception as err:
        # 손상된 색인 하나 때문에 분할 화면이 죽지 않도록 reason 만 실어 보낸다.
        print(f"[scene-index] 읽기 실패({index_path}): {err}")
        return {"available": False, "segments": [], "reason": str(err)[:200]}

    # asset_id → filename. 없는 asset_id 는 빈 결과(에러 아님)로 처리해,
    # 자산 삭제 직후의 낡은 요청이 화면을 깨뜨리지 않게 한다.
    target_filename: str | None = None
    if asset_id:
        asset = next((a for a in p.studio_assets if a.asset_id == asset_id), None)
        if not asset:
            return {"available": True, "segments": [], "stale": False, "missing": False}
        target_filename = asset.filename

    from src.render_spec import preview_stages as _stages

    footage_dir = Path(p.footage_dir)
    # 파일별 판정은 한 번만 계산해 재사용 — segments 는 같은 파일을 여러 번 가리킨다.
    verdict_cache: dict[str, tuple[bool, bool]] = {}

    def _verdict(filename: str) -> tuple[bool, bool]:
        """(stale, missing) 판정. 색인 signature 와 현재 파일 실체를 비교한다."""
        if filename in verdict_cache:
            return verdict_cache[filename]
        cur = _stages.file_signature(footage_dir / filename)
        missing = bool(cur.get("missing"))
        recorded = raw_signatures.get(filename)
        if missing:
            stale = False  # 파일 자체가 없으면 stale 보다 missing 이 더 정확한 정보다.
        elif not isinstance(recorded, dict):
            # 색인에 시그니처가 없는 파일 → 변경 여부를 알 수 없으므로 stale 로 본다.
            stale = True
        else:
            stale = any(
                recorded.get(k) != cur.get(k) for k in ("name", "mtime", "size")
            )
        verdict_cache[filename] = (stale, missing)
        return stale, missing

    out: list[dict] = []
    any_stale = False
    any_missing = False
    for seg in raw_segments:
        if not isinstance(seg, dict):
            continue
        filename = str(seg.get("file") or "")
        if not filename:
            continue
        if target_filename is not None and filename != target_filename:
            continue
        stale, missing = _verdict(filename)
        item = dict(seg)
        if stale:
            item["stale"] = True
            any_stale = True
        if missing:
            item["missing"] = True
            any_missing = True
        out.append(item)

    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    return {
        "available": True,
        "meta": meta,
        "segments": out,
        # 필터링된 결과 안에 하나라도 섞여 있으면 참 — 프론트 경고 한 줄용.
        "stale": any_stale,
        "missing": any_missing,
    }
