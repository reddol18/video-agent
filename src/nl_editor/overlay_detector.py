"""이벤트 기반 자동 텍스트+SFX 오버레이 생성 — Gemini로 영상 분석.

사용자가 "X 할 때마다 'Y' 텍스트가 뜨고 효과음 재생" 같은 명령을 내리면,
720p 프록시 영상을 Gemini에 보내 이벤트 시점·위치·텍스트·폰트·SFX를 한 번에 결정한다.

split_detector.py와 동일 구조(키워드 분류 + 영상+JSON 강제 호출).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 한 번에 너무 많이 추가하는 폭주 방지 상한.
MAX_OVERLAYS = 10

# 폰트 화이트리스트 (fonts/ 폴더의 실제 파일과 일치). LLM 환각 차단.
ALLOWED_FONTS = ["NotoSansKR", "DungGeunMo", "BMDOHYEON", "PF_STARDUST"]
FONT_HINTS = {
    "NotoSansKR": "표준 고딕, 깔끔한 본문/자막용",
    "DungGeunMo": "둥글둥글, 부드러운 느낌",
    "BMDOHYEON": "굵고 귀여운 느낌, 강조에 적합",
    "PF_STARDUST": "픽셀 게임 느낌, 레트로",
}


def is_overlay_intent(instruction: str) -> bool:
    """이벤트 기반 텍스트/효과음 추가 의도인지 키워드로 판단."""
    s = instruction.lower()
    text_kw = ["텍스트", "자막", "말풍선", "효과", "표시", "뜨고", "뜨게", "넣어", "달아", "올려"]
    event_kw = ["마다", "할 때마다", "찌를", "찌르", "순간", "장면", "치는", "웃는", "보이는", "나올", "나오"]
    has_text = any(k in s for k in text_kw)
    has_event = any(k in s for k in event_kw)
    return has_text and has_event


def detect_overlay_events(
    video_path: str,
    instruction: str,
    scene_context: dict[str, Any],
    sfx_candidates: list[dict[str, str]],
    model: str | None = None,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Gemini에 영상+명령을 보내 overlay 목록을 받는다.

    반환:
      overlays: [{ time_sec, text, x, y, font_family, color, sfx_asset_id }, ...]
                time_sec는 프록시 영상 기준 절대 초(호출부가 씬 상대로 변환).
      reasoning: LLM 설명.
      meta: 토큰/글자수 비용 기록용.
    """
    if not Path(video_path).exists():
        raise FileNotFoundError(f"프록시 영상 없음: {video_path}")

    clip_start = float(scene_context.get("clip_start_sec") or 0.0)
    clip_end = scene_context.get("clip_end_sec")
    clip_end_str = f"{float(clip_end):.2f}" if clip_end is not None else "(끝)"

    font_lines = "\n".join(f"  - {f}: {FONT_HINTS[f]}" for f in ALLOWED_FONTS)
    sfx_block = (
        "(등록된 SFX 자산 없음 — 모든 항목 sfx_asset_id=null)"
        if not sfx_candidates
        else "\n".join(
            f"  - id: {s['asset_id']}, label: {s.get('label') or '(없음)'}, file: {s.get('filename') or '(없음)'}"
            for s in sfx_candidates
        )
    )

    system_prompt = (
        "당신은 영상 편집 보조 AI입니다. 첨부된 영상에서 사용자가 말한 이벤트가 발생하는 모든 시점을 찾아, "
        "각 시점에 표시할 텍스트 오버레이를 자동 생성합니다.\n\n"
        "규칙:\n"
        "1) 시간 단위: 초(소수점 1자리). 첨부 영상의 처음(0초) 기준.\n"
        "2) 텍스트는 사용자 명령에서 추출(예: \"'뽈록'\" → text='뽈록'). 명시되지 않으면 의성어/감탄사 1~3자.\n"
        "3) 위치(x, y)는 영상 프레임 정규화 좌표(0.0~1.0). 좌상단 (0,0), 우하단 (1,1). "
        "명령이 '배 위'처럼 객체 위치를 가리키면 비전으로 추정. 모호하면 (0.5, 0.3).\n"
        "4) font_family는 아래 4종 중 명령 톤에 맞는 것을 선택:\n"
        f"{font_lines}\n"
        "5) color는 #RRGGBB. 명령에 색 지정 없으면 가독성 높은 흰색 '#ffffff'.\n"
        "6) SFX: 명령에 효과음 단서('뽈록 소리', '팝 사운드')가 있으면 아래 후보에서 가장 비슷한 asset_id 선택, "
        "없거나 매칭 불가면 null.\n\n"
        f"7) 최대 {MAX_OVERLAYS}개. **같은 동작의 연속된 프레임은 1개 이벤트로 간주**하라 — "
        f"동작이 시작되고 끝나는 동안 여러 시점을 따로 셀 것이 아니라, 동작이 시작되는 순간 하나만 잡는다. "
        f"이전 동작이 끝나고 1초 이상 지난 뒤 새로 시작되는 동작만 별도 이벤트.\n"
        "8) 응답은 JSON만: {\"overlays\":[{\"time_sec\":float,\"text\":str,\"x\":float,\"y\":float,"
        "\"font_family\":str,\"color\":str,\"sfx_asset_id\":str|null}],\"reasoning\":str}\n"
        "9) 이벤트를 못 찾으면 overlays=[]."
    )

    user_prompt = (
        f"이 영상은 더 큰 원본 footage 중 {clip_start:.2f}초~{clip_end_str} 구간을 720p로 압축한 미리보기입니다.\n\n"
        f"사용자 명령: \"{instruction}\"\n\n"
        f"사용 가능한 SFX 후보:\n{sfx_block}\n\n"
        f"명령에 맞는 오버레이들을 반환하세요."
    )

    from src.utils.gemini_client import generate_json_with_video

    raw = generate_json_with_video(video_path, user_prompt, system=system_prompt, model=model)
    overlays, reasoning = _parse_response(raw)

    # 정규화 + 안전장치
    overlays = _normalize_overlays(overlays, sfx_candidates)
    overlays = overlays[:MAX_OVERLAYS]

    # 영상 입력 토큰(295 tok/s) 누락 방지를 위해 video_duration_sec도 함께 기록.
    if clip_end is not None:
        duration = max(0.0, float(clip_end) - clip_start)
    else:
        duration = 0.0
    # #355 — 어떤 모델로 호출됐는지 meta 에 명시.
    try:
        from src.cost_estimator import get_gemini_default_model
        used_model = model or get_gemini_default_model()
    except Exception:
        used_model = model or "gemini-2.5-flash"
    meta: dict[str, Any] = {
        "input_chars": len(system_prompt) + len(user_prompt),
        "output_chars": len(raw),
        "video_duration_sec": float(duration),
        "model": used_model,
    }
    return overlays, reasoning, meta


