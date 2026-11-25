import { describe, it, expect } from "vitest";
import { filterVoices, collectUseCases } from "../typecastVoiceFilter";
import type { TypecastVoice } from "../../api/client";

const fixture: TypecastVoice[] = [
  {
    voice_id: "tc_1",
    voice_name: "Sanghyun",
    gender: "male",
    age: "young_adult",
    use_cases: ["Game", "Anime"],
  },
  {
    voice_id: "tc_2",
    voice_name: "Jimin",
    gender: "female",
    age: "middle_age",
    use_cases: ["Audiobook/Storytelling", "Documentary"],
  },
  {
    voice_id: "tc_3",
    voice_name: "SANDY",
    gender: "female",
    age: "young_adult",
    use_cases: ["Game", "TikTok/Reels/Shorts"],
  },
  {
    voice_id: "tc_4",
    voice_name: "Minho",
    gender: "male",
    age: "elder",
    use_cases: ["News Reporter"],
  },
];

describe("filterVoices", () => {
  it("이름 부분일치는 대소문자를 무시한다", () => {
    const out = filterVoices(fixture, { q: "san" });
    expect(out.map((v) => v.voice_id).sort()).toEqual(["tc_1", "tc_3"]);
  });

  it("성별로 필터한다", () => {
    const out = filterVoices(fixture, { gender: "female" });
    expect(out.map((v) => v.voice_id).sort()).toEqual(["tc_2", "tc_3"]);
  });

  it("용도(use_case) 로 필터한다", () => {
    const out = filterVoices(fixture, { useCase: "Game" });
    expect(out.map((v) => v.voice_id).sort()).toEqual(["tc_1", "tc_3"]);
  });

  it("복합 조건은 AND 로 적용한다 (여성 + young_adult + Game)", () => {
    const out = filterVoices(fixture, {
      gender: "female",
      age: "young_adult",
      useCase: "Game",
    });
    expect(out.map((v) => v.voice_id)).toEqual(["tc_3"]);
  });

  it("빈 필터는 전체를 반환한다", () => {
    expect(filterVoices(fixture, {})).toHaveLength(4);
  });
});

describe("collectUseCases", () => {
  it("use_cases 를 빈도 내림차순으로 중복 제거한다", () => {
    const out = collectUseCases(fixture);
    // Game 2회로 최상위, 나머지는 1회 → 이름 오름차순.
    expect(out[0]).toBe("Game");
    expect(new Set(out).size).toBe(out.length);
    expect(out).toContain("Anime");
    expect(out).toContain("News Reporter");
  });
});
