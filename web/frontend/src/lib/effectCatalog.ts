/**
 * 이펙트 카탈로그 fetcher + UI 상수 (#306).
 *
 * 백엔드 `GET /api/effect_catalog` 응답을 받아 `EffectAddPanel` 등 UI 컴포넌트에서 사용한다.
 * 매니페스트 자체는 `src/render_spec/effects/` 의 Python 모듈에 1차 진실로 살아 있고,
 * 본 모듈은 그것을 그대로 받아 표시한다 — 신규 효과 추가 시 프론트 하드코딩이 *0*개가 되도록 함.
 *
 * 1단계 (#306) 범위:
 * - fetcher + 카테고리별 색상 상수
 * - 카탈로그 로딩 실패 시 폴백(기존 하드코딩 목록) — 백엔드 비활성 환경에서도 UI가 동작하도록
 *
 * 폴백 목록의 의도:
 * 김감독 환경에서는 백엔드가 항상 켜져 있지만, 개발 중 백엔드 미기동/에러 상황에서 EffectAddPanel
 * 이 비어버리는 회귀를 막기 위한 안전망. 폴백 목록은 매니페스트와 동일한 20종을 유지한다.
 */
import { useEffect, useState } from "react";
import type { EffectDefinition } from "../types/generated";

export type EffectCategory =
  | "text"
  | "shape"
  | "graphic"
  | "filter"
  | "motion"
  | "mosaic"
  | "image";

export interface CategoryStyle {
  title: string;
  border: string;
  text: string;
}

// 기존 EffectAddPanel 색상 토큰 (회귀 방지를 위해 동일하게 유지)
export const CATEGORY_STYLE: Record<EffectCategory, CategoryStyle> = {
  text: { title: "텍스트 오버레이", border: "#4338ca", text: "#818cf8" },
  shape: { title: "도형", border: "#166534", text: "#4ade80" },
  graphic: { title: "그래픽 오버레이", border: "#581c87", text: "#c084fc" },
  filter: { title: "필터", border: "#b45309", text: "#fbbf24" },
  motion: { title: "모션", border: "#0e7490", text: "#67e8f9" },
  mosaic: { title: "모자이크", border: "#7c2d12", text: "#fb923c" },
  // 이미지 오버레이 — footage 스튜디오의 이미지 자산을 씬 위에 합성.
  image: { title: "이미지", border: "#155e75", text: "#22d3ee" },
};

// EffectAddPanel UI 섹션 정렬 순서 — 기존 패널 순서를 그대로 유지.
export const CATEGORY_ORDER: EffectCategory[] = [
  "shape",
  "mosaic",
  "image",
  "graphic",
  "filter",
  "motion",
  "text",
];

