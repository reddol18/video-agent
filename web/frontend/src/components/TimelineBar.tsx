/**
 * TimelineBar (#360 / #364) — Editor 상단의 전체 영상 타임라인 시각화.
 *
 * 씬을 길이 비례 색 블록으로 나란히 그린다. 클릭 시 selectScene 으로 해당 씬을 선택한다.
 * 블록 위에 씬 번호 + 짧은 라벨, hover 시 narration 미리보기 (title 속성으로 OS 툴팁).
 *
 * 구현 범위:
 *  - 씬 길이 비례 블록 + 씬 종류(영상/이미지/title_gen 등)별 색
 *  - 블록 클릭 → selectScene + 부드러운 스크롤 to 씬 카드
 *  - 선택된 씬 강조 테두리
 *  - BGM 트랙 1줄 표시 (start_sec~end_sec 비율로)
 *  - #364 — playhead 빨간 세로선 + 드래그/클릭 seek (씬 단위 점프 + 씬 안의 sec 위치까지).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { SceneCard, BgmTrack, FootageItem, NarrationSegment } from "../types";
import { useStore } from "../store";
import { isSegmentOverflow, computeSceneVisualDuration } from "../lib/project";

/** #narrow-scene-block — 씬 번호를 그리기 위해 필요한 최소 블록 폭(px).
 *  sceneBlock 의 좌우 padding(4+4) + border(1+1) = 10px 가 내용 전에 소비되므로,
 *  두 자리 숫자(≈12px)를 담으려면 최소 이 정도는 있어야 한다. 이보다 좁으면
 *  숫자가 블록 밖으로 흘러 이웃 씬과 겹쳐 보인다. */
const NUM_MIN_PX = 22;

/** #narrow-scene-block — 이웃 씬 블록 사이에 두는 시각적 간격(px).
 *  블록은 left/width 로 정확히 맞닿게 배치되므로, 이 간격이 없으면 양쪽의
 *  1px 테두리와 둥근 모서리가 같은 픽셀에 겹쳐 두 씬이 하나로 뭉쳐 보인다. */
const SCENE_GAP_PX = 2;

/** 간격을 뺀 뒤에도 보장할 최소 블록 폭(px). 아주 짧은 씬이 사라지지 않도록. */
const MIN_BLOCK_PX = 3;

/** #timeline-zoom — 타임라인 가로 배율. 1 = 화면 폭에 딱 맞춤(기존 동작). */
const ZOOM_MIN = 1;
const ZOOM_MAX = 16;
const ZOOM_STEP = 1.5;
export function clampZoom(z: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
}

/** 씬 블록의 실제 픽셀 폭.
 *
 *  `trackPx` 는 스크롤 영역의 보이는 폭(zoom 과 무관하게 일정)이고,
 *  실제 트랙 폭은 그 zoom 배다. ResizeObserver 콜백에만 의존하면 확대 직후
 *  한 프레임 동안 옛 값이 남아 씬 번호가 사라져 보이므로, zoom 을 곱해
 *  렌더 시점에 직접 계산한다. */
export function blockPx(trackPx: number, zoom: number, widthPct: number): number {
  return trackPx * zoom * (widthPct / 100);
}

const EFFECT_COLORS: Record<string, string> = {
  clip: "#3b82f6",
  kenburns: "#8b5cf6",
  kenburns_copy_img: "#7c3aed",
  edit: "#f59e0b",
  sync: "#ef4444",
  multiscreen: "#06b6d4",
  title_gen: "#10b981",
};

/**
 * 씬의 "추정 길이(초)" 를 반환한다. (#388 — `computeSceneVisualDuration` 으로 통합)
 *
 * 옛 `getSceneDurationSec` 와 인스펙터의 `computeSceneVisualDuration` 이 서로 다른 값을
 * 반환해 (#388) 사용자 화면 3 곳이 모두 어긋났던 회귀를 해소하기 위해, 두 함수를
 * 같은 구현(=lib/project.ts 의 `computeSceneVisualDuration`)으로 합쳤다.
 *
 * 우선순위 (#375/#378 → #394 에서 ③ 제거 → #397 로 복원):
 *  ① cache/timeline.json cut 길이 (`cutDurations` 가 있고 `footage_speed === 1.0`).
 *  ② footage(clip).clip_end_sec - clip_start_sec  (footage trim / footage_speed). TTS/hint 와 max.
 *  ③ footage(clip) 원본 duration — clip_end_sec=null + footageItems 제공 시 사용.
 *     사용자 의도("16번 씬이 5초라면 잘못된 거야. 34.8초가 맞아.")에 따라 #397 에서 복원.
 *     TTS/hint 는 footage 원본보다 길 때만 footage 를 연장(Phase 5 Time Warp)한다.
 *  ④ max(TTS seg_0 또는 scene.tts, duration_hint_sec, 1.0)  — image/kenburns/footage 부재 시 폴백.
 *
 * 회귀 안전: 옛 인터페이스(시그니처/반환값 의미)와 동일. 단위 테스트(#375/#378) 그대로 통과.
 *
 * 단, 이 함수는 폴백 시 `pickMax(...)` 가 0 이면 `hint || 3.0` 으로 떨어뜨렸지만,
 * `computeSceneVisualDuration` 은 ② 분기에서 trim 값이 양수이면 `Math.max(trim, tts0, hint)` 만
 * 반환한다(`|| hintSec || 3.0` 폴백 없음). 양수 trim 이 있는 한 결과는 동일하므로 회귀 없음.
 * (trim 이 0 이거나 음수면 ③/④ 로 자연 폴백.)
 */
