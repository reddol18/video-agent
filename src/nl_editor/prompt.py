"""자연어 편집 — 시스템 프롬프트 빌더 + few-shot 예시 (#315).

LLM에 던지는 컨텍스트는 다음 5가지를 결합한다.

1. **역할 + 응답 형식 명세** — JSON으로 `{patch, summary, safety_flags}` 만 응답하게 강제.
2. **RenderSpec 스키마 요약** — 어떤 path 가 존재하는지, 좌표 단위는 무엇인지.
3. **이펙트 카탈로그** — `EFFECT_REGISTRY` 를 압축한 JSON. 사용 가능한 효과명/파라미터.
4. **현재 RenderSpec JSON** — 사용자가 보고 있는 *지금* 상태.
5. **Few-shot 예시 N종** — "이런 명령에는 이런 patch" 패턴 학습.

Few-shot 분량
------------
#315 본문 완료 조건 "30종 few-shot 예시 작성 및 정확도 검증" 을 충족하기 위해,
#332 단계에서 **총 30개** 예시로 확장했다. 카테고리는 이슈 #315 가 명시한 5종
(텍스트/색/위치/속도/순서 변경) 을 각 5개 이상, 그 외 효과 추가/위험 패턴까지 포함한다.

시스템 프롬프트가 길어지는 비용은 #317(Gemini 컨텍스트 캐싱)에서 시스템 블록
(스키마 + 카탈로그 + few-shot)을 캐시 토큰으로 분리함으로써 회수한다. 그 전에는
`build_system_prompt` 자체 호출이 한 턴당 1회뿐이므로 30종이 곧바로 비용을 30배로
만들지는 않는다(자세한 측정은 docs/review-notes.md 참고).

정확도 검증
----------
각 예시의 `response.patch` 가 표준 RenderSpec 샘플에 round-trip 적용 가능한지 회귀
테스트(`tests/nl_editor/test_few_shot_accuracy.py`) 가 자동으로 검증한다. patcher 가
거부하는 예시는 prompt.py 가 즉시 빨간불을 받는 구조다.

응답 형식 강제
-------------
Gemini SDK는 `response_mime_type="application/json"` 으로 JSON 강제를 지원한다. 다만
스키마를 함께 넘기면 안정성이 더 올라가는데, 본 구현에서는 프롬프트로 형식을 명시하고
다운스트림 `patcher.py` 가 Pydantic 으로 재검증하는 2중 안전망을 둔다.
"""

from __future__ import annotations

import json
from typing import Any, Optional


# ── 이펙트 카탈로그 압축 ─────────────────────────────────────────────────

def catalog_to_compact_json(effect_registry: dict[str, Any]) -> str:
    """`EFFECT_REGISTRY` 를 LLM 친화 JSON으로 압축.

    `display_label`/`tooltip`/`notes` 처럼 사람용 한글 라벨은 LLM이 명령을 해석할 때
    유용하므로 그대로 둔다. 반대로 `preview_supported`/`backend_layer_type` 같은
    UI 힌트는 LLM 결정에 영향이 없으므로 *제거*하여 토큰을 절약한다.

    반환은 보기 좋게 들여쓴 JSON 문자열 — 디버깅·테스트 가독성 우선.
    """
    compact: list[dict[str, Any]] = []
    for key, eff in effect_registry.items():
        # EffectDefinition Pydantic 모델 → dict
        if hasattr(eff, "model_dump"):
            d = eff.model_dump()
        elif isinstance(eff, dict):
            d = dict(eff)
        else:
            continue
        # 토큰 절약 — UI 전용 필드 제거
        for drop in ("preview_supported", "backend_layer_type", "notes"):
            d.pop(drop, None)
        # 카탈로그 키도 같이 노출 (LLM이 효과명을 정확히 짚을 수 있도록)
        d["catalog_key"] = key
        compact.append(d)
    return json.dumps(
        {"effects": compact},
        ensure_ascii=False,
        indent=2,
        default=str,
    )


# ── Few-shot 예시 ────────────────────────────────────────────────────────