// 백엔드 비활성 환경 대비 폴백. 매니페스트와 1:1.
// (display_label/tooltip 만으로도 패널이 동작하도록 최소 필드만 채움)
const FALLBACK_CATALOG: EffectDefinition[] = [
  // text
  {
    name: "text",
    display_label: "텍스트",
    category: "text",
    params: {},
    preview_supported: true,
    tooltip: "화면에 자막/문구 추가 — 추가 후 칩 클릭으로 편집",
    notes: "추가 후 씬 카드의 칩을 클릭하면 텍스트·스타일·프리셋 편집기가 열립니다.",
    backend_layer_type: "text",
  },
  // shape
  { name: "rect", display_label: "사각형", category: "shape", params: {}, preview_supported: true, tooltip: "사각형 도형 오버레이", backend_layer_type: "shape" },
  { name: "ellipse", display_label: "타원", category: "shape", params: {}, preview_supported: true, tooltip: "타원 도형 오버레이", backend_layer_type: "shape" },
  // graphic
  { name: "spotlight", display_label: "Spotlight", category: "graphic", params: {}, preview_supported: true, tooltip: "집중조명 — 중앙 원형만 밝게", backend_layer_type: "graphic" },
  { name: "alert", display_label: "Alert", category: "graphic", params: {}, preview_supported: true, tooltip: "알림 팝업", backend_layer_type: "graphic" },
  { name: "target", display_label: "Target", category: "graphic", params: {}, preview_supported: true, tooltip: "타겟 마커 (십자선/화살표)", backend_layer_type: "graphic" },
  { name: "reaction", display_label: "Reaction", category: "graphic", params: {}, preview_supported: true, tooltip: "리액션 아이콘 (번개/땀/물음표/느낌표)", backend_layer_type: "graphic" },
  { name: "smell", display_label: "Smell", category: "graphic", params: {}, preview_supported: true, tooltip: "냄새/연기 효과", backend_layer_type: "graphic" },
  { name: "emoji", display_label: "Emoji", category: "graphic", params: {}, preview_supported: true, tooltip: "이모지 아이콘 (❓ 🔍 🚫 💩)", backend_layer_type: "graphic" },
  { name: "dash", display_label: "Dash", category: "graphic", params: {}, preview_supported: true, tooltip: "질주 표시 (=3=3=3) — 마크가 흘러가는 애니메이션", backend_layer_type: "graphic" },
  // filter (Notice: prefix "+" 는 패널에서 붙임)
  { name: "flashback", display_label: "Flashback", category: "filter", params: {}, preview_supported: true, tooltip: "회상 분위기 — 흐릿하고 따뜻한 톤" },
  { name: "horror", display_label: "Horror", category: "filter", params: {}, preview_supported: true, tooltip: "공포 분위기 — 채도 저하/그린 틴트" },
  { name: "vivid", display_label: "Vivid", category: "filter", params: {}, preview_supported: true, tooltip: "선명한 색감 강화" },
  { name: "nightvision", display_label: "NightVision", category: "filter", params: {}, preview_supported: true, tooltip: "야간 투시경 분위기 — 그린 모노톤" },
  { name: "pov", display_label: "POV", category: "filter", params: {}, preview_supported: true, tooltip: "1인칭 시점 분위기" },
  { name: "warm", display_label: "Warm", category: "filter", params: {}, preview_supported: true, tooltip: "따뜻한 분위기 — 노란/주황 톤 + 부드러운 비네팅 (황혼/카페)" },
  { name: "cool", display_label: "Cool", category: "filter", params: {}, preview_supported: true, tooltip: "시원한 분위기 — 청록/파란 톤 + 살짝 저채도 (얼음/에어컨/여름)" },
  // motion
  { name: "shake", display_label: "Shake", category: "motion", params: {}, preview_supported: false, tooltip: "충격 셰이크" },
  { name: "clip_zoom", display_label: "Zoom", category: "motion", params: {}, preview_supported: false, tooltip: "영역 줌" },
  { name: "stutter", display_label: "Stutter", category: "motion", params: { intensity: { kind: "float", label: "끊김 강도", default: 0.3, min: 0.0, max: 1.0, description: "끊김 강도 (0.0=거의 없음 ~ 1.0=글리치풍). 기본 0.3 은 초당 약 8~12프레임 수준의 은은한 영화적 느낌." } }, preview_supported: false, tooltip: "이동 촬영 영상에 끊김/점프 모션 (정지 화면에는 효과 미미)" },
  { name: "slowmo", display_label: "Slow", category: "motion", params: {}, preview_supported: false, tooltip: "슬로우 모션" },
  // mosaic
  { name: "rect", display_label: "사각형", category: "mosaic", params: {}, preview_supported: true, tooltip: "사각형 모자이크 영역", backend_layer_type: "mosaic" },
  { name: "ellipse", display_label: "타원", category: "mosaic", params: {}, preview_supported: true, tooltip: "타원 모자이크 — 1차 구현에서는 사각형으로 폴백됩니다 (#264 후속)", backend_layer_type: "mosaic" },
  // image — footage 스튜디오의 이미지 자산을 씬 위에 합성 + 선택적 캡션.
  { name: "image", display_label: "이미지", category: "image", params: {}, preview_supported: true, tooltip: "footage 스튜디오의 이미지 자산을 씬 위에 합성 (캡션 선택)" },
];

/**
 * /api/effect_catalog 응답을 받아 카탈로그를 노출하는 hook.
 *
 * - 최초 마운트 시 1회 fetch.
 * - 백엔드 응답이 실패하면 `FALLBACK_CATALOG` 를 사용.
 * - 응답을 카테고리별로 그룹화한 결과(`byCategory`)도 함께 노출.
 */
export function useEffectCatalog(): {
  effects: EffectDefinition[];
  byCategory: Record<EffectCategory, EffectDefinition[]>;
  loaded: boolean;
} {
  const [effects, setEffects] = useState<EffectDefinition[]>(FALLBACK_CATALOG);
  const [loaded, setLoaded] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/effect_catalog");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        const list = Array.isArray(body?.effects) ? (body.effects as EffectDefinition[]) : [];
        if (!cancelled && list.length > 0) {
          setEffects(list);
        }
      } catch (e) {
        // 폴백 유지 — 콘솔 경고만 남긴다.
        // eslint-disable-next-line no-console
        console.warn(
          "[effectCatalog] 백엔드 카탈로그 fetch 실패 — 폴백 사용.",
          e,
        );
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const byCategory = (CATEGORY_ORDER.reduce((acc, c) => {
    acc[c] = effects.filter((e) => e.category === c);
    return acc;
  }, {} as Record<EffectCategory, EffectDefinition[]>));

  return { effects, byCategory, loaded };
}
