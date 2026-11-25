/**
 * clientAudioWaveform (#312)
 * ─────────────────────────────────────────────────────────────────────────
 * 오디오 Blob에서 진폭 피크(peaks) 배열을 브라우저(Web Audio API)에서 직접
 * 추출한다. BGM 편집기의 파형 시각화를 백엔드 호출 없이 그리기 위한
 * 클라이언트 오프로드 모듈.
 *
 * 사전 점검 결과(이슈 #312):
 * - 백엔드에는 파형 생성 엔드포인트 자체가 없다 (서버 부담 0 이 이미 baseline).
 *   따라서 본 모듈의 가치는 "있어야 할 파형 시각화"를 클라이언트에서 무비용으로
 *   제공하는 것이며, 백엔드 회피라기보다 "백엔드 미구현 영역의 클라이언트 단독
 *   제공"에 가깝다.
 * - 백엔드 `_wav_duration` 호출들은 모두 백엔드가 자체 생성한 TTS WAV 측정용이라
 *   client_meta 가 의미 없다. duration 절감의 잔여 작업은 없음.
 *
 * 디자인 원칙:
 * - 모듈은 순수하다 — DOM/IndexedDB 의존 없음. 캐시는 호출측이 책임.
 * - decode 실패/타임아웃 시 명시적 에러 throw — 호출측에서 placeholder 렌더 결정.
 * - AudioContext 는 작업 종료 후 즉시 close() — Safari 등에서 동시 열린
 *   컨텍스트 수 제한 회피 + GC 빠른 진입.
 * - 큰 파일(>50MB) 안전장치: decodeAudioData 자체에 명시적 타임아웃 적용
 *   (Promise.race). 이론적으로 decodeAudioData 는 abort 가 없어서 decode 자체는
 *   백그라운드에서 계속되지만, await 시점에는 에러로 빠진다.
 */

export interface WaveformData {
  /** 0~1 로 정규화된 진폭 피크. 길이는 호출 시 peakCount 옵션 값. */
  peaks: number[];
  /** 디코딩된 오디오 길이(초). 단정밀도 float. */
  duration: number;
  /** 디코딩된 sampleRate (Hz). */
  sample_rate: number;
  /** 디코딩된 채널 수. */
  channels: number;
}

export interface ExtractWaveformOptions {
  /** 추출할 피크 개수. UI 캔버스 width 와 동일하게 잡으면 1px = 1peak. */
  peakCount?: number;
  /** AudioContext.decodeAudioData 호출 자체에 적용할 타임아웃(ms). */
  timeoutMs?: number;
  /**
   * 외부 abort 신호. 디코딩 도중 호출되면 reject 한다.
   * (decodeAudioData 자체는 abort 가 없음 — abort 시 컨텍스트만 close).
   */
  signal?: AbortSignal;
}

const DEFAULT_PEAK_COUNT = 800;
const DEFAULT_TIMEOUT_MS = 15000; // 50MB WAV ≈ 4~6s 디코드, 여유롭게 15s.

/** 브라우저 AudioContext 생성 — webkit prefix 폴백 포함. */
function createAudioContext(): AudioContext {
  // Safari 옛 버전은 webkitAudioContext 만 노출. 표준 타이핑 위해 any 캐스팅.
  const Ctor: typeof AudioContext | undefined =
    (typeof window !== "undefined" &&
      ((window as unknown as { AudioContext?: typeof AudioContext }).AudioContext ||
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext)) ||
    undefined;
  if (!Ctor) {
    throw new Error("clientAudioWaveform: 브라우저가 AudioContext를 지원하지 않습니다");
  }
  // Safari 의 OfflineAudioContext 가 더 가볍지만, 디코드만 한다면 일반 컨텍스트로
  // 충분하고, sampleRate 고정 옵션이 모바일에서 메타와 불일치를 일으킬 수 있어
  // 일반 컨텍스트로 통일한다.
  return new Ctor();
}

/**
 * PCM(Float32Array) 채널 데이터에서 peakCount 개의 피크(절댓값 max)를 추출.
 *
 * 알고리즘: samples 를 peakCount 개의 같은 길이 슬라이스로 분할 → 각 슬라이스의
 * 절댓값 max 를 peak 로. 마지막 슬라이스가 약간 길거나 짧을 수 있다.
 *
 * 정규화: 전체 max 로 나눠 0~1 범위. 무음 트랙(max=0)이면 0 만 반환.
 *
 * 순수 함수 — 테스트/검증이 쉽게 가능.
 */
