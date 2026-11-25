"""집중선(속도선) 오버레이 미리보기 핸들러 등록.

집중선은 기존 6종 효과 카테고리(text/shape/graphic/filter/motion/mosaic)와 달리 별도
오버레이 타입이라 ``EFFECT_REGISTRY`` (=EffectAddPanel 버튼 목록) 에는 넣지 않는다. 대신
미리보기 파이프(``web/backend/routers/project.py`` 의 ``_PREVIEW_CATEGORIES``) 가 호출할 수
있도록 ``"speed_lines"`` 키로 미리보기 핸들러만 등록한다.

미리보기 ≡ 렌더 동일성: 미리보기와 최종 렌더가 모두 ``src.clip_editor.apply_speed_lines`` →
``src.speed_lines.generate_speed_lines_png`` 를 호출하고 동일한 캐시 PNG 를 재사용한다.
"""

from __future__ import annotations

from src.render_spec.effects._handlers import PY_EFFECT_HANDLERS_PREVIEW


def _speed_lines_preview_handler(
    src_path: str,
    dst_path: str,
    overlays_data: list[dict],
    video_width: int,
    video_height: int,
    **kwargs,
) -> bool:
    """집중선 오버레이 미리보기 합성 진입점 — 최종 렌더와 동일 함수 호출."""
    from src.clip_editor import apply_speed_lines

    try:
        return bool(apply_speed_lines(
            src_path, dst_path, overlays_data, video_width, video_height,
        ))
    except Exception:
        return False


# 카테고리 literal 이 아닌 별도 오버레이 키이므로 dict 에 직접 등록한다
# (register_preview_handler 데코레이터는 EffectCategory literal 을 기대).
if "speed_lines" not in PY_EFFECT_HANDLERS_PREVIEW:
    PY_EFFECT_HANDLERS_PREVIEW["speed_lines"] = _speed_lines_preview_handler


__all__ = ["_speed_lines_preview_handler"]
