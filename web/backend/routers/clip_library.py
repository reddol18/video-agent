"""커스텀 영상 클립 라이브러리 라우터.

밈영상 · 로딩 애니메이션처럼 여러 프로젝트에서 재사용하는 짧은 영상을 전역으로
보관하고, 씬 에디터에서 씬 삽입 시 즉시 지정할 수 있게 한다.

저장 위치: web/data/_cliplib/{clip_id}/
  - clip.json          — 메타데이터 (원자적 쓰기: .tmp + os.replace)
  - media.{ext}        — 실제 영상 파일
  - thumb.jpg          — 첫 프레임 썸네일 (lazy 생성)

`_cliplib` 폴더는 UUID 패턴이 아니므로 `state.load_all()` 의 `*/state.json` glob 에
걸리지 않는다 (`templates.py` 의 `_templates` 와 동일 규약). 별도 index 파일은 두지
않고 디렉토리 스캔으로 목록을 구성한다 — 이 코드베이스 컨벤션.

씬 지정 방식 — 하드링크
──────────────────────
라이브러리 클립을 씬에 지정할 때 `clip-lib:` 같은 prefix 참조를 쓰지 않는다.
경로 해석 지점이 40곳 이상이고, basename 우연 일치로 엉뚱한 영상이 조용히
렌더링되는 실패 모드가 있기 때문이다.

대신 지정 시점에 프로젝트의 `data/{project_id}/footage/` 로 `os.link()` 하드링크를
건다 (`OSError` 시 `shutil.copy2` 폴백). 씬 입장에서는 평범한 basename footage 이므로
렌더러 · 매처 · 프론트 URL 생성부를 일절 수정할 필요가 없다.

API:
  - POST   /api/clip-library                  — URL 다운로드 → 등록
  - GET    /api/clip-library                  — 목록
  - DELETE /api/clip-library/{clip_id}        — 삭제
  - GET    /api/clip-library/{clip_id}/stream — 미리보기 스트리밍
  - GET    /api/clip-library/{clip_id}/thumbnail — 썸네일
  - POST   /api/projects/{project_id}/scenes/{scene_id}/from-clip-library
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import state as store
from ..models import ClipLibraryEntry, ClipLibraryRegisterRequest, SceneFromClipLibraryRequest, SceneFromClipLibraryResponse
# `_ffprobe_duration` 은 project.py 의 기존 헬퍼를 재사용한다 (새로 만들지 않음).
# duration 메타가 없는 webm 도 패킷 pts 로 폴백 측정하는 검증된 구현.
from .project import _ffprobe_duration, _ffprobe_video_size

import sys as _sys_ffpath
_ROOT_FFPATH = Path(__file__).parent.parent.parent.parent
if str(_ROOT_FFPATH) not in _sys_ffpath.path:
    _sys_ffpath.path.insert(0, str(_ROOT_FFPATH))
from src.utils.ffmpeg_utils import ffmpeg_bin

router = APIRouter(prefix="/api/clip-library", tags=["clip-library"])

# 클립 라이브러리 저장 루트. templates.py 의 TEMPLATES_DIR 패턴과 동일.
CLIPLIB_DIR = store.DATA_DIR / "_cliplib"
CLIPLIB_DIR.mkdir(parents=True, exist_ok=True)

# 다운로드를 허용하는 영상/애니메이션 확장자.
_VIDEO_EXTS = {".mp4", ".webm", ".gif", ".mov", ".m4v", ".mkv", ".avi", ".apng"}

# 직접 다운로드 허용 Content-Type prefix. gif/apng 은 image/* 로 내려온다.
_DIRECT_CONTENT_TYPES = ("video/", "image/gif", "image/apng", "application/octet-stream")

# 단일 클립 최대 크기 (200MB). 밈/로딩 애니메이션 용도에 충분하며,
# 실수로 장편 영상을 등록해 디스크를 채우는 것을 막는다.
_MAX_BYTES = 200 * 1024 * 1024

# 다운로드 타임아웃(초).
_DOWNLOAD_TIMEOUT = 120.0


# ── 경로 가드 ──────────────────────────────────────────────────────────────

# clip_id 는 서버가 발급한 UUID4 만 허용. path traversal 원천 차단(화이트리스트 접근).
_CLIP_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _clip_dir(clip_id: str) -> Path:
    """clip_id → 클립 디렉토리. 형식이 어긋나면 404.

    `..`, `/`, `\\` 를 포함한 어떤 입력도 UUID 정규식을 통과하지 못한다.
    추가 방어로 resolve() 후 CLIPLIB_DIR 하위인지 재확인한다.
    """
    if not clip_id or not _CLIP_ID_RE.match(clip_id):
        raise HTTPException(404, "클립을 찾을 수 없습니다")
    d = (CLIPLIB_DIR / clip_id).resolve()
    try:
        d.relative_to(CLIPLIB_DIR.resolve())
    except ValueError:
        raise HTTPException(404, "클립을 찾을 수 없습니다")
    return d


def _meta_path(clip_id: str) -> Path:
    return _clip_dir(clip_id) / "clip.json"


def _load_clip(clip_id: str) -> ClipLibraryEntry:
    path = _meta_path(clip_id)
    if not path.exists():
        raise HTTPException(404, "클립을 찾을 수 없습니다")
    try:
        return ClipLibraryEntry(**json.loads(path.read_text(encoding="utf-8")))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"클립 메타 로드 실패: {e}")


def _dump_clip(entry: ClipLibraryEntry) -> None:
    """원자적 쓰기 — templates.py::_dump_template 와 동일 패턴."""
    path = _meta_path(entry.clip_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _media_path(clip_id: str, filename: str) -> Path:
    """메타의 filename 을 basename 으로 강제하여 클립 디렉토리 내부로 제한."""
    safe = Path(filename).name
    if not safe or safe in (".", ".."):
        raise HTTPException(404, "클립 파일을 찾을 수 없습니다")
    return _clip_dir(clip_id) / safe


# ── 다운로드 ───────────────────────────────────────────────────────────────

def _looks_like_direct_video(url: str) -> bool:
    """URL 경로 확장자로 직접 영상 링크 여부 판단.

    쿼리스트링은 제외하고 path 만 본다 (CDN 의 `?token=...` 대응).
    """
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return Path(unquote(path)).suffix.lower() in _VIDEO_EXTS


def _safe_stem(raw: str, fallback: str = "clip") -> str:
    """제목/URL → 파일명 stem. 영숫자·한글·-·_ 만 남기고 60자로 절단."""
    cleaned = re.sub(r"[^\w가-힣-]+", "_", (raw or "").strip(), flags=re.UNICODE).strip("_")
    return (cleaned or fallback)[:60]


def _download_direct(url: str, dest_dir: Path, title_hint: str) -> Path:
    """표준 HTTP 다운로드. yt-dlp 없이 동작해야 하는 경로.

    스트리밍으로 받으며 _MAX_BYTES 초과 시 중단하고 부분 파일을 지운다.
    """
    import httpx  # FastAPI 스택에 이미 포함된 의존성.

    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix not in _VIDEO_EXTS:
        suffix = ".mp4"
    dest = dest_dir / f"{_safe_stem(title_hint or Path(unquote(urlparse(url).path)).stem)}{suffix}"
    tmp = dest.with_suffix(dest.suffix + ".part")

    written = 0
    try:
        with httpx.stream(
            "GET", url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (video-agent clip-library)"},
        ) as resp:
            if resp.status_code >= 400:
                raise HTTPException(400, f"다운로드 실패 (HTTP {resp.status_code}): {url}")
            ctype = (resp.headers.get("content-type") or "").lower()
            # Content-Type 이 명확히 비영상(html 등)이면 플랫폼 페이지일 가능성이 높다.
            if ctype and not any(ctype.startswith(p) for p in _DIRECT_CONTENT_TYPES):
                raise HTTPException(
                    400,
                    f"영상 파일이 아닙니다 (Content-Type: {ctype}). "
                    "YouTube 등 플랫폼 링크라면 yt-dlp 설치가 필요합니다.",
                )
            with tmp.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    written += len(chunk)
                    if written > _MAX_BYTES:
                        raise HTTPException(400, f"영상이 너무 큽니다 (최대 {_MAX_BYTES // (1024*1024)}MB)")
                    f.write(chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"다운로드 실패: {e}") from e

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "다운로드된 파일이 비어 있습니다")
    os.replace(tmp, dest)
    return dest


_YTDLP_INSTALL_HINT = (
    "YouTube 등 플랫폼 링크를 등록하려면 yt-dlp 가 필요합니다. "
    "터미널에서 다음을 실행해 주세요:\n\n"
    '    uv pip install "video-agent[cliplib]"\n\n'
    "설치 후 서버를 재시작하면 플랫폼 링크 등록이 활성화됩니다. "
    "(.mp4 / .webm / .gif 처럼 영상 파일을 직접 가리키는 URL 은 yt-dlp 없이도 등록됩니다.)"
)


def _download_with_ytdlp(url: str, dest_dir: Path, title_hint: str) -> tuple[Path, str]:
    """yt-dlp 로 플랫폼 영상 다운로드. 미설치 시 안내 메시지와 함께 400.

    Returns:
        (다운로드된 파일 경로, yt-dlp 가 추출한 제목)
    """
    try:
        from yt_dlp import YoutubeDL  # 선택적 의존성 — [cliplib] extra.
    except ImportError:
        # 500 이 아니라 사용자가 조치 가능한 400 + 설치 안내.
        raise HTTPException(400, _YTDLP_INSTALL_HINT)

    outtmpl = str(dest_dir / "%(title).60s.%(ext)s")
    ydl_opts = {
        # mp4 컨테이너 우선 — 렌더 파이프라인 호환성이 가장 좋다.
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": _MAX_BYTES,
        "socket_timeout": 30,
        "restrictfilenames": True,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            info = ydl.sanitize_info(info)
            # 플레이리스트가 걸러지지 않은 경우 첫 항목 사용.
            if isinstance(info, dict) and info.get("entries"):
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise HTTPException(400, "다운로드 가능한 영상을 찾지 못했습니다")
                info = entries[0]
            path_str = ydl.prepare_filename(info)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"영상 다운로드 실패: {e}") from e

    downloaded = Path(path_str)
    if not downloaded.exists():
        # merge_output_format 로 확장자가 바뀐 경우 같은 stem 의 실제 파일을 찾는다.
        candidates = [p for p in dest_dir.iterdir()
                      if p.is_file() and p.stem == downloaded.stem and p.suffix.lower() in _VIDEO_EXTS]
        if not candidates:
            raise HTTPException(400, "다운로드된 파일을 찾을 수 없습니다")
        downloaded = candidates[0]
    resolved_title = (info.get("title") if isinstance(info, dict) else None) or title_hint or downloaded.stem
    return downloaded, resolved_title


def _make_thumbnail(media: Path, thumb: Path) -> bool:
    """첫 프레임 썸네일 생성. 실패해도 치명적이지 않으므로 bool 반환."""
    tmp = thumb.with_suffix(".jpg.tmp")
    cmd = [ffmpeg_bin(), "-y", "-i", str(media), "-vf", "thumbnail,scale=320:-2",
           "-frames:v", "1", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, thumb)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


# ── 하드링크 / 복사 ─────────────────────────────────────────────────────────

def link_or_copy(src: Path, dst: Path) -> str:
    """`src` 를 `dst` 로 하드링크한다. 실패 시 복사 폴백.

    하드링크는 디스크를 추가로 쓰지 않고 즉시 완료되므로 라이브러리 클립을 여러
    프로젝트에서 재사용할 때 유리하다. 다만 다음 경우 `OSError` 가 발생한다:
      - src 와 dst 가 다른 볼륨/파일시스템에 있음 (EXDEV)
      - 파일시스템이 하드링크를 지원하지 않음 (exFAT/FAT32, 일부 네트워크 마운트)
      - 링크 수 상한 초과 (EMLINK)
    이때 `shutil.copy2` 로 자동 폴백하여 기능은 항상 성공하도록 한다.

    Returns:
        "link" (하드링크 성공) 또는 "copy" (복사 폴백).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


