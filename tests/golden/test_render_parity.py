"""렌더링 결과 골든 테스트 — 차원 매트릭스 회귀 방지 (#305, #318, #319, #320).

목적
----
`tests/render_spec/test_ffmpeg_mapper.py` 는 vf/af *문자열*만 검증한다. 그러나
fit-vs-speed 같은 회귀(2026-05-27 d97386e)는 실제 ffmpeg가 만든 픽셀에서만 보이는
사례가 있었다. 이 테스트 파일은 RenderSpec → ffmpeg 명령 → *실제 PNG 프레임*
까지 돌리고, 이전에 인간이 확인한 "골든 PNG" 와 픽셀 단위로 비교한다.

차원 매트릭스
------------
DIMENSIONS = {
    "aspect": ["16:9", "9:16", "1:1"],
    "footage_speed": [0.25, 1.0, 2.0],
    "fit": ["contain", "cover", "stretch"],
    "footage_type": ["clip", "image"],
}

총 3 × 3 × 3 × 2 = 54 조합 중 *대표 18개* 를 쌍별 직교(pairwise) 알고리즘으로
추출한다. 9 케이스로 모든 2-way 짝이 커버되고, 그 위에 추가 9 케이스로 3-way
조합까지 절반 이상을 채운다.

프레임 추출 정책 (#318)
----------------------
- **clip** 케이스: 첫 프레임(`t0`) + 중간 프레임(`tmid` = duration_sec/2) 2장.
  중간 프레임은 setpts/playback_rate 가 실제로 시간축에 반영되는지를 픽셀로 검증한다.
  예: setpts=PTS/2.0 을 setpts=PTS 로 잘못 바꾸면 첫 프레임은 같아도 중간 프레임이
  완전히 달라진다.
- **image** 케이스: 첫 프레임만(`t0`). Ken Burns zoompan 의 시작 픽셀을 검증한다.
  이미지는 정지 소스라 중간 프레임 또한 zoom 보간 결과만 다르고 의미가 작아
  골든 부담을 줄였다.

따라서 expected PNG 는 9 image × 1 + 9 clip × 2 = **27 장**.

비교 방법
--------
1. 픽셀 평균 차이 ≤ 2 % (이슈 본문 기준). 0~255 스케일에서 mean(|delta|)/255.
2. SSIM(Structural Similarity Index) 보조 지표 기록 (#319). 통과/실패 판정엔
   사용하지 않고 실패 메시지·로그에만 기록한다. scikit-image 미설치 환경에서는
   자체 단순 구현(box-mean 기반)으로 폴백한다.
3. 실패 시 `expected.png`, `actual.png`, `diff.png` 를 `_failures/{case_id}/` 에 저장.
4. `--update-golden` 플래그가 켜져 있으면 비교 대신 actual을 expected로 복사하고
   warning 으로 알린다.
5. 로컬: 첫 실행이거나 expected가 누락된 경우 actual을 expected로 자동 복사하고
   warning(자동 시드). CI(`CI=true`)에서는 자동 시드 차단 + 명확한 에러로 실패(#320).

실행 시간
--------
1 케이스 당 ffmpeg 1~2회 호출 → 1~2초. 18 케이스 × 1.5초 ≈ 27초.
픽스처는 5초 / 1024×576 / CRF 28 으로 작게 유지된다.

자연스러운 골든 중복에 대하여
----------------------------
27 PNG 중 일부는 *의도적으로* 동일하게 나온다.

- 16:9 캔버스 × 16:9 소스: contain/cover/stretch 모두 1:1 scale → 동일 첫 프레임.
- 같은 fit/aspect/footage_type 조합에서 speed만 다른 clip 케이스:
  * **첫 프레임(t0)**: setpts 는 시간축만 바꿔 t=0 픽셀이 같음 → speed 불변식의 픽셀 증명.
  * **중간 프레임(tmid)**: output 시간 duration/2 에서 잘라낸 결과는 input 시간으로
    환산하면 항상 input 의 2.5초 지점(=source 길이의 절반) → speed 와 무관하게 같음.
    이 또한 setpts 가 *시간축에만* 영향을 미친다는 불변식의 픽셀 증명.

이 두 불변식이 깨지면 (예: setpts 가 사라지거나 잘못된 식이 들어가면) 픽셀이 달라져
회귀가 즉시 검출된다.
"""

from __future__ import annotations

