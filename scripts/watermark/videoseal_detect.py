"""비가시 워터마크 검출 (별도 환경에서 실행 — src/watermark.py 의 detect_video 가 호출).

영상 전체에서 프레임을 고르게 최대 --max-frames 장 뽑아 모델별로 비트를 읽고,
기대 페이로드와의 비트 일치율을 JSON 한 줄로 stdout 에 낸다.

사용:
  python videoseal_detect.py --input x.mp4 --videoseal-dir <clone> --models pixelseal,videoseal \
      --payload-hex <64자 hex>
"""
import argparse
import json
import os
import sys

import numpy as np
import torch

_OUT = sys.stdout
sys.stdout = sys.stderr  # 모델 로드 메시지가 결과 JSON 에 섞이지 않게

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True)
ap.add_argument("--videoseal-dir", required=True)
ap.add_argument("--models", default="pixelseal,videoseal")
ap.add_argument("--payload-hex", required=True)
ap.add_argument("--max-frames", type=int, default=300)
ap.add_argument("--batch", type=int, default=16)
args = ap.parse_args()

sys.path.insert(0, args.videoseal_dir)
os.chdir(args.videoseal_dir)
import av  # noqa: E402
import videoseal  # noqa: E402

dev = "cuda" if torch.cuda.is_available() else "cpu"


def sample_frames(path, k):
    with av.open(path) as c:
        st = c.streams.video[0]
        st.thread_type = "AUTO"
        total = st.frames or 0
        if not total and c.duration and st.average_rate:  # 컨테이너에 프레임 수가 없으면 길이×fps 로 추정
            total = int(c.duration / av.time_base * float(st.average_rate))
        step = max(1, total // k) if total else 1
        out = []
        for i, fr in enumerate(c.decode(st)):
            if i % step == 0:
                out.append(fr.to_ndarray(format="rgb24"))
            if len(out) >= k:
                break
        info = {"size": f"{st.codec_context.width}x{st.codec_context.height}", "frames_total": total,
                "frames_used": len(out), "step": step}
    return np.stack(out), info


def bits_of(hexstr):
    return torch.tensor([int(b) for b in bin(int(hexstr, 16))[2:].zfill(256)][:256])


frames, info = sample_frames(args.input, args.max_frames)
expect = bits_of(args.payload_hex)
result = {"input": args.input, **info, "models": {}}
for name in [m.strip() for m in args.models.split(",") if m.strip()]:
    model = videoseal.load(name).eval().to(dev)
    preds = []
    with torch.no_grad():
        for i in range(0, len(frames), args.batch):
            x = torch.from_numpy(frames[i:i + args.batch].copy()).to(dev).permute(0, 3, 1, 2).float() / 255
            preds.append(model.detect(x, is_video=True)["preds"].cpu())
    bits = torch.cat(preds)[:, 1:].mean(0)
    dec = (bits > 0).int()
    result["models"][name] = {
        "bit_acc": round((dec == expect).float().mean().item(), 4),
        "decoded_hex": hex(int("".join(map(str, dec.tolist())), 2))[2:].zfill(64),
    }
    del model
    if dev == "cuda":
        torch.cuda.empty_cache()

_OUT.write(json.dumps(result, ensure_ascii=False) + "\n")
_OUT.flush()
