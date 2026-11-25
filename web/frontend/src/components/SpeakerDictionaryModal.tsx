/**
 * SpeakerDictionaryModal — 다중 화자 사전 편집
 *
 * 화자명 ↔ TTS 목소리를 등록한다. 씬 태그 `speaker:{name}` 이 여기 등록된 이름을
 * 가리키면 그 씬의 TTS 가 해당 목소리로 생성된다.
 *
 * 저장은 전체 교체(PUT). 서버가 이름 중복을 거부하므로 그 메시지를 그대로 보여 준다.
 */
import { useEffect, useState } from "react";
import type { Speaker, SpeakersResponse } from "../types";
import { listSpeakers, putSpeakers } from "../api/client";

interface Props {
  projectId: string;
  onClose: () => void;
  /** 저장 성공 시 최신 목록을 상위로 — 씬 카드 선택지를 갱신한다. */
  onSaved: (speakers: Speaker[]) => void;
}

type Row = Speaker & { scene_ids?: number[] };

export default function SpeakerDictionaryModal({ projectId, onClose, onSaved }: Props) {
  const [rows, setRows] = useState<Row[]>([]);
  const [info, setInfo] = useState<SpeakersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    listSpeakers(projectId)
      .then((d) => {
        if (!alive) return;
        setInfo(d);
        setRows(d.speakers);
      })
      .catch((e) => alive && setError((e as Error).message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [projectId]);

  function patch(i: number, next: Partial<Speaker>) {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...next } : r)));
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const cleaned = rows.map((r) => ({
        name: r.name.trim(),
        voice_name: r.voice_name.trim(),
        provider: r.provider ?? null,
        note: r.note ?? "",
      }));
      const saved = await putSpeakers(projectId, cleaned);
      onSaved(saved);
      onClose();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || "저장에 실패했습니다");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={styles.backdrop} onClick={onClose}>
      <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div style={styles.header}>
          <span style={styles.title}>🎙 화자 사전</span>
          <button style={styles.close} onClick={onClose}>
            ✕
          </button>
        </div>

        <div style={styles.hint}>
          씬 태그에 <code style={styles.code}>speaker:이름</code> 을 달면 그 씬의 TTS 가 해당 목소리로
          생성됩니다. 씬 카드의 “화자” 선택으로도 같은 태그가 붙습니다.
        </div>

        {loading ? (
          <div style={styles.dim}>불러오는 중…</div>
        ) : (
          <>
            <div style={styles.list}>
              {rows.length === 0 && <div style={styles.dim}>등록된 화자가 없습니다.</div>}
              {rows.map((r, i) => (
                <div key={i} style={styles.row}>
                  <input
                    style={{ ...styles.input, flex: "0 0 110px" }}
                    value={r.name}
                    onChange={(e) => patch(i, { name: e.target.value })}
                    placeholder="화자명"
                  />
                  <input
                    style={{ ...styles.input, flex: 1, fontFamily: "monospace", fontSize: 11 }}
                    value={r.voice_name}
                    onChange={(e) => patch(i, { voice_name: e.target.value })}
                    placeholder="목소리 id (예: ko-KR-Chirp3-HD-Orus)"
                  />
                  <input
                    style={{ ...styles.input, flex: "0 0 110px" }}
                    value={r.note ?? ""}
                    onChange={(e) => patch(i, { note: e.target.value })}
                    placeholder="메모"
                  />
                  {/* 사용 중인 씬이 있으면 지울 때 영향 범위를 알 수 있게 표시 */}
                  <span style={styles.usage} title="이 화자가 지정된 씬">
                    {r.scene_ids && r.scene_ids.length > 0 ? `씬 ${r.scene_ids.join(", ")}` : "미사용"}
                  </span>
                  <button
                    style={styles.del}
                    title="삭제 (씬 태그는 그대로 남아 미등록 상태가 됩니다)"
                    onClick={() => setRows((prev) => prev.filter((_, idx) => idx !== i))}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>

            <button
              style={styles.add}
              onClick={() =>
                setRows((prev) => [...prev, { name: "", voice_name: info?.default_voice || "", note: "" }])
              }
            >
              + 화자 추가
            </button>

            {/* 태그에는 있는데 사전에 없는 이름 — 오타 잡기용 */}
            {info && info.unknown_speakers.length > 0 && (
              <div style={styles.unknown}>
                <b>미등록 화자 태그</b> — 기본 목소리로 생성됩니다.
                {info.unknown_speakers.map((u) => (
                  <div key={u.name} style={styles.unknownRow}>
                    <span style={styles.unknownName}>{u.name}</span>
                    <span style={styles.dim}>씬 {u.scene_ids.join(", ")}</span>
                    <button
                      style={styles.addSmall}
                      onClick={() =>
                        setRows((prev) => [
                          ...prev,
                          { name: u.name, voice_name: info.default_voice || "", note: "" },
                        ])
                      }
                    >
                      + 등록
                    </button>
                  </div>
                ))}
              </div>
            )}

            {error && <div style={styles.error}>{error}</div>}

            <div style={styles.footer}>
              <span style={styles.dim}>
                목소리를 바꾸면 해당 씬의 TTS 를 다시 만들어야 반영됩니다.
              </span>
              <button style={styles.ghost} onClick={onClose} disabled={busy}>
                취소
              </button>
              <button style={styles.primary} onClick={save} disabled={busy}>
                {busy ? "저장 중…" : "저장"}
              </button>
            </div>
          </>
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
    background: "#0f0f14",
    border: "1px solid #2a2a3a",
    borderRadius: 10,
    padding: 16,
    width: "min(760px, 92vw)",
    maxHeight: "82vh",
    overflowY: "auto",
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between" },
  title: { color: "#e2e8f0", fontSize: 15, fontWeight: 700 },
  close: { background: "transparent", border: "none", color: "#64748b", fontSize: 16, cursor: "pointer" },
  hint: { color: "#94a3b8", fontSize: 11, lineHeight: 1.6 },
  code: { background: "#1e293b", padding: "1px 4px", borderRadius: 3, color: "#a78bfa" },
  list: { display: "flex", flexDirection: "column", gap: 6 },
  row: { display: "flex", alignItems: "center", gap: 6 },
  input: {
    background: "#08080c",
    border: "1px solid #2a2a3a",
    borderRadius: 5,
    color: "#e2e8f0",
    fontSize: 12,
    padding: "5px 7px",
    minWidth: 0,
  },
  usage: { color: "#64748b", fontSize: 10, flex: "0 0 84px", textAlign: "right" },
  del: { background: "transparent", border: "none", color: "#f87171", cursor: "pointer", fontSize: 12 },
  add: {
    background: "#1e293b",
    border: "1px solid #334155",
    color: "#93c5fd",
    borderRadius: 6,
    padding: "5px 10px",
    fontSize: 12,
    cursor: "pointer",
    alignSelf: "flex-start",
  },
  addSmall: {
    background: "#1e293b",
    border: "1px solid #334155",
    color: "#93c5fd",
    borderRadius: 4,
    padding: "1px 6px",
    fontSize: 10,
    cursor: "pointer",
  },
  unknown: {
    border: "1px solid #78350f",
    background: "#1c1408",
    borderRadius: 6,
    padding: "6px 9px",
    color: "#fbbf24",
    fontSize: 11,
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  unknownRow: { display: "flex", alignItems: "center", gap: 8 },
  unknownName: { color: "#fde68a", fontWeight: 700 },
  dim: { color: "#64748b", fontSize: 11 },
  error: { color: "#f87171", fontSize: 11 },
  footer: { display: "flex", alignItems: "center", gap: 8, marginTop: 2 },
  ghost: {
    background: "transparent",
    border: "1px solid #2a2a3a",
    color: "#94a3b8",
    borderRadius: 5,
    padding: "5px 12px",
    fontSize: 12,
    cursor: "pointer",
    marginLeft: "auto",
  },
  primary: {
    background: "#1d4ed8",
    border: "none",
    color: "#fff",
    borderRadius: 5,
    padding: "5px 14px",
    fontSize: 12,
    fontWeight: 700,
    cursor: "pointer",
  },
};
