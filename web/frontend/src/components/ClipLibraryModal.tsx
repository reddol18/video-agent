/**
 * 커스텀 영상 클립 라이브러리 선택 모달.
 *
 * 밈영상 · 로딩 애니메이션처럼 재사용하는 영상을 URL 로 등록해 두고, 씬 삽입 시
 * 바로 지정한다. 구조는 TemplatePickerModal 을 참고하되 영상 썸네일 그리드 +
 * URL 등록 폼 + 삭제를 포함한다.
 *
 * 부모(Editor.tsx)가 onPick 을 받아 "빈 씬 삽입 → from-clip-library 지정" 흐름을
 * 담당한다. 이 컴포넌트는 등록 · 표시 · 선택 · 삭제까지만 책임진다.
 */
import { useEffect, useState } from "react";
import {
  listClipLibrary,
  registerClip,
  deleteClip,
  clipThumbnailUrl,
  clipStreamUrl,
  type ClipLibraryEntry,
} from "../api/client";
import { ui } from "../store/ui";

export interface ClipLibraryModalProps {
  open: boolean;
  onClose: () => void;
  /** 클립 선택 — 부모가 씬 삽입 + 지정을 수행한다. */
  onPick: (clip: ClipLibraryEntry) => void;
}

function formatDuration(sec: number): string {
  if (!sec || sec <= 0) return "-";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${sec.toFixed(1)}초`;
}

function formatSize(bytes: number): string {
  if (!bytes) return "";
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)}MB` : `${Math.round(bytes / 1024)}KB`;
}