def build_few_shot_examples() -> list[dict[str, Any]]:
    """32개 대표 예시 (#332 확장, #347 stutter motion 2종 추가). 카테고리별로 path 패턴/응답 구조를 보여준다.

    각 예시 구조:
    - `category`: 분류 (디버그/테스트용)
    - `instruction`: 자연어 명령 — 김감독(CLI 비숙련자)이 실제로 입력할 법한 한국어
    - `response`: 모델이 출력해야 하는 JSON

    카테고리 분포 (#315 본문 명세 ≥5종/카테고리):
    - text: 5종 (내용/크기/색/위치/외곽선·굵게 등)
    - filter: 5종 (vivid/horror/flashback/nightvision/pov)
    - shape: 5종 (도형 추가/이동/크기 조정/색 변경/모자이크 이동)
    - time: 5종 (씬 길이/배속/시작시점/줄임/슬로우)
    - order: 5종 (위치 교체/삭제/복사 add/순서 셔플/맨 앞 이동)
    - effect_add: 5종 (모자이크/그래픽/복합 + stutter 모션 2종 #347)
    - safety: 2종 (canvas 변경, 전체 씬 삭제) — 위험 패턴 학습용
    """
    return [
        # ── text 카테고리 (5종) ──────────────────────────────────────────────
        # 1. 텍스트 내용 변경
        {
            "category": "text",
            "instruction": "1번 씬의 자막을 '오늘의 핵심'으로 바꿔줘",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/0/layers/1/text", "value": "오늘의 핵심"}
                ],
                "summary": "1번 씬 텍스트를 '오늘의 핵심'으로 변경합니다.",
                "safety_flags": [],
            },
        },
        # 2. 텍스트 크기 조정
        {
            "category": "text",
            "instruction": "2번 씬 자막 더 크게",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/1/layers/1/font_size_px", "value": 72}
                ],
                "summary": "2번 씬 자막 폰트를 키웠습니다(예: 48 → 72px).",
                "safety_flags": [],
            },
        },
        # 3. 텍스트 색 변경
        {
            "category": "text",
            "instruction": "마지막 씬 자막을 빨간색으로",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/-1/layers/1/color", "value": "#FF0000"}
                ],
                "summary": "마지막 씬 자막 색을 빨간색(#FF0000)으로 변경합니다.",
                "safety_flags": [],
            },
        },
        # 4. 텍스트 위치 이동 (가운데 정렬용 좌표)
        {
            "category": "text",
            "instruction": "1번 씬 자막을 화면 아래쪽으로 내려줘",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/0/layers/1/position_y", "value": 920}
                ],
                "summary": "1번 씬 자막의 세로 위치를 화면 하단(y=920)으로 이동합니다.",
                "safety_flags": [],
            },
        },
        # 5. 텍스트 굵게 + 외곽선
        {
            "category": "text",
            "instruction": "3번 씬 자막을 굵게 하고 검정 외곽선 좀 두껍게",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/2/layers/1/bold", "value": True},
                    {"op": "replace", "path": "/scenes/2/layers/1/outline_color", "value": "#000000"},
                    {"op": "replace", "path": "/scenes/2/layers/1/outline_width_px", "value": 4},
                ],
                "summary": "3번 씬 자막을 굵게 만들고 검정 외곽선을 4px로 두껍게 합니다.",
                "safety_flags": [],
            },
        },

        # ── filter 카테고리 (5종) ────────────────────────────────────────────
        # 6. 필터 — vivid (밝고 선명)
        {
            "category": "filter",
            "instruction": "전체적으로 좀 더 밝고 선명하게 해줘",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/0/layers/-",
                        "value": {
                            "type": "graphic",
                            "graphic_type": "spotlight",
                            "center_x": 960,
                            "center_y": 540,
                            "params": {"filter": "vivid", "intensity": 0.6},
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "1번 씬에 vivid 필터를 추가해 색감을 강화합니다.",
                "safety_flags": [],
            },
        },
        # 7. 필터 — horror (어둡고 음울)
        {
            "category": "filter",
            "instruction": "2번 씬 분위기를 좀 무섭게 어둡게 해줘",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/1/layers/-",
                        "value": {
                            "type": "graphic",
                            "graphic_type": "spotlight",
                            "center_x": 960,
                            "center_y": 540,
                            "params": {"filter": "horror", "intensity": 0.7},
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "2번 씬에 horror 필터를 추가해 어둡고 음울한 분위기를 만듭니다.",
                "safety_flags": [],
            },
        },
        # 8. 필터 — flashback (회상/흑백 톤)
        {
            "category": "filter",
            "instruction": "마지막 씬에 회상 느낌 입혀줘",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/-1/layers/-",
                        "value": {
                            "type": "graphic",
                            "graphic_type": "spotlight",
                            "center_x": 960,
                            "center_y": 540,
                            "params": {"filter": "flashback", "intensity": 0.8},
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "마지막 씬에 flashback 필터를 추가해 회상 분위기를 입힙니다.",
                "safety_flags": [],
            },
        },
        # 9. 필터 — nightvision (야간 투시)
        {
            "category": "filter",
            "instruction": "1번 씬 야시경처럼 만들어줘",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/0/layers/-",
                        "value": {
                            "type": "graphic",
                            "graphic_type": "spotlight",
                            "center_x": 960,
                            "center_y": 540,
                            "params": {"filter": "nightvision", "intensity": 1.0},
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "1번 씬에 nightvision 필터를 추가해 야간 투시경 분위기를 만듭니다.",
                "safety_flags": [],
            },
        },
        # 10. 필터 — pov (1인칭 시점)
        {
            "category": "filter",
            "instruction": "3번 씬 1인칭 시점 같은 느낌 줘",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/2/layers/-",
                        "value": {
                            "type": "graphic",
                            "graphic_type": "spotlight",
                            "center_x": 960,
                            "center_y": 540,
                            "params": {"filter": "pov", "intensity": 0.7},
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "3번 씬에 pov 필터를 추가해 1인칭 시점 분위기를 만듭니다.",
                "safety_flags": [],
            },
        },

        # ── shape/position 카테고리 (5종) ────────────────────────────────────
        # 11. 모자이크 영역 이동
        {
            "category": "shape",
            "instruction": "3번 씬의 모자이크를 가운데로 이동",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/2/layers/2/x", "value": 760},
                    {"op": "replace", "path": "/scenes/2/layers/2/y", "value": 440},
                ],
                "summary": "3번 씬 모자이크 위치를 캔버스 중앙으로 옮깁니다.",
                "safety_flags": [],
            },
        },
        # 12. 도형 추가 — 빨간 사각형 강조
        {
            "category": "shape",
            "instruction": "2번 씬에 빨간 사각형 강조 표시 추가",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/1/layers/-",
                        "value": {
                            "type": "shape",
                            "shape": "rect",
                            "x": 660,
                            "y": 340,
                            "width": 600,
                            "height": 400,
                            "color": "#FF0000",
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 0.6,
                        },
                    }
                ],
                "summary": "2번 씬에 빨간 사각형 강조 표시를 추가합니다.",
                "safety_flags": [],
            },
        },
        # 13. 도형 크기 조정 — 강조 사각형(shape 레이어, 일반적으로 /layers/3)
        {
            "category": "shape",
            "instruction": "1번 씬 강조 사각형 두 배로 키워줘",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/0/layers/3/width", "value": 800},
                    {"op": "replace", "path": "/scenes/0/layers/3/height", "value": 600},
                ],
                "summary": "1번 씬 강조 사각형의 가로/세로를 두 배로 키웁니다.",
                "safety_flags": [],
            },
        },
        # 14. 도형 색 변경 — shape 레이어
        {
            "category": "shape",
            "instruction": "마지막 씬 도형 색을 파란색으로",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/-1/layers/3/color", "value": "#1E90FF"}
                ],
                "summary": "마지막 씬 도형 색을 파란색(#1E90FF)으로 변경합니다.",
                "safety_flags": [],
            },
        },
        # 15. 모자이크 크기 줄이기
        {
            "category": "shape",
            "instruction": "1번 씬 모자이크 좀 작게 해줘",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/0/layers/2/width", "value": 240},
                    {"op": "replace", "path": "/scenes/0/layers/2/height", "value": 160},
                ],
                "summary": "1번 씬 모자이크 가로/세로를 240x160으로 줄입니다.",
                "safety_flags": [],
            },
        },

        # ── time 카테고리 (5종) ──────────────────────────────────────────────
        # 16. 씬 길이 늘리기
        {
            "category": "time",
            "instruction": "2번 씬을 8초로 늘려",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/1/duration_sec", "value": 8.0}
                ],
                "summary": "2번 씬 길이를 8.0초로 변경합니다.",
                "safety_flags": [],
            },
        },
        # 17. 배속 — 빠르게
        {
            "category": "time",
            "instruction": "4번 씬 영상을 1.5배속으로",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/3/layers/0/playback_rate", "value": 1.5}
                ],
                "summary": "4번 씬 영상의 배속을 1.5배로 변경합니다.",
                "safety_flags": [],
            },
        },
        # 18. 배속 — 슬로우 모션
        {
            "category": "time",
            "instruction": "마지막 씬 영상 절반 속도로 천천히",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/-1/layers/0/playback_rate", "value": 0.5}
                ],
                "summary": "마지막 씬 영상의 배속을 0.5배(슬로우)로 변경합니다.",
                "safety_flags": [],
            },
        },
        # 19. 자막 등장 시점 조정
        {
            "category": "time",
            "instruction": "1번 씬 자막을 1초 뒤에 나오게 해줘",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/0/layers/1/start_sec", "value": 1.0}
                ],
                "summary": "1번 씬 자막의 등장 시점을 1.0초로 늦춥니다.",
                "safety_flags": [],
            },
        },
        # 20. 씬 길이 줄이기
        {
            "category": "time",
            "instruction": "3번 씬 너무 기니까 4초로 줄여",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/2/duration_sec", "value": 4.0}
                ],
                "summary": "3번 씬 길이를 4.0초로 줄입니다.",
                "safety_flags": [],
            },
        },

        # ── order 카테고리 (5종) ─────────────────────────────────────────────
        # 21. 씬 순서 바꾸기 (RFC 6902 move)
        {
            "category": "order",
            "instruction": "2번 씬과 3번 씬 순서를 바꿔줘",
            "response": {
                "patch": [
                    {"op": "move", "from": "/scenes/1", "path": "/scenes/2"}
                ],
                "summary": "2번과 3번 씬 순서를 교체합니다.",
                "safety_flags": [],
            },
        },
        # 22. 마지막 씬 삭제
        {
            "category": "order",
            "instruction": "마지막 씬 지워줘",
            "response": {
                "patch": [
                    {"op": "remove", "path": "/scenes/-1"}
                ],
                "summary": "마지막 씬을 삭제합니다.",
                "safety_flags": [],
            },
        },
        # 23. 중간 씬 삭제
        {
            "category": "order",
            "instruction": "3번 씬은 빼줘",
            "response": {
                "patch": [
                    {"op": "remove", "path": "/scenes/2"}
                ],
                "summary": "3번 씬을 삭제합니다.",
                "safety_flags": [],
            },
        },
        # 24. 씬 맨 앞으로 이동
        {
            "category": "order",
            "instruction": "마지막 씬을 맨 앞으로 옮겨줘",
            "response": {
                "patch": [
                    {"op": "move", "from": "/scenes/-1", "path": "/scenes/0"}
                ],
                "summary": "마지막 씬을 첫 번째 위치로 이동합니다.",
                "safety_flags": [],
            },
        },
        # 25. 씬 복사 추가 (RFC 6902 copy)
        {
            "category": "order",
            "instruction": "1번 씬을 맨 뒤에 한 번 더 복사해줘",
            "response": {
                "patch": [
                    {"op": "copy", "from": "/scenes/0", "path": "/scenes/-"}
                ],
                "summary": "1번 씬을 복사해 맨 뒤에 추가합니다.",
                "safety_flags": [],
            },
        },

        # ── effect_add 카테고리 (3종) ────────────────────────────────────────
        # 26. 모자이크 추가 (기존 유지)
        {
            "category": "effect_add",
            "instruction": "1번 씬에 사각 모자이크를 추가",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/0/layers/-",
                        "value": {
                            "type": "mosaic",
                            "shape": "rect",
                            "x": 760,
                            "y": 440,
                            "width": 400,
                            "height": 200,
                            "mode": "pixelate",
                            "pixel_size": 20,
                            "blur_radius": 20,
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "1번 씬 중앙에 사각형 모자이크를 추가합니다.",
                "safety_flags": [],
            },
        },
        # 27. 그래픽 추가 — alert 아이콘
        {
            "category": "effect_add",
            "instruction": "2번 씬에 경고 아이콘 띄워줘",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/1/layers/-",
                        "value": {
                            "type": "graphic",
                            "graphic_type": "alert",
                            "center_x": 1600,
                            "center_y": 200,
                            "params": {},
                            "start_sec": 0.0,
                            "end_sec": 3.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "2번 씬에 경고(alert) 그래픽을 우상단에 추가합니다.",
                "safety_flags": [],
            },
        },
        # 28-a. 모션 추가 — stutter (끊김/점프) 기본 강도
        {
            "category": "effect_add",
            "instruction": "3번 씬 카메라가 뚝뚝 끊기게 해줘",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/2/layers/-",
                        "value": {
                            "type": "motion",
                            "motion_type": "stutter",
                            "motion_start_sec": 0.0,
                            "motion_duration_sec": 5.0,
                            "params": {"intensity": 0.3},
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "3번 씬에 끊김/점프(stutter) 모션을 추가합니다 (강도 0.3, 은은한 영화적 느낌).",
                "safety_flags": [],
            },
        },
        # 28-b. 모션 추가 — stutter 강하게 (글리치풍)
        {
            "category": "effect_add",
            "instruction": "이동하는 1번 씬을 좀 강하게 끊기는 느낌으로",
            "response": {
                "patch": [
                    {
                        "op": "add",
                        "path": "/scenes/0/layers/-",
                        "value": {
                            "type": "motion",
                            "motion_type": "stutter",
                            "motion_start_sec": 0.0,
                            "motion_duration_sec": 5.0,
                            "params": {"intensity": 0.8},
                            "start_sec": 0.0,
                            "end_sec": 5.0,
                            "opacity": 1.0,
                        },
                    }
                ],
                "summary": "1번 씬에 강한 끊김(stutter) 모션을 추가합니다 (강도 0.8, 글리치풍).",
                "safety_flags": [],
            },
        },
        # 29. 복합 — 텍스트 크기+색 한 번에
        {
            "category": "effect_add",
            "instruction": "1번 씬 자막 더 크게, 노란색으로",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/0/layers/1/font_size_px", "value": 80},
                    {"op": "replace", "path": "/scenes/0/layers/1/color", "value": "#FFFF00"},
                ],
                "summary": "1번 씬 자막 폰트를 키우고 색을 노란색으로 변경합니다.",
                "safety_flags": [],
            },
        },

        # ── safety 카테고리 (2종) — 위험 패턴 학습 ──────────────────────────
        # 31. canvas 크기 변경 — safety_flags 부착
        {
            "category": "safety",
            "instruction": "1번 씬을 세로 9:16 캔버스로 바꿔줘",
            "response": {
                "patch": [
                    {"op": "replace", "path": "/scenes/0/canvas/width", "value": 1080},
                    {"op": "replace", "path": "/scenes/0/canvas/height", "value": 1920},
                ],
                "summary": "1번 씬 캔버스를 1080x1920(9:16)으로 변경합니다. 다른 씬 레이아웃이 깨질 수 있어 확인이 필요합니다.",
                "safety_flags": ["canvas_size_change"],
            },
        },
        # 32. 전체 씬 삭제 — safety_flags 부착
        {
            "category": "safety",
            "instruction": "전체 씬 다 지워줘",
            "response": {
                "patch": [
                    {"op": "remove", "path": "/scenes"}
                ],
                "summary": "모든 씬을 삭제합니다. 되돌릴 수 없는 작업이므로 확인이 필요합니다.",
                "safety_flags": ["delete_all_scenes"],
            },
        },
    ]


