"""Phase 7 — 자연어 후처리 편집기

자연어 편집 요청 → EditPlan 생성 → 사용자 승인 → 영향받는 Phase 재실행
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from .utils.file_cache import load_json, save_json
from .utils.gemini_client import generate_json

console = Console()

EDIT_PLAN_PATH = "cache/edit_plan.json"
HISTORY_DIR = "output/history"
HISTORY_MAX = 3

FFMPEG_FILTER_WHITELIST = {"color", "fade", "audio_volume", "brightness", "contrast", "saturation"}
FFMPEG_PARAM_RANGES = {
    "audio_volume": (0.0, 1.0),
    "fade_duration_sec": (0.1, 2.0),
    "brightness": (-1.0, 1.0),
    "contrast": (-1.0, 1.0),
    "saturation": (-1.0, 1.0),
}

EDIT_PLAN_PROMPT = """당신은 영상 편집 어시스턴트입니다.
아래 컨텍스트와 편집 요청을 읽고 EditPlan JSON을 생성하세요.

[현재 씬 계획]
{scene_plan}

[현재 클립 선택]
{clip_selection}

[현재 타임라인]
{timeline}

[편집 요청]
{request}

EditPlan 스키마:
{{
  "request_summary": string,      // 요청을 어떻게 해석했는지 한국어로 설명
  "edit_type": "A" | "B" | "AB",
  "patches": [                    // 유형 A: JSON 패치 목록
    {{
      "target_file": string,
      "operation": "update" | "delete" | "reorder",
      "scene_id": int,
      "field": string,
      "new_value": any
    }}
  ],
  "ffmpeg_params": [              // 유형 B: FFmpeg 파라미터
    {{
      "scope": "global" | "scene",
      "scene_id": int | null,
      "filter_type": string,
      "parameters": object,
      "description": string
    }}
  ],
  "affected_phases": [int],
  "estimated_time": string
}}

주관적 표현(예: "약간", "좀 더")은 구체적인 수치로 변환하고
request_summary에 "이렇게 해석했습니다. 수치는 X로 설정했습니다"라고 명시하세요.
순수 JSON만 반환하세요."""


def _validate_ffmpeg_params(ffmpeg_params: list) -> tuple[list, list]:
    """FFmpegPatch 화이트리스트 검증. (통과 목록, 거부 목록) 반환."""
    ok, rejected = [], []
    for p in ffmpeg_params:
        ft = p.get("filter_type", "")
        if ft not in FFMPEG_FILTER_WHITELIST:
            rejected.append((p, f"허용되지 않는 필터 유형: '{ft}'"))
            continue

        # 파라미터 값 범위 클리핑
        params = p.get("parameters", {})
        clipped = {}
        for k, v in params.items():
            key = f"{ft}_{k}" if f"{ft}_{k}" in FFMPEG_PARAM_RANGES else k
            if key in FFMPEG_PARAM_RANGES:
                lo, hi = FFMPEG_PARAM_RANGES[key]
                if not (lo <= float(v) <= hi):
                    clipped[k] = max(lo, min(hi, float(v)))
                    console.print(f"  [yellow]⚠️  {k} 값 {v}를 허용 범위 {lo}~{hi}로 조정했어요 → {clipped[k]}[/yellow]")
                else:
                    clipped[k] = v
            else:
                clipped[k] = v
        p["parameters"] = clipped
        ok.append(p)
    return ok, rejected


def _backup_files(affected_phases: list) -> str:
    """편집 전 영향받는 파일 백업. 백업 디렉토리 경로 반환."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(HISTORY_DIR) / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    phase_file_map = {
        3: ["cache/clip_selection.json"],
        4: ["output/muted_output.mp4", "cache/timeline.json"],
        5: ["output/narration/narration_timeline.json", "output/final_output.sbv"],
        6: ["output/final_output.mp4"],
    }
    for phase in affected_phases:
        for f in phase_file_map.get(phase, []):
            src = Path(f)
            if src.exists():
                shutil.copy2(src, backup_dir / src.name)

    # 오래된 백업 정리 (최근 3회만 유지)
    history = sorted(Path(HISTORY_DIR).iterdir())
    for old in history[:-HISTORY_MAX]:
        shutil.rmtree(old, ignore_errors=True)

    return str(backup_dir)


