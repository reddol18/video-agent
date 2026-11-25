"""pydantic2ts 입력용 합본 모듈 (#304).

pydantic2ts CLI는 `--module` 인자 한 개만 받기 때문에, 두 곳에 흩어진 Pydantic 모델을
한 번에 변환하려면 한 모듈에서 모두 import해두어야 한다. 이 파일은 그 역할만 하는
얇은 래퍼이며, 런타임 import 외에 어떤 로직도 가지지 않는다.

새 Pydantic 모델 그룹을 자동 변환 대상에 추가하고 싶다면 아래 import 목록에 한 줄만
더해주면 된다.
"""

from __future__ import annotations

# 백엔드 API 모델 (씬 카드, 프로젝트, 요청/응답 바디 등)
from web.backend.models import *  # noqa: F401,F403

# 렌더 스펙 중간 모델 (#302)
from src.render_spec.models import *  # noqa: F401,F403

# 이펙트 카탈로그 매니페스트 모델 (#306) — EffectDefinition, ParamSpec
from src.render_spec.effects._base import *  # noqa: F401,F403
