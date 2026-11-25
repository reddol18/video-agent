import { create } from "zustand";
import * as jsondiffpatch from "jsondiffpatch";
import type { Delta } from "jsondiffpatch";
import type {
  Project,
  SceneCard,
  MatchingMethod,
  TtsMeta,
  TextOverlay,
  ShapeOverlay,
  GraphicOverlay,
  SceneFilter,
  SceneMotion,
  MosaicOverlay,
  ImageOverlay,
  SpeedLinesOverlay,
} from "../types";

// #369 — patch 기반 history 인프라.
//
// 50개의 Project deep snapshot 을 메모리에 들고 다니는 기존 구현 대신,
// "가장 오래된 commit 시점 직전 state(base)" 1개 + N개의 forward delta 만 보관한다.
//
// 모델:
//   state[0] = historyBase
//   state[i] = patch(state[i-1], undoDeltas[i-1])
//   현재 project 는 state[N-1] 일 수도, 그 이후로 변경된 in-progress 상태일 수도 있다.
//
// commitProjectHistory():
//   prevState = state[N-1] 계산 (base 에 모든 delta 적용)
//   delta = diff(prevState, currentProject)
//   delta 가 비어 있으면 push 안 함(변경 없음).
//   delta 가 있으면 undoDeltas.push(delta), redoDeltas = [].
//
// undo():
//   마지막 delta D = undoDeltas.pop()
//   project = unpatch(currentProject, D) 또는 동치인 state[N-2]
//   redoDeltas.push(D)
//
// redo():
//   D = redoDeltas.pop()
//   project = patch(currentProject, D)
//   undoDeltas.push(D)
//
// MAX_HISTORY 초과 시: undoDeltas[0] 을 base 에 적용해 새로운 base 로 만들고 shift.
//
// jsondiffpatch 기본 옵션은 배열 안 객체 매칭에 objectHash 가 필요하지만, Project 의
// 배열은 모두 *각 항목의 정체성(scene_id, overlay id 등)을 갖는다*. 객체 매칭 정확도를
// 위해 인스턴스 단위 instance.

const _diffpatcher = jsondiffpatch.create({
  // Project 의 모든 배열 항목은 안정된 식별자가 있다. 어떤 키든 우선순위로 시도해 매칭.
  objectHash: (obj: unknown, _idx?: number): string | undefined => {
    if (obj && typeof obj === "object") {
      const o = obj as Record<string, unknown>;
      // 씬: scene_id. 오버레이/효과: id. 자산: asset_id. footage: file/image. 기타 fallback.
      if (typeof o.scene_id === "number") return `scene:${o.scene_id}`;
      if (typeof o.id === "string" || typeof o.id === "number") return `id:${o.id}`;
      if (typeof o.asset_id === "string") return `asset:${o.asset_id}`;
      if (typeof o.file === "string") return `file:${o.file}`;
      if (typeof o.image === "string") return `image:${o.image}`;
    }
    return undefined;
  },
  arrays: { detectMove: true, includeValueOnMove: false },
  // 텍스트 diff(diff-match-patch) 비활성화 — Project 의 텍스트 필드는 길지 않다.
  textDiff: { minLength: Number.MAX_SAFE_INTEGER },
  cloneDiffValues: true,
});

const MAX_HISTORY = 50;

/**
 * #369 — IndexedDB 영속화용 patch 1 단계 레코드.
 * key: `${projectId}:${ts}:${rand}` (정렬 가능 + 충돌 회피)
 * value: 직렬화된 patch + 타임스탬프 + projectId(다른 프로젝트 혼입 방지용 명시 필드).
 *
 * scene_history store 와 base store 는 lib/db.ts 에 v8 마이그레이션으로 추가된다.
 * store 내부는 IndexedDB 의 실제 I/O 를 직접 호출하지 않는다 — Editor 가 store 의
 * undo/redo 직후 별도로 db 모듈을 호출한다(서버 동기화와 같은 패턴). 다만
 * 메모리 patch round-trip 자체는 store 안에서 처리한다.
 */
