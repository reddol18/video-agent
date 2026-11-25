/**
 * Typecast voice 검색·필터 순수 로직.
 * ─────────────────────────────────────────────────────────────
 * TypecastVoiceModal 이 596개 voice 를 클라이언트 메모리에서 필터링할 때 쓰는
 * 부수효과 없는 함수들. React/네트워크와 분리해 vitest 로 단위 테스트한다.
 *
 * 언어 필드는 API 에 없으므로(voice_name 이 전부 로마자) 언어 필터/추측은 만들지 않는다.
 * 사용자가 미리듣기로 귀 확인하는 것이 정책.
 */
import type { TypecastVoice } from "../api/client";

/** age enum → 한글 라벨. API 에 없는 값은 원문 그대로 노출. */
export const AGE_LABELS: Record<string, string> = {
  young_adult: "청년",
  middle_age: "중년",
  child: "아동",
  teenager: "청소년",
  elder: "노년",
};

/** age enum 선택지 순서(전체 제외). 분포 많은 순. */
export const AGE_ORDER = ["young_adult", "middle_age", "child", "teenager", "elder"] as const;

/** gender enum → 한글 라벨. */
export const GENDER_LABELS: Record<string, string> = {
  male: "남",
  female: "여",
};

export interface VoiceFilter {
  /** 이름 부분일치(대소문자 무시). 빈 문자열이면 이름 조건 없음. */
  q?: string;
  /** "male" | "female" | 빈 문자열(전체). */
  gender?: string;
  /** age enum | 빈 문자열(전체). */
  age?: string;
  /** use_cases 중 하나와 정확히 일치 | 빈 문자열(전체). */
  useCase?: string;
}

/** age 라벨 조회 — 미매핑 값은 원문. */
export function ageLabel(age: string | undefined | null): string {
  const a = (age || "").trim();
  if (!a) return "";
  return AGE_LABELS[a] ?? a;
}

/** gender 라벨 조회 — 미매핑 값은 원문. */
export function genderLabel(gender: string | undefined | null): string {
  const g = (gender || "").trim();
  if (!g) return "";
  return GENDER_LABELS[g] ?? g;
}

/**
 * voices 에서 나타나는 use_cases 를 빈도 내림차순으로 중복 제거해 반환.
 * 필터 셀렉트 옵션 구성용. (스키마상 13종이지만 데이터 기준으로 동적 산출.)
 */
export function collectUseCases(voices: TypecastVoice[]): string[] {
  const count = new Map<string, number>();
  for (const v of voices) {
    for (const uc of v.use_cases ?? []) {
      const key = (uc || "").trim();
      if (!key) continue;
      count.set(key, (count.get(key) ?? 0) + 1);
    }
  }
  return [...count.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([k]) => k);
}

/**
 * 다중 조건 AND 필터. 각 조건은 빈 값이면 무시.
 *  - q: voice_name 부분일치(대소문자 무시). voice_name 없으면 voice_id 로 폴백.
 *  - gender: 정확히 일치.
 *  - age: 정확히 일치.
 *  - useCase: use_cases 배열에 포함.
 */
export function filterVoices(voices: TypecastVoice[], f: VoiceFilter): TypecastVoice[] {
  const q = (f.q || "").trim().toLowerCase();
  const gender = (f.gender || "").trim();
  const age = (f.age || "").trim();
  const useCase = (f.useCase || "").trim();

  return voices.filter((v) => {
    if (q) {
      const name = (v.voice_name || v.voice_id || "").toLowerCase();
      if (!name.includes(q)) return false;
    }
    if (gender && (v.gender || "") !== gender) return false;
    if (age && (v.age || "") !== age) return false;
    if (useCase && !(v.use_cases ?? []).includes(useCase)) return false;
    return true;
  });
}
