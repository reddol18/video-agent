"""골든 테스트용 mock 푸티지 생성 스크립트 (#305).

이 스크립트는 `tests/golden/fixtures/` 에 두 개의 작은 테스트용 파일을 만든다.

생성물:
- `clip_5s.mp4`  — 5초, 1024×576, 30fps, H.264. ffmpeg `testsrc2` 패턴 + 타임코드 텍스트.
  컬러 그라데이션 + 매 프레임 식별 가능한 패턴이라 크롭/스케일/회전 결과를 픽셀 단위로
  비교하기 좋다. 무음 트랙.
- `image_landscape.png`  — 1024×576, 식별 가능한 4분면 색상 + 중앙 십자선.

설계 원칙:
1. **멱등성** — 이미 파일이 있으면 건너뛴다. CI/로컬 어디서 호출해도 같은 결과.
2. **작은 크기** — 1024×576 / 5초 / CRF 28 → 수백 KB. lfs 없이 커밋 가능.
3. **결정론적 출력** — testsrc2 패턴은 시드 없이도 항상 같은 픽셀을 만든다.
4. **외부 의존 없음** — ffmpeg 바이너리만 있으면 동작. PIL은 PNG 생성에만 사용.

사용법:
    python tests/golden/_generate_fixtures.py            # 누락된 것만 생성
    python tests/golden/_generate_fixtures.py --force    # 강제 재생성

pytest 안에서는 `conftest.py` 의 `ensure_fixtures` 픽스처가 자동으로 호출한다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw


FIXTURES_DIR = Path(__file__).parent / "fixtures"
CLIP_PATH = FIXTURES_DIR / "clip_5s.mp4"
IMAGE_PATH = FIXTURES_DIR / "image_landscape.png"

# 모든 픽스처가 같은 해상도를 쓰면 fit 결과 비교가 단순해진다.
FIXTURE_W = 1024
FIXTURE_H = 576
CLIP_DURATION_SEC = 5
CLIP_FPS = 30


def generate_clip(dst: Path, *, force: bool = False) -> bool:
    """5초 × 1024×576 mp4 생성. 이미 있으면 건너뜀.

    반환: 새로 생성했으면 True, 건너뛰었으면 False.
    """
    if dst.exists() and not force:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    # testsrc2 = 컬러 그라데이션 + 시계 + 카운터. 결정론적 픽셀 출력.
    # drawtext 로 "GOLDEN" 워터마크를 추가해 좌상단/우하단 변별력을 높인다.
    vf = (
        f"testsrc2=size={FIXTURE_W}x{FIXTURE_H}:rate={CLIP_FPS}:duration={CLIP_DURATION_SEC}"
        ",drawbox=x=0:y=0:w=120:h=80:color=red@0.8:t=fill"
        ",drawbox=x=iw-120:y=ih-80:w=120:h=80:color=blue@0.8:t=fill"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "28", "-preset", "veryfast",
        "-an",
        "-t", str(CLIP_DURATION_SEC),
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"테스트용 clip 생성 실패: {result.stderr}\n명령: {' '.join(cmd)}"
        )
    return True


def generate_image(dst: Path, *, force: bool = False) -> bool:
    """1024×576 PNG 생성. 4분면 색상 + 중앙 십자선.

    fit/crop이 가운데 정렬이라 4분면 색상을 보면 어느 방향으로 잘렸는지가 한눈에 보인다.
    """
    if dst.exists() and not force:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (FIXTURE_W, FIXTURE_H), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    half_w = FIXTURE_W // 2
    half_h = FIXTURE_H // 2
    # 좌상=빨강, 우상=초록, 좌하=파랑, 우하=노랑
    draw.rectangle((0, 0, half_w, half_h), fill=(220, 60, 60))
    draw.rectangle((half_w, 0, FIXTURE_W, half_h), fill=(60, 200, 80))
    draw.rectangle((0, half_h, half_w, FIXTURE_H), fill=(60, 100, 220))
    draw.rectangle((half_w, half_h, FIXTURE_W, FIXTURE_H), fill=(230, 220, 60))
    # 중앙 십자선 (흰색) — 가운데 정렬 여부 확인용
    draw.line((half_w, 0, half_w, FIXTURE_H), fill=(255, 255, 255), width=4)
    draw.line((0, half_h, FIXTURE_W, half_h), fill=(255, 255, 255), width=4)
    # 모서리 텍스트로 회전/뒤집힘 검출
    draw.text((20, 20), "TL", fill=(255, 255, 255))
    draw.text((FIXTURE_W - 40, 20), "TR", fill=(255, 255, 255))
    draw.text((20, FIXTURE_H - 20), "BL", fill=(255, 255, 255))
    draw.text((FIXTURE_W - 40, FIXTURE_H - 20), "BR", fill=(255, 255, 255))
    img.save(dst, format="PNG", optimize=True)
    return True


def ensure_all(force: bool = False) -> dict[str, bool]:
    """모든 픽스처가 존재함을 보장한다. 반환: {파일명: 새로_생성됐는지}."""
    return {
        CLIP_PATH.name: generate_clip(CLIP_PATH, force=force),
        IMAGE_PATH.name: generate_image(IMAGE_PATH, force=force),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="골든 테스트 픽스처 생성")
    parser.add_argument("--force", action="store_true", help="기존 파일을 덮어쓴다")
    args = parser.parse_args()
    created = ensure_all(force=args.force)
    for name, was_new in created.items():
        print(f"{'생성' if was_new else '유지'}: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
