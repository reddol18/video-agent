"""자연어 RenderSpec 편집 엔진 (Phase 7, #315).

사용자가 "마지막 씬 좀 더 밝게", "BGM 작게" 같은 *자연어 명령*을 입력하면 Gemini 2.5
Flash가 현재 RenderSpec과 효과 카탈로그를 보고 **RFC 6902 JSON Patch**를 생성한다.
그 patch를 RenderSpec(또는 RenderSpec list)에 적용하여 렌더 파이프라인에 반영한다.

설계 핵심
--------
LLM이 ffmpeg 명령을 직접 만들지 *않는다*. "정규화된 JSON(RenderSpec)" → "정규화된 JSON
(RenderSpec)" 변환만 수행하므로 모델 호출이 짧고(=저렴/빠르고), Pydantic 스키마 검증으로
잘못된 응답을 즉시 reject할 수 있다(=안전).

본 패키지는 *백엔드 엔진*만 다룬다. UI(승인 다이얼로그, 미리보기 패널)는 후속 이슈
(#316), 컨텍스트 캐싱 최적화는 #317에서 처리한다.

공개 모듈
--------
- `models` — 요청/응답 Pydantic 모델 (`NlEditRequest`, `NlEditResponse`, `JsonPatchOp` 등)
- `prompt` — 시스템 프롬프트 빌더 + few-shot 예시 (카테고리별)
- `gemini_client` — Gemini 2.5 Flash 호출 래퍼 (LLM 호출은 외부에서 mock 주입 가능)
- `patcher` — JSON Patch 적용 + Pydantic 재검증
- `safety` — 위험 명령 감지 (전체 layer 삭제, canvas 크기 변경 등)
"""

from src.nl_editor.models import (
    JsonPatchOp,
    NlEditApplyRequest,
    NlEditApplyResponse,
    NlEditRequest,
    NlEditResponse,
    NlEditTurn,
    NlEditUndoResponse,
    SafetyFlag,
)
from src.nl_editor.patcher import (
    PatchApplyError,
    apply_patch_to_specs,
    validate_specs_payload,
)
from src.nl_editor.prompt import (
    build_few_shot_examples,
    build_system_prompt,
    catalog_to_compact_json,
)
from src.nl_editor.safety import detect_safety_flags

__all__ = [
    # models
    "JsonPatchOp",
    "NlEditRequest",
    "NlEditResponse",
    "NlEditApplyRequest",
    "NlEditApplyResponse",
    "NlEditUndoResponse",
    "NlEditTurn",
    "SafetyFlag",
    # patcher
    "PatchApplyError",
    "apply_patch_to_specs",
    "validate_specs_payload",
    # prompt
    "build_system_prompt",
    "build_few_shot_examples",
    "catalog_to_compact_json",
    # safety
    "detect_safety_flags",
]
