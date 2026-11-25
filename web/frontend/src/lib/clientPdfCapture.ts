/**
 * clientPdfCapture (#313)
 * ─────────────────────────────────────────────────────────────────────────
 * PDF 프레젠테이션 → 페이지별 PNG 이미지 변환을 브라우저에서 직접 수행한다.
 *
 * 기존: 백엔드 `_capture_html_pages` 에서 Playwright(Chromium headless) 로
 * 페이지를 캡처 → CPU/메모리 부담이 매우 큰 작업이었음.
 *
 * 신규: pdf.js (`pdfjs-dist@5.x`) 로 클라이언트에서 페이지별 Canvas 렌더 후
 * `canvas.toBlob` 으로 PNG 추출. 결과 Blob[] 을 백엔드에 이미지 푸티지로
 * 일괄 업로드한다 (헤드리스 브라우저 비용 0).
 *
 * pdf.js 5.x API 변경점:
 * - `getDocument({...})` 의 반환은 `PDFDocumentLoadingTask`
 * - `page.render({ canvas, viewport })` — `canvasContext` 는 deprecated.
 *   대신 `canvas` 를 직접 넘긴다.
 * - Worker 스크립트는 ESM (.mjs). Vite 의 `?url` import 로 정적 자산 URL 을
 *   확보해 `GlobalWorkerOptions.workerSrc` 에 주입한다.
 *
 * 메모리 관리:
 * - 페이지마다 `page.cleanup()` 호출 (pdf.js 권장)
 * - 마지막에 `loadingTask.destroy()` 로 워커 자원 해제
 *
 * AbortSignal:
 * - 페이지 루프에서 매 iteration 마다 `signal?.aborted` 확인.
 * - render 가 시작된 이후에는 `task.cancel()` 로 즉시 중단.
 */

import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
} from "pdfjs-dist";

// Worker 등록 — 모듈 최초 import 시점에 1회만 수행.
//
// pdfjs-dist 5.x 는 `pdf.worker.min.mjs` 를 제공한다. Vite 의 `?url` 쿼리로
// 번들에 포함된 정적 파일의 URL 을 받아온다.
//
// 동적 import 로 라이브러리 본체와 함께 한 번에 로드한다 — 프레젠테이션
// 캡처 화면에 진입할 때까지 번들 페이로드를 늦춘다 (#313 라이브러리 lazy
// loading 요구사항).
let _pdfjsModule: typeof import("pdfjs-dist") | null = null;
let _workerInitPromise: Promise<void> | null = null;

async function ensurePdfjsReady(): Promise<typeof import("pdfjs-dist")> {
  if (_pdfjsModule) return _pdfjsModule;
  // 동적 import — 이 모듈을 호출한 곳도 lazy 경로에 있어야 의미가 있다.
  const pdfjs = await import("pdfjs-dist");
  if (!_workerInitPromise) {
    _workerInitPromise = (async () => {
      // Vite `?url` 로 워커 정적 자산 URL 확보.
      // 5.x 는 ESM (.mjs) 만 공식 제공.
      const workerSrc = (
        await import("pdfjs-dist/build/pdf.worker.min.mjs?url")
      ).default as string;
      pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;
    })();
  }
  await _workerInitPromise;
  _pdfjsModule = pdfjs;
  return pdfjs;
}

export interface PdfCaptureOptions {
  /** 페이지 렌더 배율. 1.0 = PDF 원본 좌표, 1.5 = 1.5배 (선명도 향상). */
  scale?: number;
  /** PNG 변환 품질 (mime image/png 은 무손실이라 사실상 무시되지만, 인터페이스 호환을 위해 유지) */
  quality?: number;
  /** 외부에서 중단 신호 (사용자 취소 등). */
  signal?: AbortSignal;
  /** 페이지별 진행률 콜백. 1-based. */
  onProgress?: (current: number, total: number) => void;
}

export interface PdfCaptureResult {
  /** 페이지별 PNG Blob (1-based 순서). */
  blobs: Blob[];
  /** 총 페이지 수 (도큐먼트 기준). */
  totalPages: number;
}

class AbortError extends Error {
  constructor(message = "PDF capture aborted") {
    super(message);
    this.name = "AbortError";
  }
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) {
    throw new AbortError();
  }
}

/**
 * Blob → ArrayBuffer (Safari 호환 — `await blob.arrayBuffer()` 가 안 되는
 * 경우 거의 없지만 안전한 fallback 한 번 더 두지는 않음. 표준 API).
 */
async function blobToArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
  return blob.arrayBuffer();
}

