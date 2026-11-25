/**
 * 화면 녹화 다이얼로그 — Studio.tsx 의 RecordTab 을 별도 컴포넌트로 추출한 모듈.
 *
 * 두 가지 표시 모드를 지원한다:
 *  - mode="inline":  컨테이너 안에서 그대로 펼쳐 표시 (Studio 의 탭 안 사용).
 *  - mode="modal" :  화면 중앙 오버레이 다이얼로그 (Editor 사이드바 트리거 등에서 사용).
 *
 * onSaved 콜백은 저장이 성공한 시점에 새로 추가된 FootageAsset 배열을 받는다.
 * 호출자가 사이드바/Studio 자산 목록을 즉시 갱신할 수 있다.
 *
 * #380 — 씬 에디터에서도 화면 녹화로 footage 추가 가능하게.
 */
import { useState, useRef, useEffect, useCallback } from "react";
import type { FootageAsset } from "../types";
import { addStudioAssets } from "../lib/project";
import {
  saveAsset,
  getProjectDirectory,
  requestProjectDirectory,
} from "../lib/fileStore";
import { trimVideo } from "../lib/ffmpeg";
import { fixWebmDuration } from "../lib/webmDurationFix";

// ── 타입 / 상수 ───────────────────────────────────────────────────────────

type RecordStep = "idle" | "crop-select" | "recording" | "done";

interface CropSelection {
  x: number; y: number; w: number; h: number;
  sourceW?: number; sourceH?: number;
}

export interface TimelineSegment {
  start: number;
  end: number;
  label: string;
}

const COLORS = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4"];

function formatSec(sec: number): string {
  if (!isFinite(sec) || isNaN(sec)) return "0:00.0";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  const ds = Math.floor((sec % 1) * 10);
  return `${m}:${String(s).padStart(2, "0")}.${ds}`;
}

// 외부에서 split 등의 분할 UI 가 재사용할 수 있도록 동일 export.
export { formatSec, COLORS };

// ── ScreenRecorderDialog props ───────────────────────────────────────────

export type ScreenRecorderDialogProps = {
  open: boolean;
  projectId: string;
  onClose: () => void;
  onSaved: (assets: FootageAsset[]) => void;
  /**
   * "inline": Studio 처럼 탭 안에서 그대로 펼쳐 표시 (모달 오버레이 없음).
   * "modal" : 화면 가운데 다이얼로그로 표시 (기본). 닫기 X 버튼 표시.
   */
  mode?: "inline" | "modal";
};

export default function ScreenRecorderDialog(props: ScreenRecorderDialogProps) {
  const { open, mode = "modal" } = props;
  if (!open) return null;

  if (mode === "inline") {
    return <ScreenRecorderBody {...props} />;
  }

  // modal 모드: 화면 가운데 dialog. 오버레이 클릭 시에는 닫지 않는다 —
  // 녹화 중 실수로 닫히는 사고를 피하기 위함. 명시적 X 버튼만 닫기 핸들.
  return (
    <div style={ms.backdrop} role="dialog" aria-modal="true">
      <div style={ms.dialog}>
        <div style={ms.dialogHeader}>
          <span style={ms.dialogTitle}>화면 녹화</span>
          <button
            type="button"
            style={ms.closeBtn}
            onClick={props.onClose}
            aria-label="닫기"
            title="닫기"
          >×</button>
        </div>
        <div style={ms.dialogBody}>
          <ScreenRecorderBody {...props} />
        </div>
      </div>
    </div>
  );
}

// ── 본체 (Studio.tsx 의 RecordTab 과 동일 로직) ─────────────────────────────

function CropPreview({ stream }: { stream: MediaStream | null }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (!ref.current || !stream) return;
    ref.current.srcObject = stream;
    ref.current.play().catch(() => {});
  }, [stream]);
  return <video ref={ref} muted style={{ width: "100%", display: "block", borderRadius: 8, background: "#000" }} />;
}

