import { useEffect, useRef, useState } from "react";
import { dbGetSettings, dbSaveSettings, dbSaveGcpKey } from "../lib/db";
import { useTtsPreview } from "../lib/ttsPreview";
import { TTS_VOICES as TTS_VOICE_LIST } from "../lib/ttsVoices";
import { GEMINI_ENABLED } from "../lib/features";
import type { AppSettings } from "../types";

// Settings 화면은 value 만 필요하고 label 은 자동 표시(존재 시)한다.
const TTS_VOICES = TTS_VOICE_LIST.map((v) => v.value);
const TTS_VOICE_LABEL: Record<string, string> = Object.fromEntries(
  TTS_VOICE_LIST.map((v) => [v.value, v.label]),
);

export default function Settings({ onBack, onMigrate }: { onBack: () => void; onMigrate?: () => void }) {
  const [form, setForm] = useState<Omit<AppSettings, "gcp_key_configured">>({
    google_api_key: "",
    gemini_request_interval_sec: "4",
    gemini_max_retry: "3",
    tts_voice_name: "ko-KR-Chirp3-HD-Orus",
    tts_speaking_rate: "0.9",
  });
  const [gcpConfigured, setGcpConfigured] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [gcpUploading, setGcpUploading] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [gcpError, setGcpError] = useState("");
  const [gcpSuccess, setGcpSuccess] = useState("");
  const [showApiKey, setShowApiKey] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const ttsPreview = useTtsPreview();

  useEffect(() => {
    dbGetSettings()
      .then((s) => {
        setForm({
          google_api_key: s.google_api_key,
          gemini_request_interval_sec: s.gemini_request_interval_sec,
          gemini_max_retry: s.gemini_max_retry,
          tts_voice_name: s.tts_voice_name,
          tts_speaking_rate: s.tts_speaking_rate,
        });
        setGcpConfigured(s.gcp_key_configured);
      })
      .catch(() => setError("설정을 불러오지 못했습니다."))
      .finally(() => setLoading(false));
  }, []);

  async function handleSave() {
    setSaving(true);
    setError("");
    setSaved(false);
    try {
      await dbSaveSettings(form);
      // 서버가 진실의 원천 — 서버 .env에도 저장. 다른 origin/기기에서도 키가 인식된다.
      const { updateSettings } = await import("../api/client");
      await updateSettings(form).catch((e) => {
        console.warn("[settings] 서버 저장 실패(로컬에는 저장됨):", e);
      });
      // 키 메모리 캐시 무효화 — 변경 즉시 반영.
      const { invalidateGoogleApiKeyCache } = await import("../lib/apiKey");
      invalidateGoogleApiKeyCache();
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch {
      setError("저장에 실패했습니다.");
    } finally {
      setSaving(false);
    }
  }

  async function handleGcpUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setGcpUploading(true);
    setGcpError("");
    setGcpSuccess("");
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      await dbSaveGcpKey(text);
      setGcpConfigured(true);
      setGcpSuccess(`업로드 완료 (프로젝트: ${parsed.project_id ?? "—"})`);
    } catch {
      setGcpError("업로드 실패 — 올바른 JSON 파일인지 확인하세요.");
    } finally {
      setGcpUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function set(key: keyof typeof form, val: string) {
    setForm((prev) => ({ ...prev, [key]: val }));
  }

  if (loading) {
    // #297 — 한 줄 텍스트 대신 스피너 + 메시지.
    return (
      <div style={styles.container}>
        <style>{"@keyframes va-spin { to { transform: rotate(360deg); } }"}</style>
        <div style={{ ...styles.card, display: "flex", flexDirection: "column", alignItems: "center", gap: 16, padding: "48px 40px" }}>
          <div style={{ width: 32, height: 32, border: "3px solid #2a2a2a", borderTopColor: "#6366f1", borderRadius: "50%", animation: "va-spin 0.8s linear infinite" }} />
          <p style={{ color: "#888", margin: 0 }}>설정 불러오는 중...</p>
        </div>
      </div>
    );
  }

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <div style={styles.header}>
          <button style={styles.backBtn} onClick={onBack}>← 홈</button>
          <h1 style={styles.title}>설정</h1>
        </div>

        {error && <div style={styles.errorBox}>{error}</div>}

        {/* ── Google API 키 ───────────────────────────────── */}
        {/* 키는 브라우저 TTS 미리듣기에도 쓰여 항상 노출. Gemini 전용 항목(요청 간격·재시도)은
            Gemini 기능이 켜졌을 때만 보인다(lib/features.ts). */}
        <section style={styles.section}>
          <div style={styles.sectionTitle}>{GEMINI_ENABLED ? "Gemini API" : "Google API 키"}</div>

          <label style={styles.label}>Google API Key</label>
          <div style={styles.apiKeyRow}>
            <input
              type={showApiKey ? "text" : "password"}
              style={styles.input}
              value={form.google_api_key}
              onChange={(e) => set("google_api_key", e.target.value)}
              placeholder="AIzaSy..."
              autoComplete="off"
            />
            <button
              style={styles.eyeBtn}
              onClick={() => setShowApiKey((v) => !v)}
              title={showApiKey ? "숨기기" : "표시"}
            >
              {showApiKey ? "숨김" : "표시"}
            </button>
          </div>
          <p style={styles.hint}>
            {GEMINI_ENABLED
              ? "Google AI Studio에서 발급 — 매칭·씬 분석과 TTS 미리듣기에 사용됩니다."
              : "Google Cloud 콘솔에서 발급 — 브라우저 TTS 미리듣기에 사용됩니다."}
          </p>

          {GEMINI_ENABLED && (
          <div style={styles.row2}>
            <div style={styles.fieldGroup}>
              <label style={styles.label}>요청 간격 (초)</label>
              <input
                type="number"
                style={styles.inputSm}
                value={form.gemini_request_interval_sec}
                onChange={(e) => set("gemini_request_interval_sec", e.target.value)}
                min="0"
                step="1"
              />
              <p style={styles.hint}>무료 티어: 4 / 유료 티어: 1 이하</p>
            </div>
            <div style={styles.fieldGroup}>
              <label style={styles.label}>최대 재시도 횟수</label>
              <input
                type="number"
                style={styles.inputSm}
                value={form.gemini_max_retry}
                onChange={(e) => set("gemini_max_retry", e.target.value)}
                min="0"
                step="1"
              />
            </div>
          </div>
          )}
        </section>

        {/* ── Google Cloud TTS ───────────────────────────── */}
        <section style={styles.section}>
          <div style={styles.sectionTitle}>Google Cloud TTS</div>

          <label style={styles.label}>TTS 음성</label>
          <div style={styles.voiceRow}>
            <select
              style={{ ...styles.select, flex: 1 }}
              value={form.tts_voice_name}
              onChange={(e) => set("tts_voice_name", e.target.value)}
            >
              {TTS_VOICES.map((v) => (
                <option key={v} value={v}>{TTS_VOICE_LABEL[v] ?? v}</option>
              ))}
              {!TTS_VOICES.includes(form.tts_voice_name) && (
                <option value={form.tts_voice_name}>{form.tts_voice_name}</option>
              )}
            </select>
            {(() => {
              const playing = ttsPreview.playingVoice === form.tts_voice_name;
              return (
                <button
                  type="button"
                  style={styles.previewBtn}
                  onClick={() => ttsPreview.preview(form.tts_voice_name, parseFloat(form.tts_speaking_rate))}
                  disabled={ttsPreview.loading}
                  title={playing ? "미리듣기 정지" : "선택한 목소리로 샘플 재생"}
                >
                  {ttsPreview.loading ? "…" : playing ? "■ 정지" : "▶ 미리듣기"}
                </button>
              );
            })()}
          </div>
          {ttsPreview.error && (
            <p style={{ ...styles.hint, color: "#f87171" }}>미리듣기 실패: {ttsPreview.error}</p>
          )}

          <label style={styles.label}>말하기 속도</label>
          <div style={styles.rateRow}>
            <input
              type="range"
              min="0.5"
              max="2.0"
              step="0.05"
              value={form.tts_speaking_rate}
              onChange={(e) => set("tts_speaking_rate", e.target.value)}
              style={styles.slider}
            />
            <span style={styles.rateValue}>{parseFloat(form.tts_speaking_rate).toFixed(2)}</span>
          </div>

          {/* GCP 서비스 계정 키 */}
          <label style={styles.label}>
            GCP 서비스 계정 키 (JSON)
            {gcpConfigured && <span style={styles.badge}>등록됨</span>}
          </label>
          <p style={styles.hint}>
            Google Cloud Console에서 다운로드한 서비스 계정 JSON 키 파일을 업로드하세요.
          </p>
          <div style={styles.uploadRow}>
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,application/json"
              style={{ display: "none" }}
              onChange={handleGcpUpload}
            />
            <button
              style={styles.uploadBtn}
              onClick={() => fileInputRef.current?.click()}
              disabled={gcpUploading}
            >
              {gcpUploading ? "업로드 중..." : "JSON 파일 선택"}
            </button>
          </div>
          {gcpSuccess && <div style={styles.successBox}>{gcpSuccess}</div>}
          {gcpError && <div style={styles.errorBox}>{gcpError}</div>}
        </section>

        {/* ── 데이터 가져오기 (#298) ─────────────────────── */}
        {onMigrate && (
          <section style={styles.section}>
            <div style={styles.sectionTitle}>데이터 가져오기</div>
            <p style={styles.hint}>
              v1.x 백엔드(state.json)에 저장된 프로젝트를 v2.x 브라우저 저장소로 옮길 때만 사용하는
              일회성 기능입니다. 평소에는 사용할 필요가 없습니다.
            </p>
            <div>
              <button style={styles.migrateBtn} onClick={onMigrate}>
                마이그레이션 화면 열기 →
              </button>
            </div>
          </section>
        )}

        {/* ── Save ───────────────────────────────────────── */}
        <div style={styles.saveRow}>
          {saved && <span style={styles.savedMsg}>저장됐습니다</span>}
          <button style={styles.saveBtn} onClick={handleSave} disabled={saving}>
            {saving ? "저장 중..." : "저장"}
          </button>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: { minHeight: "100vh", background: "#0f0f0f", display: "flex", alignItems: "flex-start", justifyContent: "center", padding: "60px 24px" },
  card: { width: "100%", maxWidth: 600, background: "#1a1a1a", borderRadius: 16, padding: 40, boxShadow: "0 4px 40px rgba(0,0,0,0.6)" },
  header: { display: "flex", alignItems: "center", gap: 16, marginBottom: 32 },
  backBtn: { background: "none", border: "1px solid #333", color: "#aaa", padding: "6px 14px", borderRadius: 7, cursor: "pointer", fontSize: 13 },
  title: { color: "#fff", fontSize: 24, margin: 0 },
  section: { borderTop: "1px solid #2a2a2a", paddingTop: 24, marginBottom: 24, display: "flex", flexDirection: "column", gap: 10 },
  sectionTitle: { color: "#6366f1", fontSize: 11, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: 1, marginBottom: 4 },
  label: { color: "#ccc", fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 8 },
  hint: { color: "#666", fontSize: 12, margin: 0 },
  input: { padding: "10px 14px", borderRadius: 7, border: "1px solid #333", background: "#111", color: "#fff", fontSize: 14, width: "100%", boxSizing: "border-box" as const },
  inputSm: { padding: "8px 12px", borderRadius: 7, border: "1px solid #333", background: "#111", color: "#fff", fontSize: 14, width: "100%", boxSizing: "border-box" as const },
  apiKeyRow: { display: "flex", gap: 8 },
  eyeBtn: { padding: "0 14px", background: "#252525", border: "1px solid #333", color: "#aaa", borderRadius: 7, cursor: "pointer", fontSize: 12, whiteSpace: "nowrap" as const, flexShrink: 0 },
  select: { padding: "10px 14px", borderRadius: 7, border: "1px solid #333", background: "#111", color: "#fff", fontSize: 14, width: "100%" },
  voiceRow: { display: "flex", gap: 8, alignItems: "center" },
  previewBtn: { padding: "10px 14px", borderRadius: 7, border: "1px solid #333", background: "#252525", color: "#a5b4fc", fontSize: 13, cursor: "pointer", whiteSpace: "nowrap" as const, flexShrink: 0 },
  row2: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 },
  fieldGroup: { display: "flex", flexDirection: "column", gap: 6 },
  rateRow: { display: "flex", alignItems: "center", gap: 12 },
  slider: { flex: 1, accentColor: "#6366f1" },
  rateValue: { color: "#fff", fontSize: 14, fontVariantNumeric: "tabular-nums" as const, width: 36 },
  uploadRow: { display: "flex", gap: 8 },
  uploadBtn: { padding: "9px 18px", background: "#252525", border: "1px solid #444", color: "#ccc", borderRadius: 7, cursor: "pointer", fontSize: 13 },
  migrateBtn: { padding: "9px 18px", background: "#252525", border: "1px solid #444", color: "#ccc", borderRadius: 7, cursor: "pointer", fontSize: 13 },
  badge: { background: "#14532d", color: "#4ade80", fontSize: 11, padding: "2px 8px", borderRadius: 4, fontWeight: 600 },
  saveRow: { display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 16, marginTop: 8 },
  saveBtn: { padding: "12px 32px", background: "#6366f1", color: "#fff", border: "none", borderRadius: 8, fontSize: 15, cursor: "pointer", fontWeight: 600 },
  savedMsg: { color: "#4ade80", fontSize: 13 },
  errorBox: { background: "#3b0e0e", color: "#f87171", padding: "10px 14px", borderRadius: 7, fontSize: 13 },
  successBox: { background: "#0d2e1a", color: "#4ade80", padding: "10px 14px", borderRadius: 7, fontSize: 13 },
};
