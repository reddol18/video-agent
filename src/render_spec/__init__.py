"""RenderSpec — 씬 카드 정규화 변환기 (#302).

씬 카드(`SceneCard`)를 백엔드(ffmpeg)와 프론트엔드(미리보기) 양쪽이 *동일하게* 해석할 수 있는
중간 JSON 모델로 한 번만 변환한다. 추상 키워드(contain/cover/stretch 등)는 이 단계에서 모두
절대 픽셀/배율로 풀어내며, footage_speed 같은 시간축 변수는 fit 계산과 완전히 독립된
`playback_rate` 필드로 분리한다 — 이전 fit-vs-speed 회귀 버그(2026-05-27 d97386e 시리즈)의
근본 원인이었던 "speed 분기 안에서 fit이 강제로 contain으로 덮어쓰임" 패턴을 구조적으로
재발 불가능하게 만드는 것이 목표.

본 패키지의 역할은 *변환과 직렬화*만 다룬다. 실제 ffmpeg vf chain을 만들거나, 프론트 CSS를
계산하는 일은 후속 이슈(#303 백엔드 매퍼, #309 프론트 매퍼)에서 RenderSpec을 입력으로 받아
처리한다.

공개 API:
- `RenderSpec`, `Canvas`, `VideoLayer`, `ImageLayer`, `TextLayer`, `ShapeLayer`,
  `GraphicLayer`, `MosaicLayer`, `CropBox`, `Size`
- `build_render_spec(scene, project, footage_size, footage_duration_sec) -> RenderSpec`
- `save_render_spec(project_id, specs, cache_dir) -> Path`
"""

from src.render_spec.models import (
    Canvas,
    CropBox,
    GraphicLayer,
    ImageLayer,
    Layer,
    MosaicLayer,
    RenderSpec,
    ShapeLayer,
    Size,
    TextLayer,
    VideoLayer,
)
from src.render_spec.builder import (
    RENDER_SPEC_VERSION,
    build_render_spec,
    save_render_spec,
)
# RenderSpec → SceneCard 역동기화 (#335). NL 편집 적용 결과를 SceneCard 차원으로
# 환산해 IndexedDB Project.scenes 를 갱신할 수 있게 한다.
from src.render_spec.scene_card_sync import (
    merge_render_spec_into_scene_card,
    sync_scene_cards_from_render_spec,
)
from src.render_spec.ffmpeg_mapper import (
    find_footage_layer,
    image_layer_to_kenburns_vf,
    spec_to_video_ffmpeg_args,
    video_layer_to_atempo,
    video_layer_to_vf,
)
# 이펙트 카탈로그 매니페스트 (#306). 백엔드/프론트가 동일한 카탈로그를 본다.
# 2단계(#322): 백엔드 합성 핸들러 dict 도 함께 노출 — builder.py / project.py 가
# 카테고리별 if 분기 없이 레지스트리 조회로 효과를 일원화한다.
from src.render_spec.effects import (
    EFFECT_REGISTRY,
    PY_EFFECT_HANDLERS_PREVIEW,
    PY_EFFECT_HANDLERS_RENDER_SPEC,
    EffectCategory,
    EffectDefinition,
    ParamSpec,
    get_effect,
    get_preview_handler,
    get_render_spec_handler,
    iter_effects_by_category,
)

__all__ = [
    "Canvas",
    "CropBox",
    "GraphicLayer",
    "ImageLayer",
    "Layer",
    "MosaicLayer",
    "RenderSpec",
    "ShapeLayer",
    "Size",
    "TextLayer",
    "VideoLayer",
    "RENDER_SPEC_VERSION",
    "build_render_spec",
    "save_render_spec",
    # scene_card_sync (#335)
    "merge_render_spec_into_scene_card",
    "sync_scene_cards_from_render_spec",
    # ffmpeg mapper (#303)
    "find_footage_layer",
    "image_layer_to_kenburns_vf",
    "spec_to_video_ffmpeg_args",
    "video_layer_to_atempo",
    "video_layer_to_vf",
    # effect catalog (#306)
    "EFFECT_REGISTRY",
    "EffectCategory",
    "EffectDefinition",
    "ParamSpec",
    "get_effect",
    "iter_effects_by_category",
    # effect handler registry (#322)
    "PY_EFFECT_HANDLERS_PREVIEW",
    "PY_EFFECT_HANDLERS_RENDER_SPEC",
    "get_preview_handler",
    "get_render_spec_handler",
]
