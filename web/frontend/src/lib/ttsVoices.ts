/**
 * Google Cloud TTS 한국어(ko-KR) 목소리 목록.
 * ─────────────────────────────────────────────────────────────
 * 2026-07 API `voices.list?languageCode=ko-KR` 조회 기준.
 *   Chirp3-HD: 30개 (남 16 + 여 14) — 최신, 자연스러움 최우선.
 *   Neural2: 3개, Wavenet: 4개 — 이전 세대 고품질.
 *   Standard: 4개 — 구형, 저비용.
 * 총 41개.
 *
 * Editor 와 Settings 두 화면이 동일한 목록을 써야 하므로 공통 모듈로 분리.
 * (과거에는 두 곳에 서로 다른 부분 목록이 하드코딩되어 있어 동기화 부담이 있었다.)
 */
export interface TtsVoice {
  value: string;
  label: string;
  /** 공급자 구분. 미지정이면 google(기본). */
  provider?: "google" | "typecast";
}

/** voice_id/voice_name 프리픽스로 공급자 판별 (백엔드 resolve_provider 와 동일 규약). */
export function resolveProvider(voice: string | null | undefined): "google" | "typecast" {
  const v = (voice || "").trim().toLowerCase();
  if (v.startsWith("tc_") || v.startsWith("uc_")) return "typecast";
  return "google";
}

/**
 * Typecast voice 목록을 백엔드 프록시로 동적 로드해 TtsVoice[] 로 변환한다.
 * (Google 목록은 하드코딩 TTS_VOICES 를 그대로 사용하고, 이 결과와 병합해서 쓴다.)
 *
 * getTypecastVoices 는 순환 임포트를 피하기 위해 호출부에서 주입한다.
 */
export async function loadTypecastVoices(
  fetcher: () => Promise<
    Array<{ voice_id: string; voice_name?: string; gender?: string }>
  >,
): Promise<TtsVoice[]> {
  try {
    const raw = await fetcher();
    return raw
      .filter((v) => !!v.voice_id)
      .map((v) => {
        const name = v.voice_name || v.voice_id;
        const gender = v.gender ? ` (${v.gender})` : "";
        return {
          value: v.voice_id,
          label: `${name}${gender} · Typecast`,
          provider: "typecast" as const,
        };
      });
  } catch {
    return [];
  }
}

/**
 * 하드코딩 Google 목록 + 동적 Typecast 목록을 병합한다(순수 함수 — 테스트 용이).
 *
 *  - value 중복 제거: 같은 value 가 여러 번 나오면 먼저 나온 것만 유지.
 *  - 현재 선택값(currentValue)이 병합 목록에 없으면(예: Typecast 로드 전 tc_ 값 선택 상태)
 *    콤보박스가 빈값이 되지 않도록 임시 option 을 맨 앞에 끼워 값을 유지한다.
 */
export function mergeVoiceOptions(
  base: TtsVoice[],
  typecast: TtsVoice[],
  currentValue?: string | null,
): TtsVoice[] {
  const seen = new Set<string>();
  const out: TtsVoice[] = [];
  for (const v of [...base, ...typecast]) {
    if (!v.value || seen.has(v.value)) continue;
    seen.add(v.value);
    out.push(v);
  }
  const cur = (currentValue || "").trim();
  if (cur && !seen.has(cur)) {
    out.unshift({
      value: cur,
      label: `${cur} (로드 중…)`,
      provider: resolveProvider(cur),
    });
  }
  return out;
}

