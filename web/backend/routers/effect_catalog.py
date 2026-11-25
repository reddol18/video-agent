"""이펙트 카탈로그 API (#306).

프론트(`EffectAddPanel` 등)가 효과 메타데이터를 백엔드에서 *한 번* 가져가도록 한다.
런타임 dict인 `EFFECT_REGISTRY` 자체는 `pydantic2ts` 가 자동 변환할 수 없으므로,
- 모델 정의(`EffectDefinition`, `ParamSpec`)는 TS 자동 생성 대상으로 두고
- 카탈로그 *데이터*는 본 라우터가 JSON으로 흘려준다.

엔드포인트
---------
- `GET /api/effect_catalog` → `{"effects": [EffectDefinition, ...]}`
  카테고리 순서는 매니페스트 등록 순서를 그대로 보존한다 — EffectAddPanel이 정렬 로직 없이
  그대로 섹션을 그릴 수 있게 하기 위함.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.render_spec.effects import EFFECT_REGISTRY, EffectDefinition


router = APIRouter(prefix="/api/effect_catalog", tags=["effect_catalog"])


@router.get("", summary="이펙트 카탈로그 전체")
def list_effects() -> dict[str, list[EffectDefinition]]:
    """6종 효과 메타데이터를 매니페스트 등록 순서대로 반환.

    프론트는 `category` 필드로 섹션을 묶고, 순서는 응답 배열 순서를 그대로 사용한다.
    """
    return {"effects": list(EFFECT_REGISTRY.values())}
