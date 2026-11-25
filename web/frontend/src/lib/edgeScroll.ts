/**
 * 드래그 중 화면 가장자리 자동 스크롤 — 순수 속도 계산 로직.
 *
 * 카드를 드래그한 채 뷰포트 상/하단 가장자리(zone px) 안으로 커서가 들어오면
 * 자동으로 위/아래로 스크롤한다. 가장자리에 가까울수록 빠르게(선형 가속).
 *
 * 반환값(px/frame):
 *   음수 = 위로 스크롤, 양수 = 아래로 스크롤, 0 = 스크롤 안 함(존 밖).
 */

/** 프레임당 최대 스크롤 속도(px). 60fps 기준 약 22px/frame. */
export const MAX_EDGE_SCROLL_SPEED = 22;

/**
 * @param pointerY      뷰포트 기준 커서 Y 좌표(clientY)
 * @param viewportHeight 뷰포트 높이(px)
 * @param zone          상/하단 각각의 활성 존 높이(px, 기본 80)
 * @param maxSpeed      존 가장자리에서의 최대 속도(px/frame)
 * @returns 프레임당 스크롤 델타(px). 위=음수, 아래=양수, 존 밖=0.
 */
export function edgeScrollSpeed(
  pointerY: number,
  viewportHeight: number,
  zone = 80,
  maxSpeed = MAX_EDGE_SCROLL_SPEED,
): number {
  // 방어적 처리: 비정상 입력이면 스크롤 없음.
  if (!Number.isFinite(pointerY) || !Number.isFinite(viewportHeight) || viewportHeight <= 0) {
    return 0;
  }
  const z = Math.max(1, zone);

  // 상단 존: pointerY 가 0(맨 위)에 가까울수록 빠르게 위로.
  if (pointerY < z) {
    const intensity = (z - Math.max(0, pointerY)) / z; // 0~1
    return -maxSpeed * intensity;
  }
  // 하단 존: pointerY 가 viewportHeight(맨 아래)에 가까울수록 빠르게 아래로.
  const distFromBottom = viewportHeight - pointerY;
  if (distFromBottom < z) {
    const intensity = (z - Math.max(0, distFromBottom)) / z; // 0~1
    return maxSpeed * intensity;
  }
  return 0;
}