# ── 시스템 프롬프트 본체 ─────────────────────────────────────────────────

_SCHEMA_HINT = """\
RenderSpec 스키마 요약 (LLM 참고용):

- 루트: {"scenes": [RenderSpec, ...]}
- RenderSpec:
  - scene_id: int (씬 번호)
  - canvas: { width: int, height: int, fps: int }
  - duration_sec: float (씬 전체 길이, 초)
  - layers: list[Layer] (아래에서 위 순서)
- Layer 공통:
  - type: "video"|"image"|"text"|"shape"|"graphic"|"mosaic"|"motion"
  - start_sec/end_sec: float, opacity: 0.0~1.0
- TextLayer: text(str), position_x/y(int px), font_size_px(int),
  color/outline_color(#RRGGBB), align("left"|"center"|"right"),
  bold/italic(bool), enter_effect/exit_effect("none"|"fade"|"typing"|"zoom"),
  enter/exit_duration_sec(float)
- ShapeLayer: shape("rect"|"ellipse"), x/y(int), width/height(int), color(#RRGGBB)
- MosaicLayer: shape, x/y/width/height(int), mode("pixelate"|"blur"),
  pixel_size(int), blur_radius(int)
- GraphicLayer: graphic_type("spotlight"|"target"|"alert"|"reaction"|"smell"|"emoji"),
  center_x/y(int), params(dict)
- VideoLayer: source(str), source_size, scale_x/y(float),
  playback_rate(float, 1.0=정상), rotation_deg(0|90|180|270)
- ImageLayer: zoom_from/to(float)
- MotionLayer: 화면 다이나믹 효과. motion_type("shake"|"clip_zoom"|"stutter"),
  motion_start_sec(float), motion_duration_sec(float, >0), params(dict).
  · shake: params.intensity(0.0~1.0) — 흔들림 강도
  · clip_zoom: params.target_x/target_y(0~1), start_scale/end_scale(float)
  · stutter: 이동 촬영 영상에 끊김/점프(저프레임) 모션. params.intensity(0.0~1.0,
    기본 0.3=은은한 영화적 끊김, 1.0=강한 글리치). 영상 길이는 보존된다.
  MotionLayer 는 씬 전체에 활성이므로 start_sec=0.0, end_sec=씬 duration_sec 로 둔다.

규칙:
- 좌표·크기는 절대 픽셀(int)
- 시간은 초(float)
- 색은 "#RRGGBB" 대문자
- fit/cover/contain 같은 추상 키워드는 RenderSpec에 없음 — scale_x/scale_y 만 존재
"""

