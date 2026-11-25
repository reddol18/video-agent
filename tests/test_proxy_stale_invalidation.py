"""`_ensure_proxy` 의 stale 프록시 재사용 방지 테스트.

프록시는 원본 footage 의 720p 재인코딩본이다. 예전에는 "프록시 파일이 존재하고
ffprobe 로 읽히기만 하면" 그대로 재사용했기 때문에, 써드파티 세션이나 직접 편집으로
원본이 바뀌어도 미리보기는 옛 영상을 계속 재생했다. (최종 미리보기 캐시는 캐시 키에
원본 sig 를 넣어 정상 무효화되지만, 재생성의 *입력* 인 프록시가 낡아 반쪽만 동작.)

여기서 지키는 계약 3가지:
  1. 원본이 안 바뀌면 캐시 적중 — 재인코딩 없음. (성능 회귀 방지: 씬 전환 미리보기)
  2. 원본 mtime/size 가 바뀌면 프록시 재생성.        ← 이 수정의 핵심
  3. 깨진 프록시(ffprobe 실패)는 기존대로 재생성.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from web.backend.routers import project as project_router


class _FakeProject:
    def __init__(self, footage_dir: Path):
        self.footage_dir = str(footage_dir)


@pytest.fixture
def proxy_env(tmp_path, monkeypatch):
    """프록시 생성을 흉내내는 환경. 실제 ffmpeg/ffprobe 는 실행하지 않는다.

    반환 객체의 `encodes` 에 프록시 재인코딩 횟수가 쌓인다.
    """
    footage_dir = tmp_path / "footage"
    footage_dir.mkdir()
    cache_dir = tmp_path / "cache"
    src = footage_dir / "clip.mp4"
    src.write_bytes(b"original video bytes")

    state = {"encodes": 0, "probe_ok": True}

    monkeypatch.setattr(project_router.store, "get_cache_dir", lambda pid: cache_dir)
    monkeypatch.setattr(
        project_router.store, "get_project", lambda pid: _FakeProject(footage_dir)
    )
    # 해상도 조회는 스킵(스케일 필터 불필요 경로)
    monkeypatch.setattr(project_router, "_ffprobe_video_size", lambda path: None)
    # 프록시 길이 검증 — probe_ok 를 내리면 "깨진 프록시" 를 흉내낸다.
    monkeypatch.setattr(
        project_router,
        "_ffprobe_duration",
        lambda path: 3.0 if state["probe_ok"] else None,
    )

    def _fake_run(cmd, *a, **kw):
        """ffmpeg 호출을 가로채 tmp 출력만 만들어 준다."""
        assert cmd[0] == "ffmpeg", f"예상치 못한 외부 호출: {cmd[0]}"
        state["encodes"] += 1
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"proxy bytes %d" % state["encodes"])

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    monkeypatch.setattr(project_router.subprocess, "run", _fake_run)

    class _Env:
        pass

    env = _Env()
    env.src = src
    env.cache_dir = cache_dir
    env.state = state
    env.proxy = project_router._proxy_path("proj-1", "clip.mp4")
    env.meta = project_router._proxy_meta_path("proj-1", "clip.mp4")
    return env


def test_unchanged_source_hits_proxy_cache(proxy_env):
    """원본 불변 → 재인코딩 없이 캐시 적중.

    이게 깨지면 씬을 넘길 때마다 프록시를 새로 인코딩해 미리보기가 다시 느려진다.
    """
    first = project_router._ensure_proxy("proj-1", "clip.mp4")
    assert first is not None
    assert proxy_env.state["encodes"] == 1
    assert proxy_env.meta.exists(), "사이드카(원본 sig)가 기록돼야 다음 호출이 대조할 수 있다"

    for _ in range(3):
        again = project_router._ensure_proxy("proj-1", "clip.mp4")
        assert again == first

    assert proxy_env.state["encodes"] == 1, (
        "원본이 그대로인데 프록시를 다시 인코딩했다 — 미리보기 성능 회귀"
    )


def test_changed_source_regenerates_proxy(proxy_env):
    """원본 mtime/size 변경 → 프록시 재생성. (이 수정의 핵심)

    써드파티 세션이 footage 를 고치면 사용자는 새 영상을 봐야 한다.
    """
    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.state["encodes"] == 1
    old_bytes = proxy_env.proxy.read_bytes()

    # 원본을 외부에서 교체 — 내용(size)과 mtime 이 모두 바뀐 상황.
    proxy_env.src.write_bytes(b"THIRD PARTY EDITED video bytes, longer")
    st = proxy_env.src.stat()
    os.utime(proxy_env.src, (st.st_atime + 120, st.st_mtime + 120))

    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.state["encodes"] == 2, (
        "원본이 바뀌었는데 옛 프록시를 재사용했다 — 사용자가 옛 영상을 계속 본다"
    )
    assert proxy_env.proxy.read_bytes() != old_bytes

    # 재생성 후에는 다시 캐시가 적중해야 한다(매번 재인코딩 금지).
    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.state["encodes"] == 2


def test_missing_sidecar_regenerates_proxy(proxy_env):
    """메타가 없으면 대조 불가 → 보수적으로 재생성.

    이 수정 이전 버전이 만들어 둔 프록시(사이드카 없음)가 여기 해당한다.
    낡은 것을 쓰느니 다시 만든다.
    """
    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.state["encodes"] == 1

    proxy_env.meta.unlink()

    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.state["encodes"] == 2
    assert proxy_env.meta.exists()


def test_corrupt_proxy_regenerates(proxy_env):
    """깨진 프록시(ffprobe 실패) → 삭제 후 재생성. 기존 동작 유지."""
    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.state["encodes"] == 1

    # 원본은 그대로지만 프록시가 읽히지 않는 상태(ffmpeg 중단 등).
    proxy_env.state["probe_ok"] = False

    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.state["encodes"] == 2, (
        "ffprobe 로 읽히지 않는 프록시를 재사용하면 미리보기가 500 으로 죽는다"
    )


def test_invalidate_footage_caches_removes_sidecar(proxy_env, monkeypatch):
    """덮어쓰기 무효화 시 프록시와 사이드카가 함께 사라져야 고아 메타가 안 남는다."""
    from web.backend import footage_paths

    project_router._ensure_proxy("proj-1", "clip.mp4")
    assert proxy_env.proxy.exists() and proxy_env.meta.exists()

    monkeypatch.setattr(
        "web.backend.state.get_cache_dir", lambda pid: proxy_env.cache_dir, raising=False
    )
    footage_paths.invalidate_footage_caches("proj-1", "clip.mp4")

    assert not proxy_env.proxy.exists()
    assert not proxy_env.meta.exists()
