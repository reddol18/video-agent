export type EffectType =
  | "clip"
  | "kenburns"
  | "kenburns_copy_img"
  | "edit"
  | "sync"
  | "multiscreen"
  | "title_gen";

// #258 footage 종횡비 fit 옵션
//   "contain" — 비율 유지하며 letterbox/pillarbox 패딩 (기본)
//   "cover"   — 비율 유지하며 출력 영역을 가득 채우고 넘치는 부분 crop
//   "stretch" — 비율 무시하고 강제 늘림 (오프셋 무시)
export type FitMode = "contain" | "cover" | "stretch";

// #263 원본 영상 수동 회전 (0/90/180/270 도). 0이 기본 (회전 없음).
// 0이 아니면 자동 회전 휴리스틱 비활성화.
export type RotationDeg = 0 | 90 | 180 | 270;

export interface ClipEffect {
  type: "clip";
  file: string;
  x_offset?: number;  // 비율 -1.0~1.0 (양수: 오른쪽, 음수: 왼쪽)
  y_offset?: number;  // 비율 -1.0~1.0 (양수: 아래, 음수: 위)
  fit?: FitMode;      // #258 종횡비 fit (기본 "contain")
  rotation?: RotationDeg; // #263 수동 회전 각도 (기본 0)
  // 씬 나누기 — 같은 footage를 여러 씬이 공유하면서 각자 다른 구간만 보여줄 때 사용.
  // 기본(미지정): 0 ~ footage 끝까지.
  clip_start_sec?: number;
  clip_end_sec?: number | null;
}
export interface KenburnsEffect {
  type: "kenburns";
  image: string;
  // 이미지가 출력 비율과 다를 때 처리: cover(가득+crop, 기본) / contain(여백) / stretch(찌그러뜨림).
  fit?: "cover" | "contain" | "stretch";
}
export interface KenburnsCopyImgOptions {
  input: string;
  output: string;
  extra?: Record<string, unknown>;
}
export interface KenburnsCopyImgEffect {
  type: "kenburns_copy_img";
  copy_img_options: KenburnsCopyImgOptions;
  image: string;
}
export interface EditEffect {
  type: "edit";
  description: string;
  file: string;
  min_duration: number;
  preview_placeholder?: boolean;
}
export interface SyncEffect {
  type: "sync";
  word: string;
  inner?: EditEffect | ClipEffect | null;
  preview_placeholder?: boolean;
}
// N×M 그리드 비교 셀 광원 그라데이션 보더
export interface GridBorder {
  width_px: number;       // 보더 두께(px). 0이면 보더 없음.
  color_from: string;     // 그라데이션 시작 색 (#RRGGBB)
  color_to: string;       // 그라데이션 끝 색 (#RRGGBB)
  animate: boolean;       // 광원 회전 애니메이션 on/off
  speed?: number;         // 광원 회전 속도 배수 (기본 1.0)
}
export interface GridCell {
  file: string;           // 셀 이미지/영상 파일명 (빈 문자열이면 배경색 셀)
  border?: GridBorder | null;
}
export interface MultiscreenEffect {
  type: "multiscreen";
  files: string[];
  // N×M 그리드 비교 확장 (옵셔널). rows/cols/cells 가 모두 있으면 그리드 렌더.
  rows?: number | null;
  cols?: number | null;
  cells?: GridCell[] | null;
}
export interface TitleOptions {
  text: string;
  mode?: string;
  duration?: number;
  font_size?: number;
  text_color?: string;
  bg_color?: string;
  dissolve_time?: number;
}
export interface TitleGenEffect {
  type: "title_gen";
  options: TitleOptions;
}

export type EffectConfig =
  | ClipEffect
  | KenburnsEffect
  | KenburnsCopyImgEffect
  | EditEffect
  | SyncEffect
  | MultiscreenEffect
  | TitleGenEffect;

// ── Overlay enter/exit effects (#287) ─────────────────────────────────────
// 등장/사라짐 효과. 텍스트만 typing/zoom 추가 옵션을 가진다.
// 기존 데이터(필드 없음)는 백엔드/프론트 모두 fade + 0.3초로 폴백한다.
// pass_* 는 "관통" — 한쪽 화면 밖에서 날아 들어와 반대편 화면 밖으로 빠져나간다.
// 등장에만 지정하면 사라짐 방향은 백엔드가 자동으로 반대편으로 연결한다.
export type PassThroughEffect =
  | "pass_left_to_right"
  | "pass_right_to_left"
  | "pass_top_to_bottom"
  | "pass_bottom_to_top";
export type TextEnterExitEffect =
  | "none"
  | "fade"
  | "typing"
  | "zoom"
  | PassThroughEffect;
