"""MirrorSession against an in-process fake Cast receiver (TCP control + UDP media), no ffmpeg."""
import asyncio
import struct
import time

import pytest

from airlinx.core import models
from airlinx.core.cast import castv2 as cv
from airlinx.core.cast import mirror as mm
from airlinx.core.cast import rtp as cr
from airlinx.core.capture import EncodedFrame

from test_cast_castv2 import FakeReceiver, server_ctx


@pytest.fixture(autouse=True)
def isolated_pins(tmp_path, monkeypatch):
    path = tmp_path / "known_tvs.json"
    monkeypatch.setenv("AIRLINX_KNOWN_TVS", str(path))
    return path


# ---------------------------------------------------------------------------------- pure helpers
def test_build_offer_and_answer_mapping():
    v, a = mm.StreamKeys(11, bytes(16), bytes([1] * 16)), mm.StreamKeys(22, bytes([2] * 16), bytes([3] * 16))
    o = mm.build_offer(video=v, audio=a, width=1920, height=1080, fps=60, bitrate=12_000_000, target_delay_ms=100)
    vs, au = o["offer"]["supportedStreams"]
    assert vs["index"] == 0 and vs["ssrc"] == 11 and vs["maxFrameRate"] == "60000/1000"
    assert vs["resolutions"] == [{"width": 1920, "height": 1080}] and vs["aesIvMask"] == "01" * 16
    assert au["index"] == 1 and au["codecName"] == "opus" and au["timeBase"] == "1/48000"
    # TV may list indexes in any order: mapping must be pairwise by position
    ans = {"type": "ANSWER", "result": "ok", "seqNum": 4,
           "answer": {"udpPort": 5000, "sendIndexes": [1, 0], "ssrcs": [23, 12]}}
    info = mm.parse_answer(ans)
    assert info.udp_port == 5000 and info.receiver_ssrcs == {0: 12, 1: 23}
    with pytest.raises(cv.CastError):
        mm.parse_answer({"type": "ANSWER", "result": "ok", "answer": {"udpPort": 1, "sendIndexes": [1], "ssrcs": [2]}})
    with pytest.raises(cv.CastError):
        mm.parse_answer({"type": "ANSWER", "result": "error", "error": {"description": "x"}})
    with pytest.raises(cv.CastError):
        mm.parse_answer({"type": "ANSWER", "result": "ok", "answer": {"udpPort": 1, "sendIndexes": [0, 1],
                                                                       "ssrcs": [2]}})


def test_presets():
    g = mm.effective_options(models.MirrorOptions(preset="game"))
    c = mm.effective_options(models.MirrorOptions(preset="cinema", bitrate=12_000_000))
    assert g.target_delay_ms == 100 and c.target_delay_ms == 400 and c.bitrate == 18_000_000
    assert mm.is_structural_change(g, mm.effective_options(models.MirrorOptions(audio=False)))
    assert not mm.is_structural_change(g, mm.effective_options(models.MirrorOptions(bitrate=5_000_000)))


