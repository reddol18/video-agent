"""비가시 워터마크 (내보내기 옵션, 기본 끔).

최종 합성(final_composer)의 마지막 영상 인코딩 직전 프레임에 Video Seal 워터마크를 1회 넣는다.
  muted_output.mp4 ─(디코딩)→ videoseal_filter.py ─(raw 프레임)→ libx264 → muted_output_wm.mp4
이후 최종 합성은 이 파일의 영상을 재인코딩 없이 복사(-c:v copy)하므로 워터마크 뒤 추가 압축이 없다.

PyTorch·Video Seal 은 프로젝트 venv 에 넣지 않고 별도 환경에서 실행한다.
  WATERMARK_PYTHON   : 그 환경의 python 실행 파일 (기본 ../../wm-lab/.venv/Scripts/python.exe)
  VIDEOSEAL_DIR      : facebookresearch/videoseal clone 경로 (기본 ../../wm-lab/videoseal)
  WATERMARK_CREATOR_ID: 페이로드로 넣을 제작자 ID 문자열 (기본 "video-agent")
  WATERMARK_MODEL    : 모델 카드 이름 pixelseal | videoseal (기본 pixelseal, 같은 clone 에 포함)
페이로드 = SHA-256(제작자 ID) 256비트. 같은 ID 는 항상 같은 페이로드라 검증 때 재계산 가능.

검증(의심 파일 검사):
  uv run python -m src.watermark <파일> [--creator ID] [--model pixelseal|videoseal] [--json]
  비트 일치율 0.70 이상이면 검출. web/data/*/output/watermark_log.json 과 파일 해시를 대조해 삽입 기록을 찾는다.
  종료 코드: 검출 0, 미검출 1.

삽입마다 output/watermark_log.json 에 원본·결과 해시, 페이로드, 시각을 남긴다(출처 기록).
워터마크 전 원본은 muted_output.mp4 로 그대로 남는다(다음 렌더 때 갱신).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .utils.ffmpeg_utils import ffmpeg_bin, ffprobe_bin

_REPO = Path(__file__).resolve().parent.parent
_LAB = _REPO.parent.parent / "wm-lab"
FILTER_SCRIPT = _REPO / "scripts" / "watermark" / "videoseal_filter.py"
LOG_PATH = "output/watermark_log.json"
# 모델 카드 이름 → 기록용 방식 이름. 검출할 때도 삽입한 모델과 같은 모델을 써야 한다.
# 기본 pixelseal: #410 측정에서 슬라이드형(단색 면 91%) CRF32 재압축 비트 일치율이
# videoseal 0.770 → pixelseal 0.926, 실사 화질(PSNR)은 동급이었다.
METHODS = {
    "pixelseal": "pixelseal (256b)",
    "videoseal": "videoseal-1.0 (y_256b_img)",
}
DEFAULT_MODEL = "pixelseal"


def model_name() -> str:
    return os.environ.get("WATERMARK_MODEL") or DEFAULT_MODEL


def _python() -> str:
    return os.environ.get("WATERMARK_PYTHON") or str(_LAB / ".venv" / "Scripts" / "python.exe")


def _videoseal_dir() -> str:
    return os.environ.get("VIDEOSEAL_DIR") or str(_LAB / "videoseal")


def creator_id() -> str:
    return os.environ.get("WATERMARK_CREATOR_ID") or "video-agent"


def payload_hex(cid: str | None = None) -> str:
    return hashlib.sha256((cid or creator_id()).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def available() -> tuple[bool, str]:
    py, vs = _python(), _videoseal_dir()
    if not Path(py).exists():
        return False, f"워터마크용 파이썬이 없습니다: {py} (WATERMARK_PYTHON 설정)"
    if not Path(vs).exists():
        return False, f"Video Seal 경로가 없습니다: {vs} (VIDEOSEAL_DIR 설정)"
    if model_name() not in METHODS:
        return False, f"지원하지 않는 워터마크 모델: {model_name()} (WATERMARK_MODEL: {', '.join(METHODS)})"
    return True, ""


def _probe(path: str) -> tuple[int, int, str]:
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip().split(",")
    return int(out[0]), int(out[1]), out[2]


def _duration(path: str) -> float | None:
    """영상 스트림 길이(초). 못 읽으면 None."""
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def embed_video(src: str, dst: str, *, vf: str | None = None,
                out_size: tuple[int, int] | None = None) -> dict:
    """src 영상 프레임에 워터마크를 넣어 dst(영상만)로 인코딩. 기록용 dict 반환.

    vf: 디코딩 단계에 적용할 필터(해상도 맞춤 scale/pad). 이 경우 out_size 필수.
    """
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    w, h, fps = _probe(src)
    if vf:
        assert out_size, "vf 를 쓰면 out_size 가 필요합니다"
        w, h = out_size
    cid = creator_id()
    phex = payload_hex(cid)
    t0 = time.time()

    dec_cmd = [ffmpeg_bin(), "-v", "error", "-i", src]
    if vf:
        # 가변 fps 입력도 고정 fps 로 맞춰야 인코더(-r fps)에서 시각이 어긋나지 않는다.
        dec_cmd += ["-vf", f"{vf},fps={fps}"]
    dec_cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    flt_cmd = [_python(), str(FILTER_SCRIPT), "--width", str(w), "--height", str(h),
               "--payload-hex", phex, "--videoseal-dir", _videoseal_dir(), "--model", model_name()]
    enc_cmd = [ffmpeg_bin(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{w}x{h}", "-r", fps, "-i", "-",
               "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p", dst]

    flt_log = Path(dst).with_suffix(".filter.log")
    with open(flt_log, "w", encoding="utf-8") as flt_err:
        if vf:
            # 해상도 맞춤이 필요하면 ffmpeg 가 scale/pad 해서 파이프로 넘긴다(드문 경우).
            dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            flt = subprocess.Popen(flt_cmd, stdin=dec.stdout, stdout=subprocess.PIPE, stderr=flt_err)
            dec.stdout.close()  # flt 가 끝나면 dec 가 종료되도록
        else:
            # 보통은 필터가 파일을 직접 디코딩한다(Windows 파이프 입력이 병목이라).
            dec = None
            flt = subprocess.Popen([*flt_cmd, "--input", str(Path(src).resolve()), "--fps", fps],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=flt_err)
        enc = subprocess.Popen(enc_cmd, stdin=flt.stdout, stderr=subprocess.PIPE)
        flt.stdout.close()
        _, enc_err = enc.communicate()
        flt_rc = flt.wait()
        dec_err, dec_rc = b"", 0
        if dec is not None:
            dec_err = dec.stderr.read()  # stdout 은 위에서 닫았으므로 communicate() 를 쓰지 않는다
            dec_rc = dec.wait()
    if dec_rc or flt_rc or enc.returncode:
        raise RuntimeError(
            "워터마크 삽입 실패 "
            f"(decode={dec_rc}, filter={flt_rc}, encode={enc.returncode})\n"
            f"filter log: {flt_log}\n{(enc_err or b'').decode(errors='replace')[-400:]}"
            f"{(dec_err or b'').decode(errors='replace')[-400:]}"
        )
    elapsed = time.time() - t0
    src_dur, dst_dur = _duration(src), _duration(dst)
    if src_dur and dst_dur and abs(src_dur - dst_dur) > 0.5:
        raise RuntimeError(
            f"워터마크 결과 영상 길이가 원본과 다릅니다 ({src_dur:.2f}s → {dst_dur:.2f}s). "
            f"오디오와 어긋나므로 중단합니다. filter log: {flt_log}"
        )
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_dir": Path.cwd().name,
        "method": METHODS[model_name()],
        "model": model_name(),
        "nbits": 256,
        "creator_id": cid,
        "payload_hex": phex,
        "src_file": src,
        "src_sha256": sha256_file(src),
        "wm_file": dst,
        "wm_sha256": sha256_file(dst),
        "size": f"{w}x{h}",
        "fps": fps,
        "elapsed_sec": round(elapsed, 1),
        "filter_log": flt_log.read_text(encoding="utf-8", errors="replace").strip()[-300:],
    }


DETECT_SCRIPT = _REPO / "scripts" / "watermark" / "videoseal_detect.py"
THRESHOLD = 0.70  # 비트 일치율 판정 기준 (#409·#410 측정: 무워터마크 124구간 최대 0.582, 이론 오탐률 ≈3e-11)


def _all_logs() -> list[dict]:
    """web/data/*/output/watermark_log.json 전부 + 현재 폴더 기록. 각 항목에 _log_path 를 붙인다."""
    paths = set((_REPO / "web" / "data").glob("*/output/watermark_log.json"))
    if Path(LOG_PATH).exists():
        paths.add(Path(LOG_PATH).resolve())
    out = []
    for p in sorted(paths):
        try:
            for rec in json.loads(p.read_text(encoding="utf-8")):
                rec["_log_path"] = str(p)
                rec.setdefault("model", "videoseal")  # model 필드 도입 전 기록은 videoseal
                out.append(rec)
        except Exception:
            continue
    return out


