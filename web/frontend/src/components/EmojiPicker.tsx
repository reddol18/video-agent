/**
 * #emoji-catalog — 이모지 선택기.
 *
 * 이모지가 4개일 때는 콤보박스로 충분했지만 96개로 늘면서 목록을 훑기 어려워졌다.
 * 카테고리 탭 + 검색 + 그리드로 바꿔 한눈에 고를 수 있게 한다.
 *
 * - 선택값(key)은 백엔드 `EMOJI_ICON_TABLE` 의 키와 1:1. 저장값이라 바뀌지 않는다.
 * - 검색은 키 문자열(영문)과 이모지 문자 양쪽을 대상으로 한다.
 * - 접힌 상태에서는 현재 선택된 이모지만 보여 인스펙터 공간을 아낀다.
 */
import { useMemo, useState } from "react";

import { EMOJI_BY_KEY, EMOJI_GROUPS } from "../lib/emojiCatalog";

export interface EmojiPickerProps {
  value: string;
  onChange: (key: string) => void;
}

export default function EmojiPicker({ value, onChange }: EmojiPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<string>(EMOJI_GROUPS[0]?.label ?? "");

  const q = query.trim().toLowerCase();

  // 검색어가 있으면 전체에서 찾고, 없으면 선택된 탭만 보여준다.
  const shown = useMemo(() => {
    if (q) {
      return EMOJI_GROUPS.flatMap((g) => g.items).filter(
        (i) => i.key.includes(q) || i.char === query.trim(),
      );
    }
    return EMOJI_GROUPS.find((g) => g.label === tab)?.items ?? [];
  }, [q, query, tab]);

  const current = EMOJI_BY_KEY[value] ?? "❓";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <label style={{ color: "#666", fontSize: 11 }}>아이콘</label>

      {/* 현재 선택 — 누르면 목록이 펼쳐진다. */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={open ? "목록 접기" : "이모지 고르기"}
        style={{
          display: "flex", alignItems: "center", gap: 8,
          background: "#1a1a1a", color: "#ccc",
          border: "1px solid #333", borderRadius: 4,
          padding: "4px 8px", fontSize: 12, cursor: "pointer",
        }}
      >
        <span style={{ fontSize: 20, lineHeight: 1 }}>{current}</span>
        <span style={{ color: "#888" }}>{value}</span>
        <span style={{ marginLeft: "auto", color: "#666" }}>{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div
          style={{
            background: "#161616", border: "1px solid #333",
            borderRadius: 4, padding: 6,
            display: "flex", flexDirection: "column", gap: 6,
          }}
        >
          <input
            type="text"
            value={query}
            placeholder="검색 (예: fire, star)"
            onChange={(e) => setQuery(e.target.value)}
            style={{
              background: "#1e1e1e", color: "#ddd",
              border: "1px solid #3a3a3a", borderRadius: 4,
              padding: "3px 6px", fontSize: 11,
            }}
          />

          {/* 카테고리 탭 — 검색 중에는 전체를 뒤지므로 숨긴다. */}
          {!q && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
              {EMOJI_GROUPS.map((g) => {
                const active = g.label === tab;
                return (
                  <button
                    key={g.label}
                    type="button"
                    onClick={() => setTab(g.label)}
                    style={{
                      fontSize: 10, padding: "2px 7px", borderRadius: 8,
                      cursor: "pointer",
                      border: `1px solid ${active ? "#a78bfa" : "#3a3a3a"}`,
                      background: active ? "#a78bfa" : "#2a2a2a",
                      color: active ? "#1a1a1a" : "#bbb",
                      fontWeight: active ? 600 : 400,
                    }}
                  >
                    {g.label}
                  </button>
                );
              })}
            </div>
          )}

          {/* 이모지 그리드 */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(34px, 1fr))",
              gap: 3, maxHeight: 180, overflowY: "auto",
            }}
          >
            {shown.map((i) => {
              const active = i.key === value;
              return (
                <button
                  key={i.key}
                  type="button"
                  title={i.key}
                  onClick={() => { onChange(i.key); setOpen(false); }}
                  style={{
                    fontSize: 20, lineHeight: 1, padding: 4,
                    borderRadius: 4, cursor: "pointer",
                    border: `1px solid ${active ? "#a78bfa" : "transparent"}`,
                    background: active ? "#2f2a45" : "#1e1e1e",
                  }}
                >
                  {i.char}
                </button>
              );
            })}
          </div>

          {shown.length === 0 && (
            <span style={{ color: "#666", fontSize: 11 }}>
              "{query}" 에 맞는 이모지가 없습니다
            </span>
          )}
        </div>
      )}
    </div>
  );
}