# ---------------------------------------------------------------------------------- fake TV
class FakeTv(FakeReceiver):
    def __init__(self, apps=None, ssl_ctx=None):
        super().__init__(apps, ssl_ctx)
        self.udp = None
        self.udp_port = None
        self.offer = None
        self.video_frames = set()
        self.key_frames = 0
        self.audio_packets = 0
        self.sr_seen = set()
        self.send_pli = False
        self.mute_feedback = False
        self.hold_launch = False
        self.answer_error = False
        self.answer_delay = 0.0

    async def start(self):
        port = await super().start()
        loop = asyncio.get_running_loop()
        tv = self

        class P(asyncio.DatagramProtocol):
            def connection_made(self, t):
                tv.udp = t

            def datagram_received(self, data, addr):
                tv.on_udp(data, addr)
        await loop.create_datagram_endpoint(P, local_addr=("127.0.0.1", 0))
        self.udp_port = self.udp.get_extra_info("sockname")[1]
        return port

    async def close(self):
        self.udp.close()
        await super().close()

    def on_udp(self, data, addr):
        if 200 <= data[1] <= 207:
            self.sr_seen.add(struct.unpack_from(">I", data, 4)[0])
            return
        pt = data[1] & 0x7F
        ssrc = struct.unpack_from(">I", data, 8)[0]
        fid8 = data[13]
        streams = {s["ssrc"]: s for s in self.offer["offer"]["supportedStreams"]}
        if ssrc not in self.sr_seen:
            return  # like the real receiver: no SR yet -> ignore
        rssrc = ssrc + 1
        if pt == cr.VIDEO_PT:
            if data[12] & 0x80 and struct.unpack_from(">H", data, 14)[0] == 0:
                self.key_frames += 1
            self.video_frames.add(fid8)
        else:
            self.audio_packets += 1
        last_pkt = struct.unpack_from(">H", data, 14)[0] == struct.unpack_from(">H", data, 16)[0]
        if last_pkt and not self.mute_feedback:
            fb = struct.pack(">III", rssrc, ssrc, cr.CAST) + struct.pack(">BBH", fid8, 0,
                                                                        streams[ssrc]["targetDelay"])
            fb += struct.pack(">IBB", cr.CST2, 1, 0) + b"\x00\x00"
            pkt = struct.pack(">BBH", 0x80 | 15, 206, len(fb) // 4) + fb
            if self.send_pli and pt == cr.VIDEO_PT:
                self.send_pli = False
                pkt += struct.pack(">BBHII", 0x81, 206, 2, rssrc, ssrc)
            self.udp.sendto(pkt, addr)

    async def handle(self, w, m, p):
        if m.namespace == cv.NS_RECEIVER and p.get("type") == "LAUNCH" and self.hold_launch:
            return  # consent dialog on the TV, user hasn't answered
        if m.namespace == cv.NS_WEBRTC and p.get("type") == "OFFER":
            if self.answer_delay:
                await asyncio.sleep(self.answer_delay)
            if self.answer_error:
                self.send(w, cv.NS_WEBRTC, {"type": "ANSWER", "seqNum": p["seqNum"], "result": "error",
                                            "error": {"code": 1, "description": "boom"}}, src="T1")
                await w.drain()
                return
            self.offer = p
            streams = p["offer"]["supportedStreams"]
            idx = [s["index"] for s in streams][::-1]
            ssrcs = [s["ssrc"] + 1 for s in streams][::-1]
            self.send(w, cv.NS_WEBRTC, {"type": "ANSWER", "seqNum": p["seqNum"], "result": "ok",
                                        "answer": {"udpPort": self.udp_port, "sendIndexes": idx, "ssrcs": ssrcs,
                                                   "display": {"scaling": "sender"}}}, src="T1")
            await w.drain()
            return
        await super().handle(w, m, p)


class FakeVideo:
    instances = []

    def __init__(self, params, on_frame, on_fatal, **kw):
        self.params, self.on_frame = params, on_frame
        self.restarts = []
        self.task = None
        self.force_key = True
        self.encoder_label = "fake"
        self.encode_latency_ms = 5.0
        self.stopped = False
        FakeVideo.instances.append(self)

    async def start(self):
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        i = 0
        while True:
            now = time.monotonic()
            key = self.force_key or i % 60 == 0
            self.force_key = False
            self.on_frame(EncodedFrame(b"\x00\x00\x00\x01" + bytes(6000 if key else 1500), key, now - 0.005, now))
            i += 1
            await asyncio.sleep(1 / 60)

    async def restart(self, **changes):
        self.restarts.append(changes)
        for k, v in changes.items():
            setattr(self.params, k, v)
        self.force_key = True
        return True

    async def stop(self):
        self.stopped = True
        if self.task:
            self.task.cancel()


class FakeAudio:
    instances: list = []

    def __init__(self, source, on_packet, on_fatal, **kw):
        self.source = source
        self.on_packet = on_packet
        self.on_fatal = on_fatal
        self.task = None
        self.stopped = False
        FakeAudio.instances.append(self)

    async def start(self):
        async def run():
            while True:
                self.on_packet(b"\xfc" + bytes(100), time.monotonic() - 0.02)
                await asyncio.sleep(0.01)
        self.task = asyncio.create_task(run())

    async def stop(self):
        self.stopped = True
        if self.task:
            self.task.cancel()


class FakePipeline:
    instances: list = []
    SOURCES = {"system": ["fake.monitor"], "mic": ["@DEFAULT_SOURCE@"], "system_mic": ["fake.monitor", "mic"],
               "app": ["airlinx_app.monitor"]}

    def __init__(self, spec, **kw):
        self.spec = spec
        self.kw = kw
        self.opened = self.closed = False
        FakePipeline.instances.append(self)

    async def open(self):
        self.opened = True
        return list(self.SOURCES[self.spec.source])

    async def close(self):
        self.closed = True


@pytest.fixture
def patched(monkeypatch):
    orig_open = cv.CastChannel.open.__func__

    async def open_plain(cls, host, port=8009, **kw):
        kw["use_tls"] = False
        return await orig_open(cls, host, port, **kw)

    async def fake_caps():
        return {"nvenc": True, "cuda_scale": True, "x264": True}

    async def fake_screen():
        return (1920, 1080)

    async def fake_monitor():
        return "fake.monitor"

    monkeypatch.setattr(cv.CastChannel, "open", classmethod(open_plain))
    monkeypatch.setattr(mm, "VideoCapture", FakeVideo)
    monkeypatch.setattr(mm, "AudioCapture", FakeAudio)
    monkeypatch.setattr(mm, "probe_encoders", fake_caps)
    monkeypatch.setattr(mm, "screen_size", fake_screen)
    monkeypatch.setattr(mm, "default_audio_monitor", fake_monitor)
    monkeypatch.setattr(mm, "KEYFRAME_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(mm, "AudioPipeline", FakePipeline)
    FakeVideo.instances.clear()
    FakeAudio.instances.clear()
    FakePipeline.instances.clear()


def make_session(port, states, stats, opts=None):
    dev = models.Device(id="tv", name="TV", model="x", ip="127.0.0.1", cast_port=port, caps={"cast_mirror"})
    src = models.Source(id="monitor:X", kind="monitor", name="X", geometry=(0, 0, 1920, 1080))
    return mm.MirrorSession(dev, src, opts or models.MirrorOptions(),
                            on_stats=stats.append, on_state=lambda s, d: states.append((s, d)))


async def wait_until(pred, timeout=5.0):
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.02)


def test_busy_tv_guard(patched):
    async def main():
        tv = FakeTv(apps=[{"appId": "NETFLIX", "displayName": "Netflix", "sessionId": "N"}])
        port = await tv.start()
        states = []
        s = make_session(port, states, [])
        with pytest.raises(models.TvBusyError) as ei:
            await s.start()
        assert "Netflix" in ei.value.message_pl
        assert not any(p.get("type") == "LAUNCH" for _, _, p in tv.received)
        assert states[-1][0] == "stopped"
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_full_session_live_stats_pli_stop(patched):
    async def main():
        tv = FakeTv()
        port = await tv.start()
        states, stats = [], []
        s = make_session(port, states, stats)
        await s.start()
        await wait_until(lambda: s.state == "live")
        assert [x for x, _ in states][:3] == ["connecting", "waiting_tv", "live"]
        streams = tv.offer["offer"]["supportedStreams"]
        assert [x["type"] for x in streams] == ["video_source", "audio_source"]
        await wait_until(lambda: len(stats) >= 3)
        st = stats[-1]
        assert st.width == 1920 and st.height == 1080 and st.protocol == "cast"
        assert st.fps > 30 and st.bitrate > 0 and st.latency_ms is not None
        assert set(st.stages) == {"capture", "encode", "network", "tv"}
        assert abs(sum(st.stages.values()) - st.latency_ms) < 0.5
        assert tv.audio_packets > 50 and len(tv.video_frames) > 30
        # PLI -> encoder restart (IDR)
        tv.send_pli = True
        await wait_until(lambda: FakeVideo.instances[-1].restarts)
        # bitrate update -> restart with new bitrate, no renegotiation
        offers_before = sum(1 for _, _, p in tv.received if p.get("type") == "OFFER")
        await s.update(models.MirrorOptions(bitrate=6_000_000))
        assert FakeVideo.instances[-1].restarts[-1]["bitrate"] == 6_000_000
        assert sum(1 for _, _, p in tv.received if p.get("type") == "OFFER") == offers_before
        # structural update -> new OFFER without audio
        await s.update(models.MirrorOptions(bitrate=6_000_000, audio=False))
        assert len(tv.offer["offer"]["supportedStreams"]) == 1
        await s.stop()
        assert states[-1][0] == "stopped"
        stops = [p for ns, _, p in tv.received if p.get("type") == "STOP"]
        assert stops and stops[-1]["sessionId"] == "S1"
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 20))


