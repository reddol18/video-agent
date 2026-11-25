# 신규 효과 추가 가이드 (#306, #322)

이펙트 카탈로그 매니페스트(`src/render_spec/effects/`)와 백엔드 핸들러 레지스트리
(`PY_EFFECT_HANDLERS_*`)가 백엔드와 프론트 양쪽의 효과 목록·합성 진입점을 일원화하므로,
새 효과를 추가할 때는 *한 매니페스트 파일만 손대면* UI 버튼·백엔드 카탈로그·미리보기
합성·RenderSpec 변환이 모두 자동으로 연결된다.

본 가이드는 신규 효과 1종을 추가하는 표준 절차를 단계별로 설명한다.

---

## 1. 카테고리를 고른다

기존 6개 카테고리 중 하나를 선택한다:

| 카테고리 | 의미 | RenderSpec Layer |
|---------|------|------------------|
| `text` | 자막/문구 텍스트 | `TextLayer` |
| `shape` | 사각형/타원 도형 | `ShapeLayer` |
| `graphic` | 그래픽 마커 (spotlight/alert/target/...) | `GraphicLayer` |
| `filter` | 씬 전체 색감/무드 필터 | `FilterLayer` (footage 후처리) |
| `motion` | 씬 내 화면 다이나믹 효과 | `MotionLayer` (footage 후처리) |
| `mosaic` | 영역 모자이크/블러 | `MosaicLayer` |

새 카테고리가 필요하면 다음 위치를 모두 갱신한다:
- `src/render_spec/effects/_base.py` 의 `EffectCategory` Literal
- `src/render_spec/builder.py` 의 `_EFFECT_CATEGORY_ORDER` 튜플 (적용 순서)
- `src/render_spec/effects/_handlers.py` 는 카테고리 문자열 기반 dict 이므로 별도 수정 불필요
- `web/backend/routers/project.py` 의 `_PREVIEW_CATEGORIES` 메타 리스트
- `web/frontend/src/lib/effectCatalog.ts` 의 `CATEGORY_STYLE`/`CATEGORY_ORDER` (색상·정렬)

---

## 2. 매니페스트 파일에 `EffectDefinition` 을 추가한다

예: 그래픽 카테고리에 "speech_bubble" 효과를 추가한다고 하자.

`src/render_spec/effects/graphic.py` 끝에 다음을 더한다:

```python
EFFECT_GRAPHIC_SPEECH_BUBBLE = EffectDefinition(
    name="speech_bubble",
    display_label="Speech Bubble",
    category="graphic",
    params={
        "tail_direction": ParamSpec(
            kind="enum",
            label="꼬리 방향",
            default="bottom-left",
            options=["top-left", "top-right", "bottom-left", "bottom-right"],
            description="말풍선 꼬리가 향하는 방향",
        ),
    },
    preview_supported=True,
    backend_layer_type="graphic",
    tooltip="말풍선",
)
```

파일 맨 아래 `__all__` 리스트에도 새 상수를 추가한다.

---

## 3. `EFFECT_REGISTRY` 에 등록한다

`src/render_spec/effects/__init__.py` 의 `_ALL_EFFECTS` 리스트에 새 상수를 추가한다:

```python
_ALL_EFFECTS: list[EffectDefinition] = [
    ...
    EFFECT_GRAPHIC_EMOJI,
    EFFECT_GRAPHIC_SPEECH_BUBBLE,  # ← 추가
    ...
]
```

같은 파일의 `__all__` 에도 새 상수 이름을 추가한다.

> 등록 시 `category:name` 키가 중복되면 import 시점에 `RuntimeError` 가 발생한다.

---

## 4. (필요 시) 백엔드 모델 Literal 을 늘린다

새 효과가 백엔드 데이터 모델의 `Literal` 으로 식별된다면 그 범위를 늘려준다.

