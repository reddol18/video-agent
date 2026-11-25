"""Phase 1 — 시나리오 파서

시나리오 텍스트 파일을 읽어 Gemini로 씬 단위로 분해하고
cache/scene_plan.json을 저장한다.
"""

import json
import re
import sys
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz
from rich.console import Console
from rich.table import Table

from .utils.file_cache import load_json, save_json, text_hash
from .utils.gemini_client import generate_json

console = Console()

CACHE_PATH = "cache/scene_plan.json"

SYSTEM_PROMPT = """당신은 영상 편집 어시스턴트입니다.
사용자가 제공하는 시나리오 텍스트를 줄 단위로 분해하세요.
한 씬 = [N] 번호 하나입니다. 절대로 한 줄을 여러 씬으로 쪼개거나, 여러 줄을 하나의 씬으로 합치지 마세요.
[N] 번호가 곧 line_number이자 scene_id입니다.
문장 시작의 (#effect ...) 프리픽스는 영상 효과 지시어이므로 내용 분석에서 제외하고 narration_text에 포함하지 마세요.
문장 시작의 (#tts "...") 프리픽스는 이미 제거된 상태로 전달되므로 신경 쓰지 않아도 됩니다."""

USER_PROMPT_TEMPLATE = """아래 시나리오를 줄 단위로 분해하여 JSON 배열로 반환하세요.

규칙:
- 한 씬 = [N] 번호 하나. 절대 쪼개거나 합치지 마세요
- [N] 번호가 곧 line_number이자 scene_id입니다. 반드시 line_number 필드에 그대로 반환하세요
- 한 줄에 마침표가 여러 개 있어도 쪼개지 말고 그대로 하나의 씬으로 만드세요
- 씬을 합치거나 요약하지 마세요. 원문 문장을 그대로 narration_text에 사용하세요
- narration_text 생성 시 쉼표(,)로 연결되는 긴 나열 대신 마침표(.)로 문장을 끊어 TTS 속도를 안정적으로 유지하세요
  - 나쁜 예: "시장은 북적이고, 상인들은 소리치며, 손님들은 흥정을 벌이고 있습니다"
  - 좋은 예: "시장은 북적입니다. 상인들이 소리치며 손님들과 흥정을 벌입니다"

각 씬은 다음 스키마를 따릅니다:
{{
  "line_number": int,        // [N] 번호를 그대로 기입 (필수)
  "scene_id": int,           // line_number와 동일하게 기입
  "title": string,           // 씬 제목 (짧은 레이블)
  "description": string,     // 씬 내용 (시각적 장면 위주, 인물/배경/행동 포함)
  "mood": string,            // 감정·분위기 키워드 (예: "긴장감, 빠른 전개")
  "keywords": [string],      // 매칭에 사용할 핵심 명사 5개 이하
  "duration_hint_sec": int,  // 나레이션 글자 수 기준 최소 길이 추정 (한국어 약 5~6자/초, 최소 3초)
  "narration_text": string   // 원문 문장. 쉼표 나열 대신 마침표로 문장을 끊어서 작성
}}

반환 형식: JSON 배열만 출력. 설명이나 마크다운 없이 순수 JSON만.

---
시나리오:
{scenario}
"""


_MULTISCREEN_RE = re.compile(
    r"^\(#effect\s+multiscreen\s+([^)]+)\)\s*",
    re.IGNORECASE,
)

_SOUND_RE = re.compile(
    r"\(#effect\s+sound\s+([^)]+)\)\s*",
    re.IGNORECASE,
)

_CLIP_RE = re.compile(
    r"^\(#effect\s+clip\s+([^)]+)\)\s*",
    re.IGNORECASE,
)

_KENBURNS_RE = re.compile(
    r"^\(#effect\s+kenburns\s+([^)]+)\)\s*",
    re.IGNORECASE,
)

_TTS_RE = re.compile(
    r"^\(#tts\s+\"([^\"]*)\"\)\s*",
    re.IGNORECASE,
)

# 씬 태그 프리픽스: narration_text 앞쪽에 연속된 [Tag1][Tag2][Tag3] (사이 공백 허용)
# - 토큰 1개: \[[A-Za-z][A-Za-z0-9_-]*\]
# - 전체 패턴: 토큰 1개 이상 + 뒤 공백
_TAG_PREFIX_RE = re.compile(
    r"^(?:\s*\[[A-Za-z][A-Za-z0-9_-]*\])+\s*",
)
_TAG_TOKEN_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)\]")

# 빌트인 태그 사전 캐시
_BUILTIN_TAG_IDS_CACHE: set[str] | None = None
_BUILTIN_TAG_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "scene_tags.json"


def _load_builtin_tag_ids(config_path: Path | None = None) -> set[str]:
    """config/scene_tags.json에서 빌트인 태그 ID 목록을 set으로 로드(캐시).

    파일이 없거나 읽기 실패 시 빈 set 반환.
    config_path를 명시하면 캐시를 우회하여 그 경로에서 로드한다(테스트용).
    """
    global _BUILTIN_TAG_IDS_CACHE
    if config_path is not None:
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        return set((data.get("tags") or {}).keys())

    if _BUILTIN_TAG_IDS_CACHE is not None:
        return _BUILTIN_TAG_IDS_CACHE
    try:
        data = json.loads(_BUILTIN_TAG_CONFIG_PATH.read_text(encoding="utf-8"))
        _BUILTIN_TAG_IDS_CACHE = set((data.get("tags") or {}).keys())
    except (OSError, json.JSONDecodeError):
        _BUILTIN_TAG_IDS_CACHE = set()
    return _BUILTIN_TAG_IDS_CACHE

# title_gen 효과: (#effect title_gen --text "제목" --mode shimmer ...)
_TITLE_GEN_RE = re.compile(
    r"^\(#effect\s+title_gen\s+([^)]+)\)\s*",
    re.IGNORECASE,
)

# kenburns + copy_img 중첩 패턴:
# (#effect kenburns (#effect copy_img --input "파일" --output "파일" ...))
_KENBURNS_COPY_IMG_RE = re.compile(
    r"^\(#effect\s+kenburns\s+\(#effect\s+copy_img\s+([^)]+)\)\s*\)\s*",
    re.IGNORECASE,
)

# edit 패턴: (#edit 장면설명 파일명 [최소길이])
# 장면 설명과 파일명 사이 구분은 파일명(확장자 포함)을 마지막에서 두 번째/세 번째 토큰으로 인식
# 형식: (#edit <description> <filename.ext> [min_duration])
_EDIT_RE = re.compile(
    r"^\(#edit\s+(.+?)\s+([\w\-\.]+\.\w+)(?:\s+(\d+(?:\.\d+)?))?\)\s*",
    re.IGNORECASE,
)

