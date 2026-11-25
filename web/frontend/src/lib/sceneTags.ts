/**
 * 씬 태그 시스템 (#208 1차 + #215 효과 연결 확장 + #207 1차 motion 머지)
 *
 * 범위:
 *  - 빌트인 태그 사전 (12종)
 *  - applySceneTags / removeSceneTag 효과 머지/제거 엔진
 *    · text_overlay 머지 (#208 1차)
 *    · filter / graphic_overlay 머지 (#215 추가)
 *    · motion 머지 (#207 1차 — shake/clip_zoom)
 *  - 출처 추적: 추가된 효과 객체에 `id`(TextOverlay) / `source_id`(SceneFilter, GraphicOverlay, SceneMotion)에
 *    `tag-{tagname}-{kind}` 형식 부여. removeSceneTag는 해당 식별자만 제거.
 *
 * 의도적으로 제외 (후속 사이클):
 *  - Editor.tsx UI 통합 (입력칸/칩/자동완성/프리뷰 invalidate)
 *  - 시나리오 파서 자동 추출
 *  - 유저 정의 태그 IndexedDB CRUD
 *  - 백엔드 register_project/patchScene tags 동기화
 *  - motion 슬로우모션 (#207 후속 — 영상 길이 변경 + narration_timeline 정합성)
 *  - POV 필터 머지 (#219)
 *
 * 진실 소스: `config/scene_tags.json` (프로젝트 루트). 본 TS 모듈은 동일한 정의를
 * 인라인으로 보유하여 빌드 시 정적 import 의존성을 만들지 않는다.
 * 두 파일을 동기 상태로 유지할 책임은 본 이슈의 후속 통합 작업이 진다.
 */
import type {
  GraphicOverlay,
  GraphicOverlayType,
  MotionType,
  SceneCard,
  SceneFilter,
  SceneFilterType,
  SceneMotion,
  TextOverlay,
  TextOverlayPreset,
} from "../types";
import { BUILTIN_TEXT_PRESETS } from "./textPresetSeeds";

// ─── 사전 타입 정의 ────────────────────────────────────────────────────────

/**
 * 현재 미구현 효과 종류. 의존 이슈 구현 시 실제 효과 객체로 교체된다.
 * - filter (pov 한정): #219 미머지
 * - motion (slowmo 한정): #207 후속 사이클
 */
export type PendingEffectKind = "filter" | "graphic_overlay" | "motion";

/**
 * 태그 사전이 보유하는 filter 효과 템플릿. 적용 시 SceneFilter 객체로 변환되며,
 * source_id가 부여된다.
 */
export interface SceneTagFilterEffect {
  filter_type: SceneFilterType;
  intensity: number;
  params?: Record<string, unknown>;
}

/**
 * 태그 사전이 보유하는 graphic_overlay 효과 템플릿. 적용 시 GraphicOverlay 객체로
 * 변환되며, id/source_id가 부여된다.
 */
export interface SceneTagGraphicOverlayEffect {
  type: GraphicOverlayType;
  start_sec?: number;
  end_sec?: number | null;
  x?: number;
  y?: number;
  params?: Record<string, unknown>;
}

/**
 * 태그 사전이 보유하는 motion 효과 템플릿. 적용 시 SceneMotion 객체로 변환되며,
 * source_id가 부여된다.
 */
export interface SceneTagMotionEffect {
  motion_type: MotionType;
  start_sec?: number;
  duration_sec?: number;
  params?: Record<string, unknown>;
}

export interface SceneTagEffects {
  /** TextOverlayPreset.id 참조 (예: "builtin-horror"). */
  text_preset?: string;
  /** 적용할 SceneFilter 템플릿. */
  filter?: SceneTagFilterEffect;
  /** 적용할 GraphicOverlay 템플릿. */
  graphic_overlay?: SceneTagGraphicOverlayEffect;
  /** 적용할 SceneMotion 템플릿 (#207). */
  motion?: SceneTagMotionEffect;
  /** 보류된 효과 종류 목록 (의존 이슈 미구현으로 인한 placeholder). */
  _pending?: PendingEffectKind[];
}

