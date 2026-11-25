"""nl_editor.cache + gemini_client 캐시 통합 회귀 테스트 (#317).

실제 Gemini API 호출 없이 mock 으로 검증한다. 항목:

1. 캐시 환경변수 OFF → cached_content 미사용, 일반 system_instruction 사용.
2. 캐시 환경변수 ON + project_id → 첫 호출에서 캐시 생성, 두 번째 호출에서 같은 캐시 재사용.
3. 캐시 만료 후 재생성.
4. 캐시 생성 실패 시 fallback 일반 호출로 동작 (예외 전파 금지).
5. 캐시 사용 중 generate_fn 예외 → 캐시 무효화 후 일반 호출 fallback.
6. usage_metadata 의 cached/regular 토큰 분리가 meta 에 그대로 전달된다.
7. 정적 프롬프트 버전 변경 시 캐시 재생성 (버전 무효화).
8. 호출 통계(record_call/calls_per_hour) 동작.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from src.nl_editor import prompt as prompt_mod
from src.nl_editor.cache import (
    NlEditorCacheManager,
    get_cache_manager,
    is_cache_enabled,
    reset_cache_manager_for_tests,
)
from src.nl_editor.gemini_client import generate_nl_edit
from src.render_spec.effects import EFFECT_REGISTRY


# ── 픽스처/유틸 ─────────────────────────────────────────────────────────

def _payload() -> dict:
    return {
        "scenes": [
            {
                "version": 1,
                "scene_id": 1,
                "canvas": {"width": 1920, "height": 1080, "fps": 30},
                "duration_sec": 5.0,
                "layers": [
                    {
                        "type": "video",
                        "source": "footage/a.mp4",
                        "source_size": {"width": 1920, "height": 1080},
                        "scale_x": 1.0,
                        "scale_y": 1.0,
                        "playback_rate": 1.0,
                        "rotation_deg": 0,
                        "start_sec": 0.0,
                        "end_sec": 5.0,
                        "opacity": 1.0,
                    },
                    {
                        "type": "text",
                        "text": "원본",
                        "position_x": 960,
                        "position_y": 540,
                        "font_size_px": 48,
                        "font_family": "NotoSansKR",
                        "color": "#FFFFFF",
                        "start_sec": 0.0,
                        "end_sec": 5.0,
                        "opacity": 1.0,
                    },
                ],
            }
        ]
    }


def _valid_response_json(text: str = "x") -> str:
    return json.dumps({
        "patch": [{"op": "replace", "path": "/scenes/0/layers/1/text", "value": text}],
        "summary": "변경",
        "safety_flags": [],
    })


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """싱글톤 매니저를 매 테스트마다 초기화."""
    reset_cache_manager_for_tests()
    # 환경변수도 깨끗하게.
    monkeypatch.delenv("NL_EDITOR_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("NL_EDITOR_CACHE_TTL_SEC", raising=False)
    yield
    reset_cache_manager_for_tests()


def _make_gen_recorder():
    """generate_fn 호출 인자/횟수를 기록하는 mock."""
    calls = []

    def gen(prompt, *, system=None, cached_content=None):
        calls.append({
            "prompt": prompt,
            "system": system,
            "cached_content": cached_content,
        })
        usage = {
            "cached_input_tokens": 5000 if cached_content else 0,
            "regular_input_tokens": 200,
            "output_tokens": 100,
            "total_tokens": 5300 if cached_content else 300,
        }
        return _valid_response_json("ok"), usage
    return gen, calls


def _make_cache_creator(name: str = "cachedContents/test-cache-1", tokens: int = 5000):
    """create_cache_fn mock — 호출 횟수 기록."""
    invocations = []

    def create(system_prompt: str, ttl_seconds: int) -> tuple[str, int]:
        invocations.append({"prompt_len": len(system_prompt), "ttl": ttl_seconds})
        return name, tokens
    return create, invocations


# ── 테스트 본체 ─────────────────────────────────────────────────────────

def test_cache_disabled_by_default():
    """환경변수 없으면 캐시 비활성 — generate_fn 의 cached_content 는 항상 None."""
    assert is_cache_enabled() is False

    gen, calls = _make_gen_recorder()

    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막 바꿔",
        generate_fn=gen,
        project_id="proj-A",
    )
    assert len(calls) == 1
    assert calls[0]["cached_content"] is None
    assert calls[0]["system"] is not None  # 일반 모드 — system_instruction 사용
    assert meta["cache_used"] is False
    assert meta["cached_input_tokens"] == 0


def test_cache_enabled_creates_and_reuses(monkeypatch):
    """환경변수 ON + project_id → 캐시 생성 1회, 두 번째 호출에서 재사용.

    #342: 손익분기 환산식이 1시간 윈도 실측 기반으로 바뀌면서, 기본값
    (`min_samples=2`, `threshold=4`) 환경에서는 2호출 누적 시 cph=2 < 4 가 되어
    자동 해제 분기가 발동한다. 본 테스트의 의도는 "캐시 재사용 경로 검증" 이므로
    자동 해제 기능을 명시적으로 OFF(`threshold=0`) 하여 의도를 격리한다.
    """
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "0")
    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    _, meta1 = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="첫 명령",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id="proj-cache-1",
    )
    _, meta2 = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="두 번째 명령",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id="proj-cache-1",
    )
    # 캐시는 1회만 생성, 두 호출 모두 같은 캐시 이름 재사용
    assert len(creates) == 1
    assert len(calls) == 2
    assert calls[0]["cached_content"] == "cachedContents/test-cache-1"
    assert calls[1]["cached_content"] == "cachedContents/test-cache-1"
    # 캐시 사용 시 system_instruction 은 보내지 않는다
    assert calls[0]["system"] is None
    assert calls[1]["system"] is None
    # meta 의 cache_used / 토큰 분리 확인
    assert meta1["cache_used"] is True
    assert meta1["cached_input_tokens"] > 0
    assert meta2["cache_used"] is True
    assert meta2["cache_storage_tokens"] == 5000


def test_cache_create_failure_fallback(monkeypatch):
    """캐시 생성이 예외를 던지면 일반 호출로 fallback (예외 전파 금지)."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    gen, calls = _make_gen_recorder()

    def failing_create(system_prompt: str, ttl_seconds: int) -> tuple[str, int]:
        raise RuntimeError("min token 미달 또는 권한 오류")

    resp, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=failing_create,
        project_id="proj-cache-fail",
    )
    assert resp.patch[0].value == "ok"
    assert calls[0]["cached_content"] is None  # fallback → 일반 호출
    assert calls[0]["system"] is not None
    assert meta["cache_used"] is False


