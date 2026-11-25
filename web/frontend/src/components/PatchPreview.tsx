/**
 * PatchPreview — 자연어 편집 변경 미리보기 카드 (#316)
 *
 * NlEditorPanel 의 AI 응답 메시지 안에 박혀, 백엔드가 돌려준 JSON Patch 를
 * 김감독이 읽기 쉬운 한국어로 표시한다. "적용" / "거부" / "수정 요청" 버튼을 노출.
 *
 * 책임 분리
 * --------
 * - 본 컴포넌트는 *표시*와 *버튼 콜백 위임*만 한다.
 *   실제 apply/undo API 호출, 채팅 히스토리 갱신, 미리보기 새로고침은 부모(NlEditorPanel)가 담당.
 * - 변경 사항 한 줄 표현은 `jsonPatchToHuman.summarizePatch` 가 만든 라인을 그대로 표시.
 * - 영향 받는 씬 번호 칩은 *씬 카드 라벨과 동일한 scene_id* 로 표시 (#334).
 *   (사용자가 보는 씬 카드는 "씬 {scene_id}" 형식이므로, 정렬 1-based 순번을 표시하면
 *    삭제·재배치 후 비연속 scene_id 환경에서 어긋난다.)
 */
import {
  extractAffectedSceneIndices,
  safetyFlagToWarning,
  summarizePatch,
} from "../lib/jsonPatchToHuman";
import type { JsonPatchOp } from "../api/nlEdit";
import { useStore } from "../store";

export interface PatchPreviewProps {
  patch: JsonPatchOp[];
  summary?: string;
  safetyFlags?: string[];
  estimatedCostUsd?: number;
  /** 적용 완료 후 true. 버튼 비활성 + "되돌리기" 노출. */
  applied?: boolean;
  /** 사용자가 거부했거나 더 이상 적용 불가한 경우. */
  rejected?: boolean;
  /** 적용 → 되돌리기 후 true. UI 상에서 회색 처리. */
  undone?: boolean;
  onApply?: () => void;
  onReject?: () => void;
  onRequestRevise?: () => void;
  onUndo?: () => void;
  /** 적용 처리 중 여부 — 버튼 비활성. */
  busy?: boolean;
}

const KRW_RATE = 1380;

function fmtCostKrw(usd: number): string {
  const won = usd * KRW_RATE;
  if (won === 0) return "0원";
  if (won < 1) return "1원 미만";
  if (won < 10_000) return `${Math.round(won).toLocaleString()}원`;
  return `${Math.round(won / 100) / 10}천원`;
}

