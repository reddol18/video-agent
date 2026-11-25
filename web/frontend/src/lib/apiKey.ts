/**
 * Google API 키 조회 — 서버를 진실의 원천으로 삼는다(웹서비스 전환).
 *
 * 키는 서버 .env(GOOGLE_API_KEY)에 저장되며, 클라이언트는 /api/settings에서 가져온다.
 * 서버 미응답(오프라인 등) 시에만 클라이언트 IndexedDB 설정으로 폴백한다.
 * Gemini 매칭·TTS 등 브라우저 직접 호출 경로가 공용으로 사용한다.
 */
// 메모리 캐시 — Gemini/TTS는 키를 호출마다 조회하므로, 매번 /api/settings 왕복하면
// 매칭·TTS 다건 처리 시 네트워크 지연이 누적된다. 짧은 TTL로 캐싱한다.
let _cachedKey: string | null = null;
let _cachedAt = 0;
const _KEY_TTL_MS = 60_000;

/** 설정 변경(키 입력) 직후 즉시 반영하려면 호출해 캐시를 비운다. */
export function invalidateGoogleApiKeyCache(): void {
  _cachedKey = null;
  _cachedAt = 0;
}

export async function getGoogleApiKey(): Promise<string> {
  const nowMs = performance.now();
  if (_cachedKey && nowMs - _cachedAt < _KEY_TTL_MS) return _cachedKey;

  // 1) 서버 우선
  try {
    const { getSettings } = await import("../api/client");
    const s = await getSettings();
    if (s?.google_api_key?.trim()) {
      _cachedKey = s.google_api_key;
      _cachedAt = nowMs;
      return _cachedKey;
    }
  } catch {
    /* 서버 미응답 — 로컬 폴백 */
  }
  // 2) 로컬 IndexedDB 폴백
  try {
    const { dbGetSettings } = await import("./db");
    const s = await dbGetSettings();
    if (s?.google_api_key?.trim()) {
      _cachedKey = s.google_api_key;
      _cachedAt = nowMs;
      return _cachedKey;
    }
  } catch {
    /* ignore */
  }
  throw new Error("Google API Key가 설정되지 않았습니다. 설정에서 입력해주세요.");
}
