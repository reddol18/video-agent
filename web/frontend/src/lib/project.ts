/**
 * 프로젝트 CRUD — IndexedDB 기반 (백엔드 API 대체)
 */
import { v4 as uuidv4 } from "uuid";
import type { Project, SceneCard, FootageAsset, CropRect, NarrationSegment, FootageItem } from "../types";
import {
  dbGetProject as _dbGetProjectRaw,
  dbSaveProject as _dbSaveProjectRaw,
  dbDeleteProject,
  dbListProjects as _dbListProjectsRaw,
  dbGetAllTtsAudioKeysForProject,
  dbGetTtsAudioByKey,
  dbSaveTtsAudioByKey,
  dbDeleteTtsAudio,
} from "./db";
import {
  getSceneSpeedSpec,
  isRampActive,
  outputToSourceTime,
  solveRampSpeedForDuration,
  sourceToOutputTime,
  speedOutputDuration,
} from "./speedRamp";

/**
 * 옛 IndexedDB 데이터(force_sibling_dissolve) → 새 필드(force_split_scene_dissolve) 마이그레이션.
 * "스플릿 씬" 명명 통일(자매→스플릿) 이전에 저장된 Project 객체는 옛 키만 들고 있으므로,
 * dbGetProject/dbListProjects 결과를 새 키로 변환하고, dbSaveProject 직전에는 옛 키를 제거한다.
 *
 * 양방향(읽기/쓰기) 모두 적용해 IndexedDB의 옛 키 잔재가 점진적으로 해소되도록 한다.
 */
function _migrateLegacyFields<T = unknown>(p: T): T {
  if (!p || typeof p !== "object") return p;
  const obj = p as { [k: string]: unknown };
  if (obj.force_sibling_dissolve !== undefined && obj.force_split_scene_dissolve === undefined) {
    obj.force_split_scene_dissolve = obj.force_sibling_dissolve;
  }
  // 옛 키는 항상 제거 — 직렬화 시 옛/새가 공존해 서버 alias 매핑이 흔들리지 않도록.
  if (obj.force_sibling_dissolve !== undefined) {
    delete obj.force_sibling_dissolve;
  }
  return p;
}

async function dbGetProject(id: string): Promise<Project | undefined> {
  const p = await _dbGetProjectRaw(id);
  return p ? _migrateLegacyFields(p) : p;
}

async function dbSaveProject(project: Project): Promise<void> {
  // 저장 직전에 한 번 더 마이그레이션 — 옛 키가 어디선가 새어 들어와도 IndexedDB에는 새 키만 남는다.
  await _dbSaveProjectRaw(_migrateLegacyFields(project));
}

async function dbListProjects(): Promise<Project[]> {
  const list = await _dbListProjectsRaw();
  return list.map((p) => _migrateLegacyFields(p));
}

export async function listProjects(): Promise<Project[]> {
  // 로컬(IndexedDB)과 서버 목록을 병합한다. origin/기기가 달라도 서버의 프로젝트가
  // 모두 보이도록 하기 위함(#293 getProject의 서버 fallback 정책과 일관).
  const local = await dbListProjects().catch(() => [] as Project[]);
  let server: Project[] = [];
  try {
    const { listProjects: apiListProjects } = await import("../api/client");
    server = await apiListProjects();
  } catch {
    // 서버 미응답(오프라인 등) — 로컬만 반환
    return local;
  }
  // project_id 기준 병합. 서버를 진실의 원천으로 삼되, 로컬에만 있는 항목은 포함한다.
  // name처럼 한쪽이 비어있을 수 있는 필드는 '비어있지 않은 쪽'을 택한다(과도기 데이터 보정).
  const byId = new Map<string, Project>();
  for (const p of server) byId.set(p.project_id, p);
  for (const lp of local) {
    const sp = byId.get(lp.project_id);
    if (!sp) {
      byId.set(lp.project_id, lp); // 로컬 전용
    } else {
      // 서버 우선이되, 서버 name이 비었으면 로컬 name으로 보완
      byId.set(lp.project_id, { ...sp, name: sp.name?.trim() ? sp.name : lp.name });
    }
  }
  return Array.from(byId.values());
}

/**
 * 프로젝트 조회 — IndexedDB 우선, 없으면 서버 fallback.
 *
 * 정책(#293): 서버를 source of truth로 삼는다. 새 브라우저/시크릿 모드처럼
 * IndexedDB가 비어 있는 환경에서 `?screen=editor&pid=...`로 직접 진입해도
 * 서버 `/api/projects/{id}`에서 프로젝트를 받아와 화면을 정상 렌더한다.
 * 받아온 프로젝트는 IndexedDB에도 저장하여 이후 오프라인/로컬 조작이 가능하게 한다.
 *
 * 주의: 서버 응답은 scenes/studio_assets 등 전체 Project 스키마를 포함하지만,
 * TTS 오디오 blob은 IndexedDB에만 존재하므로 이 경로로 복원되지 않는다.
 * 이 함수의 책임은 "씬 카드가 보이게" 하는 메타데이터 복원까지이며, TTS 오디오는
 * 서버 스트림(ttsAudioUrl)이나 재생성 흐름에서 별도로 다룬다.
 *
 * @throws 서버에서도 찾지 못하면(404 등) "프로젝트를 찾을 수 없습니다" throw.
 */
/** 의미있는 차이만 반환 — updated_at·created_at·cost_estimate 같은 자동 갱신/노이즈 필드 제외. */
function _diffMeaningfulFields(a: Project, b: Project): string[] {
  const fields: { key: keyof Project; label: string }[] = [
    { key: "name", label: "이름" },
    { key: "scenes", label: "씬" },
    { key: "studio_assets", label: "footage 자산" },
    { key: "bgm_tracks", label: "BGM" },
    { key: "scenario_text", label: "시나리오" },
    { key: "tts_voice", label: "TTS 음성" },
    { key: "output_width", label: "해상도" },
    { key: "output_height", label: "해상도" },
    { key: "bg_color", label: "배경색" },
    { key: "footage_dir", label: "footage 폴더" },
  ];
  const out = new Set<string>();
  for (const { key, label } of fields) {
    if (JSON.stringify(a[key] ?? null) !== JSON.stringify(b[key] ?? null)) {
      out.add(label);
    }
  }
  return Array.from(out);
}

export async function getProject(id: string, opts: { checkConflict?: boolean } = {}): Promise<Project> {
  // 웹서비스 정책: 서버가 단일 진실의 원천. IndexedDB 는 오프라인 폴백 캐시일 뿐.
  // #413 — 로컬 vs 서버 충돌 모달 제거. 사용자 명시 요청.
  //   서버가 진실이라면 차이가 있어도 항상 서버를 따라야 한다. 모달이 노출되면
  //   사용자가 "로컬 보존" 을 선택하는 회피 경로가 생기고, 그게 진짜 회귀의 우회로가
  //   되어 근본 문제 (서버에 데이터가 안 가는 race) 가 숨겨진다.
  //   opts.checkConflict 인자는 호출처 호환을 위해 받되 무시한다.
  void opts;
  let serverProject: Project | undefined;
  try {
    const { getProject: fetchServerProject } = await import("../api/client");
    serverProject = await fetchServerProject(id);
  } catch {
    /* 서버 미응답 */
  }

  // 서버 미응답 → 로컬 폴백 (오프라인 작업 보호 — 이 분기만 IndexedDB 사용).
  if (!serverProject || serverProject.project_id !== id) {
    const local = await dbGetProject(id).catch(() => undefined);
    if (local) return _withDedupedStudioAssets(local);
    throw new Error("프로젝트를 찾을 수 없습니다");
  }

  // 서버 최신본을 IndexedDB에 미러링(편집 캐시·오프라인 대비)
  // #381 — 서버 데이터에 asset_id 중복이 남아있을 수 있으므로(이전 회귀 흔적)
  // 한 번 정리해 IndexedDB·반환값 양쪽에 모두 단일 항목만 남도록 자동 복구한다.
  const cleaned = _withDedupedStudioAssets(serverProject);
  try { await dbSaveProject(cleaned); } catch { /* ignore */ }
  return cleaned;
}

