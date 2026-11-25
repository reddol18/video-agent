"""Phase 7 확장 — 텍스트 기반 비용 효율적 숏폼(Shorts) 자동 추출기

영상을 Gemini API로 전송하지 않고, 텍스트 데이터(scene_plan.json,
narration_timeline.json)만으로 하이라이트 구간을 선정한 뒤
FFmpeg으로 9:16 세로 크롭 + 훅 문구 오버레이를 적용하여
output/shorts/ 폴더에 숏폼 영상을 출력한다.

처리 흐름:
  Step A. Gemini 텍스트 전용 호출 → 하이라이트 구간 3~4개 선정
  Step B. 로컬 피사체 탐지(YOLOv8-nano 또는 mediapipe) → EMA 스무딩 → x좌표 시계열
          탐지 불가 시 씬 description 기반 정적 위치 추정 (left/center/right) 폴백
  Step C. FFmpeg: final_output.mp4 절단 + 9:16 동적 크롭 + 훅 문구 drawtext
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from rich.console import Console

from .utils.file_cache import load_json, save_json
from .utils.gemini_client import generate_json
from .utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

# YOLOv8-nano (ultralytics) 선택적 임포트
try:
    from ultralytics import YOLO as _YOLO  # type: ignore
    _YOLO_AVAILABLE = True
except ImportError:
    _YOLO_AVAILABLE = False

# mediapipe 선택적 임포트 (YOLO 폴백)
try:
    import mediapipe as mp  # type: ignore
    _MEDIAPIPE_AVAILABLE = True
except ImportError:
    _MEDIAPIPE_AVAILABLE = False

console = Console()

PROJECT_ROOT = str(Path(__file__).parent.parent)

# 출력 경로
SHORTS_DIR = "output/shorts"
SHORTS_META_PATH = "output/shorts/shorts_meta.json"

# 소스 경로
SCENE_PLAN_PATH = "cache/scene_plan.json"
NARRATION_TIMELINE_PATH = "output/narration/narration_timeline.json"
FINAL_OUTPUT_PATH = "output/final_output.mp4"

# 숏폼 목표 길이 범위(초)
MIN_DURATION = 15
MAX_DURATION = 30


# ─────────────────────────────────────────────
# Step A: Gemini 텍스트 기반 하이라이트 선정
# ─────────────────────────────────────────────

def _build_highlight_prompt(scenes: list, timeline: list) -> str:
    """씬 정보와 나레이션 타임라인을 합쳐 하이라이트 선정 프롬프트를 구성한다."""

    timeline_by_id = {entry["scene_id"]: entry for entry in timeline}

    scene_lines = []
    for scene in scenes:
        sid = scene["scene_id"]
        t = timeline_by_id.get(sid, {})
        start = t.get("start_sec", 0.0)
        end = t.get("end_sec", 0.0)
        duration = round(end - start, 2)
        line = (
            f"씬 {sid} [{start:.2f}s~{end:.2f}s, {duration}초]: "
            f"제목={scene.get('title', '')}, "
            f"내용={scene.get('description', '')}, "
            f"분위기={scene.get('mood', '')}, "
            f"나레이션={scene.get('narration_text', '')}"
        )
        scene_lines.append(line)

    scenes_text = "\n".join(scene_lines)

    prompt = f"""당신은 숏폼(YouTube Shorts, Instagram Reels) 영상 편집 전문가입니다.

아래는 영상의 씬 목록과 각 씬의 시작/종료 시각(초) 및 나레이션 내용입니다.

{scenes_text}

위 정보만 보고 YouTube Shorts 기준으로 매력적인 하이라이트 구간을 3~4개 선정하세요.
각 구간은 {MIN_DURATION}초 이상 {MAX_DURATION}초 이하여야 합니다.
구간은 씬 단위로 묶되, 씬 경계의 start_sec/end_sec 값을 그대로 사용하세요.

결과는 아래 JSON 배열 형식으로만 응답하세요. 설명 없이 JSON만 출력하세요.

