#!/usr/bin/env node
/**
 * 오버레이 합성 모드 판정 + 위치 환산 검증 — 이슈 #310.
 *
 * 검증 항목
 * ────────
 *  1. `isOverlayRenderSpec(spec)` 판정이 다양한 layers 조합에서 기대값과 일치.
 *  2. RenderSpec 의 TextLayer/ShapeLayer/GraphicLayer 절대 픽셀 좌표가 CSS 비율(%)로
 *     정확히 환산되는지 (소수 오차 1e-9 이내).
 *
 * 실행:
 *   node scripts/verify_overlay_render_parity.mjs
 *
 * 결과:
 *   - 모든 케이스 통과 → exit 0
 *   - 어긋난 케이스 발견 → 첫 5개 출력 + exit 1
 *
 * 본 스크립트는 vitest / npm 의존성 없이 Node 만으로 실행된다. 핵심 알고리즘(판정 함수와
 * 위치 환산)을 TS 원본과 1:1 미러로 복제하여 검증한다. TS 원본이 변경되면 본 스크립트도
 * 함께 업데이트해야 한다.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const REPO_ROOT = resolve(__dirname, "..");
const TS_SOURCE = resolve(REPO_ROOT, "web/frontend/src/lib/isOverlayRenderSpec.ts");

const EPSILON = 1e-9;

// ── 알고리즘 미러: isOverlayRenderSpec ─────────────────────────────────────
// TS 원본의 핵심 본문을 그대로 옮긴 함수. TS 와 일치하지 않으면 drift 검출 단계에서 실패.
function isOverlayRenderSpecMirror(spec) {
  if (!spec) return false;
  const layers = spec.layers ?? [];
  if (layers.length === 0) return false;
  const videoLayers = layers.filter((l) => l && l.type === "video");
  if (videoLayers.length !== 1) return false;
  const vl = videoLayers[0];
  if (vl.opacity !== undefined && vl.opacity !== 1.0) return false;
  if (vl.rotation_deg !== undefined && vl.rotation_deg !== 0) return false;
  for (const l of layers) {
    const t = l?.type;
    if (t === "video") continue;
    if (t === "text" || t === "shape" || t === "graphic") continue;
    return false;
  }
  return true;
}

// ── 위치 환산 미러: OverlayCanvas 의 좌표 → % 비율 ──────────────────────────
function textLayerCss(layer, canvas) {
  return {
    leftPct: (layer.position_x / canvas.width) * 100,
    topPct: (layer.position_y / canvas.height) * 100,
    fontSizeRatio: layer.font_size_px / canvas.height,
  };
}
function shapeLayerCss(layer, canvas) {
  return {
    leftPct: (layer.x / canvas.width) * 100,
    topPct: (layer.y / canvas.height) * 100,
    widthPct: (layer.width / canvas.width) * 100,
    heightPct: (layer.height / canvas.height) * 100,
  };
}
function graphicLayerCss(layer, canvas) {
  return {
    leftPct: (layer.center_x / canvas.width) * 100,
    topPct: (layer.center_y / canvas.height) * 100,
  };
}

// ── 케이스 ────────────────────────────────────────────────────────────────

const canvas1080p = { width: 1920, height: 1080 };
const canvas9_16 = { width: 1080, height: 1920 };

const baseVideoLayer = {
  type: "video",
  source: "foo.mp4",
  source_size: { width: 1920, height: 1080 },
  scale_x: 1.0,
  scale_y: 1.0,
  opacity: 1.0,
  rotation_deg: 0,
  playback_rate: 1.0,
};

const PASS_CASES = [
  // 1. video 1개 — 가장 단순 (simple 도 통과)
  { name: "video only", spec: { canvas: canvas1080p, layers: [baseVideoLayer] }, expected: true },
  // 2. video + text 1개
  {
    name: "video + text",
    spec: {
      canvas: canvas1080p,
      layers: [
        baseVideoLayer,
        { type: "text", text: "안녕", position_x: 960, position_y: 540, font_size_px: 48 },
      ],
    },
    expected: true,
  },
  // 3. video + text + shape + graphic 혼합
  {
    name: "video + text + shape + graphic",
    spec: {
      canvas: canvas9_16,
      layers: [
        baseVideoLayer,
        { type: "text", text: "타이틀", position_x: 540, position_y: 200, font_size_px: 64 },
        { type: "shape", shape: "rect", x: 100, y: 1000, width: 880, height: 200, color: "#ff0000" },
        { type: "graphic", graphic_type: "spotlight", center_x: 540, center_y: 960 },
      ],
    },
    expected: true,
  },
];

const FAIL_CASES = [
  // 1. layers 비어있음
  { name: "empty layers", spec: { canvas: canvas1080p, layers: [] }, expected: false },
  // 2. video 없음
  {
    name: "no video",
    spec: { canvas: canvas1080p, layers: [{ type: "text", text: "x", position_x: 0, position_y: 0, font_size_px: 40 }] },
    expected: false,
  },
  // 3. video 2개
  {
    name: "two videos",
    spec: { canvas: canvas1080p, layers: [baseVideoLayer, baseVideoLayer] },
    expected: false,
  },
  // 4. video + image — image 차단
  {
    name: "video + image (kenburns)",
    spec: {
      canvas: canvas1080p,
      layers: [
        baseVideoLayer,
        { type: "image", source: "i.png", source_size: { width: 1, height: 1 }, scale_x: 1, scale_y: 1 },
      ],
    },
    expected: false,
  },
  // 5. video + filter — filter 차단
  {
    name: "video + filter",
    spec: {
      canvas: canvas1080p,
      layers: [baseVideoLayer, { type: "filter", filter_type: "vivid", intensity: 1 }],
    },
    expected: false,
  },
  // 6. video + motion — motion 차단
  {
    name: "video + motion",
    spec: {
      canvas: canvas1080p,
      layers: [
        baseVideoLayer,
        { type: "motion", motion_type: "shake", motion_start_sec: 0, motion_duration_sec: 1 },
      ],
    },
    expected: false,
  },
  // 7. video + mosaic — mosaic 차단
  {
    name: "video + mosaic",
    spec: {
      canvas: canvas1080p,
      layers: [
        baseVideoLayer,
        { type: "mosaic", x: 100, y: 100, width: 200, height: 200, mode: "pixelate", pixel_size: 20 },
      ],
    },
    expected: false,
  },
  // 8. video 회전
  {
    name: "video rotated 90deg",
    spec: { canvas: canvas1080p, layers: [{ ...baseVideoLayer, rotation_deg: 90 }] },
    expected: false,
  },
  // 9. video opacity != 1
  {
    name: "video opacity 0.5",
    spec: { canvas: canvas1080p, layers: [{ ...baseVideoLayer, opacity: 0.5 }] },
    expected: false,
  },
];

// ── 위치 환산 케이스 ──────────────────────────────────────────────────────

const POSITION_CASES = [
  {
    name: "text center on 1920x1080",
    canvas: canvas1080p,
    layer: { type: "text", text: "t", position_x: 960, position_y: 540, font_size_px: 108 },
    expected: { leftPct: 50.0, topPct: 50.0, fontSizeRatio: 0.1 },
    fn: textLayerCss,
  },
  {
    name: "text top-left",
    canvas: canvas1080p,
    layer: { type: "text", text: "t", position_x: 0, position_y: 0, font_size_px: 54 },
    expected: { leftPct: 0.0, topPct: 0.0, fontSizeRatio: 0.05 },
    fn: textLayerCss,
  },
  {
    name: "shape rect 9:16 lower banner",
    canvas: canvas9_16,
    layer: { type: "shape", shape: "rect", x: 108, y: 1728, width: 864, height: 192, color: "#ff0000" },
    expected: { leftPct: 10.0, topPct: 90.0, widthPct: 80.0, heightPct: 10.0 },
    fn: shapeLayerCss,
  },
  {
    name: "graphic spotlight center 1080x1080",
    canvas: { width: 1080, height: 1080 },
    layer: { type: "graphic", graphic_type: "spotlight", center_x: 540, center_y: 540 },
    expected: { leftPct: 50.0, topPct: 50.0 },
    fn: graphicLayerCss,
  },
];

// ── Drift 검출: TS 원본의 핵심 라인이 본 스크립트와 일치하는지 확인 ───────
function checkDrift() {
  const src = readFileSync(TS_SOURCE, "utf-8");
  const required = [
    'videoLayers.length !== 1',
    'vl.opacity !== undefined && vl.opacity !== 1.0',
    'vl.rotation_deg !== undefined && vl.rotation_deg !== 0',
    't === "text" || t === "shape" || t === "graphic"',
  ];
  const missing = required.filter((s) => !src.includes(s));
  if (missing.length > 0) {
    console.error("[verify_overlay_render_parity] DRIFT 검출 — TS 원본에서 다음 핵심 표현을 찾지 못함:");
    for (const m of missing) console.error("  -", m);
    process.exit(3);
  }
}

// ── 메인 ─────────────────────────────────────────────────────────────────

function main() {
  checkDrift();

  const failures = [];

  for (const c of [...PASS_CASES, ...FAIL_CASES]) {
    const actual = isOverlayRenderSpecMirror(c.spec);
    if (actual !== c.expected) {
      failures.push({ kind: "judge", name: c.name, expected: c.expected, actual });
    }
  }

  for (const c of POSITION_CASES) {
    const out = c.fn(c.layer, c.canvas);
    for (const key of Object.keys(c.expected)) {
      if (Math.abs(out[key] - c.expected[key]) > EPSILON) {
        failures.push({
          kind: "position",
          name: `${c.name} / ${key}`,
          expected: c.expected[key],
          actual: out[key],
        });
      }
    }
  }

  if (failures.length > 0) {
    console.error(`[verify_overlay_render_parity] FAIL — ${failures.length}건 (앞 5건):`);
    for (const f of failures.slice(0, 5)) {
      console.error(`  - [${f.kind}] ${f.name}: expected=${f.expected}, actual=${f.actual}`);
    }
    process.exit(1);
  }

  console.log(
    `[verify_overlay_render_parity] OK — 판정 ${PASS_CASES.length + FAIL_CASES.length}건, ` +
      `위치 환산 ${POSITION_CASES.length}건 모두 통과.`,
  );
}

main();