/**
 * #381 — Project 객체에 dedupeStudioAssets 를 적용한 얕은 복사본을 반환.
 * studio_assets 가 비어 있거나 중복이 없으면 원본을 그대로 돌려준다(불필요한 복사 방지).
 */
function _withDedupedStudioAssets(p: Project): Project {
  if (!p.studio_assets || p.studio_assets.length === 0) return p;
  const cleaned = dedupeStudioAssets(p.studio_assets);
  if (cleaned.length === p.studio_assets.length) {
    const same = cleaned.every((a, i) => a.asset_id === p.studio_assets[i].asset_id);
    if (same) return p;
  }
  return { ...p, studio_assets: cleaned };
}

export async function createProject(
  name: string,
  scenarioText = "",
  footageDir = "",
  series = ""
): Promise<Project> {
  const now = new Date().toISOString();
  const project: Project = {
    project_id: uuidv4(),
    name,
    // 프로젝트 그룹(시리즈)명. "" 이면 미편성 — 서버 register 가 그대로 반영한다.
    series: series.trim(),
    footage_dir: footageDir,
    scenario_text: scenarioText,
    status: "setup",
    scenes: [],
    tts_voice: "ko-KR-Chirp3-HD-Orus",
    created_at: now,
    updated_at: now,
    studio_assets: [],
    default_crop: null,
    output_width: 1920,
    output_height: 1080,
    bg_color: "black",
  };
  await dbSaveProject(project);
  // 서버가 진실의 원천(웹서비스 전환) — 생성 즉시 서버에 메타 등록.
  // 서버 실패해도 로컬은 저장돼 편집은 가능(다음 동기화 때 반영).
  try {
    const { pushProjectState } = await import("../api/client");
    await pushProjectState(project.project_id, project);
  } catch (e) {
    console.warn("[createProject] 서버 등록 실패(로컬에는 저장됨):", e);
  }
  return project;
}

export async function updateProject(
  id: string,
  patch: Partial<Omit<Project, "project_id" | "created_at">>
): Promise<Project> {
  const p = await getProject(id);
  const updated = { ...p, ...patch, updated_at: new Date().toISOString() };
  await dbSaveProject(updated);
  // 메타 변경(이름 등)을 서버에도 반영 — 서버가 진실의 원천.
  try {
    const { pushProjectState } = await import("../api/client");
    await pushProjectState(id, updated);
  } catch (e) {
    console.warn("[updateProject] 서버 동기화 실패(로컬에는 저장됨):", e);
  }
  return updated;
}

export async function deleteProject(id: string): Promise<void> {
  await dbDeleteProject(id);
}

/**
 * 프로젝트 복제 — 서버가 진실의 출처. 서버에서 새 project_id로 state를 복제하고,
 * footage_dir은 원본과 동일 경로를 공유한다(파일 미복사).
 * 클라이언트는 서버 응답을 IndexedDB에 저장하고 TTS blob만 새 키로 복사한다.
 *
 * 복제 직전에 IndexedDB의 최신 state(scenes/studio_assets/bgm_tracks 등)를
 * 서버에 푸시(register)해서 양쪽을 일치시킨 뒤 복제한다.
 */
export async function duplicateProject(id: string): Promise<Project> {
  const original = await getProject(id);
  if (original) {
    // 동적 import — 순환 의존성 회피
    const { pushProjectState } = await import("../api/client");
    await pushProjectState(id, original).catch((e) => {
      console.warn("복제 전 서버 동기화 실패 — 서버 state로 복제 진행", e);
    });
  }

  const res = await fetch(`/api/projects/${id}/duplicate`, { method: "POST" });
  if (!res.ok) {
    throw new Error(`프로젝트 복제 실패 (HTTP ${res.status})`);
  }
  const duplicated: Project = await res.json();
  await dbSaveProject(duplicated);

  // TTS 오디오 blob 복사 — `{originalId}:{sceneId}` → `{newId}:{sceneId}`
  const ttsKeys = await dbGetAllTtsAudioKeysForProject(id);
  await Promise.all(
    ttsKeys.map(async (key) => {
      const blob = await dbGetTtsAudioByKey(key);
      if (!blob) return;
      const sceneIdPart = key.slice(id.length + 1);
      const newKey = `${duplicated.project_id}:${sceneIdPart}`;
      await dbSaveTtsAudioByKey(newKey, blob);
    })
  );

  return duplicated;
}

// ── Scene 조작 ────────────────────────────────────────────────────────────

export async function updateScene(
  projectId: string,
  sceneId: number,
  patch: Partial<SceneCard>
): Promise<Project> {
  const p = await getProject(projectId);
  const scenes = p.scenes.map((s) =>
    s.scene_id === sceneId ? { ...s, ...patch } : s
  );
  return updateProject(projectId, { scenes });
}

export async function setScenes(projectId: string, scenes: SceneCard[]): Promise<Project> {
  return updateProject(projectId, { scenes });
}

export async function deleteScene(projectId: string, sceneId: number): Promise<Project> {
  const p = await getProject(projectId);
  const scenes = p.scenes.filter((s) => s.scene_id !== sceneId);
  await dbDeleteTtsAudio(projectId, sceneId);
  // 잔재 정리 — 활성 scene_id 외의 TTS blob 키를 일괄 삭제(옛 미정리 키 회수).
  try {
    const _active = new Set(scenes.map((s) => s.scene_id));
    const _keys = await dbGetAllTtsAudioKeysForProject(projectId);
    for (const _k of _keys) {
      const _sidStr = _k.slice(projectId.length + 1);
      const _sid = Number(_sidStr);
      if (Number.isFinite(_sid) && !_active.has(_sid)) {
        await dbDeleteTtsAudio(projectId, _sid).catch(() => {});
      }
    }
  } catch {
    // 정리 실패는 삭제 흐름을 막지 않는다.
  }
  return updateProject(projectId, { scenes });
}

/**
 * 모든 씬을 한 번에 삭제한다. TTS 오디오도 함께 정리한다.
 * 복구 불가능한 작업이므로 호출 전에 사용자 확인이 필요하다.
 */
export async function deleteAllScenes(projectId: string): Promise<Project> {
  const p = await getProject(projectId);
  await Promise.all(p.scenes.map((s) => dbDeleteTtsAudio(projectId, s.scene_id)));
  return updateProject(projectId, { scenes: [] });
}

// ── 씬 추가 / 사이 삽입 / 복제 (#245) ────────────────────────────────────
//
// 신규 scene_id는 항상 기존 scenes의 max(scene_id) + 1로 발급한다.
// 빈 배열인 경우 1부터 시작.
function nextSceneId(scenes: SceneCard[]): number {
  if (scenes.length === 0) return 1;
  return Math.max(...scenes.map((s) => s.scene_id)) + 1;
}

