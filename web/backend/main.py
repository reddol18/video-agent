from __future__ import annotations
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocket

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# .env 로드 — 프로젝트 루트의 .env 를 명시적 경로로 읽는다.
# narration_generator 는 임포트 시 자체 load_dotenv() 를 부르지만, voices 프록시 등
# 그 모듈을 거치지 않는 경로(TYPECAST_API_KEY 사용)를 위해 앱 시작 시 한 번 로드한다.
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from . import state as store
from .ws.progress import websocket_handler
from .routers import (
    project,
    matching,
    tts,
    render,
    settings,
    studio,
    cost,
    effect_catalog,
    nl_editor,
    sfx_presets,
    graphic_assets,
    templates,
    clip_library,
    series,
    scene_comments,
)

app = FastAPI(title="video-agent web editor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(project.router)
app.include_router(matching.router)
app.include_router(tts.router)
app.include_router(tts.voices_router)
app.include_router(render.router)
app.include_router(settings.router)
app.include_router(studio.router)
app.include_router(cost.router)
app.include_router(effect_catalog.router)
app.include_router(nl_editor.router)
# #SFX 프리셋 — 75개 효과음 카탈로그 + 단일 스트리밍 (#sfx-presets).
app.include_router(sfx_presets.router)
# #그래픽 자산 — assets/graphics 의 Twemoji PNG 미리보기용 정적 스트리밍 (#72).
app.include_router(graphic_assets.router)
# #406/#407 — 영상 구조 템플릿. 원본 영상 없이 씬/오버레이/BGM 구조만 보존·재사용.
# `/api/templates` 와 `/api/projects/from-template` 두 라우터를 함께 마운트.
app.include_router(templates.router)
app.include_router(templates.from_template_router)
# 커스텀 영상 클립 라이브러리 — 밈/로딩 애니메이션 등 재사용 영상의 전역 카탈로그.
# 씬 지정은 프로젝트 footage 로의 하드링크로 처리되어 렌더 경로 변경이 없다.
app.include_router(clip_library.router)
app.include_router(clip_library.scene_router)
# 시리즈 목록 — 빈 시리즈(프로젝트 0개)까지 유지·표시하기 위한 보조 저장소.
# 프로젝트-시리즈 연결은 여전히 Project.series(문자열). _series/ 폴더 사용.
app.include_router(series.router)
# 씬 편집 지시 코멘트 — 김감독이 씬에 남긴 지시를 저장하고, 클로드 에이전트가
# open 코멘트를 처리한 뒤 resolve 로 완료 이력을 남긴다.
app.include_router(scene_comments.router)


@app.websocket("/ws/{task_id}")
async def ws_endpoint(websocket: WebSocket, task_id: str):
    await websocket_handler(websocket, task_id)


@app.on_event("startup")
async def on_startup():
    store.load_all()
    # push_sync(진행률 푸시)가 run_in_executor 워커 스레드에서도 동작하도록
    # 메인 이벤트 루프를 progress 모듈에 등록한다.
    import asyncio
    from .ws.progress import set_main_loop
    set_main_loop(asyncio.get_running_loop())


@app.get("/api/health")
def health():
    return {"status": "ok"}
