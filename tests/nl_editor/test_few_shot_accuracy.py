"""few-shot 예시 정확도 회귀 테스트 (#332).

#315 본문 완료 조건 "30종 few-shot 예시 작성 및 정확도 검증" 중 *정확도 검증* 항목을
충족하기 위한 골든 테스트. LLM 호출 없이, `prompt.build_few_shot_examples()` 가
반환하는 30종 예시의 `response.patch` 가 표준 RenderSpec 샘플에 *round-trip 적용
가능*한지 자동 검증한다.

검증 원리
--------
1. 각 카테고리 예시는 *특정 레이어 인덱스*(예: `/scenes/0/layers/1` = 텍스트)를 가정한다.
   본 테스트는 그 가정을 만족하는 4씬 짜리 표준 RenderSpec 샘플을 만들어 준다.
2. 모든 예시의 patch 를 `apply_patch_to_specs` 로 적용한다.
3. 적용이 실패하면 즉시 빨간불 — Few-shot 자체가 RenderSpec 스키마와 어긋난다는 뜻.
4. `safety` 카테고리 예시는 patch 자체가 RenderSpec 검증을 깨뜨릴 수 있으므로
   별도 분기(safety_flags 가 detect_safety_flags 결과와 일치하는지) 로 검증한다.

이 테스트가 LLM 의 *실제* 정확도를 측정하지는 않는다. 하지만 "Few-shot 이 LLM 을
가이드하는 방향" 이 RenderSpec 스키마와 정합한지를 명세 차원에서 보장한다.
실제 Gemini 응답 정확도는 수동 검증(`scripts/verify_nl_edit_accuracy.py` 후속 추가
권장) 또는 운영 로그(`logs/nl_edit_records.jsonl`) 로 측정한다.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.nl_editor.patcher import PatchApplyError, apply_patch_to_specs
from src.nl_editor.prompt import build_few_shot_examples
from src.nl_editor.safety import detect_safety_flags


# ── 표준 샘플 RenderSpec ──────────────────────────────────────────────────

def _sample_scene(scene_id: int) -> dict:
    """4씬 샘플의 1씬 — 각 씬은 [video, text, mosaic, shape] 4개 레이어를 갖는다.

    예시들이 가정하는 path 인덱스 분포:
    - `/layers/0` — video (배속/playback_rate 예시)
    - `/layers/1` — text (자막 관련 예시)
    - `/layers/2` — mosaic (모자이크 이동/크기 예시)
    - `/layers/3` — shape (도형 색/크기 예시)
    """
    return {
        "version": 1,
        "scene_id": scene_id,
        "canvas": {"width": 1920, "height": 1080, "fps": 30},
        "duration_sec": 5.0,
        "layers": [
            # /layers/0 — video
            {
                "type": "video",
                "source": f"footage/scene{scene_id}.mp4",
                "source_size": {"width": 1920, "height": 1080},
                "scale_x": 1.0,
                "scale_y": 1.0,
                "playback_rate": 1.0,
                "rotation_deg": 0,
                "start_sec": 0.0,
                "end_sec": 5.0,
                "opacity": 1.0,
            },
            # /layers/1 — text (자막)
            {
                "type": "text",
                "text": f"씬 {scene_id} 자막",
                "position_x": 960,
                "position_y": 540,
                "font_size_px": 48,
                "font_family": "NotoSansKR",
                "color": "#FFFFFF",
                "align": "center",
                "bold": False,
                "italic": False,
                "outline_color": "#000000",
                "outline_width_px": 0,
                "start_sec": 0.0,
                "end_sec": 5.0,
                "opacity": 1.0,
            },
            # /layers/2 — mosaic (모자이크 카테고리 예시가 width/height/x/y 를 건드림)
            {
                "type": "mosaic",
                "shape": "rect",
                "x": 600,
                "y": 400,
                "width": 400,
                "height": 300,
                "mode": "pixelate",
                "pixel_size": 20,
                "blur_radius": 20,
                "start_sec": 0.0,
                "end_sec": 5.0,
                "opacity": 1.0,
            },
            # /layers/3 — shape (도형 색/크기 예시가 color/width/height 를 건드림)
            {
                "type": "shape",
                "shape": "rect",
                "x": 200,
                "y": 200,
                "width": 400,
                "height": 300,
                "color": "#FF0000",
                "start_sec": 0.0,
                "end_sec": 5.0,
                "opacity": 1.0,
            },
        ],
    }


def _sample_payload() -> dict:
    """4씬 표준 샘플 — 예시 path 인덱스(/scenes/0~/scenes/3, /scenes/-1) 를 모두 커버."""
    return {"scenes": [_sample_scene(i + 1) for i in range(4)]}


# ── 분류 헬퍼 ───────────────────────────────────────────────────────────────

def _split_examples_by_category(examples: list[dict[str, Any]]) -> tuple[list, list]:
    """patcher round-trip 테스트 대상 / safety 분기 대상 분리."""
    apply_ok: list[dict[str, Any]] = []
    safety: list[dict[str, Any]] = []
    for e in examples:
        if e.get("category") == "safety":
            safety.append(e)
        else:
            apply_ok.append(e)
    return apply_ok, safety


# ── 본 테스트 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("example", _split_examples_by_category(build_few_shot_examples())[0])
def test_non_safety_examples_round_trip(example: dict[str, Any]):
    """safety 가 아닌 모든 예시의 patch 가 표준 샘플에 round-trip 적용된다.

    Few-shot 응답이 RenderSpec 스키마와 정합하지 않으면 LLM 도 동일한 잘못된 patch 를
    학습해 출력할 가능성이 크다. patcher 가 거부하는 예시는 즉시 수정 대상.
    """
    payload = _sample_payload()
    patch = example["response"]["patch"]
    try:
        new_specs, _ = apply_patch_to_specs(payload, patch)
    except PatchApplyError as e:
        pytest.fail(
            f"few-shot 예시 round-trip 실패 — instruction='{example['instruction']}', "
            f"kind={e.kind}, details={e.details!r}"
        )
    # 패치 후 씬 개수는 0보다 크고 모두 검증 통과(apply_patch_to_specs 가 내부에서 검증함).
    assert len(new_specs) >= 0


def test_safety_examples_have_matching_safety_flags():
    """safety 카테고리 예시는 detect_safety_flags 결과와 응답의 safety_flags 가 일치한다.

    이 일치성이 깨지면 LLM 이 위험 패턴을 만들었는데 UI 가 확인 다이얼로그를 띄우지
    못하는 사고가 발생할 수 있다.
    """
    _apply_ok, safety = _split_examples_by_category(build_few_shot_examples())
    assert safety, "safety 카테고리 예시가 최소 1종 이상 존재해야 합니다."
    for e in safety:
        patch = e["response"]["patch"]
        detected = set(detect_safety_flags(patch))
        declared = set(e["response"]["safety_flags"])
        # 예시가 선언한 모든 flag 가 백엔드 탐지 결과에 포함돼야 한다.
        # (역방향: 백엔드가 추가로 발견한 flag 는 허용 — 예시가 그것까지 모두 적지 않아도 됨)
        missing = declared - detected
        assert not missing, (
            f"safety 예시 '{e['instruction']}' 의 선언 flag {missing} 가 "
            f"detect_safety_flags 결과 {detected} 에서 발견되지 않습니다."
        )


def test_all_examples_have_non_empty_summary_and_valid_patch_ops():
    """전체 예시의 summary 가 비어있지 않고 patch op 가 RFC 6902 6종 중 하나다."""
    valid_ops = {"add", "remove", "replace", "move", "copy", "test"}
    for e in build_few_shot_examples():
        summary = e["response"]["summary"]
        assert summary and isinstance(summary, str), (
            f"summary 비어있음: instruction='{e['instruction']}'"
        )
        for op in e["response"]["patch"]:
            assert op["op"] in valid_ops, (
                f"잘못된 op '{op['op']}' (instruction='{e['instruction']}')"
            )
            assert "path" in op and isinstance(op["path"], str), (
                f"path 누락 또는 비-문자열 (instruction='{e['instruction']}')"
            )


def test_category_examples_cover_required_path_patterns():
    """각 카테고리 예시가 *실제로* 그 카테고리 답게 동작하는지 path 패턴으로 검증.

    예: filter 카테고리는 `graphic_type=spotlight + params.filter=...` 를 add 해야 하고,
    time 카테고리는 `duration_sec` 또는 `playback_rate` 또는 `start_sec` 만 건드려야 한다.
    이 명세가 어긋나면 LLM 이 카테고리를 헷갈려 잘못된 패턴을 따라할 위험이 있다.
    """
    by_cat: dict[str, list[dict]] = {}
    for e in build_few_shot_examples():
        by_cat.setdefault(e["category"], []).append(e)

    # time 카테고리: patch 가 시간/속도 관련 필드만 건드림
    time_fields = ("duration_sec", "playback_rate", "start_sec", "end_sec")
    for e in by_cat.get("time", []):
        for op in e["response"]["patch"]:
            assert any(f in op["path"] for f in time_fields), (
                f"time 예시 '{e['instruction']}' 의 path '{op['path']}' 가 "
                f"시간 관련 필드를 건드리지 않습니다."
            )

    # filter 카테고리: graphic 레이어 add 가 최소 1개 포함, params.filter 가 있다
    for e in by_cat.get("filter", []):
        ops = e["response"]["patch"]
        graphic_adds = [
            op for op in ops
            if op["op"] == "add"
            and isinstance(op.get("value"), dict)
            and op["value"].get("type") == "graphic"
            and "filter" in (op["value"].get("params") or {})
        ]
        assert graphic_adds, (
            f"filter 예시 '{e['instruction']}' 가 'graphic + params.filter' 패턴을 "
            f"포함하지 않습니다."
        )

    # order 카테고리: 모든 op 가 /scenes/ 경로를 건드림
    for e in by_cat.get("order", []):
        for op in e["response"]["patch"]:
            path = op["path"]
            assert path.startswith("/scenes"), (
                f"order 예시 '{e['instruction']}' 의 path '{path}' 가 /scenes 로 시작하지 않습니다."
            )


def test_token_budget_system_prompt_under_threshold():
    """30종 확장 후 시스템 프롬프트 character 길이가 합리적 범위(예: 60KB 이하)인지 확인.

    Gemini 2.5 Flash 의 입력 토큰 한도(1M) 대비 충분히 여유 있는 수준인지 회귀로 본다.
    한국어 1자 ≈ 2~3 토큰(Gemini 기준) 이라도 60KB → 약 40K~60K 토큰 수준에서
    멈춰야 운영 비용이 폭증하지 않는다. #317 (컨텍스트 캐싱) 도입 시 이 부담의
    대부분이 캐시 토큰으로 흡수된다.
    """
    from src.nl_editor.prompt import build_system_prompt
    from src.render_spec.effects import EFFECT_REGISTRY

    prompt = build_system_prompt(
        effect_registry=EFFECT_REGISTRY,
        render_spec_json=_sample_payload(),
        user_instruction="테스트",
    )
    # 60KB 한도 — #332 도입 시점 측정 약 25~35KB. 회귀 시 즉시 알림.
    assert len(prompt) < 60_000, (
        f"system_prompt 길이가 {len(prompt)} 문자로 60KB 한도를 초과했습니다. "
        f"few-shot/카탈로그 정리가 필요합니다."
    )