export interface SceneTagDef {
  /** 표시용 한국어 라벨. */
  label: string;
  /** 사용자가 칩에 hover 시 보여줄 설명. */
  description: string;
  /** 태그가 적용할 효과 세트. */
  effects: SceneTagEffects;
}

export interface SceneTagDictionary {
  tags: Record<string, SceneTagDef>;
}

/**
 * IndexedDB `userSceneTags` 스토어에 저장되는 유저 정의 씬 태그 (#214).
 *
 * 빌트인 사전(`SceneTagDef`)과 동일한 `label`/`description`/`effects` 구조를 갖되,
 * 영속 저장에 필요한 `id`/`created_at`/`updated_at`을 추가한다.
 * `loadTagDictionary()`는 IndexedDB 사전을 빌트인과 머지해서 반환한다 (같은 id면 유저 오버라이드).
 */
export interface UserSceneTag {
  /** 태그 ID — 영문 소문자 + 하이픈 (예: "epic-battle"). */
  id: string;
  /** 표시용 한국어 라벨. */
  label: string;
  /** 칩 hover 시 설명. */
  description?: string;
  /** 태그가 적용할 효과 세트 (빌트인과 동일 스키마). */
  effects: SceneTagEffects;
  /** 최초 생성 ISO 8601. */
  created_at: string;
  /** 마지막 수정 ISO 8601. */
  updated_at: string;
}

// ─── 빌트인 태그 사전 (config/scene_tags.json 미러) ────────────────────────

const BUILTIN_TAG_DICTIONARY: SceneTagDictionary = {
  tags: {
    narrative: {
      label: "내러티브",
      description: "일상/담담 톤. 가벼운 흰색 텍스트 자막.",
      effects: { text_preset: "builtin-narrative" },
    },
    witty: {
      label: "위트",
      description: "당황/강조/리액션. 노란 두꺼운 텍스트.",
      effects: { text_preset: "builtin-witty" },
    },
    horror: {
      label: "공포",
      description: "공포·경고·분노 분위기. 붉은 텍스트 + 어두운 horror 필터 + 스포트라이트 오버레이.",
      effects: {
        text_preset: "builtin-horror",
        filter: { filter_type: "horror", intensity: 0.7, params: {} },
        graphic_overlay: {
          type: "spotlight",
          start_sec: 0.0,
          end_sec: 2.0,
          x: 0.5,
          y: 0.5,
          params: { darkness: 0.65, radius: 0.35 },
        },
      },
    },
    crash: {
      label: "충돌",
      description: "충격 순간. 위트 텍스트 + 강한 흔들림(shake heavy) 모션.",
      effects: {
        text_preset: "builtin-witty",
        motion: {
          motion_type: "shake",
          start_sec: 0.0,
          duration_sec: 0.8,
          params: { intensity: 1.0 },
        },
      },
    },
    alert: {
      label: "경고",
      description: "경고/주의 알림. 위트 텍스트 + 경고 그래픽 오버레이.",
      effects: {
        text_preset: "builtin-witty",
        graphic_overlay: {
          type: "alert",
          start_sec: 0.0,
          end_sec: 2.0,
          x: 0.5,
          y: 0.5,
          params: { text: "경고", style: "warning" },
        },
      },
    },
    flashback: {
      label: "회상",
      description: "과거 회상 장면. 내러티브 텍스트 + 빛바랜 flashback 필터.",
      effects: {
        text_preset: "builtin-narrative",
        filter: { filter_type: "flashback", intensity: 0.8, params: {} },
      },
    },
    vivid: {
      label: "선명",
      description: "채도·대비 강화. 내러티브 텍스트 + vivid 필터.",
      effects: {
        text_preset: "builtin-narrative",
        filter: { filter_type: "vivid", intensity: 0.7, params: {} },
      },
    },
    reaction: {
      label: "리액션",
      description: "당황·놀람 강한 리액션. 위트 텍스트 + 만화풍 리액션 오버레이.",
      effects: {
        text_preset: "builtin-witty",
        graphic_overlay: {
          type: "reaction",
          start_sec: 0.0,
          end_sec: 1.5,
          x: 0.5,
          y: 0.5,
          params: { icon_type: "thunder" },
        },
      },
    },
    nightvision: {
      label: "야간투시",
      description: "초록 nightvision 필터 + 스포트라이트 오버레이.",
      effects: {
        filter: { filter_type: "nightvision", intensity: 0.8, params: {} },
        graphic_overlay: {
          type: "spotlight",
          start_sec: 0.0,
          end_sec: 2.0,
          x: 0.5,
          y: 0.5,
          params: { darkness: 0.7, radius: 0.35 },
        },
      },
    },
    pov: {
      label: "1인칭 시점",
      description: "POV 줌·왜곡 + 스포트라이트 오버레이. (POV 필터는 #219 후속.)",
      effects: {
        graphic_overlay: {
          type: "spotlight",
          start_sec: 0.0,
          end_sec: 2.0,
          x: 0.5,
          y: 0.5,
          params: { darkness: 0.8, radius: 0.3 },
        },
        _pending: ["filter"],
      },
    },
    smell: {
      label: "냄새",
      description: "초록 냄새 표현 오버레이 + 위트 텍스트.",
      effects: {
        text_preset: "builtin-witty",
        graphic_overlay: {
          type: "smell",
          start_sec: 0.0,
          end_sec: 2.5,
          x: 0.5,
          y: 0.5,
          params: { color: "green", intensity: 0.6 },
        },
      },
    },
    target: {
      label: "조준",
      description: "조준선/크로스헤어 오버레이 + 위트 텍스트.",
      effects: {
        text_preset: "builtin-witty",
        graphic_overlay: {
          type: "target",
          start_sec: 0.0,
          end_sec: 2.0,
          x: 0.5,
          y: 0.5,
          params: { marker_type: "crosshair", color: "#ff3030" },
        },
      },
    },
  },
};

