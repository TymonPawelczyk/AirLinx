"""ffmpeg capture/encode pipelines for Cast mirroring.

Video: ``x11grab`` (region of the root window) → scale to ``max_height`` (GPU
``hwupload_cuda,scale_cuda`` when available, else CPU ``fast_bilinear``) → ``h264_nvenc``
low-latency (preset p1, tune ull, no B-frames, CBR, short GOP) → FLV on stdout. FLV gives
exact access-unit boundaries, the keyframe flag and a per-frame timestamp without waiting
for the next AUD. Fallback encoder: ``libx264`` ultrafast/zerolatency.

Audio: one or two PulseAudio/PipeWire sources (chosen by core/audio.py: default-sink monitor,
a microphone, both mixed with ``amix``, or the ``airlinx_app`` null-sink monitor) → Opus 48 kHz
stereo, 10 ms frames → Ogg (one page per packet) on stdout.

IDR strategy (v1): ffmpeg CLI cannot force an IDR on demand, so a keyframe request
(PLI, stalled ACK window, bitrate change, window move) *restarts the encoder*. The
restart overlaps: the new process is started while the old one keeps streaming, and the
switch happens on the new process' first frame (always an IDR), so the TV sees no gap.
NVENC init takes ~150–300 ms; restarts are rate-limited by the caller.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import shutil
import struct
import time
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Sequence, Union

log = logging.getLogger("airlinx.capture")

START_CODE = b"\x00\x00\x00\x01"
ENCODE_FLOOR_S = 0.004        # minimal capture→encoder-output latency we assume (not measurable)
OFFSET_DECAY_S = 0.00002      # per frame; lets the min-offset estimator follow clock drift upwards
AUDIO_LATENCY_S = 0.020       # pulse fragment + Opus lookahead (estimate)
AUDIO_REANCHOR_S = 0.040
RESTART_TIMEOUT_S = 4.0
MAX_FAILS, FAIL_WINDOW_S = 3, 15.0


# --------------------------------------------------------------------------------------
# Container parsers (incremental, pure)
# --------------------------------------------------------------------------------------
@dataclass
class H264Frame:
    data: bytes          # Annex-B; SPS/PPS prepended to every IDR
    key: bool
    ts_ms: int           # FLV timestamp


class FlvParser:
    """Incremental FLV → H.264 Annex-B frames."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._header_done = False
        self._sps_pps = b""
        self._nal_len = 4

    def feed(self, data: bytes) -> list[H264Frame]:
        self._buf += data
        out: list[H264Frame] = []
        if not self._header_done:
            if len(self._buf) < 9:
                return out
            if self._buf[:3] != b"FLV":
                raise ValueError("not an FLV stream")
            hlen = struct.unpack_from(">I", self._buf, 5)[0]
            if len(self._buf) < hlen + 4:
                return out
            del self._buf[:hlen + 4]
            self._header_done = True
        while len(self._buf) >= 11:
            size = int.from_bytes(self._buf[1:4], "big")
            total = 11 + size + 4
            if len(self._buf) < total:
                break
            ttype = self._buf[0] & 0x1F
            ts = int.from_bytes(self._buf[4:7], "big") | (self._buf[7] << 24)
            body = bytes(self._buf[11:11 + size])
            del self._buf[:total]
            fr = self._tag(ttype, ts, body)
            if fr is not None:
                out.append(fr)
        return out

    def _tag(self, ttype: int, ts: int, body: bytes) -> Optional[H264Frame]:
        if ttype != 9 or len(body) < 5 or (body[0] & 0x0F) != 7:
            return None
        frame_type, pkt_type, payload = body[0] >> 4, body[1], body[5:]
        if pkt_type == 0:  # AVCDecoderConfigurationRecord
            self._nal_len = (payload[4] & 0x03) + 1
            nsps = payload[5] & 0x1F
            p = 6
            out = b""
            for _ in range(nsps):
                ln = struct.unpack_from(">H", payload, p)[0]
                out += START_CODE + payload[p + 2:p + 2 + ln]
                p += 2 + ln
            npps = payload[p]
            p += 1
            for _ in range(npps):
                ln = struct.unpack_from(">H", payload, p)[0]
                out += START_CODE + payload[p + 2:p + 2 + ln]
                p += 2 + ln
            self._sps_pps = out
            return None
        if pkt_type != 1:
            return None
        nals = []
        has_sps = False
        p, n = 0, self._nal_len
        while p + n <= len(payload):
            ln = int.from_bytes(payload[p:p + n], "big")
            nal = payload[p + n:p + n + ln]
            p += n + ln
            if nal and (nal[0] & 0x1F) == 7:
                has_sps = True
            nals.append(START_CODE + nal)
        key = frame_type == 1
        data = b"".join(nals)
        if key and not has_sps:
            data = self._sps_pps + data
        return H264Frame(data=data, key=key, ts_ms=ts)


