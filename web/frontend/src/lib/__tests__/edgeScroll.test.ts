/**
 * 드래그 자동 스크롤 속도 계산 — edgeScrollSpeed 순수 함수 검증.
 *
 * 정책: 뷰포트 상단 zone px 안 → 위로(음수), 하단 zone px 안 → 아래로(양수),
 *   존 밖 → 0. 가장자리에 가까울수록 절댓값 최대(maxSpeed).
 */
import { describe, expect, it } from "vitest";
import { edgeScrollSpeed, MAX_EDGE_SCROLL_SPEED } from "../edgeScroll";

const VH = 800;
const ZONE = 80;

describe("edgeScrollSpeed", () => {
  it("존 밖(화면 중앙)에서는 0 을 반환한다", () => {
    expect(edgeScrollSpeed(400, VH, ZONE)).toBe(0);
    // 존 경계 바로 안쪽이 아닌 지점.
    expect(edgeScrollSpeed(ZONE + 1, VH, ZONE)).toBe(0);
    expect(edgeScrollSpeed(VH - ZONE - 1, VH, ZONE)).toBe(0);
  });

  it("상단 존에서는 음수(위로 스크롤)를 반환한다", () => {
    expect(edgeScrollSpeed(40, VH, ZONE)).toBeLessThan(0);
  });

  it("하단 존에서는 양수(아래로 스크롤)를 반환한다", () => {
    expect(edgeScrollSpeed(VH - 40, VH, ZONE)).toBeGreaterThan(0);
  });

  it("가장자리(맨 위/맨 아래)에서 최대 속도에 도달한다", () => {
    // 맨 위(0) → -maxSpeed, 맨 아래(VH) → +maxSpeed.
    expect(edgeScrollSpeed(0, VH, ZONE)).toBeCloseTo(-MAX_EDGE_SCROLL_SPEED);
    expect(edgeScrollSpeed(VH, VH, ZONE)).toBeCloseTo(MAX_EDGE_SCROLL_SPEED);
  });

  it("비정상 입력(뷰포트 높이 0/NaN)은 0 을 반환한다", () => {
    expect(edgeScrollSpeed(10, 0, ZONE)).toBe(0);
    expect(edgeScrollSpeed(NaN, VH, ZONE)).toBe(0);
  });
});
