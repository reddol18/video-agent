"""시리즈 목록 라우터 (홈 프로젝트 그룹핑 보조 저장소).

배경:
  프로젝트-시리즈 연결은 여전히 `Project.series`(문자열=시리즈명)로 이뤄진다.
  다만 "프로젝트 0개인 빈 시리즈"는 어느 프로젝트의 series 값에도 없으므로 홈
  화면에 렌더될 수 없다. 이 라우터는 그런 빈 시리즈까지 유지·표시하기 위한
  보조 저장소다. 그룹핑 시 = [빈 시리즈 목록 ∪ 프로젝트들의 series 값] 합집합.

저장 위치: web/data/_series/series.json  (단일 JSON, 시리즈 배열)
  - `_series` 폴더는 UUID 패턴이 아니고 `state.load_all()` 의 `*/state.json`
    glob 에도 걸리지 않는다(state.load_all 은 `_` 로 시작하는 폴더도 명시적으로
    스킵). 따라서 시리즈 저장 파일이 깨진 프로젝트로 오인 로드될 일이 없다.
  - templates.py 와 동일하게 원자적 저장(`.json.tmp` → os.replace)을 쓴다.

정책 결정(review-notes 명기):
  - 중복 이름 생성: 400 대신 **기존 시리즈를 그대로 반환**(멱등). 사용자가 이미
    있는 이름을 다시 만들어도 오류 없이 그 그룹으로 흡수되게 한다.
  - 시리즈 삭제: 해당 시리즈에 속한 프로젝트들의 `series` 필드를 **빈 문자열로
    되돌려 미편성**으로 보낸다(프로젝트 자체는 삭제하지 않음).
  - 이름 변경(PATCH): 그 시리즈에 속한 프로젝트들의 `series` 값도 새 이름으로
    일괄 갱신하여 일관성을 유지한다.

API:
  - GET    /api/series            — 시리즈 목록
  - POST   /api/series {name}     — 생성(멱등)
  - PATCH  /api/series/{id} {name}— 이름 변경(+ 소속 프로젝트 series 일괄 갱신)
  - DELETE /api/series/{id}       — 삭제(+ 소속 프로젝트 series 미편성 처리)
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import state as store

router = APIRouter(prefix="/api/series", tags=["series"])


class SeriesItem(BaseModel):
    id: str
    name: str
    created_at: str


class CreateSeriesRequest(BaseModel):
    name: str


class UpdateSeriesRequest(BaseModel):
    name: str


# store.DATA_DIR 은 테스트에서 런타임에 교체되므로, import 시점에 경로를 고정하지
# 않고 매 호출마다 계산한다(templates.py 는 고정하지만 그건 회귀 위험이 있음).
def _series_dir() -> Path:
    d = store.DATA_DIR / "_series"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _series_path() -> Path:
    return _series_dir() / "series.json"


def _load_series() -> list[SeriesItem]:
    path = _series_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("series", []) if isinstance(data, dict) else data
        return [SeriesItem(**it) for it in items]
    except Exception as e:
        print(f"[series] 손상된 series.json 무시: {e}", flush=True)
        return []


def _dump_series(items: list[SeriesItem]) -> None:
    path = _series_path()
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(
        {"series": [it.model_dump() for it in items]},
        ensure_ascii=False,
        indent=2,
    )
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


# ── API ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[SeriesItem])
def list_series():
    """저장된 시리즈 목록. 생성 시각 오름차순."""
    items = _load_series()
    items.sort(key=lambda x: x.created_at)
    return items


@router.post("", response_model=SeriesItem)
def create_series(req: CreateSeriesRequest):
    """시리즈 생성. 같은 이름이 이미 있으면 기존 항목을 그대로 반환(멱등)."""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "시리즈 이름은 비어있을 수 없습니다")
    items = _load_series()
    for it in items:
        if it.name == name:
            return it  # 멱등 — 중복 이름은 기존 반환
    now = datetime.now(timezone.utc).isoformat()
    new_item = SeriesItem(id=str(uuid.uuid4()), name=name, created_at=now)
    items.append(new_item)
    _dump_series(items)
    return new_item


@router.patch("/{series_id}", response_model=SeriesItem)
def rename_series(series_id: str, req: UpdateSeriesRequest):
    """시리즈 이름 변경 + 소속 프로젝트들의 series 값 일괄 갱신."""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "시리즈 이름은 비어있을 수 없습니다")
    items = _load_series()
    target = next((it for it in items if it.id == series_id), None)
    if target is None:
        raise HTTPException(404, "시리즈를 찾을 수 없습니다")

    old_name = target.name
    if name == old_name:
        return target

    # 다른 시리즈가 이미 같은 이름을 쓰고 있으면 충돌.
    if any(it.name == name and it.id != series_id for it in items):
        raise HTTPException(400, "같은 이름의 시리즈가 이미 존재합니다")

    target.name = name
    _dump_series(items)

    # 소속 프로젝트 series 값 일괄 갱신(일관성 유지).
    for p in store.list_projects():
        if (getattr(p, "series", "") or "").strip() == old_name:
            store.update_project(p.project_id, series=name)

    return target


@router.delete("/{series_id}")
def delete_series(series_id: str):
    """시리즈 삭제 + 소속 프로젝트들의 series 를 미편성(빈 문자열)으로 되돌림."""
    items = _load_series()
    target = next((it for it in items if it.id == series_id), None)
    if target is None:
        raise HTTPException(404, "시리즈를 찾을 수 없습니다")

    remaining = [it for it in items if it.id != series_id]
    _dump_series(remaining)

    # 소속 프로젝트 series 초기화(프로젝트 자체는 유지).
    for p in store.list_projects():
        if (getattr(p, "series", "") or "").strip() == target.name:
            store.update_project(p.project_id, series="")

    return {"deleted": True, "name": target.name}