def test_app_closed_on_tv_ends_session(patched):
    async def main():
        tv = FakeTv()
        port = await tv.start()
        states = []
        s = make_session(port, states, [])
        await s.start()
        await wait_until(lambda: s.state == "live")
        tv.apps = []
        w = tv.writers[0]
        tv.send(w, cv.NS_RECEIVER, tv.status())
        await w.drain()
        await wait_until(lambda: s.state == "stopped")
        assert "telewizorze" in (states[-1][1] or "")
        await s.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_feedback_stall_triggers_reconnect(patched, monkeypatch):
    monkeypatch.setattr(mm, "STALL_TIMEOUT_LIVE", 0.6)
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.05, 0.05))

    async def main():
        tv = FakeTv()
        port = await tv.start()
        states = []
        s = make_session(port, states, [])
        await s.start()
        await wait_until(lambda: s.state == "live")
        tv.mute_feedback = True
        await wait_until(lambda: any(x == "reconnecting" for x, _ in states))
        tv.mute_feedback = False
        await wait_until(lambda: s.state == "live" and states[-2][0] != "live", timeout=8)
        offers = [p for _, _, p in tv.received if p.get("type") == "OFFER"]
        assert len(offers) >= 2
        launches = [p for _, _, p in tv.received if p.get("type") == "LAUNCH"]
        assert len(launches) == 1  # re-attached to the running session, no new LAUNCH
        await s.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 20))


