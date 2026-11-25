/**
 * JSON Patch → 사람이 읽기 쉬운 한국어 요약 변환 (#316).
 *
 * 백엔드 nl_editor 의 응답 `patch: JsonPatchOp[]` 를 김감독(=CLI 비숙련자)에게 직접
 * 보여줄 형식이 아니다. 본 모듈은 path / op / value 패턴을 한국어로 매핑한다.
 *
 * 설계 원칙
 * --------
 * - RenderSpec 의 1차 컨테이너인 `/scenes/{idx}/...` 만 *확실하게* 의역하고,
 *   알 수 없는 경로는 보수적으로 raw path 를 그대로 노출한다(거짓말보다 솔직함).
 * - "N번 씬" 표현은 1-based (사용자 인식). 내부 인덱스는 0-based 이므로 +1 한다.
 * - 값 표현은 단순 타입(number/string/bool)은 인라인, 객체/배열은 type/길이 요약만 노출.
 *
 * 영향 받는 씬 인덱스(0-based) 집합은 `extractAffectedSceneIndices()` 가 따로 돌려준다.
 * UI(`Editor`)가 zustand store 의 `highlightedSceneIds` 에 +1 해서 씬 카드 테두리 강조.
 */

import type { JsonPatchOp } from "../api/nlEdit";

// ── 경로 해석 ─────────────────────────────────────────────────────────────

interface ParsedPath {
  /** 0-based 씬 인덱스. `/scenes/{N}/...` 인 경우만 채워진다. */
  sceneIndex: number | null;
  /** `/scenes/{N}/` 다음에 오는 세그먼트(점/슬래시 정규화). */
  rest: string;
  /** 마지막 세그먼트(최종 필드 이름) — UI 텍스트용. */
  leaf: string;
}

function parsePath(rawPath: string): ParsedPath {
  // RFC 6901 escape 해제: ~1 → "/", ~0 → "~"
  const unescape = (s: string): string =>
    s.replace(/~1/g, "/").replace(/~0/g, "~");

  const segments = rawPath
    .split("/")
    .slice(1) // 선행 빈 토큰 제거
    .map(unescape);

  let sceneIndex: number | null = null;
  let rest = segments.slice();

  if (segments[0] === "scenes" && segments.length >= 2) {
    const n = Number(segments[1]);
    if (Number.isInteger(n) && n >= 0) {
      sceneIndex = n;
      rest = segments.slice(2);
    }
  }

  return {
    sceneIndex,
    rest: rest.join("/"),
    leaf: rest[rest.length - 1] ?? segments[segments.length - 1] ?? "",
  };
}

/**
 * 한국어 노출용 씬 라벨. sceneIndex 가 null 이면 빈 문자열.
 *
 * #334 — 과거에는 sceneIndex+1 로 1-based 정렬 위치를 표시했으나, 씬 카드 라벨이
 * "씬 {scene_id}" 형식(=비연속 가능)이므로 사용자가 보는 번호와 어긋났다. 이제는
 * 호출자가 정렬된 scene_id 배열을 함께 넘기면 그 매핑을 우선 사용한다.
 * sceneIds 가 None 이거나 인덱스가 범위를 벗어나면 폴백으로 +1 1-based 표시.
 */
function sceneLabel(sceneIndex: number | null, sceneIds?: number[]): string {
  if (sceneIndex == null) return "";
  if (sceneIds && sceneIndex >= 0 && sceneIndex < sceneIds.length) {
    return `${sceneIds[sceneIndex]}번 씬`;
  }
  return `${sceneIndex + 1}번 씬`;
}

// ── 필드명 한글화 ────────────────────────────────────────────────────────

const FIELD_LABELS: Record<string, string> = {
  text: "텍스트 내용",
  font_size: "글자 크기",
  font_color: "글자 색",
  outline_color: "테두리 색",
  outline_width: "테두리 두께",
  font_family: "글꼴",
  x: "X 위치",
  y: "Y 위치",
  width: "너비",
  height: "높이",
  fps: "프레임 속도(fps)",
  duration: "길이",
  duration_sec: "길이(초)",
  start_sec: "시작 시간",
  end_sec: "끝 시간",
  opacity: "투명도",
  rotation: "회전 각도",
  background: "배경",
  type: "유형",
  layers: "레이어",
  canvas: "캔버스",
  scenes: "씬",
  filter: "필터",
  motions: "모션",
  shape_overlays: "도형",
  graphic_overlays: "그래픽",
  text_overlays: "텍스트",
  mosaic_overlays: "모자이크",
  image_overlays: "이미지",
  footage_speed: "재생 속도",
  footage_speed_mode: "재생 속도 모드(정속/가속/감속)",
  footage_speed_ramp_hold: "가속 모드 정속 구간",
  footage_speed_ramp_accel: "가속 모드 가속 구간",
  footage_speed_ramp_base: "가속/감속 모드 기본 배속",
  narration_text: "나레이션 대본",
  subtitle_text: "자막",
};

function fieldLabel(name: string): string {
  if (!name) return "";
  return FIELD_LABELS[name] ?? `\`${name}\``;
}