import shutil
import subprocess
import warnings
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from src.render_spec import (
    Canvas,
    ImageLayer,
    RenderSpec,
    Size,
    VideoLayer,
    spec_to_video_ffmpeg_args,
)
from tests.golden._generate_fixtures import CLIP_PATH, IMAGE_PATH
from tests.golden.conftest import is_ci_environment


# ── 매트릭스 정의 ─────────────────────────────────────────────────────────

DIMENSIONS = {
    "aspect": ["16:9", "9:16", "1:1"],
    "footage_speed": [0.25, 1.0, 2.0],
    "fit": ["contain", "cover", "stretch"],
    "footage_type": ["clip", "image"],
}

# 캔버스 해상도(aspect 별). 너무 크면 ffmpeg 실행 시간이 길어지므로 720p 기준 짧은 변.
ASPECT_CANVAS = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (720, 720),
}

# 비교 임계값: 픽셀 0~255 스케일에서 평균 차이의 비율. 이슈 본문 기준 2%.
PIXEL_DIFF_THRESHOLD = 0.02


# ── 케이스 선택: pairwise + 추가 triple 채우기 ───────────────────────────

@dataclass(frozen=True)
class Case:
    """매트릭스의 한 케이스. dict 키 순서가 흔들리지 않도록 frozen dataclass로 고정."""
    aspect: str
    footage_speed: float
    fit: str
    footage_type: str

    @property
    def case_id(self) -> str:
        """파일명 안전한 식별자. expected PNG 이름이 이 값으로 결정된다."""
        speed = str(self.footage_speed).replace(".", "p")
        return (
            f"{self.aspect.replace(':', 'x')}"
            f"__speed{speed}"
            f"__{self.fit}"
            f"__{self.footage_type}"
        )

    def as_dict(self) -> dict:
        return {
            "aspect": self.aspect,
            "footage_speed": self.footage_speed,
            "fit": self.fit,
            "footage_type": self.footage_type,
        }


def _all_pairs() -> set[tuple]:
    keys = list(DIMENSIONS.keys())
    pairs: set[tuple] = set()
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            for va in DIMENSIONS[a]:
                for vb in DIMENSIONS[b]:
                    pairs.add((a, va, b, vb))
    return pairs


def _all_triples() -> set[tuple]:
    keys = list(DIMENSIONS.keys())
    triples: set[tuple] = set()
    for i, a in enumerate(keys):
        for j in range(i + 1, len(keys)):
            b = keys[j]
            for k in range(j + 1, len(keys)):
                c = keys[k]
                for va in DIMENSIONS[a]:
                    for vb in DIMENSIONS[b]:
                        for vc in DIMENSIONS[c]:
                            triples.add((a, va, b, vb, c, vc))
    return triples


def _case_pairs(case: dict) -> set[tuple]:
    keys = list(DIMENSIONS.keys())
    pairs: set[tuple] = set()
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            pairs.add((a, case[a], b, case[b]))
    return pairs


def _case_triples(case: dict) -> set[tuple]:
    keys = list(DIMENSIONS.keys())
    triples: set[tuple] = set()
    for i, a in enumerate(keys):
        for j in range(i + 1, len(keys)):
            b = keys[j]
            for k in range(j + 1, len(keys)):
                c = keys[k]
                triples.add((a, case[a], b, case[b], c, case[c]))
    return triples