function _cloneProject(p: Project): Project {
  // 빠른 deep clone — structuredClone 미지원 환경 대비.
  return JSON.parse(JSON.stringify(p)) as Project;
}

/** delta 가 의미 있는 변경을 담고 있는지 — jsondiffpatch 는 변경 없을 때 undefined 반환. */
function _deltaIsEmpty(d: Delta | undefined): boolean {
  return d === undefined || d === null;
}

/**
 * state[N-1] (= 마지막 history 시점) 을 base + 모든 delta 누적 적용해서 복원한다.
 * deltas 는 변경하지 않는다(jsondiffpatch.patch 는 in-place 적용이라 clone 후 처리).
 */
function _replayToTip(base: Project | null, deltas: Delta[]): Project | null {
  if (!base) return null;
  let cur: Project = _cloneProject(base);
  for (const d of deltas) {
    if (!d) continue;
    const next = _diffpatcher.patch(cur, _diffpatcher.clone(d) as Delta) as Project;
    cur = next;
  }
  return cur;
}

/**
 * #369 — patch round-trip 헬퍼. 외부 테스트(vitest 등)나 dev 콘솔에서 호출 가능.
 *
 * 두 project 객체 사이의 forward delta 를 만들고, patch 적용 + unpatch 되돌리기를 수행한다.
 * 반환값:
 *   - forwardOk : patch(prev, delta) deep-equal next
 *   - backwardOk: unpatch(next, delta) deep-equal prev
 * 둘 다 true 면 round-trip OK.
 */
export function verifyPatchRoundtrip(
  prev: Project,
  next: Project,
): { forwardOk: boolean; backwardOk: boolean; delta: Delta } {
  const delta = _diffpatcher.diff(prev, next) as Delta;
  // jsondiffpatch.patch 는 in-place 변경이라 비교 전에 clone.
  const forwarded = _diffpatcher.patch(_cloneProject(prev), _diffpatcher.clone(delta) as Delta);
  const reversed = _diffpatcher.unpatch(_cloneProject(next), _diffpatcher.clone(delta) as Delta);
  const eq = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
  return {
    forwardOk: eq(forwarded, next),
    backwardOk: eq(reversed, prev),
    delta,
  };
}

// ── Effect Clipboard (#281) ─────────────────────────────────────────────────
// 씬 간 모든 효과 종류(text/shape/graphic/filter/motion/mosaic/image)를 한 번에 복사·붙여넣기.
// 기존 copiedOverlays(텍스트 단일/개별 ⧉)는 그대로 유지 — 두 클립보드는 독립적으로 동작.
export interface CopiedEffects {
  text_overlays: TextOverlay[];
  shape_overlays: ShapeOverlay[];
  graphic_overlays: GraphicOverlay[];
  filters: SceneFilter[];
  motions: SceneMotion[];
  mosaic_overlays: MosaicOverlay[];
  // 이미지 오버레이도 복사·붙여넣기 대상에 포함. 기존 클립보드(필드 없음)는
  // 빈 배열로 폴백되도록 Editor 측 paste 로직에서 ?? [] 처리한다 (회귀 안전).
  image_overlays: ImageOverlay[];
  // 집중선(속도선) 오버레이. 기존 클립보드(필드 없음)는 Editor paste 로직에서 ?? [] 폴백.
  speed_lines?: SpeedLinesOverlay[];
}

interface ProgressState {
  current: number;
  total: number;
  message: string;
  phase: string;
}

interface AppStore {
  // ── Setup ──────────────────────────────────────────────────────────────
  setupStep: 1 | 2 | 3 | 4 | 5;
  projectName: string;
  // 생성 시 지정하는 프로젝트 그룹(시리즈)명. "" 이면 시리즈 미편성.
  projectSeries: string;
  scenarioText: string;
  matchingMethod: MatchingMethod | null;
  isProcessing: boolean;
  progress: ProgressState;
  activeTaskId: string | null;

