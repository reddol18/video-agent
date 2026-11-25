// 열린 에디터가 서버(진실의 원천)의 바깥 변경을 따라가도록 하는 동기화.
//
// 배경(2026-11-05): 에이전트/스크립트가 서버 state 를 고쳐도 이미 열린 에디터는 진입 때 읽은
//   옛 store 를 그대로 들고 있었다. 그 상태로 미리보기를 누르면 loadPreview 가 옛 씬 값으로
//   PATCH 를 보내 서버 최신본을 되덮었다(같은 날 1·13번 씬이 세 번 되돌아감).
//
// 규칙 — 항목(씬 필드 / 프로젝트 필드) 단위로 판단한다.
//   prevLocal  = 직전 동기화 직후의 store 값
//   prevServer = 직전 동기화 때 받은 서버 값
//   - 서버 값이 직전 이후 바뀌었고(server ≠ prevServer)
//     로컬은 그동안 손대지 않았으면(local == prevLocal) → 서버 값을 받는다.
//   - 로컬에서 고친 항목(local ≠ prevLocal)은 그대로 둔다 — 편집 중인 작업을 덮지 않는다.
//     로컬 편집은 각 편집 액션의 patch 가 서버로 보낸다.
//   - 서버가 안 바뀌었으면 아무것도 하지 않는다(실패한 patch 로 서버에 못 간 로컬 편집 보존).
//   비교는 키 순서를 정렬한 JSON 으로 한다(같은 데이터의 키 순서 차이를 편집으로 오인하지 않게).
import type { Project, SceneCard } from "../types";

type Snap = { local: Project; server: Project };
const _snaps = new Map<string, Snap>();

function stable(v: unknown): string {
  return JSON.stringify(v, (_k, val) => {
    if (val && typeof val === "object" && !Array.isArray(val)) {
      return Object.keys(val as Record<string, unknown>).sort().reduce((acc, k) => {
        acc[k] = (val as Record<string, unknown>)[k];
        return acc;
      }, {} as Record<string, unknown>);
    }
    return val;
  });
}
const same = (a: unknown, b: unknown) => stable(a) === stable(b);
const ids = (p: Project) => (p.scenes ?? []).map((s) => s.scene_id).join(",");

function mergeObject<T extends Record<string, any>>(local: T, prevLocal: T, server: T, prevServer: T, skip: string[] = []) {
  const out: Record<string, any> = { ...local };
  let changed = false;
  const keys = new Set([...Object.keys(server), ...Object.keys(prevServer)]);
  for (const k of keys) {
    if (skip.includes(k)) continue;
    if (same(server[k], prevServer[k])) continue; // 서버 쪽 변화 없음
    if (!same(local[k], prevLocal[k])) continue; // 로컬에서 편집 중 → 보존
    if (same(local[k], server[k])) continue;
    out[k] = server[k];
    changed = true;
  }
  return { merged: out as T, changed };
}

/** 3-way 병합. 스냅샷이 없으면(첫 동기화) 서버를 그대로 채택한다 — 에디터 진입 정책과 같다. */
export function mergeServerProject(local: Project, server: Project, snap: Snap | undefined): { merged: Project; changed: boolean } {
  if (!snap) return { merged: server, changed: !same(local, server) };
  const { local: prevLocal, server: prevServer } = snap;
  // 씬 구성(번호 목록)이 서버에서 바뀌었는데 로컬은 구성을 안 바꿨으면 → 서버 씬 목록을 통째로 채택
  //   (분할·재배치 등 구조 변경은 필드 단위로 맞출 수 없다). 로컬도 구성을 바꿨으면 로컬 유지.
  const serverRestructured = ids(server) !== ids(prevServer);
  const localRestructured = ids(local) !== ids(prevLocal);
  const top = mergeObject(local as any, prevLocal as any, server as any, prevServer as any, ["scenes", "updated_at"]);
  let changed = top.changed;
  let scenes: SceneCard[] = local.scenes;
  if (serverRestructured) {
    if (!localRestructured) {
      scenes = server.scenes;
      changed = true;
    }
  } else if (!localRestructured) {
    const byId = (p: Project) => new Map((p.scenes ?? []).map((s) => [s.scene_id, s]));
    const pl = byId(prevLocal), sv = byId(server), ps = byId(prevServer);
    scenes = local.scenes.map((s) => {
      const a = pl.get(s.scene_id), b = sv.get(s.scene_id), c = ps.get(s.scene_id);
      if (!a || !b || !c) return s;
      const r = mergeObject(s as any, a as any, b as any, c as any);
      if (r.changed) changed = true;
      return r.merged as SceneCard;
    });
  }
  const merged = { ...(top.merged as Project), scenes, updated_at: (server as any).updated_at } as Project;
  return { merged, changed };
}

export function rememberSync(projectId: string, local: Project, server: Project) {
  _snaps.set(projectId, { local, server });
}

export function getSnap(projectId: string) {
  return _snaps.get(projectId);
}
