"""키 단위 락 / 동시 실행 상한 유틸.

FastAPI 는 `async def` 가 아닌 `def` 핸들러를 **스레드풀** 에서 실행한다.
따라서 이 프로젝트의 미리보기·프록시 경로에서 발생하는 동시성은 전부
**스레드 동시성** 이다. `asyncio.Lock` 은 이벤트 루프에 묶여 있어 워커 스레드에서
쓸 수 없으므로 `threading` 계열을 쓴다.

파일락(fcntl 등)을 쓰지 않는 근거:
  - 백엔드는 단일 프로세스(uvicorn 단일 워커)로 기동한다. 캐시/프록시 디렉토리를
    다른 프로세스가 동시에 쓰는 시나리오가 없다.
  - 파일락은 NFS/락파일 잔재 등 실패 모드가 추가되는 반면, 여기서 막아야 할 것은
    "같은 프로세스 안 여러 워커 스레드의 동시 인코딩" 하나뿐이다.
  - 만약 다중 워커로 확장한다면 이 모듈의 `keyed_lock()` 구현만 파일락으로
    갈아끼우면 호출부는 그대로 둘 수 있다.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Hashable, Iterator


class LockTimeout(Exception):
    """제한 시간 안에 락/슬롯을 얻지 못했다."""


class KeyedLock:
    """키마다 별도의 재진입 불가 락을 제공한다.

    서로 다른 키는 서로를 막지 않는다(= 서로 다른 원본 파일은 병렬 인코딩 가능).
    참조 카운트로 미사용 키의 락 객체를 회수해, 파일 수만큼 락이 영구 누적되는 것을 막는다.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Hashable, threading.Lock] = {}
        self._refs: dict[Hashable, int] = {}

    @contextmanager
    def acquire(self, key: Hashable, timeout: float) -> Iterator[None]:
        """`key` 의 락을 잡는다. `timeout` 초 안에 못 잡으면 `LockTimeout`.

        타임아웃을 두는 이유: 앞선 인코딩이 비정상적으로 오래 걸릴 때(대용량 원본 등)
        뒤따르는 요청이 무한정 매달려 워커 스레드를 고갈시키면 안 된다.
        호출부는 타임아웃을 "안전한 폴백"(원본 사용/캐시 미사용)으로 처리한다.
        """
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            self._refs[key] = self._refs.get(key, 0) + 1
        try:
            if not lock.acquire(timeout=timeout):
                raise LockTimeout(f"락 획득 시간 초과: {key!r} ({timeout}s)")
            try:
                yield
            finally:
                lock.release()
        finally:
            with self._guard:
                n = self._refs.get(key, 1) - 1
                if n <= 0:
                    self._refs.pop(key, None)
                    self._locks.pop(key, None)
                else:
                    self._refs[key] = n


class BoundedSlots:
    """동시에 실행될 수 있는 작업 수의 상한(세마포어 래퍼)."""

    def __init__(self, limit: int) -> None:
        self._sem = threading.BoundedSemaphore(max(1, int(limit)))

    @contextmanager
    def slot(self, timeout: float) -> Iterator[None]:
        if not self._sem.acquire(timeout=timeout):
            raise LockTimeout(f"동시 실행 슬롯 대기 시간 초과 ({timeout}s)")
        try:
            yield
        finally:
            self._sem.release()


class SingleFlight:
    """같은 키의 동시 호출을 하나로 합쳐 결과를 공유한다.

    첫 호출자만 실제 작업(`fn`)을 수행하고, 같은 키로 뒤따라 들어온 호출자는
    그 결과(또는 예외)를 그대로 받는다. 프리페치와 사용자 클릭이 같은 씬에
    겹치는 상황에서 ffmpeg 를 두 번 돌리지 않기 위한 장치.
    """

    class _Call:
        __slots__ = ("done", "value", "error")

        def __init__(self) -> None:
            self.done = threading.Event()
            self.value = None
            self.error: BaseException | None = None

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._calls: dict[Hashable, "SingleFlight._Call"] = {}

    def do(self, key: Hashable, fn, timeout: float):
        """`key` 에 대해 `fn()` 을 한 번만 실행하고 결과를 공유한다.

        합류자(follower)가 `timeout` 안에 결과를 못 받으면 `LockTimeout` 을 올린다.
        반환값 `(result, is_leader)`.
        """
        with self._guard:
            call = self._calls.get(key)
            leader = call is None
            if leader:
                call = SingleFlight._Call()
                self._calls[key] = call
        assert call is not None
        if not leader:
            if not call.done.wait(timeout=timeout):
                raise LockTimeout(f"선행 작업 대기 시간 초과: {key!r} ({timeout}s)")
            if call.error is not None:
                raise call.error
            return call.value, False
        try:
            call.value = fn()
            return call.value, True
        except BaseException as e:  # noqa: BLE001 — 합류자에게 그대로 전파
            call.error = e
            raise
        finally:
            call.done.set()
            with self._guard:
                self._calls.pop(key, None)
