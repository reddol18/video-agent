/**
 * #narrow-scene-block — 좁은 씬 블록의 씬 번호 표시 규칙.
 *
 * 실제 사고: 씬 53(2.16s)처럼 짧은 씬은 타임라인에서 약 9px 폭으로 그려지는데,
 * sceneBlock 의 좌우 padding(4+4) + border(1+1) = 10px 가 내용 전에 소비된다.
 * 그래서 씬 번호가 블록 밖으로 흘러 이웃 씬(54)과 겹쳐 보였고,
 * 사용자는 "씬 53, 54 가 겹친다" 고 인식했다.
 *
 * 규칙: 블록 실제 폭이 NUM_MIN_PX 미만이면 숫자를 그리지 않는다.
 * 단 트랙 폭 측정 전(0)에는 기존 동작대로 항상 그린다.
 */
import { describe, expect, it } from "vitest";

const NUM_MIN_PX = 22;

/** TimelineBar 의 렌더 조건과 동일한 판정.
 *  trackPx 는 "보이는 폭"(zoom 무관), 실제 블록 폭은 zoom 을 곱해 구한다. */
function showsSceneNum(trackPx: number, widthPct: number, zoom = 1): boolean {
  return trackPx <= 0 || trackPx * zoom * (widthPct / 100) >= NUM_MIN_PX;
}

describe("좁은 씬 블록의 씬 번호 표시", () => {
  it("측정 전(trackPx=0)에는 항상 표시한다", () => {
    expect(showsSceneNum(0, 0.55)).toBe(true);
  });

  it("씬 53 재현: 1600px 트랙에서 0.55% 폭(≈8.8px)이면 숨긴다", () => {
    expect(showsSceneNum(1600, 0.55)).toBe(false);
  });

  it("씬 54 재현: 1.50% 폭(=24px)이면 표시한다", () => {
    expect(showsSceneNum(1600, 1.5)).toBe(true);
  });

  it("경계값: 정확히 NUM_MIN_PX 면 표시한다", () => {
    const pct = (NUM_MIN_PX / 1600) * 100;
    expect(showsSceneNum(1600, pct)).toBe(true);
  });

  it("경계값 바로 아래는 숨긴다", () => {
    const pct = ((NUM_MIN_PX - 0.5) / 1600) * 100;
    expect(showsSceneNum(1600, pct)).toBe(false);
  });

  it("트랙이 넓어지면 같은 비율도 표시로 바뀐다", () => {
    expect(showsSceneNum(1200, 1.6)).toBe(false); // 19.2px
    expect(showsSceneNum(2000, 1.6)).toBe(true);  // 32px
  });
});

/**
 * 블록 사이 시각적 간격 — 겹쳐 보이던 진짜 원인.
 *
 * 씬 블록은 left/width 로 정확히 맞닿게(간격 0) 배치된다. 그 상태에서는
 * 이웃 블록의 1px 테두리와 borderRadius 둥근 모서리가 같은 픽셀에 그려져
 * 짧은 씬일수록 두 씬이 하나로 뭉쳐 보인다.
 */
const SCENE_GAP_PX = 2;
const MIN_BLOCK_PX = 3;

/** TimelineBar 의 inline width 계산과 동일. */
function blockWidthPx(trackPx: number, widthPct: number): number {
  return Math.max(MIN_BLOCK_PX, (widthPct / 100) * trackPx - SCENE_GAP_PX);
}

describe("씬 블록 사이 간격", () => {
  const TRACK = 1600;

  it("이웃 블록과 겹치지 않는다 (씬 53→54 실측)", () => {
    const left53 = 1010.39, w53pct = 0.555;
    const left54 = 1019.26;
    const right53 = left53 + blockWidthPx(TRACK, w53pct);
    expect(right53).toBeLessThan(left54);
  });

  it("맞닿은 블록에도 최소 간격이 생긴다", () => {
    const widthPct = 1.5;
    const left = 100;
    const nextLeft = left + (widthPct / 100) * TRACK; // 원래는 정확히 맞닿음
    const right = left + blockWidthPx(TRACK, widthPct);
    expect(nextLeft - right).toBeCloseTo(SCENE_GAP_PX, 5);
  });

  it("아주 짧은 씬도 사라지지 않는다", () => {
    expect(blockWidthPx(TRACK, 0.01)).toBeGreaterThanOrEqual(MIN_BLOCK_PX);
  });
});

/** #timeline-zoom — 타임라인 가로 확대/축소. */
import { clampZoom } from "../TimelineBar";

describe("타임라인 확대/축소", () => {
  it("기본 배율은 1(화면 폭에 맞춤)", () => {
    expect(clampZoom(1)).toBe(1);
  });

  it("1배 미만으로는 축소되지 않는다", () => {
    expect(clampZoom(0.2)).toBe(1);
  });

  it("상한(16배)을 넘지 않는다", () => {
    expect(clampZoom(100)).toBe(16);
  });

  it("확대하면 짧은 씬도 번호를 담을 만큼 넓어진다", () => {
    const w53pct = 0.555;
    // 1배: 8.9px → 숫자 숨김
    expect(showsSceneNum(1600, w53pct, 1)).toBe(false);
    // 4배 확대: 실제 블록 폭 35.5px → 숫자 표시
    expect(showsSceneNum(1600, w53pct, 4)).toBe(true);
  });

  it("확대 시 보이는 폭(trackPx)은 그대로여도 번호가 살아난다", () => {
    // 회귀: trackPx 를 안쪽(확대된) 트랙에서 재면 zoom 이 이중 반영되거나,
    // ResizeObserver 가 늦게 발화해 확대 직후 번호가 사라져 보였다.
    const w = 0.555;
    expect(showsSceneNum(1600, w, 1)).toBe(false);
    expect(showsSceneNum(1600, w, 2)).toBe(false);   // 17.8px, 아직 부족
    expect(showsSceneNum(1600, w, 3)).toBe(true);    // 26.6px
  });

  it("확대해도 블록 간 겹침은 없다", () => {
    const TRACK = 1600 * 4;
    const widthPct = 0.555;
    const left = 0.63 * TRACK;
    const nextLeft = left + (widthPct / 100) * TRACK;
    expect(left + blockWidthPx(TRACK, widthPct)).toBeLessThan(nextLeft);
  });
});