export function computePeaks(samples: Float32Array, peakCount: number): number[] {
  if (peakCount <= 0) return [];
  if (samples.length === 0) return new Array(peakCount).fill(0);

  const peaks = new Array<number>(peakCount).fill(0);
  // peakCount 가 samples.length 보다 클 수도 있다 — 그 경우 보간 없이 0 패딩.
  // (캔버스가 너무 클 때 의도된 케이스: 짧은 오디오 파일에 큰 트랙 막대)
  const blockSize = samples.length / peakCount;
  if (blockSize < 1) {
    // 샘플이 더 적은 경우: 각 sample 을 하나의 peak 로 매핑하고 나머지는 0.
    for (let i = 0; i < samples.length; i++) {
      const v = Math.abs(samples[i]);
      if (v > peaks[i]) peaks[i] = v;
    }
  } else {
    for (let i = 0; i < peakCount; i++) {
      const start = Math.floor(i * blockSize);
      const end = Math.min(samples.length, Math.floor((i + 1) * blockSize));
      let max = 0;
      // typed array 직접 인덱싱이 가장 빠름 (for-of 보다 30%+ 빠름 — V8 측정).
      for (let j = start; j < end; j++) {
        const v = samples[j] < 0 ? -samples[j] : samples[j];
        if (v > max) max = v;
      }
      peaks[i] = max;
    }
  }

  // 정규화 — 전체 최대값으로 나눔. 0 이면 그대로 0 배열.
  let globalMax = 0;
  for (let i = 0; i < peaks.length; i++) {
    if (peaks[i] > globalMax) globalMax = peaks[i];
  }
  if (globalMax > 0) {
    const inv = 1 / globalMax;
    for (let i = 0; i < peaks.length; i++) peaks[i] *= inv;
  }
  return peaks;
}

/**
 * 두 채널을 평균해 mono 로 합친 PCM 을 반환.
 * 스테레오 → 좌/우 동일 가중치 평균.
 *
 * AudioBuffer 의 getChannelData 는 같은 ArrayBuffer 뷰를 돌려주므로 새 Float32Array
 * 를 할당해 원본을 건드리지 않는다.
 */
function mergeChannelsToMono(buf: AudioBuffer): Float32Array {
  if (buf.numberOfChannels <= 1) {
    // 단일 채널: 복사 없이 그대로 반환해도 read-only 로 쓰면 안전하지만,
    // 호출측 안전을 위해 한 번 슬라이스해 새 view 로 분리.
    return buf.getChannelData(0).slice();
  }
  const len = buf.length;
  const out = new Float32Array(len);
  const ch0 = buf.getChannelData(0);
  const ch1 = buf.getChannelData(1);
  if (buf.numberOfChannels === 2) {
    for (let i = 0; i < len; i++) out[i] = (ch0[i] + ch1[i]) * 0.5;
    return out;
  }
  // 3채널 이상(5.1 등): 모든 채널을 동일 가중치로 평균.
  const n = buf.numberOfChannels;
  for (let i = 0; i < len; i++) out[i] = (ch0[i] + ch1[i]) / n;
  for (let c = 2; c < n; c++) {
    const ch = buf.getChannelData(c);
    for (let i = 0; i < len; i++) out[i] += ch[i] / n;
  }
  return out;
}

/**
 * Blob 입력을 받아 파형(peaks)/duration/sample_rate/channels 를 추출한다.
 *
 * 실패 케이스:
 * - 빈 Blob → Error
 * - decodeAudioData 실패(지원 안 되는 포맷 등) → Error
 * - timeoutMs 초과 → Error
 * - abort signal trigger → Error
 *
 * 호출측은 실패 시 캐시에 negative entry 를 저장하지 말고 단순히 placeholder
 * (빈 막대) 렌더로 폴백하면 된다.
 */
