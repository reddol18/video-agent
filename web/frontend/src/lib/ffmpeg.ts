/**
 * 브라우저 ffmpeg.wasm 래퍼
 * SharedArrayBuffer + COOP/COEP 헤더 필요
 */
import { FFmpeg } from "@ffmpeg/ffmpeg";
import { fetchFile, toBlobURL } from "@ffmpeg/util";
import { measureAudioDuration } from "./tts";

let _ffmpeg: FFmpeg | null = null;
let _loading: Promise<FFmpeg> | null = null;

const BASE_URL = "https://unpkg.com/@ffmpeg/core@0.12.6/dist/esm";

export async function getFFmpeg(): Promise<FFmpeg> {
  if (_ffmpeg?.loaded) return _ffmpeg;
  if (_loading) return _loading;

  _loading = (async () => {
    const ff = new FFmpeg();
    await ff.load({
      coreURL: await toBlobURL(`${BASE_URL}/ffmpeg-core.js`, "text/javascript"),
      wasmURL: await toBlobURL(`${BASE_URL}/ffmpeg-core.wasm`, "application/wasm"),
    });
    _ffmpeg = ff;
    return ff;
  })();

  return _loading;
}

export type ProgressCallback = (progress: number) => void;

function toBlob(data: unknown, type: string): Blob {
  // readFile은 SharedArrayBuffer 기반 Uint8Array를 반환할 수 있어 Blob에 직접 못 넣음
  // → 새 ArrayBuffer로 복사
  const u8 = data as Uint8Array;
  const copy = new Uint8Array(u8.length);
  copy.set(u8);
  return new Blob([copy], { type });
}

function getExt(f: Blob | File): string {
  if (f instanceof File) {
    const m = f.name.match(/\.[^.]+$/);
    return m ? m[0] : ".bin";
  }
  if (f.type.includes("webm")) return ".webm";
  if (f.type.includes("mp4")) return ".mp4";
  return ".bin";
}

/** webm/mov 등 → mp4 (faststart) 트랜스코딩 */
export async function transcodeToMp4(
  input: Blob | File,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const ff = await getFFmpeg();
  const inName = "input" + getExt(input);
  const outName = "output.mp4";

  const handler = onProgress ? ({ progress }: { progress: number }) => onProgress(Math.min(progress, 1)) : null;
  if (handler) ff.on("progress", handler);

  await ff.writeFile(inName, await fetchFile(input));
  await ff.exec([
    "-i", inName,
    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
    "-c:a", "aac",
    "-movflags", "+faststart",
    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    outName,
  ]);

  const data = await ff.readFile(outName);
  await ff.deleteFile(inName);
  await ff.deleteFile(outName);
  if (handler) ff.off("progress", handler);

  return toBlob(data as Uint8Array, "video/mp4");
}

/** 구간 자르기 (-c copy, 재인코딩 없음) */
export async function trimVideo(
  input: Blob | File,
  startSec: number,
  endSec: number,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const ff = await getFFmpeg();
  const inName = "input" + getExt(input);
  const outName = "output.mp4";

  const handler = onProgress ? ({ progress }: { progress: number }) => onProgress(Math.min(progress, 1)) : null;
  if (handler) ff.on("progress", handler);

  await ff.writeFile(inName, await fetchFile(input));
  await ff.exec([
    "-ss", String(startSec),
    "-to", String(endSec),
    "-i", inName,
    "-c", "copy",
    outName,
  ]);

  const data = await ff.readFile(outName);
  await ff.deleteFile(inName);
  await ff.deleteFile(outName);
  if (handler) ff.off("progress", handler);

  return toBlob(data as Uint8Array, "video/mp4");
}