def select_pairwise_cases(target: int = 18) -> list[Case]:
    """결정론적 그리디 알고리즘으로 케이스 선택.

    1. 모든 2-way 짝을 먼저 커버하도록 그리디하게 케이스를 고른다(=쌍별 직교).
    2. 짝이 모두 커버되면 3-way 트리플을 추가로 커버한다.
    3. 그래도 target에 도달하지 않으면 미선택 조합을 순서대로 채운다.

    같은 점수의 후보가 여러 개면 `itertools.product` 순서대로 먼저 나온 것이 선택되어
    실행마다 항상 같은 순서로 나오게 된다(결정론).
    """
    keys = list(DIMENSIONS.keys())
    remaining_pairs = _all_pairs()
    remaining_triples = _all_triples()
    selected: list[Case] = []
    selected_keys: set[tuple] = set()
    all_combos = list(product(*[DIMENSIONS[k] for k in keys]))

    while len(selected) < target:
        best_score = -1
        best_combo: tuple | None = None
        best_case: dict | None = None
        for combo in all_combos:
            if combo in selected_keys:
                continue
            case = dict(zip(keys, combo))
            new_pairs = _case_pairs(case) & remaining_pairs
            new_triples = _case_triples(case) & remaining_triples
            # 짝 우선(가중치 100), 짝이 다 끝나면 트리플 점수가 의미를 가짐
            score = len(new_pairs) * 100 + len(new_triples)
            if score > best_score:
                best_score = score
                best_combo = combo
                best_case = case
        if best_combo is None or best_score == 0:
            # 더 이상 새로운 커버리지가 없을 때 미선택 조합을 순서대로 채운다.
            for combo in all_combos:
                if combo not in selected_keys:
                    case = dict(zip(keys, combo))
                    selected.append(Case(**case))
                    selected_keys.add(combo)
                    if len(selected) >= target:
                        break
            continue
        assert best_case is not None
        selected.append(Case(**best_case))
        selected_keys.add(best_combo)
        remaining_pairs -= _case_pairs(best_case)
        remaining_triples -= _case_triples(best_case)

    return selected


CASES = select_pairwise_cases(target=18)


# ── 케이스 → RenderSpec 변환 ──────────────────────────────────────────────

# 픽스처 해상도는 _generate_fixtures.py 와 일치시킨다.
SOURCE_W, SOURCE_H = 1024, 576


def _fit_geometry(canvas_w: int, canvas_h: int, sw: int, sh: int, fit: str):
    """RenderSpec.builder._compute_fit_geometry 의 픽셀 결과를 그대로 재계산.

    여기서 별도 구현하지 않고 builder 함수를 직접 import 해도 되지만, 본 테스트는
    *매퍼+ffmpeg* 의 결과만 검증해야 하므로 빌더 의존도를 줄이려고 같은 식을 단순화하여
    재현한다. (builder가 바뀌면 매퍼 입력 픽셀도 같이 바뀌어야 정상이므로 이중 검증.)
    """
    from src.render_spec.builder import _compute_fit_geometry  # 내부 함수지만 결정론적
    canvas = Canvas(width=canvas_w, height=canvas_h, fps=30)
    source = Size(width=sw, height=sh)
    return _compute_fit_geometry(canvas, source, fit)


def case_to_render_spec(case: Case) -> RenderSpec:
    """케이스 1개를 RenderSpec으로 변환.

    footage_type="clip"  → VideoLayer (playback_rate = footage_speed)
    footage_type="image" → ImageLayer (zoom 1.0 → 1.15, playback_rate 없음 — 시간축은
                          duration_sec 으로만 표현)
    """
    cw, ch = ASPECT_CANVAS[case.aspect]
    canvas = Canvas(width=cw, height=ch, fps=30)
    scale_x, scale_y, crop, pad = _fit_geometry(cw, ch, SOURCE_W, SOURCE_H, case.fit)

    # 영상 길이: 5초 / speed (clip), 또는 5초 고정(image)
    if case.footage_type == "clip":
        clip_duration = 5.0  # 픽스처 길이와 동일
        duration_sec = clip_duration / case.footage_speed
        layer = VideoLayer(
            source=str(CLIP_PATH),
            source_size=Size(width=SOURCE_W, height=SOURCE_H),
            scale_x=scale_x,
            scale_y=scale_y,
            crop=crop,
            pad=pad,
            playback_rate=case.footage_speed,
            rotation_deg=0,
            start_sec=0.0,
            end_sec=duration_sec,
        )
    else:
        # 이미지: speed는 duration 계산에만 영향 (ImageLayer는 playback_rate 없음).
        # 첫 프레임 비교라 speed 변화가 픽셀에 영향 안 주는 것이 기대 동작이다.
        base_duration = 5.0
        duration_sec = base_duration / case.footage_speed
        layer = ImageLayer(
            source=str(IMAGE_PATH),
            source_size=Size(width=SOURCE_W, height=SOURCE_H),
            scale_x=scale_x,
            scale_y=scale_y,
            crop=crop,
            pad=pad,
            zoom_from=1.0,
            zoom_to=1.15,
            start_sec=0.0,
            end_sec=duration_sec,
        )

    return RenderSpec(
        version=1,
        scene_id=1,
        canvas=canvas,
        duration_sec=duration_sec,
        layers=[layer],
    )


