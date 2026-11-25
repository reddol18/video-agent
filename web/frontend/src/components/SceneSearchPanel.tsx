/**
 * SceneSearchPanel — 장면 텍스트 검색 (구간 분할 보조)
 * ─────────────────────────────────────────────────────────────────────────
 * 구간 분할 화면에서 "그 장면이 몇 초쯤이었나"를 영상 스크러빙 없이
 * **설명 문장으로** 찾게 돕는 패널. FilmstripBar(눈으로 찾기)의 짝이다.
 *
 * 동작:
 *   1) `GET /{project_id}/studio/scene-index?asset_id=` 1회 조회 → 해당 자산의
 *      segments 만 받는다(필터링은 백엔드가 수행).
 *   2) 검색어를 `desc` + `subjects` 에 부분일치(대소문자 무시)로 필터.
 *      입력은 200ms 디바운스 — 117개 규모에서 매 타이핑 재필터는 낭비.
 *   3) segment 버튼 클릭 → `onSeek(start_sec)` → 호출부가 video.currentTime 이동.
 *      **FilmstripBar 와 동일한 seek 경로를 공유한다**(Studio.tsx seekSplitPlayer).
 *   4) Ctrl/Cmd+클릭 → 다중 선택 토글, Shift+클릭 → 범위 선택.
 *      선택된 구간을 `onAddSegments` 로 올려 구간 분할 목록에 담는다(label=desc 자동).
 *      **일반 클릭은 3) 의 seek 만** 수행한다 — 기존 동작을 바꾸지 않는다.
 *
 * 주의:
 *   - 색인 파일은 **없는 것이 정상**이다. 백엔드가 `available: false` 로 답하면
 *     한 줄 안내만 남기고 조용히 비활성화한다.
 *   - 조회가 실패해도 에러를 바깥으로 던지지 않는다. 이 패널이 깨져도
 *     기존 구간 분할 기능은 멀쩡해야 한다.
 */

import type { CSSProperties, MouseEvent as ReactMouseEvent } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getStudioSceneIndex,
  type SceneIndexSegment,
} from "../api/client";

/** 분할 목록에 담을 구간 — Studio 의 TimelineSegment 와 같은 모양. */
export interface SceneSearchPick {
  start: number;
  end: number;
  label: string;
}

/** label 최대 길이 — 너무 길면 분할 목록 UI 가 깨진다. 검색성을 위해 과하게 줄이지 않는다. */
const LABEL_MAX = 40;

function toLabel(seg: SceneIndexSegment): string {
  const raw = (seg.desc ?? "").replace(/\s+/g, " ").trim();
  if (!raw) return `${formatTimecode(seg.start_sec)}-${formatTimecode(seg.end_sec)}`;
  return raw.length > LABEL_MAX ? `${raw.slice(0, LABEL_MAX - 1)}…` : raw;
}

/** quality 별 표시 색 — 요구사항: good=초록, fair=노랑, poor=회색. */
const QUALITY_COLOR: Record<string, string> = {
  good: "#22c55e",
  fair: "#eab308",
  poor: "#6b7280",
};

/** quality 값이 비었거나 모르는 값이면 회색으로 떨어뜨린다. */
function qualityColor(q?: string): string {
  return QUALITY_COLOR[q ?? ""] ?? "#6b7280";
}

/** 선택 툴바 버튼 공통 스타일. enabled=false 면 흐리게. */
function pickBtnStyle(enabled: boolean): CSSProperties {
  return {
    padding: "3px 8px",
    fontSize: 11,
    borderRadius: 4,
    border: "1px solid #2a2a2a",
    background: "#1a1a1a",
    color: enabled ? "#bbb" : "#555",
    cursor: enabled ? "pointer" : "default",
  };
}

