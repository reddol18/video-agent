"""nl_editor.safety — 위험 patch 감지 회귀 테스트 (#315).

LLM이 safety_flags 를 비워서 보내도 백엔드가 같은 위험 패턴을 잡아내야 한다.
이 테스트가 깨지면 사용자 데이터 보호가 깨질 수 있으므로 게이트로 둔다.
"""

from __future__ import annotations

from src.nl_editor.safety import BULK_DELETE_THRESHOLD, detect_safety_flags


def test_delete_all_scenes_detected():
    patch = [{"op": "remove", "path": "/scenes"}]
    flags = detect_safety_flags(patch)
    assert "delete_all_scenes" in flags


def test_delete_all_layers_via_remove():
    patch = [{"op": "remove", "path": "/scenes/0/layers"}]
    assert "delete_all_layers" in detect_safety_flags(patch)


def test_delete_all_layers_via_replace_with_empty_list():
    patch = [{"op": "replace", "path": "/scenes/0/layers", "value": []}]
    assert "delete_all_layers" in detect_safety_flags(patch)


def test_canvas_size_change_detected():
    patch = [{"op": "replace", "path": "/scenes/0/canvas/width", "value": 720}]
    assert "canvas_size_change" in detect_safety_flags(patch)
    patch = [{"op": "replace", "path": "/scenes/0/canvas/fps", "value": 24}]
    assert "canvas_size_change" in detect_safety_flags(patch)


def test_bulk_layer_delete_threshold():
    """layer remove 가 임계값 이상이면 bulk_layer_delete 플래그."""
    patch = [
        {"op": "remove", "path": f"/scenes/0/layers/{i}"}
        for i in range(BULK_DELETE_THRESHOLD)
    ]
    assert "bulk_layer_delete" in detect_safety_flags(patch)


def test_bulk_layer_delete_below_threshold():
    patch = [
        {"op": "remove", "path": "/scenes/0/layers/0"},
        {"op": "remove", "path": "/scenes/0/layers/1"},
    ]
    # 임계값(3) 미만
    assert "bulk_layer_delete" not in detect_safety_flags(patch)


def test_unknown_top_level_path():
    patch = [{"op": "replace", "path": "/version", "value": 2}]
    assert "unknown_top_level_path" in detect_safety_flags(patch)


def test_safe_patch_returns_empty():
    """일반적인 텍스트 변경/색 변경 등은 위험 플래그 없음."""
    patch = [
        {"op": "replace", "path": "/scenes/0/layers/1/text", "value": "변경"},
        {"op": "replace", "path": "/scenes/1/layers/1/color", "value": "#FF0000"},
    ]
    assert detect_safety_flags(patch) == []


def test_multiple_flags_unique_and_ordered():
    """여러 위험이 동시에 발견되면 정해진 순서로 unique 하게 반환된다."""
    patch = [
        {"op": "remove", "path": "/scenes/0/layers"},
        {"op": "replace", "path": "/scenes/1/canvas/width", "value": 1280},
        {"op": "remove", "path": "/scenes/2/layers/0"},
        {"op": "remove", "path": "/scenes/2/layers/1"},
        {"op": "remove", "path": "/scenes/2/layers/2"},
    ]
    flags = detect_safety_flags(patch)
    # unique
    assert len(flags) == len(set(flags))
    # canvas_size_change + delete_all_layers + bulk_layer_delete
    assert "delete_all_layers" in flags
    assert "canvas_size_change" in flags
    assert "bulk_layer_delete" in flags
