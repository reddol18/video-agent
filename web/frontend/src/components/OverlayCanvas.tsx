/**
 * 오버레이 합성 캔버스 (#310).
 *
 * `<video>` 위에 RenderSpec 의 text / shape / graphic 레이어를 절대 위치 `<div>` 로 표시한다.
 * 백엔드 ffmpeg 호출 없이 브라우저가 직접 합성하므로, 오버레이 편집 중 미리보기 비용을
 * 대폭 절감한다.
 *
 * 단일 진실(SSOT) 원칙
 * ───────────────────
 *  - 좌표/크기/색은 **RenderSpec 의 레이어 값을 그대로** CSS 로 환산한다.
 *  - 캔버스 절대 픽셀(예: position_x, font_size_px) 을 캔버스 너비/높이로 나눠 비율로 만들고
 *    부모 컨테이너의 % 로 그대로 매핑한다. 컨테이너 픽셀 크기가 어떻든 백엔드 결과와
 *    상대 위치가 일치한다.
 *  - font-family 도 RenderSpec 의 값을 그대로 사용. 백엔드 PIL 이 같은 font 명을 탐색해
 *    같은 폰트 파일을 사용하도록 폰트 일관성을 유지한다(미세한 anti-aliasing 차이는 최종
 *    렌더가 항상 백엔드라 사용자에게는 보이지 않는다).
 *
 * 위치 의미
 * ────────
 *  - TextLayer.position_x/y : 텍스트 박스의 *중심* 좌표(`models.py` 주석 참조).
 *      → CSS `transform: translate(-50%, -50%)` 로 중심 정렬.
 *  - ShapeLayer.x/y         : 좌상단 좌표(`models.py` 참조). 그대로 left/top 으로 사용.
 *  - GraphicLayer.center_x/y: 중심 좌표. 위와 같이 translate(-50%,-50%).
 */
import { useEffect, useRef, type CSSProperties } from "react";
import { resolveSfxStreamUrl } from "../api/client";
import type {
  RenderSpec,
  RenderCanvas,
  TextLayer,
  ShapeLayer,
  GraphicLayer,
  VideoLayer,
} from "../types";
import { applySimpleRenderTransform, type SimpleRenderTransform } from "../lib/clientSceneRenderer";
import { speedAtSource, type SpeedSpec } from "../lib/speedRamp";

/** 브라우저 <video>.playbackRate 상한(Chrome). */
const MAX_BROWSER_PLAYBACK_RATE = 16;

/**
 * 가속(ramp) 모드 미리보기 — 재생 중 rAF 로 현재 원본 위치(currentTime - start) 의 순간 속도 v(x) 를
 * 계산해 playbackRate 를 갱신한다. constant 모드면 아무것도 하지 않는다(기존 onLoadedMetadata 경로).
 */
