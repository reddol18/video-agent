// ── 이슈 #311 — 키워드(파일명) 매칭 클라이언트 포팅 ───────────────────────
//
// 백엔드 `_match_keyword`(`web/backend/routers/matching.py:123`)와 **알고리즘 1:1 동등**한
// TypeScript 구현. Gemini를 호출하지 않는 순수 문자열 토큰화 + 스코어링이라 백엔드 CPU만
// 잡아먹는 작업이므로 클라이언트에서 실행해 비용을 절감한다.
//
// 회귀 방지 — 토큰화/스코어링 알고리즘은 백엔드와 **완전히 동일**해야 한다.
//
// 백엔드 원본 알고리즘:
//   1. text = f"{scene.title} {scene.description} {' '.join(scene.keywords)}"
//   2. 각 chunk에 대해 source_file의 Path.stem을 추출, 같은 stem은 첫 번째만 사용 (중복 제거)
//   3. score = rapidfuzz.fuzz.token_set_ratio(text, stem.replace('_',' ').replace('-',' '))
//   4. 가장 점수가 높은 chunk 채택
//   5. chunk가 없으면 footage_dir 내 파일을 직접 스캔하여 동일하게 처리
//   6. 이미지 확장자 → KenburnsEffect, 영상 확장자 → ClipEffect
//
// rapidfuzz.fuzz.token_set_ratio (processor=None) 알고리즘:
//   • a.split() / b.split() (Python: 공백/탭/개행 연속 분리, 빈 토큰 제거)
//   • intersection = sorted(tok1 & tok2), diff1 = sorted(tok1 - tok2), diff2 = sorted(tok2 - tok1)
//   • t0 = " ".join(intersection)
//   • t1 = (t0 + " " + " ".join(diff1)).strip()
//   • t2 = (t0 + " " + " ".join(diff2)).strip()
//   • return max(indel_ratio(t0,t1), indel_ratio(t0,t2), indel_ratio(t1,t2)) * 100
//
// Indel.normalized_similarity 공식: 2 * LCS(s1,s2) / (len(s1) + len(s2))
//   - 빈 문자열 양쪽: 1.0
//   - 길이 비교는 코드포인트 단위 (한글 BMP는 UTF-16 단일 단위 → JS String iterator와 일치)

import type { SceneCard, FootageChunk, FootageItem } from "../types";

// ── 토큰화 ─────────────────────────────────────────────────────────────────
// Python `str.split()` (no args)와 동일 의미: 임의 공백 문자열 분리 + 빈 토큰 제거.
// 공백 문자 집합: \s = [ \t\n\r\f\v] (JS 정규식과 Python str.split 모두 동일 범주).
export function tokenize(text: string): string[] {
  if (!text) return [];
  return text.split(/\s+/).filter((t) => t.length > 0);
}

// ── 코드포인트 단위 길이/시퀀스 ─────────────────────────────────────────────
// JS의 length는 UTF-16 코드유닛이라 surrogate pair(이모지 등)에서 Python과 어긋난다.
// 한국어 일반 문자(가-힣)는 BMP라 영향 없지만 정확성 위해 코드포인트 단위로 처리한다.
function toCodePoints(s: string): string[] {
  return Array.from(s);
}

// ── Indel(LCS 기반) 정규화 유사도 ───────────────────────────────────────────
// rapidfuzz.distance.Indel.normalized_similarity 와 동일 결과:
//   sim = 2 * LCS / (len(s1) + len(s2))
//   두 문자열 모두 빈 경우 1.0
function indelNormalizedSimilarity(a: string, b: string): number {
  const s1 = toCodePoints(a);
  const s2 = toCodePoints(b);
  const n = s1.length;
  const m = s2.length;
  if (n === 0 && m === 0) return 1.0;
  if (n === 0 || m === 0) return 0.0;

  // LCS — 메모리 절약 위해 1D DP (이전 행만 보관).
  // s1[i] vs s2[j]: 같으면 prev[j] + 1, 다르면 max(prev[j+1], cur[j])
  let prev = new Int32Array(m + 1);
  let cur = new Int32Array(m + 1);
  for (let i = 0; i < n; i++) {
    cur[0] = 0;
    const ci = s1[i];
    for (let j = 0; j < m; j++) {
      if (ci === s2[j]) {
        cur[j + 1] = prev[j] + 1;
      } else {
        cur[j + 1] = Math.max(prev[j + 1], cur[j]);
      }
    }
    // swap
    const tmp = prev;
    prev = cur;
    cur = tmp;
  }
  const lcs = prev[m];
  return (2 * lcs) / (n + m);
}

