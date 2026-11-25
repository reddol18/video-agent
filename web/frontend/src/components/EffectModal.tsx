import { useState } from "react";
import type { EffectConfig, GridBorder, GridCell, SceneCard } from "../types";
import { updateScene as dbUpdateScene } from "../lib/project";
import { useStore } from "../store";
import { ui } from "../store/ui";

interface Props {
  scene: SceneCard;
  projectId: string;
  onClose: () => void;
}

const EFFECT_TYPES = [
  { id: "clip", label: "clip", desc: "영상 클립" },
  { id: "kenburns", label: "kenburns", desc: "이미지 Ken Burns 효과" },
  { id: "edit", label: "edit ⚡AI", desc: "AI가 장면 구간을 찾아 편집" },
  { id: "sync", label: "sync ⚡AI", desc: "특정 단어와 영상 싱크" },
  { id: "multiscreen", label: "multiscreen", desc: "여러 클립 동시 재생" },
  { id: "title_gen", label: "title_gen", desc: "텍스트 타이틀 생성" },
];

export default function EffectModal({ scene, projectId, onClose }: Props) {
  const { updateScene } = useStore();
  const [effectType, setEffectType] = useState<string>(scene.footage?.type ?? "clip");
  const [form, setForm] = useState<Record<string, any>>(scene.footage ?? {});
  const [saving, setSaving] = useState(false);

  // ── N×M 그리드 비교 상태 ────────────────────────────────────────────────
  const initFootage = (scene.footage as any) ?? {};
  const hasGrid = initFootage.type === "multiscreen" && initFootage.rows && initFootage.cols && Array.isArray(initFootage.cells);
  const [gridMode, setGridMode] = useState<boolean>(!!hasGrid);
  const [gridRows, setGridRows] = useState<number>(hasGrid ? initFootage.rows : 2);
  const [gridCols, setGridCols] = useState<number>(hasGrid ? initFootage.cols : 1);
  const [gridCells, setGridCells] = useState<GridCell[]>(
    hasGrid ? (initFootage.cells as GridCell[]) : []
  );

  function defaultBorder(): GridBorder {
    return { width_px: 16, color_from: "#00e5ff", color_to: "#a855f7", animate: true, speed: 1.0 };
  }
  function ensureCells(n: number): GridCell[] {
    const next = [...gridCells];
    while (next.length < n) next.push({ file: "", border: null });
    return next.slice(0, n);
  }
  function setCellFile(idx: number, file: string) {
    const next = ensureCells(gridRows * gridCols);
    next[idx] = { ...next[idx], file };
    setGridCells(next);
  }
  function setCellBorder(idx: number, border: GridBorder | null) {
    const next = ensureCells(gridRows * gridCols);
    next[idx] = { ...next[idx], border };
    setGridCells(next);
  }

  function setField(key: string, value: any) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function handleSave() {
    const effect = buildEffect();
    if (!effect) return;
    setSaving(true);
    try {
      updateScene(scene.scene_id, { footage: effect });
      await dbUpdateScene(projectId, scene.scene_id, { footage: effect });
      onClose();
    } catch (e: unknown) {
      ui.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  function buildEffect(): EffectConfig | null {
    // 기존 footage 객체의 부수 필드(x_offset, y_offset, fit, rotation 등)를 보존한다.
    // type만 바꿔도 사용자가 설정한 cover/contain·오프셋·회전 등이 날아가지 않도록.
    const existing: Record<string, any> = (scene.footage as any) ?? {};
    // 같은 타입이면 기존 값 위에 form을 덧씌우는 식, 타입이 바뀌면 공통 필드만 유지.
    const SHARED_KEYS = ["x_offset", "y_offset", "fit", "rotation", "footage_speed"];
    const sharedFromExisting: Record<string, any> = {};
    for (const k of SHARED_KEYS) {
      if (existing[k] !== undefined) sharedFromExisting[k] = existing[k];
    }
    // clip ↔ kenburns 전환 시 파일명도 자동 승계 (사용자가 비우지 않은 한).
    const inheritedName: string = (existing.file ?? existing.image ?? "") as string;

    switch (effectType) {
      case "clip":
        return { ...sharedFromExisting, type: "clip", file: form.file ?? existing.file ?? inheritedName ?? "" } as EffectConfig;
      case "kenburns":
        return { ...sharedFromExisting, type: "kenburns", image: form.image ?? existing.image ?? inheritedName ?? "" } as EffectConfig;
      case "edit":
        return { ...sharedFromExisting, type: "edit", description: form.description ?? existing.description ?? "", file: form.file ?? existing.file ?? inheritedName ?? "", min_duration: Number(form.min_duration ?? existing.min_duration ?? 5) } as EffectConfig;
      case "sync":
        return { type: "sync", word: form.word ?? existing.word ?? "", inner: form.inner ?? existing.inner ?? null };
      case "multiscreen": {
        // 그리드 모드면 rows/cols/cells 포함, 아니면 기존 files 기반(회귀 보존).
        if (gridMode) {
          return {
            type: "multiscreen",
            files: [],
            rows: gridRows,
            cols: gridCols,
            cells: gridCells.slice(0, gridRows * gridCols),
          };
        }
        return { type: "multiscreen", files: (form.files_str ?? (Array.isArray(existing.files) ? existing.files.join("\n") : "")).split("\n").filter(Boolean) };
      }
      case "title_gen":
        return { type: "title_gen", options: { text: form.text ?? existing?.options?.text ?? "", mode: form.mode ?? existing?.options?.mode ?? "shimmer", duration: Number(form.duration ?? existing?.options?.duration ?? 3), font_size: Number(form.font_size ?? existing?.options?.font_size ?? 80), text_color: form.text_color ?? existing?.options?.text_color ?? "white", bg_color: form.bg_color ?? existing?.options?.bg_color ?? "black" } };
      default: return null;
    }
  }

  return (
    <div style={styles.overlay} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div style={styles.modal}>
        <div style={styles.header}>
          <h3 style={{ margin: 0, color: "#fff" }}>씬 {scene.scene_id} · 이펙트 설정</h3>
          <button style={styles.closeBtn} onClick={onClose}>✕</button>
        </div>

        <div style={styles.body}>
          <div style={styles.label}>이펙트 타입</div>
          <div style={styles.typeGrid}>
            {EFFECT_TYPES.map((t) => (
              <div
                key={t.id}
                style={{ ...styles.typeCard, ...(effectType === t.id ? styles.typeCardSelected : {}) }}
                onClick={() => setEffectType(t.id)}
              >
                <div style={styles.typeLabel}>{t.label}</div>
                <div style={styles.typeDesc}>{t.desc}</div>
              </div>
            ))}
          </div>

          {(effectType === "edit" || effectType === "sync") && (
            <div style={styles.aiWarning}>
              ⚡ 이 이펙트는 최종 렌더링 시 AI가 처리합니다. 미리보기에서는 플레이스홀더로 표시됩니다.
            </div>
          )}

          <div style={styles.formSection}>
            {effectType === "clip" && <FormField label="파일명" value={form.file ?? ""} onChange={(v) => setField("file", v)} placeholder="scene.mp4" />}
            {effectType === "kenburns" && <FormField label="이미지 파일명" value={form.image ?? ""} onChange={(v) => setField("image", v)} placeholder="image.png" />}
            {effectType === "edit" && (
              <>
                <FormField label="파일명" value={form.file ?? ""} onChange={(v) => setField("file", v)} placeholder="footage.mp4" />
                <FormField label="장면 설명 (AI 검색용)" value={form.description ?? ""} onChange={(v) => setField("description", v)} placeholder="고양이가 장난감을 치는 장면" />
                <FormField label="최소 길이 (초)" value={String(form.min_duration ?? 5)} onChange={(v) => setField("min_duration", v)} placeholder="5" />
              </>
            )}
            {effectType === "sync" && (
              <>
                <FormField label="동기화 기준 단어" value={form.word ?? ""} onChange={(v) => setField("word", v)} placeholder="싸다구" />
                <div style={styles.label}>선행 이펙트 (edit 또는 clip)</div>
                <ChainEditor value={form.inner} onChange={(v) => setField("inner", v)} />
              </>
            )}
            {effectType === "multiscreen" && (
              <>
                <div style={styles.row}>
                  <label style={{ ...styles.label, display: "flex", alignItems: "center", gap: 6 }}>
                    <input type="checkbox" checked={gridMode} onChange={(e) => setGridMode(e.target.checked)} />
                    N×M 그리드 비교 (셀별 광원 보더)
                  </label>
                </div>
                {!gridMode && (
                  <FormField label="파일명 목록 (줄바꿈으로 구분)" value={form.files_str ?? ""} onChange={(v) => setField("files_str", v)} placeholder={"a.mp4\nb.mp4"} textarea />
                )}
                {gridMode && (
                  <GridEditor
                    rows={gridRows}
                    cols={gridCols}
                    cells={ensureCells(gridRows * gridCols)}
                    onRows={(v) => { setGridRows(v); setGridCells(ensureCellsFor(gridCells, v * gridCols)); }}
                    onCols={(v) => { setGridCols(v); setGridCells(ensureCellsFor(gridCells, gridRows * v)); }}
                    onCellFile={setCellFile}
                    onCellBorder={setCellBorder}
                    defaultBorder={defaultBorder}
                  />
                )}
              </>
            )}
            {effectType === "title_gen" && (
              <>
                <FormField label="텍스트" value={form.text ?? ""} onChange={(v) => setField("text", v)} placeholder="타이틀 텍스트" />
                <FormField label="모드" value={form.mode ?? "shimmer"} onChange={(v) => setField("mode", v)} placeholder="shimmer" />
                <FormField label="지속 시간 (초)" value={String(form.duration ?? 3)} onChange={(v) => setField("duration", v)} placeholder="3" />
                <div style={styles.row}>
                  <FormField label="폰트 크기" value={String(form.font_size ?? 80)} onChange={(v) => setField("font_size", v)} placeholder="80" />
                  <FormField label="글자 색" value={form.text_color ?? "white"} onChange={(v) => setField("text_color", v)} placeholder="white" />
                  <FormField label="배경 색" value={form.bg_color ?? "black"} onChange={(v) => setField("bg_color", v)} placeholder="black" />
                </div>
              </>
            )}
          </div>
        </div>

        <div style={styles.footer}>
          <button style={styles.cancelBtn} onClick={onClose}>취소</button>
          <button style={styles.saveBtn} onClick={handleSave} disabled={saving}>
            {saving ? "저장 중..." : "저장"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FormField({ label, value, onChange, placeholder, textarea }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; textarea?: boolean;
}) {
  const inputStyle = { ...formStyles.input, ...(textarea ? { height: 80, resize: "vertical" as const } : {}) };
  return (
    <div style={formStyles.field}>
      <label style={formStyles.label}>{label}</label>
      {textarea
        ? <textarea style={inputStyle} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
        : <input style={inputStyle} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
      }
    </div>
  );
}

function ChainEditor({ value, onChange }: { value: any; onChange: (v: any) => void }) {
  const type = value?.type ?? "none";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <select
        style={formStyles.input}
        value={type}
        onChange={(e) => {
          const t = e.target.value;
          if (t === "none") onChange(null);
          else if (t === "clip") onChange({ type: "clip", file: "" });
          else if (t === "edit") onChange({ type: "edit", description: "", file: "", min_duration: 5 });
        }}
      >
        <option value="none">선행 이펙트 없음</option>
        <option value="clip">clip</option>
        <option value="edit">edit (AI)</option>
      </select>
      {type === "clip" && (
        <FormField label="파일명" value={value?.file ?? ""} onChange={(v) => onChange({ ...value, file: v })} placeholder="footage.mp4" />
      )}
      {type === "edit" && (
        <>
          <FormField label="파일명" value={value?.file ?? ""} onChange={(v) => onChange({ ...value, file: v })} placeholder="footage.mp4" />
          <FormField label="장면 설명" value={value?.description ?? ""} onChange={(v) => onChange({ ...value, description: v })} />
        </>
      )}
    </div>
  );
}

// 셀 배열을 목표 길이로 맞춘다(부족분 빈 셀 추가, 초과분 절단). 순수 함수.
function ensureCellsFor(cells: GridCell[], n: number): GridCell[] {
  const next = [...cells];
  while (next.length < n) next.push({ file: "", border: null });
  return next.slice(0, n);
}

function GridEditor({
  rows, cols, cells, onRows, onCols, onCellFile, onCellBorder, defaultBorder,
}: {
  rows: number; cols: number; cells: GridCell[];
  onRows: (v: number) => void; onCols: (v: number) => void;
  onCellFile: (idx: number, file: string) => void;
  onCellBorder: (idx: number, border: GridBorder | null) => void;
  defaultBorder: () => GridBorder;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 12 }}>
        <div style={formStyles.field}>
          <label style={formStyles.label}>행 (rows)</label>
          <select style={formStyles.input} value={rows} onChange={(e) => onRows(Number(e.target.value))}>
            {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
        <div style={formStyles.field}>
          <label style={formStyles.label}>열 (cols)</label>
          <select style={formStyles.input} value={cols} onChange={(e) => onCols(Number(e.target.value))}>
            {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: `repeat(${Math.min(cols, 4)}, 1fr)`, gap: 8 }}>
        {cells.map((cell, idx) => (
          <CellEditor
            key={idx}
            index={idx}
            cell={cell}
            onFile={(f) => onCellFile(idx, f)}
            onBorder={(b) => onCellBorder(idx, b)}
            defaultBorder={defaultBorder}
          />
        ))}
      </div>
    </div>
  );
}

function CellEditor({
  index, cell, onFile, onBorder, defaultBorder,
}: {
  index: number; cell: GridCell;
  onFile: (f: string) => void; onBorder: (b: GridBorder | null) => void;
  defaultBorder: () => GridBorder;
}) {
  const b = cell.border ?? null;
  return (
    <div style={{ border: "1px solid #333", borderRadius: 8, padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ color: "#aaa", fontSize: 11, fontWeight: 600 }}>셀 {index + 1}</div>
      <input
        style={formStyles.input}
        value={cell.file}
        onChange={(e) => onFile(e.target.value)}
        placeholder="파일명 (빈칸=배경)"
      />
      <label style={{ ...formStyles.label, display: "flex", alignItems: "center", gap: 6 }}>
        <input
          type="checkbox"
          checked={!!b}
          onChange={(e) => onBorder(e.target.checked ? defaultBorder() : null)}
        />
        광원 보더
      </label>
      {b && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ color: "#888", fontSize: 11, width: 44 }}>두께</span>
            <input
              type="range" min={2} max={60} value={b.width_px}
              onChange={(e) => onBorder({ ...b, width_px: Number(e.target.value) })}
              style={{ flex: 1 }}
            />
            <span style={{ color: "#ccc", fontSize: 11, width: 32, textAlign: "right" }}>{b.width_px}px</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ color: "#888", fontSize: 11 }}>색</span>
            <input type="color" value={b.color_from} onChange={(e) => onBorder({ ...b, color_from: e.target.value })} />
            <span style={{ color: "#888", fontSize: 11 }}>→</span>
            <input type="color" value={b.color_to} onChange={(e) => onBorder({ ...b, color_to: e.target.value })} />
          </div>
          <label style={{ color: "#888", fontSize: 11, display: "flex", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={b.animate} onChange={(e) => onBorder({ ...b, animate: e.target.checked })} />
            광원 애니메이션
          </label>
        </div>
      )}
    </div>
  );
}