  setSetupStep: (s: AppStore["setupStep"]) => void;
  setProjectName: (v: string) => void;
  setProjectSeries: (v: string) => void;
  setScenarioText: (v: string) => void;
  setMatchingMethod: (m: MatchingMethod) => void;
  setProcessing: (v: boolean) => void;
  setProgress: (p: Partial<ProgressState>) => void;
  setActiveTaskId: (id: string | null) => void;

  // ── Project / Editor ───────────────────────────────────────────────────
  project: Project | null;
  selectedSceneId: number | null;
  effectModalOpen: boolean;
  mediaFilter: string;

  setProject: (p: Project | null) => void;
  updateScene: (sceneId: number, patch: Partial<SceneCard>) => void;
  selectScene: (id: number | null) => void;
  openEffectModal: () => void;
  closeEffectModal: () => void;
  setMediaFilter: (q: string) => void;

  // ── TTS ────────────────────────────────────────────────────────────────
  ttsPlayingSceneId: number | null;
  setTtsPlaying: (id: number | null) => void;
  updateSceneTts: (sceneId: number, tts: TtsMeta) => void;

  // ── Overlay clipboard ──────────────────────────────────────────────────
  copiedOverlays: TextOverlay[];
  setCopiedOverlays: (overlays: TextOverlay[]) => void;
  // #graphic-copy — 그래픽 오버레이 단일 복사 클립보드.
  // 텍스트(copiedOverlays)와 별개로 둔다: 붙여넣기 대상 배열이 다르고,
  // 하나를 복사했다고 다른 쪽 클립보드가 지워지면 혼란스럽다.
  copiedGraphic: GraphicOverlay | null;
  setCopiedGraphic: (g: GraphicOverlay | null) => void;

  // ── Effect clipboard (#281) ───────────────────────────────────────────
  // 텍스트 외 효과(도형/그래픽/필터/모션/모자이크)까지 모두 한 번에 복사·붙여넣기.
  // null이면 "아직 한 번도 효과 복사 안 함" 상태.
  copiedEffects: CopiedEffects | null;
  setCopiedEffects: (effects: CopiedEffects | null) => void;

  // ── 자연어 편집 패널 (#316, #334) ────────────────────────────────────────
  // patch 미리보기에서 영향 받는 *SceneCard.scene_id* 집합 — 씬 카드 테두리 강조용.
  // (과거에는 정렬 1-based 순번이었으나, 씬 삭제/재배치 후 scene_id 가 1,3,4 처럼
  //  비연속이 되면 Editor.tsx 의 `includes(scene.scene_id)` 비교가 엉뚱한 씬을 가리켰다.
  //  NlEditorPanel 이 `project.scenes` 를 정렬한 후 인덱스→scene_id 로 변환해 set 한다.)
  highlightedSceneIds: number[];
  setHighlightedSceneIds: (ids: number[]) => void;
  nlEditorPanelOpen: boolean;
  setNlEditorPanelOpen: (v: boolean) => void;
  /** 외부에서 강제 갱신용 — Editor 가 zustand store 의 project.scenes 를 다시 로드하도록 알린다. */
  nlEditApplyTick: number;
  bumpNlEditApplyTick: () => void;

  // ── Undo / Redo (#358 / #363 / #369) ──────────────────────────────────────
  // #369 — 이전엔 Project 의 deep clone 50개를 undoStack/redoStack 에 그대로 들고 다녔다.
  // 메모리 사용량이 컸고 새로고침 시 모두 사라지는 문제가 있었다.
  // 이번 변경으로 base(가장 오래된 commit 시점 직전) 1개 + forward delta 배열 N 개만
  // 메모리에 보관한다. 외부 API(commitProjectHistory/pushHistorySnapshot/undo/redo) 의
  // 시맨틱은 그대로 유지하여 Editor 의 호출 패턴을 변경할 필요가 없다.
  //
  // 영속화: Editor 가 commit 후 dbAppendSceneHistoryPatch / dbReplaceSceneHistoryBase
  // 를 호출해 IndexedDB 의 sceneHistory store 에 누적 patch 를 저장한다. 새로고침 시
  // Editor 진입에서 dbLoadSceneHistory 로 복원한다.

