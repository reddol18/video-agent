/**
 * TagDictionaryModal — 씬 태그 사전 편집 모달 (이슈 #214 / #239)
 *
 * 기능:
 *  - 좌측: 사전 항목 목록 (빌트인 + 유저 정의 mix). 빌트인은 자물쇠 아이콘으로 구분.
 *  - 우측: 선택된 항목 편집 폼
 *      · id (신규일 때만 편집 가능 — 영문 소문자 + 하이픈 검증)
 *      · label / description
 *      · effects.text_preset 드롭다운 (빌트인 + 유저 프리셋, IndexedDB에서 로드)
 *      · effects.filter / graphic_overlay / motion: 정식 폼 UI (#239).
 *        각 효과는 "사용" 체크박스로 토글, 타입별 동적 params 슬라이더/select/input.
 *  - 신규 추가 / 저장 / 삭제 / 복제(빌트인을 유저 정의로 복사) / 닫기.
 *  - 빌트인 항목은 편집/삭제 불가능 (UI에서 disable). "복제" 버튼으로 유저 정의 카피 생성.
 *  - 저장/삭제 후 onChanged 콜백 호출 — 호출 측이 SceneTagsEditor 사전 재로드 트리거.
 *
 * 의존:
 *  - `lib/sceneTags.ts` UserSceneTag/SceneTagEffects/SceneTagDef
 *    및 SceneTagFilterEffect/SceneTagGraphicOverlayEffect/SceneTagMotionEffect
 *  - `lib/db.ts` dbListUserSceneTags / dbSaveUserSceneTag / dbDeleteUserSceneTag
 *  - `lib/textPresetSeeds.ts` BUILTIN_TEXT_PRESETS
 *  - `lib/db.ts` dbListOverlayPresets (유저 프리셋 머지)
 *
 * #239 구현 노트:
 *  - filter/graphic_overlay/motion 각 1개씩만 (현재 schema가 단일 필드).
 *  - 필터/오버레이/모션의 타입별 슬라이더 범위·기본값은 Editor.tsx의 직접 편집 패널과 동일.
 *  - pov 필터의 keyframes는 1차에서 raw JSON textarea로 단순 표시 (편집 UI는 후속).
 *  - 유저가 textarea로 저장했던 기존 데이터도 폼에 정확히 역직렬화되어야 한다.
 */
import { useEffect, useMemo, useState } from "react";
import type {
  TextOverlayPreset,
  GraphicOverlayType,
  SceneFilterType,
  MotionType,
} from "../types";
import {
  type SceneTagEffects,
  type SceneTagFilterEffect,
  type SceneTagGraphicOverlayEffect,
  type SceneTagMotionEffect,
  type UserSceneTag,
  getBuiltinTagDictionary,
} from "../lib/sceneTags";
import {
  dbDeleteUserSceneTag,
  dbListOverlayPresets,
  dbListUserSceneTags,
  dbSaveUserSceneTag,
} from "../lib/db";
import { BUILTIN_TEXT_PRESETS } from "../lib/textPresetSeeds";
import { ui } from "../store/ui";

interface Props {
  onClose: () => void;
  /** 사전이 저장/삭제되어 변경되었을 때 호출. 호출 측은 사전을 다시 로드해야 한다. */
  onChanged?: () => void;
}

// ─── 검증 ────────────────────────────────────────────────────────────────

/** 영문 소문자 + 하이픈만 허용 (선두/말미 하이픈 금지). */
const VALID_ID_RE = /^[a-z]+(?:-[a-z]+)*$/;

function isValidId(id: string): boolean {
  return VALID_ID_RE.test(id);
}

function clamp(v: number, lo: number, hi: number): number {
  if (Number.isNaN(v)) return lo;
  return Math.max(lo, Math.min(hi, v));
}

/** "#rrggbb" 형식 hex color 검증. */
function isValidHexColor(c: string): boolean {
  return /^#[0-9a-fA-F]{6}$/.test(c);
}

// ─── 폼 상태 ──────────────────────────────────────────────────────────────

interface FormState {
  id: string;
  label: string;
  description: string;
  text_preset: string; // 빈 문자열이면 미지정
  // ── 필터 폼 ──
  filter_enabled: boolean;
  filter_type: SceneFilterType;
  filter_intensity: number;
  filter_params: Record<string, unknown>;
  /** pov 키프레임 raw JSON (1차에서는 textarea로 fallback). */
  filter_pov_keyframes_json: string;
  // ── 그래픽 오버레이 폼 ──
  graphic_enabled: boolean;
  graphic_type: GraphicOverlayType;
  graphic_start_sec: number;
  graphic_end_sec: number | null;
  graphic_x: number;
  graphic_y: number;
  graphic_params: Record<string, unknown>;
  // ── 모션 폼 ──
  motion_enabled: boolean;
  motion_type: MotionType;
  motion_start_sec: number;
  motion_duration_sec: number;
  motion_params: Record<string, unknown>;
}

const EMPTY_FORM: FormState = {
  id: "",
  label: "",
  description: "",
  text_preset: "",
  // filter
  filter_enabled: false,
  filter_type: "horror",
  filter_intensity: 0.7,
  filter_params: {},
  filter_pov_keyframes_json: "",
  // graphic_overlay
  graphic_enabled: false,
  graphic_type: "spotlight",
  graphic_start_sec: 0,
  graphic_end_sec: null,
  graphic_x: 0.5,
  graphic_y: 0.5,
  graphic_params: {},
  // motion
  motion_enabled: false,
  motion_type: "shake",
  motion_start_sec: 0,
  motion_duration_sec: 0.6,
  motion_params: {},
};

// ── 타입별 기본 파라미터 (Editor.tsx의 handleAddFilter/Graphic/Motion과 동일) ──

function defaultFilterParams(t: SceneFilterType): Record<string, unknown> {
  if (t === "pov") {
    return { center_x: 0.5, center_y: 0.5, radius: 0.35, feather: 80 };
  }
  return {};
}

function defaultGraphicParams(t: GraphicOverlayType): {
  params: Record<string, unknown>;
  x: number;
  y: number;
} {
  if (t === "spotlight") {
    return { params: { radius: 0.2, darkness: 0.6, feather: 40 }, x: 0.5, y: 0.5 };
  }
  if (t === "alert") {
    return {
      params: { text: "경고", style: "warning", width: 0.4 },
      x: 0.5,
      y: 0.15,
    };
  }
  if (t === "target") {
    return {
      params: { marker_type: "crosshair", size: 0.12, color: "#ff3030", rotation: 0 },
      x: 0.5,
      y: 0.5,
    };
  }
  if (t === "reaction") {
    return {
      params: { icon_type: "thunder", size: 0.15, bounce: false },
      x: 0.5,
      y: 0.3,
    };
  }
  // smell
  return {
    params: { width: 0.2, height: 0.3, color: "green", intensity: 0.7, loop: false },
    x: 0.5,
    y: 0.65,
  };
}