const formStyles: Record<string, React.CSSProperties> = {
  field: { display: "flex", flexDirection: "column", gap: 4 },
  label: { color: "#888", fontSize: 12 },
  input: { padding: "8px 12px", background: "#111", border: "1px solid #333", borderRadius: 6, color: "#fff", fontSize: 14, width: "100%", boxSizing: "border-box" },
};

const styles: Record<string, React.CSSProperties> = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 },
  modal: { background: "#1a1a1a", borderRadius: 12, width: 580, maxHeight: "85vh", display: "flex", flexDirection: "column", overflow: "hidden" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "20px 24px", borderBottom: "1px solid #333" },
  closeBtn: { background: "none", border: "none", color: "#888", fontSize: 20, cursor: "pointer" },
  body: { padding: 24, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 16 },
  footer: { padding: "16px 24px", borderTop: "1px solid #333", display: "flex", justifyContent: "flex-end", gap: 10 },
  label: { color: "#888", fontSize: 12, fontWeight: 600 },
  typeGrid: { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 },
  typeCard: { padding: "10px 12px", border: "2px solid #333", borderRadius: 8, cursor: "pointer" },
  typeCardSelected: { borderColor: "#6366f1", background: "#1e1b4b" },
  typeLabel: { color: "#fff", fontSize: 13, fontWeight: 600 },
  typeDesc: { color: "#888", fontSize: 11, marginTop: 2 },
  aiWarning: { background: "#1c1a00", border: "1px solid #444", color: "#f59e0b", padding: "10px 14px", borderRadius: 8, fontSize: 13 },
  formSection: { display: "flex", flexDirection: "column", gap: 12 },
  row: { display: "flex", gap: 12 },
  cancelBtn: { padding: "10px 20px", background: "#333", color: "#aaa", border: "none", borderRadius: 8, cursor: "pointer" },
  saveBtn: { padding: "10px 20px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, cursor: "pointer" },
};
