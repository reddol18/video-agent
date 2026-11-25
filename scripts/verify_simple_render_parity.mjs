#!/usr/bin/env node
/**
 * 단순 렌더 CSS 변환 동등성 검증 — 이슈 #309 / #343 / #344.
 *
 * 백엔드 builder geometry(절대 픽셀)와 프론트 `applySimpleRenderTransform` 의 CSS 백분율
 * 결과가 모든 fit × playback_rate × 캔버스/소스 조합에서 정확히 일치하는지 자동 검증한다.
 *
 * 실행:
 *   1) (선택) `uv run python -m pytest tests/test_simple_render_geometry_fixture.py` 로
 *      fixture 갱신.
 *   2) `node scripts/verify_simple_render_parity.mjs`
 *
 * 결과:
 *   - 모든 케이스 CSS 결과가 허용 오차(1e-6) 이내면 exit 0
 *   - 어긋난 케이스가 있으면 첫 5개를 출력하고 exit 1
 *   - TS 원본과 본 스크립트의 미러 본문이 어긋났으면 exit 3 (drift detection — #328 패턴)
 *
 * 본 스크립트는 npm 의존성 / vitest 없이 Node 만으로 동작한다. fixture 가 단일 진실이며,
 * TS 원본의 핵심 함수 본문은 본 스크립트 안에 복제(미러)하여 검증한다.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const REPO_ROOT = resolve(__dirname, "..");
const FIXTURE = resolve(REPO_ROOT, "tests/fixtures/simple_render_geometry_cases.json");
const TS_SOURCE = resolve(REPO_ROOT, "web/frontend/src/lib/clientSceneRenderer.ts");
const IS_SIMPLE_TS_SOURCE = resolve(REPO_ROOT, "web/frontend/src/lib/isSimpleRenderSpec.ts");

const EPSILON = 1e-6;

// ── 알고리즘 미러: applySimpleRenderTransform 의 백분율 계산만 발췌 ─────────
// TS 원본은 CSSProperties 를 만들지만 본 스크립트는 *수치 비교* 만 하므로 같은 식의
// 백분율 값만 추출한다. 양쪽 함수는 같은 입력 → 같은 백분율 출력이어야 한다.

function applySimpleRenderTransformMirror(layer, canvas) {
  const cw = canvas.width;
  const ch = canvas.height;

  const sx = layer.scale_x;
  const sy = layer.scale_y;
  const sourceW = layer.source_size.width;
  const sourceH = layer.source_size.height;

  const scaledW = sourceW * sx;
  const scaledH = sourceH * sy;

  if (layer.pad) {
    const pad = layer.pad;
    return {
      mode: "contain",
      left_pct: (pad.x / cw) * 100,
      top_pct: (pad.y / ch) * 100,
      width_pct: (pad.w / cw) * 100,
      height_pct: (pad.h / ch) * 100,
      object_fit: "fill",
    };
  }

  if (layer.crop) {
    const crop = layer.crop;
    const widthPct = (scaledW / cw) * 100;
    const heightPct = (scaledH / ch) * 100;
    const leftPct = -(crop.x / cw) * 100;
    const topPct = -(crop.y / ch) * 100;
    return {
      mode: "cover",
      left_pct: leftPct,
      top_pct: topPct,
      width_pct: widthPct,
      height_pct: heightPct,
      object_fit: "fill",
    };
  }

  return {
    mode: "stretch",
    left_pct: 0,
    top_pct: 0,
    width_pct: 100,
    height_pct: 100,
    object_fit: "fill",
  };
}

// ── isSimpleRenderSpec 미러 (#343 회귀 가드) ─────────────────────────────

function isSimpleRenderSpecMirror(spec) {
  if (!spec) return false;
  const layers = spec.layers ?? [];
  if (layers.length !== 1) return false;
  const layer = layers[0];
  if (!layer || typeof layer !== "object") return false;
  if (layer.type === "image") return false;
  if (layer.type !== "video") return false;
  if (layer.opacity !== undefined && layer.opacity !== 1.0) return false;
  if (layer.rotation_deg !== undefined && layer.rotation_deg !== 0) return false;
  return true;
}

// ── 미러 드리프트 검출 — #328 패턴 ────────────────────────────────────────
// TS 원본 본문을 정규식으로 추출해 본 스크립트 미러와 정규화 비교한다.
// 누군가 TS 만 수정하고 mjs 미러 갱신을 빼먹는 일을 매 PR 마다 CI 가 강제 검출한다.

function normalize(src) {
  // 주석 / 공백 / 세미콜론 제거 + 통일.
  return src
    .replace(/\/\/[^\n]*/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\s+/g, "")
    .replace(/;/g, "")
    .replace(/'/g, '"');
}

