import { describe, it, expect } from "vitest";
import { buildInsertImageScene, DEFAULT_INSERT_IMAGE_DURATION_SEC } from "../project";

describe("buildInsertImageScene", () => {
  it("이미지 전용 삽입 씬을 마커와 함께 생성한다", () => {
    const scene = buildInsertImageScene(5, "pic.png", 4.0);
    expect(scene.scene_id).toBe(5);
    expect(scene.is_insert_image_scene).toBe(true);
    expect(scene.duration_hint_sec).toBe(4.0);
    // narration 없음.
    expect(scene.narration_text).toBe("");
    expect(scene.narration_segments).toEqual([]);
  });

  it("footage 를 kenburns(fit=contain, image=파일명)로 설정한다 (전체화면 이미지)", () => {
    const scene = buildInsertImageScene(1, "cover.jpg");
    const f = scene.footage as unknown as { type: string; image: string; fit: string };
    expect(f.type).toBe("kenburns");
    expect(f.image).toBe("cover.jpg");
    expect(f.fit).toBe("contain");
  });

  it("durationSec 미지정/비정상 시 기본값 사용", () => {
    expect(buildInsertImageScene(1, "a.png").duration_hint_sec).toBe(
      DEFAULT_INSERT_IMAGE_DURATION_SEC,
    );
    expect(buildInsertImageScene(1, "a.png", 0).duration_hint_sec).toBe(
      DEFAULT_INSERT_IMAGE_DURATION_SEC,
    );
    expect(buildInsertImageScene(1, "a.png", -3).duration_hint_sec).toBe(
      DEFAULT_INSERT_IMAGE_DURATION_SEC,
    );
  });
});