# 공용 모듈로 승격 (`..footage_paths`). 라이브러리 클립 배치는 기존대로 `name_2.mp4`
# 접미사를 쓴다 — 기본 인자가 SUFFIX_UNDERSCORE 이므로 호출부 동작은 변하지 않는다.
from ..footage_paths import resolve_footage_name  # noqa: E402  (re-export)


# ── API ────────────────────────────────────────────────────────────────────

@router.post("", response_model=ClipLibraryEntry)
def register_clip(req: ClipLibraryRegisterRequest):
    """URL 을 받아 영상을 다운로드하고 라이브러리에 등록한다.

    - 직접 영상 URL(.mp4/.webm/.gif 등): 표준 HTTP 다운로드 (yt-dlp 불필요)
    - 그 외(YouTube 등 플랫폼 링크): yt-dlp — 미설치 시 설치 안내 400
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(400, "url 은 비어있을 수 없습니다")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "http/https URL 만 지원합니다")

    clip_id = str(uuid.uuid4())
    cdir = _clip_dir(clip_id)
    cdir.mkdir(parents=True, exist_ok=True)

    title = (req.title or "").strip()
    try:
        if _looks_like_direct_video(url):
            media = _download_direct(url, cdir, title)
            resolved_title = title or media.stem
        else:
            media, resolved_title = _download_with_ytdlp(url, cdir, title)
            resolved_title = title or resolved_title
    except Exception:
        shutil.rmtree(cdir, ignore_errors=True)
        raise

    size = media.stat().st_size
    duration = _ffprobe_duration(str(media)) or 0.0
    wh = _ffprobe_video_size(str(media))
    _make_thumbnail(media, cdir / "thumb.jpg")

    entry = ClipLibraryEntry(
        clip_id=clip_id,
        title=resolved_title or "제목 없음",
        category=(req.category or "").strip(),
        filename=media.name,
        source_url=url,
        duration_sec=round(float(duration), 3),
        width=wh[0] if wh else 0,
        height=wh[1] if wh else 0,
        size_bytes=size,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _dump_clip(entry)
    return entry


@router.get("", response_model=list[ClipLibraryEntry])
def list_clips():
    """등록된 클립 목록. 최신 등록 순."""
    if not CLIPLIB_DIR.exists():
        return []
    items: list[ClipLibraryEntry] = []
    for sub in CLIPLIB_DIR.iterdir():
        if not sub.is_dir():
            continue
        meta = sub / "clip.json"
        if not meta.exists():
            continue
        try:
            items.append(ClipLibraryEntry(**json.loads(meta.read_text(encoding="utf-8"))))
        except Exception as e:
            print(f"[clip-library] 손상된 클립 무시 {sub.name}: {e}", flush=True)
    items.sort(key=lambda x: x.created_at, reverse=True)
    return items


@router.delete("/{clip_id}")
def delete_clip(clip_id: str):
    """클립 삭제.

    이미 씬에 지정된 파일은 프로젝트 footage 로 하드링크/복사된 별도 엔트리이므로
    라이브러리를 지워도 기존 프로젝트 렌더에는 영향이 없다 (하드링크 방식의 이점).
    """
    cdir = _clip_dir(clip_id)
    if not cdir.exists():
        raise HTTPException(404, "클립을 찾을 수 없습니다")
    shutil.rmtree(cdir, ignore_errors=True)
    return {"deleted": True}


@router.get("/{clip_id}/stream")
def stream_clip(clip_id: str):
    """미리보기 스트리밍."""
    entry = _load_clip(clip_id)
    path = _media_path(clip_id, entry.filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "클립 파일을 찾을 수 없습니다")
    ext = path.suffix.lower()
    media_type = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
        ".mov": "video/quicktime", ".mkv": "video/x-matroska",
        ".gif": "image/gif", ".apng": "image/apng", ".avi": "video/x-msvideo",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@router.get("/{clip_id}/thumbnail")
def clip_thumbnail(clip_id: str):
    """썸네일. 없으면 lazy 생성 시도."""
    entry = _load_clip(clip_id)
    thumb = _clip_dir(clip_id) / "thumb.jpg"
    if not thumb.exists():
        media = _media_path(clip_id, entry.filename)
        if not media.exists() or not _make_thumbnail(media, thumb):
            raise HTTPException(404, "썸네일을 생성할 수 없습니다")
    return FileResponse(thumb, media_type="image/jpeg")


# ── 씬에 지정 ──────────────────────────────────────────────────────────────
#
# 라우터 prefix 가 `/api/clip-library` 라, 프로젝트 씬 경로는 별도 라우터로 마운트한다
# (templates.py 의 from_template_router 와 동일 구조).

scene_router = APIRouter(prefix="/api/projects", tags=["clip-library"])


@scene_router.post(
    "/{project_id}/scenes/{scene_id}/from-clip-library",
    response_model=SceneFromClipLibraryResponse,
)
def assign_clip_to_scene(project_id: str, scene_id: int, req: SceneFromClipLibraryRequest):
    """라이브러리 클립을 프로젝트 footage 로 하드링크한 뒤 해당 씬의 footage 로 지정한다.

    핵심: prefix 참조를 쓰지 않고 실제 파일을 프로젝트 footage 폴더에 만든다.
    따라서 씬 입장에서는 평범한 basename footage 이고, 렌더러/매처/프론트 URL
    생성부는 아무 변경 없이 그대로 동작한다.

    이전 match(source_file/trim_start/trim_end)는 새 footage 와 어긋나므로 함께
    클리어한다 (Editor.tsx::reassignSceneFootage 와 동일 정책). 그래야 렌더가 옛
    source_file 로 영상을 잘못 깔지 않는다.
    """
    p = store.get_project(project_id)
    if not p:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    scene = next((s for s in p.scenes if s.scene_id == scene_id), None)
    if not scene:
        raise HTTPException(404, f"씬 {scene_id} 를 찾을 수 없습니다")

    entry = _load_clip(req.clip_id)
    src = _media_path(req.clip_id, entry.filename)
    if not src.exists() or not src.is_file():
        raise HTTPException(404, "클립 파일을 찾을 수 없습니다")

    footage_dir = Path(p.footage_dir) if p.footage_dir else store.get_output_dir(project_id).parent / "footage"
    footage_dir.mkdir(parents=True, exist_ok=True)

    dst = resolve_footage_name(footage_dir, entry.filename)
    try:
        mode = link_or_copy(src, dst)
    except Exception as e:
        raise HTTPException(500, f"클립 배치 실패: {e}") from e

    filename = dst.name
    # 이미지(gif)는 kenburns, 영상은 clip — lib/footage.ts::makeFootageFromFilename 과 동일 규칙.
    ext = dst.suffix.lower()
    if ext in {".gif", ".png", ".jpg", ".jpeg", ".webp"}:
        from ..models import KenburnsEffect
        footage_effect = KenburnsEffect(image=filename)
    else:
        from ..models import ClipEffect
        footage_effect = ClipEffect(file=filename)

    duration = entry.duration_sec or (_ffprobe_duration(str(dst)) or 0.0)

    scene.footage = footage_effect
    scene.match = None
    if duration > 0:
        scene.duration_hint_sec = round(float(duration), 3)
    if not p.footage_dir:
        p.footage_dir = str(footage_dir)
    store._dump(p)

    print(f"[clip-library] scene {scene_id} ← {entry.title} ({mode}: {filename})", flush=True)
    return SceneFromClipLibraryResponse(
        filename=filename,
        duration_sec=round(float(duration), 3),
        mode=mode,
    )