// non-text 오버레이 등장/사라짐 효과. 이미지 오버레이는 아래 slide/zoom/pop 확장까지 지원.
// 도형/그래픽 오버레이는 이 타입을 공유하되 실제 렌더러는 fade 로 폴백한다 (이미지 전용 확장).
export type NonTextEnterExitEffect =
  | "none"
  | "fade"
  | "slide_in_left"
  | "slide_in_right"
  | "slide_in_top"
  | "slide_in_bottom"
  | "zoom"
  | "pop"
  | PassThroughEffect;
// 텍스트 표시 중 반복 애니메이션. 백엔드 TextLoopAnimation 과 1:1 대응.
// speedlines 는 집중선이 글자가 차지하는 영역 안에만 마스킹되어 나타나는 효과.
export type TextLoopAnimation =
  | "none"
  | "bounce"
  | "shake"
  | "pulse"
  | "wave"
  | "speedlines";

// 텍스트 마스킹 집중선 파라미터. 모두 선택값이며 생략 시 백엔드 기본값을 쓴다.
// color 를 비우면 글자색, center_x/center_y 를 비우면 글자 위치를 따라간다.
export interface TextSpeedLinesConfig {
  color?: string | null;
  density?: number | null;
  center_x?: number | null;
  center_y?: number | null;
  // 선이 글자 외곽에서 안쪽으로 파고드는 깊이 비율(0~1).
  depth?: number | null;
  // "tail"(기본) — 날아온 방향으로 글자 *밖*에 꼬리를 그린다(관통 효과 필요).
  // "inner"      — 글자 획 *안쪽*을 집중선으로 갉아낸다(아주 큰 글자에만).
  mode?: "tail" | "inner" | null;
  // 꼬리 길이(글자 크기 대비 배수), 불투명도(0~1), 줄무늬 주기(px).
  tail_length?: number | null;
  tail_opacity?: number | null;
  tail_stripe?: number | null;
  inner_ratio?: number | null;
  thickness?: number | null;
  width_variance?: number | null;
  jitter?: number | null;
  seed?: number | null;
  supersample?: number | null;
  num_buckets?: number | null;
  animate?: boolean | null;
  anim_speed?: number | null;
  anim_period_sec?: number | null;
}

// ── Text Overlay ──────────────────────────────────────────────────────────

export interface TextOverlayPreset {
  id: string;          // uuid
  name: string;        // 사용자가 지정한 이름
  created_at: string;
  style: Omit<TextOverlay, "id" | "text" | "start_sec" | "end_sec">;
  // text, 타임코드는 씬마다 다르므로 제외. 스타일 속성만 저장.
  // ── #290 — 커스텀(프리셋) 저장 시 "SFX 포함" 토글 결과 ─────────────────
  // include_sfx=false면 적용 시 sfx_asset_id/sfx_volume을 빈 값으로 강제. true면 style 내 값을 함께 적용.
  // 기존 프리셋(undefined)은 false 취급(SFX 없이 적용)으로 회귀 안전.
  include_sfx?: boolean;
}

export interface GradientStop {
  color: string;
  position: number; // 0.0 ~ 1.0
}

export interface GradientConfig {
  angle: number;
  stops: GradientStop[];
}

export interface TextOverlay {
  id: string;
  text: string;
  x: number;           // 0.0 ~ 1.0
  y: number;           // 0.0 ~ 1.0
  font_size: number;
  font_family: string; // 기본값 "NotoSansKR"
  color: string;       // hex
  gradient: GradientConfig | null;
  bold: boolean;
  italic: boolean;
  outline_color: string;
  outline_width: number;
  glow_color: string;
  glow_radius: number;
  opacity: number;
  align: "left" | "center" | "right";
  start_sec: number;
  end_sec: number | null;
  // ── SFX (#290) — 오버레이별 1개 SFX, enter(start_sec) 시점에 트리거 ────
  // sfx_asset_id가 null이면 SFX 없음. studio_assets에서 asset_type="audio" 자산을 참조.
  // sfx_volume은 0.0~1.0 (기본 0.7). 최종 렌더 시 TTS 활성 구간에서 자동 더킹.
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  // ── 등장/사라짐 효과 (#287) — optional + 기본값 처리 ─────────────────────
  // 기존 데이터(필드 없음)는 fade + 0.3초로 회귀 안전. 슬라이더는 sliderOnly 패턴.
  enter_effect?: TextEnterExitEffect;
  exit_effect?: TextEnterExitEffect;
  enter_duration_sec?: number;
  exit_duration_sec?: number;
  // ── 표시 중 반복 애니메이션 ─────────────────────────────────────────────
  // 기본 'none'. amplitude/period 의미는 백엔드 모델 주석 참조.
  loop_animation?: TextLoopAnimation;
  loop_amplitude?: number;
  loop_period_sec?: number;
  // loop_animation="speedlines" 일 때만 쓰이는 집중선 파라미터.
  text_speed_lines?: TextSpeedLinesConfig | null;
  // ── 카운트다운 ──────────────────────────────────────────────────────────
  // countdown_from > 0 이면 text 대신 숫자가 1초(interval)마다 줄어들며 표시된다.
  // 폰트/색/외곽선/위치 등 나머지 스타일은 일반 텍스트와 동일하게 적용된다.
  countdown_from?: number;
  countdown_to?: number;
  countdown_interval_sec?: number;
  countdown_end_text?: string;
}

