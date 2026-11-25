/**
 * NlEditorPanel — 자연어 편집 채팅 패널 (#316)
 *
 * Editor 우측에 부착되는 사이드 패널. 사용자가 자연어 명령("3번 씬 글자 더 크게")을
 * 입력하면 백엔드 `POST /nl-edit` 으로 patch 미리보기를 받고, 사용자가 적용을 누르면
 * `/nl-edit/apply` 로 실제 RenderSpec 을 갱신한다.
 *
 * 흐름
 * ----
 * 1) 사용자가 textarea 에 명령 입력 → Enter (Shift+Enter 줄바꿈) 또는 전송 버튼.
 * 2) "user" 메시지를 채팅 히스토리에 즉시 push, 동시에 "ai" 자리표시(pending) 메시지도 push.
 * 3) `previewNlEdit` 호출 → 응답을 ai 메시지에 채워 넣음. 실패면 error 필드 설정.
 * 4) 사용자가 ai 메시지의 PatchPreview "적용" 클릭 → safety_flags 있으면 확인 다이얼로그,
 *    승인되면 `applyNlEdit` 호출 → ai 메시지에 `applied=true, snapshot_id` 기록 + Editor 갱신.
 * 5) 적용된 메시지의 "되돌리기" 클릭 → `undoNlEdit` 호출 → ai 메시지에 `undone=true` 기록 + Editor 갱신.
 *
 * 영향 씬 하이라이트
 * ----------------
 * 현재 입력 박스 위에 떠있는 *가장 최근 미리보기 카드*가 가리키는 씬 번호를 store 에
 * `highlightedSceneIds`(1-based) 로 등록한다. Editor 의 씬 카드가 이를 읽어 테두리 강조.
 * 적용/거부/되돌리기 후에는 자동 해제.
 *
 * 세션 영속화
 * ----------
 * 모든 채팅 턴은 IndexedDB `nlEditChat` store 에 즉시 저장된다. 패널을 닫았다가 다시 열거나
 * 새로고침해도 동일 프로젝트 내 히스토리는 보존.
 *
 * 동시성
 * ------
 * 백엔드는 같은 프로젝트에 동시 nl-edit 요청을 409 로 거부한다. 본 컴포넌트는 호출 중
 * 입력창을 비활성화해 사용자가 명령을 연속 입력하지 못하게 한다.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyNlEdit,
  previewNlEdit,
  releaseNlEditCache,
  undoNlEdit,
  type JsonPatchOp,
  type NlEditTurn,
} from "../api/nlEdit";
import {
  dbClearNlEditChat,
  dbListNlEditChat,
  dbSaveNlEditChatTurn,
  newNlEditTurnId,
  nlEditChatKey,
  type NlEditChatTurnRecord,
} from "../lib/db";
import { extractAffectedSceneIndices } from "../lib/jsonPatchToHuman";
import { updateScene as dbUpdateScene, splitSceneAt as dbSplitSceneAt } from "../lib/project";
import { patchScene } from "../api/client";
import { useStore } from "../store";
import { ui } from "../store/ui";
import type { SceneCard } from "../types";
import PatchPreview from "./PatchPreview";

interface NlEditorPanelProps {
  projectId: string;
  /** 패널 닫기 — Editor 가 상태를 보유. */
  onClose: () => void;
  /** apply/undo 직후 Editor 가 IndexedDB 에서 프로젝트를 다시 로드하도록 알린다. */
  onAfterApply?: () => void;
}

// 멀티턴 컨텍스트로 LLM 에 보내는 최근 N개의 *완료된 사이클*. 한 사이클 = (user 명령 +
// applied=true ai 응답). #337 — 과거에는 user/ai 메시지를 각각 별도 NlEditTurn 으로 보내
// 실질 사이클 수가 절반(3)에 불과했다. 이제는 NlEditTurn 1개 = 사이클 1개로 통일.
const MAX_HISTORY_TURNS_FOR_LLM = 6;