function defaultMotionParams(t: MotionType): {
  params: Record<string, unknown>;
  duration_sec: number;
} {
  if (t === "shake") {
    return { params: { intensity: 0.6 }, duration_sec: 0.6 };
  }
  if (t === "clip_zoom") {
    return {
      params: { target_x: 0.5, target_y: 0.5, start_scale: 1.0, end_scale: 1.3 },
      duration_sec: 1.5,
    };
  }
  // slowmo
  return {
    params: { speed: 0.5, interpolate: false, freeze_at_end_sec: 0.0 },
    duration_sec: 10.0,
  };
}

// ─── effects ⇄ form 변환 ──────────────────────────────────────────────────

function effectsToForm(effects: SceneTagEffects | undefined): Partial<FormState> {
  if (!effects) return {};
  const out: Partial<FormState> = {
    text_preset: effects.text_preset ?? "",
  };
  // filter
  if (effects.filter) {
    out.filter_enabled = true;
    out.filter_type = effects.filter.filter_type;
    out.filter_intensity = effects.filter.intensity;
    const params = { ...(effects.filter.params ?? {}) };
    // pov keyframes는 raw JSON으로 분리해서 textarea fallback
    if (effects.filter.filter_type === "pov" && Array.isArray(params.keyframes)) {
      out.filter_pov_keyframes_json = JSON.stringify(params.keyframes, null, 2);
      delete params.keyframes;
    } else {
      out.filter_pov_keyframes_json = "";
    }
    out.filter_params = params;
  }
  // graphic_overlay
  if (effects.graphic_overlay) {
    const g = effects.graphic_overlay;
    out.graphic_enabled = true;
    out.graphic_type = g.type;
    out.graphic_start_sec = g.start_sec ?? 0;
    out.graphic_end_sec = g.end_sec ?? null;
    out.graphic_x = g.x ?? 0.5;
    out.graphic_y = g.y ?? 0.5;
    out.graphic_params = { ...(g.params ?? {}) };
  }
  // motion
  if (effects.motion) {
    const m = effects.motion;
    out.motion_enabled = true;
    out.motion_type = m.motion_type;
    out.motion_start_sec = m.start_sec ?? 0;
    out.motion_duration_sec = m.duration_sec ?? 1.0;
    out.motion_params = { ...(m.params ?? {}) };
  }
  return out;
}

/**
 * 폼을 SceneTagEffects로 변환. POV keyframes JSON parse 오류 시 throw.
 */
function formToEffects(form: FormState): SceneTagEffects {
  const out: SceneTagEffects = {};
  if (form.text_preset.trim()) {
    out.text_preset = form.text_preset.trim();
  }
  if (form.filter_enabled) {
    const params: Record<string, unknown> = { ...form.filter_params };
    // POV keyframes JSON 병합
    if (form.filter_type === "pov" && form.filter_pov_keyframes_json.trim()) {
      try {
        const parsed = JSON.parse(form.filter_pov_keyframes_json);
        if (!Array.isArray(parsed)) {
          throw new Error("pov.keyframes는 배열이어야 합니다.");
        }
        params.keyframes = parsed;
      } catch (err) {
        throw new Error(`filter.params.keyframes JSON 파싱 실패: ${(err as Error).message}`);
      }
    }
    const filter: SceneTagFilterEffect = {
      filter_type: form.filter_type,
      intensity: clamp(form.filter_intensity, 0, 1),
      params,
    };
    out.filter = filter;
  }
  if (form.graphic_enabled) {
    const graphic: SceneTagGraphicOverlayEffect = {
      type: form.graphic_type,
      start_sec: form.graphic_start_sec,
      end_sec: form.graphic_end_sec,
      x: clamp(form.graphic_x, 0, 1),
      y: clamp(form.graphic_y, 0, 1),
      params: { ...form.graphic_params },
    };
    out.graphic_overlay = graphic;
  }
  if (form.motion_enabled) {
    const motion: SceneTagMotionEffect = {
      motion_type: form.motion_type,
      start_sec: form.motion_start_sec,
      duration_sec: form.motion_duration_sec,
      params: { ...form.motion_params },
    };
    out.motion = motion;
  }
  return out;
}

// ─── 목록 항목 표현 ───────────────────────────────────────────────────────

interface DictItem {
  id: string;
  label: string;
  description: string;
  effects: SceneTagEffects;
  source: "builtin" | "user";
  /** 유저가 빌트인을 오버라이드한 경우 true (양쪽에 같은 id 존재). */
  overridesBuiltin?: boolean;
}

function buildItems(userTags: UserSceneTag[]): DictItem[] {
  const builtin = getBuiltinTagDictionary().tags;
  const userIds = new Set(userTags.map((u) => u.id));
  const items: DictItem[] = [];

  // 빌트인 — 유저가 오버라이드하지 않은 항목만 표시 (유저 항목이 같은 ID로 표시됨).
  for (const [id, def] of Object.entries(builtin)) {
    if (userIds.has(id)) continue; // 유저 정의로 표시됨
    items.push({
      id,
      label: def.label,
      description: def.description,
      effects: def.effects,
      source: "builtin",
    });
  }

  // 유저 정의 — 빌트인 오버라이드 여부 표시.
  for (const u of userTags) {
    items.push({
      id: u.id,
      label: u.label,
      description: u.description ?? "",
      effects: u.effects ?? {},
      source: "user",
      overridesBuiltin: builtin[u.id] !== undefined,
    });
  }

  // 정렬 — 빌트인 먼저, 그 안에서 id 알파벳순. 유저 정의는 그 다음.
  items.sort((a, b) => {
    if (a.source !== b.source) return a.source === "builtin" ? -1 : 1;
    return a.id.localeCompare(b.id);
  });
  return items;
}

// ─── 컴포넌트 ─────────────────────────────────────────────────────────────

