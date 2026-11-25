/**
 * IndexedDB 저장소 — 백엔드 state.json 대체
 * idb 라이브러리로 Promise 기반 접근
 */
import { openDB, type IDBPDatabase } from "idb";
import type { Project, AppSettings, TextOverlayPreset } from "../types";
import { BUILTIN_TEXT_PRESETS } from "./textPresetSeeds";
import type { UserSceneTag } from "./sceneTags";

const DB_NAME = "video-agent";
// v4 (#214): userSceneTags objectStore 추가 — 유저 정의 씬 태그 사전.
// v5 (#272): localThumbnailCache objectStore 추가 — 로컬 파일 첫프레임 썸네일 캐시.
// v6 (#312): audioWaveformCache objectStore 추가 — 클라 계산 파형 피크 캐시.
// v7 (#316): nlEditChat objectStore 추가 — 자연어 편집 세션 채팅 히스토리(프로젝트별).
// v8 (#369): sceneHistory objectStore 추가 — Undo/Redo patch + 가장 오래된 base.
const DB_VERSION = 8;

/** 로컬 파일 썸네일 캐시 1개 레코드 (#272) */
export interface LocalThumbnailRecord {
  key: string;        // `${filename}:${lastModified}` 형태 (mtime 기반 무효화)
  dataUrl: string;    // image/png base64 data URL
  created_at: string;
}

/**
 * 자연어 편집 채팅 1턴 레코드 (#316)
 *
 * 프로젝트별로 가져오기 쉽게 `${projectId}:${turnId}` 를 키로 사용한다.
 * - role: "user" — 사용자 자연어 입력
 * - role: "ai"   — AI 응답(요약 + patch + safety_flags + 비용)
 *
 * `applied`/`undone` 은 메시지 표시 후 사용자가 적용/되돌리기를 클릭했을 때 갱신된다.
 * `pending` 은 LLM 호출 중인 자리표시 메시지를 구분하는 용도(스피너 노출).
 */
export interface NlEditChatTurnRecord {
  /** PK — `${projectId}:${turnId}` */
  key: string;
  project_id: string;
  /** 턴 식별자 — ISO timestamp + 짧은 랜덤. 동일 프로젝트 내에서 정렬 키 역할도 한다. */
  turn_id: string;
  role: "user" | "ai" | "system";
  /** 사용자 메시지: 원본 instruction. AI 메시지: summary(요약). system: 안내 텍스트. */
  content: string;
  created_at: string;
  /** AI 응답일 때만 — RFC 6902 JSON Patch 배열(직렬화 가능 JSON). */
  patch?: unknown[];
  /** AI 응답일 때만 — 위험 플래그 키. */
  safety_flags?: string[];
  /** AI 응답일 때만 — 예상 비용(USD). */
  estimated_cost_usd?: number;
  /** AI 응답일 때만 — 적용 완료 여부. */
  applied?: boolean;
  /** AI 응답일 때만 — 적용 후 사용자가 되돌리기한 경우 true. */
  undone?: boolean;
  /** AI 응답을 사용자가 명시적으로 거부한 경우. UI에서 "거부됨" 표시. */
  rejected?: boolean;
  /** AI 응답일 때만 — apply 시 받은 snapshot id (없을 수 있음). */
  snapshot_id?: string;
  /** LLM 호출 중인 자리표시일 때 true — UI 가 스피너를 보여준다. */
  pending?: boolean;
  /** 호출 실패 시 에러 메시지. */
  error?: string;
  /** AI 응답일 때만 — 이벤트 기반 자동 분할 결과(kind='split'). */
  split_result?: { scene_id: number; cuts_sec: number[]; reasoning: string; scene_duration_sec?: number | null } | null;
  /** AI 응답일 때만 — 이벤트 기반 자동 오버레이 결과(kind='overlay'). */
  overlay_result?: {
    scene_id: number;
    overlays: Array<{
      id: string; text: string; x: number; y: number;
      font_family: string; color: string;
      start_sec: number; end_sec: number;
      sfx_asset_id: string | null; sfx_volume: number;
    }>;
    reasoning: string;
  } | null;
}

