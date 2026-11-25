# RenderSpec — 씬 카드 정규화 명세

> 도입 이슈: https://github.com/reddol18/video_agent/issues/302
> 도입일: 2026-05-29
> 담당: 노팀장

---

## 1. 도입 배경

씬 카드(`SceneCard`)를 백엔드(ffmpeg)와 프론트엔드(미리보기) 양쪽이 *각각* 해석하기 때문에 동일 입력이 두 곳에서 다르게 그려지는 회귀 버그가 반복 발생해왔다.

대표 사례 — `footage_speed != 1.0` 분기 안에서 `fit`이 강제로 `contain`으로 덮어쓰여 사용자가 `cover`를 선택해도 미리보기가 `contain`으로 나오던 버그(`d97386e`, `51e017c` 시리즈, 2026-05-27).

이 패턴은 본질적으로 **"같은 데이터를 두 군데에서 해석하면 언젠가 어긋난다"**는 구조 문제다. 따라서 씬 카드를 **단 한 번** 변환한 중간 JSON(`RenderSpec`)을 만들고, 양쪽이 이 JSON만 보고 기계적으로 그리도록 흐름을 바꾼다.

```
SceneCard ──▶ build_render_spec() ──▶ RenderSpec ──▶ ffmpeg / 프리뷰 (동일 입력)
```

---

## 2. 설계 원칙 (필수 4가지)

1. **추상 키워드 금지** — `cover` / `contain` / `stretch` 같은 키워드는 RenderSpec 어디에도 등장하지 않는다. 변환 단계에서 `scale_x / scale_y / crop / pad` 절대 픽셀로 풀어 환산한다. 직렬화 결과에 `fit` 키가 남아 있으면 그 자체로 위반.
2. **시간과 공간의 분리** — `footage_speed`는 `playback_rate`(시간축)로 매핑되고, 공간 변환(`scale/crop/pad`)과 같은 표현식 안에서 다루지 않는다. 빌더 함수의 분기 구조 자체가 두 변수의 결합 가능성을 차단한다(이전 fit-vs-speed 회귀의 재발 방지).
3. **절대 픽셀 단위** — 좌표·크기는 `int` 픽셀, 시간은 `float` 초. 비율(0~1)·ms·프레임 인덱스 같은 다른 단위를 RenderSpec에 섞지 않는다.
4. **색상은 `#RRGGBB`로 통일** — 이름(`black`), 짧은 헥스(`#fff`), `rgb(...)` 표기 모두 변환 단계에서 6자리 대문자 헥스로 정규화한다.

---

## 3. 스키마

### 3.1 RenderSpec (루트)

| 필드 | 타입 | 의미 |
|---|---|---|
| `version` | `int` | 스키마 버전(현재 `1`). 호환 깨지는 변경 시 +1. |
| `scene_id` | `int` | 같은 프로젝트 내 씬 식별자. |
| `canvas` | `Canvas` | 출력 캔버스(해상도 + fps). |
| `duration_sec` | `float` | 씬 전체 길이(초). footage 길이 / playback_rate 와 `duration_hint_sec` 중 큰 값. |
| `layers` | `list[Layer]` | 아래에서 위 순서로 그릴 레이어 목록. footage가 가장 아래, 텍스트/도형/그래픽이 위. |

### 3.2 Canvas

| 필드 | 타입 | 의미 |
|---|---|---|
| `width` | `int (≥1)` | 캔버스 가로 픽셀. |
| `height` | `int (≥1)` | 캔버스 세로 픽셀. |
| `fps` | `int (1~240)` | 프레임 레이트. 기본 30. |

### 3.3 공통 자료형

- `Size { width: int≥1, height: int≥1 }` — 가로/세로 픽셀.
- `CropBox { x: int≥0, y: int≥0, w: int≥1, h: int≥1 }` — 좌상단 기준 사각 영역(픽셀).

### 3.4 Layer (Union, discriminator=`type`)

