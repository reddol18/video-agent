/**
 * 자연어 RenderSpec 편집 API 클라이언트 (#316)
 *
 * 백엔드 라우터: `web/backend/routers/nl_editor.py`
 * 모델: `src/nl_editor/models.py`
 *
 * 엔드포인트
 * ---------
 * - `POST /api/projects/{id}/nl-edit`        — 자연어 → patch 미리보기 (적용 안 함)
 * - `POST /api/projects/{id}/nl-edit/apply`  — 미리보기 patch 를 실제 RenderSpec 에 적용
 * - `POST /api/projects/{id}/nl-edit/undo`   — 직전 snapshot 으로 롤백
 *
 * 본 모듈은 Pydantic 모델의 핵심 필드를 TS 인터페이스로 미러링한다.
 * `web/frontend/src/types/generated.ts` 가 자동 생성될 때까지의 *수동* 미러.
 * 필드 추가 시 양쪽을 동기화한다.
 */
import { api } from "./client";

// ── RFC 6902 JSON Patch ──────────────────────────────────────────────────

export type JsonPatchOpType =
  | "add"
  | "remove"
  | "replace"
  | "move"
  | "copy"
  | "test";

/**
 * 단일 JSON Patch 연산.
 * `from` 은 JS 예약어가 아니지만 Pydantic alias 와 1:1 호환되도록 그대로 사용한다.
 * (백엔드가 `populate_by_name=True` 로 alias 키만 받기 때문)
 */
export interface JsonPatchOp {
  op: JsonPatchOpType;
  path: string;
  value?: unknown;
  from?: string;
}

// ── 대화 컨텍스트 ────────────────────────────────────────────────────────

export interface NlEditTurn {
  instruction: string;
  summary?: string;
  applied?: boolean;
}

// ── 응답 ─────────────────────────────────────────────────────────────────

/** 백엔드 `SafetyFlag` Literal 과 1:1. UI 에서 분기에 사용. */
export type SafetyFlag =
  | "delete_all_layers"
  | "delete_all_scenes"
  | "canvas_size_change"
  | "bulk_layer_delete"
  | "unknown_top_level_path";

export interface SplitDetectResult {
  scene_id: number;
  cuts_sec: number[];
  reasoning: string;
  scene_duration_sec?: number | null;
}

export interface OverlayItem {
  id: string;
  text: string;
  x: number;
  y: number;
  font_family: string;
  color: string;
  start_sec: number;
  end_sec: number;
  sfx_asset_id: string | null;
  sfx_volume: number;
}

export interface OverlayDetectResult {
  scene_id: number;
  overlays: OverlayItem[];
  reasoning: string;
}

export interface NlEditPreviewResponse {
  patch: JsonPatchOp[];
  summary: string;
  safety_flags: SafetyFlag[];
  estimated_cost_usd: number;
  notes: string;
  // 응답 종류: 일반 patch / 자동 씬 분할 / 자동 텍스트+SFX 오버레이
  kind?: "patch" | "split" | "overlay";
  split?: SplitDetectResult | null;
  overlay?: OverlayDetectResult | null;
}

export interface NlEditApplyResponse {
  scenes: Record<string, unknown>[];
  /** #335 — RenderSpec → SceneCard 역동기화 결과. 프론트는 이 값으로 IndexedDB
   *  Project.scenes 와 zustand store 를 갱신해야 후속 액션에서 NL 편집 변경 사항이
   *  유실되지 않는다. 변환 불가 필드(filters/motions/narration_text 등)는 기존 값을
   *  그대로 유지하므로 *덮어쓰기 가능 안전*. */
  scene_cards: Record<string, unknown>[];
  snapshot_id: string;
  applied_at: string;
}

export interface NlEditUndoResponse {
  scenes: Record<string, unknown>[];
  /** #335 — undo 시에도 SceneCard 를 함께 롤백. */
  scene_cards: Record<string, unknown>[];
  snapshot_id: string;
  has_more_snapshots: boolean;
  rolled_back_at: string;
}

// ── 호출 함수 ────────────────────────────────────────────────────────────

/**
 * 자연어 명령 → patch 미리보기.
 *
 * @throws axios 에러를 그대로 전파. 호출자는 status 별로 사용자 친화 메시지를 변환한다.
 *   - 422: LLM 응답이 유효한 patch 로 변환되지 않음(=명령을 이해 못 함)
 *   - 503: LLM 호출 실패(외부 API 문제)
 *   - 409: 같은 프로젝트에 이미 다른 nl-edit 요청 진행 중
 */
export async function previewNlEdit(
  projectId: string,
  instruction: string,
  history: NlEditTurn[] = [],
  targetSceneId: number | null = null,
): Promise<NlEditPreviewResponse> {
  const r = await api.post<NlEditPreviewResponse>(
    `/api/projects/${projectId}/nl-edit`,
    { instruction, history, target_scene_id: targetSceneId },
  );
  return r.data;
}

/**
 * 미리보기에서 받은 patch 를 실제 RenderSpec 에 적용.
 *
 * @param patch     미리보기 응답의 patch 그대로 다시 전달.
 * @param meta      감사 로그용(서버에 함께 저장). instruction/summary 는 빈 문자열도 허용.
 */
export async function applyNlEdit(
  projectId: string,
  patch: JsonPatchOp[],
  meta?: { instruction?: string; summary?: string },
): Promise<NlEditApplyResponse> {
  const r = await api.post<NlEditApplyResponse>(
    `/api/projects/${projectId}/nl-edit/apply`,
    {
      patch,
      instruction: meta?.instruction ?? "",
      summary: meta?.summary ?? "",
    },
  );
  return r.data;
}

/** 직전 snapshot 으로 RenderSpec 롤백. 추가 snapshot 이 더 없을 경우 has_more=false. */
export async function undoNlEdit(
  projectId: string,
): Promise<NlEditUndoResponse> {
  const r = await api.post<NlEditUndoResponse>(
    `/api/projects/${projectId}/nl-edit/undo`,
    {},
  );
  return r.data;
}

/**
 * #317 — 자연어 편집 세션 종료 시 Gemini 컨텍스트 캐시를 명시 해제.
 * NlEditorPanel 언마운트 시 호출. 실패해도 TTL 자동 만료로 안전망 작동.
 */
export async function releaseNlEditCache(projectId: string): Promise<void> {
  try {
    await api.post(`/api/projects/${projectId}/nl-edit/cache/release`, {});
  } catch (e) {
    // 캐시 해제 실패는 사용자에게 노출하지 않음 — TTL 만료가 안전망.
    // eslint-disable-next-line no-console
    console.warn("[nlEdit] 캐시 해제 실패(무시)", e);
  }
}
