"""
RenderSpec 단계별 미리보기 캐싱 (#370).

미리보기 파이프라인을 다음 단계로 나누고, 각 단계의 입력 해시를 기반으로
캐시 파일을 보관·재사용한다. 슬라이더 한 칸 변경, 텍스트 한 글자 추가 같은
국소 편집에서 전 단계 ffmpeg 재인코딩을 건너뛰는 것이 목표다.

단계 (#370 본문):
    Stage 1: footage trim (clip_start/end_sec)
    Stage 2: speed/atempo
    Stage 3: filter/motion
    Stage 4: overlay 합성 (text/shape/graphic/image/mosaic)
    Stage 5: narration mux + audio ducking

1·2단계는 푸티지 자체에 대한 비싼 인코딩이므로 캐시 적중 시 효용이 가장 크다.
3~5단계는 입력이 너무 다양해서 1차 도입에서는 1·2단계만 캐시했다.

Stage 6 (최종 결과 캐시, preview-final-cache):
    3~5단계를 단계별로 쪼개는 대신, *완성된 미리보기 mp4 자체* 를 씬 편집 상태 전체
    해시로 캐시한다. POST /preview 는 매 씬 전환마다 전 구간 재인코딩(실측 3.3~3.5초,
    ffmpeg CPU 570%)을 수행하고 있었고, 그 앞단의 trim=HIT 는 중간 산출물일 뿐이라
    체감 지연을 전혀 줄이지 못했다. 최종 산출물을 캐시하면 무편집 씬 전환에서
    ffmpeg 호출이 0 이 된다.

캐시 디렉토리:
    <project>/cache/scene-{scene_id}/

캐시 키 규칙:
    Stage 1: trim-{hash}.mp4  — input fname + mtime + clip_start + clip_end
    Stage 2: speed-{hash}.mp4 — stage1 hash + speed
    Stage 6: final-{hash}.mp4 — 씬 편집 상태 전체 + footage/narration/asset file_signature
                                + 캔버스 해상도 + bg_color + dissolve_pad

LRU/사이즈 제한:
    프로젝트별 1GB. 캐시 디렉토리(`cache/scene-*`)의 mp4 파일이 합쳐서 한계를 넘으면
    atime(접근 시간) 오래된 순으로 삭제한다.

무효화:
    - footage 원본 파일 mtime/size 변경 → 같은 파일명이라도 sig 가 달라 새 hash 가 되고
      과거 캐시는 자연 사장. 별도 trigger 불요. **써드파티 세션에서 푸티지를 직접
      수정/교체한 경우가 정확히 이 경로로 감지된다** (서버가 통보받지 못해도 안전).
    - 캔버스 해상도 변경 → 1·2단계는 캔버스에 종속되지 않으므로 그대로 재사용 가능.
      Stage 6 은 hash 에 캔버스 크기를 포함한다.
    - 명시적 무효화(DELETE /preview-cache) → ``purge_stage(.., "final")`` 로 씬의 최종
      캐시 파일을 모두 제거. 중간(trim/speed) 캐시는 입력 실체 기반이라 유지해도 안전.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ──────────────────────────────────────────────────────────────────────────────
# Hash 계산
# ──────────────────────────────────────────────────────────────────────────────

def _stable_dumps(obj) -> str:
    """딕셔너리 정렬·NaN 미허용 직렬화 — 같은 의미면 항상 같은 해시."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def stage_hash(stage: str, payload: dict) -> str:
    """단계 식별자 + payload 의 SHA256 앞 16 hex.

    한 프로젝트 내에서 충돌 가능성은 2^64 분의 1 (현실적으로 0).
    파일명 길이를 줄이기 위해 16 헥스만 사용한다.
    """
    blob = (stage + "|" + _stable_dumps(payload)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def file_signature(path: str | Path) -> dict:
    """입력 파일의 식별 시그니처(이름 + mtime + size).

    같은 파일이라도 mtime 이 바뀌면 다른 sig 가 되어 캐시 무효화된다.
    파일이 없으면 ``{"missing": True}`` 를 반환해 호출자가 별도 처리할 수 있게 한다.
    """
    p = Path(path)
    try:
        st = p.stat()
        return {
            "name": p.name,
            "mtime": int(st.st_mtime),
            "size": int(st.st_size),
        }
    except FileNotFoundError:
        return {"missing": True}


# ──────────────────────────────────────────────────────────────────────────────
# 캐시 디렉토리 + LRU 정리
# ──────────────────────────────────────────────────────────────────────────────

def scene_cache_dir(project_cache_dir: Path, scene_id: int) -> Path:
    """씬별 preview-stage 캐시 디렉토리. 생성 보장."""
    d = Path(project_cache_dir) / f"scene-{scene_id:03d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_cache_path(project_cache_dir: Path, scene_id: int, stage: str, key: str) -> Path:
    """캐시 경로: <project>/cache/scene-XXX/{stage}-{key}.mp4"""
    return scene_cache_dir(project_cache_dir, scene_id) / f"{stage}-{key}.mp4"


def touch_atime(p: Path) -> None:
    """LRU 정확도를 위해 캐시 적중 시 atime 만 갱신.

    POSIX 의 ``os.utime`` 으로 atime/mtime 을 명시 설정한다. mtime 은 그대로 유지해
    내용 해시 무결성에 영향을 주지 않는다.
    """
    try:
        st = p.stat()
        os.utime(p, (time.time(), st.st_mtime))
    except OSError:
        # 캐시 정리 락 등으로 실패해도 본 흐름에는 영향이 없으므로 침묵.
        pass


DEFAULT_LIMIT_BYTES = 1024 * 1024 * 1024  # 1 GB


def enforce_lru(project_cache_dir: Path, limit_bytes: int = DEFAULT_LIMIT_BYTES) -> int:
    """프로젝트의 ``cache/scene-*/*.mp4`` 합계가 limit_bytes 를 넘으면 atime 오래된 순으로 삭제.

    반환값: 삭제된 바이트 수(보조 로그 용도).
    호출 빈도가 잦으면 부하가 있을 수 있으니 한 번의 미리보기 요청 끝에 1회만 호출하는 것이 권장된다.
    """
    root = Path(project_cache_dir)
    if not root.exists():
        return 0
    files: list[tuple[Path, int, float]] = []
    total = 0
    for sub in root.glob("scene-*"):
        if not sub.is_dir():
            continue
        for f in sub.glob("*.mp4"):
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((f, int(st.st_size), st.st_atime))
            total += int(st.st_size)
    if total <= limit_bytes:
        return 0
    # atime 오래된 순으로 정렬해 limit 까지 삭제.
    files.sort(key=lambda t: t[2])
    removed = 0
    while files and total > limit_bytes:
        f, sz, _ = files.pop(0)
        try:
            f.unlink(missing_ok=True)
            total -= sz
            removed += sz
        except OSError:
            continue
    return removed


# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: footage trim 캐시
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrimStageKey:
    """Stage 1 캐시 키 입력 — 같은 값이면 같은 해시."""
    input_path: str
    input_sig: dict
    clip_start_sec: float
    clip_end_sec: float | None

    def to_payload(self) -> dict:
        return {
            "input": self.input_sig,
            # 동일 footage 라도 fname 변경 검출용으로 명시 보관.
            "fname": Path(self.input_path).name,
            "cs": round(float(self.clip_start_sec), 4),
            "ce": None if self.clip_end_sec is None else round(float(self.clip_end_sec), 4),
        }

    def hash(self) -> str:
        return stage_hash("trim", self.to_payload())


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: speed/atempo 캐시
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpeedStageKey:
    """Stage 2 캐시 키 입력 — 직전 단계 hash + speed factor."""
    prev_hash: str          # Stage 1 hash (또는 trim 이 없을 때 footage file_signature 의 stable hash)
    footage_speed: float
    canvas_w: int           # speed 단계에서 fit 계산이 들어가므로 캔버스 종속.
    canvas_h: int
    fit: str                # "contain" / "cover" / "stretch"
    # bg-preview-fix — letterbox pad 색이 speed 결과에 구워지므로 캐시 키에 포함.
    # 기본값 "black" 이면 payload 에서 접미사를 생략해 기존 캐시 키와 바이트 동일.
    bg_color: str = "black"
    # 재생 속도 모드 — constant(기본)면 payload 에 넣지 않아 기존 캐시 키와 바이트 동일.
    speed_mode: str = "constant"
    ramp_hold: float = 0.2
    ramp_accel: float = 0.3
    ramp_base: float = 1.0

    def to_payload(self) -> dict:
        payload = {
            "prev": self.prev_hash,
            "speed": round(float(self.footage_speed), 4),
            "cw": int(self.canvas_w),
            "ch": int(self.canvas_h),
            "fit": self.fit,
        }
        # black(기본)일 때는 키를 추가하지 않아 기존 캐시가 그대로 유효 — 회귀 안전.
        if self.bg_color and self.bg_color != "black":
            payload["bg"] = self.bg_color
        if self.speed_mode in ("ramp", "ramp_down"):
            payload["mode"] = self.speed_mode
            payload["hold"] = round(float(self.ramp_hold), 4)
            payload["accel"] = round(float(self.ramp_accel), 4)
            # 기본 배속 1.0(기본)이면 키를 넣지 않아 기존 ramp 캐시 키와 바이트 동일.
            if abs(float(self.ramp_base) - 1.0) > 1e-9:
                payload["base"] = round(float(self.ramp_base), 4)
        return payload

    def hash(self) -> str:
        return stage_hash("speed", self.to_payload())


# ──────────────────────────────────────────────────────────────────────────────
# Stage 6(최종): 완성된 미리보기 mp4 캐시
# ──────────────────────────────────────────────────────────────────────────────

# 씬 상태 dict 에서 미리보기 결과에 *영향을 주지 않는* 필드.
#
# 정책(보수적): 여기 나열되지 않은 모든 씬 필드는 캐시 키에 포함한다. 즉 새 효과
# 필드가 모델에 추가되면 자동으로 키에 들어간다 — "사용자가 편집했는데 옛 영상이
# 계속 보이는" 최악의 실패 모드가 신규 필드 누락으로 재발하지 않도록 하기 위함.
#
# 제외 근거:
#   - 순수 메타/기획 텍스트(title/description/mood/keywords/tags/comments)는 렌더 미반영.
#   - match: 매칭 근거 메타. 실제 사용되는 값은 footage 에 복사되어 있다.
#   - tts / narration_segments 의 상태 필드(audio_url/status/synced/duration_sec)는
#     WAV 파일 자체의 file_signature 로 대체 검증하므로 중복.
#   - subtitle_text / sbv_text 는 SBV 자막 전용 — 미리보기 영상에 그려지지 않는다.
#   - beep_* / sfx_* 는 POST 미리보기 경로가 합성하지 않는다(최종 렌더 전용).
FINAL_STAGE_IGNORED_SCENE_FIELDS = frozenset({
    "title", "description", "mood", "keywords", "tags", "comments",
    "match", "tts", "subtitle_text", "narration_rev",
    "sfx_asset_id", "sfx_volume", "sfx_offset_sec",
    "beep_on_narration_end", "beep_volume",
    "is_insert_image_scene",
})

# narration_segments 안에서 무시할 필드 — WAV file_signature 로 대체 검증되는 상태값.
# (text/offset_sec/duration_sec/voice_name 등 합성 결과에 영향을 주는 값은 유지)
FINAL_STAGE_IGNORED_SEGMENT_FIELDS = frozenset({
    "audio_url", "status", "synced", "error_message", "sbv_text",
})


def _prune_scene_state(scene_state: dict) -> dict:
    """씬 상태 dict 에서 미리보기 결과에 영향 없는 필드만 제거한 사본을 반환."""
    pruned = {
        k: v for k, v in scene_state.items()
        if k not in FINAL_STAGE_IGNORED_SCENE_FIELDS
    }
    segs = pruned.get("narration_segments")
    if isinstance(segs, list):
        pruned["narration_segments"] = [
            {k: v for k, v in seg.items() if k not in FINAL_STAGE_IGNORED_SEGMENT_FIELDS}
            if isinstance(seg, dict) else seg
            for seg in segs
        ]
    return pruned


@dataclass(frozen=True)
class FinalStageKey:
    """최종 미리보기 캐시 키 입력.

    가장 중요한 실패 모드는 "사용자가 편집했는데 옛 영상이 계속 보이는 것"이다.
    따라서 씬 편집 상태 전체(``scene_state``)를 통째로 키에 넣고, 여기에
    서버 밖(써드파티 세션/직접 편집)에서 바뀔 수 있는 파일들의 실체 시그니처
    (``file_signature`` = name+mtime+size)를 더한다. 웹 API 를 거치지 않은
    외부 파일 교체도 mtime/size 변화로 자동 감지된다.

    Attributes:
        scene_state: ``SceneCard.model_dump()`` 에서 렌더 무관 필드만 제거한 dict.
        footage_sig: footage(또는 프록시) 원본 파일 시그니처. **외부 수정 감지의 핵심.**
        narration_sigs: 참조하는 narration WAV 들의 시그니처 목록(TTS 재생성 감지).
        asset_sigs: 이미지 오버레이 등 참조 자산 파일 시그니처 목록.
        canvas_w/canvas_h: 미리보기 출력 해상도.
        output_h: 원본 출력 높이 — 텍스트 오버레이 스케일 비율 계산에 쓰임.
        bg_color: 프로젝트 배경색(letterbox pad).
        dissolve_pad: 씬 인덱스/엣지 여부에서 유도된 dissolve 여유(초).
        scene_index: 씬 인덱스(엣지 판정 근거 자체도 키에 보관).
        scene_count: 전체 씬 수.
    """
    scene_state: dict
    footage_sig: dict
    narration_sigs: list
    asset_sigs: list
    canvas_w: int
    canvas_h: int
    output_h: int
    bg_color: str
    dissolve_pad: float
    scene_index: int
    scene_count: int

    def to_payload(self) -> dict:
        return {
            "scene": _prune_scene_state(self.scene_state),
            "footage": self.footage_sig,
            "narration": list(self.narration_sigs),
            "assets": list(self.asset_sigs),
            "cw": int(self.canvas_w),
            "ch": int(self.canvas_h),
            "oh": int(self.output_h),
            "bg": self.bg_color,
            "pad": round(float(self.dissolve_pad), 4),
            "idx": int(self.scene_index),
            "n": int(self.scene_count),
        }

    def hash(self) -> str:
        return stage_hash("final", self.to_payload())


# ──────────────────────────────────────────────────────────────────────────────
# 캐시 적중 헬퍼 + 히트/미스 로그용 결과 구조체
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class StageCacheResult:
    """캐시 조회 결과. cached 가 None 이면 미스."""
    stage: str
    key_hash: str
    cached: Path | None

    @property
    def hit(self) -> bool:
        return self.cached is not None and self.cached.exists() and self.cached.stat().st_size > 0


def lookup(project_cache_dir: Path, scene_id: int, stage: str, key_hash: str) -> StageCacheResult:
    """캐시 적중 여부 확인. 적중 시 atime 갱신.

    호출 측은 ``result.hit`` 으로 분기하고, 적중이면 ``result.cached`` 를 그 단계 산출물로 사용한다.
    """
    p = stage_cache_path(project_cache_dir, scene_id, stage, key_hash)
    if p.exists() and p.stat().st_size > 0:
        touch_atime(p)
        return StageCacheResult(stage=stage, key_hash=key_hash, cached=p)
    return StageCacheResult(stage=stage, key_hash=key_hash, cached=None)


def store(project_cache_dir: Path, scene_id: int, stage: str, key_hash: str, source_path: Path | str) -> Path:
    """단계 산출물을 캐시 경로로 옮긴다(또는 복사).

    원자성을 위해 임시 경로에 먼저 쓰고 마지막에 rename. 같은 캐시가 이미 있으면 덮어쓴다.
    """
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"stage 산출물이 존재하지 않습니다: {src}")
    dst = stage_cache_path(project_cache_dir, scene_id, stage, key_hash)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    # cross-device 가능성 대비 copy → rename. 일반적으로 같은 디스크라 즉시.
    import shutil
    shutil.copyfile(str(src), str(tmp))
    os.replace(str(tmp), str(dst))
    return dst


