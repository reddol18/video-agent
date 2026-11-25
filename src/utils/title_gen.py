import os
from pathlib import Path
import numpy as np
from PIL import Image
from moviepy import CompositeVideoClip, TextClip, ColorClip, ImageClip
from moviepy.video.VideoClip import VideoClip as BaseVideoClip
from moviepy import concatenate_videoclips
import matplotlib.colors as mcolors


def _find_korean_font() -> str | None:
    """한글을 지원하는 폰트 경로를 자동으로 탐색하여 반환한다. 없으면 None."""
    candidates = [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/Library/Fonts/NanumGothic.ttf",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None

def to_rgb_tuple(color):
    if isinstance(color, tuple):
        return color
    return tuple(int(255 * c) for c in mcolors.to_rgb(color))

def make_shimmer_mask(size, t, duration, freq=2, min_alpha=0.0, max_alpha=0.3):
    w, h = size
    # Sinusoidal shimmer, never above max_alpha
    phase = 2 * np.pi * freq * (t / duration)
    alpha = min_alpha + (max_alpha - min_alpha) * (0.5 + 0.5 * np.cos(phase))
    if alpha < min_alpha:
        alpha = min_alpha
    return np.full((h, w), alpha, dtype=np.float32)

def _render_text_frame(line, font_size, font_path, text_color, rect_w, line_height, bg_color_tuple=None):
    """TextClip으로 한 줄을 렌더링해서 numpy 배열(H,W,3)로 반환."""
    tc = TextClip(
        text=line,
        font_size=font_size,
        font=font_path if font_path else "Arial",
        color=text_color,
        bg_color=bg_color_tuple if bg_color_tuple is not None else (0, 0, 0),
        size=(rect_w, line_height),
        method="caption",
        text_align="center",
        horizontal_align="center",
        vertical_align="center"
    ).with_duration(0.1)
    return tc.get_frame(0)  # (H, W, 3)


def _scale_frame_centered(frame, scale, canvas_w, canvas_h, bg_color_tuple=None):
    """frame을 scale 배율로 리사이즈하고, canvas 중앙에 붙여 반환(numpy H,W,3)."""
    new_w = int(canvas_w * scale)
    new_h = int(canvas_h * scale)
    img = Image.fromarray(frame.astype(np.uint8))
    img_r = img.resize((new_w, new_h), Image.LANCZOS)
    fill = bg_color_tuple if bg_color_tuple is not None else (0, 0, 0)
    canvas = np.full((canvas_h, canvas_w, 3), fill, dtype=np.uint8)
    px = (canvas_w - new_w) // 2
    py = (canvas_h - new_h) // 2
    src_x = max(0, -px); src_y = max(0, -py)
    dst_x = max(0, px);  dst_y = max(0, py)
    cw = min(new_w - src_x, canvas_w - dst_x)
    ch = min(new_h - src_y, canvas_h - dst_y)
    if cw > 0 and ch > 0:
        canvas[dst_y:dst_y+ch, dst_x:dst_x+cw] = np.array(img_r)[src_y:src_y+ch, src_x:src_x+cw]
    return canvas


def _find_spc_word_x(full_line, spc_word, font_path, font_size, canvas_w):
    """PIL로 중앙정렬된 full_line 안에서 spc_word의 x 시작좌표와 픽셀 폭을 반환."""
    from PIL import ImageFont, ImageDraw, Image as PILImage
    try:
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
        dummy = PILImage.new('RGB', (canvas_w * 3, font_size * 3))
        draw  = ImageDraw.Draw(dummy)
        idx   = full_line.find(spc_word)
        if idx < 0:
            return 0, canvas_w
        text_before = full_line[:idx]
        full_bbox   = draw.textbbox((0, 0), full_line, font=font)
        full_w      = full_bbox[2] - full_bbox[0]
        before_w    = 0
        if text_before:
            bb       = draw.textbbox((0, 0), text_before, font=font)
            before_w = bb[2] - bb[0]
        spc_bbox = draw.textbbox((0, 0), spc_word, font=font)
        spc_w    = spc_bbox[2] - spc_bbox[0]
        text_start_x = (canvas_w - full_w) // 2
        spc_x = max(0, text_start_x + before_w)
        spc_w = min(spc_w, canvas_w - spc_x)
        return int(spc_x), int(spc_w)
    except Exception:
        return 0, canvas_w


def _place_region(region, x_pos, y_pos, canvas_w, canvas_h, bg_color_tuple=None):
    """region(rh, rw, 3)을 (x_pos, y_pos)에 배치한 (canvas_h, canvas_w, 3) canvas."""
    fill = bg_color_tuple if bg_color_tuple is not None else (0, 0, 0)
    canvas = np.full((canvas_h, canvas_w, 3), fill, dtype=np.uint8)
    rh, rw = region.shape[:2]
    y0 = max(0, y_pos);  y1 = min(canvas_h, y_pos + rh)
    x0 = max(0, x_pos);  x1 = min(canvas_w, x_pos + rw)
    sy0 = max(0, -y_pos); sy1 = sy0 + (y1 - y0)
    sx0 = max(0, -x_pos); sx1 = sx0 + (x1 - x0)
    if y1 > y0 and x1 > x0:
        canvas[y0:y1, x0:x1] = region[sy0:sy1, sx0:sx1]
    return canvas


def make_spc_word_post_clips(
    lines, spc_word, rect_x, rect_y, start_y, rect_w, rect_h, line_height,
    font_size, font_path, text_color, effect_duration, duration, line_spacing,
    bg_color_tuple=None
):
    """
    effect_duration 이후의 spc_word 강조 단계 클립 목록을 반환.
      - Phase 1 (0.5s): 非spc 줄 dissolve out + spc 줄에서 spc_word 외 컬럼 fade out
                        (spc_word는 줄 내 원래 x 위치에 그대로 남아 있음)
      - Phase 2 (0.5s): spc_word가 원래 (x, y) 위치 → rect 중앙으로 이동 (smoothstep)
      - Phase 3 (0.5s): 1x → 1.2x 확대
      - Phase 4 (나머지): 1.2x + shimmer
    """
    DISSOLVE_TIME = 0.5
    MOVE_TIME     = 0.5
    SCALE_TIME    = 0.5
    post_duration    = duration - effect_duration
    move_start       = DISSOLVE_TIME
    scale_start      = DISSOLVE_TIME + MOVE_TIME
    shimmer_start    = DISSOLVE_TIME + MOVE_TIME + SCALE_TIME
    shimmer_duration = max(0.05, post_duration - shimmer_start)

    spc_line_idx = next((i for i, l in enumerate(lines) if spc_word in l), None)
    if spc_line_idx is None:
        return []

    spc_line = lines[spc_line_idx]
    spc_y    = start_y + spc_line_idx * (line_height + int(font_size * (line_spacing - 1)))

    # PIL로 spc_word의 실제 x 범위 측정
    spc_x_px, spc_w_px = _find_spc_word_x(spc_line, spc_word, font_path, font_size, rect_w)
    spc_x_px = max(0, spc_x_px)
    spc_w_px = max(1, min(spc_w_px, rect_w - spc_x_px))

    # 전체 spc 줄 렌더링 → spc_word 픽셀 영역 추출
    spc_line_frame = _render_text_frame(spc_line, font_size, font_path, text_color, rect_w, line_height, bg_color_tuple=bg_color_tuple)
    spc_region     = spc_line_frame[:, spc_x_px:spc_x_px + spc_w_px].copy()  # (line_height, spc_w_px, 3)

    _cw, _ch    = rect_w, rect_h
    orig_rel_y   = spc_y - rect_y                       # rect 캔버스 기준 y
    center_rel_y = (_ch - line_height) // 2              # 중앙 y
    center_x     = (_cw - spc_w_px) // 2                # 중앙 x

    clips = []

    # ── Phase 1-A: 非spc 줄 전체 dissolve out ────────────────────────────────
    for i, line in enumerate(lines):
        if i == spc_line_idx:
            continue
        y = start_y + i * (line_height + int(font_size * (line_spacing - 1)))
        f = _render_text_frame(line, font_size, font_path, text_color, rect_w, line_height, bg_color_tuple=bg_color_tuple).copy()
        h_f, w_f = f.shape[:2]

        def _dout_mask(t, _w=w_f, _h=h_f):
            return np.full((_h, _w), max(0.0, 1.0 - t / DISSOLVE_TIME), dtype=np.float32)

        dout_vid  = BaseVideoClip(lambda t, _f=f: _f, duration=DISSOLVE_TIME)
        dout_mask = BaseVideoClip(_dout_mask, is_mask=True, duration=DISSOLVE_TIME)
        clips.append(dout_vid.with_mask(dout_mask).with_position((rect_x, y)).with_start(effect_duration))

    # ── Phase 1-B: spc 줄 — spc_word 외 컬럼만 fade out, spc_word 제자리 유지 ─
    spc_lf = spc_line_frame.copy()

    def _spc_line_fn(t, _f=spc_lf):
        return _f

    def _spc_col_mask(t, _cw=_cw, _lh=line_height, _sx=spc_x_px, _sw=spc_w_px):
        alpha_fade = max(0.0, 1.0 - t / DISSOLVE_TIME)
        mask = np.ones((_lh, _cw), dtype=np.float32)
        if _sx > 0:
            mask[:, :_sx] = alpha_fade
        if _sx + _sw < _cw:
            mask[:, _sx + _sw:] = alpha_fade
        return mask

    spc_vid  = BaseVideoClip(_spc_line_fn, duration=DISSOLVE_TIME)
    spc_mask = BaseVideoClip(_spc_col_mask, is_mask=True, duration=DISSOLVE_TIME)
    clips.append(
        spc_vid.with_mask(spc_mask)
               .with_position((rect_x, spc_y))
               .with_start(effect_duration)
    )

    # ── Phase 2: (orig_x, orig_y) → (center_x, center_y) smoothstep 이동 ────
    sr2 = spc_region.copy()

    def _phase2_frame(t, _sr=sr2, _cw=_cw, _ch=_ch, _lh=line_height,
                      _ox=spc_x_px, _oy=orig_rel_y, _cx=center_x, _cy=center_rel_y,
                      _bg=bg_color_tuple):
        p = min(1.0, t / MOVE_TIME)
        p = p * p * (3 - 2 * p)  # smoothstep
        x = int(_ox + (_cx - _ox) * p)
        y = int(_oy + (_cy - _oy) * p)
        return _place_region(_sr, x, y, _cw, _ch, bg_color_tuple=_bg)

    clips.append(
        BaseVideoClip(_phase2_frame, duration=MOVE_TIME)
            .with_position((rect_x, rect_y))
            .with_start(effect_duration + move_start)
    )

    # ── Phase 3: 중앙에서 1x → 1.2x 확대 ────────────────────────────────────
    sr3 = spc_region.copy()

    def _phase3_frame(t, _sr=sr3, _cw=_cw, _ch=_ch, _lh=line_height,
                      _cx=center_x, _cy=center_rel_y, _bg=bg_color_tuple):
        base  = _place_region(_sr, _cx, _cy, _cw, _ch, bg_color_tuple=_bg)
        scale = 1.0 + 0.2 * min(1.0, t / SCALE_TIME)
        return _scale_frame_centered(base, scale, _cw, _ch, bg_color_tuple=_bg)

    clips.append(
        BaseVideoClip(_phase3_frame, duration=SCALE_TIME)
            .with_position((rect_x, rect_y))
            .with_start(effect_duration + scale_start)
    )

    # ── Phase 4: spc_word 1.2x + shimmer ─────────────────────────────────────
    if shimmer_duration > 0.0:
        sf2 = _scale_frame_centered(
            _place_region(spc_region.copy(), center_x, center_rel_y, _cw, _ch, bg_color_tuple=bg_color_tuple),
            1.2, _cw, _ch, bg_color_tuple=bg_color_tuple
        )

        def _shimmer_vid_frame(t, _sf2=sf2):
            return _sf2

        def _shimmer_mask_frame(t, __w=_cw, __h=_ch):
            alpha = 0.925 + 0.075 * np.cos(2 * np.pi * 1.5 * t)
            return np.full((__h, __w), float(alpha), dtype=np.float32)

        shimmer_vid  = BaseVideoClip(_shimmer_vid_frame,  duration=shimmer_duration)
        shimmer_mask = BaseVideoClip(_shimmer_mask_frame, is_mask=True, duration=shimmer_duration)
        clips.append(
            shimmer_vid.with_mask(shimmer_mask)
                       .with_position((rect_x, rect_y))
                       .with_start(effect_duration + shimmer_start)
        )

    return clips


def make_dissolve_mask(size, t, dissolve_time):
    w, h = size
    # moviepy 2.2: 0.0(검정)=완전 보임, 1.0(흰색)=완전 투명
    # 처음엔 완전히 안 보이고, dissolve_time까지 선형적으로 완전히 보이게
    if t < dissolve_time:
        alpha = 0.0 + (t / dissolve_time)  # 1.0(투명) → 0.0(보임)
    else:
        alpha = 1.0  # 완전히 보임
    return np.full((h, w), alpha, dtype=np.float32)

def generate_text_video(
    output_path,
    video_size=(1280, 720),
    rect_ratio=(0.8, 0.3),
    font_path=None,
    font_size=None,
    text="Hello,\nWorld!",
    text_color="white",
    rect_color="#222222",
    bg_color="#000000",
    duration=5.0,
    effect_duration=None,
    dissolve_time=1.0,
    line_spacing=1.0,
    max_lines=None,
    mode="shimmer",  # "shimmer" or "typing"
    spc_word=None    # 지정 시 효과 완료 후 해당 단어만 남기고 확대+shimmer
):
    # font_path가 지정되지 않은 경우 한글 지원 폰트를 자동 탐색
    font_path = font_path or _find_korean_font()

    if effect_duration is None:
        effect_duration = max(duration - 2, 0.5)
    W, H = video_size
    rw, rh = rect_ratio
    rect_w, rect_h = int(W * rw), int(H * rh)
    rect_x, rect_y = (W - rect_w) // 2, (H - rect_h) // 2

    # 줄 나누기 및 최대 줄 제한
    lines = text.split('\n')
    if max_lines is not None:
        lines = lines[:max_lines]
    n_lines = len(lines)

    # 폰트 크기 자동 계산
    if font_size is None:
        total_spacing = n_lines + (n_lines - 1) * (line_spacing - 1)
        fs_h = rect_h / total_spacing
        longest = max(lines, key=len) if lines else text
        fs_w = rect_w / (len(longest) * 0.9 + 1)
        font_size = int(min(fs_h, fs_w))
        if font_size < 10:
            font_size = 10

    # 배경, 사각형
    bg_clip = ColorClip(size=video_size, color=to_rgb_tuple(bg_color), duration=duration)
    rect_clip = ColorClip(size=(rect_w, rect_h), color=to_rgb_tuple(rect_color), duration=duration).with_position((rect_x, rect_y))

    # 각 줄마다 TextClip 생성 및 위치 계산
    text_clips = []
    line_height = int(font_size * 1.2)  # ascender/descender 여백 확보
    total_text_height = n_lines * line_height + (n_lines - 1) * int(font_size * (line_spacing - 1))
    start_y = rect_y + (rect_h - total_text_height) // 2

    # rect_color를 tuple로 미리 변환 (TextClip bg_color에 사용)
    rect_color_tuple = to_rgb_tuple(rect_color)

    if mode == "shimmer":
        for i, line in enumerate(lines):
            y = start_y + i * (line_height + int(font_size * (line_spacing - 1)))
            # 효과 구간: effect_duration, 이후 정지 구간: duration-effect_duration
            clip_effect = TextClip(
                text=line,
                font_size=font_size,
                font=font_path if font_path else "Arial",
                color=text_color,
                bg_color=rect_color_tuple,
                size=(rect_w, line_height),
                method="caption",
                text_align="center",
                horizontal_align="center",
                vertical_align="center"
            ).with_duration(effect_duration)
            clip_static = clip_effect.to_ImageClip().with_duration(duration - effect_duration)
            # shimmer/typing 모두에서
            concatenated = concatenate_videoclips([clip_effect.with_position((rect_x, y)), clip_static.with_position((rect_x, y))])
            text_clips.append(concatenated.with_position((rect_x, y)))

        # 마스크(디졸브+반짝임) — 각 클립의 실제 크기(rect_w, line_height)에 맞게 생성
        def make_line_mask(w, h):
            def mask_frame(t):
                t_eff = min(t, effect_duration)
                dissolve = make_dissolve_mask((w, h), t_eff, dissolve_time)
                shimmer = make_shimmer_mask((w, h), t_eff, effect_duration, min_alpha=0.7, max_alpha=1.0)
                return np.minimum(dissolve, shimmer)
            return BaseVideoClip(mask_frame, is_mask=True, duration=duration)

        text_clips = [clip.with_mask(make_line_mask(rect_w, line_height)) for clip in text_clips]

    elif mode == "typing":
        # 타이핑 효과: effect_duration 동안만 진행, 이후 정지
        total_chars = sum(len(line) for line in lines)
        if total_chars == 0:
            total_chars = 1
        char_time = effect_duration / total_chars

        for i, line in enumerate(lines):
            y = start_y + i * (line_height + int(font_size * (line_spacing - 1)))
            start_idx = sum(len(l) for l in lines[:i])
            def make_typing_clip(line, y, start_idx, _rc=rect_color_tuple):
                def make_frame(t, _rc=_rc):
                    # 마지막 프레임에서 전체 문장이 보이도록 np.ceil 사용 및 t==effect_duration 보정
                    chars = int(np.ceil((t / char_time) - start_idx))
                    if t >= effect_duration:
                        chars = len(line)
                    chars = int(np.clip(chars, 0, len(line)))
                    txt = line[:chars]
                    if not txt:
                        # 빈 프레임은 rect_color로 채운 RGB 반환
                        img = np.full((line_height, rect_w, 3), _rc, dtype=np.uint8)
                        return img
                    # TextClip을 매 프레임 생성 (RGB, bg_color=rect_color)
                    txt_clip = TextClip(
                        text=txt,
                        font_size=font_size,
                        font=font_path if font_path else "Arial",
                        color=text_color,
                        bg_color=_rc,
                        size=(rect_w, line_height),
                        method="caption",
                        text_align="center"
                    ).with_duration(effect_duration)
                    frame = txt_clip.get_frame(0)
                    # 채널이 4개인 경우 RGB로 변환
                    if frame.shape[2] == 4:
                        frame = frame[:, :, :3]
                    return frame
                # 효과 구간: effect_duration, 이후 정지 구간: duration-effect_duration
                effect_clip = BaseVideoClip(make_frame, duration=effect_duration).with_position((rect_x, y)).with_duration(effect_duration)
                # 마지막 상태의 정지 이미지
                static_img = np.full((line_height, rect_w, 3), _rc, dtype=np.uint8)
                if len(line) > 0:
                    txt_clip = TextClip(
                        text=line,
                        font_size=font_size,
                        font=font_path if font_path else "Arial",
                        color=text_color,
                        bg_color=_rc,
                        size=(rect_w, line_height),
                        method="caption",
                        text_align="center"
                    ).with_duration(duration)
                    static_img = txt_clip.get_frame(0)
                    if static_img.shape[2] == 4:
                        static_img = static_img[:, :, :3]
                static_clip = BaseVideoClip(lambda t: static_img, duration=duration-effect_duration).with_position((rect_x, y)).with_duration(duration-effect_duration)
                concatenated = concatenate_videoclips([effect_clip.with_position((rect_x, y)), static_clip.with_position((rect_x, y))])
                return concatenated.with_position((rect_x, y))
            text_clips.append(make_typing_clip(line, y, start_idx))

    # ── spc_word 후처리 단계 ──────────────────────────────────────────────────
    if spc_word:
        # 기존 text_clips 을 effect_duration 까지로 잘라냄 (subclipped가 더 안정적)
        text_clips = [clip.subclipped(0, effect_duration) for clip in text_clips]

        # spc_word 강조 클립 추가
        post_clips = make_spc_word_post_clips(
            lines=lines,
            spc_word=spc_word,
            rect_x=rect_x,
            rect_y=rect_y,
            start_y=start_y,
            rect_w=rect_w,
            rect_h=rect_h,
            line_height=line_height,
            font_size=font_size,
            font_path=font_path,
            text_color=text_color,
            effect_duration=effect_duration,
            duration=duration,
            line_spacing=line_spacing,
            bg_color_tuple=rect_color_tuple,
        )
        text_clips = text_clips + post_clips

    video = CompositeVideoClip([bg_clip, rect_clip] + text_clips, size=video_size, use_bgclip=True)
    video.write_videofile(output_path, fps=30, codec="libx264", audio=False)

# 아래 argparse에 mode 인자 추가
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate a video with dissolve and shimmer or typing text.")
    parser.add_argument('--output', type=str, default='output.mp4', help='Output video file path')
    parser.add_argument('--width', type=int, default=1280, help='Video width')
    parser.add_argument('--height', type=int, default=720, help='Video height')
    parser.add_argument('--rect_ratio_w', type=float, default=0.9, help='Rectangle width ratio')
    parser.add_argument('--rect_ratio_h', type=float, default=0.3, help='Rectangle height ratio')
    parser.add_argument('--font_path', type=str, default=None, help='Font path (.ttf)')
    parser.add_argument('--font_size', type=int, default=None, help='Font size (auto if not set)')
    parser.add_argument('--text', type=str, default='샘플 텍스트', help='Text to display')
    parser.add_argument('--text_color', type=str, default='#FFFFFF', help='Text color')
    parser.add_argument('--rect_color', type=str, default="#000000", help='Rectangle color')
    parser.add_argument('--bg_color', type=str, default='#000000', help='Background color')
    parser.add_argument('--duration', type=float, default=5.0, help='Total video duration (seconds)')
    parser.add_argument('--dissolve_time', type=float, default=1.0, help='Dissolve effect duration (seconds)')
    parser.add_argument('--effect_duration', type=float, default=None, help='텍스트 효과가 진행되는 시간(초), None이면 duration-2')
    parser.add_argument('--line_spacing', type=float, default=1.0, help='Line spacing (e.g. 1.2 for 20% extra)')
    parser.add_argument('--max_lines', type=int, default=None, help='Maximum number of lines for font size calculation')
    parser.add_argument('--mode', type=str, choices=['shimmer', 'typing'], default='typing', help='출력 모드(shimmer/typing)')
    parser.add_argument('--spc_word', type=str, default=None, help='효과 완료 후 이 단어만 남기고 나머지는 디졸브, 해당 단어는 1.2배 확대 후 shimmer')
    args = parser.parse_args()

    generate_text_video(
        output_path=args.output,
        video_size=(args.width, args.height),
        rect_ratio=(args.rect_ratio_w, args.rect_ratio_h),
        font_path=args.font_path,
        font_size=args.font_size,
        text=args.text,
        text_color=args.text_color,
        rect_color=args.rect_color,
        bg_color=args.bg_color,
        duration=args.duration,
        effect_duration=args.effect_duration,
        dissolve_time=args.dissolve_time,
        line_spacing=args.line_spacing,
        max_lines=args.max_lines,
        mode=args.mode,
        spc_word=args.spc_word,
    )
