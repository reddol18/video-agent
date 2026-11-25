import { describe, it, expect } from "vitest";
import { sfxCrossed, SFX_MAX_LATE_SEC } from "../OverlayCanvas";

describe("sfxCrossed — 미리보기 SFX 발화 판정", () => {
  it("첫 tick 이 늦게 와도(0 → 0.917) start 0.3 을 지났으면 울린다 — 이어보기 1번 씬 회귀", () => {
    expect(sfxCrossed(0, 0.917, 0.3)).toBe(true);
  });
  it("start 0 은 재생 시작 첫 tick 에 울린다", () => {
    expect(sfxCrossed(0, 0.25, 0)).toBe(true);
  });
  it("아직 오지 않은 시작점은 울리지 않는다", () => {
    expect(sfxCrossed(0, 0.25, 0.3)).toBe(false);
  });
  it("이미 지난 구간의 시작점은 다시 울리지 않는다", () => {
    expect(sfxCrossed(1.0, 1.25, 0.3)).toBe(false);
  });
  it("너무 늦게 알아차리면 울리지 않는다", () => {
    expect(sfxCrossed(0, 0.3 + SFX_MAX_LATE_SEC + 0.1, 0.3)).toBe(false);
  });
  it("2배속처럼 tick 간격이 넓어도 그 사이 시작점은 잡는다", () => {
    expect(sfxCrossed(1.1, 1.6, 1.2)).toBe(true);
  });
});
