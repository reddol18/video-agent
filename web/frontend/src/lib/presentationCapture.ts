/**
 * presentationCapture (#313)
 * ─────────────────────────────────────────────────────────────────────────
 * 프레젠테이션(PDF/HTML) 클라이언트 캡처 통합 진입점.
 *
 * - 파일 유형 자동 판별 (PDF → clientPdfCapture, HTML → clientHtmlCapture).
 * - 결과: 페이지별 PNG Blob[] + 메타 (총 페이지 수, 경고, 사용된 모드).
 * - 실패 시 호출측이 백엔드 fallback (`uploadPresentation`) 호출 가능.
 *
 * lazy loading:
 * - 이 모듈 자체를 호출하는 시점이 곧 lazy load 트리거이므로, 호출측에서
 *   `await import("../lib/presentationCapture")` 형태로 import 할 것.
 */

import {
  capturePdfPages,
  isPdfBlob,
  type PdfCaptureOptions,
} from "./clientPdfCapture";
import {
  captureHtmlBlob,
  isHtmlBlob,
  type HtmlCaptureOptions,
} from "./clientHtmlCapture";

export type CaptureMode = "pdf" | "html";

export interface PresentationCaptureOptions {
  /** 캡처 모드 강제 지정 — 미지정 시 파일 확장자/Mime 으로 자동 판별. */
  mode?: CaptureMode;
  /** PDF 모드일 때 — 렌더 배율 (기본 1.5). */
  pdfScale?: number;
  /** HTML 모드일 때 — 캡처 viewport (기본 1920×1080). */
  htmlWidth?: number;
  htmlHeight?: number;
  /** 외부 중단 신호 (사용자 취소). */
  signal?: AbortSignal;
  /** 페이지별 진행률 콜백. 1-based. */
  onProgress?: (current: number, total: number) => void;
}

export interface PresentationCaptureResult {
  /** 페이지별 PNG Blob 배열 (1-based 순서). */
  blobs: Blob[];
  /** 총 페이지 수. */
  totalPages: number;
  /** 실제 사용된 모드. */
  mode: CaptureMode;
  /** 사용자에게 보여줄 안내 경고 (예: 외부 CORS 리소스 가능성). */
  warnings: string[];
}

export class PresentationCaptureUnsupportedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PresentationCaptureUnsupportedError";
  }
}

function inferMode(blob: Blob, filename?: string): CaptureMode | null {
  if (isPdfBlob(blob, filename)) return "pdf";
  if (isHtmlBlob(blob, filename)) return "html";
  return null;
}

/**
 * 프레젠테이션 파일 → PNG 페이지 배열.
 *
 * 호출자 흐름:
 *   ```ts
 *   try {
 *     const r = await capturePresentation(file, file.name, { onProgress });
 *     await uploadImagesAsFootage(r.blobs);
 *   } catch (e) {
 *     // 클라 캡처 실패 → 백엔드 fallback
 *     await uploadPresentation(projectId, file);
 *   }
 *   ```
 */
export async function capturePresentation(
  blob: Blob,
  filename?: string,
  options?: PresentationCaptureOptions
): Promise<PresentationCaptureResult> {
  const mode = options?.mode ?? inferMode(blob, filename);
  if (!mode) {
    throw new PresentationCaptureUnsupportedError(
      `지원하지 않는 프레젠테이션 형식입니다 (${filename ?? (blob.type || "unknown")}). PDF/HTML 만 클라이언트 캡처가 가능합니다.`
    );
  }

  if (mode === "pdf") {
    const pdfOpts: PdfCaptureOptions = {
      scale: options?.pdfScale,
      signal: options?.signal,
      onProgress: options?.onProgress,
    };
    const r = await capturePdfPages(blob, pdfOpts);
    return {
      blobs: r.blobs,
      totalPages: r.totalPages,
      mode: "pdf",
      warnings: [],
    };
  }

  // mode === "html"
  const htmlOpts: HtmlCaptureOptions = {
    width: options?.htmlWidth,
    height: options?.htmlHeight,
    signal: options?.signal,
    onProgress: options?.onProgress,
  };
  const r = await captureHtmlBlob(blob, htmlOpts);
  return {
    blobs: r.blobs,
    totalPages: r.totalPages,
    mode: "html",
    warnings: r.warnings,
  };
}

/** PNG Blob 배열 → File 배열로 변환 (업로드용 파일명 부여). */
export function blobsToPresentationFiles(
  blobs: Blob[],
  stem: string
): File[] {
  return blobs.map((b, i) => {
    const name = `${stem}_page_${(i + 1).toString().padStart(3, "0")}.png`;
    return new File([b], name, { type: "image/png" });
  });
}