# sync 패턴:
# 형식 1: (#sync <word>)
# 형식 2: (#sync <word> (#edit <description> <file.ext> [min_duration]))
# 형식 3: (#sync <word> (#effect ...))
# <word>는 T_audio를 맞출 기준 단어 (나레이션 내 등장 위치)
_SYNC_RE = re.compile(
    r"^\(#sync\s+(\S+?)(?:\s+(\(#(?:edit|effect)\s+[^)]*(?:\([^)]*\)[^)]*)*\)))?\)\s*",
    re.IGNORECASE,
)


def _parse_tts_prefix(text: str) -> tuple[str | None, str]:
    """문장에서 (#tts "커스텀 나레이션") 프리픽스를 파싱한다.

    Returns:
        (tts_text, cleaned_text)
        tts_text: 따옴표 안의 커스텀 나레이션 텍스트 (없으면 None)
        cleaned_text: 프리픽스를 제거한 순수 원문 텍스트 (장면 분석에 사용)
    """
    m = _TTS_RE.match(text)
    if not m:
        return None, text

    tts_text = m.group(1)
    cleaned = text[m.end():]
    return tts_text, cleaned


def _parse_tag_prefix(
    text: str,
    known_tag_ids: set[str] | None = None,
) -> tuple[list[str] | None, str]:
    """문장 앞쪽에 연속된 [Tag1][Tag2][Tag3] 패턴을 파싱한다.

    - narration_text 앞쪽에 연속된 `[Horror][Crash]` 형태만 추출한다.
      본문 중간의 `[...]`는 일반 텍스트로 간주하여 건드리지 않는다.
    - 태그 ID는 소문자로 정규화한다 (`[Horror]` → `horror`).
    - `known_tag_ids`에 들어있는 태그만 결과에 포함하고, 없는 태그는 경고 후 무시한다.
      단, 정규식에 매칭된 토큰은 본문에서 모두 제거되어 TTS·자막에 진입하지 않는다.

    Args:
        text: 입력 문장 원본 (TTS/effect 프리픽스 이후의 텍스트 권장)
        known_tag_ids: 인식할 태그 ID 집합. None이면 config/scene_tags.json의
            빌트인 사전을 로드한다.

    Returns:
        (tags, cleaned_text)
        tags: 추출된 태그 ID 리스트(소문자, 입력 순서 유지, 중복 제거). 매칭이
              전혀 없으면 None.
        cleaned_text: 앞쪽 태그 토큰을 제거한 본문 (앞뒤 공백 정리 안 함 — 호출자
                      가 strip() 결정)
    """
    m = _TAG_PREFIX_RE.match(text)
    if not m:
        return None, text

    raw_tokens = _TAG_TOKEN_RE.findall(m.group(0))
    if not raw_tokens:
        return None, text

    if known_tag_ids is None:
        known_tag_ids = _load_builtin_tag_ids()

    # 소문자 정규화 + 입력 순서 유지 중복 제거 + known 필터
    seen: set[str] = set()
    tags: list[str] = []
    unknown: list[str] = []
    for tok in raw_tokens:
        tid = tok.lower()
        if tid in seen:
            continue
        seen.add(tid)
        if tid in known_tag_ids:
            tags.append(tid)
        else:
            unknown.append(tok)

    if unknown:
        # 알 수 없는 태그는 무시하고 stderr로 경고
        print(
            f"[scenario_parser] 알 수 없는 태그 무시: {unknown} (원문: {text!r})",
            file=sys.stderr,
        )

    cleaned = text[m.end():]
    if not tags:
        # 매칭된 토큰이 전부 unknown이라도 본문은 정리해서 반환 (TTS 진입 방지)
        return None, cleaned
    return tags, cleaned


_MAX_EFFECT_NESTING_DEPTH = 3


def _count_effect_nesting_depth(text: str) -> int:
    """문자열에서 (#effect ...) 중첩 깊이를 계산한다."""
    depth = 0
    max_depth = 0
    i = 0
    while i < len(text):
        if text[i:i+8] == "(#effect":
            depth += 1
            max_depth = max(max_depth, depth)
        elif text[i] == ")" and depth > 0:
            depth -= 1
        i += 1
    return max_depth


def _parse_kenburns_prefix(text: str) -> tuple[dict | None, str]:
    """문장에서 (#effect kenburns 이미지파일명) 프리픽스를 파싱한다.

    중첩 copy_img 패턴(#effect kenburns (#effect copy_img ...))을 먼저 확인하고,
    없으면 단순 kenburns 패턴을 확인한다.

    중첩 깊이가 _MAX_EFFECT_NESTING_DEPTH(3)를 초과하면 ValueError를 발생시킨다.

    Returns:
        (kenburns_footage, cleaned_text)
        kenburns_footage: {"type": "kenburns", "image": "image.jpg"} 또는
                          {"type": "kenburns_copy_img", "copy_img_options": {...}} 형태의 dict
                          (없으면 None)
        cleaned_text: 프리픽스를 제거한 순수 나레이션 텍스트
    """
    # 중첩 깊이 검사 (3단계 초과 시 파싱 에러)
    depth = _count_effect_nesting_depth(text)
    if depth > _MAX_EFFECT_NESTING_DEPTH:
        raise ValueError(
            f"effect 중첩 깊이가 최대값({_MAX_EFFECT_NESTING_DEPTH})을 초과했습니다 "
            f"(현재 깊이: {depth}). 중첩을 줄여주세요."
        )

    # 중첩 copy_img 패턴 먼저 확인 (깊이 1단계)
    m_nested = _KENBURNS_COPY_IMG_RE.match(text)
    if m_nested:
        copy_img_args_str = m_nested.group(1).strip()
        copy_img_options = _parse_option_args(copy_img_args_str)
        cleaned = text[m_nested.end():]
        return {
            "type": "kenburns_copy_img",
            "copy_img_options": copy_img_options,
        }, cleaned

    # 단순 kenburns 패턴
    m = _KENBURNS_RE.match(text)
    if not m:
        return None, text

    image_file = m.group(1).strip()
    cleaned = text[m.end():]
    return {"type": "kenburns", "image": image_file}, cleaned


def _parse_option_args(args_str: str) -> dict:
    """--key value 형태의 옵션 문자열을 dict로 파싱한다.

    value가 따옴표로 감싸인 경우 따옴표를 제거한다.
    숫자 값은 int 또는 float으로 자동 변환한다.

    Args:
        args_str: "--text '제목' --mode shimmer --duration 5" 형태의 문자열

    Returns:
        {"text": "제목", "mode": "shimmer", "duration": 5} 형태의 dict
    """
    result: dict = {}
    # --key "value" 또는 --key value 패턴
    pattern = re.compile(
        r"--(\w+)\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))"
    )
    for m in pattern.finditer(args_str):
        key = m.group(1)
        # 따옴표 있는 값, 작은따옴표 있는 값, 따옴표 없는 값 순서로 확인
        val_str = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4)
        )
        # 숫자 변환 시도
        try:
            if "." in val_str:
                result[key] = float(val_str)
            else:
                result[key] = int(val_str)
        except (ValueError, TypeError):
            result[key] = val_str
    return result