/** 크롭 (비율 기반) + mp4 변환 */
export async function cropVideo(
  input: Blob | File,
  crop: { x: number; y: number; w: number; h: number },
  onProgress?: ProgressCallback
): Promise<Blob> {
  const ff = await getFFmpeg();
  const inName = "input" + getExt(input);
  const outName = "output.mp4";

  // 원본 해상도 파악
  let vw = 1920, vh = 1080;
  try {
    await ff.writeFile(inName, await fetchFile(input));
    const logs: string[] = [];
    const logHandler = ({ message }: { message: string }) => logs.push(message);
    ff.on("log", logHandler);
    await ff.exec(["-i", inName, "-t", "0", "-f", "null", "-"]).catch(() => {});
    ff.off("log", logHandler);
    const dim = logs.join("\n").match(/(\d{3,5})x(\d{3,5})/);
    if (dim) { vw = parseInt(dim[1]); vh = parseInt(dim[2]); }
  } catch { /* 기본값 사용 */ }

  const sx = Math.round(crop.x * vw) & ~1;
  const sy = Math.round(crop.y * vh) & ~1;
  const sw = Math.max(2, Math.round(crop.w * vw) & ~1);
  const sh = Math.max(2, Math.round(crop.h * vh) & ~1);

  const handler = onProgress ? ({ progress }: { progress: number }) => onProgress(Math.min(progress, 1)) : null;
  if (handler) ff.on("progress", handler);

  await ff.exec([
    "-i", inName,
    "-vf", `crop=${sw}:${sh}:${sx}:${sy}`,
    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
    "-c:a", "aac",
    "-movflags", "+faststart",
    outName,
  ]);

  const data = await ff.readFile(outName);
  await ff.deleteFile(inName);
  await ff.deleteFile(outName);
  if (handler) ff.off("progress", handler);

  return toBlob(data as Uint8Array, "video/mp4");
}

/** 픽셀 좌표로 직접 크롭 (해상도 probe 불필요) */
export async function cropVideoPixels(
  input: Blob,
  sx: number, sy: number, sw: number, sh: number,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const ff = await getFFmpeg();
  const inName = "input" + getExt(input);
  const outName = "output.mp4";
  await ff.writeFile(inName, await fetchFile(input));
  const handler = onProgress ? ({ progress }: { progress: number }) => onProgress(Math.min(progress, 1)) : null;
  if (handler) ff.on("progress", handler);
  await ff.exec([
    "-i", inName,
    "-vf", `crop=${sw & ~1}:${sh & ~1}:${sx & ~1}:${sy & ~1}`,
    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
    "-c:a", "aac", "-movflags", "+faststart",
    outName,
  ]);
  const data = await ff.readFile(outName);
  await ff.deleteFile(inName);
  await ff.deleteFile(outName);
  if (handler) ff.off("progress", handler);
  return toBlob(data as Uint8Array, "video/mp4");
}

/**
 * 두 클립을 dissolve(xfade) 전환으로 합친다.
 *
 * 성능 최적화 전략 (CLI _xfade_chunk와 동일):
 * - clipA의 [0, durationA - dissolveSec) → -c copy 임시 파일
 * - clipA의 [durationA - dissolveSec, durationA] + clipB의 [0, dissolveSec]
 *   → xfade 리인코딩 (dissolve 구간만, dissolveSec * 2 분량)
 * - clipB의 [dissolveSec, durationB) → -c copy 임시 파일
 * - 세 파트를 concat demuxer(-c copy)로 합침
 *
 * 트림 구간이 < 1초이면 전체 리인코딩 폴백.
 */
