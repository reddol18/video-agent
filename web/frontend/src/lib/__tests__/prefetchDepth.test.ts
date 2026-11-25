/**
 * 이어보기 적응형 프리페치 깊이 산출 검증.
 *
 * 핵심 요구: 고정 개수가 아니라 "누적 재생시간이 예상 로딩시간을 덮을 때까지만" 앞서 받는다.
 * 짧은 씬이 연속되면 깊이가 늘고, 긴 씬이면 1개, 로딩이 느린 환경(캐시 미스)에서는 더 깊이.
 */
import { describe, expect, it } from "vitest";
import {
  computePrefetchDepth,
  updateLoadEstimate,
  PREFETCH_MAX_DEPTH,
  PREFETCH_GIVEUP_SEC,
} from "../prefetchDepth";

describe("computePrefetchDepth", () => {
  it("① 긴 씬(10초+)이 연속되면 로딩 속도와 무관하게 깊이 1", () => {
    // 12초 씬은 그 한 개만 버퍼에 있어도 최악(2.3s 미스)의 로딩을 여유 있게 덮는다.
    // 더 받아 봐야 전환 공백이 줄지 않고 서버 ffmpeg 큐만 늘어난다.
    for (const est of [0.12, 0.6, 1.5, 2.3]) {
      const depth = computePrefetchDepth({
        upcomingDurations: [12, 15, 11, 14],
        currentRemainingSec: 12,
        estLoadSec: est,
        playbackRate: 1,
      });
      expect(depth).toBe(1);
    }
  });

  it("② 짧은 씬(2초)이 연속되면 깊이 2 이상 — 프리페치가 실제로 필요한 구간", () => {
    // est 0.12s(캐시 전량 적중)에서는 2초 버퍼만으로도 충분하므로 깊이 1 이 맞다.
    // 프리페치가 의미 있는 구간(로딩이 1초 이상)에서 깊이가 2 이상으로 올라가야 한다.
    for (const est of [1.0, 1.5, 2.3]) {
      const depth = computePrefetchDepth({
        upcomingDurations: [2, 2, 2, 2, 2, 2, 2, 2],
        currentRemainingSec: 2,
        estLoadSec: est,
        playbackRate: 1,
      });
      expect(depth).toBeGreaterThanOrEqual(2);
    }

    // 같은 로딩 조건에서 짧은 씬이 긴 씬보다 얕아서는 안 된다.
    const shortDepth = computePrefetchDepth({
      upcomingDurations: [2, 2, 2, 2, 2, 2],
      currentRemainingSec: 2,
      estLoadSec: 1.5,
    });
    const longDepth = computePrefetchDepth({
      upcomingDurations: [30, 30, 30, 30, 30, 30],
      currentRemainingSec: 30,
      estLoadSec: 1.5,
    });
    expect(shortDepth).toBeGreaterThan(longDepth);
  });

  it("③ 로딩 추정치가 크면(캐시 미스) 같은 씬 구성에서 깊이가 단조 증가", () => {
    const durations = [2, 2, 2, 2, 2, 2, 2, 2];
    const depths = [0.12, 0.6, 1.0, 1.5, 2.3, 4.0].map((est) =>
      computePrefetchDepth({
        upcomingDurations: durations,
        currentRemainingSec: 2,
        estLoadSec: est,
        playbackRate: 1,
      }),
    );
    // 느려질수록 깊이가 줄어드는 구간이 있으면 안 된다(단조 비감소).
    for (let i = 1; i < depths.length; i++) {
      expect(depths[i]).toBeGreaterThanOrEqual(depths[i - 1]);
    }
    // 그리고 실제로 증가해야 한다 — 상수 함수면 "적응형"이 아니다.
    expect(depths[depths.length - 1]).toBeGreaterThan(depths[0]);
  });

  it("배속이 오르면 벌 수 있는 벽시계 시간이 줄어 깊이가 늘거나 같다", () => {
    const durations = [3, 3, 3, 3, 3, 3];
    const at1x = computePrefetchDepth({
      upcomingDurations: durations, currentRemainingSec: 3, estLoadSec: 0.6, playbackRate: 1,
    });
    const at3x = computePrefetchDepth({
      upcomingDurations: durations, currentRemainingSec: 3, estLoadSec: 0.6, playbackRate: 3,
    });
    // 3배속이면 3초 씬이 실제로는 1초만 벌어 주므로 더 깊은 버퍼가 필요하다.
    expect(at3x).toBeGreaterThanOrEqual(at1x);
  });

  it("④ 상한을 넘지 않는다 (메모리 보호)", () => {
    // 아주 짧은 씬 + 느린 로딩 = 깊이가 무한정 늘어나려는 조건.
    // 이때도 상한에서 잘려야 한다.
    // estLoadSec 은 포기 임계값(PREFETCH_GIVEUP_SEC) 아래로 둔다 — 그 위는
    // 깊이 1 로 고정되는 별도 정책이라 상한 검증이 무의미해진다(아래 ⑤ 참고).
    const depth = computePrefetchDepth({
      upcomingDurations: new Array(50).fill(0.5),
      currentRemainingSec: 0.5,
      estLoadSec: 2.0,
      playbackRate: 3,
    });
    expect(depth).toBe(PREFETCH_MAX_DEPTH);

    // 명시적 maxDepth 도 존중.
    const capped = computePrefetchDepth({
      upcomingDurations: new Array(50).fill(0.5),
      currentRemainingSec: 0.5,
      estLoadSec: 2.0,
      playbackRate: 3,
      maxDepth: 2,
    });
    expect(capped).toBe(2);
  });

  it("⑤ 로딩이 포기 임계값을 넘으면 깊이 1 로 고정 (서버 큐 보호)", () => {
    // 실측: 캐시 미스가 12.9 / 27.9 / 72.5초까지 나온다. 이 구간에서 깊이를 늘리면
    // 공백도 못 메우면서 서버 ffmpeg 큐만 밀려 현재 씬 요청이 뒤로 밀린다.
    for (const est of [PREFETCH_GIVEUP_SEC, 12.9, 72.5]) {
      const depth = computePrefetchDepth({
        upcomingDurations: new Array(50).fill(0.5),
        currentRemainingSec: 0.5,
        estLoadSec: est,
        playbackRate: 3,
      });
      expect(depth).toBe(1);
    }

    // 남은 씬이 없으면 0 (포기 경로에서도 범위를 벗어나지 않는다).
    expect(
      computePrefetchDepth({ upcomingDurations: [], estLoadSec: 72.5 }),
    ).toBe(0);
  });

  it("남은 씬이 없으면 0 — 마지막 씬에서 불필요한 요청을 만들지 않는다", () => {
    expect(computePrefetchDepth({ upcomingDurations: [], currentRemainingSec: 10 })).toBe(0);
  });

  it("남은 씬이 1개뿐이면 깊이가 1을 넘지 않는다", () => {
    const depth = computePrefetchDepth({
      upcomingDurations: [2],
      currentRemainingSec: 999,
      estLoadSec: 0.05,
    });
    expect(depth).toBe(1);
  });

  it("남은 씬이 있으면 항상 최소 1개는 받는다 (지연 도착도 공백을 줄인다)", () => {
    const depth = computePrefetchDepth({
      upcomingDurations: [5, 5],
      currentRemainingSec: 0,
      estLoadSec: 5,
    });
    expect(depth).toBeGreaterThanOrEqual(1);
  });

  it("비정상 입력(NaN/음수)에 방어적", () => {
    const depth = computePrefetchDepth({
      upcomingDurations: [NaN, -3, 2],
      currentRemainingSec: NaN,
      estLoadSec: -1,
      playbackRate: 0,
    });
    expect(depth).toBeGreaterThanOrEqual(1);
    expect(depth).toBeLessThanOrEqual(PREFETCH_MAX_DEPTH);
  });
});

describe("updateLoadEstimate", () => {
  it("첫 표본은 그대로 채택", () => {
    expect(updateLoadEstimate(null, 2.3)).toBeCloseTo(2.3, 5);
  });

  it("EMA 로 최근 표본을 따라간다 (캐시 미스 → 적중 전환 추적)", () => {
    let est = updateLoadEstimate(null, 2.3);
    for (let i = 0; i < 10; i++) est = updateLoadEstimate(est, 0.12);
    expect(est).toBeLessThan(0.5);
  });

  it("음수/NaN 표본은 0 으로 취급해 폭주하지 않는다", () => {
    const est = updateLoadEstimate(1.0, NaN);
    expect(Number.isFinite(est)).toBe(true);
    expect(est).toBeGreaterThanOrEqual(0);
  });
});
