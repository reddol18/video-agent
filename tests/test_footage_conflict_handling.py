"""footage 파일명 충돌 처리 — 접미사 포맷 2종 + 덮어쓰기 캐시 무효화 테스트.

가져오기에서 같은 파일명이 충돌할 때 "둘 다 유지(rename) / 덮어쓰기 / 건너뛰기" 를
사용자가 고른다. 여기서 검증하는 건 두 가지다.

  1) `resolve_footage_name` 이 접미사 포맷 파라미터에 따라 `_2` 와 ` (2)` 를 모두
     만들어내는가 — clip_library 는 `_2` 를 계속 써야 하므로 기본값 회귀가 없어야 한다.
  2) 덮어쓰기 시 프록시 캐시가 실제로 삭제되고 footage_index 청크가 제거되는가 —
     빠뜨리면 미리보기에 옛 영상이, 새 영상에 옛 AI 설명이 남는 조용한 버그가 된다.
"""
from __future__ import annotations

import json

from web.backend.footage_paths import (
    SUFFIX_PAREN,
    SUFFIX_UNDERSCORE,
    count_scene_references,
    normalize_policy,
    remove_index_chunks,
    resolve_footage_name,
)


# ── 1. 접미사 포맷 ─────────────────────────────────────────────────────────

def test_resolve_default_suffix_is_underscore(tmp_path):
    """기본값은 `_2` — clip_library 기존 동작 회귀 방지."""
    (tmp_path / "meme.mp4").write_bytes(b"x")
    assert resolve_footage_name(tmp_path, "meme.mp4").name == "meme_2.mp4"


def test_resolve_paren_suffix(tmp_path):
    """사용자 대면 가져오기는 OS 탐색기 관례인 `이름 (2).mp4` 를 쓴다."""
    (tmp_path / "영상.mp4").write_bytes(b"x")
    got = resolve_footage_name(tmp_path, "영상.mp4", SUFFIX_PAREN)
    assert got.name == "영상 (2).mp4"


def test_resolve_increments_until_free(tmp_path):
    """이미 (2) 까지 있으면 (3) 으로 — 두 포맷 모두 빈 자리를 찾을 때까지 증가."""
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "a (2).mp4").write_bytes(b"x")
    assert resolve_footage_name(tmp_path, "a.mp4", SUFFIX_PAREN).name == "a (3).mp4"

    (tmp_path / "b.mp4").write_bytes(b"x")
    (tmp_path / "b_2.mp4").write_bytes(b"x")
    assert resolve_footage_name(tmp_path, "b.mp4", SUFFIX_UNDERSCORE).name == "b_3.mp4"


def test_resolve_no_conflict_keeps_original_name(tmp_path):
    """충돌이 없으면 원래 이름 그대로 — 불필요한 접미사가 붙지 않아야 한다."""
    assert resolve_footage_name(tmp_path, "fresh.mp4", SUFFIX_PAREN).name == "fresh.mp4"


def test_resolve_strips_path_components(tmp_path):
    """경로가 섞여 들어와도 basename 만 사용 — 경로 조작 방지."""
    assert resolve_footage_name(tmp_path, "../../etc/x.mp4", SUFFIX_PAREN).name == "x.mp4"


def test_normalize_policy_defaults_to_skip():
    """미지정·오타는 하위호환상 skip 으로 떨어져야 한다."""
    assert normalize_policy("overwrite") == "overwrite"
    assert normalize_policy("rename") == "rename"
    assert normalize_policy(None) == "skip"
    assert normalize_policy("bogus") == "skip"


# ── 2. 덮어쓰기 캐시 무효화 ────────────────────────────────────────────────

def test_overwrite_deletes_proxy_cache(tmp_path, monkeypatch):
    """덮어쓰기 시 프록시가 삭제돼야 미리보기가 새 영상을 보여준다."""
    from web.backend import footage_paths
    from web.backend.routers import project as project_router

    cache_dir = tmp_path / "cache"
    (cache_dir / "proxies").mkdir(parents=True)
    proxy = cache_dir / "proxies" / "clip_proxy720.mp4"
    proxy.write_bytes(b"old proxy")

    monkeypatch.setattr(project_router.store, "get_cache_dir", lambda pid: cache_dir)
    monkeypatch.setattr(
        "web.backend.state.get_cache_dir", lambda pid: cache_dir, raising=False
    )

    assert proxy.exists()
    footage_paths.invalidate_footage_caches("proj-1", "clip.mp4")
    assert not proxy.exists(), "덮어쓰기 후에도 옛 프록시가 남아 있으면 미리보기가 옛 영상을 재생한다"


def test_remove_index_chunks_drops_matching_source_file(tmp_path):
    """해당 source_file 청크만 제거하고 나머지는 보존한다."""
    idx = tmp_path / "footage_index.json"
    idx.write_text(json.dumps({"chunks": [
        {"chunk_id": "c1", "source_file": "clip.mp4", "description": "옛 설명"},
        {"chunk_id": "c2", "source_file": "/abs/path/clip.mp4", "description": "옛 설명2"},
        {"chunk_id": "c3", "source_file": "other.mp4", "description": "유지"},
    ]}), encoding="utf-8")

    removed = remove_index_chunks(idx, ["clip.mp4"])

    assert removed == 2, "전체 경로로 기록된 source_file 도 basename 으로 매칭돼야 한다"
    kept = json.loads(idx.read_text(encoding="utf-8"))["chunks"]
    assert [c["chunk_id"] for c in kept] == ["c3"]


def test_remove_index_chunks_missing_file_is_noop(tmp_path):
    """인덱스가 아직 없으면 조용히 0 — 업로드를 실패시키면 안 된다."""
    assert remove_index_chunks(tmp_path / "nope.json", ["clip.mp4"]) == 0


# ── 3. 참조 씬 카운트 ──────────────────────────────────────────────────────

def test_count_scene_references_counts_clip_and_kenburns():
    """ClipEffect.file 과 KenburnsEffect.image 둘 다 세어야 한다."""
    from web.backend.models import ClipEffect, KenburnsEffect, SceneCard

    scenes = [
        SceneCard(scene_id=1, footage=ClipEffect(file="clip.mp4")),
        SceneCard(scene_id=2, footage=ClipEffect(file="other.mp4")),
        SceneCard(scene_id=3, footage=KenburnsEffect(image="clip.mp4")),
        SceneCard(scene_id=4, footage=None),
    ]

    assert count_scene_references(scenes, "clip.mp4") == 2
    assert count_scene_references(scenes, "other.mp4") == 1
    assert count_scene_references(scenes, "absent.mp4") == 0
