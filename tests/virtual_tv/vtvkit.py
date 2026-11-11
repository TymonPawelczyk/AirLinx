"""Helpers for the virtual-TV end-to-end tests: a synthetic screen/audio source for the real MirrorSession,
device construction, ffprobe, polling."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from airlinx.core import models
from airlinx.core.cast import mirror as mirror_mod

W, H = 1280, 720


def ffmpeg_ok() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_device(tv, *, ip: str = "127.0.0.1", dev_id: Optional[str] = None) -> models.Device:
    """The Device discovery would have produced for the virtual TV (loopback is rejected by discovery itself,
    so tests inject the device; the netns scenario uses real discovery)."""
    return models.Device(id=dev_id or tv.device_id, name=tv.name, model=tv.model, ip=ip, cast_port=tv.port,
                         caps={"cast_mirror", "cast_media"})


def synthetic_source(w: int = W, h: int = H) -> models.Source:
    return models.Source(id="monitor:vtv", kind="monitor", name="Syntetyczny ekran", geometry=(0, 0, w, h))


def patch_capture(monkeypatch, *, encoder: str = "x264") -> None:
    """Replace x11grab/pulse by lavfi test signals, keep everything else (encoder args, FLV/Ogg parsing, RTP)."""
    from airlinx.core import capture

    real_video, real_audio = capture.build_video_cmd, capture.build_audio_cmd

    def video_cmd(p, disp=None):
        cmd = list(real_video(p, disp))
        i = cmd.index("-f")                      # '-f x11grab ... -i :1+0,0'  ->  lavfi testsrc2
        j = cmd.index("-i", i) + 1
        cmd[i:j + 1] = ["-re", "-f", "lavfi", "-i", f"testsrc2=size={p.geometry[2]}x{p.geometry[3]}:rate={p.fps}"]
        return cmd

    def audio_cmd(source, bitrate=128_000):
        return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-re", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000", "-ac", "2", "-ar", "48000", "-c:a", "libopus",
                "-b:a", str(bitrate), "-application", "lowdelay", "-frame_duration", "10", "-page_duration",
                "10000", "-flush_packets", "1", "-f", "ogg", "pipe:1"]

    async def screen():
        return (3840, 2160)          # large enough for any synthetic source geometry

    async def monitor():
        return "vtv.monitor"

    async def encoders():
        caps = await real_probe()
        if encoder == "nvenc" and caps.get("nvenc"):
            return {"nvenc": True, "cuda_scale": False, "x264": caps.get("x264", True)}
        return {"nvenc": False, "cuda_scale": False, "x264": True}

    real_probe = capture.probe_encoders
    monkeypatch.setattr(capture, "build_video_cmd", video_cmd)
    monkeypatch.setattr(capture, "build_audio_cmd", audio_cmd)
    monkeypatch.setattr(mirror_mod, "screen_size", screen)
    monkeypatch.setattr(mirror_mod, "default_audio_monitor", monitor)
    monkeypatch.setattr(mirror_mod, "probe_encoders", encoders)


async def wait_until(pred: Callable[[], bool], timeout: float, what: str, step: float = 0.05) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError(f"timeout ({timeout}s) waiting for {what}")
        await asyncio.sleep(step)


def ffprobe_frames(path: Path, fmt: Optional[str] = None) -> dict:
    """Decode the whole file with ffprobe: codec, size, number of decoded frames/packets."""
    cmd = ["ffprobe", "-v", "error", "-count_frames", "-count_packets", "-show_entries",
           "stream=codec_name,codec_type,width,height,nb_read_frames,nb_read_packets,duration,sample_rate",
           "-of", "json"]
    if fmt:
        cmd += ["-f", fmt]
    r = subprocess.run(cmd + [str(path)], capture_output=True, text=True, timeout=60)
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or []
    return {"streams": streams, "stderr": r.stderr.strip(), "returncode": r.returncode}


def decode_errors(path: Path, fmt: Optional[str] = None) -> str:
    """ffmpeg decodes to null; returns whatever it complained about (empty = clean)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"] + (["-f", fmt] if fmt else []) + ["-i", str(path), "-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stderr.strip()


