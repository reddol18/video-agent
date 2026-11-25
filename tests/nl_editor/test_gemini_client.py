"""nl_editor.gemini_client — LLM 호출 루프 회귀 테스트 (#315, #317 캐시 시그니처).

실제 Gemini API 는 호출하지 않는다(mock 주입). 검증 흐름:
1. mock 이 유효한 응답을 1회에 반환하면 그대로 사용한다.
2. 첫 응답이 JSON 형식 위반이면 재시도하여 두 번째 응답을 사용한다.
3. 재시도 한도(2회)를 모두 실패하면 PatchApplyError 가 raise 된다.
4. LLM 이 safety_flags 를 비워 보내도 백엔드 safety 가 보강한다.

#317: generate_fn 시그니처가 `(prompt, *, system=None, cached_content=None) -> (text, usage_dict)`
으로 확장되었다. 기존 행동은 모두 유지된다 — 캐시 옵트인 환경변수가 꺼져 있으면 캐시는
사용되지 않고 호출 흐름은 #315 와 동일하다.
"""

from __future__ import annotations

import json

import pytest

from src.nl_editor.gemini_client import generate_nl_edit
from src.nl_editor.patcher import PatchApplyError
from src.render_spec.effects import EFFECT_REGISTRY


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


def _valid_response_json(text: str = "새 자막") -> str:
    return json.dumps({
        "patch": [
            {"op": "replace", "path": "/scenes/0/layers/1/text", "value": text}
        ],
        "summary": f"1번 씬 자막을 '{text}' 로 변경합니다.",
        "safety_flags": [],
    })


def _empty_usage() -> dict:
    return {
        "cached_input_tokens": 0,
        "regular_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def test_single_call_success():
    """첫 호출에서 유효한 응답을 받으면 그대로 사용한다."""
    calls = []

    def mock_gen(prompt, *, system=None, cached_content=None):
        calls.append(prompt)
        return _valid_response_json("바뀐"), _empty_usage()

    resp, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막 바꿔",
        generate_fn=mock_gen,
    )
    assert len(calls) == 1
    assert resp.patch[0].path == "/scenes/0/layers/1/text"
    assert resp.summary
    assert meta["attempts"] == 1
    assert meta["estimated_cost_usd"] > 0
    # 캐시 옵트인이 꺼져있으므로 cache_used False
    assert meta["cache_used"] is False


def test_retry_then_success():
    """첫 응답이 깨진 JSON 이면 재시도 후 두 번째 응답을 사용."""
    seq = iter([
        ("이건 JSON 이 아니에요", _empty_usage()),  # 1차 — 파싱 실패
        (_valid_response_json("결국"), _empty_usage()),  # 2차 — 성공
    ])

    def mock_gen(prompt, *, system=None, cached_content=None):
        return next(seq)

    resp, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막 바꿔",
        generate_fn=mock_gen,
    )
    assert resp.patch[0].value == "결국"
    assert meta["attempts"] == 2


def test_exhausted_retries_raises():
    """모든 시도가 실패하면 PatchApplyError 가 raise."""

    def mock_gen(prompt, *, system=None, cached_content=None):
        return "totally not json", _empty_usage()

    with pytest.raises(PatchApplyError):
        generate_nl_edit(
            effect_registry=EFFECT_REGISTRY,
            render_spec_payload=_payload(),
            user_instruction="자막 바꿔",
            generate_fn=mock_gen,
            max_retry=1,
        )


def test_backend_safety_flags_merged():
    """LLM이 safety_flags 를 비워 보내도 백엔드가 위험 패턴을 잡는다.

    여기서는 canvas/width 변경 patch 를 LLM이 만들어도 safety_flags 가 비었다고 가정.
    백엔드가 'canvas_size_change' 를 보강해야 함.
    """
    raw = json.dumps({
        "patch": [
            {"op": "replace", "path": "/scenes/0/canvas/width", "value": 720}
        ],
        "summary": "캔버스 가로를 720으로 변경",
        "safety_flags": [],
    })

    def mock_gen(prompt, *, system=None, cached_content=None):
        return raw, _empty_usage()

    resp, _ = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="해상도 줄여",
        generate_fn=mock_gen,
    )
    assert "canvas_size_change" in resp.safety_flags


def test_invalid_patch_then_valid_retry():
    """잘못된 path 를 만든 1차 응답 → 재시도 후 정상 응답으로 통과."""
    bad = json.dumps({
        "patch": [{"op": "replace", "path": "/scenes/99/layers/0/text", "value": "x"}],
        "summary": "존재하지 않는 씬",
        "safety_flags": [],
    })
    good = _valid_response_json("정상")
    seq = iter([(bad, _empty_usage()), (good, _empty_usage())])

    def mock_gen(prompt, *, system=None, cached_content=None):
        return next(seq)

    resp, meta = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=mock_gen,
    )
    assert meta["attempts"] == 2
    assert resp.patch[0].value == "정상"


def test_fenced_response_is_parsed():
    """LLM 응답에 ```json fence``` 가 붙어도 파싱된다."""
    body = _valid_response_json("fence 안")
    fenced = "```json\n" + body + "\n```"

    def mock_gen(prompt, *, system=None, cached_content=None):
        return fenced, _empty_usage()

    resp, _ = generate_nl_edit(
        effect_registry=EFFECT_REGISTRY,
        render_spec_payload=_payload(),
        user_instruction="자막",
        generate_fn=mock_gen,
    )
    assert resp.patch[0].value == "fence 안"
