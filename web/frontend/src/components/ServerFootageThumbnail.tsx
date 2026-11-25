/**
 * ServerFootageThumbnail (#307)
 * ─────────────────────────────────────────────────────────────────────────
 * 서버에 업로드된 푸티지 자산의 카드 썸네일을 브라우저에서 직접 추출한다.
 *
 * 동작 순서:
 *   1) IndexedDB 캐시 조회 — hit이면 dataURL 즉시 표시
 *   2) miss면 video/image 추출 시도
 *      - 비디오: `/api/projects/{id}/footage/{filename}/stream` 을 <video src>로 로드.
 *        브라우저가 Range 요청으로 첫 청크만 받아 디코드하므로 전체 다운로드를 강제하지 않음.
 *      - 이미지: 같은 stream URL을 <img src>로 로드.
 *   3) 추출 성공 시 IndexedDB 캐시에 저장
 *   4) 추출 실패(타임아웃·디코드 에러·CORS 등) 시 백엔드 thumbnail 엔드포인트로 폴백
 *
 * 큰 파일에서 클라 추출이 느릴 경우를 대비해 `extractTimeoutMs` 로 cap을 둔다.
 * 이 시간을 넘기면 백엔드 fallback으로 즉시 전환된다.
 *
 * 오디오 자산(.mp3/.wav 등)은 파형 썸네일이 필요하므로 백엔드 엔드포인트를
 * 그대로 사용한다(클라 추출 시도 자체를 건너뜀).
 */

import { useEffect, useState } from "react";
import {
  classifyFilename,
  extractFirstFrameFromVideoUrl,
  extractThumbnailFromImageUrl,
} from "../lib/clientThumbnail";
import { getThumbnailCache, serverFootageCacheKey } from "../lib/indexedDbCache";

interface ServerFootageThumbnailProps {
  projectId: string;
  filename: string;
  /** 자산 유형. "audio"면 추출 시도 없이 곧장 백엔드 파형 썸네일을 사용한다. */
  assetType?: "video" | "image" | "audio";
  alt: string;
  style?: React.CSSProperties;
  /** 추출 타임아웃(ms). 큰 영상에서 UX 저해 방지. 기본 4000ms. */
  extractTimeoutMs?: number;
}

export default function ServerFootageThumbnail({
  projectId,
  filename,
  assetType,
  alt,
  style,
  extractTimeoutMs = 4000,
}: ServerFootageThumbnailProps) {
  const [dataUrl, setDataUrl] = useState<string | null>(null);
  const [fallback, setFallback] = useState<boolean>(false);
  const [hidden, setHidden] = useState<boolean>(false);

  // 백엔드 엔드포인트 — 클라 추출 실패 또는 audio 자산일 때 사용.
  const backendThumbUrl = `/api/projects/${projectId}/footage/${encodeURIComponent(filename)}/thumbnail`;
  const streamUrl = `/api/projects/${projectId}/footage/${encodeURIComponent(filename)}/stream`;

  useEffect(() => {
    let cancelled = false;
    setDataUrl(null);
    setFallback(false);
    setHidden(false);

    // 오디오는 백엔드 파형 썸네일을 그대로 사용.
    if (assetType === "audio") {
      setFallback(true);
      return () => { cancelled = true; };
    }

    // 명시 assetType이 없으면 파일명 확장자로 분류 (legacy 호환).
    const kind = assetType ?? classifyFilename(filename);
    if (kind === "unsupported") {
      setFallback(true);
      return () => { cancelled = true; };
    }

    const cache = getThumbnailCache();
    const cacheKey = serverFootageCacheKey(projectId, filename);

    (async () => {
      try {
        const cached = await cache.get(cacheKey);
        if (cancelled) return;
        if (cached) {
          setDataUrl(cached);
          return;
        }
      } catch {
        // 캐시 조회 실패는 무시하고 추출로 진행.
      }
      try {
        const extracted = kind === "video"
          ? await extractFirstFrameFromVideoUrl(streamUrl, { width: 320, timeoutMs: extractTimeoutMs })
          : await extractThumbnailFromImageUrl(streamUrl, { width: 320 });
        if (cancelled) return;
        setDataUrl(extracted);
        // 캐시 저장 실패는 표시에 영향 없음.
        getThumbnailCache().set(cacheKey, extracted).catch(() => {});
      } catch {
        // 추출 실패 시 백엔드 fallback.
        if (!cancelled) setFallback(true);
      }
    })();

    return () => { cancelled = true; };
    // assetType은 외부에서 안정적으로 들어오므로 deps 포함.
  }, [projectId, filename, assetType, extractTimeoutMs, streamUrl]);

  if (hidden) {
    return <div style={{ ...style, background: "#1a1a1a" }} aria-label={`${alt} (썸네일 없음)`} />;
  }

  if (dataUrl) {
    return <img src={dataUrl} alt={alt} loading="lazy" style={style} />;
  }

  if (fallback) {
    return (
      <img
        src={backendThumbUrl}
        alt={alt}
        loading="lazy"
        style={style}
        onError={() => setHidden(true)}
      />
    );
  }

  // 추출 진행 중 placeholder.
  return <div style={{ ...style, background: "#1a1a1a" }} aria-label={`${alt} (로딩 중)`} />;
}
