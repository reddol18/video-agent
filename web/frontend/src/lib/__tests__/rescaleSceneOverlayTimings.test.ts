/**
 * footage_speed 변경 시 오버레이 타임라인 재조정 — rescaleSceneOverlayTimings 순수 함수 검증.
 *
 * 정책: 오버레이 start_sec/end_sec 은 "속도가 반영된 출력(재생) 타임라인" 기준이므로
 *   footage_speed 가 oldSpeed→newSpeed 로 바뀌면 factor=oldSpeed/newSpeed 를 곱해 재배치한다.
 *   (예: speed 1.0→4.0, start_sec 31.5 → 31.5*(1/4)=7.88, 소수 둘째자리 반올림)
 */
import { describe, expect, it } from "vitest";
import { rescaleSceneOverlayTimings } from "../project";
import type { SceneCard, TextOverlay, ShapeOverlay } from "../../types";

function makeScene(partial: Partial<SceneCard> = {}): SceneCard {
  return {
    scene_id: 1,
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

function makeText(start_sec: number, end_sec: number | null): TextOverlay {
  return {
    id: "t1",
    text: "hi",
    x: 0.5,
    y: 0.5,
    font_size: 40,
    font_family: "NotoSansKR",
    color: "#fff",
    gradient: null,
    bold: false,
    italic: false,
    outline_color: "#000",
    outline_width: 0,
    glow_color: "#000",
    glow_radius: 0,
    opacity: 1,
    align: "center",
    start_sec,
    end_sec,
  };
}

function makeShape(start_sec: number, end_sec: number | null): ShapeOverlay {
  return {
    id: "s1",
    shape: "rect",
    x: 0.1,
    y: 0.1,
    width: 0.2,
    height: 0.2,
    color: "#ff0000",
    opacity: 1,
    start_sec,
    end_sec,
  };
}

describe("rescaleSceneOverlayTimings", () => {
  it("speed 1.0→4.0: start_sec 31.5 → 7.88, end_sec null 유지", () => {
    const scene = makeScene({ text_overlays: [makeText(31.5, null)] });
    const out = rescaleSceneOverlayTimings(scene, 1.0, 4.0);
    expect(out.text_overlays[0].start_sec).toBe(7.88);
    expect(out.text_overlays[0].end_sec).toBeNull();
    // 원본 불변
    expect(scene.text_overlays[0].start_sec).toBe(31.5);
  });

  it("speed 동일(2.0→2.0)이면 무변경(동일 참조 반환)", () => {
    const scene = makeScene({ text_overlays: [makeText(10, 20)] });
    const out = rescaleSceneOverlayTimings(scene, 2.0, 2.0);
    expect(out).toBe(scene);
  });

  it("end_sec 있는 케이스도 비례 축소 (speed 1.0→2.0, factor 0.5)", () => {
    const scene = makeScene({
      text_overlays: [makeText(10, 20)],
      shape_overlays: [makeShape(4, 8)],
    });
    const out = rescaleSceneOverlayTimings(scene, 1.0, 2.0);
    expect(out.text_overlays[0].start_sec).toBe(5);
    expect(out.text_overlays[0].end_sec).toBe(10);
    expect(out.shape_overlays![0].start_sec).toBe(2);
    expect(out.shape_overlays![0].end_sec).toBe(4);
  });

  it("oldSpeed<=0 이면 무변경", () => {
    const scene = makeScene({ text_overlays: [makeText(10, 20)] });
    expect(rescaleSceneOverlayTimings(scene, 0, 2.0)).toBe(scene);
    expect(rescaleSceneOverlayTimings(scene, -1, 2.0)).toBe(scene);
  });
});
