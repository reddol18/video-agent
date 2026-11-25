import { useEffect, useState } from "react";
import type { FootageConflict, FootageConflictPolicy } from "../api/client";

interface Props {
  open: boolean;
  conflicts: FootageConflict[];
  /** 충돌 없이 그대로 들어가는 파일 수 — "나머지 N개는 그대로 가져옵니다" 안내용. */
  cleanCount: number;
  onCancel: () => void;
  /** 파일명 → 선택한 정책. 충돌 파일 전부에 대한 결정이 담긴다. */
  onResolve: (decisions: Record<string, FootageConflictPolicy>) => void;
}

const POLICIES: FootageConflictPolicy[] = ["rename", "overwrite", "skip"];
const POLICY_LABEL: Record<FootageConflictPolicy, string> = {
  rename: "둘 다 유지",
  overwrite: "덮어쓰기",
  skip: "건너뛰기",
};

/**
 * 푸티지 가져오기 파일명 충돌 해결 다이얼로그.
 *
 * 같은 이름의 파일이 이미 프로젝트에 있을 때, 예전처럼 조용히 건너뛰지 않고
 * 사용자에게 묻는다. 충돌하지 않는 파일은 여기 나타나지 않고 그대로 진행된다.
 *
 * 선택지
 *   - 둘 다 유지 — `이름 (2).mp4` 로 저장. 기존 씬은 옛 영상을 계속 참조한다(안전).
 *   - 덮어쓰기   — 기존 파일을 교체. 그 파일을 쓰는 씬이 **전부 새 영상으로 바뀌므로**
 *                 참조 씬 수를 경고로 표시한다.
 *   - 건너뛰기   — 가져오지 않음.
 *
 * "전체에 일괄 적용" 을 켜면 선택이 모든 충돌 파일에 복사된다.
 */
export default function FootageConflictDialog({
  open, conflicts, cleanCount, onCancel, onResolve,
}: Props) {
  const [decisions, setDecisions] = useState<Record<string, FootageConflictPolicy>>({});
  const [applyAll, setApplyAll] = useState(false);

  // 열릴 때마다 초기화 — 기본은 가장 안전한 "둘 다 유지"(데이터 손실 없음).
  useEffect(() => {
    if (!open) return;
    const init: Record<string, FootageConflictPolicy> = {};
    for (const c of conflicts) init[c.filename] = "rename";
    setDecisions(init);
    setApplyAll(false);
  }, [open, conflicts]);

  if (!open || conflicts.length === 0) return null;

  const pick = (filename: string, policy: FootageConflictPolicy) => {
    if (applyAll) {
      // 일괄 적용 — 모든 충돌 파일에 같은 정책.
      const next: Record<string, FootageConflictPolicy> = {};
      for (const c of conflicts) next[c.filename] = policy;
      setDecisions(next);
      return;
    }
    setDecisions((prev) => ({ ...prev, [filename]: policy }));
  };

  const toggleApplyAll = (checked: boolean) => {
    setApplyAll(checked);
    if (checked && conflicts.length > 0) {
      // 켜는 즉시 첫 항목의 현재 선택으로 전체를 맞춘다.
      const first = decisions[conflicts[0].filename] ?? "rename";
      const next: Record<string, FootageConflictPolicy> = {};
      for (const c of conflicts) next[c.filename] = first;
      setDecisions(next);
    }
  };

  const overwriteRefs = conflicts
    .filter((c) => decisions[c.filename] === "overwrite")
    .reduce((sum, c) => sum + c.scene_refs, 0);

  return (
    <div style={styles.overlay} onClick={onCancel}>
      <div
        style={styles.modal}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="파일명 충돌 해결"
      >
        <div style={styles.header}>
          <strong>같은 이름의 파일이 이미 있습니다</strong>
          <button type="button" style={styles.closeBtn} onClick={onCancel}>✕</button>
        </div>

        <div style={styles.body}>
          <div style={styles.help}>
            {conflicts.length}개 파일의 이름이 겹칩니다. 어떻게 할지 선택해 주세요.
            {cleanCount > 0 && ` 나머지 ${cleanCount}개는 그대로 가져옵니다.`}
          </div>

          <label style={styles.applyAll}>
            <input
              type="checkbox"
              checked={applyAll}
              onChange={(e) => toggleApplyAll(e.target.checked)}
            />
            <span>전체에 일괄 적용</span>
          </label>

          <div style={styles.list}>
            {conflicts.map((c) => {
              const current = decisions[c.filename] ?? "rename";
              return (
                <div key={c.filename} style={styles.row}>
                  <div style={styles.filename}>{c.filename}</div>
                  <div style={styles.choices}>
                    {POLICIES.map((pol) => (
                      <button
                        key={pol}
                        type="button"
                        onClick={() => pick(c.filename, pol)}
                        style={current === pol ? styles.choiceOn : styles.choiceOff}
                      >
                        {POLICY_LABEL[pol]}
                      </button>
                    ))}
                  </div>
                  {current === "rename" && (
                    <div style={styles.note}>→ {c.suggested} 로 저장됩니다.</div>
                  )}
                  {current === "overwrite" && c.scene_refs > 0 && (
                    <div style={styles.warn}>
                      ⚠ 이 파일을 쓰는 씬 {c.scene_refs}개의 영상이 함께 바뀝니다.
                    </div>
                  )}
                  {current === "overwrite" && c.scene_refs === 0 && (
                    <div style={styles.note}>참조 중인 씬이 없어 안전하게 교체됩니다.</div>
                  )}
                </div>
              );
            })}
          </div>

          {overwriteRefs > 0 && (
            <div style={styles.warn}>
              총 {overwriteRefs}개 씬의 영상이 교체됩니다. 되돌릴 수 없습니다.
            </div>
          )}
        </div>

        <div style={styles.footer}>
          <button type="button" style={styles.cancelBtn} onClick={onCancel}>취소</button>
          <button type="button" style={styles.applyBtn} onClick={() => onResolve(decisions)}>
            가져오기
          </button>
        </div>
      </div>
    </div>
  );
}