def detect_video(path: str, *, creator: str | None = None, models: list[str] | None = None,
                 max_frames: int = 300) -> dict:
    """의심 파일 검사. 반환: 검출 결과 + 판정 + 일치하는 삽입 기록."""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    cid = creator or creator_id()
    phex = payload_hex(cid)
    fhash = sha256_file(path)
    logs = _all_logs()
    exact = [r for r in logs if fhash in (r.get("final_sha256"), r.get("wm_sha256"))]
    if not models:
        # 파일이 기록과 똑같으면 그 모델만, 아니면 지원 모델 전부로 검사한다.
        models = [exact[0]["model"]] if exact else list(METHODS)
    proc = subprocess.run(
        [_python(), str(DETECT_SCRIPT), "--input", str(Path(path).resolve()), "--videoseal-dir", _videoseal_dir(),
         "--models", ",".join(models), "--payload-hex", phex, "--max-frames", str(max_frames)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"검출 실패 (rc={proc.returncode})\n{proc.stderr[-800:]}")
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    best = max(res["models"].items(), key=lambda kv: kv[1]["bit_acc"])
    res.update({
        "file_sha256": fhash,
        "creator_id": cid,
        "payload_hex": phex,
        "threshold": THRESHOLD,
        "best_model": best[0],
        "bit_acc": best[1]["bit_acc"],
        "detected": best[1]["bit_acc"] >= THRESHOLD,
        "exact_records": exact,
        # 해시가 다르면(재압축·편집본) 같은 페이로드·모델의 기록을 후보로 최신순 제시
        "candidate_records": [] if exact else sorted(
            (r for r in logs if r.get("payload_hex") == phex and r.get("model") == best[0]),
            key=lambda r: r.get("created_at", ""), reverse=True)[:5],
    })
    return res


def _print_report(r: dict) -> None:
    print(f"파일        : {r['input']}")
    print(f"해상도/샘플 : {r['size']}, 전체 {r['frames_total']}프레임 중 {r['frames_used']}장 검사")
    print(f"제작자 ID   : {r['creator_id']}  (페이로드 {r['payload_hex'][:16]}…)")
    for name, m in r["models"].items():
        print(f"  {name:<10} 비트 일치율 {m['bit_acc']:.4f}")
    verdict = "✅ 워터마크 있음 — 페이로드 일치" if r["detected"] else "❌ 검출 안 됨"
    print(f"판정        : {verdict} (최고 {r['bit_acc']:.4f} / 기준 {r['threshold']}, 모델 {r['best_model']})")
    recs, kind = (r["exact_records"], "파일 해시가 똑같은 기록") if r["exact_records"] else \
        (r["candidate_records"], "같은 제작자·모델의 최근 기록(후보 — 파일이 편집·재압축돼 해시로는 특정 불가)")
    if r["detected"] or r["exact_records"]:
        print(f"삽입 기록   : {kind} {len(recs)}건")
        for rec in recs:
            print(f"  - {rec.get('created_at')}  프로젝트 {rec.get('project_dir')}  {rec.get('size')}  "
                  f"{rec.get('method')}  ({rec['_log_path']})")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m src.watermark",
                                 description="의심 파일에 우리 비가시 워터마크가 있는지 검사합니다.")
    ap.add_argument("file")
    ap.add_argument("--creator", help="검사할 제작자 ID (기본 WATERMARK_CREATOR_ID)")
    ap.add_argument("--model", choices=list(METHODS), help="검출 모델 고정 (기본: 기록에서 찾거나 전부 시도)")
    ap.add_argument("--max-frames", type=int, default=300)
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    a = ap.parse_args(argv)
    r = detect_video(a.file, creator=a.creator, models=[a.model] if a.model else None, max_frames=a.max_frames)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        _print_report(r)
    return 0 if r["detected"] else 1


def append_log(record: dict, log_path: str = LOG_PATH) -> None:
    p = Path(log_path)
    items = []
    if p.exists():
        try:
            items = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            items = []
    items.append(record)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
