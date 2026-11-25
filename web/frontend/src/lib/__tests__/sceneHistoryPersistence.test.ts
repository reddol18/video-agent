/**
 * #372 — scene history 영속화 단위 테스트.
 *
 * 메모리 undoDeltas 의 변화에 따라 IndexedDB 의 patch 키 집합이 1:1 로
 * 동기화되는지 확인한다. 특히 다음 시나리오를 회귀 방지로 검증한다:
 *   1) commit A→B→C→D 후 undo 2회 → 새 commit X.
 *      IDB 패치 seq 가 메모리 undoDeltas 의 seq 집합과 정확히 일치.
 *      (옛 분기 BC, CD 는 모두 삭제되어 새로고침 시 잘못된 history 재구성을 방지.)
 *   2) redoDeltas 가 차 있는 상태에서 commit → 같은 분기 truncate.
 *   3) 새로고침 시뮬레이션: dbLoadSceneHistory 가 메모리 모델의 patch 순서로 반환.
 */
import "fake-indexeddb/auto";
import { describe, expect, it } from "vitest";
import {
  dbAppendSceneHistoryPatch,
  dbClearSceneHistory,
  dbDeleteSceneHistoryPatchesBelow,
  dbKeepOnlySceneHistoryPatchSeqs,
  dbListSceneHistoryPatchSeqs,
  dbLoadSceneHistory,
  dbSaveSceneHistoryBase,
} from "../db";
import {
  createPersistenceRef,
  persistHistoryChange,
  type PersistenceDeps,
  type PersistenceState,
} from "../sceneHistoryPersistence";
// `PersistenceState` 는 snapshot 헬퍼의 반환 타입 명시에 쓰인다.
import type { Project } from "../../types";

// ── 테스트 환경 헬퍼 ────────────────────────────────────────────────────────────

function makeProject(idSuffix: string = ""): Project {
  return {
    project_id: `p-test${idSuffix}`,
    name: "test",
    footage_dir: "",
    scenario_text: "",
    status: "edit",
    scenes: [],
    tts_voice: "ko-KR-Chirp3-HD-Orus",
    created_at: "2026-06-15T00:00:00Z",
    updated_at: "2026-06-15T00:00:00Z",
    studio_assets: [],
    output_width: 1920,
    output_height: 1080,
    bgm_tracks: [],
  } as unknown as Project;
}

function makeDeps(): PersistenceDeps {
  return {
    dbClearSceneHistory,
    dbSaveSceneHistoryBase,
    dbDeleteSceneHistoryPatchesBelow,
    dbKeepOnlySceneHistoryPatchSeqs,
    dbAppendSceneHistoryPatch,
  };
}

/** IDB 를 비우고 새 ref 를 만들어 깔끔한 상태에서 시작. */
async function freshSession(projectId: string) {
  await dbClearSceneHistory(projectId);
  return {
    ref: createPersistenceRef(),
    deps: makeDeps(),
  };
}

/**
 * 메모리 store 의 commit/undo/redo 동작을 단순 모사하고, 각 단계 직후 영속화를
 * 호출해 IDB 와 메모리 모델이 1:1 동기화되도록 한다.
 */
function makeStore() {
  const base: Project | null = null;
  return {
    historyBase: base as Project | null,
    undoDeltas: [] as unknown[],
    redoDeltas: [] as unknown[],
    nextHistorySeq: 0,
  };
}

function snapshot(s: ReturnType<typeof makeStore>): PersistenceState {
  return {
    historyBase: s.historyBase,
    undoDeltas: s.undoDeltas.slice(),
    nextHistorySeq: s.nextHistorySeq,
  };
}

// 모든 테스트는 고유 projectId 로 격리(병렬 실행 시 간섭 방지).
let _tid = 0;
function nextProjectId() {
  _tid += 1;
  return `pid-${Date.now()}-${_tid}`;
}