/**
 * 태그 사전 로드 — 빌트인 사전과 IndexedDB의 유저 정의 사전을 머지한다 (#214).
 *
 * 머지 규칙:
 *  - 빌트인 + 유저 정의 모두 포함.
 *  - 같은 `id`가 양쪽에 있으면 **유저 정의가 빌트인을 오버라이드**.
 *  - IndexedDB 접근 실패 (브라우저 미지원/권한 거부/스토어 누락) 시에도 빌트인은 항상 반환.
 *
 * 호출 측은 모달 저장/삭제 후 재호출해 사전을 갱신하면 된다.
 *
 * 순환 import 방지를 위해 `db.ts`를 동적 import한다 — `db.ts`가 `UserSceneTag` 타입을
 * 본 파일에서 받는 구조라 정적 import는 불가.
 */
export async function loadTagDictionary(): Promise<SceneTagDictionary> {
  const merged: SceneTagDictionary = {
    tags: { ...BUILTIN_TAG_DICTIONARY.tags },
  };
  try {
    const { dbListUserSceneTags } = await import("./db");
    const userTags = await dbListUserSceneTags();
    for (const u of userTags) {
      // id 정규화 — 영문 소문자 + 하이픈만. 잘못된 키는 건너뛴다 (방어).
      if (!u.id || typeof u.id !== "string") continue;
      merged.tags[u.id] = {
        label: u.label,
        description: u.description ?? "",
        effects: u.effects ?? {},
      };
    }
  } catch (err) {
    // IndexedDB 실패 — 빌트인만 반환 (앱 동작 보장).
    console.warn("[sceneTags] 유저 태그 로드 실패, 빌트인만 사용:", err);
  }
  return merged;
}

/**
 * 동기 버전 — 테스트 및 빌트인 전용 사용처용.
 */
export function getBuiltinTagDictionary(): SceneTagDictionary {
  return BUILTIN_TAG_DICTIONARY;
}

// ─── 효과 적용 엔진 ────────────────────────────────────────────────────────

/**
 * `TextOverlayPreset` ID로 빌트인 프리셋을 찾는다.
 * 유저 정의 프리셋은 IndexedDB에 있어 본 모듈에서 직접 조회하지 않는다 (후속 사이클).
 */
