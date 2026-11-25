/**
 * #369 — Undo/Redo patch round-trip 검증 단위 테스트.
 *
 * 모든 효과 종류(text/shape/graphic/mosaic/image/filter/motion/bgm/sfx) 의 변경에 대해
 * jsondiffpatch 의 diff → patch → unpatch 가 정확히 역연산이 되는지 확인한다.
 *
 * 가짜 Project 객체를 손으로 만들어 1) 변경 전 prev, 2) 변경 후 next 두 인스턴스를 비교한다.
 * verifyPatchRoundtrip 가 두 방향 모두 OK 면 통과.
 */
import { describe, it, expect } from "vitest";
import { verifyPatchRoundtrip } from "../index";
import type { Project, SceneCard } from "../../types";

function makeBaseProject(): Project {
  const scene: SceneCard = {
    scene_id: 1,
    title: "씬1",
    description: "테스트 씬",
    mood: "",
    keywords: ["t1"],
    duration_hint_sec: 3,
    narration_text: "안녕하세요",
    subtitle_text: null,
    footage: null,
    match: null,
    tts: null,
    text_overlays: [],
    footage_speed: 1.0,
    footage_audio_volume: 0.3,
    shape_overlays: [],
    graphic_overlays: [],
    filters: [],
    motions: [],
    mosaic_overlays: [],
    tags: [],
  };
  return {
    project_id: "p1",
    name: "test",
    footage_dir: "",
    scenario_text: "",
    status: "edit",
    scenes: [scene, { ...scene, scene_id: 2, title: "씬2" }],
    tts_voice: "ko-KR-Chirp3-HD-Orus",
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T00:00:00Z",
    studio_assets: [],
    output_width: 1920,
    output_height: 1080,
    bgm_tracks: [],
  };
}

function clone<T>(o: T): T {
  return JSON.parse(JSON.stringify(o));
}

describe("verifyPatchRoundtrip — 모든 효과 변경에 대한 round-trip", () => {
  it("text_overlays 추가", () => {
    const prev = makeBaseProject();
    const next = clone(prev);
    next.scenes[0].text_overlays = [
      {
        id: "t1",
        text: "안녕",
        x: 0.1, y: 0.2,
        font_family: "NotoSansKR-Bold",
        font_size: 0.05,
        color: "#FFFFFF",
        bg_color: null,
        bg_opacity: 0,
        outline_color: null,
        outline_width: 0,
        start_sec: 0, end_sec: 2,
        opacity: 1,
        sfx_asset_id: null,
        sfx_volume: 0.5,
      } as any,
    ];
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("shape_overlays 변경", () => {
    const prev = makeBaseProject();
    prev.scenes[0].shape_overlays = [{
      id: "s1", shape_type: "rect", x: 0, y: 0, width: 0.5, height: 0.5,
      fill_color: "#000000", fill_opacity: 1, stroke_color: null, stroke_width: 0,
      start_sec: 0, end_sec: 1, opacity: 1, sfx_asset_id: null, sfx_volume: 0,
    } as any];
    const next = clone(prev);
    next.scenes[0].shape_overlays![0].fill_color = "#ff0000";
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("graphic_overlays 신규 추가", () => {
    const prev = makeBaseProject();
    const next = clone(prev);
    next.scenes[1].graphic_overlays = [{
      id: "g1", type: "lower_third", x: 0, y: 0.8, width: 1, height: 0.2,
      start_sec: 0, end_sec: 2, opacity: 1, sfx_asset_id: null, sfx_volume: 0,
    } as any];
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("mosaic_overlays 좌표 변경", () => {
    const prev = makeBaseProject();
    prev.scenes[0].mosaic_overlays = [{
      id: "m1", x: 0.2, y: 0.3, width: 0.2, height: 0.2,
      strength: 0.5, start_sec: 0, end_sec: 2,
    } as any];
    const next = clone(prev);
    next.scenes[0].mosaic_overlays![0].x = 0.4;
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("image overlay 추가", () => {
    const prev = makeBaseProject();
    const next = clone(prev);
    (next.scenes[0] as any).image_overlays = [{
      id: "img1", image_asset_id: "a1", x: 0, y: 0, width: 0.5, height: 0.5,
      start_sec: 0, end_sec: 2, opacity: 1, sfx_asset_id: null, sfx_volume: 0,
    }];
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("filters 변경 (warm 강도)", () => {
    const prev = makeBaseProject();
    prev.scenes[0].filters = [{ filter_type: "warm", strength: 0.5 } as any];
    const next = clone(prev);
    next.scenes[0].filters![0].strength = 0.8;
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("motions zoom 비율 변경", () => {
    const prev = makeBaseProject();
    prev.scenes[0].motions = [{ type: "clip_zoom", strength: 1.1, start_sec: 0, end_sec: 2 } as any];
    const next = clone(prev);
    next.scenes[0].motions![0].strength = 1.3;
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("bgm_tracks 추가 + 볼륨 변경", () => {
    const prev = makeBaseProject();
    prev.bgm_tracks = [{
      id: "b1", audio_asset_id: "a1", start_sec: 0, end_sec: 10,
      fade_in_sec: 1, fade_out_sec: 1, volume: 0.4,
    }];
    const next = clone(prev);
    next.bgm_tracks![0].volume = 0.6;
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("sfx_asset_id 변경 (text overlay 의 sfx)", () => {
    const prev = makeBaseProject();
    prev.scenes[0].text_overlays = [{
      id: "t1", text: "안녕", x: 0.1, y: 0.2,
      font_family: "NotoSansKR-Bold", font_size: 0.05,
      color: "#FFFFFF", bg_color: null, bg_opacity: 0,
      outline_color: null, outline_width: 0,
      start_sec: 0, end_sec: 2, opacity: 1,
      sfx_asset_id: null, sfx_volume: 0.5,
    } as any];
    const next = clone(prev);
    next.scenes[0].text_overlays[0].sfx_asset_id = "audio-1";
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("씬 1 추가(삽입) — 배열 길이 변화", () => {
    const prev = makeBaseProject();
    const next = clone(prev);
    next.scenes.push({
      ...prev.scenes[0],
      scene_id: 3,
      title: "씬3",
    });
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("씬 1 삭제 — 배열 길이 감소", () => {
    const prev = makeBaseProject();
    const next = clone(prev);
    next.scenes.splice(0, 1);
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });

  it("scene_id 와 무관한 필드 변경(footage_speed)", () => {
    const prev = makeBaseProject();
    const next = clone(prev);
    next.scenes[0].footage_speed = 1.5;
    const r = verifyPatchRoundtrip(prev, next);
    expect(r.forwardOk).toBe(true);
    expect(r.backwardOk).toBe(true);
  });
});