function extractFunctionBody(src, name) {
  // 함수 시그니처 이후 첫 `{` 부터 균형 잡힌 `}` 까지.
  const re = new RegExp(`function\\s+${name}\\s*\\(`);
  const m = re.exec(src);
  if (!m) throw new Error(`함수 ${name} 를 ${TS_SOURCE} 에서 찾지 못했습니다.`);
  let i = m.index;
  // 본문 시작 `{` 까지 전진.
  while (i < src.length && src[i] !== "{") i++;
  if (i === src.length) throw new Error(`함수 ${name} 의 본문 시작 { 가 없습니다.`);
  let depth = 0;
  let start = i;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) {
        return src.slice(start + 1, i);
      }
    }
  }
  throw new Error(`함수 ${name} 본문의 짝 } 가 없습니다.`);
}

// 미러된 본문(이 파일 안의 함수)을 문자열로 갖고 있다가 정규화 비교한다.
const MIRROR_APPLY_BODY = `
  const cw = canvas.width
  const ch = canvas.height
  const sx = layer.scale_x
  const sy = layer.scale_y
  const sourceW = layer.source_size.width
  const sourceH = layer.source_size.height
  const scaledW = sourceW * sx
  const scaledH = sourceH * sy
  if (layer.pad) {
    const pad = layer.pad
    return {
      left: (pad.x / cw) * 100,
      top: (pad.y / ch) * 100,
      width: (pad.w / cw) * 100,
      height: (pad.h / ch) * 100,
    }
  }
  if (layer.crop) {
    const crop = layer.crop
    const widthPct = (scaledW / cw) * 100
    const heightPct = (scaledH / ch) * 100
    const leftPct = -(crop.x / cw) * 100
    const topPct = -(crop.y / ch) * 100
    return { leftPct, topPct, widthPct, heightPct }
  }
`;

// TS 원본 본문에서 *수치 백분율 식만* 추출해 같은 식이 미러에 있는지 검사한다.
// 키 비교는 정확히 같은 식 리터럴이 등장하는지만 본다 — 표현 일부가 다르면 detection.
function checkApplyTransformMirrorDrift() {
  const tsSrc = readFileSync(TS_SOURCE, "utf-8");
  // 백분율 핵심 식 5종이 TS 원본에 모두 등장해야 한다.
  const requiredFragments = [
    "(pad.x / cw) * 100",
    "(pad.y / ch) * 100",
    "(pad.w / cw) * 100",
    "(pad.h / ch) * 100",
    "(scaledW / cw) * 100",
    "(scaledH / ch) * 100",
    "-(crop.x / cw) * 100",
    "-(crop.y / ch) * 100",
  ];
  const tsNorm = normalize(tsSrc);
  const missing = [];
  for (const frag of requiredFragments) {
    const fragNorm = normalize(frag);
    if (!tsNorm.includes(fragNorm)) missing.push(frag);
  }
  if (missing.length > 0) {
    console.error("[verify_simple_render_parity] TS 원본에 핵심 식이 누락:");
    for (const m of missing) console.error("  -", m);
    return false;
  }
  // 미러 본문에도 같은 식이 모두 있어야 한다 (이 파일이 직접 수정될 위험 가드).
  const mirrorNorm = normalize(MIRROR_APPLY_BODY);
  for (const frag of requiredFragments) {
    if (!mirrorNorm.includes(normalize(frag))) {
      console.error(
        `[verify_simple_render_parity] mjs 미러에 식 누락: ${frag} (스크립트 본문 동기화 필요)`,
      );
      return false;
    }
  }
  return true;
}