/**
 * BGM/오디오 자산 파형 캐시 1개 레코드 (#312)
 *
 * 키: `${projectId}:${assetId}:${peakCount}` — 같은 자산을 다른 줌 레벨로 다시
 * 요청해도 캐시 적중하도록 peakCount 도 키에 포함. 자산이 교체(asset_id 변경)
 * 되면 자연 무효화된다.
 */
export interface AudioWaveformRecord {
  key: string;
  peaks: number[];
  duration: number;
  sample_rate: number;
  channels: number;
  created_at: string;
}

export interface DB {
  projects: {
    key: string;
    value: Project;
  };
  settings: {
    key: string;
    value: string; // JSON 직렬화
  };
  // 파일 핸들 저장 (File System Access API)
  fileHandles: {
    key: string; // projectId
    value: FileSystemDirectoryHandle;
  };
  // TTS 오디오 blob 저장 (key: `${projectId}:${sceneId}`)
  ttsAudio: {
    key: string;
    value: Blob;
  };
  // 텍스트 오버레이 스타일 프리셋 (프로젝트 무관 전역 저장)
  overlayPresets: {
    key: string; // id
    value: TextOverlayPreset;
  };
  // 유저 정의 씬 태그 사전 (#214) — 빌트인 사전과 머지되어 사용된다.
  // 같은 id가 빌트인에도 있으면 유저 정의가 오버라이드한다.
  userSceneTags: {
    key: string; // id (영문 소문자 + 하이픈)
    value: UserSceneTag;
  };
  // 로컬 파일 첫프레임 썸네일 캐시 (#272)
  // 키: `${filename}:${lastModified}` — 파일이 변경되면 자동 무효화된다.
  localThumbnailCache: {
    key: string;
    value: LocalThumbnailRecord;
  };
  // BGM/오디오 자산 파형 캐시 (#312)
  // 키: `${projectId}:${assetId}:${peakCount}` — 자산 교체/줌 변경 시 자동 무효화.
  audioWaveformCache: {
    key: string;
    value: AudioWaveformRecord;
  };
  // 자연어 편집 채팅 히스토리 (#316)
  // 키: `${projectId}:${turnId}` — 프로젝트별 prefix scan 으로 일괄 조회/삭제.
  nlEditChat: {
    key: string;
    value: NlEditChatTurnRecord;
  };
  // Undo/Redo patch 영속화 (#369) — 새로고침 후 직전 N단계 history 복원용.
  // 키: 두 종류가 한 store 에 공존한다.
  //   - `${projectId}:base`   → SceneHistoryBaseRecord (가장 오래된 commit 시점 직전 project)
  //   - `${projectId}:p:${seq}` → SceneHistoryPatchRecord (forward delta, seq 는 정렬 가능)
  // 다른 프로젝트의 patch 가 섞이지 않도록 prefix scan 으로 격리한다.
  sceneHistory: {
    key: string;
    value: SceneHistoryRecord;
  };
}

/**
 * Undo/Redo patch 1단계 영속화 레코드 (#369).
 *
 * 한 store(`sceneHistory`) 에 base + patch 두 종류 레코드가 키 prefix 로 구분되어 공존한다.
 */
export type SceneHistoryRecord = SceneHistoryBaseRecord | SceneHistoryPatchRecord;

export interface SceneHistoryBaseRecord {
  /** PK — `${projectId}:base` */
  key: string;
  project_id: string;
  kind: "base";
  /** 가장 오래된 commit 시점의 *직전* project (deep clone, JSON 직렬화 가능). */
  base: Project;
  /** 최근 갱신 시각 — TTL 정리에서 base 가 너무 오래되어 다른 프로젝트와 무관하게
   *  유지될 위험을 차단하기 위한 보조 메타. */
  updated_at: string;
}

