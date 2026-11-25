"""JSON Patch 적용 + RenderSpec 재검증 (#315).

핵심 책임
--------
1. LLM이 만든 JSON Patch를 RenderSpec 리스트(dict 표현)에 적용한다.
2. 적용 결과를 Pydantic `RenderSpec` 으로 *다시 검증*하여 잘못된 형식이면 reject한다.
3. 검증 실패 시 호출자(엔드포인트)가 LLM 재시도(최대 2회)를 진행할 수 있도록
   구분된 예외를 던진다.

설계 메모
--------
- `jsonpatch.JsonPatch` 는 RFC 6902 표준 구현. `apply(obj, in_place=False)` 가 deep copy
  후 결과를 반환하므로 원본 부작용 없음.
- 음수 인덱스(`/scenes/-1`)는 RFC 6902 가 *append-only* 의미로만 정의하지만, 사용자
  의도(마지막 씬 지칭)도 자주 나올 수 있어 본 구현은 사전 *전처리* 단계에서
  음수 인덱스를 양수로 환산한다. 그래야 jsonpatch 라이브러리가 정상 처리한다.
- 적용 후 Pydantic 검증은 RenderSpec 단위로 수행. 한 씬이라도 실패하면 전체 reject.

`/scenes` 가 아닌 루트 키(예: `/canvas`)에 대한 patch는 본 구현에서는 *현재 형태로
는 거부* 한다 — RenderSpec 리스트 컨테이너에 그런 키가 없기 때문. canvas 크기 변경은
씬별 `/scenes/N/canvas/...` 로 들어와야 한다(safety_flags 가 함께 부착됨).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import jsonpatch
import jsonpointer
from pydantic import ValidationError

from src.nl_editor.models import JsonPatchOp
from src.render_spec.models import RenderSpec


# ── 예외 ──────────────────────────────────────────────────────────────────

class PatchApplyError(Exception):
    """patch 적용 또는 RenderSpec 검증 실패.

    `kind` 는 호출자(엔드포인트)가 재시도 여부를 판단할 때 사용:
    - "apply" — jsonpatch 가 적용에서 실패 (path 없음, 인덱스 초과 등)
    - "validate" — 적용은 됐지만 결과가 RenderSpec 스키마 위반
    - "format" — patch 자체가 JsonPatchOp 모델을 만족하지 않음

    호출자는 모든 종류에서 LLM 재시도를 시도할 수 있다.
    """

    def __init__(self, message: str, *, kind: str, details: Any = None):
        super().__init__(message)
        self.kind = kind
        self.details = details


# ── 음수 인덱스 환산 ──────────────────────────────────────────────────────

# `/foo/-3/bar` 형태에서 음수 토큰(`-숫자`)을 찾기 위한 패턴.
_NEG_INDEX_RE = re.compile(r"(?<=/)-(\d+)(?=/|$)")


def _resolve_negative_indices_in_path(path: str, doc: Any) -> str:
    """RFC 6902 표준은 음수 인덱스를 허용하지 않지만, LLM이 종종 "/scenes/-1" 같은
    표기를 만드는 경우가 있어 적용 직전에 양수로 바꿔준다.

    `/scenes/-` 는 RFC 6902 표준 *append* 연산이므로 그대로 둔다(JsonPatchOp.add).
    `/scenes/-1` 같은 음수 인덱스만 해당 시점의 길이로 환산한다.

    환산 도중 path 가 가리키는 리스트 길이를 알 수 없으면 원본 path를 그대로 둔다 —
    jsonpatch 가 자체 에러를 던지도록 한다.
    """
    if "/-" not in path:
        return path
    tokens = path.split("/")
    # 첫 토큰은 항상 빈 문자열(루트). 토큰을 순회하면서 doc 을 따라간다.
    cur: Any = doc
    out_tokens = list(tokens)
    for i in range(1, len(tokens)):
        tok = tokens[i]
        # tok 가 음수 정수일 때만 환산. "-" 단독(append)은 건너뜀.
        if re.fullmatch(r"-\d+", tok) and isinstance(cur, list):
            neg = int(tok)
            length = len(cur)
            resolved = length + neg
            if resolved < 0:
                # 길이 초과 — 그대로 두면 jsonpatch 가 에러를 던진다(올바른 거부).
                return path
            out_tokens[i] = str(resolved)
            # cur 도 그 인덱스로 진입
            try:
                cur = cur[resolved]
            except IndexError:
                return path
            continue
        # RFC 6902 append 토큰 "-" — 더 이상 진입할 수 없는 *말단* 위치이므로
        # 그대로 두고 순회 종료. (예: `/scenes/-1/layers/-` 의 마지막 `-`)
        # 환산 도중 마주치는 경우(드물지만) 안전하게 종료. — #332 발견.
        if tok == "-":
            break
        # 일반 토큰 — cur 갱신
        if isinstance(cur, list):
            try:
                cur = cur[int(tok)]
            except (ValueError, IndexError):
                return path
        elif isinstance(cur, dict):
            cur = cur.get(tok)
        else:
            return path
    return "/".join(out_tokens)


def _resolve_negative_indices(patch_ops: list[dict], doc: Any) -> list[dict]:
    """patch 의 모든 path/from 의 음수 인덱스를 doc 기준으로 환산한다."""
    resolved = []
    for op in patch_ops:
        new_op = dict(op)
        if "path" in new_op and isinstance(new_op["path"], str):
            new_op["path"] = _resolve_negative_indices_in_path(new_op["path"], doc)
        if "from" in new_op and isinstance(new_op["from"], str):
            new_op["from"] = _resolve_negative_indices_in_path(new_op["from"], doc)
        resolved.append(new_op)
    return resolved


# ── 입력 검증 ─────────────────────────────────────────────────────────────

def _patch_ops_to_dicts(patch: list[Any]) -> list[dict]:
    """JsonPatchOp 모델 또는 dict 리스트 → jsonpatch.JsonPatch 가 받는 dict 리스트.

    `from_` alias 처리: Pydantic 직렬화 시 `by_alias=True` 로 `"from"` 키를 노출한다.
    """
    out: list[dict] = []
    for i, op in enumerate(patch):
        if isinstance(op, JsonPatchOp):
            d = op.model_dump(by_alias=True, exclude_none=True)
        elif isinstance(op, dict):
            # dict 인 경우 그대로 사용. 다만 JsonPatchOp 로 한 번 검증해 형식 보장.
            try:
                d = JsonPatchOp.model_validate(op).model_dump(by_alias=True, exclude_none=True)
            except ValidationError as e:
                raise PatchApplyError(
                    f"patch[{i}] 형식이 RFC 6902 JsonPatchOp 스키마를 만족하지 않습니다: {e.errors()[:1]}",
                    kind="format",
                    details=e.errors(),
                )
        else:
            raise PatchApplyError(
                f"patch[{i}] 타입이 dict 또는 JsonPatchOp 가 아닙니다: {type(op).__name__}",
                kind="format",
            )
        out.append(d)
    return out


# ── 핵심: patch 적용 + RenderSpec 재검증 ──────────────────────────────────

def validate_specs_payload(payload: Any) -> list[RenderSpec]:
    """`{"scenes": [...]}` 또는 `[...]` 형태의 dict/list 를 RenderSpec 리스트로 검증한다.

    실패 시 `PatchApplyError(kind="validate")`.
    """
    if isinstance(payload, dict) and "scenes" in payload:
        scenes_raw = payload["scenes"]
    elif isinstance(payload, list):
        scenes_raw = payload
    else:
        raise PatchApplyError(
            "validate: payload 가 list 또는 {'scenes': [...]} 형식이 아닙니다.",
            kind="validate",
            details=type(payload).__name__,
        )

    out: list[RenderSpec] = []
    errors: list[dict] = []
    for i, raw in enumerate(scenes_raw):
        try:
            out.append(RenderSpec.model_validate(raw))
        except ValidationError as e:
            errors.append({"scene_index": i, "errors": e.errors()[:2]})
    if errors:
        raise PatchApplyError(
            f"validate: RenderSpec 스키마 검증 실패 — 씬 {len(errors)}개",
            kind="validate",
            details=errors,
        )
    return out


def apply_patch_to_specs(
    specs: list[RenderSpec] | list[dict] | dict,
    patch: list[JsonPatchOp] | list[dict],
) -> tuple[list[RenderSpec], dict]:
    """patch 를 RenderSpec 리스트에 적용하고, Pydantic 으로 다시 검증한다.

    반환:
    - 검증된 새 RenderSpec 리스트
    - dict 형태의 새 payload (`{"scenes": [...]}`)

    예외: `PatchApplyError(kind=apply|validate|format)`.

    인자:
    - `specs`: RenderSpec 객체 리스트, dict 리스트, 또는 `{"scenes": [...]}` dict 모두 허용.
    - `patch`: JsonPatchOp 객체 또는 RFC 6902 dict 리스트.
    """
    # 1) 입력 → dict payload 로 정규화
    if isinstance(specs, dict) and "scenes" in specs:
        doc = copy.deepcopy(specs)
    elif isinstance(specs, list):
        scenes_dicts: list[dict] = []
        for s in specs:
            if isinstance(s, RenderSpec):
                scenes_dicts.append(json.loads(s.model_dump_json()))
            elif isinstance(s, dict):
                scenes_dicts.append(copy.deepcopy(s))
            else:
                raise PatchApplyError(
                    f"specs 항목이 RenderSpec 또는 dict 가 아닙니다: {type(s).__name__}",
                    kind="format",
                )
        doc = {"scenes": scenes_dicts}
    else:
        raise PatchApplyError(
            "specs 가 list 또는 {'scenes': [...]} dict 형식이 아닙니다.",
            kind="format",
            details=type(specs).__name__,
        )

    # 2) patch 형식 검증 + 음수 인덱스 환산
    patch_dicts = _patch_ops_to_dicts(patch)
    patch_dicts = _resolve_negative_indices(patch_dicts, doc)

    # 3) jsonpatch 적용
    try:
        jp = jsonpatch.JsonPatch(patch_dicts)
        new_doc = jp.apply(doc, in_place=False)
    except (
        jsonpatch.JsonPatchException,
        jsonpatch.InvalidJsonPatch,
        jsonpointer.JsonPointerException,
        KeyError,
        IndexError,
        TypeError,
    ) as e:
        raise PatchApplyError(
            f"apply: JSON Patch 적용 실패 — {e.__class__.__name__}: {e}",
            kind="apply",
            details=str(e),
        )

    # 4) RenderSpec 재검증
    new_specs = validate_specs_payload(new_doc)

    return new_specs, new_doc


__all__ = [
    "PatchApplyError",
    "apply_patch_to_specs",
    "validate_specs_payload",
]
