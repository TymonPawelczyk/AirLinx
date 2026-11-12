"""Controller tests with fake core modules (offline, no TV)."""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from airlinx.controller import Controller, Factories, UiHooks, media_kind
from airlinx.core.models import (AirlinxError, AppState, Device, MirrorOptions, PlaybackState, Source, Stats,
                                 TvBusyError, to_json)
from airlinx.settings import Settings

@pytest.fixture(autouse=True)
def known_tvs(tmp_path, monkeypatch):
    """Never read the user's real ~/.config/airlinx/known_tvs.json (a pinned TV disables the DLNA fallback)."""
    path = tmp_path / "known_tvs.json"
    monkeypatch.setenv("AIRLINX_KNOWN_TVS", str(path))
    return path


def _pin(path, key="cast-salon"):
    import json

    path.write_text(json.dumps({key: {"auth": "device", "device_subject": "CN=Salon", "tls": {"sha256": "ab"}}}))


SALON = Device("cast-salon", "Salon", "Samsung S85F", "192.0.2.10", 8009, "http://192.0.2.10:9197/dmr",
               {"cast_mirror", "cast_media", "dlna"})
SYPIALNIA = Device("cast-syp", "Sypialnia", "Chromecast", "192.0.2.23", 8009, None, {"cast_mirror", "cast_media"})
DP4 = Source("monitor:DP-4", "monitor", "DP-4", (0, 420, 4096, 2304))
DP28 = Source("monitor:DP-2.8", "monitor", "DP-2.8", (4096, 0, 5120, 2880))


class FakeDiscovery:
    def __init__(self, on_change, devices=None):
        self.on_change = on_change
        self.devices: list[Device] = []
        self._initial = devices if devices is not None else [SALON, SYPIALNIA]
        self.stopped = False

    async def start(self):
        self.devices = list(self._initial)
        self.on_change(self.devices)

    async def stop(self):
        self.stopped = True

    async def rescan(self):
        self.on_change(self.devices)

    async def add_manual(self, ip):
        if ip == "10.0.0.99":
            return None
        d = Device(f"dlna-{ip}", f"TV {ip}", "DMR", ip, None, f"http://{ip}/dmr", {"dlna"})
        self.devices.append(d)
        return d


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, proto, device, source, opts, on_stats, on_state, *_a, busy=False, fail=False, mode=None):
        self.proto, self.device, self.source, self.opts = proto, device, source, opts
        self.on_stats, self.on_state = on_stats, on_state
        self.busy, self.fail = busy, fail
        self.started = self.stopped = False
        self.forced = None
        self.mode = mode
        self.cancelled = False
        FakeSession.instances.append(self)

    async def start(self, force: bool = False):
        self.forced = force
        if self.mode == "hang":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True  # real MirrorSession sends STOP here
                raise
        if self.mode == "error_then_fail":
            self.on_state("error", "handshake failed")
            raise RuntimeError("handshake failed")
        if self.mode == "denied":
            raise AirlinxError("tv_denied", "Telewizor odrzucił połączenie.")
        if self.mode == "cert":
            from airlinx.core.cast.castv2 import TvCertChangedError
            raise TvCertChangedError("cast-salon")
        if self.mode == "refused":
            from airlinx.core.models import TvNotFoundError
            raise TvNotFoundError("192.0.2.10:8009: [Errno 111] Connect call failed")
        if self.mode == "auth":
            from airlinx.core.cast.castv2 import TvAuthFailedError
            raise TvAuthFailedError("bad signature")
        if self.fail:
            raise RuntimeError("boom")
        if self.busy and not force:
            raise TvBusyError("YouTube")
        self.started = True
        self.on_state("connecting", None)
        self.on_state("live", None)

    async def stop(self):
        self.stopped = True
        self.on_state("stopped", None)

    async def update(self, opts):
        self.opts = opts

    async def switch_source(self, source):
        self.source = source


class FakePlayer:
    def __init__(self, proto, device, busy=False, fail=None):
        self.proto, self.device, self.busy, self.fail = proto, device, busy, fail
        self.on_state = lambda st: None
        self.loaded: list[tuple] = []
        self.calls: list[str] = []
        self.item = None

    async def load(self, item, url, mime, start=0, *, force=False, subtitles=None, poster_url=None,
                   active_subtitle=None):
        if self.fail is not None:
            raise self.fail
        if self.busy and not force:
            raise TvBusyError("Netflix")
        self.item = item
        self.loaded.append((item.id, url, mime, start, force))
        self.on_state(PlaybackState("loading", item_id=item.id))

    async def play(self):
        self.calls.append("play")
        self.on_state(PlaybackState("playing", position=0, duration=10, item_id=self.item.id))

    async def pause(self):
        self.calls.append("pause")

    async def stop(self):
        self.calls.append("stop")
        self.on_state(PlaybackState("idle"))

    async def seek(self, s):
        self.calls.append(f"seek:{s}")

    async def set_volume(self, v):
        self.calls.append(f"vol:{v}")


class FakeServer:
    def __init__(self):
        self.started = self.stopped = False
        self.published = []
        self.unpublished = []
        self.target_ips = []

    async def start(self, host="0.0.0.0", port=0):
        self.started = True

    def publish(self, item, target):
        self.published.append((item.id, target))
        return f"http://192.0.2.5:8000/m/{item.id}", "video/mp4"

    async def publish_async(self, item, target, start=0, *, target_ip=None):
        self.target_ips.append(target_ip)
        return self.publish(item, target)

    def poster_url(self, url):
        return None

    def subtitle_tracks(self, url, fmt="vtt"):
        return [{"id": 1, "url": url + ".vtt", "label": "film.pl", "language": "pl", "mime": "text/vtt"}]

    def unpublish(self, url):
        self.unpublished.append(url)

    async def stop(self):
        self.stopped = True


class FakeAudioService:
    """Stands in for core.audio (pactl): what is available is set by the test."""

    def __init__(self):
        self.mics = [{"name": "usb-yeti", "label": "Blue Yeti", "default": True}]
        self.apps = [{"key": "firefox", "name": "Firefox", "icon": "firefox", "icon_url": None, "streams": 1}]
        self.cleanups = 0
        self.resolved = []

    async def list(self):
        return {"mics": list(self.mics), "apps": list(self.apps),
                "default_mic": next((m["label"] for m in self.mics if m["default"]), None)}

    async def resolve(self, spec):
        from airlinx.core import audio as au
        self.resolved.append(spec)
        if spec.source == "app" and not any(a["key"] == spec.app for a in self.apps):
            return au.AudioSpec("system", app=spec.app, app_name=spec.app_name), \
                f"{spec.app_name or spec.app} nic teraz nie odtwarza. Wysyłam dźwięk systemu."
        if spec.source in ("mic", "system_mic") and spec.mic != "default" and \
                not any(m["name"] == spec.mic for m in self.mics):
            return au.AudioSpec("system", mic=spec.mic), "Mikrofon niedostępny. Wysyłam dźwięk systemu."
        return spec, None

    async def cleanup(self):
        self.cleanups += 1
        return 0