export default function NlEditorPanel({
  projectId,
  onClose,
  onAfterApply,
}: NlEditorPanelProps) {
  const setHighlightedSceneIds = useStore((s) => s.setHighlightedSceneIds);
  const bumpNlEditApplyTick = useStore((s) => s.bumpNlEditApplyTick);
  // #334 — patch 의 `/scenes/{idx}` 인덱스를 SceneCard.scene_id 로 매핑할 때 사용한다.
  // 씬을 삭제/재배치한 후에는 scene_id 가 1, 3, 4 처럼 비연속이 되므로 "idx+1" 만으로는
  // Editor.tsx 의 `highlightedSceneIds.includes(scene.scene_id)` 비교가 어긋난다.
  const project = useStore((s) => s.project);
  // #335 — apply/undo 응답의 scene_cards 로 zustand store 와 IndexedDB Project.scenes 를
  // 갱신해 후속 액션(footage 드롭, narration 수정 등) 에서 NL 편집 변경 사항이 SceneCard
  // 기반 RenderSpec 재빌드로 *덮어써져 사라지는* 문제를 차단한다.
  const setProject = useStore((s) => s.setProject);
  // 이벤트 기반 자동 씬 분할(NL 편집 split 분기)에 현재 선택 씬을 백엔드로 전달.
  const selectedSceneId = useStore((s) => s.selectedSceneId);

  // ── 채팅 히스토리 (IndexedDB 영속) ────────────────────────────────────
  const [turns, setTurns] = useState<NlEditChatTurnRecord[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);

  // ── 입력 ──────────────────────────────────────────────────────────────
  const [input, setInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [applyingTurnId, setApplyingTurnId] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // ── safety 확인 다이얼로그 ────────────────────────────────────────────
  // 적용 클릭 시 safety_flags 가 있으면 ai 메시지를 여기에 보관해 모달을 띄운다.
  const [pendingSafetyTurn, setPendingSafetyTurn] = useState<NlEditChatTurnRecord | null>(null);

  // ── 히스토리 로드 (마운트 시 1회) ─────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    setLoadingHistory(true);
    dbListNlEditChat(projectId)
      .then((rows) => {
        if (cancelled) return;
        setTurns(rows);
      })
      .catch((e) => {
        // 히스토리 로드 실패는 빈 상태로 시작 — 입력은 가능해야 한다.
        // eslint-disable-next-line no-console
        console.warn("[nlEditor] 히스토리 로드 실패", e);
        if (!cancelled) setTurns([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // ── #317 — 패널 언마운트 시 컨텍스트 캐시 해제 ─────────────────────────
  // 캐시 저장 비용($1.00/M/h)을 절감하기 위해 명시 삭제. TTL 자동 만료(1시간)도 안전망.
  useEffect(() => {
    return () => {
      // 비동기 해제 — 반환 전에 결과를 기다리지 않는다. 실패해도 TTL 로 정리됨.
      void releaseNlEditCache(projectId);
    };
  }, [projectId]);

  // ── highlight 동기화 ──────────────────────────────────────────────────
  // 마지막으로 *미적용/미거부* 상태로 떠있는 ai 메시지의 patch 가 영향 씬을 결정.
  // 적용/거부 후에는 자동으로 빈 배열로 돌아가 씬 카드 강조가 사라진다.
  const liveAiTurn = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i--) {
      const t = turns[i];
      if (t.role !== "ai") continue;
      if (t.applied || t.rejected || t.undone || t.pending || t.error) continue;
      return t;
    }
    return null;
  }, [turns]);

  useEffect(() => {
    if (liveAiTurn && Array.isArray(liveAiTurn.patch)) {
      const indices = extractAffectedSceneIndices(liveAiTurn.patch as JsonPatchOp[]);
      // #334 — RenderSpec scenes 배열은 SceneCard 와 *같은 순서* (scene_id 오름차순) 로
      // 빌드되므로(`web/backend/routers/nl_editor.py:_load_render_spec_payload` 참고),
      // 인덱스를 그대로 사용해 정렬된 SceneCard 의 scene_id 로 매핑한다.
      const sortedScenes = (project?.scenes ?? [])
        .slice()
        .sort((a, b) => a.scene_id - b.scene_id);
      const sceneIds = indices
        .map((i) => sortedScenes[i]?.scene_id)
        .filter((id): id is number => typeof id === "number");
      setHighlightedSceneIds(sceneIds);
    } else {
      setHighlightedSceneIds([]);
    }
    // 컴포넌트 언마운트 시 해제
    return () => setHighlightedSceneIds([]);
  }, [liveAiTurn, project?.scenes, setHighlightedSceneIds]);

  // ── 채팅 자동 스크롤 ──────────────────────────────────────────────────
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [turns]);

  // ── 턴 저장 헬퍼 (IndexedDB + state) ──────────────────────────────────
  const upsertTurn = useCallback(
    async (turn: NlEditChatTurnRecord) => {
      await dbSaveNlEditChatTurn(turn);
      setTurns((prev) => {
        const idx = prev.findIndex((t) => t.key === turn.key);
        if (idx >= 0) {
          const next = prev.slice();
          next[idx] = turn;
          return next;
        }
        return [...prev, turn];
      });
    },
    [],
  );

  // ── 전송 ──────────────────────────────────────────────────────────────
  const sendInstruction = useCallback(async () => {
    const trimmed = input.trim();
    if (!trimmed || submitting) return;

    setSubmitting(true);

    // 1) 사용자 메시지 저장
    const userTurnId = newNlEditTurnId();
    const now = () => new Date().toISOString();
    const userRecord: NlEditChatTurnRecord = {
      key: nlEditChatKey(projectId, userTurnId),
      project_id: projectId,
      turn_id: userTurnId,
      role: "user",
      content: trimmed,
      created_at: now(),
    };
    await upsertTurn(userRecord);

    // 2) AI pending 자리표시
    const aiTurnId = newNlEditTurnId();
    const aiPending: NlEditChatTurnRecord = {
      key: nlEditChatKey(projectId, aiTurnId),
      project_id: projectId,
      turn_id: aiTurnId,
      role: "ai",
      content: "변경 사항을 계산 중…",
      created_at: now(),
      pending: true,
    };
    await upsertTurn(aiPending);

    // 3) 입력창 비우기
    setInput("");

    // 4) LLM 컨텍스트로 보낼 최근 턴 (user + applied AI) — pending/error 제외
    //
    // #337 — 백엔드 NlEditTurn(`instruction`/`summary`/`applied`) 은 *한 사이클* 을 표현하는
    // 통합 모델이다. user 와 ai 메시지를 각각 별도 NlEditTurn 으로 보내면 빈 instruction
    // 또는 빈 summary 가 섞여 LLM 컨텍스트 정합성이 망가진다.
    // 여기서는 채팅 히스토리를 (user → 직후 ai 응답) 쌍으로 묶어 한 NlEditTurn 으로 합친다.
    // 다음 명령("그거 더 크게")이 직전 사이클을 정확히 가리킬 수 있다.
    //
    // 포함 규칙
    //  - applied=true 인 ai 응답이 있는 사이클만 포함 (미적용 응답은 의미 없는 노이즈).
    //  - pending/error 상태의 ai 응답은 제외 (정상적으로 완료된 사이클만).
    //  - applied 사이클이 N개 미만이면 최근 N개만큼만 보낸다 (`slice(-N)`).
    const pairs: NlEditTurn[] = [];
    for (let i = 0; i < turns.length; i++) {
      const u = turns[i];
      if (u.role !== "user") continue;
      // 직후의 첫 ai 응답을 찾는다 (정상 완료된 것만).
      let a: NlEditChatTurnRecord | undefined;
      for (let j = i + 1; j < turns.length; j++) {
        const t = turns[j];
        if (t.role === "user") break; // 다음 user 가 나오면 이 user 는 대응 ai 없이 끝남
        if (t.role === "ai" && !t.pending && !t.error) {
          a = t;
          break;
        }
      }
      // applied 사이클만 LLM 컨텍스트로 채택 (미적용/거부/되돌리기 사이클은 노이즈)
      if (a && a.applied && !a.rejected && !a.undone) {
        pairs.push({
          instruction: u.content,
          summary: a.content,
          applied: true,
        });
      }
    }
    const ctx: NlEditTurn[] = pairs.slice(-MAX_HISTORY_TURNS_FOR_LLM);

    try {
      const resp = await previewNlEdit(projectId, trimmed, ctx, selectedSceneId);
      const filled: NlEditChatTurnRecord = {
        ...aiPending,
        content: resp.summary || "(요약 없음)",
        patch: resp.patch as unknown[],
        safety_flags: resp.safety_flags,
        estimated_cost_usd: resp.estimated_cost_usd,
        pending: false,
        // 이벤트 기반 자동 씬 분할 응답이면 결과를 별도 필드에 보관 — 승인 시 splitSceneAt 호출.
        split_result: resp.kind === "split" && resp.split ? resp.split : null,
        // 이벤트 기반 자동 오버레이 응답 — 승인 시 applyScenePatch(text_overlays 머지) 호출.
        overlay_result: resp.kind === "overlay" && resp.overlay ? resp.overlay : null,
      };
      await upsertTurn(filled);
    } catch (e: unknown) {
      const err = e as { response?: { status?: number; data?: { detail?: string } }; message?: string };
      const status = err.response?.status;
      const detail = err.response?.data?.detail ?? err.message ?? "알 수 없는 오류";
      const friendly =
        status === 422
          ? `명령을 이해하지 못했어요. 더 구체적으로 알려주세요. (${detail})`
          : status === 503
          ? `AI 호출에 실패했어요. 잠시 후 다시 시도해 주세요. (${detail})`
          : status === 409
          ? `같은 프로젝트에 다른 자연어 편집이 진행 중입니다. 잠시 후 다시 시도해 주세요.`
          : `요청 실패: ${detail}`;
      const failed: NlEditChatTurnRecord = {
        ...aiPending,
        content: friendly,
        pending: false,
        error: detail,
      };
      await upsertTurn(failed);
    } finally {
      setSubmitting(false);
      // 자동으로 입력창 포커스 — 후속 명령 빠르게 입력
      requestAnimationFrame(() => textareaRef.current?.focus());
    }
  }, [input, projectId, selectedSceneId, submitting, turns, upsertTurn]);

  // ── #335 SceneCard 역동기화 헬퍼 ──────────────────────────────────────
  // 백엔드 apply/undo 응답의 `scene_cards` 를 받아 zustand store 와 IndexedDB
  // Project.scenes 를 갱신한다. 이로써 NL 편집 결과(text/shape/graphic/mosaic +
  // footage_speed) 가 SceneCard 차원에서 단일 진실의 원천이 된다. footage 드롭/
  // narration 수정 등 후속 액션이 SceneCard 기반으로 RenderSpec 을 재빌드해도
  // NL 편집 변경 사항이 *유지*된다.
  //
  // 변환 불가 필드(filters / motions / narration_text / footage / match / tts 등) 는
  // 백엔드 sync_scene_cards_from_render_spec 가 기존 SceneCard 값을 그대로 보존한다
  // — 손실 없이 안전하게 머지 가능.
  const applySceneCardsToLocal = useCallback(
    async (sceneCards: unknown[] | undefined) => {
      if (!sceneCards || sceneCards.length === 0) return;
      // 1) zustand 즉시 갱신 — UI(씬 카드 효과 칩 등) 가 즉시 반영되도록.
      const currentProject = useStore.getState().project;
      if (currentProject && currentProject.project_id === projectId) {
        const incomingById = new Map<number, SceneCard>();
        for (const raw of sceneCards) {
          const sc = raw as SceneCard;
          if (sc && typeof sc.scene_id === "number") {
            incomingById.set(sc.scene_id, sc);
          }
        }
        const mergedScenes = currentProject.scenes.map((sc) => {
          const incoming = incomingById.get(sc.scene_id);
          if (!incoming) return sc;
          // tts 의 blob: URL 은 IndexedDB 복원 후만 유효하므로 기존 store 값을 우선.
          // 백엔드는 tts 메타를 알지 못해 빈 값을 줄 수 있으므로 신중히 보존.
          return {
            ...sc,
            ...incoming,
            tts: sc.tts ?? incoming.tts ?? null,
          };
        });
        setProject({ ...currentProject, scenes: mergedScenes });
      }
      // 2) IndexedDB 갱신 — 새로고침/다른 페이지 복귀 후에도 NL 편집 결과 유지.
      // updateScene 은 한 번에 한 씬만 처리하므로 직렬로 순회.
      for (const raw of sceneCards) {
        const sc = raw as SceneCard;
        if (sc && typeof sc.scene_id === "number") {
          // 변환 가능 필드만 patch — tts/footage/match/narration_text/filters/motions 등
          // 변환 불가 필드는 IndexedDB 의 기존 값이 더 신뢰 가능하므로 패치하지 않는다.
          await dbUpdateScene(projectId, sc.scene_id, {
            text_overlays: sc.text_overlays ?? [],
            shape_overlays: sc.shape_overlays ?? [],
            graphic_overlays: sc.graphic_overlays ?? [],
            mosaic_overlays: sc.mosaic_overlays ?? [],
            footage_speed: sc.footage_speed,
            footage_speed_mode: sc.footage_speed_mode,
            footage_speed_ramp_hold: sc.footage_speed_ramp_hold,
            footage_speed_ramp_accel: sc.footage_speed_ramp_accel,
            footage_speed_ramp_base: sc.footage_speed_ramp_base,
          }).catch((e) => {
            // eslint-disable-next-line no-console
            console.warn(`[nlEditor] IndexedDB 갱신 실패 (씬 ${sc.scene_id})`, e);
          });
        }
      }
    },
    [projectId, setProject],
  );

  // ── 적용 처리 (safety 통과 후) ────────────────────────────────────────
  const performApply = useCallback(
    async (turn: NlEditChatTurnRecord) => {
      // 이벤트 기반 자동 오버레이 응답 — 기존 text_overlays에 머지 후 patchScene.
      if (turn.overlay_result && turn.overlay_result.overlays.length > 0) {
        setApplyingTurnId(turn.turn_id);
        try {
          const { scene_id, overlays } = turn.overlay_result;
          const cur = project?.scenes.find((s) => s.scene_id === scene_id);
          const existing = (cur?.text_overlays ?? []) as unknown[];
          // OverlayItem(백엔드 모델)을 그대로 TextOverlay로 사용 — 필드 호환됨.
          const merged = [...existing, ...overlays];
          // store + IndexedDB + 서버 모두 갱신
          if (cur) {
            const updated = { ...cur, text_overlays: merged as typeof cur.text_overlays };
            if (project) {
              setProject({
                ...project,
                scenes: project.scenes.map((s) => (s.scene_id === scene_id ? updated : s)),
              });
            }
          }
          await dbUpdateScene(projectId, scene_id, { text_overlays: merged as never });
          await patchScene(projectId, scene_id, { text_overlays: merged as never }).catch(() => {});
          await upsertTurn({ ...turn, applied: true, undone: false, rejected: false });
          bumpNlEditApplyTick();
          onAfterApply?.();
        } catch (e: unknown) {
          const err = e as { response?: { status?: number; data?: { detail?: string } }; message?: string };
          const detail = err.response?.data?.detail ?? err.message ?? "알 수 없는 오류";
          await upsertTurn({ ...turn, error: `오버레이 적용 실패: ${detail}` });
        } finally {
          setApplyingTurnId(null);
        }
        return;
      }
      // 이벤트 기반 자동 씬 분할 응답이면 별도 경로(splitSceneAt). patch 없이도 처리.
      if (turn.split_result && turn.split_result.cuts_sec.length > 0) {
        setApplyingTurnId(turn.turn_id);
        try {
          const { scene_id, cuts_sec, scene_duration_sec } = turn.split_result;
          // 백엔드가 알려준 씬 길이로 clamp(누적 분할 시 baseEnd 초과 방지).
          const updated = await dbSplitSceneAt(projectId, scene_id, cuts_sec, scene_duration_sec ?? undefined);
          // 분할은 store 전체 갱신이 안전 — 새 씬 ID들이 추가됨.
          setProject(updated.project);
          await upsertTurn({ ...turn, applied: true, undone: false, rejected: false });
          bumpNlEditApplyTick();
          onAfterApply?.();
        } catch (e: unknown) {
          const err = e as { response?: { status?: number; data?: { detail?: string } }; message?: string };
          const detail = err.response?.data?.detail ?? err.message ?? "알 수 없는 오류";
          await upsertTurn({ ...turn, error: `분할 적용 실패: ${detail}` });
        } finally {
          setApplyingTurnId(null);
        }
        return;
      }
      const patch = (turn.patch ?? []) as JsonPatchOp[];
      if (patch.length === 0) return;
      setApplyingTurnId(turn.turn_id);
      try {
        // 사용자 명령(직전 user 턴)을 meta 로 함께 전송 — 감사 로그용
        const prevUser = [...turns].reverse().find(
          (t) => t.role === "user" && t.turn_id < turn.turn_id,
        );
        const resp = await applyNlEdit(projectId, patch, {
          instruction: prevUser?.content ?? "",
          summary: turn.content,
        });
        await upsertTurn({
          ...turn,
          applied: true,
          undone: false,
          rejected: false,
          snapshot_id: resp.snapshot_id,
        });
        // #335 — 백엔드가 돌려준 SceneCard 역동기화 결과로 store + IndexedDB 갱신.
        // (응답 이전에 bumpNlEditApplyTick → preview-cache 삭제가 일어나도 안전 —
        //  preview 재생성은 갱신된 SceneCard 기준으로 진행됨.)
        await applySceneCardsToLocal(resp.scene_cards);
        bumpNlEditApplyTick();
        onAfterApply?.();
      } catch (e: unknown) {
        const err = e as { response?: { status?: number; data?: { detail?: string } }; message?: string };
        const detail = err.response?.data?.detail ?? err.message ?? "알 수 없는 오류";
        // 적용 실패는 메시지를 error 로 남기되 미적용 상태 유지 (재시도 가능).
        await upsertTurn({ ...turn, error: detail });
      } finally {
        setApplyingTurnId(null);
      }
    },
    [applySceneCardsToLocal, bumpNlEditApplyTick, onAfterApply, project, projectId, setProject, turns, upsertTurn],
  );

  // ── 적용 버튼 — safety_flags 있으면 모달 열기 ─────────────────────────
  const onApplyClick = useCallback(
    (turn: NlEditChatTurnRecord) => {
      const flags = turn.safety_flags ?? [];
      if (flags.length > 0) {
        setPendingSafetyTurn(turn);
        return;
      }
      void performApply(turn);
    },
    [performApply],
  );

  // ── 거부 ──────────────────────────────────────────────────────────────
  const onRejectClick = useCallback(
    async (turn: NlEditChatTurnRecord) => {
      await upsertTurn({ ...turn, rejected: true });
    },
    [upsertTurn],
  );

  // ── 수정 요청 — 입력창에 user 메시지 prefill ─────────────────────────
  const onReviseClick = useCallback(
    (turn: NlEditChatTurnRecord) => {
      const prevUser = [...turns].reverse().find(
        (t) => t.role === "user" && t.turn_id < turn.turn_id,
      );
      const seed = prevUser?.content ?? "";
      setInput(seed);
      requestAnimationFrame(() => {
        const ta = textareaRef.current;
        if (ta) {
          ta.focus();
          ta.setSelectionRange(seed.length, seed.length);
        }
      });
    },
    [turns],
  );

  // ── 되돌리기 ──────────────────────────────────────────────────────────
  const onUndoClick = useCallback(
    async (turn: NlEditChatTurnRecord) => {
      setApplyingTurnId(turn.turn_id);
      try {
        const resp = await undoNlEdit(projectId);
        await upsertTurn({ ...turn, undone: true });
        // #335 — undo 결과로도 SceneCard 를 함께 롤백 (apply 와 대칭).
        await applySceneCardsToLocal(resp.scene_cards);
        bumpNlEditApplyTick();
        onAfterApply?.();
      } catch (e: unknown) {
        const err = e as { response?: { status?: number; data?: { detail?: string } }; message?: string };
        const detail = err.response?.data?.detail ?? err.message ?? "알 수 없는 오류";
        await upsertTurn({ ...turn, error: detail });
      } finally {
        setApplyingTurnId(null);
      }
    },
    [applySceneCardsToLocal, bumpNlEditApplyTick, onAfterApply, projectId, upsertTurn],
  );

  // ── 대화 비우기 ───────────────────────────────────────────────────────
  const onClearChat = useCallback(async () => {
    if (turns.length === 0) return;
    const ok = await ui.confirm({
      title: "대화 기록 비우기",
      message: "이 프로젝트의 자연어 편집 대화 기록을 모두 비웁니다.\n적용된 변경 사항은 그대로 유지됩니다.",
      variant: "warning",
      confirmLabel: "비우기",
    });
    if (!ok) return;
    await dbClearNlEditChat(projectId);
    setTurns([]);
    setHighlightedSceneIds([]);
  }, [projectId, setHighlightedSceneIds, turns.length]);

  // ── safety 확인 다이얼로그 핸들러 ─────────────────────────────────────
  const onConfirmSafety = useCallback(async () => {
    if (!pendingSafetyTurn) return;
    const t = pendingSafetyTurn;
    setPendingSafetyTurn(null);
    await performApply(t);
  }, [pendingSafetyTurn, performApply]);

  const onCancelSafety = useCallback(() => {
    setPendingSafetyTurn(null);
  }, []);

  // ── 키보드 핸들러 ─────────────────────────────────────────────────────
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        void sendInstruction();
      }
    },
    [sendInstruction],
  );

  // ── 렌더 ──────────────────────────────────────────────────────────────
  return (
    <aside style={styles.panel} aria-label="자연어 편집 패널">
      <div style={styles.header}>
        <div>
          <div style={styles.title}>🤖 AI 편집 도우미</div>
          <div style={styles.subtitle}>자연어로 영상을 수정하세요</div>
        </div>
        <div style={styles.headerButtons}>
          <button
            type="button"
            onClick={onClearChat}
            style={styles.headerBtn}
            disabled={turns.length === 0}
            title="이 프로젝트의 대화 기록 비우기"
          >
            대화 비우기
          </button>
          <button
            type="button"
            onClick={onClose}
            style={styles.headerBtn}
            title="패널 닫기"
          >
            ✕
          </button>
        </div>
      </div>

      <div style={styles.chatScroll} ref={scrollRef}>
        {loadingHistory ? (
          <div style={styles.placeholderText}>대화 기록을 불러오는 중…</div>
        ) : turns.length === 0 ? (
          <div style={styles.emptyState}>
            <div style={styles.emptyTitle}>무엇을 바꿔드릴까요?</div>
            <div style={styles.emptyHints}>
              예시
              <ul style={styles.exampleList}>
                <li>"3번 씬의 글자 크기를 더 크게"</li>
                <li>"마지막 씬을 더 어둡게"</li>
                <li>"2번 씬에 빨간 사각형 추가"</li>
                <li>"전체 자막 색깔을 노란색으로"</li>
              </ul>
            </div>
          </div>
        ) : (
          turns.map((turn) => <TurnBubble
            key={turn.key}
            turn={turn}
            busy={applyingTurnId === turn.turn_id}
            onApply={() => onApplyClick(turn)}
            onReject={() => onRejectClick(turn)}
            onRevise={() => onReviseClick(turn)}
            onUndo={() => onUndoClick(turn)}
          />)
        )}
      </div>

      <div style={styles.inputArea}>
        <textarea
          ref={textareaRef}
          style={styles.textarea}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={submitting ? "AI가 응답 중입니다…" : "자연어로 수정 명령을 입력하세요 (Enter 전송, Shift+Enter 줄바꿈)"}
          rows={3}
          disabled={submitting}
        />
        <div style={styles.inputRow}>
          <div style={styles.inputHint}>
            {submitting ? "응답을 기다리는 중…" : `${input.length}/1000자`}
          </div>
          <button
            type="button"
            onClick={() => void sendInstruction()}
            disabled={submitting || input.trim().length === 0}
            style={{
              ...styles.sendBtn,
              ...(submitting || input.trim().length === 0 ? styles.sendBtnDisabled : {}),
            }}
          >
            {submitting ? "전송 중…" : "전송 ↵"}
          </button>
        </div>
      </div>

      {pendingSafetyTurn && (
        <SafetyConfirmDialog
          flags={pendingSafetyTurn.safety_flags ?? []}
          summary={pendingSafetyTurn.content}
          onConfirm={onConfirmSafety}
          onCancel={onCancelSafety}
        />
      )}
    </aside>
  );
}

