/**
 * SceneCommentEditor — 씬 편집 지시 코멘트 UI
 *
 * 김감독이 씬에 "여기 컷 더 짧게" 같은 편집 지시를 남기면 서버에 저장되고,
 * 나중에 클로드 에이전트가 open 코멘트를 읽어 수정한 뒤 완료 처리한다.
 * 완료된 코멘트는 삭제되지 않고 접힌 이력으로 남는다.
 *
 * 구성:
 *  - 입력창 (Enter 저장 / Shift+Enter 줄바꿈)
 *  - 열린 코멘트 목록 (수정 · 삭제)
 *  - "완료된 코멘트 N건" 접기 토글 → 펼치면 처리 메모 + 처리 시각
 *
 * 서버가 진실의 원천이다. 이 컴포넌트는 자체 API 호출로 코멘트를 CRUD 하고,
 * 결과를 onCommentsChange 로 올려보내 상위(zustand/IndexedDB)와 동기화한다.
 * 코멘트는 렌더 결과에 영향을 주지 않으므로 preview-cache 무효화가 필요 없다.
 */
import { useMemo, useRef, useState } from "react";
import type { CommentImage, SceneComment } from "../types";
import {
  createSceneComment,
  deleteSceneComment,
  reopenSceneComment,
  resolveSceneComment,
  updateSceneComment,
  uploadCommentImage,
} from "../api/client";

interface Props {
  projectId: string;
  sceneId: number;
  comments: SceneComment[];
  /** 서버 반영 후 최신 목록을 상위로 전달 (zustand + IndexedDB 동기화용). */
  onCommentsChange: (comments: SceneComment[]) => void;
}

