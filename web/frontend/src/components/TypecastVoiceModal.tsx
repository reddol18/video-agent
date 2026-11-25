/**
 * Typecast voice 탐색 모달 (#typecast-voice-ux)
 * ─────────────────────────────────────────────────────────────
 * Typecast voice 596개를 단일 콤보박스에서 찾기 불가능하던 문제를 필터+검색 UI 로 해결.
 *   - 이름 검색(부분일치, 대소문자 무시, 실시간)
 *   - 성별 / 연령 / 용도(use_cases) 필터 — 다중 조건 AND
 *   - 결과 행: 이름, 성별/연령 배지, 용도 태그, 미리듣기, 선택
 *   - 결과 개수 표시 + 긴 목록은 slice + "더 보기"
 *
 * 언어 필터는 API 에 언어 필드가 없어 만들지 않는다(오판 위험). 미리듣기로 귀 확인.
 * Google voice 는 이 모달과 무관 — 기존 콤보박스 그대로 사용.
 */
import { useMemo, useState } from "react";
import type { TypecastVoice } from "../api/client";
import { useTypecastRawVoices } from "../lib/ttsVoices";
import { useTtsPreview } from "../lib/ttsPreview";
import {
  filterVoices,
  collectUseCases,
  ageLabel,
  genderLabel,
  AGE_ORDER,
  AGE_LABELS,
} from "../lib/typecastVoiceFilter";

const PAGE_SIZE = 60;