// ── Shape Overlay ─────────────────────────────────────────────────────────

export type ShapeType = "rect" | "ellipse";

export interface ShapeOverlay {
  id: string;
  shape: ShapeType;
  x: number;        // 0.0~1.0 비율 x 좌표 (좌상단 기준)
  y: number;        // 0.0~1.0 비율 y 좌표 (좌상단 기준)
  width: number;    // 0.0~1.0 비율 너비
  height: number;   // 0.0~1.0 비율 높이
  color: string;    // hex 색상 (예: "#ff0000")
  opacity: number;  // 0.0 ~ 1.0
  start_sec: number;
  end_sec: number | null;
  // ── SFX (#290) — 오버레이별 1개 SFX, enter(start_sec) 시점에 트리거 ────
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  // ── 등장/사라짐 효과 (#287) — optional + 기본값 처리 ─────────────────────
  enter_effect?: NonTextEnterExitEffect;
  exit_effect?: NonTextEnterExitEffect;
  enter_duration_sec?: number;
  exit_duration_sec?: number;
}

// ── Image Overlay ─────────────────────────────────────────────────────────
// footage 스튜디오의 이미지 자산을 씬 위에 자유 위치·크기로 합성하는 오버레이.
// 좌표 규약은 ShapeOverlay/MosaicOverlay와 동일 (좌상단 기준 0~1 비율).
// 주 용도: "이전 영상 썸네일 + 제목" 카드처럼 이미지 + 캡션을 한 묶음으로 표시.
//
// caption_text가 비어있지 않으면 이미지 *아래*에 캡션 박스가 추가로 그려진다
// (이미지 영역은 그대로 유지 — width/height는 이미지 영역만 가리킴).
// 적용 순서: filters → motions → mosaic_overlays → image_overlays → text/shape/graphic.

export interface ImageOverlay {
  id: string;
  /** studio_assets 중 asset_type="image"인 자산의 asset_id. */
  image_asset_id: string;
  // 좌상단 기준 비율 좌표
  x: number;
  y: number;
  width: number;        // 캔버스 폭 대비 비율 — 이미지 영역만
  height: number;       // 캔버스 높이 대비 비율 — 이미지 영역만
  opacity?: number;
  rotation_deg?: number;
  start_sec?: number;
  end_sec?: number | null;
  // ── 캡션 (선택) ──────────────────────────────────────────────────────────
  // null/빈 문자열이면 캡션 미표시 (이미지만 합성).
  caption_text?: string | null;
  caption_font_size?: number;     // 캔버스 폭 대비 비율
  caption_color?: string;
  caption_bg_color?: string | null;
  caption_bg_opacity?: number;
  caption_padding?: number;        // 캔버스 폭 대비 비율
  // ── 등장/사라짐 효과 — 다른 오버레이와 동일한 fade/none 정책 ───────────
  enter_effect?: NonTextEnterExitEffect;
  exit_effect?: NonTextEnterExitEffect;
  enter_duration_sec?: number;
  exit_duration_sec?: number;
  // ── SFX — 오버레이별 1개 SFX ──────────────────────────────────────────
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  // ── Scene Tag 시스템 (#215) ──────────────────────────────────────────────
  source_id?: string;
}

// ── Mosaic Overlay (#264) ─────────────────────────────────────────────────
// 영상의 일부 영역에 모자이크/블러를 입혀 가리는 오버레이.
// 도형 오버레이(ShapeOverlay)와 동일한 좌표 규약(좌상단 기준 0~1 비율).
// 1차 구현 범위: shape="rect" 정식 렌더링. shape="ellipse"는 데이터만 받고 렌더러에서 rect 폴백.
export type MosaicMode = "pixelate" | "blur";
export type MosaicShape = "rect" | "ellipse";

export interface MosaicOverlay {
  id: string;
  shape: MosaicShape;
  x: number;          // 0.0~1.0 비율 (좌상단 기준)
  y: number;
  width: number;      // 0.0~1.0 비율
  height: number;
  pixel_size: number; // 모자이크 격자 크기 (px)
  mode: MosaicMode;
  blur_radius: number; // mode="blur"일 때 적용 (px)
  start_sec: number;
  end_sec: number | null;
  source_id?: string;
}

