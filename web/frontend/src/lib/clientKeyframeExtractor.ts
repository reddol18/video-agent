/**
 * clientKeyframeExtractor (#314)
 * ─────────────────────────────────────────────────────────────────────────
 * WebCodecs `VideoDecoder` 기반 키프레임 추출기.
 *
 * 본 모듈은 **사전 점검 결과 본격 구현이 보류된 실험적 스캐폴드**다.
 * (자세한 내용은 `docs/review-notes.md` 의 #314 항목과 이슈 코멘트를 참고)
 *
 * 보류 결정의 핵심 근거:
 *   1. 현 Phase 2 인덱싱(`_do_index_footage`)은 키프레임 이미지가 아니라
 *      **10초 비디오 청크 자체**를 480p/500kbps 로 트랜스코딩 후 Gemini Video
 *      API 로 전송한다 (`generate_json_with_video`). 즉 키프레임 이미지만으로는
 *      `quality_score`(흔들림), `has_audio`(현장음/대화) 같은 핵심 필드를
 *      동일하게 산출할 수 없다.
 *   2. 절감 대상은 ffmpeg 트랜스코딩 cpu_seconds 뿐이며, 청크당 약 $0.000025
 *      수준. 동일 청크의 Gemini Video API 비용 ~$0.001 대비 ~1/40 수준으로
 *      절대 절감액이 미미하다 (`src/cost_estimator/estimator.py` 참고).
 *   3. 인덱싱은 푸티지 폴더가 갱신될 때 1회만 실행되며, 현재 프론트엔드에서
 *      사용자가 직접 트리거하는 UI 도 없다 (빈도 매우 낮음).
 *
 * 그러나 향후 인덱싱 파이프라인이 "비디오 청크 → Gemini Vision(이미지)" 로
 * 재설계되거나, 같은 keyframe blob 을 다른 용도(썸네일/미리보기/AI 추론) 로
 * 재사용하기 시작하면 본 모듈을 본격 구현으로 확장할 가치가 생긴다.
 *
 * 그 시점에 누구든 빠르게 본격 구현으로 옮길 수 있도록, 본 파일은 다음을 제공한다:
 *   - WebCodecs 지원 감지 (`isWebCodecsSupported`)
 *   - localStorage 기반 Feature Flag (`isClientIndexingEnabled` / `setClientIndexingEnabled`)
 *   - 본격 구현용 공개 시그니처(`extractKeyframes`) — 현재는 명시적으로 throw 한다.
 *
 * 회귀 안전성:
 *   - 본 모듈은 어디에서도 import 되지 않는다. 동작 변경 없음 (실험 스캐폴드).
 *   - Feature Flag 기본값 false. 켜더라도 호출처가 없으므로 부작용 없음.
 *   - 본격 구현 도입 시 `clientMediaMeta`/`clientKeywordMatching` 패턴을 따라
 *     호출측이 try/catch 로 감싸고 실패 시 백엔드 fallback 으로 보내야 한다.
 */

// ── 공개 타입 ──────────────────────────────────────────────────────────────

export interface KeyframeData {
  /** 추출된 키프레임 이미지(JPEG) base64 (data URL prefix 제외). */
  base64: string;
  /** 원본 영상 기준 키프레임 시각(초). */
  timestamp_sec: number;
  /** 추출 순서 (0-based). */
  index: number;
}

export interface ExtractOptions {
  /** 최대 키프레임 수. 기본 10. 너무 큰 값은 메모리/시간 부담. */
  maxKeyframes?: number;
  /** AbortController.signal — 호출측 취소 지원. */
  signal?: AbortSignal;
  /** 진행률 콜백 `(current, total)`. total 은 추정치이므로 over 가능. */
  onProgress?: (current: number, total: number) => void;
  /** JPEG 출력 품질 (0~1). 기본 0.85. */
  jpegQuality?: number;
}