export async function extractWaveform(
  blob: Blob,
  options: ExtractWaveformOptions = {},
): Promise<WaveformData> {
  if (!(blob instanceof Blob)) {
    throw new Error("extractWaveform: 입력이 Blob이 아닙니다");
  }
  if (blob.size === 0) {
    throw new Error("extractWaveform: 빈 Blob입니다");
  }
  const peakCount = Math.max(1, Math.floor(options.peakCount ?? DEFAULT_PEAK_COUNT));
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const signal = options.signal;

  if (signal?.aborted) {
    throw new Error("extractWaveform: 이미 aborted 상태입니다");
  }

  const arrayBuf = await blob.arrayBuffer();
  // 빈 ArrayBuffer 방어 — 위 size 체크와 중복이지만 안전망.
  if (arrayBuf.byteLength === 0) {
    throw new Error("extractWaveform: ArrayBuffer가 비어 있습니다");
  }

  const ctx = createAudioContext();

  // 타임아웃 + abort 동시 race. decodeAudioData 는 abort 를 지원하지 않으므로
  // 외부에서는 컨텍스트 close 로만 정리한다(디코드는 백그라운드에서 계속될 수
  // 있지만 결과를 받지 않으므로 GC 됨).
  let timer: ReturnType<typeof setTimeout> | null = null;
  let abortHandler: (() => void) | null = null;

  try {
    const decoded = await new Promise<AudioBuffer>((resolve, reject) => {
      timer = setTimeout(
        () => reject(new Error(`extractWaveform: 디코딩 타임아웃 (${timeoutMs}ms)`)),
        timeoutMs,
      );
      if (signal) {
        abortHandler = () => reject(new Error("extractWaveform: aborted"));
        signal.addEventListener("abort", abortHandler, { once: true });
      }
      // decodeAudioData 는 promise 와 콜백 두 변형이 있다. Safari 14 미만은
      // promise 변형이 없어서 콜백을 써야 안전하다.
      try {
        const maybePromise = ctx.decodeAudioData(
          arrayBuf,
          (buf) => resolve(buf),
          (err) =>
            reject(
              err instanceof Error
                ? err
                : new Error(`extractWaveform: decodeAudioData 실패`),
            ),
        );
        // 표준 Promise 반환형도 지원 — 위 콜백 두 개가 호출되면 unhandled
        // rejection 이 나지 않도록 catch 만 붙인다.
        if (maybePromise && typeof (maybePromise as Promise<AudioBuffer>).then === "function") {
          (maybePromise as Promise<AudioBuffer>).then(resolve, (err: unknown) =>
            reject(
              err instanceof Error
                ? err
                : new Error(`extractWaveform: decodeAudioData 실패`),
            ),
          );
        }
      } catch (e) {
        // 매우 큰 ArrayBuffer 가 동기 throw 하는 경우도 있다 (Chrome).
        reject(e instanceof Error ? e : new Error(String(e)));
      }
    });

    const mono = mergeChannelsToMono(decoded);
    const peaks = computePeaks(mono, peakCount);
    return {
      peaks,
      duration: decoded.duration,
      sample_rate: decoded.sampleRate,
      channels: decoded.numberOfChannels,
    };
  } finally {
    if (timer) clearTimeout(timer);
    if (signal && abortHandler) {
      signal.removeEventListener("abort", abortHandler);
    }
    // close() 가 Promise 인데 결과를 기다리지 않음 — fire and forget.
    // Safari 옛 버전엔 close 가 없을 수 있어 옵셔널 호출.
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const maybeClose = (ctx as any).close;
      if (typeof maybeClose === "function") {
        maybeClose.call(ctx).catch?.(() => {
          /* ignore */
        });
      }
    } catch {
      /* ignore */
    }
  }
}

/**
 * 파형 데이터의 sanity check. IndexedDB 캐시 로드 시 손상된 레코드를 필터.
 */
export function isWaveformDataSane(w: WaveformData | null | undefined): w is WaveformData {
  if (!w) return false;
  if (!Array.isArray(w.peaks) || w.peaks.length === 0) return false;
  if (!Number.isFinite(w.duration) || w.duration <= 0) return false;
  if (!Number.isFinite(w.sample_rate) || w.sample_rate <= 0) return false;
  if (!Number.isFinite(w.channels) || w.channels < 1) return false;
  // peaks 가 0~1 범위 안에 있는지 표본 5개로 빠른 검사.
  const n = Math.min(5, w.peaks.length);
  for (let i = 0; i < n; i++) {
    const v = w.peaks[Math.floor((i * w.peaks.length) / n)];
    if (!Number.isFinite(v) || v < 0 || v > 1.001) return false;
  }
  return true;
}