모든 레이어는 공통으로 다음 필드를 가진다:

| 필드 | 타입 | 의미 |
|---|---|---|
| `type` | `str` | 디스크리미네이터. `video` / `image` / `text` / `shape` / `graphic` / `mosaic`. |
| `start_sec` | `float (≥0)` | 레이어 등장 시점(씬 시작 기준 초). |
| `end_sec` | `float \| null` | 레이어 종료 시점. `null`이면 씬 끝까지 자동 연장. |
| `opacity` | `float (0~1)` | 알파 투명도. |

### 3.5 VideoLayer (`type="video"`)

| 필드 | 타입 | 의미 |
|---|---|---|
| `source` | `str` | footage 파일 경로(또는 footage_dir 기준 상대). |
| `source_size` | `Size` | **회전 적용 후**의 가시 해상도. 매퍼는 이 크기 위에 scale을 곱한다고 가정한다. |
| `scale_x`, `scale_y` | `float > 0` | source_size에 곱할 단일 배율. cover/contain/stretch가 모두 이 값으로 환원. |
| `crop` | `CropBox \| null` | scale 적용 후의 픽셀 좌표계에서 잘라낼 영역. `cover`에서 사용. |
| `pad` | `CropBox \| null` | 캔버스 안에서 scale 결과를 배치할 영역. `contain`에서 사용(letterbox/pillarbox 의미). |
| `playback_rate` | `float > 0` | `footage_speed`가 그대로 매핑. `1.0`은 원속도, `2.0`은 2배속, `0.5`는 절반 속도. fit 계산과 절대 엮이지 않는다. |
| `rotation_deg` | `0/90/180/270` | 사용자 수동 회전. 매퍼는 이 값을 ffmpeg `transpose` 또는 CSS `transform`에 그대로 전달. |

### 3.6 ImageLayer (`type="image"`, Ken Burns)

VideoLayer와 동일한 공간 필드(`source_size`, `scale_x/y`, `crop`, `pad`)에 다음을 더한다:

| 필드 | 타입 | 의미 |
|---|---|---|
| `zoom_from`, `zoom_to` | `float > 0` | Ken Burns 줌 시작/끝 배율(기본 1.0 → 1.15). |

### 3.7 TextLayer (`type="text"`)

| 필드 | 타입 | 의미 |
|---|---|---|
| `text` | `str` | 화면에 표시할 문구. |
| `position_x`, `position_y` | `int` | 캔버스 픽셀(텍스트 박스 중심 기준). |
| `font_size_px` | `int ≥ 1` | 폰트 크기(px). |
| `font_family` | `str` | 기본 `NotoSansKR`. |
| `color`, `outline_color` | `str` | `#RRGGBB`. |
| `outline_width_px` | `int ≥ 0` | 외곽선 두께. |
| `align` | `left/center/right` | 정렬. |
| `bold`, `italic` | `bool` | 굵게/기울임. |
| `enter_effect`, `exit_effect` | `none/fade/typing/zoom` | 등장/사라짐 효과. |
| `enter_duration_sec`, `exit_duration_sec` | `float ≥ 0` | 효과 길이. |

### 3.8 ShapeLayer (`type="shape"`)

| 필드 | 타입 | 의미 |
|---|---|---|
| `shape` | `rect/ellipse` | 도형 종류. |
| `x`, `y` | `int` | 좌상단 픽셀 좌표. |
| `width`, `height` | `int ≥ 1` | 도형 크기(픽셀). |
| `color` | `str` | `#RRGGBB`. |
| `enter_effect`, `exit_effect` | `none/fade` | 도형은 fade만 지원(typing/zoom은 fade로 폴백). |

### 3.9 GraphicLayer (`type="graphic"`)

| 필드 | 타입 | 의미 |
|---|---|---|
| `graphic_type` | `spotlight/target/alert/reaction/smell/emoji` | 그래픽 종류. |
| `center_x`, `center_y` | `int` | 중심 픽셀 좌표(GraphicOverlay 규약 그대로). |
| `params` | `dict` | 그래픽별 세부 파라미터. 매퍼가 해석. |