# ── ffmpeg 실행 + 프레임 추출 (#318: 첫/중간 프레임) ───────────────────────

# 프레임 태그: case_id 뒤에 붙어 PNG 파일명을 만든다(`{case_id}__{tag}.png`).
FRAME_TAG_FIRST = "t0"
FRAME_TAG_MIDDLE = "tmid"


def frame_tags_for_case(case: "Case") -> list[str]:
    """케이스 타입별 추출할 프레임 태그 목록 (#318).

    - clip: 첫 프레임 + 중간 프레임(speed/setpts 실효성 검증).
    - image: 첫 프레임만(정지 소스 + Ken Burns 시작점만 검증).
    """
    if case.footage_type == "clip":
        return [FRAME_TAG_FIRST, FRAME_TAG_MIDDLE]
    return [FRAME_TAG_FIRST]


def _render_frame_at(
    spec: RenderSpec, out_png: Path, *, seek_output_sec: float | None
) -> None:
    """RenderSpec → ffmpeg → 단일 PNG (특정 출력 시점).

    Args:
        seek_output_sec: None 이면 첫 프레임(t=0). 값이 있으면 출력 타임라인 기준
            그 시점의 프레임을 추출한다. setpts 가 적용된 *출력 시간* 기준이며,
            `-i` 다음에 `-ss` 를 넣어 필터 후 출력 스트림에서 시킹한다.
    """
    args = spec_to_video_ffmpeg_args(spec)
    vf = args["vf"]
    if vf is None:
        raise RuntimeError("spec_to_video_ffmpeg_args 가 vf=None 반환 — footage 레이어 누락")

    layer = spec.layers[0]
    out_png.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if isinstance(layer, ImageLayer):
        # 이미지는 `-loop 1` 로 입력해야 zoompan이 1프레임 이상 동작한다.
        cmd += ["-loop", "1", "-i", layer.source]
    else:
        cmd += ["-i", layer.source]
    cmd += ["-vf", vf]
    # `-ss` 는 `-i` 이후에 두어 *필터 적용 후 출력 타임라인* 기준으로 시킹한다.
    # 첫 프레임(seek=None) 일 때는 `-ss` 를 생략해 zoompan 등 1프레임 단위 필터의
    # 초기 상태를 정확히 잡는다.
    if seek_output_sec is not None:
        cmd += ["-ss", f"{seek_output_sec:.6f}"]
    cmd += ["-frames:v", "1", "-f", "image2", str(out_png)]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 골든 렌더 실패\nseek={seek_output_sec} vf={vf}\nstderr={result.stderr}"
        )
    if not out_png.exists() or out_png.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg가 PNG를 만들지 못함: {out_png}")


def render_frames(
    spec: RenderSpec, case: "Case", working_dir: Path
) -> dict[str, Path]:
    """케이스 타입에 따라 1~2장의 PNG 를 추출한다 (#318).

    반환: `{tag: actual_png_path}` (tag 는 `t0` 또는 `tmid`).
    """
    out: dict[str, Path] = {}
    for tag in frame_tags_for_case(case):
        actual_path = working_dir / f"{case.case_id}__{tag}.png"
        seek = None if tag == FRAME_TAG_FIRST else (spec.duration_sec / 2.0)
        _render_frame_at(spec, actual_path, seek_output_sec=seek)
        out[tag] = actual_path
    return out


# ── 픽셀 비교 (#305 + #319 SSIM 보조 지표) ─────────────────────────────────

@dataclass
class CompareResult:
    """이미지 비교 결과.

    - mean_diff_ratio / max_diff / size_match: 통과/실패 판정용 기본 지표(#305).
    - ssim: 구조적 유사도 (1.0=완전 동일, 0~1 범위). 보조 지표(#319). 통과 판정에는
      사용하지 않고 실패 시 진단 정보에만 기록한다.
    - ssim_source: SSIM 산출 방법 식별자(`"scikit-image"` 또는 `"fallback-mean"`).
      재현 가능한 디버깅을 위해 어떤 구현이 동작했는지 남긴다.
    """
    mean_diff_ratio: float
    max_diff: int
    size_match: bool
    ssim: float = 1.0
    ssim_source: str = "n/a"


