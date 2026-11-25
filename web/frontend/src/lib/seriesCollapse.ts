/**
 * 시리즈 그룹 접기/펼치기 상태를 localStorage 에 저장/조회한다.
 *
 * 키는 시리즈명 기준(group 이 name 기반이므로). 새로고침해도 유지.
 * 기본값은 펼침(false = 접히지 않음).
 */

const KEY_PREFIX = "series-collapsed:";

function keyFor(seriesName: string): string {
  return KEY_PREFIX + seriesName;
}

/** 해당 시리즈가 접힌 상태인지 조회. 기본값 펼침(false). */
export function isSeriesCollapsed(seriesName: string): boolean {
  try {
    return localStorage.getItem(keyFor(seriesName)) === "1";
  } catch {
    return false;
  }
}

/** 접기 상태 저장. collapsed=true 면 접힘, false 면 펼침(키 제거). */
export function setSeriesCollapsed(seriesName: string, collapsed: boolean): void {
  try {
    if (collapsed) {
      localStorage.setItem(keyFor(seriesName), "1");
    } else {
      localStorage.removeItem(keyFor(seriesName));
    }
  } catch {
    // localStorage 불가 환경(프라이빗 모드 등)에서는 조용히 무시.
  }
}