export default function PatchPreview({
  patch,
  summary,
  safetyFlags = [],
  estimatedCostUsd = 0,
  applied = false,
  rejected = false,
  undone = false,
  onApply,
  onReject,
  onRequestRevise,
  onUndo,
  busy = false,
}: PatchPreviewProps) {
  // #334 — patch 의 `/scenes/{idx}` 0-based 인덱스를 사용자 시각의 SceneCard.scene_id 로
  // 변환. RenderSpec scenes 배열은 SceneCard 와 *같은 순서*(scene_id 오름차순)로
  // 빌드되므로(`web/backend/routers/nl_editor.py:_load_render_spec_payload` 참고),
  // 정렬된 SceneCard 배열의 같은 위치 scene_id 를 사용한다.
  const project = useStore((s) => s.project);
  const sortedSceneIds = (project?.scenes ?? [])
    .slice()
    .sort((a, b) => a.scene_id - b.scene_id)
    .map((s) => s.scene_id);
  const lines = summarizePatch(patch, sortedSceneIds);
  const affected = extractAffectedSceneIndices(patch)
    .map((i) => sortedSceneIds[i])
    .filter((id): id is number => typeof id === "number");
  const hasSafety = safetyFlags.length > 0;
  const isEmptyPatch = patch.length === 0;

  // 상태 라벨 — 적용/거부/되돌리기 시 시각적 안내.
  let statusBadge: { text: string; color: string; bg: string } | null = null;
  if (rejected) statusBadge = { text: "거부됨", color: "#f87171", bg: "#1f0a0a" };
  else if (undone) statusBadge = { text: "되돌림", color: "#a3a3a3", bg: "#1a1a1a" };
  else if (applied) statusBadge = { text: "적용됨", color: "#4ade80", bg: "#0a1f0f" };

  return (
    <div style={{ ...styles.card, ...(undone || rejected ? styles.cardDimmed : {}) }}>
      {summary && (
        <div style={styles.summaryRow}>
          <span style={styles.aiIcon}>🤖</span>
          <span style={styles.summary}>{summary}</span>
          {statusBadge && (
            <span style={{ ...styles.statusBadge, color: statusBadge.color, background: statusBadge.bg }}>
              {statusBadge.text}
            </span>
          )}
        </div>
      )}

      {affected.length > 0 && (
        <div style={styles.chipRow}>
          <span style={styles.chipLabel}>영향 받는 씬</span>
          {affected.map((n) => (
            <span key={n} style={styles.sceneChip}>{n}번</span>
          ))}
        </div>
      )}

      {isEmptyPatch ? (
        <div style={styles.emptyHint}>
          변경할 항목을 찾지 못했어요. 명령을 조금 더 구체적으로 알려주세요.
        </div>
      ) : (
        <ul style={styles.changeList}>
          {lines.map((ln, i) => (
            <li key={i} style={styles.changeItem}>{ln}</li>
          ))}
        </ul>
      )}

      {hasSafety && !applied && !rejected && (
        <div style={styles.safetyBox}>
          <div style={styles.safetyTitle}>⚠️ 위험할 수 있는 변경</div>
          <ul style={styles.safetyList}>
            {safetyFlags.map((f) => (
              <li key={f} style={styles.safetyItem}>{safetyFlagToWarning(f)}</li>
            ))}
          </ul>
        </div>
      )}

      <div style={styles.metaRow}>
        {estimatedCostUsd > 0 && (
          <span style={styles.costBadge} title={`AI 호출 비용 추정: $${estimatedCostUsd.toFixed(6)}`}>
            예상 비용 약 {fmtCostKrw(estimatedCostUsd)}
          </span>
        )}
      </div>

      {/* 버튼 영역 — 상태 별로 분기 */}
      <div style={styles.actions}>
        {applied && !undone && onUndo && (
          <button
            type="button"
            onClick={onUndo}
            disabled={busy}
            style={{ ...styles.actionBtn, ...styles.undoBtn, ...(busy ? styles.btnDisabled : {}) }}
            title="이 변경을 적용 직전 상태로 되돌립니다."
          >
            ↩ 되돌리기
          </button>
        )}
        {!applied && !rejected && !undone && (
          <>
            <button
              type="button"
              onClick={onApply}
              disabled={busy || isEmptyPatch}
              style={{ ...styles.actionBtn, ...styles.applyBtn, ...(busy || isEmptyPatch ? styles.btnDisabled : {}) }}
              title={hasSafety ? "위험 경고 확인 후 적용됩니다." : "변경 사항을 영상에 적용합니다."}
            >
              {busy ? "적용 중…" : hasSafety ? "⚠ 확인 후 적용" : "✓ 적용"}
            </button>
            <button
              type="button"
              onClick={onReject}
              disabled={busy}
              style={{ ...styles.actionBtn, ...styles.rejectBtn, ...(busy ? styles.btnDisabled : {}) }}
              title="이 변경을 적용하지 않습니다."
            >
              ✕ 거부
            </button>
            <button
              type="button"
              onClick={onRequestRevise}
              disabled={busy}
              style={{ ...styles.actionBtn, ...styles.reviseBtn, ...(busy ? styles.btnDisabled : {}) }}
              title="입력창에 명령을 다시 적어 수정 요청합니다."
            >
              ✎ 수정 요청
            </button>
          </>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  card: {
    background: "#161623",
    border: "1px solid #2a2a4a",
    borderRadius: 10,
    padding: 12,
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  cardDimmed: {
    opacity: 0.6,
  },
  summaryRow: {
    display: "flex",
    alignItems: "flex-start",
    gap: 8,
  },
  aiIcon: { fontSize: 16, lineHeight: "20px" },
  summary: { color: "#e5e7eb", fontSize: 13, lineHeight: 1.5, flex: 1 },
  statusBadge: {
    fontSize: 10,
    fontWeight: 700,
    padding: "2px 8px",
    borderRadius: 4,
    border: "1px solid currentColor",
    alignSelf: "flex-start",
  },
  chipRow: {
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
    alignItems: "center",
  },
  chipLabel: { color: "#777", fontSize: 11 },
  sceneChip: {
    background: "#1e1b4b",
    color: "#a78bfa",
    border: "1px solid #4338ca",
    borderRadius: 4,
    padding: "1px 6px",
    fontSize: 11,
    fontWeight: 600,
  },
  changeList: {
    margin: 0,
    paddingLeft: 18,
    color: "#cbd5e1",
    fontSize: 12,
    lineHeight: 1.6,
  },
  changeItem: { marginBottom: 2 },
  emptyHint: {
    color: "#9ca3af",
    fontSize: 12,
    fontStyle: "italic",
    padding: "8px 0",
  },
  safetyBox: {
    background: "#2a1810",
    border: "1px solid #b45309",
    borderRadius: 8,
    padding: "8px 10px",
  },
  safetyTitle: { color: "#fbbf24", fontSize: 12, fontWeight: 700, marginBottom: 4 },
  safetyList: { margin: 0, paddingLeft: 18, color: "#fde68a", fontSize: 11, lineHeight: 1.5 },
  safetyItem: { marginBottom: 1 },
  metaRow: { display: "flex", gap: 8, alignItems: "center" },
  costBadge: {
    color: "#a78bfa",
    background: "#1a1a2e",
    border: "1px solid #2a2a4a",
    borderRadius: 4,
    padding: "2px 8px",
    fontSize: 11,
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
  },
  actions: {
    display: "flex",
    gap: 6,
    flexWrap: "wrap",
  },
  actionBtn: {
    padding: "6px 12px",
    border: "1px solid #333",
    borderRadius: 6,
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
    background: "#111",
    color: "#ccc",
  },
  applyBtn: {
    background: "#0f2318",
    color: "#22c55e",
    borderColor: "#166534",
  },
  rejectBtn: {
    background: "#1f0a0a",
    color: "#f87171",
    borderColor: "#7f1d1d",
  },
  reviseBtn: {
    background: "#1a1a2e",
    color: "#a78bfa",
    borderColor: "#4338ca",
  },
  undoBtn: {
    background: "#1c1500",
    color: "#fbbf24",
    borderColor: "#b45309",
  },
  btnDisabled: {
    opacity: 0.5,
    cursor: "not-allowed",
  },
};