// ── Feature Flag ──────────────────────────────────────────────────────────
//
// 본격 구현이 도입되더라도 WebCodecs 경로는 안정성 검증 전까지 opt-in 으로
// 운영한다. 김감독 사용자 환경(주로 Mac Safari/Chrome)에서 코덱별 호환성
// 차이가 크기 때문이다.
//
// 기본값은 false. 사용자가 설정에서 명시적으로 켜야 활성화된다.

const FLAG_KEY = "video_agent_client_indexing_enabled";

export function isClientIndexingEnabled(): boolean {
  try {
    return localStorage.getItem(FLAG_KEY) === "true";
  } catch {
    return false;
  }
}

export function setClientIndexingEnabled(enabled: boolean): void {
  try {
    if (enabled) localStorage.setItem(FLAG_KEY, "true");
    else localStorage.removeItem(FLAG_KEY);
  } catch {
    /* localStorage 미가용(시크릿 모드 등)은 무시 — Flag OFF 로 간주 */
  }
}

// ── WebCodecs 지원 감지 ────────────────────────────────────────────────────
//
// `'VideoDecoder' in window` 는 객체 존재 여부만 본다. 실제 코덱 지원은
// `VideoDecoder.isConfigSupported({ codec })` 로 별도 확인해야 하지만, 본
// 스캐폴드는 호출처가 없으므로 객체 수준 검사까지만 제공한다.

export function isWebCodecsSupported(): boolean {
  if (typeof globalThis === "undefined") return false;
  // VideoDecoder + EncodedVideoChunk 둘 다 필요.
  // 일부 구버전(Safari 14)은 VideoEncoder만 있고 Decoder는 없다.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const g = globalThis as any;
  return typeof g.VideoDecoder === "function" && typeof g.EncodedVideoChunk === "function";
}

// ── 본격 구현 시그니처 (현재 throw) ────────────────────────────────────────
//
// 본격 구현 시 아래를 채워 넣는다:
//   1. MP4Box.js 동적 import — 번들 크기 영향 최소화.
//   2. blob → ArrayBuffer → MP4Box 파서 → encoded video chunks (keyframe 만).
//   3. VideoDecoder.configure({ codec: avc.description 등 }) — codec 문자열은
//      MP4Box 의 `mp4a/avcC` 박스에서 추출해야 한다.
//   4. decoder.decode(EncodedVideoChunk) → VideoFrame.
//   5. OffscreenCanvas.drawImage(frame) → convertToBlob({ type:"image/jpeg" })
//      → arrayBuffer → btoa.
//   6. AbortSignal 처리 — decoder.close() 호출 후 reject.
//   7. 결과 KeyframeData[] 반환.
//
// 핵심 구현 노트 (향후 작업자용):
//   • MP4Box.js 는 약 200KB(min, gzip 80KB) — 동적 import 필수.
//   • VideoDecoder 는 비동기 콜백 기반이라 Promise wrapping 필요.
//   • Safari 의 VideoDecoder 는 일부 codec string 을 거부하므로 isConfigSupported
//     로 사전 검증 후 fallback throw 해야 한다.
//   • blob 이 mov/webm 이면 MP4Box 가 파싱 못 함 — mp4 만 지원.

export async function extractKeyframes(
  blob: Blob,
  options: ExtractOptions = {},
): Promise<KeyframeData[]> {
  // 신호가 이미 abort 된 경우 즉시 throw.
  if (options.signal?.aborted) {
    throw new DOMException("Aborted before start", "AbortError");
  }
  if (!isWebCodecsSupported()) {
    throw new Error(
      "WebCodecs 가 지원되지 않습니다. 백엔드 인덱싱 경로(ffmpeg)를 사용하세요.",
    );
  }
  // 본격 구현 보류 — 호출처가 생기면 본격 구현으로 교체할 것.
  // 여기서 명시적으로 throw 함으로써 "실수로 사용" 을 차단한다.
  void blob;
  throw new Error(
    "extractKeyframes 본격 구현은 보류 상태입니다 (#314 사전 점검 보류). " +
      "활성화하려면 docs/review-notes.md 의 #314 항목과 이슈 본문을 검토 후 " +
      "MP4Box.js 도입과 VideoDecoder 구현을 진행해야 합니다.",
  );
}