class Env:
    def __init__(self, tmp_path, *, devices=None, busy=False, cast_fail=False, player_busy=False):
        self.settings = Settings(tmp_path / "config.json")
        self.events: list[tuple[str, Any]] = []
        self.players: list[FakePlayer] = []
        self.server = FakeServer()
        self.busy = busy
        self.cast_fail = cast_fail
        self.player_busy = player_busy
        self.modes: dict[str, str] = {}
        self.player_fail: dict[str, BaseException] = {}
        self.caps: Any = {"nvenc": True, "cuda_scale": True, "x264": True}   # probe_encoders() result
        FakeSession.instances = []

        async def encoders():
            if isinstance(self.caps, BaseException):
                raise self.caps
            return dict(self.caps)

        def mirror(proto, dev, src, opts, on_stats, on_state, *_a):
            return FakeSession(proto, dev, src, opts, on_stats, on_state, busy=self.busy and proto == "cast",
                               fail=self.cast_fail and proto == "cast", mode=self.modes.get(proto))

        def player(proto, dev):
            p = FakePlayer(proto, dev, busy=self.player_busy, fail=self.player_fail.get(proto))
            self.players.append(p)
            return p

        async def list_sources(with_thumbnails=True):
            return [DP4, DP28]

        self.picked = ["/nonexistent.mp4"]

        async def pick():
            return self.picked

        self.audio = FakeAudioService()
        self.ctl = Controller(self.settings, Factories(
            discovery=lambda cb: FakeDiscovery(cb, devices), list_sources=list_sources, mirror=mirror,
            player=player, media_server=lambda: self.server, audio=self.audio, encoders=encoders),
            UiHooks(pick_files=pick))
        self.ctl.subscribe(lambda e, p: self.events.append((e, p)))

    def names(self):
        return [e for e, _ in self.events]

    def last(self, name):
        return next(p for e, p in reversed(self.events) if e == name)


def run(coro):
    return asyncio.run(coro)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_media_kind():
    assert media_kind("/a/b.MKV") == "video"
    assert media_kind("/a/b.flac") == "audio"
    assert media_kind("http://x/y.jpg?z=1") == "image"
    assert media_kind("/a/b.txt") is None


@pytest.mark.parametrize("caps, expected", [
    ({"nvenc": True, "cuda_scale": True, "x264": True}, "h264_nvenc"),
    ({"nvenc": False, "cuda_scale": False, "x264": True}, "libx264"),
    ({"nvenc": False, "cuda_scale": False, "x264": False}, ""),
])
def test_state_reports_detected_encoder(tmp_path, caps, expected):
    env = Env(tmp_path)
    env.caps = caps

    async def go():
        assert env.ctl.state().encoder is None            # not probed yet: the UI says it is checking
        await env.ctl.start()
        await settle()
        return env.ctl.state().encoder, env.last("state").encoder

    assert run(go()) == (expected, expected)             # and the UI is told without asking


def test_encoder_probe_failure_leaves_encoder_unknown(tmp_path):
    env = Env(tmp_path)
    env.caps = RuntimeError("ffmpeg exploded")

    async def go():
        await env.ctl.start()
        await settle()
        return env.ctl.state().encoder

    assert run(go()) is None


@pytest.mark.parametrize("session, expected", [("wayland", True), ("x11", False)])
def test_state_flags_wayland_session(tmp_path, monkeypatch, session, expected):
    monkeypatch.setenv("XDG_SESSION_TYPE", session)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    env = Env(tmp_path)
    assert env.ctl.state().wayland is expected
    assert to_json(env.ctl.state())["wayland"] is expected


def test_start_autoselects_last_device(tmp_path):
    env = Env(tmp_path)
    env.settings.set("last_device_id", "cast-syp")
    env.ctl.selected_device_id = "cast-syp"

    async def go():
        await env.ctl.start()
        return env.ctl.state()

    st = run(go())
    assert st.selected_device_id == "cast-syp"
    assert "devices" in env.names() and "state" in env.names()
    j = to_json(st)
    assert j["devices"][0]["caps"] == ["cast_media", "cast_mirror", "dlna"]


def test_single_device_autoselected(tmp_path):
    env = Env(tmp_path, devices=[SALON])
    run(env.ctl.start())
    assert env.ctl.selected_device_id == "cast-salon"