[
  {{
    "highlight_id": 1,
    "title": "숏폼 제목 (20자 이내)",
    "hook": "영상 상단에 표시할 훅 문구 (20자 이내, 시청자가 멈추게 만드는 문장)",
    "start_sec": 씬의 start_sec,
    "end_sec": 씬의 end_sec,
    "scene_ids": [포함된 씬 번호 배열],
    "reason": "선정 이유 한 줄"
  }}
]
"""
    return prompt


def _select_highlights(scenes: list, timeline: list) -> list[dict]:
    """Gemini 텍스트 전용 호출로 하이라이트 구간을 선정한다."""
    console.print("  [cyan]Step A: Gemini로 하이라이트 구간 선정 중...[/cyan]")
    prompt = _build_highlight_prompt(scenes, timeline)
    system = "당신은 숏폼 영상 편집 전문가입니다. 지시한 JSON 형식으로만 응답하세요."

    raw = generate_json(prompt, system=system)

    # JSON 파싱: 마크다운 코드 블록 제거 후 파싱
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
    highlights = json.loads(cleaned)

    # 유효성 검사: 길이 필터링
    valid = []
    for h in highlights:
        duration = h.get("end_sec", 0) - h.get("start_sec", 0)
        if duration < MIN_DURATION:
            console.print(f"    [yellow]  하이라이트 {h.get('highlight_id')} 제외: {duration:.1f}초 (최소 {MIN_DURATION}초 미만)[/yellow]")
            continue
        if duration > MAX_DURATION:
            # end_sec을 잘라서 MAX_DURATION에 맞춤
            h["end_sec"] = h["start_sec"] + MAX_DURATION
            console.print(f"    [dim]  하이라이트 {h.get('highlight_id')} end_sec 조정: {MAX_DURATION}초로 클리핑[/dim]")
        valid.append(h)

    console.print(f"  [green]하이라이트 {len(valid)}개 선정 완료[/green]")
    return valid


# ─────────────────────────────────────────────
# Step B: 로컬 피사체 탐지 → EMA 스무딩 → x좌표 시계열
# ─────────────────────────────────────────────

# YOLO 탐지 대상 클래스 ID (COCO 기준: 사람=0, 고양이=15, 강아지=16)
_YOLO_TARGET_CLASSES = {0, 15, 16}

# mediapipe 탐지 대상: 사람(전신 감지)


def _detect_subject_track(
    video_path: str,
    sample_fps: float = 5.0,
    ema_alpha: float = 0.3,
    src_width: int = 1920,
) -> Optional[list[float]]:
    """영상 클립에서 프레임을 샘플링하여 피사체 중심 x좌표 시계열을 반환한다.

    탐지 우선순위:
      1. ultralytics(YOLOv8-nano) — 사람/고양이/강아지 바운딩 박스 중심 x
      2. mediapipe Pose (전신 랜드마크 중심 x)
      3. 모두 미설치이거나 탐지 실패 → None 반환 (정적 크롭 폴백)

    Args:
        video_path: 분석할 영상 파일 경로
        sample_fps: 초당 분석 프레임 수 (기본 5)
        ema_alpha:  EMA 스무딩 계수 (0 < alpha <= 1, 작을수록 부드러움)
        src_width:  원본 영상 너비 (px), x좌표 정규화 기준

    Returns:
        EMA 스무딩된 x좌표 시계열 (픽셀 단위, float or None list) 또는 None.
        탐지 실패 시점의 항목은 None 대신 이전 EMA 값으로 채워지므로
        인덱스가 sample_times와 1:1 대응된다.
    """
    if not (_YOLO_AVAILABLE or _MEDIAPIPE_AVAILABLE):
        return None

    try:
        import cv2  # type: ignore  # ultralytics/mediapipe 환경에서는 항상 존재
    except ImportError:
        return None

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / video_fps if video_fps > 0 else 0.0

    # YOLO 모델 초기화 (최초 호출 시 다운로드)
    yolo_model = None
    if _YOLO_AVAILABLE:
        try:
            yolo_model = _YOLO("yolov8n.pt")
        except Exception:
            yolo_model = None

    # mediapipe Pose 초기화
    mp_pose = None
    if _MEDIAPIPE_AVAILABLE and yolo_model is None:
        try:
            mp_pose = mp.solutions.pose.Pose(  # type: ignore
                static_image_mode=True,
                model_complexity=0,
                min_detection_confidence=0.5,
            )
        except Exception:
            mp_pose = None

    # 타임스탬프별 seek 방식: 중간 프레임 읽기 실패로 인한 조기 종료 방지
    sample_times = [i / sample_fps for i in range(int(duration * sample_fps))]
    cx_series: list[Optional[float]] = []

    for t in sample_times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret:
            # 해당 시점 탐지 실패 → None으로 채움 (인덱스 정합성 유지)
            cx_series.append(None)
            continue
        cx = _extract_subject_cx(frame, yolo_model, mp_pose, src_width)
        cx_series.append(cx)

    cap.release()
    if mp_pose is not None:
        mp_pose.close()

    # 유효한 탐지값이 하나도 없으면 None 반환
    if not any(v is not None for v in cx_series):
        return None

    # EMA 스무딩: None은 이전 유효값으로 대체
    last_valid: Optional[float] = None
    for v in cx_series:
        if v is not None:
            last_valid = v
            break

    if last_valid is None:
        return None

    smoothed: list[Optional[float]] = []
    ema_val: float = last_valid
    for val in cx_series:
        if val is not None:
            ema_val = ema_alpha * val + (1.0 - ema_alpha) * ema_val
            smoothed.append(ema_val)
        else:
            # None 시점은 이전 EMA 값 유지 (인덱스 정합성 보존)
            smoothed.append(ema_val)

    return smoothed  # type: ignore[return-value]


def _extract_subject_cx(
    frame,  # numpy ndarray (BGR)
    yolo_model,
    mp_pose,
    src_width: int,
) -> Optional[float]:
    """단일 프레임에서 피사체 중심 x좌표(픽셀)를 추출한다.

    YOLO → mediapipe Pose 순으로 시도. 탐지 없으면 None.
    """
    # YOLO 우선
    if yolo_model is not None:
        try:
            results = yolo_model(frame, verbose=False)
            best_cx: Optional[float] = None
            best_conf: float = 0.0
            for result in results:
                for box in result.boxes:
                    cls_id = int(box.cls[0].item())
                    if cls_id not in _YOLO_TARGET_CLASSES:
                        continue
                    conf = float(box.conf[0].item())
                    if conf > best_conf:
                        best_conf = conf
                        x1, _y1, x2, _y2 = box.xyxy[0].tolist()
                        best_cx = (x1 + x2) / 2.0
            if best_cx is not None:
                return best_cx
        except Exception:
            pass

    # mediapipe Pose 폴백
    if mp_pose is not None:
        try:
            import cv2  # type: ignore
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = mp_pose.process(rgb)
            if res.pose_landmarks:
                h, w = frame.shape[:2]
                # 랜드마크 x좌표 평균으로 중심 추정
                xs = [lm.x * w for lm in res.pose_landmarks.landmark]
                return float(sum(xs) / len(xs))
        except Exception:
            pass

    return None


def _cx_series_to_crop_x(
    cx_series: list,
    src_width: int = 1920,
    crop_w: int = 607,
) -> list[int]:
    """x좌표 시계열을 FFmpeg crop 필터용 x 오프셋 시계열로 변환한다.

    피사체 중심(cx)이 크롭 영역 중앙에 오도록 오프셋 계산.
    경계(0 ~ src_width - crop_w) 클리핑 적용.
    None 항목은 직전 유효값으로 대체한다 (이전값 없으면 화면 중앙 사용).
    """
    max_x = max(0, src_width - crop_w)
    center_x = (src_width - crop_w) // 2
    result: list[int] = []
    last_x: int = center_x
    for cx in cx_series:
        if cx is None:
            result.append(last_x)
            continue
        x = int(cx - crop_w / 2)
        x = max(0, min(x, max_x))
        last_x = x
        result.append(x)
    return result


# ─────────────────────────────────────────────
# Step B: 피사체 위치 추정 → 크롭 x 좌표 결정 (정적 폴백)
# ─────────────────────────────────────────────

_LEFT_KEYWORDS = ["왼쪽", "좌측", "left", "왼편", "오른쪽 여백"]
_RIGHT_KEYWORDS = ["오른쪽", "우측", "right", "오른편", "왼쪽 여백"]
_CENTER_KEYWORDS = ["중앙", "가운데", "center", "정면", "중심"]


def _infer_crop_position(scenes: list, scene_ids: list[int]) -> str:
    """씬 description에서 피사체 위치 키워드를 분석하여 left/center/right 반환.

    여러 씬이 포함된 경우 다수결. 명시 없으면 center.
    """
    votes = {"left": 0, "center": 0, "right": 0}

    scene_map = {s["scene_id"]: s for s in scenes}
    for sid in scene_ids:
        desc = scene_map.get(sid, {}).get("description", "").lower()
        matched = False
        for kw in _LEFT_KEYWORDS:
            if kw in desc:
                votes["left"] += 1
                matched = True
                break
        if not matched:
            for kw in _RIGHT_KEYWORDS:
                if kw in desc:
                    votes["right"] += 1
                    matched = True
                    break
        if not matched:
            for kw in _CENTER_KEYWORDS:
                if kw in desc:
                    votes["center"] += 1
                    break

    # 명시 없으면 center
    if max(votes.values()) == 0:
        return "center"
    return max(votes, key=lambda k: votes[k])


def _crop_x_for_position(position: str, src_width: int = 1920, target_width: int = 1080) -> int:
    """crop 필터에 사용할 x 오프셋 계산.

    9:16 숏폼 기준 target_width는 원본 영상 높이(1080)에서 유도된 값.
    1920×1080 원본에서 9:16 (608×1080) 크롭.
    """
    crop_w = target_width * 9 // 16  # 9:16 비율의 너비 = 1080 * 9 / 16 = 607.5 → 607
    if position == "left":
        return 0
    elif position == "right":
        return max(0, src_width - crop_w)
    else:  # center
        return (src_width - crop_w) // 2


# ─────────────────────────────────────────────
# Step C: FFmpeg 영상 처리
# ─────────────────────────────────────────────

def _is_drawtext_available() -> bool:
    """FFmpeg drawtext 필터 가용 여부를 확인한다.

    `ffmpeg -filters` 출력에서 'drawtext' 항목을 탐색한다.
    FFmpeg 실행 실패 시 False 반환.
    """
    try:
        result = subprocess.run(
            [ffmpeg_bin(), "-filters"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # stdout + stderr 모두 확인 (버전에 따라 다름)
        output = result.stdout + result.stderr
        return "drawtext" in output
    except Exception:
        return False


def _sanitize_drawtext(text: str) -> str:
    """drawtext 필터에 사용할 텍스트를 이스케이프한다."""
    # 콜론, 작은따옴표, 역슬래시 이스케이프
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    return text


# ─────────────────────────────────────────────
# AI 업스케일링 (Real-ESRGAN-ncnn-vulkan)
# ─────────────────────────────────────────────

def _find_realesrgan_bin() -> Optional[str]:
    """realesrgan-ncnn-vulkan 바이너리 경로를 탐지한다.

    탐지 순서:
      1. 환경변수 REALESRGAN_BIN
      2. which realesrgan-ncnn-vulkan (PATH 탐색)

    Returns:
        바이너리 절대 경로 문자열, 없으면 None.
    """
    import os
    import shutil

    env_bin = os.getenv("REALESRGAN_BIN")
    if env_bin and Path(env_bin).is_file():
        return env_bin

    which_result = shutil.which("realesrgan-ncnn-vulkan")
    if which_result:
        return which_result

    return None


def _run_realesrgan_on_frames(
    input_dir: str,
    output_dir: str,
    bin_path: str,
    model: str = "realesrgan-x4plus",
    scale: int = 4,
) -> None:
    """realesrgan-ncnn-vulkan CLI를 이용해 프레임 이미지를 업스케일링한다.

    Args:
        input_dir:  원본 프레임 이미지가 담긴 폴더 경로.
        output_dir: 업스케일링 결과를 저장할 폴더 경로.
        bin_path:   realesrgan-ncnn-vulkan 바이너리 경로.
        model:      사용할 모델명 (기본: realesrgan-x4plus).
        scale:      업스케일 배율 (기본: 4).

    Raises:
        RuntimeError: 바이너리 실행 실패 시.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    cmd = [
        bin_path,
        "-i", input_dir,
        "-o", output_dir,
        "-n", model,
        "-s", str(scale),
        "-f", "png",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"realesrgan-ncnn-vulkan 실패 (종료코드={result.returncode}): "
            f"{result.stderr[-500:]}"
        )


