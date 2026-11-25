/**
 * BgmEditor — 후처리 전용 BGM 다중 트랙 에디터 (#289)
 *
 * 진입 조건: 렌더링이 끝나 final_output.mp4가 존재하는 프로젝트.
 * 입력: final_output.mp4 (불변 영상 입력) + Project.bgm_tracks
 * 출력: final_with_bgm.mp4 (별도 파일, 원본은 그대로 유지)
 *
 * UI:
 *   - 상단: 영상 + 시크 바 (HTMLVideoElement). 재생 시 모든 BGM 트랙이 합성되어 들림 (Web Audio 미리듣기).
 *   - 중앙: 가로 타임라인 (px = sec * pxPerSec). 트랙별 막대 표시 + 좌우 핸들로 길이 조정.
 *           트랙 드래그로 시작 시점 조정.
 *   - 우측 패널: 선택된 트랙의 자산/볼륨/페이드 편집.
 *   - 하단: "BGM 렌더링" 버튼 → POST /render-bgm, WS 진행률 표시.
 *
 * 데이터 저장:
 *   - BgmTrack 변경은 IndexedDB(updateProject) + 서버 PATCH 둘 다 호출 (이중 동기화).
 *   - 다른 화면(Editor)과의 일관성 위해 store도 갱신.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import type { BgmTrack, FootageAsset, Project } from "../types";
import { updateProject as dbUpdateProject } from "../lib/project";
import {
  startBgmRender,
  bgmRenderStatus,
  bgmRenderOutputUrl,
  renderOutputUrl,
  studioAssetStreamUrl,
  resolveBgmStreamUrl,
} from "../api/client";
import { api } from "../api/client";
import { ui } from "../store/ui";
import { useWs } from "../hooks/useWs";
// #312 — BGM 트랙 파형 시각화. 데이터 추출/캐시/캔버스 렌더는 서브 모듈에서 처리.
import { useTrackWaveform } from "../hooks/useTrackWaveform";

const PX_PER_SEC_DEFAULT = 24;
const TRACK_HEIGHT = 44;
const ACCENT = "#a78bfa"; // 보라 — BGM 식별색

interface Props {
  projectId: string;
  project: Project;
  /** 영상 길이(초). final_output.mp4 ffprobe 결과를 호출자가 미리 전달. */
  videoDurationSec: number;
  onProjectChange: (p: Project) => void;
  onBack: () => void;
}