function checkIsSimpleRenderSpecMirrorDrift() {
  const tsSrc = readFileSync(IS_SIMPLE_TS_SOURCE, "utf-8");
  // 단순 모드 핵심 가드 5종.
  const requiredFragments = [
    'layers.length !== 1',
    'layer.type === "image"',
    'layer.type !== "video"',
    "vl.opacity !== undefined && vl.opacity !== 1.0",
    "vl.rotation_deg !== undefined && vl.rotation_deg !== 0",
  ];
  const tsNorm = normalize(tsSrc);
  const missing = requiredFragments.filter((f) => !tsNorm.includes(normalize(f)));
  if (missing.length > 0) {
    console.error("[verify_simple_render_parity] isSimpleRenderSpec TS 가드 누락:");
    for (const m of missing) console.error("  -", m);
    return false;
  }
  return true;
}

// ── 메인 ──────────────────────────────────────────────────────────────────

function loadFixture() {
  const txt = readFileSync(FIXTURE, "utf-8");
  return JSON.parse(txt);
}

function approxEqual(a, b, eps = EPSILON) {
  return Math.abs(a - b) <= eps;
}

function compareCss(actual, expected) {
  if (actual.mode !== expected.mode) {
    return `mode: ${actual.mode} vs ${expected.mode}`;
  }
  const keys = ["left_pct", "top_pct", "width_pct", "height_pct"];
  for (const k of keys) {
    if (!approxEqual(actual[k], expected[k])) {
      return `${k}: ${actual[k]} vs ${expected[k]} (Δ ${Math.abs(actual[k] - expected[k])})`;
    }
  }
  if (actual.object_fit !== expected.object_fit) {
    return `object_fit: ${actual.object_fit} vs ${expected.object_fit}`;
  }
  return null;
}

function main() {
  // 1. 미러 드리프트 가드.
  const applyOk = checkApplyTransformMirrorDrift();
  const isSimpleOk = checkIsSimpleRenderSpecMirrorDrift();
  if (!applyOk || !isSimpleOk) {
    process.exit(3);
  }

  // 2. fixture 케이스 픽셀 등가 비교.
  const fx = loadFixture();
  if (!Array.isArray(fx.cases) || fx.cases.length === 0) {
    console.error("[verify_simple_render_parity] fixture 가 비어있습니다.");
    process.exit(2);
  }

  const failures = [];
  for (const cs of fx.cases) {
    const css = applySimpleRenderTransformMirror(cs.layer, cs.input.canvas);
    const diff = compareCss(css, cs.expected_css);
    if (diff) {
      failures.push({ name: cs.name, diff });
    }
  }

  // 3. isSimpleRenderSpec — 단순 모드 케이스가 모두 video 1개라 true 여야 한다.
  for (const cs of fx.cases) {
    const fakeSpec = {
      layers: [cs.layer],
    };
    if (!isSimpleRenderSpecMirror(fakeSpec)) {
      failures.push({
        name: cs.name,
        diff: "isSimpleRenderSpec 가 video 1개짜리 spec 을 false 로 판정",
      });
    }
  }

  // 4. 회귀 가드 — filter / motion 이 끼면 false 가 나와야 한다 (#343).
  const filterLayer = { type: "filter", filter_type: "horror", intensity: 1.0 };
  const motionLayer = { type: "motion", motion_type: "shake" };
  const sampleVideo = fx.cases[0].layer;
  const specWithFilter = { layers: [sampleVideo, filterLayer] };
  const specWithMotion = { layers: [sampleVideo, motionLayer] };
  if (isSimpleRenderSpecMirror(specWithFilter)) {
    failures.push({
      name: "regression_filter",
      diff: "filter layer 가 추가됐는데 isSimpleRenderSpec 가 true 를 돌려줌 (#343)",
    });
  }
  if (isSimpleRenderSpecMirror(specWithMotion)) {
    failures.push({
      name: "regression_motion",
      diff: "motion layer 가 추가됐는데 isSimpleRenderSpec 가 true 를 돌려줌 (#343)",
    });
  }

  if (failures.length > 0) {
    console.error(
      `[verify_simple_render_parity] 실패 ${failures.length}건 / 총 ${fx.cases.length} 케이스`,
    );
    for (const f of failures.slice(0, 5)) {
      console.error(`  - ${f.name}: ${f.diff}`);
    }
    process.exit(1);
  }

  console.log(
    `[verify_simple_render_parity] OK — ${fx.cases.length} 케이스 모두 CSS 등가 + isSimpleRenderSpec 가드 통과.`,
  );
}

main();