function lookupBuiltinPreset(presetId: string): TextOverlayPreset | null {
  return BUILTIN_TEXT_PRESETS.find((p) => p.id === presetId) ?? null;
}

/**
 * 단조 증가 ID 생성. (간단한 timestamp + random; 동시 호출 충돌은 후속 사이클에서 uuid로 교체)
 */
function newOverlayId(tag: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  return `tag-${tag}-${Date.now().toString(36)}-${rand}`;
}

/**
 * 태그가 추가한 filter/graphic_overlay 출처 식별자.
 * removeSceneTag가 이 prefix로 시작하는 객체만 정리하도록 사용한다.
 *
 * 형식: `tag-{tagname}-filter` / `tag-{tagname}-graphic`
 * 같은 태그를 두 번 추가해도 중복 차단(applySceneTags 초입)으로 단일 객체 보장.
 */
function tagFilterSourceId(tag: string): string {
  return `tag-${tag}-filter`;
}
function tagGraphicSourceId(tag: string): string {
  return `tag-${tag}-graphic`;
}
function tagMotionSourceId(tag: string): string {
  return `tag-${tag}-motion`;
}
function tagGraphicOverlayId(tag: string): string {
  // GraphicOverlay.id는 어차피 고유해야 하므로 timestamp suffix 추가.
  // source_id가 제거 키, id는 React key 등 일반 용도.
  const rand = Math.random().toString(36).slice(2, 8);
  return `tag-${tag}-graphic-${Date.now().toString(36)}-${rand}`;
}

/**
 * 프리셋으로부터 씬에 추가할 TextOverlay 인스턴스를 생성한다.
 * 텍스트 본문은 빈 문자열로 두어, 후속 UI에서 유저가 입력하도록 한다.
 *
 * TextOverlay.start_sec/end_sec는 씬 길이를 모르므로 0~null로 둔다.
 * (실제 렌더 시 씬 전체 구간이 기본값으로 적용된다.)
 */
function presetToOverlay(preset: TextOverlayPreset, sourceTag: string): TextOverlay {
  return {
    id: newOverlayId(sourceTag),
    text: "",
    ...preset.style,
    start_sec: 0,
    end_sec: null,
  };
}

/**
 * 태그 사전의 filter 템플릿을 SceneFilter 인스턴스로 변환한다.
 * source_id에 `tag-{tag}-filter` 부여 — removeSceneTag가 이를 식별자로 제거.
 */
function templateToSceneFilter(
  template: SceneTagFilterEffect,
  sourceTag: string,
): SceneFilter {
  return {
    filter_type: template.filter_type,
    intensity: template.intensity,
    params: template.params ? { ...template.params } : {},
    source_id: tagFilterSourceId(sourceTag),
  };
}

/**
 * 태그 사전의 graphic_overlay 템플릿을 GraphicOverlay 인스턴스로 변환한다.
 * id는 React key 등 일반 용도, source_id는 출처 추적 전용.
 */
function templateToGraphicOverlay(
  template: SceneTagGraphicOverlayEffect,
  sourceTag: string,
): GraphicOverlay {
  return {
    id: tagGraphicOverlayId(sourceTag),
    type: template.type,
    start_sec: template.start_sec ?? 0,
    end_sec: template.end_sec ?? null,
    x: template.x ?? 0.5,
    y: template.y ?? 0.5,
    params: template.params ? { ...template.params } : {},
    source_id: tagGraphicSourceId(sourceTag),
  };
}

/**
 * 태그 사전의 motion 템플릿을 SceneMotion 인스턴스로 변환한다 (#207).
 * source_id에 `tag-{tag}-motion` 부여 — removeSceneTag가 이를 식별자로 제거.
 */
function templateToSceneMotion(
  template: SceneTagMotionEffect,
  sourceTag: string,
): SceneMotion {
  return {
    motion_type: template.motion_type,
    start_sec: template.start_sec ?? 0,
    duration_sec: template.duration_sec ?? 1.0,
    params: template.params ? { ...template.params } : {},
    source_id: tagMotionSourceId(sourceTag),
  };
}