def purge_stage(project_cache_dir: Path, scene_id: int, stage: str) -> int:
    """한 씬의 특정 단계 캐시 파일을 모두 삭제한다. 반환값: 삭제된 파일 수.

    명시적 무효화 경로(DELETE /scenes/{id}/preview-cache)에서 최종 캐시를 비우는 데 쓴다.
    디렉토리가 없으면 0 을 반환하고 아무 것도 하지 않는다(캐시 생성 부작용 없음).
    """
    d = Path(project_cache_dir) / f"scene-{scene_id:03d}"
    if not d.exists():
        return 0
    removed = 0
    for f in d.glob(f"{stage}-*.mp4"):
        try:
            f.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# ──────────────────────────────────────────────────────────────────────────────
# 진단용 — 효용 추적 로그 라인 생성
# ──────────────────────────────────────────────────────────────────────────────

def log_line(scene_id: int, results: Iterable[StageCacheResult]) -> str:
    """효용 추적용 단일 줄 로그.

    예: ``[preview-cache] scene=3 trim=HIT(abcdef0123) speed=MISS(112233)``
    """
    parts = [f"scene={scene_id}"]
    for r in results:
        parts.append(f"{r.stage}={'HIT' if r.hit else 'MISS'}({r.key_hash})")
    return "[preview-cache] " + " ".join(parts)
