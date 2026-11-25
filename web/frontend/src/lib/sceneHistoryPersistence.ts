/**
 * #369 / #372 — Editor 의 useStore subscribe 핸들러를 분리한 영속화 로직.
 *
 * Editor.tsx 에서 인라인으로 작성되어 있던 history 영속화 코드를 단위 테스트
 * 가능한 형태로 추출했다. 핸들러는 zustand subscribe(prev, next) 콜백과 동일한
 * 시그니처를 가지며, IDB 호출은 인자로 주입받는 PersistenceDeps 를 통해 한다
 * (테스트에서 fake-indexeddb 기반 실제 함수, 운영에서 src/lib/db.ts 의 export 함수).
 *
 * 핵심 책임:
 *   1) 메모리 undoDeltas ↔ IDB patch seq 의 1:1 대응을 유지한다.
 *   2) 새 commit 으로 분기가 시작되어 옛 redoDeltas 대응분이 메모리에서 사라졌을 때,
 *      IDB 의 그 옛 buried patch 를 함께 정리한다(#372).
 *   3) undo 는 IDB 를 건드리지 않는다(redo 로 같은 seq 에 재append 가능).
 */
import type { Project } from "../types";

export interface PersistenceState {
  /** zustand store 의 historyBase. */
  historyBase: Project | null;
  /** undoDeltas. */
  undoDeltas: unknown[];
  /** 다음 commit 시 부여될 seq 카운터(undo/redo 시에는 변화 없음). */
  nextHistorySeq: number;
}

export interface PersistenceRef {
  /** 마지막으로 영속화에 반영된 historyBase 참조. base 객체 식별자가 변하면 새 base 로 인식. */
  baseRef: Project | null;
  /** 메모리 undoDeltas 의 각 delta 가 IDB 에 영속화된 seq(인덱스 기준 i ↔ undoDeltas[i]). */
  persistedSeqs: number[];
  /** 마지막으로 ref 가 인지한 메모리 undoDeltas.length(분기 4 영향 추적용). */
  undoLen: number;
  /** clearHistory 가 한 번 호출된 후의 멱등 가드. */
  cleared: boolean;
}

export interface PersistenceDeps {
  dbClearSceneHistory: (projectId: string) => Promise<void>;
  dbSaveSceneHistoryBase: (projectId: string, base: Project) => Promise<void>;
  dbDeleteSceneHistoryPatchesBelow: (projectId: string, cutoffSeq: number) => Promise<void>;
  dbKeepOnlySceneHistoryPatchSeqs: (projectId: string, keep: number[]) => Promise<void>;
  dbAppendSceneHistoryPatch: (projectId: string, seq: number, patch: unknown) => Promise<void>;
}

export function createPersistenceRef(): PersistenceRef {
  return { baseRef: null, persistedSeqs: [], undoLen: 0, cleared: false };
}

/**
 * 새 (state, prev) 콜백.
 *
 * 반환되는 Promise 는 IDB 작업이 완료된 시점에 resolve 된다(테스트에서 await 가능).
 * 운영 코드(Editor.tsx)는 fire-and-forget 으로 사용해도 무방하지만, 분기 3 의 keepOnly
 * → append 순서를 보장하기 위해 await 체인을 사용한다.
 */
export async function persistHistoryChange(
  projectId: string,
  state: PersistenceState,
  prev: PersistenceState,
  ref: PersistenceRef,
  deps: PersistenceDeps,
): Promise<void> {
  const curBase = state.historyBase;
  const prevBase = prev.historyBase;
  const curLen = state.undoDeltas.length;

  // 1) clearHistory — base 가 null 이 되었거나 명시적 초기화.
  if (curBase === null && (prevBase !== null || curLen === 0)) {
    if (!ref.cleared) {
      ref.cleared = true;
      ref.baseRef = null;
      ref.persistedSeqs = [];
      ref.undoLen = 0;
      try { await deps.dbClearSceneHistory(projectId); } catch { /* ignore */ }
    }
    return;
  }

  // 2) base 신규 또는 흡수(다른 객체로 교체) — 새 base put + 흡수된 patch 삭제.
  //    실제 store.commitProjectHistory 는 base 흡수와 새 delta append 를 같은 set 으로
  //    처리하므로 분기 2 안에서 메모리 undoDeltas 전체를 IDB 의 모든 patch seq 와 동기화한다.
  if (curBase && curBase !== ref.baseRef) {
    ref.cleared = false;
    ref.baseRef = curBase;
    const oldestSeq = Math.max(0, state.nextHistorySeq - curLen);
    const desiredSeqs = Array.from({ length: curLen }, (_, i) => oldestSeq + i);
    // 새 base 흡수에 따라 기존 IDB 의 어느 seq 가 살아남는지 결정.
    const prevPersisted = new Set(ref.persistedSeqs);
    const toAppend: Array<{ seq: number; delta: unknown }> = [];
    for (let i = 0; i < curLen; i++) {
      const seq = desiredSeqs[i];
      if (!prevPersisted.has(seq)) {
        // 메모리상 이 인덱스의 delta 가 새로 추가된 것(흡수와 함께 append 된 신규 delta).
        toAppend.push({ seq, delta: state.undoDeltas[i] });
      }
    }
    ref.persistedSeqs = desiredSeqs.slice();
    ref.undoLen = curLen;
    try {
      await deps.dbSaveSceneHistoryBase(projectId, curBase);
      if (oldestSeq > 0) await deps.dbDeleteSceneHistoryPatchesBelow(projectId, oldestSeq);
      // 메모리에 없는 잔존 patch 제거.
      await deps.dbKeepOnlySceneHistoryPatchSeqs(projectId, desiredSeqs);
      for (const { seq, delta } of toAppend) {
        await deps.dbAppendSceneHistoryPatch(projectId, seq, delta);
      }
    } catch { /* ignore */ }
    return;
  }

  // 3) 같은 base 에서 새 delta append (commit / pushHistorySnapshot / redo).
  if (curBase && curBase === ref.baseRef && curLen > ref.undoLen) {
    const newDeltas = state.undoDeltas.slice(ref.undoLen);
    const baseSeq = state.nextHistorySeq - newDeltas.length;
    const newSeqs = newDeltas.map((_, i) => baseSeq + i);
    const survivingOldSeqs = ref.persistedSeqs.slice(0, ref.undoLen);
    const nextPersistedSeqs = [...survivingOldSeqs, ...newSeqs];
    ref.persistedSeqs = nextPersistedSeqs;
    ref.undoLen = curLen;
    try {
      // 새 분기 시작 시 옛 buried patch 정리 — 메모리 undoDeltas 에 없는 seq 는 모두 삭제(#372).
      await deps.dbKeepOnlySceneHistoryPatchSeqs(projectId, nextPersistedSeqs);
      for (let i = 0; i < newDeltas.length; i++) {
        await deps.dbAppendSceneHistoryPatch(projectId, newSeqs[i], newDeltas[i]);
      }
    } catch { /* ignore */ }
    return;
  }

  // 4) undo 로 길이가 줄어든 경우 — ref.undoLen 만 감소시키고 IDB 는 그대로 둠.
  //    redo 시 분기 3 으로 다시 들어와 같은 seq 에 덮어쓰기되어 복원.
  //    이후 새 commit 으로 분기가 확정되면 분기 3 의 keepOnly 가 옛 buried patch 를 정리.
  if (curLen < ref.undoLen) {
    ref.undoLen = curLen;
  }
}
