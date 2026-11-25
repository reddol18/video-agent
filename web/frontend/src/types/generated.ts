// ────────────────────────────────────────────────────────────────────────
// 자동 생성 파일 (#304) — 직접 수정 금지
// 원본: web/backend/models.py + src/render_spec/models.py (Pydantic v2)
// 재생성: `npm run gen-types` 또는 `uv run python scripts/gen_ts_types.py`
// ────────────────────────────────────────────────────────────────────────

/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

/**
 * 후처리 BGM 렌더링 요청 (#289).
 *
 * Project.bgm_tracks와 final_output.mp4를 입력으로 받아 final_with_bgm.mp4를 생성한다.
 */
export interface BgmRenderRequest {}
/**
 * 후처리 전용 BGM 트랙 (#289).
 *
 * 렌더링이 끝난 final_output.mp4 위에 다중 트랙으로 합성된다. 트랙 간 겹침은 허용 (믹스).
 * audio_asset_id는 studio_assets 내 asset_type="audio" 항목을 가리킨다.
 */
export interface BgmTrack {
  id: string;
  audio_asset_id: string;
  start_sec?: number;
  end_sec?: number;
  fade_in_sec?: number;
  fade_out_sec?: number;
  volume?: number;
}
/**
 * 최종 렌더 캔버스(=프로젝트 출력 해상도).
 */
export interface Canvas {
  width: number;
  height: number;
  fps?: number;
  bg_color?: string;
}
export interface ClipEffect {
  type?: "clip";
  file: string;
  x_offset?: number;
  y_offset?: number;
  fit?: "contain" | "cover" | "stretch";
  rotation?: 0 | 90 | 180 | 270;
  clip_start_sec?: number | null;
  clip_end_sec?: number | null;
}
/**
 * 라이브러리 클립 1개의 메타. clip.json 의 직렬화 스키마이자 API 응답 모델.
 */
export interface ClipLibraryEntry {
  clip_id: string;
  title: string;
  category?: string;
  filename: string;
  source_url?: string;
  duration_sec?: number;
  width?: number;
  height?: number;
  size_bytes?: number;
  created_at?: string;
}
/**
 * URL → 클립 등록 요청 본문 (POST /api/clip-library).
 */
export interface ClipLibraryRegisterRequest {
  url: string;
  title?: string;
  category?: string;
}
/**
 * 편집 코멘트에 붙인 참조 이미지 1건.
 *
 * 코멘트 본문에는 `[이미지1]` 같은 자리표시자로 등장하고, 실제 파일은
 * web/data/{project_id}/comment_images/ 에 저장된다. `index` 가 자리표시자
 * 번호(1-base)이며, 에이전트는 이 번호로 본문 ↔ 파일을 매칭한다.
 */
export interface CommentImage {
  id: string;
  index: number;
  filename: string;
  path: string;
  url: string;
  mime?: string;
  size?: number;
  created_at?: string;
}
export interface CopyImgOptions {
  input: string;
  output: string;
  extra?: {
    [k: string]: unknown;
  };
}
/**
 * 씬에 신규 narration segment 추가.
 *
 * id 가 지정되면 그 id 를 사용하고(중복 시 400), 미지정이면 seg_{n} 형식으로 자동 부여.
 * offset_sec/text/voice_name/sbv_text 는 옵셔널이며 미지정 시 기본값 사용.
 */
export interface CreateNarrationSegmentRequest {
  id?: string | null;
  text?: string;
  offset_sec?: number;
  voice_name?: string | null;
  sbv_text?: string | null;
}
/**
 * 템플릿에서 새 프로젝트 생성 요청 본문 (POST /api/projects/from-template).
 */
export interface CreateProjectFromTemplateRequest {
  template_id: string;
  title?: string;
}
export interface CreateProjectRequest {
  name: string;
  footage_dir?: string;
  scenario_text?: string;
  auto_footage_dir?: boolean;
}
/**
 * 프로젝트 → 템플릿 변환 요청 본문 (POST /api/templates).
 */
export interface CreateTemplateRequest {
  source_project_id: string;
  title: string;
  description?: string;
}
/**
 * 소스(스케일 적용 *후*)에서 잘라낼 사각 영역. 좌상단 기준 픽셀.
 *
 * `scale_x/scale_y`를 곱해 키워진 결과 위에서 `(x, y, w, h)` 만큼 잘라낸다.
 * 예: 9:16 캔버스에 16:9 원본을 cover로 채우면 scale로 양쪽이 캔버스 높이만큼 늘어나고
 * 그 위에서 가운데 9:16 영역을 잘라내게 된다.
 */