def test_cert_change_blocks_session_before_offer(tmp_path, isolated_pins, monkeypatch):
    """Real TLS path: pinned fingerprint differs -> tv_cert_changed, no LAUNCH/OFFER with keys."""
    monkeypatch.setattr(mm, "probe_encoders", lambda: asyncio.sleep(0, {"nvenc": True}))

    async def main():
        ctx, _ = server_ctx(tmp_path, "impostor")
        tv = FakeTv(ssl_ctx=ctx)
        port = await tv.start()
        cv.check_cert_pin("tv", "00" * 32)  # previously paired TV had another certificate
        states = []
        s = make_session(port, states, [])
        with pytest.raises(models.AirlinxError) as ei:
            await s.start()
        assert ei.value.code == "tv_cert_changed"
        assert states[-1][0] == "error"
        await asyncio.sleep(0.05)
        assert not any(p.get("type") in ("LAUNCH", "OFFER") for _, _, p in tv.received)
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def count(tv, typ):
    return sum(1 for _, _, p in tv.received if p.get("type") == typ)


def test_reconnect_when_session_gone_does_not_relaunch(patched, monkeypatch):
    monkeypatch.setattr(mm, "STALL_TIMEOUT_LIVE", 0.6)
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.05, 0.05))

    async def main():
        tv = FakeTv()
        port = await tv.start()
        states = []
        s = make_session(port, states, [])
        await s.start()
        await wait_until(lambda: s.state == "live")
        tv.apps = []              # e.g. TV went to standby / app closed, no status pushed
        tv.mute_feedback = True
        await wait_until(lambda: s.state == "stopped", timeout=8)
        assert states[-1] == ("stopped", mm.APP_GONE_PL)
        assert count(tv, "LAUNCH") == 1
        await s.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 20))