export interface SceneHistoryPatchRecord {
  /** PK — `${projectId}:p:${seq}` (seq 는 zero-padded 정수, 정렬 가능) */
  key: string;
  project_id: string;
  kind: "patch";
  /** 같은 프로젝트 내 단조 증가 시퀀스(0 부터). */
  seq: number;
  /** jsondiffpatch Delta — JSON 직렬화 가능. */
  patch: unknown;
  /** 생성 시각 ISO 문자열. 7일 이상 된 patch 는 진입 시 정리. */
  created_at: string;
}

let _db: IDBPDatabase<DB> | null = null;
let _seedPromise: Promise<void> | null = null;

export async function getDb(): Promise<IDBPDatabase<DB>> {
  if (_db) return _db;
  _db = await openDB<DB>(DB_NAME, DB_VERSION, {
    upgrade(db) {
      if (!db.objectStoreNames.contains("projects")) {
        db.createObjectStore("projects", { keyPath: "project_id" });
      }
      if (!db.objectStoreNames.contains("settings")) {
        db.createObjectStore("settings");
      }
      if (!db.objectStoreNames.contains("fileHandles")) {
        db.createObjectStore("fileHandles");
      }
      if (!db.objectStoreNames.contains("ttsAudio")) {
        db.createObjectStore("ttsAudio");
      }
      if (!db.objectStoreNames.contains("overlayPresets")) {
        db.createObjectStore("overlayPresets", { keyPath: "id" });
      }
      // v4 추가 (#214): 유저 정의 씬 태그 사전
      if (!db.objectStoreNames.contains("userSceneTags")) {
        db.createObjectStore("userSceneTags", { keyPath: "id" });
      }
      // v5 추가 (#272): 로컬 파일 첫프레임 썸네일 캐시
      if (!db.objectStoreNames.contains("localThumbnailCache")) {
        db.createObjectStore("localThumbnailCache", { keyPath: "key" });
      }
      // v6 추가 (#312): 오디오 자산 파형 캐시
      if (!db.objectStoreNames.contains("audioWaveformCache")) {
        db.createObjectStore("audioWaveformCache", { keyPath: "key" });
      }
      // v7 추가 (#316): 자연어 편집 채팅 히스토리
      if (!db.objectStoreNames.contains("nlEditChat")) {
        db.createObjectStore("nlEditChat", { keyPath: "key" });
      }
      // v8 추가 (#369): Undo/Redo patch 영속화
      if (!db.objectStoreNames.contains("sceneHistory")) {
        db.createObjectStore("sceneHistory", { keyPath: "key" });
      }
    },
  });
  // DB가 새로 열린 직후 1회만 빌트인 시드를 주입한다.
  // 이미 같은 ID가 존재하면 덮어쓰지 않음 (유저 수정 보존).
  if (!_seedPromise) {
    _seedPromise = seedBuiltinOverlayPresets(_db).catch((err) => {
      console.error("[db] builtin preset seed failed:", err);
    });
  }
  await _seedPromise;
  return _db;
}

/**
 * 빌트인 텍스트 오버레이 프리셋 시드 주입.
 * - 같은 ID가 이미 존재하면 건너뛴다 (덮어쓰지 않음).
 * - 신규 ID만 `put`으로 삽입한다.
 *
 * 향후 `BUILTIN_TEXT_PRESETS` 배열에 항목이 추가되면, 다음 앱 로드 시
 * 누락된 항목만 자동으로 시드된다.
 */
async function seedBuiltinOverlayPresets(db: IDBPDatabase<DB>): Promise<void> {
  const tx = db.transaction("overlayPresets", "readwrite");
  const store = tx.objectStore("overlayPresets");
  for (const preset of BUILTIN_TEXT_PRESETS) {
    const existing = await store.get(preset.id);
    if (!existing) {
      await store.put(preset);
    }
  }
  await tx.done;
}

// ── Projects ───────────────────────────────────────────────────────────────

