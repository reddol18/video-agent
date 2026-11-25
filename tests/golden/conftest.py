"""골든 테스트용 pytest 설정 (#305, #320).

- `--update-golden` 커맨드라인 옵션 등록. 활성화하면 기대 PNG를 *덮어쓴다*.
- 픽스처 영상/이미지가 누락되어 있으면 세션 시작 시 자동 생성한다(`_generate_fixtures`).
- 비교 실패 시 diff 이미지를 저장할 폴더(`_failures/`)를 비워서 시작한다.
- CI 환경(`CI=true`)에서는 expected PNG 자동 시드를 차단한다(#320).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.golden._generate_fixtures import (
    CLIP_PATH,
    FIXTURES_DIR,
    IMAGE_PATH,
    ensure_all,
)


GOLDEN_DIR = Path(__file__).parent
EXPECTED_DIR = GOLDEN_DIR / "expected"
FAILURES_DIR = GOLDEN_DIR / "_failures"


def is_ci_environment() -> bool:
    """CI 환경 여부 판정 (#320).

    GitHub Actions, GitLab CI, CircleCI 등 대부분의 CI 시스템은 `CI=true` 환경변수를
    기본으로 설정한다. 이 함수는 그 변수가 truthy 값(`true`/`1`)인지 확인한다.

    CI 환경에서는 `test_render_parity.py` 가 expected PNG 자동 시드를 거부하고
    명확한 에러로 실패한다. 자동 시드는 회귀 가드의 핵심 가치를 우회할 수 있어
    로컬 개발 환경에서만 허용한다.
    """
    value = os.environ.get("CI", "").strip().lower()
    return value in ("true", "1", "yes", "on")


def pytest_addoption(parser: pytest.Parser) -> None:
    """`--update-golden` 옵션 등록.

    이 옵션이 있으면 골든 비교 대신 *기대 PNG를 덮어쓴다*. 의도된 렌더 변경 후 골든
    이미지를 갱신할 때만 사용한다 — PR 리뷰어가 시각적으로 확인해야 한다.
    """
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="골든 PNG 를 새로 생성/덮어쓴다. 의도된 렌더 변경 후에만 사용.",
    )


@pytest.fixture(scope="session")
def update_golden(request: pytest.FixtureRequest) -> bool:
    """`--update-golden` 플래그 값."""
    return bool(request.config.getoption("--update-golden"))


@pytest.fixture(scope="session", autouse=True)
def _ensure_fixtures_and_dirs() -> None:
    """세션 시작 시: 픽스처 영상이 없으면 만든다 + `_failures/` 비움 + `expected/` 보장."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
    # 실패 이미지는 매 실행마다 새로 모음
    if FAILURES_DIR.exists():
        shutil.rmtree(FAILURES_DIR)
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    ensure_all(force=False)
    # 픽스처가 실제로 생성되었는지 사후 검증
    assert CLIP_PATH.exists(), f"clip 픽스처 누락: {CLIP_PATH}"
    assert IMAGE_PATH.exists(), f"image 픽스처 누락: {IMAGE_PATH}"