// 빈 SceneCard 템플릿. 모든 효과 필드(#197/#205/#206/#207/#208 추가분 포함)를
// 빈 배열로 초기화하여 추후 효과 추가 시 undefined 가드가 필요 없도록 한다.
function buildEmptyScene(sceneId: number): SceneCard {
  return {
    scene_id: sceneId,
    title: "",
    description: "",
    mood: "",
    keywords: [],
    duration_hint_sec: 3.0,
    narration_text: "",
    footage: null,
    match: null,
    tts: null,
    text_overlays: [],
    footage_speed: 1.0,
    tts_speed: 1.0,
    shape_overlays: [],
    graphic_overlays: [],
    filters: [],
    motions: [],
    image_overlays: [],
    tags: [],
    // #382 — 신규 씬은 빈 narration_segments 로 시작.
    narration_segments: [],
  };
}


// ── #382 — narration_segments 헬퍼 ──────────────────────────────────────
// 씬 1개에 여러 segment(TTS 구간)를 가진다. seg_0 은 시나리오 파서가 만든 첫 segment 로
// 씬 길이 자동 연장이 유지되고, 그 외 segment 는 offset_sec 기준 배치.

/** 씬에서 다음 segment id 발급 (seg_0, seg_1, ...). 기존 id 와 충돌하지 않는 최소 정수. */
export function nextNarrationSegmentId(segments: NarrationSegment[] | undefined): string {
  const existing = new Set((segments ?? []).map((s) => s.id));
  let n = 0;
  while (existing.has(`seg_${n}`)) n += 1;
  return `seg_${n}`;
}

/** 씬의 시각 길이를 계산한다 (#382 overflow 클램프용, #388 통합, #397 ③ 복원).
 *
 *  단일 진실의 출처(single source of truth) — TimelineBar 의 `getSceneDurationSec`,
 *  인스펙터의 segment overflow 경고 모두 이 함수만 사용한다.
 *
 *  우선순위 (TimelineBar #375/#378 정책과 일치):
 *    ① cutDurations(speed===1.0) — Phase 5 가 확정한 cache/timeline.json cut 길이.
 *       단 footage_speed != 1.0 이면 옛 settings 의 결과라 ②~④ 폴백.
 *    ② footage(clip).clip_end_sec - clip_start_sec  (footage trim, /speed) — clip 타입만.
 *       TTS 가 더 길면 max(trim, tts_seg0, hint) 결합 (Phase 5 Time Warp 가 footage 를 늘림).
 *    ③ footage(clip) 원본 duration (clip_end_sec=null 이고 footageItems 로 조회 가능할 때).
 *       #394 에서 한 차례 제거했으나 사용자 의도와 달라 #397 로 복원.
 *       → "16번 씬이 5초라면 잘못된 거야. 16번 씬은 34.8초가 맞아." (clip_end_sec=null + 원본 34.75s).
 *    ④ max(TTS seg_0, hint, 1.0) — image/kenburns 씬 또는 footage 정보 부재 시 폴백.
 *
 *  옵셔널 인자(footageItems/cutDurations)가 없으면 ②~③ 을 건너뛰고 ④ 만 사용한다.
 *  → 기존 무인자 호출처(예: 단순 overflow 판정)는 회귀 없이 그대로 동작.
 *
 *  주의: 최소값 1.0 폴백은 ④ 경로에만 적용한다. cut/trim/원본 footage 가 1.0s 미만이어도
 *        실제 footage 길이를 그대로 반환해야 segment offset/마커 위치가 정확하다.
 */
export function computeSceneVisualDuration(
  scene: SceneCard,
  footageItems?: ReadonlyArray<FootageItem> | ReadonlyMap<string, FootageItem>,
  cutDurations?: ReadonlyMap<number, number>,
): number {
  const segs = scene.narration_segments ?? [];
  const seg0 = segs.find((s) => s.id === "seg_0");
  // seg_0.duration_sec 가 우선(신규 데이터). 없으면 scene.tts.duration_sec(레거시 호환).
  //   legacy scene.tts 는 status === "ready" 일 때만 신뢰 (옛 #375 정책 유지 — generating/idle 무시).
  //   seg_0 는 #382 도입 후 status 와 무관하게 duration_sec 가 의미를 가진다.
  const seg0Dur = seg0?.duration_sec ?? 0;
  const legacyTtsDur =
    scene.tts?.status === "ready" && (scene.tts?.duration_sec ?? 0) > 0
      ? (scene.tts!.duration_sec as number)
      : 0;
  const ttsDurRaw = seg0Dur > 0 ? seg0Dur : legacyTtsDur;
  const tts0 = ttsDurRaw > 0 ? ttsDurRaw : 0;
  const hintRaw = scene.duration_hint_sec ?? 0;
  const hint = hintRaw > 0 ? hintRaw : 0;
  const speed = typeof scene.footage_speed === "number" && isFinite(scene.footage_speed) && scene.footage_speed > 0
    ? scene.footage_speed
    : 1.0;
  // 정속/가속(ramp) 모드 공용 출력 길이 계산 — lib/speedRamp 단일 헬퍼.
  const speedSpec = getSceneSpeedSpec(scene);

  // ① cache/timeline.json cut 길이 (Phase 5 산출물).
  //    speed != 1.0 인 경우 옛 settings 결과이므로 ②~④ 폴백.
  if (cutDurations && speed === 1.0) {
    const cut = cutDurations.get(scene.scene_id);
    if (typeof cut === "number" && isFinite(cut) && cut > 0) {
      return cut;
    }
  }

  const footage = scene.footage;
  // ② clip 타입 — footage 트림(clip_*_sec)이 명시되어 있을 때 footage 길이를 진실의 출처로 본다.
  //   TTS 와는 max 결합. hint 는 이 단계에서 제외 (footage 정보가 있으면 hint 무시).
  if (footage && footage.type === "clip") {
    const start = typeof footage.clip_start_sec === "number" && isFinite(footage.clip_start_sec)
      ? Math.max(0, footage.clip_start_sec)
      : 0;
    if (footage.clip_end_sec != null && isFinite(footage.clip_end_sec) && footage.clip_end_sec > start) {
      const trimDur = speedOutputDuration(footage.clip_end_sec - start, speedSpec);
      return Math.max(trimDur, tts0);
    }
    // ③ #397 — clip_end_sec 가 없으면 footage 원본 길이를 폴백으로 사용한다 (#394 의 제거를 revert).
    //   사용자 확인 결과: "16번 씬이 5초라면 그건 뭔가 잘못된 거야. 16번 씬은 34.8초가 맞아."
    //   → 사용자 의도상 footage 원본 길이가 진실의 출처이며, TTS/hint 는 footage 보다 길 때만
    //     footage 를 연장(Phase 5 Time Warp)한다. trim 이 명시되지 않은 미매칭 씬도 footage
    //     원본 그대로 표시되어야 시각 정합성이 유지된다.
    //   회귀 안전:
    //     - clip_*_sec 명시 씬: 위 분기에서 그대로 처리 (변경 없음).
    //     - footage.type='image'/'kenburns': 이 if 블록 자체에 진입하지 않음 (변경 없음).
    //     - footageItems 미제공: 폴백 ④ 로 합류 (외부에서 인자 안 주면 무시).
    if (footage.file && footageItems) {
      const origDur = _lookupFootageDuration(footage.file, footageItems);
      if (origDur > 0) {
        const remaining = speedOutputDuration(Math.max(0, origDur - start), speedSpec);
        return Math.max(remaining, tts0);
      }
    }
    // footage 원본 duration 미상 → ④ 폴백.
  }

  // ④ image/kenburns/기타 + footage 정보 부재 시: TTS / hint / 1.0.
  return Math.max(tts0, hint, 1.0);
}

