import { useState, useEffect } from "react";
import type { TextOverlay, GradientStop, TextOverlayPreset, FootageAsset, TextEnterExitEffect, TextLoopAnimation } from "../types";
import {
  dbListOverlayPresets,
  dbSaveOverlayPreset,
  dbDeleteOverlayPreset,
} from "../lib/db";
import { isBuiltinPreset } from "../lib/textPresetSeeds";
import { ui } from "../store/ui";
import SfxEditor from "./SfxEditor";

interface Props {
  overlay: TextOverlay | null; // null이면 신규 생성
  onSave: (overlay: TextOverlay) => void;
  onClose: () => void;
  // #290 — SFX 편집을 위한 컨텍스트. 미전달 시 SFX 섹션 숨김(회귀 안전).
  projectId?: string;
  audioAssets?: FootageAsset[];
}

function randomId(): string {
  return Math.random().toString(36).slice(2, 10);
}

const FONT_OPTIONS = [
  { value: "NotoSansKR", label: "Noto Sans KR (기본)" },
  { value: "DungGeunMo", label: "둥근모" },
  { value: "BMDOHYEON", label: "BM도현체" },
  { value: "Dongle", label: "동글 (귀여움)" },
  { value: "PF_STARDUST3", label: "PF 스타더스트" },
  { value: "Jalnan", label: "여기어때 잘난체 (굵은 강조)" },
  { value: "BMJUA", label: "BM주아 (귀여움)" },
  { value: "Cafe24Ssurround", label: "카페24 써라운드 (둥근)" },
  { value: "Dovemayo-Wild", label: "도브메이요 와일드 (손글씨)" },
];

// #287 — 텍스트 오버레이 전용 등장/사라짐 효과 옵션 (4종)
const TEXT_EFFECT_OPTIONS: { value: TextEnterExitEffect; label: string }[] = [
  { value: "none", label: "없음" },
  { value: "fade", label: "페이드" },
  { value: "typing", label: "타이핑" },
  { value: "zoom", label: "줌" },
  // 관통 — 한쪽에서 날아와 반대편으로 빠져나간다. 등장에만 고르면 사라짐 방향은 자동으로 반대편.
  { value: "pass_left_to_right", label: "관통 (왼→오)" },
  { value: "pass_right_to_left", label: "관통 (오→왼)" },
  { value: "pass_top_to_bottom", label: "관통 (위→아래)" },
  { value: "pass_bottom_to_top", label: "관통 (아래→위)" },
];

// 표시 중 반복 애니메이션 옵션 (5종).
// pulse 는 그라디언트/글로우 텍스트(PNG overlay 분기)에서는 정적으로 폴백된다 — UI 안내 텍스트로 표시.
const LOOP_ANIM_OPTIONS: { value: TextLoopAnimation; label: string }[] = [
  { value: "none", label: "없음" },
  { value: "bounce", label: "통통 (위아래)" },
  { value: "shake", label: "흔들림 (좌우 빠름)" },
  { value: "pulse", label: "숨쉬기 (크기)" },
  { value: "wave", label: "물결 (좌우 부드럽게)" },
  { value: "speedlines", label: "집중선 (꼬리/글자안쪽)" },
];

const DEFAULT_OVERLAY: TextOverlay = {
  id: "",
  text: "",
  x: 0.5,
  y: 0.1,
  font_size: 40,
  font_family: "NotoSansKR",
  color: "#ffffff",
  gradient: null,
  bold: false,
  italic: false,
  outline_color: "#000000",
  outline_width: 2,
  glow_color: "#000000",
  glow_radius: 0,
  opacity: 1.0,
  align: "center",
  start_sec: 0.0,
  end_sec: null,
  // #290 — SFX 기본값 (없음 + 0.7).
  sfx_asset_id: null,
  sfx_volume: 0.7,
  // #287 — 등장/사라짐 효과 기본값 (fade + 0.3초)
  enter_effect: "fade",
  exit_effect: "fade",
  enter_duration_sec: 0.3,
  exit_duration_sec: 0.3,
  // 표시 중 반복 애니메이션 기본값 (없음).
  loop_animation: "none",
  loop_amplitude: 0.02,
  loop_period_sec: 0.8,
  // 카운트다운 기본값 — 0이면 꺼짐(일반 텍스트).
  countdown_from: 0,
  countdown_to: 1,
  countdown_interval_sec: 1.0,
  countdown_end_text: "",
};

type ColorMode = "solid" | "gradient";