def _parse_response(raw: str) -> tuple[list[dict[str, Any]], str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return [], ""
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [], ""
    if not isinstance(data, dict):
        return [], ""
    overlays = data.get("overlays") or []
    if not isinstance(overlays, list):
        overlays = []
    reasoning = str(data.get("reasoning") or "")
    return overlays, reasoning


def _normalize_overlays(
    overlays: list[Any], sfx_candidates: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """폰트·색·SFX·좌표 검증/보정 + 시간순 정렬 + 너무 짧은 간격 제거."""
    valid_sfx_ids = {s["asset_id"] for s in sfx_candidates}
    cleaned: list[dict[str, Any]] = []
    for o in overlays:
        if not isinstance(o, dict):
            continue
        try:
            t = float(o.get("time_sec", 0))
        except (TypeError, ValueError):
            continue
        if t < 0:
            continue
        text = str(o.get("text") or "").strip()
        if not text:
            continue
        font = str(o.get("font_family") or "NotoSansKR")
        if font not in ALLOWED_FONTS:
            font = "NotoSansKR"
        color = str(o.get("color") or "#ffffff")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = "#ffffff"
        try:
            x = max(0.0, min(1.0, float(o.get("x", 0.5))))
            y = max(0.0, min(1.0, float(o.get("y", 0.3))))
        except (TypeError, ValueError):
            x, y = 0.5, 0.3
        sfx_id = o.get("sfx_asset_id")
        if sfx_id is not None and sfx_id not in valid_sfx_ids:
            sfx_id = None
        cleaned.append({
            "time_sec": round(t, 2),
            "text": text,
            "x": round(x, 3),
            "y": round(y, 3),
            "font_family": font,
            "color": color,
            "sfx_asset_id": sfx_id,
        })
    cleaned.sort(key=lambda x: x["time_sec"])
    # 1.0초 미만 간격은 같은 동작으로 보고 합치기(뒤쪽 항목 제거) — LLM 중복 검출 후처리.
    merged: list[dict[str, Any]] = []
    for o in cleaned:
        if merged and o["time_sec"] - merged[-1]["time_sec"] < 1.0:
            continue
        merged.append(o)
    return merged