def test_cancelled_start_stops_late_launched_app(patched, monkeypatch):
    monkeypatch.setattr(mm, "LAUNCH_GUARD_POLL", 0.05)

    async def main():
        tv = FakeTv()
        tv.hold_launch = True
        port = await tv.start()
        states = []
        s = make_session(port, states, [])
        task = asyncio.create_task(s.start())
        await wait_until(lambda: count(tv, "LAUNCH") == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s.state == "stopped"
        # the user accepts on the TV only now: the guard must STOP that session
        tv.apps = [{"appId": cv.MIRRORING_APP_ID, "displayName": "Chrome Mirroring", "sessionId": "LATE",
                    "transportId": "TL"}]
        await wait_until(lambda: any(p.get("type") == "STOP" and p.get("sessionId") == "LATE"
                                     for _, _, p in tv.received))
        await s.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_guard_of_cancelled_session_does_not_stop_next_session(patched, monkeypatch):
    """The controller creates a new MirrorSession per start: A's late-launch guard must not STOP B."""
    monkeypatch.setattr(mm, "LAUNCH_GUARD_POLL", 0.05)

    async def main():
        tv = FakeTv()
        tv.hold_launch = True
        port = await tv.start()
        a = make_session(port, [], [])
        task = asyncio.create_task(a.start())
        await wait_until(lambda: count(tv, "LAUNCH") == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        tv.hold_launch = False
        b = make_session(port, [], [])       # same device, new object (as the controller does)
        await b.start()
        await wait_until(lambda: b.state == "live")
        await asyncio.sleep(0.5)             # ~10 guard polls
        assert not any(p.get("type") == "STOP" and p.get("sessionId") == "S1" for _, _, p in tv.received)
        assert b.state == "live"
        await b.stop()
        await a.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_closed_shared_window_ends_session_with_error(patched, monkeypatch):
    """x11grab keeps grabbing the same rectangle: once the window is gone, the TV would show what lies under it."""
    monkeypatch.setattr(mm, "WINDOW_POLL", 0.05)
    geoms = [(10, 10, 640, 480), None, (10, 10, 640, 480), None, None]   # one None = transient, two = gone

    async def fake_geometry(xid):
        return geoms.pop(0) if geoms else None

    monkeypatch.setattr(mm, "window_geometry", fake_geometry)

    async def main():
        tv = FakeTv()
        port = await tv.start()
        states = []
        dev = models.Device(id="tv", name="TV", model="x", ip="127.0.0.1", cast_port=port, caps={"cast_mirror"})
        src = models.Source(id="window:0x123", kind="window", name="Okno", geometry=(10, 10, 640, 480), xid=0x123)
        s = mm.MirrorSession(dev, src, models.MirrorOptions(), on_stats=lambda st: None,
                             on_state=lambda st, d: states.append((st, d)))
        await s.start()
        await wait_until(lambda: s.state == "error")
        assert states[-1] == ("error", "Udostępniane okno zostało zamknięte lub zminimalizowane.")
        assert geoms == []                       # the single transient None did not end the session
        assert any(p.get("type") == "STOP" and p.get("sessionId") == "S1" for _, _, p in tv.received)
        assert FakeVideo.instances[-1].stopped
        await s.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_window_gone_during_reconnect_still_ends_session(patched, monkeypatch):
    """The follower's "fatal" lands behind a "stall" in the queue; the supervisor reconnects (and used to
    drop the queue). The session must still end in error, without starting a new capture."""
    monkeypatch.setattr(mm, "WINDOW_POLL", 0.05)
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.05, 0.05))
    holder = {}
    calls = {"n": 0}

    async def fake_geometry(xid):
        calls["n"] += 1
        if calls["n"] <= 2:
            return (10, 10, 640, 480)
        if calls["n"] == 4:   # 2nd miss: the supervisor gets a stall first, the follower's fatal right after it
            holder["s"]._events.put_nowait(("stall", "test stall"))
        return None

    monkeypatch.setattr(mm, "window_geometry", fake_geometry)

    async def main():
        tv = FakeTv()
        port = await tv.start()
        states = []
        dev = models.Device(id="tv", name="TV", model="x", ip="127.0.0.1", cast_port=port, caps={"cast_mirror"})
        src = models.Source(id="window:0x123", kind="window", name="Okno", geometry=(10, 10, 640, 480), xid=0x123)
        s = mm.MirrorSession(dev, src, models.MirrorOptions(), on_stats=lambda st: None,
                             on_state=lambda st, d: states.append((st, d)))
        holder["s"] = s
        await s.start()
        captures = len(FakeVideo.instances)
        await wait_until(lambda: s.state == "error", timeout=3)
        assert states[-1] == ("error", mm.WINDOW_GONE_PL)
        assert len(FakeVideo.instances) == captures       # no capture of whatever lies under the window
        assert any(p.get("type") == "STOP" for _, _, p in tv.received)
        await s.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_failed_start_after_launch_stops_app(patched):
    async def main():
        tv = FakeTv()
        tv.answer_error = True
        port = await tv.start()
        states = []
        s = make_session(port, states, [])
        with pytest.raises(models.AirlinxError):
            await s.start()
        assert states[-1][0] == "error"
        assert any(p.get("type") == "STOP" and p.get("sessionId") == "S1" for _, _, p in tv.received)
        assert tv.apps == []
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_stop_during_renegotiation_leaves_no_capture(patched):
    async def main():
        tv = FakeTv()
        port = await tv.start()
        states = []
        s = make_session(port, states, [])
        await s.start()
        await wait_until(lambda: s.state == "live")
        tv.answer_delay = 0.5
        upd = asyncio.create_task(s.update(models.MirrorOptions(audio=False)))
        await asyncio.sleep(0.1)
        await s.stop()           # waits for the in-flight update (op lock), which aborts
        await upd
        assert s._media is None and all(v.stopped for v in FakeVideo.instances)
        assert states[-1][0] == "stopped"
        await asyncio.sleep(0.7)  # the delayed ANSWER arrives after stop: nothing restarts
        assert s._media is None and all(v.stopped for v in FakeVideo.instances)
        assert any(p.get("type") == "STOP" for _, _, p in tv.received)
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_supervisor_waits_for_inflight_update_before_touching_media(patched, monkeypatch):
    """A supervisor event (reconnect) must not tear media down/up while update()/switch_source() holds the op lock."""
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.05,))

    async def main():
        tv = FakeTv()
        port = await tv.start()
        s = make_session(port, [], [])
        await s.start()
        await wait_until(lambda: s.state == "live")
        first = FakeVideo.instances[-1]
        async with s._op_lock:                       # an update() is in flight
            s._events.put_nowait(("stall", "test"))
            await asyncio.sleep(0.4)
            assert not first.stopped, "supervisor stopped media while an update held the op lock"
        await wait_until(lambda: first.stopped)
        await wait_until(lambda: s.state == "live" and len(FakeVideo.instances) >= 2)
        await s.stop()
        assert all(v.stopped for v in FakeVideo.instances)
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 20))