describe("#372 scene history 영속화 — 메모리 ↔ IDB 1:1 동기화", () => {
  it("기본 시퀀스: commit 4회 → IDB 에 base + p:00~p:03", async () => {
    const projectId = nextProjectId();
    const { ref, deps } = await freshSession(projectId);
    const store = makeStore();
    const baseProject = makeProject("-base");

    // 첫 commit — historyBase 가 baseProject 로 설정되고 undoDeltas=[].
    const prev0 = snapshot(store);
    store.historyBase = baseProject;
    await persistHistoryChange(projectId, snapshot(store), prev0, ref, deps);

    // 이후 commit 4회 (A→B, B→C, C→D, D→E 처럼 가짜 delta 4개).
    for (let i = 0; i < 4; i++) {
      const prev = snapshot(store);
      store.undoDeltas.push({ op: `step${i}` });
      store.redoDeltas = [];
      store.nextHistorySeq += 1;
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }

    const seqs = await dbListSceneHistoryPatchSeqs(projectId);
    expect(seqs).toEqual([0, 1, 2, 3]);
    expect(ref.persistedSeqs).toEqual([0, 1, 2, 3]);
  });

  it("undo 2회 후 새 commit → IDB 에 옛 분기 patch 가 남지 않음 (#372 회귀)", async () => {
    const projectId = nextProjectId();
    const { ref, deps } = await freshSession(projectId);
    const store = makeStore();

    // 초기 base.
    const baseProject = makeProject("-base");
    const prev0 = snapshot(store);
    store.historyBase = baseProject;
    await persistHistoryChange(projectId, snapshot(store), prev0, ref, deps);

    // commit A→B, B→C, C→D.
    const deltas = ["AB", "BC", "CD"];
    for (const d of deltas) {
      const prev = snapshot(store);
      store.undoDeltas.push({ op: d });
      store.redoDeltas = [];
      store.nextHistorySeq += 1;
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1, 2]);

    // Undo 2회 — undoDeltas=[AB], redoDeltas=[CD,BC], nextHistorySeq=3 불변.
    for (let i = 0; i < 2; i++) {
      const prev = snapshot(store);
      const d = store.undoDeltas.pop();
      if (d !== undefined) store.redoDeltas.push(d);
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }
    expect(store.undoDeltas).toEqual([{ op: "AB" }]);
    // 분기 4: IDB 는 그대로(redo 가능성), ref.undoLen 만 감소.
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1, 2]);
    expect(ref.undoLen).toBe(1);

    // 새 commit BX (분기 시작).
    const prevBefore = snapshot(store);
    store.undoDeltas.push({ op: "BX" });
    store.redoDeltas = [];
    store.nextHistorySeq += 1; // 3 → 4
    await persistHistoryChange(projectId, snapshot(store), prevBefore, ref, deps);

    // 검증: 메모리 undoDeltas=[AB, BX]. IDB seq = [0, 3] (BC, CD 는 삭제).
    expect(store.undoDeltas).toEqual([{ op: "AB" }, { op: "BX" }]);
    const seqsAfter = await dbListSceneHistoryPatchSeqs(projectId);
    expect(seqsAfter).toEqual([0, 3]);
    expect(ref.persistedSeqs).toEqual([0, 3]);

    // 새로고침 시뮬레이션 — dbLoadSceneHistory 가 메모리와 동일한 순서의 patch 반환.
    const loaded = await dbLoadSceneHistory(projectId);
    expect(loaded.base).not.toBeNull();
    expect(loaded.patches).toEqual([{ op: "AB" }, { op: "BX" }]);
  });

  it("redo: undo 후 redo 1회 → 메모리/IDB 가 commit 직후 상태로 복귀", async () => {
    const projectId = nextProjectId();
    const { ref, deps } = await freshSession(projectId);
    const store = makeStore();
    store.historyBase = makeProject("-redo");
    await persistHistoryChange(projectId, snapshot(store), { historyBase: null, undoDeltas: [], nextHistorySeq: 0 }, ref, deps);

    // commit A→B, B→C.
    for (const d of ["AB", "BC"]) {
      const prev = snapshot(store);
      store.undoDeltas.push({ op: d });
      store.redoDeltas = [];
      store.nextHistorySeq += 1;
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1]);

    // Undo 1회.
    {
      const prev = snapshot(store);
      const d = store.undoDeltas.pop();
      if (d !== undefined) store.redoDeltas.push(d);
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }
    expect(store.undoDeltas).toEqual([{ op: "AB" }]);
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1]); // IDB 그대로.

    // Redo 1회 — 같은 BC 가 메모리에 다시 들어옴.
    {
      const prev = snapshot(store);
      const d = store.redoDeltas.pop();
      if (d !== undefined) store.undoDeltas.push(d);
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }
    expect(store.undoDeltas).toEqual([{ op: "AB" }, { op: "BC" }]);
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1]);
    expect(ref.persistedSeqs).toEqual([0, 1]);
    const loaded = await dbLoadSceneHistory(projectId);
    expect(loaded.patches).toEqual([{ op: "AB" }, { op: "BC" }]);
  });

  it("redoDeltas 가 차 있는 상태에서 commit → 옛 redo 분기 IDB 정리", async () => {
    // commitProjectHistory 가 redoDeltas 를 비우는 경로 — 분기 발생.
    const projectId = nextProjectId();
    const { ref, deps } = await freshSession(projectId);
    const store = makeStore();
    store.historyBase = makeProject("-redocom");
    await persistHistoryChange(projectId, snapshot(store), { historyBase: null, undoDeltas: [], nextHistorySeq: 0 }, ref, deps);

    // commit 3회.
    for (const d of ["AB", "BC", "CD"]) {
      const prev = snapshot(store);
      store.undoDeltas.push({ op: d });
      store.redoDeltas = [];
      store.nextHistorySeq += 1;
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }

    // Undo 1회 → undoDeltas=[AB,BC], redoDeltas=[CD].
    {
      const prev = snapshot(store);
      const d = store.undoDeltas.pop();
      if (d !== undefined) store.redoDeltas.push(d);
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }

    // 새 commit X → undoDeltas=[AB,BC,CX], redoDeltas=[], nextHistorySeq=4.
    {
      const prev = snapshot(store);
      store.undoDeltas.push({ op: "CX" });
      store.redoDeltas = [];
      store.nextHistorySeq += 1;
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }

    // 검증: 메모리 3개, IDB seq = [0, 1, 3] (옛 CD 의 seq=2 는 삭제).
    expect(store.undoDeltas).toEqual([{ op: "AB" }, { op: "BC" }, { op: "CX" }]);
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1, 3]);
    expect(ref.persistedSeqs).toEqual([0, 1, 3]);
    const loaded = await dbLoadSceneHistory(projectId);
    expect(loaded.patches).toEqual([{ op: "AB" }, { op: "BC" }, { op: "CX" }]);
  });

  it("clearHistory: base=null 전환 시 IDB 전체 삭제", async () => {
    const projectId = nextProjectId();
    const { ref, deps } = await freshSession(projectId);
    const store = makeStore();
    store.historyBase = makeProject("-clr");
    await persistHistoryChange(projectId, snapshot(store), { historyBase: null, undoDeltas: [], nextHistorySeq: 0 }, ref, deps);

    // commit 2회.
    for (const d of ["AB", "BC"]) {
      const prev = snapshot(store);
      store.undoDeltas.push({ op: d });
      store.redoDeltas = [];
      store.nextHistorySeq += 1;
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1]);

    // clearHistory — historyBase=null, undoDeltas=[].
    const prev = snapshot(store);
    store.historyBase = null;
    store.undoDeltas = [];
    store.redoDeltas = [];
    store.nextHistorySeq = 0;
    await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);

    // IDB 비어 있어야 함.
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([]);
    const loaded = await dbLoadSceneHistory(projectId);
    expect(loaded.base).toBeNull();
    expect(loaded.patches).toEqual([]);
  });

  it("base 흡수(MAX_HISTORY 초과 시뮬레이션): 새 base 로 교체되면 oldestSeq 미만 patch 삭제", async () => {
    const projectId = nextProjectId();
    const { ref, deps } = await freshSession(projectId);
    const store = makeStore();
    store.historyBase = makeProject("-absorb1");
    await persistHistoryChange(projectId, snapshot(store), { historyBase: null, undoDeltas: [], nextHistorySeq: 0 }, ref, deps);

    // commit 3회 → seq 0,1,2.
    for (const d of ["AB", "BC", "CD"]) {
      const prev = snapshot(store);
      store.undoDeltas.push({ op: d });
      store.redoDeltas = [];
      store.nextHistorySeq += 1;
      await persistHistoryChange(projectId, snapshot(store), prev, ref, deps);
    }
    expect(await dbListSceneHistoryPatchSeqs(projectId)).toEqual([0, 1, 2]);

    // 다음 commit 에서 base 흡수가 일어났다고 가정:
    //   - AB 가 새 base 에 흡수됨 → store.historyBase 가 새 객체로 교체, undoDeltas=[BC,CD,DX].
    const prevAbsorb = snapshot(store);
    store.historyBase = makeProject("-absorb2"); // 새 객체 참조 → 분기 2 트리거.
    store.undoDeltas = [{ op: "BC" }, { op: "CD" }, { op: "DX" }];
    store.nextHistorySeq += 1; // 3 → 4
    await persistHistoryChange(projectId, snapshot(store), prevAbsorb, ref, deps);

    // ref.persistedSeqs 는 [1, 2, 3] 으로 갱신되어야 하고, IDB 의 seq=0 (=AB, 흡수됨) 은 삭제,
    // 새로 흡수와 함께 들어온 DX(seq=3) 가 append 되어 메모리와 1:1.
    expect(ref.persistedSeqs).toEqual([1, 2, 3]);
    const seqsAfter = await dbListSceneHistoryPatchSeqs(projectId);
    expect(seqsAfter).toEqual([1, 2, 3]);
    const loaded = await dbLoadSceneHistory(projectId);
    expect(loaded.patches).toEqual([{ op: "BC" }, { op: "CD" }, { op: "DX" }]);
  });
});
