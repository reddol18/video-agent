"""Video Seal 비가시 워터마크 프레임 필터 (stdin raw rgb24 → stdout raw rgb24).

이 스크립트는 프로젝트 venv 가 아니라 PyTorch·Video Seal 이 설치된 별도 환경의
파이썬으로 실행한다(src/watermark.py 가 호출). 프로젝트 의존성에 torch 를 넣지 않기 위함.

사용:
  python videoseal_filter.py --width 1920 --height 1080 --payload-hex <64자 hex> --videoseal-dir <clone 경로>
"""
import argparse
import os
import sys
import time

import numpy as np
import torch

# 프레임 스트림은 stdout 으로만 나가야 한다. 모델 로드 메시지(print)가 섞이면 영상이 깨지므로
# 원래 stdout 바이너리 핸들을 따로 잡아 두고, 이후 print 는 전부 stderr 로 보낸다.
_FRAME_OUT = sys.stdout.buffer
sys.stdout = sys.stderr

ap = argparse.ArgumentParser()
ap.add_argument("--width", type=int, required=True)
ap.add_argument("--height", type=int, required=True)
ap.add_argument("--payload-hex", required=True, help="256비트 페이로드 (hex 64자)")
ap.add_argument("--videoseal-dir", required=True)
ap.add_argument("--model", default="videoseal")
# 배치 16: TITAN X(12GB) 실측 1080p 기준 batch 16 ≈ 50fps, 32 는 화면에 따라 15~50fps,
# 64 는 GPU 메모리가 넘쳐 공유 메모리로 밀려나며 ≈ 4fps 로 급락했다.
ap.add_argument("--batch", type=int, default=16)
ap.add_argument("--input", default=None, help="지정하면 stdin 대신 이 파일을 PyAV 로 직접 디코딩")
ap.add_argument("--fps", default=None,
                help="--input 과 함께: 이 고정 fps(예 30/1)로 맞춰 내보낸다. 가변 fps 입력의 시각을 보존하기 위함")
args = ap.parse_args()

sys.path.insert(0, args.videoseal_dir)
os.chdir(args.videoseal_dir)  # 모델 카드의 상대경로(configs/..., ckpts/...) 해석용
import videoseal  # noqa: E402

dev = "cuda" if torch.cuda.is_available() else "cpu"
model = videoseal.load(args.model).eval().to(dev)
bits = bin(int(args.payload_hex, 16))[2:].zfill(len(args.payload_hex) * 4)[:256]
msg = torch.tensor([[int(b) for b in bits]], dtype=torch.float32, device=dev)

w, h = args.width, args.height
fsize = w * h * 3
src = sys.stdin.buffer
dst = _FRAME_OUT
import queue  # noqa: E402
import threading  # noqa: E402

# 읽기(디코더 파이프) · GPU 삽입 · 쓰기(인코더 파이프)를 스레드로 겹쳐 돌린다.
# 순차로 하면 디코딩·삽입·인코딩 시간이 그대로 더해져 느려진다.
in_q: "queue.Queue[np.ndarray | None]" = queue.Queue(maxsize=2)
out_q: "queue.Queue[np.ndarray | None]" = queue.Queue(maxsize=2)


