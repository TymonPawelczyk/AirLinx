"""Audio source selection (core/audio.py) with a fake pactl — no PipeWire needed."""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import pytest

from airlinx.core import audio as au

ANALOG = "alsa_output.pci-0000_09_00.4.analog-stereo"
HDMI = "alsa_output.pci-0000_07_00.1.hdmi-stereo"
MIC = "alsa_input.usb-Blue_Yeti-00.analog-stereo"


def _sink_input(index: int, sink: int, binary: Optional[str], name: Optional[str], icon: Optional[str] = None,
                media: str = "Playback", role: Optional[str] = None, object_id: Optional[int] = None) -> dict:
    props = {"client.api": "pipewire-pulse", "media.name": media, "object.id": str(object_id or index - 200)}
    if binary:
        props["application.process.binary"] = binary
    if name:
        props["application.name"] = name
    if icon:
        props["application.icon_name"] = icon
    if role:
        props["media.role"] = role
    return {"index": index, "driver": "PipeWire", "owner_module": None, "client": "1", "sink": sink,
            "corked": False, "mute": False, "balance": 0.0, "properties": props}


class FakePulse:
    """In-memory pulse server answering the pactl/pw-metadata argv the module uses."""

    def __init__(self) -> None:
        self.sinks = {47: ANALOG, 73: HDMI}
        self.owner: dict[int, int] = {}
        self.modules: dict[int, tuple[str, str]] = {}
        self.sources = [
            {"index": 48, "name": ANALOG + ".monitor", "description": "Monitor of Analog", "monitor_source": ANALOG,
             "properties": {"device.class": "monitor"}},
            {"index": 50, "name": MIC, "description": "Blue Yeti Analog Stereo", "monitor_source": "",
             "properties": {"device.class": "sound", "device.icon_name": "audio-input-microphone"}},
            {"index": 51, "name": "alsa_input.pci.analog-stereo", "description": "Wbudowany mikrofon",
             "monitor_source": "", "properties": {"device.class": "sound"}},
        ]
        self.default_sink = ANALOG
        self.default_source = MIC
        self.inputs: dict[int, dict] = {
            300: _sink_input(300, 47, "firefox", "Firefox", "firefox", "YouTube"),
            301: _sink_input(301, 73, "firefox", "Firefox", "firefox", "Spotify Web"),
            302: _sink_input(302, 47, "spotify", "Spotify"),
            303: _sink_input(303, 47, None, "GNOME Shell", role="event"),
        }
        self.calls: list[list[str]] = []
        self.fail: dict[str, int] = {}     # argv[1] (or "load-module") -> remaining failures
        self.hang: Optional[str] = None     # argv[1] that never returns
        self.steal_default_on_load = False
        self._next_mod = 536870913
        self._next_sink = 400

    # --- helpers for tests
    def sink_of(self, idx: int) -> str:
        return self.sinks[self.inputs[idx]["sink"]]

    def sink_index(self, name: str) -> int:
        return next(i for i, n in self.sinks.items() if n == name)

    def airlinx_modules(self) -> list[int]:
        return [m for m, (n, a) in self.modules.items() if "airlinx_" in a]

    def add_input(self, idx: int, sink_name: str, binary: str, name: str) -> None:
        self.inputs[idx] = _sink_input(idx, self.sink_index(sink_name), binary, name)

    # --- runner
    async def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        await asyncio.sleep(0)
        if argv[0] != "pactl":
            key = argv[0]
        else:
            key = argv[4] if argv[1] == "-f" else argv[1]
        if self.hang == key:
            await asyncio.Event().wait()
        if self.fail.get(key, 0) > 0:
            self.fail[key] -= 1
            raise au.PactlError(f"{key} failed")
        if argv[0] == "pw-metadata":
            return ""
        a = argv[1:]
        if a[:3] == ["-f", "json", "list"]:
            what = a[3]
            if what == "sinks":
                return json.dumps([{"index": i, "name": n, "description": n, "owner_module": self.owner.get(i, 4294967295)}
                                   for i, n in self.sinks.items()])
            if what == "sources":
                return json.dumps(self.sources)
            if what == "sink-inputs":
                return json.dumps(list(self.inputs.values()))
        if a == ["list", "modules", "short"]:
            return "".join(f"{m}\t{n}\t{arg}\t\n" for m, (n, arg) in self.modules.items())
        if a == ["get-default-sink"]:
            return self.default_sink + "\n"
        if a == ["get-default-source"]:
            return self.default_source + "\n"
        if a[0] == "set-default-sink":
            self.default_sink = a[1]
            return ""
        if a[0] == "load-module":
            assert a[1] == "module-null-sink"
            sink_name = next(x.split("=", 1)[1] for x in a[2:] if x.startswith("sink_name="))
            mid = self._next_mod
            self._next_mod += 1
            sid = self._next_sink
            self._next_sink += 1
            self.modules[mid] = (a[1], " ".join(a[2:]))
            self.sinks[sid] = sink_name
            self.owner[sid] = mid
            if self.steal_default_on_load:
                self.default_sink = sink_name
            return f"{mid}\n"
        if a[0] == "unload-module":
            mid = int(a[1])
            if mid not in self.modules:
                raise au.PactlError("no such module")
            del self.modules[mid]
            for sid in [s for s, m in self.owner.items() if m == mid]:
                del self.owner[sid]
                del self.sinks[sid]
                if self.default_sink not in self.sinks.values():
                    self.default_sink = ANALOG
                for inp in self.inputs.values():  # the server rescues streams to the default sink
                    if inp["sink"] == sid:
                        inp["sink"] = self.sink_index(self.default_sink)
            return ""
        if a[0] == "move-sink-input":
            idx, target = int(a[1]), a[2]
            if idx not in self.inputs:
                raise au.PactlError("no such sink-input")
            if target == "@DEFAULT_SINK@":
                target = self.default_sink
            self.inputs[idx]["sink"] = self.sink_index(target)
            return ""
        raise AssertionError(f"unexpected pactl call {argv}")


