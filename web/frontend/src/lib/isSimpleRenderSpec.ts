/**
 * 단순 RenderSpec 판정기 (#309).
 *
 * "단순 씬"은 클라이언트 `<video>` + CSS transform 만으로 백엔드 ffmpeg 호출 없이
 * 정확하게 그릴 수 있는 씬을 말한다. 단순 판정 규칙:
 *
 * 1. layers 가 *정확히 1개* 이고, 그 레이어가 `video` 또는 `image` 타입.
 * 2. video 레이어는 fit(scale_x/scale_y/crop/pad)과 playback_rate 만 사용 — opacity 1.0,
 *    rotation_deg 0 인 경우만 단순 모드로 간주한다. 그 외는 안전하게 백엔드 fallback.
 * 3. image 레이어(Ken Burns)는 zoompan 보간을 CSS 만으로 정밀하게 재현하기 어려우므로
 *    단순 모드 *제외* — 항상 백엔드 렌더.
 *
 * 이 함수가 `true` 를 돌려준 RenderSpec 만 `applySimpleRenderTransform` 으로 그릴 수 있다.
 * 함수 자체는 *예외를 던지지 않으며*, 입력이 손상되었으면 false 를 반환한다.
 */
import type { RenderSpec, VideoLayer, ImageLayer } from "../types"

/**
 * 단순 모드 여부. true 면 클라 렌더 가능.
 *
 * 회귀 방지 원칙: 새 effect 타입이 layers 에 끼어들 가능성을 막기 위해 *허용 리스트*
 * 방식으로 검사한다. layers 가 1개여도 그 타입이 video/image 외이면 false.
 */
export function isSimpleRenderSpec(spec: RenderSpec | null | undefined): boolean {
  if (!spec) return false
  const layers = spec.layers ?? []
  if (layers.length !== 1) return false

  const layer = layers[0]
  if (!layer || typeof layer !== "object") return false

  // image 레이어(Ken Burns)는 zoompan 시간축 보간이 까다로워 클라 렌더 제외.
  // - canvas zoompan(약 30fps × duration)을 CSS animation 으로 재현 가능하지만 정밀도가
  //   백엔드와 미세하게 다를 수 있어 #309 1차 범위에서는 안전하게 백엔드 fallback.
  if (layer.type === "image") {
    return false
  }

  if (layer.type !== "video") return false

  const vl = layer as VideoLayer

  // opacity 1.0 만 단순 모드. 0.5 같은 반투명은 백엔드와 동등성 보장 어려움.
  if (vl.opacity !== undefined && vl.opacity !== 1.0) return false

  // rotation 0 만. 90/180/270 회전은 CSS `transform: rotate()` 로 가능하지만 source_size 가
  // 이미 회전 후 기준으로 계산되어 있어 CSS 매핑 시 추가 보정이 필요. 1차 범위 제외.
  if (vl.rotation_deg !== undefined && vl.rotation_deg !== 0) return false

  // playback_rate 는 `<video>.playbackRate` 로 그대로 적용 가능.

  return true
}

/**
 * 디버그용 — 단순 모드가 아닌 이유를 한글로 반환. UI 툴팁/콘솔에 활용.
 *
 * #343 회귀 이후로 filter / motion 레이어가 명시적으로 layers 에 들어오므로 이 함수도
 * 동일한 layer 타입을 인식해 사용자에게 친화적인 메시지를 보여준다.
 */
export function explainSimpleRenderSpec(spec: RenderSpec | null | undefined): string {
  if (!spec) return "RenderSpec 없음"
  const layers = spec.layers ?? []
  if (layers.length === 0) return "레이어 없음 — 빈 씬"
  if (layers.length > 1) {
    // #343 — filter / motion 이 끼면 사용자가 가장 흔히 추가하는 효과이므로 우선적으로 안내.
    const layerTypes = layers.map((l) => (l as { type?: string }).type ?? "?")
    if (layerTypes.includes("filter")) return "색감 필터 적용 — 백엔드 렌더"
    if (layerTypes.includes("motion")) return "모션 효과 적용 — 백엔드 렌더"
    return `복합 효과 ${layers.length}개 (${layerTypes.join(", ")}) — 백엔드 렌더`
  }
  const layer = layers[0] as { type?: string }
  if (layer.type === "image") return "Ken Burns(이미지) — 백엔드 렌더"
  if (layer.type !== "video") return `미지원 레이어 타입(${layer.type}) — 백엔드 렌더`
  const vl = layer as VideoLayer
  if (vl.opacity !== undefined && vl.opacity !== 1.0) return "투명도 적용 — 백엔드 렌더"
  if (vl.rotation_deg !== undefined && vl.rotation_deg !== 0) return "회전 적용 — 백엔드 렌더"
  return "단순 씬 — 클라 렌더 가능"
}

export type { RenderSpec, VideoLayer, ImageLayer }
