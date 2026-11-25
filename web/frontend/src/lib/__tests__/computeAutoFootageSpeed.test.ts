/**
 * 씬 영상 속도 자동 맞춤 — computeAutoFootageSpeed 순수 함수 검증.
 *
 * 정책: 각 씬 영상 재생 길이 = (TTS 길이 + 1.5초) 가 되도록 footage_speed 계산.
 *   speed = footageDur(원본/트림, speed=1.0 기준) / (ttsDur + 1.5), [0.25, 4.0] 클램프.
 *   ttsDur 없음 / 영상 원본 길이 조회 불가 → null(건너뜀).
 */
import { describe, expect, it } from "vitest";
import { computeAutoFootageSpeed } from "../project";
import type { FootageItem, SceneCard, NarrationSegment } from "../../types";

function makeScene(partial: Partial<SceneCard> & { scene_id?: number } = {}): SceneCard {
  return {
    scene_id: partial.scene_id ?? 1,
    title: "",
    description: "",
    mood: "",
    keywords: [],
    duration_hint_sec: 0,
    narration_text: "",
    footage: null,
    match: null,
    tts: null,
    text_overlays: [],
    ...partial,
  } as SceneCard;
}

function makeFootageItem(filename: string, duration_sec: number): FootageItem {
  return {
    filename,
    path: "",
    thumbnail_url: "",
    duration_sec,
    file_type: "video",
    chunks: [],
  } as FootageItem;
}

function makeSeg(id: string, duration_sec: number): NarrationSegment {
  return { id, text: "...", offset_sec: 0, audio_url: "", duration_sec, status: "ready", synced: true };
}