const choiceBase: React.CSSProperties = {
  padding: "5px 12px", fontSize: 12, borderRadius: 6, cursor: "pointer",
};

const styles: Record<string, React.CSSProperties> = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 },
  modal: { background: "#1a1a1a", borderRadius: 12, width: 620, maxWidth: "95vw", maxHeight: "90vh", display: "flex", flexDirection: "column", overflow: "hidden" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "16px 20px", borderBottom: "1px solid #333", color: "#eee" },
  closeBtn: { background: "none", border: "none", color: "#888", fontSize: 18, cursor: "pointer" },
  body: { padding: 20, display: "flex", flexDirection: "column", gap: 14, overflowY: "auto" },
  help: { color: "#aaa", fontSize: 13 },
  applyAll: { display: "flex", alignItems: "center", gap: 8, color: "#ccc", fontSize: 13, cursor: "pointer" },
  list: { display: "flex", flexDirection: "column", gap: 10 },
  row: { border: "1px solid #333", borderRadius: 8, padding: "10px 12px", background: "#0f0f0f" },
  filename: { fontSize: 13, fontWeight: 600, color: "#eee", wordBreak: "break-all" },
  choices: { display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" },
  choiceOn: { ...choiceBase, background: "#6366f1", color: "#fff", border: "1px solid #6366f1" },
  choiceOff: { ...choiceBase, background: "transparent", color: "#bbb", border: "1px solid #444" },
  note: { fontSize: 12, color: "#888", marginTop: 6, wordBreak: "break-all" },
  warn: { fontSize: 12, color: "#fbbf24", marginTop: 6 },
  footer: { padding: "14px 20px", borderTop: "1px solid #333", display: "flex", justifyContent: "flex-end", gap: 10 },
  cancelBtn: { padding: "10px 18px", background: "#333", color: "#aaa", border: "none", borderRadius: 8, cursor: "pointer" },
  applyBtn: { padding: "10px 18px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, cursor: "pointer" },
};
