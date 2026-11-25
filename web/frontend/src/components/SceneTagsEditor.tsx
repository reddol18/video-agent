/**
 * SceneTagsEditor — 씬 카드 태그 입력 UI (이슈 #211)
 *
 * 구성:
 *  - 태그 입력 input (콤마/스페이스/엔터로 토큰화)
 *  - 추가된 태그 칩 (✕ 삭제, 클릭 시 출처 펼침)
 *  - 자동완성 드롭다운 (label + tag id, 화살표/엔터 선택)
 *  - 펼침 패널: 선택된 칩이 적용한 효과(text_overlay 미리보기 + _pending 안내)
 *
 * 호출 측은 onAddTag/onRemoveTag만 처리하면 되며, 효과 머지·preview invalidate는
 * 호출 측 책임 (Editor의 onUpdateTags 핸들러).
 *
 * 의존: web/frontend/src/lib/sceneTags.ts (1차 구현, 변경 없음)
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { SceneCard, TextOverlayPreset } from "../types";
import {
  type SceneTagDictionary,
  type SceneTagDef,
  getBuiltinTagDictionary,
  loadTagDictionary,
  getTagSources,
} from "../lib/sceneTags";
import { BUILTIN_TEXT_PRESETS } from "../lib/textPresetSeeds";

interface Props {
  scene: SceneCard;
  onAddTag: (tagName: string) => void;
  onRemoveTag: (tagName: string) => void;
  /**
   * 사전 재로드 트리거 (#214). 호출 측이 태그 사전 편집 모달 저장/삭제 시 값을 증가시키면
   * useEffect가 재실행되어 IndexedDB 머지 결과를 반영한다.
   */
  dictVersion?: number;
}

// 토큰 분리자: 콤마/공백/엔터
const TOKEN_SPLIT_RE = /[\s,]+/;

const PENDING_KIND_LABEL: Record<string, string> = {
  filter: "필터",
  graphic_overlay: "그래픽 오버레이",
  motion: "모션",
};