_RESPONSE_FORMAT = """\
응답은 반드시 다음 JSON 형식만 사용한다. 다른 키 금지.

{
  "patch": [ { "op": "replace|add|remove|move|copy|test", "path": "/...", "value": ..., "from": "/..." }, ... ],
  "summary": "사용자에게 보여줄 한국어 변경 요약 (1~2문장)",
  "safety_flags": []
}

- patch는 RFC 6902 JSON Patch 배열.
- path는 JSON Pointer 형식. 음수 인덱스(-1, -2)와 끝 추가(-)도 사용 가능하다.
  · /scenes/0/layers/1/text — 1번 씬의 두 번째 레이어 text 필드
  · /scenes/-1 — 마지막 씬
  · /scenes/0/layers/- — 1번 씬 layers 배열 끝에 추가
- value는 RenderSpec 스키마를 만족해야 한다. 새 레이어를 추가할 때는 type 필드 필수.
- 위험한 요청(전체 layer 삭제, canvas 크기 변경)은 patch에 포함하되 safety_flags 에
  ["delete_all_layers"], ["canvas_size_change"], ["delete_all_scenes"] 중 해당 항목을 넣는다.
- 모호한 요청이면 가장 안전하고 가능성 높은 1개 변경만 patch에 담는다.
- JSON 외 텍스트(설명/마크다운)는 절대 출력하지 않는다.
"""