/** filename 으로 FootageItem.duration_sec 조회. 배열/맵 모두 지원.
 *  #397 — #394 에서 제거되었던 헬퍼를 사용자 의도에 따라 복원. */
function _lookupFootageDuration(
  filename: string,
  items: ReadonlyArray<FootageItem> | ReadonlyMap<string, FootageItem>,
): number {
  if (items instanceof Map) {
    const it = items.get(filename);
    return it?.duration_sec && it.duration_sec > 0 ? it.duration_sec : 0;
  }
  if (Array.isArray(items)) {
    for (const it of items) {
      if (it.filename === filename) {
        return it.duration_sec && it.duration_sec > 0 ? it.duration_sec : 0;
      }
    }
  }
  return 0;
}

// ── 씬 영상 속도 자동 맞춤 (TTS 길이 기준) ────────────────────────────────
/** footage_speed 슬라이더 허용 범위 (Editor.tsx 슬라이더와 동일). */
export const FOOTAGE_SPEED_MIN = 0.25;
export const FOOTAGE_SPEED_MAX = 16.0;
/** 목표 영상 길이 = TTS 길이 + 이 여유(초). */
export const AUTO_SPEED_MARGIN_SEC = 1.5;

/** 씬의 TTS 길이(초)를 구한다. seg_0 우선, 없으면 legacy scene.tts(ready 한정). 없으면 0. */
export function getSceneTtsDuration(scene: SceneCard): number {
  const segs = scene.narration_segments ?? [];
  const seg0 = segs.find((s) => s.id === "seg_0");
  const seg0Dur = seg0?.duration_sec ?? 0;
  if (seg0Dur > 0) return seg0Dur;
  const legacy =
    scene.tts?.status === "ready" && (scene.tts?.duration_sec ?? 0) > 0
      ? (scene.tts!.duration_sec as number)
      : 0;
  return legacy > 0 ? legacy : 0;
}

/** 씬의 speed=1.0 기준 "원본(또는 트림) 영상 길이"(초)를 구한다.
 *  computeSceneVisualDuration 과 달리 speed 로 나누지 않은 순수 길이를 반환한다.
 *  clip 타입만 대상. clip_end_sec 트림이 있으면 트림 구간 길이, 없으면 원본 duration - clip_start_sec.
 *  image/kenburns 등 원본 길이 개념이 없거나 조회 불가하면 null. */
export function getSceneRawFootageDuration(
  scene: SceneCard,
  footageItems: ReadonlyArray<FootageItem> | ReadonlyMap<string, FootageItem>,
): number | null {
  const footage = scene.footage;
  if (!footage || footage.type !== "clip") return null;
  const start =
    typeof footage.clip_start_sec === "number" && isFinite(footage.clip_start_sec)
      ? Math.max(0, footage.clip_start_sec)
      : 0;
  if (
    footage.clip_end_sec != null &&
    isFinite(footage.clip_end_sec) &&
    footage.clip_end_sec > start
  ) {
    return footage.clip_end_sec - start;
  }
  if (footage.file) {
    const orig = _lookupFootageDuration(footage.file, footageItems);
    if (orig > 0) {
      const remaining = orig - start;
      return remaining > 0 ? remaining : null;
    }
  }
  return null;
}

/**
 * 씬 하나에 대해 "영상 재생 길이 >= TTS + 여유" 가 되도록 footage_speed 를 계산한다.
 * 순수 함수 — 상태를 바꾸지 않고 결과만 반환한다.
 *   - ttsDur <= 0 (TTS 없음)  → null (건너뜀)
 *   - footageDur 조회 불가(image/kenburns/미매칭) → null (건너뜀)
 *   - 현재 배속 적용 후 재생 길이가 이미 목표보다 길면 → null (건너뜀). 아래 "느리게만" 정책 참고.
 *   - 그 외: speed = footageDur / (ttsDur + margin), [0.25, max(1.0, 현재 배속)] 클램프.
 *     (2배속 씬이면 1.62배처럼 1.0 보다 클 수 있다 — 현재보다 느려지는 방향이므로 정책 위반 아님)
 *
 * ⚠ 느리게만 조절한다(현재 배속보다 빠르게 만들지 않음).
 *   목적은 "나레이션이 끝나기 전에 영상이 먼저 끝나버리는 것"을 막는 것이다.
 *   영상이 TTS 보다 이미 길면 그대로 두어야 한다 — 배속을 올려 억지로 줄이면
 *   멀쩡한 장면이 빨라져 부자연스러워지고, 원래 의도한 연출·트림이 깨진다.
 *   (사용자 지적: "TTS 보다 짧은 영상만 길이를 늘려줘야 하는데 긴 것도 짧게 만들어 버렸다")
 *   영상을 짧게 만들고 싶으면 배속이 아니라 트림(clip_start/end_sec)으로 자른다.
 */
export function computeAutoFootageSpeed(
  scene: SceneCard,
  footageItems: ReadonlyArray<FootageItem> | ReadonlyMap<string, FootageItem>,
  marginSec: number = AUTO_SPEED_MARGIN_SEC,
): number | null {
  const ttsDur = getSceneTtsDuration(scene);
  if (ttsDur <= 0) return null;
  const footageDur = getSceneRawFootageDuration(scene, footageItems);
  if (footageDur === null || footageDur <= 0) return null;
  const target = ttsDur + marginSec;
  if (target <= 0) return null;
  const spec = getSceneSpeedSpec(scene);
  // "이미 충분히 긴가" 는 원본 길이가 아니라 *현재 배속이 적용된* 재생 길이로 판단한다.
  //   원본 8.9초를 2배속으로 쓰면 실제 4.45초인데, 원본 길이로 보면 TTS 5.5초보다 길다고
  //   오판해 건너뛰던 버그(사용자 보고 2026-11-05).
  if (speedOutputDuration(footageDur, spec) >= target) return null;
  // 상한 = 현재 배속(1배 미만이면 1.0). 현재보다 빠르게는 만들지 않는다 — 느리게만 정책 유지.
  const hi = Math.max(1.0, spec.speed);
  if (spec.mode === "ramp" || spec.mode === "ramp_down") {
    // ramp/ramp_down 출력 길이는 S 에 단조감소 → [MIN, hi] 에서 이분탐색.
    return solveRampSpeedForDuration(footageDur, target, spec.hold, spec.accel, FOOTAGE_SPEED_MIN, hi, spec.mode, spec.base ?? 1);
  }
  return Math.max(FOOTAGE_SPEED_MIN, Math.min(hi, footageDur / target));
}

/**
 * footage_speed 변경 시 오버레이의 start_sec/end_sec 을 재조정하는 순수 헬퍼.
 *
 * 배경: 오버레이의 start_sec/end_sec 은 "속도가 반영된 출력(재생) 타임라인" 기준이다
 *   (백엔드 clip_editor 의 enable=/alpha 가 출력 t 사용, ffmpeg_mapper 의 setpts=(1/rate)*PTS).
 *   따라서 footage_speed 가 oldSpeed→newSpeed 로 바뀌면 이미 배치된 오버레이의 출력 시각도
 *   비례해서 옮겨야 씬 안에 그대로 남는다. 안 그러면 오버레이가 씬 밖으로 밀려나 사라진다.
 *
 *   factor = oldSpeed/newSpeed 를 각 오버레이의 start_sec/end_sec 에 곱한다
 *   (예: speed 1.0→4.0, start_sec 31.5 → 31.5*(1/4)=7.88).
 *
 * 대상 배열: text_overlays, shape_overlays, graphic_overlays, image_overlays, mosaic_overlays
 *   (모두 start_sec/end_sec 필드를 가진다). motions 는 필드명(start_sec/duration_sec)과
 *   의미(씬 내 상대 좌표)가 달라 이 함수의 대상에서 제외한다.
 *
 * 상태 불변 — scene 을 변형하지 않고 새 객체를 반환한다.
 * oldSpeed<=0 / newSpeed<=0 / oldSpeed===newSpeed 이면 scene 을 그대로 반환(무변경).
 */
