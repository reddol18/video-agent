/**
 * FilmstripBar — 프레임 스트립 (구간 분할 보조)
 * ─────────────────────────────────────────────────────────────────────────
 * 구간 분할 화면에서 "자를 지점"을 영상 슬라이딩 없이 눈으로 찾게 돕는 띠.
 *
 * 동작:
 *   1) `?meta=1` 로 메타(적용 interval, 타일 수, 타일 w/h)를 먼저 받는다.
 *   2) 같은 URL(meta 없이)을 <img> 1회 로드 → **요청 1회로 N개 썸네일**.
 *   3) 각 썸네일은 같은 이미지를 공유하고 CSS background-position 으로 잘라 보여준다.
 *   4) 썸네일 클릭 → `onSeek(t)` → 호출부가 video.currentTime 을 옮긴다.
 *
 * 주의:
 *   - 백엔드가 뱉는 프레임 i 의 실제 타임스탬프는 정확히 `i * interval` 초다
 *     (ffmpeg `fps=1/interval` 이 0, interval, 2*interval... 로 샘플링).
 *     따라서 타임코드는 인덱스 × interval 로 계산해도 어긋나지 않는다.
 *   - 스트립 생성이 긴 영상에서 10초 이상 걸릴 수 있어 로딩 상태를 반드시 보여준다.
 *   - **스트립이 실패해도 분할 기능 자체는 멀쩡해야 하므로** 에러는 이 컴포넌트
 *     안에서만 표시하고 바깥으로 throw 하지 않는다.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  getStudioAssetFilmstripMeta,
  studioAssetFilmstripUrl,
  type FilmstripMeta,
} from "../api/client";

/** interval 토글 선택지(초) — 백엔드 허용 목록과 동일. */
const INTERVAL_OPTIONS = [1, 2, 5, 10] as const;

/** 썸네일 표시 높이(px). 타일 원본 종횡비를 유지하며 가로폭을 역산한다. */
const DISPLAY_HEIGHT = 72;