def build_system_prompt(
    *,
    effect_registry: dict[str, Any],
    render_spec_json: Any,
    history: Optional[list[dict[str, Any]]] = None,
    user_instruction: str = "",
) -> str:
    """LLM에 전달할 시스템 프롬프트 전문을 만든다.

    인자:
    - `effect_registry`: `EFFECT_REGISTRY` (dict[str, EffectDefinition])
    - `render_spec_json`: 현재 RenderSpec list (dict 형태 또는 list[dict])
    - `history`: 직전 N턴 — `[{"instruction": "...", "summary": "...", "applied": True}, ...]`
    - `user_instruction`: 이번 자연어 명령

    반환: 한국어로 작성된 단일 시스템 프롬프트 문자열.

    내부적으로 `build_static_prompt_block` + `build_dynamic_prompt_block` 을 단순 결합한다.
    Gemini 컨텍스트 캐싱(#317) 에서는 위 두 함수를 *별도로* 호출해서 정적 블록만 캐시한다.
    """
    static = build_static_prompt_block(effect_registry=effect_registry)
    dynamic = build_dynamic_prompt_block(
        render_spec_json=render_spec_json,
        history=history,
        user_instruction=user_instruction,
    )
    return static + "\n\n" + dynamic


# ── #317 — 정적/동적 분리 ────────────────────────────────────────────────