export const TTS_VOICES: TtsVoice[] = [
  // ─── Chirp3 HD 남성 (16) ─────────────────────────────
  { value: "ko-KR-Chirp3-HD-Achird",        label: "Achird (남성)" },
  { value: "ko-KR-Chirp3-HD-Algenib",       label: "Algenib (남성)" },
  { value: "ko-KR-Chirp3-HD-Algieba",       label: "Algieba (남성)" },
  { value: "ko-KR-Chirp3-HD-Alnilam",       label: "Alnilam (남성)" },
  { value: "ko-KR-Chirp3-HD-Charon",        label: "Charon (남성, 중후)" },
  { value: "ko-KR-Chirp3-HD-Enceladus",     label: "Enceladus (남성)" },
  { value: "ko-KR-Chirp3-HD-Fenrir",        label: "Fenrir (남성)" },
  { value: "ko-KR-Chirp3-HD-Iapetus",       label: "Iapetus (남성)" },
  { value: "ko-KR-Chirp3-HD-Orus",          label: "Orus (남성, 차분)" },
  { value: "ko-KR-Chirp3-HD-Puck",          label: "Puck (남성)" },
  { value: "ko-KR-Chirp3-HD-Rasalgethi",    label: "Rasalgethi (남성)" },
  { value: "ko-KR-Chirp3-HD-Sadachbia",     label: "Sadachbia (남성)" },
  { value: "ko-KR-Chirp3-HD-Sadaltager",    label: "Sadaltager (남성)" },
  { value: "ko-KR-Chirp3-HD-Schedar",       label: "Schedar (남성)" },
  { value: "ko-KR-Chirp3-HD-Umbriel",       label: "Umbriel (남성)" },
  { value: "ko-KR-Chirp3-HD-Zubenelgenubi", label: "Zubenelgenubi (남성)" },
  // ─── Chirp3 HD 여성 (14) ─────────────────────────────
  { value: "ko-KR-Chirp3-HD-Achernar",      label: "Achernar (여성)" },
  { value: "ko-KR-Chirp3-HD-Aoede",         label: "Aoede (여성, 밝음)" },
  { value: "ko-KR-Chirp3-HD-Autonoe",       label: "Autonoe (여성)" },
  { value: "ko-KR-Chirp3-HD-Callirrhoe",    label: "Callirrhoe (여성)" },
  { value: "ko-KR-Chirp3-HD-Despina",       label: "Despina (여성)" },
  { value: "ko-KR-Chirp3-HD-Erinome",       label: "Erinome (여성)" },
  { value: "ko-KR-Chirp3-HD-Gacrux",        label: "Gacrux (여성)" },
  { value: "ko-KR-Chirp3-HD-Kore",          label: "Kore (여성)" },
  { value: "ko-KR-Chirp3-HD-Laomedeia",     label: "Laomedeia (여성)" },
  { value: "ko-KR-Chirp3-HD-Leda",          label: "Leda (여성, 부드러움)" },
  { value: "ko-KR-Chirp3-HD-Pulcherrima",   label: "Pulcherrima (여성)" },
  { value: "ko-KR-Chirp3-HD-Sulafat",       label: "Sulafat (여성)" },
  { value: "ko-KR-Chirp3-HD-Vindemiatrix",  label: "Vindemiatrix (여성)" },
  { value: "ko-KR-Chirp3-HD-Zephyr",        label: "Zephyr (여성)" },
  // ─── Neural2 (3) ─────────────────────────────────────
  { value: "ko-KR-Neural2-A",               label: "Neural2-A (여성)" },
  { value: "ko-KR-Neural2-B",               label: "Neural2-B (여성)" },
  { value: "ko-KR-Neural2-C",               label: "Neural2-C (남성)" },
  // ─── Wavenet (4) ─────────────────────────────────────
  { value: "ko-KR-Wavenet-A",               label: "Wavenet-A (여성)" },
  { value: "ko-KR-Wavenet-B",               label: "Wavenet-B (여성)" },
  { value: "ko-KR-Wavenet-C",               label: "Wavenet-C (남성)" },
  { value: "ko-KR-Wavenet-D",               label: "Wavenet-D (남성)" },
  // ─── Standard (구형, 저품질/저비용) (4) ──────────────
  { value: "ko-KR-Standard-A",              label: "Standard-A (여성)" },
  { value: "ko-KR-Standard-B",              label: "Standard-B (여성)" },
  { value: "ko-KR-Standard-C",              label: "Standard-C (남성)" },
  { value: "ko-KR-Standard-D",              label: "Standard-D (남성)" },
];

import { useEffect, useState } from "react";
import { getTypecastVoices, type TypecastVoice } from "../api/client";