def test_select_persists(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.dispatch("devices.select", {"id": "cast-salon"})

    run(go())
    assert Settings(tmp_path / "config.json").get("last_device_id") == "cast-salon"


def test_unknown_method_and_no_device(tmp_path):
    env = Env(tmp_path, devices=[])

    async def go():
        await env.ctl.start()
        with pytest.raises(AirlinxError) as e1:
            await env.ctl.dispatch("nope.nope")
        assert e1.value.code == "unknown_method"
        with pytest.raises(AirlinxError) as e2:
            await env.ctl.dispatch("mirror.start", {"sourceId": "monitor:DP-4"})
        assert e2.value.code == "no_device"

    run(go())


def test_add_manual(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        d = await env.ctl.dispatch("devices.addManual", {"ip": "192.0.2.50"})
        assert d.ip == "192.0.2.50" and env.ctl.selected_device_id == d.id
        with pytest.raises(AirlinxError) as e:
            await env.ctl.dispatch("devices.addManual", {"ip": "10.0.0.99"})
        assert e.value.code == "tv_not_found"

    run(go())
    assert env.settings.get("manual_devices") == ["192.0.2.50"]


def test_mirror_lifecycle_and_stats_throttle(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        res = await env.ctl.dispatch("mirror.start", {"sourceId": "monitor:DP-4", "options": {"bitrate": 8_000_000}})
        assert res == {"ok": True, "protocol": "cast"}
        await settle()
        assert env.ctl.mirror.state == "live" and env.ctl.mode == "mirror"
        s = FakeSession.instances[-1]
        assert s.opts.bitrate == 8_000_000 and s.forced is False
        st = Stats(120.0, 60, 8e6, 1920, 1080, 4.0, 0, "cast", "h264_nvenc", {})
        for _ in range(5):
            s.on_stats(st)
        await settle()
        assert env.names().count("mirror.stats") == 1  # throttled ≤ 2/s
        await env.ctl.dispatch("mirror.update", {"options": {"audio": False}})
        assert s.opts.audio is False and s.opts.bitrate == 8_000_000
        await env.ctl.dispatch("mirror.switchSource", {"sourceId": "monitor:DP-2.8"})
        assert s.source is DP28
        await env.ctl.dispatch("mirror.stop")
        assert s.stopped and env.ctl.mirror.state == "idle" and env.ctl.mode == "idle"

    run(go())
    assert env.settings.get("mirror.audio") is False
    assert env.settings.get("last_source_id") == "monitor:DP-2.8"


def test_tv_busy_flow(tmp_path):
    env = Env(tmp_path, busy=True)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        res = await env.ctl.dispatch("mirror.start", {"sourceId": "monitor:DP-4"})
        assert res["ok"] is False and res["code"] == "tv_busy"
        err = env.last("error")
        assert err["code"] == "tv_busy" and "YouTube" in err["message"]
        assert err["retry"]["method"] == "mirror.start" and err["retry"]["params"]["force"] is True
        assert env.ctl.mirror.state == "idle" and env.ctl.mode == "idle"
        res = await env.ctl.dispatch(err["retry"]["method"], err["retry"]["params"])
        assert res["ok"] is True
        assert FakeSession.instances[-1].forced is True

    run(go())


def test_cast_failure_falls_back_to_dlna(tmp_path):
    env = Env(tmp_path, cast_fail=True)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        res = await env.ctl.mirror_start("monitor:DP-4")
        assert res["protocol"] == "dlna"
        assert [s.proto for s in FakeSession.instances] == ["cast", "dlna"]
        assert any(e == "toast" for e, _ in env.events)

    run(go())


def test_protocol_setting_forces_cast_only(tmp_path):
    env = Env(tmp_path, cast_fail=True)
    env.settings.set("protocol", "cast")

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        with pytest.raises(AirlinxError):
            await env.ctl.mirror_start("monitor:DP-4")
        assert env.ctl.mirror.state == "error"

    run(go())


def test_session_error_emits_error(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        await env.ctl.mirror_start("monitor:DP-4")
        FakeSession.instances[-1].on_state("error", "TV zamknął sesję")
        await settle()
        assert env.ctl.mirror.state == "error" and env.ctl.mode == "idle"
        assert env.last("error")["code"] == "mirror_failed"

    run(go())


def _media_files(tmp_path, n=3):
    out = []
    for i in range(n):
        p = tmp_path / f"film{i}.mp4"
        p.write_bytes(b"x")
        out.append(str(p))
    (tmp_path / "film0.pl.srt").write_text("1\n")
    (tmp_path / "notes.txt").write_text("x")
    return out


def test_queue_add_remove_reorder(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path)

    async def go():
        await env.ctl.start()
        items = await env.ctl.dispatch("media.add", {"paths": files + [str(tmp_path / "notes.txt"), "/missing.mp4"]})
        assert [i.title for i in items] == ["film0", "film1", "film2"]
        assert items[0].subtitles and items[0].subtitles[0].endswith("film0.pl.srt")
        assert env.last("toast")["message"].startswith("Pominięto 2")
        ids = [i.id for i in items]
        await env.ctl.dispatch("media.reorder", {"ids": [ids[2], ids[0]]})
        assert [q.id for q in env.ctl.queue] == [ids[2], ids[0], ids[1]]
        await env.ctl.dispatch("media.remove", {"id": ids[0]})
        assert [q.id for q in env.ctl.queue] == [ids[2], ids[1]]
        urls = await env.ctl.media_add(urls=["https://example.com/v/klip.m3u8"])
        assert urls[0].kind == "video" and urls[0].title == "klip.m3u8"
        dir_items = await env.ctl.media_add(paths=[str(tmp_path)])
        assert len(dir_items) == 3

    run(go())


def test_pick_files(tmp_path):
    env = Env(tmp_path)
    env.picked = _media_files(tmp_path, 2)
    items = run(env.ctl.dispatch("media.pickFiles"))
    assert len(items) == 2 and len(env.ctl.queue) == 2


def test_media_play_controls_and_auto_advance(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        res = await env.ctl.dispatch("media.play", {"id": items[0].id})
        assert res["ok"] and res["protocol"] == "cast"
        await settle()
        p = env.players[-1]
        assert p.loaded[0][0] == items[0].id and env.server.started
        assert env.ctl.playback.state == "playing" and env.ctl.mode == "media"
        await env.ctl.dispatch("media.pause")
        await env.ctl.dispatch("media.resume")
        await env.ctl.dispatch("media.seek", {"seconds": 42})
        await env.ctl.dispatch("media.volume", {"value": 1.7})
        assert p.calls[-3:] == ["play", "seek:42.0", "vol:1.0"]
        # ended → auto-advance to the next item
        p.on_state(PlaybackState("ended", position=10, duration=10, item_id=items[0].id))
        await settle()
        await asyncio.sleep(0.01)
        assert env.ctl.playback.item_id == items[1].id
        assert p.loaded[-1][0] == items[1].id  # same player reused
        await env.ctl.dispatch("media.next")
        assert env.ctl.playback.item_id == items[2].id
        with pytest.raises(AirlinxError) as e:
            await env.ctl.dispatch("media.next")
        assert e.value.code == "queue_end"
        await env.ctl.dispatch("media.prev")
        assert env.ctl.playback.item_id == items[1].id
        # last item ends → idle, no advance
        await env.ctl.media_play(items[2].id)
        p.on_state(PlaybackState("ended", item_id=items[2].id))
        await settle()
        assert env.ctl.mode == "idle"
        await env.ctl.dispatch("media.stop")
        assert "stop" in p.calls and env.ctl.playback.state == "idle"

    run(go())


def test_media_tv_busy_then_force(tmp_path):
    env = Env(tmp_path, player_busy=True)
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        res = await env.ctl.media_play(items[0].id)
        assert res["code"] == "tv_busy"
        err = env.last("error")
        assert err["retry"] == {"method": "media.play", "params": {"id": items[0].id, "force": True}}
        res = await env.ctl.dispatch("media.play", err["retry"]["params"])
        assert res["ok"] and env.players[-1].loaded[-1][4] is True

    run(go())


def test_media_stops_mirror_and_vice_versa(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        await env.ctl.mirror_start("monitor:DP-4")
        s = FakeSession.instances[-1]
        items = await env.ctl.media_add(paths=files)
        await env.ctl.media_play(items[0].id)
        assert s.stopped and env.ctl.mode == "media"
        await env.ctl.mirror_start("monitor:DP-4")
        assert "stop" in env.players[-1].calls and env.ctl.mode == "mirror"

    run(go())


def test_subtitles_select(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        items = await env.ctl.media_add(paths=files)
        it = await env.ctl.dispatch("media.subtitles", {"id": items[0].id, "track": 0})
        assert it.subtitle_track.endswith("film0.pl.srt")
        it = await env.ctl.dispatch("media.subtitles", {"id": items[0].id, "track": None})
        assert it.subtitle_track is None

    run(go())


def test_settings_set_and_autostart_hook(tmp_path):
    env = Env(tmp_path)
    calls = []
    env.ctl.hooks.set_autostart = calls.append

    async def go():
        data = await env.ctl.dispatch("settings.set", {"patch": {"autostart": True, "mirror": {"fps": 30}}})
        assert data["autostart"] is True and data["mirror"]["fps"] == 30 and data["mirror"]["bitrate"] == 12_000_000
        assert env.ctl.mirror.options.fps == 30
        assert (await env.ctl.dispatch("settings.get"))["autostart"] is True

    run(go())
    assert calls == [True]


def test_tray_toggle_uses_last_source(tmp_path):
    env = Env(tmp_path)
    env.settings.set("last_source_id", "monitor:DP-2.8")

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        await env.ctl.mirror_toggle()
        assert FakeSession.instances[-1].source is DP28
        await env.ctl.mirror_toggle()
        assert FakeSession.instances[-1].stopped

    run(go())


def test_shutdown_stops_everything(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        await env.ctl.media_play(items[0].id)
        await env.ctl.shutdown()
        assert "stop" in env.players[-1].calls and env.server.stopped
        assert env.ctl._discovery.stopped

    run(go())


def test_state_json_shape(tmp_path):
    env = Env(tmp_path)
    run(env.ctl.start())
    j = to_json(env.ctl.state())
    assert set(j) >= {"devices", "selected_device_id", "mode", "mirror", "queue", "playback", "settings", "protocol"}
    assert j["mirror"]["options"]["fps"] == 60 and j["playback"]["state"] == "idle"


# ---------------------------------------------------------------- review fixes


def test_mirror_stop_cancels_pending_start_immediately(tmp_path):
    env = Env(tmp_path)
    env.modes["cast"] = "hang"

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        start = asyncio.ensure_future(env.ctl.dispatch("mirror.start", {"sourceId": "monitor:DP-4"}))
        await asyncio.sleep(0.01)
        assert env.ctl.mirror.state == "connecting"
        assert not env.ctl.lock.locked()  # handshake does not hold the operation lock
        stop = await asyncio.wait_for(env.ctl.dispatch("mirror.stop"), 1)
        assert stop == {"ok": True, "cancelled": True}
        res = await asyncio.wait_for(start, 1)
        assert res["code"] == "cancelled"
        assert FakeSession.instances[-1].cancelled
        assert len(FakeSession.instances) == 1  # cancel does not fall back to DLNA
        assert env.ctl.mirror.state == "idle" and env.ctl.mirror.detail == "Anulowano" and env.ctl.mode == "idle"

    run(go())


def test_shutdown_cancels_pending_start(tmp_path):
    env = Env(tmp_path)
    env.modes["cast"] = "hang"

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        start = asyncio.ensure_future(env.ctl.mirror_start("monitor:DP-4"))
        await asyncio.sleep(0.01)
        await asyncio.wait_for(env.ctl.shutdown(), 1)
        assert (await start)["code"] == "cancelled" and FakeSession.instances[-1].cancelled

    run(go())


def test_new_start_cancels_previous_start(tmp_path):
    env = Env(tmp_path)
    env.modes["cast"] = "hang"

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        first = asyncio.ensure_future(env.ctl.mirror_start("monitor:DP-4"))
        await asyncio.sleep(0.01)
        env.modes.clear()
        res = await asyncio.wait_for(env.ctl.mirror_start("monitor:DP-2.8"), 1)
        assert res["ok"] and (await first)["code"] == "cancelled"
        await settle()
        assert env.ctl.mirror.state == "live" and env.ctl.mirror.source_id == "monitor:DP-2.8"

    run(go())


@pytest.mark.parametrize("mode,code", [("denied", "tv_denied"), ("cert", "tv_cert_changed"),
                                       ("auth", "tv_auth_failed")])
def test_fallback_stops_on_terminal_codes(tmp_path, mode, code):
    env = Env(tmp_path)
    env.modes["cast"] = mode

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        with pytest.raises(AirlinxError) as e:
            await env.ctl.mirror_start("monitor:DP-4")
        assert e.value.code == code
        assert [s.proto for s in FakeSession.instances] == ["cast"]  # no DLNA attempt
        assert env.ctl.mirror.state == "error" and env.ctl.mirror.detail == e.value.message_pl  # shown in UI

    run(go())


def test_failed_cast_attempt_error_suppressed_when_dlna_left(tmp_path):
    env = Env(tmp_path)
    env.modes["cast"] = "error_then_fail"

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        res = await env.ctl.mirror_start("monitor:DP-4")
        await settle()
        assert res["protocol"] == "dlna" and env.ctl.mirror.state == "live"
        assert "error" not in env.names()
        assert all(p.state != "error" for e, p in env.events if e == "mirror.state")

    run(go())


def test_last_protocol_error_is_reported(tmp_path):
    env = Env(tmp_path, devices=[SYPIALNIA])  # cast only
    env.modes["cast"] = "error_then_fail"

    async def go():
        await env.ctl.start()
        with pytest.raises(AirlinxError):
            await env.ctl.mirror_start("monitor:DP-4")
        assert env.ctl.mirror.state == "error"

    run(go())


def test_cert_changed_emits_trust_again_action(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.modes["cast"] = "cert"
    forgotten = []
    import airlinx.core.cast.castv2 as castv2
    monkeypatch.setattr(castv2, "forget_tv_cert", lambda key: forgotten.append(key) or key == "cast-salon")

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        with pytest.raises(AirlinxError):
            await env.ctl.mirror_start("monitor:DP-4")
        err = env.last("error")
        assert err["code"] == "tv_cert_changed" and err["device_id"] == "cast-salon"
        assert err["retry"] == {"method": "devices.trustAgain", "params": {"id": "cast-salon"}}
        res = await env.ctl.dispatch(err["retry"]["method"], err["retry"]["params"])
        assert res == {"ok": True, "removed": True}
        assert forgotten == ["cast-salon", "192.0.2.10"]
        assert "Zaufano ponownie: Salon" in env.last("toast")["message"]

    run(go())


def test_unpublish_on_stop_and_next(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path, 2)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        await env.ctl.media_play(items[0].id)
        u0 = f"http://192.0.2.5:8000/m/{items[0].id}"
        assert env.server.unpublished == []
        await env.ctl.media_next()
        assert env.server.unpublished == [u0]
        await env.ctl.media_stop()
        assert env.server.unpublished == [u0, f"http://192.0.2.5:8000/m/{items[1].id}"]

    run(go())


def test_shutdown_stops_in_parallel(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        await env.ctl.mirror_start("monitor:DP-4")
        items = await env.ctl.media_add(paths=files)
        await env.ctl.media_play(items[0].id)  # stops the mirror session
        # put both back to exercise the parallel path with slow stops
        sess = FakeSession("cast", SALON, DP4, MirrorOptions(), lambda s: None, lambda *a: None)
        player = env.players[-1]

        async def slow_sess_stop():
            await asyncio.sleep(0.2)
            sess.stopped = True

        async def slow_player_stop():
            await asyncio.sleep(0.2)
            player.calls.append("stop")

        sess.stop = slow_sess_stop
        player.stop = slow_player_stop
        env.ctl._session = sess
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await env.ctl.shutdown()
        assert loop.time() - t0 < 0.35
        assert sess.stopped and "stop" in player.calls and env.server.stopped

    run(go())


def test_publish_passes_tv_ip(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        await env.ctl.media_play(items[0].id)
        assert env.server.target_ips == ["192.0.2.10"]

    run(go())


def test_publish_without_target_ip_support():
    from airlinx.controller import Controller

    class Old:
        def publish(self, item, target):
            return "u", "m"

    from airlinx.core.models import MediaItem
    res = asyncio.run(Controller._publish(Old(), MediaItem("i", "/a.mp4", "a", None, "video"), "dlna", SALON))
    assert res == ("u", "m")


def test_unknown_player_volume_keeps_user_volume(tmp_path):
    env = Env(tmp_path)
    files = _media_files(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        await env.ctl.dispatch("media.play", {"id": items[0].id})
        await settle()
        p = env.players[-1]
        await env.ctl.dispatch("media.volume", {"value": 0.4})
        p.on_state(PlaybackState("playing", position=1, duration=10, volume=None, item_id=items[0].id))
        assert env.ctl.playback.volume == 0.4
        assert env.ctl.playback.state == "playing"

    run(go())


# ----------------------------------------------------------------------------- audio source
def _start_mirror(env):
    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        return await env.ctl.dispatch("mirror.start", {"sourceId": "monitor:DP-4"})
    return go


def test_startup_cleans_orphaned_audio_routing(tmp_path):
    env = Env(tmp_path)
    run(env.ctl.start())
    assert env.audio.cleanups == 1


def test_audio_list_rpc(tmp_path):
    env = Env(tmp_path)
    res = run(env.ctl.dispatch("audio.list"))
    assert res["mics"][0]["label"] == "Blue Yeti" and res["apps"][0]["key"] == "firefox"
    assert res["default_mic"] == "Blue Yeti"


def test_mirror_uses_audio_source_from_settings(tmp_path):
    env = Env(tmp_path)
    env.settings.update({"audio": {"source": "app", "app": "firefox", "app_name": "Firefox"}})

    async def go():
        await _start_mirror(env)()
        await settle()
        s = FakeSession.instances[-1]
        assert (s.opts.audio_source, s.opts.audio_app, s.opts.audio_app_name) == ("app", "firefox", "Firefox")
        assert env.ctl.mirror.audio == {"source": "app", "requested": "app", "label": "Tylko Firefox", "note": None}

    run(go())
    # the mirror section keeps only video/stream options; the audio choice lives in "audio"
    assert "audio_source" not in env.settings.get("mirror")
    assert env.settings.get("audio.source") == "app"


def test_missing_app_falls_back_to_system_with_notice(tmp_path):
    env = Env(tmp_path)
    env.settings.update({"audio": {"source": "app", "app": "vlc", "app_name": "VLC"}})

    async def go():
        res = await _start_mirror(env)()
        assert res["ok"] is True                         # mirroring is not interrupted
        await settle()
        s = FakeSession.instances[-1]
        assert s.opts.audio_source == "system"
        assert env.ctl.mirror.audio["source"] == "system" and env.ctl.mirror.audio["requested"] == "app"
        assert env.ctl.mirror.audio["note"] == "VLC nic teraz nie odtwarza. Wysyłam dźwięk systemu."

    run(go())
    toasts = [p["message"] for e, p in env.events if e == "toast"]
    assert "VLC nic teraz nie odtwarza. Wysyłam dźwięk systemu." in toasts
    assert env.settings.get("audio.source") == "app"    # the user's choice is kept for next time


def test_audio_disabled_skips_resolution(tmp_path):
    env = Env(tmp_path)
    env.settings.update({"mirror": {"audio": False}, "audio": {"source": "app", "app": "vlc"}})
    run(_start_mirror(env)())
    assert env.audio.resolved == [] and env.ctl.mirror.audio is None


def test_changing_audio_source_while_live_updates_session(tmp_path):
    env = Env(tmp_path)

    async def go():
        await _start_mirror(env)()
        await settle()
        s = FakeSession.instances[-1]
        s.live_audio_switch = True
        data = await env.ctl.dispatch("settings.set", {"patch": {"audio": {"source": "mic", "mic": "usb-yeti",
                                                                          "mic_name": "Blue Yeti"}}})
        assert data["audio"]["source"] == "mic"
        assert (s.opts.audio_source, s.opts.audio_mic) == ("mic", "usb-yeti")
        assert env.ctl.mirror.audio["label"] == "Mikrofon: Blue Yeti"
        # a vanished mic during the session → system, with a notice
        await env.ctl.dispatch("settings.set", {"patch": {"audio": {"source": "mic", "mic": "gone",
                                                                   "mic_name": "Rode"}}})
        assert s.opts.audio_source == "system" and env.ctl.mirror.audio["note"]

    run(go())


def test_audio_change_on_session_without_live_switch_applies_next_time(tmp_path):
    env = Env(tmp_path)

    async def go():
        await _start_mirror(env)()
        await settle()
        s = FakeSession.instances[-1]       # FakeSession (like the DLNA adapter) has no live_audio_switch
        before = s.opts.audio_source
        await env.ctl.dispatch("settings.set", {"patch": {"audio": {"source": "mic"}}})
        assert s.opts.audio_source == before
        assert env.ctl.mirror.audio["pending"] is True

    run(go())
    toasts = [p["message"] for e, p in env.events if e == "toast"]
    assert any("następnym" in t for t in toasts)


def test_session_notices_become_toasts(tmp_path):
    env = Env(tmp_path)

    async def go():
        await _start_mirror(env)()
        await settle()
        s = FakeSession.instances[-1]
        assert callable(getattr(s, "on_notice", None))
        s.on_notice("Dźwięk na TV przestał działać. Obraz leci dalej.")
        await settle()

    run(go())
    assert ("toast", {"message": "Dźwięk na TV przestał działać. Obraz leci dalej."}) in env.events


# ----------------------------------------------------------------------------- DLNA mirror audio
class _FakePipe:
    def __init__(self, spec, **kw):
        self.spec, self.kw = spec, kw
        self.opened = self.closed = False

    async def open(self):
        self.opened = True
        return {"system": ["sink.monitor"], "mic": ["@DEFAULT_SOURCE@"], "system_mic": ["sink.monitor", "mic1"],
                "app": ["airlinx_app.monitor"]}[self.spec.source]

    async def close(self):
        self.closed = True


def _probe(nvenc=True, x264=True, calls=None):
    """Stand-in for core.capture.probe_encoders (no ffmpeg runs in the offline suite)."""
    async def probe():
        if calls is not None:
            calls.append(1)
        return {"nvenc": nvenc, "cuda_scale": nvenc, "x264": x264}
    return probe


def _dlna(opts, pipes, fail=False, monkeypatch=None, probe=None, on_stats=None):
    from airlinx import controller as ctl_mod
    from airlinx.core import dlna

    class FakeDlnaMirror:
        def __init__(self, *a, **kw):
            pass

        async def start(self, force=False):
            if fail:
                raise RuntimeError("TV refused")

        async def stop(self):
            pass

    monkeypatch.setattr(dlna, "DlnaMirror", FakeDlnaMirror)

    def factory(spec, **kw):
        p = _FakePipe(spec, **kw)
        pipes.append(p)
        return p

    async def server():
        return object()

    return ctl_mod.DlnaMirrorAdapter(SALON, DP4, opts, on_stats or (lambda s: None), lambda s, d: None, server,
                                     pipeline_factory=factory, encoder_probe=probe or _probe())


def _video_codec(cmd):
    return cmd[cmd.index("-c:v") + 1]


def test_dlna_mirror_uses_nvenc_when_it_works(monkeypatch):
    monkeypatch.delenv("AIRLINX_DLNA_ENCODER", raising=False)

    async def go():
        a = _dlna(MirrorOptions(audio=False), [], monkeypatch=monkeypatch, probe=_probe(nvenc=True))
        await a.start()
        cmd = a.ffmpeg_cmd()
        await a.stop()
        return cmd

    cmd = run(go())
    assert _video_codec(cmd) == "h264_nvenc"
    assert "libx264" not in cmd


def test_dlna_mirror_falls_back_to_libx264_without_nvidia(monkeypatch):
    monkeypatch.delenv("AIRLINX_DLNA_ENCODER", raising=False)
    stats = []

    async def go():
        a = _dlna(MirrorOptions(audio=False), [], monkeypatch=monkeypatch, probe=_probe(nvenc=False),
                  on_stats=stats.append)
        await a.start()
        cmd = a.ffmpeg_cmd()
        await asyncio.sleep(1.1)                     # one stats tick
        await a.stop()
        return cmd

    cmd = run(go())
    s = " ".join(cmd)
    assert _video_codec(cmd) == "libx264"
    assert "nvenc" not in s
    assert "-preset ultrafast" in s and "-tune zerolatency" in s     # low latency, not "veryfast"
    assert stats and stats[-1].encoder == "libx264"                  # UI shows what really encodes


def test_dlna_mirror_env_encoder_overrides_detection(monkeypatch):
    monkeypatch.setenv("AIRLINX_DLNA_ENCODER", "libx264")
    calls = []

    async def go():
        a = _dlna(MirrorOptions(audio=False), [], monkeypatch=monkeypatch, probe=_probe(nvenc=True, calls=calls))
        await a.start()
        cmd = a.ffmpeg_cmd()
        await a.stop()
        return cmd

    assert _video_codec(run(go())) == "libx264"
    assert calls == []                               # no probing when the user chose


def test_dlna_mirror_without_any_h264_encoder_fails_cleanly(monkeypatch):
    monkeypatch.delenv("AIRLINX_DLNA_ENCODER", raising=False)
    pipes = []

    async def go():
        a = _dlna(MirrorOptions(audio_source="app", audio_app="firefox"), pipes, monkeypatch=monkeypatch,
                  probe=_probe(nvenc=False, x264=False))
        with pytest.raises(AirlinxError) as e:
            await a.start()
        return e.value.code

    assert run(go()) == "no_encoder"
    assert not pipes or pipes[0].closed              # nothing left routed


def test_dlna_mirror_audio_sources_per_mode(monkeypatch):
    async def go():
        out = {}
        for src in ("system", "mic", "system_mic", "app"):
            pipes = []
            a = _dlna(MirrorOptions(audio_source=src, audio_app="firefox"), pipes, monkeypatch=monkeypatch)
            await a.start()
            out[src] = a.ffmpeg_cmd()
            await a.stop()
            assert pipes[0].opened and pipes[0].closed
        return out

    out = run(go())
    s = " ".join(out["system"])
    assert "-f pulse" in s and "-i sink.monitor" in s and "-i default" not in s   # system = monitor, not the mic
    assert "-map 0:v -map 1:a" in s and "-c:a aac" in s
    assert "-i @DEFAULT_SOURCE@" in " ".join(out["mic"])
    m = " ".join(out["system_mic"])
    assert m.count("-f pulse") == 2 and "amix=inputs=2" in m and "[1:a]" in m and "[2:a]" in m
    assert "-map 0:v -map [aout]" in m
    assert "-i airlinx_app.monitor" in " ".join(out["app"])


def test_dlna_mirror_without_audio_has_no_pulse(monkeypatch):
    async def go():
        pipes = []
        a = _dlna(MirrorOptions(audio=False), pipes, monkeypatch=monkeypatch)
        await a.start()
        cmd = " ".join(a.ffmpeg_cmd())
        await a.stop()
        return cmd, pipes

    cmd, pipes = run(go())
    assert "pulse" not in cmd and not pipes


def test_dlna_mirror_failed_start_releases_routing(monkeypatch):
    async def go():
        pipes = []
        a = _dlna(MirrorOptions(audio_source="app", audio_app="firefox"), pipes, fail=True, monkeypatch=monkeypatch)
        with pytest.raises(RuntimeError):
            await a.start()
        return pipes

    pipes = run(go())
    assert pipes[0].opened and pipes[0].closed


def test_dlna_mirror_ended_on_tv_releases_routing(monkeypatch):
    async def go():
        pipes = []
        a = _dlna(MirrorOptions(audio_source="app", audio_app="firefox"), pipes, monkeypatch=monkeypatch)
        await a.start()
        a._on_playback(PlaybackState(state="ended"))     # TV closed the stream; controller drops the session
        await settle()
        return pipes

    pipes = run(go())
    assert pipes[0].closed


# ----------------------------------------------------------------------------- DLNA mirror cleanup
class _LiveServer:
    def __init__(self):
        self.lives: dict[str, Any] = {}

    def register_live(self, name, source, target_ip=None):
        self.lives[name] = source
        return f"http://192.0.2.5:8000/live/tok/{name}.ts"

    def unregister_live(self, name):
        self.lives.pop(name, None)


def _real_dlna_mirror(monkeypatch, *, load_fail=False):
    """DlnaMirrorAdapter over the real core.dlna.DlnaMirror with a fake DlnaPlayer (no network)."""
    from airlinx import controller as ctl_mod
    from airlinx.core import dlna

    players = []

    class FakeDlnaPlayer:
        def __init__(self, device, on_state):
            self.on_state = on_state
            self.calls: list[str] = []
            players.append(self)

        async def load(self, item, url, mime, start=0, *, force=False):
            self.calls.append("load")
            if load_fail:
                raise AirlinxError("dlna_load_failed", "Telewizor nie mógł odtworzyć tego pliku (DLNA).")

        async def stop(self):
            self.calls.append("stop")

        async def close(self):
            self.calls.append("close")

    monkeypatch.setattr(dlna, "DlnaPlayer", FakeDlnaPlayer)
    server = _LiveServer()
    states, stats, pipes = [], [], []

    async def ensure_server():
        return server

    def factory(spec, **kw):
        p = _FakePipe(spec, **kw)
        pipes.append(p)
        return p

    a = ctl_mod.DlnaMirrorAdapter(SALON, DP4, MirrorOptions(audio_source="app", audio_app="firefox"),
                                  stats.append, lambda s, d: states.append(s), ensure_server,
                                  pipeline_factory=factory, encoder_probe=_probe())
    return a, server, players, states, stats, pipes


@pytest.mark.parametrize("tv_state", ["ended", "error"])
def test_dlna_mirror_ended_on_tv_cleans_up(monkeypatch, tv_state):
    async def go():
        a, server, players, states, stats, pipes = _real_dlna_mirror(monkeypatch)
        await a.start()
        assert any(n.startswith("mirror") for n in server.lives)
        a._on_playback(PlaybackState(state=tv_state, error="x" if tv_state == "error" else None))
        a._on_playback(PlaybackState(state=tv_state))   # repeated poll result: cleanup runs once
        await settle()
        assert not server.lives                              # no ffmpeg on a stray LAN GET
        assert a._stats_task is not None and a._stats_task.done()   # no more stats
        assert players[0].calls.count("close") == 1          # HTTP session closed, once
        assert pipes[0].closed
        assert states[-1] == ("stopped" if tv_state == "ended" else "error")  # no extra "stopped" after error
        await a.stop()                                       # controller may still call stop(): harmless
        assert players[0].calls.count("close") == 1

    run(go())


def test_dlna_mirror_failed_start_cleans_up(monkeypatch):
    async def go():
        a, server, players, states, stats, pipes = _real_dlna_mirror(monkeypatch, load_fail=True)
        with pytest.raises(AirlinxError):
            await a.start()
        assert not server.lives
        assert "close" in players[0].calls and pipes[0].closed
        assert a._stats_task is None

    run(go())


@pytest.mark.parametrize("code", ["tv_auth_failed", "tv_cert_changed"])
def test_media_fallback_stops_on_identity_errors(tmp_path, code):
    from airlinx.core.cast.castv2 import TvAuthFailedError, TvCertChangedError

    env = Env(tmp_path)
    env.player_fail["cast"] = TvAuthFailedError("x") if code == "tv_auth_failed" else TvCertChangedError("cast-salon")
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        with pytest.raises(AirlinxError) as e:
            await env.ctl.media_play(items[0].id)
        assert e.value.code == code
        assert [p.proto for p in env.players] == ["cast"]          # no DLNA attempt to the same host
        assert env.ctl.playback.state == "error"
        if code == "tv_cert_changed":
            assert env.last("error")["code"] == "tv_cert_changed"

    run(go())


def test_media_play_cancels_pending_mirror_start(tmp_path):
    env = Env(tmp_path)
    env.modes["cast"] = "hang"
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        start = asyncio.ensure_future(env.ctl.mirror_start("monitor:DP-4"))
        await asyncio.sleep(0.01)
        res = await asyncio.wait_for(env.ctl.media_play(items[0].id), 1)
        assert res["ok"]
        assert (await asyncio.wait_for(start, 1))["code"] == "cancelled"
        await settle()
        assert [s.proto for s in FakeSession.instances] == ["cast"]   # no DLNA mirror fighting the film
        assert env.ctl.mode == "media" and env.ctl._session is None
        assert env.ctl.mirror.state == "idle"

    run(go())


@pytest.mark.parametrize("cfg", [
    {"media": {"volume": None}},
    {"mirror": "x"},
    {"media": "x"},
    {"mirror": {"fps": "60", "bitrate": None}},
    {"audio": ["system"], "version": 2},
])
def test_wrongly_typed_config_values_fall_back_to_defaults(tmp_path, cfg, caplog):
    import json

    from airlinx.settings import DEFAULTS

    (tmp_path / "config.json").write_text(json.dumps(cfg))
    env = Env(tmp_path)                     # Controller() must not raise
    run(env.ctl.start())
    assert env.ctl.playback.volume == DEFAULTS["media"]["volume"]
    assert to_json(env.ctl.mirror.options)["fps"] == DEFAULTS["mirror"]["fps"]
    assert env.ctl.mirror.options.bitrate == DEFAULTS["mirror"]["bitrate"]
    assert env.ctl.mirror.options.audio_source == "system"
    assert env.settings.get("media.auto_advance") is True
    assert not (tmp_path / "config.json.bak").exists()   # valid JSON: only the bad key is reset


# ----------------------------------------------------------------------------- Cast → DLNA downgrade
UNVERIFIED_PL = "Nie udało się potwierdzić tożsamości telewizora przez Google Cast. Wysłać przez DLNA bez weryfikacji?"


@pytest.mark.parametrize("key", ["cast-salon", "192.0.2.10"])
def test_pinned_tv_does_not_silently_fall_back_to_dlna_for_mirror(tmp_path, known_tvs, key):
    _pin(known_tvs, key)
    env = Env(tmp_path)
    env.modes["cast"] = "refused"     # an impostor on the TV's IP simply closes 8009

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        res = await env.ctl.mirror_start("monitor:DP-4")
        assert res == {"ok": False, "code": "cast_unverified", "message": UNVERIFIED_PL}
        assert [s.proto for s in FakeSession.instances] == ["cast"]          # no DLNA attempt
        assert env.ctl.mode == "idle" and env.ctl.mirror.state == "idle"
        assert not any(e == "toast" and "DLNA" in p["message"] for e, p in env.events)
        err = env.last("error")
        assert err["code"] == "cast_unverified" and err["message"] == UNVERIFIED_PL
        assert err["retry"]["method"] == "mirror.start"
        assert err["retry"]["params"]["protocol"] == "dlna" and err["retry"]["params"]["sourceId"] == "monitor:DP-4"
        # the user confirms in the dialog → DLNA only
        res = await env.ctl.dispatch(err["retry"]["method"], err["retry"]["params"])
        await settle()
        assert res == {"ok": True, "protocol": "dlna"}
        assert [s.proto for s in FakeSession.instances] == ["cast", "dlna"]
        assert env.ctl.mirror.state == "live" and env.ctl.mirror.protocol == "dlna"

    run(go())


def test_pinned_tv_does_not_silently_fall_back_to_dlna_for_media(tmp_path, known_tvs):
    from airlinx.core.models import TvNotFoundError

    _pin(known_tvs)
    env = Env(tmp_path)
    env.player_fail["cast"] = TvNotFoundError("192.0.2.10:8009: refused")
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        res = await env.ctl.media_play(items[0].id)
        assert res == {"ok": False, "code": "cast_unverified", "message": UNVERIFIED_PL}
        assert [p.proto for p in env.players] == ["cast"]
        assert env.server.published == [(items[0].id, "cast")]   # nothing was published for DLNA
        err = env.last("error")
        assert err["code"] == "cast_unverified"
        assert err["retry"] == {"method": "media.play", "params": {"id": items[0].id, "protocol": "dlna"}}
        res = await env.ctl.dispatch(err["retry"]["method"], err["retry"]["params"])
        assert res["ok"] and res["protocol"] == "dlna"
        assert [p.proto for p in env.players] == ["cast", "dlna"] and env.ctl.mode == "media"

    run(go())


def test_unpinned_tv_still_falls_back_to_dlna(tmp_path):
    env = Env(tmp_path)
    env.modes["cast"] = "refused"
    env.player_fail["cast"] = RuntimeError("cast down")
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        assert (await env.ctl.mirror_start("monitor:DP-4"))["protocol"] == "dlna"
        items = await env.ctl.media_add(paths=files)
        assert (await env.ctl.media_play(items[0].id))["protocol"] == "dlna"
        assert "cast_unverified" not in [p.get("code") for e, p in env.events if e == "error"]

    run(go())


def test_bad_protocol_param_rejected(tmp_path):
    env = Env(tmp_path)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        with pytest.raises(AirlinxError) as e:
            await env.ctl.mirror_start("monitor:DP-4", protocol="airplay")
        assert e.value.code == "bad_params"

    run(go())


# ----------------------------------------------------------------------------- pin check fails closed
@pytest.mark.parametrize("content", ["{not json", "[]", "\x00\xff garbage"])
def test_unreadable_known_tvs_counts_as_pinned(tmp_path, known_tvs, content):
    known_tvs.write_bytes(content.encode("latin-1"))
    env = Env(tmp_path)
    env.modes["cast"] = "refused"

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        res = await env.ctl.mirror_start("monitor:DP-4")
        assert res["code"] == "cast_unverified"
        assert [s.proto for s in FakeSession.instances] == ["cast"]

    run(go())


def test_known_tvs_that_cannot_be_read_counts_as_pinned(tmp_path, known_tvs):
    known_tvs.mkdir()       # exists, but reading it fails (IsADirectoryError)
    env = Env(tmp_path)
    env.modes["cast"] = "refused"

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        assert (await env.ctl.mirror_start("monitor:DP-4"))["code"] == "cast_unverified"

    run(go())


def test_pin_check_without_castv2_counts_as_pinned(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "airlinx.core.cast.castv2", None)   # import → ImportError
    env = Env(tmp_path)
    env.modes["cast"] = "refused"

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        assert (await env.ctl.mirror_start("monitor:DP-4"))["code"] == "cast_unverified"

    run(go())


# ----------------------------------------------------------------------------- pinned TV without Cast caps
def _dlna_only(dev_id="cast-salon", ip="192.0.2.10"):
    return Device(dev_id, "Salon", "Samsung S85F", ip, None, f"http://{ip}:9197/dmr", {"dlna"})


@pytest.mark.parametrize("key", ["cast-salon", "192.0.2.10"])
def test_pinned_tv_seen_only_as_dlna_asks_before_mirroring(tmp_path, known_tvs, key):
    _pin(known_tvs, key)
    dev = _dlna_only(dev_id="uuid:dmr-salon" if key == "192.0.2.10" else "cast-salon")
    env = Env(tmp_path, devices=[dev])

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select(dev.id)
        res = await env.ctl.mirror_start("monitor:DP-4")
        assert res == {"ok": False, "code": "cast_unverified", "message": UNVERIFIED_PL}
        assert FakeSession.instances == []                       # nothing sent to the TV
        err = env.last("error")
        assert err["retry"]["method"] == "mirror.start" and err["retry"]["params"]["protocol"] == "dlna"
        res = await env.ctl.dispatch(err["retry"]["method"], err["retry"]["params"])
        assert res == {"ok": True, "protocol": "dlna"}

    run(go())


def test_pinned_tv_seen_only_as_dlna_asks_before_media(tmp_path, known_tvs):
    _pin(known_tvs)
    env = Env(tmp_path, devices=[_dlna_only()])
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        items = await env.ctl.media_add(paths=files)
        res = await env.ctl.media_play(items[0].id)
        assert res["code"] == "cast_unverified"
        assert env.players == [] and env.server.published == []
        err = env.last("error")
        assert err["retry"] == {"method": "media.play", "params": {"id": items[0].id, "protocol": "dlna"}}
        res = await env.ctl.dispatch(err["retry"]["method"], err["retry"]["params"])
        assert res["ok"] and res["protocol"] == "dlna"

    run(go())


def test_dlna_protocol_setting_is_explicit_consent(tmp_path, known_tvs):
    _pin(known_tvs)
    env = Env(tmp_path, devices=[_dlna_only()])
    env.settings.set("protocol", "dlna")
    files = _media_files(tmp_path, 1)

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        assert (await env.ctl.mirror_start("monitor:DP-4"))["protocol"] == "dlna"
        items = await env.ctl.media_add(paths=files)
        assert (await env.ctl.media_play(items[0].id))["protocol"] == "dlna"
        assert "cast_unverified" not in [p.get("code") for e, p in env.events if e == "error"]

    run(go())


def test_unpinned_dlna_only_tv_plays_without_asking(tmp_path):
    env = Env(tmp_path, devices=[_dlna_only()])

    async def go():
        await env.ctl.start()
        await env.ctl.devices_select("cast-salon")
        assert (await env.ctl.mirror_start("monitor:DP-4"))["protocol"] == "dlna"

    run(go())
