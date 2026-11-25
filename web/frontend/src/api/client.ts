import axios from "axios";
import type {
  Project,
  SceneCard,
  FootageItem,
  FootageAsset,
  SplitPoint,
  MatchingMethod,
  EffectConfig,
  AppSettings,
} from "../types";

const BASE = typeof window !== "undefined" ? window.location.origin : "http://localhost:5173";

export const api = axios.create({ baseURL: BASE });

// ── Cost (#301) ────────────────────────────────────────────────────────────

export interface PhaseCost {
  phase: string;
  api_usd: number;
  compute_usd: number;
  storage_usd: number;
  egress_usd: number;
  total_usd: number;
  gemini_input_chars: number;
  gemini_output_chars: number;
  gemini_video_sec: number;
  tts_chars: number;
  cpu_seconds: number;
  bytes_stored: number;
  bytes_egress: number;
  duration_sec: number;
  // #355 — 어떤 Gemini 모델로 발생한 비용인지. 빈 문자열이면 모델 정보 없음
  // (Gemini 와 무관한 phase 또는 구버전 누적 항목).
  gemini_model?: string;
}

export interface CostBreakdown {
  provider: string;
  currency: string;
  pricing_updated_at: string;
  total_usd: number;
  api_usd: number;
  compute_usd: number;
  storage_usd: number;
  egress_usd: number;
  phases: PhaseCost[];
  // #355 — 모델별 API 비용 분해. 키는 모델 ID(`gemini-2.5-flash` 등), 값은 누적 USD.
  // 모델 정보가 없는 항목은 키 ""(빈 문자열)로 합산된다. 구버전 응답에는 없을 수 있으므로 옵셔널.
  by_model?: Record<string, number>;
  // Typecast — preview 응답에만 포함. tts_provider="typecast" 일 때 크레딧 예상.
  tts_provider?: string;
  tts_credits?: number;
  tts_credits_usd?: number;
  tts_free_covered?: boolean;
  tts_billable_credits?: number;
}

// ── TTS voices (Typecast 동적 조회) ─────────────────────────────────────────
export interface TypecastVoice {
  voice_id: string;
  voice_name?: string;
  gender?: string;
  age?: string;
  voice_type?: string;
  use_cases?: string[];
  models?: Array<{ version?: string; emotions?: string[] }>;
}

export const getTypecastVoices = () =>
  api
    .get<{ provider: string; voices: TypecastVoice[]; cached?: boolean }>(
      `/api/tts/voices?provider=typecast`,
    )
    .then((r) => r.data.voices);

/**
 * 서버측 미리듣기 합성 — provider 분기(Google/Typecast)를 백엔드 src.tts.synthesize 가 처리.
 * 브라우저가 Google REST 를 직접 못 타는 Typecast voice 미리듣기에 사용한다.
 * 응답은 오디오 Blob(Typecast=wav, Google=mp3).
 */
export const previewTtsAudio = (voiceName: string, rate?: number) =>
  api
    .post(
      `/api/tts/preview`,
      { voice_name: voiceName, ...(rate !== undefined ? { rate } : {}) },
      { responseType: "blob" },
    )
    .then((r) => r.data as Blob);

export const getCost = (projectId: string) =>
  api.get<CostBreakdown>(`/api/projects/${projectId}/cost`).then((r) => r.data);

export const previewCost = (projectId: string) =>
  api.get<CostBreakdown>(`/api/projects/${projectId}/cost/preview`).then((r) => r.data);

export const getPricing = () =>
  api.get<Record<string, unknown>>(`/api/cost/pricing`).then((r) => r.data);

// ── Projects ───────────────────────────────────────────────────────────────
export const createProject = (name: string, footage_dir: string, scenario_text: string) =>
  api.post<Project>("/api/projects", { name, footage_dir, scenario_text }).then((r) => r.data);

export const listProjects = () =>
  api.get<Project[]>("/api/projects").then((r) => r.data);

export const getProject = (id: string) =>
  api.get<Project>(`/api/projects/${id}`).then((r) => r.data);

export const registerProject = (id: string, name: string, scenes?: import("../types").SceneCard[], outputWidth?: number, outputHeight?: number) =>
  api.put<Project & { footage_exists?: Record<number, boolean> }>(`/api/projects/${id}/register`, { name, scenes, output_width: outputWidth, output_height: outputHeight }).then((r) => r.data);

// 전체 프로젝트 state(메타·studio_assets·bgm_tracks 포함)를 한 번에 푸시한다.
// IndexedDB의 진실을 서버 state.json으로 동기화할 때 사용.
/**
 * register 로 보낼 씬에서 **나레이션 필드를 제거**한다.
 *
 * 배경(#stale-narration): pushProjectState 는 IndexedDB 의 프로젝트 전체를 통째로
 * 보낸다. 그런데 나레이션 편집은 patchScene(서버가 진실의 원천)으로만 저장되므로,
 * IndexedDB 쪽 narration_text/narration_rev 는 쉽게 뒤처진다. 그 상태에서 이름 변경·
 * BGM 편집 같은 *나레이션과 무관한* 동작이 register 를 부르면, 옛 나레이션이 서버로
 * 다시 밀려가 최신 대본을 덮어쓰려 한다.
 *
 * 서버가 리비전 검사로 막아주긴 하지만(register#stale), 그러면 이번엔 "고쳐도 반영이
 * 안 되는" 증상이 된다(실측: 씬 29 에서 326회 거부, rev 62 까지 상승).
 * → 아예 보내지 않는 것이 옳다. 나레이션의 단일 창구는 patchScene 이다.
 */
