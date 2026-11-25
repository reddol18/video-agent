/**
 * clientThumbnail (#307)
 * ─────────────────────────────────────────────────────────────────────────
 * 푸티지 썸네일을 브라우저에서 직접 추출한다. 백엔드 ffmpeg spawn 비용을
 * 줄이기 위한 클라이언트 오프로드 1단계.
 *
 * Phase A에서 도입된 RenderSpec과 무관하며 (썸네일 정확도 요구가 낮음),
 * `LocalThumbnail`(#272)에 흩어져 있던 추출 로직을 일반화하여 서버 푸티지에도
 * 재사용 가능하게 정리했다.
 *
 * 지원 소스 타입:
 * - 비디오: <video> 엘리먼트로 첫 프레임을 디코드 → canvas로 캡처
 * - 이미지: <img>로 디코드 → canvas로 리사이즈
 *
 * 오디오 파형(showwavespic) 썸네일은 백엔드 ffmpeg가 계속 담당한다.
 * (Web Audio API로 파형을 그리는 비용이 크고, 카드 UI 비중도 낮음.)
 */

const VIDEO_EXTS = [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"];
const IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"];

export type ClientThumbnailKind = "video" | "image" | "unsupported";

export function classifyFilename(name: string): ClientThumbnailKind {
  const ext = name.toLowerCase().match(/\.[^.]+$/)?.[0] ?? "";
  if (VIDEO_EXTS.includes(ext)) return "video";
  if (IMAGE_EXTS.includes(ext)) return "image";
  return "unsupported";
}

export interface ExtractOptions {
  /** 결과 폭(px). 기본 320, 80~1920 범위로 클램프. */
  width?: number;
  /** 비디오 프레임 캡처 시점(초). 기본 0.5초, 영상이 더 짧으면 절반 지점. */
  seekSec?: number;
  /** 추출 타임아웃(ms). 큰 영상에서 무한 대기를 막기 위함. 기본 8000ms. */
  timeoutMs?: number;
}

function clampWidth(w: number | undefined): number {
  const v = Math.round(w ?? 320);
  if (!isFinite(v) || v <= 0) return 320;
  return Math.max(80, Math.min(1920, v));
}

/** 빈 Blob을 받으면 즉시 에러 — 호출자가 잘못된 입력을 빠르게 인지하도록. */
function ensureNonEmptyBlob(blob: Blob): void {
  if (!(blob instanceof Blob)) {
    throw new Error("clientThumbnail: 입력이 Blob이 아닙니다");
  }
  if (blob.size === 0) {
    throw new Error("clientThumbnail: 빈 Blob입니다");
  }
}

/**
 * 비디오 Blob의 첫 프레임을 PNG dataURL로 추출한다.
 *
 * @throws Blob이 비어 있거나, 디코드 실패, 또는 timeout
 */
export async function extractFirstFrameFromVideoBlob(
  blob: Blob,
  options: ExtractOptions = {},
): Promise<string> {
  ensureNonEmptyBlob(blob);
  const url = URL.createObjectURL(blob);
  try {
    return await extractFirstFrameFromVideoUrl(url, options);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * 비디오 URL(blob: 혹은 http(s):) 첫 프레임을 PNG dataURL로 추출한다.
 *
 * 서버 푸티지의 경우 stream URL을 그대로 넘기면 <video> 엘리먼트가
 * Range 요청으로 필요한 만큼만 받아오므로 전체 다운로드를 강제하지 않는다.
 */
export function extractFirstFrameFromVideoUrl(
  url: string,
  options: ExtractOptions = {},
): Promise<string> {
  const width = clampWidth(options.width);
  const seekSec = options.seekSec ?? 0.5;
  const timeoutMs = options.timeoutMs ?? 8000;
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    video.crossOrigin = "anonymous";
    (video as unknown as { playsInline: boolean }).playsInline = true;

    let settled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try { fn(); } finally {
        try {
          video.removeAttribute("src");
          video.load();
        } catch { /* ignore cleanup errors */ }
      }
    };

    const fail = (msg: string) => finish(() => reject(new Error(msg)));

    video.onloadeddata = () => {
      const dur = video.duration;
      const target = isFinite(dur) && dur > 0
        ? Math.min(seekSec, dur / 2)
        : 0;
      try {
        video.currentTime = target;
      } catch {
        // currentTime 설정 실패 시 즉시 캡처 시도
        capture();
      }
    };

    video.onseeked = () => capture();

    video.onerror = () => fail("video load error");

    const capture = () => {
      if (settled) return;
      try {
        const aspect = video.videoWidth > 0 && video.videoHeight > 0
          ? video.videoHeight / video.videoWidth
          : 9 / 16;
        const h = Math.max(1, Math.round(width * aspect));
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          fail("canvas 2d context 생성 실패");
          return;
        }
        ctx.drawImage(video, 0, 0, width, h);
        const dataUrl = canvas.toDataURL("image/png");
        finish(() => resolve(dataUrl));
      } catch (err) {
        fail(err instanceof Error ? err.message : String(err));
      }
    };

    timer = setTimeout(() => fail(`thumbnail extract timeout (${timeoutMs}ms)`), timeoutMs);
    video.src = url;
  });
}

/** 이미지 Blob을 받아 width 폭의 PNG dataURL을 반환한다. */
export async function extractThumbnailFromImageBlob(
  blob: Blob,
  options: ExtractOptions = {},
): Promise<string> {
  ensureNonEmptyBlob(blob);
  const url = URL.createObjectURL(blob);
  try {
    return await extractThumbnailFromImageUrl(url, options);
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function extractThumbnailFromImageUrl(
  url: string,
  options: ExtractOptions = {},
): Promise<string> {
  const width = clampWidth(options.width);
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      try {
        const aspect = img.naturalWidth > 0 && img.naturalHeight > 0
          ? img.naturalHeight / img.naturalWidth
          : 9 / 16;
        const h = Math.max(1, Math.round(width * aspect));
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          reject(new Error("canvas 2d context 생성 실패"));
          return;
        }
        ctx.drawImage(img, 0, 0, width, h);
        resolve(canvas.toDataURL("image/png"));
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    };
    img.onerror = () => reject(new Error("image load error"));
    img.src = url;
  });
}

/**
 * File 객체에서 종류를 분류하고 첫 프레임을 추출한다.
 * `LocalThumbnail`(#272)에서 호출하는 래퍼.
 */
export async function extractThumbnailFromFile(
  file: File,
  options: ExtractOptions = {},
): Promise<string> {
  const kind = classifyFilename(file.name);
  if (kind === "video") return extractFirstFrameFromVideoBlob(file, options);
  if (kind === "image") return extractThumbnailFromImageBlob(file, options);
  throw new Error(`clientThumbnail: 지원되지 않는 파일 형식 (${file.name})`);
}
