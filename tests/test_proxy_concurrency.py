"""`_ensure_proxy` 동시 호출 경쟁(race) 방지 테스트.

배경(실측 버그): 이어보기 프리페치가 여러 씬의 `POST /scenes/{id}/preview` 를 동시에
쏜다. 여러 씬이 **같은 원본 footage** 를 공유하면 `_ensure_proxy()` 가 같은 프록시
경로에 동시에 썼고, 서로의 프록시/사이드카를 덮어쓰거나 지웠다. 그 결과

  - "[proxy] 생성 실패", "[proxy] 원본 변경 감지 → 재생성" 이 원본이 멀쩡한데도 발생
  - 프록시의 file_signature 가 호출마다 달라져 `TrimStageKey.input_sig` 가 흔들리고
  - trim/final 미리보기 캐시가 영구 MISS → 매번 전량 재인코딩(씬당 12~72초)

여기서 지키는 계약 3가지:
  1. 같은 원본 동시 N건 → ffmpeg 인코딩 정확히 1회, 반환 경로 동일.
  2. 서로 다른 원본 2건은 서로 막지 않는다(병렬 생성 가능).
  3. 동시 호출 후에도 사이드카 sig 가 원본과 일치 = 다음 호출이 캐시 적중.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from web.backend.routers import project as project_router


class _FakeProject:
    def __init__(self, footage_dir: Path):
        self.footage_dir = str(footage_dir)


@pytest.fixture
def race_env(tmp_path, monkeypatch):
    """프록시 생성을 흉내내되, ffmpeg 를 '느린 작업' 으로 만들어 경쟁을 재현한다."""
    footage_dir = tmp_path / "footage"
    footage_dir.mkdir()
    cache_dir = tmp_path / "cache"
    for name in ("a.mp4", "b.mp4"):
        (footage_dir / name).write_bytes(b"source bytes for " + name.encode())

    state = {
        "encodes": [],          # 인코딩된 출력 경로 기록
        "concurrent": 0,        # 현재 동시 인코딩 수
        "max_concurrent": 0,    # 관측된 최대 동시 인코딩 수
        "gate": threading.Event(),  # 첫 인코딩을 붙잡아 경쟁 창을 넓힌다
        "use_gate": False,
    }
    lock = threading.Lock()

    monkeypatch.setattr(project_router.store, "get_cache_dir", lambda pid: cache_dir)
    monkeypatch.setattr(
        project_router.store, "get_project", lambda pid: _FakeProject(footage_dir)
    )
    monkeypatch.setattr(project_router, "_ffprobe_video_size", lambda path: None)
    monkeypatch.setattr(project_router, "_ffprobe_duration", lambda path: 3.0)

    def _fake_run(cmd, *a, **kw):
        assert cmd[0] == "ffmpeg", f"예상치 못한 외부 호출: {cmd[0]}"
        out = Path(cmd[-1])
        with lock:
            state["encodes"].append(str(out))
            state["concurrent"] += 1
            state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
        try:
            if state["use_gate"]:
                # 인코딩이 '진행 중' 인 동안 다른 스레드가 같은 경로를 건드릴 시간을 준다.
                state["gate"].wait(timeout=5.0)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"proxy of " + Path(cmd[2]).name.encode())
        finally:
            with lock:
                state["concurrent"] -= 1

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    monkeypatch.setattr(project_router.subprocess, "run", _fake_run)

    class _Env:
        pass

    env = _Env()
    env.footage_dir = footage_dir
    env.state = state
    return env


def test_concurrent_same_source_encodes_once(race_env):
    """같은 원본에 대한 동시 N건 → ffmpeg 인코딩 1회, 반환 경로 동일. (핵심 재현)"""
    race_env.state["use_gate"] = True
    n = 8

    def _call(_i):
        return project_router._ensure_proxy("proj-1", "a.mp4")

    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(_call, i) for i in range(n)]
        # 첫 인코딩이 시작될 때까지 기다렸다가 문을 연다 — 그 사이 나머지는 모두
        # 락에 걸려 있어야 한다(예전 구현이라면 여기서 8건이 동시에 인코딩됐다).
        for _ in range(500):
            if race_env.state["encodes"]:
                break
            threading.Event().wait(0.01)
        race_env.state["gate"].set()
        results = [f.result(timeout=20) for f in futures]

    assert all(r is not None for r in results), "동시 호출 중 일부가 원본 폴백으로 빠졌다"
    assert len(set(map(str, results))) == 1, "동시 호출이 서로 다른 프록시 경로를 돌려줬다"
    assert len(race_env.state["encodes"]) == 1, (
        f"같은 원본을 {len(race_env.state['encodes'])}회 중복 인코딩했다 — 프록시 경쟁 재발"
    )


def test_concurrent_different_sources_run_in_parallel(race_env):
    """서로 다른 원본 2건은 서로 막지 않는다 — 락 범위가 (project, filename) 단위."""
    race_env.state["use_gate"] = True

    def _call(name):
        return project_router._ensure_proxy("proj-1", name)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_call, n) for n in ("a.mp4", "b.mp4")]
        # 둘 다 인코딩에 진입할 때까지 기다린다. 파일 단위 락이 제대로 쪼개져 있으면
        # 게이트를 열지 않아도 두 건이 동시에 진입한다.
        for _ in range(500):
            if race_env.state["max_concurrent"] >= 2:
                break
            threading.Event().wait(0.01)
        race_env.state["gate"].set()
        results = [f.result(timeout=20) for f in futures]

    assert race_env.state["max_concurrent"] >= 2, (
        "서로 다른 원본인데 직렬화됐다 — 락 범위가 너무 넓다"
    )
    assert len(race_env.state["encodes"]) == 2
    assert results[0] != results[1]
    assert all(r is not None and r.exists() for r in results)


def test_sidecar_consistent_after_race(race_env):
    """동시 호출 후에도 사이드카 sig 가 원본과 일치 → 다음 호출이 캐시 적중."""
    from src.render_spec import preview_stages as stages

    race_env.state["use_gate"] = True

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(project_router._ensure_proxy, "proj-1", "a.mp4") for _ in range(6)]
        for _ in range(500):
            if race_env.state["encodes"]:
                break
            threading.Event().wait(0.01)
        race_env.state["gate"].set()
        [f.result(timeout=20) for f in futures]

    meta = project_router._proxy_meta_path("proj-1", "a.mp4")
    assert meta.exists(), "경쟁 후 사이드카가 사라졌다 — 다음 호출이 무조건 재생성된다"
    cached_sig = project_router._read_proxy_meta(meta)
    assert cached_sig == stages.file_signature(race_env.footage_dir / "a.mp4"), (
        "사이드카 sig 가 원본과 어긋난다 — '원본 변경 감지' 오탐으로 캐시가 영구 MISS"
    )

    # 실제로 다음 호출이 인코딩 없이 적중하는지 확인.
    encodes_before = len(race_env.state["encodes"])
    again = project_router._ensure_proxy("proj-1", "a.mp4")
    assert again is not None
    assert len(race_env.state["encodes"]) == encodes_before, (
        "경쟁 직후 호출이 재인코딩됐다 — trim/final 캐시까지 연쇄 MISS 된다"
    )