describe("computeAutoFootageSpeed", () => {
  // 느리게만 조절한다 — 영상이 TTS+여유보다 이미 길면 손대지 않는다.
  //   배속을 올려 억지로 줄이면 멀쩡한 장면이 빨라져 연출이 깨진다.
  it("긴 영상 → 건드리지 않음(null). 20s 영상, TTS 5s → target 6.5s 이미 초과", () => {
    const items = [makeFootageItem("a.mp4", 20)];
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4" } as never,
      narration_segments: [makeSeg("seg_0", 5)],
    });
    expect(computeAutoFootageSpeed(scene, items)).toBeNull();
  });

  it("target 과 정확히 같은 길이 → 건드리지 않음(null). 6.5s 영상, TTS 5s", () => {
    const items = [makeFootageItem("a2.mp4", 6.5)];
    const scene = makeScene({
      footage: { type: "clip", file: "a2.mp4" } as never,
      narration_segments: [makeSeg("seg_0", 5)],
    });
    expect(computeAutoFootageSpeed(scene, items)).toBeNull();
  });

  it("짧은 영상 → 느리게(speed<1). 3s 영상, TTS 5s → target 6.5s → speed ≈ 0.4615", () => {
    const items = [makeFootageItem("b.mp4", 3)];
    const scene = makeScene({
      footage: { type: "clip", file: "b.mp4" } as never,
      narration_segments: [makeSeg("seg_0", 5)],
    });
    const speed = computeAutoFootageSpeed(scene, items);
    expect(speed).not.toBeNull();
    expect(speed!).toBeCloseTo(3 / 6.5, 4);
    expect(speed!).toBeLessThan(1);
  });

  it("아주 긴 영상도 건드리지 않음(null). 200s 영상, TTS 5s", () => {
    const items = [makeFootageItem("c.mp4", 200)];
    const scene = makeScene({
      footage: { type: "clip", file: "c.mp4" } as never,
      narration_segments: [makeSeg("seg_0", 5)],
    });
    expect(computeAutoFootageSpeed(scene, items)).toBeNull();
  });

  it("아주 짧은 영상 → 하한 0.25 클램프. 0.5s 영상, TTS 10s → 이론 0.043x → 0.25", () => {
    const items = [makeFootageItem("d.mp4", 0.5)];
    const scene = makeScene({
      footage: { type: "clip", file: "d.mp4" } as never,
      narration_segments: [makeSeg("seg_0", 10)],
    });
    expect(computeAutoFootageSpeed(scene, items)).toBe(0.25);
  });

  it("TTS 없으면 null(건너뜀)", () => {
    const items = [makeFootageItem("e.mp4", 20)];
    const scene = makeScene({ footage: { type: "clip", file: "e.mp4" } as never });
    expect(computeAutoFootageSpeed(scene, items)).toBeNull();
  });

  it("image/kenburns 씬은 원본 길이 개념 없음 → null(건너뜀)", () => {
    const scene = makeScene({
      footage: { type: "kenburns", image: "img.png" } as never,
      narration_segments: [makeSeg("seg_0", 5)],
    });
    expect(computeAutoFootageSpeed(scene, [])).toBeNull();
  });

  it("트림(clip_end_sec)이 있으면 트림 구간 길이 기준. 10~13s(3s) 트림, TTS 3s → target 4.5s → speed 3/4.5", () => {
    const items = [makeFootageItem("f.mp4", 100)];
    const scene = makeScene({
      footage: { type: "clip", file: "f.mp4", clip_start_sec: 10, clip_end_sec: 13 } as never,
      narration_segments: [makeSeg("seg_0", 3)],
    });
    const speed = computeAutoFootageSpeed(scene, items);
    expect(speed!).toBeCloseTo(3 / 4.5, 4);
  });

  it("트림 구간이 이미 target 보다 길면 건드리지 않음(null)", () => {
    const items = [makeFootageItem("f2.mp4", 100)];
    const scene = makeScene({
      footage: { type: "clip", file: "f2.mp4", clip_start_sec: 10, clip_end_sec: 16 } as never,
      narration_segments: [makeSeg("seg_0", 3)],
    });
    expect(computeAutoFootageSpeed(scene, items)).toBeNull();
  });

  it("legacy scene.tts(ready) 폴백 — seg_0 없을 때 사용", () => {
    const items = [makeFootageItem("g.mp4", 4)];
    const scene = makeScene({
      footage: { type: "clip", file: "g.mp4" } as never,
      tts: { audio_url: "", duration_sec: 4, status: "ready", synced: true },
    });
    const speed = computeAutoFootageSpeed(scene, items);
    expect(speed!).toBeCloseTo(4 / 5.5, 4);
  });

  // TTS 전체 재생성 후 speed 자동 재계산의 핵심 불변식:
  //   새 TTS duration 이 반영된 씬으로 재계산하면 speed 도 그에 맞게 달라진다.
  //   (옛 duration 으로 계산하면 이 값이 안 바뀌는 순서 의존 버그.)
  it("TTS duration 이 바뀌면 speed 도 새 duration 기준으로 재계산된다", () => {
    const items = [makeFootageItem("h.mp4", 5)];
    // 초기: 5s 영상, TTS 5s → target 6.5s → speed 5/6.5.
    const before = makeScene({
      footage: { type: "clip", file: "h.mp4" } as never,
      narration_segments: [makeSeg("seg_0", 5)],
    });
    const speedBefore = computeAutoFootageSpeed(before, items);
    expect(speedBefore!).toBeCloseTo(5 / 6.5, 4);

    // 재생성 후: 같은 영상, TTS 8s 로 변경 → target 9.5s → speed 5/9.5.
    const after: SceneCard = {
      ...before,
      narration_segments: [makeSeg("seg_0", 8)],
    };
    const speedAfter = computeAutoFootageSpeed(after, items);
    expect(speedAfter!).toBeCloseTo(5 / 9.5, 4);
    expect(speedAfter!).not.toBeCloseTo(speedBefore!, 4);
    expect(speedAfter!).toBeLessThan(speedBefore!);  // TTS 길어짐 → 더 느리게.
  });
});
