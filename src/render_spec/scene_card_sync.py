"""RenderSpec → SceneCard 역동기화 (#335).

배경
----
자연어 편집(`/nl-edit/apply`)은 RenderSpec 캐시(`render_spec.json`)만 갱신한다.
그러나 미리보기 렌더(`/preview`) 와 최종 합성(`/render`) 은 모두 SceneCard 를
입력으로 사용하므로, NL 편집 변경 사항이 후속 액션(footage 드롭, narration 수정 등)
이후에 SceneCard 기반으로 다시 RenderSpec 이 빌드되면 *덮어써져 사라진다*.

본 모듈은 NL 편집 적용 직후 *변환 가능한 RenderSpec layers 만* SceneCard 필드로
역매핑하는 함수를 제공한다. 프론트는 이 결과로 IndexedDB Project.scenes 와
zustand store 를 갱신한다.

변환 범위 (정확히 매핑 가능한 항목만)
- VideoLayer/ImageLayer.playback_rate → SceneCard.footage_speed
- TextLayer → SceneCard.text_overlays
- ShapeLayer → SceneCard.shape_overlays
- GraphicLayer → SceneCard.graphic_overlays
- MosaicLayer → SceneCard.mosaic_overlays
- MotionLayer → SceneCard.motions (#349) — builder 가 SceneCard.motions 를 MotionLayer 로
  직렬화하므로 라운드트립 복원이 성립한다. NL 편집으로 추가/변경된 stutter 등 모션이
  SceneCard.motions 로 환원되어 실제 렌더링까지 도달한다.

변환 범위 외 (RenderSpec 에 없음 — 기존 SceneCard 값 보존)
- SceneCard.filters: RenderSpec builder 가 layers 화하지 않으므로 RenderSpec 만으로는
  복원 불가. NL 편집이 filter 를 건드리는 경로(아직 없음)는 본 모듈로 표현할 수 없다 —
  후속 이슈로 별도 처리.
- SceneCard.footage / .match / .tts / .narration_text / .subtitle_text 등: NL 편집이
  건드리지 않음 (RenderSpec 차원 외 메타데이터).

설계 원칙
--------
- **정보 손실 최소화**: 변환 가능 항목만 *추가/대체* 한다. 변환 불가 항목(filter/motion 등)
  은 기존 SceneCard 값을 그대로 유지한다.
- **id 발급**: 기존 SceneCard 의 overlay id 가 가능하면 그대로 유지되도록 매칭한다
  (동일 type + 동일 시간대 + 동일 위치 근사). 매칭 실패 시 새 id 발급.
- **좌표 환산**: RenderSpec 은 절대 픽셀 (canvas 기준), SceneCard 는 0~1 비율.
  canvas.width / canvas.height 로 나눠 환산.

진입점
------
- `sync_scene_cards_from_render_spec(scenes_payload, project)` → `list[SceneCard dict]`
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


# ── 기본 헬퍼 ──────────────────────────────────────────────────────────────

def _new_id(prefix: str) -> str:
    """기존 id 와 매칭되지 않을 때 부여하는 새 id."""
    return f"{prefix}-{uuid4().hex[:8]}"


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _px_to_ratio(value: Any, span: int, default: float = 0.0) -> float:
    """절대 픽셀 → 0~1 비율. span 이 0/음수면 default 폴백."""
    if span <= 0:
        return default
    try:
        return float(value) / float(span)
    except (TypeError, ValueError):
        return default


def _find_id_by_pos(
    existing_overlays: list[dict],
    type_key: str,
    target_x: float,
    target_y: float,
    target_start: float,
    *,
    tol: float = 0.02,
) -> str | None:
    """기존 overlay 들 중 위치(비율)+시작시간이 가까운 것의 id 를 찾는다.

    NL 편집이 기존 overlay 의 속성만 바꾼 경우(예: font_size 만 replace) id 가
    보존되어야 사용자가 인지하는 \"같은 객체\" 라는 느낌을 유지한다.
    """
    best_id: str | None = None
    best_score = float("inf")
    for ov in existing_overlays:
        if not isinstance(ov, dict):
            continue
        if type_key and ov.get("type") and ov.get("type") != type_key:
            # graphic 의 경우 type 필드 검사. text/shape/mosaic 은 type 필드 없음.
            continue
        dx = abs(_safe_float(ov.get("x"), -99) - target_x)
        dy = abs(_safe_float(ov.get("y"), -99) - target_y)
        dt = abs(_safe_float(ov.get("start_sec"), -99) - target_start)
        if dx > tol or dy > tol:
            continue
        score = dx + dy + dt * 0.1
        if score < best_score:
            best_score = score
            best_id = str(ov.get("id") or "")
    return best_id or None


# ── 레이어 → 오버레이 ────────────────────────────────────────────────────


def _text_layer_to_overlay(
    layer: dict,
    canvas_w: int,
    canvas_h: int,
    existing_texts: list[dict],
) -> dict:
    """RenderSpec TextLayer → SceneCard TextOverlay."""
    x_ratio = _px_to_ratio(layer.get("position_x"), canvas_w, 0.5)
    y_ratio = _px_to_ratio(layer.get("position_y"), canvas_h, 0.1)
    start = _safe_float(layer.get("start_sec"), 0.0)
    matched_id = _find_id_by_pos(existing_texts, "", x_ratio, y_ratio, start)
    return {
        "id": matched_id or _new_id("text"),
        "text": str(layer.get("text") or ""),
        "x": x_ratio,
        "y": y_ratio,
        "font_size": _safe_int(layer.get("font_size_px"), 40),
        "font_family": str(layer.get("font_family") or "NotoSansKR"),
        "color": str(layer.get("color") or "#FFFFFF"),
        "gradient": None,
        "bold": bool(layer.get("bold", False)),
        "italic": bool(layer.get("italic", False)),
        "outline_color": str(layer.get("outline_color") or "#000000"),
        "outline_width": _safe_int(layer.get("outline_width_px"), 0),
        "glow_color": "#000000",
        "glow_radius": 0,
        "opacity": _safe_float(layer.get("opacity"), 1.0),
        "align": layer.get("align") if layer.get("align") in ("left", "center", "right") else "center",
        "start_sec": start,
        "end_sec": (
            None
            if layer.get("end_sec") is None
            else _safe_float(layer.get("end_sec"), 0.0)
        ),
        "enter_effect": layer.get("enter_effect", "fade"),
        "exit_effect": layer.get("exit_effect", "fade"),
        "enter_duration_sec": _safe_float(layer.get("enter_duration_sec"), 0.3),
        "exit_duration_sec": _safe_float(layer.get("exit_duration_sec"), 0.3),
    }


def _shape_layer_to_overlay(
    layer: dict,
    canvas_w: int,
    canvas_h: int,
    existing_shapes: list[dict],
) -> dict:
    """RenderSpec ShapeLayer → SceneCard ShapeOverlay."""
    x_ratio = _px_to_ratio(layer.get("x"), canvas_w, 0.0)
    y_ratio = _px_to_ratio(layer.get("y"), canvas_h, 0.0)
    w_ratio = _px_to_ratio(layer.get("width"), canvas_w, 0.1)
    h_ratio = _px_to_ratio(layer.get("height"), canvas_h, 0.1)
    start = _safe_float(layer.get("start_sec"), 0.0)
    matched_id = _find_id_by_pos(existing_shapes, "", x_ratio, y_ratio, start)
    return {
        "id": matched_id or _new_id("shape"),
        "shape": layer.get("shape") if layer.get("shape") in ("rect", "ellipse") else "rect",
        "x": x_ratio,
        "y": y_ratio,
        "width": w_ratio,
        "height": h_ratio,
        "color": str(layer.get("color") or "#FFFFFF"),
        "opacity": _safe_float(layer.get("opacity"), 1.0),
        "start_sec": start,
        "end_sec": (
            None
            if layer.get("end_sec") is None
            else _safe_float(layer.get("end_sec"), 0.0)
        ),
        "enter_effect": layer.get("enter_effect", "fade"),
        "exit_effect": layer.get("exit_effect", "fade"),
        "enter_duration_sec": _safe_float(layer.get("enter_duration_sec"), 0.3),
        "exit_duration_sec": _safe_float(layer.get("exit_duration_sec"), 0.3),
    }


def _graphic_layer_to_overlay(
    layer: dict,
    canvas_w: int,
    canvas_h: int,
    existing_graphics: list[dict],
) -> dict:
    """RenderSpec GraphicLayer → SceneCard GraphicOverlay."""
    x_ratio = _px_to_ratio(layer.get("center_x"), canvas_w, 0.5)
    y_ratio = _px_to_ratio(layer.get("center_y"), canvas_h, 0.5)
    start = _safe_float(layer.get("start_sec"), 0.0)
    gtype = layer.get("graphic_type") or "spotlight"
    matched_id = _find_id_by_pos(existing_graphics, gtype, x_ratio, y_ratio, start)
    return {
        "id": matched_id or _new_id("graphic"),
        "type": gtype,
        "start_sec": start,
        "end_sec": (
            None
            if layer.get("end_sec") is None
            else _safe_float(layer.get("end_sec"), 0.0)
        ),
        "x": x_ratio,
        "y": y_ratio,
        "params": dict(layer.get("params") or {}),
    }


def _mosaic_layer_to_overlay(
    layer: dict,
    canvas_w: int,
    canvas_h: int,
    existing_mosaics: list[dict],
) -> dict:
    """RenderSpec MosaicLayer → SceneCard MosaicOverlay."""
    x_ratio = _px_to_ratio(layer.get("x"), canvas_w, 0.4)
    y_ratio = _px_to_ratio(layer.get("y"), canvas_h, 0.4)
    w_ratio = _px_to_ratio(layer.get("width"), canvas_w, 0.2)
    h_ratio = _px_to_ratio(layer.get("height"), canvas_h, 0.2)
    start = _safe_float(layer.get("start_sec"), 0.0)
    matched_id = _find_id_by_pos(existing_mosaics, "", x_ratio, y_ratio, start)
    return {
        "id": matched_id or _new_id("mosaic"),
        "shape": layer.get("shape") if layer.get("shape") in ("rect", "ellipse") else "rect",
        "x": x_ratio,
        "y": y_ratio,
        "width": w_ratio,
        "height": h_ratio,
        "pixel_size": _safe_int(layer.get("pixel_size"), 20),
        "mode": layer.get("mode") if layer.get("mode") in ("pixelate", "blur") else "pixelate",
        "blur_radius": _safe_int(layer.get("blur_radius"), 20),
        "start_sec": start,
        "end_sec": (
            None
            if layer.get("end_sec") is None
            else _safe_float(layer.get("end_sec"), 0.0)
        ),
    }


# RenderSpec MotionLayer 가 표현하는 유효 motion_type. builder._MOTION_TYPES_VALID 와
# 동일 집합(shake / slowmo / clip_zoom / stutter). 미지원 타입은 환원 시 스킵한다.
_MOTION_TYPES_VALID = ("shake", "slowmo", "clip_zoom", "stutter")


def _find_motion_source_id(
    existing_motions: list[dict],
    motion_type: str,
    start_sec: float,
    *,
    tol: float = 0.05,
) -> str | None:
    """기존 motion 중 동일 motion_type + 시작시간 근사인 것의 source_id 를 복구한다.

    MotionLayer 는 ``source_id`` 를 직렬화하지 않으므로(tag 시스템 메타데이터),
    RenderSpec 라운드트립 시 출처 식별자가 소실된다. NL 편집이 기존 motion 의
    속성만 바꾼 경우 source_id 가 보존되도록 위치(motion_type)+시작시간으로 매칭한다.
    """
    best_id: str | None = None
    best_dt = float("inf")
    for m in existing_motions:
        if not isinstance(m, dict):
            continue
        if m.get("motion_type") != motion_type:
            continue
        dt = abs(_safe_float(m.get("start_sec"), -99) - start_sec)
        if dt <= tol and dt < best_dt:
            best_dt = dt
            sid = m.get("source_id")
            best_id = str(sid) if sid else None
    return best_id


def _motion_layer_to_motion(
    layer: dict,
    existing_motions: list[dict],
) -> dict | None:
    """RenderSpec MotionLayer → SceneCard SceneMotion (#349).

    MotionLayer 의 ``motion_start_sec`` / ``motion_duration_sec`` 는 SceneMotion 의
    ``start_sec`` / ``duration_sec`` 에 대응한다(builder._build_motion_layer 의 역변환).
    미지원/빈 motion_type 은 None 으로 스킵.
    """
    mtype = layer.get("motion_type")
    if mtype not in _MOTION_TYPES_VALID:
        return None
    start = max(0.0, _safe_float(layer.get("motion_start_sec"), 0.0))
    dur = _safe_float(layer.get("motion_duration_sec"), 1.0)
    if dur <= 0:
        dur = 1.0
    source_id = _find_motion_source_id(existing_motions, str(mtype), start)
    return {
        "motion_type": mtype,
        "start_sec": start,
        "duration_sec": dur,
        "params": dict(layer.get("params") or {}),
        "source_id": source_id,
    }


# ── 씬 단위 머지 ─────────────────────────────────────────────────────────


def merge_render_spec_into_scene_card(
    render_spec_scene: dict,
    existing_scene: dict | None,
) -> dict:
    """RenderSpec 1개 씬을 SceneCard dict 로 머지한다.

    `existing_scene` 이 있으면 변환 불가 필드(filters, narration_text 등)는
    그대로 유지하고, 변환 가능 필드(motions 포함, #349)만 RenderSpec 값으로 덮어쓴다.
    단 motions 는 RenderSpec 에 motion 레이어가 있을 때만 대체한다. `existing_scene` 이
    None 이면 변환 가능 필드만 채운 최소 SceneCard 를 반환한다(드물게 RenderSpec 만
    살아남은 상황 — 정상 운영에선 발생하지 않아야 함).
    """
    canvas = render_spec_scene.get("canvas") or {}
    cw = _safe_int(canvas.get("width"), 1280) or 1280
    ch = _safe_int(canvas.get("height"), 720) or 720

    layers = render_spec_scene.get("layers") or []

    # 기존 overlay 들 (id 보존용 매칭 풀)
    existing_texts = list((existing_scene or {}).get("text_overlays") or [])
    existing_shapes = list((existing_scene or {}).get("shape_overlays") or [])
    existing_graphics = list((existing_scene or {}).get("graphic_overlays") or [])
    existing_mosaics = list((existing_scene or {}).get("mosaic_overlays") or [])
    existing_motions = list((existing_scene or {}).get("motions") or [])

    new_texts: list[dict] = []
    new_shapes: list[dict] = []
    new_graphics: list[dict] = []
    new_mosaics: list[dict] = []
    new_motions: list[dict] = []
    # RenderSpec 에 motion 레이어가 하나라도 있으면 motions 를 *대체*한다.
    # (builder 가 모든 SceneCard.motions 를 MotionLayer 로 직렬화하므로 라운드트립이
    #  성립한다 — 부분 누락이 아닌 단일 진실 채택.) 하나도 없으면 기존 motions 보존.
    has_motion_layer = False
    new_footage_speed: float | None = None
    new_speed_mode: dict | None = None

    for layer in layers:
        if not isinstance(layer, dict):
            continue
        ltype = layer.get("type")
        if ltype == "text":
            new_texts.append(_text_layer_to_overlay(layer, cw, ch, existing_texts))
        elif ltype == "shape":
            new_shapes.append(_shape_layer_to_overlay(layer, cw, ch, existing_shapes))
        elif ltype == "graphic":
            new_graphics.append(_graphic_layer_to_overlay(layer, cw, ch, existing_graphics))
        elif ltype == "mosaic":
            new_mosaics.append(_mosaic_layer_to_overlay(layer, cw, ch, existing_mosaics))
        elif ltype == "motion":
            has_motion_layer = True
            converted = _motion_layer_to_motion(layer, existing_motions)
            if converted is not None:
                new_motions.append(converted)
        elif ltype in ("video", "image"):
            raw_rate = layer.get("playback_rate")
            # ImageLayer 모델은 playback_rate 필드를 갖지 않으므로 직렬화 결과에 키가
            # 존재하지 않는다(`None`). 이 경우 footage_speed 갱신 대상이 아니다 —
            # 기존 SceneCard.footage_speed 를 그대로 유지해야 한다 (정보 손실 최소화 원칙).
            if raw_rate is None:
                continue
            rate = _safe_float(raw_rate, 1.0)
            # 0.0 / 음수 방어 — 기본 1.0 유지
            if rate > 0:
                new_footage_speed = rate
            # 재생 속도 모드(ramp) — VideoLayer 에 키가 있을 때만 반영(없으면 기존 값 유지).
            if layer.get("speed_mode") in ("constant", "ramp", "ramp_down"):
                new_speed_mode = {
                    "footage_speed_mode": layer.get("speed_mode"),
                    "footage_speed_ramp_hold": _safe_float(layer.get("speed_ramp_hold", 0.2), 0.2),
                    "footage_speed_ramp_accel": _safe_float(layer.get("speed_ramp_accel", 0.3), 0.3),
                    "footage_speed_ramp_base": _safe_float(layer.get("speed_ramp_base", 1.0), 1.0),
                }

    # 기반: 기존 씬을 복사하고 변환 가능 필드만 갱신
    merged: dict = dict(existing_scene or {})
    # RenderSpec 의 scene_id 를 우선시 (UI 가 표시할 라벨)
    rs_scene_id = render_spec_scene.get("scene_id")
    if rs_scene_id is not None:
        merged["scene_id"] = _safe_int(rs_scene_id, merged.get("scene_id", 0))

    # 새 RenderSpec 이 가진 overlay 들로 *대체* (NL 편집 결과를 단일 진실로 채택)
    merged["text_overlays"] = new_texts
    merged["shape_overlays"] = new_shapes
    merged["graphic_overlays"] = new_graphics
    merged["mosaic_overlays"] = new_mosaics
    if new_footage_speed is not None:
        merged["footage_speed"] = new_footage_speed
    if new_speed_mode is not None:
        merged.update(new_speed_mode)

    # motions: RenderSpec 에 motion 레이어가 존재할 때만 대체(#349).
    # 한 개도 없으면 기존 SceneCard.motions 를 보존한다(NL 편집이 motion 을 건드리지
    # 않은 경우 — RenderSpec builder 가 항상 motions 를 직렬화하므로, motion 레이어
    # 부재 == "이 씬에 motion 이 없다"는 의미가 아니라 builder 를 거치지 않은 RenderSpec
    # 일 수 있어 보수적으로 보존한다).
    if has_motion_layer:
        merged["motions"] = new_motions

    # 변환 불가 필드(filters, narration_text, footage 등) 는 그대로 보존
    return merged


def sync_scene_cards_from_render_spec(
    render_spec_payload: dict,
    project: Any,
) -> list[dict]:
    """RenderSpec 페이로드(`{"scenes": [...]}`)에서 SceneCard dict 리스트를 만든다.

    Project.scenes 와 RenderSpec scenes 를 scene_id 기준으로 매칭하여,
    각 RenderSpec 씬을 동일 scene_id 의 기존 SceneCard 와 머지한다. 매칭 실패 시
    (예: NL 편집이 새 씬을 추가) 빈 베이스로 변환만 수행.

    반환은 *SceneCard 의 dict 직렬화* 형태. 프론트가 `Project.scenes` 에 그대로
    꽂아 넣을 수 있다.
    """
    rs_scenes = list(render_spec_payload.get("scenes") or [])
    existing_scenes_list = list(getattr(project, "scenes", []) or [])

    # scene_id → 기존 SceneCard dict 매핑
    existing_by_id: dict[int, dict] = {}
    for sc in existing_scenes_list:
        try:
            # Pydantic 모델이면 model_dump, 아니면 dict 캐스팅 시도
            if hasattr(sc, "model_dump"):
                d = sc.model_dump()
            elif isinstance(sc, dict):
                d = dict(sc)
            else:
                d = dict(sc.__dict__)
            sid = _safe_int(d.get("scene_id"))
            existing_by_id[sid] = d
        except Exception:
            continue

    merged_cards: list[dict] = []
    for rs_scene in rs_scenes:
        if not isinstance(rs_scene, dict):
            continue
        sid = _safe_int(rs_scene.get("scene_id"))
        existing = existing_by_id.get(sid)
        merged_cards.append(merge_render_spec_into_scene_card(rs_scene, existing))

    return merged_cards


__all__ = [
    "sync_scene_cards_from_render_spec",
    "merge_render_spec_into_scene_card",
]
