"""Sender -> receiver latency baseline, with no TV decoder in the loop.

What is measured: for every frame, (time the last packet of the frame reached the receiver) minus (the
instant the sender stamped on the frame). The stamp is recovered the way a real receiver does it: the RTP
timestamp is mapped to wall-clock time through the latest RTCP Sender Report (NTP <-> RTP). Both ends run
on this machine, so one clock serves both and the figure is a true one-way delay.

What it covers: the sender's packetizer, AES, pacer, the loopback socket and reassembly + decryption in
the receiver. What it does not cover: screen capture and the encoder (the sender stamps the frame with
its own *estimate* of the capture time, see core/capture.py), Wi-Fi, and everything a real TV adds
(jitter buffer = targetDelay, hardware decode, display). Treat it as the floor under the end-to-end
figure; the real TV's share has to be measured with the real TV (tests/live).

Writes docs/perf/virtual-tv-latency-baseline.md when VTV_WRITE_BASELINE=1.
"""
import asyncio
import os
import platform
import statistics
import time
from pathlib import Path

import pytest

from airlinx.core import models
from airlinx.core.cast.mirror import MirrorSession

import vtvkit
from vtvkit import make_device, synthetic_source

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "perf" / "virtual-tv-latency-baseline.md"
SAMPLE_S = 10
WARMUP_FRACTION = 0.2

CONFIGS = [
    ("libx264", "x264", 1280, 720, 60, 8_000_000),
    ("libx264", "x264", 1920, 1080, 60, 12_000_000),
    ("h264_nvenc", "nvenc", 1920, 1080, 60, 12_000_000),
]


def pct(sorted_vals, p):
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p / 100))]


def summarize(vals_ms):
    v = sorted(vals_ms)
    return {"n": len(v), "p50": pct(v, 50), "p90": pct(v, 90), "p99": pct(v, 99), "max": v[-1],
            "mean": statistics.fmean(v), "stdev": statistics.pstdev(v)}


async def measure(make_tv, monkeypatch, encoder, w, h, fps, bitrate):
    vtvkit.patch_capture(monkeypatch, encoder=encoder)
    tv = await make_tv()
    live, stats = asyncio.Event(), []
    s = MirrorSession(make_device(tv), synthetic_source(w, h),
                      models.MirrorOptions(fps=fps, bitrate=bitrate, max_height=h, audio=True, target_delay_ms=100),
                      stats.append, lambda st, d: live.set() if st == "live" else None)
    await s.start()
    await asyncio.wait_for(live.wait(), 10)
    await asyncio.sleep(SAMPLE_S)
    plane = tv.plane
    label = s._media.vcap.encoder_label                      # noqa: SLF001 (diagnostics)
    sender_view = stats[-1]
    await s.stop()
    out = {"encoder": label, "w": w, "h": h, "fps": fps, "bitrate": bitrate,
           "sender_latency_ms": sender_view.latency_ms, "sender_stages": sender_view.stages}
    for kind in ("video", "audio"):
        recs = [r for r in plane.records(kind) if r.capture_wall is not None]
        recs = recs[int(len(recs) * WARMUP_FRACTION):]
        out[kind] = summarize([(r.t_done - r.capture_wall) * 1000 for r in recs])
        out[kind + "_rate"] = len(plane.records(kind)) / (SAMPLE_S + 1.5)
    return out


def render(results) -> str:
    lines = ["# Virtual TV: sender -> receiver latency baseline", "",
             "Measured by `tests/virtual_tv/test_latency.py` against the virtual TV on this host "
             f"({platform.processor() or platform.machine()}, {os.cpu_count()} cores, Python {platform.python_version()}, "
             f"{time.strftime('%Y-%m-%d')}). One-way delay on a single clock: *time the frame was complete at the "
             "receiver* minus *the instant the sender stamped on it* (RTP timestamp mapped through the RTCP "
             "Sender Report). Per-frame, 10 s of steady state per row, first 20% dropped.", "",
             "**Not included:** screen capture and encoding (the stamp is the sender's own capture-time estimate), "
             "Wi-Fi, and everything inside a real TV (jitter buffer of `targetDelay`, hardware decode, display). "
             "This is the floor under the end-to-end latency, with no TV decoder in the loop.", "",
             "| Encoder | Output | Stream | frames | p50 ms | p90 ms | p99 ms | max ms | stdev ms |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        for kind in ("video", "audio"):
            m = r[kind]
            lines.append(f"| {r['encoder']} | {r['w']}x{r['h']}@{r['fps']} {r['bitrate'] // 1_000_000} Mb/s | {kind} | "
                         f"{m['n']} | {m['p50']:.1f} | {m['p90']:.1f} | {m['p99']:.1f} | {m['max']:.1f} | {m['stdev']:.1f} |")
    lines += ["", "For comparison, the sender's own estimate shown in the UI for the same runs "
              "(`capture + encode + network + tv`; its `tv` term is the *assumed* 100 ms `targetDelay`, not measured):", ""]
    for r in results:
        st = r["sender_stages"]
        lines.append(f"- {r['encoder']} {r['w']}x{r['h']}: {r['sender_latency_ms']:.0f} ms total = "
                     + " + ".join(f"{k} {v}" for k, v in st.items()))
    lines += ["", "Reading the table: video p50 of ~9 ms is packetization, pacing and loopback; the audio figure is "
              "larger because the sender back-dates audio by an assumed 20 ms Pulse/Opus fragment and packets are 10 ms "
              "long. The p99/max columns show scheduling jitter of the sender process, the number to watch for regressions "
              "(for example a pacer change that bursts keyframes).", ""]
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_sender_to_receiver_latency_baseline(make_tv, monkeypatch):
    caps = await __import__("airlinx.core.capture", fromlist=["probe_encoders"]).probe_encoders()
    results = []
    for label, enc, w, h, fps, br in CONFIGS:
        if enc == "nvenc" and not caps.get("nvenc"):
            continue
        results.append(await measure(make_tv, monkeypatch, enc, w, h, fps, br))
        monkeypatch.undo()
    assert results, "no configuration ran"
    for r in results:
        v, a = r["video"], r["audio"]
        assert v["n"] > 300 and a["n"] > 500, f"too few frames measured: {r}"
        # generous bounds: this is a regression tripwire, the committed baseline has the real numbers
        assert 0 <= v["p50"] < 60, f"video p50 {v['p50']:.1f} ms"
        assert v["p99"] < 150, f"video p99 {v['p99']:.1f} ms (sender-side stall?)"
        assert 0 <= a["p50"] < 100 and a["p99"] < 150, f"audio {a}"
    if os.environ.get("VTV_WRITE_BASELINE") == "1":
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(render(results), "utf-8")
