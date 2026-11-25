#!/usr/bin/env node
/**
 * 키워드 매칭 알고리즘 동등성 검증 — 이슈 #311 + #328.
 *
 * 백엔드(rapidfuzz)가 생성한 fixture(`tests/fixtures/keyword_matching_cases.json`)와
 * 프론트 TS 구현(`web/frontend/src/lib/clientKeywordMatching.ts`)의 점수를 직접 비교한다.
 *
 * 실행:
 *   1) (선택) `uv run python -m pytest tests/test_keyword_matching_parity.py` 로 fixture 갱신
 *   2) `node scripts/verify_keyword_matching_parity.mjs`
 *
 * 결과:
 *   - 모든 케이스 점수가 허용 오차(1e-6) 이내면 exit 0
 *   - 한 건이라도 어긋나면 어긋난 케이스를 출력하고 exit 1
 *
 * 이 스크립트는 TS 컴파일러나 vitest 없이 Node만으로 실행 가능하다. TS 파일에서
 * 알고리즘 함수의 본문을 그대로 복제(미러)하여 검증한다.
 *
 * #328 — 미러 동기화 가드:
 *   본문 복제 방식은 누군가 TS 원본만 수정하고 미러를 빠뜨릴 위험이 있다. 이를 막기 위해
 *   verifyMirrorDrift()가 `clientKeywordMatching.ts`에서 핵심 함수(tokenize, tokenSetRatio,
 *   indelNormalizedSimilarity, pathStem, matchKeyword) 본문을 정규식으로 추출하여
 *   이 스크립트의 미러 본문과 정규화 비교한다. 차이가 발견되면 즉시 exit 3 (미러 drift)로
 *   실패한다. 결과적으로 매 PR마다 CI(test.yml)가 강제로 detection.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const REPO_ROOT = resolve(__dirname, "..");
const FIXTURE = resolve(REPO_ROOT, "tests/fixtures/keyword_matching_cases.json");
const SCENARIO_FIXTURE = resolve(REPO_ROOT, "tests/fixtures/keyword_matching_scenarios.json");
const TS_SOURCE = resolve(REPO_ROOT, "web/frontend/src/lib/clientKeywordMatching.ts");

// ── 알고리즘 미러: clientKeywordMatching.ts 의 tokenize/tokenSetRatio와 동일 ──

function tokenize(text) {
  if (!text) return [];
  return text.split(/\s+/).filter((t) => t.length > 0);
}

function toCodePoints(s) {
  return Array.from(s);
}

function indelNormalizedSimilarity(a, b) {
  const s1 = toCodePoints(a);
  const s2 = toCodePoints(b);
  const n = s1.length;
  const m = s2.length;
  if (n === 0 && m === 0) return 1.0;
  if (n === 0 || m === 0) return 0.0;
  let prev = new Int32Array(m + 1);
  let cur = new Int32Array(m + 1);
  for (let i = 0; i < n; i++) {
    cur[0] = 0;
    const ci = s1[i];
    for (let j = 0; j < m; j++) {
      if (ci === s2[j]) cur[j + 1] = prev[j] + 1;
      else cur[j + 1] = Math.max(prev[j + 1], cur[j]);
    }
    const tmp = prev;
    prev = cur;
    cur = tmp;
  }
  const lcs = prev[m];
  return (2 * lcs) / (n + m);
}

function tokenSetRatio(a, b) {
  const tokens1 = tokenize(a);
  const tokens2 = tokenize(b);
  if (tokens1.length === 0 || tokens2.length === 0) return 0.0;
  const tok1 = new Set(tokens1);
  const tok2 = new Set(tokens2);
  const inter = [];
  const diff1 = [];
  for (const t of tok1) {
    if (tok2.has(t)) inter.push(t);
    else diff1.push(t);
  }
  const diff2 = [];
  for (const t of tok2) {
    if (!tok1.has(t)) diff2.push(t);
  }
  inter.sort();
  diff1.sort();
  diff2.sort();
  const t0 = inter.join(" ");
  const t1 = (t0 + " " + diff1.join(" ")).replace(/^\s+|\s+$/g, "");
  const t2 = (t0 + " " + diff2.join(" ")).replace(/^\s+|\s+$/g, "");
  const r1 = indelNormalizedSimilarity(t0, t1);
  const r2 = indelNormalizedSimilarity(t0, t2);
  const r3 = indelNormalizedSimilarity(t1, t2);
  return Math.max(r1, r2, r3) * 100;
}

// ── 시나리오 매칭 미러 (clientKeywordMatching.matchKeyword와 동일 명세) ──

function pathStem(srcFile) {
  if (!srcFile) return "";
  const lastSep = Math.max(srcFile.lastIndexOf("/"), srcFile.lastIndexOf("\\"));
  const basename = lastSep >= 0 ? srcFile.slice(lastSep + 1) : srcFile;
  const dot = basename.lastIndexOf(".");
  if (dot <= 0) return basename;
  return basename.slice(0, dot);
}

function matchKeywordCore(scene, chunks) {
  const text = `${scene.title} ${scene.description} ${scene.keywords.join(" ")}`;
  let bestScore = -1;
  let bestChunk = null;
  const seen = new Set();
  for (const c of chunks) {
    const stem = pathStem(c.source_file);
    if (seen.has(stem)) continue;
    seen.add(stem);
    const target = stem.replace(/_/g, " ").replace(/-/g, " ");
    const score = tokenSetRatio(text, target);
    if (score > bestScore) {
      bestScore = score;
      bestChunk = c;
    }
  }
  return { bestChunk, bestScore };
}

// ── #328 — 미러 동기화 가드 ────────────────────────────────────────────────
// TS 원본 `clientKeywordMatching.ts`에서 핵심 함수 본문을 추출해 이 스크립트의
// 미러 본문과 정규화 비교한다. 비교 단위는 "함수 본문의 토큰 시퀀스"이므로 공백/주석/
// 줄바꿈 차이는 무시되고, 실질적 알고리즘 변경만 감지한다.
//
// 추출 대상 함수: tokenize, indelNormalizedSimilarity, tokenSetRatio, pathStem,
//                matchKeyword(core 부분 — chunks 처리 루프).

function readTsSource() {
  try {
    return readFileSync(TS_SOURCE, "utf8");
  } catch (e) {
    console.error(`[verify_keyword_matching_parity] TS 원본을 읽지 못했습니다: ${TS_SOURCE}`);
    console.error(e?.message ?? String(e));
    process.exit(2);
  }
}

// 함수 본문에서 주석/공백/TS 타입 어노테이션/단일 statement 중괄호 차이를
// 정규화해 비교한다. 알고리즘 동등성과 무관한 표기 차이는 무시.
function normalizeFnBody(src) {
  let s = src
    // 줄단위 주석 제거
    .replace(/\/\/[^\n]*/g, "")
    // 블록 주석 제거 (비탐욕)
    .replace(/\/\*[\s\S]*?\*\//g, "");
  // TS 타입 어노테이션 제거 — `: Type` 형태(타입 끝 토큰: `=`, `;`, `,`, `)`, `{`, `}` 직전).
  // 예: `const inter: string[] = []` → `const inter = []`
  //     `function foo(a: string): number`는 본문 추출 단계에서 시그니처가 제외되므로 본문엔 거의 없음.
  s = s.replace(/:\s*[A-Za-z_$][A-Za-z0-9_$<>[\]\s,|]*(?=\s*[=;),{}\n])/g, "");
  // 단일 statement 중괄호 제거 — `if(x){y;}` 와 `if(x)y;`를 동일하게 본다.
  // 단순화: 모든 `{` `}` 를 정규화 단계에서 제거 (본문 단위 비교라 블록 구조 정보는 무시해도
  // 알고리즘 동등성에 영향 없음 — 동일 statement 시퀀스를 비교하므로).
  s = s.replace(/[{}]/g, "");
  // 모든 공백 압축.
  s = s.replace(/\s+/g, "");
  return s;
}

