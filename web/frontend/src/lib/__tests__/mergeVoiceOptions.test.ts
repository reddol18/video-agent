import { describe, it, expect } from "vitest";
import { mergeVoiceOptions, type TtsVoice } from "../ttsVoices";

const google: TtsVoice[] = [
  { value: "ko-KR-Chirp3-HD-Orus", label: "Orus (남성)" },
  { value: "ko-KR-Chirp3-HD-Leda", label: "Leda (여성)" },
];
const typecast: TtsVoice[] = [
  { value: "tc_daegil", label: "대길 (male) · Typecast", provider: "typecast" },
  { value: "tc_other", label: "기타 · Typecast", provider: "typecast" },
];

describe("mergeVoiceOptions", () => {
  it("Google 뒤에 Typecast 를 붙여 병합한다", () => {
    const out = mergeVoiceOptions(google, typecast);
    expect(out.map((v) => v.value)).toEqual([
      "ko-KR-Chirp3-HD-Orus",
      "ko-KR-Chirp3-HD-Leda",
      "tc_daegil",
      "tc_other",
    ]);
  });

  it("value 중복은 먼저 나온 것만 유지한다", () => {
    const dup: TtsVoice[] = [{ value: "tc_daegil", label: "중복", provider: "typecast" }];
    const out = mergeVoiceOptions(google, [...typecast, ...dup]);
    const values = out.map((v) => v.value);
    expect(values.filter((v) => v === "tc_daegil").length).toBe(1);
  });

  it("현재 선택값이 목록에 없으면 임시 option 으로 맨 앞에 유지한다", () => {
    const out = mergeVoiceOptions(google, [], "tc_missing");
    expect(out[0].value).toBe("tc_missing");
    expect(out[0].provider).toBe("typecast");
  });

  it("현재 선택값이 이미 목록에 있으면 임시 option 을 추가하지 않는다", () => {
    const out = mergeVoiceOptions(google, typecast, "tc_daegil");
    expect(out.filter((v) => v.value === "tc_daegil").length).toBe(1);
    expect(out[0].value).toBe("ko-KR-Chirp3-HD-Orus");
  });

  it("Typecast 가 비어도 Google 목록은 그대로 보장한다(회귀 방지)", () => {
    const out = mergeVoiceOptions(google, []);
    expect(out.map((v) => v.value)).toEqual([
      "ko-KR-Chirp3-HD-Orus",
      "ko-KR-Chirp3-HD-Leda",
    ]);
  });
});
