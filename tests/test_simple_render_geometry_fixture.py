"""백엔드 builder geometry → fixture 자동 생성 + 회귀 가드 (#344).

배경
----
`scripts/verify_simple_render_parity.mjs` (Node only) 가 프론트 TS
`applySimpleRenderTransform` 의 미러 구현으로 *같은 RenderSpec 입력* 을 받아 CSS 백분율을
계산한 결과를, 백엔드 builder geometry(절대 픽셀)와 다시 1:1 매칭한다.

본 pytest 모듈은 그 검증의 *입력 데이터(fixture)* 를 자동 생성한다. 같은 케이스 매트릭스를
다른 곳(예: vitest, 골든 테스트)에서도 재사용할 수 있도록 fixture JSON 으로 직접 저장한다.

설계 원칙
---------
1. fixture 가 항상 git 트래킹된 최신 상태를 유지하도록 *결과를 파일에 쓰면서 동시에 회귀
   가드* 한다. 변경이 일어나면 본 pytest 가 통과해야 fixture 가 갱신되고, CI 의 mjs 검증이
   같은 fixture 를 읽어 TS 결과와 비교한다.
2. 케이스는 ``(canvas, source_size, fit, playback_rate, rotation)`` 매트릭스로 구성한다.
   #309 / #343 회귀 시리즈에서 문제가 됐던 9:16 캔버스, 1080→1920 cover/contain 분기를
   포함한다.

CI 의 mjs 검증은 두 단계로 동작한다:
- 1단계: fixture 의 ``expected_css`` 가 비어 있지 않은지, 입력/출력 키가 정합한지 검사.
- 2단계: fixture 의 ``layer`` 입력을 mjs 의 ``applySimpleRenderTransform`` 미러로 재계산해
  ``expected_css`` 와 비교 (±0.001 % 허용).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.render_spec.builder import build_render_spec
from src.render_spec.models import VideoLayer

# fixture 출력 경로 — mjs 스크립트와 단일 진실 공유.
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "simple_render_geometry_cases.json"


# ── 케이스 매트릭스: 회귀가 일어났던 fit / 캔버스 / 소스 조합 ──

# 캔버스 × 소스 매트릭스. 16:9 ↔ 9:16 cross-fit 회귀(2026-05-27 시리즈) 포함.
_GEOMETRY_CASES: list[tuple[tuple[int, int], tuple[int, int], str]] = [
    # 16:9 캔버스 + 가로 소스
    ((1920, 1080), (1920, 1080), "contain"),
    ((1920, 1080), (1920, 1080), "cover"),
    ((1920, 1080), (1920, 1080), "stretch"),
    # 16:9 캔버스 + 세로 소스 (letterbox / pillarbox)
    ((1920, 1080), (1080, 1920), "contain"),
    ((1920, 1080), (1080, 1920), "cover"),
    # 9:16 캔버스 (숏폼) + 가로 소스 — #343 회귀 빈발 영역
    ((1080, 1920), (1920, 1080), "contain"),
    ((1080, 1920), (1920, 1080), "cover"),
    ((1080, 1920), (1920, 1080), "stretch"),
    # 9:16 캔버스 + 세로 소스
    ((1080, 1920), (1080, 1920), "contain"),
    ((1080, 1920), (1080, 1920), "cover"),
    # 정사각 캔버스
    ((1080, 1080), (1920, 1080), "contain"),
    ((1080, 1080), (1920, 1080), "cover"),
    # 작은 소스 → 큰 캔버스 (업스케일)
    ((1920, 1080), (640, 360), "contain"),
    ((1920, 1080), (640, 360), "cover"),
]

# playback_rate 조합 — fit 과 독립 (시간축 분리 원칙).
_PLAYBACK_RATES: list[float] = [1.0, 1.5, 2.0]


def _make_fake_clip_scene(fit: str, playback_rate: float, source_w: int, source_h: int):
    """builder 입력용 더미 SceneCard / Project 구성.

    ``footage_size`` 는 builder 인자로 직접 주입하므로 ClipEffect 측 파일 경로는 임의.
    """
    from web.backend.models import SceneCard, ClipEffect, Project as ProjectModel

    proj = ProjectModel(project_id="p", name="t", output_width=1920, output_height=1080)
    scene = SceneCard(
        scene_id=1,
        title="t",
        description="t",
        narration_text="t",
        duration_hint_sec=5.0,
        footage_speed=playback_rate,
        footage=ClipEffect(type="clip", file="clip.mp4", fit=fit, rotation=0),
    )
    return scene, proj


def _compute_css_for_layer(layer_dict: dict, canvas_w: int, canvas_h: int) -> dict:
    """`applySimpleRenderTransform` 의 Python 미러 — CSS 백분율 결과 dict.

    *동일한 알고리즘* 을 TS / Python 양쪽에 두면 두 미러가 어긋날 위험이 있지만, 본 fixture
    는 *백엔드 builder geometry* 를 단일 진실로 삼고 CSS 결과는 그 결과를 통과시킨 derivation
    이므로 TS 와 Python 양쪽이 모두 같은 식을 만족해야 정상이다 (cross-verify).
    """
    pad = layer_dict.get("pad")
    crop = layer_dict.get("crop")
    if pad:
        return {
            "mode": "contain",
            "left_pct": (pad["x"] / canvas_w) * 100,
            "top_pct": (pad["y"] / canvas_h) * 100,
            "width_pct": (pad["w"] / canvas_w) * 100,
            "height_pct": (pad["h"] / canvas_h) * 100,
            "object_fit": "fill",
        }
    if crop:
        source_w = layer_dict["source_size"]["width"]
        source_h = layer_dict["source_size"]["height"]
        scale_x = layer_dict["scale_x"]
        scale_y = layer_dict["scale_y"]
        scaled_w = source_w * scale_x
        scaled_h = source_h * scale_y
        return {
            "mode": "cover",
            "left_pct": -(crop["x"] / canvas_w) * 100,
            "top_pct": -(crop["y"] / canvas_h) * 100,
            "width_pct": (scaled_w / canvas_w) * 100,
            "height_pct": (scaled_h / canvas_h) * 100,
            "object_fit": "fill",
        }
    return {
        "mode": "stretch",
        "left_pct": 0.0,
        "top_pct": 0.0,
        "width_pct": 100.0,
        "height_pct": 100.0,
        "object_fit": "fill",
    }


def _build_all_cases() -> list[dict]:
    from src.render_spec.models import Size as RsSize

    cases: list[dict] = []
    for canvas_wh, source_wh, fit in _GEOMETRY_CASES:
        cw, ch = canvas_wh
        sw, sh = source_wh
        for rate in _PLAYBACK_RATES:
            scene, proj = _make_fake_clip_scene(fit, rate, sw, sh)
            # 캔버스를 매트릭스 값으로 강제 (Project 의 output_w/h 덮어쓰기).
            proj.output_width = cw
            proj.output_height = ch
            spec = build_render_spec(
                scene,
                proj,
                footage_size=RsSize(width=sw, height=sh),
                footage_duration_sec=10.0,
                fps=30,
            )
            footage = spec.layers[0]
            assert isinstance(footage, VideoLayer), "fixture 케이스는 VideoLayer 만 지원"
            layer_dict = json.loads(footage.model_dump_json())
            expected_css = _compute_css_for_layer(layer_dict, cw, ch)
            cases.append(
                {
                    "name": f"canvas_{cw}x{ch}_source_{sw}x{sh}_{fit}_rate_{rate}",
                    "input": {
                        "canvas": {"width": cw, "height": ch, "fps": 30},
                        "fit": fit,
                        "playback_rate": rate,
                        "source_size": {"width": sw, "height": sh},
                    },
                    "layer": layer_dict,
                    "expected_css": expected_css,
                }
            )
    return cases


def test_simple_render_geometry_fixture_writes_and_matches():
    """매트릭스 케이스를 fixture 파일에 갱신하고, 동일 입력 두 번 호출 시 결과가 결정적임을 확인.

    fixture 가 변경됐다면 git diff 가 잡아서 PR 리뷰에 노출된다. 동시에 mjs 검증은 갱신된
    fixture 를 즉시 사용한다.
    """
    cases_a = _build_all_cases()
    cases_b = _build_all_cases()
    # 결정성 가드 — 같은 입력은 같은 fixture 를 만든다.
    assert cases_a == cases_b, "builder 결과가 비결정적입니다 (실행마다 달라짐)"
    assert len(cases_a) == len(_GEOMETRY_CASES) * len(_PLAYBACK_RATES)

    payload = {"version": 1, "cases": cases_a}
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    # 파일이 정상적으로 써졌는지 확인.
    loaded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert loaded["version"] == 1
    assert len(loaded["cases"]) == len(cases_a)
    for case in loaded["cases"]:
        assert case["expected_css"]["mode"] in ("contain", "cover", "stretch")
        assert "layer" in case and case["layer"]["type"] == "video"
