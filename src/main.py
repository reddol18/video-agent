"""CLI 진입점 + 파이프라인 오케스트레이터"""

import argparse
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.prompt import Confirm

load_dotenv()

console = Console()
PROJECT_ROOT = str(Path(__file__).parent.parent)


def _editor_link(rel_path: str) -> str:
    abs_path = str(Path(PROJECT_ROOT) / rel_path)
    return f"[link=cursor://file/{abs_path}]cursor://file/{abs_path}[/link]"


def _check_env() -> bool:
    """필수 환경변수 확인."""
    import os
    missing = []
    if not os.getenv("GOOGLE_API_KEY"):
        missing.append("GOOGLE_API_KEY (Gemini API 키)")
    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        missing.append("GOOGLE_APPLICATION_CREDENTIALS (Google Cloud TTS 키 경로)")

    if missing:
        console.print("\n[red]❌ 환경 설정이 빠져 있어요[/red]\n")
        for m in missing:
            console.print(f"  • {m}")
        console.print(f"\n  설정 방법: 📄 → {_editor_link('.env.example')}")
        console.print("  .env.example을 복사해서 .env 파일로 만들고 키를 입력해주세요.")
        return False
    return True


def _rollback() -> None:
    """직전 백업으로 롤백."""
    history_dir = Path(PROJECT_ROOT) / "output" / "history"
    backups = sorted(history_dir.iterdir()) if history_dir.exists() else []
    if not backups:
        console.print("[yellow]되돌릴 수 있는 이전 상태가 없어요.[/yellow]")
        return

    latest = backups[-1]
    console.print(f"  직전 상태로 되돌릴게요: [dim]{latest.name}[/dim]")
    if not Confirm.ask("계속할까요?"):
        return

    file_map = {
        "clip_selection.json": "cache/clip_selection.json",
        "timeline.json": "cache/timeline.json",
        "narration_timeline.json": "output/narration/narration_timeline.json",
        "final_output.sbv": "output/final_output.sbv",
        "muted_output.mp4": "output/muted_output.mp4",
        "final_output.mp4": "output/final_output.mp4",
    }
    for fname, dst in file_map.items():
        src = latest / fname
        if src.exists():
            shutil.copy2(src, Path(PROJECT_ROOT) / dst)
            console.print(f"  [green]✓[/green] {fname} 복원 완료")

    console.print("\n[bold green]✅ 롤백 완료![/bold green]")


def _preview(scene_id: int) -> None:
    """특정 씬 클립 미리보기."""
    from .utils.file_cache import load_json
    from .utils.ffmpeg_utils import preview_clip

    sel_data = load_json("cache/clip_selection.json")
    if not sel_data:
        console.print("[red]❌ clip_selection.json 이 없어요. Phase 3을 먼저 실행해주세요.[/red]")
        return

    sel = next((s for s in sel_data["selections"] if s["scene_id"] == scene_id), None)
    if not sel:
        console.print(f"[red]❌ 씬 {scene_id}를 찾을 수 없어요.[/red]")
        return

    console.print(f"\n  🎬 씬 {scene_id} 미리보기 ({Path(sel['source_file']).name} [{sel['trim_start']:.1f}s ~ {sel['trim_end']:.1f}s])")
    console.print("  잠깐만요, 해당 구간을 잘라내고 있어요...")
    tmp = preview_clip(sel["source_file"], sel["trim_start"], sel["trim_end"])
    console.print(f"  ▶ 재생 중: [dim]{tmp}[/dim]")


def _run_shorts(force: bool = False, ai_enhance: bool = False) -> None:
    """숏폼 자동 추출 실행."""
    from . import shorts_generator

    if not _check_env():
        sys.exit(1)

    try:
        meta = shorts_generator.run(force=force, ai_enhance=ai_enhance)
        shorts_generator.print_summary(meta, PROJECT_ROOT)
    except RuntimeError as e:
        console.print(f"\n[red]❌ {e}[/red]")
        sys.exit(1)


