/**
 * 오버레이 확장 RenderSpec 판정기 (#310).
 *
 * `isSimpleRenderSpec` (#309) 가 video 레이어 1개만 통과시키는 것과 달리, 본 모듈은
 * 그 위에 **text / shape / graphic 오버레이가 추가된 경우**까지 클라이언트 합성으로
 * 그릴 수 있는지 판단한다.
 *
 * 클라 합성 허용 규칙
 * ──────────────────
 * 1. video 레이어가 **정확히 1개** 존재.
 * 2. 그 video 레이어는 `isSimpleRenderSpec` 와 동일한 단순 조건을 만족
 *    (opacity 1.0, rotation 0).
 * 3. 추가 레이어는 **text / shape / graphic** 만 허용.
 * 4. filter / motion / mosaic / image 레이어가 *하나라도* 있으면 false (백엔드 fallback).
 *    - filter: 색감 변환은 CSS 만으로 백엔드 PIL 결과와 동일 보장 어려움.
 *    - motion: 시간축 보간이 필요해 정밀 재현 부담 큼.
 *    - mosaic: 픽셀화/블러 알고리즘 차이.
 *    - image (Ken Burns): zoompan 보간 차이 — `isSimpleRenderSpec` 와 동일 사유.
 *
 * 비용 영향
 * ────────
 * 텍스트/도형 오버레이는 사용자가 위치/색/크기를 자주 바꾼다. 한 번의 위치 드래그가
 * 수십 번의 미리보기 요청을 유발하므로, 클라 합성으로 대체하면 백엔드 ffmpeg 호출
 * 비용을 ~90% 절감한다.
 *
 * `isSimpleRenderSpec` 와의 관계
 * ──────────────────────────────
 *  - `isSimpleRenderSpec(spec) === true` 이면 본 함수도 true (오버레이가 0개여도 OK).
 *  - 반대는 성립하지 않음 (text 1개만 추가돼도 isSimpleRenderSpec 는 false 지만
 *    isOverlayRenderSpec 는 true).
 */
import type { RenderSpec, VideoLayer } from "../types";

type LayerLike = { type?: string };

/** 오버레이 모드 합성 가능 여부. true 면 클라가 video + 텍스트/도형/그래픽을 직접 그린다. */
export function isOverlayRenderSpec(spec: RenderSpec | null | undefined): boolean {
  if (!spec) return false;
  const layers = (spec.layers ?? []) as LayerLike[];
  if (layers.length === 0) return false;

  // 1) video 레이어가 정확히 1개
  const videoLayers = layers.filter((l) => l && l.type === "video");
  if (videoLayers.length !== 1) return false;

  const vl = videoLayers[0] as VideoLayer;
  // 2) video 단순 조건 (isSimpleRenderSpec 의 규칙과 동일하게 유지)
  if (vl.opacity !== undefined && vl.opacity !== 1.0) return false;
  if (vl.rotation_deg !== undefined && vl.rotation_deg !== 0) return false;

  // 3/4) 나머지 레이어는 text/shape/graphic 만 허용. 그 외는 모두 차단.
  for (const l of layers) {
    const t = l?.type;
    if (t === "video") continue;
    if (t === "text" || t === "shape" || t === "graphic") continue;
    // image(KenBurns)/filter/motion/mosaic 또는 미지원 타입 → 백엔드 fallback
    return false;
  }
  return true;
}

/**
 * 디버그용 — 오버레이 모드가 아닌 이유를 한글로 반환.
 *
 * `isSimpleRenderSpec` 가 false 라도 본 함수가 true 라면 "단순 + 오버레이" 모드. 이 메시지는
 * 그 경계를 사용자가 이해하기 쉽도록 백엔드 fallback 사유만 기록한다.
 */
export function explainOverlayRenderSpec(spec: RenderSpec | null | undefined): string {
  if (!spec) return "RenderSpec 없음";
  const layers = (spec.layers ?? []) as LayerLike[];
  if (layers.length === 0) return "레이어 없음 — 빈 씬";

  const videoLayers = layers.filter((l) => l && l.type === "video");
  if (videoLayers.length === 0) return "video 레이어 없음 (Ken Burns 이미지일 가능성) — 백엔드 렌더";
  if (videoLayers.length > 1) return `video 레이어 ${videoLayers.length}개 — 백엔드 렌더`;

  const vl = videoLayers[0] as VideoLayer;
  if (vl.opacity !== undefined && vl.opacity !== 1.0) return "video 투명도 적용 — 백엔드 렌더";
  if (vl.rotation_deg !== undefined && vl.rotation_deg !== 0) return "video 회전 적용 — 백엔드 렌더";

  for (const l of layers) {
    const t = l?.type;
    if (t === "video" || t === "text" || t === "shape" || t === "graphic") continue;
    if (t === "image") return "이미지(Ken Burns) — 백엔드 렌더";
    if (t === "filter") return "색감 필터 — 백엔드 렌더";
    if (t === "motion") return "모션 효과 — 백엔드 렌더";
    if (t === "mosaic") return "모자이크 오버레이 — 백엔드 렌더";
    return `미지원 레이어(${t}) — 백엔드 렌더`;
  }
  return "오버레이 단순 모드 — 클라 렌더 가능";
}
