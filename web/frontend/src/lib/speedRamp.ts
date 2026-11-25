/**
 * 씬 재생 속도 "가속(ramp)" 모드 공용 수학 헬퍼 (프론트 단일 구현).
 * 백엔드 대응 구현: src/utils/speed_ramp.py — 공식이 반드시 같아야 한다.
 *
 * footage_speed_mode:
 *   - "constant": 전 구간 footage_speed 배속 (기존 동작)
 *   - "ramp": 처음 정속(1x) → 선형 가속 → footage_speed(S) 도달 후 끝까지 유지
 *
 * 원본 좌표 x(초), 원본 길이 L, a=hold, b=accel (a+b<=1):
 *   v(x): x<aL → 1; aL<=x<(a+b)L → 1+(S-1)(x-aL)/(bL); 이후 S
 *   출력 길이 = aL + bL·ln(S)/(S-1) + (1-a-b)L/S
 */
import type { SceneCard } from "../types";

/**
 * "ramp_down"(감속): 처음 S 로 빠르게 → 선형 감속 → 정속(1x) 으로 끝까지. hold=빠른 구간, accel=감속 구간.
 *   v(x): x<aL → S; aL<=x<(a+b)L → S-(S-1)(x-aL)/(bL); 이후 1
 *   출력 길이 = aL/S + bL·ln(S)/(S-1) + (1-a-b)L
 */
export type FootageSpeedMode = "constant" | "ramp" | "ramp_down";

export function isRampMode(mode: FootageSpeedMode): boolean {
  return mode === "ramp" || mode === "ramp_down";
}

export interface SpeedSpec {
  mode: FootageSpeedMode;
  speed: number;
  hold: number;
  accel: number;
  /**
   * ramp/ramp_down 의 기본 배속 B(기본 1) — 가속의 시작 / 감속의 끝 속도.
   * v(x) = B·v₁(x; S/B) → T(x) = T₁(x; S/B)/B. 백엔드 footage_speed_ramp_base 와 동일.
   */
  base?: number;
}

/** 기본 배속을 1 로 정규화한 spec 과 B. B==1 이면 null. */
function relSpec(spec: SpeedSpec): [SpeedSpec, number] | null {
  const B = typeof spec.base === "number" && isFinite(spec.base) && spec.base > 0 ? spec.base : 1;
  if (!isRampMode(spec.mode) || Math.abs(B - 1) < 1e-6) return null;
  const S = spec.speed > 0 ? spec.speed : 1;
  return [{ ...spec, speed: S / B, base: 1 }, B];
}

export const DEFAULT_RAMP_HOLD = 0.2;
export const DEFAULT_RAMP_ACCEL = 0.3;
const EPS = 1e-6;

export function normalizeRamp(hold: unknown, accel: unknown): [number, number] {
  let a = typeof hold === "number" && isFinite(hold) ? hold : DEFAULT_RAMP_HOLD;
  let b = typeof accel === "number" && isFinite(accel) ? accel : DEFAULT_RAMP_ACCEL;
  a = Math.min(1, Math.max(0, a));
  b = Math.min(1 - a, Math.max(0, b));
  return [a, b];
}

/** SceneCard(또는 그 부분 patch 를 덮어쓴 값) 에서 SpeedSpec 을 읽는다. */
export function getSceneSpeedSpec(scene: Partial<SceneCard>): SpeedSpec {
  const raw = scene.footage_speed;
  const speed = typeof raw === "number" && isFinite(raw) && raw > 0 ? raw : 1.0;
  const rawMode = scene.footage_speed_mode;
  const mode: FootageSpeedMode = rawMode === "ramp" || rawMode === "ramp_down" ? rawMode : "constant";
  const [hold, accel] = normalizeRamp(scene.footage_speed_ramp_hold, scene.footage_speed_ramp_accel);
  const rawBase = scene.footage_speed_ramp_base;
  const base = typeof rawBase === "number" && isFinite(rawBase) && rawBase > 0 ? rawBase : 1.0;
  return { mode, speed, hold, accel, base };
}

/** ramp 모드이면서 실제 속도 변화가 있는가 (S==기본 배속이면 정속과 동일). */
export function isRampActive(spec: SpeedSpec): boolean {
  const B = spec.base && spec.base > 0 ? spec.base : 1;
  return isRampMode(spec.mode) && Math.abs(spec.speed - B) > EPS;
}

function accelFactor(S: number): number {
  return Math.abs(S - 1) < EPS ? 1 : Math.log(S) / (S - 1);
}

/** 원본 길이 L → 출력 길이. constant 는 L/S. */
export function speedOutputDuration(L: number, spec: SpeedSpec): number {
  if (!(L > 0)) return 0;
  const rel = relSpec(spec);
  if (rel) return speedOutputDuration(L, rel[0]) / rel[1];
  const S = spec.speed > 0 ? spec.speed : 1;
  if (!isRampMode(spec.mode)) return L / S;
  const [a, b] = normalizeRamp(spec.hold, spec.accel);
  if (spec.mode === "ramp_down") return (a * L) / S + b * L * accelFactor(S) + (1 - a - b) * L;
  return a * L + b * L * accelFactor(S) + ((1 - a - b) * L) / S;
}