def _run_pipeline(scenario: str, footage: str, force_phase: int | None, yes: bool = False) -> None:
    """전체 파이프라인 순서대로 실행."""
    from . import (
        scenario_parser,
        footage_indexer,
        clip_matcher,
        clip_editor,
        narration_generator,
        sbv_generator,
        final_composer,
    )
    from .utils.file_cache import load_json, save_json

    state_path = "cache/pipeline_state.json"

    def update_state(**kwargs):
        state = load_json(state_path) or {}
        from datetime import datetime
        state.update(kwargs)
        state["last_updated"] = datetime.now().isoformat()
        save_json(state_path, state)

    # ── Phase 1 ─────────────────────────────────────────────
    force1 = force_phase is not None and force_phase <= 1
    scenes = scenario_parser.run(scenario, force=force1)
    scenario_parser.print_summary(scenes, PROJECT_ROOT)

    if not yes and not Confirm.ask("계속 진행할까요?"):
        console.print("[dim]중단했어요.[/dim]")
        return
    update_state(phase1_done=True)

    # ── Phase 2 ─────────────────────────────────────────────
    force2 = force_phase is not None and force_phase <= 2

    # (#edit)/(#sync) 씬은 Gemini가 영상을 직접 분석하므로 청크 분석 불필요
    # kenburns/clip/multiscreen/title_gen 씬도 청크 불필요
    NO_CHUNK_TYPES = {"kenburns", "kenburns_copy_img", "clip", "multiscreen", "title_gen", "edit", "sync"}

    def _needs_chunk(scene: dict) -> bool:
        footage = scene.get("footage")
        if isinstance(footage, dict):
            return footage.get("type") not in NO_CHUNK_TYPES
        if isinstance(footage, list):
            return False  # multiscreen 직접 지정
        return True  # 문자열·None: 일반 footage 매칭 필요

    # 청크 분석이 필요한 씬이 쓰는 footage 파일명 집합
    chunk_needed_files = {
        scene["footage"] for scene in scenes
        if _needs_chunk(scene) and isinstance(scene.get("footage"), str)
    }

    if not chunk_needed_files:
        console.print("[dim]  Phase 2: 모든 씬이 edit/effect 타입 — footage 청크 분석 스킵[/dim]")
        chunks = []
        save_json("cache/footage_index.json", {"chunks": []})
    else:
        chunks = footage_indexer.run(footage, force=force2, include_files=chunk_needed_files)
        footage_indexer.print_summary(chunks, PROJECT_ROOT)
    update_state(phase2_done=True)

    # ── Phase 4 (TTS 먼저) ───────────────────────────────────
    # 설계 의도: TTS 음성 길이를 먼저 확정한 뒤 클립 매칭(Phase 3)에서 tts_dur 활용
    # --force-phase 4 → Phase 4를 재실행하고 Phase 3/5도 반드시 재실행
    force4 = force_phase is not None and force_phase <= 4

    # Phase 4 재실행 전 기존 TTS 길이 스냅샷 (Phase 3 재실행 여부 판단용)
    import os as _os
    clip_sel_path = "cache/clip_selection.json"
    _prev_tts_durs: dict[int, float] = {}
    if force4:
        _prev_narr = load_json("output/narration/narration_timeline.json")
        if _prev_narr:
            _prev_tts_durs = {e["scene_id"]: e["duration_sec"] for e in _prev_narr.get("entries", [])}

    entries = narration_generator.run(force=force4)
    narration_generator.print_summary(entries, PROJECT_ROOT)
    update_state(phase4_done=True)

    # TTS 길이가 실제로 바뀐 씬이 있을 때만 clip_selection.json 무효화
    # → 보이스/대본만 바꾸고 길이 변화가 없으면 Phase 3 Gemini 재호출 생략
    _tts_dur_changed = False
    if force4 and _prev_tts_durs:
        for e in entries:
            prev = _prev_tts_durs.get(e["scene_id"])
            if prev is None or abs(prev - e["duration_sec"]) > 0.1:
                _tts_dur_changed = True
                break
    elif force4:
        _tts_dur_changed = True  # 이전 캐시 없으면 변경으로 간주

    # (#edit)/(#sync) 전용 씬만 있으면 TTS 길이가 바뀌어도 Gemini 재호출 불필요
    # → clip_selection.json의 tts_duration_sec만 갱신하고 Phase 3 스킵
    _all_edit_sync = all(
        isinstance(s.get("footage"), dict) and s["footage"].get("type") in ("edit", "sync")
        for s in scenes
    )

    if force4 and _tts_dur_changed:
        _tts_dur_map = {e["scene_id"]: e["duration_sec"] for e in entries}
        _sel_data = load_json(clip_sel_path)
        if _all_edit_sync and _sel_data:
            # clip_selection.json의 tts_duration_sec만 갱신 — Gemini 재호출 없음
            for sel in _sel_data.get("selections", []):
                new_dur = _tts_dur_map.get(sel["scene_id"])
                if new_dur is not None:
                    sel["tts_duration_sec"] = round(new_dur, 3)
            from .utils.file_cache import save_json as _save_json
            _save_json(clip_sel_path, _sel_data)
            console.print("[dim]  Phase 4 재실행: (#edit)/(#sync) 전용 — tts_duration_sec 갱신, Phase 3 스킵[/dim]")
            _tts_dur_changed = False  # Phase 3 재실행 억제
        else:
            if Path(clip_sel_path).exists():
                _os.remove(clip_sel_path)
            console.print("[dim]  Phase 4 재실행: TTS 길이 변경 감지 — clip_selection.json 캐시 무효화[/dim]")
    elif force4:
        console.print("[dim]  Phase 4 재실행: TTS 길이 변화 없음 — clip_selection.json 캐시 유지[/dim]")

    # ── Phase 3 ─────────────────────────────────────────────
    force3 = force_phase is not None and force_phase <= 3 or (force4 and _tts_dur_changed)
    selections = clip_matcher.run(force=force3)
    clip_matcher.print_summary(selections, PROJECT_ROOT)

    if not yes and not Confirm.ask("클립 매칭 결과를 확인했나요? 계속 진행할까요?"):
        console.print("[dim]중단했어요. 편집 모드(-\\-edit)에서 클립을 교체할 수 있어요.[/dim]")
        return
    update_state(phase3_done=True)

    # ── Phase 5 (영상 편집) ──────────────────────────────────
    # --force-phase 5 → Phase 5만 강제 재실행 (Phase 4는 이미 완료된 상태여야 함)
    # --force-phase 4 → Phase 4 재실행 후 Phase 5도 반드시 재실행 (타임라인 갱신 필요)
    force5 = force_phase is not None and (force_phase == 5 or force_phase <= 5 or force4)
    timestamps = clip_editor.run(force=force5)
    clip_editor.print_summary(timestamps, PROJECT_ROOT)
    update_state(phase5_done=True)

    # ── SBV 자막 생성 (Phase 5 이후) ─────────────────────────
    # Phase 5의 _update_narration_timing이 start_sec/end_sec를 갱신한 후 SBV를 생성해야
    # 타임코드가 0이 아닌 실제 영상 타임라인 기반으로 계산됨
    sbv_path = sbv_generator.run(force=force4 or force5)
    sbv_generator.print_summary(sbv_path, PROJECT_ROOT)

    # ── Phase 6 ─────────────────────────────────────────────
    force6 = force_phase is not None and force_phase <= 6
    output = final_composer.run(force=force6)
    final_composer.print_summary(output, PROJECT_ROOT)
    update_state(phase6_done=True)

    # ── FCPXML 내보내기 (Phase 6 이후 자동 생성) ─────────────────
    try:
        from . import fcpxml_generator
        fcp_path = fcpxml_generator.run(force=force6)
        if fcp_path:
            fcpxml_generator.print_summary(fcp_path, PROJECT_ROOT)
    except Exception as fcp_err:
        console.print(f"[yellow]⚠️  FCPXML 생성 중 오류가 발생했어요 (건너뜀): {fcp_err}[/yellow]")

    console.print("\n[bold green]🎉 파이프라인 완료![/bold green]")
    console.print("  수정이 필요하면 [bold]--edit[/bold] 옵션으로 편집 모드를 실행하세요.")
    console.print(f"  python src/main.py --edit")