def test_cache_call_failure_fallback(monkeypatch):
    """캐시 호출 시 generate_fn 이 예외 → 캐시 무효화 + 일반 호출 fallback."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")

    state = {"cache_attempts": 0, "fallback_attempts": 0}

    def gen(prompt, *, system=None, cached_content=None):
        if cached_content is not None:
            state["cache_attempts"] += 1
            raise RuntimeError("cache not found")
        state["fallback_attempts"] += 1
        return _valid_response_json("after-fallback"), {
            "cached_input_tokens": 0,
            "regular_input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }

    create_fn, _ = _make_cache_creator()
    resp, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id="proj-cache-runtime-fail",
    )
    assert resp.patch[0].value == "after-fallback"
    assert state["cache_attempts"] == 1
    assert state["fallback_attempts"] >= 1
    assert meta["cache_used"] is False  # 최종 호출이 일반 호출이므로 False
    # 캐시는 무효화됐어야 한다 (peek = None)
    assert get_cache_manager().peek("proj-cache-runtime-fail") is None


def test_cache_token_breakdown_in_meta(monkeypatch):
    """usage_metadata 의 cached/regular 토큰이 meta 로 그대로 전달된다."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    create_fn, _ = _make_cache_creator(tokens=6000)

    def gen(prompt, *, system=None, cached_content=None):
        return _valid_response_json("ok"), {
            "cached_input_tokens": 5800,
            "regular_input_tokens": 250,
            "output_tokens": 180,
            "total_tokens": 6230,
        }

    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id="proj-tokens",
    )
    assert meta["cached_input_tokens"] == 5800
    assert meta["regular_input_tokens"] == 250
    assert meta["output_tokens"] == 180
    assert meta["cache_storage_tokens"] == 6000