| 카테고리 | 모델 위치 | 필드 |
|---------|----------|------|
| `text` | `web/backend/models.py::TextOverlay` | — (text는 자유 텍스트) |
| `shape` | `web/backend/models.py::ShapeOverlay` | `shape: Literal["rect", "ellipse"]` |
| `graphic` | `web/backend/models.py::GraphicOverlay` | `type: Literal["spotlight", "alert", "target", "reaction", "smell", "emoji"]` |
| `filter` | `web/backend/models.py::SceneFilter` | `filter_type: Literal[...]` |
| `motion` | `web/backend/models.py::SceneMotion` | `motion_type: Literal["shake", "slowmo", "clip_zoom"]` |
| `mosaic` | `web/backend/models.py::MosaicOverlay` | `shape` / `mode` |

`src/render_spec/models.py` 측의 대응 Layer 도 함께 갱신한다.

> 위 예시(speech_bubble)는 `GraphicOverlay.type` Literal에 "speech_bubble" 을 추가하고
> `GraphicLayer.graphic_type` Literal에도 동일하게 추가한다.

---

## 5. TS 자동 생성 → 프론트 타입 갱신

다음 명령으로 `web/frontend/src/types/generated.ts` 를 재생성한다:

```bash
uv run python scripts/gen_ts_types.py
# 또는 frontend 빌드 시 자동 실행
cd web/frontend && npm run gen-types
```

이제 프론트의 `EffectAddPanel` 은 백엔드 `GET /api/effect_catalog` 응답을 읽어 새 버튼을
자동으로 표시한다. 별도 UI 코드 수정은 필요 없다.

---

## 6. 폴백 카탈로그 갱신 (FALLBACK_CATALOG)

> ⚠️ **필수 단계** — 빠뜨리면 백엔드 미기동 환경에서 신규 효과 버튼이 누락된다.

프론트는 `GET /api/effect_catalog` 응답을 1차로 사용하지만, **백엔드 응답이 실패하면
`web/frontend/src/lib/effectCatalog.ts::FALLBACK_CATALOG` 하드코딩 목록으로 폴백한다.**
이 폴백은 매니페스트와 1:1로 동기화되어야 한다(현재 19종 → 신규 효과 추가 시 +1).

### 6-1) FALLBACK_CATALOG 항목 추가

`web/frontend/src/lib/effectCatalog.ts` 의 `FALLBACK_CATALOG` 배열에서 해당 카테고리 섹션에
신규 효과 1줄을 추가한다. 매니페스트의 핵심 필드를 복제한다:

```typescript
// graphic 섹션 끝에 1줄 추가
{
  name: "speech_bubble",
  display_label: "Speech Bubble",
  category: "graphic",
  params: {},
  preview_supported: true,
  tooltip: "말풍선",
  backend_layer_type: "graphic",
},
```

필수 필드(매니페스트와 일치해야 하는 것):
- `name` — `EffectDefinition.name` 과 정확히 동일
- `display_label` — UI 버튼 라벨
- `category` — 6개 카테고리 중 하나
- `params` — 폴백에서는 빈 객체 `{}` 로 충분 (편집기는 백엔드 응답을 기다리므로)
- `preview_supported` — boolean
- `tooltip` — 버튼 hover 시 표시 문자열
- `backend_layer_type` — 해당하는 카테고리(`text`/`shape`/`graphic`/`mosaic`)면 매니페스트와 동일하게

### 6-2) 동기화 검증

매니페스트(`src/render_spec/effects/`)와 폴백(`FALLBACK_CATALOG`)의 `(category, name)`
키 집합이 일치하는지 확인한다. 불일치는 백엔드 미기동 환경에서 회귀로 이어진다.

> 향후 개선: 빌드 타임에 매니페스트에서 `FALLBACK_CATALOG` 를 자동 생성하면 이 단계가
> 제거된다(별도 이슈). 현재는 수동 동기화.

---

## 7. 렌더링 코드 추가

### 7-1) 카테고리 내 신규 효과 (예: graphic 에 speech_bubble)

대부분의 케이스. 기존 카테고리에 효과 1종만 더하는 경우다.