def test_stop_during_supervisor_reconnect_leaves_no_capture(patched, monkeypatch):
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.3,))

    async def main():
        tv = FakeTv()
        port = await tv.start()
        s = make_session(port, [], [])
        await s.start()
        await wait_until(lambda: s.state == "live")
        s._events.put_nowait(("stall", "test"))
        await wait_until(lambda: s.state == "reconnecting")
        await s.stop()
        await asyncio.sleep(0.5)
        assert s._media is None and all(v.stopped for v in FakeVideo.instances)
        assert all(a.stopped for a in FakeAudio.instances)
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 20))


def _app_opts(**kw):
    return models.MirrorOptions(audio_source="app", audio_app="firefox", audio_app_name="Firefox", **kw)


def test_app_audio_source_routes_and_unroutes_with_the_session(patched):
    async def main():
        tv = FakeTv()
        port = await tv.start()
        s = make_session(port, [], [], opts=_app_opts())
        await s.start()
        await wait_until(lambda: s.state == "live")
        pipe = FakePipeline.instances[-1]
        assert pipe.spec.source == "app" and pipe.spec.app == "firefox" and pipe.opened
        assert FakeAudio.instances[-1].source == ["airlinx_app.monitor"]
        await wait_until(lambda: tv.audio_packets > 10)
        await s.stop()
        assert pipe.closed and FakeAudio.instances[-1].stopped
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_live_audio_source_switch_restarts_only_audio(patched):
    async def main():
        tv = FakeTv()
        port = await tv.start()
        s = make_session(port, [], [])
        await s.start()
        await wait_until(lambda: s.state == "live")
        assert FakePipeline.instances[-1].spec.source == "system"
        offers = sum(1 for _, _, p in tv.received if p.get("type") == "OFFER")
        videos = len(FakeVideo.instances)
        assert s.live_audio_switch is True
        await s.update(_app_opts())
        old, new = FakePipeline.instances[-2], FakePipeline.instances[-1]
        assert old.closed and new.opened and new.spec.source == "app" and not new.closed
        assert FakeAudio.instances[-2].stopped and FakeAudio.instances[-1].source == ["airlinx_app.monitor"]
        assert sum(1 for _, _, p in tv.received if p.get("type") == "OFFER") == offers   # no renegotiation
        assert len(FakeVideo.instances) == videos and not FakeVideo.instances[-1].restarts
        n = tv.audio_packets
        await wait_until(lambda: tv.audio_packets > n + 10)                          # audio keeps flowing
        # same source again → nothing restarts
        await s.update(_app_opts(bitrate=12_000_000))
        assert len(FakePipeline.instances) == 2
        # mic + system mix
        await s.update(models.MirrorOptions(audio_source="system_mic", audio_mic="usb-mic"))
        assert FakePipeline.instances[-2].closed and FakeAudio.instances[-1].source == ["fake.monitor", "mic"]
        await s.stop()
        assert all(p.closed for p in FakePipeline.instances)
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 20))


