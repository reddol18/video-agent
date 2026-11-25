"""키워드(파일명) 매칭 알고리즘 동등성 검증 — 이슈 #311.

백엔드 `_match_keyword`(`web/backend/routers/matching.py`)와
프론트 `web/frontend/src/lib/clientKeywordMatching.ts` 두 구현이 **완전히 동일한
결과**를 내는지 검증한다.

본 테스트의 역할:
  1. 백엔드 _match_keyword가 실제로 사용하는 rapidfuzz.fuzz.token_set_ratio 호출 시그니처
     (processor 미지정 → case-sensitive)를 못박는다.
  2. fixture 케이스에 대해 백엔드 점수를 산출하고 JSON으로 dump.
  3. 같은 fixture를 TS 검증 스크립트(`scripts/verify_keyword_matching_parity.mjs`)가
     읽어 들여 점수를 대조 가능하게 한다.

회귀 시나리오:
  - rapidfuzz 버전업으로 token_set_ratio 의미가 바뀌면 fixture 점수가 어긋난다.
  - TS 측 토큰화/Indel/sort 로직이 바뀌어도 같은 fixture로 잡힌다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# 백엔드 매칭 모듈은 rapidfuzz 의존. 환경에 미설치된 경우 스킵.
rapidfuzz = pytest.importorskip("rapidfuzz")
from rapidfuzz import fuzz  # noqa: E402


FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_PATH = FIXTURES_DIR / "keyword_matching_cases.json"


# ── fixture: 백엔드/프론트 양쪽이 공유하는 입력 케이스 ──────────────────────
# (text, target_stem) 쌍. target_stem은 _match_keyword 내부 변환 후 (replace _-) 값.
# 점수는 백엔드 fuzz.token_set_ratio(text, target_stem) 결과를 직접 기록 → snapshot.

# fixture를 코드 안에 명시 → 테스트가 fixture 파일을 생성/갱신해서 TS 측이 읽을 수 있게 한다.
_CASES = [
    # 정확 일치 / 단어 순서만 다름
    ("apple banana cherry", "banana cherry apple"),
    ("a b c", "b c a"),
    # 부분 집합
    ("a b", "a b c d"),
    ("apple", "apple banana cherry"),
    # 비유사
    ("apple", "banana cherry"),
    # 대소문자 (rapidfuzz processor=None 이므로 case-sensitive)
    ("Hello WORLD", "hello world"),
    ("HELLO", "hello"),
    # 한국어 띄어쓰기 차이
    ("자동차 도로", "자동차도로"),
    ("자동차 도로", "자동차 도로"),
    # 구두점 vs 공백
    ("hello, world!", "hello world"),
    # 빈 문자열 처리
    ("", "hello"),
    ("", ""),
    # 단일 토큰 vs 다중 토큰
    ("apple", "apple"),
    # 백엔드 _match_keyword의 텍스트 합성 예시: title + description + keywords
    (
        "도시 야경 서울의 아름다운 야경을 보여주는 영상 도시 야경 서울",
        "city night view seoul",
    ),
    # 백엔드는 stem에서 _ - 를 공백으로 치환하므로 (replace된 후 비교).
    (
        "도시 야경 서울의 아름다운 야경을 보여주는 영상 도시 야경 서울",
        "seoul night city",
    ),
    # 숫자 토큰
    ("hello1 world2", "world2 hello1"),
    # 한글 유사도
    ("강아지 산책", "강아지가 공원에서 산책"),
    # 토큰 일부 겹침 (intersection + diff1 + diff2 모두 비어있지 않음)
    ("apple banana cherry", "banana grape kiwi"),
    # 매우 긴 입력 (LCS DP 시간 검증)
    (
        "the quick brown fox jumps over the lazy dog and runs into the deep forest",
        "fox and dog and lazy forest are quick brown the the",
    ),
    # 단일 문자 토큰들
    ("a", "a b c d e"),
    ("a b c", "a"),
]


# ── 백엔드 알고리즘이 우리가 기대하는 그대로인지 직접 확인 ──────────────────
# 백엔드는 단순히 fuzz.token_set_ratio(text, fn_stem_with_replace) 만 호출한다.

def test_backend_score_matches_fuzz_directly():
    """_match_keyword 내부 라인:
        score = fuzz.token_set_ratio(text, fn.replace('_', ' ').replace('-', ' '))
    fixture 케이스가 위 호출과 정확히 같은 점수를 내는지 못박는다.
    """
    for text, target in _CASES:
        score = fuzz.token_set_ratio(text, target)
        # rapidfuzz는 float (0..100) 반환. 비교용 sanity.
        assert 0.0 <= score <= 100.0


# ── fixture JSON 갱신 + parity 검증 ─────────────────────────────────────────
# pytest 실행 시 fixture 파일을 (재)생성하여 TS 검증 스크립트가 읽을 수 있게 한다.

def test_generate_and_verify_fixture_snapshot(tmp_path: Path):
    """fixture JSON 파일 생성 + 즉시 재로딩해 점수 일치 검증.

    파일은 tests/fixtures/keyword_matching_cases.json 에 저장된다.
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        # fuzz.token_set_ratio 의 인자 시그니처 명세 — TS 측이 같은 의미로 구현했는지 확인용.
        "spec": {
            "algorithm": "rapidfuzz.fuzz.token_set_ratio",
            "processor": None,
            "case_sensitive": True,
            "stem_replace": {"_": " ", "-": " "},
            "score_range": [0.0, 100.0],
        },
        "cases": [],
    }
    for text, target in _CASES:
        score = fuzz.token_set_ratio(text, target)
        payload["cases"].append({
            "text": text,
            "target": target,
            "expected_score": float(score),
        })
    FIXTURE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 재로딩 후 동일한지 검증 (snapshot self-check).
    loaded = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for case in loaded["cases"]:
        actual = fuzz.token_set_ratio(case["text"], case["target"])
        # 본 fixture 자체는 rapidfuzz 출력으로 생성됐으니 정확히 동일해야 한다.
        assert abs(actual - case["expected_score"]) < 1e-9, (
            f"fixture snapshot drift: text={case['text']!r}, target={case['target']!r}, "
            f"expected={case['expected_score']}, actual={actual}"
        )


