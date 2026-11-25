/**
 * 공용 효과음 라이브러리 모달 — 메인 화면 "🔊 효과음 라이브러리" 버튼.
 *
 * 여기서 올린 효과음은 모든 프로젝트의 SFX 선택 칸 "프리셋" 탭 맨 위에 ⭐ 로 나온다.
 * 오버레이에는 `preset:user-xxxx` 로 저장되므로, 삭제하면 그 효과음을 쓰던 오버레이는
 * 소리 없이 지나간다(렌더 실패는 아님).
 */
import { useEffect, useRef, useState } from "react";
import {
  listSfxPresets,
  uploadUserSfx,
  renameUserSfx,
  deleteUserSfx,
  sfxPresetStreamUrl,
  type SfxPreset,
} from "../api/client";
import { invalidateSfxPresetCache } from "./SfxEditor";
import { ui } from "../store/ui";

const AUDIO_ACCEPT = "audio/*,.mp3,.wav,.m4a,.ogg,.aac,.flac";

export default function SfxLibraryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [items, setItems] = useState<SfxPreset[] | null>(null);
  // 기본 제공(앱 내장, assets/sfx_presets) 효과음 — 읽기 전용. 미리듣기로 골라 쓰기 위해 함께 보여준다.
  const [builtins, setBuiltins] = useState<SfxPreset[]>([]);
  const [tab, setTab] = useState<"user" | "builtin">("user");
  const [query, setQuery] = useState("");
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editLabel, setEditLabel] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  async function reload() {
    try {
      const all = await listSfxPresets();
      setItems(all.filter((p) => p.is_user));
      setBuiltins(all.filter((p) => !p.is_user));
    } catch (e) {
      ui.error(`효과음 목록 불러오기 실패: ${(e as Error).message}`);
      setItems([]);
      setBuiltins([]);
    }
  }

  useEffect(() => {
    if (open) reload();
  }, [open]);

  async function handleFiles(list: FileList | File[]) {
    const files = Array.from(list);
    if (files.length === 0) return;
    setUploading(true);
    try {
      const r = await uploadUserSfx(files);
      invalidateSfxPresetCache();
      if (r.added.length > 0) ui.success(`효과음 ${r.added.length}개를 올렸습니다.`);
      if (r.skipped.length > 0) ui.error(`건너뜀: ${r.skipped.join(", ")}`);
      await reload();
    } catch (e) {
      ui.error(`업로드 실패: ${(e as Error).message}`);
    } finally {
      setUploading(false);
    }
  }

  async function saveLabel(key: string) {
    try {
      await renameUserSfx(key, editLabel);
      invalidateSfxPresetCache();
      setEditingKey(null);
      await reload();
    } catch (e) {
      ui.error(`이름 변경 실패: ${(e as Error).message}`);
    }
  }

  async function handleDelete(p: SfxPreset) {
    const ok = await ui.confirm({
      title: "효과음 삭제",
      message: `"${p.preset_id}" 을(를) 라이브러리에서 지웁니다. 이 효과음을 쓰던 오버레이는 소리가 나지 않게 됩니다. 되돌릴 수 없습니다.`,
      variant: "danger",
      confirmLabel: "삭제",
    });
    if (!ok) return;
    try {
      await deleteUserSfx(p.preset_key);
      invalidateSfxPresetCache();
      await reload();
    } catch (e) {
      ui.error(`삭제 실패: ${(e as Error).message}`);
    }
  }

  function play(key: string) {
    const a = audioRef.current;
    if (!a) return;
    // 같은 항목을 다시 누르면 정지(긴 효과음 끊기).
    if (playingKey === key && !a.paused) {
      a.pause();
      setPlayingKey(null);
      return;
    }
    a.src = sfxPresetStreamUrl(key);
    a.currentTime = 0;
    setPlayingKey(key);
    a.play().catch(() => setPlayingKey(null));
  }

  const q = query.trim().toLowerCase();
  const filteredBuiltins = q
    ? builtins.filter((p) => p.preset_key.toLowerCase().includes(q) || (p.prompt || "").toLowerCase().includes(q))
    : builtins;

  if (!open) return null;

  return (
    <div style={st.backdrop} onClick={onClose}>
      <div style={st.modal} onClick={(e) => e.stopPropagation()}>
        <div style={st.header}>
          <div style={st.title}>🔊 효과음 라이브러리</div>
          <button style={st.closeBtn} onClick={onClose} title="닫기">×</button>
        </div>
        <div style={st.tabs}>
          <button style={{ ...st.tab, ...(tab === "user" ? st.tabActive : {}) }} onClick={() => setTab("user")}>
            ⭐ 내가 올린 효과음 {items ? `(${items.length})` : ""}
          </button>
          <button style={{ ...st.tab, ...(tab === "builtin" ? st.tabActive : {}) }} onClick={() => setTab("builtin")}>
            기본 효과음 ({builtins.length})
          </button>
        </div>

        {tab === "builtin" ? (
          <>
            <div style={st.subtitle}>
              기본 제공 효과음입니다. ▶ 로 들어보고 고르세요(다시 누르면 정지). 오버레이·씬의 SFX 칸 → "프리셋" 탭에서 같은 이름으로 선택할 수 있습니다.
            </div>
            <input
              style={{ ...st.input, width: "100%", boxSizing: "border-box", marginBottom: 10 }}
              placeholder="이름·설명으로 검색 (예: pop, sparkle, wind)"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            {filteredBuiltins.length === 0 ? (
              <div style={st.empty}>{builtins.length === 0 ? "기본 효과음이 없습니다." : "검색 결과가 없습니다."}</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {filteredBuiltins.map((p) => (
                  <div key={p.preset_key} style={st.row}>
                    <button
                      style={{ ...st.playBtn, ...(playingKey === p.preset_key ? st.playBtnOn : {}) }}
                      onClick={() => play(p.preset_key)}
                      title={playingKey === p.preset_key ? "정지" : "미리듣기"}
                    >
                      {playingKey === p.preset_key ? "■" : "▶"}
                    </button>
                    <span style={{ ...st.label, cursor: "default", flex: "0 0 auto", minWidth: 150 }}>{p.preset_key}</span>
                    <span style={st.prompt} title={p.prompt}>{p.prompt}</span>
                    <span style={st.dur}>{p.duration > 0 ? `${p.duration.toFixed(1)}s` : ""}</span>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
        <>
        <div style={st.subtitle}>
          여기 올린 효과음은 모든 프로젝트에서 쓸 수 있습니다. 오버레이·씬의 SFX 칸 → "프리셋" 탭 맨 위에 ⭐ 로 나옵니다.
        </div>

        <input
          ref={fileRef}
          type="file"
          multiple
          accept={AUDIO_ACCEPT}
          style={{ display: "none" }}
          onChange={(e) => {
            const fl = e.target.files;
            if (fl) handleFiles(Array.from(fl));
            e.target.value = "";
          }}
        />
        <div
          style={{ ...st.drop, ...(dragOver ? st.dropActive : {}) }}
          onClick={() => !uploading && fileRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragOver(false);
            if (!uploading) handleFiles(e.dataTransfer.files);
          }}
        >
          {uploading ? "업로드 중…" : "＋ 클릭하거나 파일을 끌어다 놓으세요 (mp3 · wav · m4a · ogg · aac · flac, 여러 개 가능)"}
        </div>

        {items === null ? (
          <div style={st.empty}>불러오는 중…</div>
        ) : items.length === 0 ? (
          <div style={st.empty}>아직 올린 효과음이 없습니다.</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {items.map((p) => (
              <div key={p.preset_key} style={st.row}>
                <button style={st.playBtn} onClick={() => play(p.preset_key)} title="미리듣기">▶</button>
                {editingKey === p.preset_key ? (
                  <>
                    <input
                      style={st.input}
                      value={editLabel}
                      autoFocus
                      onChange={(e) => setEditLabel(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") saveLabel(p.preset_key);
                        if (e.key === "Escape") setEditingKey(null);
                      }}
                    />
                    <button style={st.smallBtn} onClick={() => saveLabel(p.preset_key)}>저장</button>
                    <button style={st.smallBtn} onClick={() => setEditingKey(null)}>취소</button>
                  </>
                ) : (
                  <>
                    <span
                      style={st.label}
                      title="클릭하여 이름 변경"
                      onClick={() => { setEditingKey(p.preset_key); setEditLabel(p.preset_id); }}
                    >
                      {p.preset_id}
                    </span>
                    <span style={st.dur}>{p.duration > 0 ? `${p.duration.toFixed(1)}s` : ""}</span>
                    <button style={st.delBtn} onClick={() => handleDelete(p)} title="삭제">×</button>
                  </>
                )}
              </div>
            ))}
          </div>
        )}
        </>
        )}
        <audio ref={audioRef} style={{ display: "none" }} onEnded={() => setPlayingKey(null)} />
      </div>
    </div>
  );
}

const st: Record<string, React.CSSProperties> = {
  backdrop: {
    position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
  },
  modal: {
    background: "#1a1a1a", border: "1px solid #2a2a2a", borderRadius: 12,
    padding: "20px 24px", width: "min(640px, 92vw)", maxHeight: "80vh",
    overflowY: "auto", color: "#e4e4e4",
  },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 },
  title: { fontSize: 18, fontWeight: 600, color: "#fff" },
  closeBtn: {
    background: "transparent", border: "none", color: "#888", fontSize: 22, cursor: "pointer", lineHeight: 1,
  },
  subtitle: { color: "#888", fontSize: 12, lineHeight: 1.5, marginBottom: 14 },
  tabs: { display: "flex", gap: 6, marginBottom: 12, borderBottom: "1px solid #2a2a2a" },
  tab: {
    background: "transparent", color: "#888", border: "none", borderBottom: "2px solid transparent",
    padding: "6px 10px", cursor: "pointer", fontSize: 13,
  },
  tabActive: { color: "#e0e7ff", borderBottom: "2px solid #818cf8" },
  prompt: {
    flex: 1, color: "#777", fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  playBtnOn: { background: "#312e81", color: "#fff" },
  drop: {
    border: "1px dashed #4f46e5", borderRadius: 8, padding: "18px 12px", textAlign: "center",
    color: "#a5b4fc", fontSize: 13, cursor: "pointer", marginBottom: 14, background: "#141414",
  },
  dropActive: { background: "#1e1b4b", borderColor: "#818cf8" },
  empty: { color: "#666", fontSize: 13, padding: "8px 0" },
  row: {
    display: "flex", alignItems: "center", gap: 8, padding: "6px 8px",
    background: "#111", border: "1px solid #222", borderRadius: 6,
  },
  playBtn: {
    background: "#1f1f1f", color: "#a5b4fc", border: "1px solid #333", borderRadius: 4,
    padding: "2px 8px", cursor: "pointer", fontSize: 12,
  },
  label: { flex: 1, color: "#ddd", fontSize: 13, cursor: "text", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  dur: { color: "#777", fontSize: 12, minWidth: 40, textAlign: "right" },
  delBtn: {
    background: "transparent", color: "#888", border: "1px solid #333", borderRadius: 4,
    padding: "0 8px", cursor: "pointer", fontSize: 14,
  },
  input: {
    flex: 1, background: "#0f0f0f", color: "#eee", border: "1px solid #333", borderRadius: 4,
    padding: "4px 6px", fontSize: 13,
  },
  smallBtn: {
    background: "#1f1f1f", color: "#ccc", border: "1px solid #333", borderRadius: 4,
    padding: "2px 8px", cursor: "pointer", fontSize: 12,
  },
};
