import type { Project } from "../types";

/** 시리즈 미편성 그룹에 사용하는 표시명(항상 맨 아래로 정렬됨). */
export const UNGROUPED_SERIES_LABEL = "시리즈 미편성";

export interface SeriesGroup {
  /** 시리즈명. 미편성 그룹은 UNGROUPED_SERIES_LABEL. */
  series: string;
  /** 미편성(빈 series) 그룹 여부. 정렬 시 항상 맨 아래로 보낸다. */
  ungrouped: boolean;
  /** updated_at 내림차순으로 정렬된 프로젝트 목록. */
  projects: Project[];
}

function updatedAtMs(p: Project): number {
  const t = Date.parse(p.updated_at || "");
  return Number.isNaN(t) ? 0 : t;
}

/**
 * 프로젝트 목록을 수동 series 필드로 그룹핑하고 정렬한다.
 *
 * 정렬 규칙:
 * - 그룹 내부: updated_at 내림차순(최신 먼저).
 * - 그룹 간: 그룹 내 최신 updated_at 내림차순.
 * - "시리즈 미편성"(빈/공백 series) 그룹은 항상 맨 아래.
 *
 * @param projects   프로젝트 목록.
 * @param seriesNames 서버에 저장된 "빈 시리즈"까지 포함한 시리즈명 목록(선택).
 *   여기 포함되지만 어느 프로젝트의 series 값에도 없는 이름은 프로젝트 0개의
 *   빈 그룹으로 렌더된다("먼저 시리즈 만들고 나중에 채우기"). 빈 그룹도 DnD
 *   드롭 대상이 된다.
 *
 * 순수 함수 — 입력 배열을 변형하지 않는다.
 */
export function groupProjectsBySeries(
  projects: Project[],
  seriesNames: string[] = [],
): SeriesGroup[] {
  const byKey = new Map<string, Project[]>();
  // 빈 시리즈까지 먼저 키로 등록해 프로젝트 0개 그룹이 생성되게 한다.
  for (const raw of seriesNames) {
    const key = (raw || "").trim();
    if (!key) continue; // 미편성은 빈 시리즈로 등록하지 않는다.
    if (!byKey.has(key)) byKey.set(key, []);
  }
  for (const p of projects) {
    const key = (p.series || "").trim();
    const arr = byKey.get(key);
    if (arr) arr.push(p);
    else byKey.set(key, [p]);
  }

  const groups: SeriesGroup[] = [];
  for (const [key, arr] of byKey.entries()) {
    const sorted = [...arr].sort((a, b) => updatedAtMs(b) - updatedAtMs(a));
    const ungrouped = key === "";
    groups.push({
      series: ungrouped ? UNGROUPED_SERIES_LABEL : key,
      ungrouped,
      projects: sorted,
    });
  }

  groups.sort((a, b) => {
    // 미편성 그룹은 무조건 맨 아래.
    if (a.ungrouped !== b.ungrouped) return a.ungrouped ? 1 : -1;
    // 그룹 내 최신 updated_at 내림차순.
    const aLatest = a.projects.length ? updatedAtMs(a.projects[0]) : 0;
    const bLatest = b.projects.length ? updatedAtMs(b.projects[0]) : 0;
    return bLatest - aLatest;
  });

  return groups;
}