export default function TypecastVoiceModal({
  currentVoiceId,
  onSelect,
  onClose,
}: {
  currentVoiceId?: string | null;
  onSelect: (voiceId: string, voice: TypecastVoice) => void;
  onClose: () => void;
}) {
  const { voices, loading, error } = useTypecastRawVoices();
  const preview = useTtsPreview();

  const [q, setQ] = useState("");
  const [gender, setGender] = useState("");
  const [age, setAge] = useState("");
  const [useCase, setUseCase] = useState("");
  const [visible, setVisible] = useState(PAGE_SIZE);

  const useCases = useMemo(() => collectUseCases(voices), [voices]);
  const filtered = useMemo(
    () => filterVoices(voices, { q, gender, age, useCase }),
    [voices, q, gender, age, useCase],
  );
  const shown = filtered.slice(0, visible);

  // 필터가 바뀌면 페이지네이션 초기화.
  const resetVisible = () => setVisible(PAGE_SIZE);

  return (
    <div style={S.backdrop} onClick={onClose}>
      <div style={S.modal} onClick={(e) => e.stopPropagation()}>
        <div style={S.header}>
          <h3 style={{ margin: 0, fontSize: 17 }}>Typecast 목소리 찾기</h3>
          <button type="button" style={S.closeBtn} onClick={onClose} title="닫기">
            ✕
          </button>
        </div>

        {/* 검색 + 필터 */}
        <div style={S.filters}>
          <input
            style={S.search}
            placeholder="이름으로 검색 (예: Sanghyun)"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              resetVisible();
            }}
            autoFocus
          />
          <div style={S.selectRow}>
            <select
              style={S.select}
              value={gender}
              onChange={(e) => {
                setGender(e.target.value);
                resetVisible();
              }}
              title="성별"
            >
              <option value="">성별 전체</option>
              <option value="male">남</option>
              <option value="female">여</option>
            </select>
            <select
              style={S.select}
              value={age}
              onChange={(e) => {
                setAge(e.target.value);
                resetVisible();
              }}
              title="연령"
            >
              <option value="">연령 전체</option>
              {AGE_ORDER.map((a) => (
                <option key={a} value={a}>
                  {AGE_LABELS[a]}
                </option>
              ))}
            </select>
            <select
              style={S.select}
              value={useCase}
              onChange={(e) => {
                setUseCase(e.target.value);
                resetVisible();
              }}
              title="용도"
            >
              <option value="">용도 전체</option>
              {useCases.map((uc) => (
                <option key={uc} value={uc}>
                  {uc}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* 결과 개수 */}
        <div style={S.count}>
          {loading
            ? "목소리 불러오는 중…"
            : error
              ? "목록을 불러오지 못했어요. Typecast API 키 설정을 확인해주세요."
              : `${voices.length}개 중 ${filtered.length}개`}
        </div>

        {/* 결과 리스트 */}
        <div style={S.list}>
          {!loading && !error && filtered.length === 0 && (
            <div style={S.empty}>조건에 맞는 목소리가 없어요.</div>
          )}
          {shown.map((v) => {
            const playing = preview.playingVoice === v.voice_id;
            const selected = currentVoiceId === v.voice_id;
            return (
              <div
                key={v.voice_id}
                style={{ ...S.row, ...(selected ? S.rowSelected : {}) }}
              >
                <div style={S.rowMain}>
                  <div style={S.nameLine}>
                    <span style={S.name}>{v.voice_name || v.voice_id}</span>
                    {v.gender && <span style={S.badge}>{genderLabel(v.gender)}</span>}
                    {v.age && <span style={S.badge}>{ageLabel(v.age)}</span>}
                  </div>
                  {(v.use_cases?.length ?? 0) > 0 && (
                    <div style={S.tags}>
                      {v.use_cases!.slice(0, 4).map((uc) => (
                        <span key={uc} style={S.tag}>
                          {uc}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div style={S.rowActions}>
                  <button
                    type="button"
                    style={S.previewBtn}
                    onClick={() => preview.preview(v.voice_id)}
                    disabled={preview.loading}
                    title={playing ? "정지" : "미리듣기 (크레딧 소량 소모)"}
                  >
                    {preview.loading && playing ? "…" : playing ? "■" : "▶"}
                  </button>
                  <button
                    type="button"
                    style={S.selectBtn}
                    onClick={() => onSelect(v.voice_id, v)}
                    title="이 목소리 선택"
                  >
                    {selected ? "선택됨" : "선택"}
                  </button>
                </div>
              </div>
            );
          })}
          {!loading && filtered.length > visible && (
            <button
              type="button"
              style={S.moreBtn}
              onClick={() => setVisible((n) => n + PAGE_SIZE)}
            >
              더 보기 ({filtered.length - visible}개)
            </button>
          )}
        </div>

        {preview.error && (
          <p style={{ margin: "8px 16px 0", color: "#f87171", fontSize: 12 }}>
            미리듣기 실패: {preview.error}
          </p>
        )}
      </div>
    </div>
  );
}

const S: Record<string, React.CSSProperties> = {
  backdrop: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.55)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 9600,
  },
  modal: {
    background: "#1a1a1a",
    border: "1px solid #333",
    borderRadius: 12,
    width: 620,
    maxWidth: "94vw",
    maxHeight: "86vh",
    display: "flex",
    flexDirection: "column",
    color: "#e5e5e5",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "16px 16px 10px",
  },
  closeBtn: {
    background: "transparent",
    border: "none",
    color: "#aaa",
    fontSize: 16,
    cursor: "pointer",
  },
  filters: { padding: "0 16px 8px", display: "flex", flexDirection: "column", gap: 8 },
  search: {
    padding: "9px 12px",
    background: "#111",
    color: "#fff",
    border: "1px solid #333",
    borderRadius: 7,
    fontSize: 14,
  },
  selectRow: { display: "flex", gap: 8 },
  select: {
    flex: 1,
    padding: "7px 10px",
    background: "#111",
    color: "#fff",
    border: "1px solid #333",
    borderRadius: 7,
    fontSize: 13,
  },
  count: { padding: "4px 16px", fontSize: 12, color: "#aaa" },
  list: { overflowY: "auto", padding: "4px 16px 16px", flex: 1 },
  empty: { padding: "24px 0", textAlign: "center", color: "#888", fontSize: 13 },
  row: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    padding: "10px 12px",
    borderBottom: "1px solid #262626",
  },
  rowSelected: { background: "#1e293b" },
  rowMain: { minWidth: 0, flex: 1 },
  nameLine: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" },
  name: { fontSize: 14, fontWeight: 600 },
  badge: {
    fontSize: 11,
    color: "#a5b4fc",
    background: "#232338",
    borderRadius: 5,
    padding: "1px 6px",
  },
  tags: { display: "flex", gap: 4, flexWrap: "wrap", marginTop: 4 },
  tag: {
    fontSize: 10,
    color: "#9ca3af",
    background: "#222",
    borderRadius: 4,
    padding: "1px 5px",
  },
  rowActions: { display: "flex", gap: 6, flexShrink: 0 },
  previewBtn: {
    width: 34,
    padding: "6px 0",
    background: "#252525",
    color: "#a5b4fc",
    border: "1px solid #333",
    borderRadius: 6,
    fontSize: 13,
    cursor: "pointer",
  },
  selectBtn: {
    padding: "6px 14px",
    background: "#166534",
    color: "#4ade80",
    border: "1px solid #22c55e",
    borderRadius: 6,
    fontSize: 13,
    cursor: "pointer",
    fontWeight: 600,
  },
  moreBtn: {
    width: "100%",
    marginTop: 10,
    padding: "9px 0",
    background: "#252525",
    color: "#ddd",
    border: "1px solid #333",
    borderRadius: 7,
    fontSize: 13,
    cursor: "pointer",
  },
};