// ── Speed Lines Overlay (집중선/속도선) ──────────────────────────────────
// 만화/웹툰 특유의 집중선 — 중심점에서 사방으로 뻗는 날카로운 쐐기 선들로 박진감을 준다.
// 백엔드가 Pillow 로 고해상도 PNG 를 프로시저럴 생성(파라미터+해상도 해시 캐시)해 합성한다.
// 적용 순서: 모든 오버레이 중 최상단(그래픽 오버레이 다음).
export interface SpeedLinesOverlay {
  id: string;
  color: string;         // #RRGGBB (기본 검정)
  density: number;       // 선 개수(밀도)
  center_x: number;      // 0.0~1.0 (중심점)
  center_y: number;
  inner_ratio: number;   // 중앙 빈 타원 영역 반경 (영역 반변 대비 비율, area 비율대로 타원)
  thickness: number;     // 바깥쪽 선 굵기 배수 (1.0=표준)
  width_variance?: number; // 선별 굵기 랜덤 편차 (0=균일, 0.2=±20%). 기본 0.2
  jitter: number;        // 각도 랜덤 지터 (0=균등, 1=손그림)
  seed: number;          // 재현용 시드 (같은 시드+파라미터면 같은 그림)
  supersample: number;   // 캔버스 배율 (2~3)
  // 표시 영역 크기(비율). (center_x, center_y) 중심의 area_w×area_h 사각형 안에만 방사.
  // 기본 1.0=전체 화면. 값이 작을수록 좁은 영역에만 집중선이 나타난다.
  area_w?: number;       // 0.0~1.0 (기본 1.0)
  area_h?: number;       // 0.0~1.0 (기본 1.0)
  opacity: number;       // 0.0~1.0
  start_sec: number;
  end_sec: number | null;
  // 선별 페이드 인/아웃 반복 애니메이션. animate=true(기본)면 각 선이 최대 길이로 고정된 채
  // 무작위 타이밍(seed 기반 위상)으로 깜빡인다(페이드 인→아웃→다시 인). 선 길이 성장 폐기.
  animate?: boolean;      // 기본 true (필드 없는 기존 씬도 애니 on)
  num_buckets?: number;   // 페이드 위상 버킷 개수. 기본 8
  start_scale?: number;   // (deprecated) 과거 scale 방식 잔존 필드. 미사용.
  anim_speed?: number;    // 페이드 속도 배수. 기본 1.0
  anim_period_sec?: number; // 한 페이드 사이클 기준 길이(초). 기본 0.7
  enter_effect?: NonTextEnterExitEffect;
  exit_effect?: NonTextEnterExitEffect;
  enter_duration_sec?: number;
  exit_duration_sec?: number;
  source_id?: string;
}

// ── Graphic Overlay (#205) ────────────────────────────────────────────────
// UI/감정용 그래픽 오버레이. 1차 구현 범위: spotlight + alert.
// target/reaction/smell은 모델만 받고 렌더링은 후속 사이클에서 추가한다.
// #288 — "emoji" 타입 추가: 이모지 아이콘 4종 (question/lens/prohibit/poo).
export type GraphicOverlayType =
  | "spotlight"
  | "target"
  | "alert"
  | "reaction"
  | "smell"
  | "emoji"
  | "dash";

// #288 — emoji 그래픽 오버레이가 사용하는 4종 아이콘 키.
export type EmojiIconType = "question" | "lens" | "prohibit" | "poo";

// #288 — 아이콘 키 → 표시 이모지 + 한글 라벨 매핑.
// 백엔드 drawtext와 프론트 캔버스 미리보기 모두 이 매핑을 통해 동일한 글리프를 렌더한다.
export const EMOJI_ICON_TABLE: Record<EmojiIconType, { emoji: string; label: string }> = {
  question: { emoji: "❓", label: "물음표" },
  lens: { emoji: "🔍", label: "돋보기" },
  prohibit: { emoji: "🚫", label: "금지" },
  poo: { emoji: "💩", label: "똥" },
};

export interface GraphicOverlay {
  id: string;
  type: GraphicOverlayType;
  start_sec: number;
  end_sec: number | null;
  x: number;        // 0.0~1.0 비율 (중심 기준)
  y: number;        // 0.0~1.0 비율 (중심 기준)
  params: Record<string, unknown>;
  // ── Scene Tag 시스템 (#215) ──────────────────────────────────────────────
  // 태그가 자동 추가한 오버레이를 출처 추적 위해 부여하는 옵셔널 ID 식별자.
  // 형식: "tag-{tagname}-graphic" (applySceneTags 부여 규칙).
  // 유저가 수동 추가한 오버레이는 undefined.
  source_id?: string;
  // ── SFX (#290) — 오버레이별 1개 SFX, enter(start_sec) 시점에 트리거 ────
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  // ── 등장/사라짐 효과 (#287) — optional + 기본값 처리 ─────────────────────
  enter_effect?: NonTextEnterExitEffect;
  exit_effect?: NonTextEnterExitEffect;
  enter_duration_sec?: number;
  exit_duration_sec?: number;
}

