"""client_meta sanity check 회귀 테스트 (#308).

푸티지/TTS 업로드 시 프론트가 첨부한 메타 JSON 문자열을 파싱·검증하는
`_parse_client_video_meta` / `_parse_client_audio_meta` 단위 검증.

검증 포인트
1. None/빈 문자열/JSON 파싱 실패 → None (fallback 진입)
2. 필수 키 누락 / 타입 비정상 → None
3. 정상 범위 → dict 반환
4. 범위 초과(해상도/길이) → None
"""

from __future__ import annotations

import json
import pytest

from web.backend.routers.project import (
    _parse_client_video_meta,
    _parse_client_audio_meta,
)


# ── 1) None / 형식 이상 ────────────────────────────────────────────────────

def test_video_meta_none_returns_none():
    assert _parse_client_video_meta(None) is None


def test_video_meta_empty_string_returns_none():
    assert _parse_client_video_meta("") is None


def test_video_meta_invalid_json_returns_none():
    assert _parse_client_video_meta("not-json") is None
    assert _parse_client_video_meta("{") is None


def test_video_meta_non_dict_json_returns_none():
    assert _parse_client_video_meta("[1, 2, 3]") is None
    assert _parse_client_video_meta('"string"') is None
    assert _parse_client_video_meta("null") is None


# ── 2) 필수 키 누락 / 타입 비정상 ──────────────────────────────────────────

def test_video_meta_missing_keys_returns_none():
    assert _parse_client_video_meta(json.dumps({"width": 1920})) is None
    assert _parse_client_video_meta(json.dumps({"width": 1920, "height": 1080})) is None
    assert (
        _parse_client_video_meta(json.dumps({"height": 1080, "duration": 5.0}))
        is None
    )


def test_video_meta_non_numeric_returns_none():
    # 문자열로 들어와도 int/float 변환되면 통과해야 함(JS Number→string 직렬화 대비)
    assert (
        _parse_client_video_meta(
            json.dumps({"width": "1920", "height": "1080", "duration": "5.0"})
        )
        is not None
    )
    # 명백히 비정상 — 변환 실패
    assert (
        _parse_client_video_meta(
            json.dumps({"width": "abc", "height": 1080, "duration": 5.0})
        )
        is None
    )


# ── 3) 정상 범위 ──────────────────────────────────────────────────────────

def test_video_meta_normal_resolution_returns_dict():
    result = _parse_client_video_meta(
        json.dumps({"width": 1920, "height": 1080, "duration": 5.5, "source": "client"})
    )
    assert result == {"width": 1920, "height": 1080, "duration": 5.5}


def test_video_meta_4k_returns_dict():
    result = _parse_client_video_meta(
        json.dumps({"width": 3840, "height": 2160, "duration": 60.0})
    )
    assert result is not None
    assert result["width"] == 3840
    assert result["height"] == 2160


# ── 4) 범위 초과 ──────────────────────────────────────────────────────────

def test_video_meta_dimensions_too_small_returns_none():
    assert (
        _parse_client_video_meta(
            json.dumps({"width": 8, "height": 1080, "duration": 5.0})
        )
        is None
    )
    assert (
        _parse_client_video_meta(
            json.dumps({"width": 1920, "height": 8, "duration": 5.0})
        )
        is None
    )


def test_video_meta_dimensions_too_large_returns_none():
    assert (
        _parse_client_video_meta(
            json.dumps({"width": 32768, "height": 1080, "duration": 5.0})
        )
        is None
    )


def test_video_meta_duration_too_short_returns_none():
    assert (
        _parse_client_video_meta(
            json.dumps({"width": 1920, "height": 1080, "duration": 0.05})
        )
        is None
    )


def test_video_meta_duration_too_long_returns_none():
    # 1시간 초과 → fallback
    assert (
        _parse_client_video_meta(
            json.dumps({"width": 1920, "height": 1080, "duration": 3601})
        )
        is None
    )