/**
 * 태그 적용 결과의 효과 출처 추적.
 */
export interface TagApplyTrace {
  /** 추가된 TextOverlay id 목록 (해당 태그로 인해 새로 생성된 것). */
  text_overlay_ids: string[];
  /** 추가된 SceneFilter의 source_id 목록 (보통 1개). */
  filter_source_ids: string[];
  /** 추가된 GraphicOverlay의 source_id 목록 (보통 1개). */
  graphic_overlay_source_ids: string[];
  /** 추가된 SceneMotion의 source_id 목록 (보통 1개) — #207 1차. */
  motion_source_ids: string[];
  /** 보류 처리된 효과 종류 (사용자에게 "아직 적용 안 됨"을 표시하는 용도). */
  pending: PendingEffectKind[];
}

export interface ApplyTagsResult {
  /** 효과가 머지된 새 SceneCard (불변 원본은 보존). */
  scene: SceneCard;
  /** 이번 호출에서 추가된 효과의 출처 추적. */
  trace: TagApplyTrace;
}

/**
 * 씬에 태그를 추가하고 해당 태그의 효과 세트를 머지한다.
 *
 * - 이미 같은 태그가 `scene.tags`에 있으면 no-op (효과 중복 추가 방지).
 * - text_preset이 정의되어 있으면 빌트인 프리셋을 찾아 TextOverlay로 변환 후 `text_overlays`에 append.
 * - filter가 정의되어 있으면 SceneFilter로 변환 후 `filters`에 append (source_id 부여).
 * - graphic_overlay가 정의되어 있으면 GraphicOverlay로 변환 후 `graphic_overlays`에 append (source_id 부여).
 * - 알 수 없는 태그면 콘솔 경고 후 태그만 추가하여 반환.
 * - `_pending` 효과들은 trace에만 기록하고 실제 머지하지 않음 (motion / POV filter 등).
 *
 * @param scene 원본 SceneCard (불변)
 * @param addedTag 추가할 태그 (예: "horror")
 * @param dict 태그 사전 (loadTagDictionary()로 얻음)
 */
export function applySceneTags(
  scene: SceneCard,
  addedTag: string,
  dict: SceneTagDictionary,
): ApplyTagsResult {
  const trace: TagApplyTrace = {
    text_overlay_ids: [],
    filter_source_ids: [],
    graphic_overlay_source_ids: [],
    motion_source_ids: [],
    pending: [],
  };
  const existingTags = scene.tags ?? [];

  // 이미 있으면 무시 (출처 추적 정보 손실 방지 — 1차 구현에서는 단순 중복 차단)
  if (existingTags.includes(addedTag)) {
    return { scene, trace };
  }

  const def = dict.tags[addedTag];
  if (!def) {
    console.warn(`[sceneTags] 알 수 없는 태그: "${addedTag}". 사전에 정의되지 않음.`);
    return {
      scene: { ...scene, tags: [...existingTags, addedTag] },
      trace,
    };
  }

  const nextOverlays: TextOverlay[] = [...scene.text_overlays];
  const nextFilters: SceneFilter[] = [...(scene.filters ?? [])];
  const nextGraphicOverlays: GraphicOverlay[] = [...(scene.graphic_overlays ?? [])];
  const nextMotions: SceneMotion[] = [...(scene.motions ?? [])];

  // 1) text_preset 머지
  if (def.effects.text_preset) {
    const preset = lookupBuiltinPreset(def.effects.text_preset);
    if (preset) {
      const overlay = presetToOverlay(preset, addedTag);
      nextOverlays.push(overlay);
      trace.text_overlay_ids.push(overlay.id);
    } else {
      console.warn(
        `[sceneTags] 태그 "${addedTag}"가 참조한 text_preset "${def.effects.text_preset}"이 빌트인에 없음.`,
      );
    }
  }

  // 2) filter 머지 (#215)
  if (def.effects.filter) {
    const filter = templateToSceneFilter(def.effects.filter, addedTag);
    nextFilters.push(filter);
    if (filter.source_id) {
      trace.filter_source_ids.push(filter.source_id);
    }
  }

  // 3) graphic_overlay 머지 (#215)
  if (def.effects.graphic_overlay) {
    const overlay = templateToGraphicOverlay(def.effects.graphic_overlay, addedTag);
    nextGraphicOverlays.push(overlay);
    if (overlay.source_id) {
      trace.graphic_overlay_source_ids.push(overlay.source_id);
    }
  }

  // 4) motion 머지 (#207 1차 — shake/clip_zoom)
  if (def.effects.motion) {
    const motion = templateToSceneMotion(def.effects.motion, addedTag);
    nextMotions.push(motion);
    if (motion.source_id) {
      trace.motion_source_ids.push(motion.source_id);
    }
  }

  // 5) 미구현 효과 (POV filter 등): trace에만 기록
  if (def.effects._pending && def.effects._pending.length > 0) {
    trace.pending = [...def.effects._pending];
    console.info(
      `[sceneTags] 태그 "${addedTag}" 적용 — 보류된 효과 ${def.effects._pending.join(", ")}는 의존 이슈 머지 후 활성화.`,
    );
  }

  return {
    scene: {
      ...scene,
      tags: [...existingTags, addedTag],
      text_overlays: nextOverlays,
      filters: nextFilters,
      graphic_overlays: nextGraphicOverlays,
      motions: nextMotions,
    },
    trace,
  };
}