// ── Scene Filter (#206) ───────────────────────────────────────────────────
// 씬 전체 색감/무드 필터. 1차 구현 범위: flashback/horror/vivid/nightvision 4종.
// pov는 모델만 받고 렌더링은 후속 사이클(Pillow radial mask + 키프레임).
// 적용 순서: cut_clip → filters → text_overlays → shape_overlays → graphic_overlays
export type SceneFilterType =
  | "flashback"
  | "horror"
  | "vivid"
  | "nightvision"
  | "pov"
  | "warm"
  | "cool"
  | "cartoon";

export interface SceneFilter {
  filter_type: SceneFilterType;
  intensity: number;  // 0.0 ~ 1.0
  params: Record<string, unknown>;
  // ── Scene Tag 시스템 (#215) ──────────────────────────────────────────────
  // 태그가 자동 추가한 필터를 출처 추적 위해 부여하는 옵셔널 ID 식별자.
  // 형식: "tag-{tagname}-filter" (applySceneTags 부여 규칙).
  // 유저가 수동 추가한 필터는 undefined.
  source_id?: string;
}

// ── Scene Motion (#207) ───────────────────────────────────────────────────
// 씬 내 화면 다이나믹 효과 (셰이크 / clip 줌 / 슬로우 모션).
// 적용 순서: cut_clip → filters → motions → text/shape/graphic overlays.
// 1차 구현 범위: shake / clip_zoom / stutter 실제 렌더링 (영상 길이 변경 없음).
// stutter(#347): 끊김/점프 모션. params.intensity(0.0~1.0)로 강도 조절.
// slowmo는 모델만 받고 렌더링은 후속 사이클 (#207 후속) — narration_timeline 재계산 필요.
export type MotionType = "shake" | "slowmo" | "clip_zoom" | "stutter";

export interface SceneMotion {
  motion_type: MotionType;
  start_sec: number;     // 씬 내 상대 시작 시각
  duration_sec: number;  // 효과 지속 시간
  params: Record<string, unknown>;
  // ── Scene Tag 시스템 (#215) ──────────────────────────────────────────────
  // 태그가 자동 추가한 모션을 출처 추적 위해 부여하는 옵셔널 ID 식별자.
  // 형식: "tag-{tagname}-motion" (applySceneTags 부여 규칙).
  // 유저가 수동 추가한 모션은 undefined.
  source_id?: string;
}

export interface MatchMeta {
  chunk_id: string;
  source_file: string;
  trim_start: number;
  trim_end: number;
  match_score: number;
  match_reason: string;
}

export interface TtsMeta {
  audio_url: string;
  duration_sec: number;
  status: "idle" | "generating" | "ready" | "error";
  synced: boolean;
}

// ── #382 — NarrationSegment ──────────────────────────────────────────────
// 씬 1개에 여러 TTS 구간(N:1) 을 가지기 위한 단위.
// 첫 segment("seg_0")만 씬 길이를 자동 연장하고, 그 외 segment 는 offset_sec 기준으로 배치되며
// 씬 길이를 넘어가는 경우 UI 에 빨간 마커로 표시 + 렌더 시 씬 경계에서 잘림.
export interface NarrationSegment {
  id: string;                         // "seg_0", "seg_1" ... 또는 uuid
  text: string;
  offset_sec: number;                  // 씬 시작 기준 시작 시점
  audio_url: string;                   // /api/projects/{pid}/tts/segment/{seg_id}/audio
  duration_sec: number;                // TTS 실제 길이
  status: "idle" | "generating" | "ready" | "error";
  synced: boolean;                     // 현재 text 기준으로 TTS 생성됐는지
  voice_name?: string | null;          // segment 별 voice override (null/undefined = 프로젝트 기본)
  sbv_text?: string | null;            // 자막용 별도 텍스트 (null/undefined = text 사용)
  /** #392 — TTS 생성 실패 시 사용자에게 표시할 에러 메시지. status="error" 일 때만 의미 있음. */
  error_message?: string | null;
}

/**
 * 씬 편집 지시 코멘트.
 *
 * 상태 전이는 open → done 단방향이며, 완료돼도 삭제하지 않고 이력으로 남긴다.
 * resolved_* 필드는 status === "done" 일 때만 값이 있다.
 */
/** 화자 1명 — 씬 태그 `speaker:{name}` 이 이 이름을 가리키면 voice_name 으로 TTS 를 만든다. */
export interface Speaker {
  name: string;
  voice_name: string;
  provider?: string | null;
  note?: string;
}

/** GET /speakers 응답 — 사용 현황과 오타(미등록) 이름을 함께 준다. */
export interface SpeakersResponse {
  speakers: (Speaker & { scene_ids: number[] })[];
  default_voice: string;
  unknown_speakers: { name: string; scene_ids: number[] }[];
}

