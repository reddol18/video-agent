/**
 * #stale-narration — pushProjectState(register) 는 나레이션을 보내면 안 된다.
 *
 * 실제 사고: 나레이션 편집은 patchScene(서버가 진실의 원천)으로만 저장되는데,
 * pushProjectState 는 IndexedDB 의 프로젝트 전체를 통째로 보낸다. IndexedDB 쪽
 * narration_text/narration_rev 는 쉽게 뒤처지므로, 이름 변경·BGM 편집처럼
 * 나레이션과 무관한 동작이 register 를 부르면 옛 대본이 서버로 다시 밀려간다.
 *
 * 서버가 리비전 검사로 막긴 하지만, 그러면 "TTS 텍스트를 고쳐도 반영이 안 되는"
 * 증상이 된다(실측: 씬 29 에서 326회 거부, narration_rev 62 까지 상승).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const putMock = vi.fn(() => Promise.resolve({ data: {} }));

vi.mock("axios", () => ({
  default: {
    create: () => ({
      put: putMock,
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
      interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    }),
  },
}));

const makeProject = () =>
  ({
    project_id: "p1",
    name: "테스트",
    scenes: [
      {
        scene_id: 1,
        narration_text: "옛 대본 — 서버의 최신본을 덮으면 안 된다",
        narration_rev: 3,
        narration_segments: [{ id: "seg_0", text: "옛 대본", duration_sec: 1 }],
        footage_speed: 1.0,
        text_overlays: [],
      },
    ],
    output_width: 1920,
    output_height: 1080,
  }) as never;

describe("pushProjectState — 나레이션 제외", () => {
  beforeEach(() => putMock.mockClear());

  it("scenes 에서 narration_* 필드를 제거하고 보낸다", async () => {
    const { pushProjectState } = await import("../../api/client");
    await pushProjectState("p1", makeProject());

    expect(putMock).toHaveBeenCalledTimes(1);
    const body = putMock.mock.calls[0][1] as { scenes: Record<string, unknown>[] };
    const scene = body.scenes[0];

    expect(scene).not.toHaveProperty("narration_text");
    expect(scene).not.toHaveProperty("narration_rev");
    expect(scene).not.toHaveProperty("narration_segments");
  });

  it("나레이션 외 필드는 그대로 보낸다(회귀 방지)", async () => {
    const { pushProjectState } = await import("../../api/client");
    await pushProjectState("p1", makeProject());

    const body = putMock.mock.calls[0][1] as { scenes: Record<string, unknown>[] };
    const scene = body.scenes[0];

    expect(scene.scene_id).toBe(1);
    expect(scene.footage_speed).toBe(1.0);
    expect(scene).toHaveProperty("text_overlays");
  });
});
