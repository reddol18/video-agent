"""nl_editor.prompt — 시스템 프롬프트 구성 회귀 테스트 (#315 / #332 확장).

LLM 호출은 하지 않고, *프롬프트 문자열 자체*가 다음을 포함하는지 검증한다.
1. RenderSpec 스키마 요약 블록
2. 효과 카탈로그 (적어도 하나의 카탈로그 키)
3. 현재 RenderSpec JSON (scene_id, canvas 등 핵심 필드)
4. 응답 형식(JSON Patch) 명세
5. Few-shot 예시 32종 — 카테고리(text/filter/shape/time/order) 각 5종 이상 (#347 stutter 2종 포함)

이 테스트가 깨지면 LLM 응답 품질이 즉시 떨어지므로 회귀 게이트로 둔다.
"""

from __future__ import annotations

import json

from src.nl_editor.prompt import (
    build_few_shot_examples,
    build_system_prompt,
    catalog_to_compact_json,
)
from src.render_spec.effects import EFFECT_REGISTRY


def _sample_render_spec_payload() -> dict:
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
                        "source": "footage/clip1.mp4",
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
                        "text": "안녕",
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


def test_catalog_to_compact_json_includes_all_registry_keys():
    """효과 카탈로그 JSON 에 EFFECT_REGISTRY 의 모든 키가 그대로 노출된다."""
    raw = catalog_to_compact_json(EFFECT_REGISTRY)
    data = json.loads(raw)
    keys = {e["catalog_key"] for e in data["effects"]}
    assert keys == set(EFFECT_REGISTRY.keys()), "catalog JSON 키가 EFFECT_REGISTRY 와 불일치"


def test_catalog_drops_ui_only_fields():
    """LLM에 불필요한 필드(preview_supported 등)는 제거된다."""
    raw = catalog_to_compact_json(EFFECT_REGISTRY)
    data = json.loads(raw)
    for e in data["effects"]:
        for drop in ("preview_supported", "backend_layer_type", "notes"):
            assert drop not in e, f"{drop} 가 카탈로그 JSON 에서 제거되지 않음"


def test_build_few_shot_examples_has_32_examples():
    """few-shot 예시 32종 — #315 본문 완료 조건(30종) + #347 stutter motion 2종."""
    examples = build_few_shot_examples()
    assert len(examples) == 32, f"few-shot 예시는 32종이어야 합니다 (현재 {len(examples)}종)"


def test_build_few_shot_examples_category_distribution():
    """#315 명시 카테고리(텍스트/색/위치/속도/순서) 각 5종 이상 포함."""
    examples = build_few_shot_examples()
    counts: dict[str, int] = {}
    for e in examples:
        counts[e["category"]] = counts.get(e["category"], 0) + 1

    # #315 본문이 명시한 5개 카테고리는 각 5종 이상이어야 함.
    for required in ("text", "filter", "shape", "time", "order"):
        assert counts.get(required, 0) >= 5, (
            f"카테고리 '{required}' 는 5종 이상이어야 합니다 (현재 {counts.get(required, 0)}종)"
        )

    # 그 외 안전 패턴/효과 추가도 적어도 1종 이상.
    for optional in ("safety", "effect_add"):
        assert counts.get(optional, 0) >= 1, (
            f"카테고리 '{optional}' 는 1종 이상이어야 합니다 (현재 {counts.get(optional, 0)}종)"
        )


def test_few_shot_safety_examples_have_flags():
    """safety 카테고리 예시는 반드시 safety_flags 가 비어있지 않다."""
    for e in build_few_shot_examples():
        if e["category"] == "safety":
            flags = e["response"]["safety_flags"]
            assert flags, (
                f"safety 예시 '{e['instruction']}' 는 safety_flags 가 비어있어선 안 됩니다."
            )


def test_few_shot_examples_have_valid_response_shape():
    """각 예시의 response 가 patch/summary/safety_flags 필드를 갖춘다."""
    for e in build_few_shot_examples():
        r = e["response"]
        assert "patch" in r and isinstance(r["patch"], list)
        assert "summary" in r and isinstance(r["summary"], str) and r["summary"]
        assert "safety_flags" in r and isinstance(r["safety_flags"], list)
        for op in r["patch"]:
            assert "op" in op and "path" in op


def test_build_system_prompt_includes_required_sections():
    """build_system_prompt 가 5가지 핵심 섹션을 모두 포함한다."""
    prompt = build_system_prompt(
        effect_registry=EFFECT_REGISTRY,
        render_spec_json=_sample_render_spec_payload(),
        user_instruction="자막을 노란색으로",
    )
    # 1) 역할 명시
    assert "RenderSpec" in prompt
    # 2) 스키마 요약 블록
    assert "TextLayer" in prompt and "ShapeLayer" in prompt
    # 3) 효과 카탈로그
    assert "filter:vivid" in prompt or "text:overlay" in prompt
    # 4) 현재 RenderSpec — 샘플의 scene_id, text 값이 들어가야 함
    assert '"scene_id": 1' in prompt
    assert "안녕" in prompt
    # 5) 응답 형식
    assert '"patch"' in prompt and '"summary"' in prompt and '"safety_flags"' in prompt
    # 6) 사용자 명령
    assert "자막을 노란색으로" in prompt


def test_build_system_prompt_history_inclusion():
    """history 가 비어있지 않으면 프롬프트에 컨텍스트가 들어간다."""
    history = [
        {"instruction": "1번 씬 더 크게", "summary": "폰트 50→72", "applied": True}
    ]
    prompt = build_system_prompt(
        effect_registry=EFFECT_REGISTRY,
        render_spec_json=_sample_render_spec_payload(),
        history=history,
        user_instruction="추가로 빨갛게",
    )
    assert "1번 씬 더 크게" in prompt
    assert "최근 대화 컨텍스트" in prompt