export default function ClipLibraryModal({ open, onClose, onPick }: ClipLibraryModalProps) {
  const [clips, setClips] = useState<ClipLibraryEntry[] | null>(null);
  const [error, setError] = useState<string>("");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState("");
  const [registering, setRegistering] = useState(false);
  // hover 시 <video> 로 자동재생 미리보기를 띄울 클립.
  const [previewId, setPreviewId] = useState<string | null>(null);

  async function reload() {
    setError("");
    try {
      setClips(await listClipLibrary());
    } catch (e) {
      setError(`클립 목록 불러오기 실패: ${(e as Error).message}`);
      setClips([]);
    }
  }

  useEffect(() => {
    if (!open) return;
    reload();
  }, [open]);

  async function handleRegister() {
    const u = url.trim();
    if (!u) return;
    setRegistering(true);
    setError("");
    try {
      const entry = await registerClip({
        url: u,
        title: title.trim() || undefined,
        category: category.trim() || undefined,
      });
      ui.success(`"${entry.title}" 클립이 등록되었습니다`);
      setUrl("");
      setTitle("");
      setCategory("");
      await reload();
    } catch (e) {
      // 백엔드가 yt-dlp 미설치 등 조치 가능한 사유를 400 detail 로 내려준다.
      const detail =
        (e as { response?: { data?: { detail?: string } } }).response?.data?.detail ??
        (e as Error).message;
      setError(detail);
    } finally {
      setRegistering(false);
    }
  }

  async function handleDelete(clip: ClipLibraryEntry) {
    const ok = await ui.confirm({
      title: "클립 삭제",
      message:
        `"${clip.title}" 을 라이브러리에서 삭제합니다.\n` +
        "이미 씬에 사용한 영상은 프로젝트에 복사되어 있으므로 영향받지 않습니다.",
      variant: "danger",
      confirmLabel: "삭제",
    });
    if (!ok) return;
    try {
      await deleteClip(clip.clip_id);
      ui.success("클립이 삭제되었습니다");
      await reload();
    } catch (e) {
      ui.error(`클립 삭제 실패: ${(e as Error).message}`);
    }
  }

  if (!open) return null;

  return (
    <div style={styles.backdrop} onClick={onClose}>
      <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div style={styles.header}>
          <div style={styles.title}>클립 라이브러리에서 씬 삽입</div>
          <button style={styles.closeBtn} onClick={onClose} title="닫기">×</button>
        </div>
        <div style={styles.subtitle}>
          밈영상 · 로딩 애니메이션처럼 자주 쓰는 영상을 등록해 두고 씬으로 바로 삽입합니다.
          선택하면 현재 위치 다음에 새 씬이 만들어지고 이 영상이 지정됩니다.
        </div>

        {/* URL 등록 폼 */}
        <div style={styles.form}>
          <input
            style={styles.input}
            placeholder="영상 URL (.mp4/.webm/.gif 직접 링크 또는 YouTube 주소)"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !registering) handleRegister(); }}
            disabled={registering}
          />
          <div style={styles.formRow}>
            <input
              style={{ ...styles.input, flex: 2 }}
              placeholder="제목 (선택)"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={registering}
            />
            <input
              style={{ ...styles.input, flex: 1 }}
              placeholder="분류 (선택)"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              disabled={registering}
            />
            <button
              style={{ ...styles.addBtn, ...(registering || !url.trim() ? styles.addBtnDisabled : {}) }}
              onClick={handleRegister}
              disabled={registering || !url.trim()}
            >
              {registering ? "다운로드 중..." : "+ 등록"}
            </button>
          </div>
        </div>

        {error && <div style={styles.error}>{error}</div>}

        {clips === null && <div style={styles.loading}>불러오는 중...</div>}

        {clips !== null && clips.length === 0 && (
          <div style={styles.empty}>
            등록된 클립이 없습니다.<br />
            위에 영상 URL 을 입력해 첫 클립을 등록해 보세요.
          </div>
        )}

        {clips !== null && clips.length > 0 && (
          <div style={styles.grid}>
            {clips.map((c) => (
              <div
                key={c.clip_id}
                style={styles.card}
                onMouseEnter={() => setPreviewId(c.clip_id)}
                onMouseLeave={() => setPreviewId(null)}
              >
                <div style={styles.thumbWrap} onClick={() => onPick(c)} title="이 클립으로 씬 삽입">
                  {previewId === c.clip_id ? (
                    // hover 중에만 실제 영상을 로드한다 (목록 진입 시 전체 로드 방지).
                    <video
                      src={clipStreamUrl(c.clip_id)}
                      style={styles.thumb}
                      autoPlay
                      loop
                      muted
                      playsInline
                    />
                  ) : (
                    <img src={clipThumbnailUrl(c.clip_id)} style={styles.thumb} alt={c.title} loading="lazy" />
                  )}
                  <div style={styles.durationBadge}>{formatDuration(c.duration_sec)}</div>
                </div>
                <div style={styles.cardBody}>
                  <div style={styles.cardTitle} title={c.title}>{c.title}</div>
                  <div style={styles.cardMeta}>
                    {c.category && <span style={styles.categoryTag}>{c.category}</span>}
                    {c.width > 0 && `${c.width}×${c.height}`}
                    {c.size_bytes > 0 && ` · ${formatSize(c.size_bytes)}`}
                  </div>
                </div>
                <div style={styles.cardActions}>
                  <button style={styles.pickBtn} onClick={() => onPick(c)}>씬으로 삽입 →</button>
                  <button style={styles.deleteBtn} onClick={() => handleDelete(c)} title="라이브러리에서 삭제">
                    삭제
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  backdrop: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.6)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
  },
  modal: {
    background: "#1a1a1a",
    border: "1px solid #2a2a2a",
    borderRadius: 12,
    padding: "20px 24px",
    width: "min(860px, 94vw)",
    maxHeight: "84vh",
    overflowY: "auto",
    color: "#e4e4e4",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 6,
  },
  title: { fontSize: 18, fontWeight: 600, color: "#fff" },
  closeBtn: {
    background: "transparent",
    border: "none",
    color: "#999",
    fontSize: 22,
    cursor: "pointer",
    padding: 0,
    lineHeight: 1,
  },
  subtitle: { fontSize: 12, color: "#888", marginBottom: 16, lineHeight: 1.5 },
  form: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 8,
    padding: 12,
    background: "#222",
    border: "1px solid #2a2a2a",
    borderRadius: 8,
    marginBottom: 14,
  },
  formRow: { display: "flex", gap: 8 },
  input: {
    flex: 1,
    padding: "8px 10px",
    background: "#151515",
    border: "1px solid #333",
    borderRadius: 6,
    color: "#e4e4e4",
    fontSize: 13,
    outline: "none",
    minWidth: 0,
  },
  addBtn: {
    padding: "8px 16px",
    background: "#2563eb",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
    whiteSpace: "nowrap" as const,
  },
  addBtnDisabled: { background: "#374151", color: "#9ca3af", cursor: "not-allowed" },
  loading: { padding: 24, textAlign: "center" as const, color: "#999" },
  error: {
    padding: 12,
    background: "#3b1f1f",
    border: "1px solid #5a2a2a",
    borderRadius: 6,
    color: "#f87171",
    fontSize: 13,
    marginBottom: 12,
    whiteSpace: "pre-wrap" as const,
    lineHeight: 1.6,
  },
  empty: {
    padding: 32,
    textAlign: "center" as const,
    color: "#999",
    fontSize: 13,
    lineHeight: 1.7,
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))",
    gap: 12,
  },
  card: {
    display: "flex",
    flexDirection: "column" as const,
    background: "#222",
    border: "1px solid #2a2a2a",
    borderRadius: 8,
    overflow: "hidden",
  },
  thumbWrap: {
    position: "relative",
    width: "100%",
    aspectRatio: "16 / 9",
    background: "#111",
    cursor: "pointer",
  },
  thumb: { width: "100%", height: "100%", objectFit: "contain" as const, display: "block" },
  durationBadge: {
    position: "absolute",
    right: 6,
    bottom: 6,
    padding: "2px 6px",
    background: "rgba(0,0,0,0.75)",
    borderRadius: 4,
    fontSize: 11,
    color: "#fff",
  },
  cardBody: { padding: "8px 10px", display: "flex", flexDirection: "column" as const, gap: 4 },
  cardTitle: {
    fontSize: 13,
    fontWeight: 600,
    color: "#fff",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap" as const,
  },
  cardMeta: { fontSize: 11, color: "#777", display: "flex", alignItems: "center", gap: 6 },
  categoryTag: {
    padding: "1px 6px",
    background: "#1e3a5f",
    color: "#93c5fd",
    borderRadius: 3,
    fontSize: 10,
  },
  cardActions: { display: "flex", gap: 6, padding: "0 10px 10px" },
  pickBtn: {
    flex: 1,
    padding: "6px 8px",
    background: "#2563eb",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 12,
    fontWeight: 600,
  },
  deleteBtn: {
    padding: "6px 10px",
    background: "transparent",
    color: "#f87171",
    border: "1px solid #5a2a2a",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 11,
  },
};
