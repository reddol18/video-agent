/**
 * useTrackWaveform (#312)
 *
 * BGM 트랙 막대에 표시할 파형 피크 데이터를 IndexedDB 캐시 + 클라이언트 디코딩으로
 * 가져온다. 컴포넌트는 결과만 받으면 되고, 캐시 적중/실패는 훅 내부에서 처리.
 *
 * 흐름:
 *   1) IndexedDB(audioWaveformCache) 에서 (projectId, assetId, peakCount) 조회
 *   2) 히트 → 즉시 setPeaks
 *   3) 미스 → 자산 스트림 URL fetch → blob → extractWaveform → setPeaks → 캐시 저장
 *   4) 실패 시 peaks=null 유지 (캔버스는 placeholder 렌더)
 *
 * AbortController 로 cleanup. 컴포넌트가 사라지면 진행 중인 fetch 와 decode 를 모두
 * 취소한다(decode 는 AudioContext.close 로 GC).
 */
import { useEffect, useState } from "react";
import { extractWaveform, isWaveformDataSane } from "../lib/clientAudioWaveform";
import {
  dbGetAudioWaveform,
  dbSaveAudioWaveform,
} from "../lib/db";
import { studioAssetStreamUrl } from "../api/client";

export interface UseTrackWaveformResult {
  /** 0~1 정규화 peak 배열. 로딩 중 / 실패 시 null. */
  peaks: number[] | null;
  /** 디코딩된 오디오 길이(초). 로딩 중 / 실패 시 null. */
  duration: number | null;
  /** 진행 상태. UI 가 인디케이터를 표시할 때 사용. */
  status: "idle" | "loading" | "ready" | "error";
  /** 실패 시 에러 메시지(개발자 콘솔용). */
  error: string | null;
}

export function useTrackWaveform(
  projectId: string,
  assetId: string | null | undefined,
  peakCount: number,
): UseTrackWaveformResult {
  const [peaks, setPeaks] = useState<number[] | null>(null);
  const [duration, setDuration] = useState<number | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId || !assetId) {
      setPeaks(null);
      setDuration(null);
      setStatus("idle");
      setError(null);
      return;
    }

    const ac = new AbortController();
    let cancelled = false;

    setStatus("loading");
    setError(null);

    (async () => {
      try {
        // ── 1) IndexedDB 캐시 조회 ────────────────────────────────────────
        const cached = await dbGetAudioWaveform(projectId, assetId, peakCount).catch(
          () => undefined,
        );
        if (cancelled) return;
        if (cached && isWaveformDataSane(cached)) {
          setPeaks(cached.peaks);
          setDuration(cached.duration);
          setStatus("ready");
          return;
        }

        // ── 2) 자산 스트림 다운로드 ──────────────────────────────────────
        const url = studioAssetStreamUrl(projectId, assetId);
        const res = await fetch(url, { signal: ac.signal });
        if (!res.ok) {
          throw new Error(`자산 다운로드 실패 HTTP ${res.status}`);
        }
        const blob = await res.blob();
        if (cancelled) return;
        if (blob.size === 0) {
          throw new Error("자산이 비어 있음");
        }

        // ── 3) Web Audio API 로 파형 추출 ────────────────────────────────
        const waveform = await extractWaveform(blob, {
          peakCount,
          signal: ac.signal,
        });
        if (cancelled) return;

        setPeaks(waveform.peaks);
        setDuration(waveform.duration);
        setStatus("ready");

        // ── 4) 캐시 저장 (fire-and-forget) ───────────────────────────────
        dbSaveAudioWaveform(projectId, assetId, peakCount, {
          peaks: waveform.peaks,
          duration: waveform.duration,
          sample_rate: waveform.sample_rate,
          channels: waveform.channels,
        }).catch((e) => {
          // 캐시 저장 실패는 무시. 다음 마운트 때 다시 추출되면 됨.
          console.warn("[useTrackWaveform] 캐시 저장 실패(무시):", e);
        });
      } catch (e) {
        if (cancelled || ac.signal.aborted) return;
        const msg = e instanceof Error ? e.message : String(e);
        // 사용자 흐름에 영향이 없도록 콘솔만. UI 는 placeholder.
        console.warn("[useTrackWaveform] 파형 로드 실패:", msg);
        setError(msg);
        setStatus("error");
        setPeaks(null);
        setDuration(null);
      }
    })();

    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [projectId, assetId, peakCount]);

  return { peaks, duration, status, error };
}
