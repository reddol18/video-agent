/**
 * #graphic-copy — 그래픽 오버레이 단일 복사/붙여넣기 규칙.
 *
 * 텍스트 오버레이에는 있던 "이 오버레이만 복사(⧉)" 를 그래픽에도 추가했다.
 * 핵심 규칙은 붙여넣을 때 id 를 새로 발급하는 것 — 그러지 않으면 같은 씬에
 * 두 번 붙여넣었을 때 id 가 겹쳐 편집/삭제가 엉뚱한 오버레이에 적용된다.
 */
import { describe, expect, it } from "vitest";

import type { GraphicOverlay } from "../../types";

/** Editor 의 onPasteGraphic 과 동일한 규칙. */
function pasteGraphic(
  scene: GraphicOverlay[],
  copied: GraphicOverlay,
  newId: string,
): GraphicOverlay[] {
  return [...scene, { ...copied, id: newId, params: { ...(copied.params ?? {}) } }];
}

const SRC: GraphicOverlay = {
  id: "orig1", type: "emoji", x: 0.4, y: 0.6,
  start_sec: 1, end_sec: 3,
  params: { icon_type: "fire", size: 150 },
} as GraphicOverlay;

describe("그래픽 오버레이 붙여넣기", () => {
  it("씬에 추가된다", () => {
    const out = pasteGraphic([], SRC, "new1");
    expect(out).toHaveLength(1);
    expect(out[0].type).toBe("emoji");
  });

  it("id 를 새로 발급한다", () => {
    const out = pasteGraphic([], SRC, "new1");
    expect(out[0].id).toBe("new1");
    expect(out[0].id).not.toBe(SRC.id);
  });

  it("같은 씬에 두 번 붙여도 id 가 겹치지 않는다", () => {
    let out = pasteGraphic([], SRC, "new1");
    out = pasteGraphic(out, SRC, "new2");
    expect(new Set(out.map((o) => o.id)).size).toBe(2);
  });

  it("위치·타이밍·params 가 그대로 복사된다", () => {
    const out = pasteGraphic([], SRC, "new1");
    expect(out[0].x).toBe(0.4);
    expect(out[0].y).toBe(0.6);
    expect(out[0].start_sec).toBe(1);
    expect(out[0].params?.icon_type).toBe("fire");
  });

  it("params 는 복제본이라 원본이 오염되지 않는다", () => {
    const out = pasteGraphic([], SRC, "new1");
    (out[0].params as Record<string, unknown>).icon_type = "star";
    expect(SRC.params?.icon_type).toBe("fire");
  });

  it("기존 오버레이를 지우지 않고 뒤에 붙인다", () => {
    const existing = { ...SRC, id: "keep" } as GraphicOverlay;
    const out = pasteGraphic([existing], SRC, "new1");
    expect(out.map((o) => o.id)).toEqual(["keep", "new1"]);
  });
});

/**
 * 붙여넣기 버튼 노출 조건.
 *
 * 회귀(사용자 보고): 그래픽 오버레이 칩 블록 전체를 `length > 0` 으로 감싸는 바람에,
 * 오버레이가 하나도 없는 씬에는 붙여넣기 버튼이 아예 렌더되지 않아
 * 복사해 둔 오버레이를 붙여넣을 방법이 없었다.
 * 텍스트 오버레이는 이미 `|| copiedOverlays.length > 0` 로 처리돼 있었다.
 */
function showsGraphicRow(overlayCount: number, hasClipboard: boolean): boolean {
  return overlayCount > 0 || hasClipboard;
}

describe("그래픽 오버레이 영역 노출 조건", () => {
  it("오버레이가 있으면 보인다", () => {
    expect(showsGraphicRow(2, false)).toBe(true);
  });

  it("핵심 회귀: 오버레이가 없어도 복사본이 있으면 보인다", () => {
    expect(showsGraphicRow(0, true)).toBe(true);
  });

  it("둘 다 없으면 숨긴다 (빈 줄이 생기지 않도록)", () => {
    expect(showsGraphicRow(0, false)).toBe(false);
  });
});