  /** 가장 오래된 commit 시점 직전 project (deep clone). 비어있으면 history 없음. */
  historyBase: Project | null;
  /** forward delta 들 — 각 항목 i: state[i-1] -> state[i] (i 번째 commit 시점) */
  undoDeltas: Delta[];
  /** undo 로 빠진 forward delta 들 — 가장 최근 undo 가 마지막. redo 시 마지막부터 다시 적용. */
  redoDeltas: Delta[];
  /** 현재 project 상태를 한 단계 history 로 누적한다 — setProject/updateScene *직전* 호출.
   *  내부적으로 (이전 commit 시점 -> currentProject) 의 forward delta 를 push 한다.
   *  변경이 없으면(delta 가 비어있으면) push 하지 않는다. */
  commitProjectHistory: () => void;
  /** #363 — 임의의 snapshot(=이전에 보관한 project 상태)을 commit 시점으로 push.
   *  슬라이더 드래그 시작 직전 상태를 ref 로 보관했다가 pointerup 시점에 호출한다.
   *  redoDeltas 도 함께 비운다 (= 새 분기 시작). */
  pushHistorySnapshot: (snapshot: Project) => void;
  /** undo: 마지막 commit 이전 상태로 되돌린다. 가능 여부는 canUndo()로 확인.
   *  내부: 마지막 delta D = undoDeltas.pop(), redoDeltas.push(D), project = unpatch(project, D). */
  undo: () => Project | null;
  /** redo: 직전에 undo한 변경을 다시 적용한다. */
  redo: () => Project | null;
  canUndo: () => boolean;
  canRedo: () => boolean;
  /** history 전체 초기화. 프로젝트 진입/교체 시 호출. */
  clearHistory: () => void;
  /** #369 — IndexedDB 에서 patch 들과 base 를 일괄 복원(새로고침 후 진입 시).
   *  base 는 호출자가 미리 dbLoadSceneHistory 로 가져온 결과를 그대로 넘긴다.
   *  startingSeq 는 *다음* append 가 사용할 seq. 복원한 patch 수와 일치시키면 된다. */
  restoreHistoryFromPatches: (base: Project, deltas: Delta[], startingSeq: number) => void;
  /** #369 — 가장 최근 push 된 forward delta 를 외부에 반환. IndexedDB append 용. */
  getLastUndoDelta: () => Delta | undefined;
  /** #369 — 다음에 IndexedDB 로 append 할 patch 의 seq. 매 commit/push 마다 단조 증가. */
  nextHistorySeq: number;

  // ── 오버레이 편집 가이드 표시 토글 (#359) ────────────────────────────────
  // 미리보기 위 텍스트/도형/그래픽/이미지 오버레이의 편집용 외곽선·드래그 핸들·라벨만
  // 숨긴다. 실제 합성 결과(text/shape/graphic content)는 그대로 보이게 한다.
  // localStorage 영속화 — 다음 세션에도 마지막 상태 유지.
  showOverlayGuides: boolean;
  setShowOverlayGuides: (v: boolean) => void;
  toggleOverlayGuides: () => void;

  // ── TimelineBar playhead / seek (#364) ────────────────────────────────────
  /** 미리보기 video 의 현재 재생 위치(씬 로컬 sec). PreviewPanel 의 onTimeUpdate 가 갱신.
   *  TimelineBar 는 (선택 씬의 누적 시작 sec) + previewCurrentTime 으로 글로벌 위치 계산.
   *  씬이 바뀌면 PreviewPanel 재마운트 시 0 으로 초기화된다. */
  previewCurrentTime: number;
  setPreviewCurrentTime: (sec: number) => void;
  /** TimelineBar 클릭/드래그로 seek 요청 — PreviewPanel 이 이 값을 감지해
   *  video.currentTime 을 sec 으로 설정한다. tick 은 같은 (sceneId, sec) 요청을
   *  반복 트리거하기 위한 단조 증가 카운터. */
  previewSeekRequest: { sceneId: number; sec: number; tick: number } | null;
  requestPreviewSeek: (sceneId: number, sec: number) => void;

