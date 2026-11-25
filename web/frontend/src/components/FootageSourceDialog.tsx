/**
 * FootageSourceDialog (#362 / #365) — 씬 소스 영상(footage) 변경 모달.
 *
 * 좌측: 프로젝트에 등록된 footage 그리드 (썸네일 + 파일명 + 길이).
 * 우측: 선택한 footage 미리보기 + clip_start/end 트리밍 슬라이더 (#365).
 *
 * 검색: 파일명·레이블 부분 일치 (대소문자 무시).
 * 정렬: "현재 매칭된 후보 우선" 옵션 — 현재 씬의 footage.file 과 같은 파일을 맨 앞에.
 *
 * 적용 시 onApply(asset, trim?) 콜백을 호출하여 부모에서 footage 재배정 흐름을 실행한다.
 * trim 이 주어지면 footage(clip).clip_start_sec / clip_end_sec 으로 기록되어 백엔드가 해당 구간만 잘라 사용한다.
 * 외부 영역 클릭/ESC 로 취소된다.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { FootageAsset, FootageItem, SceneCard } from "../types";
import LocalThumbnail from "./LocalThumbnail";
import ServerFootageThumbnail from "./ServerFootageThumbnail";

export interface FootageTrimRange {
  /** 시작 sec (>= 0). */
  start: number;
  /** 끝 sec (<= duration). */
  end: number;
}

export interface FootageSourceDialogProps {
  projectId: string;
  scene: SceneCard;
  assets: FootageAsset[];
  /** footage 메타(길이/file_type) — assets 와 매핑하여 표시. */
  footageItems: FootageItem[];
  /**
   * trim 이 주어지면 footage(clip) 의 clip_start_sec / clip_end_sec 으로 기록되어
   * 백엔드가 해당 구간만 잘라 사용한다. 이미지 자산이면 trim 은 무시된다.
   */
  onApply: (asset: FootageAsset, trim?: FootageTrimRange) => void;
  onClose: () => void;
}