/** ISO8601 → "3월 5일 14:22" 형태의 짧은 로컬 표기. */
function formatWhen(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getMonth() + 1}월 ${d.getDate()}일 ${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

export default function SceneCommentEditor({ projectId, sceneId, comments, onCommentsChange }: Props) {
  const [draft, setDraft] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 수정 중인 코멘트 id + 편집 텍스트
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  // 아직 저장되지 않은(작성 중) 코멘트에 붙인 이미지들. 저장 시 함께 올라간다.
  const [draftImages, setDraftImages] = useState<CommentImage[]>([]);
  const [uploading, setUploading] = useState(false);
  const draftRef = useRef<HTMLTextAreaElement>(null);

  const open = useMemo(() => comments.filter((c) => c.status === "open"), [comments]);
  const done = useMemo(() => comments.filter((c) => c.status === "done"), [comments]);

  /** API 호출을 감싸 busy/error 를 일관되게 처리한다. */
  async function run(fn: () => Promise<SceneComment[]>) {
    setBusy(true);
    setError(null);
    try {
      onCommentsChange(await fn());
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || "코멘트 처리에 실패했습니다");
    } finally {
      setBusy(false);
    }
  }

  /** 커서 위치에 자리표시자를 끼워 넣는다. 커서를 못 읽으면 끝에 덧붙인다. */
  function insertPlaceholder(token: string) {
    const el = draftRef.current;
    setDraft((prev) => {
      if (!el) return prev ? `${prev} ${token}` : token;
      const start = el.selectionStart ?? prev.length;
      const end = el.selectionEnd ?? prev.length;
      const before = prev.slice(0, start);
      const after = prev.slice(end);
      // 앞뒤로 공백을 한 칸씩 보장해 "짧게[이미지1]처럼" 붙는 것을 막는다.
      const lead = before && !/\s$/.test(before) ? " " : "";
      const tail = after && !/^\s/.test(after) ? " " : "";
      const next = `${before}${lead}${token}${tail}${after}`;
      const caret = before.length + lead.length + token.length;
      requestAnimationFrame(() => {
        el.focus();
        try { el.setSelectionRange(caret, caret); } catch { /* ignore */ }
      });
      return next;
    });
  }

  /** 클립보드/드롭으로 들어온 이미지들을 업로드하고 본문에 자리표시자를 넣는다. */
  async function attachImages(files: (File | Blob)[]) {
    if (!files.length || uploading) return;
    setUploading(true);
    setError(null);
    try {
      for (const f of files) {
        const img = await uploadCommentImage(projectId, sceneId, f);
        setDraftImages((prev) => [...prev, img]);
        insertPlaceholder(`[이미지${img.index}]`);
      }
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || "이미지 업로드에 실패했습니다");
    } finally {
      setUploading(false);
    }
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const items = Array.from(e.clipboardData?.items || []);
    const imgs = items
      .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
      .map((it) => it.getAsFile())
      .filter((f): f is File => !!f);
    if (!imgs.length) return; // 순수 텍스트 붙여넣기는 기본 동작 유지
    e.preventDefault();
    void attachImages(imgs);
  }

  async function handleAdd() {
    const text = draft.trim();
    if (!text || busy) return;
    await run(async () => {
      const created = await createSceneComment(projectId, sceneId, text, draftImages);
      setDraft("");
      setDraftImages([]);
      return [...comments, created];
    });
  }

  async function handleSaveEdit(id: string) {
    const text = editingText.trim();
    if (!text || busy) return;
    await run(async () => {
      const updated = await updateSceneComment(projectId, sceneId, id, text);
      setEditingId(null);
      return comments.map((c) => (c.id === id ? updated : c));
    });
  }

  async function handleResolve(id: string) {
    await run(async () => {
      const updated = await resolveSceneComment(projectId, sceneId, id, { resolvedBy: "user" });
      return comments.map((c) => (c.id === id ? updated : c));
    });
  }

  async function handleReopen(id: string) {
    await run(async () => {
      const updated = await reopenSceneComment(projectId, sceneId, id);
      return comments.map((c) => (c.id === id ? updated : c));
    });
  }

  async function handleDelete(id: string) {
    if (!window.confirm("이 코멘트를 삭제할까요? 처리 이력도 함께 사라집니다.")) return;
    await run(async () => {
      await deleteSceneComment(projectId, sceneId, id);
      return comments.filter((c) => c.id !== id);
    });
  }

  return (
    <div
      style={styles.container}
      // dnd-kit 이 카드 전체 드래그를 가로채지 않도록 자체 영역에서 stopPropagation
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div style={styles.header}>
        <span style={styles.headerLabel}>편집 지시</span>
        <span style={styles.headerHint}>
          {open.length > 0 ? `처리 대기 ${open.length}건` : "Enter 저장 · Shift+Enter 줄바꿈"}
        </span>
      </div>

      {/* 열린 코멘트 — 에이전트가 처리할 작업 목록 */}
      {open.map((c) =>
        editingId === c.id ? (
          <div key={c.id} style={styles.editRow}>
            <textarea
              style={styles.input}
              value={editingText}
              onChange={(e) => setEditingText(e.target.value)}
              rows={2}
              autoFocus
            />
            <div style={styles.editActions}>
              <button style={styles.btnPrimary} onClick={() => handleSaveEdit(c.id)} disabled={busy}>
                저장
              </button>
              <button style={styles.btnGhost} onClick={() => setEditingId(null)} disabled={busy}>
                취소
              </button>
            </div>
          </div>
        ) : (
          <div key={c.id} style={styles.openItem}>
            <span style={styles.bullet}>●</span>
            <span style={styles.openBody}>
              {c.body}
              {(c.images?.length ?? 0) > 0 && (
                <span style={styles.savedThumbRow}>
                  {c.images!.map((img) => (
                    <a
                      key={img.id}
                      href={img.url}
                      target="_blank"
                      rel="noreferrer"
                      style={styles.thumbWrap}
                      title={`[이미지${img.index}] — 새 탭으로 열기`}
                    >
                      <img src={img.url} alt={`이미지${img.index}`} style={styles.thumb} />
                      <span style={styles.thumbBadge}>{img.index}</span>
                    </a>
                  ))}
                </span>
              )}
            </span>
            <span style={styles.itemActions}>
              <button
                style={styles.iconBtn}
                title="완료 처리"
                onClick={() => handleResolve(c.id)}
                disabled={busy}
              >
                ✓
              </button>
              <button
                style={styles.iconBtn}
                title="수정"
                onClick={() => {
                  setEditingId(c.id);
                  setEditingText(c.body);
                }}
                disabled={busy}
              >
                ✎
              </button>
              <button style={styles.iconBtn} title="삭제" onClick={() => handleDelete(c.id)} disabled={busy}>
                ✕
              </button>
            </span>
          </div>
        ),
      )}

      {/* 새 코멘트 입력 — 이미지 붙여넣기(Ctrl/Cmd+V) 및 드롭 지원 */}
      <textarea
        ref={draftRef}
        style={styles.input}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onPaste={handlePaste}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          const imgs = Array.from(e.dataTransfer?.files || []).filter((f) => f.type.startsWith("image/"));
          if (!imgs.length) return;
          e.preventDefault();
          void attachImages(imgs);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void handleAdd();
          }
        }}
        rows={2}
        placeholder="편집 지시 남기기 (예: 컷을 더 짧게 / 이미지 붙여넣기 Ctrl+V)"
        disabled={busy}
      />

      {/* 작성 중 붙임 이미지 — 번호가 본문의 [이미지N] 과 대응 */}
      {(draftImages.length > 0 || uploading) && (
        <div style={styles.thumbRow}>
          {draftImages.map((img) => (
            <div key={img.id} style={styles.thumbWrap} title={`[이미지${img.index}] — ${img.filename}`}>
              <img src={img.url} alt={`이미지${img.index}`} style={styles.thumb} />
              <span style={styles.thumbBadge}>{img.index}</span>
              <button
                style={styles.thumbRemove}
                title="이 이미지 빼기 (본문의 자리표시자도 지워집니다)"
                onClick={() => {
                  setDraftImages((prev) => prev.filter((x) => x.id !== img.id));
                  setDraft((prev) => prev.replace(new RegExp(`\\s*\\[이미지${img.index}\\]`, "g"), ""));
                }}
                disabled={busy || uploading}
              >
                ✕
              </button>
            </div>
          ))}
          {uploading && <span style={styles.uploadingHint}>올리는 중…</span>}
        </div>
      )}

      {error && <div style={styles.error}>{error}</div>}

      {/* 완료 이력 — 기본 접힘 */}
      {done.length > 0 && (
        <div style={styles.historyWrap}>
          <div style={styles.historyToggle} onClick={() => setHistoryOpen((v) => !v)}>
            <span style={styles.caret}>{historyOpen ? "▾" : "▸"}</span>
            완료된 코멘트 {done.length}건
          </div>
          {historyOpen &&
            done.map((c) => (
              <div key={c.id} style={styles.doneItem}>
                <div style={styles.doneBodyRow}>
                  <span style={styles.doneBody}>{c.body}</span>
                  <span style={styles.itemActions}>
                    <button
                      style={styles.iconBtn}
                      title="다시 열기"
                      onClick={() => handleReopen(c.id)}
                      disabled={busy}
                    >
                      ↩
                    </button>
                    <button
                      style={styles.iconBtn}
                      title="삭제"
                      onClick={() => handleDelete(c.id)}
                      disabled={busy}
                    >
                      ✕
                    </button>
                  </span>
                </div>
                <div style={styles.doneMeta}>
                  {c.resolved_by === "agent" ? "🤖 에이전트" : "👤 직접"} 처리 · {formatWhen(c.resolved_at)}
                </div>
                {c.resolution_note && <div style={styles.doneNote}>↳ {c.resolution_note}</div>}
              </div>
            ))}
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
    color: "#38bdf8",
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: 0.4,
    textTransform: "uppercase",
  },
  headerHint: { color: "#555", fontSize: 10 },
  openItem: {
    display: "flex",
    alignItems: "flex-start",
    gap: 6,
    background: "#12202e",
    border: "1px solid #1e3a5f",
    borderRadius: 6,
    padding: "5px 8px",
  },
  bullet: { color: "#38bdf8", fontSize: 8, lineHeight: "16px" },
  openBody: {
    flex: 1,
    color: "#dbeafe",
    fontSize: 12,
    lineHeight: 1.5,
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  },
  itemActions: { display: "inline-flex", gap: 2, flexShrink: 0 },
  iconBtn: {
    background: "transparent",
    border: "none",
    color: "#64748b",
    cursor: "pointer",
    fontSize: 12,
    padding: "0 3px",
    lineHeight: "16px",
  },
  editRow: { display: "flex", flexDirection: "column", gap: 4 },
  editActions: { display: "flex", gap: 4 },
  btnPrimary: {
    background: "#1d4ed8",
    border: "none",
    color: "#fff",
    borderRadius: 4,
    padding: "3px 10px",
    fontSize: 11,
    cursor: "pointer",
  },
  btnGhost: {
    background: "transparent",
    border: "1px solid #2a2a3a",
    color: "#94a3b8",
    borderRadius: 4,
    padding: "3px 10px",
    fontSize: 11,
    cursor: "pointer",
  },
  input: {
    background: "#08080c",
    border: "1px solid #2a2a3a",
    borderRadius: 6,
    color: "#e2e8f0",
    fontSize: 12,
    padding: "6px 8px",
    resize: "vertical",
    fontFamily: "inherit",
    lineHeight: 1.5,
  },
  error: { color: "#f87171", fontSize: 11 },
  thumbRow: { display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" },
  savedThumbRow: { display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 },
  thumbWrap: {
    position: "relative",
    display: "inline-block",
    lineHeight: 0,
    borderRadius: 4,
    overflow: "hidden",
    border: "1px solid #2a2a3a",
  },
  thumb: { width: 56, height: 56, objectFit: "cover", display: "block" },
  thumbBadge: {
    position: "absolute",
    left: 0,
    top: 0,
    background: "#1d4ed8",
    color: "#fff",
    fontSize: 9,
    fontWeight: 700,
    lineHeight: "14px",
    minWidth: 14,
    textAlign: "center",
    padding: "0 3px",
  },
  thumbRemove: {
    position: "absolute",
    right: 0,
    top: 0,
    background: "rgba(0,0,0,0.65)",
    border: "none",
    color: "#fca5a5",
    fontSize: 10,
    lineHeight: "14px",
    padding: "0 4px",
    cursor: "pointer",
  },
  uploadingHint: { color: "#64748b", fontSize: 10 },
  historyWrap: { display: "flex", flexDirection: "column", gap: 4 },
  historyToggle: {
    color: "#64748b",
    fontSize: 11,
    cursor: "pointer",
    userSelect: "none",
    display: "flex",
    alignItems: "center",
    gap: 4,
  },
  caret: { fontSize: 9 },
  doneItem: {
    borderLeft: "2px solid #334155",
    paddingLeft: 8,
    display: "flex",
    flexDirection: "column",
    gap: 2,
  },
  doneBodyRow: { display: "flex", alignItems: "flex-start", gap: 6 },
  doneBody: {
    flex: 1,
    color: "#64748b",
    fontSize: 11,
    textDecoration: "line-through",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  },
  doneMeta: { color: "#475569", fontSize: 10 },
  doneNote: { color: "#7c8ba1", fontSize: 10, lineHeight: 1.5 },
};
