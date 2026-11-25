import cv2
import numpy as np
import os
import argparse
import subprocess
from tqdm import tqdm
from moviepy import VideoClip

def _imread(img_path, flags=cv2.IMREAD_COLOR):
    """한글 등 비ASCII 경로에서도 읽히는 imread.

    윈도우의 cv2.imread 는 비ASCII 경로를 못 열고 None 을 돌려준다
    (실측 2026-11-05: 'E:/claude-dev/호박이환풍기업그레이드/*.png' → 켄번스 씬 2개 생성 실패).
    파일을 바이트로 읽어 imdecode 하면 경로 인코딩과 무관하다.
    """
    try:
        data = np.fromfile(img_path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def get_image_dimensions(img_path):
    """OpenCV를 사용하여 이미지의 가로/세로 크기를 빠르게 읽어옵니다."""
    try:
        img = _imread(img_path)
        if img is None:
            return None, None
        return img.shape[1], img.shape[0] # width, height
    except Exception as e:
        print(f"Error reading image dimensions for {img_path}: {e}")
        return None, None

def generate_moviepy_clip(
    img_path, output_path, duration, start_scale, end_scale, video_size,
    fit_mode, anchor_x, anchor_y, fps=30
):
    """
    프레임마다 crop & resize를 계산하여 Ken Burns 줌 효과를 구현합니다.

    fit_mode:
      - "contain": 이미지 비율 유지 + letterbox/pillarbox (짧은 축을 영상 축에 맞춤)
      - "cover":   이미지 비율 유지 + 영역 가득 채움 (긴 축을 영상 축에 맞추고 반대 축은 crop)
      - "stretch": 비율 무시, 영상 크기로 강제 늘림

    어느 모드든 start_scale → end_scale 배율로 zoom-in이 추가 적용된다.
    """
    img = _imread(img_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        print(f"Error: Cannot read image {img_path}")
        return False

    # #grayscale-kenburns — 흑백(1채널) 이미지 방어.
    # cv2.imread(IMREAD_UNCHANGED) 는 흑백 PNG 를 2차원 배열(H, W)로 읽어온다.
    # 아래 로직은 모두 img.shape[2] (채널 수)를 전제하므로 그대로 두면
    # "tuple index out of range" 로 Ken Burns 가 통째로 실패하고, 해당 씬의
    # 클립이 아예 생성되지 않는다(실제 사고: 씬 53 이 영상에서 누락 →
    # 뒤 씬들의 TTS 타이밍까지 밀림).
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    # #16bit-png — 16비트 PNG(uint16) 를 8비트로 먼저 정규화한다.
    #   cv2.imread(IMREAD_UNCHANGED) 는 16비트 PNG 를 값 범위 0~65535 의 uint16 으로
    #   읽는다. 아래 알파 합성이 255 로 나누는 것을 전제하므로, 16비트를 그대로 넣으면
    #   alpha = 65535/255 = 257 이 되어 색이 257배로 폭주하고 uint8 캐스팅에서
    #   래핑되어 흰색이 청록/분홍 노이즈로 깨진다(사용자 보고: 켄번스 이미지 색 깨짐).
    if img.dtype == np.uint16:
        img = (img.astype(np.float32) / 257.0).clip(0, 255).astype(np.uint8)
    elif img.dtype != np.uint8:
        img = img.astype(np.uint8)

    # 알파채널이 있는 PNG: 투명 영역을 검정으로 합성 후 BGR로 변환
    if img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        bgr = img[:, :, :3].astype(np.float32)
        img = (bgr * alpha).clip(0, 255).astype(np.uint8)

    vw, vh = video_size
    orig_h, orig_w = img.shape[:2]

    # 유효 fit_mode (잘못된 값은 cover로 폴백)
    fit = fit_mode if fit_mode in ("contain", "cover", "stretch") else "cover"

    # base_scale: fit 모드에 따라 다른 기준 배율을 사용.
    #   contain → 짧은 축 맞춤 (min) → letterbox 가능
    #   cover   → 긴 축 맞춤 (max)   → crop 가능
    #   stretch → x/y 각각 별도 (비율 무시)
    if fit == "contain":
        base_scale_x = base_scale_y = min(vw / orig_w, vh / orig_h)
    elif fit == "cover":
        base_scale_x = base_scale_y = max(vw / orig_w, vh / orig_h)
    else:  # stretch
        base_scale_x = vw / orig_w
        base_scale_y = vh / orig_h

    # base fit 상태에서의 이미지 크기
    fitted_w = orig_w * base_scale_x
    fitted_h = orig_h * base_scale_y

    # 오프셋 — 캔버스 중앙 배치 (cover면 음수=crop, contain이면 양수=padding)
    offset_x = (vw - fitted_w) / 2.0
    offset_y = (vh - fitted_h) / 2.0

    # 줌인만 허용 — start_scale >= end_scale이면 end_scale을 살짝 더 크게 보정
    if end_scale <= start_scale:
        end_scale = start_scale + 0.01

    # 확대 기준점
    anchor_px = offset_x + fitted_w * anchor_x
    anchor_py = offset_y + fitted_h * anchor_y

    print(
        f"[kenburns] fit={fit}, base_scale=({base_scale_x:.4f},{base_scale_y:.4f}), "
        f"fitted={fitted_w:.1f}x{fitted_h:.1f}, "
        f"offset=({offset_x:.1f},{offset_y:.1f}), "
        f"start_scale={start_scale:.2f}, end_scale={end_scale:.2f}"
    )

    def make_frame(t):
        alpha = min(max(t / duration, 0), 1)
        scale = start_scale + (end_scale - start_scale) * alpha

        # 이미지 좌표계 기준 crop 영역 크기
        img_crop_w = vw / (base_scale_x * scale)
        img_crop_h = vh / (base_scale_y * scale)

        # 이미지 좌표계 기준 crop 시작점 (anchor 기준점 유지)
        img_anchor_x = (anchor_px - offset_x) / base_scale_x
        img_anchor_y = (anchor_py - offset_y) / base_scale_y
        img_crop_x = img_anchor_x - img_crop_w * anchor_x
        img_crop_y = img_anchor_y - img_crop_h * anchor_y

        # crop 영역이 이미지 경계를 벗어나지 않도록 clamp
        img_crop_x = max(0.0, min(img_crop_x, orig_w - img_crop_w))
        img_crop_y = max(0.0, min(img_crop_y, orig_h - img_crop_h))
        img_crop_w = min(img_crop_w, orig_w)
        img_crop_h = min(img_crop_h, orig_h)

        if fit == "contain":
            # 비율 유지 + letterbox
            crop_frame = float_crop_contain(img, img_crop_x, img_crop_y, img_crop_w, img_crop_h, vw, vh)
        elif fit == "cover":
            # 비율 유지 + 영역 가득 (crop). float_crop_cover는 crop 영역의 비율과
            # 캔버스 비율이 다를 때 캔버스 비율에 맞게 다시 crop해서 풀로 채움.
            crop_frame = float_crop_cover(img, img_crop_x, img_crop_y, img_crop_w, img_crop_h, vw, vh)
        else:  # stretch
            crop_frame = float_crop(img, img_crop_x, img_crop_y, img_crop_w, img_crop_h, vw, vh)
        return crop_frame[:, :, ::-1]

    video = VideoClip(make_frame, duration=duration)

    try:
        video.write_videofile(
            output_path,
            fps=fps,
            codec='libx264',
            bitrate='5000k',
            audio=False,
            logger=None,
            threads=4
        )
        return True
    except Exception as e:
        print(f"\n[MoviePy 실행 오류] 비디오 생성 실패: {output_path}")
        print(f"오류: {e}")
        return False


def float_crop(img, x, y, w, h, out_w, out_h):
    """비율 무시하고 crop 영역을 out_w x out_h로 스트레칭 (레거시 — 직접 사용 비권장)."""
    src = np.array([
        [x, y],
        [x + w, y],
        [x, y + h]
    ], dtype=np.float32)
    dst = np.array([
        [0, 0],
        [out_w, 0],
        [0, out_h]
    ], dtype=np.float32)
    M = cv2.getAffineTransform(src, dst)
    return cv2.warpAffine(img, M, (out_w, out_h), flags=cv2.INTER_LANCZOS4)


def float_crop_cover(img, x, y, w, h, out_w, out_h):
    """
    이미지에서 (x, y, w, h) 영역을 crop하되 원본 비율을 유지하면서
    out_w x out_h 캔버스를 가득 채운다 (영역 일부가 잘려도 letterbox 없음).

    구현: crop 영역의 비율을 캔버스 비율과 맞춘 뒤(중앙 기준 일부 추가 crop)
    out_w x out_h로 affine 변환.
    """
    if w <= 0 or h <= 0 or out_w <= 0 or out_h <= 0:
        return np.zeros((out_h, out_w, img.shape[2]), dtype=img.dtype)

    crop_ratio = w / h
    canvas_ratio = out_w / out_h

    if crop_ratio > canvas_ratio:
        # crop이 더 가로로 김 → 좌우를 잘라 비율 맞춤
        new_w = h * canvas_ratio
        x = x + (w - new_w) / 2.0
        w = new_w
    elif crop_ratio < canvas_ratio:
        # crop이 더 세로로 김 → 위아래를 잘라 비율 맞춤
        new_h = w / canvas_ratio
        y = y + (h - new_h) / 2.0
        h = new_h

    # affine 변환: 비율 맞춰진 crop 영역 → out_w x out_h
    src = np.array([
        [x,     y    ],
        [x + w, y    ],
        [x,     y + h]
    ], dtype=np.float32)
    dst = np.array([
        [0,     0    ],
        [out_w, 0    ],
        [0,     out_h]
    ], dtype=np.float32)
    M = cv2.getAffineTransform(src, dst)
    return cv2.warpAffine(img, M, (out_w, out_h), flags=cv2.INTER_LANCZOS4)


def float_crop_contain(img, x, y, w, h, out_w, out_h):
    """
    이미지에서 (x, y, w, h) 영역을 crop하되 원본 비율을 유지하면서
    out_w x out_h 캔버스에 contain fit으로 배치한다 (letterbox/pillarbox 허용).
    """
    orig_h_img, orig_w_img = img.shape[:2]

    # sub-pixel 정밀도로 crop 영역 추출 — affine 변환으로 w x h → out_w x out_h 매핑
    # 단, 비율 유지를 위해 w:h 비율과 out_w:out_h 비율을 비교하여
    # 실제 리사이즈 크기(fit_w, fit_h)를 결정한 후 패딩 적용
    scale_x = out_w / w if w > 0 else 1.0
    scale_y = out_h / h if h > 0 else 1.0
    fit_scale = min(scale_x, scale_y)

    fit_w = int(round(w * fit_scale))
    fit_h = int(round(h * fit_scale))

    # clamp
    fit_w = max(1, min(fit_w, out_w))
    fit_h = max(1, min(fit_h, out_h))

    # affine 변환: crop 영역(x, y, w, h) → fit_w x fit_h
    src = np.array([
        [x,     y    ],
        [x + w, y    ],
        [x,     y + h]
    ], dtype=np.float32)
    dst = np.array([
        [0,     0    ],
        [fit_w, 0    ],
        [0,     fit_h]
    ], dtype=np.float32)
    M = cv2.getAffineTransform(src, dst)
    resized = cv2.warpAffine(img, M, (fit_w, fit_h), flags=cv2.INTER_LANCZOS4)

    # out_w x out_h 검은 캔버스 중앙에 배치 (letterbox/pillarbox)
    canvas = np.zeros((out_h, out_w, img.shape[2]), dtype=img.dtype)
    pad_x = (out_w - fit_w) // 2
    pad_y = (out_h - fit_h) // 2
    canvas[pad_y:pad_y + fit_h, pad_x:pad_x + fit_w] = resized
    return canvas


def main(
    img_folder, output_folder, duration, start_scale, end_scale,
    width, height, fit_mode, anchor_x, anchor_y, fps
):
    print(f"--- MoviePy 기반 줌 클립 생성 시작 (트위닝 방식 V2.2) ---")
    
    # MoviePy는 FFmpeg가 시스템에 설치되어 있어야 작동합니다.
    try:
        subprocess.run(['ffmpeg', '-version'], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("\n[오류] FFmpeg이 시스템에 설치되어 있지 않거나 환경 변수에 등록되어 있지 않습니다.")
        print("MoviePy는 FFmpeg를 내부적으로 사용합니다. FFmpeg을 설치 후 다시 시도해 주세요.")
        return

    try:
        # moviepy 설치 확인
        import moviepy
        print(f"MoviePy version: {moviepy.__version__}")
    except ImportError:
        print("\n[오류] moviepy 라이브러리가 설치되어 있지 않습니다.")
        print("pip install moviepy 명령으로 설치 후 다시 시도해 주세요.")
        return


    allowed_extensions = ('.png', '.jpg', '.jpeg', '.bmp')
    video_size = (width, height)

    # 입력이 파일이면 단일 파일 처리
    if os.path.isfile(img_folder):
        img_path = img_folder
        out_path = output_folder
        print(f"\n[Processing single file]: {img_path} -> {out_path}")
        success = generate_moviepy_clip(
            img_path, out_path, duration, start_scale, end_scale, video_size,
            fit_mode=fit_mode, anchor_x=anchor_x, anchor_y=anchor_y, fps=fps
        )
        if success:
            print(f"Successfully created {out_path}")
        else:
            print(f"Failed to create {out_path}. Check the error message above.")
        return

    # 폴더면 기존 방식
    os.makedirs(output_folder, exist_ok=True)
    images = sorted([
        os.path.join(img_folder, f)
        for f in os.listdir(img_folder)
        if f.lower().endswith(allowed_extensions)
    ])
    if not images:
        print(f"Error: No images found in the input folder: {img_folder}")
        return
    for img_path in tqdm(images, desc="Processing Images"):
        base = os.path.splitext(os.path.basename(img_path))[0]
        out_path = os.path.join(output_folder, f"{base}.mp4")
        print(f"\n[Processing]: {img_path} -> {out_path}")
        success = generate_moviepy_clip(
            img_path, out_path, duration, start_scale, end_scale, video_size,
            fit_mode=fit_mode, anchor_x=anchor_x, anchor_y=anchor_y, fps=fps
        )
        if success:
            print(f"Successfully created {out_path}")
        else:
            print(f"Failed to create {out_path}. Check the error message above.")


if __name__ == "__main__": 
    # TQDM이 경고를 출력할 수 있어 환경 변수를 설정합니다.
    os.environ['TQDM_DISABLE'] = '0'
    
    parser = argparse.ArgumentParser(description='MoviePy Ken Burns 트위닝 방식으로 줌인/줌아웃 동영상을 생성합니다.')
    parser.add_argument('--input', type=str, required=True, help='이미지 폴더 경로')
    parser.add_argument('--output', type=str, required=True, help='출력 폴더 경로')
    parser.add_argument('--duration', type=float, default=3.0, help='각 이미지 영상 길이(초)')
    parser.add_argument('--start_scale', type=float, default=1.0, help='시작 배율 (1.0 = 기본 크기)')
    parser.add_argument('--end_scale', type=float, default=1.2, help='끝 배율 (1.2 = 20%% 확대)')
    parser.add_argument('--width', type=int, default=1280, help='영상 가로 해상도')
    parser.add_argument('--height', type=int, default=720, help='영상 세로 해상도')
    parser.add_argument('--fit_mode', type=str, choices=['width', 'height', 'cover', 'contain'], default='cover', 
                        help='width: 가로축 기준 맞춤, height: 세로축 기준 맞춤, cover: 화면 꽉 채움(잘림), contain: 이미지 전체 포함(검은 바)')
    parser.add_argument('--anchor_x', type=float, default=0.5, help='가로축 기준 확대 중심 (0~1)')
    parser.add_argument('--anchor_y', type=float, default=0.5, help='세로축 기준 확대 중심 (0~1)')
    parser.add_argument('--fps', type=int, default=30, help='출력 영상 프레임 수')
    args = parser.parse_args()

    # 입력 경로 유효성 검사 (파일 또는 폴더)
    if not (os.path.isdir(args.input) or os.path.isfile(args.input)):
        print(f"Error: Input file/folder not found at {args.input}")
    else:
        main(
            img_folder=args.input,
            output_folder=args.output,
            duration=args.duration,
            start_scale=args.start_scale,
            end_scale=args.end_scale,
            width=args.width,
            height=args.height,
            fit_mode=args.fit_mode,
            anchor_x=args.anchor_x,
            anchor_y=args.anchor_y,
            fps=args.fps
        )