def _parse_title_gen_prefix(text: str) -> tuple[dict | None, str]:
    """문장에서 (#effect title_gen --text "제목" --mode shimmer ...) 프리픽스를 파싱한다.

    지원 옵션: --text, --mode, --duration, --font_path, --font_size,
               --text_color, --rect_color, --bg_color, --dissolve_time, --spc_word

    Returns:
        (title_gen_footage, cleaned_text)
        title_gen_footage: {"type": "title_gen", "options": {...}} 형태의 dict (없으면 None)
        cleaned_text: 프리픽스를 제거한 순수 나레이션 텍스트
    """
    m = _TITLE_GEN_RE.match(text)
    if not m:
        return None, text

    options_str = m.group(1).strip()
    options = _parse_option_args(options_str)
    cleaned = text[m.end():]
    return {"type": "title_gen", "options": options}, cleaned


_MULTISCREEN_LAYOUT_VALUES = {
    "left_right", "top_bottom", "diagonal_lr", "diagonal_rl",
    "big_left", "big_right", "grid2x2", "pip_br", "pip_bl",
    "triptych", "auto",
}

_MULTISCREEN_LAYOUT_RE = re.compile(
    r"\blayout=([\w]+)\b",
    re.IGNORECASE,
)


def _parse_multiscreen_prefix(text: str) -> tuple[list[str] | dict | None, str]:
    """문장에서 (#effect multiscreen ...) 프리픽스를 파싱한다.

    layout=xxx 파라미터가 있으면 footage_layout 필드를 포함한 dict를 반환한다.

    Returns:
        (footage_data, cleaned_text)
        footage_data:
          - layout=auto 또는 layout 없음: 파일명 리스트
          - layout=xxx (auto 이외): {"files": [...], "layout": "xxx"} dict
          (없으면 None)
        cleaned_text: 프리픽스를 제거한 순수 나레이션 텍스트
    """
    m = _MULTISCREEN_RE.match(text)
    if not m:
        return None, text

    raw_args = m.group(1)

    # layout=xxx 파라미터 추출 (있으면 파일명 목록에서 제거)
    layout = "auto"
    layout_m = _MULTISCREEN_LAYOUT_RE.search(raw_args)
    if layout_m:
        layout_val = layout_m.group(1).lower()
        if layout_val in _MULTISCREEN_LAYOUT_VALUES:
            layout = layout_val
        raw_args = raw_args[: layout_m.start()] + raw_args[layout_m.end():]

    # 파일명 목록 파싱 (layout= 토큰 제거 후 처리)
    raw_args = raw_args.strip().rstrip(",")
    if "," in raw_args:
        footage_list = [f.strip() for f in raw_args.split(",") if f.strip()]
    else:
        footage_list = [f.strip() for f in raw_args.split() if f.strip()]

    cleaned = text[m.end():]

    if layout == "auto":
        # 기존 동작 유지: 단순 파일명 리스트 반환
        return footage_list, cleaned
    else:
        # 레이아웃 지정: {"type": "multiscreen", "files": [...], "layout": "xxx"} 반환
        return {"type": "multiscreen", "files": footage_list, "layout": layout}, cleaned


def _parse_sound_prefix(text: str) -> tuple[dict | None, str]:
    """문장에서 (#effect sound 파일명) 프리픽스를 파싱한다.

    문장 어느 위치에도 올 수 있으며, 발견되면 제거한다.

    Returns:
        (sound_effect, cleaned_text)
        sound_effect: {"type": "sound", "file": "파일명"} 형태의 dict (없으면 None)
        cleaned_text: 프리픽스를 제거한 나머지 텍스트
    """
    m = _SOUND_RE.search(text)
    if not m:
        return None, text

    file_name = m.group(1).strip()
    cleaned = text[:m.start()] + text[m.end():]
    return {"type": "sound", "file": file_name}, cleaned.strip()


def _parse_clip_prefix(text: str) -> tuple[dict | None, str]:
    """문장에서 (#effect clip 파일명) 프리픽스를 파싱한다.

    Returns:
        (clip_footage, cleaned_text)
        clip_footage: {"type": "clip", "file": "파일명"} 형태의 dict (없으면 None)
        cleaned_text: 프리픽스를 제거한 순수 나레이션 텍스트
    """
    m = _CLIP_RE.match(text)
    if not m:
        return None, text

    file_name = m.group(1).strip()
    cleaned = text[m.end():]
    return {"type": "clip", "file": file_name}, cleaned


def _parse_sync_prefix(text: str) -> tuple[dict | None, str]:
    """문장에서 (#sync <word> [inner]) 프리픽스를 파싱한다.

    지원 형식:
    - (#sync <word>)
    - (#sync <word> (#edit <description> <file.ext> [min_duration]))
    - (#sync <word> (#effect ...))

    inner 지시어가 있으면 먼저 _parse_edit_prefix / _parse_kenburns_prefix 등으로
    파싱한 결과를 "inner" 필드에 포함한다.

    Returns:
        (sync_footage, cleaned_text)
        sync_footage: {"type": "sync", "word": "단어"} 또는
                      {"type": "sync", "word": "단어", "inner": {...}} (없으면 None)
        cleaned_text: 프리픽스를 제거한 순수 나레이션 텍스트
    """
    m = _SYNC_RE.match(text)
    if not m:
        return None, text

    word = m.group(1).strip()
    inner_str = m.group(2)
    cleaned = text[m.end():]

    if not inner_str:
        return {"type": "sync", "word": word}, cleaned

    # inner 지시어 파싱 (#edit 또는 #effect ...)
    inner_str = inner_str.strip()
    inner_footage: dict | None = None

    # #edit 내부 지시어
    edit_footage, _ = _parse_edit_prefix(inner_str)
    if edit_footage is not None:
        inner_footage = edit_footage
    else:
        # #effect kenburns, clip 등 시도
        kenburns_footage, _ = _parse_kenburns_prefix(inner_str)
        if kenburns_footage is not None:
            inner_footage = kenburns_footage
        else:
            clip_footage, _ = _parse_clip_prefix(inner_str)
            if clip_footage is not None:
                inner_footage = clip_footage

    if inner_footage is not None:
        return {"type": "sync", "word": word, "inner": inner_footage}, cleaned
    else:
        # inner를 파싱하지 못한 경우 raw 문자열로라도 보존
        return {"type": "sync", "word": word, "inner": {"type": "unknown", "raw": inner_str}}, cleaned


