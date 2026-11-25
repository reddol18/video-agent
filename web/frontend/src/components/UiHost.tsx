/**
 * 공용 Toast / Modal 렌더러 (#296).
 *
 * App 루트에 한 번 렌더하면 useUiStore 의 toasts/dialog 상태를 화면에 표시한다.
 * - ToastStack: 우측 상단, 종류별 색/아이콘, 클릭 또는 자동 소멸.
 * - ConfirmModal: variant(danger/warning/info)별 색, requireType 타이핑 확인,
 *                 "다시 묻지 않기" 체크박스.
 * - PromptModal: 텍스트 입력.
 */
import { useEffect, useRef, useState } from "react";
import { useUiStore, ui } from "../store/ui";
import type { ConfirmVariant, ToastKind } from "../store/ui";

const TOAST_STYLE: Record<ToastKind, { bg: string; border: string; color: string; icon: string }> = {
  success: { bg: "#0a1f0f", border: "#166534", color: "#4ade80", icon: "✓" },
  error: { bg: "#1f0a0a", border: "#7f1d1d", color: "#f87171", icon: "✕" },
  info: { bg: "#0f1a2e", border: "#1e3a5f", color: "#93c5fd", icon: "ℹ" },
};

const VARIANT_STYLE: Record<ConfirmVariant, { accent: string; bg: string; icon: string }> = {
  danger: { accent: "#f87171", bg: "#1f0a0a", icon: "⚠" },
  warning: { accent: "#fbbf24", bg: "#1c1200", icon: "⚠" },
  info: { accent: "#93c5fd", bg: "#0f1a2e", icon: "?" },
};

export default function UiHost() {
  return (
    <>
      <style>{ANIM_CSS}</style>
      <ToastStack />
      <DialogHost />
    </>
  );
}

const ANIM_CSS = `
@keyframes ui-toast-in { from { opacity: 0; transform: translateX(20px); } to { opacity: 1; transform: translateX(0); } }
@keyframes ui-modal-in { from { opacity: 0; transform: translateY(-8px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
`;

function ToastStack() {
  const toasts = useUiStore((s) => s.toasts);
  const dismiss = useUiStore((s) => s.dismissToast);
  if (toasts.length === 0) return null;
  return (
    <div style={styles.toastStack}>
      {toasts.map((t) => {
        const st = TOAST_STYLE[t.kind];
        return (
          <div
            key={t.id}
            style={{ ...styles.toast, background: st.bg, borderColor: st.border, color: st.color }}
            onClick={() => dismiss(t.id)}
            role="status"
          >
            <span style={{ ...styles.toastIcon, color: st.color }}>{st.icon}</span>
            <span style={styles.toastMsg}>{t.message}</span>
            <span style={styles.toastClose}>×</span>
          </div>
        );
      })}
    </div>
  );
}

function DialogHost() {
  const dialog = useUiStore((s) => s.dialog);
  const resolveDialog = useUiStore((s) => s.resolveDialog);
  if (!dialog) return null;
  if (dialog.type === "confirm") {
    return <ConfirmModal key={dialog.id} dialog={dialog} onResolve={resolveDialog} />;
  }
  return <PromptModal key={dialog.id} dialog={dialog} onResolve={resolveDialog} />;
}

function ConfirmModal({
  dialog,
  onResolve,
}: {
  dialog: Extract<NonNullable<ReturnType<typeof useUiStore.getState>["dialog"]>, { type: "confirm" }>;
  onResolve: (r: boolean) => void;
}) {
  const variant: ConfirmVariant = dialog.variant ?? "info";
  const st = VARIANT_STYLE[variant];
  const [typed, setTyped] = useState("");
  const [dontAsk, setDontAsk] = useState(false);
  const typeOk = !dialog.requireType || typed.trim() === dialog.requireType.trim();

  function confirm() {
    if (!typeOk) return;
    if (dialog.dontAskAgainKey && dontAsk) ui.markDontAskAgain(dialog.dontAskAgainKey);
    onResolve(true);
  }
  function cancel() {
    onResolve(false);
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") cancel();
      else if (e.key === "Enter" && !dialog.requireType) confirm();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typeOk, dontAsk]);

  return (
    <Backdrop onBackdrop={cancel}>
      <div style={{ ...styles.modal, borderColor: st.accent }}>
        <div style={styles.modalHead}>
          <span style={{ ...styles.modalIcon, background: st.bg, color: st.accent, borderColor: st.accent }}>{st.icon}</span>
          <div style={{ ...styles.modalTitle, color: st.accent }}>{dialog.title ?? "확인"}</div>
        </div>
        <div style={styles.modalBody}>{dialog.message}</div>
        {dialog.requireType && (
          <div style={{ marginTop: 12 }}>
            <div style={styles.requireHint}>
              계속하려면 <b style={{ color: st.accent }}>{dialog.requireType}</b> 를 입력하세요.
            </div>
            <input
              autoFocus
              style={styles.input}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={dialog.requireType}
            />
          </div>
        )}
        {dialog.dontAskAgainKey && (
          <label style={styles.dontAsk}>
            <input type="checkbox" checked={dontAsk} onChange={(e) => setDontAsk(e.target.checked)} />
            다시 묻지 않기
          </label>
        )}
        <div style={styles.modalActions}>
          <button style={styles.btnCancel} onClick={cancel}>{dialog.cancelLabel ?? "취소"}</button>
          <button
            style={{
              ...styles.btnConfirm,
              background: typeOk ? st.accent : "#333",
              color: typeOk ? "#0f0f0f" : "#666",
              cursor: typeOk ? "pointer" : "not-allowed",
            }}
            onClick={confirm}
            disabled={!typeOk}
          >
            {dialog.confirmLabel ?? "확인"}
          </button>
        </div>
      </div>
    </Backdrop>
  );
}

