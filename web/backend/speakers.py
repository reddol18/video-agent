"""다중 화자 — 씬 태그에서 화자를 읽어 TTS 목소리를 결정한다.

규칙은 한 곳에만 둔다. TTS 생성, 미리듣기, 프론트 표시가 모두 같은 함수를 쓰도록
해서 "미리듣기는 A 목소리인데 렌더는 B" 같은 어긋남을 원천 차단한다.

태그 문법:
    speaker:홍길동      → 화자 "홍길동"
기존 효과 태그(horror, witty ...)와 섞이지 않도록 접두사로 구분한다.

목소리 우선순위 (좁은 것이 이긴다):
    1. segment.voice_name    — 사용자가 그 문장만 콕 집어 지정
    2. 씬의 speaker 태그     — 이 기능
    3. 요청/프로젝트 기본    — 기존 동작
"""

from __future__ import annotations

from typing import Iterable, Optional

SPEAKER_TAG_PREFIX = "speaker:"


def normalize_name(name: str) -> str:
    """화자명 비교용 정규화. 앞뒤 공백과 대소문자 차이는 무시한다."""
    return (name or "").strip().casefold()


def speaker_tag(name: str) -> str:
    """화자명을 씬 태그 문자열로."""
    return f"{SPEAKER_TAG_PREFIX}{(name or '').strip()}"


def extract_speaker_name(tags: Iterable[str] | None) -> Optional[str]:
    """씬 태그 목록에서 화자명을 뽑는다. 없으면 None.

    씬당 화자는 1명이다. 태그가 여러 개면 첫 번째만 쓴다 — 뒤엣것을 조용히
    덮어쓰면 어느 목소리가 나올지 예측할 수 없기 때문에, 순서대로 첫 태그를
    택하는 규칙을 명시적으로 고정한다.
    """
    for raw in tags or []:
        if not isinstance(raw, str):
            continue
        t = raw.strip()
        if len(t) > len(SPEAKER_TAG_PREFIX) and t[: len(SPEAKER_TAG_PREFIX)].casefold() == SPEAKER_TAG_PREFIX:
            name = t[len(SPEAKER_TAG_PREFIX):].strip()
            if name:
                return name
    return None


def find_speaker(speakers: Iterable, name: str | None):
    """화자 사전에서 이름으로 찾기. 못 찾으면 None."""
    if not name:
        return None
    key = normalize_name(name)
    for sp in speakers or []:
        if normalize_name(getattr(sp, "name", "")) == key:
            return sp
    return None


def resolve_scene_voice(scene, project, fallback_voice: str | None = None) -> tuple[str, Optional[str]]:
    """씬 하나에 적용할 목소리를 정한다.

    반환: (voice_name, 사용된 화자명 또는 None)

    화자 태그가 있지만 사전에 없는 이름이면 기본 목소리로 떨어진다 — 오타 하나로
    TTS 생성이 통째로 실패하는 것보다, 기본 목소리로 만들고 어느 씬이 그랬는지
    알려주는 편이 낫다. 호출부가 경고를 띄울 수 있도록 이름은 그대로 돌려준다.
    """
    base = fallback_voice or getattr(project, "tts_voice", "") or ""
    name = extract_speaker_name(getattr(scene, "tags", None))
    if not name:
        return base, None
    sp = find_speaker(getattr(project, "speakers", None), name)
    if sp is None:
        return base, name  # 미등록 — 호출부에서 경고
    return (sp.voice_name or base), sp.name


def resolve_segment_voice(segment, scene, project, fallback_voice: str | None = None) -> str:
    """segment 하나에 실제로 쓸 목소리. 위 우선순위를 그대로 적용한다."""
    seg_voice = getattr(segment, "voice_name", None)
    if seg_voice:
        return seg_voice
    voice, _ = resolve_scene_voice(scene, project, fallback_voice)
    return voice
