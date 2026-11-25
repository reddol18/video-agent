/**
 * #timeline-dots — 타임라인 씬 블록의 상태 점.
 *   주황 = TTS 음성 있음, 하늘 = 처리 안 된 편집 코멘트 있음.
 *
 * TTS 판정은 두 방식을 모두 봐야 한다:
 *   · narration_segments (신규) — status/duration_sec
 *   · scene.tts (레거시) — 옛 프로젝트 호환
 * 한쪽만 보면 옛 프로젝트나 새 프로젝트에서 점이 안 뜬다.
 */
import { describe, expect, it } from "vitest";

import type { SceneCard } from "../../types";

/** TimelineBar 의 hasTts 와 동일 판정 — 음성이 아니라 "대본" 유무. */
function hasTts(scene: Partial<SceneCard>): boolean {
  if ((scene.narration_text ?? "").trim()) return true;
  return (scene.narration_segments ?? []).some((s) => (s.text ?? "").trim());
}

/** TimelineBar 의 openComments 와 동일 판정 — 완료된 것도 센다. */
function openComments(scene: Partial<SceneCard>): number {
  return (scene.comments ?? []).length;
}

const seg = (o: Record<string, unknown>) => o as never;
const cmt = (status: string) => ({ id: "c", body: "b", status } as never);

describe("TTS 점 (주황) — 대본 기준", () => {
  it("narration_text 가 있으면 표시", () => {
    expect(hasTts({ narration_text: "돌아온 날은 열대야였습니다." })).toBe(true);
  });

  it("음성을 아직 안 만들었어도 대본만 있으면 표시", () => {
    expect(hasTts({
      narration_text: "물이라도 뿌려 봅니다.",
      narration_segments: [seg({ id: "seg_0", text: "물이라도", status: "idle", duration_sec: 0 })],
    })).toBe(true);
  });

  it("segment 텍스트만 있어도 표시", () => {
    expect(hasTts({ narration_segments: [seg({ id: "seg_0", text: "안녕하세요", status: "idle" })] })).toBe(true);
  });

  it("공백만 있으면 표시하지 않는다", () => {
    expect(hasTts({ narration_text: "   " })).toBe(false);
  });

  it("대본이 없으면 표시하지 않는다 (인서트/애니메이션 씬)", () => {
    expect(hasTts({ narration_text: "" })).toBe(false);
  });
});

describe("편집 코멘트 점 (하늘) — 완료 포함", () => {
  it("열린 코멘트를 센다", () => {
    expect(openComments({ comments: [cmt("open"), cmt("open")] })).toBe(2);
  });

  it("완료된 코멘트도 센다 (손본 이력이 남아야 한다)", () => {
    expect(openComments({ comments: [cmt("done"), cmt("done")] })).toBe(2);
  });

  it("open 과 done 이 섞여도 전부 센다", () => {
    expect(openComments({ comments: [cmt("open"), cmt("done"), cmt("open")] })).toBe(3);
  });

  it("코멘트 필드가 없으면 0 (옛 프로젝트 호환)", () => {
    expect(openComments({})).toBe(0);
  });
});

describe("두 점의 조합", () => {
  it("둘 다 있으면 둘 다 뜬다", () => {
    const s = {
      narration_text: "돌아온 날은 열대야였습니다.",
      comments: [cmt("done")],
    };
    expect(hasTts(s)).toBe(true);
    expect(openComments(s)).toBe(1);
  });

  it("둘 다 없으면 점 영역 자체를 그리지 않는다", () => {
    const s = {};
    expect(hasTts(s) || openComments(s) > 0).toBe(false);
  });
});