def _apply_ai_enhance(
    clip_path: str,
    dst: str,
    bin_path: str,
) -> bool:
    """크롭된 숏폼 클립에 Real-ESRGAN AI 업스케일링을 적용한다.

    처리 흐름:
      1. FFmpeg으로 클립을 PNG 프레임 시퀀스로 추출
      2. realesrgan-ncnn-vulkan으로 각 프레임 업스케일링 (x4)
      3. FFmpeg으로 업스케일된 프레임 + 원본 오디오를 합쳐 최종 클립 재조립
         (업스케일 결과를 원본 해상도로 스케일 다운하여 파일 크기 억제)
      4. 임시 프레임 폴더 정리

    Args:
        clip_path: 업스케일링할 원본 숏폼 클립 경로 (이미 크롭+필터 적용 완료본).
        dst:       최종 출력 경로.
        bin_path:  realesrgan-ncnn-vulkan 바이너리 경로.

    Returns:
        True: 업스케일링 성공, False: 실패(폴백 필요).
    """
    import tempfile
    import os

    tmp_base = tempfile.mkdtemp(prefix="resr_")
    frames_in = os.path.join(tmp_base, "frames_in")
    frames_out = os.path.join(tmp_base, "frames_out")
    Path(frames_in).mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: 클립 → PNG 프레임 추출
        extract_cmd = [
            ffmpeg_bin(), "-y",
            "-i", clip_path,
            "-vf", "fps=30",
            os.path.join(frames_in, "frame_%06d.png"),
        ]
        r = subprocess.run(extract_cmd, capture_output=True, text=True)
        if r.returncode != 0:
            console.print(f"    [yellow]프레임 추출 실패: {r.stderr[-200:]}[/yellow]")
            return False

        # Step 2: Real-ESRGAN 업스케일링 (x4)
        _run_realesrgan_on_frames(frames_in, frames_out, bin_path, scale=4)

        # Step 3: 업스케일된 프레임 + 원본 오디오 → 최종 클립 재조립
        # 업스케일 결과(x4 해상도)를 원본 해상도(608×1080)로 스케일 다운
        reassemble_cmd = [
            ffmpeg_bin(), "-y",
            "-framerate", "30",
            "-i", os.path.join(frames_out, "frame_%06d.png"),
            "-i", clip_path,
            "-map", "0:v",
            "-map", "1:a?",
            "-vf", "scale=608:1080:flags=lanczos",
            "-c:v", "libx264", "-crf", "20", "-preset", "fast",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            dst,
        ]
        r2 = subprocess.run(reassemble_cmd, capture_output=True, text=True)
        if r2.returncode != 0:
            console.print(f"    [yellow]프레임 재조립 실패: {r2.stderr[-200:]}[/yellow]")
            return False

        return True

    except RuntimeError as e:
        console.print(f"    [yellow]Real-ESRGAN 업스케일 실패: {e}[/yellow]")
        return False
    finally:
        # 임시 프레임 폴더 정리
        import shutil as _shutil
        try:
            _shutil.rmtree(tmp_base, ignore_errors=True)
        except Exception:
            pass


