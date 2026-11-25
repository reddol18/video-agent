"""FFmpeg / FFprobe 래퍼 함수 모음"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

# 6자리 hex 컬러(#RRGGBB) 매칭용. 프론트 컬러피커는 항상 #RRGGBB 를 준다.
_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


# ── ffmpeg/ffprobe 실행 경로 중앙화 (#ffmpeg-path) ────────────────────────────
# PATH 에 구버전 ffmpeg(예: anaconda 의 4.3.1)이 먼저 걸리면 amix 의 normalize 처럼
# 4.4+ 전용 옵션이 "Option 'normalize' not found" 로 조용히 터진다. 그래서 실행
# 경로를 한 곳에서 결정한다. 해석은 모듈 import 가 아니라 첫 호출 시점에 1회만
# 수행하고 lru_cache 로 캐시한다(디스크 glob 반복 방지).
#
# repo_root = <repo>/src/utils/ffmpeg_utils.py → parents[2] == <repo>
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _is_runnable(path: Path) -> bool:
    """해당 경로가 실제 실행 가능한 파일인지 확인한다(권한/타입 포함)."""
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    # Windows 는 X_OK 가 사실상 무의미하므로 파일 존재만 확인한다.
    if sys.platform == "win32":
        return True
    return os.access(str(path), os.X_OK)


def _bundled_candidates(tool: str) -> list[Path]:
    """<repo>/tools/ffmpeg-*/bin/<tool>[.exe] 후보를 최신 버전 우선으로 반환.

    디렉터리 이름 역순 정렬(ffmpeg-9.0.2-... > ffmpeg-7.1-...)로 최신을 앞에 둔다.
    """
    exe = f"{tool}.exe" if sys.platform == "win32" else tool
    tool_dirs = sorted(
        (p for p in _REPO_ROOT.glob("tools/ffmpeg-*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    return [d / "bin" / exe for d in tool_dirs]


def _resolve_tool(tool: str, env_var: str) -> str:
    """ffmpeg/ffprobe 실행 경로를 우선순위대로 해석한다.

    1. 환경변수(env_var) — 운영자 명시 오버라이드
    2. 프로젝트 번들 <repo>/tools/ffmpeg-*/bin/ (여러 버전이면 최신 우선)
    3. shutil.which(tool) — PATH 폴백
    4. tool 문자열 그대로 (기존 동작과 동일한 마지막 폴백)
    """
    override = os.getenv(env_var)
    if override:
        cand = Path(override.strip('"').strip())
        if _is_runnable(cand):
            return str(cand)
        # 경로가 아니라 명령 이름을 준 경우도 허용.
        which_override = shutil.which(override)
        if which_override:
            return which_override

    for cand in _bundled_candidates(tool):
        if _is_runnable(cand):
            return str(cand)

    found = shutil.which(tool)
    if found:
        return found

    return tool


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    """사용할 ffmpeg 실행 경로. 첫 호출 시 1회 해석 후 캐시."""
    return _resolve_tool("ffmpeg", "VIDEO_AGENT_FFMPEG")


@lru_cache(maxsize=1)
def ffprobe_bin() -> str:
    """사용할 ffprobe 실행 경로. 첫 호출 시 1회 해석 후 캐시."""
    return _resolve_tool("ffprobe", "VIDEO_AGENT_FFPROBE")


def normalize_bg_color(color: str | None) -> str:
    """프로젝트 배경색을 ffmpeg pad/xstack 이 받는 색 토큰으로 정규화한다.

    - `#RRGGBB` → `0xRRGGBB` (pad/xstack fill 에 안전한 형식)
    - `black` 등 named 값 / None / 빈 문자열 → `black` (기본값·하위호환)
    - 그 외 알 수 없는 값은 안전하게 `black` 으로 폴백.

    반환값이 `black` 이면 호출부는 기존과 100% 동일한 문자열(암묵/명시 black)을
    유지해야 회귀가 없다. `0x..` 이면 그 색을 명시적으로 pad 에 넣는다.
    """
    if not color:
        return "black"
    c = str(color).strip()
    if not c:
        return "black"
    m = _HEX_COLOR_RE.match(c)
    if m:
        return "0x" + m.group(1).upper()
    if c.lower() == "black":
        return "black"
    # named 컬러(white, red 등)는 ffmpeg 가 그대로 이해하므로 통과.
    if re.match(r"^[a-zA-Z]+$", c):
        return c.lower()
    # 알 수 없는 형식은 회귀 안전을 위해 기본 black.
    return "black"


# ── N×M 그리드 비교 + 광원 그라데이션 보더 (#grid-compare) ────────────────────
# 순수 함수(파일/프로세스 부작용 없음) — 좌표·색·필터 문자열 계산만 담당하여 단위 테스트 가능.

def normalize_gradient_color(color: str | None, fallback: str = "0x00E5FF") -> str:
    """그라데이션 보더 색을 ffmpeg gradients 필터가 받는 토큰으로 정규화.

    - `#RRGGBB` → `0xRRGGBB`
    - named 컬러(cyan, purple 등) → 소문자 그대로 통과
    - None/빈/알 수 없는 값 → fallback
    """
    if not color:
        return fallback
    c = str(color).strip()
    if not c:
        return fallback
    m = _HEX_COLOR_RE.match(c)
    if m:
        return "0x" + m.group(1).upper()
    if re.match(r"^[a-zA-Z]+$", c):
        return c.lower()
    return fallback


def compute_grid_cells(
    rows: int, cols: int, out_w: int, out_h: int
) -> list[tuple[int, int, int, int]]:
    """N×M 그리드의 각 셀 좌표/크기를 계산한다.

    행 우선(row-major) 순서로 (x, y, w, h) 튜플 리스트 반환.
    나머지 픽셀은 마지막 행/열에 흡수시켜 전체 out_w×out_h 를 정확히 덮는다.
    세로 영상(1080×1920)에서도 셀 경계가 정확하도록 정수 픽셀로 배분.
    """
    if rows < 1 or cols < 1:
        raise ValueError(f"rows/cols 는 1 이상이어야 합니다: rows={rows}, cols={cols}")

    base_w = out_w // cols
    base_h = out_h // rows

    # 각 열/행 경계 좌표 (마지막은 out_w/out_h 로 맞춤)
    x_edges = [c * base_w for c in range(cols)] + [out_w]
    y_edges = [r * base_h for r in range(rows)] + [out_h]

    cells: list[tuple[int, int, int, int]] = []
    for r in range(rows):
        for c in range(cols):
            x = x_edges[c]
            y = y_edges[r]
            w = x_edges[c + 1] - x
            h = y_edges[r + 1] - y
            cells.append((x, y, w, h))
    return cells


def build_grid_cell_filter(
    input_idx: int,
    cell_w: int,
    cell_h: int,
    out_label: str,
    pts_ratio: float,
    border: dict | None = None,
    bg_color: str = "black",
) -> list[str]:
    """그리드 셀 하나에 대한 filter_complex 파트 목록을 생성한다.

    border 가 없거나 width_px<=0 이면: 단순히 셀 크기로 스케일/패딩만 한다(기존 동작 동등).
    border 가 있으면: gradients 소스를 셀 크기로 깔고, (width_px 만큼 안쪽에) 이미지를
    overlay 하여 바깥 테두리만 그라데이션이 노출되게 한다.

    광원 애니메이션은 hue=H=2*PI*speed*t 회전으로 구현한다(검증된 최소 필터).

    - input_idx: filter_complex 입력 스트림 인덱스 (예: 0 → [0:v])
    - out_label: 이 셀의 출력 라벨 (예: "cell0") — 대괄호 없이 이름만.
    - pts_ratio: 재생 속도 계수 (target_duration / src_dur)
    반환: filter_parts (";" 로 join 하기 전의 개별 노드 문자열 리스트)
    """
    _bg = normalize_bg_color(bg_color)
    _pad_suffix = "" if _bg == "black" else f":{_bg}"

    bw = 0
    if border and int(border.get("width_px", 0) or 0) > 0:
        bw = int(border["width_px"])
        # 보더 두께가 셀 절반을 넘으면 콘텐츠가 사라지므로 안전 clamp.
        max_bw = min(cell_w, cell_h) // 2 - 1
        if max_bw < 0:
            max_bw = 0
        if bw > max_bw:
            bw = max_bw

    # 보더 없음: 기존 xstack 셀과 동일한 스케일/패딩만.
    if bw <= 0:
        parts = [
            f"[{input_idx}:v]setpts={pts_ratio:.6f}*PTS,"
            f"scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
            f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30[{out_label}]"
        ]
        return parts

    inner_w = cell_w - 2 * bw
    inner_h = cell_h - 2 * bw

    color_from = normalize_gradient_color(
        (border or {}).get("color_from"), "0x00E5FF"
    )
    color_to = normalize_gradient_color(
        (border or {}).get("color_to"), "0xA855F7"
    )
    animate = bool((border or {}).get("animate", True))
    try:
        speed = float((border or {}).get("speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        speed = 1.0

    # 그라데이션 소스: 셀 전체 크기. 광원 = hue 회전(animate).
    hue_suffix = f",hue=H=2*PI*{speed:.4f}*t" if animate else ""
    grad_label = f"{out_label}_g"
    content_label = f"{out_label}_c"
    parts = [
        f"gradients=s={cell_w}x{cell_h}:c0={color_from}:c1={color_to}"
        f":x0=0:y0=0:x1={cell_w}:y1={cell_h}{hue_suffix},fps=30[{grad_label}]",
        f"[{input_idx}:v]setpts={pts_ratio:.6f}*PTS,"
        f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease,"
        f"pad={inner_w}:{inner_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30[{content_label}]",
        f"[{grad_label}][{content_label}]overlay=x={bw}:y={bw}:shortest=1[{out_label}]",
    ]
    return parts


def probe(video_path: str) -> dict:
    """FFprobe로 영상 메타데이터 추출."""
    cmd = [
        ffprobe_bin(), "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {result.stderr}")
    return json.loads(result.stdout)


def get_video_size(video_path: str) -> "tuple[int, int] | None":
    """영상의 (width, height) 를 반환한다. 실패하거나 비디오 스트림이 없으면 None.

    오버레이 합성은 rawvideo 파이프를 width*height*3 바이트로 잘라 읽으므로,
    "선언한 캔버스 크기"와 "실제 디코딩되는 프레임 크기"가 어긋나면 바이트 경계가
    밀려 화면이 비스듬히 찢어진다. 그 사고를 막기 위한 실측 헬퍼.
    """
    try:
        info = probe(video_path)
    except Exception:
        return None
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            try:
                w = int(stream.get("width"))
                h = int(stream.get("height"))
            except (TypeError, ValueError):
                return None
            return (w, h) if w > 0 and h > 0 else None
    return None


def get_duration(video_path: str) -> float:
    """영상 총 길이(초) 반환. 비디오 스트림 우선, 없으면 오디오, 없으면 format."""
    import subprocess as _sp

    def _valid(val) -> float | None:
        try:
            f = float(val)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    info = probe(video_path)
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            v = _valid(stream.get("duration"))
            if v: return v
    for stream in info.get("streams", []):
        v = _valid(stream.get("duration"))
        if v: return v
    v = _valid(info.get("format", {}).get("duration"))
    if v: return v

    # webm 등 duration=N/A 폴백 — 마지막 패킷의 pts_time 읽기
    r = _sp.run(
        [ffprobe_bin(), "-select_streams", "v:0", "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    lines = [l.strip().rstrip(",") for l in r.stdout.splitlines() if l.strip()]
    if lines:
        v = _valid(lines[-1])
        if v: return v

    raise RuntimeError(f"duration을 읽을 수 없어요: {video_path}")


def has_audio(video_path: str) -> bool:
    """영상에 오디오 스트림이 있으면 True."""
    info = probe(video_path)
    return any(s["codec_type"] == "audio" for s in info.get("streams", []))


def get_video_dimensions(video_path: str) -> tuple[int, int]:
    """영상의 실제(가시) width, height 반환.

    컨테이너의 displaymatrix rotation(또는 legacy tags.rotate)이 90/270이면
    raw width/height를 swap하여 ffmpeg가 디코딩 시 실제 출력하는 프레임 차원과
    일치시킨다 (스마트폰 세로 영상 대응).
    """
    # ffprobe로 width/height + side_data rotation + legacy tags.rotate 한 번에 조회.
    r = subprocess.run(
        [
            ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
            "-of", "json", video_path,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"비디오 스트림을 찾을 수 없어요: {video_path}")
    s = streams[0]
    w = int(s.get("width") or 0)
    h = int(s.get("height") or 0)
    if w == 0 or h == 0:
        raise RuntimeError(f"비디오 차원을 읽을 수 없어요: {video_path}")

    rot = 0
    # side_data_list — ffmpeg 버전에 따라 side_data_type이 "Display Matrix"인 항목에
    # rotation 키가 들어있기도 하고, 일부 8.x JSON 출력은 type 키 없이 rotation만 노출함.
    # 따라서 rotation 키를 가진 첫 항목을 사용한다.
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd.get("rotation") or 0))
            except (TypeError, ValueError):
                rot = 0
            break
    # legacy tags.rotate (mp4)
    if rot == 0:
        try:
            rot = int((s.get("tags") or {}).get("rotate") or 0)
        except (TypeError, ValueError):
            rot = 0

    # 정규화 (음수/360 초과 모두 0~359 범위로) 후 90/270이면 swap.
    rot = ((rot % 360) + 360) % 360
    if rot in (90, 270):
        w, h = h, w
    return w, h


def _needs_rotation(src: str, target_w: int = 1920, target_h: int = 1080) -> bool:
    """영상을 90도 회전했을 때 target 비율에 더 가까워지면 True.

    세로 영상(portrait)을 가로로 돌렸을 때 target 종횡비와의 오차가
    원본보다 작으면 회전이 유리하다고 판단한다.
    """
    w, h = get_video_dimensions(src)
    if w == 0 or h == 0:
        return False

    target_ratio = target_w / target_h  # 1920/1080 = 16/9 ≈ 1.778

    orig_ratio = w / h
    rotated_ratio = h / w  # 90도 회전 시

    orig_err = abs(orig_ratio - target_ratio)
    rotated_err = abs(rotated_ratio - target_ratio)

    return rotated_err < orig_err


# cut_clip 의 비디오 인코더 인자. 디졸브 경계 재인코딩(#416 1안)이 같은 인자로 경계 조각을
# 인코딩해 스트림 복사 구간과 SPS/PPS 를 맞추므로 한 곳에서 정의한다.
CUT_CLIP_VIDEO_ARGS = ["-c:v", "libx264", "-crf", "18", "-preset", "fast"]
# 클립 앞뒤에 강제 키프레임을 넣을 위치(초). 디졸브 0.5s + 여유 프레임보다 커야 한다.
DISSOLVE_KEYFRAME_LEAD_SEC = 0.7


def cut_clip(
    src: str,
    start: float,
    end: float,
    dst: str,
    mute: bool = True,
    target_duration: float | None = None,
    out_w: int = 1920,
    out_h: int = 1080,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
    fit: str = "contain",
    rotation: int = 0,
    bg_color: str = "black",
) -> None:
    """src 영상에서 start~end 구간을 잘라 dst에 저장.

    - 해상도: out_w×out_h
    - 프레임레이트: 30fps
    - 코덱: H.264 CRF 23
    - mute=True면 오디오 제거
    - target_duration이 지정되면 setpts 필터로 영상 속도를 조절하여 target_duration에 맞춤
      (clip > target → speed-up, clip < target → 슬로모션)
    - 자동 회전 없음: 미리보기(RenderSpec builder)와 동일하게 수동 rotation 값만 존중한다.
      (구 #261 자동 회전 휴리스틱은 미리보기≡렌더 원칙 위반으로 제거됨)
    - x_offset/y_offset(-1.0~1.0 비율): footage 위치 이동 (fit=contain/cover에서만 유효)
    - fit (#258):
        "contain" — 비율 유지하며 letterbox/pillarbox 패딩 (기본, 기존 동작)
        "cover"   — 비율 유지하며 출력 영역을 가득 채우고 넘치는 부분은 잘라냄
        "stretch" — 비율 무시하고 출력 해상도에 맞게 늘림 (오프셋 무시)
    - rotation (#263): 0/90/180/270 도(수동 지정값).
      적용 순서: rotation → fit/scale → crop/pad. 회전 후의 종횡비를 기준으로 fit 처리되며,
      x_offset/y_offset도 회전 후 좌표계 기준으로 동작한다.
      잘못된 값(90의 배수가 아니거나 0/90/180/270 외)은 0으로 폴백.
    """
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    clip_duration = end - start

    # 재생 속도 배율 계산: target_duration이 지정된 경우 속도 조절 적용
    pts_ratio = 1.0
    if target_duration is not None and clip_duration > 0 and target_duration > 0:
        pts_ratio = target_duration / clip_duration

    # fit 모드 검증 (잘못된 값은 contain으로 폴백 — 회귀 보장)
    if fit not in ("contain", "cover", "stretch"):
        fit = "contain"

    # rotation 검증 (#263): 0/90/180/270만 허용. 그 외는 0으로 폴백.
    try:
        rotation_int = int(rotation)
    except (TypeError, ValueError):
        rotation_int = 0
    if rotation_int not in (0, 90, 180, 270):
        rotation_int = 0

    # 자동 회전 휴리스틱(#261) 제거 — 렌더를 미리보기(RenderSpec builder)와 일치시킴.
    # 미리보기 경로(builder._footage_to_layer)는 수동 footage.rotation 값만 존중하고
    # 자동 회전을 하지 않는다. 렌더만 _needs_rotation으로 자동 90도 회전을 하던 탓에
    # "미리보기 ≡ 렌더" 원칙이 깨졌다(세로 숏폼에 가로 footage를 넣으면 렌더만 옆으로
    # 눕는 버그). 이제 렌더도 수동 rotation 값만 사용해 미리보기와 동일하게
    # fit(contain=레터/필러박스)로 처리한다.
    effective_rotation = rotation_int

    # x/y offset(픽셀)
    x_px = int(x_offset * out_w)
    y_px = int(y_offset * out_h)

    # ── RenderSpec 매퍼 경로 (#303) ────────────────────────────────────────
    # offset == 0이면 RenderSpec 매퍼가 만든 vf chain을 그대로 사용한다.
    # 빌더가 fit을 (scale_x, scale_y, crop, pad) 픽셀로 풀고, 매퍼는 그 값을 ffmpeg
    # 필터로 직역만 한다. fit과 setpts(speed)가 매퍼 안에서 *완전히 분리된 단계*로
    # 적용되므로 fit-vs-speed 회귀(#fit-bug 시리즈)가 구조적으로 차단된다.
    # offset != 0이면 builder가 아직 offset을 지원하지 않으므로(후속 이슈) 기존 경로를
    # 유지한다 — 회귀 방지.
    use_render_spec_mapper = (x_px == 0 and y_px == 0)
    if use_render_spec_mapper:
        # local import: 순환 참조 회피 + 외부 의존(RenderSpec)을 ffmpeg_utils 상단에 끌어올리지 않음.
        from src.render_spec.models import Canvas as _Canvas, Size as _Size, VideoLayer as _VideoLayer
        from src.render_spec.builder import _compute_fit_geometry, _apply_rotation_to_size
        from src.render_spec.ffmpeg_mapper import video_layer_to_vf as _video_layer_to_vf

        _canvas = _Canvas(width=out_w, height=out_h, fps=30, bg_color=normalize_bg_color(bg_color))
        # 원본 해상도 측정. 실패해도 1920×1080 가정으로 폴백(매퍼는 픽셀만 보고 동작).
        try:
            src_w, src_h = get_video_dimensions(src)
        except Exception:
            src_w, src_h = 1920, 1080
        # 회전 후 가시 해상도(빌더와 같은 규약): rotation이 90/270이면 가로/세로 스왑.
        _visible = _apply_rotation_to_size(_Size(width=src_w, height=src_h), effective_rotation)
        _sx, _sy, _crop, _pad = _compute_fit_geometry(_canvas, _visible, fit)
        # cover에서 pad가 None인지, contain에서 crop이 None인지는 빌더가 보장.
        _layer = _VideoLayer(
            source=src,
            source_size=_visible,
            scale_x=_sx, scale_y=_sy,
            crop=_crop, pad=_pad,
            rotation_deg=effective_rotation,
            playback_rate=(1.0 / pts_ratio) if pts_ratio != 1.0 else 1.0,
            start_sec=0.0, end_sec=clip_duration,
        )
        vf = _video_layer_to_vf(_layer, _canvas)
    else:
        # ── 레거시 경로 (offset != 0 — RenderSpec builder에 offset 미지원) ─
        # 이 분기는 #302/#303의 범위 밖이라 기존 표현식 기반 vf 조립을 유지한다.
        if effective_rotation == 90:
            rotate_filter = "transpose=1,"
        elif effective_rotation == 180:
            rotate_filter = "hflip,vflip,"
        elif effective_rotation == 270:
            rotate_filter = "transpose=2,"
        else:
            rotate_filter = ""

        if fit == "stretch":
            base_vf = (
                f"{rotate_filter}scale={out_w}:{out_h},"
                f"setsar=1,fps=30"
            )
        elif fit == "cover":
            crop_x_expr = f"(iw-{out_w})/2+{x_px}"
            crop_y_expr = f"(ih-{out_h})/2+{y_px}"
            base_vf = (
                f"{rotate_filter}scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h}:{crop_x_expr}:{crop_y_expr},"
                f"setsar=1,fps=30"
            )
        else:
            abs_x = abs(x_px)
            abs_y = abs(y_px)
            pad_w = out_w + 2 * abs_x
            pad_h = out_h + 2 * abs_y
            pad_x = f"({pad_w}-iw)/2+{x_px}"
            pad_y = f"({pad_h}-ih)/2+{y_px}"
            base_vf = (
                f"{rotate_filter}scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"pad={pad_w}:{pad_h}:{pad_x}:{pad_y}:{normalize_bg_color(bg_color)},"
                f"crop={out_w}:{out_h}:{abs_x}:{abs_y},"
                f"fps=30"
            )

        if pts_ratio != 1.0:
            vf = f"setpts={pts_ratio:.6f}*PTS,{base_vf}"
        else:
            vf = base_vf

    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", str(start),
        "-to", str(end),
        "-i", src,
        "-vf", vf,
        # #xfade-sparse-fps — 출력 프레임레이트를 30 으로 못 박는다.
        #
        # setpts 로 크게 늘린 클립(짧은 소재를 슬로모션으로 확장)은 vf 의 fps=30 을
        # 거쳐도 컨테이너 avg_frame_rate 가 7~8fps 로 남는 경우가 있다.
        # 그런 클립이 섞이면 xfade 체인이 조용히 무너져 뒤쪽 입력이 통째로 사라진다
        # (실측: 청크 하나가 120.6s → 33.4s, 최종 영상이 7분44초에서 잘림).
        "-r", "30",
        *CUT_CLIP_VIDEO_ARGS,
    ]
    # #416 (1안) — 디졸브 경계 재인코딩용 키프레임. 연결 단계(clip_editor
    # _concat_with_dissolve_boundary)가 앞뒤 DISSOLVE_KEYFRAME_LEAD_SEC 안쪽 키프레임에서
    # 클립을 잘라 가운데는 스트림 복사, 경계만 xfade 재인코딩한다. 실제 자를 위치는
    # 연결 단계가 ffprobe 로 키프레임을 다시 읽어 정하므로 이 값은 속도 힌트일 뿐이다.
    out_dur = clip_duration * pts_ratio
    if out_dur > 2 * DISSOLVE_KEYFRAME_LEAD_SEC + 0.2:
        cmd += ["-force_key_frames",
                f"{DISSOLVE_KEYFRAME_LEAD_SEC:.3f},{out_dur - DISSOLVE_KEYFRAME_LEAD_SEC:.3f}"]
    if mute:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd.append(dst)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg cut_clip 실패: {result.stderr}")


def concat_clips(clip_paths: list[str], dst: str) -> None:
    """클립 목록을 순서대로 이어붙여 dst에 저장. concat demuxer 사용."""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{Path(p).resolve()}'\n")
        list_file = f.name
    cmd = [
        ffmpeg_bin(), "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        dst,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    Path(list_file).unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat 실패: {result.stderr}")


def transcode_for_gemini(src: str, dst: str, start: float | None = None, end: float | None = None) -> int:
    """Gemini 전송용 최소 압축 트랜스코딩. 결과 파일 크기(bytes) 반환.

    사용자 결정(2026-06): 인덱싱 비용·시간 최소화를 위해 압축률을 강하게 적용한다.
    환경변수로 오버라이드 가능 — 시그니처는 변경하지 않아 호출처 영향 없음.

    기본값:
      - 해상도: 360p (`scale={GEMINI_TRANSCODE_HEIGHT * 16 / 9 : GEMINI_TRANSCODE_HEIGHT}`, 기본 360 → 640x360).
        Gemini Vision 은 영상 인식에 360p 면 충분(공식 문서 권장도 360p 이상).
      - 비디오 비트레이트: 200k (기존 500k 의 40%).
      - 오디오: 32kbps mono 22050Hz — 음성/대화 분석엔 충분.
      - fps: 2 — Gemini 가 입력 fps 에 비례해 프레임 토큰을 산정할 가능성 있어 잠재적 절감.
        (사용자 결정 2026-06-05 재조정: 5 → 2. 인덱싱 1회 분석 용도엔 2fps 면 충분하고,
         TPM 한도 보호 차원에서 추가 절감.)

    환경변수:
      - GEMINI_TRANSCODE_HEIGHT (기본 360)
      - GEMINI_TRANSCODE_BITRATE (기본 "200k")
      - GEMINI_TRANSCODE_FPS (기본 2)
      - GEMINI_TRANSCODE_AUDIO_BITRATE (기본 "32k")

    ⚠️ Gemini 청구는 영상 길이 × 295 tok/sec 가 1차 기준이므로 압축 자체가 토큰 청구를
       직접 줄이지는 않는다. 단 (a) 업로드/처리 시간 단축, (b) 디스크 임시 사용 절감,
       (c) fps 다운샘플은 잠재적 토큰 절감 가능(보수적 추정).
    """
    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    # 환경변수 로드 — 잘못된 값은 기본값으로 폴백.
    try:
        height = int(os.getenv("GEMINI_TRANSCODE_HEIGHT", "360"))
        if height < 144:
            height = 360
    except (TypeError, ValueError):
        height = 360
    width = int(round(height * 16 / 9))
    # ffmpeg 는 width/height 가 짝수여야 함 — ceil 처리.
    if width % 2 == 1:
        width += 1
    if height % 2 == 1:
        height += 1

    bitrate = os.getenv("GEMINI_TRANSCODE_BITRATE", "200k") or "200k"
    audio_bitrate = os.getenv("GEMINI_TRANSCODE_AUDIO_BITRATE", "32k") or "32k"

    try:
        fps = int(os.getenv("GEMINI_TRANSCODE_FPS", "2"))
        if fps < 1:
            fps = 2
    except (TypeError, ValueError):
        fps = 2

    cmd = [ffmpeg_bin(), "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += [
        "-i", src,
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2,fps={fps}",
        "-b:v", bitrate,
        "-b:a", audio_bitrate, "-ac", "1", "-ar", "22050",
        "-c:v", "libx264", "-preset", "fast",
        dst,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg transcode 실패: {result.stderr}")
    return Path(dst).stat().st_size


def _build_diagonal_mask(
    out_w: int,
    out_h: int,
    direction: str,
    tmp_path: str,
) -> None:
    """대각선 분할용 마스크 PNG를 Pillow로 생성하여 tmp_path에 저장.

    direction:
      "lr" — 좌상→우하 대각선 (왼쪽 위 영역이 흰색)
      "rl" — 우상→좌하 대각선 (오른쪽 위 영역이 흰색)
    """
    try:
        from PIL import Image as _PILImage, ImageDraw as _PILDraw
    except ImportError:
        raise RuntimeError("대각선 레이아웃에는 Pillow가 필요합니다 (pip install Pillow)")

    mask = _PILImage.new("L", (out_w, out_h), 0)
    draw = _PILDraw.Draw(mask)

    if direction == "lr":
        # 좌상→우하 삼각형 (위-왼쪽 영역)
        draw.polygon([(0, 0), (out_w, 0), (0, out_h)], fill=255)
    else:
        # 우상→좌하 삼각형 (위-오른쪽 영역)
        draw.polygon([(0, 0), (out_w, 0), (out_w, out_h)], fill=255)

    mask.save(tmp_path)


def _build_xstack_filter_parts(
    clip_paths: list[str],
    target_duration: float,
    layout: str,
    out_w: int,
    out_h: int,
    bg_color: str = "black",
) -> tuple[list[str], str]:
    """레이아웃에 따른 FFmpeg filter_complex 파트 목록과 최종 출력 레이블을 반환한다.

    반환값: (filter_parts, output_label)
    """
    import math

    n = len(clip_paths)

    # 배경색 정규화. black(기본)이면 pad 접미사를 붙이지 않아 기존 문자열과 바이트 동일.
    _bg = normalize_bg_color(bg_color)
    _pad_suffix = "" if _bg == "black" else f":{_bg}"

    def _pts_ratio(path: str) -> float:
        src_dur = get_duration(path)
        if src_dur <= 0:
            src_dur = target_duration
        return target_duration / src_dur if src_dur > 0 else 1.0

    # ──── 직사각형 분할 레이아웃 (xstack) ────────────────────────────────────
    # 셀 정의: [(x, y, w, h), ...] 형식
    def _xstack_from_cells(cells: list[tuple[int, int, int, int]]) -> tuple[list[str], str]:
        """셀 정의로부터 xstack filter_parts와 출력 레이블 반환."""
        parts = []
        layout_parts = []
        for i, (x, y, cw, ch) in enumerate(cells[:n]):
            pr = _pts_ratio(clip_paths[i])
            parts.append(
                f"[{i}:v]setpts={pr:.6f}*PTS,"
                f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30[v{i}]"
            )
            layout_parts.append(f"{x}_{y}")
        inputs_str = "".join(f"[v{i}]" for i in range(min(n, len(cells))))
        layout_str = "|".join(layout_parts[:n])
        parts.append(
            f"{inputs_str}xstack=inputs={min(n,len(cells))}:layout={layout_str}:fill={_bg}[vout]"
        )
        return parts, "[vout]"

    if layout == "left_right":
        half_w = out_w // 2
        cells = [(0, 0, half_w, out_h), (half_w, 0, out_w - half_w, out_h)]
        return _xstack_from_cells(cells)

    if layout == "top_bottom":
        half_h = out_h // 2
        cells = [(0, 0, out_w, half_h), (0, half_h, out_w, out_h - half_h)]
        return _xstack_from_cells(cells)

    if layout == "big_left":
        big_w = (out_w * 2) // 3
        small_w = out_w - big_w
        cells = [(0, 0, big_w, out_h), (big_w, 0, small_w, out_h)]
        return _xstack_from_cells(cells)

    if layout == "big_right":
        small_w = out_w // 3
        big_w = out_w - small_w
        cells = [(0, 0, small_w, out_h), (small_w, 0, big_w, out_h)]
        return _xstack_from_cells(cells)

    if layout == "grid2x2":
        hw = out_w // 2
        hh = out_h // 2
        cells = [
            (0, 0, hw, hh), (hw, 0, out_w - hw, hh),
            (0, hh, hw, out_h - hh), (hw, hh, out_w - hw, out_h - hh),
        ]
        return _xstack_from_cells(cells)

    if layout == "triptych":
        w1 = out_w // 3
        w2 = out_w // 3
        w3 = out_w - w1 - w2
        cells = [(0, 0, w1, out_h), (w1, 0, w2, out_h), (w1 + w2, 0, w3, out_h)]
        return _xstack_from_cells(cells)

    # ──── 대각선 분할 ─────────────────────────────────────────────────────────
    if layout in ("diagonal_lr", "diagonal_rl"):
        direction = "lr" if layout == "diagonal_lr" else "rl"
        # 두 클립 각각을 전체 해상도로 스케일 후 마스크 합성
        parts = []
        for i in range(min(n, 2)):
            pr = _pts_ratio(clip_paths[i])
            parts.append(
                f"[{i}:v]setpts={pr:.6f}*PTS,"
                f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30[base{i}]"
            )
        # 마스크 PNG를 임시 파일로 생성한 경로를 호출 측에서 -i로 추가해야 하므로
        # 특별 마커 반환: filter_parts에 "DIAGONAL_MASK_{direction}" 태그 포함
        parts.append(f"DIAGONAL_MASK_{direction.upper()}")
        return parts, f"[diagout]"

    # ──── PIP (Picture-in-Picture) ─────────────────────────────────────────────
    if layout in ("pip_br", "pip_bl"):
        pip_w = out_w // 4
        pip_h = out_h // 4
        margin = 20
        if layout == "pip_br":
            pip_x = out_w - pip_w - margin
        else:
            pip_x = margin
        pip_y = out_h - pip_h - margin

        parts = []
        # 메인 클립 (인덱스 0)
        pr0 = _pts_ratio(clip_paths[0])
        parts.append(
            f"[0:v]setpts={pr0:.6f}*PTS,"
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30[main]"
        )
        if n >= 2:
            pr1 = _pts_ratio(clip_paths[1])
            parts.append(
                f"[1:v]setpts={pr1:.6f}*PTS,"
                f"scale={pip_w}:{pip_h}:force_original_aspect_ratio=decrease,"
                f"pad={pip_w}:{pip_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30[pip]"
            )
            parts.append(f"[main][pip]overlay={pip_x}:{pip_y}[vout]")
        else:
            parts.append("[main]copy[vout]")
        return parts, "[vout]"

    # ──── auto (기존 균등 격자) ────────────────────────────────────────────────
    if n == 2:
        cols, rows = 2, 1
    elif n <= 4:
        cols, rows = 2, 2
    else:
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

    cell_w = out_w // cols
    cell_h = out_h // rows

    parts = []
    for i in range(n):
        pr = _pts_ratio(clip_paths[i])
        parts.append(
            f"[{i}:v]setpts={pr:.6f}*PTS,"
            f"scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
            f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30[v{i}]"
        )

    layout_parts_list = []
    for i in range(n):
        row_i = i // cols
        col_i = i % cols
        x = str(col_i * cell_w) if col_i > 0 else "0"
        y = str(row_i * cell_h) if row_i > 0 else "0"
        layout_parts_list.append(f"{x}_{y}")

    inputs_str = "".join(f"[v{i}]" for i in range(n))
    layout_str_val = "|".join(layout_parts_list)
    parts.append(
        f"{inputs_str}xstack=inputs={n}:layout={layout_str_val}:fill={_bg}[vout]"
    )
    return parts, "[vout]"


def build_xstack_multiscreen(
    clip_paths: list[str],
    dst: str,
    target_duration: float,
    output_duration: float | None = None,
    out_w: int = 1920,
    out_h: int = 1080,
    layout: str = "auto",
    bg_color: str = "black",
) -> None:
    """여러 클립을 지정된 레이아웃으로 분할 화면 합성하여 dst에 저장.

    각 클립은 target_duration에 맞춰 재생 속도가 자동 조절된다.
    (모든 영상이 동시에 시작해서 동시에 종료)

    - target_duration: 속도 계산 기준 (pts_ratio = target_duration / src_dur)
    - output_duration: 실제 출력 클립 길이 (-t 옵션). None이면 target_duration 사용.
    - layout: 레이아웃 이름.
        "auto"        — 클립 수 기반 자동 균등 격자 (기존 동작)
        "left_right"  — 좌우 균등 분할 (2개)
        "top_bottom"  — 상하 균등 분할 (2개)
        "diagonal_lr" — 좌상→우하 대각선 분할 (2개, Pillow 마스크 사용)
        "diagonal_rl" — 우상→좌하 대각선 분할 (2개, Pillow 마스크 사용)
        "big_left"    — 좌 2/3 + 우 1/3 (2개)
        "big_right"   — 좌 1/3 + 우 2/3 (2개)
        "grid2x2"     — 2×2 균등 격자 (4개)
        "pip_br"      — 메인 전체 + 우하단 PIP 25% (2개)
        "pip_bl"      — 메인 전체 + 좌하단 PIP 25% (2개)
        "triptych"    — 3분할 세로 패널 (3개)

    클립 수와 레이아웃이 맞지 않으면 경고 후 auto로 폴백.

    출력 해상도: out_w×out_h
    프레임레이트: 30fps
    코덱: H.264 CRF 23
    오디오: 제거 (무음)
    """
    import math
    import warnings

    if not clip_paths:
        raise ValueError("clip_paths가 비어 있습니다.")

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    n = len(clip_paths)

    # 실제 출력 클립 길이 결정 (output_duration이 없으면 target_duration 사용)
    out_dur = output_duration if output_duration is not None else target_duration

    # 레이아웃-클립 수 적합성 경고 및 auto 폴백
    _layout_min_clips = {
        "left_right": 2, "top_bottom": 2, "diagonal_lr": 2, "diagonal_rl": 2,
        "big_left": 2, "big_right": 2, "grid2x2": 4, "pip_br": 2, "pip_bl": 2,
        "triptych": 3,
    }
    if layout != "auto" and layout in _layout_min_clips:
        min_n = _layout_min_clips[layout]
        if n < min_n:
            import warnings as _w
            _w.warn(
                f"layout='{layout}'은 최소 {min_n}개 클립이 필요하지만 {n}개만 있습니다. "
                "auto 레이아웃으로 폴백합니다.",
                stacklevel=2,
            )
            layout = "auto"

    # 클립이 하나인 경우 xstack 불필요 — 일반 리사이즈 + 속도 조절
    if n == 1:
        src_dur = get_duration(clip_paths[0])
        if src_dur <= 0:
            src_dur = target_duration
        pts_ratio = target_duration / src_dur if src_dur > 0 else 1.0

        _bg = normalize_bg_color(bg_color)
        _pad_suffix = "" if _bg == "black" else f":{_bg}"
        vf = (
            f"setpts={pts_ratio:.6f}*PTS,"
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30"
        )
        cmd = [
            ffmpeg_bin(), "-y",
            "-i", clip_paths[0],
            "-vf", vf,
            "-t", str(out_dur),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-an",
            dst,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg xstack(단일) 실패: {result.stderr}")
        return

    # 레이아웃별 filter_complex 구성
    filter_parts, output_label = _build_xstack_filter_parts(
        clip_paths, target_duration, layout, out_w, out_h, bg_color
    )

    # 대각선 레이아웃 처리: 마스크 PNG 생성 후 filter_complex에 합성
    is_diagonal = any(p.startswith("DIAGONAL_MASK_") for p in filter_parts)
    if is_diagonal:
        import tempfile as _tmpmod

        # 마커에서 방향 추출
        marker = next(p for p in filter_parts if p.startswith("DIAGONAL_MASK_"))
        direction = "lr" if "LR" in marker else "rl"
        filter_parts_clean = [p for p in filter_parts if not p.startswith("DIAGONAL_MASK_")]

        tmp_mask_dir = _tmpmod.mkdtemp(prefix="vag_diag_")
        mask_path = str(Path(tmp_mask_dir) / "diag_mask.png")
        try:
            _build_diagonal_mask(out_w, out_h, direction, mask_path)

            # alphamerge + overlay 합성
            # [base0]에 마스크 적용하여 [fg], [base1]을 [bg]로 사용하여 overlay
            diag_filter = (
                ";".join(filter_parts_clean)
                + ";[base0][2:v]alphamerge[fg]"
                + ";[base1][fg]overlay=0:0[diagout]"
            )

            cmd = [ffmpeg_bin(), "-y"]
            for p in clip_paths[:2]:
                cmd += ["-i", p]
            # 마스크 PNG를 3번째 입력으로 추가
            cmd += ["-i", mask_path]
            cmd += [
                "-filter_complex", diag_filter,
                "-map", "[diagout]",
                "-t", str(out_dur),
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-an",
                dst,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg 대각선 분할 합성 실패: {result.stderr}")
        finally:
            import shutil as _sh
            _sh.rmtree(tmp_mask_dir, ignore_errors=True)
        return

    # 일반 xstack / overlay 레이아웃
    filter_complex = ";".join(filter_parts)

    cmd = [ffmpeg_bin(), "-y"]
    for path in clip_paths:
        cmd += ["-i", path]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", output_label,
        "-t", str(out_dur),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-an",
        dst,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg xstack 분할 화면 합성 실패: {result.stderr}")


def build_grid_compare_filter(
    cells: list[tuple[int, int, int, int]],
    clip_paths: list[str],
    borders: list[dict | None],
    pts_ratios: list[float],
    bg_color: str = "black",
) -> tuple[list[str], str]:
    """N×M 그리드 비교 화면의 filter_complex 파트 목록과 출력 라벨을 반환한다(순수 함수).

    각 셀마다 build_grid_cell_filter 로 (보더 포함) 셀을 만들고, xstack 으로 합성한다.
    cells/clip_paths/borders/pts_ratios 는 같은 길이(=슬롯 수)여야 하며 행 우선 순서.
    """
    _bg = normalize_bg_color(bg_color)
    n = len(cells)
    parts: list[str] = []
    layout_parts: list[str] = []
    for i in range(n):
        x, y, cw, ch = cells[i]
        parts.extend(
            build_grid_cell_filter(
                input_idx=i,
                cell_w=cw,
                cell_h=ch,
                out_label=f"cell{i}",
                pts_ratio=pts_ratios[i],
                border=borders[i],
                bg_color=bg_color,
            )
        )
        layout_parts.append(f"{x}_{y}")
    inputs_str = "".join(f"[cell{i}]" for i in range(n))
    layout_str = "|".join(layout_parts)
    parts.append(
        f"{inputs_str}xstack=inputs={n}:layout={layout_str}:fill={_bg}[vout]"
    )
    return parts, "[vout]"


def build_grid_compare(
    clip_paths: list[str],
    dst: str,
    rows: int,
    cols: int,
    target_duration: float,
    borders: list[dict | None] | None = None,
    output_duration: float | None = None,
    out_w: int = 1080,
    out_h: int = 1920,
    bg_color: str = "black",
) -> None:
    """N×M 그리드 비교 화면(셀별 광원 그라데이션 보더 지원)을 dst 에 렌더한다.

    - rows×cols 그리드. clip_paths 는 행 우선(row-major) 순서로 슬롯에 배치.
      슬롯 수보다 clip_paths 가 적으면 앞에서부터 채우고 나머지 셀은 생략된다.
    - borders[i]: i번째 셀의 보더 dict({width_px,color_from,color_to,animate,speed}) 또는 None.
    - 광원: animate=True 셀은 hue 회전으로 그라데이션이 움직인다.
    - 주 용도: 세로 영상(1080×1920). 가로도 out_w/out_h 지정으로 동작.

    FFmpeg 실패 시 RuntimeError 를 던진다(호출부에서 씬 스킵/폴백 처리).
    """
    if not clip_paths:
        raise ValueError("clip_paths 가 비어 있습니다.")
    if rows < 1 or cols < 1:
        raise ValueError(f"rows/cols 는 1 이상이어야 합니다: rows={rows}, cols={cols}")

    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    all_cells = compute_grid_cells(rows, cols, out_w, out_h)
    n = min(len(clip_paths), len(all_cells))
    cells = all_cells[:n]
    used_paths = clip_paths[:n]

    if borders is None:
        borders = [None] * n
    # 길이 정합 — 부족하면 None 패딩, 초과분 절단.
    borders = (list(borders) + [None] * n)[:n]

    out_dur = output_duration if output_duration is not None else target_duration

    def _pts_ratio(path: str) -> float:
        src_dur = get_duration(path)
        if src_dur <= 0:
            src_dur = target_duration
        return target_duration / src_dur if src_dur > 0 else 1.0

    pts_ratios = [_pts_ratio(p) for p in used_paths]

    filter_parts, output_label = build_grid_compare_filter(
        cells, used_paths, borders, pts_ratios, bg_color
    )
    filter_complex = ";".join(filter_parts)

    cmd = [ffmpeg_bin(), "-y"]
    for path in used_paths:
        cmd += ["-i", path]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", output_label,
        "-t", str(out_dur),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-an",
        dst,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 그리드 비교 합성 실패: {result.stderr}")


def image_to_clip(src: str, duration: float, dst: str, out_w: int = 1920, out_h: int = 1080, bg_color: str = "black") -> None:
    """정지 이미지를 duration 길이의 영상으로 변환하여 dst에 저장.

    - 해상도: out_w×out_h (letterbox/pillarbox 패딩)
    - 프레임레이트: 30fps
    - 코덱: H.264 CRF 23
    - 오디오: 없음 (무음)
    지원 포맷: .png .jpg .jpeg .gif .webp .bmp .tiff .jfif
    """
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    _bg = normalize_bg_color(bg_color)
    _pad_suffix = "" if _bg == "black" else f":{_bg}"
    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2{_pad_suffix},fps=30"
    )
    cmd = [
        ffmpeg_bin(), "-y",
        "-loop", "1",
        "-i", src,
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-an",
        dst,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg image_to_clip 실패: {result.stderr}")


def preview_clip(src: str, start: float, end: float) -> str:
    """src 영상의 start~end 구간을 /tmp에 잘라내고 macOS 기본 플레이어로 실행. 임시 파일 경로 반환."""
    import time, subprocess as sp
    dst = f"/tmp/preview_{int(time.time())}.mp4"
    cut_clip(src, start, end, dst, mute=False)
    sp.Popen(["open", dst])
    return dst