/** 편집 코멘트에 붙인 참조 이미지. 본문의 `[이미지N]` 과 index 로 대응한다. */
export interface CommentImage {
  id: string;
  /** 본문 [이미지N] 의 N (1-base). */
  index: number;
  filename: string;
  /** 에이전트가 바로 읽는 서버 절대경로. */
  path: string;
  /** 브라우저 표시용 API URL. */
  url: string;
  mime: string;
  size: number;
  created_at: string;
}

export interface SceneComment {
  id: string;
  body: string;
  status: "open" | "done";
  created_at: string;
  author: string;
  /** 붙임 이미지 목록. 본문 자리표시자와 index 로 매칭. */
  images?: CommentImage[];
  resolved_at?: string | null;
  resolved_by?: "agent" | "user" | null;
  /** 에이전트가 무엇을 고쳤는지 한 줄 요약. */
  resolution_note?: string | null;
}

export interface SceneCard {
  scene_id: number;
  title: string;
  description: string;
  mood: string;
  keywords: string[];
  duration_hint_sec: number;
  narration_text: string;
  // #stale-write — narration_text 의 서버측 편집 리비전.
  // 클라이언트는 이 값을 해석하지 않고 읽은 그대로 되돌려보내기만 한다.
  // 서버는 자기 값보다 낮은 리비전의 대본 덮어쓰기를 거부한다(옛 스냅샷 유실 방지).
  narration_rev?: number;
  // 자막용 텍스트(SBV) 별도 지정. null/빈 문자열이면 narration_text를 그대로 사용.
  subtitle_text?: string | null;
  footage: EffectConfig | null;
  match: MatchMeta | null;
  tts: TtsMeta | null;
  text_overlays: TextOverlay[];
  footage_speed?: number;
  /** 재생 속도 모드 — constant(기본, 전 구간 footage_speed) / ramp(정속 → 선형 가속 → footage_speed 유지).
   *  수학: lib/speedRamp.ts (백엔드 src/utils/speed_ramp.py 와 동일 공식). */
  footage_speed_mode?: "constant" | "ramp" | "ramp_down";
  /** ramp/ramp_down 기본 배속 — 가속의 시작 / 감속의 끝 속도 (기본 1.0) */
  footage_speed_ramp_base?: number;
  /** ramp 정속 구간 비율 (원본 클립 대비, 0~1, 기본 0.2). */
  footage_speed_ramp_hold?: number;
  /** ramp 가속 구간 비율 (원본 클립 대비, 0~1, 기본 0.3). hold+accel<=1 로 클램프. */
  footage_speed_ramp_accel?: number;
  /** 씬 footage 원본 오디오 음량 (0=음소거, 0.3=기본 더킹, 1.0=풀). */
  footage_audio_volume?: number;
  /** 씬별 TTS 재생 속도 (1.0=원본). Chirp3-HD 는 API speakingRate 를 무시하므로
   *  렌더/미리보기 시점에 atempo 필터로 후처리해 속도를 변경한다.
   *  권장 범위 0.5~2.0. 그 밖의 값은 build_merged_wav 에서 자동 체이닝. */
  tts_speed?: number;
  // #tts-emotion — 씬별 TTS 말투/감정 지시문(자연어).
  // 예: "신나고 들뜬 목소리로". 빈 값이면 기본 톤.
  tts_emotion?: string;
  shape_overlays?: ShapeOverlay[];
  // ── Graphic Overlay (#205) ──────────────────────────────────────────────
  // 기존 씬 카드(graphic_overlays 미존재)와 호환되도록 optional + undefined 허용.
  graphic_overlays?: GraphicOverlay[];
  // ── Scene Filter (#206) ─────────────────────────────────────────────────
  // 씬 전체 색감/무드 필터. 기존 씬 카드(filters 미존재)와 호환.
  filters?: SceneFilter[];
  // ── Scene Motion (#207) ─────────────────────────────────────────────────
  // 씬 다이나믹 효과 (shake/clip_zoom/slowmo). 기존 씬 카드(motions 미존재)와 호환.
  // 1차 구현 범위: shake/clip_zoom 렌더링. slowmo는 모델만 (후속).
  motions?: SceneMotion[];
  // ── Mosaic Overlay (#264) ───────────────────────────────────────────────
  // 영역 모자이크/블러 오버레이. 기존 씬 카드(mosaic_overlays 미존재)와 호환.
  // 적용 순서: filters → motions → mosaic_overlays → image_overlays → text/shape/graphic overlays.
  mosaic_overlays?: MosaicOverlay[];
  // ── Image Overlay ────────────────────────────────────────────────────────
  // footage 스튜디오의 이미지 자산을 씬 위에 합성하는 오버레이. 빈 배열이 기본.
  // 기존 씬 카드(image_overlays 미존재)와 호환.
  // 적용 순서: filters → motions → mosaic_overlays → image_overlays → text/shape/graphic overlays.
  image_overlays?: ImageOverlay[];
  // ── Speed Lines Overlay (집중선) ─────────────────────────────────────────
  // 만화 집중선 오버레이. 기존 씬 카드(speed_lines 미존재)와 호환.
  // 적용 순서: 모든 오버레이 중 최상단(그래픽 오버레이 다음).
  speed_lines?: SpeedLinesOverlay[];
  // ── Scene SFX — 씬 본체에 붙는 1개 SFX.
  // 트리거 시점 = 씬 영상 시작 + sfx_offset_sec.
  // 기존 씬 카드(필드 없음)와 회귀 안전.
  sfx_asset_id?: string | null;
  sfx_volume?: number;       // 0.0~1.0
  sfx_offset_sec?: number;   // 씬 시작부터 SFX 트리거까지의 지연(초)
  // ── "삐 소리" 씬 단위 자동 효과 ────────────────────────────────────────────
  // true 면 이 씬의 마지막 활성 TTS narration segment 가 끝나는 시점에 비프음을 이어붙인다.
  // 비프 시작 이후에는 그 씬의 TTS 가 더 들리지 않는다("이어붙이기" 방식, 검열/자르기 아님).
  beep_on_narration_end?: boolean;
  beep_volume?: number;      // 0.0~1.0 (기본 0.7)
  // ── "중간 이미지 삽입" 씬 마커 ──────────────────────────────────────────────
  // true 면 사용자가 "중간 이미지 삽입" 원클릭 액션으로 자동 생성한 이미지 전용 삽입 씬.
  // 전체화면 이미지 오버레이 + 짧은 지속시간 + narration 없음. 프론트가 단순 요소로 취급.
  is_insert_image_scene?: boolean;
  // ── Scene Tag 시스템 (#208) ─────────────────────────────────────────────
  // 씬 카드 태그. 태그 추가 시 effects 자동 머지, 제거 시 해당 태그가 추가한 효과만 제거.
  // 사전은 `config/scene_tags.json` (정적 import).
  // 1차 구현 범위: 데이터 모델 + 빌트인 사전 + applySceneTags 엔진만.
  // UI/시나리오 파서/유저 정의 태그/백엔드 동기화는 후속 사이클.
  tags?: string[];
  // ── 씬-TTS N:1 매칭 (#382) ────────────────────────────────────────────
  // 마이그레이션 자동(백엔드 로드 시 narration_text → seg_0 자동 변환).
  // 새 프로젝트/마이그레이션된 프로젝트 모두 동일하게 narration_segments 만 참조하면 된다.
  narration_segments?: NarrationSegment[];
  // ── 편집 지시 코멘트 (씬 단위) ────────────────────────────────────────────
  // 김감독이 씬에 남기는 편집 지시. 클로드 에이전트가 open 코멘트를 읽고 수정 후
  // resolve 로 완료 처리하면 done 이력으로 남는다.
  // 기존 씬 카드(comments 미존재)와 호환되도록 optional.
  comments?: SceneComment[];
}