function _round2(v: number): number {
  return Math.round((v + Number.EPSILON) * 100) / 100;
}

export function rescaleSceneOverlayTimings(
  scene: SceneCard,
  oldSpeed: number,
  newSpeed: number,
): SceneCard {
  if (
    !(oldSpeed > 0) ||
    !(newSpeed > 0) ||
    oldSpeed === newSpeed
  ) {
    return scene;
  }
  const factor = oldSpeed / newSpeed;
  const rescale = <T extends { start_sec?: number; end_sec?: number | null }>(
    arr: T[] | undefined,
  ): T[] | undefined => {
    if (!arr || arr.length === 0) return arr;
    return arr.map((ov) => {
      const next: T = { ...ov };
      if (typeof ov.start_sec === "number" && isFinite(ov.start_sec)) {
        next.start_sec = _round2(ov.start_sec * factor);
      }
      if (ov.end_sec != null && isFinite(ov.end_sec)) {
        next.end_sec = _round2(ov.end_sec * factor);
      }
      return next;
    });
  };
  return {
    ...scene,
    text_overlays: rescale(scene.text_overlays) ?? scene.text_overlays,
    shape_overlays: rescale(scene.shape_overlays),
    graphic_overlays: rescale(scene.graphic_overlays),
    image_overlays: rescale(scene.image_overlays),
    mosaic_overlays: rescale(scene.mosaic_overlays),
  };
}

/**
 * 재생 속도(배속/모드/ramp 비율) 변경 시 오버레이 출력 시각을 재조정한다.
 *   - 이전·이후 모두 정속(constant) → rescaleSceneOverlayTimings(배율) 와 동일.
 *   - 한쪽이라도 ramp → 출력 t → 원본 x(이전 spec 역함수) → 출력 t'(새 spec) 로 사상.
 *     원본 길이(rawDur)를 모르면 전체 출력 길이 비율로 근사(L=1 정규화, 공식이 L 에 선형).
 * next 는 scene 에 덮어쓸 속도 관련 필드(부분)다.
 */
export function rescaleSceneOverlayTimingsForSpeed(
  scene: SceneCard,
  next: Pick<Partial<SceneCard>, "footage_speed" | "footage_speed_mode" | "footage_speed_ramp_hold" | "footage_speed_ramp_accel" | "footage_speed_ramp_base">,
  rawDur?: number | null,
): SceneCard {
  const oldSpec = getSceneSpeedSpec(scene);
  const newSpec = getSceneSpeedSpec({ ...scene, ...next });
  if (!isRampActive(oldSpec) && !isRampActive(newSpec)) {
    return rescaleSceneOverlayTimings(scene, oldSpec.speed, newSpec.speed);
  }
  if (
    oldSpec.mode === newSpec.mode && oldSpec.speed === newSpec.speed &&
    oldSpec.hold === newSpec.hold && oldSpec.accel === newSpec.accel && oldSpec.base === newSpec.base
  ) {
    return scene;
  }
  let map: (t: number) => number;
  if (typeof rawDur === "number" && isFinite(rawDur) && rawDur > 0) {
    const L = rawDur;
    map = (t) => sourceToOutputTime(outputToSourceTime(t, L, oldSpec), L, newSpec);
  } else {
    const oldOut = speedOutputDuration(1, oldSpec);
    const newOut = speedOutputDuration(1, newSpec);
    const factor = oldOut > 0 ? newOut / oldOut : 1;
    map = (t) => t * factor;
  }
  const rescale = <T extends { start_sec?: number; end_sec?: number | null }>(
    arr: T[] | undefined,
  ): T[] | undefined => {
    if (!arr || arr.length === 0) return arr;
    return arr.map((ov) => {
      const nx: T = { ...ov };
      if (typeof ov.start_sec === "number" && isFinite(ov.start_sec)) {
        nx.start_sec = _round2(map(ov.start_sec));
      }
      if (ov.end_sec != null && isFinite(ov.end_sec)) {
        nx.end_sec = _round2(map(ov.end_sec));
      }
      return nx;
    });
  };
  return {
    ...scene,
    text_overlays: rescale(scene.text_overlays) ?? scene.text_overlays,
    shape_overlays: rescale(scene.shape_overlays),
    graphic_overlays: rescale(scene.graphic_overlays),
    image_overlays: rescale(scene.image_overlays),
    mosaic_overlays: rescale(scene.mosaic_overlays),
  };
}

/** segment 가 씬 길이를 넘어가는지 (overflow) 판정. */
export function isSegmentOverflow(
  segment: NarrationSegment,
  sceneVisualDuration: number,
): boolean {
  const end = (segment.offset_sec ?? 0) + (segment.duration_sec ?? 0);
  return end > sceneVisualDuration + 0.001;
}

/**
 * 씬 리스트 끝에 빈 씬을 추가한다.
 * @returns 갱신된 프로젝트와 신규 scene_id
 */
export async function addScene(
  projectId: string
): Promise<{ project: Project; newSceneId: number }> {
  const p = await getProject(projectId);
  const newSceneId = nextSceneId(p.scenes);
  const newScene = buildEmptyScene(newSceneId);
  const scenes = [...p.scenes, newScene];
  const project = await updateProject(projectId, { scenes });
  return { project, newSceneId };
}

/**
 * 지정한 인덱스(현재 정렬 순서) 다음 위치에 빈 씬을 삽입한다.
 * - `afterSceneIndex < 0`: 맨 앞에 삽입
 * - `afterSceneIndex >= scenes.length - 1`: 맨 뒤에 추가
 *
 * 주의: scene_id가 아닌 "정렬된 배열의 인덱스" 기준이다. UI에서 두 씬 사이의
 * 인서트 핸들에서 호출되므로 순서 의존적이다.
 */
export async function insertSceneAt(
  projectId: string,
  afterSceneIndex: number
): Promise<{ project: Project; newSceneId: number }> {
  const p = await getProject(projectId);
  // 화면 순서를 보존하기 위해 scene_id 오름차순 정렬 후 splice.
  const sorted = [...p.scenes].sort((a, b) => a.scene_id - b.scene_id);
  const tempNewId = nextSceneId(sorted);
  const newScene = buildEmptyScene(tempNewId);
  const insertAt = Math.min(Math.max(afterSceneIndex + 1, 0), sorted.length);
  sorted.splice(insertAt, 0, newScene);

  // splitSceneAt 과 동일 정책 — 새 씬에 max+1 을 주면 서버가 register 시 scene_id 오름차순으로
  // 강제 정렬해(routers/project.py:211) 새 씬이 맨 뒤로 이동하는 회귀가 발생한다.
  // sorted 배열 순서대로 1부터 재번호하면 사용자 의도(중간 삽입)가 그대로 유지된다.
  // TTS blob 도 새 scene_id 키로 마이그레이션한다.
  const renumberMap: Map<number, number> = new Map();
  sorted.forEach((s, i) => {
    const newSid = i + 1;
    if (s.scene_id !== newSid) renumberMap.set(s.scene_id, newSid);
  });

  if (renumberMap.size > 0) {
    // cascading(1→2, 2→3) 데이터 손실 회피: (1) 옛 키들 blob 읽기 → (2) 옛 키 삭제 → (3) 새 키 저장.
    const blobs: Map<number, Blob> = new Map();
    for (const [oldSid, newSid] of renumberMap) {
      const blob = await dbGetTtsAudioByKey(`${projectId}:${oldSid}`).catch(() => undefined);
      if (blob) blobs.set(newSid, blob);
    }
    for (const oldSid of renumberMap.keys()) {
      await dbDeleteTtsAudio(projectId, oldSid).catch(() => {});
    }
    for (const [newSid, blob] of blobs) {
      await dbSaveTtsAudioByKey(`${projectId}:${newSid}`, blob).catch(() => {});
    }

    sorted.forEach((s, i) => { s.scene_id = i + 1; });
  }
  const newSceneId = renumberMap.get(tempNewId) ?? tempNewId;

  const project = await updateProject(projectId, { scenes: sorted });
  return { project, newSceneId };
}