export interface CropBox {
  x: number;
  y: number;
  w: number;
  h: number;
}
export interface EditEffect {
  type?: "edit";
  description: string;
  file: string;
  min_duration?: number;
  preview_placeholder?: boolean;
}
/**
 * 효과 1종의 매니페스트.
 *
 * 각 카테고리(text/shape/graphic/filter/motion/mosaic) 내부에서 효과 1개를 식별한다.
 *
 * - `name`: 카테고리 내부의 식별자(예: "rect", "ellipse", "spotlight", "shake").
 *   카테고리가 다르면 같은 name이 있어도 무방하다(`EFFECT_REGISTRY` 키는 `category:name` 조합).
 * - `display_label`: UI 버튼에 표시되는 한글 라벨. "+" 같은 prefix는 EffectAddPanel 측에서 붙인다.
 * - `category`: 6개 카테고리 중 하나.
 * - `params`: 파라미터 명세. 빈 dict면 별도 옵션 없는 효과(예: + 사각형 버튼).
 * - `preview_supported`: 프론트가 즉시 미리보기를 그릴 수 있는가. 백엔드 렌더만 가능하면 False.
 * - `backend_layer_type`: 이 효과가 RenderSpec 상에서 어떤 Layer 로 매핑되는지 hint.
 *   ("video"/"image"는 footage이므로 여기서는 등장하지 않는다.)
 * - `tooltip`: UI 툴팁(있으면).
 * - `notes`: 개발자용 비고(있으면). UI에는 표시되지 않는다.
 *
 * 본 모델에는 *데이터만* 둔다. 실제 변환·합성 콜백(``register_render_spec_handler`` /
 * ``register_preview_handler`` 로 등록)은 ``_handlers.PY_EFFECT_HANDLERS_*`` dict 가
 * 카테고리 단위로 보관한다 — Pydantic 모델에 callable 을 직접 두면 JSON 직렬화 / TS
 * 자동 생성이 깨지기 때문에 분리한 것이다.
 */
export interface EffectDefinition {
  name: string;
  display_label: string;
  category: "text" | "shape" | "graphic" | "filter" | "motion" | "mosaic" | "image";
  params?: {
    [k: string]: ParamSpec;
  };
  preview_supported?: boolean;
  backend_layer_type?: ("text" | "shape" | "graphic" | "mosaic") | null;
  tooltip?: string;
  notes?: string;
}
/**
 * 효과 파라미터 1개의 타입/범위/기본값 명세.
 *
 * Pydantic v2 모델이므로 `pydantic2ts` 가 자동으로 TS 타입으로 변환한다.
 * 값 자체는 `default` 한 필드로 일원화한다(`int|float|str|bool` 유니온).
 * 프론트는 이 정보를 읽어 입력 위젯(슬라이더/체크박스/셀렉트)을 만들 수 있다.
 */
export interface ParamSpec {
  kind: "int" | "float" | "str" | "bool" | "enum";
  label: string;
  default: number | string | boolean;
  min?: number | null;
  max?: number | null;
  options?: string[] | null;
  description?: string;
}
export interface ErrorMsg {
  type?: "error";
  task_id: string;
  code: string;
  message: string;
  scene_id?: number | null;
}
/**
 * 씬 전체 색감/무드 필터 (#206 SceneFilter 의 RenderSpec 표현).
 *
 * builder 가 ``scene.filters`` 항목을 그대로 보존해 layers 에 끼워 넣는다. 클라이언트는
 * 이 레이어가 한 개라도 있으면 단순 미리보기를 포기하고 백엔드 렌더로 폴백한다 (#343).
 *
 * 설계 메모:
 * - ``intensity`` / ``params`` 는 백엔드 ``clip_editor.apply_scene_filters`` 가 해석하는
 *   값과 그대로 매칭되도록 둔다. RenderSpec 단계에서 별도 변환은 하지 않는다 (추상 키워드
 *   금지 원칙 위배가 아닌가? — 색감 필터는 "키워드 → 픽셀"이 1:1 의미를 갖지 않는 정성적
 *   효과라 키워드 자체가 단일 진실이다. fit/motion 같은 *기하* 변환만 픽셀 환원 대상).
 * - ``filter_type`` 은 ``SceneFilter.filter_type`` Literal 과 동일 집합을 유지한다.
 */