export default function FootageSourceDialog({
  projectId,
  scene,
  assets,
  footageItems,
  onApply,
  onClose,
}: FootageSourceDialogProps) {
  const [query, setQuery] = useState("");
  const currentFilename = scene.footage
    ? ("file" in scene.footage
        ? (scene.footage as { file?: string }).file ?? null
        : "image" in scene.footage
          ? (scene.footage as unknown as { image?: string }).image ?? null
          : null)
    : null;
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(() => {
    if (!currentFilename) return null;
    const match = assets.find((a) => a.filename === currentFilename);
    return match?.asset_id ?? null;
  });

  // ESC 닫기.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // footage 길이 메타 매핑.
  const itemByFilename = useMemo(() => {
    const m = new Map<string, FootageItem>();
    for (const f of footageItems) m.set(f.filename, f);
    return m;
  }, [footageItems]);

  // 정렬: 현재 파일 → 영상(비-오디오) → 오디오 제외.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = assets.filter((a) => {
      if (a.asset_type === "audio") return false; // 오디오는 footage 가 아님.
      if (!q) return true;
      return (
        a.filename.toLowerCase().includes(q) ||
        (a.label ?? "").toLowerCase().includes(q)
      );
    });
    // "현재 매칭된 후보 우선" — currentFilename 과 같은 항목을 맨 앞에.
    return matched.sort((a, b) => {
      const aCur = a.filename === currentFilename ? 0 : 1;
      const bCur = b.filename === currentFilename ? 0 : 1;
      if (aCur !== bCur) return aCur - bCur;
      return (a.label || a.filename).localeCompare(b.label || b.filename);
    });
  }, [assets, query, currentFilename]);

  const selectedAsset = filtered.find((a) => a.asset_id === selectedAssetId) ?? null;
  const selectedItem = selectedAsset ? itemByFilename.get(selectedAsset.filename) ?? null : null;
  const isImage = selectedAsset?.asset_type === "image";
  const previewUrl = selectedAsset
    ? `/api/projects/${projectId}/footage/${encodeURIComponent(selectedAsset.filename)}/stream`
    : null;

  const fmtDur = (s: number): string =>
    s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`;

  // ── #365 트리밍 ─────────────────────────────────────────────────────────
  // 비디오 길이: selectedItem.duration_sec 우선, 0/없으면 video onLoadedMetadata 가 보정.
  const initialDuration = selectedItem?.duration_sec && selectedItem.duration_sec > 0
    ? selectedItem.duration_sec
    : 0;
  const [videoDuration, setVideoDuration] = useState<number>(initialDuration);
  // selectedAsset 이 바뀌면 트리밍을 자산 전체로 초기화.
  const [trimStart, setTrimStart] = useState<number>(0);
  const [trimEnd, setTrimEnd] = useState<number>(initialDuration);
  // 사용자가 슬라이더를 직접 한 번이라도 움직였는지 추적 — true 이면 onApply 에 trim 을 동봉한다.
  const [trimEdited, setTrimEdited] = useState<boolean>(false);

  // 자산 변경 시 트리밍 상태 초기화.
  useEffect(() => {
    const d = selectedItem?.duration_sec && selectedItem.duration_sec > 0 ? selectedItem.duration_sec : 0;
    setVideoDuration(d);
    setTrimStart(0);
    setTrimEnd(d);
    setTrimEdited(false);
  }, [selectedAssetId]);  // eslint-disable-line react-hooks/exhaustive-deps

  // 미리보기 video 의 currentTime 을 슬라이더 변경 시점에 점프시켜 시각화한다.
  const previewVideoRef = useRef<HTMLVideoElement>(null);
  // 활성 핸들 추적 ref(`activeHandleRef`) 는 #371 클린업으로 제거되었다.
  // — onChange 가 직접 seekPreviewTo(v) 를 호출하므로 어떤 핸들인지 별도로 보관할 필요 없음.

  function seekPreviewTo(sec: number) {
    const v = previewVideoRef.current;
    if (!v) return;
    if (!isFinite(v.duration) || v.duration === 0) return;
    try { v.currentTime = Math.max(0, Math.min(v.duration, sec)); } catch { /* ignore */ }
  }

  function handleApply() {
    if (!selectedAsset) return;
    // 이미지 자산은 trim 무의미. 비디오 + 사용자가 트림을 만졌고 범위가 자산 전체와 다르면 동봉.
    let trim: FootageTrimRange | undefined = undefined;
    if (!isImage && trimEdited && videoDuration > 0) {
      // 안전 클램프 — start <= end, start >= 0, end <= duration.
      const s = Math.max(0, Math.min(trimStart, videoDuration));
      const e = Math.max(s, Math.min(trimEnd, videoDuration));
      // 전체 범위와 사실상 동일하면(0~duration) trim 없이 패스 — 백엔드 분기 단순화.
      const isFullRange = s <= 0.001 && Math.abs(e - videoDuration) < 0.05;
      if (!isFullRange) {
        trim = { start: s, end: e };
      }
    }
    onApply(selectedAsset, trim);
    onClose();
  }

  return (
    <div style={styles.backdrop} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={styles.modal} role="dialog" aria-label="소스 영상 변경">
        <div style={styles.header}>
          <span style={styles.title}>소스 영상 변경 — 씬 {scene.scene_id}</span>
          <button type="button" style={styles.closeBtn} onClick={onClose} title="닫기 (Esc)">×</button>
        </div>

        <div style={styles.body}>
          {/* 좌: 그리드 + 검색 */}
          <div style={styles.left}>
            <input
              type="text"
              style={styles.search}
              placeholder="파일명·레이블 검색…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
            <div style={styles.grid}>
              {filtered.map((a) => {
                const item = itemByFilename.get(a.filename);
                const isCurrent = a.filename === currentFilename;
                const isSelected = a.asset_id === selectedAssetId;
                return (
                  <button
                    key={a.asset_id}
                    type="button"
                    style={{
                      ...styles.gridItem,
                      borderColor: isSelected ? "#6366f1" : isCurrent ? "#facc15" : "#333",
                      background: isSelected ? "#1d1d3a" : "#161616",
                    }}
                    onClick={() => setSelectedAssetId(a.asset_id)}
                    onDoubleClick={() => { setSelectedAssetId(a.asset_id); handleApply(); }}
                    title={a.filename}
                  >
                    <div style={styles.thumb}>
                      {a.origin === "local" && a.localFile ? (
                        <LocalThumbnail file={a.localFile} alt={a.filename} style={styles.thumbImg} />
                      ) : (
                        <ServerFootageThumbnail
                          projectId={projectId}
                          filename={a.filename}
                          assetType={a.asset_type}
                          alt={a.filename}
                          style={styles.thumbImg}
                        />
                      )}
                      {item && item.duration_sec > 0 && (
                        <span style={styles.durBadge}>{fmtDur(item.duration_sec)}</span>
                      )}
                      {isCurrent && <span style={styles.curBadge}>현재</span>}
                    </div>
                    <div style={styles.label}>{a.label || a.filename.replace(/\.[^.]+$/, "")}</div>
                  </button>
                );
              })}
              {filtered.length === 0 && (
                <div style={styles.empty}>일치하는 footage가 없습니다.</div>
              )}
            </div>
          </div>

          {/* 우: 미리보기 + 적용 */}
          <div style={styles.right}>
            {selectedAsset ? (
              <>
                <div style={styles.previewLabel}>{selectedAsset.label || selectedAsset.filename}</div>
                <div style={styles.previewBox}>
                  {previewUrl && (isImage ? (
                    <img src={previewUrl} alt={selectedAsset.filename} style={styles.previewMedia} />
                  ) : (
                    <video
                      key={previewUrl}
                      ref={previewVideoRef}
                      src={previewUrl}
                      style={styles.previewMedia}
                      controls
                      muted
                      preload="metadata"
                      // #365 — duration_sec 메타가 없거나 0 인 경우 video 메타에서 보정.
                      onLoadedMetadata={(e) => {
                        const v = e.currentTarget;
                        if (!isFinite(v.duration) || v.duration === 0) return;
                        // 자산 메타와 다르면 video.duration 을 진실의 원천으로 사용.
                        if (videoDuration <= 0) {
                          setVideoDuration(v.duration);
                          setTrimEnd(v.duration);
                        }
                      }}
                    />
                  ))}
                </div>
                {/* #365 — clip_start/end 트리밍 슬라이더. 비디오 자산일 때만 표시. */}
                {!isImage && videoDuration > 0 && (
                  <div style={styles.trim}>
                    <div style={styles.trimHeader}>
                      <span style={styles.trimLabel}>구간 트리밍</span>
                      <span style={styles.trimValues}>
                        {fmtDur(trimStart)} ~ {fmtDur(trimEnd)}
                        <span style={styles.trimDur}> ({fmtDur(Math.max(0, trimEnd - trimStart))})</span>
                      </span>
                      {trimEdited && (
                        <button
                          type="button"
                          style={styles.trimReset}
                          onClick={() => {
                            setTrimStart(0);
                            setTrimEnd(videoDuration);
                            setTrimEdited(false);
                          }}
                          title="처음 ~ 끝까지로 되돌리기"
                        >
                          초기화
                        </button>
                      )}
                    </div>
                    <div style={styles.trimRow}>
                      <label style={styles.trimSliderLabel}>시작</label>
                      <input
                        type="range"
                        min={0}
                        max={videoDuration}
                        step={0.1}
                        value={trimStart}
                        onChange={(e) => {
                          let v = parseFloat(e.target.value);
                          if (!isFinite(v)) v = 0;
                          // 끝보다 커지지 않도록 클램프.
                          v = Math.min(v, Math.max(0, trimEnd - 0.1));
                          setTrimStart(v);
                          setTrimEdited(true);
                          seekPreviewTo(v);
                        }}
                        style={styles.trimSlider}
                      />
                    </div>
                    <div style={styles.trimRow}>
                      <label style={styles.trimSliderLabel}>끝</label>
                      <input
                        type="range"
                        min={0}
                        max={videoDuration}
                        step={0.1}
                        value={trimEnd}
                        onChange={(e) => {
                          let v = parseFloat(e.target.value);
                          if (!isFinite(v)) v = videoDuration;
                          // 시작보다 작아지지 않도록 클램프.
                          v = Math.max(v, trimStart + 0.1);
                          setTrimEnd(v);
                          setTrimEdited(true);
                          seekPreviewTo(v);
                        }}
                        style={styles.trimSlider}
                      />
                    </div>
                  </div>
                )}
                <div style={styles.meta}>
                  <div><b>파일</b>: {selectedAsset.filename}</div>
                  {selectedItem && selectedItem.duration_sec > 0 && (
                    <div><b>길이</b>: {fmtDur(selectedItem.duration_sec)}</div>
                  )}
                  <div><b>유형</b>: {selectedAsset.asset_type}</div>
                </div>
                <div style={styles.actions}>
                  <button type="button" style={styles.cancelBtn} onClick={onClose}>취소</button>
                  <button
                    type="button"
                    style={styles.applyBtn}
                    onClick={handleApply}
                    // 동일 파일이라도 trim 만 바꾸는 경우는 적용 허용 (#365).
                    disabled={selectedAsset.filename === currentFilename && !trimEdited}
                    title={selectedAsset.filename === currentFilename && !trimEdited
                      ? "현재 씬과 동일한 footage입니다 — 트리밍을 변경하면 적용 가능합니다"
                      : "이 footage로 씬의 소스 영상을 교체합니다"}
                  >
                    이 영상으로 적용
                  </button>
                </div>
                <div style={styles.hint}>
                  팁: 더블클릭하면 바로 적용됩니다.
                </div>
              </>
            ) : (
              <div style={styles.empty}>좌측에서 footage를 선택하세요.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  backdrop: {
    position: "fixed" as const,
    inset: 0,
    background: "rgba(0,0,0,0.7)",
    zIndex: 2000,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
  },
  modal: {
    width: "min(1100px, 96vw)",
    height: "min(700px, 92vh)",
    background: "#0d0d0d",
    border: "1px solid #333",
    borderRadius: 8,
    display: "flex",
    flexDirection: "column" as const,
    overflow: "hidden",
    color: "#ddd",
    fontFamily: "system-ui, sans-serif",
  },
  header: {
    display: "flex",
    alignItems: "center",
    padding: "10px 14px",
    borderBottom: "1px solid #222",
  },
  title: { fontWeight: 700, fontSize: 14 },
  closeBtn: {
    marginLeft: "auto",
    background: "transparent",
    border: "none",
    color: "#aaa",
    fontSize: 22,
    cursor: "pointer",
    width: 28,
    height: 28,
  },
  body: {
    flex: 1,
    display: "grid",
    gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)",
    minHeight: 0,
  },
  left: {
    display: "flex",
    flexDirection: "column" as const,
    borderRight: "1px solid #222",
    minHeight: 0,
  },
  search: {
    margin: 10,
    padding: "6px 10px",
    background: "#1a1a1a",
    border: "1px solid #333",
    borderRadius: 4,
    color: "#ddd",
    fontSize: 13,
  },
  grid: {
    flex: 1,
    overflowY: "auto" as const,
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
    gap: 8,
    padding: "0 10px 12px",
  },
  gridItem: {
    border: "2px solid #333",
    borderRadius: 4,
    padding: 4,
    cursor: "pointer",
    overflow: "hidden",
    color: "#ddd",
    textAlign: "left" as const,
    fontFamily: "inherit",
    fontSize: 11,
  },
  thumb: {
    position: "relative" as const,
    width: "100%",
    aspectRatio: "16 / 9",
    background: "#1a1a1a",
    borderRadius: 3,
    overflow: "hidden",
  },
  thumbImg: {
    width: "100%",
    height: "100%",
    objectFit: "cover" as const,
  },
  durBadge: {
    position: "absolute" as const,
    bottom: 2,
    right: 4,
    background: "rgba(0,0,0,0.75)",
    color: "#fff",
    padding: "1px 5px",
    borderRadius: 2,
    fontSize: 10,
  },
  curBadge: {
    position: "absolute" as const,
    top: 2,
    left: 4,
    background: "#facc15",
    color: "#222",
    padding: "1px 6px",
    borderRadius: 2,
    fontSize: 10,
    fontWeight: 700,
  },
  label: {
    marginTop: 4,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap" as const,
  },
  empty: {
    gridColumn: "1/-1",
    padding: 24,
    color: "#666",
    textAlign: "center" as const,
  },
  right: {
    display: "flex",
    flexDirection: "column" as const,
    padding: 14,
    minHeight: 0,
  },
  previewLabel: { fontWeight: 600, marginBottom: 6, fontSize: 13 },
  previewBox: {
    flex: 1,
    minHeight: 180,
    background: "#000",
    border: "1px solid #222",
    borderRadius: 4,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  previewMedia: {
    maxWidth: "100%",
    maxHeight: "100%",
    width: "auto",
    height: "auto",
  },
  meta: {
    marginTop: 8,
    fontSize: 12,
    color: "#bbb",
    display: "flex",
    flexDirection: "column" as const,
    gap: 2,
  },
  actions: {
    marginTop: 10,
    display: "flex",
    gap: 8,
    justifyContent: "flex-end",
  },
  cancelBtn: {
    background: "#222",
    color: "#ddd",
    border: "1px solid #444",
    borderRadius: 4,
    padding: "6px 14px",
    cursor: "pointer",
  },
  applyBtn: {
    background: "#6366f1",
    color: "#fff",
    border: "1px solid #6366f1",
    borderRadius: 4,
    padding: "6px 14px",
    cursor: "pointer",
    fontWeight: 600,
  },
  hint: {
    marginTop: 6,
    fontSize: 11,
    color: "#666",
    textAlign: "right" as const,
  },
  // ── #365 트리밍 슬라이더 ───────────────────────────────────────────────
  trim: {
    marginTop: 10,
    padding: "8px 10px",
    background: "#141414",
    border: "1px solid #2a2a2a",
    borderRadius: 4,
    display: "flex",
    flexDirection: "column" as const,
    gap: 5,
  },
  trimHeader: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    fontSize: 11,
  },
  trimLabel: { color: "#bbb", fontWeight: 600 },
  trimValues: { color: "#ccc", marginLeft: "auto", fontVariantNumeric: "tabular-nums" as const },
  trimDur: { color: "#888", marginLeft: 4 },
  trimReset: {
    background: "#1a1a1a",
    color: "#aaa",
    border: "1px solid #333",
    borderRadius: 3,
    fontSize: 10,
    padding: "2px 8px",
    cursor: "pointer",
    marginLeft: 6,
  },
  trimRow: {
    display: "flex",
    alignItems: "center",
    gap: 6,
  },
  trimSliderLabel: {
    color: "#777",
    fontSize: 11,
    minWidth: 28,
  },
  trimSlider: {
    flex: 1,
    accentColor: "#6366f1",
  },
};