function formatTimecode(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export default function SceneSearchPanel({
  projectId,
  assetId,
  currentTime,
  onSeek,
  onAddSegments,
}: {
  projectId: string;
  assetId: string;
  /** 현재 재생 위치(초) — 포함 구간을 하이라이트한다. FilmstripBar 와 같은 구독. */
  currentTime: number;
  /** 구간 클릭 시 이동할 시각(초). */
  onSeek: (sec: number) => void;
  /**
   * 선택한 구간들을 분할 목록에 담는다. 호출부가 기존 목록에 병합하고,
   * **중복으로 건너뛴 개수**를 돌려준다(없으면 생략 가능 — 버튼도 숨긴다).
   */
  onAddSegments?: (picks: SceneSearchPick[]) => { added: number; skipped: number };
}) {
  const [segments, setSegments] = useState<SceneIndexSegment[]>([]);
  // null = 조회 전(로딩). true/false = 색인 사용 가능 여부.
  const [available, setAvailable] = useState<boolean | null>(null);
  const [stale, setStale] = useState(false);
  const [query, setQuery] = useState("");
  // 디바운스된 검색어 — 실제 필터는 이 값으로만 수행한다.
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [goodOnly, setGoodOnly] = useState(false);
  // 다중 선택된 구간 키(`file|start_sec`) 집합. 일반 클릭은 건드리지 않는다.
  const [picked, setPicked] = useState<Set<string>>(new Set());
  // Shift+클릭 범위 선택의 기준점(마지막으로 Ctrl 클릭한 구간 키).
  const [anchorKey, setAnchorKey] = useState<string>("");
  // "N개 추가, M개 중복" 안내 한 줄.
  const [notice, setNotice] = useState("");

  // 색인 조회. 자산이 바뀌면 다시 받는다(백엔드에서 파일 단위 필터링).
  useEffect(() => {
    let cancelled = false;
    setAvailable(null);
    setPicked(new Set());
    setAnchorKey("");
    setNotice("");
    getStudioSceneIndex(projectId, assetId)
      .then((res) => {
        if (cancelled) return;
        setAvailable(!!res.available);
        setSegments(res.available ? res.segments ?? [] : []);
        setStale(!!res.stale);
      })
      .catch(() => {
        // 조회 실패도 "데이터 없음"과 동일하게 취급 — 바깥으로 던지지 않는다.
        if (cancelled) return;
        setAvailable(false);
        setSegments([]);
        setStale(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, assetId]);

  // 검색어 디바운스 200ms.
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(query), 200);
    return () => window.clearTimeout(t);
  }, [query]);

  const filtered = useMemo(() => {
    // 한글은 대소문자 개념이 없어 toLowerCase 가 무해하다(영문/숫자만 영향).
    const q = debouncedQuery.trim().toLowerCase();
    return segments.filter((seg) => {
      if (goodOnly && seg.quality === "poor") return false;
      if (!q) return true;
      const haystack = [seg.desc ?? "", ...(seg.subjects ?? [])]
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [segments, debouncedQuery, goodOnly]);

  // 현재 재생 위치가 속한 구간(원본 배열 기준 식별자: file+start).
  const activeKey = useMemo(() => {
    const hit = segments.find(
      (s) => currentTime >= s.start_sec && currentTime < s.end_sec,
    );
    return hit ? `${hit.file}|${hit.start_sec}` : "";
  }, [segments, currentTime]);

  /**
   * 구간 클릭 라우터.
   *   - Ctrl/Cmd: 선택 토글(seek 하지 않음 — 선택 중 영상이 튀는 걸 막는다)
   *   - Shift: anchor ~ 현재 구간을 **필터링된 목록 순서** 기준으로 일괄 선택
   *   - 그 외: 기존과 동일하게 seek 만
   */
  const handleItemClick = useCallback(
    (seg: SceneIndexSegment, e: ReactMouseEvent) => {
      const key = `${seg.file}|${seg.start_sec}`;
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        setPicked((prev) => {
          const next = new Set(prev);
          if (next.has(key)) next.delete(key);
          else next.add(key);
          return next;
        });
        setAnchorKey(key);
        setNotice("");
        return;
      }
      if (e.shiftKey && anchorKey) {
        e.preventDefault();
        const keys = filtered.map((s) => `${s.file}|${s.start_sec}`);
        const from = keys.indexOf(anchorKey);
        const to = keys.indexOf(key);
        // anchor 가 현재 필터 결과에서 빠졌으면 범위를 특정할 수 없다 → 단일 선택으로 처리.
        if (from === -1 || to === -1) {
          setPicked((prev) => new Set(prev).add(key));
          setAnchorKey(key);
          return;
        }
        const [lo, hi] = from <= to ? [from, to] : [to, from];
        setPicked((prev) => {
          const next = new Set(prev);
          for (let i = lo; i <= hi; i++) next.add(keys[i]);
          return next;
        });
        setNotice("");
        return;
      }
      onSeek(seg.start_sec);
    },
    [anchorKey, filtered, onSeek],
  );

  function selectAll(list: SceneIndexSegment[]) {
    setPicked((prev) => {
      const next = new Set(prev);
      for (const s of list) next.add(`${s.file}|${s.start_sec}`);
      return next;
    });
    setNotice("");
  }

  function clearPicked() {
    setPicked(new Set());
    setAnchorKey("");
    setNotice("");
  }

  function addPickedToSplit() {
    if (!onAddSegments || picked.size === 0) return;
    // 원본 배열 순서(시간 오름차순)대로 올린다 — 호출부에서 다시 정렬하지만
    // 안내 문구의 개수 집계가 화면과 어긋나지 않도록 여기서도 순서를 유지한다.
    const picks: SceneSearchPick[] = segments
      .filter((s) => picked.has(`${s.file}|${s.start_sec}`))
      .map((s) => ({ start: s.start_sec, end: s.end_sec, label: toLabel(s) }));
    const { added, skipped } = onAddSegments(picks);
    // 선택 해제가 notice 를 지우지 않도록 clearPicked 를 쓰지 않고 직접 비운다.
    setPicked(new Set());
    setAnchorKey("");
    setNotice(
      skipped > 0
        ? `${added}개 추가, ${skipped}개는 이미 목록에 있어 건너뜀`
        : `${added}개 구간을 분할 목록에 추가했습니다`,
    );
  }

  // 색인이 아예 없으면 한 줄만 — 분할 화면을 어지럽히지 않는다.
  if (available === false) {
    return (
      <div style={{ marginTop: 12, color: "#555", fontSize: 11 }}>
        장면 설명 데이터 없음
      </div>
    );
  }

  return (
    <div style={{ marginTop: 12 }}>
      {/* 헤더 — 제목 + 검색창 + "쓸 만한 것만" 토글 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 6,
          flexWrap: "wrap",
        }}
      >
        <span style={{ color: "#aaa", fontSize: 12, fontWeight: 600 }}>
          장면 찾기
        </span>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="설명·대상 검색 (예: 장난감)"
          style={{
            flex: "1 1 200px",
            minWidth: 160,
            padding: "4px 8px",
            fontSize: 12,
            borderRadius: 4,
            border: "1px solid #333",
            background: "#1a1a1a",
            color: "#ddd",
          }}
        />
        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 4,
            color: "#888",
            fontSize: 11,
            cursor: "pointer",
          }}
          title="quality 가 poor 인 구간을 숨깁니다"
        >
          <input
            type="checkbox"
            checked={goodOnly}
            onChange={(e) => setGoodOnly(e.target.checked)}
          />
          쓸 만한 것만
        </label>
        {available && (
          <span style={{ color: "#555", fontSize: 11 }}>
            {filtered.length}/{segments.length}개
          </span>
        )}
      </div>

      {/* 다중 선택 툴바 — onAddSegments 를 받은 경우에만. 색인이 있을 때만 의미가 있다. */}
      {onAddSegments && available && segments.length > 0 && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginBottom: 6,
            flexWrap: "wrap",
          }}
        >
          <span
            style={{
              color: picked.size > 0 ? "#c7d2fe" : "#666",
              fontSize: 11,
              fontWeight: picked.size > 0 ? 700 : 400,
            }}
          >
            선택 {picked.size}개
          </span>
          <button
            onClick={() => selectAll(filtered)}
            disabled={filtered.length === 0}
            title="현재 검색·필터 결과만 선택합니다"
            style={pickBtnStyle(filtered.length > 0)}
          >
            검색 결과 전체 선택 ({filtered.length})
          </button>
          <button
            onClick={() => selectAll(segments)}
            style={pickBtnStyle(true)}
            title="필터와 무관하게 이 영상의 모든 구간을 선택합니다"
          >
            전체 선택
          </button>
          <button
            onClick={clearPicked}
            disabled={picked.size === 0}
            style={pickBtnStyle(picked.size > 0)}
          >
            선택 해제
          </button>
          <button
            onClick={addPickedToSplit}
            disabled={picked.size === 0}
            style={{
              ...pickBtnStyle(picked.size > 0),
              background: picked.size > 0 ? "#4f46e5" : "#1a1a1a",
              borderColor: picked.size > 0 ? "#6366f1" : "#2a2a2a",
              color: picked.size > 0 ? "#fff" : "#555",
              fontWeight: 700,
            }}
          >
            선택 구간 분할 목록에 추가
          </button>
          <span style={{ color: "#555", fontSize: 10 }}>
            Ctrl(⌘)+클릭 선택 · Shift+클릭 범위
          </span>
        </div>
      )}

      {notice && (
        <div style={{ color: "#86efac", fontSize: 11, marginBottom: 6 }}>{notice}</div>
      )}

      {/* 원본 변경 경고 — 색인 생성 당시와 파일 시그니처가 달라진 경우. */}
      {stale && (
        <div style={{ color: "#f59e0b", fontSize: 11, marginBottom: 6 }}>
          원본이 변경되어 설명이 오래됐을 수 있습니다
        </div>
      )}

      {available === null && (
        <div style={{ color: "#777", fontSize: 12 }}>장면 설명 불러오는 중…</div>
      )}

      {available && segments.length === 0 && (
        <div style={{ color: "#555", fontSize: 11 }}>
          이 영상에 대한 장면 설명이 없습니다
        </div>
      )}

      {available && segments.length > 0 && (
        <div
          style={{
            maxHeight: 220,
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 3,
            padding: 4,
            background: "#141414",
            border: "1px solid #2a2a2a",
            borderRadius: 6,
          }}
        >
          {filtered.length === 0 && (
            <div style={{ color: "#666", fontSize: 11, padding: "6px 4px" }}>
              검색 결과가 없습니다
            </div>
          )}
          {filtered.map((seg) => {
            const key = `${seg.file}|${seg.start_sec}`;
            const active = key === activeKey;
            const sel = picked.has(key);
            return (
              <button
                key={key}
                onClick={(e) => handleItemClick(seg, e)}
                title={
                  onAddSegments
                    ? `클릭: ${formatTimecode(seg.start_sec)} 로 이동 / Ctrl(⌘)+클릭: 선택`
                    : `${formatTimecode(seg.start_sec)} 로 이동`
                }
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 8,
                  textAlign: "left",
                  padding: "5px 8px",
                  borderRadius: 4,
                  cursor: "pointer",
                  // 선택 표시가 재생 위치 하이라이트를 덮는다 — 선택이 사용자의 의도이므로 우선.
                  border: `1px solid ${sel ? "#22d3ee" : active ? "#6366f1" : "transparent"}`,
                  background: sel
                    ? "rgba(34,211,238,0.16)"
                    : active
                      ? "rgba(99,102,241,0.14)"
                      : "#1a1a1a",
                  color: sel ? "#a5f3fc" : active ? "#c7d2fe" : "#bbb",
                  fontSize: 12,
                  lineHeight: 1.4,
                }}
              >
                {/* 선택 체크 표시 — onAddSegments 가 있을 때만 자리를 차지한다. */}
                {onAddSegments && (
                  <span
                    aria-hidden
                    style={{
                      flex: "0 0 auto",
                      width: 11,
                      color: sel ? "#22d3ee" : "#444",
                      fontWeight: 700,
                    }}
                  >
                    {sel ? "☑" : "☐"}
                  </span>
                )}
                {/* quality 색 점 */}
                <span
                  aria-hidden
                  style={{
                    flex: "0 0 auto",
                    width: 7,
                    height: 7,
                    marginTop: 5,
                    borderRadius: "50%",
                    background: qualityColor(seg.quality),
                  }}
                />
                <span
                  style={{
                    flex: "0 0 auto",
                    fontVariantNumeric: "tabular-nums",
                    color: active ? "#c7d2fe" : "#888",
                    fontWeight: active ? 700 : 400,
                  }}
                >
                  {formatTimecode(seg.start_sec)}-{formatTimecode(seg.end_sec)}
                </span>
                <span style={{ flex: "1 1 auto" }}>{seg.desc}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