export interface FilterLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "filter";
  filter_type: "flashback" | "horror" | "vivid" | "nightvision" | "pov" | "warm" | "cool";
  intensity?: number;
  params?: {
    [k: string]: unknown;
  };
}
export interface FootageAsset {
  asset_id: string;
  filename: string;
  label?: string;
  asset_type: "video" | "image" | "audio";
  source: "recorded" | "split" | "presentation" | "imported";
  created_at?: string;
  split_from?: string;
  has_splits?: boolean;
  markers?: FootageMarker[];
  note?: string;
}
/**
 * 푸티지 타임라인의 특정 시각에 꽂는 편집 지시 코멘트 (구간 분할 화면).
 */
export interface FootageMarker {
  marker_id: string;
  time_sec: number;
  comment?: string;
  created_at?: string;
}
export interface FootageChunk {
  chunk_id: string;
  start_sec: number;
  end_sec: number;
  description?: string;
  keywords?: string[];
  quality_score?: number;
}
export interface FootageItem {
  filename: string;
  path: string;
  thumbnail_url: string;
  duration_sec: number;
  file_type: "video" | "image" | "audio";
  chunks?: FootageChunk[];
}
/**
 * 특정 segment 의 TTS 생성/재생성. force=True 면 synced 상관없이 재호출.
 */
export interface GenerateNarrationSegmentRequest {
  force?: boolean;
  voice_name?: string | null;
}
export interface GenerateTtsRequest {
  scene_ids?: number[] | null;
  force?: boolean;
  voice_name?: string | null;
}
export interface GradientConfig {
  angle?: number;
  stops?: GradientStop[];
}
export interface GradientStop {
  color: string;
  position: number;
}
/**
 * 그래픽 오버레이(spotlight/target/alert/...). 좌표는 캔버스 픽셀(중심 기준).
 */
export interface GraphicLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "graphic";
  graphic_type: "spotlight" | "target" | "alert" | "reaction" | "smell" | "emoji" | "dash";
  center_x: number;
  center_y: number;
  params?: {
    [k: string]: unknown;
  };
}
export interface GraphicOverlay {
  id: string;
  type: "spotlight" | "target" | "alert" | "reaction" | "smell" | "emoji" | "dash";
  start_sec?: number;
  end_sec?: number | null;
  x?: number;
  y?: number;
  params?: {
    [k: string]: unknown;
  };
  source_id?: string | null;
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  enter_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  exit_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  enter_duration_sec?: number;
  exit_duration_sec?: number;
}
/**
 * 그리드 셀 광원 그라데이션 보더 설정.
 *
 * animate=True 이면 재생 중 광원(hue)이 회전하며 살아있는 느낌을 준다.
 * 모든 필드는 옵셔널 기본값 — 보더 미지정 셀은 기존 무보더 동작과 동일하게 렌더된다.
 */
export interface GridBorder {
  width_px?: number;
  color_from?: string;
  color_to?: string;
  animate?: boolean;
  speed?: number;
}
/**
 * N×M 그리드의 셀 하나. file 이 비면 배경색만 채워진다.
 */
export interface GridCell {
  file?: string;
  border?: GridBorder | null;
}
/**
 * 이미지 + Ken Burns 줌인 레이어.
 */
export interface ImageLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "image";
  source: string;
  source_size: Size;
  scale_x: number;
  scale_y: number;
  crop?: CropBox | null;
  pad?: CropBox | null;
  zoom_from?: number;
  zoom_to?: number;
}
/**
 * 가로/세로 픽셀 크기.
 */
