import { useMemo, useState } from "react";
import type {
  SceneCard,
  ShapeType,
  GraphicOverlayType,
  SceneFilterType,
  MotionType,
  MosaicShape,
} from "../types";
import {
  CATEGORY_ORDER,
  CATEGORY_STYLE,
  useEffectCatalog,
  type EffectCategory,
} from "../lib/effectCatalog";
import type { EffectDefinition } from "../types/generated";

// #257 — 씬 카드에 흩어져 있던 15개 효과 추가 버튼을 사이드 패널로 일원화한다.
// #306 — 이펙트 카탈로그 매니페스트화. 패널이 더 이상 효과 목록을 하드코딩하지 않고,
//        백엔드 `GET /api/effect_catalog` (= `src/render_spec/effects/` 매니페스트)를
//        읽어 동적으로 렌더링한다.
// #295 — 정보 위계 개선:
//        - 상단 검색 입력(효과 이름/라벨/카테고리/툴팁/한글 카테고리명 필터)
//        - 카테고리 헤더 접기/펴기 (localStorage 저장)
//        - 즐겨찾기(별) localStorage 저장 + 상단 고정 섹션
//        - 최근 사용한 효과 상단 표시 (localStorage)
//
// 단일 씬 선택 정책(selectedSceneIds.size > 1 시 비활성)은 그대로 유지한다 (이슈 본문 합의).
//
// 핸들러 시그니처는 기존과 동일. Editor.tsx의 상위 함수를 그대로 전달받아 호출한다.

interface Props {
  selectedScene: SceneCard | null;
  selectedSceneIds: Set<number>;
  onAddShape: (shape: ShapeType) => void;
  onAddGraphic: (type: GraphicOverlayType) => void;
  onAddFilter: (filterType: SceneFilterType) => void;
  onAddMotion: (motionType: MotionType) => void;
  onAddTextOverlay: () => void;
  onAddMosaic: (shape: MosaicShape) => void;
  // 이미지 오버레이 — footage 스튜디오의 이미지 자산 선택 모달을 띄우고 추가.
  onAddImage: () => void;
  // 집중선(속도선) 오버레이 추가 — EFFECT_REGISTRY 에 없는 별도 오버레이 타입이라
  // 카탈로그 버튼이 아닌 전용 버튼으로 노출한다.
  onAddSpeedLines: () => void;
}

// ── localStorage 헬퍼 ──────────────────────────────────────────────────────
const FAV_KEY = "effectPanel.favorites";       // string[]  "category:name"
const RECENT_KEY = "effectPanel.recent";       // string[]  "category:name" (최신 우선)
const COLLAPSE_KEY = "effectPanel.collapsed";  // string[]  category
const RECENT_MAX = 5;

function effKey(eff: EffectDefinition): string {
  return `${eff.category}:${eff.name}`;
}
function loadList(key: string): string[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : [];
  } catch { return []; }
}
function saveList(key: string, list: string[]): void {
  try { localStorage.setItem(key, JSON.stringify(list)); } catch { /* ignore */ }
}