class FakeSubscribe:
    """Stands in for `pactl subscribe`: tests push event lines."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.opened = 0

    async def __call__(self):
        self.opened += 1
        while True:
            line = await self.q.get()
            if line is None:
                return
            yield line


@pytest.fixture(autouse=True)
def _private_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


# ----------------------------------------------------------------------------- parsing
def test_parse_sink_inputs_keys_and_labels():
    pulse = FakePulse()
    items = au.parse_sink_inputs(json.dumps(list(pulse.inputs.values())))
    by = {i.index: i for i in items}
    assert by[300].app_key == "firefox" and by[300].app_name == "Firefox" and by[300].icon == "firefox"
    assert by[300].sink == 47 and by[300].object_id == 100
    assert by[303].app_key == "GNOME Shell" and by[303].role == "event"  # no binary → application.name


def test_parse_json_tolerates_decimal_commas():
    # pactl 15.99 prints locale decimals ("balance":0,00) when LC_NUMERIC is not C
    broken = '[{"index":5,"sink":47,"balance":0,00,"buffer_latency_usec":12,50,"properties":{"application.name":"x, y"}}]'
    items = au.parse_sink_inputs(broken)
    assert items[0].index == 5 and items[0].app_name == "x, y"


def test_parse_sources_excludes_monitors_marks_default():
    pulse = FakePulse()
    mics = au.parse_sources(json.dumps(pulse.sources), default_source=MIC)
    assert [m.name for m in mics] == [MIC, "alsa_input.pci.analog-stereo"]
    assert mics[0].label == "Blue Yeti Analog Stereo" and mics[0].is_default
    assert not mics[1].is_default


def test_list_devices_groups_playing_apps():
    pulse = FakePulse()
    res = run(au.list_devices(pulse, icon_lookup=lambda name: f"data:{name}" if name == "firefox" else None))
    apps = {a["key"]: a for a in res["apps"]}
    assert set(apps) == {"firefox", "spotify"}  # event sounds (GNOME Shell) are not "apps"
    assert apps["firefox"]["streams"] == 2 and apps["firefox"]["name"] == "Firefox"
    assert apps["firefox"]["icon_url"] == "data:firefox" and apps["spotify"]["icon_url"] is None
    assert res["mics"][0] == {"name": MIC, "label": "Blue Yeti Analog Stereo", "default": True}
    assert res["default_mic"] == "Blue Yeti Analog Stereo"


def test_list_devices_without_pactl_is_empty():
    async def broken(argv):
        raise au.PactlError("pactl missing")
    res = run(au.list_devices(broken))
    assert res == {"mics": [], "apps": [], "default_mic": None}


# ----------------------------------------------------------------------------- resolve / labels
def test_resolve_keeps_available_choice():
    pulse = FakePulse()
    spec, note = run(au.resolve(au.AudioSpec("mic", mic=MIC, mic_name="Blue Yeti"), pulse))
    assert spec.source == "mic" and note is None
    spec, note = run(au.resolve(au.AudioSpec("app", app="spotify", app_name="Spotify"), pulse))
    assert spec.source == "app" and note is None
    spec, note = run(au.resolve(au.AudioSpec("system_mic", mic="default"), pulse))
    assert spec.source == "system_mic" and note is None


def test_resolve_falls_back_to_system_when_app_silent():
    pulse = FakePulse()
    spec, note = run(au.resolve(au.AudioSpec("app", app="vlc", app_name="VLC"), pulse))
    assert spec.source == "system"
    assert note == "VLC nic teraz nie odtwarza. Wysyłam dźwięk systemu."


def test_resolve_falls_back_to_system_when_mic_missing():
    pulse = FakePulse()
    spec, note = run(au.resolve(au.AudioSpec("system_mic", mic="usb-gone", mic_name="Rode NT"), pulse))
    assert spec.source == "system" and note == "Mikrofon „Rode NT” jest niedostępny. Wysyłam dźwięk systemu."
    pulse.sources = [s for s in pulse.sources if s["properties"]["device.class"] == "monitor"]
    pulse.default_source = ANALOG + ".monitor"   # no mic at all: the default source is a monitor
    spec, note = run(au.resolve(au.AudioSpec("mic", mic="default"), pulse))
    assert spec.source == "system" and note == "Nie znaleziono mikrofonu. Wysyłam dźwięk systemu."


def test_audio_label():
    assert au.audio_label(au.AudioSpec("system")) == "Dźwięk systemu"
    assert au.audio_label(au.AudioSpec("mic", mic=MIC, mic_name="Blue Yeti")) == "Mikrofon: Blue Yeti"
    assert au.audio_label(au.AudioSpec("mic")) == "Mikrofon: domyślny"
    assert au.audio_label(au.AudioSpec("system_mic", mic_name="Blue Yeti")) == "System + mikrofon"
    assert au.audio_label(au.AudioSpec("app", app="firefox", app_name="Firefox")) == "Tylko Firefox"


# ----------------------------------------------------------------------------- pipeline sources
def test_pipeline_sources_for_each_mode():
    pulse = FakePulse()

    async def monitor():
        return ANALOG + ".monitor"

    async def go():
        out = {}
        for spec in (au.AudioSpec("system"), au.AudioSpec("mic"), au.AudioSpec("mic", mic=MIC),
                     au.AudioSpec("system_mic", mic=MIC)):
            p = au.AudioPipeline(spec, runner=pulse, monitor=monitor)
            out[(spec.source, spec.mic)] = await p.open()
            await p.close()
        p = au.AudioPipeline(au.AudioSpec("app", app="spotify"), runner=pulse, monitor=monitor,
                             subscribe=FakeSubscribe())
        out["app"] = await p.open()
        assert pulse.sink_of(302) == "airlinx_app"
        await p.close()
        return out

    out = run(go())
    assert out[("system", "default")] == [ANALOG + ".monitor"]
    assert out[("mic", "default")] == ["@DEFAULT_SOURCE@"]
    assert out[("mic", MIC)] == [MIC]
    assert out[("system_mic", MIC)] == [ANALOG + ".monitor", MIC]
    assert out["app"] == ["airlinx_app.monitor"]
    assert pulse.sink_of(302) == ANALOG and not pulse.airlinx_modules()


def test_pipeline_app_failure_falls_back_to_system():
    pulse = FakePulse()
    pulse.fail["load-module"] = 1
    notes = []

    async def monitor():
        return ANALOG + ".monitor"

    async def go():
        p = au.AudioPipeline(au.AudioSpec("app", app="spotify", app_name="Spotify"), runner=pulse, monitor=monitor,
                             subscribe=FakeSubscribe(), on_notice=notes.append)
        srcs = await p.open()
        await p.close()
        return srcs

    assert run(go()) == [ANALOG + ".monitor"]
    assert notes and "Spotify" in notes[0] and "systemu" in notes[0]
    assert not pulse.airlinx_modules() and pulse.sink_of(302) == ANALOG


# ----------------------------------------------------------------------------- app routing + cleanup
def _router(pulse, key="firefox", tmp_path=None, sub=None):
    return au.AppRouter(key, runner=pulse, subscribe=sub or FakeSubscribe(),
                        state_path=(tmp_path / "routes.json") if tmp_path else None)


def test_router_moves_only_that_app_and_restores_original_sinks(tmp_path):
    pulse = FakePulse()

    async def go():
        r = _router(pulse, tmp_path=tmp_path)
        await r.start()
        assert r.monitor == "airlinx_app.monitor"
        assert pulse.sink_of(300) == "airlinx_app" and pulse.sink_of(301) == "airlinx_app"
        assert pulse.sink_of(302) == ANALOG          # spotify untouched
        assert pulse.default_sink == ANALOG
        assert (tmp_path / "routes.json").exists()
        await r.stop()
        await r.stop()                               # idempotent

    run(go())
    assert pulse.sink_of(300) == ANALOG and pulse.sink_of(301) == HDMI
    assert not pulse.airlinx_modules() and "airlinx_app" not in pulse.sinks.values()
    assert not (tmp_path / "routes.json").exists()


def test_router_load_args_and_default_sink_guard():
    pulse = FakePulse()
    pulse.steal_default_on_load = True

    async def go():
        r = _router(pulse)
        await r.start()
        load = next(c for c in pulse.calls if "load-module" in c)
        assert "sink_name=airlinx_app" in load
        assert pulse.default_sink == ANALOG          # a freshly loaded null sink must not become the default
        await r.stop()

    run(go())


def test_router_unpins_streams_that_followed_the_default_sink():
    pulse = FakePulse()

    async def go():
        r = _router(pulse)
        await r.start()
        await r.stop()

    run(go())
    # 300 came from the default sink → its WirePlumber target pin is cleared; 301 (HDMI) keeps its pin
    unpins = [c for c in pulse.calls if c[0] == "pw-metadata"]
    assert any("100" in c for c in unpins) and not any("101" in c for c in unpins)


def test_router_moves_new_streams_of_the_app(tmp_path):
    pulse = FakePulse()
    sub = FakeSubscribe()

    async def go():
        r = _router(pulse, tmp_path=tmp_path, sub=sub)
        await r.start()
        pulse.add_input(310, HDMI, "firefox", "Firefox")     # new tab starts playing
        pulse.add_input(311, ANALOG, "vlc", "VLC")
        await sub.q.put("Event 'new' on sink-input #310")
        await sub.q.put("Event 'new' on sink-input #311")
        for _ in range(50):
            if pulse.sink_of(310) == "airlinx_app":
                break
            await asyncio.sleep(0.01)
        assert pulse.sink_of(310) == "airlinx_app" and pulse.sink_of(311) == ANALOG
        await r.stop()

    run(go())
    assert pulse.sink_of(310) == HDMI and not pulse.airlinx_modules()


def test_router_polls_when_subscribe_dies():
    pulse = FakePulse()
    sub = FakeSubscribe()

    async def go():
        r = au.AppRouter("firefox", runner=pulse, subscribe=sub, poll_interval=0.02)
        await r.start()
        await sub.q.put(None)                         # pactl subscribe exited
        pulse.add_input(320, ANALOG, "firefox", "Firefox")
        for _ in range(100):
            if pulse.sink_of(320) == "airlinx_app":
                break
            await asyncio.sleep(0.01)
        assert pulse.sink_of(320) == "airlinx_app"
        await r.stop()

    run(go())
    assert pulse.sink_of(320) == ANALOG


def test_router_restores_even_when_a_move_fails():
    pulse = FakePulse()

    async def go():
        r = _router(pulse)
        await r.start()
        pulse.fail["move-sink-input"] = 1             # first move back fails
        await r.stop()

    run(go())
    # the failing stream is rescued by the module unload (default sink), the other one got its sink back
    assert {pulse.sink_of(300), pulse.sink_of(301)} <= {ANALOG, HDMI}
    assert not pulse.airlinx_modules()


def test_router_original_sink_vanished_goes_to_default():
    pulse = FakePulse()

    async def go():
        r = _router(pulse)
        await r.start()
        del pulse.sinks[73]                           # HDMI unplugged meanwhile
        await r.stop()

    run(go())
    assert pulse.inputs[301]["sink"] == 47 and not pulse.airlinx_modules()


def test_router_start_failure_cleans_up():
    pulse = FakePulse()
    pulse.fail["move-sink-input"] = 99

    async def go():
        r = _router(pulse)
        with pytest.raises(au.PactlError):
            await r.start()

    run(go())
    assert not pulse.airlinx_modules()
    assert pulse.sink_of(300) == ANALOG and pulse.sink_of(301) == HDMI


def test_router_cancelled_start_cleans_up():
    pulse = FakePulse()

    async def go():
        r = _router(pulse)
        pulse.hang = "sink-inputs"
        t = asyncio.ensure_future(r.start())
        for _ in range(20):
            await asyncio.sleep(0)
        assert pulse.airlinx_modules()                # null sink already loaded
        pulse.hang = None
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    run(go())
    assert not pulse.airlinx_modules()


def test_cleanup_orphans_after_crash_uses_saved_routes(tmp_path):
    pulse = FakePulse()

    async def go():
        r = _router(pulse, tmp_path=tmp_path)
        await r.start()
        r._watch_task.cancel()                        # simulate a crash: no stop()
        n = await au.cleanup_orphans(pulse, state_path=tmp_path / "routes.json")
        return n

    assert run(go()) == 1
    assert pulse.sink_of(300) == ANALOG and pulse.sink_of(301) == HDMI
    assert not pulse.airlinx_modules() and not (tmp_path / "routes.json").exists()


def test_cleanup_orphans_without_state_moves_to_default(tmp_path):
    pulse = FakePulse()
    pulse.modules[7] = ("module-null-sink", "sink_name=airlinx_app sink_properties=x")
    pulse.sinks[90] = "airlinx_app"
    pulse.owner[90] = 7
    pulse.modules[8] = ("module-null-sink", "sink_name=someone_else")
    pulse.inputs[300]["sink"] = 90

    n = run(au.cleanup_orphans(pulse, state_path=tmp_path / "missing.json"))
    assert n == 1 and 7 not in pulse.modules and 8 in pulse.modules
    assert pulse.sink_of(300) == ANALOG


def test_cleanup_orphans_noop_when_clean(tmp_path):
    pulse = FakePulse()
    assert run(au.cleanup_orphans(pulse, state_path=tmp_path / "r.json")) == 0
    assert not any(c[1] in ("unload-module", "move-sink-input") for c in pulse.calls if c[0] == "pactl")


def test_router_evicts_other_apps_restored_into_our_sink():
    # WirePlumber remembers "target=airlinx_app" for streams that ended while routed; when the sink
    # exists again, a *different* app's new stream may land in it → send it back to the default sink
    pulse = FakePulse()
    sub = FakeSubscribe()

    async def go():
        r = _router(pulse, key="spotify", sub=sub)
        await r.start()
        pulse.inputs[330] = _sink_input(330, r.sink_index, "firefox", "Firefox", object_id=130)
        await sub.q.put("Event 'new' on sink-input #330")
        for _ in range(50):
            if pulse.sink_of(330) != "airlinx_app":
                break
            await asyncio.sleep(0.01)
        assert pulse.sink_of(330) == ANALOG and pulse.sink_of(302) == "airlinx_app"
        assert ["pw-metadata", "-d", "130", "target.node"] in pulse.calls
        await r.stop()

    run(go())
