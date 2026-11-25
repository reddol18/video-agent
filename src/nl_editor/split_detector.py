"""이벤트 기반 자동 씬 분할 — Gemini로 영상 분석해 cuts_sec 검출.

사용자가 "X 장면을 찾아 그 직전에 나눠줘" 같은 명령을 내리면, 현재 선택된 씬의
720p 프록시 영상을 Gemini에 보내 이벤트 시점을 식별하고 분할 지점을 반환한다.

비용 최소화 — 원본이 아닌 cache/proxies/의 720p 프록시 사용(보통 < 20MB inline).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 한 번에 너무 많이 자르는 폭주 방지 상한.
MAX_CUTS = 10

# 이벤트 직전 여백(초) — 사용자가 보기에 이벤트가 새 씬의 시작점이 되도록 살짝 앞에서 자른다.
PRE_EVENT_LEAD_SEC = 0.3


def is_split_intent(instruction: str) -> bool:
    """간단한 키워드 분류 — 영상 이벤트 기반 분할 의도인지 판단.

    LLM 호출 없이 즉시 분류해 비용 절감. 키워드 못 잡으면 False(기존 patch 경로).
    """
    s = instruction.lower()
    # 분할 + 영상 이벤트 키워드가 함께 나오면 의도 인정
    split_kw = ["나눠", "나누어", "나누", "분할", "쪼개", "잘라", "자르"]
    event_kw = ["장면", "순간", "부분", "구간", "씬", "찌르", "치는", "웃는", "보이는", "나오는"]
    has_split = any(k in s for k in split_kw)
    has_event = any(k in s for k in event_kw)
    return has_split and has_event


def detect_split_points(
    video_path: str,
    instruction: str,
    scene_context: dict[str, Any],
    model: str | None = None,
) -> tuple[list[float], str, dict[str, Any]]:
    """Gemini에게 영상+명령을 보내 cuts_sec(footage 기준 절대 초)를 받는다.

    반환: (cuts_sec, reasoning, meta).
        cuts_sec: footage 시작 기준 절대 초(아직 씬 기준 보정 전).
        meta: 토큰 사용량 등 비용 기록용.
    """
    if not Path(video_path).exists():
        raise FileNotFoundError(f"프록시 영상 없음: {video_path}")

    clip_start = float(scene_context.get("clip_start_sec") or 0.0)
    clip_end = scene_context.get("clip_end_sec")
    clip_end_str = f"{float(clip_end):.2f}" if clip_end is not None else "(끝)"

    system_prompt = (
        "당신은 영상 편집 보조 AI입니다. 첨부된 영상에서 사용자가 말한 이벤트가 "
        "발생하는 시점을 모두 찾아 분할 지점을 반환합니다.\n"
        "규칙:\n"
        f"1) 시간 단위: 초(소수점 1자리). 첨부 영상의 처음(0초) 기준으로 측정합니다.\n"
        f"2) 각 이벤트가 새 씬의 첫 프레임으로 보이도록, 이벤트 발생 직전 "
        f"{PRE_EVENT_LEAD_SEC}초 시점을 cut으로 잡습니다.\n"
        f"3) 최대 {MAX_CUTS}개의 cut만 반환합니다. 너무 짧은 간격(< 0.5초)은 합칩니다.\n"
        f"4) 응답은 JSON: {{\"cuts_sec\": [3.2, 7.8, ...], \"reasoning\": \"...\"}}\n"
        "5) 이벤트를 못 찾았으면 cuts_sec=[] 반환."
    )

    user_prompt = (
        f"이 영상은 더 큰 원본 footage 중 {clip_start:.2f}초~{clip_end_str} 구간을 "
        f"잘라 720p로 압축한 미리보기입니다.\n\n"
        f"사용자 명령: \"{instruction}\"\n\n"
        f"명령에 맞는 이벤트들의 cut 시점을 반환하세요."
    )

    # 순환 import 회피: 호출 시점에 import.
    from src.utils.gemini_client import generate_json_with_video

    raw = generate_json_with_video(video_path, user_prompt, system=system_prompt, model=model)
    cuts, reasoning = _parse_response(raw)

    # 상한·정렬·중복 제거
    cuts = sorted({round(c, 2) for c in cuts if isinstance(c, (int, float)) and c >= 0})
    cuts = cuts[:MAX_CUTS]

    # 비용 기록용 meta — 영상 인라인이면 토큰 측정이 부정확하므로 글자수 폴백.
    # 영상 입력 토큰(295 tok/s) 누락 방지를 위해 video_duration_sec도 함께 기록.
    clip_end_val = scene_context.get("clip_end_sec")
    if clip_end_val is not None:
        duration = max(0.0, float(clip_end_val) - clip_start)
    else:
        duration = 0.0
    # #355 — 어떤 모델로 호출됐는지 meta 에 명시. None 이면 default_model.
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
    return cuts, reasoning, meta


def _parse_response(raw: str) -> tuple[list[float], str]:
    """LLM JSON 응답을 안전하게 파싱."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 코드 블록·여분 텍스트 제거 시도
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return [], ""
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [], ""
    if not isinstance(data, dict):
        return [], ""
    cuts = data.get("cuts_sec") or []
    if not isinstance(cuts, list):
        cuts = []
    reasoning = str(data.get("reasoning") or "")
    return cuts, reasoning
