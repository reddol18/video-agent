"""footage 파일명 충돌 처리 공용 모듈.

프로젝트 `footage/` 폴더에 같은 이름의 파일이 들어올 때의 정책을 한 곳에 모은다.
호출부가 clip_library(하드링크 배치) · studio(파일 동기화) · project(업로드) 세 곳으로
흩어져 있어 각자 다른 방식으로 충돌을 처리하던 것을 통일했다.

접미사 포맷
──────────
`resolve_footage_name()` 은 회피 파일명 포맷을 파라미터로 받는다.

  - `SUFFIX_UNDERSCORE` (`name_2.mp4`)  — clip_library 기존 동작. 회귀 방지를 위해 유지.
  - `SUFFIX_PAREN`      (`name (2).mp4`) — 사용자 대면 가져오기의 기본. OS 파일 탐색기
                                           (macOS/Windows) 관례와 같아 김감독이 인지하기 쉽다.

덮어쓰기 시 캐시 무효화
──────────────────────
같은 이름으로 다른 영상을 덮어쓰면 파일명을 키로 삼는 파생 캐시가 전부 낡은 상태가
된다. `invalidate_footage_caches()` 가 이를 일괄 정리한다. 빠뜨리면 원본만 바뀌고
미리보기·AI 설명은 옛 영상 것이 남는 조용한 버그가 된다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable, Literal

# 충돌 정책 — 프론트 다이얼로그의 "둘 다 유지 / 덮어쓰기 / 건너뛰기" 와 1:1 대응.
ConflictPolicy = Literal["skip", "overwrite", "rename"]
CONFLICT_POLICIES: frozenset[str] = frozenset({"skip", "overwrite", "rename"})

# 접미사 포맷 — `stem`, `n` 을 받아 확장자를 뺀 파일명을 만든다.
SUFFIX_UNDERSCORE: Callable[[str, int], str] = lambda stem, n: f"{stem}_{n}"
SUFFIX_PAREN: Callable[[str, int], str] = lambda stem, n: f"{stem} ({n})"


def resolve_footage_name(
    footage_dir: Path,
    filename: str,
    suffix_fmt: Callable[[str, int], str] = SUFFIX_UNDERSCORE,
) -> Path:
    """`footage_dir` 안에서 충돌하지 않는 경로를 만든다.

    같은 이름이 이미 있으면 덮어쓰지 않고 `suffix_fmt` 로 만든 이름으로 회피한다.
    기존 씬이 그 파일명을 footage 로 참조 중일 수 있어 덮어쓰기는 조용한 영상
    교체 사고를 낸다.

    Args:
        footage_dir: 대상 폴더.
        filename: 원하는 파일명(경로가 섞여 있어도 basename 만 사용).
        suffix_fmt: 회피 이름 생성기. 기본은 `_2` (clip_library 기존 동작 호환).
                    사용자 대면 가져오기는 `SUFFIX_PAREN` 을 넘긴다.

    Returns:
        아직 존재하지 않는 파일 경로.
    """
    base = Path(filename).name
    stem, suffix = Path(base).stem, Path(base).suffix
    candidate = footage_dir / base
    n = 2
    while candidate.exists():
        candidate = footage_dir / f"{suffix_fmt(stem, n)}{suffix}"
        n += 1
    return candidate


def normalize_policy(value: str | None) -> ConflictPolicy:
    """Form 으로 들어온 문자열을 정책으로 정규화. 미지정·오타는 하위호환상 skip."""
    v = (value or "").strip().lower()
    return v if v in CONFLICT_POLICIES else "skip"  # type: ignore[return-value]


def remove_index_chunks(index_path: Path, filenames: Iterable[str]) -> int:
    """`footage_index.json` 에서 해당 source_file 의 청크를 제거한다.

    `src/footage_indexer.py` 의 증분 인덱싱은 source_file 이 이미 인덱스에 있으면
    재분석을 건너뛴다. 덮어쓴 뒤에도 옛 청크가 남아 있으면 새 영상에 옛 AI 설명·
    키워드가 그대로 붙으므로 반드시 제거해야 한다.

    source_file 은 전체 경로로 기록되기도 하므로 basename 으로 비교한다.

    Returns:
        제거한 청크 수 (파일이 없거나 형식이 깨졌으면 0).
    """
    targets = {Path(f).name for f in filenames}
    if not targets or not index_path.exists():
        return 0
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    # 인덱스는 {"chunks": [...]} 또는 최상위 배열 두 형태가 모두 유통된다.
    chunks = data.get("chunks") if isinstance(data, dict) else data
    if not isinstance(chunks, list):
        return 0

    kept = [c for c in chunks
            if not (isinstance(c, dict) and Path(str(c.get("source_file", ""))).name in targets)]
    removed = len(chunks) - len(kept)
    if removed == 0:
        return 0

    if isinstance(data, dict):
        data["chunks"] = kept
    else:
        data = kept
    try:
        tmp = index_path.with_suffix(index_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(index_path)
    except Exception:
        return 0
    return removed


def invalidate_footage_caches(project_id: str, filename: str) -> None:
    """덮어쓴 footage 의 파생 캐시를 무효화한다.

    - 프록시 영상(`cache/proxies/{stem}_proxy720.mp4`): 삭제. 남겨두면 미리보기가
      계속 옛 영상을 재생한다.
    - `cache/footage_index.json` 의 해당 청크: 삭제. 남겨두면 증분 인덱싱이 재분석을
      건너뛰어 옛 AI 설명이 새 영상에 붙는다.
    - 썸네일: 별도 처리 불필요 — 캐시 키에 mtime 이 포함되어 자동 무효화된다.

    캐시 정리 실패가 업로드 자체를 실패시키면 안 되므로 예외는 삼킨다.
    """
    # 순환 import 방지 — project 라우터가 이 모듈을 import 한다.
    from .routers.project import _proxy_path, _proxy_meta_path
    from . import state as store

    try:
        _proxy_path(project_id, filename).unlink(missing_ok=True)
        # 사이드카(원본 sig)도 함께 제거 — 남겨두면 다음 프록시 생성 전까지
        # 고아 메타가 쌓인다.
        _proxy_meta_path(project_id, filename).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        remove_index_chunks(store.get_cache_dir(project_id) / "footage_index.json", [filename])
    except Exception:
        pass


def count_scene_references(scenes: Iterable, filename: str) -> int:
    """`filename` 을 footage 로 참조 중인 씬 개수를 센다.

    씬이 파일명을 참조하는 지점은 두 곳이다:
      - `ClipEffect.file`      (영상)
      - `KenburnsEffect.image` (이미지 Ken Burns)
    덮어쓰기 경고에서 "이 파일을 쓰는 씬 N개가 함께 바뀝니다" 를 보여주는 데 쓴다.
    """
    target = Path(filename).name
    n = 0
    for s in scenes:
        fx = getattr(s, "footage", None)
        if fx is None:
            continue
        ref = getattr(fx, "file", None) or getattr(fx, "image", None)
        if ref and Path(str(ref)).name == target:
            n += 1
    return n