def make_clip(path: Path, seconds: float = 3.0, *, size: str = "320x180", audio: bool = True) -> Path:
    """Control clip: lavfi testsrc2 + sine, H.264/AAC, faststart (so a pipe can play it)."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=523:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += ["-movflags", "+faststart", str(path)]
    subprocess.run(cmd, check=True, timeout=60)
    return path


def ffmpeg_pids() -> set[int]:
    out = subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True, text=True).stdout
    return {int(x) for x in out.split()}


class NullAudio:
    """Stands in for core.audio so the controller never touches the user's PulseAudio."""

    async def list(self):
        return {"mics": [], "apps": [], "default_mic": None}

    async def resolve(self, spec):
        return spec, None

    async def cleanup(self):
        return 0


class FixedDiscovery:
    """Discovery that just reports the given devices (loopback is rejected by the real one)."""

    def __init__(self, on_change, devices):
        self.on_change, self.devices = on_change, list(devices)

    async def start(self):
        self.on_change(self.devices)

    async def stop(self):
        pass

    async def rescan(self):
        self.on_change(self.devices)

    async def add_manual(self, ip, _notify=True):
        return None


def make_controller(tmp_path, devices, *, discovery=None, settings_patch: Optional[dict] = None):
    """A real Controller wired to the real MirrorSession / CastMediaPlayer / DlnaPlayer / MediaServer; only
    discovery, the screen source and audio are test doubles. Returns (controller, events list)."""
    from airlinx.controller import Controller, Factories
    from airlinx.settings import Settings

    settings = Settings(tmp_path / "config.json")
    for k, v in (settings_patch or {}).items():
        settings.set(k, v)

    async def list_sources(with_thumbnails=True):
        return [synthetic_source()]

    async def encoders():
        return {"nvenc": False, "cuda_scale": False, "x264": True}

    ctl = Controller(settings, Factories(
        discovery=discovery or (lambda cb: FixedDiscovery(cb, devices)), list_sources=list_sources,
        audio=NullAudio(), encoders=encoders))
    events: list = []
    ctl.subscribe(lambda e, p: events.append((e, p)))
    return ctl, events


def errors_of(events) -> list[dict]:
    return [p for e, p in events if e == "error"]


# ---------------------------------------------------------------------------------------------- raw sender
def split_access_units(data: bytes, codec: str) -> list[tuple[bytes, bool]]:
    """Annex-B stream with AUD NALs -> [(access unit, is_keyframe)]. AUDs: h264 type 9, hevc type 35."""
    import re as _re
    starts = [m.start() for m in _re.finditer(b"\x00\x00\x01", data)]
    nals = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(data)
        begin = st - 1 if st > 0 and data[st - 1] == 0 else st
        nals.append((begin, end, data[st + 3]))
    aus, cur, key = [], b"", False
    for begin, end, hdr in nals:
        typ = (hdr >> 1) & 0x3F if codec == "hevc" else hdr & 0x1F
        if typ == (35 if codec == "hevc" else 9) and cur:
            aus.append((cur, key))
            cur, key = b"", False
        cur += data[begin:end]
        if (codec == "hevc" and 16 <= typ <= 21) or (codec == "h264" and typ == 5):
            key = True
    if cur:
        aus.append((cur, key))
    return aus


def encode_stream(codec: str, seconds: float = 1.5, fps: int = 30, size: str = "640x360") -> bytes:
    """A short elementary stream (Annex-B, AUDs inserted, no B-frames, keyframe every 15 frames)."""
    if codec == "hevc":
        enc = ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "keyint=15:bframes=0:log-level=none",
               "-bsf:v", "hevc_metadata=aud=insert", "-f", "hevc"]
    else:
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-g", "15", "-pix_fmt", "yuv420p",
               "-bsf:v", "h264_metadata=aud=insert", "-f", "h264"]
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}:rate={fps}:duration={seconds}", *enc, "pipe:1"],
                       capture_output=True, timeout=60)
    assert r.returncode == 0 and r.stdout, r.stderr.decode()
    return r.stdout