export interface Size {
  width: number;
  height: number;
}
export interface ImageOverlay {
  id: string;
  image_asset_id: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  opacity?: number;
  rotation_deg?: number;
  start_sec?: number;
  end_sec?: number | null;
  caption_text?: string | null;
  caption_font_size?: number;
  caption_color?: string;
  caption_bg_color?: string | null;
  caption_bg_opacity?: number;
  caption_padding?: number;
  enter_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  exit_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  enter_duration_sec?: number;
  exit_duration_sec?: number;
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  source_id?: string | null;
}
export interface IndexFootageRequest {
  force?: boolean;
}
export interface KenburnsCopyImgEffect {
  type?: "kenburns_copy_img";
  copy_img_options: CopyImgOptions;
  image: string;
}
export interface KenburnsEffect {
  type?: "kenburns";
  image: string;
  fit?: "cover" | "contain" | "stretch";
}
export interface KeywordMatchClientItem {
  scene_id: number;
  source_file: string;
  filename: string;
  chunk_id?: string;
  trim_start?: number;
  trim_end?: number;
  match_score?: number;
  is_image?: boolean;
}
export interface ManualMatchRequest {
  filename: string;
  trim_start?: number;
  trim_end?: number | null;
}
export interface MatchDoneMsg {
  type?: "match_done";
  task_id: string;
  scene_id: number;
  matched_scene: SceneCard;
}
export interface SceneCard {
  scene_id: number;
  title?: string;
  description?: string;
  mood?: string;
  keywords?: string[];
  duration_hint_sec?: number;
  narration_text?: string;
  narration_rev?: number;
  subtitle_text?: string | null;
  footage?:
    | ClipEffect
    | KenburnsEffect
    | KenburnsCopyImgEffect
    | EditEffect
    | SyncEffect
    | MultiscreenEffect
    | TitleGenEffect
    | null;
  match?: MatchMeta | null;
  tts?: TtsMeta | null;
  text_overlays?: TextOverlay[];
  footage_speed?: number;
  footage_speed_mode?: "constant" | "ramp" | "ramp_down";
  footage_speed_ramp_hold?: number;
  footage_speed_ramp_accel?: number;
  footage_speed_ramp_base?: number;
  footage_audio_volume?: number;
  tts_speed?: number;
  tts_emotion?: string;
  shape_overlays?: ShapeOverlay[];
  graphic_overlays?: GraphicOverlay[];
  filters?: SceneFilter[];
  motions?: SceneMotion[];
  mosaic_overlays?: MosaicOverlay[];
  image_overlays?: ImageOverlay[];
  speed_lines?: SpeedLinesOverlay[];
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  sfx_offset_sec?: number;
  beep_on_narration_end?: boolean;
  beep_volume?: number;
  is_insert_image_scene?: boolean;
  tags?: string[];
  narration_segments?: NarrationSegment[];
  comments?: SceneComment[];
}
export interface SyncEffect {
  type?: "sync";
  word: string;
  inner?: EditEffect | ClipEffect | null;
  preview_placeholder?: boolean;
}
export interface MultiscreenEffect {
  type?: "multiscreen";
  files?: string[];
  rows?: number | null;
  cols?: number | null;
  cells?: GridCell[] | null;
}
export interface TitleGenEffect {
  type?: "title_gen";
  options: TitleOptions;
}
export interface TitleOptions {
  text: string;
  mode?: string;
  duration?: number;
  font_path?: string | null;
  font_size?: number;
  text_color?: string;
  rect_color?: string | null;
  bg_color?: string;
  dissolve_time?: number;
  spc_word?: string | null;
}
export interface MatchMeta {
  chunk_id: string;
  source_file: string;
  trim_start: number;
  trim_end: number;
  match_score?: number;
  match_reason?: string;
}
export interface TtsMeta {
  audio_url: string;
  duration_sec: number;
  status?: "idle" | "generating" | "ready" | "error";
  synced?: boolean;
}
export interface TextOverlay {
  id: string;
  text: string;
  x?: number;
  y?: number;
  font_size?: number;
  font_family?: string;
  color?: string;
  gradient?: GradientConfig | null;
  bold?: boolean;
  italic?: boolean;
  outline_color?: string;
  outline_width?: number;
  glow_color?: string;
  glow_radius?: number;
  opacity?: number;
  align?: "left" | "center" | "right";
  start_sec?: number;
  end_sec?: number | null;
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  enter_effect?:
    | "none"
    | "fade"
    | "typing"
    | "zoom"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  exit_effect?:
    | "none"
    | "fade"
    | "typing"
    | "zoom"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  enter_duration_sec?: number;
  exit_duration_sec?: number;
  loop_animation?: "none" | "bounce" | "shake" | "pulse" | "wave" | "speedlines";
  loop_amplitude?: number;
  loop_period_sec?: number;
  text_speed_lines?: TextSpeedLinesConfig | null;
  countdown_from?: number;
  countdown_to?: number;
  countdown_interval_sec?: number;
  countdown_end_text?: string;
}
/**
 * 텍스트 마스킹 집중선(loop_animation="speedlines") 파라미터.
 *
 * 모두 선택값이며, 생략 시 렌더러가 씬 집중선과 동일한 기본값을 쓴다.
 * color 를 비우면 글자색을, center_x/center_y 를 비우면 글자 위치를 따라간다.
 */