// ── "중간 이미지 삽입" 원클릭 자동화 ──────────────────────────────────────
//
// 사용자가 이미지를 하나 지정하면, 그 이미지를 전체화면으로 보여주는 "이미지 전용
// 삽입 씬"을 자동 생성해 원하는 위치(특정 씬 뒤)에 넣는다. (기존 2단계: 빈 씬 추가 →
// 이미지 footage 지정, 을 하나의 동작으로 합친 것.)
//
// 삽입 씬 구성:
//  - is_insert_image_scene=true 마커 (프론트가 단순 요소로 취급).
//  - footage = kenburns(image=파일명, fit=contain) — 이 파이프라인에서 이미지 전체화면
//    씬의 정식 표현. 렌더러(clip_editor)가 이미지를 출력 해상도에 맞춰 전체화면 합성한다.
//    (footage=null 인 빈 씬은 렌더 시 스킵되므로 사용할 수 없다 — render.py 참조.)
//  - narration 없음(빈 narration_segments) + 짧은 지속시간(기본 3초).

/** 중간 이미지 삽입 씬 기본 지속시간(초). */
export const DEFAULT_INSERT_IMAGE_DURATION_SEC = 3.0;

/**
 * 이미지 전용 삽입 씬 1개를 만드는 순수 함수 (단위 테스트 대상).
 *
 * @param sceneId  이 씬에 부여할 scene_id (호출자가 발급/재번호 책임).
 * @param imageFilename  footage 디렉토리 내 이미지 파일명(basename). 전체화면으로 표시.
 * @param durationSec  지속시간(초). 미지정 시 기본값.
 */
export function buildInsertImageScene(
  sceneId: number,
  imageFilename: string,
  durationSec: number = DEFAULT_INSERT_IMAGE_DURATION_SEC,
): SceneCard {
  const dur = durationSec > 0 ? durationSec : DEFAULT_INSERT_IMAGE_DURATION_SEC;
  const scene = buildEmptyScene(sceneId);
  scene.duration_hint_sec = dur;
  scene.is_insert_image_scene = true;
  scene.title = "중간 이미지";
  // 이미지 전체화면 — kenburns footage 로 표현(이 파이프라인의 이미지 씬 정식 방식).
  // fit=contain 으로 이미지를 잘리지 않게 전체 표시(여백은 프로젝트 bg_color).
  scene.footage = {
    type: "kenburns",
    image: imageFilename,
    fit: "contain",
  } as unknown as SceneCard["footage"];
  return scene;
}

/**
 * 지정한 인덱스(현재 정렬 순서) 다음 위치에 "중간 이미지 삽입 씬"을 자동 생성한다.
 *
 * insertSceneAt 과 동일한 재번호/TTS blob 마이그레이션 정책을 따른다 — 삽입 씬이
 * 화면 순서상 사용자가 원한 위치에 그대로 유지되도록 sorted 배열 순서로 1부터 재번호.
 *
 * @param afterSceneIndex  이 인덱스 뒤에 삽입 (`< 0`=맨 앞, `>= length-1`=맨 뒤).
 * @param imageFilename  footage 디렉토리 내 이미지 파일명(basename).
 */
export async function insertImageSceneAt(
  projectId: string,
  afterSceneIndex: number,
  imageFilename: string,
  durationSec: number = DEFAULT_INSERT_IMAGE_DURATION_SEC,
): Promise<{ project: Project; newSceneId: number }> {
  const p = await getProject(projectId);
  const sorted = [...p.scenes].sort((a, b) => a.scene_id - b.scene_id);
  const tempNewId = nextSceneId(sorted);
  const newScene = buildInsertImageScene(tempNewId, imageFilename, durationSec);
  const insertAt = Math.min(Math.max(afterSceneIndex + 1, 0), sorted.length);
  sorted.splice(insertAt, 0, newScene);

  // insertSceneAt 과 동일 재번호 정책 (routers/project.py 가 register 시 scene_id 오름차순
  // 강제 정렬하므로, sorted 순서대로 1부터 재번호해야 삽입 위치가 유지된다).
  const renumberMap: Map<number, number> = new Map();
  sorted.forEach((s, i) => {
    const newSid = i + 1;
    if (s.scene_id !== newSid) renumberMap.set(s.scene_id, newSid);
  });

  if (renumberMap.size > 0) {
    const blobs: Map<number, Blob> = new Map();
    for (const [oldSid, newSid] of renumberMap) {
      const blob = await dbGetTtsAudioByKey(`${projectId}:${oldSid}`).catch(() => undefined);
      if (blob) blobs.set(newSid, blob);
    }
    for (const oldSid of renumberMap.keys()) {
      await dbDeleteTtsAudio(projectId, oldSid).catch(() => {});
    }
    for (const [newSid, blob] of blobs) {
      await dbSaveTtsAudioByKey(`${projectId}:${newSid}`, blob).catch(() => {});
    }
    sorted.forEach((s, i) => { s.scene_id = i + 1; });
  }
  const newSceneId = renumberMap.get(tempNewId) ?? tempNewId;

  const project = await updateProject(projectId, { scenes: sorted });
  return { project, newSceneId };
}

/**
 * 지정한 sourceSceneId의 씬을 deep copy하여 바로 다음 위치에 삽입한다.
 *
 * 복제 규칙:
 *  - scene_id는 max+1로 재발급
 *  - text_overlays[*].id는 모두 재발급 (충돌 방지)
 *  - filters/graphic_overlays/motions의 source_id는 유지 — 태그 추적 일관성
 *  - tts/match는 null로 초기화 (오디오는 복사하지 않음; 새 씬은 빈 상태)
 *  - 그 외 효과 필드는 모두 그대로 deep copy
 */
