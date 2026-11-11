#!/usr/bin/env python3
"""H.264 vs H.265 on the app's own encoder command lines: time per frame, size and quality.

Everything is local: synthetic lavfi sources are rendered once to raw YUV in /dev/shm, then encoded with the
exact ffmpeg arguments `airlinx.core.capture.build_video_cmd` builds (only the x11grab input and the pipe output
are swapped for a raw file), decoded again and compared with the source (PSNR, SSIM). No TV, no network.

  .venv/bin/python scripts/bench_hevc.py --out /tmp/hevc.json            # full matrix (~10 min)
  .venv/bin/python scripts/bench_hevc.py --quick --out /tmp/hevc.json    # one resolution, one content

The result is also printed as Markdown tables (the ones in docs/perf/hevc.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from airlinx.core.capture import VideoParams, build_video_cmd, codec_of  # noqa: E402

SECONDS = 5
# (name, width, height, fps, [bitrates Mb/s])
RESOLUTIONS = [("1080p60", 1920, 1080, 60, [12, 8, 6]),
               ("1440p60", 2560, 1440, 60, [18, 12]),
               ("2160p30", 3840, 2160, 30, [28, 18, 12])]
CONTENTS = {   # lavfi graph, rendered once per resolution
    "testsrc2": "testsrc2=s={w}x{h}:r={fps}:d={d}",                      # flat colours, text, steady motion (UI-like)
    # Game of Life, blocky cells, every pixel changes every frame: the worst case for a screen encoder
    "life": "life=s=480x270:r={fps}:ratio=0.2:mold=10:death_color=#202040:life_color=#40ff80,scale={w}:{h}:flags=neighbor,trim=duration={d}",
}
ENCODERS = [("h264", "h264_nvenc"), ("hevc", "hevc_nvenc")]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def render(workdir: Path, content: str, w: int, h: int, fps: int) -> Path:
    out = workdir / f"{content}_{w}x{h}_{fps}.yuv"
    if not out.exists():
        graph = CONTENTS[content].format(w=w, h=h, fps=fps, d=SECONDS)
        r = run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", graph, "-pix_fmt", "yuv420p", "-f", "rawvideo", str(out)])
        if r.returncode:
            raise SystemExit(f"cannot render {content}: {r.stderr}")
    return out


def encode_cmd(src: Path, w: int, h: int, fps: int, bitrate: int, encoder: str, out: Path, frames: int | None,
               preset: str = "p1") -> list[str]:
    p = VideoParams(geometry=(0, 0, w, h), out_w=w, out_h=h, fps=fps, bitrate=bitrate, encoder=encoder, gpu_scale=True)
    cmd = build_video_cmd(p, ":0")
    cmd[cmd.index("-loglevel") + 1] = "info"          # the benchmark line is info level
    cmd.insert(1, "-y")
    if "nvenc" in encoder:
        cmd[cmd.index("-preset") + 1] = preset
    i = cmd.index("-f")
    j = cmd.index("-vf")
    cmd = cmd[:i] + ["-benchmark", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-r", str(fps),
                     "-i", str(src)] + cmd[j:]
    cmd = cmd[:-3] + ["-f", "hevc" if codec_of(encoder) == "hevc" else "h264", str(out)]   # drop '-f <mux> pipe:1'
    if frames:
        cmd[cmd.index("-vf"):cmd.index("-vf")] = ["-frames:v", str(frames)]
    return cmd


def rtime(stderr: str) -> float:
    m = re.search(r"bench: utime=\S+ stime=\S+ rtime=([\d.]+)s", stderr)
    if not m:
        raise RuntimeError("no benchmark line:\n" + stderr[-800:])
    return float(m.group(1))


def quality(src: Path, enc: Path, w: int, h: int, fps: int, codec: str) -> tuple[float, float]:
    r = run(["ffmpeg", "-hide_banner", "-nostdin", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-r", str(fps),
             "-i", str(src), "-r", str(fps), "-f", codec if codec == "hevc" else "h264", "-i", str(enc), "-lavfi",
             "[0:v]split=2[a1][a2];[1:v]split=2[b1][b2];[b1][a1]psnr;[b2][a2]ssim", "-f", "null", "-"])
    psnr = re.search(r"PSNR y:\S+ u:\S+ v:\S+ average:([\d.inf]+)", r.stderr)
    ssim = re.search(r"SSIM Y:\S+ \(\S+\) U:\S+ \(\S+\) V:\S+ \(\S+\) All:([\d.]+)", r.stderr)
    if not psnr or not ssim:
        raise RuntimeError("no quality lines:\n" + r.stderr[-800:])
    return float(psnr.group(1)), float(ssim.group(1))


def measure(workdir: Path, content: str, res, bitrate_mbps: int, codec_name: str, encoder: str,
            preset: str = "p1") -> dict:
    name, w, h, fps, _ = res
    src = render(workdir, content, w, h, fps)
    frames = SECONDS * fps
    out = workdir / f"{content}_{name}_{encoder}_{bitrate_mbps}_{preset}.bin"
    one = run(encode_cmd(src, w, h, fps, bitrate_mbps * 1_000_000, encoder, out, 1, preset))
    init = rtime(one.stderr)
    full = run(encode_cmd(src, w, h, fps, bitrate_mbps * 1_000_000, encoder, out, None, preset))
    if full.returncode:
        raise RuntimeError(full.stderr[-800:])
    total = rtime(full.stderr)
    size = out.stat().st_size
    psnr, ssim = quality(src, out, w, h, fps, codec_name)
    out.unlink()
    return {"content": content, "res": name, "codec": codec_name, "encoder": encoder, "preset": preset, "target_mbps": bitrate_mbps,
            "actual_mbps": round(size * 8 / SECONDS / 1e6, 2), "ms_per_frame": round((total - init) / (frames - 1) * 1000, 2),
            "init_ms": round(init * 1000), "psnr": psnr, "ssim": ssim, "frames": frames}


def bursts(workdir: Path) -> list[dict]:
    """Size of the key frame, of an ordinary frame and of the biggest frame (ffprobe packets), per profile bitrate."""
    rows = []
    for res, mbps in ((RESOLUTIONS[1], 18), (RESOLUTIONS[2], 28)):
        name, w, h, fps, _ = res
        for content in CONTENTS:
            src = render(workdir, content, w, h, fps)
            for codec_name, encoder in ENCODERS:
                out = workdir / "burst.bin"
                run(encode_cmd(src, w, h, fps, mbps * 1_000_000, encoder, out, None))
                lines = run(["ffprobe", "-v", "error", "-f", codec_name, "-show_entries", "packet=size,flags", "-of", "csv=p=0",
                             str(out)]).stdout.split()
                pk = [(int(x.split(",")[0]), "K" in x.split(",")[1]) for x in lines if "," in x]
                keys = [sz for sz, k in pk if k]
                delta = sorted(sz for sz, k in pk if not k)
                rows.append({"res": name, "content": content, "codec": codec_name, "target_mbps": mbps,
                             "key_kb": round(statistics.mean(keys) / 1024, 1), "delta_mean_kb": round(statistics.mean(delta) / 1024, 1),
                             "delta_p99_kb": round(delta[int(len(delta) * .99)] / 1024, 1),
                             "max_kb": round(max(sz for sz, _ in pk) / 1024, 1)})
                print(json.dumps(rows[-1]), flush=True)
        for f in workdir.glob("*.yuv"):
            f.unlink()
    return rows


def software(workdir: Path) -> list[dict]:
    """Throughput of the CPU fallbacks (libx264 / libx265 ultrafast) at 1080p60, 8 Mb/s, whole pipeline."""
    name, w, h, fps, _ = RESOLUTIONS[0]
    src = render(workdir, "testsrc2", w, h, fps)
    rows = []
    for encoder in ("libx264", "libx265"):
        out = workdir / "sw.bin"
        t0 = time.monotonic()
        run(encode_cmd(src, w, h, fps, 8_000_000, encoder, out, None))
        dt = time.monotonic() - t0
        rows.append({"encoder": encoder, "fps": round(SECONDS * fps / dt, 1), "actual_mbps": round(out.stat().st_size * 8 / SECONDS / 1e6, 2)})
        print(json.dumps(rows[-1]), flush=True)
    return rows


def markdown(rows: list[dict]) -> str:
    out = []
    for content in CONTENTS:
        out.append(f"\n**{content}**\n")
        out.append("| Rozdzielczość | Preset | Docelowo | Kodek | Rzeczywiście | ms / klatkę | PSNR (dB) | SSIM |")
        out.append("|---|---|---|---|---|---|---|---|")
        for r in (x for x in rows if x["content"] == content):
            out.append(f"| {r['res']} | {r['preset']} | {r['target_mbps']} Mb/s | {r['codec'].upper().replace('HEVC', 'H.265').replace('H264', 'H.264')} "
                       f"| {r['actual_mbps']} Mb/s | {r['ms_per_frame']} | {r['psnr']:.2f} | {r['ssim']:.4f} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="JSON results file")
    ap.add_argument("--quick", action="store_true", help="1080p60 and testsrc2 only")
    ap.add_argument("--bursts", action="store_true", help="key frame / biggest frame sizes at the 1440p60 and 4K30 profile bitrates")
    ap.add_argument("--software", action="store_true", help="speed of libx264 / libx265 ultrafast at 1080p60")
    ap.add_argument("--presets", action="store_true", help="NVENC preset sweep (p1/p4/p7) at 1080p60 6 Mb/s and 1440p60 12 Mb/s")
    ap.add_argument("--workdir", default=None, help="raw sources go here (default: a temp dir in /dev/shm)")
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found")
    work = Path(a.workdir or tempfile.mkdtemp(prefix="airlinx-bench-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None))
    work.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    try:
        if a.bursts or a.software:
            rows = (bursts(work) if a.bursts else []) + (software(work) if a.software else [])
            Path(a.out).write_text(json.dumps(rows, indent=1))
            return
        if a.presets:
            for content in CONTENTS:
                for res, mbps in ((RESOLUTIONS[0], 6), (RESOLUTIONS[1], 12)):
                    for preset in ("p1", "p4", "p7"):
                        for codec_name, encoder in ENCODERS:
                            row = measure(work, content, res, mbps, codec_name, encoder, preset)
                            rows.append(row)
                            print(json.dumps(row), flush=True)
                    for f in work.glob(f"{content}_*.yuv"):
                        f.unlink()
            Path(a.out).write_text(json.dumps(rows, indent=1))
            print(markdown(rows))
            return
        for content in (["testsrc2"] if a.quick else list(CONTENTS)):
            for res in (RESOLUTIONS[:1] if a.quick else RESOLUTIONS):
                for mbps in res[4]:
                    for codec_name, encoder in ENCODERS:
                        row = measure(work, content, res, mbps, codec_name, encoder)
                        rows.append(row)
                        print(json.dumps(row), flush=True)
                for f in work.glob(f"{content}_{res[1]}x{res[2]}_*.yuv"):
                    f.unlink()                        # free RAM before the next resolution
        Path(a.out).write_text(json.dumps(rows, indent=1))
        print(markdown(rows))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
