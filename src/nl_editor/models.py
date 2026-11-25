"""자연어 편집 엔진 요청/응답 Pydantic 모델 (#315).

설계 의도
--------
1. **RFC 6902 호환** — `JsonPatchOp` 는 표준 JSON Patch 스펙을 그대로 따른다.
   `from` 은 Python 예약어이므로 별칭(`alias="from"`)으로 `from_` 필드에 매핑한다.
2. **LLM 응답을 그대로 받기** — Gemini가 JSON으로 반환하는 `{patch, summary, safety_flags}`
   구조를 `NlEditResponse` 한 모델로 받아 다운스트림에서 그대로 사용한다.
3. **요청·응답을 분리** — 미리보기(`/nl-edit`), 적용(`/nl-edit/apply`), undo(`/nl-edit/undo`)
   세 엔드포인트가 서로 다른 입출력을 가지므로 모델도 분리한다.

이 모델들은 Pydantic v2 기반이므로 자동 TS 타입 생성(`scripts/gen_ts_types.sh`) 대상에
포함될 수 있다. 다만 본 PR 단계에서는 백엔드 검증·테스트만 보장하고, UI 연결은 #316.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── JSON Patch 단일 연산 ───────────────────────────────────────────────────

JsonPatchOpType = Literal["add", "remove", "replace", "move", "copy", "test"]


class JsonPatchOp(BaseModel):
    """RFC 6902 JSON Patch 1개 연산.

    필드 의미는 표준을 그대로 따른다.
    - `op`: 6가지 연산 중 하나.
    - `path`: 대상 위치를 가리키는 JSON Pointer (`/scenes/0/layers/1/text` 형식).
    - `value`: `add` / `replace` / `test` 시 사용. `remove` / `move` / `copy` 에서는 무시.
    - `from_`: `move` / `copy` 의 원본 경로. JSON 직렬화 시 `"from"` 키로 노출된다 —
      Python 예약어 회피용 alias.
    """

    # alias 허용: LLM이 표준대로 `"from"` 키를 보내도 받아낼 수 있도록 한다.
    model_config = ConfigDict(populate_by_name=True)

    op: JsonPatchOpType
    path: str
    value: Optional[Any] = None
    from_: Optional[str] = Field(default=None, alias="from")


# ── 대화 컨텍스트 1턴 (선택) ───────────────────────────────────────────────

class NlEditTurn(BaseModel):
    """이전 자연어 편집 1턴 — 멀티턴 대화 컨텍스트 전송용 (선택).

    프론트가 최근 N턴을 추려 보내면 LLM이 "방금 그 씬을 더 어둡게" 같은 후속
    명령을 이해할 수 있다. 본 PR 단계에서는 데이터 통과만 보장한다.
    """

    instruction: str  # 사용자 자연어
    summary: str = ""  # 모델이 만든 직전 변경 요약
    applied: bool = False  # 사용자가 적용했는지 여부


# ── 안전 플래그 ───────────────────────────────────────────────────────────

# UI(#316)가 확인 다이얼로그로 띄울 수 있도록 *고정 키* 집합으로 정의한다.
# 새 위험 패턴이 발견되면 이 Literal에 추가해 프론트가 즉시 인지하게 한다.
SafetyFlag = Literal[
    "delete_all_layers",       # path: "/scenes/*/layers" 통째로 remove
    "delete_all_scenes",       # path: "/scenes" 통째로 remove
    "canvas_size_change",      # path: "/scenes/*/canvas/width|height|fps"
    "bulk_layer_delete",       # 한 번의 patch에서 layer remove가 N개 이상
    "unknown_top_level_path",  # /scenes 외 루트 경로를 건드림 — 보호 차원
]


# ── 미리보기 요청/응답 ─────────────────────────────────────────────────────

class NlEditRequest(BaseModel):
    """`POST /api/projects/{id}/nl-edit` — 자연어 명령 → patch 미리보기 요청.

    이 호출은 RenderSpec을 *적용하지 않는다*. patch + summary + safety_flags 를
    돌려주어 UI가 사용자에게 보여주고, 사용자 승인 후 `apply` 엔드포인트가 실제로
    RenderSpec을 갱신하는 흐름.
    """

    instruction: str = Field(..., min_length=1)
    history: list[NlEditTurn] = Field(default_factory=list)
    # 사용자가 현재 보고 있는 씬(이벤트 기반 자동 분할 등 씬 단위 동작에 사용).
    target_scene_id: int | None = None


class SplitDetectResult(BaseModel):
    """이벤트 기반 자동 분할 결과."""
    scene_id: int
    cuts_sec: list[float]  # 씬 기준 상대 초(splitSceneAt이 받는 형식)
    reasoning: str = ""
    # 분할 대상 씬의 길이(초). 프론트 splitSceneAt이 footage 길이로 clamp할 때 사용.
    scene_duration_sec: float | None = None


class OverlayItem(BaseModel):
    """이벤트 기반 자동 오버레이 1개. 프론트가 그대로 TextOverlay에 머지."""
    id: str
    text: str
    x: float
    y: float
    font_family: str
    color: str
    start_sec: float
    end_sec: float
    sfx_asset_id: str | None = None
    sfx_volume: float = 0.7


class OverlayDetectResult(BaseModel):
    """이벤트 기반 자동 오버레이 생성 결과."""
    scene_id: int
    overlays: list[OverlayItem]
    reasoning: str = ""


class NlEditResponse(BaseModel):
    """미리보기 응답 — patch + 요약 + 안전 플래그 + 예상 비용.

    `estimated_cost_usd` 는 cost_estimator 기준으로 계산된 USD. KRW는 프론트가
    환율을 곱해 표시한다.

    `kind`가 "split"이면 patch는 빈 배열이고 `split` 필드가 분할 명령을 담는다.
    """

    patch: list[JsonPatchOp]
    summary: str
    safety_flags: list[SafetyFlag] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0
    # 후속 디버그/감사용 부가 정보. 비어 있어도 무방.
    notes: str = ""
    # 응답 종류: 일반 RenderSpec patch면 "patch", 자동 씬 분할이면 "split", 자동 오버레이면 "overlay".
    kind: str = "patch"
    split: SplitDetectResult | None = None
    overlay: OverlayDetectResult | None = None


# ── 적용 요청/응답 ────────────────────────────────────────────────────────

class NlEditApplyRequest(BaseModel):
    """사용자가 미리보기를 승인 후 실제 patch 적용을 요청.

    `instruction` / `summary` 는 감사 로그용. 미리보기에서 받은 patch를 그대로 다시
    실어 보내야 한다(서버는 멱등하게 적용한다).
    """

    patch: list[JsonPatchOp]
    instruction: str = ""
    summary: str = ""


class NlEditApplyResponse(BaseModel):
    """적용 응답 — 새 RenderSpec 리스트 + 역동기화된 SceneCard 리스트 + snapshot id.

    `scene_cards` 는 #335 에서 추가된 필드. 프론트가 IndexedDB Project.scenes 와
    zustand store 를 갱신할 때 사용한다. RenderSpec 의 layers 중 SceneCard 로
    환산 가능한 항목(text/shape/graphic/mosaic + footage_speed)만 반영되며,
    환산 불가 항목(filters/motions/narration_text 등) 은 기존 SceneCard 값을 그대로 유지한다.
    """

    scenes: list[dict] = Field(default_factory=list)  # RenderSpec 리스트 (dict 형태)
    scene_cards: list[dict] = Field(default_factory=list)  # SceneCard 역동기화 결과 (#335)
    snapshot_id: str = ""  # undo 호출용 식별자
    applied_at: str = ""  # ISO 8601 타임스탬프


# ── undo 응답 ─────────────────────────────────────────────────────────────

class NlEditUndoResponse(BaseModel):
    """undo 응답 — 롤백된 RenderSpec 리스트 + 역동기화 SceneCard 리스트.

    추가 snapshot이 더 없을 경우 `has_more_snapshots=False` 로 신호한다.
    `scene_cards` 는 #335 에서 추가 — 프론트가 IndexedDB / store 를 함께 롤백.
    """

    scenes: list[dict] = Field(default_factory=list)
    scene_cards: list[dict] = Field(default_factory=list)
    snapshot_id: str = ""
    has_more_snapshots: bool = False
    rolled_back_at: str = ""


__all__ = [
    "JsonPatchOp",
    "JsonPatchOpType",
    "NlEditTurn",
    "NlEditRequest",
    "NlEditResponse",
    "NlEditApplyRequest",
    "NlEditApplyResponse",
    "NlEditUndoResponse",
    "SafetyFlag",
]