/** #timeline-dots — 이 씬에 TTS 로 읽을 "대본"이 있는가.
 *  음성 생성 여부가 아니라 텍스트 유무로 판정한다 — 아직 안 만든 씬도
 *  "여기 나레이션이 있다" 를 타임라인에서 바로 보려는 것이 목적. */
function hasTts(scene: SceneCard): boolean {
  if ((scene.narration_text ?? "").trim()) return true;
  return (scene.narration_segments ?? []).some((s) => (s.text ?? "").trim());
}

/** #timeline-dots — 이 씬에 달린 편집 코멘트 수(완료된 것 포함).
 *  완료해도 "이 씬은 손을 본 곳" 이라는 이력이 남아야 하므로 status 로 거르지 않는다. */
function openComments(scene: SceneCard): number {
  return (scene.comments ?? []).length;
}

export function getSceneDurationSec(
  scene: SceneCard,
  footageItems?: ReadonlyArray<FootageItem> | Map<string, FootageItem>,
  cutDurations?: ReadonlyMap<number, number>,
): number {
  // 통합 함수 본체 — 이슈 #388 우선순위(① cut → ② trim → ③ 원본 → ④ max(TTS,hint,1.0)).
  const dur = computeSceneVisualDuration(scene, footageItems, cutDurations);
  // TimelineBar 의 "씬 블록 시각화" 정책상 완전 빈 씬 폭이 3.0 으로 그려지는 옛 동작 유지.
  //  (관련 테스트: "아무 것도 없음 → 3.0 폴백")
  //  단, cut/footage/TTS/hint 중 하나라도 결과 1.0 보다 큰 값을 만들었다면 dur 가 그 값이라 그대로 신뢰.
  //  진짜 "빈 씬" 만 1.0 → 3.0 으로 보정.
  if (dur <= 1.0) {
    const hasCut = !!(cutDurations && (cutDurations.get(scene.scene_id) ?? 0) > 0);
    const hasFootageInfo = !!scene.footage;
    const hint = (scene.duration_hint_sec ?? 0) > 0;
    const seg0Dur = (scene.narration_segments ?? []).find((s) => s.id === "seg_0")?.duration_sec ?? 0;
    const legacyTts = (scene.tts?.status === "ready" && (scene.tts?.duration_sec ?? 0) > 0)
      ? (scene.tts!.duration_sec as number) : 0;
    const ttsAny = seg0Dur > 0 || legacyTts > 0;
    if (!hasCut && !hasFootageInfo && !hint && !ttsAny) {
      return 3.0;
    }
  }
  return dur;
}