export default function TagDictionaryModal({ onClose, onChanged }: Props) {
  const [userTags, setUserTags] = useState<UserSceneTag[]>([]);
  const [overlayPresets, setOverlayPresets] = useState<TextOverlayPreset[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [isNew, setIsNew] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<boolean>(false);

  // 초기 로드 — 유저 태그 + 텍스트 프리셋
  useEffect(() => {
    let cancelled = false;
    Promise.all([dbListUserSceneTags(), dbListOverlayPresets()])
      .then(([tags, presets]) => {
        if (cancelled) return;
        setUserTags(tags);
        setOverlayPresets(presets);
      })
      .catch((err) => {
        console.error("[TagDictionaryModal] 초기 로드 실패:", err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const items = useMemo(() => buildItems(userTags), [userTags]);
  const selectedItem = useMemo(
    () => items.find((it) => it.id === selectedId) ?? null,
    [items, selectedId],
  );

  // 텍스트 프리셋 옵션 (빌트인 + 유저, 중복 id는 유저가 후순위)
  const presetOptions = useMemo(() => {
    const map = new Map<string, { id: string; name: string }>();
    for (const p of BUILTIN_TEXT_PRESETS) {
      map.set(p.id, { id: p.id, name: p.name });
    }
    for (const p of overlayPresets) {
      if (!map.has(p.id)) {
        map.set(p.id, { id: p.id, name: p.name });
      }
    }
    return Array.from(map.values());
  }, [overlayPresets]);

  function selectItem(item: DictItem) {
    setSelectedId(item.id);
    setIsNew(false);
    setError(null);
    setForm({
      ...EMPTY_FORM,
      ...effectsToForm(item.effects),
      id: item.id,
      label: item.label,
      description: item.description,
    });
  }

  function startNew() {
    setSelectedId(null);
    setIsNew(true);
    setError(null);
    setForm({ ...EMPTY_FORM });
  }

  function duplicateBuiltin(item: DictItem) {
    // 빌트인을 복제해 유저 정의로 만들기 — id에 "-copy" 접미사
    setSelectedId(null);
    setIsNew(true);
    setError(null);
    setForm({
      ...EMPTY_FORM,
      ...effectsToForm(item.effects),
      id: `${item.id}-copy`,
      label: `${item.label} (복제)`,
      description: item.description,
    });
  }

  async function handleSave() {
    setError(null);
    const id = form.id.trim();
    const label = form.label.trim();
    if (!id) {
      setError("id를 입력하세요.");
      return;
    }
    if (!isValidId(id)) {
      setError("id는 영문 소문자와 하이픈만 사용할 수 있습니다 (예: my-tag).");
      return;
    }
    if (!label) {
      setError("label을 입력하세요.");
      return;
    }
    // 신규 모드인데 이미 유저 정의에 존재하면 차단 (덮어쓰기 방지)
    if (isNew && userTags.some((u) => u.id === id)) {
      setError(`id "${id}"는 이미 유저 정의로 존재합니다. 덮어쓰기는 목록에서 선택 후 수정하세요.`);
      return;
    }
    // alert 텍스트 필수 검증
    if (form.graphic_enabled && form.graphic_type === "alert") {
      const t = String(form.graphic_params.text ?? "").trim();
      if (!t) {
        setError("graphic_overlay(alert)의 표시 텍스트를 입력하세요.");
        return;
      }
    }
    let effects: SceneTagEffects;
    try {
      effects = formToEffects(form);
    } catch (err) {
      setError((err as Error).message);
      return;
    }
    setSaving(true);
    try {
      const now = new Date().toISOString();
      const tag: UserSceneTag = {
        id,
        label,
        description: form.description.trim() || undefined,
        effects,
        created_at: now, // dbSaveUserSceneTag가 기존 created_at 보존
        updated_at: now,
      };
      await dbSaveUserSceneTag(tag);
      const updated = await dbListUserSceneTags();
      setUserTags(updated);
      setSelectedId(id);
      setIsNew(false);
      onChanged?.();
    } catch (err) {
      setError(`저장 실패: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!selectedItem || selectedItem.source !== "user") return;
    const ok = await ui.confirm({
      title: "태그 삭제",
      message: `태그 "${selectedItem.id}"를 삭제하시겠습니까?`,
      variant: "danger",
      confirmLabel: "삭제",
      cancelLabel: "취소",
    });
    if (!ok) return;
    setSaving(true);
    try {
      await dbDeleteUserSceneTag(selectedItem.id);
      const updated = await dbListUserSceneTags();
      setUserTags(updated);
      setSelectedId(null);
      setForm(EMPTY_FORM);
      setIsNew(false);
      onChanged?.();
    } catch (err) {
      setError(`삭제 실패: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  function updateForm<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function patchFilterParams(patch: Record<string, unknown>) {
    setForm((prev) => ({
      ...prev,
      filter_params: { ...prev.filter_params, ...patch },
    }));
  }

  function patchGraphicParams(patch: Record<string, unknown>) {
    setForm((prev) => ({
      ...prev,
      graphic_params: { ...prev.graphic_params, ...patch },
    }));
  }

  function patchMotionParams(patch: Record<string, unknown>) {
    setForm((prev) => ({
      ...prev,
      motion_params: { ...prev.motion_params, ...patch },
    }));
  }

  /** 필터 타입 변경 — 새 타입의 기본 파라미터로 초기화 (사용자가 일관된 입력을 받도록). */
  function handleChangeFilterType(t: SceneFilterType) {
    setForm((prev) => ({
      ...prev,
      filter_type: t,
      filter_params: defaultFilterParams(t),
      filter_pov_keyframes_json: t === "pov" ? prev.filter_pov_keyframes_json : "",
    }));
  }

  /** 그래픽 오버레이 타입 변경 — 새 타입의 기본 파라미터로 초기화. */
  function handleChangeGraphicType(t: GraphicOverlayType) {
    const def = defaultGraphicParams(t);
    setForm((prev) => ({
      ...prev,
      graphic_type: t,
      graphic_params: def.params,
      graphic_x: def.x,
      graphic_y: def.y,
    }));
  }

  /** 모션 타입 변경 — 새 타입의 기본 파라미터로 초기화. */
  function handleChangeMotionType(t: MotionType) {
    const def = defaultMotionParams(t);
    setForm((prev) => ({
      ...prev,
      motion_type: t,
      motion_params: def.params,
      motion_duration_sec: def.duration_sec,
    }));
  }

  /** "사용" 토글 ON 시 빈 params면 기본값 주입. */
  function handleToggleFilter(on: boolean) {
    setForm((prev) => {
      if (!on) return { ...prev, filter_enabled: false };
      const params =
        Object.keys(prev.filter_params).length === 0
          ? defaultFilterParams(prev.filter_type)
          : prev.filter_params;
      return { ...prev, filter_enabled: true, filter_params: params };
    });
  }

  function handleToggleGraphic(on: boolean) {
    setForm((prev) => {
      if (!on) return { ...prev, graphic_enabled: false };
      if (Object.keys(prev.graphic_params).length === 0) {
        const def = defaultGraphicParams(prev.graphic_type);
        return {
          ...prev,
          graphic_enabled: true,
          graphic_params: def.params,
          graphic_x: def.x,
          graphic_y: def.y,
        };
      }
      return { ...prev, graphic_enabled: true };
    });
  }

  function handleToggleMotion(on: boolean) {
    setForm((prev) => {
      if (!on) return { ...prev, motion_enabled: false };
      if (Object.keys(prev.motion_params).length === 0) {
        const def = defaultMotionParams(prev.motion_type);
        return {
          ...prev,
          motion_enabled: true,
          motion_params: def.params,
          motion_duration_sec: def.duration_sec,
        };
      }
      return { ...prev, motion_enabled: true };
    });
  }

  // 빌트인 선택 시 폼은 readonly 표시
  const isReadOnly = !!selectedItem && selectedItem.source === "builtin" && !isNew;

  return (
    <div style={styles.overlay} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div style={styles.modal}>
        <div style={styles.header}>
          <h3 style={{ margin: 0, color: "#fff" }}>씬 태그 사전 편집</h3>
          <button style={styles.closeBtn} onClick={onClose} title="닫기">
            ✕
          </button>
        </div>

        <div style={styles.body}>
          {/* 좌측 목록 */}
          <div style={styles.listPanel}>
            <div style={styles.listHeader}>
              <span style={styles.listTitle}>태그 목록 ({items.length})</span>
              <button style={styles.newBtn} onClick={startNew} title="새 태그 추가">
                + 새 태그
              </button>
            </div>
            <div style={styles.listScroll}>
              {items.map((item) => {
                const active = item.id === selectedId && !isNew;
                return (
                  <div
                    key={item.id}
                    style={{
                      ...styles.listItem,
                      ...(active ? styles.listItemActive : {}),
                    }}
                    onClick={() => selectItem(item)}
                  >
                    <div style={styles.listItemLabel}>
                      {item.label}{" "}
                      <span style={styles.listItemId}>({item.id})</span>
                    </div>
                    <div style={styles.listItemMeta}>
                      {item.source === "builtin" ? (
                        <span style={styles.builtinBadge} title="빌트인 (편집 불가)">
                          빌트인
                        </span>
                      ) : (
                        <span style={styles.userBadge}>
                          유저
                          {item.overridesBuiltin ? " · 오버라이드" : ""}
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* 우측 편집 폼 */}
          <div style={styles.formPanel}>
            {!selectedItem && !isNew ? (
              <div style={styles.emptyHint}>
                좌측에서 태그를 선택하거나 "+ 새 태그"를 눌러 추가하세요.
              </div>
            ) : (
              <>
                <div style={styles.formField}>
                  <label style={styles.formLabel}>id</label>
                  <input
                    style={{
                      ...styles.input,
                      ...(isReadOnly || !isNew ? styles.inputReadOnly : {}),
                    }}
                    value={form.id}
                    disabled={!isNew || isReadOnly}
                    onChange={(e) => updateForm("id", e.target.value.toLowerCase())}
                    placeholder="my-tag (영문 소문자 + 하이픈)"
                  />
                  <span style={styles.formHint}>
                    영문 소문자 + 하이픈만 가능. 한 번 저장하면 변경 불가.
                  </span>
                </div>

                <div style={styles.formField}>
                  <label style={styles.formLabel}>label (표시명)</label>
                  <input
                    style={{
                      ...styles.input,
                      ...(isReadOnly ? styles.inputReadOnly : {}),
                    }}
                    value={form.label}
                    disabled={isReadOnly}
                    onChange={(e) => updateForm("label", e.target.value)}
                    placeholder="내 태그"
                  />
                </div>

                <div style={styles.formField}>
                  <label style={styles.formLabel}>description (설명)</label>
                  <textarea
                    style={{
                      ...styles.textarea,
                      ...(isReadOnly ? styles.inputReadOnly : {}),
                    }}
                    value={form.description}
                    disabled={isReadOnly}
                    onChange={(e) => updateForm("description", e.target.value)}
                    rows={2}
                    placeholder="칩 hover 시 표시되는 설명"
                  />
                </div>

                <div style={styles.formField}>
                  <label style={styles.formLabel}>effects.text_preset</label>
                  <select
                    style={{
                      ...styles.input,
                      ...(isReadOnly ? styles.inputReadOnly : {}),
                    }}
                    value={form.text_preset}
                    disabled={isReadOnly}
                    onChange={(e) => updateForm("text_preset", e.target.value)}
                  >
                    <option value="">(없음)</option>
                    {presetOptions.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} ({p.id})
                      </option>
                    ))}
                  </select>
                </div>

                {/* ── effects.filter 폼 ── */}
                <FilterEffectForm
                  form={form}
                  readOnly={isReadOnly}
                  onToggle={handleToggleFilter}
                  onChangeType={handleChangeFilterType}
                  onChangeIntensity={(v) => updateForm("filter_intensity", v)}
                  onPatchParams={patchFilterParams}
                  onChangeKeyframesJson={(v) => updateForm("filter_pov_keyframes_json", v)}
                />

                {/* ── effects.graphic_overlay 폼 ── */}
                <GraphicOverlayEffectForm
                  form={form}
                  readOnly={isReadOnly}
                  onToggle={handleToggleGraphic}
                  onChangeType={handleChangeGraphicType}
                  onChangeStartSec={(v) => updateForm("graphic_start_sec", v)}
                  onChangeEndSec={(v) => updateForm("graphic_end_sec", v)}
                  onChangeX={(v) => updateForm("graphic_x", v)}
                  onChangeY={(v) => updateForm("graphic_y", v)}
                  onPatchParams={patchGraphicParams}
                />

                {/* ── effects.motion 폼 ── */}
                <MotionEffectForm
                  form={form}
                  readOnly={isReadOnly}
                  onToggle={handleToggleMotion}
                  onChangeType={handleChangeMotionType}
                  onChangeStartSec={(v) => updateForm("motion_start_sec", v)}
                  onChangeDurationSec={(v) => updateForm("motion_duration_sec", v)}
                  onPatchParams={patchMotionParams}
                />

                {error && <div style={styles.errorBox}>{error}</div>}

                <div style={styles.formActions}>
                  {isReadOnly ? (
                    <>
                      <span style={styles.builtinNotice}>
                        빌트인 태그는 직접 편집할 수 없습니다. "복제하여 편집"으로 유저 정의 카피를 만드세요.
                      </span>
                      <button
                        style={styles.duplicateBtn}
                        onClick={() => selectedItem && duplicateBuiltin(selectedItem)}
                      >
                        복제하여 편집
                      </button>
                    </>
                  ) : (
                    <>
                      {selectedItem?.source === "user" && !isNew && (
                        <button
                          style={styles.deleteBtn}
                          onClick={handleDelete}
                          disabled={saving}
                        >
                          삭제
                        </button>
                      )}
                      <div style={{ flex: 1 }} />
                      <button
                        style={styles.saveBtn}
                        onClick={handleSave}
                        disabled={saving}
                      >
                        {saving ? "저장 중..." : isNew ? "추가" : "저장"}
                      </button>
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── 하위 폼 컴포넌트 ─────────────────────────────────────────────────────

interface FilterFormProps {
  form: FormState;
  readOnly: boolean;
  onToggle: (on: boolean) => void;
  onChangeType: (t: SceneFilterType) => void;
  onChangeIntensity: (v: number) => void;
  onPatchParams: (patch: Record<string, unknown>) => void;
  onChangeKeyframesJson: (v: string) => void;
}

const FILTER_TYPE_OPTIONS: { value: SceneFilterType; label: string }[] = [
  { value: "flashback", label: "flashback (회상)" },
  { value: "horror", label: "horror (공포)" },
  { value: "vivid", label: "vivid (생생함)" },
  { value: "nightvision", label: "nightvision (야간)" },
  { value: "pov", label: "pov (시점)" },
];

function FilterEffectForm({
  form,
  readOnly,
  onToggle,
  onChangeType,
  onChangeIntensity,
  onPatchParams,
  onChangeKeyframesJson,
}: FilterFormProps) {
  const p = form.filter_params;
  return (
    <div style={styles.effectGroup}>
      <label style={styles.effectGroupHeader}>
        <input
          type="checkbox"
          checked={form.filter_enabled}
          disabled={readOnly}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span style={styles.effectGroupTitle}>effects.filter (씬 필터)</span>
      </label>
      {form.filter_enabled && (
        <div style={styles.effectBody}>
          <FormRow label="filter_type">
            <select
              style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
              value={form.filter_type}
              disabled={readOnly}
              onChange={(e) => onChangeType(e.target.value as SceneFilterType)}
            >
              {FILTER_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </FormRow>
          <RangeRow
            label="intensity"
            min={0}
            max={1}
            step={0.05}
            value={form.filter_intensity}
            onChange={onChangeIntensity}
            format={(v) => `${Math.round(v * 100)}%`}
            disabled={readOnly}
          />
          {/* POV 전용 파라미터 */}
          {form.filter_type === "pov" && (
            <>
              <RangeRow
                label="center_x"
                min={0}
                max={1}
                step={0.01}
                value={Number(p.center_x ?? 0.5)}
                onChange={(v) => onPatchParams({ center_x: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="center_y"
                min={0}
                max={1}
                step={0.01}
                value={Number(p.center_y ?? 0.5)}
                onChange={(v) => onPatchParams({ center_y: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="radius"
                min={0.05}
                max={0.8}
                step={0.01}
                value={Number(p.radius ?? 0.35)}
                onChange={(v) => onPatchParams({ radius: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="feather"
                min={0}
                max={200}
                step={2}
                value={Number(p.feather ?? 80)}
                onChange={(v) => onPatchParams({ feather: Math.round(v) })}
                format={(v) => `${Math.round(v)}px`}
                disabled={readOnly}
              />
              <FormRow label="keyframes (JSON, 선택)">
                <textarea
                  style={{
                    ...styles.textareaMono,
                    ...(readOnly ? styles.inputReadOnly : {}),
                  }}
                  value={form.filter_pov_keyframes_json}
                  disabled={readOnly}
                  rows={3}
                  placeholder='(비어있으면 정적 마스크) 또는 [{"t":0,"center_x":0.5,"center_y":0.5,"radius":0.3},...]'
                  onChange={(e) => onChangeKeyframesJson(e.target.value)}
                />
                <span style={styles.formHint}>
                  2개 이상의 키프레임을 넣으면 동적 보간 적용. 비우면 정적 center_x/y/radius 사용.
                </span>
              </FormRow>
            </>
          )}
        </div>
      )}
    </div>
  );
}

interface GraphicFormProps {
  form: FormState;
  readOnly: boolean;
  onToggle: (on: boolean) => void;
  onChangeType: (t: GraphicOverlayType) => void;
  onChangeStartSec: (v: number) => void;
  onChangeEndSec: (v: number | null) => void;
  onChangeX: (v: number) => void;
  onChangeY: (v: number) => void;
  onPatchParams: (patch: Record<string, unknown>) => void;
}

const GRAPHIC_TYPE_OPTIONS: { value: GraphicOverlayType; label: string }[] = [
  { value: "spotlight", label: "spotlight (스포트라이트)" },
  { value: "alert", label: "alert (경고 텍스트)" },
  { value: "target", label: "target (조준 마커)" },
  { value: "reaction", label: "reaction (리액션 아이콘)" },
  { value: "smell", label: "smell (냄새/연기)" },
];

function GraphicOverlayEffectForm({
  form,
  readOnly,
  onToggle,
  onChangeType,
  onChangeStartSec,
  onChangeEndSec,
  onChangeX,
  onChangeY,
  onPatchParams,
}: GraphicFormProps) {
  const p = form.graphic_params;
  const t = form.graphic_type;
  return (
    <div style={styles.effectGroup}>
      <label style={styles.effectGroupHeader}>
        <input
          type="checkbox"
          checked={form.graphic_enabled}
          disabled={readOnly}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span style={styles.effectGroupTitle}>effects.graphic_overlay (그래픽 오버레이)</span>
      </label>
      {form.graphic_enabled && (
        <div style={styles.effectBody}>
          <FormRow label="type">
            <select
              style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
              value={t}
              disabled={readOnly}
              onChange={(e) => onChangeType(e.target.value as GraphicOverlayType)}
            >
              {GRAPHIC_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </FormRow>
          {/* 공통: 시간 */}
          <div style={{ display: "flex", gap: 8 }}>
            <FormRow label="start_sec" style={{ flex: 1 }}>
              <input
                type="number"
                step={0.1}
                value={form.graphic_start_sec}
                disabled={readOnly}
                onChange={(e) => onChangeStartSec(parseFloat(e.target.value) || 0)}
                style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
              />
            </FormRow>
            <FormRow label="end_sec (빈칸=끝까지)" style={{ flex: 1 }}>
              <input
                type="number"
                step={0.1}
                value={form.graphic_end_sec ?? ""}
                disabled={readOnly}
                onChange={(e) =>
                  onChangeEndSec(e.target.value === "" ? null : parseFloat(e.target.value))
                }
                style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
              />
            </FormRow>
          </div>
          {/* 공통: 위치 */}
          <RangeRow
            label="x (0=좌)"
            min={0}
            max={1}
            step={0.01}
            value={form.graphic_x}
            onChange={onChangeX}
            format={(v) => v.toFixed(2)}
            disabled={readOnly}
          />
          <RangeRow
            label="y (0=상)"
            min={0}
            max={1}
            step={0.01}
            value={form.graphic_y}
            onChange={onChangeY}
            format={(v) => v.toFixed(2)}
            disabled={readOnly}
          />
          {/* 타입별 params */}
          {t === "spotlight" && (
            <>
              <RangeRow
                label="radius"
                min={0.05}
                max={0.6}
                step={0.01}
                value={Number(p.radius ?? 0.2)}
                onChange={(v) => onPatchParams({ radius: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="darkness"
                min={0}
                max={1}
                step={0.05}
                value={Number(p.darkness ?? 0.6)}
                onChange={(v) => onPatchParams({ darkness: v })}
                format={(v) => `${Math.round(v * 100)}%`}
                disabled={readOnly}
              />
              <RangeRow
                label="feather"
                min={0}
                max={120}
                step={2}
                value={Number(p.feather ?? 40)}
                onChange={(v) => onPatchParams({ feather: Math.round(v) })}
                format={(v) => `${Math.round(v)}px`}
                disabled={readOnly}
              />
            </>
          )}
          {t === "alert" && (
            <>
              <FormRow label="text (표시 문자)">
                <input
                  type="text"
                  value={String(p.text ?? "")}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ text: e.target.value })}
                  style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                  placeholder="경고"
                />
              </FormRow>
              <FormRow label="style">
                <select
                  value={String(p.style ?? "warning")}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ style: e.target.value })}
                  style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                >
                  <option value="warning">warning (경고)</option>
                  <option value="info">info (정보)</option>
                  <option value="error">error (오류)</option>
                </select>
              </FormRow>
              <RangeRow
                label="width"
                min={0.1}
                max={0.9}
                step={0.01}
                value={Number(p.width ?? 0.4)}
                onChange={(v) => onPatchParams({ width: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
            </>
          )}
          {t === "target" && (
            <>
              <FormRow label="marker_type">
                <select
                  value={String(p.marker_type ?? "crosshair")}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ marker_type: e.target.value })}
                  style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                >
                  <option value="crosshair">crosshair (십자선)</option>
                  <option value="arrow">arrow (화살표)</option>
                </select>
              </FormRow>
              <RangeRow
                label="size"
                min={0.03}
                max={0.5}
                step={0.01}
                value={Number(p.size ?? 0.12)}
                onChange={(v) => onPatchParams({ size: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <HexColorRow
                label="color"
                value={String(p.color ?? "#ff3030")}
                onChange={(v) => onPatchParams({ color: v })}
                disabled={readOnly}
              />
              {String(p.marker_type ?? "crosshair") === "arrow" && (
                <RangeRow
                  label="rotation"
                  min={0}
                  max={360}
                  step={5}
                  value={Number(p.rotation ?? 0)}
                  onChange={(v) => onPatchParams({ rotation: v })}
                  format={(v) => `${Math.round(v)}°`}
                  disabled={readOnly}
                />
              )}
            </>
          )}
          {t === "reaction" && (
            <>
              <FormRow label="icon_type">
                <select
                  value={String(p.icon_type ?? "thunder")}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ icon_type: e.target.value })}
                  style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                >
                  <option value="thunder">thunder (번개)</option>
                  <option value="sweat">sweat (땀)</option>
                  <option value="question">question (물음표)</option>
                  <option value="exclamation">exclamation (느낌표)</option>
                </select>
              </FormRow>
              <RangeRow
                label="size"
                min={0.05}
                max={0.5}
                step={0.01}
                value={Number(p.size ?? 0.15)}
                onChange={(v) => onPatchParams({ size: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <FormRow label="color (옵션, 빈칸=기본색)">
                <input
                  type="text"
                  value={String(p.color ?? "")}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ color: e.target.value })}
                  placeholder="#ffd200"
                  style={{
                    ...styles.input,
                    ...(readOnly ? styles.inputReadOnly : {}),
                    ...(String(p.color ?? "") && !isValidHexColor(String(p.color ?? ""))
                      ? { borderColor: "#dc2626" }
                      : {}),
                  }}
                />
                {String(p.color ?? "") !== "" && !isValidHexColor(String(p.color ?? "")) && (
                  <span style={{ ...styles.formHint, color: "#f87171" }}>
                    유효한 hex 색상이 아닙니다 (예: #ffd200). 기본색이 사용됩니다.
                  </span>
                )}
              </FormRow>
              <label style={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={Boolean(p.bounce ?? false)}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ bounce: e.target.checked })}
                />
                bounce (바운스 애니메이션)
              </label>
              {Boolean(p.bounce ?? false) && (
                <>
                  <RangeRow
                    label="bounce_freq"
                    min={0.1}
                    max={10}
                    step={0.1}
                    value={Number(p.bounce_freq ?? 2.0)}
                    onChange={(v) => onPatchParams({ bounce_freq: v })}
                    format={(v) => `${v.toFixed(1)} Hz`}
                    disabled={readOnly}
                  />
                  <RangeRow
                    label="bounce_amplitude_ratio"
                    min={0}
                    max={1}
                    step={0.05}
                    value={Number(p.bounce_amplitude_ratio ?? 0.15)}
                    onChange={(v) => onPatchParams({ bounce_amplitude_ratio: v })}
                    format={(v) => `${Math.round(v * 100)}%`}
                    disabled={readOnly}
                  />
                  <FormRow label="motion">
                    <select
                      value={String(p.motion ?? "sine")}
                      disabled={readOnly}
                      onChange={(e) => onPatchParams({ motion: e.target.value })}
                      style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                    >
                      <option value="sine">sine (사인)</option>
                      <option value="perlin">perlin (펄린 노이즈)</option>
                    </select>
                  </FormRow>
                </>
              )}
            </>
          )}
          {t === "smell" && (
            <>
              <FormRow label="color">
                <select
                  value={String(p.color ?? "green")}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ color: e.target.value })}
                  style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                >
                  <option value="green">green (악취/독)</option>
                  <option value="brown">brown (음식)</option>
                  <option value="white">white (연기/김)</option>
                  <option value="yellow">yellow (자극)</option>
                  <option value="gray">gray (먼지)</option>
                </select>
              </FormRow>
              <RangeRow
                label="width"
                min={0.05}
                max={0.8}
                step={0.01}
                value={Number(p.width ?? 0.25)}
                onChange={(v) => onPatchParams({ width: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="height"
                min={0.05}
                max={0.9}
                step={0.01}
                value={Number(p.height ?? 0.35)}
                onChange={(v) => onPatchParams({ height: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="intensity"
                min={0}
                max={1}
                step={0.05}
                value={Number(p.intensity ?? 0.7)}
                onChange={(v) => onPatchParams({ intensity: v })}
                format={(v) => `${Math.round(v * 100)}%`}
                disabled={readOnly}
              />
              <label style={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={Boolean(p.loop ?? false)}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ loop: e.target.checked })}
                />
                loop (반복 모션)
              </label>
              {Boolean(p.loop ?? false) && (
                <>
                  <RangeRow
                    label="loop_cycle_sec"
                    min={0.2}
                    max={20}
                    step={0.1}
                    value={Number(p.loop_cycle_sec ?? 2.0)}
                    onChange={(v) => onPatchParams({ loop_cycle_sec: v })}
                    format={(v) => `${v.toFixed(1)} s`}
                    disabled={readOnly}
                  />
                  <RangeRow
                    label="loop_alpha_min"
                    min={0}
                    max={1}
                    step={0.05}
                    value={Number(p.loop_alpha_min ?? 0.5)}
                    onChange={(v) => onPatchParams({ loop_alpha_min: v })}
                    format={(v) => `${Math.round(v * 100)}%`}
                    disabled={readOnly}
                  />
                  <FormRow label="motion">
                    <select
                      value={String(p.motion ?? "linear")}
                      disabled={readOnly}
                      onChange={(e) => onPatchParams({ motion: e.target.value })}
                      style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                    >
                      <option value="linear">linear (수직 상승)</option>
                      <option value="perlin">perlin (펄린 좌우 흔들림)</option>
                    </select>
                  </FormRow>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

interface MotionFormProps {
  form: FormState;
  readOnly: boolean;
  onToggle: (on: boolean) => void;
  onChangeType: (t: MotionType) => void;
  onChangeStartSec: (v: number) => void;
  onChangeDurationSec: (v: number) => void;
  onPatchParams: (patch: Record<string, unknown>) => void;
}

const MOTION_TYPE_OPTIONS: { value: MotionType; label: string }[] = [
  { value: "shake", label: "shake (셰이크)" },
  { value: "clip_zoom", label: "clip_zoom (영역 줌)" },
  { value: "slowmo", label: "slowmo (슬로우 모션)" },
];

function MotionEffectForm({
  form,
  readOnly,
  onToggle,
  onChangeType,
  onChangeStartSec,
  onChangeDurationSec,
  onPatchParams,
}: MotionFormProps) {
  const p = form.motion_params;
  const t = form.motion_type;
  return (
    <div style={styles.effectGroup}>
      <label style={styles.effectGroupHeader}>
        <input
          type="checkbox"
          checked={form.motion_enabled}
          disabled={readOnly}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span style={styles.effectGroupTitle}>effects.motion (씬 모션)</span>
      </label>
      {form.motion_enabled && (
        <div style={styles.effectBody}>
          <FormRow label="motion_type">
            <select
              style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
              value={t}
              disabled={readOnly}
              onChange={(e) => onChangeType(e.target.value as MotionType)}
            >
              {MOTION_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </FormRow>
          <div style={{ display: "flex", gap: 8 }}>
            <FormRow label="start_sec" style={{ flex: 1 }}>
              <input
                type="number"
                step={0.1}
                value={form.motion_start_sec}
                disabled={readOnly}
                onChange={(e) => onChangeStartSec(parseFloat(e.target.value) || 0)}
                style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
              />
            </FormRow>
            <FormRow label="duration_sec" style={{ flex: 1 }}>
              <input
                type="number"
                step={0.1}
                min={0.1}
                value={form.motion_duration_sec}
                disabled={readOnly}
                onChange={(e) => onChangeDurationSec(parseFloat(e.target.value) || 0.1)}
                style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
              />
            </FormRow>
          </div>
          {t === "shake" && (
            <RangeRow
              label="intensity"
              min={0}
              max={1}
              step={0.05}
              value={Number(p.intensity ?? 0.6)}
              onChange={(v) => onPatchParams({ intensity: v })}
              format={(v) => v.toFixed(2)}
              disabled={readOnly}
            />
          )}
          {t === "clip_zoom" && (
            <>
              <RangeRow
                label="target_x"
                min={0}
                max={1}
                step={0.01}
                value={Number(p.target_x ?? 0.5)}
                onChange={(v) => onPatchParams({ target_x: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="target_y"
                min={0}
                max={1}
                step={0.01}
                value={Number(p.target_y ?? 0.5)}
                onChange={(v) => onPatchParams({ target_y: v })}
                format={(v) => v.toFixed(2)}
                disabled={readOnly}
              />
              <RangeRow
                label="start_scale"
                min={1.0}
                max={3.0}
                step={0.05}
                value={Number(p.start_scale ?? 1.0)}
                onChange={(v) => onPatchParams({ start_scale: v })}
                format={(v) => `${v.toFixed(2)}x`}
                disabled={readOnly}
              />
              <RangeRow
                label="end_scale"
                min={1.0}
                max={3.0}
                step={0.05}
                value={Number(p.end_scale ?? 1.3)}
                onChange={(v) => onPatchParams({ end_scale: v })}
                format={(v) => `${v.toFixed(2)}x`}
                disabled={readOnly}
              />
            </>
          )}
          {t === "slowmo" && (
            <>
              <FormRow label="speed">
                <select
                  value={String(p.speed ?? 0.5)}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ speed: parseFloat(e.target.value) })}
                  style={{ ...styles.input, ...(readOnly ? styles.inputReadOnly : {}) }}
                >
                  <option value="0.25">0.25x (4배 느림)</option>
                  <option value="0.5">0.5x (2배 느림)</option>
                  <option value="0.75">0.75x</option>
                  <option value="1">1.0x (원본 속도)</option>
                </select>
              </FormRow>
              <label style={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={Boolean(p.interpolate ?? false)}
                  disabled={readOnly}
                  onChange={(e) => onPatchParams({ interpolate: e.target.checked })}
                />
                interpolate (프레임 보간)
              </label>
              <RangeRow
                label="freeze_at_end_sec"
                min={0}
                max={2}
                step={0.1}
                value={Number(p.freeze_at_end_sec ?? 0.0)}
                onChange={(v) => onPatchParams({ freeze_at_end_sec: v })}
                format={(v) => `${v.toFixed(1)} s`}
                disabled={readOnly}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ─── 공용 폼 헬퍼 ─────────────────────────────────────────────────────────

function FormRow({
  label,
  children,
  style,
}: {
  label: string;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div style={{ ...styles.formField, ...(style ?? {}) }}>
      <label style={styles.formSubLabel}>{label}</label>
      {children}
    </div>
  );
}

function RangeRow({
  label,
  min,
  max,
  step,
  value,
  onChange,
  format,
  disabled,
}: {
  label: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (v: number) => void;
  format: (v: number) => string;
  disabled?: boolean;
}) {
  const clamped = clamp(value, min, max);
  return (
    <div style={styles.rangeRow}>
      <label style={styles.rangeLabel}>{label}</label>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={clamped}
        disabled={disabled}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        style={{ flex: 1, accentColor: "#a78bfa" }}
      />
      <span style={styles.rangeValue}>{format(clamped)}</span>
    </div>
  );
}

function HexColorRow({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  const valid = isValidHexColor(value);
  const safeValue = valid ? value : "#ff0000";
  return (
    <div style={styles.formField}>
      <label style={styles.formSubLabel}>{label}</label>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="color"
          value={safeValue}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: 40, height: 28, border: "none", background: "transparent" }}
        />
        <input
          type="text"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          style={{
            ...styles.input,
            flex: 1,
            ...(disabled ? styles.inputReadOnly : {}),
            ...(valid ? {} : { borderColor: "#dc2626" }),
          }}
        />
      </div>
      {!valid && (
        <span style={{ ...styles.formHint, color: "#f87171" }}>
          유효한 hex 색상이 아닙니다 (예: #ff3030). 빨강이 폴백으로 사용됩니다.
        </span>
      )}
    </div>
  );
}

// ─── 스타일 ───────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.7)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
  },
  modal: {
    background: "#1a1a1a",
    borderRadius: 12,
    width: 880,
    maxWidth: "92vw",
    maxHeight: "88vh",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    padding: "16px 22px",
    borderBottom: "1px solid #333",
  },
  closeBtn: {
    background: "none",
    border: "none",
    color: "#888",
    fontSize: 20,
    cursor: "pointer",
  },
  body: {
    display: "flex",
    flex: 1,
    minHeight: 0,
  },
  listPanel: {
    width: 280,
    borderRight: "1px solid #2a2a2a",
    display: "flex",
    flexDirection: "column",
    background: "#141420",
  },
  listHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    padding: "10px 12px",
    borderBottom: "1px solid #2a2a2a",
  },
  listTitle: {
    color: "#a78bfa",
    fontSize: 12,
    fontWeight: 700,
    letterSpacing: 0.4,
    textTransform: "uppercase",
  },
  newBtn: {
    padding: "4px 10px",
    background: "#4338ca",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 11,
    fontWeight: 600,
  },
  listScroll: {
    flex: 1,
    overflowY: "auto",
  },
  listItem: {
    padding: "8px 12px",
    cursor: "pointer",
    borderBottom: "1px solid #1f1f2a",
    display: "flex",
    flexDirection: "column",
    gap: 3,
  },
  listItemActive: {
    background: "#1e1b4b",
  },
  listItemLabel: {
    color: "#ddd",
    fontSize: 13,
    fontWeight: 600,
  },
  listItemId: {
    color: "#888",
    fontFamily: "monospace",
    fontSize: 11,
    fontWeight: 400,
  },
  listItemMeta: {
    display: "flex",
    gap: 6,
    alignItems: "center",
  },
  builtinBadge: {
    background: "#2a2a3a",
    color: "#888",
    fontSize: 10,
    padding: "1px 6px",
    borderRadius: 8,
  },
  userBadge: {
    background: "#1f3a4a",
    color: "#7dd3fc",
    fontSize: 10,
    padding: "1px 6px",
    borderRadius: 8,
  },
  formPanel: {
    flex: 1,
    padding: "16px 22px",
    overflowY: "auto",
    display: "flex",
    flexDirection: "column",
    gap: 12,
  },
  emptyHint: {
    color: "#666",
    fontSize: 13,
    textAlign: "center",
    margin: "auto",
    padding: 40,
  },
  formField: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  formLabel: {
    color: "#a78bfa",
    fontSize: 11,
    fontWeight: 600,
    letterSpacing: 0.3,
    textTransform: "uppercase",
  },
  formSubLabel: {
    color: "#888",
    fontSize: 11,
    fontWeight: 500,
  },
  formHint: {
    color: "#666",
    fontSize: 11,
    fontStyle: "italic",
  },
  input: {
    padding: "8px 12px",
    background: "#111",
    border: "1px solid #333",
    borderRadius: 6,
    color: "#fff",
    fontSize: 13,
    width: "100%",
    boxSizing: "border-box",
  },
  textarea: {
    padding: "8px 12px",
    background: "#111",
    border: "1px solid #333",
    borderRadius: 6,
    color: "#fff",
    fontSize: 13,
    width: "100%",
    boxSizing: "border-box",
    resize: "vertical",
    fontFamily: "inherit",
  },
  textareaMono: {
    padding: "8px 12px",
    background: "#0a0a14",
    border: "1px solid #333",
    borderRadius: 6,
    color: "#c4b5fd",
    fontSize: 12,
    width: "100%",
    boxSizing: "border-box",
    resize: "vertical",
    fontFamily: "Menlo, Consolas, monospace",
  },
  inputReadOnly: {
    background: "#0a0a0a",
    color: "#666",
    cursor: "not-allowed",
  },
  effectGroup: {
    border: "1px solid #2a2a2a",
    borderRadius: 8,
    padding: 10,
    background: "#0f0f18",
    display: "flex",
    flexDirection: "column",
    gap: 8,
  },
  effectGroupHeader: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    cursor: "pointer",
    userSelect: "none",
  },
  effectGroupTitle: {
    color: "#a78bfa",
    fontSize: 12,
    fontWeight: 600,
    letterSpacing: 0.3,
    textTransform: "uppercase",
  },
  effectBody: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
    paddingLeft: 4,
    paddingTop: 4,
    borderTop: "1px dashed #2a2a2a",
  },
  rangeRow: {
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  rangeLabel: {
    color: "#888",
    fontSize: 11,
    fontWeight: 500,
    minWidth: 130,
  },
  rangeValue: {
    color: "#ccc",
    fontSize: 11,
    minWidth: 60,
    textAlign: "right",
    fontFamily: "monospace",
  },
  checkRow: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    color: "#aaa",
    fontSize: 12,
    cursor: "pointer",
  },
  errorBox: {
    background: "#3a1818",
    border: "1px solid #92400e",
    color: "#fbbf24",
    padding: "8px 12px",
    borderRadius: 6,
    fontSize: 12,
  },
  formActions: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    paddingTop: 6,
    borderTop: "1px solid #2a2a2a",
    marginTop: 4,
  },
  builtinNotice: {
    color: "#888",
    fontSize: 11,
    fontStyle: "italic",
    flex: 1,
  },
  duplicateBtn: {
    padding: "8px 16px",
    background: "#7c3aed",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
  },
  deleteBtn: {
    padding: "8px 16px",
    background: "#7f1d1d",
    color: "#fecaca",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
  },
  saveBtn: {
    padding: "8px 20px",
    background: "#4338ca",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
  },
};