def _reader_av():
    """--input 지정 시: PyAV 로 파일을 직접 디코딩. Windows 익명 파이프(4KB 버퍼)로
    1080p raw 프레임을 받으면 입력 대기가 병목이 되어(실측 GPU 23s vs 대기 53s / 900프레임) 우회한다."""
    import av
    from fractions import Fraction
    batch = []

    def emit(a):
        nonlocal batch
        batch.append(a)
        _counts["out"] += 1
        if len(batch) == args.batch:
            in_q.put(np.stack(batch))
            batch = []

    with av.open(args.input) as c:
        st = c.streams.video[0]
        st.thread_type = "AUTO"
        # 출력은 고정 fps(인코더가 -r 로 받음). 입력이 가변 fps(편집 결과물 muted_output 은
        # 평균 28.4fps 처럼 간격이 들쭉날쭉할 수 있다)이면 프레임을 그대로 흘릴 때 시각이 사라져
        # 영상이 짧아지고 오디오와 어긋난다(2026-11-20 92fb0034: 531.3s → 502.8s).
        # 그래서 ffmpeg fps 필터처럼 각 출력 틱(k/fps)에 그 시각 직전 프레임을 복제·생략해 맞춘다.
        fps = Fraction(args.fps) if args.fps else None
        k = 0
        t0 = None
        pending = None
        last_t, last_dur = 0.0, 0.0
        for fr in c.decode(st):
            a = fr.to_ndarray(format="rgb24")
            if a.shape[0] != h or a.shape[1] != w:
                raise RuntimeError(f"입력 해상도 {a.shape[1]}x{a.shape[0]} != {w}x{h}")
            _counts["in"] += 1
            if fps is None:
                emit(a)
                continue
            t = float(fr.time if fr.time is not None else _counts["in"] - 1)
            if t0 is None:
                t0 = t
            t -= t0
            while pending is not None and k / fps < t - 0.5 / fps:
                emit(pending)
                k += 1
            pending = a
            last_t = t
            last_dur = float(fr.duration * st.time_base) if fr.duration else float(1 / fps)
        if fps is not None and pending is not None:
            end = last_t + last_dur
            while k / fps < end - 0.5 / fps:
                emit(pending)
                k += 1
        if batch:
            in_q.put(np.stack(batch))
    in_q.put(None)


_counts = {"in": 0, "out": 0}


_reader_err: list[BaseException] = []


def _reader():
    # 읽기 스레드 예외 시 메인 루프가 영원히 기다리지 않도록 종료 신호(None)를 넣는다.
    try:
        _reader_impl()
    except BaseException as e:  # noqa: BLE001
        _reader_err.append(e)
        in_q.put(None)


def _reader_impl():
    if args.input:
        return _reader_av()
    while True:
        arr = np.empty((args.batch, h, w, 3), np.uint8)
        mv = memoryview(arr).cast("B")
        got = 0
        while got < len(mv):
            k = src.readinto(mv[got:])
            if not k:
                break
            got += k
        cnt = got // fsize
        if cnt:
            in_q.put(arr[:cnt])
        if cnt < args.batch:
            in_q.put(None)
            return


def _writer():
    while True:
        out = out_q.get()
        if out is None:
            return
        dst.write(memoryview(out).cast("B"))


rt = threading.Thread(target=_reader, daemon=True)
wt = threading.Thread(target=_writer, daemon=True)
rt.start()
wt.start()
n = 0
gpu_sec = wait_in = wait_out = 0.0
t0 = time.time()
with torch.no_grad():
    while True:
        tw = time.time()
        frames = in_q.get()
        wait_in += time.time() - tw
        if frames is None:
            break
        tg = time.time()
        x = torch.from_numpy(frames).to(dev).permute(0, 3, 1, 2).float().div_(255.0)
        yw = model.embed(x, msgs=msg, is_video=True)["imgs_w"]
        out = (yw.clamp_(0, 1) * 255).round_().byte().permute(0, 2, 3, 1).contiguous().cpu().numpy()
        gpu_sec += time.time() - tg
        tq = time.time()
        out_q.put(out)
        wait_out += time.time() - tq
        n += len(frames)
out_q.put(None)
wt.join()
dst.flush()
if _reader_err:
    print(f"[videoseal_filter] 입력 읽기 실패: {_reader_err[0]!r}", file=sys.stderr, flush=True)
    sys.exit(2)
el = time.time() - t0
print(f"[videoseal_filter] device={dev} frames={n} (in={_counts['in']} out={_counts['out']}) sec={el:.1f} fps={n / max(el, 1e-6):.1f} "
      f"gpu={gpu_sec:.1f}s wait_input={wait_in:.1f}s wait_output={wait_out:.1f}s", file=sys.stderr, flush=True)
