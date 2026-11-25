"""그래픽 오버레이 자산 정적 스트리밍 (#72).

`assets/graphics/{category}/{name}.png` 의 Twemoji PNG 를 미리보기 UI 가 직접
`<img>` 로 표시할 수 있도록 단일 파일 스트리밍을 제공한다. 카테고리/이름은 모두
``src/graphic_assets._ASSET_TABLE`` 화이트리스트 안에서만 허용 — 임의 path 접근 차단.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from src.graphic_assets import get_asset_path

router = APIRouter(prefix="/api/graphic-assets", tags=["graphic-assets"])


@router.get("/{category}/{name}.png")
def stream_graphic_asset(category: str, name: str) -> FileResponse:
    """카테고리/이름 → Twemoji PNG. 화이트리스트 외 요청은 404."""
    path = get_asset_path(category, name)
    if not path:
        raise HTTPException(404, f"graphic asset not found: {category}/{name}")
    return FileResponse(
        path,
        media_type="image/png",
        # 자산은 불변(파일명에 디자인 버전 박혀 있지 않으므로) — 브라우저 캐시 1일.
        headers={"Cache-Control": "public, max-age=86400"},
    )
