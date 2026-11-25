/**
 * #381 — studio_assets dedup 가드 단위 테스트.
 *
 * `dedupeStudioAssets` 는 asset_id 가 같은 항목을 한 개로 정리해야 한다.
 * - 빈 배열: 그대로 빈 배열.
 * - 중복 없음: 입력과 동일한 순서/내용.
 * - 같은 asset_id 가 두 번 등장: 첫 항목 위치를 유지하고 한 개만 남음.
 * - 첫 항목의 label/crop 이 비어 있으면 후속 항목 값으로 보완(보수적 머지).
 * - 잘못된 입력(asset_id 누락 등)은 조용히 스킵.
 */
import { describe, expect, it } from "vitest";
import { dedupeStudioAssets } from "../project";
import type { FootageAsset } from "../../types";

function asset(
  id: string,
  overrides: Partial<FootageAsset> = {}
): FootageAsset {
  return {
    asset_id: id,
    filename: `${id}.mp4`,
    label: id,
    asset_type: "video",
    source: "imported",
    created_at: "2026-06-18T00:00:00Z",
    ...overrides,
  };
}

describe("#381 dedupeStudioAssets", () => {
  it("빈 배열은 그대로 반환한다", () => {
    expect(dedupeStudioAssets([])).toEqual([]);
  });

  it("중복이 없으면 입력과 동일한 순서/길이로 반환한다", () => {
    const input = [asset("a"), asset("b"), asset("c")];
    const out = dedupeStudioAssets(input);
    expect(out).toHaveLength(3);
    expect(out.map((a) => a.asset_id)).toEqual(["a", "b", "c"]);
  });

  it("동일 asset_id 가 2번 들어와도 1개로 정리된다 (#381 핵심 회귀 방지)", () => {
    // paste 버그 재현: 같은 asset 이 연달아 두 번 등록된 상황.
    const dup = asset("X");
    const out = dedupeStudioAssets([asset("a"), dup, asset("b"), dup]);
    expect(out.map((a) => a.asset_id)).toEqual(["a", "X", "b"]);
  });

  it("첫 항목 위치를 유지한다(첫 등장 순서 보존)", () => {
    const out = dedupeStudioAssets([
      asset("a"),
      asset("b"),
      asset("a"),
      asset("c"),
    ]);
    expect(out.map((a) => a.asset_id)).toEqual(["a", "b", "c"]);
  });

  it("첫 항목 label 이 비어 있으면 후속 항목 label 로 보완한다", () => {
    const out = dedupeStudioAssets([
      asset("a", { label: "" }),
      asset("a", { label: "복구된 라벨" }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].label).toBe("복구된 라벨");
  });

  it("첫 항목 crop 이 null 이면 후속 항목 crop 으로 보완한다", () => {
    const crop = { x: 0, y: 0, width: 1, height: 1 };
    const out = dedupeStudioAssets([
      asset("a", { crop: null }),
      asset("a", { crop }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].crop).toEqual(crop);
  });

  it("첫 항목에 값이 있으면 후속 항목 값으로 덮어쓰지 않는다", () => {
    const out = dedupeStudioAssets([
      asset("a", { label: "원본" }),
      asset("a", { label: "다른 값" }),
    ]);
    expect(out[0].label).toBe("원본");
  });

  it("asset_id 가 없거나 잘못된 항목은 조용히 스킵한다", () => {
    const broken = { filename: "x.mp4" } as unknown as FootageAsset;
    const out = dedupeStudioAssets([asset("a"), broken, asset("b")]);
    expect(out.map((a) => a.asset_id)).toEqual(["a", "b"]);
  });
});