def _parse_edit_prefix(text: str) -> tuple[dict | None, str]:
    """문장에서 (#edit 장면설명 파일명 [최소길이]) 프리픽스를 파싱한다.

    형식: (#edit <description> <filename.ext> [min_duration])
    - description: 장면 설명 (Gemini에게 전달할 자연어)
    - filename: 영상 파일명 (확장자 포함)
    - min_duration: 최소 길이(초), 미지정 시 기본값 5

    Returns:
        (edit_footage, cleaned_text)
        edit_footage: {"type": "edit", "description": "...", "file": "파일명", "min_duration": 5} (없으면 None)
        cleaned_text: 프리픽스를 제거한 순수 나레이션 텍스트
    """
    m = _EDIT_RE.match(text)
    if not m:
        return None, text

    description = m.group(1).strip()
    file_name = m.group(2).strip()
    min_dur_str = m.group(3)
    min_duration = float(min_dur_str) if min_dur_str else 5.0
    cleaned = text[m.end():]
    return {
        "type": "edit",
        "description": description,
        "file": file_name,
        "min_duration": min_duration,
    }, cleaned


def _tokenize(text: str) -> set[str]:
    """파일명/키워드를 소문자 토큰 집합으로 분리."""
    text = unicodedata.normalize("NFC", text)
    tokens = re.split(r"[\s_\-\.]+", text.lower())
    return {t for t in tokens if t}


def _footage_score(stem: str, keywords: list[str], title: str, description: str) -> float:
    """footage 파일명(stem) ↔ 씬 정보 유사도 점수 (0.0~1.0).

    rapidfuzz token_sort_ratio + Jaccard 가중 평균.
    """
    query = " ".join(keywords + [title, description])
    # rapidfuzz: 토큰 정렬 후 편집거리 유사도
    fuzzy_score = fuzz.token_sort_ratio(stem, query) / 100.0

    # Jaccard: 토큰 집합 교집합 비율
    stem_tokens = _tokenize(stem)
    query_tokens = _tokenize(query)
    union = stem_tokens | query_tokens
    jaccard = len(stem_tokens & query_tokens) / len(union) if union else 0.0

    return round(fuzzy_score * 0.7 + jaccard * 0.3, 4)