export default function BgmEditor({
  projectId,
  project,
  videoDurationSec,
  onProjectChange,
  onBack,
}: Props) {
  // ── 자산 풀 (audio만) ──────────────────────────────────────────────────
  const audioAssets = useMemo<FootageAsset[]>(
    () => (project.studio_assets || []).filter((a) => a.asset_type === "audio"),
    [project.studio_assets]
  );

  // 트랙별 원본 BGM 길이(초). 스튜디오 audio 자산은 메타 부재로 무제한(Infinity) 처리.
  // (#419 — 공용 BGM 라이브러리 제거. BGM 은 프로젝트에 등록한 오디오만 쓴다.)
  function getTrackMaxDuration(_audioAssetId: string): number {
    return Number.POSITIVE_INFINITY;
  }

  // 트랙 라벨 해석 — 스튜디오(프로젝트 등록) 오디오 자산.
  function getTrackLabel(audioAssetId: string): string {
    const a = (project.studio_assets || []).find((x) => x.asset_id === audioAssetId);
    return a?.label || a?.filename || "(자산 없음)";
  }

  // ── 트랙 상태 ──────────────────────────────────────────────────────────
  const [tracks, setTracks] = useState<BgmTrack[]>(project.bgm_tracks || []);
  const [selectedTrackId, setSelectedTrackId] = useState<string | null>(null);

  useEffect(() => {
    setTracks(project.bgm_tracks || []);
  }, [project.bgm_tracks]);

  // ── 타임라인 px/sec ────────────────────────────────────────────────────
  const [pxPerSec, setPxPerSec] = useState<number>(PX_PER_SEC_DEFAULT);
  const timelineWidth = Math.max(800, Math.ceil(videoDurationSec * pxPerSec));

  // ── 영상 + 미리듣기 ─────────────────────────────────────────────────────
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const audioElsRef = useRef<Map<string, HTMLAudioElement>>(new Map());
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  // #bgm-preview-cache — 렌더 결과의 mtime 을 받아 URL 에 붙인다.
  // 이게 없으면 브라우저가 옛 영상을 캐시해, 다시 렌더해도 이 화면엔 이전 버전이 나온다.
  const [videoVersion, setVideoVersion] = useState<number>(0);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/projects/${projectId}/render/output-info`);
        if (!r.ok) return;
        const info = await r.json();
        if (!cancelled && typeof info?.mtime === "number") setVideoVersion(info.mtime);
      } catch { /* 실패해도 버전 없이 로드 — 기존 동작 */ }
    })();
    return () => { cancelled = true; };
  }, [projectId]);
  const videoUrl = useMemo(
    () => renderOutputUrl(projectId, videoVersion || undefined),
    [projectId, videoVersion],
  );

  // 재생 시 모든 BGM 트랙을 HTMLAudioElement로 동기화 재생.
  // 완벽한 더킹은 불가능하지만 사용자가 볼륨 균형을 잡기에는 충분하다.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onTime = () => {
      setCurrentTime(v.currentTime);
      // 트랙별로 위치 보정
      tracks.forEach((t) => {
        const a = audioElsRef.current.get(t.id);
        if (!a) return;
        const tInTrack = v.currentTime - t.start_sec;
        const trackDur = t.end_sec - t.start_sec;
        if (tInTrack < 0 || tInTrack > trackDur) {
          if (!a.paused) a.pause();
        } else {
          // 큰 편차일 때만 보정 (자잘한 drift 방지)
          if (Math.abs(a.currentTime - tInTrack) > 0.3) {
            a.currentTime = Math.max(0, tInTrack);
          }
          if (v.paused) {
            if (!a.paused) a.pause();
          } else if (a.paused) {
            a.play().catch(() => {});
          }
          // ── 페이드 인/아웃 — 백엔드 ffmpeg afade 와 동일한 선형 곡선 ──
          // 슬라이더로 fade 값을 바꿔도 청각적으로 즉시 반영되도록 매 ontime 마다 a.volume 재계산.
          // ffmpeg afade=t=in 은 [0, fade_in] 구간에서 0 → 1 선형, afade=t=out 은
          // [duration-fade_out, duration] 구간에서 1 → 0 선형. 두 구간은 겹치지 않게 fade ≤ duration/2 클램프.
          const baseVol = Math.max(0, Math.min(1, t.volume));
          const fadeIn = Math.max(0, Math.min(t.fade_in_sec, trackDur / 2));
          const fadeOut = Math.max(0, Math.min(t.fade_out_sec, trackDur / 2));
          let envelope = 1;
          if (fadeIn > 0 && tInTrack < fadeIn) {
            envelope = tInTrack / fadeIn;
          } else if (fadeOut > 0 && tInTrack > trackDur - fadeOut) {
            envelope = Math.max(0, (trackDur - tInTrack) / fadeOut);
          }
          a.volume = Math.max(0, Math.min(1, baseVol * envelope));
        }
      });
    };
    const onPlay = () => setPlaying(true);
    const onPause = () => {
      setPlaying(false);
      // 모든 BGM 정지
      audioElsRef.current.forEach((a) => { if (!a.paused) a.pause(); });
    };
    const onSeek = () => { onTime(); };

    v.addEventListener("timeupdate", onTime);
    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    v.addEventListener("seeked", onSeek);
    return () => {
      v.removeEventListener("timeupdate", onTime);
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
      v.removeEventListener("seeked", onSeek);
    };
  }, [tracks]);

  // BGM 트랙 볼륨/소스/페이드가 바뀌면 audio 요소도 갱신.
  // 슬라이더 변경 직후엔 video.timeupdate 가 발화하지 않으므로 여기서 즉시 envelope 을 한 번 더 계산해
  // 미리듣기에 즉시 반영되게 한다.
  useEffect(() => {
    const seen = new Set<string>();
    const vNow = videoRef.current?.currentTime ?? 0;
    tracks.forEach((t) => {
      seen.add(t.id);
      let a = audioElsRef.current.get(t.id);
      if (!a) {
        a = new Audio();
        a.preload = "auto";
        audioElsRef.current.set(t.id, a);
      }
      const desiredSrc = resolveBgmStreamUrl(projectId, t.audio_asset_id);
      if (a.src !== desiredSrc) {
        a.src = desiredSrc;
      }
      // 슬라이더 즉시 반영 — fade envelope 을 현재 video 위치 기준으로 계산.
      const baseVol = Math.max(0, Math.min(1, t.volume));
      const trackDur = t.end_sec - t.start_sec;
      const tInTrack = vNow - t.start_sec;
      const fadeIn = Math.max(0, Math.min(t.fade_in_sec, trackDur / 2));
      const fadeOut = Math.max(0, Math.min(t.fade_out_sec, trackDur / 2));
      let envelope = 1;
      if (tInTrack < 0 || tInTrack > trackDur) {
        envelope = 0;
      } else if (fadeIn > 0 && tInTrack < fadeIn) {
        envelope = tInTrack / fadeIn;
      } else if (fadeOut > 0 && tInTrack > trackDur - fadeOut) {
        envelope = Math.max(0, (trackDur - tInTrack) / fadeOut);
      }
      a.volume = Math.max(0, Math.min(1, baseVol * envelope));
    });
    // 제거된 트랙 정리
    for (const id of Array.from(audioElsRef.current.keys())) {
      if (!seen.has(id)) {
        const a = audioElsRef.current.get(id);
        if (a && !a.paused) a.pause();
        audioElsRef.current.delete(id);
      }
    }
  }, [tracks, projectId]);

  // 컴포넌트 언마운트 시 모든 audio 요소 정지·해제 (페이지 이동 후 잔류 재생 방지)
  useEffect(() => {
    return () => {
      audioElsRef.current.forEach((a) => {
        if (!a.paused) a.pause();
        a.removeAttribute("src");
        a.load();
      });
      audioElsRef.current.clear();
    };
  }, []);

  // ── 상태 저장 (IndexedDB + 서버) ────────────────────────────────────────
  async function persist(nextTracks: BgmTrack[]) {
    setTracks(nextTracks);
    try {
      const updated = await dbUpdateProject(projectId, { bgm_tracks: nextTracks });
      onProjectChange(updated);
      // 서버 PATCH (BGM 렌더가 서버에서 일어나므로 동기화 필수)
      await api.patch(`/api/projects/${projectId}`, { bgm_tracks: nextTracks }).catch(() => {});
    } catch (e) {
      console.warn("[BgmEditor] persist 실패:", e);
    }
  }

  // BGM 트랙 추가 — 항상 파일 선택 dialog로 새 오디오를 받아 업로드 후 트랙 생성.
  // 사용자가 dialog 취소하고 기존 자산이 있으면 첫 항목으로 폴백.
  async function addTrack() {
    let newAsset: FootageAsset | null = null;

    // 1) 파일 선택 dialog. cancel/onchange 한 번만 처리.
    newAsset = await new Promise<FootageAsset | null>((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "audio/*";
      let settled = false;
      input.onchange = async () => {
        if (settled) return;
        settled = true;
        const file = input.files?.[0];
        if (!file) { resolve(null); return; }
        try {
          const form = new FormData();
          form.append("file", file, file.name);
          // #308 — 클라에서 오디오 duration 추출해 백엔드 ffprobe 절감. 실패는 무시.
          // #312 — BGM 편집기 경로에서는 sample_rate/channels 까지 보강.
          //   같은 시점에 파형 추출도 이후 BGM 타임라인에서 일어나므로 decodeAudioData
          //   비용을 한 번에 부담해도 무방. 백엔드 로그/응답에 진단 정보가 풍부해진다.
          try {
            const { extractAudioMeta, isAudioMetaSane } = await import("../lib/clientMediaMeta");
            const meta = await extractAudioMeta(file, { withSampleRate: true }).catch(
              () => null,
            );
            if (meta && isAudioMetaSane(meta)) {
              form.append("client_meta", JSON.stringify(meta));
            }
          } catch {
            /* 동적 import 실패는 무시 */
          }
          const res = await fetch(`/api/projects/${projectId}/studio/audio-upload`, {
            method: "POST",
            body: form,
          });
          if (!res.ok) {
            const t = await res.text().catch(() => "");
            ui.error(`오디오 업로드 실패: HTTP ${res.status} ${t.slice(0, 200)}`);
            resolve(null);
            return;
          }
          const data = await res.json();
          resolve((data?.asset as FootageAsset) ?? null);
        } catch (e) {
          ui.error(`오디오 업로드 실패: ${(e as Error).message}`);
          resolve(null);
        }
      };
      // cancel 감지(브라우저별 차이) — focus 복귀 후 settled 안 되었으면 취소로 간주.
      input.oncancel = () => { if (!settled) { settled = true; resolve(null); } };
      const onFocus = () => {
        setTimeout(() => {
          if (!settled) { settled = true; resolve(null); }
          window.removeEventListener("focus", onFocus);
        }, 500);
      };
      window.addEventListener("focus", onFocus);
      input.click();
    });

    // 2) 새 자산을 못 얻었으면 기존 자산 첫 항목으로 폴백, 그것도 없으면 중단.
    let assetIdToUse: string;
    if (newAsset?.asset_id) {
      assetIdToUse = newAsset.asset_id;
    } else if (audioAssets.length > 0) {
      assetIdToUse = audioAssets[0].asset_id;
    } else {
      return;
    }

    const startSec = Math.max(0, currentTime);
    const endSec = Math.min(videoDurationSec, startSec + 10);
    const next: BgmTrack = {
      id: uuidv4(),
      audio_asset_id: assetIdToUse,
      start_sec: startSec,
      end_sec: endSec,
      fade_in_sec: 0.5,
      fade_out_sec: 0.5,
      volume: 0.4,
    };

    // bgm_tracks와 studio_assets를 한 번에 저장해 race condition 회피.
    // 신규 자산이 있으면 그것을 포함한 studio_assets로 patch.
    const nextStudioAssets = newAsset
      ? [...(project.studio_assets || []), newAsset]
      : project.studio_assets;
    const nextTracks = [...tracks, next];
    setTracks(nextTracks);
    try {
      const updated = await dbUpdateProject(projectId, {
        bgm_tracks: nextTracks,
        ...(newAsset ? { studio_assets: nextStudioAssets } : {}),
      });
      onProjectChange(updated);
      // 서버에도 동시 push — bgm_tracks + studio_assets.
      await api.patch(`/api/projects/${projectId}`, {
        bgm_tracks: nextTracks,
        ...(newAsset ? { studio_assets: nextStudioAssets } : {}),
      }).catch(() => {});
    } catch (e) {
      console.warn("[BgmEditor] addTrack persist 실패:", e);
    }
    setSelectedTrackId(next.id);
  }

  /**
   * 트랙 patch 반영. ``opts.localOnly`` 이면 로컬 state 만 갱신하고 서버 PATCH/IndexedDB 는 보류.
   * 드래그 중(mousemove) 처럼 매 픽셀 발생하는 변경은 localOnly:true 로 호출하고, mouseup 1회에
   * 전체 persist 를 호출해 race condition + 점멸을 막는다.
   */
  function updateTrack(id: string, patch: Partial<BgmTrack>, opts?: { localOnly?: boolean }) {
    const next = tracks.map((t) => (t.id === id ? { ...t, ...patch } : t));
    // 검증: end_sec >= start_sec, 영상 길이 안에서 클램프, BGM 원본 길이 이상 확장 차단.
    // src_dur 가 Infinity(스튜디오 자산 등)면 영상 길이만 한도.
    const clamped = next.map((t) => {
      const maxDur = getTrackMaxDuration(t.audio_asset_id);
      const s = Math.max(0, Math.min(videoDurationSec, t.start_sec));
      let e = Math.max(s + 0.1, Math.min(videoDurationSec, t.end_sec));
      if (Number.isFinite(maxDur) && e - s > maxDur) {
        e = s + maxDur;
      }
      return { ...t, start_sec: s, end_sec: e };
    });
    if (opts?.localOnly) {
      setTracks(clamped);
    } else {
      persist(clamped);
    }
  }

  async function deleteTrack(id: string) {
    const ok = await ui.confirm({
      title: "BGM 트랙 삭제",
      message: "이 BGM 트랙을 삭제하시겠습니까?",
      variant: "danger",
      confirmLabel: "삭제",
    });
    if (!ok) return;
    persist(tracks.filter((t) => t.id !== id));
    if (selectedTrackId === id) setSelectedTrackId(null);
  }

  // ── 드래그(시작 시점 이동) ──────────────────────────────────────────────
  type DragMode = "move" | "resize-start" | "resize-end";
  const dragRef = useRef<{
    id: string;
    mode: DragMode;
    startX: number;
    origStart: number;
    origEnd: number;
    maxDur: number;  // 트랙 원본 길이(라이브러리). Infinity = 무제한(스튜디오 자산).
  } | null>(null);

  function onTrackMouseDown(e: React.MouseEvent, t: BgmTrack, mode: DragMode) {
    e.preventDefault();
    e.stopPropagation();
    setSelectedTrackId(t.id);
    dragRef.current = {
      id: t.id,
      mode,
      startX: e.clientX,
      origStart: t.start_sec,
      origEnd: t.end_sec,
      maxDur: getTrackMaxDuration(t.audio_asset_id),
    };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  }
  function onMouseMove(e: MouseEvent) {
    const d = dragRef.current;
    if (!d) return;
    const dx = (e.clientX - d.startX) / pxPerSec;
    const dur = d.origEnd - d.origStart;
    // 드래그 중에는 localOnly. 위치 계산은 origStart/origEnd 기반.
    // resize 는 BGM 원본 길이 이내로 즉시 차단 — 드래그 자체가 더 이상 늘어나지 않게.
    if (d.mode === "move") {
      const newStart = Math.max(0, Math.min(videoDurationSec - dur, d.origStart + dx));
      updateTrack(d.id, { start_sec: newStart, end_sec: newStart + dur }, { localOnly: true });
    } else if (d.mode === "resize-start") {
      // start 를 줄이면 (end - start) 가 커지므로 maxDur 한도 확인.
      const minStart = Number.isFinite(d.maxDur) ? Math.max(0, d.origEnd - d.maxDur) : 0;
      const newStart = Math.max(minStart, Math.min(d.origEnd - 0.1, d.origStart + dx));
      updateTrack(d.id, { start_sec: newStart }, { localOnly: true });
    } else if (d.mode === "resize-end") {
      const maxEnd = Number.isFinite(d.maxDur)
        ? Math.min(videoDurationSec, d.origStart + d.maxDur)
        : videoDurationSec;
      const newEnd = Math.max(d.origStart + 0.1, Math.min(maxEnd, d.origEnd + dx));
      updateTrack(d.id, { end_sec: newEnd }, { localOnly: true });
    }
  }
  function onMouseUp() {
    const d = dragRef.current;
    dragRef.current = null;
    window.removeEventListener("mousemove", onMouseMove);
    window.removeEventListener("mouseup", onMouseUp);
    // 드래그 종료 시점에 최종 state 한 번만 서버/IndexedDB 로 flush.
    if (d) {
      // setTracks(localOnly) 후의 최신 tracks 를 functional setter 로 안전하게 가져온다.
      setTracks((cur) => {
        // persist 는 비동기지만 다음 sync render 와 race 가 없도록 다음 tick 에 호출.
        Promise.resolve().then(() => persist(cur));
        return cur;
      });
    }
  }

  // ── BGM 렌더링 ─────────────────────────────────────────────────────────
  const [renderTaskId, setRenderTaskId] = useState<string | null>(null);
  const [renderStatus, setRenderStatus] = useState<"idle" | "running" | "done" | "error">("idle");
  const [renderMsg, setRenderMsg] = useState("");

  useEffect(() => {
    // 마운트 시 기존 상태 조회
    bgmRenderStatus(projectId).then((s) => {
      if (s.status === "done") {
        setRenderStatus("done");
      } else if (s.status === "error") {
        setRenderStatus("error");
        setRenderMsg((s as any).error ?? "이전 BGM 합성이 실패했습니다.");
      }
    }).catch(() => {});
  }, [projectId]);

  useWs(renderTaskId, (msg) => {
    if (msg.type === "progress") setRenderMsg((msg as { message?: string }).message ?? "BGM 합성 중...");
    if (msg.type === "complete") {
      setRenderStatus("done");
      setRenderMsg("BGM 합성 완료");
      setRenderTaskId(null);
    }
    if (msg.type === "error") {
      setRenderStatus("error");
      setRenderMsg((msg as { message?: string }).message ?? "BGM 합성 실패");
      setRenderTaskId(null);
    }
  }, () => {
    setRenderTaskId(null);
  });

  async function handleRender() {
    if (tracks.length === 0) {
      ui.info("BGM 트랙이 없습니다.");
      return;
    }
    setRenderStatus("running");
    setRenderMsg("BGM 합성 시작...");
    try {
      // BGM 렌더링은 서버 state.json의 studio_assets + bgm_tracks를 읽는다.
      // audio-upload 직후 다른 흐름이 옛 studio_assets로 서버를 덮어씌웠을 수 있으므로,
      // 시작 직전에 현재 부모 project의 audio 자산 + 트랙을 서버에 강제 push.
      try {
        await api.patch(`/api/projects/${projectId}`, {
          bgm_tracks: tracks,
          studio_assets: project.studio_assets,
        });
      } catch (e) {
        console.warn("[BgmEditor] 렌더 전 sync 실패:", e);
      }
      const taskId = await startBgmRender(projectId);
      setRenderTaskId(taskId);
    } catch (e) {
      setRenderStatus("error");
      setRenderMsg(`BGM 렌더 시작 실패: ${(e as Error).message}`);
    }
  }

  const selectedTrack = tracks.find((t) => t.id === selectedTrackId) ?? null;

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <button style={styles.backBtn} onClick={onBack}>← 편집기</button>
        <span style={styles.title}>BGM 편집 — {project.name}</span>
        <span style={styles.duration}>총 {videoDurationSec.toFixed(1)}초</span>
      </div>

      {/* 영상 */}
      <div style={styles.videoBox}>
        <video
          ref={videoRef}
          src={videoUrl}
          controls
          style={styles.video}
          onLoadedMetadata={(e) => {
            // 영상 길이 다를 경우 videoDurationSec를 사용자에게 표시. 보정은 호출자가 함.
            console.log("[BgmEditor] video duration:", (e.target as HTMLVideoElement).duration);
          }}
        />
      </div>

      {/* 타임라인 */}
      <div style={styles.timelineContainer}>
        <div style={styles.timelineControls}>
          <button style={styles.addBtn} onClick={addTrack} title="오디오 파일 선택 → 트랙 추가">
            + BGM 트랙 추가
          </button>
          {audioAssets.length === 0 && (
            <span style={styles.warn}>등록된 오디오가 없습니다. 버튼을 눌러 파일을 선택하세요.</span>
          )}
          <span style={styles.zoom}>줌:</span>
          <input
            type="range" min={8} max={120} step={1}
            value={pxPerSec}
            onChange={(e) => setPxPerSec(Number(e.target.value))}
            style={{ width: 120 }}
          />
        </div>

        <div style={styles.timelineScroll}>
          <div style={{ ...styles.timeline, width: timelineWidth }}>
            {/* 시간 눈금 */}
            <TimelineRuler durationSec={videoDurationSec} pxPerSec={pxPerSec} />

            {/* 트랙들 */}
            {tracks.map((t, i) => {
              const left = t.start_sec * pxPerSec;
              const width = Math.max(20, (t.end_sec - t.start_sec) * pxPerSec);
              const label = getTrackLabel(t.audio_asset_id);
              const selected = selectedTrackId === t.id;
              return (
                <div
                  key={t.id}
                  style={{
                    ...styles.track,
                    top: 36 + i * (TRACK_HEIGHT + 6),
                    left,
                    width,
                    ...(selected ? styles.trackSelected : {}),
                  }}
                  onMouseDown={(e) => onTrackMouseDown(e, t, "move")}
                  onClick={() => setSelectedTrackId(t.id)}
                  title={`${label} — ${t.start_sec.toFixed(1)}s~${t.end_sec.toFixed(1)}s vol ${(t.volume * 100).toFixed(0)}%`}
                >
                  <div
                    style={styles.handleLeft}
                    onMouseDown={(e) => onTrackMouseDown(e, t, "resize-start")}
                  />
                  {/* #312 — 파형 캔버스: 클라 계산, 트랙 막대 안에 absolute 배치 */}
                  <TrackWaveformCanvas
                    projectId={projectId}
                    assetId={t.audio_asset_id}
                    width={width}
                    height={TRACK_HEIGHT}
                  />
                  <span style={styles.trackLabel}>{label}</span>
                  <div
                    style={styles.handleRight}
                    onMouseDown={(e) => onTrackMouseDown(e, t, "resize-end")}
                  />
                </div>
              );
            })}

            {/* 현재 재생 위치 표시 */}
            <div
              style={{
                ...styles.playhead,
                left: currentTime * pxPerSec,
              }}
            />
          </div>
        </div>
      </div>

      {/* 우측 패널 (선택 트랙 편집) */}
      {selectedTrack && (
        <TrackInspector
          projectId={projectId}
          track={selectedTrack}
          audioAssets={audioAssets}
          videoDurationSec={videoDurationSec}
          onPatch={(p, opts) => updateTrack(selectedTrack.id, p, opts)}
          onDelete={() => deleteTrack(selectedTrack.id)}
        />
      )}

      {/* 렌더링 */}
      <div style={styles.footer}>
        <button
          style={{ ...styles.renderBtn, opacity: renderStatus === "running" ? 0.6 : 1 }}
          onClick={handleRender}
          disabled={renderStatus === "running" || tracks.length === 0}
        >
          {renderStatus === "running" ? "BGM 합성 중..." : "▶ BGM 렌더링"}
        </button>
        {renderStatus !== "idle" && (
          <span
            style={{
              color: renderStatus === "error" ? "#f87171" : renderStatus === "done" ? "#4ade80" : "#aaa",
              fontSize: 12,
              flex: 1,
            }}
          >
            {renderMsg}
          </span>
        )}
        {renderStatus === "done" && (
          <a
            href={bgmRenderOutputUrl(projectId)}
            download="final_with_bgm.mp4"
            style={styles.downloadLink}
          >
            final_with_bgm.mp4 다운로드
          </a>
        )}
      </div>
      {/* 재생 중 BGM 트랙 동기화는 useEffect 안에서 audioElsRef로 직접 처리됨 */}
      <span style={{ display: "none" }}>{playing ? "playing" : "paused"}</span>
    </div>
  );
}

/**
 * BGM 트랙 막대 안에 표시되는 파형 캔버스 (#312).
 *
 * - peaks 데이터는 `useTrackWaveform` 훅이 IndexedDB 캐시 → 없으면 자산을 fetch 후
 *   `extractWaveform` 호출 → 캐시 저장의 순서로 비동기 로드.
 * - 로드 전/실패 시 캔버스를 비워두고 placeholder 색만 깔린 트랙 막대로 보이게 한다.
 * - 파형 색상은 트랙 라벨 가독성을 위해 흰색 반투명. selectedTrack 일 때 약간 더
 *   진하게.
 */
function TrackWaveformCanvas({
  projectId,
  assetId,
  width,
  height,
}: {
  projectId: string;
  assetId: string;
  width: number;
  height: number;
}) {
  // 캔버스 너비를 그대로 peakCount 로 — 1px = 1peak. 너무 좁으면 의미가 없으므로 최소 20.
  // 너무 넓으면 캐시 분기가 자주 바뀌므로 최대 1600.
  const peakCount = Math.max(20, Math.min(1600, Math.floor(width)));
  const { peaks, status } = useTrackWaveform(projectId, assetId, peakCount);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    // 디바이스 픽셀비 적용 — 레티나에서 흐릿하지 않도록.
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.floor(width * dpr));
    canvas.height = Math.max(1, Math.floor(height * dpr));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (!peaks || peaks.length === 0) {
      // placeholder: 가로 1px 선 — 로딩 중이면 더 밝게 표시해 진행을 암시.
      ctx.fillStyle = status === "loading"
        ? "rgba(255,255,255,0.32)"
        : "rgba(255,255,255,0.18)";
      ctx.fillRect(0, Math.floor(canvas.height / 2) - 1, canvas.width, 2);
      return;
    }

    // peaks 를 캔버스 width 에 맞게 늘리거나 줄임.
    // 단순 nearest-neighbor — 평균 1~2px 라 보간 효과는 미미.
    const w = canvas.width;
    const h = canvas.height;
    const mid = h / 2;
    ctx.fillStyle = "rgba(255,255,255,0.55)";
    for (let x = 0; x < w; x++) {
      const peakIdx = Math.floor((x * peaks.length) / w);
      const v = peaks[peakIdx] ?? 0;
      const barH = Math.max(1, v * (h - 2));
      ctx.fillRect(x, mid - barH / 2, 1, barH);
    }
  }, [peaks, width, height, status]);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        width,
        height,
        pointerEvents: "none",
      }}
      aria-hidden
    />
  );
}

function TimelineRuler({ durationSec, pxPerSec }: { durationSec: number; pxPerSec: number }) {
  const stepSec = pxPerSec >= 60 ? 1 : pxPerSec >= 24 ? 5 : 10;
  const total = Math.ceil(durationSec);
  const marks: number[] = [];
  for (let s = 0; s <= total; s += stepSec) marks.push(s);
  return (
    <div style={styles.ruler}>
      {marks.map((s) => (
        <div key={s} style={{ ...styles.rulerMark, left: s * pxPerSec }}>
          <div style={styles.rulerTick} />
          <span style={styles.rulerLabel}>{formatSec(s)}</span>
        </div>
      ))}
    </div>
  );
}

function formatSec(s: number): string {
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return m > 0 ? `${m}:${String(Math.floor(r)).padStart(2, "0")}` : `${Math.floor(r)}s`;
}

function TrackInspector({
  projectId,
  track,
  audioAssets,
  videoDurationSec,
  onPatch,
  onDelete,
}: {
  projectId: string;
  track: BgmTrack;
  audioAssets: FootageAsset[];
  videoDurationSec: number;
  /** opts.localOnly 면 로컬 state 만 갱신. 슬라이더 onChange 폭주 차단용. mouseup/blur 시점에 false 호출. */
  onPatch: (patch: Partial<BgmTrack>, opts?: { localOnly?: boolean }) => void;
  onDelete: () => void;
}) {
  // 슬라이더 변경 종료(commit) 시 호출 — 마지막으로 변경한 필드만 다시 persist.
  // value 는 이미 localOnly 로 store 에 반영됐으므로, 빈 patch 로 호출해도 persist 가 현재 state 를 그대로 동기화한다.
  // 다만 updateTrack 은 patch 머지 후 setTracks/persist — track 객체 그대로 전송하면 안전.
  const commit = () => onPatch({}, { localOnly: false });
  const previewRef = useRef<HTMLAudioElement | null>(null);
  function preview() {
    if (!previewRef.current) return;
    previewRef.current.src = resolveBgmStreamUrl(projectId, track.audio_asset_id);
    previewRef.current.volume = track.volume;
    previewRef.current.currentTime = 0;
    previewRef.current.play().catch(() => {});
  }
  return (
    <div style={styles.inspector}>
      <div style={styles.inspectorHeader}>
        <span style={styles.inspectorTitle}>트랙 편집</span>
        <button style={styles.deleteBtn} onClick={onDelete}>삭제</button>
      </div>

      <label style={styles.label}>오디오 자산</label>
      <select
        style={styles.select}
        value={track.audio_asset_id}
        onChange={(e) => onPatch({ audio_asset_id: e.target.value })}
      >
        {audioAssets.length > 0 && (
          <optgroup label="스튜디오 자산">
            {audioAssets.map((a) => (
              <option key={a.asset_id} value={a.asset_id}>
                {a.label || a.filename}
              </option>
            ))}
          </optgroup>
        )}
      </select>
      <button style={styles.previewBtn} onClick={preview}>미리듣기</button>

      <label style={styles.label}>볼륨 ({Math.round(track.volume * 100)}%)</label>
      <input
        type="range" min={0} max={1} step={0.01} value={track.volume}
        // 슬라이더 폭주 차단 — 드래그 중에는 localOnly, mouseup/touchend/keyup 시점에만 persist.
        onChange={(e) => onPatch({ volume: Number(e.target.value) }, { localOnly: true })}
        onMouseUp={commit}
        onTouchEnd={commit}
        onKeyUp={commit}
        style={styles.range}
      />

      {(() => {
        const maxEnd = videoDurationSec;
        return (
          <>
            <label style={styles.label}>시작 (초)</label>
            <input
              type="number" step={0.1} min={0} max={videoDurationSec}
              value={track.start_sec.toFixed(2)}
              onChange={(e) => onPatch({ start_sec: Number(e.target.value) }, { localOnly: true })}
              onBlur={commit}
              style={styles.numberInput}
            />

            <label style={styles.label}>종료 (초)</label>
            <input
              type="number" step={0.1} min={0} max={maxEnd}
              value={track.end_sec.toFixed(2)}
              onChange={(e) => onPatch({ end_sec: Number(e.target.value) }, { localOnly: true })}
              onBlur={commit}
              style={styles.numberInput}
            />
            <div style={{ color: "#666", fontSize: 10 }}>
              현재 길이: {(track.end_sec - track.start_sec).toFixed(1)}s
            </div>
          </>
        );
      })()}

      <label style={styles.label}>페이드 인 ({track.fade_in_sec.toFixed(2)}s)</label>
      <input
        type="range" min={0} max={5} step={0.05} value={track.fade_in_sec}
        onChange={(e) => onPatch({ fade_in_sec: Number(e.target.value) }, { localOnly: true })}
        onMouseUp={commit}
        onTouchEnd={commit}
        onKeyUp={commit}
        style={styles.range}
      />

      <label style={styles.label}>페이드 아웃 ({track.fade_out_sec.toFixed(2)}s)</label>
      <input
        type="range" min={0} max={5} step={0.05} value={track.fade_out_sec}
        onChange={(e) => onPatch({ fade_out_sec: Number(e.target.value) }, { localOnly: true })}
        onMouseUp={commit}
        onTouchEnd={commit}
        onKeyUp={commit}
        style={styles.range}
      />

      <audio ref={previewRef} style={{ display: "none" }} />
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    minHeight: "100vh", background: "#0f0f0f", padding: 16,
    display: "flex", flexDirection: "column", gap: 12,
    color: "#ddd", fontFamily: "system-ui, -apple-system, sans-serif",
  },
  header: { display: "flex", alignItems: "center", gap: 12 },
  backBtn: {
    padding: "6px 12px", background: "#252525", color: "#a78bfa",
    border: "1px solid #3a2a4a", borderRadius: 6, cursor: "pointer", fontSize: 13,
  },
  title: { color: "#fff", fontWeight: 700, fontSize: 16, flex: 1 },
  duration: { color: "#888", fontSize: 12 },
  videoBox: { display: "flex", justifyContent: "center", background: "#000", padding: 4, borderRadius: 6 },
  video: { maxHeight: 360, maxWidth: "100%", background: "#000" },
  timelineContainer: {
    background: "#1a1a1a", borderRadius: 8, padding: 8,
    display: "flex", flexDirection: "column", gap: 8,
  },
  timelineControls: { display: "flex", alignItems: "center", gap: 10 },
  addBtn: {
    padding: "6px 12px", background: "#3a2a4a", color: "#a78bfa",
    border: "1px solid #4a3a5a", borderRadius: 6, cursor: "pointer", fontSize: 12,
  },
  warn: { color: "#f87171", fontSize: 12 },
  zoom: { color: "#888", fontSize: 12, marginLeft: 12 },
  timelineScroll: { overflowX: "auto", background: "#0f0f0f", borderRadius: 6 },
  timeline: {
    position: "relative",
    minHeight: 260,
    background: "#0f0f0f",
  },
  ruler: { position: "relative", height: 28, borderBottom: "1px solid #2a2a2a" },
  rulerMark: { position: "absolute", top: 0, height: 28 },
  rulerTick: { width: 1, height: 8, background: "#444" },
  rulerLabel: {
    position: "absolute", top: 10, left: 2, fontSize: 10, color: "#666",
    whiteSpace: "nowrap",
  },
  track: {
    position: "absolute", height: TRACK_HEIGHT,
    background: "rgba(167, 139, 250, 0.35)",
    border: `1px solid ${ACCENT}`, borderRadius: 4,
    display: "flex", alignItems: "center",
    cursor: "grab", overflow: "hidden",
    color: "#fff", fontSize: 11, fontWeight: 600,
  },
  trackSelected: {
    background: "rgba(167, 139, 250, 0.6)",
    boxShadow: "0 0 0 2px #fff",
  },
  trackLabel: {
    flex: 1, padding: "0 16px", overflow: "hidden",
    textOverflow: "ellipsis", whiteSpace: "nowrap", pointerEvents: "none",
  },
  handleLeft: {
    position: "absolute", left: 0, top: 0, bottom: 0, width: 8,
    cursor: "ew-resize", background: "rgba(255,255,255,0.2)",
  },
  handleRight: {
    position: "absolute", right: 0, top: 0, bottom: 0, width: 8,
    cursor: "ew-resize", background: "rgba(255,255,255,0.2)",
  },
  playhead: {
    position: "absolute", top: 28, bottom: 0, width: 2,
    background: "#ef4444", pointerEvents: "none", zIndex: 50,
  },
  inspector: {
    position: "fixed", right: 16, top: 80, width: 280,
    background: "#1a1a1a", border: "1px solid #333", borderRadius: 8,
    padding: 12, display: "flex", flexDirection: "column", gap: 6, zIndex: 100,
  },
  inspectorHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  inspectorTitle: { color: "#a78bfa", fontWeight: 700, fontSize: 13 },
  deleteBtn: {
    padding: "3px 10px", background: "#2a0a0a", border: "1px solid #500",
    color: "#f87171", borderRadius: 5, cursor: "pointer", fontSize: 11,
  },
  label: { color: "#888", fontSize: 11, marginTop: 6 },
  select: { background: "#111", border: "1px solid #333", color: "#ccc", padding: "4px 6px", fontSize: 12, borderRadius: 4 },
  previewBtn: {
    padding: "3px 10px", background: "#1a1a1a", color: "#a78bfa",
    border: "1px solid #3a2a4a", borderRadius: 5, cursor: "pointer", fontSize: 11,
    width: "fit-content", marginTop: 4,
  },
  range: { width: "100%" },
  numberInput: {
    background: "#111", border: "1px solid #333", color: "#ccc",
    padding: "4px 6px", fontSize: 12, borderRadius: 4, width: "100%",
  },
  footer: {
    background: "#1a1a1a", borderRadius: 8, padding: "10px 14px",
    display: "flex", alignItems: "center", gap: 12,
  },
  renderBtn: {
    padding: "8px 18px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  downloadLink: {
    color: "#a78bfa", fontSize: 12, textDecoration: "underline",
  },
};