export interface TextSpeedLinesConfig {
  color?: string | null;
  density?: number | null;
  center_x?: number | null;
  center_y?: number | null;
  depth?: number | null;
  mode?: ("tail" | "inner") | null;
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
export interface ShapeOverlay {
  id: string;
  shape: "rect" | "ellipse";
  x: number;
  y: number;
  width: number;
  height: number;
  color: string;
  opacity?: number;
  start_sec?: number;
  end_sec?: number | null;
  sfx_asset_id?: string | null;
  sfx_volume?: number;
  enter_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  exit_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  enter_duration_sec?: number;
  exit_duration_sec?: number;
}
export interface SceneFilter {
  filter_type: "flashback" | "horror" | "vivid" | "nightvision" | "pov" | "warm" | "cool" | "cartoon";
  intensity?: number;
  params?: {
    [k: string]: unknown;
  };
  source_id?: string | null;
}
export interface SceneMotion {
  motion_type: "shake" | "slowmo" | "clip_zoom" | "stutter";
  start_sec?: number;
  duration_sec?: number;
  params?: {
    [k: string]: unknown;
  };
  source_id?: string | null;
}
export interface MosaicOverlay {
  id: string;
  shape?: "rect" | "ellipse";
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  pixel_size?: number;
  mode?: "pixelate" | "blur";
  blur_radius?: number;
  start_sec?: number;
  end_sec?: number | null;
  source_id?: string | null;
}
export interface SpeedLinesOverlay {
  id: string;
  color?: string;
  density?: number;
  center_x?: number;
  center_y?: number;
  inner_ratio?: number;
  thickness?: number;
  width_variance?: number;
  jitter?: number;
  seed?: number;
  supersample?: number;
  area_w?: number;
  area_h?: number;
  opacity?: number;
  start_sec?: number;
  end_sec?: number | null;
  animate?: boolean;
  num_buckets?: number;
  start_scale?: number;
  anim_speed?: number;
  anim_period_sec?: number;
  enter_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  exit_effect?:
    | "none"
    | "fade"
    | "slide_in_left"
    | "slide_in_right"
    | "slide_in_top"
    | "slide_in_bottom"
    | "zoom"
    | "pop"
    | "pass_left_to_right"
    | "pass_right_to_left"
    | "pass_top_to_bottom"
    | "pass_bottom_to_top";
  enter_duration_sec?: number;
  exit_duration_sec?: number;
  source_id?: string | null;
}
export interface NarrationSegment {
  id: string;
  text?: string;
  offset_sec?: number;
  audio_url?: string;
  duration_sec?: number;
  status?: "idle" | "generating" | "ready" | "error";
  synced?: boolean;
  voice_name?: string | null;
  sbv_text?: string | null;
  error_message?: string | null;
}
export interface SceneComment {
  id: string;
  body: string;
  status?: "open" | "done";
  created_at?: string;
  author?: string;
  images?: CommentImage[];
  resolved_at?: string | null;
  resolved_by?: string | null;
  resolution_note?: string | null;
}
export interface MatchRequest {
  method: "gemini_filename" | "gemini_content" | "keyword_filename" | "manual" | "label";
  scene_ids?: number[] | null;
  model?: string | null;
}
/**
 * 영역 모자이크/블러. 좌상단 기준 픽셀.
 */
export interface MosaicLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "mosaic";
  shape?: "rect" | "ellipse";
  x: number;
  y: number;
  width: number;
  height: number;
  mode?: "pixelate" | "blur";
  pixel_size?: number;
  blur_radius?: number;
}
/**
 * 씬 내 화면 다이나믹 효과 (#207 SceneMotion 의 RenderSpec 표현).
 *
 * shake / clip_zoom 등 시간 변화가 있는 효과를 표현한다. 단순 미리보기에서는 해석할 수
 * 없으므로 ``isSimpleRenderSpec`` 이 자동으로 false 를 돌려준다 (#343).
 *
 * - ``motion_start_sec`` / ``motion_duration_sec`` 은 ``SceneMotion`` 의 ``start_sec`` /
 *   ``duration_sec`` 과 동일. 부모 ``_LayerBase.start_sec`` 는 *레이어 활성 구간*이며
 *   모션은 씬 전체에 걸쳐 활성이라 0 으로 둔다.
 */