def _suggest_footage(scenes: list, footage_dir: str = "footage") -> list:
    """각 씬에 footage 후보 파일을 추천하여 'footage' 필드를 채운다.

    footage_dir 안의 영상 파일을 스캔하고, 씬 키워드·제목·설명과의
    rapidfuzz + Jaccard 점수로 가장 유사한 파일명을 제안한다.
    footage 필드가 이미 있으면 덮어쓰지 않는다.
    """
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    footage_path = Path(footage_dir)
    if not footage_path.exists():
        return scenes

    files = [f for f in footage_path.iterdir() if f.suffix.lower() in video_exts]
    if not files:
        return scenes

    stems = [(f.name, unicodedata.normalize("NFC", f.stem)) for f in files]

    for scene in scenes:
        if scene.get("footage"):
            continue  # 이미 지정된 경우 건드리지 않음

        keywords = scene.get("keywords", [])
        title = scene.get("title", "")
        description = scene.get("description", "")

        scored = [
            (filename, _footage_score(stem, keywords, title, description))
            for filename, stem in stems
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        scene["footage"] = scored[0][0]  # 1위 파일명만 기록

    return scenes


def _validate_and_fix(scenes: list) -> list:
    """유효성 검사 및 자동 보정."""
    fixed = []
    for i, s in enumerate(scenes):
        # line_number 필드가 있으면 그것을 scene_id로 고정 (줄 번호 기반 footage 매핑 보장)
        # line_number가 없으면 연속 번호로 fallback
        if "line_number" in s and isinstance(s["line_number"], int):
            s["scene_id"] = s["line_number"]
        else:
            s["scene_id"] = i + 1  # 연속 번호 보정 (fallback)

        if not s.get("narration_text"):
            s["narration_text"] = s.get("description", "")

        if s.get("duration_hint_sec", 0) < 3:
            s["duration_hint_sec"] = 3

        if not s.get("keywords"):
            # description에서 단순 분리로 명사 추출 재시도
            words = s.get("description", "").split()
            s["keywords"] = [w for w in words if len(w) >= 2][:5]

        # sound_effect 필드 초기화 (아직 _apply_sound_effect 호출 전)
        if "sound_effect" not in s:
            s["sound_effect"] = None

        fixed.append(s)
    return fixed


def _extract_kenburns_map(scenario_text: str) -> dict[int, dict]:
    """시나리오 텍스트에서 문장 순서별 kenburns footage 정보를 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#effect kenburns ...) 프리픽스가
    붙은 문장의 순번(0-based)과 kenburns footage 정보를 매핑한 dict를 반환한다.
    """
    kenburns_map: dict[int, dict] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        kenburns_footage, _ = _parse_kenburns_prefix(line)
        if kenburns_footage is not None:
            kenburns_map[sentence_idx] = kenburns_footage
        sentence_idx += 1
    return kenburns_map


def _apply_kenburns_footage(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 kenburns footage 정보를 적용한다.

    Gemini는 (#effect ...) 프리픽스를 제거한 순수 나레이션 문장을 반환하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 footage 정보를 주입한다.

    직접 생성 씬(_direct=True)은 footage가 이미 채워져 있으므로 건너뜀.
    """
    kenburns_map = _extract_kenburns_map(scenario_text)
    if not kenburns_map:
        return scenes

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, kenburns_footage in kenburns_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        scene = sorted_scenes[scene_idx_0]
        if scene.get("_direct"):
            continue  # 직접 생성 씬은 footage 덮어쓰기 금지
        scene["footage"] = kenburns_footage

    return scenes


def _extract_multiscreen_map(scenario_text: str) -> dict[int, list[str] | dict]:
    """시나리오 텍스트에서 문장 순서별 multiscreen footage 목록을 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#effect multiscreen ...) 프리픽스가
    붙은 문장의 순번(0-based)과 footage 목록(또는 layout dict)을 매핑한 dict를 반환한다.
    반환된 map은 Gemini 파싱 결과의 scene_id와 매칭하기 위해 보존된다.
    """
    multiscreen_map: dict[int, list[str] | dict] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        footage_data, _ = _parse_multiscreen_prefix(line)
        if footage_data is not None:
            multiscreen_map[sentence_idx] = footage_data
        sentence_idx += 1
    return multiscreen_map


def _extract_tts_map(scenario_text: str) -> dict[int, str]:
    """시나리오 텍스트에서 문장 순서별 커스텀 TTS 텍스트를 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#tts "...") 프리픽스가
    붙은 문장의 순번(0-based)과 TTS 텍스트를 매핑한 dict를 반환한다.
    """
    tts_map: dict[int, str] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        tts_text, _ = _parse_tts_prefix(line)
        if tts_text is not None:
            tts_map[sentence_idx] = tts_text
        sentence_idx += 1
    return tts_map


def _apply_tts_narration(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 커스텀 TTS narration_text를 덮어쓴다.

    Gemini는 (#tts "...") 프리픽스를 제거한 원문을 받아 description 등을 생성하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 narration_text를 프리픽스에서 추출한 값으로 교체한다.

    직접 생성 씬(_direct=True)은 생성 시 이미 TTS narration_text가 반영되므로 건너뜀.
    """
    tts_map = _extract_tts_map(scenario_text)
    if not tts_map:
        return scenes

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, tts_text in tts_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        scene = sorted_scenes[scene_idx_0]
        if scene.get("_direct"):
            continue  # 직접 생성 씬은 생성 시 이미 처리됨
        scene["narration_text"] = tts_text

    return scenes


def _apply_multiscreen_footage(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 multiscreen footage 배열을 적용한다.

    Gemini는 (#effect ...) 프리픽스를 제거한 순수 나레이션 문장을 반환하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 footage 배열을 주입한다.

    직접 생성 씬(_direct=True)은 footage가 이미 채워져 있으므로 건너뜀.
    """
    multiscreen_map = _extract_multiscreen_map(scenario_text)
    if not multiscreen_map:
        return scenes

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, footage_list in multiscreen_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        scene = sorted_scenes[scene_idx_0]
        if scene.get("_direct"):
            continue  # 직접 생성 씬은 footage 덮어쓰기 금지
        scene["footage"] = footage_list

    return scenes


def _extract_tag_map(
    scenario_text: str,
    known_tag_ids: set[str] | None = None,
) -> dict[int, list[str]]:
    """시나리오 텍스트에서 문장 순서별 씬 태그 목록을 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, 줄의 모든 prefix(`[N]` 번호,
    `(#tts ...)`, `(#effect ...)`, `(#edit ...)`, `(#sync ...)`)를 벗겨낸 뒤
    남는 본문 앞쪽에 `[Tag1][Tag2]` 패턴이 있으면 태그 ID 리스트를 매핑한다.

    이 함수는 Gemini가 본문에서 prefix를 어떻게 처리하더라도 원문 시나리오
    순서를 기준으로 결정론적으로 매핑할 수 있게 해준다.

    Args:
        scenario_text: 시나리오 원문 (캐시 해시 대상이 되는 그 텍스트)
        known_tag_ids: 인식할 태그 ID 집합. None이면 빌트인 사전을 로드.

    Returns:
        {sentence_idx_0based: [tag_ids]} 매핑. 태그가 없는 줄은 키 미포함.
    """
    if known_tag_ids is None:
        known_tag_ids = _load_builtin_tag_ids()

    tag_map: dict[int, list[str]] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue

        # [N] 번호 prefix 제거 (run()과 동일 처리)
        line = re.sub(r"^\[\d+\]\s*", "", line)

        # 다른 모든 prefix를 제거하여 본문만 추린 뒤 앞쪽 태그 추출
        _, cleaned = _parse_tts_prefix(line)
        _, cleaned = _parse_multiscreen_prefix(cleaned)
        _, cleaned = _parse_kenburns_prefix(cleaned)
        _, cleaned = _parse_title_gen_prefix(cleaned)
        _, cleaned = _parse_clip_prefix(cleaned)
        _, cleaned = _parse_edit_prefix(cleaned)
        _, cleaned = _parse_sync_prefix(cleaned)
        _, cleaned = _parse_sound_prefix(cleaned)
        cleaned = cleaned.lstrip()

        tags, _ = _parse_tag_prefix(cleaned, known_tag_ids)
        if tags:
            tag_map[sentence_idx] = tags
        sentence_idx += 1
    return tag_map


def _apply_tags(
    scenes: list,
    scenario_text: str,
    known_tag_ids: set[str] | None = None,
) -> list:
    """추출된 씬 태그를 scenes의 `tags` 필드로 주입한다.

    원본 시나리오 줄 순서(0-based)와 scene_id 순서 정렬을 매핑하여 태그를 주입한다.
    직접 생성 씬(_direct=True)도 _validate_and_fix 단계에서 narration_text가 비어있을
    수 있으므로 동일하게 매핑된다. `_direct` 씬은 이미 narration_text/footage가
    채워져 있어 태그 주입과 충돌이 없다.
    """
    tag_map = _extract_tag_map(scenario_text, known_tag_ids)
    if not tag_map:
        # 태그 추출 결과가 없어도 모든 씬에 tags=[] 기본값을 보장
        for s in scenes:
            s.setdefault("tags", [])
        return scenes

    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)
    for scene_idx_0, tags in tag_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        sorted_scenes[scene_idx_0]["tags"] = list(tags)

    # 매핑되지 않은 씬에는 빈 리스트 기본값 보장
    for s in scenes:
        s.setdefault("tags", [])
    return scenes


def _strip_leading_tags(text: str, known_tag_ids: set[str] | None = None) -> str:
    """문장 앞쪽 태그 토큰을 제거하고 본문만 반환한다 (narration_text 정제용).

    내부적으로 `_parse_tag_prefix`를 호출하지만 태그 결과는 버린다.
    호출 후 `.lstrip()`을 적용하여 빈 공백 제거.
    """
    _, cleaned = _parse_tag_prefix(text, known_tag_ids)
    return cleaned.lstrip()


def _extract_title_gen_map(scenario_text: str) -> dict[int, dict]:
    """시나리오 텍스트에서 문장 순서별 title_gen footage 정보를 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#effect title_gen ...) 프리픽스가
    붙은 문장의 순번(0-based)과 title_gen footage 정보를 매핑한 dict를 반환한다.
    """
    title_gen_map: dict[int, dict] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        title_gen_footage, _ = _parse_title_gen_prefix(line)
        if title_gen_footage is not None:
            title_gen_map[sentence_idx] = title_gen_footage
        sentence_idx += 1
    return title_gen_map


def _apply_title_gen_footage(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 title_gen footage 정보를 적용한다.

    Gemini는 (#effect ...) 프리픽스를 제거한 순수 나레이션 문장을 반환하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 footage 정보를 주입한다.

    직접 생성 씬(_direct=True)은 footage가 이미 채워져 있으므로 건너뜀.
    """
    title_gen_map = _extract_title_gen_map(scenario_text)
    if not title_gen_map:
        return scenes

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, title_gen_footage in title_gen_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        scene = sorted_scenes[scene_idx_0]
        if scene.get("_direct"):
            continue  # 직접 생성 씬은 footage 덮어쓰기 금지
        scene["footage"] = title_gen_footage

    return scenes


def _extract_sound_map(scenario_text: str) -> dict[int, dict]:
    """시나리오 텍스트에서 문장 순서별 sound_effect 정보를 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#effect sound 파일명) 프리픽스가
    붙은 문장의 순번(0-based)과 sound_effect 정보를 매핑한 dict를 반환한다.
    """
    sound_map: dict[int, dict] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        sound_effect, _ = _parse_sound_prefix(line)
        if sound_effect is not None:
            sound_map[sentence_idx] = sound_effect
        sentence_idx += 1
    return sound_map


def _apply_sound_effect(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 sound_effect 정보를 적용한다.

    Gemini는 (#effect ...) 프리픽스를 제거한 순수 나레이션 문장을 반환하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 sound_effect 정보를 주입한다.

    (#effect sound)가 없는 씬은 sound_effect: None 으로 설정한다.
    """
    sound_map = _extract_sound_map(scenario_text)

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, scene in enumerate(sorted_scenes):
        if scene_idx_0 in sound_map:
            scene["sound_effect"] = {
                "file": sound_map[scene_idx_0]["file"],
                "offset_sec": 0.3,
            }
        else:
            if "sound_effect" not in scene:
                scene["sound_effect"] = None

    return scenes


def _extract_clip_map(scenario_text: str) -> dict[int, dict]:
    """시나리오 텍스트에서 문장 순서별 clip footage 정보를 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#effect clip 파일명) 프리픽스가
    붙은 문장의 순번(0-based)과 clip footage 정보를 매핑한 dict를 반환한다.
    """
    clip_map: dict[int, dict] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        clip_footage, _ = _parse_clip_prefix(line)
        if clip_footage is not None:
            clip_map[sentence_idx] = clip_footage
        sentence_idx += 1
    return clip_map


def _apply_clip_footage(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 clip footage 정보를 적용한다.

    Gemini는 (#effect ...) 프리픽스를 제거한 순수 나레이션 문장을 반환하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 footage 정보를 주입한다.

    직접 생성 씬(_direct=True)은 footage가 이미 채워져 있으므로 건너뜀.
    """
    clip_map = _extract_clip_map(scenario_text)
    if not clip_map:
        return scenes

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, clip_footage in clip_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        scene = sorted_scenes[scene_idx_0]
        if scene.get("_direct"):
            continue  # 직접 생성 씬은 footage 덮어쓰기 금지
        scene["footage"] = clip_footage

    return scenes


def _extract_sync_map(scenario_text: str) -> dict[int, dict]:
    """시나리오 텍스트에서 문장 순서별 sync footage 정보를 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#sync ...) 프리픽스가
    붙은 문장의 순번(0-based)과 sync footage 정보를 매핑한 dict를 반환한다.
    """
    sync_map: dict[int, dict] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        sync_footage, _ = _parse_sync_prefix(line)
        if sync_footage is not None:
            sync_map[sentence_idx] = sync_footage
        sentence_idx += 1
    return sync_map


def _apply_sync_footage(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 sync footage 정보를 적용한다.

    Gemini는 (#sync ...) 프리픽스를 제거한 순수 나레이션 문장을 반환하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 footage 정보를 주입한다.

    scene_id 기반 매핑은 Gemini가 line_number를 잘못 반환하는 경우 불일치가
    발생하므로, scenes를 scene_id 순으로 정렬한 뒤 순서 인덱스로 매핑한다.

    직접 생성 씬(_direct=True)은 footage가 이미 채워져 있으므로 건너뜀.
    """
    sync_map = _extract_sync_map(scenario_text)
    if not sync_map:
        return scenes

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, sync_footage in sync_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        scene = sorted_scenes[scene_idx_0]
        if scene.get("_direct"):
            continue  # 직접 생성 씬은 footage 덮어쓰기 금지
        scene["footage"] = sync_footage

    return scenes


def _extract_edit_map(scenario_text: str) -> dict[int, dict]:
    """시나리오 텍스트에서 문장 순서별 edit footage 정보를 추출한다.

    시나리오의 각 줄(=문장)을 순서대로 검사하여, (#edit ...) 프리픽스가
    붙은 문장의 순번(0-based)과 edit footage 정보를 매핑한 dict를 반환한다.
    """
    edit_map: dict[int, dict] = {}
    sentence_idx = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        edit_footage, _ = _parse_edit_prefix(line)
        if edit_footage is not None:
            edit_map[sentence_idx] = edit_footage
        sentence_idx += 1
    return edit_map


def _apply_edit_footage(scenes: list, scenario_text: str) -> list:
    """Gemini 파싱 결과 씬에 edit footage 정보를 적용한다.

    Gemini는 (#edit ...) 프리픽스를 제거한 순수 나레이션 문장을 반환하므로,
    원본 시나리오 줄 순서(0-based sentence_idx)와 scenes 리스트의 정렬된
    순서 인덱스를 매핑하여 footage 정보를 주입한다.

    scene_id 기반 매핑은 Gemini가 line_number를 잘못 반환하는 경우 불일치가
    발생하므로, scenes를 scene_id 순으로 정렬한 뒤 순서 인덱스로 매핑한다.

    직접 생성 씬(_direct=True)은 footage가 이미 채워져 있으므로 건너뜀.
    """
    edit_map = _extract_edit_map(scenario_text)
    if not edit_map:
        return scenes

    # scenes를 scene_id(=line_number) 순으로 정렬한 순서 인덱스로 매핑
    # sentence_idx(0-based 전체 줄 순번) = 해당 씬의 정렬 순서 인덱스
    sorted_scenes = sorted(scenes, key=lambda s: s.get("scene_id") or 0)

    for scene_idx_0, edit_footage in edit_map.items():
        if scene_idx_0 >= len(sorted_scenes):
            continue
        scene = sorted_scenes[scene_idx_0]
        if scene.get("_direct"):
            continue  # 직접 생성 씬은 footage 덮어쓰기 금지
        scene["footage"] = edit_footage

    return scenes


def run(scenario_path: str, force: bool = False, *, extract_tags: bool = True) -> list:
    """시나리오 파일을 파싱하여 ScenePlan 배열 반환.

    이미 캐시가 있고 시나리오가 동일하면 재파싱 건너뜀.
    force=True면 항상 재실행.

    Args:
        scenario_path: 시나리오 텍스트 파일 경로
        force: True면 캐시를 무시하고 재실행
        extract_tags: True면 narration_text 앞쪽 `[Tag1][Tag2]` 패턴을 자동
            추출하여 SceneCard.tags에 주입 (빌트인 사전 기준). False면 태그
            토큰을 본문 그대로 두고 tags 필드도 비운다(빈 리스트).
    """
    scenario_text = Path(scenario_path).read_text(encoding="utf-8")
    current_hash = text_hash(scenario_text)
    known_tag_ids = _load_builtin_tag_ids() if extract_tags else set()

    # 캐시 확인
    if not force:
        cached = load_json(CACHE_PATH)
        if cached and cached.get("_hash") == current_hash:
            console.print("[dim]  Phase 1: 시나리오 변경 없음 — 캐시 사용[/dim]")
            scenes = _suggest_footage(cached["scenes"])
            # 구버전 캐시에는 tags가 없을 수 있어 기본값 보장
            for s in scenes:
                s.setdefault("tags", [])
            save_json(CACHE_PATH, {"_hash": current_hash, "scenes": scenes})
            return scenes

    console.print("[bold cyan]Phase 1[/bold cyan] 시나리오 분석 중...")

    # 줄을 순회하면서 (#effect ...) 프리픽스 유무로 분기:
    # - 프리픽스 있는 줄: 직접 씬 dict 생성 (Gemini 스킵)
    # - 프리픽스 없는 줄: Gemini 배치에 포함
    direct_scenes: list[dict] = []   # 직접 생성된 씬 (프리픽스 있음)
    gemini_lines: list[str] = []     # Gemini에 보낼 [N] 번호가 붙은 정제 줄
    line_number = 0
    for line in scenario_text.splitlines():
        line = line.strip()
        if not line:
            continue
        line_number += 1

        # [N] 번호 prefix 제거 (시나리오 파일에 [1] 형태로 작성된 경우)
        line = re.sub(r"^\[\d+\]\s*", "", line)

        # (#effect ...) / (#edit ...) / (#sync ...) 프리픽스 유무 확인 — kenburns → clip → multiscreen → title_gen → edit → sync 순서 시도
        kenburns_footage, _ = _parse_kenburns_prefix(line)
        clip_footage, _ = _parse_clip_prefix(line)
        multiscreen_footage, _ = _parse_multiscreen_prefix(line)
        title_gen_footage, _ = _parse_title_gen_prefix(line)
        edit_footage, _ = _parse_edit_prefix(line)
        sync_footage, _ = _parse_sync_prefix(line)

        has_effect_prefix = any([
            kenburns_footage is not None,
            clip_footage is not None,
            multiscreen_footage is not None,
            title_gen_footage is not None,
            edit_footage is not None,
            sync_footage is not None,
        ])

        if has_effect_prefix:
            # 프리픽스 파싱: kenburns → clip → multiscreen → title_gen → edit → sync 순서로 첫 매칭 사용
            if kenburns_footage is not None:
                footage = kenburns_footage
                remaining = line
                _, remaining = _parse_kenburns_prefix(remaining)
            elif clip_footage is not None:
                footage = clip_footage
                remaining = line
                _, remaining = _parse_clip_prefix(remaining)
            elif multiscreen_footage is not None:
                footage = multiscreen_footage
                remaining = line
                _, remaining = _parse_multiscreen_prefix(remaining)
            elif title_gen_footage is not None:
                footage = title_gen_footage
                remaining = line
                _, remaining = _parse_title_gen_prefix(remaining)
            elif edit_footage is not None:
                footage = edit_footage
                remaining = line
                _, remaining = _parse_edit_prefix(remaining)
            else:
                footage = sync_footage
                remaining = line
                _, remaining = _parse_sync_prefix(remaining)

            # (#tts "...") 프리픽스가 있으면 narration_text에 TTS 텍스트 적용
            tts_text, remaining = _parse_tts_prefix(remaining)
            # 남은 텍스트에서 sound 프리픽스 제거 (sound는 _apply_sound_effect가 별도 처리)
            _, remaining = _parse_sound_prefix(remaining)
            # 앞쪽 [Tag1][Tag2] 태그 토큰 제거 (tags 자체는 _apply_tags에서 주입)
            if extract_tags:
                remaining = _strip_leading_tags(remaining, known_tag_ids)
            narration = remaining.strip()

            # (#tts ...) 가 있으면 그 값을 narration_text로 사용
            if tts_text is not None:
                narration_text = tts_text
            else:
                narration_text = narration

            # title: narration_text 앞 10자 또는 footage 파일명
            if narration_text:
                title = narration_text[:10]
            elif isinstance(footage, dict) and footage.get("type") == "clip":
                title = footage.get("file", "")[:10]
            elif isinstance(footage, dict) and footage.get("type") == "kenburns":
                title = footage.get("image", "")[:10]
            elif isinstance(footage, dict) and footage.get("type") == "title_gen":
                title = footage.get("options", {}).get("text", "")[:10]
            elif isinstance(footage, dict) and footage.get("type") == "edit":
                title = footage.get("description", footage.get("file", ""))[:10]
            elif isinstance(footage, list) and footage:
                title = footage[0][:10]
            elif isinstance(footage, dict) and footage.get("type") == "multiscreen":
                files = footage.get("files", [])
                title = files[0][:10] if files else ""
            else:
                title = ""

            scene = {
                "line_number": line_number,
                "scene_id": line_number,
                "title": title,
                "description": narration_text,
                "mood": "",
                "keywords": [],
                "duration_hint_sec": max(3, len(narration_text) // 5),
                "narration_text": narration_text,
                "footage": footage,
                "sound_effect": None,
                "_direct": True,  # 직접 생성 씬 마커 (_apply_* 함수에서 덮어쓰기 방지용)
            }
            direct_scenes.append(scene)
        else:
            # 프리픽스 없는 줄 → Gemini 배치에 포함
            # (#tts ...), (#effect ...), (#edit ...) 프리픽스 모두 제거한 뒤 [N] 번호 부착
            _, cleaned = _parse_tts_prefix(line)
            _, cleaned = _parse_multiscreen_prefix(cleaned)
            _, cleaned = _parse_kenburns_prefix(cleaned)
            _, cleaned = _parse_title_gen_prefix(cleaned)
            _, cleaned = _parse_clip_prefix(cleaned)
            _, cleaned = _parse_edit_prefix(cleaned)
            _, cleaned = _parse_sync_prefix(cleaned)
            _, cleaned = _parse_sound_prefix(cleaned)
            # 앞쪽 [Tag1][Tag2] 태그 토큰 제거 — Gemini 본문 분석에서 제외 (tags는 _apply_tags에서 주입)
            if extract_tags:
                cleaned = _strip_leading_tags(cleaned, known_tag_ids)
            gemini_lines.append(f"[{line_number}] {cleaned}")

    # Gemini 파싱: 프리픽스 없는 줄만 처리
    if gemini_lines:
        cleaned_scenario = "\n".join(gemini_lines)
        prompt = USER_PROMPT_TEMPLATE.format(scenario=cleaned_scenario)
        raw = generate_json(prompt, system=SYSTEM_PROMPT)

        try:
            gemini_scenes = json.loads(raw)
            if isinstance(gemini_scenes, dict):
                # 최상위가 dict로 감싸진 경우 대응
                gemini_scenes = next(iter(gemini_scenes.values()))
        except json.JSONDecodeError as e:
            console.print(f"[red]❌ Gemini 응답을 파싱할 수 없어요: {e}[/red]")
            raise
    else:
        gemini_scenes = []

    # 두 결과(직접 생성 씬 + Gemini 파싱 씬)를 줄 번호(scene_id) 순서로 합산
    all_scenes = direct_scenes + gemini_scenes
    all_scenes.sort(key=lambda s: s.get("line_number") or s.get("scene_id") or 0)

    scenes = _validate_and_fix(all_scenes)
    # 커스텀 TTS narration_text 덮어쓰기 (원본 시나리오 기준, 가장 먼저 적용)
    # 직접 생성 씬은 _apply_tts_narration 내부에서 _direct 마커로 건너뜀
    scenes = _apply_tts_narration(scenes, scenario_text)
    # footage 적용 우선순위 (낮음 → 높음 순서로 호출, 나중에 호출될수록 최종 적용됨):
    # multiscreen → title_gen → edit → clip → kenburns
    # Gemini 자동 매칭 < multiscreen < title_gen < edit < clip < kenburns
    # 직접 생성 씬은 각 _apply_* 함수 내부에서 _direct 마커로 건너뜀
    # multiscreen footage 배열 적용 (원본 시나리오 기준)
    scenes = _apply_multiscreen_footage(scenes, scenario_text)
    # title_gen footage 적용 (원본 시나리오 기준, multiscreen보다 높은 우선순위)
    scenes = _apply_title_gen_footage(scenes, scenario_text)
    # edit footage 적용 (원본 시나리오 기준, title_gen보다 높은 우선순위)
    scenes = _apply_edit_footage(scenes, scenario_text)
    # sync footage 적용 (원본 시나리오 기준, edit보다 높은 우선순위)
    scenes = _apply_sync_footage(scenes, scenario_text)
    # clip footage 적용 (원본 시나리오 기준, sync보다 높은 우선순위)
    scenes = _apply_clip_footage(scenes, scenario_text)
    # kenburns footage 적용 (원본 시나리오 기준, 최고 우선순위 — title_gen·clip·multiscreen 모두 덮어씀)
    scenes = _apply_kenburns_footage(scenes, scenario_text)
    # sound_effect 적용 (footage와 독립적으로 처리 — 직접 생성 씬 포함 전체 적용)
    scenes = _apply_sound_effect(scenes, scenario_text)
    # 씬 태그 적용 (원본 시나리오 기준, footage 무관 — 직접 생성 씬 포함 전체 적용)
    # extract_tags=False여도 _apply_tags는 모든 씬에 tags=[] 기본값을 보장
    if extract_tags:
        scenes = _apply_tags(scenes, scenario_text, known_tag_ids)
    else:
        for s in scenes:
            s.setdefault("tags", [])
    # footage 미지정 씬에 대해 단일 footage 추천
    scenes = _suggest_footage(scenes)

    # 저장 전 내부 마커 _direct 필드 제거
    for s in scenes:
        s.pop("_direct", None)

    save_json(CACHE_PATH, {"_hash": current_hash, "scenes": scenes})

    return scenes


def print_summary(scenes: list, project_root: str) -> None:
    """씬 목록 요약을 콘솔에 출력."""
    total_sec = sum(s.get("duration_hint_sec", 0) for s in scenes)

    console.print()
    console.rule("[bold green]✅ Phase 1 완료 — 시나리오 씬 분해 결과[/bold green]")

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("씬", style="cyan", width=4)
    table.add_column("제목", width=18)
    table.add_column("길이", width=6)
    table.add_column("footage (추천)", width=24)
    table.add_column("나레이션 (앞 30자)")

    for s in scenes:
        narr = s.get("narration_text", "")[:30] + ("..." if len(s.get("narration_text", "")) > 30 else "")
        raw_footage = s.get("footage")
        if isinstance(raw_footage, list):
            footage = "[multiscreen] " + ", ".join(raw_footage)
        elif isinstance(raw_footage, dict) and raw_footage.get("type") == "multiscreen":
            files = raw_footage.get("files", [])
            layout = raw_footage.get("layout", "auto")
            footage = f"[multiscreen:{layout}] " + ", ".join(files)
        elif isinstance(raw_footage, dict) and raw_footage.get("type") == "kenburns":
            footage = "[kenburns] " + raw_footage.get("image", "")
        elif isinstance(raw_footage, dict) and raw_footage.get("type") == "title_gen":
            text_preview = raw_footage.get("options", {}).get("text", "")[:10]
            footage = f"[title_gen] {text_preview}"
        elif isinstance(raw_footage, dict) and raw_footage.get("type") == "kenburns_copy_img":
            input_file = raw_footage.get("copy_img_options", {}).get("input", "")
            footage = f"[kenburns+copy_img] {input_file}"
        elif isinstance(raw_footage, dict) and raw_footage.get("type") == "clip":
            footage = "[clip] " + raw_footage.get("file", "")
        elif isinstance(raw_footage, dict) and raw_footage.get("type") == "edit":
            desc_preview = raw_footage.get("description", "")[:10]
            footage = f"[edit] {raw_footage.get('file', '')} ({desc_preview})"
        elif isinstance(raw_footage, dict) and raw_footage.get("type") == "sync":
            word = raw_footage.get("word", "")
            inner = raw_footage.get("inner")
            if inner:
                inner_type = inner.get("type", "")
                footage = f"[sync:{word}] inner={inner_type}"
            else:
                footage = f"[sync:{word}]"
        elif raw_footage:
            footage = raw_footage
        else:
            footage = "[dim]없음[/dim]"
        se = s.get("sound_effect")
        sound_label = f" [dim][sound: {se['file']}][/dim]" if se and se.get("file") else ""
        table.add_row(
            str(s["scene_id"]),
            s.get("title", ""),
            f"{s.get('duration_hint_sec', 0)}초",
            footage,
            narr + sound_label,
        )
    console.print(table)
    console.print(f"  총 예상 길이: [bold]{total_sec}초[/bold] ({total_sec // 60}분 {total_sec % 60}초)\n")

    cache_abs = str(Path(project_root) / CACHE_PATH)
    console.print(f"  📄 전체 내용 보기 → [link=cursor://file/{cache_abs}]cursor://file/{cache_abs}[/link]")
    console.print()

    console.print("[bold]검토 항목:[/bold]")
    console.print("  □ 씬 수가 시나리오 의도와 맞나요?")
    console.print("  □ 각 씬 설명이 시각적으로 구체적인가요?")
    console.print("  □ 나레이션 문장이 자연스럽게 읽히나요?")
    console.print(f"  □ 총 길이({total_sec}초)가 목표 영상 길이와 맞나요?")
    console.print(f"  □ [bold yellow]각 씬의 footage 추천이 맞나요? 틀리면 {cache_abs} 에서 직접 수정하세요.[/bold yellow]")


if __name__ == "__main__":
    # 단독 실행: python -m src.scenario_parser <시나리오.txt>
    if len(sys.argv) < 2:
        print("사용법: python -m src.scenario_parser <시나리오.txt>")
        sys.exit(1)

    project_root = str(Path(__file__).parent.parent)
    result = run(sys.argv[1])
    print_summary(result, project_root)