export async function duplicateScene(
  projectId: string,
  sourceSceneId: number
): Promise<{ project: Project; newSceneId: number }> {
  const p = await getProject(projectId);
  const sorted = [...p.scenes].sort((a, b) => a.scene_id - b.scene_id);
  const idx = sorted.findIndex((s) => s.scene_id === sourceSceneId);
  if (idx < 0) throw new Error(`복제할 씬을 찾을 수 없습니다: scene_id=${sourceSceneId}`);
  const source = sorted[idx];
  const newSceneId = nextSceneId(sorted);

  // structuredClone이 가능한 환경(modern browser)이면 사용, 아니면 JSON fallback.
  const cloned: SceneCard =
    typeof structuredClone === "function"
      ? structuredClone(source)
      : JSON.parse(JSON.stringify(source));

  cloned.scene_id = newSceneId;
  // text_overlay id 재발급: 태그 prefix "tag-{tag}-..." 형식은 prefix 유지하고 뒤를 새로 발급.
  // 다단어 태그명(예: "cyber-punk")을 지원하기 위해 cloned.tags 리스트와 직접 비교한다.
  // (정규식 `/^(tag-[^-]+-)/`은 첫 하이픈에서 잘려 다단어 태그를 깨뜨림 — #246)
  cloned.text_overlays = (cloned.text_overlays ?? []).map((ov) => {
    const matchedTag = (cloned.tags ?? []).find((t) => ov.id?.startsWith(`tag-${t}-`));
    const newId = matchedTag
      ? `tag-${matchedTag}-${uuidv4().slice(0, 8)}`
      : uuidv4();
    return { ...ov, id: newId };
  });
  // 이미지 오버레이 — id 재발급 (충돌 방지). image_asset_id는 그대로 유지 (같은 자산 참조 OK).
  cloned.image_overlays = (cloned.image_overlays ?? []).map((ov) => ({
    ...ov,
    id: `image_${Date.now()}_${uuidv4().slice(0, 8)}`,
  }));
  // tts/match는 초기화 — 복제본은 새 씬이므로 오디오를 공유하지 않는다.
  cloned.tts = null;
  cloned.match = null;
  // filters/graphic_overlays/motions의 source_id는 그대로 유지 (태그 추적 일관성).

  sorted.splice(idx + 1, 0, cloned);
  const project = await updateProject(projectId, { scenes: sorted });
  return { project, newSceneId };
}

/**
 * 씬 나누기 — footage는 공유하고 한 씬을 N+1개 씬으로 분할한다.
 *
 * 동작 규칙(사용자 정책):
 *  - 원본 씬은 첫 번째 구간을 가리키도록 갱신(clip_start_sec=0, clip_end_sec=cuts[0]).
 *  - 나머지 N개는 새 scene_id를 부여한 빈 씬으로 삽입.
 *    각 새 씬은 같은 footage를 공유하되 clip_start/clip_end만 다르게 설정.
 *  - 나레이션/TTS/오버레이/모션 등은 원본(첫) 씬에만 남기고 새 씬들은 비움.
 *
 * @param cuts 분할 지점(초) 리스트. 오름차순 정렬 후 footage 길이(0~totalDur)로 클램프.
 * @param totalDur footage 전체 길이(초). 없으면 마지막 구간은 clip_end=null로 설정.
 */
export async function splitSceneAt(
  projectId: string,
  sourceSceneId: number,
  cuts: number[],
  totalDur?: number
): Promise<{ project: Project; newSceneIds: number[] }> {
  const p = await getProject(projectId);
  const sorted = [...p.scenes].sort((a, b) => a.scene_id - b.scene_id);
  const idx = sorted.findIndex((s) => s.scene_id === sourceSceneId);
  if (idx < 0) throw new Error(`분할할 씬을 찾을 수 없습니다: scene_id=${sourceSceneId}`);
  const source = sorted[idx];
  if (!source.footage) throw new Error("footage가 없는 씬은 나눌 수 없습니다");
  const ftype = (source.footage as { type: string }).type;
  if (ftype !== "clip") throw new Error("현재는 clip 타입 footage만 나눌 수 있습니다");

  // cuts는 *씬 내 상대 좌표*(0~씬길이) 기준이다. 호출자(예: Editor.handleApplySplit)에서
  // footage 절대 좌표를 받으면 baseStart를 빼서 상대 좌표로 변환한 뒤 호출해야 한다.

  // 기존 footage에서 clip_start_sec가 있으면 그 값을 baseStart로(부분 씬 추가 분할 대응).
  const baseStart = (source.footage as { clip_start_sec?: number }).clip_start_sec ?? 0;
  let baseEnd = (source.footage as { clip_end_sec?: number | null }).clip_end_sec ?? null;
  // footage 길이(totalDur)가 주어지면 baseEnd가 footage 길이를 초과하지 않도록 clamp.
  // 또한 baseEnd가 null이고 totalDur가 있으면 명시적 끝값으로 설정해 새 씬도 끝을 알게 한다.
  if (totalDur && totalDur > 0) {
    if (baseEnd === null || baseEnd > totalDur) baseEnd = totalDur;
  }

  // 씬 길이(상대 좌표의 최댓값). baseEnd가 null이면 totalDur, 둘 다 없으면 무한대.
  const sceneSpan = baseEnd !== null
    ? Math.max(0, baseEnd - baseStart)
    : (totalDur && totalDur > 0 ? Math.max(0, totalDur - baseStart) : Number.POSITIVE_INFINITY);

  // 분할 지점 정리: 0/씬길이 클램프 → 양 끝 제외 → 중복 제거 → 정렬.
  const cleaned = Array.from(new Set(cuts.map((c) => Math.max(0, Math.min(sceneSpan, c)))))
    .filter((c) => c > 0 && (sceneSpan === Infinity || c < sceneSpan))
    .sort((a, b) => a - b);
  if (cleaned.length === 0) throw new Error("유효한 분할 지점이 없습니다");

  // 경계: [baseStart, baseStart+cuts[0], ..., baseEnd]
  const safeCuts = cleaned.map((c) => c + baseStart);
  const boundaries: (number | null)[] = [baseStart, ...safeCuts, baseEnd];

  // 첫 구간은 원본 씬 갱신, 나머지는 새 씬 삽입.
  const firstStart = boundaries[0] as number;
  const firstEnd = boundaries[1] as number | null;
  // 분할로 영향받는 모든 씬(원본 첫 씬 포함)의 match는 클리어한다.
  // 이유: 원본 match.trim_start/trim_end는 분할 전 전체 구간을 가리키므로
  //   - DurationStatus가 분할로 줄어든 clip 길이가 아닌 옛 전체 길이를 표시하고
  //   - 백엔드 렌더 우선순위(match > footage)에 따라 옛 구간이 깔릴 수 있다.
  // 사용자가 분할 후 다시 매칭을 돌리거나 수동으로 채우면 새 match가 생성된다.
  const updatedSource: SceneCard = {
    ...source,
    footage: { ...(source.footage as object), clip_start_sec: firstStart, clip_end_sec: firstEnd } as typeof source.footage,
    match: null,
  };
  sorted[idx] = updatedSource;

  // 새 씬 N개 생성 — narration/tts/overlays 없이 빈 씬.
  // nextSceneId는 현재 sorted 기준 max+1이라 여러 개 생성 시 매 번 갱신해야 충돌이 없다.
  const workingSorted: SceneCard[] = sorted.slice();
  const newSceneIds: number[] = [];
  for (let k = 1; k < boundaries.length - 1; k++) {
    const segStart = boundaries[k] as number;
    const segEnd = boundaries[k + 1] as number | null;
    const newId = nextSceneId(workingSorted);
    const cloned: SceneCard =
      typeof structuredClone === "function"
        ? structuredClone(source)
        : JSON.parse(JSON.stringify(source));
    cloned.scene_id = newId;
    cloned.footage = { ...(source.footage as object), clip_start_sec: segStart, clip_end_sec: segEnd } as typeof source.footage;
    // 텍스트/오버레이/오디오는 비움 (첫 씬에만 남음)
    cloned.narration_text = "";
    cloned.text_overlays = [];
    cloned.shape_overlays = [];
    cloned.graphic_overlays = [];
    cloned.mosaic_overlays = [];
    cloned.image_overlays = [];
    cloned.filters = [];
    cloned.motions = [];
    cloned.tts = null;
    cloned.match = null;
    cloned.tags = [];
    cloned.title = "";
    // 분할로 생긴 새 씬은 sfx도 비움 (첫 씬에만 남김).
    cloned.sfx_asset_id = null;
    cloned.sfx_volume = 0.7;
    cloned.sfx_offset_sec = 0;
    // 원본 씬 바로 뒤에 차례대로 끼워 넣으면서 workingSorted를 갱신 → nextSceneId가 정확히 max+1을 반환.
    const insertPos = idx + 1 + newSceneIds.length;
    workingSorted.splice(insertPos, 0, cloned);
    newSceneIds.push(newId);
  }

  // 전체 씬 재번호 — 새 씬에 max+1을 주면 서버 scene_id 정렬 시 맨 뒤로 가는 회귀가 발생한다.
  // 클라이언트 sorted 순서대로 1부터 다시 부여하면 사용자 의도(원본 바로 뒤)가 보존된다.
  // TTS blob도 새 scene_id 키로 옮긴다(서버 wav는 register/렌더 흐름에서 재생성됨).
  const renumberMap: Map<number, number> = new Map();
  const oldIdInOrder: number[] = workingSorted.map((s) => s.scene_id);
  oldIdInOrder.forEach((oldId, i) => {
    const newSid = i + 1;
    if (oldId !== newSid) renumberMap.set(oldId, newSid);
  });

  if (renumberMap.size > 0) {
    // TTS blob 마이그레이션 — cascading(1→2, 2→3) 케이스에서 데이터 손실 회피를 위해
    // (1) 옛 키들의 blob을 메모리에 모두 읽고 → (2) 옛 키 삭제 → (3) 새 키로 저장.
    const blobs: Map<number /* newSid */, Blob> = new Map();
    for (const [oldSid, newSid] of renumberMap) {
      const blob = await dbGetTtsAudioByKey(`${projectId}:${oldSid}`).catch(() => undefined);
      if (blob) blobs.set(newSid, blob);
    }
    for (const oldSid of renumberMap.keys()) {
      await dbDeleteTtsAudio(projectId, oldSid).catch(() => {});
    }
    for (const [newSid, blob] of blobs) {
      await dbSaveTtsAudioByKey(`${projectId}:${newSid}`, blob).catch(() => {});
    }

    // 씬 객체의 scene_id를 재부여 (순서는 이미 정해져 있음).
    workingSorted.forEach((s, i) => { s.scene_id = i + 1; });
  }
  const renumberedNewIds = newSceneIds.map((oldId) => renumberMap.get(oldId) ?? oldId);

  // 잔재 정리 — 재번호/분할 후 활성 scene_id 집합 밖의 IndexedDB TTS blob 키를 일괄 삭제.
  // (renumberMap 마이그레이션 이후에도 누락된 옛 키가 남아 다음 register/렌더에서 부활하는 회귀 방지)
  try {
    const _active = new Set(workingSorted.map((s) => s.scene_id));
    const _keys = await dbGetAllTtsAudioKeysForProject(projectId);
    for (const _k of _keys) {
      const _sidStr = _k.slice(projectId.length + 1);
      const _sid = Number(_sidStr);
      if (Number.isFinite(_sid) && !_active.has(_sid)) {
        await dbDeleteTtsAudio(projectId, _sid).catch(() => {});
      }
    }
  } catch {
    // 잔재 정리 실패는 분할 자체를 막지 않는다.
  }

  const project = await updateProject(projectId, { scenes: workingSorted });
  return { project, newSceneIds: renumberedNewIds };
}