// ── #289 — 후처리 BGM 트랙 ────────────────────────────────────────────────
// 렌더링이 끝난 final_output.mp4 위에 다중 트랙으로 합성된다. 트랙 간 겹침은 허용 (믹스).
// audio_asset_id는 studio_assets 내 asset_type="audio" 자산을 참조한다.
export interface BgmTrack {
  id: string;
  audio_asset_id: string;
  start_sec: number;     // 영상 타임라인 기준 BGM 시작 시각
  end_sec: number;       // 영상 타임라인 기준 BGM 종료 시각 (>= start_sec)
  fade_in_sec: number;
  fade_out_sec: number;
  volume: number;        // 0.0~1.0 (기본 0.4 권장 — TTS와 공존)
}

export interface Project {
  project_id: string;
  name: string;
  // 수동 시리즈 그룹명(홈 리스트 그룹핑). 빈 문자열/미지정이면 "시리즈 미편성" 그룹.
  // 옵셔널로 두어 기존 IndexedDB Project 데이터와 호환.
  series?: string;
  footage_dir: string;
  scenario_text: string;
  status: string;
  scenes: SceneCard[];
  tts_voice: string;
  /** 다중 화자 사전. 씬 태그 `speaker:{name}` 이 여기를 참조한다. */
  speakers?: Speaker[];
  created_at: string;
  updated_at: string;
  studio_assets: FootageAsset[];
  output_width: number;
  output_height: number;
  // 프로젝트 전역 배경색(letterbox 여백). "#RRGGBB" 또는 named("black"). 기본 "black".
  bg_color?: string;
  default_crop?: CropRect | null;
  // ── #289 — 후처리 BGM 트랙 (옵셔널, 기존 프로젝트 호환) ─────────────────
  bgm_tracks?: BgmTrack[];
  // ── 스플릿 씬 디졸브 강제 적용 옵션 ────────────────────────────────────────
  // true면 같은 source_file을 공유하는 인접 씬(분할 스플릿 씬) 사이에도 일반 dissolve
  // 효과를 강제 적용한다. false(기본)는 clip_editor의 잔상 방지 안전망이 작동해
  // 스플릿 씬 경계 dissolve를 0초로 처리한다.
  force_split_scene_dissolve?: boolean;
  // ── #355 — 프로젝트 단위 Gemini 모델 선택 ───────────────────────────────
  // 자연어 편집(nl_editor) · 자동 씬 분할 · 자동 오버레이 검출 등 Gemini 호출에
  // 사용할 모델 ID. 기본값 "gemini-2.5-flash". 빈 문자열이면 백엔드가 default_model
  // 로 폴백한다. 옵셔널로 두어 기존 IndexedDB Project 데이터와 호환 유지.
  gemini_model?: string;
}