// ── rapidfuzz.fuzz.token_set_ratio (processor=None) ──────────────────────────
export function tokenSetRatio(a: string, b: string): number {
  // rapidfuzz는 어느 한쪽이라도 빈 문자열(또는 공백만 있는 문자열)이면 0.0 을 반환.
  // 토큰 분리 후 비어있는 경우 동일하게 0 처리 — 그렇지 않으면 t0=t1='' → indel 1.0이 되어
  // 백엔드와 어긋난다 (회귀 케이스, 이슈 #311 검증 fixture에서 확인).
  const tokens1 = tokenize(a);
  const tokens2 = tokenize(b);
  if (tokens1.length === 0 || tokens2.length === 0) return 0.0;

  const tok1 = new Set(tokens1);
  const tok2 = new Set(tokens2);

  // 교집합/차집합 (Python sorted = 코드포인트 순; JS String.localeCompare가 아닌
  // 기본 비교 사용 — `<`/`>` 연산자는 UTF-16 코드유닛 순. BMP 문자는 코드포인트 순과 동일).
  const inter: string[] = [];
  const diff1: string[] = [];
  for (const t of tok1) {
    if (tok2.has(t)) inter.push(t);
    else diff1.push(t);
  }
  const diff2: string[] = [];
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

// ── 파일명 stem 추출 ─────────────────────────────────────────────────────────
// Python `Path(p).stem`: 마지막 path-separator 이후의 basename에서 마지막 확장자 제거.
// 백엔드에선 source_file이 절대 경로일 수 있으므로 basename으로 정규화한 뒤 stem 추출.
function pathStem(srcFile: string): string {
  if (!srcFile) return "";
  // 마지막 / 또는 \ 이후가 basename.
  const lastSep = Math.max(srcFile.lastIndexOf("/"), srcFile.lastIndexOf("\\"));
  const basename = lastSep >= 0 ? srcFile.slice(lastSep + 1) : srcFile;
  // 마지막 . 이후가 확장자 (단, leading dot는 stem으로 간주 — Path.stem 동작).
  const dot = basename.lastIndexOf(".");
  if (dot <= 0) return basename;
  return basename.slice(0, dot);
}

// 백엔드와 정확히 일치하는 확장자 집합.
const VIDEO_EXTS = new Set([".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"]);
const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp"]);

function pathExt(srcFile: string): string {
  const lastSep = Math.max(srcFile.lastIndexOf("/"), srcFile.lastIndexOf("\\"));
  const basename = lastSep >= 0 ? srcFile.slice(lastSep + 1) : srcFile;
  const dot = basename.lastIndexOf(".");
  if (dot <= 0) return "";
  return basename.slice(dot).toLowerCase();
}

function pathBasename(srcFile: string): string {
  const lastSep = Math.max(srcFile.lastIndexOf("/"), srcFile.lastIndexOf("\\"));
  return lastSep >= 0 ? srcFile.slice(lastSep + 1) : srcFile;
}

// ── 매칭 알고리즘 입력/출력 ─────────────────────────────────────────────────
// 백엔드는 footage_index.json의 chunk 리스트와 footage_dir 파일 스캔 두 가지 소스를 사용한다.
// 클라이언트는 그 정보를 GET /api/projects/{id}/footage 한 번으로 모두 얻을 수 있다
// (FootageItem.filename + FootageItem.chunks).

export interface KeywordMatchSource {
  // 백엔드 footage_index.json의 chunks와 호환되는 형태.
  // source_file: 백엔드와 동일한 절대 경로 또는 적어도 파일명을 포함하는 문자열.
  source_file: string;
  chunk_id: string;
  start_sec: number;
  end_sec: number;
}

export interface KeywordMatchResult {
  // 백엔드의 result["footage"] / result["match"]에 매핑되는 정보.
  source_file: string;   // 절대 경로 또는 footage 폴더 상대 경로 (저장은 백엔드에서 정규화)
  filename: string;      // basename (백엔드에서 footage 엔티티 생성 시 사용)
  chunk_id: string;
  trim_start: number;
  trim_end: number;
  match_score: number;
  match_reason: "keyword_filename";
  is_image: boolean;     // true면 KenburnsEffect, false면 ClipEffect로 저장
}

/**
 * 백엔드 `_match_keyword`와 동등한 단일 씬 매칭.
 *
 * @param scene 매칭 대상 씬 카드 (title/description/keywords 사용)
 * @param indexChunks footage_index.json의 chunks (스템 중복 제거 후 우선 사용)
 * @param fallbackFiles footage_dir 내 파일 정보 (인덱스에 없는 파일 fallback 처리)
 * @returns 매칭 결과 또는 null (후보 없음)
 */
export function matchKeyword(
  scene: { title: string; description: string; keywords: string[] },
  indexChunks: KeywordMatchSource[],
  fallbackFiles: { source_file: string; filename: string }[] = []
): KeywordMatchResult | null {
  const text = `${scene.title} ${scene.description} ${scene.keywords.join(" ")}`;

  let bestScore = -1;
  let bestChunk: KeywordMatchSource | null = null;

  // 1) footage_index.json chunks — 동일 stem은 첫 번째만 사용 (백엔드와 동일 순서/dedup).
  const seenStems = new Set<string>();
  for (const chunk of indexChunks) {
    const stem = pathStem(chunk.source_file);
    if (seenStems.has(stem)) continue;
    seenStems.add(stem);
    const target = stem.replace(/_/g, " ").replace(/-/g, " ");
    const score = tokenSetRatio(text, target);
    if (score > bestScore) {
      bestScore = score;
      bestChunk = chunk;
    }
  }

  // 2) fallback — 백엔드는 best_chunk가 None일 때만 폴더 스캔.
  //    실제로는 chunks가 비어있을 때만 동작. 동일 동작 보장.
  if (bestChunk === null) {
    for (const f of fallbackFiles) {
      const ext = pathExt(f.source_file);
      if (!VIDEO_EXTS.has(ext) && !IMAGE_EXTS.has(ext)) continue;
      const stem = pathStem(f.source_file);
      // 백엔드는 fallback에서 `_`만 ' '로 치환 (`-`은 그대로). 백엔드 코드:
      //   score = fuzz.token_set_ratio(text, f.stem.replace("_", " "))
      const target = stem.replace(/_/g, " ");
      const score = tokenSetRatio(text, target);
      if (score > bestScore) {
        bestScore = score;
        bestChunk = {
          source_file: f.source_file,
          chunk_id: stem,
          start_sec: 0.0,
          end_sec: 0.0,
        };
      }
    }
  }

  if (bestChunk === null) return null;

  const ext = pathExt(bestChunk.source_file);
  const isImage = IMAGE_EXTS.has(ext);

  return {
    source_file: bestChunk.source_file,
    filename: pathBasename(bestChunk.source_file),
    chunk_id: bestChunk.chunk_id ?? "",
    trim_start: bestChunk.start_sec ?? 0.0,
    trim_end: bestChunk.end_sec ?? 0.0,
    match_score: bestScore,
    match_reason: "keyword_filename",
    is_image: isImage,
  };
}

// ── FootageItem → 매칭 소스 변환 헬퍼 ──────────────────────────────────────
// GET /api/projects/{id}/footage 응답을 백엔드 _match_keyword 입력으로 변환한다.
// FootageItem.chunks는 인덱싱된 청크 목록 (없을 수 있음).
// FootageItem.path는 백엔드의 footage source_file 절대 경로와 동일.
export function footageItemsToMatchSources(
  items: FootageItem[]
): { chunks: KeywordMatchSource[]; fallbackFiles: { source_file: string; filename: string }[] } {
  const chunks: KeywordMatchSource[] = [];
  const fallbackFiles: { source_file: string; filename: string }[] = [];
  for (const it of items) {
    // audio는 매칭 대상에서 제외 (백엔드도 video/image만 처리).
    if (it.file_type === "audio") continue;
    if (it.chunks && it.chunks.length > 0) {
      for (const c of it.chunks as FootageChunk[]) {
        chunks.push({
          source_file: it.path,
          chunk_id: c.chunk_id,
          start_sec: c.start_sec,
          end_sec: c.end_sec,
        });
      }
    }
    // fallback 후보에는 항상 추가 (백엔드는 chunks가 비어있을 때만 사용하지만
    // 클라에서도 동일 분기 — matchKeyword 내부에서 bestChunk가 null일 때만 fallback 실행).
    fallbackFiles.push({
      source_file: it.path,
      filename: it.filename,
    });
  }
  return { chunks, fallbackFiles };
}

// ── 다중 씬 일괄 매칭 ──────────────────────────────────────────────────────
// progressCb는 매 씬 처리 후 호출 (UI 진행률 표시용).
export interface SceneKeywordMatchResult {
  scene_id: number;
  result: KeywordMatchResult | null;
}

export function matchKeywordBatch(
  scenes: SceneCard[],
  items: FootageItem[],
  progressCb?: (done: number, total: number, sceneId: number) => void
): SceneKeywordMatchResult[] {
  const { chunks, fallbackFiles } = footageItemsToMatchSources(items);
  const results: SceneKeywordMatchResult[] = [];
  for (let i = 0; i < scenes.length; i++) {
    const s = scenes[i];
    const r = matchKeyword(
      {
        title: s.title ?? "",
        description: s.description ?? "",
        keywords: s.keywords ?? [],
      },
      chunks,
      fallbackFiles
    );
    results.push({ scene_id: s.scene_id, result: r });
    progressCb?.(i + 1, scenes.length, s.scene_id);
  }
  return results;
}

// ── 통합 헬퍼: 클라에서 매칭 → 결과만 백엔드 저장 ──────────────────────────
// 사용 예:
//   const items = await getFootage(projectId);
//   const r = await runKeywordMatchClient(projectId, scenes, items);
//
// 백엔드의 POST /api/projects/{id}/match (method=keyword_filename) 대비:
//   - Gemini 호출 없음 → 비용 동일 (0원)
//   - 백엔드 CPU 절감 (토큰화/스코어링이 클라에서 실행)
//   - 결과 저장만 백엔드 호출 → 네트워크 비용 미미
export async function runKeywordMatchClient(
  projectId: string,
  scenes: SceneCard[],
  items: FootageItem[],
  progressCb?: (done: number, total: number, sceneId: number) => void
): Promise<{
  computed_results: SceneKeywordMatchResult[];
  updated_scene_ids: number[];
  failed_scene_ids: number[];
}> {
  const computed = matchKeywordBatch(scenes, items, progressCb);

  // 동적 import — 순환 의존 방지 (api/client.ts가 types를 import하므로).
  const { saveKeywordMatchResult } = await import("../api/client");

  const payload = computed
    .filter((c): c is { scene_id: number; result: KeywordMatchResult } => c.result !== null)
    .map((c) => ({
      scene_id: c.scene_id,
      source_file: c.result.source_file,
      filename: c.result.filename,
      chunk_id: c.result.chunk_id,
      trim_start: c.result.trim_start,
      trim_end: c.result.trim_end,
      match_score: c.result.match_score,
      is_image: c.result.is_image,
    }));

  if (payload.length === 0) {
    return {
      computed_results: computed,
      updated_scene_ids: [],
      failed_scene_ids: computed.map((c) => c.scene_id),
    };
  }

  const saved = await saveKeywordMatchResult(projectId, payload);
  return {
    computed_results: computed,
    updated_scene_ids: saved.updated_scene_ids,
    failed_scene_ids: saved.failed_scene_ids,
  };
}