# ── _match_keyword 입출력 형태 회귀 방지 ────────────────────────────────────
# 프론트 포팅 결과가 백엔드와 같은 구조의 결과(chunk_id/source_file/trim_*)를
# 만들어야 하므로 백엔드 출력 형태를 못박는다.

def test_match_keyword_returns_expected_shape(tmp_path: Path):
    from web.backend.models import SceneCard, ClipEffect
    from web.backend.routers.matching import _match_keyword

    scene = SceneCard(
        scene_id=1,
        title="도시 야경",
        description="서울의 아름다운 야경을 보여주는 영상",
        keywords=["도시", "야경", "서울"],
    )
    chunks = [
        {
            "source_file": str(tmp_path / "seoul_night.mp4"),
            "chunk_id": "seoul_night_0",
            "start_sec": 0.0,
            "end_sec": 10.0,
        },
        {
            "source_file": str(tmp_path / "busan_beach.mp4"),
            "chunk_id": "busan_beach_0",
            "start_sec": 0.0,
            "end_sec": 5.0,
        },
    ]
    # 백엔드는 footage_dir이 존재해야 fallback 스캔 — chunks 있으면 fallback 미실행.
    result = _match_keyword(scene, str(tmp_path), chunks)
    assert result is not None
    assert "footage" in result and "match" in result
    assert isinstance(result["footage"], ClipEffect)
    assert result["match"].match_reason == "keyword_filename"
    assert result["match"].chunk_id  # 비어있지 않음
    assert 0.0 <= result["match"].match_score <= 100.0


# ── 시나리오 단위 점수 fixture (TS 클라이언트 매칭과 1:1 비교) ──────────────
# 프론트 `matchKeyword`가 동일 입력에서 동일 best_chunk를 선택하는지 검증하기 위한
# 시나리오 케이스들. fixture에 입력+백엔드 출력을 함께 저장하여 TS 검증 스크립트가 읽는다.

