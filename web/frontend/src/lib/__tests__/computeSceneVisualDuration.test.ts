/**
 * #388 — `computeSceneVisualDuration` 통합 검증.
 *
 * 옛 함수(`TimelineBar.getSceneDurationSec`) 와 인스펙터(`computeSceneVisualDuration`) 가
 * 서로 다른 우선순위로 씬 길이를 계산해 화면 3 곳이 어긋났던 버그를 해소했다.
 * 통합 후 두 함수는 같은 구현을 호출하며, 본 테스트는 새 시그니처(footage trim/cut 캐시)에서
 * 정확한 값을 반환하는지 검증한다.
 *
 * TimelineBar 의 옛 단위 테스트(`timelineBarDuration.test.ts`) 도 통합 후 그대로 통과해야 한다
 * (회귀 안전 — wrapper `getSceneDurationSec` 가 외부 인터페이스 유지).
 */
import { describe, expect, it } from "vitest";
import { computeSceneVisualDuration } from "../project";
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
  };
}

function makeSeg(id: string, duration_sec: number, offset_sec = 0): NarrationSegment {
  return {
    id,
    text: "...",
    offset_sec,
    audio_url: "",
    duration_sec,
    status: "ready",
    synced: true,
  };
}

describe("#388 computeSceneVisualDuration — footage trim 반영", () => {
  it("16번 씬 회귀 — footage trim 3.1s + TTS seg_0 5s → 5s (max, Phase 5 Time Warp 정책)", () => {
    // 이슈 본문 회귀: footage 트림 3.1s, TTS seg_0 = 5s.
    //   기존 인스펙터는 footage 트림을 보지 않아 "씬 길이 ≈ 5.00s" 표시.
    //   통합 후 footage trim (3.1s) 과 TTS (5s) 의 max → 5s — TimelineBar 와 정확히 일치.
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 3.1 },
      narration_segments: [makeSeg("seg_0", 5.0)],
    });
    expect(computeSceneVisualDuration(scene, [])).toBeCloseTo(5.0, 3);
  });

  it("footage trim 4.5s + hint 10.0s + TTS 없음 → trim 값 (hint 는 footage 가 있으면 무시)", () => {
    // 옛 #375 정책: footage trim 이 있으면 hint 는 무시 (footage 가 진실의 출처).
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 2.0, clip_end_sec: 6.5 },
      duration_hint_sec: 10.0,
    });
    expect(computeSceneVisualDuration(scene, [])).toBeCloseTo(4.5, 3);
  });

  it("footage trim 짧고 TTS/hint 없음 → trim 값 그대로 (sceneDur 가 footage 와 일치)", () => {
    // TTS 도 없으면 footage trim 자체가 씬 길이.
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 3.1 },
    });
    expect(computeSceneVisualDuration(scene, [])).toBeCloseTo(3.1, 3);
  });

  it("footage trim + cut 캐시 → cut 우선 (Phase 5 확정값)", () => {
    const scene = makeScene({
      scene_id: 7,
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 3.1 },
      narration_segments: [makeSeg("seg_0", 5.0)],
    });
    const cuts = new Map<number, number>([[7, 4.2]]);
    expect(computeSceneVisualDuration(scene, [], cuts)).toBeCloseTo(4.2, 3);
  });

  it("cut 캐시 있지만 footage_speed != 1.0 → cut 무시 (옛 settings 결과)", () => {
    const scene = makeScene({
      scene_id: 7,
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 6.0 },
      footage_speed: 2.0,  // 영상이 절반 시간에 끝남 → 3.0s
    });
    const cuts = new Map<number, number>([[7, 5.0]]);
    expect(computeSceneVisualDuration(scene, [], cuts)).toBeCloseTo(3.0, 3);
  });

  it("#397 — clip_end_sec=null + footageItems 원본 제공 → footage 원본 사용 (remaining = orig - clip_start)", () => {
    // #394 로 footage 원본 폴백을 제거했으나 사용자 의도와 달라 #397 로 복원.
    //   "16번 씬이 5초라면 그건 뭔가 잘못된 거야. 16번 씬은 34.8초가 맞아."
    //   → footage 원본 11.9s 에서 clip_start_sec 1.0s 를 뺀 10.9s 가 씬 길이.
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 1.0, clip_end_sec: null },
    });
    const items = [makeFootageItem("a.mp4", 11.9)];
    expect(computeSceneVisualDuration(scene, items)).toBeCloseTo(10.9, 3);
  });

  it("#397 — clip_end_sec=null + footageItems Map + TTS seg_0 → max(footage 원본, TTS) — 16번 씬 시나리오", () => {
    // 사용자 시나리오: footage 원본 34.75s, clip_*_sec=null, TTS seg_0 = 5s.
    //   #394 시점에는 TTS 5s 가 사용되어 "16번 = 5초" → 잘못. #397 복원 후 footage 원본 34.75s 가 진실의 출처.
    //   TTS 가 더 짧으므로 max(34.75, 5.0) = 34.75.
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: null },
      narration_segments: [makeSeg("seg_0", 5.0)],
    });
    const map = new Map<string, FootageItem>([["a.mp4", makeFootageItem("a.mp4", 34.75)]]);
    expect(computeSceneVisualDuration(scene, map)).toBeCloseTo(34.75, 3);
  });

  it("#397 — clip_end_sec=null + footageItems Map + hint 만 → footage 원본 사용 (hint 는 footage 가 있으면 무시)", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: null },
      duration_hint_sec: 7.0,
    });
    const map = new Map<string, FootageItem>([["a.mp4", makeFootageItem("a.mp4", 100.0)]]);
    // 원본 100s 가 사용되고 hint 7s 는 무시 — footage 가 진실의 출처.
    expect(computeSceneVisualDuration(scene, map)).toBeCloseTo(100.0, 3);
  });

  it("#397 — clip_end_sec=null + footageItems Map + TTS 가 footage 원본보다 김 → TTS 사용 (Time Warp 연장)", () => {
    // footage 원본 3s 인데 TTS 5s → TTS 가 길어 footage 를 늘림 (Phase 5 Time Warp).
    //   max(remaining=3, tts0=5) = 5.
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: null },
      narration_segments: [makeSeg("seg_0", 5.0)],
    });
    const map = new Map<string, FootageItem>([["a.mp4", makeFootageItem("a.mp4", 3.0)]]);
    expect(computeSceneVisualDuration(scene, map)).toBeCloseTo(5.0, 3);
  });

  it("clip_end_sec=null + footageItems 미제공 → TTS/hint 폴백 (변경 없음)", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: null },
      duration_hint_sec: 4.2,
    });
    expect(computeSceneVisualDuration(scene)).toBeCloseTo(4.2, 3);
  });

  it("기존 무인자 호출 회귀 안전 — TTS seg_0 만 → seg_0 값", () => {
    const scene = makeScene({
      narration_segments: [makeSeg("seg_0", 4.7)],
    });
    expect(computeSceneVisualDuration(scene)).toBeCloseTo(4.7, 3);
  });

  it("기존 무인자 호출 회귀 안전 — hint 만 → hint 값", () => {
    const scene = makeScene({ duration_hint_sec: 5.5 });
    expect(computeSceneVisualDuration(scene)).toBeCloseTo(5.5, 3);
  });

  it("기존 무인자 호출 회귀 안전 — 아무 것도 없음 → 1.0 (인스펙터 최소값)", () => {
    // 인스펙터는 TimelineBar 와 달리 빈 씬 폴백이 1.0 이다 (overflow 클램프용).
    const scene = makeScene();
    expect(computeSceneVisualDuration(scene)).toBeCloseTo(1.0, 3);
  });

  it("legacy scene.tts.duration_sec 만 있음 (seg_0 없음, ready) → scene.tts 사용 (회귀 안전)", () => {
    const scene = makeScene({
      tts: { audio_url: "u", duration_sec: 4.7, status: "ready", synced: false },
    });
    expect(computeSceneVisualDuration(scene)).toBeCloseTo(4.7, 3);
  });

  it("legacy scene.tts 가 generating 상태 → 무시하고 hint 폴백 (#375 정책 유지)", () => {
    const scene = makeScene({
      duration_hint_sec: 5.5,
      tts: { audio_url: "", duration_sec: 9.9, status: "generating", synced: false },
    });
    expect(computeSceneVisualDuration(scene)).toBeCloseTo(5.5, 3);
  });

  it("seg_0 와 scene.tts 동시 존재 → seg_0 우선", () => {
    const scene = makeScene({
      narration_segments: [makeSeg("seg_0", 6.2)],
      tts: { audio_url: "u", duration_sec: 9.9, status: "ready", synced: false },
    });
    expect(computeSceneVisualDuration(scene)).toBeCloseTo(6.2, 3);
  });

  it("footage_speed=2.0 + trim 6s + TTS 0 → trim/speed = 3.0", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 6.0 },
      footage_speed: 2.0,
    });
    expect(computeSceneVisualDuration(scene, [])).toBeCloseTo(3.0, 3);
  });

  it("kenburns + TTS → footage 시간 무시, TTS 사용", () => {
    const scene = makeScene({
      footage: { type: "kenburns", image: "img.png" },
      narration_segments: [makeSeg("seg_0", 6.0)],
    });
    expect(computeSceneVisualDuration(scene, [])).toBeCloseTo(6.0, 3);
  });
});