def test_static_prompt_version_invalidates_cache(monkeypatch):
    """정적 프롬프트 버전이 바뀌면 기존 캐시는 폐기되고 새로 생성된다.

    #342: 손익분기 자동 해제 분기와의 간섭을 피하기 위해 임계값 0 (OFF).
    """
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "0")
    gen, _ = _make_gen_recorder()

    create_calls = {"count": 0, "names": []}

    def create(system_prompt: str, ttl_seconds: int) -> tuple[str, int]:
        create_calls["count"] += 1
        nm = f"cachedContents/v{create_calls['count']}"
        create_calls["names"].append(nm)
        return nm, 5000

    pid = "proj-version"

    # 1회차 — 첫 캐시 생성.
    generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="x1",
        generate_fn=gen,
        create_cache_fn=create,
        project_id=pid,
    )
    assert create_calls["count"] == 1

    # 정적 프롬프트 버전 변경 시뮬레이션.
    monkeypatch.setattr(prompt_mod, "STATIC_PROMPT_VERSION", "v-bumped", raising=True)
    # cache 모듈에서 import 한 상수도 patch.
    monkeypatch.setattr("src.nl_editor.cache.STATIC_PROMPT_VERSION", "v-bumped", raising=True)

    generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="x2",
        generate_fn=gen,
        create_cache_fn=create,
        project_id=pid,
    )
    assert create_calls["count"] == 2


def test_cache_expiry_triggers_recreation(monkeypatch):
    """TTL 만료 → 다음 호출에서 캐시 재생성.

    #342: 손익분기 자동 해제 분기와의 간섭을 피하기 위해 임계값 0 (OFF).
    """
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    monkeypatch.setenv("NL_EDITOR_CACHE_TTL_SEC", "120")  # 2분 (안전마진 60초)
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "0")
    gen, _ = _make_gen_recorder()

    create_count = {"n": 0}

    def create(system_prompt: str, ttl_seconds: int) -> tuple[str, int]:
        create_count["n"] += 1
        return f"cachedContents/exp-{create_count['n']}", 5000

    pid = "proj-expiry"

    generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="x1",
        generate_fn=gen,
        create_cache_fn=create,
        project_id=pid,
    )
    assert create_count["n"] == 1

    # 매니저 내부 created_at 을 과거로 변경 → 만료 강제.
    mgr = get_cache_manager()
    entry = mgr.peek(pid)
    assert entry is not None
    # 직접 entry 조작 — 만료 시뮬레이션
    with mgr._lock:
        e = mgr._entries[pid]
        e.created_at = time.time() - 999_999

    generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="x2",
        generate_fn=gen,
        create_cache_fn=create,
        project_id=pid,
    )
    assert create_count["n"] == 2


def test_call_statistics_record_and_count(monkeypatch):
    """record_call → calls_per_hour 환산 (#342: 1시간 윈도 실측).

    이전 #340 구현은 10분 윈도 × 6 환산식을 사용했으나, samples ≥ 5 와 cph < 4 가
    동시 만족 불가한 모순이 있었다. #342 에서 윈도를 1시간으로 늘려 cph 가 곧
    1시간 내 호출 수와 같도록 보정.
    """
    mgr = NlEditorCacheManager()
    pid = "proj-stat"
    assert mgr.calls_per_hour(pid) == 0
    for _ in range(5):
        mgr.record_call(pid)
    cph = mgr.calls_per_hour(pid)
    # 1시간 윈도 내 5건 호출 → cph = 5
    assert cph == 5.0
    assert mgr.sample_count(pid) == 5


