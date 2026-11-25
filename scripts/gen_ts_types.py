#!/usr/bin/env python3
"""백엔드 Pydantic 모델 → 프론트 TypeScript 타입 자동 생성 스크립트 (#304).

설계 의도:
- 백엔드 `web.backend.models`와 `src.render_spec.models`를 **단일 진실(SSOT)** 로 두고,
  프론트엔드 TS 타입은 그로부터 자동 생성한다. 이렇게 하면 필드명/타입 불일치로 인한
  미세 직렬화 버그(예: `fit_mode` vs `fit`)를 원천 차단한다.
- 도구는 `pydantic-to-typescript` (pydantic v2 호환) + `json-schema-to-typescript`
  (Node CLI) 조합. Node 의존성은 이미 `web/frontend`에 있으므로 추가 환경 부담 없음.

사용법:
    uv run python scripts/gen_ts_types.py
    # 또는 (frontend에서)
    npm run gen-types

출력:
    web/frontend/src/types/generated.ts

옵션:
    --check   생성 결과가 기존 파일과 다르면 비-0 종료 (CI용)

CLAUDE.md 노팀장 규약 준수: src/ 외부 자동화 스크립트는 scripts/ 하위에 둔다.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_MODULE = "scripts._pydantic_models_collector"
OUTPUT_PATH = PROJECT_ROOT / "web" / "frontend" / "src" / "types" / "generated.ts"
JSON2TS_LOCAL = PROJECT_ROOT / "web" / "frontend" / "node_modules" / ".bin" / "json2ts"


def _resolve_json2ts_cmd() -> str:
    """`json2ts` 실행 파일을 찾는다.

    우선순위:
    1. web/frontend/node_modules/.bin/json2ts (devDependency 설치 결과)
    2. PATH의 글로벌 json2ts
    """
    if JSON2TS_LOCAL.exists():
        return str(JSON2TS_LOCAL)
    global_cmd = shutil.which("json2ts")
    if global_cmd:
        return global_cmd
    raise FileNotFoundError(
        "json2ts 실행 파일을 찾을 수 없습니다. 다음 중 하나로 설치하세요:\n"
        "  cd web/frontend && npm install --save-dev json-schema-to-typescript\n"
        "  npm install -g json-schema-to-typescript"
    )


def _resolve_pydantic2ts_cmd() -> list[str]:
    """`pydantic2ts` 실행 명령어를 결정한다.

    - 가상환경 내 직접 설치돼 있으면 그 실행 파일
    - 그렇지 않으면 `uv run pydantic2ts`
    """
    direct = shutil.which("pydantic2ts")
    if direct:
        return [direct]
    if shutil.which("uv"):
        return ["uv", "run", "pydantic2ts"]
    raise FileNotFoundError(
        "pydantic2ts 실행 파일을 찾을 수 없습니다. 다음으로 설치하세요:\n"
        "  uv pip install pydantic-to-typescript\n"
        "또는 uv 자체를 설치한 뒤 'uv run' 경유로 호출하세요."
    )


def _build_header_note() -> str:
    return (
        "// ────────────────────────────────────────────────────────────────────────\n"
        "// 자동 생성 파일 (#304) — 직접 수정 금지\n"
        "// 원본: web/backend/models.py + src/render_spec/models.py (Pydantic v2)\n"
        "// 재생성: `npm run gen-types` 또는 `uv run python scripts/gen_ts_types.py`\n"
        "// ────────────────────────────────────────────────────────────────────────\n"
        "\n"
    )


def generate(check_only: bool = False) -> int:
    """Pydantic 모델 → TS 타입 변환을 실행한다.

    Returns:
        프로세스 종료 코드 (0=정상, 1=실패, 2=--check 모드에서 변경 감지)
    """
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    json2ts_cmd = _resolve_json2ts_cmd()
    pydantic2ts_cmd = _resolve_pydantic2ts_cmd()

    # PYTHONPATH에 프로젝트 루트를 넣어줘야 `web.backend.models` 같은 dotted path가 import된다.
    # (web/ 하위에는 __init__.py가 없어 PYTHONPATH 없이는 import 실패.)
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(PROJECT_ROOT)
    )

    with tempfile.NamedTemporaryFile(
        mode="w+",
        suffix=".ts",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        cmd = pydantic2ts_cmd + [
            "--module",
            COLLECTOR_MODULE,
            "--output",
            str(tmp_path),
            "--json2ts-cmd",
            json2ts_cmd,
        ]
        print(f"[gen_ts_types] 실행: {' '.join(cmd)}", file=sys.stderr)
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            print(
                "[gen_ts_types] pydantic2ts 실행 실패 — 로그를 확인하세요.",
                file=sys.stderr,
            )
            return 1
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        generated = tmp_path.read_text(encoding="utf-8")
        # 우리 측 헤더 노트를 pydantic2ts 자동 헤더 위에 한 줄 더 얹는다.
        final_content = _build_header_note() + generated

        if check_only:
            if not OUTPUT_PATH.exists():
                print(
                    f"[gen_ts_types] --check 실패: {OUTPUT_PATH} 가 존재하지 않습니다.",
                    file=sys.stderr,
                )
                return 2
            existing = OUTPUT_PATH.read_text(encoding="utf-8")
            if existing != final_content:
                print(
                    f"[gen_ts_types] --check 실패: {OUTPUT_PATH} 가 Pydantic 모델과 어긋납니다.\n"
                    f"  → `npm run gen-types` 를 실행해 갱신한 뒤 다시 커밋하세요.",
                    file=sys.stderr,
                )
                return 2
            print("[gen_ts_types] --check 통과: generated.ts 최신.", file=sys.stderr)
            return 0

        OUTPUT_PATH.write_text(final_content, encoding="utf-8")
        print(f"[gen_ts_types] 생성 완료: {OUTPUT_PATH}", file=sys.stderr)
        return 0
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="생성 결과가 기존 파일과 다르면 비-0 종료 (CI용)",
    )
    args = parser.parse_args(argv)
    return generate(check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
