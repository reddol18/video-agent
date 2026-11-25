/**
 * WebGL + MediaRecorder 기반 시각 효과 렌더러
 *
 * 켄번스(Ken Burns), 디졸브(Dissolve), 페이드(Fade) 효과를
 * WebGL GPU 가속으로 렌더링하고 MediaRecorder로 인코딩한다.
 *
 * 브라우저 지원 요구사항:
 * - WebGL 2 (또는 WebGL 1 폴백)
 * - MediaRecorder with video/webm;codecs=vp9 또는 video/webm
 * - canvas.captureStream()
 */

import { fixWebmDuration } from "./webmDurationFix";

export type ProgressCallback = (progress: number) => void;

// ── WebGL 유틸리티 ─────────────────────────────────────────────────────────

function createShader(gl: WebGLRenderingContext, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("WebGL shader 생성 실패");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`WebGL shader 컴파일 실패: ${info}`);
  }
  return shader;
}

function createProgram(gl: WebGLRenderingContext, vertSrc: string, fragSrc: string): WebGLProgram {
  const vert = createShader(gl, gl.VERTEX_SHADER, vertSrc);
  const frag = createShader(gl, gl.FRAGMENT_SHADER, fragSrc);
  const program = gl.createProgram();
  if (!program) throw new Error("WebGL program 생성 실패");
  gl.attachShader(program, vert);
  gl.attachShader(program, frag);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(program);
    gl.deleteProgram(program);
    throw new Error(`WebGL program 링크 실패: ${info}`);
  }
  gl.deleteShader(vert);
  gl.deleteShader(frag);
  return program;
}

// 공통 정점 셰이더: 전체 화면 쿼드
const COMMON_VERT = `
  attribute vec2 a_position;
  attribute vec2 a_texCoord;
  varying vec2 v_texCoord;
  void main() {
    gl_Position = vec4(a_position, 0.0, 1.0);
    v_texCoord = a_texCoord;
  }
`;

// 전체 화면 쿼드 geometry 설정
function setupQuad(gl: WebGLRenderingContext, program: WebGLProgram) {
  const positions = new Float32Array([
    -1, -1,  1, -1,  -1,  1,
     1, -1,  1,  1,  -1,  1,
  ]);
  const texCoords = new Float32Array([
    0, 1,  1, 1,  0, 0,
    1, 1,  1, 0,  0, 0,
  ]);

  const posBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
  gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
  const posLoc = gl.getAttribLocation(program, "a_position");
  gl.enableVertexAttribArray(posLoc);
  gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

  const texBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, texBuf);
  gl.bufferData(gl.ARRAY_BUFFER, texCoords, gl.STATIC_DRAW);
  const texLoc = gl.getAttribLocation(program, "a_texCoord");
  gl.enableVertexAttribArray(texLoc);
  gl.vertexAttribPointer(texLoc, 2, gl.FLOAT, false, 0, 0);
}

function createTexture(gl: WebGLRenderingContext): WebGLTexture {
  const tex = gl.createTexture();
  if (!tex) throw new Error("WebGL texture 생성 실패");
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  return tex;
}

// ── MediaRecorder 래퍼 ──────────────────────────────────────────────────────

function startRecorder(canvas: HTMLCanvasElement, fps: number): {
  recorder: MediaRecorder;
  chunks: Blob[];
  mimeType: string;
} {
  const stream = canvas.captureStream(fps);
  const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
    ? "video/webm;codecs=vp9"
    : "video/webm";
  const chunks: Blob[] = [];
  const recorder = new MediaRecorder(stream, { mimeType });
  recorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data); };
  recorder.start(100);
  return { recorder, chunks, mimeType };
}

// ── 켄번스 (Ken Burns) 렌더러 ──────────────────────────────────────────────

/**
 * 켄번스 효과 파라미터
 * 이미지에서 시작 뷰포트 → 종료 뷰포트로 서서히 이동/줌인하는 효과.
 */
export interface KenBurnsParams {
  /** 총 재생 시간 (초) */
  durationSec: number;
  /** 시작 스케일 (1.0 = 원본 크기, > 1.0 이면 줌인 시작) */
  startScale?: number;
  /** 종료 스케일 */
  endScale?: number;
  /** 시작 중심점 (0~1 정규화 좌표) */
  startCenter?: { x: number; y: number };
  /** 종료 중심점 */
  endCenter?: { x: number; y: number };
  /** FPS */
  fps?: number;
  /** 출력 해상도 */
  width?: number;
  height?: number;
}

/**
 * 이미지 또는 비디오 프레임에 켄번스 효과를 적용하여 webm Blob을 반환한다.
 *
 * @param source - HTMLImageElement 또는 HTMLVideoElement
 * @param params - 켄번스 파라미터
 * @param onProgress - 진행률 콜백 (0~1)
 */
