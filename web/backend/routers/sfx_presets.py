"""SFX 프리셋 라우터 — 효과음 카탈로그 노출 + 스트리밍 + 사용자 공용 라이브러리.

기존 studio_assets(=프로젝트별 업로드 오디오)와 별개로, 모든 프로젝트가 공유하는
효과음 두 종류를 다룬다.
  1) 앱 내장 기본 효과음 프리셋(assets/sfx_presets, 읽기 전용, #418).
  2) 사용자가 메인 화면에서 직접 올린 효과음 라이브러리(`user-` key, 업로드/이름변경/삭제).
둘 다 오버레이에서는 `preset:{key}` 로 참조되며, 렌더 시 해석은 `render` 라우터의
`_resolve_sfx()` → `get_preset_file_path()` 가 담당한다.
"""

from __future__ import annotations

import mimetypes
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.sfx_presets import (
    USER_SFX_DIR,
    USER_SFX_PREFIX,
    SfxPresetEntry,
    get_preset_file_path,
    load_sfx_presets,
    load_user_sfx,
    read_user_sfx_manifest,
    write_user_sfx_manifest,
)

router = APIRouter(prefix="/api/sfx-presets", tags=["sfx-presets"])

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}


@router.get("")
def list_presets() -> dict[str, list[SfxPresetEntry]]:
    """전체 목록 — 사용자 라이브러리(최근 순) + 내장 기본 효과음. SFX 선택 UI 가 사용."""
    return {"presets": load_user_sfx() + load_sfx_presets()}


@router.get("/{preset_key}/stream")
def stream_preset(preset_key: str) -> FileResponse:
    """효과음 단일 스트리밍. 프론트 미리듣기 + 미리보기 `<audio>` 재생용."""
    path = get_preset_file_path(preset_key)
    if not path:
        raise HTTPException(404, f"preset not found: {preset_key}")
    # manifest 에는 있지만 파일이 누락된 경우(부분 동기화) — 정확한 사유로 404.
    if not Path(path).exists():
        raise HTTPException(404, f"preset file missing on disk: {preset_key}")
    media_type = mimetypes.guess_type(path)[0] or "audio/wav"
    return FileResponse(path, media_type=media_type)


# ── 사용자 공용 효과음 라이브러리 ─────────────────────────────────────────────

def _probe_duration(path: Path) -> float:
    try:
        from src.utils.ffmpeg_utils import ffprobe_bin
        out = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return round(float(out.stdout.strip()), 3)
    except Exception:
        return 0.0


@router.post("/user")
async def upload_user_sfx(
    files: list[UploadFile] = File(...),
    label: str = Form(""),
) -> dict:
    """효과음 파일 업로드(여러 개 가능). label 은 파일이 1개일 때만 적용, 기본은 파일 이름."""
    USER_SFX_DIR.mkdir(parents=True, exist_ok=True)
    items = read_user_sfx_manifest()
    added: list[dict] = []
    skipped: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for f in files:
        orig = f.filename or "sfx"
        ext = Path(orig).suffix.lower()
        if ext not in AUDIO_EXTS:
            skipped.append(f"{orig} (오디오 형식 아님)")
            continue
        content = await f.read()
        if not content:
            skipped.append(f"{orig} (빈 파일)")
            continue
        key = f"{USER_SFX_PREFIX}{uuid.uuid4().hex[:8]}"
        fname = f"{key}{ext}"
        dst = USER_SFX_DIR / fname
        dst.write_bytes(content)
        item = {
            "key": key,
            "label": (label.strip() if (label.strip() and len(files) == 1) else Path(orig).stem),
            "file": fname,
            "original_name": orig,
            "duration": _probe_duration(dst),
            "created_at": now,
        }
        items.append(item)
        added.append(item)
    if added:
        write_user_sfx_manifest(items)
    return {"added": added, "skipped": skipped}


class RenameUserSfxRequest(BaseModel):
    label: str


@router.patch("/user/{key}")
def rename_user_sfx(key: str, req: RenameUserSfxRequest) -> dict:
    items = read_user_sfx_manifest()
    for it in items:
        if it.get("key") == key:
            it["label"] = req.label.strip()
            write_user_sfx_manifest(items)
            return it
    raise HTTPException(404, f"효과음을 찾을 수 없습니다: {key}")


@router.delete("/user/{key}")
def delete_user_sfx(key: str) -> dict:
    """라이브러리에서 제거 + 파일 삭제. 이 효과음을 쓰던 오버레이는 렌더 시 소리 없이 지나간다."""
    items = read_user_sfx_manifest()
    target = next((it for it in items if it.get("key") == key), None)
    if not target:
        raise HTTPException(404, f"효과음을 찾을 수 없습니다: {key}")
    write_user_sfx_manifest([it for it in items if it.get("key") != key])
    fname = target.get("file")
    if isinstance(fname, str) and fname:
        try:
            (USER_SFX_DIR / fname).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": True}