  // ── Scene cut durations (#378) ────────────────────────────────────────────
  /** Phase 5 (`cache/timeline.json`) 의 실제 cut 길이 매핑. scene_id → cut_sec(>0).
   *  TimelineBar.getSceneDurationSec 의 ① 우선순위 폴백. 부재(=Phase 5 미실행)이면
   *  비어 있는 Map 이며 클라이언트는 footage/TTS/hint/3.0 폴백으로 자연 회귀한다.
   *  Editor 진입 + 전체 렌더 complete 후에 갱신된다. */
  sceneCutDurations: Map<number, number>;
  setSceneCutDurations: (m: Map<number, number>) => void;
}

export const useStore = create<AppStore>((set, get) => ({
  // Setup
  setupStep: 1,
  projectName: "",
  projectSeries: "",
  scenarioText: "",
  matchingMethod: null,
  isProcessing: false,
  progress: { current: 0, total: 0, message: "", phase: "" },
  activeTaskId: null,

  setSetupStep: (s) => set({ setupStep: s }),
  setProjectName: (v) => set({ projectName: v }),
  setProjectSeries: (v) => set({ projectSeries: v }),
  setScenarioText: (v) => set({ scenarioText: v }),
  setMatchingMethod: (m) => set({ matchingMethod: m }),
  setProcessing: (v) => set({ isProcessing: v }),
  setProgress: (p) => set((s) => ({ progress: { ...s.progress, ...p } })),
  setActiveTaskId: (id) => set({ activeTaskId: id }),

  // Project
  project: null,
  selectedSceneId: null,
  effectModalOpen: false,
  mediaFilter: "",

  setProject: (p) => set({ project: p }),
  updateScene: (sceneId, patch) =>
    set((s) => {
      if (!s.project) return {};
      const scenes = s.project.scenes.map((sc) =>
        sc.scene_id === sceneId ? { ...sc, ...patch } : sc
      );
      return { project: { ...s.project, scenes } };
    }),
  selectScene: (id) =>
    set((s) => {
      // #398 — 다른 씬으로 전환되는 순간 `previewCurrentTime` 을 0 으로 리셋한다.
      //   이유: PreviewPanel 은 key={scene_id-...} 로 씬 변경 시 재마운트되며,
      //         새 <video> 가 마운트되어 첫 timeupdate(=currentTime=0) 를 발화하기까지
      //         약 100~수백 ms 의 지연이 있다. 그 동안 store 의 `previewCurrentTime`
      //         에는 이전 씬의 마지막 currentTime(예: 8.3s) 이 남아 있고,
      //         TimelineBar 는 (새 씬의 누적 시작 sec) + 8.3 으로 playhead 를 그린다.
      //         이 race 가 #398 의 핵심 — 사용자는 "16번 선택했는데 14번 위에 박힘"
      //         처럼 인식한다 (실제로는 16번 시작점 + 옛 currentTime 위치).
      //   같은 씬 클릭(동일 id) 은 리셋하지 않아 재생 중 점프가 발생하지 않게 한다.
      if (id !== null && id === s.selectedSceneId) return { selectedSceneId: id };
      return { selectedSceneId: id, previewCurrentTime: 0 };
    }),
  openEffectModal: () => set({ effectModalOpen: true }),
  closeEffectModal: () => set({ effectModalOpen: false }),
  setMediaFilter: (q) => set({ mediaFilter: q }),

  // TTS
  ttsPlayingSceneId: null,
  setTtsPlaying: (id) => set({ ttsPlayingSceneId: id }),
  updateSceneTts: (sceneId, tts) => {
    const { updateScene } = get();
    updateScene(sceneId, { tts });
  },

  // Overlay clipboard
  copiedOverlays: [],
  setCopiedOverlays: (overlays) => set({ copiedOverlays: overlays }),
  copiedGraphic: null,
  setCopiedGraphic: (g) => set({ copiedGraphic: g }),

  // Effect clipboard (#281)
  copiedEffects: null,
  setCopiedEffects: (effects) => set({ copiedEffects: effects }),

  // 자연어 편집 패널 (#316)
  highlightedSceneIds: [],
  setHighlightedSceneIds: (ids) => set({ highlightedSceneIds: ids }),
  nlEditorPanelOpen: false,
  setNlEditorPanelOpen: (v) => set({ nlEditorPanelOpen: v }),
  nlEditApplyTick: 0,
  bumpNlEditApplyTick: () => set((s) => ({ nlEditApplyTick: s.nlEditApplyTick + 1 })),

  // ── Undo / Redo (#358 / #363 / #369) ────────────────────────────────────────
  historyBase: null,
  undoDeltas: [],
  redoDeltas: [],
  nextHistorySeq: 0,
  commitProjectHistory: () => {
    const s = get();
    if (!s.project) return;
    // 첫 commit — historyBase 가 비어 있다면 currentProject 가 곧 base(가장 오래된 직전 상태).
    if (s.historyBase === null) {
      set({
        historyBase: _cloneProject(s.project),
        undoDeltas: [],
        redoDeltas: [],
        nextHistorySeq: 0,
      });
      return;
    }
    // 이전 commit 시점 state[N-1] 복원 → 현재 project 와 diff.
    const prevState = _replayToTip(s.historyBase, s.undoDeltas);
    if (!prevState) return;
    const delta = _diffpatcher.diff(prevState, s.project);
    if (_deltaIsEmpty(delta)) {
      // 변경이 없으면 push 하지 않는다 — 빈 단계로 history 가 부풀지 않도록.
      // redoDeltas 만 비워 새 분기 시작 시맨틱은 유지.
      if (s.redoDeltas.length > 0) set({ redoDeltas: [] });
      return;
    }
    let nextBase = s.historyBase;
    let nextDeltas = [...s.undoDeltas, delta as Delta];
    if (nextDeltas.length > MAX_HISTORY) {
      // 가장 오래된 delta 1 개를 base 에 흡수해 새 base 로.
      const oldest = nextDeltas.shift()!;
      nextBase = _diffpatcher.patch(
        _cloneProject(nextBase),
        _diffpatcher.clone(oldest) as Delta,
      ) as Project;
    }
    set({
      historyBase: nextBase,
      undoDeltas: nextDeltas,
      redoDeltas: [],
      nextHistorySeq: s.nextHistorySeq + 1,
    });
  },
  // #363 / #369 — 외부에서 보관한 snapshot 을 한 단계 commit 시점으로 push.
  //               snapshot 자체를 보관하지 않고, (이전 commit -> snapshot) 의 forward delta 만 저장.
  //               이후 사용자가 슬라이더 종료 이후 추가 변경분이 있다면 다음 commitProjectHistory 에서 별개 단계.
  pushHistorySnapshot: (snapshot) => {
    const s = get();
    if (!snapshot) return;
    if (s.historyBase === null) {
      // 첫 history — snapshot 자체를 base 로.
      set({
        historyBase: _cloneProject(snapshot),
        undoDeltas: [],
        redoDeltas: [],
        nextHistorySeq: 0,
      });
      return;
    }
    const prevState = _replayToTip(s.historyBase, s.undoDeltas);
    if (!prevState) return;
    const delta = _diffpatcher.diff(prevState, snapshot);
    if (_deltaIsEmpty(delta)) {
      if (s.redoDeltas.length > 0) set({ redoDeltas: [] });
      return;
    }
    let nextBase = s.historyBase;
    let nextDeltas = [...s.undoDeltas, delta as Delta];
    if (nextDeltas.length > MAX_HISTORY) {
      const oldest = nextDeltas.shift()!;
      nextBase = _diffpatcher.patch(
        _cloneProject(nextBase),
        _diffpatcher.clone(oldest) as Delta,
      ) as Project;
    }
    set({
      historyBase: nextBase,
      undoDeltas: nextDeltas,
      redoDeltas: [],
      nextHistorySeq: s.nextHistorySeq + 1,
    });
  },
  undo: () => {
    const s = get();
    if (s.undoDeltas.length === 0 || !s.project || !s.historyBase) return null;
    // 마지막 delta 를 unpatch 로 되돌린다.
    const D = s.undoDeltas[s.undoDeltas.length - 1];
    // 현재 project 가 마지막 commit 시점에서 추가 변경이 있을 수 있다.
    // 해결: state[N-1] 을 재계산한 뒤 거기에 unpatch 적용 (= state[N-2]). 안전하다.
    const tip = _replayToTip(s.historyBase, s.undoDeltas);
    if (!tip) return null;
    const prev = _diffpatcher.unpatch(_cloneProject(tip), _diffpatcher.clone(D) as Delta) as Project;
    set({
      project: prev,
      undoDeltas: s.undoDeltas.slice(0, -1),
      redoDeltas: [...s.redoDeltas, D],
    });
    return prev;
  },
  redo: () => {
    const s = get();
    if (s.redoDeltas.length === 0 || !s.project || !s.historyBase) return null;
    const D = s.redoDeltas[s.redoDeltas.length - 1];
    const tip = _replayToTip(s.historyBase, s.undoDeltas);
    if (!tip) return null;
    const next = _diffpatcher.patch(_cloneProject(tip), _diffpatcher.clone(D) as Delta) as Project;
    set({
      project: next,
      redoDeltas: s.redoDeltas.slice(0, -1),
      undoDeltas: [...s.undoDeltas, D],
    });
    return next;
  },
  canUndo: () => get().undoDeltas.length > 0,
  canRedo: () => get().redoDeltas.length > 0,
  clearHistory: () =>
    set({ historyBase: null, undoDeltas: [], redoDeltas: [], nextHistorySeq: 0 }),
  restoreHistoryFromPatches: (base, deltas, startingSeq) => {
    set({
      historyBase: _cloneProject(base),
      undoDeltas: deltas,
      redoDeltas: [],
      nextHistorySeq: Math.max(0, startingSeq),
    });
  },
  getLastUndoDelta: () => {
    const s = get();
    return s.undoDeltas[s.undoDeltas.length - 1];
  },

  // ── 오버레이 편집 가이드 표시 토글 (#359) ────────────────────────────────
  showOverlayGuides: (() => {
    try {
      const raw = localStorage.getItem("editor_show_overlay_guides");
      return raw === null ? true : raw !== "0";
    } catch {
      return true;
    }
  })(),
  setShowOverlayGuides: (v) => {
    try { localStorage.setItem("editor_show_overlay_guides", v ? "1" : "0"); } catch { /* ignore */ }
    set({ showOverlayGuides: v });
  },
  toggleOverlayGuides: () => {
    const next = !get().showOverlayGuides;
    try { localStorage.setItem("editor_show_overlay_guides", next ? "1" : "0"); } catch { /* ignore */ }
    set({ showOverlayGuides: next });
  },

  // ── TimelineBar playhead / seek (#364) ────────────────────────────────────
  previewCurrentTime: 0,
  setPreviewCurrentTime: (sec) => set({ previewCurrentTime: sec }),
  previewSeekRequest: null,
  requestPreviewSeek: (sceneId, sec) =>
    set((s) => ({
      previewSeekRequest: {
        sceneId,
        sec: Math.max(0, sec),
        // tick 은 같은 (sceneId, sec) 재요청도 PreviewPanel 이 알 수 있도록 단조 증가.
        tick: (s.previewSeekRequest?.tick ?? 0) + 1,
      },
    })),

  // ── Scene cut durations (#378) ────────────────────────────────────────────
  sceneCutDurations: new Map<number, number>(),
  setSceneCutDurations: (m) => set({ sceneCutDurations: m }),
}));