function useRampPlaybackRate(
  videoRef: React.RefObject<HTMLVideoElement | null>,
  spec: RenderSpec,
) {
  const vl = (spec.layers ?? []).find((l) => (l as { type?: string }).type === "video") as
    | (VideoLayer & { speed_mode?: string; speed_ramp_hold?: number; speed_ramp_accel?: number; speed_ramp_base?: number })
    | undefined;
  const rampMode: "ramp" | "ramp_down" = vl?.speed_mode === "ramp_down" ? "ramp_down" : "ramp";
  const isRamp = (vl?.speed_mode === "ramp" || vl?.speed_mode === "ramp_down") && (vl.playback_rate ?? 1) !== 1;
  const rate = vl?.playback_rate ?? 1;
  const hold = vl?.speed_ramp_hold ?? 0.2;
  const accel = vl?.speed_ramp_accel ?? 0.3;
  const base = vl?.speed_ramp_base ?? 1;
  const start = vl?.start_sec ?? 0;
  const end = vl?.end_sec ?? null;
  useEffect(() => {
    if (!isRamp) return;
    const rampSpec: SpeedSpec = { mode: rampMode, speed: rate, hold, accel, base };
    let raf = 0;
    const tick = () => {
      const v = videoRef.current;
      if (v) {
        const L = end != null && end > start ? end - start : (isFinite(v.duration) ? v.duration : 0);
        const x = Math.max(0, v.currentTime - start);
        const r = Math.min(MAX_BROWSER_PLAYBACK_RATE, Math.max(0.0625, speedAtSource(x, L, rampSpec)));
        if (Math.abs(v.playbackRate - r) > 0.01) {
          try { v.playbackRate = r; } catch { /* ignore */ }
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [videoRef, isRamp, rampMode, rate, hold, accel, base, start, end]);
}

// ── 폰트 매핑 ────────────────────────────────────────────────────────────
// 백엔드 PIL 이 사용하는 폰트 파일(`fonts/NotoSansKR-Regular.otf`, `BMDOHYEON_TTF.TTF`,
// `DungGeunMo.ttf`, `PF_STARDUST3.0.ttf`) 과 브라우저 CSS 의 font-family 를 같은 키로 매핑.
// fallback 으로 한글이 깨지지 않도록 시스템 기본 한글 폰트를 끝에 둔다.
function cssFontFamily(name: string | undefined): string {
  const fallback = '"Noto Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif';
  if (!name) return fallback;
  const key = name.toLowerCase().replace(/\s+/g, "");
  if (key.includes("notosanskr")) return `"Noto Sans KR", ${fallback}`;
  if (key.includes("bmdohyeon")) return `"BM DoHyeon", "Do Hyeon", ${fallback}`;
  if (key.includes("dunggeunmo")) return `"DungGeunMo", ${fallback}`;
  if (key.includes("dongle")) return `"Dongle", ${fallback}`;
  if (key.includes("stardust")) return `"PF Stardust", ${fallback}`;
  // 사용자 폰트명을 그대로 두고 시스템 fallback 추가
  return `"${name}", ${fallback}`;
}

// hex → "r,g,b" (rgba 합성용)
function hexToRgbTriplet(hex: string | undefined, fallback = "255,255,255"): string {
  if (!hex) return fallback;
  const s = hex.replace(/^#/, "");
  if (s.length === 3) {
    const r = parseInt(s[0] + s[0], 16);
    const g = parseInt(s[1] + s[1], 16);
    const b = parseInt(s[2] + s[2], 16);
    return `${r},${g},${b}`;
  }
  if (s.length === 6) {
    const r = parseInt(s.slice(0, 2), 16);
    const g = parseInt(s.slice(2, 4), 16);
    const b = parseInt(s.slice(4, 6), 16);
    if ([r, g, b].every((v) => !Number.isNaN(v))) {
      return `${r},${g},${b}`;
    }
  }
  return fallback;
}

// 폭/높이 비율(0~1) → % 문자열
function pct(value: number): string {
  return `${value * 100}%`;
}

// ── 개별 레이어 컴포넌트 ────────────────────────────────────────────────

// 모든 loop_animation 의 CSS keyframes 를 페이지 한 곳에 주입한다. 같은 이름이라 중복 주입돼도 무해.
// 각 효과는 transform/font-size 만 변조 — 기존 translate(-50%,-50%) 중심 정렬을 보존하기 위해
// 모든 키프레임은 베이스 transform 뒤에 추가 transform 을 합성한다.
const LOOP_ANIM_KEYFRAMES = `
@keyframes vag-text-bounce { 0%{transform:translate(-50%,-50%) translateY(0)} 50%{transform:translate(-50%,-50%) translateY(var(--vag-bounce-px,-20px))} 100%{transform:translate(-50%,-50%) translateY(0)} }
@keyframes vag-text-shake  { 0%,100%{transform:translate(-50%,-50%) translateX(0)} 25%{transform:translate(-50%,-50%) translateX(var(--vag-shake-px,3px))} 75%{transform:translate(-50%,-50%) translateX(calc(var(--vag-shake-px,3px) * -1))} }
@keyframes vag-text-pulse  { 0%,100%{transform:translate(-50%,-50%) scale(1)} 50%{transform:translate(-50%,-50%) scale(var(--vag-pulse-scale,1.1))} }
@keyframes vag-text-wave   { 0%,100%{transform:translate(-50%,-50%) translateX(0)} 50%{transform:translate(-50%,-50%) translateX(var(--vag-wave-px,15px))} }
`;

function TextOverlayItem({ layer, canvas }: { layer: TextLayer; canvas: RenderCanvas }) {
  // position_x/y 는 *중심*. translate(-50%,-50%) 로 중앙 정렬.
  const leftRatio = layer.position_x / canvas.width;
  const topRatio = layer.position_y / canvas.height;

  // font-size 는 캔버스 픽셀 단위. 부모 컨테이너의 폭에 대한 비율로 환산해 CSS px 대신 vw-like
  // 비율로 두는 게 깔끔하지만, 컨테이너가 정확히 캔버스 종횡비를 유지하므로 *높이 비율* 로
  // 환산해 다른 길이 단위와 일관성을 맞춘다. (font-size 는 일반적으로 캔버스 높이 기준.)
  const fontSizeRatio = (layer.font_size_px ?? 40) / canvas.height;

  const outlineWidth = layer.outline_width_px ?? 0;
  const outlineColor = layer.outline_color ?? "#000000";
  // CSS text-stroke 는 일부 브라우저 호환성이 떨어지므로 text-shadow 4 방향으로 외곽선 대용.
  const outlineShadow = outlineWidth > 0
    ? [
        `${outlineWidth}px 0 0 ${outlineColor}`,
        `-${outlineWidth}px 0 0 ${outlineColor}`,
        `0 ${outlineWidth}px 0 ${outlineColor}`,
        `0 -${outlineWidth}px 0 ${outlineColor}`,
        `${outlineWidth}px ${outlineWidth}px 0 ${outlineColor}`,
        `-${outlineWidth}px -${outlineWidth}px 0 ${outlineColor}`,
        `${outlineWidth}px -${outlineWidth}px 0 ${outlineColor}`,
        `-${outlineWidth}px ${outlineWidth}px 0 ${outlineColor}`,
      ].join(", ")
    : "none";

  // ── 표시 중 반복 애니메이션 ─────────────────────────────────────────────
  // 백엔드 ffmpeg drawtext 와 동일한 의미의 amplitude/period 를 CSS 변수로 환산해 keyframe 에 주입.
  // wall-clock 기반 animation 이므로 video.currentTime 과 정확 동기화되진 않지만, 편집 중 시각 확인
  // 용도로는 충분. 백엔드 렌더링 결과가 최종 진실의 원천이다.
  const loopAnim = layer.loop_animation ?? "none";
  const loopAmp = layer.loop_amplitude ?? 0.02;
  const loopPeriod = layer.loop_period_sec ?? 0.8;
  const animStyle: CSSProperties = {};
  const cssVars: Record<string, string> = {};
  if (loopAnim === "bounce") {
    animStyle.animation = `vag-text-bounce ${loopPeriod}s ease-in-out infinite`;
    // y 진폭: 캔버스 높이 비율 → 컨테이너 cqh 단위로 환산. 음수(위로).
    cssVars["--vag-bounce-px"] = `${-loopAmp * 100}cqh`;
  } else if (loopAnim === "shake") {
    // 빠른 좌우 진동 — 백엔드는 12Hz. 1/12 ≈ 0.083s.
    animStyle.animation = `vag-text-shake 0.083s linear infinite`;
    cssVars["--vag-shake-px"] = `calc(${fontSizeRatio} * ${loopAmp} * 100cqh)`;
  } else if (loopAnim === "pulse") {
    animStyle.animation = `vag-text-pulse ${loopPeriod}s ease-in-out infinite`;
    cssVars["--vag-pulse-scale"] = `${1 + loopAmp}`;
  } else if (loopAnim === "wave") {
    animStyle.animation = `vag-text-wave ${loopPeriod}s ease-in-out infinite`;
    cssVars["--vag-wave-px"] = `${loopAmp * 100}cqw`;
  }

  const align = layer.align ?? "center";
  const style: CSSProperties = {
    position: "absolute",
    left: pct(leftRatio),
    top: pct(topRatio),
    transform: "translate(-50%, -50%)",
    color: layer.color ?? "#FFFFFF",
    opacity: layer.opacity ?? 1.0,
    fontSize: `calc(${fontSizeRatio} * 100cqh)`,
    fontFamily: cssFontFamily(layer.font_family),
    fontWeight: layer.bold ? 700 : 400,
    fontStyle: layer.italic ? "italic" : "normal",
    textAlign: align,
    textShadow: outlineShadow,
    whiteSpace: "pre",
    lineHeight: 1.2,
    pointerEvents: "none",
    userSelect: "none",
    ...animStyle,
    ...(cssVars as CSSProperties),
  };
  return (
    <>
      {loopAnim !== "none" && <style>{LOOP_ANIM_KEYFRAMES}</style>}
      <div style={style}>{layer.text ?? ""}</div>
    </>
  );
}

function ShapeOverlayItem({ layer, canvas }: { layer: ShapeLayer; canvas: RenderCanvas }) {
  // ShapeLayer.x/y 는 좌상단 픽셀.
  const leftRatio = layer.x / canvas.width;
  const topRatio = layer.y / canvas.height;
  const widthRatio = layer.width / canvas.width;
  const heightRatio = layer.height / canvas.height;
  const opacity = layer.opacity ?? 1.0;
  const colorRgb = hexToRgbTriplet(layer.color, "255,255,255");
  const background = `rgba(${colorRgb}, ${opacity})`;
  const style: CSSProperties = {
    position: "absolute",
    left: pct(leftRatio),
    top: pct(topRatio),
    width: pct(widthRatio),
    height: pct(heightRatio),
    background,
    borderRadius: layer.shape === "ellipse" ? "50%" : 0,
    pointerEvents: "none",
  };
  return <div style={style} />;
}

function GraphicOverlayItem({ layer, canvas }: { layer: GraphicLayer; canvas: RenderCanvas }) {
  // GraphicLayer.center_x/y 는 중심.
  const leftRatio = layer.center_x / canvas.width;
  const topRatio = layer.center_y / canvas.height;
  const opacity = layer.opacity ?? 1.0;
  // 기본 크기: 캔버스 짧은변의 일정 비율(spotlight 가 가장 큼).
  const params = (layer.params ?? {}) as Record<string, unknown>;
  const sizeParam = typeof params.size === "number" ? (params.size as number) : null;
  const radiusParam = typeof params.radius === "number" ? (params.radius as number) : null;

  // 캔버스 높이 비율로 크기 환산. params 에 size/radius 가 있으면 그 절대 픽셀을 그대로 사용.
  // #72 — Twemoji 자산 도입 + 글로우 후 시각 인지 크기가 작아져 기본을 한 단계 키움.
  const defaultSizes: Record<string, number> = {
    spotlight: 0.55, // 캔버스 높이의 55%
    target: 0.28,
    alert: 0.32,
    reaction: 0.3,
    smell: 0.32,
    emoji: 0.22,
  };
  const sizeRatio = sizeParam !== null
    ? sizeParam / canvas.height
    : radiusParam !== null
    ? (radiusParam * 2) / canvas.height
    : defaultSizes[layer.graphic_type] ?? 0.22;

  const commonContainer: CSSProperties = {
    position: "absolute",
    left: pct(leftRatio),
    top: pct(topRatio),
    transform: "translate(-50%, -50%)",
    pointerEvents: "none",
    opacity,
  };

  if (layer.graphic_type === "spotlight") {
    // 어두운 외곽 + 가운데 빛(원형 마스크). 단순 라디얼 그라디언트로 근사.
    const dim = typeof params.dim === "number" ? (params.dim as number) : 0.6;
    const size = `${sizeRatio * 100}cqh`;
    return (
      <div
        style={{
          ...commonContainer,
          width: size,
          height: size,
          borderRadius: "50%",
          background: `radial-gradient(circle, rgba(255,255,255,0) 40%, rgba(0,0,0,${dim}) 70%, rgba(0,0,0,${dim}) 100%)`,
          boxShadow: `0 0 0 9999px rgba(0,0,0,${dim})`,
        }}
      />
    );
  }

  if (layer.graphic_type === "alert") {
    // #72 — Twemoji 자산 + 글로우. 텍스트는 미리보기에선 생략, 정확한 합성은 백엔드.
    const style = typeof params.style === "string" ? (params.style as string) : "warning";
    return renderGraphicAssetPlaceholder({
      commonContainer,
      sizeRatio,
      category: "alert",
      name: style,
    });
  }

  if (layer.graphic_type === "emoji") {
    const iconType = typeof params.icon_type === "string" ? (params.icon_type as string) : "question";
    const EMOJI: Record<string, string> = {
      question: "❓",
      lens: "🔍",
      prohibit: "🚫",
      poo: "💩",
    };
    const emoji = EMOJI[iconType] ?? "❓";
    const size = `${sizeRatio * 100}cqh`;
    return (
      <div
        style={{
          ...commonContainer,
          width: size,
          height: size,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: `${sizeRatio * 90}cqh`,
          lineHeight: 1,
        }}
      >
        {emoji}
      </div>
    );
  }

  // #72 — target / reaction / smell 미리보기. 백엔드 합성과 동일한 Twemoji PNG 를
  // 그대로 표시 (이모지 placeholder 대신). 글로우는 CSS drop-shadow 로 근사.
  let assetCategory: string | null = null;
  let assetName: string | null = null;
  if (layer.graphic_type === "reaction") {
    assetCategory = "reaction";
    const t = typeof params.icon_type === "string" ? (params.icon_type as string) : "thunder";
    assetName = ["thunder", "sweat", "question", "exclamation"].includes(t) ? t : "thunder";
  } else if (layer.graphic_type === "target") {
    assetCategory = "target";
    const t = typeof params.marker_type === "string" ? (params.marker_type as string) : "crosshair";
    assetName = ["crosshair", "arrow"].includes(t) ? t : "crosshair";
  } else if (layer.graphic_type === "smell") {
    assetCategory = "smell";
    assetName = "cloud";
  }
  if (assetCategory && assetName) {
    return renderGraphicAssetPlaceholder({
      commonContainer,
      sizeRatio,
      category: assetCategory,
      name: assetName,
      // smell 은 색조가 다양 — 단일 cloud.png 에 CSS tint 입힘.
      tintColor: layer.graphic_type === "smell"
        ? smellTintColor(typeof params.color === "string" ? (params.color as string) : "green")
        : null,
      // target 은 rotation 도 지원
      rotationDeg: layer.graphic_type === "target" && typeof params.rotation === "number"
        ? (params.rotation as number)
        : 0,
    });
  }

  // 알 수 없는 타입 — 점 하나로 위치만 표시
  const size = `${sizeRatio * 100}cqh`;
  return (
    <div
      style={{
        ...commonContainer,
        width: size,
        height: size,
        borderRadius: "50%",
        background: "rgba(255,255,255,0.4)",
        boxShadow: "0 0 8px rgba(0,0,0,0.5)",
      }}
    />
  );
}

/** smell.color (green/brown/white/...) → CSS hue-rotate 가 어려우니 단순 색상 매핑. */
function smellTintColor(color: string): string {
  const m: Record<string, string> = {
    green: "rgba(90, 138, 58, 0.85)",
    brown: "rgba(122, 90, 42, 0.85)",
    white: "rgba(232, 232, 232, 0.85)",
    yellow: "rgba(210, 200, 80, 0.85)",
    gray: "rgba(140, 140, 140, 0.85)",
  };
  return m[color.toLowerCase()] ?? m.green;
}

/** Twemoji PNG 자산을 미리보기에 표시. drop-shadow 로 글로우 근사. */
function renderGraphicAssetPlaceholder({
  commonContainer,
  sizeRatio,
  category,
  name,
  tintColor,
  rotationDeg,
}: {
  commonContainer: CSSProperties;
  sizeRatio: number;
  category: string;
  name: string;
  tintColor?: string | null;
  rotationDeg?: number;
}) {
  const size = `${sizeRatio * 100}cqh`;
  const url = `/api/graphic-assets/${encodeURIComponent(category)}/${encodeURIComponent(name)}.png`;
  // 글로우는 drop-shadow 2단으로 근사 — 흰색 둘레는 가독성 유지, 색상 둘레는 톤 강조.
  const filterParts = [
    "drop-shadow(0 0 6px rgba(255,255,255,0.6))",
    "drop-shadow(0 0 12px rgba(255,200,0,0.45))",
  ];
  if (tintColor) {
    // smell — 자산은 흰 구름. 같은 위치에 색상 레이어를 mix-blend-mode 로 곱하기.
    return (
      <div
        style={{
          ...commonContainer,
          width: size,
          height: size,
          transform: `${commonContainer.transform ?? ""} rotate(${rotationDeg ?? 0}deg)`,
        }}
      >
        <img
          src={url}
          alt={`${category}/${name}`}
          draggable={false}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "contain",
            filter: filterParts.join(" "),
            opacity: 0.85,
          }}
        />
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: tintColor,
            mixBlendMode: "multiply",
            maskImage: `url(${url})`,
            WebkitMaskImage: `url(${url})`,
            maskRepeat: "no-repeat",
            WebkitMaskRepeat: "no-repeat",
            maskSize: "contain",
            WebkitMaskSize: "contain",
            maskPosition: "center",
            WebkitMaskPosition: "center",
            opacity: 0.7,
          }}
        />
      </div>
    );
  }
  return (
    <img
      src={url}
      alt={`${category}/${name}`}
      draggable={false}
      style={{
        ...commonContainer,
        width: size,
        height: size,
        objectFit: "contain",
        filter: filterParts.join(" "),
        transform: `${commonContainer.transform ?? ""} rotate(${rotationDeg ?? 0}deg)`,
      }}
    />
  );
}

// ── 공개 컴포넌트 ────────────────────────────────────────────────────────

/** 오버레이별 SFX 트리거 정보 — RenderSpec에 없는 필드를 별도 전달. */
export interface SfxTrigger {
  id: string;
  start_sec: number;
  end_sec?: number | null;
  sfx_asset_id: string;
  sfx_volume: number;
}

export interface OverlayCanvasProps {
  spec: RenderSpec;
  /** `<video src>` 로 사용할 footage 스트림 URL. */
  footageUrl: string;
  /** 부모가 video 요소에 접근할 수 있도록 ref 전달 (드래그 핸들 등). */
  videoRef?: React.RefObject<HTMLVideoElement | null>;
  /** video transform 결과(시뮬레이션 가능). 외부에서 주입할 수도 있음. */
  videoTransform?: SimpleRenderTransform;
  /** 자식 — 그래픽 드래그 핸들 등 외부 레이어를 video 사각형 위에 겹쳐 표시. */
  children?: React.ReactNode;
  /** SFX 미리듣기용 — projectId + sfx_asset_id 보유 오버레이 목록. 둘 다 있어야 동작. */
  projectId?: string;
  sfxTriggers?: SfxTrigger[];
  /** 씬 footage 원본 오디오 음량 (0~1). 기본 0.3(TTS 더킹). */
  footageAudioVolume?: number;
}

/**
 * 단순 비디오 + 텍스트/도형/그래픽 오버레이를 합성해 그린다.
 *
 * `isOverlayRenderSpec(spec) === true` 가 보장된 입력에서만 호출. 그 외엔 false 가 보장되어
 * 호출 측이 백엔드 fallback 으로 분기한다.
 */
export default function OverlayCanvas({
  spec,
  footageUrl,
  videoRef,
  videoTransform,
  children,
  projectId,
  sfxTriggers,
  footageAudioVolume,
}: OverlayCanvasProps) {
  // SFX 미리듣기 — videoRef가 있어야 timeupdate를 구독할 수 있다.
  // (외부에서 ref를 주입하지 않으면 내부 fallback ref 사용)
  const internalVideoRef = useRef<HTMLVideoElement | null>(null);
  const effectiveVideoRef = videoRef ?? internalVideoRef;
  useSfxPreview(effectiveVideoRef, projectId, sfxTriggers);
  useRampPlaybackRate(effectiveVideoRef, spec);
  // 슬라이더 드래그 중에도 즉시 반영되도록 footageAudioVolume 변경 시 video.volume 갱신.
  useEffect(() => {
    const v = effectiveVideoRef.current;
    if (!v) return;
    try { v.volume = Math.max(0, Math.min(1, footageAudioVolume ?? 0.3)); } catch { /* ignore */ }
  }, [effectiveVideoRef, footageAudioVolume]);
  const layers = spec.layers ?? [];
  const videoLayer = layers.find((l) => (l as { type?: string }).type === "video") as
    | VideoLayer
    | undefined;
  if (!videoLayer) {
    // 호출 측 보장 위반. 디버그용 메시지 — 정상 흐름에선 발생하지 않음.
    return <div style={{ color: "#aaa" }}>OverlayCanvas: video 레이어 없음</div>;
  }

  const transform: SimpleRenderTransform = videoTransform ?? applySimpleRenderTransform(videoLayer, spec.canvas);
  const playbackRate = transform.playbackRate ?? 1.0;

  // text/shape/graphic 레이어만 골라 합성 순서대로 렌더.
  // RenderSpec layers 배열 순서가 그리는 순서이므로 그대로 따른다 (filter/mosaic 등은
  // isOverlayRenderSpec 가 미리 차단했음).
  const overlayLayers = layers.filter((l) => {
    const t = (l as { type?: string }).type;
    return t === "text" || t === "shape" || t === "graphic";
  });

  // containerQuery (cqh, cqw) 를 활용해 font-size 와 그래픽 크기를 컨테이너 높이 기준으로
  // 환산한다. containerType: "size" 가 켜져 있어야 cqh 가 동작.
  const containerStyle: CSSProperties = {
    ...transform.container,
    containerType: "size" as CSSProperties["containerType"],
  };

  return (
    <div style={containerStyle}>
      <video
        ref={effectiveVideoRef}
        key={footageUrl}
        src={footageUrl}
        style={transform.media}
        controls
        loop
        onLoadedMetadata={(e) => {
          const v = e.currentTarget;
          try {
            v.volume = Math.max(0, Math.min(1, footageAudioVolume ?? 0.3));
          } catch { /* ignore */ }
          try {
            v.playbackRate = playbackRate;
          } catch {
            /* ignore */
          }
          // webm Infinity duration 회피
          if (!isFinite(v.duration) || v.duration === 0) {
            const onSeeked = () => {
              v.currentTime = 0;
              v.removeEventListener("seeked", onSeeked);
            };
            v.addEventListener("seeked", onSeeked);
            try {
              v.currentTime = 1e10;
            } catch {
              /* ignore */
            }
          }
        }}
      />
      {overlayLayers.map((layer, idx) => {
        const t = (layer as { type?: string }).type;
        if (t === "text") {
          return <TextOverlayItem key={`text-${idx}`} layer={layer as TextLayer} canvas={spec.canvas} />;
        }
        if (t === "shape") {
          return <ShapeOverlayItem key={`shape-${idx}`} layer={layer as ShapeLayer} canvas={spec.canvas} />;
        }
        if (t === "graphic") {
          return (
            <GraphicOverlayItem
              key={`graphic-${idx}`}
              layer={layer as GraphicLayer}
              canvas={spec.canvas}
            />
          );
        }
        return null;
      })}
      {children}
    </div>
  );
}

/** 미리보기 SFX 늦게 울려도 되는 상한(초). 이보다 늦게 알아차리면(탭 비활성 등) 울리지 않는다. */
export const SFX_MAX_LATE_SEC = 1.5;

/**
 * 직전 timeupdate 시각 `from` 과 이번 시각 `to` 사이에 `start` 를 지났는가.
 *
 * [회귀] 예전에는 `to ∈ [start, start+0.3)` 고정 창으로 판정했다. timeupdate 는 250ms 안팎
 * 간격이고, 씬 전환 직후 첫 tick 은 더 늦게 온다(실측: 이어보기 1번 씬 첫 tick t=0.917s).
 * 그래서 start_sec 0.3 인 1번 씬 SFX 가 창을 통째로 건너뛰어 울리지 않았다. 배속 재생이면
 * 창이 실시간으로 더 좁아져 더 자주 놓친다. 「창 안에 tick 이 떨어졌나」가 아니라
 * 「두 tick 사이에 시작점을 지났나」로 판정한다.
 */
export function sfxCrossed(from: number, to: number, start: number): boolean {
  if (start < from - 1e-3) return false; // 이미 지난 구간
  if (start > to) return false; // 아직 안 옴
  return to - start <= SFX_MAX_LATE_SEC;
}

/** 미리보기 SFX 트리거 — video의 timeupdate를 구독해 start_sec 도달 시 1회 재생.
 * 같은 트리거가 video 루프/seek로 반복 통과해도 마지막 통과 시각을 기억해 중복 재생을 막는다.
 * 외부 모듈(PreviewPanel 등)에서도 임의의 video 요소에 SFX를 걸 수 있도록 export.
 */
export function useSfxPreview(
  videoOrRef: HTMLVideoElement | React.RefObject<HTMLVideoElement | null> | null,
  projectId: string | undefined,
  triggers: SfxTrigger[] | undefined,
): void {
  // videoOrRef 가 element 직접이면 그 값을, RefObject 면 .current 를 쓴다.
  //   과거 videoRef.current 를 폴링으로 대기했으나, 5초 안에 마운트 못 하면 영원히 실패.
  //   호출부에서 callback ref 로 element state 를 만들어 여기에 직접 넘기면
  //   video 요소가 늦게 마운트되거나 재마운트돼도 훅이 즉시 다시 실행된다.
  const video: HTMLVideoElement | null = (() => {
    if (!videoOrRef) return null;
    if (videoOrRef instanceof HTMLVideoElement) return videoOrRef;
    return videoOrRef.current ?? null;
  })();
  useEffect(() => {
    if (!projectId || !triggers || triggers.length === 0) {
      console.warn(`[sfx-preview] 훅 비활성 — projectId=${!!projectId} triggers=${triggers?.length ?? 0}`);
      return;
    }
    if (!video) {
      console.warn(`[sfx-preview] <video> 미마운트 (element state 미확정)`);
      return;
    }
    console.warn(
      `[sfx-preview] 훅 등록 — triggers=${triggers.length}`,
      `video.src=${video.src.slice(0, 60)}...`,
      `paused=${video.paused}`,
      `currentTime=${video.currentTime}`,
      `readyState=${video.readyState}`,
    );
    (window as unknown as { __sfxVideo?: HTMLVideoElement }).__sfxVideo = video;
    const cleanup = attachSfxListeners(video, projectId, triggers);
    return cleanup;
  }, [video, projectId, triggers]);
}

/**
 * <video> 에 SFX 트리거 listener 를 부착하고 cleanup 함수를 반환한다.
 * useSfxPreview 의 내부 헬퍼 — videoRef 가 늦게 채워지는 케이스 분리용.
 */
function attachSfxListeners(
  v: HTMLVideoElement,
  projectId: string,
  triggers: SfxTrigger[],
): () => void {
  // (원본 effect 본문 — videoRef.current 확정된 video 인스턴스에 직접 부착)
  // 핵심 로직은 변경 없음.
  {
    // 트리거별 마지막 발화 시각 — video.currentTime 기준. seek로 뒤로 가면 리셋.
    const lastFired = new Map<string, number>();
    let prevTime = v.currentTime;
    // audio 풀: 같은 자산을 여러 번 빠르게 재생할 수 있게 자산 ID당 HTMLAudioElement 캐시.
    const audioPool = new Map<string, HTMLAudioElement>();
    const getAudio = (assetId: string): HTMLAudioElement => {
      let a = audioPool.get(assetId);
      if (!a) {
        // #sfx-presets: `preset:` prefix 면 내장 기본 효과음 프리셋 URL, 아니면 studio asset URL.
        a = new Audio(resolveSfxStreamUrl(projectId, assetId));
        a.preload = "auto";
        audioPool.set(assetId, a);
      }
      return a;
    };

    // 자동재생 정책 우회 — <video> play() 가 user gesture 로 시작될 때 SFX audio 도 prime.
    // load() 만 호출해 디코딩을 미리 시작. muted 트릭은 race 조건으로 muted 가 남는
    // 경우가 있어 안 쓴다 — <video> play() 가 같은 user gesture 였기 때문에 audio.play()
    // 도 자동 허용된다 (Chrome 의 transient activation 정책).
    function primeAudiosOnce() {
      for (const tr of triggers!) {
        const a = getAudio(tr.sfx_asset_id);
        try {
          a.load();
        } catch { /* ignore */ }
      }
    }

    let tickCount = 0;
    function onTimeUpdate() {
      const t = v!.currentTime;
      // 첫 10번은 항상 로그로 timeupdate 가 정말 발화하는지 확인
      tickCount += 1;
      if (tickCount <= 10) {
        console.warn(`[sfx-preview] timeupdate#${tickCount} t=${t.toFixed(3)}s`);
      }
      // 뒤로 감기(루프 포함)면 lastFired 초기화. 루프는 seeked 없이 0 으로 돌아오므로
      // 구간 시작을 0 으로 잡아 start_sec=0 트리거도 다시 울리게 한다.
      if (t + 0.05 < prevTime) { lastFired.clear(); prevTime = 0; }
      const from = prevTime;
      prevTime = t;
      for (const tr of triggers!) {
        // 직전 tick ~ 이번 tick 사이에 start_sec 을 지났으면 발화(sfxCrossed 주석 참고).
        if (sfxCrossed(from, t, tr.start_sec)) {
          const last = lastFired.get(tr.id);
          // 같은 사이클(루프)에서 중복 방지: 직전 발화가 같은 위치(±0.2초)면 스킵.
          if (last !== undefined && Math.abs(last - t) < 0.5) continue;
          lastFired.set(tr.id, t);
          console.warn(`[sfx-preview] 트리거 발화 t=${t.toFixed(2)}s asset=${tr.sfx_asset_id} url=${resolveSfxStreamUrl(projectId, tr.sfx_asset_id)}`);
          try {
            const audio = getAudio(tr.sfx_asset_id);
            // muted 가 prime 으로 켜진 채 남아 있으면 명시적으로 끔.
            if (audio.muted) audio.muted = false;
            audio.volume = Math.max(0, Math.min(1, tr.sfx_volume));
            audio.currentTime = 0;
            console.warn(
              `[sfx-preview] audio 상태: muted=${audio.muted} volume=${audio.volume} readyState=${audio.readyState} duration=${audio.duration} src=${audio.src.slice(-50)}`,
            );
            void audio.play().then(() => {
              console.warn(`[sfx-preview] play() 성공 (asset=${tr.sfx_asset_id})`);
            }).catch((err) => {
              console.warn(`[sfx-preview] play() 실패 (asset=${tr.sfx_asset_id}):`, err?.name || err);
            });
          } catch (e) {
            console.warn(`[sfx-preview] getAudio/play 예외:`, e);
          }
        }
      }
    }
    function onSeeked() {
      lastFired.clear();
      prevTime = v!.currentTime;
    }
    function onPause() {
      // 일시정지 시 진행 중인 SFX 정지 — 미리보기 UX 일관성.
      for (const a of audioPool.values()) {
        try { a.pause(); } catch { /* ignore */ }
      }
    }
    // <video> 가 user gesture 로 play 되는 시점에 prime (자동재생 정책 우회).
    function onPlayPrime() {
      primeAudiosOnce();
      v!.removeEventListener("play", onPlayPrime);
    }

    v.addEventListener("timeupdate", onTimeUpdate);
    v.addEventListener("seeked", onSeeked);
    v.addEventListener("pause", onPause);
    // 씬 영상이 끝나면 긴 효과음(장작·박수 등)도 씬 경계에서 끊는다 — 최종 렌더의 씬 끝 클램프와 일치.
    v.addEventListener("ended", onPause);
    v.addEventListener("play", onPlayPrime);
    return () => {
      v.removeEventListener("timeupdate", onTimeUpdate);
      v.removeEventListener("seeked", onSeeked);
      v.removeEventListener("pause", onPause);
      v.removeEventListener("ended", onPause);
      v.removeEventListener("play", onPlayPrime);
      for (const a of audioPool.values()) {
        try { a.pause(); } catch { /* ignore */ }
      }
      audioPool.clear();
    };
  }
}
