"""클립 라이브러리 — 하드링크/복사 폴백 단위 테스트.

라이브러리 클립을 씬에 지정할 때 `clip-lib:` prefix 참조 대신 프로젝트 footage
폴더로 실제 파일을 배치한다. 이 배치가 항상 성공해야 렌더러/매처를 건드리지 않고
기능이 성립하므로, 다음 두 가지를 검증한다.

  1) 같은 볼륨 → `os.link` 하드링크 성공 ("link"), 내용 동일 + inode 공유.
  2) `os.link` 가 OSError(EXDEV 등) → `shutil.copy2` 폴백 ("copy"), 내용 동일.

추가로 footage 폴더 내 이름 충돌 회피(덮어쓰기 금지)도 함께 확인한다.
"""
from __future__ import annotations

import os

import pytest

from web.backend.routers.clip_library import link_or_copy, resolve_footage_name


def test_link_or_copy_uses_hardlink_same_volume(tmp_path):
    """같은 볼륨에서는 하드링크를 사용한다 — 디스크 추가 소모 없음."""
    src = tmp_path / "meme.mp4"
    src.write_bytes(b"fake video payload")
    dst = tmp_path / "footage" / "meme.mp4"

    mode = link_or_copy(src, dst)

    assert mode == "link"
    assert dst.exists()
    assert dst.read_bytes() == b"fake video payload"
    # 하드링크이므로 동일 inode 를 공유한다.
    assert os.stat(src).st_ino == os.stat(dst).st_ino


def test_link_or_copy_falls_back_to_copy_on_oserror(tmp_path, monkeypatch):
    """os.link 가 OSError 면 shutil.copy2 로 폴백한다.

    실제 EXDEV(다른 볼륨)/exFAT 미지원 상황을 monkeypatch 로 재현. 폴백 후에도
    파일 내용이 온전해야 하고, 별개의 inode 여야 한다.
    """
    src = tmp_path / "loading.webm"
    src.write_bytes(b"webm payload")
    dst = tmp_path / "footage" / "loading.webm"

    def _boom(*args, **kwargs):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", _boom)

    mode = link_or_copy(src, dst)

    assert mode == "copy"
    assert dst.exists()
    assert dst.read_bytes() == b"webm payload"
    # 복사본이므로 inode 가 다르다.
    assert os.stat(src).st_ino != os.stat(dst).st_ino


def test_resolve_footage_name_avoids_collision(tmp_path):
    """이미 같은 이름이 있으면 덮어쓰지 않고 접미사로 회피한다.

    덮어쓰면 그 파일명을 참조하던 기존 씬의 영상이 조용히 바뀌는 사고가 난다.
    """
    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "meme.mp4").write_bytes(b"existing")

    resolved = resolve_footage_name(footage, "meme.mp4")

    assert resolved.name == "meme_2.mp4"
    # 기존 파일은 그대로 보존된다.
    assert (footage / "meme.mp4").read_bytes() == b"existing"

    # 두 번째 충돌도 계속 회피한다.
    resolved.write_bytes(b"second")
    assert resolve_footage_name(footage, "meme.mp4").name == "meme_3.mp4"


def test_resolve_footage_name_strips_directory_components(tmp_path):
    """경로 성분이 섞여 들어와도 basename 으로 강제되어 footage 폴더를 벗어나지 않는다."""
    footage = tmp_path / "footage"
    footage.mkdir()

    resolved = resolve_footage_name(footage, "../../etc/passwd.mp4")

    assert resolved.parent == footage
    assert resolved.name == "passwd.mp4"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