def encoder_available(codec: str) -> bool:
    name = "libx265" if codec == "hevc" else "libx264"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return name in r.stdout


def offer_for(codecs: list[str], *, ssrc0: int = 1000, w: int = 640, h: int = 360, fps: int = 30) -> tuple[dict, list]:
    """OFFER with one video stream per codec (index order = list order). Returns (offer, keys)."""
    streams, keys = [], []
    for i, c in enumerate(codecs):
        key, iv = os.urandom(16), os.urandom(16)
        keys.append((key, iv, ssrc0 + 10 * i))
        streams.append({"index": i, "type": "video_source", "codecName": c, "rtpProfile": "cast",
                        "rtpPayloadType": 96, "ssrc": ssrc0 + 10 * i, "maxFrameRate": f"{fps * 1000}/1000",
                        "timeBase": "1/90000", "maxBitRate": 4_000_000, "profile": "main", "level": "4",
                        "targetDelay": 100, "aesKey": key.hex(), "aesIvMask": iv.hex(), "receiverRtcpEventLog": True,
                        "channels": 1, "resolutions": [{"width": w, "height": h}]})
    return {"type": "OFFER", "offer": {"castMode": "mirroring", "receiverGetStatus": True,
                                       "supportedStreams": streams}}, keys


async def offer_only(tv, offer: dict, pin_key: str = "raw-sender"):
    """Open a channel, LAUNCH mirroring, send ``offer``; returns (channel, app, answer dict)."""
    from airlinx.core.cast import castv2 as cv
    ch = await cv.CastChannel.open(tv.host, tv.port, heartbeat=False, pin_key=pin_key)
    app = await ch.launch(cv.MIRRORING_APP_ID, timeout=10)
    await ch.connect_virtual(app["transportId"])
    ans = await ch.request(cv.NS_WEBRTC, offer, app["transportId"], id_field="seqNum", timeout=10)
    return ch, app, ans


async def send_stream(tv, codec: str, *, seconds: float = 1.5, fps: int = 30, size: str = "640x360") -> dict:
    """Push a short real H.264 or HEVC stream through AirLinx's own RTP packetizer to the virtual TV
    (no MirrorSession involved): checks the receiver by itself. Returns what was sent and the TV's plane."""
    import socket
    from airlinx.core.cast import castv2 as cv
    from airlinx.core.cast.mirror import parse_answer
    from airlinx.core.cast.rtp import StreamSender

    w, h = (int(x) for x in size.split("x"))
    data = await asyncio.to_thread(encode_stream, codec, seconds, fps, size)
    aus = split_access_units(data, codec)
    offer, [(key, iv, ssrc)] = offer_for([codec], w=w, h=h, fps=fps)
    ch, app, ans = await offer_only(tv, offer)
    try:
        info = parse_answer(ans)
        sender = StreamSender(kind="video", ssrc=ssrc, receiver_ssrc=info.receiver_ssrcs[0], pt=96, key=key,
                              iv_mask=iv, timebase=90000, origin=time.monotonic(), target_delay_ms=100)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((tv.host, info.udp_port))
        t0 = time.monotonic()
        for n, (au, key_frame) in enumerate(aus):
            out = sender.build_frame(au, key_frame, t0 + n / fps, rtp_ts=n * 90000 // fps)
            for p in out or []:
                sock.send(p.data)
            await asyncio.sleep(1 / fps)
        plane = tv.plane
        await wait_until(lambda: plane.stats()["video"]["frames"] >= len(aus), 5, "all frames at the receiver")
        sock.close()
        return {"sent": len(aus), "keyframes": sum(1 for _, k in aus if k), "plane": plane, "answer": ans}
    finally:
        await ch.stop_app(app["sessionId"])
        await ch.close()