def build_static_prompt_block(*, effect_registry: dict[str, Any]) -> str:
    """캐시 가능한 *정적* 시스템 프롬프트 블록.

    포함:
    - 역할 선언
    - RenderSpec 스키마 요약 (`_SCHEMA_HINT`)
    - 효과 카탈로그 (compact JSON)
    - 응답 형식 명세 (`_RESPONSE_FORMAT`)
    - Few-shot 예시 30종

    제외(=동적 블록으로 이동):
    - 현재 RenderSpec
    - 최근 대화 history
    - 사용자 명령

    Gemini 컨텍스트 캐싱(#317) 에서는 이 블록을 `caches.create(...)` 로 등록하고,
    호출마다 `GenerateContentConfig.cached_content` 로 재사용한다. 약 6,000 토큰
    범위 — 캐시 단가($0.03/1M) 적용 시 호출당 입력 비용이 약 90% 절감된다.
    """
    catalog_block = catalog_to_compact_json(effect_registry)
    examples = build_few_shot_examples()
    examples_block = json.dumps(
        {"examples": [{"instruction": e["instruction"], "response": e["response"]} for e in examples]},
        ensure_ascii=False,
        indent=2,
    )
    parts = [
        "당신은 영상 편집 RenderSpec을 자연어 명령에 따라 변경하는 JSON Patch 생성기입니다.",
        "",
        _SCHEMA_HINT,
        "",
        "사용 가능한 효과 카탈로그 (참고):",
        catalog_block,
        "",
        "응답 형식:",
        _RESPONSE_FORMAT,
        "",
        "예시:",
        examples_block,
    ]
    return "\n".join(parts)