export interface MotionLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "motion";
  motion_type: "shake" | "slowmo" | "clip_zoom" | "stutter";
  motion_start_sec?: number;
  motion_duration_sec?: number;
  params?: {
    [k: string]: unknown;
  };
}
export interface ParseScenarioRequest {
  scenario_text: string;
  force?: boolean;
}
export interface ProgressMsg {
  type?: "progress";
  task_id: string;
  phase: string;
  current: number;
  total: number;
  message: string;
  item?: string;
}
export interface Project {
  project_id: string;
  name: string;
  series?: string;
  footage_dir?: string;
  scenario_text?: string;
  status?: string;
  scenes?: SceneCard[];
  tts_voice?: string;
  speakers?: Speaker[];
  created_at?: string;
  updated_at?: string;
  studio_assets?: FootageAsset[];
  output_width?: number;
  output_height?: number;
  bg_color?: string;
  bgm_tracks?: BgmTrack[];
  cost_estimate?: {
    [k: string]: unknown;
  };
  force_split_scene_dissolve?: boolean;
  gemini_model?: string;
}
/**
 * 화자 1명 — 이름과 그 화자가 쓸 TTS 목소리.
 *
 * 씬 태그에 `speaker:{name}` 을 달면 그 씬의 TTS 가 이 목소리로 생성된다.
 * 이름은 태그에서 찾는 키이므로 프로젝트 안에서 유일해야 한다(대소문자 무시).
 */
export interface Speaker {
  name: string;
  voice_name: string;
  provider?: string | null;
  note?: string;
}
export interface RenderRequest {
  output_name?: string | null;
  skip_subtitle?: boolean;
  watermark?: boolean;
}
/**
 * 씬 1개에 해당하는 정규화된 렌더 명세.
 *
 * `scene_id`는 같은 프로젝트 내 씬을 구분하기 위해 보관한다. 캐시 파일에는 씬 리스트로
 * 저장된다.
 */
export interface RenderSpec {
  version?: number;
  scene_id: number;
  canvas: Canvas;
  duration_sec: number;
  layers?: (
    | VideoLayer
    | ImageLayer
    | TextLayer
    | ShapeLayer
    | GraphicLayer
    | MosaicLayer
    | FilterLayer
    | MotionLayer
  )[];
}
/**
 * 영상(또는 자른 영상) 레이어.
 *
 * - `source_size`: 원본 파일 해상도(픽셀). 회전 적용 후 가로/세로가 바뀐 값을 넣는다.
 *   즉, source_size는 항상 "캔버스에 그릴 직전의" 가시 영역 해상도다.
 * - `scale_x/scale_y`: 캔버스를 채우기 위한 단일 배율. cover/contain/stretch가 모두 이
 *   배율로 환원된다.
 * - `crop`: scale 적용 후 캔버스 내부에 그릴 영역. None이면 scale 결과를 그대로 사용.
 * - `playback_rate`: footage_speed가 그대로 매핑된다(1.0이면 원속도). fit과 절대로 묶지
 *   말 것 — 본 필드 분리가 #302 도입 동기.
 * - `rotation_deg`: 사용자가 수동으로 지정한 회전. source_size는 이미 회전 후 기준이므로
 *   파이프라인 매퍼는 이 값을 그대로 ffmpeg `transpose`/CSS `transform` 에 전달만 한다.
 */
export interface VideoLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "video";
  source: string;
  source_size: Size;
  scale_x: number;
  scale_y: number;
  crop?: CropBox | null;
  pad?: CropBox | null;
  playback_rate?: number;
  speed_mode?: "constant" | "ramp" | "ramp_down";
  speed_ramp_hold?: number;
  speed_ramp_accel?: number;
  speed_ramp_base?: number;
  rotation_deg?: 0 | 90 | 180 | 270;
}
/**
 * 자막/문구 텍스트 오버레이.
 *
 * 좌표는 캔버스 픽셀 기준. font_size는 px. color는 `#RRGGBB`로 정규화된다.
 * `position.x/y`는 텍스트 박스의 *중심* 위치다.
 */
