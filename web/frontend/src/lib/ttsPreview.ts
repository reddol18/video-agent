/**
 * TTS 목소리 미리듣기 훅
 * ─────────────────────────────────────────────────────────────
 * 콤보박스에서 목소리를 골라도 실제 톤을 알 수 없어 발생하는 UX 문제를 해결.
 * 짧은 샘플 문장을 즉석 합성 → Audio 재생. 재생 중 재클릭 시 정지.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { synthesizeTts } from "./tts";
import { resolveProvider } from "./ttsVoices";
import { previewTtsAudio } from "../api/client";

const SAMPLE_TEXT = "안녕하세요. 이 목소리로 나레이션이 만들어집니다.";

export function useTtsPreview() {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [playingVoice, setPlayingVoice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const stop = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = "";
      audioRef.current = null;
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
    setPlayingVoice(null);
  }, []);

  const preview = useCallback(async (voiceName: string, speakingRate?: number) => {
    // 같은 목소리 재생 중이면 정지 토글.
    if (playingVoice === voiceName) {
      stop();
      return;
    }
    stop();
    setError(null);
    setLoading(true);
    try {
      // Typecast voice(tc_/uc_) 는 브라우저에서 Google REST 를 못 타므로 백엔드
      // /api/tts/preview 로 위임(provider 분기). Google 은 기존 직접 호출 유지(무회귀).
      const audioBlob =
        resolveProvider(voiceName) === "typecast"
          ? await previewTtsAudio(voiceName, speakingRate)
          : (await synthesizeTts(SAMPLE_TEXT, voiceName, speakingRate)).audioBlob;
      const url = URL.createObjectURL(audioBlob);
      urlRef.current = url;
      const audio = new Audio(url);
      audioRef.current = audio;
      audio.onended = () => {
        setPlayingVoice(null);
        if (urlRef.current === url) {
          URL.revokeObjectURL(url);
          urlRef.current = null;
        }
        audioRef.current = null;
      };
      setPlayingVoice(voiceName);
      await audio.play();
    } catch (e) {
      setError((e as Error).message);
      setPlayingVoice(null);
    } finally {
      setLoading(false);
    }
  }, [playingVoice, stop]);

  useEffect(() => {
    return () => { stop(); };
  }, [stop]);

  return { preview, stop, loading, playingVoice, error };
}
