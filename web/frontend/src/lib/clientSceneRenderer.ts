/**
 * 단순 RenderSpec 클라이언트 렌더러 (#309).
 *
 * 백엔드 ffmpeg `video_layer_to_vf` 가 만드는 결과를 *그대로* CSS transform 으로 재현한다.
 * 같은 RenderSpec 입력 → 같은 픽셀 출력이 보장되어야 fit 회귀(2026-05-27 시리즈)가
 * 다시 발생하지 않는다.
 *
 * 핵심 매핑
 * --------
 *  - canvas: 출력 해상도 (예: 1920×1080)
 *  - source_size: 영상 원본 (회전 적용 후) 해상도
 *  - scale_x / scale_y: source_size * scale_? = "스케일된 가시 영역" 크기
 *  - crop: 스케일 결과 위에서 (x, y, w, h) 만큼 잘라낸 부분만 캔버스 좌상단에 그림 (cover)
 *  - pad: 캔버스 안에서 그릴 위치 (contain — 가운데 정렬 + letterbox)
 *  - 두 케이스 모두 *결과는 캔버스(=출력 해상도)에 정확히 들어맞음*
 *
 * CSS 적용 방법
 * -------------
 *  컨테이너(`<div>`)는 캔버스 종횡비(aspectRatio) 로 두고, 안의 `<video>` 를 다음 식으로
 *  변환한다 — 캔버스 좌표계의 *비율* 로 환산:
 *
 *  contain (pad 사용):
 *    video width  = pad.w / canvas.width  (% of container)
 *    video height = pad.h / canvas.height
 *    video left   = pad.x / canvas.width
 *    video top    = pad.y / canvas.height
 *    video objectFit = "fill" (이미 비율을 맞게 스케일했으므로 fill)
 *
 *  cover (crop 사용):
 *    영상 전체 크기 = (source_size * scale) / canvas (overflow)
 *    위치 = -(crop.x / canvas.width) 만큼 좌측으로 끌어옴
 *    컨테이너 overflow: hidden 으로 잘라냄
 *
 *  stretch (crop/pad 둘 다 None):
 *    video 가 컨테이너 100%×100% 채움. objectFit fill.
 *
 *  비율 좌표(0~1) 만 사용 → 컨테이너가 어떤 픽셀 크기여도 동일하게 표시.
 */
import type { CSSProperties } from "react"
import type { RenderSpec, VideoLayer, RenderCanvas } from "../types"

export interface SimpleRenderTransform {
  /** 컨테이너 스타일(캔버스 종횡비 유지). */
  container: CSSProperties
  /** `<video>` 또는 `<img>` 스타일(절대 위치). */
  media: CSSProperties
  /** `<video>.playbackRate` 에 대입할 값. video 레이어가 아니면 undefined. */
  playbackRate?: number
}

/**
 * VideoLayer 1개를 CSS transform 으로 환산한다.
 *
 * 입력은 백엔드와 *완전히 동일한 RenderSpec* 이므로, 결과 픽셀은 백엔드 ffmpeg 출력과
 * (반올림 오차 ±1px 이내) 일치해야 한다.
 */
export function applySimpleRenderTransform(
  layer: VideoLayer,
  canvas: RenderCanvas,
): SimpleRenderTransform {
  const cw = canvas.width
  const ch = canvas.height

  const container: CSSProperties = {
    position: "relative",
    width: "100%",
    height: "100%",
    aspectRatio: `${cw} / ${ch}`,
    overflow: "hidden",
    background: "#000",
  }

  const playbackRate = layer.playback_rate ?? 1.0

  const sx = layer.scale_x
  const sy = layer.scale_y
  const sourceW = layer.source_size.width
  const sourceH = layer.source_size.height

  // 스케일된 가시 영역(픽셀)
  const scaledW = sourceW * sx
  const scaledH = sourceH * sy

  // pad 가 있으면 contain. 컨테이너 *내부* 의 일부 영역에만 그린다.
  if (layer.pad) {
    const pad = layer.pad
    const media: CSSProperties = {
      position: "absolute",
      left: `${(pad.x / cw) * 100}%`,
      top: `${(pad.y / ch) * 100}%`,
      width: `${(pad.w / cw) * 100}%`,
      height: `${(pad.h / ch) * 100}%`,
      // pad 영역이 종횡비 보존된 스케일 결과이지만, 부모 컨테이너가 부동 소수점/CSS 라운딩
      // 으로 정확히 캔버스 비율이 되지 않는 경우 fill 은 영상을 늘려 표시한다. contain 으로
      // 두면 약간의 letterbox(1~2px) 만 생기고 영상 원본 비율이 유지된다.
      objectFit: "contain" as const,
      display: "block",
    }
    return { container, media, playbackRate }
  }

  // crop 이 있으면 cover. 영상 전체를 컨테이너 비율 기준으로 크게 그리고 overflow 로 잘라냄.
  if (layer.crop) {
    const crop = layer.crop
    // 컨테이너 폭/높이 대비 스케일된 영상 폭/높이 비율 (>= 1)
    const widthPct = (scaledW / cw) * 100
    const heightPct = (scaledH / ch) * 100
    // crop 좌상단을 컨테이너 좌상단(0,0) 으로 끌어오기 위해 음수 offset
    const leftPct = -(crop.x / cw) * 100
    const topPct = -(crop.y / ch) * 100
    const media: CSSProperties = {
      position: "absolute",
      left: `${leftPct}%`,
      top: `${topPct}%`,
      width: `${widthPct}%`,
      height: `${heightPct}%`,
      objectFit: "fill" as const,
      display: "block",
    }
    return { container, media, playbackRate }
  }

  // stretch — 캔버스 가득 채움.
  const media: CSSProperties = {
    position: "absolute",
    left: 0,
    top: 0,
    width: "100%",
    height: "100%",
    objectFit: "fill" as const,
    display: "block",
  }
  return { container, media, playbackRate }
}

/**
 * RenderSpec 1개에서 단일 VideoLayer 를 꺼내 transform 을 계산.
 * `isSimpleRenderSpec(spec) === true` 가 보장된 입력에서만 안전하게 호출한다.
 */
export function transformForSimpleSpec(spec: RenderSpec): SimpleRenderTransform {
  const layer = (spec.layers ?? []).find((l) => (l as { type?: string }).type === "video") as
    | VideoLayer
    | undefined
  if (!layer) {
    throw new Error("transformForSimpleSpec: video 레이어가 없습니다 (isSimpleRenderSpec 위배)")
  }
  return applySimpleRenderTransform(layer, spec.canvas)
}
