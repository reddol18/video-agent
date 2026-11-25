import { useEffect, useRef, useState } from "react";

interface Props {
  open: boolean;
  videoUrl: string | null;
  filename: string;
  // 부분 씬에서 추가 분할하는 경우, 현재 씬의 보이는 구간(절대 시간 기준).
  // 둘 다 null이면 footage 전체.
  baseStart?: number;
  baseEnd?: number | null;
  onClose: () => void;
  onApply: (cuts: number[], totalDur: number) => void;
}

/**
 * 씬 나누기 다이얼로그.
 *
 * 미니 영상 플레이어 + 가로 타임라인. 사용자가 타임라인의 임의 지점을 클릭하면 분할 마커가
 * 추가되고, 마커를 다시 클릭하면 제거된다. "적용" 버튼을 누르면 정렬된 cut 시각 리스트를
 * 부모로 전달한다.
 *
 * 좌표 규칙: 타임라인 표시 좌표는 [baseStart, baseEnd] 구간을 0~width로 매핑한다.
 * cut은 항상 footage 절대 시간(초) 기준으로 반환된다.
 */
export default function SplitSceneDialog({
  open, videoUrl, filename, baseStart, baseEnd, onClose, onApply,
}: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const [duration, setDuration] = useState<number>(0);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [cuts, setCuts] = useState<number[]>([]);

  // 모달 열릴 때 상태 초기화 — 매 호출마다 새 컷 목록.
  useEffect(() => {
    if (open) {
      setCuts([]);
      setCurrentTime(0);
    }
  }, [open]);

  // 영상 metadata 로드 시 duration 갱신. Infinity인 경우 seek 트릭으로 강제 측정.
  const handleLoadedMeta = () => {
    const v = videoRef.current;
    if (!v) return;
    if (isFinite(v.duration) && v.duration > 0) {
      setDuration(v.duration);
      // 씬 구간 시작점부터 보여준다(원본 0초가 아니라).
      if (baseStart) v.currentTime = baseStart;
    } else {
      // webm 등 duration 메타 깨짐 우회.
      const onSeeked = () => {
        const d = v.duration;
        if (isFinite(d) && d > 0) setDuration(d);
        v.currentTime = baseStart ?? 0;
        v.removeEventListener("seeked", onSeeked);
      };
      v.addEventListener("seeked", onSeeked);
      try { v.currentTime = 1e10; } catch { /* ignore */ }
    }
  };

  if (!open) return null;

  // 표시 범위 — baseStart/baseEnd가 있으면 그 구간, 없으면 전체.
  const viewStart = baseStart ?? 0;
  const viewEnd = baseEnd && baseEnd > 0 ? baseEnd : duration;
  const viewSpan = Math.max(0.001, viewEnd - viewStart);

  function toPct(t: number) {
    return Math.max(0, Math.min(100, ((t - viewStart) / viewSpan) * 100));
  }

  function pctToTime(pct: number) {
    return viewStart + (pct / 100) * viewSpan;
  }

  function handleTrackClick(e: React.MouseEvent<HTMLDivElement>) {
    const el = trackRef.current;
    if (!el || duration <= 0) return;
    const rect = el.getBoundingClientRect();
    const pct = ((e.clientX - rect.left) / rect.width) * 100;
    const t = pctToTime(pct);
    // 이미 가까운(<= 0.2s) cut이 있으면 제거, 없으면 추가.
    const THRESHOLD = Math.max(0.2, viewSpan * 0.01);
    const nearIdx = cuts.findIndex((c) => Math.abs(c - t) < THRESHOLD);
    if (nearIdx >= 0) {
      setCuts(cuts.filter((_, i) => i !== nearIdx));
    } else {
      // viewStart/viewEnd와 너무 가까우면 의미 없음 — 무시.
      if (t - viewStart < THRESHOLD || viewEnd - t < THRESHOLD) return;
      const next = [...cuts, t].sort((a, b) => a - b);
      setCuts(next);
      // 클릭 지점으로 영상 시킹
      if (videoRef.current) videoRef.current.currentTime = t;
    }
  }

  // 재생을 씬 구간 [viewStart, viewEnd] 안에 묶는다.
  function handleTimeUpdate() {
    const v = videoRef.current;
    if (!v) return;
    if (v.currentTime < viewStart - 0.05) v.currentTime = viewStart;
    else if (duration > 0 && v.currentTime >= viewEnd) {
      v.pause();
      v.currentTime = viewEnd;
    }
    setCurrentTime(v.currentTime);
  }

  function handleApply() {
    if (cuts.length === 0) {
      onClose();
      return;
    }
    onApply(cuts, duration);
  }

  // 표시는 씬 기준 상대 시간(0 ~ 씬 길이). cut 값 자체는 footage 절대 시간 유지.
  const fmt = (s: number) => `${Math.max(0, s - viewStart).toFixed(2)}s`;
  // 원본 기준 시각 — 브라우저 기본 컨트롤 표기(m:ss)와 맞춘다.
  const fmtClock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  return (
    <div style={styles.overlay} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div style={styles.modal}>
        <div style={styles.header}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <h3 style={{ margin: 0, color: "#fff", fontSize: 16 }}>씬 나누기</h3>
            <span style={{ color: "#888", fontSize: 12 }}>{filename}</span>
          </div>
          <button style={styles.closeBtn} onClick={onClose}>✕</button>
        </div>

        <div style={styles.body}>
          <div style={styles.videoWrap}>
            {videoUrl ? (
              <video
                ref={videoRef}
                src={videoUrl}
                style={styles.video}
                controls
                onLoadedMetadata={handleLoadedMeta}
                onTimeUpdate={handleTimeUpdate}
                onPlay={() => {
                  const v = videoRef.current;
                  if (v && duration > 0 && v.currentTime >= viewEnd - 0.05) v.currentTime = viewStart;
                }}
              />
            ) : (
              <div style={styles.videoPlaceholder}>영상을 불러올 수 없습니다.</div>
            )}
          </div>

          {duration > 0 && (
            <div style={styles.sourceHint}>
              ⓘ 영상 컨트롤의 재생바·시간은 <b>원본 전체({fmtClock(duration)})</b> 기준입니다.
              이 씬은 원본 {fmtClock(viewStart)}~{fmtClock(viewEnd)} 구간이며, 그 안에서만 재생됩니다.
              분할 지점은 아래 바(씬 기준 시간)에서 고르세요. 씬 내 현재 위치: <b>{fmt(currentTime)}</b>
            </div>
          )}

          <div style={styles.help}>
            타임라인을 클릭해 분할 지점을 추가/제거하세요. 현재 분할 지점 {cuts.length}개 → 씬 {cuts.length + 1}개로 나뉩니다.
          </div>

          <div style={styles.trackWrap}>
            <div ref={trackRef} style={styles.track} onClick={handleTrackClick}>
              {/* 현재 재생 위치 표시 */}
              {duration > 0 && (
                <div
                  style={{ ...styles.playhead, left: `${toPct(currentTime)}%` }}
                  title={`현재 ${fmt(currentTime)}`}
                />
              )}
              {/* 분할 마커 */}
              {cuts.map((c, i) => (
                <div
                  key={i}
                  style={{ ...styles.cutMarker, left: `${toPct(c)}%` }}
                  title={`${fmt(c)} (클릭해서 제거)`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setCuts(cuts.filter((_, j) => j !== i));
                  }}
                >
                  <span style={styles.cutLabel}>{fmt(c)}</span>
                </div>
              ))}
            </div>
            <div style={styles.trackScale}>
              <span>{fmt(viewStart)}</span>
              <span>{fmt(viewEnd)}</span>
            </div>
          </div>

          {cuts.length > 0 && (
            <div style={styles.cutList}>
              <div style={{ color: "#888", fontSize: 12, marginBottom: 6 }}>분할 지점</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {cuts.map((c, i) => (
                  <span key={i} style={styles.cutChip}>
                    {fmt(c)}
                    <button
                      type="button"
                      style={styles.cutChipX}
                      onClick={() => setCuts(cuts.filter((_, j) => j !== i))}
                    >
                      ✕
                    </button>
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        <div style={styles.footer}>
          <button style={styles.cancelBtn} onClick={onClose}>취소</button>
          <button
            style={styles.applyBtn}
            onClick={handleApply}
            disabled={cuts.length === 0}
          >
            적용 ({cuts.length + 1}개 씬)
          </button>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 },
  modal: { background: "#1a1a1a", borderRadius: 12, width: 720, maxWidth: "95vw", maxHeight: "90vh", display: "flex", flexDirection: "column", overflow: "hidden" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "16px 20px", borderBottom: "1px solid #333" },
  closeBtn: { background: "none", border: "none", color: "#888", fontSize: 18, cursor: "pointer" },
  body: { padding: 20, display: "flex", flexDirection: "column", gap: 14, overflowY: "auto" },
  videoWrap: { background: "#000", borderRadius: 8, overflow: "hidden", display: "flex", justifyContent: "center", alignItems: "center", maxHeight: 320 },
  video: { width: "100%", maxHeight: 320, display: "block" },
  videoPlaceholder: { color: "#888", padding: 40, fontSize: 13 },
  help: { color: "#aaa", fontSize: 12 },
  sourceHint: { color: "#fcd34d", background: "#2a2210", border: "1px solid #4a3b12", borderRadius: 6, padding: "6px 10px", fontSize: 12, lineHeight: 1.6 },
  trackWrap: { display: "flex", flexDirection: "column", gap: 4 },
  track: { position: "relative", height: 44, background: "#222", borderRadius: 6, cursor: "crosshair", border: "1px solid #333" },
  playhead: { position: "absolute", top: 0, bottom: 0, width: 2, background: "#a78bfa", pointerEvents: "none" },
  cutMarker: { position: "absolute", top: -4, bottom: -4, width: 2, background: "#f87171", cursor: "pointer", display: "flex", alignItems: "flex-end", justifyContent: "center" },
  cutLabel: { background: "#7f1d1d", color: "#fecaca", fontSize: 10, padding: "1px 4px", borderRadius: 3, transform: "translateY(100%)", whiteSpace: "nowrap" },
  trackScale: { display: "flex", justifyContent: "space-between", color: "#666", fontSize: 11 },
  cutList: { background: "#0f0f0f", border: "1px solid #2a2a2a", borderRadius: 6, padding: 10 },
  cutChip: { display: "inline-flex", alignItems: "center", gap: 4, background: "#1f2937", color: "#cbd5e1", fontSize: 12, padding: "3px 8px", borderRadius: 4, border: "1px solid #334155" },
  cutChipX: { background: "none", border: "none", color: "#94a3b8", cursor: "pointer", fontSize: 12, padding: 0, marginLeft: 2 },
  footer: { padding: "14px 20px", borderTop: "1px solid #333", display: "flex", justifyContent: "flex-end", gap: 10 },
  cancelBtn: { padding: "10px 18px", background: "#333", color: "#aaa", border: "none", borderRadius: 8, cursor: "pointer" },
  applyBtn: { padding: "10px 18px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, cursor: "pointer" },
};