// 모듈 레벨 캐시 — Editor 안 여러 컴포넌트(프로젝트 select / TtsStartModal / segment inspector /
// TypecastVoiceModal)가 각자 훅을 호출해도 Typecast 프록시는 세션당 1회만 부른다
// (596개, 크레딧 무관하지만 네트워크 절약). raw(원본)와 mapped(콤보박스용) 둘 다 같은 1회
// 네트워크 결과에서 파생한다.
let _typecastRawCache: TypecastVoice[] | null = null;
let _typecastRawPromise: Promise<TypecastVoice[]> | null = null;
let _typecastCache: TtsVoice[] | null = null;

/** raw Typecast voice(gender/age/use_cases 포함) 를 세션당 1회 로드. 실패 시 빈 배열. */
function fetchTypecastRawOnce(): Promise<TypecastVoice[]> {
  if (_typecastRawCache) return Promise.resolve(_typecastRawCache);
  if (_typecastRawPromise) return _typecastRawPromise;
  _typecastRawPromise = getTypecastVoices()
    .then((list) => {
      _typecastRawCache = Array.isArray(list) ? list : [];
      return _typecastRawCache;
    })
    .catch((err) => {
      // 실패해도 앱이 깨지지 않도록 빈 목록으로 회귀(Google 목록만 노출).
      console.warn("[ttsVoices] Typecast voice 로드 실패 — Google 목록만 사용", err);
      _typecastRawCache = [];
      return [];
    });
  return _typecastRawPromise;
}

function fetchTypecastVoicesOnce(): Promise<TtsVoice[]> {
  if (_typecastCache) return Promise.resolve(_typecastCache);
  return fetchTypecastRawOnce().then((raw) => {
    // loadTypecastVoices 는 fetcher 를 주입받는다 — 이미 로드된 raw 를 그대로 넘겨
    // 추가 네트워크 없이 mapped 목록으로 변환한다.
    return loadTypecastVoices(async () => raw).then((list) => {
      _typecastCache = list;
      return list;
    });
  });
}

/**
 * Google(하드코딩) + Typecast(동적) 병합 목소리 목록을 돌려주는 훅.
 * 마운트 시 Typecast 를 1회 로드하고, 실패/빈 응답이어도 Google 목록은 항상 보장한다.
 */
export function useVoiceOptions(currentValue?: string | null): {
  voices: TtsVoice[];
  typecast: TtsVoice[];
  loading: boolean;
} {
  const [typecast, setTypecast] = useState<TtsVoice[]>(_typecastCache ?? []);
  const [loading, setLoading] = useState<boolean>(_typecastCache === null);

  useEffect(() => {
    if (_typecastCache !== null) {
      setTypecast(_typecastCache);
      setLoading(false);
      return;
    }
    let alive = true;
    fetchTypecastVoicesOnce().then((list) => {
      if (!alive) return;
      setTypecast(list);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, []);

  return {
    voices: mergeVoiceOptions(TTS_VOICES, typecast, currentValue),
    typecast,
    loading,
  };
}

/**
 * 원본 Typecast voice 목록(gender/age/use_cases 포함)을 돌려주는 훅.
 * TypecastVoiceModal 의 필터/검색용. useVoiceOptions 와 동일한 세션 캐시를 공유해
 * 네트워크는 세션당 1회만 발생한다. 로드 실패 시 error=true, voices=[].
 */
export function useTypecastRawVoices(): {
  voices: TypecastVoice[];
  loading: boolean;
  error: boolean;
} {
  const [voices, setVoices] = useState<TypecastVoice[]>(_typecastRawCache ?? []);
  const [loading, setLoading] = useState<boolean>(_typecastRawCache === null);
  const [error, setError] = useState<boolean>(false);

  useEffect(() => {
    if (_typecastRawCache !== null) {
      setVoices(_typecastRawCache);
      setLoading(false);
      setError(_typecastRawCache.length === 0);
      return;
    }
    let alive = true;
    fetchTypecastRawOnce().then((list) => {
      if (!alive) return;
      setVoices(list);
      setLoading(false);
      setError(list.length === 0);
    });
    return () => {
      alive = false;
    };
  }, []);

  return { voices, loading, error };
}
