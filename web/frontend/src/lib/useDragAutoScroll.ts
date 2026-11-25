import { useEffect, useRef } from "react";
import { edgeScrollSpeed } from "./edgeScroll";

/**
 * 드래그 중 window 자동 스크롤 훅.
 *
 * 카드 드래그(dragstart)로 활성화된 뒤, 커서가 뷰포트 상/하단 가장자리에 오면
 * requestAnimationFrame 루프로 window 를 자동 스크롤한다. drop/dragend 로 정지.
 *
 * 홈 화면은 window(브라우저) 스크롤 구조(styles.container: minHeight 100vh, overflow 없음)
 * 이므로 스크롤 대상은 window 로 고정한다.
 *
 * - 드래그가 아닐 때(일반 스크롤)는 절대 발동하지 않는다: dragover 이벤트에만 반응하고,
 *   dragMimeGuard 로 카드 드래그(DND_PROJECT_MIME)만 대상으로 삼는다.
 * - 언마운트/드래그 종료 시 rAF·이벤트 리스너를 모두 정리한다(누수 방지).
 *
 * @param dragMimeGuard 드래그 dataTransfer.types 를 받아 자동스크롤 대상 여부 반환.
 *                      (예: types.includes(DND_PROJECT_MIME))
 * @param zone          상/하단 활성 존 높이(px). 기본 80.
 */
export function useDragAutoScroll(
  dragMimeGuard: (types: readonly string[]) => boolean,
  zone = 80,
): void {
  // 최신 speed 를 rAF 루프에 전달하기 위한 ref.
  const speedRef = useRef(0);
  const rafRef = useRef<number | null>(null);
  const guardRef = useRef(dragMimeGuard);
  guardRef.current = dragMimeGuard;

  useEffect(() => {
    function stopLoop() {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      speedRef.current = 0;
    }

    function tick() {
      const speed = speedRef.current;
      if (speed !== 0) {
        window.scrollBy(0, speed);
        rafRef.current = requestAnimationFrame(tick);
      } else {
        rafRef.current = null;
      }
    }

    function handleDragOver(e: DragEvent) {
      // dataTransfer 가 없거나 카드 드래그가 아니면 자동스크롤 대상 아님.
      const types = e.dataTransfer?.types ?? [];
      if (!guardRef.current(types)) {
        speedRef.current = 0;
        return;
      }
      const speed = edgeScrollSpeed(e.clientY, window.innerHeight, zone);
      speedRef.current = speed;
      // 존 안이고 루프가 멈춰 있으면 시작.
      if (speed !== 0 && rafRef.current === null) {
        rafRef.current = requestAnimationFrame(tick);
      }
    }

    // 드래그 종료(drop/dragend/드래그 취소)에서 루프 정지.
    window.addEventListener("dragover", handleDragOver);
    window.addEventListener("drop", stopLoop);
    window.addEventListener("dragend", stopLoop);

    return () => {
      window.removeEventListener("dragover", handleDragOver);
      window.removeEventListener("drop", stopLoop);
      window.removeEventListener("dragend", stopLoop);
      stopLoop();
    };
  }, [zone]);
}
