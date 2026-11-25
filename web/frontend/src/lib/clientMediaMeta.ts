/**
 * clientMediaMeta (#308)
 * ─────────────────────────────────────────────────────────────────────────
 * 푸티지/오디오 업로드 직전 브라우저에서 메타(해상도/길이)를 추출한다.
 * 백엔드 ffprobe 프로세스 spawn 비용을 줄이기 위한 클라이언트 오프로드.
 *
 * - 비디오: `<video>` `loadedmetadata` → videoWidth/videoHeight/duration
 * - 오디오: `<audio>` `loadedmetadata` → duration. AudioContext로 sample_rate/
 *   channels 까지 시도(실패해도 무시 — duration 만 있어도 백엔드는 신뢰 모드 진입).
 *
 * 디자인 원칙:
 * - 항상 4초 타임아웃. 큰 webm/mov가 metadata loaded까지 무한 대기하는 케이스를
 *   막기 위함. (썸네일 추출 #307은 디코드 완료까지 8s 가량 허용했지만, 메타는
 *   metadata 단계에서 끝나므로 더 짧게 잡는다.)
 * - 실패 시 명시적 에러 throw — 호출측에서 fallback 결정 (백엔드 ffprobe).
 * - source 필드로 client/server 판별 가능. 백엔드는 "client" 만 신뢰 모드에
 *   투입한다 (sanity check 후).
 *
 * 회귀 안전성:
 * - duration 이 `NaN`/`Infinity`/`<= 0` 인 경우(webm MediaRecorder의 unknown
 *   duration 등)는 명시 실패시켜 백엔드 ffprobe fallback 으로 보낸다.
 *   (#tts-truncate 케이스와 동일한 webm duration 이슈)
 */

export interface VideoMeta {
  width: number;
  height: number;
  duration: number;
  source: "client" | "server";
}

export interface AudioMeta {
  duration: number;
  sample_rate?: number;
  channels?: number;
  source: "client" | "server";
}

const DEFAULT_TIMEOUT_MS = 4000;

function isFiniteNumber(v: number): boolean {
  return typeof v === "number" && Number.isFinite(v) && v > 0;
}

/**
 * Blob 입력을 받아 `<video>` 엘리먼트로 메타를 추출한다.
 * 실패(타임아웃/디코드 실패/메타 이상) 시 에러 throw.
 */
export function extractVideoMeta(
  blob: Blob,
  options: { timeoutMs?: number } = {},
): Promise<VideoMeta> {
  if (!(blob instanceof Blob)) {
    return Promise.reject(new Error("extractVideoMeta: 입력이 Blob이 아닙니다"));
  }
  if (blob.size === 0) {
    return Promise.reject(new Error("extractVideoMeta: 빈 Blob입니다"));
  }
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const url = URL.createObjectURL(blob);
  return new Promise<VideoMeta>((resolve, reject) => {
    const video = document.createElement("video");
    video.preload = "metadata";
    video.muted = true;
    (video as unknown as { playsInline: boolean }).playsInline = true;

    let settled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const cleanup = () => {
      try {
        video.removeAttribute("src");
        video.load();
      } catch {
        /* ignore */
      }
      URL.revokeObjectURL(url);
    };

    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try {
        fn();
      } finally {
        cleanup();
      }
    };

    const fail = (msg: string) => finish(() => reject(new Error(msg)));

    video.onloadedmetadata = () => {
      const width = video.videoWidth;
      const height = video.videoHeight;
      const duration = video.duration;
      if (!isFiniteNumber(width) || !isFiniteNumber(height)) {
        fail(`extractVideoMeta: 잘못된 해상도 (${width}x${height})`);
        return;
      }
      if (!isFiniteNumber(duration)) {
        // webm MediaRecorder 출력 등 duration=Infinity/NaN 케이스 — 실패시켜
        // 백엔드 ffprobe(packet pts_time 폴백) 가 처리하도록 한다.
        fail(`extractVideoMeta: duration 측정 실패 (${duration})`);
        return;
      }
      finish(() =>
        resolve({
          width: Math.round(width),
          height: Math.round(height),
          duration,
          source: "client",
        }),
      );
    };
    video.onerror = () => fail("extractVideoMeta: video load error");

    timer = setTimeout(
      () => fail(`extractVideoMeta: timeout (${timeoutMs}ms)`),
      timeoutMs,
    );
    video.src = url;
  });
}