function _stripNarration(scenes: Project["scenes"]): Project["scenes"] {
  if (!Array.isArray(scenes)) return scenes;
  return scenes.map((s) => {
    const { narration_text, narration_rev, narration_segments, ...rest } = s as any;
    return rest;
  }) as Project["scenes"];
}

export const pushProjectState = (id: string, project: Project) =>
  api.put<Project>(`/api/projects/${id}/register`, {
    name: project.name,
    // 수동 시리즈 그룹명. 빈 문자열은 시리즈 해제로 서버에 반영된다.
    series: (project as any).series,
    scenario_text: (project as any).scenario_text,
    tts_voice: (project as any).tts_voice,
    status: (project as any).status,
    // 나레이션은 제외 — patchScene 이 단일 창구다. 위 _stripNarration 주석 참고.
    scenes: _stripNarration(project.scenes),
    studio_assets: (project as any).studio_assets,
    bgm_tracks: (project as any).bgm_tracks,
    output_width: (project as any).output_width,
    output_height: (project as any).output_height,
    bg_color: (project as any).bg_color,
    force_split_scene_dissolve: (project as any).force_split_scene_dissolve,
    // #355 — Gemini 모델 선택. 서버 register 가 RegisterProjectRequest 에 필드를
    // 추가하지 않아도 무시되도록 처리되지만, 정식 채널은 PATCH 가 더 적합하므로
    // updateProject 호출 시점에 같이 전달된다(아래 path 가 register 인 한 무해).
    gemini_model: (project as any).gemini_model,
  }).then((r) => r.data);

export const deleteProject = (id: string) =>
  api.delete(`/api/projects/${id}`);

// ── Scenario ───────────────────────────────────────────────────────────────
export const parseScenario = (id: string, scenario_text: string) =>
  api
    .post<{ scenes: SceneCard[] }>(`/api/projects/${id}/parse-scenario`, {
      scenario_text,
    })
    .then((r) => r.data.scenes);

// ── Footage ────────────────────────────────────────────────────────────────
export const indexFootage = (id: string, force = false) =>
  api
    .post<{ task_id: string }>(`/api/projects/${id}/index-footage`, { force })
    .then((r) => r.data.task_id);

// Setup Step 3의 `gemini_content` 카드 활성화 판정용.
// indexed=false 면 청크 description 활용이 0이라 결과가 `gemini_filename`과 동일해지므로
// 카드를 disabled 처리. 인덱싱된 footage 가 1개 이상이면 indexed=true.
export interface FootageIndexStatus {
  indexed: boolean;
  chunk_count: number;
}

export const getFootageIndexStatus = (projectId: string) =>
  api
    .get<FootageIndexStatus>(`/api/projects/${projectId}/footage-index-status`)
    .then((r) => r.data);

// 인덱싱 예상 비용 — Studio "footage 목록" 탭 인덱싱 카드에 "예상 ~원" 라벨용.
// 2026-06: 파일 1개 = 청크 1개 정책. 영상 총 길이 × 295 tok/sec + 파일수 × 130 출력 토큰.
export interface IndexCostEstimate {
  file_count: number;
  cached_file_count: number;
  total_duration_sec: number;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  usd: number;
  krw: number;
  model: string;
}

export const getIndexCostEstimate = (projectId: string, model?: string) => {
  const params = new URLSearchParams();
  if (model) params.set("model", model);
  const qs = params.toString();
  return api
    .get<IndexCostEstimate>(
      `/api/projects/${projectId}/index-cost-estimate${qs ? `?${qs}` : ""}`,
    )
    .then((r) => r.data);
};

export const getFootage = (id: string) =>
  api.get<FootageItem[]>(`/api/projects/${id}/footage`).then((r) => r.data);

// ── Scene Cut Durations (#378) ─────────────────────────────────────────────
// Phase 5 (clip_editor) 가 산출한 `cache/timeline.json` 의 (scene_id, cut_sec) 매핑.
// 파일 부재 시 백엔드가 `{scenes:[]}` 를 200 으로 돌려준다(클라이언트는 폴백 우선순위로 자연 회귀).
// ETag 기반 캐시를 위해 마지막 etag 와 응답을 모듈 안에 보관 — 304 시 캐시된 결과 재사용.
export interface SceneCutDuration {
  scene_id: number;
  cut_sec: number;
}
const _cutDurEtagCache: Map<string, { etag: string; scenes: SceneCutDuration[] }> = new Map();
export async function getSceneCutDurations(projectId: string): Promise<SceneCutDuration[]> {
  const cached = _cutDurEtagCache.get(projectId);
  try {
    const headers: Record<string, string> = {};
    if (cached) headers["If-None-Match"] = cached.etag;
    const r = await api.get<{ scenes: SceneCutDuration[] }>(
      `/api/projects/${projectId}/scene-cut-durations`,
      { headers, validateStatus: (s) => s === 200 || s === 304 },
    );
    if (r.status === 304 && cached) return cached.scenes;
    const etag = (r.headers["etag"] as string | undefined) ?? "";
    const scenes = Array.isArray(r.data?.scenes) ? r.data.scenes : [];
    if (etag) _cutDurEtagCache.set(projectId, { etag, scenes });
    return scenes;
  } catch {
    return [];
  }
}

export const thumbnailUrl = (projectId: string, filename: string) =>
  `${BASE}/api/projects/${projectId}/footage/${encodeURIComponent(filename)}/thumbnail`;

export const footageStreamUrl = (projectId: string, filename: string) =>
  `${BASE}/api/projects/${projectId}/footage/${encodeURIComponent(filename)}/stream`;

/** BGM 트랙 audio_asset_id → 스트림 URL. BGM 은 프로젝트에 등록한 오디오(스튜디오 자산)만 쓴다(#419). */
export function resolveBgmStreamUrl(projectId: string, audioAssetId: string): string {
  return studioAssetStreamUrl(projectId, audioAssetId);
}