export async function renderKenBurns(
  source: HTMLImageElement | HTMLVideoElement,
  params: KenBurnsParams,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const {
    durationSec,
    startScale = 1.1,
    endScale = 1.0,
    startCenter = { x: 0.5, y: 0.5 },
    endCenter = { x: 0.5, y: 0.5 },
    fps = 30,
    width = 1280,
    height = 720,
  } = params;

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;

  const glRaw = canvas.getContext("webgl") ?? canvas.getContext("experimental-webgl") as WebGLRenderingContext | null;
  if (!glRaw) throw new Error("WebGL을 지원하지 않는 브라우저입니다.");
  const gl = glRaw as WebGLRenderingContext;

  const fragSrc = `
    precision mediump float;
    uniform sampler2D u_texture;
    uniform vec2 u_center;
    uniform float u_scale;
    uniform vec2 u_canvasAspect; // (canvasW/canvasH, 1.0) normalized
    uniform vec2 u_texAspect;    // (texW/texH, 1.0) normalized
    varying vec2 v_texCoord;

    void main() {
      // 캔버스 중앙 기준 UV 오프셋
      vec2 uv = v_texCoord - u_center;
      // 스케일 적용
      uv = uv / u_scale;
      // 중앙으로 복귀
      uv = uv + vec2(0.5, 0.5);
      // 범위 벗어나면 가장자리 클램프
      uv = clamp(uv, 0.0, 1.0);
      gl_FragColor = texture2D(u_texture, uv);
    }
  `;

  const program = createProgram(gl, COMMON_VERT, fragSrc);
  gl.useProgram(program);
  setupQuad(gl, program);

  const tex = createTexture(gl);
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);

  const uTexture = gl.getUniformLocation(program, "u_texture");
  const uCenter = gl.getUniformLocation(program, "u_center");
  const uScale = gl.getUniformLocation(program, "u_scale");

  const { recorder, chunks, mimeType } = startRecorder(canvas, fps);

  const totalFrames = Math.ceil(durationSec * fps);

  await new Promise<void>((resolve) => {
    let frame = 0;
    function drawFrame() {
      const t = totalFrames > 1 ? frame / (totalFrames - 1) : 0;
      const scale = startScale + (endScale - startScale) * t;
      const cx = startCenter.x + (endCenter.x - startCenter.x) * t;
      const cy = startCenter.y + (endCenter.y - startCenter.y) * t;

      gl.viewport(0, 0, width, height);
      gl.uniform1i(uTexture, 0);
      gl.uniform2f(uCenter, cx, cy);
      gl.uniform1f(uScale, scale);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      onProgress?.(frame / totalFrames);
      frame++;

      if (frame >= totalFrames) {
        resolve();
        return;
      }
      requestAnimationFrame(drawFrame);
    }
    drawFrame();
  });

  return new Promise((resolve) => {
    recorder.onstop = async () => {
      const blob = new Blob(chunks, { type: mimeType });
      // MediaRecorder webm은 Segment Duration이 비어 있어 일부 플레이어에서
      // 길이가 1초 등으로 잘못 표시된다. 실제 렌더링된 길이로 보정.
      const fixed = await fixWebmDuration(blob, durationSec * 1000).catch(() => blob);
      resolve(fixed);
    };
    recorder.stop();
  });
}

// ── 디졸브 (Dissolve) 렌더러 ───────────────────────────────────────────────

/**
 * 두 클립/이미지 사이에 디졸브(cross-fade) 전환 효과를 렌더링한다.
 *
 * @param sourceA - 첫 번째 소스 (HTMLImageElement | HTMLVideoElement)
 * @param sourceB - 두 번째 소스
 * @param durationSec - 디졸브 시간 (초)
 * @param fps - 프레임레이트
 * @param onProgress - 진행률 콜백
 */