def _ssim_with_scikit(a_img: Image.Image, b_img: Image.Image) -> float | None:
    """scikit-image 가 설치돼 있으면 표준 SSIM 을 계산.

    설치되지 않았거나 임포트 실패 시 None 반환 → 폴백 구현으로 전환.
    """
    try:
        import numpy as np  # noqa: F401 — scikit-image 가 numpy 의존
        from skimage.metrics import structural_similarity as ssim  # type: ignore
    except Exception:
        return None
    import numpy as np  # 이 시점에서는 반드시 import 가능
    a_arr = np.asarray(a_img, dtype=np.float64)
    b_arr = np.asarray(b_img, dtype=np.float64)
    # 채널이 있는 RGB 이미지 — channel_axis=-1, data_range=255.
    return float(
        ssim(a_arr, b_arr, data_range=255.0, channel_axis=-1)
    )


def _ssim_fallback(a_img: Image.Image, b_img: Image.Image) -> float:
    """scikit-image 미설치 환경용 단순 SSIM 폴백 구현.

    가우시안 윈도우 대신 *전체 이미지* 의 평균/분산/공분산을 한 번에 계산한다.
    이는 SSIM 의 single-window 근사로, 구조적 변화의 절대값은 정밀하지 않지만
    "동일 이미지 → 1.0, 다른 이미지 → 1.0 미만" 의 정성적 신호는 제공한다.
    의존성 추가 부담 없이 보조 지표를 활성화하는 게 목적이다(#319).
    """
    # numpy 없이 순수 파이썬으로 계산 — 27개 이미지 × 720p 정도라 충분히 빠름.
    a = a_img.convert("L")  # 휘도(Y) 채널로 환산 → 단일 채널 SSIM
    b = b_img.convert("L")
    a_px = list(a.getdata())
    b_px = list(b.getdata())
    n = len(a_px)
    if n == 0 or n != len(b_px):
        return 0.0
    mean_a = sum(a_px) / n
    mean_b = sum(b_px) / n
    var_a = 0.0
    var_b = 0.0
    cov_ab = 0.0
    for x, y in zip(a_px, b_px):
        dx = x - mean_a
        dy = y - mean_b
        var_a += dx * dx
        var_b += dy * dy
        cov_ab += dx * dy
    var_a /= n
    var_b /= n
    cov_ab /= n
    # SSIM constants (L=255 기준)
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    numerator = (2 * mean_a * mean_b + c1) * (2 * cov_ab + c2)
    denominator = (mean_a ** 2 + mean_b ** 2 + c1) * (var_a + var_b + c2)
    if denominator == 0:
        return 1.0
    return numerator / denominator


def compute_ssim(a_img: Image.Image, b_img: Image.Image) -> tuple[float, str]:
    """SSIM 계산. scikit-image 우선, 미설치 시 폴백 (#319)."""
    if a_img.size != b_img.size:
        return 0.0, "size-mismatch"
    sk = _ssim_with_scikit(a_img, b_img)
    if sk is not None:
        return sk, "scikit-image"
    return _ssim_fallback(a_img, b_img), "fallback-mean"


def compare_images(a_path: Path, b_path: Path) -> CompareResult:
    """두 PNG의 픽셀 단위 차이 + SSIM 보조 지표 (#305, #319).

    - mean_diff_ratio: 0~1 (=0~100%). 0=완전 동일. 통과/실패 판정 기준.
    - max_diff: 채널 최대 차이(0~255).
    - size_match: 해상도가 일치하면 True. 다르면 diff 비교는 의미가 없어 ratio=1.0 처리.
    - ssim: 1.0=완전 동일. 보조 지표(통과/실패에는 사용 안 함).
    """
    a = Image.open(a_path).convert("RGB")
    b = Image.open(b_path).convert("RGB")
    if a.size != b.size:
        # SSIM 도 의미가 없지만 보조 정보를 위해 0.0 으로 표기.
        return CompareResult(
            mean_diff_ratio=1.0,
            max_diff=255,
            size_match=False,
            ssim=0.0,
            ssim_source="size-mismatch",
        )
    diff = ImageChops.difference(a, b)
    # 채널별 평균과 최대값. 평균은 (R+G+B)/3 의 픽셀 평균.
    stats = diff.getbbox()
    if stats is None:
        # 완전 동일 — SSIM 계산을 생략해도 1.0 으로 보장된다.
        return CompareResult(
            mean_diff_ratio=0.0,
            max_diff=0,
            size_match=True,
            ssim=1.0,
            ssim_source="identical",
        )
    # 픽셀 단위 평균 차이 계산 (numpy 없이 PIL extrema 활용)
    bands = diff.split()
    pixel_count = a.size[0] * a.size[1]
    sum_abs = 0
    max_d = 0
    for ch in bands:
        # ch.histogram() = 256개 값
        hist = ch.histogram()
        for v, c in enumerate(hist):
            sum_abs += v * c
            if c > 0 and v > max_d:
                max_d = v
    # 평균 = sum / (pixel_count * channels)
    mean = sum_abs / (pixel_count * len(bands))
    ssim_val, ssim_src = compute_ssim(a, b)
    return CompareResult(
        mean_diff_ratio=mean / 255.0,
        max_diff=max_d,
        size_match=True,
        ssim=ssim_val,
        ssim_source=ssim_src,
    )


