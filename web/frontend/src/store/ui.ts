/**
 * 공용 UI 알림/확인 스토어 (#296).
 *
 * 브라우저 기본 window.confirm/alert/prompt 를 앱 디자인과 일관된 Toast/Modal 로 대체한다.
 * - Toast: 성공/실패/정보 알림. 화면 우측 상단에 일정 시간 표시 후 자동 소멸. (alert 대체)
 * - Modal(confirm): 위험도(danger/warning/info)별 색/아이콘. (confirm 대체)
 *     · "다시 묻지 않기" 체크박스(localStorage) 지원.
 *     · 위험 동작은 requireType 으로 특정 문구를 직접 타이핑해야 확정 가능.
 * - Modal(prompt): 텍스트 입력 받기. (prompt 대체)
 *
 * 사용처에서는 `ui.confirm({...})` 가 Promise<boolean> 을, `ui.prompt({...})` 가
 * Promise<string|null> 을 반환하므로 기존 동기 window.confirm/prompt 호출을 await 로
 * 바꾸기만 하면 된다.
 *
 * <UiHost /> 를 앱 루트(App.tsx)에 한 번 렌더하면 Toast/Modal 이 화면에 표시된다.
 */
import { create } from "zustand";

export type ToastKind = "success" | "error" | "info";

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
  /** 자동 소멸까지 ms (기본 4000). 0 이면 수동 닫기 전까지 유지. */
  durationMs: number;
}

export type ConfirmVariant = "danger" | "warning" | "info";

export interface ConfirmOptions {
  title?: string;
  message: string;
  variant?: ConfirmVariant;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 이 문구를 입력해야 확정 버튼 활성화 (위험 동작용). 예: "삭제" */
  requireType?: string;
  /** "다시 묻지 않기" 체크박스 노출 + localStorage 키. 체크 시 다음부터 자동 확정. */
  dontAskAgainKey?: string;
}

export interface PromptOptions {
  title?: string;
  message?: string;
  defaultValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /**
   * 선택 입력 2번째 필드(예: 프로젝트 그룹). 지정하면 기본 입력칸 아래에 하나 더 표시된다.
   * 값은 `prompt()` 의 resolve 대신 `onExtra` 콜백으로 전달한다 — 기존 호출부의
   * `Promise<string|null>` 계약을 그대로 유지하기 위함.
   */
  extraField?: {
    label: string;
    placeholder?: string;
    defaultValue?: string;
    /** datalist 후보 목록(자유 입력도 허용). */
    options?: string[];
    onChange: (v: string) => void;
  };
}

interface ConfirmDialogState extends ConfirmOptions {
  id: number;
  type: "confirm";
  resolve: (ok: boolean) => void;
}

interface PromptDialogState extends PromptOptions {
  id: number;
  type: "prompt";
  resolve: (value: string | null) => void;
}

type DialogState = ConfirmDialogState | PromptDialogState;

interface UiStore {
  toasts: ToastItem[];
  dialog: DialogState | null;

  // ── Toast ──
  pushToast: (kind: ToastKind, message: string, durationMs?: number) => void;
  dismissToast: (id: number) => void;

  // ── 편의 헬퍼 (alert 대체) ──
  success: (message: string, durationMs?: number) => void;
  error: (message: string, durationMs?: number) => void;
  info: (message: string, durationMs?: number) => void;

  // ── Dialog ──
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  prompt: (opts: PromptOptions) => Promise<string | null>;
  resolveDialog: (result: boolean | string | null) => void;
}

let seq = 1;

function dontAskKey(key: string): string {
  return `ui.dontAskAgain:${key}`;
}

export const useUiStore = create<UiStore>((set, get) => ({
  toasts: [],
  dialog: null,

  pushToast: (kind, message, durationMs = 4000) => {
    const id = seq++;
    set((s) => ({ toasts: [...s.toasts, { id, kind, message, durationMs }] }));
    if (durationMs > 0) {
      setTimeout(() => get().dismissToast(id), durationMs);
    }
  },
  dismissToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  success: (message, durationMs) => get().pushToast("success", message, durationMs),
  error: (message, durationMs) => get().pushToast("error", message, durationMs),
  info: (message, durationMs) => get().pushToast("info", message, durationMs),

  confirm: (opts) =>
    new Promise<boolean>((resolve) => {
      // "다시 묻지 않기" 가 이전에 체크됐으면 즉시 통과.
      if (opts.dontAskAgainKey) {
        try {
          if (localStorage.getItem(dontAskKey(opts.dontAskAgainKey)) === "1") {
            resolve(true);
            return;
          }
        } catch { /* ignore */ }
      }
      set({
        dialog: {
          id: seq++,
          type: "confirm",
          variant: "info",
          ...opts,
          resolve,
        },
      });
    }),

  prompt: (opts) =>
    new Promise<string | null>((resolve) => {
      set({
        dialog: {
          id: seq++,
          type: "prompt",
          ...opts,
          resolve,
        },
      });
    }),

  resolveDialog: (result) => {
    const d = get().dialog;
    if (!d) return;
    set({ dialog: null });
    if (d.type === "confirm") {
      (d.resolve as (ok: boolean) => void)(result === true);
    } else {
      (d.resolve as (v: string | null) => void)(
        typeof result === "string" ? result : null
      );
    }
  },
}));

/** 비-React 컨텍스트(=라이브러리 함수)에서 토스트를 띄우기 위한 전역 접근자. */
export const ui = {
  success: (m: string, d?: number) => useUiStore.getState().success(m, d),
  error: (m: string, d?: number) => useUiStore.getState().error(m, d),
  info: (m: string, d?: number) => useUiStore.getState().info(m, d),
  confirm: (o: ConfirmOptions) => useUiStore.getState().confirm(o),
  prompt: (o: PromptOptions) => useUiStore.getState().prompt(o),
  markDontAskAgain: (key: string) => {
    try { localStorage.setItem(dontAskKey(key), "1"); } catch { /* ignore */ }
  },
};
