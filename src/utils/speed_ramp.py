"""씬 재생 속도 "가속(ramp)" 모드 공용 수학 헬퍼.

footage_speed_mode:
- "constant": 전 구간 footage_speed 배속 (기존 동작)
- "ramp": 처음 정속(1x) → 선형으로 빨라짐 → footage_speed(S) 도달 후 끝까지 유지

원본 좌표 x(초), 원본 클립 길이 L, 목표 S, a=hold 비율, b=accel 비율(a+b<=1):
- 속도 v(x): x<aL → 1; aL<=x<(a+b)L → 1+(S-1)(x-aL)/(bL); 이후 S
- 출력 시각 T_out(x) = ∫ dx / v(x)
- 출력 길이 = aL + bL·ln(S)/(S-1) + (1-a-b)L/S

- "ramp_down"(감속): 처음 footage_speed(S) 로 빠르게 → 선형 감속 → 정속(1x) 으로 끝까지.
  hold=빠른 구간 비율, accel=감속 구간 비율. (ramp 의 시간 대칭)
  - v(x): x<aL → S; aL<=x<(a+b)L → S-(S-1)(x-aL)/(bL); 이후 1
  - 출력 길이 = aL/S + bL·ln(S)/(S-1) + (1-a-b)L

- footage_speed_ramp_base(B, 기본 1.0): 위의 "1x" 자리를 B 배속으로 바꾼다.
  v(x) = B·v₁(x; S/B) → T(x) = T₁(x; S/B)/B. (기존 배속 위에 추가 배속을 얹을 때 사용)

렌더(clip_editor) / 미리보기(project.py) / RenderSpec 매퍼 / 길이 계산이 모두
이 모듈 하나를 재사용한다. 프론트 대응 구현: web/frontend/src/lib/speedRamp.ts
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_RAMP_HOLD = 0.2
DEFAULT_RAMP_ACCEL = 0.3
_EPS = 1e-6


def normalize_ramp(hold: Any, accel: Any) -> tuple[float, float]:
    """hold/accel 을 0~1 로 클램프하고 hold+accel <= 1 을 보장한다."""
    try:
        a = float(hold)
    except (TypeError, ValueError):
        a = DEFAULT_RAMP_HOLD
    try:
        b = float(accel)
    except (TypeError, ValueError):
        b = DEFAULT_RAMP_ACCEL
    if not math.isfinite(a):
        a = DEFAULT_RAMP_HOLD
    if not math.isfinite(b):
        b = DEFAULT_RAMP_ACCEL
    a = min(1.0, max(0.0, a))
    b = min(1.0 - a, max(0.0, b))
    return a, b


def _accel_factor(S: float) -> float:
    """가속 구간 1초(원본) 가 출력에서 차지하는 비율 = ln(S)/(S-1). S==1 이면 1."""
    if abs(S - 1.0) < _EPS:
        return 1.0
    return math.log(S) / (S - 1.0)


RAMP_MODES = ("ramp", "ramp_down")


def is_ramp_mode(mode: Any) -> bool:
    """가속(ramp) 또는 감속(ramp_down) 모드인가."""
    return mode in RAMP_MODES


def normalize_speed_mode(mode: Any) -> str:
    """constant/ramp/ramp_down 외 값은 constant 로 정규화."""
    return mode if mode in RAMP_MODES else "constant"


def normalize_base(base: Any) -> float:
    """기본 배속(ramp 의 시작 / ramp_down 의 끝 속도). 기본 1.0, 양수만 허용."""
    try:
        B = float(base)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(B) or B <= 0:
        return 1.0
    return B


def _rel(S: float, base: Any) -> tuple[float, float]:
    """기본 배속 B 를 1 로 정규화한 상대 목표 배속 S/B 와 B.

    base 적용 수학: v(x) = B·v₁(x; S/B) → 출력 시각 T(x) = T₁(x; S/B) / B.
    """
    B = normalize_base(base)
    if S <= 0:
        S = 1.0
    return S / B, B


def ramp_output_duration(
    L: float, S: float, hold: float, accel: float, down: bool = False, base: float = 1.0,
) -> float:
    """ramp(가속) / ramp_down(감속) 모드 출력 길이."""
    if L <= 0:
        return 0.0
    S, B = _rel(S, base)
    if abs(B - 1.0) > _EPS:
        return ramp_output_duration(L, S, hold, accel, down) / B
    a, b = normalize_ramp(hold, accel)
    if down:
        return a * L / S + b * L * _accel_factor(S) + (1.0 - a - b) * L
    return a * L + b * L * _accel_factor(S) + (1.0 - a - b) * L / S


def ramp_output_time(
    x: float, L: float, S: float, hold: float, accel: float, down: bool = False, base: float = 1.0,
) -> float:
    """원본 위치 x(초) → 출력 시각(초)."""
    if L <= 0:
        return 0.0
    S, B = _rel(S, base)
    if abs(B - 1.0) > _EPS:
        return ramp_output_time(x, L, S, hold, accel, down) / B
    a, b = normalize_ramp(hold, accel)
    x = min(max(0.0, x), L)
    aL = a * L
    bL = b * L
    if down:
        if x < aL:
            return x / S
        if x < aL + bL and bL > _EPS:
            if abs(S - 1.0) < _EPS:
                return x
            k = (S - 1.0) / bL
            return aL / S + (1.0 / k) * math.log(S / (S - k * (x - aL)))
        return aL / S + bL * _accel_factor(S) + (x - aL - bL)
    if x < aL:
        return x
    if x < aL + bL and bL > _EPS:
        if abs(S - 1.0) < _EPS:
            return x
        return aL + (bL / (S - 1.0)) * math.log(1.0 + (S - 1.0) * (x - aL) / bL)
    return aL + bL * _accel_factor(S) + (x - aL - bL) / S


def ramp_speed_at(
    x: float, L: float, S: float, hold: float, accel: float, down: bool = False, base: float = 1.0,
) -> float:
    """원본 위치 x 에서의 순간 속도 v(x)."""
    if L <= 0 or S <= 0:
        return normalize_base(base)
    S, B = _rel(S, base)
    if abs(B - 1.0) > _EPS:
        return B * ramp_speed_at(x, L, S, hold, accel, down)
    a, b = normalize_ramp(hold, accel)
    aL = a * L
    bL = b * L
    if down:
        if x < aL:
            return S
        if x < aL + bL and bL > _EPS:
            return S - (S - 1.0) * (x - aL) / bL
        return 1.0
    if x < aL:
        return 1.0
    if x < aL + bL and bL > _EPS:
        return 1.0 + (S - 1.0) * (x - aL) / bL
    return S


def scene_speed_params(obj: Any) -> tuple[str, float, float, float]:
    """SceneCard / selection dict 에서 (mode, speed, hold, accel) 을 읽는다."""
    def _get(key: str, default: Any) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    try:
        S = float(_get("footage_speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        S = 1.0
    if not math.isfinite(S) or S <= 0:
        S = 1.0
    mode = normalize_speed_mode(_get("footage_speed_mode", "constant") or "constant")
    a, b = normalize_ramp(
        _get("footage_speed_ramp_hold", DEFAULT_RAMP_HOLD),
        _get("footage_speed_ramp_accel", DEFAULT_RAMP_ACCEL),
    )
    return mode, S, a, b


def scene_speed_base(obj: Any) -> float:
    """SceneCard / selection dict 에서 ramp 기본 배속(footage_speed_ramp_base, 기본 1.0)."""
    v = obj.get("footage_speed_ramp_base", 1.0) if isinstance(obj, dict) else getattr(
        obj, "footage_speed_ramp_base", 1.0)
    return normalize_base(v)


def is_ramp_active(mode: str, S: float, base: float = 1.0) -> bool:
    """ramp/ramp_down 모드이면서 실제로 속도 변화가 있는가 (S==기본 배속이면 정속과 동일)."""
    return is_ramp_mode(mode) and abs(S - normalize_base(base)) > _EPS


def output_duration(
    L: float, mode: str, S: float, hold: float, accel: float, base: float = 1.0,
) -> float:
    """모드에 따른 출력 길이. constant 는 L/S."""
    if S <= 0:
        S = 1.0
    if is_ramp_mode(mode):
        return ramp_output_duration(L, S, hold, accel, down=(mode == "ramp_down"), base=base)
    return L / S


def scene_output_duration(L: float, obj: Any) -> float:
    """SceneCard / selection dict 기준 출력 길이."""
    mode, S, a, b = scene_speed_params(obj)
    return output_duration(L, mode, S, a, b, base=scene_speed_base(obj))


def atempo_chain(rate: float) -> str:
    """기존 규칙과 동일한 atempo 체인 (0.5~2.0 단일, 그 밖은 2단 체이닝)."""
    if 0.5 <= rate <= 2.0:
        return f"atempo={rate:.6f}"
    if rate > 2.0:
        return f"atempo=2.0,atempo={rate / 2.0:.6f}"
    return f"atempo=0.5,atempo={rate / 0.5:.6f}"


def ramp_setpts_expr(
    L: float, S: float, hold: float, accel: float, down: bool = False, base: float = 1.0,
) -> str:
    """setpts 용 piecewise 식 (작은따옴표로 감싼 `setpts='...'` 형태 반환).

    x = (PTS-STARTPTS)*TB 를 원본 위치로 보고 T_out(x)/TB 를 새 PTS 로 쓴다.
    """
    S, B = _rel(S, base)
    if abs(B - 1.0) > _EPS:
        inner = ramp_setpts_expr(L, S, hold, accel, down)  # "setpts='(EXPR)/TB'"
        expr = inner[len("setpts='"):-len("/TB'")]
        return f"setpts='({expr}/{B:.6f})/TB'"
    a, b = normalize_ramp(hold, accel)
    aL = a * L
    bL = b * L
    x = "((PTS-STARTPTS)*TB)"
    k_acc = _accel_factor(S)
    if down:
        fast_end_out = aL / S
        decel_end_out = fast_end_out + bL * k_acc
        tail = f"({decel_end_out:.6f}+({x}-{aL + bL:.6f}))"
        head = f"({x}/{S:.6f})"
        if bL > _EPS and abs(S - 1.0) > _EPS:
            k = (S - 1.0) / bL
            decel_expr = (
                f"({fast_end_out:.6f}+{1.0 / k:.6f}*log({S:.6f}/({S:.6f}-{k:.6f}*({x}-{aL:.6f}))))"
            )
            expr = f"if(lt({x},{aL:.6f}),{head},if(lt({x},{aL + bL:.6f}),{decel_expr},{tail}))"
        else:
            expr = f"if(lt({x},{aL:.6f}),{head},{tail})"
        return f"setpts='({expr})/TB'"
    hold_end_out = aL + bL * k_acc
    tail = f"({hold_end_out:.6f}+({x}-{aL + bL:.6f})/{S:.6f})"
    if bL > _EPS and abs(S - 1.0) > _EPS:
        accel_expr = (
            f"({aL:.6f}+{bL / (S - 1.0):.6f}*log(1+{(S - 1.0) / bL:.6f}*({x}-{aL:.6f})))"
        )
        expr = f"if(lt({x},{aL:.6f}),{x},if(lt({x},{aL + bL:.6f}),{accel_expr},{tail}))"
    else:
        expr = f"if(lt({x},{aL:.6f}),{x},{tail})"
    return f"setpts='({expr})/TB'"


def ramp_audio_pieces(
    L: float, S: float, hold: float, accel: float, piece_sec: float = 0.5,
    down: bool = False, base: float = 1.0,
) -> list[tuple[float, float, float]]:
    """오디오 계단식 조각 [(src_start, src_end, tempo)].

    가속 구간은 약 piece_sec(원본 기준) 조각으로 나누고, 각 조각의 tempo 는
    (원본 길이)/(T_out 차이) 로 잡아 조각 합계가 영상 출력 길이와 정확히 일치하게 한다
    (조각 내 평균 속도 = 중간 속도 근사).
    """
    if L <= 0:
        return []
    S, B = _rel(S, base)
    if abs(B - 1.0) > _EPS:
        return [(s0, e0, t0 * B) for (s0, e0, t0) in ramp_audio_pieces(L, S, hold, accel, piece_sec, down)]
    a, b = normalize_ramp(hold, accel)
    aL = a * L
    bL = b * L
    pieces: list[tuple[float, float, float]] = []
    if aL > 1e-3:
        pieces.append((0.0, aL, S if down else 1.0))
    if bL > 1e-3:
        n = max(1, int(math.ceil(bL / max(0.05, piece_sec))))
        step = bL / n
        for i in range(n):
            x0 = aL + i * step
            x1 = aL + (i + 1) * step if i < n - 1 else aL + bL
            out = (ramp_output_time(x1, L, S, a, b, down=down)
                   - ramp_output_time(x0, L, S, a, b, down=down))
            tempo = (x1 - x0) / out if out > _EPS else 1.0
            pieces.append((x0, x1, tempo))
    if L - (aL + bL) > 1e-3:
        pieces.append((aL + bL, L, 1.0 if down else S))
    return pieces


def ramp_audio_filter(
    L: float, S: float, hold: float, accel: float, piece_sec: float = 0.5,
    down: bool = False, base: float = 1.0,
) -> str:
    """단일 -af 필터그래프(asplit → atrim/asetpts/atempo → concat).

    입력 오디오의 0~L 구간 기준. 앞단 필터는 호출자가 `{prefix},` + 반환값 형태로 붙인다.
    """
    pieces = ramp_audio_pieces(L, S, hold, accel, piece_sec, down=down, base=base)
    if not pieces:
        return "anull"
    n = len(pieces)
    if n == 1:
        s0, e0, t0 = pieces[0]
        f = f"atrim=start={s0:.6f}:end={e0:.6f},asetpts=PTS-STARTPTS"
        if abs(t0 - 1.0) > _EPS:
            f += "," + atempo_chain(t0)
        return f
    split_labels = "".join(f"[rs{i}]" for i in range(n))
    parts = [f"asplit={n}{split_labels}"]
    for i, (s0, e0, t0) in enumerate(pieces):
        chain = f"[rs{i}]atrim=start={s0:.6f}:end={e0:.6f},asetpts=PTS-STARTPTS"
        if abs(t0 - 1.0) > _EPS:
            chain += "," + atempo_chain(t0)
        chain += f"[ro{i}]"
        parts.append(chain)
    parts.append("".join(f"[ro{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1")
    return ";".join(parts)