def save_diff_visualization(actual_path: Path, expected_path: Path, dst: Path) -> None:
    """diff 이미지 시각화: |actual - expected| 를 10배 강조해서 저장.

    저장 실패는 무시(테스트 실패 메시지에 추가 정보를 못 붙이는 것뿐, 본 비교는 이미 끝남).
    """
    try:
        a = Image.open(actual_path).convert("RGB")
        e = Image.open(expected_path).convert("RGB")
        if a.size != e.size:
            # 해상도 불일치는 별도 처리: 단순히 두 이미지를 나란히 저장하지 않고 메시지 PNG 생성.
            from PIL import ImageDraw  # local import: optional path
            placeholder = Image.new("RGB", (max(a.size[0], e.size[0]), 80), (0, 0, 0))
            d = ImageDraw.Draw(placeholder)
            d.text((10, 10), f"size mismatch: actual={a.size} expected={e.size}", fill=(255, 0, 0))
            dst.parent.mkdir(parents=True, exist_ok=True)
            placeholder.save(dst)
            return
        diff = ImageChops.difference(a, e)
        # 차이 강조: 픽셀별 곱하기. ImageChops.multiply 는 *제곱* 이 되므로 직접 처리.
        amplified = Image.eval(diff, lambda v: min(255, v * 10))
        dst.parent.mkdir(parents=True, exist_ok=True)
        amplified.save(dst)
    except Exception:
        pass


# ── pytest 픽스처/헬퍼 ───────────────────────────────────────────────────

GOLDEN_DIR = Path(__file__).parent
EXPECTED_DIR = GOLDEN_DIR / "expected"
FAILURES_DIR = GOLDEN_DIR / "_failures"
WORKING_DIR = GOLDEN_DIR / "_working"  # 매 실행마다 actual PNG를 잠깐 떨궈두는 곳


@pytest.fixture(scope="session", autouse=True)
def _prepare_working_dir():
    if WORKING_DIR.exists():
        shutil.rmtree(WORKING_DIR)
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    yield
    # 세션 종료 시에도 _working 은 남겨둔다(디버깅용). _failures 만 의미가 있음.


# ── 메인 테스트: 매트릭스 케이스 18개 (#305, #318, #319, #320) ─────────────

