import { describe, expect, it } from "vitest";
import type { Project, SceneCard } from "../../types";
import {
  extractSpeakerName,
  resolveSceneVoice,
  resolveSegmentVoice,
  setSpeakerTag,
} from "../speakers";

const scene = (tags: string[]): SceneCard => ({ scene_id: 1, tags } as unknown as SceneCard);
const proj = {
  tts_voice: "BASE",
  speakers: [{ name: "홍길동", voice_name: "VOICE_A" }],
} as Pick<Project, "tts_voice" | "speakers">;

describe("extractSpeakerName", () => {
  it("speaker: 접두사 태그만 화자로 읽는다", () => {
    expect(extractSpeakerName(["speaker:홍길동", "horror"])).toBe("홍길동");
    expect(extractSpeakerName(["horror", "witty"])).toBeNull();
  });

  it("대소문자와 앞뒤 공백을 무시한다", () => {
    expect(extractSpeakerName(["Speaker:  철수  "])).toBe("철수");
  });

  it("이름이 비면 화자로 보지 않는다", () => {
    expect(extractSpeakerName(["speaker:"])).toBeNull();
  });

  it("여러 개면 첫 번째를 쓴다", () => {
    expect(extractSpeakerName(["speaker:철수", "speaker:영희"])).toBe("철수");
  });
});

describe("resolveSceneVoice", () => {
  it("등록된 화자는 그 목소리로", () => {
    expect(resolveSceneVoice(scene(["speaker:홍길동"]), proj)).toEqual({
      voice: "VOICE_A",
      speakerName: "홍길동",
      known: true,
    });
  });

  it("미등록 이름은 기본 목소리로 떨어지되 known=false 로 알린다", () => {
    const r = resolveSceneVoice(scene(["speaker:없는사람"]), proj);
    expect(r.voice).toBe("BASE");
    expect(r.known).toBe(false);
  });

  it("화자 태그가 없으면 기본 목소리", () => {
    expect(resolveSceneVoice(scene(["horror"]), proj).voice).toBe("BASE");
  });
});

describe("resolveSegmentVoice", () => {
  it("segment 지정이 화자 태그를 이긴다", () => {
    expect(resolveSegmentVoice({ voice_name: "SEG_V" }, scene(["speaker:홍길동"]), proj)).toBe("SEG_V");
  });

  it("segment 지정이 없으면 화자 목소리를 물려받는다", () => {
    expect(resolveSegmentVoice({ voice_name: null }, scene(["speaker:홍길동"]), proj)).toBe("VOICE_A");
  });
});

describe("setSpeakerTag", () => {
  it("기존 효과 태그는 보존하고 화자 태그만 교체한다", () => {
    expect(setSpeakerTag(["speaker:철수", "horror"], "영희")).toEqual(["speaker:영희", "horror"]);
  });

  it("null 이면 화자 태그만 제거한다", () => {
    expect(setSpeakerTag(["speaker:철수", "horror"], null)).toEqual(["horror"]);
  });
});
