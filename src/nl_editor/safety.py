"""위험한 patch 감지 (#315).

UI(#316)가 확인 다이얼로그를 띄울 수 있도록 위험 패턴을 *고정 키*로 라벨링한다.
LLM이 만든 safety_flags 만 믿지 않고 백엔드에서 한 번 더 검증하여 모델이 깜빡한
패턴까지 잡아낸다(이중 안전망).

감지 규칙
--------
1. `delete_all_layers` — `/scenes/N/layers` 전체를 remove 하거나, 한 씬의 모든 layer를
   순회 remove 하는 경우.
2. `delete_all_scenes` — `/scenes` 전체 remove.
3. `canvas_size_change` — `/scenes/N/canvas/width|height|fps` 의 replace/add.
4. `bulk_layer_delete` — 한 번의 patch에서 layer remove가 `BULK_DELETE_THRESHOLD` 이상.
5. `unknown_top_level_path` — `/scenes` 가 아닌 루트 경로 변경(예: `/version`, `/canvas`).
"""

from __future__ import annotations

import re
from typing import Any

from src.nl_editor.models import JsonPatchOp, SafetyFlag


# ── 임계값 ────────────────────────────────────────────────────────────────

# 한 번에 layer를 이 개수 이상 지우면 위험으로 본다.
BULK_DELETE_THRESHOLD = 3


# ── path 분석 헬퍼 ────────────────────────────────────────────────────────

_LAYERS_ALL_RE = re.compile(r"^/scenes/-?\d+/layers/?$")
_LAYER_SINGLE_RE = re.compile(r"^/scenes/-?\d+/layers/-?\d+$")
_CANVAS_FIELD_RE = re.compile(r"^/scenes/-?\d+/canvas/(width|height|fps)$")


def _root_key(path: str) -> str:
    """`/scenes/0/...` → `scenes`. 빈 path 또는 루트면 `""`."""
    if not path or path == "/":
        return ""
    if not path.startswith("/"):
        return path
    return path[1:].split("/", 1)[0]


# ── 본체 ──────────────────────────────────────────────────────────────────

def detect_safety_flags(patch: list[Any]) -> list[SafetyFlag]:
    """patch 를 분석해 위험 플래그 리스트를 반환한다.

    인자 `patch`: `JsonPatchOp` 객체 또는 dict(`{"op", "path", ...}`) 리스트.
    반환은 순서 무관 unique 한 SafetyFlag 리스트.

    이 함수는 LLM 응답과 무관하게 *백엔드 측 보호*로 사용된다 — LLM이 safety_flags 를
    비워서 보냈더라도 본 함수가 같은 결론을 다시 내려준다.
    """
    flags: set[SafetyFlag] = set()
    layer_remove_count = 0

    for op_raw in patch:
        if isinstance(op_raw, JsonPatchOp):
            op = op_raw.op
            path = op_raw.path or ""
        elif isinstance(op_raw, dict):
            op = str(op_raw.get("op", "")).lower()
            path = str(op_raw.get("path", ""))
        else:
            continue

        # 0) 루트 키 검사 — /scenes 가 아닌 곳을 건드리면 unknown_top_level_path.
        rk = _root_key(path)
        if rk and rk != "scenes":
            flags.add("unknown_top_level_path")

        # 1) /scenes 전체 remove
        if op == "remove" and path == "/scenes":
            flags.add("delete_all_scenes")

        # 2) /scenes/N/layers 전체 remove (또는 replace 로 빈 배열로 바꾸기)
        if _LAYERS_ALL_RE.match(path):
            if op == "remove":
                flags.add("delete_all_layers")
            elif op == "replace":
                # replace 로 빈 리스트를 박는 케이스
                value = op_raw.value if isinstance(op_raw, JsonPatchOp) else op_raw.get("value")
                if isinstance(value, list) and len(value) == 0:
                    flags.add("delete_all_layers")

        # 3) canvas 필드 변경
        if op in ("replace", "add") and _CANVAS_FIELD_RE.match(path):
            flags.add("canvas_size_change")

        # 4) bulk layer delete 카운트
        if op == "remove" and _LAYER_SINGLE_RE.match(path):
            layer_remove_count += 1

    if layer_remove_count >= BULK_DELETE_THRESHOLD:
        flags.add("bulk_layer_delete")

    # 안정 정렬 — 테스트 결정성
    order = [
        "delete_all_scenes",
        "delete_all_layers",
        "canvas_size_change",
        "bulk_layer_delete",
        "unknown_top_level_path",
    ]
    return [f for f in order if f in flags]


__all__ = [
    "BULK_DELETE_THRESHOLD",
    "detect_safety_flags",
]