/**
 * 씬에서 태그를 제거하고, 해당 태그가 추가했던 효과만 제거한다.
 *
 * 식별 규칙:
 *  - text_overlays: `id`가 `tag-{removedTag}-` prefix로 시작하는 항목 제거.
 *    (presetToOverlay가 부여한 명명 규칙)
 *  - filters: `source_id`가 `tag-{removedTag}-filter`와 정확히 일치하는 항목 제거.
 *  - graphic_overlays: `source_id`가 `tag-{removedTag}-graphic`과 정확히 일치하는 항목 제거.
 *  - motions: `source_id`가 `tag-{removedTag}-motion`과 정확히 일치하는 항목 제거 (#207).
 *
 * 유저가 수동 추가한 효과 (id/source_id prefix 미일치)는 보존된다.
 *
 * @param scene 원본 SceneCard (불변)
 * @param removedTag 제거할 태그
 */
export function removeSceneTag(scene: SceneCard, removedTag: string): SceneCard {
  const existingTags = scene.tags ?? [];
  if (!existingTags.includes(removedTag)) {
    return scene;
  }

  const overlayPrefix = `tag-${removedTag}-`;
  const filterSourceId = tagFilterSourceId(removedTag);
  const graphicSourceId = tagGraphicSourceId(removedTag);
  const motionSourceId = tagMotionSourceId(removedTag);

  const filteredOverlays = scene.text_overlays.filter(
    (o) => !o.id.startsWith(overlayPrefix),
  );
  const filteredFilters = (scene.filters ?? []).filter(
    (f) => f.source_id !== filterSourceId,
  );
  const filteredGraphicOverlays = (scene.graphic_overlays ?? []).filter(
    (g) => g.source_id !== graphicSourceId,
  );
  const filteredMotions = (scene.motions ?? []).filter(
    (m) => m.source_id !== motionSourceId,
  );

  return {
    ...scene,
    tags: existingTags.filter((t) => t !== removedTag),
    text_overlays: filteredOverlays,
    filters: filteredFilters,
    graphic_overlays: filteredGraphicOverlays,
    motions: filteredMotions,
  };
}

/**
 * 씬에 적용된 모든 태그의 출처 정보를 사전 기준으로 조회한다.
 * 칩 hover/펼침 패널에서 "이 태그가 어떤 효과를 적용했나"를 보여주는 용도.
 *
 * 반환: 태그명 → 정의(label/description/effects). 사전에 없는 태그는 누락.
 */
export function getTagSources(
  scene: SceneCard,
  dict: SceneTagDictionary,
): Record<string, SceneTagDef> {
  const result: Record<string, SceneTagDef> = {};
  for (const tag of scene.tags ?? []) {
    const def = dict.tags[tag];
    if (def) {
      result[tag] = def;
    }
  }
  return result;
}
