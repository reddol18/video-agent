"""자연어 편집 — Gemini 2.5 Flash 호출 래퍼 (#315, #317 캐싱 확장).

핵심 설계
--------
1. **재시도 + 검증 루프** — LLM 응답이 JSON 형식을 깨거나 patch 적용에 실패하면
   최대 `MAX_RETRY` 회 재시도한다. 매 시도마다 직전 실패 사유를 프롬프트에 덧붙여
   모델이 같은 실수를 반복하지 않도록 유도한다.
2. **호출 함수 주입 가능** — 테스트에서 실제 Gemini API 호출을 mock 으로 대체할 수
   있도록 `generate_fn` 인자를 받는다. 기본값은 `src.utils.gemini_client.generate_json_with_usage`.
3. **컨텍스트 캐싱(#317)** — 시스템 프롬프트의 정적 블록(스키마 + 카탈로그 + few-shot)을
   `NlEditorCacheManager` 로 캐시하여 호출당 입력 단가 90% 인하. 옵트인 환경변수
   `NL_EDITOR_CACHE_ENABLED=1` 로만 활성화되고, 캐시 생성 실패 시 자동 폴백.
4. **비용 추정** — usage_metadata 의 캐시/일반 입력 토큰 수를 그대로 호출자에게 전달.
   호출자는 `cost_estimator.record_nl_edit(cached_input_tokens=..., regular_input_tokens=...)`
   로 누적한다.

캐시 동작 모드
-------------
| 모드 | 환경변수 | 호출 시 동작 |
|------|---------|-------------|
| 캐시 OFF | `NL_EDITOR_CACHE_ENABLED=0` (기본) | system_instruction 으로 매 호출 송신 |
| 캐시 ON, 캐시 성공 | `NL_EDITOR_CACHE_ENABLED=1` | cached_content 재사용 (입력 -90%) |
| 캐시 ON, 캐시 실패 | 동일 | 일반 호출로 fallback, 로그만 남김 |
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from src.cost_estimator import load_pricing
from src.nl_editor.cache import (
    CacheEntry,
    get_cache_manager,
    is_cache_enabled,
)
from src.nl_editor.models import JsonPatchOp, NlEditResponse, NlEditTurn, SafetyFlag
from src.nl_editor.patcher import (
    PatchApplyError,
    apply_patch_to_specs,
)
from src.nl_editor.prompt import (
    build_dynamic_prompt_block,
    build_static_prompt_block,
    build_system_prompt,
)
from src.nl_editor.safety import detect_safety_flags

logger = logging.getLogger(__name__)


# ── 기본 호출 함수 ────────────────────────────────────────────────────────

MAX_RETRY = 2  # 첫 호출 + 재시도 2회 → 총 3회

# Gemini Flash 호출 1회당 평균 비용을 미리 측정해두기 위한 상수 (실패 시 폴백용)
_FALLBACK_INPUT_USD = 0.0003
_FALLBACK_OUTPUT_USD = 0.0010


# `generate_fn` 시그니처
# - in: (prompt, *, system=None, cached_content=None) → (text, usage_dict)
# - usage_dict: {"cached_input_tokens": int, "regular_input_tokens": int,
#                "output_tokens": int, "total_tokens": int}
GenerateFn = Callable[..., tuple[str, dict]]


def _default_generate(
    prompt: str,
    *,
    system: Optional[str] = None,
    cached_content: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[str, dict]:
    """기본 LLM 호출 — `src.utils.gemini_client.generate_json_with_usage` 위임.

    테스트에서는 이 함수 대신 mock 을 주입한다.
    `model` 미지정 시 utils 측 폴백 체인을 사용.
    """
    from src.utils.gemini_client import generate_json_with_usage
    return generate_json_with_usage(
        prompt, system=system, cached_content=cached_content, model=model,
    )


def _default_create_cache(
    system_prompt: str, ttl_seconds: int, model: Optional[str] = None,
) -> tuple[str, int]:
    from src.utils.gemini_client import create_cached_content
    return create_cached_content(system_prompt, ttl_seconds=ttl_seconds, model=model)


def _default_delete_cache(name: str) -> None:
    from src.utils.gemini_client import delete_cached_content
    delete_cached_content(name)


# ── LLM 응답 파싱 ─────────────────────────────────────────────────────────

def _parse_response_text(raw: str) -> NlEditResponse:
    """LLM이 돌려준 raw 문자열을 `NlEditResponse` 로 파싱.

    Gemini가 JSON mime 모드라도 가끔 앞뒤에 ```json``` fence 를 붙이는 경우가 있어
    유연하게 처리한다.
    """
    if raw is None:
        raise PatchApplyError("LLM 응답이 비어있습니다.", kind="format")
    text = raw.strip()
    # ```json ... ``` fence 제거
    if text.startswith("```"):
        # 첫 줄과 마지막 줄을 제거
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PatchApplyError(f"LLM 응답이 JSON 파싱 실패: {e}", kind="format", details=text[:300])

    # 기본 필드 보정 — LLM 누락 대비
    data.setdefault("patch", [])
    data.setdefault("summary", "")
    data.setdefault("safety_flags", [])

    try:
        return NlEditResponse.model_validate(data)
    except Exception as e:
        raise PatchApplyError(f"LLM 응답이 NlEditResponse 스키마 실패: {e}", kind="format")


# ── 비용 추정 ─────────────────────────────────────────────────────────────

def _estimate_usd(input_chars: int, output_chars: int, model: Optional[str] = None) -> float:
    """Gemini 텍스트 호출 1회 비용 추정(폴백용 — 토큰 미가용 시).

    `model` 미지정 시 default_model 단가 사용.
    """
    try:
        from src.cost_estimator import get_gemini_model_pricing
        g = get_gemini_model_pricing(model)
        chars_per_token = g["chars_per_token_korean"]
        in_tokens = input_chars / chars_per_token
        out_tokens = output_chars / chars_per_token
        return (
            in_tokens / 1_000_000 * g["input_per_1m_tokens_usd"]
            + out_tokens / 1_000_000 * g["output_per_1m_tokens_usd"]
        )
    except Exception:
        return _FALLBACK_INPUT_USD + _FALLBACK_OUTPUT_USD


def _estimate_usd_from_tokens(
    cached_input_tokens: int,
    regular_input_tokens: int,
    output_tokens: int,
    model: Optional[str] = None,
) -> float:
    """토큰 수 기반 정확 비용 — usage_metadata 가 있을 때 사용.

    `model` 미지정 시 default_model 단가 사용.
    """
    try:
        from src.cost_estimator import get_gemini_model_pricing
        g = get_gemini_model_pricing(model)
        return (
            cached_input_tokens / 1_000_000 * g.get("cached_input_per_1m_tokens_usd", 0.03)
            + regular_input_tokens / 1_000_000 * g["input_per_1m_tokens_usd"]
            + output_tokens / 1_000_000 * g["output_per_1m_tokens_usd"]
        )
    except Exception:
        return _FALLBACK_INPUT_USD + _FALLBACK_OUTPUT_USD


# ── 공개 API: 미리보기 (patch 생성) ──────────────────────────────────────

def generate_nl_edit(
    *,
    effect_registry: dict[str, Any],
    render_spec_payload: Any,
    user_instruction: str,
    history: Optional[list[NlEditTurn]] = None,
    generate_fn: Optional[GenerateFn] = None,
    create_cache_fn: Optional[Callable[..., tuple[str, int]]] = None,
    project_id: Optional[str] = None,
    max_retry: int = MAX_RETRY,
    model: Optional[str] = None,
) -> tuple[NlEditResponse, dict]:
    """자연어 명령 → patch + summary + 안전 플래그 생성.

    인자:
    - `effect_registry`: `EFFECT_REGISTRY` (dict).
    - `render_spec_payload`: 현재 RenderSpec 리스트 (`{"scenes": [...]}` 또는 list[dict]).
    - `user_instruction`: 자연어 명령.
    - `history`: 직전 N턴 (선택).
    - `generate_fn`: LLM 호출 함수 (테스트용 주입). 기본은 Gemini 2.5 Flash.
      시그니처: `(prompt, *, system=None, cached_content=None) -> (text, usage_dict)`.
    - `create_cache_fn`: 캐시 생성 함수 (테스트용 주입). 시그니처: `(system_prompt, ttl) -> (name, tokens)`.
    - `project_id`: 프로젝트 ID — 캐시 키 구성에 사용. None 이면 캐시 비활성.
    - `max_retry`: 검증 실패 시 재시도 횟수(첫 호출 제외).

    반환:
    - `NlEditResponse` — patch 가 적용 검증까지 통과한 응답.
    - 메타 정보 dict:
      ```
      {
        "attempts": int,
        "estimated_cost_usd": float,
        "input_chars": int,                # 동적 블록 + (캐시 비사용 시 정적 블록) 글자수
        "output_chars": int,
        "cache_used": bool,                # 실제 캐시가 호출에 적용됐는지
        "cached_input_tokens": int,        # usage_metadata 기반, 없으면 0
        "regular_input_tokens": int,
        "output_tokens": int,
        "cache_storage_tokens": int,       # 활성 캐시의 저장 비용 산정용 토큰 수
        "cache_age_seconds": float,        # 활성 캐시 나이 (없으면 0)
      }
      ```

    `PatchApplyError` 가 `max_retry+1` 회 연속 발생하면 마지막 예외를 그대로 raise.
    """
    if generate_fn is None:
        generate_fn = _default_generate
    if create_cache_fn is None:
        create_cache_fn = _default_create_cache

    history_dicts = [t.model_dump() for t in (history or [])]

    # 1) 정적/동적 블록 분리. 캐시 사용 가능 여부 판단.
    static_prompt = build_static_prompt_block(effect_registry=effect_registry)
    dynamic_block = build_dynamic_prompt_block(
        render_spec_json=render_spec_payload,
        history=history_dicts,
        user_instruction=user_instruction,
    )

    # 2) 호출 통계 기록을 ensure 보다 먼저 수행 — 이번 호출이 빈도 계산에 포함되어야
    #    손익분기 평가가 현재 시점 부하를 정확히 반영한다 (#340).
    if project_id:
        get_cache_manager().record_call(project_id)

    # 3) 캐시 확보 시도 (옵트인 + project_id 필수).
    #    손익분기 미달 시 자동 해제 (#340) — 시간당 호출 빈도가 임계값보다 낮으면
    #    저장 비용(8.3원/시간) > 절감액(2.2원/회) 이 되어 순손실. 기존 캐시를 명시 무효화하고
    #    이번 호출은 일반 호출로 진행한다. 임계값 회복 시 다음 호출에서 ensure 가 다시 생성.
    cache_entry: Optional[CacheEntry] = None
    if project_id and is_cache_enabled():
        mgr = get_cache_manager()
        if mgr.should_skip_for_break_even(project_id):
            existing = mgr.peek(project_id)
            cph = mgr.calls_per_hour(project_id)
            if existing is not None:
                # 활성 캐시가 남아있으면 저장 비용을 즉시 줄이기 위해 원격까지 삭제 시도.
                # delete_fn 미지정 시 기본 SDK 사용.
                release_project_cache(project_id)
                logger.info(
                    "[nl_editor.cache] 손익분기 미달로 캐시 자동 해제 project=%s rate=%.1f rph",
                    project_id, cph,
                )
            else:
                logger.info(
                    "[nl_editor.cache] 손익분기 미달로 캐시 생성 보류 project=%s rate=%.1f rph",
                    project_id, cph,
                )
            # cache_entry 를 None 으로 유지 → 일반 호출 경로.
        else:
            # 캐시 매니저는 create_fn(system_prompt, ttl) 시그니처를 기대 — model 을
            # 클로저로 묶어서 넣는다. 모델별 캐시를 별도로 유지하려면 캐시 매니저
            # 키에 model 까지 포함해야 하지만, 1차 구현 범위는 전역 모델 하나이므로
            # 매니저 측 변경 없이 클로저로 model 전달만 추가한다.
            _create_with_model = (
                (lambda sp, ttl, _m=model: create_cache_fn(sp, ttl, model=_m))
                if model is not None else create_cache_fn
            )
            cache_entry = mgr.ensure(
                project_id=project_id,
                system_prompt=static_prompt,
                create_fn=_create_with_model,
            )

    last_error: Optional[PatchApplyError] = None
    total_input_chars = 0
    total_output_chars = 0
    total_cached_input_tokens = 0
    total_regular_input_tokens = 0
    total_output_tokens = 0
    cache_used_any = False
    attempts = 0

    for attempt in range(max_retry + 1):
        attempts += 1
        # 재시도 시 직전 실패 사유를 보강 메시지로 덧붙인다.
        instr = user_instruction
        if last_error is not None:
            instr = (
                f"이전 시도가 실패했습니다 ({last_error.kind}): {str(last_error)}\n"
                f"동일한 명령을 *유효한 JSON Patch* 로 다시 생성하세요.\n\n"
                f"사용자 명령: {user_instruction}"
            )
            # 동적 블록도 재시도 사유 포함하여 다시 만든다.
            dynamic_block = build_dynamic_prompt_block(
                render_spec_json=render_spec_payload,
                history=history_dicts,
                user_instruction=instr,
            )

        # 4) 캐시 사용 / 일반 호출 분기.
        # 캐시 미사용 모드에서는 전체 시스템 프롬프트(정적+동적의 system부분 없이)와 사용자 instr 분리:
        # 기존 구현 호환 — system_instruction 에 static, prompt 에 dynamic+instr 를 넣는다.
        # 캐시 사용 모드에서는 cached_content 에 static 이 들어있으므로 prompt 만 보낸다.
        try:
            if cache_entry is not None:
                text, usage = generate_fn(
                    dynamic_block,
                    cached_content=cache_entry.name,
                    model=model,
                )
                cache_used_any = True
                # 입력 글자수 — 동적 블록만 (정적 블록은 캐시로 전송 안 함)
                step_input_chars = len(dynamic_block)
            else:
                text, usage = generate_fn(
                    dynamic_block,
                    system=static_prompt,
                    model=model,
                )
                step_input_chars = len(static_prompt) + len(dynamic_block)
        except PatchApplyError:
            raise
        except Exception as e:
            # 캐시 사용 시 cache_not_found 또는 expired 가 흔한 실패 — fallback 한 번 시도.
            if cache_entry is not None:
                logger.warning(
                    "[nl_editor] 캐시 호출 실패 → 일반 호출로 fallback. err=%s", e,
                )
                cache_entry = None  # 이번 attempt 부터는 캐시 미사용
                # 매니저에서도 무효화 (다음 ensure 가 새로 만든다)
                if project_id:
                    get_cache_manager().invalidate(project_id, delete_fn=None)
                try:
                    text, usage = generate_fn(
                        dynamic_block,
                        system=static_prompt,
                        model=model,
                    )
                    step_input_chars = len(static_prompt) + len(dynamic_block)
                except Exception:
                    raise
            else:
                # 캐시 미사용에서 또 실패한 경우 → 그대로 상위 전파.
                raise

        total_input_chars += step_input_chars
        total_output_chars += len(text or "")
        total_cached_input_tokens += int(usage.get("cached_input_tokens", 0) or 0)
        total_regular_input_tokens += int(usage.get("regular_input_tokens", 0) or 0)
        total_output_tokens += int(usage.get("output_tokens", 0) or 0)

        try:
            parsed = _parse_response_text(text)
        except PatchApplyError as e:
            last_error = e
            logger.warning(f"[nl_editor] attempt={attempt+1} 파싱 실패: {e}")
            continue

        # patch 적용 dry-run — 실제 RenderSpec list 에 적용해보고 검증.
        try:
            apply_patch_to_specs(render_spec_payload, parsed.patch)
        except PatchApplyError as e:
            last_error = e
            logger.warning(f"[nl_editor] attempt={attempt+1} 적용/검증 실패: {e}")
            continue

        # 백엔드 측 safety 보강 — LLM이 비워 보냈을 가능성에 대비.
        backend_flags = detect_safety_flags(parsed.patch)
        merged: list[SafetyFlag] = []
        for f in list(parsed.safety_flags) + backend_flags:
            if f not in merged:
                merged.append(f)
        parsed.safety_flags = merged

        # 비용 계산 — 토큰 수가 있으면 정확 계산, 없으면 글자수 추정.
        if total_cached_input_tokens or total_regular_input_tokens or total_output_tokens:
            parsed.estimated_cost_usd = _estimate_usd_from_tokens(
                total_cached_input_tokens, total_regular_input_tokens, total_output_tokens,
                model=model,
            )
        else:
            parsed.estimated_cost_usd = _estimate_usd(
                total_input_chars, total_output_chars, model=model,
            )

        # 실제로 사용된 모델 ID — meta 에 명시. None 이면 default_model 로 폴백된 결과.
        try:
            from src.cost_estimator import get_gemini_default_model
            used_model = model or get_gemini_default_model()
        except Exception:
            used_model = model or "gemini-2.5-flash"

        meta = {
            "attempts": attempts,
            "estimated_cost_usd": parsed.estimated_cost_usd,
            "input_chars": total_input_chars,
            "output_chars": total_output_chars,
            "cache_used": cache_used_any,
            "cached_input_tokens": total_cached_input_tokens,
            "regular_input_tokens": total_regular_input_tokens,
            "output_tokens": total_output_tokens,
            "cache_storage_tokens": cache_entry.token_count if cache_entry else 0,
            "cache_age_seconds": cache_entry.age_seconds if cache_entry else 0.0,
            "model": used_model,
        }
        return parsed, meta

    # 모든 시도 실패
    assert last_error is not None
    raise last_error


def release_project_cache(
    project_id: str,
    delete_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """세션 종료 시 프로젝트 캐시 명시 해제 (`#317` 저장 비용 절감).

    `delete_fn` 미지정 시 기본 SDK 삭제 사용.
    """
    if delete_fn is None:
        delete_fn = _default_delete_cache
    get_cache_manager().invalidate(project_id, delete_fn=delete_fn)


__all__ = [
    "MAX_RETRY",
    "generate_nl_edit",
    "release_project_cache",
]