export default function SceneTagsEditor({ scene, onAddTag, onRemoveTag, dictVersion = 0 }: Props) {
  const [dict, setDict] = useState<SceneTagDictionary>(() => getBuiltinTagDictionary());
  const [inputValue, setInputValue] = useState("");
  const [openTag, setOpenTag] = useState<string | null>(null);
  const [autocompleteIdx, setAutocompleteIdx] = useState(0);
  const [autocompleteOpen, setAutocompleteOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // 마운트 시 + dictVersion 변경 시 사전 재로드 (#214 — 사전 편집 모달 저장/삭제 후 갱신)
  useEffect(() => {
    let cancelled = false;
    loadTagDictionary()
      .then((d) => {
        if (!cancelled) setDict(d);
      })
      .catch(() => {
        // 빌트인 fallback 유지
      });
    return () => {
      cancelled = true;
    };
  }, [dictVersion]);

  const currentTags = scene.tags ?? [];
  const tagSources = useMemo(() => getTagSources(scene, dict), [scene, dict]);

  // 자동완성 후보: 사전에 있고 + 이미 추가되지 않은 + (인풋이 비면 전체, 있으면 fuzzy)
  const candidates = useMemo(() => {
    const all = Object.entries(dict.tags);
    const q = inputValue.trim().toLowerCase();
    const filtered = all.filter(([id]) => !currentTags.includes(id));
    if (!q) return filtered;
    return filtered.filter(([id, def]) => {
      const lid = id.toLowerCase();
      const label = def.label.toLowerCase();
      // 한글 라벨 포함 또는 영문 id 포함 (대소문자 무시)
      return lid.includes(q) || label.includes(q);
    });
  }, [dict, inputValue, currentTags]);

  // 인풋 변경 시 자동완성 인덱스 리셋
  useEffect(() => {
    setAutocompleteIdx(0);
  }, [inputValue]);

  function commitToken(rawToken: string) {
    const token = rawToken.trim().toLowerCase();
    if (!token) return;
    if (currentTags.includes(token)) return;
    onAddTag(token);
  }

  function handleInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    // 콤마/공백/엔터 → 토큰화
    if (e.key === "Enter") {
      e.preventDefault();
      // 자동완성이 열려있고 후보가 있으면 자동완성 선택
      if (autocompleteOpen && candidates.length > 0) {
        const [id] = candidates[autocompleteIdx] ?? candidates[0];
        commitToken(id);
        setInputValue("");
        return;
      }
      // 자동완성 없으면 인풋 값 그대로 토큰화
      commitToken(inputValue);
      setInputValue("");
      return;
    }

    if (e.key === "," || e.key === " ") {
      // 콤마/스페이스: 인풋이 비어있지 않으면 토큰 커밋
      if (inputValue.trim()) {
        e.preventDefault();
        commitToken(inputValue);
        setInputValue("");
      }
      return;
    }

    if (e.key === "Backspace" && inputValue === "" && currentTags.length > 0) {
      // Backspace + 인풋 비어있음 → 마지막 칩 제거
      e.preventDefault();
      onRemoveTag(currentTags[currentTags.length - 1]);
      return;
    }

    // 자동완성 네비게이션
    if (autocompleteOpen && candidates.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setAutocompleteIdx((i) => (i + 1) % candidates.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setAutocompleteIdx((i) => (i - 1 + candidates.length) % candidates.length);
      } else if (e.key === "Escape") {
        e.preventDefault();
        setAutocompleteOpen(false);
      }
    }
  }

  function handleInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const value = e.target.value;
    // 입력 중 콤마/공백이 포함되면 분리하여 토큰화
    if (TOKEN_SPLIT_RE.test(value)) {
      const parts = value.split(TOKEN_SPLIT_RE);
      // 마지막 토큰은 인풋에 남기고, 나머지는 커밋
      const last = parts.pop() ?? "";
      for (const p of parts) {
        if (p.trim()) commitToken(p);
      }
      setInputValue(last);
    } else {
      setInputValue(value);
    }
    setAutocompleteOpen(true);
  }

  function handleChipClick(tag: string) {
    setOpenTag((cur) => (cur === tag ? null : tag));
  }

  function handleChipRemove(tag: string) {
    onRemoveTag(tag);
    if (openTag === tag) setOpenTag(null);
  }

  return (
    <div
      style={styles.container}
      // dnd-kit이 카드 전체 클릭을 가로채지 않도록 자체 영역에서 stopPropagation
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div style={styles.header}>
        <span style={styles.headerLabel}>씬 태그</span>
        <span style={styles.headerHint}>콤마·공백·엔터로 추가 / Backspace로 마지막 제거</span>
      </div>

      <div style={styles.chipRow}>
        {currentTags.map((tag) => {
          const def = tagSources[tag];
          const isOpen = openTag === tag;
          const isUnknown = !def;
          return (
            <span
              key={tag}
              style={{
                ...styles.chip,
                ...(isOpen ? styles.chipOpen : {}),
                ...(isUnknown ? styles.chipUnknown : {}),
              }}
              onClick={() => handleChipClick(tag)}
              title={def ? def.description : "사전에 없는 태그 (효과 미적용)"}
            >
              <span style={styles.chipLabel}>
                {def ? `${def.label} ` : ""}
                <span style={styles.chipId}>{tag}</span>
              </span>
              <span
                style={styles.chipDel}
                onClick={(e) => {
                  e.stopPropagation();
                  handleChipRemove(tag);
                }}
                title="태그 제거"
              >
                ✕
              </span>
            </span>
          );
        })}

        <div style={styles.inputWrap}>
          <input
            ref={inputRef}
            style={styles.input}
            value={inputValue}
            onChange={handleInputChange}
            onKeyDown={handleInputKeyDown}
            onFocus={() => setAutocompleteOpen(true)}
            onBlur={() => {
              // 드롭다운 클릭이 처리될 시간 확보
              setTimeout(() => setAutocompleteOpen(false), 150);
            }}
            placeholder={currentTags.length === 0 ? "태그 추가 (예: horror, witty)" : "+ 태그"}
          />
          {autocompleteOpen && candidates.length > 0 && (
            <div style={styles.dropdown}>
              {candidates.map(([id, def], idx) => (
                <div
                  key={id}
                  style={{
                    ...styles.dropdownItem,
                    ...(idx === autocompleteIdx ? styles.dropdownItemActive : {}),
                  }}
                  onMouseDown={(e) => {
                    // onBlur보다 먼저 실행되도록 onMouseDown 사용
                    e.preventDefault();
                    commitToken(id);
                    setInputValue("");
                    setAutocompleteOpen(false);
                    inputRef.current?.focus();
                  }}
                  onMouseEnter={() => setAutocompleteIdx(idx)}
                >
                  <span style={styles.dropdownLabel}>{def.label}</span>
                  <span style={styles.dropdownId}>{id}</span>
                  <span style={styles.dropdownDesc}>{def.description}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {openTag && tagSources[openTag] && (
        <TagSourcePanel tag={openTag} def={tagSources[openTag]} />
      )}
    </div>
  );
}

function TagSourcePanel({ tag, def }: { tag: string; def: SceneTagDef }) {
  const textPreset: TextOverlayPreset | null = def.effects.text_preset
    ? BUILTIN_TEXT_PRESETS.find((p) => p.id === def.effects.text_preset) ?? null
    : null;

  const pending = def.effects._pending ?? [];

  return (
    <div style={styles.sourcePanel}>
      <div style={styles.sourceHeader}>
        <span style={styles.sourceTitle}>
          {def.label} <span style={styles.sourceTag}>({tag})</span>
        </span>
        <span style={styles.sourceDesc}>{def.description}</span>
      </div>

      {textPreset ? (
        <div style={styles.effectRow}>
          <span style={styles.effectLabel}>텍스트 오버레이</span>
          <span style={styles.effectValue}>
            <span style={styles.previewSwatch} aria-hidden>
              <span
                style={{
                  color: textPreset.style.color,
                  fontWeight: textPreset.style.bold ? 700 : 400,
                  fontStyle: textPreset.style.italic ? "italic" : "normal",
                  textShadow: textPreset.style.outline_width > 0
                    ? `0 0 ${textPreset.style.outline_width}px ${textPreset.style.outline_color}`
                    : "none",
                }}
              >
                {textPreset.name}
              </span>
            </span>
            <span style={styles.effectDetail}>
              {textPreset.style.font_family} · {textPreset.style.font_size}px
            </span>
          </span>
        </div>
      ) : (
        <div style={styles.effectRow}>
          <span style={styles.effectLabel}>텍스트 오버레이</span>
          <span style={styles.effectValueMuted}>없음</span>
        </div>
      )}

      {pending.length > 0 && (
        <div style={styles.effectRow}>
          <span style={styles.effectLabel}>구현 예정</span>
          <span style={styles.effectValueMuted}>
            {pending.map((p) => PENDING_KIND_LABEL[p] ?? p).join(" / ")}
          </span>
        </div>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    background: "#0f0f14",
    border: "1px solid #2a2a3a",
    borderRadius: 8,
    padding: "8px 10px",
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  header: {
    display: "flex",
    alignItems: "baseline",
    gap: 8,
    flexWrap: "wrap",
  },
  headerLabel: {
    color: "#a78bfa",
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: 0.4,
    textTransform: "uppercase",
  },
  headerHint: {
    color: "#555",
    fontSize: 10,
  },
  chipRow: {
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
    alignItems: "center",
  },
  chip: {
    background: "#2a1f4a",
    color: "#c4b5fd",
    border: "1px solid #5b21b6",
    borderRadius: 12,
    padding: "2px 8px",
    fontSize: 11,
    cursor: "pointer",
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    transition: "background 0.12s",
  },
  chipOpen: {
    background: "#3b1f7a",
    borderColor: "#a78bfa",
  },
  chipUnknown: {
    background: "#3a2418",
    color: "#fbbf24",
    borderColor: "#92400e",
  },
  chipLabel: {
    display: "inline-flex",
    alignItems: "baseline",
    gap: 4,
  },
  chipId: {
    opacity: 0.6,
    fontSize: 10,
  },
  chipDel: {
    color: "#f87171",
    cursor: "pointer",
    fontSize: 10,
    fontWeight: 700,
  },
  inputWrap: {
    position: "relative",
    flex: "1 1 140px",
    minWidth: 120,
  },
  input: {
    width: "100%",
    boxSizing: "border-box",
    background: "transparent",
    border: "1px dashed #3a3a4a",
    borderRadius: 6,
    color: "#ddd",
    fontSize: 12,
    padding: "3px 8px",
    outline: "none",
  },
  dropdown: {
    position: "absolute",
    top: "calc(100% + 4px)",
    left: 0,
    right: 0,
    minWidth: 280,
    background: "#15151f",
    border: "1px solid #3a3a4a",
    borderRadius: 6,
    boxShadow: "0 6px 24px rgba(0,0,0,0.6)",
    maxHeight: 220,
    overflowY: "auto",
    zIndex: 50,
  },
  dropdownItem: {
    display: "grid",
    gridTemplateColumns: "auto auto 1fr",
    gap: 8,
    alignItems: "center",
    padding: "6px 10px",
    cursor: "pointer",
    borderBottom: "1px solid #1f1f2a",
  },
  dropdownItemActive: {
    background: "#1e1b4b",
  },
  dropdownLabel: {
    color: "#e5e7eb",
    fontSize: 12,
    fontWeight: 600,
  },
  dropdownId: {
    color: "#a78bfa",
    fontSize: 11,
    fontFamily: "monospace",
  },
  dropdownDesc: {
    color: "#666",
    fontSize: 11,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  sourcePanel: {
    background: "#15151f",
    border: "1px solid #2a2a3a",
    borderRadius: 6,
    padding: "8px 10px",
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  sourceHeader: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
  },
  sourceTitle: {
    color: "#c4b5fd",
    fontSize: 12,
    fontWeight: 700,
  },
  sourceTag: {
    color: "#666",
    fontSize: 10,
    fontFamily: "monospace",
    fontWeight: 400,
  },
  sourceDesc: {
    color: "#888",
    fontSize: 11,
  },
  effectRow: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    fontSize: 11,
  },
  effectLabel: {
    color: "#a78bfa",
    minWidth: 90,
    fontWeight: 600,
  },
  effectValue: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    color: "#ddd",
    flexWrap: "wrap",
  },
  effectValueMuted: {
    color: "#666",
    fontStyle: "italic",
  },
  previewSwatch: {
    background: "#0a0a0a",
    border: "1px solid #2a2a2a",
    borderRadius: 4,
    padding: "2px 6px",
    fontSize: 12,
  },
  effectDetail: {
    color: "#666",
    fontSize: 10,
  },
};
