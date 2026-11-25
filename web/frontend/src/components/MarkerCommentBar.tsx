/**
 * MarkerCommentBar — 지점 편집 코멘트 (구간 분할 보조)
 * ─────────────────────────────────────────────────────────────────────────
 * 구간 분할 화면에서 영상의 특정 시각에 편집 지시 코멘트를 꽂는 띠.
 * 씬 카드의 편집 코멘트(씬 단위)와 달리 **푸티지 타임라인의 한 지점**에 묶인다.
 * 예) "이 지점 앞 3초 ~ 뒤 5초까지 잘라서 '…' 대사가 들어가게"
 *
 * 동작:
 *   1) 트랙 클릭 → 그 시각으로 seek + 새 코멘트 입력창 열림.
 *      "현재 위치에 코멘트" 버튼은 재생 위치 그대로 입력창을 연다.
 *   2) 핀 클릭 → 해당 시각으로 seek + 그 코멘트 편집.
 *   3) 저장/삭제는 `onChange(다음 마커 배열)` 로 호출부에 위임한다(호출부가 영구 저장).
 *
 * 저장 실패는 이 컴포넌트 안에서만 표시하고 바깥으로 throw 하지 않는다
 * (기존 분할 UI 는 마커와 무관하게 동작해야 하므로).
 */

import { useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import type { FootageMarker } from "../types";

function formatTime(sec: number): string {
  const t = Math.max(0, sec);
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

/** 편집 중인 코멘트 — marker_id 가 null 이면 새 코멘트. */
interface Draft {
  marker_id: string | null;
  time_sec: number;
  comment: string;
}

export default function MarkerCommentBar({
  duration,
  currentTime,
  markers,
  onSeek,
  onChange,
}: {
  duration: number;
  currentTime: number;
  markers: FootageMarker[];
  onSeek: (sec: number) => void;
  onChange: (next: FootageMarker[]) => Promise<void>;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const sorted = [...markers].sort((a, b) => a.time_sec - b.time_sec);
  const pct = (t: number) => (duration > 0 ? Math.min(100, Math.max(0, (t / duration) * 100)) : 0);

  function openNew(t: number) {
    setError("");
    setDraft({ marker_id: null, time_sec: t, comment: "" });
  }

  function openExisting(m: FootageMarker) {
    setError("");
    onSeek(m.time_sec);
    setDraft({ marker_id: m.marker_id, time_sec: m.time_sec, comment: m.comment });
  }

  function handleTrackClick(e: React.MouseEvent<HTMLDivElement>) {
    const el = trackRef.current;
    if (!el || duration <= 0) return;
    const rect = el.getBoundingClientRect();
    const t = Math.round(((e.clientX - rect.left) / rect.width) * duration * 10) / 10;
    const clamped = Math.min(duration, Math.max(0, t));
    onSeek(clamped);
    openNew(clamped);
  }

  async function commit(next: FootageMarker[]) {
    setSaving(true);
    setError("");
    try {
      await onChange(next);
      setDraft(null);
    } catch (e) {
      setError(`코멘트 저장 실패: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  function handleSave() {
    if (!draft || !draft.comment.trim()) return;
    const comment = draft.comment.trim();
    const next = draft.marker_id
      ? markers.map((m) => (m.marker_id === draft.marker_id ? { ...m, comment } : m))
      : [
          ...markers,
          {
            marker_id: uuidv4(),
            time_sec: draft.time_sec,
            comment,
            created_at: new Date().toISOString(),
          },
        ];
    void commit(next);
  }

  function handleDelete(id: string) {
    if (!window.confirm("이 코멘트를 삭제할까요?")) return;
    void commit(markers.filter((m) => m.marker_id !== id));
  }

  return (
    <div style={st.wrap}>
      <div style={st.header}>
        <span style={st.title}>📍 지점 편집 코멘트</span>
        <span style={st.hint}>띠를 클릭하면 그 지점에 코멘트를 남깁니다</span>
        <button
          style={st.addBtn}
          onClick={() => openNew(Math.round(currentTime * 10) / 10)}
          disabled={duration <= 0 || saving}
        >
          + 현재 위치({formatTime(currentTime)})에 코멘트
        </button>
      </div>

      <div ref={trackRef} style={st.track} onClick={handleTrackClick} title="클릭해서 이 지점에 코멘트 추가">
        <div style={{ ...st.playhead, left: `${pct(currentTime)}%` }} />
        {draft && draft.marker_id === null && (
          <div style={{ ...st.pin, ...st.pinDraft, left: `${pct(draft.time_sec)}%` }} />
        )}
        {sorted.map((m) => (
          <div
            key={m.marker_id}
            style={{
              ...st.pin,
              left: `${pct(m.time_sec)}%`,
              ...(draft?.marker_id === m.marker_id ? st.pinActive : null),
            }}
            title={`${formatTime(m.time_sec)} — ${m.comment}`}
            onClick={(e) => {
              e.stopPropagation();
              openExisting(m);
            }}
          />
        ))}
      </div>

      {draft && (
        <div style={st.editor}>
          <div style={st.editorHead}>
            <span style={{ color: "#a5b4fc", fontWeight: 700 }}>{formatTime(draft.time_sec)}</span>
            <span style={{ color: "#888" }}>{draft.marker_id ? "코멘트 수정" : "새 코멘트"}</span>
          </div>
          <textarea
            autoFocus
            style={st.textarea}
            rows={3}
            value={draft.comment}
            placeholder="예: 이 지점 앞 3초 ~ 뒤 5초까지 잘라서 '여기서부터가 진짜 시작입니다' 대사 넣기"
            onChange={(e) => setDraft({ ...draft, comment: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) handleSave();
              if (e.key === "Escape") setDraft(null);
            }}
          />
          <div style={st.editorActions}>
            <button style={st.saveBtn} onClick={handleSave} disabled={saving || !draft.comment.trim()}>
              {saving ? "저장 중..." : "저장 (Ctrl+Enter)"}
            </button>
            <button style={st.cancelBtn} onClick={() => setDraft(null)} disabled={saving}>
              취소
            </button>
          </div>
        </div>
      )}

      {error && <div style={st.error}>{error}</div>}

      {sorted.length > 0 && (
        <ul style={st.list}>
          {sorted.map((m) => (
            <li
              key={m.marker_id}
              style={{ ...st.item, ...(draft?.marker_id === m.marker_id ? st.itemActive : null) }}
            >
              <button style={st.timeBtn} onClick={() => onSeek(m.time_sec)} title="이 지점으로 이동">
                {formatTime(m.time_sec)}
              </button>
              <span style={st.comment} onClick={() => openExisting(m)} title="클릭해서 수정">
                {m.comment}
              </span>
              <button style={st.delBtn} onClick={() => handleDelete(m.marker_id)} disabled={saving} title="삭제">
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const st: Record<string, React.CSSProperties> = {
  wrap: { marginTop: 16, padding: 12, background: "#161616", border: "1px solid #2a2a2a", borderRadius: 8 },
  header: { display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" },
  title: { color: "#fff", fontWeight: 700, fontSize: 14 },
  hint: { color: "#777", fontSize: 12, flex: 1 },
  addBtn: {
    background: "#312e81", color: "#e0e7ff", border: "1px solid #4f46e5", borderRadius: 6,
    padding: "4px 10px", fontSize: 12, cursor: "pointer",
  },
  track: {
    position: "relative", height: 28, background: "#222", border: "1px solid #333",
    borderRadius: 4, cursor: "crosshair",
  },
  playhead: { position: "absolute", top: 0, bottom: 0, width: 2, marginLeft: -1, background: "#ef4444", pointerEvents: "none" },
  pin: {
    position: "absolute", top: 3, width: 12, height: 20, marginLeft: -6, borderRadius: "6px 6px 2px 2px",
    background: "#f59e0b", border: "1px solid #78350f", cursor: "pointer",
  },
  pinDraft: { background: "#a5b4fc", border: "1px dashed #4f46e5", pointerEvents: "none" },
  pinActive: { background: "#fde047", boxShadow: "0 0 0 2px #6366f1" },
  editor: { marginTop: 10, padding: 10, background: "#1e1e2e", border: "1px solid #3730a3", borderRadius: 6 },
  editorHead: { display: "flex", gap: 10, fontSize: 12, marginBottom: 6 },
  textarea: {
    width: "100%", boxSizing: "border-box", background: "#111", color: "#eee", border: "1px solid #333",
    borderRadius: 4, padding: 8, fontSize: 13, resize: "vertical", fontFamily: "inherit",
  },
  editorActions: { display: "flex", gap: 8, marginTop: 6 },
  saveBtn: {
    background: "#4f46e5", color: "#fff", border: "none", borderRadius: 6,
    padding: "5px 12px", fontSize: 12, cursor: "pointer",
  },
  cancelBtn: {
    background: "transparent", color: "#aaa", border: "1px solid #444", borderRadius: 6,
    padding: "5px 12px", fontSize: 12, cursor: "pointer",
  },
  error: { marginTop: 8, color: "#fca5a5", fontSize: 12 },
  list: { listStyle: "none", margin: "10px 0 0", padding: 0, display: "flex", flexDirection: "column", gap: 4 },
  item: {
    display: "flex", alignItems: "flex-start", gap: 8, padding: "6px 8px",
    background: "#1b1b1b", borderRadius: 4, border: "1px solid transparent",
  },
  itemActive: { border: "1px solid #6366f1" },
  timeBtn: {
    background: "#292524", color: "#fbbf24", border: "1px solid #57534e", borderRadius: 4,
    padding: "2px 6px", fontSize: 12, cursor: "pointer", fontVariantNumeric: "tabular-nums", flexShrink: 0,
  },
  comment: { color: "#ddd", fontSize: 13, flex: 1, whiteSpace: "pre-wrap", cursor: "pointer" },
  delBtn: { background: "transparent", color: "#888", border: "none", cursor: "pointer", fontSize: 12, flexShrink: 0 },
};