def main():
    parser = argparse.ArgumentParser(
        description="영상 자동 편집 파이프라인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python -m src.main --scenario scenario.txt --footage footage/
  python -m src.main --force-phase 3
  python -m src.main --edit
  python -m src.main --preview 2
  python -m src.main --rollback
  python -m src.main --shorts
  python -m src.main --shorts --shorts-force
  python -m src.main --shorts-banner
  python -m src.main --shorts-banner output/shorts/shorts_01.mp4 output/shorts/shorts_02.mp4
  python -m src.main --export-fcp
  python -m src.main --export-fcp-force
        """,
    )
    parser.add_argument("--scenario", help="시나리오 텍스트 파일 경로 (.txt)")
    parser.add_argument("--footage", help="재료 영상 폴더 경로")
    parser.add_argument("--force-phase", type=int, metavar="N", help="N번 Phase부터 강제 재실행")
    parser.add_argument("--edit", action="store_true", help="자연어 후처리 편집 모드 실행")
    parser.add_argument("--preview", type=int, metavar="SCENE_ID", help="특정 씬 클립 미리보기")
    parser.add_argument("--rollback", action="store_true", help="직전 상태로 롤백")
    parser.add_argument("--yes", "-y", action="store_true", help="중간 확인 프롬프트 자동 승인")
    parser.add_argument("--shorts", action="store_true", help="텍스트 기반 숏폼(Shorts) 자동 추출")
    parser.add_argument("--shorts-force", action="store_true", help="숏폼 캐시 무시하고 재생성")
    parser.add_argument(
        "--shorts-banner",
        nargs="*",
        metavar="FILE",
        help="기존 숏폼 파일에 관련 영상 안내 배너만 추가. 파일 미지정 시 output/shorts/*.mp4 전체 처리",
    )
    parser.add_argument(
        "--enhance",
        action="store_true",
        help="숏폼 AI 업스케일링 활성화 (realesrgan-ncnn-vulkan 바이너리 필요)",
    )
    parser.add_argument(
        "--export-fcp",
        action="store_true",
        help="기존 파이프라인 산출물로 FCPXML 타임라인 파일을 생성 (output/final_output.fcpxml)",
    )
    parser.add_argument(
        "--export-fcp-force",
        action="store_true",
        help="FCPXML 파일이 이미 있어도 강제로 재생성",
    )

    args = parser.parse_args()

    if args.rollback:
        _rollback()
        return

    if args.export_fcp or args.export_fcp_force:
        from . import fcpxml_generator
        try:
            fcp_path = fcpxml_generator.run(force=args.export_fcp_force)
            if fcp_path:
                fcpxml_generator.print_summary(fcp_path, PROJECT_ROOT)
        except RuntimeError as e:
            console.print(f"\n[red]❌ {e}[/red]")
            sys.exit(1)
        return

    if args.shorts_banner is not None:
        from . import shorts_generator
        files = args.shorts_banner if args.shorts_banner else None
        shorts_generator.apply_banner(files)
        return

    if args.shorts:
        _run_shorts(force=args.shorts_force, ai_enhance=args.enhance)
        return

    if args.preview is not None:
        _preview(args.preview)
        return

    if args.edit:
        if not _check_env():
            sys.exit(1)
        from .post_editor import run_interactive
        run_interactive()
        return

    # 전체 파이프라인
    if not args.scenario or not args.footage:
        parser.print_help()
        console.print("\n[yellow]--scenario 와 --footage 옵션이 필요해요.[/yellow]")
        sys.exit(1)

    if not _check_env():
        sys.exit(1)

    if not Path(args.scenario).exists():
        console.print(f"[red]❌ 시나리오 파일을 찾을 수 없어요: {args.scenario}[/red]")
        sys.exit(1)

    if not Path(args.footage).is_dir():
        console.print(f"[red]❌ footage 폴더를 찾을 수 없어요: {args.footage}[/red]")
        sys.exit(1)

    _run_pipeline(args.scenario, args.footage, args.force_phase, yes=args.yes)


if __name__ == "__main__":
    main()