def build_dynamic_prompt_block(
    *,
    render_spec_json: Any,
    history: Optional[list[dict[str, Any]]] = None,
    user_instruction: str = "",
) -> str:
    """호출마다 달라지는 *동적* 블록 — RenderSpec + history + instruction.

    캐시되지 않는 부분으로, 매 호출 입력 토큰으로 청구된다.
    """
    if hasattr(render_spec_json, "model_dump"):
        render_block = json.dumps(render_spec_json.model_dump(), ensure_ascii=False, indent=2, default=str)
    else:
        render_block = json.dumps(render_spec_json, ensure_ascii=False, indent=2, default=str)

    history_block = ""
    if history:
        # 최근 5턴만 — 그 이상은 토큰 낭비.
        recent = history[-5:]
        history_block = "최근 대화 컨텍스트 (직전 → 가장 최근 순):\n" + json.dumps(
            recent, ensure_ascii=False, indent=2
        ) + "\n"

    parts = [
        "현재 RenderSpec (이번 명령의 입력):",
        render_block,
        "",
        history_block if history_block else "",
        f"사용자 명령: {user_instruction}" if user_instruction else "",
    ]
    return "\n".join(p for p in parts if p)


# #317 — 정적 블록의 버전 식별자. 캐시 키 구성에 사용한다.
# few-shot 또는 스키마 힌트를 변경할 때 이 값을 올려서 기존 캐시를 무효화한다.
STATIC_PROMPT_VERSION = "v2-2026-05-31-issue-347"


__all__ = [
    "build_system_prompt",
    "build_static_prompt_block",
    "build_dynamic_prompt_block",
    "build_few_shot_examples",
    "catalog_to_compact_json",
    "STATIC_PROMPT_VERSION",
]
