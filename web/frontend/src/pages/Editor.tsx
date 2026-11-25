import { useCallback, useEffect, useMemo, useRef, useState, Fragment } from "react";
import { useDroppable, useDraggable, DndContext } from "@dnd-kit/core";
import type { DragEndEvent } from "@dnd-kit/core";
import { useStore } from "../store";
import { ui } from "../store/ui";
import { updateProject, updateScene as dbUpdateScene, deleteScene as dbDeleteScene, deleteAllScenes as dbDeleteAllScenes, addScene as dbAddScene, insertSceneAt as dbInsertSceneAt, insertImageSceneAt as dbInsertImageSceneAt, duplicateScene as dbDuplicateScene, splitSceneAt as dbSplitSceneAt, setScenes as dbSetScenes, computeAutoFootageSpeed, getSceneTtsDuration, getSceneRawFootageDuration, AUTO_SPEED_MARGIN_SEC, FOOTAGE_SPEED_MIN, FOOTAGE_SPEED_MAX, rescaleSceneOverlayTimingsForSpeed } from "../lib/project";
import { getSceneSpeedSpec, speedOutputDuration, DEFAULT_RAMP_HOLD, DEFAULT_RAMP_ACCEL, type FootageSpeedMode } from "../lib/speedRamp";
import { makeFootageFromFilename } from "../lib/footage";
// 이어보기 적응형 프리페치 — 깊이 산출(순수 함수) + blob 저장소(모듈 스코프).
import { computePrefetchDepth, updateLoadEstimate } from "../lib/prefetchDepth";
import {
  take as takePrefetched,
  start as startPrefetch,
  invalidate as invalidatePrefetch,
  clearAll as clearAllPrefetch,
  retainOnly as retainPrefetch,
  has as hasPrefetch,
} from "../lib/prefetchStore";
import { getTtsBlobUrl } from "../lib/tts";
import { useTtsPreview } from "../lib/ttsPreview";
// 미리보기는 서버 API로 통일 — WebGL/ffmpeg.wasm은 fit/속성 미반영 회귀 원인이라 미사용.
// TTS 는 서버가 진실의 원천 — IndexedDB blob 을 서버로 부활(tts-upload)시키던 경로를 제거하며
//   dbGetTtsAudio / synthesizeTtsAndSave / uploadSceneTts import 도 함께 걷어냈다.

// 프로젝트 단위로 "이미 서버에 업로드된 footage 파일명" 추적 — 모듈 스코프.
// PreviewPanel 인스턴스가 재마운트되어도 동일 파일명은 재업로드하지 않게 한다.
// (인스턴스 ref로 두면 fit 변경마다 새 인스턴스 = 새 ref = 매번 재업로드라 매우 느림.)
const _uploadedFootageByProject: Map<string, Set<string>> = new Map();
function _hasUploadedFootage(projectId: string, filename: string): boolean {
  return _uploadedFootageByProject.get(projectId)?.has(filename) ?? false;
}
function _markUploadedFootage(projectId: string, filename: string): void {
  let s = _uploadedFootageByProject.get(projectId);
  if (!s) { s = new Set(); _uploadedFootageByProject.set(projectId, s); }
  s.add(filename);
}

/** 씬의 서버 preview-cache 를 삭제하면서 이어보기 프리페치분도 함께 무효화한다.
 *
 *  ⚠ 씬 편집 경로는 반드시 이 함수를 쓸 것. `api.delete` 를 직접 호출하면 서버 캐시만 지워지고
 *  브라우저에 미리 받아 둔 낡은 mp4 blob 이 남아, 이어보기 중 수정 전 영상이 재생되는
 *  회귀가 생긴다(적응형 프리페치 도입 시 신설한 불변식). */
function _invalidateScenePreview(projectId: string, sceneId: number): Promise<unknown> {
  invalidatePrefetch(projectId, sceneId);
  return api.delete(`/api/projects/${projectId}/scenes/${sceneId}/preview-cache`).catch(() => {});
}

/** 씬 한 개를 **IndexedDB 에만** 반영한다(네트워크 호출 없음).
 *
 *  ⚠ `lib/project.ts` 의 `updateScene`(= 이 파일의 `dbUpdateScene`)을 쓰면 안 되는 자리에 쓴다.
 *  그 함수는 이름과 달리 로컬 전용이 아니다:
 *      getProject(서버 GET, 프로젝트 전체)
 *        → updateProject(또 getProject → pushProjectState = PUT /register)
 *  #413 에서 `getProject` 가 "서버 우선" 으로 바뀌며 로컬 함수가 아니게 됐는데, 호출부들은
 *  여전히 "로컬 저장" 으로 알고 쓰고 있었다. 그 결과 대본을 고칠 때마다 프로젝트 전체(80KB)를
 *  두 번 읽고 씬 전체를 되쓰는 요청이 나가, 타이핑이 멈추고 register 가 폭주했다.
 *
 *  서버 반영은 각 호출부가 이미 `patchScene`(해당 씬만 PATCH)으로 하고 있으므로,
 *  여기서는 오프라인 폴백용 로컬 캐시만 갱신하면 된다. */
async function _localUpdateScene(
  projectId: string,
  sceneId: number,
  patch: Record<string, unknown>,
): Promise<void> {
  try {
    const { dbGetProject, dbSaveProject } = await import("../lib/db");
    const p = await dbGetProject(projectId);
    if (!p) return;  // 로컬 캐시가 아직 없으면 스킵(서버가 진실이므로 무해).
    const scenes = p.scenes.map((s) =>
      s.scene_id === sceneId ? { ...s, ...patch } : s
    );
    await dbSaveProject({ ...p, scenes });
  } catch {
    /* 로컬 캐시 갱신 실패는 무시 — 서버에는 patchScene 으로 이미 반영된다. */
  }
}

/** 서버의 바깥 변경(에이전트/스크립트 편집)을 열린 에디터 store 로 가져온다.
 *  로컬에서 편집 중인 항목은 덮지 않는다 — 규칙은 lib/serverSync.ts 참고.
 *  미리보기 직전(loadPreview)과 주기 폴링에서 호출한다. 반환값: store 가 바뀌었는지. */
async function syncProjectFromServer(projectId: string): Promise<boolean> {
  const server = await getProjectFromServer(projectId).catch(() => null);
  if (!server || !Array.isArray((server as any).scenes)) return false;
  const local = useStore.getState().project;
  if (!local || local.project_id !== projectId) return false;
  const { merged, changed } = mergeServerProject(local, server, getSyncSnap(projectId));
  // 응답을 기다리는 사이 사용자가 편집했으면 이번 병합은 버린다(다음 동기화에서 다시 판단).
  if (useStore.getState().project !== local) return false;
  if (changed) {
    useStore.getState().setProject(merged);
    try {
      const { dbSaveProject } = await import("../lib/db");
      await dbSaveProject(merged);
    } catch { /* 로컬 캐시 실패는 무해 — 서버가 진실 */ }
  }
  rememberSync(projectId, changed ? merged : local, server);
  return changed;
}
import { mergeServerProject, rememberSync, getSnap as getSyncSnap } from "../lib/serverSync";
import { getProject as getProjectFromServer } from "../api/client";
import { api, uploadSceneFootage, getScenePreview, registerProject, startRender, renderOutputUrl, patchScene, createTemplate, assignClipToScene, generateTts, type ClipLibraryEntry } from "../api/client";
// 미리보기 단일화: 백엔드 ffmpeg 미리보기만 사용 — 클라 렌더(simple/overlay) 모드는 제거.
//   - 회귀 위험을 줄이기 위해 lib/isSimpleRenderSpec, lib/isOverlayRenderSpec,
//     lib/clientSceneRenderer, components/OverlayCanvas 파일 자체는 보존하고 import만 해제한다.
//   - SFX 미리듣기 훅(useSfxPreview)과 타입(SfxTrigger)은 OverlayCanvas 모듈에 함께 있어
//     named import만 유지한다. default export(OverlayCanvas) 는 더 이상 사용하지 않는다.
import { useSfxPreview, type SfxTrigger } from "../components/OverlayCanvas";
import { useWs } from "../hooks/useWs";
import EffectModal from "../components/EffectModal";
import SplitSceneDialog from "../components/SplitSceneDialog";
import TextOverlayEditor from "../components/TextOverlayEditor";
import SfxEditor from "../components/SfxEditor";
import SceneTagsEditor from "../components/SceneTagsEditor";
import SceneCommentEditor from "../components/SceneCommentEditor";
import SceneSpeakerPicker from "../components/SceneSpeakerPicker";
import SpeakerDictionaryModal from "../components/SpeakerDictionaryModal";
import EmojiPicker from "../components/EmojiPicker";
import TagDictionaryModal from "../components/TagDictionaryModal";
import EffectAddPanel from "../components/EffectAddPanel";
import CostBadge from "../components/CostBadge";
import NlEditorPanel from "../components/NlEditorPanel";
import { GEMINI_ENABLED } from "../lib/features";
import TimelineBar from "../components/TimelineBar";
import FootageSourceDialog from "../components/FootageSourceDialog";
import LocalThumbnail from "../components/LocalThumbnail";
import ServerFootageThumbnail from "../components/ServerFootageThumbnail";
// #380 — Editor 사이드바에서도 화면 녹화로 footage 즉시 추가 가능.
import ScreenRecorderDialog from "../components/ScreenRecorderDialog";
import ClipLibraryModal from "../components/ClipLibraryModal";
import { applySceneTags, removeSceneTag, getBuiltinTagDictionary, loadTagDictionary, type SceneTagDictionary } from "../lib/sceneTags";
import type { SceneCard, FootageAsset, TextOverlay, ShapeOverlay, ShapeType, GraphicOverlay, GraphicOverlayType, SceneFilter, SceneFilterType, SceneMotion, MotionType, MosaicOverlay, MosaicShape, MosaicMode, ImageOverlay, SpeedLinesOverlay, NonTextEnterExitEffect, Speaker } from "../types";
import { EMOJI_ICON_TABLE } from "../types";
import { EMOJI_BY_KEY } from "../lib/emojiCatalog";

import { useVoiceOptions, useTypecastRawVoices, resolveProvider } from "../lib/ttsVoices";
import VoiceOptions from "../components/VoiceOptions";
import TypecastVoiceModal from "../components/TypecastVoiceModal";

// #355 — Gemini 모델 선택 옵션. pricing.json `api.gemini.models` 키와 값(value)이
// 1:1 매칭되어야 한다. 기본값은 "gemini-2.5-flash" (Project.gemini_model 기본값과 일치).
// 정확도/비용 트레이드오프를 사용자가 직접 고를 수 있도록 헤더 드롭다운에 노출.
const GEMINI_MODELS: { value: string; label: string }[] = [
  { value: "gemini-2.5-flash-lite",   label: "Gemini 2.5 Flash-Lite (저렴)" },
  { value: "gemini-2.5-flash",        label: "Gemini 2.5 Flash (기본)" },
  { value: "gemini-2.5-pro",          label: "Gemini 2.5 Pro (정확)" },
  { value: "gemini-3-flash-preview",  label: "Gemini 3 Flash Preview (실험)" },
];


const EFFECT_COLORS: Record<string, string> = {
  clip: "#3b82f6",
  kenburns: "#8b5cf6",
  kenburns_copy_img: "#7c3aed",
  edit: "#f59e0b",
  sync: "#ef4444",
  multiscreen: "#06b6d4",
  title_gen: "#10b981",
};

// 줌 박스 오버레이 좌표 계산 (모든 미리보기 경로에서 재사용).
// 첫 번째 clip_zoom 모션의 end_scale/target 으로 "최종 줌 영역"을 퍼센트로 환산한다.
// end_scale === 1.0 이거나 motion이 없으면 null(=박스 미표시).
function _computeZoomBox(
  motions: Array<{ motion_type?: string; params?: Record<string, unknown> | null }> | null | undefined,
): { leftPct: number; topPct: number; wPct: number; hPct: number } | null {
  const zm = (motions ?? []).find((m) => m.motion_type === "clip_zoom");
  if (!zm) return null;
  const p = (zm.params ?? {}) as Record<string, unknown>;
  const tx = typeof p.target_x === "number" ? (p.target_x as number) : 0.5;
  const ty = typeof p.target_y === "number" ? (p.target_y as number) : 0.5;
  const z1 = typeof p.end_scale === "number" ? (p.end_scale as number) : 1.3;
  if (Math.abs(z1 - 1.0) < 1e-6) return null;
  const wPct = 100 / z1;
  const hPct = 100 / z1;
  const rawLeft = tx * 100 - wPct / 2;
  const rawTop = ty * 100 - hPct / 2;
  const left = Math.max(0, Math.min(100 - wPct, rawLeft));
  const top = Math.max(0, Math.min(100 - hPct, rawTop));
  return { leftPct: left, topPct: top, wPct, hPct };
}

// 서버 footage_dir에서 파일을 직접 받아온다. 서버가 footage 메타의 유일한 진실.
async function fetchFootageBlob(projectId: string, filename: string): Promise<Blob> {
  const url = `/api/projects/${projectId}/footage/${encodeURIComponent(filename)}/stream`;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`footage 읽기 실패 (HTTP ${res.status}): ${filename}`);
  }
  return res.blob();
}

const RESOLUTION_PRESETS = [
  { label: "1920×1080 (가로)", width: 1920, height: 1080 },
  { label: "1080×1920 (세로/숏폼)", width: 1080, height: 1920 },
  { label: "1080×1080 (정사각)", width: 1080, height: 1080 },
  { label: "직접 입력", width: 0, height: 0 },
];

// 이어보기 배속 선택지. 1 을 반드시 포함해야 저장값 복원이 기본으로 떨어진다.
// 16 은 Chrome <video>.playbackRate 상한(초과 시 NotSupportedError). 4배속 초과는 브라우저가 소리를 끈다.
const SEQUENTIAL_RATES: number[] = [1, 1.25, 1.5, 2, 3, 4, 8, 16];

// ── Preview Panel ─────────────────────────────────────────────────────────

function PreviewPanel({ projectId, scene, outputWidth, outputHeight, previewHeight, onPreviewHeightChange, onUpdateGraphicOverlays, refreshSignal, showOverlayGuides = true, onTimeUpdate, seekRequest, sequentialPlay = false, sequentialRate = 1, onSceneEnded }: {
  projectId: string;
  scene: SceneCard | null;
  outputWidth: number;
  outputHeight: number;
  previewHeight: number;
  onPreviewHeightChange: (h: number) => void;
  /** 부모의 previewTrigger 값. 변경 시 인플레이스로 loadPreview 1회 재실행. */
  refreshSignal: number;
  /** 이어보기(연속 재생) 모드. true 면 loop 를 끄고, preview 로드 완료 후 자동재생하며,
   *  video ended 시 onSceneEnded 를 호출한다. false 면 기존 단일 씬 재생과 100% 동일. */
  sequentialPlay?: boolean;
  /** 이어보기 재생 배속(playbackRate). 일반 모드에서는 항상 1 로 둔다. */
  sequentialRate?: number;
  /** 이어보기 모드에서 현재 씬 video 재생이 끝났을 때 호출. 부모가 다음 씬으로 전환한다. */
  onSceneEnded?: () => void;
  // #222 — 캔버스 드래그로 graphic_overlay (x, y) 조절. 드래그 종료 시에만 호출.
  onUpdateGraphicOverlays?: (graphics: GraphicOverlay[]) => void;
  /** #359 — 오버레이 편집 가이드(드래그 핸들·점선 박스·줌 박스 라벨) 표시 여부. 기본 true. */
  showOverlayGuides?: boolean;
  /** #364 — video 의 currentTime 이 갱신될 때 호출. TimelineBar playhead 동기화용.
   *  씬 로컬 sec(=video.currentTime) 를 그대로 전달. */
  onTimeUpdate?: (sec: number) => void;
  /** #364 — TimelineBar 클릭/드래그가 요청한 seek. sceneId 가 현재 씬과 같을 때만
   *  videoRef.current.currentTime 을 sec 로 설정. tick 이 바뀌면 같은 sec 도 재요청. */
  seekRequest?: { sceneId: number; sec: number; tick: number } | null;
}) {
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [isRendering, setIsRendering] = useState(false);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [previewProgress, setPreviewProgress] = useState<number | null>(null);
  const prevBlobUrlRef = useRef<string | null>(null);
  const previewHeightRef = useRef(previewHeight);

  // 미리보기는 백엔드 ffmpeg 결과 mp4 하나만 사용. 클라 렌더(simple/overlay) 모드는 제거됨.
  // 이전에 분기되던 simpleRender/overlayRender/renderMode/forceBackend 상태는 더 이상 필요 없음.

  const matchedFile = scene?.footage
    ? ("file" in scene.footage ? scene.footage.file : "image" in scene.footage ? (scene.footage as unknown as Record<string, string>).image : null)
    : null;

  // refs: useCallback 내에서 최신 값 참조 (deps 불필요)
  const sceneRef = useRef(scene);
  useEffect(() => { sceneRef.current = scene; }, [scene]);
  const matchedFileRef = useRef(matchedFile);
  useEffect(() => { matchedFileRef.current = matchedFile; }, [matchedFile]);
  const outputWidthRef = useRef(outputWidth);
  useEffect(() => { outputWidthRef.current = outputWidth; }, [outputWidth]);
  const outputHeightRef = useRef(outputHeight);
  useEffect(() => { outputHeightRef.current = outputHeight; }, [outputHeight]);

  // 진행 중인 loadPreview 요청 ID — 동일 PreviewPanel 인스턴스에서 빠르게 fit을 여러 번
  // 바꿀 때 이전 요청의 응답이 늦게 도착해 새 요청 결과를 덮어쓰는 race를 막는다.
  const loadRequestIdRef = useRef(0);

  const loadPreview = useCallback(async () => {
    // 아래 patchScene 은 store 의 씬 값을 서버로 보낸다. 그 전에 서버의 바깥 변경을 먼저
    // 받아 와야 한다 — 안 그러면 새로고침 안 한 에디터가 옛 값으로 서버 최신본을 되덮는다
    // (2026-11-05 1·13번 씬이 미리보기 때마다 되돌아간 회귀).
    await syncProjectFromServer(projectId);
    // ⚠ sceneRef.current 는 useEffect 로 갱신되어 React 렌더 사이클 이후에야 최신.
    // 슬라이더 변경 직후 새로고침 버튼 클릭 시 ref 가 옛 값이라 loadPreview 내부의 patchScene 이
    // 백엔드 state 를 옛 image_overlays 로 다시 덮어쓰는 race 가 발생한다. zustand store 에서
    // 직접 최신 scene 을 읽어 race 차단. ref 폴백은 store 가 아직 로드되지 않은 초기 마운트 대응.
    const refScene = sceneRef.current;
    const latestProject = useStore.getState().project;
    const scene = refScene
      ? (latestProject?.scenes.find((s) => s.scene_id === refScene.scene_id) ?? refScene)
      : refScene;
    const matchedFile = matchedFileRef.current;
    const outW = outputWidthRef.current;
    const outH = outputHeightRef.current;
    if (!scene) return;

    // 이 호출의 ID 발급. 응답 도착 시점에 최신 ID와 다르면 stale로 간주하고 무시.
    const myId = ++loadRequestIdRef.current;
    const isStale = () => loadRequestIdRef.current !== myId;

    setRenderError(null);
    if (!matchedFile) return;

    // ── 이어보기 프리페치 적중 경로 ────────────────────────────────────────
    // 이어보기 중 부모(Editor)가 미리 받아 둔 blob 이 있으면 register/patch/upload 왕복과
    // 서버 preview 요청을 전부 건너뛰고 즉시 표시한다. 여기서 꺼낸 URL 의 revoke 책임은
    // prevBlobUrlRef 로 넘어온다(store 는 take 시점에 엔트리를 제거).
    // 편집 시에는 부모가 prefetchStore.invalidate 를 호출하므로 낡은 blob 이 남지 않는다.
    {
      const pre = takePrefetched(projectId, scene.scene_id);
      if (pre) {
        if (prevBlobUrlRef.current) {
          URL.revokeObjectURL(prevBlobUrlRef.current);
        }
        prevBlobUrlRef.current = pre;
        setVideoUrl(pre);
        setIsRendering(false);
        setPreviewProgress(null);
        return;
      }
    }

    // 미리보기는 기본적으로 서버 API. #309에서 단순 씬은 클라 렌더 우선 시도.
    setIsRendering(true);
    setPreviewProgress(null);
    try {
      // 프로젝트 서버 등록(upsert) — 출력 해상도만 동기화.
      // 주의: scenes를 보내면 서버가 "보낸 scene_id 외 기존 씬을 삭제"하므로 단일 씬만 보내면
      // 다른 모든 씬이 사라진다. 씬 자체 동기화는 아래 patchScene과 편집 시점의 patch가 담당.
      await registerProject(projectId, "", undefined, outW, outH).catch(() => {});

      // 씬 footage/text_overlays/footage_speed 서버 동기화 (브라우저 전용 프로젝트 대응)
      await patchScene(projectId, scene.scene_id, {
        text_overlays: scene.text_overlays ?? [],
        footage_speed: scene.footage_speed ?? 1.0,
        footage_speed_mode: scene.footage_speed_mode ?? "constant",
        footage_speed_ramp_hold: scene.footage_speed_ramp_hold ?? DEFAULT_RAMP_HOLD,
        footage_speed_ramp_accel: scene.footage_speed_ramp_accel ?? DEFAULT_RAMP_ACCEL,
        footage_speed_ramp_base: scene.footage_speed_ramp_base ?? 1.0,
        footage: scene.footage,
        shape_overlays: scene.shape_overlays ?? [],
        // 이미지 오버레이 — 미리보기 합성이 image_overlays를 참조하므로 함께 동기화.
        image_overlays: scene.image_overlays ?? [],
        // 집중선(속도선) — 미리보기 합성이 speed_lines(색/영역 포함)를 참조하므로 함께 동기화.
        // 이 필드를 빠뜨리면 색/영역 변경이 미리보기에 반영되지 않는 회귀가 발생한다.
        speed_lines: scene.speed_lines ?? [],
        // 켄번스/삽입 이미지 씬 길이 — 미리보기/렌더 길이가 duration_hint_sec 을 참조하므로 함께 동기화.
        // 빠뜨리면 슬라이더로 바꾼 씬 길이가 미리보기에 반영되지 않는 회귀가 발생한다.
        duration_hint_sec: scene.duration_hint_sec,
      }).catch(() => {});

      // footage: IndexedDB → 서버 업로드 (모듈 스코프 캐시로 중복 업로드 방지).
      // 인스턴스 ref는 fit 변경마다 PreviewPanel 재마운트로 초기화돼서 매번 재업로드되는
      // 회귀가 있었음. 모듈 스코프 Map으로 옮겨 프로젝트+파일명 단위로 1회만 업로드.
      if (!_hasUploadedFootage(projectId, matchedFile)) {
        // 서버에 이미 파일이 있으면 download/upload 왕복을 스킵 (322MB 왕복 방지).
        let _alreadyOnServer = false;
        try {
          const _existsRes = await fetch(`/api/projects/${projectId}/footage/${encodeURIComponent(matchedFile)}/exists`);
          if (_existsRes.ok) {
            const _ex = await _existsRes.json();
            _alreadyOnServer = !!_ex.exists;
          }
        } catch { /* 네트워크 오류면 기존 경로로 폴백 */ }
        if (!_alreadyOnServer) {
          const footageBlob = await fetchFootageBlob(projectId, matchedFile);
          await uploadSceneFootage(projectId, scene.scene_id, footageBlob, matchedFile).catch((e: Error) => {
            console.warn(`footage 업로드 경고 (계속 진행): ${e.message}`);
          });
        }
        _markUploadedFootage(projectId, matchedFile);
      }

      // TTS 는 서버에 이미 있는 WAV 를 사용한다("서버가 진실의 원천").
      //   과거: IndexedDB 옛 blob → tts-upload 로 서버 부활. 텍스트가 바뀐 씬을 옛 음성으로
      //   덮어쓰는 오염 회귀 원인이었다. 미리보기는 서버 build_merged_wav 가 서버 WAV 를 읽으므로
      //   브라우저가 blob 을 올릴 이유가 없다. 서버에 WAV 가 없는 씬은 무음(또는 사용자가
      //   재생성 버튼으로 서버 생성)으로 처리 — 옛 blob 부활 경로는 완전히 제거한다.

      // ── 백엔드 ffmpeg 미리보기 단일 경로 ─────────────────────────────────
      // 씬 1개를 mp4로 받아 <video>로 표시. footage_audio_volume 더킹은 이미 mp4에 적용된다.
      const previewBlob = await getScenePreview(projectId, scene.scene_id).catch((e: Error) => {
        throw new Error(`서버 미리보기 생성 실패: ${e.message}`);
      });
      if (isStale()) {
        return;
      }
      const url = URL.createObjectURL(previewBlob);
      if (prevBlobUrlRef.current) {
        URL.revokeObjectURL(prevBlobUrlRef.current);
      }
      prevBlobUrlRef.current = url;
      setVideoUrl(url);
    } catch (e: unknown) {
      setRenderError((e as Error).message ?? "미리보기 생성 실패");
    } finally {
      setIsRendering(false);
      setPreviewProgress(null);
    }
  }, [projectId]);

  useEffect(() => {
    previewHeightRef.current = previewHeight;
  }, [previewHeight]);

  // 마운트(=PreviewPanel key 변경) 시 자동 preview 재생성.
  // footage 속성 변경 시 부모 컴포넌트가 PreviewPanel key를 새로 만들어 재마운트시킨다.
  // (인플레이스 갱신 + 재마운트가 동시에 일어나면 race 발생해 마지막에 도착한 응답이 stale로
  //  덮일 수 있어, deps는 최소화하고 mount 1회만 호출되게 유지.)
  useEffect(() => {
    loadPreview();
    return () => {
      if (prevBlobUrlRef.current) {
        URL.revokeObjectURL(prevBlobUrlRef.current);
        prevBlobUrlRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 부모의 refreshSignal(=previewTrigger) 변경 시 인플레이스로 1회 재로드.
  // 마운트 effect와 deps 분리 — 마운트는 [], 새로고침은 [refreshSignal] 따로.
  const _mountedRef = useRef(false);
  useEffect(() => {
    if (!_mountedRef.current) {
      _mountedRef.current = true;
      return; // 최초 mount는 위 effect가 처리하므로 스킵 (중복 호출 방지).
    }
    loadPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSignal]);

  // #364 — TimelineBar 가 요청한 seek 처리.
  //   seekRequest 의 sceneId 가 현재 PreviewPanel 의 씬과 같을 때만 currentTime 을 설정.
  //   다른 씬으로의 점프는 selectScene 으로 부모 컴포넌트가 PreviewPanel 자체를 재마운트하므로,
  //   이 effect 는 같은 씬 안에서의 시간 위치 이동만 담당한다.
  //   tick 까지 deps 에 넣어 같은 sec 의 반복 요청도 처리.
  useEffect(() => {
    if (!seekRequest) return;
    const sc = sceneRef.current;
    if (!sc || sc.scene_id !== seekRequest.sceneId) return;
    const v = videoRef.current;
    if (!v) return;
    try {
      // duration 측정 전에는 currentTime 설정이 무시될 수 있어 metadata 로딩 전이면 한 번 대기.
      if (!isFinite(v.duration) || v.duration === 0) {
        const handler = () => {
          try { v.currentTime = Math.max(0, seekRequest.sec); } catch { /* ignore */ }
          v.removeEventListener("loadedmetadata", handler);
        };
        v.addEventListener("loadedmetadata", handler);
      } else {
        v.currentTime = Math.max(0, Math.min(v.duration, seekRequest.sec));
      }
    } catch { /* ignore */ }
  }, [seekRequest]);

  // 이어보기(연속 재생) — preview 로드 완료(videoUrl 세팅) 후 sequentialPlay 이면 자동재생.
  //   씬 전환 시 부모가 key 로 PreviewPanel 을 재마운트하므로, 새 씬의 videoUrl 이 준비되면
  //   이 effect 가 재실행되어 play() 를 호출한다. 사용자가 "이어보기" 버튼을 이미 눌러
  //   user gesture 컨텍스트가 있으므로 음소거 없이 재생 허용된다.
  //   sequentialPlay=false 인 일반 모드에서는 play() 를 호출하지 않아 기존 동작과 동일.
  useEffect(() => {
    if (!sequentialPlay || !videoUrl) return;
    const v = videoRef.current;
    if (!v) return;
    v.muted = false;
    // src 가 바뀌면 playbackRate 가 1 로 돌아가므로 재생 직전에 매번 다시 걸어 준다.
    v.playbackRate = sequentialRate;
    const p = v.play();
    if (p && typeof p.catch === "function") p.catch(() => { /* autoplay 차단 시 무시 */ });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoUrl, sequentialPlay, sequentialRate]);

  // 재생 중에 배속을 바꾸면 즉시 반영한다(다음 씬까지 기다리지 않게).
  // 이어보기를 끄면 1 배속으로 되돌려 일반 재생에 영향을 남기지 않는다.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    v.playbackRate = sequentialPlay ? sequentialRate : 1;
  }, [sequentialPlay, sequentialRate]);

  function handleDragHandleMouseDown(e: React.MouseEvent) {
    e.preventDefault();
    const startY = e.clientY;
    const startH = previewHeightRef.current;
    const onMove = (ev: MouseEvent) => {
      const newH = Math.min(600, Math.max(120, startH + ev.clientY - startY));
      onPreviewHeightChange(newH);
    };
    const onUp = () => {
      localStorage.setItem("preview_panel_height", String(previewHeightRef.current));
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  // ── #222 후속: 그래픽 오버레이 (x,y) 드래그 핸들 ─────────────────────────
  // video 요소의 실제 렌더 사각형을 기준으로 비율 좌표를 계산한다.
  // 드래그 중에는 로컬 state(dragOverlays)에 기록하고, 종료 시 onUpdateGraphicOverlays 호출.
  const videoRef = useRef<HTMLVideoElement | null>(null);
  // SFX 훅에 넘길 element state. callback ref 를 통해 마운트/언마운트 즉시 갱신.
  //   과거엔 videoRef.current 만 참조해 훅이 첫 렌더 시점에 null 이면 5초 폴링 후 포기했다.
  //   이제 element 가 mount 될 때마다 훅이 자동 재실행되어 SFX listener 가 새 video 에 붙는다.
  const [videoEl, setVideoEl] = useState<HTMLVideoElement | null>(null);
  const setVideoRefAndEl = useCallback((el: HTMLVideoElement | null) => {
    videoRef.current = el;
    setVideoEl(el);
  }, []);
  // 줌 박스 오버레이용 — <video> 의 실제 letterbox 사각형(부모 컨테이너 기준 상대 좌표).
  // CSS로 video 종횡비를 추정하면 maxWidth/maxHeight 사이 어느 축이 바인딩되는지 부정확.
  // ResizeObserver + getBoundingClientRect 로 매번 정확한 픽셀을 잡는다.
  const [videoRect, setVideoRect] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  // 미리보기 컨트롤바 호버 표시 — 기본 컨트롤바(검은 반투명 띠)가 영상 하단을 상시 가려
  // 그 부분 내용이 안 보이던 문제. 마우스가 미리보기 영역 안에 있을 때만 controls 를 켠다.
  // 브라우저 기본 컨트롤바는 셰도우 DOM 이라 CSS 로 숨길 수 없어 속성 자체를 토글한다.
  const [controlsVisible, setControlsVisible] = useState(false);
  useEffect(() => {
    const v = videoRef.current;
    if (!v) { setVideoRect(null); return; }
    const update = () => {
      const parent = v.parentElement;
      if (!parent) return;
      const vr = v.getBoundingClientRect();
      const pr = parent.getBoundingClientRect();
      setVideoRect({
        left: vr.left - pr.left,
        top: vr.top - pr.top,
        width: vr.width,
        height: vr.height,
      });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(v);
    if (v.parentElement) ro.observe(v.parentElement);
    window.addEventListener("resize", update);
    return () => { ro.disconnect(); window.removeEventListener("resize", update); };
  }, [videoUrl, previewHeight]);
  // 미리보기 SFX — 어느 렌더 경로(클라/오버레이/백엔드)든 PreviewPanel의 videoRef가 가리키는
  // <video>에 SFX 훅을 한 번만 건다. text/shape/graphic/image 오버레이 중 sfx_asset_id가
  // 있는 항목을 트리거로 변환한다. resolveSfxStreamUrl 이 `preset:` prefix 를 판별해
  // studio_asset 과 내장 기본 효과음 프리셋을 모두 같은 훅으로 재생한다.
  //
  // [회귀 가드] useMemo 로 안정화 — 렌더마다 새 배열을 만들면 useSfxPreview 의 useEffect 가
  // 매 timeupdate(=리렌더) 마다 재실행되어 listener 가 교체되고 트리거가 발화하지 못한다.
  // deps 는 실제로 SFX 트리거에 영향을 주는 필드들의 직렬화만 사용.
  const sfxTriggers = useMemo<SfxTrigger[]>(() => {
    const collect = (arr: Array<{ id: string; start_sec?: number; end_sec?: number | null; sfx_asset_id?: string | null; sfx_volume?: number }> | undefined) =>
      (arr ?? [])
        .filter((o) => !!o.sfx_asset_id)
        .map((o) => ({
          id: o.id,
          start_sec: o.start_sec ?? 0,
          end_sec: o.end_sec ?? null,
          sfx_asset_id: o.sfx_asset_id as string,
          sfx_volume: o.sfx_volume ?? 0.7,
        }));
    const out: SfxTrigger[] = [
      ...collect(scene?.text_overlays as never),
      ...collect(scene?.shape_overlays as never),
      ...collect(scene?.graphic_overlays as never),
      ...collect(scene?.image_overlays as never),
    ];
    // 씬 본체 SFX — 트리거 시점 = 씬 시작(영상 0초) + sfx_offset_sec.
    if (scene?.sfx_asset_id) {
      out.push({
        id: `scene-sfx-${scene.scene_id}`,
        start_sec: scene.sfx_offset_sec ?? 0,
        end_sec: null,
        sfx_asset_id: scene.sfx_asset_id,
        sfx_volume: scene.sfx_volume ?? 0.7,
      });
    }
    return out;
    // deps: SFX 트리거 정의에 영향을 주는 모든 필드의 안정 직렬화. JSON.stringify 로 한 번에.
  }, [
    JSON.stringify(scene?.text_overlays?.map((o) => ({ id: o.id, s: o.start_sec, e: o.end_sec, a: o.sfx_asset_id, v: o.sfx_volume }))),
    JSON.stringify(scene?.shape_overlays?.map((o) => ({ id: o.id, s: o.start_sec, e: o.end_sec, a: o.sfx_asset_id, v: o.sfx_volume }))),
    JSON.stringify(scene?.graphic_overlays?.map((o) => ({ id: o.id, s: o.start_sec, e: o.end_sec, a: o.sfx_asset_id, v: o.sfx_volume }))),
    JSON.stringify(scene?.image_overlays?.map((o) => ({ id: o.id, s: o.start_sec, e: o.end_sec, a: o.sfx_asset_id, v: o.sfx_volume }))),
    scene?.scene_id,
    scene?.sfx_asset_id,
    scene?.sfx_volume,
    scene?.sfx_offset_sec,
  ]);
  useSfxPreview(videoEl, projectId, sfxTriggers);
  // footage_audio_volume 더킹은 백엔드 ffmpeg가 mp4를 만들 때 이미 적용했다.
  // 여기서 v.volume = footage_audio_volume 로 다시 곱하면 이중 더킹이 되어 더 작게 들린다.
  // 따라서 <video>는 항상 풀 볼륨(1.0)으로 재생하고, 슬라이더 변경 시에는 caller가
  // preview-cache 무효화 + 재렌더로 mp4 자체를 갱신한다.
  const [dragOverlays, setDragOverlays] = useState<GraphicOverlay[] | null>(null);
  // graphic_overlays 변경(외부)이면 dragOverlays 무효화 — 다음 렌더는 scene 원본 사용
  useEffect(() => { setDragOverlays(null); }, [scene?.graphic_overlays]);
  const displayedOverlays = dragOverlays ?? (scene?.graphic_overlays ?? []);

  function startGraphicDrag(e: React.MouseEvent, overlayId: string) {
    e.preventDefault();
    e.stopPropagation();
    const videoEl = videoRef.current;
    if (!videoEl) return;
    const rect = videoEl.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;

    // 시작 시점의 overlay 스냅샷 — 외부 변경 방지
    const baseOverlays = (scene?.graphic_overlays ?? []).map((g) => ({ ...g }));
    let latest = baseOverlays;

    const onMove = (ev: MouseEvent) => {
      const r = videoEl.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      const nx = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
      const ny = Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height));
      latest = latest.map((g) => (g.id === overlayId ? { ...g, x: nx, y: ny } : g));
      setDragOverlays(latest);
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      // 외부 콜백으로 영속화 (preview 무효화 포함)
      if (onUpdateGraphicOverlays) onUpdateGraphicOverlays(latest);
      // dragOverlays는 scene.graphic_overlays 변경을 감지하는 useEffect가 자동으로 비움
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  if (!scene) {
    return (
      <div style={{ ...styles.previewPanel, height: previewHeight, position: "relative" }}>
        <div style={styles.previewEmpty}>씬을 선택하면 미리보기가 표시됩니다</div>
        <div
          style={styles.previewDragHandle}
          onMouseDown={handleDragHandleMouseDown}
          title="드래그하여 높이 조절"
        />
      </div>
    );
  }

  return (
    <div style={{ ...styles.previewPanel, height: previewHeight, position: "relative" }}>
      <div style={styles.previewInner}>
        <div style={styles.previewVideoWrap}>
          {isRendering ? (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 8, width: "80%", maxWidth: 320 }}>
              <div style={{ color: "#aaa", fontSize: 13 }}>
                미리보기 생성 중...{previewProgress !== null ? ` ${previewProgress}%` : ""}
              </div>
              <progress
                value={previewProgress ?? 0}
                max={100}
                style={{ width: "100%" }}
              />
            </div>
          ) : renderError ? (
            <div style={styles.previewEmpty}>오류: {renderError}</div>
          ) : videoUrl ? (
            // #249 — wrapper를 부모 영역(previewVideoWrap)에 가둔다. 이전 `inline-block`은
            // 비디오 원본 크기로 펼쳐져 PreviewPanel 박스를 벗어났음. 100%×100% flex 컨테이너로
            // 두고 video의 maxWidth/maxHeight 100% + objectFit:contain 으로 letterbox 처리.
            <div
              onMouseEnter={() => setControlsVisible(true)}
              onMouseLeave={() => setControlsVisible(false)}
              style={{
                position: "relative",
                width: "100%",
                height: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                overflow: "hidden",
              }}
            >
              {/* 줌 박스 오버레이 — <video> 의 실제 letterbox 픽셀 사각형(videoRect) 기준으로
                  절대 좌표를 산출한다. CSS aspectRatio 추정 방식은 maxWidth/maxHeight 중 어느
                  축이 바인딩되는지 부정확해 박스 크기가 어긋났다. ref + ResizeObserver로 정확
                  측정한 videoRect를 사용해 video와 정확히 겹치도록 그린다.
                  #359 — showOverlayGuides=false 면 편집 가이드를 숨겨 결과 영상만 보이게 한다. */}
              {showOverlayGuides && (() => {
                const zoomBox = _computeZoomBox(scene?.motions ?? null);
                if (!zoomBox || !videoRect) return null;
                return (
                  <div
                    style={{
                      position: "absolute",
                      left: videoRect.left,
                      top: videoRect.top,
                      width: videoRect.width,
                      height: videoRect.height,
                      pointerEvents: "none",
                    }}
                  >
                    <div
                      style={{
                        position: "absolute",
                        left: `${zoomBox.leftPct}%`,
                        top: `${zoomBox.topPct}%`,
                        width: `${zoomBox.wPct}%`,
                        height: `${zoomBox.hPct}%`,
                        border: "2px solid #FFD700",
                        boxSizing: "border-box",
                        pointerEvents: "none",
                      }}
                      title="clip_zoom 최종 줌 영역 (end_scale 기준)"
                    >
                      <div style={{
                        position: "absolute", top: 2, right: 4,
                        fontSize: 11, color: "#FFD700",
                        textShadow: "0 0 4px rgba(0,0,0,0.8)",
                      }}>
                        최종 줌 영역
                      </div>
                    </div>
                  </div>
                );
              })()}
              <video
                ref={setVideoRefAndEl}
                key={videoUrl}
                src={videoUrl}
                style={{
                  ...styles.previewVideo,
                  aspectRatio: `${outputWidth}/${outputHeight}`,
                  display: "block",
                }}
                // 마우스가 미리보기 영역 안에 있을 때만 컨트롤바 표시 (상시 가림 해소).
                controls={controlsVisible}
                // 이어보기 모드에서는 loop 를 꺼야 video ended 이벤트가 발생한다.
                // 일반 모드(sequentialPlay=false)에서는 기존대로 loop 유지(회귀 방지).
                loop={!sequentialPlay}
                // 이어보기 모드에서 재생이 끝나면 부모에게 알려 다음 씬으로 전환한다.
                onEnded={() => { if (sequentialPlay && onSceneEnded) onSceneEnded(); }}
                // MediaRecorder webm은 duration 메타데이터가 Infinity로 들어와
                // 컨트롤바에 0:00으로 표시되는 경우가 있다. 끝까지 seek해서
                // 실제 길이를 강제 측정하는 표준 우회법.
                onLoadedMetadata={(e) => {
                  const v = e.currentTarget;
                  // footage_audio_volume 더킹은 mp4에 이미 반영됨 — v.volume 으로 다시 곱하지 않는다.
                  if (!isFinite(v.duration) || v.duration === 0) {
                    const onSeeked = () => {
                      v.currentTime = 0;
                      v.removeEventListener("seeked", onSeeked);
                    };
                    v.addEventListener("seeked", onSeeked);
                    try { v.currentTime = 1e10; } catch { /* ignore */ }
                  }
                }}
                // #364 — TimelineBar playhead 동기화. video 의 currentTime 갱신을 부모에게 전달.
                //         씬 별 mp4 라 sec 은 "씬 로컬" 값이며, 부모(Editor)가 누적 시작 sec 을
                //         더해 글로벌 sec 로 환산해 TimelineBar 에 넘긴다.
                onTimeUpdate={(e) => {
                  const v = e.currentTarget;
                  if (onTimeUpdate) onTimeUpdate(v.currentTime || 0);
                }}
              />
              {/* #222 후속: graphic_overlay 드래그 핸들 레이어 */}
              {/* #249 — 드래그 핸들 레이어는 비디오의 실제 표시 사각형을 기준으로 배치해야 한다.
                  wrapper는 100%×100%지만 비디오는 그 안에서 letterbox(contain)되므로 wrapper에
                  맞춘 absolute 레이어는 비디오 영역과 어긋난다. videoRef.getBoundingClientRect()로
                  비디오의 실제 위치/크기를 측정해 레이어를 그 위에 정확히 겹친다.
                  #359 — showOverlayGuides=false 면 드래그 핸들/색 점/이모지 라벨을 숨긴다.
                  실제 합성 결과(드래그 핸들 아닌 백엔드 mp4 자체)는 <video> 가 그리므로
                  결과 영상엔 영향 없음. */}
              {showOverlayGuides && displayedOverlays.length > 0 && (
                <GraphicOverlayLayer
                  videoRef={videoRef}
                  overlays={displayedOverlays}
                  onStartDrag={startGraphicDrag}
                />
              )}
            </div>
          ) : (
            <div style={styles.previewEmpty}>
              {matchedFile ? "파일 로딩 중..." : "footage가 배정되지 않은 씬입니다"}
            </div>
          )}
        </div>
        <div style={styles.previewMeta}>
          <span style={styles.previewSceneNum}>씬 {scene.scene_id}</span>
          {scene.footage && <span style={styles.previewEffectChip}>{scene.footage.type}</span>}
          {/* 미리보기 모드 단일화 — 백엔드 ffmpeg 결과 mp4만 사용. 라벨도 "미리보기"로 통일. */}
          {videoUrl && !isRendering && !renderError && (
            <span
              style={{
                color: "#f59e0b",
                fontSize: 11,
                background: "#2e220a",
                borderRadius: 4,
                padding: "2px 7px",
                flexShrink: 0,
              }}
              title="백엔드 ffmpeg로 합성한 정확한 미리보기"
            >
              미리보기
            </span>
          )}
          <span style={styles.previewDesc}>{scene.description || scene.title}</span>
          {matchedFile && (
            <button
              style={styles.previewRefreshBtn}
              onClick={() => { loadPreview(); }}
              disabled={isRendering}
              title="현재 씬 미리보기를 다시 생성합니다"
            >
              {isRendering ? "생성 중..." : "미리보기 새로고침"}
            </button>
          )}
        </div>
      </div>
      <div
        style={styles.previewDragHandle}
        onMouseDown={handleDragHandleMouseDown}
        title="드래그하여 높이 조절"
      />
    </div>
  );
}

// ClientSimplePreview / ClientOverlayPreview 컴포넌트는 미리보기 단일화에 따라 제거됨.
// 회귀 가능성에 대비해 의존 모듈(clientSceneRenderer, OverlayCanvas의 default export 등) 자체는 보존한다.

// #249 — 그래픽 오버레이 드래그 핸들 레이어
// 비디오의 실제 표시 사각형(getBoundingClientRect 기준, contain letterbox 반영)에 맞춰
// absolute 레이어를 배치한다. wrapper 크기에 맞추면 letterbox 영역에 핸들이 떠버린다.
function GraphicOverlayLayer({
  videoRef,
  overlays,
  onStartDrag,
}: {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  overlays: GraphicOverlay[];
  onStartDrag: (e: React.MouseEvent, overlayId: string) => void;
}) {
  const [rect, setRect] = useState<{ left: number; top: number; width: number; height: number } | null>(null);

  useEffect(() => {
    const videoEl = videoRef.current;
    if (!videoEl) return;
    const parent = videoEl.parentElement;
    if (!parent) return;

    const update = () => {
      const v = videoRef.current;
      const p = v?.parentElement;
      if (!v || !p) return;
      const vr = v.getBoundingClientRect();
      const pr = p.getBoundingClientRect();
      setRect({
        left: vr.left - pr.left,
        top: vr.top - pr.top,
        width: vr.width,
        height: vr.height,
      });
    };

    update();

    const ro = new ResizeObserver(update);
    ro.observe(videoEl);
    ro.observe(parent);

    // 메타데이터 로드 후 비디오 표시 크기가 결정되므로 한번 더 갱신
    const onMeta = () => update();
    videoEl.addEventListener("loadedmetadata", onMeta);
    videoEl.addEventListener("resize", onMeta);
    window.addEventListener("resize", update);

    return () => {
      ro.disconnect();
      videoEl.removeEventListener("loadedmetadata", onMeta);
      videoEl.removeEventListener("resize", onMeta);
      window.removeEventListener("resize", update);
    };
  }, [videoRef]);

  if (!rect || rect.width <= 0 || rect.height <= 0) return null;

  return (
    <div
      style={{
        position: "absolute",
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
        pointerEvents: "none", // 비디오 컨트롤 클릭 차단 방지
      }}
    >
      {overlays.map((g) => {
        const colorMap: Record<string, string> = {
          spotlight: "#fbbf24",
          alert: "#ef4444",
          target: "#f87171",
          reaction: "#fde047",
          smell: "#86efac",
          emoji: "#c084fc",
        };
        const dot = colorMap[g.type] ?? "#c084fc";
        // #288 — emoji 타입은 실제 이모지를 렌더하여 백엔드 결과와 시각적으로 일치시킨다.
        // size(px)는 1080p 기준 백엔드 drawtext fontsize와 1:1. 미리보기는 비디오 실제 표시 크기에
        // 비례하여 스케일 (rect.height / 1080) — 약식 매핑이지만 위치/대략적 크기 감을 잘 전달한다.
        if (g.type === "emoji") {
          const iconKey = String((g.params as Record<string, unknown> | undefined)?.icon_type ?? "question");
          // #emoji-catalog — 96종 카탈로그를 우선 조회하고, 없으면 옛 4종 표로 폴백.
          const emojiChar = EMOJI_BY_KEY[iconKey]
            ?? (EMOJI_ICON_TABLE as Record<string, { emoji: string }>)[iconKey]?.emoji
            ?? "❓";
          const sizePx = Number((g.params as Record<string, unknown> | undefined)?.size ?? 150);
          // 미리보기 높이 / 백엔드 출력 높이(가정 1080) 비율로 스케일
          const previewFontPx = Math.max(12, Math.round(sizePx * (rect.height / 1080)));
          return (
            <div
              key={g.id}
              title={`emoji ${iconKey} (드래그하여 위치 이동)`}
              onMouseDown={(e) => onStartDrag(e, g.id)}
              style={{
                position: "absolute",
                left: `${g.x * 100}%`,
                top: `${g.y * 100}%`,
                transform: "translate(-50%, -50%)",
                fontSize: previewFontPx,
                lineHeight: 1,
                userSelect: "none",
                cursor: "move",
                pointerEvents: "auto",
                textShadow: "0 0 4px rgba(0,0,0,0.6)",
                zIndex: 5,
              }}
            >
              {emojiChar}
            </div>
          );
        }
        return (
          <div
            key={g.id}
            title={`${g.type} (드래그하여 위치 이동)`}
            onMouseDown={(e) => onStartDrag(e, g.id)}
            style={{
              position: "absolute",
              left: `${g.x * 100}%`,
              top: `${g.y * 100}%`,
              width: 14,
              height: 14,
              marginLeft: -7,
              marginTop: -7,
              background: dot,
              border: "2px solid #111",
              borderRadius: "50%",
              cursor: "move",
              pointerEvents: "auto",
              boxShadow: "0 0 0 1px rgba(0,0,0,0.6)",
              zIndex: 5,
            }}
          />
        );
      })}
    </div>
  );
}

// ── Footage Thumbnail ─────────────────────────────────────────────────────

function AssetThumbnail({ projectId, asset }: { projectId: string; asset: FootageAsset }) {
  // #324: Studio.tsx::AssetThumb와 동일한 패턴으로 통일.
  //  - 로컬 푸티지(origin === "local"): 서버 footage_dir에 없으므로 백엔드 호출 시 404.
  //    localFile이 있으면 LocalThumbnail로 브라우저 추출, 없으면 placeholder.
  //  - 서버 푸티지: ServerFootageThumbnail이 IndexedDB 캐시 → 브라우저 추출 → 백엔드 fallback 순으로
  //    동작하며 백엔드 ffmpeg spawn을 절감(#307). 오디오는 컴포넌트 내부에서 백엔드 파형 썸네일로 자동 라우팅.
  if (asset.origin === "local") {
    if (asset.localFile) {
      return <LocalThumbnail file={asset.localFile} alt={asset.filename} style={styles.thumbImg} />;
    }
    return <div style={{ ...styles.thumbImg, background: "#1a1a1a" }} aria-label={asset.filename} />;
  }
  return (
    <ServerFootageThumbnail
      projectId={projectId}
      filename={asset.filename}
      assetType={asset.asset_type}
      alt={asset.filename}
      style={styles.thumbImg}
    />
  );
}

// ── Editor ────────────────────────────────────────────────────────────────

export default function Editor({ projectId, onBack, onOpenStudio, onOpenBgm }: { projectId: string; onBack?: () => void; onOpenStudio?: (assetId?: string) => void; onOpenBgm?: () => void }) {
  const {
    project, setProject, updateScene,
    selectedSceneId, selectScene,
    effectModalOpen, openEffectModal, closeEffectModal,
    mediaFilter, setMediaFilter,
    ttsPlayingSceneId, setTtsPlaying,
    updateSceneTts,
    isProcessing, setProcessing,
    progress, setProgress,
    copiedOverlays, setCopiedOverlays,
    copiedGraphic, setCopiedGraphic,
    copiedEffects, setCopiedEffects,
    highlightedSceneIds,
    nlEditorPanelOpen, setNlEditorPanelOpen,
    nlEditApplyTick,
    // #358 — Undo / Redo
    commitProjectHistory, undo, redo, canUndo, canRedo, clearHistory,
    // #363 — 슬라이더 드래그 종료 시점 history push 진입점
    pushHistorySnapshot,
    // #359 — 오버레이 편집 가이드 토글
    showOverlayGuides, toggleOverlayGuides,
    // #364 — TimelineBar playhead / seek (액션 + seekRequest 만 destructuring)
    //   `previewCurrentTime` 자체는 #371 클린업으로 Editor 가 더 이상 직접 구독하지 않는다.
    //   미리보기 video 가 ~4Hz 로 setPreviewCurrentTime 을 호출하므로 destructuring 으로
    //   받으면 Editor 본체가 ~4Hz 로 통째 리렌더된다.
    //   대신 TimelineBar 가 내부에서 selector 로 직접 구독한다.
    setPreviewCurrentTime,
    previewSeekRequest, requestPreviewSeek,
    // #378 — Phase 5 cut 길이 setter. TimelineBar 는 store selector 로 직접 구독한다.
    setSceneCutDurations,
  } = useStore();

  // #388 — NarrationSegmentInspector 가 TimelineBar 와 같은 cut 캐시를 reactive 하게 참조하도록
  //   Editor 본체에서 selector 구독. 이 값이 갱신되면 인스펙터의 "씬 길이 ≈" 도 즉시 반영.
  //   TimelineBar 는 자기 내부에서 또 한 번 selector 로 구독한다(중복이지만 Editor 본체 리렌더 부담은 동일).
  const sceneCutDurations = useStore((s) => s.sceneCutDurations);

  // 목소리 콤보박스 옆 미리듣기.
  const ttsPreview = useTtsPreview();
  // Google(하드코딩) + Typecast(동적) 병합 목소리 목록. 마운트 시 1회 로드.
  //   현재 프로젝트 tts_voice 가 tc_ 값이라 로드 전 목록에 없으면 임시 option 으로 유지.
  const { voices: mergedVoices } = useVoiceOptions(project?.tts_voice ?? null);
  // 'TTS 생성' 버튼 클릭 시 뜨는 모달. 사용자가 이 프로젝트에 쓸 목소리를
  //   확정하고 시작할 수 있게 한다. null 이면 미노출.
  const [ttsStartVoice, setTtsStartVoice] = useState<string | null>(null);
  // Typecast 목소리 찾기 모달 노출 여부(프로젝트 tts_voice 대상).
  const [typecastModalOpen, setTypecastModalOpen] = useState(false);
  // 현재 프로젝트 tts_voice 가 tc_ 값일 때 콤보박스 옆에 보여줄 이름 라벨.
  const { voices: typecastRaw } = useTypecastRawVoices();
  const currentTypecastName = (() => {
    const v = project?.tts_voice ?? "";
    if (resolveProvider(v) !== "typecast") return null;
    const found = typecastRaw.find((t) => t.voice_id === v);
    return found?.voice_name || v;
  })();

  // #385 — 씬 길이에 영향을 주는 필드(footage trim, footage_speed, duration_hint_sec, footage 교체)
  //   가 갱신되면 sceneCutDurations(Phase 5 캐시) 의 해당 항목을 즉시 invalidate.
  //   캐시 1순위가 옛 값을 잡고 있어 TimelineBar 비율이 변경에 즉시 반영되지 않던 회귀를 해소한다.
  //   (Phase 5 가 다시 실행되면 setSceneCutDurations 가 새 값으로 채워준다.)
  //
  // #388 — 일부 호출처는 footage 객체 전체가 아닌 부분 필드(예: `footage.clip_start_sec` 만 patch)
  //   로 들어올 수 있다. 현재 코드 베이스의 trim 변경 경로는 모두 footage 전체 교체로 들어오지만
  //   회귀 안전을 위해 한 단계 깊이의 필드명 prefix(`footage.`) 가 patch 에 있어도 detect.
  const _patchAffectsDuration = useCallback((patch: Partial<SceneCard>): boolean => {
    if ("duration_hint_sec" in patch) return true;
    if ("footage" in patch) return true;
    if ("footage_speed" in patch) return true;
    if ("footage_speed_mode" in patch || "footage_speed_ramp_hold" in patch || "footage_speed_ramp_accel" in patch || "footage_speed_ramp_base" in patch) return true;
    // 부분 필드 patch 보강(미래 호출처 대비) — 어떤 키든 "footage." 또는 "clip_start_sec"/"clip_end_sec"
    // 단독으로 오더라도 cut 캐시 invalidate 트리거.
    for (const k of Object.keys(patch)) {
      if (k.startsWith("footage.") || k === "clip_start_sec" || k === "clip_end_sec") {
        return true;
      }
    }
    return false;
  }, []);
  const _invalidateSceneCutDuration = useCallback((sceneId: number) => {
    const cur = useStore.getState().sceneCutDurations;
    if (!cur.has(sceneId)) return;
    const next = new Map(cur);
    next.delete(sceneId);
    setSceneCutDurations(next);
  }, [setSceneCutDurations]);

  const audioRef = useRef<HTMLAudioElement>(null);
  // narration/speed 등 빈번 편집 서버 동기화 debounce (씬별 800ms) — patchScene 폭주 방지.
  const narrationSyncTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  // #narration-flush — 디바운스 대기 중인 대본(sceneId → 최신 텍스트).
  // 800ms 안에 새로고침/탭닫기가 일어나면 patchScene 이 발화되지 않아
  // 마지막에 친 글자가 서버에 없고, 다시 열면 "치다 만 텍스트" 가 보인다.
  const pendingNarration = useRef<Map<number, string>>(new Map());
  const speedSyncTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  const audioVolumeSyncTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  // 씬별 TTS 재생 속도 슬라이더 서버 동기화 debounce — 슬라이더 폭주 방지.
  const ttsSpeedSyncTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  // #tts-emotion — 말투 입력 디바운스 타이머(씬별).
  const ttsEmotionSyncTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  // 씬 본체 SFX 패치 — 슬라이더/셀렉트 폭주를 디바운스하기 위해 씬 단위 타이머 보관.
  const sceneSfxSyncTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  // #363 — 슬라이더 드래그 1 회를 한 단계 history snapshot 으로 묶기 위한 추적 ref.
  //   sliderOnly:true 패턴(triggerRefresh:false)으로 들어오는 첫 호출 시점에
  //   "변경 직전 project snapshot" 을 보관한다. 이후 같은 드래그 시퀀스의
  //   onChange 들은 추가 snapshot 을 만들지 않는다 (마지막 손 뗌까지 한 덩어리).
  //   document 의 pointerup/mouseup/touchend 시점에 보관된 snapshot 을
  //   pushHistorySnapshot 으로 1 단계 history 에 push 한 뒤 ref 를 비운다.
  const pendingSliderSnapshotRef = useRef<typeof project>(null);

  /** #363 — 슬라이더/연속 입력 핸들러가 첫 호출 시점에 직전 snapshot 을 보관한다.
   *  이미 보관 중이면 no-op (= 같은 드래그 시퀀스 중간 onChange). */
  const stashSliderSnapshotIfFirst = useCallback(() => {
    if (pendingSliderSnapshotRef.current != null) return;
    const latest = useStore.getState().project;
    if (latest) {
      pendingSliderSnapshotRef.current = JSON.parse(JSON.stringify(latest));
    }
  }, []);
  const [serverFootage, setServerFootage] = useState<import("../types").FootageItem[]>([]);
  const [selectedSceneIds, setSelectedSceneIds] = useState<Set<number>>(new Set());
  // 씬 나누기 다이얼로그 대상 씬 ID(null이면 닫힘).
  const [splitTargetSceneId, setSplitTargetSceneId] = useState<number | null>(null);
  // #362 — 푸티지(소스영상) 변경 다이얼로그 대상 씬 ID(null이면 닫힘).
  const [footageDialogSceneId, setFootageDialogSceneId] = useState<number | null>(null);
  // #380 — 씬 에디터에서 화면 녹화로 footage 추가. 사이드바 헤더의 "+ 화면 녹화" 버튼이 토글한다.
  const [recorderOpen, setRecorderOpen] = useState(false);
  // 클립 라이브러리 삽입 모달 — 열려 있을 때 "어느 씬 다음에 삽입할지"의 인덱스를 담는다.
  // null 이면 닫힘. 인덱스는 SceneInsertHandle 이 넘겨준 정렬 순서 기준 값이다.
  const [clipLibraryInsertIndex, setClipLibraryInsertIndex] = useState<number | null>(null);
  // "중간 이미지 삽입" — 이미지 자산 선택 모달이 열려 있을 때 "어느 씬 다음에 삽입할지"의
  // 정렬 순서 인덱스. null 이면 닫힘. 선택 시 이미지 전용 삽입 씬이 자동 생성된다.
  const [insertImageIndex, setInsertImageIndex] = useState<number | null>(null);
  // ── 씬 전체 클립보드 (#287 후속) ─────────────────────────────────────────
  // `⎘` 복제 버튼은 더 이상 즉시 새 씬을 만들지 않고, 씬 전체를 이 state에 deep copy로
  // 보관한다. 이후 사용자가 원하는 위치에 빈 씬을 추가(or 사이 삽입)한 뒤 새로 노출되는
  // "씬 붙여넣기" 버튼을 눌러야 비로소 해당 씬에 모든 필드가 적용된다.
  //
  // `copiedEffects`(#281: 효과 종류만 append)와는 의도적으로 분리:
  //  - copiedScene:   "씬을 통째로" 다른 위치에 복원 (덮어쓰기, 텍스트/footage/match 포함)
  //  - copiedEffects: "효과만" 다른 씬에 추가 (append, footage/narration 건드리지 않음)
  const [copiedScene, setCopiedScene] = useState<SceneCard | null>(null);

  // 패널 표시 토글 — 작업 공간을 넓힐 때 임의 영역을 숨길 수 있다.
  // localStorage에 저장해 다음 진입 시에도 유지.
  //
  // #374: 푸티지 패널 기본값 OFF 로 변경 (씬 에디터 진입 시 미리보기/씬리스트에 공간을 양보).
  // 토글 자체는 그대로 유지 — 사용자가 펼치고 싶을 때 펼칠 수 있다.
  // 기존 사용자(localStorage 에 footage:true 가 저장된 사용자)도 한 번은 OFF 를 경험하도록
  // 마이그레이션 플래그(`editor_panels_footage_default_off_migrated`)를 두고 옛 키의 footage 만 false 로
  // 덮어쓴다. 마이그레이션이 끝난 뒤에는 사용자가 다시 ON 으로 토글하면 그대로 유지된다.
  const PANEL_KEYS = ["footage", "topbar", "preview", "scenes", "effects"] as const;
  type PanelKey = (typeof PANEL_KEYS)[number];
  const PANEL_LS_KEY = "editor_panels_visible";
  const PANEL_FOOTAGE_OFF_MIGRATION_KEY = "editor_panels_footage_default_off_migrated";
  const [visiblePanels, setVisiblePanels] = useState<Record<PanelKey, boolean>>(() => {
    try {
      const raw = localStorage.getItem(PANEL_LS_KEY);
      const migrated = localStorage.getItem(PANEL_FOOTAGE_OFF_MIGRATION_KEY) === "1";
      if (raw) {
        const parsed = JSON.parse(raw);
        // 옛 사용자: 마이그레이션이 아직 안 됐으면 footage 만 강제로 false 로 덮어쓰고 플래그 set.
        const footageVal = migrated
          ? (parsed.footage === true) // 마이그레이션 후에는 명시적 true 만 ON 으로 간주 (기본 OFF)
          : false;
        const next = {
          footage: footageVal,
          topbar: parsed.topbar !== false,
          preview: parsed.preview !== false,
          scenes: parsed.scenes !== false,
          effects: parsed.effects !== false,
        };
        if (!migrated) {
          try {
            localStorage.setItem(PANEL_LS_KEY, JSON.stringify(next));
            localStorage.setItem(PANEL_FOOTAGE_OFF_MIGRATION_KEY, "1");
          } catch { /* ignore */ }
        }
        return next;
      }
      // 신규 사용자: 마이그레이션 플래그도 함께 set 해서 다음 번에도 같은 경로 (default OFF) 를 탄다.
      try { localStorage.setItem(PANEL_FOOTAGE_OFF_MIGRATION_KEY, "1"); } catch { /* ignore */ }
    } catch { /* ignore */ }
    // #374: footage 기본 OFF.
    return { footage: false, topbar: true, preview: true, scenes: true, effects: true };
  });
  function togglePanel(key: PanelKey) {
    setVisiblePanels((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try { localStorage.setItem(PANEL_LS_KEY, JSON.stringify(next)); } catch { /* ignore */ }
      return next;
    });
  }
  const outputWidth = project?.output_width ?? 1920;
  const outputHeight = project?.output_height ?? 1080;
  const [previewTrigger, setPreviewTrigger] = useState(0);
  // 이어보기(연속 재생) 모드 — true 면 현재 씬부터 마지막 씬까지 자동으로 이어 재생.
  const [isSequentialPlay, setIsSequentialPlay] = useState(false);
  // 이어보기 재생 배속 — 빠르게 훑어볼 때 쓴다. 씬이 바뀌어도 유지되도록 localStorage 에 저장.
  const [sequentialRate, setSequentialRate] = useState<number>(() => {
    try {
      const saved = parseFloat(localStorage.getItem("sequential_play_rate") || "");
      return SEQUENTIAL_RATES.includes(saved) ? saved : 1;
    } catch {
      return 1;
    }
  });
  const [previewHeight, setPreviewHeight] = useState<number>(() => {
    try {
      const saved = localStorage.getItem("preview_panel_height");
      return saved ? parseInt(saved, 10) : 220;
    } catch {
      return 220;
    }
  });
  // 세로 영상(숏츠 등)으로 전환 시 패널 높이를 자동 조정 (사용자가 직접 조절하지 않은 경우)
  useEffect(() => {
    const isPortrait = outputHeight > outputWidth;
    setPreviewHeight(prev => {
      const defaultH = isPortrait ? 480 : 220;
      // 이전 값이 기본값에서 크게 벗어나지 않으면(±50) 자동 조정
      const prevDefault = outputHeight > outputWidth ? 220 : 480;
      if (Math.abs(prev - prevDefault) < 100) return defaultH;
      return prev;
    });
  }, [outputWidth, outputHeight]);
  const [fullRenderTaskId, setFullRenderTaskId] = useState<string | null>(null);
  const [fullRenderStatus, setFullRenderStatus] = useState<"idle" | "uploading" | "rendering" | "done" | "error">("idle");
  const [fullRenderMessage, setFullRenderMessage] = useState<string>("");
  // 렌더 진행률(0~100). current/total 을 % 로 환산해 저장. -1 이면 진행 바 숨김.
  const [fullRenderPct, setFullRenderPct] = useState<number>(-1);
  const [fullRenderOutputUrl, setFullRenderOutputUrl] = useState<string | null>(null);
  // 이번 렌더가 자막(SBV)을 생성했는지. "영상만 렌더링"(skipSubtitle)이면 false 라
  // 결과 카드의 자막 다운로드 링크를 숨긴다(없는 파일 → 404 방지).
  const [fullRenderHasSubtitle, setFullRenderHasSubtitle] = useState<boolean>(true);
  // TTS 전체 재생성(모든 씬 × 모든 세그먼트) — 서버 일괄 엔드포인트 task_id.
  const [regenTtsTaskId, setRegenTtsTaskId] = useState<string | null>(null);
  const [highlightedAssetId, setHighlightedAssetId] = useState<string | null>(null);
  // 씬 태그 사전 (이슈 #211 + #214):
  //  - 마운트 시 1회 로드, 실패 시 빌트인 fallback.
  //  - 태그 사전 편집 모달이 저장/삭제 시 `tagDictVersion`을 bump하여 재로드.
  //  - 재로드된 사전은 setTagDict로 반영되어 SceneTagsEditor도 자동 갱신.
  const [tagDict, setTagDict] = useState<SceneTagDictionary>(() => getBuiltinTagDictionary());
  const [tagDictVersion, setTagDictVersion] = useState<number>(0);
  const [tagDictModalOpen, setTagDictModalOpen] = useState<boolean>(false);
  // 다중 화자 사전 모달 — 씬 카드의 "관리" 버튼 또는 상단 메뉴에서 연다.
  const [speakerModalOpen, setSpeakerModalOpen] = useState<boolean>(false);
  // #294 — 단축키 도움말 모달.
  const [shortcutHelpOpen, setShortcutHelpOpen] = useState<boolean>(false);
  // #382 — narration segment 인스펙터 상태.
  //   pendingSegmentAdd: TimelineBar Alt/Shift+click 으로 신규 segment 추가 후보 시점(offset_sec).
  //   editingSegmentId: 기존 segment 마커 클릭 시 편집 대상.
  const [pendingSegmentAdd, setPendingSegmentAdd] = useState<{ sceneId: number; offsetSec: number } | null>(null);
  const [editingSegmentId, setEditingSegmentId] = useState<{ sceneId: number; segId: string } | null>(null);
  // #384 — TimelineBar 클릭으로 선택된 "TTS 구간 추가 후보 시점".
  //   사용자가 TimelineBar 의 임의 지점을 클릭하면 (sceneId, offsetSec) 가 보관되고,
  //   상단 툴바의 [+ TTS 구간 추가] 버튼이 활성화된다. 버튼을 누르면 pendingSegmentAdd 로
  //   복사되며 NarrationSegmentInspector 가 열린다.
  //   Alt/Shift+클릭의 기존 진입 경로는 그대로 보존.
  const [ttsAnchor, setTtsAnchor] = useState<{ sceneId: number; offsetSec: number } | null>(null);
  useEffect(() => {
    let cancelled = false;
    loadTagDictionary()
      .then((d) => {
        if (!cancelled) setTagDict(d);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [tagDictVersion]);

  // #363 — 슬라이더 드래그 종료(pointerup / mouseup / touchend / blur) 시점에
  //        "드래그 시작 직전 snapshot" 을 한 단계 history 로 push 한다.
  //        applyScenePatch 의 sliderOnly:true 경로가 첫 호출 시 ref 에 snapshot 을 보관하고,
  //        여기서 글로벌 리스너로 그 snapshot 을 비워 한 단계로 묶는다.
  //        - 슬라이더와 무관한 mouseup 도 이 핸들러를 통과하지만, ref 가 비어 있으면 no-op.
  //        - blur 까지 추가하여 number input(키보드 입력 후 탭) 도 한 단계로 묶이도록 함.
  //
  // #371 — 일부 텍스트/색상 input 은 자체 onBlur 에서 `sliderOnly:false` 로 직접 commit
  //        한다. 같은 input 에서 발생한 window blur 가 capture phase 에서 추가로
  //        pendingSliderSnapshotRef 를 pushHistorySnapshot 으로 push 하면 하나의 액션이
  //        Undo 두 단계로 분리된다. 그래서 `data-skip-history-stash` 마커가 붙은 input 의
  //        blur 는 commitIfPending 을 건너뛰도록 분기한다 — 그 input 의 onBlur 핸들러가
  //        직접 commit 한 단일 history 단계만 남는다.
  useEffect(() => {
    const commitIfPending = (evt?: Event) => {
      // 마커가 붙은 input 에서 발생한 blur 는 자체 onBlur 가 commitProjectHistory 로
      // 직접 1 단계 push 하므로, 여기서 추가 push 하지 않는다.
      if (evt && evt.type === "blur" && evt.target instanceof HTMLElement) {
        if (evt.target.hasAttribute("data-skip-history-stash")) {
          // 마커 input → snapshot 은 비우지 않는다(다른 진짜 슬라이더 변경분이
          // 같이 묶여 있을 수 있음). 여기서는 그냥 no-op.
          return;
        }
      }
      const snap = pendingSliderSnapshotRef.current;
      if (!snap) return;
      pendingSliderSnapshotRef.current = null;
      pushHistorySnapshot(snap);
    };
    window.addEventListener("pointerup", commitIfPending, true);
    window.addEventListener("mouseup", commitIfPending, true);
    window.addEventListener("touchend", commitIfPending, true);
    // input/select blur — 키보드 step / 탭 이동도 한 단계로 묶기.
    window.addEventListener("blur", commitIfPending, true);
    return () => {
      window.removeEventListener("pointerup", commitIfPending, true);
      window.removeEventListener("mouseup", commitIfPending, true);
      window.removeEventListener("touchend", commitIfPending, true);
      window.removeEventListener("blur", commitIfPending, true);
    };
  }, [pushHistorySnapshot]);

  useWs(fullRenderTaskId, (msg) => {
    if (msg.type === "progress") {
      setFullRenderMessage((msg as any).message ?? "렌더링 중...");
      const cur = (msg as any).current;
      const tot = (msg as any).total;
      if (typeof cur === "number" && typeof tot === "number" && tot > 0) {
        const pct = Math.max(0, Math.min(100, Math.round((cur / tot) * 100)));
        // 진행 바는 역행하지 않게 단조 증가로만 반영.
        setFullRenderPct((prev) => (pct > prev ? pct : prev));
      }
    }
    if (msg.type === "complete") {
      setFullRenderStatus("done");
      setFullRenderOutputUrl(renderOutputUrl(projectId));
      setFullRenderTaskId(null);
      setFullRenderPct(-1);
      // #378 — 전체 렌더가 끝나면 Phase 5 의 cache/timeline.json 이 갱신됐다.
      //   ETag 가 무효화돼 새 결과가 내려오므로 TimelineBar 가 실제 cut 길이로 갱신된다.
      import("../api/client").then(({ getSceneCutDurations }) =>
        getSceneCutDurations(projectId).then((scenes) => {
          const m = new Map<number, number>();
          for (const it of scenes) {
            if (typeof it?.scene_id === "number" && typeof it?.cut_sec === "number" && it.cut_sec > 0) {
              m.set(it.scene_id, it.cut_sec);
            }
          }
          setSceneCutDurations(m);
        }).catch(() => {})
      );
    }
    if (msg.type === "error") {
      setFullRenderStatus("error");
      setFullRenderMessage((msg as any).message ?? "렌더링 실패");
      setFullRenderTaskId(null);
      setFullRenderPct(-1);
    }
  }, () => {
    // WS가 끊겼을 때 렌더 상태 API로 실제 결과 확인
    if (fullRenderStatus === "rendering") {
      api.get(`/api/projects/${projectId}/render/status`).then((r) => {
        const s = r.data as { status: string; output_file?: string };
        if (s.status === "done") {
          setFullRenderStatus("done");
          setFullRenderOutputUrl(renderOutputUrl(projectId));
        } else if (s.status === "error") {
          setFullRenderStatus("error");
          setFullRenderMessage((s as any).error ?? "렌더링 실패");
        }
        // status가 running이면 아직 진행 중 — 아무것도 하지 않음 (WS만 끊긴 것)
      }).catch(() => {});
    }
    setFullRenderTaskId(null);
  });

  // TTS 전체 재생성 진행률 구독 — 서버 /tts/generate(force=true) 단일 task 의 progress/complete 를 수신.
  //   서버가 모든 씬 × 모든 세그먼트를 한 task 에서 순차 재합성하며, 세그먼트별 voice_name(개별 지정)은
  //   존중되고 None 인 세그먼트만 프로젝트 tts_voice 로 재생성된다(백엔드 _do_generate_tts 정책).
  //   완료 시 서버에서 프로젝트를 다시 읽어 각 세그먼트 duration_sec/synced 등 최신 메타를 반영한다.
  useWs(regenTtsTaskId, (msg) => {
    if (msg.type === "progress") {
      const m = msg as any;
      setProgress({
        current: m.current ?? 0,
        total: m.total ?? 0,
        message: m.message ?? "TTS 재생성 중...",
        phase: "tts",
      });
    }
    if (msg.type === "complete") {
      setRegenTtsTaskId(null);
      const summary = (msg as any).summary ?? {};
      setProgress({
        current: summary.total ?? 0,
        total: summary.total ?? 0,
        message: `TTS 재생성 완료 (성공 ${summary.success ?? 0}${summary.failed ? `, 실패 ${summary.failed}` : ""})`,
        phase: "tts",
      });
      setProcessing(false);
      // 서버 기준 최신 세그먼트 메타를 프론트 store 에 반영하고,
      // 바뀐 TTS duration 기준으로 footage_speed 를 자동 재계산한다.
      //   - 순서 보장: apiGetProject(새 TTS duration 반영) → 그 최신 씬으로 speed 재계산.
      //     옛 duration 으로 계산하면 순서 의존 버그가 재발하므로 반드시 재조회 뒤 계산한다.
      //   - 대상 정책: 데이터 모델에 "자동맞춤 대상" 플래그가 없어, 속도가 조정된
      //     clip 씬(footage_speed !== 1.0)만 재계산한다. 원속도(1.0, 손 안 댐) 씬은 보존.
      //     computeAutoFootageSpeed 가 TTS·clip 없는 씬은 null 로 건너뛴다.
      //   - 저장: setProject + dbSetScenes 로 일괄(개별 patch 금지, 81씬 멈춤 회귀 방지).
      const base = project;  // useWs 콜백은 매 렌더 최신 클로저를 참조하므로 현재 project.
      import("../api/client").then(({ getProject: apiGetProject }) =>
        apiGetProject(projectId).then((p) => {
          if (!base) return;
          const freshById = new Map<number, SceneCard>();
          for (const s of p.scenes ?? []) freshById.set(s.scene_id, s);
          let respeed = 0;
          // 1) 새 TTS 메타 병합 후 2) 그 씬으로 speed 재계산 — 한 번의 map 으로.
          const newScenes = base.scenes.map((scene) => {
            const fresh = freshById.get(scene.scene_id);
            const merged: SceneCard = fresh
              ? { ...scene, narration_segments: fresh.narration_segments, tts: fresh.tts }
              : scene;
            const curSpeed = merged.footage_speed ?? 1.0;
            if (curSpeed === 1.0) return merged;  // 원속도 씬은 보존.
            const speed = computeAutoFootageSpeed(merged, serverFootage);
            if (speed === null || speed === curSpeed) return merged;
            respeed += 1;
            // footage_speed 변경 시 오버레이 출력 타임라인도 함께 재조정(씬 밖 밀림 방지).
            return {
              ...rescaleSceneOverlayTimingsForSpeed(
                merged, { footage_speed: speed }, getSceneRawFootageDuration(merged, serverFootage),
              ),
              footage_speed: speed,
            };
          });
          const next = { ...base, scenes: newScenes };
          setProject(next);
          dbSetScenes(projectId, newScenes).catch((e) =>
            console.error("[regenAllTts] 씬 저장 실패:", e)
          );
          if (respeed > 0) {
            ui.success(`TTS 재생성 + 영상 속도 재조정 ${respeed}개 완료`);
          }
          if (selectedSceneId !== null) setPreviewTrigger((n) => n + 1);
        }).catch(() => {})
      );
    }
    if (msg.type === "error" && !("scene_id" in (msg as any))) {
      setRegenTtsTaskId(null);
      setProcessing(false);
      ui.error(`TTS 재생성 실패: ${(msg as any).message ?? "알 수 없는 오류"}`);
    }
  }, () => {
    // WS 끊김 — task 종료로 간주하고 상태 정리.
    setRegenTtsTaskId(null);
    setProcessing(false);
  });

  // #358 — Undo/Redo 실행 헬퍼. 서버/IndexedDB 동기화까지 한 번에 수행한다.
  // 단순 store 변경만으로는 새로고침 후 잃을 수 있으므로 pushProjectState로 진실의 원천을 일치시킨다.
  const handleUndo = useCallback(async () => {
    if (!canUndo()) return;
    const restored = undo();
    if (!restored) return;
    // IndexedDB / 서버 동기화. preview-cache는 정확한 정합성을 위해 전체 무효화하지 않고
    // 사용자가 "미리보기 새로고침" 또는 다음 편집 시 자연스럽게 갱신되도록 둔다.
    try {
      const { dbSaveProject: _save } = await import("../lib/db");
      await _save(restored);
    } catch { /* ignore */ }
    try {
      const { pushProjectState } = await import("../api/client");
      await pushProjectState(projectId, restored);
    } catch { /* ignore */ }
    // 미리보기는 한 번 갱신 — 현재 선택 씬이 바뀌었을 수 있다.
    setPreviewTrigger((n) => n + 1);
    ui.info("되돌리기 적용", 2000);
  }, [canUndo, undo, projectId]);

  const handleRedo = useCallback(async () => {
    if (!canRedo()) return;
    const restored = redo();
    if (!restored) return;
    try {
      const { dbSaveProject: _save } = await import("../lib/db");
      await _save(restored);
    } catch { /* ignore */ }
    try {
      const { pushProjectState } = await import("../api/client");
      await pushProjectState(projectId, restored);
    } catch { /* ignore */ }
    setPreviewTrigger((n) => n + 1);
    ui.info("다시 실행 적용", 2000);
  }, [canRedo, redo, projectId]);

  // #narration-flush — 새로고침/탭닫기 직전, 아직 디바운스 대기 중인 대본을 즉시 보낸다.
  //
  // 브라우저는 unload 시점에 일반 fetch 를 취소해버리므로 navigator.sendBeacon 을 쓴다
  // (백그라운드로 보장 전송). PATCH 는 beacon 이 지원하지 않아 전용 POST 경로를 쓴다.
  // visibilitychange(hidden) 에서도 한 번 흘려보내 모바일/탭전환도 함께 커버한다.
  useEffect(() => {
    const flush = () => {
      const pend = pendingNarration.current;
      if (pend.size === 0) return;
      for (const [sceneId, text] of pend) {
        try {
          const blob = new Blob(
            [JSON.stringify({ narration_text: text })],
            { type: "application/json" },
          );
          navigator.sendBeacon?.(
            `/api/projects/${projectId}/scenes/${sceneId}/narration-flush`,
            blob,
          );
        } catch { /* 전송 실패해도 IndexedDB 사본은 남아 있다 */ }
      }
      pend.clear();
    };
    const onHide = () => { if (document.visibilityState === "hidden") flush(); };
    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", flush);
    document.addEventListener("visibilitychange", onHide);
    return () => {
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("beforeunload", flush);
      document.removeEventListener("visibilitychange", onHide);
    };
  }, [projectId]);

  // 프로젝트 진입/교체 시 history 초기화.
  //
  // #no-history-restore — 예전에는 IndexedDB 에 저장해 둔 undo 히스토리를 새로고침 후
  // 되살리고 "Undo 히스토리 N단계 복구됨" 토스트를 띄웠다(#369). 그런데 그 히스토리는
  // 새로고침 이전 시점의 프로젝트 스냅샷이라, 복원 직후 사용자가 Cmd+Z 를 한 번만 눌러도
  // 방금 저장한 편집이 통째로 옛 상태로 되돌아갔다(실사고: 대본 여러 개가 이전 값으로 롤백).
  // 세션이 끝나면 히스토리도 함께 끝나는 것이 안전하다 — 복원하지 않는다.
  useEffect(() => {
    clearHistory();
  }, [projectId, clearHistory]);

  // #no-history-restore — undo 히스토리를 IndexedDB 에 영속화하던 로직(#369/#372) 제거.
  //
  // 저장해 둔 히스토리는 새로고침 후 복원되면서 "새로고침 이전 스냅샷" 을 되살렸고,
  // 그 상태에서 Cmd+Z 한 번이면 방금 저장한 편집이 통째로 롤백됐다.
  // 히스토리는 세션 안에서만 의미가 있으므로 저장하지 않는다.
  // 진입 시 남아 있는 옛 기록은 한 번 청소한다(이전 버전이 남긴 데이터 제거).
  useEffect(() => {
    (async () => {
      try {
        const m = await import("../lib/db");
        await m.dbClearSceneHistory(projectId);
      } catch { /* storage 접근 실패는 무시 */ }
    })();
  }, [projectId]);

  // #294 — 전역 키보드 단축키.
  //  - Cmd/Ctrl+B : BGM 편집
  //  - Cmd/Ctrl+Z : Undo (#358)
  //  - Cmd/Ctrl+Shift+Z / Cmd/Ctrl+Y : Redo (#358)
  //  - H          : 오버레이 가이드 토글 (#359, input 포커스 외 only)
  //  - ?          : 단축키 도움말 모달 토글
  // 렌더링 단축키는 브라우저 하드 리프레시(⌘+Shift+R)와 충돌해 제거.
  // input/textarea/select 포커스 중에는 ?(도움말)/H 단축키를 무시해 텍스트 입력을 방해하지 않는다.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      const target = e.target as HTMLElement | null;
      const inField = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
      if (mod && !e.shiftKey && (e.key === "b" || e.key === "B")) {
        e.preventDefault();
        if (onOpenBgm) handleOpenBgmSmart();
        return;
      }
      // #358 Undo / Redo
      if (mod && !e.shiftKey && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        void handleUndo();
        return;
      }
      if (mod && ((e.shiftKey && (e.key === "z" || e.key === "Z")) || (!e.shiftKey && (e.key === "y" || e.key === "Y")))) {
        e.preventDefault();
        void handleRedo();
        return;
      }
      // #359 — H 키로 오버레이 가이드 토글
      if (!mod && !inField && (e.key === "h" || e.key === "H")) {
        e.preventDefault();
        toggleOverlayGuides();
        return;
      }
      if (!mod && !inField && (e.key === "?" || (e.key === "/" && e.shiftKey))) {
        e.preventDefault();
        setShortcutHelpOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onOpenBgm, handleUndo, handleRedo, toggleOverlayGuides]);

  const scenes = (project?.scenes ?? []).slice().sort((a, b) => a.scene_id - b.scene_id);
  // 사이드바 footage 목록은 서버 footage_dir이 진실의 출처.
  // 사용자가 부여한 레이블·crop 등 메타는 studio_assets에서 filename 매칭으로 덮어쓴다.
  const studioAssets = project?.studio_assets ?? [];
  const studioAssetByFilename = new Map(studioAssets.map((a) => [a.filename, a]));

  // studio_assets에 등록된 파일만 사이드바에 표시한다.
  // 사용자가 footage 스튜디오에서 등록한 자산만 의도된 사용 대상이고,
  // footage_dir에 같이 있는 부산물(임시 변환 mp4 등)은 제외.
  // studio_assets가 비어 있는 과거 프로젝트는 폴백으로 서버 목록 전체 표시(회귀 회피).
  const assets: FootageAsset[] = (studioAssets.length > 0
    ? serverFootage.filter((f) => studioAssetByFilename.has(f.filename))
    : serverFootage
  ).map((f) => {
    const meta = studioAssetByFilename.get(f.filename);
    return {
      asset_id: meta?.asset_id ?? f.filename,
      filename: f.filename,
      label: meta?.label || f.filename.replace(/\.[^.]+$/, ""),
      asset_type: f.file_type,
      source: meta?.source ?? "imported",
      created_at: meta?.created_at ?? "",
      crop: meta?.crop ?? null,
      origin: "server" as const,
    };
  });
  // #290 — 오버레이 SFX UI에서 사용. studio_assets 중 asset_type="audio"만 추출.
  //         서버 footage에는 audio asset_type이 없을 수 있으므로 studioAssets에서만 가져온다.
  const audioAssets: FootageAsset[] = studioAssets.filter((a) => a.asset_type === "audio");
  // 이미지 오버레이 자산 풀 — 서버 footage 인덱스(file_type="image")를 우선,
  // studioAssets에 있는 메타와 머지하여 origin/label 등 추가 정보를 보존한다.
  const imageAssets: FootageAsset[] = assets.filter((a) => a.asset_type === "image");
  const filteredAssets = assets.filter((a) =>
    // #289/#290 — 오디오 자산은 footage 사이드바에서 제외 (BGM/SFX 전용).
    a.asset_type !== "audio" &&
    (a.filename.toLowerCase().includes(mediaFilter.toLowerCase()) ||
     a.label.toLowerCase().includes(mediaFilter.toLowerCase()))
  );
  const selectedScene = scenes.find((s) => s.scene_id === selectedSceneId) ?? null;

  // ── 이어보기(연속 재생) ────────────────────────────────────────────────
  // "이어보기" 버튼: 현재 선택 씬부터 재생 시작. 이어보기 중엔 "멈춤" 으로 토글.
  //   선택된 씬이 없으면 첫 씬을 선택해 시작한다.
  function toggleSequentialPlay() {
    if (isSequentialPlay) {
      setIsSequentialPlay(false);
      return;
    }
    if (scenes.length === 0) return;
    if (selectedSceneId === null || !scenes.some((s) => s.scene_id === selectedSceneId)) {
      selectScene(scenes[0].scene_id);
    }
    setIsSequentialPlay(true);
  }

  // 현재 씬 video 재생이 끝났을 때 — 다음 씬(배열 순서상 현재 다음)이 있으면 전환,
  //   없으면(마지막 씬) 이어보기 모드 종료.
  function handleSceneEnded() {
    const idx = scenes.findIndex((s) => s.scene_id === selectedSceneId);
    if (idx >= 0 && idx < scenes.length - 1) {
      selectScene(scenes[idx + 1].scene_id);
    } else {
      setIsSequentialPlay(false);
    }
  }

  // ── 이어보기 적응형 프리페치 ────────────────────────────────────────────
  //   기존에는 현재 씬이 ended 된 *뒤에야* 다음 씬 mp4 요청을 시작해 전환마다 공백이 생겼다.
  //   여기서는 이어보기 중에만, 프로젝트 상태(앞선 씬들의 재생 길이 + 실측 로딩시간 EMA + 배속)로
  //   필요한 깊이를 산출해 그만큼만 미리 받아 둔다. PreviewPanel 은 loadPreview 진입 시
  //   prefetchStore.take 로 적중분을 꺼내 즉시 표시한다.
  //
  //   제약 준수:
  //     - sequentialPlay=false 이면 프리페치하지 않는다(일반 편집 중 서버 부하 금지).
  //     - 이어보기 종료/언마운트 시 clearAll 로 blob 전량 revoke + pending abort.
  //     - 씬 점프/진행 시 retainOnly 로 창(window) 밖 엔트리를 즉시 해제.
  //     - 편집 시에는 applyScenePatch 등 preview-cache DELETE 경로에서 invalidatePrefetch 호출.
  //     - 실패는 조용히 무시(prefetchStore 내부에서 삼킴) → 기존 경로로 폴백.
  const loadEstimateRef = useRef<number | null>(null);
  // 프리페치 판단에 쓰는 최신 값들을 ref 로 들고 있는다(effect deps 폭주 방지).
  const prefetchDepsRef = useRef({ scenes, serverFootage, sceneCutDurations, sequentialRate, projectId });
  prefetchDepsRef.current = { scenes, serverFootage, sceneCutDurations, sequentialRate, projectId };

  useEffect(() => {
    if (!isSequentialPlay) return;
    if (selectedSceneId === null) return;

    const {
      scenes: curScenes,
      serverFootage: curFootage,
      sceneCutDurations: curCuts,
      sequentialRate: rate,
      projectId: pid,
    } = prefetchDepsRef.current;

    const idx = curScenes.findIndex((s) => s.scene_id === selectedSceneId);
    if (idx < 0) return;

    const upcoming = curScenes.slice(idx + 1);
    const upcomingDurations = upcoming.map((s) =>
      _computeSceneVisualDuration(s, curFootage, curCuts),
    );
    // 현재 씬 잔여 시간은 정확히 알기 어렵다(video 가 아직 로딩 중일 수 있다).
    //   보수적으로 "현재 씬 전체 길이"를 상한으로 쓰지 않고, 갓 전환된 직후라는 점을 이용해
    //   전체 길이를 잔여로 본다. 과대평가 시 깊이가 1 더 늘 뿐이고 상한이 막아 준다.
    const currentRemainingSec = _computeSceneVisualDuration(curScenes[idx], curFootage, curCuts);

    const depth = computePrefetchDepth({
      upcomingDurations,
      currentRemainingSec,
      estLoadSec: loadEstimateRef.current ?? undefined,
      playbackRate: rate,
    });

    const targets = upcoming.slice(0, depth).map((s) => s.scene_id);
    // 창 밖(이미 지나갔거나 깊이 밖) 엔트리는 즉시 해제 — 메모리 보호.
    retainPrefetch(pid, new Set(targets));

    for (const sid of targets) {
      if (hasPrefetch(pid, sid)) continue;
      startPrefetch(
        pid,
        sid,
        (signal) => getScenePreview(pid, sid, signal),
        (elapsed) => {
          loadEstimateRef.current = updateLoadEstimate(loadEstimateRef.current, elapsed);
        },
      );
    }
    // selectedSceneId 가 바뀔 때마다(=씬 전환/점프) 재산출. isSequentialPlay 토글도 트리거.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSequentialPlay, selectedSceneId]);

  // 이어보기를 끄면 진행 중 프리페치를 전부 취소하고 blob 을 해제한다.
  //   (씬 점프로 이어보기가 자동 해제되는 경로 포함 — Editor.tsx 의 씬 클릭 핸들러가
  //    setIsSequentialPlay(false) 를 호출하므로 이 effect 가 정리를 맡는다.)
  useEffect(() => {
    if (isSequentialPlay) return;
    clearAllPrefetch(projectId);
  }, [isSequentialPlay, projectId]);

  // 언마운트 시 잔여 blob 전량 해제 (누수 금지).
  useEffect(() => {
    return () => { clearAllPrefetch(projectId); };
  }, [projectId]);

  // 에디터 진입 시 서버(진실의 원천)에서 프로젝트를 1회 재조회해 프론트 state 를 갱신한다.
  //   과거: store 에 같은 projectId 가 있으면 즉시 return → 서버를 다시 안 읽어, 서버 state.json
  //   을 고쳐도 브라우저 React state 는 옛 데이터 그대로였다(직접 수정한 서버 데이터를 stale
  //   브라우저가 이후 patch/register 로 되덮어쓰는 오염 회귀의 한 원인).
  //   현재: 진입 시(projectId 단위 1회) 서버 getProject 로 갱신. 서버가 진실이므로 진입 시점의
  //   서버 상태를 따르는 게 맞다. 서버 실패 시에만 IndexedDB 로 폴백한다.
  //   포커스마다 refetch 는 편집 중 작업을 덮을 수 있어 하지 않고, projectId 진입 1회로 한정한다.
  const _serverRefetchedRef = useRef<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const alreadyRefetched = _serverRefetchedRef.current === projectId;
      // 서버 재조회를 이 projectId 에 대해 이미 했고 store 에도 있으면 스킵(포커스 재마운트 등).
      const current = useStore.getState().project;
      if (alreadyRefetched && current?.project_id === projectId) return;

      // 진입 시 1회 서버 재조회 — 서버가 진실의 원천.
      const { getProject: apiGetProject } = await import("../api/client");
      const server = await apiGetProject(projectId).catch(() => null);
      if (cancelled) return;
      if (server && Array.isArray((server as any).scenes)) {
        _serverRefetchedRef.current = projectId;
        setProject(server as any);
        // 이후 바깥 변경 동기화(syncProjectFromServer)의 기준점.
        rememberSync(projectId, server as any, server as any);
        return;
      }
      // 서버 실패 — IndexedDB 폴백(오프라인/서버 미기동 등).
      if (current?.project_id === projectId) return;
      const { getProject: dbGet } = await import("../lib/project");
      const local = await dbGet(projectId).catch(() => null);
      if (cancelled) return;
      if (local) setProject(local);
    })();
    return () => { cancelled = true; };
  }, [projectId]);

  // 서버의 바깥 변경을 열린 에디터에 반영 — 새로고침 없이도 현재 서버 상태가 보이도록.
  //   진입 1회 재조회만으로는 이후 에이전트/스크립트가 고친 내용이 안 보였다(2026-11-05).
  //   5초 폴링 + 창 복귀(focus/visibilitychange) 시 동기화. 로컬에서 편집 중인 항목은
  //   lib/serverSync 규칙에 따라 덮지 않는다. 숨겨진 탭에서는 폴링하지 않는다.
  useEffect(() => {
    let stopped = false;
    let busy = false;
    const tick = async () => {
      if (stopped || busy || document.visibilityState !== "visible") return;
      busy = true;
      try { await syncProjectFromServer(projectId); } finally { busy = false; }
    };
    const timer = window.setInterval(tick, 5000);
    const onVisible = () => { if (document.visibilityState === "visible") tick(); };
    window.addEventListener("focus", tick);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", tick);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [projectId]);

  // 프로젝트 진입 시 무조건 pushProjectState 하던 로직 — 제거됨.
  //   과거에는 IndexedDB store 를 서버로 밀어 "동기화" 를 보장하려 했지만,
  //   store 가 stale 이면 서버 최신본을 옛 상태로 덮어씌우는 회귀 원인이 됐다
  //   (사례: 2026-07-16 여름나기 1부 프로젝트에서 매칭 21개 유실).
  //   서버가 진실의 원천 정책상, 진입 시 push 는 오히려 위험하다. 필요한 동기화는
  //   각 편집 액션이 개별 patch 로 이미 수행하고 있다.

  // 마지막 선택 씬 복원 (projectId별 localStorage). scenes 로드된 후 복원.
  // 사용자가 다시 들어왔을 때 마지막 작업 씬으로 자동 포커스되어 작업 연속성 확보.
  // sessionStorage가 아닌 localStorage라 브라우저 닫고 다시 와도 유지.
  // 복원 후 scrollIntoView로 씬 카드를 화면에 보이게.
  useEffect(() => {
    if (scenes.length === 0) return;
    if (selectedSceneId !== null && scenes.some((s) => s.scene_id === selectedSceneId)) return;
    try {
      const key = `last_scene:${projectId}`;
      const raw = localStorage.getItem(key);
      if (!raw) return;
      const lastId = parseInt(raw, 10);
      if (Number.isFinite(lastId) && scenes.some((s) => s.scene_id === lastId)) {
        selectScene(lastId);
        // 다음 paint 사이클에서 DOM 쿼리 후 스크롤 — data-scene-id로 식별
        requestAnimationFrame(() => {
          const el = document.querySelector(`[data-scene-id="${lastId}"]`);
          el?.scrollIntoView({ behavior: "smooth", block: "center" });
        });
      }
    } catch { /* localStorage 비활성 환경 무시 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, scenes.length]);

  // selectedSceneId 변경 시 localStorage에 저장 (다음 진입 시 복원용)
  useEffect(() => {
    try {
      const key = `last_scene:${projectId}`;
      if (selectedSceneId !== null) {
        localStorage.setItem(key, String(selectedSceneId));
      }
    } catch { /* ignore */ }
  }, [projectId, selectedSceneId]);

  // footage 목록: 로컬 폴더 핸들 + 서버 footage API 항상 병렬 로드
  useEffect(() => {
    // 서버 footage는 duration 정보를 위해 항상 로드
    api.get<import("../types").FootageItem[]>(`/api/projects/${projectId}/footage`)
      .then((r) => setServerFootage(r.data))
      .catch(() => {});
    // #378 — Phase 5 cut 길이 fetch. cache/timeline.json 부재 시 빈 배열 → store Map 비움.
    //   TimelineBar 는 빈 Map 일 때 ②~⑥ 폴백을 그대로 사용한다.
    import("../api/client").then(({ getSceneCutDurations }) =>
      getSceneCutDurations(projectId).then((scenes) => {
        const m = new Map<number, number>();
        for (const it of scenes) {
          if (typeof it?.scene_id === "number" && typeof it?.cut_sec === "number" && it.cut_sec > 0) {
            m.set(it.scene_id, it.cut_sec);
          }
        }
        setSceneCutDurations(m);
      }).catch(() => {})
    );
    // studio_assets 레이블이 Studio에서 바뀌었을 수 있으므로 항상 IndexedDB에서 갱신
    function refreshStudioAssets() {
      import("../lib/project").then(({ getProject: dbGet }) =>
        dbGet(projectId).then((p) => {
          const cur = useStore.getState().project;
          if (cur?.project_id === projectId) setProject({ ...cur, studio_assets: p.studio_assets });
        }).catch(() => {})
      );
    }
    refreshStudioAssets();
    document.addEventListener("visibilitychange", refreshStudioAssets);
    return () => {
      document.removeEventListener("visibilitychange", refreshStudioAssets);
    };
  }, [projectId]);

  // 자연어 편집 apply/undo 후 백엔드 RenderSpec 캐시가 갱신됐다.
  // (NL 편집은 RenderSpec 레벨에서 작동 — Project.scenes 의 high-level 필드는 그대로지만
  //  씬별 preview-cache 는 무효화되어야 새로 반영된다.)
  // 모든 씬의 preview-cache 를 삭제하고 미리보기 새로고침 시그널을 띄운다.
  // #316 — 초기 mount 시에는 ref 가드로 호출 회피 (tick=0 이므로 effect 자체가 무동작).
  const _nlEditTickRef = useRef(0);
  useEffect(() => {
    if (!project || project.project_id !== projectId) return;
    if (nlEditApplyTick === _nlEditTickRef.current) return;
    _nlEditTickRef.current = nlEditApplyTick;
    if (nlEditApplyTick === 0) return; // 마운트 시 의도치 않은 무효화 방지
    (async () => {
      // 1) 모든 씬의 preview-cache 삭제 — 다음 미리보기 호출이 강제 재생성
      await Promise.all(
        (project?.scenes ?? []).map((s) =>
          _invalidateScenePreview(projectId, s.scene_id)
        )
      );
      // 2) 미리보기 새로고침 트리거 (선택된 씬이 있다면 재로드)
      setPreviewTrigger((n) => n + 1);
    })();
  }, [nlEditApplyTick, projectId, project?.project_id]);

  // 앱 재시작 / 마이그레이션 후 TTS blob URL 복원
  useEffect(() => {
    if (!project || !Array.isArray(project.scenes)) return;
    for (const scene of project.scenes) {
      // tts audio_url은 blob 복원 시 미사용
      // blob: URL은 페이지 재로드 시 무효화되므로 항상 IndexedDB에서 재복원
      const needsRestore = scene.tts?.status === "ready";
      if (needsRestore) {
        getTtsBlobUrl(projectId, scene.scene_id).then((blobUrl) => {
          if (blobUrl) updateSceneTts(scene.scene_id, { ...scene.tts!, audio_url: blobUrl });
        });
      }
    }
  }, [projectId, project?.project_id]);

  // 씬 footage 를 주어진 asset 으로 교체하는 핵심 로직.
  // drag&drop 핸들러와 #362 FootageSourceDialog 가 모두 이 함수를 호출한다.
  // #365 — trim 이 주어지면 footage(clip).clip_start_sec / clip_end_sec 에 기록한다.
  //         clip 타입(=비디오)인 경우에만 적용되며, 그 외 footage 타입은 무시한다.
  async function reassignSceneFootage(
    sceneId: number,
    asset: FootageAsset,
    trim?: { start: number; end: number },
  ) {
    commitProjectHistory(); // #358 — footage 재배정 직전 snapshot
    let footage = makeFootageFromFilename(asset.filename);
    if (trim && footage && footage.type === "clip") {
      // 클램프 보호 — 음수/역전된 값 방어.
      const cs = Math.max(0, trim.start);
      const ce = Math.max(cs + 0.05, trim.end);
      footage = { ...footage, clip_start_sec: cs, clip_end_sec: ce };
    }
    // 서버에 새 footage 파일 업로드 + 캐시 삭제 + scene patch (모두 loadPreview 트리거 전에)
    await _invalidateScenePreview(projectId, sceneId);
    const footageBlob = await fetchFootageBlob(projectId, asset.filename).catch(() => null as Blob | null);
    if (footageBlob) {
      await uploadSceneFootage(projectId, sceneId, footageBlob, asset.filename).catch(() => {});
    }
    // footage를 교체할 때는 이전 match(source_file/trim_start/trim_end)가 새 footage와
    // 어긋나므로 함께 클리어한다. 그래야:
    //   - DurationStatus의 match.trim_end - trim_start 폴백이 옛 길이를 표시하지 않고
    //   - 백엔드 렌더가 옛 source_file로 영상을 잘못 깔지 않는다(렌더는 match가 있으면 우선시).
    // 새 매칭이 필요하면 사용자가 다시 매칭을 돌리거나 수동 매칭으로 채운다.
    await patchScene(projectId, sceneId, { footage, match: null }).catch(() => {});
    updateScene(sceneId, { footage, match: null });
    // #385 — footage 교체는 씬 길이를 직접 바꾼다. Phase 5 cut 캐시도 옛 footage 기준이라 무효.
    _invalidateSceneCutDuration(sceneId);
    await _localUpdateScene(projectId, sceneId, { footage, match: null });
    setPreviewTrigger((n) => n + 1);
  }

  async function handleFootageDrop(e: DragEndEvent) {
    const { active, over } = e;
    if (!over) return;
    const assetId = active.id as string;
    const sceneId = Number(over.id);
    const asset = assets.find((a) => a.asset_id === assetId);
    if (!asset) return;
    await reassignSceneFootage(sceneId, asset);
  }

  function handleNarrationChange(sceneId: number, text: string) {
    updateScene(sceneId, { narration_text: text });
    // ⚠ 여기서 dbUpdateScene 을 부르지 않는다.
    //   이름과 달리 로컬 전용이 아니다. lib/project.ts 의 updateScene 은
    //     getProject(서버 GET, 프로젝트 전체) → updateProject(또 getProject → PUT register)
    //   로 이어져, **글자 하나마다 네트워크 3 회**(전체 조회 2 + 전체 씬 쓰기 1)가 나간다.
    //   (#413 에서 getProject 가 "서버 우선" 으로 바뀌며 로컬 함수가 아니게 됐다.)
    //   씬 45 개 기준 요청 한 번에 80KB 를 읽어 타이핑이 멈추고 register 가 폭주한다.
    //   로컬 캐시 반영은 아래 디바운스 블록에서 patchScene 과 함께 1 회만 수행한다.
    const scene = scenes.find((s) => s.scene_id === sceneId);
    if (scene?.tts) {
      updateSceneTts(sceneId, { ...scene.tts, synced: false });
    }
    // 서버 동기화 (디바운스 800ms) — 다른 PC/origin에서도 narration이 보이도록 진실의 원천 갱신.
    const timers = narrationSyncTimers.current;
    const prev = timers.get(sceneId);
    if (prev) clearTimeout(prev);
    pendingNarration.current.set(sceneId, text);
    const t = setTimeout(() => {
      timers.delete(sceneId);
      pendingNarration.current.delete(sceneId);
      // #stale-write — 서버가 돌려준 narration_rev 를 로컬에도 반영한다.
      // 이걸 안 하면 로컬 사본이 낡은 rev 를 계속 들고 있어, 주기적 전체 push 가
      // "옛 리비전 + 새 텍스트" 로 서버에 거부당해 편집이 반영되지 않는다.
      patchScene(projectId, sceneId, { narration_text: text })
        .then((updated: any) => {
          const rev = updated?.narration_rev;
          // 로컬 캐시(IndexedDB)에도 최종 텍스트 + rev 를 한 번에 기록.
          // 타이핑 중이 아니라 디바운스 종료 시점이므로 네트워크 3 회가 1 회로 줄어든다.
          const patch: Record<string, unknown> = { narration_text: text };
          if (typeof rev === "number") {
            patch.narration_rev = rev;
            updateScene(sceneId, { narration_rev: rev } as any);
          }
          // 로컬 캐시만 갱신 — 서버는 바로 위 patchScene 이 이미 반영했다.
          void _localUpdateScene(projectId, sceneId, patch);
        })
        .catch(() => {
          // 서버 실패 시에도 로컬에는 남겨 둔다(오프라인 편집 보호).
          void _localUpdateScene(projectId, sceneId, { narration_text: text });
        });
    }, 800);
    timers.set(sceneId, t);
  }

  function handlePlayTts(scene: SceneCard) {
    if (ttsPlayingSceneId === scene.scene_id) {
      audioRef.current?.pause();
      setTtsPlaying(null);
      return;
    }
    if (!scene.tts || scene.tts.status !== "ready") return;
    setTtsPlaying(scene.scene_id);
    if (audioRef.current) {
      audioRef.current.src = scene.tts.audio_url;
      audioRef.current.play();
    }
  }

  // 개별 씬 TTS 재생성 — 서버 경로("서버가 진실의 원천").
  //   과거: synthesizeTtsAndSave(브라우저 합성) → tts-upload 로 서버 덮어쓰기.
  //     브라우저 React state 의 stale narration_text 로 합성해 서버를 오염시키는 회귀 원인이었다.
  //   현재: generateTts(force=true) 단일 씬 대상 → 서버가 서버 state 의 텍스트로 재합성한다.
  //     완료/진행률/최신 메타 재조회는 전체 재생성과 동일한 regenTtsTaskId useWs 구독이 처리한다.
  async function handleRegenTts(sceneId: number) {
    const scene = scenes.find((s) => s.scene_id === sceneId);
    // 세그먼트 또는 레거시 narration_text 중 하나라도 텍스트가 있으면 대상.
    const hasText =
      (scene?.narration_segments || []).some((seg) => seg.text?.trim()) ||
      !!scene?.narration_text?.trim();
    if (!hasText) return;
    updateSceneTts(sceneId, { audio_url: "", duration_sec: 0, status: "generating", synced: false });
    // 미리보기 캐시 무효화 — TTS 길이가 바뀌면 영상 길이도 다시 맞춰야 함.
    await _invalidateScenePreview(projectId, sceneId);
    try {
      // voice_name 미전달 → 서버가 프로젝트 tts_voice 기본 + 세그먼트별 개별 voice 존중.
      const taskId = await generateTts(projectId, [sceneId], true);
      setRegenTtsTaskId(taskId);
      // 진행률/완료(getProject 재조회 → store 반영 → 미리보기 트리거)는 regenTtsTaskId useWs 담당.
    } catch (e: unknown) {
      updateSceneTts(sceneId, { audio_url: "", duration_sec: 0, status: "error", synced: false });
      ui.error(`TTS 생성 실패: ${(e as Error).message}`);
    }
  }

  // 모든 씬의 모든 나레이션 세그먼트를 일괄 재생성한다.
  //   개별 목소리(voice_name)가 지정된 세그먼트는 그 목소리를 유지하고,
  //   미지정(None) 세그먼트만 프로젝트 목소리(tts_voice)로 재합성한다.
  //   서버 /tts/generate(force=true) 단일 백그라운드 task 가 모든 씬 × 세그먼트를 순차 처리하고,
  //   진행률은 WebSocket(regenTtsTaskId useWs)으로 스트리밍된다. (81씬 개별 HTTP 호출 회귀 방지)
  async function handleRegenerateAllTts() {
    // 세그먼트 기준 대상 필터 — text 가 있는 세그먼트를 하나라도 가진 씬(레거시 narration_text 도 포함).
    const targets = scenes.filter(
      (s) =>
        (s.narration_segments || []).some((seg) => seg.text?.trim()) ||
        s.narration_text?.trim(),
    );
    if (targets.length === 0) return;
    {
      const ok = await ui.confirm({
        title: "전체 TTS 재생성",
        message:
          `전체 ${targets.length}개 씬의 모든 나레이션 세그먼트를 재생성합니다.\n` +
          `개별 목소리가 지정된 세그먼트는 그대로 유지되고, 지정되지 않은 세그먼트는 ` +
          `현재 음성(${project?.tts_voice ?? "기본"})으로 재생성됩니다.\n기존 음성은 덮어쓰여집니다.`,
        variant: "warning",
        confirmLabel: "재생성",
        dontAskAgainKey: "regenAllTts",
      });
      if (!ok) return;
    }
    setProcessing(true);
    setProgress({ current: 0, total: targets.length, message: "TTS 일괄 재생성 준비 중...", phase: "tts" });
    // 대상 씬 seg_0(레거시 tts 메타)만 generating 표시 — 세부 세그먼트 상태는 완료 후 서버에서 재조회.
    targets.forEach((s) => updateSceneTts(s.scene_id, { audio_url: "", duration_sec: 0, status: "generating", synced: false }));
    // 미리보기 캐시 무효화 — TTS 길이가 바뀌면 영상 길이도 다시 맞춰야 함.
    await Promise.all(
      targets.map((s) =>
        _invalidateScenePreview(projectId, s.scene_id),
      ),
    );
    try {
      // voice_name 미전달(undefined) → 서버가 프로젝트 tts_voice 를 기본으로 사용하고,
      //   세그먼트별 voice_name 이 지정된 것은 존중한다(백엔드 정책).
      const taskId = await generateTts(
        projectId,
        targets.map((s) => s.scene_id),
        true,
      );
      setRegenTtsTaskId(taskId);
      // 이후 진행률/완료 처리는 regenTtsTaskId useWs 구독이 담당한다.
    } catch (e: unknown) {
      setProcessing(false);
      ui.error(`TTS 재생성 실패: ${(e as Error).message}`);
    }
  }

  // 아직 TTS 가 없는(생성 안 됐거나 실패한) 씬만 서버에서 일괄 생성한다.
  //   과거 브라우저 합성 경로(synthesizeTtsAndSave → tts-upload)를 서버 경로로 전환:
  //   서버가 서버 state 의 텍스트로 합성하므로 stale narration_text 오염이 없다.
  //   완료/진행률/최신 메타 재조회는 regenTtsTaskId useWs 구독이 처리한다.
  async function handleGenerateMissing(overrideVoice?: string) {
    // 텍스트(세그먼트 또는 레거시)가 있으면서 TTS 가 아직 ready 가 아닌 씬.
    const missing = scenes.filter(
      (s) =>
        ((s.narration_segments || []).some((seg) => seg.text?.trim()) ||
          s.narration_text?.trim()) &&
        (!s.tts || s.tts.status !== "ready"),
    );
    if (missing.length === 0) return;
    const voice = overrideVoice ?? undefined;
    setProcessing(true);
    setProgress({ current: 0, total: missing.length, message: "TTS 생성 준비 중...", phase: "tts" });
    missing.forEach((s) => updateSceneTts(s.scene_id, { audio_url: "", duration_sec: 0, status: "generating", synced: false }));
    await Promise.all(
      missing.map((s) =>
        _invalidateScenePreview(projectId, s.scene_id),
      ),
    );
    try {
      // force=false → 이미 서버에 있는 TTS 는 보존, 없는 것만 생성.
      // voice 는 overrideVoice 가 있으면 전달, 없으면 undefined(서버 프로젝트 tts_voice 기본).
      const taskId = await generateTts(
        projectId,
        missing.map((s) => s.scene_id),
        false,
        voice,
      );
      setRegenTtsTaskId(taskId);
      // 이후 처리는 regenTtsTaskId useWs 구독이 담당한다.
    } catch (e: unknown) {
      setProcessing(false);
      ui.error(`TTS 생성 실패: ${(e as Error).message}`);
    }
  }

  // BGM 편집 진입 — 현재 세션에 렌더 결과가 없어도 디스크에서 final_output.mp4를 찾고,
  // 그것도 없으면 사용자가 mp4를 직접 선택해서 업로드한 뒤 진입한다.
  async function handleOpenBgmSmart() {
    if (!onOpenBgm) return;
    try {
      // 1) 디스크 메타 확인.
      const res = await fetch(`/api/projects/${projectId}/render/output-info`);
      const info = res.ok ? await res.json() : null;
      if (info?.has_final_mp4) {
        onOpenBgm();
        return;
      }
      // 2) 디스크에도 없음 — 사용자에게 mp4 직접 선택을 요청.
      const ok = await ui.confirm({
        title: "렌더링된 영상 없음",
        message:
          "이 프로젝트에는 렌더링된 영상이 없습니다.\n" +
          "BGM을 입힐 mp4 파일을 직접 선택해서 업로드하시겠습니까?\n" +
          "(취소 시 BGM 편집은 진행되지 않습니다.)",
        variant: "info",
        confirmLabel: "파일 선택",
      });
      if (!ok) return;
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "video/mp4,video/*";
      input.onchange = async () => {
        const file = input.files?.[0];
        if (!file) return;
        const form = new FormData();
        form.append("file", file, file.name);
        try {
          const up = await fetch(`/api/projects/${projectId}/render/output-upload`, {
            method: "POST",
            body: form,
          });
          if (!up.ok) {
            const t = await up.text().catch(() => "");
            ui.error(`업로드 실패: HTTP ${up.status} ${t.slice(0, 200)}`);
            return;
          }
          onOpenBgm();
        } catch (e) {
          ui.error(`업로드 실패: ${(e as Error).message}`);
        }
      };
      input.click();
    } catch (e) {
      ui.error(`BGM 진입 실패: ${(e as Error).message}`);
    }
  }

  // 비가시 워터마크 — 렌더 버튼 옆 토글. 기본 꺼짐.
  const [renderWatermark, setRenderWatermark] = useState(false);

  async function handleRender(opts?: { skipSubtitle?: boolean }) {
    if (fullRenderStatus === "uploading" || fullRenderStatus === "rendering") return;
    setFullRenderStatus("uploading");
    setFullRenderMessage("서버에 footage 업로드 준비 중...");
    setFullRenderPct(-1);
    setFullRenderOutputUrl(null);
    // 영상만 렌더링(skipSubtitle)이면 SBV 를 만들지 않으므로 결과 카드에서 자막 링크를 숨긴다.
    setFullRenderHasSubtitle(!opts?.skipSubtitle);

    try {
      // 1. 프로젝트 서버 등록(upsert) — footage_exists 확인용. 씬 메타는 각 편집 액션이
      //    이미 patch 로 서버에 동기화하므로, 여기서는 서버가 알려주는 footage 존재 여부만 사용한다.
      const serverProject = await registerProject(projectId, project?.name ?? "", scenes).catch(() => null);
      const footageExists: Record<number, boolean> = (serverProject as any)?.footage_exists ?? {};

      // 2. 서버에 없는 footage 파일만 업로드 (TTS 는 서버가 진실 — blob 업로드 안 함).
      const uploadNeeded = scenes.filter(s => {
        if (!s.footage) return false;
        const fname = (s.footage as any).file ?? (s.footage as any).image;
        if (!fname) return false;
        return !footageExists[s.scene_id];
      });

      if (uploadNeeded.length === 0) {
        setFullRenderMessage("모든 footage 가 서버에 있음 — 업로드 스킵");
      }

      for (const scene of scenes) {
        if (!scene.footage) continue;
        const fname = (scene.footage as any).file ?? (scene.footage as any).image;
        if (!fname) continue;

        if (!footageExists[scene.scene_id]) {
          const footageBlob = await fetchFootageBlob(projectId, fname).catch(() => null as Blob | null);
          if (footageBlob) {
            setFullRenderMessage(`footage 업로드 중... 씬 ${scene.scene_id}`);
            await uploadSceneFootage(projectId, scene.scene_id, footageBlob, fname).catch(() => {});
          }
        }

        // TTS 는 서버가 진실의 원천 — IndexedDB 옛 blob 을 tts-upload 로 부활시키지 않는다.
        //   서버 렌더(build_merged_wav)는 서버 TTS WAV 를 직접 읽는다. 서버에 WAV 가 없는 씬은
        //   서버에서 재생성(재생성 버튼/전체 재생성)으로 채워야 하며, 브라우저 blob 부활은
        //   텍스트가 바뀐 씬을 옛 음성으로 덮어쓰는 오염 회귀 원인이라 완전히 제거했다.
      }

      // 3. 렌더링 시작
      setFullRenderStatus("rendering");
      setFullRenderMessage("렌더링 시작 중...");
      const taskId = await startRender(projectId, { skipSubtitle: !!opts?.skipSubtitle, watermark: renderWatermark });
      setFullRenderTaskId(taskId);
    } catch (e: unknown) {
      setFullRenderStatus("error");
      setFullRenderMessage((e as Error).message ?? "렌더링 준비 실패");
    }
  }

  async function handleDeleteScene(sceneId: number) {
    {
      const ok = await ui.confirm({
        title: "씬 삭제",
        message: `씬 ${sceneId}을(를) 삭제합니다. 이 작업은 되돌릴 수 없습니다.`,
        variant: "danger",
        confirmLabel: "삭제",
        dontAskAgainKey: "deleteScene",
      });
      if (!ok) return;
    }
    commitProjectHistory(); // #358 — 사용자가 confirm한 시점에 history snapshot.
    // 백엔드 preview-cache 삭제 (실패 무시)
    await _invalidateScenePreview(projectId, sceneId);
    // IndexedDB에서 씬 삭제 (TTS blob 포함)
    const updated = await dbDeleteScene(projectId, sceneId);
    setProject(updated);
    // 삭제된 씬이 현재 선택된 씬이면 선택 해제
    if (selectedSceneId === sceneId) {
      selectScene(null);
    }
    // 서버 state.json과 동기화 — 보내지 않은 씬은 register 시 자동 제거됨
    await syncScenesToServer(updated);
  }

  // ── 씬 추가 / 사이 삽입 / 복제 (#245) ──────────────────────────────────────
  //
  // 공통 흐름:
  //  1) IndexedDB에 씬 추가/삽입/복제 (project.ts 헬퍼 호출)
  //  2) store에 setProject로 즉시 반영 (UI 갱신)
  //  3) 신규 씬 선택 (selectScene)
  //  4) 서버에 registerProject로 전체 씬 동기화 (백엔드 패치 라운드트립 불필요한 일괄 갱신)
  async function syncScenesToServer(updatedProject: typeof project) {
    if (!updatedProject) return;
    await registerProject(
      projectId,
      updatedProject.name ?? "",
      updatedProject.scenes,
      updatedProject.output_width,
      updatedProject.output_height
    ).catch(() => {});
  }

  async function handleAddScene() {
    commitProjectHistory(); // #358
    const { project: updated, newSceneId } = await dbAddScene(projectId);
    setProject(updated);
    selectScene(newSceneId);
    await syncScenesToServer(updated);
  }

  async function handleInsertSceneAfter(sceneIndex: number) {
    commitProjectHistory(); // #358
    const { project: updated, newSceneId } = await dbInsertSceneAt(projectId, sceneIndex);
    setProject(updated);
    selectScene(newSceneId);
    await syncScenesToServer(updated);
  }

  // ── 클립 라이브러리에서 씬 삽입 ────────────────────────────────────────────
  //
  // 흐름:
  //   1) insertSceneAt 으로 빈 씬 삽입.
  //   2) 서버에 씬 배열을 먼저 동기화 — from-clip-library 는 서버 state 의 씬을
  //      찾아 patch 하므로, 동기화 전에 호출하면 404 가 난다.
  //   3) from-clip-library 호출 → 서버가 라이브러리 파일을 프로젝트 footage 로
  //      하드링크(복사 폴백)하고 확정 filename 을 돌려준다.
  //   4) 응답의 filename/duration 을 로컬 state·IndexedDB 에 반영.
  //
  // scene_id 주의: insertSceneAt 은 전체 씬 renumber 를 동반하지만(project.ts:505-533),
  // 반환하는 newSceneId 는 이미 renumber 후의 최종 id 다(project.ts:532). 따라서
  // 이 값을 그대로 쓰면 되고, 삽입 전 인덱스로 재계산해서는 안 된다.
  async function handleInsertSceneFromClipLibrary(sceneIndex: number, clip: ClipLibraryEntry) {
    commitProjectHistory(); // #358
    try {
      const { project: updated, newSceneId } = await dbInsertSceneAt(projectId, sceneIndex);
      setProject(updated);
      selectScene(newSceneId);
      // 서버가 새 씬을 알아야 from-clip-library 가 대상 씬을 찾을 수 있다.
      await syncScenesToServer(updated);

      const res = await assignClipToScene(projectId, newSceneId, clip.clip_id);

      // 서버가 확정한 filename 으로 footage 구성 — 충돌 회피 접미사가 붙었을 수 있어
      // 라이브러리의 원본 filename 을 쓰면 안 된다.
      const footage = makeFootageFromFilename(res.filename);
      // match 를 함께 null 로 둔다 — reassignSceneFootage(L1484-1487) 와 동일 정책.
      // 옛 match 가 남으면 렌더가 옛 source_file 로 영상을 잘못 깔고, DurationStatus 도
      // 옛 길이를 표시한다. 여기서는 새 빈 씬이라 match 가 원래 없지만, 서버도 함께
      // 클리어하므로 로컬을 명시적으로 맞춰 상태 일관성을 유지한다.
      const patch: Partial<SceneCard> = { footage, match: null };
      if (res.duration_sec > 0) patch.duration_hint_sec = res.duration_sec;

      updateScene(newSceneId, patch);
      await _localUpdateScene(projectId, newSceneId, patch);
      _invalidateSceneCutDuration(newSceneId);
      setPreviewTrigger((n) => n + 1);
      ui.success(`"${clip.title}" 이(가) 씬 ${newSceneId} 로 삽입되었습니다`);
    } catch (e) {
      const detail =
        (e as { response?: { data?: { detail?: string } } }).response?.data?.detail ??
        (e as Error).message;
      ui.error(`클립 삽입 실패: ${detail}`);
    }
  }

  // ── "중간 이미지 삽입" 원클릭 자동화 ──────────────────────────────────────
  //
  // 사용자가 이미지 자산을 하나 고르면, 그 이미지를 전체화면으로 보여주는 "이미지 전용
  // 삽입 씬"이 자동 생성되어 지정 위치(sceneIndex 뒤)에 들어간다. (기존 2단계: 빈 씬 추가
  // → footage 지정, 을 하나의 동작으로 합친 것.) 삽입 씬은 is_insert_image_scene=true 로
  // 표식되며 kenburns footage(fit=contain)로 이미지를 전체화면 합성한다.
  async function handleInsertImageSceneAt(sceneIndex: number, asset: FootageAsset) {
    commitProjectHistory(); // #358
    try {
      const { project: updated, newSceneId } = await dbInsertImageSceneAt(
        projectId,
        sceneIndex,
        asset.filename,
      );
      setProject(updated);
      selectScene(newSceneId);
      await syncScenesToServer(updated);
      _invalidateSceneCutDuration(newSceneId);
      setPreviewTrigger((n) => n + 1);
      ui.success(`중간 이미지 씬이 씬 ${newSceneId} 로 삽입되었습니다`);
    } catch (e) {
      const detail =
        (e as { response?: { data?: { detail?: string } } }).response?.data?.detail ??
        (e as Error).message;
      ui.error(`중간 이미지 삽입 실패: ${detail}`);
    }
  }

  // [보존] 씬 즉시 복제(다음 위치에 새 씬 생성) — 현재는 UI에서 호출하지 않는다.
  // 동선이 "복사 → 새 씬 추가 → 붙여넣기"로 바뀌었지만 (#349 후속),
  // NL 편집/자동화 등에서 재사용할 수 있도록 함수와 dbDuplicateScene import는 보존.
  async function handleDuplicateScene(sourceSceneId: number) {
    commitProjectHistory(); // #358
    const { project: updated, newSceneId } = await dbDuplicateScene(projectId, sourceSceneId);
    setProject(updated);
    selectScene(newSceneId);
    await syncScenesToServer(updated);
  }
  // TS noUnusedLocals 회피 — 함수를 보존하지만 모듈 외부에서 직접 참조하지 않는다.
  void handleDuplicateScene;

  // ── 씬 전체 복사/붙여넣기 핸들러 ──────────────────────────────────────────
  //
  // 흐름:
  //  1) `⎘` 클릭 → handleCopyScene: 현재 씬 deep copy를 copiedScene state에 보관.
  //     새 씬 생성 X. (기존 즉시-복제 동작과의 차이점)
  //  2) 사용자가 원하는 위치에 빈 씬 추가 (handleAddScene / handleInsertSceneAfter).
  //  3) 빈 씬(또는 임의 씬)의 "씬 붙여넣기" 클릭 → handlePasteScene:
  //     - copiedScene의 모든 필드를 현재 씬에 덮어쓴다.
  //     - 단 scene_id는 현재 씬 것을 유지(순서 보존).
  //     - tts는 null로 초기화(나레이션 텍스트는 복사되므로 사용자가 ↻로 재생성).
  //     - text_overlays/shape_overlays/graphic_overlays/mosaic_overlays의 id는 재발급
  //       (충돌 방지). text_overlays는 태그 prefix "tag-{tag}-..." 보존 — duplicateScene/
  //        onPasteEffects와 동일 규칙. (#246, #281)
  //     - 기존 내용이 있으면 confirm 다이얼로그로 덮어쓰기 의사 확인.
  const handleCopyScene = useCallback((sourceSceneId: number) => {
    const src = (project?.scenes ?? []).find((s) => s.scene_id === sourceSceneId);
    if (!src) return;
    const cloned: SceneCard =
      typeof structuredClone === "function"
        ? structuredClone(src)
        : JSON.parse(JSON.stringify(src));
    setCopiedScene(cloned);
    ui.success("씬 복사됨 — 새 씬 추가 후 '씬 붙여넣기'를 누르세요");
  }, [project]);

  const handlePasteScene = useCallback(async (targetSceneId: number) => {
    if (!copiedScene) return;
    const target = (project?.scenes ?? []).find((s) => s.scene_id === targetSceneId);
    if (!target) return;
    // 같은 씬(자기자신)에 붙여넣는 것은 의미가 없다 — 가드.
    if (target.scene_id === copiedScene.scene_id) {
      ui.info("같은 씬에는 붙여넣을 수 없습니다");
      return;
    }
    // 기존 내용(footage/오버레이/나레이션/효과 등)이 있으면 덮어쓰기 확인.
    const hasContent =
      (target.text_overlays?.length ?? 0) > 0 ||
      (target.shape_overlays?.length ?? 0) > 0 ||
      (target.graphic_overlays?.length ?? 0) > 0 ||
      (target.mosaic_overlays?.length ?? 0) > 0 ||
      (target.image_overlays?.length ?? 0) > 0 ||
      (target.filters?.length ?? 0) > 0 ||
      (target.motions?.length ?? 0) > 0 ||
      target.footage?.type !== undefined ||
      (target.narration_text?.trim() ?? "").length > 0;
    if (hasContent) {
      const ok = await ui.confirm({
        title: "씬 붙여넣기",
        message: `씬 ${targetSceneId}의 모든 효과/footage/나레이션을 덮어씁니다. 계속하시겠어요?`,
        variant: "warning",
        confirmLabel: "덮어쓰기",
        dontAskAgainKey: "pasteScene",
      });
      if (!ok) return;
    }

    // id 재발급 헬퍼 — onPasteEffects(#281)와 동일 규칙.
    const newId = () => Math.random().toString(36).slice(2, 10);
    const targetTags = copiedScene.tags ?? [];
    const reissueTextId = (ov: TextOverlay): TextOverlay => {
      const matchedTag = targetTags.find((t) => ov.id?.startsWith(`tag-${t}-`));
      return {
        ...ov,
        id: matchedTag ? `tag-${matchedTag}-${newId()}` : newId(),
      };
    };
    const clone = <T,>(v: T): T =>
      typeof structuredClone === "function" ? structuredClone(v) : JSON.parse(JSON.stringify(v));

    const patch: Partial<SceneCard> = {
      // footage 자체는 deep copy. 같은 source_file을 가리키는 것은 정상(원본도 가리킴).
      footage: copiedScene.footage ? clone(copiedScene.footage) : null,
      narration_text: copiedScene.narration_text,
      subtitle_text: copiedScene.subtitle_text,
      tags: clone(copiedScene.tags ?? []),
      text_overlays: (copiedScene.text_overlays ?? []).map((ov) => reissueTextId(clone(ov))),
      shape_overlays: (copiedScene.shape_overlays ?? []).map((s) => ({
        ...clone(s),
        id: `shape_${Date.now()}_${newId()}`,
      })),
      graphic_overlays: (copiedScene.graphic_overlays ?? []).map((g) => ({
        ...clone(g),
        id: newId(),
        // source_id는 유지 (태그 추적 일관성)
      })),
      mosaic_overlays: (copiedScene.mosaic_overlays ?? []).map((m) => ({
        ...clone(m),
        id: newId(),
      })),
      // 이미지 오버레이 — id 재발급 + image_asset_id 그대로 유지 (같은 자산 참조 OK).
      image_overlays: (copiedScene.image_overlays ?? []).map((m) => ({
        ...clone(m),
        id: `image_${Date.now()}_${newId()}`,
      })),
      // filters/motions는 id 필드 자체가 없음 — deep copy만.
      filters: (copiedScene.filters ?? []).map((f) => clone(f)),
      motions: (copiedScene.motions ?? []).map((m) => clone(m)),
      // 집중선(속도선) — id 재발급 + deep copy.
      speed_lines: (copiedScene.speed_lines ?? []).map((s) => ({
        ...clone(s),
        id: `speedlines_${Date.now()}_${newId()}`,
      })),
      footage_speed: copiedScene.footage_speed,
      footage_speed_mode: copiedScene.footage_speed_mode,
      footage_speed_ramp_hold: copiedScene.footage_speed_ramp_hold,
      footage_speed_ramp_accel: copiedScene.footage_speed_ramp_accel,
      footage_speed_ramp_base: copiedScene.footage_speed_ramp_base,
      footage_audio_volume: copiedScene.footage_audio_volume,
      // match는 원본의 trim_start/trim_end를 그대로 가져온다 — 같은 source_file이면 유효.
      match: copiedScene.match ? clone(copiedScene.match) : null,
      // tts는 항상 초기화 — 나레이션 텍스트가 그대로 복사되었으므로 사용자가 ↻로 재생성한다.
      tts: null,
    };
    await applyScenePatch(targetSceneId, patch);
    ui.success("씬 붙여넣기 완료");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [copiedScene, project]);

  // 씬 나누기 다이얼로그 — 어떤 씬을 분할 중인지만 보관.
  function handleOpenSplitScene(sceneId: number) {
    setSplitTargetSceneId(sceneId);
  }
  async function handleApplySplit(cuts: number[], totalDur: number) {
    const sid = splitTargetSceneId;
    if (sid === null) return;
    commitProjectHistory(); // #358
    try {
      // SplitDialog는 cuts/totalDur 모두 footage 절대 시간으로 반환한다.
      // splitSceneAt은 씬 내 상대 좌표(0~씬길이)를 기대하므로 baseStart를 빼서 변환한다.
      const targetScene = project?.scenes.find((s) => s.scene_id === sid);
      const baseStart = (targetScene?.footage as { clip_start_sec?: number } | undefined)?.clip_start_sec ?? 0;
      const relCuts = cuts.map((c) => c - baseStart).filter((c) => c > 0);
      const { project: updated } = await dbSplitSceneAt(projectId, sid, relCuts, totalDur || undefined);
      setProject(updated);
      await syncScenesToServer(updated);
    } catch (e) {
      ui.error(`씬 나누기 실패: ${(e as Error).message}`);
    } finally {
      setSplitTargetSceneId(null);
    }
  }

  // 씬 영상 재생속도를 TTS 길이에 자동으로 맞추기(일괄).
  //   사용자가 지정한 시작 씬 번호부터 끝까지, 각 씬의 영상 재생 길이가 (TTS + 1.5초)가 되도록
  //   footage_speed 를 계산해 세팅한다. 값↑=빠름. TTS 음성은 별도 tts_speed 라 영향 없음.
  //   TTS 없는 씬 / 영상 원본 길이 조회 불가(image/kenburns/미매칭) 씬은 건너뛴다.
  //   성능: 씬마다 patchScene/dbUpdateScene 를 개별 호출하면 대량(70+) 에서 멈춘다.
  //   메모리에서 전 씬 speed 를 계산한 뒤 setScenes(IndexedDB+서버 1회)/setProject(store 1회) 로 저장.
  async function handleAutoFitFootageSpeed() {
    if (!project || project.scenes.length === 0) {
      ui.error("씬이 1개 이상 있어야 합니다.");
      return;
    }
    const total = project.scenes.length;
    const defaultStart =
      selectedSceneId !== null && project.scenes.some((s) => s.scene_id === selectedSceneId)
        ? selectedSceneId
        : project.scenes[0].scene_id;
    const answer = await ui.prompt({
      title: "영상 속도 자동 조절",
      message: `지정한 씬 번호부터 끝까지, 영상이 나레이션보다 짧은 씬만 느리게 늘려 (TTS 길이 + 1.5초)를 채웁니다. 이미 충분히 긴 영상은 그대로 둡니다. 시작 씬 번호를 입력하세요.`,
      defaultValue: String(defaultStart),
      confirmLabel: "적용",
    });
    if (answer === null) return;
    const startId = Number(answer.trim());
    if (!Number.isFinite(startId)) {
      ui.error("올바른 씬 번호를 입력하세요.");
      return;
    }
    const startIdx = project.scenes.findIndex((s) => s.scene_id === startId);
    if (startIdx < 0) {
      ui.error(`씬 번호 ${startId} 를 찾을 수 없습니다.`);
      return;
    }

    let changed = 0;
    let skipped = 0;
    const newScenes = project.scenes.map((scene, i) => {
      if (i < startIdx) return scene;
      const speed = computeAutoFootageSpeed(scene, serverFootage);
      if (speed === null) {
        skipped += 1;
        return scene;
      }
      changed += 1;
      // footage_speed 변경 시 오버레이 출력 타임라인도 함께 재조정(씬 밖 밀림 방지).
      return {
        ...rescaleSceneOverlayTimingsForSpeed(
          scene, { footage_speed: speed }, getSceneRawFootageDuration(scene, serverFootage),
        ),
        footage_speed: speed,
      };
    });

    if (changed === 0) {
      ui.info(`늘릴 씬이 없습니다 (건너뜀 ${skipped}개). 영상이 나레이션보다 짧은 씬만 대상입니다 — 이미 충분히 긴 영상은 그대로 둡니다.`);
      return;
    }

    // 히스토리 1단계 push 후 store/IndexedDB/서버 각각 1회 저장.
    commitProjectHistory();
    const nextProject = { ...project, scenes: newScenes };
    setProject(nextProject);
    try {
      await dbSetScenes(projectId, newScenes);
    } catch (e) {
      console.error("[autoFitSpeed] 저장 실패:", e);
      ui.error(`속도 저장에 실패했습니다: ${(e as Error).message}`);
      return;
    }
    // 현재 선택된 씬이 대상 범위면 미리보기를 새 speed 로 갱신.
    if (selectedSceneId !== null && project.scenes.findIndex((s) => s.scene_id === selectedSceneId) >= startIdx) {
      setPreviewTrigger((n) => n + 1);
    }
    ui.success(`${changed}개 씬 속도 조절 완료 (건너뜀 ${skipped}개, 전체 ${total}개)`);
  }

  // 씬 하나만 영상 재생속도를 TTS 길이에 자동으로 맞추기.
  //   handleAutoFitFootageSpeed(일괄)와 같은 computeAutoFootageSpeed 규칙을 쓰되,
  //   범위 입력 없이 해당 씬 1개만 즉시 적용한다. 씬 카드 메뉴의 ⏱ 버튼에서 호출.
  async function handleAutoFitSceneSpeed(sceneId: number) {
    if (!project) return;
    const scene = project.scenes.find((s) => s.scene_id === sceneId);
    if (!scene) return;
    const speed = computeAutoFootageSpeed(scene, serverFootage);
    if (speed === null) {
      // null 사유가 둘이라 구분해서 안내한다 — "이미 충분히 김"은 정상이므로 error 가 아니다.
      const ttsDur = getSceneTtsDuration(scene);
      const rawDur = getSceneRawFootageDuration(scene, serverFootage);
      // 판정과 같은 기준(현재 배속 적용 후 재생 길이)으로 안내한다.
      const playDur = rawDur !== null ? speedOutputDuration(rawDur, getSceneSpeedSpec(scene)) : null;
      if (ttsDur > 0 && playDur !== null && playDur >= ttsDur + AUTO_SPEED_MARGIN_SEC) {
        ui.info(`영상이 이미 나레이션보다 깁니다 (영상 ${playDur.toFixed(1)}초 / 나레이션 ${ttsDur.toFixed(1)}초). 그대로 둡니다.`);
      } else {
        ui.error("이 씬은 자동 맞춤 대상이 아닙니다. TTS와 영상(clip)이 모두 있어야 합니다.");
      }
      return;
    }
    const oldSpeed = scene.footage_speed ?? 1.0;
    if (oldSpeed === speed) {
      ui.info(`이미 맞춰져 있습니다 (${speed.toFixed(2)}배).`);
      return;
    }
    // footage_speed 변경 시 오버레이 출력 타임라인도 함께 재조정(씬 밖 밀림 방지).
    const rescaled = rescaleSceneOverlayTimingsForSpeed(
      scene, { footage_speed: speed }, getSceneRawFootageDuration(scene, serverFootage),
    );
    const patch: Partial<SceneCard> = {
      footage_speed: speed,
      text_overlays: rescaled.text_overlays,
      shape_overlays: rescaled.shape_overlays,
      graphic_overlays: rescaled.graphic_overlays,
      image_overlays: rescaled.image_overlays,
      mosaic_overlays: rescaled.mosaic_overlays,
    };
    commitProjectHistory();
    updateScene(sceneId, patch);
    try {
      await _localUpdateScene(projectId, sceneId, patch);
      await patchScene(projectId, sceneId, patch).catch(() => {});
    } catch (e) {
      console.error("[autoFitSceneSpeed] 저장 실패:", e);
      ui.error(`속도 저장에 실패했습니다: ${(e as Error).message}`);
      return;
    }
    if (selectedSceneId === sceneId) setPreviewTrigger((n) => n + 1);
    ui.success(`씬 ${sceneId} 속도 ${oldSpeed.toFixed(2)}→${speed.toFixed(2)}배`);
  }

  // #406 — 현재 프로젝트를 템플릿으로 저장.
  // 사용자가 입력한 제목/설명을 받아 백엔드 createTemplate 호출. 백엔드는 footage/match/tts
  // 를 제외한 씬 구조 + 오버레이 + BGM 만 저장한다.
  // 저장 직전 서버에 최신 state 를 한번 푸시해 IndexedDB ↔ 서버 차이가 있어도 최신본이
  // 템플릿화되도록 한다.
  async function handleSaveAsTemplate() {
    if (!project || project.scenes.length === 0) {
      ui.error("씬이 1개 이상 있어야 템플릿으로 저장할 수 있습니다.");
      return;
    }
    const title = await ui.prompt({
      title: "템플릿으로 저장",
      message: "이 영상 구조(씬 배치 + 자막 + 오버레이 + BGM)를 템플릿으로 저장합니다. 원본 영상은 포함되지 않습니다.",
      defaultValue: project.name || "새 템플릿",
      confirmLabel: "저장",
    });
    if (!title || !title.trim()) return;
    const description = await ui.prompt({
      title: "템플릿 설명 (선택)",
      message: "이 템플릿이 어떤 영상 구조인지 메모해두면 나중에 찾기 쉽습니다.",
      defaultValue: "",
      confirmLabel: "저장",
    });
    try {
      // #408 — pushProjectState(register) 호출 금지. production state.json 을
      // stale 데이터로 덮어쓸 위험(과거 27씬 → 1씬 사고와 동일 경로).
      // 템플릿은 백엔드가 서버 state.json 을 진실로 읽어 생성하므로 push 불필요.
      const meta = await createTemplate({
        source_project_id: projectId,
        title: title.trim(),
        description: (description ?? "").trim(),
      });
      ui.success(`템플릿 "${meta.title}" 저장 완료 (씬 ${meta.scene_count}개)`);
    } catch (e) {
      ui.error(`템플릿 저장 실패: ${(e as Error).message}`);
    }
  }

  async function handleDeleteAllScenes() {
    const count = project?.scenes.length ?? 0;
    if (count === 0) return;
    // #296 — 위험 동작: "삭제"를 직접 타이핑해야 확정.
    {
      const ok = await ui.confirm({
        title: "모든 씬 삭제",
        message: `모든 씬 ${count}개를 삭제합니다. 이 작업은 되돌릴 수 없습니다.`,
        variant: "danger",
        confirmLabel: "전체 삭제",
        requireType: "삭제",
      });
      if (!ok) return;
    }
    commitProjectHistory(); // #358 — 전체 삭제는 위험 동작이지만 Undo 로 복원 가능해야 한다.
    // 씬별 preview-cache 삭제 (실패 무시)
    await Promise.all(
      (project?.scenes ?? []).map((s) =>
        _invalidateScenePreview(projectId, s.scene_id)
      )
    );
    const updated = await dbDeleteAllScenes(projectId);
    setProject(updated);
    selectScene(null);
    setSelectedSceneIds(new Set());
    await syncScenesToServer(updated);
  }

  async function handleBulkDeleteScenes() {
    const count = selectedSceneIds.size;
    if (count === 0) return;
    {
      const ok = await ui.confirm({
        title: "선택 씬 삭제",
        message: `선택한 ${count}개 씬을 삭제합니다. 이 작업은 되돌릴 수 없습니다.`,
        variant: "danger",
        confirmLabel: "삭제",
      });
      if (!ok) return;
    }
    commitProjectHistory(); // #358
    let latestProject = project!;
    for (const sceneId of selectedSceneIds) {
      await _invalidateScenePreview(projectId, sceneId);
      latestProject = await dbDeleteScene(projectId, sceneId);
    }
    setProject(latestProject);
    // 단일 선택된 씬이 삭제 대상이었으면 선택 해제
    if (selectedSceneId !== null && selectedSceneIds.has(selectedSceneId)) {
      selectScene(null);
    }
    setSelectedSceneIds(new Set());
    await syncScenesToServer(latestProject);
  }

  // ── #257 — 사이드 패널(EffectAddPanel)용 lifted 핸들러들 ───────────────────
  // SceneItem 내부의 effect 추가 버튼들을 우측 사이드 패널로 이동하면서,
  // 핸들러는 Editor scope에서 선택된 씬에 대해 동작하도록 lift한다.
  // 기존 onUpdate* 파이프라인(로컬 store → IndexedDB → patchScene → 프리뷰 캐시 무효화)을
  // 그대로 재사용하여 회귀 위험을 최소화한다.

  // #279 — 슬라이더 변경 시 자동 미리보기 트리거 제거.
  // opts.invalidateCache: 서버 preview-cache DELETE + patchScene 동기화 여부 (기본 true).
  // opts.triggerRefresh: previewTrigger bump (PreviewPanel 자동 재생성 트리거) 여부 (기본 true).
  // 슬라이더 onChange처럼 연속 변경이 발생하는 경로는 triggerRefresh: false로 호출하여,
  // 사용자가 명시적으로 "미리보기 새로고침" 버튼을 눌렀을 때만 재생성되도록 한다.
  async function applyScenePatch(
    sceneId: number,
    patch: Partial<SceneCard>,
    opts: { invalidateCache?: boolean; triggerRefresh?: boolean } | boolean = {}
  ) {
    // 하위 호환: 두 번째 인자가 boolean이면 기존 invalidatePreview 의미로 해석.
    const normalized = typeof opts === "boolean"
      ? { invalidateCache: opts, triggerRefresh: opts }
      : opts;
    const { invalidateCache = true, triggerRefresh = true } = normalized;
    // #358 — Undo/Redo: triggerRefresh=true 는 "한 액션의 commit 시점" 으로 간주하고
    // 변경 직전 snapshot 을 history 에 push 한다.
    // sliderOnly(=triggerRefresh:false) 호출은 슬라이더 중간값이므로 history 에 쌓지 않는다.
    if (triggerRefresh) {
      commitProjectHistory();
    } else {
      // #363 — sliderOnly 경로: 드래그 시작 직전 snapshot 보관 (헬퍼로 위임).
      stashSliderSnapshotIfFirst();
    }
    updateScene(sceneId, patch);
    // #385 — 길이 영향 필드 변경 시 Phase 5 cut 캐시 즉시 invalidate.
    //   getSceneDurationSec 의 1순위 경로(cutDurations)가 옛 값을 잡고 있던 회귀를 해소하여
    //   TimelineBar 의 씬 블록 비율이 사용자 수정 즉시 반영되도록 한다.
    if (_patchAffectsDuration(patch)) {
      _invalidateSceneCutDuration(sceneId);
    }
    await _localUpdateScene(projectId, sceneId, patch);
    if (invalidateCache) {
      await _invalidateScenePreview(projectId, sceneId);
      await patchScene(projectId, sceneId, patch).catch(() => {});
    }
    if (triggerRefresh) {
      setPreviewTrigger((n) => n + 1);
    }
  }

  function handleSideAddShape(shapeType: ShapeType) {
    if (!selectedScene) return;
    const newId = `shape_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newShape: ShapeOverlay = {
      id: newId,
      shape: shapeType,
      x: 0.1,
      y: 0.1,
      width: 0.2,
      height: 0.2,
      color: "#ff0000",
      opacity: 0.7,
      start_sec: 0,
      end_sec: null,
      // #287 — 등장/사라짐 효과 기본값 (fade + 0.3초)
      enter_effect: "fade",
      exit_effect: "fade",
      enter_duration_sec: 0.3,
      exit_duration_sec: 0.3,
    };
    const merged = [...(selectedScene.shape_overlays ?? []), newShape];
    applyScenePatch(selectedScene.scene_id, { shape_overlays: merged }, false);
  }

  function handleSideAddGraphic(type: GraphicOverlayType) {
    if (!selectedScene) return;
    // SceneItem.handleAddGraphic과 동일한 기본 파라미터 로직.
    let defaultParams: Record<string, unknown> = {};
    let defaultX = 0.5;
    let defaultY = 0.5;
    if (type === "spotlight") {
      defaultParams = { radius: 0.2, darkness: 0.6, feather: 40 };
    } else if (type === "alert") {
      defaultParams = { text: "알림", style: "warning", width: 0.4 };
      defaultY = 0.15;
    } else if (type === "target") {
      defaultParams = { marker_type: "crosshair", size: 0.12, color: "#ff3030", rotation: 0 };
    } else if (type === "reaction") {
      defaultParams = { icon_type: "thunder", size: 0.15, bounce: false };
      defaultY = 0.3;
    } else if (type === "smell") {
      defaultParams = { width: 0.2, height: 0.3, color: "green", intensity: 0.7, loop: false };
      defaultY = 0.65;
    } else if (type === "emoji") {
      // #288 — 이모지 아이콘 4종. size는 px 단위 (drawtext fontsize와 1:1).
      defaultParams = { icon_type: "question", size: 150 };
    } else if (type === "dash") {
      // 질주 표시 "=3=3=3" — 마크가 진행 방향으로 흘러간다(항상 애니메이션).
      defaultParams = {
        mark: "=3", count: 3, size: 0.06, color: "#222222",
        direction: "right", speed: 3, spacing: 1.15, trail: true,
      };
      defaultY = 0.5;
    }
    const newId = `graphic_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newGraphic: GraphicOverlay = {
      id: newId,
      type,
      x: defaultX,
      y: defaultY,
      start_sec: 0,
      end_sec: null,
      params: defaultParams,
      // #287 — 등장/사라짐 효과 기본값 (fade + 0.3초)
      enter_effect: "fade",
      exit_effect: "fade",
      enter_duration_sec: 0.3,
      exit_duration_sec: 0.3,
    };
    const merged = [...(selectedScene.graphic_overlays ?? []), newGraphic];
    applyScenePatch(selectedScene.scene_id, { graphic_overlays: merged });
  }

  function handleSideAddFilter(filterType: SceneFilterType) {
    if (!selectedScene) return;
    const existing = selectedScene.filters ?? [];
    if (existing.some((f) => f.filter_type === filterType)) return; // 중복 무시
    const defaultParams: Record<string, unknown> =
      filterType === "pov"
        ? { center_x: 0.5, center_y: 0.5, radius: 0.35, feather: 80 }
        : {};
    const next: SceneFilter = {
      filter_type: filterType,
      intensity: 1.0,
      params: defaultParams,
    };
    applyScenePatch(selectedScene.scene_id, { filters: [...existing, next] });
  }

  function handleSideAddMotion(motionType: MotionType) {
    if (!selectedScene) return;
    let defaultParams: Record<string, unknown> = {};
    let defaultDuration = 1.0;
    if (motionType === "shake") {
      defaultParams = { intensity: 0.6 };
      defaultDuration = 0.6;
    } else if (motionType === "clip_zoom") {
      defaultParams = { target_x: 0.5, target_y: 0.5, start_scale: 1.0, end_scale: 1.3 };
      defaultDuration = 1.5;
    } else if (motionType === "slowmo") {
      defaultParams = { speed: 0.5, interpolate: false, freeze_at_end_sec: 0.0 };
      defaultDuration = 10.0;
    } else if (motionType === "stutter") {
      // #347 — 끊김/점프 모션. intensity(0.0~1.0)로 끊김 강도 조절.
      defaultParams = { intensity: 0.3 };
      defaultDuration = 1.5;
    }
    const next: SceneMotion = {
      motion_type: motionType,
      start_sec: 0,
      duration_sec: defaultDuration,
      params: defaultParams,
    };
    const merged = [...(selectedScene.motions ?? []), next];
    applyScenePatch(selectedScene.scene_id, { motions: merged });
  }

  // #264 — 모자이크 오버레이 추가 (도형 오버레이 추가 패턴과 동일).
  // 5단계 동기화: store → IndexedDB → preview-cache invalidate → patchScene → previewTrigger bump (applyScenePatch).
  function handleSideAddMosaic(shapeType: MosaicShape) {
    if (!selectedScene) return;
    const newId = `mosaic_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newMosaic: MosaicOverlay = {
      id: newId,
      shape: shapeType,
      x: 0.4,
      y: 0.4,
      width: 0.2,
      height: 0.2,
      pixel_size: 20,
      mode: "pixelate",
      blur_radius: 20,
      start_sec: 0,
      end_sec: null,
    };
    const merged = [...(selectedScene.mosaic_overlays ?? []), newMosaic];
    applyScenePatch(selectedScene.scene_id, { mosaic_overlays: merged });
  }

  // 집중선(속도선) 오버레이 추가 — 세로 영상 기준 자연스러운 기본값(중심 살짝 위, 중앙 빈 원).
  // 파라미터 편집은 EffectListPanel(효과 목록)에서 수행. 백엔드가 파라미터+해상도 해시 캐시로
  // Pillow PNG 를 생성/재사용하므로 미리보기 반복 비용이 낮다.
  function handleSideAddSpeedLines() {
    if (!selectedScene) return;
    const newId = `speedlines_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newSpeedLines: SpeedLinesOverlay = {
      id: newId,
      color: "#000000",
      density: 90,
      center_x: 0.5,
      center_y: 0.4,   // 세로 영상: 인물 얼굴 근처가 자연스러움
      inner_ratio: 0.28,
      thickness: 0.9,
      width_variance: 0.2,  // 선별 굵기 ±20% 랜덤 편차(자연스러움)
      jitter: 0.6,
      seed: Math.floor(Math.random() * 100000),
      supersample: 3,
      area_w: 1.0,   // 기본 전체 화면 방사
      area_h: 1.0,
      opacity: 1.0,
      // 선별 페이드 인/아웃 반복(깜빡임) 애니메이션 기본 on. 각 선이 무작위 타이밍으로 깜빡.
      // (start_scale 은 dead 라 신규 기본값에서 제외 — 823c543)
      animate: true,
      num_buckets: 8,
      anim_speed: 1.0,
      anim_period_sec: 0.7,
      start_sec: 0,
      end_sec: null,
      enter_effect: "fade",
      exit_effect: "fade",
      enter_duration_sec: 0.3,
      exit_duration_sec: 0.3,
    };
    const merged = [...(selectedScene.speed_lines ?? []), newSpeedLines];
    applyScenePatch(selectedScene.scene_id, { speed_lines: merged });
  }

  // ── 이미지 오버레이 추가 — 자산 선택 모달 ─────────────────────────────────
  // 기존 도형/그래픽/모자이크와 달리 이미지 오버레이는 "어느 이미지 자산을 쓸지" 사용자가
  // 골라야 하므로, 클릭 즉시 생성하지 않고 자산 선택 모달을 먼저 띄운다.
  // - imageAssetPickerOpen: 모달 표시 여부.
  // - imageAssetPickerSceneId: 어느 씬에 추가할지 (모달이 떠 있을 때 선택 씬이 바뀌면 잠긴 씬 유지).
  const [imageAssetPickerOpen, setImageAssetPickerOpen] = useState(false);
  const [imageAssetPickerSceneId, setImageAssetPickerSceneId] = useState<number | null>(null);

  function handleSideAddImage() {
    if (!selectedScene) return;
    setImageAssetPickerSceneId(selectedScene.scene_id);
    setImageAssetPickerOpen(true);
  }

  function handlePickImageAsset(asset: FootageAsset) {
    const sceneId = imageAssetPickerSceneId;
    setImageAssetPickerOpen(false);
    setImageAssetPickerSceneId(null);
    if (sceneId == null) return;
    const sc = scenes.find((s) => s.scene_id === sceneId);
    if (!sc) return;
    const newId = `image_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const newImage: ImageOverlay = {
      id: newId,
      image_asset_id: asset.asset_id,
      x: 0.05,
      y: 0.05,
      width: 0.25,
      height: 0.20,
      opacity: 1.0,
      rotation_deg: 0,
      start_sec: 0,
      end_sec: null,
      caption_text: null,
      caption_font_size: 0.025,
      caption_color: "#FFFFFF",
      caption_bg_color: "#000000",
      caption_bg_opacity: 0.6,
      caption_padding: 0.005,
      enter_effect: "fade",
      exit_effect: "fade",
      enter_duration_sec: 0.3,
      exit_duration_sec: 0.3,
    };
    const merged = [...(sc.image_overlays ?? []), newImage];
    applyScenePatch(sc.scene_id, { image_overlays: merged });
  }

  function handleSideAddTextOverlay() {
    if (!selectedScene) return;
    // 1차 단순화: 기본 스타일의 빈 텍스트 오버레이를 추가하고,
    // 사용자는 씬 카드의 칩을 클릭하여 텍스트/스타일/프리셋을 편집한다 (#257 이슈 합의).
    const newId = Math.random().toString(36).slice(2, 10);
    const newOverlay: TextOverlay = {
      id: newId,
      text: "텍스트",
      x: 0.5,
      y: 0.1,
      font_size: 40,
      font_family: "NotoSansKR",
      color: "#ffffff",
      gradient: null,
      bold: false,
      italic: false,
      outline_color: "#000000",
      outline_width: 2,
      glow_color: "#000000",
      glow_radius: 0,
      opacity: 1.0,
      align: "center",
      start_sec: 0.0,
      end_sec: null,
      // #287 — 등장/사라짐 효과 기본값 (fade + 0.3초)
      enter_effect: "fade",
      exit_effect: "fade",
      enter_duration_sec: 0.3,
      exit_duration_sec: 0.3,
    };
    const merged = [...(selectedScene.text_overlays ?? []), newOverlay];
    // 텍스트 오버레이는 프리뷰 캐시만 무효화하면 됨 (SceneItem.onUpdateOverlays 정책과 동일).
    updateScene(selectedScene.scene_id, { text_overlays: merged });
    _localUpdateScene(projectId, selectedScene.scene_id, { text_overlays: merged }).catch(() => {});
    patchScene(projectId, selectedScene.scene_id, { text_overlays: merged }).catch(() => {});
  }

  return (
    <DndContext onDragEnd={handleFootageDrop}>
      <div style={styles.layout}>
        {/* Left: footage 브라우저 */}
        {visiblePanels.footage && (
          <div style={styles.sidebar}>
            <div style={styles.sidebarHeader}>
              {/* #380 — 사이드바 제목 옆에 "+ 화면 녹화" 트리거 버튼.
                   클릭하면 ScreenRecorderDialog 가 모달로 열리고, 저장 완료 시
                   onSaved 콜백이 새 assets 을 store/IndexedDB 에 머지한다. */}
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={styles.sidebarTitle}>footage</span>
                <button
                  type="button"
                  style={styles.sidebarRecordBtn}
                  onClick={() => setRecorderOpen(true)}
                  title="화면 녹화로 footage 추가"
                >+ 화면 녹화</button>
              </div>
              <input
                style={styles.searchInput}
                placeholder="파일명·레이블 검색..."
                value={mediaFilter}
                onChange={(e) => setMediaFilter(e.target.value)}
              />
            </div>
            <div style={styles.thumbGrid}>
              {filteredAssets.map((a) => (
                <DraggableAsset
                  key={a.asset_id}
                  asset={a}
                  projectId={projectId}
                  isHighlighted={highlightedAssetId === a.asset_id}
                  onGoToStudio={onOpenStudio ? () => onOpenStudio(a.asset_id) : undefined}
                />
              ))}
              {filteredAssets.length === 0 && (
                <div style={{ gridColumn: "1/-1", color: "#444", fontSize: 12, textAlign: "center", padding: 16 }}>
                  footage 스튜디오에서 footage를 추가하세요
                </div>
              )}
            </div>
          </div>
        )}

        {/* Right */}
        <div style={styles.main}>
          {/* Panel toggles — 각 영역을 숨기거나 다시 표시 */}
          <div style={styles.panelToggleBar}>
            {/* #358 — Undo / Redo. 가능할 때만 활성. */}
            <button
              type="button"
              style={{
                ...styles.panelToggleBtn,
                opacity: canUndo() ? 1 : 0.4,
                cursor: canUndo() ? "pointer" : "not-allowed",
              }}
              onClick={() => { void handleUndo(); }}
              disabled={!canUndo()}
              title="되돌리기 (⌘/Ctrl+Z)"
            >↶ 되돌리기</button>
            <button
              type="button"
              style={{
                ...styles.panelToggleBtn,
                opacity: canRedo() ? 1 : 0.4,
                cursor: canRedo() ? "pointer" : "not-allowed",
              }}
              onClick={() => { void handleRedo(); }}
              disabled={!canRedo()}
              title="다시 실행 (⌘/Ctrl+⇧+Z 또는 ⌘/Ctrl+Y)"
            >↷ 다시 실행</button>
            {/* #359 — 오버레이 가이드 표시/숨김. 단축키 H */}
            <button
              type="button"
              style={{ ...styles.panelToggleBtn, ...(showOverlayGuides ? styles.panelToggleOn : {}) }}
              onClick={toggleOverlayGuides}
              title={showOverlayGuides
                ? "오버레이 편집 가이드 숨김 (H) — 결과만 보기"
                : "오버레이 편집 가이드 표시 (H)"}
            >{showOverlayGuides ? "👁 가이드" : "👁‍🗨 가이드"}</button>
            <button
              type="button"
              style={{ ...styles.panelToggleBtn, ...(visiblePanels.footage ? styles.panelToggleOn : {}) }}
              onClick={() => togglePanel("footage")}
              title="Footage 사이드바"
            >📁 Footage</button>
            <button
              type="button"
              style={{ ...styles.panelToggleBtn, ...(visiblePanels.topbar ? styles.panelToggleOn : {}) }}
              onClick={() => togglePanel("topbar")}
              title="상단 도구 모음"
            >⚙ 상단</button>
            <button
              type="button"
              style={{ ...styles.panelToggleBtn, ...(visiblePanels.preview ? styles.panelToggleOn : {}) }}
              onClick={() => togglePanel("preview")}
              title="미리보기"
            >▶ 미리보기</button>
            <button
              type="button"
              style={{ ...styles.panelToggleBtn, ...(visiblePanels.scenes ? styles.panelToggleOn : {}) }}
              onClick={() => togglePanel("scenes")}
              title="씬 리스트"
            >🎬 씬</button>
            <button
              type="button"
              style={{ ...styles.panelToggleBtn, ...(visiblePanels.effects ? styles.panelToggleOn : {}) }}
              onClick={() => togglePanel("effects")}
              title="효과 추가 패널"
            >✨ 효과</button>
            {/* #316 — 자연어 편집 AI 패널 토글 (Gemini). 기본 숨김 — lib/features.ts */}
            {GEMINI_ENABLED && (
            <button
              type="button"
              style={{ ...styles.panelToggleBtn, ...(nlEditorPanelOpen ? styles.panelToggleOn : {}) }}
              onClick={() => setNlEditorPanelOpen(!nlEditorPanelOpen)}
              title="자연어로 영상을 수정하는 AI 편집 도우미"
            >🤖 AI 편집</button>
            )}
          </div>
          {/* Top bar */}
          {visiblePanels.topbar && (
          <div style={styles.topBar}>
            {/* 첫째 줄 — 홈·타이틀(좌측 정렬) + 해상도·음성·전체 재생성(우측) 같은 줄 공유 */}
            <div style={styles.topRowFirst}>
              <div style={styles.topBarHeader}>
                {onBack && <button style={styles.backBtn} onClick={onBack}>← 홈</button>}
                <span style={styles.projectTitle}>{project?.name}</span>
              </div>
              <div style={{ ...styles.topActions, marginLeft: "auto" }}>
                <ResolutionSelector
                  outputWidth={project?.output_width ?? 1920}
                  outputHeight={project?.output_height ?? 1080}
                  onChange={async (w, h) => {
                    setProject({ ...project!, output_width: w, output_height: h });
                    await updateProject(projectId, { output_width: w, output_height: h });
                  }}
                />
                <BgColorPicker
                  bgColor={project?.bg_color ?? "black"}
                  // onPreview: 드래그 중 매 이벤트 — 로컬 state 만 갱신(서버 저장/렌더 없음).
                  onPreview={(c) => setProject({ ...project!, bg_color: c })}
                  // onCommit: 피커를 닫을 때 1회 — 서버 저장 + 프리뷰 재렌더.
                  //   letterbox 여백 pad 색이라 서버 프리뷰를 다시 렌더해야 반영된다.
                  onCommit={async (c) => {
                    setProject({ ...project!, bg_color: c });
                    await updateProject(projectId, { bg_color: c });
                    if (selectedSceneId !== null) setPreviewTrigger((n) => n + 1);
                  }}
                />
                {/* 스플릿 씬 디졸브 강제 적용 — 기본 false, true면 같은 source_file 인접 씬(분할로 생긴 씬)에도 일반 dissolve 적용. */}
                <label
                  style={styles.splitSceneDissolveToggle}
                  title="같은 영상에서 분할로 만든 인접 씬(스플릿 씬) 사이에도 디졸브 전환 효과를 강제로 적용합니다. 기본값은 끔(잔상 방지)."
                >
                  <input
                    type="checkbox"
                    checked={project?.force_split_scene_dissolve ?? false}
                    onChange={async (e) => {
                      const checked = e.target.checked;
                      setProject({ ...project!, force_split_scene_dissolve: checked });
                      await updateProject(projectId, { force_split_scene_dissolve: checked });
                    }}
                  />
                  스플릿 씬도 디졸브
                </label>
                {/* #355 — Gemini 모델 선택 (자연어 편집/자동 분할/자동 오버레이에 사용). 기본 숨김. */}
                {GEMINI_ENABLED && (
                <select
                  style={styles.voiceSelect}
                  value={project?.gemini_model ?? "gemini-2.5-flash"}
                  onChange={async (e) => {
                    const v = e.target.value;
                    // 낙관적 업데이트 — UI 즉시 반영 후 IndexedDB+서버 동기화.
                    // 동기화 실패 시 콘솔 경고만 남기고 UI 상태는 유지.
                    setProject({ ...project!, gemini_model: v });
                    try {
                      await updateProject(projectId, { gemini_model: v });
                    } catch (err) {
                      console.warn("[gemini_model] 저장 실패", err);
                    }
                  }}
                  title="자연어 편집/자동 분할에 사용할 Gemini 모델 선택. 모델별 단가는 비용 모달에서 확인하세요."
                >
                  {GEMINI_MODELS.map((m) => (
                    <option key={m.value} value={m.value}>{m.label}</option>
                  ))}
                </select>
                )}
                <select
                  style={styles.voiceSelect}
                  value={project?.tts_voice ?? "ko-KR-Chirp3-HD-Orus"}
                  onChange={async (e) => {
                    const v = e.target.value;
                    setProject({ ...project!, tts_voice: v });
                    await updateProject(projectId, { tts_voice: v });
                  }}
                  title="TTS 음성 선택 (Google 위주 — Typecast 는 우측 찾기 버튼)"
                >
                  <VoiceOptions voices={mergedVoices} />
                </select>
                <button
                  type="button"
                  style={styles.voicePreviewBtn}
                  onClick={() => setTypecastModalOpen(true)}
                  title="Typecast 목소리를 성별·연령·용도로 검색"
                >
                  🔍 Typecast 찾기
                </button>
                {currentTypecastName && (
                  <span
                    style={{ fontSize: 12, color: "#a5b4fc", whiteSpace: "nowrap" }}
                    title={`선택된 Typecast 목소리: ${currentTypecastName}`}
                  >
                    🎙 {currentTypecastName}
                  </span>
                )}
                {(() => {
                  const voice = project?.tts_voice ?? "ko-KR-Chirp3-HD-Orus";
                  const playing = ttsPreview.playingVoice === voice;
                  return (
                    <button
                      style={styles.voicePreviewBtn}
                      onClick={() => ttsPreview.preview(voice)}
                      disabled={ttsPreview.loading}
                      title={playing ? "미리듣기 정지" : "선택한 목소리로 샘플 재생"}
                    >
                      {ttsPreview.loading ? "…" : playing ? "■" : "▶"}
                    </button>
                  );
                })()}
                <button
                  style={styles.regenAllTtsBtn}
                  onClick={handleRegenerateAllTts}
                  disabled={isProcessing || scenes.filter((s) => s.narration_text?.trim()).length === 0}
                  title="현재 선택된 음성으로 전체 씬 TTS를 일괄 재생성합니다"
                >
                  🔄 전체 재생성
                </button>
              </div>
            </div>
            {/* 둘째 줄 — 상태 배지·일괄 작업·렌더링 */}
            <div style={styles.topActions}>
                {/* #301 — 모의 클라우드 비용 표시. fullRenderStatus 변경 시 자동 갱신. */}
                <CostBadge
                  projectId={projectId}
                  reloadKey={fullRenderStatus === "done" ? 1 : 0}
                />
                {isProcessing && (
                  <span style={styles.processingBadge}>{progress.message || "처리 중..."}</span>
                )}
                {!isProcessing && (() => {
                  const unsyncedCount = scenes.filter((s) => s.narration_text && s.tts && !s.tts.synced).length;
                  return unsyncedCount > 0 ? (
                    <span style={styles.unsyncedBadge}>TTS 미동기화 {unsyncedCount}개</span>
                  ) : null;
                })()}
                {(() => {
                  const missingCount = scenes.filter((s) => s.narration_text && (!s.tts || s.tts.status !== "ready")).length;
                  return missingCount > 0 && !isProcessing ? (
                    <button
                      style={styles.generateAllBtn}
                      onClick={() => setTtsStartVoice(project?.tts_voice ?? "ko-KR-Chirp3-HD-Orus")}
                    >
                      ♪ TTS 생성 ({missingCount}개)
                    </button>
                  ) : null;
                })()}
                {/* #384 — 타임라인 클릭 시점에 새 TTS 구간(segment) 을 추가하는 발견 가능한 진입 경로.
                    Alt/Shift+클릭이 단서가 없어 사용자가 기능 자체를 찾지 못한 회귀(#382 후속) 대응.
                    ttsAnchor 가 비어 있으면 비활성화 + 안내 툴팁으로 사용법 노출. */}
                <button
                  type="button"
                  style={{
                    ...styles.addTtsSegmentBtn,
                    ...(ttsAnchor ? {} : styles.addTtsSegmentBtnDisabled),
                  }}
                  disabled={!ttsAnchor}
                  onClick={() => {
                    if (!ttsAnchor) return;
                    setPendingSegmentAdd(ttsAnchor);
                    setEditingSegmentId(null);
                  }}
                  title={ttsAnchor
                    ? `씬 ${ttsAnchor.sceneId} · ${ttsAnchor.offsetSec.toFixed(2)}s 에 새 TTS 구간 추가`
                    : "타임라인을 먼저 클릭해 추가할 시점을 선택하세요"}
                >
                  + TTS 구간 추가
                </button>
                {selectedSceneIds.size > 0 && (
                  <button
                    style={styles.bulkDeleteBtn}
                    onClick={handleBulkDeleteScenes}
                  >
                    {selectedSceneIds.size}개 씬 삭제
                  </button>
                )}
                {/* #294 — 보조 액션(태그 사전/BGM 편집)은 회색 톤으로 통일. */}
                <button
                  style={styles.secondaryActionBtn}
                  onClick={() => setTagDictModalOpen(true)}
                  title="씬 태그 사전 편집 (빌트인 + 유저 정의)"
                >
                  🏷 태그 사전
                </button>
                {/* #289 — 후처리 BGM/효과음 에디터.
                    항상 활성. 클릭 시 디스크의 final_output.mp4를 우선 사용, 없으면 사용자가 mp4를 직접 업로드. */}
                {onOpenBgm && (
                  <button
                    style={styles.secondaryActionBtn}
                    onClick={handleOpenBgmSmart}
                    title="후처리 BGM/효과음 다중 트랙 편집 (단축키: ⌘/Ctrl+B)"
                  >
                    🎵 BGM 편집
                  </button>
                )}
                {/* #406 — 현재 프로젝트를 템플릿으로 저장.
                    원본 영상은 제외되고 씬 구조 + 자막 + 오버레이 + BGM 만 저장된다.
                    저장된 템플릿은 홈 화면 '템플릿으로 만들기' 에서 새 프로젝트 출발점으로 사용. */}
                <button
                  style={styles.secondaryActionBtn}
                  onClick={handleSaveAsTemplate}
                  title="현재 영상 구조(원본 영상 제외)를 템플릿으로 저장 — 동일한 포맷을 반복 제작할 때 출발점으로 사용"
                >
                  📐 템플릿으로 지정
                </button>
                {/* 지정한 씬부터 끝까지 영상 재생속도를 TTS 길이에 자동으로 맞춘다(일괄). */}
                <button
                  style={styles.secondaryActionBtn}
                  onClick={handleAutoFitFootageSpeed}
                  disabled={(project?.scenes.length ?? 0) === 0}
                  title="TTS 길이에 맞춰 영상 속도 자동 조절 — 지정한 씬부터 끝까지 각 씬 영상 길이를 (TTS + 1.5초)로 통일합니다"
                >
                  ⏱ 영상속도 자동맞춤
                </button>
                {/* 비가시 워터마크 토글 — 켜면 이번 렌더의 최종 합성 때 Video Seal 워터마크를 넣는다.
                    기본 꺼짐(렌더 시간이 수 분 늘어남). 새로고침하면 다시 꺼진다. */}
                <button
                  style={{
                    ...styles.secondaryActionBtn,
                    ...(renderWatermark ? { background: "#2b6cb0", color: "#fff", borderColor: "#2b6cb0" } : {}),
                  }}
                  onClick={() => setRenderWatermark((v) => !v)}
                  disabled={fullRenderStatus === "uploading" || fullRenderStatus === "rendering"}
                  aria-pressed={renderWatermark}
                  title={renderWatermark
                    ? "비가시 워터마크 켜짐 — 렌더링 시 최종 영상에 제작자 표식을 넣습니다(수 분 더 걸림). 클릭하면 끕니다."
                    : "비가시 워터마크 꺼짐 — 클릭하면 다음 렌더링에 눈에 안 보이는 제작자 표식을 넣습니다(수 분 더 걸림)."}
                >
                  🔏 워터마크 {renderWatermark ? "ON" : "OFF"}
                </button>
                {/* #294 — 메인 액션: 렌더링(강조색). */}
                <RenderSplitButton
                  status={fullRenderStatus}
                  onRender={() => handleRender()}
                  onRenderVideoOnly={() => handleRender({ skipSubtitle: true })}
                  projectId={projectId}
                />
                {/* #294 — 단축키 도움말 (? 키와 동일). */}
                <button
                  style={styles.helpBtn}
                  onClick={() => setShortcutHelpOpen(true)}
                  title="단축키 도움말 (?)"
                >
                  ?
                </button>
                {/* #294 — 위험 동작(전체 삭제)은 우측 끝에 고립 배치 + 구분선으로 오클릭 방지. */}
                {(project?.scenes.length ?? 0) > 0 && (
                  <>
                    <span style={{ ...styles.toolbarDivider, marginLeft: "auto" }} />
                    <button
                      style={styles.deleteAllScenesBtn}
                      onClick={handleDeleteAllScenes}
                      title="모든 씬 삭제 (되돌릴 수 없음)"
                    >
                      🗑 전체 삭제
                    </button>
                  </>
                )}
            </div>
            {(fullRenderStatus === "uploading" || fullRenderStatus === "rendering") && (
              <div style={{ padding: "8px 12px", fontSize: 12, color: "#aaa", background: "#111", display: "flex", flexDirection: "column", gap: 6 }}>
                {/* 진행 바 — current/total 이 들어온 이후(pct>=0)에만 표시. */}
                {fullRenderPct >= 0 && (
                  <div style={{ height: 6, background: "#2a2a2a", borderRadius: 3, overflow: "hidden" }}>
                    <div style={{ width: `${fullRenderPct}%`, height: "100%", background: "#6366f1", transition: "width 0.3s ease" }} />
                  </div>
                )}
                <div>
                  {fullRenderMessage}
                  {fullRenderPct >= 0 && <span style={{ color: "#6366f1", marginLeft: 6 }}>{fullRenderPct}%</span>}
                </div>
              </div>
            )}
            {/* #294 — 렌더 완료 결과: 작은 한 줄 링크 대신 미리보기 + 길이 + 다운로드 카드. */}
            {fullRenderStatus === "done" && fullRenderOutputUrl && (
              <RenderResultCard
                projectId={projectId}
                outputUrl={fullRenderOutputUrl}
                hasSubtitle={fullRenderHasSubtitle}
              />
            )}
            {fullRenderStatus === "error" && (
              <div style={{ padding: "6px 12px", fontSize: 11, color: "#f87171", background: "#111", whiteSpace: "pre-wrap", maxHeight: 200, overflowY: "auto", fontFamily: "monospace" }}>
                오류: {fullRenderMessage}
              </div>
            )}
          </div>
          )}

          {/* #360 — 전체 타임라인 바. 씬 길이 비례 블록 + 클릭 시 selectScene.
              미리보기 패널 위에 위치하여 사용자가 한눈에 영상 구조를 파악할 수 있게 한다. */}
          {visiblePanels.preview && (
            <TimelineBar
              scenes={scenes}
              selectedSceneId={selectedSceneId}
              onSelectScene={(sid) => {
                selectScene(sid);
                setSelectedSceneIds(new Set());
              }}
              bgmTracks={project?.bgm_tracks}
              // #375 — footage 원본 duration 조회용. clip_end_sec=null 트림 미설정 씬에서
              //         footage 원본 길이로 폴백하기 위해 필요.
              footageItems={serverFootage}
              // #364 — 미리보기 video 의 currentTime 을 playhead 계산에 반영.
              //   #371: TimelineBar 가 내부 selector 로 직접 구독하므로 prop 전달 불요.
              //   (명시적으로 다른 값을 표시하고 싶다면 previewCurrentSec prop 을 다시 넘기면 된다.)
              // #364 — 드래그/클릭 seek: 다른 씬이면 selectScene 으로 전환 + (재마운트 후) PreviewPanel
              //         seekRequest 처리, 같은 씬이면 바로 seek 만.
              // #384 — TimelineBar 클릭 시점을 ttsAnchor 로 보관 → 툴바의 [+ TTS 구간 추가] 버튼이 활성화됨.
              onSeekToGlobalSec={(sid, sceneLocalSec) => {
                if (sid !== selectedSceneId) {
                  selectScene(sid);
                  setSelectedSceneIds(new Set());
                }
                // requestPreviewSeek 는 tick 을 단조 증가시키므로 같은 (sid, sec) 도 재요청 가능.
                // 씬 전환 직후의 PreviewPanel 재마운트는 useEffect 가 sceneRef 매칭 후 처리.
                requestPreviewSeek(sid, sceneLocalSec);
                setTtsAnchor({ sceneId: sid, offsetSec: sceneLocalSec });
              }}
              // #382 — Alt/Shift + 클릭으로 신규 segment 추가 후보 시점 전달.
              //   Editor 에 보관된 pendingSegmentAdd 상태를 갱신해 인스펙터에 폼을 띄운다.
              // #386 — TimelineBar 의 hotspot 이 e.stopPropagation() 으로 sceneTrack
              //   onSeekToGlobalSec 를 차단하므로, 여기서 동일 흐름(selectScene +
              //   requestPreviewSeek + setTtsAnchor) 을 직접 호출해 포커스/플레이헤드 이동을
              //   보장한다. (B안의 stopPropagation 제거는 드래그/일반 클릭과 충돌 우려가 있어
              //   A안으로 처리.)
              onAddNarrationSegment={(sid, offsetSec) => {
                if (sid !== selectedSceneId) {
                  selectScene(sid);
                  setSelectedSceneIds(new Set());
                }
                requestPreviewSeek(sid, offsetSec);
                setTtsAnchor({ sceneId: sid, offsetSec });
                setPendingSegmentAdd({ sceneId: sid, offsetSec });
                setEditingSegmentId(null);
              }}
              // 기존 segment 마커 클릭 → 인스펙터/편집 UI 열기.
              // #386 — 마커 클릭도 동일 이유로 e.stopPropagation() 에 의해 sceneTrack 의
              //   포커스/seek 흐름이 끊긴다. 여기서 직접 selectScene + segment.offset_sec 로의
              //   playhead seek + ttsAnchor 갱신을 수행한다.
              onEditNarrationSegment={(sid, segId) => {
                if (sid !== selectedSceneId) {
                  selectScene(sid);
                  setSelectedSceneIds(new Set());
                }
                const scene = project?.scenes.find((s) => s.scene_id === sid);
                const seg = scene?.narration_segments?.find((s) => s.id === segId);
                const segOffset = seg?.offset_sec ?? 0;
                requestPreviewSeek(sid, segOffset);
                setTtsAnchor({ sceneId: sid, offsetSec: segOffset });
                setEditingSegmentId({ sceneId: sid, segId });
                setPendingSegmentAdd(null);
              }}
            />
          )}

          {/* #382 — narration segment 인스펙터 패널.
              TimelineBar 의 Alt/Shift+클릭(추가) 또는 마커 클릭(편집) 시 표시된다.
              "추가" 폼: text 입력 + [추가 후 TTS 생성] 버튼.
              "편집" 폼: text/offset/voice 편집 + [재생성] / [삭제] / [닫기] 버튼.
              넘침 segment 는 경고 메시지 표시.

              #383 — selectedScene 에 의존하면 React setState 비동기 갱신 race 때문에
              "사용자가 클릭한 씬 ≠ 인스펙터/onChanged 가 다루는 씬" 어긋남이 발생한다.
              인스펙터가 다룰 대상 씬은 반드시 pendingAdd.sceneId / editing.sceneId
              로부터 직접 찾는다 (selectedScene 은 단순 사이드바 선택 동기화 용도). */}
          {(() => {
            const activeSegmentSceneId =
              pendingSegmentAdd?.sceneId ?? editingSegmentId?.sceneId ?? null;
            const activeSegmentScene =
              activeSegmentSceneId != null
                ? scenes.find((s) => s.scene_id === activeSegmentSceneId) ?? null
                : null;
            if (!(pendingSegmentAdd || editingSegmentId) || !activeSegmentScene) {
              return null;
            }
            return (
              <NarrationSegmentInspector
                projectId={projectId}
                scene={activeSegmentScene}
                pendingAdd={pendingSegmentAdd}
                editing={editingSegmentId}
                onClose={() => {
                  setPendingSegmentAdd(null);
                  setEditingSegmentId(null);
                }}
                onChanged={async (nextSegments) => {
                  // #383 — store/IndexedDB 모두 activeSegmentScene.scene_id 기준으로 갱신.
                  //   기존엔 selectedScene.scene_id 로 저장해 잘못된 씬에 반영되었음.
                  updateScene(activeSegmentScene.scene_id, { narration_segments: nextSegments });
                  await dbUpdateScene(
                    projectId,
                    activeSegmentScene.scene_id,
                    { narration_segments: nextSegments } as unknown as Partial<SceneCard>,
                  );
                }}
                // #388 — TimelineBar 와 같은 길이 계산 출처를 공유.
                //   sceneCutDurations 는 Editor 본체에서 store selector 로 reactive 구독 중.
                footageItems={serverFootage}
                cutDurations={sceneCutDurations}
              />
            );
          })()}

          {/* Preview */}
          {visiblePanels.preview && (
          <>
          {/* 이어보기(전체 훑어보기) — 현재 씬부터 마지막 씬까지 자동으로 이어 재생. */}
          <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 6, padding: "0 14px", flexShrink: 0 }}>
            {/* 배속 — 이어보기 중에도 즉시 반영된다. */}
            <label style={{ color: "#64748b", fontSize: 11 }} htmlFor="seq-rate">
              배속
            </label>
            <select
              id="seq-rate"
              value={sequentialRate}
              onChange={(e) => {
                const r = parseFloat(e.target.value);
                setSequentialRate(r);
                try { localStorage.setItem("sequential_play_rate", String(r)); } catch { /* ignore */ }
              }}
              title="이어보기 재생 속도 — 빠르게 훑어볼 때 사용합니다"
              style={{
                background: "#1e293b",
                color: sequentialRate === 1 ? "#93c5fd" : "#fbbf24",
                border: "1px solid #334155",
                borderRadius: 6,
                padding: "4px 6px",
                fontSize: 12,
                fontWeight: 700,
                cursor: "pointer",
              }}
            >
              {SEQUENTIAL_RATES.map((r) => (
                <option key={r} value={r}>
                  {r}×
                </option>
              ))}
            </select>
            <button
              onClick={toggleSequentialPlay}
              disabled={scenes.length === 0}
              title="현재 씬부터 마지막 씬까지 자동으로 이어 재생합니다"
              style={{
                background: isSequentialPlay ? "#7f1d1d" : "#1e293b",
                color: isSequentialPlay ? "#fecaca" : "#93c5fd",
                border: "1px solid " + (isSequentialPlay ? "#b91c1c" : "#334155"),
                borderRadius: 6,
                padding: "4px 10px",
                fontSize: 12,
                fontWeight: 700,
                cursor: scenes.length === 0 ? "not-allowed" : "pointer",
              }}
            >
              {isSequentialPlay ? "⏹ 멈춤" : "▶ 이어보기"}
            </button>
          </div>
          <PreviewPanel
            key={(() => {
              if (!selectedScene) return "no-scene";
              // #fit-bug — footage 객체 전체를 key에 포함해 footage 변경 시 1회 재마운트.
              // previewTrigger를 key에 넣으면 동일 footage에 대해 setPreviewTrigger++ → 또 재마운트가
              // 일어나 loadPreview가 2번 호출되고 응답 race로 stale 영상이 박히는 회귀가 발생했다.
              // previewTrigger는 PreviewPanel 안의 별도 effect로 재로드하지 않고, footage/씬 변경 시
              // 자동 재마운트만 사용한다.
              const f = selectedScene.footage as Record<string, unknown> | null;
              const footageKey = f ? JSON.stringify(f) : "";
              return `${selectedScene.scene_id}-${footageKey}`;
            })()}
            projectId={projectId}
            scene={selectedScene}
            outputWidth={outputWidth}
            outputHeight={outputHeight}
            previewHeight={previewHeight}
            onPreviewHeightChange={setPreviewHeight}
            refreshSignal={previewTrigger}
            showOverlayGuides={showOverlayGuides}
            // #364 — playhead 동기화: video timeupdate 를 store 에 전달.
            //         씬 변경 시(key 재마운트) 0 으로 자연 초기화됨.
            onTimeUpdate={setPreviewCurrentTime}
            // #364 — TimelineBar 드래그/클릭의 seek 요청을 PreviewPanel 내부 useEffect 가 처리.
            seekRequest={previewSeekRequest}
            // 이어보기(연속 재생) — true 면 loop off + 로드 후 자동재생, ended 시 onSceneEnded.
            sequentialPlay={isSequentialPlay}
            sequentialRate={sequentialRate}
            onSceneEnded={handleSceneEnded}
            onUpdateGraphicOverlays={async (graphics) => {
              // #222 후속 — 캔버스 드래그로 graphic overlay (x,y) 갱신.
              // onUpdateGraphicOverlays(SceneItem용)과 동일 파이프라인.
              if (!selectedScene) return;
              updateScene(selectedScene.scene_id, { graphic_overlays: graphics });
              await _localUpdateScene(projectId, selectedScene.scene_id, { graphic_overlays: graphics });
              await _invalidateScenePreview(projectId, selectedScene.scene_id);
              await patchScene(projectId, selectedScene.scene_id, { graphic_overlays: graphics }).catch(() => {});
              setPreviewTrigger((n) => n + 1);
            }}
          />
          </>
          )}

          {/* Scene list */}
          {visiblePanels.scenes && (
          <div style={styles.sceneList}>
            {scenes.map((scene, idx) => (
              <Fragment key={scene.scene_id}>
              <SceneItem
                key={scene.scene_id}
                projectId={projectId}
                scene={scene}
                isSelected={selectedSceneId === scene.scene_id}
                isMultiSelected={selectedSceneIds.has(scene.scene_id)}
                isHighlighted={highlightedSceneIds.includes(scene.scene_id)}
                isPlayingTts={ttsPlayingSceneId === scene.scene_id}
                footageItems={serverFootage}
                sidebarAssets={assets}
                audioAssets={audioAssets}
                imageAssets={imageAssets}
                copiedOverlays={copiedOverlays}
                copiedEffects={copiedEffects}
                hasCopiedScene={copiedScene !== null}
                copiedSceneId={copiedScene?.scene_id ?? null}
                tagDictVersion={tagDictVersion}
                onSelect={(e) => {
                  // 이어보기 중 사용자가 씬을 수동 클릭하면 이어보기 모드 해제.
                  //   (자동 전환은 handleSceneEnded 가 selectScene 을 직접 호출하므로 여기 안 탄다.)
                  if (isSequentialPlay) setIsSequentialPlay(false);
                  if (e.shiftKey) {
                    // Shift+클릭: 다중 선택 토글, 단일 선택 해제
                    selectScene(null);
                    setSelectedSceneIds((prev) => {
                      const next = new Set(prev);
                      if (next.has(scene.scene_id)) {
                        next.delete(scene.scene_id);
                      } else {
                        next.add(scene.scene_id);
                      }
                      return next;
                    });
                  } else {
                    // 일반 클릭: 단일 선택, 다중 선택 초기화
                    selectScene(scene.scene_id);
                    setSelectedSceneIds(new Set());
                  }
                }}
                onOpenEffect={() => { selectScene(scene.scene_id); openEffectModal(); }}
                onNarrationChange={(text) => handleNarrationChange(scene.scene_id, text)}
                onUpdateComments={(comments) => {
                  // SceneCommentEditor 가 서버 반영까지 마친 뒤 최신 목록을 넘긴다.
                  // 여기서는 로컬 상태(zustand + IndexedDB)만 맞춰주면 된다.
                  updateScene(scene.scene_id, { comments });
                  void _localUpdateScene(projectId, scene.scene_id, { comments });
                }}
                onSubtitleChange={async (text) => {
                  // 자막 텍스트 변경 — null이면 narration_text 사용으로 되돌림.
                  // SBV는 렌더 시 새로 생성되니 미리보기 캐시는 무관(영상에 자막을 굽지 않음).
                  updateScene(scene.scene_id, { subtitle_text: text });
                  await _localUpdateScene(projectId, scene.scene_id, { subtitle_text: text });
                  await patchScene(projectId, scene.scene_id, { subtitle_text: text }).catch(() => {});
                }}
                onPlayTts={() => handlePlayTts(scene)}
                onRegenTts={() => handleRegenTts(scene.scene_id)}
                onDeleteScene={() => handleDeleteScene(scene.scene_id)}
                onCopyScene={() => handleCopyScene(scene.scene_id)}
                onPasteScene={() => handlePasteScene(scene.scene_id)}
                onSplitScene={() => handleOpenSplitScene(scene.scene_id)}
                onAutoFitSpeed={() => handleAutoFitSceneSpeed(scene.scene_id)}
                onChangeFootageSource={() => setFootageDialogSceneId(scene.scene_id)}
                onUpdateOverlays={async (overlays) => {
                  // 텍스트 오버레이는 모달 닫힘 시(저장 시점)에만 반영 — 자동 트리거 유지.
                  // ⚠ preview-cache 를 삭제하지 않으면 옛 mp4 가 그대로 재생되어 폰트/색/내용/애니메이션
                  // 변경이 시각적으로 반영되지 않는다. 캐시 무효화 후 setPreviewTrigger 로 강제 재생성.
                  updateScene(scene.scene_id, { text_overlays: overlays });
                  await _localUpdateScene(projectId, scene.scene_id, { text_overlays: overlays });
                  await patchScene(projectId, scene.scene_id, { text_overlays: overlays }).catch(() => {});
                  await _invalidateScenePreview(projectId, scene.scene_id);
                  setPreviewTrigger((n) => n + 1);
                }}
                onUpdateSpeed={async (speed, opts) => {
                  // opts.ramp — 재생 속도 모드(정속/가속)와 가속 비율 변경을 같은 경로로 처리.
                  // #279 — 슬라이더 onChange에서 호출되므로 opts.sliderOnly로 자동 미리보기 억제.
                  // #358/#363 — sliderOnly 면 첫 호출에 직전 snapshot 보관(pointerup 시 한 단계 push),
                  //              아니면 commitProjectHistory 로 즉시 한 단계 push.
                  if (opts?.sliderOnly) stashSliderSnapshotIfFirst();
                  else commitProjectHistory();
                  // footage_speed 변경 시 오버레이 출력 타임라인도 함께 재조정(씬 밖 밀림 방지).
                  //   재조정된 오버레이 배열을 store·IndexedDB·서버 patch 세 곳 모두에 포함해 일치시킨다.
                  const speedFields: Partial<SceneCard> = { footage_speed: speed, ...(opts?.ramp ?? {}) };
                  const rescaled = rescaleSceneOverlayTimingsForSpeed(
                    scene, speedFields, getSceneRawFootageDuration(scene, serverFootage),
                  );
                  const speedPatch: Partial<SceneCard> = {
                    ...speedFields,
                    text_overlays: rescaled.text_overlays,
                    shape_overlays: rescaled.shape_overlays,
                    graphic_overlays: rescaled.graphic_overlays,
                    image_overlays: rescaled.image_overlays,
                    mosaic_overlays: rescaled.mosaic_overlays,
                  };
                  updateScene(scene.scene_id, speedPatch);
                  await _localUpdateScene(projectId, scene.scene_id, speedPatch);
                  // 서버 동기화 debounce — 슬라이더 폭주 방지.
                  const timers = speedSyncTimers.current;
                  const prev = timers.get(scene.scene_id);
                  if (prev) clearTimeout(prev);
                  const t = setTimeout(() => {
                    timers.delete(scene.scene_id);
                    patchScene(projectId, scene.scene_id, speedPatch).catch(() => {});
                  }, 800);
                  timers.set(scene.scene_id, t);
                  if (!opts?.sliderOnly) {
                    setPreviewTrigger((n) => n + 1);
                  }
                }}
                onUpdateFootageAudioVolume={async (vol, opts) => {
                  // 원본 음량 슬라이더 — store/IndexedDB 즉시, 서버 patchScene + 미리보기 재렌더는
                  // 디바운스 후 1회. footage_audio_volume 더킹은 백엔드 ffmpeg가 mp4에 굽는다.
                  // 따라서 슬라이더 변경 후 실제로 다른 더킹을 들으려면 mp4를 다시 만들어야 한다.
                  // #363 — sliderOnly 첫 호출 시 snapshot 보관. (이 핸들러는 사실상 항상 sliderOnly.)
                  if (opts?.sliderOnly !== false) stashSliderSnapshotIfFirst();
                  else commitProjectHistory();
                  updateScene(scene.scene_id, { footage_audio_volume: vol } as never);
                  await _localUpdateScene(projectId, scene.scene_id, { footage_audio_volume: vol } as never);
                  const timers = audioVolumeSyncTimers.current;
                  const prev = timers.get(scene.scene_id);
                  if (prev) clearTimeout(prev);
                  const t = setTimeout(async () => {
                    timers.delete(scene.scene_id);
                    await patchScene(projectId, scene.scene_id, { footage_audio_volume: vol } as never).catch(() => {});
                    // preview-cache 삭제 후 미리보기 재트리거 — 새 더킹 값으로 mp4 재생성.
                    await _invalidateScenePreview(projectId, scene.scene_id);
                    setPreviewTrigger((n) => n + 1);
                  }, 800);
                  timers.set(scene.scene_id, t);
                }}
                onUpdateTtsSpeed={async (speed, opts) => {
                  // TTS 재생 속도 슬라이더 — Chirp3-HD atempo 후처리.
                  // footage_audio_volume 과 동일한 패턴: store/IndexedDB 즉시, 서버 patchScene +
                  // preview-cache 삭제 + 미리보기 재렌더는 디바운스 후 1회.
                  // tts_speed 는 렌더/미리보기 시점에 build_merged_wav 가 각 segment WAV 에
                  // atempo 필터를 적용해 반영하므로, 슬라이더 값 변경 후 미리보기를 다시 만들어야
                  // 실제 속도 변화가 들린다 (mp4 재생성 필요).
                  if (opts?.sliderOnly !== false) stashSliderSnapshotIfFirst();
                  else commitProjectHistory();
                  updateScene(scene.scene_id, { tts_speed: speed } as never);
                  await _localUpdateScene(projectId, scene.scene_id, { tts_speed: speed } as never);
                  const timers = ttsSpeedSyncTimers.current;
                  const prev = timers.get(scene.scene_id);
                  if (prev) clearTimeout(prev);
                  const t = setTimeout(async () => {
                    timers.delete(scene.scene_id);
                    await patchScene(projectId, scene.scene_id, { tts_speed: speed } as never).catch(() => {});
                    // preview-cache 삭제 후 미리보기 재트리거 — 새 tts_speed 로 merged wav 재합성.
                    await _invalidateScenePreview(projectId, scene.scene_id);
                    setPreviewTrigger((n) => n + 1);
                  }, 800);
                  timers.set(scene.scene_id, t);
                }}
                onUpdateTtsEmotion={async (emotion) => {
                  // #tts-emotion — 말투 지시문. store/IndexedDB 즉시 반영,
                  // 서버 patchScene 은 타이핑 중 폭주를 막기 위해 디바운스 후 1회.
                  // TTS 재합성이 필요한 값이라 preview-cache 는 건드리지 않는다
                  // (사용자가 "TTS 생성" 을 눌러야 실제 음성에 반영된다).
                  commitProjectHistory();
                  updateScene(scene.scene_id, { tts_emotion: emotion } as never);
                  // 말투가 바뀌면 기존 음성은 옛 말투로 만들어진 것이므로
                  // synced=false 로 내려 재생성 버튼(↻)이 활성화되게 한다.
                  // 이게 없으면 사용자가 말투만 바꾸고 "이미 생성됨" 으로 오인한다.
                  if (scene.tts) {
                    updateSceneTts(scene.scene_id, { ...scene.tts, synced: false });
                  }
                  await _localUpdateScene(projectId, scene.scene_id, { tts_emotion: emotion } as never);
                  const timers = ttsEmotionSyncTimers.current;
                  const prev = timers.get(scene.scene_id);
                  if (prev) clearTimeout(prev);
                  const t = setTimeout(async () => {
                    timers.delete(scene.scene_id);
                    await patchScene(projectId, scene.scene_id, { tts_emotion: emotion } as never).catch(() => {});
                  }, 800);
                  timers.set(scene.scene_id, t);
                }}
                onUpdateSceneSfx={async (patch, opts) => {
                  // 씬 본체 SFX — 변경된 필드만 store/IndexedDB 즉시, 서버 patchScene 은 디바운스 후 1회.
                  // 미리보기에는 useSfxPreview 가 자동으로 반영되므로 mp4 재생성은 필요 없다.
                  // 다만 최종 렌더 결과(final_output.mp4) 에 반영되려면 사용자가 렌더를 다시 돌려야 한다.
                  // #363 — sliderOnly 면 snapshot 보관, 아니면 즉시 commit.
                  if (opts?.sliderOnly) stashSliderSnapshotIfFirst();
                  else commitProjectHistory();
                  updateScene(scene.scene_id, patch as never);
                  await _localUpdateScene(projectId, scene.scene_id, patch as never);
                  const timers = sceneSfxSyncTimers.current;
                  const prev = timers.get(scene.scene_id);
                  if (prev) clearTimeout(prev);
                  const t = setTimeout(() => {
                    timers.delete(scene.scene_id);
                    patchScene(projectId, scene.scene_id, patch as never).catch(() => {});
                  }, 600);
                  timers.set(scene.scene_id, t);
                  if (!opts?.sliderOnly) {
                    setPreviewTrigger((n) => n + 1);
                  }
                }}
                onHighlightAsset={(assetId) => setHighlightedAssetId(assetId)}
                onCopyOverlays={() => setCopiedOverlays(scene.text_overlays ?? [])}
                onCopySingleOverlay={(overlay) => setCopiedOverlays([overlay])}
                onCopySingleGraphic={(g) => setCopiedGraphic(g)}
                onPasteGraphic={async () => {
                  // #graphic-copy — 복사한 그래픽 오버레이를 이 씬에 추가한다.
                  // id 는 새로 발급해야 같은 씬에 두 번 붙여넣어도 충돌하지 않는다
                  // (onPasteOverlays / onPasteEffects 와 동일 규칙).
                  if (!copiedGraphic) return;
                  commitProjectHistory();
                  const pasted = {
                    ...copiedGraphic,
                    id: Math.random().toString(36).slice(2, 10),
                    params: { ...(copiedGraphic.params ?? {}) },
                  };
                  const next = [...(scene.graphic_overlays ?? []), pasted];
                  updateScene(scene.scene_id, { graphic_overlays: next } as never);
                  await _localUpdateScene(projectId, scene.scene_id, { graphic_overlays: next } as never);
                  await patchScene(projectId, scene.scene_id, { graphic_overlays: next } as never).catch(() => {});
                  await _invalidateScenePreview(projectId, scene.scene_id);
                  setPreviewTrigger((n) => n + 1);
                }}
                copiedGraphic={copiedGraphic}
                onPasteOverlays={async () => {
                  if (copiedOverlays.length === 0) return;
                  const newOverlays = copiedOverlays.map((o) => ({
                    ...o,
                    id: Math.random().toString(36).slice(2, 10),
                  }));
                  const merged = [...(scene.text_overlays ?? []), ...newOverlays];
                  updateScene(scene.scene_id, { text_overlays: merged });
                  await _localUpdateScene(projectId, scene.scene_id, { text_overlays: merged });
                  patchScene(projectId, scene.scene_id, { text_overlays: merged }).catch(() => {});
                }}
                onCopyEffects={() => {
                  // #281 — 현재 씬의 모든 효과 종류를 한 번에 클립보드에 저장.
                  //        빈 배열도 그대로 저장(빈 효과 복사를 명시적으로 허용).
                  //        deep clone — 이후 원본 씬이 수정되어도 클립보드는 영향받지 않도록.
                  const cloneList = <T,>(arr: T[] | undefined): T[] =>
                    typeof structuredClone === "function"
                      ? structuredClone(arr ?? [])
                      : JSON.parse(JSON.stringify(arr ?? []));
                  setCopiedEffects({
                    text_overlays: cloneList(scene.text_overlays),
                    shape_overlays: cloneList(scene.shape_overlays),
                    graphic_overlays: cloneList(scene.graphic_overlays),
                    filters: cloneList(scene.filters),
                    motions: cloneList(scene.motions),
                    mosaic_overlays: cloneList(scene.mosaic_overlays),
                    image_overlays: cloneList(scene.image_overlays),
                    speed_lines: cloneList(scene.speed_lines),
                  });
                }}
                onPasteEffects={async () => {
                  // #281 — 클립보드의 모든 효과 종류를 현재 씬에 append.
                  // id 재발급 규칙:
                  //  - text_overlays: 태그 prefix("tag-{tag}-...")는 유지, 나머지는 새 random id.
                  //    (duplicateScene 패턴 차용 — 다단어 태그명 지원 위해 scene.tags 기반 비교.)
                  //  - shape_overlays: 새 random id (충돌 회피).
                  //  - graphic_overlays / mosaic_overlays: 새 random id + source_id 그대로 유지.
                  //  - filters / motions: id 자체가 없음 → source_id만 유지 (그대로 복제).
                  if (!copiedEffects) return;
                  const newId = () => Math.random().toString(36).slice(2, 10);
                  const sceneTags = scene.tags ?? [];

                  const reissueTextId = (ov: TextOverlay): TextOverlay => {
                    const matchedTag = sceneTags.find((t) => ov.id?.startsWith(`tag-${t}-`));
                    return {
                      ...ov,
                      id: matchedTag ? `tag-${matchedTag}-${newId()}` : newId(),
                    };
                  };

                  const newTextOverlays = copiedEffects.text_overlays.map(reissueTextId);
                  const newShapeOverlays = copiedEffects.shape_overlays.map((s) => ({
                    ...s,
                    id: `shape_${Date.now()}_${newId()}`,
                  }));
                  const newGraphicOverlays = copiedEffects.graphic_overlays.map((g) => ({
                    ...g,
                    id: newId(),
                    // source_id는 그대로 유지 (태그 추적 일관)
                  }));
                  const newMosaicOverlays = copiedEffects.mosaic_overlays.map((m) => ({
                    ...m,
                    id: newId(),
                  }));
                  // 이미지 오버레이 — 새 random id 부여. image_asset_id는 그대로 유지(같은 자산 참조).
                  // 기존 클립보드(image_overlays 필드 없음)는 빈 배열 폴백.
                  const newImageOverlays = (copiedEffects.image_overlays ?? []).map((m) => ({
                    ...m,
                    id: `image_${Date.now()}_${newId()}`,
                  }));
                  // filters / motions는 id 필드 자체가 없어 그대로 spread.
                  const newFilters = copiedEffects.filters.map((f) => ({ ...f }));
                  const newMotions = copiedEffects.motions.map((m) => ({ ...m }));
                  // 집중선(속도선) — 새 random id + seed 재발급(같은 씬에 겹쳐 붙여도 다른 패턴).
                  // 기존 클립보드(speed_lines 필드 없음)는 빈 배열 폴백.
                  const newSpeedLines = (copiedEffects.speed_lines ?? []).map((s) => ({
                    ...s,
                    id: `speedlines_${Date.now()}_${newId()}`,
                  }));

                  const patch: Partial<SceneCard> = {
                    text_overlays: [...(scene.text_overlays ?? []), ...newTextOverlays],
                    shape_overlays: [...(scene.shape_overlays ?? []), ...newShapeOverlays],
                    graphic_overlays: [...(scene.graphic_overlays ?? []), ...newGraphicOverlays],
                    filters: [...(scene.filters ?? []), ...newFilters],
                    motions: [...(scene.motions ?? []), ...newMotions],
                    mosaic_overlays: [...(scene.mosaic_overlays ?? []), ...newMosaicOverlays],
                    image_overlays: [...(scene.image_overlays ?? []), ...newImageOverlays],
                    speed_lines: [...(scene.speed_lines ?? []), ...newSpeedLines],
                  };
                  // 단일 patch로 모든 효과 종류를 묶어 동기화 (5단계 1회).
                  await applyScenePatch(scene.scene_id, patch);
                }}
                onUpdateClipOffset={async (xOffset, yOffset, opts) => {
                  // #279 — X/Y 오프셋 슬라이더 onChange. sliderOnly면 자동 미리보기 트리거 억제.
                  if (!scene.footage || scene.footage.type !== "clip") return;
                  const updatedFootage = { ...scene.footage, x_offset: xOffset, y_offset: yOffset };
                  await applyScenePatch(scene.scene_id, { footage: updatedFootage }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateClipFit={async (fit) => {
                  // #258 footage 종횡비 fit 옵션. clip + kenburns/kenburns_copy_img 모두 지원.
                  const ftype = scene.footage?.type;
                  if (!scene.footage || (ftype !== "clip" && ftype !== "kenburns" && ftype !== "kenburns_copy_img")) return;
                  const updatedFootage = { ...scene.footage, fit };
                  await applyScenePatch(scene.scene_id, { footage: updatedFootage });
                }}
                onUpdateSceneDuration={async (durationSec, opts) => {
                  // 켄번스/삽입 이미지 씬 화면 길이. TTS 없는 씬이라 duration_hint_sec 이 곧 씬 길이.
                  // 드래그 중(sliderOnly)은 로컬만 갱신, pointerUp/blur 커밋 시 서버 patchScene 1회.
                  await applyScenePatch(scene.scene_id, { duration_hint_sec: durationSec }, {
                    invalidateCache: !opts?.sliderOnly,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateClipRotation={async (rotation) => {
                  // #263 원본 영상 수동 회전. select 입력 — 자동 트리거 유지.
                  if (!scene.footage || scene.footage.type !== "clip") return;
                  const updatedFootage = { ...scene.footage, rotation };
                  await applyScenePatch(scene.scene_id, { footage: updatedFootage });
                }}
                onUpdateShapeOverlays={async (shapes, opts) => {
                  // 도형 오버레이 — 슬라이더 onChange면 sliderOnly로 자동 트리거 억제.
                  await applyScenePatch(scene.scene_id, { shape_overlays: shapes }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateGraphicOverlays={async (graphics, opts) => {
                  // #205 그래픽 오버레이. 슬라이더 onChange면 sliderOnly로 자동 트리거 억제.
                  await applyScenePatch(scene.scene_id, { graphic_overlays: graphics }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateFilters={async (filters, opts) => {
                  // #206 씬 필터. 슬라이더 onChange면 sliderOnly로 자동 트리거 억제.
                  await applyScenePatch(scene.scene_id, { filters }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateMotions={async (motions, opts) => {
                  // #207 씬 모션. 슬라이더 onChange면 sliderOnly로 자동 트리거 억제.
                  await applyScenePatch(scene.scene_id, { motions }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateMosaicOverlays={async (mosaics, opts) => {
                  // #264 모자이크 오버레이. 슬라이더 onChange면 sliderOnly로 자동 트리거 억제.
                  await applyScenePatch(scene.scene_id, { mosaic_overlays: mosaics }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateImageOverlays={async (images, opts) => {
                  // 이미지 오버레이. 슬라이더 onChange면 sliderOnly로 자동 트리거 억제.
                  await applyScenePatch(scene.scene_id, { image_overlays: images }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onUpdateSpeedLines={async (lines, opts) => {
                  // 집중선(속도선) 오버레이. 슬라이더 onChange면 sliderOnly로 자동 트리거 억제.
                  await applyScenePatch(scene.scene_id, { speed_lines: lines }, {
                    invalidateCache: true,
                    triggerRefresh: !opts?.sliderOnly,
                  });
                }}
                onAddTag={async (tagName) => {
                  // 1) 효과 머지: applySceneTags가 새 scene을 반환 (tags + text_overlays + filters + graphic_overlays + motions 갱신)
                  const { scene: nextScene } = applySceneTags(scene, tagName, tagDict);
                  // 2) 변경된 부분만 store/DB/서버에 반영
                  //    motions(#207)도 포함 — crash 태그 활성화 시 shake 모션이 함께 머지됨.
                  const patch = {
                    tags: nextScene.tags ?? [],
                    text_overlays: nextScene.text_overlays,
                    filters: nextScene.filters ?? [],
                    graphic_overlays: nextScene.graphic_overlays ?? [],
                    motions: nextScene.motions ?? [],
                  };
                  updateScene(scene.scene_id, patch);
                  await _localUpdateScene(projectId, scene.scene_id, patch);
                  // 3) 프리뷰 캐시 무효화 + patchScene + 프리뷰 재로딩
                  await _invalidateScenePreview(projectId, scene.scene_id);
                  await patchScene(projectId, scene.scene_id, patch).catch(() => {});
                  setPreviewTrigger((n) => n + 1);
                }}
                speakers={project?.speakers ?? []}
                defaultVoice={project?.tts_voice ?? ""}
                onChangeSpeakerTags={async (tags) => {
                  // 화자 태그는 시각 효과가 없다 — tags 만 갱신하면 되고,
                  // 미리보기(영상)도 바뀌지 않으므로 캐시 무효화가 필요 없다.
                  // 실제 목소리는 TTS 재생성 시점에 반영된다.
                  const patch = { tags };
                  updateScene(scene.scene_id, patch);
                  await _localUpdateScene(projectId, scene.scene_id, patch);
                  await patchScene(projectId, scene.scene_id, patch).catch(() => {});
                }}
                onManageSpeakers={() => setSpeakerModalOpen(true)}
                onRemoveTag={async (tagName) => {
                  const nextScene = removeSceneTag(scene, tagName);
                  const patch = {
                    tags: nextScene.tags ?? [],
                    text_overlays: nextScene.text_overlays,
                    filters: nextScene.filters ?? [],
                    graphic_overlays: nextScene.graphic_overlays ?? [],
                    motions: nextScene.motions ?? [],
                  };
                  updateScene(scene.scene_id, patch);
                  await _localUpdateScene(projectId, scene.scene_id, patch);
                  await _invalidateScenePreview(projectId, scene.scene_id);
                  await patchScene(projectId, scene.scene_id, patch).catch(() => {});
                  setPreviewTrigger((n) => n + 1);
                }}
              />
              {/* #245 — 씬 사이 인서트 핸들. hover 시에만 진해지며 클릭 시 빈 씬 삽입.
                  dnd-kit drag 충돌 회피를 위해 onClick 자체에 stopPropagation. */}
              <SceneInsertHandle
                onClick={() => handleInsertSceneAfter(idx)}
                onPickFromLibrary={() => setClipLibraryInsertIndex(idx)}
                onInsertImage={() => setInsertImageIndex(idx)}
              />
              </Fragment>
            ))}
            {/* #245 — 씬 리스트 끝에 큰 + 추가 버튼 (빈 씬 카드 1개 생성). */}
            <button
              type="button"
              style={styles.addSceneBtn}
              onClick={(e) => { e.stopPropagation(); handleAddScene(); }}
              title="새 씬 추가"
            >
              + 씬 추가
            </button>
          </div>
          )}
        </div>

        {/* #257 — 우측 효과 추가 사이드 패널 (씬 카드의 효과 버튼을 일원화) */}
        {visiblePanels.effects && (
        <EffectAddPanel
          selectedScene={selectedScene}
          selectedSceneIds={selectedSceneIds}
          onAddShape={handleSideAddShape}
          onAddGraphic={handleSideAddGraphic}
          onAddFilter={handleSideAddFilter}
          onAddMotion={handleSideAddMotion}
          onAddTextOverlay={handleSideAddTextOverlay}
          onAddMosaic={handleSideAddMosaic}
          onAddImage={handleSideAddImage}
          onAddSpeedLines={handleSideAddSpeedLines}
        />
        )}
        {/* 이미지 오버레이 자산 선택 모달 */}
        {imageAssetPickerOpen && (
          <ImageAssetPickerModal
            projectId={projectId}
            assets={imageAssets}
            onPick={handlePickImageAsset}
            onClose={() => {
              setImageAssetPickerOpen(false);
              setImageAssetPickerSceneId(null);
            }}
          />
        )}

        {/* #316 — 자연어 편집 AI 사이드 패널 */}
        {GEMINI_ENABLED && nlEditorPanelOpen && (
          <NlEditorPanel
            projectId={projectId}
            onClose={() => setNlEditorPanelOpen(false)}
            onAfterApply={async () => {
              // apply/undo 후 IndexedDB 에 저장된 프로젝트 메타가 변하지는 않지만,
              // 백엔드 RenderSpec 캐시(render_spec.json) 가 갱신되었다. 미리보기 재로드를
              // 위해 nlEditApplyTick 을 bump 하면 위 effect 가 모든 씬 preview-cache 를 삭제하고
              // previewTrigger 를 올려 자동으로 새 영상이 뜬다.
              // (zustand 의 bumpNlEditApplyTick 은 NlEditorPanel 내부에서 이미 호출됨.)
            }}
          />
        )}
      </div>

      {effectModalOpen && selectedScene && (
        <EffectModal scene={selectedScene} projectId={projectId} onClose={closeEffectModal} />
      )}

      {ttsStartVoice !== null && (
        <TtsStartModal
          initialVoice={ttsStartVoice}
          missingCount={scenes.filter((s) => s.narration_text && (!s.tts || s.tts.status !== "ready")).length}
          onCancel={() => setTtsStartVoice(null)}
          onStart={async (voice) => {
            setTtsStartVoice(null);
            // 프로젝트 기본 목소리도 함께 갱신 — 다음번 재생성 시에도 같은 목소리가 기본이 된다.
            if (project && voice !== project.tts_voice) {
              setProject({ ...project, tts_voice: voice });
              await updateProject(projectId, { tts_voice: voice }).catch(() => {});
            }
            await handleGenerateMissing(voice);
          }}
        />
      )}

      {typecastModalOpen && (
        <TypecastVoiceModal
          currentVoiceId={project?.tts_voice ?? null}
          onClose={() => setTypecastModalOpen(false)}
          onSelect={async (voiceId) => {
            setTypecastModalOpen(false);
            if (project && voiceId !== project.tts_voice) {
              setProject({ ...project, tts_voice: voiceId });
              await updateProject(projectId, { tts_voice: voiceId }).catch(() => {});
            }
          }}
        />
      )}

      {splitTargetSceneId !== null && (() => {
        const target = scenes.find((s) => s.scene_id === splitTargetSceneId);
        if (!target || target.footage?.type !== "clip") return null;
        const f = target.footage as import("../types").ClipEffect;
        const url = `/api/projects/${projectId}/footage/${encodeURIComponent(f.file)}/stream`;
        return (
          <SplitSceneDialog
            open={true}
            videoUrl={url}
            filename={f.file}
            baseStart={f.clip_start_sec ?? undefined}
            baseEnd={f.clip_end_sec ?? undefined}
            onClose={() => setSplitTargetSceneId(null)}
            onApply={(cuts, totalDur) => handleApplySplit(cuts, totalDur)}
          />
        );
      })()}

      {tagDictModalOpen && (
        <TagDictionaryModal
          onClose={() => setTagDictModalOpen(false)}
          onChanged={() => setTagDictVersion((v) => v + 1)}
        />
      )}

      {/* 다중 화자 사전 — 화자명 ↔ TTS 목소리 등록 */}
      {speakerModalOpen && (
        <SpeakerDictionaryModal
          projectId={projectId}
          onClose={() => setSpeakerModalOpen(false)}
          onSaved={(speakers) => {
            // 서버가 진실의 원천 — 저장 결과를 그대로 로컬 project 에 반영한다.
            if (project) setProject({ ...project, speakers });
          }}
        />
      )}

      {/* #294 — 단축키 도움말 모달 */}
      {shortcutHelpOpen && <ShortcutHelpModal onClose={() => setShortcutHelpOpen(false)} />}

      {/* #362 — 소스영상 변경 다이얼로그 */}
      {footageDialogSceneId !== null && (() => {
        const target = scenes.find((s) => s.scene_id === footageDialogSceneId);
        if (!target) return null;
        return (
          <FootageSourceDialog
            projectId={projectId}
            scene={target}
            assets={assets}
            footageItems={serverFootage}
            onApply={(asset, trim) => { void reassignSceneFootage(target.scene_id, asset, trim); }}
            onClose={() => setFootageDialogSceneId(null)}
          />
        );
      })()}

      {/* #380 — 화면 녹화 모달. 사이드바 footage 헤더의 "+ 화면 녹화" 버튼이 토글한다.
           저장 완료 시 onSaved 콜백이 새 assets 을 store/IndexedDB 의 studio_assets 에 머지하고,
           서버 footage 인덱스도 다시 fetch 해서 사이드바가 새로고침 없이 즉시 갱신된다. */}
      <ScreenRecorderDialog
        open={recorderOpen}
        projectId={projectId}
        onClose={() => setRecorderOpen(false)}
        onSaved={(newAssets) => {
          if (!newAssets.length) return;
          // 1) zustand store 의 project.studio_assets 즉시 머지 — 사이드바가 곧바로 새 항목을 인식.
          //    addStudioAssets 는 ScreenRecorderDialog 내부에서 이미 IndexedDB 에 반영했으므로
          //    여기서는 store 상태만 동기화한다.
          const cur = useStore.getState().project;
          if (cur && cur.project_id === projectId) {
            const merged = [...(cur.studio_assets ?? []), ...newAssets];
            setProject({ ...cur, studio_assets: merged });
          }
          // 2) 서버 footage 인덱스 재조회 — saveAsset 이 syncStudioAssetFile 로 서버에도 올렸으므로
          //    /api/projects/{id}/footage 가 최신 파일 목록을 반환한다.
          api.get<import("../types").FootageItem[]>(`/api/projects/${projectId}/footage`)
            .then((r) => setServerFootage(r.data))
            .catch(() => {});
        }}
      />

      {/* 클립 라이브러리 삽입 모달 — 선택 시 해당 위치에 새 씬을 만들고 클립을 지정한다. */}
      <ClipLibraryModal
        open={clipLibraryInsertIndex !== null}
        onClose={() => setClipLibraryInsertIndex(null)}
        onPick={(clip) => {
          const idx = clipLibraryInsertIndex;
          if (idx === null) return;
          setClipLibraryInsertIndex(null);
          handleInsertSceneFromClipLibrary(idx, clip);
        }}
      />

      {/* 중간 이미지 삽입 모달 — 이미지 자산 선택 시 전체화면 이미지 전용 씬을 자동 생성해 삽입. */}
      {insertImageIndex !== null && (
        <ImageAssetPickerModal
          projectId={projectId}
          assets={imageAssets}
          onPick={(asset) => {
            const idx = insertImageIndex;
            setInsertImageIndex(null);
            if (idx === null) return;
            void handleInsertImageSceneAt(idx, asset);
          }}
          onClose={() => setInsertImageIndex(null)}
        />
      )}

      <audio ref={audioRef} onEnded={() => setTtsPlaying(null)} style={{ display: "none" }} />
    </DndContext>
  );
}

function ResolutionSelector({ outputWidth, outputHeight, onChange }: {
  outputWidth: number;
  outputHeight: number;
  onChange: (w: number, h: number) => void;
}) {
  const matched = RESOLUTION_PRESETS.find(
    (p) => p.width === outputWidth && p.height === outputHeight
  );
  const isCustom = !matched || matched.width === 0;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <select
        style={{ ...styles.voiceSelect, fontSize: 11 }}
        value={matched && matched.width !== 0 ? `${matched.width}x${matched.height}` : "custom"}
        onChange={(e) => {
          const preset = RESOLUTION_PRESETS.find(
            (p) => `${p.width}x${p.height}` === e.target.value
          );
          if (preset && preset.width !== 0) onChange(preset.width, preset.height);
        }}
        title="출력 해상도"
      >
        {RESOLUTION_PRESETS.map((p) => (
          <option
            key={p.label}
            value={p.width !== 0 ? `${p.width}x${p.height}` : "custom"}
          >{p.label}</option>
        ))}
      </select>
      {isCustom && (
        <>
          <input
            type="number"
            style={{ width: 60, padding: "4px 6px", background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 5, fontSize: 11 }}
            value={outputWidth}
            min={240}
            max={7680}
            onChange={(e) => onChange(Number(e.target.value) || outputWidth, outputHeight)}
          />
          <span style={{ color: "#666", fontSize: 11 }}>×</span>
          <input
            type="number"
            style={{ width: 60, padding: "4px 6px", background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 5, fontSize: 11 }}
            value={outputHeight}
            min={240}
            max={7680}
            onChange={(e) => onChange(outputWidth, Number(e.target.value) || outputHeight)}
          />
        </>
      )}
    </div>
  );
}

// 프로젝트 전역 배경색(letterbox 여백) 선택기.
// 저장값은 "#RRGGBB" 또는 named "black". 컬러 인풋은 항상 #RRGGBB 를 다루므로
// "black" 은 #000000 으로 표시하고, #000000 을 고르면 기본 "black" 으로 되돌린다(회귀 안전).
function BgColorPicker({ bgColor, onPreview, onCommit }: {
  bgColor: string;
  /** 드래그 중 매 이벤트: 로컬 미리보기용(서버 저장·렌더 없음). */
  onPreview: (color: string) => void;
  /** 피커 닫힐 때 1회: 서버 저장 + 프리뷰 재렌더. */
  onCommit: (color: string) => void;
}) {
  const toHex = (c: string): string => {
    if (!c) return "#000000";
    if (c.toLowerCase() === "black") return "#000000";
    return /^#[0-9a-fA-F]{6}$/.test(c) ? c : "#000000";
  };
  // #000000 은 named "black" 로 저장 — 기존 프로젝트와 값 일치.
  const norm = (v: string) => (v.toLowerCase() === "#000000" ? "black" : v);
  const isDefault = !bgColor || bgColor.toLowerCase() === "black" || bgColor === "#000000";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }} title="영상 배경색(letterbox 여백)">
      <span style={{ color: "#888", fontSize: 11 }}>배경</span>
      <input
        type="color"
        style={{ width: 26, height: 22, padding: 0, background: "#1a1a1a", border: "1px solid #333", borderRadius: 5, cursor: "pointer" }}
        value={toHex(bgColor)}
        // <input type="color"> 는 드래그 중 onChange 가 연속 발생한다. 여기선 로컬 미리보기만.
        onChange={(e) => onPreview(norm(e.target.value))}
        // 피커를 닫을 때(포커스 해제) 최종값 1회만 커밋 → 서버 저장·프리뷰 렌더 폭주 방지.
        onBlur={(e) => onCommit(norm(e.target.value))}
      />
      {!isDefault && (
        <button
          type="button"
          style={{ background: "transparent", color: "#888", border: "none", fontSize: 11, cursor: "pointer", padding: "0 2px" }}
          title="기본값(검정)으로 되돌리기"
          onClick={() => onCommit("black")}
        >⟲</button>
      )}
    </div>
  );
}

function DraggableAsset({ asset, projectId, isHighlighted, onGoToStudio }: { asset: FootageAsset; projectId: string; isHighlighted?: boolean; onGoToStudio?: () => void }) {
  const { attributes, listeners, setNodeRef: dndRef, isDragging } = useDraggable({ id: asset.asset_id, disabled: !!isHighlighted });
  const highlightRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isHighlighted && highlightRef.current) {
      highlightRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [isHighlighted]);

  return (
    <div
      ref={(el) => {
        dndRef(el);
        (highlightRef as React.MutableRefObject<HTMLDivElement | null>).current = el;
      }}
      {...listeners}
      {...attributes}
      style={{
        ...styles.thumb,
        opacity: isDragging ? 0.4 : 1,
        border: isHighlighted ? "2px solid #facc15" : "2px solid transparent",
        boxSizing: "border-box",
        cursor: isHighlighted && onGoToStudio ? "pointer" : "grab",
      }}
      title={asset.label || asset.filename.replace(/\.[^.]+$/, "")}
      onClick={() => { if (isHighlighted && onGoToStudio) onGoToStudio(); }}
    >
      <AssetThumbnail projectId={projectId} asset={asset} />
      <div style={styles.thumbLabel}>{asset.label || asset.filename.replace(/\.[^.]+$/, "")}</div>
    </div>
  );
}

const DISSOLVE_SEC = 0.5;

/** 가속(ramp) 모드 정속%/가속% 입력 — 입력 중엔 로컬 값만 바꾸고 blur/Enter 시 한 번만 반영
 *  (타이핑마다 히스토리·미리보기가 갱신되지 않도록). */
function RampPercentInput({ label, title, value, onCommit }: {
  label: string;
  title: string;
  value: number;
  onCommit: (pct: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => { setDraft(String(value)); }, [value]);
  const commit = () => {
    const n = Number(draft);
    if (!Number.isFinite(n)) { setDraft(String(value)); return; }
    const pct = Math.round(Math.min(100, Math.max(0, n)));
    if (pct !== value) onCommit(pct);
    else setDraft(String(value));
  };
  return (
    <label title={title} style={{ display: "flex", alignItems: "center", gap: 2, color: "#888", fontSize: 10 }}>
      {label}
      <input
        type="number"
        min={0}
        max={100}
        step={5}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        style={{
          width: 38, background: "#1e1e1e", color: "#ddd",
          border: "1px solid #333", borderRadius: 4, fontSize: 10, padding: "1px 3px",
        }}
      />
      %
    </label>
  );
}

/** ramp/ramp_down 기본 배속 입력 (×). 가속의 시작 / 감속의 끝 속도. */
function RampBaseInput({ title, value, onCommit }: {
  title: string;
  value: number;
  onCommit: (base: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => { setDraft(String(value)); }, [value]);
  const commit = () => {
    const n = Number(draft);
    if (!Number.isFinite(n) || n <= 0) { setDraft(String(value)); return; }
    const v = Math.round(Math.min(16, Math.max(0.1, n)) * 100) / 100;
    if (v !== value) onCommit(v);
    else setDraft(String(value));
  };
  return (
    <label title={title} style={{ display: "flex", alignItems: "center", gap: 2, color: "#888", fontSize: 10 }}>
      기본
      <input
        type="number"
        min={0.1}
        max={16}
        step={0.1}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
        style={{
          width: 42, background: "#1e1e1e", color: "#ddd",
          border: "1px solid #333", borderRadius: 4, fontSize: 10, padding: "1px 3px",
        }}
      />
      ×
    </label>
  );
}

function DurationStatus({ scene, footageItems }: { scene: SceneCard; footageItems: import("../types").FootageItem[] }) {
  const ttsDur = scene.tts?.status === "ready" ? scene.tts.duration_sec : null;
  const match = scene.match;
  const effectType = scene.footage?.type;
  const speed = scene.footage_speed ?? 1.0;
  // 정속/가속(ramp) 공용 출력 길이 — lib/speedRamp 단일 헬퍼.
  const speedSpec = getSceneSpeedSpec(scene);
  const fmt = (s: number) => s.toFixed(1) + "s";

  if (!effectType || effectType === "title_gen" || effectType === "multiscreen") return null;
  if (ttsDur === null) return null;

  // 배속 적용 시 영상 표시 포맷:
  //   배속=1.0: "TTS / 영상"
  //   배속≠1.0: "TTS / 배속반영영상 (원본영상)"
  // 배속반영영상 = 원본영상 / 배속 (예: 0.25x → 4배 길어짐)
  const speedSuffix = (effective: number, original: number) =>
    Math.abs(effective - original) < 0.05
      ? fmt(effective)
      : `${fmt(effective)} (${fmt(original)})`;
  const speedTitle = (effective: number, original: number) =>
    Math.abs(effective - original) < 0.05
      ? fmt(effective)
      : `${fmt(effective)} — 배속 ${speed}×${speedSpec.mode === "ramp" ? "(가속)" : speedSpec.mode === "ramp_down" ? "(감속)" : ""} 반영, 원본 ${fmt(original)}`;

  // 이미지 기반(kenburns) — 기본은 TTS 길이로 자동 맞춰진다.
  // 사용자가 footage_speed를 조절하면 영상 길이만 단축/연장되고 TTS는 그대로.
  // → 영상이 TTS보다 짧으면 경고 표시(clip과 동일 포맷).
  if (effectType === "kenburns" || effectType === "kenburns_copy_img") {
    const outDur = speed > 0 ? speedOutputDuration(ttsDur, speedSpec) : ttsDur;
    if (outDur + 0.05 < ttsDur) {
      return (
        <div style={styles.durationWarning} title={`영상(${fmt(outDur)}) < TTS(${fmt(ttsDur)}). 속도가 너무 빠릅니다.`}>
          ⚠ TTS {fmt(ttsDur)} / 영상 {fmt(outDur)}
        </div>
      );
    }
    return (
      <div style={styles.durationOk} title={`TTS(${fmt(ttsDur)}) / 영상(${fmt(outDur)}) — 정상`}>
        ✓ TTS {fmt(ttsDur)} / 영상 {fmt(outDur)}
      </div>
    );
  }

  // 영상 길이 결정 — 씬의 clip_start_sec/clip_end_sec 메타 우선(씬 나누기 결과 반영).
  // 메타 없으면 match.trim_*, 그것도 없으면 footage 전체 duration_sec.
  //
  // [P2 가드] match 폴백 신뢰성 — 과거 씬 분할/재할당 버그의 잔재로 footage.file 과
  // match.source_file 이 어긋난 씬이 state.json 에 남아 있을 수 있다 (예: footage 는
  // "Scene-007.mp4"(15초)인데 match 는 옛 137초짜리 원본을 그대로 들고 있는 경우).
  // 이 경우 match.trim_end - match.trim_start 폴백은 *전혀 다른 영상의 길이* 를
  // 화면에 표시하게 된다 (P1 패치 이전부터 누적된 데이터). 사용자 결정에 따라
  // state.json 자체는 건드리지 않고 표시 로직에서만 basename 일치를 검사한다.
  // 일치하지 않으면 폴백을 건너뛰고 영상 길이 미표시("/ —")로 안전하게 떨어진다.
  let footageDur: number | null = null;
  const fc = scene.footage as { file?: string; clip_start_sec?: number | null; clip_end_sec?: number | null } | null;
  const cs = fc?.clip_start_sec ?? null;
  const ce = fc?.clip_end_sec ?? null;
  const filename = fc?.file ?? null;
  const item = filename ? footageItems.find((f) => f.filename === filename) : undefined;
  const footageBasename = filename ? filename.split("/").pop() ?? null : null;
  const matchBasename = match?.source_file ? match.source_file.split("/").pop() ?? null : null;
  const matchTrustworthy =
    !!footageBasename && !!matchBasename && footageBasename === matchBasename;
  if (cs !== null || ce !== null) {
    const baseStart = cs ?? 0;
    // match 가 신뢰 가능한 경우에만 trim_end 폴백 허용. 그 외엔 item.duration_sec 만.
    const baseEnd = ce ?? item?.duration_sec ?? (matchTrustworthy && match ? match.trim_end : null);
    if (baseEnd !== null) footageDur = baseEnd - baseStart;
  }
  if (footageDur === null && match && matchTrustworthy) {
    footageDur = match.trim_end - match.trim_start;
  }
  if (footageDur === null && item) {
    footageDur = item.duration_sec;
  }

  // footage 길이를 결정할 수 없으면 — TTS 만 표시하고 영상 길이 부분은 생략.
  // 과거에는 null 반환으로 라벨 전체가 사라졌으나, TTS 정보까지 사라지는 건 과한 회귀.
  if (footageDur === null) {
    return (
      <div
        style={styles.durationOk}
        title={`TTS(${fmt(ttsDur)}) — 영상 길이 메타 없음`}
      >
        ✓ {fmt(ttsDur)} / —
      </div>
    );
  }

  // 배속 반영 후 실제 영상 길이 (clip이 setpts로 늘어남/줄어듦).
  const effectiveDur = speed > 0 && speed !== 1.0 ? speedOutputDuration(footageDur, speedSpec) : footageDur;
  const minNeeded = ttsDur + DISSOLVE_SEC;

  if (effectiveDur < minNeeded) {
    return (
      <div
        style={styles.durationWarning}
        title={`영상(${speedTitle(effectiveDur, footageDur)}) < TTS+dissolve(${fmt(minNeeded)}). 렌더링 시 자동 조정됩니다.`}
      >
        ⚠ TTS {fmt(ttsDur)} / 영상 {speedSuffix(effectiveDur, footageDur)}
      </div>
    );
  }

  return (
    <div
      style={styles.durationOk}
      title={`TTS(${fmt(ttsDur)}) / 영상(${speedTitle(effectiveDur, footageDur)}) — 정상`}
    >
      ✓ {fmt(ttsDur)} / {speedSuffix(effectiveDur, footageDur)}
    </div>
  );
}

/**
 * 자막 전용 텍스트 편집기 — 기본은 닫혀 있고 narration_text가 그대로 자막으로 사용된다.
 * 사용자가 "자막 따로 쓰기" 버튼을 누르면 편집 영역이 열리고, 그 텍스트가 SBV에 사용된다.
 * 빈 문자열로 저장하면 다시 narration_text 폴백.
 */
function SubtitleEditor({
  narrationText, subtitleText, onChange,
}: { narrationText: string; subtitleText: string | null; onChange: (text: string | null) => void }) {
  const hasCustom = subtitleText !== null && subtitleText.length > 0;
  const [editing, setEditing] = useState(hasCustom);
  const [draft, setDraft] = useState<string>(hasCustom ? (subtitleText as string) : narrationText);

  // subtitleText가 외부에서 바뀌면 draft 동기화(다른 씬 선택·복원 케이스).
  useEffect(() => {
    if (hasCustom) {
      setDraft(subtitleText as string);
      setEditing(true);
    } else {
      setDraft(narrationText);
      setEditing(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subtitleText]);

  if (!editing) {
    return (
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          type="button"
          style={styles.subtitleToggleBtn}
          onClick={(e) => {
            e.stopPropagation();
            setDraft(narrationText);
            setEditing(true);
          }}
          title="이 씬의 자막 텍스트를 나레이션과 다르게 지정"
        >
          🅰 자막 따로 쓰기
        </button>
      </div>
    );
  }
  return (
    <div style={styles.subtitleBox} onClick={(e) => e.stopPropagation()}>
      <div style={styles.subtitleLabel}>
        <span>자막 텍스트 (TTS 대본과 별도)</span>
        <button
          type="button"
          style={styles.subtitleResetBtn}
          onClick={() => {
            onChange(null);
            setDraft(narrationText);
            setEditing(false);
          }}
          title="나레이션 텍스트로 되돌리기"
        >
          ↩ 기본으로
        </button>
      </div>
      <textarea
        style={styles.narrationInput}
        value={draft}
        rows={2}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const trimmed = draft.trim();
          if (!trimmed || trimmed === narrationText.trim()) {
            onChange(null);
          } else {
            onChange(draft);
          }
        }}
        placeholder="자막에 보일 텍스트... (비우면 나레이션 텍스트를 사용)"
      />
    </div>
  );
}

// #294 — 렌더 완료 결과 카드.
// 작게 한 줄로 표시되던 다운로드 링크를, 영상 미리보기 + 길이 + 다운로드/미리보기로 확장.
function RenderResultCard({ projectId, outputUrl, hasSubtitle = true }: { projectId: string; outputUrl: string; hasSubtitle?: boolean }) {
  const [duration, setDuration] = useState<number | null>(null);
  const [showPreview, setShowPreview] = useState(false);
  const previewSrc = `/api/projects/${projectId}/render/output`;

  useEffect(() => {
    const v = document.createElement("video");
    v.preload = "metadata";
    v.src = previewSrc;
    v.onloadedmetadata = () => {
      if (isFinite(v.duration) && v.duration > 0) setDuration(v.duration);
    };
    return () => { v.src = ""; };
  }, [previewSrc]);

  const fmtDur = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = Math.round(s % 60);
    return `${m}:${String(sec).padStart(2, "0")}`;
  };

  return (
    <div style={styles.renderResultCard}>
      <div style={styles.renderResultRow}>
        <span style={styles.renderResultBadge}>✓ 렌더링 완료</span>
        {duration !== null && <span style={styles.renderResultDur}>길이 {fmtDur(duration)}</span>}
        <button
          style={styles.renderResultPreviewBtn}
          onClick={() => setShowPreview((v) => !v)}
        >
          {showPreview ? "미리보기 닫기" : "▶ 미리보기"}
        </button>
        <a href={outputUrl} download="final_output.mp4" style={styles.renderResultDownload}>📽 영상 다운로드</a>
        {hasSubtitle && (
          <a
            href={`/api/projects/${projectId}/render/subtitle`}
            download="final_output.sbv"
            style={styles.renderResultDownloadSub}
            title="렌더링 결과의 SBV 자막 파일"
          >📄 자막 다운로드</a>
        )}
      </div>
      {showPreview && (
        <video
          src={previewSrc}
          controls
          autoPlay
          style={styles.renderResultVideo}
        />
      )}
    </div>
  );
}

// #294 — 단축키 도움말 모달.
function ShortcutHelpModal({ onClose }: { onClose: () => void }) {
  const rows: { keys: string; desc: string }[] = [
    { keys: "⌘/Ctrl + Z", desc: "되돌리기 (Undo)" },
    { keys: "⌘/Ctrl + ⇧ + Z", desc: "다시 실행 (Redo, ⌘/Ctrl+Y 도 가능)" },
    { keys: "⌘/Ctrl + B", desc: "BGM 편집 열기" },
    { keys: "H", desc: "오버레이 편집 가이드 표시/숨김" },
    { keys: "?", desc: "이 도움말 열기/닫기" },
  ];
  useEffect(() => {
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div style={styles.shortcutBackdrop} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={styles.shortcutModal}>
        <div style={styles.shortcutTitle}>⌨ 키보드 단축키</div>
        <div style={styles.shortcutList}>
          {rows.map((r) => (
            <div key={r.keys} style={styles.shortcutRow}>
              <kbd style={styles.shortcutKbd}>{r.keys}</kbd>
              <span style={styles.shortcutDesc}>{r.desc}</span>
            </div>
          ))}
        </div>
        <button style={styles.shortcutClose} onClick={onClose}>닫기</button>
      </div>
    </div>
  );
}

/**
 * 렌더링 split-button.
 *  - 본 버튼: 전체 렌더링(영상+자막) 트리거.
 *  - ▾ 메뉴 → "영상만 렌더링": 본 버튼과 동일(전체 렌더). 결과 mp4는 렌더 완료 시 자동 다운로드.
 *  - ▾ 메뉴 → "자막만 생성": 영상 없이 TTS 길이 누적으로 SBV 즉시 생성 + 다운로드.
 */
function RenderSplitButton({
  status, onRender, onRenderVideoOnly, projectId,
}: {
  status: "idle" | "uploading" | "rendering" | "done" | "error";
  onRender: () => void;
  /** 자막 없이 영상만 렌더링. 미지정이면 onRender 호출. */
  onRenderVideoOnly?: () => void;
  projectId: string;
}) {
  const [open, setOpen] = useState(false);
  const [subtitleBusy, setSubtitleBusy] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const busy = status === "uploading" || status === "rendering";

  // 외부 클릭으로 메뉴 닫기.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const mainLabel =
    status === "uploading" ? "업로드 중..." :
    status === "rendering" ? "렌더링 중..." :
    "▶ 렌더링";

  async function handleSubtitleOnly() {
    setOpen(false);
    setSubtitleBusy(true);
    try {
      const res = await fetch(`/api/projects/${projectId}/render/subtitle`, { method: "POST" });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        throw new Error(`자막 생성 실패 (HTTP ${res.status}): ${detail.slice(0, 200)}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "quick_subtitle.sbv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 잠시 후 revoke
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      ui.error((e as Error).message);
    } finally {
      setSubtitleBusy(false);
    }
  }

  return (
    <div ref={wrapRef} style={{ position: "relative", display: "inline-flex" }}>
      <button
        style={{ ...styles.renderBtnMain, opacity: busy ? 0.6 : 1 }}
        onClick={onRender}
        disabled={busy}
        title="전체 렌더링 시작 (영상 + 자막)"
      >
        {mainLabel}
      </button>
      <button
        style={{ ...styles.renderBtnCaret, opacity: busy ? 0.6 : 1 }}
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        title="개별 작업 메뉴"
      >
        ▾
      </button>
      {open && (
        <div style={styles.renderMenu}>
          <button
            type="button"
            style={styles.renderMenuItemBtn}
            onClick={() => { setOpen(false); (onRenderVideoOnly ?? onRender)(); }}
            disabled={busy}
            title="자막(.sbv) 생성 없이 영상만 렌더링"
          >
            📽 영상만 렌더링
          </button>
          <button
            type="button"
            style={styles.renderMenuItemBtn}
            onClick={handleSubtitleOnly}
            disabled={subtitleBusy}
            title="영상 없이 TTS 길이 기반 SBV만 즉시 생성"
          >
            {subtitleBusy ? "📄 생성 중..." : "📄 자막만 생성 (.sbv)"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── 도형 오버레이 편집기 ────────────────────────────────────────────────────

function ShapeOverlayEditor({ shape, onSave, onClose, projectId, audioAssets }: {
  shape: ShapeOverlay | null;
  onSave: (sh: ShapeOverlay) => void;
  onClose: () => void;
  // #290 — SFX 편집 컨텍스트.
  projectId?: string;
  audioAssets?: FootageAsset[];
}) {
  const newId = () => `shape_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
  const [form, setForm] = useState<ShapeOverlay>(shape ?? {
    id: newId(),
    shape: "rect",
    x: 0.1,
    y: 0.1,
    width: 0.2,
    height: 0.2,
    color: "#ff0000",
    opacity: 0.7,
    start_sec: 0,
    end_sec: null,
    // #290 — SFX 기본값.
    sfx_asset_id: null,
    sfx_volume: 0.7,
    // #287 — 등장/사라짐 효과 기본값
    enter_effect: "fade",
    exit_effect: "fade",
    enter_duration_sec: 0.3,
    exit_duration_sec: 0.3,
  });

  function handleChange(field: keyof ShapeOverlay, value: unknown) {
    setForm((prev) => ({ ...prev, [field]: value }));
  }

  return (
    <div style={{ background: "#111", border: "1px solid #333", borderRadius: 8, padding: 12, marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ color: "#aaa", fontSize: 12, fontWeight: 700, marginBottom: 4 }}>도형 오버레이 편집</div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1, minWidth: 120 }}>
          <label style={{ color: "#666", fontSize: 11 }}>도형</label>
          <select
            value={form.shape}
            onChange={(e) => handleChange("shape", e.target.value as ShapeType)}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
          >
            <option value="rect">사각형</option>
            <option value="ellipse">타원</option>
          </select>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1, minWidth: 80 }}>
          <label style={{ color: "#666", fontSize: 11 }}>색상</label>
          <input type="color" value={form.color} onChange={(e) => handleChange("color", e.target.value)}
            style={{ width: "100%", height: 30, border: "none", background: "none", cursor: "pointer" }} />
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {(["x", "y", "width", "height"] as const).map((field) => {
          const fieldLabel: Record<string, string> = {
            x: "X (0=좌, 1=우)",
            y: "Y (0=상, 1=하)",
            width: "너비 (비율)",
            height: "높이 (비율)",
          };
          return (
          <div key={field} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 72 }}>{fieldLabel[field]}</label>
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={form[field] as number}
              onChange={(e) => handleChange(field, parseFloat(e.target.value))}
              style={{ flex: 1, accentColor: "#4ade80" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {(form[field] as number).toFixed(2)}
            </span>
          </div>
          );
        })}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{ color: "#666", fontSize: 11, flexShrink: 0 }}>불투명도</span>
        <input
          type="range" min={0} max={1} step={0.05} value={form.opacity}
          onChange={(e) => handleChange("opacity", parseFloat(e.target.value))}
          style={{ flex: 1, accentColor: "#4ade80" }}
        />
        <span style={{ color: "#aaa", fontSize: 11, minWidth: 35, textAlign: "right" }}>{Math.round(form.opacity * 100)}%</span>
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>시작(초)</label>
          <input type="number" step={0.1} value={form.start_sec}
            onChange={(e) => handleChange("start_sec", parseFloat(e.target.value) || 0)}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>종료(초, 빈칸=끝까지)</label>
          <input type="number" step={0.1} value={form.end_sec ?? ""}
            onChange={(e) => handleChange("end_sec", e.target.value === "" ? null : parseFloat(e.target.value))}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }} />
        </div>
      </div>

      {/* #287 — 등장/사라짐 효과 (도형: none/fade 2종, sliderOnly 패턴) */}
      <EnterExitEffectFields
        kind="non-text"
        enterEffect={form.enter_effect ?? "fade"}
        exitEffect={form.exit_effect ?? "fade"}
        enterDuration={form.enter_duration_sec ?? 0.3}
        exitDuration={form.exit_duration_sec ?? 0.3}
        onEnterEffectChange={(v) => handleChange("enter_effect", v)}
        onExitEffectChange={(v) => handleChange("exit_effect", v)}
        onEnterDurationChange={(v) => handleChange("enter_duration_sec", v)}
        onExitDurationChange={(v) => handleChange("exit_duration_sec", v)}
        accent="#4ade80"
      />

      {/* #290 — SFX 섹션 */}
      {projectId && audioAssets && (
        <SfxEditor
          projectId={projectId}
          audioAssets={audioAssets}
          sfxAssetId={form.sfx_asset_id ?? null}
          sfxVolume={form.sfx_volume ?? 0.7}
          onChange={(patch) => {
            if ("sfx_asset_id" in patch) handleChange("sfx_asset_id", patch.sfx_asset_id ?? null);
            if ("sfx_volume" in patch && patch.sfx_volume !== undefined) handleChange("sfx_volume", patch.sfx_volume);
          }}
          accent="#4ade80"
        />
      )}

      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button onClick={onClose} style={{ padding: "4px 12px", background: "#111", color: "#666", border: "1px solid #444", borderRadius: 5, cursor: "pointer", fontSize: 12 }}>취소</button>
        <button onClick={() => onSave(form)} style={{ padding: "4px 12px", background: "#0f2318", color: "#4ade80", border: "1px solid #166534", borderRadius: 5, cursor: "pointer", fontSize: 12 }}>저장</button>
      </div>
    </div>
  );
}

// ── #287 — 등장/사라짐 효과 공통 UI 필드 ──────────────────────────────────
// 슬라이더는 sliderOnly: true로 자동 미리보기 트리거 억제 (#279 패턴 준수).
// select 변경(이산값)은 즉시 미리보기 트리거 (sliderOnly: false 또는 미지정).
// kind="text"면 typing/zoom 옵션 포함, "non-text"면 none/fade만.
function EnterExitEffectFields({
  kind,
  enterEffect,
  exitEffect,
  enterDuration,
  exitDuration,
  onEnterEffectChange,
  onExitEffectChange,
  onEnterDurationChange,
  onExitDurationChange,
  accent,
}: {
  kind: "text" | "non-text" | "image";
  enterEffect: string;
  exitEffect: string;
  enterDuration: number;
  exitDuration: number;
  onEnterEffectChange: (v: string) => void;
  onExitEffectChange: (v: string) => void;
  onEnterDurationChange: (v: number) => void;
  onExitDurationChange: (v: number) => void;
  accent: string;
}) {
  const options =
    kind === "text"
      ? [
          { value: "none", label: "없음" },
          { value: "fade", label: "페이드" },
          { value: "typing", label: "타이핑" },
          { value: "zoom", label: "줌" },
        ]
      : kind === "image"
      ? [
          { value: "none", label: "없음" },
          { value: "fade", label: "페이드" },
          { value: "slide_in_left", label: "슬라이드 (←)" },
          { value: "slide_in_right", label: "슬라이드 (→)" },
          { value: "slide_in_top", label: "슬라이드 (↑)" },
          { value: "slide_in_bottom", label: "슬라이드 (↓)" },
          { value: "zoom", label: "줌인" },
          { value: "pop", label: "팝" },
        ]
      : [
          { value: "none", label: "없음" },
          { value: "fade", label: "페이드" },
        ];
  const selectStyle: React.CSSProperties = {
    background: "#1a1a1a",
    color: "#ccc",
    border: "1px solid #333",
    borderRadius: 4,
    padding: "4px 6px",
    fontSize: 12,
    flex: 1,
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>등장</label>
        <select
          value={enterEffect}
          onChange={(e) => onEnterEffectChange(e.target.value)}
          style={selectStyle}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <span style={{ color: "#aaa", fontSize: 11, minWidth: 40, textAlign: "right" }}>
          {enterDuration.toFixed(2)}s
        </span>
      </div>
      {enterEffect !== "none" && (
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>지속(s)</label>
          <input
            type="range" min={0} max={2} step={0.05}
            value={enterDuration}
            onChange={(e) => onEnterDurationChange(parseFloat(e.target.value))}
            style={{ flex: 1, accentColor: accent }}
          />
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>사라짐</label>
        <select
          value={exitEffect}
          onChange={(e) => onExitEffectChange(e.target.value)}
          style={selectStyle}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <span style={{ color: "#aaa", fontSize: 11, minWidth: 40, textAlign: "right" }}>
          {exitDuration.toFixed(2)}s
        </span>
      </div>
      {exitEffect !== "none" && (
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>지속(s)</label>
          <input
            type="range" min={0} max={2} step={0.05}
            value={exitDuration}
            onChange={(e) => onExitDurationChange(parseFloat(e.target.value))}
            style={{ flex: 1, accentColor: accent }}
          />
        </div>
      )}
    </div>
  );
}

// #264 — 모자이크 오버레이 펼침 편집 패널.
// ShapeOverlayEditor와 동일한 좌표/시간 UI에 mode(pixelate/blur)와 pixel_size/blur_radius 슬라이더를 추가했다.
// 외부에서 칩 클릭으로 열고 닫으며, 변경 시 onPatch로 patch를 전달한다 (GraphicOverlayEditor 패턴과 동일).
function MosaicOverlayEditor({
  overlay,
  onPatch,
  onClose,
}: {
  overlay: MosaicOverlay;
  /** #279 — opts.sliderOnly: 슬라이더/number input onChange 호출 시 자동 미리보기 트리거 억제. */
  onPatch: (patch: Partial<MosaicOverlay>, opts?: { sliderOnly?: boolean }) => void;
  onClose: () => void;
}) {
  return (
    <div style={{ background: "#1a1209", border: "1px solid #7c2d12", borderRadius: 8, padding: 12, marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ color: "#fb923c", fontSize: 12, fontWeight: 700 }}>모자이크 오버레이 편집</div>
        <button
          onClick={onClose}
          style={{ background: "transparent", color: "#888", border: "none", cursor: "pointer", fontSize: 14 }}
          title="편집 닫기"
        >
          ✕
        </button>
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1, minWidth: 120 }}>
          <label style={{ color: "#666", fontSize: 11 }}>도형 (1차: ellipse는 사각형 폴백)</label>
          <select
            value={overlay.shape}
            onChange={(e) => onPatch({ shape: e.target.value as MosaicShape })}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
          >
            <option value="rect">사각형</option>
            <option value="ellipse">타원 (폴백)</option>
          </select>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1, minWidth: 120 }}>
          <label style={{ color: "#666", fontSize: 11 }}>모드</label>
          <select
            value={overlay.mode}
            onChange={(e) => onPatch({ mode: e.target.value as MosaicMode })}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
          >
            <option value="pixelate">픽셀화</option>
            <option value="blur">블러</option>
          </select>
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {(["x", "y", "width", "height"] as const).map((field) => {
          const fieldLabel: Record<string, string> = {
            x: "X (0=좌, 1=우)",
            y: "Y (0=상, 1=하)",
            width: "너비 (비율)",
            height: "높이 (비율)",
          };
          return (
            <div key={field} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>{fieldLabel[field]}</label>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={overlay[field] as number}
                onChange={(e) => onPatch({ [field]: parseFloat(e.target.value) } as Partial<MosaicOverlay>, { sliderOnly: true })}
                style={{ flex: 1, accentColor: "#fb923c" }}
              />
              <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
                {(overlay[field] as number).toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>
          {overlay.mode === "blur" ? "블러 반경(px)" : "픽셀 크기(px)"}
        </label>
        {overlay.mode === "blur" ? (
          <input
            type="range"
            min={1}
            max={50}
            step={1}
            value={overlay.blur_radius}
            onChange={(e) => onPatch({ blur_radius: parseInt(e.target.value, 10) || 1 }, { sliderOnly: true })}
            style={{ flex: 1, accentColor: "#fb923c" }}
          />
        ) : (
          <input
            type="range"
            min={2}
            max={60}
            step={1}
            value={overlay.pixel_size}
            onChange={(e) => onPatch({ pixel_size: parseInt(e.target.value, 10) || 2 }, { sliderOnly: true })}
            style={{ flex: 1, accentColor: "#fb923c" }}
          />
        )}
        <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
          {overlay.mode === "blur" ? overlay.blur_radius : overlay.pixel_size}
        </span>
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>시작(초)</label>
          <input
            type="number"
            step={0.1}
            value={overlay.start_sec}
            onChange={(e) => onPatch({ start_sec: parseFloat(e.target.value) || 0 }, { sliderOnly: true })}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
          />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>종료(초, 빈칸=끝까지)</label>
          <input
            type="number"
            step={0.1}
            value={overlay.end_sec ?? ""}
            onChange={(e) => onPatch({ end_sec: e.target.value === "" ? null : parseFloat(e.target.value) }, { sliderOnly: true })}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
          />
        </div>
      </div>
    </div>
  );
}

// ── 이미지 오버레이 펼침 편집 패널 ─────────────────────────────────────────
// MosaicOverlayEditor와 동일한 좌표/시간 UI에 자산 변경 드롭다운 + 캡션 입력 그룹을 추가했다.
// 이미지 영역(width/height)과 캡션은 한 묶음으로 묶여 함께 움직인다(데이터 모델 자체가 통합).
function ImageOverlayEditor({
  projectId,
  overlay,
  imageAssets,
  audioAssets,
  onPatch,
  onClose,
}: {
  projectId: string;
  overlay: ImageOverlay;
  imageAssets: FootageAsset[];
  /** #sfx-presets — 이미지 오버레이도 SFX 트리거 지원. studio 자산 + 프리셋 라이브러리 공통. */
  audioAssets?: FootageAsset[];
  onPatch: (patch: Partial<ImageOverlay>, opts?: { sliderOnly?: boolean }) => void;
  onClose: () => void;
}) {
  const accent = "#22d3ee";
  const currentAsset = imageAssets.find((a) => a.asset_id === overlay.image_asset_id);
  return (
    <div style={{ background: "#062a33", border: "1px solid #155e75", borderRadius: 8, padding: 12, marginTop: 8, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ color: accent, fontSize: 12, fontWeight: 700 }}>이미지 오버레이 편집</div>
        <button
          onClick={onClose}
          style={{ background: "transparent", color: "#888", border: "none", cursor: "pointer", fontSize: 14 }}
          title="편집 닫기"
        >
          ✕
        </button>
      </div>

      {/* 자산 변경 — 현재 선택된 이미지 자산. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <label style={{ color: "#666", fontSize: 11 }}>이미지 자산</label>
        <select
          value={overlay.image_asset_id}
          onChange={(e) => onPatch({ image_asset_id: e.target.value }, { sliderOnly: true })}
          style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
        >
          {!currentAsset && (
            <option value={overlay.image_asset_id}>{`(미해석: ${overlay.image_asset_id})`}</option>
          )}
          {imageAssets.map((a) => (
            <option key={a.asset_id} value={a.asset_id}>
              {a.label || a.filename}
            </option>
          ))}
        </select>
        {currentAsset && projectId && (
          <div style={{ display: "flex", justifyContent: "center", marginTop: 2 }}>
            <img
              src={`/api/projects/${projectId}/footage/${encodeURIComponent(currentAsset.filename)}/thumbnail?w=160`}
              alt={currentAsset.label || currentAsset.filename}
              style={{ width: 120, height: 70, objectFit: "contain", background: "#000", borderRadius: 4, border: "1px solid #222" }}
              onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
            />
          </div>
        )}
      </div>

      {/* 위치/크기 — 도형/모자이크와 동일한 0~1 비율 슬라이더. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {(["x", "y", "width", "height"] as const).map((field) => {
          const fieldLabel: Record<string, string> = {
            x: "X (0=좌, 1=우)",
            y: "Y (0=상, 1=하)",
            width: "너비 (비율)",
            height: "높이 (비율)",
          };
          return (
            <div key={field} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>{fieldLabel[field]}</label>
              <input
                type="range"
                min={0.01}
                max={1}
                step={0.01}
                value={(overlay[field] as number) ?? 0}
                onChange={(e) => onPatch({ [field]: parseFloat(e.target.value) } as Partial<ImageOverlay>, { sliderOnly: true })}
                style={{ flex: 1, accentColor: accent }}
              />
              <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
                {((overlay[field] as number) ?? 0).toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>

      {/* 투명도 */}
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>투명도</label>
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={overlay.opacity ?? 1.0}
          onChange={(e) => onPatch({ opacity: parseFloat(e.target.value) }, { sliderOnly: true })}
          style={{ flex: 1, accentColor: accent }}
        />
        <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
          {(overlay.opacity ?? 1.0).toFixed(2)}
        </span>
      </div>

      {/* 등장/사라짐 애니메이션 — 이미지 전용 확장 (slide/zoom/pop). */}
      <EnterExitEffectFields
        kind="image"
        enterEffect={overlay.enter_effect ?? "fade"}
        exitEffect={overlay.exit_effect ?? "fade"}
        enterDuration={overlay.enter_duration_sec ?? 0.3}
        exitDuration={overlay.exit_duration_sec ?? 0.3}
        onEnterEffectChange={(v) => onPatch({ enter_effect: v as NonTextEnterExitEffect }, { sliderOnly: true })}
        onExitEffectChange={(v) => onPatch({ exit_effect: v as NonTextEnterExitEffect }, { sliderOnly: true })}
        onEnterDurationChange={(v) => onPatch({ enter_duration_sec: v }, { sliderOnly: true })}
        onExitDurationChange={(v) => onPatch({ exit_duration_sec: v }, { sliderOnly: true })}
        accent={accent}
      />

      {/* 캡션 — caption_text가 비어있지 않으면 이미지 아래에 박스 합성. */}
      <div style={{ borderTop: "1px solid #114a55", paddingTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ color: "#22d3ee", fontSize: 11, fontWeight: 700 }}>캡션 (선택)</div>
        <input
          type="text"
          value={overlay.caption_text ?? ""}
          onChange={(e) => {
            const v = e.target.value;
            // 빈 문자열은 null로 보내 백엔드에서 캡션 미표시로 처리되도록 한다.
            // sliderOnly:true 로 매 키스트로크마다 미리보기 재생성이 트리거되지 않게 한다.
            // 입력이 끝난 뒤(blur) 미리보기가 한 번 갱신된다 — 아래 onBlur 참고.
            onPatch({ caption_text: v.trim() === "" ? null : v }, { sliderOnly: true });
          }}
          onBlur={() => onPatch({}, { sliderOnly: false })}
          // #371 — onBlur 가 sliderOnly:false 로 직접 commit 하므로
          //        Editor 의 글로벌 blur capture 는 이 input 의 stash 를 건너뛴다.
          data-skip-history-stash="1"
          placeholder="예: 지난 영상에서…"
          style={{ background: "#1a1a1a", color: "#ddd", border: "1px solid #333", borderRadius: 4, padding: "6px 8px", fontSize: 12 }}
        />
        {(overlay.caption_text ?? "").trim() !== "" && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>폰트 크기 (비율)</label>
              <input
                type="range" min={0.01} max={0.08} step={0.005}
                value={overlay.caption_font_size ?? 0.025}
                onChange={(e) => onPatch({ caption_font_size: parseFloat(e.target.value) }, { sliderOnly: true })}
                style={{ flex: 1, accentColor: accent }}
              />
              <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
                {(overlay.caption_font_size ?? 0.025).toFixed(3)}
              </span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>텍스트 색</label>
              <input
                type="color"
                value={overlay.caption_color ?? "#FFFFFF"}
                onChange={(e) => onPatch({ caption_color: e.target.value }, { sliderOnly: true })}
                onBlur={() => onPatch({}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 40, height: 24, background: "transparent", border: "none" }}
              />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>배경 색</label>
              <input
                type="color"
                value={overlay.caption_bg_color ?? "#000000"}
                onChange={(e) => onPatch({ caption_bg_color: e.target.value }, { sliderOnly: true })}
                onBlur={() => onPatch({}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 40, height: 24, background: "transparent", border: "none" }}
              />
              <label style={{ color: "#666", fontSize: 11, marginLeft: 6 }}>배경 불투명도</label>
              <input
                type="range" min={0} max={1} step={0.05}
                value={overlay.caption_bg_opacity ?? 0.6}
                onChange={(e) => onPatch({ caption_bg_opacity: parseFloat(e.target.value) }, { sliderOnly: true })}
                style={{ flex: 1, accentColor: accent }}
              />
              <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
                {(overlay.caption_bg_opacity ?? 0.6).toFixed(2)}
              </span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ color: "#666", fontSize: 11, minWidth: 90 }}>여백 (비율)</label>
              <input
                type="range" min={0} max={0.03} step={0.001}
                value={overlay.caption_padding ?? 0.005}
                onChange={(e) => onPatch({ caption_padding: parseFloat(e.target.value) }, { sliderOnly: true })}
                style={{ flex: 1, accentColor: accent }}
              />
              <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
                {(overlay.caption_padding ?? 0.005).toFixed(3)}
              </span>
            </div>
          </>
        )}
      </div>

      {/* 시간 범위 */}
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>시작(s)</label>
          <input
            type="number"
            min={0}
            step={0.1}
            value={overlay.start_sec ?? 0}
            onChange={(e) => onPatch({ start_sec: parseFloat(e.target.value) || 0 }, { sliderOnly: true })}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
          />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>끝(s) - 빈칸=씬 끝까지</label>
          <input
            type="number"
            min={0}
            step={0.1}
            value={overlay.end_sec ?? ""}
            onChange={(e) => onPatch({ end_sec: e.target.value === "" ? null : parseFloat(e.target.value) }, { sliderOnly: true })}
            style={{ background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 4, padding: "4px 6px", fontSize: 12 }}
          />
        </div>
      </div>

      {/* #sfx-presets — SFX 섹션 (이미지 오버레이). 텍스트/도형/그래픽과 동일 패턴. */}
      {projectId && audioAssets && (
        <SfxEditor
          projectId={projectId}
          audioAssets={audioAssets}
          sfxAssetId={overlay.sfx_asset_id ?? null}
          sfxVolume={overlay.sfx_volume ?? 0.7}
          onChange={(patch) => {
            onPatch(patch as Partial<ImageOverlay>);
          }}
          accent={accent}
        />
      )}
    </div>
  );
}

// ── 이미지 자산 선택 모달 ─────────────────────────────────────────────────
// "+ 이미지" 버튼 클릭 시 띄워지는 footage 스튜디오 이미지 자산 선택 모달.
// 사용자가 자산을 클릭하면 onPick(asset) → 호출 측에서 ImageOverlay 생성.
function ImageAssetPickerModal({
  projectId,
  assets,
  onPick,
  onClose,
}: {
  projectId: string;
  assets: FootageAsset[];
  onPick: (asset: FootageAsset) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const filtered = q
    ? assets.filter((a) => (a.filename + " " + (a.label ?? "")).toLowerCase().includes(q))
    : assets;
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: 640,
          maxHeight: "80vh",
          background: "#0c1517",
          border: "1px solid #155e75",
          borderRadius: 10,
          padding: 16,
          display: "flex",
          flexDirection: "column",
          gap: 10,
          overflow: "hidden",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ color: "#22d3ee", fontWeight: 700, fontSize: 14 }}>이미지 자산 선택</div>
          <button
            onClick={onClose}
            style={{ background: "transparent", color: "#888", border: "none", cursor: "pointer", fontSize: 16 }}
            title="닫기"
          >
            ✕
          </button>
        </div>
        <input
          type="text"
          placeholder="파일명·레이블 검색…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{
            background: "#1a1a1a",
            color: "#ddd",
            border: "1px solid #333",
            borderRadius: 6,
            padding: "8px 10px",
            fontSize: 13,
          }}
        />
        {assets.length === 0 ? (
          <div style={{ color: "#666", fontSize: 12, textAlign: "center", padding: 24 }}>
            footage 스튜디오에 등록된 이미지 자산이 없습니다.
            <br />
            먼저 footage 스튜디오에서 이미지 파일을 추가해주세요.
          </div>
        ) : (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))",
              gap: 8,
              overflowY: "auto",
              padding: 4,
            }}
          >
            {filtered.map((a) => (
              <button
                key={a.asset_id}
                onClick={() => onPick(a)}
                style={{
                  background: "#111",
                  border: "1px solid #1f3a44",
                  borderRadius: 6,
                  cursor: "pointer",
                  padding: 6,
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 4,
                  color: "#ccc",
                }}
                title={a.filename}
              >
                <img
                  src={`/api/projects/${projectId}/footage/${encodeURIComponent(a.filename)}/thumbnail?w=160`}
                  alt={a.label || a.filename}
                  style={{ width: "100%", height: 70, objectFit: "cover", background: "#000", borderRadius: 4 }}
                  onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                />
                <span style={{ fontSize: 11, textAlign: "center", overflow: "hidden", textOverflow: "ellipsis", maxWidth: "100%", whiteSpace: "nowrap" }}>
                  {a.label || a.filename}
                </span>
              </button>
            ))}
            {filtered.length === 0 && q && (
              <div style={{ color: "#666", fontSize: 12, gridColumn: "1/-1", textAlign: "center" }}>
                "{query}" 와 일치하는 자산 없음
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function GraphicOverlayEditor({
  overlay,
  onPatch,
  onPatchParams,
  onClose,
  projectId,
  audioAssets,
}: {
  overlay: GraphicOverlay;
  /** #279 — opts.sliderOnly: 슬라이더/number input onChange 호출 시 자동 미리보기 트리거 억제. */
  onPatch: (patch: Partial<GraphicOverlay>, opts?: { sliderOnly?: boolean }) => void;
  onPatchParams: (p: Record<string, unknown>, opts?: { sliderOnly?: boolean }) => void;
  onClose: () => void;
  // #290 — SFX 편집 컨텍스트.
  projectId?: string;
  audioAssets?: FootageAsset[];
}) {
  // #279 — 슬라이더 onChange 단축 호출용 wrapper. 슬라이더/number input은 모두 이걸 사용.
  const onPatchSlider = (patch: Partial<GraphicOverlay>) => onPatch(patch, { sliderOnly: true });
  const onPatchParamsSlider = (p: Record<string, unknown>) => onPatchParams(p, { sliderOnly: true });
  const params = overlay.params ?? {};
  return (
    <div
      style={{
        background: "#111",
        border: "1px solid #581c87",
        borderRadius: 8,
        padding: 12,
        marginTop: 8,
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ color: "#c084fc", fontSize: 12, fontWeight: 700 }}>
        그래픽 오버레이 편집 — {overlay.type}
      </div>

      {/* 공통 위치 */}
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>X (0=좌)</label>
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={overlay.x}
          onChange={(e) => onPatchSlider({ x: parseFloat(e.target.value) })}
          style={{ flex: 1, accentColor: "#c084fc" }}
        />
        <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
          {overlay.x.toFixed(2)}
        </span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>Y (0=상)</label>
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={overlay.y}
          onChange={(e) => onPatchSlider({ y: parseFloat(e.target.value) })}
          style={{ flex: 1, accentColor: "#c084fc" }}
        />
        <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
          {overlay.y.toFixed(2)}
        </span>
      </div>

      {/* 타입별 파라미터 */}
      {overlay.type === "spotlight" && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>반경</label>
            <input
              type="range"
              min={0.05}
              max={0.6}
              step={0.01}
              value={Number(params.radius ?? 0.2)}
              onChange={(e) => onPatchParamsSlider({ radius: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Number(params.radius ?? 0.2).toFixed(2)}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>어둠</label>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={Number(params.darkness ?? 0.6)}
              onChange={(e) => onPatchParamsSlider({ darkness: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Math.round(Number(params.darkness ?? 0.6) * 100)}%
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>엣지</label>
            <input
              type="range"
              min={0}
              max={120}
              step={2}
              value={Number(params.feather ?? 40)}
              onChange={(e) => onPatchParamsSlider({ feather: parseInt(e.target.value, 10) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Number(params.feather ?? 40)}px
            </span>
          </div>
        </>
      )}

      {overlay.type === "alert" && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <label style={{ color: "#666", fontSize: 11 }}>표시 텍스트</label>
            <input
              type="text"
              value={String(params.text ?? "")}
              onChange={(e) => onPatchParams({ text: e.target.value })}
              style={{
                background: "#1a1a1a",
                color: "#ccc",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "4px 6px",
                fontSize: 12,
              }}
            />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <label style={{ color: "#666", fontSize: 11 }}>스타일</label>
            <select
              value={String(params.style ?? "warning")}
              onChange={(e) => onPatchParams({ style: e.target.value })}
              style={{
                background: "#1a1a1a",
                color: "#ccc",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "4px 6px",
                fontSize: 12,
              }}
            >
              <option value="warning">경고 (warning)</option>
              <option value="info">정보 (info)</option>
              <option value="error">오류 (error)</option>
            </select>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>너비</label>
            <input
              type="range"
              min={0.1}
              max={0.9}
              step={0.01}
              value={Number(params.width ?? 0.4)}
              onChange={(e) => onPatchParamsSlider({ width: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Number(params.width ?? 0.4).toFixed(2)}
            </span>
          </div>
        </>
      )}

      {overlay.type === "target" && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <label style={{ color: "#666", fontSize: 11 }}>마커 종류</label>
            <select
              value={String(params.marker_type ?? "crosshair")}
              onChange={(e) => onPatchParams({ marker_type: e.target.value })}
              style={{
                background: "#1a1a1a",
                color: "#ccc",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "4px 6px",
                fontSize: 12,
              }}
            >
              <option value="crosshair">십자선 (crosshair)</option>
              <option value="arrow">화살표 (arrow)</option>
            </select>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>크기</label>
            <input
              type="range"
              min={0.03}
              max={0.4}
              step={0.01}
              value={Number(params.size ?? 0.1)}
              onChange={(e) => onPatchParamsSlider({ size: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Number(params.size ?? 0.1).toFixed(2)}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>색상</label>
            <input
              type="color"
              value={String(params.color ?? "#ff3030")}
              onChange={(e) => onPatchParams({ color: e.target.value })}
              style={{ width: 40, height: 24, border: "none", background: "transparent" }}
            />
            <input
              type="text"
              value={String(params.color ?? "#ff3030")}
              onChange={(e) => onPatchParams({ color: e.target.value })}
              style={{
                flex: 1,
                background: "#1a1a1a",
                color: "#ccc",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "4px 6px",
                fontSize: 12,
              }}
            />
          </div>
          {String(params.marker_type ?? "crosshair") === "arrow" && (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>회전</label>
              <input
                type="range"
                min={0}
                max={360}
                step={5}
                value={Number(params.rotation ?? 0)}
                onChange={(e) => onPatchParamsSlider({ rotation: parseFloat(e.target.value) })}
                style={{ flex: 1, accentColor: "#c084fc" }}
              />
              <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
                {Number(params.rotation ?? 0)}°
              </span>
            </div>
          )}
        </>
      )}

      {overlay.type === "reaction" && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <label style={{ color: "#666", fontSize: 11 }}>아이콘</label>
            <select
              value={String(params.icon_type ?? "thunder")}
              onChange={(e) => onPatchParams({ icon_type: e.target.value })}
              style={{
                background: "#1a1a1a",
                color: "#ccc",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "4px 6px",
                fontSize: 12,
              }}
            >
              <option value="thunder">번개 (thunder)</option>
              <option value="sweat">땀방울 (sweat)</option>
              <option value="question">물음표 (question)</option>
              <option value="exclamation">느낌표 (exclamation)</option>
            </select>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>크기</label>
            <input
              type="range"
              min={0.05}
              max={0.5}
              step={0.01}
              value={Number(params.size ?? 0.15)}
              onChange={(e) => onPatchParamsSlider({ size: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Number(params.size ?? 0.15).toFixed(2)}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>색상 (옵션)</label>
            <input
              type="text"
              placeholder="#ffd200 (비우면 기본색)"
              value={String(params.color ?? "")}
              onChange={(e) => onPatchParams({ color: e.target.value })}
              style={{
                flex: 1,
                background: "#1a1a1a",
                color: "#ccc",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "4px 6px",
                fontSize: 12,
              }}
            />
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 6, color: "#aaa", fontSize: 11 }}>
            <input
              type="checkbox"
              checked={Boolean(params.bounce ?? false)}
              onChange={(e) => onPatchParams({ bounce: e.target.checked })}
            />
            바운스 애니메이션 (#217 동적 sin 모션)
          </label>
          {Boolean(params.bounce ?? false) && (
            <>
              {/* #222 후속 — bounce 파라미터 슬라이더 */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>주파수</label>
                <input
                  type="range"
                  min={0.1}
                  max={10}
                  step={0.1}
                  value={Number(params.bounce_freq ?? 2.0)}
                  onChange={(e) => onPatchParamsSlider({ bounce_freq: parseFloat(e.target.value) })}
                  style={{ flex: 1, accentColor: "#c084fc" }}
                />
                <span style={{ color: "#aaa", fontSize: 11, minWidth: 56, textAlign: "right" }}>
                  {Number(params.bounce_freq ?? 2.0).toFixed(1)} Hz
                </span>
              </div>
              <div style={{ color: "#666", fontSize: 10, marginLeft: 66 }}>권장 2~3Hz가 자연스러움</div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>진폭</label>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={Number(params.bounce_amplitude_ratio ?? 0.15)}
                  onChange={(e) => onPatchParamsSlider({ bounce_amplitude_ratio: parseFloat(e.target.value) })}
                  style={{ flex: 1, accentColor: "#c084fc" }}
                />
                <span style={{ color: "#aaa", fontSize: 11, minWidth: 56, textAlign: "right" }}>
                  {Math.round(Number(params.bounce_amplitude_ratio ?? 0.15) * 100)}%
                </span>
              </div>
              <div style={{ color: "#666", fontSize: 10, marginLeft: 66 }}>아이콘 크기 대비 흔들림 폭 (권장 10~20%)</div>
              {/* #224 — 모션 모드 선택 (sine = 규칙적 / perlin = 자연스러운 노이즈) */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>모션</label>
                <select
                  value={String(params.motion ?? "sine")}
                  onChange={(e) => onPatchParams({ motion: e.target.value })}
                  style={{
                    flex: 1,
                    background: "#1a1a1a",
                    color: "#ccc",
                    border: "1px solid #333",
                    borderRadius: 4,
                    padding: "4px 6px",
                    fontSize: 12,
                  }}
                >
                  <option value="sine">사인 (규칙적)</option>
                  <option value="perlin">펄린 노이즈 (자연스러운)</option>
                </select>
              </div>
              <div style={{ color: "#666", fontSize: 10, marginLeft: 66 }}>펄린은 2축 흔들림으로 더 자연스럽게 보임 (#224)</div>
            </>
          )}
        </>
      )}

      {overlay.type === "smell" && (
        <>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <label style={{ color: "#666", fontSize: 11 }}>색상</label>
            <select
              value={String(params.color ?? "green")}
              onChange={(e) => onPatchParams({ color: e.target.value })}
              style={{
                background: "#1a1a1a",
                color: "#ccc",
                border: "1px solid #333",
                borderRadius: 4,
                padding: "4px 6px",
                fontSize: 12,
              }}
            >
              <option value="green">초록 (악취/독)</option>
              <option value="brown">갈색 (음식 냄새)</option>
              <option value="white">흰색 (연기/김)</option>
              <option value="yellow">노랑 (자극)</option>
              <option value="gray">회색 (먼지)</option>
            </select>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>너비</label>
            <input
              type="range"
              min={0.05}
              max={0.8}
              step={0.01}
              value={Number(params.width ?? 0.25)}
              onChange={(e) => onPatchParamsSlider({ width: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Number(params.width ?? 0.25).toFixed(2)}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>높이</label>
            <input
              type="range"
              min={0.05}
              max={0.9}
              step={0.01}
              value={Number(params.height ?? 0.35)}
              onChange={(e) => onPatchParamsSlider({ height: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Number(params.height ?? 0.35).toFixed(2)}
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>강도</label>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={Number(params.intensity ?? 0.7)}
              onChange={(e) => onPatchParamsSlider({ intensity: parseFloat(e.target.value) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 36, textAlign: "right" }}>
              {Math.round(Number(params.intensity ?? 0.7) * 100)}%
            </span>
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 6, color: "#aaa", fontSize: 11 }}>
            <input
              type="checkbox"
              checked={Boolean(params.loop ?? false)}
              onChange={(e) => onPatchParams({ loop: e.target.checked })}
            />
            반복 모션 (#217 동적 loop)
          </label>
          {Boolean(params.loop ?? false) && (
            <>
              {/* #222 후속 — loop 파라미터 슬라이더 */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>주기</label>
                <input
                  type="range"
                  min={0.2}
                  max={20}
                  step={0.1}
                  value={Number(params.loop_cycle_sec ?? 2.0)}
                  onChange={(e) => onPatchParamsSlider({ loop_cycle_sec: parseFloat(e.target.value) })}
                  style={{ flex: 1, accentColor: "#c084fc" }}
                />
                <span style={{ color: "#aaa", fontSize: 11, minWidth: 56, textAlign: "right" }}>
                  {Number(params.loop_cycle_sec ?? 2.0).toFixed(1)} s
                </span>
              </div>
              <div style={{ color: "#666", fontSize: 10, marginLeft: 66 }}>한 사이클(피어오름→사라짐)이 끝나는 시간 (권장 1.5~3s)</div>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>최소 알파</label>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={Number(params.loop_alpha_min ?? 0.5)}
                  onChange={(e) => onPatchParamsSlider({ loop_alpha_min: parseFloat(e.target.value) })}
                  style={{ flex: 1, accentColor: "#c084fc" }}
                />
                <span style={{ color: "#aaa", fontSize: 11, minWidth: 56, textAlign: "right" }}>
                  {Math.round(Number(params.loop_alpha_min ?? 0.5) * 100)}%
                </span>
              </div>
              <div style={{ color: "#666", fontSize: 10, marginLeft: 66 }}>사이클 저점에서의 불투명도 (낮을수록 더 사라졌다 나옴)</div>
              {/* #224 — 모션 모드 선택 (linear = 기존 / perlin = 좌우 자연 흔들림) */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>모션</label>
                <select
                  value={String(params.motion ?? "linear")}
                  onChange={(e) => onPatchParams({ motion: e.target.value })}
                  style={{
                    flex: 1,
                    background: "#1a1a1a",
                    color: "#ccc",
                    border: "1px solid #333",
                    borderRadius: 4,
                    padding: "4px 6px",
                    fontSize: 12,
                  }}
                >
                  <option value="linear">선형 (수직 상승)</option>
                  <option value="perlin">펄린 노이즈 (좌우 흔들림)</option>
                </select>
              </div>
              <div style={{ color: "#666", fontSize: 10, marginLeft: 66 }}>펄린은 좌우로 자연스럽게 흔들리며 피어오름 (#224)</div>
            </>
          )}
        </>
      )}

      {/* #288 — emoji 아이콘 4종 (Apple Color Emoji 기반) */}
      {overlay.type === "emoji" && (
        <>
          <EmojiPicker
            value={String(params.icon_type ?? "question")}
            onChange={(key) => onPatchParams({ icon_type: key })}
          />
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label style={{ color: "#666", fontSize: 11, minWidth: 60 }}>크기(px)</label>
            <input
              type="range"
              min={32}
              max={400}
              step={1}
              value={Number(params.size ?? 150)}
              onChange={(e) => onPatchParamsSlider({ size: parseInt(e.target.value, 10) })}
              style={{ flex: 1, accentColor: "#c084fc" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, minWidth: 50, textAlign: "right" }}>
              {Number(params.size ?? 150)}px
            </span>
          </div>
          <div style={{ color: "#666", fontSize: 10, marginLeft: 66 }}>
            drawtext fontsize와 1:1 매핑 (1080p 기준 약 150×150px)
          </div>
        </>
      )}

      {/* 시간 */}
      <div style={{ display: "flex", gap: 8 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>시작(초)</label>
          <input
            type="number"
            step={0.1}
            value={overlay.start_sec}
            onChange={(e) => onPatchSlider({ start_sec: parseFloat(e.target.value) || 0 })}
            style={{
              background: "#1a1a1a",
              color: "#ccc",
              border: "1px solid #333",
              borderRadius: 4,
              padding: "4px 6px",
              fontSize: 12,
            }}
          />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: 1 }}>
          <label style={{ color: "#666", fontSize: 11 }}>종료(초, 빈칸=끝까지)</label>
          <input
            type="number"
            step={0.1}
            value={overlay.end_sec ?? ""}
            onChange={(e) =>
              onPatchSlider({
                end_sec: e.target.value === "" ? null : parseFloat(e.target.value),
              })
            }
            style={{
              background: "#1a1a1a",
              color: "#ccc",
              border: "1px solid #333",
              borderRadius: 4,
              padding: "4px 6px",
              fontSize: 12,
            }}
          />
        </div>
      </div>

      {/* #287 — 등장/사라짐 효과 (그래픽: none/fade 2종, sliderOnly 슬라이더) */}
      <EnterExitEffectFields
        kind="non-text"
        enterEffect={(overlay.enter_effect ?? "fade")}
        exitEffect={(overlay.exit_effect ?? "fade")}
        enterDuration={overlay.enter_duration_sec ?? 0.3}
        exitDuration={overlay.exit_duration_sec ?? 0.3}
        onEnterEffectChange={(v) => onPatch({ enter_effect: v as NonTextEnterExitEffect })}
        onExitEffectChange={(v) => onPatch({ exit_effect: v as NonTextEnterExitEffect })}
        onEnterDurationChange={(v) => onPatchSlider({ enter_duration_sec: v })}
        onExitDurationChange={(v) => onPatchSlider({ exit_duration_sec: v })}
        accent="#c084fc"
      />

      {/* #290 — SFX 섹션 (그래픽 오버레이) */}
      {projectId && audioAssets && (
        <SfxEditor
          projectId={projectId}
          audioAssets={audioAssets}
          sfxAssetId={overlay.sfx_asset_id ?? null}
          sfxVolume={overlay.sfx_volume ?? 0.7}
          onChange={(patch) => {
            // GraphicOverlayEditor의 onPatch는 즉시 반영(슬라이더 아님). 그대로 호출.
            onPatch(patch as Partial<GraphicOverlay>);
          }}
          accent="#c084fc"
        />
      )}

      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          onClick={onClose}
          style={{
            padding: "4px 12px",
            background: "#1a1620",
            color: "#c084fc",
            border: "1px solid #581c87",
            borderRadius: 5,
            cursor: "pointer",
            fontSize: 12,
          }}
        >
          닫기
        </button>
      </div>
    </div>
  );
}

// ── 씬 사이 인서트 핸들 (#245) ────────────────────────────────────────────
// 두 씬 카드 사이에 얇은 hover-only 가로 영역. 평소엔 거의 안 보이다가 hover 시 진해진다.
// 클릭 시 onClick으로 빈 씬 삽입.
// `onPickFromLibrary` 는 클립 라이브러리 모달을 여는 별도 경로. 기존 빈 씬 삽입
// 동작(onClick)은 그대로 두고 버튼만 하나 추가한다.
function SceneInsertHandle({ onClick, onPickFromLibrary, onInsertImage }: { onClick: () => void; onPickFromLibrary: () => void; onInsertImage: () => void }) {
  const [hover, setHover] = useState(false);
  return (
    <div
      style={{
        ...styles.sceneInsertHandle,
        ...(hover ? styles.sceneInsertHandleHover : {}),
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      title="여기에 새 씬 삽입"
    >
      <span style={hover ? styles.sceneInsertPlusHover : styles.sceneInsertPlus}>+ 씬 삽입</span>
      <span
        style={hover ? styles.sceneInsertLibHover : styles.sceneInsertLib}
        onClick={(e) => { e.stopPropagation(); onPickFromLibrary(); }}
        title="클립 라이브러리(밈영상·로딩 애니메이션)에서 골라 삽입"
      >
        🎞 라이브러리
      </span>
      <span
        style={hover ? styles.sceneInsertLibHover : styles.sceneInsertLib}
        onClick={(e) => { e.stopPropagation(); onInsertImage(); }}
        title="이미지를 골라 전체화면 중간 이미지 씬을 여기에 삽입"
      >
        🖼 중간 이미지
      </span>
    </div>
  );
}

/** #tts-emotion — 자주 쓰는 말투 프리셋.
 *  value 가 Chirp3-HD 의 SynthesisInput.prompt 로 그대로 전달된다.
 *  빈 문자열은 "지시 없음"(기본 톤). */
const TTS_EMOTION_PRESETS: { label: string; value: string }[] = [
  { label: "기본", value: "" },
  { label: "신남", value: "아주 신나고 들뜬 목소리로 말해줘" },
  { label: "차분", value: "차분하고 낮게 깔린 목소리로 말해줘" },
  { label: "진지", value: "진지하고 무게감 있게 말해줘" },
  { label: "다급", value: "다급하고 빠르게 몰아치듯 말해줘" },
  { label: "친근", value: "친근하고 편안하게 대화하듯 말해줘" },
];

function SceneItem({ projectId, scene, isSelected, isMultiSelected, isHighlighted, isPlayingTts, footageItems, sidebarAssets, audioAssets, imageAssets, copiedOverlays, copiedEffects, hasCopiedScene, copiedSceneId, tagDictVersion, onSelect, onOpenEffect, onNarrationChange, onSubtitleChange, onPlayTts, onRegenTts, onDeleteScene, onCopyScene, onPasteScene, onSplitScene, onAutoFitSpeed, onChangeFootageSource, onUpdateOverlays, onUpdateSpeed, onUpdateFootageAudioVolume, onUpdateTtsSpeed, onUpdateTtsEmotion, onUpdateSceneSfx, onCopyOverlays, onCopySingleOverlay, onPasteOverlays, onCopyEffects, onPasteEffects, onHighlightAsset, onUpdateClipOffset, onUpdateClipFit, onUpdateSceneDuration, onUpdateClipRotation, onUpdateShapeOverlays, onUpdateGraphicOverlays, onCopySingleGraphic, onPasteGraphic, copiedGraphic, onUpdateFilters, onUpdateMotions, onUpdateMosaicOverlays, onUpdateImageOverlays, onUpdateSpeedLines, onAddTag, onRemoveTag, onUpdateComments, speakers, defaultVoice, onChangeSpeakerTags, onManageSpeakers }: {
  // #290 — TextOverlayEditor에 SFX UI를 노출하기 위한 컨텍스트.
  projectId: string;
  scene: SceneCard;
  isSelected: boolean;
  isMultiSelected: boolean;
  /** #316 — 자연어 편집 패널의 미리보기 patch 가 가리키는 씬일 때 true. 카드 테두리 강조. */
  isHighlighted: boolean;
  isPlayingTts: boolean;
  footageItems: import("../types").FootageItem[];
  sidebarAssets: FootageAsset[];
  /** #290 — Project.studio_assets 중 asset_type="audio"만 필터링된 자산 목록. */
  audioAssets: FootageAsset[];
  /** 이미지 오버레이 자산 풀 — asset_type="image" 만 필터링. ImageOverlayEditor 의 자산 변경/썸네일에 사용. */
  imageAssets: FootageAsset[];
  copiedOverlays: TextOverlay[];
  /** #281 — 씬 간 모든 효과 종류 일괄 복사·붙여넣기 클립보드. null이면 미복사 상태. */
  copiedEffects: import("../store").CopiedEffects | null;
  /** 씬 전체 클립보드(#349 후속)에 복사된 씬이 있는지. true일 때만 "씬 붙여넣기" 버튼 노출. */
  hasCopiedScene: boolean;
  /** 클립보드에 복사된 씬의 원본 scene_id — 자기자신 씬에 붙여넣기 노출 방지용. */
  copiedSceneId: number | null;
  /** 사전 편집 모달 저장/삭제 시 bump되는 버전 — SceneTagsEditor에 전달하여 재로드 트리거 (#214). */
  tagDictVersion: number;
  onSelect: (e: React.MouseEvent) => void;
  onOpenEffect: () => void;
  onNarrationChange: (text: string) => void;
  /** 편집 지시 코멘트 변경 — 서버 반영 후 최신 목록을 zustand/IndexedDB 에 동기화. */
  onUpdateComments: (comments: import("../types").SceneComment[]) => void;
  /** 자막 전용 텍스트 변경 — 빈 문자열은 null로 저장(=narration_text 사용). */
  onSubtitleChange: (text: string | null) => void;
  onPlayTts: () => void;
  onRegenTts: () => void;
  onDeleteScene: () => void;
  /**
   * 씬 카드 내 `⎘` 버튼 (#349 후속) — 즉시 복제가 아니라 클립보드에 씬 전체 deep copy를 저장한다.
   * 새 씬 생성은 사용자가 별도 버튼(씬 추가 / 사이 삽입)으로 한 뒤 onPasteScene으로 적용.
   */
  onCopyScene: () => void;
  /** 씬 카드 내 `📋` 버튼 — 클립보드의 씬 내용을 현재 씬에 덮어쓰기. hasCopiedScene일 때만 노출. */
  onPasteScene: () => void;
  /** 씬 나누기 — 같은 footage를 공유하면서 N개 분할 지점으로 N+1개 씬으로 나눈다. */
  onSplitScene: () => void;
  /** 이 씬 하나만 영상 재생속도를 (TTS 길이 + 여유)에 맞춰 자동 조절. clip + TTS 있는 씬에서만 노출. */
  onAutoFitSpeed: () => void;
  /** #362 — 소스영상 변경 다이얼로그 열기. Editor 에서 footageDialogSceneId 를 set. */
  onChangeFootageSource: () => void;
  onUpdateOverlays: (overlays: TextOverlay[]) => void;
  /** #279 — opts.sliderOnly: true면 자동 미리보기 트리거 억제 (저장은 IndexedDB/서버까지 즉시 반영). */
  onUpdateSpeed: (
    speed: number,
    opts?: {
      sliderOnly?: boolean;
      /** 재생 속도 모드/가속 비율 동시 변경 (정속/가속 토글, 정속%·가속% 입력). */
      ramp?: Pick<Partial<SceneCard>, "footage_speed_mode" | "footage_speed_ramp_hold" | "footage_speed_ramp_accel" | "footage_speed_ramp_base">;
    },
  ) => void;
  onUpdateFootageAudioVolume: (volume: number, opts?: { sliderOnly?: boolean }) => void;
  /** 씬별 TTS 재생 속도 조절 — Chirp3-HD atempo 후처리용. 1.0=원본. */
  onUpdateTtsSpeed: (speed: number, opts?: { sliderOnly?: boolean }) => void;
  onUpdateTtsEmotion: (emotion: string) => void;
  /** 씬 본체 SFX 패치 — sfx_asset_id / sfx_volume / sfx_offset_sec, 및 "삐 소리" 효과
   *  (beep_on_narration_end / beep_volume) 중 변경된 것만 부분 업데이트. */
  onUpdateSceneSfx: (patch: { sfx_asset_id?: string | null; sfx_volume?: number; sfx_offset_sec?: number; beep_on_narration_end?: boolean; beep_volume?: number }, opts?: { sliderOnly?: boolean }) => void;
  onCopyOverlays: () => void;
  onCopySingleOverlay: (overlay: TextOverlay) => void;
  onPasteOverlays: () => void;
  /** #281 — 현재 씬의 모든 효과 종류를 클립보드로 복사. */
  onCopyEffects: () => void;
  /** #281 — 클립보드에 저장된 모든 효과 종류를 현재 씬에 append. id 재발급 + source_id 유지. */
  onPasteEffects: () => void;
  onHighlightAsset: (assetId: string) => void;
  /** #279 — opts.sliderOnly: 슬라이더 onChange 호출 시 자동 미리보기 억제. */
  onUpdateClipOffset: (xOffset: number, yOffset: number, opts?: { sliderOnly?: boolean }) => void;
  /** #258 footage 종횡비 fit 옵션 (contain/cover/stretch). */
  onUpdateClipFit: (fit: import("../types").FitMode) => void;
  /** 켄번스/삽입 이미지 씬 화면 표시 길이(초) — duration_hint_sec. sliderOnly: 드래그 중 로컬만. */
  onUpdateSceneDuration: (durationSec: number, opts?: { sliderOnly?: boolean }) => void;
  /** #263 원본 영상 수동 회전 (0/90/180/270). */
  onUpdateClipRotation: (rotation: import("../types").RotationDeg) => void;
  /** #279 — opts.sliderOnly: 슬라이더 onChange 호출 시 자동 미리보기 억제. */
  onUpdateShapeOverlays: (shapes: ShapeOverlay[], opts?: { sliderOnly?: boolean }) => void;
  onUpdateGraphicOverlays: (graphics: GraphicOverlay[], opts?: { sliderOnly?: boolean }) => void;
  onCopySingleGraphic: (g: GraphicOverlay) => void;
  onPasteGraphic: () => void;
  copiedGraphic: GraphicOverlay | null;
  onUpdateFilters: (filters: SceneFilter[], opts?: { sliderOnly?: boolean }) => void;
  onUpdateMotions: (motions: SceneMotion[], opts?: { sliderOnly?: boolean }) => void;
  /** #264 모자이크 오버레이 — 도형 오버레이와 동일한 좌표/크기 표현 + pixelate/blur 모드. */
  onUpdateMosaicOverlays: (mosaics: MosaicOverlay[], opts?: { sliderOnly?: boolean }) => void;
  /** 이미지 오버레이 — footage 스튜디오 이미지 자산 + 선택적 캡션 합성. */
  onUpdateImageOverlays: (images: ImageOverlay[], opts?: { sliderOnly?: boolean }) => void;
  /** 집중선(속도선) 오버레이 — 만화 박진감. */
  onUpdateSpeedLines: (lines: SpeedLinesOverlay[], opts?: { sliderOnly?: boolean }) => void;
  onAddTag: (tagName: string) => void;
  onRemoveTag: (tagName: string) => void;
  /** 다중 화자 — 프로젝트에 등록된 화자 목록과 기본 목소리. */
  speakers: Speaker[];
  defaultVoice: string;
  /** 화자 선택 시 갱신된 태그 배열 전체를 전달한다. */
  onChangeSpeakerTags: (tags: string[]) => void;
  onManageSpeakers: () => void;
}) {
  const { isOver, setNodeRef } = useDroppable({ id: scene.scene_id });
  const effectType = scene.footage?.type;
  const effectColor = effectType ? EFFECT_COLORS[effectType] ?? "#888" : "#444";
  const isAiEffect = effectType === "edit" || effectType === "sync";
  const [overlayEditorOpen, setOverlayEditorOpen] = useState(false);
  const [editingOverlay, setEditingOverlay] = useState<TextOverlay | null>(null);
  const [shapeEditorOpen, setShapeEditorOpen] = useState(false);
  const [editingShape, setEditingShape] = useState<ShapeOverlay | null>(null);
  // #205 그래픽 오버레이(spotlight/alert) 편집 상태
  const [editingGraphicId, setEditingGraphicId] = useState<string | null>(null);

  // #257 — handleAddOverlay는 EffectAddPanel(handleSideAddTextOverlay)로 이동.

  function handleEditOverlay(ov: TextOverlay) {
    setEditingOverlay(ov);
    setOverlayEditorOpen(true);
  }

  function handleDeleteOverlay(id: string) {
    console.log("[deleteOverlay]", id, scene.text_overlays);
    onUpdateOverlays((scene.text_overlays ?? []).filter((o) => o.id !== id));
  }

  function handleSaveOverlay(ov: TextOverlay) {
    const existing = scene.text_overlays ?? [];
    const idx = existing.findIndex((o) => o.id === ov.id);
    if (idx >= 0) {
      onUpdateOverlays(existing.map((o) => o.id === ov.id ? ov : o));
    } else {
      onUpdateOverlays([...existing, ov]);
    }
    setOverlayEditorOpen(false);
  }

  // #257 — handleAddShape는 EffectAddPanel(handleSideAddShape)로 이동.

  function handleEditShape(sh: ShapeOverlay) {
    setEditingShape(sh);
    setShapeEditorOpen(true);
  }

  function handleDeleteShape(id: string) {
    onUpdateShapeOverlays((scene.shape_overlays ?? []).filter((s) => s.id !== id));
  }

  function handleSaveShape(sh: ShapeOverlay) {
    const existing = scene.shape_overlays ?? [];
    const idx = existing.findIndex((s) => s.id === sh.id);
    if (idx >= 0) {
      onUpdateShapeOverlays(existing.map((s) => s.id === sh.id ? sh : s));
    } else {
      onUpdateShapeOverlays([...existing, sh]);
    }
    setShapeEditorOpen(false);
  }

  // ── 모자이크 오버레이(#264) 핸들러 ───────────────────────────────────────
  // 도형 오버레이와 동일한 좌표 규약. 1차에서는 rect만 정식 렌더링, ellipse는 백엔드에서 사각형으로 폴백.
  const [editingMosaicId, setEditingMosaicId] = useState<string | null>(null);

  function handleUpdateMosaic(id: string, patch: Partial<MosaicOverlay>, opts?: { sliderOnly?: boolean }) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.mosaic_overlays ?? [];
    onUpdateMosaicOverlays(
      existing.map((m) => (m.id === id ? { ...m, ...patch } : m)),
      opts
    );
  }

  function handleDeleteMosaic(id: string) {
    onUpdateMosaicOverlays((scene.mosaic_overlays ?? []).filter((m) => m.id !== id));
    if (editingMosaicId === id) setEditingMosaicId(null);
  }

  // ── 이미지 오버레이 핸들러 ────────────────────────────────────────────────
  // 도형/모자이크 오버레이와 동일한 칩 패턴 + 펼침 편집기 패턴.
  const [editingImageId, setEditingImageId] = useState<string | null>(null);

  // 집중선 슬라이더 이중 재생성 가드 — onPointerUp 과 onBlur 가 슬라이더 릴리즈 시 둘 다
  // sliderOnly:false 를 발화해 미리보기가 2번 생성되던 버그(문제4)를 막는다.
  // sliderOnly:true(값 변경) 가 오면 dirty=true, 첫 commit(sliderOnly:false) 만 재생성하고
  // dirty=false 로 리셋 → 뒤이은 중복 commit(같은 릴리즈의 다른 이벤트)은 재생성 없이 무시된다.
  const speedLinesDirtyRef = useRef(false);

  function handleUpdateImage(id: string, patch: Partial<ImageOverlay>, opts?: { sliderOnly?: boolean }) {
    // ⚠ props.scene 은 빠른 슬라이더 변경 시 stale 일 수 있다(이전 patch 반영 전 두 번째 onChange).
    // store 의 최신 scene 을 직접 조회해 patch 머지 — 중간 값 lost 회귀 차단.
    const latestProject = useStore.getState().project;
    const latestScene = latestProject?.scenes.find((s) => s.scene_id === scene.scene_id);
    const existing = latestScene?.image_overlays ?? scene.image_overlays ?? [];
    onUpdateImageOverlays(
      existing.map((m) => (m.id === id ? { ...m, ...patch } : m)),
      opts
    );
  }

  function handleDeleteImage(id: string) {
    onUpdateImageOverlays((scene.image_overlays ?? []).filter((m) => m.id !== id));
    if (editingImageId === id) setEditingImageId(null);
  }

  // ── 그래픽 오버레이(#205) 핸들러 ──────────────────────────────────────────
  // #257 — handleAddGraphic은 EffectAddPanel(handleSideAddGraphic)로 이동.

  function handleUpdateGraphic(id: string, patch: Partial<GraphicOverlay>, opts?: { sliderOnly?: boolean }) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.graphic_overlays ?? [];
    onUpdateGraphicOverlays(
      existing.map((g) => (g.id === id ? { ...g, ...patch } : g)),
      opts
    );
  }

  function handleUpdateGraphicParams(id: string, paramPatch: Record<string, unknown>, opts?: { sliderOnly?: boolean }) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.graphic_overlays ?? [];
    onUpdateGraphicOverlays(
      existing.map((g) =>
        g.id === id ? { ...g, params: { ...(g.params ?? {}), ...paramPatch } } : g
      ),
      opts
    );
  }

  function handleDeleteGraphic(id: string) {
    onUpdateGraphicOverlays((scene.graphic_overlays ?? []).filter((g) => g.id !== id));
    if (editingGraphicId === id) setEditingGraphicId(null);
  }

  // ── 씬 필터(#206/#219) 핸들러 ────────────────────────────────────────────
  // 구현 범위: flashback/horror/vivid/nightvision (#206) + POV (#219 1차, 정적 마스크).
  // #257 — handleAddFilter는 EffectAddPanel(handleSideAddFilter)로 이동.

  function handleUpdateFilterIntensity(filterType: SceneFilterType, intensity: number, opts?: { sliderOnly?: boolean }) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.filters ?? [];
    const clamped = Math.max(0, Math.min(1, intensity));
    onUpdateFilters(
      existing.map((f) => (f.filter_type === filterType ? { ...f, intensity: clamped } : f)),
      opts
    );
  }

  // POV 전용 params 업데이트 (center_x/center_y/radius/feather).
  function handleUpdateFilterParam(
    filterType: SceneFilterType,
    key: string,
    value: number,
    opts?: { sliderOnly?: boolean }
  ) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.filters ?? [];
    onUpdateFilters(
      existing.map((f) =>
        f.filter_type === filterType
          ? { ...f, params: { ...(f.params ?? {}), [key]: value } }
          : f
      ),
      opts
    );
  }

  function handleDeleteFilter(filterType: SceneFilterType) {
    onUpdateFilters((scene.filters ?? []).filter((f) => f.filter_type !== filterType));
  }

  // ── 집중선(속도선) 핸들러 ────────────────────────────────────────────────
  function handleUpdateSpeedLines(
    id: string,
    patch: Partial<SpeedLinesOverlay>,
    opts?: { sliderOnly?: boolean },
  ) {
    const isSliderOnly = !!opts?.sliderOnly;
    // ── 이중 재생성 가드(문제4) ─────────────────────────────────────────────
    // sliderOnly:true(값 변경) → dirty=true. 값이 바뀌었을 때만 이후 commit 이 재생성한다.
    // sliderOnly:false(commit) 인데 dirty=false 면 = 값 변화 없이 온 중복 commit
    // (같은 슬라이더 릴리즈에서 onPointerUp 이후 onBlur 가 또 발화한 경우) → 아무것도 안 함.
    if (isSliderOnly) {
      speedLinesDirtyRef.current = true;
    } else {
      // commit: patch 가 비었고(빈 {}) dirty 하지 않으면 중복 commit → 스킵.
      const hasPatchChange = patch && Object.keys(patch).length > 0;
      if (!hasPatchChange && !speedLinesDirtyRef.current) {
        return;
      }
      speedLinesDirtyRef.current = false;
    }
    // ⚠ props.scene 은 빠른 슬라이더/색 변경 시 stale 일 수 있다(이전 patch 반영 전 두 번째 onChange).
    // store 의 최신 scene 을 직접 조회해 patch 머지 — 중간 값 lost 회귀 차단(이미지 오버레이와 동일 패턴).
    const latestProject = useStore.getState().project;
    const latestScene = latestProject?.scenes.find((s) => s.scene_id === scene.scene_id);
    const existing = latestScene?.speed_lines ?? scene.speed_lines ?? [];
    onUpdateSpeedLines(
      existing.map((s) => (s.id === id ? { ...s, ...patch } : s)),
      opts,
    );
  }
  function handleDeleteSpeedLines(id: string) {
    onUpdateSpeedLines((scene.speed_lines ?? []).filter((s) => s.id !== id));
  }

  // ── 씬 모션(#207 / #234) 핸들러 ───────────────────────────────────────────
  // #257 — handleAddMotion은 EffectAddPanel(handleSideAddMotion)로 이동.

  function handleUpdateMotion(index: number, patch: Partial<SceneMotion>, opts?: { sliderOnly?: boolean }) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.motions ?? [];
    if (index < 0 || index >= existing.length) return;
    onUpdateMotions(
      existing.map((m, i) => (i === index ? { ...m, ...patch } : m)),
      opts
    );
  }

  function handleUpdateMotionParam(index: number, key: string, value: number, opts?: { sliderOnly?: boolean }) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.motions ?? [];
    if (index < 0 || index >= existing.length) return;
    onUpdateMotions(
      existing.map((m, i) =>
        i === index
          ? { ...m, params: { ...(m.params ?? {}), [key]: value } }
          : m
      ),
      opts
    );
  }

  function handleDeleteMotion(index: number) {
    const existing = scene.motions ?? [];
    if (index < 0 || index >= existing.length) return;
    onUpdateMotions(existing.filter((_, i) => i !== index));
  }

  // ── POV 키프레임(#231) 핸들러 ────────────────────────────────────────────
  // params.keyframes 배열에 {t, center_x, center_y, radius} 형식으로 저장한다.
  // 백엔드(_interpolate_pov_at_time)는 keyframes.length >= 2일 때 동적 보간으로 동작.
  function handleAddPovKeyframe() {
    const existing = scene.filters ?? [];
    const pov = existing.find((f) => f.filter_type === "pov");
    if (!pov) return;
    const p = (pov.params ?? {}) as Record<string, unknown>;
    const prevList = Array.isArray(p.keyframes) ? (p.keyframes as Array<Record<string, number>>) : [];
    // 새 키프레임 t: 마지막 t + 1.0 (없으면 0.0), 10초 상한
    const lastT = prevList.length > 0
      ? Math.max(...prevList.map((kf) => Number(kf.t ?? 0)))
      : -1.0;
    const newT = Math.min(10, lastT + 1.0);
    // 새 키프레임 기본값: 현재 정적 center_x/y/radius (없으면 0.5/0.5/0.35)
    const defaultCx = typeof p.center_x === "number" ? p.center_x : 0.5;
    const defaultCy = typeof p.center_y === "number" ? p.center_y : 0.5;
    const defaultR = typeof p.radius === "number" ? p.radius : 0.35;
    // 직전 키프레임을 기반으로 새 키 생성 (없으면 정적값)
    const seedKf = prevList.length > 0
      ? prevList.reduce((acc, kf) => (Number(kf.t ?? 0) > Number(acc.t ?? 0) ? kf : acc), prevList[0])
      : { center_x: defaultCx, center_y: defaultCy, radius: defaultR };
    const newKf: Record<string, number> = {
      t: newT,
      center_x: Number(seedKf.center_x ?? defaultCx),
      center_y: Number(seedKf.center_y ?? defaultCy),
      radius: Number(seedKf.radius ?? defaultR),
    };
    const nextList = [...prevList, newKf];
    onUpdateFilters(
      existing.map((f) =>
        f.filter_type === "pov"
          ? { ...f, params: { ...(f.params ?? {}), keyframes: nextList } }
          : f
      )
    );
  }

  function handleUpdatePovKeyframe(index: number, key: "t" | "center_x" | "center_y" | "radius", value: number, opts?: { sliderOnly?: boolean }) {
    // #279 — opts.sliderOnly: 슬라이더 onChange에서 호출 시 자동 미리보기 트리거 억제.
    const existing = scene.filters ?? [];
    const pov = existing.find((f) => f.filter_type === "pov");
    if (!pov) return;
    const p = (pov.params ?? {}) as Record<string, unknown>;
    const prevList = Array.isArray(p.keyframes) ? (p.keyframes as Array<Record<string, number>>) : [];
    if (index < 0 || index >= prevList.length) return;
    const nextList = prevList.map((kf, i) =>
      i === index ? { ...kf, [key]: value } : kf
    );
    onUpdateFilters(
      existing.map((f) =>
        f.filter_type === "pov"
          ? { ...f, params: { ...(f.params ?? {}), keyframes: nextList } }
          : f
      ),
      opts
    );
  }

  function handleDeletePovKeyframe(index: number) {
    const existing = scene.filters ?? [];
    const pov = existing.find((f) => f.filter_type === "pov");
    if (!pov) return;
    const p = (pov.params ?? {}) as Record<string, unknown>;
    const prevList = Array.isArray(p.keyframes) ? (p.keyframes as Array<Record<string, number>>) : [];
    if (index < 0 || index >= prevList.length) return;
    const nextList = prevList.filter((_, i) => i !== index);
    onUpdateFilters(
      existing.map((f) =>
        f.filter_type === "pov"
          ? { ...f, params: { ...(f.params ?? {}), keyframes: nextList } }
          : f
      )
    );
  }

  const matchedFile = scene.footage
    ? ("file" in scene.footage ? scene.footage.file : "image" in scene.footage ? (scene.footage as unknown as Record<string, string>).image : "")
    : "";
  const matchedLabel = (() => {
    if (!matchedFile) return "";
    const asset = sidebarAssets.find((a) => a.filename === matchedFile);
    return asset?.label || matchedFile.replace(/\.[^.]+$/, "");
  })();

  return (
    <div
      ref={setNodeRef}
      data-scene-id={scene.scene_id}
      style={{
        ...styles.sceneCard,
        ...(isSelected ? styles.sceneCardSelected : {}),
        ...(isMultiSelected ? styles.sceneCardMultiSelected : {}),
        ...(isHighlighted ? styles.sceneCardNlHighlight : {}),
        ...(isOver ? styles.sceneCardOver : {}),
      }}
      onClick={onSelect}
    >
      <div style={styles.sceneTop}>
        <span style={styles.sceneNum}>{scene.scene_id}</span>

        <div
          style={{ ...styles.effectBadge, background: effectColor + "22", borderColor: effectColor, color: effectColor }}
          onClick={(e) => { e.stopPropagation(); onOpenEffect(); }}
          title="이펙트 변경"
        >
          {effectType ?? "미배정"} {isAiEffect && "⚡"}
        </div>

        {matchedFile && (
          <span
            style={{ ...styles.fileChip, cursor: "pointer" }}
            onClick={(e) => {
              e.stopPropagation();
              const asset = sidebarAssets.find((a) => a.filename === matchedFile);
              if (asset) onHighlightAsset(asset.asset_id);
            }}
          >
            {matchedLabel !== matchedFile.replace(/\.[^.]+$/, "") ? (
              <>{matchedLabel} <span style={{ opacity: 0.5, fontSize: "0.85em" }}>{matchedFile}</span></>
            ) : matchedLabel}
          </span>
        )}
        {/* #362 — 소스영상 변경 다이얼로그 트리거. matchedFile 유무와 무관하게 항상 노출.
            footage가 없으면 "소스 지정", 있으면 "소스 변경"으로 라벨 차등. */}
        <button
          type="button"
          style={styles.changeFootageBtn}
          onClick={(e) => { e.stopPropagation(); onChangeFootageSource(); }}
          title={matchedFile
            ? "이 씬의 소스 영상을 다른 footage로 교체합니다"
            : "이 씬에 소스 영상을 지정합니다"}
        >
          🔁 {matchedFile ? "소스 변경" : "소스 지정"}
        </button>

        <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
          {(() => {
            const isGenerating = scene.tts?.status === "generating";
            const isReady = scene.tts?.status === "ready";
            const isUnsynced = isReady && scene.tts && !scene.tts.synced;
            return (
              <>
                <button
                  style={{
                    ...styles.ttsBtn,
                    ...(isGenerating ? styles.ttsBtnLoading : {}),
                    ...(isUnsynced ? styles.ttsBtnUnsynced : {}),
                  }}
                  onClick={(e) => { e.stopPropagation(); onRegenTts(); }}
                  title={isGenerating ? "TTS 생성 중..." : isUnsynced ? "TTS 재생성 (대본 변경됨)" : "TTS 생성"}
                  disabled={isGenerating}
                >
                  {isGenerating ? "⟳" : isUnsynced ? "↻" : "♪"}
                </button>
                {isReady && (
                  <button
                    style={{ ...styles.ttsBtn, ...(isPlayingTts ? styles.ttsBtnActive : {}) }}
                    onClick={(e) => { e.stopPropagation(); onPlayTts(); }}
                    title="TTS 재생"
                  >
                    {isPlayingTts ? "■" : "▶"}
                  </button>
                )}
                <button
                  style={styles.duplicateSceneBtn}
                  onClick={(e) => { e.stopPropagation(); onCopyScene(); }}
                  title="씬 복사 (효과/오버레이/footage 포함) — 새 씬 추가 후 '📋'로 붙여넣기"
                >
                  ⎘
                </button>
                {hasCopiedScene && copiedSceneId !== scene.scene_id && (
                  <button
                    style={styles.pasteSceneBtn}
                    onClick={(e) => { e.stopPropagation(); onPasteScene(); }}
                    title="씬 붙여넣기 — 클립보드의 씬 전체를 이 씬에 덮어쓰기 (TTS는 재생성 필요)"
                  >
                    📋
                  </button>
                )}
                {scene.footage?.type === "clip" && (
                  <button
                    style={styles.splitSceneBtn}
                    onClick={(e) => { e.stopPropagation(); onSplitScene(); }}
                    title="씬 나누기 — footage는 공유, 구간만 나눠 새 씬 생성"
                  >
                    ✂
                  </button>
                )}
                {scene.footage?.type === "clip" && getSceneTtsDuration(scene) > 0 && (
                  <button
                    style={styles.autoFitSpeedBtn}
                    onClick={(e) => { e.stopPropagation(); onAutoFitSpeed(); }}
                    title="이 씬만 영상속도 자동맞춤 — 영상이 나레이션보다 짧으면 느리게 늘려 (TTS 길이 + 1.5초)를 채운다. 이미 길면 그대로 둔다."
                  >
                    ⏱
                  </button>
                )}
                <button
                  style={styles.deleteSceneBtn}
                  onClick={(e) => { e.stopPropagation(); onDeleteScene(); }}
                  title="씬 삭제"
                >
                  ×
                </button>
              </>
            );
          })()}
        </div>
      </div>

      {/* 씬 태그 입력 — 이슈 #211. 나레이션 입력과 물리적으로 분리.
          #214: 부모의 tagDictVersion을 전달하여 사전 편집 모달 저장/삭제 시 재로드. */}
      <SceneTagsEditor
        scene={scene}
        onAddTag={onAddTag}
        onRemoveTag={onRemoveTag}
        dictVersion={tagDictVersion}
      />

      {/* 다중 화자 — 씬당 1명. 고르면 speaker:{name} 태그가 붙고 TTS 목소리가 바뀐다. */}
      <SceneSpeakerPicker
        scene={scene}
        speakers={speakers}
        defaultVoice={defaultVoice}
        onChangeTags={onChangeSpeakerTags}
        onManage={onManageSpeakers}
      />

      {/* 편집 지시 코멘트 — 김감독이 남긴 지시를 저장하고, 클로드 에이전트가
          처리 후 완료 이력으로 남긴다. 렌더에 영향 없음(미리보기 캐시 무관). */}
      <SceneCommentEditor
        projectId={projectId}
        sceneId={scene.scene_id}
        comments={scene.comments ?? []}
        onCommentsChange={onUpdateComments}
      />

      <textarea
        style={styles.narrationInput}
        value={scene.narration_text}
        onChange={(e) => onNarrationChange(e.target.value)}
        onClick={(e) => e.stopPropagation()}
        rows={2}
        placeholder="나레이션 텍스트..."
      />

      <SubtitleEditor
        narrationText={scene.narration_text}
        subtitleText={scene.subtitle_text ?? null}
        onChange={onSubtitleChange}
      />

      <DurationStatus scene={scene} footageItems={footageItems} />

      {/* footage 재생 속도 슬라이더 — clip/sync/kenburns 효과 + footage 있는 씬에 표시.
          상한은 고정값 (사용자 결정 2026-06, 2026-10 에 4.0x → 16.0x 로 상향): clipDur/ttsDur 동적 상한은 옛 match 데이터로
          잘못 계산되어 1.0x 미만으로 막히는 경우가 있었다. TTS-영상 길이 매칭은 별도 정책이고,
          슬라이더는 단순 속도 조절 도구로만 동작한다. */}
      {((scene.footage?.type === "clip" && scene.footage.file)
        || (scene.footage?.type === "sync" && scene.match)
        || ((scene.footage?.type === "kenburns" || scene.footage?.type === "kenburns_copy_img") && (scene.footage as any).image)
      ) && (() => {
        const maxSpeed = FOOTAGE_SPEED_MAX;
        const minSpeed = FOOTAGE_SPEED_MIN;
        const currentSpeed = scene.footage_speed ?? 1.0;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
            <span style={{ color: "#666", fontSize: 11, flexShrink: 0 }}>재생 속도</span>
            <input
              type="range"
              min={minSpeed}
              max={maxSpeed}
              step={0.05}
              value={currentSpeed}
              onChange={(e) => onUpdateSpeed(parseFloat(e.target.value), { sliderOnly: true })}
              style={{ flex: 1, minWidth: 80, accentColor: "#6366f1" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 36 }}>
              {currentSpeed.toFixed(2)}x
            </span>
            <span style={{ color: "#555", fontSize: 10, flexShrink: 0 }}>
              (최대 {maxSpeed.toFixed(0)}x)
            </span>
            {/* 넓어진 범위(0.25~16x)에서 슬라이더로 정확히 맞추기 어려워 자주 쓰는 배속을 버튼으로 둔다.
                버튼은 일반 변경(즉시 히스토리 기록 + 미리보기 갱신)으로 처리한다. */}
            <span style={{ display: "flex", gap: 3, flexShrink: 0 }}>
              {[1, 2, 4, 8, 16].map((r) => (
                <button
                  key={r}
                  onClick={() => onUpdateSpeed(r)}
                  style={{
                    background: Math.abs(currentSpeed - r) < 1e-6 ? "#4f46e5" : "#1e1e1e",
                    color: Math.abs(currentSpeed - r) < 1e-6 ? "#fff" : "#aaa",
                    border: "1px solid #333", borderRadius: 4,
                    padding: "1px 6px", fontSize: 10, cursor: "pointer",
                  }}
                >
                  {r}×
                </button>
              ))}
            </span>
            {/* 재생 속도 모드 — 정속(전 구간 같은 배속) / 가속(처음 정속 → 점점 빨라져 지정 배속 도달 후 유지). */}
            {(() => {
              const spec = getSceneSpeedSpec(scene);
              const isRamp = spec.mode === "ramp" || spec.mode === "ramp_down";
              const isDown = spec.mode === "ramp_down";
              const setMode = (mode: FootageSpeedMode) => {
                if (mode === spec.mode) return;
                onUpdateSpeed(currentSpeed, {
                  ramp: {
                    footage_speed_mode: mode,
                    footage_speed_ramp_hold: spec.hold,
                    footage_speed_ramp_accel: spec.accel,
                    footage_speed_ramp_base: spec.base ?? 1,
                  },
                });
              };
              const modeBtn = (mode: FootageSpeedMode, label: string, title: string) => (
                <button
                  key={mode}
                  onClick={() => setMode(mode)}
                  title={title}
                  style={{
                    background: spec.mode === mode ? "#4f46e5" : "#1e1e1e",
                    color: spec.mode === mode ? "#fff" : "#aaa",
                    border: "1px solid #333", borderRadius: 4,
                    padding: "1px 6px", fontSize: 10, cursor: "pointer",
                  }}
                >
                  {label}
                </button>
              );
              return (
                <span style={{ display: "flex", alignItems: "center", gap: 3, flexShrink: 0 }}>
                  {modeBtn("constant", "정속", "처음부터 끝까지 같은 배속으로 재생합니다")}
                  {modeBtn("ramp", "가속", "처음 정속 → 점점 빨라져 지정 배속 도달 후 유지")}
                  {modeBtn("ramp_down", "감속", "처음 지정 배속으로 빠르게 → 점점 느려져 원래 속도(1배)로 끝까지")}
                  {isRamp && (
                    <>
                      <RampBaseInput
                        title={isDown
                          ? "감속이 끝난 뒤 유지할 기본 배속 (기본 1배). 위 배속 버튼은 처음 빠른 구간의 배속입니다"
                          : "처음에 재생할 기본 배속 (기본 1배). 위 배속 버튼은 가속이 끝난 뒤 도달할 배속입니다"}
                        value={spec.base ?? 1}
                        onCommit={(base) => onUpdateSpeed(currentSpeed, { ramp: { footage_speed_ramp_base: base } })}
                      />
                      <RampPercentInput
                        label={isDown ? "빠름" : "정속"}
                        title={isDown
                          ? "처음에 지정 배속으로 빠르게 재생할 구간 — 영상 길이 대비 %"
                          : "처음에 원래 속도(1배)로 재생할 구간 — 영상 길이 대비 %"}
                        value={Math.round(spec.hold * 100)}
                        onCommit={(pct) => {
                          const hold = Math.min(100, Math.max(0, pct)) / 100;
                          onUpdateSpeed(currentSpeed, {
                            ramp: {
                              footage_speed_ramp_hold: hold,
                              footage_speed_ramp_accel: Math.min(spec.accel, 1 - hold),
                            },
                          });
                        }}
                      />
                      <RampPercentInput
                        label={isDown ? "감속" : "가속"}
                        title={isDown
                          ? "점점 느려지는 구간 — 영상 길이 대비 %. 이 구간이 끝나면 원래 속도(1배)로 끝까지"
                          : "점점 빨라지는 구간 — 영상 길이 대비 %. 이 구간이 끝나면 지정 배속으로 끝까지 유지"}
                        value={Math.round(spec.accel * 100)}
                        onCommit={(pct) => {
                          const accel = Math.min(100 - Math.round(spec.hold * 100), Math.max(0, pct)) / 100;
                          onUpdateSpeed(currentSpeed, { ramp: { footage_speed_ramp_accel: accel } });
                        }}
                      />
                    </>
                  )}
                </span>
              );
            })()}
          </div>
        );
      })()}

      {/* TTS 재생 속도 슬라이더 — 나레이션 텍스트가 있는 모든 씬에 표시.
          Chirp3-HD 는 API speakingRate 를 무시하므로 렌더 시점에 각 segment WAV 에
          atempo 필터로 후처리해 속도를 변경한다 (build_merged_wav).
          1.0 = 원본 속도(기본), 2.0 = 2배 빠르게. 슬라이더 값이 1.0 아닐 때 배지 표시. */}
      {(() => {
        const currentTtsSpeed = scene.tts_speed ?? 1.0;
        const hasTtsContent = Boolean(
          (scene.narration_text && scene.narration_text.trim()) ||
          (scene.narration_segments || []).some((s) => s.text && s.text.trim())
        );
        if (!hasTtsContent) return null;
        const isModified = Math.abs(currentTtsSpeed - 1.0) > 1e-3;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
            <span style={{ color: "#666", fontSize: 11, flexShrink: 0 }}>TTS 속도</span>
            <input
              type="range"
              min={0.5}
              max={2.0}
              step={0.05}
              value={currentTtsSpeed}
              onChange={(e) => onUpdateTtsSpeed(parseFloat(e.target.value), { sliderOnly: true })}
              style={{ flex: 1, minWidth: 80, accentColor: "#f59e0b" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 36 }}>
              {currentTtsSpeed.toFixed(2)}x
            </span>
            {isModified && (
              <span
                title="TTS 속도가 원본과 다릅니다. 렌더/미리보기 시 atempo 로 후처리됩니다."
                style={{
                  fontSize: 10,
                  padding: "1px 6px",
                  borderRadius: 8,
                  background: "#f59e0b",
                  color: "#1a1a1a",
                  fontWeight: 600,
                  flexShrink: 0,
                }}
              >
                수정됨
              </span>
            )}
            {!isModified && (
              <span style={{ color: "#555", fontSize: 10, flexShrink: 0 }}>
                (0.5~2.0x)
              </span>
            )}
          </div>
        );
      })()}

      {/* #tts-emotion — 씬별 TTS 말투/감정 지시문.
          Chirp3-HD 의 SynthesisInput.prompt 로 전달된다. 프리셋 버튼으로 빠르게 넣고,
          입력칸에서 자유롭게 고칠 수 있다. 값을 바꾸면 TTS 를 다시 생성해야 반영된다. */}
      {(() => {
        const hasTtsContent = Boolean(
          (scene.narration_text && scene.narration_text.trim()) ||
          (scene.narration_segments || []).some((s) => s.text && s.text.trim())
        );
        if (!hasTtsContent) return null;
        const cur = scene.tts_emotion ?? "";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
            <span style={{ color: "#666", fontSize: 11, flexShrink: 0 }}>말투</span>
            {TTS_EMOTION_PRESETS.map((preset) => {
              const active = cur === preset.value;
              return (
                <button
                  key={preset.label}
                  type="button"
                  title={preset.value || "지시 없이 기본 톤으로 합성"}
                  onClick={() => onUpdateTtsEmotion(active ? "" : preset.value)}
                  style={{
                    fontSize: 10,
                    padding: "2px 7px",
                    borderRadius: 8,
                    cursor: "pointer",
                    border: `1px solid ${active ? "#a78bfa" : "#3a3a3a"}`,
                    background: active ? "#a78bfa" : "#2a2a2a",
                    color: active ? "#1a1a1a" : "#bbb",
                    fontWeight: active ? 600 : 400,
                  }}
                >
                  {preset.label}
                </button>
              );
            })}
            <input
              type="text"
              value={cur}
              placeholder="직접 입력 (예: 속삭이듯 낮게)"
              onChange={(e) => onUpdateTtsEmotion(e.target.value)}
              style={{
                flex: 1, minWidth: 120, fontSize: 11, padding: "3px 6px",
                background: "#1e1e1e", color: "#ddd",
                border: "1px solid #3a3a3a", borderRadius: 4,
              }}
            />
            {cur && (
              <span
                title={
                  "말투는 Gemini TTS / Typecast 보이스에서만 반영됩니다.\n" +
                  "현재 기본 보이스(Chirp3-HD)는 이 지시를 무시하고 평소 톤으로 합성합니다.\n" +
                  "반영하려면 지원 보이스로 바꾼 뒤 ↻ 로 재생성하세요."
                }
                style={{ color: "#a78bfa", fontSize: 10, flexShrink: 0 }}
              >
                ↻ 재생성 · Chirp3-HD 미지원
              </span>
            )}
          </div>
        );
      })()}

      {/* footage 원본 오디오 음량 슬라이더 — 비디오 footage(clip/sync)에만 표시. 이미지엔 오디오 없음. */}
      {(scene.footage?.type === "clip" || scene.footage?.type === "sync") && (() => {
        const fav = scene.footage_audio_volume ?? 0.3;
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
            <span style={{ color: "#666", fontSize: 11, flexShrink: 0 }}>원본 음량</span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={fav}
              onChange={(e) => onUpdateFootageAudioVolume(parseFloat(e.target.value), { sliderOnly: true })}
              style={{ flex: 1, minWidth: 80, accentColor: "#6366f1" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 36 }}>
              {(fav * 100).toFixed(0)}%
            </span>
            <span style={{ color: "#555", fontSize: 10, flexShrink: 0 }}>
              (기본 30%, TTS 더킹)
            </span>
          </div>
        );
      })()}

      {/* 씬 본체 SFX — 시작 시점은 씬 시작 + 오프셋(초). 오버레이 SFX와 동일한 풀(studio audio + 프리셋)에서 선택. */}
      <div onClick={(e) => e.stopPropagation()}>
        <SfxEditor
          projectId={projectId}
          audioAssets={audioAssets}
          sfxAssetId={scene.sfx_asset_id ?? null}
          sfxVolume={scene.sfx_volume ?? 0.7}
          onChange={(patch) => {
            const p: { sfx_asset_id?: string | null; sfx_volume?: number } = {};
            if ("sfx_asset_id" in patch) p.sfx_asset_id = patch.sfx_asset_id ?? null;
            if ("sfx_volume" in patch && patch.sfx_volume !== undefined) p.sfx_volume = patch.sfx_volume;
            if (Object.keys(p).length > 0) onUpdateSceneSfx(p, { sliderOnly: true });
          }}
          accent="#a78bfa"
        />
        {scene.sfx_asset_id && (
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
            <span style={{ color: "#666", fontSize: 11, flexShrink: 0 }}>시작 오프셋</span>
            <input
              type="range"
              min={0}
              max={Math.max(1, scene.duration_hint_sec ?? 5)}
              step={0.1}
              value={scene.sfx_offset_sec ?? 0}
              onChange={(e) => onUpdateSceneSfx({ sfx_offset_sec: parseFloat(e.target.value) }, { sliderOnly: true })}
              style={{ flex: 1, minWidth: 80, accentColor: "#a78bfa" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 36 }}>
              {(scene.sfx_offset_sec ?? 0).toFixed(1)}s
            </span>
            <span style={{ color: "#555", fontSize: 10, flexShrink: 0 }}>
              (씬 시작부터)
            </span>
          </div>
        )}
      </div>

      {/* "삐 소리" 씬 단위 자동 효과 — 마지막 TTS 나레이션이 끝나는 시점에 비프음 이어붙이기. */}
      <div onClick={(e) => e.stopPropagation()} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }} title="지정하면 이 씬의 마지막 TTS 나레이션이 끝나는 시점에 '삐' 비프음을 이어붙입니다. 비프 이후에는 이 씬의 TTS 가 더 들리지 않습니다.">
          <input
            type="checkbox"
            checked={!!scene.beep_on_narration_end}
            onChange={(e) => onUpdateSceneSfx({ beep_on_narration_end: e.target.checked })}
            style={{ accentColor: "#f472b6" }}
          />
          <span style={{ color: "#ddd", fontSize: 12 }}>🔔 삐 소리 (나레이션 끝에)</span>
        </label>
        {scene.beep_on_narration_end && (
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", paddingLeft: 22 }}>
            <span style={{ color: "#666", fontSize: 11, flexShrink: 0 }}>비프 음량</span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={scene.beep_volume ?? 0.7}
              onChange={(e) => onUpdateSceneSfx({ beep_volume: parseFloat(e.target.value) }, { sliderOnly: true })}
              style={{ flex: 1, minWidth: 80, accentColor: "#f472b6" }}
            />
            <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 36 }}>
              {((scene.beep_volume ?? 0.7) * 100).toFixed(0)}%
            </span>
          </div>
        )}
      </div>

      {/* 켄번스/삽입 이미지 씬 길이 편집 — TTS 가 없으므로 duration_hint_sec 이 곧 씬 화면 길이. */}
      {(scene.footage?.type === "kenburns" || scene.footage?.type === "kenburns_copy_img") && (
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }} onClick={(e) => e.stopPropagation()}>
          <span style={{ color: "#666", fontSize: 11, flexShrink: 0, minWidth: 50 }} title="이 이미지 씬이 화면에 표시되는 길이(초)">씬 길이</span>
          <input
            type="range"
            min={0.5}
            max={15}
            step={0.5}
            value={scene.duration_hint_sec ?? 5}
            onChange={(e) => onUpdateSceneDuration(parseFloat(e.target.value), { sliderOnly: true })}
            onPointerUp={(e) => onUpdateSceneDuration(parseFloat((e.target as HTMLInputElement).value), { sliderOnly: false })}
            onBlur={(e) => onUpdateSceneDuration(parseFloat(e.target.value), { sliderOnly: false })}
            style={{ flex: 1, minWidth: 80, accentColor: "#8b5cf6" }}
          />
          <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 36 }}>
            {(scene.duration_hint_sec ?? 5).toFixed(1)}s
          </span>
        </div>
      )}

      {/* kenburns 타입 fit 컨트롤 — 이미지가 출력 비율과 다를 때 표시 방식 선택. */}
      {(scene.footage?.type === "kenburns" || scene.footage?.type === "kenburns_copy_img") && (scene.footage as any).image && (() => {
        const footage = scene.footage as { fit?: "cover" | "contain" | "stretch" };
        const fit = footage.fit ?? "cover";
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 6 }} onClick={(e) => e.stopPropagation()}>
            <span style={{ color: "#666", fontSize: 11, flexShrink: 0, minWidth: 50 }}>Fit</span>
            <select
              value={fit}
              onChange={(e) => onUpdateClipFit(e.target.value as import("../types").FitMode)}
              style={{
                flex: 1,
                minWidth: 80,
                fontSize: 11,
                background: "#222",
                color: "#ddd",
                border: "1px solid #444",
                borderRadius: 3,
                padding: "2px 4px",
              }}
              title="이미지가 출력 비율과 다를 때 처리 방식"
            >
              <option value="cover">cover (가득 채움)</option>
              <option value="contain">contain (여백)</option>
              <option value="stretch">stretch (비율 무시)</option>
            </select>
          </div>
        );
      })()}

      {/* clip 타입 fit / 회전 / x / y offset 컨트롤 (#258, #263) */}
      {scene.footage?.type === "clip" && scene.footage.file && (() => {
        const footage = scene.footage as import("../types").ClipEffect;
        const xOff = footage.x_offset ?? 0;
        const yOff = footage.y_offset ?? 0;
        const fit: import("../types").FitMode = footage.fit ?? "contain";
        const rotation: import("../types").RotationDeg = (footage.rotation ?? 0) as import("../types").RotationDeg;
        // stretch는 화면 가득 늘려서 오프셋이 의미 없음 → 슬라이더 비활성화.
        const offsetDisabled = fit === "stretch";
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ color: "#666", fontSize: 11, flexShrink: 0, minWidth: 50 }}>Fit</span>
              <select
                value={fit}
                onChange={(e) => onUpdateClipFit(e.target.value as import("../types").FitMode)}
                style={{
                  flex: 1,
                  minWidth: 80,
                  fontSize: 11,
                  background: "#222",
                  color: "#ddd",
                  border: "1px solid #444",
                  borderRadius: 3,
                  padding: "2px 4px",
                }}
                title="footage 종횡비가 출력과 다를 때 처리 방식"
              >
                <option value="contain">contain (여백)</option>
                <option value="cover">cover (가득 채움)</option>
                <option value="stretch">stretch (비율 무시)</option>
              </select>
              {/* #263 회전 드롭다운 — Fit 옆에 배치. 0/90/180/270만 허용.
                  회전 후 fit/offset이 그대로 동작 (rotation → fit/scale → crop/pad 순서). */}
              <span style={{ color: "#666", fontSize: 11, flexShrink: 0, minWidth: 32, marginLeft: 4 }}>회전</span>
              <select
                value={rotation}
                onChange={(e) => onUpdateClipRotation(parseInt(e.target.value, 10) as import("../types").RotationDeg)}
                style={{
                  minWidth: 70,
                  fontSize: 11,
                  background: "#222",
                  color: "#ddd",
                  border: "1px solid #444",
                  borderRadius: 3,
                  padding: "2px 4px",
                }}
                title="원본 영상 수동 회전 (0/90/180/270). 회전 후 fit/오프셋이 적용됩니다."
              >
                <option value={0}>0°</option>
                <option value={90}>90°</option>
                <option value={180}>180°</option>
                <option value={270}>270°</option>
              </select>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, opacity: offsetDisabled ? 0.4 : 1 }}>
              <span style={{ color: "#666", fontSize: 11, flexShrink: 0, minWidth: 50 }}>X 오프셋</span>
              <input
                type="range"
                min={-1.0}
                max={1.0}
                step={0.05}
                value={xOff}
                disabled={offsetDisabled}
                onChange={(e) => onUpdateClipOffset(parseFloat(e.target.value), yOff, { sliderOnly: true })}
                style={{ flex: 1, minWidth: 80, accentColor: "#6366f1" }}
              />
              <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 40, textAlign: "right" }}>
                {xOff.toFixed(2)}
              </span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, opacity: offsetDisabled ? 0.4 : 1 }}>
              <span style={{ color: "#666", fontSize: 11, flexShrink: 0, minWidth: 50 }}>Y 오프셋</span>
              <input
                type="range"
                min={-1.0}
                max={1.0}
                step={0.05}
                value={yOff}
                disabled={offsetDisabled}
                onChange={(e) => onUpdateClipOffset(xOff, parseFloat(e.target.value), { sliderOnly: true })}
                style={{ flex: 1, minWidth: 80, accentColor: "#6366f1" }}
              />
              <span style={{ color: "#aaa", fontSize: 11, flexShrink: 0, minWidth: 40, textAlign: "right" }}>
                {yOff.toFixed(2)}
              </span>
            </div>
          </div>
        );
      })()}

      {/* 도형 오버레이 목록 (#257 — + 도형 추가 버튼은 우측 EffectAddPanel로 이동) */}
      {(scene.shape_overlays ?? []).length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
          {(scene.shape_overlays ?? []).map((sh) => (
            <span
              key={sh.id}
              style={{ ...styles.overlayChip, background: "#1a1f1a", color: "#4ade80", borderColor: "#166534" }}
              onClick={(e) => { e.stopPropagation(); handleEditShape(sh); }}
              title={`${sh.shape} (${sh.width}×${sh.height})`}
            >
              {sh.shape === "ellipse" ? "○" : "□"} {sh.color}
              <span
                style={styles.overlayChipDel}
                onClick={(e) => { e.stopPropagation(); handleDeleteShape(sh.id); }}
                title="삭제"
              >✕</span>
            </span>
          ))}
        </div>
      )}

      {shapeEditorOpen && (
        <div onClick={(e) => e.stopPropagation()}>
          <ShapeOverlayEditor
            shape={editingShape}
            onSave={handleSaveShape}
            onClose={() => setShapeEditorOpen(false)}
            projectId={projectId}
            audioAssets={audioAssets}
          />
        </div>
      )}

      {/* 모자이크 오버레이 목록 (#264) — 도형 오버레이와 동일한 칩 패턴.
          쉡 칩을 클릭하면 펼침 패널이 열려 좌표/크기/시간/모드/세기를 편집할 수 있다.
          + 추가 버튼은 우측 EffectAddPanel(모자이크 섹션)으로 일원화. */}
      {(scene.mosaic_overlays ?? []).length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
          {(scene.mosaic_overlays ?? []).map((m) => {
            const isEditing = editingMosaicId === m.id;
            const shapeIcon = m.shape === "ellipse" ? "○" : "□";
            const modeIcon = m.mode === "blur" ? "🌫" : "🟧";
            return (
              <span
                key={m.id}
                style={{
                  ...styles.overlayChip,
                  background: "#241509",
                  color: "#fb923c",
                  borderColor: isEditing ? "#fb923c" : "#7c2d12",
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  setEditingMosaicId(isEditing ? null : m.id);
                }}
                title={`${m.shape} ${m.mode} @ (${m.x.toFixed(2)},${m.y.toFixed(2)}) ${m.width.toFixed(2)}×${m.height.toFixed(2)}`}
              >
                {modeIcon} {shapeIcon} 모자이크
                <span
                  style={styles.overlayChipDel}
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDeleteMosaic(m.id);
                  }}
                  title="삭제"
                >
                  ✕
                </span>
              </span>
            );
          })}
        </div>
      )}

      {editingMosaicId && (() => {
        const current = (scene.mosaic_overlays ?? []).find((m) => m.id === editingMosaicId);
        if (!current) return null;
        return (
          <div onClick={(e) => e.stopPropagation()}>
            <MosaicOverlayEditor
              overlay={current}
              onPatch={(patch, opts) => handleUpdateMosaic(current.id, patch, opts)}
              onClose={() => setEditingMosaicId(null)}
            />
          </div>
        );
      })()}

      {/* 이미지 오버레이 목록 — footage 스튜디오 이미지 자산 + 캡션 합성.
          shape/mosaic 와 동일한 칩 패턴. 칩 클릭으로 펼침 편집기를 토글한다.
          + 이미지 버튼은 우측 EffectAddPanel(이미지 섹션)로 일원화. */}
      {(scene.image_overlays ?? []).length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
          {(scene.image_overlays ?? []).map((img) => {
            const isEditing = editingImageId === img.id;
            const asset = imageAssets.find((a) => a.asset_id === img.image_asset_id);
            const label = asset?.label || asset?.filename || img.image_asset_id;
            const caption = (img.caption_text && img.caption_text.trim()) || "";
            return (
              <span
                key={img.id}
                style={{
                  ...styles.overlayChip,
                  background: "#082f3a",
                  color: "#22d3ee",
                  borderColor: isEditing ? "#22d3ee" : "#155e75",
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  setEditingImageId(isEditing ? null : img.id);
                }}
                title={`이미지 ${label}${caption ? ` / 캡션: ${caption}` : ""}`}
              >
                🖼 {label.length > 14 ? label.slice(0, 12) + "…" : label}
                {caption && <span style={{ opacity: 0.7, marginLeft: 4 }}>· "{caption.length > 10 ? caption.slice(0, 9) + "…" : caption}"</span>}
                <span
                  style={styles.overlayChipDel}
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDeleteImage(img.id);
                  }}
                  title="삭제"
                >
                  ✕
                </span>
              </span>
            );
          })}
        </div>
      )}

      {editingImageId && (() => {
        const current = (scene.image_overlays ?? []).find((m) => m.id === editingImageId);
        if (!current) return null;
        return (
          <div onClick={(e) => e.stopPropagation()}>
            <ImageOverlayEditor
              projectId={projectId}
              overlay={current}
              imageAssets={imageAssets}
              audioAssets={audioAssets}
              onPatch={(patch, opts) => handleUpdateImage(current.id, patch, opts)}
              onClose={() => setEditingImageId(null)}
            />
          </div>
        );
      })()}

      {/* 그래픽 오버레이 목록 (#205 1차: spotlight/alert, #216 2차: target/reaction/smell)
          #257 — 추가 버튼은 우측 EffectAddPanel로 이동, 칩만 유지 */}
      {/* #graphic-copy — 오버레이가 없어도 붙여넣기 버튼은 떠야 한다.
          예전엔 length > 0 일 때만 이 블록을 그려서, 비어 있는 씬에는
          붙여넣을 방법이 아예 없었다(사용자 보고). */}
      {((scene.graphic_overlays ?? []).length > 0 || copiedGraphic) && (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
        {(scene.graphic_overlays ?? []).map((g) => {
          const reactionIconMap: Record<string, string> = {
            thunder: "⚡",
            sweat: "💧",
            question: "❓",
            exclamation: "❗",
          };
          const smellColorMap: Record<string, string> = {
            green: "🟢",
            brown: "🟤",
            white: "⚪",
            yellow: "🟡",
            gray: "⬜",
          };
          let label: string;
          if (g.type === "spotlight") {
            label = "💡 Spotlight";
          } else if (g.type === "alert") {
            label = `🔔 ${(g.params?.text as string) || "알림"}`;
          } else if (g.type === "target") {
            const mt = String(g.params?.marker_type ?? "crosshair");
            label = mt === "arrow" ? "➤ Target(arrow)" : "✚ Target";
          } else if (g.type === "reaction") {
            const it = String(g.params?.icon_type ?? "thunder");
            label = `${reactionIconMap[it] ?? "✦"} Reaction`;
          } else if (g.type === "smell") {
            const cc = String(g.params?.color ?? "green");
            label = `${smellColorMap[cc] ?? "☁"} Smell`;
          } else if (g.type === "emoji") {
            // #graphic-copy — 이모지는 실제 글리프를 칩에 보여준다(96종이라 타입명만으론 구분 불가).
            const ik = String(g.params?.icon_type ?? "question");
            label = `${EMOJI_BY_KEY[ik] ?? "❓"} Emoji`;
          } else if (g.type === "dash") {
            label = "💨 Dash";
          } else {
            label = `· ${g.type}`;
          }
          const isEditing = editingGraphicId === g.id;
          return (
            <span
              key={g.id}
              style={{
                ...styles.overlayChip,
                background: isEditing ? "#1a1620" : "#1a1620",
                color: "#c084fc",
                borderColor: isEditing ? "#a855f7" : "#581c87",
              }}
              onClick={(e) => {
                e.stopPropagation();
                setEditingGraphicId(isEditing ? null : g.id);
              }}
              title={`${g.type} @ (${g.x.toFixed(2)},${g.y.toFixed(2)})`}
            >
              {label}
              <span
                style={{ ...styles.overlayChipDel, marginLeft: 2 }}
                onClick={(e) => { e.stopPropagation(); onCopySingleGraphic(g); }}
                title="이 오버레이만 복사"
              >
                ⧉
              </span>
              <span
                style={styles.overlayChipDel}
                onClick={(e) => {
                  e.stopPropagation();
                  handleDeleteGraphic(g.id);
                }}
                title="삭제"
              >
                ✕
              </span>
            </span>
          );
        })}
        {/* #graphic-copy — 복사해 둔 그래픽 오버레이가 있으면 이 씬에 붙여넣기. */}
        {copiedGraphic && (
          <button
            style={styles.overlayClipBtn}
            onClick={(e) => { e.stopPropagation(); onPasteGraphic(); }}
            title={`복사한 ${copiedGraphic.type} 오버레이를 이 씬에 추가`}
          >
            ⧉ 붙여넣기
          </button>
        )}
        {/* #257 — + Spotlight/Alert/Target/Reaction/Smell 버튼은 우측 EffectAddPanel로 이동 */}
      </div>
      )}

      {editingGraphicId && (() => {
        const current = (scene.graphic_overlays ?? []).find((g) => g.id === editingGraphicId);
        if (!current) return null;
        return (
          <div onClick={(e) => e.stopPropagation()}>
            <GraphicOverlayEditor
              overlay={current}
              onPatch={(patch, opts) => handleUpdateGraphic(current.id, patch, opts)}
              onPatchParams={(p, opts) => handleUpdateGraphicParams(current.id, p, opts)}
              onClose={() => setEditingGraphicId(null)}
              projectId={projectId}
              audioAssets={audioAssets}
            />
          </div>
        );
      })()}

      {/* 씬 필터 목록 (#206/#219: flashback/horror/vivid/nightvision/POV)
          #257 — 필터 추가 버튼(FilterAddButton)은 우측 EffectAddPanel로 이동, 칩만 유지 */}
      {(scene.filters ?? []).length > 0 && (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
        {(scene.filters ?? []).map((f) => {
          const labelMap: Record<SceneFilterType, string> = {
            flashback: "Flashback",
            horror: "Horror",
            vivid: "Vivid",
            nightvision: "NightVision",
            pov: "POV",
            warm: "Warm",
            cool: "Cool",
            cartoon: "Cartoon",
          };
          const label = labelMap[f.filter_type] ?? f.filter_type;
          return (
            <span
              key={f.filter_type}
              style={{
                ...styles.overlayChip,
                background: "#181a22",
                color: "#fbbf24",
                borderColor: "#b45309",
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
              title={`${label} (강도 ${(f.intensity * 100).toFixed(0)}%)`}
              onClick={(e) => e.stopPropagation()}
            >
              {label}
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={f.intensity}
                onChange={(e) => {
                  e.stopPropagation();
                  handleUpdateFilterIntensity(f.filter_type, parseFloat(e.target.value), { sliderOnly: true });
                }}
                onClick={(e) => e.stopPropagation()}
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`강도: ${(f.intensity * 100).toFixed(0)}%`}
              />
              <span
                style={styles.overlayChipDel}
                onClick={(e) => {
                  e.stopPropagation();
                  handleDeleteFilter(f.filter_type);
                }}
                title="제거"
              >
                ✕
              </span>
            </span>
          );
        })}
        {/* #257 — 필터 추가 버튼은 우측 EffectAddPanel로 이동 */}
      </div>
      )}

      {/* 집중선(속도선) 파라미터 패널 — 색/밀도/중심/두께 조절 + 제거 */}
      {(scene.speed_lines ?? []).map((s) => (
        <div
          key={s.id}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            padding: "4px 8px",
            borderRadius: 6,
            border: "1px solid #b45309",
            background: "#181a22",
            color: "#fbbf24",
            fontSize: "0.85em",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <span style={{ fontWeight: 600 }}>집중선</span>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              색
              <input
                type="color"
                value={s.color}
                // type="color" 는 네이티브 피커 드래그 중 onChange 가 연속 발생한다.
                // sliderOnly:true 로 store/DB/서버 patch 만 갱신하고 미리보기 재생성은 억제,
                // 피커 닫힘(onBlur) 시 sliderOnly:false 로 1회만 미리보기 재생성한다.
                onChange={(e) => handleUpdateSpeedLines(s.id, { color: e.target.value }, { sliderOnly: true })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                title="선 색상"
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              밀도
              <input
                type="range" min={20} max={200} step={5}
                value={s.density}
                onChange={(e) => handleUpdateSpeedLines(s.id, { density: parseInt(e.target.value, 10) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 70, accentColor: "#fbbf24" }}
                title={`선 개수: ${s.density}`}
              />
              <span style={{ width: 28, textAlign: "right" }}>{s.density}</span>
            </label>
            <span
              style={styles.overlayChipDel}
              onClick={(e) => { e.stopPropagation(); handleDeleteSpeedLines(s.id); }}
              title="제거"
            >
              ✕
            </span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              중심X
              <input
                type="range" min={0} max={1} step={0.05}
                value={s.center_x}
                onChange={(e) => handleUpdateSpeedLines(s.id, { center_x: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`center_x: ${s.center_x.toFixed(2)}`}
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              중심Y
              <input
                type="range" min={0} max={1} step={0.05}
                value={s.center_y}
                onChange={(e) => handleUpdateSpeedLines(s.id, { center_y: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`center_y: ${s.center_y.toFixed(2)}`}
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              빈원
              <input
                type="range" min={0} max={1.5} step={0.05}
                value={s.inner_ratio}
                onChange={(e) => handleUpdateSpeedLines(s.id, { inner_ratio: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`중앙 빈 영역(타원, 영역 비율): ${s.inner_ratio.toFixed(2)}`}
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              굵기
              <input
                type="range" min={0.2} max={2.5} step={0.1}
                value={s.thickness}
                onChange={(e) => handleUpdateSpeedLines(s.id, { thickness: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`두께: ${s.thickness.toFixed(1)}`}
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              굵기편차
              <input
                type="range" min={0} max={0.6} step={0.05}
                value={s.width_variance ?? 0.2}
                onChange={(e) => handleUpdateSpeedLines(s.id, { width_variance: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`선별 굵기 랜덤 편차: ±${(((s.width_variance ?? 0.2)) * 100).toFixed(0)}%`}
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              간격
              <input
                type="range" min={0} max={1} step={0.05}
                value={s.jitter ?? 0.6}
                onChange={(e) => handleUpdateSpeedLines(s.id, { jitter: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`선 사이 간격 랜덤 정도(0=균등): ${(s.jitter ?? 0.6).toFixed(2)}`}
              />
            </label>
          </div>
          {/* 표시 영역 크기 — (center_x, center_y) 중심의 area_w×area_h 사각형 안에만 방사. */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              영역폭
              <input
                type="range" min={0.05} max={1} step={0.01}
                value={s.area_w ?? 1.0}
                onChange={(e) => handleUpdateSpeedLines(s.id, { area_w: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 140, accentColor: "#fbbf24" }}
                title={`영역 폭: ${(s.area_w ?? 1.0).toFixed(2)}`}
              />
              <span style={{ width: 32, textAlign: "right" }}>{(s.area_w ?? 1.0).toFixed(2)}</span>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              영역높이
              <input
                type="range" min={0.05} max={1} step={0.01}
                value={s.area_h ?? 1.0}
                onChange={(e) => handleUpdateSpeedLines(s.id, { area_h: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 140, accentColor: "#fbbf24" }}
                title={`영역 높이: ${(s.area_h ?? 1.0).toFixed(2)}`}
              />
              <span style={{ width: 32, textAlign: "right" }}>{(s.area_h ?? 1.0).toFixed(2)}</span>
            </label>
          </div>
          {/* 선별 페이드 인/아웃 반복(깜빡임) 애니메이션 — on/off + 깜빡임 속도.
              각 선이 최대 길이로 고정된 채 무작위 타이밍으로 페이드 인/아웃을 반복한다. */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <input
                type="checkbox"
                checked={s.animate ?? true}
                onChange={(e) => handleUpdateSpeedLines(s.id, { animate: e.target.checked })}
                title="재생 중 각 집중선이 무작위 타이밍으로 나타났다 사라지길 반복하는 깜빡임 애니메이션"
              />
              깜빡임 애니
            </label>
            {/* start_scale 슬라이더는 제거됨 — dead control(823c543). 선 길이 성장 방식도 폐기,
                이제 애니는 선별 페이드 인/아웃 반복. */}
            <label style={{ display: "inline-flex", alignItems: "center", gap: 4, opacity: (s.animate ?? true) ? 1 : 0.4 }}>
              속도
              <input
                type="range" min={0.3} max={3.0} step={0.1}
                value={s.anim_speed ?? 1.0}
                disabled={!(s.animate ?? true)}
                onChange={(e) => handleUpdateSpeedLines(s.id, { anim_speed: parseFloat(e.target.value) }, { sliderOnly: true })}
                onPointerUp={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                onBlur={() => handleUpdateSpeedLines(s.id, {}, { sliderOnly: false })}
                data-skip-history-stash="1"
                style={{ width: 60, accentColor: "#fbbf24" }}
                title={`깜빡임 속도 배수: ${(s.anim_speed ?? 1.0).toFixed(1)}`}
              />
              <span style={{ width: 24, textAlign: "right" }}>{(s.anim_speed ?? 1.0).toFixed(1)}</span>
            </label>
          </div>
        </div>
      ))}

      {/* POV 전용 파라미터 패널 (center_x / center_y / radius / feather) — #219 1차 / #231 키프레임 */}
      {(() => {
        const povFilter = (scene.filters ?? []).find((f) => f.filter_type === "pov");
        if (!povFilter) return null;
        const p = (povFilter.params ?? {}) as Record<string, unknown>;
        const centerX = typeof p.center_x === "number" ? p.center_x : 0.5;
        const centerY = typeof p.center_y === "number" ? p.center_y : 0.5;
        const radius = typeof p.radius === "number" ? p.radius : 0.35;
        const feather = typeof p.feather === "number" ? p.feather : 80;
        // #231: 키프레임 모드 — keyframes.length >= 2일 때 정적 center_x/y/radius 슬라이더 숨김.
        const keyframes: Array<Record<string, number>> = Array.isArray(p.keyframes)
          ? (p.keyframes as Array<Record<string, number>>)
          : [];
        const isDynamic = keyframes.length >= 2;
        const TIMELINE_MAX_T = 10;
        return (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
              padding: "4px 8px",
              borderRadius: 6,
              border: "1px solid #b45309",
              background: "#181a22",
              color: "#fbbf24",
              fontSize: "0.85em",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                alignItems: "center",
              }}
            >
              <span style={{ fontWeight: 600 }}>POV {isDynamic ? "(키프레임 모드)" : ""}</span>
              {!isDynamic && (
                <>
                  <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    X
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={centerX}
                      onChange={(e) =>
                        handleUpdateFilterParam("pov", "center_x", parseFloat(e.target.value), { sliderOnly: true })
                      }
                      style={{ width: 70, accentColor: "#fbbf24" }}
                      title={`center_x: ${centerX.toFixed(2)}`}
                    />
                    <span style={{ width: 32, textAlign: "right" }}>{centerX.toFixed(2)}</span>
                  </label>
                  <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    Y
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={centerY}
                      onChange={(e) =>
                        handleUpdateFilterParam("pov", "center_y", parseFloat(e.target.value), { sliderOnly: true })
                      }
                      style={{ width: 70, accentColor: "#fbbf24" }}
                      title={`center_y: ${centerY.toFixed(2)}`}
                    />
                    <span style={{ width: 32, textAlign: "right" }}>{centerY.toFixed(2)}</span>
                  </label>
                  <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    R
                    <input
                      type="range"
                      min={0.05}
                      max={0.8}
                      step={0.05}
                      value={radius}
                      onChange={(e) =>
                        handleUpdateFilterParam("pov", "radius", parseFloat(e.target.value), { sliderOnly: true })
                      }
                      style={{ width: 70, accentColor: "#fbbf24" }}
                      title={`radius: ${radius.toFixed(2)}`}
                    />
                    <span style={{ width: 32, textAlign: "right" }}>{radius.toFixed(2)}</span>
                  </label>
                </>
              )}
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                Feather
                <input
                  type="range"
                  min={0}
                  max={200}
                  step={10}
                  value={feather}
                  onChange={(e) =>
                    handleUpdateFilterParam("pov", "feather", parseFloat(e.target.value), { sliderOnly: true })
                  }
                  style={{ width: 70, accentColor: "#fbbf24" }}
                  title={`feather: ${feather}px`}
                />
                <span style={{ width: 40, textAlign: "right" }}>{feather}px</span>
              </label>
            </div>

            {/* #231: 키프레임 섹션 */}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 4,
                paddingTop: 4,
                borderTop: "1px dashed #b45309",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <span style={{ fontWeight: 600 }}>키프레임 ({keyframes.length})</span>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleAddPovKeyframe();
                  }}
                  style={{
                    fontSize: "0.85em",
                    padding: "2px 8px",
                    borderRadius: 4,
                    border: "1px solid #fbbf24",
                    background: "#1f2937",
                    color: "#fbbf24",
                    cursor: "pointer",
                  }}
                  title="새 키프레임 추가"
                >
                  + 키프레임
                </button>
                {keyframes.length === 1 && (
                  <span style={{ fontSize: "0.85em", opacity: 0.7 }}>
                    (2개 이상이어야 동적 보간 활성화)
                  </span>
                )}
              </div>

              {/* 타임라인 시각화 (0~10초 가로 막대 + 키프레임 점) */}
              {keyframes.length > 0 && (
                <div
                  style={{
                    position: "relative",
                    height: 14,
                    background: "#0f1117",
                    border: "1px solid #374151",
                    borderRadius: 4,
                    margin: "2px 0",
                  }}
                  title="0~10초 타임라인"
                >
                  {keyframes.map((kf, idx) => {
                    const t = Number(kf.t ?? 0);
                    const leftPct = Math.max(0, Math.min(100, (t / TIMELINE_MAX_T) * 100));
                    return (
                      <div
                        key={idx}
                        style={{
                          position: "absolute",
                          left: `${leftPct}%`,
                          top: 2,
                          width: 8,
                          height: 8,
                          marginLeft: -4,
                          borderRadius: "50%",
                          background: "#fbbf24",
                          border: "1px solid #b45309",
                        }}
                        title={`t=${t.toFixed(2)}s`}
                      />
                    );
                  })}
                </div>
              )}

              {/* 키프레임 목록 */}
              {keyframes.map((kf, idx) => {
                const kfT = Number(kf.t ?? 0);
                const kfCx = Number(kf.center_x ?? 0.5);
                const kfCy = Number(kf.center_y ?? 0.5);
                const kfR = Number(kf.radius ?? 0.35);
                return (
                  <div
                    key={idx}
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      gap: 6,
                      alignItems: "center",
                      padding: "2px 4px",
                      borderRadius: 4,
                      background: "#1f2937",
                    }}
                  >
                    <span style={{ minWidth: 32, fontWeight: 600 }}>#{idx + 1}</span>
                    <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      t
                      <input
                        type="range"
                        min={0}
                        max={TIMELINE_MAX_T}
                        step={0.1}
                        value={kfT}
                        onChange={(e) =>
                          handleUpdatePovKeyframe(idx, "t", parseFloat(e.target.value), { sliderOnly: true })
                        }
                        style={{ width: 70, accentColor: "#fbbf24" }}
                        title={`t: ${kfT.toFixed(2)}s`}
                      />
                      <span style={{ width: 36, textAlign: "right" }}>{kfT.toFixed(2)}s</span>
                    </label>
                    <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      X
                      <input
                        type="range"
                        min={0}
                        max={1}
                        step={0.05}
                        value={kfCx}
                        onChange={(e) =>
                          handleUpdatePovKeyframe(idx, "center_x", parseFloat(e.target.value), { sliderOnly: true })
                        }
                        style={{ width: 60, accentColor: "#fbbf24" }}
                        title={`center_x: ${kfCx.toFixed(2)}`}
                      />
                      <span style={{ width: 32, textAlign: "right" }}>{kfCx.toFixed(2)}</span>
                    </label>
                    <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      Y
                      <input
                        type="range"
                        min={0}
                        max={1}
                        step={0.05}
                        value={kfCy}
                        onChange={(e) =>
                          handleUpdatePovKeyframe(idx, "center_y", parseFloat(e.target.value), { sliderOnly: true })
                        }
                        style={{ width: 60, accentColor: "#fbbf24" }}
                        title={`center_y: ${kfCy.toFixed(2)}`}
                      />
                      <span style={{ width: 32, textAlign: "right" }}>{kfCy.toFixed(2)}</span>
                    </label>
                    <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      R
                      <input
                        type="range"
                        min={0.05}
                        max={0.8}
                        step={0.05}
                        value={kfR}
                        onChange={(e) =>
                          handleUpdatePovKeyframe(idx, "radius", parseFloat(e.target.value), { sliderOnly: true })
                        }
                        style={{ width: 60, accentColor: "#fbbf24" }}
                        title={`radius: ${kfR.toFixed(2)}`}
                      />
                      <span style={{ width: 32, textAlign: "right" }}>{kfR.toFixed(2)}</span>
                    </label>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeletePovKeyframe(idx);
                      }}
                      style={{
                        fontSize: "0.85em",
                        padding: "0 6px",
                        borderRadius: 4,
                        border: "1px solid #ef4444",
                        background: "transparent",
                        color: "#ef4444",
                        cursor: "pointer",
                      }}
                      title="삭제"
                    >
                      ×
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })()}

      {/* 씬 모션 목록 (#207: shake / clip_zoom — 1차 구현). slowmo는 후속 사이클.
          #257 — 모션 추가 버튼은 우측 EffectAddPanel로 이동, 칩만 유지 */}
      {(scene.motions ?? []).length > 0 && (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
        {(scene.motions ?? []).map((m, idx) => {
          const labelMap: Record<MotionType, string> = {
            shake: "Shake",
            clip_zoom: "Zoom",
            slowmo: "Slow",
            stutter: "Stutter",
          };
          const label = labelMap[m.motion_type] ?? m.motion_type;
          return (
            <span
              key={`${m.motion_type}-${idx}`}
              style={{
                ...styles.overlayChip,
                background: "#0a1c1c",
                color: "#67e8f9",
                borderColor: "#0e7490",
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
              }}
              title={`${label} start=${m.start_sec.toFixed(2)}s dur=${m.duration_sec.toFixed(2)}s`}
              onClick={(e) => e.stopPropagation()}
            >
              {label}
              <span
                style={styles.overlayChipDel}
                onClick={(e) => {
                  e.stopPropagation();
                  handleDeleteMotion(idx);
                }}
                title="제거"
              >
                ✕
              </span>
            </span>
          );
        })}
        {/* #257 — 모션 추가 버튼은 우측 EffectAddPanel로 이동 */}
      </div>
      )}

      {/* 씬 모션 파라미터 패널 (#207, #347) — shake / clip_zoom / slowmo / stutter 슬라이더 */}
      {(scene.motions ?? []).map((m, idx) => {
        const p = (m.params ?? {}) as Record<string, unknown>;
        if (m.motion_type === "shake") {
          const intensity = typeof p.intensity === "number" ? p.intensity : 0.6;
          return (
            <div
              key={`params-${idx}`}
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                alignItems: "center",
                padding: "4px 8px",
                borderRadius: 6,
                border: "1px solid #0e7490",
                background: "#0a1c1c",
                color: "#67e8f9",
                fontSize: "0.85em",
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <span style={{ fontWeight: 600 }}>Shake #{idx + 1}</span>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                강도
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.1}
                  value={intensity}
                  onChange={(e) =>
                    handleUpdateMotionParam(idx, "intensity", parseFloat(e.target.value), { sliderOnly: true })
                  }
                  style={{ width: 70, accentColor: "#67e8f9" }}
                  title={`intensity: ${intensity.toFixed(2)}`}
                />
                <span style={{ width: 32, textAlign: "right" }}>{intensity.toFixed(2)}</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                시작
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  value={m.start_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { start_sec: parseFloat(e.target.value) || 0 }, { sliderOnly: true })
                  }
                  style={{ width: 50, fontSize: 11, padding: "2px 4px", background: "#0f1117", color: "#67e8f9", border: "1px solid #0e7490", borderRadius: 4 }}
                />
                <span>s</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                길이
                <input
                  type="range"
                  min={0.2}
                  max={5}
                  step={0.1}
                  value={m.duration_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { duration_sec: parseFloat(e.target.value) }, { sliderOnly: true })
                  }
                  style={{ width: 70, accentColor: "#67e8f9" }}
                  title={`duration: ${m.duration_sec.toFixed(2)}s`}
                />
                <span style={{ width: 40, textAlign: "right" }}>{m.duration_sec.toFixed(1)}s</span>
              </label>
            </div>
          );
        }
        if (m.motion_type === "stutter") {
          // #347 — 끊김/점프 모션 강도 조절. shake intensity 슬라이더 패턴 재사용.
          const intensity = typeof p.intensity === "number" ? p.intensity : 0.3;
          return (
            <div
              key={`params-${idx}`}
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                alignItems: "center",
                padding: "4px 8px",
                borderRadius: 6,
                border: "1px solid #0e7490",
                background: "#0a1c1c",
                color: "#67e8f9",
                fontSize: "0.85em",
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <span style={{ fontWeight: 600 }}>Stutter #{idx + 1}</span>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                끊김 강도
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.1}
                  value={intensity}
                  onChange={(e) =>
                    handleUpdateMotionParam(idx, "intensity", parseFloat(e.target.value), { sliderOnly: true })
                  }
                  style={{ width: 70, accentColor: "#67e8f9" }}
                  title={`intensity: ${intensity.toFixed(2)}`}
                />
                <span style={{ width: 32, textAlign: "right" }}>{intensity.toFixed(2)}</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                시작
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  value={m.start_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { start_sec: parseFloat(e.target.value) || 0 }, { sliderOnly: true })
                  }
                  style={{ width: 50, fontSize: 11, padding: "2px 4px", background: "#0f1117", color: "#67e8f9", border: "1px solid #0e7490", borderRadius: 4 }}
                />
                <span>s</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                길이
                <input
                  type="range"
                  min={0.2}
                  max={5}
                  step={0.1}
                  value={m.duration_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { duration_sec: parseFloat(e.target.value) }, { sliderOnly: true })
                  }
                  style={{ width: 70, accentColor: "#67e8f9" }}
                  title={`duration: ${m.duration_sec.toFixed(2)}s`}
                />
                <span style={{ width: 40, textAlign: "right" }}>{m.duration_sec.toFixed(1)}s</span>
              </label>
            </div>
          );
        }
        if (m.motion_type === "clip_zoom") {
          const targetX = typeof p.target_x === "number" ? p.target_x : 0.5;
          const targetY = typeof p.target_y === "number" ? p.target_y : 0.5;
          const startScale = typeof p.start_scale === "number" ? p.start_scale : 1.0;
          const endScale = typeof p.end_scale === "number" ? p.end_scale : 1.3;
          // 씬 전체 길이 — TTS duration 이 있으면 그것을, 없으면 footage clip_end-clip_start 를 사용.
          // 둘 다 없거나 0이면 안전한 기본값(5초) 유지.
          // (사용자 요구: 길이 슬라이더 max 를 씬 전체 길이로.)
          const ttsDurMax = scene.tts?.status === "ready" ? scene.tts.duration_sec : 0;
          const fcZoom = scene.footage as { clip_start_sec?: number | null; clip_end_sec?: number | null } | null;
          const clipDurMax = fcZoom && typeof fcZoom.clip_end_sec === "number" && typeof fcZoom.clip_start_sec === "number"
            ? Math.max(0, fcZoom.clip_end_sec - fcZoom.clip_start_sec)
            : 0;
          const sceneDurZoom = Math.max(ttsDurMax || 0, clipDurMax || 0);
          const lenMax = sceneDurZoom > 0.3 ? Math.round(sceneDurZoom * 10) / 10 : 5;
          // 시작 슬라이더 max 도 씬 길이 안쪽으로 제한 (초과 입력 방지).
          const startMax = sceneDurZoom > 0 ? Math.round(sceneDurZoom * 10) / 10 : undefined;
          return (
            <div
              key={`params-${idx}`}
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                alignItems: "center",
                padding: "4px 8px",
                borderRadius: 6,
                border: "1px solid #0e7490",
                background: "#0a1c1c",
                color: "#67e8f9",
                fontSize: "0.85em",
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <span style={{ fontWeight: 600 }}>Zoom #{idx + 1}</span>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                X
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={targetX}
                  onChange={(e) =>
                    handleUpdateMotionParam(idx, "target_x", parseFloat(e.target.value), { sliderOnly: true })
                  }
                  style={{ width: 60, accentColor: "#67e8f9" }}
                />
                <span style={{ width: 32, textAlign: "right" }}>{targetX.toFixed(2)}</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                Y
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={targetY}
                  onChange={(e) =>
                    handleUpdateMotionParam(idx, "target_y", parseFloat(e.target.value), { sliderOnly: true })
                  }
                  style={{ width: 60, accentColor: "#67e8f9" }}
                />
                <span style={{ width: 32, textAlign: "right" }}>{targetY.toFixed(2)}</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                End
                <input
                  type="range"
                  min={1.0}
                  max={3.0}
                  step={0.05}
                  value={endScale}
                  onChange={(e) =>
                    handleUpdateMotionParam(idx, "end_scale", parseFloat(e.target.value), { sliderOnly: true })
                  }
                  style={{ width: 60, accentColor: "#67e8f9" }}
                  title={`end_scale: ${endScale.toFixed(2)}x (start_scale=${startScale.toFixed(2)}x)`}
                />
                <span style={{ width: 40, textAlign: "right" }}>{endScale.toFixed(2)}x</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                시작
                <input
                  type="number"
                  min={0}
                  max={startMax}
                  step={0.1}
                  value={m.start_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { start_sec: parseFloat(e.target.value) || 0 }, { sliderOnly: true })
                  }
                  style={{ width: 50, fontSize: 11, padding: "2px 4px", background: "#0f1117", color: "#67e8f9", border: "1px solid #0e7490", borderRadius: 4 }}
                  title={startMax ? `0 ~ ${startMax.toFixed(1)}s (씬 길이)` : undefined}
                />
                <span>s</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                길이
                <input
                  type="range"
                  min={0.3}
                  max={lenMax}
                  step={0.1}
                  value={m.duration_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { duration_sec: parseFloat(e.target.value) }, { sliderOnly: true })
                  }
                  style={{ width: 70, accentColor: "#67e8f9" }}
                  title={`duration: ${m.duration_sec.toFixed(2)}s (max ${lenMax.toFixed(1)}s = 씬 길이)`}
                />
                <span style={{ width: 40, textAlign: "right" }}>{m.duration_sec.toFixed(1)}s</span>
              </label>
            </div>
          );
        }
        if (m.motion_type === "slowmo") {
          // #234 1차: speed + interpolate (씬 전체 구간).
          // #236: start_sec/duration_sec(부분 구간 자동 분기) + freeze_at_end_sec.
          const speed = typeof p.speed === "number" ? p.speed : 0.5;
          const interpolate = p.interpolate === true;
          const freezeAtEnd = typeof p.freeze_at_end_sec === "number" ? p.freeze_at_end_sec : 0.0;
          return (
            <div
              key={`params-${idx}`}
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                alignItems: "center",
                padding: "4px 8px",
                borderRadius: 6,
                border: "1px solid #0e7490",
                background: "#0a1c1c",
                color: "#67e8f9",
                fontSize: "0.85em",
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <span style={{ fontWeight: 600 }}>Slow #{idx + 1}</span>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                속도
                <select
                  value={String(speed)}
                  onChange={(e) =>
                    handleUpdateMotionParam(idx, "speed", parseFloat(e.target.value))
                  }
                  style={{
                    fontSize: 11,
                    padding: "2px 4px",
                    background: "#0f1117",
                    color: "#67e8f9",
                    border: "1px solid #0e7490",
                    borderRadius: 4,
                  }}
                  title={`speed: ${speed.toFixed(2)}x (길이 ${(1 / speed).toFixed(2)}배)`}
                >
                  <option value="0.25">0.25x</option>
                  <option value="0.5">0.5x</option>
                  <option value="0.75">0.75x</option>
                  <option value="1">1.0x</option>
                </select>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                시작
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  value={m.start_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { start_sec: parseFloat(e.target.value) || 0 }, { sliderOnly: true })
                  }
                  style={{ width: 50, fontSize: 11, padding: "2px 4px", background: "#0f1117", color: "#67e8f9", border: "1px solid #0e7490", borderRadius: 4 }}
                  title="0이고 길이가 씬 전체를 덮으면 씬 전체 경로(setpts), 그 외엔 부분 구간(filter_complex) 경로로 자동 분기"
                />
                <span>s</span>
              </label>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                길이
                <input
                  type="range"
                  min={0.1}
                  max={10}
                  step={0.1}
                  value={m.duration_sec}
                  onChange={(e) =>
                    handleUpdateMotion(idx, { duration_sec: parseFloat(e.target.value) }, { sliderOnly: true })
                  }
                  style={{ width: 70, accentColor: "#67e8f9" }}
                  title={`duration: ${m.duration_sec.toFixed(2)}s (씬 길이 초과 시 자동 클램프)`}
                />
                <span style={{ width: 40, textAlign: "right" }}>{m.duration_sec.toFixed(1)}s</span>
              </label>
              <label
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                title="slowmo 구간 끝에서 마지막 프레임을 N초간 정지 (tpad=stop_mode=clone). 0이면 미적용."
              >
                동결
                <input
                  type="range"
                  min={0}
                  max={2}
                  step={0.1}
                  value={freezeAtEnd}
                  onChange={(e) =>
                    handleUpdateMotionParam(idx, "freeze_at_end_sec", parseFloat(e.target.value), { sliderOnly: true })
                  }
                  style={{ width: 60, accentColor: "#67e8f9" }}
                />
                <span style={{ width: 32, textAlign: "right" }}>{freezeAtEnd.toFixed(1)}s</span>
              </label>
              <label
                style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                title="minterpolate=fps=60 (프레임 보간으로 부드러움 향상, 인코딩 시간 증가)"
              >
                <input
                  type="checkbox"
                  checked={interpolate}
                  onChange={(e) => {
                    const existing = scene.motions ?? [];
                    if (idx < 0 || idx >= existing.length) return;
                    const nextParams = {
                      ...(existing[idx].params ?? {}),
                      interpolate: e.target.checked,
                    };
                    onUpdateMotions(
                      existing.map((mm, i) =>
                        i === idx ? { ...mm, params: nextParams } : mm
                      )
                    );
                  }}
                  style={{ accentColor: "#67e8f9" }}
                />
                보간(60fps)
              </label>
            </div>
          );
        }
        return null;
      })}

      {/* 텍스트 오버레이 목록 (#257 — + 오버레이 추가 버튼은 우측 EffectAddPanel로 이동.
          복사/붙여넣기 버튼은 클립보드 유틸리티로 SceneItem에 유지) */}
      {((scene.text_overlays ?? []).length > 0 || copiedOverlays.length > 0) && (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
        {(scene.text_overlays ?? []).map((ov) => (
          <span
            key={ov.id}
            style={styles.overlayChip}
            onClick={(e) => { e.stopPropagation(); handleEditOverlay(ov); }}
            title={ov.text}
          >
            T: {ov.text.length > 10 ? ov.text.slice(0, 10) + "…" : ov.text}
            <span
              style={{ ...styles.overlayChipDel, marginLeft: 2 }}
              onClick={(e) => { e.stopPropagation(); onCopySingleOverlay(ov); }}
              title="이 오버레이만 복사"
            >⧉</span>
            <span
              style={styles.overlayChipDel}
              onClick={(e) => { e.stopPropagation(); handleDeleteOverlay(ov.id); }}
              title="삭제"
            >✕</span>
          </span>
        ))}
        {(scene.text_overlays ?? []).length > 0 && (
          <button
            style={styles.overlayClipBtn}
            onClick={(e) => { e.stopPropagation(); onCopyOverlays(); }}
            title="이 씬의 오버레이 전체 복사"
          >복사</button>
        )}
        {copiedOverlays.length > 0 && (
          <button
            style={{ ...styles.overlayClipBtn, color: "#6366f1", borderColor: "#6366f1" }}
            onClick={(e) => { e.stopPropagation(); onPasteOverlays(); }}
            title={`오버레이 ${copiedOverlays.length}개 붙여넣기`}
          >붙여넣기 ({copiedOverlays.length})</button>
        )}
      </div>
      )}

      {/* #281 — 효과 복사 / 효과 붙여넣기: 텍스트 외 모든 효과 종류를 한 번에 일괄 복사·붙여넣기.
          기존 텍스트 오버레이 "복사"/"붙여넣기"(위쪽)는 텍스트 전용으로 유지하여 회귀를 막는다. */}
      {(() => {
        const hasAnyEffect =
          (scene.text_overlays ?? []).length > 0 ||
          (scene.shape_overlays ?? []).length > 0 ||
          (scene.graphic_overlays ?? []).length > 0 ||
          (scene.filters ?? []).length > 0 ||
          (scene.motions ?? []).length > 0 ||
          (scene.mosaic_overlays ?? []).length > 0 ||
          (scene.image_overlays ?? []).length > 0;
        const copiedNonEmptyCount = copiedEffects
          ? (copiedEffects.text_overlays.length > 0 ? 1 : 0) +
            (copiedEffects.shape_overlays.length > 0 ? 1 : 0) +
            (copiedEffects.graphic_overlays.length > 0 ? 1 : 0) +
            (copiedEffects.filters.length > 0 ? 1 : 0) +
            (copiedEffects.motions.length > 0 ? 1 : 0) +
            (copiedEffects.mosaic_overlays.length > 0 ? 1 : 0) +
            ((copiedEffects.image_overlays ?? []).length > 0 ? 1 : 0)
          : 0;
        // 현재 씬에 효과가 하나도 없고 클립보드도 비어있으면 렌더하지 않음 (UI 노이즈 회피).
        if (!hasAnyEffect && copiedNonEmptyCount === 0) return null;
        return (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, alignItems: "center" }}>
            {hasAnyEffect && (
              <button
                style={{ ...styles.overlayClipBtn, color: "#fbbf24", borderColor: "#7c5803" }}
                onClick={(e) => { e.stopPropagation(); onCopyEffects(); }}
                title="이 씬의 모든 효과(텍스트/도형/그래픽/필터/모션/모자이크) 복사"
              >효과 복사</button>
            )}
            {copiedNonEmptyCount > 0 && (
              <button
                style={{ ...styles.overlayClipBtn, color: "#fbbf24", borderColor: "#fbbf24" }}
                onClick={(e) => { e.stopPropagation(); onPasteEffects(); }}
                title="클립보드에 저장된 모든 효과를 이 씬에 추가 (id 재발급, 기존 효과 유지)"
              >효과 붙여넣기 ({copiedNonEmptyCount}종)</button>
            )}
          </div>
        );
      })()}

      {overlayEditorOpen && (
        <div onClick={(e) => e.stopPropagation()}>
          <TextOverlayEditor
            overlay={editingOverlay}
            onSave={handleSaveOverlay}
            onClose={() => setOverlayEditorOpen(false)}
            projectId={projectId}
            audioAssets={audioAssets}
          />
        </div>
      )}

      {isAiEffect && (
        <div style={styles.aiPlaceholder}>
          ⚡ {effectType} — 렌더링 시 AI가 처리합니다
        </div>
      )}

      {isOver && (
        <div style={styles.dropOverlay}>여기에 footage 드롭</div>
      )}
    </div>
  );
}

// #257 — FilterAddButton은 EffectAddPanel.tsx로 이동 (씬 카드 외 사이드 패널에서만 사용).

const styles: Record<string, React.CSSProperties> = {
  layout: { display: "flex", height: "100vh", background: "#0f0f0f", color: "#fff" },
  sidebar: { width: 240, borderRight: "1px solid #222", display: "flex", flexDirection: "column", flexShrink: 0 },
  sidebarHeader: { padding: "12px 12px 8px", borderBottom: "1px solid #222", display: "flex", flexDirection: "column", gap: 8 },
  sidebarTitle: { color: "#aaa", fontSize: 11, fontWeight: 700, textTransform: "uppercase" },
  // #380 — 사이드바 헤더의 "+ 화면 녹화" 트리거 버튼.
  sidebarRecordBtn: {
    marginLeft: "auto", padding: "3px 8px", fontSize: 11, fontWeight: 600,
    background: "#1e1e2e", color: "#a5b4fc",
    border: "1px solid #2a2a4a", borderRadius: 6, cursor: "pointer", flexShrink: 0,
  },
  searchInput: { padding: "6px 10px", background: "#111", border: "1px solid #333", borderRadius: 6, color: "#fff", fontSize: 12, width: "100%", boxSizing: "border-box" },
  thumbGrid: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, padding: 8, overflowY: "auto", flex: 1 },
  thumb: { cursor: "grab", background: "#1a1a1a", borderRadius: 6, overflow: "hidden" },
  thumbImg: { width: "100%", aspectRatio: "16/9", objectFit: "cover", display: "block" },
  thumbLabel: { color: "#666", fontSize: 9, padding: "3px 4px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  main: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  topBar: { padding: "10px 20px", borderBottom: "1px solid #222", display: "flex", flexDirection: "column" as const, alignItems: "stretch", gap: 8, flexShrink: 0 },
  topBarHeader: { display: "flex", alignItems: "center", gap: 12, flexShrink: 0 },
  projectTitle: { color: "#fff", fontWeight: 700, fontSize: 16 },
  topActions: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" as const },
  topRowFirst: { display: "flex", alignItems: "center", gap: 16, width: "100%" },
  panelToggleBar: { display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 6, padding: "6px 14px", background: "#0c0c0c", borderBottom: "1px solid #1a1a1a", flexShrink: 0 },
  panelToggleBtn: { padding: "3px 10px", background: "#0f0f0f", color: "#555", border: "1px solid #222", borderRadius: 5, fontSize: 11, cursor: "pointer" },
  panelToggleOn: { background: "#1e293b", color: "#93c5fd", borderColor: "#334155" },
  durationWarning: { fontSize: 10, color: "#f59e0b", background: "#1c1200", border: "1px solid #3a2800", borderRadius: 4, padding: "2px 6px", marginTop: 4, cursor: "default" },
  durationOk: { fontSize: 10, color: "#4ade80", background: "#0a1f0f", border: "1px solid #166534", borderRadius: 4, padding: "2px 6px", marginTop: 4, cursor: "default" },
  processingBadge: { color: "#6366f1", fontSize: 13 },
  unsyncedBadge: { color: "#f59e0b", fontSize: 12, background: "#1c1500", border: "1px solid #3a2e00", padding: "3px 10px", borderRadius: 6 },
  backBtn: { padding: "6px 14px", background: "transparent", color: "#666", border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 13 },
  generateAllBtn: { padding: "6px 14px", background: "#0f2318", color: "#22c55e", border: "1px solid #166534", borderRadius: 7, cursor: "pointer", fontSize: 12 },
  // #384 — 타임라인 클릭 후 새 TTS segment 추가 진입 버튼.
  //   anchor 가 있으면 보라(인스펙터 헤더 색과 통일), 없으면 회색 disabled.
  addTtsSegmentBtn: { padding: "6px 14px", background: "#1e1b4b", color: "#a78bfa", border: "1px solid #4338ca", borderRadius: 7, cursor: "pointer", fontSize: 12, whiteSpace: "nowrap" as const },
  addTtsSegmentBtnDisabled: { background: "#161616", color: "#555", border: "1px solid #2a2a2a", cursor: "not-allowed" as const },
  voiceSelect: { padding: "6px 10px", background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 7, fontSize: 12, cursor: "pointer" },
  voicePreviewBtn: { padding: "6px 10px", background: "#1a1a1a", color: "#a5b4fc", border: "1px solid #333", borderRadius: 7, fontSize: 12, cursor: "pointer", minWidth: 32 },
  splitSceneDissolveToggle: { display: "flex", alignItems: "center", gap: 6, padding: "6px 10px", background: "#1a1a1a", color: "#ccc", border: "1px solid #333", borderRadius: 7, fontSize: 12, cursor: "pointer", whiteSpace: "nowrap" as const, userSelect: "none" as const },
  regenAllTtsBtn: { padding: "6px 12px", background: "#1e293b", color: "#93c5fd", border: "1px solid #334155", borderRadius: 7, fontSize: 12, cursor: "pointer", whiteSpace: "nowrap" as const },
  renderBtn: { padding: "8px 20px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, cursor: "pointer", fontSize: 14 },
  renderBtnMain: { padding: "8px 18px", background: "#6366f1", color: "#fff", border: "none", borderTopLeftRadius: 8, borderBottomLeftRadius: 8, borderTopRightRadius: 0, borderBottomRightRadius: 0, cursor: "pointer", fontSize: 14 },
  renderBtnCaret: { padding: "8px 10px", background: "#4f46e5", color: "#fff", border: "none", borderTopRightRadius: 8, borderBottomRightRadius: 8, borderTopLeftRadius: 0, borderBottomLeftRadius: 0, borderLeft: "1px solid #4338ca", cursor: "pointer", fontSize: 12 },
  renderMenu: { position: "absolute" as const, top: "calc(100% + 6px)", right: 0, minWidth: 220, background: "#1a1a1a", border: "1px solid #333", borderRadius: 8, boxShadow: "0 6px 20px rgba(0,0,0,0.5)", padding: 6, display: "flex", flexDirection: "column" as const, gap: 2, zIndex: 50 },
  renderMenuItem: { display: "block", padding: "8px 12px", color: "#cbd5e1", fontSize: 13, textDecoration: "none" as const, borderRadius: 5, cursor: "pointer" },
  renderMenuItemBtn: { display: "block", width: "100%", padding: "8px 12px", color: "#cbd5e1", fontSize: 13, background: "transparent", border: "none", borderRadius: 5, cursor: "pointer", textAlign: "left" as const },
  renderMenuItemDisabled: { color: "#555", cursor: "not-allowed" as const },
  renderMenuHint: { padding: "6px 12px", color: "#666", fontSize: 11 },
  tagDictBtn: { padding: "6px 14px", background: "#1e1b4b", color: "#a78bfa", border: "1px solid #4338ca", borderRadius: 7, cursor: "pointer", fontSize: 12 },
  bgmEditBtn: { padding: "6px 14px", background: "#3a2a4a", color: "#a78bfa", border: "1px solid #4a3a5a", borderRadius: 7, cursor: "pointer", fontSize: 12 },
  bgmEditBtnDisabled: { background: "#1a1a1a", color: "#555", border: "1px solid #2a2a2a", cursor: "not-allowed" as const, opacity: 0.7 },
  // #294 — 보조 액션 회색 톤 통일 / 도움말 / 구분선 / 렌더 결과 카드 / 단축키 모달
  secondaryActionBtn: { padding: "6px 14px", background: "#1f1f1f", color: "#bbb", border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 12, whiteSpace: "nowrap" as const },
  helpBtn: { width: 28, height: 28, padding: 0, background: "#1f1f1f", color: "#888", border: "1px solid #333", borderRadius: "50%", cursor: "pointer", fontSize: 14, fontWeight: 700 },
  toolbarDivider: { width: 1, height: 22, background: "#333", margin: "0 2px" },
  renderResultCard: { padding: "10px 12px", background: "#0a1f0f", borderTop: "1px solid #166534", display: "flex", flexDirection: "column" as const, gap: 8 },
  renderResultRow: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" as const, fontSize: 12 },
  renderResultBadge: { color: "#4ade80", fontWeight: 700 },
  renderResultDur: { color: "#aaa" },
  renderResultPreviewBtn: { padding: "5px 12px", background: "#166534", color: "#dcfce7", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12 },
  renderResultDownload: { color: "#6366f1", fontWeight: 600, textDecoration: "none" as const },
  renderResultDownloadSub: { color: "#6366f1", textDecoration: "none" as const },
  renderResultVideo: { width: "100%", maxWidth: 480, maxHeight: 280, borderRadius: 8, background: "#000", alignSelf: "flex-start" },
  shortcutBackdrop: { position: "fixed" as const, inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 9000, display: "flex", alignItems: "center", justifyContent: "center", padding: 24 },
  shortcutModal: { background: "#1a1a1a", border: "1px solid #333", borderRadius: 12, padding: 24, width: "100%", maxWidth: 380, boxShadow: "0 10px 40px rgba(0,0,0,0.7)" },
  shortcutTitle: { color: "#fff", fontSize: 16, fontWeight: 700, marginBottom: 16 },
  shortcutList: { display: "flex", flexDirection: "column" as const, gap: 10 },
  shortcutRow: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 },
  shortcutKbd: { fontFamily: "monospace", fontSize: 12, color: "#a5b4fc", background: "#111", border: "1px solid #333", borderRadius: 6, padding: "4px 10px" },
  shortcutDesc: { color: "#ccc", fontSize: 13 },
  shortcutClose: { marginTop: 20, width: "100%", padding: "9px", background: "#333", color: "#ccc", border: "none", borderRadius: 8, fontSize: 14, cursor: "pointer" },
  previewPanel: { borderBottom: "1px solid #222", background: "#0a0a0a", flexShrink: 0 },
  previewDragHandle: { position: "absolute", bottom: 0, left: 0, right: 0, height: 6, cursor: "row-resize", background: "#2a2a2a", transition: "background 0.15s", zIndex: 10 },
  previewInner: { display: "flex", flexDirection: "column", height: "100%" },
  previewVideoWrap: { flex: 1, position: "relative", display: "flex", alignItems: "center", justifyContent: "center", minHeight: 0 },
  previewVideo: { maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" },
  previewEmpty: { display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#444", fontSize: 13 },
  previewMeta: { display: "flex", alignItems: "center", gap: 8, padding: "6px 14px", borderTop: "1px solid #1a1a1a", flexShrink: 0 },
  previewSceneNum: { background: "#333", color: "#aaa", borderRadius: 4, padding: "2px 7px", fontSize: 11, fontWeight: 700, flexShrink: 0 },
  previewEffectChip: { color: "#6366f1", fontSize: 11, background: "#1e1b4b", borderRadius: 4, padding: "2px 7px", flexShrink: 0 },
  previewDesc: { color: "#666", fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 },
  previewRefreshBtn: { padding: "3px 10px", background: "#1a1a2e", color: "#6366f1", border: "1px solid #4338ca", borderRadius: 5, cursor: "pointer", fontSize: 11, flexShrink: 0 },
  sceneList: { overflowY: "auto", flex: 1, padding: "12px 20px", display: "flex", flexDirection: "column", gap: 8 },
  sceneCard: { position: "relative", background: "#1a1a1a", borderRadius: 10, padding: "12px 14px", border: "2px solid transparent", cursor: "pointer", display: "flex", flexDirection: "column", gap: 8, transition: "border-color 0.15s" },
  sceneCardSelected: { borderColor: "#6366f1" },
  sceneCardMultiSelected: { borderColor: "#3b82f6", background: "#0f1a2e" },
  sceneCardOver: { borderColor: "#22c55e", background: "#0f2318" },
  // #316 — 자연어 편집 미리보기가 가리키는 씬. selected/multiSelected/over 와 동시 적용 가능하도록
  //         테두리 색만 보라색으로 덮어쓰고 박스 그림자로 강조한다.
  sceneCardNlHighlight: { borderColor: "#a78bfa", boxShadow: "0 0 0 2px rgba(167, 139, 250, 0.35)" },
  sceneTop: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" },
  sceneNum: { background: "#333", color: "#aaa", borderRadius: 4, padding: "2px 7px", fontSize: 12, fontWeight: 700, flexShrink: 0 },
  effectBadge: { border: "1px solid", borderRadius: 6, padding: "2px 8px", fontSize: 12, fontWeight: 600, cursor: "pointer", flexShrink: 0 },
  fileChip: { background: "#222", color: "#888", borderRadius: 4, padding: "2px 7px", fontSize: 11, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 160 },
  // #362 — 소스영상 변경 다이얼로그 트리거. fileChip 옆 작은 보조 버튼.
  changeFootageBtn: { background: "#111", color: "#9ca3af", border: "1px solid #333", borderRadius: 4, padding: "2px 7px", fontSize: 11, cursor: "pointer", flexShrink: 0 },
  ttsBtn: { marginLeft: "auto", width: 28, height: 28, borderRadius: 6, border: "1px solid #333", background: "#111", color: "#aaa", cursor: "pointer", fontSize: 12, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 },
  ttsBtnActive: { background: "#1e1b4b", borderColor: "#6366f1", color: "#6366f1" },
  ttsBtnLoading: { color: "#f59e0b", borderColor: "#f59e0b", animation: "spin 1s linear infinite" },
  ttsBtnUnsynced: { color: "#f59e0b", borderColor: "#f59e0b", background: "#1c1500" },
  narrationInput: { background: "#111", border: "1px solid #2a2a2a", borderRadius: 6, color: "#ccc", fontSize: 13, padding: "8px 10px", resize: "none", fontFamily: "inherit", width: "100%", boxSizing: "border-box" },
  subtitleBox: { display: "flex", flexDirection: "column" as const, gap: 4, marginTop: 2 },
  subtitleLabel: { display: "flex", alignItems: "center", justifyContent: "space-between", color: "#888", fontSize: 11 },
  subtitleToggleBtn: { padding: "2px 8px", background: "transparent", color: "#666", border: "1px dashed #333", borderRadius: 4, fontSize: 11, cursor: "pointer" },
  subtitleResetBtn: { padding: "2px 8px", background: "transparent", color: "#888", border: "1px solid #333", borderRadius: 4, fontSize: 11, cursor: "pointer" },
  aiPlaceholder: { background: "#1c1a00", border: "1px solid #3a3000", color: "#f59e0b", borderRadius: 6, padding: "6px 10px", fontSize: 12 },
  dropOverlay: { position: "absolute", inset: 0, background: "rgba(34,197,94,0.15)", borderRadius: 10, display: "flex", alignItems: "center", justifyContent: "center", color: "#22c55e", fontWeight: 700, pointerEvents: "none" },
  addOverlayBtn: { padding: "2px 8px", background: "#111", color: "#6366f1", border: "1px solid #6366f1", borderRadius: 5, cursor: "pointer", fontSize: 11 },
  overlayClipBtn: { padding: "2px 8px", background: "#111", color: "#888", border: "1px solid #444", borderRadius: 5, cursor: "pointer", fontSize: 11 },
  overlayChip: { background: "#1e1b4b", color: "#818cf8", border: "1px solid #4338ca", borderRadius: 5, padding: "3px 6px", fontSize: 11, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 },
  overlayChipDel: { color: "#f87171", cursor: "pointer", fontSize: 16, fontWeight: 700, lineHeight: 1, padding: "2px 5px", borderRadius: 4, display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 22, minHeight: 22 },
  deleteSceneBtn: { width: 28, height: 28, borderRadius: 6, border: "1px solid #444", background: "#111", color: "#f87171", cursor: "pointer", fontSize: 16, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, lineHeight: 1 },
  duplicateSceneBtn: { width: 28, height: 28, borderRadius: 6, border: "1px solid #444", background: "#111", color: "#6ee7b7", cursor: "pointer", fontSize: 14, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, lineHeight: 1 },
  // 씬 붙여넣기 버튼 — 복사된 씬이 있을 때만 노출. 보라색 계열로 "씬 복사"와 시각 구분.
  pasteSceneBtn: { width: 28, height: 28, borderRadius: 6, border: "1px solid #6366f1", background: "#1e1b4b", color: "#c7d2fe", cursor: "pointer", fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, lineHeight: 1 },
  splitSceneBtn: { width: 28, height: 28, borderRadius: 6, border: "1px solid #444", background: "#111", color: "#fbbf24", cursor: "pointer", fontSize: 14, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, lineHeight: 1 },
  autoFitSpeedBtn: { width: 28, height: 28, borderRadius: 6, border: "1px solid #444", background: "#111", color: "#38bdf8", cursor: "pointer", fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, lineHeight: 1 },
  bulkDeleteBtn: { padding: "6px 14px", background: "#1f0a0a", color: "#f87171", border: "1px solid #7f1d1d", borderRadius: 7, cursor: "pointer", fontSize: 12 },
  // #245 — 씬 사이 인서트 핸들 (평소 얇고 투명, hover 시 진해짐)
  sceneInsertHandle: { height: 8, margin: "-4px 0", borderRadius: 4, display: "flex", alignItems: "center", justifyContent: "center", gap: 10, cursor: "pointer", transition: "all 0.15s", opacity: 0.3 },
  sceneInsertHandleHover: { height: 28, margin: "2px 0", background: "#1a1a2e", border: "1px dashed #4338ca", opacity: 1 },
  sceneInsertPlus: { color: "transparent", fontSize: 11, fontWeight: 600, userSelect: "none" },
  sceneInsertPlusHover: { color: "#818cf8", fontSize: 12, fontWeight: 600, userSelect: "none" },
  // 클립 라이브러리 삽입 — 기본 삽입과 동일하게 hover 시에만 보인다.
  sceneInsertLib: { color: "transparent", fontSize: 11, fontWeight: 600, userSelect: "none" },
  sceneInsertLibHover: { color: "#34d399", fontSize: 12, fontWeight: 600, userSelect: "none" },
  // #245 — 씬 리스트 끝의 + 씬 추가 버튼 (큰 dashed border)
  addSceneBtn: { marginTop: 12, padding: "16px", background: "transparent", color: "#6366f1", border: "2px dashed #4338ca", borderRadius: 10, cursor: "pointer", fontSize: 14, fontWeight: 600, transition: "all 0.15s" },
  deleteAllScenesBtn: { padding: "6px 14px", background: "#1f0a0a", color: "#f87171", border: "1px solid #7f1d1d", borderRadius: 7, cursor: "pointer", fontSize: 12 },
  folderPermBtn: { margin: "8px 12px", padding: "10px 12px", background: "#1e1b4b", color: "#a78bfa", border: "1px dashed #4338ca", borderRadius: 7, cursor: "pointer", fontSize: 12, fontWeight: 600 },
};

// ── #382 — NarrationSegmentInspector ───────────────────────────────────
// TimelineBar 의 Alt/Shift+클릭(추가) 또는 마커 클릭(편집) 시 띄우는 작은 패널.
// 추가/편집/삭제/TTS 재생성 동작은 모두 백엔드 API 를 호출하고 onChanged 콜백으로
// 갱신된 segments 배열을 상위에 전달한다. 인스펙터는 항상 영구 표시(close 버튼 누를 때까지)
// 되어 사용자가 텍스트 수정 후 즉시 [TTS 재생성]을 누를 수 있다.
import { computeSceneVisualDuration as _computeSceneVisualDuration, isSegmentOverflow as _isSegmentOverflow } from "../lib/project";
import {
  createNarrationSegment as _createSegment,
  updateNarrationSegment as _updateSegment,
  deleteNarrationSegment as _deleteSegment,
  generateNarrationSegmentTts as _generateSegmentTts,
} from "../api/client";
import type { NarrationSegment } from "../types";

interface NarrationSegmentInspectorProps {
  projectId: string;
  scene: SceneCard;
  pendingAdd: { sceneId: number; offsetSec: number } | null;
  editing: { sceneId: number; segId: string } | null;
  onClose: () => void;
  onChanged: (next: NarrationSegment[]) => void | Promise<void>;
  /** #388 — TimelineBar 와 동일한 길이 계산을 위해 footage 원본 목록을 함께 받는다.
   *  (clip_end_sec=null 인 trim 미설정 씬에서 footage 원본 duration 으로 폴백) */
  footageItems?: ReadonlyArray<import("../types").FootageItem>;
  /** #388 — TimelineBar 와 동일한 길이 계산을 위해 Phase 5 cut 캐시도 함께 받는다.
   *  (인스펙터의 "씬 길이 ≈" 가 TimelineBar 의 씬 블록 폭과 일치하도록.) */
  cutDurations?: ReadonlyMap<number, number>;
}

function NarrationSegmentInspector({
  projectId,
  scene,
  pendingAdd,
  editing,
  onClose,
  onChanged,
  footageItems,
  cutDurations,
}: NarrationSegmentInspectorProps) {
  const segs = scene.narration_segments ?? [];
  // #388 — footage trim / cut 캐시 까지 반영해 TimelineBar 씬 블록 폭과 항상 일치.
  //   기존엔 TTS/hint 만 봐서 footage 트림 3.1s 인 씬에 "씬 길이 ≈ 5.00s" 가 표시되는 버그.
  const sceneDur = _computeSceneVisualDuration(scene, footageItems, cutDurations);

  // 편집 대상 / 추가 신규 — 두 경로의 폼 상태를 분리해서 보관.
  const targetSeg = editing ? segs.find((s) => s.id === editing.segId) ?? null : null;
  const [draftText, setDraftText] = useState<string>(targetSeg?.text ?? "");
  const [draftOffset, setDraftOffset] = useState<number>(
    targetSeg?.offset_sec ?? (pendingAdd?.offsetSec ?? 0),
  );
  const [draftVoice, setDraftVoice] = useState<string>(targetSeg?.voice_name ?? "");
  // segment override 목소리 드롭다운도 Google+Typecast 병합 목록 사용.
  //   draftVoice(현재 override 값)가 로드 전 tc_ 값이면 임시 option 으로 유지.
  const { voices: segmentVoices } = useVoiceOptions(draftVoice || null);
  const { voices: segTypecastRaw } = useTypecastRawVoices();
  const [segFindOpen, setSegFindOpen] = useState(false);
  const segTypecastName =
    resolveProvider(draftVoice) === "typecast"
      ? segTypecastRaw.find((t) => t.voice_id === draftVoice)?.voice_name || draftVoice
      : null;
  const [busy, setBusy] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  // 편집 대상이 바뀌면 폼 리셋
  useEffect(() => {
    if (editing && targetSeg) {
      setDraftText(targetSeg.text);
      setDraftOffset(targetSeg.offset_sec ?? 0);
      setDraftVoice(targetSeg.voice_name ?? "");
    } else if (pendingAdd) {
      setDraftText("");
      setDraftOffset(pendingAdd.offsetSec);
      setDraftVoice("");
    }
    setErrMsg(null);
  }, [editing?.segId, pendingAdd?.offsetSec, targetSeg]);

  const overflowWarn = targetSeg
    ? _isSegmentOverflow(targetSeg, sceneDur)
    : pendingAdd
    ? pendingAdd.offsetSec > sceneDur
    : false;

  const handleAdd = useCallback(async () => {
    if (!pendingAdd || !draftText.trim()) return;
    setBusy(true);
    setErrMsg(null);
    try {
      const created = await _createSegment(projectId, pendingAdd.sceneId, {
        text: draftText.trim(),
        offset_sec: draftOffset,
        voice_name: draftVoice.trim() || null,
      });
      // segments 리스트를 즉시 반영 후 TTS 생성 트리거.
      const next = [...(scene.narration_segments ?? []), created];
      await onChanged(next);
      // TTS 생성 백그라운드 트리거 — WebSocket tts_done 이 결과 알림.
      await _generateSegmentTts(projectId, pendingAdd.sceneId, created.id, { force: true });
    } catch (e) {
      setErrMsg(`segment 추가 실패: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [pendingAdd, draftText, draftOffset, draftVoice, projectId, scene, onChanged]);

  const handleUpdate = useCallback(async () => {
    if (!editing || !targetSeg) return;
    setBusy(true);
    setErrMsg(null);
    try {
      const updated = await _updateSegment(projectId, editing.sceneId, editing.segId, {
        text: draftText,
        offset_sec: draftOffset,
        voice_name: draftVoice.trim() || null,
      });
      const next = (scene.narration_segments ?? []).map((s) =>
        s.id === editing.segId ? updated : s,
      );
      await onChanged(next);
    } catch (e) {
      setErrMsg(`segment 편집 실패: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [editing, targetSeg, draftText, draftOffset, draftVoice, projectId, scene, onChanged]);

  const handleRegenerate = useCallback(async () => {
    if (!editing) return;
    setBusy(true);
    setErrMsg(null);
    try {
      // 폼 변경분 먼저 저장.
      if (
        targetSeg &&
        (targetSeg.text !== draftText
          || (targetSeg.offset_sec ?? 0) !== draftOffset
          || (targetSeg.voice_name ?? "") !== draftVoice)
      ) {
        await handleUpdate();
      }
      await _generateSegmentTts(projectId, editing.sceneId, editing.segId, { force: true });
    } catch (e) {
      setErrMsg(`TTS 재생성 실패: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [editing, targetSeg, draftText, draftOffset, draftVoice, projectId, handleUpdate]);

  const handleDelete = useCallback(async () => {
    if (!editing) return;
    if (!confirm(`segment "${editing.segId}" 를 삭제하시겠어요?`)) return;
    setBusy(true);
    setErrMsg(null);
    try {
      await _deleteSegment(projectId, editing.sceneId, editing.segId);
      const next = (scene.narration_segments ?? []).filter((s) => s.id !== editing.segId);
      await onChanged(next);
      onClose();
    } catch (e) {
      setErrMsg(`segment 삭제 실패: ${String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [editing, projectId, scene, onChanged, onClose]);

  const isAddMode = !!pendingAdd && !editing;

  return (
    <div
      style={{
        background: "#0f0f17",
        borderTop: "1px solid #2a2a3a",
        borderBottom: "1px solid #2a2a3a",
        padding: "10px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        fontFamily: "system-ui, sans-serif",
        fontSize: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ color: "#a78bfa", fontWeight: 700 }}>
          {isAddMode
            ? `씬 ${scene.scene_id} · 새 TTS 구간 추가 (offset ${pendingAdd!.offsetSec.toFixed(2)}s)`
            : `씬 ${scene.scene_id} · segment 편집 — ${editing?.segId}`}
        </span>
        <span style={{ color: "#666", marginLeft: 6 }}>
          씬 길이 ≈ {sceneDur.toFixed(2)}s
        </span>
        <button
          type="button"
          onClick={onClose}
          style={{
            marginLeft: "auto",
            background: "#222",
            color: "#bbb",
            border: "1px solid #444",
            borderRadius: 5,
            padding: "3px 10px",
            cursor: "pointer",
          }}
        >
          닫기
        </button>
      </div>
      {overflowWarn && (
        <div
          style={{
            color: "#ef4444",
            background: "#2a0f0f",
            border: "1px solid #7f1d1d",
            borderRadius: 5,
            padding: "4px 8px",
          }}
        >
          ⚠ 이 segment 는 씬 길이를 초과합니다. 렌더 시 씬 경계에서 잘립니다.
        </div>
      )}
      {/* #392 — segment.status === "error" 면 TTS 실패 메시지를 빨갛게 표시.
          백엔드 _do_generate_segment 의 except 가 error_message 에 traceback 요약을
          저장한다. [재생성] 버튼으로 같은 흐름을 다시 트리거할 수 있다. */}
      {targetSeg && targetSeg.status === "error" && (
        <div
          style={{
            color: "#fecaca",
            background: "#2a0f0f",
            border: "1px solid #b91c1c",
            borderRadius: 5,
            padding: "6px 10px",
            display: "flex",
            flexDirection: "column",
            gap: 3,
          }}
          role="alert"
        >
          <div style={{ fontWeight: 700, color: "#ef4444" }}>
            ⛔ TTS 생성 실패
          </div>
          <div style={{ fontFamily: "ui-monospace, monospace", fontSize: 11, whiteSpace: "pre-wrap" as const }}>
            {targetSeg.error_message || "원인 불명 — 백엔드 로그를 확인하세요."}
          </div>
          <div style={{ color: "#fbbf24", fontSize: 11 }}>
            텍스트/보이스를 확인한 뒤 아래 [🔄 TTS 재생성] 버튼을 눌러주세요.
          </div>
        </div>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ color: "#aaa", display: "flex", flexDirection: "column", gap: 2, flex: "1 1 380px" }}>
          텍스트
          <textarea
            value={draftText}
            onChange={(e) => setDraftText(e.target.value)}
            rows={2}
            style={{
              background: "#0a0a0f",
              color: "#eee",
              border: "1px solid #2a2a3a",
              borderRadius: 5,
              padding: "6px 8px",
              fontSize: 12,
              resize: "vertical",
              minHeight: 36,
            }}
            placeholder="이 segment 에서 발화할 텍스트"
            disabled={busy}
          />
        </label>
        <label style={{ color: "#aaa", display: "flex", flexDirection: "column", gap: 2 }}>
          offset_sec
          <input
            type="number"
            step={0.1}
            min={0}
            value={draftOffset}
            onChange={(e) => setDraftOffset(Math.max(0, parseFloat(e.target.value) || 0))}
            style={{
              width: 90,
              background: "#0a0a0f",
              color: "#eee",
              border: "1px solid #2a2a3a",
              borderRadius: 5,
              padding: "6px 8px",
              fontSize: 12,
            }}
            disabled={busy}
          />
        </label>
        <label style={{ color: "#aaa", display: "flex", flexDirection: "column", gap: 2 }}>
          voice (선택)
          <select
            value={draftVoice}
            onChange={(e) => setDraftVoice(e.target.value)}
            title="비우면 프로젝트 기본 목소리 사용"
            style={{
              width: 220,
              background: "#0a0a0f",
              color: "#eee",
              border: "1px solid #2a2a3a",
              borderRadius: 5,
              padding: "6px 8px",
              fontSize: 12,
            }}
            disabled={busy}
          >
            <option value="">(프로젝트 기본)</option>
            <VoiceOptions voices={segmentVoices} />
          </select>
          <div style={{ display: "flex", gap: 6, marginTop: 4, alignItems: "center" }}>
            <button
              type="button"
              onClick={() => setSegFindOpen(true)}
              disabled={busy}
              style={{
                padding: "4px 8px", background: "#1a1a24", color: "#a5b4fc",
                border: "1px solid #2a2a3a", borderRadius: 5, fontSize: 11, cursor: "pointer",
                whiteSpace: "nowrap",
              }}
            >
              🔍 Typecast 찾기
            </button>
            {segTypecastName && (
              <span style={{ fontSize: 11, color: "#a5b4fc" }}>🎙 {segTypecastName}</span>
            )}
          </div>
        </label>
      </div>
      {segFindOpen && (
        <TypecastVoiceModal
          currentVoiceId={draftVoice || null}
          onClose={() => setSegFindOpen(false)}
          onSelect={(voiceId) => {
            setDraftVoice(voiceId);
            setSegFindOpen(false);
          }}
        />
      )}
      <div style={{ display: "flex", gap: 8 }}>
        {isAddMode && (
          <button
            type="button"
            onClick={handleAdd}
            disabled={busy || !draftText.trim()}
            style={{
              background: "#4338ca",
              color: "#fff",
              border: "none",
              borderRadius: 5,
              padding: "6px 12px",
              cursor: busy ? "wait" : "pointer",
              opacity: busy || !draftText.trim() ? 0.6 : 1,
            }}
          >
            ➕ 추가 + TTS 생성
          </button>
        )}
        {!isAddMode && editing && (
          <>
            <button
              type="button"
              onClick={handleUpdate}
              disabled={busy}
              style={{
                background: "#2a2a3a",
                color: "#eee",
                border: "1px solid #4338ca",
                borderRadius: 5,
                padding: "6px 12px",
                cursor: busy ? "wait" : "pointer",
              }}
            >
              💾 저장
            </button>
            <button
              type="button"
              onClick={handleRegenerate}
              disabled={busy}
              style={{
                background: "#4338ca",
                color: "#fff",
                border: "none",
                borderRadius: 5,
                padding: "6px 12px",
                cursor: busy ? "wait" : "pointer",
              }}
            >
              🔄 TTS 재생성
            </button>
            <button
              type="button"
              onClick={handleDelete}
              disabled={busy}
              style={{
                background: "#1f0a0a",
                color: "#f87171",
                border: "1px solid #7f1d1d",
                borderRadius: 5,
                padding: "6px 12px",
                cursor: busy ? "wait" : "pointer",
                marginLeft: "auto",
              }}
            >
              🗑 삭제
            </button>
          </>
        )}
      </div>
      {errMsg && <div style={{ color: "#f87171" }}>{errMsg}</div>}
    </div>
  );
}

// ── TTS 생성 시작 모달 ────────────────────────────────────────────────
// '♪ TTS 생성 (N개)' 클릭 시 목소리 확정 + 미리듣기 후 시작.
//   씬/수동 배치는 절대 건드리지 않는다 — 이 모달은 오디오 파일만 새로 만든다.
function TtsStartModal({
  initialVoice,
  missingCount,
  onCancel,
  onStart,
}: {
  initialVoice: string;
  missingCount: number;
  onCancel: () => void;
  onStart: (voice: string) => void | Promise<void>;
}) {
  const [voice, setVoice] = useState(initialVoice);
  const [findOpen, setFindOpen] = useState(false);
  const preview = useTtsPreview();
  const { voices: mergedVoices } = useVoiceOptions(voice);
  const { voices: typecastRaw } = useTypecastRawVoices();
  const playing = preview.playingVoice === voice;
  const selectedTypecastName =
    resolveProvider(voice) === "typecast"
      ? typecastRaw.find((t) => t.voice_id === voice)?.voice_name || voice
      : null;
  return (
    <div
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)",
        display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9500,
      }}
      onClick={onCancel}
    >
      <div
        style={{
          background: "#1a1a1a", border: "1px solid #333", borderRadius: 12,
          width: 460, maxWidth: "92vw", padding: 24, color: "#e5e5e5",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ margin: 0, fontSize: 18 }}>TTS 생성 시작</h3>
        <p style={{ marginTop: 8, color: "#aaa", fontSize: 13 }}>
          아직 생성되지 않은 씬 <b>{missingCount}개</b> 에 대해 나레이션 오디오를 만듭니다.
          목소리를 확정하고 [생성 시작] 을 누르세요.
        </p>
        <label style={{ display: "block", marginTop: 14, fontSize: 12, color: "#aaa" }}>목소리</label>
        <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
          <select
            value={voice}
            onChange={(e) => setVoice(e.target.value)}
            style={{
              flex: 1, padding: "8px 12px", background: "#111", color: "#fff",
              border: "1px solid #333", borderRadius: 7, fontSize: 13,
            }}
          >
            <VoiceOptions voices={mergedVoices} />
          </select>
          <button
            type="button"
            onClick={() => preview.preview(voice)}
            disabled={preview.loading}
            title={playing ? "미리듣기 정지" : "선택한 목소리로 샘플 재생"}
            style={{
              padding: "8px 14px", background: "#252525", color: "#a5b4fc",
              border: "1px solid #333", borderRadius: 7, fontSize: 13, cursor: "pointer",
              whiteSpace: "nowrap", flexShrink: 0,
            }}
          >
            {preview.loading ? "…" : playing ? "■ 정지" : "▶ 미리듣기"}
          </button>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
          <button
            type="button"
            onClick={() => setFindOpen(true)}
            style={{
              padding: "7px 12px", background: "#252525", color: "#a5b4fc",
              border: "1px solid #333", borderRadius: 7, fontSize: 12, cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            🔍 Typecast 찾기
          </button>
          {selectedTypecastName && (
            <span style={{ fontSize: 12, color: "#a5b4fc" }}>🎙 {selectedTypecastName}</span>
          )}
        </div>
        {findOpen && (
          <TypecastVoiceModal
            currentVoiceId={voice}
            onClose={() => setFindOpen(false)}
            onSelect={(voiceId) => {
              setVoice(voiceId);
              setFindOpen(false);
            }}
          />
        )}
        {preview.error && (
          <p style={{ marginTop: 8, color: "#f87171", fontSize: 12 }}>미리듣기 실패: {preview.error}</p>
        )}
        <div style={{ display: "flex", gap: 8, marginTop: 20, justifyContent: "flex-end" }}>
          <button
            type="button"
            onClick={onCancel}
            style={{
              padding: "9px 18px", background: "#252525", color: "#aaa",
              border: "1px solid #333", borderRadius: 7, fontSize: 13, cursor: "pointer",
            }}
          >
            취소
          </button>
          <button
            type="button"
            onClick={() => onStart(voice)}
            style={{
              padding: "9px 18px", background: "#166534", color: "#4ade80",
              border: "1px solid #22c55e", borderRadius: 7, fontSize: 13, cursor: "pointer", fontWeight: 600,
            }}
          >
            ♪ 생성 시작
          </button>
        </div>
      </div>
    </div>
  );
}