// 함수 시그니처(`function name(...) {` 또는 `export function name(...) {`)부터
// 같은 깊이의 닫는 `}`까지 본문 추출. 단순 brace 카운터 — 알고리즘 함수에는
// 문자열 리터럴 내부 `}`가 없으므로 안전.
function extractFunctionBody(src, fnName) {
  const re = new RegExp(`function\\s+${fnName}\\s*\\([^)]*\\)\\s*(?::[^{]+)?\\{`);
  const m = src.match(re);
  if (!m) return null;
  let depth = 1;
  let i = m.index + m[0].length;
  const start = i;
  while (i < src.length && depth > 0) {
    const ch = src[i];
    if (ch === "{") depth++;
    else if (ch === "}") depth--;
    i++;
  }
  if (depth !== 0) return null;
  return src.slice(start, i - 1);
}

// 미러 본문 — 이 스크립트에 정의된 함수 본문을 캡처해 비교 대상으로 사용한다.
// (스크립트 자체의 함수 정의를 직접 읽어 같은 정규화 후 비교)
function selfFnBody(fnName) {
  const selfSrc = readFileSync(__filename, "utf8");
  return extractFunctionBody(selfSrc, fnName);
}

function verifyMirrorDrift() {
  const tsSrc = readTsSource();
  const targets = [
    "tokenize",
    "indelNormalizedSimilarity",
    "tokenSetRatio",
    "pathStem",
  ];
  const drift = [];
  for (const name of targets) {
    const tsBody = extractFunctionBody(tsSrc, name);
    const mirrorBody = selfFnBody(name);
    if (tsBody === null) {
      drift.push({ name, reason: "TS 원본에서 함수를 찾을 수 없음" });
      continue;
    }
    if (mirrorBody === null) {
      drift.push({ name, reason: "이 스크립트의 미러에서 함수를 찾을 수 없음" });
      continue;
    }
    if (normalizeFnBody(tsBody) !== normalizeFnBody(mirrorBody)) {
      drift.push({
        name,
        reason: "TS 원본과 미러 본문이 불일치 — 알고리즘 변경 의심",
      });
    }
  }
  // matchKeyword의 chunks 처리 루프 — 핵심 시나리오 매칭 로직. TS에는 matchKeyword
  // 한 함수에 chunks 루프와 fallback 루프가 함께 있고 이 스크립트는 chunks 루프만
  // matchKeywordCore에 미러했다. 변경 감지는 첫 루프 텍스트의 정규화 비교로 한다.
  const tsMatch = extractFunctionBody(tsSrc, "matchKeyword");
  if (tsMatch !== null) {
    // chunks 루프 시그니처(`for (const chunk of indexChunks)`)부터 fallback 루프 직전까지.
    const startIdx = tsMatch.indexOf("for (const chunk of indexChunks)");
    const endIdx = tsMatch.indexOf("if (bestChunk === null)");
    if (startIdx >= 0 && endIdx > startIdx) {
      const tsChunkLoop = tsMatch.slice(startIdx, endIdx);
      const mirrorChunkLoop = (selfFnBody("matchKeywordCore") || "").match(
        /for\s*\(const c of chunks\)[\s\S]*?(?=return)/
      );
      if (!mirrorChunkLoop) {
        drift.push({
          name: "matchKeyword(chunks loop)",
          reason: "미러에서 chunks 루프를 찾을 수 없음",
        });
      } else {
        // 변수명이 다르므로(chunk vs c, indexChunks vs chunks, seenStems vs seen)
        // 정규화 **전**에 TS 본문에서 변수명을 치환한 뒤 비교한다.
        // 정규화 후엔 단어 경계가 사라져 `\bchunk\b`가 매치되지 않으므로 순서가 중요하다.
        const tsRenamed = tsChunkLoop
          .replace(/\bchunk\b/g, "c")
          .replace(/\bindexChunks\b/g, "chunks")
          .replace(/\bseenStems\b/g, "seen");
        const normTs = normalizeFnBody(tsRenamed);
        const normMirror = normalizeFnBody(mirrorChunkLoop[0]);
        if (normTs !== normMirror) {
          drift.push({
            name: "matchKeyword(chunks loop)",
            reason: "TS 원본 chunks 루프와 미러가 불일치 — 매칭 로직 변경 의심",
          });
        }
      }
    }
  }
  if (drift.length > 0) {
    console.error(
      `[verify_keyword_matching_parity] 미러 동기화 실패 — TS 원본과 스크립트 미러 본문이 어긋났습니다 (#328):`
    );
    for (const d of drift) {
      console.error(`  - ${d.name}: ${d.reason}`);
    }
    console.error(
      "TS 원본을 수정했다면 scripts/verify_keyword_matching_parity.mjs의 미러 본문도 함께 갱신하세요."
    );
    process.exit(3);
  }
}