이 경우 **카테고리 단위 핸들러(`PY_EFFECT_HANDLERS_*`)는 이미 등록되어 있으므로 별도
등록이 필요 없다**. 다음만 보강한다:

- 백엔드(미리보기): `web/backend/routers/project.py` 의 카테고리별 `_apply_*_overlays_preview`
  또는 `src/clip_editor.py::apply_*` 의 내부 분기에 새 type 처리 코드 추가
- RenderSpec 빌더: `src/render_spec/builder.py` 의 `_build_*_layer` 가 자동으로 처리
  (모델 Literal 만 늘렸다면 별도 분기 불필요)

### 7-2) 신규 카테고리 추가 (드문 경우)

새 카테고리(예: `audio`)를 도입하는 경우 핸들러 레지스트리에 직접 등록한다:

```python
# src/render_spec/effects/audio.py (신규)
from src.render_spec.effects._handlers import (
    register_preview_handler,
    register_render_spec_handler,
)

@register_render_spec_handler("audio")
def _render_spec_handler(scene, canvas, duration_sec):
    for ov in (getattr(scene, "audio_overlays", None) or []):
        yield AudioLayer(...)

@register_preview_handler("audio")
def _preview_handler(src_path, dst_path, overlays_data, video_width, video_height, **kwargs):
    # 기존 apply_audio_overlays 함수를 import 해서 호출
    from src.clip_editor import apply_audio_overlays
    return bool(apply_audio_overlays(src_path, dst_path, overlays_data, video_width, video_height))
```

그리고 `src/render_spec/builder.py::_EFFECT_CATEGORY_ORDER` 와
`web/backend/routers/project.py::_PREVIEW_CATEGORIES` 메타 리스트에 새 카테고리를 추가한다.

> 중복 등록 시 `register_*_handler` 가 import 시점에 `RuntimeError` 를 던지므로 자동
> 감지된다.

---

## 8. 회귀 테스트

`tests/render_spec/test_effect_registry.py` 의 카테고리별 검증 테스트에서 신규 효과가
포함되도록 항목 카운트를 갱신한다.

```python
def test_existing_graphic_types_preserved():
    expected = {"spotlight", "alert", "target", "reaction", "smell", "emoji", "speech_bubble"}
    ...
```

신규 카테고리를 추가했다면 `tests/render_spec/test_effect_handlers.py` 의 `CATEGORIES`
튜플도 갱신한다.

---

## 검증 체크리스트

새 효과 PR을 만들 때 다음을 확인한다.

- [ ] `src/render_spec/effects/{카테고리}.py` 에 `EffectDefinition` 1개 추가
- [ ] 매니페스트 파일의 `__all__` 에 등록
- [ ] `src/render_spec/effects/__init__.py` 의 `_ALL_EFFECTS` 리스트와 `__all__` 에 등록
- [ ] (해당하면) `web/backend/models.py` / `src/render_spec/models.py` 의 Literal 갱신
- [ ] (신규 카테고리만) 핸들러 등록 + `_EFFECT_CATEGORY_ORDER` / `_PREVIEW_CATEGORIES` 갱신
- [ ] `uv run python scripts/gen_ts_types.py` 로 TS 타입 재생성
- [ ] **`web/frontend/src/lib/effectCatalog.ts::FALLBACK_CATALOG` 에 신규 효과 1줄 추가** (백엔드 미기동 환경 안전망 — 매니페스트와 1:1 동기화)
- [ ] `uv run python -m pytest tests/render_spec/` 통과
- [ ] `cd web/frontend && npm run build` 통과
- [ ] 백엔드 로컬 기동 → `GET /api/effect_catalog` 응답에 새 효과가 포함되는지 확인
- [ ] 프론트 EffectAddPanel 에서 새 버튼이 보이는지 확인
- [ ] **백엔드 끈 상태로 프론트만 띄워 EffectAddPanel 에 새 버튼이 보이는지 확인** (폴백 동작 검증)
