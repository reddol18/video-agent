"""WebSocket 기반 태스크 진행률 브리지."""
from __future__ import annotations
import asyncio
import uuid
from typing import Any

from fastapi import WebSocket

# task_id → asyncio.Queue
_queues: dict[str, asyncio.Queue] = {}
# task_id → asyncio.Task
_tasks: dict[str, asyncio.Task] = {}
# push_sync 가 워커 스레드(run_in_executor)에서 호출될 때 쓸 메인 이벤트 루프.
# 워커 스레드에는 실행 중인 루프가 없어 asyncio.get_event_loop() 가 실패하므로,
# 루프가 살아있는 컨텍스트에서 미리 잡아둔 이 루프로 call_soon_threadsafe 한다.
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def create_task_id() -> str:
    tid = str(uuid.uuid4())
    _queues[tid] = asyncio.Queue()
    return tid


def get_queue(task_id: str) -> asyncio.Queue | None:
    return _queues.get(task_id)


async def push(task_id: str, msg: dict[str, Any]) -> None:
    q = _queues.get(task_id)
    if q:
        await q.put(msg)


def push_sync(task_id: str, msg: dict[str, Any]) -> None:
    """동기 컨텍스트(스레드)에서 호출 가능한 진행률 푸시.

    _do_render 는 run_in_executor 워커 스레드에서 돌기 때문에 그 스레드에는
    실행 중인 이벤트 루프가 없다(Python 3.11 에서 asyncio.get_event_loop() 실패).
    미리 잡아둔 메인 루프(_main_loop)로 call_soon_threadsafe 하고, 없을 때만
    현재 실행 루프를 폴백으로 시도한다.
    """
    q = _queues.get(task_id)
    if q is None:
        return
    loop = _main_loop
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is not None:
        loop.call_soon_threadsafe(q.put_nowait, msg)


async def websocket_handler(websocket: WebSocket, task_id: str) -> None:
    await websocket.accept()
    q = _queues.get(task_id)
    if not q:
        await websocket.send_json({"type": "error", "task_id": task_id, "code": "NOT_FOUND", "message": "태스크를 찾을 수 없습니다"})
        await websocket.close()
        return

    try:
        # WS 연결 시점에 이미 태스크가 완료됐고 queue가 비어있으면 즉시 complete 전송
        task = _tasks.get(task_id)
        if task and task.done() and q.empty():
            await websocket.send_json({"type": "complete", "task_id": task_id, "summary": {}})
            return

        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=2.0)
            except asyncio.TimeoutError:
                task = _tasks.get(task_id)
                if task and task.done():
                    # task 완료 후 queue에 남은 메시지를 모두 flush
                    await asyncio.sleep(0.1)
                    while not q.empty():
                        leftover = q.get_nowait()
                        await websocket.send_json(leftover)
                        if leftover.get("type") in ("complete", "error") and not leftover.get("scene_id"):
                            return
                    # flush 후에도 complete 없으면 자체 전송
                    await websocket.send_json({"type": "complete", "task_id": task_id, "summary": {}})
                    break
                await websocket.send_json({"type": "ping"})
                continue

            await websocket.send_json(msg)

            if msg.get("type") in ("complete", "error") and not msg.get("scene_id"):
                break
    except Exception:
        pass
    finally:
        _queues.pop(task_id, None)
        # 클라이언트가 먼저 끊었거나(새로고침/네트워크) 이미 close 가 전송된 뒤면
        # starlette 이 RuntimeError("Cannot call send once a close message has been sent")
        # 를 던진다. finally 에서 이게 전파되면 handler 가 크래시해 이후 진행률 전달이
        # 막히므로 방어적으로 무시한다.
        try:
            await websocket.close()
        except Exception:
            pass


def register_bg_task(task_id: str, task: asyncio.Task) -> None:
    _tasks[task_id] = task


def cancel_task(task_id: str) -> bool:
    t = _tasks.pop(task_id, None)
    if t:
        t.cancel()
        return True
    return False