export interface FootageChunk {
  chunk_id: string;
  start_sec: number;
  end_sec: number;
  description: string;
  // 자산 카드에서 뱃지로 표시될 검색 키워드. 미인덱싱 청크는 빈 배열.
  keywords?: string[];
  quality_score: number;
}

export interface FootageItem {
  filename: string;
  path: string;
  thumbnail_url: string;
  duration_sec: number;
  // #289/#290 — audio 타입 추가 (BGM/SFX 자산).
  file_type: "video" | "image" | "audio";
  chunks: FootageChunk[];
}

export type MatchingMethod =
  | "gemini_filename"
  | "gemini_content"
  | "keyword_filename"
  | "manual"
  | "label";

export interface CropRect {
  x: number;  // 0.0~1.0
  y: number;
  w: number;
  h: number;
}

export interface FootageAsset {
  asset_id: string;
  filename: string;
  label: string;
  // #289/#290 — audio 타입 자산 (BGM/SFX 공용) 추가.
  asset_type: "video" | "image" | "audio";
  source: "recorded" | "split" | "presentation" | "imported";
  created_at: string;
  crop?: CropRect | null;
  // ── 로컬 파일 출처 표시 (#272) ────────────────────────────────────────────
  // origin === "local" 인 경우 썸네일을 서버 API가 아닌 브라우저(File→video→canvas)로 생성한다.
  // 기존 데이터(undefined)와 명시적 "server"는 모두 서버 썸네일 API 경로를 사용한다 (회귀 안전).
  // localFile은 브라우저 메모리 전용이며 IndexedDB에 직렬화되지 않는다 (런타임에만 부여).
  origin?: "local" | "server";
  localFile?: File;
  // ── 분할 출처 추적 / 지점 편집 코멘트 (백엔드 FootageAsset 과 동일) ────────
  split_from?: string;
  has_splits?: boolean;
  markers?: FootageMarker[];
  // 푸티지 전체에 대한 자유 메모 (구간 분할 화면에서 편집).
  note?: string;
}

/** 푸티지 타임라인의 특정 시각에 꽂는 편집 지시 코멘트 (구간 분할 화면). */
export interface FootageMarker {
  marker_id: string;
  time_sec: number;
  comment: string;
  created_at: string;
}

export interface SplitPoint {
  start: number;
  end: number;
  label: string;
}

export interface AppSettings {
  google_api_key: string;
  gemini_request_interval_sec: string;
  gemini_max_retry: string;
  tts_voice_name: string;
  tts_speaking_rate: string;
  gcp_key_configured: boolean;
}

// WebSocket messages
export type WsMessage =
  | { type: "progress"; task_id: string; phase: string; current: number; total: number; message: string; item: string }
  | { type: "tts_done"; task_id: string; scene_id: number; audio_url: string; duration_sec: number }
  | { type: "match_done"; task_id: string; scene_id: number; matched_scene: SceneCard }
  | { type: "complete"; task_id: string; summary: Record<string, number> }
  | { type: "error"; task_id: string; code: string; message: string; scene_id?: number }
  | { type: "ping" };

// ── Pydantic 자동 생성 타입 re-export (#304) ──────────────────────────────
// `web.backend.models` / `src.render_spec.models` 의 Pydantic 모델로부터
// `generated.ts` 가 자동 생성된다. 신규 도입되는 RenderSpec 계열은 처음부터
// generated.ts 를 단일 진실로 사용하기 위해 여기서 re-export 한다.
//
// 기존 수동 정의 타입(SceneCard/Project/FootageItem 등)은 호환을 위해 그대로 두며,
// 후속 PR에서 generated.ts 의 동명 타입으로 점진 교체 예정.
// 점진 교체 시 import 경로(`@/types`)는 그대로 유지되므로 호출 측 변경이 최소화된다.
export type {
  RenderSpec,
  Canvas as RenderCanvas,
  Size as RenderSize,
  CropBox,
  VideoLayer,
  ImageLayer,
  TextLayer,
  ShapeLayer,
  GraphicLayer,
  MosaicLayer,
  FilterLayer,
  MotionLayer,
} from "./generated";
