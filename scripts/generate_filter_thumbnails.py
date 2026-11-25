"""필터 프리셋 썸네일 생성 스크립트 (#227)

5종 필터(flashback/horror/vivid/nightvision/pov)에 대해 testsrc 1초 영상을 만들어
각 필터를 적용 후 1프레임(160x90 PNG)을 추출하여
`web/frontend/public/filter_previews/{filter_type}.png`에 저장한다.

빌드/배포 시 한 번만 실행. 결과 PNG는 git에 커밋한다.

실행: `uv run python scripts/generate_filter_thumbnails.py` 또는
      `python scripts/generate_filter_thumbnails.py`
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# clip_editor의 필터 빌더를 재사용
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.clip_editor import _build_filter_vf, _render_pov_mask_png  # noqa: E402

OUTPUT_DIR = ROOT / "web" / "frontend" / "public" / "filter_previews"
THUMB_W = 160
THUMB_H = 90
SRC_W = 640
SRC_H = 360
DURATION_SEC = 1
INTENSITY = 0.8  # 시각적으로 변화를 잘 보여주는 강도

FILTERS = ["flashback", "horror", "vivid", "nightvision", "pov"]


def _generate_testsrc(out_path: Path) -> bool:
    """testsrc2 1초 영상을 생성한다. 외부 다운로드 없이 ffmpeg 내장 소스 사용."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"testsrc2=size={SRC_W}x{SRC_H}:rate=30:duration={DURATION_SEC}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"[ERROR] testsrc 생성 실패: {r.stderr.decode(errors='ignore')[:300]}")
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def _apply_vf_filter(src_path: Path, dst_path: Path, vf_chain: str) -> bool:
    cmd = [
        "ffmpeg", "-y", "-i", str(src_path),
        "-vf", vf_chain,
        "-an",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        str(dst_path),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"[ERROR] vf 필터 적용 실패: {r.stderr.decode(errors='ignore')[:300]}")
        return False
    return dst_path.exists() and dst_path.stat().st_size > 0


def _apply_pov_filter(src_path: Path, dst_path: Path, tmp_dir: str) -> bool:
    """POV 마스크를 PNG로 만든 뒤 ffmpeg overlay로 합성한다.

    스크립트 단순화를 위해 프레임별 alpha_composite 대신 ffmpeg overlay 1패스로 처리.
    """
    mask_path = _render_pov_mask_png(
        SRC_W, SRC_H,
        {"center_x": 0.5, "center_y": 0.5, "radius": 0.35, "feather": 60, "darkness": 1.0},
        INTENSITY,
        tmp_dir,
    )
    if not mask_path:
        print("[ERROR] POV mask 생성 실패")
        return False

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_path),
        "-i", mask_path,
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto",
        "-an",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        str(dst_path),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"[ERROR] POV 합성 실패: {r.stderr.decode(errors='ignore')[:300]}")
        return False
    return dst_path.exists() and dst_path.stat().st_size > 0


def _extract_thumbnail(src_path: Path, dst_path: Path) -> bool:
    """src 영상의 0.5초 지점에서 1프레임 추출 → 160x90 PNG로 저장."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", "0.5",
        "-i", str(src_path),
        "-frames:v", "1",
        "-vf", f"scale={THUMB_W}:{THUMB_H}:flags=lanczos",
        str(dst_path),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"[ERROR] 썸네일 추출 실패: {r.stderr.decode(errors='ignore')[:300]}")
        return False
    return dst_path.exists() and dst_path.stat().st_size > 0


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("[ERROR] ffmpeg가 설치되지 않았습니다.")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="vag_filter_thumb_")
    tmp_path = Path(tmp_dir)
    print(f"[info] 작업 폴더: {tmp_dir}")

    src_path = tmp_path / "testsrc.mp4"
    if not _generate_testsrc(src_path):
        return 1
    print(f"[info] testsrc 생성: {src_path}")

    ok_count = 0
    for ftype in FILTERS:
        print(f"\n[filter] {ftype}")
        filtered_path = tmp_path / f"{ftype}.mp4"

        if ftype == "pov":
            ok = _apply_pov_filter(src_path, filtered_path, tmp_dir)
        else:
            vf = _build_filter_vf(ftype, INTENSITY)
            if not vf:
                print(f"  [WARN] {ftype} vf chain 생성 실패")
                continue
            ok = _apply_vf_filter(src_path, filtered_path, vf)

        if not ok:
            print(f"  [FAIL] {ftype} 적용 실패")
            continue

        thumb_path = OUTPUT_DIR / f"{ftype}.png"
        if _extract_thumbnail(filtered_path, thumb_path):
            size = thumb_path.stat().st_size
            print(f"  [OK] {thumb_path} ({size} bytes)")
            ok_count += 1
        else:
            print(f"  [FAIL] {ftype} 썸네일 추출 실패")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\n[result] 성공 {ok_count}/{len(FILTERS)}")
    return 0 if ok_count == len(FILTERS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
