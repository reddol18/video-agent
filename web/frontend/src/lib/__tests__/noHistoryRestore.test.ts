/**
 * #no-history-restore — undo 히스토리를 새로고침 후 되살리지 않는다.
 *
 * 실제 사고(2회): 새로고침하면 "Undo 히스토리 N단계 복구됨" 토스트가 뜨고,
 * 그 상태에서 Cmd+Z 를 누르면 방금 저장한 편집(대본 여러 개)이 통째로
 * 새로고침 이전 상태로 롤백됐다.
 *
 * 저장된 히스토리는 "새로고침 이전 시점의 프로젝트 스냅샷"이라, 그 사이에
 * 서버에 저장된 편집을 알지 못한다. 세션이 끝나면 히스토리도 끝나야 한다.
 *
 * 이 테스트는 Editor 소스에 복원 경로가 되살아나지 않는지 지킨다.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const EDITOR = readFileSync(
  join(__dirname, "../../pages/Editor.tsx"),
  "utf-8",
);

describe("Editor 는 undo 히스토리를 복원하지 않는다", () => {
  it("dbLoadSceneHistory 를 호출하지 않는다", () => {
    expect(EDITOR).not.toContain("dbLoadSceneHistory");
  });

  it("restoreHistoryFromPatches 를 호출하지 않는다", () => {
    expect(EDITOR).not.toContain("restoreHistoryFromPatches");
  });

  it('"복구됨" 토스트를 실제로 띄우지 않는다 (주석 언급은 허용)', () => {
    // 주석에는 제거 경위 설명으로 문구가 남아 있을 수 있다.
    // 실행되는 호출 형태(ui.info(`...복구됨...`))만 금지한다.
    expect(EDITOR).not.toMatch(/ui\.info\([^)]*복구됨/);
  });

  it("히스토리를 IndexedDB 에 영속화하지 않는다", () => {
    expect(EDITOR).not.toContain("persistHistoryChange");
  });

  it("진입 시 히스토리를 초기화한다", () => {
    expect(EDITOR).toContain("clearHistory()");
  });

  it("남아 있는 옛 기록은 청소한다", () => {
    expect(EDITOR).toContain("dbClearSceneHistory");
  });
});
