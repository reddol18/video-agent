/**
 * #375 — TimelineBar `getSceneDurationSec` 우선순위 단위 테스트.
 *
 * 이슈 본문 "기대 동작" 의 우선순위(2~6, `cache/timeline.json` 노출은 후속):
 *   2) footage(clip).clip_end_sec - clip_start_sec   (trim)
 *   3) footage(clip) 원본 duration (clip_end_sec=null)
 *   4) tts.duration_sec (TTS 준비)
 *   5) duration_hint_sec
 *   6) 3.0 폴백
 *
 * 단 2/3 ↔ 4 는 **max** 로 결합(TTS 가 footage 보다 길면 Phase 5 Time Warp 가 footage 를 늘림).
 * 이미지 씬은 2/3 을 건너뛰고 4→5→6 만.
 */
import { describe, expect, it } from "vitest";
import { getSceneDurationSec } from "../TimelineBar";
import type { FootageItem, SceneCard } from "../../types";

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

describe("getSceneDurationSec (#375)", () => {
  it("clip + trim 있고 TTS 없음 → trim 값", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 2.0, clip_end_sec: 6.5 },
      duration_hint_sec: 10.0,
    });
    expect(getSceneDurationSec(scene, [])).toBeCloseTo(4.5, 3);
  });

  it("#397 — clip + trim 없고 (clip_end_sec=null) → footage 원본 사용 (#394 revert)", () => {
    // #394 에서 footage 원본 폴백을 제거했으나 사용자 의도와 달라 #397 로 복원.
    //   "16번 씬이 5초라면 그건 뭔가 잘못된 거야. 16번 씬은 34.8초가 맞아."
    //   footage 원본 11.9s 에서 clip_start_sec 1.0s 를 빼고 / speed(=1) → 10.9s.
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 1.0, clip_end_sec: null },
    });
    const items = [makeFootageItem("a.mp4", 11.9)];
    expect(getSceneDurationSec(scene, items)).toBeCloseTo(10.9, 3);
  });

  it("#397 — clip + clip_end_sec 미지정(undefined) → footage 원본 사용", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0 },
    });
    const items = [makeFootageItem("a.mp4", 11.9)];
    // 원본 11.9s 전체.
    expect(getSceneDurationSec(scene, items)).toBeCloseTo(11.9, 3);
  });

  it("clip + trim 짧고 TTS 김 → max(trim, TTS) = TTS", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 4.7 },
      tts: { audio_url: "u", duration_sec: 8.0, status: "ready", synced: false },
    });
    expect(getSceneDurationSec(scene, [])).toBeCloseTo(8.0, 3);
  });

  it("clip + trim 길고 TTS 짧음 → max(trim, TTS) = trim", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 11.9 },
      tts: { audio_url: "u", duration_sec: 4.7, status: "ready", synced: false },
    });
    expect(getSceneDurationSec(scene, [])).toBeCloseTo(11.9, 3);
  });

  it("trim 없고 TTS 있음 (footage 없음) → TTS 값", () => {
    const scene = makeScene({
      tts: { audio_url: "u", duration_sec: 4.7, status: "ready", synced: false },
    });
    expect(getSceneDurationSec(scene)).toBeCloseTo(4.7, 3);
  });

  it("TTS 가 'generating' 등 미준비 상태이면 무시 → hint 또는 폴백", () => {
    const scene = makeScene({
      duration_hint_sec: 5.5,
      tts: { audio_url: "", duration_sec: 9.9, status: "generating", synced: false },
    });
    expect(getSceneDurationSec(scene)).toBeCloseTo(5.5, 3);
  });

  it("둘 다 없음 → duration_hint_sec", () => {
    const scene = makeScene({ duration_hint_sec: 5.5 });
    expect(getSceneDurationSec(scene)).toBeCloseTo(5.5, 3);
  });

  it("아무 것도 없음 → 3.0 폴백", () => {
    const scene = makeScene();
    expect(getSceneDurationSec(scene)).toBeCloseTo(3.0, 3);
  });

  it("kenburns 이미지 씬 (시간 개념 없음) + TTS 있음 → TTS", () => {
    const scene = makeScene({
      footage: { type: "kenburns", image: "img.png" },
      tts: { audio_url: "u", duration_sec: 6.0, status: "ready", synced: false },
    });
    // kenburns 는 footage 길이 무시, TTS 또는 hint 사용.
    expect(getSceneDurationSec(scene, [])).toBeCloseTo(6.0, 3);
  });

  it("kenburns 이미지 씬 + TTS 없고 hint 만 → hint", () => {
    const scene = makeScene({
      footage: { type: "kenburns", image: "img.png" },
      duration_hint_sec: 4.0,
    });
    expect(getSceneDurationSec(scene, [])).toBeCloseTo(4.0, 3);
  });

  it("#397 — clip + footageItems 가 Map 으로 전달되면 footage 원본 사용", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: null },
    });
    const map = new Map<string, FootageItem>([["a.mp4", makeFootageItem("a.mp4", 7.7)]]);
    expect(getSceneDurationSec(scene, map)).toBeCloseTo(7.7, 3);
  });

  it("#397 — clip + clip_end_sec 가 0/음수 → ② 분기 실패 후 ③ footage 원본 사용", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 0 },
    });
    const items = [makeFootageItem("a.mp4", 5.0)];
    // ② 분기 진입 실패(clip_end_sec=0 ≤ start) → ③ footage 원본 5s.
    expect(getSceneDurationSec(scene, items)).toBeCloseTo(5.0, 3);
  });

  it("clip + footageItems 미제공이고 clip_end_sec=null → TTS 또는 hint 폴백", () => {
    const scene = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: null },
      duration_hint_sec: 4.2,
    });
    expect(getSceneDurationSec(scene)).toBeCloseTo(4.2, 3);
  });

  it("스크린샷 회귀 (씬1 trim 4.7s, 씬2 trim 짧음): 비율이 합리적", () => {
    // 이슈 본문 스크린샷 예: 씬1 4.7s, 씬2 4.0s → 약 8.7s 합 (이슈 본문 "약 8.7s" 라벨과 일치).
    const s1 = makeScene({
      scene_id: 1,
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 4.7 },
    });
    const s2 = makeScene({
      scene_id: 2,
      footage: { type: "clip", file: "b.mp4", clip_start_sec: 0, clip_end_sec: 4.0 },
    });
    const d1 = getSceneDurationSec(s1, []);
    const d2 = getSceneDurationSec(s2, []);
    const total = d1 + d2;
    expect(total).toBeCloseTo(8.7, 1);
    // 씬1 이 씬2 보다 약간 길어야 (4.7 > 4.0) — 기존 버그에서는 비슷하거나 역전됐음.
    expect(d1).toBeGreaterThan(d2);
  });
});

