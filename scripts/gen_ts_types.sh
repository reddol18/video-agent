#!/usr/bin/env bash
# Pydantic 모델 → TypeScript 타입 자동 생성 (#304).
#
# 본 스크립트는 scripts/gen_ts_types.py 를 호출하는 얇은 wrapper다.
# 실제 변환 로직과 옵션 처리는 모두 Python 측에 있다.
#
# 사용법:
#   ./scripts/gen_ts_types.sh
#   ./scripts/gen_ts_types.sh --check       # 결과가 기존 파일과 다르면 비-0 종료
#
# uv가 설치돼 있으면 `uv run python ...` 으로, 아니면 시스템 python3로 실행한다.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

if command -v uv >/dev/null 2>&1; then
  exec uv run python "${SCRIPT_DIR}/gen_ts_types.py" "$@"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "${SCRIPT_DIR}/gen_ts_types.py" "$@"
fi

echo "[gen_ts_types.sh] uv 또는 python3 가 PATH에 없습니다." >&2
exit 1
