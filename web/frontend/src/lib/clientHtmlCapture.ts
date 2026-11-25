/**
 * clientHtmlCapture (#313)
 * ─────────────────────────────────────────────────────────────────────────
 * HTML 프레젠테이션 → 페이지별 PNG 이미지 변환을 브라우저에서 직접 수행.
 *
 * 단순 케이스만 지원한다 (이슈 #313 "HTML 클라 캡처 (단순 케이스)" 요구사항).
 * 복잡한 사례(외부 CSS/폰트/대용량 이미지, 인라인 JS 로 페이지 전환) 는
 * 백엔드 Playwright fallback 에 맡긴다.
 *
 * 처리 절차:
 *  1. 격리된 `<iframe sandbox>` 를 만들어 HTML 을 로드.
 *  2. 동기 폴링으로 `documentElement` 가 준비될 때까지 대기.
 *  3. 'section', '.slide', '[data-page]', '.step' 순으로 슬라이드 후보 탐지
 *     (백엔드 `_capture_html_pages` 와 동일 규칙 — 동작 일관성).
 *  4. 후보가 2개 이상이면 각각 html2canvas 로 캡처.
 *     단일 페이지면 body 전체를 1장 캡처.
 *  5. iframe 제거.
 *
 * 보안:
 * - sandbox 속성으로 스크립트 실행은 허용하되 (slide framework 가 필요로
 *   할 수 있으므로 `allow-scripts`), top navigation/popups 는 차단.
 * - 단, sandbox 동일 origin 없이는 부모 윈도우가 iframe 내부 DOM 에 접근할
 *   수 없다. 따라서 `allow-same-origin` 도 허용한다. (악성 HTML 업로드는
 *   사용자가 직접 올린 자기 자료이므로 위협 모델상 허용 가능.)
 *
 * 폴백:
 * - 외부 CORS 리소스를 참조하는 슬라이드는 html2canvas 가 빈 박스로
 *   캡처할 수 있다. 호출측에서 캡처 실패/이상 감지 시 백엔드 업로드 경로로
 *   fallback 한다.
 */

// html2canvas 는 큰 라이브러리 — 동적 import 로 lazy load.
let _html2canvas: typeof import("html2canvas").default | null = null;
async function ensureHtml2canvas(): Promise<typeof import("html2canvas").default> {
  if (_html2canvas) return _html2canvas;
  const mod = await import("html2canvas");
  _html2canvas = mod.default ?? mod;
  return _html2canvas;
}

export interface HtmlCaptureOptions {
  /** 캡처 viewport 폭 (기본 1920) */
  width?: number;
  /** 캡처 viewport 높이 (기본 1080) */
  height?: number;
  /** 외부 중단 신호. */
  signal?: AbortSignal;
  /** 페이지 캡처마다 호출되는 진행률 콜백. 1-based. */
  onProgress?: (current: number, total: number) => void;
  /** 슬라이드 탐색 selector 우선순위. 기본은 백엔드 규칙과 동일. */
  slideSelectors?: string[];
}

export interface HtmlCaptureResult {
  blobs: Blob[];
  /** 감지된 슬라이드 수. */
  totalPages: number;
  /** 외부 CORS 리소스 등으로 캡처 정확도가 떨어졌을 가능성. UI 안내용. */
  warnings: string[];
}

const DEFAULT_SLIDE_SELECTORS = ["section", ".slide", "[data-page]", ".step"];
const DEFAULT_WIDTH = 1920;
const DEFAULT_HEIGHT = 1080;
const FRAME_LOAD_TIMEOUT_MS = 8000;

class AbortError extends Error {
  constructor(message = "HTML capture aborted") {
    super(message);
    this.name = "AbortError";
  }
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw new AbortError();
}

/** iframe 에 HTML 을 로드하고 document 객체를 반환한다. */
function loadIframe(
  htmlContent: string,
  width: number,
  height: number,
  signal: AbortSignal | undefined
): Promise<{ iframe: HTMLIFrameElement; doc: Document }> {
  return new Promise((resolve, reject) => {
    const iframe = document.createElement("iframe");
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
    // 보이지 않게 — 화면 밖에 배치.
    iframe.style.position = "fixed";
    iframe.style.left = "-100000px";
    iframe.style.top = "0";
    iframe.style.width = `${width}px`;
    iframe.style.height = `${height}px`;
    iframe.style.border = "0";
    iframe.style.visibility = "hidden";
    iframe.style.pointerEvents = "none";

    let settled = false;
    const cleanup = () => {
      if (settled) return;
      settled = true;
      try {
        iframe.remove();
      } catch {
        /* DOM 에 미부착이면 무시 */
      }
    };

    const timer = window.setTimeout(() => {
      cleanup();
      reject(new Error("iframe 로드 타임아웃"));
    }, FRAME_LOAD_TIMEOUT_MS);

    const onAbort = () => {
      window.clearTimeout(timer);
      cleanup();
      reject(new AbortError());
    };
    if (signal) signal.addEventListener("abort", onAbort);

    iframe.onload = () => {
      window.clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
      const doc = iframe.contentDocument;
      if (!doc) {
        cleanup();
        reject(new Error("iframe contentDocument 접근 불가 (sandbox 제약)"));
        return;
      }
      // settled 는 reject 회피용 — 성공 시에는 호출측이 cleanup 책임.
      settled = true;
      resolve({ iframe, doc });
    };

    iframe.onerror = () => {
      window.clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
      cleanup();
      reject(new Error("iframe 로드 실패"));
    };

    document.body.appendChild(iframe);
    iframe.srcdoc = htmlContent;
  });
}