/**
 * Blob 입력을 받아 `<audio>` 엘리먼트로 메타를 추출한다.
 *
 * sample_rate / channels는 `AudioContext.decodeAudioData` 가 가능하면 채워주고,
 * 실패해도 무시한다(백엔드는 duration 만으로도 충분히 신뢰 가능).
 *
 * #312 — `withSampleRate: true` 옵션으로 sample_rate/channels 보강을 활성화.
 * 기본값(false)은 `<audio>` 메타만 — 디코드를 수행하지 않아 빠르다.
 * BGM 편집기처럼 파형 추출도 함께 하는 흐름이면 별도 `extractWaveform` 을 호출하는
 * 편이 효율적이므로, 본 함수의 디코드 보강은 명시 요청 시에만 수행한다.
 */
export function extractAudioMeta(
  blob: Blob,
  options: { timeoutMs?: number; withSampleRate?: boolean } = {},
): Promise<AudioMeta> {
  if (!(blob instanceof Blob)) {
    return Promise.reject(new Error("extractAudioMeta: 입력이 Blob이 아닙니다"));
  }
  if (blob.size === 0) {
    return Promise.reject(new Error("extractAudioMeta: 빈 Blob입니다"));
  }
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const url = URL.createObjectURL(blob);
  return new Promise<AudioMeta>((resolve, reject) => {
    const audio = document.createElement("audio");
    audio.preload = "metadata";

    let settled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const cleanup = () => {
      try {
        audio.removeAttribute("src");
        audio.load();
      } catch {
        /* ignore */
      }
      URL.revokeObjectURL(url);
    };

    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try {
        fn();
      } finally {
        cleanup();
      }
    };

    const fail = (msg: string) => finish(() => reject(new Error(msg)));

    audio.onloadedmetadata = async () => {
      const duration = audio.duration;
      if (!isFiniteNumber(duration)) {
        fail(`extractAudioMeta: duration 측정 실패 (${duration})`);
        return;
      }
      // 기본: duration 만으로 즉시 resolve. 빠른 응답이 우선.
      if (!options.withSampleRate) {
        finish(() =>
          resolve({
            duration,
            source: "client",
          }),
        );
        return;
      }
      // #312 — withSampleRate 활성 시 decodeAudioData 로 sample_rate/channels 보강.
      // 디코드 실패해도 duration 만 가지고 성공으로 resolve — 메타는 누락만 발생.
      try {
        const arrayBuf = await blob.arrayBuffer();
        const Ctor: typeof AudioContext | undefined =
          (window as unknown as { AudioContext?: typeof AudioContext }).AudioContext ||
          (window as unknown as { webkitAudioContext?: typeof AudioContext })
            .webkitAudioContext;
        if (!Ctor) {
          finish(() => resolve({ duration, source: "client" }));
          return;
        }
        const ctx = new Ctor();
        try {
          const buf = await ctx.decodeAudioData(arrayBuf.slice(0));
          finish(() =>
            resolve({
              duration: Number.isFinite(buf.duration) && buf.duration > 0 ? buf.duration : duration,
              sample_rate: buf.sampleRate,
              channels: buf.numberOfChannels,
              source: "client",
            }),
          );
        } catch {
          finish(() => resolve({ duration, source: "client" }));
        } finally {
          try {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const cl = (ctx as any).close;
            if (typeof cl === "function") cl.call(ctx).catch?.(() => {});
          } catch {
            /* ignore */
          }
        }
      } catch {
        finish(() => resolve({ duration, source: "client" }));
      }
    };
    audio.onerror = () => fail("extractAudioMeta: audio load error");

    timer = setTimeout(
      () => fail(`extractAudioMeta: timeout (${timeoutMs}ms)`),
      timeoutMs,
    );
    audio.src = url;
  });
}

/**
 * VideoMeta/AudioMeta가 sanity check 범위를 벗어나는지 검사한다.
 *
 * 백엔드와 동일한 범위로 클라이언트에서 미리 거른다 — 잘못된 메타로 백엔드에
 * 도달해 fallback ffprobe 가 추가로 호출되는 낭비를 줄이기 위함.
 * (백엔드도 동일 검사를 하지만, 클라에서 일찍 버리면 form payload 가 줄어든다.)
 */
export function isVideoMetaSane(meta: VideoMeta): boolean {
  return (
    Number.isFinite(meta.width) &&
    Number.isFinite(meta.height) &&
    Number.isFinite(meta.duration) &&
    meta.width >= 16 &&
    meta.width <= 16384 &&
    meta.height >= 16 &&
    meta.height <= 16384 &&
    meta.duration >= 0.1 &&
    meta.duration <= 3600
  );
}

export function isAudioMetaSane(meta: AudioMeta): boolean {
  return (
    Number.isFinite(meta.duration) &&
    meta.duration >= 0.1 &&
    meta.duration <= 3600
  );
}