// ── 채팅 1턴 버블 ─────────────────────────────────────────────────────────

function TurnBubble({
  turn,
  busy,
  onApply,
  onReject,
  onRevise,
  onUndo,
}: {
  turn: NlEditChatTurnRecord;
  busy: boolean;
  onApply: () => void;
  onReject: () => void;
  onRevise: () => void;
  onUndo: () => void;
}) {
  if (turn.role === "user") {
    return (
      <div style={styles.userRow}>
        <div style={styles.userBubble}>
          <div style={styles.userIcon}>🧑</div>
          <div style={styles.userText}>{turn.content}</div>
        </div>
      </div>
    );
  }

  if (turn.role === "system") {
    return <div style={styles.systemBubble}>{turn.content}</div>;
  }

  // role === "ai"
  if (turn.pending) {
    return (
      <div style={styles.aiRow}>
        <div style={styles.aiBubblePending}>
          <span style={styles.pendingDot} />
          {turn.content}
        </div>
      </div>
    );
  }

  if (turn.error && !turn.applied) {
    return (
      <div style={styles.aiRow}>
        <div style={styles.aiBubbleError}>
          <div style={styles.errorTitle}>⚠️ 처리 실패</div>
          <div style={styles.errorBody}>{turn.content}</div>
        </div>
      </div>
    );
  }

  const patch = (turn.patch ?? []) as JsonPatchOp[];
  // 이벤트 기반 자동 텍스트+SFX 오버레이 응답 — 별도 카드 렌더링.
  if (turn.overlay_result) {
    return (
      <div style={styles.aiRow}>
        <div style={styles.aiBubble}>
          <OverlayPreview
            overlay={turn.overlay_result}
            summary={turn.content}
            applied={!!turn.applied}
            rejected={!!turn.rejected}
            busy={busy}
            onApply={onApply}
            onReject={onReject}
          />
          {turn.error && (
            <div style={styles.errorBanner}>마지막 작업 중 오류: {turn.error}</div>
          )}
        </div>
      </div>
    );
  }
  // 이벤트 기반 자동 씬 분할 응답 — 별도 카드 렌더링.
  if (turn.split_result) {
    return (
      <div style={styles.aiRow}>
        <div style={styles.aiBubble}>
          <SplitPreview
            split={turn.split_result}
            summary={turn.content}
            applied={!!turn.applied}
            rejected={!!turn.rejected}
            busy={busy}
            onApply={onApply}
            onReject={onReject}
          />
          {turn.error && (
            <div style={styles.errorBanner}>마지막 작업 중 오류: {turn.error}</div>
          )}
        </div>
      </div>
    );
  }
  return (
    <div style={styles.aiRow}>
      <div style={styles.aiBubble}>
        <PatchPreview
          patch={patch}
          summary={turn.content}
          safetyFlags={turn.safety_flags ?? []}
          estimatedCostUsd={turn.estimated_cost_usd ?? 0}
          applied={!!turn.applied}
          rejected={!!turn.rejected}
          undone={!!turn.undone}
          busy={busy}
          onApply={onApply}
          onReject={onReject}
          onRequestRevise={onRevise}
          onUndo={onUndo}
        />
        {turn.error && (
          <div style={styles.errorBanner}>마지막 작업 중 오류: {turn.error}</div>
        )}
      </div>
    </div>
  );
}