function canvasToPng(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    if (typeof canvas.toBlob === "function") {
      canvas.toBlob((b) => {
        if (b) resolve(b);
        else reject(new Error("Canvas.toBlob 이 null 을 반환했습니다"));
      }, "image/png");
    } else {
      try {
        const dataUrl = canvas.toDataURL("image/png");
        const bin = atob(dataUrl.split(",")[1]);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        resolve(new Blob([arr], { type: "image/png" }));
      } catch (e) {
        reject(e);
      }
    }
  });
}

/** iframe 내 외부 절대 URL (http/https/CORS) 리소스 비율을 추정 — 경고 노출용. */
function estimateExternalResourceRatio(doc: Document): number {
  const imgs = Array.from(doc.querySelectorAll("img[src]"));
  const links = Array.from(doc.querySelectorAll("link[rel='stylesheet'][href]"));
  const scripts = Array.from(doc.querySelectorAll("script[src]"));
  const all = [...imgs, ...links, ...scripts];
  if (all.length === 0) return 0;
  const external = all.filter((el) => {
    const url = el.getAttribute("src") ?? el.getAttribute("href") ?? "";
    return /^https?:\/\//i.test(url);
  });
  return external.length / all.length;
}

/**
 * HTML 텍스트(srcdoc) 또는 Blob 을 받아 페이지별 PNG Blob 으로 반환.
 */
export async function captureHtmlPages(
  htmlContent: string,
  options?: HtmlCaptureOptions
): Promise<HtmlCaptureResult> {
  const width = options?.width ?? DEFAULT_WIDTH;
  const height = options?.height ?? DEFAULT_HEIGHT;
  const signal = options?.signal;
  const selectors = options?.slideSelectors ?? DEFAULT_SLIDE_SELECTORS;

  throwIfAborted(signal);

  const html2canvas = await ensureHtml2canvas();
  const { iframe, doc } = await loadIframe(htmlContent, width, height, signal);
  const warnings: string[] = [];

  try {
    throwIfAborted(signal);

    // 외부 리소스 비율 경고.
    const extRatio = estimateExternalResourceRatio(doc);
    if (extRatio > 0.2) {
      warnings.push(
        `외부 절대 URL 리소스가 약 ${Math.round(extRatio * 100)}% 감지됐습니다 — CORS 제약으로 일부 이미지/폰트가 누락될 수 있습니다.`
      );
    }

    // 슬라이드 탐지 — 백엔드와 동일 순서.
    let slides: HTMLElement[] = [];
    for (const sel of selectors) {
      const found = Array.from(doc.querySelectorAll<HTMLElement>(sel));
      if (found.length > 1) {
        slides = found;
        break;
      }
    }

    const captures: Blob[] = [];
    if (slides.length === 0) {
      // 단일 페이지 — body 전체 캡처.
      const target = doc.body ?? doc.documentElement;
      const canvas = await html2canvas(target, {
        backgroundColor: "#ffffff",
        width,
        height,
        windowWidth: width,
        windowHeight: height,
        useCORS: true,
        logging: false,
      });
      captures.push(await canvasToPng(canvas));
      options?.onProgress?.(1, 1);
    } else {
      for (let i = 0; i < slides.length; i++) {
        throwIfAborted(signal);
        const el = slides[i];
        const canvas = await html2canvas(el, {
          backgroundColor: "#ffffff",
          width,
          height,
          windowWidth: width,
          windowHeight: height,
          useCORS: true,
          logging: false,
        });
        captures.push(await canvasToPng(canvas));
        options?.onProgress?.(i + 1, slides.length);
      }
    }

    return {
      blobs: captures,
      totalPages: captures.length,
      warnings,
    };
  } finally {
    try {
      iframe.remove();
    } catch {
      /* DOM 에서 이미 제거됐을 수도 있음 */
    }
  }
}

/** HTML Blob → 텍스트로 읽어 captureHtmlPages 에 위임. */
export async function captureHtmlBlob(
  blob: Blob,
  options?: HtmlCaptureOptions
): Promise<HtmlCaptureResult> {
  const text = await blob.text();
  return captureHtmlPages(text, options);
}

export function isHtmlBlob(blob: Blob, filename?: string): boolean {
  if (blob.type === "text/html") return true;
  if (filename && /\.html?$/i.test(filename)) return true;
  return false;
}
