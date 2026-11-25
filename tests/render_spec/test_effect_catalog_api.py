"""이펙트 카탈로그 API 회귀 테스트 (#306).

`GET /api/effect_catalog` 가 매니페스트 19종을 그대로 노출하는지를 검증.
프론트가 이 응답을 받아 `EffectAddPanel` 버튼 목록을 그리므로, 백엔드/프론트의 효과 목록
불일치가 발생하지 않도록 본 테스트가 회귀를 막는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def client():
    # web/ 패키지를 import 가능하도록 PYTHONPATH 설정 (다른 backend 테스트와 동일 패턴)
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from fastapi.testclient import TestClient
    from web.backend.main import app

    return TestClient(app)


def test_effect_catalog_returns_all_registry_entries(client):
    """/api/effect_catalog 응답이 EFFECT_REGISTRY와 동일한 개수/키를 노출한다."""
    from src.render_spec.effects import EFFECT_REGISTRY

    resp = client.get("/api/effect_catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert "effects" in body
    effects = body["effects"]
    assert len(effects) == len(EFFECT_REGISTRY)

    # 키 셋 동등성
    api_keys = {f"{e['category']}:{e['name']}" for e in effects}
    assert api_keys == set(EFFECT_REGISTRY.keys())


def test_effect_catalog_preserves_required_fields(client):
    """각 항목이 name/display_label/category/params 필드를 보유한다."""
    resp = client.get("/api/effect_catalog")
    body = resp.json()
    for e in body["effects"]:
        for field in ("name", "display_label", "category", "params"):
            assert field in e, f"필드 누락: {field}"
        assert isinstance(e["params"], dict)
