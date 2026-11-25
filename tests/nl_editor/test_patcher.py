"""nl_editor.patcher — JSON Patch 적용 + RenderSpec 검증 회귀 테스트 (#315).

검증 흐름:
1. 유효한 patch 가 적용되고 결과가 Pydantic RenderSpec 검증을 통과한다.
2. 음수 인덱스(`-1`)가 마지막 씬으로 환산된다.
3. 새 레이어 추가(`add` + `/scenes/0/layers/-`)가 성공한다.
4. 스키마 위반 patch 는 `validate` kind 예외를 던진다.
5. 존재하지 않는 path 적용은 `apply` kind 예외.
6. 잘못된 patch 형식은 `format` kind 예외.
"""

from __future__ import annotations

import pytest

from src.nl_editor.models import JsonPatchOp
from src.nl_editor.patcher import (
    PatchApplyError,
    apply_patch_to_specs,
    validate_specs_payload,
)


def _two_scenes_payload() -> dict:
    """2씬 짜리 최소 유효 RenderSpec payload."""
    base_scene = lambda sid: {
        "version": 1,
        "scene_id": sid,
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
                "align": "center",
                "start_sec": 0.0,
                "end_sec": 5.0,
                "opacity": 1.0,
            },
        ],
    }
    return {"scenes": [base_scene(1), base_scene(2)]}


def test_replace_text_succeeds():
    payload = _two_scenes_payload()
    patch = [{"op": "replace", "path": "/scenes/0/layers/1/text", "value": "변경"}]
    new_specs, new_payload = apply_patch_to_specs(payload, patch)
    assert new_specs[0].layers[1].text == "변경"
    # 다른 씬은 그대로
    assert new_specs[1].layers[1].text == "원본"


def test_negative_index_resolves_to_last_scene():
    payload = _two_scenes_payload()
    patch = [{"op": "replace", "path": "/scenes/-1/duration_sec", "value": 9.0}]
    new_specs, _ = apply_patch_to_specs(payload, patch)
    assert new_specs[-1].duration_sec == 9.0


def test_add_new_text_layer_at_end():
    payload = _two_scenes_payload()
    patch = [
        {
            "op": "add",
            "path": "/scenes/0/layers/-",
            "value": {
                "type": "shape",
                "shape": "rect",
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 200,
                "color": "#FF0000",
                "start_sec": 0.0,
                "end_sec": 5.0,
                "opacity": 1.0,
            },
        }
    ]
    new_specs, _ = apply_patch_to_specs(payload, patch)
    assert len(new_specs[0].layers) == 3
    last = new_specs[0].layers[-1]
    assert last.type == "shape"
    assert last.color == "#FF0000"


def test_invalid_value_fails_validation():
    """font_size_px 음수 → RenderSpec 검증 실패."""
    payload = _two_scenes_payload()
    patch = [{"op": "replace", "path": "/scenes/0/layers/1/font_size_px", "value": -10}]
    with pytest.raises(PatchApplyError) as ex:
        apply_patch_to_specs(payload, patch)
    assert ex.value.kind == "validate"


def test_missing_path_fails_apply():
    payload = _two_scenes_payload()
    patch = [{"op": "replace", "path": "/scenes/99/layers/1/text", "value": "x"}]
    with pytest.raises(PatchApplyError) as ex:
        apply_patch_to_specs(payload, patch)
    assert ex.value.kind == "apply"


def test_malformed_op_fails_format():
    payload = _two_scenes_payload()
    # 'op' 가 enum 외 값
    patch = [{"op": "shrink", "path": "/scenes/0", "value": 1}]
    with pytest.raises(PatchApplyError) as ex:
        apply_patch_to_specs(payload, patch)
    assert ex.value.kind == "format"


def test_jsonpatchop_with_alias_from():
    """move 연산이 alias 'from' 으로 직렬화/적용된다."""
    payload = _two_scenes_payload()
    op = JsonPatchOp(op="move", path="/scenes/0", from_="/scenes/1")
    new_specs, _ = apply_patch_to_specs(payload, [op])
    # 원래 scene_id=2 가 앞에 와야 함
    assert new_specs[0].scene_id == 2
    assert new_specs[1].scene_id == 1


def test_validate_specs_payload_accepts_list_form():
    """리스트 형태 입력도 검증된다."""
    payload = _two_scenes_payload()
    out = validate_specs_payload(payload["scenes"])
    assert len(out) == 2


def test_validate_specs_payload_rejects_unknown_type():
    payload = _two_scenes_payload()
    payload["scenes"][0]["layers"][0]["type"] = "ghost"
    with pytest.raises(PatchApplyError) as ex:
        validate_specs_payload(payload)
    assert ex.value.kind == "validate"