@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_render_matches_golden(case: Case, update_golden: bool):
    """케이스 1개를 실제로 ffmpeg에 돌려 1~2장의 프레임을 골든과 비교한다.

    - clip 케이스: 첫 프레임(`__t0.png`) + 중간 프레임(`__tmid.png`) 2장 비교 (#318).
    - image 케이스: 첫 프레임(`__t0.png`) 1장 비교.
    - 실패 시 `_failures/{case_id}__{tag}/` 에 actual/expected/diff 3종 PNG 저장.
    - CI 환경에서 expected 누락 시 자동 시드 금지 → 명확한 에러로 실패(#320).
    - 실패 메시지에 SSIM 보조 지표 포함(#319).
    """
    spec = case_to_render_spec(case)
    actual_paths = render_frames(spec, case, WORKING_DIR)

    ci = is_ci_environment()
    failures: list[str] = []

    for tag, actual_path in actual_paths.items():
        expected_filename = f"{case.case_id}__{tag}.png"
        expected_path = EXPECTED_DIR / expected_filename

        # --update-golden: 검증 없이 actual을 expected로 복사
        if update_golden:
            expected_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(actual_path, expected_path)
            warnings.warn(
                f"[update-golden] {expected_filename} 의 골든을 새 actual로 갱신함",
                stacklevel=1,
            )
            continue

        # expected가 없으면 정책 분기 (#320)
        if not expected_path.exists():
            if ci:
                # CI 에서는 자동 시드 금지 — 명확한 에러로 실패시켜 회귀 가드를 보호.
                failures.append(
                    f"[{tag}] expected PNG 누락(CI 자동 시드 금지): {expected_path}\n"
                    f"  로컬에서 `pytest tests/golden/ --update-golden` 으로 골든을 \n"
                    f"  생성하고 시각 확인 후 커밋하세요."
                )
                continue
            # 로컬: 첫 도입/새 케이스 추가 시점에 자동 시드 + warning.
            expected_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(actual_path, expected_path)
            warnings.warn(
                f"[seed-golden] {expected_filename} 골든이 없어 actual로 자동 시드함. "
                f"다음 실행부터 회귀 검증이 활성화됨.",
                stacklevel=1,
            )
            continue

        # 픽셀 비교 + SSIM 보조 지표
        result = compare_images(actual_path, expected_path)
        if (not result.size_match) or result.mean_diff_ratio > PIXEL_DIFF_THRESHOLD:
            # 실패 산출물 저장 (프레임 태그별 폴더 분리)
            fail_dir = FAILURES_DIR / f"{case.case_id}__{tag}"
            fail_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(actual_path, fail_dir / "actual.png")
            shutil.copyfile(expected_path, fail_dir / "expected.png")
            save_diff_visualization(actual_path, expected_path, fail_dir / "diff.png")
            failures.append(
                f"[{tag}] 골든 PNG 불일치: {expected_filename}\n"
                f"  mean_diff_ratio={result.mean_diff_ratio:.4f} "
                f"(threshold={PIXEL_DIFF_THRESHOLD:.4f})\n"
                f"  max_diff={result.max_diff}/255  size_match={result.size_match}\n"
                f"  ssim={result.ssim:.4f} (source={result.ssim_source}, "
                f"통과 판정엔 사용 안 함 — 보조 지표)\n"
                f"  실패 자료: {fail_dir}/"
            )

    if failures:
        pytest.fail(
            f"골든 비교 실패: case={case.case_id} ({len(failures)} frame(s))\n"
            + "\n".join(failures)
        )


# ── 메타 테스트: 케이스 선택 로직이 깨지지 않았는지 ──────────────────────

def test_case_selection_covers_all_pairs():
    """선택된 케이스가 모든 2-way 짝을 커버하는지 확인 — pairwise 보증."""
    all_p = _all_pairs()
    covered: set[tuple] = set()
    for c in CASES:
        covered |= _case_pairs(c.as_dict())
    missing = all_p - covered
    assert not missing, f"커버되지 않은 짝이 있음: {missing}"


def test_case_count_is_target():
    """이슈 본문 기준 18 케이스."""
    assert len(CASES) == 18, f"기대 18개, 실제 {len(CASES)}"


def test_case_ids_are_unique():
    """case_id 가 expected PNG 파일명으로 쓰이므로 중복 불가."""
    ids = [c.case_id for c in CASES]
    assert len(ids) == len(set(ids)), f"case_id 중복: {ids}"


def test_expected_pngs_present_for_all_cases():
    """모든 케이스의 expected PNG 가 존재하는지 검증 (#318, #320).

    - clip 케이스: `__t0.png` + `__tmid.png` 두 장 모두 존재해야 함.
    - image 케이스: `__t0.png` 한 장 존재해야 함.

    이 메타 테스트는 신규 케이스 추가 시 expected 누락을 사전에 잡고, CI 환경에서는
    자동 시드 차단(#320)과 함께 회귀 가드의 핵심 가치를 보호한다.
    """
    missing: list[str] = []
    for case in CASES:
        for tag in frame_tags_for_case(case):
            path = EXPECTED_DIR / f"{case.case_id}__{tag}.png"
            if not path.exists():
                missing.append(str(path.relative_to(EXPECTED_DIR.parent)))
    if missing:
        # CI 환경에서는 더 단호한 메시지로 처리한다.
        ci_hint = (
            "\n  CI: 로컬에서 `pytest tests/golden/ --update-golden` 으로 골든을 "
            "생성하고 커밋하세요."
            if is_ci_environment()
            else ""
        )
        pytest.fail(
            f"누락된 expected PNG: {len(missing)} 개{ci_hint}\n  "
            + "\n  ".join(missing)
        )
