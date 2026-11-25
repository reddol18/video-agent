"""Phase 5 — 클립 편집기

ClipSelection[]에 따라 클립을 커팅하고 하나의 무음 영상으로 연결한다.
산출물: output/clips/scene_NNN.mp4, output/muted_output.mp4, cache/timeline.json
"""

import math
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

from .utils.file_cache import load_json, save_json
from .utils.ffmpeg_utils import build_grid_compare, build_xstack_multiscreen, cut_clip, get_duration, get_video_size, image_to_clip
from .utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

NARRATION_TIMELINE_PATH = "output/narration/narration_timeline.json"

# multiscreen 슬롯에서 이미지로 처리할 확장자 목록
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".jfif"}


@lru_cache(maxsize=1)
def _detect_h264_encoder() -> str:
    """이 머신에서 실제로 동작하는 H.264 인코더 이름을 한 번만 탐지해 캐시한다.

    `ffmpeg -encoders` 목록에 있다고 쓸 수 있는 것이 아니다(GPU/드라이버가 없으면
    빌드에는 포함돼도 런타임에 실패한다). 그래서 1 프레임짜리 더미 인코딩을 실제로
    돌려 보고 성공하는 것을 고른다.

    우선순위: 플랫폼 하드웨어 인코더 → libx264(소프트웨어, 어디서나 동작).
    """
    import platform
    import subprocess
    import tempfile

    if platform.system() == "Darwin":
        candidates = ["h264_videotoolbox"]       # Apple Silicon
    elif platform.system() == "Windows":
        candidates = ["h264_nvenc", "h264_qsv", "h264_amf"]  # NVIDIA / Intel / AMD
    else:
        candidates = ["h264_nvenc", "h264_vaapi"]

    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "probe.mp4")
        for enc in candidates:
            try:
                r = subprocess.run(
                    [ffmpeg_bin(), "-hide_banner", "-v", "error",
                     "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
                     "-c:v", enc, "-y", out],
                    capture_output=True, timeout=60,
                )
                if r.returncode == 0:
                    return enc
            except Exception:
                continue
    return "libx264"


def _video_encoder_args(quality: int = 60) -> list[str]:
    """FFmpeg 비디오 인코더 인자를 반환한다.

    하드웨어 인코더가 있으면 그것을 써서 소프트웨어 `libx264` 대비 5~10배 빠른
    인코딩을 확보하고, 없으면 `libx264` 로 폴백한다. 어떤 인코더가 실제로 동작하는지는
    `_detect_h264_encoder()` 가 런타임에 더미 인코딩으로 판정한다.

    ⚠ 품질 파라미터는 인코더마다 **방향이 반대**다. 이걸 혼동하면 화질이 무너진다.
      - videotoolbox `-q:v` : 0~100, **클수록** 고품질
      - qsv `-q:v` (= -global_quality) : 1~51, **작을수록** 고품질 (crf 와 같은 방향)
      - nvenc `-cq`, libx264 `-crf`    : 0~51, **작을수록** 고품질
      (실제 사고: qsv 에 q:v 60 을 그대로 넘겨 1080p 결과물이 4MB 로 뭉개졌다.)

    이 함수의 인자 `quality` 는 videotoolbox 스케일(클수록 좋음)을 기준으로 한다.
    기준: q:v 60 ≈ crf 18(원본 화질), q:v 50 ≈ crf 23(미리보기).
    """
    enc = _detect_h264_encoder()
    q = int(quality)
    # videotoolbox 스케일(높을수록 좋음) → crf 스케일(낮을수록 좋음) 환산.
    crf = max(0, min(51, round(48 - q * 0.5)))

    if enc == "h264_videotoolbox":
        return ["-c:v", enc, "-q:v", str(q), "-b:v", "0"]
    if enc == "h264_qsv":
        # qsv 는 crf 와 같은 방향이므로 환산값을 그대로 쓴다.
        return ["-c:v", enc, "-global_quality", str(crf), "-preset", "medium"]
    if enc == "h264_nvenc":
        return ["-c:v", enc, "-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-preset", "p4"]
    if enc == "h264_amf":
        return ["-c:v", enc, "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf)]

    return ["-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast"]


# ── 1D Perlin Noise (#224 — 그래픽 오버레이 자연스러운 흔들림) ───────────
# 외부 의존성 없이 표준 Perlin 알고리즘을 구현한다.
# - 출력 범위: [-1, 1] (이상치는 클램프)
# - seed별 permutation table로 서로 다른 noise sequence 생성
# - 같은 (t, seed)는 항상 같은 값을 반환 — deterministic

_PERLIN_PERMS: dict[int, list[int]] = {}


def _perlin_get_perm(seed: int) -> list[int]:
    """seed별 permutation table을 캐시하여 반환한다. P는 길이 512(0~255 반복)."""
    if seed in _PERLIN_PERMS:
        return _PERLIN_PERMS[seed]
    import random as _rand

    rng = _rand.Random(int(seed))
    base = list(range(256))
    rng.shuffle(base)
    perm = base + base  # 길이 512, mod 256 보조용
    _PERLIN_PERMS[seed] = perm
    return perm


def _perlin_fade(t: float) -> float:
    """Perlin fade 곡선 6t^5 - 15t^4 + 10t^3 (C2 연속)."""
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _perlin_grad1(hash_val: int, x: float) -> float:
    """1D gradient: hash의 LSB가 0이면 +x, 1이면 -x."""
    return x if (hash_val & 1) == 0 else -x


def _perlin_1d(t: float, seed: int = 0) -> float:
    """1차원 Perlin noise. t는 임의의 float, 반환은 대략 [-1, 1].

    표준 알고리즘:
      x0 = floor(t), x1 = x0 + 1
      g0 = grad(P[x0]), g1 = grad(P[x1])
      n0 = g0 * (t - x0), n1 = g1 * (t - x1)
      u = fade(t - x0)
      return lerp(n0, n1, u)
    """
    perm = _perlin_get_perm(int(seed))
    x_floor = math.floor(t)
    xi = int(x_floor) & 255
    xf = t - x_floor

    h0 = perm[xi]
    h1 = perm[xi + 1]
    n0 = _perlin_grad1(h0, xf)
    n1 = _perlin_grad1(h1, xf - 1.0)

    u = _perlin_fade(xf)
    val = n0 + u * (n1 - n0)
    # 1D Perlin은 이론상 |val| <= 1이지만, 안전을 위해 클램프
    if val > 1.0:
        val = 1.0
    elif val < -1.0:
        val = -1.0
    return val


def _build_kenburns_group_params(selections: list) -> dict:
    """연속된 kenburns/kenburns_copy_img 씬 그룹을 탐지하여 씬별 scale_start/scale_end를 반환한다.

    동일 이미지 파일을 참조하는 인접 kenburns 씬들을 하나의 그룹으로 묶고,
    그룹 전체 duration에 걸쳐 scale 1.0 → 1.3을 선형 보간한다.
    그룹이 끊기면(다른 effect 또는 다른 이미지) 리셋된다.

    반환값: {scene_id: {"scale_start": float, "scale_end": float}}
    """
    KENBURNS_EFFECTS = {"kenburns", "kenburns_copy_img"}
    GROUP_START_SCALE = 1.0
    GROUP_END_SCALE = 1.3

    narr_data = load_json(NARRATION_TIMELINE_PATH)
    duration_map: dict[int, float] = {}
    if narr_data and narr_data.get("entries"):
        for entry in narr_data["entries"]:
            sid = entry.get("scene_id")
            dur = entry.get("duration_sec")
            if dur is None:
                end = entry.get("end_sec", 0.0)
                start = entry.get("start_sec", 0.0)
                dur = end - start if end > start else 0.0
            if sid is not None and dur is not None:
                duration_map[sid] = float(dur)

    def _get_image_key(sel: dict) -> str:
        effect = sel.get("effect", "")
        if effect == "kenburns":
            return sel.get("kenburns_image", sel.get("source_file", ""))
        if effect == "kenburns_copy_img":
            copy_img = sel.get("copy_img_options", {})
            return copy_img.get("input", sel.get("source_file", ""))
        return ""

    def _get_tts_dur(sel: dict) -> float:
        sid = sel["scene_id"]
        return float(
            sel.get("tts_duration_sec")
            or duration_map.get(sid)
            or sel.get("clip_duration_sec", 5.0)
        )

    # 1차 패스: 연속 그룹 경계 및 각 그룹의 총 duration 계산
    # groups: list of list of scene_id
    groups: list[list] = []
    cur_group: list = []
    cur_image: str | None = None

    for sel in selections:
        effect = sel.get("effect", "")
        if effect not in KENBURNS_EFFECTS:
            if cur_group:
                groups.append(cur_group)
                cur_group = []
            cur_image = None
            continue

        img_key = _get_image_key(sel)
        if img_key != cur_image:
            if cur_group:
                groups.append(cur_group)
            cur_group = []
            cur_image = img_key

        cur_group.append(sel)

    if cur_group:
        groups.append(cur_group)

    # 2차 패스: 그룹별 씬 scale 구간 계산
    result: dict[int, dict] = {}
    for group in groups:
        total_dur = sum(_get_tts_dur(sel) for sel in group)
        if total_dur <= 0:
            total_dur = 1.0

        t_cursor = 0.0
        for sel in group:
            sid = sel["scene_id"]
            tts_dur = _get_tts_dur(sel)
            t_start = t_cursor
            t_end = t_cursor + tts_dur

            def scale_at(t: float) -> float:
                return GROUP_START_SCALE + (GROUP_END_SCALE - GROUP_START_SCALE) * (t / total_dur)

            if len(group) == 1:
                # 단독 씬: 기존 기본값 유지
                result[sid] = {"scale_start": 1.0, "scale_end": 1.2}
            else:
                result[sid] = {
                    "scale_start": round(scale_at(t_start), 6),
                    "scale_end": round(scale_at(t_end), 6),
                }
            t_cursor += tts_dur

    return result


def _build_multiscreen_group_offsets(selections: list) -> dict:
    """연속된 multiscreen 씬 그룹을 탐지하여 씬별 트림 오프셋(초)을 반환한다.

    슬롯 파일 목록(순서 포함)이 동일한 인접 multiscreen 씬들을 하나의 그룹으로 묶고,
    그룹 내 각 씬의 오프셋 = 이전 씬들의 TTS duration 합산.
    그룹이 끊기면(다른 effect 또는 슬롯 구성 변경) 오프셋을 0으로 리셋한다.

    반환값: {scene_id: offset_sec}
    """
    narr_data = load_json(NARRATION_TIMELINE_PATH)
    duration_map: dict[int, float] = {}
    if narr_data and narr_data.get("entries"):
        for entry in narr_data["entries"]:
            sid = entry.get("scene_id")
            dur = entry.get("duration_sec")
            if dur is None:
                end = entry.get("end_sec", 0.0)
                start = entry.get("start_sec", 0.0)
                dur = end - start if end > start else 0.0
            if sid is not None and dur is not None:
                duration_map[sid] = float(dur)

    def _slot_key(sel: dict) -> tuple:
        clips = sel.get("multiscreen_clips", [])
        return tuple(mc.get("source_file", "") for mc in clips)

    offsets: dict[int, float] = {}
    prev_slot_key: tuple | None = None
    accumulated: float = 0.0

    for sel in selections:
        scene_id = sel["scene_id"]
        effect = sel.get("effect", "")

        if effect != "multiscreen":
            prev_slot_key = None
            accumulated = 0.0
            continue

        slot_key = _slot_key(sel)
        if slot_key != prev_slot_key:
            accumulated = 0.0
            prev_slot_key = slot_key

        offsets[scene_id] = accumulated

        tts_dur = float(
            sel.get("tts_duration_sec")
            or duration_map.get(scene_id)
            or sel.get("clip_duration_sec", 5.0)
        )
        accumulated += tts_dur

    return offsets


def _build_scene_group_ids(selections: list) -> dict:
    """각 씬이 속한 그룹 ID를 반환한다.

    동일 effect + 동일 source(파일/슬롯/이미지)를 가진 인접 씬들을 같은 그룹으로 묶는다.
    그룹이 다르면 그룹 ID가 달라진다.

    반환값: {scene_id: group_id}  (group_id는 정수, 씬마다 고유하지 않음)
    """
    group_ids: dict[int, int] = {}
    gid = 0
    prev_key: tuple | None = None

    for sel in selections:
        scene_id = sel["scene_id"]
        effect = sel.get("effect", "")

        if effect == "clip":
            key = ("clip", sel.get("source_file", ""))
        elif effect in ("kenburns", "kenburns_copy_img"):
            if effect == "kenburns":
                img = sel.get("kenburns_image", sel.get("source_file", ""))
            else:
                img = sel.get("copy_img_options", {}).get("input", sel.get("source_file", ""))
            key = ("kenburns", img)
        elif effect == "multiscreen":
            clips = sel.get("multiscreen_clips", [])
            key = ("multiscreen", tuple(mc.get("source_file", "") for mc in clips))
        elif effect == "sync":
            key = ("sync", scene_id)  # sync는 항상 별개 그룹 (Time Warping 독립 처리)
        else:
            key = (effect, scene_id)  # title_gen 등은 항상 별개 그룹

        if key != prev_key:
            gid += 1
            prev_key = key

        group_ids[scene_id] = gid

    return group_ids


def _build_multiscreen_group_total_tts(selections: list) -> dict:
    """연속 multiscreen 씬 그룹 각각의 전체 tts_dur 합산을 반환한다.

    슬롯 파일 목록(순서 포함)이 동일한 인접 multiscreen 씬들을 하나의 그룹으로 묶고,
    그룹 내 모든 씬의 tts_dur를 합산하여 각 씬에 매핑한다.

    반환값: {scene_id: total_group_tts_sec}
    """
    narr_data = load_json(NARRATION_TIMELINE_PATH)
    duration_map: dict[int, float] = {}
    if narr_data and narr_data.get("entries"):
        for entry in narr_data["entries"]:
            sid = entry.get("scene_id")
            dur = entry.get("duration_sec")
            if dur is None:
                end = entry.get("end_sec", 0.0)
                start = entry.get("start_sec", 0.0)
                dur = end - start if end > start else 0.0
            if sid is not None and dur is not None:
                duration_map[sid] = float(dur)

    def _slot_key(sel: dict) -> tuple:
        clips = sel.get("multiscreen_clips", [])
        return tuple(mc.get("source_file", "") for mc in clips)

    def _get_tts(sel: dict) -> float:
        sid = sel["scene_id"]
        return float(
            sel.get("tts_duration_sec")
            or duration_map.get(sid)
            or sel.get("clip_duration_sec", 5.0)
        )

    # 연속 그룹 탐지
    groups: list[list[dict]] = []
    cur_group: list[dict] = []
    prev_slot_key: tuple | None = None

    for sel in selections:
        effect = sel.get("effect", "")
        if effect != "multiscreen":
            if cur_group:
                groups.append(cur_group)
                cur_group = []
            prev_slot_key = None
            continue

        slot_key = _slot_key(sel)
        if slot_key != prev_slot_key:
            if cur_group:
                groups.append(cur_group)
            cur_group = []
            prev_slot_key = slot_key

        cur_group.append(sel)

    if cur_group:
        groups.append(cur_group)

    result: dict[int, float] = {}
    for group in groups:
        total_tts = sum(_get_tts(sel) for sel in group)
        for sel in group:
            result[sel["scene_id"]] = total_tts

    return result


def _build_clip_group_offsets(selections: list) -> dict:
    """연속된 (#effect clip) 씬 그룹을 탐지하여 씬별 오프셋(초)을 반환한다.

    동일 source_file을 참조하는 인접 clip 씬들을 하나의 그룹으로 묶고,
    그룹 내 각 씬의 트림 시작 오프셋 = 이전 씬들의 TTS duration 합산.
    그룹이 끊기면(다른 effect 또는 다른 파일) 오프셋을 0으로 리셋한다.

    반환값: {scene_id: offset_sec}
    """
    narr_data = load_json(NARRATION_TIMELINE_PATH)
    duration_map: dict[int, float] = {}
    if narr_data and narr_data.get("entries"):
        for entry in narr_data["entries"]:
            sid = entry.get("scene_id")
            dur = entry.get("duration_sec")
            if dur is None:
                end = entry.get("end_sec", 0.0)
                start = entry.get("start_sec", 0.0)
                dur = end - start if end > start else 0.0
            if sid is not None and dur is not None:
                duration_map[sid] = float(dur)

    def _tts_of(sel: dict) -> float:
        return float(
            sel.get("tts_duration_sec")
            or duration_map.get(sel["scene_id"])
            or sel.get("clip_duration_sec", 5.0)
        )

    # ── 그룹 단위로 먼저 묶는다 ──────────────────────────────────────────
    # #short-source-group — 예전에는 오프셋을 TTS 합으로 그냥 누적했다.
    # 그룹의 TTS 총합이 원본 길이보다 길면 뒤쪽 씬의 오프셋이 파일 끝을 넘어
    # "마지막 프레임 사용"(0.1s 정지화면)으로 떨어졌다.
    # 실제 사고: 씬 63~67 이 5.4s 짜리 rec_R2_choice.mp4 를 공유하는데 필요한
    # 시간은 21.0s → 씬 64~67 이 전부 0.1s 로 렌더됨.
    #
    # 해결: 그룹 총 TTS 가 원본을 초과하면, 원본을 TTS 비율대로 나눠 배분한다.
    # 각 씬은 자기 몫의 구간을 받고, 부족한 길이는 뒤에서 슬로모션으로 늘어난다.
    groups: list[list[dict]] = []
    prev_file: str | None = None
    for sel in selections:
        if sel.get("effect", "") != "clip":
            prev_file = None
            continue
        source_file = sel.get("source_file", "")
        if source_file != prev_file:
            groups.append([])
            prev_file = source_file
        groups[-1].append(sel)

    offsets: dict[int, float] = {}
    for group in groups:
        total_tts = sum(_tts_of(s) for s in group)
        src_dur = 0.0
        if len(group) > 1:
            # 원본 길이는 그룹의 첫 씬 기준(같은 파일이므로 동일).
            src_path = group[0].get("source_file")
            try:
                src_dur = get_duration(str(src_path)) if src_path else 0.0
            except Exception:
                src_dur = 0.0

        # 원본이 모자란 그룹만 비율 배분. 그 외에는 기존(TTS 누적) 동작 유지.
        squeeze = (
            len(group) > 1 and src_dur > 0 and total_tts > src_dur
        )
        accumulated = 0.0
        for sel in group:
            if squeeze:
                offsets[sel["scene_id"]] = accumulated
                accumulated += src_dur * (_tts_of(sel) / total_tts)
            else:
                offsets[sel["scene_id"]] = accumulated
                accumulated += _tts_of(sel)

    return offsets


def _build_clip_group_total_tts(selections: list) -> dict:
    """연속된 (#effect clip) 씬 그룹 각각의 전체 TTS duration 합산을 반환한다.

    동일 source_file을 참조하는 인접 clip 씬들을 하나의 그룹으로 묶고,
    그룹 내 모든 씬의 tts_dur를 합산하여 각 씬에 매핑한다.
    단독 씬(그룹 크기 1)도 포함하여 매핑한다.

    반환값: {scene_id: total_group_tts_sec}
    """
    narr_data = load_json(NARRATION_TIMELINE_PATH)
    duration_map: dict[int, float] = {}
    if narr_data and narr_data.get("entries"):
        for entry in narr_data["entries"]:
            sid = entry.get("scene_id")
            dur = entry.get("duration_sec")
            if dur is None:
                end = entry.get("end_sec", 0.0)
                start = entry.get("start_sec", 0.0)
                dur = end - start if end > start else 0.0
            if sid is not None and dur is not None:
                duration_map[sid] = float(dur)

    def _get_tts(sel: dict) -> float:
        sid = sel["scene_id"]
        return float(
            sel.get("tts_duration_sec")
            or duration_map.get(sid)
            or sel.get("clip_duration_sec", 5.0)
        )

    # 연속 그룹 탐지
    groups: list[list[dict]] = []
    cur_group: list[dict] = []
    prev_file: str | None = None

    for sel in selections:
        effect = sel.get("effect", "")
        if effect != "clip":
            if cur_group:
                groups.append(cur_group)
                cur_group = []
            prev_file = None
            continue

        source_file = sel.get("source_file", "")
        if source_file != prev_file:
            if cur_group:
                groups.append(cur_group)
            cur_group = []
            prev_file = source_file

        cur_group.append(sel)

    if cur_group:
        groups.append(cur_group)

    result: dict[int, float] = {}
    for group in groups:
        total_tts = sum(_get_tts(sel) for sel in group)
        for sel in group:
            result[sel["scene_id"]] = total_tts

    return result


def _build_clip_group_sizes(selections: list) -> dict:
    """연속된 (#effect clip) 씬 그룹의 크기(씬 수)를 씬별로 반환한다.

    반환값: {scene_id: group_size}  (단독 씬은 1, 연속 그룹은 2 이상)
    """
    # 연속 그룹 탐지
    groups: list[list[dict]] = []
    cur_group: list[dict] = []
    prev_file: str | None = None

    for sel in selections:
        effect = sel.get("effect", "")
        if effect != "clip":
            if cur_group:
                groups.append(cur_group)
                cur_group = []
            prev_file = None
            continue

        source_file = sel.get("source_file", "")
        if source_file != prev_file:
            if cur_group:
                groups.append(cur_group)
            cur_group = []
            prev_file = source_file

        cur_group.append(sel)

    if cur_group:
        groups.append(cur_group)

    result: dict[int, int] = {}
    for group in groups:
        size = len(group)
        for sel in group:
            result[sel["scene_id"]] = size

    return result


# ── Text overlay helpers ───────────────────────────────────────────────────

def _escape_filter_path(path: str) -> str:
    """ffmpeg 필터 인자에 넣을 파일 경로를 이스케이프한다.

    Windows 경로는 그대로 넣으면 필터 문법이 깨진다.
      - 백슬래시(\)를 ffmpeg 이 이스케이프 문자로 먹는다
      - 드라이브 콜론(E:)을 옵션 구분자로 본다
    둘 다 역슬래시로 escape 하고, 슬래시로 통일해 안전하게 만든다.
    (macOS/Linux 경로는 바꿀 것이 없어 기존 동작과 동일하다.)
    """
    p = str(path).replace("\\", "/")
    return p.replace(":", "\:")


def _escape_drawtext(text: str) -> str:
    """FFmpeg drawtext 필터용 특수문자 이스케이프.

    작은따옴표는 `\\'` 로 감싸면 안 된다. ffmpeg 필터그래프 파서는 작은따옴표 문자열
    안에서 백슬래시 이스케이프를 해석하지 않아서, `text='...\\'...'` 는 그 지점에서
    문자열이 끝난 것으로 읽힌다. 그러면 뒤따르는 `enable='between(t,3.2,3.3)'` 의
    콤마가 필터 구분자로 오인돼 "No such filter: '3.3176'" 로 렌더가 통째로 실패한다.
    (실제 사고: "진성왕을 '진성여왕'이라 쓰지 않습니다" 자막이 통째로 사라짐.)

    올바른 관용구는 `'\\''` — 문자열을 닫고, 이스케이프된 리터럴 따옴표를 넣고, 다시 연다.

    `%` 는 여기서 이스케이프하지 않는다. drawtext 는 text 를 strftime 포맷으로 해석해
    `%` 뒤 문자를 지정자로 먹는데, `\\%` 도 `%%` 도 ffmpeg 8.x 에서는 통하지 않는다
    (셋 다 `Stray % near ...` 로 실패하는 것을 실측 확인). 유일하게 동작하는 방법은
    drawtext 에 **`expansion=none`** 을 주어 확장 자체를 끄는 것이고, 그 처리는
    필터를 조립하는 `_build_text_drawtext_parts` 쪽에 있다.
    (실제 사고: "명성 +1 · 방어 +60% · 이동 +2" 자막이 렌더 결과에서 통째로 사라짐.
     로그에는 `drawtext 실패 → 진입/종료 효과 없이 재시도` 만 남고 폴백도 같은 이유로 실패.)
    """
    return (
        text.replace("\\", "\\\\")
        .replace("'", "'\\''")
        .replace(":", "\\:")
    )


def _find_font_path(font_family: str, bold: bool = False) -> str | None:
    """폰트 패밀리 이름으로 폰트 파일을 탐색한다. bold=True면 Bold 변형 파일을 우선 탐색한다."""
    _repo_root = Path(__file__).parent.parent
    search_dirs = [
        _repo_root / "fonts",
        Path("fonts"),
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]
    family_key = font_family.lower().replace(" ", "")
    candidates: list[str] = []
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.suffix.lower() in (".ttf", ".otf"):
                stem = f.stem.lower().replace(" ", "")
                if family_key in stem:
                    candidates.append(str(f))
    if not candidates:
        return None
    if bold:
        for c in candidates:
            if "bold" in Path(c).stem.lower():
                return c
    # bold 변형 없으면 첫 번째 반환 (합성 볼드는 호출 측에서 처리)
    return candidates[0]


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)


def _hex_to_ffmpeg_color(hex_color: str, opacity: float) -> str:
    """hex 색상 + opacity → FFmpeg RRGGBBAA 형식.

    FFmpeg drawtext fontcolor 컨벤션: AA=FF가 불투명, AA=00이 투명 (표준).
    즉 opacity=1.0 → AA=FF, opacity=0.0 → AA=00.
    """
    r, g, b = _hex_to_rgb(hex_color)
    a = int(max(0.0, min(1.0, opacity)) * 255)
    return f"0x{r:02X}{g:02X}{b:02X}{a:02X}"


def _interpolate_gradient(stops: list[dict], pos: float) -> tuple[int, int, int]:
    """stops 목록에서 pos(0.0~1.0)에 해당하는 RGB를 선형 보간한다."""
    if not stops:
        return (255, 255, 255)
    sorted_stops = sorted(stops, key=lambda s: s["position"])
    if pos <= sorted_stops[0]["position"]:
        return _hex_to_rgb(sorted_stops[0]["color"])
    if pos >= sorted_stops[-1]["position"]:
        return _hex_to_rgb(sorted_stops[-1]["color"])
    for i in range(len(sorted_stops) - 1):
        s0, s1 = sorted_stops[i], sorted_stops[i + 1]
        if s0["position"] <= pos <= s1["position"]:
            t = (pos - s0["position"]) / (s1["position"] - s0["position"] + 1e-9)
            r0, g0, b0 = _hex_to_rgb(s0["color"])
            r1, g1, b1 = _hex_to_rgb(s1["color"])
            return (
                int(r0 + (r1 - r0) * t),
                int(g0 + (g1 - g0) * t),
                int(b0 + (b1 - b0) * t),
            )
    return _hex_to_rgb(sorted_stops[-1]["color"])


# ── Enter/Exit Effects (#287) ─────────────────────────────────────────────
# 모든 오버레이 공통: "none"/"fade". 텍스트만 추가로 "typing"/"zoom" 지원.
# 기본값은 fade + 0.3초 (회귀 안전: 기존 데이터에 enter_effect 필드가 없으면 .get()이 fade 반환).

# 관통(pass-through) 효과 — 한쪽 화면 밖에서 날아 들어와 원위치를 지나 "반대쪽" 화면 밖으로
# 빠져나간다. 기존 slide_in_* 은 들어온 쪽으로 되돌아가므로(왕복) 방향이 다르다.
# enter_effect 에 이 값을 주면 exit 방향은 자동으로 반대편이 되므로 exit_effect 는 지정할 필요가 없다.
_PASS_THROUGH_EFFECTS = {
    "pass_left_to_right",
    "pass_right_to_left",
    "pass_top_to_bottom",
    "pass_bottom_to_top",
}

# 관통 효과의 (진입 방향, 퇴장 방향). 값은 "화면 밖" 단위 오프셋 (dx, dy) 배수.
_PASS_THROUGH_VECTORS = {
    "pass_left_to_right": ((-1.0, 0.0), (1.0, 0.0)),
    "pass_right_to_left": ((1.0, 0.0), (-1.0, 0.0)),
    "pass_top_to_bottom": ((0.0, -1.0), (0.0, 1.0)),
    "pass_bottom_to_top": ((0.0, 1.0), (0.0, -1.0)),
}

# enter_effect/exit_effect로 들어올 수 있는 모든 값 (텍스트 기준).
_ENTER_EXIT_EFFECTS_TEXT = {"none", "fade", "typing", "zoom"} | _PASS_THROUGH_EFFECTS
_ENTER_EXIT_EFFECTS_NON_TEXT = {"none", "fade"}
# 이미지 오버레이 전용 확장 효과 집합. slide 계열 + zoom + pop.
# 도형/그래픽에서는 여전히 _ENTER_EXIT_EFFECTS_NON_TEXT 만 유효(=fade 로 폴백).
_ENTER_EXIT_EFFECTS_IMAGE = {
    "none",
    "fade",
    "slide_in_left",
    "slide_in_right",
    "slide_in_top",
    "slide_in_bottom",
    "zoom",
    "pop",
} | _PASS_THROUGH_EFFECTS


def _get_overlay_effect_config(
    overlay: dict, text_supported: bool = False, image_supported: bool = False
) -> tuple[str, str, float, float]:
    """오버레이 dict에서 (enter_effect, exit_effect, enter_dur, exit_dur)를 추출한다.

    회귀 안전:
      - 필드가 없으면 fade + 0.3초로 폴백.
      - duration은 0.0~2.0 범위로 클램프.
      - text_supported=False면 typing/zoom은 fade로 폴백 (도형/그래픽).
      - image_supported=True면 slide_in_left/right/top/bottom + zoom + pop 도 유효값으로 통과 (이미지 오버레이 전용).
    """
    if image_supported:
        valid_effects = _ENTER_EXIT_EFFECTS_IMAGE
    elif text_supported:
        valid_effects = _ENTER_EXIT_EFFECTS_TEXT
    else:
        valid_effects = _ENTER_EXIT_EFFECTS_NON_TEXT
    enter_effect = str(overlay.get("enter_effect") or "fade").lower()
    exit_effect = str(overlay.get("exit_effect") or "fade").lower()
    if enter_effect not in valid_effects:
        enter_effect = "fade"
    if exit_effect not in valid_effects:
        exit_effect = "fade"
    # 관통 효과는 "들어와서 반대편으로 빠져나간다" 가 한 동작이다. enter 에만 지정하면
    # exit 도 같은 관통 효과로 자동 연결해 사용자가 두 곳을 맞출 필요가 없게 한다.
    # (exit 를 명시적으로 다른 관통 효과로 지정했다면 그 의도를 존중해 덮어쓰지 않는다.)
    if enter_effect in _PASS_THROUGH_EFFECTS and exit_effect not in _PASS_THROUGH_EFFECTS:
        exit_effect = enter_effect
    try:
        enter_dur = float(overlay.get("enter_duration_sec", 0.3))
    except (TypeError, ValueError):
        enter_dur = 0.3
    try:
        exit_dur = float(overlay.get("exit_duration_sec", 0.3))
    except (TypeError, ValueError):
        exit_dur = 0.3
    enter_dur = max(0.0, min(2.0, enter_dur))
    exit_dur = max(0.0, min(2.0, exit_dur))
    # "none" 효과는 duration을 0으로 강제 (안전)
    if enter_effect == "none":
        enter_dur = 0.0
    if exit_effect == "none":
        exit_dur = 0.0
    return enter_effect, exit_effect, enter_dur, exit_dur


def _build_fade_alpha_expr(
    start_sec: float,
    end_sec: float | None,
    enter_effect: str,
    exit_effect: str,
    enter_dur: float,
    exit_dur: float,
    base_alpha: float = 1.0,
    visual_clip_duration: float | None = None,
) -> str:
    """ffmpeg drawtext의 alpha 표현식을 생성한다.

    enter_effect가 "fade"면 [start_sec, start_sec+enter_dur] 구간 0→base_alpha 보간,
    exit_effect가 "fade"면 [end_sec-exit_dur, end_sec] 구간 base_alpha→0 보간한다.
    end_sec가 None이면 시각적 클립 끝(visual_clip_duration)을 사용한다.

    반환되는 표현식은 drawtext의 `alpha='...'` 인자에 직접 들어간다.
    enable= 시각 가시성과 함께 사용되므로 enable 구간 바깥은 알파 식의 값에 관계없이 보이지 않는다.
    구조:
        if(lt(t, enter_end),
           (t-start)/enter_dur * base,
           if(gt(t, exit_start),
              (end-t)/exit_dur * base,
              base))
    """
    if end_sec is not None:
        end = float(end_sec)
    elif visual_clip_duration is not None and visual_clip_duration > 0:
        end = float(visual_clip_duration)
    else:
        end = 1e9  # 사실상 무한 (exit 페이드 비활성화)

    start = float(start_sec)
    base = max(0.0, min(1.0, float(base_alpha)))
    base_s = f"{base:.4f}"

    do_enter = enter_effect == "fade" and enter_dur > 0
    do_exit = exit_effect == "fade" and exit_dur > 0 and end < 1e8 and end > start

    if not do_enter and not do_exit:
        return base_s

    # exit-only / enter-only 분기 처리
    if do_exit:
        exit_start = end - exit_dur
        exit_expr = (
            f"if(gt(t,{exit_start:.4f}),"
            f"max(0,min(1,({end:.4f}-t)/{exit_dur:.4f}))*{base_s},"
            f"{base_s})"
        )
    else:
        exit_expr = base_s

    if do_enter:
        enter_end = start + enter_dur
        return (
            f"if(lt(t,{enter_end:.4f}),"
            f"max(0,min(1,(t-{start:.4f})/{enter_dur:.4f}))*{base_s},"
            f"{exit_expr})"
        )
    return exit_expr


def _compute_overlay_alpha_scalar(
    overlay: dict,
    t: float,
    visual_clip_duration: float | None = None,
    text_supported: bool = False,
) -> float:
    """프레임 시각 t에서 오버레이의 알파 배수(0~1)를 계산한다 (#287).

    PIL 합성 파이프라인(shape/graphic) 전용. start_sec/end_sec 바깥은 호출 측에서
    이미 스킵하므로 여기는 enter/exit 페이드 영역만 계산한다.
    fade가 아닌 enter/exit 효과는 즉시 1.0(완전 표시)으로 폴백한다.
    """
    enter_effect, exit_effect, enter_dur, exit_dur = _get_overlay_effect_config(
        overlay, text_supported=text_supported
    )
    start = float(overlay.get("start_sec", 0.0) or 0.0)
    end_raw = overlay.get("end_sec")
    if end_raw is not None:
        end = float(end_raw)
    elif visual_clip_duration is not None and visual_clip_duration > 0:
        end = float(visual_clip_duration)
    else:
        end = float("inf")

    alpha = 1.0
    if enter_effect == "fade" and enter_dur > 0:
        if t < start + enter_dur:
            phase = max(0.0, min(1.0, (t - start) / enter_dur))
            alpha = min(alpha, phase)
    if exit_effect == "fade" and exit_dur > 0 and end != float("inf"):
        exit_start = end - exit_dur
        if t > exit_start:
            phase = max(0.0, min(1.0, (end - t) / exit_dur))
            alpha = min(alpha, phase)
    return max(0.0, min(1.0, alpha))


def _build_zoom_fontsize_expr(
    start_sec: float,
    end_sec: float | None,
    enter_effect: str,
    exit_effect: str,
    enter_dur: float,
    exit_dur: float,
    base_fontsize: int,
    visual_clip_duration: float | None = None,
) -> str:
    """텍스트 zoom 효과용 drawtext fontsize 표현식을 생성한다 (#287).

    zoom 진입: base*0.2 → base 보간
    zoom 퇴장: base → base*1.8 보간 (동시에 alpha 페이드아웃)
    효과가 zoom이 아닌 쪽은 base 그대로.
    """
    if end_sec is not None:
        end = float(end_sec)
    elif visual_clip_duration is not None and visual_clip_duration > 0:
        end = float(visual_clip_duration)
    else:
        end = 1e9

    start = float(start_sec)
    base = float(base_fontsize)
    base_s = f"{base:.2f}"

    do_enter = enter_effect == "zoom" and enter_dur > 0
    do_exit = exit_effect == "zoom" and exit_dur > 0 and end < 1e8 and end > start

    if not do_enter and not do_exit:
        return base_s

    if do_exit:
        exit_start = end - exit_dur
        # base → base*1.8 (커지면서 사라짐)
        exit_max = base * 1.8
        exit_expr = (
            f"if(gt(t,{exit_start:.4f}),"
            f"{base_s}+({exit_max - base:.4f})*max(0,min(1,(t-{exit_start:.4f})/{exit_dur:.4f})),"
            f"{base_s})"
        )
    else:
        exit_expr = base_s

    if do_enter:
        enter_end = start + enter_dur
        # base*0.2 → base (작게 시작해서 커짐)
        enter_min = base * 0.2
        return (
            f"if(lt(t,{enter_end:.4f}),"
            f"{enter_min:.4f}+({base - enter_min:.4f})*max(0,min(1,(t-{start:.4f})/{enter_dur:.4f})),"
            f"{exit_expr})"
        )
    return exit_expr


def _build_text_alpha_mask(
    overlay: dict,
    video_width: int,
    video_height: int,
):
    """텍스트 글리프 모양의 알파 마스크(PIL "L" 이미지)와 bbox 정보를 만든다.

    글자가 차지하는 픽셀만 255, 나머지는 0. 그라디언트 채색이 쓰는 것과 동일한 마스크로,
    집중선 등 "글자 안쪽에만 보이는" 레이어를 만들 때 재사용한다.

    반환: (mask, (tx, ty), (b_left, b_top, b_right, b_bottom)) 또는 텍스트가 없으면 None.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    text = overlay.get("text", "")
    if not text:
        return None

    font_size = int(overlay.get("font_size", 40))
    font_family = overlay.get("font_family", "NotoSansKR")
    bold = bool(overlay.get("bold", False))
    font_path = _find_font_path(font_family, bold=bold)
    try:
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    has_native_bold = bold and font_path and "bold" in Path(font_path).stem.lower()
    synthetic_bold = bold and not has_native_bold

    dummy = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    b_left, b_top, b_right, b_bottom = bbox
    PAD = 40
    tw = b_right - b_left + PAD * 2
    th = b_bottom - b_top + PAD * 2

    x_ratio = float(overlay.get("x", 0.5))
    y_ratio = float(overlay.get("y", 0.1))
    tx = int((video_width - tw) * x_ratio) - b_left + PAD
    ty = int((video_height - th) * y_ratio) - b_top + PAD

    mask = Image.new("L", (video_width, video_height), 0)
    mask_draw = ImageDraw.Draw(mask)
    if synthetic_bold:
        for ox, oy in [(1, 0), (0, 1), (1, 1)]:
            mask_draw.text((tx + ox, ty + oy), text, font=font, fill=255)
    mask_draw.text((tx, ty), text, font=font, fill=255)
    return mask, (tx, ty), (b_left, b_top, b_right, b_bottom)


#: 텍스트 꼬리(speed tail)가 글자 폭 대비 몇 배까지 뻗는지 상한.
_TAIL_MAX_LEN_RATIO = 0.55


def _render_text_speed_tail_layer(
    overlay: dict,
    video_width: int,
    video_height: int,
    alpha: int,
    direction: "tuple[float, float]",
    phase_bucket: "int | None" = None,
):
    """글자가 날아온 방향으로 "꼬리"를 그린 RGBA 레이어를 만든다 (없으면 None).

    집중선을 글자 *안쪽*에 그리면 획을 갉아 글자가 깨져 보인다(사용자 피드백).
    대신 글자 바깥, 날아온 쪽으로 집중선 스타일의 꼬리를 늘어뜨린다 —
    오른쪽에서 날아오면 오른쪽으로 잔상이 뻗는 식이라 속도감이 살고 글자는 온전하다.

    구현: 글자 마스크를 꼬리 방향으로 조금씩 밀며 겹쳐 그린 뒤(모션 블러와 같은 원리),
    가로 줄무늬로 잘라 "선"처럼 보이게 한다. 글자 본체 영역은 마지막에 비워서
    글자 자신은 이 레이어에 가려지지 않는다.

    `direction` 은 꼬리가 뻗어 나갈 방향 단위 벡터. (1,0)이면 오른쪽으로 뻗는다.
    """
    try:
        from PIL import Image, ImageChops, ImageDraw
    except ImportError:
        return None

    cfg = dict(overlay.get("text_speed_lines") or {})

    def _num(key, default, cast=float):
        v = cfg.get(key)
        if v is None:
            return default
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default

    built = _build_text_alpha_mask(overlay, video_width, video_height)
    if built is None:
        return None
    text_mask = built[0]
    bbox_px = text_mask.getbbox()
    if not bbox_px:
        return None
    gx0, gy0, gx1, gy1 = bbox_px

    dx, dy = direction
    norm = math.hypot(dx, dy) or 1.0
    dx, dy = dx / norm, dy / norm

    # 꼬리 길이 — 글자 크기(진행 방향 쪽 변) 기준.
    span = (gx1 - gx0) if abs(dx) >= abs(dy) else (gy1 - gy0)
    length = max(8.0, span * max(0.05, min(1.5, _num("tail_length", _TAIL_MAX_LEN_RATIO))))

    steps = max(6, min(90, int(length / 3.0)))
    tail = Image.new("L", (video_width, video_height), 0)
    for i in range(1, steps + 1):
        f = i / steps
        off_x = int(round(dx * length * f))
        off_y = int(round(dy * length * f))
        # 멀어질수록 옅어진다(제곱으로 떨어뜨려 끝이 자연스럽게 사라지도록).
        v = int(255 * (1.0 - f) ** 2)
        if v <= 0:
            continue
        # ImageChops.offset 은 화면 밖으로 나간 부분이 반대편으로 되감긴다(wrap).
        # 그대로 쓰면 오른쪽으로 뻗은 꼬리가 왼쪽 끝에 유령처럼 나타난다.
        # 빈 캔버스에 paste 해서 넘어간 부분은 잘리도록 한다.
        shifted = Image.new("L", (video_width, video_height), 0)
        shifted.paste(text_mask, (off_x, off_y))
        tail = ImageChops.lighter(tail, shifted.point(lambda p, m=v: (p > 128) and m or 0))

    # 줄무늬로 잘라 "집중선" 질감을 만든다. 진행 방향과 나란한 띠라서 속도선처럼 읽힌다.
    stripes = Image.new("L", (video_width, video_height), 0)
    sd = ImageDraw.Draw(stripes)
    period = max(3, int(_num("tail_stripe", 9)))
    duty = max(1, int(period * 0.45))
    if abs(dx) >= abs(dy):
        for y in range(0, video_height, period):      # 가로로 흐르면 가로 줄무늬
            sd.rectangle([0, y, video_width, y + duty], fill=255)
    else:
        for x in range(0, video_width, period):
            sd.rectangle([x, 0, x + duty, video_height], fill=255)
    tail = ImageChops.multiply(tail, stripes)

    # 글자 본체는 비운다 — 꼬리는 글자 "밖"에만 있어야 글자가 온전히 읽힌다.
    # 마스크 가장자리는 안티에일리어싱으로 값이 0~255 사이라 그냥 빼면 테두리에
    # 꼬리가 남는다. 한 번 부풀린(MaxFilter) 이진 마스크로 빼서 완전히 지운다.
    #
    # 외곽선(outline_width)도 함께 비워야 한다. 화면에 보이는 글자는 획 + 외곽선이라,
    # 획 마스크만 빼면 외곽선 띠 위로 줄무늬가 지나가 글자가 지저분해 보인다.
    from PIL import ImageFilter as _IF

    solid = text_mask.point(lambda p: 255 if p > 8 else 0)
    try:
        _ow = int(overlay.get("outline_width", 0) or 0)
    except (TypeError, ValueError):
        _ow = 0
    grow = max(1, _ow) * 2 + 1          # MaxFilter 는 홀수 커널만 받는다
    solid = solid.filter(_IF.MaxFilter(min(grow, 31)))
    tail = ImageChops.subtract(tail, solid)

    if not tail.getbbox():
        return None

    opacity_scale = max(0, min(255, int(alpha))) / 255.0
    tail_opacity = max(0.0, min(1.0, _num("tail_opacity", 0.85))) * opacity_scale
    if tail_opacity < 0.999:
        tail = tail.point(lambda v, m=tail_opacity: int(v * m))

    # 색: 지정이 없으면 글자색을 그대로 쓴다 (사용자 요청 — "텍스트 색상으로 꼬리").
    color_hex = str(cfg.get("color") or overlay.get("color") or "#ffffff")
    cr, cg, cb = _hex_to_rgb(color_hex)
    layer = Image.new("RGBA", (video_width, video_height), (cr, cg, cb, 0))
    layer.putalpha(tail)
    return layer


def _render_text_masked_speed_lines_layer(
    overlay: dict,
    video_width: int,
    video_height: int,
    alpha: int,
    phase_bucket: "int | None" = None,
):
    """집중선을 글자 모양으로 마스킹한 RGBA 레이어를 만든다 (없으면 None).

    `src.speed_lines.generate_speed_lines_png` 로 만든 화면 크기 집중선 PNG 에
    글자 알파 마스크를 곱해, 글자가 차지하는 픽셀에만 집중선이 보이게 한다.
    파라미터는 씬 집중선(speed_lines)과 동일한 키를 쓰되 `text_speed_lines` dict 로
    덮어쓸 수 있다. `phase_bucket` 이 주어지면 그 버킷 선만 그려 애니메이션에 쓴다.
    """
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter
    except ImportError:
        return None
    try:
        from src.speed_lines import (
            DEFAULT_COLOR, DEFAULT_JITTER, DEFAULT_NUM_BUCKETS, DEFAULT_SEED,
            DEFAULT_THICKNESS, DEFAULT_WIDTH_VARIANCE, INNER_STOP_SPREAD,
        )
    except Exception:
        DEFAULT_COLOR, DEFAULT_JITTER = "#ffffff", 0.6
        DEFAULT_NUM_BUCKETS, DEFAULT_SEED = 8, 12345
        DEFAULT_THICKNESS, DEFAULT_WIDTH_VARIANCE = 1.2, 0.2
        INNER_STOP_SPREAD = 0.7

    built = _build_text_alpha_mask(overlay, video_width, video_height)
    if built is None:
        return None
    text_mask, _pos, _bbox = built

    cfg = overlay.get("text_speed_lines") or {}
    if not isinstance(cfg, dict):
        cfg = {}

    def _num(key, default, cast=float):
        v = cfg.get(key)
        if v is None:
            return default
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default

    # ── 텍스트 외곽 영역 검출 ────────────────────────────────────────────────
    # 글자 픽셀의 bbox 를 구해 그 중심/크기를 집중선 기준으로 삼는다. 원형(정원) 기준으로
    # 그리면 가로로 긴 글자에서 위아래가 비고 좌우가 잘리므로, 글자 실제 범위를 쓴다.
    bbox_px = text_mask.getbbox()
    if not bbox_px:
        return None
    gx0, gy0, gx1, gy1 = bbox_px
    cx = (gx0 + gx1) / 2.0
    cy = (gy0 + gy1) / 2.0
    if cfg.get("center_x") is not None:
        cx = float(cfg["center_x"]) * video_width
    if cfg.get("center_y") is not None:
        cy = float(cfg["center_y"]) * video_height

    # 각 방향으로 "글자 외곽선까지의 거리" 를 재기 위한 룩업. 마스크를 실제로 따라가며
    # 글자 밖으로 나가는 지점을 찾으므로, 원/타원이 아니라 글자 모양 그대로를 따른다.
    mask_np = None
    try:
        import numpy as _np

        mask_np = _np.array(text_mask)
    except Exception:
        mask_np = None

    import random

    # 외곽선 기반은 선 하나가 짧으므로(획 두께만큼) 화면 집중선보다 촘촘해야 한다.
    # 텍스트 집중선 기본값은 화면 전체 집중선과 다르다. 글자 안은 면적이 작아서
    # 화면용 밀도/굵기(260 / 1.2)를 그대로 쓰면 획이 선으로 가득 차 글자가 깨져 보인다.
    # 아래 값은 "글자가 먼저 읽히고 선은 가장자리 질감으로 얹히는" 수준으로 맞춘 것.
    # 90 = 버킷 8개로 쪼개도 각 버킷에 선이 남는 최소선(45 로 낮췄더니 빈 버킷이 생겨
    # 애니메이션 중 한 프레임이 통째로 비었다). 대신 두께/깊이를 줄여 획을 덜 갉는다.
    n_lines = int(_num("density", 90, int))
    n_lines = max(8, min(4000, n_lines))
    seed = int(_num("seed", DEFAULT_SEED, int))
    jit = max(0.0, min(1.0, _num("jitter", DEFAULT_JITTER)))
    wv = max(0.0, min(0.9, _num("width_variance", DEFAULT_WIDTH_VARIANCE)))
    th = max(0.05, min(5.0, _num("thickness", 0.45)))
    nb = max(1, int(_num("num_buckets", DEFAULT_NUM_BUCKETS, int)))
    # 선을 글자 외곽에서 안쪽으로 얼마나 밀어넣을지(0~1). 1.0 이면 뻗을 수 있는 끝까지.
    # 0.72 → 0.3. 깊이 0.72 는 획을 가로질러 반대편 선과 만나 글자가 깨져 보였다.
    depth = max(0.05, min(1.0, _num("depth", 0.30)))

    rng = random.Random(seed)
    phase_rng = random.Random(seed ^ 0x5EED)
    depth_rng = random.Random(seed ^ 0xDEE7)
    gap_rng = random.Random(seed ^ 0x9A17)

    # 선 사이 간격 랜덤화 — 화면 집중선과 동일한 방식(균등 분할 + jitter 가중).
    gaps = [1.0 + jit * gap_rng.random() for _ in range(n_lines)]
    gsum = sum(gaps) or 1.0
    angles: list[float] = []
    acc = 0.0
    for g in gaps:
        angles.append(acc / gsum * 2.0 * math.pi)
        acc += g

    line_phase = [phase_rng.random() for _ in range(n_lines)]
    line_bucket = [min(nb - 1, int(p * nb)) for p in line_phase]
    depth_spread = [depth_rng.random() for _ in range(n_lines)]

    if mask_np is None:
        return None

    short_side = min(video_width, video_height)
    w_out = (short_side * 0.010) * th

    # 집중선은 supersample 캔버스에 그린 뒤 축소해 계단을 없앤다.
    ss = max(1, int(_num("supersample", 3, int)))
    SW, SH = video_width * ss, video_height * ss
    # 선 색: 지정이 없으면 글자색과 "대비되는" 색을 쓴다. 글자색을 그대로 쓰면 같은 색
    # 위에 같은 색을 얹는 셈이라 선이 보이지 않는다(글자가 통짜로 보임). 밝은 글자에는
    # 어두운 선, 어두운 글자에는 밝은 선을 기본값으로 둔다.
    if cfg.get("color"):
        color_hex = str(cfg["color"])
    else:
        _tr, _tg, _tb = _hex_to_rgb(str(overlay.get("color") or DEFAULT_COLOR))
        _lum = (0.299 * _tr + 0.587 * _tg + 0.114 * _tb) / 255.0
        color_hex = "#1a1a1a" if _lum > 0.5 else "#ffffff"
    cr, cg, cb = _hex_to_rgb(color_hex)

    lines_img = Image.new("L", (SW, SH), 0)
    ldraw = ImageDraw.Draw(lines_img)

    # ── 외곽선 위의 점 + 그 지점의 "안쪽 방향" 을 구한다 ──────────────────────
    # 한 점(중심)에서 방사하면 글자가 가로로 길 때 양끝이 거의 수평선이 되어 뭉갠다.
    # 대신 외곽선 자체를 따라가며 각 지점에서 글자 안쪽으로 파고드는 선을 그린다.
    # 안쪽 방향 = 마스크를 블러한 거리장(distance field)의 그래디언트 방향.
    blurred = text_mask.filter(ImageFilter.GaussianBlur(radius=max(2, int(short_side * 0.012))))
    try:
        import numpy as _np

        bl = _np.asarray(blurred, dtype=_np.float32)
        grad_y, grad_x = _np.gradient(bl)  # 값이 커지는 방향 = 글자 안쪽
    except Exception:
        return None

    # 외곽선 픽셀 = 글자 - 침식(erode). 여기서 선이 출발한다.
    eroded = text_mask.filter(ImageFilter.MinFilter(3))
    edge_np = (mask_np > 128) & (_np.asarray(eroded) <= 128)
    ys, xs = _np.nonzero(edge_np)
    if len(xs) == 0:
        return None

    # 외곽선 픽셀을 각도 순으로 정렬해 균등하게 n_lines 개 고른다.
    # (무작위로 고르면 뭉치는 구간이 생긴다 — 각도 정렬이 시각적으로 고르다.)
    ang_of = _np.arctan2(ys - cy, xs - cx)
    order = _np.argsort(ang_of)
    xs, ys = xs[order], ys[order]

    total_edge = len(xs)
    take = min(n_lines, total_edge)
    # gap jitter 를 인덱스 간격에 반영 — 선 사이 간격이 랜덤해진다.
    idx_pos = [a / (2.0 * math.pi) * total_edge for a in angles[:take]]

    # 글자 두께(=선이 파고들 수 있는 최대 깊이) 기준. 너무 길면 반대편으로 뚫고 나간다.
    diag = math.hypot(gx1 - gx0, gy1 - gy0)
    max_len = max(4.0, diag * 0.5)
    # 선이 "처음 만난 획" 두께의 몇 배까지 파고들 수 있는지. 집중선은 획 가장자리를
    # 갉아 강조하는 장식이지 글자를 가로지르는 선이 아니다. 1.0 을 넘기면 획을 뚫고
    # 나가 글자 속이 비어 보이고, 반대편 선과 만나 글자가 깨진 것처럼 읽힌다.
    _STROKE_DEPTH_CAP = 0.55

    drew_any = False
    for i in range(take):
        if phase_bucket is not None and line_bucket[i] != int(phase_bucket):
            continue
        pi = int(idx_pos[i]) % total_edge
        px, py = float(xs[pi]), float(ys[pi])

        # 안쪽 방향 — 거리장 그래디언트. 크기가 0 이면(평탄) 중심 방향으로 폴백.
        vx, vy = float(grad_x[int(py), int(px)]), float(grad_y[int(py), int(px)])
        norm = math.hypot(vx, vy)
        if norm < 1e-6:
            vx, vy = cx - px, cy - py
            norm = math.hypot(vx, vy) or 1.0
        vx, vy = vx / norm, vy / norm

        # 이 지점에서 글자 안쪽으로 얼마나 뻗을 수 있는지 실제로 따라가 본다.
        # 획 하나를 가로지른 뒤 잠깐 배경이 나와도(획 사이 틈) 조금 더 훑어 다음 획까지
        # 이어 준다 — 그러지 않으면 선이 획 두께에서 끊겨 "부스러기" 처럼 보인다.
        #
        # `stroke` 는 "처음 만난 획 하나"의 두께다. 선 길이는 이걸 기준으로 잘라야 한다.
        # reach 는 획 사이 틈을 건너뛰며 재기 때문에 한글처럼 획이 여러 겹인 글자에서는
        # 글자를 관통하는 길이가 나온다. 거기에 depth 를 곱하면(예: 0.5) 여전히 글자
        # 절반 깊이라, 반대편에서 들어온 선과 가운데서 만나 글자가 깨져 보인다.
        # → 실제 상한은 min(reach*depth, stroke*STROKE_CAP) 로 둔다.
        reach = 0.0
        stroke = 0.0
        gap = 0.0
        r = 1.0
        while r <= max_len:
            sx, sy = int(px + vx * r), int(py + vy * r)
            if not (0 <= sx < video_width and 0 <= sy < video_height):
                break
            if mask_np[sy, sx] > 128:
                reach = r
                if gap == 0.0:
                    stroke = r      # 아직 첫 획 안 — 첫 획 두께를 계속 갱신
                gap = 0.0
            else:
                gap += 1.5
                if gap > max_len * 0.25:
                    break
            r += 1.5
        if reach < 2.0:
            continue
        if stroke < 1.0:
            stroke = reach

        seg = reach * depth * (1.0 - INNER_STOP_SPREAD * 0.5 * depth_spread[i])
        # 획 두께 상한 — 선이 획을 넘어 글자 속으로 파고들지 않게 한다.
        seg = min(seg, stroke * _STROKE_DEPTH_CAP)
        if seg < 1.5:
            continue

        wo = w_out * (1.0 + (rng.random() - 0.5) * 2.0 * wv)
        nx, ny = -vy, vx   # 외곽선 접선 방향(선 폭 방향)
        base_a = ((px + nx * wo / 2.0) * ss, (py + ny * wo / 2.0) * ss)
        base_b = ((px - nx * wo / 2.0) * ss, (py - ny * wo / 2.0) * ss)
        apex = ((px + vx * seg) * ss, (py + vy * seg) * ss)
        ldraw.polygon([base_a, base_b, apex], fill=255)
        drew_any = True

    if not drew_any:
        return None

    lines_img = lines_img.resize((video_width, video_height), Image.LANCZOS)

    # 글자 마스크와 곱해 글자 안쪽에만 남긴다(곱셈이라 경계 밖 유출 0).
    opacity_scale = max(0, min(255, int(alpha))) / 255.0
    combined = ImageChops.multiply(lines_img, text_mask)
    if opacity_scale < 0.999:
        combined = combined.point(lambda v, m=opacity_scale: int(v * m))

    layer = Image.new("RGBA", (video_width, video_height), (cr, cg, cb, 0))
    layer.putalpha(combined)
    return layer


# 텍스트 오버레이 좌표 계산에 쓰는 여백. 1080p 기준 40px.
#   Pillow 경로는 글리프 bbox 밖으로 나오는 descender/ascender 를 담기 위해 이 값을
#   상하좌우로 덧댄 뒤 그 확장된 박스를 기준으로 위치를 잡는다. drawtext 경로도
#   같은 값을 써야 두 경로의 자막이 같은 자리에 놓인다(경로는 그라디언트/글로우
#   유무로 갈린다). 한쪽만 바꾸면 자막이 위아래로 어긋나므로 반드시 공유한다.
_OVERLAY_TEXT_PAD_AT_1080 = 40


def _overlay_text_pad(video_height: int) -> int:
    """캔버스 높이에 비례한 텍스트 여백(px).

    고정 40px 로 두면 720p 미리보기에서는 화면 대비 여백이 1.5배 커져
    자막이 최종 렌더보다 아래로 내려간다(실측 190 vs 174, 1080 환산).
    해상도에 비례시켜 미리보기와 렌더의 자막 위치를 일치시킨다.
    """
    if video_height <= 0:
        return _OVERLAY_TEXT_PAD_AT_1080
    return max(8, int(round(_OVERLAY_TEXT_PAD_AT_1080 * video_height / 1080)))


def _render_overlay_png(
    overlay: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
    phase_bucket: "int | None" = None,
) -> str | None:
    """Pillow로 그라디언트/글로우 텍스트 PNG를 생성하고 경로를 반환한다."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        return None

    text = overlay.get("text", "")
    if not text:
        return None

    font_size = int(overlay.get("font_size", 40))
    font_family = overlay.get("font_family", "NotoSansKR")
    opacity = float(overlay.get("opacity", 1.0))
    alpha = int(opacity * 255)
    bold = bool(overlay.get("bold", False))
    align = overlay.get("align", "center")

    font_path = _find_font_path(font_family, bold=bold)
    try:
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    # bold 변형 파일이 없을 때 합성 볼드 여부 결정
    has_native_bold = bold and font_path and "bold" in Path(font_path).stem.lower()
    synthetic_bold = bold and not has_native_bold

    # 텍스트 크기 측정 (bbox offset 보정 포함)
    dummy = Image.new("RGBA", (1, 1))
    draw_dummy = ImageDraw.Draw(dummy)
    bbox = draw_dummy.textbbox((0, 0), text, font=font)
    # bbox = (left, top, right, bottom); left/top이 음수일 수 있음
    b_left, b_top, b_right, b_bottom = bbox
    PAD = _overlay_text_pad(video_height)  # 글리프 descender/ascender가 bbox 밖으로 나올 수 있으므로 넉넉히
    tw = b_right - b_left + PAD * 2
    th = b_bottom - b_top + PAD * 2

    # 전체 비디오 크기 캔버스 (투명)
    img = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x_ratio = float(overlay.get("x", 0.5))
    y_ratio = float(overlay.get("y", 0.1))
    # bbox offset 보정: textbbox의 left/top을 반영하여 실제 위치 계산
    tx = int((video_width - tw) * x_ratio) - b_left + PAD
    ty = int((video_height - th) * y_ratio) - b_top + PAD

    def _draw_text_with_bold(target_draw: "ImageDraw.ImageDraw", pos: tuple, txt: str, fnt, fill, synth: bool):
        """합성 볼드: 1px씩 오른쪽/아래에 겹쳐 그린다.

        여러 줄(\n 포함) 텍스트는 PIL 기본값이 left 정렬이라, 오버레이의 align
        설정과 무관하게 각 줄이 왼쪽에 붙었다. align 을 그대로 넘겨 줄 간 정렬을 맞춘다.
        """
        if synth:
            for ox, oy in [(1, 0), (0, 1), (1, 1)]:
                target_draw.text((pos[0] + ox, pos[1] + oy), txt, font=fnt, fill=fill, align=align)
        target_draw.text(pos, txt, font=fnt, fill=fill, align=align)

    # 글로우 효과
    glow_radius = int(overlay.get("glow_radius", 0))
    glow_color_hex = overlay.get("glow_color", "#000000")
    if glow_radius > 0:
        glow_r, glow_g, glow_b = _hex_to_rgb(glow_color_hex)
        glow_layer = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow_layer)
        _draw_text_with_bold(glow_draw, (tx, ty), text, font, (glow_r, glow_g, glow_b, alpha), synthetic_bold)
        for _ in range(glow_radius):
            glow_layer = glow_layer.filter(ImageFilter.BLUR)
        img = Image.alpha_composite(img, glow_layer)
        draw = ImageDraw.Draw(img)

    # 외곽선 (그라디언트/단색 공통 — 텍스트보다 먼저 그려야 아래에 깔림)
    outline_w = int(overlay.get("outline_width", 0))
    outline_hex = overlay.get("outline_color", "#000000")
    or_, og, ob = _hex_to_rgb(outline_hex)
    if outline_w > 0:
        for dx in range(-outline_w, outline_w + 1):
            for dy in range(-outline_w, outline_w + 1):
                if dx == 0 and dy == 0:
                    continue
                _draw_text_with_bold(draw, (tx + dx, ty + dy), text, font, (or_, og, ob, alpha), synthetic_bold)

    # 텍스트 색상 결정 (그라디언트 또는 단색)
    gradient_config = overlay.get("gradient")
    if gradient_config and gradient_config.get("stops"):
        # 그라디언트: angle에 따라 수평(0°) 또는 수직(90°) 방향 채색
        stops = gradient_config["stops"]
        angle = float(gradient_config.get("angle", 0))
        text_mask = Image.new("L", (video_width, video_height), 0)
        mask_draw = ImageDraw.Draw(text_mask)
        _draw_text_with_bold(mask_draw, (tx, ty), text, font, 255, synthetic_bold)
        grad_layer = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
        grad_y_start = ty + b_top
        grad_y_end = min(ty + b_bottom + 1, video_height)
        grad_x_start = tx + b_left
        grad_x_end = min(tx + b_right + 1, video_width)
        grad_h = max(grad_y_end - grad_y_start, 1)
        grad_w = max(grad_x_end - grad_x_start, 1)
        if abs(angle - 90) < 45 or abs(angle - 270) < 45:
            reverse = abs(angle - 270) < 45
            for py in range(grad_y_start, grad_y_end):
                raw_pos = (py - grad_y_start) / grad_h
                pos = max(0.0, min(1.0, 1.0 - raw_pos if reverse else raw_pos))
                r, g, b = _interpolate_gradient(stops, pos)
                grad_layer.paste(Image.new("RGBA", (video_width, 1), (r, g, b, alpha)), (0, py))
        else:
            reverse = abs(angle - 180) < 45
            for px in range(grad_x_start, grad_x_end):
                raw_pos = (px - grad_x_start) / grad_w
                pos = max(0.0, min(1.0, 1.0 - raw_pos if reverse else raw_pos))
                r, g, b = _interpolate_gradient(stops, pos)
                grad_layer.paste(Image.new("RGBA", (1, video_height), (r, g, b, alpha)), (px, 0))
        grad_layer.putalpha(text_mask)
        img = Image.alpha_composite(img, grad_layer)
    else:
        color_hex = overlay.get("color", "#ffffff")
        cr, cg, cb = _hex_to_rgb(color_hex)
        _draw_text_with_bold(draw, (tx, ty), text, font, (cr, cg, cb, alpha), synthetic_bold)

    # ── 텍스트 집중선 (loop_animation="speedlines") ─────────────────────────
    # 두 가지 모드가 있다.
    #   tail(기본) — 글자가 날아온 방향으로 집중선 스타일 꼬리를 글자 "밖"에 그린다.
    #                글자 획을 건드리지 않아 가독성이 유지된다.
    #   inner      — 글자 획 "안쪽"을 집중선으로 갉아낸다. 큰 글자에만 어울린다.
    # 관통(pass_*) 오버레이는 날아온 쪽(vec_in)이 곧 꼬리 방향이다. 관통이 아니면
    # 꼬리를 그릴 방향이 없으므로 inner 로 폴백한다.
    if str(overlay.get("loop_animation") or "none").lower() == "speedlines":
        _sl_cfg = dict(overlay.get("text_speed_lines") or {})
        _mode = str(_sl_cfg.get("mode") or "tail").lower()
        _pt_eff = (
            overlay.get("_pass_through_effect")
            or (overlay.get("enter_effect") if overlay.get("enter_effect") in _PASS_THROUGH_EFFECTS else None)
        )
        sl_layer = None
        if _mode == "tail" and _pt_eff:
            # vec_in = 글자가 들어온 쪽 방향 → 잔상이 남아야 할 쪽.
            _vec_in, _ = _PASS_THROUGH_VECTORS.get(_pt_eff, ((0.0, 0.0), (0.0, 0.0)))
            if _vec_in != (0.0, 0.0):
                sl_layer = _render_text_speed_tail_layer(
                    overlay, video_width, video_height, alpha,
                    direction=_vec_in, phase_bucket=phase_bucket,
                )
        if sl_layer is None:
            sl_layer = _render_text_masked_speed_lines_layer(
                overlay, video_width, video_height, alpha, phase_bucket=phase_bucket
            )
        if sl_layer is not None:
            img = Image.alpha_composite(img, sl_layer)

    suffix = "" if phase_bucket is None else f"_b{int(phase_bucket):02d}"
    out_path = str(Path(tmp_dir) / f"overlay_{idx:03d}{suffix}.png")
    img.save(out_path)
    return out_path


def _render_shape_overlay_png(
    shape_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> str | None:
    """Pillow로 도형(rect/ellipse) PNG를 video_width×video_height 크기로 생성하고 경로를 반환한다."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    shape = shape_data.get("shape", "rect")
    x = int(float(shape_data.get("x", 0)) * video_width)
    y = int(float(shape_data.get("y", 0)) * video_height)
    w = int(float(shape_data.get("width", 0.1)) * video_width)
    h = int(float(shape_data.get("height", 0.1)) * video_height)
    color_hex = shape_data.get("color", "#ff0000").lstrip("#")
    opacity = float(shape_data.get("opacity", 1.0))
    alpha = max(0, min(255, int(opacity * 255)))
    try:
        r, g, b = int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 255, 0, 0

    img = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = [x, y, x + w, y + h]
    if shape == "ellipse":
        draw.ellipse(bbox, fill=(r, g, b, alpha))
    else:
        draw.rectangle(bbox, fill=(r, g, b, alpha))

    out_path = str(Path(tmp_dir) / f"shape_{idx:03d}.png")
    img.save(out_path, "PNG")
    return out_path


def _resolve_overlay_canvas(
    src_path: str,
    video_width: int,
    video_height: int,
    where: str,
) -> tuple[int, int]:
    """오버레이 합성에 쓸 (width, height) 를 실제 영상 크기로 보정한다.

    오버레이 합성은 rawvideo 파이프를 ``width*height*3`` 바이트 단위로 잘라 읽는다.
    호출 측이 넘긴 "캔버스 크기"가 실제 디코딩 프레임 크기와 다르면 바이트 경계가
    어긋나 프레임이 비스듬히 밀리거나 색이 깨진다(치명적·조용한 실패).

    미리보기 경로(`web.backend.routers.project._apply_image_overlays_preview`)는
    예전부터 ffprobe 로 실측해 보정했지만 최종 렌더 경로는 캔버스 값을 그대로 믿었다.
    두 경로의 이 비대칭을 없애기 위해 공통 헬퍼로 통일한다.

    실측에 실패하면 넘겨받은 값을 그대로 쓴다(회귀 안전 — 기존 동작 유지).
    """
    actual = get_video_size(src_path)
    if not actual:
        return video_width, video_height
    if actual != (video_width, video_height):
        console.print(
            f"  [yellow]{where}: 캔버스({video_width}x{video_height}) ≠ "
            f"실제 영상({actual[0]}x{actual[1]}) — 실제 크기로 보정합니다[/yellow]"
        )
    return actual


def apply_shape_overlays(
    src_path: str,
    dst_path: str,
    shape_overlays: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """도형 오버레이를 Pillow PNG → ffmpeg overlay 방식으로 적용한다.

    성공 시 True 반환. 실패 시 src를 dst로 복사하고 False 반환.
    """
    import shutil

    if not shape_overlays:
        shutil.copy2(src_path, dst_path)
        return True

    video_width, video_height = _resolve_overlay_canvas(
        src_path, video_width, video_height, "도형 오버레이"
    )
    tmp_dir = tempfile.mkdtemp(prefix="vag_shape_overlay_")
    try:
        from PIL import Image as _PILImage

        # #287 — enter/exit 페이드 적용 위해 sh_data dict 자체도 함께 보관 (text_supported=False)
        overlay_layers: list[tuple] = []
        for i, sh_data in enumerate(shape_overlays):
            png_path = _render_shape_overlay_png(sh_data, video_width, video_height, tmp_dir, i)
            if not png_path:
                continue
            try:
                png_img = _PILImage.open(png_path).convert("RGBA")
            except Exception:
                continue
            start_sec = float(sh_data.get("start_sec", 0.0))
            end_sec = sh_data.get("end_sec")
            overlay_layers.append((png_img, start_sec, end_sec, sh_data))

        if not overlay_layers:
            shutil.copy2(src_path, dst_path)
            return True

        fps_r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        fps_str = fps_r.stdout.strip()
        fps = 30.0
        if "/" in fps_str:
            n, d = fps_str.split("/")
            fps = float(n) / max(float(d), 1)
        elif fps_str:
            try:
                fps = float(fps_str)
            except ValueError:
                pass

        audio_check = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        has_audio_stream = bool(audio_check.stdout.strip())

        overlay_out = str(Path(tmp_dir) / "shape_overlay.mp4")
        audio_tmp = str(Path(tmp_dir) / "audio_tmp.aac")
        if has_audio_stream:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-i", src_path, "-vn", "-c:a", "copy", audio_tmp],
                capture_output=True,
            )

        dec = subprocess.Popen(
            [ffmpeg_bin(), "-i", src_path,
             "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        enc_cmd = [
            ffmpeg_bin(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{video_width}x{video_height}",
            "-r", str(fps),
            "-i", "pipe:0",
        ]
        if has_audio_stream and Path(audio_tmp).exists() and Path(audio_tmp).stat().st_size > 0:
            enc_cmd += ["-i", audio_tmp, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
        else:
            enc_cmd += ["-an"]
        enc_cmd += [*_video_encoder_args(60),
                    "-pix_fmt", "yuv420p", overlay_out]
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        # #287 — 클립 길이(visual_clip_duration)로 end_sec=None인 fade 종료점 결정
        try:
            clip_duration = get_duration(src_path)
        except Exception:
            clip_duration = None

        frame_size = video_width * video_height * 3
        frame_idx = 0
        # 도형 오버레이 페이드 인/아웃 (#ux) — 등장/사라짐 0.3초 알파 보간.
        FADE_BASE_SHAPE = 0.3
        try:
            while True:
                raw = dec.stdout.read(frame_size)
                if len(raw) < frame_size:
                    break
                t = frame_idx / fps
                base = _PILImage.frombytes("RGB", (video_width, video_height), raw).convert("RGBA")
                for (png_img, start_sec, end_sec, sh_data) in overlay_layers:
                    if t < start_sec:
                        continue
                    if end_sec is not None and t > end_sec:
                        continue
                    # #287 — enter/exit 페이드 적용 (없으면 1.0)
                    alpha_scalar = _compute_overlay_alpha_scalar(
                        sh_data, t,
                        visual_clip_duration=clip_duration,
                        text_supported=False,
                    )
                    if alpha_scalar >= 0.999:
                        base = _PILImage.alpha_composite(base, png_img)
                    elif alpha_scalar > 0.0:
                        r_, g_, b_, a_ = png_img.split()
                        a_scaled = a_.point(lambda v, m=alpha_scalar: int(v * m))
                        faded = _PILImage.merge("RGBA", (r_, g_, b_, a_scaled))
                        base = _PILImage.alpha_composite(base, faded)
                enc.stdin.write(base.convert("RGB").tobytes())
                frame_idx += 1
        finally:
            dec.stdout.close()
            dec.wait()
            enc.stdin.close()
            enc.wait()

        if Path(overlay_out).exists() and Path(overlay_out).stat().st_size > 0:
            shutil.copy2(overlay_out, dst_path)
            return True
        else:
            shutil.copy2(src_path, dst_path)
            return False

    except Exception as e:
        console.print(f"  [yellow]도형 오버레이 적용 실패 (건너뜀): {e}[/yellow]")
        import shutil as _sh2
        _sh2.copy2(src_path, dst_path)
        return False
    finally:
        import shutil as _sh3
        _sh3.rmtree(tmp_dir, ignore_errors=True)


# ── Image Overlay ───────────────────────────────────────────────────────────
# footage 스튜디오의 이미지 자산을 씬 위에 자유 위치·크기로 합성한다.
# 캡션(이미지 아래 텍스트 박스)도 같은 오버레이 단위에서 함께 그린다.
# 좌표/크기 규약은 ShapeOverlay/MosaicOverlay와 동일 (좌상단 기준 0~1 비율).


def _ease_out_cubic(p: float) -> float:
    """0→1 로 진행되는 정규화 시간 p 에 대해 ease-out cubic 값을 반환.

    슬라이드/zoom 진입/이탈이 뚝뚝 끊기지 않고 자연스럽게 감속/가속하도록 사용.
    """
    p = max(0.0, min(1.0, p))
    return 1.0 - (1.0 - p) ** 3


def _compute_image_overlay_transform(
    overlay: dict,
    t: float,
    video_width: int,
    video_height: int,
    visual_clip_duration: float | None = None,
    text_supported: bool = False,
) -> tuple[float, float, float, float]:
    """이미지 오버레이의 매 프레임 변환 파라미터 (dx, dy, scale, alpha) 계산 (#issue-확장).

    반환값:
      - dx, dy: 정적 layer 위치에서의 픽셀 오프셋 (슬라이드 계열)
      - scale: 정적 layer 크기 대비 스케일 (zoom/pop 계열, 1.0 = 원본)
      - alpha: 알파 배수 (fade + 페이드 폴백)

    슬라이드 방향은 "화면 밖 → 원위치" 로 정의:
      - slide_in_left  : 왼쪽 밖에서 오른쪽으로 이동해 원위치. exit 시 왼쪽으로 되돌아가며 사라짐.
      - slide_in_right : 오른쪽 밖에서 왼쪽으로. exit 시 오른쪽으로.
      - slide_in_top   : 위쪽 밖에서 아래로. exit 시 위로.
      - slide_in_bottom: 아래쪽 밖에서 위로. exit 시 아래로.

    zoom : scale 0.3 → 1.0 (진입), 1.0 → 0.3 (이탈)
    pop  : scale 1.3 → 1.0 오버슈트(진입). 이탈은 1.0 → 1.3.
    none/fade 및 알 수 없는 값: 오프셋 0, 스케일 1.0.

    alpha 는 fade 효과일 때만 페이드 보간되며, 그 외에는 1.0.
    (slide/zoom/pop 자체는 alpha 로는 사라지지 않고 오프셋/스케일로 사라짐 표현.)
    """
    # text_supported=True 면 텍스트 오버레이 규칙(typing/zoom 허용)으로 해석한다.
    # 미리보기의 텍스트 관통 효과가 이 경로를 쓴다.
    if text_supported:
        enter_effect, exit_effect, enter_dur, exit_dur = _get_overlay_effect_config(
            overlay, text_supported=True
        )
    else:
        enter_effect, exit_effect, enter_dur, exit_dur = _get_overlay_effect_config(
            overlay, image_supported=True
        )
    start = float(overlay.get("start_sec", 0.0) or 0.0)
    end_raw = overlay.get("end_sec")
    if end_raw is not None:
        end = float(end_raw)
    elif visual_clip_duration is not None and visual_clip_duration > 0:
        end = float(visual_clip_duration)
    else:
        end = float("inf")

    # 현재 프레임이 enter/steady/exit 구간 중 어디에 속하는지 판별.
    in_enter = enter_dur > 0 and t < start + enter_dur
    in_exit = (
        exit_dur > 0
        and end != float("inf")
        and t > end - exit_dur
    )

    dx = 0.0
    dy = 0.0
    scale = 1.0
    alpha = 1.0

    # 슬라이드 오프셋 계산에 쓸 "화면 밖" 기준 거리.
    # 화면 폭/높이의 100% 를 밖으로 밀면 완전히 사라짐 → 그것보다 조금 더 밀어 확실히 안 보이게.
    off_w = float(video_width) * 1.1
    off_h = float(video_height) * 1.1

    def _slide_offset(direction: str, progress_out: float) -> tuple[float, float]:
        """progress_out=0 → 화면 밖(시작 위치), progress_out=1 → 원위치(오프셋 0)."""
        p = max(0.0, min(1.0, progress_out))
        remaining = 1.0 - p  # 밖으로 나가있는 비율
        if direction == "slide_in_left":
            return (-off_w * remaining, 0.0)
        if direction == "slide_in_right":
            return (off_w * remaining, 0.0)
        if direction == "slide_in_top":
            return (0.0, -off_h * remaining)
        if direction == "slide_in_bottom":
            return (0.0, off_h * remaining)
        return (0.0, 0.0)

    def _pass_offset(effect: str, progress: float, leaving: bool) -> tuple[float, float]:
        """관통 효과의 오프셋.

        leaving=False(진입): progress 0→1 이면 진입쪽 화면 밖 → 원위치.
        leaving=True(퇴장):  progress 1→0 이면 원위치 → 퇴장쪽(반대편) 화면 밖.
        """
        vec_in, vec_out = _PASS_THROUGH_VECTORS.get(effect, ((0.0, 0.0), (0.0, 0.0)))
        p = max(0.0, min(1.0, progress))
        vx, vy = vec_out if leaving else vec_in
        remaining = 1.0 - p  # 밖으로 나가있는 비율
        return (vx * off_w * remaining, vy * off_h * remaining)

    if in_enter:
        phase = (t - start) / enter_dur if enter_dur > 0 else 1.0
        eased = _ease_out_cubic(phase)
        if enter_effect == "fade":
            alpha = min(alpha, max(0.0, min(1.0, phase)))
        elif enter_effect in _PASS_THROUGH_EFFECTS:
            dx, dy = _pass_offset(enter_effect, eased, leaving=False)
        elif enter_effect.startswith("slide_in_"):
            dx, dy = _slide_offset(enter_effect, eased)
        elif enter_effect == "zoom":
            # 0.3 → 1.0
            scale = 0.3 + (1.0 - 0.3) * eased
        elif enter_effect == "pop":
            # 1.3 → 1.0 (오버슈트 느낌: 살짝 튀었다가 자리 잡음).
            # eased 만 쓰면 단조 감소라 튐이 약하므로, 후반부에 아주 살짝만 언더슛(0.97)했다 복귀.
            base = 1.3 + (1.0 - 1.3) * eased  # 1.3 → 1.0
            # 진입 마지막 20% 구간에서 살짝 언더슛(0.97) 후 1.0 로 복귀
            if phase > 0.8:
                bounce_p = (phase - 0.8) / 0.2  # 0→1
                # 0 → -0.03 → 0 (사인 반주기)
                import math as _m
                bounce = -0.03 * _m.sin(bounce_p * _m.pi)
                scale = base + bounce
            else:
                scale = base
        # "none" 은 변환 없음.

    if in_exit and end != float("inf"):
        # exit 구간에서는 진입과 대칭.
        phase = (end - t) / exit_dur if exit_dur > 0 else 1.0  # 1→0
        eased = _ease_out_cubic(phase)  # 1→0
        if exit_effect == "fade":
            alpha = min(alpha, max(0.0, min(1.0, phase)))
        elif exit_effect in _PASS_THROUGH_EFFECTS:
            # 관통: 들어온 쪽이 아니라 "반대편"으로 빠져나간다.
            dx, dy = _pass_offset(exit_effect, eased, leaving=True)
        elif exit_effect.startswith("slide_in_"):
            # 진입과 반대 방향으로 슬라이드아웃.
            # phase=1(exit 시작)이면 원위치, phase=0(exit 끝)이면 화면 밖.
            dx, dy = _slide_offset(exit_effect, eased)
        elif exit_effect == "zoom":
            # 1.0 → 0.3
            scale = 0.3 + (1.0 - 0.3) * eased
        elif exit_effect == "pop":
            # 1.0 → 1.3 (커지면서 사라짐)
            scale = 1.3 + (1.0 - 1.3) * eased
        # "none" 은 변환 없음.

    return dx, dy, scale, max(0.0, min(1.0, alpha))


def _render_image_overlay_layer(
    overlay_data: dict,
    video_width: int,
    video_height: int,
) -> "Image.Image | None":  # type: ignore[name-defined]
    """ImageOverlay 1개를 (video_width×video_height) 크기의 RGBA 레이어로 렌더한다.

    이미지 파일은 ``overlay_data["_image_path"]`` 키로 미리 절대 경로가 들어와 있어야 한다
    (백엔드 라우터/_build_sel 에서 image_asset_id → studio_assets → footage_dir 로 해석).
    해당 키가 없거나 파일이 존재하지 않으면 None을 반환해 호출 측이 스킵하도록 한다.

    캡션은 이미지 *아래*에 별도 박스로 그려진다(이미지 면적은 보존).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    img_path = overlay_data.get("_image_path")
    if not img_path or not Path(img_path).exists():
        return None

    try:
        x_ratio = float(overlay_data.get("x", 0.05))
        y_ratio = float(overlay_data.get("y", 0.05))
        w_ratio = float(overlay_data.get("width", 0.25))
        h_ratio = float(overlay_data.get("height", 0.20))
    except (TypeError, ValueError):
        return None

    # ⚠ 픽셀 값이 들어온 경우 방어 — x/y/width/height 는 0.0~1.0 비율 규약이다.
    #   옛 데이터/외부 편집으로 픽셀 값(예: width=620.0)이 섞이면
    #   620 * 1920 = 119만 픽셀 폭짜리 이미지를 할당하려다 메모리를 수 TB 요구하고
    #   백엔드 프로세스가 OOM 으로 통째로 죽는다(실측: 씬 18 미리보기에서 11GB 까지
    #   치솟다 사망 → 미리보기가 영영 안 뜸). 캔버스 기준으로 환산해 살려낸다.
    if w_ratio > 1.0 or h_ratio > 1.0 or x_ratio > 1.0 or y_ratio > 1.0:
        console.print(
            f"  [yellow]이미지 오버레이 좌표가 비율(0~1) 범위를 벗어남 — 픽셀 값으로 보고 환산합니다: "
            f"id={overlay_data.get('id')} x={x_ratio} y={y_ratio} w={w_ratio} h={h_ratio}[/yellow]"
        )
        if w_ratio > 1.0:
            w_ratio = w_ratio / video_width
        if h_ratio > 1.0:
            h_ratio = h_ratio / video_height
        if x_ratio > 1.0:
            x_ratio = x_ratio / video_width
        if y_ratio > 1.0:
            y_ratio = y_ratio / video_height

    # 그래도 이상하면(음수/과대) 안전 범위로 클램프 — 어떤 데이터가 와도 죽지 않는다.
    w_ratio = max(0.001, min(1.0, w_ratio))
    h_ratio = max(0.001, min(1.0, h_ratio))
    x_ratio = max(0.0, min(1.0, x_ratio))
    y_ratio = max(0.0, min(1.0, y_ratio))

    # 화면 사이즈 기반 절대 좌표 — zoom 모션과 무관. (x, y) 는 박스 좌상단.
    box_w = max(1, int(round(w_ratio * video_width)))
    box_h = max(1, int(round(h_ratio * video_height)))
    img_x = int(round(x_ratio * video_width))
    img_y = int(round(y_ratio * video_height))
    # 박스가 화면 밖으로 나가면 안쪽으로 자동 클램프 — 사용자가 x=0.6/width=0.5 같이 설정해도
    # 이미지의 우측 절반이 잘리지 않게 보정한다 (사용자가 본 "x 가 zoom 으로 이동" 증상의 진짜 원인).
    img_x = max(0, min(img_x, video_width - box_w))
    img_y = max(0, min(img_y, video_height - box_h))

    opacity = max(0.0, min(1.0, float(overlay_data.get("opacity", 1.0) or 1.0)))

    # 1) 캔버스(전체 영상 크기) 위에 이미지를 합성하기 위해 RGBA 빈 캔버스 준비
    canvas = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))

    # 2) 이미지 로드 — 원본 종횡비 유지(contain) 하면서 (box_w, box_h) 박스 안에 맞춤.
    # 이전엔 강제 (img_w, img_h) 리사이즈라 사용자가 박스 비율을 영상 비율(보통 16:9) 그대로 두면
    # 세로 이미지가 가로로 늘어나는 회귀가 있었다.
    try:
        src_img = Image.open(img_path).convert("RGBA")
    except Exception:
        return None
    try:
        resample = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover
        resample = Image.LANCZOS  # type: ignore[attr-defined]

    src_w, src_h = src_img.size
    if src_w <= 0 or src_h <= 0:
        return None
    src_ratio = src_w / src_h
    box_ratio = box_w / box_h
    if src_ratio >= box_ratio:
        # 가로가 더 넓음 — 박스 폭에 맞추고 세로는 비율대로.
        img_w = box_w
        img_h = max(1, int(round(box_w / src_ratio)))
    else:
        # 세로가 더 김 — 박스 높이에 맞추고 가로는 비율대로.
        img_h = box_h
        img_w = max(1, int(round(box_h * src_ratio)))
    # 박스 안 가운데 정렬(여백은 투명).
    img_x_centered = img_x + (box_w - img_w) // 2
    img_y_centered = img_y + (box_h - img_h) // 2
    scaled = src_img.resize((img_w, img_h), resample=resample)

    # 3) opacity 적용
    if opacity < 0.999:
        r_, g_, b_, a_ = scaled.split()
        a_scaled = a_.point(lambda v, m=opacity: int(round(v * m)))
        scaled = Image.merge("RGBA", (r_, g_, b_, a_scaled))

    canvas.alpha_composite(scaled, dest=(img_x_centered, img_y_centered))

    # 4) 캡션 — caption_text가 있으면 이미지 *아래*에 박스 추가
    caption_text = overlay_data.get("caption_text")
    if isinstance(caption_text, str) and caption_text.strip():
        try:
            font_ratio = float(overlay_data.get("caption_font_size", 0.025) or 0.025)
        except (TypeError, ValueError):
            font_ratio = 0.025
        font_size_px = max(8, int(round(font_ratio * video_width)))
        try:
            padding_ratio = float(overlay_data.get("caption_padding", 0.005) or 0.005)
        except (TypeError, ValueError):
            padding_ratio = 0.005
        padding_px = max(0, int(round(padding_ratio * video_width)))

        # 폰트 로드 — NotoSansKR 우선, 못 찾으면 기본 폰트.
        font_path = _find_font_path("NotoSansKR")
        try:
            font = ImageFont.truetype(font_path, font_size_px) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()

        # 캡션 박스 영역: 종횡비 보정 후의 *실제 이미지* 폭에 맞춤, 높이는 폰트+패딩×2.
        cap_x = img_x_centered
        cap_w = img_w
        cap_h = font_size_px + padding_px * 2

        # 기본: 이미지 *아래*. 아래가 화면 밖이면 이미지 *위*로 폴백.
        # 위도 부족하면 이미지 영역 *내부 하단* 에 겹쳐 그린다 (이미지 가림이 있어도 캡션 보장).
        cap_y_below = img_y_centered + img_h
        cap_y_above = img_y_centered - cap_h
        if cap_y_below + cap_h <= video_height:
            cap_y = cap_y_below
        elif cap_y_above >= 0:
            cap_y = cap_y_above
        else:
            # 둘 다 안 되면 이미지 하단 내부에 겹침.
            cap_y = max(0, img_y_centered + img_h - cap_h)

        bg_color_hex = overlay_data.get("caption_bg_color")
        try:
            bg_opacity = float(overlay_data.get("caption_bg_opacity", 0.6) or 0.6)
        except (TypeError, ValueError):
            bg_opacity = 0.6
        bg_opacity = max(0.0, min(1.0, bg_opacity))

        if bg_color_hex:
            try:
                br, bg, bb = _parse_hex_color(bg_color_hex, default=(0, 0, 0))
            except Exception:
                br, bg, bb = 0, 0, 0
            bg_alpha = int(round(bg_opacity * 255))
            draw_bg = ImageDraw.Draw(canvas)
            draw_bg.rectangle(
                [cap_x, cap_y, cap_x + cap_w, cap_y + cap_h],
                fill=(br, bg, bb, bg_alpha),
            )

        # 텍스트 색
        try:
            tr, tg, tb = _parse_hex_color(
                overlay_data.get("caption_color", "#FFFFFF"), default=(255, 255, 255)
            )
        except Exception:
            tr, tg, tb = 255, 255, 255
        text_alpha = int(round(opacity * 255))

        # 가로/세로 중앙 정렬. PIL textbbox 의 top/left 오프셋(폰트 ascent)이 0 이 아닐 수 있어
        # text 그리기 좌표에서 그만큼 빼줘야 박스 안에서 정확히 중앙 정렬됨 — 안 빼면 캡션이 박스
        # 상단으로 치우치고, 하단 padding 이 시각적으로 더 커보이는(=상단에만 여백 적용된 듯한) 회귀 발생.
        draw_txt = ImageDraw.Draw(canvas)
        tb_left = tb_top = 0
        try:
            tb_box = draw_txt.textbbox((0, 0), caption_text, font=font)
            tb_left = tb_box[0]
            tb_top = tb_box[1]
            text_w = tb_box[2] - tb_box[0]
            text_h = tb_box[3] - tb_box[1]
        except Exception:
            try:
                text_w, text_h = draw_txt.textsize(caption_text, font=font)  # type: ignore[attr-defined]
            except Exception:
                text_w, text_h = cap_w, font_size_px
        text_px = cap_x + max(0, (cap_w - text_w) // 2) - tb_left
        text_py = cap_y + max(0, (cap_h - text_h) // 2) - tb_top
        draw_txt.text(
            (text_px, text_py),
            caption_text,
            font=font,
            fill=(tr, tg, tb, text_alpha),
        )

    return canvas


def apply_image_overlays(
    src_path: str,
    dst_path: str,
    image_overlays: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """이미지 오버레이를 Pillow alpha_composite + ffmpeg 재인코딩으로 적용한다.

    각 overlay dict 는 ``image_asset_id`` 기반 데이터에 더해 ``_image_path`` 키로 절대
    파일 경로가 주입되어 있어야 한다(호출 측 책임). 키가 없거나 파일이 없는 항목은 스킵된다.

    성공 시 True 반환. 실패 시 src를 dst로 복사하고 False 반환.
    apply_shape_overlays / apply_graphic_overlays 와 동일한 decode→PIL composite→encode 파이프.
    """
    import shutil

    if not image_overlays:
        shutil.copy2(src_path, dst_path)
        return True

    video_width, video_height = _resolve_overlay_canvas(
        src_path, video_width, video_height, "이미지 오버레이"
    )
    tmp_dir = tempfile.mkdtemp(prefix="vag_image_overlay_")
    try:
        from PIL import Image as _PILImage

        # (crop_img, (x0, y0), start_sec, end_sec, ov_data)
        # 속도 최적화 — 전체 캔버스(예: 1920×1080 RGBA ≈ 8MB) 레이어 대신 실제 그림이 있는
        # 영역(알파 bbox)만 잘라 보관하고 그 영역만 합성한다. 결과 픽셀은 기존(전체 캔버스
        # alpha_composite)과 같다 — bbox 밖은 알파 0 이라 합성해도 변화가 없기 때문.
        overlay_layers: list[tuple] = []
        for ov in image_overlays:
            layer = _render_image_overlay_layer(ov, video_width, video_height)
            if layer is None:
                continue
            bbox = layer.getchannel("A").getbbox()
            if bbox is None:
                continue  # 완전 투명 — 합성해도 변화 없음
            crop = layer.crop(bbox)
            del layer
            start_sec = float(ov.get("start_sec", 0.0) or 0.0)
            end_sec = ov.get("end_sec")
            overlay_layers.append((crop, (bbox[0], bbox[1]), start_sec, end_sec, ov))

        if not overlay_layers:
            shutil.copy2(src_path, dst_path)
            return True

        fps_r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        fps_str = fps_r.stdout.strip()
        fps = 30.0
        if "/" in fps_str:
            n, d = fps_str.split("/")
            try:
                fps = float(n) / max(float(d), 1)
            except ValueError:
                pass
        elif fps_str:
            try:
                fps = float(fps_str)
            except ValueError:
                pass

        audio_check = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        has_audio_stream = bool(audio_check.stdout.strip())

        overlay_out = str(Path(tmp_dir) / "image_overlay.mp4")
        audio_tmp = str(Path(tmp_dir) / "audio_tmp.aac")
        if has_audio_stream:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-i", src_path, "-vn", "-c:a", "copy", audio_tmp],
                capture_output=True,
            )

        dec = subprocess.Popen(
            [ffmpeg_bin(), "-i", src_path,
             "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        enc_cmd = [
            ffmpeg_bin(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{video_width}x{video_height}",
            "-r", str(fps),
            "-i", "pipe:0",
        ]
        if has_audio_stream and Path(audio_tmp).exists() and Path(audio_tmp).stat().st_size > 0:
            enc_cmd += ["-i", audio_tmp, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
        else:
            enc_cmd += ["-an"]
        enc_cmd += [*_video_encoder_args(60),
                    "-pix_fmt", "yuv420p", overlay_out]
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        try:
            clip_duration = get_duration(src_path)
        except Exception:
            clip_duration = None

        frame_size = video_width * video_height * 3
        frame_idx = 0
        try:
            resample_lanczos = None
            try:
                resample_lanczos = _PILImage.Resampling.LANCZOS  # type: ignore[attr-defined]
            except AttributeError:  # pragma: no cover
                resample_lanczos = _PILImage.LANCZOS  # type: ignore[attr-defined]

            def _with_alpha(img, alpha_scalar):
                if alpha_scalar >= 0.999:
                    return img
                r_, g_, b_, a_ = img.split()
                a_scaled = a_.point(lambda v, m=alpha_scalar: int(v * m))
                return _PILImage.merge("RGBA", (r_, g_, b_, a_scaled))

            def _composite_region(base_rgb, piece, x, y):
                """불투명 RGB 프레임의 (x, y) 영역에 RGBA piece 를 합성(화면 밖 부분은 잘라냄).

                영역만 RGBA 로 바꿔 합성한 뒤 되돌려 붙인다 — 프레임 전체 RGBA 변환·합성을 피한다.
                바탕이 불투명이라 합성 결과도 불투명 → RGB 로 되돌려도 손실이 없다.
                """
                pw, ph = piece.size
                x0, y0 = max(0, x), max(0, y)
                x1, y1 = min(video_width, x + pw), min(video_height, y + ph)
                if x1 <= x0 or y1 <= y0:
                    return
                if (x0, y0, x1, y1) != (x, y, x + pw, y + ph):
                    piece = piece.crop((x0 - x, y0 - y, x1 - x, y1 - y))
                box = (x0, y0, x1, y1)
                region = base_rgb.crop(box).convert("RGBA")
                region.alpha_composite(piece)
                base_rgb.paste(region.convert("RGB"), box)

            while True:
                raw = dec.stdout.read(frame_size)
                if len(raw) < frame_size:
                    break
                t = frame_idx / fps
                active = [
                    lyr for lyr in overlay_layers
                    if t >= lyr[2] and (lyr[3] is None or t <= lyr[3])
                ]
                if not active:
                    # 이 프레임엔 보이는 이미지가 없음 — 디코드 결과를 그대로 넘긴다(합성 생략).
                    enc.stdin.write(raw)
                    frame_idx += 1
                    continue
                base = _PILImage.frombytes("RGB", (video_width, video_height), raw)
                for (crop_img, (cx0, cy0), start_sec, end_sec, ov_data) in active:
                    # 이미지 오버레이 전용 변환 파라미터 (dx/dy 픽셀 오프셋, scale, alpha 배수).
                    # slide/zoom/pop 등 확장 효과가 있으면 dx/dy/scale 로 반영,
                    # fade/none 인 경우엔 dx=0/dy=0/scale=1.0 이므로 아래 fast path 로 빠진다.
                    dx, dy, scale, alpha_scalar = _compute_image_overlay_transform(
                        ov_data, t,
                        video_width=video_width,
                        video_height=video_height,
                        visual_clip_duration=clip_duration,
                    )
                    if alpha_scalar <= 0.0:
                        continue
                    # ── Fast path: 애니메이션이 없는 정상 상태(기존 fade/none 경로와 동일) ──
                    if abs(dx) < 0.5 and abs(dy) < 0.5 and abs(scale - 1.0) < 1e-3:
                        _composite_region(base, _with_alpha(crop_img, alpha_scalar), cx0, cy0)
                        continue

                    # ── Slow path: dx/dy 이동 + scale ──
                    # 기존 의미 유지: "전체 캔버스 레이어를 round(W*scale)×round(H*scale) 로 리사이즈해
                    # 화면 중앙에 놓고 dx/dy 만큼 민다" 를 잘린 조각(crop) 기준으로 똑같이 계산한다.
                    piece = crop_img
                    px, py = cx0, cy0
                    if abs(scale - 1.0) >= 1e-3:
                        new_w = max(1, int(round(video_width * scale)))
                        new_h = max(1, int(round(video_height * scale)))
                        sx, sy = new_w / video_width, new_h / video_height
                        cw = max(1, int(round(crop_img.width * sx)))
                        ch = max(1, int(round(crop_img.height * sy)))
                        try:
                            piece = crop_img.resize((cw, ch), resample=resample_lanczos)
                        except Exception:
                            piece = crop_img
                        px = (video_width - new_w) // 2 + int(round(cx0 * sx))
                        py = (video_height - new_h) // 2 + int(round(cy0 * sy))
                    if abs(dx) >= 0.5 or abs(dy) >= 0.5:
                        px += int(round(dx))
                        py += int(round(dy))
                    # alpha 배수 적용 (fade 와 slide/zoom/pop 동시 사용 시 대비).
                    _composite_region(base, _with_alpha(piece, alpha_scalar), px, py)
                enc.stdin.write(base.tobytes())
                frame_idx += 1
        finally:
            dec.stdout.close()
            dec.wait()
            enc.stdin.close()
            enc.wait()

        if Path(overlay_out).exists() and Path(overlay_out).stat().st_size > 0:
            shutil.copy2(overlay_out, dst_path)
            return True
        else:
            shutil.copy2(src_path, dst_path)
            return False

    except Exception as e:
        console.print(f"  [yellow]이미지 오버레이 적용 실패 (건너뜀): {e}[/yellow]")
        import shutil as _sh2
        _sh2.copy2(src_path, dst_path)
        return False
    finally:
        import shutil as _sh3
        _sh3.rmtree(tmp_dir, ignore_errors=True)


# ── Graphic Overlay (#205) ──────────────────────────────────────────────────

def _parse_hex_color(color_hex: str, default=(255, 0, 0)) -> tuple[int, int, int]:
    """'#rrggbb' 또는 'rrggbb' → (r, g, b)."""
    try:
        h = color_hex.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError, AttributeError, TypeError):
        return default


def _render_spotlight_png(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "str | None":
    """집중조명(spotlight) PNG를 생성한다.

    동작: 검은 RGBA 레이어에서 중심 원형 영역만 알파를 0으로 깎아내어
    프레임 위에 합성하면 가운데가 밝고 바깥은 어둡게 보이도록 한다.

    params:
        radius (float, 비율): 원의 반경 (짧은 변 기준 비율). 기본 0.2
        darkness (float, 0~1): 바깥 영역 어둠 강도 (alpha = darkness * 255). 기본 0.6
        feather (int, px): 마스크 엣지 가우시안 블러 반경. 기본 40
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return None

    cx = float(overlay_data.get("x", 0.5)) * video_width
    cy = float(overlay_data.get("y", 0.5)) * video_height
    params = overlay_data.get("params") or {}

    short_edge = min(video_width, video_height)
    radius = float(params.get("radius", 0.2)) * short_edge
    radius = max(10.0, radius)
    darkness = float(params.get("darkness", 0.6))
    darkness = max(0.0, min(1.0, darkness))
    feather = int(params.get("feather", 40))
    feather = max(0, feather)

    dark_alpha = int(darkness * 255)

    # 1) 어둠 레이어: 전체를 검정+darkness 알파로 채움
    dark_layer = Image.new("RGBA", (video_width, video_height), (0, 0, 0, dark_alpha))

    # 2) 마스크: 흰색=알파 유지, 검은색=알파 0(투명).
    #    중심 원을 검은색으로 그리면 그 영역의 어둠이 제거됨.
    mask = Image.new("L", (video_width, video_height), 255)
    mdraw = ImageDraw.Draw(mask)
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    mdraw.ellipse(bbox, fill=0)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather))

    # 마스크를 알파 채널로 적용
    dark_layer.putalpha(
        Image.eval(mask, lambda v: int(v * dark_alpha / 255))
    )

    out_path = str(Path(tmp_dir) / f"graphic_spotlight_{idx:03d}.png")
    dark_layer.save(out_path, "PNG")
    return out_path


def _render_alert_png(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "str | None":
    """알림 팝업(alert) PNG를 생성한다.

    동작: 둥근 사각 배경 + 좌측 아이콘(이모지/심볼) + 텍스트를 합성하여
    프레임의 (x, y) 위치(중심 기준)에 얹는다.

    params:
        text (str): 표시 문자열. 기본 "알림"
        style (str): "warning" | "info" | "error". 기본 "warning"
        width (float, 비율): 팝업 너비 (영상 너비 대비). 기본 0.4
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    params = overlay_data.get("params") or {}
    text = str(params.get("text", "알림"))
    style = str(params.get("style", "warning")).lower()
    box_w_ratio = float(params.get("width", 0.4))
    box_w_ratio = max(0.1, min(1.0, box_w_ratio))

    box_w = int(box_w_ratio * video_width)
    # 텍스트 길이/스타일에 따라 높이 동적 결정 (단순화: 너비의 ~20%)
    box_h = max(int(box_w * 0.22), 70)

    # 스타일 → 배경/테두리/아이콘 색
    style_palette = {
        "warning": {"bg": (255, 235, 130, 230), "border": (200, 130, 0, 255), "fg": (50, 30, 0, 255), "icon": "!"},
        "info":    {"bg": (200, 230, 255, 230), "border": (40, 100, 200, 255), "fg": (10, 30, 80, 255), "icon": "i"},
        "error":   {"bg": (255, 200, 200, 230), "border": (180, 30, 30, 255), "fg": (80, 0, 0, 255), "icon": "x"},
    }
    palette = style_palette.get(style, style_palette["warning"])

    # 팝업 카드를 별도 RGBA 이미지로 그린 뒤, 큰 영상 캔버스의 (x,y) 중심에 붙임
    card = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(card)
    radius = max(8, box_h // 5)

    # 둥근 사각 배경
    try:
        cdraw.rounded_rectangle([0, 0, box_w - 1, box_h - 1], radius=radius, fill=palette["bg"], outline=palette["border"], width=3)
    except AttributeError:
        # 매우 오래된 Pillow는 rounded_rectangle 미지원 — 일반 사각형 폴백
        cdraw.rectangle([0, 0, box_w - 1, box_h - 1], fill=palette["bg"], outline=palette["border"], width=3)

    # 폰트 로드 — 한글 가능한 폰트 시도, 실패 시 기본 폰트
    font = None
    title_font = None
    font_candidates = [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        # Windows — 없으면 load_default() 로 떨어져 한글이 두부(□)로 깨진다.
        "C:/Windows/Fonts/malgunbd.ttf",
        "C:/Windows/Fonts/malgun.ttf",
        "assets/fonts/NotoSansKR-Regular.ttf",
    ]
    text_size = max(18, box_h // 3)
    icon_size = max(24, box_h // 2)
    for cand in font_candidates:
        try:
            font = ImageFont.truetype(cand, text_size)
            title_font = ImageFont.truetype(cand, icon_size)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()
        title_font = font

    # 아이콘 영역 — Twemoji 자산이 있으면 원형 배경 없이 그대로 그리고, 없으면
    # 원형 배경 + 흰 글자 폴백 (#71).
    icon_pad = box_h // 6
    icon_d = box_h - 2 * icon_pad

    from src.graphic_assets import (
        add_outer_glow, load_resized_asset, preferred_glow_color,
    )

    alert_asset = load_resized_asset("alert", style, icon_d)
    if alert_asset is not None:
        glow_color = preferred_glow_color("alert", style)
        glowed = add_outer_glow(alert_asset, color_rgb=glow_color, intensity=1.2)
        if glowed is not None:
            # 글로우로 캔버스가 커진 만큼 카드 안 아이콘 위치를 중앙 정렬 보정
            gw, gh = glowed.size
            offset = (icon_d - gw) // 2  # 음수 — 글로우가 넘침
            card.alpha_composite(glowed, dest=(icon_pad + offset, icon_pad + offset))
        else:
            card.alpha_composite(alert_asset, dest=(icon_pad, icon_pad))
    else:
        cdraw.ellipse(
            [icon_pad, icon_pad, icon_pad + icon_d, icon_pad + icon_d],
            fill=palette["border"], outline=palette["border"],
        )
        # 아이콘 글자 중앙 정렬
        icon_ch = palette["icon"]
        icon_offset_y = 0
        try:
            bbox_i = title_font.getbbox(icon_ch)
            iw = bbox_i[2] - bbox_i[0]
            ih = bbox_i[3] - bbox_i[1]
            icon_offset_y = -bbox_i[1]
        except AttributeError:
            iw, ih = title_font.getsize(icon_ch)
        cdraw.text(
            (icon_pad + (icon_d - iw) / 2, icon_pad + (icon_d - ih) / 2 + icon_offset_y),
            icon_ch, fill=(255, 255, 255, 255), font=title_font,
        )

    # 텍스트 — 아이콘 오른쪽
    tx0 = icon_pad + icon_d + icon_pad
    ty0 = (box_h - text_size) // 2 - 2
    cdraw.text((tx0, ty0), text, fill=palette["fg"], font=font)

    # 영상 크기 캔버스에 (x,y) 중심으로 배치
    canvas = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    cx = float(overlay_data.get("x", 0.5)) * video_width
    cy = float(overlay_data.get("y", 0.5)) * video_height
    px = int(cx - box_w / 2)
    py = int(cy - box_h / 2)
    canvas.alpha_composite(card, dest=(px, py))

    out_path = str(Path(tmp_dir) / f"graphic_alert_{idx:03d}.png")
    canvas.save(out_path, "PNG")
    return out_path


def _render_target_png(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "str | None":
    """타겟 마커(target) PNG를 생성한다.

    params:
        marker_type (str): "crosshair" | "arrow". 기본 "crosshair"
        size (float, 비율): 마커 전체 크기 (짧은 변 기준). 기본 0.1
        color (str): hex 색상. 기본 "#ff3030" (빨강)
        rotation (float, deg): 회전 각도. arrow에서 주로 사용. 기본 0
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    params = overlay_data.get("params") or {}
    marker_type = str(params.get("marker_type", "crosshair")).lower()
    # #72 — Twemoji 자산 + 글로우 도입 이후 인지 크기 보완. 기본을 0.1 → 0.18 로.
    size_ratio = float(params.get("size", 0.18))
    size_ratio = max(0.02, min(0.5, size_ratio))
    color_param = params.get("color")
    color_rgb = _parse_hex_color(str(color_param) if color_param else "#ff3030", default=(255, 48, 48))
    rotation = float(params.get("rotation", 0.0))

    short_edge = min(video_width, video_height)
    marker_px = max(20, int(size_ratio * short_edge))

    # ── Twemoji 자산 우선 (#71) ───────────────────────────────────────────
    # 사용자가 ``color`` 를 직접 지정한 경우에만 기존 벡터 그리기로 폴백 (자산은
    # 다채색이라 colorize 시 톤이 깨짐). rotation 은 자산에서도 PIL 회전으로 적용.
    if not color_param:
        from src.graphic_assets import (
            add_outer_glow, load_resized_asset, preferred_glow_color,
        )

        asset = load_resized_asset("target", marker_type, marker_px)
        if asset is not None:
            # 글로우는 회전 전에 적용 (블러가 회전 후 적용되면 비대칭 잔상 발생)
            glow_color = preferred_glow_color("target", marker_type)
            glowed = add_outer_glow(asset, color_rgb=glow_color, intensity=1.2)
            final_marker = glowed if glowed is not None else asset
            if rotation != 0.0:
                final_marker = final_marker.rotate(rotation, resample=Image.BICUBIC, expand=True)
            canvas = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
            cx = float(overlay_data.get("x", 0.5)) * video_width
            cy = float(overlay_data.get("y", 0.5)) * video_height
            mw2, mh2 = final_marker.size
            px = int(cx - mw2 / 2)
            py = int(cy - mh2 / 2)
            canvas.alpha_composite(final_marker, dest=(px, py))
            out_path = str(Path(tmp_dir) / f"graphic_target_{idx:03d}.png")
            canvas.save(out_path, "PNG")
            return out_path

    # 마커는 별도 RGBA 캔버스(marker_px x marker_px)에 그린 뒤 회전 후 합성
    pad = max(6, marker_px // 8)  # 외곽선/꼬리가 잘리지 않도록 패딩
    mw = marker_px + pad * 2
    mh = marker_px + pad * 2
    marker_img = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
    mdraw = ImageDraw.Draw(marker_img)

    color_rgba = color_rgb + (235,)
    line_w = max(2, marker_px // 16)
    cx_m = mw // 2
    cy_m = mh // 2
    r = marker_px // 2

    if marker_type == "arrow":
        # 위쪽을 향하는 화살표(삼각형 머리 + 꼬리). rotation 0 = 위쪽.
        head_w = marker_px // 2
        head_h = int(marker_px * 0.55)
        tail_w = max(4, marker_px // 6)
        tail_h = marker_px - head_h
        # 머리(삼각형)
        head = [
            (cx_m, cy_m - r),
            (cx_m - head_w, cy_m - r + head_h),
            (cx_m + head_w, cy_m - r + head_h),
        ]
        mdraw.polygon(head, fill=color_rgba, outline=(255, 255, 255, 255))
        # 꼬리(직사각형)
        tail_top = cy_m - r + head_h
        tail = [
            cx_m - tail_w // 2, tail_top,
            cx_m + tail_w // 2, tail_top + tail_h,
        ]
        mdraw.rectangle(tail, fill=color_rgba, outline=(255, 255, 255, 255))
    else:
        # crosshair: 외곽 원 + 십자선
        # 외곽 원
        mdraw.ellipse(
            [cx_m - r, cy_m - r, cx_m + r, cy_m + r],
            outline=color_rgba, width=line_w,
        )
        # 내부 작은 원(중앙 점)
        inner_r = max(2, r // 8)
        mdraw.ellipse(
            [cx_m - inner_r, cy_m - inner_r, cx_m + inner_r, cy_m + inner_r],
            fill=color_rgba,
        )
        # 십자선(중심부에 갭)
        gap = r // 3
        mdraw.line([(cx_m, cy_m - r), (cx_m, cy_m - gap)], fill=color_rgba, width=line_w)
        mdraw.line([(cx_m, cy_m + gap), (cx_m, cy_m + r)], fill=color_rgba, width=line_w)
        mdraw.line([(cx_m - r, cy_m), (cx_m - gap, cy_m)], fill=color_rgba, width=line_w)
        mdraw.line([(cx_m + gap, cy_m), (cx_m + r, cy_m)], fill=color_rgba, width=line_w)

    # 회전 (expand=True로 잘림 방지)
    if rotation != 0.0:
        marker_img = marker_img.rotate(rotation, resample=Image.BICUBIC, expand=True)

    # 영상 캔버스에 (x,y) 중심으로 합성
    canvas = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    cx = float(overlay_data.get("x", 0.5)) * video_width
    cy = float(overlay_data.get("y", 0.5)) * video_height
    mw2, mh2 = marker_img.size
    px = int(cx - mw2 / 2)
    py = int(cy - mh2 / 2)
    canvas.alpha_composite(marker_img, dest=(px, py))

    out_path = str(Path(tmp_dir) / f"graphic_target_{idx:03d}.png")
    canvas.save(out_path, "PNG")
    return out_path


def _build_reaction_icon_image(
    overlay_data: dict,
    video_width: int,
    video_height: int,
):
    """리액션 아이콘 RGBA PIL.Image와 (iw, ih)를 반환한다.

    `_render_reaction_png`(정적)와 `_build_reaction_dynamic_layer`(동적 bounce)에서 공유.
    PIL 미설치 시 (None, 0, 0)을 반환한다.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None, 0, 0

    params = overlay_data.get("params") or {}
    icon_type = str(params.get("icon_type", "thunder")).lower()
    # #72 — Twemoji 자산 + 글로우 도입 이후 인지 크기 보완. 기본을 0.15 → 0.22 로.
    size_ratio = float(params.get("size", 0.22))
    size_ratio = max(0.04, min(0.6, size_ratio))
    custom_color = params.get("color")

    # 기본 색 매핑
    default_color_map = {
        "thunder": (255, 210, 0),     # 옐로우
        "sweat": (140, 200, 245),     # 라이트블루
        "question": (90, 160, 255),   # 블루
        "exclamation": (255, 100, 100),  # 레드
    }
    base_rgb = default_color_map.get(icon_type, (255, 210, 0))
    if custom_color:
        base_rgb = _parse_hex_color(str(custom_color), default=base_rgb)

    short_edge = min(video_width, video_height)
    icon_px = max(28, int(size_ratio * short_edge))

    # ── Twemoji 자산 우선 (#71) ───────────────────────────────────────────
    # 사용자가 ``color`` 를 직접 지정하지 않은 경우에만 자산을 사용. 자산은 다채색
    # 그래픽이라 colorize 시 톤이 깨지므로 색 커스터마이즈는 기존 벡터 그리기로
    # 폴백한다. 단색 자산이 영상 배경에 묻히는 문제를 보완하기 위해 자산 주색상
    # 기반 아우터 글로우를 자동 적용(#71 후속).
    if not custom_color:
        from src.graphic_assets import (
            add_outer_glow, load_resized_asset, preferred_glow_color,
        )

        asset = load_resized_asset("reaction", icon_type, icon_px)
        if asset is not None:
            glow_color = preferred_glow_color("reaction", icon_type)
            glowed = add_outer_glow(asset, color_rgb=glow_color, intensity=1.3)
            if glowed is not None:
                iw, ih = glowed.size
                icon_img = glowed
            else:
                pad = max(6, icon_px // 8)
                iw = icon_px + pad * 2
                ih = icon_px + pad * 2
                icon_img = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
                icon_img.alpha_composite(asset, dest=(pad, pad))
            return icon_img, iw, ih

    pad = max(6, icon_px // 8)
    iw = icon_px + pad * 2
    ih = icon_px + pad * 2
    icon_img = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    idraw = ImageDraw.Draw(icon_img)

    cx_i = iw // 2
    cy_i = ih // 2
    outline_rgba = (40, 40, 40, 255)
    outline_w = max(2, icon_px // 24)

    _draw_reaction_icon_shape(
        icon_type, idraw, cx_i, cy_i, icon_px,
        base_rgb, outline_rgba, outline_w,
        ImageFont,
    )
    return icon_img, iw, ih


def _draw_reaction_icon_shape(
    icon_type: str,
    idraw,
    cx_i: int,
    cy_i: int,
    icon_px: int,
    base_rgb: tuple,
    outline_rgba: tuple,
    outline_w: int,
    ImageFont,
) -> None:
    """아이콘 형상 자체를 idraw에 그린다 (reaction 4종)."""
    if icon_type == "thunder":
        top = cy_i - icon_px // 2
        bot = cy_i + icon_px // 2
        pts = [
            (cx_i + icon_px // 6, top),
            (cx_i - icon_px // 4, cy_i - icon_px // 8),
            (cx_i, cy_i - icon_px // 8),
            (cx_i - icon_px // 6, bot),
            (cx_i + icon_px // 3, cy_i + icon_px // 10),
            (cx_i, cy_i + icon_px // 10),
        ]
        idraw.polygon(pts, fill=base_rgb + (240,), outline=outline_rgba)
    elif icon_type == "sweat":
        r = icon_px // 2
        idraw.ellipse(
            [cx_i - r // 1, cy_i - r // 3, cx_i + r // 1, cy_i + r],
            fill=base_rgb + (235,), outline=outline_rgba, width=outline_w,
        )
        idraw.polygon(
            [
                (cx_i, cy_i - r),
                (cx_i - r // 2, cy_i - r // 6),
                (cx_i + r // 2, cy_i - r // 6),
            ],
            fill=base_rgb + (235,), outline=outline_rgba,
        )
        hr = max(2, r // 5)
        idraw.ellipse(
            [cx_i - r // 3 - hr, cy_i - hr, cx_i - r // 3 + hr, cy_i + hr],
            fill=(255, 255, 255, 200),
        )
    elif icon_type in ("question", "exclamation"):
        r = icon_px // 2
        idraw.ellipse(
            [cx_i - r, cy_i - r, cx_i + r, cy_i + r],
            fill=base_rgb + (235,), outline=outline_rgba, width=outline_w,
        )
        ch = "?" if icon_type == "question" else "!"
        font = None
        font_size = max(16, int(icon_px * 0.7))
        font_candidates = [
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            # Windows — 없으면 load_default() 로 떨어져 한글이 두부(□)로 깨진다.
            "C:/Windows/Fonts/malgunbd.ttf",
            "C:/Windows/Fonts/malgun.ttf",
            "assets/fonts/NotoSansKR-Regular.ttf",
        ]
        for cand in font_candidates:
            try:
                font = ImageFont.truetype(cand, font_size)
                break
            except (OSError, IOError):
                continue
        if font is None:
            font = ImageFont.load_default()
        try:
            bbox_t = font.getbbox(ch)
            tw = bbox_t[2] - bbox_t[0]
            th = bbox_t[3] - bbox_t[1]
            t_off_y = -bbox_t[1]
        except AttributeError:
            tw, th = font.getsize(ch)
            t_off_y = 0
        idraw.text(
            (cx_i - tw / 2, cy_i - th / 2 + t_off_y),
            ch, fill=(255, 255, 255, 255), font=font,
        )
    else:
        r = icon_px // 2
        idraw.ellipse(
            [cx_i - r, cy_i - r, cx_i + r, cy_i + r],
            fill=base_rgb + (235,), outline=outline_rgba, width=outline_w,
        )


def _build_reaction_dynamic_layer(
    overlay_data: dict,
    video_width: int,
    video_height: int,
):
    """reaction.bounce=true용 동적 layer 데이터를 반환한다.

    반환 dict 필드:
        kind: "dynamic_reaction"
        icon_image: PIL.RGBA
        iw, ih: 아이콘 캔버스 크기
        anchor_x, anchor_y: 기준 중심 좌표 (영상 픽셀)
        start_sec, end_sec, bounce_freq, bounce_amp_px
    """
    icon_img, iw, ih = _build_reaction_icon_image(overlay_data, video_width, video_height)
    if icon_img is None:
        return None

    params = overlay_data.get("params") or {}
    bounce_freq = float(params.get("bounce_freq", 2.0))
    bounce_freq = max(0.1, min(10.0, bounce_freq))
    amp_ratio = float(params.get("bounce_amplitude_ratio", 0.15))
    amp_ratio = max(0.0, min(1.0, amp_ratio))
    # amp_ratio: 아이콘 크기(짧은 변 기준) 대비 진폭. icon_px ≈ min(iw,ih) - pad*2
    icon_px = min(iw, ih)
    amp_px = max(1.0, amp_ratio * icon_px)
    # #224 — 흔들림 모드 ("sine" 기본, "perlin"이면 자연스러운 1D noise)
    motion_raw = str(params.get("motion", "sine")).strip().lower()
    motion = motion_raw if motion_raw in ("sine", "perlin") else "sine"

    anchor_x = float(overlay_data.get("x", 0.5)) * video_width
    anchor_y = float(overlay_data.get("y", 0.5)) * video_height

    return {
        "kind": "dynamic_reaction",
        "icon_image": icon_img,
        "iw": iw,
        "ih": ih,
        "anchor_x": anchor_x,
        "anchor_y": anchor_y,
        "start_sec": float(overlay_data.get("start_sec", 0.0)),
        "end_sec": overlay_data.get("end_sec"),
        "bounce_freq": bounce_freq,
        "bounce_amp_px": amp_px,
        "motion": motion,
        # #287 — enter/exit 효과 계산용 원본
        "overlay_data": overlay_data,
    }


def _render_reaction_png(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "str | None":
    """리액션(reaction) 아이콘 PNG를 생성한다 — 정적 1장.

    bounce=true인 경우는 호출 측(_render_graphic_overlay_png)이
    별도 dynamic layer 경로를 타므로 여기까지 오지 않는다.

    params:
        icon_type (str): "thunder" | "sweat" | "question" | "exclamation". 기본 "thunder"
        size (float, 비율): 아이콘 크기 (짧은 변 기준). 기본 0.15
        color (str, optional): hex. 미지정 시 icon_type별 기본색.
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    icon_img, iw, ih = _build_reaction_icon_image(overlay_data, video_width, video_height)
    if icon_img is None:
        return None

    # 영상 캔버스에 합성
    canvas = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    cx = float(overlay_data.get("x", 0.5)) * video_width
    cy = float(overlay_data.get("y", 0.5)) * video_height
    px = int(cx - iw / 2)
    py = int(cy - ih / 2)
    canvas.alpha_composite(icon_img, dest=(px, py))

    out_path = str(Path(tmp_dir) / f"graphic_reaction_{idx:03d}.png")
    canvas.save(out_path, "PNG")
    return out_path


def _render_smell_png(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "str | None":
    """냄새/연기(smell) PNG를 생성한다 — 정적 1장 vertical gradient.

    loop=true인 경우는 호출 측(_render_graphic_overlay_png)이 별도
    dynamic layer 경로를 타므로 여기까지 오지 않는다.

    params:
        width (float, 비율): 폭 (영상 너비 대비). 기본 0.25
        height (float, 비율): 높이 (영상 높이 대비). 기본 0.35
        color (str): "green" | "brown" | "white" 또는 hex. 기본 "green"
        intensity (float, 0~1): 알파 multiplier. 기본 0.7
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    cloud, box_w, box_h = _build_smell_cloud_image(
        overlay_data, video_width, video_height,
    )
    if cloud is None:
        return None

    # 영상 캔버스에 (x,y) 중심으로 합성
    canvas = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    cx = float(overlay_data.get("x", 0.5)) * video_width
    cy = float(overlay_data.get("y", 0.5)) * video_height
    px = int(cx - box_w / 2)
    py = int(cy - box_h / 2)
    canvas.alpha_composite(cloud, dest=(px, py))

    out_path = str(Path(tmp_dir) / f"graphic_smell_{idx:03d}.png")
    canvas.save(out_path, "PNG")
    return out_path


def _build_smell_cloud_image(
    overlay_data: dict,
    video_width: int,
    video_height: int,
):
    """smell 구름 RGBA PIL.Image와 (box_w, box_h)를 반환한다.

    `_render_smell_png`(정적)와 `_build_smell_dynamic_layer`(loop)에서 공유.
    PIL 미설치 시 (None, 0, 0)을 반환한다.
    """
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter
    except ImportError:
        return None, 0, 0

    params = overlay_data.get("params") or {}
    w_ratio = float(params.get("width", 0.25))
    h_ratio = float(params.get("height", 0.35))
    w_ratio = max(0.05, min(1.0, w_ratio))
    h_ratio = max(0.05, min(1.0, h_ratio))
    color_in = str(params.get("color", "green")).strip().lower()
    intensity = float(params.get("intensity", 0.7))
    intensity = max(0.0, min(1.0, intensity))

    color_map = {
        "green": (90, 138, 58),
        "brown": (122, 90, 42),
        "white": (232, 232, 232),
        "yellow": (210, 200, 80),
        "gray": (140, 140, 140),
    }
    if color_in in color_map:
        color_rgb = color_map[color_in]
    else:
        color_rgb = _parse_hex_color(color_in, default=color_map["green"])

    box_w = max(20, int(w_ratio * video_width))
    box_h = max(20, int(h_ratio * video_height))

    base_alpha = int(intensity * 255)

    # ── Twemoji 자산 우선 (#71) ───────────────────────────────────────────
    # cloud.png 를 box 가로폭으로 리사이즈하고 색조 변환 후 세로 fade 만 적용.
    # 블러 미적용 (자산이 이미 카툰체로 다듬어져 있음). 글로우는 색조와 같은
    # 톤으로 살짝만 (#71 후속).
    from src.graphic_assets import _load_asset_cached, add_outer_glow, colorize_asset

    asset_cloud = _load_asset_cached("smell", "cloud")
    if asset_cloud is not None:
        target_w = box_w
        scale = target_w / asset_cloud.width
        target_h = max(10, int(asset_cloud.height * scale))
        scaled = asset_cloud.copy().resize((target_w, target_h), Image.LANCZOS)
        tinted = colorize_asset(scaled, color_rgb)
        if tinted is None:
            tinted = scaled

        # 색조와 동일한 글로우. smell 은 자체 영역이 크므로 intensity 낮춤.
        glowed = add_outer_glow(tinted, color_rgb=color_rgb, intensity=0.7)
        final_cloud = glowed if glowed is not None else tinted

        # box_h 캔버스에 세로 중앙 정렬해 배치 (자산 비율 유지)
        fw, fh = final_cloud.size
        cloud = Image.new("RGBA", (max(box_w, fw), max(box_h, fh)), (0, 0, 0, 0))
        x_off = max(0, (cloud.width - fw) // 2)
        y_off = max(0, (cloud.height - fh) // 2)
        cloud.alpha_composite(final_cloud, dest=(x_off, y_off))

        # intensity 알파 + 세로 fade (위로 갈수록 진하게 — 기존 동작과 동일)
        cw, ch = cloud.size
        fade = Image.new("L", (cw, ch), 255)
        fade_pixels = fade.load()
        for y in range(ch):
            ratio = y / max(1, ch - 1)
            f_val = int(255 * (ratio ** 1.3))
            for x in range(cw):
                fade_pixels[x, y] = f_val
        r_c, g_c, b_c, a_c = cloud.split()
        a_scaled = Image.eval(a_c, lambda v: int(v * base_alpha / 255))
        new_alpha = ImageChops.multiply(a_scaled, fade)
        cloud = Image.merge("RGBA", (r_c, g_c, b_c, new_alpha))
        return cloud, cw, ch

    cloud = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(cloud)

    main_alpha = int(base_alpha * 0.95)
    cdraw.ellipse(
        [int(box_w * 0.05), int(box_h * 0.45), int(box_w * 0.95), int(box_h * 0.98)],
        fill=color_rgb + (main_alpha,),
    )
    puffs = [
        (0.20, 0.20, 0.60, 0.70, 0.75),
        (0.05, 0.05, 0.45, 0.55, 0.55),
        (0.55, 0.00, 0.95, 0.50, 0.55),
        (0.30, -0.10, 0.70, 0.35, 0.35),
    ]
    for (x0, y0, x1, y1, a_mul) in puffs:
        a = int(base_alpha * a_mul)
        bx0 = int(box_w * x0)
        by0 = int(box_h * y0)
        bx1 = int(box_w * x1)
        by1 = int(box_h * y1)
        cdraw.ellipse([bx0, by0, bx1, by1], fill=color_rgb + (a,))

    fade = Image.new("L", (box_w, box_h), 255)
    fade_pixels = fade.load()
    for y in range(box_h):
        ratio = y / max(1, box_h - 1)
        f_val = int(255 * (ratio ** 1.3))
        for x in range(box_w):
            fade_pixels[x, y] = f_val

    r_c, g_c, b_c, a_c = cloud.split()
    new_alpha = Image.eval(a_c, lambda v: v)
    new_alpha = ImageChops.multiply(new_alpha, fade)
    cloud = Image.merge("RGBA", (r_c, g_c, b_c, new_alpha))

    blur_r = max(4, min(box_w, box_h) // 12)
    cloud = cloud.filter(ImageFilter.GaussianBlur(radius=blur_r))

    return cloud, box_w, box_h


def _build_dash_mark_image(overlay_data: dict, video_width: int, video_height: int):
    """dash 오버레이의 마크 1개("=3" 등)를 RGBA 이미지로 그린다.

    반환: (PIL.RGBA, w, h) — 실패 시 (None, 0, 0).
    마크는 진행 방향을 향하므로 direction="left" 면 좌우 반전한다
    (기본 문자 "=3" 은 오른쪽을 향하는 모양이라 왼쪽 이동 시 뒤집어야 자연스럽다).
    """
    from PIL import Image, ImageDraw, ImageFont

    params = overlay_data.get("params") or {}
    mark = str(params.get("mark", "=3")) or "=3"
    # size: 캔버스 폭 대비 마크 높이 비율.
    size_ratio = float(params.get("size", 0.06))
    size_ratio = max(0.01, min(0.5, size_ratio))
    font_px = max(8, int(video_width * size_ratio))
    color = _hex_to_rgb(str(params.get("color", "#222222")))
    direction = str(params.get("direction", "right")).strip().lower()
    if direction not in ("left", "right"):
        direction = "right"

    font_path = _find_font_path("NotoSansKR", bold=True) or _find_font_path("NotoSansKR")
    try:
        font = ImageFont.truetype(font_path, font_px) if font_path else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    # 텍스트 박스 측정 후 여유를 두고 캔버스 생성(외곽선 대비).
    probe = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), mark, font=font)
    tw = max(1, bbox[2] - bbox[0])
    th = max(1, bbox[3] - bbox[1])
    pad = max(2, font_px // 8)
    w, h = tw + pad * 2, th + pad * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 흰 외곽선 — 어두운 배경에서도 마크가 보이도록.
    ox, oy = pad - bbox[0], pad - bbox[1]
    stroke = max(1, font_px // 20)
    d.text((ox, oy), mark, font=font, fill=color + (255,),
           stroke_width=stroke, stroke_fill=(255, 255, 255, 200))
    if direction == "left":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    return img, w, h


def _build_dash_dynamic_layer(
    overlay_data: dict,
    video_width: int,
    video_height: int,
):
    """dash(질주 "=3=3=3") 동적 layer 데이터를 반환한다.

    마크 N개가 진행 방향으로 일정 간격을 두고 늘어서 있고, 프레임마다 전체가
    같은 속도로 흘러간다. 한 칸(간격) 만큼 이동하면 위치를 되감아(modulo) 무한
    반복되므로 끊김 없이 "슉슉 지나가는" 느낌이 난다.

    반환 dict 필드:
        kind: "dynamic_dash"
        mark_image, mw, mh: 마크 이미지와 크기
        count: 마크 개수
        step_px: 마크 간 간격(px)
        anchor_x, anchor_y: 기준 중심 좌표(px)
        dir_sign: +1(오른쪽) / -1(왼쪽)
        speed_px_sec: 초당 이동 픽셀
        trail: 뒤쪽 마크를 흐리게 할지 여부
        start_sec, end_sec
    """
    mark_img, mw, mh = _build_dash_mark_image(overlay_data, video_width, video_height)
    if mark_img is None:
        return None

    params = overlay_data.get("params") or {}
    count = int(params.get("count", 3))
    count = max(1, min(12, count))
    spacing = float(params.get("spacing", 1.15))   # 마크 폭 대비 간격 배수
    spacing = max(0.2, min(4.0, spacing))
    step_px = max(1.0, mw * spacing)
    direction = str(params.get("direction", "right")).strip().lower()
    dir_sign = -1.0 if direction == "left" else 1.0
    # speed: 초당 "간격 몇 칸" 을 이동할지. 1.0 이면 1초에 한 칸.
    speed = float(params.get("speed", 3.0))
    speed = max(0.1, min(30.0, speed))
    trail = bool(params.get("trail", True))

    return {
        "kind": "dynamic_dash",
        "mark_image": mark_img,
        "mw": mw,
        "mh": mh,
        "count": count,
        "step_px": step_px,
        "anchor_x": float(overlay_data.get("x", 0.5)) * video_width,
        "anchor_y": float(overlay_data.get("y", 0.5)) * video_height,
        "dir_sign": dir_sign,
        "speed_px_sec": step_px * speed,
        "trail": trail,
        "start_sec": float(overlay_data.get("start_sec", 0.0)),
        "end_sec": overlay_data.get("end_sec"),
    }


def _build_smell_dynamic_layer(
    overlay_data: dict,
    video_width: int,
    video_height: int,
):
    """smell.loop=true용 동적 layer 데이터를 반환한다.

    반환 dict 필드:
        kind: "dynamic_smell"
        cloud_image: PIL.RGBA (frame 루프에서 위로 y_offset 이동 + alpha 변조)
        box_w, box_h
        anchor_x, anchor_y: 기준 중심 좌표
        start_sec, end_sec, loop_cycle_sec, loop_alpha_min
        rise_px: 한 사이클 동안 위로 이동할 픽셀 거리 (= box_h * 0.5)
    """
    cloud, box_w, box_h = _build_smell_cloud_image(overlay_data, video_width, video_height)
    if cloud is None:
        return None

    params = overlay_data.get("params") or {}
    cycle_sec = float(params.get("loop_cycle_sec", 2.0))
    cycle_sec = max(0.2, min(20.0, cycle_sec))
    alpha_min = float(params.get("loop_alpha_min", 0.5))
    alpha_min = max(0.0, min(1.0, alpha_min))
    # #224 — 모드 ("linear" 기본 = 기존 동작, "perlin"이면 좌우 흔들림 추가)
    motion_raw = str(params.get("motion", "linear")).strip().lower()
    motion = motion_raw if motion_raw in ("linear", "perlin") else "linear"

    anchor_x = float(overlay_data.get("x", 0.5)) * video_width
    anchor_y = float(overlay_data.get("y", 0.5)) * video_height
    rise_px = float(box_h) * 0.5

    return {
        "kind": "dynamic_smell",
        "cloud_image": cloud,
        "box_w": box_w,
        "box_h": box_h,
        "anchor_x": anchor_x,
        "anchor_y": anchor_y,
        "start_sec": float(overlay_data.get("start_sec", 0.0)),
        "end_sec": overlay_data.get("end_sec"),
        "loop_cycle_sec": cycle_sec,
        "loop_alpha_min": alpha_min,
        "rise_px": rise_px,
        "motion": motion,
        # #287 — enter/exit 효과 계산용 원본
        "overlay_data": overlay_data,
    }


# ── Emoji Graphic Overlay (#288) ───────────────────────────────────────────
# 이모지 아이콘 4종 (question/lens/prohibit/poo)을 시스템 컬러 이모지 폰트로 렌더.
# size 기본값 150, 범위 32~400. drawtext의 fontsize와 동일한 1:1 픽셀 매핑.
# Apple Color Emoji는 macOS에서 비트맵 컬러 폰트라 특정 사이즈만 지원 (20/32/40/48/64/96/160).
# 어떤 size든 동일하게 그려지도록 가장 큰 네이티브 사이즈로 렌더 후 PIL.resize로 스케일링.

# #emoji-catalog — 이모지 목록.
# 키는 저장값이라 절대 바꾸지 않는다(기존 프로젝트 호환). 추가만 한다.
# 앞의 4개(question/lens/prohibit/poo)가 원래 있던 항목.
# 모든 글리프는 Apple Color Emoji 로 렌더됨을 실측 확인(두부 0건).
EMOJI_ICON_TABLE: dict = {
    # ── 기본/강조 ──────────────────────────────────────────────────────
    "question": "❓", "lens": "🔍", "prohibit": "🚫", "poo": "💩",
    "exclaim": "❗", "bulb": "💡", "fire": "🔥", "sparkles": "✨",
    "star": "⭐", "hundred": "💯", "zap": "⚡", "boom": "💥",
    "target": "🎯", "alarm": "⏰", "pin": "📌", "bell": "🔔",
    # ── 표정 ───────────────────────────────────────────────────────────
    "grin": "😀", "joy": "😂", "sweat_smile": "😅", "heart_eyes": "😍",
    "cool": "😎", "thinking": "🤔", "scream": "😱", "sob": "😭",
    "rage": "😡", "cold": "🥶", "exploding": "🤯", "sleep": "😴",
    "roll_eyes": "🙄", "innocent": "😇", "rofl": "🤣", "cry": "😢",
    # ── 손짓/반응 ──────────────────────────────────────────────────────
    "thumbsup": "👍", "thumbsdown": "👎", "clap": "👏", "pray": "🙏",
    "muscle": "💪", "eyes": "👀", "salute": "🫡", "handshake": "🤝",
    "check": "✅", "cross": "❌", "warning": "⚠️", "heart": "❤️",
    "broken_heart": "💔", "party": "🎉", "confetti": "🎊", "trophy": "🏆",
    # ── 게임/제작 ──────────────────────────────────────────────────────
    "money": "💰", "money_fly": "💸", "gamepad": "🎮", "joystick": "🕹️",
    "desktop": "🖥️", "phone": "📱", "keyboard": "⌨️", "clapper": "🎬",
    "camera": "🎥", "video_camera": "📹", "tv": "📺", "mic": "🎤", "music": "🎵",
    "speaker": "🔊", "mute": "🔇", "megaphone": "📢", "speech": "💬",
    # ── 사극/판타지 ────────────────────────────────────────────────────
    "sword": "🗡️", "shield": "🛡️", "bow": "🏹", "crown": "👑",
    "dragon": "🐉", "wizard": "🧙", "ninja": "🥷", "crossed_swords": "⚔️",
    "castle": "🏰", "scroll": "📜", "moai": "🗿", "shrine": "⛩️",
    "volcano": "🌋", "wave": "🌊", "beach": "🏖", "skull": "☠️", "ghost": "👻",
    # ── 기타 ───────────────────────────────────────────────────────────
    "rice": "🍚", "noodle": "🍜", "chicken": "🍗", "coffee": "☕",
    "beer": "🍺", "brain": "🧠", "hourglass": "⏳", "rocket": "🚀",
    "hammer": "🛠️", "wrench": "🔧", "chart_up": "📈", "chart_down": "📉",
    "puzzle": "🧩", "dice": "🎲", "joker": "🃏", "crystal": "🔮",
}

# Apple Color Emoji가 지원하는 네이티브 사이즈 (실측 기준, Pillow 12.x).
_EMOJI_NATIVE_SIZES = [20, 32, 40, 48, 64, 96, 160]


def _find_emoji_font_path() -> "str | None":
    """이모지 폰트 파일 경로를 탐색한다. macOS 우선, 리눅스/기타는 fallback."""
    candidates = [
        # macOS — 비트맵 컬러 이모지 (시스템 기본)
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        # macOS 사용자 폰트
        "/Library/Fonts/Apple Color Emoji.ttc",
        # Linux — Noto Color Emoji (있으면 우선)
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
        # Windows (참고용)
        "C:\\Windows\\Fonts\\seguiemj.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _render_emoji_png(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "str | None":
    """이모지 그래픽 오버레이 PNG를 생성한다 (#288).

    params:
        icon_type (str): "question" | "lens" | "prohibit" | "poo"
        size (int, px): 폰트 크기. 32~400 권장. drawtext fontsize와 동일한 픽셀 단위.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    params = overlay_data.get("params") or {}
    icon_type = str(params.get("icon_type", "question")).lower()
    emoji_char = EMOJI_ICON_TABLE.get(icon_type)
    if not emoji_char:
        console.print(
            f"  [yellow]알 수 없는 emoji icon_type='{icon_type}' (스킵)[/yellow]"
        )
        return None

    try:
        size_px = int(params.get("size", 150))
    except (TypeError, ValueError):
        size_px = 150
    size_px = max(8, min(800, size_px))

    font_path = _find_emoji_font_path()
    if not font_path:
        console.print(
            "  [yellow]이모지 폰트를 찾을 수 없어 emoji 오버레이를 스킵합니다 "
            "(macOS: /System/Library/Fonts/Apple Color Emoji.ttc, Linux: Noto Color Emoji)[/yellow]"
        )
        return None

    # 사이즈 결정 전략:
    #  - Apple Color Emoji 같은 비트맵 컬러 폰트는 특정 사이즈만 로드 가능.
    #  - 목표 size_px를 직접 로드해보고 실패하면 가장 가까운(>=) 네이티브 사이즈로 로드 후 PIL.resize.
    font = None
    try:
        font = ImageFont.truetype(font_path, size_px)
        native_size = size_px
    except Exception:
        # 가장 가까운 큰 네이티브 사이즈로 로드 (퀄리티 유지 위해 ≥ target 우선)
        native = next((s for s in _EMOJI_NATIVE_SIZES if s >= size_px), _EMOJI_NATIVE_SIZES[-1])
        try:
            font = ImageFont.truetype(font_path, native)
            native_size = native
        except Exception as e:
            console.print(f"  [yellow]이모지 폰트 로드 실패: {e}[/yellow]")
            return None

    # 이모지 측정 후 적당한 캔버스에 렌더
    # canvas: 짧은변 1.3배 padding (글리프가 cell 경계 밖으로 살짝 나올 수 있음)
    canvas_size = int(native_size * 1.35)
    tile = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tile)
    # 중앙 정렬 (anchor="mm"). Apple Color Emoji는 embedded_color=True 필수.
    try:
        tdraw.text(
            (canvas_size // 2, canvas_size // 2),
            emoji_char,
            font=font,
            embedded_color=True,
            anchor="mm",
        )
    except Exception:
        # anchor 미지원 폴백
        tdraw.text(
            (canvas_size // 4, canvas_size // 4),
            emoji_char,
            font=font,
            embedded_color=True,
        )

    # 실제 글리프 영역만 잘라낸 뒤 목표 크기(size_px)에 꽉 맞춘다.
    #   미리보기(OverlayCanvas)는 size 를 "이모지가 차지하는 한 변"으로 그린다.
    #   반면 여기서 폰트 크기를 size_px 로 주면 Apple Color Emoji 글리프가 em 의 약 0.78배라
    #   실제 이모지가 요청보다 작게 나왔다(실측: 미리보기 117px vs 렌더 79px).
    #   → 여백을 잘라내고 size_px 로 리사이즈해 두 경로의 시각 크기를 일치시킨다.
    bbox = tile.getbbox()
    if bbox:
        tile = tile.crop(bbox)
        side = max(tile.size)
        if side > 0:
            square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            square.alpha_composite(tile, dest=((side - tile.width) // 2, (side - tile.height) // 2))
            tile = square.resize((size_px, size_px), Image.LANCZOS)
    canvas_size = tile.width

    # 비디오 전체 캔버스에 합성 (중심이 x, y 비율 좌표)
    img = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    cx = float(overlay_data.get("x", 0.5)) * video_width
    cy = float(overlay_data.get("y", 0.5)) * video_height
    px = int(round(cx - canvas_size / 2))
    py = int(round(cy - canvas_size / 2))
    img.alpha_composite(tile, dest=(px, py))

    out_path = str(Path(tmp_dir) / f"graphic_emoji_{idx:03d}.png")
    img.save(out_path, "PNG")
    return out_path


def _render_graphic_overlay_png(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "str | None":
    """그래픽 오버레이 type별 PNG 렌더링 디스패처.

    #205 1차: spotlight/alert
    #216 2차: target/reaction/smell (정적).
    #217 후속: reaction.bounce/smell.loop는 별도 `_build_graphic_overlay_layer`에서
    동적 layer로 처리한다. 이 함수는 정적 PNG만 다룬다.
    #288: emoji 4종 — Apple Color Emoji (macOS) / Noto Color Emoji (Linux) 사용.
    """
    ov_type = (overlay_data.get("type") or "").lower()
    if ov_type == "spotlight":
        return _render_spotlight_png(overlay_data, video_width, video_height, tmp_dir, idx)
    if ov_type == "alert":
        return _render_alert_png(overlay_data, video_width, video_height, tmp_dir, idx)
    if ov_type == "target":
        return _render_target_png(overlay_data, video_width, video_height, tmp_dir, idx)
    if ov_type == "reaction":
        return _render_reaction_png(overlay_data, video_width, video_height, tmp_dir, idx)
    if ov_type == "smell":
        return _render_smell_png(overlay_data, video_width, video_height, tmp_dir, idx)
    if ov_type == "emoji":
        return _render_emoji_png(overlay_data, video_width, video_height, tmp_dir, idx)
    console.print(f"  [yellow]알 수 없는 그래픽 오버레이 type='{ov_type}' (스킵)[/yellow]")
    return None


def _build_graphic_overlay_layer(
    overlay_data: dict,
    video_width: int,
    video_height: int,
    tmp_dir: str,
    idx: int,
) -> "dict | None":
    """그래픽 오버레이 1건을 frame 합성용 layer dict로 변환한다.

    반환 dict는 두 형태 중 하나:
      - 정적 layer: {"kind": "static", "png_image": PIL.RGBA, "start_sec", "end_sec"}
      - 동적 layer: {"kind": "dynamic_reaction" | "dynamic_smell", ...}

    bounce/loop가 True가 아니면 항상 정적 layer로 반환된다.
    """
    ov_type = (overlay_data.get("type") or "").lower()
    params = overlay_data.get("params") or {}

    # bounce/loop이면 동적 layer 우선 시도
    if ov_type == "reaction" and bool(params.get("bounce", False)):
        layer = _build_reaction_dynamic_layer(overlay_data, video_width, video_height)
        if layer:
            layer["start_sec"] = float(overlay_data.get("start_sec", 0.0))
            layer["end_sec"] = overlay_data.get("end_sec")
            layer["overlay_data"] = overlay_data  # #287
            return layer
        # PIL 미설치 등 — 정적 폴백
    if ov_type == "smell" and bool(params.get("loop", False)):
        layer = _build_smell_dynamic_layer(overlay_data, video_width, video_height)
        if layer:
            layer["start_sec"] = float(overlay_data.get("start_sec", 0.0))
            layer["end_sec"] = overlay_data.get("end_sec")
            layer["overlay_data"] = overlay_data  # #287
            return layer
    # dash 는 항상 동적(흘러가는 게 본질) — 정적 PNG 경로가 없다.
    if ov_type == "dash":
        layer = _build_dash_dynamic_layer(overlay_data, video_width, video_height)
        if layer:
            layer["start_sec"] = float(overlay_data.get("start_sec", 0.0))
            layer["end_sec"] = overlay_data.get("end_sec")
            layer["overlay_data"] = overlay_data
            return layer
        return None
        # 정적 폴백

    # 정적 경로: 기존 dispatcher를 그대로 사용 (PNG → 풀-캔버스 RGBA)
    png_path = _render_graphic_overlay_png(overlay_data, video_width, video_height, tmp_dir, idx)
    if not png_path:
        return None
    try:
        from PIL import Image as _PILImage
        png_img = _PILImage.open(png_path).convert("RGBA")
    except Exception:
        return None
    return {
        "kind": "static",
        "png_image": png_img,
        "start_sec": float(overlay_data.get("start_sec", 0.0)),
        "end_sec": overlay_data.get("end_sec"),
        # #287 — 원본 데이터 전체 보관 (enter/exit 효과 계산용)
        "overlay_data": overlay_data,
    }


# ── Scene Filter (#206) ───────────────────────────────────────────────────
# 씬 전체 색감/무드 필터. ffmpeg vf chain 단일 인코딩으로 여러 필터를 누적 적용한다.
# 1차 구현 범위: flashback / horror / vivid / nightvision 4종. pov는 스킵 + 로그.

def _build_cartoon_core_vf(inten: float) -> str:
    """cartoon 필터의 코어 효과 체인(median + eq + edgedetect)만 반환한다.

    crop/pad 래핑은 apply_scene_filters 가 콘텐츠 영역을 감지한 뒤 담당한다.
    (레터박스 여백 경계·프레임 외곽에 edgedetect 엣지가 생기는 회귀를 막기 위함.)
    """
    inten = max(0.0, min(1.0, float(inten)))
    radius = 1 + int(round(inten * 2))          # 1 ~ 3
    sat = 1.4 + 0.5 * inten                       # 1.4 ~ 1.9
    contr = 1.1 + 0.15 * inten                    # 1.10 ~ 1.25
    gamma = 1.0 + 0.08 * inten                    # 1.00 ~ 1.08
    edge_low = 0.1
    edge_high = 0.4 - 0.15 * inten                # 강도 높을수록 엣지 민감(선 굵고 많이)
    return (
        f"median=radius={radius},"
        f"eq=saturation={sat:.4f}:contrast={contr:.4f}:gamma={gamma:.4f},"
        f"edgedetect=low={edge_low:.4f}:high={max(0.05, edge_high):.4f}:mode=colormix"
    )


def _detect_content_region(
    src_path: str, video_width: int, video_height: int
) -> tuple[int, int, int, int] | None:
    """cropdetect 로 src 영상의 (검은 레터박스를 제외한) 콘텐츠 사각형을 감지한다.

    반환: (w, h, x, y). 감지 실패 / 여백 없음(전체 프레임) / 이상 값이면 None.
    None 은 "여백 없음 = 전체 프레임 = crop 불필요"를 의미한다.

    cropdetect 는 검은 여백을 자동 감지하는 ffmpeg 필터로, fit=contain 세로 숏폼의
    위아래 레터박스(및 fit 에 따른 좌우 필러박스)를 정확히 잡아낸다.
    limit=24(0~255): 이 값 이하 밝기를 여백으로 간주. round=2 로 짝수 정렬(코덱 안전).
    """
    try:
        r = subprocess.run(
            [
                ffmpeg_bin(), "-hide_banner", "-i", src_path,
                "-vf", "cropdetect=limit=24:round=2:reset=0",
                "-frames:v", "12", "-f", "null", "-",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception:
        return None
    # stderr 의 마지막 "crop=W:H:X:Y" 를 사용 (누적 감지 결과).
    import re as _re
    matches = _re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr or "")
    if not matches:
        return None
    w, h, x, y = (int(v) for v in matches[-1])
    # 유효성: 양수 크기, 프레임 경계 이내.
    if w <= 0 or h <= 0:
        return None
    if x < 0 or y < 0 or x + w > video_width or y + h > video_height:
        return None

    # 축별 여백 스냅: cropdetect 가 콘텐츠 가장자리의 어두운 부분을 여백으로 오인해
    # 실제 콘텐츠를 잘라내는 것을 방지한다. 여백이 프레임 대비 3% 미만인 축은
    # 여백 없음으로 간주해 전체 축으로 되돌린다(레터박스는 보통 이보다 훨씬 큼).
    MIN_MARGIN_RATIO = 0.03
    if (video_width - w) < video_width * MIN_MARGIN_RATIO:
        w, x = video_width, 0
    if (video_height - h) < video_height * MIN_MARGIN_RATIO:
        h, y = video_height, 0

    # #cartoon-halfcrop 회귀 방지: cropdetect 는 "어두운 픽셀 = 여백"으로 가정하므로,
    # 콘텐츠 자체에 어두운 영역(검은 배경 그래프·야경·어두운 앱 화면)이 있으면 그 부분을
    # 레터박스로 오인해 콘텐츠 절반만 crop한다(예: 오른쪽이 검정 → crop=540:1920:0:0 →
    # pad 로 오른쪽 절반이 검정 출력). 밝기만으로는 "레터박스 검정"과 "콘텐츠 검정"을
    # 구분할 수 없다(둘 다 어둡고 균일). 결정적 차이는 *대칭성*이다: fit=contain 은
    # 콘텐츠를 중앙 정렬하므로 레터박스/필러박스는 항상 양쪽에 (거의) 대칭으로 생긴다.
    # 한쪽에만 여백이 잡히면(예: x=0 인데 우측만 잘림) 그것은 레터박스가 아니라
    # 콘텐츠의 어두운 가장자리를 오인한 것이므로, 해당 축을 전체로 되돌린다.
    # 허용 비대칭: 짝수 정렬(round=2)·홀수 여백으로 인한 최대 몇 px 오차만 감안.
    SYM_TOL = max(4, int(round(video_width * 0.02)))   # 좌우 여백 차 허용치
    SYM_TOL_V = max(4, int(round(video_height * 0.02)))  # 상하 여백 차 허용치

    # 좌우 여백 대칭성 검증.
    left_margin = x
    right_margin = video_width - (x + w)
    if left_margin > 0 or right_margin > 0:
        if abs(left_margin - right_margin) > SYM_TOL:
            # 비대칭 → 레터박스 아님(콘텐츠 어두운 가장자리 오인) → 폭 전체 복원
            w, x = video_width, 0

    # 상하 여백 대칭성 검증.
    top_margin = y
    bot_margin = video_height - (y + h)
    if top_margin > 0 or bot_margin > 0:
        if abs(top_margin - bot_margin) > SYM_TOL_V:
            # 비대칭 → 높이 전체 복원
            h, y = video_height, 0

    # 두 축 모두 여백이 없으면 crop 불필요 → None (fit=cover 무회귀).
    if w >= video_width and h >= video_height:
        return None
    return (w, h, x, y)


def _wrap_cartoon_vf_with_content_crop(
    core_vf: str,
    region: tuple[int, int, int, int] | None,
    video_width: int,
    video_height: int,
) -> str:
    """cartoon 코어 vf 를 콘텐츠 영역 crop→효과→pad 로 감싼다.

    region 이 None(여백 없음/감지 실패)이면 core_vf 를 그대로 반환(전체 프레임 적용).
    region 이 있으면:
      1) crop 으로 콘텐츠만 분리 → edgedetect 가 레터박스 여백을 못 본다.
      2) core_vf 적용.
      3) pad 로 원래 위치에 검은 여백을 복원 → 최종 프레임 크기/정렬 유지.
    """
    if not region:
        return core_vf
    w, h, x, y = region
    return (
        f"crop={w}:{h}:{x}:{y},"
        f"{core_vf},"
        f"pad={video_width}:{video_height}:{x}:{y}:color=black"
    )


def _build_filter_vf(filter_type: str, intensity: float) -> str | None:
    """필터 타입과 강도(0.0~1.0)를 받아 ffmpeg -vf 표현식 한 줄을 반환한다.

    여러 필터를 중첩할 때는 호출자가 콤마로 연결한다.
    pov는 None을 반환하여 호출자가 스킵하도록 한다.
    """
    # intensity 범위 클램프
    inten = max(0.0, min(1.0, float(intensity)))

    if filter_type == "flashback":
        # 회상: 채도 제거(흑백) + 입자 노이즈 + 빈티지 톤커브 + 시안/노란 톤 + 비네팅
        # #229: 필름 그레인 디테일 강화 — noise 1.5배, curves=vintage, colorbalance.
        noise_alls = int(round(inten * 30 * 1.5))
        rs = 0.05 * inten
        bs = -0.05 * inten
        return (
            f"hue=s=0,"
            f"noise=alls={noise_alls}:allf=t+u,"
            f"curves=preset=vintage,"
            f"colorbalance=rs={rs:.4f}:bs={bs:.4f},"
            f"vignette=PI/5"
        )
    if filter_type == "horror":
        # 공포: 채도 낮춤 + 어둡게 + 푸른빛 + 비네팅
        sat = 0.4 * (1.0 - inten * 0.5)
        bright = -0.05 * inten
        blue_shift = 0.15 * inten
        return (
            f"eq=saturation={sat:.4f}:brightness={bright:.4f},"
            f"colorbalance=bs={blue_shift:.4f},"
            f"vignette=PI/4"
        )
    if filter_type == "vivid":
        # 선명: 채도/밝기/명암 강조
        sat = 1.0 + 0.4 * inten
        bright = 0.05 * inten
        contr = 1.0 + 0.1 * inten
        return (
            f"eq=saturation={sat:.4f}:brightness={bright:.4f}:contrast={contr:.4f}"
        )
    if filter_type == "nightvision":
        # 야간투시경: 녹색 채널만 보존(R=0, B=0) + 노이즈 + 비네팅
        noise_alls = int(round(20 * inten))
        return (
            f"colorchannelmixer=0:0:0:0:0.4:0.6:0:0:0:0:0:0,"
            f"noise=alls={noise_alls}:allf=t+u,"
            f"vignette=PI/4"
        )
    if filter_type == "cartoon":
        # 만화/웹툰 필터 — 색 평탄화(median) + 채도/대비 강화(eq) + 검은 윤곽선(edgedetect).
        # 구현 전략(메인 Claude 프로토타입 검증):
        #   1) median 으로 미세 질감을 뭉개 "셀 셰이딩"처럼 평탄한 색면을 만든다.
        #   2) eq 로 채도/대비/감마를 끌어올려 웹툰 특유의 쨍한 색감을 얻는다.
        #   3) edgedetect(mode=colormix) 로 검은 윤곽선을 원본 위에 얹은 듯한 만화 라인을 만든다.
        # intensity 로 median 반경/채도/엣지 강도를 함께 조절한다.
        #   - 성능: median radius 는 렌더 시간에 민감하므로 1~3 범위로 제한(intensity 0→1 = radius 1→3).
        #   - 저조도 소스 색 죽음 방지: 채도를 항상 1.0 이상 유지(sat = 1.4 ~ 1.9).
        # edgedetect 는 -vf 체인 안에서 원본과 곱 블렌드가 어려우므로 mode=colormix 를 사용한다.
        # colormix 는 엣지를 원본 색 위에 어둡게 얹어 "검은 선 + 원본 채색" 만화 느낌을 낸다.
        #
        # #letterbox-edge 회귀: edgedetect 는 검은 레터박스 여백↔콘텐츠 경계와 프레임
        #   최외곽을 강한 엣지로 잡아 원치 않는 가로/세로 선을 그린다. 이 함수는 *코어
        #   효과 체인*만 반환하고, apply_scene_filters 가 콘텐츠 영역(cropdetect)을 감지해
        #   crop→(이 체인)→pad 로 감싸 여백 경계·외곽 엣지를 근본 차단한다.
        #   (fit=cover 로 여백이 없으면 crop 이 전체 프레임이 되어 무변화.)
        return _build_cartoon_core_vf(inten)
    if filter_type == "pov":
        # POV 마스크는 vf chain으로 처리 불가 (radial mask 필요). 호출자가 별도 PNG composite로 처리.
        return None
    if filter_type == "warm":
        # 따뜻한 분위기 — curves 로 R 채널은 미드톤을 끌어올리고 B 채널은 끌어내려 골든 톤을 만든다.
        # 채도 + 명도 살짝 + 부드러운 비네팅으로 황혼/카페/노을 무드.
        # colorbalance 의 shadows 시프트 방식은 회색 벽 같은 무채색 영역이 시안 캐스트가 도드라져 오히려
        # 차갑게 보이는 회귀가 있어, curves 의 *미드톤 시프트*(0.5 → 0.6/0.4) 패턴을 사용한다.
        # intensity 0 이면 변화 없음(보간), 1 이면 풍부한 골든 톤.
        # mid 점만 보간 — 0/0 과 1/1 끝점은 고정해 하이라이트/셰도 디테일 보존.
        r_mid = 0.5 + 0.10 * inten    # 0.50 → 0.60 (1.0 강도)
        b_mid = 0.5 - 0.10 * inten    # 0.50 → 0.40
        sat = 1.0 + 0.15 * inten
        bright = 0.04 * inten
        return (
            f"curves=red='0/0 0.5/{r_mid:.4f} 1/1':blue='0/0 0.5/{b_mid:.4f} 1/1',"
            f"eq=saturation={sat:.4f}:brightness={bright:.4f},"
            f"vignette=PI/6"
        )
    if filter_type == "cool":
        # 시원한 분위기 — warm 의 미러 이미지. B 채널 미드톤 상향 + R 채널 미드톤 하향 → 청록/블루 톤.
        # 채도 살짝 낮춰 서늘한 느낌 + 하이라이트 살짝 밝게 (얼음/유리 반사감).
        # 예: 여름 냉수매트/에어컨/얼음 씬에 어울림.
        b_mid = 0.5 + 0.10 * inten    # 0.50 → 0.60
        r_mid = 0.5 - 0.08 * inten    # 0.50 → 0.42 (푸른 캐스트가 살짝 덜 극단적)
        # green 채널을 미미하게 올려 시안 톤 부여 — 순 파랑보다 상쾌.
        g_mid = 0.5 + 0.03 * inten
        sat = 1.0 - 0.05 * inten      # 살짝 저채도
        bright = 0.03 * inten
        return (
            f"curves=red='0/0 0.5/{r_mid:.4f} 1/1':green='0/0 0.5/{g_mid:.4f} 1/1':blue='0/0 0.5/{b_mid:.4f} 1/1',"
            f"eq=saturation={sat:.4f}:brightness={bright:.4f},"
            f"vignette=PI/6"
        )
    return None


# ── POV 마스크 (#219) ──────────────────────────────────────────────────────
# POV(시점) 필터는 ffmpeg vf chain으로는 표현 불가. Pillow로 radial mask PNG를
# 생성한 뒤 그래픽 오버레이와 동일한 프레임별 alpha_composite 파이프로 합성한다.

def _interpolate_pov_at_time(
    keyframes: list,
    t: float,
    default_params: dict,
) -> dict:
    """#229: POV keyframes를 시간 t에서 linear 보간하여 정적 마스크 params dict를 반환한다.

    - 입력 keyframes가 비어있거나 1개 → 정적 폴백 (default_params 또는 단일 keyframe).
    - 두 인접 키프레임 사이는 alpha = (t - kf_prev.t) / (kf_next.t - kf_prev.t) 로 보간.
    - 보간 대상: center_x / center_y / radius. feather/darkness는 default_params에서 그대로.
    - keyframes의 t는 자동 정렬한다. t가 양 끝을 벗어나면 가장 가까운 keyframe으로 클램프.
    """
    base = dict(default_params or {})
    if not keyframes or not isinstance(keyframes, list):
        return base
    valid = [kf for kf in keyframes if isinstance(kf, dict) and "t" in kf]
    if not valid:
        return base
    valid_sorted = sorted(valid, key=lambda kf: float(kf.get("t", 0.0)))
    if len(valid_sorted) == 1:
        only = valid_sorted[0]
        for key in ("center_x", "center_y", "radius"):
            if key in only:
                base[key] = float(only[key])
        return base
    # 클램프 양 끝
    if t <= float(valid_sorted[0].get("t", 0.0)):
        first = valid_sorted[0]
        for key in ("center_x", "center_y", "radius"):
            if key in first:
                base[key] = float(first[key])
        return base
    if t >= float(valid_sorted[-1].get("t", 0.0)):
        last = valid_sorted[-1]
        for key in ("center_x", "center_y", "radius"):
            if key in last:
                base[key] = float(last[key])
        return base
    # 사이값 보간
    for i in range(len(valid_sorted) - 1):
        kf_prev = valid_sorted[i]
        kf_next = valid_sorted[i + 1]
        t_prev = float(kf_prev.get("t", 0.0))
        t_next = float(kf_next.get("t", 0.0))
        if t_prev <= t <= t_next and t_next > t_prev:
            alpha = (t - t_prev) / (t_next - t_prev)
            for key in ("center_x", "center_y", "radius"):
                v_prev = float(kf_prev.get(key, base.get(key, 0.5)))
                v_next = float(kf_next.get(key, base.get(key, 0.5)))
                base[key] = v_prev + (v_next - v_prev) * alpha
            return base
    return base


def _render_pov_mask_png(
    video_width: int,
    video_height: int,
    params: dict,
    intensity: float,
    tmp_dir: str,
) -> "str | None":
    """POV radial mask PNG 1장(정적)을 생성한다.

    동작: 전체 캔버스를 검정 알파로 채우고, 중앙 원형 영역만 알파 0(투명)으로
    뚫어 영상의 가운데만 보이고 가장자리는 어두워지도록 한다. 가장자리는
    GaussianBlur로 부드럽게 페더링한다.

    params:
        center_x (float, 0~1, 기본 0.5): 원의 중심 x (비율)
        center_y (float, 0~1, 기본 0.5): 원의 중심 y (비율)
        radius   (float, 0~1, 기본 0.35): 원의 반지름 (짧은 변 기준 비율)
        feather  (int, px, 기본 80): 마스크 엣지 가우시안 블러 반경
        darkness (float, 0~1, 기본 1.0): 바깥 영역의 어둠 강도 (alpha 배수)

    intensity는 darkness에 곱해져 최종 알파를 결정한다 (intensity * darkness).
    intensity=0이면 효과 없음, 1이면 params.darkness 그대로.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return None

    inten = max(0.0, min(1.0, float(intensity)))
    p = params or {}
    cx_ratio = float(p.get("center_x", 0.5))
    cy_ratio = float(p.get("center_y", 0.5))
    cx_ratio = max(0.0, min(1.0, cx_ratio))
    cy_ratio = max(0.0, min(1.0, cy_ratio))
    cx = cx_ratio * video_width
    cy = cy_ratio * video_height

    short_edge = min(video_width, video_height)
    radius_ratio = float(p.get("radius", 0.35))
    radius_ratio = max(0.05, min(0.95, radius_ratio))
    radius = radius_ratio * short_edge

    feather = int(p.get("feather", 80))
    feather = max(0, min(400, feather))

    darkness = float(p.get("darkness", 1.0))
    darkness = max(0.0, min(1.0, darkness))

    final_alpha = int(round(darkness * inten * 255))
    if final_alpha <= 0:
        # 효과 없음 → mask 생성하지 않고 None 반환 (호출자가 스킵)
        return None

    # 1) 검정 RGBA 캔버스 (알파는 마스크로 덮어씀)
    dark_layer = Image.new("RGBA", (video_width, video_height), (0, 0, 0, final_alpha))

    # 2) 마스크: 흰=알파 유지, 검정=알파 0. 중앙 원을 검정으로 그려 가운데를 투명화.
    mask = Image.new("L", (video_width, video_height), 255)
    mdraw = ImageDraw.Draw(mask)
    bbox = [cx - radius, cy - radius, cx + radius, cy + radius]
    mdraw.ellipse(bbox, fill=0)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather))

    # 마스크를 알파 채널로 적용 (검정 영역의 알파를 0으로 변환)
    dark_layer.putalpha(
        Image.eval(mask, lambda v, a=final_alpha: int(v * a / 255))
    )

    out_path = str(Path(tmp_dir) / "pov_mask.png")
    dark_layer.save(out_path, "PNG")
    return out_path


def _apply_pov_filters(
    src_path: str,
    dst_path: str,
    pov_filters: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """POV 필터(들)를 Pillow radial mask PNG + 프레임별 alpha_composite로 적용한다.

    여러 POV 필터가 리스트에 있으면 각각의 마스크를 순서대로 합성한다.
    실패/스킵 시 src를 dst로 복사하고 False 반환.
    #229: params.keyframes 지정 시 시간에 따라 center_x/y/radius를 linear 보간하여
    동적 마스크를 재생성한다. 키프레임 미지정 시 정적 마스크 1장 (회귀).
    """
    import shutil

    if not pov_filters:
        shutil.copy2(src_path, dst_path)
        return True

    # 마스크 재생성 캐시 간격(프레임). linear 보간이라 3프레임마다 갱신해도
    # 시각적 차이 거의 없음. fps=30 기준 ~10Hz 갱신.
    DYNAMIC_MASK_INTERVAL = 3

    tmp_dir = tempfile.mkdtemp(prefix="vag_pov_filter_")
    try:
        from PIL import Image as _PILImage

        # 각 POV 필터를 static/dynamic spec으로 정리.
        # static: {"kind":"static", "mask": PIL.RGBA}
        # dynamic: {"kind":"dynamic", "params": dict, "intensity": float, "keyframes": list, "cached_mask": PIL|None, "last_t": float|None}
        pov_specs: list = []
        for f in pov_filters:
            if not isinstance(f, dict):
                continue
            params = f.get("params") or {}
            intensity = float(f.get("intensity", 1.0) or 1.0)
            keyframes = params.get("keyframes") if isinstance(params, dict) else None
            is_dynamic = (
                isinstance(keyframes, list)
                and len([kf for kf in keyframes if isinstance(kf, dict) and "t" in kf]) >= 2
            )
            if is_dynamic:
                pov_specs.append({
                    "kind": "dynamic",
                    "params": params,
                    "intensity": intensity,
                    "keyframes": keyframes,
                    "cached_mask": None,
                    "last_frame": -DYNAMIC_MASK_INTERVAL,
                })
            else:
                mask_path = _render_pov_mask_png(
                    video_width, video_height, params, intensity, tmp_dir,
                )
                if mask_path is None:
                    continue
                try:
                    mask_img = _PILImage.open(mask_path).convert("RGBA")
                    pov_specs.append({"kind": "static", "mask": mask_img})
                except Exception:
                    continue

        if not pov_specs:
            shutil.copy2(src_path, dst_path)
            return True

        # fps 추출
        fps_r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        fps_str = fps_r.stdout.strip()
        fps = 30.0
        if "/" in fps_str:
            n, d = fps_str.split("/")
            fps = float(n) / max(float(d), 1)
        elif fps_str:
            try:
                fps = float(fps_str)
            except ValueError:
                pass

        audio_check = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        has_audio_stream = bool(audio_check.stdout.strip())

        pov_out = str(Path(tmp_dir) / "pov_out.mp4")
        audio_tmp = str(Path(tmp_dir) / "audio_tmp.aac")
        if has_audio_stream:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-i", src_path, "-vn", "-c:a", "copy", audio_tmp],
                capture_output=True,
            )

        dec = subprocess.Popen(
            [ffmpeg_bin(), "-i", src_path,
             "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        enc_cmd = [
            ffmpeg_bin(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{video_width}x{video_height}",
            "-r", str(fps),
            "-i", "pipe:0",
        ]
        if has_audio_stream and Path(audio_tmp).exists() and Path(audio_tmp).stat().st_size > 0:
            enc_cmd += ["-i", audio_tmp, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
        else:
            enc_cmd += ["-an"]
        enc_cmd += [*_video_encoder_args(60),
                    "-pix_fmt", "yuv420p", pov_out]
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        frame_size = video_width * video_height * 3
        frame_idx = 0
        try:
            while True:
                raw = dec.stdout.read(frame_size)
                if len(raw) < frame_size:
                    break
                base = _PILImage.frombytes(
                    "RGB", (video_width, video_height), raw
                ).convert("RGBA")
                t_sec = frame_idx / fps
                for spec in pov_specs:
                    if spec["kind"] == "static":
                        base = _PILImage.alpha_composite(base, spec["mask"])
                    else:
                        # dynamic: N프레임마다 마스크 재생성
                        if (
                            spec["cached_mask"] is None
                            or frame_idx - spec["last_frame"] >= DYNAMIC_MASK_INTERVAL
                        ):
                            interpolated = _interpolate_pov_at_time(
                                spec["keyframes"], t_sec, spec["params"],
                            )
                            mp = _render_pov_mask_png(
                                video_width, video_height,
                                interpolated, spec["intensity"], tmp_dir,
                            )
                            if mp is not None:
                                try:
                                    spec["cached_mask"] = _PILImage.open(mp).convert("RGBA")
                                    spec["last_frame"] = frame_idx
                                except Exception:
                                    pass
                        if spec["cached_mask"] is not None:
                            base = _PILImage.alpha_composite(base, spec["cached_mask"])
                enc.stdin.write(base.convert("RGB").tobytes())
                frame_idx += 1
        finally:
            dec.stdout.close()
            dec.wait()
            enc.stdin.close()
            enc.wait()

        if Path(pov_out).exists() and Path(pov_out).stat().st_size > 0:
            shutil.copy2(pov_out, dst_path)
            return True
        shutil.copy2(src_path, dst_path)
        return False

    except Exception as e:
        console.print(f"  [yellow]POV 마스크 필터 적용 실패 (건너뜀): {e}[/yellow]")
        shutil.copy2(src_path, dst_path)
        return False
    finally:
        import shutil as _sh
        _sh.rmtree(tmp_dir, ignore_errors=True)


# ── 필터 결과 캐싱 (#227) ──────────────────────────────────────────────
# 같은 (src 파일 + filters 조합)을 두 번 적용하면 두 번째는 캐시 hit으로 즉시 복사.
# 캐시 폴더: output/clips/filter_cache/
# 키 산정: filters(JSON sort_keys) + src 경로 + src mtime → sha256 → 앞 16자.

FILTER_CACHE_DIR = "output/clips/filter_cache"


def _filter_cache_should_skip(filters: list[dict]) -> bool:
    """캐시할 가치가 없는 필터 입력인지 판단.

    - 빈 리스트
    - 모든 항목이 dict이며 intensity가 0(또는 음수)인 경우
    """
    if not filters:
        return True
    has_effective = False
    for f in filters:
        if not isinstance(f, dict):
            continue
        try:
            inten = float(f.get("intensity", 1.0) or 0.0)
        except (TypeError, ValueError):
            inten = 0.0
        if inten > 0.0:
            has_effective = True
            break
    return not has_effective


def _compute_filter_cache_key(src_path: str, filters: list[dict]) -> str | None:
    """캐시 키 산정. src 파일이 존재하지 않으면 None."""
    import hashlib
    import json
    import os

    try:
        mtime = os.path.getmtime(src_path)
    except OSError:
        return None
    try:
        filters_json = json.dumps(filters, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    raw = filters_json.encode("utf-8") + src_path.encode("utf-8") + str(mtime).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def apply_scene_filters(
    src_path: str,
    dst_path: str,
    filters: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """씬 전체 필터(회상/공포/선명/야간투시경/POV)를 적용한다.

    구현: filter_type 별로 두 그룹으로 분리한다.
      - vf 그룹 (flashback/horror/vivid/nightvision): ffmpeg vf chain 단일 인코딩
      - mask 그룹 (POV): Pillow radial mask + 프레임별 alpha_composite

    적용 순서: vf 그룹 먼저, 그 결과물 위에 POV mask 합성. (POV는 가장자리 검정
    어둠을 얹는 효과이므로 색감/노이즈 처리 *후*에 합성하는 것이 자연스럽다.)
    호출자는 cut_clip 직후, 텍스트/도형/그래픽 오버레이 *이전*에 본 함수를 호출.

    성공/스킵 시 dst_path에 결과가 있으며 True 반환. 일부 실패 시에도 가능한
    한 부분 결과를 복사한다.

    #227 캐싱: (src 파일 + filters) 동일 조합이면 결과를 재사용한다. 효과 없는
    필터(빈 리스트 / intensity=0)는 캐시하지 않고 src를 그대로 복사한다.
    """
    import shutil

    if not filters:
        shutil.copy2(src_path, dst_path)
        return True

    # 캐시 가치 없는 케이스: src 그대로 복사
    skip_cache = _filter_cache_should_skip(filters)
    cache_key = None if skip_cache else _compute_filter_cache_key(src_path, filters)

    # 캐시 hit 검사
    if cache_key:
        cache_dir = Path(FILTER_CACHE_DIR)
        cache_path = cache_dir / f"{cache_key}.mp4"
        if cache_path.exists() and cache_path.stat().st_size > 0:
            try:
                shutil.copy2(str(cache_path), dst_path)
                console.print(f"  [cyan]필터 캐시 hit: {cache_key} → {dst_path}[/cyan]")
                return True
            except Exception as e:
                console.print(f"  [yellow]필터 캐시 복사 실패 ({e}), 재처리[/yellow]")
        else:
            console.print(f"  [dim]필터 캐시 miss: {cache_key}[/dim]")

    # #letterbox-edge: cartoon 필터가 있으면 콘텐츠 영역(레터박스 제외)을 미리 감지한다.
    # edgedetect 가 검은 여백 경계/프레임 외곽에 엣지를 그리는 회귀를 막기 위해,
    # cartoon 코어 효과만 crop→효과→pad 로 감싼다. (감지 실패/여백 없으면 무변화.)
    _cartoon_region = None
    _has_cartoon = any(
        isinstance(f, dict) and str(f.get("filter_type", "")).strip() == "cartoon"
        and float(f.get("intensity", 1.0) or 0.0) > 0.0
        for f in filters
    )
    if _has_cartoon:
        _cartoon_region = _detect_content_region(src_path, video_width, video_height)

    # 필터를 두 그룹으로 분리
    vf_parts: list[str] = []
    pov_filters: list[dict] = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        ftype = str(f.get("filter_type", "")).strip()
        intensity = float(f.get("intensity", 1.0) or 1.0)
        if ftype == "pov":
            pov_filters.append(f)
            continue
        vf = _build_filter_vf(ftype, intensity)
        if vf:
            if ftype == "cartoon":
                vf = _wrap_cartoon_vf_with_content_crop(
                    vf, _cartoon_region, video_width, video_height
                )
            vf_parts.append(vf)
        else:
            console.print(f"  [yellow]알 수 없거나 미지원 필터 type='{ftype}' (스킵)[/yellow]")

    # 두 그룹 모두 없으면 그대로 복사
    if not vf_parts and not pov_filters:
        shutil.copy2(src_path, dst_path)
        return True

    # 단계 1: vf 그룹 적용 (있으면 임시 파일로, 없으면 src 그대로 사용)
    stage1_path = src_path
    intermediate_path = None
    if vf_parts:
        vf_chain = ",".join(vf_parts)

        audio_check = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        has_audio_stream = bool(audio_check.stdout.strip())

        # POV 단계가 뒤따르면 임시 파일, 아니면 dst로 직접 출력
        if pov_filters:
            intermediate_path = str(
                Path(tempfile.mkdtemp(prefix="vag_scene_filter_vf_")) / "stage1.mp4"
            )
            stage1_out = intermediate_path
        else:
            stage1_out = dst_path

        cmd = [ffmpeg_bin(), "-y", "-i", src_path, "-vf", vf_chain]
        if has_audio_stream:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += [
            *_video_encoder_args(60),
            "-pix_fmt", "yuv420p", stage1_out,
        ]

        vf_ok = False
        try:
            r = subprocess.run(cmd, capture_output=True)
            if (
                r.returncode == 0
                and Path(stage1_out).exists()
                and Path(stage1_out).stat().st_size > 0
            ):
                vf_ok = True
            else:
                console.print(
                    f"  [yellow]씬 필터(vf chain) 적용 실패 (ffmpeg returncode={r.returncode}): "
                    f"{r.stderr.decode(errors='ignore')[:200]}[/yellow]"
                )
        except Exception as e:
            console.print(f"  [yellow]씬 필터(vf chain) 적용 예외 (건너뜀): {e}[/yellow]")

        if not vf_ok:
            # vf 단계 실패 시: POV가 있으면 src를 stage1으로 간주, 없으면 src→dst 복사 후 종료
            if pov_filters and intermediate_path:
                shutil.copy2(src_path, intermediate_path)
            else:
                shutil.copy2(src_path, dst_path)
                return False
        stage1_path = stage1_out

    # 단계 2: POV mask composite (있을 때만)
    pov_ok = True
    if pov_filters:
        pov_ok = _apply_pov_filters(
            stage1_path, dst_path, pov_filters, video_width, video_height,
        )

    # 임시 파일 정리
    if intermediate_path:
        try:
            tmp_parent = Path(intermediate_path).parent
            import shutil as _sh_clean
            _sh_clean.rmtree(tmp_parent, ignore_errors=True)
        except Exception:
            pass

    # 결과 검증
    if Path(dst_path).exists() and Path(dst_path).stat().st_size > 0:
        # 캐시 저장 (cache_key가 산정되었을 때만)
        if cache_key:
            try:
                cache_dir = Path(FILTER_CACHE_DIR)
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path = cache_dir / f"{cache_key}.mp4"
                shutil.copy2(dst_path, str(cache_path))
                console.print(f"  [dim]필터 캐시 저장: {cache_key}[/dim]")
            except Exception as e:
                console.print(f"  [yellow]필터 캐시 저장 실패 (무시): {e}[/yellow]")
        return True
    # 만일 dst가 비어있다면 src로 fallback
    shutil.copy2(src_path, dst_path)
    return pov_ok and bool(vf_parts or pov_filters)


# ── 씬 모션 (#207 / #234) — shake / clip_zoom / slowmo ─────────────────
# shake / clip_zoom: 영상 길이를 보존하는 모션 (vf chain 단일 인코딩으로 적용).
# slowmo (#234 1차): 씬 전체 구간을 덮는 경우에 한해 `setpts={1/speed}*PTS`로
#   영상 길이를 늘린다. 부분 구간 slowmo(split/trim/setpts/concat)는 후속 이슈로 분리.
# 영상 길이 정합성은 `_concat_with_dissolve_group_aware`가 `get_duration(clip_path)`
# 기반으로 자동 누적하므로 timeline.json / narration_timeline.json / final_output.sbv가
# 함께 갱신된다 (이슈 #234 본문 명시).


def _build_shake_vf(start_sec: float, duration_sec: float, params: dict) -> str | None:
    """shake motion → ffmpeg vf 표현식.

    구간 [start, start+duration) 안에서만 sin/cos 기반 crop 오프셋으로 화면을 흔든다.
    그 외 시각에는 흔들림이 0이 되도록 시간 게이트 `between(t,start,end)`를 곱한다.

    params:
      - intensity (float 0~1 또는 'light'/'medium'/'heavy'): 흔들림 진폭 비율.
        light=0.3, medium=0.6, heavy=1.0 매핑. 화면 폭/높이 대비 최대 4% 진폭.

    반환: vf 표현식 (단일 crop 필터). 효과가 없으면 None.
    """
    if duration_sec <= 0:
        return None
    intensity_raw = params.get("intensity", 0.6)
    if isinstance(intensity_raw, str):
        intensity_map = {"light": 0.3, "medium": 0.6, "heavy": 1.0}
        intensity = intensity_map.get(intensity_raw.lower().strip(), 0.6)
    else:
        try:
            intensity = float(intensity_raw)
        except (TypeError, ValueError):
            intensity = 0.6
    intensity = max(0.0, min(1.0, intensity))
    if intensity <= 0.0:
        return None

    # 화면 폭의 4%를 최대 진폭으로 사용. (input_width=iw, input_height=ih)
    # crop 폭은 iw - 2A, 높이는 ih - 2A. x/y 오프셋은 A 기준에 sin/cos 흔들림.
    # 시간 게이트 `between(t,s,e)`를 곱해 구간 밖에서는 정중앙 고정.
    s = float(start_sec)
    e = s + float(duration_sec)
    amp_ratio = 0.04 * intensity
    # NOTE: ffmpeg vf 표현식에서는 콤마(`,`)가 필터 구분자이므로 사용 불가.
    #       대신 산술식만 사용한다. iw/ih는 변수 그대로 접근 가능.
    # crop:
    #   w = iw*(1 - 2*amp_ratio)
    #   h = ih*(1 - 2*amp_ratio)
    #   x = iw*amp_ratio + iw*amp_ratio*sin(2*PI*15*t)*between(t,s,e)
    #   y = ih*amp_ratio + ih*amp_ratio*cos(2*PI*17*t)*between(t,s,e)
    w_expr = f"iw*(1-2*{amp_ratio})"
    h_expr = f"ih*(1-2*{amp_ratio})"
    x_expr = f"iw*{amp_ratio}+iw*{amp_ratio}*sin(2*PI*15*t)*between(t\\,{s}\\,{e})"
    y_expr = f"ih*{amp_ratio}+ih*{amp_ratio}*cos(2*PI*17*t)*between(t\\,{s}\\,{e})"
    return f"crop=w='{w_expr}':h='{h_expr}':x='{x_expr}':y='{y_expr}'"


def _build_clip_zoom_vf(
    start_sec: float,
    duration_sec: float,
    params: dict,
    video_width: int | None = None,
    video_height: int | None = None,
    fps: float | None = None,
) -> str | None:
    """clip_zoom motion → ffmpeg vf 표현식 (zoompan 기반).

    구간 [start, start+duration) 동안 start_scale→end_scale로 선형 보간하며 줌.
    target_x/target_y(0~1)는 **확대 후 보이는 영역의 중심점** (원본 영상의 정규화 좌표).
    예) target_x=0.8 → 줌인된 화면의 정중앙에 원본의 가로 80% 지점이 위치.

    구현: ffmpeg ``zoompan`` 필터 사용.

    구현 이력 (#버그수정 — target 무시 + 종료 후 복귀 두 가지 문제):
      이전 구현은 ``scale=eval=frame`` + ``crop`` 조합이었는데, scale eval=frame 의
      출력 stream 메타데이터가 첫 프레임 기준으로 고정되어 (ffmpeg 8.x), 매 프레임 다른
      크기의 scale 결과를 다음 crop 필터가 받지 못하고 좌상단으로 강제 cropping 되는
      문제가 있었다. 그 결과 target_x/y 가 무시되고 항상 좌상단(=시각적 중앙)으로
      줌되는 버그가 발생.

      해결: ffmpeg 표준 ``zoompan`` 필터로 교체. zoompan 은 한 필터 내부에서
      zoom/x/y 표현식을 모두 매 프레임 평가하고 출력 해상도(s)·fps 를 명시적으로
      받으므로 동적 줌이 안정적으로 동작한다.

    Z(t) (구간 종료 후 z1 유지 — 카메라가 줌인 상태로 머무름):
      - t < s        → z0
      - s ≤ t ≤ e   → z0 + (z1-z0)*(t-s)/dur  (선형 보간)
      - t > e        → z1  (종료 후 줌 유지 — 사용자 보고 반영)

    params:
      - target_x: float 0~1 (기본 0.5) — 확대 영역 중심의 가로 좌표 (원본 기준)
      - target_y: float 0~1 (기본 0.5) — 확대 영역 중심의 세로 좌표 (원본 기준)
      - start_scale: float (기본 1.0)
      - end_scale: float (기본 1.3)

    video_width/video_height/fps:
      zoompan 출력 해상도(s)·fps 를 강제하기 위해 필요. 미지정 시 hd720@25fps
      기본값으로 폴백(zoompan 자체 default). 호출자(`apply_scene_motions`)가
      ffprobe 로 얻어 전달하는 것을 권장.
    """
    if duration_sec <= 0:
        return None
    s = float(start_sec)
    e = s + float(duration_sec)
    try:
        tx = float(params.get("target_x", 0.5))
        ty = float(params.get("target_y", 0.5))
        z0 = float(params.get("start_scale", 1.0))
        z1 = float(params.get("end_scale", 1.3))
    except (TypeError, ValueError):
        tx, ty, z0, z1 = 0.5, 0.5, 1.0, 1.3
    tx = max(0.0, min(1.0, tx))
    ty = max(0.0, min(1.0, ty))
    z0 = max(0.1, min(10.0, z0))
    z1 = max(0.1, min(10.0, z1))

    # 효과 없음 (시작=끝=1.0)이면 스킵
    if abs(z0 - 1.0) < 1e-6 and abs(z1 - 1.0) < 1e-6:
        return None

    # zoompan 의 변수:
    #   in_time : 현재 입력 프레임의 시간(초) — t 와 동일 의미.
    #   iw/ih   : 입력 영상의 폭/높이 (원본 좌표계).
    #   zoom    : 현재 프레임의 z 값 (x/y 표현식 안에서 참조 가능).
    # Z(in_time): 구간 전 z0, 구간 중 보간, 구간 후 z1 유지.
    #   short-circuit if 로 NAN-safe (init 시점 in_time=NAN 보호).
    dur = max(float(duration_sec), 1e-6)
    z_expr = (
        f"if(between(in_time\\,{s}\\,{e})\\,{z0}+({z1}-{z0})*(in_time-{s})/{dur}\\,"
        f"if(gte(in_time\\,{e})\\,{z1}\\,{z0}))"
    )

    # 보이는 영역의 좌상단 (zoompan 좌표계 = 입력 좌표계):
    #   영역 폭 = iw/zoom, 중심 = iw*target_x → 좌상단 = iw*target_x - (iw/zoom)/2
    # 클램프는 zoompan 이 내부적으로 처리하지만 안전을 위해 max/min 적용.
    x_expr = f"max(0\\,min(iw-iw/zoom\\,iw*{tx}-(iw/zoom)/2))"
    y_expr = f"max(0\\,min(ih-ih/zoom\\,ih*{ty}-(ih/zoom)/2))"

    # 출력 해상도/fps 결정.
    # video_width/height 가 짝수가 아니면 yuv420p 인코딩 실패하므로 짝수로 보정.
    if video_width and video_height:
        w_even = int(video_width) - (int(video_width) % 2)
        h_even = int(video_height) - (int(video_height) % 2)
        s_opt = f":s={w_even}x{h_even}"
    else:
        s_opt = ""  # zoompan default hd720
    if fps and fps > 0:
        fps_opt = f":fps={fps:.6g}"
    else:
        fps_opt = ""  # zoompan default 25
    # d=1 — 입력 1 프레임당 출력 1 프레임. 입력/출력 fps 일치 시 길이 보존.
    return (
        f"zoompan=z='{z_expr}'"
        f":x='{x_expr}':y='{y_expr}'"
        f":d=1{s_opt}{fps_opt}"
    )


def _build_stutter_vf(start_sec: float, duration_sec: float, params: dict) -> str | None:
    """stutter(끊김/점프) motion → ffmpeg vf 표현식 (#347).

    이동 촬영 footage 에 "뚝뚝 끊기며 순간이동" 하는 저(低)프레임 모션을 만든다.
    프레임을 홀드 → 점프시켜 끊기는 느낌을 내며, **영상 전체 길이는 보존**한다
    (shake / clip_zoom 과 동일하게 길이 변화 없음).

    구현 원리
    --------
    ffmpeg ``fps`` 필터는 입력 영상을 목표 프레임레이트로 *재타이밍*하되 전체 길이는
    그대로 유지한다(프레임을 버리거나 복제). 목표 fps 를 낮게(예: 10fps) 잡으면 같은
    프레임이 여러 출력 프레임 동안 홀드되었다가 다음 프레임으로 점프 — 정확히 스터터
    효과가 된다.

    1. ``fps=fps=N`` — 입력을 N fps 로 다운샘플(프레임 홀드). N 이 낮을수록 끊김이 강함.
    2. ``fps=fps=30`` — 다시 30fps 스트림으로 복원(concat/인코딩 정합성). 이미 홀드된
       프레임이 그대로 복제되므로 "샤프하게 홀드된 점프" 가 유지된다(모션블러 없음).

    intensity → 목표 fps 매핑 (강할수록 fps 낮음 = 끊김 강함):
      - 0.0 → 효과 없음(None)
      - 0.3(기본) → 약 10fps  (은은한 영화적 느낌, 초당 8~12프레임)
      - 1.0 → 약 4fps   (글리치풍 강한 끊김)
    선형 보간: target_fps = 12 - intensity * 8  → [0.0→12, 0.3→9.6, 1.0→4].

    params:
      - intensity (float 0~1 또는 'light'/'medium'/'heavy'): 끊김 강도.
        light=0.3, medium=0.6, heavy=1.0 매핑.

    반환: vf 표현식 ("fps=...,fps=..."). 효과가 없으면 None.

    주의: ``fps`` 필터는 시간 게이트(between)로 부분 구간만 적용할 수 없으므로 본 효과는
    클립 전체에 적용된다(이동 footage 전체에 거는 것이 일반적 용법). start_sec/duration_sec
    은 유효성(>0) 판별에만 사용한다.
    """
    if duration_sec <= 0:
        return None
    intensity_raw = params.get("intensity", 0.3)
    if isinstance(intensity_raw, str):
        intensity_map = {"light": 0.3, "medium": 0.6, "heavy": 1.0}
        intensity = intensity_map.get(intensity_raw.lower().strip(), 0.3)
    else:
        try:
            intensity = float(intensity_raw)
        except (TypeError, ValueError):
            intensity = 0.3
    intensity = max(0.0, min(1.0, intensity))
    if intensity <= 0.0:
        return None

    # 목표 fps: 강도 0→12fps(거의 원활) ~ 1→4fps(강한 글리치). 기본 0.3 ≈ 9.6fps.
    target_fps = 12.0 - intensity * 8.0
    target_fps = max(2.0, min(15.0, target_fps))
    # 다운샘플(프레임 홀드) 후 30fps 로 복원해 후속 concat/인코딩 정합성 유지.
    return f"fps=fps={target_fps:.4f},fps=fps=30"


def _normalize_slowmo_params(params: dict) -> tuple[float, bool, float] | None:
    """slowmo params 정규화 → (speed, interpolate, freeze_at_end_sec) 또는 None.

    speed가 1.0(효과 없음)인 경우 None을 반환한다.
    """
    try:
        speed = float(params.get("speed", 0.5))
    except (TypeError, ValueError):
        speed = 0.5
    speed = max(0.25, min(1.0, speed))
    if abs(speed - 1.0) < 1e-6:
        return None
    interpolate = bool(params.get("interpolate", False))
    try:
        freeze_at_end_sec = float(params.get("freeze_at_end_sec", 0.0) or 0.0)
    except (TypeError, ValueError):
        freeze_at_end_sec = 0.0
    freeze_at_end_sec = max(0.0, min(5.0, freeze_at_end_sec))
    return (speed, interpolate, freeze_at_end_sec)


def _build_slowmo_vf(
    start_sec: float,
    duration_sec: float,
    params: dict,
    clip_duration: float,
) -> str | None:
    """slowmo motion → ffmpeg vf 표현식 (#234 1차, 씬 전체 구간 한정).

    **반환 조건**: motion이 씬 전체 구간을 덮는 경우만 vf 표현식을 반환한다.
      - 판별: start_sec ≤ 0.01 AND start_sec + duration_sec ≥ clip_duration - 0.01
      - 부분 구간 slowmo는 vf 표현식으로 표현 불가 → None 반환 (호출자가
        `apply_scene_motions`에서 filter_complex 분기로 우회한다, #236).

    효과:
      - `setpts={1/speed}*PTS` — 비디오 PTS 재매핑으로 길이를 1/speed배로 늘린다.
      - `interpolate=true`이면 vf 체인 뒤에 `minterpolate=fps=60:mi_mode=mci`를 추가
        하여 프레임 보간으로 부드러움을 향상.

    params:
      - speed (float 0.25~1.0, 기본 0.5): 1.0이면 효과 없음(None 반환).
      - interpolate (bool, 기본 false): minterpolate 추가 여부.
      - freeze_at_end_sec: 씬 전체 구간 경로에서는 미적용 (의미상 동결 구간이
        씬 끝에 추가되면 영상 길이가 더 늘어나므로 filter_complex 경로에서만
        처리. 씬 전체 경로에서는 무시).

    오디오 처리: 본 1차 구현은 비디오 PTS만 변경한다 (오디오는 원본 길이 유지).
    Phase 6에서 narration 오디오가 별도로 합성되므로 영상 트랙 길이만 늘리는 것으로
    충분하다 (이슈 본문 명시 — narration_timeline은 `get_duration(clip_path)` 기반).

    반환: vf 표현식. 효과가 없거나 부분 구간이라 vf로 표현 불가하면 None.
    """
    if duration_sec <= 0 or clip_duration <= 0:
        return None
    norm = _normalize_slowmo_params(params)
    if norm is None:
        return None
    speed, interpolate, _freeze = norm

    # 씬 전체 덮음 여부 판별. 부분 구간이면 None을 반환하여
    # 호출자(`apply_scene_motions`)가 filter_complex 경로로 분기하도록 한다.
    covers_full = (
        float(start_sec) <= 0.01
        and (float(start_sec) + float(duration_sec)) >= (float(clip_duration) - 0.01)
    )
    if not covers_full:
        return None

    # setpts PTS 재매핑: 1/speed 배로 늘림.
    # 예: speed=0.5 → setpts=2.0*PTS → 길이 2배.
    pts_mult = 1.0 / speed
    vf = f"setpts={pts_mult}*PTS"

    if interpolate:
        # minterpolate는 PTS 재매핑 후 적용해야 새 타임라인 기준으로 프레임을 보간한다.
        vf = f"{vf},minterpolate=fps=60:mi_mode=mci"
    return vf


def _build_motion_filter_complex(
    base_vf_chain: str,
    slowmo_segments: list[tuple[float, float, float, bool, float]],
    clip_duration: float,
) -> str:
    """부분 구간 slowmo + 길이 보존 vf(shake/clip_zoom) → filter_complex 표현식 (#236).

    전략:
      1. 입력 영상 `[0:v]`에 (있다면) shake/clip_zoom vf chain을 전체 적용.
      2. 결과를 시간순으로 split → trim/setpts/concat 으로 부분 구간 slowmo 적용.

    slowmo_segments: 각 원소는 (start_sec, end_sec, pts_mult, interpolate, freeze_at_end_sec).
      - start_sec/end_sec는 원본 영상 기준의 부분 구간.
      - pts_mult = 1/speed (>1.0).
      - freeze_at_end_sec > 0이면 해당 slowmo segment 뒤에
        `tpad=stop_mode=clone:stop_duration={N}` 로 마지막 프레임을 N초 더 동결.

    반환: filter_complex 문자열 — 출력 라벨은 `[out]`.

    구현 메모:
      - 다중 부분 구간을 시간순으로 정렬한 뒤 사이 구간(slowmo가 없는 일반 구간)을
        섞어 segments 리스트를 만든다.
      - 각 segment는 trim + setpts=PTS-STARTPTS 로 잘라낸 뒤,
        slowmo인 경우 setpts={pts_mult}*PTS와 (옵션) freeze(tpad)를 추가한다.
      - 모든 segment를 concat=v=1:a=0 로 이어붙인다.
      - clip_duration이 정확하지 않으면 마지막 일반 구간은 trim end 생략(자동으로 끝까지).
    """
    # 정렬 + 클램핑
    segs = sorted(
        (
            (max(0.0, s), max(s, min(e, clip_duration if clip_duration > 0 else e)), pts, interp, freeze)
            for (s, e, pts, interp, freeze) in slowmo_segments
            if e > s
        ),
        key=lambda x: x[0],
    )
    if not segs:
        # slowmo segment가 비어있으면 호출 측이 잘못 진입한 것. 항등 chain만 반환.
        prefix = f"[0:v]{base_vf_chain}[out]" if base_vf_chain else "[0:v]copy[out]"
        return prefix

    # 1단계: base vf chain(있으면) 적용. 출력 라벨은 [vbase].
    if base_vf_chain:
        head = f"[0:v]{base_vf_chain}[vbase]"
        src_label = "vbase"
    else:
        head = ""
        src_label = "0:v"

    # 2단계: 시간순 segments 구성 — 일반 / slowmo 교차.
    timeline_segments: list[tuple[float, float, bool, float, bool, float]] = []
    # 각 원소: (s, e, is_slowmo, pts_mult, interpolate, freeze_at_end_sec)
    cursor = 0.0
    for (s, e, pts, interp, freeze) in segs:
        if s > cursor + 1e-3:
            timeline_segments.append((cursor, s, False, 1.0, False, 0.0))
        timeline_segments.append((s, e, True, pts, interp, freeze))
        cursor = e
    # 마지막 일반 구간 (e ~ clip_duration)
    if clip_duration > 0 and cursor < clip_duration - 1e-3:
        timeline_segments.append((cursor, clip_duration, False, 1.0, False, 0.0))
    elif clip_duration <= 0 and cursor > 0:
        # clip_duration 미상 → 마지막 일반 구간을 "끝까지"로 추가 (end 생략)
        # 실제로는 ffmpeg trim에서 end가 없으면 끝까지이므로 e=0으로 마커.
        timeline_segments.append((cursor, -1.0, False, 1.0, False, 0.0))

    # split=N — N = len(timeline_segments). 라벨 [s0][s1]...[sN-1].
    n = len(timeline_segments)
    split_labels = "".join(f"[s{i}]" for i in range(n))
    split_stmt = f"[{src_label}]split={n}{split_labels}"

    # 각 segment chain.
    segment_stmts: list[str] = []
    out_labels: list[str] = []
    for i, (s, e, is_slowmo, pts_mult, interp, freeze) in enumerate(timeline_segments):
        in_label = f"s{i}"
        out_label = f"seg{i}"
        out_labels.append(out_label)
        # trim
        if e > 0:
            trim_filter = f"trim={s}:{e}"
        else:
            # 끝까지 — end 생략
            trim_filter = f"trim=start={s}"
        chain_parts = [trim_filter, "setpts=PTS-STARTPTS"]
        if is_slowmo:
            chain_parts.append(f"setpts={pts_mult}*PTS")
            if interp:
                chain_parts.append("minterpolate=fps=60:mi_mode=mci")
            if freeze > 0.0:
                chain_parts.append(f"tpad=stop_mode=clone:stop_duration={freeze}")
        segment_stmts.append(f"[{in_label}]{','.join(chain_parts)}[{out_label}]")

    # concat
    concat_inputs = "".join(f"[{lbl}]" for lbl in out_labels)
    concat_stmt = f"{concat_inputs}concat=n={n}:v=1:a=0[out]"

    stmts = [s for s in [head, split_stmt] if s] + segment_stmts + [concat_stmt]
    return ";".join(stmts)


def apply_scene_motions(
    src_path: str,
    dst_path: str,
    motions: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """씬 모션(shake / clip_zoom / stutter / slowmo)을 적용한다.

    구현:
      - shake / clip_zoom / stutter: 영상 길이를 보존하는 vf 표현식 (단일 인코딩 chain 합류).
      - slowmo:
        - **씬 전체 구간** (#234 1차): `setpts={1/speed}*PTS`를 vf chain 마지막에 합류.
        - **부분 구간** (#236): split/trim/setpts/concat 조합의 filter_complex 분기로
          전환. shake/clip_zoom vf는 split 앞에 전체 영상에 한 번 적용한 뒤
          split→부분 구간만 setpts→concat 한다.
        - **freeze_at_end_sec** (#236): 부분 구간 slowmo segment 끝에 `tpad`로
          마지막 프레임을 N초 동결 (filter_complex 경로 전용).

    호출 순서: cut_clip → filters → motions → text/shape/graphic overlays.
    성공 시 dst_path에 결과가 있으며 True 반환.

    빈 motions 또는 vf 토큰을 만들 수 없는 경우 src를 dst로 복사한다.
    """
    import shutil

    if not motions:
        shutil.copy2(src_path, dst_path)
        return True

    # slowmo 처리를 위해 입력 영상의 실측 duration이 필요하다.
    # ffprobe 실패 시 0.0 — 씬 전체 덮음 판별이 실패하므로 부분 구간으로 우회.
    clip_duration = 0.0
    try:
        clip_duration = float(get_duration(src_path))
    except Exception:
        clip_duration = 0.0

    # clip_zoom(zoompan) 의 출력 해상도/fps 강제용. zoompan 은 s/fps default 가
    # hd720@25 이므로 명시 안 하면 해상도가 변경되어 후속 필터/인코더와 불일치.
    src_fps = 0.0
    try:
        fps_r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        fps_str = fps_r.stdout.strip()
        if "/" in fps_str:
            n, d = fps_str.split("/")
            src_fps = float(n) / max(float(d), 1)
        elif fps_str:
            src_fps = float(fps_str)
    except Exception:
        src_fps = 0.0

    vf_tokens: list[str] = []  # 길이 보존 모션(shake/clip_zoom) — vf chain 앞쪽
    slowmo_full_tokens: list[str] = []  # 씬 전체 구간 slowmo — vf chain 마지막에 합류
    slowmo_partial: list[tuple[float, float, float, bool, float]] = []
    # 각 원소: (start_sec, end_sec, pts_mult, interpolate, freeze_at_end_sec)
    for m in motions:
        if not isinstance(m, dict):
            continue
        mtype = str(m.get("motion_type", "")).strip()
        start_sec = float(m.get("start_sec", 0.0) or 0.0)
        duration_sec = float(m.get("duration_sec", 1.0) or 0.0)
        params = m.get("params") if isinstance(m.get("params"), dict) else {}
        if mtype == "shake":
            vf = _build_shake_vf(start_sec, duration_sec, params)
            if vf:
                vf_tokens.append(vf)
        elif mtype == "clip_zoom":
            vf = _build_clip_zoom_vf(
                start_sec, duration_sec, params,
                video_width=video_width, video_height=video_height,
                fps=src_fps if src_fps > 0 else None,
            )
            if vf:
                # #zoompan-speed — zoompan 앞에 fps 필터를 반드시 둔다.
                #
                # footage_speed 는 setpts 로 적용되는데, setpts 는 프레임을 새로
                # 만들지 않고 표시 시각만 늘린다(6.93s/208프레임 → 12.63s/208프레임).
                # 그 상태로 zoompan(d=1:fps=N) 을 태우면 zoompan 이 208 프레임을
                # 다시 N fps 로 재타이밍해 길이가 원본으로 되돌아간다
                # (실측: 0.55배 슬로모션 씬이 12.66s → 6.97s 로 복귀).
                #
                # fps 필터를 먼저 태우면 늘어난 시간만큼 프레임이 실제로 복제되어
                # (208 → 380) zoompan 이 길이를 보존한다.
                if src_fps > 0:
                    vf_tokens.append(f"fps={src_fps:.6g}")
                vf_tokens.append(vf)
        elif mtype == "stutter":
            # 끊김/점프 모션 — 길이 보존 vf (shake/clip_zoom 과 동일 경로). #347
            vf = _build_stutter_vf(start_sec, duration_sec, params)
            if vf:
                vf_tokens.append(vf)
        elif mtype == "slowmo":
            # 씬 전체 구간이면 vf 표현식, 부분 구간이면 None 반환.
            vf = _build_slowmo_vf(start_sec, duration_sec, params, clip_duration)
            if vf:
                slowmo_full_tokens.append(vf)
                continue
            # vf가 None이면 (1) speed=1.0 무효, (2) 부분 구간 → filter_complex로 처리.
            norm = _normalize_slowmo_params(params)
            if norm is None:
                continue  # speed=1.0 등 무효
            if duration_sec <= 0:
                continue
            speed, interpolate, freeze = norm
            s = max(0.0, float(start_sec))
            if clip_duration > 0:
                e = min(s + float(duration_sec), float(clip_duration))
            else:
                e = s + float(duration_sec)
            if e <= s:
                continue
            slowmo_partial.append((s, e, 1.0 / speed, interpolate, freeze))
        else:
            console.print(f"  [yellow]알 수 없는 motion_type='{mtype}' (스킵)[/yellow]")
            continue

    if not vf_tokens and not slowmo_full_tokens and not slowmo_partial:
        # 효과 토큰이 없으면 src 그대로 (의도된 스킵 — 실패 아님).
        shutil.copy2(src_path, dst_path)
        return True

    # shake/stutter 의 crop 은 출력 해상도를 줄인다(예: 1920x1080 → 1828x1028).
    # 해상도가 다른 클립이 섞이면 xfade 연결이 실패해 단순 concat 으로 폴백된다
    # (실측 2026-11-05). 모션 뒤에 출력 해상도로 되돌린다 — 이미 같으면 무해.
    if vf_tokens:
        vf_tokens.append(f"scale={video_width}:{video_height},setsar=1")

    audio_check = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    has_audio_stream = bool(audio_check.stdout.strip())

    # 부분 구간 slowmo가 있으면 filter_complex 분기.
    # (전체 구간 slowmo와 동시에 들어오면 의미가 모호하므로 전체 우선 — vf chain 사용)
    use_filter_complex = bool(slowmo_partial) and not slowmo_full_tokens

    if use_filter_complex:
        # shake/clip_zoom vf chain을 split 앞 base로 적용.
        base_chain = ",".join(vf_tokens)
        fc = _build_motion_filter_complex(base_chain, slowmo_partial, clip_duration)
        cmd = [
            ffmpeg_bin(), "-y", "-i", src_path,
            "-filter_complex", fc,
            "-map", "[out]",
        ]
        # 부분 구간 slowmo는 비디오 길이가 늘어나므로 오디오 트랙은 단순화를 위해 제거.
        cmd += ["-an"]
        cmd += [
            *_video_encoder_args(60),
            "-pix_fmt", "yuv420p", dst_path,
        ]
    else:
        # vf chain 경로 (#234 1차와 동일): shake/clip_zoom 먼저, slowmo(setpts) 마지막.
        vf_chain = ",".join(vf_tokens + slowmo_full_tokens)
        cmd = [ffmpeg_bin(), "-y", "-i", src_path, "-vf", vf_chain]
        # slowmo가 있으면 비디오 길이가 늘어나는데 오디오는 그대로 두면 비디오 종료 후
        # 무음 영상이 되므로 ffmpeg가 처리 시 오디오 종료 시점 미스매치를 일으킬 수 있다.
        # 단순화를 위해 slowmo 적용 시 오디오 트랙을 제거한다.
        if has_audio_stream and not slowmo_full_tokens:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += [
            *_video_encoder_args(60),
            "-pix_fmt", "yuv420p", dst_path,
        ]

    try:
        r = subprocess.run(cmd, capture_output=True)
        if (
            r.returncode == 0
            and Path(dst_path).exists()
            and Path(dst_path).stat().st_size > 0
        ):
            return True
        console.print(
            f"  [yellow]씬 모션 적용 실패 (ffmpeg returncode={r.returncode}): "
            f"{r.stderr.decode(errors='ignore')[:200]}[/yellow]"
        )
    except Exception as e:
        console.print(f"  [yellow]씬 모션 적용 예외 (건너뜀): {e}[/yellow]")

    # 실패 시 src로 fallback
    shutil.copy2(src_path, dst_path)
    return False


# ── Mosaic Overlay (#264) ──────────────────────────────────────────────────


def _normalize_mosaic_overlay(
    m: dict,
    video_width: int,
    video_height: int,
) -> "dict | None":
    """MosaicOverlay 입력 dict를 ffmpeg filter_complex용 픽셀 단위 dict로 정규화한다.

    반환: {"x": int, "y": int, "w": int, "h": int, "mode": "pixelate"|"blur",
            "pixel_size": int, "blur_radius": int,
            "start_sec": float, "end_sec": float|None, "shape": "rect"|"ellipse"}
    유효하지 않은 경우(영역이 0 등) None.
    """
    if not isinstance(m, dict):
        return None
    shape = str(m.get("shape", "rect"))
    if shape not in ("rect", "ellipse"):
        shape = "rect"
    mode = str(m.get("mode", "pixelate"))
    if mode not in ("pixelate", "blur"):
        mode = "pixelate"
    try:
        x_ratio = float(m.get("x", 0.0))
        y_ratio = float(m.get("y", 0.0))
        w_ratio = float(m.get("width", 0.0))
        h_ratio = float(m.get("height", 0.0))
    except (TypeError, ValueError):
        return None

    # 0~1 비율 → 픽셀 좌표. crop 결과 size는 libx264 호환을 위해 짝수로 보정.
    x = int(round(x_ratio * video_width))
    y = int(round(y_ratio * video_height))
    w = int(round(w_ratio * video_width))
    h = int(round(h_ratio * video_height))
    # 영역을 영상 안쪽으로 클램프
    x = max(0, min(video_width - 2, x))
    y = max(0, min(video_height - 2, y))
    w = max(2, min(video_width - x, w))
    h = max(2, min(video_height - y, h))
    # 짝수화
    if w % 2 == 1:
        w -= 1
    if h % 2 == 1:
        h -= 1
    if w < 2 or h < 2:
        return None
    if x + w > video_width:
        w = video_width - x
        if w % 2 == 1:
            w -= 1
    if y + h > video_height:
        h = video_height - y
        if h % 2 == 1:
            h -= 1
    if w < 2 or h < 2:
        return None

    try:
        pixel_size = int(m.get("pixel_size", 20))
    except (TypeError, ValueError):
        pixel_size = 20
    pixel_size = max(2, min(min(w, h), pixel_size))

    try:
        blur_radius = int(m.get("blur_radius", 20))
    except (TypeError, ValueError):
        blur_radius = 20
    # boxblur radius는 너무 크면 ffmpeg가 거부하므로 영역 크기 절반으로 상한.
    blur_radius = max(1, min(min(w, h) // 2, blur_radius))

    start_sec = float(m.get("start_sec", 0.0) or 0.0)
    end_sec_raw = m.get("end_sec")
    end_sec = None if end_sec_raw is None else float(end_sec_raw)

    return {
        "shape": shape,
        "mode": mode,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "pixel_size": pixel_size,
        "blur_radius": blur_radius,
        "start_sec": start_sec,
        "end_sec": end_sec,
    }


def _build_mosaic_filter_complex(mosaics: list[dict]) -> str:
    """정규화된 mosaic 리스트를 받아 ffmpeg `-filter_complex` 문자열을 생성한다.

    동작 개요:
      [0:v]                                             ← 입력 비디오
        split=N+1 [base][src1][src2]...[srcN]            ← 베이스 + 모자이크 영역별 분기

      각 모자이크 i:
        pixelate: [srci] crop=W:H:X:Y, scale=Ws:Hs, scale=W:H:flags=neighbor [mos_i]
        blur    : [srci] crop=W:H:X:Y, boxblur=R         [mos_i]

      체이닝:
        [base]   [mos_1] overlay=X1:Y1:enable=...  [v1]
        [v1]     [mos_2] overlay=X2:Y2:enable=...  [v2]
        ...
        [v_{N-1}][mos_N] overlay=XN:YN:enable=...  [out]

    최종 출력 라벨은 항상 `[out]`.
    """
    n = len(mosaics)
    assert n > 0
    # split 라벨: base + src_i (i=1..N)
    src_labels = [f"mos_src_{i+1}" for i in range(n)]
    split_outs = "[base]" + "".join(f"[{lbl}]" for lbl in src_labels)
    stmts: list[str] = [f"[0:v]split={n+1}{split_outs}"]

    mos_out_labels: list[str] = []
    for i, m in enumerate(mosaics):
        src_lbl = src_labels[i]
        x, y, w, h = m["x"], m["y"], m["w"], m["h"]
        out_lbl = f"mos_{i+1}"
        if m["mode"] == "blur":
            r = m["blur_radius"]
            # boxblur 파라미터: luma_radius:luma_power[:chroma_radius...]. power=1, chroma 동일.
            chain = f"crop={w}:{h}:{x}:{y},boxblur={r}:1"
        else:
            ps = max(2, int(m["pixel_size"]))
            small_w = max(1, w // ps)
            small_h = max(1, h // ps)
            # 다운스케일(area) → 업스케일(neighbor)로 픽셀화. 다시 크기를 원본 영역으로 복원.
            chain = (
                f"crop={w}:{h}:{x}:{y},"
                f"scale={small_w}:{small_h}:flags=area,"
                f"scale={w}:{h}:flags=neighbor"
            )
        stmts.append(f"[{src_lbl}]{chain}[{out_lbl}]")
        mos_out_labels.append(out_lbl)

    cur = "base"
    for i, m in enumerate(mosaics):
        x, y = m["x"], m["y"]
        start_sec = m["start_sec"]
        end_sec = m["end_sec"]
        if end_sec is None:
            enable = f"gte(t,{start_sec})"
        else:
            enable = f"between(t,{start_sec},{end_sec})"
        next_lbl = "out" if i == n - 1 else f"v_{i+1}"
        stmts.append(
            f"[{cur}][{mos_out_labels[i]}]"
            f"overlay={x}:{y}:enable='{enable}'[{next_lbl}]"
        )
        cur = next_lbl

    return ";".join(stmts)


def apply_mosaic_overlays(
    src_path: str,
    dst_path: str,
    mosaic_overlays: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """모자이크/블러 영역 오버레이를 ffmpeg filter_complex로 적용한다.

    구현 노트:
      - 좌표/크기는 입력 영상 해상도 기준(0~1 비율 → 픽셀).
      - shape="rect"만 정식 처리. "ellipse"는 동작은 하되 사각형으로 폴백
        (Pillow 알파 마스크는 #264 후속에서 추가).
      - 1차에서는 enable='between(t,S,E)'로 시간 범위 처리. end_sec=None이면 끝까지.

    실패 시 src를 dst로 복사하고 False 반환.
    """
    import shutil

    if not mosaic_overlays:
        shutil.copy2(src_path, dst_path)
        return True

    normalized: list[dict] = []
    ellipse_fallback = 0
    for m in mosaic_overlays:
        norm = _normalize_mosaic_overlay(m, video_width, video_height)
        if norm is None:
            continue
        if norm["shape"] == "ellipse":
            ellipse_fallback += 1
        normalized.append(norm)

    if not normalized:
        shutil.copy2(src_path, dst_path)
        return True

    if ellipse_fallback > 0:
        console.print(
            f"  [yellow]모자이크 {ellipse_fallback}개: shape=ellipse는 1차 구현에서 "
            f"사각형으로 폴백됩니다 (#264 후속 사이클에서 타원 마스크 적용 예정).[/yellow]"
        )

    audio_check = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    has_audio_stream = bool(audio_check.stdout.strip())

    fc = _build_mosaic_filter_complex(normalized)
    cmd = [
        ffmpeg_bin(), "-y", "-i", src_path,
        "-filter_complex", fc,
        "-map", "[out]",
    ]
    if has_audio_stream:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    else:
        cmd += ["-an"]
    # 다른 미리보기 단계와 동일한 crf=23 근사 (q:v 50).
    # 최종 렌더 품질은 Phase 6 별도 흐름이라 영향 없음.
    cmd += [
        *_video_encoder_args(50),
        "-pix_fmt", "yuv420p", dst_path,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True)
        if (
            r.returncode == 0
            and Path(dst_path).exists()
            and Path(dst_path).stat().st_size > 0
        ):
            return True
        console.print(
            f"  [yellow]모자이크 오버레이 적용 실패 (ffmpeg returncode={r.returncode}): "
            f"{r.stderr.decode(errors='ignore')[:200]}[/yellow]"
        )
    except Exception as e:
        console.print(f"  [yellow]모자이크 오버레이 적용 예외 (건너뜀): {e}[/yellow]")

    shutil.copy2(src_path, dst_path)
    return False


def apply_graphic_overlays(
    src_path: str,
    dst_path: str,
    graphic_overlays: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """그래픽 오버레이를 Pillow PNG → 프레임별 alpha_composite 방식으로 적용한다.

    `apply_shape_overlays`와 동일한 파이프(decode→PIL composite→encode)를 사용한다.
    성공 시 True, 실패 시 src를 dst로 복사하고 False 반환.
    """
    import shutil

    if not graphic_overlays:
        shutil.copy2(src_path, dst_path)
        return True

    video_width, video_height = _resolve_overlay_canvas(
        src_path, video_width, video_height, "그래픽 오버레이"
    )
    tmp_dir = tempfile.mkdtemp(prefix="vag_graphic_overlay_")
    try:
        from PIL import Image as _PILImage

        overlay_layers: list[dict] = []
        for i, g_data in enumerate(graphic_overlays):
            layer = _build_graphic_overlay_layer(
                g_data, video_width, video_height, tmp_dir, i,
            )
            if layer:
                overlay_layers.append(layer)

        if not overlay_layers:
            shutil.copy2(src_path, dst_path)
            return True

        fps_r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        fps_str = fps_r.stdout.strip()
        fps = 30.0
        if "/" in fps_str:
            n, d = fps_str.split("/")
            fps = float(n) / max(float(d), 1)
        elif fps_str:
            try:
                fps = float(fps_str)
            except ValueError:
                pass

        audio_check = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        has_audio_stream = bool(audio_check.stdout.strip())

        overlay_out = str(Path(tmp_dir) / "graphic_overlay.mp4")
        audio_tmp = str(Path(tmp_dir) / "audio_tmp.aac")
        if has_audio_stream:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-i", src_path, "-vn", "-c:a", "copy", audio_tmp],
                capture_output=True,
            )

        dec = subprocess.Popen(
            [ffmpeg_bin(), "-i", src_path,
             "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        enc_cmd = [
            ffmpeg_bin(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{video_width}x{video_height}",
            "-r", str(fps),
            "-i", "pipe:0",
        ]
        if has_audio_stream and Path(audio_tmp).exists() and Path(audio_tmp).stat().st_size > 0:
            enc_cmd += ["-i", audio_tmp, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
        else:
            enc_cmd += ["-an"]
        enc_cmd += [*_video_encoder_args(60),
                    "-pix_fmt", "yuv420p", overlay_out]
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        # #287 — 클립 길이로 end_sec=None인 fade 종료점 결정
        try:
            clip_duration = get_duration(src_path)
        except Exception:
            clip_duration = None

        frame_size = video_width * video_height * 3
        frame_idx = 0
        try:
            while True:
                raw = dec.stdout.read(frame_size)
                if len(raw) < frame_size:
                    break
                t = frame_idx / fps
                base = _PILImage.frombytes("RGB", (video_width, video_height), raw).convert("RGBA")
                # 그래픽 오버레이 페이드 인/아웃 (#ux) — 등장/사라짐 0.3초 알파 보간.
                FADE_BASE_G = 0.3

                def _apply_with_fade(base_img, layer_img, layer_start, layer_end):
                    """layer_img를 base_img 위에 합성하되, 페이드 알파를 시간에 따라 적용."""
                    eff_end = layer_end if layer_end is not None else (t + FADE_BASE_G * 2)
                    span = max(0.05, eff_end - layer_start)
                    fade_d = min(FADE_BASE_G, span / 2.0)
                    a_in = min(1.0, max(0.0, (t - layer_start) / fade_d)) if fade_d > 0 else 1.0
                    a_out = (
                        min(1.0, max(0.0, (eff_end - t) / fade_d))
                        if layer_end is not None and fade_d > 0 else 1.0
                    )
                    fa = a_in * a_out
                    if fa >= 0.999:
                        return _PILImage.alpha_composite(base_img, layer_img)
                    if fa <= 0:
                        return base_img
                    r2, g2, b2, a2 = layer_img.split()
                    a2 = a2.point(lambda v, k=fa: int(round(v * k)))
                    return _PILImage.alpha_composite(base_img, _PILImage.merge("RGBA", (r2, g2, b2, a2)))

                for layer in overlay_layers:
                    start_sec = float(layer.get("start_sec", 0.0))
                    end_sec = layer.get("end_sec")
                    if t < start_sec:
                        continue
                    if end_sec is not None and t > end_sec:
                        continue
                    kind = layer.get("kind")
                    # #287 — enter/exit 페이드 알파 계산 (overlay_data 없는 구버전 layer는 1.0)
                    ov_data_for_alpha = layer.get("overlay_data")
                    if ov_data_for_alpha is not None:
                        alpha_scalar = _compute_overlay_alpha_scalar(
                            ov_data_for_alpha, t,
                            visual_clip_duration=clip_duration,
                            text_supported=False,
                        )
                    else:
                        alpha_scalar = 1.0

                    def _composite_with_alpha(target_base, layer_img):
                        if alpha_scalar >= 0.999:
                            return _PILImage.alpha_composite(target_base, layer_img)
                        if alpha_scalar <= 0.001:
                            return target_base
                        r_, g_, b_, a_ = layer_img.split()
                        a_scaled = a_.point(lambda v, m=alpha_scalar: int(v * m))
                        faded = _PILImage.merge("RGBA", (r_, g_, b_, a_scaled))
                        return _PILImage.alpha_composite(target_base, faded)

                    if kind == "static":
                        png_img = layer.get("png_image")
                        if png_img is not None:
                            base = _composite_with_alpha(base, png_img)

                    elif kind == "dynamic_reaction":
                        # bounce: y 오프셋을 sin/perlin으로 변조하여 아이콘을 흔든다.
                        icon_img = layer.get("icon_image")
                        if icon_img is None:
                            continue
                        iw = int(layer.get("iw", icon_img.size[0]))
                        ih = int(layer.get("ih", icon_img.size[1]))
                        anchor_x = float(layer.get("anchor_x", video_width / 2))
                        anchor_y = float(layer.get("anchor_y", video_height / 2))
                        freq = float(layer.get("bounce_freq", 2.0))
                        amp_px = float(layer.get("bounce_amp_px", 0.0))
                        motion = str(layer.get("motion", "sine"))
                        if motion == "perlin":
                            # #224 — perlin 노이즈로 자연스러운 2축 흔들림.
                            # seed는 anchor 좌표로 만들어 overlay별로 다른 패턴.
                            hash_seed_x = (int(anchor_x) * 31 + int(anchor_y) * 17) & 0xFFFF
                            hash_seed_y = (int(anchor_x) * 53 + int(anchor_y) * 91 + 7) & 0xFFFF
                            dx = _perlin_1d(t * freq, seed=hash_seed_x) * amp_px
                            dy = _perlin_1d(t * freq, seed=hash_seed_y) * amp_px
                        else:
                            dx = 0.0
                            dy = math.sin(2.0 * math.pi * t * freq) * amp_px
                        px = int(round(anchor_x - iw / 2 + dx))
                        py = int(round(anchor_y - ih / 2 + dy))
                        shifted = _PILImage.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
                        shifted.alpha_composite(icon_img, dest=(px, py))
                        base = _composite_with_alpha(base, shifted)

                    elif kind == "dynamic_smell":
                        # loop: 한 사이클 동안 위로 rise_px만큼 이동 + 알파 페이드아웃.
                        cloud_img = layer.get("cloud_image")
                        if cloud_img is None:
                            continue
                        box_w = int(layer.get("box_w", cloud_img.size[0]))
                        box_h = int(layer.get("box_h", cloud_img.size[1]))
                        anchor_x = float(layer.get("anchor_x", video_width / 2))
                        anchor_y = float(layer.get("anchor_y", video_height / 2))
                        cycle = float(layer.get("loop_cycle_sec", 2.0))
                        alpha_min = float(layer.get("loop_alpha_min", 0.5))
                        rise_px = float(layer.get("rise_px", box_h * 0.5))
                        motion = str(layer.get("motion", "linear"))
                        if cycle <= 0:
                            cycle = 2.0
                        phase = ((t - start_sec) % cycle) / cycle  # 0~1
                        dy = -phase * rise_px
                        # #224 — perlin이면 좌우 흔들림 추가 (상승은 linear 유지)
                        if motion == "perlin":
                            hash_seed = (int(anchor_x) * 41 + int(anchor_y) * 67 + 13) & 0xFFFF
                            # 천천히 흔들리도록 t * 0.5 사용. 박스 너비의 5% 진폭.
                            dx = _perlin_1d(t * 0.5, seed=hash_seed) * (box_w * 0.05)
                        else:
                            dx = 0.0
                        # 1.0(시작) → alpha_min(끝)로 선형 페이드(연기 모션 자체의 페이드)
                        alpha_mul = 1.0 - (1.0 - alpha_min) * phase
                        alpha_mul = max(0.0, min(1.0, alpha_mul))
                        # #287 — enter/exit 페이드를 loop 알파와 곱한다
                        alpha_mul *= alpha_scalar
                        r, g, b, a = cloud_img.split()
                        a = a.point(lambda v, m=alpha_mul: int(v * m))
                        faded = _PILImage.merge("RGBA", (r, g, b, a))
                        px = int(round(anchor_x - box_w / 2 + dx))
                        py = int(round(anchor_y - box_h / 2 + dy))
                        shifted = _PILImage.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
                        shifted.alpha_composite(faded, dest=(px, py))
                        # 추가로 등장/사라짐 시점의 페이드도 적용.
                        base = _apply_with_fade(base, shifted, start_sec, end_sec)

                    elif kind == "dynamic_dash":
                        # "=3=3=3" 질주 — 마크들이 진행 방향으로 흘러가며 무한 반복.
                        mark_img = layer.get("mark_image")
                        if mark_img is None:
                            continue
                        mw = int(layer.get("mw", mark_img.size[0]))
                        mh = int(layer.get("mh", mark_img.size[1]))
                        count = int(layer.get("count", 3))
                        step_px = float(layer.get("step_px", mw))
                        anchor_x = float(layer.get("anchor_x", video_width / 2))
                        anchor_y = float(layer.get("anchor_y", video_height / 2))
                        dir_sign = float(layer.get("dir_sign", 1.0))
                        spd = float(layer.get("speed_px_sec", step_px * 3.0))
                        trail = bool(layer.get("trail", True))
                        # 한 칸(step_px) 이동하면 되감아 무한 반복 → 끊김 없는 흐름.
                        travel = ((t - start_sec) * spd) % step_px
                        # 마크 줄 전체를 anchor 중심에 오도록 시작 오프셋 계산.
                        total_w = step_px * (count - 1) + mw
                        shifted = _PILImage.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
                        for i in range(count):
                            # 진행 방향으로 travel 만큼 밀되, 뒤쪽(꼬리)일수록 흐리게.
                            off = (step_px * i) + travel * dir_sign
                            px = int(round(anchor_x - total_w / 2 + off))
                            py = int(round(anchor_y - mh / 2))
                            if px < -mw or px > video_width:
                                continue
                            if trail and count > 1:
                                # 진행 방향 기준 뒤쪽 마크를 더 투명하게(잔상 느낌).
                                rank = (count - 1 - i) if dir_sign > 0 else i
                                a_mul = 1.0 - 0.55 * (rank / max(1, count - 1))
                                m = mark_img.copy()
                                m.putalpha(m.getchannel("A").point(
                                    lambda v, _a=a_mul: int(v * _a)))
                            else:
                                m = mark_img
                            shifted.alpha_composite(m, dest=(px, py))
                        base = _apply_with_fade(base, shifted, start_sec, end_sec)

                enc.stdin.write(base.convert("RGB").tobytes())
                frame_idx += 1
        finally:
            dec.stdout.close()
            dec.wait()
            enc.stdin.close()
            enc.wait()

        if Path(overlay_out).exists() and Path(overlay_out).stat().st_size > 0:
            shutil.copy2(overlay_out, dst_path)
            return True
        else:
            shutil.copy2(src_path, dst_path)
            return False

    except Exception as e:
        console.print(f"  [yellow]그래픽 오버레이 적용 실패 (건너뜀): {e}[/yellow]")
        import shutil as _sh2
        _sh2.copy2(src_path, dst_path)
        return False
    finally:
        import shutil as _sh3
        _sh3.rmtree(tmp_dir, ignore_errors=True)


def _build_pass_through_exprs(
    ov: dict,
    base_x_expr: str,
    base_y_expr: str,
    clip_duration: float,
    effect: str,
) -> tuple[str, str]:
    """관통(pass-through) 효과의 drawtext x/y 시간 표현식을 만든다.

    한쪽 화면 밖에서 날아 들어와 원위치에 머물다가 "반대편" 화면 밖으로 빠져나간다.
    `_compute_overlay_transform` 의 PNG 경로와 같은 궤적을 ffmpeg 표현식으로 표현한 것이다.

    진입: [start, start+enter_dur] 에서 진입쪽 밖 → 원위치 (ease-out cubic)
    유지: 오프셋 0
    퇴장: [end-exit_dur, end] 에서 원위치 → 반대편 밖 (ease-out cubic, 진입의 시간 역순)
    """
    vec_in, vec_out = _PASS_THROUGH_VECTORS.get(effect, ((0.0, 0.0), (0.0, 0.0)))
    _, _, enter_dur, exit_dur = _get_overlay_effect_config(ov, text_supported=True)

    start_sec = float(ov.get("start_sec", 0.0) or 0.0)
    end_raw = ov.get("end_sec")
    end_eff = float(end_raw) if end_raw is not None else float(clip_duration)

    # 화면 밖 거리 — drawtext 는 w/h(=영상 크기) 를 표현식에서 쓸 수 있다.
    off_w, off_h = "(w*1.1)", "(h*1.1)"

    def _axis(base_expr: str, v_in: float, v_out: float, off: str) -> str:
        if abs(v_in) < 1e-9 and abs(v_out) < 1e-9:
            return base_expr
        terms = []
        if enter_dur > 0 and abs(v_in) > 1e-9:
            # p = 진행도 0→1, eased = 1-(1-p)^3 (ease-out cubic). remaining = 1-eased.
            p = f"clip((t-{start_sec:.4f})/{enter_dur:.4f},0,1)"
            remaining = f"pow(1-{p},3)"
            terms.append(
                f"if(lt(t,{start_sec + enter_dur:.4f}),{v_in:+.4f}*{off}*{remaining},0)"
            )
        if exit_dur > 0 and abs(v_out) > 1e-9 and end_eff > start_sec:
            exit_start = end_eff - exit_dur
            # p = 퇴장 진행도 0→1 (exit_start→end). remaining 은 진입의 시간 역순.
            p = f"clip((t-{exit_start:.4f})/{exit_dur:.4f},0,1)"
            gone = f"pow({p},3)"
            terms.append(
                f"if(gte(t,{exit_start:.4f}),{v_out:+.4f}*{off}*{gone},0)"
            )
        if not terms:
            return base_expr
        return f"({base_expr})+" + "+".join(f"({tm})" for tm in terms)

    return (
        _axis(base_x_expr, vec_in[0], vec_out[0], off_w),
        _axis(base_y_expr, vec_in[1], vec_out[1], off_h),
    )


def _build_loop_animation_exprs(
    ov: dict,
    base_x_expr: str,
    base_y_expr: str,
    font_size: int,
) -> tuple[str, str, str]:
    """표시 중 반복 애니메이션(loop_animation)을 적용한 (x, y, fontsize_factor) 표현식을 반환한다.

    - bounce: y 가 위로 통통 튀어오름. 진폭은 캔버스 높이 비율.
    - shake: x 가 빠르게 좌우 진동. 진폭은 폰트 크기 비율(작게).
    - pulse: fontsize 가 주기적으로 커졌다 작아짐 (factor = 1 + amp*sin).
    - wave: x 가 부드럽게 좌우 흐름. 진폭은 캔버스 폭 비율.
    'none'이면 base 표현식 그대로 반환 (factor=1).
    시간 기준은 (t - start_sec) — 표시 시작 시점에서 0.
    """
    anim = str(ov.get("loop_animation") or "none").lower()
    if anim not in ("bounce", "shake", "pulse", "wave"):
        return base_x_expr, base_y_expr, "1"
    amp = float(ov.get("loop_amplitude") or 0.02)
    period = max(0.1, float(ov.get("loop_period_sec") or 0.8))
    start_sec = float(ov.get("start_sec") or 0.0)
    # ffmpeg expression: t 는 현재 프레임 시각(초). 표시 시작 기준 상대시간.
    rel_t = f"(t-{start_sec:.4f})"
    two_pi_over_T = f"(2*PI/{period:.4f})"
    if anim == "bounce":
        # 위로 튕김 — abs(sin) 으로 한쪽만 튀고 (y 좌표는 위가 작아짐), 진폭은 캔버스 높이 비율.
        amp_px = f"(h*{amp:.4f})"
        # 부호: 위로 튕기려면 y 좌표를 줄여야 함 → -abs(sin).
        offset = f"(-abs(sin({rel_t}*{two_pi_over_T}))*{amp_px})"
        return base_x_expr, f"({base_y_expr})+{offset}", "1"
    if anim == "shake":
        # 좌우 빠른 진동 — 폰트 크기 대비 작은 진폭. 빈도는 12Hz 고정 (자연스러운 떨림).
        amp_px = f"({font_size}*{amp:.4f})"
        offset = f"(sin({rel_t}*2*PI*12)*{amp_px})"
        return f"({base_x_expr})+{offset}", base_y_expr, "1"
    if anim == "pulse":
        # 폰트 크기 호흡 — factor = 1 + amp*sin. 호출 측에서 fontsize 에 곱한다.
        factor = f"(1+sin({rel_t}*{two_pi_over_T})*{amp:.4f})"
        return base_x_expr, base_y_expr, factor
    # wave
    amp_px = f"(w*{amp:.4f})"
    offset = f"(sin({rel_t}*{two_pi_over_T})*{amp_px})"
    return f"({base_x_expr})+{offset}", base_y_expr, "1"


# 페이드 알파 양자화 스텝 수 — 매 프레임 알파를 계속 바꾸지 않고 이 스텝으로 양자화해
# alpha_composite 결과를 캐시하기 좋게 한다. 미리보기 ≡ 렌더 동일(같은 알파→같은 합성).
SPEED_LINES_ALPHA_STEPS = 20


def compute_speed_lines_bucket_alpha(
    t: float,
    start_sec: float,
    bucket: int,
    num_buckets: int,
    *,
    animate: bool,
    anim_speed: float,
    anim_period: float,
    steps: int = SPEED_LINES_ALPHA_STEPS,
) -> float:
    """집중선 페이드 버킷의 시각 t 에서의 알파(0~1)를 양자화해 반환.

    각 선은 최대 길이로 고정 그려지고(선 길이 성장 폐기), 대신 버킷별로 페이드 인/아웃을
    반복한다. 버킷 b 의 위상 offset = b/num_buckets 만큼 시간축에서 밀려, 버킷마다 다른
    타이밍에 깜빡인다(선별 랜덤 위상 → 무작위 깜빡임).

    페이드 곡선: 한 주기 안에서 sin^2 로 0→1→0 부드럽게 왕복(페이드 인→아웃). 매 주기
    반복. anim_period/anim_speed 로 주기 길이를 정한다.

    animate=False 면 1.0(정적, 모든 선 완전 표시) 반환. 순수 함수.
    """
    if not animate:
        return 1.0
    nb = max(1, int(num_buckets))
    spd = max(0.05, float(anim_speed))
    period = max(0.05, float(anim_period)) / spd
    elapsed = max(0.0, float(t) - float(start_sec))
    # 버킷별 위상 offset — 버킷마다 주기의 b/nb 만큼 시간차.
    offset = (int(bucket) % nb) / nb
    frac = ((elapsed / period) + offset) % 1.0   # 0..1 주기 위상
    # sin^2 페이드 인/아웃: frac 0→0.5→1 에서 0→1→0.
    alpha = math.sin(math.pi * frac) ** 2
    n = max(1, int(steps))
    q = round(alpha * n) / n
    return max(0.0, min(1.0, q))


def _composite_speed_lines_layer(pil_image_mod, base, layer, alpha_scalar: float):
    """집중선 레이어(PIL RGBA) 를 base 위에 alpha_scalar(0~1) 로 합성해 반환한다.

    alpha_scalar>=0.999 면 직접 alpha_composite, 아니면 레이어 알파 채널을 스케일 후 합성.
    미리보기/렌더 공통 경로가 재사용하도록 분리(픽셀 동일성 보장).
    """
    if alpha_scalar >= 0.999:
        return pil_image_mod.alpha_composite(base, layer)
    r_, g_, b_, a_ = layer.split()
    a_scaled = a_.point(lambda v, m=alpha_scalar: int(v * m))
    faded = pil_image_mod.merge("RGBA", (r_, g_, b_, a_scaled))
    return pil_image_mod.alpha_composite(base, faded)


def apply_speed_lines(
    src_path: str,
    dst_path: str,
    speed_lines: list[dict],
    video_width: int,
    video_height: int,
) -> bool:
    """집중선(속도선) 오버레이를 Pillow PNG → 프레임별 alpha_composite 로 적용한다.

    `apply_graphic_overlays`/`apply_image_overlays` 와 동일한 decode→PIL composite→encode
    파이프를 사용한다. 각 speed_lines dict 는 색/밀도/중심/두께 등 파라미터를 담으며,
    `src.speed_lines.generate_speed_lines_png` 로 파라미터+해상도 해시 캐시 PNG 를 얻는다.

    **미리보기 ≡ 렌더 동일성**: 미리보기와 최종 렌더가 모두 이 함수를 호출하고 동일한
    캐시 PNG 를 재사용하므로 결과가 같다.

    성공 시 True, 실패 시 src 를 dst 로 복사하고 False 반환.
    """
    import shutil

    if not speed_lines:
        shutil.copy2(src_path, dst_path)
        return True

    from src.speed_lines import (
        DEFAULT_AREA_H, DEFAULT_AREA_W, DEFAULT_CENTER_X, DEFAULT_CENTER_Y,
        DEFAULT_COLOR, DEFAULT_DENSITY, DEFAULT_INNER_RATIO, DEFAULT_JITTER,
        DEFAULT_NUM_BUCKETS, DEFAULT_SEED, DEFAULT_SUPERSAMPLE, DEFAULT_THICKNESS,
        DEFAULT_WIDTH_VARIANCE,
        generate_speed_lines_png,
    )

    tmp_dir = tempfile.mkdtemp(prefix="vag_speed_lines_")
    try:
        from PIL import Image as _PILImage

        # 각 오버레이의 파라미터를 미리 정규화해 둔다. 선은 항상 최대 길이로 고정 그리고
        # (선 길이 성장 anim_t 폐기), 대신 선을 페이즈 버킷 N개로 나눠 버킷별 PNG 를 미리
        # 생성한다. 프레임마다 각 버킷의 페이드 알파(sin^2 왕복)를 계산해 버킷 PNG 를 그
        # 알파로 합성 → 선마다 무작위 타이밍으로 깜빡인다. 버킷 PNG 는 시간 불변이라
        # 오버레이당 최대 N개만 생성/캐시(성능 안전).
        overlay_layers: list[tuple] = []
        for ov in speed_lines:
            if not isinstance(ov, dict):
                continue
            num_buckets = int(ov.get("num_buckets", DEFAULT_NUM_BUCKETS) or DEFAULT_NUM_BUCKETS)
            num_buckets = max(1, num_buckets)
            params = dict(
                out_w=video_width,
                out_h=video_height,
                color=str(ov.get("color", DEFAULT_COLOR) or DEFAULT_COLOR),
                density=int(ov.get("density", DEFAULT_DENSITY) or DEFAULT_DENSITY),
                center_x=float(ov.get("center_x", DEFAULT_CENTER_X)),
                center_y=float(ov.get("center_y", DEFAULT_CENTER_Y)),
                inner_ratio=float(ov.get("inner_ratio", DEFAULT_INNER_RATIO)),
                thickness=float(ov.get("thickness", DEFAULT_THICKNESS)),
                jitter=float(ov.get("jitter", DEFAULT_JITTER)),
                seed=int(ov.get("seed", DEFAULT_SEED) or 0),
                supersample=int(ov.get("supersample", DEFAULT_SUPERSAMPLE) or DEFAULT_SUPERSAMPLE),
                area_w=float(ov.get("area_w", DEFAULT_AREA_W) if ov.get("area_w") is not None else DEFAULT_AREA_W),
                area_h=float(ov.get("area_h", DEFAULT_AREA_H) if ov.get("area_h") is not None else DEFAULT_AREA_H),
                width_variance=float(ov.get("width_variance", DEFAULT_WIDTH_VARIANCE) if ov.get("width_variance") is not None else DEFAULT_WIDTH_VARIANCE),
                num_buckets=num_buckets,
            )
            start_sec = float(ov.get("start_sec", 0.0) or 0.0)
            end_sec = ov.get("end_sec")
            opacity = float(ov.get("opacity", 1.0) if ov.get("opacity") is not None else 1.0)
            # ── 선별 페이드 인/아웃 반복 애니메이션 파라미터 ─────────────────────
            # animate=True(기본, 필드 없는 기존 씬 포함) 면 버킷별 페이드 알파로 선들이
            # 무작위 타이밍에 깜빡인다. False 면 정적(모든 선 알파 1.0, 단일 PNG).
            animate = bool(ov.get("animate", True) if ov.get("animate") is not None else True)
            anim_speed = float(ov.get("anim_speed", 1.0) if ov.get("anim_speed") is not None else 1.0)
            anim_period = float(ov.get("anim_period_sec", 0.7) if ov.get("anim_period_sec") is not None else 0.7)
            anim = (animate, anim_speed, anim_period)

            # 버킷 PNG 를 미리 생성해 in-memory 로드. animate=False 면 단일 PNG(phase_bucket=None).
            bucket_imgs: dict[Optional[int], object] = {}

            def _load_png(pb):
                png_path = generate_speed_lines_png(phase_bucket=pb, **params)
                img = _PILImage.open(png_path).convert("RGBA")
                if img.size != (video_width, video_height):
                    img = img.resize((video_width, video_height))
                return img

            if animate:
                for b in range(num_buckets):
                    bucket_imgs[b] = _load_png(b)
            else:
                bucket_imgs[None] = _load_png(None)
            overlay_layers.append((start_sec, end_sec, opacity, ov, anim, num_buckets, bucket_imgs))

        if not overlay_layers:
            shutil.copy2(src_path, dst_path)
            return True

        fps_r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        fps_str = fps_r.stdout.strip()
        fps = 30.0
        if "/" in fps_str:
            n, d = fps_str.split("/")
            try:
                fps = float(n) / max(float(d), 1)
            except ValueError:
                pass
        elif fps_str:
            try:
                fps = float(fps_str)
            except ValueError:
                pass

        audio_check = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        has_audio_stream = bool(audio_check.stdout.strip())

        overlay_out = str(Path(tmp_dir) / "speed_lines.mp4")
        audio_tmp = str(Path(tmp_dir) / "audio_tmp.aac")
        if has_audio_stream:
            subprocess.run(
                [ffmpeg_bin(), "-y", "-i", src_path, "-vn", "-c:a", "copy", audio_tmp],
                capture_output=True,
            )

        dec = subprocess.Popen(
            [ffmpeg_bin(), "-i", src_path, "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        enc_cmd = [
            ffmpeg_bin(), "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{video_width}x{video_height}",
            "-r", str(fps), "-i", "pipe:0",
        ]
        if has_audio_stream and Path(audio_tmp).exists() and Path(audio_tmp).stat().st_size > 0:
            enc_cmd += ["-i", audio_tmp, "-map", "0:v", "-map", "1:a", "-c:a", "copy"]
        else:
            enc_cmd += ["-an"]
        enc_cmd += [*_video_encoder_args(60), "-pix_fmt", "yuv420p", overlay_out]
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

        try:
            clip_duration = get_duration(src_path)
        except Exception:
            clip_duration = None

        frame_size = video_width * video_height * 3
        frame_idx = 0
        try:
            while True:
                raw = dec.stdout.read(frame_size)
                if len(raw) < frame_size:
                    break
                t = frame_idx / fps
                base = _PILImage.frombytes("RGB", (video_width, video_height), raw).convert("RGBA")
                for (start_sec, end_sec, opacity, ov_data, anim, num_buckets, bucket_imgs) in overlay_layers:
                    if t < start_sec:
                        continue
                    if end_sec is not None and t > end_sec:
                        continue
                    # enter/exit fade — 다른 오버레이와 동일 알파 계산.
                    alpha_scalar = _compute_overlay_alpha_scalar(
                        ov_data, t,
                        visual_clip_duration=clip_duration,
                        text_supported=False,
                    )
                    alpha_scalar *= max(0.0, min(1.0, opacity))
                    if alpha_scalar <= 0.001:
                        continue
                    (an_on, an_spd, an_period) = anim
                    if not an_on:
                        # 정적: 모든 선을 담은 단일 PNG 를 alpha_scalar 로 합성.
                        frame_png = bucket_imgs.get(None)
                        if frame_png is None:
                            continue
                        base = _composite_speed_lines_layer(
                            _PILImage, base, frame_png, alpha_scalar,
                        )
                        continue
                    # 애니: 각 버킷의 페이드 알파(sin^2 왕복)로 버킷 PNG 를 합성.
                    for b in range(num_buckets):
                        frame_png = bucket_imgs.get(b)
                        if frame_png is None:
                            continue
                        bucket_alpha = compute_speed_lines_bucket_alpha(
                            t, start_sec, b, num_buckets,
                            animate=True, anim_speed=an_spd, anim_period=an_period,
                        )
                        eff_alpha = alpha_scalar * bucket_alpha
                        if eff_alpha <= 0.001:
                            continue
                        base = _composite_speed_lines_layer(
                            _PILImage, base, frame_png, eff_alpha,
                        )
                enc.stdin.write(base.convert("RGB").tobytes())
                frame_idx += 1
        finally:
            dec.stdout.close()
            dec.wait()
            enc.stdin.close()
            enc.wait()

        if Path(overlay_out).exists() and Path(overlay_out).stat().st_size > 0:
            shutil.copy2(overlay_out, dst_path)
            return True
        shutil.copy2(src_path, dst_path)
        return False
    except Exception as e:
        console.print(f"  [yellow]집중선 오버레이 적용 예외 (원본 유지): {e}[/yellow]")
        import shutil as _sh_e
        _sh_e.copy2(src_path, dst_path)
        return False
    finally:
        try:
            import shutil as _sh_f
            _sh_f.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _expand_countdown_overlay(ov: dict, clip_duration: float | None = None) -> list[dict]:
    """카운트다운 오버레이 1건을 "텍스트가 고정된" 오버레이 여러 건으로 펼친다.

    PNG 합성(complex) 경로는 "오버레이 1건 = 고정 텍스트 1개 = PNG 1장" 전제라
    시간대별로 텍스트가 바뀌는 카운트다운을 그대로 처리할 수 없다. 숫자마다
    `text` 가 확정된 별도 오버레이로 펼쳐 두면 각자 자기 enable 구간에 합성되므로
    그라디언트/글로우가 그대로 살아난다.

    `web/backend/routers/project.py::_apply_text_overlays_preview` 의
    `_expand_countdown` 과 동일한 규칙을 따른다(미리보기 ↔ 최종 렌더 일치).

    카운트다운이 아니면 `[ov]` 를 그대로 돌려준다.
    """
    cd_from = int(ov.get("countdown_from", 0) or 0)
    if cd_from <= 0:
        return [ov]
    cd_to = int(ov.get("countdown_to", 1) or 0)
    # 내림차순만 지원(줄어드는 카운트다운). to > from 이면 값을 교환해 방어.
    if cd_to > cd_from:
        cd_from, cd_to = cd_to, cd_from
    try:
        interval = float(ov.get("countdown_interval_sec", 1.0) or 1.0)
    except (TypeError, ValueError):
        interval = 1.0
    interval = max(0.05, min(60.0, interval))

    base_start = float(ov.get("start_sec", 0.0) or 0.0)
    end_raw = ov.get("end_sec")
    end_eff = (
        float(end_raw)
        if end_raw is not None
        else (float(clip_duration) if clip_duration else None)
    )

    out: list[dict] = []
    # 숫자가 너무 많으면 PNG 생성/ffmpeg 패스가 폭주하므로 상한을 둔다.
    numbers = list(range(cd_from, cd_to - 1, -1))[:120]
    for i, num in enumerate(numbers):
        seg_start = base_start + i * interval
        seg_end = seg_start + interval
        if end_raw is not None:
            seg_end = min(seg_end, float(end_raw))
        if seg_end <= seg_start:
            continue
        step = dict(ov)
        step["text"] = str(num)
        step["start_sec"] = seg_start
        step["end_sec"] = seg_end
        # 숫자마다 등장/사라짐 페이드가 겹치면 깜빡이므로 진입/종료 효과는 끈다.
        step["enter_effect"] = "none"
        step["exit_effect"] = "none"
        step["countdown_from"] = 0  # 재귀 방지
        out.append(step)

    # 카운트다운이 끝난 뒤 문구(예: "액션!") — 숫자와 완전히 동일한 스타일로 그린다.
    end_text = str(ov.get("countdown_end_text", "") or "")
    if end_text:
        fin_start = base_start + len(numbers) * interval
        if end_eff is None or end_eff > fin_start:
            fin = dict(ov)
            fin["text"] = end_text
            fin["start_sec"] = fin_start
            fin["end_sec"] = float(end_raw) if end_raw is not None else None
            fin["enter_effect"] = "none"
            fin["exit_effect"] = "none"
            fin["countdown_from"] = 0
            out.append(fin)
    return out


def _expand_text_speedlines_overlays(
    overlays: list[dict], clip_duration: float
) -> list[dict]:
    """`loop_animation="speedlines"` 오버레이를 버킷별 시간 조각으로 펼친다.

    집중선은 선 그룹(버킷)마다 다른 타이밍에 깜빡여야 살아 있는 느낌이 난다. PNG 합성
    경로는 "PNG 1장 = 고정 이미지" 이므로, 표시 구간을 짧은 스텝으로 잘라 각 스텝에
    버킷이 순환하도록 배정한다. 각 조각은 `_speedlines_bucket` 으로 어떤 버킷 PNG 를
    쓸지 지정하고, 조각 경계에서 페이드가 겹쳐 깜빡이지 않도록 enter/exit 는 끈다.

    speedlines 가 아닌 오버레이는 그대로 통과시킨다.
    """
    try:
        from src.speed_lines import DEFAULT_NUM_BUCKETS
    except Exception:
        DEFAULT_NUM_BUCKETS = 8

    out: list[dict] = []
    for ov in overlays:
        if str(ov.get("loop_animation") or "none").lower() != "speedlines":
            out.append(ov)
            continue

        cfg = ov.get("text_speed_lines") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        try:
            num_buckets = max(1, int(cfg.get("num_buckets", DEFAULT_NUM_BUCKETS) or DEFAULT_NUM_BUCKETS))
        except (TypeError, ValueError):
            num_buckets = DEFAULT_NUM_BUCKETS
        # animate=False 면 정적(모든 선) 1장 — 버킷 분할 없이 그대로 둔다.
        #
        # tail 모드는 기본이 정적이다. 꼬리는 "글자가 날아온 잔상"이라 모양이 고정이고
        # 깜빡일 이유가 없다. 그런데 버킷 분할을 그대로 타면 표시 구간이 수십 조각으로
        # 쪼개져 조각마다 1080x1920 PNG 를 새로 그린다(장당 약 0.28초). 실측: 오버레이
        # 7개가 292 조각으로 펼쳐져 PNG 생성만 80초 + 조각마다 ffmpeg overlay 필터가
        # 붙어 27초 영상 렌더가 15분 넘게 걸렸다. tail 은 1장이면 충분하다.
        mode = str(cfg.get("mode") or "tail").lower()
        animate = cfg.get("animate")
        if animate is None:
            animate = mode != "tail"
        else:
            animate = bool(animate)
        if not animate:
            step_ov = dict(ov)
            step_ov["_speedlines_bucket"] = None
            out.append(step_ov)
            continue

        try:
            period = float(cfg.get("anim_period_sec", 0.7) or 0.7)
        except (TypeError, ValueError):
            period = 0.7
        try:
            speed = float(cfg.get("anim_speed", 1.0) or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        period = max(0.05, period) / max(0.05, speed)

        start_sec = float(ov.get("start_sec", 0.0) or 0.0)
        end_raw = ov.get("end_sec")
        end_eff = float(end_raw) if end_raw is not None else float(clip_duration)
        if end_eff <= start_sec:
            out.append(ov)
            continue

        # 한 주기를 버킷 수만큼 나눈 길이가 한 조각. 조각이 너무 잘게 쪼개져 ffmpeg 패스가
        # 폭주하지 않도록 최소 길이와 총 개수에 상한을 둔다.
        step_dur = max(0.05, period / num_buckets)
        max_steps = 240
        total = end_eff - start_sec
        n_steps = min(max_steps, max(1, int(math.ceil(total / step_dur))))
        for i in range(n_steps):
            seg_start = start_sec + i * step_dur
            seg_end = min(seg_start + step_dur, end_eff)
            if seg_end <= seg_start:
                continue
            step_ov = dict(ov)
            step_ov["start_sec"] = seg_start
            step_ov["end_sec"] = seg_end
            step_ov["_speedlines_bucket"] = i % num_buckets
            # 조각마다 페이드가 들어가면 깜빡이므로 진입/종료 효과는 끈다.
            step_ov["enter_effect"] = "none"
            step_ov["exit_effect"] = "none"
            # 관통 효과는 오버레이 "전체" 의 궤적이므로 조각마다 껐다 켜면 안 된다.
            # 원본 구간/효과를 남겨 두면 각 조각이 같은 궤적 위의 자기 시점을 그린다.
            if str(ov.get("enter_effect") or "").lower() in _PASS_THROUGH_EFFECTS:
                step_ov["_pass_through_effect"] = str(ov.get("enter_effect")).lower()
                step_ov["_pass_through_start"] = start_sec
                step_ov["_pass_through_end"] = end_eff
            out.append(step_ov)
    return out


def _build_text_drawtext_parts(
    ov: dict,
    clip_duration: float,
    video_height: int = 1080,
) -> list[str]:
    """텍스트 오버레이 1건에 대해 drawtext 필터 표현식 목록을 생성한다 (#287).

    enter_effect/exit_effect 조합에 따라 1개 또는 여러 drawtext가 만들어진다:
      - typing: 부분 문자열 N개의 drawtext (각 1글자씩 추가, enable 구간 분할)
      - fade/zoom/none: 1개의 drawtext (alpha/fontsize 표현식 적용)

    loop_animation(bounce/shake/pulse/wave)이 'none'이 아니면 x/y/fontsize 표현식에
    삼각함수 기반 모션을 합성한다. ffmpeg drawtext 는 x/y/fontsize 를 매 프레임 재평가하므로
    별도 단계 없이 표현식 합성만으로 애니메이션이 적용된다.
    """
    text = ov.get("text", "")
    # 카운트다운은 숫자를 직접 그리므로 text 가 비어 있어도 진행한다.
    if not text and int(ov.get("countdown_from", 0) or 0) <= 0:
        return []
    text_escaped = _escape_drawtext(text)
    font_size = int(ov.get("font_size", 40))
    color_hex = ov.get("color", "#ffffff")
    opacity = float(ov.get("opacity", 1.0))
    x_ratio = float(ov.get("x", 0.5))
    y_ratio = float(ov.get("y", 0.1))
    fc = _hex_to_ffmpeg_color(color_hex, opacity)
    # 앵커: (w-text_w)*x — 박스가 화면 안에 들어오도록 하는 비례 배치.
    #
    # ⚠ Pillow 경로(_render_overlay_png)와 좌표를 맞추기 위한 PAD 보정이 필요하다.
    #   Pillow 는 글리프 bbox 에 PAD(=40) 를 상하좌우로 덧대 tw/th 를 부풀린 뒤
    #     ty = (H - th)*y - b_top + PAD   (th = 글리프높이 + PAD*2)
    #   로 그린다. drawtext 의 text_h 에는 그 PAD 가 없어서, 같은 y 라도 두 경로가
    #   어긋난다(실측: 같은 y=0.1 에서 Pillow 가 약 31px 아래).
    #   그라디언트/글로우 유무로 두 경로가 갈리므로, 같은 씬 안에서도 자막이
    #   위아래로 들쭉날쭉해진다. 그래서 drawtext 쪽에 동일한 PAD 항을 넣어 맞춘다.
    #   PAD 는 파이썬에서 상수로 계산해 넣는다. ffmpeg 표현식(`40*h/1080`)으로 쓰면
    #   zoom 진입 효과처럼 fontsize 가 시간 표현식일 때 text_h 평가와 겹쳐
    #   필터 파싱이 실패하고, 그러면 자막이 통째로 사라진다(사용자 보고: 13번 씬 이후).
    _pad = _overlay_text_pad(video_height)
    base_x_expr = f"(w-(text_w+{_pad * 2}))*{x_ratio:.4f}+{_pad}"
    base_y_expr = f"(h-(text_h+{_pad * 2}))*{y_ratio:.4f}+{_pad}"
    x_expr, y_expr, loop_fontsize_factor = _build_loop_animation_exprs(
        ov, base_x_expr, base_y_expr, font_size
    )
    # 관통(pass-through): drawtext 는 x/y 를 매 프레임 재평가하므로 시간 표현식으로 처리한다.
    # 진입 구간엔 진입쪽 화면 밖에서 원위치로, 퇴장 구간엔 원위치에서 반대편 화면 밖으로 이동.
    _pass_enter = str(ov.get("enter_effect") or "").lower()
    if _pass_enter in _PASS_THROUGH_EFFECTS:
        x_expr, y_expr = _build_pass_through_exprs(
            ov, x_expr, y_expr, clip_duration, _pass_enter
        )
    # ffmpeg 8.x 회피 — drawtext 의 fontsize 를 "표현식" 으로 주면 인코딩 중 segfault(-11).
    # 정적 숫자면 안전하므로 여기서 배율을 떨군다. pulse 단독 텍스트는 상위에서 PNG 합성
    # 경로로 보내 실제 크기 애니메이션이 유지되고, 카운트다운처럼 drawtext 가 필수인
    # 경우에만 크기 변화가 생략된다(문자는 정상 표시 — 크래시로 통째로 사라지는 것보다 낫다).
    if loop_fontsize_factor != "1":
        loop_fontsize_factor = "1"

    outline_w = int(ov.get("outline_width", 2))
    outline_hex = ov.get("outline_color", "#000000")
    # outline은 보통 불투명. (이전: 알파 반전 버그가 있던 시절 0.0이 불투명이었음)
    bc = _hex_to_ffmpeg_color(outline_hex, 1.0)

    font_path = _find_font_path(ov.get("font_family", "NotoSansKR"))
    font_part = f":fontfile='{_escape_filter_path(font_path)}'" if font_path else ""

    start_sec = float(ov.get("start_sec", 0.0))
    end_sec_raw = ov.get("end_sec")
    end_sec_eff = float(end_sec_raw) if end_sec_raw is not None else float(clip_duration)
    visible_end = (
        f"between(t,{start_sec:.3f},{float(end_sec_raw):.3f})"
        if end_sec_raw is not None
        else f"gte(t,{start_sec:.3f})"
    )

    enter_effect, exit_effect, enter_dur, exit_dur = _get_overlay_effect_config(
        ov, text_supported=True
    )

    # ── 카운트다운 처리: 숫자마다 별도 drawtext + enable 구간 분할 ──
    # typing 과 같은 원리(시간대별로 다른 텍스트를 그린다)지만, 텍스트가 prefix 가 아니라
    # N, N-1, … countdown_to 로 바뀐다. 폰트/색/외곽선/위치/loop 애니메이션 표현식은 위에서
    # 이미 계산한 값을 그대로 재사용하므로 일반 텍스트 오버레이와 스타일이 완전히 동일하다.
    parts: list[str] = []
    cd_from = int(ov.get("countdown_from", 0) or 0)
    if cd_from > 0:
        cd_to = int(ov.get("countdown_to", 1) or 0)
        interval = float(ov.get("countdown_interval_sec", 1.0) or 1.0)
        interval = max(0.05, min(60.0, interval))
        end_text = str(ov.get("countdown_end_text", "") or "")
        # 내림차순만 지원(줄어드는 카운트다운). to > from 이면 값을 교환해 방어.
        if cd_to > cd_from:
            cd_from, cd_to = cd_to, cd_from
        # 숫자가 너무 많으면 ffmpeg 명령어가 폭주하므로 상한을 둔다.
        numbers = list(range(cd_from, cd_to - 1, -1))[:120]
        for i, num in enumerate(numbers):
            seg_start = start_sec + i * interval
            seg_end = seg_start + interval
            # 오버레이 종료 시각을 넘어서면 잘라낸다.
            if end_sec_raw is not None:
                seg_end = min(seg_end, float(end_sec_raw))
            if seg_end <= seg_start:
                continue
            if loop_fontsize_factor != "1":
                fs_part = f"({font_size})*{loop_fontsize_factor}"
            else:
                fs_part = str(font_size)
            parts.append(
                f"drawtext=expansion=none:text='{_escape_drawtext(str(num))}':x='{x_expr}':y='{y_expr}'"
                f":fontsize='{fs_part}':fontcolor={fc}"
                f":borderw={outline_w}:bordercolor={bc}"
                f"{font_part}:enable='between(t,{seg_start:.4f},{seg_end:.4f})'"
            )
        # 카운트다운이 끝난 뒤 문구(예: "시작!") — 오버레이 종료까지 표시.
        if end_text:
            fin_start = start_sec + len(numbers) * interval
            fin_end = float(end_sec_raw) if end_sec_raw is not None else end_sec_eff
            if fin_end > fin_start:
                if loop_fontsize_factor != "1":
                    fs_part = f"({font_size})*{loop_fontsize_factor}"
                else:
                    fs_part = str(font_size)
                parts.append(
                    f"drawtext=expansion=none:text='{_escape_drawtext(end_text)}':x='{x_expr}':y='{y_expr}'"
                    f":fontsize='{fs_part}':fontcolor={fc}"
                    f":borderw={outline_w}:bordercolor={bc}"
                    f"{font_part}:enable='between(t,{fin_start:.4f},{fin_end:.4f})'"
                )
        return parts

    # ── typing 처리: 각 prefix를 별도 drawtext로 그려 enable로 시점 제어 ──
    # 진입 구간 [start, start+enter_dur]를 글자 수로 나눠 한 글자씩 노출.
    # 진입 효과가 typing일 때만 typing 분할. 종료 효과가 typing이면 동일하게 역방향 적용
    # (글자가 하나씩 사라짐). 둘 다 typing 아니면 단일 drawtext.
    if enter_effect == "typing" or exit_effect == "typing":
        # 안전 한도: 너무 긴 문자열에 typing 적용 시 ffmpeg 명령어 길이 폭주 방지.
        # 60자 이상이면 6개 청크 단위로 노출. 60자 미만이면 1글자 단위.
        n = len(text)
        if n == 0:
            return []
        if n > 60:
            step = max(1, n // 30)  # 최대 30 chunks
        else:
            step = 1
        chunks: list[int] = list(range(step, n + 1, step))
        if chunks[-1] != n:
            chunks.append(n)
        # 진입 typing: chunks[i]가 [start + i/len(chunks) * enter_dur, ...] 구간부터 보임
        # 종료 typing: chunks[i]가 ... ~ [end - (1 - (i+1)/len(chunks)) * exit_dur] 구간까지 보임
        m = len(chunks)
        # 진입/종료 효과 중 하나만 typing이고 나머지가 fade이면 청크 단위에서 fade alpha 추가.
        combined_enter_fade = enter_effect == "fade"
        combined_exit_fade = exit_effect == "fade"
        for i, char_count in enumerate(chunks):
            substr = text[:char_count]
            substr_escaped = _escape_drawtext(substr)
            # 진입 시점
            if enter_effect == "typing" and enter_dur > 0:
                seg_appear = start_sec + (i / m) * enter_dur
            else:
                seg_appear = start_sec
            # 다음 청크가 나타나기 직전까지 이 prefix 유지
            if i + 1 < m:
                if enter_effect == "typing" and enter_dur > 0:
                    seg_disappear_enter = start_sec + ((i + 1) / m) * enter_dur
                else:
                    seg_disappear_enter = end_sec_eff
            else:
                seg_disappear_enter = end_sec_eff
            # 종료 typing: 마지막 prefix는 종료 구간 동안 점차 사라짐
            if exit_effect == "typing" and exit_dur > 0:
                # 종료 typing은 역방향이므로 chunks[i]가 보이는 종료점은
                # (i+1)/m 만큼 끝에서 일찍 사라진다 → end - (1 - i/m) * exit_dur
                seg_disappear_exit = end_sec_eff - (1.0 - i / m) * exit_dur
            else:
                seg_disappear_exit = end_sec_eff
            # 전체 가시 윈도우는 둘의 교집합 [seg_appear, min(seg_disappear_enter, seg_disappear_exit)]
            seg_end = min(seg_disappear_enter, seg_disappear_exit)
            if seg_end <= seg_appear:
                continue
            enable_expr = f"between(t,{seg_appear:.4f},{seg_end:.4f})"
            # 마지막 청크 + exit_effect=fade이면 페이드아웃 alpha 적용
            # base_alpha=1.0: fontcolor에 opacity가 이미 들어있으므로 이중 적용 방지.
            alpha_part = ""
            if i == m - 1 and combined_exit_fade and exit_dur > 0:
                exit_alpha_expr = _build_fade_alpha_expr(
                    start_sec=start_sec,
                    end_sec=end_sec_raw,
                    enter_effect="none",
                    exit_effect="fade",
                    enter_dur=0.0,
                    exit_dur=exit_dur,
                    base_alpha=1.0,
                    visual_clip_duration=clip_duration,
                )
                try:
                    float(exit_alpha_expr.strip())
                except ValueError:
                    alpha_part = f":alpha='{exit_alpha_expr}'"
            # 첫 청크 + enter_effect=fade이면 페이드인 alpha 적용 (typing+fade 동시 진입은 흔치 않지만 안전)
            if i == 0 and combined_enter_fade and enter_dur > 0:
                enter_alpha_expr = _build_fade_alpha_expr(
                    start_sec=start_sec,
                    end_sec=end_sec_raw,
                    enter_effect="fade",
                    exit_effect="none",
                    enter_dur=enter_dur,
                    exit_dur=0.0,
                    base_alpha=1.0,
                    visual_clip_duration=clip_duration,
                )
                try:
                    float(enter_alpha_expr.strip())
                except ValueError:
                    alpha_part = f":alpha='{enter_alpha_expr}'"
            # pulse 등 fontsize factor 적용 (typing 도중에도 폰트 호흡 가능).
            if loop_fontsize_factor != "1":
                fs_part = f"({font_size})*{loop_fontsize_factor}"
            else:
                fs_part = str(font_size)
            parts.append(
                f"drawtext=expansion=none:text='{substr_escaped}':x='{x_expr}':y='{y_expr}'"
                f":fontsize='{fs_part}':fontcolor={fc}"
                f":borderw={outline_w}:bordercolor={bc}"
                f"{font_part}{alpha_part}:enable='{enable_expr}'"
            )
        return parts

    # ── fade/zoom/none 단일 drawtext 처리 ──
    # base_alpha=1.0: fontcolor에 이미 opacity가 들어있으므로 alpha 표현식은 fade 비율(0~1)만 표현.
    # 그렇지 않으면 opacity가 fontcolor 알파 × alpha 표현식으로 2번 곱해져 절반 이하로 보임.
    alpha_expr = _build_fade_alpha_expr(
        start_sec=start_sec,
        end_sec=end_sec_raw,
        enter_effect=enter_effect,
        exit_effect=exit_effect,
        enter_dur=enter_dur,
        exit_dur=exit_dur,
        base_alpha=1.0,
        visual_clip_duration=clip_duration,
    )
    fontsize_expr = _build_zoom_fontsize_expr(
        start_sec=start_sec,
        end_sec=end_sec_raw,
        enter_effect=enter_effect,
        exit_effect=exit_effect,
        enter_dur=enter_dur,
        exit_dur=exit_dur,
        base_fontsize=font_size,
        visual_clip_duration=clip_duration,
    )
    # zoom이면 fontsize는 표현식, alpha는 fade 효과까지 합치는 게 자연스러움
    # → 진입 zoom 동안은 alpha도 0→1 페이드, 종료 zoom 동안은 alpha 1→0 페이드.
    use_alpha_for_zoom = enter_effect == "zoom" or exit_effect == "zoom"
    if use_alpha_for_zoom:
        # zoom용 alpha — enter zoom: 0→1, exit zoom: 1→0 (base_alpha=1.0)
        alpha_expr = _build_fade_alpha_expr(
            start_sec=start_sec,
            end_sec=end_sec_raw,
            enter_effect="fade" if enter_effect in ("fade", "zoom") else enter_effect,
            exit_effect="fade" if exit_effect in ("fade", "zoom") else exit_effect,
            enter_dur=enter_dur if enter_effect in ("fade", "zoom") else 0.0,
            exit_dur=exit_dur if exit_effect in ("fade", "zoom") else 0.0,
            base_alpha=1.0,
            visual_clip_duration=clip_duration,
        )

    # alpha 표현식이 상수면 alpha 인자 생략 (단순화 + 호환).
    alpha_clean = alpha_expr.strip()
    # 단순 상수("1.0000")인지 검사
    try:
        float(alpha_clean)
        alpha_part = ""
    except ValueError:
        alpha_part = f":alpha='{alpha_expr}'"

    # pulse 등 loop_fontsize_factor 가 '1' 이 아니면 fontsize 표현식에 곱한다.
    if loop_fontsize_factor != "1":
        fontsize_combined = f"({fontsize_expr})*{loop_fontsize_factor}"
    else:
        fontsize_combined = str(fontsize_expr)

    parts.append(
        f"drawtext=expansion=none:text='{text_escaped}':x='{x_expr}':y='{y_expr}'"
        f":fontsize='{fontsize_combined}':fontcolor={fc}"
        f":borderw={outline_w}:bordercolor={bc}"
        f"{font_part}{alpha_part}:enable='{visible_end}'"
    )
    return parts


# 복잡 텍스트(그라데이션/글로우/pulse) PNG 를 한 번의 ffmpeg 패스로 묶어 합성할지.
# False 면 예전처럼 PNG 1장마다 인코딩 1회(결과 비교·문제 시 되돌리기용 스위치).
_TEXT_OVERLAY_SINGLE_PASS = True
# 한 패스에 묶는 PNG 수 상한 — 명령줄 길이·필터 그래프 크기를 안전하게 유지.
_TEXT_OVERLAY_BATCH = 10


def apply_text_overlays(
    src_path: str,
    dst_path: str,
    overlays: list[dict],
    video_width: int,
    video_height: int,
    clip_duration: float,
) -> bool:
    """텍스트 오버레이를 적용한다. 단색 텍스트는 drawtext, 그라디언트/글로우는 Pillow PNG overlay 사용.

    성공 시 dst_path에 결과를 저장하고 True 반환. 실패 시 False 반환.

    #287 — 단순 텍스트는 enter/exit 효과(none/fade/typing/zoom)를 drawtext 표현식으로 처리.
    복잡(그라디언트/글로우) 텍스트는 PNG overlay에 alpha 표현식 fade만 적용 (typing/zoom은 fade 폴백).
    """
    import shutil

    # 카운트다운 오버레이는 `text` 가 비어 있어도 숫자를 그리므로 활성 대상에 포함한다.
    active_overlays = [
        ov for ov in overlays
        if ov.get("text") or int(ov.get("countdown_from", 0) or 0) > 0
    ]
    if not active_overlays:
        shutil.copy2(src_path, dst_path)
        return True

    # 그라디언트/글로우 여부로 처리 방법 분류
    simple_overlays = []
    complex_overlays = []
    for ov in active_overlays:
        gradient = ov.get("gradient")
        glow_radius = int(ov.get("glow_radius", 0))
        is_countdown = int(ov.get("countdown_from", 0) or 0) > 0
        # pulse 는 drawtext 의 `fontsize` 를 매 프레임 바뀌는 표현식으로 만드는데,
        # ffmpeg 8.x 에서 동적 fontsize 표현식이 인코딩 도중 segfault(-11) 를 낸다.
        # (정적 fontsize 는 정상. sin() 유무와 무관하게 "표현식"이면 크래시.)
        # PNG 합성 경로는 pulse 를 PNG 자체 scale 로 처리하므로 그쪽으로 보낸다.
        is_pulse = str(ov.get("loop_animation") or "none").lower() == "pulse"
        # 텍스트 마스킹 집중선은 글자 알파로 잘라낸 PNG 를 합성해야 하므로 drawtext 로는 불가.
        is_text_speedlines = str(ov.get("loop_animation") or "none").lower() == "speedlines"
        needs_complex = (
            (gradient and gradient.get("stops"))
            or glow_radius > 0
            or is_pulse
            or is_text_speedlines
        )
        if not needs_complex:
            simple_overlays.append(ov)
            continue
        if is_countdown:
            # 카운트다운은 시간대별로 텍스트가 바뀌어 PNG 1장으로 표현할 수 없다.
            # 숫자/종료 문구마다 텍스트가 고정된 오버레이로 펼쳐 각자 PNG 를 만들면
            # 그라디언트/글로우가 그대로 적용된다. (이전에는 통째로 drawtext 로 보내
            # 그라디언트/글로우가 조용히 무시되고 흰색 단색으로 렌더됐다.)
            complex_overlays.extend(_expand_countdown_overlay(ov, clip_duration))
        else:
            complex_overlays.append(ov)

    tmp_dir = tempfile.mkdtemp(prefix="vag_overlay_")
    try:
        current_src = src_path

        # 1단계: drawtext로 단색 텍스트 처리 (#287 진입/종료 효과 반영)
        if simple_overlays:
            vf_parts: list[str] = []
            for ov in simple_overlays:
                vf_parts.extend(_build_text_drawtext_parts(ov, clip_duration, video_height))

            if vf_parts:
                simple_dst = str(Path(tmp_dir) / "simple.mp4")
                vf = ",".join(vf_parts)
                r = subprocess.run(
                    [ffmpeg_bin(), "-y", "-i", current_src,
                     "-vf", vf,
                     *_video_encoder_args(60),
                     "-pix_fmt", "yuv420p",
                     "-c:a", "copy", simple_dst],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                if r.returncode == 0 and Path(simple_dst).exists():
                    current_src = simple_dst
                else:
                    # ⚠ 여기서 그냥 넘어가면 자막이 **통째로 사라진다**(경고문과 달리
                    #   "효과만 무시"되는 게 아니다). 실제로 zoom 진입 효과가 있는 씬에서
                    #   fontsize 가 시간 표현식이 되면 ffmpeg 이 text_h 를 평가하지 못해
                    #   필터 전체가 실패했고, 그 씬들의 자막이 전부 누락됐다(사용자 보고).
                    #   → 효과를 뺀 정적 표현식으로 한 번 더 시도해 자막만은 반드시 남긴다.
                    console.print(
                        f"  [yellow]drawtext 실패 → 진입/종료 효과 없이 재시도: {r.stderr[:160]}[/yellow]"
                    )
                    static_parts: list[str] = []
                    for ov in simple_overlays:
                        ov_static = dict(ov)
                        ov_static["enter_effect"] = "none"
                        ov_static["exit_effect"] = "none"
                        ov_static["loop_animation"] = "none"
                        static_parts.extend(
                            _build_text_drawtext_parts(ov_static, clip_duration, video_height)
                        )
                    if static_parts:
                        r2 = subprocess.run(
                            [ffmpeg_bin(), "-y", "-i", current_src,
                             "-vf", ",".join(static_parts),
                             *_video_encoder_args(60),
                             "-pix_fmt", "yuv420p",
                             "-c:a", "copy", simple_dst],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                        )
                        if r2.returncode == 0 and Path(simple_dst).exists():
                            current_src = simple_dst
                        else:
                            console.print(
                                f"  [red]drawtext 재시도도 실패 — 이 씬 자막이 누락됩니다: {r2.stderr[:160]}[/red]"
                            )

        # 텍스트 마스킹 집중선(loop_animation="speedlines")은 시간에 따라 선이 깜빡여야 한다.
        # 버킷별 PNG 를 만들어 각자의 짧은 enable 구간에 순환 합성하면 화면 전체 집중선과
        # 같은 "선별 랜덤 페이드" 느낌이 글자 안에서 재현된다.
        complex_overlays = _expand_text_speedlines_overlays(complex_overlays, clip_duration)

        # 2단계: Pillow PNG overlay로 복잡한 텍스트 처리 (#287 fade는 alpha 표현식)
        # 성능 — 예전엔 PNG 1장마다 씬 영상 전체를 디코드·재인코딩했다(텍스트 조각 N개 = 인코딩 N회).
        # 단어별 강조 스타일은 조각이 많아 텍스트 단계가 렌더의 1/3 을 차지했다(실측 281s/825s).
        # 이제 같은 사양을 모아 overlay 체인 한 번(조각 10개 단위)으로 합성한다. 실패하면 기존
        # 조각별 경로로 다시 실행해 결과를 보장한다.
        complex_specs: list[dict] = []
        for idx, ov in enumerate(complex_overlays):
            png_path = _render_overlay_png(
                ov, video_width, video_height, tmp_dir, idx,
                phase_bucket=ov.get("_speedlines_bucket"),
            )
            if not png_path:
                continue

            start_sec = float(ov.get("start_sec", 0.0))
            end_sec_raw = ov.get("end_sec")
            end_sec = float(end_sec_raw) if end_sec_raw is not None else clip_duration
            enable = (
                f"between(t,{start_sec:.3f},{end_sec:.3f})"
                if end_sec_raw is not None
                else f"gte(t,{start_sec:.3f})"
            )
            # #287 — 복잡 텍스트도 fade 가능: ffmpeg fade 필터로 PNG의 알파를 시간에 따라 변조.
            # typing/zoom은 PNG 한 장 기반 파이프라인에서 정확히 구현하기 까다로워 fade로 폴백.
            enter_effect, exit_effect, enter_dur, exit_dur = _get_overlay_effect_config(
                ov, text_supported=True
            )
            if enter_effect in ("typing", "zoom"):
                enter_effect = "fade"
            if exit_effect in ("typing", "zoom"):
                exit_effect = "fade"

            # 종료 시점 결정 (None이면 클립 끝)
            if end_sec is not None:
                end_eff = float(end_sec)
            elif clip_duration is not None and clip_duration > 0:
                end_eff = float(clip_duration)
            else:
                end_eff = 0.0  # 효과 없음

            fade_filters: list[str] = []
            if enter_effect == "fade" and enter_dur > 0:
                fade_filters.append(
                    f"fade=t=in:st={start_sec:.4f}:d={enter_dur:.4f}:alpha=1"
                )
            if exit_effect == "fade" and exit_dur > 0 and end_eff > start_sec:
                exit_start = end_eff - exit_dur
                fade_filters.append(
                    f"fade=t=out:st={exit_start:.4f}:d={exit_dur:.4f}:alpha=1"
                )

            # loop_animation — bounce/shake/wave 는 overlay x/y 시간 표현식, pulse 는 PNG 자체 scale.
            # mask 입력은 itsoffset 으로 영상 t=start_sec 에 시작하므로, mask 스트림의 PTS=0 가 영상 t=start_sec.
            # 따라서 mask 필터(scale, fade) 내부 표현식의 t 는 *마스크 기준 상대시간* — start_sec 보정 불필요.
            # overlay 의 t/W/H 는 *output 기준 시간/해상도* — start_sec 보정 필요.
            loop_anim = str(ov.get("loop_animation") or "none").lower()
            loop_amp = float(ov.get("loop_amplitude") or 0.02)
            loop_period = max(0.1, float(ov.get("loop_period_sec") or 0.8))
            ov_rel_t = f"(t-{start_sec:.4f})"  # overlay 의 t 는 영상 절대시간
            two_pi_over_T = f"(2*PI/{loop_period:.4f})"
            ox_expr = "0"
            oy_expr = "0"
            pulse_scale_filter = ""
            if loop_anim == "bounce":
                oy_expr = f"-abs(sin({ov_rel_t}*{two_pi_over_T}))*(H*{loop_amp:.4f})"
            elif loop_anim == "shake":
                fs_for_shake = int(ov.get("font_size") or 40)
                ox_expr = f"sin({ov_rel_t}*2*PI*12)*({fs_for_shake}*{loop_amp:.4f})"
            elif loop_anim == "wave":
                ox_expr = f"sin({ov_rel_t}*{two_pi_over_T})*(W*{loop_amp:.4f})"
            elif loop_anim == "pulse":
                # PNG 자체를 매 프레임 (1 + amp*sin) 배율로 리사이즈.
                # mask 필터 내부 t 는 PTS 기준이라 시작점이 0 → start_sec 보정 불필요.
                # scale 결과 크기가 변하므로 overlay x/y 를 (W-w)/2, (H-h)/2 로 두어 화면 중앙을 기준
                # 으로 균등 확대/축소. PNG 가 캔버스 전체 크기(video_w×video_h)였으므로 1.0 배율에서는
                # 정확히 가득 차고, 1.0+amp 까지만 키워도 약간 잘림 — amp 는 작게(<=0.1) 권장.
                # eval=frame 으로 매 프레임 재평가.
                factor_expr = f"(1+sin(t*{two_pi_over_T})*{loop_amp:.4f})"
                pulse_scale_filter = f",scale=w='iw*{factor_expr}':h='ih*{factor_expr}':eval=frame"
                ox_expr = "(W-w)/2"
                oy_expr = "(H-h)/2"
            # 관통(pass-through) — PNG 는 캔버스 전체 크기라 PNG 통째로 오프셋을 주면
            # 글자가 화면 밖에서 들어와 반대편으로 빠져나간다. loop_animation 오프셋 위에 누적.
            # speedlines 로 조각난 오버레이는 원본 궤적 정보를 _pass_through_* 에 보관한다.
            _pt_enter = str(
                ov.get("_pass_through_effect") or ov.get("enter_effect") or ""
            ).lower()
            if _pt_enter in _PASS_THROUGH_EFFECTS:
                _pt_ov = ov
                if ov.get("_pass_through_effect"):
                    # 궤적은 조각이 아니라 오버레이 전체 구간 기준으로 계산해야 한다.
                    _pt_ov = dict(ov)
                    _pt_ov["start_sec"] = ov.get("_pass_through_start", start_sec)
                    _pt_ov["end_sec"] = ov.get("_pass_through_end")
                    _pt_ov["enter_effect"] = _pt_enter
                    _pt_ov["exit_effect"] = _pt_enter
                ox_expr, oy_expr = _build_pass_through_exprs(
                    _pt_ov, ox_expr, oy_expr, clip_duration, _pt_enter
                )
                # overlay 필터의 화면 크기 변수는 대문자 W/H (drawtext 의 w/h 와 다름).
                ox_expr = ox_expr.replace("(w*1.1)", "(W*1.1)")
                oy_expr = oy_expr.replace("(h*1.1)", "(H*1.1)")
            overlay_xy = f"x='{ox_expr}':y='{oy_expr}'"

            # 단일 패스용 사양 수집 — 아래 반복 실행(기존 경로)과 완전히 같은 입력·필터 값.
            _in_dur = (
                (max(0.1, end_eff - start_sec) if end_eff > start_sec else 1.0)
                if fade_filters
                else (max(0.1, end_eff - start_sec) if end_eff > start_sec else (clip_duration or 60.0))
            )
            complex_specs.append({
                "png": png_path,
                "start": start_sec,
                "dur": _in_dur,
                "pre": "format=rgba" + (("," + ",".join(fade_filters)) if fade_filters else "") + pulse_scale_filter,
                "xy": overlay_xy,
                "enable": enable,
            })
            if _TEXT_OVERLAY_SINGLE_PASS:
                continue

            complex_dst = str(Path(tmp_dir) / f"complex_{idx:03d}.mp4")
            if fade_filters:
                # PNG → format=rgba → fade in/out alpha → overlay.
                # 단일 PNG 입력은 시간축이 없어 fade가 동작하지 않으므로 -loop 1 -t로 영상 스트림화.
                # ⚠ PNG 입력을 -itsoffset start_sec 으로 밀어 mask PTS를 영상 t=start_sec 에 맞춘다.
                # setpts=PTS-STARTPTS 를 쓰면 mask 가 0초부터 시작해 overlay 의 enable=between(t,start,end)
                # 시점에 mask 스트림은 이미 끝나 있어 텍스트가 안 보이는 회귀가 발생한다.
                oh_dur = max(0.1, end_eff - start_sec) if end_eff > start_sec else 1.0
                fc = (
                    f"[1:v]format=rgba,{','.join(fade_filters)}{pulse_scale_filter}[mask];"
                    f"[0:v][mask]overlay={overlay_xy}:enable='{enable}':shortest=0[v]"
                )
                r = subprocess.run(
                    [ffmpeg_bin(), "-y",
                     "-i", current_src,
                     "-itsoffset", f"{start_sec:.3f}",
                     "-loop", "1", "-t", f"{oh_dur:.3f}", "-i", png_path,
                     "-filter_complex", fc,
                     "-map", "[v]", "-map", "0:a?",
                     *_video_encoder_args(60),
                     "-pix_fmt", "yuv420p",
                     "-c:a", "copy", complex_dst],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
            else:
                # fade 없음 — PNG 한 장을 시간 무관하게 overlay. itsoffset 으로 PNG 시작 위치도 맞춰
                # 영상 끝까지 mask 가 살아 있게 한다(-loop 1, -t 없음). 이전엔 PNG 가 단일 프레임이라
                # PTS=0 이고 overlay 의 enable=between(t,start,end) 시점에 mask 가 만료되어 안 보였다.
                fc = (
                    f"[1:v]format=rgba{pulse_scale_filter}[mask];"
                    f"[0:v][mask]overlay={overlay_xy}:enable='{enable}':shortest=0[v]"
                )
                # -t 로 PNG 입력 길이 제한 — pulse 의 scale eval=frame 이 무한 입력에서 끝없이 돌 수 있어
                # 안전을 위해 명시적 종료 시점을 준다. end_eff 미정이면 영상 끝까지 충분히 늘림.
                _oh_dur_nofade = max(0.1, end_eff - start_sec) if end_eff > start_sec else (clip_duration or 60.0)
                r = subprocess.run(
                    [ffmpeg_bin(), "-y",
                     "-i", current_src,
                     "-itsoffset", f"{start_sec:.3f}",
                     "-loop", "1", "-t", f"{_oh_dur_nofade:.3f}", "-i", png_path,
                     "-filter_complex", fc,
                     "-map", "[v]", "-map", "0:a?",
                     *_video_encoder_args(60),
                     "-pix_fmt", "yuv420p",
                     "-c:a", "copy", complex_dst],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
            if r.returncode == 0 and Path(complex_dst).exists():
                current_src = complex_dst
            else:
                console.print(
                    f"  [yellow]복잡 텍스트 overlay 실패 (건너뜀): {r.stderr[:200]}[/yellow]"
                )

        if _TEXT_OVERLAY_SINGLE_PASS and complex_specs:
            _single_src = current_src
            ok_all = True
            for b0 in range(0, len(complex_specs), _TEXT_OVERLAY_BATCH):
                batch = complex_specs[b0:b0 + _TEXT_OVERLAY_BATCH]
                batch_dst = str(Path(tmp_dir) / f"complex_batch_{b0:03d}.mp4")
                cmd = [ffmpeg_bin(), "-y", "-i", _single_src]
                fc_parts: list[str] = []
                prev = "0:v"
                for j, sp in enumerate(batch, start=1):
                    cmd += ["-itsoffset", f"{sp['start']:.3f}", "-loop", "1",
                            "-t", f"{sp['dur']:.3f}", "-i", sp["png"]]
                    fc_parts.append(f"[{j}:v]{sp['pre']}[m{j}]")
                    out = "v" if j == len(batch) else f"o{j}"
                    fc_parts.append(
                        f"[{prev}][m{j}]overlay={sp['xy']}:enable='{sp['enable']}':shortest=0[{out}]"
                    )
                    prev = out
                cmd += ["-filter_complex", ";".join(fc_parts),
                        "-map", "[v]", "-map", "0:a?",
                        *_video_encoder_args(60),
                        "-pix_fmt", "yuv420p",
                        "-c:a", "copy", batch_dst]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if r.returncode == 0 and Path(batch_dst).exists() and Path(batch_dst).stat().st_size > 0:
                    _single_src = batch_dst
                else:
                    ok_all = False
                    console.print(
                        f"  [yellow]텍스트 단일 패스 합성 실패 → 조각별 합성으로 재시도: {r.stderr[-200:]}[/yellow]"
                    )
                    break
            if ok_all:
                current_src = _single_src
            else:
                # 폴백 — 기존 조각별 경로(PNG 1장 = 인코딩 1회)로 다시 합성.
                for k, sp in enumerate(complex_specs):
                    fb_dst = str(Path(tmp_dir) / f"complex_fb_{k:03d}.mp4")
                    fc = (f"[1:v]{sp['pre']}[mask];"
                          f"[0:v][mask]overlay={sp['xy']}:enable='{sp['enable']}':shortest=0[v]")
                    r = subprocess.run(
                        [ffmpeg_bin(), "-y", "-i", current_src,
                         "-itsoffset", f"{sp['start']:.3f}", "-loop", "1", "-t", f"{sp['dur']:.3f}",
                         "-i", sp["png"], "-filter_complex", fc,
                         "-map", "[v]", "-map", "0:a?", *_video_encoder_args(60),
                         "-pix_fmt", "yuv420p", "-c:a", "copy", fb_dst],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                    )
                    if r.returncode == 0 and Path(fb_dst).exists():
                        current_src = fb_dst
                    else:
                        console.print(
                            f"  [yellow]복잡 텍스트 overlay 실패 (건너뜀): {r.stderr[:200]}[/yellow]"
                        )

        shutil.copy2(current_src, dst_path)
        return True

    except Exception as e:
        console.print(f"  [yellow]텍스트 오버레이 적용 실패 (건너뜀): {e}[/yellow]")
        return False
    finally:
        import shutil as _sh
        _sh.rmtree(tmp_dir, ignore_errors=True)


console = Console()

CLIPS_DIR = "output/clips"
MUTED_OUTPUT = "output/muted_output.mp4"
TIMELINE_PATH = "cache/timeline.json"
DISSOLVE_SEC = 0.5  # 디졸브 길이(초)
# 씬 시작 후 디졸브가 완전히 끝난 시점 + 여유 0.1s에 나레이션 시작
# 첫 씬은 앞 디졸브가 없지만 동일 패딩 적용 (시작부터 음성이 바로 나오는 것 방지)
NARRATION_PADDING = DISSOLVE_SEC + 0.1  # 0.6s


def _xfade_chunk(clip_paths: list[str], dst: str, dissolve_sec: float) -> bool:
    """clip_paths를 xfade로 연결하여 dst에 저장. 성공 시 True 반환.

    전체 리인코딩 방식(filter_complex xfade)을 사용한다.
    -c copy 최적화는 keyframe 경계 불일치로 인해 실제 컷 위치가 요청 위치와 어긋나고,
    그 결과 _calc_start_times 계산과 실제 muted_output PTS가 불일치하여
    오디오-비디오 싱크 오류가 발생한다.
    """
    import shutil

    durations = [get_duration(p) for p in clip_paths]
    effective_dissolve = dissolve_sec
    min_clip_dur = min(durations)
    if effective_dissolve >= min_clip_dur / 2.0:
        effective_dissolve = min_clip_dur / 4.0

    # 클립이 1개면 그냥 복사
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], dst)
        return True

    return _xfade_chunk_fallback(clip_paths, dst, effective_dissolve, durations)


def _xfade_chunk_fallback(
    clip_paths: list[str], dst: str, effective_dissolve: float, durations: list[float]
) -> bool:
    """xfade 최적화 실패 시 기존 방식(전체 리인코딩)으로 폴백."""
    inputs_args: list[str] = []
    for p in clip_paths:
        inputs_args += ["-i", p]

    filter_parts: list[str] = []
    accumulated_offset = 0.0
    prev_label = "0:v"
    for i in range(1, len(clip_paths)):
        accumulated_offset += durations[i - 1] - effective_dissolve
        offset = max(0.001, accumulated_offset)
        next_label = f"v{i:02d}"
        filter_parts.append(
            f"[{prev_label}][{i}:v]"
            f"xfade=transition=fade:duration={effective_dissolve:.3f}"
            f":offset={offset:.3f}[{next_label}]"
        )
        prev_label = next_label

    filter_complex = ";".join(filter_parts)
    cmd = [
        ffmpeg_bin(), "-y",
        *inputs_args,
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        *_video_encoder_args(60),
        "-pix_fmt", "yuv420p",
        "-an", dst,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.returncode == 0


def _calc_start_times(durations: list[float], dissolve_sec: float) -> list[float]:
    """클립 길이 목록과 dissolve 길이로 각 클립의 시작 시각을 계산한다."""
    starts = []
    cursor = 0.0
    for i, d in enumerate(durations):
        if i > 0:
            cursor -= dissolve_sec
        starts.append(round(cursor, 3))
        cursor += d
    return starts


def _xfade_parallel_workers(n_chunks: int) -> int:
    """#416 — 청크 xfade 동시 실행 수. 환경변수 RENDER_XFADE_WORKERS 로 조정(1 = 기존 순차).

    기본 3: 청크 하나가 디코더 8개를 동시에 열고, 소비자용 GPU 인코더는 동시 세션 수가
    제한되므로 코어 수만큼 늘리지 않는다.
    """
    import os as _os
    try:
        w = int(_os.environ.get("RENDER_XFADE_WORKERS", "3"))
    except ValueError:
        w = 3
    cpu = _os.cpu_count() or 1
    return max(1, min(w, n_chunks, cpu))


# ── #416 (1안) 디졸브 경계 구간만 재인코딩 ────────────────────────────────────
# 2안(전체 xfade)은 영상 전체를 두 번 재인코딩한다. 1안은 각 클립의 가운데를 키프레임
# 경계에서 잘라 `-c copy` 로 가져오고, 경계(앞 클립 끝 + 뒤 클립 처음)만 xfade 로 새로
# 인코딩한 뒤 concat demuxer 로 잇는다.
#
# 과거 -c copy 시도가 싱크를 깨뜨린 원인(키프레임이 아닌 곳에서 자름)을 막기 위해:
#   - 자르는 지점은 ffprobe 로 읽은 실제 키프레임(디코드 순서 패킷)만 쓴다. 추정 없음.
#   - 경계 조각의 xfade offset 은 2안 경로가 쓰는 offset(소수 3자리 반올림, 8개 묶음 병합
#     포함)을 프레임 단위로 재현해 조각 기준으로 옮긴다 → 디졸브 시작 프레임이 같다.
#   - 복사 조각은 원본 패킷 크기 순서까지 대조하고, 경계 조각·최종 결과는 프레임 수와
#     스트림 서명(SPS/PPS 해시·해상도·SAR·timebase·pix_fmt)을 대조한다.
#   - 하나라도 어긋나면 False → 호출부가 2안 경로로 폴백한다.
_XFADE_CHUNK_SIZE = 8          # 2안 경로의 묶음 크기 (1안 프레임 배치 재현도 이 값을 쓴다)
_BD_FPS = 30                   # 클립은 30fps CFR (#xfade-sparse-fps, #xfade-vfr-segment)
_BD_MARGIN_FRAMES = 2          # 디졸브 구간과 자르는 키프레임 사이 최소 여유 프레임
_BD_MAX_PIECE_INPUTS = 24      # 경계 조각 하나에 들어갈 최대 입력 수(명령줄 길이 보호)
_BD_SIG_FIELDS = (
    "codec_name", "profile", "level", "width", "height", "pix_fmt",
    "sample_aspect_ratio", "r_frame_rate", "time_base", "has_b_frames",
    "field_order", "color_range", "color_space", "color_transfer",
    "color_primaries", "extradata_hash",
)


def _dissolve_mode() -> str:
    """RENDER_DISSOLVE_MODE: boundary(기본, 1안) | full(2안 전체 xfade)."""
    import os as _os
    m = (_os.environ.get("RENDER_DISSOLVE_MODE") or "boundary").strip().lower()
    return "full" if m == "full" else "boundary"


def _bd_probe(path: str) -> "dict | None":
    """스트림 서명 + 디코드 순서 패킷 목록[(프레임번호, 키프레임여부, 크기)].

    30fps CFR 이고 프레임 번호가 0..n-1 로 빈틈없이 이어질 때만 'frames' 를 채운다.
    """
    import json as _json
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_data_hash", "sha256",
         "-show_entries", "stream=" + ",".join(_BD_SIG_FIELDS) + ":packet=pts_time,flags,size",
         "-of", "json", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return None
    try:
        data = _json.loads(r.stdout)
    except ValueError:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    st = streams[0]
    sig = tuple(str(st.get(k, "")) for k in _BD_SIG_FIELDS)
    order: list[tuple[int, bool, int]] = []
    for p in data.get("packets") or []:
        try:
            pts = float(p["pts_time"])
            size = int(p["size"])
        except (KeyError, TypeError, ValueError):
            return {"sig": sig, "frames": None}
        idx = round(pts * _BD_FPS)
        if abs(pts * _BD_FPS - idx) > 0.01:
            return {"sig": sig, "frames": None}
        order.append((idx, "K" in str(p.get("flags", "")), size))
    if not order or sorted(i for i, _k, _s in order) != list(range(len(order))):
        return {"sig": sig, "frames": None}
    return {
        "sig": sig,
        "frames": len(order),
        "order": order,
        "keys": sorted(i for i, k, _s in order if k),
    }


def _bd_chain(lens_sec: list[float], lens_frames: list[int], d: float):
    """_xfade_chunk_fallback 의 xfade 체인을 프레임 단위로 재현한다.

    xfade 는 첫 입력 프레임 pts 가 offset 보다 작으면 그대로 내보내고, 그 다음 프레임부터
    둘째 입력과 섞는다. 둘째 입력의 j번째 프레임은 출력 (전환 시작 프레임 + j) 에 놓인다.
    반환: (offsets[초, 반올림 그대로], 각 입력 0번 프레임의 출력 위치, 출력 총 프레임 수).
    """
    import math
    acc = 0.0
    offs: list = [None]
    starts = [0]
    total = lens_frames[0]
    for i in range(1, len(lens_sec)):
        acc += lens_sec[i - 1] - d
        o = float(f"{max(0.001, acc):.3f}")
        f = max(0, math.ceil(o * _BD_FPS - 1e-4))
        if f > total:
            return None
        offs.append(o)
        starts.append(f)
        total = f + lens_frames[i]
    return offs, starts, total


def _bd_layout(durations: list[float], frames: list[int], d: float):
    """2안(_concat_with_dissolve) 결과의 프레임 배치를 재현한다.

    반환: (G: 각 클립 0번 프레임의 최종 위치, trans: 클립 k 앞 전환의 (offset, 기준프레임),
    total: 최종 프레임 수). 전환 k 의 전역 판정 시각 = offset + 기준프레임/30.
    """
    n = len(durations)
    if n <= _XFADE_CHUNK_SIZE:
        res = _bd_chain(durations, frames, d)
        if res is None:
            return None
        offs, starts, total = res
        return starts, [None] + [(offs[k], 0) for k in range(1, n)], total
    ranges = [(i, min(i + _XFADE_CHUNK_SIZE, n)) for i in range(0, n, _XFADE_CHUNK_SIZE)]
    c_res = []
    m_secs: list[float] = []
    m_frames: list[int] = []
    for i0, i1 in ranges:
        if i1 - i0 == 1:
            c_res.append(([None], [0], frames[i0]))
            m_secs.append(durations[i0])          # 1개 묶음은 원본 복사 → 같은 길이
        else:
            res = _bd_chain(durations[i0:i1], frames[i0:i1], d)
            if res is None:
                return None
            c_res.append(res)
            m_secs.append(float(f"{res[2] / _BD_FPS:.6f}"))  # get_duration(묶음 결과)
        m_frames.append(c_res[-1][2])
    m = _bd_chain(m_secs, m_frames, d)
    if m is None:
        return None
    m_offs, m_starts, total = m
    G: list[int] = []
    trans: list = [None] * n
    for c, (i0, i1) in enumerate(ranges):
        gc = m_starts[c]
        offs, starts, _t = c_res[c]
        for j in range(i0, i1):
            G.append(gc + starts[j - i0])
            if j > i0:
                trans[j] = (offs[j - i0], gc)
        if c > 0:
            trans[i0] = (m_offs[c], 0)
    return G, trans, total


def _concat_with_dissolve_boundary(
    clip_paths: list[str], durations: list[float], dst: str, d: float
) -> bool:
    """#416 1안. 성공 시 dst 를 쓰고 True. 조건이 안 맞거나 검사가 어긋나면 False(dst 미변경)."""
    import math
    import shutil
    import tempfile
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    from .utils.ffmpeg_utils import CUT_CLIP_VIDEO_ARGS

    n = len(clip_paths)
    if n < 2:
        return False
    _t_start = _time.perf_counter()
    probes = [_bd_probe(p) for p in clip_paths]
    if any(p is None or p["frames"] is None for p in probes):
        return False
    frames = [p["frames"] for p in probes]
    layout = _bd_layout(durations, frames, d)
    if layout is None:
        return False
    G, trans, total = layout

    d_f = math.ceil(d * _BD_FPS - 1e-6)
    head_min = d_f + _BD_MARGIN_FRAMES

    def _middle(i: int) -> "tuple[int, int] | None":
        p = probes[i]
        nf = p["frames"]
        if abs(durations[i] * _BD_FPS - nf) > 0.05:
            return None
        keys = p["keys"]
        if i == 0:
            h = 0 if keys and keys[0] == 0 else None
        else:
            h = next((k for k in keys if k >= head_min), None)
        if i == n - 1:
            t = nf
        else:
            cands = [k for k in keys if k <= nf - d_f - _BD_MARGIN_FRAMES]
            t = cands[-1] if cands else None
        if h is None or t is None or h >= t:
            return None
        return h, t

    # 복사 기준 서명: yuv420p·30fps·h264 중 복사 가능한 프레임이 가장 많은 것.
    weights: dict = {}
    for i, p in enumerate(probes):
        sig = p["sig"]
        if sig[0] != "h264" or sig[5] != "yuv420p" or sig[7] != f"{_BD_FPS}/1":
            continue
        mid = _middle(i)
        if mid:
            weights[sig] = weights.get(sig, 0) + (mid[1] - mid[0])
    if not weights:
        return False

    tmp = Path(tempfile.mkdtemp(prefix="video_agent_bd_"))
    try:
        # 경계 조각을 복사 구간과 같은 SPS/PPS 로 인코딩할 인코더 인자를 고른다.
        cand_args: list[list[str]] = []
        for a in (["-r", str(_BD_FPS), *CUT_CLIP_VIDEO_ARGS],
                  _video_encoder_args(60), _video_encoder_args(50)):
            if a not in cand_args:
                cand_args.append(a)
        ref_sig = None
        enc_args: "list[str] | None" = None
        for sig, _w in sorted(weights.items(), key=lambda kv: -kv[1]):
            ref_i = next(i for i, p in enumerate(probes) if p["sig"] == sig)
            for k, a in enumerate(cand_args):
                probe_out = str(tmp / f"enc_probe_{k}.mp4")
                r = subprocess.run(
                    [ffmpeg_bin(), "-y", "-v", "error", "-i", clip_paths[ref_i],
                     "-map", "0:v:0", "-frames:v", "8", *a, "-pix_fmt", "yuv420p", "-an", probe_out],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                pr = _bd_probe(probe_out) if r.returncode == 0 else None
                if pr is not None and pr["sig"] == sig:
                    ref_sig, enc_args = sig, a
                    break
            if enc_args is not None:
                break
        if enc_args is None:
            return False

        mids = {i: m for i in range(n)
                if probes[i]["sig"] == ref_sig and (m := _middle(i)) is not None}
        if not mids:
            return False

        # 조각 목록: ("copy", i, h, t) / ("piece", inputs[(i, 시작프레임, 프레임수)], P)
        segs: list = []
        cur = None
        for i in range(n):
            nf = frames[i]
            if i in mids:
                h, t = mids[i]
                if i > 0:
                    cur["inputs"].append((i, 0, h))
                    segs.append(("piece", cur["inputs"], cur["P"]))
                    cur = None
                segs.append(("copy", i, h, t))
                if i < n - 1:
                    cur = {"inputs": [(i, t, nf - t)], "P": G[i] + t}
            elif cur is None:
                cur = {"inputs": [(i, 0, nf)], "P": G[i]}
            else:
                cur["inputs"].append((i, 0, nf))
        if cur is not None:
            segs.append(("piece", cur["inputs"], cur["P"]))

        jobs: list = []
        pos = 0
        for k, seg in enumerate(segs):
            if seg[0] == "copy":
                _c, i, h, t = seg
                if G[i] + h != pos:
                    return False
                jobs.append(("copy", k, i, h, t))
                pos += t - h
            else:
                _p, inputs, P = seg
                if P != pos or not (2 <= len(inputs) <= _BD_MAX_PIECE_INPUTS):
                    return False
                last_i, _s, last_c = inputs[-1]
                expect = G[last_i] + last_c - P
                jobs.append(("piece", k, inputs, P, expect))
                pos += expect
        if pos != total:
            return False

        def _run_copy(job) -> "dict | None":
            _c, k, i, h, t = job
            out = str(tmp / f"seg_{k:04d}.mp4")
            cmd = [ffmpeg_bin(), "-y", "-v", "error"]
            if h > 0:
                cmd += ["-ss", f"{h / _BD_FPS:.6f}"]
            cmd += ["-i", clip_paths[i], "-map", "0:v:0", "-c", "copy", "-an",
                    "-frames:v", str(t - h), out]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return None
            pr = _bd_probe(out)
            if pr is None or pr["frames"] != t - h or pr["sig"] != ref_sig:
                return None
            # 원본의 디코드 순서 패킷과 1:1 대조 — 다른 키프레임에서 잘렸거나 GOP 가
            # 열려 있으면(키프레임 앞 프레임을 참조) 여기서 걸린다.
            order = probes[i]["order"]
            p0 = next((q for q, (idx, key, _s) in enumerate(order) if idx == h and key), None)
            if p0 is None:
                return None
            src = order[p0:p0 + (t - h)]
            if sorted(x[0] for x in src) != list(range(h, t)):
                return None
            if [x[2] for x in src] != [x[2] for x in pr["order"]]:
                return None
            return {"path": out, "probe": pr}

        def _run_piece(job) -> "dict | None":
            _p, k, inputs, P, expect = job
            out = str(tmp / f"seg_{k:04d}.mp4")
            cmd = [ffmpeg_bin(), "-y", "-v", "error"]
            for i, s, c in inputs:
                if s > 0:
                    cmd += ["-ss", f"{s / _BD_FPS:.6f}"]
                if s + c < frames[i]:
                    cmd += ["-t", f"{c / _BD_FPS:.6f}"]
                cmd += ["-i", clip_paths[i]]
            parts: list[str] = []
            prev = "0:v"
            for j in range(1, len(inputs)):
                o, base = trans[inputs[j][0]]
                lo = o + (base - P) / _BD_FPS   # 2안의 전역 판정 시각을 조각 기준으로 옮김
                if lo <= 0:
                    return None
                nxt = f"x{j:02d}"
                parts.append(f"[{prev}][{j}:v]xfade=transition=fade:duration={d:.3f}"
                             f":offset={lo:.6f}[{nxt}]")
                prev = nxt
            # 색 정보 태그(VUI)는 입력 프레임에서 인코더로 넘어가 SPS 를 바꾼다. 서명이 다른
            # 클립이 조각에 섞여도 복사 구간과 SPS 가 같도록 기준 값으로 고정한다(화소 불변).
            parts.append(
                f"[{prev}]setparams=range={ref_sig[11] or 'unknown'}"
                f":colorspace={ref_sig[12] or 'unknown'}"
                f":color_trc={ref_sig[13] or 'unknown'}"
                f":color_primaries={ref_sig[14] or 'unknown'}[vout]"
            )
            prev = "vout"
            cmd += ["-filter_complex", ";".join(parts), "-map", f"[{prev}]",
                    *enc_args, "-pix_fmt", "yuv420p", "-an", out]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return None
            pr = _bd_probe(out)
            if pr is None or pr["frames"] != expect or pr["sig"] != ref_sig:
                return None
            if not pr["order"][0][1]:
                return None
            return {"path": out, "probe": pr}

        def _run(job):
            return _run_copy(job) if job[0] == "copy" else _run_piece(job)

        n_pieces = sum(1 for j in jobs if j[0] == "piece")
        workers = _xfade_parallel_workers(max(1, n_pieces))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, jobs))
        else:
            results = [_run(j) for j in jobs]
        if any(res is None for res in results):
            return False

        list_file = tmp / "concat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for res in results:
                f.write("file '" + Path(res["path"]).resolve().as_posix().replace("'", "'\\''") + "'\n")
                # 조각 길이를 프레임 수로 못 박는다. 생략하면 B 프레임 편집 목록 때문에 concat 이
                # 파일 길이를 조금 다르게 읽어 뒤 조각 전체가 1프레임 미만씩 밀린다(실측 -0.65ms).
                f.write(f"duration {res['probe']['frames'] / _BD_FPS:.6f}\n")
        out_tmp = str(tmp / "joined.mp4")
        # µs 반올림 누적으로 생기는 틱 단위 오차는 프레임 격자로 다시 맞춘다.
        _snap = f"round(PTS*TB*{_BD_FPS})/(TB*{_BD_FPS})"
        _snap_d = f"round(DTS*TB*{_BD_FPS})/(TB*{_BD_FPS})"
        r = subprocess.run(
            [ffmpeg_bin(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-map", "0:v:0", "-c", "copy", "-an",
             "-bsf:v", f"setts=pts={_snap}:dts={_snap_d}", out_tmp],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return False
        pr = _bd_probe(out_tmp)
        if pr is None or pr["frames"] != total or pr["sig"] != ref_sig:
            return False
        # 이어붙인 결과의 패킷이 조각 패킷과 순서·크기까지 같은지 대조한다. 단, 키프레임
        # 패킷에는 concat 이 같은 SPS/PPS 를 인밴드로 덧붙이므로(실측 +38B) 키프레임에서만,
        # 그리고 전체에서 같은 크기만큼 늘어난 경우만 허용한다.
        joined = pr["order"]
        q = 0
        grow: set[int] = set()
        for res in results:
            seg_order = res["probe"]["order"]
            part = joined[q:q + len(seg_order)]
            if len(part) != len(seg_order):
                return False
            for (j_idx, _jk, j_size), (s_idx, s_key, s_size) in zip(part, seg_order):
                if j_idx - part[0][0] != s_idx:
                    return False
                if j_size != s_size:
                    if not s_key:
                        return False
                    grow.add(j_size - s_size)
            q += len(seg_order)
        if q != len(joined) or len(grow) > 1 or (grow and min(grow) < 0):
            return False

        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(out_tmp, dst)
        enc_frames = sum(j[4] for j in jobs if j[0] == "piece")
        console.print(
            f"  [dim]경계 재인코딩(1안): 경계 조각 {n_pieces}개 · 복사 {len(jobs) - n_pieces}개, "
            f"재인코딩 {enc_frames / _BD_FPS:.1f}s / 전체 {total / _BD_FPS:.1f}s, "
            f"{_time.perf_counter() - _t_start:.1f}s[/dim]"
        )
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _concat_with_dissolve(clip_paths: list[str], dst: str, dissolve_sec: float) -> list[float]:
    """FFmpeg xfade 필터를 사용하여 클립들을 dissolve 전환으로 연결한다.

    클립 수가 많으면 OS 명령어 길이 한계(ARG_MAX)를 초과하므로
    CHUNK_SIZE개씩 나눠 xfade 처리 후 결과물을 다시 xfade로 합친다.

    muted_output은 무음이므로 오디오는 단순 concat (acrossfade 미적용).

    Returns:
        각 클립의 실제 시작 시각 리스트 (출력 영상 기준)
    """
    import shutil
    import tempfile

    CHUNK_SIZE = _XFADE_CHUNK_SIZE  # 한 번에 xfade로 처리할 최대 클립 수

    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], dst)
        return [0.0]

    # 각 클립의 길이 측정
    durations = [get_duration(p) for p in clip_paths]
    effective_dissolve = dissolve_sec
    min_clip_dur = min(durations)
    if effective_dissolve >= min_clip_dur / 2.0:
        effective_dissolve = min_clip_dur / 4.0

    if effective_dissolve <= 0:
        _simple_concat(clip_paths, dst)
        return _calc_start_times(durations, 0.0)

    # #416 (1안) — 경계 구간만 재인코딩하고 나머지는 스트림 복사. 결과(프레임 배치)는
    # 아래 2안 경로와 같도록 맞추고, 어떤 검사라도 어긋나면 2안 경로로 자동 폴백한다.
    # RENDER_DISSOLVE_MODE=full 이면 1안을 건너뛰고 2안만 쓴다.
    if _dissolve_mode() == "boundary":
        try:
            _bd_ok = _concat_with_dissolve_boundary(clip_paths, durations, dst, effective_dissolve)
        except Exception as e:  # 예기치 못한 오류도 2안으로 되돌린다.
            console.print(f"  [yellow]경계 재인코딩 오류: {e}[/yellow]")
            _bd_ok = False
        if _bd_ok:
            return _calc_start_times(durations, effective_dissolve)
        console.print("  [yellow]경계 재인코딩 불가 — 전체 xfade(2안)로 진행[/yellow]")

    # 클립을 CHUNK_SIZE개씩 단순 분할하여 각각 xfade 처리 후 결과물을 다시 xfade로 합침
    chunks: list[list[str]] = [
        clip_paths[i:i + CHUNK_SIZE]
        for i in range(0, len(clip_paths), CHUNK_SIZE)
    ]
    chunk_durations: list[list[float]] = [
        durations[i:i + CHUNK_SIZE]
        for i in range(0, len(durations), CHUNK_SIZE)
    ]

    if len(chunks) == 1:
        if not _xfade_chunk(chunks[0], dst, effective_dissolve):
            console.print("  [yellow]xfade 적용 실패 — 단순 concat으로 폴백[/yellow]")
            _simple_concat(clip_paths, dst)
            return _calc_start_times(durations, 0.0)
        return _calc_start_times(durations, effective_dissolve)

    tmp_files: list[str] = []
    try:
        for idx, chunk in enumerate(chunks):
            tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, prefix=f"xfade_chunk_{idx}_")
            tmp_path = tmp.name
            tmp.close()
            tmp_files.append(tmp_path)

        # #416 (2안) — 청크별 xfade 는 서로 독립이므로 병렬로 돌린다.
        # 각 청크에 들어가는 ffmpeg 명령(입력·filter_complex·인코더 인자)은 순차 실행 때와
        # 완전히 같고 실행 시점만 겹치므로 결과 파일·시작 시각 계산은 바뀌지 않는다.
        # 하드웨어 인코더 동시 세션 한도 등으로 병렬 실행이 실패한 청크는 순차로 한 번 더
        # 시도한 뒤에만 기존 폴백(단순 concat)으로 간다 — 병렬화 때문에 결과가 달라지지 않게.
        _workers = _xfade_parallel_workers(len(chunks))
        if _workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=_workers) as _ex:
                _oks = list(_ex.map(
                    lambda a: _xfade_chunk(a[0], a[1], effective_dissolve),
                    zip(chunks, tmp_files),
                ))
        else:
            _oks = [False] * len(chunks)
        for chunk, tmp_path, ok in zip(chunks, tmp_files, _oks):
            if ok:
                continue
            if not _xfade_chunk(chunk, tmp_path, effective_dissolve):
                console.print("  [yellow]xfade 청크 처리 실패 — 단순 concat으로 폴백[/yellow]")
                _simple_concat(clip_paths, dst)
                return _calc_start_times(durations, 0.0)

        if not _xfade_chunk(tmp_files, dst, effective_dissolve):
            console.print("  [yellow]xfade 병합 실패 — 단순 concat으로 폴백[/yellow]")
            _simple_concat(clip_paths, dst)
            return _calc_start_times(durations, 0.0)

        # 실제 시작 시각 계산:
        # 청크 내부: effective_dissolve씩 겹침
        # 청크 간 경계: 청크 결과물끼리 xfade 시 effective_dissolve씩 추가 겹침
        start_times: list[float] = []
        cursor = 0.0
        for c_idx, (chunk, c_durs) in enumerate(zip(chunks, chunk_durations)):
            if c_idx > 0:
                # 청크 결과물 간 xfade 겹침
                cursor -= effective_dissolve
            for i, d in enumerate(c_durs):
                if i > 0:
                    cursor -= effective_dissolve
                start_times.append(round(cursor, 3))
                cursor += d
        return start_times

    finally:
        for f in tmp_files:
            try:
                Path(f).unlink()
            except OSError:
                pass


def _concat_with_dissolve_group_aware(
    clip_paths: list[str],
    clip_scene_ids: list[int],
    scene_group_ids: dict[int, int],
    dst: str,
    dissolve_sec: float,
) -> list[float]:
    """그룹 경계에만 fade in/out을 적용하여 클립을 연결한다.

    같은 그룹 내 인접 씬 경계에서는 디졸브를 생략하고 단순 concat한다.
    그룹이 달라지는 경계에서만 각 클립에 fade in/out을 적용한다.

    구현 전략:
    1. clip_paths를 "세그먼트" 단위로 분리: 같은 그룹끼리 묶는다.
    2. 각 세그먼트 내 클립들은 fade 없이 단순 concat → 임시 파일.
    3. 세그먼트 임시 파일들을 _concat_with_dissolve로 연결 (그룹 간 fade 적용).
    """
    import tempfile
    import shutil

    if not clip_paths:
        return []

    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    # 세그먼트 분리
    segments: list[list[str]] = []
    cur_seg: list[str] = []
    prev_gid: int | None = None

    for path, sid in zip(clip_paths, clip_scene_ids):
        gid = scene_group_ids.get(sid, sid)
        if gid != prev_gid and cur_seg:
            segments.append(cur_seg)
            cur_seg = []
        cur_seg.append(path)
        prev_gid = gid

    if cur_seg:
        segments.append(cur_seg)

    durations = [get_duration(p) for p in clip_paths]

    if len(segments) == 1 and len(segments[0]) == len(clip_paths):
        # 모든 클립이 같은 그룹 → dissolve 없이 단순 concat
        _simple_concat(clip_paths, dst)
        return _calc_start_times(durations, 0.0)

    tmp_root = Path(tempfile.mkdtemp(prefix="video_agent_seg_"))
    try:
        seg_paths: list[str] = []
        for k, seg in enumerate(segments):
            seg_dst = str(tmp_root / f"seg_{k:03d}.mp4")
            if len(seg) == 1:
                import shutil as _sh
                _sh.copy2(seg[0], seg_dst)
            else:
                _simple_concat(seg, seg_dst)
            seg_paths.append(seg_dst)

        if len(seg_paths) == 1:
            import shutil as _sh
            _sh.copy2(seg_paths[0], dst)
            return _calc_start_times(durations, 0.0)
        else:
            start_times_seg = _concat_with_dissolve(seg_paths, dst, dissolve_sec)
            # seg_paths 기준 start_times를 원본 clip_paths 기준으로 변환
            # 각 세그먼트 내 클립들은 dissolve 없이 이어붙였으므로 세그먼트 시작 시각 + 누적 길이
            result: list[float] = []
            seg_idx = 0
            clip_idx = 0
            for seg in segments:
                seg_start = start_times_seg[seg_idx]
                seg_cursor = 0.0
                for p in seg:
                    result.append(round(seg_start + seg_cursor, 3))
                    seg_cursor += durations[clip_idx]
                    clip_idx += 1
                seg_idx += 1
            return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _simple_concat(clip_paths: list[str], dst: str) -> None:
    """fade 없이 클립들을 단순 concat demuxer로 연결한다."""
    import tempfile

    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    if len(clip_paths) == 1:
        # #xfade-vfr-segment — 1개여도 그냥 복사하면 안 된다.
        # 슬로모션으로 늘린 단독 클립(예: 8fps)이 그대로 세그먼트가 되면
        # 그 세그먼트가 낀 xfade 에서 뒤쪽 입력이 사라진다. 여기서도 30fps 로 고정.
        src = clip_paths[0]
        r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate", "-of", "csv=p=0", src],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.stdout.strip() == "30/1":
            import shutil
            shutil.copy2(src, dst)
            return
        subprocess.run(
            [ffmpeg_bin(), "-y", "-v", "error", "-i", src,
             "-vf", "fps=30", "-r", "30",
             *_video_encoder_args(60), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", dst],
            check=True,
        )
        return

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{Path(p).resolve()}'\n")
        list_file = f.name

    try:
        # #xfade-vfr-segment — `-c copy` 로 이어붙이면 입력들의 프레임 간격이
        # 그대로 남아, 결과 세그먼트가 가변 프레임레이트(VFR)가 된다.
        #
        # 짧은 소재를 슬로모션으로 늘린 클립(프레임이 듬성듬성)이 섞이면 세그먼트
        # avg_frame_rate 가 21.3fps 같은 값이 되고, 그 세그먼트를 xfade 에 넣으면
        # 뒤쪽 입력이 조용히 사라진다(실측: 8.87+21.17 → 13.70초, 최종 영상이
        # 7분44초에서 잘림). ffmpeg 는 오류도 내지 않는다.
        #
        # 재인코딩 비용을 치르더라도 30fps CFR 로 고정해 xfade 가 항상 성립하게 한다.
        cmd = [
            ffmpeg_bin(), "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-vf", "fps=30",
            "-r", "30",
            *_video_encoder_args(60),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            dst,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg simple concat 실패: {result.stderr}")
    finally:
        Path(list_file).unlink(missing_ok=True)


def _extract_src_audio(
    source_file: str,
    trim_start: float,
    trim_end: float,
    scene_id: int,
    target_duration: float | None = None,
    footage_speed: float = 1.0,
    footage_speed_mode: str = "constant",
    ramp_hold: float = 0.2,
    ramp_accel: float = 0.3,
    ramp_base: float = 1.0,
) -> str | None:
    """원본 영상에서 현장음을 output/clips/scene_NNN_src.wav로 추출한다.

    원본 오디오가 없거나 추출에 실패하면 None을 반환한다.
    target_duration이 지정된 경우 atempo 필터로 오디오도 동일 비율로 속도 조정한다.
    footage_speed가 1.0이 아니면 영상 속도와 동일하게 오디오에도 atempo를 적용한다.
    """
    src_wav = str(Path(CLIPS_DIR) / f"scene_{scene_id:03d}_src.wav")
    Path(CLIPS_DIR).mkdir(parents=True, exist_ok=True)

    duration = trim_end - trim_start if trim_end > trim_start else 0.0
    if duration <= 0:
        return None

    # atempo 계산 (target_duration이 주어진 경우)
    atempo_filter = ""
    if target_duration is not None and target_duration > 0 and abs(duration - target_duration) > 0.01:
        ratio = duration / target_duration
        # atempo는 0.5~2.0 범위만 지원; 범위 밖이면 체이닝
        if 0.5 <= ratio <= 2.0:
            atempo_filter = f",atempo={ratio:.6f}"
        elif ratio > 2.0:
            # 두 단계 체이닝
            atempo_filter = f",atempo=2.0,atempo={ratio/2.0:.6f}"
        elif ratio < 0.5:
            atempo_filter = f",atempo=0.5,atempo={ratio/0.5:.6f}"

    # footage_speed 반영: 영상이 setpts로 빨라지/느려지면 오디오도 동일 비율 atempo 적용
    # (영상이 N배 빨라지면 atempo=N; 0.5~2.0 범위 밖이면 체이닝)
    from .utils.speed_ramp import is_ramp_active, ramp_audio_filter
    if footage_speed and is_ramp_active(footage_speed_mode, footage_speed, ramp_base):
        # ramp: 계단식 atempo. target_duration atempo 가 앞에 있으면 그 결과 길이가 x 축.
        _ramp_L = target_duration if (target_duration and target_duration > 0 and atempo_filter) else duration
        atempo_filter += "," + ramp_audio_filter(
            _ramp_L, footage_speed, ramp_hold, ramp_accel,
            down=(footage_speed_mode == "ramp_down"), base=ramp_base,
        )
    elif footage_speed and abs(footage_speed - 1.0) > 1e-6:
        if 0.5 <= footage_speed <= 2.0:
            atempo_filter += f",atempo={footage_speed:.6f}"
        elif footage_speed > 2.0:
            atempo_filter += f",atempo=2.0,atempo={footage_speed/2.0:.6f}"
        else:
            atempo_filter += f",atempo=0.5,atempo={footage_speed/0.5:.6f}"

    af = f"aresample=44100{atempo_filter}" if atempo_filter else "aresample=44100"

    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", str(trim_start),
        "-t", str(duration),
        "-i", source_file,
        "-vn",
        "-af", af,
        "-ar", "44100",
        "-ac", "2",
        src_wav,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        # 오디오 트랙 없음 또는 추출 실패: 무시하고 계속 진행
        return None
    # 빈 파일(0바이트) 이면 삭제
    if not Path(src_wav).exists() or Path(src_wav).stat().st_size == 0:
        Path(src_wav).unlink(missing_ok=True)
        return None
    return src_wav


def _build_sound_duration_map(selections: list) -> dict[int, float]:
    """scene_plan.json의 sound_effect 필드를 읽어 씬별 사운드 파일 기반 duration을 반환한다.

    sound_effect가 있는 씬에 대해 사운드 파일의 실제 길이 + offset_sec를 계산한다.
    사운드 파일을 찾을 수 없으면 해당 씬은 포함하지 않는다.

    반환값: {scene_id: duration_sec}
    """
    from pathlib import Path as _Path

    scene_data = load_json("cache/scene_plan.json")
    if not scene_data:
        return {}

    scene_map = {s["scene_id"]: s for s in scene_data.get("scenes", [])}
    result: dict[int, float] = {}

    for sel in selections:
        scene_id = sel["scene_id"]
        scene = scene_map.get(scene_id)
        if not scene:
            continue
        sound_effect = scene.get("sound_effect")
        if not sound_effect or not sound_effect.get("file"):
            continue

        sound_file = sound_effect["file"]
        offset_sec = float(sound_effect.get("offset_sec", 0.3))

        # 사운드 파일 경로 해석
        p = _Path(sound_file)
        if not p.is_absolute() and not p.exists():
            p = _Path("sounds") / sound_file

        if not p.exists():
            continue

        try:
            sound_dur = get_duration(str(p))
            result[scene_id] = round(sound_dur + offset_sec, 3)
        except Exception:
            pass

    return result


# ── 렌더 단계별 소요 시간 기록 (병목 진단용) ─────────────────────────────────
# run() 이 씬별 클립 생성·후처리·연결 시간을 모아 끝에 요약을 콘솔에 찍고
# output/render_timing.json 으로 남긴다. 렌더 결과에는 영향 없음.
_RENDER_TIMING: list[tuple[str, object, float]] = []


def _timing_add(stage: str, scene_id: object, sec: float) -> None:
    _RENDER_TIMING.append((stage, scene_id, sec))
    console.print(f"  [dim][timing] 씬 {scene_id} {stage}: {sec:.2f}s[/dim]")


# ── #415 클립 생성 결과 렌더 간 캐시 ─────────────────────────────────────────
# 미리보기 단계 캐시(src/render_spec/preview_stages.py)와 같은 디렉토리·파일 규칙
# (<project>/cache/scene-XXX/{stage}-{hash}.mp4)을 쓰되 stage 이름을 "render-clip" 으로
# 분리한다. 렌더는 미리보기보다 해상도·화질이 높으므로 서로 섞이지 않는다.
# 크기 관리는 preview_stages.enforce_lru(프로젝트별 1GB, atime LRU)가 함께 맡는다.
# 렌더 cwd = 프로젝트 데이터 폴더이므로 "cache" 가 곧 프로젝트 캐시 폴더다.
RENDER_CLIP_CACHE_DIR = "cache"
RENDER_CLIP_CACHE_STAGE = "render-clip"
# 클립 생성 파이프라인(cut_clip 인코더 설정, 배속 단계 인자)이 바뀌면 올려서 옛 캐시를 무효화한다.
# v2 (#416 1안): cut_clip 에 디졸브 경계용 강제 키프레임(-force_key_frames) 추가.
RENDER_CLIP_CACHE_VERSION = 2
_RENDER_CLIP_CACHE: dict[object, str] = {}


def _render_clip_cache_key(
    cut_kwargs: dict,
    *,
    apply_speed: bool,
    footage_speed: float,
    speed_mode: str,
    ramp_hold: float,
    ramp_accel: float,
    ramp_base: float,
) -> "str | None":
    """클립 생성 단계 캐시 키. 계산할 수 없으면(원본 없음 등) None — 캐시 미사용."""
    import os as _os
    try:
        from .render_spec import preview_stages as _stages
        src = str(cut_kwargs["src"])
        st = Path(src).stat()
        payload = {
            "v": RENDER_CLIP_CACHE_VERSION,
            "src": {
                "path": _os.path.abspath(src),
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
            },
            "start": round(float(cut_kwargs["start"]), 6),
            "end": round(float(cut_kwargs["end"]), 6),
            "target": (None if cut_kwargs.get("target_duration") is None
                       else round(float(cut_kwargs["target_duration"]), 6)),
            "mute": bool(cut_kwargs.get("mute", True)),
            "w": int(cut_kwargs["out_w"]),
            "h": int(cut_kwargs["out_h"]),
            "xo": round(float(cut_kwargs.get("x_offset", 0.0)), 6),
            "yo": round(float(cut_kwargs.get("y_offset", 0.0)), 6),
            "fit": str(cut_kwargs.get("fit", "contain")),
            "rot": int(cut_kwargs.get("rotation", 0)),
            "bg": str(cut_kwargs.get("bg_color", "black")),
            # 배속 단계가 실제로 적용될 때만 배속 값이 결과에 반영된다.
            "speed": ({
                "x": round(float(footage_speed), 6),
                "mode": str(speed_mode),
                "hold": round(float(ramp_hold), 6),
                "accel": round(float(ramp_accel), 6),
                "base": round(float(ramp_base), 6),
            } if apply_speed else None),
            # cut_clip 은 libx264 고정, 배속 단계는 _video_encoder_args(50) — 인코더가 바뀌면 미스.
            "enc": _video_encoder_args(50) if apply_speed else None,
        }
        return _stages.stage_hash(RENDER_CLIP_CACHE_STAGE, payload)
    except Exception:
        return None


def _render_clip_cache_fetch(scene_id: int, key: "str | None", dst: str) -> bool:
    """캐시 적중 시 dst 로 복사하고 True. 미스·오류면 False(기존 경로로 생성)."""
    if not key:
        return False
    try:
        import shutil as _sh
        from .render_spec import preview_stages as _stages
        res = _stages.lookup(Path(RENDER_CLIP_CACHE_DIR), scene_id, RENDER_CLIP_CACHE_STAGE, key)
        if not res.hit:
            return False
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        _sh.copyfile(str(res.cached), dst)
        console.print(f"  [dim]씬 {scene_id}: 클립 생성 캐시 적중 ({key[:10]})[/dim]")
        return True
    except Exception:
        return False


def _render_clip_cache_store(scene_id: int, key: str, dst: str) -> None:
    try:
        if not Path(dst).exists() or Path(dst).stat().st_size == 0:
            return
        from .render_spec import preview_stages as _stages
        _stages.store(Path(RENDER_CLIP_CACHE_DIR), scene_id, RENDER_CLIP_CACHE_STAGE, key, dst)
    except Exception as e:
        console.print(f"  [dim]씬 {scene_id}: 클립 캐시 저장 실패(무시): {e}[/dim]")


def _timing_report() -> None:
    import json as _json
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for stage, _sid, sec in _RENDER_TIMING:
        totals[stage] = totals.get(stage, 0.0) + sec
        counts[stage] = counts.get(stage, 0) + 1
    grand = sum(totals.values()) or 1e-9
    console.print("  [bold][timing] 단계별 합계[/bold]")
    for stage, sec in sorted(totals.items(), key=lambda kv: -kv[1]):
        console.print(f"    {stage:<22} {sec:8.1f}s  ({sec / grand * 100:4.1f}%, 씬 {counts[stage]}개)")
    top = sorted(_RENDER_TIMING, key=lambda r: -r[2])[:12]
    console.print("  [bold][timing] 오래 걸린 순 (씬·단계)[/bold]")
    for stage, sid, sec in top:
        console.print(f"    씬 {sid!s:>3} {stage:<22} {sec:7.1f}s")
    try:
        Path("output").mkdir(exist_ok=True)
        Path("output/render_timing.json").write_text(_json.dumps({
            "totals_sec": {k: round(v, 2) for k, v in totals.items()},
            "entries": [{"stage": s, "scene_id": sid, "sec": round(sec, 2)} for s, sid, sec in _RENDER_TIMING],
            # #415 — 씬별 클립 생성 캐시 결과(hit/miss/off). clip 효과 씬만 기록된다.
            "clip_cache": {str(k): v for k, v in _RENDER_CLIP_CACHE.items()},
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def run(
    force: bool = False,
    progress_cb: Optional[Callable[[int, int, Optional[int]], None]] = None,
) -> list:
    """클립 편집 실행. SceneTimestamp 배열 반환.

    progress_cb: 씬 편집 진행 콜백. 지정 시 각 씬 처리 시작 시점에
      ``progress_cb(done, total, scene_id)`` 로 호출된다(done=완료 씬 수).
      기본 None — None 이면 기존 동작과 100% 동일(콜백 호출 없음).
    """
    sel_data = load_json("cache/clip_selection.json")
    if not sel_data:
        raise RuntimeError("cache/clip_selection.json 이 없어요. Phase 3을 먼저 실행해주세요.")
    selections = sorted(sel_data["selections"], key=lambda x: x["scene_id"])

    # 출력 해상도 읽기 (기본값 1920×1080)
    output_width = int(sel_data.get("output_width", 1920))
    output_height = int(sel_data.get("output_height", 1080))

    # 프로젝트 전역 배경색(letterbox 여백). 기본 "black" — 미지정 시 기존과 동일.
    bg_color = str(sel_data.get("bg_color", "black") or "black")
    # sync Time Warp 등 인라인 ffmpeg pad 표현식용 접미사. black(기본)이면 빈 문자열 —
    # 기존 `pad=...:(oh-ih)/2` 문자열과 바이트 동일.
    from .utils.ffmpeg_utils import normalize_bg_color as _normalize_bg_color
    _bg_norm = _normalize_bg_color(bg_color)
    _bg_pad_suffix = "" if _bg_norm == "black" else f":{_bg_norm}"

    # 스플릿 씬 디졸브 강제 적용 옵션 — 기본 False(안전망 작동).
    # True면 같은 source_file을 공유하는 인접 씬 사이에도 일반 dissolve 효과를 강제 적용한다.
    # 옛 키(force_sibling_dissolve)도 한 번 폴백 — 옛 selections JSON 캐시 호환.
    force_split_scene_dissolve = bool(
        sel_data.get(
            "force_split_scene_dissolve",
            sel_data.get("force_sibling_dissolve", False),
        )
    )

    if not force and Path(MUTED_OUTPUT).exists():
        cached_tl = load_json(TIMELINE_PATH)
        if cached_tl:
            console.print("[dim]  Phase 5: muted_output 캐시 사용[/dim]")
            # 캐시 사용 시에도 narration_timeline.json의 start_sec가 0인 경우
            # (Phase 4-A 직후 Phase 5가 아직 갱신하지 않은 상태) 타임라인을 갱신한다.
            narr_data = load_json("output/narration/narration_timeline.json")
            if narr_data and narr_data.get("entries"):
                first_entry = narr_data["entries"][0]
                if first_entry.get("start_sec", 0.0) == 0.0:
                    _update_narration_timing(cached_tl["timestamps"])
            return cached_tl["timestamps"]

    console.print(f"\n[bold cyan]Phase 5[/bold cyan] 클립 편집 중... ({len(selections)}개 씬, 디졸브 {DISSOLVE_SEC}초)")

    # 연속 (#effect clip) 씬 그룹별 오프셋 사전 계산
    clip_group_offsets = _build_clip_group_offsets(selections)

    # 연속 (#effect clip) 씬 그룹별 총 TTS 합산 사전 계산 (speed-up 기준)
    clip_group_total_tts = _build_clip_group_total_tts(selections)

    # 연속 (#effect clip) 씬 그룹 크기 사전 계산 (단독/연속 판별)
    clip_group_sizes = _build_clip_group_sizes(selections)

    # 연속 kenburns 씬 그룹별 scale 파라미터 사전 계산
    kenburns_group_params = _build_kenburns_group_params(selections)

    # 연속 multiscreen 씬 그룹별 오프셋 사전 계산
    multiscreen_group_offsets = _build_multiscreen_group_offsets(selections)

    # 연속 multiscreen 씬 그룹별 전체 tts 합산 (재생 속도 조절용)
    multiscreen_group_total_tts = _build_multiscreen_group_total_tts(selections)

    # sound_effect 씬별 사운드 파일 기반 duration 사전 계산
    sound_duration_map = _build_sound_duration_map(selections)

    # 씬별 그룹 ID (같은 그룹 내 씬 경계에는 디졸브 미적용)
    # force_split_scene_dissolve=True면 _build_scene_group_ids의 기본 묶음(같은 effect+source_file
    # 인접 씬을 한 그룹으로)도 우회해 모든 씬을 별개 그룹으로 강제. 그래야 스플릿 씬 경계에
    # 실제로 dissolve가 적용된다. (c) 안전망만 건너뛰는 것으로는 부족 — 본체가 이미 묶기 때문.
    if force_split_scene_dissolve:
        scene_group_ids = {sel["scene_id"]: sel["scene_id"] for sel in selections if "scene_id" in sel}
    else:
        scene_group_ids = _build_scene_group_ids(selections)

    # ── 분할 스플릿 씬 dissolve 폴백 (#잔상 방지) ────────────────────────────
    # 같은 source_file을 공유하는 인접 씬은 xfade dissolve로 겹치면 동일 영상의
    # 두 시점이 잔상으로 보인다. _build_scene_group_ids가 (effect, source_file)
    # 기반으로 같은 그룹화를 시도하지만 effect가 다른 케이스 등은 놓칠 수 있어
    # 안전망으로 source_file 동일성만 기준으로 인접 그룹을 한 번 더 병합한다.
    #
    # force_split_scene_dissolve=True면 본 안전망을 건너뛰어 스플릿 씬 사이에도 일반
    # dissolve가 강제 적용되도록 한다(의도적 잔상/전환 효과 허용).
    if selections and not force_split_scene_dissolve:
        sorted_sels = list(selections)
        prev_sf: str | None = None
        prev_gid_target: int | None = None
        for _sel in sorted_sels:
            _sid = _sel.get("scene_id")
            if _sid is None:
                continue
            _sf = _sel.get("source_file") or ""
            _cur_gid = scene_group_ids.get(_sid)
            if _sf and _sf == prev_sf and prev_gid_target is not None and _cur_gid != prev_gid_target:
                # 이전 인접 씬과 같은 source_file → 같은 그룹으로 강제 병합
                scene_group_ids[_sid] = prev_gid_target
            else:
                prev_gid_target = scene_group_ids.get(_sid)
            prev_sf = _sf if _sf else None

    clip_paths = []
    clip_scene_ids = []  # clip_paths와 1:1 대응하는 scene_id 목록

    # 이전 렌더에서 같은 scene_id로 생성된 현장음(src.wav) 잔재를 모두 제거.
    # 분할/재배치로 scene_id에 매핑된 footage가 바뀌면 이전 src.wav가 잘못된 사운드로 입혀지는 회귀가 발생.
    # 매 렌더 시작 시 selection의 scene_id 범위에 해당하는 잔재를 청소하고,
    # 이번 렌더에서 clip 타입 씬만 새로 추출하도록 한다.
    for _sel in selections:
        _sid = _sel.get("scene_id")
        if _sid is None:
            continue
        _src_wav = Path(CLIPS_DIR) / f"scene_{_sid:03d}_src.wav"
        if _src_wav.exists():
            try:
                _src_wav.unlink()
            except OSError:
                pass

    with Progress(
        TextColumn("  씬 {task.fields[scene_id]}"),
        BarColumn(bar_width=20),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("", total=len(selections), scene_id="?")

        import time as _time_t
        _RENDER_TIMING.clear()
        _RENDER_CLIP_CACHE.clear()
        _cut_prev: tuple | None = None  # (scene_id, 시작 시각) — 씬 루프에 continue 가 많아 다음 씬 시작 시점에 마감
        for i, sel in enumerate(selections):
            scene_id = sel["scene_id"]
            _now_t = _time_t.perf_counter()
            if _cut_prev is not None:
                _timing_add("1_클립생성(자르기·배속)", _cut_prev[0], _now_t - _cut_prev[1])
            _cut_prev = (scene_id, _now_t)
            progress.update(task, scene_id=scene_id)
            # 씬별 진행 콜백 — 이 씬 처리 시작 시점 = 앞선 i개 완료.
            # 콜백 예외가 렌더를 깨지 않도록 방어.
            if progress_cb is not None:
                try:
                    progress_cb(i, len(selections), scene_id)
                except Exception:
                    pass

            dst = str(Path(CLIPS_DIR) / f"scene_{scene_id:03d}.mp4")
            # sound_effect가 있는 씬은 사운드 파일 길이 + offset_sec 기반 duration 사용
            if scene_id in sound_duration_map:
                tts_dur = sound_duration_map[scene_id]
            else:
                tts_dur = sel.get("tts_duration_sec")

            # title_gen 씬 처리: 텍스트 타이틀 영상 생성
            if sel.get("effect") == "title_gen":
                options = sel.get("title_gen_options", {})
                clip_duration = tts_dur or sel.get("clip_duration_sec", 5.0)

                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                try:
                    from .utils.title_gen import generate_text_video
                    video_w = int(options.get("width", output_width))
                    video_h = int(options.get("height", output_height))
                    generate_text_video(
                        output_path=dst,
                        video_size=(video_w, video_h),
                        text=str(options.get("text", "")),
                        mode=str(options.get("mode", "shimmer")),
                        duration=float(options.get("duration", clip_duration)),
                        font_path=options.get("font_path"),
                        font_size=int(options["font_size"]) if "font_size" in options else None,
                        text_color=str(options.get("text_color", "white")),
                        rect_color=str(options.get("rect_color", "#000000")),
                        bg_color=str(options.get("bg_color", "#000000")),
                        dissolve_time=float(options.get("dissolve_time", 1.0)),
                        spc_word=options.get("spc_word"),
                    )
                except Exception as e:
                    console.print(f"  [red]씬 {scene_id}: title_gen 처리 중 오류 — {e}[/red]")
                    progress.advance(task)
                    continue

                clip_paths.append(dst)
                clip_scene_ids.append(scene_id)
                progress.advance(task)
                continue

            # kenburns_copy_img 씬 처리: copy_img로 픽셀화 후 kenburns 클립 생성
            if sel.get("effect") == "kenburns_copy_img":
                copy_img_options = sel.get("copy_img_options", {})
                clip_duration = tts_dur or sel.get("clip_duration_sec", 5.0)

                input_file = copy_img_options.get("input", "")
                output_file = copy_img_options.get("output", "")
                pixel_size = int(copy_img_options.get("pixel_size", 2))
                palette_size = int(copy_img_options.get("palette_size", 16))

                # 입력 이미지 경로 해석
                input_path = Path(input_file)
                if not input_path.is_absolute() and not input_path.exists():
                    input_path = Path("footage") / input_file

                if not input_path.exists():
                    console.print(f"  [red]씬 {scene_id}: copy_img 입력 이미지를 찾을 수 없어요: {input_file}[/red]")
                    progress.advance(task)
                    continue

                # 출력 이미지 경로 (output/clips/ 폴더 내에 저장)
                if output_file:
                    pixelated_path = Path(CLIPS_DIR) / output_file
                else:
                    pixelated_path = Path(CLIPS_DIR) / f"scene_{scene_id:03d}_pixel.png"
                pixelated_path.parent.mkdir(parents=True, exist_ok=True)

                try:
                    from PIL import Image as PILImage
                    from .utils.copy_img import pixelate
                    img = PILImage.open(str(input_path)).convert("RGB")
                    pixel_img = pixelate(img, pixel_size=pixel_size, palette_size=palette_size)
                    pixel_img.save(str(pixelated_path))
                except Exception as e:
                    console.print(f"  [red]씬 {scene_id}: copy_img 픽셀화 중 오류 — {e}[/red]")
                    progress.advance(task)
                    continue

                # 픽셀화된 이미지에 kenburns 효과 적용
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                try:
                    from .utils.ken_burns import generate_moviepy_clip
                    video_w = int(copy_img_options.get("width", output_width))
                    video_h = int(copy_img_options.get("height", output_height))
                    kb_params = kenburns_group_params.get(scene_id, {"scale_start": 1.0, "scale_end": 1.2})
                    # #fit-bug — selection의 fit을 전달 (kenburns_copy_img도 동일).
                    _kb_fit_raw = sel.get("fit", "cover") or "cover"
                    _kb_fit = _kb_fit_raw if _kb_fit_raw in ("contain", "cover", "stretch") else "cover"
                    success = generate_moviepy_clip(
                        img_path=str(pixelated_path),
                        output_path=dst,
                        duration=clip_duration,
                        start_scale=kb_params["scale_start"],
                        end_scale=kb_params["scale_end"],
                        video_size=(video_w, video_h),
                        fit_mode=_kb_fit,
                        anchor_x=0.5,
                        anchor_y=0.5,
                        fps=30,
                    )
                    if not success:
                        console.print(f"  [red]씬 {scene_id}: kenburns+copy_img 클립 생성 실패[/red]")
                        progress.advance(task)
                        continue
                except Exception as e:
                    console.print(f"  [red]씬 {scene_id}: kenburns+copy_img 처리 중 오류 — {e}[/red]")
                    progress.advance(task)
                    continue

                clip_paths.append(dst)
                clip_scene_ids.append(scene_id)
                progress.advance(task)
                continue

            # clip 씬 처리: footage/ 폴더의 영상 파일을 그대로 로드하여 트림
            # - 케이스 1 (연속 그룹, 2씬 이상): 그룹 총 TTS 합산 < 클립 길이이면 speed-up
            # - 케이스 2 (단독 씬): 클립 끝까지 재생 (trim_end = actual_dur)
            if sel.get("effect") == "clip":
                file_name = sel.get("source_file", "")
                clip_duration = tts_dur or sel.get("clip_duration_sec", 5.0)
                footage_speed = float(sel.get("footage_speed", 1.0) or 1.0)
                # 재생 속도 모드(constant/ramp) — 수학은 src/utils/speed_ramp 단일 헬퍼.
                from .utils.speed_ramp import (
                    scene_speed_params as _scene_speed_params,
                    is_ramp_active as _is_ramp_active,
                    output_duration as _speed_out_dur,
                    scene_speed_base as _scene_speed_base,
                )
                _spd_mode, _, _spd_hold, _spd_accel = _scene_speed_params(sel)
                _spd_base = _scene_speed_base(sel)
                _speed_ramp = _is_ramp_active(_spd_mode, footage_speed, _spd_base)
                clip_x_offset = float(sel.get("x_offset", 0) or 0)
                clip_y_offset = float(sel.get("y_offset", 0) or 0)
                # #258 fit 모드 (contain/cover/stretch). 미지정 시 contain (회귀 보장).
                _fit_raw = sel.get("fit", "contain")
                clip_fit = _fit_raw if _fit_raw in ("contain", "cover", "stretch") else "contain"
                # #263 수동 회전 각도 (0/90/180/270). 잘못된 값은 0으로 폴백.
                try:
                    _rot_raw = int(sel.get("rotation", 0) or 0)
                except (TypeError, ValueError):
                    _rot_raw = 0
                clip_rotation = _rot_raw if _rot_raw in (0, 90, 180, 270) else 0

                # 영상 경로 해석: footage/ 폴더 기준 또는 절대경로
                clip_path = Path(file_name)
                if not clip_path.is_absolute() and not clip_path.exists():
                    clip_path = Path("footage") / file_name

                if not clip_path.exists():
                    console.print(f"  [red]씬 {scene_id}: clip 영상을 찾을 수 없어요: {file_name}[/red]")
                    progress.advance(task)
                    continue

                Path(dst).parent.mkdir(parents=True, exist_ok=True)

                # 분할 씬: selection에 explicit_trim이 True면 trim_start/trim_end를 그대로 사용.
                # group_offsets/단독 씬 trim_end 결정 로직을 건너뜀.
                explicit_trim = bool(sel.get("explicit_trim"))
                actual_dur = get_duration(str(clip_path))
                if explicit_trim:
                    trim_start = float(sel.get("trim_start", 0.0))
                else:
                    # 연속 그룹 오프셋 적용
                    trim_start = clip_group_offsets.get(scene_id, 0.0)

                if trim_start >= actual_dur:
                    console.print(
                        f"  [yellow]씬 {scene_id}: clip 오프셋({trim_start:.2f}s)이 "
                        f"원본 길이({actual_dur:.2f}s)를 초과 — 마지막 프레임 사용[/yellow]"
                    )
                    trim_start = max(0.0, actual_dur - 0.1)

                group_size = clip_group_sizes.get(scene_id, 1)
                total_tts = clip_group_total_tts.get(scene_id, clip_duration)

                # 분할 씬은 같은 source_file이라도 각자 명시된 구간을 사용해야 하므로 단독 처리.
                if explicit_trim:
                    group_size = 1

                # 케이스 2 단독 씬의 일부 분기(영상 늘림/오디오 유지)는 cut_clip에서 이미
                # 최종 길이를 확정하므로 아래 setpts(1/footage_speed) 이중 적용을 스킵한다.
                # 케이스 1(그룹)에서도 참조되므로 여기서 미리 초기화.
                skip_footage_speed = False

                if group_size >= 2:
                    # 케이스 1: 연속 그룹 — total_tts 기준으로 클립 전체를 speed-up 재생
                    # speed-up 비율에 맞게 각 씬의 트림 구간도 조정
                    clip_avail = actual_dur  # 클립 전체 사용
                    if total_tts < clip_avail:
                        # speed-up: total_tts 안에 clip_avail을 다 보여줌
                        speedup_ratio = total_tts / clip_avail
                        # 이 씬의 조정된 트림 구간: 오프셋과 씬 duration을 speedup 역산
                        adjusted_start = trim_start / speedup_ratio
                        adjusted_end = adjusted_start + (clip_duration / speedup_ratio)
                        if adjusted_end > actual_dur:
                            adjusted_end = actual_dur
                        # target_duration = clip_duration (원래 TTS 길이로 압축)
                        # #415 — 실제 cut_clip 호출은 아래 캐시 블록에서 한 번에 수행.
                        _cut_kwargs = dict(
                            src=str(clip_path),
                            start=adjusted_start,
                            end=adjusted_end,
                            dst=dst,
                            mute=True,
                            target_duration=clip_duration,
                            out_w=output_width,
                            out_h=output_height,
                            x_offset=clip_x_offset,
                            y_offset=clip_y_offset,
                            fit=clip_fit,
                            rotation=clip_rotation,
                            bg_color=bg_color,
                        )
                        console.print(
                            f"  [dim]씬 {scene_id}: clip speed-up (그룹 total_tts={total_tts:.2f}s "
                            f"< 클립={clip_avail:.2f}s, 배율={1/speedup_ratio:.2f}x)[/dim]"
                        )
                    else:
                        # total_tts >= 클립 길이: 원본이 그룹 전체를 감당 못 한다.
                        #
                        # #short-source-group — 예전에는 trim_end = trim_start + TTS 로
                        # 잡아 원본 끝을 넘겼고, 넘친 만큼 잘려나가 뒤쪽 씬일수록 영상이
                        # 짧아졌다(씬 67: 0.63s vs 나레이션 2.44s).
                        # 이제 오프셋은 _build_clip_group_offsets 가 원본을 TTS 비율대로
                        # 배분해 주므로, 끝점도 "다음 씬 오프셋"(=자기 몫의 끝)으로 잡고
                        # target_duration 으로 나레이션 길이만큼 늘린다(슬로모션).
                        next_off = clip_group_offsets.get(scene_id + 1)
                        share_end = (
                            next_off if (next_off is not None and next_off > trim_start)
                            else actual_dur
                        )
                        trim_end = min(actual_dur, share_end)
                        if trim_end <= trim_start:
                            trim_end = min(actual_dur, trim_start + 0.1)
                        # 자기 몫(짧음)을 나레이션 길이로 늘려 영상이 먼저 끝나지 않게 한다.
                        _share = trim_end - trim_start
                        _stretch_to = clip_duration if clip_duration > _share else None
                        if _stretch_to:
                            console.print(
                                f"  [dim]씬 {scene_id}: 그룹 소재 부족 — 몫 {_share:.2f}s 를 "
                                f"{_stretch_to:.2f}s 로 늘림(슬로모션 {_share / _stretch_to:.2f}x)[/dim]"
                            )
                        _cut_kwargs = dict(
                            src=str(clip_path), start=trim_start, end=trim_end, dst=dst, mute=True,
                            target_duration=_stretch_to,
                            out_w=output_width, out_h=output_height,
                            x_offset=clip_x_offset, y_offset=clip_y_offset, fit=clip_fit,
                            rotation=clip_rotation, bg_color=bg_color,
                        )
                        # cut_clip 이 최종 길이를 확정했으므로 setpts 이중 적용 금지.
                        if _stretch_to:
                            skip_footage_speed = True
                else:
                    # 케이스 2: 단독 씬
                    # 나레이션은 씬 시작 후 앞 디졸브(DISSOLVE_SEC)가 끝난 뒤 시작하고,
                    # 씬 끝에도 뒤 디졸브(DISSOLVE_SEC) 구간이 필요하므로
                    # 최소 영상 길이 = TTS + DISSOLVE_SEC * 2
                    min_dur = clip_duration + DISSOLVE_SEC * 2
                    # 분할 씬은 selection의 trim_end가 의도된 끝 — actual_dur 대신 그것을 기준으로.
                    eff_end = float(sel.get("trim_end", actual_dur)) if explicit_trim else actual_dur
                    eff_end = max(trim_start, min(actual_dur, eff_end))
                    avail_dur = eff_end - trim_start
                    # "영상 늘림"(target_dur=min_dur) / "오디오 있음 길이 유지" 분기는
                    # cut_clip 단계에서 이미 최종 길이를 확정하므로, 아래 setpts(1/footage_speed)를
                    # 또 적용하면 min_dur/영상 길이가 깨진다. 이 경우 footage_speed 이중 적용을 스킵.
                    # (skip_footage_speed는 group_size 분기 앞에서 False로 초기화됨)
                    if avail_dur < min_dur:
                        # 영상이 짧음 → TTS + DISSOLVE_SEC * 2 길이로 슬로모션 늘림
                        # clip effect 영상에 원본 오디오가 있으면 늘릴 수 없음 → 그대로 사용
                        from .utils.ffmpeg_utils import has_audio
                        # 사용자가 footage_speed 를 명시적으로 조절했다면 그 의도가 최우선이다.
                        # 영상이 TTS 보다 짧아서 속도를 이미 지정해 둔 씬이므로, 렌더는 그
                        # 지정값을 그대로 반영하기만 하면 된다. 여기서 속도를 버리면 영상이
                        # 원본 길이에 멈춰 나레이션이 씬 밖으로 잘린다(실측: 씬54 TTS 5.44s →
                        # 3.47s 로 렌더되어 약 2초 잘림).
                        # "원본 오디오 보존" 분기는 현장음이 늘어지는 걸 막으려는 것이지만,
                        # 나레이션이 잘리는 손실이 현장음 늘어짐보다 크다 — 명시적 속도 지정이
                        # 있으면 현장음 사용 여부와 무관하게 속도를 따른다.
                        # ramp(가속) 모드도 명시적 속도 지정으로 취급한다.
                        _explicit_speed = abs(footage_speed - 1.0) > 1e-6 or _speed_ramp
                        if has_audio(str(clip_path)) and not _explicit_speed:
                            trim_end = eff_end
                            target_dur = None
                            # 원본 오디오 보존 위해 길이 유지 → setpts로 길이 바꾸면 안 됨
                            skip_footage_speed = True
                            console.print(
                                f"  [yellow]씬 {scene_id}: clip 단독 씬, 원본 오디오 있음 — "
                                f"영상({avail_dur:.2f}s) < TTS+dissolve*2({min_dur:.2f}s)이지만 "
                                f"사운드 늘어짐 방지를 위해 영상 길이 유지[/yellow]"
                            )
                        elif _explicit_speed:
                            # 명시적 footage_speed → cut_clip 은 구간만 자르고 길이를 건드리지
                            # 않는다(target_dur=None). 실제 속도 변환은 아래 setpts 단계가
                            # 담당하므로 skip_footage_speed 를 켜지 않는다.
                            trim_end = eff_end
                            target_dur = None
                            console.print(
                                f"  [dim]씬 {scene_id}: clip 단독 씬 — footage_speed="
                                f"{footage_speed:.2f}x 명시 지정, 속도 유지 "
                                f"{' (가속)' if _speed_ramp else ''} "
                                f"(avail={avail_dur:.2f}s → 예상 "
                                f"{_speed_out_dur(avail_dur, _spd_mode, footage_speed, _spd_hold, _spd_accel, base=_spd_base):.2f}s)[/dim]"
                            )
                        else:
                            trim_end = eff_end
                            target_dur = min_dur
                            # cut_clip이 이미 min_dur로 슬로모션 늘림 완료 →
                            # footage_speed setpts 재적용 금지(이중 적용 시 min_dur 깨짐)
                            skip_footage_speed = True
                            console.print(
                                f"  [dim]씬 {scene_id}: clip 단독 씬, 영상 늘림 — "
                                f"avail={avail_dur:.2f}s → target={min_dur:.2f}s "
                                f"(footage_speed 이중적용 스킵)[/dim]"
                            )
                    else:
                        # 영상이 나레이션보다 김 → **원본 길이를 그대로 유지한다.**
                        #
                        # 예전에는 여기서 나레이션 길이(TTS + dissolve*2)에 맞춰 영상을
                        # 잘라냈다. 그 탓에 30초짜리 클립이 3.6초만 나오는 등 사용자가 고른
                        # 영상이 통째로 사라졌다(1분 58초 영상이 40초로 줄어든 사고).
                        #
                        # 길이를 나레이션에 맞추는 것은 사용자가 명시적으로 요청했을 때
                        # (씬 나누기로 구간 지정, footage_speed 조절 등) 하는 일이지
                        # 기본 동작이 아니다. TTS 가 영상보다 "짧을" 때는 위 분기에서
                        # 영상을 늘려 맞추지만, 반대 방향으로는 손대지 않는다.
                        trim_end = eff_end
                        target_dur = None
                        console.print(
                            f"  [dim]씬 {scene_id}: clip 단독 씬 — 영상 원본 길이 유지 "
                            f"(avail={avail_dur:.2f}s, TTS+dissolve*2={min_dur:.2f}s)[/dim]"
                        )
                    _cut_kwargs = dict(
                        src=str(clip_path), start=trim_start, end=trim_end, dst=dst, mute=True,
                        target_duration=target_dur,
                        out_w=output_width, out_h=output_height,
                        x_offset=clip_x_offset, y_offset=clip_y_offset, fit=clip_fit,
                        rotation=clip_rotation, bg_color=bg_color,
                    )

                # #415 — 클립 생성(자르기 + 배속 + 화면 맞춤) 결과 렌더 간 캐시.
                # 키는 cut_clip 인자 전체 + 배속 단계 입력 + 원본 서명 + 인코더. 하나라도 바뀌면 미스.
                # 적중하면 이전 렌더가 같은 입력으로 만든 바로 그 파일을 복사하므로 프레임이 동일하다.
                _apply_speed_step = bool((footage_speed != 1.0 or _speed_ramp) and not skip_footage_speed)
                _clip_cache_key = _render_clip_cache_key(
                    _cut_kwargs,
                    apply_speed=_apply_speed_step,
                    footage_speed=footage_speed,
                    speed_mode=_spd_mode,
                    ramp_hold=_spd_hold,
                    ramp_accel=_spd_accel,
                    ramp_base=_spd_base,
                )
                _clip_cache_hit = _render_clip_cache_fetch(scene_id, _clip_cache_key, dst)
                _RENDER_CLIP_CACHE[scene_id] = "hit" if _clip_cache_hit else ("miss" if _clip_cache_key else "off")
                if not _clip_cache_hit:
                    cut_clip(**_cut_kwargs)

                # footage_speed 적용: 1.0이 아닌 경우 setpts로 재생 속도 조절.
                # skip_footage_speed=True인 분기(영상 늘림 target_dur=min_dur / 오디오 유지)는
                # cut_clip에서 이미 최종 길이를 확정했으므로 setpts를 또 적용하면 길이가 깨진다.
                if _apply_speed_step and not _clip_cache_hit and Path(dst).exists() and Path(dst).stat().st_size > 0:
                    _speed_dst = dst + ".speed.mp4"
                    _pts_factor = 1.0 / footage_speed
                    _vf_speed = f"setpts={_pts_factor:.6f}*PTS"
                    if 0.5 <= footage_speed <= 2.0:
                        _af_speed = f"atempo={footage_speed:.6f}"
                    elif footage_speed > 2.0:
                        _af_speed = f"atempo=2.0,atempo={footage_speed / 2.0:.6f}"
                    else:
                        _af_speed = f"atempo=0.5,atempo={footage_speed / 0.5:.6f}"
                    if _speed_ramp:
                        # ramp: 정속 → 선형 가속 → 지정 배속 유지. x 축 = cut_clip 결과(dst) 길이.
                        from .utils.speed_ramp import ramp_setpts_expr, ramp_audio_filter
                        _ramp_L = get_duration(dst) or max(0.1, trim_end - trim_start)
                        _down = _spd_mode == "ramp_down"
                        _vf_speed = ramp_setpts_expr(_ramp_L, footage_speed, _spd_hold, _spd_accel, down=_down, base=_spd_base)
                        _af_speed = ramp_audio_filter(_ramp_L, footage_speed, _spd_hold, _spd_accel, down=_down, base=_spd_base)
                    # dst에 오디오 트랙이 있는지 확인 (cut_clip이 mute=True로 호출되어 보통 없음.
                    # 그래도 안전하게 검사하여 있으면 atempo로 같이 늘리고, 없으면 -an 유지).
                    _pa_clip_speed = subprocess.run(
                        [ffprobe_bin(), "-v", "error", "-select_streams", "a",
                         "-show_entries", "stream=codec_type", "-of", "csv=p=0", dst],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                    )
                    _dst_has_audio = bool(_pa_clip_speed.stdout.strip())
                    if _dst_has_audio:
                        _r_speed = subprocess.run(
                            [ffmpeg_bin(), "-y", "-i", dst,
                             "-vf", _vf_speed,
                             "-af", _af_speed,
                             *_video_encoder_args(50),
                             "-pix_fmt", "yuv420p",
                             "-c:a", "aac",
                             _speed_dst],
                            capture_output=True,
                        )
                    else:
                        _r_speed = subprocess.run(
                            [ffmpeg_bin(), "-y", "-i", dst,
                             "-vf", _vf_speed,
                             "-an",
                             *_video_encoder_args(50),
                             "-pix_fmt", "yuv420p",
                             _speed_dst],
                            capture_output=True,
                        )
                    if _r_speed.returncode == 0 and Path(_speed_dst).exists() and Path(_speed_dst).stat().st_size > 0:
                        import os as _os_clip
                        _os_clip.replace(_speed_dst, dst)
                        console.print(
                            f"  [dim]씬 {scene_id}: footage_speed={footage_speed:.2f}x"
                            f"{' 가속(ramp)' if _speed_ramp else ''} 적용[/dim]"
                        )
                    else:
                        Path(_speed_dst).unlink(missing_ok=True)
                        # 배속 실패한 결과는 캐시에 남기지 않는다(다음 렌더에서 재시도).
                        _clip_cache_key = None

                if not _clip_cache_hit and _clip_cache_key:
                    _render_clip_cache_store(scene_id, _clip_cache_key, dst)

                # 현장음 추출 — clip effect 단독/그룹 모두
                # trim_end: footage 원본 기준 끝 지점 (cut_clip에 넘긴 값과 동일)
                # target_duration: 영상 슬로모션/스피드업 배율과 오디오를 일치시킴
                # footage_speed: 영상이 setpts로 빨라지/느려지면 오디오도 동일 비율로 atempo 적용
                # 영상에 setpts를 스킵한 분기는 오디오에도 footage_speed를 적용하면
                # 영상-오디오 길이가 어긋난다. 이 경우 footage_speed=1.0으로 추출하여
                # target_duration(있으면 min_dur)만으로 영상 길이와 일치시킨다.
                _audio_footage_speed = 1.0 if skip_footage_speed else footage_speed
                _extract_src_audio(
                    str(clip_path), trim_start, trim_end,
                    scene_id, target_duration=target_dur,
                    footage_speed=_audio_footage_speed,
                    footage_speed_mode=_spd_mode,
                    ramp_hold=_spd_hold,
                    ramp_accel=_spd_accel,
                    ramp_base=_spd_base,
                )

                clip_paths.append(dst)
                clip_scene_ids.append(scene_id)
                progress.advance(task)
                continue

            # kenburns 씬 처리: 이미지에 Ken Burns 효과를 적용하여 영상 클립 생성
            if sel.get("effect") == "kenburns":
                image_file = sel.get("kenburns_image", sel["source_file"])
                # 앞뒤 디졸브 구간을 포함한 최소 클립 길이 = TTS + DISSOLVE_SEC * 2
                clip_duration = (tts_dur or sel.get("clip_duration_sec", 5.0)) + DISSOLVE_SEC * 2

                # 이미지 경로 해석: footage/ 폴더 기준 또는 절대경로
                image_path = Path(image_file)
                if not image_path.is_absolute() and not image_path.exists():
                    image_path = Path("footage") / image_file

                if not image_path.exists():
                    console.print(f"  [red]씬 {scene_id}: kenburns 이미지를 찾을 수 없어요: {image_file}[/red]")
                    progress.advance(task)
                    continue

                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                try:
                    from .utils.ken_burns import generate_moviepy_clip
                    kb_params = kenburns_group_params.get(scene_id, {"scale_start": 1.0, "scale_end": 1.2})
                    # #fit-bug — 이전엔 fit_mode="cover" 하드코딩이었고 ken_burns가
                    # 인자를 무시하고 무조건 contain으로만 처리했음. 이제 selection의 fit을
                    # 그대로 전달하고 ken_burns가 cover/contain/stretch를 분기 처리한다.
                    _kb_fit_raw = sel.get("fit", "cover") or "cover"
                    _kb_fit = _kb_fit_raw if _kb_fit_raw in ("contain", "cover", "stretch") else "cover"
                    success = generate_moviepy_clip(
                        img_path=str(image_path),
                        output_path=dst,
                        duration=clip_duration,
                        start_scale=kb_params["scale_start"],
                        end_scale=kb_params["scale_end"],
                        video_size=(output_width, output_height),
                        fit_mode=_kb_fit,
                        anchor_x=0.5,
                        anchor_y=0.5,
                        fps=30,
                    )
                    if not success:
                        console.print(f"  [red]씬 {scene_id}: Ken Burns 클립 생성 실패[/red]")
                        progress.advance(task)
                        continue
                except Exception as e:
                    console.print(f"  [red]씬 {scene_id}: Ken Burns 처리 중 오류 — {e}[/red]")
                    progress.advance(task)
                    continue

                clip_paths.append(dst)
                clip_scene_ids.append(scene_id)
                progress.advance(task)
                continue

            # multiscreen 씬 처리: 각 소스 클립을 커팅 후 xstack으로 합성
            if sel.get("effect") == "multiscreen":
                multiscreen_clips = sel.get("multiscreen_clips", [])
                if not multiscreen_clips:
                    console.print(f"  [red]씬 {scene_id}: multiscreen_clips가 없어요. 건너뜁니다.[/red]")
                    progress.advance(task)
                    continue

                # 연속 그룹 전체 tts 합산을 target_duration으로 사용 (속도 동기화)
                # 그룹 정보가 없으면 현재 씬의 tts_dur 사용
                scene_tts = tts_dur if tts_dur is not None else sel.get("clip_duration_sec", 5.0)
                # 앞뒤 디졸브 구간을 포함한 최소 클립 길이 = TTS + DISSOLVE_SEC * 2
                target_dur = multiscreen_group_total_tts.get(scene_id, scene_tts) + DISSOLVE_SEC * 2

                # 연속 그룹 오프셋 적용
                ms_offset = multiscreen_group_offsets.get(scene_id, 0.0)

                # 각 소스 클립을 개별 커팅
                raw_clip_paths = []
                tmp_dir = Path(CLIPS_DIR) / f"multiscreen_{scene_id:03d}_parts"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                for j, mc in enumerate(multiscreen_clips):
                    part_dst = str(tmp_dir / f"part_{j:02d}.mp4")
                    src_file = mc.get("source_file", "") or ""
                    # 빈 슬롯(grid_nxm 에서 파일 미지정 셀) — 배경색 정지 영상으로 채움.
                    if not src_file.strip():
                        _bg_lavfi = _normalize_bg_color(bg_color)  # "black" 또는 "0xRRGGBB"
                        _color_cmd = [
                            ffmpeg_bin(), "-y",
                            "-f", "lavfi",
                            "-i", f"color=c={_bg_lavfi}:s={output_width}x{output_height}:r=30:d={target_dur}",
                            "-t", str(target_dur),
                            "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-an",
                            part_dst,
                        ]
                        _r = subprocess.run(_color_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if _r.returncode != 0:
                            console.print(f"  [red]씬 {scene_id} 슬롯 {j}: 빈 셀 배경 생성 실패 — 건너뜁니다.[/red]")
                            continue
                        raw_clip_paths.append(part_dst)
                        continue
                    src_path = Path(src_file)
                    if not src_path.is_absolute() and not src_path.exists():
                        src_path = Path("footage") / src_file
                    src_file = str(src_path)
                    src_ext = Path(src_file).suffix.lower()

                    # 이미지 파일인 경우: target_dur(TTS + dissolve*2) 기준으로 정지 영상 변환
                    if src_ext in IMAGE_EXTENSIONS:
                        console.print(
                            f"  [cyan]씬 {scene_id} 슬롯 {j}: 이미지({src_ext}) → "
                            f"{target_dur:.2f}s 정지 영상으로 변환[/cyan]"
                        )
                        image_to_clip(src_file, target_dur, part_dst, out_w=output_width, out_h=output_height, bg_color=bg_color)
                        raw_clip_paths.append(part_dst)
                        continue

                    # 각 씬마다 원본 영상 전체(0~끝)를 scene_tts에 맞게 배속 재생
                    # offset 없이 항상 처음부터 재생하여 n배속으로 전체를 보여줌
                    actual_dur = get_duration(src_file)
                    cut_clip(
                        src_file,
                        0.0,
                        actual_dur,
                        part_dst,
                        mute=True,
                        target_duration=None,
                        out_w=output_width,
                        out_h=output_height,
                        bg_color=bg_color,
                    )
                    raw_clip_paths.append(part_dst)

                # 합성: grid_nxm(셀별 광원 보더) vs 기존 xstack 레이아웃 분기.
                # target_duration = TTS + DISSOLVE_SEC*2 (앞뒤 디졸브 여유 포함)
                ms_layout = sel.get("layout", "auto") or "auto"
                if ms_layout == "grid_nxm":
                    grid_rows = int(sel.get("grid_rows", 1) or 1)
                    grid_cols = int(sel.get("grid_cols", 1) or 1)
                    # 셀별 border 메타(멀티스크린 clips 순서 = 슬롯 순서)
                    borders = [mc.get("border") for mc in multiscreen_clips]
                    try:
                        build_grid_compare(
                            raw_clip_paths,
                            dst,
                            rows=grid_rows,
                            cols=grid_cols,
                            target_duration=target_dur,
                            borders=borders,
                            output_duration=target_dur,
                            out_w=output_width,
                            out_h=output_height,
                            bg_color=bg_color,
                        )
                    except Exception as _grid_err:
                        # 렌더 실패 시 전체 중단 대신 씬 스킵 (명확한 로그).
                        console.print(
                            f"  [red]씬 {scene_id}: 그리드 비교 합성 실패 — 씬을 건너뜁니다.\n"
                            f"  {_grid_err}[/red]"
                        )
                        progress.advance(task)
                        continue
                else:
                    build_xstack_multiscreen(
                        raw_clip_paths,
                        dst,
                        target_duration=target_dur,
                        output_duration=target_dur,
                        out_w=output_width,
                        out_h=output_height,
                        layout=ms_layout,
                        bg_color=bg_color,
                    )
                clip_paths.append(dst)
                clip_scene_ids.append(scene_id)
                progress.advance(task)
                continue

            # sync 씬 처리: T_audio와 T_video를 기준으로 구간별 Time Warping 적용
            if sel.get("effect") == "sync":
                source_file = sel.get("source_file", "")
                trim_start = sel.get("trim_start", 0.0)
                trim_end = sel.get("trim_end")
                tts_dur_val = tts_dur if tts_dur is not None else sel.get("tts_duration_sec", 5.0)
                sync_video_sec = sel.get("sync_video_sec")

                # 영상 경로 해석
                src_path = Path(source_file)
                if not src_path.is_absolute() and not src_path.exists():
                    src_path = Path("footage") / Path(source_file).name
                if not src_path.exists():
                    console.print(f"  [red]씬 {scene_id}: sync 영상 파일을 찾을 수 없어요: {source_file}[/red]")
                    progress.advance(task)
                    continue

                actual_dur = get_duration(str(src_path))
                if trim_end is None or trim_end <= trim_start:
                    trim_end = actual_dur

                # narration_timeline에서 sync_word_sec (T_audio) 읽기
                narr_data_inner = load_json(NARRATION_TIMELINE_PATH)
                t_audio: float | None = None
                if narr_data_inner and narr_data_inner.get("entries"):
                    for entry in narr_data_inner["entries"]:
                        if entry.get("scene_id") == scene_id:
                            t_audio = entry.get("sync_word_sec")
                            break

                Path(dst).parent.mkdir(parents=True, exist_ok=True)

                # sync_video_sec 또는 t_audio가 없으면 단순 트림으로 폴백
                if sync_video_sec is None or t_audio is None:
                    console.print(
                        f"  [yellow]씬 {scene_id}: sync T_video 또는 T_audio 없음 — 단순 트림[/yellow]"
                    )
                    cut_clip(str(src_path), trim_start, trim_end, dst, mute=True,
                             target_duration=tts_dur_val, out_w=output_width, out_h=output_height, bg_color=bg_color)
                    clip_paths.append(dst)
                    clip_scene_ids.append(scene_id)
                    progress.advance(task)
                    continue

                # Time Warping 구현
                # 영상 구간 [trim_start, trim_end] 내에서 T_video를 기준으로 두 구간 분리
                # 전방 구간: [trim_start, trim_start + T_video]
                # 후방 구간: [trim_start + T_video, trim_end]
                D_audio = float(tts_dur_val)
                D_video = trim_end - trim_start
                T_v = float(sync_video_sec) - trim_start  # 영상 내 상대 T_video
                T_v = max(0.0, min(T_v, D_video))
                T_a = float(t_audio)
                T_a = max(0.0, min(T_a, D_audio))

                # 속도 비율 계산 (0.5x ~ 2.0x 클램핑)
                SPEED_MIN = 0.5
                SPEED_MAX = 2.0

                # 전방 구간 비율: T_audio / T_video (영상 T_v 구간을 T_a 시간에 맞게 조정)
                # setpts=PTS*N: N>1 → 느리게(영상 늘어남), N<1 → 빠르게(영상 줄어듦)
                if T_a > 0 and T_v > 0:
                    front_ratio_raw = T_a / T_v
                    front_ratio = max(SPEED_MIN, min(SPEED_MAX, front_ratio_raw))
                else:
                    front_ratio_raw = 1.0
                    front_ratio = 1.0

                # 후방 구간 비율
                rem_video = D_video - T_v
                rem_audio = D_audio - T_a
                if rem_audio > 0 and rem_video > 0:
                    back_ratio_raw = rem_audio / rem_video
                    back_ratio = max(SPEED_MIN, min(SPEED_MAX, back_ratio_raw))
                else:
                    back_ratio_raw = 1.0
                    back_ratio = 1.0

                # 속도 비율이 클램핑됐으면 싱크 포기 → 단순 트림으로 폴백
                if front_ratio != front_ratio_raw or back_ratio != back_ratio_raw:
                    console.print(
                        f"  [yellow]씬 {scene_id}: sync 비율 초과 "
                        f"(front={front_ratio_raw:.2f}x, back={back_ratio_raw:.2f}x) — 싱크 포기, 단순 트림[/yellow]"
                    )
                    cut_clip(str(src_path), trim_start, trim_end, dst, mute=True,
                             target_duration=D_audio, out_w=output_width, out_h=output_height, bg_color=bg_color)
                    clip_paths.append(dst)
                    clip_scene_ids.append(scene_id)
                    progress.advance(task)
                    continue

                console.print(
                    f"  [dim]씬 {scene_id}: sync Time Warp — "
                    f"T_a={T_a:.2f}s T_v={T_v:.2f}s "
                    f"front_pts={front_ratio:.3f} back_pts={back_ratio:.3f}[/dim]"
                )

                # FFmpeg filter_complex로 구간별 setpts 적용
                # 전방 구간과 후방 구간을 각각 추출 후 concat
                import tempfile as _tempfile
                import shutil as _shutil
                tmp_sync_dir = Path(_tempfile.mkdtemp(prefix="video_agent_sync_"))
                try:
                    front_dst = str(tmp_sync_dir / "front.mp4")
                    back_dst = str(tmp_sync_dir / "back.mp4")
                    warped_dst = str(tmp_sync_dir / "warped.mp4")

                    # 전방 구간 추출 + setpts
                    front_abs_start = trim_start
                    front_abs_end = trim_start + T_v
                    if front_abs_end > front_abs_start:
                        front_pts_expr = f"PTS*{front_ratio:.6f}"
                        front_cmd = [
                            ffmpeg_bin(), "-y",
                            "-ss", str(front_abs_start),
                            "-to", str(front_abs_end),
                            "-i", str(src_path),
                            "-vf", f"setpts={front_pts_expr},fps=30,scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2{_bg_pad_suffix}",
                            "-an", *_video_encoder_args(60),
                            "-pix_fmt", "yuv420p",
                            front_dst,
                        ]
                        front_result = subprocess.run(front_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if front_result.returncode != 0:
                            raise RuntimeError(f"sync 전방 구간 처리 실패: {front_result.stderr[:200]}")
                    else:
                        # 전방 구간이 없으면 빈 클립 대신 back만 사용
                        front_dst = None  # type: ignore

                    # 후방 구간 추출 + setpts
                    back_abs_start = trim_start + T_v
                    back_abs_end = trim_end
                    if back_abs_end > back_abs_start:
                        back_pts_expr = f"PTS*{back_ratio:.6f}"
                        back_cmd = [
                            ffmpeg_bin(), "-y",
                            "-ss", str(back_abs_start),
                            "-to", str(back_abs_end),
                            "-i", str(src_path),
                            "-vf", f"setpts={back_pts_expr},fps=30,scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2{_bg_pad_suffix}",
                            "-an", *_video_encoder_args(60),
                            "-pix_fmt", "yuv420p",
                            back_dst,
                        ]
                        back_result = subprocess.run(back_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if back_result.returncode != 0:
                            raise RuntimeError(f"sync 후방 구간 처리 실패: {back_result.stderr[:200]}")
                    else:
                        back_dst = None  # type: ignore

                    # 두 구간 concat
                    valid_parts = [p for p in [front_dst, back_dst] if p is not None]
                    if len(valid_parts) == 0:
                        raise RuntimeError("sync: 유효한 구간이 없어요")
                    elif len(valid_parts) == 1:
                        _shutil.copy2(valid_parts[0], warped_dst)
                    else:
                        _simple_concat(valid_parts, warped_dst)

                    # 보정 후 길이가 D_audio보다 짧으면 마지막 프레임 freeze로 패딩
                    warped_actual = get_duration(warped_dst)
                    if warped_actual < D_audio - 0.05:
                        pad_dur = D_audio - warped_actual
                        console.print(
                            f"  [dim]씬 {scene_id}: sync 길이 부족 — "
                            f"마지막 프레임 freeze {pad_dur:.2f}s 패딩[/dim]"
                        )
                        freeze_dst = str(tmp_sync_dir / "freeze.mp4")
                        freeze_cmd = [
                            ffmpeg_bin(), "-y",
                            "-i", warped_dst,
                            "-vf", f"tpad=stop_mode=clone:stop_duration={pad_dur:.3f},fps=30",
                            "-an", *_video_encoder_args(60),
                            "-pix_fmt", "yuv420p",
                            freeze_dst,
                        ]
                        freeze_result = subprocess.run(freeze_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
                        if freeze_result.returncode == 0:
                            _shutil.copy2(freeze_dst, warped_dst)

                    _shutil.copy2(warped_dst, dst)
                finally:
                    _shutil.rmtree(tmp_sync_dir, ignore_errors=True)

                clip_paths.append(dst)
                clip_scene_ids.append(scene_id)
                progress.advance(task)
                continue

            # 단일 footage 씬 처리 (기존 로직)
            clip_dur = sel.get("clip_duration_sec", sel["trim_end"] - sel["trim_start"])

            # 나레이션은 앞 디졸브 완료 후 시작, 뒤 디졸브 구간도 확보해야 하므로
            # 최소 영상 길이 = TTS + DISSOLVE_SEC * 2
            min_needed = (tts_dur + DISSOLVE_SEC * 2) if tts_dur is not None else None

            if min_needed is not None and clip_dur < min_needed:
                # 원본에 오디오가 있으면 영상을 늘릴 경우 사운드 늘어짐 현상 발생
                # → 영상 길이 그대로 유지하고 경고만 출력 (TTS 편집은 Phase 4 소관)
                from .utils.ffmpeg_utils import has_audio
                if has_audio(sel["source_file"]):
                    console.print(
                        f"  [yellow]씬 {scene_id}: 원본 오디오 있음 — "
                        f"영상({clip_dur:.2f}s) < TTS+dissolve*2({min_needed:.2f}s)이지만 "
                        f"사운드 늘어짐 방지를 위해 영상 길이 유지[/yellow]"
                    )
                    target = None
                else:
                    # 원본 오디오 없음 → 영상을 TTS + DISSOLVE_SEC * 2 길이로 늘림
                    target = min_needed
            elif tts_dur is not None and clip_dur < tts_dur:
                # clip_dur < tts_dur 인 경우 슬로모션
                target = tts_dur
            else:
                target = None

            cut_clip(sel["source_file"], sel["trim_start"], sel["trim_end"], dst, mute=True,
                     target_duration=target, out_w=output_width, out_h=output_height, bg_color=bg_color)

            # 원본 오디오(현장음) 별도 추출 → output/clips/scene_NNN_src.wav
            _extract_src_audio(
                sel["source_file"], sel["trim_start"], sel["trim_end"],
                scene_id, target_duration=target,
            )

            clip_paths.append(dst)
            clip_scene_ids.append(scene_id)
            progress.advance(task)

        if _cut_prev is not None:
            _timing_add("1_클립생성(자르기·배속)", _cut_prev[0], _time_t.perf_counter() - _cut_prev[1])
        # 씬 편집 루프 완료 — 100% 도달 알림.
        if progress_cb is not None:
            try:
                progress_cb(len(selections), len(selections), None)
            except Exception:
                pass

    # 클립이 만들어지지 않은 씬이 하나라도 있으면 여기서 중단한다.
    # 아래 후처리(필터·모션·오버레이·타임라인)는 selections 와 clip_paths 를 zip 으로 순서대로
    # 짝짓는다. 씬 하나가 빠지면 뒤의 모든 씬이 한 칸씩 밀려 엉뚱한 클립에 자막·효과·나레이션이
    # 붙고, 끝 씬들은 타임라인에서 통째로 빠져 나레이션이 0초에 겹친다(실측 2026-11-05:
    # 켄번스 2개 실패 → 순서 뒤섞임·소리 겹침). 엉망인 결과물을 내느니 실패를 알린다.
    _missing = [sel["scene_id"] for sel in selections if sel["scene_id"] not in set(clip_scene_ids)]
    if _missing or len(clip_paths) != len(selections):
        raise RuntimeError(
            f"씬 {', '.join(str(s) for s in _missing) or '?'} 의 클립을 만들지 못해 렌더를 중단합니다. "
            f"위 로그의 해당 씬 오류를 확인하세요. (클립 {len(clip_paths)}개 / 씬 {len(selections)}개)"
        )

    # 씬 필터 후처리 (#206): 씬별 filters가 있으면 ffmpeg vf chain으로 색감/무드 필터 적용
    # 적용 순서상 텍스트/도형/그래픽 오버레이 *이전*에 적용해야 함 (전체 색감 위에 오버레이가 얹힘)
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        filters = sel.get("filters", [])
        if not filters:
            continue
        flt_dst = clip_path.replace(".mp4", "_flt.mp4")
        _t0 = _time_t.perf_counter()
        success = apply_scene_filters(
            clip_path, flt_dst, filters, output_width, output_height
        )
        _timing_add("2_필터", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(flt_dst).exists():
            import shutil as _flt_sh
            _flt_sh.move(flt_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 씬 필터 {len(filters)}개 적용[/dim]")

    # 씬 모션 후처리 (#207, #347): 씬별 motions(shake/clip_zoom/stutter)을 적용한다.
    # 적용 순서: filters → motions → text/shape/graphic overlays.
    # shake/clip_zoom/stutter 렌더링 (영상 길이 보존). slowmo는 함수 내 분기 처리.
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        motions = sel.get("motions", [])
        if not motions:
            continue
        mo_dst = clip_path.replace(".mp4", "_mo.mp4")
        _t0 = _time_t.perf_counter()
        success = apply_scene_motions(
            clip_path, mo_dst, motions, output_width, output_height
        )
        _timing_add("3_모션(줌·흔들림)", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(mo_dst).exists():
            import shutil as _mo_sh
            _mo_sh.move(mo_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 씬 모션 {len(motions)}개 적용[/dim]")

    # 모자이크 오버레이 후처리 (#264): 씬별 mosaic_overlays(rect/ellipse, pixelate/blur)를
    # filter_complex로 적용한다. 적용 순서는 motions 다음, text/shape/graphic overlay *이전*.
    # 이렇게 두면 motion(흔들림/줌)으로 변형된 화면 위에 모자이크가 얹히고, 그 위에 텍스트/도형/그래픽이 올라간다.
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        mosaic_overlays = sel.get("mosaic_overlays", [])
        if not mosaic_overlays:
            continue
        mo_dst = clip_path.replace(".mp4", "_mos.mp4")
        _t0 = _time_t.perf_counter()
        success = apply_mosaic_overlays(
            clip_path, mo_dst, mosaic_overlays, output_width, output_height
        )
        _timing_add("4_모자이크", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(mo_dst).exists():
            import shutil as _mos_sh
            _mos_sh.move(mo_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 모자이크 오버레이 {len(mosaic_overlays)}개 적용[/dim]")

    # 이미지 오버레이 후처리 (ImageOverlay): footage 스튜디오의 이미지 자산을 씬 위에 합성.
    # 적용 순서: mosaic 다음, text/shape/graphic overlay *이전*. 이미지 오버레이는 다른 텍스트/도형/
    # 그래픽 오버레이보다 *아래*에 깔리므로, 썸네일 위에 자막 텍스트나 도형 장식을 자연스럽게 얹을 수 있다.
    # 각 overlay dict에는 호출 측(_build_sel)이 `_image_path` 절대 경로를 주입해야 한다.
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        image_overlays = sel.get("image_overlays", [])
        if not image_overlays:
            continue
        im_dst = clip_path.replace(".mp4", "_im.mp4")
        _t0 = _time_t.perf_counter()
        success = apply_image_overlays(
            clip_path, im_dst, image_overlays, output_width, output_height
        )
        _timing_add("5_이미지오버레이", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(im_dst).exists():
            import shutil as _im_sh
            _im_sh.move(im_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 이미지 오버레이 {len(image_overlays)}개 적용[/dim]")

    # 텍스트 오버레이 후처리: 씬별 text_overlays가 있으면 drawtext/Pillow overlay 적용
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        text_overlays = sel.get("text_overlays", [])
        if not text_overlays:
            continue
        ov_dst = clip_path.replace(".mp4", "_ov.mp4")
        clip_duration = get_duration(clip_path)
        _t0 = _time_t.perf_counter()
        success = apply_text_overlays(
            clip_path, ov_dst, text_overlays, output_width, output_height, clip_duration
        )
        _timing_add("6_텍스트오버레이", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(ov_dst).exists():
            import shutil as _ov_sh
            _ov_sh.move(ov_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 텍스트 오버레이 {len(text_overlays)}개 적용[/dim]")

    # 도형 오버레이 후처리: 씬별 shape_overlays가 있으면 Pillow PNG overlay 적용
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        shape_overlays = sel.get("shape_overlays", [])
        if not shape_overlays:
            continue
        sh_dst = clip_path.replace(".mp4", "_sh.mp4")
        _t0 = _time_t.perf_counter()
        success = apply_shape_overlays(
            clip_path, sh_dst, shape_overlays, output_width, output_height
        )
        _timing_add("7_도형오버레이", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(sh_dst).exists():
            import shutil as _sh_ov
            _sh_ov.move(sh_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 도형 오버레이 {len(shape_overlays)}개 적용[/dim]")

    # 그래픽 오버레이 후처리: 씬별 graphic_overlays(스포트라이트/알림 등) 적용 (#205)
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        graphic_overlays = sel.get("graphic_overlays", [])
        if not graphic_overlays:
            continue
        gr_dst = clip_path.replace(".mp4", "_gr.mp4")
        _t0 = _time_t.perf_counter()
        success = apply_graphic_overlays(
            clip_path, gr_dst, graphic_overlays, output_width, output_height
        )
        _timing_add("8_그래픽오버레이", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(gr_dst).exists():
            import shutil as _gr_ov
            _gr_ov.move(gr_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 그래픽 오버레이 {len(graphic_overlays)}개 적용[/dim]")

    # 집중선(속도선) 오버레이 후처리 — 만화 박진감. 모든 오버레이 중 최상단에 얹는다.
    # (그래픽 오버레이 다음). 그래야 화면 전체를 덮는 집중선이 다른 요소 위로 뻗어 강조된다.
    for i, (sel, clip_path) in enumerate(zip(selections, clip_paths)):
        scene_id = sel["scene_id"]
        speed_lines = sel.get("speed_lines", [])
        if not speed_lines:
            continue
        sl_dst = clip_path.replace(".mp4", "_sl.mp4")
        _t0 = _time_t.perf_counter()
        success = apply_speed_lines(
            clip_path, sl_dst, speed_lines, output_width, output_height
        )
        _timing_add("9_집중선", scene_id, _time_t.perf_counter() - _t0)
        if success and Path(sl_dst).exists():
            import shutil as _sl_ov
            _sl_ov.move(sl_dst, clip_path)
            console.print(f"  [dim]씬 {scene_id}: 집중선 오버레이 {len(speed_lines)}개 적용[/dim]")

    console.print("  xfade dissolve 효과 적용하며 클립 연결 중...")
    _t0 = _time_t.perf_counter()
    clip_start_times = _concat_with_dissolve_group_aware(
        clip_paths, clip_scene_ids, scene_group_ids, MUTED_OUTPUT, DISSOLVE_SEC
    )
    _timing_add("10_클립연결(디졸브)", "전체", _time_t.perf_counter() - _t0)
    _timing_report()
    # #415 — 클립 생성 캐시 크기 관리. 미리보기 캐시와 같은 LRU(프로젝트별 1GB) 정책.
    try:
        from .render_spec import preview_stages as _stages_lru
        _stages_lru.enforce_lru(Path(RENDER_CLIP_CACHE_DIR))
    except Exception:
        pass

    # 타임스탬프 계산: _concat_with_dissolve_group_aware가 반환한 실제 시작 시각 사용
    timestamps = []
    for idx, (sel, clip_path, start) in enumerate(zip(selections, clip_paths, clip_start_times)):
        dur = get_duration(clip_path)
        scene_id = sel["scene_id"]
        end = start + dur

        # 현장음 WAV 경로 — 존재하면 포함, 없으면 None
        src_wav = str(Path(CLIPS_DIR) / f"scene_{scene_id:03d}_src.wav")
        src_audio_file = src_wav if Path(src_wav).exists() else None
        # 씬별 footage 원본 오디오 음량 (0=음소거, 0.3=기본 더킹, 1.0=풀). 키 없으면 0.3.
        fav = float(sel.get("footage_audio_volume", 0.3)) if isinstance(sel, dict) else 0.3
        timestamps.append({
            "scene_id": scene_id,
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "src_audio_file": src_audio_file,
            "footage_audio_volume": fav,
        })

    save_json(TIMELINE_PATH, {"timestamps": timestamps})

    # 나레이션 타임라인의 start_sec/end_sec를 실제 영상 타임라인 기준으로 갱신
    _update_narration_timing(timestamps)

    # SBV 자막 타임코드를 갱신된 나레이션 타임라인 기준으로 재생성.
    # 단, 환경변수 VAG_SKIP_SUBTITLE=1이면 자막 생성 스킵(렌더 요청에서 "영상만").
    import os as _os_sbv
    if _os_sbv.environ.get("VAG_SKIP_SUBTITLE") == "1":
        # 이전 잔재 sbv가 남아 있으면 삭제 — "자막 없음"이 명확해지도록.
        try:
            Path("output/final_output.sbv").unlink(missing_ok=True)
        except OSError:
            pass
    else:
        from . import sbv_generator
        sbv_generator.run(force=True)

    return timestamps


def _update_narration_timing(timestamps: list) -> None:
    """Phase 4-A에서 생성된 나레이션 타임라인의 start_sec/end_sec를
    실제 영상 타임라인(timestamps) 기준으로 갱신한다.

    xfade dissolve 방식은 타임스탬프에 dissolve 오프셋이 이미 반영되어 있으므로
    timeline의 start_sec + NARRATION_PADDING을 그대로 나레이션 시작 시각으로 사용한다.

    씬 N의 나레이션 end_sec가 씬 N+1의 나레이션 start_sec를 넘어가면
    씬 N의 end_sec를 씬 N+1 start_sec 직전으로 잘라 겹침을 방지한다.
    """
    from .utils.file_cache import load_json, save_json

    narr_path = "output/narration/narration_timeline.json"
    narr_data = load_json(narr_path)
    if not narr_data:
        return

    ts_map = {t["scene_id"]: t for t in timestamps}

    # 1패스: start_sec / end_sec 초기 계산
    updated = []
    for entry in narr_data["entries"]:
        ts = ts_map.get(entry["scene_id"])
        if ts:
            narr_start = ts["start_sec"] + NARRATION_PADDING
            entry["start_sec"] = round(narr_start, 3)
            entry["end_sec"] = round(narr_start + entry["duration_sec"], 3)
        updated.append(entry)

    # 2패스: 인접 씬 나레이션 겹침 보정
    # 씬 N end_sec > 씬 N+1 start_sec 이면 씬 N end_sec를 씬 N+1 start_sec로 잘라냄
    for i in range(len(updated) - 1):
        cur = updated[i]
        nxt = updated[i + 1]
        if cur.get("end_sec", 0) > nxt.get("start_sec", float("inf")):
            original_end = cur["end_sec"]
            cur["end_sec"] = nxt["start_sec"]
            console.print(
                f"  [yellow]씬 {cur['scene_id']} 나레이션 겹침 보정: "
                f"end {original_end:.3f}s → {cur['end_sec']:.3f}s "
                f"(씬 {nxt['scene_id']} 시작 기준)[/yellow]"
            )

    save_json(narr_path, {"entries": updated})


def print_summary(timestamps: list, project_root: str) -> None:
    """편집 결과 요약 출력."""
    total = timestamps[-1]["end_sec"] if timestamps else 0

    console.print()
    console.rule("[bold green]✅ Phase 5 완료 — 무음 편집본 생성[/bold green]")
    console.print(f"  총 길이: [bold]{total:.1f}초[/bold] ({int(total) // 60}분 {int(total) % 60}초)")
    console.print(f"  씬 수: {len(timestamps)}개\n")

    for t in timestamps:
        console.print(f"  씬 {t['scene_id']:2d}  {t['start_sec']:.1f}s ~ {t['end_sec']:.1f}s")

    muted_abs = str(Path(project_root) / MUTED_OUTPUT)
    console.print(f"\n  🎬 무음 편집본 확인 → [link=cursor://file/{muted_abs}]cursor://file/{muted_abs}[/link]")
    console.print("  (영상 플레이어로 열어서 씬 순서와 화면 구성을 확인해보세요.)")


if __name__ == "__main__":
    project_root = str(Path(__file__).parent.parent)
    result = run()
    print_summary(result, project_root)