class OggOpusParser:
    """Incremental Ogg → raw Opus packets (OpusHead/OpusTags skipped)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pending = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while len(self._buf) >= 27:
            if self._buf[:4] != b"OggS":
                idx = self._buf.find(b"OggS", 1)
                if idx < 0:
                    del self._buf[:-3]
                    break
                del self._buf[:idx]
                continue
            nseg = self._buf[26]
            if len(self._buf) < 27 + nseg:
                break
            segs = self._buf[27:27 + nseg]
            total = 27 + nseg + sum(segs)
            if len(self._buf) < total:
                break
            body = bytes(self._buf[27 + nseg:total])
            del self._buf[:total]
            p = 0
            for ln in segs:
                self._pending += body[p:p + ln]
                p += ln
                if ln < 255:
                    pkt, self._pending = self._pending, b""
                    if pkt and not pkt.startswith((b"OpusHead", b"OpusTags")):
                        out.append(pkt)
        return out


def opus_samples(pkt: bytes, rate: int = 48000) -> int:
    """Samples in an Opus packet (RFC 6716 §3.1, TOC byte)."""
    if not pkt:
        return 0
    toc = pkt[0]
    cfg = toc >> 3
    if cfg < 12:
        dur = [10, 20, 40, 60][cfg & 3] * rate // 1000
    elif cfg < 16:
        dur = [10, 20][cfg & 1] * rate // 1000
    else:
        dur = [25, 50, 100, 200][cfg & 3] * rate // 10000
    c = toc & 3
    n = 1 if c == 0 else 2 if c in (1, 2) else (pkt[1] & 0x3F if len(pkt) > 1 else 1)
    return dur * n


# --------------------------------------------------------------------------------------
# Geometry / capability helpers
# --------------------------------------------------------------------------------------
def output_size(w: int, h: int, max_height: int, max_width: int = 3840) -> tuple[int, int]:
    """Output resolution keeping aspect: height ≤ max_height, width ≤ max_width, both even."""
    if w <= 0 or h <= 0:
        raise ValueError("empty geometry")
    scale = min(1.0, max_height / h, max_width / w)
    ow, oh = int(round(w * scale)), int(round(h * scale))
    return max(2, ow - ow % 2), max(2, oh - oh % 2)


def display() -> str:
    return os.environ.get("DISPLAY") or ":0"


_caps: dict[str, bool] = {}


async def _ffmpeg_ok(args: list[str], timeout: float = 8.0) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await asyncio.wait_for(p.wait(), timeout) == 0
    except (OSError, asyncio.TimeoutError):
        return False


async def probe_encoders() -> dict[str, bool]:
    """Detect NVENC and CUDA scaling once (cached)."""
    if not _caps:
        src = ["-f", "lavfi", "-i", "testsrc2=s=256x144:r=30", "-frames:v", "3"]
        _caps["nvenc"] = await _ffmpeg_ok(src + ["-c:v", "h264_nvenc", "-f", "null", "-"])
        _caps["cuda_scale"] = _caps["nvenc"] and await _ffmpeg_ok(
            src + ["-vf", "format=bgr0,hwupload_cuda,scale_cuda=192:108", "-c:v", "h264_nvenc", "-f", "null", "-"])
        _caps["x264"] = await _ffmpeg_ok(src + ["-c:v", "libx264", "-f", "null", "-"])
        log.info("encoder capabilities: %s", _caps)
    return dict(_caps)


@dataclass
class VideoParams:
    geometry: tuple[int, int, int, int]       # capture region x, y, w, h (root coords)
    out_w: int
    out_h: int
    fps: int = 60
    bitrate: int = 12_000_000
    gop_s: float = 2.0
    draw_mouse: bool = True
    encoder: str = "h264_nvenc"               # or libx264
    gpu_scale: bool = True


def build_video_cmd(p: VideoParams, disp: Optional[str] = None) -> list[str]:
    x, y, w, h = p.geometry
    gop = max(1, int(round(p.fps * p.gop_s)))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
           "-f", "x11grab", "-draw_mouse", "1" if p.draw_mouse else "0", "-framerate", str(p.fps),
           "-video_size", f"{w}x{h}", "-i", f"{disp or display()}+{x},{y}"]
    if p.encoder == "h264_nvenc" and p.gpu_scale:
        cmd += ["-vf", f"hwupload_cuda,scale_cuda={p.out_w}:{p.out_h}"]
    else:
        cmd += ["-vf", f"scale={p.out_w}:{p.out_h}:flags=fast_bilinear,format=nv12"]
    if p.encoder == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-delay", "0",
                "-rc", "cbr", "-forced-idr", "1"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
    cmd += ["-bf", "0", "-g", str(gop), "-profile:v", "main",
            "-b:v", str(p.bitrate), "-maxrate", str(p.bitrate), "-bufsize", str(max(p.bitrate // 2, 500_000)),
            "-vsync", "0", "-flush_packets", "1", "-f", "flv", "pipe:1"]
    return cmd


def _pulse_input(source: str) -> list[str]:
    return ["-f", "pulse", "-fragment_size", "1920", "-i", source]


def pulse_audio_args(sources: Union[str, Sequence[str]], first_index: int = 0) -> tuple[list[str], list[str]]:
    """ffmpeg input (+ mix filter) args for one or more pulse sources, and the ``-map`` for the result.

    Two sources (system + mic) are mixed inside ffmpeg: each live input is first drift-compensated
    against its wallclock timestamps (``aresample=async``: the two sound cards have independent
    clocks), then ``amix`` sums them without the default 1/n attenuation. One output stream →
    one Opus stream whose timestamps come from its own sample count."""
    srcs = [sources] if isinstance(sources, str) else list(sources)
    if not srcs:
        raise ValueError("no audio source")
    args: list[str] = []
    for src in srcs:
        args += _pulse_input(src)
    if len(srcs) == 1:
        return args, ["-map", f"{first_index}:a"]
    chains = "".join(f"[{first_index + i}:a]aresample=async=1000[s{i}];" for i in range(len(srcs)))
    pads = "".join(f"[s{i}]" for i in range(len(srcs)))
    args += ["-filter_complex", f"{chains}{pads}amix=inputs={len(srcs)}:duration=longest:normalize=0[aout]"]
    return args, ["-map", "[aout]"]


def build_audio_cmd(source: Union[str, Sequence[str]], bitrate: int = 128_000) -> list[str]:
    # Opus 10 ms, lowdelay; one Ogg page per packet so the muxer doesn't buffer (~1 s otherwise)
    srcs = [source] if isinstance(source, str) else list(source)
    if len(srcs) == 1:
        inputs = _pulse_input(srcs[0])
    else:
        args, maps = pulse_audio_args(srcs)
        inputs = args + maps
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", *inputs,
            "-ac", "2", "-ar", "48000", "-c:a", "libopus", "-b:a", str(bitrate),
            "-application", "lowdelay", "-frame_duration", "10", "-page_duration", "10000",
            "-flush_packets", "1", "-f", "ogg", "pipe:1"]


async def default_audio_monitor() -> Optional[str]:
    """``<default sink>.monitor`` via pactl (PulseAudio or pipewire-pulse)."""
    if not shutil.which("pactl"):
        return None

    async def run(*a: str) -> str:
        p = await asyncio.create_subprocess_exec("pactl", *a, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 3)
        return out.decode("utf-8", "replace") if p.returncode == 0 else ""

    try:
        sink = (await run("get-default-sink")).strip()
        if not sink:
            for line in (await run("info")).splitlines():
                if line.startswith(("Default Sink:", "Domyślny odpływ:")):
                    sink = line.split(":", 1)[1].strip()
        return f"{sink}.monitor" if sink else None
    except (OSError, asyncio.TimeoutError):
        return None


# --------------------------------------------------------------------------------------
# Process wrapper
# --------------------------------------------------------------------------------------
class _FfmpegProc:
    def __init__(self, cmd: list[str], name: str):
        self.cmd = cmd
        self.name = name
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.stderr_tail: Deque[str] = collections.deque(maxlen=20)
        self._err_task: Optional[asyncio.Task] = None
        self.started = 0.0

    async def start(self) -> None:
        log.info("%s: %s", self.name, " ".join(self.cmd))
        self.started = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=dict(os.environ, DISPLAY=display()), limit=1 << 20)
        self._err_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        with contextlib.suppress(Exception):
            async for line in self.proc.stderr:
                s = line.decode("utf-8", "replace").rstrip()
                if s:
                    self.stderr_tail.append(s)
                    log.debug("%s: %s", self.name, s)

    async def read(self, n: int = 1 << 16) -> bytes:
        assert self.proc and self.proc.stdout
        return await self.proc.stdout.read(n)

    async def stop(self, timeout: float = 2.0) -> None:
        p = self.proc
        if p is None:
            return
        if p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                p.terminate()
            try:
                await asyncio.wait_for(p.wait(), timeout)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    p.kill()
                with contextlib.suppress(Exception):
                    await p.wait()
        if self._err_task:
            self._err_task.cancel()
            with contextlib.suppress(BaseException):
                await self._err_task


class _FailureBudget:
    def __init__(self) -> None:
        self.times: Deque[float] = collections.deque()

    def hit(self) -> bool:
        """Record a failure; True when the budget is exhausted."""
        now = time.monotonic()
        self.times.append(now)
        while self.times and now - self.times[0] > FAIL_WINDOW_S:
            self.times.popleft()
        return len(self.times) >= MAX_FAILS


# --------------------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------------------
@dataclass
class EncodedFrame:
    data: bytes
    key: bool
    cap: float           # estimated capture time (time.monotonic seconds)
    arrival: float       # when the frame left the encoder


class VideoCapture:
    """Restartable x11grab → encoder pipeline."""

    def __init__(self, params: VideoParams, on_frame: Callable[[EncodedFrame], None],
                 on_fatal: Callable[[str], None], *, disp: Optional[str] = None):
        self.params = params
        self.on_frame = on_frame
        self.on_fatal = on_fatal
        self.disp = disp
        self._active: Optional[_FfmpegProc] = None
        self._tasks: dict[_FfmpegProc, asyncio.Task] = {}
        self._promoted: dict[_FfmpegProc, asyncio.Event] = {}
        self._stopping = False
        self._stop_evt = asyncio.Event()
        self._fails = _FailureBudget()
        self._restart_lock = asyncio.Lock()
        self._ts_window: Deque[float] = collections.deque(maxlen=240)
        self._lat_window: Deque[float] = collections.deque(maxlen=120)
        self.restarts = 0
        self.last_restart_ms: Optional[float] = None

    @property
    def encoder_label(self) -> str:
        p = self.params
        if p.encoder == "h264_nvenc":
            return "h264_nvenc" + (" + scale_cuda" if p.gpu_scale else " + CPU scale")
        return p.encoder

    @property
    def measured_fps(self) -> float:
        ts = self._ts_window
        if len(ts) < 2:
            return 0.0
        now = time.monotonic()
        recent = [t for t in ts if now - t <= 1.0]
        if len(recent) < 2:
            return 0.0
        return (len(recent) - 1) / max(1e-3, recent[-1] - recent[0])

    @property
    def encode_latency_ms(self) -> Optional[float]:
        if not self._lat_window:
            return None
        v = sorted(self._lat_window)
        return v[len(v) // 2]

    async def start(self) -> None:
        self._stopping = False
        self._stop_evt.clear()
        proc = await self._spawn()
        if proc is None:
            return
        self._active = proc
        self._promoted[proc].set()

    async def _spawn(self) -> Optional[_FfmpegProc]:
        """Start an encoder process; None (and nothing left running) if stop() raced with it."""
        if self._stopping:
            return None
        proc = _FfmpegProc(build_video_cmd(self.params, self.disp), "ffmpeg-video")
        try:
            await proc.start()
        except BaseException:
            await asyncio.shield(proc.stop())
            raise
        if self._stopping:
            await proc.stop()
            return None
        self._promoted[proc] = asyncio.Event()
        self._tasks[proc] = asyncio.create_task(self._reader(proc), name="video-reader")
        return proc

    async def restart(self, **changes: object) -> bool:
        """Restart the encoder (forces an IDR), optionally with changed params.

        Returns True when the new process took over."""
        async with self._restart_lock:
            if self._stopping:
                return False
            t0 = time.monotonic()
            for k, v in changes.items():
                setattr(self.params, k, v)
            new = await self._spawn()
            if new is None:
                return False
            promoted = asyncio.ensure_future(self._promoted[new].wait())
            stopped = asyncio.ensure_future(self._stop_evt.wait())
            try:
                await asyncio.wait({promoted, stopped}, timeout=RESTART_TIMEOUT_S,
                                   return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                await asyncio.shield(self._kill(new))
                raise
            finally:
                promoted.cancel()
                stopped.cancel()
            if self._stopping or not self._promoted.get(new, asyncio.Event()).is_set():
                if not self._stopping:
                    log.warning("video restart: new encoder produced no frame in %.1fs", RESTART_TIMEOUT_S)
                await self._kill(new)
                return False
            self.restarts += 1
            self.last_restart_ms = (time.monotonic() - t0) * 1000
            log.info("video encoder restarted in %.0f ms", self.last_restart_ms)
            return True

    async def _kill(self, proc: _FfmpegProc) -> None:
        task = self._tasks.pop(proc, None)
        self._promoted.pop(proc, None)
        await proc.stop()
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def _reader(self, proc: _FfmpegProc) -> None:
        parser = FlvParser()
        offset: Optional[float] = None
        try:
            while True:
                chunk = await proc.read()
                if not chunk:
                    break
                for fr in parser.feed(chunk):
                    now = time.monotonic()
                    # capture time ≈ ts mapped with the minimal observed (arrival - ts) offset
                    off = now - fr.ts_ms / 1000.0
                    offset = off if offset is None else min(offset + OFFSET_DECAY_S, off)
                    cap = offset + fr.ts_ms / 1000.0 - ENCODE_FLOOR_S
                    if self._active is not proc:
                        if self._promoted.get(proc) is None or not fr.key:
                            continue
                        old, self._active = self._active, proc
                        self._promoted[proc].set()
                        if old is not None:
                            asyncio.create_task(self._kill(old))
                    self._ts_window.append(now)
                    self._lat_window.append((now - cap) * 1000)
                    try:
                        self.on_frame(EncodedFrame(fr.data, fr.key, cap, now))
                    except Exception:  # noqa: BLE001
                        log.exception("on_frame failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("video reader failed")
        # process ended
        if proc.proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.proc.wait(), 2)
        if self._stopping or self._active is not proc:
            return
        rc = proc.proc.returncode if proc.proc else None
        detail = "; ".join(list(proc.stderr_tail)[-3:])
        log.warning("video encoder exited unexpectedly (rc=%s): %s", rc, detail)
        self._tasks.pop(proc, None)
        self._active = None
        if self._fails.hit():
            self.on_fatal(f"ffmpeg (wideo) kończy się błędem: {detail or rc}")
            return
        await asyncio.sleep(0.3)
        try:
            newp = await self._spawn()
        except OSError as e:
            self.on_fatal(f"Nie można uruchomić ffmpeg: {e}")
            return
        if newp is not None:
            self._active = newp
            self._promoted[newp].set()

    @property
    def running_processes(self) -> int:
        return sum(1 for p in self._tasks if p.proc is not None and p.proc.returncode is None)

    async def stop(self) -> None:
        self._stopping = True
        self._stop_evt.set()
        self._active = None
        while self._tasks:  # loop: a racing _spawn may register a process meanwhile
            procs = list(self._tasks)
            await asyncio.gather(*(self._kill(p) for p in procs), return_exceptions=True)


# --------------------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------------------
class AudioCapture:
    """Pulse source(s) → Opus packets with capture timestamps derived from the sample count."""

    def __init__(self, source: Union[str, Sequence[str]], on_packet: Callable[[bytes, float], None],
                 on_fatal: Callable[[str], None], *, bitrate: int = 128_000):
        self.source = source
        self.bitrate = bitrate
        self.on_packet = on_packet
        self.on_fatal = on_fatal
        self._proc: Optional[_FfmpegProc] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._fails = _FailureBudget()
        self._last_cap: Optional[float] = None
        self.reanchors = 0

    async def start(self) -> None:
        self._stopping = False
        await self._launch()

    async def _launch(self) -> None:
        if self._stopping:
            return
        proc = _FfmpegProc(build_audio_cmd(self.source, self.bitrate), "ffmpeg-audio")
        try:
            await proc.start()
        except BaseException:
            await asyncio.shield(proc.stop())
            raise
        self._proc = proc
        if self._stopping:
            await proc.stop()
            return
        self._task = asyncio.create_task(self._reader(proc), name="audio-reader")

    async def _reader(self, proc: _FfmpegProc) -> None:
        parser = OggOpusParser()
        anchor: Optional[float] = None
        samples = 0
        try:
            while True:
                chunk = await proc.read(8192)
                if not chunk:
                    break
                for pk in parser.feed(chunk):
                    now = time.monotonic()
                    cap = None if anchor is None else anchor + samples / 48000
                    # (re)anchor at start or when the sound-card clock drifts vs monotonic
                    if cap is None or cap > now or now - cap > AUDIO_LATENCY_S + AUDIO_REANCHOR_S:
                        if anchor is not None:
                            self.reanchors += 1
                        anchor = now - AUDIO_LATENCY_S - samples / 48000
                        cap = anchor + samples / 48000
                    assert anchor is not None
                    if self._last_cap is not None and cap <= self._last_cap:  # RTP time must grow
                        anchor +=self._last_cap + 0.0005 - cap
                        cap = self._last_cap + 0.0005
                    self._last_cap = cap
                    samples += opus_samples(pk)
                    try:
                        self.on_packet(pk, cap)
                    except Exception:  # noqa: BLE001
                        log.exception("on_packet failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("audio reader failed")
        if self._stopping:
            return
        detail = "; ".join(list(proc.stderr_tail)[-3:])
        log.warning("audio encoder exited unexpectedly: %s", detail)
        if self._fails.hit():
            self.on_fatal(f"ffmpeg (dźwięk) kończy się błędem: {detail}")
            return
        await asyncio.sleep(0.5)
        await proc.stop()
        await self._launch()

    async def stop(self) -> None:
        self._stopping = True
        if self._proc:
            await self._proc.stop()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