verifyMirrorDrift();

// ── fixture 로드 + 비교 ─────────────────────────────────────────────────────

const TOL = 1e-6;

let fixture;
try {
  fixture = JSON.parse(readFileSync(FIXTURE, "utf8"));
} catch (e) {
  console.error(`[verify_keyword_matching_parity] fixture 로드 실패: ${FIXTURE}`);
  console.error(`먼저 'uv run python -m pytest tests/test_keyword_matching_parity.py' 실행 후 다시 시도하세요.`);
  console.error(e?.message ?? String(e));
  process.exit(2);
}

if (!fixture?.cases || !Array.isArray(fixture.cases)) {
  console.error("[verify_keyword_matching_parity] fixture 형식 오류: cases 배열 누락");
  process.exit(2);
}

let failed = 0;
const failures = [];
for (const c of fixture.cases) {
  const got = tokenSetRatio(c.text, c.target);
  const want = c.expected_score;
  const diff = Math.abs(got - want);
  if (diff > TOL) {
    failed++;
    failures.push({ kind: "token_set_ratio", text: c.text, target: c.target, want, got, diff });
  }
}

// ── 시나리오 검증 (matchKeyword 동등성) ─────────────────────────────────────

let scenarios = null;
try {
  scenarios = JSON.parse(readFileSync(SCENARIO_FIXTURE, "utf8"));
} catch {
  // 선택적 fixture — 없으면 건너뛴다.
}