function PromptModal({
  dialog,
  onResolve,
}: {
  dialog: Extract<NonNullable<ReturnType<typeof useUiStore.getState>["dialog"]>, { type: "prompt" }>;
  onResolve: (r: string | null) => void;
}) {
  const [value, setValue] = useState(dialog.defaultValue ?? "");
  const extra = dialog.extraField;
  const [extraValue, setExtraValue] = useState(extra?.defaultValue ?? "");
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.select(); }, []);

  function submit() {
    // 확정 시점에 2번째 필드 값을 호출부로 전달한다(취소하면 전달하지 않음).
    extra?.onChange(extraValue.trim());
    onResolve(value.trim() ? value : (dialog.defaultValue ? value : ""));
  }
  return (
    <Backdrop onBackdrop={() => onResolve(null)}>
      <div style={{ ...styles.modal, borderColor: "#4338ca" }}>
        <div style={styles.modalHead}>
          <div style={{ ...styles.modalTitle, color: "#a5b4fc" }}>{dialog.title ?? "입력"}</div>
        </div>
        {dialog.message && <div style={styles.modalBody}>{dialog.message}</div>}
        <input
          ref={inputRef}
          autoFocus
          style={{ ...styles.input, marginTop: 12 }}
          value={value}
          placeholder={dialog.placeholder}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            else if (e.key === "Escape") onResolve(null);
          }}
        />
        {extra && (
          <>
            <div style={{ marginTop: 14, fontSize: 13, color: "#9ca3af" }}>{extra.label}</div>
            <input
              style={{ ...styles.input, marginTop: 6 }}
              list={extra.options?.length ? "ui-prompt-extra-options" : undefined}
              value={extraValue}
              placeholder={extra.placeholder}
              onChange={(e) => setExtraValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
                else if (e.key === "Escape") onResolve(null);
              }}
            />
            {!!extra.options?.length && (
              <datalist id="ui-prompt-extra-options">
                {extra.options.map((o) => <option key={o} value={o} />)}
              </datalist>
            )}
          </>
        )}
        <div style={styles.modalActions}>
          <button style={styles.btnCancel} onClick={() => onResolve(null)}>{dialog.cancelLabel ?? "취소"}</button>
          <button style={{ ...styles.btnConfirm, background: "#6366f1", color: "#fff" }} onClick={submit}>
            {dialog.confirmLabel ?? "확인"}
          </button>
        </div>
      </div>
    </Backdrop>
  );
}

function Backdrop({ children, onBackdrop }: { children: React.ReactNode; onBackdrop: () => void }) {
  return (
    <div style={styles.backdrop} onClick={(e) => { if (e.target === e.currentTarget) onBackdrop(); }}>
      {children}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  toastStack: {
    position: "fixed",
    top: 16,
    right: 16,
    zIndex: 10000,
    display: "flex",
    flexDirection: "column",
    gap: 8,
    maxWidth: 380,
  },
  toast: {
    display: "flex",
    alignItems: "flex-start",
    gap: 10,
    padding: "10px 14px",
    borderRadius: 8,
    border: "1px solid",
    fontSize: 13,
    cursor: "pointer",
    boxShadow: "0 6px 20px rgba(0,0,0,0.5)",
    animation: "ui-toast-in 0.2s ease",
  },
  toastIcon: { fontWeight: 700, flexShrink: 0, lineHeight: 1.4 },
  toastMsg: { flex: 1, lineHeight: 1.4, whiteSpace: "pre-wrap", wordBreak: "break-word" },
  toastClose: { color: "#888", flexShrink: 0, fontSize: 16, lineHeight: 1.2 },
  backdrop: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.6)",
    zIndex: 10001,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
  },
  modal: {
    background: "#1a1a1a",
    border: "1px solid",
    borderRadius: 12,
    padding: 24,
    width: "100%",
    maxWidth: 440,
    boxShadow: "0 10px 40px rgba(0,0,0,0.7)",
    animation: "ui-modal-in 0.18s ease",
  },
  modalHead: { display: "flex", alignItems: "center", gap: 12, marginBottom: 12 },
  modalIcon: {
    width: 32,
    height: 32,
    borderRadius: 8,
    border: "1px solid",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 18,
    fontWeight: 700,
    flexShrink: 0,
  },
  modalTitle: { fontSize: 16, fontWeight: 700 },
  modalBody: { color: "#ccc", fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap" },
  requireHint: { color: "#aaa", fontSize: 12, marginBottom: 6 },
  input: {
    width: "100%",
    boxSizing: "border-box",
    padding: "10px 12px",
    borderRadius: 8,
    border: "1px solid #333",
    background: "#111",
    color: "#fff",
    fontSize: 14,
    outline: "none",
  },
  dontAsk: { display: "flex", alignItems: "center", gap: 6, color: "#888", fontSize: 12, marginTop: 14, cursor: "pointer" },
  modalActions: { display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 20 },
  btnCancel: { padding: "9px 18px", background: "#333", color: "#ccc", border: "none", borderRadius: 8, fontSize: 14, cursor: "pointer" },
  btnConfirm: { padding: "9px 18px", border: "none", borderRadius: 8, fontSize: 14, fontWeight: 600 },
};