export const scenePreviewUrl = (projectId: string, sceneId: number, bust?: number) =>
  `${BASE}/api/projects/${projectId}/scenes/${sceneId}/preview${bust ? `?force=true&_=${bust}` : ""}`;

// signal: 이어보기 프리페치 취소용(옵셔널). 미지정 시 기존 동작과 100% 동일.
export async function getScenePreview(projectId: string, sceneId: number, signal?: AbortSignal): Promise<Blob> {
  const res = await fetch(`/api/projects/${projectId}/scenes/${sceneId}/preview`, { method: "POST", signal });
  if (!res.ok) throw new Error(`preview 생성 실패: ${res.status}`);
  return res.blob();
}

// #309 — 씬 1개의 RenderSpec(JSON)을 받아온다. 클라이언트 단순 렌더 분기 판정에 사용.
export async function getSceneRenderSpec(
  projectId: string,
  sceneId: number,
): Promise<import("../types").RenderSpec> {
  const res = await fetch(`/api/projects/${projectId}/scenes/${sceneId}/render-spec`);
  if (!res.ok) throw new Error(`RenderSpec 조회 실패: ${res.status}`);
  return res.json();
}

export async function uploadSceneFootage(projectId: string, sceneId: number, blob: Blob, filename: string): Promise<void> {
  const form = new FormData();
  form.append("file", blob, filename);
  // #308 — 가능하면 클라이언트가 해상도/길이 메타를 첨부해 백엔드 ffprobe 호출을 절감.
  // 추출 실패(타임아웃/디코드 실패 등)는 무시 — 백엔드가 기존 ffprobe로 fallback.
  // 이미지 파일은 video 메타 추출이 의미 없으므로 시도하지 않는다.
  const isImage = /\.(jpe?g|png|gif|webp|bmp|tiff)$/i.test(filename);
  if (!isImage) {
    try {
      const { extractVideoMeta, isVideoMetaSane } = await import("../lib/clientMediaMeta");
      const meta = await extractVideoMeta(blob).catch(() => null);
      if (meta && isVideoMetaSane(meta)) {
        form.append("client_meta", JSON.stringify(meta));
      }
    } catch {
      /* 동적 import 실패는 회귀 없이 무시 */
    }
  }
  const res = await fetch(`/api/projects/${projectId}/scenes/${sceneId}/footage-upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(`footage 업로드 실패: ${res.status}`);
}

export async function uploadSceneTts(projectId: string, sceneId: number, blob: Blob): Promise<void> {
  const form = new FormData();
  form.append("file", blob, `scene_${String(sceneId).padStart(3, "0")}.wav`);
  // #308 — TTS 음성도 동일하게 클라 메타 첨부(duration). 추출 실패는 무시.
  try {
    const { extractAudioMeta, isAudioMetaSane } = await import("../lib/clientMediaMeta");
    const meta = await extractAudioMeta(blob).catch(() => null);
    if (meta && isAudioMetaSane(meta)) {
      form.append("client_meta", JSON.stringify(meta));
    }
  } catch {
    /* 동적 import 실패는 무시 */
  }
  const res = await fetch(`/api/projects/${projectId}/scenes/${sceneId}/tts-upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(`TTS 업로드 실패: ${res.status}`);
}

// ── Matching ───────────────────────────────────────────────────────────────
// 백엔드 _do_match를 호출한다. method 분기는 호출자가 결정한다 — 특히 keyword_filename은
// 클라이언트에서 계산하고 saveKeywordMatchResult로 결과만 저장하는 경로(#311)가 권장된다.
//
// #355 후속 — `model` 인자로 Gemini 매칭에 사용할 모델 ID 를 함께 전달할 수 있다.
// 미지정 시 백엔드는 프로젝트의 `gemini_model` 로 폴백한다. method 가 gemini_* 가
// 아니면 model 은 무시된다.
export const startMatch = (
  id: string,
  method: MatchingMethod,
  scene_ids?: number[],
  model?: string,
) =>
  api
    .post<{ task_id: string }>(`/api/projects/${id}/match`, { method, scene_ids, model })
    .then((r) => r.data.task_id);

// 매칭 예상 비용 — Setup Step 3 카드에서 "예상 ~원" 라벨에 사용.
// Gemini 매칭이 아닌 method 는 0원으로 응답한다.
export interface MatchCostEstimate {
  usd: number;
  krw: number;
  per_scene_input_tokens: number;
  per_scene_output_tokens: number;
  scene_count: number;
  method: string;
  model: string;
}

export const getMatchCostEstimate = (
  projectId: string,
  method: string,
  model: string | undefined,
  sceneCount: number,
) => {
  const params = new URLSearchParams({
    method,
    scene_count: String(sceneCount),
  });
  if (model) params.set("model", model);
  return api
    .get<MatchCostEstimate>(
      `/api/projects/${projectId}/match-cost-estimate?${params.toString()}`,
    )
    .then((r) => r.data);
};

// ── #311 — 클라이언트 키워드 매칭 결과 저장 ────────────────────────────────
// 백엔드 KeywordMatchClientItem 모델은 pydantic2ts로 생성된 generated.ts에 정의됨.
// 호출자(예: clientKeywordMatching.runKeywordMatchClient)가 이 타입을 직접 사용해
// 백엔드 스키마와 1:1 정합성을 유지한다.
import type { KeywordMatchClientItem } from "../types/generated";

export type ClientKeywordMatchItem = KeywordMatchClientItem;

export const saveKeywordMatchResult = (
  projectId: string,
  items: ClientKeywordMatchItem[]
) =>
  api
    .post<{
      updated_scene_ids: number[];
      failed_scene_ids: number[];
      method: string;
      computed_on: string;
    }>(`/api/projects/${projectId}/match-keyword-client`, { items })
    .then((r) => r.data);

export const manualMatch = (
  projectId: string,
  sceneId: number,
  filename: string,
  trim_start = 0,
  trim_end?: number
) =>
  api
    .post<{ scene_id: number; updated: SceneCard }>(
      `/api/projects/${projectId}/scenes/${sceneId}/match-manual`,
      { filename, trim_start, trim_end }
    )
    .then((r) => r.data);

export const getMatchStatus = (id: string) =>
  api
    .get<{ matched: number; total: number; unmatched_scene_ids: number[]; all_matched: boolean }>(
      `/api/projects/${id}/match-status`
    )
    .then((r) => r.data);

// ── TTS ────────────────────────────────────────────────────────────────────
export const generateTts = (
  id: string,
  scene_ids?: number[],
  force = false,
  voice_name?: string | null,
) =>
  api
    .post<{ task_id: string }>(`/api/projects/${id}/tts/generate`, {
      scene_ids,
      force,
      // undefined 면 필드를 보내지 않음(서버 기본=프로젝트 tts_voice). null/문자열이면 전달.
      ...(voice_name !== undefined ? { voice_name } : {}),
    })
    .then((r) => r.data.task_id);

export const regenerateTts = (id: string, sceneId: number) =>
  generateTts(id, [sceneId], true);

export const setTtsVoice = (id: string, voice_name: string) =>
  api.patch(`/api/projects/${id}/tts/voice`, { voice_name }).then((r) => r.data);

export const updateNarration = (projectId: string, sceneId: number, narration_text: string) =>
  api
    .patch<{ scene_id: number; tts_queued: boolean; task_id: string }>(
      `/api/projects/${projectId}/scenes/${sceneId}/narration`,
      { narration_text }
    )
    .then((r) => r.data);

export const ttsAudioUrl = (projectId: string, sceneId: number) =>
  `${BASE}/api/projects/${projectId}/tts/${sceneId}/audio`;

// ── #382 — narration_segments API ───────────────────────────────────────
// 씬 1개에 여러 TTS 구간(N:1) 을 가지기 위한 segment CRUD + 생성.

import type { NarrationSegment } from "../types";

export interface CreateSegmentBody {
  id?: string;
  text?: string;
  offset_sec?: number;
  voice_name?: string | null;
  sbv_text?: string | null;
}

export interface UpdateSegmentBody {
  text?: string;
  offset_sec?: number;
  voice_name?: string | null;
  sbv_text?: string | null;
}

export const createNarrationSegment = (
  projectId: string,
  sceneId: number,
  body: CreateSegmentBody,
) =>
  api
    .post<NarrationSegment>(
      `/api/projects/${projectId}/scenes/${sceneId}/narration/segments`,
      body,
    )
    .then((r) => r.data);

export const updateNarrationSegment = (
  projectId: string,
  sceneId: number,
  segId: string,
  body: UpdateSegmentBody,
) =>
  api
    .patch<NarrationSegment>(
      `/api/projects/${projectId}/scenes/${sceneId}/narration/segments/${segId}`,
      body,
    )
    .then((r) => r.data);

export const deleteNarrationSegment = (
  projectId: string,
  sceneId: number,
  segId: string,
) =>
  api
    .delete<{ deleted: string }>(
      `/api/projects/${projectId}/scenes/${sceneId}/narration/segments/${segId}`,
    )
    .then((r) => r.data);

export const generateNarrationSegmentTts = (
  projectId: string,
  sceneId: number,
  segId: string,
  opts: { force?: boolean; voice_name?: string | null } = {},
) =>
  api
    .post<{ task_id: string; seg_id: string; scene_id: number }>(
      `/api/projects/${projectId}/scenes/${sceneId}/narration/segments/${segId}/generate`,
      { force: !!opts.force, voice_name: opts.voice_name ?? null },
    )
    .then((r) => r.data);

export const segmentAudioUrl = (
  projectId: string,
  segId: string,
  sceneId?: number,
) => {
  const q = sceneId !== undefined ? `?scene_id=${sceneId}` : "";
  return `${BASE}/api/projects/${projectId}/tts/segment/${segId}/audio${q}`;
};

// ── Scene patch ────────────────────────────────────────────────────────────
export const patchScene = (projectId: string, sceneId: number, data: Partial<SceneCard>) =>
  api.patch(`/api/projects/${projectId}/scenes/${sceneId}`, data).then((r) => r.data);

// ── Scene Comments (편집 지시 코멘트) ───────────────────────────────────────
// 씬에 남긴 편집 지시를 저장/조회/완료 처리한다. 서버가 진실의 원천이며,
// 코멘트는 렌더에 영향을 주지 않으므로 preview-cache 무효화가 필요 없다.
import type { CommentImage, SceneComment, Speaker, SpeakersResponse } from "../types";

export const listSceneComments = (projectId: string, sceneId: number) =>
  api
    .get<{ comments: SceneComment[] }>(`/api/projects/${projectId}/scenes/${sceneId}/comments`)
    .then((r) => r.data.comments);

export const createSceneComment = (
  projectId: string,
  sceneId: number,
  body: string,
  images: CommentImage[] = [],
) =>
  api
    .post<SceneComment>(`/api/projects/${projectId}/scenes/${sceneId}/comments`, { body, images })
    .then((r) => r.data);

export const updateSceneComment = (
  projectId: string,
  sceneId: number,
  commentId: string,
  body: string,
  images?: CommentImage[],
) =>
  api
    .patch<SceneComment>(`/api/projects/${projectId}/scenes/${sceneId}/comments/${commentId}`, {
      body,
      ...(images ? { images } : {}),
    })
    .then((r) => r.data);

/** 화자 사전 조회 — 사용 중인 씬 번호와 미등록(오타) 이름까지 함께 온다. */
export const listSpeakers = (projectId: string) =>
  api.get<SpeakersResponse>(`/api/projects/${projectId}/speakers`).then((r) => r.data);

/** 화자 사전 전체 교체. 이름 중복은 서버가 400 으로 거부한다. */
export const putSpeakers = (projectId: string, speakers: Speaker[]) =>
  api
    .put<{ speakers: Speaker[] }>(`/api/projects/${projectId}/speakers`, { speakers })
    .then((r) => r.data.speakers);

/** 코멘트 작성 중 붙여넣은 이미지를 업로드하고 자리표시자 번호(index)를 받는다. */
export const uploadCommentImage = (projectId: string, sceneId: number, file: File | Blob) => {
  const fd = new FormData();
  // Blob(클립보드 붙여넣기)은 name 이 없으므로 확장자만 맞춰 붙여 준다.
  const name = file instanceof File ? file.name : `pasted.${(file.type.split("/")[1] || "png")}`;
  fd.append("file", file, name);
  return api
    .post<CommentImage>(`/api/projects/${projectId}/scenes/${sceneId}/comment-images`, fd)
    .then((r) => r.data);
};

export const deleteSceneComment = (projectId: string, sceneId: number, commentId: string) =>
  api
    .delete(`/api/projects/${projectId}/scenes/${sceneId}/comments/${commentId}`)
    .then((r) => r.data);

/** 완료 처리. 기본 주체는 사용자(수동 체크)이며, 에이전트는 resolvedBy="agent" 로 호출한다. */
export const resolveSceneComment = (
  projectId: string,
  sceneId: number,
  commentId: string,
  opts?: { resolutionNote?: string; resolvedBy?: "agent" | "user" },
) =>
  api
    .post<SceneComment>(`/api/projects/${projectId}/scenes/${sceneId}/comments/${commentId}/resolve`, {
      resolution_note: opts?.resolutionNote ?? null,
      resolved_by: opts?.resolvedBy ?? "user",
    })
    .then((r) => r.data);

export const reopenSceneComment = (projectId: string, sceneId: number, commentId: string) =>
  api
    .post<SceneComment>(`/api/projects/${projectId}/scenes/${sceneId}/comments/${commentId}/reopen`)
    .then((r) => r.data);

// ── Effect ─────────────────────────────────────────────────────────────────
export const updateEffect = (projectId: string, sceneId: number, effect: EffectConfig) =>
  api
    .patch<{ scene_id: number; updated: SceneCard }>(
      `/api/projects/${projectId}/scenes/${sceneId}`,
      { footage: effect }
    )
    .then((r) => r.data);

// ── Render ─────────────────────────────────────────────────────────────────
export const startRender = (id: string, opts?: { skipSubtitle?: boolean; watermark?: boolean }) =>
  api
    .post<{ task_id: string }>(`/api/projects/${id}/render`, {
      skip_subtitle: !!opts?.skipSubtitle,
      watermark: !!opts?.watermark,
    })
    .then((r) => r.data.task_id);

export const renderOutputUrl = (projectId: string, version?: number | string) =>
  // #bgm-preview-cache — 서버가 cache-control 을 주지 않아 브라우저가 영상을 캐시한다.
  // 다시 렌더해도 BGM 편집 화면에 옛 영상이 나오므로, 렌더 결과의 mtime 을
  // 쿼리로 붙여 버전이 바뀌면 새로 받게 한다.
  `${BASE}/api/projects/${projectId}/render/output`
  + (version ? `?v=${version}` : "");

// ── BGM Render (#289) ──────────────────────────────────────────────────────
// 후처리 BGM 합성. final_output.mp4 + Project.bgm_tracks → final_with_bgm.mp4.
export const startBgmRender = (id: string) =>
  api
    .post<{ task_id: string }>(`/api/projects/${id}/render-bgm`, {})
    .then((r) => r.data.task_id);

export const bgmRenderStatus = (id: string) =>
  api.get<{ task_id: string | null; status: string; error?: string; output_file?: string }>(
    `/api/projects/${id}/render-bgm/status`
  ).then((r) => r.data);

export const bgmRenderOutputUrl = (projectId: string) =>
  `${BASE}/api/projects/${projectId}/render-bgm/output`;

// ── Settings ───────────────────────────────────────────────────────────────
export const getSettings = () =>
  api.get<AppSettings>("/api/settings").then((r) => r.data);

export const updateSettings = (data: Partial<Omit<AppSettings, "gcp_key_configured">>) =>
  api.put("/api/settings", data).then((r) => r.data);

export const uploadGcpKey = (file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<{ ok: boolean; project_id: string }>("/api/settings/gcp-key", form, {
    headers: { "Content-Type": "multipart/form-data" },
  }).then((r) => r.data);
};

// ── Studio ─────────────────────────────────────────────────────────────────
export const getStudioAssets = (projectId: string) =>
  api.get<FootageAsset[]>(`/api/projects/${projectId}/studio/assets`).then((r) => r.data);

export const updateAssetLabel = (projectId: string, assetId: string, label: string) =>
  api
    .patch<FootageAsset>(`/api/projects/${projectId}/studio/assets/${assetId}`, { label })
    .then((r) => r.data);

export const deleteAsset = (projectId: string, assetId: string) =>
  api.delete(`/api/projects/${projectId}/studio/assets/${assetId}`).then((r) => r.data);

// footage 파일을 '원본 파일명 그대로' 서버 footage_dir에 저장한다.
// 녹화·가져오기·구간분할·크롭으로 만든 자산을 서버에도 확보해 미리보기/렌더 404를 방지.
// on_conflict: 같은 이름이 이미 있을 때의 처리.
//   "skip"(기본) 기존 파일 유지 / "overwrite" 덮어쓰기 / "rename" `이름 (2).mp4` 로 저장.
// 응답 filename 은 **실제 저장된 최종 파일명** — rename 시 요청과 달라지므로
// 자산 등록에는 반드시 이 값을 써야 한다.
export type FootageConflictPolicy = "skip" | "overwrite" | "rename";

export interface AssetSyncResult {
  ok: boolean;
  filename: string;
  requested_filename: string;
  skipped: boolean;
  conflict: boolean;
  applied_policy: FootageConflictPolicy | "none";
}

export const syncStudioAssetFile = (
  projectId: string, file: File | Blob, filename: string,
  onConflict: FootageConflictPolicy = "skip",
) => {
  const form = new FormData();
  form.append("filename", filename);
  form.append("file", file, filename);
  form.append("on_conflict", onConflict);
  return api
    .post<AssetSyncResult>(
      `/api/projects/${projectId}/studio/asset-file-sync`, form,
      { headers: { "Content-Type": "multipart/form-data" } }
    )
    .then((r) => r.data);
};

// 가져오기 전 충돌 검사 — 이미 있는 파일명과 그 파일을 참조하는 씬 수를 조회한다.
export interface FootageConflict {
  filename: string;
  scene_refs: number;   // 덮어쓰기 시 함께 바뀌는 씬 수 (경고용)
  suggested: string;    // "둘 다 유지" 선택 시 저장될 이름 미리보기
}

export const checkFootageConflicts = (projectId: string, filenames: string[]) =>
  api
    .post<{ conflicts: FootageConflict[] }>(
      `/api/projects/${projectId}/studio/footage-conflicts`, { filenames }
    )
    .then((r) => r.data.conflicts);

// 다른(또는 같은) 프로젝트의 footage 파일을 destProjectId로 서버에서 직접 복사한다.
// 서버가 소스 footage_dir→타겟 footage_dir로 실제 파일을 복사하고 등록한 asset을 반환.
export const copyFootageFrom = (
  destProjectId: string,
  body: { source_project_id: string; asset_id: string }
) =>
  api
    .post<{ asset: FootageAsset }>(
      `/api/projects/${destProjectId}/studio/assets/copy-from`,
      { source_project_id: body.source_project_id, source_asset_id: body.asset_id }
    )
    .then((r) => r.data.asset);

export const uploadRecording = async (
  projectId: string,
  file: File,
  label: string,
  splitPoints?: SplitPoint[]
) => {
  const form = new FormData();
  form.append("video", file);
  form.append("label", label);
  if (splitPoints && splitPoints.length > 0) {
    form.append("split_points", JSON.stringify(splitPoints));
  }
  // #308 — 분할이 없으면(=원본 그대로 저장) 클라 메타를 첨부. 분할이 있으면
  // 백엔드가 ffmpeg split 후 각 part 의 메타를 따로 측정해야 하므로 첨부 의미가 없다.
  if (!splitPoints || splitPoints.length === 0) {
    try {
      const { extractVideoMeta, isVideoMetaSane } = await import("../lib/clientMediaMeta");
      const meta = await extractVideoMeta(file).catch(() => null);
      if (meta && isVideoMetaSane(meta)) {
        form.append("client_meta", JSON.stringify(meta));
      }
    } catch {
      /* 동적 import 실패는 무시 */
    }
  }
  return api
    .post<{ assets: FootageAsset[] }>(`/api/projects/${projectId}/studio/recording`, form, {
      headers: { "Content-Type": "multipart/form-data" },
    })
    .then((r) => r.data.assets);
};

export const uploadPresentation = (projectId: string, htmlFile: File) => {
  const form = new FormData();
  form.append("html_file", htmlFile);
  return api
    .post<{ assets: FootageAsset[]; client_capture_used?: boolean }>(
      `/api/projects/${projectId}/studio/presentation`,
      form,
      {
        headers: { "Content-Type": "multipart/form-data" },
      }
    )
    .then((r) => r.data.assets);
};

/**
 * #313 — 클라이언트(pdf.js / html2canvas) 가 미리 캡처한 PNG 페이지들을 백엔드에
 * 일괄 등록. Playwright 호출이 일어나지 않아 가장 큰 비용 절감 경로.
 *
 * @param projectId 프로젝트 ID
 * @param imageFiles 페이지별 PNG File (순서대로 1..N 페이지)
 * @param meta      원본 파일 stem / 모드 등 디버깅 정보
 */
export const uploadPresentationImages = (
  projectId: string,
  imageFiles: File[],
  meta?: { stem?: string; mode?: "pdf" | "html"; total_pages?: number }
) => {
  const form = new FormData();
  for (const f of imageFiles) {
    // 동일 키 반복 → FastAPI list[UploadFile] 로 수신.
    form.append("images", f);
  }
  if (meta) {
    form.append("client_capture_meta", JSON.stringify(meta));
  }
  return api
    .post<{ assets: FootageAsset[]; client_capture_used?: boolean }>(
      `/api/projects/${projectId}/studio/presentation`,
      form,
      {
        headers: { "Content-Type": "multipart/form-data" },
      }
    )
    .then((r) => r.data);
};

export const importExistingFootage = (projectId: string) =>
  api
    .post<{ imported: number; assets: FootageAsset[] }>(
      `/api/projects/${projectId}/studio/import-existing`
    )
    .then((r) => r.data);

export const studioAssetThumbnailUrl = (projectId: string, assetId: string) =>
  `${BASE}/api/projects/${projectId}/studio/assets/${assetId}/thumbnail`;

// #289/#290 — studio audio/video 자산 미리듣기·미리보기 스트림 URL.
export const studioAssetStreamUrl = (projectId: string, assetId: string) =>
  `${BASE}/api/projects/${projectId}/studio/assets/${assetId}/stream`;

// ── 프레임 스트립 (구간 분할 보조) ─────────────────────────────────────────
// interval 초 간격 프레임을 가로 1줄로 이어붙인 스프라이트 시트 1장.
// 좌표 계산에 필요한 메타는 `?meta=1` 로 먼저 받고, 이미지는 <img src> 로 1회만 받는다.

export interface FilmstripMeta {
  /** 실제 적용된 간격(초). 최대 타일 수 제한 때문에 요청값보다 커질 수 있다. */
  interval: number;
  /** 프론트가 요청한 간격(초). */
  requested_interval: number;
  /** 스프라이트에 들어있는 타일 수. */
  tile_count: number;
  tile_width: number;
  tile_height: number;
  /** = tile_count * tile_width. background-size 계산에 사용. */
  sheet_width: number;
  duration_sec: number;
  cache_hit: boolean;
}

export const studioAssetFilmstripUrl = (
  projectId: string,
  assetId: string,
  interval: number,
) =>
  `${BASE}/api/projects/${projectId}/studio/assets/${assetId}/filmstrip?interval=${interval}`;

export const getStudioAssetFilmstripMeta = (
  projectId: string,
  assetId: string,
  interval: number,
) =>
  api
    .get<FilmstripMeta>(
      `/api/projects/${projectId}/studio/assets/${assetId}/filmstrip`,
      { params: { interval, meta: 1 } },
    )
    .then((r) => r.data);

// ── 장면 텍스트 검색 (구간 분할 보조) ──────────────────────────────────────
// `web/data/{project_id}/scene_index.json` 의 장면 설명 색인. 외부 도구가 만든
// 읽기 전용 데이터이므로 **없는 것이 정상 상태**다. 백엔드는 파일이 없거나
// 깨져 있어도 200 + `available: false` 로 응답하므로 호출부는 에러 처리 대신
// available 플래그만 보면 된다. 타입은 백엔드 dict 응답에 맞춘 로컬 선언
// (generated.ts 재생성 스크립트는 .venv 를 건드려 실행하지 않는다).

export interface SceneIndexSegment {
  /** 원본 클립 파일명. */
  file: string;
  /** **클립 내부의 상대 시각**(초). 프로젝트 타임라인 시각이 아니다. */
  start_sec: number;
  end_sec: number;
  /** 등장 대상 목록 — 검색 대상에 포함된다. */
  subjects?: string[];
  desc: string;
  quality?: "good" | "fair" | "poor" | string;
  /** 색인 생성 당시와 원본 파일 시그니처가 다름 → 설명이 낡았을 수 있다. */
  stale?: boolean;
  /** 원본 파일이 footage_dir 에 없음. */
  missing?: boolean;
}

export interface SceneIndexResponse {
  /** false 면 색인 파일이 없거나 파싱 실패 — 기능만 조용히 끈다. */
  available: boolean;
  segments: SceneIndexSegment[];
  meta?: Record<string, unknown>;
  /** 반환된 segments 중 하나라도 stale 인지(패널 경고 한 줄용). */
  stale?: boolean;
  missing?: boolean;
  /** available: false 일 때의 사유(파싱 에러 메시지 등). */
  reason?: string;
}

export const getStudioSceneIndex = (projectId: string, assetId?: string) =>
  api
    .get<SceneIndexResponse>(`/api/projects/${projectId}/studio/scene-index`, {
      params: assetId ? { asset_id: assetId } : undefined,
    })
    .then((r) => r.data);

// ── SFX 프리셋 (#sfx-presets) ──────────────────────────────────────────────
// 앱 내장 기본 효과음 카탈로그(assets/sfx_presets, #418). 프로젝트와 무관하게 공유한다.
// 오버레이의 sfx_asset_id 에 `"preset:{preset_key}"` 형식으로 저장하면 백엔드
// `_resolve_sfx` 가 prefix 분기하여 절대 경로로 해석한다.
export interface SfxPreset {
  preset_key: string;       // "coin-pickup-1"
  preset_id: string;        // "coin-pickup"
  seq: number;              // 1, 2, 3
  file_path: string;        // 절대 경로 (디버그 표시용)
  duration: number;
  prompt: string;
  is_user?: boolean;        // true = 메인 화면에서 직접 올린 공용 효과음
}

export const listSfxPresets = () =>
  api.get<{ presets: SfxPreset[] }>("/api/sfx-presets").then((r) => r.data.presets);

// ── 사용자 공용 효과음 라이브러리 (모든 프로젝트 공유, 참조는 "preset:user-xxxx") ──
export interface UserSfxItem {
  key: string;
  label: string;
  file: string;
  original_name: string;
  duration: number;
  created_at: string;
}

export const uploadUserSfx = (files: File[]) => {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  return api
    .post<{ added: UserSfxItem[]; skipped: string[] }>("/api/sfx-presets/user", form)
    .then((r) => r.data);
};

export const renameUserSfx = (key: string, label: string) =>
  api.patch<UserSfxItem>(`/api/sfx-presets/user/${encodeURIComponent(key)}`, { label }).then((r) => r.data);

export const deleteUserSfx = (key: string) =>
  api.delete(`/api/sfx-presets/user/${encodeURIComponent(key)}`).then((r) => r.data);

export const sfxPresetStreamUrl = (presetKey: string) =>
  `${BASE}/api/sfx-presets/${encodeURIComponent(presetKey)}/stream`;

/** sfx_asset_id 가 `"preset:{key}"` 형식이면 프리셋 스트림 URL, 아니면 studio_asset 스트림 URL. */
export const resolveSfxStreamUrl = (projectId: string, sfxAssetId: string) => {
  if (sfxAssetId.startsWith("preset:")) {
    return sfxPresetStreamUrl(sfxAssetId.slice("preset:".length));
  }
  return studioAssetStreamUrl(projectId, sfxAssetId);
};

export const createStudioProject = (name: string) =>
  api
    .post<Project>("/api/projects", { name, auto_footage_dir: true })
    .then((r) => r.data);

// ── Templates (#406/#407) ──────────────────────────────────────────────────
// 영상 구조(씬 배치 + 자막 + 오버레이 + BGM)를 원본 영상 없이 보존·재사용.
// 백엔드 경로: GET/POST /api/templates, GET/DELETE /api/templates/{id}, POST /api/projects/from-template

export interface TemplateMeta {
  template_id: string;
  title: string;
  description: string;
  created_at: string;
  source_project_id: string;
  scene_count: number;
}

/** 템플릿 상세 — Template 모델 직렬화 형태. 새 프로젝트 생성 흐름에서만 사용. */
export interface TemplateDetail extends TemplateMeta {
  scenes: SceneCard[];
  output_width: number;
  output_height: number;
  tts_voice: string;
  gemini_model: string;
  bgm_tracks: unknown[];
  force_split_scene_dissolve: boolean;
}

export const listTemplates = () =>
  api.get<TemplateMeta[]>("/api/templates").then((r) => r.data);

export const getTemplate = (templateId: string) =>
  api.get<TemplateDetail>(`/api/templates/${templateId}`).then((r) => r.data);

export const createTemplate = (body: { source_project_id: string; title: string; description?: string }) =>
  api.post<TemplateMeta>("/api/templates", body).then((r) => r.data);

export const deleteTemplate = (templateId: string) =>
  api.delete(`/api/templates/${templateId}`).then((r) => r.data);

export const createProjectFromTemplate = (body: { template_id: string; title?: string }) =>
  api.post<Project>("/api/projects/from-template", body).then((r) => r.data);

// ── Clip library ───────────────────────────────────────────────────────────
// 밈영상/로딩 애니메이션 등 재사용 영상의 전역 라이브러리.
// 씬 지정은 서버가 프로젝트 footage 폴더로 하드링크(복사 폴백)를 걸어 처리하므로,
// 프론트는 응답의 `filename` 으로 평범한 footage 를 구성하기만 하면 된다.

export interface ClipLibraryEntry {
  clip_id: string;
  title: string;
  category: string;
  filename: string;
  source_url: string;
  duration_sec: number;
  width: number;
  height: number;
  size_bytes: number;
  created_at: string;
}

/** 씬 지정 결과. `filename` 은 충돌 회피가 적용된 확정 파일명 — 반드시 이 값을 쓸 것. */
export interface SceneFromClipLibraryResponse {
  filename: string;
  duration_sec: number;
  mode: string;
}

export const listClipLibrary = () =>
  api.get<ClipLibraryEntry[]>("/api/clip-library").then((r) => r.data);

/** URL 등록. 다운로드가 서버에서 일어나므로 응답이 느릴 수 있어 타임아웃을 늘린다. */
export const registerClip = (body: { url: string; title?: string; category?: string }) =>
  api.post<ClipLibraryEntry>("/api/clip-library", body, { timeout: 180_000 }).then((r) => r.data);

export const deleteClip = (clipId: string) =>
  api.delete(`/api/clip-library/${clipId}`).then((r) => r.data);

export const clipStreamUrl = (clipId: string) =>
  `${BASE}/api/clip-library/${encodeURIComponent(clipId)}/stream`;

export const clipThumbnailUrl = (clipId: string) =>
  `${BASE}/api/clip-library/${encodeURIComponent(clipId)}/thumbnail`;

export const assignClipToScene = (projectId: string, sceneId: number, clipId: string) =>
  api
    .post<SceneFromClipLibraryResponse>(
      `/api/projects/${projectId}/scenes/${sceneId}/from-clip-library`,
      { clip_id: clipId }
    )
    .then((r) => r.data);

// ── Series (빈 시리즈 유지용 보조 저장소) ────────────────────────────────────
// 프로젝트-시리즈 연결은 여전히 Project.series(문자열)이다. 이 API 는 "프로젝트
// 0개인 빈 시리즈"까지 홈 화면에 표시하기 위한 보조 목록을 관리한다.
// 백엔드: GET/POST /api/series, PATCH/DELETE /api/series/{id}. `_series/` 폴더 저장.

export interface SeriesItem {
  id: string;
  name: string;
  created_at: string;
}

export const listSeries = () =>
  api.get<SeriesItem[]>("/api/series").then((r) => r.data);

/** 시리즈 생성. 같은 이름이 이미 있으면 백엔드가 기존 항목을 그대로 반환(멱등). */
export const createSeries = (name: string) =>
  api.post<SeriesItem>("/api/series", { name }).then((r) => r.data);

/** 이름 변경. 소속 프로젝트들의 series 값도 서버가 일괄 갱신한다. */
export const renameSeries = (id: string, name: string) =>
  api.patch<SeriesItem>(`/api/series/${id}`, { name }).then((r) => r.data);

/** 삭제. 소속 프로젝트들의 series 는 서버가 빈 문자열(미편성)로 되돌린다. */
export const deleteSeries = (id: string) =>
  api.delete<{ deleted: boolean; name: string }>(`/api/series/${id}`).then((r) => r.data);

// ── WebSocket ──────────────────────────────────────────────────────────────
export const wsUrl = (taskId: string) => {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/${taskId}`;
};
