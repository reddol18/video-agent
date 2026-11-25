import { useEffect, useRef, useCallback } from "react";
import { wsUrl } from "../api/client";
import type { WsMessage } from "../types";

export function useWs(
  taskId: string | null,
  onMessage: (msg: WsMessage) => void,
  onTaskEnded?: () => void,  // complete/error(전체)/NOT_FOUND/예기치 않은 close 모두 포함
) {
  const wsRef = useRef<WebSocket | null>(null);
  const onMsgRef = useRef(onMessage);
  const onTaskEndedRef = useRef(onTaskEnded);
  onMsgRef.current = onMessage;
  onTaskEndedRef.current = onTaskEnded;

  useEffect(() => {
    if (!taskId) return;

    let settled = false;  // onTaskEnded를 한 번만 호출

    function end() {
      if (settled) return;
      settled = true;
      onTaskEndedRef.current?.();
    }

    const ws = new WebSocket(wsUrl(taskId));
    wsRef.current = ws;

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as WsMessage;

        if (msg.type === "error" && (msg as any).code === "NOT_FOUND") {
          end();
          return;
        }

        onMsgRef.current(msg);

        if (msg.type === "complete" || (msg.type === "error" && !("scene_id" in msg))) {
          end();
        }
      } catch {}
    };

    ws.onclose = () => {
      // cleanup의 ws.close()에 의한 호출도 이 경로를 탐 — settled로 중복 방지
      end();
    };

    return () => {
      // cleanup 시 onclose가 트리거되지만 settled=true면 end()는 no-op
      settled = true;
      ws.close();
      wsRef.current = null;
    };
  }, [taskId]);

  const cancel = useCallback(() => {
    wsRef.current?.send(JSON.stringify({ type: "cancel" }));
    wsRef.current?.close();
  }, []);

  return { cancel };
}