export async function dissolveMerge(
  clipA: Blob,
  clipB: Blob,
  dissolveSec: number = 0.5,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const ff = await getFFmpeg();
  const extA = getExt(clipA);
  const extB = getExt(clipB);
  const inA = "dissolve_a" + extA;
  const inB = "dissolve_b" + extB;
  const outName = "dissolve_out.mp4";

  const handler = onProgress
    ? ({ progress }: { progress: number }) => onProgress(Math.min(progress, 1))
    : null;
  if (handler) ff.on("progress", handler);

  await ff.writeFile(inA, await fetchFile(clipA));
  await ff.writeFile(inB, await fetchFile(clipB));

  // 각 클립의 길이를 ffprobe 대신 로그 파싱으로 측정
  async function getDuration(fileName: string): Promise<number> {
    const logs: string[] = [];
    const logHandler = ({ message }: { message: string }) => logs.push(message);
    ff.on("log", logHandler);
    await ff.exec(["-i", fileName, "-t", "0", "-f", "null", "-"]).catch(() => {});
    ff.off("log", logHandler);
    const combined = logs.join("\n");
    const m = combined.match(/Duration:\s*(\d+):(\d+):([\d.]+)/);
    if (m) {
      return parseInt(m[1]) * 3600 + parseInt(m[2]) * 60 + parseFloat(m[3]);
    }
    return 0;
  }

  const durA = await getDuration(inA);
  const durB = await getDuration(inB);

  let effectiveDiss = dissolveSec;
  const minDur = Math.min(durA, durB);
  if (effectiveDiss >= minDur / 2.0) {
    effectiveDiss = minDur / 4.0;
  }

  // 트림 구간이 1초 미만이면 전체 리인코딩 폴백
  const bodyA = durA - effectiveDiss;
  const tailB = durB - effectiveDiss;
  const useOptimized = bodyA >= 1.0 && tailB >= 1.0;

  if (!useOptimized) {
    // 폴백: 전체 리인코딩
    await ff.exec([
      "-i", inA,
      "-i", inB,
      "-filter_complex",
      `[0:v][1:v]xfade=transition=fade:duration=${effectiveDiss.toFixed(3)}:offset=${(durA - effectiveDiss).toFixed(3)}[vout]`,
      "-map", "[vout]",
      "-c:v", "libx264", "-crf", "23", "-preset", "fast",
      "-an",
      outName,
    ]);
  } else {
    // 최적화 경로: dissolve 구간만 리인코딩
    const partABody = "dissolve_a_body.mp4";
    const partXfade = "dissolve_xfade.mp4";
    const partBTail = "dissolve_b_tail.mp4";
    const listFile = "dissolve_list.txt";

    // 파트 A: [0, durA - effectiveDiss) -c copy
    await ff.exec([
      "-ss", "0",
      "-to", bodyA.toFixed(6),
      "-i", inA,
      "-c", "copy", "-an",
      partABody,
    ]);

    // xfade 구간: A 끝 effectiveDiss초 + B 앞 effectiveDiss초
    await ff.exec([
      "-ss", bodyA.toFixed(6),
      "-to", durA.toFixed(6),
      "-i", inA,
      "-ss", "0",
      "-to", effectiveDiss.toFixed(6),
      "-i", inB,
      "-filter_complex",
      `[0:v][1:v]xfade=transition=fade:duration=${effectiveDiss.toFixed(3)}:offset=0[vout]`,
      "-map", "[vout]",
      "-c:v", "libx264", "-crf", "23", "-preset", "fast",
      "-an",
      partXfade,
    ]);

    // 파트 B: [effectiveDiss, durB) -c copy
    await ff.exec([
      "-ss", effectiveDiss.toFixed(6),
      "-to", durB.toFixed(6),
      "-i", inB,
      "-c", "copy", "-an",
      partBTail,
    ]);

    // concat demuxer용 list.txt 작성
    const listContent = `file '${partABody}'\nfile '${partXfade}'\nfile '${partBTail}'\n`;
    const enc = new TextEncoder();
    await ff.writeFile(listFile, enc.encode(listContent));

    // concat -c copy
    await ff.exec([
      "-f", "concat", "-safe", "0",
      "-i", listFile,
      "-c", "copy",
      outName,
    ]);

    // 임시 파일 정리
    for (const f of [partABody, partXfade, partBTail, listFile]) {
      await ff.deleteFile(f).catch(() => {});
    }
  }

  const data = await ff.readFile(outName);
  await ff.deleteFile(inA);
  await ff.deleteFile(inB);
  await ff.deleteFile(outName);
  if (handler) ff.off("progress", handler);

  return toBlob(data as Uint8Array, "video/mp4");
}

/**
 * `<video>` 엘리먼트를 이용해 Blob의 재생 시간을 읽는다.
 * webm처럼 메타데이터에 duration=Infinity가 기록되는 포맷에서도
 * seekToEnd(currentTime = 1e9) 트릭으로 실제 길이를 얻는다.
 */
export function getVideoDuration(blob: Blob): Promise<number> {
  return new Promise((resolve) => {
    const video = document.createElement("video");
    video.preload = "metadata";
    const url = URL.createObjectURL(blob);
    video.src = url;

    video.addEventListener("loadedmetadata", () => {
      if (isFinite(video.duration) && video.duration > 0) {
        URL.revokeObjectURL(url);
        resolve(video.duration);
      } else {
        // duration=Infinity(webm 등) — seek trick
        video.currentTime = 1e9;
      }
    });

    video.addEventListener(
      "seeked",
      () => {
        URL.revokeObjectURL(url);
        resolve(video.duration);
      },
      { once: true }
    );
  });
}

