"""템플릿 라우터 (#406/#407).

영상 구조(씬 배치 + 자막 + 오버레이 + BGM)를 원본 영상 없이 보존·재사용한다.

저장 위치: web/data/_templates/{template_id}/template.json
  - `_templates` 폴더는 UUID 패턴이 아니므로 `state.load_all()` 의 `*/state.json`
    glob 에 걸리지 않는다 (자연 회귀 안전). 그래도 명시적으로 폴더명을 분리해
    프로젝트 목록과 절대 섞이지 않게 한다.

API:
  - POST /api/templates                        — 프로젝트 → 템플릿 변환
  - GET  /api/templates                        — 템플릿 목록 (메타만)
  - GET  /api/templates/{template_id}          — 단일 템플릿 상세
  - DELETE /api/templates/{template_id}        — 템플릿 삭제
  - POST /api/projects/from-template           — 템플릿 → 새 프로젝트 생성
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import state as store
from ..models import (
    CreateProjectFromTemplateRequest,
    CreateTemplateRequest,
    NarrationSegment,
    Project,
    SceneCard,
    Template,
    TemplateBgmFile,
    TemplateMeta,
    TtsMeta,
)
from ..models import BgmTrack, FootageAsset

router = APIRouter(prefix="/api/templates", tags=["templates"])

# 템플릿 저장 루트. state.DATA_DIR 와 같은 web/data/ 안의 `_templates/` 하위 폴더.
# 폴더 이름이 underscore 로 시작하여 일반 프로젝트 UUID 디렉토리와 시각적으로 분리된다.
TEMPLATES_DIR = store.DATA_DIR / "_templates"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


def _template_dir(template_id: str) -> Path:
    d = TEMPLATES_DIR / template_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _template_path(template_id: str) -> Path:
    return _template_dir(template_id) / "template.json"


def _scene_to_template_scene(scene: SceneCard) -> SceneCard:
    """프로젝트 SceneCard → 템플릿용 SceneCard.

    원본 영상에 의존하는 필드를 제거:
      - footage          → None (사용자가 새 프로젝트에서 업로드)
      - match            → None
      - tts              → None (WAV 미포함)
      - narration_segments[*].audio_url/status/synced/duration_sec → 초기화
        (text/offset_sec/voice_name/sbv_text/id 는 보존)
    """
    cloned = scene.model_copy(deep=True)
    cloned.footage = None
    cloned.match = None
    cloned.tts = None
    # narration_segments 텍스트만 보존, WAV 메타는 초기화.
    new_segs: list[NarrationSegment] = []
    for seg in cloned.narration_segments or []:
        new_segs.append(NarrationSegment(
            id=seg.id,
            text=seg.text,
            offset_sec=seg.offset_sec,
            audio_url="",
            duration_sec=0.0,
            status="idle",
            synced=False,
            voice_name=seg.voice_name,
            sbv_text=seg.sbv_text,
            error_message=None,
        ))
    cloned.narration_segments = new_segs
    return cloned


def _project_to_template(p: Project, *, template_id: str, title: str, description: str) -> Template:
    now = datetime.now(timezone.utc).isoformat()
    return Template(
        template_id=template_id,
        title=title,
        description=description,
        source_project_id=p.project_id,
        created_at=now,
        scenes=[_scene_to_template_scene(s) for s in p.scenes],
        output_width=p.output_width,
        output_height=p.output_height,
        tts_voice=p.tts_voice,
        gemini_model=p.gemini_model or "gemini-2.5-flash",
        # bgm_tracks 의 audio_asset_id 는 studio_assets(프로젝트별 자산) 참조라 새 프로젝트에서 깨질 수 있지만,
        # 데이터 자체는 보존하여 사용자가 새 프로젝트에서 BGM 자산을 다시 매핑하면
        # 그대로 동작하도록 한다 (BGM 정의는 보존).
        bgm_tracks=list(p.bgm_tracks or []),
        force_split_scene_dissolve=p.force_split_scene_dissolve,
    )


def _template_bgm_dir(template_id: str) -> Path:
    """템플릿이 들고 다니는 BGM 오디오 보관 폴더 (#419 후속)."""
    return TEMPLATES_DIR / template_id / "bgm"


def _copy_project_bgm_into_template(p: Project, template_id: str) -> dict[str, TemplateBgmFile]:
    """프로젝트 bgm_tracks 가 참조하는 audio studio_asset 파일을 템플릿 bgm/ 폴더로 복사.

    반환: {audio_asset_id: TemplateBgmFile}. 자산/파일을 못 찾은 트랙은 포함하지 않는다
    (그 트랙은 audio_asset_id 를 그대로 보존 — 기존 동작).
    """
    import shutil
    out: dict[str, TemplateBgmFile] = {}
    if not p.bgm_tracks or not p.footage_dir:
        return out
    footage_dir = Path(p.footage_dir)
    asset_by_id = {a.asset_id: a for a in (p.studio_assets or [])}
    bgm_dir = _template_bgm_dir(template_id)
    for tr in p.bgm_tracks:
        aid = tr.audio_asset_id
        if aid in out:
            continue
        asset = asset_by_id.get(aid)
        if not asset or asset.asset_type != "audio":
            continue
        src = footage_dir / asset.filename
        if not src.is_file():
            continue
        name = Path(asset.filename).name  # 하위경로 차단
        bgm_dir.mkdir(parents=True, exist_ok=True)
        dst = bgm_dir / name
        if dst.exists():
            dst = bgm_dir / f"{uuid.uuid4().hex[:8]}_{name}"
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            print(f"[templates] BGM 복사 실패 {src}: {e}", flush=True)
            continue
        out[aid] = TemplateBgmFile(
            filename=dst.name,
            label=asset.label or Path(name).stem,
            original_filename=asset.filename,
        )
    return out


def _restore_template_bgm(
    tpl: Template, footage_dir: Path, now: str,
) -> tuple[list[FootageAsset], list[BgmTrack]]:
    """템플릿 bgm/ 의 파일을 새 프로젝트 footage 폴더로 복사·audio 자산 등록.

    audio-upload 엔드포인트(studio.py)와 같은 FootageAsset 형태(asset_type="audio",
    source="recorded")로 만든다. 반환: (새 studio_assets, audio_asset_id 를 치환한 bgm_tracks).
    bgm/ 에 파일이 없는 트랙은 그대로 보존한다.
    """
    import shutil
    tracks = [t.model_copy(deep=True) for t in (tpl.bgm_tracks or [])]
    if not tpl.bgm_files:
        return [], tracks
    bgm_dir = _template_bgm_dir(tpl.template_id)
    id_map: dict[str, str] = {}
    assets: list[FootageAsset] = []
    for old_id, info in tpl.bgm_files.items():
        name = Path(info.filename).name
        src = bgm_dir / name
        if not name or not src.is_file():
            continue
        dst = footage_dir / name
        if dst.exists():
            dst = footage_dir / f"{uuid.uuid4().hex[:8]}_{name}"
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            print(f"[templates] BGM 복원 실패 {src}: {e}", flush=True)
            continue
        asset = FootageAsset(
            asset_id=str(uuid.uuid4()),
            filename=dst.name,
            label=info.label or Path(name).stem,
            asset_type="audio",
            source="recorded",
            created_at=now,
        )
        assets.append(asset)
        id_map[old_id] = asset.asset_id
    for t in tracks:
        if t.audio_asset_id in id_map:
            t.audio_asset_id = id_map[t.audio_asset_id]
    return assets, tracks


def _load_template(template_id: str) -> Template:
    path = _template_path(template_id)
    if not path.exists():
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Template(**data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"템플릿 로드 실패: {e}")


def _dump_template(tpl: Template) -> None:
    path = _template_path(tpl.template_id)
    tmp = path.with_suffix(".json.tmp")
    payload = tpl.model_dump_json(indent=2)
    tmp.write_text(payload, encoding="utf-8")
    import os
    os.replace(tmp, path)


# ── API ────────────────────────────────────────────────────────────────────

@router.post("", response_model=TemplateMeta)
def create_template(req: CreateTemplateRequest):
    """프로젝트를 템플릿으로 저장한다.

    원본 영상(footage/match)과 TTS WAV 메타는 제외하고 구조만 보존.
    """
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(400, "title 은 비어있을 수 없습니다")
    p = store.get_project(req.source_project_id)
    if not p:
        raise HTTPException(404, "원본 프로젝트를 찾을 수 없습니다")

    template_id = str(uuid.uuid4())
    tpl = _project_to_template(p, template_id=template_id, title=title, description=req.description or "")
    # BGM 오디오 파일을 템플릿이 직접 보관 — 새 프로젝트에서 자동 재연결.
    tpl.bgm_files = _copy_project_bgm_into_template(p, template_id)
    _dump_template(tpl)
    return TemplateMeta(
        template_id=tpl.template_id,
        title=tpl.title,
        description=tpl.description,
        created_at=tpl.created_at,
        source_project_id=tpl.source_project_id,
        scene_count=len(tpl.scenes),
    )


@router.get("", response_model=list[TemplateMeta])
def list_templates():
    """저장된 템플릿 목록 (메타만)."""
    if not TEMPLATES_DIR.exists():
        return []
    items: list[TemplateMeta] = []
    for sub in TEMPLATES_DIR.iterdir():
        if not sub.is_dir():
            continue
        tpl_path = sub / "template.json"
        if not tpl_path.exists():
            continue
        try:
            data = json.loads(tpl_path.read_text(encoding="utf-8"))
            items.append(TemplateMeta(
                template_id=data.get("template_id", sub.name),
                title=data.get("title", ""),
                description=data.get("description", ""),
                created_at=data.get("created_at", ""),
                source_project_id=data.get("source_project_id", ""),
                scene_count=len(data.get("scenes", []) or []),
            ))
        except Exception as e:
            print(f"[templates] 손상된 템플릿 무시 {sub.name}: {e}", flush=True)
    # 최신 created_at 우선 (역순)
    items.sort(key=lambda x: x.created_at, reverse=True)
    return items


@router.get("/{template_id}", response_model=Template)
def get_template(template_id: str):
    """단일 템플릿 상세."""
    return _load_template(template_id)


@router.delete("/{template_id}")
def delete_template(template_id: str):
    """템플릿 삭제."""
    tdir = TEMPLATES_DIR / template_id
    if not tdir.exists():
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    import shutil
    shutil.rmtree(tdir, ignore_errors=True)
    return {"deleted": True}


# ── 템플릿 → 새 프로젝트 ────────────────────────────────────────────────────

# 라우터 prefix 가 `/api/templates` 라 from-template 경로를 같은 라우터에 두면
# `/api/templates/from-template` 가 되어 의미가 흐려진다. 별도 라우터를 두어
# `/api/projects/from-template` 로 노출한다.

from_template_router = APIRouter(prefix="/api/projects", tags=["templates"])


@from_template_router.post("/from-template", response_model=Project)
def create_project_from_template(req: CreateProjectFromTemplateRequest):
    """템플릿에서 새 프로젝트를 생성한다.

    동작:
      1) 새 project_id 발급, footage_dir = web/data/{new_id}/footage/ 자동 생성.
      2) 템플릿의 scenes 를 deep copy 하여 새 프로젝트에 주입. (footage/match/tts 는 이미 클리어된 상태)
      3) BGM 트랙·해상도·TTS 음성·Gemini 모델은 그대로 복제.
         템플릿 bgm/ 에 보관된 BGM 파일은 footage 폴더로 복사해 studio_assets(audio)로
         등록하고 트랙 audio_asset_id 를 새 asset_id 로 바꾼다 (#419 후속).
      4) studio_assets 는 위 BGM 자산만, cost_estimate / scenario_text 는 빈 상태로 시작.

    Returns:
        새로 생성된 Project (state.json 저장 완료된 상태).
    """
    tpl = _load_template(req.template_id)

    # 새 프로젝트 메타.
    name = (req.title or "").strip() or f"[템플릿] {tpl.title}"
    now = datetime.now(timezone.utc).isoformat()
    new_id = str(uuid.uuid4())
    footage_dir = store.DATA_DIR / new_id / "footage"
    footage_dir.mkdir(parents=True, exist_ok=True)

    # 씬 deep copy — 템플릿에서 이미 footage/match/tts 가 클리어되어 있지만,
    # 안전을 위해 한 번 더 _scene_to_template_scene 동일 정제 후 사용.
    new_scenes = [_scene_to_template_scene(s) for s in tpl.scenes]

    # 템플릿 bgm/ 파일 → footage 폴더 복사 + audio 자산 등록 + 트랙 참조 치환.
    bgm_assets, bgm_tracks = _restore_template_bgm(tpl, footage_dir, now)

    new_project = Project(
        project_id=new_id,
        name=name,
        footage_dir=str(footage_dir),
        scenario_text="",
        status="setup",
        scenes=new_scenes,
        tts_voice=tpl.tts_voice,
        created_at=now,
        updated_at=now,
        studio_assets=bgm_assets,
        output_width=tpl.output_width,
        output_height=tpl.output_height,
        bgm_tracks=bgm_tracks,
        force_split_scene_dissolve=tpl.force_split_scene_dissolve,
        gemini_model=tpl.gemini_model or "gemini-2.5-flash",
    )
    store._projects[new_id] = new_project
    store._dump(new_project)
    return new_project