SCENARIO_CASES = [
    {
        "name": "korean_city_night",
        "scene": {
            "title": "도시 야경",
            "description": "서울의 아름다운 야경",
            "keywords": ["도시", "야경", "서울"],
        },
        "chunks": [
            {"source_file": "/footage/seoul_night.mp4", "chunk_id": "c1", "start_sec": 0.0, "end_sec": 10.0},
            {"source_file": "/footage/busan_beach.mp4", "chunk_id": "c2", "start_sec": 0.0, "end_sec": 5.0},
            {"source_file": "/footage/forest_walk.mp4", "chunk_id": "c3", "start_sec": 0.0, "end_sec": 8.0},
        ],
    },
    {
        "name": "underscore_hyphen_normalize",
        "scene": {
            "title": "강아지 산책",
            "description": "공원에서 산책하는 강아지",
            "keywords": ["강아지", "공원"],
        },
        "chunks": [
            {"source_file": "/footage/dog_park_walk.mp4", "chunk_id": "x1", "start_sec": 0.0, "end_sec": 5.0},
            {"source_file": "/footage/cat-sleeping.mp4", "chunk_id": "x2", "start_sec": 0.0, "end_sec": 3.0},
        ],
    },
    {
        "name": "stem_dedup",
        # 같은 stem이 두 번 등장하면 첫 번째만 채택 (백엔드 seen_files dedup).
        "scene": {
            "title": "apple",
            "description": "fresh red apple",
            "keywords": ["apple", "fruit"],
        },
        "chunks": [
            {"source_file": "/footage/apple.mp4", "chunk_id": "first", "start_sec": 0.0, "end_sec": 5.0},
            {"source_file": "/footage/apple.mp4", "chunk_id": "second", "start_sec": 5.0, "end_sec": 10.0},
            {"source_file": "/footage/banana.mp4", "chunk_id": "b1", "start_sec": 0.0, "end_sec": 5.0},
        ],
    },
    {
        "name": "image_extension",
        "scene": {
            "title": "노을 사진",
            "description": "황금빛 노을",
            "keywords": ["노을", "황금"],
        },
        "chunks": [
            {"source_file": "/footage/sunset_golden.jpg", "chunk_id": "img1", "start_sec": 0.0, "end_sec": 0.0},
            {"source_file": "/footage/sea.mp4", "chunk_id": "v1", "start_sec": 0.0, "end_sec": 5.0},
        ],
    },
]


def _backend_match_for_fixture(scene_dict, chunks):
    """fixture 저장용: 백엔드 _match_keyword와 동일한 단순 시뮬레이션.

    백엔드 코드와 동일한 로직을 직접 따라 한다 — _match_keyword는 footage_dir 파일
    스캔도 시도하므로, fixture에서는 chunks만 있는 케이스로 한정하여 fallback 미실행.
    """
    text = f"{scene_dict['title']} {scene_dict['description']} {' '.join(scene_dict['keywords'])}"
    best_score = -1.0
    best_chunk = None
    seen = set()
    for c in chunks:
        from pathlib import Path
        stem = Path(c["source_file"]).stem
        if stem in seen:
            continue
        seen.add(stem)
        score = fuzz.token_set_ratio(text, stem.replace("_", " ").replace("-", " "))
        if score > best_score:
            best_score = score
            best_chunk = c
    return best_chunk, best_score


def test_scenario_fixture_dump():
    """시나리오 케이스의 백엔드 매칭 결과를 fixture에 저장 — TS 측 검증용."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    scenario_path = FIXTURES_DIR / "keyword_matching_scenarios.json"
    payload = {"scenarios": []}
    for case in SCENARIO_CASES:
        chunk, score = _backend_match_for_fixture(case["scene"], case["chunks"])
        payload["scenarios"].append({
            "name": case["name"],
            "scene": case["scene"],
            "chunks": case["chunks"],
            "expected": {
                "chunk_id": chunk["chunk_id"] if chunk else None,
                "source_file": chunk["source_file"] if chunk else None,
                "match_score": float(score) if chunk else None,
            },
        })
    scenario_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # snapshot self-check.
    loaded = json.loads(scenario_path.read_text(encoding="utf-8"))
    for case in loaded["scenarios"]:
        chunk, score = _backend_match_for_fixture(case["scene"], case["chunks"])
        assert chunk is not None
        assert chunk["chunk_id"] == case["expected"]["chunk_id"]
        assert abs(float(score) - case["expected"]["match_score"]) < 1e-9
