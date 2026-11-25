/**
 * CostBadge (#301) — 모의 클라우드 비용 표시.
 *
 * Editor 상단에 작게 누적 비용 + 예상 비용을 보여주고,
 * 클릭하면 Phase별 breakdown 모달을 띄운다.
 *
 * 데이터:
 *   - GET /api/projects/{id}/cost       — 실제 누적
 *   - GET /api/projects/{id}/cost/preview — 현재 상태로 렌더 시 예상
 *
 * 프로바이더는 src/cost_estimator/pricing.json 으로 통제.
 */
import { useEffect, useState } from "react";
import { getCost, previewCost, type CostBreakdown, type PhaseCost } from "../api/client";

const PHASE_LABEL: Record<string, string> = {
  "1_scenario_parse": "1. 시나리오 파싱 (Gemini)",
  "2_footage_index": "2. footage 인덱싱 (Gemini Video)",
  "3_matching": "3. 매칭 (Gemini)",
  "4_narration": "4. 나레이션 (Google TTS)",
  "5_clip_edit": "5. 클립 편집 (ffmpeg)",
  "6_final_compose": "6. 최종 합성 (ffmpeg)",
  "7_egress": "7. 다운로드 (egress)",
  // #336 — `record_nl_edit` 가 사용하는 phase key. UI 상 다른 7번대 라벨과 헷갈리지
  // 않도록 "AI 편집" 식별자를 명시한다.
  "7_nl_edit": "7. 자연어 편집 (Gemini · AI 편집)",
  // #317 — 컨텍스트 캐시 적용분. 일반 입력 단가($0.30/M) 대비 90% 인하($0.03/M).
  "7_nl_edit_cached_input": "7. 자연어 편집 — 캐시 적용 입력 (90% 인하)",
  // #317 — 캐시 저장 비용. TTL 1시간 점유 단가($1.00/M/h).
  "7_nl_edit_cache_storage": "7. 자연어 편집 — 캐시 저장 보관",
  "8_scene_preview": "8. 씬 미리보기/썸네일 (ffmpeg + egress)",
  "9_bgm_render": "9. BGM 후처리 합성 (ffmpeg)",
  "10_recording_split": "10. 녹화 분할 (ffmpeg)",
  "10_transcode": "10. 미디어 변환 (ffmpeg)",
  // #313 — 클라(pdf.js/html2canvas) 캡처가 기본 경로가 된 이후에는 이 phase
  //  비용이 거의 0 으로 떨어진다. 백엔드 fallback 일 때만 Playwright CPU 가 잡힌다.
  "11_presentation_capture": "11. 프레젠테이션 캡처 (클라 또는 Playwright fallback)",
  "12_footage_upload": "12. footage 업로드 저장",
  "13_tts_upload": "13. TTS 업로드 저장",
  "13_tts_upload_transcode": "13. TTS 업로드 변환 (MP3→WAV)",
  "14_audio_upload": "14. 오디오 자산 업로드",
  "15_output_upload": "15. 최종 mp4 직접 업로드",
};