def test_break_even_skips_cache_when_rate_low(monkeypatch):
    """손익분기 미달 (시간당 호출 수 < 임계값) 시 캐시 생성을 건너뛴다 (#340).

    표본을 5건 이상 쌓되 시간당 환산값이 임계값(기본 4)보다 낮은 상황을 만든다.
    `record_call`은 매 호출당 한 번 기록되므로, 사전에 record_call 로 6건을 미리 쌓고
    아무 캐시 없이 한 번 호출하면 손익분기 미달로 캐시가 생성되지 않아야 한다.
    """
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    # 임계값을 매우 높게 잡아서 어떤 경우에도 미달이 되도록 강제.
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "10000")
    monkeypatch.setenv("NL_EDITOR_CACHE_MIN_SAMPLES", "3")

    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    pid = "proj-break-even-skip"

    # 표본 신뢰성을 위해 사전에 3건 이상 호출 기록을 쌓아둔다.
    mgr = get_cache_manager()
    for _ in range(3):
        mgr.record_call(pid)

    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    # 손익분기 미달 → 캐시 미생성, 일반 호출 경로
    assert len(creates) == 0
    assert calls[0]["cached_content"] is None
    assert calls[0]["system"] is not None
    assert meta["cache_used"] is False


def test_break_even_releases_existing_cache_when_rate_drops(monkeypatch):
    """기존 캐시가 있었더라도 빈도가 임계값 미만으로 떨어지면 즉시 해제한다 (#340)."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    # 1회차에는 임계값 0 (자동 해제 OFF) 로 캐시를 정상 생성.
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "0")
    monkeypatch.setenv("NL_EDITOR_CACHE_MIN_SAMPLES", "1")

    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator(name="cachedContents/before-release")

    pid = "proj-break-even-release"

    _, meta1 = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="첫 명령",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    assert meta1["cache_used"] is True
    assert len(creates) == 1
    assert get_cache_manager().peek(pid) is not None

    # 2회차 — 임계값을 매우 높게 잡아 손익분기 미달 상태 강제.
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "10000")

    # 원격 delete_fn 호출을 추적하기 위해 release_project_cache 내부 _default_delete_cache
    # 를 monkeypatch (실제 SDK 호출 방지).
    deleted_names: list[str] = []

    def fake_delete(name: str) -> None:
        deleted_names.append(name)

    monkeypatch.setattr(
        "src.nl_editor.gemini_client._default_delete_cache", fake_delete
    )

    _, meta2 = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="두 번째 명령",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    # 2회차 호출은 일반 모드여야 한다.
    assert meta2["cache_used"] is False
    assert calls[-1]["cached_content"] is None
    # 활성 캐시가 해제됐고 원격 delete_fn 도 호출됐다.
    assert deleted_names == ["cachedContents/before-release"]
    assert get_cache_manager().peek(pid) is None


def test_break_even_skips_evaluation_until_min_samples(monkeypatch):
    """표본 수가 부족하면 (첫 호출들) 손익분기 평가를 보류하고 캐시를 유지한다 (#340)."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "10000")  # 매우 높게
    monkeypatch.setenv("NL_EDITOR_CACHE_MIN_SAMPLES", "5")  # 5건 미만이면 평가 보류

    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    pid = "proj-min-samples"

    # 첫 호출 — 표본은 1건. 임계값이 매우 높지만 표본 부족으로 평가 보류 → 캐시 생성.
    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="첫 명령",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    assert meta["cache_used"] is True
    assert len(creates) == 1


def test_break_even_disabled_when_threshold_zero(monkeypatch):
    """임계값 0 (또는 음수) 이면 자동 해제 기능 OFF — 캐시를 항상 사용한다 (#340)."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    monkeypatch.setenv("NL_EDITOR_CACHE_BREAK_EVEN_CALLS_PER_HOUR", "0")
    monkeypatch.setenv("NL_EDITOR_CACHE_MIN_SAMPLES", "1")

    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    pid = "proj-break-even-off"

    # 사전에 표본을 충분히 쌓아도 (또는 안 쌓아도) 임계값 0 이므로 캐시는 정상 생성.
    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    assert meta["cache_used"] is True
    assert len(creates) == 1


def test_break_even_triggers_with_default_thresholds_low_freq(monkeypatch):
    """#342: 기본값 운영 환경에서 저빈도 사용자(2~3 rph)가 자동 해제 대상이 되는지 검증.

    #340 구현은 환산식(`samples × 6`)과 표본 조건(`samples ≥ 5`)이 수학적으로
    모순되어 기본값에서 자동 해제가 영원히 발동하지 않았다. #342 보정으로
    1시간 윈도 실측 cph 를 사용하므로, 2~3 rph 사용자가 실제로 해제 대상이 된다.

    시나리오: 환경변수 미설정(기본 threshold=4, min_samples=2). 1시간 내 3건 호출 누적.
    cph=3 < 4 AND samples=3 >= 2 → 자동 해제 트리거.
    """
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    # threshold/min_samples 모두 미설정 → 기본값(4, 2) 사용.

    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    pid = "proj-default-low-freq"
    mgr = get_cache_manager()
    # 사전에 2건 호출 기록 — 1시간 윈도 내에 머무름. generate_nl_edit 이 1건 더 추가하여
    # 총 samples=3, cph=3 < 4 → 자동 해제 트리거.
    for _ in range(2):
        mgr.record_call(pid)

    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    # 기본값에서 자동 해제 발동 — 캐시 미생성, 일반 호출 경로.
    assert len(creates) == 0
    assert calls[0]["cached_content"] is None
    assert calls[0]["system"] is not None
    assert meta["cache_used"] is False


def test_break_even_keeps_cache_with_default_thresholds_high_freq(monkeypatch):
    """#342: 기본값 운영 환경에서 고빈도 사용자(>= 4 rph)는 캐시 유지.

    cph=5 >= 4 → 자동 해제 미발동 → 캐시 정상 생성.
    """
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    # 기본값 사용.

    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    pid = "proj-default-high-freq"
    mgr = get_cache_manager()
    # 사전에 5건 호출 기록 — cph=5 >= 4 → 자동 해제 미발동.
    for _ in range(5):
        mgr.record_call(pid)

    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    # 손익분기 충족 → 캐시 생성 및 사용.
    assert len(creates) == 1
    assert meta["cache_used"] is True


def test_call_window_expires_old_timestamps(monkeypatch):
    """#342: 1시간 이전 호출 기록은 윈도에서 자동 제거된다."""
    mgr = NlEditorCacheManager()
    pid = "proj-window-expiry"

    # time.time() 을 우회하여 과거 타임스탬프를 직접 큐에 주입.
    now = time.time()
    with mgr._lock:
        q = mgr._recent_calls.setdefault(pid, [])
        # 1시간 + 1분 전 호출 2건 (윈도 밖) + 30분 전 호출 1건 (윈도 안)
        q.append(now - 3700)
        q.append(now - 3650)
        q.append(now - 1800)

    # 새 호출 1건 추가 → 윈도 정리 트리거.
    mgr.record_call(pid)

    # 윈도 밖 2건은 제거, 윈도 안 1건 + 새 1건 = 총 2건이어야 한다.
    assert mgr.sample_count(pid) == 2
    assert mgr.calls_per_hour(pid) == 2.0


def test_break_even_min_samples_boundary(monkeypatch):
    """#342: 기본 min_samples=2 경계 — samples=1 일 때는 평가 보류, 2 일 때는 평가 시작."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    # 기본값(threshold=4, min_samples=2) 사용.

    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    pid = "proj-boundary"
    mgr = get_cache_manager()

    # 사전 호출 1건 — generate_nl_edit 이 record_call 을 추가로 1회 호출하여
    # 총 samples=2 가 되며 그 시점에 평가됨. cph=2 < 4 → 자동 해제 트리거.
    mgr.record_call(pid)

    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=pid,
    )
    # min_samples=2 도달 + cph=2 < 4 → 자동 해제.
    assert len(creates) == 0
    assert meta["cache_used"] is False


def test_no_project_id_disables_cache(monkeypatch):
    """project_id 가 없으면 캐시 옵트인이 켜져 있어도 캐시는 사용되지 않는다."""
    monkeypatch.setenv("NL_EDITOR_CACHE_ENABLED", "1")
    gen, calls = _make_gen_recorder()
    create_fn, creates = _make_cache_creator()

    _, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=gen,
        create_cache_fn=create_fn,
        project_id=None,
    )
    assert len(creates) == 0
    assert calls[0]["cached_content"] is None
    assert meta["cache_used"] is False