function ScreenRecorderBody({
  projectId,
  onSaved,
  onClose,
  mode = "modal",
}: ScreenRecorderDialogProps) {
  const [step, setStep] = useState<RecordStep>("idle");
  const [mediaRecorder, setMediaRecorder] = useState<MediaRecorder | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const [recordedBlob, setRecordedBlob] = useState<Blob | null>(null);
  const [recordedDuration, setRecordedDuration] = useState(0);
  const [label, setLabel] = useState("");
  const [recordMode, setRecordMode] = useState<"single" | "split">("single");
  const [segments, setSegments] = useState<TimelineSegment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const streamRef = useRef<MediaStream | null>(null);
  const previewVideoRef = useRef<HTMLVideoElement>(null);
  const cropContainerRef = useRef<HTMLDivElement>(null);
  const [cropSel, setCropSel] = useState<CropSelection | null>(null);
  const cropDragRef = useRef<{ startX: number; startY: number } | null>(null);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animFrameRef = useRef<number>(0); // 크롭 canvas draw용 setInterval ID
  const chunksRef = useRef<Blob[]>([]);
  const startTimeRef = useRef<number>(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [elapsed, setElapsed] = useState(0);

  // 화면 선택 → 크롭 선택 단계
  async function handleSelectScreen() {
    setError("");
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      streamRef.current = stream;
      stream.getVideoTracks()[0].onended = () => {
        const mr = mediaRecorderRef.current;
        if (mr && mr.state !== "inactive") {
          if (animFrameRef.current) { clearInterval(animFrameRef.current); animFrameRef.current = 0; }
          if (timerRef.current) clearInterval(timerRef.current);
          mr.stop();
        } else {
          handleCancel();
        }
      };
      const surface = stream.getVideoTracks()[0]?.getSettings().displaySurface;
      if (surface === "browser") {
        // 탭 녹화: 크롭 없이 즉시 녹화 — video는 항상 DOM에 있으므로 onloadedmetadata로 시작
        const srcVideo = previewVideoRef.current!;
        srcVideo.srcObject = stream;
        srcVideo.play().catch(() => {});
        srcVideo.onloadedmetadata = () => startRecording(null);
      } else {
        // 전체화면/윈도우: 크롭 선택 단계 — previewVideoRef에 스트림 연결 후 step 전환
        const srcVideo = previewVideoRef.current!;
        srcVideo.srcObject = stream;
        srcVideo.play().catch(() => {});
        setCropSel(null);
        setStep("crop-select");
      }
    } catch (e) {
      setError(`화면 선택에 실패했습니다: ${(e as Error).message}`);
    }
  }

  // 크롭 드래그 핸들러
  function handleCropMouseDown(e: React.MouseEvent) {
    e.preventDefault();
    const rect = cropContainerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const rx = (e.clientX - rect.left) / rect.width;
    const ry = (e.clientY - rect.top) / rect.height;
    cropDragRef.current = { startX: rx, startY: ry };
    setCropSel({ x: rx, y: ry, w: 0, h: 0 });
  }

  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!cropDragRef.current || !cropContainerRef.current) return;
      const rect = cropContainerRef.current.getBoundingClientRect();
      const rx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const ry = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
      const x = Math.min(cropDragRef.current.startX, rx);
      const y = Math.min(cropDragRef.current.startY, ry);
      const w = Math.abs(rx - cropDragRef.current.startX);
      const h = Math.abs(ry - cropDragRef.current.startY);
      setCropSel({ x, y, w, h });
    }
    function onUp() { cropDragRef.current = null; }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  // 녹화 시작 — 크롭 있으면 canvas 경유, 없으면 원본 스트림 직접 녹화
  function startRecording(crop: CropSelection | null = null) {
    const stream = streamRef.current;
    if (!stream) return;

    const activeCrop = (crop && crop.w > 0.01 && crop.h > 0.01) ? crop : null;
    chunksRef.current = [];
    startTimeRef.current = Date.now();

    const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
      ? "video/webm;codecs=vp9"
      : "video/webm";

    let recordStream: MediaStream;

    if (activeCrop && activeCrop.sourceW && activeCrop.sourceH) {
      const { x, y, w, h, sourceW, sourceH } = activeCrop;
      const sx = Math.round(x * sourceW);
      const sy = Math.round(y * sourceH);
      const sw = Math.max(2, Math.round(w * sourceW));
      const sh = Math.max(2, Math.round(h * sourceH));
      const cw = sw % 2 === 0 ? sw : sw - 1;
      const ch = sh % 2 === 0 ? sh : sh - 1;

      const canvas = canvasRef.current!;
      canvas.width = cw;
      canvas.height = ch;
      const ctx = canvas.getContext("2d")!;
      const srcVideo = previewVideoRef.current!;
      // RAF는 백그라운드 탭에서 1Hz로 throttle되어 캡처가 멈춘다.
      // setInterval은 throttle을 덜 받으므로 백그라운드에서도 프레임이 그려져 webm이 끊기지 않는다.
      const drawIntervalId = window.setInterval(() => {
        try { ctx.drawImage(srcVideo, sx, sy, sw, sh, 0, 0, cw, ch); } catch { /* video not ready */ }
      }, 1000 / 30);
      // animFrameRef를 재사용하여 정리 — clearInterval은 cancelAnimationFrame과 호환되지 않으므로 별도 ref 사용
      animFrameRef.current = drawIntervalId;

      const canvasStream = canvas.captureStream(30);
      stream.getAudioTracks().forEach((t) => canvasStream.addTrack(t));
      recordStream = canvasStream;
    } else {
      recordStream = stream;
    }

    const mr = new MediaRecorder(recordStream, { mimeType });
    mr.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
    mr.onstop = () => {
      if (animFrameRef.current) { clearInterval(animFrameRef.current); animFrameRef.current = 0; }
      setTimeout(async () => {
        const rawBlob = new Blob(chunksRef.current, { type: "video/webm" });
        const elapsedMs = Date.now() - startTimeRef.current;
        // MediaRecorder webm은 Duration 헤더가 비어 있어 일부 플레이어가 길이를 잘못 계산한다.
        // 실제 경과시간으로 Segment Info > Duration을 채워 정확한 길이를 보장한다.
        const blob = await fixWebmDuration(rawBlob, elapsedMs);
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        setRecordedBlob(blob);
        setRecordedDuration(Math.round(elapsedMs / 1000));
        setPreviewUrl(URL.createObjectURL(blob));
        setStep("done");
      }, 300);
    };
    // timeslice를 1초로 키워 안정적으로 청크를 분할 — 100ms는 ondataavailable 이벤트가 폭주해
    // 중간에 일부 청크가 손실될 위험이 있다.
    mr.start(1000);
    mediaRecorderRef.current = mr;
    setMediaRecorder(mr);
    setStep("recording");
    setElapsed(0);
    timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
  }

  function stopRecording() {
    const mr = mediaRecorder;
    if (!mr) return;
    // 잔여 버퍼를 ondataavailable로 강제 flush — timeslice 경계 사이에 쌓인 데이터가
    // stop만 호출하면 누락될 수 있다.
    try { mr.requestData(); } catch { /* state 따라 불가 */ }
    mr.stop();
    if (timerRef.current) clearInterval(timerRef.current);
  }

  function handleCancel() {
    if (animFrameRef.current) { clearInterval(animFrameRef.current); animFrameRef.current = 0; }
    if (timerRef.current) clearInterval(timerRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setStep("idle");
    setError("");
  }

  async function handleSave() {
    if (!recordedBlob) return;
    if (recordMode === "single" && !label.trim()) { setError("레이블을 입력해주세요"); return; }
    if (recordMode === "split" && segments.length === 0) { setError("구간을 하나 이상 추가해주세요"); return; }
    // 폴더 미설정 시 선택 요청
    const existingDir = await getProjectDirectory(projectId);
    if (!existingDir) {
      try {
        await requestProjectDirectory(projectId);
      } catch {
        setError("저장할 폴더를 선택해주세요.");
        return;
      }
    }
    setUploading(true);
    setError("");
    try {
      const savedAssets: FootageAsset[] = [];
      if (recordMode === "single") {
        const asset = await saveAsset(projectId, recordedBlob, "recording.webm", label, "recorded");
        const p = await addStudioAssets(projectId, [asset]);
        savedAssets.push(...p.studio_assets.filter((a) => a.asset_id === asset.asset_id));
      } else {
        // 구간 분할 모드: ffmpeg.wasm으로 각 구간 trim → 저장
        for (const seg of segments) {
          const trimmed = await trimVideo(recordedBlob, seg.start, seg.end);
          const name = `recording_${seg.start.toFixed(1)}-${seg.end.toFixed(1)}.mp4`;
          const asset = await saveAsset(projectId, trimmed, name, seg.label, "split");
          savedAssets.push(asset);
        }
        if (savedAssets.length > 0) {
          const p = await addStudioAssets(projectId, savedAssets);
          savedAssets.splice(0, savedAssets.length, ...p.studio_assets.slice(-savedAssets.length));
        }
      }
      setRecordedBlob(null);
      setPreviewUrl(null);
      setLabel("");
      setSegments([]);
      setRecordMode("single");
      setStep("idle");
      onSaved(savedAssets);
      // modal 모드: 저장 직후 자동 닫기. inline 모드(Studio 탭 내부): 닫기 없이 그대로 idle 유지.
      if (mode === "modal") {
        onClose();
      }
    } catch (e) {
      setError(`저장 실패: ${(e as Error).message}`);
    } finally {
      setUploading(false);
    }
  }

  function discard() {
    setRecordedBlob(null);
    setPreviewUrl(null);
    setLabel("");
    setSegments([]);
    setRecordMode("single");
    setStep("idle");
    setCropSel(null);
    setError("");
  }

  return (
    <div style={s.tabPane}>
      {error && <div style={s.errorBox}>{error}</div>}

      {/* canvas/video — 화면 밖에 고정, display:none 쓰지 않아 재생/드로우 유지 */}
      <canvas ref={canvasRef} style={{ position: "fixed", left: -9999, top: -9999, width: 1, height: 1 }} />
      <video ref={previewVideoRef} muted style={{ position: "fixed", left: -9999, top: -9999, width: 1, height: 1 }} />

      {/* 대기 */}
      {step === "idle" && (
        <div style={s.recordCenter}>
          <button style={s.bigBtn} onClick={handleSelectScreen}>
            화면 선택
          </button>
          <p style={s.hint}>전체화면/윈도우 선택 시 녹화 영역을 드래그로 지정할 수 있습니다.</p>
        </div>
      )}

      {/* 크롭 선택 — display:none으로 숨겨도 DOM은 유지되어 previewVideoRef ref가 끊기지 않음 */}
      {(() => {
        const hasCrop = cropSel && cropSel.w > 0.01 && cropSel.h > 0.01;
        return (
          <div style={{ display: step === "crop-select" ? "block" : "none" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
              <span style={{ color: "#fff", fontWeight: 700 }}>녹화 영역 선택</span>
              <span style={{ color: "#666", fontSize: 13 }}>드래그하여 영역을 지정하거나, 지정 없이 바로 녹화하면 전체 화면이 녹화됩니다.</span>
            </div>
            <div
              ref={cropContainerRef}
              style={{ position: "relative", display: "inline-block", width: "100%", cursor: "crosshair", userSelect: "none" }}
              onMouseDown={handleCropMouseDown}
            >
              <CropPreview stream={streamRef.current} />
              {hasCrop && cropSel && (
                <>
                  {[
                    { top: 0, left: 0, right: 0, height: `${cropSel.y * 100}%` },
                    { bottom: 0, left: 0, right: 0, top: `${(cropSel.y + cropSel.h) * 100}%` },
                    { top: `${cropSel.y * 100}%`, height: `${cropSel.h * 100}%`, left: 0, width: `${cropSel.x * 100}%` },
                    { top: `${cropSel.y * 100}%`, height: `${cropSel.h * 100}%`, left: `${(cropSel.x + cropSel.w) * 100}%`, right: 0 },
                  ].map((st, i) => (
                    <div key={i} style={{ position: "absolute", background: "#00000077", pointerEvents: "none", ...st }} />
                  ))}
                  <div style={{ position: "absolute", pointerEvents: "none", boxSizing: "border-box", left: `${cropSel.x * 100}%`, top: `${cropSel.y * 100}%`, width: `${cropSel.w * 100}%`, height: `${cropSel.h * 100}%`, border: "2px solid #6366f1" }}>
                    <span style={{ position: "absolute", top: 4, left: 4, background: "#6366f1cc", color: "#fff", fontSize: 11, padding: "2px 6px", borderRadius: 4 }}>
                      {(cropSel.w * 100).toFixed(0)}×{(cropSel.h * 100).toFixed(0)}%
                    </span>
                  </div>
                </>
              )}
            </div>
            <div style={{ display: "flex", gap: 10, marginTop: 14 }}>
              <button style={s.bigBtn} onClick={() => {
                const v = previewVideoRef.current;
                const crop = hasCrop && cropSel ? {
                  ...cropSel,
                  sourceW: v?.videoWidth || undefined,
                  sourceH: v?.videoHeight || undefined,
                } : null;
                startRecording(crop);
              }}>
                {hasCrop ? "선택 영역 녹화 시작" : "전체 화면 녹화 시작"}
              </button>
              {hasCrop && (
                <button style={s.discardBtn} onClick={() => setCropSel(null)}>영역 초기화</button>
              )}
              <button style={s.discardBtn} onClick={handleCancel}>취소</button>
            </div>
          </div>
        );
      })()}

      {/* 녹화 중 */}
      {step === "recording" && (
        <div style={s.recordCenter}>
          <div style={s.recordingIndicator}>
            <span style={s.dot} /> 녹화 중... {elapsed}초
          </div>
          <button style={{ ...s.bigBtn, background: "#ef4444" }} onClick={stopRecording}>
            녹화 중지
          </button>
          <p style={{ color: "#f59e0b", fontSize: 13, marginTop: 12 }}>
            ⚠ 반드시 위 "녹화 중지" 버튼을 눌러 종료하세요.<br />
            캡처 대상 창의 "공유 중지"를 누르면 녹화가 정상 저장되지 않습니다.
          </p>
        </div>
      )}

      {/* 4단계: 완료 */}
      {step === "done" && recordedBlob && (
        <div style={s.saveArea}>
          <div style={s.modeRow}>
            <span style={s.modeLabel}>저장 방법:</span>
            <button style={{ ...s.modeBtn, ...(recordMode === "single" ? s.modeBtnActive : {}) }} onClick={() => setRecordMode("single")}>바로 저장</button>
            <button style={{ ...s.modeBtn, ...(recordMode === "split" ? s.modeBtnActive : {}) }} onClick={() => setRecordMode("split")}>구간 분할</button>
          </div>

          {recordMode === "single" && (
            <>
              <video src={previewUrl ?? undefined} controls style={s.preview} />
              <div style={s.labelRow}>
                <label style={s.labelText}>레이블</label>
                <input
                  style={s.input}
                  placeholder="예: 블렌더 UI 소개 장면"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                />
              </div>
            </>
          )}

          {recordMode === "split" && previewUrl && (
            <div style={{ marginBottom: 20 }}>
              <p style={{ ...s.hint, marginBottom: 8 }}>타임라인을 드래그하여 구간을 선택하고 레이블을 입력하세요</p>
              <VideoTimeline
                videoUrl={previewUrl}
                segments={segments}
                onSegmentsChange={setSegments}
                hintDuration={recordedDuration > 0 ? recordedDuration : undefined}
              />
            </div>
          )}

          <div style={s.actionRow}>
            <button style={s.saveBtn} onClick={handleSave} disabled={uploading}>
              {uploading ? "저장 중..." : "저장"}
            </button>
            <button style={s.discardBtn} onClick={discard} disabled={uploading}>
              취소
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── WYSIWYG 타임라인 구간 분할 컴포넌트 ─────────────────────────────────────
//
// Studio 의 AssetsTab (이미 저장된 영상을 N등분으로 다시 자르기) 에서도 동일 컴포넌트를
// 재사용하기 위해 export 한다.

// 구간 분할 플레이어 배속 선택지. 16 은 Chrome <video>.playbackRate 상한.
// (브라우저 기본 컨트롤 메뉴는 2배속까지만 제공하므로 별도 선택지를 둔다.)
const TIMELINE_RATES: number[] = [0.5, 1, 1.5, 2, 3, 4, 8, 16];

export function VideoTimeline({
  videoUrl,
  segments,
  onSegmentsChange,
  hintDuration,
}: {
  videoUrl: string;
  segments: TimelineSegment[];
  onSegmentsChange: (segs: TimelineSegment[]) => void;
  hintDuration?: number;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const barRef = useRef<HTMLDivElement>(null);

  // 영상 로드 후 실제 duration 사용; hintDuration은 webm blob처럼 메타데이터에서 읽기 어려울 때 폴백
  const [duration, setDuration] = useState(hintDuration ?? 0);
  const [currentTime, setCurrentTime] = useState(0);
  // 재생 배속 — 영상을 바꿔도 유지되도록 localStorage 에 저장.
  const [rate, setRate] = useState<number>(() => {
    try {
      const saved = parseFloat(localStorage.getItem("split_play_rate") || "");
      return TIMELINE_RATES.includes(saved) ? saved : 1;
    } catch {
      return 1;
    }
  });
  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = rate;
  }, [rate]);
  function changeRate(r: number) {
    setRate(r);
    try { localStorage.setItem("split_play_rate", String(r)); } catch { /* ignore */ }
  }

  // hintDuration이 제공되면 메타데이터 로드 전에도 타임라인 표시
  useEffect(() => {
    if (hintDuration && hintDuration > 0) setDuration(hintDuration);
  }, [hintDuration]);

  // webm Infinity duration 확정 seek 진행 중 플래그
  const seekingForDuration = useRef(false);

  // 드래그 상태
  const dragStartRef = useRef<number | null>(null);
  const [dragRange, setDragRange] = useState<{ start: number; end: number } | null>(null);
  const isDraggingRef = useRef(false);

  // 선택 완료된 구간 (레이블 입력 대기)
  const [pendingSeg, setPendingSeg] = useState<{ start: number; end: number } | null>(null);
  const [pendingLabel, setPendingLabel] = useState("");

  // 레이블 인라인 편집
  const [editIdx, setEditIdx] = useState<number | null>(null);
  const [editLabel, setEditLabel] = useState("");

  const pct = useCallback((sec: number) =>
    `${(sec / Math.max(duration, 0.001)) * 100}%`, [duration]);

  const secFromClientX = useCallback((clientX: number) => {
    const bar = barRef.current;
    if (!bar || duration === 0) return 0;
    const rect = bar.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return parseFloat((ratio * duration).toFixed(2));
  }, [duration]);

  // 마우스다운: 드래그 시작
  function handleMouseDown(e: React.MouseEvent) {
    if (duration === 0) return;
    e.preventDefault();
    const sec = secFromClientX(e.clientX);
    dragStartRef.current = sec;
    isDraggingRef.current = false;
    setDragRange({ start: sec, end: sec });
    setPendingSeg(null);
  }

  // 전역 mousemove/mouseup
  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (dragStartRef.current === null) return;
      const sec = secFromClientX(e.clientX);
      const diff = Math.abs(sec - dragStartRef.current);
      if (diff > 0.1) isDraggingRef.current = true;
      setDragRange({ start: dragStartRef.current, end: sec });
    }

    function onUp(e: MouseEvent) {
      if (dragStartRef.current === null) return;
      const sec = secFromClientX(e.clientX);

      if (!isDraggingRef.current) {
        // 단순 클릭 → seek
        if (videoRef.current) videoRef.current.currentTime = sec;
        setDragRange(null);
      } else {
        // 드래그 완료 → end 지점으로 seek + pending 구간으로 전환
        const start = Math.min(dragStartRef.current, sec);
        const end = Math.max(dragStartRef.current, sec);
        if (videoRef.current) videoRef.current.currentTime = end;
        if (end - start >= 0.2) {
          setPendingSeg({ start, end });
          setPendingLabel("");
        }
        setDragRange(null);
      }

      dragStartRef.current = null;
      isDraggingRef.current = false;
    }

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [secFromClientX]);

  // pending 구간 클릭 → 확정
  function confirmPending() {
    if (!pendingSeg) return;
    onSegmentsChange([...segments, { ...pendingSeg, label: pendingLabel }]);
    setPendingSeg(null);
    setPendingLabel("");
  }

  function removeSegment(i: number) {
    onSegmentsChange(segments.filter((_, idx) => idx !== i));
    if (editIdx === i) setEditIdx(null);
  }

  function saveEdit(i: number) {
    onSegmentsChange(segments.map((seg, idx) => idx === i ? { ...seg, label: editLabel } : seg));
    setEditIdx(null);
  }

  // 드래그 중 표시할 범위
  const dragDisplay = dragRange
    ? { start: Math.min(dragRange.start, dragRange.end), end: Math.max(dragRange.start, dragRange.end) }
    : null;

  return (
    <div style={{ userSelect: "none" }}>
      {/* 영상 플레이어 */}
      <video
        ref={videoRef}
        src={videoUrl}
        controls
        preload="metadata"
        style={{ width: "100%", maxHeight: 320, borderRadius: 8, background: "#000", display: "block" }}
        onLoadedMetadata={() => {
          const vid = videoRef.current;
          if (!vid) return;
          // src 가 바뀌면 playbackRate 가 1 로 돌아가므로 다시 건다.
          vid.playbackRate = rate;
          if (isFinite(vid.duration) && vid.duration > 0) {
            setDuration(vid.duration);
          } else {
            // webm Infinity 대응: 끝으로 seek하면 duration이 확정됨
            seekingForDuration.current = true;
            vid.currentTime = 1e9;
          }
        }}
        onSeeked={() => {
          const vid = videoRef.current;
          if (!vid) return;
          if (seekingForDuration.current) {
            seekingForDuration.current = false;
            if (isFinite(vid.duration) && vid.duration > 0) {
              setDuration(vid.duration);
            }
            vid.currentTime = 0;
          }
        }}
        onRateChange={() => {
          const r = videoRef.current?.playbackRate;
          if (r && r !== rate) setRate(r);
        }}
        onDurationChange={() => {
          const d = videoRef.current?.duration ?? 0;
          if (isFinite(d) && d > 0) setDuration(d);
        }}
        onTimeUpdate={() => {
          const vid = videoRef.current;
          if (!vid) return;
          setCurrentTime(vid.currentTime);
          // webm은 duration이 Infinity일 수 있음 — currentTime 최댓값으로 추정
          if (!isFinite(vid.duration) && vid.currentTime > 0) {
            setDuration((prev) => Math.max(prev, vid.currentTime + 1));
          }
        }}
      />

      {/* 배속 — 기본 컨트롤 메뉴(최대 2배)로 바꾼 값도 onRateChange 로 따라온다. */}
      <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 4, marginTop: 6 }}>
        <span style={{ color: "#666", fontSize: 11, marginRight: 2 }}>배속</span>
        {TIMELINE_RATES.map((r) => (
          <button
            key={r}
            onClick={() => changeRate(r)}
            style={{
              background: rate === r ? "#4f46e5" : "#1e1e1e",
              color: rate === r ? "#fff" : r === 1 ? "#93c5fd" : "#aaa",
              border: `1px solid ${rate === r ? "#6366f1" : "#333"}`,
              borderRadius: 4, padding: "2px 7px", fontSize: 11, cursor: "pointer",
              fontWeight: rate === r ? 700 : 400,
            }}
          >
            {r}×
          </button>
        ))}
      </div>

      {duration === 0 && (
        <p style={{ color: "#555", fontSize: 12, textAlign: "center", marginTop: 8 }}>
          영상을 로드하는 중...
        </p>
      )}

      {duration > 0 && (
        <>
          {/* 타임라인 바 */}
          <div style={{ marginTop: 10 }}>
            <div
              ref={barRef}
              style={{
                position: "relative", height: 56, background: "#1a1a1a",
                borderRadius: 6, cursor: duration > 0 ? "crosshair" : "default",
                border: "1px solid #333", overflow: "hidden",
              }}
              onMouseDown={handleMouseDown}
            >
              {/* 확정된 구간들 */}
              {segments.map((seg, i) => (
                <div
                  key={i}
                  style={{
                    position: "absolute", top: 6, bottom: 6,
                    left: pct(seg.start), width: pct(seg.end - seg.start),
                    background: COLORS[i % COLORS.length] + "50",
                    border: `1.5px solid ${COLORS[i % COLORS.length]}`,
                    borderRadius: 4, display: "flex", alignItems: "center",
                    justifyContent: "center", overflow: "hidden", pointerEvents: "none",
                  }}
                >
                  <span style={{ color: "#fff", fontSize: 11, padding: "0 4px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {seg.label || `구간 ${i + 1}`}
                  </span>
                </div>
              ))}

              {/* 드래그 중 범위 */}
              {dragDisplay && dragDisplay.end - dragDisplay.start > 0.05 && (
                <div style={{
                  position: "absolute", top: 6, bottom: 6,
                  left: pct(dragDisplay.start), width: pct(dragDisplay.end - dragDisplay.start),
                  background: "#ffffff18", border: "2px dashed #aaa",
                  borderRadius: 4, pointerEvents: "none",
                }} />
              )}

              {/* pending 구간 (클릭 대기) */}
              {pendingSeg && (
                <div
                  style={{
                    position: "absolute", top: 6, bottom: 6,
                    left: pct(pendingSeg.start), width: pct(pendingSeg.end - pendingSeg.start),
                    background: "#6366f133", border: "2px solid #6366f1",
                    borderRadius: 4, cursor: "pointer",
                    display: "flex", alignItems: "center", justifyContent: "center",
                  }}
                  onClick={(e) => { e.stopPropagation(); }}
                >
                  <span style={{ color: "#a5b4fc", fontSize: 11 }}>▼ 레이블 입력</span>
                </div>
              )}

              {/* 재생 위치 커서 */}
              <div style={{
                position: "absolute", top: 0, bottom: 0, width: 2,
                background: "#fff", left: pct(currentTime), pointerEvents: "none",
                boxShadow: "0 0 4px #fff8",
              }} />
            </div>

            {/* 시간 눈금 */}
            <div style={{ display: "flex", justifyContent: "space-between", marginTop: 3 }}>
              {[0, 0.25, 0.5, 0.75, 1].map((r) => (
                <span key={r} style={{ color: "#444", fontSize: 11 }}>
                  {formatSec(r * duration)}
                </span>
              ))}
            </div>
          </div>

          {/* pending 구간 레이블 입력 */}
          {pendingSeg && (
            <div style={{
              marginTop: 10, display: "flex", gap: 8, alignItems: "center",
              background: "#12122a", padding: "10px 14px", borderRadius: 8,
              border: "1.5px solid #6366f1",
            }}>
              <span style={{ color: "#a5b4fc", fontSize: 13, flexShrink: 0, minWidth: 120 }}>
                {formatSec(pendingSeg.start)} ~ {formatSec(pendingSeg.end)}
              </span>
              <input
                autoFocus
                style={{ ...tls.input, flex: 1 }}
                placeholder="이 구간 레이블 (예: 블렌더 UV 작업)"
                value={pendingLabel}
                onChange={(e) => setPendingLabel(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") confirmPending();
                  if (e.key === "Escape") setPendingSeg(null);
                }}
              />
              <button style={tls.confirmBtn} onClick={confirmPending}>구간 추가</button>
              <button style={tls.cancelSmBtn} onClick={() => setPendingSeg(null)}>취소</button>
            </div>
          )}

          {/* 확정된 구간 목록 */}
          {segments.length > 0 && (
            <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 5 }}>
              {segments.map((seg, i) => (
                <div key={i} style={{
                  display: "flex", alignItems: "center", gap: 10,
                  background: "#1a1a1a", borderRadius: 7, padding: "7px 12px",
                }}>
                  <div style={{ width: 10, height: 10, borderRadius: 2, background: COLORS[i % COLORS.length], flexShrink: 0 }} />
                  <span style={{ color: "#6366f1", fontSize: 12, minWidth: 120, flexShrink: 0 }}>
                    {formatSec(seg.start)} ~ {formatSec(seg.end)}
                  </span>
                  {editIdx === i ? (
                    <>
                      <input
                        autoFocus
                        style={{ ...tls.input, flex: 1 }}
                        value={editLabel}
                        onChange={(e) => setEditLabel(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") saveEdit(i);
                          if (e.key === "Escape") setEditIdx(null);
                        }}
                      />
                      <button style={tls.confirmBtn} onClick={() => saveEdit(i)}>저장</button>
                      <button style={tls.cancelSmBtn} onClick={() => setEditIdx(null)}>×</button>
                    </>
                  ) : (
                    <>
                      <span
                        style={{ flex: 1, color: seg.label ? "#a5b4fc" : "#555", fontSize: 13, cursor: "pointer", fontStyle: seg.label ? "normal" : "italic" }}
                        onClick={() => { setEditIdx(i); setEditLabel(seg.label); }}
                      >
                        {seg.label || "클릭하여 레이블 입력"}
                      </span>
                      <button style={tls.removeBtn} onClick={() => removeSegment(i)}>×</button>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// VideoTimeline 내부에서 사용하는 작은 컨트롤(레이블 input / 확정 / 취소 / 제거) 공통 스타일.
// Studio.tsx 의 AssetsTab 분할 UI 에서도 동일 스타일을 재사용하기 위해 named export.
export const tls: Record<string, React.CSSProperties> = {
  input: { background: "#111", border: "1px solid #333", borderRadius: 6, color: "#fff", padding: "6px 10px", fontSize: 13, outline: "none" },
  confirmBtn: { padding: "6px 14px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 13, flexShrink: 0 },
  cancelSmBtn: { padding: "6px 10px", background: "none", color: "#888", border: "1px solid #333", borderRadius: 6, cursor: "pointer", fontSize: 13, flexShrink: 0 },
  removeBtn: { background: "none", border: "none", color: "#555", cursor: "pointer", fontSize: 20, padding: "0 4px", flexShrink: 0 },
};

// ── 스타일 ─────────────────────────────────────────────────────────────────
// Studio.tsx 의 `s` 스타일에서 RecordTab 이 사용하던 항목만 발췌해 자체 보관.
// (다른 탭 전용 스타일은 옮기지 않는다.)
const s: Record<string, React.CSSProperties> = {
  tabPane: { maxWidth: 860, margin: "0 auto" },
  errorBox: {
    background: "#2a1a1a", border: "1px solid #5a2a2a", color: "#f87171",
    borderRadius: 8, padding: "10px 14px", marginBottom: 16, fontSize: 14,
  },
  recordCenter: { textAlign: "center" as const, padding: "60px 0" },
  bigBtn: {
    padding: "16px 36px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 10, fontSize: 16, cursor: "pointer", fontWeight: 600,
  },
  hint: { color: "#666", fontSize: 13, marginTop: 12 },
  recordingIndicator: {
    display: "flex", alignItems: "center", justifyContent: "center",
    gap: 8, color: "#f87171", fontWeight: 700, fontSize: 18, marginBottom: 20,
  },
  dot: {
    display: "inline-block", width: 10, height: 10,
    borderRadius: "50%", background: "#ef4444",
    animation: "pulse 1s infinite",
  },
  saveArea: {},
  preview: { width: "100%", maxHeight: 360, borderRadius: 8, marginBottom: 20, background: "#000" },
  modeRow: { display: "flex", alignItems: "center", gap: 10, marginBottom: 16 },
  modeLabel: { color: "#aaa", fontSize: 14 },
  modeBtn: {
    padding: "7px 18px", background: "#222", color: "#888",
    border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  modeBtnActive: { background: "#6366f1", color: "#fff", borderColor: "#6366f1" },
  labelRow: { display: "flex", alignItems: "center", gap: 12, marginBottom: 20 },
  labelText: { color: "#aaa", fontSize: 14, flexShrink: 0 },
  input: {
    background: "#1a1a1a", border: "1px solid #333", borderRadius: 7,
    color: "#fff", padding: "8px 12px", fontSize: 14, outline: "none",
  },
  actionRow: { display: "flex", gap: 10 },
  saveBtn: {
    padding: "12px 28px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 8, cursor: "pointer", fontSize: 15, fontWeight: 600,
  },
  discardBtn: {
    padding: "12px 20px", background: "#2a1a1a", color: "#f87171",
    border: "1px solid #3a1a1a", borderRadius: 8, cursor: "pointer", fontSize: 15,
  },
};

// modal wrapper 스타일.
const ms: Record<string, React.CSSProperties> = {
  backdrop: {
    position: "fixed", inset: 0, background: "rgba(0,0,0,0.72)",
    display: "flex", alignItems: "center", justifyContent: "center",
    zIndex: 1000, padding: 16,
  },
  dialog: {
    background: "#141414", border: "1px solid #2a2a2a", borderRadius: 12,
    width: "min(960px, 100%)", maxHeight: "calc(100vh - 64px)",
    display: "flex", flexDirection: "column" as const,
    boxShadow: "0 20px 60px rgba(0,0,0,0.5)",
  },
  dialogHeader: {
    display: "flex", alignItems: "center", justifyContent: "space-between",
    padding: "14px 20px", borderBottom: "1px solid #222",
  },
  dialogTitle: { color: "#fff", fontWeight: 700, fontSize: 15 },
  closeBtn: {
    background: "none", border: "none", color: "#888", fontSize: 24,
    cursor: "pointer", lineHeight: 1, padding: "0 6px",
  },
  dialogBody: {
    padding: "20px 24px",
    overflowY: "auto" as const,
    flex: 1,
  },
};
