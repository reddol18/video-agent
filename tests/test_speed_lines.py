"""집중선(속도선) 생성 순수 로직 + 캐시 키 + 실제 PNG 스모크 테스트.

새 모델(전면 개편):
- 선은 삼각형(3점) 채움 + 두 옆변에만 검은 외곽선(밑변 제외).
- 애니메이션은 "선 길이 성장" 폐기 → 선별 페이드 인/아웃 반복(버킷 페이드).
- 끝점(뾰족한 안쪽 꼭짓점) 위치 분산 대폭 확대(INNER_STOP_SPREAD=0.7).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from src import speed_lines as sl


# ── 캐시 키 결정성/민감도 ─────────────────────────────────────────────────

def test_cache_key_deterministic():
    """같은 파라미터+해상도면 항상 같은 키."""
    k1 = sl.compute_cache_key(out_w=1080, out_h=1920, color="#000000", density=80)
    k2 = sl.compute_cache_key(out_w=1080, out_h=1920, color="#000000", density=80)
    assert k1 == k2


def test_cache_key_resolution_sensitive():
    """해상도가 다르면 다른 키(화면비율 대응)."""
    k_v = sl.compute_cache_key(out_w=1080, out_h=1920)
    k_h = sl.compute_cache_key(out_w=1920, out_h=1080)
    assert k_v != k_h


def test_cache_key_param_sensitive():
    """색/밀도/두께/중심 중 하나만 달라도 다른 키."""
    base = dict(out_w=1080, out_h=1920)
    k0 = sl.compute_cache_key(**base)
    assert sl.compute_cache_key(**base, color="#FF0000") != k0
    assert sl.compute_cache_key(**base, density=120) != k0
    assert sl.compute_cache_key(**base, thickness=2.0) != k0
    assert sl.compute_cache_key(**base, center_x=0.3) != k0
    assert sl.compute_cache_key(**base, seed=7) != k0
    # 표시 영역(area_w/area_h) 도 캐시 키에 포함.
    assert sl.compute_cache_key(**base, area_w=0.5) != k0
    assert sl.compute_cache_key(**base, area_h=0.5) != k0
    assert sl.compute_cache_key(**base, width_variance=0.5) != k0
    # 페이드 버킷: num_buckets / phase_bucket 도 캐시 키에 포함.
    assert sl.compute_cache_key(**base, num_buckets=4) != k0
    assert sl.compute_cache_key(**base, phase_bucket=0) != k0
    assert sl.compute_cache_key(**base, phase_bucket=0) != sl.compute_cache_key(**base, phase_bucket=1)


def test_cache_key_area_default_unchanged():
    """area 기본(1.0×1.0) 명시 여부와 무관하게 같은 키 — 기존 씬 회귀 안전."""
    base = dict(out_w=1080, out_h=1920)
    assert sl.compute_cache_key(**base) == sl.compute_cache_key(
        **base, area_w=1.0, area_h=1.0,
    )


def test_cache_key_static_differs_from_bucket():
    """phase_bucket=None(정적, 모든 선) 은 특정 버킷 PNG 와 다른 캐시."""
    base = dict(out_w=1080, out_h=1920)
    static_key = sl.compute_cache_key(**base, phase_bucket=None)
    bucket0 = sl.compute_cache_key(**base, phase_bucket=0)
    assert static_key != bucket0


# ── 삼각형 기하 ────────────────────────────────────────────────────────────

def test_triangle_count_matches_density():
    tris = sl.compute_line_triangles(out_w=100, out_h=100, density=40, supersample=1)
    assert len(tris) == 40
    for t in tris:
        # (a) 삼각형이 3점 폴리곤인지.
        assert len(t["tri"]) == 3
        assert len(t["base"]) == 2
        assert isinstance(t["apex"], tuple) and len(t["apex"]) == 2


def test_triangle_base_wider_than_apex():
    """삼각형: 바깥쪽 밑변 두 점 간 거리 > 0, apex 는 밑변에서 떨어진 별개 점(뾰족)."""
    tris = sl.compute_line_triangles(
        out_w=200, out_h=200, density=1, jitter=0.0, supersample=1,
    )
    t = tris[0]
    base_a, base_b = t["base"]
    apex = t["apex"]
    base_w = math.dist(base_a, base_b)
    assert base_w > 0
    # apex 는 밑변 중점에서 확실히 떨어져 있다(선 길이 > 밑변 폭).
    base_mid = ((base_a[0] + base_b[0]) / 2, (base_a[1] + base_b[1]) / 2)
    assert math.dist(base_mid, apex) > base_w


def test_apex_respects_inner_radius_and_spread():
    """정사각 영역(area 1.0)에서 apex(안쪽 뾰족 끝)는 빈원 경계를 침범하지 않되 크게 분산."""
    W = H = 200
    cx = cy = 100.0
    inner_ratio = 0.3
    tris = sl.compute_line_triangles(
        out_w=W, out_h=H, density=16, jitter=0.0,
        center_x=0.5, center_y=0.5, inner_ratio=inner_ratio, supersample=1,
    )
    expected_inner = inner_ratio * (min(W, H) / 2.0)
    apex_dists = []
    for t in tris:
        apex = t["apex"]
        d = math.dist((cx, cy), apex)
        # 빈원 침범 금지(핵심 불변식): apex 는 항상 빈원 경계 이상.
        assert d >= expected_inner - 2.0
        apex_dists.append(d)
    # (c) 끝점 분산이 충분히 큰지: min/max apex 반경 차가 임계 이상.
    # INNER_STOP_SPREAD=0.7 이면 축~대각 방향에서 큰 편차가 나온다.
    assert max(apex_dists) - min(apex_dists) > (min(W, H) / 2.0) * 0.15


def test_endpoint_spread_is_large():
    """요구사항 1: 끝점 분산 대폭 확대 — 상수 자체가 0.7 이상."""
    assert sl.INNER_STOP_SPREAD >= 0.7


def test_inner_ellipse_when_area_aspect_differs():
    """area_w != area_h 면 빈 영역이 타원 — 가로/세로 반경이 다르다."""
    W = H = 400
    cx = cy = 200.0
    inner_ratio = 0.4
    aw, ah = 0.8, 0.4
    tris = sl.compute_line_triangles(
        out_w=W, out_h=H, density=4, jitter=0.0,
        center_x=0.5, center_y=0.5, inner_ratio=inner_ratio,
        area_w=aw, area_h=ah, supersample=1,
    )
    rx_exp = inner_ratio * (aw * W / 2.0)
    ry_exp = inner_ratio * (ah * H / 2.0)
    dists = []
    for t in tris:
        apex = t["apex"]
        dx = apex[0] - cx
        dy = apex[1] - cy
        dists.append((abs(dx), abs(dy), math.dist((cx, cy), apex)))
    horiz = [d for (adx, ady, d) in dists if ady < 1.0]
    vert = [d for (adx, ady, d) in dists if adx < 1.0]
    assert horiz and vert
    assert horiz[0] >= rx_exp - 2.0
    assert vert[0] >= ry_exp - 2.0
    assert abs(rx_exp - ry_exp) > 1.0


def test_inner_ratio_large_grows_empty_region():
    """inner_ratio 상한이 커져 큰 빈원 생성 — apex 평균 반경이 커진다."""
    W = H = 400
    cx = cy = 200.0
    small = sl.compute_line_triangles(
        out_w=W, out_h=H, density=4, jitter=0.0, inner_ratio=0.3, supersample=1,
    )
    big = sl.compute_line_triangles(
        out_w=W, out_h=H, density=4, jitter=0.0, inner_ratio=0.9, supersample=1,
    )

    def _mean_apex(tris):
        ds = [math.dist((cx, cy), t["apex"]) for t in tris]
        return sum(ds) / len(ds)

    assert small
    assert big
    assert _mean_apex(big) > _mean_apex(small)
    inner_exp = 0.9 * (W / 2.0)
    assert _mean_apex(big) >= inner_exp - 2.0   # 빈원 침범 금지


def test_width_variance_makes_lines_differ():
    """width_variance>0 이면 선마다 바깥 밑변 굵기가 다르고, seed 로 재현된다."""
    def _base_widths(wv, seed):
        tris = sl.compute_line_triangles(
            out_w=300, out_h=300, density=30, jitter=0.0,
            width_variance=wv, seed=seed, supersample=1,
        )
        return [math.dist(t["base"][0], t["base"][1]) for t in tris]

    uniform = _base_widths(0.0, 5)
    varied = _base_widths(0.3, 5)
    assert max(uniform) - min(uniform) < 1e-6
    assert max(varied) - min(varied) > 1e-3
    assert varied == _base_widths(0.3, 5)
    assert varied != _base_widths(0.3, 6)


# ── 페이드 버킷 배정 ────────────────────────────────────────────────────────

def test_buckets_assignment_deterministic_and_ranged():
    """버킷 인덱스는 [0, num_buckets) 범위이고 seed 로 재현된다."""
    b1 = sl.compute_line_buckets(seed=3, density=50, num_buckets=8)
    b2 = sl.compute_line_buckets(seed=3, density=50, num_buckets=8)
    assert b1 == b2
    assert len(b1) == 50
    assert all(0 <= b < 8 for b in b1)
    # 서로 다른 seed 면 배정이 달라진다.
    assert sl.compute_line_buckets(seed=4, density=50, num_buckets=8) != b1


def test_bucket_assignment_matches_triangles():
    """compute_line_buckets 의 배정이 compute_line_triangles 의 bucket 필드와 일치."""
    buckets = sl.compute_line_buckets(seed=7, density=40, num_buckets=8)
    tris = sl.compute_line_triangles(
        out_w=300, out_h=300, density=40, jitter=0.0, seed=7,
        num_buckets=8, inner_ratio=0.1, supersample=1,
    )
    # 축소/빈원으로 일부 선이 생략될 수 있으나, 정사각 area 1.0·작은 빈원에선 전부 생성됨.
    assert len(tris) == 40
    assert [t["bucket"] for t in tris] == buckets


def test_buckets_partition_lines_disjoint():
    """(d) 버킷별 PNG 가 서로 다른 선 집합인지 — 각 버킷에 배정된 선 집합이 서로소."""
    tris = sl.compute_line_triangles(
        out_w=300, out_h=300, density=64, jitter=0.0, seed=11,
        num_buckets=8, inner_ratio=0.1, supersample=1,
    )
    from collections import defaultdict
    per_bucket = defaultdict(list)
    for idx, t in enumerate(tris):
        per_bucket[t["bucket"]].append(idx)
    # 최소 2개 이상의 버킷이 실제로 선을 담는다(무작위 위상 → 분산).
    non_empty = [b for b, idxs in per_bucket.items() if idxs]
    assert len(non_empty) >= 2
    # 어떤 선도 두 버킷에 동시에 속하지 않는다(서로소): 전체 합 = 전체 선 수.
    total = sum(len(idxs) for idxs in per_bucket.values())
    assert total == len(tris)


# ── 실제 PNG 생성 스모크 ──────────────────────────────────────────────────

def test_generate_png_smoke(tmp_path):
    """세로 영상 해상도로 PNG 생성 — 0바이트 아니고 캔버스 크기와 일치, 캐시 재사용."""
    from PIL import Image

    cache_dir = tmp_path / "sl_cache"
    p1 = sl.generate_speed_lines_png(
        out_w=270, out_h=480, density=60, supersample=2, cache_dir=str(cache_dir),
    )
    assert Path(p1).exists() and Path(p1).stat().st_size > 0
    img = Image.open(p1)
    assert img.size == (270, 480)
    assert img.mode == "RGBA"
    assert img.getextrema()[3][1] > 0

    p2 = sl.generate_speed_lines_png(
        out_w=270, out_h=480, density=60, supersample=2, cache_dir=str(cache_dir),
    )
    assert p1 == p2


def test_bucket_png_differs_from_full(tmp_path):
    """버킷 PNG(일부 선만)는 정적 전체 PNG 보다 불투명 픽셀 수가 적다."""
    import numpy as np
    from PIL import Image

    cache_dir = tmp_path / "sl_bucket"
    common = dict(
        out_w=200, out_h=200, density=80, jitter=0.0, seed=5,
        inner_ratio=0.1, num_buckets=8, supersample=2, cache_dir=str(cache_dir),
    )
    p_full = sl.generate_speed_lines_png(phase_bucket=None, **common)
    p_b0 = sl.generate_speed_lines_png(phase_bucket=0, **common)
    assert p_full != p_b0
    full_cnt = int((np.array(Image.open(p_full))[:, :, 3] > 0).sum())
    b0_cnt = int((np.array(Image.open(p_b0))[:, :, 3] > 0).sum())
    assert b0_cnt > 0
    assert b0_cnt < full_cnt


def test_bucket_pngs_are_different_line_sets(tmp_path):
    """서로 다른 버킷 PNG 는 서로 다른 픽셀 분포(다른 선 집합)를 가진다."""
    import numpy as np
    from PIL import Image

    cache_dir = tmp_path / "sl_bucket_diff"
    common = dict(
        out_w=200, out_h=200, density=80, jitter=0.0, seed=9,
        inner_ratio=0.1, num_buckets=8, supersample=2, cache_dir=str(cache_dir),
    )
    a0 = np.array(Image.open(sl.generate_speed_lines_png(phase_bucket=0, **common)))
    a1 = np.array(Image.open(sl.generate_speed_lines_png(phase_bucket=1, **common)))
    # 알파 마스크가 서로 달라야 한다(다른 선 집합).
    assert not np.array_equal(a0[:, :, 3] > 0, a1[:, :, 3] > 0)


# ── 삼각형 외곽선(stroke): 옆변만, 밑변 제외 ────────────────────────────────

def test_stroke_on_sides_not_on_base(tmp_path):
    """(b) 옆변 2개에만 검은 stroke, 밑변엔 없음.

    검정 채움색과 구분하기 위해 채움색을 흰색(#FFFFFF)으로 지정하고 stroke(검정)를
    관찰한다. 단일 수평선(0도)을 그려 옆변 근처엔 검은 픽셀 존재, 밑변(바깥 세로 변)
    근처엔 검은 픽셀 부재를 확인한다.
    """
    import numpy as np
    from PIL import Image

    cache_dir = tmp_path / "sl_stroke"
    W = H = 400
    # density=1, jitter=0 → 각도 0(수평 오른쪽) 단일 선. thickness 크게 → 밑변 뚜렷.
    p = sl.generate_speed_lines_png(
        out_w=W, out_h=H, color="#FFFFFF", density=1, jitter=0.0,
        thickness=3.0, inner_ratio=0.2, supersample=3, seed=0,
        cache_dir=str(cache_dir),
    )
    arr = np.array(Image.open(p))
    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3]
    # 불투명 & 어두운(검정에 가까운) 픽셀 = stroke.
    dark = (alpha > 40) & (rgb.max(axis=2) < 80)
    assert dark.any(), "검은 외곽선(stroke) 픽셀이 존재해야 한다"

    ys, xs = np.where(alpha > 40)
    assert len(xs) > 0
    # 삼각형 밑변은 바깥쪽(오른쪽) 세로 변 — x 최대 부근.
    x_max = xs.max()
    x_min = xs.min()
    # 밑변 근처(x 최대의 오른쪽 끝 2px 열)에서 stroke(검정) 부재 확인.
    base_band = dark[:, max(0, x_max - 1):x_max + 1]
    # 밑변 열에는 채움(흰색)만 있고 검은 stroke 는 없어야 한다.
    assert base_band.sum() == 0, "밑변(바깥 변)에는 검은 외곽선이 없어야 한다"
    # 옆변은 apex(x 최소 근처)로 모이는 두 변 — 안쪽~중간 구간에 검은 픽셀이 존재.
    inner_band = dark[:, x_min:x_min + max(2, (x_max - x_min) // 2)]
    assert inner_band.sum() > 0, "옆변 근처엔 검은 외곽선이 존재해야 한다"


# ── 색상 반영 ─────────────────────────────────────────────────────────────

def test_color_change_produces_different_png(tmp_path):
    """색 A→B 변경 시 생성 PNG 경로가 다르고 채움색이 실제로 다르다."""
    import numpy as np
    from PIL import Image

    cache_dir = tmp_path / "sl_color"
    # stroke(검정)와 섞이지 않게 채움 픽셀만 검사하도록 밝은 색으로 대비.
    p_white = sl.generate_speed_lines_png(
        out_w=200, out_h=200, color="#FFFFFF", density=40, cache_dir=str(cache_dir),
    )
    p_red = sl.generate_speed_lines_png(
        out_w=200, out_h=200, color="#FF0000", density=40, cache_dir=str(cache_dir),
    )
    assert p_white != p_red
    a_white = np.array(Image.open(p_white))
    a_red = np.array(Image.open(p_red))
    # 흰 채움 픽셀이 존재.
    m_white = (a_white[:, :, 3] > 200) & (a_white[:, :, :3].min(axis=2) > 200)
    m_red = (a_red[:, :, 3] > 200) & (a_red[:, :, 0] > 200) & (a_red[:, :, 1] < 60)
    assert m_white.any()
    assert m_red.any()


# ── 표시 영역 ─────────────────────────────────────────────────────────────

def test_area_clips_radiation_inside_box(tmp_path):
    """area_w/area_h < 1.0 이면 방사선이 지정 영역 안에만 있고 영역 밖은 투명."""
    import numpy as np
    from PIL import Image

    W = H = 300
    aw = ah = 0.4
    cache_dir = tmp_path / "sl_area"
    p = sl.generate_speed_lines_png(
        out_w=W, out_h=H, color="#000000", density=120,
        center_x=0.5, center_y=0.5, area_w=aw, area_h=ah, supersample=2,
        cache_dir=str(cache_dir),
    )
    arr = np.array(Image.open(p))
    ys, xs = np.where(arr[:, :, 3] > 0)
    assert len(xs) > 0
    left = (0.5 - aw / 2) * W
    right = (0.5 + aw / 2) * W
    top = (0.5 - ah / 2) * H
    bottom = (0.5 + ah / 2) * H
    assert xs.min() >= left - 1
    assert xs.max() <= right + 1
    assert ys.min() >= top - 1
    assert ys.max() <= bottom + 1


def test_area_full_matches_no_area(tmp_path):
    """area 기본(1.0)이면 area 미지정과 동일 PNG(회귀 안전)."""
    cache_dir = tmp_path / "sl_full"
    p_default = sl.generate_speed_lines_png(
        out_w=200, out_h=200, density=50, seed=3, cache_dir=str(cache_dir),
    )
    p_full = sl.generate_speed_lines_png(
        out_w=200, out_h=200, density=50, seed=3, area_w=1.0, area_h=1.0,
        cache_dir=str(cache_dir),
    )
    assert p_default == p_full


# ── 선별 페이드(버킷) 알파 헬퍼 ────────────────────────────────────────────

def test_bucket_alpha_static_when_off():
    """animate=False 면 항상 1.0(정적, 모든 선 완전 표시) — 회귀 안전."""
    from src.clip_editor import compute_speed_lines_bucket_alpha
    for t in (0.0, 0.35, 0.7, 1.0):
        assert compute_speed_lines_bucket_alpha(
            t, 0.0, 0, 8, animate=False, anim_speed=1.0, anim_period=0.7,
        ) == 1.0


def test_bucket_alpha_fades_in_and_out():
    """animate=True 면 한 주기 안에서 0→1→0 으로 페이드 인/아웃(sin^2)."""
    from src.clip_editor import compute_speed_lines_bucket_alpha
    # bucket 0, offset 0 → frac=elapsed/period.
    a_start = compute_speed_lines_bucket_alpha(
        0.0, 0.0, 0, 8, animate=True, anim_speed=1.0, anim_period=1.0,
    )
    a_mid = compute_speed_lines_bucket_alpha(
        0.5, 0.0, 0, 8, animate=True, anim_speed=1.0, anim_period=1.0,
    )
    a_end = compute_speed_lines_bucket_alpha(
        0.999, 0.0, 0, 8, animate=True, anim_speed=1.0, anim_period=1.0,
    )
    assert a_start == pytest.approx(0.0, abs=0.1)   # 시작 = 안 보임
    assert a_mid == pytest.approx(1.0, abs=0.1)     # 중간 = 최대
    assert a_end == pytest.approx(0.0, abs=0.1)     # 끝 = 다시 안 보임


def test_bucket_alpha_phase_offset_between_buckets():
    """서로 다른 버킷은 같은 시각에 다른 알파(위상 offset) → 무작위 깜빡임."""
    from src.clip_editor import compute_speed_lines_bucket_alpha
    t = 0.0
    a0 = compute_speed_lines_bucket_alpha(
        t, 0.0, 0, 8, animate=True, anim_speed=1.0, anim_period=1.0,
    )
    a4 = compute_speed_lines_bucket_alpha(
        t, 0.0, 4, 8, animate=True, anim_speed=1.0, anim_period=1.0,
    )
    # bucket0 offset=0 → alpha≈0, bucket4 offset=0.5 → alpha≈1. 확연히 다름.
    assert abs(a0 - a4) > 0.5


def test_bucket_alpha_quantized():
    """알파는 스텝 단위로 양자화 → 합성 캐시에 유리."""
    from src.clip_editor import compute_speed_lines_bucket_alpha, SPEED_LINES_ALPHA_STEPS
    vals = {
        compute_speed_lines_bucket_alpha(
            t / 1000.0, 0.0, 0, 8, animate=True, anim_speed=1.0, anim_period=1.0,
        )
        for t in range(0, 1000)
    }
    n = SPEED_LINES_ALPHA_STEPS
    for v in vals:
        assert v == pytest.approx(round(v * n) / n, abs=1e-9)