function fmtUsd(n: number): string {
  if (n === 0) return "$0";
  if (n < 0.0001) return `$${n.toExponential(2)}`;
  if (n < 0.01) return `$${n.toFixed(6)}`;
  if (n < 1) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

const KRW_RATE = 1380;

/** 사람이 알아먹는 원화 표기. 1원 미만은 "1원 미만", 그 위는 원/엔/만 단위. */
function fmtKrwHuman(usd: number): string {
  const won = usd * KRW_RATE;
  if (won === 0) return "0원";
  if (won < 1) return "1원 미만";
  if (won < 1000) return `${Math.round(won).toLocaleString()}원`;
  if (won < 10_000) return `${Math.round(won).toLocaleString()}원`;
  if (won < 1_000_000) return `${Math.round(won / 100) / 10}천원`;
  return `${Math.round(won / 10_000).toLocaleString()}만원`;
}

function fmtKrw(usd: number): string {
  return `₩${Math.round(usd * KRW_RATE).toLocaleString()}`;
}

interface Props {
  projectId: string;
  /** 렌더링 완료 등 변화 시 외부에서 reload 트리거. */
  reloadKey?: number;
}

export default function CostBadge({ projectId, reloadKey = 0 }: Props) {
  const [actual, setActual] = useState<CostBreakdown | null>(null);
  const [preview, setPreview] = useState<CostBreakdown | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([getCost(projectId).catch(() => null), previewCost(projectId).catch(() => null)])
      .then(([a, p]) => {
        if (!alive) return;
        setActual(a);
        setPreview(p);
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [projectId, reloadKey, tick]);

  // 주기적 폴링 — 미리보기/썸네일 등 reload 트리거 없는 API 호출도 반영하기 위함.
  // 모달이 열려 있을 때는 더 자주 갱신해서 사용자가 변화를 즉시 체감할 수 있게.
  useEffect(() => {
    const interval = open ? 3000 : 8000;
    const t = setInterval(() => setTick((x) => x + 1), interval);
    return () => clearInterval(t);
  }, [open]);

  const actualTotal = actual?.total_usd ?? 0;
  const previewTotal = preview?.total_usd ?? 0;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        style={styles.badge}
        title={
          `지금까지 사용한 비용: ${fmtUsd(actualTotal)} (${fmtKrw(actualTotal)})\n` +
          `렌더링 시 추가될 비용: ${fmtUsd(previewTotal)} (${fmtKrw(previewTotal)})\n` +
          `(Hetzner CCX23 + Cloudflare R2 기준) — 클릭하여 세부 내역`
        }
      >
        💰 <span style={styles.badgeLabel}>지금까지</span>{" "}
        <strong>{loading ? "..." : fmtKrwHuman(actualTotal)}</strong>
        {previewTotal > 0 && (
          <span style={styles.preview}>
            {" · "}<span style={styles.badgeLabel}>렌더 시 +</span>{" "}
            <strong>{fmtKrwHuman(previewTotal)}</strong>
          </span>
        )}
      </button>
      {open && (
        <CostModal
          actual={actual}
          preview={preview}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function CostModal({ actual, preview, onClose }: {
  actual: CostBreakdown | null;
  preview: CostBreakdown | null;
  onClose: () => void;
}) {
  return (
    <div style={styles.modalOverlay} onClick={onClose}>
      <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div style={styles.modalHeader}>
          <div>
            <h3 style={{ margin: 0 }}>💰 모의 클라우드 비용</h3>
            <p style={styles.subtitle}>
              프로바이더: <code>{actual?.provider ?? preview?.provider ?? "—"}</code>
              {" · "}단가 기준일: {actual?.pricing_updated_at || preview?.pricing_updated_at || "—"}
            </p>
          </div>
          <button onClick={onClose} style={styles.closeBtn}>✕</button>
        </div>

        <div style={styles.summaryRow}>
          <SummaryCard
            title="지금까지 사용한 비용"
            subtitle="시나리오 파싱·매칭·미리보기·다운로드 등 실제로 발생한 합계"
            usd={actual?.total_usd ?? 0}
            note={actual && actual.phases.length > 0 ? `${actual.phases.length}개 항목 기록됨` : "아직 기록 없음"}
          />
          <SummaryCard
            title="▶ 렌더링 누르면 추가될 비용"
            subtitle="현재 씬·TTS·footage 상태로 영상 1편 만들 때 발생할 추가 비용"
            usd={preview?.total_usd ?? 0}
            note="실제 실행 시 변동 가능"
            extra={
              // 크레딧 예상은 provider 무관하게 tts_credits 가 있으면 항상 표시한다.
              // Google 목소리를 쓰는 프로젝트에서도 "Typecast 로 만들면 N 크레딧" 을
              // 미리 보고 전환을 판단할 수 있어야 하기 때문.
              preview && preview.tts_credits != null ? (
                <TypecastCreditNote
                  credits={preview.tts_credits ?? 0}
                  usd={preview.tts_credits_usd ?? 0}
                  freeCovered={preview.tts_free_covered ?? false}
                  isActive={preview.tts_provider === "typecast"}
                />
              ) : undefined
            }
          />
        </div>

        <h4 style={styles.sectionTitle}>지금까지 발생한 비용 — 세부 내역</h4>
        {actual && actual.phases.length > 0 ? (
          <PhaseTable phases={actual.phases} />
        ) : (
          <p style={styles.empty}>아직 발생한 비용이 없습니다. 시나리오 파싱·매칭·미리보기·렌더링 등을 실행하면 여기에 누적됩니다.</p>
        )}

        {/* #355 — Gemini 모델별 API 비용 분해. 백엔드 by_model 이 있을 때만. */}
        {actual && actual.by_model && Object.keys(actual.by_model).length > 0 && (
          <>
            <h4 style={styles.sectionTitle}>Gemini 모델별 API 비용 분해</h4>
            <ByModelTable byModel={actual.by_model} />
          </>
        )}

        <h4 style={styles.sectionTitle}>렌더링 시 추가될 예상 비용 — 세부 내역</h4>
        {preview && preview.phases.length > 0 ? (
          <PhaseTable phases={preview.phases} />
        ) : (
          <p style={styles.empty}>예상 비용을 계산할 데이터가 부족합니다.</p>
        )}

        <p style={styles.footnote}>
          단가는 <code>src/cost_estimator/pricing.json</code>에서 수정합니다.
          상세 분석: <code>docs/cost-analysis.md</code>.
        </p>
      </div>
    </div>
  );
}

function SummaryCard({ title, subtitle, usd, note, extra }: {
  title: string; subtitle?: string; usd: number; note: string;
  extra?: React.ReactNode;
}) {
  return (
    <div style={styles.summaryCard}>
      <div style={styles.summaryTitle}>{title}</div>
      {subtitle && <div style={styles.summarySubtitle}>{subtitle}</div>}
      <div style={styles.summaryKrwBig}>{fmtKrwHuman(usd)}</div>
      <div style={styles.summaryUsdSmall}>{fmtUsd(usd)} · 정확: {fmtKrw(usd)}</div>
      {extra}
      <div style={styles.summaryNote}>{note}</div>
    </div>
  );
}

/** Typecast 크레딧 예상 표기. 무료 한도 내면 배지, 초과면 크레딧+USD 병기.
 *  isActive=false(현재 Google 목소리)면 "Typecast 로 만들면" 전환 예상임을 명시. */
function TypecastCreditNote({ credits, usd, freeCovered, isActive = true }: {
  credits: number; usd: number; freeCovered: boolean; isActive?: boolean;
}) {
  return (
    <div style={styles.typecastNote}>
      🎙 {isActive ? "Typecast 예상" : "Typecast로 만들면"}{" "}
      <strong>{credits.toLocaleString()} 크레딧</strong>
      {freeCovered ? (
        <span style={styles.freeBadge}>무료 한도 내</span>
      ) : (
        <span> (≈ {fmtUsd(usd)})</span>
      )}
    </div>
  );
}

/** #355 — Gemini 모델별 API 비용 분해 표. */
function ByModelTable({ byModel }: { byModel: Record<string, number> }) {
  const rows = Object.entries(byModel).sort((a, b) => b[1] - a[1]);
  const total = rows.reduce((s, [, v]) => s + v, 0);
  return (
    <table style={styles.table}>
      <thead>
        <tr>
          <th style={styles.th}>모델</th>
          <th style={styles.thNum}>API 비용 (USD)</th>
          <th style={styles.thNum}>원화 환산</th>
          <th style={styles.thNum}>비율</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([model, usd]) => (
          <tr key={model || "(unknown)"}>
            <td style={styles.td}>
              {model ? <code>{model}</code> : <span style={{ color: "#888" }}>기타/구버전(모델 미기록)</span>}
            </td>
            <td style={styles.tdNum}>{fmtUsd(usd)}</td>
            <td style={styles.tdNum}>{fmtKrwHuman(usd)}</td>
            <td style={styles.tdNum}>
              {total > 0 ? `${((usd / total) * 100).toFixed(1)}%` : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PhaseTable({ phases }: { phases: PhaseCost[] }) {
  return (
    <table style={styles.table}>
      <thead>
        <tr>
          <th style={styles.th}>Phase</th>
          <th style={styles.thNum}>API</th>
          <th style={styles.thNum}>Compute</th>
          <th style={styles.thNum}>Storage</th>
          <th style={styles.thNum}>Egress</th>
          <th style={styles.thNum}>합계</th>
        </tr>
      </thead>
      <tbody>
        {phases.map((p) => (
          <tr key={p.phase}>
            <td style={styles.td}>
              <div>{PHASE_LABEL[p.phase] ?? p.phase}</div>
              <div style={styles.usageHint}>{usageHint(p)}</div>
            </td>
            <td style={styles.tdNum}>{fmtUsd(p.api_usd)}</td>
            <td style={styles.tdNum}>{fmtUsd(p.compute_usd)}</td>
            <td style={styles.tdNum}>{fmtUsd(p.storage_usd)}</td>
            <td style={styles.tdNum}>{fmtUsd(p.egress_usd)}</td>
            <td style={{ ...styles.tdNum, fontWeight: 600 }}>
              <div>{fmtKrwHuman(p.total_usd)}</div>
              <div style={{ fontSize: 10, color: "#666", fontWeight: 400 }}>{fmtUsd(p.total_usd)}</div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function usageHint(p: PhaseCost): string {
  const parts: string[] = [];
  // #355 — 어떤 Gemini 모델로 실행된 항목인지 한눈에 보이게 모델 ID를 앞에 넣는다.
  if (p.gemini_model) {
    parts.push(`model=${p.gemini_model}`);
  }
  if (p.gemini_input_chars > 0 || p.gemini_output_chars > 0) {
    parts.push(`Gemini in/out ${p.gemini_input_chars}/${p.gemini_output_chars} chars`);
  }
  if (p.gemini_video_sec > 0) {
    parts.push(`video ${p.gemini_video_sec.toFixed(1)}s`);
  }
  if (p.tts_chars > 0) {
    parts.push(`TTS ${p.tts_chars} chars`);
  }
  if (p.cpu_seconds > 0) {
    parts.push(`CPU ${p.cpu_seconds.toFixed(1)}s`);
  }
  if (p.bytes_stored > 0) {
    parts.push(`stored ${(p.bytes_stored / (1024 * 1024)).toFixed(1)}MB`);
  }
  if (p.bytes_egress > 0) {
    parts.push(`egress ${(p.bytes_egress / (1024 * 1024)).toFixed(1)}MB`);
  }
  return parts.join(" · ");
}

const styles = {
  badge: {
    background: "#1a1a2e",
    border: "1px solid #2a2a4a",
    color: "#a78bfa",
    padding: "4px 10px",
    borderRadius: 6,
    fontSize: 12,
    cursor: "pointer",
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
    whiteSpace: "nowrap" as const,
  },
  preview: { color: "#888", marginLeft: 2 },
  badgeLabel: { color: "#888", fontWeight: 400, fontSize: 11 },
  modalOverlay: {
    position: "fixed" as const,
    inset: 0,
    background: "rgba(0,0,0,0.7)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 9999,
  },
  modal: {
    background: "#0f0f17",
    border: "1px solid #2a2a4a",
    borderRadius: 12,
    padding: 24,
    width: "min(900px, 92vw)",
    maxHeight: "88vh",
    overflowY: "auto" as const,
    color: "#ddd",
  },
  modalHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    marginBottom: 16,
  },
  subtitle: { margin: "4px 0 0 0", color: "#888", fontSize: 12 },
  closeBtn: {
    background: "transparent",
    border: "1px solid #333",
    color: "#aaa",
    width: 32,
    height: 32,
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 16,
  },
  summaryRow: { display: "flex", gap: 16, marginBottom: 24 },
  summaryCard: {
    flex: 1,
    background: "#1a1a2e",
    border: "1px solid #2a2a4a",
    borderRadius: 8,
    padding: 16,
  },
  summaryTitle: { fontSize: 13, color: "#ddd", marginBottom: 2, fontWeight: 600 },
  summarySubtitle: { fontSize: 11, color: "#888", marginBottom: 10, lineHeight: 1.4 },
  summaryKrwBig: { fontSize: 26, fontWeight: 700, color: "#a78bfa" },
  summaryUsdSmall: { fontSize: 11, color: "#777", marginTop: 4, fontFamily: "ui-monospace, SFMono-Regular, monospace" },
  summaryUsd: { fontSize: 28, fontWeight: 700, color: "#a78bfa" },
  summaryKrw: { fontSize: 14, color: "#999", marginTop: 2 },
  summaryNote: { fontSize: 11, color: "#666", marginTop: 8 },
  typecastNote: {
    fontSize: 12,
    color: "#c4b5fd",
    marginTop: 8,
    padding: "6px 8px",
    background: "#20203a",
    border: "1px solid #3a3a6a",
    borderRadius: 6,
  },
  freeBadge: {
    marginLeft: 6,
    fontSize: 11,
    color: "#4ade80",
    border: "1px solid #2f6b46",
    background: "#132a1e",
    borderRadius: 4,
    padding: "1px 6px",
  },
  sectionTitle: { margin: "20px 0 8px 0", color: "#bbb", fontSize: 14 },
  empty: { color: "#666", fontSize: 13, padding: "12px 0" },
  table: { width: "100%", borderCollapse: "collapse" as const, fontSize: 12 },
  th: {
    textAlign: "left" as const,
    padding: "8px 10px",
    background: "#1a1a2e",
    borderBottom: "1px solid #2a2a4a",
    color: "#aaa",
    fontWeight: 500,
  },
  thNum: {
    textAlign: "right" as const,
    padding: "8px 10px",
    background: "#1a1a2e",
    borderBottom: "1px solid #2a2a4a",
    color: "#aaa",
    fontWeight: 500,
  },
  td: { padding: "8px 10px", borderBottom: "1px solid #1a1a2e" },
  tdNum: {
    padding: "8px 10px",
    borderBottom: "1px solid #1a1a2e",
    textAlign: "right" as const,
    fontFamily: "ui-monospace, SFMono-Regular, monospace",
  },
  usageHint: { fontSize: 10, color: "#666", marginTop: 2 },
  footnote: { marginTop: 20, color: "#666", fontSize: 11 },
};