def _apply_patches(patches: list) -> None:
    """EditPatch 목록을 JSON 파일에 적용.

    파일은 실제로 로드한 경로에 그대로 저장한다.
    기술설계 문서 기준 저장 경로:
      - scene_plan.json, clip_selection.json, timeline.json → cache/{target}
      - narration_timeline.json → output/narration/narration_timeline.json
      - 그 외는 target 값을 절대경로로 해석
    """
    # 파일명 → 실제 저장 경로 매핑 (기술설계 문서 기준)
    _KNOWN_PATHS: dict[str, str] = {
        "narration_timeline.json": "output/narration/narration_timeline.json",
        "final_output.sbv": "output/final_output.sbv",
        "final_output.mp4": "output/final_output.mp4",
    }

    for patch in patches:
        target = patch["target_file"]
        # 실제 파일 경로 결정: 알려진 경로 우선, 그 다음 cache/, output/, 절대경로 순 탐색
        resolved_path: str | None = None
        for candidate in [
            _KNOWN_PATHS.get(target),
            f"cache/{target}",
            f"output/{target}",
            target,
        ]:
            if candidate and Path(candidate).exists():
                resolved_path = candidate
                break

        data = load_json(resolved_path) if resolved_path else None
        if data is None:
            console.print(f"  [yellow]⚠️  {target} 파일을 찾을 수 없어요. 건너뜁니다.[/yellow]")
            continue

        scenes = data.get("scenes", data) if isinstance(data, dict) else data
        op = patch["operation"]
        sid = patch.get("scene_id")
        field = patch.get("field")
        new_val = patch.get("new_value")

        if op == "delete":
            scenes = [s for s in scenes if s.get("scene_id") != sid]
            # scene_id 재번호 부여
            for i, s in enumerate(scenes):
                s["scene_id"] = i + 1

        elif op == "update":
            for s in scenes:
                if s.get("scene_id") == sid:
                    s[field] = new_val

        elif op == "reorder":
            # new_value = 이동할 목표 위치(1-based)
            idx_from = next((i for i, s in enumerate(scenes) if s.get("scene_id") == sid), None)
            if idx_from is not None and new_val is not None:
                item = scenes.pop(idx_from)
                scenes.insert(int(new_val) - 1, item)
                for i, s in enumerate(scenes):
                    s["scene_id"] = i + 1

        if isinstance(data, dict):
            data["scenes"] = scenes
        else:
            data = scenes

        # 실제 로드한 경로 그대로 저장 (output/final_output.mp4 등 잘못된 경로 방지)
        save_json(resolved_path, data)


def _rerun_phases(affected_phases: list) -> None:
    """영향받는 Phase를 순서대로 재실행."""
    from . import clip_matcher, clip_editor, narration_generator, sbv_generator, final_composer
    project_root = str(Path(__file__).parent.parent)

    phase_runners = {
        3: lambda: clip_matcher.print_summary(clip_matcher.run(force=True), project_root),
        4: lambda: clip_editor.print_summary(clip_editor.run(force=True), project_root),
        5: lambda: (
            narration_generator.print_summary(narration_generator.run(force=True), project_root),
            sbv_generator.print_summary(sbv_generator.run(force=True), project_root),
        ),
        6: lambda: final_composer.print_summary(final_composer.run(force=True), project_root),
    }

    for phase in sorted(set(affected_phases)):
        runner = phase_runners.get(phase)
        if runner:
            runner()


def run_interactive(history: list | None = None) -> None:
    """대화형 편집 루프 실행."""
    if history is None:
        history = []

    console.print()
    console.print(Panel(
        "[bold]편집 모드[/bold] — 자연어로 수정 요청을 입력하세요\n"
        "[dim]예) '3번 씬을 삭제해줘' / '씬 사이에 페이드 트랜지션 넣어줘'[/dim]",
        border_style="cyan",
    ))

    while True:
        request = Prompt.ask("\n어떻게 수정할까요?").strip()
        if request.lower() in ("q", "quit", "exit", "종료"):
            console.print("[dim]편집 모드를 종료합니다.[/dim]")
            break
        if not request:
            continue

        # 이전 대화 맥락 포함
        history.append({"role": "user", "content": request})

        scene_plan = json.dumps(load_json("cache/scene_plan.json") or {}, ensure_ascii=False)
        clip_sel = json.dumps(load_json("cache/clip_selection.json") or {}, ensure_ascii=False)
        timeline = json.dumps(load_json("cache/timeline.json") or {}, ensure_ascii=False)

        full_request = "\n".join(f"[{h['role']}] {h['content']}" for h in history)

        console.print("\n  잠깐만요, 요청을 분석하고 있어요...")
        raw = generate_json(
            EDIT_PLAN_PROMPT.format(
                scene_plan=scene_plan[:3000],
                clip_selection=clip_sel[:2000],
                timeline=timeline[:1000],
                request=full_request,
            )
        )
        plan = json.loads(raw)
        save_json(EDIT_PLAN_PATH, plan)

        # FFmpegPatch 검증
        if plan.get("ffmpeg_params"):
            ok, rejected = _validate_ffmpeg_params(plan["ffmpeg_params"])
            if rejected:
                for p, reason in rejected:
                    console.print(f"  [red]❌ 거부된 효과: {reason}[/red]")
            plan["ffmpeg_params"] = ok

        # EditPlan 출력
        console.print()
        console.print(Panel(
            f"[bold]편집 계획서[/bold]\n\n"
            f"요청 해석: {plan.get('request_summary', '')}\n"
            f"편집 유형: {plan.get('edit_type', '')}\n"
            f"영향 단계: Phase {plan.get('affected_phases', [])}\n"
            f"예상 시간: {plan.get('estimated_time', '알 수 없음')}",
            border_style="yellow",
        ))

        if plan.get("patches"):
            console.print("\n  [bold]변경 내용 (구조):[/bold]")
            for p in plan["patches"]:
                console.print(f"    • [{p['operation']}] 씬 {p.get('scene_id')} / {p.get('field', '')} → {p.get('new_value', '')}")

        if plan.get("ffmpeg_params"):
            console.print("\n  [bold]변경 내용 (효과):[/bold]")
            for p in plan["ffmpeg_params"]:
                console.print(f"    • {p['description']}")

        if not Confirm.ask("\n이대로 진행할까요?"):
            console.print("  취소했어요. 다시 입력하거나 q로 종료하세요.")
            history.pop()
            continue

        _backup_files(plan.get("affected_phases", []))

        if plan.get("patches"):
            _apply_patches(plan["patches"])

        _rerun_phases(plan.get("affected_phases", []))

        history.append({"role": "assistant", "content": plan.get("request_summary", "")})
        console.print("\n[bold green]✅ 수정 완료![/bold green]")


if __name__ == "__main__":
    run_interactive()
