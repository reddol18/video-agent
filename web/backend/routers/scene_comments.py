"""씬 편집 지시 코멘트 라우터.

김감독이 씬에 "여기 컷 더 짧게" 같은 편집 지시를 남기면 코멘트로 저장되고,
나중에 클로드 에이전트가 open 코멘트를 읽어 수정한 뒤 resolve 로 완료 처리한다.
완료된 코멘트는 삭제하지 않고 이력으로 남아 씬 카드에서 접힌 채 조회된다.

설계 메모:
  - 저장 위치는 SceneCard.comments (state.json 안). 별도 저장소를 두지 않아
    프로젝트 복제/템플릿/백업 경로를 그대로 탄다.
  - 상태는 open → done 단방향. 되돌리려면 reopen 을 명시적으로 호출한다.
  - 에이전트 소비용 조회는 GET /agent-queue 하나로 끝나도록 씬 문맥
    (제목/나레이션)까지 함께 실어 준다. 에이전트가 state.json 을 직접
    파싱할 필요가 없게 하기 위함.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import state as store
from ..models import CommentImage, SceneComment

router = APIRouter(prefix="/api/projects", tags=["scene-comments"])

MAX_BODY_LEN = 2000
MAX_NOTE_LEN = 1000

# 붙임 이미지 — 클립보드 붙여넣기 1장이 수 MB 를 넘는 경우는 드물다.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 확장자는 저장 파일명에만 쓰고, 판별은 클라이언트가 준 content-type 으로 한다.
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# 본문에 삽입되는 자리표시자. "[이미지12]" 같은 형태.
_PLACEHOLDER_RE = re.compile(r"\[이미지(\d+)\]")


def _images_dir(project_id: str) -> Path:
    """코멘트 붙임 이미지 저장 폴더. output/ 과 형제로 두어 백업 경로를 함께 탄다."""
    d = store.get_output_dir(project_id).parent / "comment_images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def placeholder(index: int) -> str:
    """본문에 삽입할 자리표시자 문자열."""
    return f"[이미지{index}]"


def referenced_indexes(body: str) -> list[int]:
    """본문이 실제로 참조하는 이미지 번호를 등장 순서대로(중복 제거) 돌려준다."""
    seen: list[int] = []
    for m in _PLACEHOLDER_RE.finditer(body or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_scene(project_id: str, scene_id: int):
    """프로젝트 + 씬을 함께 조회. 없으면 404."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, "씬을 찾을 수 없습니다")
    return p, scene


def _find_comment(scene, comment_id: str) -> SceneComment:
    c = next((c for c in scene.comments if c.id == comment_id), None)
    if not c:
        raise HTTPException(404, "코멘트를 찾을 수 없습니다")
    return c


def _persist(project_id: str, scene_id: int, comments: list[SceneComment]) -> None:
    """씬 코멘트 목록을 저장. update_scene 이 updated_at 갱신 + state.json 덤프까지 처리."""
    store.update_scene(project_id, scene_id, comments=comments)


# ── 요청 모델 ───────────────────────────────────────────────────────────────

class CreateCommentRequest(BaseModel):
    body: str
    # 업로드 응답(CommentImage)을 그대로 돌려보낸다. 본문의 [이미지N] 과 index 로 대응.
    images: list[CommentImage] = []


class UpdateCommentRequest(BaseModel):
    body: str
    # 생략(None)하면 기존 붙임 이미지를 유지한다. 배열을 주면 그대로 교체.
    images: Optional[list[CommentImage]] = None


class ResolveCommentRequest(BaseModel):
    """에이전트(또는 사용자)가 코멘트를 완료 처리할 때 보내는 페이로드."""
    resolution_note: Optional[str] = None
    resolved_by: Literal["agent", "user"] = "agent"


