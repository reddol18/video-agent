import { describe, it, expect } from "vitest";
import { groupProjectsBySeries, UNGROUPED_SERIES_LABEL } from "../groupProjectsBySeries";
import type { Project } from "../../types";

function mk(id: string, series: string, updatedAt: string): Project {
  return {
    project_id: id,
    name: id,
    series,
    footage_dir: "",
    scenario_text: "",
    status: "setup",
    scenes: [],
    tts_voice: "",
    created_at: updatedAt,
    updated_at: updatedAt,
    studio_assets: [],
    output_width: 1920,
    output_height: 1080,
  };
}

describe("groupProjectsBySeries", () => {
  it("빈/공백 series는 '시리즈 미편성' 그룹으로 묶고 맨 아래에 둔다", () => {
    const groups = groupProjectsBySeries([
      mk("a", "", "2026-01-01T00:00:00Z"),
      mk("b", "여행", "2026-01-02T00:00:00Z"),
      mk("c", "   ", "2026-01-03T00:00:00Z"),
    ]);
    const last = groups[groups.length - 1];
    expect(last.series).toBe(UNGROUPED_SERIES_LABEL);
    expect(last.ungrouped).toBe(true);
    // 빈 문자열과 공백은 같은 미편성 그룹에 묶인다.
    expect(last.projects.map((p) => p.project_id).sort()).toEqual(["a", "c"]);
  });

  it("그룹 내부는 updated_at 내림차순으로 정렬한다", () => {
    const groups = groupProjectsBySeries([
      mk("old", "여행", "2026-01-01T00:00:00Z"),
      mk("new", "여행", "2026-03-01T00:00:00Z"),
      mk("mid", "여행", "2026-02-01T00:00:00Z"),
    ]);
    expect(groups[0].projects.map((p) => p.project_id)).toEqual(["new", "mid", "old"]);
  });

  it("그룹들은 그룹 내 최신 updated_at 내림차순으로 정렬한다(미편성 제외)", () => {
    const groups = groupProjectsBySeries([
      mk("x", "A", "2026-01-05T00:00:00Z"),
      mk("y", "B", "2026-01-10T00:00:00Z"),
      mk("z", "", "2026-12-31T00:00:00Z"), // 미편성이 가장 최신이어도 맨 아래
    ]);
    expect(groups.map((g) => g.series)).toEqual(["B", "A", UNGROUPED_SERIES_LABEL]);
  });

  it("series 필드가 없는 프로젝트도 미편성으로 처리한다", () => {
    const p = mk("noseries", "", "2026-01-01T00:00:00Z");
    delete (p as { series?: string }).series;
    const groups = groupProjectsBySeries([p]);
    expect(groups).toHaveLength(1);
    expect(groups[0].ungrouped).toBe(true);
    expect(groups[0].projects[0].project_id).toBe("noseries");
  });

  it("빈 시리즈(프로젝트 0개)도 그룹으로 렌더한다(드롭 대상)", () => {
    const groups = groupProjectsBySeries(
      [mk("a", "여행", "2026-01-01T00:00:00Z")],
      ["여행", "요리"], // '요리'는 프로젝트가 없는 빈 시리즈
    );
    const cooking = groups.find((g) => g.series === "요리");
    expect(cooking).toBeTruthy();
    expect(cooking!.projects).toHaveLength(0);
    expect(cooking!.ungrouped).toBe(false);
  });

  it("빈 시리즈 목록과 프로젝트 series 값을 합집합으로 그룹핑한다", () => {
    const groups = groupProjectsBySeries(
      [mk("a", "여행", "2026-01-01T00:00:00Z")],
      ["요리"],
    );
    expect(groups.map((g) => g.series).sort()).toEqual(["여행", "요리"]);
  });

  it("빈 시리즈 목록에 공백/미편성 표시명은 빈 그룹으로 만들지 않는다", () => {
    const groups = groupProjectsBySeries([], ["  ", ""]);
    expect(groups).toHaveLength(0);
  });

  it("seriesNames 미전달 시 기존 동작(프로젝트 series 기준)과 동일하다", () => {
    const groups = groupProjectsBySeries([mk("a", "여행", "2026-01-01T00:00:00Z")]);
    expect(groups.map((g) => g.series)).toEqual(["여행"]);
  });
});