def test_video_meta_duration_boundary_inclusive():
    # 정확히 0.1 / 3600 은 통과해야 한다 (inclusive)
    assert (
        _parse_client_video_meta(
            json.dumps({"width": 1920, "height": 1080, "duration": 0.1})
        )
        is not None
    )
    assert (
        _parse_client_video_meta(
            json.dumps({"width": 1920, "height": 1080, "duration": 3600})
        )
        is not None
    )


# ── 5) Audio meta ─────────────────────────────────────────────────────────

def test_audio_meta_none_returns_none():
    assert _parse_client_audio_meta(None) is None


def test_audio_meta_invalid_json_returns_none():
    assert _parse_client_audio_meta("{") is None
    assert _parse_client_audio_meta("[1, 2]") is None


def test_audio_meta_missing_duration_returns_none():
    assert _parse_client_audio_meta(json.dumps({})) is None
    assert _parse_client_audio_meta(json.dumps({"sample_rate": 44100})) is None


def test_audio_meta_normal_returns_dict():
    result = _parse_client_audio_meta(
        json.dumps({"duration": 12.5, "source": "client"})
    )
    assert result == {"duration": 12.5}


def test_audio_meta_out_of_range_returns_none():
    assert _parse_client_audio_meta(json.dumps({"duration": 0.05})) is None
    assert _parse_client_audio_meta(json.dumps({"duration": 3601})) is None


def test_audio_meta_boundary_inclusive():
    assert _parse_client_audio_meta(json.dumps({"duration": 0.1})) is not None
    assert _parse_client_audio_meta(json.dumps({"duration": 3600})) is not None


# ── 5.5) Audio meta — 옵셔널 필드 보존 (#312) ─────────────────────────────

def test_audio_meta_with_sample_rate_and_channels_returns_full_dict():
    """sample_rate/channels 가 유효 범위면 dict 에 그대로 포함된다."""
    result = _parse_client_audio_meta(
        json.dumps({"duration": 12.5, "sample_rate": 48000, "channels": 2})
    )
    assert result == {"duration": 12.5, "sample_rate": 48000, "channels": 2}


def test_audio_meta_with_only_sample_rate_keeps_duration_too():
    """sample_rate 만 동봉되어도 duration 과 함께 정상 반환."""
    result = _parse_client_audio_meta(
        json.dumps({"duration": 5.0, "sample_rate": 44100})
    )
    assert result == {"duration": 5.0, "sample_rate": 44100}


def test_audio_meta_sample_rate_out_of_range_is_dropped_silently():
    """sample_rate 가 범위 밖이면 옵셔널 필드만 누락하고 duration 은 신뢰."""
    result = _parse_client_audio_meta(
        json.dumps({"duration": 5.0, "sample_rate": 5000})  # 8kHz 미만
    )
    assert result == {"duration": 5.0}
    result = _parse_client_audio_meta(
        json.dumps({"duration": 5.0, "sample_rate": 300000})  # 192kHz 초과
    )
    assert result == {"duration": 5.0}


def test_audio_meta_channels_out_of_range_is_dropped_silently():
    result = _parse_client_audio_meta(
        json.dumps({"duration": 5.0, "channels": 0})
    )
    assert result == {"duration": 5.0}
    result = _parse_client_audio_meta(
        json.dumps({"duration": 5.0, "channels": 16})
    )
    assert result == {"duration": 5.0}


def test_audio_meta_invalid_sample_rate_type_is_dropped():
    """문자열 등 변환 불가 타입은 그냥 무시 — duration 만 반환."""
    result = _parse_client_audio_meta(
        json.dumps({"duration": 5.0, "sample_rate": "high"})
    )
    assert result == {"duration": 5.0}


# ── 6) 추가 필드 무시 ─────────────────────────────────────────────────────

def test_video_meta_extra_fields_ignored():
    """source 등 추가 필드가 있어도 핵심 3개 키만 검증/반환."""
    result = _parse_client_video_meta(
        json.dumps(
            {
                "width": 1920,
                "height": 1080,
                "duration": 5.0,
                "source": "client",
                "extra_field": "ignored",
            }
        )
    )
    assert result == {"width": 1920, "height": 1080, "duration": 5.0}