// ── Studio Asset 조작 ─────────────────────────────────────────────────────

/**
 * #381 — studio_assets 의 asset_id 중복을 정리하는 순수 헬퍼.
 *
 * 같은 `asset_id` 가 여러 번 등장하면 **첫 항목만 유지**한다(append-only 의미 보존).
 * 단, 뒤에 들어온 항목이 더 풍부한 메타데이터(예: crop, label 변경)를 갖는 경우가
 * 있어 보존 항목에 누락된 필드를 채워주는 보수적 머지를 한다.
 * - `label`: 첫 항목이 빈 문자열이면 후속 항목 값으로 보완.
 * - `crop` : 첫 항목이 null/undefined 이면 후속 항목 값으로 보완.
 *
 * 이 함수는 `addStudioAssets` 등록 시점의 중복 차단과 `getProject` 단계의
 * 기존 데이터 자동 복구 양쪽에서 호출된다. 입력 배열을 변형하지 않는다.
 */
export function dedupeStudioAssets(assets: FootageAsset[]): FootageAsset[] {
  const seen = new Map<string, FootageAsset>();
  const order: string[] = [];
  for (const a of assets) {
    if (!a || typeof a.asset_id !== "string") continue;
    const prev = seen.get(a.asset_id);
    if (!prev) {
      seen.set(a.asset_id, a);
      order.push(a.asset_id);
      continue;
    }
    // 보수적 머지 — 첫 항목 우선, 누락 필드만 후속값으로 보완.
    const merged: FootageAsset = { ...prev };
    if (!merged.label && a.label) merged.label = a.label;
    if ((merged.crop === null || merged.crop === undefined) && a.crop) {
      merged.crop = a.crop;
    }
    seen.set(a.asset_id, merged);
  }
  return order.map((id) => seen.get(id)!);
}

export async function addStudioAssets(
  projectId: string,
  assets: FootageAsset[]
): Promise<Project> {
  const p = await getProject(projectId);
  // #381 — asset_id 기준 중복을 1차 차단. 백엔드가 이미 등록한 asset 을
  // 프론트가 다시 append 호출하더라도 두 카드로 보이지 않도록 방어.
  const merged = dedupeStudioAssets([...p.studio_assets, ...assets]);
  // 변화가 없으면 불필요한 서버 push 를 피한다.
  if (merged.length === p.studio_assets.length) {
    const sameOrder = merged.every((a, i) => a.asset_id === p.studio_assets[i].asset_id);
    if (sameOrder) return p;
  }
  return updateProject(projectId, { studio_assets: merged });
}

export async function updateAssetLabel(
  projectId: string,
  assetId: string,
  label: string
): Promise<Project> {
  const p = await getProject(projectId);
  const studio_assets = p.studio_assets.map((a) =>
    a.asset_id === assetId ? { ...a, label } : a
  );
  return updateProject(projectId, { studio_assets });
}

export async function updateAssetCrop(
  projectId: string,
  assetId: string,
  crop: CropRect | null
): Promise<Project> {
  const p = await getProject(projectId);
  const studio_assets = p.studio_assets.map((a) =>
    a.asset_id === assetId ? { ...a, crop } : a
  );
  return updateProject(projectId, { studio_assets });
}

export async function deleteAsset(
  projectId: string,
  assetId: string
): Promise<Project> {
  const p = await getProject(projectId);
  const studio_assets = p.studio_assets.filter((a) => a.asset_id !== assetId);
  return updateProject(projectId, { studio_assets });
}

export async function setDefaultCrop(
  projectId: string,
  crop: CropRect | null,
  applyToAll: boolean
): Promise<Project> {
  const p = await getProject(projectId);
  const studio_assets = applyToAll
    ? p.studio_assets.map((a) => ({ ...a, crop }))
    : p.studio_assets;
  return updateProject(projectId, { default_crop: crop, studio_assets });
}