### 3.10 MosaicLayer (`type="mosaic"`)

| 필드 | 타입 | 의미 |
|---|---|---|
| `shape` | `rect/ellipse` | 영역 모양. |
| `x`, `y`, `width`, `height` | `int` | 좌상단 기준 픽셀 영역. |
| `mode` | `pixelate/blur` | 적용 방식. |
| `pixel_size` | `int ≥ 1` | pixelate 시 격자 크기(px). |
| `blur_radius` | `int ≥ 0` | blur 시 반경(px). |

---

## 4. 변환 규칙

### 4.1 fit → (scale, crop, pad)

원본 회전을 먼저 적용한 `visible_source(width=sw, height=sh)`와 캔버스 `(cw, ch)`가 주어지면:

| fit | scale_x | scale_y | crop | pad |
|---|---|---|---|---|
| `contain` | `min(cw/sw, ch/sh)` | 같은 값 | `null` | 캔버스 안에서 가운데 배치(letterbox/pillarbox). |
| `cover` | `max(cw/sw, ch/sh)` | 같은 값 | scale 결과에서 가운데 `cw × ch` 잘라냄. | `null` |
| `stretch` | `cw / sw` | `ch / sh` | `null` | `null` |

이 표가 빌더 함수 `_compute_fit_geometry()` 안에 그대로 구현되어 있다. **이 함수는 `footage_speed`를 인자로 받지 않는다 — 시간과 공간의 분리 보장 지점.**

### 4.2 footage_speed → playback_rate

`scene.footage_speed` 값을 그대로 `VideoLayer.playback_rate`에 대입한다. `0` 이하나 비숫자는 `1.0`으로 폴백.

`scene.duration_sec`은 `footage_duration / playback_rate`(영상 짧아지거나 길어지는 효과)와 `duration_hint_sec` 중 큰 값으로 결정한다.

`ImageLayer`는 별도의 `playback_rate`가 없고 `zoom_from→zoom_to`로 진행 속도가 결정된다. 이미지 자체는 정지이므로 영상-속도 개념이 무의미.

### 4.3 좌표 비율 → 픽셀

오버레이의 `x/y/width/height`(0~1 비율)는 `int(round(ratio * canvas_dim))`로 변환한다.

### 4.4 색상 정규화

`_normalize_color()`가 다음을 처리:
- 이름(`black`, `red`, ...) → 사전 매핑.
- `#fff` → `#FFFFFF` (3자리 → 6자리, 대문자).
- `#abcdef` → `#ABCDEF` (대문자 통일).
- `rgb(255, 0, 0)` → `#FF0000`.
- 그 외 알 수 없는 표기는 기본값(`#FFFFFF` 또는 `#000000`)으로 폴백.

### 4.5 회전 우선 적용

`source_size`는 항상 **회전 후 가시 영역**이다. `rotation_deg ∈ {90, 270}`이면 가로/세로를 swap한다. 그 다음 fit 계산이 진행되므로, 회전된 종횡비를 기준으로 cover/contain이 처리된다 — 이는 기존 `_make_scene_preview`의 `rotation → fit/scale → crop/pad` 순서와 일치.

`rotation_deg`는 RenderSpec에 그대로 남겨, 매퍼(ffmpeg/CSS)가 실제 회전 변환을 수행한다.

---

## 5. 사용 예시

### 5.1 가로 16:9 캔버스 + 가로 16:9 원본 + cover + 1배속

`canvas=1920×1080`, `source=1920×1080`, `fit=cover`, `footage_speed=1.0`