// ── 값 포맷팅 ────────────────────────────────────────────────────────────

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "(없음)";
  if (typeof v === "string") {
    if (v.length === 0) return "(빈 문자열)";
    if (v.length > 40) return `"${v.slice(0, 37)}…"`;
    return `"${v}"`;
  }
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) {
    return `배열(${v.length}개)`;
  }
  if (typeof v === "object") {
    const obj = v as Record<string, unknown>;
    if (typeof obj.type === "string") return `${obj.type} 객체`;
    const keys = Object.keys(obj);
    return keys.length === 0 ? "빈 객체" : `객체(${keys.length}개 필드)`;
  }
  return String(v);
}

// ── op 별 변환 ───────────────────────────────────────────────────────────

/**
 * 단일 patch op 1개를 한 줄짜리 한국어 문장으로.
 *
 * 형식 가이드
 * - replace: "3번 씬의 글자 크기를 64로 변경" (또는 raw path 노출)
 * - add:     "3번 씬에 텍스트 1개 추가"
 * - remove:  "3번 씬에서 도형 1개 삭제"
 * - move/copy: from→path 양쪽 모두 노출 (드물게 사용)
 * - test:    "조건 확인: <path> 가 <value> 인지 검사" (보통 LLM 이 안 보냄)
 */
export function summarizeOp(op: JsonPatchOp, sceneIds?: number[]): string {
  const { sceneIndex, leaf, rest } = parsePath(op.path);
  const scene = sceneLabel(sceneIndex, sceneIds);
  const fieldName = fieldLabel(leaf);

  switch (op.op) {
    case "replace": {
      const target = scene
        ? `${scene}의 ${fieldName || rest}`
        : `${rest || op.path}`;
      return `${target}을(를) ${formatValue(op.value)}(으)로 변경`;
    }
    case "add": {
      const target = scene
        ? `${scene}에 ${fieldName || rest}`
        : `${rest || op.path}`;
      return `${target} 추가 — ${formatValue(op.value)}`;
    }
    case "remove": {
      const target = scene
        ? `${scene}에서 ${fieldName || rest}`
        : `${rest || op.path}`;
      return `${target} 삭제`;
    }
    case "move": {
      const fromParsed = op.from ? parsePath(op.from) : null;
      const fromScene = fromParsed ? sceneLabel(fromParsed.sceneIndex, sceneIds) : "";
      return `${fromScene || op.from || "(?)"}에서 ${scene || op.path}(으)로 이동`;
    }
    case "copy": {
      const fromParsed = op.from ? parsePath(op.from) : null;
      const fromScene = fromParsed ? sceneLabel(fromParsed.sceneIndex, sceneIds) : "";
      return `${fromScene || op.from || "(?)"}을(를) ${scene || op.path}(으)로 복사`;
    }
    case "test": {
      const target = scene ? `${scene}의 ${fieldName || rest}` : op.path;
      return `조건 확인: ${target} 이(가) ${formatValue(op.value)} 인지 검사`;
    }
    default:
      return `알 수 없는 변경: ${op.op} ${op.path}`;
  }
}

/** 전체 patch 배열을 줄바꿈 없이 1줄짜리 라인 배열로. UI 가 ul 로 렌더.
 *
 * @param sceneIds  정렬된 SceneCard.scene_id 배열. 호출자가 함께 넘기면 "N번 씬" 라벨이
 *                  사용자가 보는 씬 카드 번호(scene_id)로 표시된다(#334). 생략 시 1-based
 *                  정렬 위치로 폴백. */
export function summarizePatch(patch: JsonPatchOp[], sceneIds?: number[]): string[] {
  return patch.map((op) => summarizeOp(op, sceneIds));
}

// ── 영향 씬 추출 ─────────────────────────────────────────────────────────

/**
 * patch 가 건드리는 0-based 씬 인덱스 집합. UI 가 +1 해서 사용자 표시.
 *
 * `/scenes/*` 외 경로는 무시한다(전체 영향이라고 단정 짓지 않음). 그런 경우 호출자가
 * 별도로 "전체" 경고를 띄울 수 있다(safety_flags 가 더 정확한 신호).
 */
export function extractAffectedSceneIndices(patch: JsonPatchOp[]): number[] {
  const set = new Set<number>();
  for (const op of patch) {
    const { sceneIndex } = parsePath(op.path);
    if (sceneIndex != null) set.add(sceneIndex);
    if (op.from) {
      const f = parsePath(op.from);
      if (f.sceneIndex != null) set.add(f.sceneIndex);
    }
  }
  return Array.from(set).sort((a, b) => a - b);
}

// ── safety flag 사용자 메시지 ────────────────────────────────────────────

/**
 * 백엔드 safety flag → 사용자 확인 다이얼로그용 한 줄 경고 문구.
 *
 * 미지의 플래그는 보수적으로 "주의가 필요한 변경입니다"로 폴백한다 — UI 는
 * 적어도 한 번은 사용자에게 명시적 확인을 받게 된다.
 */
export function safetyFlagToWarning(flag: string): string {
  switch (flag) {
    case "delete_all_layers":
      return "씬의 모든 레이어를 삭제합니다.";
    case "delete_all_scenes":
      return "전체 씬을 삭제합니다.";
    case "canvas_size_change":
      return "캔버스 크기/해상도/프레임 속도를 변경합니다.";
    case "bulk_layer_delete":
      return "여러 레이어를 한 번에 삭제합니다.";
    case "unknown_top_level_path":
      return "/scenes 외부의 알 수 없는 영역을 건드립니다.";
    default:
      return "주의가 필요한 변경입니다.";
  }
}
