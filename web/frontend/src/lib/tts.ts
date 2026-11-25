/**
 * Google Cloud TTS REST API 브라우저 직접 호출
 * Chirp HD 음성 지원
 */
import { dbGetSettings, dbSaveTtsAudio, dbGetTtsAudio } from "./db";
import { getGoogleApiKey } from "./apiKey";

export interface TtsResult {
  audioBlob: Blob;
  durationSec: number;
}

export async function synthesizeTts(
  text: string,
  voiceName?: string,
  speakingRate?: number
): Promise<TtsResult> {
  // API 키는 서버 우선(웹서비스 전환). voice/rate 기본값은 로컬 설정에서 보완.
  const apiKey = await getGoogleApiKey();
  const settings = await dbGetSettings();

  const voice = voiceName ?? settings.tts_voice_name ?? "ko-KR-Chirp3-HD-Orus";
  const rate = speakingRate ?? parseFloat(settings.tts_speaking_rate ?? "0.9");
  const languageCode = voice.split("-").slice(0, 2).join("-");

  // Chirp HD 계열은 speakingRate 미지원
  const isChirpHd = voice.includes("Chirp3-HD");

  const body = {
    input: { text },
    voice: { languageCode, name: voice },
    audioConfig: {
      audioEncoding: "MP3",
      ...(isChirpHd ? {} : { speakingRate: rate }),
    },
  };

  const res = await fetch(
    `https://texttospeech.googleapis.com/v1/text:synthesize?key=${apiKey}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(`TTS 오류: ${(err as Record<string, { message: string }>).error?.message ?? res.statusText}`);
  }

  const data = await res.json() as { audioContent: string };
  const binary = atob(data.audioContent);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const audioBlob = new Blob([bytes], { type: "audio/mpeg" });

  // MP3 duration 측정 — AudioContext로 디코딩
  const durationSec = await measureAudioDuration(audioBlob);

  return { audioBlob, durationSec };
}

/** TTS 합성 후 IndexedDB에 저장하고 blob URL 반환 */
export async function synthesizeTtsAndSave(
  projectId: string,
  sceneId: number,
  text: string,
  voiceName?: string,
  speakingRate?: number
): Promise<{ blobUrl: string; durationSec: number }> {
  const { audioBlob, durationSec } = await synthesizeTts(text, voiceName, speakingRate);
  await dbSaveTtsAudio(projectId, sceneId, audioBlob);
  // 서버 영구 저장(웹서비스 전환) — 다른 PC/origin에서도 같은 TTS를 쓰도록.
  // 실패해도 IndexedDB는 이미 저장됐고, 미리보기 시점 폴백 업로드가 보장한다.
  try {
    const { uploadSceneTts } = await import("../api/client");
    await uploadSceneTts(projectId, sceneId, audioBlob);
  } catch (e) {
    console.warn("[tts] 서버 영구 저장 실패(로컬 저장됨):", e);
  }
  const blobUrl = URL.createObjectURL(audioBlob);
  return { blobUrl, durationSec };
}

/** TTS blob URL — IndexedDB 우선, 없으면 서버에서 받아 IndexedDB에 저장 후 URL 반환. */
export async function getTtsBlobUrl(projectId: string, sceneId: number): Promise<string | null> {
  const local = await dbGetTtsAudio(projectId, sceneId);
  if (local) return URL.createObjectURL(local);
  // 서버 폴백 (다른 PC/origin에서 처음 열 때)
  try {
    const res = await fetch(`/api/projects/${projectId}/tts/${sceneId}/audio`);
    if (!res.ok) return null;
    const blob = await res.blob();
    if (blob.size === 0) return null;
    await dbSaveTtsAudio(projectId, sceneId, blob).catch(() => {});
    return URL.createObjectURL(blob);
  } catch {
    return null;
  }
}

export async function measureAudioDuration(blob: Blob): Promise<number> {
  try {
    const ctx = new AudioContext();
    const arrayBuffer = await blob.arrayBuffer();
    const decoded = await ctx.decodeAudioData(arrayBuffer);
    await ctx.close();
    return decoded.duration;
  } catch {
    return 0;
  }
}