export default function TextOverlayEditor({ overlay, onSave, onClose, projectId, audioAssets }: Props) {
  const [form, setForm] = useState<TextOverlay>(
    overlay ? { ...DEFAULT_OVERLAY, ...overlay } : { ...DEFAULT_OVERLAY, id: randomId() }
  );
  const [colorMode, setColorMode] = useState<ColorMode>(
    overlay?.gradient ? "gradient" : "solid"
  );
  const [presets, setPresets] = useState<TextOverlayPreset[]>([]);
  const [showPresetList, setShowPresetList] = useState(false);
  // #290 — 프리셋 저장 다이얼로그에서 "SFX 포함" 토글에 사용.
  const [presetIncludeSfx, setPresetIncludeSfx] = useState<boolean>(false);

  useEffect(() => {
    dbListOverlayPresets().then(setPresets).catch(console.error);
  }, []);

  function update<K extends keyof TextOverlay>(key: K, value: TextOverlay[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSavePreset() {
    const name = await ui.prompt({
      title: "프리셋 이름",
      placeholder: "프리셋 이름을 입력하세요",
      confirmLabel: "저장",
      cancelLabel: "취소",
    });
    if (!name || !name.trim()) return;
    const { id: _id, text: _text, start_sec: _s, end_sec: _e, ...styleFields } = form;
    // #290 — "SFX 포함" 토글이 꺼져 있으면 프리셋에서 SFX 필드 제거하여 저장.
    //        포함이면 sfx_asset_id/sfx_volume 유지. include_sfx 플래그도 함께 저장.
    const styleForPreset = presetIncludeSfx
      ? styleFields
      : { ...styleFields, sfx_asset_id: null, sfx_volume: 0.7 };
    const preset: TextOverlayPreset = {
      id: randomId(),
      name: name.trim(),
      created_at: new Date().toISOString(),
      style: styleForPreset,
      include_sfx: presetIncludeSfx,
    };
    await dbSaveOverlayPreset(preset);
    const updated = await dbListOverlayPresets();
    setPresets(updated);
  }

  async function handleLoadPreset(preset: TextOverlayPreset) {
    // #290 — include_sfx === false(또는 미지정)이면 SFX 필드는 적용하지 않는다.
    //        true면 style 내 sfx_asset_id/sfx_volume을 그대로 복사.
    const styleApply = preset.include_sfx
      ? preset.style
      : { ...preset.style, sfx_asset_id: null, sfx_volume: 0.7 };
    setForm((prev) => ({ ...prev, ...styleApply }));
    setColorMode(preset.style.gradient ? "gradient" : "solid");
    setShowPresetList(false);
  }

  async function handleDeletePreset(id: string) {
    await dbDeleteOverlayPreset(id);
    const updated = await dbListOverlayPresets();
    setPresets(updated);
  }

  function handleSave() {
    // 카운트다운은 숫자를 직접 그리므로 text 가 비어 있어도 저장할 수 있어야 한다.
    const isCountdown = (form.countdown_from ?? 0) > 0;
    if (!isCountdown && !form.text.trim()) {
      ui.error("텍스트를 입력하세요.");
      return;
    }
    const saved: TextOverlay = {
      ...form,
      gradient: colorMode === "gradient" ? form.gradient : null,
    };
    onSave(saved);
  }

  // 그라데이션 스톱 관리
  function addGradientStop() {
    const cur = form.gradient ?? { angle: 0, stops: [] };
    const maxPos = cur.stops.length > 0 ? Math.max(...cur.stops.map((s) => s.position)) : 0.0;
    const newPos = Math.min(parseFloat(((maxPos + 1.0) / 2).toFixed(2)), 1.0);
    const stops: GradientStop[] = [...cur.stops, { color: "#ffffff", position: newPos }];
    setForm((prev) => ({ ...prev, gradient: { ...cur, stops } }));
  }

  function removeGradientStop(idx: number) {
    const cur = form.gradient ?? { angle: 0, stops: [] };
    const stops = cur.stops.filter((_, i) => i !== idx);
    setForm((prev) => ({ ...prev, gradient: { ...cur, stops } }));
  }

  function updateGradientStop(idx: number, field: keyof GradientStop, value: string | number) {
    const cur = form.gradient ?? { angle: 0, stops: [] };
    const stops = cur.stops.map((s, i) =>
      i === idx ? { ...s, [field]: value } : s
    );
    setForm((prev) => ({ ...prev, gradient: { ...cur, stops } }));
  }

  function updateGradientAngle(angle: number) {
    const cur = form.gradient ?? { angle: 0, stops: [] };
    setForm((prev) => ({ ...prev, gradient: { ...cur, angle } }));
  }

  return (
    <div style={styles.backdrop} onClick={onClose}>
      <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div style={styles.header}>
          <span style={styles.title}>{overlay ? "텍스트 오버레이 수정" : "텍스트 오버레이 추가"}</span>
          <button style={styles.closeBtn} onClick={onClose}>x</button>
        </div>

        <div style={styles.body}>
          {/* 텍스트 입력 */}
          <label style={styles.label}>텍스트</label>
          <textarea
            style={styles.textarea}
            value={form.text}
            onChange={(e) => update("text", e.target.value)}
            rows={3}
            placeholder={(form.countdown_from ?? 0) > 0 ? "카운트다운 사용 중 — 이 텍스트는 무시됩니다" : "표시할 텍스트..."}
            disabled={(form.countdown_from ?? 0) > 0}
          />

          {/* ── 카운트다운 ─────────────────────────────────────────────
              켜면 text 대신 숫자가 1씩 줄어들며 표시된다. 폰트·색·외곽선·위치·
              애니메이션 등 아래의 모든 스타일 설정이 그대로 적용된다. */}
          <label style={styles.label}>
            <input
              type="checkbox"
              checked={(form.countdown_from ?? 0) > 0}
              onChange={(e) => update("countdown_from", e.target.checked ? 3 : 0)}
              style={{ marginRight: 6 }}
            />
            카운트다운 (숫자가 줄어드는 애니메이션)
          </label>
          {(form.countdown_from ?? 0) > 0 && (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 8 }}>
              <label style={{ fontSize: 12, color: "#aaa" }}>
                시작 숫자
                <input
                  type="number" min={1} max={120}
                  style={{ ...styles.hexInput, width: 70, marginLeft: 6 }}
                  value={form.countdown_from ?? 3}
                  onChange={(e) => update("countdown_from", Math.max(1, Math.min(120, parseInt(e.target.value) || 1)))}
                />
              </label>
              <label style={{ fontSize: 12, color: "#aaa" }}>
                끝 숫자
                <input
                  type="number" min={0} max={120}
                  style={{ ...styles.hexInput, width: 70, marginLeft: 6 }}
                  value={form.countdown_to ?? 1}
                  onChange={(e) => update("countdown_to", Math.max(0, Math.min(120, parseInt(e.target.value) || 0)))}
                />
              </label>
              <label style={{ fontSize: 12, color: "#aaa" }}>
                간격(초)
                <input
                  type="number" min={0.05} max={60} step={0.1}
                  style={{ ...styles.hexInput, width: 80, marginLeft: 6 }}
                  value={form.countdown_interval_sec ?? 1.0}
                  onChange={(e) => update("countdown_interval_sec", Math.max(0.05, Math.min(60, parseFloat(e.target.value) || 1)))}
                />
              </label>
              <label style={{ fontSize: 12, color: "#aaa" }}>
                끝난 뒤 문구
                <input
                  type="text"
                  style={{ ...styles.hexInput, width: 120, marginLeft: 6 }}
                  value={form.countdown_end_text ?? ""}
                  placeholder="예: 시작!"
                  onChange={(e) => update("countdown_end_text", e.target.value)}
                />
              </label>
            </div>
          )}

          {/* 폰트 선택 */}
          <label style={styles.label}>폰트</label>
          <select
            style={styles.select}
            value={form.font_family}
            onChange={(e) => update("font_family", e.target.value)}
          >
            {FONT_OPTIONS.map((f) => (
              <option key={f.value} value={f.value}>{f.label}</option>
            ))}
          </select>

          {/* 폰트 크기 */}
          <label style={styles.label}>폰트 크기 ({form.font_size})</label>
          <input
            type="range" min={8} max={120} value={form.font_size}
            style={styles.range}
            onChange={(e) => update("font_size", Number(e.target.value))}
          />

          {/* 굵게 / 기울임 / 정렬 */}
          <div style={styles.row}>
            <button
              style={{ ...styles.toggleBtn, ...(form.bold ? styles.toggleActive : {}) }}
              onClick={() => update("bold", !form.bold)}
            >B</button>
            <button
              style={{ ...styles.toggleBtn, ...(form.italic ? styles.toggleActive : {}), fontStyle: "italic" }}
              onClick={() => update("italic", !form.italic)}
            >I</button>
            <span style={{ marginLeft: 8, color: "#aaa", fontSize: 12 }}>정렬:</span>
            {(["left", "center", "right"] as const).map((a) => (
              <button
                key={a}
                style={{ ...styles.toggleBtn, ...(form.align === a ? styles.toggleActive : {}) }}
                onClick={() => update("align", a)}
              >{a[0].toUpperCase()}</button>
            ))}
          </div>

          {/* 색상 탭 */}
          <div style={styles.row}>
            <span style={{ color: "#aaa", fontSize: 12 }}>색상 모드:</span>
            <button
              style={{ ...styles.toggleBtn, ...(colorMode === "solid" ? styles.toggleActive : {}) }}
              onClick={() => setColorMode("solid")}
            >단색</button>
            <button
              style={{ ...styles.toggleBtn, ...(colorMode === "gradient" ? styles.toggleActive : {}) }}
              onClick={() => {
                setColorMode("gradient");
                if (!form.gradient) {
                  setForm((prev) => ({
                    ...prev,
                    gradient: { angle: 0, stops: [{ color: "#ffffff", position: 0 }, { color: "#000000", position: 1 }] },
                  }));
                }
              }}
            >그라데이션</button>
          </div>

          {colorMode === "solid" && (
            <div style={styles.row}>
              <span style={{ color: "#aaa", fontSize: 12 }}>색상:</span>
              <input type="color" value={form.color}
                style={styles.colorInput}
                onChange={(e) => update("color", e.target.value)} />
              <input type="text" value={form.color}
                style={styles.hexInput}
                onChange={(e) => update("color", e.target.value)} />
            </div>
          )}

          {colorMode === "gradient" && (
            <div style={{ marginBottom: 8 }}>
              <div style={styles.row}>
                <span style={{ color: "#aaa", fontSize: 12 }}>각도 ({form.gradient?.angle ?? 0}deg):</span>
                <input type="range" min={0} max={360} value={form.gradient?.angle ?? 0}
                  style={styles.range}
                  onChange={(e) => updateGradientAngle(Number(e.target.value))} />
              </div>
              {(form.gradient?.stops ?? []).map((stop, idx) => (
                <div key={idx} style={{ ...styles.row, marginBottom: 4 }}>
                  <span style={{ color: "#aaa", fontSize: 11, minWidth: 40 }}>스톱{idx + 1}</span>
                  <input type="color" value={stop.color}
                    style={styles.colorInput}
                    onChange={(e) => updateGradientStop(idx, "color", e.target.value)} />
                  <input type="number" min={0} max={1} step={0.01} value={stop.position}
                    style={{ ...styles.hexInput, width: 60 }}
                    onChange={(e) => updateGradientStop(idx, "position", Number(e.target.value))} />
                  <button style={styles.removeBtn} onClick={() => removeGradientStop(idx)}>-</button>
                </div>
              ))}
              <button style={styles.addBtn} onClick={addGradientStop}>+ 스톱 추가</button>
            </div>
          )}

          {/* 테두리 */}
          <label style={styles.label}>테두리 두께 ({form.outline_width})</label>
          <input type="range" min={0} max={20} value={form.outline_width}
            style={styles.range}
            onChange={(e) => update("outline_width", Number(e.target.value))} />
          <div style={styles.row}>
            <span style={{ color: "#aaa", fontSize: 12 }}>테두리 색상:</span>
            <input type="color" value={form.outline_color}
              style={styles.colorInput}
              onChange={(e) => update("outline_color", e.target.value)} />
          </div>

          {/* 글로우 */}
          <label style={styles.label}>글로우 반경 ({form.glow_radius})</label>
          <input type="range" min={0} max={50} value={form.glow_radius}
            style={styles.range}
            onChange={(e) => update("glow_radius", Number(e.target.value))} />
          <div style={styles.row}>
            <span style={{ color: "#aaa", fontSize: 12 }}>글로우 색상:</span>
            <input type="color" value={form.glow_color}
              style={styles.colorInput}
              onChange={(e) => update("glow_color", e.target.value)} />
          </div>

          {/* 불투명도 */}
          <label style={styles.label}>불투명도 ({form.opacity.toFixed(2)})</label>
          <input type="range" min={0} max={1} step={0.01} value={form.opacity}
            style={styles.range}
            onChange={(e) => update("opacity", Number(e.target.value))} />

          {/* 위치 */}
          <div style={styles.row}>
            <span style={{ color: "#aaa", fontSize: 12 }}>위치 X:</span>
            <input type="number" min={0} max={1} step={0.01} value={form.x}
              style={{ ...styles.hexInput, width: 70 }}
              onChange={(e) => update("x", Number(e.target.value))} />
            <span style={{ color: "#aaa", fontSize: 12, marginLeft: 8 }}>Y:</span>
            <input type="number" min={0} max={1} step={0.01} value={form.y}
              style={{ ...styles.hexInput, width: 70 }}
              onChange={(e) => update("y", Number(e.target.value))} />
          </div>

          {/* 표시 시간 */}
          <div style={styles.row}>
            <span style={{ color: "#aaa", fontSize: 12 }}>표시 시작(s):</span>
            <input type="number" min={0} step={0.1} value={form.start_sec}
              style={{ ...styles.hexInput, width: 70 }}
              onChange={(e) => update("start_sec", Number(e.target.value))} />
            <span style={{ color: "#aaa", fontSize: 12, marginLeft: 8 }}>종료(s):</span>
            <input type="number" min={0} step={0.1}
              value={form.end_sec ?? ""}
              placeholder="끝까지"
              style={{ ...styles.hexInput, width: 70 }}
              onChange={(e) => update("end_sec", e.target.value === "" ? null : Number(e.target.value))} />
          </div>

          {/* #287 — 등장/사라짐 효과 (텍스트 전용: none/fade/typing/zoom) */}
          <label style={styles.label}>
            등장 효과 ({form.enter_effect ?? "fade"} · {(form.enter_duration_sec ?? 0.3).toFixed(2)}초)
          </label>
          <div style={styles.row}>
            <select
              style={{ ...styles.select, flex: 1, marginBottom: 0 }}
              value={form.enter_effect ?? "fade"}
              onChange={(e) => update("enter_effect", e.target.value as TextEnterExitEffect)}
            >
              {TEXT_EFFECT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          {(form.enter_effect ?? "fade") !== "none" && (
            <input
              type="range" min={0} max={2} step={0.05}
              value={form.enter_duration_sec ?? 0.3}
              style={styles.range}
              onChange={(e) => update("enter_duration_sec", Number(e.target.value))}
            />
          )}

          <label style={styles.label}>
            사라짐 효과 ({form.exit_effect ?? "fade"} · {(form.exit_duration_sec ?? 0.3).toFixed(2)}초)
          </label>
          <div style={styles.row}>
            <select
              style={{ ...styles.select, flex: 1, marginBottom: 0 }}
              value={form.exit_effect ?? "fade"}
              onChange={(e) => update("exit_effect", e.target.value as TextEnterExitEffect)}
            >
              {TEXT_EFFECT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          {(form.exit_effect ?? "fade") !== "none" && (
            <input
              type="range" min={0} max={2} step={0.05}
              value={form.exit_duration_sec ?? 0.3}
              style={styles.range}
              onChange={(e) => update("exit_duration_sec", Number(e.target.value))}
            />
          )}

          {/* 표시 중 반복 애니메이션 (bounce/shake/pulse/wave). 'none' 이면 진폭/주기 컨트롤 숨김. */}
          <label style={styles.label}>
            반복 애니메이션 ({form.loop_animation ?? "none"})
          </label>
          <div style={styles.row}>
            <select
              style={{ ...styles.select, flex: 1, marginBottom: 0 }}
              value={form.loop_animation ?? "none"}
              onChange={(e) => update("loop_animation", e.target.value as TextLoopAnimation)}
            >
              {LOOP_ANIM_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          {/* 집중선은 진폭/주기 대신 전용 파라미터(밀도/속도/색)를 쓴다. */}
          {form.loop_animation === "speedlines" && (
            <>
              <label style={styles.label}>표현 방식</label>
              <select
                style={styles.input}
                value={form.text_speed_lines?.mode ?? "tail"}
                onChange={(e) =>
                  update("text_speed_lines", {
                    ...(form.text_speed_lines ?? {}),
                    mode: e.target.value as "tail" | "inner",
                  })
                }
              >
                <option value="tail">꼬리 (날아온 방향으로 잔상)</option>
                <option value="inner">글자 안쪽 (획을 갉아냄)</option>
              </select>
              {(form.text_speed_lines?.mode ?? "tail") === "tail" ? (
                <>
                  <div style={{ color: "#94a3b8", fontSize: 11, padding: "4px 0" }}>
                    글자가 날아온 쪽으로 집중선 잔상을 늘어뜨립니다. 글자 자체는 가리지
                    않아 잘 읽힙니다. <b>등장 효과를 "관통"으로 지정해야</b> 방향이 생깁니다.
                  </div>
                  <label style={styles.label}>
                    꼬리 길이 ({((form.text_speed_lines?.tail_length ?? 0.55) * 100).toFixed(0)}%)
                  </label>
                  <input
                    type="range" min={0.1} max={1.5} step={0.05}
                    value={form.text_speed_lines?.tail_length ?? 0.55}
                    style={styles.range}
                    onChange={(e) =>
                      update("text_speed_lines", {
                        ...(form.text_speed_lines ?? {}),
                        tail_length: Number(e.target.value),
                      })
                    }
                  />
                  <label style={styles.label}>
                    선 간격 ({form.text_speed_lines?.tail_stripe ?? 9}px)
                  </label>
                  <input
                    type="range" min={3} max={24} step={1}
                    value={form.text_speed_lines?.tail_stripe ?? 9}
                    style={styles.range}
                    onChange={(e) =>
                      update("text_speed_lines", {
                        ...(form.text_speed_lines ?? {}),
                        tail_stripe: Number(e.target.value),
                      })
                    }
                  />
                  <label style={styles.label}>
                    진하기 ({((form.text_speed_lines?.tail_opacity ?? 0.85) * 100).toFixed(0)}%)
                  </label>
                  <input
                    type="range" min={0.1} max={1.0} step={0.05}
                    value={form.text_speed_lines?.tail_opacity ?? 0.85}
                    style={styles.range}
                    onChange={(e) =>
                      update("text_speed_lines", {
                        ...(form.text_speed_lines ?? {}),
                        tail_opacity: Number(e.target.value),
                      })
                    }
                  />
                </>
              ) : (
                <>
                  <div style={{ color: "#94a3b8", fontSize: 11, padding: "4px 0" }}>
                    글자 외곽선을 검출해 각 획 안쪽으로 파고드는 집중선을 그립니다.
                    깊이가 크면 글자가 깨져 보이니 아주 큰 글자에만 쓰세요.
                  </div>
                  <label style={styles.label}>
                    깊이 ({((form.text_speed_lines?.depth ?? 0.3) * 100).toFixed(0)}%)
                  </label>
                  <input
                    type="range" min={0.1} max={1.0} step={0.02}
                    value={form.text_speed_lines?.depth ?? 0.3}
                    style={styles.range}
                    onChange={(e) =>
                      update("text_speed_lines", {
                        ...(form.text_speed_lines ?? {}),
                        depth: Number(e.target.value),
                      })
                    }
                  />
                  <label style={styles.label}>
                    밀도 ({form.text_speed_lines?.density ?? 90})
                  </label>
                  <input
                    type="range" min={40} max={800} step={20}
                    value={form.text_speed_lines?.density ?? 90}
                    style={styles.range}
                    onChange={(e) =>
                      update("text_speed_lines", {
                        ...(form.text_speed_lines ?? {}),
                        density: Number(e.target.value),
                      })
                    }
                  />
                </>
              )}
              <label style={styles.label}>
                애니메이션 속도 ({(form.text_speed_lines?.anim_speed ?? 1.0).toFixed(2)}x)
              </label>
              <input
                type="range" min={0.2} max={3.0} step={0.1}
                value={form.text_speed_lines?.anim_speed ?? 1.0}
                style={styles.range}
                onChange={(e) =>
                  update("text_speed_lines", {
                    ...(form.text_speed_lines ?? {}),
                    anim_speed: Number(e.target.value),
                  })
                }
              />
              <label style={{ ...styles.label, display: "flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={form.text_speed_lines?.animate ?? true}
                  onChange={(e) =>
                    update("text_speed_lines", {
                      ...(form.text_speed_lines ?? {}),
                      animate: e.target.checked,
                    })
                  }
                />
                선 깜빡임 애니메이션
              </label>
              <label style={styles.label}>집중선 색 (비우면 글자색)</label>
              <input
                type="color"
                value={form.text_speed_lines?.color ?? form.color ?? "#ffffff"}
                style={styles.colorInput}
                onChange={(e) =>
                  update("text_speed_lines", {
                    ...(form.text_speed_lines ?? {}),
                    color: e.target.value,
                  })
                }
              />
            </>
          )}
          {(form.loop_animation ?? "none") !== "none" &&
            form.loop_animation !== "speedlines" && (
            <>
              <label style={styles.label}>
                진폭 ({((form.loop_amplitude ?? 0.02) * 100).toFixed(1)}%)
              </label>
              <input
                type="range" min={0.005} max={0.15} step={0.005}
                value={form.loop_amplitude ?? 0.02}
                style={styles.range}
                onChange={(e) => update("loop_amplitude", Number(e.target.value))}
              />
              {(form.loop_animation ?? "none") !== "shake" && (
                <>
                  <label style={styles.label}>
                    주기 ({(form.loop_period_sec ?? 0.8).toFixed(2)}초)
                  </label>
                  <input
                    type="range" min={0.2} max={3.0} step={0.05}
                    value={form.loop_period_sec ?? 0.8}
                    style={styles.range}
                    onChange={(e) => update("loop_period_sec", Number(e.target.value))}
                  />
                </>
              )}
              {(form.loop_animation === "pulse") && (form.gradient?.stops?.length || (form.glow_radius ?? 0) > 0) && (
                <div style={{ color: "#f59e0b", fontSize: 11, padding: "4px 0" }}>
                  ⚠ pulse 는 그라디언트/글로우 텍스트에는 적용되지 않습니다 (정적 PNG 로 폴백).
                </div>
              )}
            </>
          )}

          {/* #290 — SFX 섹션. projectId/audioAssets가 전달된 경우에만 표시. */}
          {projectId && audioAssets && (
            <SfxEditor
              projectId={projectId}
              audioAssets={audioAssets}
              sfxAssetId={form.sfx_asset_id ?? null}
              sfxVolume={form.sfx_volume ?? 0.7}
              onChange={(patch) => {
                if ("sfx_asset_id" in patch) update("sfx_asset_id", patch.sfx_asset_id ?? null);
                if ("sfx_volume" in patch && patch.sfx_volume !== undefined) update("sfx_volume", patch.sfx_volume);
              }}
              accent="#6366f1"
            />
          )}
        </div>

        {/* 프리셋 패널 */}
        {showPresetList && (
          <div style={styles.presetPanel}>
            {presets.length === 0 ? (
              <span style={styles.presetEmpty}>저장된 프리셋이 없습니다.</span>
            ) : (
              (() => {
                const builtinPresets = presets.filter(isBuiltinPreset);
                const userPresets = presets.filter((p) => !isBuiltinPreset(p));
                return (
                  <>
                    {builtinPresets.length > 0 && (
                      <>
                        <div style={styles.presetSectionHeader}>⭐ 빌트인</div>
                        {builtinPresets.map((p) => (
                          <div key={p.id} style={styles.presetRow}>
                            <button
                              style={styles.presetName}
                              onClick={() => handleLoadPreset(p)}
                            >
                              {p.name}
                              <span style={styles.builtinBadge}>빌트인</span>
                            </button>
                            <button
                              style={styles.presetDeleteBtnDisabled}
                              disabled
                              title="빌트인 프리셋은 삭제할 수 없습니다"
                            >
                              x
                            </button>
                          </div>
                        ))}
                      </>
                    )}
                    {userPresets.length > 0 && (
                      <>
                        <div style={styles.presetSectionHeader}>내 프리셋</div>
                        {userPresets.map((p) => (
                          <div key={p.id} style={styles.presetRow}>
                            <button
                              style={styles.presetName}
                              onClick={() => handleLoadPreset(p)}
                            >
                              {p.name}
                            </button>
                            <button
                              style={styles.presetDeleteBtn}
                              onClick={() => handleDeletePreset(p.id)}
                            >
                              x
                            </button>
                          </div>
                        ))}
                      </>
                    )}
                  </>
                );
              })()
            )}
          </div>
        )}

        <div style={styles.footer}>
          <div style={{ display: "flex", gap: 6, flex: 1, alignItems: "center", flexWrap: "wrap" }}>
            <button style={styles.presetBtn} onClick={handleSavePreset}>
              프리셋으로 저장
            </button>
            {/* #290 — 커스텀 오버레이 등록 시 "SFX 포함" 토글. */}
            <label
              title="체크 시 프리셋에 SFX 자산 ID·볼륨까지 저장됩니다."
              style={{
                display: "flex",
                alignItems: "center",
                gap: 4,
                fontSize: 11,
                color: "#888",
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={presetIncludeSfx}
                onChange={(e) => setPresetIncludeSfx(e.target.checked)}
              />
              SFX 포함
            </label>
            <button
              style={{
                ...styles.presetBtn,
                ...(showPresetList ? styles.presetBtnActive : {}),
              }}
              onClick={() => setShowPresetList((v) => !v)}
            >
              프리셋 불러오기
            </button>
          </div>
          <button style={styles.cancelBtn} onClick={onClose}>취소</button>
          <button style={styles.saveBtn} onClick={handleSave}>저장</button>
        </div>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  backdrop: {
    position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
  },
  modal: {
    background: "#1a1a1a", borderRadius: 10, border: "1px solid #333",
    width: 480, maxHeight: "85vh", display: "flex", flexDirection: "column",
  },
  header: {
    padding: "14px 16px", borderBottom: "1px solid #2a2a2a",
    display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0,
  },
  title: { color: "#fff", fontWeight: 700, fontSize: 15 },
  closeBtn: {
    background: "transparent", border: "none", color: "#666",
    fontSize: 18, cursor: "pointer", padding: "0 4px",
  },
  body: { padding: "14px 16px", overflowY: "auto", flex: 1 },
  footer: {
    padding: "12px 16px", borderTop: "1px solid #2a2a2a",
    display: "flex", justifyContent: "flex-end", gap: 8, flexShrink: 0,
  },
  label: { display: "block", color: "#aaa", fontSize: 12, marginBottom: 4, marginTop: 10 },
  textarea: {
    width: "100%", background: "#111", border: "1px solid #333",
    borderRadius: 6, color: "#fff", fontSize: 13, padding: "8px 10px",
    resize: "vertical", fontFamily: "inherit", boxSizing: "border-box",
  },
  range: { width: "100%", marginBottom: 4 },
  row: { display: "flex", alignItems: "center", gap: 6, marginBottom: 6, flexWrap: "wrap" },
  toggleBtn: {
    padding: "3px 10px", background: "#111", border: "1px solid #333",
    borderRadius: 5, color: "#aaa", cursor: "pointer", fontSize: 12,
  },
  toggleActive: { background: "#2a2a6a", borderColor: "#6366f1", color: "#6366f1" },
  colorInput: { width: 32, height: 26, border: "none", borderRadius: 4, cursor: "pointer", padding: 0 },
  hexInput: {
    background: "#111", border: "1px solid #333", borderRadius: 4,
    color: "#ccc", fontSize: 12, padding: "3px 6px", width: 90,
  },
  removeBtn: {
    background: "#2a0a0a", border: "1px solid #500",
    color: "#f87171", borderRadius: 4, cursor: "pointer", fontSize: 14, padding: "1px 7px",
  },
  select: {
    width: "100%", background: "#111", border: "1px solid #333",
    borderRadius: 6, color: "#ccc", fontSize: 13, padding: "6px 8px",
    marginBottom: 4,
  },
  addBtn: {
    background: "#0a2a0a", border: "1px solid #166534",
    color: "#4ade80", borderRadius: 5, cursor: "pointer", fontSize: 12, padding: "4px 10px",
  },
  cancelBtn: {
    padding: "7px 18px", background: "transparent", color: "#666",
    border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  saveBtn: {
    padding: "7px 18px", background: "#6366f1", color: "#fff",
    border: "none", borderRadius: 7, cursor: "pointer", fontSize: 13,
  },
  presetBtn: {
    padding: "6px 12px", background: "#111", color: "#aaa",
    border: "1px solid #333", borderRadius: 7, cursor: "pointer", fontSize: 12,
  },
  presetBtnActive: {
    borderColor: "#6366f1", color: "#6366f1", background: "#1a1a3a",
  },
  presetPanel: {
    padding: "8px 16px", borderTop: "1px solid #2a2a2a",
    maxHeight: 160, overflowY: "auto" as const, background: "#111",
  },
  presetEmpty: { color: "#555", fontSize: 12 },
  presetRow: {
    display: "flex", alignItems: "center", gap: 6, marginBottom: 4,
  },
  presetName: {
    flex: 1, textAlign: "left" as const, background: "#1a1a1a",
    border: "1px solid #333", borderRadius: 5, color: "#ccc",
    fontSize: 12, padding: "4px 8px", cursor: "pointer",
  },
  presetDeleteBtn: {
    background: "#2a0a0a", border: "1px solid #500",
    color: "#f87171", borderRadius: 4, cursor: "pointer", fontSize: 12,
    padding: "2px 7px",
  },
  presetDeleteBtnDisabled: {
    background: "#1a1a1a", border: "1px solid #2a2a2a",
    color: "#444", borderRadius: 4, cursor: "not-allowed", fontSize: 12,
    padding: "2px 7px",
  },
  presetSectionHeader: {
    color: "#888", fontSize: 11, fontWeight: 700,
    marginTop: 6, marginBottom: 4, paddingBottom: 3,
    borderBottom: "1px solid #2a2a2a",
    letterSpacing: "0.5px", textTransform: "uppercase" as const,
  },
  builtinBadge: {
    display: "inline-block", marginLeft: 8, padding: "1px 6px",
    background: "#1a1a3a", border: "1px solid #6366f1", borderRadius: 4,
    color: "#a5b4fc", fontSize: 10, fontWeight: 600,
  },
};
