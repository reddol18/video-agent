/**
 * 드래그&드롭 시 실제로 이동할 프로젝트 id 집합을 결정하는 순수 로직.
 *
 * 규칙(요구사항):
 * - 드래그한 카드가 멀티셀렉트(selected)에 포함되어 있으면 → 선택된 전체가 이동 대상.
 * - 포함되어 있지 않으면 → 드래그한 그 카드 하나만 이동 대상.
 *
 * 순수 함수 — 입력을 변형하지 않는다. UI(HTML5 DnD) 없이 단위 테스트 가능.
 *
 * @param draggedId 드래그를 시작한 카드의 project_id.
 * @param selected  현재 체크박스로 선택된 project_id 집합.
 * @returns 이동 대상 project_id 배열(중복 없음). draggedId 는 항상 포함된다.
 */
export function resolveDragMoveTargets(
  draggedId: string,
  selected: ReadonlySet<string>,
): string[] {
  if (selected.has(draggedId)) {
    // 선택 집합 전체 이동. draggedId 는 이미 집합에 있으므로 그대로 반환.
    return Array.from(selected);
  }
  // 선택에 없으면 그 카드만.
  return [draggedId];
}