```json
{
  "version": 1,
  "scene_id": 1,
  "canvas": { "width": 1920, "height": 1080, "fps": 30 },
  "duration_sec": 10.0,
  "layers": [
    {
      "type": "video",
      "source": "footage.mp4",
      "source_size": { "width": 1920, "height": 1080 },
      "scale_x": 1.0,
      "scale_y": 1.0,
      "crop": { "x": 0, "y": 0, "w": 1920, "h": 1080 },
      "pad": null,
      "playback_rate": 1.0,
      "rotation_deg": 0,
      "start_sec": 0.0,
      "end_sec": 10.0,
      "opacity": 1.0
    }
  ]
}
```

### 5.2 세로 9:16 캔버스 + 가로 16:9 원본 + cover + 0.25배속

`canvas=1080×1920`, `source=1920×1080`, `fit=cover`, `footage_speed=0.25`

cover scale은 `max(1080/1920, 1920/1080) ≈ 1.7778`. scale 결과는 `3414×1920` → 가운데 `1080×1920` 잘라내기.

`playback_rate=0.25`이지만 위 scale 값은 변하지 않는다. 이전 fit-vs-speed 버그가 RenderSpec 도입으로 구조적으로 차단된 지점.

```json
{
  "type": "video",
  "source": "footage.mp4",
  "source_size": { "width": 1920, "height": 1080 },
  "scale_x": 1.7777778,
  "scale_y": 1.7777778,
  "crop": { "x": 1167, "y": 0, "w": 1080, "h": 1920 },
  "pad": null,
  "playback_rate": 0.25,
  "rotation_deg": 0,
  "start_sec": 0.0,
  "end_sec": 40.0
}
```

### 5.3 세로 9:16 캔버스 + 가로 16:9 원본 + contain (letterbox)

`fit=contain`. scale은 `min(1080/1920, 1920/1080) = 0.5625`. scale 결과는 `1080×607.5` → 캔버스 안에서 세로 가운데 배치.

```json
{
  "type": "video",
  "source_size": { "width": 1920, "height": 1080 },
  "scale_x": 0.5625,
  "scale_y": 0.5625,
  "crop": null,
  "pad": { "x": 0, "y": 656, "w": 1080, "h": 608 },
  "playback_rate": 1.0
}
```

---

## 6. 산출물 — `cache/render_spec.json`

`save_render_spec(project_id, [RenderSpec, ...], cache_dir)`가 다음 구조로 저장한다.

```json
{
  "version": 1,
  "project_id": "abc-123",
  "scenes": [
    { "version": 1, "scene_id": 1, "canvas": {...}, "layers": [...] },
    { "version": 1, "scene_id": 2, "canvas": {...}, "layers": [...] }
  ]
}
```

박테스터 감사 항목으로 활용한다 — 씬 카드와 RenderSpec의 1:1 대응, fit 키워드 미존재, playback_rate가 footage_speed와 일치하는지 검증.

---

## 7. 후속 작업 (이 이슈 #302의 범위 외)

- `#303` 백엔드 ffmpeg 매퍼를 RenderSpec 입력으로 전환 (vf chain 생성 로직 교체).
- `#309` 프론트엔드 미리보기를 RenderSpec 입력으로 전환 (CSS transform 생성).
- TS 타입 자동 생성 (별도 이슈).
- `edit/sync/multiscreen/title_gen` 등 나머지 EffectConfig 타입 매핑(현재는 None 반환).

---

## 8. 회귀 가드 (필수 유지)

`tests/render_spec/test_builder.py`의 다음 테스트가 깨지면 #302 도입 의도가 무너졌음을 의미한다:

- `test_speed_does_not_affect_spatial_geometry` — `footage_speed`가 `scale/crop/pad`에 영향을 주는지 검사.
- `test_speed_only_affects_playback_rate` — `playback_rate`만 바뀌고 공간 값은 동일한지 검사.
- `test_no_abstract_fit_keyword_in_serialized_layer` — 직렬화 결과에 `fit` 키가 남았는지 검사.

리뷰 단계에서 이 세 가드 테스트는 반드시 통과 상태로 유지되어야 한다.