def test_audio_fatal_releases_app_routing(patched):
    async def main():
        tv = FakeTv()
        port = await tv.start()
        notices = []
        s = make_session(port, [], [], opts=_app_opts())
        s.on_notice = notices.append
        await s.start()
        await wait_until(lambda: s.state == "live")
        pipe = FakePipeline.instances[-1]
        FakeAudio.instances[-1].on_fatal("ffmpeg (dźwięk) kończy się błędem")
        await wait_until(lambda: pipe.closed)
        assert s.state == "live" and notices and "dźwięk" in notices[-1].lower()
        await s.stop()
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_failed_start_releases_app_routing(patched):
    async def main():
        tv = FakeTv()
        tv.answer_error = True
        port = await tv.start()
        s = make_session(port, [], [], opts=_app_opts())
        with pytest.raises(models.AirlinxError):
            await s.start()
        assert all(p.closed for p in FakePipeline.instances if p.opened)
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 15))


def test_reconnect_reopens_and_session_end_closes_pipeline(patched, monkeypatch):
    monkeypatch.setattr(mm, "STALL_TIMEOUT_LIVE", 0.6)
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.05, 0.05))

    async def main():
        tv = FakeTv()
        port = await tv.start()
        states = []
        s = make_session(port, states, [], opts=_app_opts())
        await s.start()
        await wait_until(lambda: s.state == "live")
        tv.mute_feedback = True
        await wait_until(lambda: any(x == "reconnecting" for x, _ in states))
        tv.mute_feedback = False
        await wait_until(lambda: len(FakePipeline.instances) >= 2 and FakePipeline.instances[-1].opened)
        assert FakePipeline.instances[0].closed
        tv.apps = []
        w = tv.writers[-1]
        tv.send(w, cv.NS_RECEIVER, tv.status())
        await w.drain()
        await wait_until(lambda: s.state == "stopped")
        assert all(p.closed for p in FakePipeline.instances)
        await tv.close()
    asyncio.run(asyncio.wait_for(main(), 20))