/** 원본 위치 x → 출력 시각. */
export function sourceToOutputTime(x: number, L: number, spec: SpeedSpec): number {
  const rel = relSpec(spec);
  if (rel && L > 0) return sourceToOutputTime(x, L, rel[0]) / rel[1];
  const S = spec.speed > 0 ? spec.speed : 1;
  if (!isRampMode(spec.mode) || !(L > 0)) return x / S;
  const [a, b] = normalizeRamp(spec.hold, spec.accel);
  const aL = a * L;
  const bL = b * L;
  const xc = Math.min(Math.max(0, x), L);
  if (spec.mode === "ramp_down") {
    if (xc < aL) return xc / S;
    if (xc < aL + bL && bL > EPS) {
      if (Math.abs(S - 1) < EPS) return xc;
      const k = (S - 1) / bL;
      return aL / S + (1 / k) * Math.log(S / (S - k * (xc - aL)));
    }
    return aL / S + bL * accelFactor(S) + (xc - aL - bL);
  }
  if (xc < aL) return xc;
  if (xc < aL + bL && bL > EPS) {
    if (Math.abs(S - 1) < EPS) return xc;
    return aL + (bL / (S - 1)) * Math.log(1 + ((S - 1) * (xc - aL)) / bL);
  }
  return aL + bL * accelFactor(S) + (xc - aL - bL) / S;
}

/** 출력 시각 t → 원본 위치 x (sourceToOutputTime 의 역함수). */
export function outputToSourceTime(t: number, L: number, spec: SpeedSpec): number {
  const S = spec.speed > 0 ? spec.speed : 1;
  const rel = relSpec(spec);
  if (rel && L > 0) return outputToSourceTime(t * rel[1], L, rel[0]);
  if (!isRampMode(spec.mode) || !(L > 0)) return t * S;
  const [a, b] = normalizeRamp(spec.hold, spec.accel);
  const aL = a * L;
  const bL = b * L;
  const tc = Math.max(0, t);
  if (spec.mode === "ramp_down") {
    const fastOutEnd = aL / S;
    if (tc < fastOutEnd) return tc * S;
    const decelOutEnd = fastOutEnd + bL * accelFactor(S);
    if (tc < decelOutEnd && bL > EPS) {
      if (Math.abs(S - 1) < EPS) return tc;
      const k = (S - 1) / bL;
      return aL + (S / k) * (1 - Math.exp(-(tc - fastOutEnd) * k));
    }
    return aL + bL + (tc - decelOutEnd);
  }
  if (tc < aL) return tc;
  const accelOutEnd = aL + bL * accelFactor(S);
  if (tc < accelOutEnd && bL > EPS) {
    if (Math.abs(S - 1) < EPS) return tc;
    return aL + (bL / (S - 1)) * (Math.exp(((tc - aL) * (S - 1)) / bL) - 1);
  }
  return aL + bL + (tc - accelOutEnd) * S;
}

/** 원본 위치 x 의 순간 속도 v(x). */
export function speedAtSource(x: number, L: number, spec: SpeedSpec): number {
  const S = spec.speed > 0 ? spec.speed : 1;
  const rel = relSpec(spec);
  if (rel && L > 0) return rel[1] * speedAtSource(x, L, rel[0]);
  if (!isRampMode(spec.mode) || !(L > 0)) return S;
  const [a, b] = normalizeRamp(spec.hold, spec.accel);
  const aL = a * L;
  const bL = b * L;
  if (spec.mode === "ramp_down") {
    if (x < aL) return S;
    if (x < aL + bL && bL > EPS) return S - ((S - 1) * (x - aL)) / bL;
    return 1;
  }
  if (x < aL) return 1;
  if (x < aL + bL && bL > EPS) return 1 + ((S - 1) * (x - aL)) / bL;
  return S;
}

/**
 * 출력 길이가 target 이 되는 S 를 이분탐색으로 구한다 (ramp 출력 길이는 S 에 단조감소).
 * [lo, hi] 범위 밖이면 경계로 클램프.
 */
export function solveRampSpeedForDuration(
  L: number,
  target: number,
  hold: number,
  accel: number,
  lo: number,
  hi: number,
  mode: FootageSpeedMode = "ramp",
  base = 1,
): number {
  const f = (S: number) => speedOutputDuration(L, { mode, speed: S, hold, accel, base });
  if (f(lo) <= target) return lo;
  if (f(hi) >= target) return hi;
  let l = lo;
  let h = hi;
  for (let i = 0; i < 60; i++) {
    const m = (l + h) / 2;
    if (f(m) > target) l = m;
    else h = m;
  }
  return (l + h) / 2;
}