export default function EffectAddPanel({
  selectedScene,
  selectedSceneIds,
  onAddShape,
  onAddGraphic,
  onAddFilter,
  onAddMotion,
  onAddTextOverlay,
  onAddMosaic,
  onAddImage,
  onAddSpeedLines,
}: Props) {
  const multiSelected = selectedSceneIds.size > 1;
  const sceneAvailable = !!selectedScene && !multiSelected;
  const { effects, byCategory } = useEffectCatalog();

  const [query, setQuery] = useState("");
  const [favorites, setFavorites] = useState<string[]>(() => loadList(FAV_KEY));
  const [recent, setRecent] = useState<string[]>(() => loadList(RECENT_KEY));
  const [collapsed, setCollapsed] = useState<string[]>(() => loadList(COLLAPSE_KEY));

  function toggleFavorite(eff: EffectDefinition) {
    const k = effKey(eff);
    setFavorites((prev) => {
      const next = prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k];
      saveList(FAV_KEY, next);
      return next;
    });
  }
  function pushRecent(eff: EffectDefinition) {
    const k = effKey(eff);
    setRecent((prev) => {
      const next = [k, ...prev.filter((x) => x !== k)].slice(0, RECENT_MAX);
      saveList(RECENT_KEY, next);
      return next;
    });
  }
  function toggleCollapse(cat: EffectCategory) {
    setCollapsed((prev) => {
      const next = prev.includes(cat) ? prev.filter((x) => x !== cat) : [...prev, cat];
      saveList(COLLAPSE_KEY, next);
      return next;
    });
  }

  // 효과 호출 분기 — 매니페스트의 (category, name)을 기존 핸들러 시그니처로 라우팅.
  function dispatchEffect(eff: EffectDefinition): void {
    pushRecent(eff);
    switch (eff.category) {
      case "text": onAddTextOverlay(); return;
      case "shape": onAddShape(eff.name as ShapeType); return;
      case "mosaic": onAddMosaic(eff.name as MosaicShape); return;
      case "image": onAddImage(); return;
      case "graphic": onAddGraphic(eff.name as GraphicOverlayType); return;
      case "filter": onAddFilter(eff.name as SceneFilterType); return;
      case "motion": onAddMotion(eff.name as MotionType); return;
    }
  }

  // 검색 매칭 — 이름/라벨/툴팁/카테고리명(영문+한글) 모두 대상.
  const q = query.trim().toLowerCase();
  function matchesQuery(eff: EffectDefinition): boolean {
    if (!q) return true;
    const catTitle = CATEGORY_STYLE[eff.category as EffectCategory]?.title ?? "";
    const hay = [
      eff.name,
      eff.display_label,
      eff.tooltip ?? "",
      eff.category,
      catTitle,
    ].join(" ").toLowerCase();
    return hay.includes(q);
  }

  const filteredByCategory = useMemo(() => {
    const out = {} as Record<EffectCategory, EffectDefinition[]>;
    for (const cat of CATEGORY_ORDER) {
      out[cat] = (byCategory[cat] ?? []).filter(matchesQuery);
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [byCategory, q]);

  // 즐겨찾기/최근은 검색어가 없을 때만 상단에 노출 (검색 중엔 결과만 보여 혼선 방지).
  const favEffects = !q ? favorites.map((k) => effects.find((e) => effKey(e) === k)).filter(Boolean) as EffectDefinition[] : [];
  const recentEffects = !q ? recent.map((k) => effects.find((e) => effKey(e) === k)).filter(Boolean) as EffectDefinition[] : [];

  const totalFiltered = CATEGORY_ORDER.reduce((n, c) => n + filteredByCategory[c].length, 0);

  return (
    <div style={styles.panel}>
      <div style={styles.header}>
        <span style={styles.headerTitle}>효과 추가</span>
        {selectedScene && !multiSelected && (
          <span style={styles.sceneBadge}>씬 {selectedScene.scene_id}</span>
        )}
      </div>

      {/* #295 — 검색 입력 */}
      <div style={styles.searchWrap}>
        <input
          style={styles.searchInput}
          placeholder="효과 검색 (예: 전환, Vivid, 모션)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query && (
          <button style={styles.searchClear} onClick={() => setQuery("")} title="검색 지우기">×</button>
        )}
      </div>

      {!selectedScene && !multiSelected && (
        <div style={styles.hint}>씬을 선택하세요</div>
      )}
      {multiSelected && (
        <div style={styles.hint}>
          {selectedSceneIds.size}개 다중 선택 중
          <br />
          단일 씬을 선택해주세요
        </div>
      )}

      <div style={styles.body}>
        {/* 즐겨찾기 */}
        {favEffects.length > 0 && (
          <FlatSection
            title="★ 즐겨찾기"
            titleColor="#fbbf24"
            borderColor="#a16207"
            effects={favEffects}
            disabled={!sceneAvailable}
            favorites={favorites}
            selectedScene={selectedScene}
            onClickEffect={dispatchEffect}
            onToggleFavorite={toggleFavorite}
          />
        )}

        {/* 최근 사용 */}
        {recentEffects.length > 0 && (
          <FlatSection
            title="최근 사용"
            titleColor="#9ca3af"
            borderColor="#3f3f46"
            effects={recentEffects}
            disabled={!sceneAvailable}
            favorites={favorites}
            selectedScene={selectedScene}
            onClickEffect={dispatchEffect}
            onToggleFavorite={toggleFavorite}
          />
        )}

        {q && totalFiltered === 0 && (
          <div style={styles.noResult}>"{query}" 와 일치하는 효과가 없습니다.</div>
        )}

        {/* 카테고리별 (접기/펴기 가능) */}
        {CATEGORY_ORDER.map((cat) => {
          const items = filteredByCategory[cat];
          if (items.length === 0) return null;
          return (
            <CategorySection
              key={cat}
              category={cat}
              effects={items}
              selectedScene={selectedScene}
              disabled={!sceneAvailable}
              // 검색 중에는 접힘 상태 무시하고 항상 펼쳐서 결과를 보여준다.
              collapsed={!q && collapsed.includes(cat)}
              favorites={favorites}
              onToggleCollapse={() => toggleCollapse(cat)}
              onClickEffect={dispatchEffect}
              onToggleFavorite={toggleFavorite}
            />
          );
        })}

        {/* 집중선(속도선) — 별도 오버레이 타입 전용 버튼. 검색어가 있으면 매칭 시에만 노출. */}
        {(!q || "집중선 속도선 speed lines 만화 cartoon".includes(q)) && (
          <div style={{ ...styles.section, borderColor: "#b45309" }}>
            <div style={styles.sectionHeader}>
              <span style={styles.sectionCaret}> </span>
              <span style={{ ...styles.sectionTitle, color: "#fbbf24" }}>집중선</span>
            </div>
            <div style={styles.sectionBody}>
              <button
                type="button"
                disabled={!sceneAvailable}
                style={{
                  ...styles.addBtn,
                  color: sceneAvailable ? "#fbbf24" : "#555",
                  borderColor: sceneAvailable ? "#b45309" : "#333",
                  cursor: sceneAvailable ? "pointer" : "not-allowed",
                }}
                title="만화 집중선(속도선) — 중심점에서 사방으로 뻗는 날카로운 선으로 박진감"
                onClick={() => sceneAvailable && onAddSpeedLines()}
              >
                + 집중선
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── 평면 섹션 (즐겨찾기/최근) ───────────────────────────────────────────────

function FlatSection({
  title, titleColor, borderColor, effects, disabled, favorites, selectedScene, onClickEffect, onToggleFavorite,
}: {
  title: string;
  titleColor: string;
  borderColor: string;
  effects: EffectDefinition[];
  disabled: boolean;
  favorites: string[];
  selectedScene: SceneCard | null;
  onClickEffect: (eff: EffectDefinition) => void;
  onToggleFavorite: (eff: EffectDefinition) => void;
}) {
  return (
    <div style={{ ...styles.section, borderColor: disabled ? "#222" : borderColor, opacity: disabled ? 0.45 : 1 }}>
      <div style={{ ...styles.sectionTitle, color: disabled ? "#555" : titleColor }}>{title}</div>
      <div style={styles.sectionBody}>
        {effects.map((eff) => (
          <EffectButton
            key={effKey(eff)}
            eff={eff}
            disabled={disabled}
            isFavorite={favorites.includes(effKey(eff))}
            forceDisabledReason={filterAlreadyApplied(eff, selectedScene)}
            onClick={() => onClickEffect(eff)}
            onToggleFavorite={() => onToggleFavorite(eff)}
          />
        ))}
      </div>
    </div>
  );
}

// ── 카테고리 섹션 ───────────────────────────────────────────────────────────

function CategorySection({
  category, effects, selectedScene, disabled, collapsed, favorites, onToggleCollapse, onClickEffect, onToggleFavorite,
}: {
  category: EffectCategory;
  effects: EffectDefinition[];
  selectedScene: SceneCard | null;
  disabled: boolean;
  collapsed: boolean;
  favorites: string[];
  onToggleCollapse: () => void;
  onClickEffect: (eff: EffectDefinition) => void;
  onToggleFavorite: (eff: EffectDefinition) => void;
}) {
  const style = CATEGORY_STYLE[category];
  return (
    <div
      style={{
        ...styles.section,
        borderColor: disabled ? "#222" : style.border,
        opacity: disabled ? 0.45 : 1,
      }}
    >
      {/* #295 — 카테고리 헤더 클릭으로 접기/펴기 */}
      <div
        style={{ ...styles.sectionHeader, color: disabled ? "#555" : style.text }}
        onClick={onToggleCollapse}
        title={collapsed ? "펼치기" : "접기"}
      >
        <span style={styles.sectionCaret}>{collapsed ? "▸" : "▾"}</span>
        <span style={styles.sectionTitle}>{style.title}</span>
        <span style={styles.sectionCount}>{effects.length}</span>
      </div>
      {!collapsed && (
        <div style={styles.sectionBody}>
          {effects.map((eff) => (
            <EffectButton
              key={effKey(eff)}
              eff={eff}
              disabled={disabled}
              isFavorite={favorites.includes(effKey(eff))}
              forceDisabledReason={
                category === "filter" ? filterAlreadyApplied(eff, selectedScene) : null
              }
              onClick={() => onClickEffect(eff)}
              onToggleFavorite={() => onToggleFavorite(eff)}
            />
          ))}
          {category === "text" && !disabled && (
            <div style={styles.hintSmall}>
              추가 후 씬 카드의 칩을 클릭하면 텍스트·스타일·프리셋 편집기가 열립니다.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// 필터의 경우 이미 적용된 동일 타입은 비활성. (기존 정책 보존)
function filterAlreadyApplied(eff: EffectDefinition, selectedScene: SceneCard | null): string | null {
  if (eff.category !== "filter") return null;
  if (!selectedScene) return null;
  return (selectedScene.filters ?? []).some((f) => f.filter_type === eff.name)
    ? `${eff.name} 이미 적용됨`
    : null;
}

// ── 버튼 ──────────────────────────────────────────────────────────────────

function EffectButton({
  eff,
  disabled,
  isFavorite,
  forceDisabledReason,
  onClick,
  onToggleFavorite,
}: {
  eff: EffectDefinition;
  disabled: boolean;
  isFavorite: boolean;
  forceDisabledReason: string | null;
  onClick: () => void;
  onToggleFavorite: () => void;
}) {
  const [hover, setHover] = useState(false);
  const style = CATEGORY_STYLE[eff.category as EffectCategory];
  const isFilter = eff.category === "filter";
  const blocked = !!forceDisabledReason;
  const effectivelyDisabled = disabled || blocked;

  const label = `+ ${eff.display_label}`;

  // #295 — hover 미리보기. 필터는 전용 PNG, 그 외는 카테고리별 정적 프리뷰(있으면) 노출.
  const showPreview = hover && !effectivelyDisabled;
  const previewSrc = isFilter
    ? `/filter_previews/${eff.name}.png`
    : `/effect_previews/${eff.category}_${eff.name}.png`;

  return (
    <span
      style={{ position: "relative", display: "inline-flex", alignItems: "center" }}
      onMouseEnter={() => !effectivelyDisabled && setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        style={{
          ...styles.addBtn,
          color: blocked ? "#555" : style.text,
          borderColor: blocked ? "#333" : style.border,
          opacity: effectivelyDisabled ? 0.5 : 1,
          cursor: effectivelyDisabled ? "not-allowed" : "pointer",
        }}
        onClick={onClick}
        disabled={effectivelyDisabled}
        title={forceDisabledReason ?? eff.tooltip ?? eff.display_label}
      >
        {label}
      </button>
      {/* #295 — 즐겨찾기 별 토글 (비활성 상태에서도 즐겨찾기는 가능) */}
      <button
        style={{ ...styles.favStar, color: isFavorite ? "#fbbf24" : "#555" }}
        onClick={(e) => { e.stopPropagation(); onToggleFavorite(); }}
        title={isFavorite ? "즐겨찾기 해제" : "즐겨찾기 추가"}
      >
        {isFavorite ? "★" : "☆"}
      </button>
      {showPreview && (
        <span
          style={{
            position: "absolute",
            bottom: "100%",
            left: 0,
            marginBottom: 4,
            background: "#000",
            border: `1px solid ${style.border}`,
            borderRadius: 4,
            padding: 2,
            zIndex: 1000,
            pointerEvents: "none",
            boxShadow: "0 4px 12px rgba(0,0,0,0.5)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
          }}
        >
          <img
            src={previewSrc}
            alt={`${eff.name} 미리보기`}
            width={160}
            height={90}
            style={{ display: "block", borderRadius: 2, objectFit: "cover" }}
            onError={(e) => {
              // 프리뷰 이미지가 없으면 이미지를 숨기고 라벨/툴팁만 보여준다.
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
          <span style={{ display: "block", fontSize: 10, color: style.text, textAlign: "center", marginTop: 2, maxWidth: 160 }}>
            {eff.display_label}
          </span>
          {eff.tooltip && (
            <span style={{ display: "block", fontSize: 9, color: "#999", textAlign: "center", marginTop: 1, maxWidth: 160, lineHeight: 1.3 }}>
              {eff.tooltip}
            </span>
          )}
        </span>
      )}
    </span>
  );
}

// ── 스타일 ────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  panel: {
    width: 260,
    borderLeft: "1px solid #222",
    background: "#0f0f0f",
    display: "flex",
    flexDirection: "column",
    flexShrink: 0,
    overflowY: "auto",
  },
  header: {
    padding: "12px 12px 8px",
    borderBottom: "1px solid #222",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
    flexShrink: 0,
  },
  headerTitle: { color: "#aaa", fontSize: 11, fontWeight: 700, textTransform: "uppercase" },
  sceneBadge: {
    background: "#1e1b4b",
    color: "#818cf8",
    borderRadius: 4,
    padding: "2px 8px",
    fontSize: 11,
    fontWeight: 700,
  },
  searchWrap: { position: "relative", padding: "10px 12px 6px", flexShrink: 0 },
  searchInput: {
    width: "100%",
    boxSizing: "border-box",
    padding: "7px 28px 7px 10px",
    background: "#1a1a1a",
    border: "1px solid #333",
    borderRadius: 6,
    color: "#ddd",
    fontSize: 12,
    outline: "none",
  },
  searchClear: {
    position: "absolute",
    right: 18,
    top: 16,
    width: 20,
    height: 20,
    background: "transparent",
    color: "#888",
    border: "none",
    cursor: "pointer",
    fontSize: 16,
    lineHeight: 1,
  },
  noResult: { color: "#666", fontSize: 12, textAlign: "center", padding: "12px 0" },
  hint: {
    padding: "16px 12px",
    color: "#666",
    fontSize: 12,
    textAlign: "center",
    background: "#111",
    borderBottom: "1px solid #222",
  },
  hintSmall: {
    padding: "4px 0 0 0",
    color: "#555",
    fontSize: 10,
    lineHeight: 1.4,
    width: "100%",
  },
  body: {
    display: "flex",
    flexDirection: "column",
    gap: 10,
    padding: 12,
  },
  section: {
    border: "1px solid",
    borderRadius: 8,
    padding: "8px 10px",
    display: "flex",
    flexDirection: "column",
    gap: 6,
    background: "#0a0a0a",
    transition: "opacity 0.15s, border-color 0.15s",
  },
  sectionHeader: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    cursor: "pointer",
    userSelect: "none",
  },
  sectionCaret: { fontSize: 10, width: 10 },
  sectionTitle: {
    fontSize: 11,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: 0.4,
    flex: 1,
  },
  sectionCount: {
    fontSize: 10,
    color: "#666",
    background: "#161616",
    borderRadius: 8,
    padding: "1px 7px",
  },
  sectionBody: {
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
    alignItems: "center",
  },
  addBtn: {
    padding: "3px 9px",
    background: "#111",
    color: "#6366f1",
    border: "1px solid #6366f1",
    borderRadius: 5,
    fontSize: 11,
    cursor: "pointer",
  },
  favStar: {
    background: "transparent",
    border: "none",
    cursor: "pointer",
    fontSize: 12,
    padding: "0 2px 0 1px",
    lineHeight: 1,
  },
};