def _run_ffmpeg_shorts(
    src: str,
    start_sec: float,
    end_sec: float,
    crop_x: int,
    hook_text: str,
    dst: str,
    use_drawtext: bool = True,
    dynamic_crop_xs: Optional[list[int]] = None,
    src_fps: float = 30.0,
    sample_fps: float = 5.0,
    enhance: bool = True,
    bg_color: str = "black",
) -> None:
    """final_output.mp4에서 숏폼 클립을 생성한다.

    처리 내용:
    - 타임코드로 절단 (start_sec ~ end_sec)
    - 9:16 세로 크롭 (crop_w:1080 → 크롭 후 scale 608:1080)
      dynamic_crop_xs가 제공되면 프레임별 동적 크롭 (sendcmd 활용).
      None이면 고정 crop_x로 단순 크롭.
    - 경량 화질 보정 필터 (enhance=True이면 항상 적용):
        unsharp (선명도) + eq (대비/채도) — 저화질 소스 뿌연 느낌 제거,
        모바일 최적화 색감 보정
    - 훅 문구 drawtext 상단 오버레이 (use_drawtext=False이면 생략)

    Args:
        dynamic_crop_xs: 샘플 프레임별 crop x 오프셋 시계열.
                         None이면 crop_x 고정값 사용.
        src_fps:         원본 영상 fps (동적 크롭 타이밍 계산용).
        sample_fps:      탐지 샘플링 fps (동적 크롭 타이밍 계산용).
        enhance:         FFmpeg 경량 보정 필터(unsharp+eq) 적용 여부 (기본 True).
    """
    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    # 쇼츠 크기: 608×1080 (9:16)
    # 짧은 축 기준으로 스케일 후 center crop:
    #   - 원본 높이(짧은 축) → 1080에 맞게 scale=-2:1080
    #   - 너비가 608보다 넓으면 center crop으로 608으로 자름
    scale_w = 608
    scale_h = 1080

    # 비율 유지하며 608×1080 캔버스에 contain fit (letterbox/pillarbox)
    # 프로젝트 전역 배경색 반영. 기본 black이면 기존과 동일.
    from .utils.ffmpeg_utils import normalize_bg_color as _normalize_bg_color
    _bg_norm = _normalize_bg_color(bg_color)
    crop_filter = (
        f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease,"
        f"pad={scale_w}:{scale_h}:(ow-iw)/2:(oh-ih)/2:{_bg_norm}"
    )

    # 경량 화질 보정 필터: unsharp(선명도) + eq(대비/채도)
    enhance_filter = (
        "unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=0.8,"
        "eq=contrast=1.05:saturation=1.1"
    )

    if use_drawtext:
        safe_hook = _sanitize_drawtext(hook_text)
        drawtext = (
            f"drawtext=text='{safe_hook}'"
            f":fontsize=42"
            f":fontcolor=white"
            f":borderw=3:bordercolor=black"
            f":x=(w-text_w)/2"
            f":y=h*0.07"
        )
        if enhance:
            vf = f"{crop_filter},{enhance_filter},{drawtext}"
        else:
            vf = f"{crop_filter},{drawtext}"
    else:
        if enhance:
            vf = f"{crop_filter},{enhance_filter}"
        else:
            vf = crop_filter

    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", str(start_sec),
        "-to", str(end_sec),
        "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        dst,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 숏폼 생성 실패: {result.stderr[-500:]}")


