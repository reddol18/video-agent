/**
 * LocalThumbnail (#272, #307 리팩토링)
 * ─────────────────────────────────────────────────────────────────────────
 * 브라우저 메모리상의 `File` 객체로부터 직접 썸네일을 만든다.
 * - 비디오: video 엘리먼트로 첫 프레임을 디코드하여 canvas로 캡처.
 * - 이미지: 그대로 ObjectURL로 표시.
 * - 결과는 IndexedDB `localThumbnailCache`에 `${filename}:${lastModified}` 키로 캐시.
 *   같은 파일이면 페이지 새로고침 후에도 즉시 로드되고, 파일이 교체되면 자동 무효화된다.
 *
 * 서버에는 없는 사용자 PC 로컬 파일(footage_dir 미복사)도 썸네일이 표시되도록 한다.
 *
 * #307: 추출/캐시 로직을 `lib/clientThumbnail.ts`와 `lib/indexedDbCache.ts`로 분리.
 *       서버 푸티지에도 동일 추출 인프라를 재사용하기 위함.
 */

import { useEffect, useState } from "react";
import { extractThumbnailFromFile } from "../lib/clientThumbnail";
import { getThumbnailCache } from "../lib/indexedDbCache";

interface LocalThumbnailProps {
  file: File;
  alt: string;
  style?: React.CSSProperties;
}

export default function LocalThumbnail({ file, alt, style }: LocalThumbnailProps) {
  const [dataUrl, setDataUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setDataUrl(null);
    const cacheKey = `${file.name}:${file.lastModified}`;
    const cache = getThumbnailCache();

    (async () => {
      try {
        const cached = await cache.get(cacheKey);
        if (cancelled) return;
        if (cached) {
          setDataUrl(cached);
          return;
        }
        const result = await extractThumbnailFromFile(file);
        if (cancelled) return;
        setDataUrl(result);
        // 캐시 저장 실패는 사용자 경험에 영향 없으므로 무시.
        cache.set(cacheKey, result).catch(() => {});
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [file]);

  if (failed) {
    return <div style={{ ...style, background: "#222" }} aria-label={`${alt} (썸네일 생성 실패)`} />;
  }
  if (!dataUrl) {
    return <div style={{ ...style, background: "#1a1a1a" }} aria-label={`${alt} (로딩 중)`} />;
  }
  return <img src={dataUrl} alt={alt} loading="lazy" style={style} />;
}
