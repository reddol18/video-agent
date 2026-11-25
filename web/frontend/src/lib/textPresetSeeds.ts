/**
 * 텍스트 오버레이 빌트인 시드 프리셋 (Type A/B/C)
 *
 * - 신규 프로젝트에서도 즉시 톤이 다른 텍스트 스타일을 쓸 수 있게 한다.
 * - 빌트인 프리셋은 사용자 정의 프리셋과 동일한 `TextOverlayPreset` 구조를 따른다.
 * - ID prefix `builtin-` 로 분류한다. 씬 태그 시스템(#208)이 이 ID를 그대로 참조하므로
 *   **절대 이름/ID를 변경하지 말 것**.
 * - IndexedDB에 같은 ID가 이미 존재하면 덮어쓰지 않는다 (유저 수정 보존).
 *
 * 폰트값은 TextOverlayEditor의 FONT_OPTIONS에 등록된 키와 일치한다:
 *   NotoSansKR, BMDOHYEON, PF_STARDUST3, DungGeunMo
 */
import type { TextOverlayPreset } from "../types";

export const BUILTIN_PRESET_IDS = {
  narrative: "builtin-narrative",
  witty: "builtin-witty",
  horror: "builtin-horror",
} as const;

const SEED_CREATED_AT = "2026-05-12T00:00:00Z";

export const BUILTIN_TEXT_PRESETS: TextOverlayPreset[] = [
  // ── Type A · 일상/내러티브 ────────────────────────────────────────────────
  {
    id: BUILTIN_PRESET_IDS.narrative,
    name: "내러티브 (일상/담담)",
    created_at: SEED_CREATED_AT,
    style: {
      x: 0.5,
      y: 0.1,
      font_size: 48,
      font_family: "NotoSansKR",
      color: "#FFFFFF",
      gradient: {
        angle: 90,
        stops: [
          { color: "#FFFFFF", position: 0.0 },
          { color: "#E8E8E8", position: 1.0 },
        ],
      },
      bold: false,
      italic: false,
      outline_color: "#000000",
      outline_width: 3,
      glow_color: "#000000",
      glow_radius: 0,
      opacity: 1.0,
      align: "center",
    },
  },
  // ── Type B · 위트/당황/강조 ──────────────────────────────────────────────
  {
    id: BUILTIN_PRESET_IDS.witty,
    name: "위트 (당황/강조)",
    created_at: SEED_CREATED_AT,
    style: {
      x: 0.5,
      y: 0.1,
      font_size: 56,
      font_family: "BMDOHYEON",
      color: "#FFE94B",
      gradient: {
        angle: 90,
        stops: [
          { color: "#FFE94B", position: 0.0 },
          { color: "#FFB627", position: 1.0 },
        ],
      },
      bold: true,
      italic: false,
      outline_color: "#3A1F00",
      outline_width: 5,
      glow_color: "#000000",
      glow_radius: 0,
      opacity: 1.0,
      align: "center",
    },
  },
  // ── Type C · 공포/경고/분노 ──────────────────────────────────────────────
  {
    id: BUILTIN_PRESET_IDS.horror,
    name: "공포 (경고/분노)",
    created_at: SEED_CREATED_AT,
    style: {
      x: 0.5,
      y: 0.1,
      font_size: 60,
      font_family: "PF_STARDUST3",
      color: "#E53935",
      gradient: {
        angle: 90,
        stops: [
          { color: "#E53935", position: 0.0 },
          { color: "#5A0000", position: 1.0 },
        ],
      },
      bold: true,
      italic: false,
      outline_color: "#000000",
      outline_width: 2,
      glow_color: "#000000",
      glow_radius: 12,
      opacity: 1.0,
      align: "center",
    },
  },
];

/**
 * 빌트인 프리셋 여부 판별.
 * ID prefix가 `builtin-`이면 빌트인 시드로 간주한다.
 */
export function isBuiltinPreset(preset: { id: string }): boolean {
  return preset.id.startsWith("builtin-");
}