// ── #378 — ① cache/timeline.json cut 길이 우선순위 ─────────────────────────
//
// `cache/timeline.json` 의 실제 cut 길이는 Phase 5 가 확정한 "참값" 이므로
// footage trim / TTS / hint 추정치를 모두 건너뛴다.
// 값이 0/NaN/음수처럼 비정상일 때는 자연 폴백(②~⑥)으로 회귀해야 한다.
describe("getSceneDurationSec — cutDurations 우선순위 (#378)", () => {
  it("cutDurations 만 있고 다른 것 없음 → cut 사용", () => {
    const scene = makeScene({ scene_id: 7 });
    const cuts = new Map<number, number>([[7, 5.2]]);
    expect(getSceneDurationSec(scene, undefined, cuts)).toBeCloseTo(5.2, 3);
  });

  it("cutDurations 와 TTS 둘 다 있음 → cut 우선 (TTS 무시)", () => {
    // TTS 가 더 길어도 Phase 5 가 확정한 cut 이 진실의 원천이므로 TTS 보다 우선.
    const scene = makeScene({
      scene_id: 3,
      tts: { audio_url: "u", duration_sec: 8.0, status: "ready", synced: false },
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 4.7 },
    });
    const cuts = new Map<number, number>([[3, 6.1]]);
    expect(getSceneDurationSec(scene, [], cuts)).toBeCloseTo(6.1, 3);
  });

  it("cutDurations 가 0/NaN/음수 → 폴백 (=footage trim 또는 TTS/hint)", () => {
    const sceneClip = makeScene({
      scene_id: 1,
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 4.7 },
    });
    // 0 → 폴백
    expect(getSceneDurationSec(sceneClip, [], new Map([[1, 0]]))).toBeCloseTo(4.7, 3);
    // NaN → 폴백
    expect(getSceneDurationSec(sceneClip, [], new Map([[1, Number.NaN]]))).toBeCloseTo(4.7, 3);
    // 음수 → 폴백
    expect(getSceneDurationSec(sceneClip, [], new Map([[1, -2.3]]))).toBeCloseTo(4.7, 3);
    // Infinity → 폴백 (isFinite 검사)
    expect(getSceneDurationSec(sceneClip, [], new Map([[1, Number.POSITIVE_INFINITY]]))).toBeCloseTo(4.7, 3);
  });

  it("cutDurations 미제공 → 기존 ②~⑥ 우선순위 그대로", () => {
    // 시그니처 확장이 회귀 없는지 확인 — cutDurations 인자를 안 넘기면 #375 동작과 동일.
    const sceneTrim = makeScene({
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 2.0, clip_end_sec: 6.5 },
      duration_hint_sec: 10.0,
    });
    expect(getSceneDurationSec(sceneTrim, [])).toBeCloseTo(4.5, 3);

    const sceneTts = makeScene({
      tts: { audio_url: "u", duration_sec: 4.7, status: "ready", synced: false },
    });
    expect(getSceneDurationSec(sceneTts)).toBeCloseTo(4.7, 3);

    const sceneEmpty = makeScene();
    expect(getSceneDurationSec(sceneEmpty)).toBeCloseTo(3.0, 3);
  });

  it("다중 씬 시나리오 — 씬1 cut=4.7s, 씬2 cut=4.0s → 합 8.7s, 비율 일관성", () => {
    // 이슈 본문 스크린샷 예시(씬1 4.7s, 씬2 4.0s) 를 cutDurations 로 직접 제공해도
    // 추정 폴백과 같은 결과가 나와야 한다 (Phase 5 확정값 = footage trim 추정값).
    const s1 = makeScene({ scene_id: 1 });
    const s2 = makeScene({ scene_id: 2 });
    const cuts = new Map<number, number>([
      [1, 4.7],
      [2, 4.0],
    ]);
    const d1 = getSceneDurationSec(s1, undefined, cuts);
    const d2 = getSceneDurationSec(s2, undefined, cuts);
    expect(d1).toBeCloseTo(4.7, 3);
    expect(d2).toBeCloseTo(4.0, 3);
    expect(d1 + d2).toBeCloseTo(8.7, 3);
    expect(d1).toBeGreaterThan(d2);
  });

  it("cutDurations 에 없는 scene_id → 폴백 (다른 씬은 cut, 미등록 씬은 추정)", () => {
    // Phase 5 가 일부 씬만 처리한 중간 상태(엣지). 등록된 씬은 cut, 미등록은 폴백.
    const s1 = makeScene({
      scene_id: 1,
      footage: { type: "clip", file: "a.mp4", clip_start_sec: 0, clip_end_sec: 4.7 },
    });
    const s2 = makeScene({
      scene_id: 2,
      duration_hint_sec: 6.0,
    });
    const cuts = new Map<number, number>([[1, 5.3]]);
    expect(getSceneDurationSec(s1, [], cuts)).toBeCloseTo(5.3, 3); // cut 사용
    expect(getSceneDurationSec(s2, [], cuts)).toBeCloseTo(6.0, 3); // 폴백 (hint)
  });
});
