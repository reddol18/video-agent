import { describe, it, expect } from "vitest";
import { resolveDragMoveTargets } from "../resolveDragMoveTargets";

describe("resolveDragMoveTargets", () => {
  it("드래그한 카드가 선택 집합에 없으면 그 카드 하나만 대상", () => {
    const selected = new Set<string>(["b", "c"]);
    expect(resolveDragMoveTargets("a", selected)).toEqual(["a"]);
  });

  it("드래그한 카드가 선택 집합에 포함되면 선택 전체가 대상", () => {
    const selected = new Set<string>(["a", "b", "c"]);
    expect(resolveDragMoveTargets("b", selected).sort()).toEqual(["a", "b", "c"]);
  });

  it("선택 집합이 비어있으면 그 카드 하나만 대상", () => {
    expect(resolveDragMoveTargets("a", new Set())).toEqual(["a"]);
  });

  it("반환에 항상 draggedId 를 포함한다", () => {
    const selected = new Set<string>(["a"]);
    expect(resolveDragMoveTargets("a", selected)).toContain("a");
  });

  it("입력 Set 을 변형하지 않는다(순수 함수)", () => {
    const selected = new Set<string>(["a", "b"]);
    resolveDragMoveTargets("a", selected);
    expect(Array.from(selected).sort()).toEqual(["a", "b"]);
  });
});