export interface TextLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "text";
  text: string;
  position_x: number;
  position_y: number;
  font_size_px: number;
  font_family?: string;
  color?: string;
  align?: "left" | "center" | "right";
  bold?: boolean;
  italic?: boolean;
  outline_color?: string;
  outline_width_px?: number;
  enter_effect?: "none" | "fade" | "typing" | "zoom";
  exit_effect?: "none" | "fade" | "typing" | "zoom";
  enter_duration_sec?: number;
  exit_duration_sec?: number;
  loop_animation?: "none" | "bounce" | "shake" | "pulse" | "wave";
  loop_amplitude?: number;
  loop_period_sec?: number;
}
/**
 * 사각형/타원 오버레이. 좌상단 기준 픽셀.
 */
export interface ShapeLayer {
  start_sec?: number;
  end_sec?: number | null;
  opacity?: number;
  type?: "shape";
  shape?: "rect" | "ellipse";
  x: number;
  y: number;
  width: number;
  height: number;
  color?: string;
  enter_effect?: "none" | "fade";
  exit_effect?: "none" | "fade";
  enter_duration_sec?: number;
  exit_duration_sec?: number;
}
/**
 * 클라이언트에서 계산한 keyword_filename 매칭 결과 일괄 저장 요청.
 *
 * 백엔드는 각 항목을 store.update_scene으로 반영하며 별도의 Gemini 호출이 없으므로
 * 비용 누적은 발생하지 않는다 (기존 _match_keyword 동작과 동일).
 */
export interface SaveKeywordMatchRequest {
  items: KeywordMatchClientItem[];
}
/**
 * 라이브러리 클립을 씬에 지정하는 요청 본문.
 */
export interface SceneFromClipLibraryRequest {
  clip_id: string;
}
/**
 * 씬 지정 결과.
 *
 * `filename` 은 충돌 회피가 적용된 **확정** 파일명이다. 프론트는 반드시 이 값으로
 * footage 를 구성해야 한다 (라이브러리의 원본 filename 과 다를 수 있음).
 */
export interface SceneFromClipLibraryResponse {
  filename: string;
  duration_sec?: number;
  mode?: string;
}
/**
 * 화자 사전 전체 교체 (PUT). 부분 수정 대신 통째로 보낸다.
 */
export interface SetSpeakersRequest {
  speakers?: Speaker[];
}
export interface SetTtsVoiceRequest {
  voice_name: string;
}
export interface SplitPoint {
  start: number;
  end: number;
  label?: string;
}
export interface TaskCompleteMsg {
  type?: "complete";
  task_id: string;
  summary: {
    [k: string]: number;
  };
}
/**
 * 템플릿 상세 모델. template.json 의 직렬화 스키마.
 *
 * scenes 의 각 SceneCard 는 footage=None / match=None / tts=None / narration_segments[*].audio_url=""
 * 로 정제되어 저장된다. 새 프로젝트 생성 시 그대로 복제하면 안전.
 */
export interface Template {
  template_id: string;
  title: string;
  description?: string;
  source_project_id?: string;
  created_at?: string;
  scenes?: SceneCard[];
  output_width?: number;
  output_height?: number;
  tts_voice?: string;
  gemini_model?: string;
  bgm_tracks?: BgmTrack[];
  force_split_scene_dissolve?: boolean;
}
/**
 * 템플릿 목록(GET /api/templates) 응답용 메타 정보.
 */
export interface TemplateMeta {
  template_id: string;
  title: string;
  description?: string;
  created_at?: string;
  source_project_id?: string;
  scene_count?: number;
}
export interface TtsDoneMsg {
  type?: "tts_done";
  task_id: string;
  scene_id: number;
  audio_url: string;
  duration_sec: number;
}
export interface UpdateNarrationRequest {
  narration_text: string;
}
/**
 * 기존 segment 부분 편집. None 필드는 변경하지 않는다.
 */
export interface UpdateNarrationSegmentRequest {
  text?: string | null;
  offset_sec?: number | null;
  voice_name?: string | null;
  sbv_text?: string | null;
}
