/**
 * 이어보기 프리페치 blob 저장소.
 *
 * PreviewPanel 은 씬이 바뀔 때마다 key 로 재마운트되므로, 프리페치 결과를 컴포넌트 안에
 * 두면 전환 즉시 사라진다. 따라서 모듈 스코프에 두고 Editor(부모)가 수명을 관리한다.
 *
 * 수명 규칙 (blob URL 누수 금지):
 *   - `take(sceneId)` 로 꺼내 쓴 URL 의 해제 책임은 **호출자**(PreviewPanel 의 prevBlobUrlRef)로 넘어간다.
 *   - 꺼내가지 않은 엔트리는 `invalidate` / `clearAll` 이 반드시 revoke 한다.
 *   - 진행 중(pending) 요청은 AbortController 로 취소하고, 취소 후 늦게 도착한 응답은 버린다.
 */

type PendingEntry = {
  kind: "pending";
  abort: AbortController;
  /** 이 요청이 무효화되었는지. true 면 완료되어도 저장하지 않고 revoke. */
  cancelled: boolean;
};

type ReadyEntry = {
  kind: "ready";
  url: string;
};

type Entry = PendingEntry | ReadyEntry;

const store = new Map<string, Map<number, Entry>>();

function bucket(projectId: string): Map<number, Entry> {
  let b = store.get(projectId);
  if (!b) {
    b = new Map();
    store.set(projectId, b);
  }
  return b;
}

/** 해당 씬에 대해 이미 프리페치 중이거나 완료되었는지. */
export function has(projectId: string, sceneId: number): boolean {
  return bucket(projectId).has(sceneId);
}

/** 프리페치 완료된 blob URL 을 꺼낸다(맵에서 제거). 없으면 null.
 *  반환된 URL 의 revoke 책임은 호출자에게 있다. */
export function take(projectId: string, sceneId: number): string | null {
  const b = bucket(projectId);
  const e = b.get(sceneId);
  if (!e || e.kind !== "ready") return null;
  b.delete(sceneId);
  return e.url;
}

/** 프로젝트별 프리페치 직렬 실행 체인.
 *
 *  프리페치를 병렬로 쏘면 안 되는 이유(실측):
 *    여러 씬이 같은 원본 footage 를 공유하는 경우가 많다. 그 씬들의 preview 요청이
 *    동시에 들어가면 서버가 같은 프록시 파일을 동시에 만들려다 서로를 덮어써
 *    "원본 변경 감지 → 재생성" 이 무한 반복되고, trim/final 캐시 해시가 매번 달라져
 *    캐시가 영원히 적중하지 않는다(씬 23 해시: 546cbb → 844f67 → 765f98).
 *    캐시 미스 1건은 최대 72초까지 걸리므로 피해가 크다.
 *
 *  따라서 프리페치는 **항상 한 번에 1건씩** 순차 실행한다. 사용자가 실제로 보고 있는
 *  현재 씬 요청(PreviewPanel 의 직접 fetch)과 경쟁하지 않게 하는 효과도 있다. */
const chains = new Map<string, Promise<void>>();

function enqueue(projectId: string, task: () => Promise<void>): void {
  const prev = chains.get(projectId) ?? Promise.resolve();
  // 앞 작업이 실패해도 체인이 끊기지 않도록 catch 로 흡수한다.
  const next = prev.then(task, task).catch(() => { /* ignore */ });
  chains.set(projectId, next);
  // 체인이 끝났고 그 사이 새 작업이 붙지 않았으면 맵에서 제거(누수 방지).
  next.then(() => {
    if (chains.get(projectId) === next) chains.delete(projectId);
  });
}

/**
 * 프리페치 1건을 **큐에 넣는다**(즉시 실행이 아니다). 이미 등록된 씬이면 아무것도 하지 않는다.
 *
 * 큐는 프로젝트별로 하나이며 동시 실행 수는 항상 1이다 — 위 `chains` 주석 참고.
 * @param fetcher AbortSignal 을 받아 Blob 을 돌려주는 함수 (getScenePreview 래핑).
 * @param onTiming 소요 시간(초) 콜백 — 성공 시에만 호출. 로딩 추정치 이동평균 갱신용.
 */
export function start(
  projectId: string,
  sceneId: number,
  fetcher: (signal: AbortSignal) => Promise<Blob>,
  onTiming?: (elapsedSec: number) => void,
): void {
  const b = bucket(projectId);
  if (b.has(sceneId)) return;

  const abort = new AbortController();
  const entry: PendingEntry = { kind: "pending", abort, cancelled: false };
  b.set(sceneId, entry);

  enqueue(projectId, () => {
    // 큐에서 꺼낼 때 이미 무효화/취소되었으면 네트워크를 쓰지 않는다.
    if (entry.cancelled || bucket(projectId).get(sceneId) !== entry) return Promise.resolve();
    return runFetch(projectId, sceneId, entry, fetcher, onTiming);
  });
}

function runFetch(
  projectId: string,
  sceneId: number,
  entry: PendingEntry,
  fetcher: (signal: AbortSignal) => Promise<Blob>,
  onTiming?: (elapsedSec: number) => void,
): Promise<void> {
  const abort = entry.abort;
  const t0 = (typeof performance !== "undefined" ? performance.now() : Date.now());
  return fetcher(abort.signal).then(
    (blob) => {
      const elapsed = ((typeof performance !== "undefined" ? performance.now() : Date.now()) - t0) / 1000;
      // 무효화/취소되었거나, 그 사이 다른 엔트리로 교체되었으면 결과를 버린다.
      const cur = bucket(projectId).get(sceneId);
      if (entry.cancelled || cur !== entry) return;
      const url = URL.createObjectURL(blob);
      bucket(projectId).set(sceneId, { kind: "ready", url });
      if (onTiming) {
        try { onTiming(elapsed); } catch { /* ignore */ }
      }
    },
    () => {
      // 실패는 조용히 무시 — 기존 경로로 폴백된다. 엔트리는 제거해 재시도 여지를 남긴다.
      const cur = bucket(projectId).get(sceneId);
      if (cur === entry) bucket(projectId).delete(sceneId);
    },
  );
}

/** 특정 씬의 프리페치를 무효화한다(편집 시 즉시 호출). ready blob 은 revoke, pending 은 abort. */
export function invalidate(projectId: string, sceneId: number): void {
  const b = bucket(projectId);
  const e = b.get(sceneId);
  if (!e) return;
  b.delete(sceneId);
  if (e.kind === "ready") {
    try { URL.revokeObjectURL(e.url); } catch { /* ignore */ }
  } else {
    e.cancelled = true;
    try { e.abort.abort(); } catch { /* ignore */ }
  }
}

/** 프로젝트의 모든 프리페치를 정리한다(이어보기 종료/씬 점프/언마운트 시). */
export function clearAll(projectId: string): void {
  const b = store.get(projectId);
  if (!b) return;
  for (const sceneId of Array.from(b.keys())) {
    invalidate(projectId, sceneId);
  }
  store.delete(projectId);
}

/** 유지할 씬 집합 외 나머지를 정리한다. 이어보기 진행에 따라 지나간 씬을 즉시 해제. */
export function retainOnly(projectId: string, keep: ReadonlySet<number>): void {
  const b = store.get(projectId);
  if (!b) return;
  for (const sceneId of Array.from(b.keys())) {
    if (!keep.has(sceneId)) invalidate(projectId, sceneId);
  }
}

/** 테스트용 — 내부 상태 초기화. revoke 는 하지 않는다(jsdom 환경). */
export function _resetForTest(): void {
  store.clear();
}
