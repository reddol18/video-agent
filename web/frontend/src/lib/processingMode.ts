/**
 * 작업 특성별 자동 엔진 라우팅
 *
 * 사용자에게 엔진 선택을 노출하지 않고, 작업 종류에 따라 최적 엔진을 자동 결정한다.
 *
 * | 작업                             | 엔진              | 이유                            |
 * |--------------------------------|-------------------|---------------------------------|
 * | 켄번스, 디졸브, 페이드 등 시각 효과  | WebGL + MediaRecorder | GPU 활용, 실시간 처리             |
 * | webm 재인코딩, Time Warp, 속도 조절 | 서버 ffmpeg        | 멀티코어 + 하드웨어 가속           |
 * | 단순 mux (재인코딩 없음)            | ffmpeg.wasm       | 재인코딩 없으므로 브라우저도 충분    |
 */

export type EngineType = "webgl" | "server-ffmpeg" | "ffmpeg-wasm";

/**
 * 시각 효과(켄번스, 디졸브, 페이드)가 필요한 작업인지 판별.
 * 이 경우 WebGL + MediaRecorder 엔진을 사용한다.
 */
export function needsVisualEffect(effectType: string): boolean {
  const webglEffects = new Set([
    "kenburns",
    "kenburns_copy_img",
    "dissolve",
    "fade",
  ]);
  return webglEffects.has(effectType);
}

/**
 * 재인코딩이 필요한 작업인지 판별.
 * webm → mp4 변환, Time Warp, 속도 조절은 서버 ffmpeg를 사용한다.
 */
export function needsReencode(params: {
  inputFormat?: string;
  isTimeWarp?: boolean;
  isSpeedChange?: boolean;
}): boolean {
  const { inputFormat, isTimeWarp, isSpeedChange } = params;
  if (isTimeWarp || isSpeedChange) return true;
  if (inputFormat) {
    const reencodeFormats = new Set(["webm", "mov", "avi"]);
    return reencodeFormats.has(inputFormat.toLowerCase().replace(/^\./, ""));
  }
  return false;
}

/**
 * 작업 특성에 따라 최적 엔진을 자동 선택한다.
 *
 * @param params.effectType - 적용할 효과 유형 (kenburns, dissolve, fade, clip 등)
 * @param params.inputFormat - 입력 파일 확장자 (mp4, webm, mov 등)
 * @param params.isTimeWarp - Time Warp(footage 길이를 TTS에 맞춤) 여부
 * @param params.isSpeedChange - 재생 속도 조절 여부
 * @returns 선택된 엔진 유형
 */
export function selectEngine(params: {
  effectType?: string;
  inputFormat?: string;
  isTimeWarp?: boolean;
  isSpeedChange?: boolean;
}): EngineType {
  const { effectType, inputFormat, isTimeWarp, isSpeedChange } = params;

  // 시각 효과 → WebGL
  if (effectType && needsVisualEffect(effectType)) {
    return "webgl";
  }

  // 재인코딩 필요 → 서버 ffmpeg
  if (needsReencode({ inputFormat, isTimeWarp, isSpeedChange })) {
    return "server-ffmpeg";
  }

  // 단순 mux / trim → ffmpeg.wasm
  return "ffmpeg-wasm";
}

/**
 * footage 미리보기 생성 시 사용할 엔진을 결정한다.
 *
 * - 켄번스/디졸브/페이드 계열 씬 → WebGL (브라우저 GPU 가속)
 *   단, webglSupported === false 이면 서버 ffmpeg 폴백
 * - webm/mov/avi footage + TTS → 서버 ffmpeg (포맷 재인코딩 필요)
 * - mp4 footage + TTS → ffmpeg.wasm (재인코딩 없이 mux 가능)
 * - footage만 재생 (TTS 없음) → ffmpeg.wasm (단순 재생)
 */
export function selectPreviewEngine(params: {
  effectType?: string;
  footageFilename?: string;
  hasTts: boolean;
  webglSupported?: boolean;
  hasTextOverlays?: boolean;
  footageSpeed?: number;
}): EngineType {
  // 결정: 미리보기는 항상 서버 ffmpeg를 사용한다.
  //
  // 과거 경로별 분기를 유지했으나 fit/x_offset/rotation/clip_start_sec/clip_end_sec
  // 등 footage 변형은 클라이언트 ffmpeg.wasm이 적용하지 않아 사용자 의도와 다른 결과가
  // 표시되는 회귀가 반복 발생. 또한 ffmpeg.wasm은 브라우저 단일 스레드 libx264로
  // 사용자 체감 매우 느림. WebGL 경로는 MediaRecorder webm duration 메타 깨짐 이슈.
  //
  // 서버 ffmpeg는 다운스케일(#254) + amix duration=first(#268) 최적화로 4초 내외에
  // 처리되고, 모든 footage 변형을 정확히 반영한다. 일관성과 정확성을 우선해 통일.
  void params;
  return "server-ffmpeg";
}

/**
 * 브라우저(ffmpeg.wasm) 처리 모드 여부를 반환한다.
 * localStorage의 video_agent_processing_mode가 "browser"이거나 미설정(기본값)이면 true.
 */
export function isBrowserMode(): boolean {
  try {
    const mode = localStorage.getItem("video_agent_processing_mode");
    return mode === null || mode === "browser";
  } catch {
    return true;
  }
}

// 하위 호환성: 크롭은 항상 서버 ffmpeg 또는 ffmpeg.wasm을 사용
// (WebGL은 시각 효과 전용이므로 크롭에는 사용하지 않음)
export function selectCropEngine(inputFormat?: string): EngineType {
  if (inputFormat) {
    const ext = inputFormat.toLowerCase().replace(/^\./, "");
    if (needsReencode({ inputFormat: ext })) {
      return "server-ffmpeg";
    }
  }
  return "ffmpeg-wasm";
}