/**
 * footage와 TTS 길이를 비교하여 footage를 TTS 길이에 맞추고, TTS 오디오를 mux한다.
 *
 * 케이스 A: TTS > footage — tpad=stop_mode=clone으로 마지막 프레임 freeze 후 TTS 오디오 mux
 *   ffmpeg -i footage -i tts
 *     -filter_complex "[0:v]tpad=stop_mode=clone:stop_duration={padDur}[v]"
 *     -map "[v]" -map 1:a
 *     -c:v libx264 -c:a aac -shortest output.mp4
 *
 * 케이스 B: footage >= TTS — footage를 TTS 길이로 trim 후 TTS 오디오 mux
 *   ffmpeg -i footage -i tts
 *     -map 0:v -map 1:a
 *     -c:v libx264 -preset fast -crf 23 -c:a aac -t {ttsDur} -movflags +faststart output.mp4
 *
 * 반환: footage 영상 + TTS 오디오가 합성된 mp4 Blob
 */
export async function timeWarpToTts(
  footageBlob: Blob,
  ttsBlob: Blob,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const ff = await getFFmpeg();

  const footageDur = await getVideoDuration(footageBlob);
  const ttsDur = await measureAudioDuration(ttsBlob);

  if (ttsDur <= 0) {
    // TTS 길이를 알 수 없으면 footage 그대로 반환
    return footageBlob;
  }

  const inFootage = "timewarp_in" + getExt(footageBlob);
  const inTts = "timewarp_tts.wav";
  const outName = "timewarp_out.mp4";

  const handler = onProgress
    ? ({ progress }: { progress: number }) => onProgress(Math.min(progress, 1))
    : null;
  if (handler) ff.on("progress", handler);

  await ff.writeFile(inFootage, await fetchFile(footageBlob));
  // ttsBlob을 Uint8Array로 변환하여 writeFile
  const ttsArrayBuffer = await ttsBlob.arrayBuffer();
  await ff.writeFile(inTts, new Uint8Array(ttsArrayBuffer));

  if (ttsDur > footageDur && footageDur > 0) {
    // 케이스 A: TTS가 footage보다 긺 — tpad freeze 후 TTS 오디오 mux
    const padDur = ttsDur - footageDur;
    await ff.exec([
      "-i", inFootage,
      "-i", inTts,
      "-filter_complex", `[0:v]tpad=stop_mode=clone:stop_duration=${padDur.toFixed(6)}[v]`,
      "-map", "[v]",
      "-map", "1:a",
      "-c:v", "libx264", "-preset", "fast", "-crf", "23",
      "-c:a", "aac",
      "-shortest",
      "-movflags", "+faststart",
      outName,
    ]);
  } else {
    // 케이스 B: footage가 TTS보다 길거나 같음 — footage 원본 길이 유지 (#268)
    // TTS는 footage 길이까지 apad로 패딩하여 끝까지 재생되고 이후 구간은 무음.
    // 이전엔 "-t ttsDur"로 footage를 잘랐으나 사용자 의도와 정반대였음:
    //   "TTS가 영상보다 길 때 맞추는건 맞지만 반대의 경우는 아니야".
    // amix duration=first로 footage 길이(첫 입력) 기준 고정.
    await ff.exec([
      "-i", inFootage,
      "-i", inTts,
      "-filter_complex", `[1:a]apad=whole_dur=${footageDur.toFixed(6)}[aout]`,
      "-map", "0:v",
      "-map", "[aout]",
      "-c:v", "libx264", "-preset", "fast", "-crf", "23",
      "-c:a", "aac",
      "-movflags", "+faststart",
      outName,
    ]);
  }

  const data = await ff.readFile(outName);
  await ff.deleteFile(inFootage);
  await ff.deleteFile(inTts);
  await ff.deleteFile(outName);
  if (handler) ff.off("progress", handler);

  return toBlob(data as Uint8Array, "video/mp4");
}

/** 이미지/비디오에서 썸네일 추출 */
export async function extractThumbnail(
  input: Blob | File,
  isVideo: boolean
): Promise<Blob> {
  const ff = await getFFmpeg();
  const inName = "input" + getExt(input);
  const outName = "thumb.jpg";

  await ff.writeFile(inName, await fetchFile(input));
  await ff.exec(isVideo
    ? ["-ss", "0", "-i", inName, "-frames:v", "1", "-q:v", "3", "-vf", "scale=320:-1", outName]
    : ["-i", inName, "-vf", "scale=320:-1", outName]
  );

  const data = await ff.readFile(outName);
  await ff.deleteFile(inName);
  await ff.deleteFile(outName);

  return toBlob(data as Uint8Array, "image/jpeg");
}
