"""SFX 프리셋 카탈로그 — 앱 내장 기본 효과음(assets/sfx_presets/manifest.json) 기반.

오버레이 SFX 선택을 기존 프로젝트 studio_assets(=업로드된 오디오)뿐 아니라
앱에 내장된 기본 효과음 프리셋에서도 가능하게 한다(#418, 예전에는 외부 폴더를 읽었다).

참조 규약
─────────
오버레이의 `sfx_asset_id` 필드는 다음 두 형태를 지원한다.
- studio_asset 일 때: studio_assets 의 `asset_id`(uuid) 그대로. (기존 동작)
- 프리셋일 때: `"preset:{preset_id}-{seq}"` 형식. `seq` 는 manifest.json `items`
  배열에서 동일 `preset_id` 가 등장한 순서(1, 2, 3 …).

이 모듈은 manifest 를 로드하고 `preset_key`("coin-pickup-1") 를 절대 경로로
해석하는 것만 담당한다. prefix 분기는 호출 측(백엔드 render router / 프론트
미리듣기) 책임이며, 본 모듈이 `preset:` 자체를 다루지 않는 것은 의도된 경계다.

회귀 안전
─────────
- manifest 파일이 없거나 비어 있으면 빈 리스트.
- manifest 항목 순서(=seq)는 이관 전과 같게 유지한다. 키 이름이 바뀌지 않으므로
  기존 프로젝트의 `preset:{key}` 참조를 이전할 필요가 없다.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

# pydantic 2.x 는 Python < 3.12 에서 typing.TypedDict 의 schema 생성을 거부한다.
# FastAPI 응답 타입으로 직접 노출되므로 typing_extensions.TypedDict 를 사용한다.
from typing_extensions import TypedDict


# 내장 기본 효과음 폴더. manifest.json 의 `file` 필드는 이 폴더 기준 상대 경로.
SFX_PRESETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "sfx_presets"

# 로컬 전용 항목(#418): 재배포 허용이 확인되지 않은 외부 출처 파일(manifest `bundled: false`)은
# 같은 폴더에 두되 .gitignore 로 공개 저장소에서 뺀다. 파일이 있는 환경(사용자 PC)에서만
# 목록에 나오고, 없는 환경에서는 건너뛴다. seq 는 manifest 순서로 매기므로 건너뛰어도
# 다른 키의 번호는 바뀌지 않는다.


class SfxPresetEntry(TypedDict):
    """프리셋 1개 메타. 프론트 UI 와 직렬화 호환되도록 평탄한 dict 형식."""

    preset_key: str       # "coin-pickup-1" — UI/저장 식별자. seq 는 manifest 순서.
    preset_id: str        # "coin-pickup"
    seq: int              # 1, 2, 3 … 같은 preset_id 가 등장한 순서.
    file_path: str        # 절대 경로 (str, JSON 호환).
    duration: float       # 초 단위.
    prompt: str           # 설명/검색용. 빈 문자열일 수 있음.
    is_user: bool         # True = 사용자가 직접 올린 공용 효과음(라이브러리).


@lru_cache(maxsize=1)
def load_sfx_presets() -> list[SfxPresetEntry]:
    """manifest.json 을 한 번 로드하고 캐시한다.

    같은 `preset_id` 가 여러 번 나오면 등장 순서대로 1, 2, 3 의 `seq` 를 부여한다.
    이 순서는 manifest.json 의 `items` 배열 인덱스 순서이며 내장 manifest 에 고정돼 있다.
    """
    manifest = SFX_PRESETS_DIR / "manifest.json"
    if not manifest.exists():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    counters: dict[str, int] = {}
    result: list[SfxPresetEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = item.get("preset_id")
        if not isinstance(pid, str) or not pid:
            continue

        counters[pid] = counters.get(pid, 0) + 1
        seq = counters[pid]

        rel_file = item.get("file", "")
        if not isinstance(rel_file, str) or not rel_file:
            # 파일 경로가 비어있는 항목은 카운터에서 제외할 수 없으므로 그대로 두되 스킵.
            # (이미 counters 증가했으니 다음 같은 preset_id 가 seq 를 1 더 받게 된다.)
            continue
        abs_path = SFX_PRESETS_DIR / rel_file
        if item.get("bundled") is False and not abs_path.exists():
            continue  # 로컬 전용 파일이 없는 환경(공개 저장소 클론)

        try:
            duration = float(item.get("duration", 1.0))
        except (TypeError, ValueError):
            duration = 1.0
        prompt = item.get("prompt", "")
        if not isinstance(prompt, str):
            prompt = ""

        result.append(SfxPresetEntry(
            preset_key=f"{pid}-{seq}",
            preset_id=pid,
            seq=seq,
            file_path=str(abs_path),
            duration=duration,
            prompt=prompt,
            is_user=False,
        ))
    return result


# ── 사용자 공용 효과음 라이브러리 ─────────────────────────────────────────────
# 메인 화면에서 직접 올린 효과음. 모든 프로젝트가 공유하며, 프리셋과 같은
# `preset:{key}` 참조 규약을 쓴다(key = "user-{hex}"). 그래서 렌더/미리듣기
# 해석 경로(get_preset_file_path)를 그대로 탄다.
#   저장 위치: web/data/_sfxlib/{key}{ext} + manifest.json
#   `_sfxlib` 는 UUID 폴더가 아니어서 프로젝트 로더(*/state.json)에 걸리지 않는다.
USER_SFX_PREFIX = "user-"
USER_SFX_DIR = Path(
    os.environ.get("VIDEO_AGENT_USER_SFX_DIR")
    or (Path(__file__).resolve().parent.parent / "web" / "data" / "_sfxlib")
)
USER_SFX_MANIFEST = USER_SFX_DIR / "manifest.json"


def read_user_sfx_manifest() -> list[dict]:
    """라이브러리 manifest 의 items. 없거나 깨졌으면 빈 리스트."""
    if not USER_SFX_MANIFEST.exists():
        return []
    try:
        data = json.loads(USER_SFX_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def write_user_sfx_manifest(items: list[dict]) -> None:
    """manifest 를 원자적으로 기록한다(임시 파일 → replace)."""
    USER_SFX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USER_SFX_MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, USER_SFX_MANIFEST)


def load_user_sfx() -> list[SfxPresetEntry]:
    """사용자 라이브러리 효과음 목록(최근 올린 것 먼저). 캐시하지 않는다(수시로 바뀜)."""
    result: list[SfxPresetEntry] = []
    for item in reversed(read_user_sfx_manifest()):
        key = item.get("key")
        fname = item.get("file")
        if not isinstance(key, str) or not key.startswith(USER_SFX_PREFIX):
            continue
        if not isinstance(fname, str) or not fname:
            continue
        label = item.get("label") if isinstance(item.get("label"), str) else ""
        try:
            duration = float(item.get("duration", 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        result.append(SfxPresetEntry(
            preset_key=key,
            preset_id=label or key,   # UI 그룹 헤더 = 라벨
            seq=1,
            file_path=str(USER_SFX_DIR / fname),
            duration=duration,
            prompt="내 효과음",
            is_user=True,
        ))
    return result


def get_preset_file_path(preset_key: str) -> str | None:
    """`"coin-pickup-1"` / `"user-ab12cd34"` → 절대 경로. 없으면 None.

    `preset:` prefix 가 포함된 채 전달되면 None 을 반환한다 — 호출 측이 prefix
    를 먼저 분리해야 한다(라우터 `resolve_sfx_path` 가 그 일을 한다).
    """
    if not preset_key:
        return None
    if preset_key.startswith(USER_SFX_PREFIX):
        for p in load_user_sfx():
            if p["preset_key"] == preset_key:
                return p["file_path"]
        return None
    for p in load_sfx_presets():
        if p["preset_key"] == preset_key:
            return p["file_path"]
    return None