export async function dbListProjects(): Promise<Project[]> {
  const db = await getDb();
  const all = await db.getAll("projects");
  return all.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

export async function dbGetProject(id: string): Promise<Project | undefined> {
  const db = await getDb();
  return db.get("projects", id);
}

export async function dbSaveProject(project: Project): Promise<void> {
  const db = await getDb();
  await db.put("projects", { ...project, updated_at: new Date().toISOString() });
}

export async function dbDeleteProject(id: string): Promise<void> {
  const db = await getDb();
  await db.delete("projects", id);
  await db.delete("fileHandles", id);
}

// ── Settings ───────────────────────────────────────────────────────────────

const SETTINGS_KEY = "app_settings";

const DEFAULT_SETTINGS: AppSettings = {
  google_api_key: "",
  gemini_request_interval_sec: "4",
  gemini_max_retry: "3",
  tts_voice_name: "ko-KR-Chirp3-HD-Orus",
  tts_speaking_rate: "0.9",
  gcp_key_configured: false,
};

export async function dbGetSettings(): Promise<AppSettings> {
  const db = await getDb();
  const raw = await db.get("settings", SETTINGS_KEY);
  if (!raw) return DEFAULT_SETTINGS;
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export async function dbSaveSettings(settings: Partial<AppSettings>): Promise<void> {
  const db = await getDb();
  const current = await dbGetSettings();
  await db.put("settings", JSON.stringify({ ...current, ...settings }), SETTINGS_KEY);
}

// ── File System Handles ────────────────────────────────────────────────────

export async function dbSaveDirectoryHandle(
  projectId: string,
  handle: FileSystemDirectoryHandle
): Promise<void> {
  const db = await getDb();
  await db.put("fileHandles", handle, projectId);
}

export async function dbGetDirectoryHandle(
  projectId: string
): Promise<FileSystemDirectoryHandle | undefined> {
  const db = await getDb();
  return db.get("fileHandles", projectId);
}

// ── GCP Key (base64로 settings에 저장) ────────────────────────────────────

export async function dbSaveGcpKey(jsonContent: string): Promise<void> {
  const db = await getDb();
  const current = await dbGetSettings();
  await db.put(
    "settings",
    JSON.stringify({ ...current, gcp_key_json: jsonContent, gcp_key_configured: true }),
    SETTINGS_KEY
  );
}

export async function dbGetGcpKey(): Promise<string | null> {
  const settings = await dbGetSettings();
  return (settings as unknown as Record<string, string>)["gcp_key_json"] ?? null;
}

// ── TTS Audio ─────────────────────────────────────────────────────────────

export async function dbSaveTtsAudio(projectId: string, sceneId: number, blob: Blob): Promise<void> {
  const db = await getDb();
  await db.put("ttsAudio", blob, `${projectId}:${sceneId}`);
}

export async function dbGetTtsAudio(projectId: string, sceneId: number): Promise<Blob | undefined> {
  const db = await getDb();
  return db.get("ttsAudio", `${projectId}:${sceneId}`);
}

export async function dbDeleteTtsAudio(projectId: string, sceneId: number): Promise<void> {
  const db = await getDb();
  await db.delete("ttsAudio", `${projectId}:${sceneId}`);
}

/**
 * 특정 projectId에 해당하는 모든 ttsAudio 키를 반환한다.
 * 키 형식: `${projectId}:${sceneId}`
 */
export async function dbGetAllTtsAudioKeysForProject(projectId: string): Promise<string[]> {
  const db = await getDb();
  const prefix = `${projectId}:`;
  const all = await db.getAllKeys("ttsAudio");
  return (all as string[]).filter((k) => k.startsWith(prefix));
}

export async function dbGetTtsAudioByKey(key: string): Promise<Blob | undefined> {
  const db = await getDb();
  return db.get("ttsAudio", key);
}

export async function dbSaveTtsAudioByKey(key: string, blob: Blob): Promise<void> {
  const db = await getDb();
  await db.put("ttsAudio", blob, key);
}

// ── Overlay Presets ────────────────────────────────────────────────────────

/** 저장된 프리셋 전체를 created_at 내림차순으로 반환 */
export async function dbListOverlayPresets(): Promise<TextOverlayPreset[]> {
  const db = await getDb();
  const all = await db.getAll("overlayPresets");
  return all.sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/** 프리셋을 저장(신규 추가 또는 덮어쓰기) */
export async function dbSaveOverlayPreset(preset: TextOverlayPreset): Promise<void> {
  const db = await getDb();
  await db.put("overlayPresets", preset);
}

/** 프리셋 삭제 */
export async function dbDeleteOverlayPreset(id: string): Promise<void> {
  const db = await getDb();
  await db.delete("overlayPresets", id);
}

// ── User Scene Tags (#214) ─────────────────────────────────────────────────

/**
 * 유저 정의 씬 태그를 updated_at 내림차순으로 반환한다.
 * 빌트인 사전은 별도로 관리되며 본 함수는 IndexedDB에 저장된 유저 정의만 반환한다.
 * 머지 책임은 `sceneTags.ts loadTagDictionary()`에 있다.
 */
export async function dbListUserSceneTags(): Promise<UserSceneTag[]> {
  const db = await getDb();
  const all = await db.getAll("userSceneTags");
  return all.sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
}

/** 특정 id의 유저 정의 태그를 조회한다. 없으면 null. */
export async function dbGetUserSceneTag(id: string): Promise<UserSceneTag | null> {
  const db = await getDb();
  const result = await db.get("userSceneTags", id);
  return result ?? null;
}

/**
 * 유저 정의 태그 저장 (신규 추가 또는 덮어쓰기).
 * created_at은 신규 저장 시 자동 부여, updated_at은 매번 갱신.
 */
export async function dbSaveUserSceneTag(tag: UserSceneTag): Promise<void> {
  const db = await getDb();
  const now = new Date().toISOString();
  const existing = await db.get("userSceneTags", tag.id);
  const merged: UserSceneTag = {
    ...tag,
    created_at: existing?.created_at ?? tag.created_at ?? now,
    updated_at: now,
  };
  await db.put("userSceneTags", merged);
}

/** 유저 정의 태그 삭제. 빌트인 태그는 IndexedDB에 없으므로 자동으로 영향 없음. */
export async function dbDeleteUserSceneTag(id: string): Promise<void> {
  const db = await getDb();
  await db.delete("userSceneTags", id);
}

// ── Local Thumbnail Cache (#272) ──────────────────────────────────────────
// 사용자 PC에만 있는 footage 파일은 서버 썸네일 API를 사용할 수 없으므로
// 브라우저에서 video → canvas로 첫 프레임을 직접 추출해 캐시한다.
// 키 형식은 `${filename}:${lastModified}` — 파일이 교체되면 자동 무효화된다.

export async function dbGetLocalThumbnail(key: string): Promise<string | null> {
  const db = await getDb();
  const record = await db.get("localThumbnailCache", key);
  return record?.dataUrl ?? null;
}

export async function dbSaveLocalThumbnail(key: string, dataUrl: string): Promise<void> {
  const db = await getDb();
  const record: LocalThumbnailRecord = {
    key,
    dataUrl,
    created_at: new Date().toISOString(),
  };
  await db.put("localThumbnailCache", record);
}

// ── Audio Waveform Cache (#312) ───────────────────────────────────────────
// BGM/오디오 자산의 파형 피크를 IndexedDB에 캐시한다. 같은 자산을 다시 열어도
// AudioContext.decodeAudioData 를 반복하지 않아 CPU/메모리 절약.
//
// 키 설계: `${projectId}:${assetId}:${peakCount}`
//   - projectId 포함 — 프로젝트 삭제 시 일괄 정리(prefix scan) 가능.
//   - assetId 포함 — 자산 교체(드물지만 같은 파일을 다른 이름으로 재업로드 등) 시
//     새 asset_id 가 부여되므로 옛 캐시는 자연 사장.
//   - peakCount 포함 — 같은 자산을 다른 캔버스 폭으로 다시 그릴 때도 캐시 적중.

export function audioWaveformCacheKey(
  projectId: string,
  assetId: string,
  peakCount: number,
): string {
  return `${projectId}:${assetId}:${Math.max(1, Math.floor(peakCount))}`;
}

export async function dbGetAudioWaveform(
  projectId: string,
  assetId: string,
  peakCount: number,
): Promise<AudioWaveformRecord | undefined> {
  const db = await getDb();
  return db.get("audioWaveformCache", audioWaveformCacheKey(projectId, assetId, peakCount));
}

export async function dbSaveAudioWaveform(
  projectId: string,
  assetId: string,
  peakCount: number,
  data: { peaks: number[]; duration: number; sample_rate: number; channels: number },
): Promise<void> {
  const db = await getDb();
  const record: AudioWaveformRecord = {
    key: audioWaveformCacheKey(projectId, assetId, peakCount),
    peaks: data.peaks,
    duration: data.duration,
    sample_rate: data.sample_rate,
    channels: data.channels,
    created_at: new Date().toISOString(),
  };
  await db.put("audioWaveformCache", record);
}

/** 특정 프로젝트의 모든 파형 캐시를 prefix scan 으로 삭제(프로젝트 삭제 시 호출용). */
export async function dbDeleteAudioWaveformsForProject(projectId: string): Promise<void> {
  const db = await getDb();
  const prefix = `${projectId}:`;
  const all = (await db.getAllKeys("audioWaveformCache")) as string[];
  await Promise.all(
    all
      .filter((k) => k.startsWith(prefix))
      .map((k) => db.delete("audioWaveformCache", k)),
  );
}

// ── NL Edit Chat History (#316) ───────────────────────────────────────────
// 자연어 편집 패널의 채팅 히스토리를 프로젝트별로 IndexedDB에 보관한다.
// - 세션 간 유지: 새로고침 후에도 직전 대화가 그대로 살아 있어 워크플로 단절이 없다.
// - 프로젝트 격리: 키 prefix `${projectId}:` 로 다른 프로젝트 대화와 섞이지 않는다.
// - 정렬: turn_id 가 ISO 타임스탬프 기반이라 사전 비교만으로 시간순 정렬이 가능하다.

export function nlEditChatKey(projectId: string, turnId: string): string {
  return `${projectId}:${turnId}`;
}

/** 새 턴 ID 생성 — ISO 타임스탬프 + 4자 랜덤. 마이크로초 동시 입력에도 충돌 회피. */
export function newNlEditTurnId(): string {
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const rand = Math.random().toString(36).slice(2, 6);
  return `${ts}_${rand}`;
}

/** 특정 프로젝트의 채팅 히스토리를 turn_id 오름차순(=시간순)으로 반환. */
export async function dbListNlEditChat(
  projectId: string,
): Promise<NlEditChatTurnRecord[]> {
  const db = await getDb();
  const prefix = `${projectId}:`;
  const all = await db.getAll("nlEditChat");
  return all
    .filter((r) => r.project_id === projectId || r.key.startsWith(prefix))
    .sort((a, b) => a.turn_id.localeCompare(b.turn_id));
}

/** 채팅 1턴 저장(신규 추가 또는 덮어쓰기). 적용/거부/되돌리기 상태 갱신도 동일 함수. */
export async function dbSaveNlEditChatTurn(
  record: NlEditChatTurnRecord,
): Promise<void> {
  const db = await getDb();
  await db.put("nlEditChat", record);
}

/** 단일 턴 삭제 — pending 메시지를 실패로 마감하거나 사용자가 명시적으로 지울 때 사용. */
export async function dbDeleteNlEditChatTurn(
  projectId: string,
  turnId: string,
): Promise<void> {
  const db = await getDb();
  await db.delete("nlEditChat", nlEditChatKey(projectId, turnId));
}

/** 프로젝트 단위 전체 히스토리 삭제 — 프로젝트 삭제 시/사용자가 "대화 비우기" 클릭 시. */
export async function dbClearNlEditChat(projectId: string): Promise<void> {
  const db = await getDb();
  const prefix = `${projectId}:`;
  const all = (await db.getAllKeys("nlEditChat")) as string[];
  await Promise.all(
    all
      .filter((k) => k.startsWith(prefix))
      .map((k) => db.delete("nlEditChat", k)),
  );
}

// ── Scene History (Undo/Redo patches, #369) ───────────────────────────────
// Undo/Redo 메모리 모델(base 1개 + forward delta 배열)을 IndexedDB 에 그대로 영속화.
// 키 prefix 로 base / patch 두 종류 레코드를 같은 store 에 보관한다.
//   - `${projectId}:base`        → SceneHistoryBaseRecord
//   - `${projectId}:p:${zseq}`   → SceneHistoryPatchRecord, zseq 는 zero-padded seq.
// 다른 프로젝트의 patch 혼입 방지: prefix scan + record.project_id 추가 검증.

/** patch 레코드 정렬용 zero-padded 시퀀스 문자열(8자리). */
function _zseq(seq: number): string {
  return Math.max(0, Math.floor(seq)).toString().padStart(8, "0");
}

const SCENE_HISTORY_TTL_MS = 7 * 24 * 60 * 60 * 1000; // #369 — 7일 자동 정리.

/** base 레코드 저장(또는 갱신). */
export async function dbSaveSceneHistoryBase(
  projectId: string,
  base: Project,
): Promise<void> {
  const db = await getDb();
  const record: SceneHistoryBaseRecord = {
    key: `${projectId}:base`,
    project_id: projectId,
    kind: "base",
    base,
    updated_at: new Date().toISOString(),
  };
  await db.put("sceneHistory", record);
}

/** 새 patch append (commit 직후 호출). */
export async function dbAppendSceneHistoryPatch(
  projectId: string,
  seq: number,
  patch: unknown,
): Promise<void> {
  const db = await getDb();
  const record: SceneHistoryPatchRecord = {
    key: `${projectId}:p:${_zseq(seq)}`,
    project_id: projectId,
    kind: "patch",
    seq,
    patch,
    created_at: new Date().toISOString(),
  };
  await db.put("sceneHistory", record);
}

/** seq 가 cutoff 미만인 patch 들 삭제 — base 흡수 후 잔존 정리용. */
export async function dbDeleteSceneHistoryPatchesBelow(
  projectId: string,
  cutoffSeq: number,
): Promise<void> {
  const db = await getDb();
  const prefix = `${projectId}:p:`;
  const all = (await db.getAllKeys("sceneHistory")) as string[];
  await Promise.all(
    all
      .filter((k) => k.startsWith(prefix))
      .filter((k) => {
        const tail = k.slice(prefix.length);
        const n = parseInt(tail, 10);
        return !Number.isNaN(n) && n < cutoffSeq;
      })
      .map((k) => db.delete("sceneHistory", k)),
  );
}

/**
 * seq 가 cutoff 이상인 patch 들 삭제 — 새 분기 시작 시 옛 분기 정리용 (#372).
 *
 * 시나리오: undo 후 새 commit → 메모리 redoDeltas 가 비워지면서 새 분기가 시작된다.
 * 이때 IDB 에는 옛 분기의 patch 들이 그대로 남아 있어 새로고침 후 복원 시 잘못된
 * history 가 재구성된다(이슈 #372). 새 patch append 직전에 새 baseSeq 이상의 옛
 * patch 를 모두 삭제해야 한다.
 *
 * base 레코드는 건드리지 않는다(base 는 분기와 무관한 가장 오래된 출발점).
 */
export async function dbDeleteSceneHistoryPatchesAtOrAbove(
  projectId: string,
  cutoffSeq: number,
): Promise<void> {
  const db = await getDb();
  const prefix = `${projectId}:p:`;
  const all = (await db.getAllKeys("sceneHistory")) as string[];
  await Promise.all(
    all
      .filter((k) => k.startsWith(prefix))
      .filter((k) => {
        const tail = k.slice(prefix.length);
        const n = parseInt(tail, 10);
        return !Number.isNaN(n) && n >= cutoffSeq;
      })
      .map((k) => db.delete("sceneHistory", k)),
  );
}

/**
 * 현재 IDB 에 영속화된 patch seq 목록을 반환(정렬됨).
 *
 * 분기 발생 시 메모리 모델과 1:1 동기화를 위해 사용한다(#372).
 */
export async function dbListSceneHistoryPatchSeqs(
  projectId: string,
): Promise<number[]> {
  const db = await getDb();
  const prefix = `${projectId}:p:`;
  const all = (await db.getAllKeys("sceneHistory")) as string[];
  const seqs: number[] = [];
  for (const k of all) {
    if (!k.startsWith(prefix)) continue;
    const tail = k.slice(prefix.length);
    const n = parseInt(tail, 10);
    if (!Number.isNaN(n)) seqs.push(n);
  }
  seqs.sort((a, b) => a - b);
  return seqs;
}

/**
 * 메모리 undoDeltas 와 1:1 대응되는 seq 집합만 남기고 나머지 patch 를 삭제(#372).
 *
 * 새 분기 시작 시 옛 buried patch 정리 + 안전한 회귀 방지. base 레코드는 건드리지 않는다.
 * keep 집합이 비어 있으면 모든 patch 를 삭제한다.
 */
export async function dbKeepOnlySceneHistoryPatchSeqs(
  projectId: string,
  keep: number[],
): Promise<void> {
  const db = await getDb();
  const prefix = `${projectId}:p:`;
  const keepSet = new Set<number>(keep);
  const all = (await db.getAllKeys("sceneHistory")) as string[];
  await Promise.all(
    all
      .filter((k) => k.startsWith(prefix))
      .filter((k) => {
        const tail = k.slice(prefix.length);
        const n = parseInt(tail, 10);
        if (Number.isNaN(n)) return false;
        return !keepSet.has(n);
      })
      .map((k) => db.delete("sceneHistory", k)),
  );
}

/** 특정 프로젝트의 전체 sceneHistory 삭제(프로젝트 삭제 시 / clearHistory 시). */
export async function dbClearSceneHistory(projectId: string): Promise<void> {
  const db = await getDb();
  const prefix = `${projectId}:`;
  const all = (await db.getAllKeys("sceneHistory")) as string[];
  await Promise.all(
    all
      .filter((k) => k.startsWith(prefix))
      .map((k) => db.delete("sceneHistory", k)),
  );
}

/**
 * 프로젝트의 sceneHistory 를 모두 가져온다 — base + seq 정렬된 patch 배열.
 *
 * - 다른 프로젝트의 레코드가 혼입되지 않도록 prefix + project_id 이중 검증.
 * - 7일 이상 된 patch 는 자동 폐기(반환 전 store 에서도 삭제). base 도 7일 이상 묵으면
 *   "복원 불가능" 으로 간주해 전체 폐기(부분 복원하면 base 와 patch 의 일관성이 깨질 수 있음).
 */
export async function dbLoadSceneHistory(
  projectId: string,
): Promise<{ base: Project | null; patches: unknown[] }> {
  const db = await getDb();
  const prefix = `${projectId}:`;
  const all = await db.getAll("sceneHistory");
  const mine = all.filter((r) => r.project_id === projectId || r.key.startsWith(prefix));
  const now = Date.now();
  let baseRec: SceneHistoryBaseRecord | undefined;
  const patchRecs: SceneHistoryPatchRecord[] = [];
  for (const r of mine) {
    if (r.kind === "base") {
      baseRec = r;
    } else if (r.kind === "patch") {
      patchRecs.push(r);
    }
  }
  // base 가 7일 이상 묵었다면 history 전체 폐기.
  if (baseRec) {
    const t = Date.parse(baseRec.updated_at);
    if (!Number.isFinite(t) || now - t > SCENE_HISTORY_TTL_MS) {
      await dbClearSceneHistory(projectId);
      return { base: null, patches: [] };
    }
  }
  // 7일 이상 된 patch 만 부분 삭제 (base 보다 새로운 부분만 보존).
  const validPatches: SceneHistoryPatchRecord[] = [];
  for (const r of patchRecs) {
    const t = Date.parse(r.created_at);
    if (!Number.isFinite(t) || now - t > SCENE_HISTORY_TTL_MS) {
      await db.delete("sceneHistory", r.key);
    } else {
      validPatches.push(r);
    }
  }
  validPatches.sort((a, b) => a.seq - b.seq);
  return {
    base: baseRec?.base ?? null,
    patches: validPatches.map((r) => r.patch),
  };
}