function formatTimecode(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export default function FilmstripBar({
  projectId,
  assetId,
  currentTime,
  onSeek,
  defaultInterval = 2,
}: {
  projectId: string;
  assetId: string;
  /** 현재 재생 위치(초) — 해당 썸네일을 하이라이트한다. */
  currentTime: number;
  /** 썸네일 클릭 시 이동할 시각(초). */
  onSeek: (sec: number) => void;
  defaultInterval?: number;
}) {
  // `interval` 이라는 이름은 전역 setInterval 과 혼동되므로 state setter 를 분리 명명한다.
  const [intervalSec, setIntervalSec] = useState<number>(defaultInterval);
  // "다시 시도" 전용 nonce — 같은 interval 로 재요청하려면 상태가 실제로 바뀌어야 한다.
  const [retryNonce, setRetryNonce] = useState(0);
  // meta/error/imgLoaded 는 모두 "어느 요청의 결과인지"(_key)를 함께 들고 다닌다.
  // 키가 현재 요청과 다르면 낡은 결과로 보고 무시 → effect 에서 리셋할 필요가 없다.
  const [metaState, setMetaState] = useState<{ key: string; meta: FilmstripMeta } | null>(null);
  const [errorState, setErrorState] = useState<{ key: string; msg: string } | null>(null);
  const [loadedKey, setLoadedKey] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  const stripUrl = useMemo(
    () => studioAssetFilmstripUrl(projectId, assetId, intervalSec),
    [projectId, assetId, intervalSec],
  );

  // 요청 1건을 식별하는 키. interval/자산이 바뀌면 키가 바뀌므로 meta·에러·이미지
  // 로드 상태를 effect 에서 수동으로 리셋하지 않아도 자동으로 무효화된다.
  // (effect 본문에서 setState 하면 cascading render 가 생기므로 키 비교로 대체)
  const reqKey = `${projectId}|${assetId}|${intervalSec}|${retryNonce}`;

  useEffect(() => {
    let cancelled = false;
    getStudioAssetFilmstripMeta(projectId, assetId, intervalSec)
      .then((m) => {
        if (cancelled) return;
        setMetaState({ key: reqKey, meta: m });
      })
      .catch((e) => {
        if (cancelled) return;
        const detail =
          (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          (e as Error).message;
        setErrorState({ key: reqKey, msg: `프레임 스트립을 만들지 못했습니다: ${detail}` });
      });
    // interval 을 빠르게 바꾸면 이전 요청은 cancelled 로 버려져 최신 응답만 반영된다.
    return () => { cancelled = true; };
  }, [projectId, assetId, intervalSec, retryNonce, reqKey]);

  // 현재 요청의 결과만 채택 — 낡은 응답/이미지는 자동으로 무시된다.
  const meta = metaState?.key === reqKey ? metaState.meta : null;
  const error = errorState?.key === reqKey ? errorState.msg : "";
  const imgLoaded = loadedKey === reqKey;

  // 실제 적용된 interval 로 타임코드를 계산한다 (긴 영상은 서버가 키울 수 있음).
  const effInterval = meta?.interval ?? intervalSec;
  const activeIdx = meta
    ? Math.min(meta.tile_count - 1, Math.max(0, Math.floor(currentTime / effInterval)))
    : -1;

  // 타일 원본 종횡비를 유지한 표시 크기. 세로 영상(9:16)도 찌그러지지 않는다.
  const scale = meta && meta.tile_height > 0 ? DISPLAY_HEIGHT / meta.tile_height : 1;
  const dispW = meta ? Math.max(1, Math.round(meta.tile_width * scale)) : 0;

  // 재생 위치가 움직이면 활성 썸네일이 보이도록 가로 스크롤을 따라가게 한다.
  useEffect(() => {
    const box = scrollRef.current;
    if (!box || activeIdx < 0 || !imgLoaded || dispW <= 0) return;
    const left = activeIdx * (dispW + 4);
    const right = left + dispW;
    if (left < box.scrollLeft || right > box.scrollLeft + box.clientWidth) {
      box.scrollTo({ left: Math.max(0, left - box.clientWidth / 2), behavior: "smooth" });
    }
  }, [activeIdx, imgLoaded, dispW]);

  const loading = !error && (!meta || !imgLoaded);

  return (
    <div style={{ marginTop: 12 }}>
      {/* 헤더 — 제목 + interval 토글 */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <span style={{ color: "#aaa", fontSize: 12, fontWeight: 600 }}>프레임 스트립</span>
        <div style={{ display: "flex", gap: 4 }}>
          {INTERVAL_OPTIONS.map((iv) => (
            <button
              key={iv}
              onClick={() => setIntervalSec(iv)}
              title={`${iv}초 간격으로 프레임 표시`}
              style={{
                padding: "2px 8px",
                fontSize: 11,
                borderRadius: 4,
                cursor: "pointer",
                border: `1px solid ${intervalSec === iv ? "#6366f1" : "#333"}`,
                background: intervalSec === iv ? "rgba(99,102,241,0.18)" : "#1a1a1a",
                color: intervalSec === iv ? "#c7d2fe" : "#888",
              }}
            >
              {iv}초
            </button>
          ))}
        </div>
        {/* 서버가 타일 수 상한 때문에 간격을 키운 경우 사용자에게 알린다. */}
        {meta && meta.interval !== meta.requested_interval && (
          <span style={{ color: "#f59e0b", fontSize: 11 }}>
            영상이 길어 {meta.interval}초 간격으로 표시합니다
          </span>
        )}
        {meta && imgLoaded && (
          <span style={{ color: "#555", fontSize: 11 }}>{meta.tile_count}장</span>
        )}
      </div>

      {error && (
        <div
          style={{
            color: "#f87171",
            fontSize: 12,
            background: "rgba(248,113,113,0.08)",
            border: "1px solid rgba(248,113,113,0.3)",
            borderRadius: 6,
            padding: "8px 10px",
          }}
        >
          {error}
          <button
            onClick={() => setRetryNonce((n) => n + 1)}
            style={{
              marginLeft: 8,
              padding: "1px 8px",
              fontSize: 11,
              borderRadius: 4,
              border: "1px solid #444",
              background: "#1a1a1a",
              color: "#aaa",
              cursor: "pointer",
            }}
          >
            다시 시도
          </button>
        </div>
      )}

      {loading && (
        <div
          style={{
            height: DISPLAY_HEIGHT + 18,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
            background: "#141414",
            border: "1px solid #2a2a2a",
            borderRadius: 6,
            color: "#777",
            fontSize: 12,
          }}
        >
          프레임 스트립 생성 중… 긴 영상은 수십 초 걸릴 수 있습니다
        </div>
      )}

      {/* 스프라이트 시트는 요청 1회. 숨겨서 로드하고 각 칸은 background 로 잘라 쓴다. */}
      {meta && !error && (
        <img
          src={stripUrl}
          alt=""
          onLoad={() => setLoadedKey(reqKey)}
          onError={() =>
            setErrorState({ key: reqKey, msg: "프레임 스트립 이미지를 불러오지 못했습니다" })
          }
          style={{ display: "none" }}
        />
      )}

      {meta && imgLoaded && !error && (
        <div
          ref={scrollRef}
          style={{
            display: "flex",
            gap: 4,
            overflowX: "auto",
            overflowY: "hidden",
            padding: "4px 2px 6px",
            background: "#141414",
            border: "1px solid #2a2a2a",
            borderRadius: 6,
          }}
        >
          {Array.from({ length: meta.tile_count }, (_, i) => {
            const t = i * effInterval;
            const active = i === activeIdx;
            return (
              <button
                key={i}
                onClick={() => onSeek(t)}
                title={`${formatTimecode(t)} 로 이동`}
                style={{
                  flex: "0 0 auto",
                  padding: 0,
                  border: "none",
                  background: "none",
                  cursor: "pointer",
                }}
              >
                <div
                  style={{
                    width: dispW,
                    height: DISPLAY_HEIGHT,
                    borderRadius: 3,
                    // 스프라이트 전체를 표시 배율로 늘리고, i번째 칸으로 오프셋.
                    backgroundImage: `url(${stripUrl})`,
                    backgroundSize: `${meta.sheet_width * scale}px ${DISPLAY_HEIGHT}px`,
                    backgroundPosition: `-${i * dispW}px 0`,
                    backgroundRepeat: "no-repeat",
                    outline: active ? "2px solid #6366f1" : "1px solid #333",
                    outlineOffset: active ? -2 : -1,
                  }}
                />
                <div
                  style={{
                    textAlign: "center",
                    fontSize: 10,
                    marginTop: 2,
                    color: active ? "#c7d2fe" : "#777",
                    fontWeight: active ? 700 : 400,
                  }}
                >
                  {formatTimecode(t)}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