let scenarioOk = 0;
if (scenarios?.scenarios) {
  for (const sc of scenarios.scenarios) {
    const { bestChunk, bestScore } = matchKeywordCore(sc.scene, sc.chunks);
    const want = sc.expected;
    if (
      !bestChunk ||
      bestChunk.chunk_id !== want.chunk_id ||
      bestChunk.source_file !== want.source_file ||
      Math.abs(bestScore - want.match_score) > TOL
    ) {
      failed++;
      failures.push({
        kind: "scenario",
        name: sc.name,
        want,
        got: {
          chunk_id: bestChunk?.chunk_id,
          source_file: bestChunk?.source_file,
          match_score: bestScore,
        },
      });
    } else {
      scenarioOk++;
    }
  }
}

if (failed === 0) {
  let msg = `[verify_keyword_matching_parity] ${fixture.cases.length} 케이스 모두 일치 (백엔드 fuzz.token_set_ratio == TS tokenSetRatio)`;
  if (scenarios) msg += ` + 시나리오 ${scenarioOk}건 일치 (백엔드 _match_keyword 선택 == TS matchKeyword 선택)`;
  console.log(msg);
  process.exit(0);
} else {
  console.error(`[verify_keyword_matching_parity] ${failed}건 불일치:`);
  for (const f of failures) {
    if (f.kind === "token_set_ratio") {
      console.error(
        `  [tokenSetRatio] text=${JSON.stringify(f.text)} target=${JSON.stringify(f.target)} ` +
          `want=${f.want.toFixed(6)} got=${f.got.toFixed(6)} diff=${f.diff.toExponential(3)}`
      );
    } else {
      console.error(
        `  [scenario:${f.name}] want=${JSON.stringify(f.want)} got=${JSON.stringify(f.got)}`
      );
    }
  }
  process.exit(1);
}
