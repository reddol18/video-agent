"""RenderSpec → SceneCard 역동기화 단위 테스트 (#335).

검증 항목:
1. 4종 layer (text/shape/graphic/mosaic) 가 올바른 SceneCard 필드로 변환되는지
2. 절대 픽셀 → 0~1 비율 환산 정확성 (canvas.width/height 로 나눔)
3. video/image layer 의 playback_rate → footage_speed 보존
4. 변환 불가 필드(filters/motions/narration_text/footage/match/tts) 가 기존 SceneCard
   에서 그대로 유지되는지 (덮어쓰기 없음)
5. 기존 overlay id 가 위치 매칭으로 보존되는지 (font_size 만 바뀐 케이스)
6. scene_id 가 RenderSpec → SceneCard 로 보존되는지
7. 비연속 scene_id (1, 3, 4) 환경에서 RenderSpec scenes 배열 순서와 SceneCard 매칭
   이 scene_id 기준으로 정확히 동작하는지
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.render_spec.scene_card_sync import (
    merge_render_spec_into_scene_card,
    sync_scene_cards_from_render_spec,
)


# ── 헬퍼 ──────────────────────────────────────────────────────────────────


def _mk_project(scenes_dict_list: list[dict]) -> SimpleNamespace:
    """Project-like 객체. scenes 는 model_dump 호환 dict 를 갖는 객체 리스트."""
    scenes = [SimpleNamespace(**s, model_dump=lambda s=s: s) for s in scenes_dict_list]
    return SimpleNamespace(scenes=scenes)


def _render_spec_scene(
    scene_id: int = 1,
    canvas_w: int = 1280,
    canvas_h: int = 720,
    layers: list[dict] | None = None,
) -> dict:
    return {
        "version": 1,
        "scene_id": scene_id,
        "canvas": {"width": canvas_w, "height": canvas_h, "fps": 30},
        "duration_sec": 5.0,
        "layers": layers or [],
    }


# ── 단일 layer 변환 ──────────────────────────────────────────────────────


def test_text_layer_pixel_to_ratio_conversion():
    """1280×720 캔버스에서 position_x=640 → x=0.5, position_y=72 → y=0.1."""
    spec = _render_spec_scene(
        layers=[
            {
                "type": "text",
                "text": "안녕",
                "position_x": 640,
                "position_y": 72,
                "font_size_px": 48,
                "color": "#FF0000",
                "opacity": 1.0,
                "start_sec": 0.0,
                "end_sec": 5.0,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    assert len(merged["text_overlays"]) == 1
    t = merged["text_overlays"][0]
    assert t["x"] == pytest.approx(0.5)
    assert t["y"] == pytest.approx(0.1)
    assert t["font_size"] == 48
    assert t["color"] == "#FF0000"
    assert t["text"] == "안녕"


def test_shape_layer_pixel_to_ratio_conversion():
    """절대 픽셀 100/200/300/400 → 0~1 비율 (1280×720 기준)."""
    spec = _render_spec_scene(
        layers=[
            {
                "type": "shape",
                "shape": "rect",
                "x": 128,  # 1280 * 0.1
                "y": 72,   # 720 * 0.1
                "width": 256,  # 1280 * 0.2
                "height": 144, # 720 * 0.2
                "color": "#00FF00",
                "opacity": 0.8,
                "start_sec": 1.0,
                "end_sec": 3.0,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    assert len(merged["shape_overlays"]) == 1
    s = merged["shape_overlays"][0]
    assert s["x"] == pytest.approx(0.1)
    assert s["y"] == pytest.approx(0.1)
    assert s["width"] == pytest.approx(0.2)
    assert s["height"] == pytest.approx(0.2)
    assert s["color"] == "#00FF00"
    assert s["shape"] == "rect"


def test_graphic_layer_center_to_ratio():
    """center_x/center_y 비율 환산."""
    spec = _render_spec_scene(
        layers=[
            {
                "type": "graphic",
                "graphic_type": "spotlight",
                "center_x": 960,  # 0.75
                "center_y": 360,  # 0.5
                "params": {"radius": 0.3},
                "start_sec": 0.0,
                "end_sec": 5.0,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    assert len(merged["graphic_overlays"]) == 1
    g = merged["graphic_overlays"][0]
    assert g["x"] == pytest.approx(0.75)
    assert g["y"] == pytest.approx(0.5)
    assert g["type"] == "spotlight"
    assert g["params"] == {"radius": 0.3}


def test_mosaic_layer_conversion():
    spec = _render_spec_scene(
        layers=[
            {
                "type": "mosaic",
                "shape": "rect",
                "x": 640,
                "y": 360,
                "width": 320,
                "height": 180,
                "mode": "blur",
                "pixel_size": 20,
                "blur_radius": 30,
                "start_sec": 0.5,
                "end_sec": 4.5,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    assert len(merged["mosaic_overlays"]) == 1
    m = merged["mosaic_overlays"][0]
    assert m["mode"] == "blur"
    assert m["x"] == pytest.approx(0.5)
    assert m["blur_radius"] == 30


def test_video_layer_playback_rate_to_footage_speed():
    spec = _render_spec_scene(
        layers=[
            {
                "type": "video",
                "source": "clip.mp4",
                "source_size": {"width": 1920, "height": 1080},
                "scale_x": 0.667,
                "scale_y": 0.667,
                "playback_rate": 1.5,
                "rotation_deg": 0,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    assert merged["footage_speed"] == pytest.approx(1.5)


def test_image_layer_playback_rate_to_footage_speed():
    spec = _render_spec_scene(
        layers=[
            {
                "type": "image",
                "source": "img.png",
                "source_size": {"width": 1920, "height": 1080},
                "scale_x": 0.667,
                "scale_y": 0.667,
                "zoom_from": 1.0,
                "zoom_to": 1.15,
                "playback_rate": 0.5,  # image 도 동일 필드 보유 — VideoLayer 와 호환되도록
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    # image layer 의 playback_rate 가 1.0 외 값이면 footage_speed 갱신
    # (실제 ImageLayer 모델에 playback_rate 가 있지 않을 수 있음 — 우리 코드는 .get() 사용)
    assert merged.get("footage_speed") == pytest.approx(0.5)


def test_image_layer_without_playback_rate_preserves_footage_speed():
    """ImageLayer 가 playback_rate 필드를 갖지 않을 때 (builder 출력의 정상 케이스)
    기존 SceneCard.footage_speed 가 그대로 유지되어야 한다 (#339).

    회귀 시나리오:
    - kenburns 씬에 사용자가 footage_speed=2.0 설정
    - NL 편집 → RenderSpec 빌드: ImageLayer 는 playback_rate 필드 없이 직렬화됨
    - scene_card_sync 가 .get("playback_rate") = None 을 _safe_float(_, 1.0) 으로
      변환 → footage_speed 가 1.0 으로 *덮어씌워지는* 회귀 발생
    - 가드 추가 후 None 일 때 분기 스킵하여 기존 2.0 유지되어야 함
    """
    existing = {
        "scene_id": 1,
        "footage_speed": 2.0,
        "text_overlays": [],
        "shape_overlays": [],
        "graphic_overlays": [],
        "mosaic_overlays": [],
        "filters": [],
        "motions": [],
    }
    # builder.py 의 ImageLayer 직렬화 결과를 그대로 모방 — playback_rate 키 없음
    spec = _render_spec_scene(
        layers=[
            {
                "type": "image",
                "source": "img.png",
                "source_size": {"width": 1920, "height": 1080},
                "scale_x": 0.667,
                "scale_y": 0.667,
                "zoom_from": 1.0,
                "zoom_to": 1.15,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    # 핵심 검증: 기존 footage_speed 2.0 이 1.0 으로 덮어씌워지지 않음
    assert merged["footage_speed"] == pytest.approx(2.0), (
        "ImageLayer 에 playback_rate 가 없으면 기존 SceneCard.footage_speed 가 유지되어야 함"
    )


def test_video_layer_without_playback_rate_preserves_footage_speed():
    """VideoLayer 가 playback_rate 키 없이 들어와도 (방어 케이스) 기존 값 유지 (#339)."""
    existing = {
        "scene_id": 1,
        "footage_speed": 1.5,
        "text_overlays": [],
        "shape_overlays": [],
        "graphic_overlays": [],
        "mosaic_overlays": [],
        "filters": [],
        "motions": [],
    }
    spec = _render_spec_scene(
        layers=[
            {
                "type": "video",
                "source": "clip.mp4",
                "source_size": {"width": 1920, "height": 1080},
                "trim_start_sec": 0.0,
                "trim_end_sec": 5.0,
                "scale_x": 0.667,
                "scale_y": 0.667,
                # playback_rate 누락
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    assert merged["footage_speed"] == pytest.approx(1.5), (
        "playback_rate 키가 없는 video layer 에서도 기존 footage_speed 유지되어야 함"
    )


# ── 변환 불가 필드 보존 ───────────────────────────────────────────────────


def test_preserves_non_translatable_fields():
    """filters/motions/narration_text/footage/match/tts 등은 기존 SceneCard 값을 유지."""
    existing = {
        "scene_id": 1,
        "title": "테스트 씬",
        "narration_text": "이것은 나레이션입니다",
        "subtitle_text": "자막",
        "footage": {"type": "clip", "file": "test.mp4"},
        "match": {
            "chunk_id": "c1",
            "source_file": "test.mp4",
            "trim_start": 0.0,
            "trim_end": 5.0,
            "match_score": 0.9,
            "match_reason": "테스트",
        },
        "tts": {"audio_url": "blob:xxx", "duration_sec": 3.0, "status": "ready", "synced": True},
        "filters": [{"filter_type": "horror", "intensity": 0.7, "params": {}}],
        "motions": [{"motion_type": "shake", "start_sec": 0.0, "duration_sec": 1.0, "params": {}}],
        "text_overlays": [],
        "shape_overlays": [],
    }
    spec = _render_spec_scene(
        layers=[
            {
                "type": "shape",
                "shape": "rect",
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 200,
                "color": "#FF0000",
                "opacity": 1.0,
                "start_sec": 0.0,
                "end_sec": 5.0,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)

    # 변환 가능 필드 — 새 값으로 대체
    assert len(merged["shape_overlays"]) == 1
    assert merged["shape_overlays"][0]["color"] == "#FF0000"

    # 변환 불가 필드 — 기존 값 보존
    assert merged["narration_text"] == "이것은 나레이션입니다"
    assert merged["subtitle_text"] == "자막"
    assert merged["footage"] == {"type": "clip", "file": "test.mp4"}
    assert merged["match"]["chunk_id"] == "c1"
    assert merged["tts"]["status"] == "ready"
    assert merged["filters"][0]["filter_type"] == "horror"
    assert merged["motions"][0]["motion_type"] == "shake"
    assert merged["title"] == "테스트 씬"


# ── id 보존 ───────────────────────────────────────────────────────────────


def test_preserves_existing_text_overlay_id_on_property_replace():
    """기존 text_overlay 와 *같은 위치/시작시각* 의 layer 가 들어오면 id 보존."""
    existing = {
        "scene_id": 1,
        "text_overlays": [
            {
                "id": "text-existing-123",
                "text": "원본",
                "x": 0.5,
                "y": 0.1,
                "font_size": 40,
                "start_sec": 0.0,
                "color": "#FFFFFF",
            }
        ],
    }
    spec = _render_spec_scene(
        layers=[
            {
                "type": "text",
                "text": "수정됨",
                "position_x": 640,  # 0.5 * 1280 = 640
                "position_y": 72,   # 0.1 * 720 = 72
                "font_size_px": 64,  # font_size 만 변경
                "color": "#FFFFFF",
                "opacity": 1.0,
                "start_sec": 0.0,
                "end_sec": 5.0,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    assert len(merged["text_overlays"]) == 1
    assert merged["text_overlays"][0]["id"] == "text-existing-123"
    assert merged["text_overlays"][0]["font_size"] == 64
    assert merged["text_overlays"][0]["text"] == "수정됨"


def test_new_layer_gets_new_id():
    """기존 overlay 가 없는 위치에 추가되면 새 id 발급."""
    existing = {"scene_id": 1, "text_overlays": []}
    spec = _render_spec_scene(
        layers=[
            {
                "type": "shape",
                "shape": "rect",
                "x": 100,
                "y": 100,
                "width": 200,
                "height": 200,
                "color": "#FF0000",
                "opacity": 1.0,
                "start_sec": 0.0,
                "end_sec": 5.0,
            }
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    assert len(merged["shape_overlays"]) == 1
    new_id = merged["shape_overlays"][0]["id"]
    assert new_id.startswith("shape-")
    assert len(new_id) > len("shape-")  # uuid 부분 존재


# ── 다씬 sync ────────────────────────────────────────────────────────────


def test_sync_handles_noncontiguous_scene_ids():
    """scene_id 1, 3, 4 (씬 2 삭제) 환경에서 정확한 매칭."""
    project = _mk_project([
        {"scene_id": 1, "narration_text": "씬1", "text_overlays": [], "shape_overlays": [], "graphic_overlays": [], "mosaic_overlays": [], "filters": [], "motions": []},
        {"scene_id": 3, "narration_text": "씬3", "text_overlays": [], "shape_overlays": [], "graphic_overlays": [], "mosaic_overlays": [], "filters": [], "motions": []},
        {"scene_id": 4, "narration_text": "씬4", "text_overlays": [], "shape_overlays": [], "graphic_overlays": [], "mosaic_overlays": [], "filters": [], "motions": []},
    ])
    payload = {
        "scenes": [
            _render_spec_scene(scene_id=1, layers=[{"type": "shape", "shape": "rect", "x": 100, "y": 100, "width": 100, "height": 100, "color": "#FF0000", "opacity": 1.0, "start_sec": 0.0, "end_sec": 5.0}]),
            _render_spec_scene(scene_id=3, layers=[]),
            _render_spec_scene(scene_id=4, layers=[]),
        ]
    }
    result = sync_scene_cards_from_render_spec(payload, project)
    assert len(result) == 3
    # scene_id=1 에 빨간 사각형
    assert result[0]["scene_id"] == 1
    assert len(result[0]["shape_overlays"]) == 1
    assert result[0]["narration_text"] == "씬1"
    # scene_id=3, 4 는 비어 있음
    assert result[1]["scene_id"] == 3
    assert len(result[1]["shape_overlays"]) == 0
    assert result[1]["narration_text"] == "씬3"
    assert result[2]["scene_id"] == 4
    assert result[2]["narration_text"] == "씬4"


def test_empty_render_spec_returns_empty_list():
    project = _mk_project([{"scene_id": 1, "text_overlays": [], "shape_overlays": [], "graphic_overlays": [], "mosaic_overlays": []}])
    result = sync_scene_cards_from_render_spec({"scenes": []}, project)
    assert result == []


def test_multiple_layers_per_scene():
    """한 씬에 여러 종류의 layer 가 있어도 모두 변환."""
    spec = _render_spec_scene(
        layers=[
            {"type": "text", "text": "T1", "position_x": 640, "position_y": 72, "font_size_px": 40, "color": "#FFFFFF", "opacity": 1.0, "start_sec": 0.0, "end_sec": 5.0},
            {"type": "text", "text": "T2", "position_x": 320, "position_y": 360, "font_size_px": 30, "color": "#00FF00", "opacity": 1.0, "start_sec": 1.0, "end_sec": 3.0},
            {"type": "shape", "shape": "rect", "x": 100, "y": 100, "width": 200, "height": 200, "color": "#FF0000", "opacity": 1.0, "start_sec": 0.0, "end_sec": 5.0},
            {"type": "graphic", "graphic_type": "alert", "center_x": 640, "center_y": 360, "params": {}, "start_sec": 0.0, "end_sec": 5.0},
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    assert len(merged["text_overlays"]) == 2
    assert len(merged["shape_overlays"]) == 1
    assert len(merged["graphic_overlays"]) == 1
    assert merged["text_overlays"][0]["text"] == "T1"
    assert merged["text_overlays"][1]["text"] == "T2"


# ── 엣지 케이스 ───────────────────────────────────────────────────────────


def test_handles_missing_canvas_with_default():
    """canvas 가 빠진 RenderSpec — 기본 1280×720 폴백."""
    spec = {
        "scene_id": 1,
        "layers": [
            {
                "type": "text",
                "text": "안녕",
                "position_x": 640,
                "position_y": 72,
                "font_size_px": 40,
                "color": "#FFFFFF",
                "opacity": 1.0,
                "start_sec": 0.0,
                "end_sec": 5.0,
            }
        ],
    }
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    # 1280 / 640 = 0.5 (기본 폴백 적용 확인)
    assert merged["text_overlays"][0]["x"] == pytest.approx(0.5)


def test_handles_zero_canvas_size():
    """canvas.width=0 같은 비정상 값에도 안전하게 동작."""
    spec = _render_spec_scene(canvas_w=0, canvas_h=0)
    spec["layers"] = [{"type": "shape", "shape": "rect", "x": 100, "y": 100, "width": 50, "height": 50, "color": "#FF0000", "opacity": 1.0, "start_sec": 0.0, "end_sec": 5.0}]
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    # 0 으로 나누지 않고 기본값으로 폴백 — 비정상 입력에도 죽지 않음
    assert len(merged["shape_overlays"]) == 1


def test_handles_unknown_layer_type():
    """모르는 type 의 layer 는 무시 (forward-compat)."""
    spec = _render_spec_scene(
        layers=[
            {"type": "future_layer_type", "x": 100, "y": 100},
            {"type": "shape", "shape": "rect", "x": 100, "y": 100, "width": 200, "height": 200, "color": "#FF0000", "opacity": 1.0, "start_sec": 0.0, "end_sec": 5.0},
        ]
    )
    merged = merge_render_spec_into_scene_card(spec, existing_scene=None)
    # shape 만 변환
    assert len(merged["shape_overlays"]) == 1
    assert len(merged["text_overlays"]) == 0


def test_round_trip_preserves_scene_id():
    """RenderSpec scene_id 가 SceneCard scene_id 로 그대로 보존."""
    spec = _render_spec_scene(scene_id=42)
    merged = merge_render_spec_into_scene_card(spec, existing_scene={"scene_id": 0})
    assert merged["scene_id"] == 42


def test_invalid_scene_card_objects_skipped():
    """sync 함수가 잘못된 형태의 RenderSpec scene 을 만나도 죽지 않음."""
    project = _mk_project([{"scene_id": 1, "text_overlays": [], "shape_overlays": [], "graphic_overlays": [], "mosaic_overlays": []}])
    payload = {"scenes": [None, "not_a_dict", _render_spec_scene(scene_id=1)]}
    result = sync_scene_cards_from_render_spec(payload, project)
    # None / 문자열은 건너뛰고 유효한 scene 1개만 결과
    assert len(result) == 1
    assert result[0]["scene_id"] == 1


# ── MotionLayer → SceneCard.motions 환원 (#349) ─────────────────────────────


def _motion_layer(motion_type: str, start: float = 0.0, dur: float = 1.5, params: dict | None = None) -> dict:
    """RenderSpec MotionLayer 직렬화 형태 (builder._build_motion_layer 결과와 동일 키)."""
    return {
        "type": "motion",
        "motion_type": motion_type,
        "motion_start_sec": start,
        "motion_duration_sec": dur,
        "params": params or {},
        "start_sec": 0.0,
        "end_sec": 5.0,
        "opacity": 1.0,
    }


def test_nl_stutter_add_reduced_into_motions():
    """#349 — NL 편집으로 add 된 stutter MotionLayer 가 SceneCard.motions 로 환원."""
    spec = _render_spec_scene(
        layers=[
            {"type": "video", "start_sec": 0.0, "end_sec": 5.0},
            _motion_layer("stutter", start=0.5, dur=2.0, params={"intensity": 0.4}),
        ]
    )
    existing = {"scene_id": 1, "motions": [], "text_overlays": [], "shape_overlays": []}
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    assert len(merged["motions"]) == 1
    m = merged["motions"][0]
    assert m["motion_type"] == "stutter"
    assert m["start_sec"] == pytest.approx(0.5)
    assert m["duration_sec"] == pytest.approx(2.0)
    assert m["params"]["intensity"] == pytest.approx(0.4)


def test_nl_stutter_add_survives_full_sync():
    """#349 E2E — sync_scene_cards_from_render_spec 후에도 stutter 가 motions 에 존재."""
    spec = _render_spec_scene(
        layers=[
            {"type": "video", "start_sec": 0.0, "end_sec": 5.0},
            _motion_layer("stutter", start=0.0, dur=1.5, params={"intensity": 0.3}),
        ]
    )
    project = _mk_project([{"scene_id": 1, "motions": [], "text_overlays": [], "shape_overlays": [], "graphic_overlays": [], "mosaic_overlays": []}])
    result = sync_scene_cards_from_render_spec({"scenes": [spec]}, project)
    assert len(result) == 1
    assert any(mm["motion_type"] == "stutter" for mm in result[0]["motions"]), "stutter lost in sync-back"


def test_motion_layer_replaces_existing_motions():
    """RenderSpec 에 motion 레이어가 있으면 기존 motions 를 *대체* (단일 진실 채택)."""
    spec = _render_spec_scene(
        layers=[_motion_layer("clip_zoom", start=0.0, dur=2.0, params={"start_scale": 1.0, "end_scale": 1.3})]
    )
    existing = {"scene_id": 1, "motions": [{"motion_type": "shake", "start_sec": 0.0, "duration_sec": 1.0, "params": {}, "source_id": None}]}
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    # shake 는 사라지고 clip_zoom 만 남는다 (RenderSpec layers 가 단일 진실)
    assert len(merged["motions"]) == 1
    assert merged["motions"][0]["motion_type"] == "clip_zoom"


def test_motion_layer_absent_preserves_existing_motions():
    """RenderSpec 에 motion 레이어가 하나도 없으면 기존 motions 보존."""
    spec = _render_spec_scene(layers=[{"type": "video", "start_sec": 0.0, "end_sec": 5.0}])
    existing = {"scene_id": 1, "motions": [{"motion_type": "shake", "start_sec": 0.0, "duration_sec": 1.0, "params": {}, "source_id": None}]}
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    assert len(merged["motions"]) == 1
    assert merged["motions"][0]["motion_type"] == "shake"


def test_motion_source_id_recovered_on_round_trip():
    """tag 시스템 source_id 는 MotionLayer 가 직렬화하지 않으므로 위치 매칭으로 복구."""
    spec = _render_spec_scene(
        layers=[_motion_layer("stutter", start=0.5, dur=1.5, params={"intensity": 0.3})]
    )
    existing = {
        "scene_id": 1,
        "motions": [{"motion_type": "stutter", "start_sec": 0.5, "duration_sec": 1.5, "params": {"intensity": 0.3}, "source_id": "tag-glitch-motion"}],
    }
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    assert merged["motions"][0]["source_id"] == "tag-glitch-motion"


def test_unsupported_motion_type_skipped_in_reduction():
    """미지원 motion_type 은 환원 시 스킵 (forward-compat)."""
    spec = _render_spec_scene(
        layers=[
            _motion_layer("future_motion", start=0.0, dur=1.0),
            _motion_layer("stutter", start=0.0, dur=1.5, params={"intensity": 0.3}),
        ]
    )
    existing = {"scene_id": 1, "motions": []}
    merged = merge_render_spec_into_scene_card(spec, existing_scene=existing)
    # 미지원 타입은 빠지고 stutter 만 남는다
    assert len(merged["motions"]) == 1
    assert merged["motions"][0]["motion_type"] == "stutter"