# ─────────────────────────────────────────────
# 공개 진입점
# ─────────────────────────────────────────────

def run(force: bool = False, ai_enhance: bool = False) -> list[dict]:
    """숏폼 자동 추출 전체 실행.

    Args:
        force:      캐시를 무시하고 재생성 여부.
        ai_enhance: Real-ESRGAN-ncnn-vulkan AI 업스케일링 적용 여부 (기본 False).
                    바이너리가 없으면 경고 후 FFmpeg 필터(enhance=True)만 적용.

    Returns:
        생성된 숏폼 메타데이터 목록 (shorts_meta.json과 동일 내용)
    """
    # 기존 결과 캐시 확인
    if not force:
        cached = load_json(SHORTS_META_PATH)
        if cached:
            console.print("  [dim]shorts_meta.json 캐시 사용 (--force로 재실행 가능)[/dim]")
            return cached

    # 입력 파일 존재 확인
    scene_plan = load_json(SCENE_PLAN_PATH)
    if not scene_plan:
        raise RuntimeError(f"scene_plan.json이 없어요: {SCENE_PLAN_PATH}\nPhase 1을 먼저 실행해주세요.")

    narration_timeline = load_json(NARRATION_TIMELINE_PATH)
    if not narration_timeline:
        raise RuntimeError(
            f"narration_timeline.json이 없어요: {NARRATION_TIMELINE_PATH}\n"
            "Phase 4 + Phase 5를 먼저 실행해주세요."
        )

    # 프로젝트 전역 배경색(letterbox 여백). clip_selection.json 에서 읽음.
    # 없거나 미지정이면 "black" — 기존과 동일.
    _clip_sel = load_json("cache/clip_selection.json")
    shorts_bg_color = str((_clip_sel or {}).get("bg_color", "black") or "black")

    final_output = Path(PROJECT_ROOT) / FINAL_OUTPUT_PATH
    if not final_output.exists():
        raise RuntimeError(
            f"final_output.mp4가 없어요: {FINAL_OUTPUT_PATH}\n"
            "Phase 6을 먼저 실행해주세요."
        )

    # scene_plan은 {"scenes": [...]} 또는 [...] 형태 모두 지원
    scenes: list = scene_plan if isinstance(scene_plan, list) else scene_plan.get("scenes", [])
    # narration_timeline은 {"entries": [...]} 또는 [...] 형태 모두 지원
    timeline: list = (
        narration_timeline
        if isinstance(narration_timeline, list)
        else narration_timeline.get("entries", [])
    )

    if not scenes:
        raise RuntimeError("scene_plan.json에 씬 데이터가 없어요.")
    if not timeline:
        raise RuntimeError("narration_timeline.json에 타임라인 데이터가 없어요.")

    # drawtext 필터 가용 여부 사전 확인
    drawtext_available = _is_drawtext_available()
    if not drawtext_available:
        console.print(
            "  [yellow]경고: drawtext 미지원 — 훅 문구 오버레이 생략[/yellow]"
        )

    # AI 업스케일링 바이너리 탐지
    realesrgan_bin: Optional[str] = None
    if ai_enhance:
        realesrgan_bin = _find_realesrgan_bin()
        if realesrgan_bin:
            console.print(f"  [dim]AI 업스케일링: realesrgan-ncnn-vulkan 사용 ({realesrgan_bin})[/dim]")
        else:
            console.print(
                "  [yellow]경고: realesrgan-ncnn-vulkan 바이너리를 찾을 수 없어요 "
                "(REALESRGAN_BIN 환경변수 또는 PATH 확인) — FFmpeg 필터만 적용합니다[/yellow]"
            )

    # 로컬 피사체 탐지 가용 여부 안내
    if _YOLO_AVAILABLE:
        console.print("  [dim]피사체 탐지: YOLOv8-nano 사용[/dim]")
    elif _MEDIAPIPE_AVAILABLE:
        console.print("  [dim]피사체 탐지: mediapipe Pose 사용[/dim]")
    else:
        console.print(
            "  [yellow]경고: ultralytics/mediapipe 미설치 — 정적 크롭(description 기반) 사용[/yellow]"
        )

    # Step A: Gemini 텍스트 기반 하이라이트 선정
    highlights = _select_highlights(scenes, timeline)
    if not highlights:
        console.print("[yellow]선정된 하이라이트가 없어요. 씬 길이 또는 콘텐츠를 확인해주세요.[/yellow]")
        return []

    # Step B + C: 크롭 위치 추정 및 FFmpeg 처리
    shorts_meta = []
    shorts_dir = Path(PROJECT_ROOT) / SHORTS_DIR
    shorts_dir.mkdir(parents=True, exist_ok=True)

    for h in highlights:
        hid = h["highlight_id"]
        console.print(f"\n  [cyan]Step B+C: 숏폼 #{hid} 생성 중 — {h['title']}[/cyan]")

        # Step B: 로컬 피사체 탐지 → 동적 크롭 좌표
        dynamic_crop_xs: Optional[list[int]] = None
        crop_mode = "static"
        _sample_fps = 5.0  # 탐지 미사용 시에도 _run_ffmpeg_shorts 호출에 필요

        if _YOLO_AVAILABLE or _MEDIAPIPE_AVAILABLE:
            console.print("    [dim]Step B: 피사체 탐지 중...[/dim]")
            cx_series = _detect_subject_track(
                video_path=str(final_output),
                sample_fps=_sample_fps,
                ema_alpha=0.3,
            )
            if cx_series:
                # 전체 cx_series에서 하이라이트 구간(start_sec ~ end_sec)에 해당하는
                # 슬라이스만 추출한다. sendcmd 타임스탬프는 t=0 기준이므로
                # 하이라이트 시작 시점의 인덱스부터 끝 인덱스까지 잘라야 정렬된다.
                start_idx = int(h["start_sec"] * _sample_fps)
                end_idx = int(h["end_sec"] * _sample_fps)
                # cx_series 길이를 초과하지 않도록 경계 처리
                start_idx = min(start_idx, len(cx_series))
                end_idx = min(end_idx, len(cx_series))
                cx_slice = cx_series[start_idx:end_idx]

                if cx_slice:
                    dynamic_crop_xs = _cx_series_to_crop_x(cx_slice)
                    crop_mode = "dynamic"
                    console.print(
                        f"    피사체 탐지 완료: {len(cx_slice)}개 샘플 "
                        f"(원본 인덱스 {start_idx}~{end_idx}), "
                        f"x=[{min(dynamic_crop_xs)}~{max(dynamic_crop_xs)}] → 동적 크롭"
                    )
                else:
                    console.print("    [yellow]해당 구간 cx 슬라이스 없음 → 정적 크롭 폴백[/yellow]")
            else:
                console.print("    [yellow]피사체 미탐지 → 정적 크롭 폴백[/yellow]")

        # 동적 탐지 실패 또는 미지원 → 정적 크롭
        position = _infer_crop_position(scenes, h.get("scene_ids", []))
        crop_x = _crop_x_for_position(position)
        if crop_mode == "static":
            console.print(f"    피사체 위치(정적): {position} → crop_x={crop_x}")

        # 출력 파일명
        dst = str(shorts_dir / f"shorts_{hid:02d}.mp4")

        try:
            _run_ffmpeg_shorts(
                src=str(final_output),
                start_sec=h["start_sec"],
                end_sec=h["end_sec"],
                crop_x=crop_x,
                hook_text=h["hook"],
                dst=dst,
                use_drawtext=drawtext_available,
                dynamic_crop_xs=dynamic_crop_xs,
                sample_fps=_sample_fps,
                enhance=True,
                bg_color=shorts_bg_color,
            )

            # AI 업스케일링 (--enhance 플래그 시): 크롭된 클립에 Real-ESRGAN 적용
            # 하이라이트 구간(15~30초)만 처리
            if ai_enhance and realesrgan_bin:
                console.print("    [dim]Step D: Real-ESRGAN AI 업스케일링 중...[/dim]")
                ai_dst = str(shorts_dir / f"shorts_{hid:02d}_ai.mp4")
                success = _apply_ai_enhance(dst, ai_dst, realesrgan_bin)
                if success:
                    # AI 업스케일 성공 시 원본을 교체
                    import os as _os
                    _os.replace(ai_dst, dst)
                    console.print("    [green]AI 업스케일링 완료[/green]")
                else:
                    # 실패 시 FFmpeg 필터 결과를 그대로 사용 (폴백)
                    console.print(
                        "    [yellow]AI 업스케일링 실패 — FFmpeg 필터 결과를 사용합니다[/yellow]"
                    )
                    try:
                        Path(ai_dst).unlink(missing_ok=True)
                    except Exception:
                        pass

            file_size = Path(dst).stat().st_size
            console.print(f"    [green]생성 완료: {Path(dst).name} ({file_size // 1024}KB)[/green]")

            shorts_meta.append({
                "highlight_id": hid,
                "title": h["title"],
                "hook": h["hook"],
                "start_sec": h["start_sec"],
                "end_sec": h["end_sec"],
                "duration_sec": round(h["end_sec"] - h["start_sec"], 2),
                "scene_ids": h.get("scene_ids", []),
                "crop_mode": crop_mode,
                "crop_position": position,
                "crop_x": crop_x,
                "output_file": dst,
                "reason": h.get("reason", ""),
                "ai_enhanced": ai_enhance and realesrgan_bin is not None,
            })
        except RuntimeError as e:
            console.print(f"    [red]숏폼 #{hid} 생성 실패: {e}[/red]")

    # 메타데이터 저장
    save_json(str(Path(PROJECT_ROOT) / SHORTS_META_PATH), shorts_meta)
    console.print(f"\n  [bold green]숏폼 {len(shorts_meta)}개 생성 완료![/bold green]")
    console.print(f"  메타데이터: {SHORTS_META_PATH}")

    return shorts_meta


def print_summary(meta: list[dict], project_root: str) -> None:
    """숏폼 생성 결과를 콘솔에 출력한다."""
    console.print("\n[bold]── 숏폼 생성 결과 ──────────────────────────────[/bold]")
    if not meta:
        console.print("  생성된 숏폼이 없어요.")
        return
    for m in meta:
        duration = m.get("duration_sec", 0)
        console.print(
            f"  #{m['highlight_id']:02d} {m['title']} "
            f"[{m['start_sec']:.1f}s ~ {m['end_sec']:.1f}s, {duration:.1f}초] "
            f"crop={m['crop_position']}"
        )
        console.print(f"       훅: {m['hook']}")
        console.print(f"       파일: {Path(m['output_file']).name}")
    console.print(f"\n  출력 폴더: output/shorts/")