// 이벤트 기반 자동 씬 분할 미리보기 카드.
function SplitPreview({
  split,
  summary,
  applied,
  rejected,
  busy,
  onApply,
  onReject,
}: {
  split: { scene_id: number; cuts_sec: number[]; reasoning: string };
  summary: string;
  applied: boolean;
  rejected: boolean;
  busy: boolean;
  onApply: () => void;
  onReject: () => void;
}) {
  const n = split.cuts_sec.length;
  return (
    <div style={{ padding: 12, background: "#1a1a2a", border: "1px solid #2a2a4a", borderRadius: 8 }}>
      <div style={{ color: "#a5b4fc", fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
        ✂ 자동 씬 분할 미리보기
      </div>
      <div style={{ color: "#ddd", fontSize: 13, marginBottom: 8 }}>{summary}</div>
      {split.reasoning && (
        <div style={{ color: "#888", fontSize: 12, marginBottom: 8, lineHeight: 1.4 }}>
          {split.reasoning}
        </div>
      )}
      <div style={{ color: "#bbb", fontSize: 12, marginBottom: 10 }}>
        씬 {split.scene_id}에서 {n}개 지점: {split.cuts_sec.map((c) => `${c.toFixed(1)}s`).join(", ")}
      </div>
      {applied ? (
        <div style={{ color: "#34d399", fontSize: 12 }}>✓ 적용됨</div>
      ) : rejected ? (
        <div style={{ color: "#888", fontSize: 12 }}>거부됨</div>
      ) : (
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={onApply}
            disabled={busy || n === 0}
            style={{ padding: "6px 14px", background: "#4338ca", color: "#fff", border: "none", borderRadius: 6, cursor: busy ? "wait" : "pointer", fontSize: 13 }}
          >
            {busy ? "분할 중..." : `${n}개 분할 적용`}
          </button>
          <button
            onClick={onReject}
            disabled={busy}
            style={{ padding: "6px 14px", background: "#252525", color: "#888", border: "1px solid #333", borderRadius: 6, cursor: "pointer", fontSize: 13 }}
          >
            거부
          </button>
        </div>
      )}
    </div>
  );
}

// 이벤트 기반 자동 텍스트+SFX 오버레이 미리보기 카드.
function OverlayPreview({
  overlay,
  summary,
  applied,
  rejected,
  busy,
  onApply,
  onReject,
}: {
  overlay: NonNullable<NlEditChatTurnRecord["overlay_result"]>;
  summary: string;
  applied: boolean;
  rejected: boolean;
  busy: boolean;
  onApply: () => void;
  onReject: () => void;
}) {
  const n = overlay.overlays.length;
  const withSfx = overlay.overlays.filter((o) => o.sfx_asset_id).length;
  return (
    <div style={{ padding: 12, background: "#1a1a2a", border: "1px solid #2a2a4a", borderRadius: 8 }}>
      <div style={{ color: "#a5b4fc", fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
        💬 자동 텍스트+SFX 오버레이 미리보기
      </div>
      <div style={{ color: "#ddd", fontSize: 13, marginBottom: 8 }}>{summary}</div>
      {overlay.reasoning && (
        <div style={{ color: "#888", fontSize: 12, marginBottom: 8, lineHeight: 1.4 }}>
          {overlay.reasoning}
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 10, maxHeight: 180, overflowY: "auto" }}>
        {overlay.overlays.map((o, i) => (
          <div key={i} style={{ color: "#bbb", fontSize: 12, display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ color: "#888" }}>{o.start_sec.toFixed(1)}s</span>
            <span style={{ color: "#fbbf24" }}>"{o.text}"</span>
            <span style={{ color: "#666" }}>· {o.font_family} · ({o.x.toFixed(2)}, {o.y.toFixed(2)})</span>
            {o.sfx_asset_id ? (
              <span style={{ color: "#34d399" }}>· 🔊 SFX</span>
            ) : (
              <span style={{ color: "#f59e0b" }}>· ⚠ SFX 미연결</span>
            )}
          </div>
        ))}
      </div>
      {withSfx < n && (
        <div style={{ color: "#f59e0b", fontSize: 11, marginBottom: 8 }}>
          SFX 미연결 {n - withSfx}개 — footage 스튜디오에서 오디오 자산 등록 후 수동 연결하세요.
        </div>
      )}
      {applied ? (
        <div style={{ color: "#34d399", fontSize: 12 }}>✓ 적용됨</div>
      ) : rejected ? (
        <div style={{ color: "#888", fontSize: 12 }}>거부됨</div>
      ) : (
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={onApply}
            disabled={busy || n === 0}
            style={{ padding: "6px 14px", background: "#4338ca", color: "#fff", border: "none", borderRadius: 6, cursor: busy ? "wait" : "pointer", fontSize: 13 }}
          >
            {busy ? "적용 중..." : `${n}개 오버레이 추가`}
          </button>
          <button
            onClick={onReject}
            disabled={busy}
            style={{ padding: "6px 14px", background: "#252525", color: "#888", border: "1px solid #333", borderRadius: 6, cursor: "pointer", fontSize: 13 }}
          >
            거부
          </button>
        </div>
      )}
    </div>
  );
}

// ── safety 확인 다이얼로그 ──────────────────────────────────────────────

function SafetyConfirmDialog({
  flags,
  summary,
  onConfirm,
  onCancel,
}: {
  flags: string[];
  summary: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div style={styles.dialogOverlay} role="dialog" aria-modal="true">
      <div style={styles.dialog}>
        <div style={styles.dialogTitle}>⚠️ 위험한 변경 확인</div>
        <p style={styles.dialogSummary}>{summary}</p>
        <ul style={styles.dialogFlags}>
          {flags.map((f) => {
            const labelMap: Record<string, string> = {
              delete_all_layers: "씬의 모든 레이어를 삭제합니다.",
              delete_all_scenes: "전체 씬을 삭제합니다.",
              canvas_size_change: "캔버스 크기/해상도/프레임 속도를 변경합니다.",
              bulk_layer_delete: "여러 레이어를 한 번에 삭제합니다.",
              unknown_top_level_path: "/scenes 외 영역을 건드립니다.",
            };
            return (
              <li key={f} style={styles.dialogFlagItem}>
                {labelMap[f] ?? "주의가 필요한 변경입니다."}
              </li>
            );
          })}
        </ul>
        <p style={styles.dialogQuestion}>그래도 적용할까요? 적용 후에는 "되돌리기" 버튼으로 복원할 수 있습니다.</p>
        <div style={styles.dialogActions}>
          <button type="button" onClick={onCancel} style={{ ...styles.dialogBtn, ...styles.dialogCancelBtn }}>
            취소
          </button>
          <button type="button" onClick={onConfirm} style={{ ...styles.dialogBtn, ...styles.dialogConfirmBtn }}>
            계속 적용
          </button>
        </div>
      </div>
    </div>
  );
}

// ── styles ────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  panel: {
    width: 380,
    minWidth: 320,
    background: "#0c0c14",
    borderLeft: "1px solid #1f1f33",
    display: "flex",
    flexDirection: "column",
    height: "100%",
    flexShrink: 0,
  },
  header: {
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    padding: "12px 14px",
    borderBottom: "1px solid #1f1f33",
    gap: 8,
  },
  title: { color: "#e5e7eb", fontWeight: 700, fontSize: 14 },
  subtitle: { color: "#6b7280", fontSize: 11, marginTop: 2 },
  headerButtons: { display: "flex", gap: 6, flexShrink: 0 },
  headerBtn: {
    padding: "4px 10px",
    background: "#1a1a2e",
    border: "1px solid #2a2a4a",
    borderRadius: 5,
    color: "#9ca3af",
    fontSize: 11,
    cursor: "pointer",
  },
  chatScroll: {
    flex: 1,
    overflowY: "auto",
    padding: "12px",
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  placeholderText: { color: "#666", fontSize: 12, textAlign: "center", padding: "20px 0" },
  emptyState: { padding: "12px 8px", color: "#9ca3af" },
  emptyTitle: { fontSize: 14, fontWeight: 600, marginBottom: 8, color: "#cbd5e1" },
  emptyHints: { fontSize: 12, color: "#6b7280" },
  exampleList: { margin: "6px 0 0 0", paddingLeft: 18, lineHeight: 1.7 },
  userRow: { display: "flex", justifyContent: "flex-end" },
  userBubble: {
    display: "flex",
    gap: 6,
    background: "#1e1b4b",
    border: "1px solid #4338ca",
    color: "#cbd5e1",
    borderRadius: 10,
    padding: "8px 10px",
    fontSize: 13,
    maxWidth: "85%",
    whiteSpace: "pre-wrap",
    lineHeight: 1.4,
  },
  userIcon: { fontSize: 14, lineHeight: "18px" },
  userText: { flex: 1 },
  aiRow: { display: "flex", justifyContent: "flex-start" },
  aiBubble: { width: "100%", display: "flex", flexDirection: "column", gap: 4 },
  aiBubblePending: {
    background: "#161623",
    border: "1px solid #2a2a4a",
    color: "#a78bfa",
    borderRadius: 10,
    padding: "8px 12px",
    fontSize: 12,
    display: "flex",
    alignItems: "center",
    gap: 8,
    fontStyle: "italic",
  },
  pendingDot: {
    display: "inline-block",
    width: 8,
    height: 8,
    borderRadius: "50%",
    background: "#a78bfa",
    animation: "pulse 1s ease-in-out infinite",
  },
  aiBubbleError: {
    background: "#2a1010",
    border: "1px solid #7f1d1d",
    color: "#fca5a5",
    borderRadius: 10,
    padding: "10px 12px",
    fontSize: 12,
    width: "100%",
  },
  errorTitle: { fontWeight: 700, marginBottom: 4 },
  errorBody: { color: "#fda4af", lineHeight: 1.4 },
  errorBanner: {
    background: "#2a1010",
    color: "#fca5a5",
    borderLeft: "3px solid #ef4444",
    padding: "4px 8px",
    fontSize: 11,
    borderRadius: 4,
  },
  systemBubble: {
    color: "#6b7280",
    fontSize: 11,
    textAlign: "center",
    padding: "4px 0",
  },
  inputArea: {
    borderTop: "1px solid #1f1f33",
    padding: "10px 12px",
    display: "flex",
    flexDirection: "column",
    gap: 6,
    background: "#0a0a14",
  },
  textarea: {
    width: "100%",
    background: "#111",
    border: "1px solid #2a2a4a",
    borderRadius: 6,
    color: "#e5e7eb",
    fontSize: 13,
    padding: "8px 10px",
    resize: "vertical",
    fontFamily: "inherit",
    boxSizing: "border-box",
    minHeight: 60,
    maxHeight: 200,
  },
  inputRow: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
  },
  inputHint: { color: "#6b7280", fontSize: 11 },
  sendBtn: {
    padding: "6px 18px",
    background: "#6366f1",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 12,
    fontWeight: 600,
  },
  sendBtnDisabled: {
    background: "#2a2a3a",
    color: "#6b7280",
    cursor: "not-allowed",
  },
  dialogOverlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.7)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 10000,
  },
  dialog: {
    background: "#0f0f17",
    border: "1px solid #b45309",
    borderRadius: 12,
    padding: 20,
    width: "min(440px, 90vw)",
    color: "#e5e7eb",
    boxShadow: "0 20px 40px rgba(0,0,0,0.6)",
  },
  dialogTitle: { fontSize: 16, fontWeight: 700, color: "#fbbf24", marginBottom: 12 },
  dialogSummary: { color: "#cbd5e1", fontSize: 13, lineHeight: 1.5, margin: "0 0 12px 0" },
  dialogFlags: { color: "#fde68a", fontSize: 12, paddingLeft: 18, margin: "0 0 12px 0", lineHeight: 1.6 },
  dialogFlagItem: { marginBottom: 2 },
  dialogQuestion: { color: "#9ca3af", fontSize: 12, margin: "0 0 16px 0", lineHeight: 1.5 },
  dialogActions: { display: "flex", justifyContent: "flex-end", gap: 8 },
  dialogBtn: {
    padding: "8px 18px",
    border: "1px solid #333",
    borderRadius: 6,
    fontSize: 13,
    cursor: "pointer",
    fontWeight: 600,
  },
  dialogCancelBtn: {
    background: "#1a1a1a",
    color: "#cbd5e1",
  },
  dialogConfirmBtn: {
    background: "#7c2d12",
    color: "#fed7aa",
    borderColor: "#9a3412",
  },
};