def _validate_body(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(400, "코멘트 내용이 비어 있습니다")
    if len(text) > MAX_BODY_LEN:
        raise HTTPException(400, f"코멘트는 {MAX_BODY_LEN}자를 넘을 수 없습니다")
    return text


# ── 씬 단위 CRUD ────────────────────────────────────────────────────────────

@router.get("/{project_id}/scenes/{scene_id}/comments")
def list_comments(project_id: str, scene_id: int) -> dict:
    """한 씬의 코멘트 전체(open + done). 생성 순서 유지."""
    _, scene = _get_scene(project_id, scene_id)
    return {"comments": scene.comments}


@router.post("/{project_id}/scenes/{scene_id}/comments")
def create_comment(project_id: str, scene_id: int, req: CreateCommentRequest) -> SceneComment:
    """씬에 편집 지시 코멘트를 남긴다."""
    _, scene = _get_scene(project_id, scene_id)
    comment = SceneComment(
        id=uuid.uuid4().hex,
        body=_validate_body(req.body),
        status="open",
        created_at=_now(),
        author="user",
        images=list(req.images),
    )
    _persist(project_id, scene_id, [*scene.comments, comment])
    return comment


@router.patch("/{project_id}/scenes/{scene_id}/comments/{comment_id}")
def update_comment(project_id: str, scene_id: int, comment_id: str, req: UpdateCommentRequest) -> SceneComment:
    """코멘트 본문 수정. 완료된 코멘트는 이력이므로 수정할 수 없다."""
    _, scene = _get_scene(project_id, scene_id)
    target = _find_comment(scene, comment_id)
    if target.status == "done":
        raise HTTPException(409, "완료된 코멘트는 수정할 수 없습니다")
    text = _validate_body(req.body)
    patch: dict = {"body": text}
    if req.images is not None:
        patch["images"] = list(req.images)
    updated = target.model_copy(update=patch)
    _persist(project_id, scene_id, [updated if c.id == comment_id else c for c in scene.comments])
    return updated


@router.delete("/{project_id}/scenes/{scene_id}/comments/{comment_id}")
def delete_comment(project_id: str, scene_id: int, comment_id: str) -> dict:
    """코멘트 삭제. 완료 이력까지 지우는 파괴적 동작이라 사용자 UI 에서만 노출한다."""
    _, scene = _get_scene(project_id, scene_id)
    target = _find_comment(scene, comment_id)
    # 코멘트가 사라지면 붙임 이미지는 참조하는 곳이 없다 — 임시 폴더에 남기지 않는다.
    for img in target.images:
        try:
            (_images_dir(project_id) / Path(img.filename).name).unlink(missing_ok=True)
        except Exception:
            pass  # 파일 정리 실패가 코멘트 삭제를 막지는 않는다
    _persist(project_id, scene_id, [c for c in scene.comments if c.id != comment_id])
    return {"ok": True, "deleted_id": comment_id}


# ── 붙임 이미지 ─────────────────────────────────────────────────────────────

@router.post("/{project_id}/scenes/{scene_id}/comment-images")
async def upload_comment_image(
    project_id: str,
    scene_id: int,
    file: UploadFile = File(...),
) -> CommentImage:
    """코멘트 작성 중 붙여넣은 이미지를 임시 폴더에 저장하고 자리표시자 번호를 부여한다.

    아직 코멘트가 만들어지기 전(작성 중)에 호출되므로 특정 코멘트에 매달지 않는다.
    프론트가 응답의 `index` 로 본문에 `[이미지N]` 을 끼워 넣고, 코멘트를 저장할 때
    images 배열을 함께 올린다.

    번호는 씬 단위로 1 부터 증가한다 — 이미 그 씬에 저장된 코멘트들이 쓴 최대 번호
    다음 값을 준다. 같은 씬에서 코멘트를 여러 번 남겨도 번호가 겹치지 않는다.
    """
    _, scene = _get_scene(project_id, scene_id)

    mime = (file.content_type or "").lower().split(";")[0].strip()
    if mime not in _MIME_EXT:
        raise HTTPException(400, f"지원하지 않는 이미지 형식입니다: {mime or '알 수 없음'}")

    content = await file.read()
    if not content:
        raise HTTPException(400, "업로드된 이미지가 비어 있습니다")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(400, f"이미지는 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 를 넘을 수 없습니다")

    used = [img.index for c in scene.comments for img in c.images]
    index = (max(used) + 1) if used else 1

    image_id = uuid.uuid4().hex
    filename = f"s{scene_id:03d}_{index:02d}_{image_id[:8]}{_MIME_EXT[mime]}"
    saved = _images_dir(project_id) / filename
    try:
        saved.write_bytes(content)
    except Exception as e:
        raise HTTPException(500, f"이미지 저장 오류: {e}") from e

    return CommentImage(
        id=image_id,
        index=index,
        filename=filename,
        path=str(saved.resolve()),
        url=f"/api/projects/{project_id}/comment-images/{filename}",
        mime=mime,
        size=len(content),
        created_at=_now(),
    )


@router.get("/{project_id}/comment-images/{filename}")
def get_comment_image(project_id: str, filename: str):
    """저장된 붙임 이미지를 그대로 반환. 브라우저 미리보기용."""
    # 경로 조작 방지 — 파일명만 취한다.
    safe = Path(filename).name
    target = _images_dir(project_id) / safe
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "이미지를 찾을 수 없습니다")
    return FileResponse(target)


# ── 완료 처리 ───────────────────────────────────────────────────────────────