/** Canvas → PNG Blob 변환. toBlob 미지원 시 toDataURL fallback. */
function canvasToPng(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    if (typeof canvas.toBlob === "function") {
      canvas.toBlob((blob) => {
        if (blob) resolve(blob);
        else reject(new Error("Canvas.toBlob 이 null 을 반환했습니다"));
      }, "image/png");
      return;
    }
    // Fallback — dataURL 디코딩.
    try {
      const dataUrl = canvas.toDataURL("image/png");
      const byteString = atob(dataUrl.split(",")[1]);
      const arr = new Uint8Array(byteString.length);
      for (let i = 0; i < byteString.length; i++) arr[i] = byteString.charCodeAt(i);
      resolve(new Blob([arr], { type: "image/png" }));
    } catch (e) {
      reject(e);
    }
  });
}

/**
 * PDF Blob → 페이지별 PNG Blob 배열.
 *
 * 호출자는 반환된 Blob 들을 `File` 로 감싸 `addStudioAssets` 와 같은
 * 이미지 업로드 엔드포인트로 보내면 된다.
 *
 * 회귀 안전성:
 * - 페이지 수가 0 인 경우 빈 배열 반환 (에러 throw 하지 않는다 — 호출측이
 *   판단).
 * - 한 페이지 렌더 실패해도 다음 페이지로 진행하지 않고 즉시 throw —
 *   부분 캡처 결과가 백엔드에 잘못 등록되는 것을 막기 위함.
 * - AbortSignal 로 중간 중단 시 RenderTask 도 cancel 한다.
 */
export async function capturePdfPages(
  blob: Blob,
  options?: PdfCaptureOptions
): Promise<PdfCaptureResult> {
  const scale = options?.scale ?? 1.5;
  const signal = options?.signal;

  throwIfAborted(signal);

  const pdfjs = await ensurePdfjsReady();
  const data = await blobToArrayBuffer(blob);

  // pdfjs.getDocument 는 data 를 transferred 시킬 수 있으므로 복사본을
  // 전달한다 (원본 Blob 은 호출측이 다른 용도로 쓸 수 있음).
  const dataCopy = data.slice(0);
  // pdf.js 5.x DocumentInitParameters — TypeScript 정의에 일부 옵션이
  // 누락되어 있어 `as` 단언으로 우회. 실제로는 라이브러리가 받아들이는
  // 안전한 옵션들이다.
  const loadingTask: PDFDocumentLoadingTask = pdfjs.getDocument({
    data: dataCopy,
    disableFontFace: false,
  } as Parameters<typeof pdfjs.getDocument>[0]);

  let pdfDoc: PDFDocumentProxy | null = null;
  const blobs: Blob[] = [];
  let currentRender: RenderTask | null = null;

  // Abort 시 진행 중 render 도 함께 cancel.
  const onAbort = () => {
    if (currentRender) {
      try {
        currentRender.cancel();
      } catch {
        /* render 가 이미 끝났을 수도 있음 — 무시 */
      }
    }
  };
  if (signal) signal.addEventListener("abort", onAbort);

  try {
    pdfDoc = await loadingTask.promise;
    const totalPages = pdfDoc.numPages;
    options?.onProgress?.(0, totalPages);

    for (let pageNo = 1; pageNo <= totalPages; pageNo++) {
      throwIfAborted(signal);

      let page: PDFPageProxy | null = null;
      let canvas: HTMLCanvasElement | null = null;
      try {
        page = await pdfDoc.getPage(pageNo);
        const viewport = page.getViewport({ scale });

        canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.ceil(viewport.width));
        canvas.height = Math.max(1, Math.ceil(viewport.height));

        // pdf.js 5.x — `canvas` 권장. canvasContext 는 deprecated.
        const renderTask = page.render({ canvas, viewport });
        currentRender = renderTask;
        await renderTask.promise;
        currentRender = null;

        const pngBlob = await canvasToPng(canvas);
        blobs.push(pngBlob);
        options?.onProgress?.(pageNo, totalPages);
      } finally {
        // 메모리 회수 — 페이지·캔버스 모두 명시적으로 비운다.
        try {
          page?.cleanup();
        } catch {
          /* 이미 정리됐을 수도 있음 */
        }
        if (canvas) {
          // 일부 브라우저는 큰 canvas 를 GC 까지 늦게 회수.
          canvas.width = 0;
          canvas.height = 0;
        }
      }
    }

    return { blobs, totalPages };
  } finally {
    if (signal) signal.removeEventListener("abort", onAbort);
    try {
      await loadingTask.destroy();
    } catch {
      /* 이미 destroyed 일 수 있음 */
    }
    pdfDoc = null;
  }
}

/** 파일이 PDF 인지 확장자/Mime 으로 판정. (느슨한 검사 — 호출측 fallback 필요시 헤더 magic 확인 가능) */
export function isPdfBlob(blob: Blob, filename?: string): boolean {
  if (blob.type === "application/pdf") return true;
  if (filename && /\.pdf$/i.test(filename)) return true;
  return false;
}