export async function renderDissolve(
  sourceA: HTMLImageElement | HTMLVideoElement,
  sourceB: HTMLImageElement | HTMLVideoElement,
  durationSec: number = 0.5,
  fps: number = 30,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const width = (sourceA as HTMLVideoElement).videoWidth
    || (sourceA as HTMLImageElement).naturalWidth
    || 1280;
  const height = (sourceA as HTMLVideoElement).videoHeight
    || (sourceA as HTMLImageElement).naturalHeight
    || 720;

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;

  const glRaw = canvas.getContext("webgl") ?? canvas.getContext("experimental-webgl") as WebGLRenderingContext | null;
  if (!glRaw) throw new Error("WebGL을 지원하지 않는 브라우저입니다.");
  const gl = glRaw as WebGLRenderingContext;

  const fragSrc = `
    precision mediump float;
    uniform sampler2D u_texA;
    uniform sampler2D u_texB;
    uniform float u_mix;
    varying vec2 v_texCoord;

    void main() {
      vec4 colorA = texture2D(u_texA, v_texCoord);
      vec4 colorB = texture2D(u_texB, v_texCoord);
      gl_FragColor = mix(colorA, colorB, u_mix);
    }
  `;

  const program = createProgram(gl, COMMON_VERT, fragSrc);
  gl.useProgram(program);
  setupQuad(gl, program);

  // 텍스처 A
  const texA = createTexture(gl);
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, texA);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, sourceA);

  // 텍스처 B
  const texB = createTexture(gl);
  gl.activeTexture(gl.TEXTURE1);
  gl.bindTexture(gl.TEXTURE_2D, texB);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, sourceB);

  const uTexA = gl.getUniformLocation(program, "u_texA");
  const uTexB = gl.getUniformLocation(program, "u_texB");
  const uMix = gl.getUniformLocation(program, "u_mix");
  gl.uniform1i(uTexA, 0);
  gl.uniform1i(uTexB, 1);

  const { recorder, chunks, mimeType } = startRecorder(canvas, fps);
  const totalFrames = Math.ceil(durationSec * fps);

  await new Promise<void>((resolve) => {
    let frame = 0;
    function drawFrame() {
      const t = totalFrames > 1 ? frame / (totalFrames - 1) : 1;
      gl.viewport(0, 0, width, height);
      gl.uniform1f(uMix, t);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      onProgress?.(frame / totalFrames);
      frame++;
      if (frame >= totalFrames) { resolve(); return; }
      requestAnimationFrame(drawFrame);
    }
    drawFrame();
  });

  return new Promise((resolve) => {
    recorder.onstop = async () => {
      const blob = new Blob(chunks, { type: mimeType });
      const fixed = await fixWebmDuration(blob, durationSec * 1000).catch(() => blob);
      resolve(fixed);
    };
    recorder.stop();
  });
}

// ── 페이드인/페이드아웃 렌더러 ─────────────────────────────────────────────

/**
 * 페이드인 또는 페이드아웃 효과를 렌더링한다.
 *
 * @param source - 소스 미디어
 * @param durationSec - 효과 시간 (초)
 * @param direction - "in" (검정 → 이미지) | "out" (이미지 → 검정)
 * @param fps - 프레임레이트
 * @param onProgress - 진행률 콜백
 */
export async function renderFade(
  source: HTMLImageElement | HTMLVideoElement,
  durationSec: number = 0.3,
  direction: "in" | "out" = "in",
  fps: number = 30,
  onProgress?: ProgressCallback
): Promise<Blob> {
  const width = (source as HTMLVideoElement).videoWidth
    || (source as HTMLImageElement).naturalWidth
    || 1280;
  const height = (source as HTMLVideoElement).videoHeight
    || (source as HTMLImageElement).naturalHeight
    || 720;

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;

  const glRaw = canvas.getContext("webgl") ?? canvas.getContext("experimental-webgl") as WebGLRenderingContext | null;
  if (!glRaw) throw new Error("WebGL을 지원하지 않는 브라우저입니다.");
  const gl = glRaw as WebGLRenderingContext;

  const fragSrc = `
    precision mediump float;
    uniform sampler2D u_texture;
    uniform float u_alpha;
    varying vec2 v_texCoord;

    void main() {
      vec4 color = texture2D(u_texture, v_texCoord);
      gl_FragColor = vec4(color.rgb * u_alpha, color.a);
    }
  `;

  const program = createProgram(gl, COMMON_VERT, fragSrc);
  gl.useProgram(program);
  setupQuad(gl, program);

  const tex = createTexture(gl);
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);

  const uTexture = gl.getUniformLocation(program, "u_texture");
  const uAlpha = gl.getUniformLocation(program, "u_alpha");
  gl.uniform1i(uTexture, 0);

  const { recorder, chunks, mimeType } = startRecorder(canvas, fps);
  const totalFrames = Math.ceil(durationSec * fps);

  await new Promise<void>((resolve) => {
    let frame = 0;
    function drawFrame() {
      const t = totalFrames > 1 ? frame / (totalFrames - 1) : 1;
      const alpha = direction === "in" ? t : 1 - t;

      gl.viewport(0, 0, width, height);
      gl.uniform1f(uAlpha, alpha);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      onProgress?.(frame / totalFrames);
      frame++;
      if (frame >= totalFrames) { resolve(); return; }
      requestAnimationFrame(drawFrame);
    }
    drawFrame();
  });

  return new Promise((resolve) => {
    recorder.onstop = async () => {
      const blob = new Blob(chunks, { type: mimeType });
      const fixed = await fixWebmDuration(blob, durationSec * 1000).catch(() => blob);
      resolve(fixed);
    };
    recorder.stop();
  });
}

// ── 지원 여부 확인 ──────────────────────────────────────────────────────────

/**
 * 현재 브라우저에서 WebGL + MediaRecorder 기반 렌더링이 가능한지 확인한다.
 */
export function isWebGLRenderingSupported(): boolean {
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") ?? canvas.getContext("experimental-webgl");
    if (!gl) return false;
    if (!canvas.captureStream) return false;
    if (!window.MediaRecorder) return false;
    return true;
  } catch {
    return false;
  }
}