@router.post("/{project_id}/scenes/{scene_id}/comments/{comment_id}/resolve")
def resolve_comment(project_id: str, scene_id: int, comment_id: str, req: ResolveCommentRequest) -> SceneComment:
    """코멘트를 완료 처리한다. 에이전트가 수정 작업을 마친 뒤 호출하는 엔드포인트.

    이미 done 인 코멘트에 다시 호출하면 409 — 중복 처리로 이력이 덮여
    "언제 누가 고쳤는지" 가 흐려지는 것을 막는다.
    """
    _, scene = _get_scene(project_id, scene_id)
    target = _find_comment(scene, comment_id)
    if target.status == "done":
        raise HTTPException(409, "이미 완료된 코멘트입니다")

    note = (req.resolution_note or "").strip() or None
    if note and len(note) > MAX_NOTE_LEN:
        raise HTTPException(400, f"처리 메모는 {MAX_NOTE_LEN}자를 넘을 수 없습니다")

    updated = target.model_copy(update={
        "status": "done",
        "resolved_at": _now(),
        "resolved_by": req.resolved_by,
        "resolution_note": note,
    })
    _persist(project_id, scene_id, [updated if c.id == comment_id else c for c in scene.comments])
    return updated


@router.post("/{project_id}/scenes/{scene_id}/comments/{comment_id}/reopen")
def reopen_comment(project_id: str, scene_id: int, comment_id: str) -> SceneComment:
    """완료 처리를 되돌린다. 수정 결과가 만족스럽지 않을 때 사용."""
    _, scene = _get_scene(project_id, scene_id)
    target = _find_comment(scene, comment_id)
    if target.status == "open":
        raise HTTPException(409, "이미 열린 코멘트입니다")
    updated = target.model_copy(update={
        "status": "open",
        "resolved_at": None,
        "resolved_by": None,
        "resolution_note": None,
    })
    _persist(project_id, scene_id, [updated if c.id == comment_id else c for c in scene.comments])
    return updated


# ── 에이전트 소비용 조회 ────────────────────────────────────────────────────

@router.get("/{project_id}/comments")
def list_project_comments(project_id: str, status: Optional[Literal["open", "done"]] = None) -> dict:
    """프로젝트 전체 코멘트를 씬 순서대로 평탄화해 반환. status 로 필터 가능."""
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    items = [
        {"scene_id": s.scene_id, **c.model_dump()}
        for s in p.scenes
        for c in s.comments
        if status is None or c.status == status
    ]
    return {
        "project_id": project_id,
        "total": len(items),
        "open_count": sum(1 for i in items if i["status"] == "open"),
        "comments": items,
    }


@router.get("/{project_id}/comments/agent-queue")
def agent_queue(project_id: str) -> dict:
    """클로드 에이전트가 처리할 open 코멘트 목록 — 씬 문맥 포함.

    에이전트는 이 응답 하나만 보고 "어느 씬의 무엇을 고쳐야 하는지" 를 알 수 있고,
    작업 후 resolve_url 로 POST 하면 완료 이력이 남는다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")

    tasks = []
    for s in p.scenes:
        for c in s.comments:
            if c.status != "open":
                continue
            by_index = {img.index: img for img in c.images}
            refs = referenced_indexes(c.body)
            tasks.append({
                "comment_id": c.id,
                "scene_id": s.scene_id,
                "instruction": c.body,
                "created_at": c.created_at,
                # 본문의 [이미지N] → 실제 파일 매칭. 에이전트는 path 를 그대로 열면 된다.
                "images": [
                    {
                        "placeholder": placeholder(n),
                        "index": n,
                        "path": by_index[n].path,
                        "url": by_index[n].url,
                        "mime": by_index[n].mime,
                    }
                    for n in refs
                    if n in by_index
                ],
                # 본문이 참조하지 않고 매달려만 있는 이미지(사용자가 자리표시자를 지운 경우).
                "unreferenced_images": [
                    {"index": img.index, "path": img.path, "url": img.url}
                    for img in c.images
                    if img.index not in refs
                ],
                # 본문은 참조하는데 파일이 없는 번호 — 깨진 참조를 에이전트가 알 수 있게.
                "missing_image_indexes": [n for n in refs if n not in by_index],
                "scene_context": {
                    "title": s.title,
                    "narration_text": s.narration_text,
                    "duration_hint_sec": s.duration_hint_sec,
                    "tags": s.tags,
                },
                "resolve_url": (
                    f"/api/projects/{project_id}/scenes/{s.scene_id}"
                    f"/comments/{c.id}/resolve"
                ),
            })
    return {"project_id": project_id, "project_name": p.name, "pending": len(tasks), "tasks": tasks}