/** 씬 카드(DOM)로 부드럽게 스크롤. data-scene-id 속성을 가진 첫 요소를 찾는다. */
function scrollToSceneCard(sceneId: number): void {
  if (typeof document === "undefined") return;
  const el = document.querySelector(`[data-scene-id="${sceneId}"]`) as HTMLElement | null;
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

export interface TimelineBarProps {
  scenes: SceneCard[];
  selectedSceneId: number | null;
  onSelectScene: (sceneId: number) => void;
  /** 후처리 BGM 트랙(옵셔널). 영상 타임라인에 상대 위치로 표시. */
  bgmTracks?: BgmTrack[];
  /** #375 — footage 원본 duration 조회용. clip_end_sec 가 null 인 trim 미설정 씬의 길이 계산에 필요. */
  footageItems?: ReadonlyArray<FootageItem>;
  /** #364 — 현재 선택 씬의 video.currentTime (씬 로컬 sec).
   *  내부에서 (선택 씬의 누적 시작 sec) + 이 값 = 글로벌 sec 로 환산해 빨간 세로선 위치를 결정.
   *  #371: prop 미전달 시 TimelineBar 내부에서 zustand selector 로 직접 구독한다.
   *        Editor 전체가 ~4Hz 로 리렌더되지 않도록 분리하는 것이 목적.
   *        명시적으로 prop 으로 받고 싶으면 그대로 가능(테스트/리허설용). */
  previewCurrentSec?: number;
  /** #364 — 사용자가 timeline 의 특정 글로벌 sec 로 점프 요청.
   *  TimelineBar 가 그 sec 이 속한 씬을 자동 계산해 (sceneId, sceneLocalSec) 를 콜백으로 넘긴다.
   *  부모는 sceneId 가 현재 선택 씬과 다르면 selectScene + (PreviewPanel 재마운트 후) seek 요청,
   *  같으면 그냥 seek 요청만 하면 된다. */
  onSeekToGlobalSec?: (sceneId: number, sceneLocalSec: number, globalSec: number) => void;
  /** #382 — TimelineBar 의 씬 영역을 alt/shift+click 하면 새 segment 추가 후보 위치(offset_sec)를 전달.
   *  부모(Editor) 가 이 콜백을 받아 인스펙터에 "TTS 구간 추가" 폼을 띄운다. 미전달 시 마커만 시각화. */
  onAddNarrationSegment?: (sceneId: number, offsetSecInScene: number) => void;
  /** #382 — 기존 segment 마커 클릭 시 호출. 부모가 인스펙터/모달로 편집 UI 열기. */
  onEditNarrationSegment?: (sceneId: number, segId: string) => void;
}

export default function TimelineBar({
  scenes,
  selectedSceneId,
  onSelectScene,
  bgmTracks,
  footageItems,
  previewCurrentSec: previewCurrentSecProp,
  onSeekToGlobalSec,
  onAddNarrationSegment,
  onEditNarrationSegment,
}: TimelineBarProps) {
  // #375 — filename 인덱스를 한 번만 구성해 다수 씬에 재사용.
  const footageIndex = useMemo(() => {
    const m = new Map<string, FootageItem>();
    if (footageItems) {
      for (const it of footageItems) m.set(it.filename, it);
    }
    return m;
  }, [footageItems]);
  // #371 — prop 우선, 미전달 시 store selector 로 직접 구독해 Editor 본체 리렌더를 차단.
  //         selector 패턴은 같은 값이면 set 호출이 와도 컴포넌트 리렌더가 생략된다.
  const previewCurrentSecFromStore = useStore((s) => s.previewCurrentTime);
  const previewCurrentSec =
    previewCurrentSecProp !== undefined ? previewCurrentSecProp : previewCurrentSecFromStore;
  // #378 — Phase 5 산출 cut 길이를 store 에서 직접 구독.
  //   Editor 에서 prop 으로 받지 않아 변경 부담을 최소화한다.
  //   부재(=Phase 5 미실행)이면 빈 Map 이며 getSceneDurationSec 가 자연 폴백.
  const sceneCutDurations = useStore((s) => s.sceneCutDurations);
  // 빈 프로젝트는 안내 띠만 보여 사용자 혼란을 막는다.
  if (!scenes || scenes.length === 0) {
    return (
      <div style={styles.empty}>
        <span style={styles.emptyText}>씬이 없습니다 — 시나리오를 파싱하면 타임라인이 표시됩니다</span>
      </div>
    );
  }

  // 정렬된 씬 순서로 그린다. (Editor 에서 이미 정렬해 전달하지만 방어적으로 한 번 더.)
  const sorted = scenes.slice().sort((a, b) => a.scene_id - b.scene_id);
  // #375/#378 — ① cache/timeline.json cut 길이 → ② footage trim → ③ footage 원본 → TTS/hint/3.0.
  const durations = sorted.map((s) => getSceneDurationSec(s, footageIndex, sceneCutDurations));
  const totalDur = durations.reduce((acc, d) => acc + d, 0);

  // 누적 시작 시각 — BGM 트랙 위치 + playhead 글로벌 sec 계산에 사용.
  const startsSec: number[] = [];
  {
    let acc = 0;
    for (const d of durations) {
      startsSec.push(acc);
      acc += d;
    }
  }

  const formatDur = (s: number) => (s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`);

  // ── #364 playhead ────────────────────────────────────────────────────────
  // 글로벌 sec = (선택 씬의 누적 시작 sec) + previewCurrentSec.
  // 선택 씬이 없거나 sorted 에 없으면 0 으로 폴백.
  //
  // #391 — previewCurrentSec 이 선택 씬의 실제 길이를 초과하면 playhead 가 다음 씬에
  //        박혀 사용자가 "다른 씬에 박혔다" 고 인식한다. 선택 씬 내부로 클램프해서
  //        시각적 일관성을 보장한다 (실제 미리보기는 PreviewPanel 이 자체 처리).
  const selectedIdx = selectedSceneId != null ? sorted.findIndex((s) => s.scene_id === selectedSceneId) : -1;
  const sceneStartGlobalSec = selectedIdx >= 0 ? (startsSec[selectedIdx] ?? 0) : 0;
  const selectedSceneDur = selectedIdx >= 0 ? (durations[selectedIdx] ?? 0) : 0;
  const playheadLocalSec = Math.max(
    0,
    Math.min(selectedSceneDur, previewCurrentSec ?? 0),
  );
  const playheadGlobalSec = sceneStartGlobalSec + playheadLocalSec;
  const playheadLeftPct = totalDur > 0 ? Math.max(0, Math.min(100, (playheadGlobalSec / totalDur) * 100)) : 0;

  // sceneTrack DOM 좌표 → 글로벌 sec 환산을 위해 ref 보유.
  const sceneTrackRef = useRef<HTMLDivElement>(null);

  // #timeline-zoom — 가로 배율. 1 이면 화면 폭에 맞고, 키우면 가로 스크롤이 생긴다.
  const [zoom, setZoom] = useState(1);

  // #narrow-scene-block — 트랙 실제 폭(px). 씬 블록이 씬 번호를 담을 만큼
  // 넓은지 판단하는 데만 쓴다. 측정 전(0)에는 숫자를 그대로 그려 기존 동작 유지.
  const [trackPx, setTrackPx] = useState(0);
  // 측정 대상은 스크롤 영역(보이는 폭) — zoom 과 무관하게 일정하다.
  // 확대 배율은 blockPx() 에서 곱하므로 여기서 중복 반영하면 안 된다.
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = scrollAreaRef.current;
    if (!el) return;
    const update = () => setTrackPx(el.getBoundingClientRect().width);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  /** 마우스/터치 위치(clientX) 를 받아 (sceneId, sceneLocalSec, globalSec) 으로 환산. */
  const xToTimeInfo = useCallback((clientX: number): { sceneId: number; sceneLocalSec: number; globalSec: number } | null => {
    const el = sceneTrackRef.current;
    if (!el || totalDur <= 0 || sorted.length === 0) return null;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return null;
    const xRatio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const globalSec = xRatio * totalDur;
    // 어느 씬에 속하는지 — 단순 선형 탐색(씬 수 100 이내라 충분히 빠름).
    let idx = sorted.length - 1;
    for (let i = 0; i < sorted.length; i++) {
      const start = startsSec[i];
      const end = start + durations[i];
      if (globalSec < end) { idx = i; break; }
    }
    const sceneLocalSec = Math.max(0, globalSec - startsSec[idx]);
    return { sceneId: sorted[idx].scene_id, sceneLocalSec, globalSec };
  }, [sorted, durations, startsSec, totalDur]);

  // 드래그 상태 (sceneTrack 위에서 pointerdown 시작 → window 의 move/up 으로 추적).
  const [isDragging, setIsDragging] = useState(false);
  useEffect(() => {
    if (!isDragging) return;
    const move = (e: PointerEvent) => {
      const info = xToTimeInfo(e.clientX);
      if (info && onSeekToGlobalSec) {
        onSeekToGlobalSec(info.sceneId, info.sceneLocalSec, info.globalSec);
      }
    };
    const up = () => setIsDragging(false);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [isDragging, xToTimeInfo, onSeekToGlobalSec]);

  // sceneTrack 의 빈 영역(블록 사이/playhead 위) pointerdown 처리 — 씬 블록 자체 클릭은 button 이 처리.
  const handleTrackPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    // 마우스 좌측 버튼만.
    if (e.button !== 0 && e.pointerType === "mouse") return;
    const info = xToTimeInfo(e.clientX);
    if (!info) return;
    // 즉시 seek + 드래그 모드 시작.
    if (onSeekToGlobalSec) onSeekToGlobalSec(info.sceneId, info.sceneLocalSec, info.globalSec);
    setIsDragging(true);
  }, [xToTimeInfo, onSeekToGlobalSec]);

  return (
    <div style={styles.wrap} title={`전체 길이 ${formatDur(totalDur)} (씬 ${sorted.length}개)`}>
      {/* 헤더 — 총 길이/씬 수 + 확대/축소 */}
      <div style={styles.header}>
        <span style={styles.headerLabel}>타임라인</span>
        {/* #timeline-zoom — 짧은 씬이 몰린 구간을 확대해서 보기 위한 컨트롤.
            확대하면 가로 스크롤이 생긴다(다른 영상 편집 도구와 동일한 방식). */}
        <div style={styles.zoomBox}>
          <button
            type="button"
            style={styles.zoomBtn}
            title="타임라인 축소"
            disabled={zoom <= ZOOM_MIN}
            onClick={() => setZoom((z) => clampZoom(z / ZOOM_STEP))}
          >
            −
          </button>
          <button
            type="button"
            style={{ ...styles.zoomBtn, minWidth: 46 }}
            title="기본 배율로 (화면 폭에 맞춤)"
            onClick={() => setZoom(1)}
          >
            {zoom.toFixed(1)}×
          </button>
          <button
            type="button"
            style={styles.zoomBtn}
            title="타임라인 확대"
            disabled={zoom >= ZOOM_MAX}
            onClick={() => setZoom((z) => clampZoom(z * ZOOM_STEP))}
          >
            +
          </button>
        </div>
        <span style={styles.headerMeta}>{sorted.length}씬 · 약 {formatDur(totalDur)}</span>
      </div>

      {/* 씬 트랙 — #364 ref + pointerdown 으로 빈 영역 클릭/드래그 seek 지원,
          playhead 빨간 세로선 absolute 오버레이.

          #400 — 과거 sceneTrack 은 `display: flex + gap:1 + minWidth:16` 로 분배되었으나
          좌표계가 playhead(글로벌 sec %) 와 달라 짧은 씬이 많을수록 어긋남이 누적됐다.
          C안 채택: sceneTrack 을 `position: relative` 단순 컨테이너로 두고, 각 sceneBlock 을
          `position: absolute; left/width = startsSec/totalDur, dur/totalDur %` 로 절대 배치.
          → 모든 좌표(씬 블록, playhead, 책갈피 트랙) 가 글로벌 sec % 기준으로 통일된다. */}
      {/* #timeline-zoom — 확대 시 가로 스크롤 컨테이너.
          안쪽 폭을 zoom 배로 늘리고, 씬/책갈피/BGM 트랙을 함께 감싸 세 트랙이
          항상 같은 좌표계로 스크롤된다. zoom=1 이면 폭 100% 라 기존과 동일. */}
      <div ref={scrollAreaRef} style={styles.scrollArea}>
      <div style={{ width: `${zoom * 100}%`, minWidth: "100%" }}>
      <div
        ref={sceneTrackRef}
        style={styles.sceneTrack}
        onPointerDown={onSeekToGlobalSec ? handleTrackPointerDown : undefined}
      >
        {sorted.map((scene, idx) => {
          const dur = durations[idx];
          const widthPct = totalDur > 0 ? (dur / totalDur) * 100 : 1 / sorted.length * 100;
          const startsPct = totalDur > 0 ? ((startsSec[idx] ?? 0) / totalDur) * 100 : (idx / sorted.length) * 100;
          const effectType = scene.footage?.type ?? "edit";
          const color = EFFECT_COLORS[effectType] ?? "#666";
          const isSelected = selectedSceneId === scene.scene_id;
          const label = (scene.title || scene.description || scene.narration_text || "").trim().slice(0, 40);
          return (
            <button
              key={scene.scene_id}
              type="button"
              // #401 — 과거(#400) 에서 디버깅 표식으로 추가했던 `data-scene-id` 를 제거.
              //         SceneCard(Editor.tsx) 도 같은 속성을 쓰는데 DOM 순서상 TimelineBar 가
              //         먼저 등장해 `document.querySelector('[data-scene-id="..."]')` 가
              //         TimelineBar 의 button 자신을 잡아버려 scrollToSceneCard 가 사실상
              //         no-op 이 되는 회귀가 있었음. 이 속성은 #400 의 본 변경(좌표계 통일)에
              //         필수가 아니라 부수적으로 추가된 표식이므로 그대로 원복.
              // #364 — 버튼 클릭(=짧은 click)은 onClick 으로 동작.
              //         pointerdown 은 부모(sceneTrack) 의 드래그 seek 트리거이므로 stopPropagation 으로
              //         버튼 영역에서는 막아둔다. 그래야 사용자가 씬 블록을 클릭할 때 selectScene 만 일어남.
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
                // #387 — Alt/Shift + 클릭은 신규 narration segment 추가 분기.
                //         과거 #382 의 segmentAddHotspot div 가 button 자식으로 깔려 있어
                //         button native click 자체가 발화되지 않는 회귀(=일반 클릭 무반응)가
                //         발생했다. hotspot div 를 제거하고 button onClick 안에서 modifier
                //         분기로 통합 — 일반 클릭 즉시 복구.
                if (onAddNarrationSegment && (e.altKey || e.shiftKey)) {
                  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                  onAddNarrationSegment(scene.scene_id, ratio * dur);
                  return;
                }
                onSelectScene(scene.scene_id);
                scrollToSceneCard(scene.scene_id);
                // #364 — 씬 블록을 직접 클릭하면 그 씬의 0 초로 seek (= playhead 가 블록 시작점에 맞춰짐).
                if (onSeekToGlobalSec) {
                  onSeekToGlobalSec(scene.scene_id, 0, startsSec[idx]);
                }
              }}
              title={`씬 ${scene.scene_id} (${formatDur(dur)})${label ? "\n" + label : ""}${
                scene.narration_text ? "\n나레이션: " + scene.narration_text.slice(0, 120) : ""
              }`}
              style={{
                ...styles.sceneBlock,
                left: `${startsPct}%`,
                // #narrow-scene-block — 씬 블록은 left/width 가 정확히 맞닿게
                // 배치된다(간격 0). 그래서 이웃 블록의 1px 테두리와 borderRadius
                // 둥근 모서리가 같은 픽셀에 겹쳐 그려져, 짧은 씬일수록 두 씬이
                // 하나로 뭉쳐 보였다(사용자 보고: 53·54).
                // 폭에서만 SCENE_GAP_PX 를 빼 실제 시각적 간격을 만든다.
                // left 는 그대로라 playhead 좌표와 어긋나지 않는다.
                width: `max(${MIN_BLOCK_PX}px, calc(${widthPct}% - ${SCENE_GAP_PX}px))`,
                background: `${color}33`,
                borderColor: isSelected ? color : `${color}66`,
                color: "#ddd",
                outline: isSelected ? `2px solid ${color}` : "none",
                outlineOffset: isSelected ? "-2px" : "0",
              }}
            >
              {/* #narrow-scene-block — 좁은 씬 블록에서 씬 번호가 이웃 블록을
                  침범해 "씬이 겹쳐 보인다" 는 오인을 만들었다(사용자 보고: 53·54).
                  블록 폭(≈9px)이 padding(8px)+border(2px)보다 작으면 숫자가
                  들어갈 자리가 없어 넘쳐 흐른다. 자리가 없으면 숫자를 숨긴다
                  — 어차피 읽을 수 없고, 식별은 hover title 과 선택 테두리로 가능. */}
              {(trackPx <= 0 || blockPx(trackPx, zoom, widthPct) >= NUM_MIN_PX) && (
                <span style={styles.sceneNum}>{scene.scene_id}</span>
              )}
              {widthPct > 6 && label && (
                <span style={styles.sceneLabel}>{label}</span>
              )}
              {/* #timeline-dots — 씬 상태를 점 하나로 표시.
                  주황 = TTS 음성이 준비된 씬, 하늘 = 처리 안 된 편집 코멘트가 있는 씬.
                  좁은 블록에서도 보이도록 텍스트와 달리 폭 제한을 두지 않는다. */}
              {(hasTts(scene) || openComments(scene) > 0) && (
                <span style={styles.dotBox}>
                  {hasTts(scene) && (
                    <i style={{ ...styles.dot, background: "#f59e0b" }}
                       title="TTS 대본 있음" />
                  )}
                  {openComments(scene) > 0 && (
                    <i style={{ ...styles.dot, background: "#5dd6ff" }}
                       title={`편집 코멘트 ${openComments(scene)}건`} />
                  )}
                </span>
              )}
              {/* #399 — 씬 블록 내부 세로선 마커(`renderSegmentMarkers`)는 제거됨.
                  씬 블록 폭 차이 + 마커 위치 비율(%) 환산이 사용자 직관과 어긋나
                  "다른 씬에 마커가 떴다" 고 오인되는 회귀(#399) 가 발생했다.
                  segment 위치는 아래 하단 책갈피 트랙(글로벌 sec 기준) 으로만 표시한다.
                  편집 콜백(`onEditNarrationSegment`) 호출 경로는 책갈피 트랙에서 그대로 유지. */}
              {/* #382/#387 — alt/shift+click 으로 신규 segment 추가하는 분기는 위 button.onClick
                  안으로 통합됨. 별도 hotspot div 를 두면 button click 이 발화되지 않아
                  일반 클릭 자체가 죽는 회귀가 발생한다 (HTML 표준: button 자식의 인터랙티브
                  요소가 onClick 을 가지면 button click 미발화). */}
            </button>
          );
        })}
        {/* #364 — playhead 빨간 세로선 (sceneTrack 기준 absolute).
            pointerEvents:none 으로 클릭/드래그가 그대로 sceneTrack 으로 전달되게 함. */}
        {totalDur > 0 && (
          <div
            style={{
              ...styles.playhead,
              left: `${playheadLeftPct}%`,
            }}
            title={`재생 위치 ${formatDur(playheadGlobalSec)} / ${formatDur(totalDur)}`}
          >
            <div style={styles.playheadKnob} />
          </div>
        )}
      </div>

      {/* #392 — segment 책갈피 트랙. 씬 블록 내 마커 폭(2px)이 너무 좁아 클릭하기 어렵다는
          사용자 피드백을 반영해 타임라인 하단에 별도 트랙을 두고 8px 너비 책갈피로 표시한다.
          seg_0(시나리오 파서 자동 생성) 는 #390 정책상 제외, 그 외 segment 만.
          글로벌 좌표 = (씬 누적 시작 sec + segment.offset_sec) / totalDur. */}
      {totalDur > 0 && renderSegmentBookmarkTrack(
        sorted, startsSec, durations, totalDur, onEditNarrationSegment,
      )}

      {/* BGM 트랙 — 옵셔널 */}
      {bgmTracks && bgmTracks.length > 0 && totalDur > 0 && (
        <div style={styles.bgmTrack} title="BGM 트랙">
          {bgmTracks.map((tr) => {
            const left = (Math.max(0, tr.start_sec) / totalDur) * 100;
            const widthPct = (Math.max(0, tr.end_sec - tr.start_sec) / totalDur) * 100;
            return (
              <div
                key={tr.id}
                style={{
                  ...styles.bgmBlock,
                  left: `${left}%`,
                  width: `${widthPct}%`,
                }}
                title={`BGM ${tr.start_sec.toFixed(1)}s–${tr.end_sec.toFixed(1)}s · vol ${(tr.volume * 100).toFixed(0)}%`}
              >
                🎵
              </div>
            );
          })}
        </div>
      )}
      </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  wrap: {
    background: "#0d0d0d",
    borderBottom: "1px solid #222",
    padding: "6px 10px 8px",
    fontFamily: "system-ui, sans-serif",
  },
  empty: {
    background: "#0d0d0d",
    borderBottom: "1px solid #222",
    padding: "8px 12px",
    textAlign: "center" as const,
    color: "#666",
    fontSize: 12,
  },
  emptyText: { fontStyle: "italic" as const },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    marginBottom: 4,
    fontSize: 11,
  },
  headerLabel: { color: "#bbb", fontWeight: 600 },
  headerMeta: { color: "#666", marginLeft: "auto" },
  // #timeline-zoom
  zoomBox: { display: "flex", alignItems: "center", gap: 2, marginLeft: 8 },
  zoomBtn: {
    background: "#2a2a2a",
    color: "#ccc",
    border: "1px solid #3a3a3a",
    borderRadius: 3,
    fontSize: 11,
    lineHeight: "16px",
    minWidth: 20,
    height: 18,
    padding: "0 4px",
    cursor: "pointer",
  },
  scrollArea: {
    overflowX: "auto" as const,
    overflowY: "hidden" as const,
    width: "100%",
  },
  // #400 — flex+gap+minWidth 분배에서 absolute(글로벌 sec %) 좌표계로 전환.
  //   기존 flex/gap/minWidth 누적이 playhead 좌표(글로벌 sec %) 와 어긋나는 회귀(#400)
  //   를 해소하기 위함. sceneTrack 은 단순 컨테이너이고, 자식 sceneBlock 이 각자
  //   `left/width = startsSec/totalDur, dur/totalDur %` 로 절대 배치된다.
  sceneTrack: {
    position: "relative" as const,
    width: "100%",
    height: 28,
    background: "#1a1a1a",
    borderRadius: 3,
    overflow: "hidden",
  },
  sceneBlock: {
    position: "absolute" as const,
    top: 0,
    bottom: 0,
    // 최소 폭은 inline style 의 width: max(MIN_BLOCK_PX, calc(% - GAP)) 가 보장한다.
    // 여기서 minWidth 를 또 주면 간격(SCENE_GAP_PX)을 덮어써 다시 맞닿아 버린다.
    display: "flex",
    alignItems: "center",
    gap: 4,
    padding: "2px 4px",
    border: "1px solid transparent",
    borderRadius: 3,
    cursor: "pointer",
    overflow: "hidden",
    whiteSpace: "nowrap" as const,
    fontSize: 11,
    boxSizing: "border-box" as const,
    background: "#333",
  },
  sceneNum: { fontWeight: 700, opacity: 0.9 },
  // #timeline-dots — 블록 우상단에 붙는 상태 점.
  dotBox: {
    position: "absolute" as const, top: 3, right: 3,
    display: "flex", gap: 2, pointerEvents: "none" as const,
  },
  dot: {
    width: 6, height: 6, borderRadius: "50%",
    boxShadow: "0 0 0 1px rgba(0,0,0,.55)",
  },
  sceneLabel: { opacity: 0.7, overflow: "hidden", textOverflow: "ellipsis" },
  bgmTrack: {
    position: "relative" as const,
    width: "100%",
    height: 14,
    marginTop: 3,
    background: "#1a1a1a",
    borderRadius: 3,
    overflow: "hidden",
  },
  bgmBlock: {
    position: "absolute" as const,
    top: 0,
    bottom: 0,
    background: "rgba(167,139,250,0.25)",
    border: "1px solid rgba(167,139,250,0.55)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 10,
    color: "#a78bfa",
    overflow: "hidden",
    minWidth: 8,
  },
  // #364 — playhead 빨간 세로선. sceneTrack 기준 absolute.
  //   pointerEvents:none 으로 클릭/드래그가 부모(sceneTrack)로 전달되어
  //   playhead 위치에서도 seek 가 자연스럽게 작동한다.
  playhead: {
    position: "absolute" as const,
    top: -2,
    bottom: -2,
    width: 2,
    marginLeft: -1, // 가운데 정렬
    background: "#ef4444",
    pointerEvents: "none" as const,
    boxShadow: "0 0 4px rgba(239,68,68,0.7)",
    zIndex: 5,
  },
  // playhead 상단 작은 손잡이 — 위치를 시각적으로 더 잘 보이게.
  playheadKnob: {
    position: "absolute" as const,
    top: -4,
    left: -3,
    width: 8,
    height: 8,
    background: "#ef4444",
    borderRadius: "50%",
    boxShadow: "0 0 4px rgba(239,68,68,0.7)",
  },
  // (#399) segmentMarker / segmentMarkerOverflow 스타일은 제거됨 — 씬 블록 내부 마커
  //  자체가 사용자 직관과 어긋나 제거되었다. 책갈피 트랙(`bookmark`) 스타일만 유지.
  // (#387) segmentAddHotspot 스타일은 제거됨 — 위 button.onClick 안에서 modifier 분기로 통합.
  // #392 — 책갈피 트랙. sceneTrack 과 동일한 가로 폭 기준으로 글로벌 sec 비율을 그대로 매핑한다.
  bookmarkTrack: {
    position: "relative" as const,
    width: "100%",
    height: 18,
    marginTop: 3,
    background: "#1a1a1a",
    borderRadius: 3,
    overflow: "visible" as const,
  },
  // 책갈피 한 개. 사각형 + 아래 끝이 화살표(triangle)처럼 보이도록 단순화 — 6px 너비.
  bookmark: {
    position: "absolute" as const,
    top: 2,
    bottom: 2,
    width: 8,
    marginLeft: -4, // 중앙 정렬
    background: "rgba(255,255,255,0.9)",
    border: "1px solid rgba(255,255,255,0.95)",
    borderRadius: 2,
    cursor: "pointer",
    boxShadow: "0 0 3px rgba(0,0,0,0.35)",
  },
  bookmarkOverflow: {
    background: "#ef4444",
    borderColor: "#ef4444",
    boxShadow: "0 0 4px rgba(239,68,68,0.8)",
  },
  bookmarkError: {
    background: "#f59e0b",
    borderColor: "#f59e0b",
    boxShadow: "0 0 4px rgba(245,158,11,0.8)",
  },
};

// ── #392 — segment 책갈피 트랙 렌더 헬퍼 ────────────────────────────────
// 타임라인 하단에 모든 씬의 segment(seg_0 제외) 를 글로벌 위치 기준으로 책갈피 형태로
// 표시한다. 사용자가 마커 클릭이 어렵다는 피드백을 받아 별도 트랙에 더 큰 hit-target 을
// 제공하는 것이 목적. 클릭 시 기존 onEditNarrationSegment 콜백을 그대로 호출한다.
//
// 동작 정책:
//   - 각 segment 의 글로벌 sec = (씬 누적 시작 sec) + (segment.offset_sec).
//   - leftPct = (globalSec / totalDur) * 100. 트랙 가로 폭은 sceneTrack 과 동일하므로
//     씬 블록 상단의 마커 위치와 책갈피 위치가 동일 X 좌표에서 만난다.
//   - status==="error" 면 주황색, segment 가 씬 길이를 넘어가면 빨간색 (overflow > error).
//   - title(툴팁)에 씬 번호 + offset + text 미리보기 + 에러 메시지 표시.
function renderSegmentBookmarkTrack(
  sorted: SceneCard[],
  startsSec: number[],
  durations: number[],
  totalDur: number,
  onEdit?: (sceneId: number, segId: string) => void,
): React.ReactNode {
  if (totalDur <= 0 || !sorted.length) return null;
  type Entry = {
    key: string;
    leftPct: number;
    sceneId: number;
    segId: string;
    seg: NarrationSegment;
    sceneDur: number;
    globalSec: number;
  };
  const entries: Entry[] = [];
  for (let i = 0; i < sorted.length; i++) {
    const scene = sorted[i];
    const start = startsSec[i] ?? 0;
    const sceneDur = durations[i] ?? 0;
    const segs = scene.narration_segments ?? [];
    for (const seg of segs) {
      // #390 정책과 동일하게 seg_0 는 책갈피 대상에서 제외.
      if (seg.id === "seg_0") continue;
      const offset = Math.max(0, seg.offset_sec ?? 0);
      const globalSec = start + offset;
      const leftPct = Math.max(0, Math.min(100, (globalSec / totalDur) * 100));
      entries.push({
        key: `${scene.scene_id}:${seg.id}`,
        leftPct,
        sceneId: scene.scene_id,
        segId: seg.id,
        seg,
        sceneDur,
        globalSec,
      });
    }
  }
  // 책갈피가 한 개도 없으면 트랙 자체를 그리지 않음 (UI 노이즈 감소).
  if (entries.length === 0) return null;
  return (
    <div style={styles.bookmarkTrack} title="TTS 책갈피 트랙">
      {entries.map((e) => {
        const overflow = isSegmentOverflow(e.seg, e.sceneDur);
        const isErr = e.seg.status === "error";
        const style: React.CSSProperties = {
          ...styles.bookmark,
          left: `${e.leftPct}%`,
          // overflow 가 error 보다 우선 — 빨간색이 더 강한 경고.
          ...(overflow ? styles.bookmarkOverflow : isErr ? styles.bookmarkError : {}),
        };
        const errSuffix = isErr && e.seg.error_message
          ? `\n⚠ TTS 실패: ${e.seg.error_message.slice(0, 120)}`
          : isErr
            ? "\n⚠ TTS 생성 실패 (재시도 필요)"
            : "";
        return (
          <div
            key={e.key}
            role="button"
            tabIndex={0}
            style={style}
            title={`씬 ${e.sceneId} · ${e.segId} · offset ${(e.seg.offset_sec ?? 0).toFixed(2)}s · 글로벌 ${e.globalSec.toFixed(2)}s${
              overflow ? "\n⚠ 씬 길이를 초과합니다 (렌더 시 씬 경계에서 잘림)" : ""
            }${errSuffix}${e.seg.text ? `\n${e.seg.text.slice(0, 80)}` : ""}`}
            onClick={(ev) => {
              ev.stopPropagation();
              if (onEdit) onEdit(e.sceneId, e.segId);
            }}
            onPointerDown={(ev) => ev.stopPropagation()}
            onKeyDown={(ev) => {
              if ((ev.key === "Enter" || ev.key === " ") && onEdit) {
                ev.preventDefault();
                onEdit(e.sceneId, e.segId);
              }
            }}
          />
        );
      })}
    </div>
  );
}

// ── #399 — `renderSegmentMarkers` 헬퍼는 제거됨 ─────────────────────────
// 사유: 씬 블록 폭은 (footage 트림/cut 결과의) 씬별 상대 길이 비율로 결정되는데,
// 그 안의 segment 마커 위치는 (offset_sec / sceneDur) * 100% 로 환산되어
// 시각적으로 "다른 씬 위에 떴다" 로 오인되는 회귀가 사용자 테스트에서 발견됨(#399).
// segment 위치는 글로벌 sec 기준의 하단 책갈피 트랙(`renderSegmentBookmarkTrack`) 으로만
// 표시한다. 책갈피는 폭이 8px 로 클릭도 더 쉽다(#392 정책 그대로).
