"""집중선(속도선) 오버레이 프로시저럴 생성 + 파라미터 해시 캐시.

만화/웹툰 특유의 "집중선(speed lines / focus lines)" — 화면 중심점에서 사방으로 뻗는
날카로운 삼각형(뾰족) 선들로 박진감을 준다. cairosvg/rsvg 같은 SVG 래스터화 라이브러리가
없으므로(사용자 승인) Pillow 로 직접 그린다.

핵심 설계
--------
- 출력 캔버스(out_w × out_h)의 ``supersample`` 배(기본 3배) 해상도로 그린 뒤 LANCZOS 로
  축소한다 → clip_zoom 등으로 확대돼도 계단현상이 덜하고 선 끝이 매끈하다(안티에일리어싱).
- 각 선은 중심(cx, cy) 기준으로 안쪽 뾰족한 끝점에서 표시 영역(area) 경계까지 뻗는다.
  중앙에 빈 **타원** 영역을 둬 피사체가 보이도록 한다.
- 선은 **삼각형(triangle) 모양**: 바깥쪽 넓은 밑변 2점 + 중심쪽 뾰족한 꼭짓점 1점.
  삼각형 채움 + 두 옆변에만 검은 외곽선(stroke) → 밑변(바깥쪽)에는 stroke 없음.
- 각도는 균등 분할 + 랜덤 지터(density 로 개수 조절)로 손그림 같은 불균일함을 준다.
  시드(seed)를 파라미터에 포함해 같은 파라미터면 항상 같은 그림 → 캐시 재현성 보장.

타원 빈 영역
------------
빈 원(중앙 영역)과 방사 범위는 표시 영역(area_w×area_h)의 가로세로 비를 따른다.
정원이 아니라 x반경 = inner_ratio·(area_w·W/2), y반경 = inner_ratio·(area_h·H/2) 의
**타원**으로 배치한다. 각 선의 안쪽 뾰족한 끝점은 각도별로 이 타원 밖에 놓인다.
area_w == area_h 면 정원과 동일(회귀 안전).

끝점 위치 분산 (INNER_STOP_SPREAD)
----------------------------------
각 선의 안쪽 뾰족한 끝(꼭짓점)이 빈원 경계보다 선마다 제각각 다른 깊이에서 멈춘다.
분산 비율 INNER_STOP_SPREAD(=0.7)는 (outer_r - inner_r) 구간 대비 편차폭. 값이 클수록
선마다 끝점 깊이가 확연히 다르다. 빈원은 **절대 침범하지 않는다**(항상 inner_r 이상).

선별 페이드 애니메이션 — "버킷 페이드 인/아웃 반복" (전면 개편)
------------------------------------------------------------
과거 방식(선 길이 성장 anim_t)을 폐기하고, 이제 선은 항상 최대 길이로 고정 그린다.
대신 각 선이 독립적으로 페이드 인 → 페이드 아웃 → (안 보임) → 다시 페이드 인을 반복한다.

- 각 선의 페이드 **위상(phase ∈ [0,1))** 은 seed 기반 완전 랜덤 → 선마다 시간차를 둬서
  무작위로 깜빡이는 박진감.
- 선마다 알파가 다르므로 모든 선을 단일 PNG 에 그릴 수 없다. **버킷(bucket) 접근**을 쓴다:
  선을 페이즈 버킷 N개(num_buckets, 기본 8)로 나눈다. 각 선의 위상 phase 를
  floor(phase*N) 버킷에 배정하고, **버킷별로 PNG 를 생성**한다(그 버킷 선들만 그린 PNG).
  각 버킷 PNG 는 시간에 안 변하므로 오버레이당 최대 N개만 생성/캐시 → 성능 안전.
- clip_editor 의 apply_speed_lines 가 프레임 시각 t 에서 각 버킷의 현재 알파(사인파 값)를
  계산해, 버킷 PNG 를 그 알파로 합성한다.
- ``phase_bucket=None`` 이면 모든 선을 한 PNG 에 그린다(정적/animate=False 용, 회귀 안전).

미리보기 ≡ 렌더 동일성
--------------------
미리보기와 최종 렌더가 **동일한 이 모듈**을 호출해 같은 PNG 를 쓴다. 캐시 키에 모든
파라미터(색/밀도/두께/중심/내부반경/지터/시드/해상도/supersample/area/굵기편차/버킷)를
포함하므로, 파라미터가 하나라도 다르면 다른 PNG, 같으면 캐시 재사용.

모델 파라미터: animate(on/off, 기본 on). animate=off 면 정적(모든 선 알파 1.0).
"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import Optional

# 집중선 PNG 캐시 디렉토리 — 필터 캐시와 같은 output/clips 하위에 둔다.
SPEED_LINES_CACHE_DIR = "output/clips/speed_lines_cache"

# 기본값 (models.SpeedLinesOverlay 와 동기화) — 캐시 키 계산 시 폴백에도 사용.
DEFAULT_COLOR = "#000000"
DEFAULT_DENSITY = 80          # 선 개수
DEFAULT_CENTER_X = 0.5        # 0~1 비율
DEFAULT_CENTER_Y = 0.5
DEFAULT_INNER_RATIO = 0.28    # 중앙 빈 타원 영역 반경 (영역 반변 대비 비율)
DEFAULT_THICKNESS = 0.9       # 바깥쪽 선 굵기 배수 (1.0 = 표준)
DEFAULT_JITTER = 0.6          # 각도 랜덤 지터 강도 (0=균등, 1=강함)
DEFAULT_SEED = 0
DEFAULT_SUPERSAMPLE = 3       # 캔버스 배율 (2~3 권장)
DEFAULT_AREA_W = 1.0          # 표시 영역 폭 비율 (1.0 = 전체 화면)
DEFAULT_AREA_H = 1.0          # 표시 영역 높이 비율 (1.0 = 전체 화면)
DEFAULT_OPACITY = 1.0
DEFAULT_WIDTH_VARIANCE = 0.2  # 선별 바깥 굵기 랜덤 편차 (0=균일, 0.2=±20%)

# 빈원(inner) 상한 — 사용자가 큰 빈원을 만들 수 있도록 상한을 높인다.
INNER_RATIO_MAX = 1.5

# 끝점(뾰족한 안쪽 꼭짓점) 위치 분산 비율 — (outer_r - inner_r) 구간 대비 편차폭.
# 사용자 반복 지적("끝점 분산 부족")을 반영해 0.35 → 0.7 로 대폭 확대.
# 빈원은 절대 침범하지 않는다(target_r 는 항상 inner_r 이상).
INNER_STOP_SPREAD = 0.7

# 페이드 버킷 개수 — 각 선을 seed 기반 위상으로 이 개수의 버킷에 나눠 배정한다.
# 오버레이당 최대 이 개수만큼의 PNG 만 생성/캐시하므로 성능 안전.
DEFAULT_NUM_BUCKETS = 8

# 검은 외곽선(stroke) 두께 — supersample 캔버스 짧은변의 비율(고정, 슬라이더 없음).
# 육안으로 얇게 보이도록 0.15%.
STROKE_WIDTH_RATIO = 0.0015


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _norm_color(color: str) -> tuple[int, int, int]:
    """#RRGGBB / #RGB / 색이름(일부) → (r, g, b). 파싱 실패 시 검정."""
    c = (color or "").strip()
    if c.startswith("#"):
        h = c[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                pass
    named = {
        "black": (0, 0, 0),
        "white": (255, 255, 255),
        "red": (220, 30, 30),
        "blue": (30, 60, 220),
        "yellow": (250, 220, 30),
    }
    return named.get(c.lower(), (0, 0, 0))


def compute_cache_key(
    *,
    out_w: int,
    out_h: int,
    color: str = DEFAULT_COLOR,
    density: int = DEFAULT_DENSITY,
    center_x: float = DEFAULT_CENTER_X,
    center_y: float = DEFAULT_CENTER_Y,
    inner_ratio: float = DEFAULT_INNER_RATIO,
    thickness: float = DEFAULT_THICKNESS,
    jitter: float = DEFAULT_JITTER,
    seed: int = DEFAULT_SEED,
    supersample: int = DEFAULT_SUPERSAMPLE,
    area_w: float = DEFAULT_AREA_W,
    area_h: float = DEFAULT_AREA_H,
    width_variance: float = DEFAULT_WIDTH_VARIANCE,
    num_buckets: int = DEFAULT_NUM_BUCKETS,
    phase_bucket: Optional[int] = None,
) -> str:
    """파라미터 + 해상도 → 결정적 해시 문자열(캐시 파일명).

    해상도(out_w/out_h)를 반드시 포함한다 → 화면비율이 바뀌면 다른 캐시가 생성된다.
    부동소수는 소수 4자리로 정규화해 미세한 float 오차로 캐시가 갈리지 않게 한다.
    ``num_buckets``/``phase_bucket`` 도 포함 → 버킷별로 다른 PNG 가 생성된다(페이드 버킷).
    phase_bucket=None(정적, 모든 선) 은 anim 버킷과 다른 별도 캐시.
    """
    parts = (
        f"{int(out_w)}x{int(out_h)}",
        f"c={_norm_color(color)}",
        f"d={int(density)}",
        f"cx={float(center_x):.4f}",
        f"cy={float(center_y):.4f}",
        f"ir={float(inner_ratio):.4f}",
        f"th={float(thickness):.4f}",
        f"j={float(jitter):.4f}",
        f"s={int(seed)}",
        f"ss={int(supersample)}",
        f"aw={float(area_w):.4f}",
        f"ah={float(area_h):.4f}",
        f"wv={float(width_variance):.4f}",
        f"nb={int(num_buckets)}",
        f"pb={'-' if phase_bucket is None else int(phase_bucket)}",
    )
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def compute_line_buckets(
    *,
    seed: int = DEFAULT_SEED,
    density: int = DEFAULT_DENSITY,
    num_buckets: int = DEFAULT_NUM_BUCKETS,
) -> list[int]:
    """각 선 i 의 페이드 버킷 인덱스 리스트를 반환한다 (seed 기반 완전 랜덤 위상).

    각 선의 위상 phase ∈ [0,1) 을 뽑아 floor(phase*num_buckets) 버킷에 배정한다.
    순수 함수 — 캐시 재현성 보장(같은 seed/density/num_buckets → 같은 배정).
    아래 compute_line_triangles 의 phase_rng 와 **동일한 rng 스트림**을 써야 선 배정이
    일관된다(같은 seed ^ 0x5EED 로 phase 를 뽑는다).
    """
    n = max(1, int(density))
    nb = max(1, int(num_buckets))
    phase_rng = random.Random(int(seed) ^ 0x5EED)
    buckets: list[int] = []
    for _ in range(n):
        phase = phase_rng.random()
        buckets.append(min(nb - 1, int(phase * nb)))
    return buckets


def compute_line_triangles(
    *,
    out_w: int,
    out_h: int,
    density: int = DEFAULT_DENSITY,
    center_x: float = DEFAULT_CENTER_X,
    center_y: float = DEFAULT_CENTER_Y,
    inner_ratio: float = DEFAULT_INNER_RATIO,
    thickness: float = DEFAULT_THICKNESS,
    jitter: float = DEFAULT_JITTER,
    seed: int = DEFAULT_SEED,
    supersample: int = DEFAULT_SUPERSAMPLE,
    area_w: float = DEFAULT_AREA_W,
    area_h: float = DEFAULT_AREA_H,
    width_variance: float = DEFAULT_WIDTH_VARIANCE,
    num_buckets: int = DEFAULT_NUM_BUCKETS,
) -> list[dict]:
    """집중선 각 선을 삼각형(3점)으로 계산한다 (supersample 적용된 픽셀 좌표).

    순수 함수 — PIL 없이도 테스트 가능. 반환: 각 선마다
      { "tri": [(x,y)×3], "base": [(baseA),(baseB)], "apex": (x,y), "bucket": int }
    - tri: 삼각형 채움용 3점 [baseA, baseB, apex].
    - base: 바깥쪽 넓은 밑변 두 점(stroke 제외 변). apex: 중심쪽 뾰족한 꼭짓점.
    - 옆변 stroke = line(apex, baseA) + line(apex, baseB). 밑변(baseA-baseB)엔 stroke 없음.

    각 선:
      - angle: 균등 분할 + 지터. seed 로 재현 가능. 선 사이 간격도 jitter 로 랜덤.
      - 빈 타원 경계(inner_r): 각도 방향으로 x반경 rx, y반경 ry 인 타원까지 거리.
      - 바깥 경계(outer_r): 표시 영역(area 박스) 경계까지.
      - 뾰족한 꼭짓점(apex): inner_r 보다 선마다 제각각 바깥(INNER_STOP_SPREAD 분산).
        빈원은 절대 침범하지 않는다. **anim_t 폐기** — 선은 항상 최대 길이 고정.
      - 밑변 폭: w_out(굵게). 각 선의 w_out 에 seed 기반 ±width_variance 편차.

    area 기본 1.0(전체)이면 정원·최대 길이로 기존 정적 동작과 유사하다.
    """
    ss = max(1, int(supersample))
    W = max(1, int(out_w)) * ss
    H = max(1, int(out_h)) * ss
    n = max(1, int(density))

    cx = _clamp(float(center_x), 0.0, 1.0) * W
    cy = _clamp(float(center_y), 0.0, 1.0) * H

    # 표시 영역(bounding box) — (cx, cy) 중심의 area_w×area_h 사각형. 픽셀 좌표.
    aw = _clamp(float(area_w), 0.02, 1.0)
    ah = _clamp(float(area_h), 0.02, 1.0)
    box_left = cx - (aw * W) / 2.0
    box_right = cx + (aw * W) / 2.0
    box_top = cy - (ah * H) / 2.0
    box_bottom = cy + (ah * H) / 2.0

    # 빈 타원 반경: x/y 를 각각 area 반변에 비례 → area 비율대로 타원.
    ir = _clamp(float(inner_ratio), 0.0, INNER_RATIO_MAX)
    inner_rx = ir * (aw * W / 2.0)
    inner_ry = ir * (ah * H / 2.0)

    # 선 굵기(바깥쪽 밑변) — 짧은 변 대비 비율 → 해상도 무관 일정 두께.
    short_side = min(W, H)
    th = _clamp(float(thickness), 0.05, 5.0)
    w_out = (short_side * 0.010) * th        # 바깥쪽(굵음) 밑변 폭

    def _ray_box_radius(dx: float, dy: float) -> float:
        """(cx, cy) 에서 방향 (dx, dy) 로 뻗을 때 박스 경계까지의 거리(t>=0)."""
        t = math.hypot(W, H) * 1.1
        if dx > 1e-9:
            t = min(t, (box_right - cx) / dx)
        elif dx < -1e-9:
            t = min(t, (box_left - cx) / dx)
        if dy > 1e-9:
            t = min(t, (box_bottom - cy) / dy)
        elif dy < -1e-9:
            t = min(t, (box_top - cy) / dy)
        return max(0.0, t)

    def _ray_ellipse_radius(dx: float, dy: float) -> float:
        """(cx, cy) 에서 방향 (dx, dy) 로 뻗을 때 빈 타원 경계까지의 거리(반경)."""
        if inner_rx <= 1e-9 or inner_ry <= 1e-9:
            return 0.0
        a = (dx / inner_rx) ** 2 + (dy / inner_ry) ** 2
        if a <= 1e-12:
            return 0.0
        return 1.0 / math.sqrt(a)

    jit = _clamp(float(jitter), 0.0, 1.0)
    wv = _clamp(float(width_variance), 0.0, 0.9)
    nb = max(1, int(num_buckets))
    rng = random.Random(int(seed))
    # 선별 끝점 분산 위상 + 버킷 배정 — compute_line_buckets 와 동일 rng 스트림(0x5EED).
    # 이 rng 로 뽑은 phase 로 (a) 끝점 깊이 흩뿌리기 (b) 버킷 배정을 함께 결정한다.
    phase_rng = random.Random(int(seed) ^ 0x5EED)
    line_phase = [phase_rng.random() for _ in range(n)]
    line_bucket = [min(nb - 1, int(p * nb)) for p in line_phase]
    # 끝점 깊이 흩뿌리기용 별도 편차(위상과 독립) — 버킷과 무관하게 깊이가 다양하도록.
    depth_rng = random.Random(int(seed) ^ 0xDEE7)
    depth_spread = [depth_rng.random() for _ in range(n)]

    # 선 사이 간격을 랜덤하게: 각 gap 을 (1 + jitter*U) 가중치로 뽑아 누적·정규화.
    # jitter=0 이면 모든 gap 동일 → 균등 배치(회귀 안전). 별도 rng 로 소비 순서 보존.
    gap_rng = random.Random(int(seed) ^ 0x9A17)
    gaps = [1.0 + jit * gap_rng.random() for _ in range(n)]
    gsum = sum(gaps) or 1.0
    angles: list[float] = []
    acc = 0.0
    for g in gaps:
        angles.append(acc / gsum * 2.0 * math.pi)
        acc += g

    tris: list[dict] = []
    for i in range(n):
        ang = angles[i]
        # 바깥쪽 폭 랜덤 편차: ±width_variance. seed 재현.
        wo = w_out * (1.0 + (rng.random() - 0.5) * 2.0 * wv)

        dx = math.cos(ang)
        dy = math.sin(ang)
        nx = -dy      # 법선(수직) 방향
        ny = dx

        outer_r = _ray_box_radius(dx, dy)
        inner_r = _ray_ellipse_radius(dx, dy)
        if outer_r <= inner_r:
            continue

        # 뾰족한 꼭짓점(apex): inner_r 보다 선마다 제각각 바깥으로 흩어진다.
        # 편차폭 = (outer_r - inner_r) * INNER_STOP_SPREAD. 빈원 절대 미침범.
        span = outer_r - inner_r
        apex_r = inner_r + span * INNER_STOP_SPREAD * depth_spread[i]

        ax = cx + dx * apex_r    # apex (뾰족한 안쪽 끝)
        ay = cy + dy * apex_r
        ox = cx + dx * outer_r   # 밑변 중점 (바깥)
        oy = cy + dy * outer_r

        # 삼각형: 밑변 2점(바깥 ±wo/2) + 꼭짓점 1점(apex).
        base_a = (ox + nx * wo / 2.0, oy + ny * wo / 2.0)
        base_b = (ox - nx * wo / 2.0, oy - ny * wo / 2.0)
        apex = (ax, ay)
        tris.append({
            "tri": [base_a, base_b, apex],
            "base": [base_a, base_b],
            "apex": apex,
            "bucket": line_bucket[i],
        })
    return tris


def generate_speed_lines_png(
    *,
    out_w: int,
    out_h: int,
    color: str = DEFAULT_COLOR,
    density: int = DEFAULT_DENSITY,
    center_x: float = DEFAULT_CENTER_X,
    center_y: float = DEFAULT_CENTER_Y,
    inner_ratio: float = DEFAULT_INNER_RATIO,
    thickness: float = DEFAULT_THICKNESS,
    jitter: float = DEFAULT_JITTER,
    seed: int = DEFAULT_SEED,
    supersample: int = DEFAULT_SUPERSAMPLE,
    area_w: float = DEFAULT_AREA_W,
    area_h: float = DEFAULT_AREA_H,
    width_variance: float = DEFAULT_WIDTH_VARIANCE,
    num_buckets: int = DEFAULT_NUM_BUCKETS,
    phase_bucket: Optional[int] = None,
    cache_dir: Optional[str] = None,
) -> str:
    """집중선 RGBA PNG 를 생성(또는 캐시 재사용)하고 파일 경로를 반환한다.

    - 캐시 hit: (파라미터+해상도+영역+굵기편차+버킷) 해시가 같은 PNG 가 이미 있으면 반환.
    - 캐시 miss: supersample 배 캔버스에 삼각형(채움 + 옆변 stroke)을 그린 뒤 축소 저장.
    - ``phase_bucket`` 이 주어지면 그 버킷에 배정된 선만 그린다(페이드 버킷 PNG).
      None 이면 모든 선을 그린다(정적/animate=False 용).
    - 삼각형 두 옆변(apex→밑변끝)에만 얇은 검은 외곽선을 그린다. 밑변엔 stroke 없음.
    - area_w/area_h < 1.0 이면 (center_x, center_y) 중심 사각형 밖은 투명하게 하드 클립.
    """
    from PIL import Image, ImageDraw

    cdir = Path(cache_dir or SPEED_LINES_CACHE_DIR)
    key = compute_cache_key(
        out_w=out_w, out_h=out_h, color=color, density=density,
        center_x=center_x, center_y=center_y, inner_ratio=inner_ratio,
        thickness=thickness, jitter=jitter, seed=seed, supersample=supersample,
        area_w=area_w, area_h=area_h, width_variance=width_variance,
        num_buckets=num_buckets, phase_bucket=phase_bucket,
    )
    out_path = cdir / f"{key}.png"
    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path)

    cdir.mkdir(parents=True, exist_ok=True)

    ss = max(1, int(supersample))
    W = max(1, int(out_w)) * ss
    H = max(1, int(out_h)) * ss
    r, g, b = _norm_color(color)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    tris = compute_line_triangles(
        out_w=out_w, out_h=out_h, density=density,
        center_x=center_x, center_y=center_y, inner_ratio=inner_ratio,
        thickness=thickness, jitter=jitter, seed=seed, supersample=supersample,
        area_w=area_w, area_h=area_h, width_variance=width_variance,
        num_buckets=num_buckets,
    )
    # 검은 외곽선 두께(고정, supersample 캔버스 기준). 최소 1px.
    stroke_w = max(1, int(round(min(W, H) * STROKE_WIDTH_RATIO)))
    for t in tris:
        if phase_bucket is not None and t["bucket"] != int(phase_bucket):
            continue
        # 1) 삼각형 채움(색).
        draw.polygon(t["tri"], fill=(r, g, b, 255))
        # 2) 두 옆변에만 검은 외곽선. 밑변(base_a-base_b)에는 그리지 않는다.
        apex = t["apex"]
        base_a, base_b = t["base"]
        draw.line([apex, base_a], fill=(0, 0, 0, 255), width=stroke_w)
        draw.line([apex, base_b], fill=(0, 0, 0, 255), width=stroke_w)

    # 다운스케일 → 안티에일리어싱
    if ss > 1:
        try:
            resample = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover
            resample = Image.LANCZOS  # type: ignore[attr-defined]
        img = img.resize((max(1, int(out_w)), max(1, int(out_h))), resample=resample)

    # 표시 영역 하드 클립 — 축소 후 박스 밖 알파 제거(LANCZOS 번짐 방지).
    # area 전체(1.0×1.0)면 클립 무효 → 기존 동작과 동일(회귀 안전).
    aw = _clamp(float(area_w), 0.02, 1.0)
    ah = _clamp(float(area_h), 0.02, 1.0)
    if aw < 1.0 or ah < 1.0:
        OW = max(1, int(out_w))
        OH = max(1, int(out_h))
        cx = _clamp(float(center_x), 0.0, 1.0) * OW
        cy = _clamp(float(center_y), 0.0, 1.0) * OH
        box_left = int(round(cx - (aw * OW) / 2.0))
        box_right = int(round(cx + (aw * OW) / 2.0))
        box_top = int(round(cy - (ah * OH) / 2.0))
        box_bottom = int(round(cy + (ah * OH) / 2.0))
        mask = Image.new("L", (OW, OH), 0)
        ImageDraw.Draw(mask).rectangle(
            [box_left, box_top, box_right, box_bottom], fill=255
        )
        cleared = Image.new("RGBA", (OW, OH), (0, 0, 0, 0))
        img = Image.composite(img, cleared, mask)

    img.save(str(out_path), "PNG")
    return str(out_path)


__all__ = [
    "SPEED_LINES_CACHE_DIR",
    "INNER_RATIO_MAX",
    "INNER_STOP_SPREAD",
    "DEFAULT_WIDTH_VARIANCE",
    "DEFAULT_NUM_BUCKETS",
    "STROKE_WIDTH_RATIO",
    "compute_cache_key",
    "compute_line_buckets",
    "compute_line_triangles",
    "generate_speed_lines_png",
]
