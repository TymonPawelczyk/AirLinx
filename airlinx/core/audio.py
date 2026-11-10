"""Which sound goes to the TV: system output, a microphone, both mixed, or one application.

Everything here talks to the sound server through ``pactl`` (PulseAudio or pipewire-pulse),
via an injectable *runner* ``async (argv) -> stdout`` so tests can use a fake server.

Modes (``AudioSpec.source``):
  * ``system``      monitor of the default sink (previous behaviour)
  * ``mic``         one input device (``@DEFAULT_SOURCE@`` for "default"), never a ``*.monitor``
  * ``system_mic``  both, mixed by ffmpeg (``amix``) into one stream — see ``capture.build_audio_cmd``
  * ``app``         only one application: a temporary null sink ``airlinx_app`` gets that app's
                    sink-inputs (moved with ``move-sink-input``) and we capture ``airlinx_app.monitor``.
                    The app is silent on the PC speakers meanwhile.

Cleanup is the critical part of ``app`` mode. ``AppRouter.stop()`` moves every stream back to its
original sink and unloads the module, also after errors and cancellation; the routes are written
to a small state file so ``cleanup_orphans()`` (run at app start) can undo a crashed session.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

log = logging.getLogger("airlinx.audio")

SOURCES = ("system", "mic", "system_mic", "app")
DEFAULT_MIC = "default"
DEFAULT_SOURCE = "@DEFAULT_SOURCE@"
PREFIX = "airlinx_"
APP_SINK = "airlinx_app"
APP_SINK_DESC = "AirLinx TV"
POLL_INTERVAL = 2.0
PACTL_TIMEOUT = 4.0

Runner = Callable[[list[str]], Awaitable[str]]
Subscribe = Callable[[], AsyncIterator[str]]


class PactlError(Exception):
    """pactl (or pw-metadata) failed or is missing."""


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------
async def pactl(argv: list[str], timeout: float = PACTL_TIMEOUT) -> str:
    """Run ``argv`` (``pactl …`` / ``pw-metadata …``) with a C locale (pactl 15.99 JSON uses locale
    decimal commas otherwise). Raises PactlError on a missing tool, timeout or non-zero exit."""
    if not shutil.which(argv[0]):
        raise PactlError(f"{argv[0]} not found")
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                 env=env)
    except OSError as e:
        raise PactlError(str(e)) from e
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError as e:
        with contextlib.suppress(ProcessLookupError):
            p.kill()
        with contextlib.suppress(Exception):
            await p.wait()
        raise PactlError(f"{' '.join(argv[:3])}: timeout") from e
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            p.kill()
        raise
    if p.returncode != 0:
        raise PactlError(f"{' '.join(argv[:3])}: {err.decode('utf-8', 'replace').strip() or p.returncode}")
    return out.decode("utf-8", "replace")


async def pactl_subscribe() -> AsyncIterator[str]:
    """Lines of ``pactl subscribe`` until it exits."""
    if not shutil.which("pactl"):
        return
    p = await asyncio.create_subprocess_exec("pactl", "subscribe", stdin=asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                             env=dict(os.environ, LC_ALL="C", LANG="C"))
    try:
        assert p.stdout is not None
        async for line in p.stdout:
            yield line.decode("utf-8", "replace").strip()
    finally:
        if p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            with contextlib.suppress(Exception):
                await p.wait()


# --------------------------------------------------------------------------------------
# Parsing (pure)
# --------------------------------------------------------------------------------------
_COMMA_DECIMAL = re.compile(r":(-?\d+),(\d+)(?=[,}\]])")


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # pactl 15.99 with a non-C LC_NUMERIC prints "balance":0,00 — repair outside strings only
        out, buf, in_str, esc = [], [], False, False
        for ch in text:
            if in_str:
                out.append(ch)
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                out.append(_COMMA_DECIMAL.sub(r":\1.\2", "".join(buf)))
                buf = []
                out.append(ch)
                in_str = True
            else:
                buf.append(ch)
        out.append(_COMMA_DECIMAL.sub(r":\1.\2", "".join(buf)))
        return json.loads("".join(out))


def _list(text: str) -> list[dict]:
    data = _loads(text) if text.strip() else []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class SinkInput:
    index: int
    sink: Optional[int]
    app_key: Optional[str]
    app_name: Optional[str]
    icon: Optional[str]
    media_name: Optional[str]
    role: Optional[str]
    object_id: Optional[int]
    corked: bool = False


def app_key(props: dict) -> Optional[str]:
    """Stable identity of an application: process binary, else application.name."""
    for k in ("application.process.binary", "application.name"):
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def parse_sink_inputs(text: str) -> list[SinkInput]:
    out = []
    for d in _list(text):
        props = d.get("properties") or {}
        idx = _int(d.get("index"))
        if idx is None:
            continue
        key = app_key(props)
        out.append(SinkInput(index=idx, sink=_int(d.get("sink")), app_key=key,
                             app_name=props.get("application.name") or key, icon=props.get("application.icon_name"),
                             media_name=props.get("media.name"), role=props.get("media.role"),
                             object_id=_int(props.get("object.id")), corked=bool(d.get("corked"))))
    return out


@dataclass
class MicInfo:
    name: str
    label: str
    is_default: bool = False


def _is_monitor(d: dict) -> bool:
    props = d.get("properties") or {}
    name = str(d.get("name") or "")
    mon = d.get("monitor_source") or d.get("monitor_of_sink")
    return (props.get("device.class") == "monitor" or name.endswith(".monitor")
            or (isinstance(mon, str) and mon not in ("", "n/a")))


def parse_sources(text: str, default_source: Optional[str] = None) -> list[MicInfo]:
    out = []
    for d in _list(text):
        name = d.get("name")
        if not isinstance(name, str) or not name or _is_monitor(d) or name.startswith(PREFIX):
            continue
        out.append(MicInfo(name=name, label=str(d.get("description") or name), is_default=name == default_source))
    return out


def _sinks(text: str) -> dict[int, str]:
    return {i: str(d.get("name")) for d in _list(text) if (i := _int(d.get("index"))) is not None}


def _modules_short(text: str) -> list[tuple[int, str, str]]:
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and (mid := _int(parts[0])) is not None:
            out.append((mid, parts[1], parts[2] if len(parts) > 2 else ""))
    return out


# --------------------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------------------
async def _json(runner: Runner, what: str) -> str:
    return await runner(["pactl", "-f", "json", "list", what])


async def _default(runner: Runner, kind: str) -> Optional[str]:
    try:
        v = (await runner(["pactl", f"get-default-{kind}"])).strip()
    except PactlError:
        return None
    return v or None


async def sink_inputs(runner: Runner) -> list[SinkInput]:
    return parse_sink_inputs(await _json(runner, "sink-inputs"))


async def microphones(runner: Runner) -> list[MicInfo]:
    default = await _default(runner, "source")
    return parse_sources(await _json(runner, "sources"), default)


def _playing_apps(inputs: list[SinkInput]) -> dict[str, dict]:
    apps: dict[str, dict] = {}
    for si in inputs:
        if not si.app_key or si.role == "event":   # system/event sounds are not "an application playing"
            continue
        a = apps.setdefault(si.app_key, {"key": si.app_key, "name": si.app_name or si.app_key,
                                         "icon": si.icon, "streams": 0})
        a["streams"] += 1
        a["icon"] = a["icon"] or si.icon
    return apps


async def list_devices(runner: Runner = pactl,
                       icon_lookup: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    """Microphones and the applications that are playing right now (for the settings UI)."""
    lookup = icon_lookup or icon_data_url
    try:
        mics = await microphones(runner)
    except (PactlError, ValueError) as e:
        log.warning("listing microphones failed: %s", e)
        mics = []
    try:
        apps = list(_playing_apps(await sink_inputs(runner)).values())
    except (PactlError, ValueError) as e:
        log.warning("listing sink-inputs failed: %s", e)
        apps = []
    for a in apps:
        a["icon_url"] = (lookup(a["icon"]) if a["icon"] else None) or lookup(a["key"].lower())
    apps.sort(key=lambda a: a["name"].lower())
    default = next((m for m in mics if m.is_default), None)
    return {"mics": [{"name": m.name, "label": m.label, "default": m.is_default} for m in mics],
            "apps": apps, "default_mic": default.label if default else None}


# --------------------------------------------------------------------------------------
# Spec, resolution, labels
# --------------------------------------------------------------------------------------
@dataclass
class AudioSpec:
    source: str = "system"
    mic: str = DEFAULT_MIC
    mic_name: Optional[str] = None
    app: Optional[str] = None
    app_name: Optional[str] = None

    def key(self) -> tuple:
        """What actually changes the capture (labels excluded)."""
        if self.source == "system":
            return ("system",)
        if self.source == "app":
            return ("app", self.app)
        return (self.source, self.mic or DEFAULT_MIC)


def spec_from(obj: Any) -> AudioSpec:
    """AudioSpec from MirrorOptions (audio_* fields) or a settings ``audio`` dict."""
    if isinstance(obj, dict):
        g = obj.get
        src, mic, app = g("source"), g("mic"), g("app")
        mic_name, app_name = g("mic_name"), g("app_name")
    else:
        src, mic, app = (getattr(obj, "audio_source", None), getattr(obj, "audio_mic", None),
                         getattr(obj, "audio_app", None))
        mic_name, app_name = getattr(obj, "audio_mic_name", None), getattr(obj, "audio_app_name", None)
    if src not in SOURCES or (src == "app" and not app):
        src = "system"
    return AudioSpec(source=src, mic=mic or DEFAULT_MIC, mic_name=mic_name, app=app, app_name=app_name)


def _app_label(spec: AudioSpec) -> str:
    return spec.app_name or spec.app or "aplikacja"


def audio_label(spec: AudioSpec) -> str:
    if spec.source == "mic":
        return f"Mikrofon: {spec.mic_name or ('domyślny' if spec.mic == DEFAULT_MIC else spec.mic)}"
    if spec.source == "system_mic":
        return "System + mikrofon"
    if spec.source == "app":
        return f"Tylko {_app_label(spec)}"
    return "Dźwięk systemu"


def _fallback(spec: AudioSpec) -> AudioSpec:
    return AudioSpec("system", mic=spec.mic, mic_name=spec.mic_name, app=spec.app, app_name=spec.app_name)


async def resolve(spec: AudioSpec, runner: Runner = pactl) -> tuple[AudioSpec, Optional[str]]:
    """Check the chosen mic/app is there. Returns (effective spec, Polish note or None)."""
    if spec.source in ("mic", "system_mic"):
        try:
            mics = await microphones(runner)
            default = await _default(runner, "source")
        except (PactlError, ValueError) as e:
            log.warning("cannot list microphones: %s", e)
            return spec, None   # cannot tell; let the capture try
        if spec.mic == DEFAULT_MIC:
            ok = bool(default) and any(m.name == default for m in mics)
            if not ok:
                return _fallback(spec), "Nie znaleziono mikrofonu. Wysyłam dźwięk systemu."
        elif not any(m.name == spec.mic for m in mics):
            name = spec.mic_name or spec.mic
            return _fallback(spec), f"Mikrofon „{name}” jest niedostępny. Wysyłam dźwięk systemu."
        return spec, None
    if spec.source == "app":
        try:
            playing = _playing_apps(await sink_inputs(runner))
        except (PactlError, ValueError) as e:
            log.warning("cannot list sink-inputs: %s", e)
            return spec, None
        if spec.app not in playing:
            return _fallback(spec), f"{_app_label(spec)} nic teraz nie odtwarza. Wysyłam dźwięk systemu."
        return spec, None
    return spec, None


# --------------------------------------------------------------------------------------
# Route state file (crash recovery)
# --------------------------------------------------------------------------------------
def default_state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "airlinx" / "audio-routes.json"


def _read_state(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Optional[Path], data: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("cannot write %s: %s", path, e)


def _drop_state(path: Optional[Path]) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


# --------------------------------------------------------------------------------------
# App routing
# --------------------------------------------------------------------------------------
async def _shielded(coro: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await task
        raise


async def _restore_streams(runner: Runner, our_sinks: set[int], origins: dict[int, str],
                           pinned: dict[int, bool]) -> None:
    """Move every stream sitting on one of ``our_sinks`` back to its origin (or the default sink)."""
    try:
        sinks = _sinks(await _json(runner, "sinks"))
        inputs = await sink_inputs(runner)
    except (PactlError, ValueError) as e:
        log.warning("restore: cannot list streams: %s", e)
        return
    default = await _default(runner, "sink")
    for si in inputs:
        if si.sink not in our_sinks:
            continue
        orig = origins.get(si.index)
        target = orig if orig in sinks.values() and not str(orig).startswith(PREFIX) else None
        try:
            await runner(["pactl", "move-sink-input", str(si.index), target or "@DEFAULT_SINK@"])
            log.info("restored sink-input #%d (%s) -> %s", si.index, si.app_key, target or "default sink")
        except PactlError as e:
            log.warning("restore of sink-input #%d failed: %s", si.index, e)
            continue
        # moving pins the stream (WirePlumber restore-stream saves target.node); a stream that came
        # from the default sink should follow the default again → clear the pin
        if target is None or (target == default and not pinned.get(si.index)):
            await _unpin(runner, si)


async def _unpin(runner: Runner, si: SinkInput) -> None:
    """Drop the stream's target pin (PipeWire metadata) so WirePlumber forgets the saved target
    and the app follows the default sink again. Best effort; no-op on plain PulseAudio."""
    if si.object_id is None:
        return
    for key in ("target.node", "target.object"):
        with contextlib.suppress(PactlError):
            await runner(["pw-metadata", "-d", str(si.object_id), key])


class AppRouter:
    """Route one application's streams into a private null sink for capturing."""

    def __init__(self, key: str, *, runner: Runner = pactl, subscribe: Optional[Subscribe] = None,
                 state_path: Optional[Path] = None, poll_interval: float = POLL_INTERVAL,
                 sink_name: str = APP_SINK):
        self.key = key
        self.runner = runner
        self.subscribe = subscribe or pactl_subscribe
        self.state_path = state_path
        self.poll_interval = poll_interval
        self.sink_name = sink_name
        self.module: Optional[int] = None
        self.sink_index: Optional[int] = None
        self.origins: dict[int, str] = {}     # sink-input index -> original sink name
        self.pinned: dict[int, bool] = {}     # original sink was not the default one
        self.moved: set[int] = set()
        self._lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self._active = False

    @property
    def monitor(self) -> str:
        return f"{self.sink_name}.monitor"

    async def start(self) -> None:
        async with self._lock:
            try:
                await self._start()
            except BaseException:
                await _shielded(self._stop())
                raise

    async def _start(self) -> None:
        r = self.runner
        default = await _default(r, "sink")
        self._active = True
        out = await r(["pactl", "load-module", "module-null-sink", f"sink_name={self.sink_name}",
                       f"sink_properties=device.description=\"{APP_SINK_DESC}\""])
        self.module = _int(out.strip().splitlines()[-1] if out.strip() else None)
        self._save()
        if default and await _default(r, "sink") != default:
            log.info("null sink became the default sink; restoring %s", default)
            await r(["pactl", "set-default-sink", default])
        await self._reconcile(only=None)
        self._watch_task = asyncio.create_task(self._watch(), name="audio-app-watch")
        log.info("app audio routing on: %s -> %s (module %s, %d stream(s))", self.key, self.sink_name,
                 self.module, len(self.moved))

    def _save(self) -> None:
        _write_state(self.state_path, {"sink": self.sink_name, "module": self.module,
                                       "origins": {str(k): v for k, v in self.origins.items()},
                                       "pinned": [k for k, v in self.pinned.items() if v]})

    async def _reconcile(self, only: Optional[int]) -> None:
        """Move this app's streams (all, or just sink-input ``only``) into our sink."""
        r = self.runner
        sinks = _sinks(await _json(r, "sinks"))
        ours = next((i for i, n in sinks.items() if n == self.sink_name), None)
        if ours is None:
            raise PactlError(f"{self.sink_name} vanished")
        self.sink_index = ours
        default = await _default(r, "sink")
        for si in await sink_inputs(r):
            if only is not None and si.index != only:
                continue
            if si.sink == ours and si.app_key != self.key:
                await self._evict(si)
                continue
            if si.app_key != self.key or si.sink == ours:
                continue
            if si.index in self.moved and only is None:
                continue  # the user moved it away on purpose; don't fight
            orig = sinks.get(si.sink) if si.sink is not None else None
            if orig:
                self.origins[si.index] = orig
                self.pinned[si.index] = orig != default
            self.moved.add(si.index)
            self._save()
            await r(["pactl", "move-sink-input", str(si.index), self.sink_name])
            log.info("moved sink-input #%d (%s) from %s", si.index, si.app_key, orig)

    async def _evict(self, si: SinkInput) -> None:
        """Another app's stream landed in our sink (a stale WirePlumber target from an earlier session)."""
        log.info("sink-input #%d (%s) is not ours; back to the default sink", si.index, si.app_key)
        await self.runner(["pactl", "move-sink-input", str(si.index), "@DEFAULT_SINK@"])
        await _unpin(self.runner, si)

    async def _watch(self) -> None:
        """Pick up new streams of the app (new browser tab…): pactl subscribe, polling as fallback."""
        try:
            async for line in self.subscribe():
                m = re.search(r"'new' on sink-input #(\d+)", line)
                if m:
                    try:
                        await self._reconcile(only=int(m.group(1)))
                    except (PactlError, ValueError) as e:
                        log.debug("reconcile after event failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("pactl subscribe failed: %s", e)
        log.info("pactl subscribe ended; polling for new streams every %.1fs", self.poll_interval)
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self._reconcile(only=None)
            except (PactlError, ValueError) as e:
                log.debug("poll reconcile failed: %s", e)

    async def stop(self) -> None:
        async with self._lock:
            await _shielded(self._stop())

    async def _stop(self) -> None:
        t, self._watch_task = self._watch_task, None
        if t is not None and t is not asyncio.current_task():
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        if not self._active:
            return
        r = self.runner
        sinks: dict[int, str] = {}
        with contextlib.suppress(PactlError, ValueError):
            sinks = _sinks(await _json(r, "sinks"))
        ours = {i for i, n in sinks.items() if n == self.sink_name}
        if self.sink_index is not None:
            ours.add(self.sink_index)
        await _restore_streams(r, ours, self.origins, self.pinned)
        await _unload_ours(r, self.module, self.sink_name)
        self._active = False
        self.module = None
        self.origins.clear()
        self.pinned.clear()
        self.moved.clear()
        _drop_state(self.state_path)
        log.info("app audio routing off: %s", self.key)


async def _unload_ours(runner: Runner, module: Optional[int], sink_name: Optional[str] = None) -> int:
    """Unload ``module`` and any module-null-sink whose sink_name starts with airlinx_ (or == sink_name)."""
    ids: set[int] = set()
    if module is not None:
        ids.add(module)
    try:
        for mid, name, arg in _modules_short(await runner(["pactl", "list", "modules", "short"])):
            m = re.search(r"sink_name=(\S+)", arg)
            if name == "module-null-sink" and m and (m.group(1).startswith(PREFIX) or m.group(1) == sink_name):
                ids.add(mid)
    except PactlError as e:
        log.warning("cannot list modules: %s", e)
    n = 0
    for mid in sorted(ids):
        try:
            await runner(["pactl", "unload-module", str(mid)])
            n += 1
        except PactlError as e:
            log.debug("unload-module %d: %s", mid, e)
    return n


async def cleanup_orphans(runner: Runner = pactl, state_path: Optional[Path] = None) -> int:
    """At start-up: undo routing left behind by a crashed AirLinx. Returns how many modules were unloaded."""
    path = state_path if state_path is not None else default_state_path()
    state = _read_state(path)
    try:
        sinks = _sinks(await _json(runner, "sinks"))
        mods = [(mid, arg) for mid, name, arg in _modules_short(await runner(["pactl", "list", "modules", "short"]))
                if name == "module-null-sink" and re.search(rf"sink_name={PREFIX}\S*", arg)]
    except (PactlError, ValueError) as e:
        log.debug("orphan cleanup skipped: %s", e)
        return 0
    ours = {i for i, n in sinks.items() if n.startswith(PREFIX)}
    if not ours and not mods:
        _drop_state(path)
        return 0
    origins = {int(k): v for k, v in (state.get("origins") or {}).items() if str(k).isdigit()}
    pinned = {int(k): True for k in state.get("pinned") or [] if str(k).isdigit()}
    log.warning("cleaning up audio routing left by a previous run (%d sink(s))", len(ours))
    await _restore_streams(runner, ours, origins, pinned)
    n = await _unload_ours(runner, None)
    _drop_state(path)
    return n


# --------------------------------------------------------------------------------------
# Pipeline: spec → pulse source names for ffmpeg (+ routing lifecycle)
# --------------------------------------------------------------------------------------
class AudioPipeline:
    """Owns everything one audio capture needs besides ffmpeg. ``open()`` → pulse sources;
    ``close()`` undoes routing (idempotent, safe after errors)."""

    def __init__(self, spec: AudioSpec, *, runner: Runner = pactl,
                 monitor: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
                 subscribe: Optional[Subscribe] = None, state_path: Optional[Path] = None,
                 on_notice: Optional[Callable[[str], None]] = None):
        self.spec = spec
        self.runner = runner
        self.monitor = monitor
        self.subscribe = subscribe
        self.state_path = state_path if state_path is not None else default_state_path()
        self.on_notice = on_notice
        self.router: Optional[AppRouter] = None
        self.effective = spec.source

    async def _system(self) -> list[str]:
        if self.monitor is not None:
            mon = await self.monitor()
        else:
            sink = await _default(self.runner, "sink")
            mon = f"{sink}.monitor" if sink else None
        return [mon] if mon else []

    def _mic(self) -> str:
        return DEFAULT_SOURCE if (self.spec.mic or DEFAULT_MIC) == DEFAULT_MIC else self.spec.mic

    async def open(self) -> list[str]:
        s = self.spec
        if s.source == "mic":
            return [self._mic()]
        if s.source == "system_mic":
            return await self._system() + [self._mic()]
        if s.source == "app" and s.app:
            router = AppRouter(s.app, runner=self.runner, subscribe=self.subscribe, state_path=self.state_path)
            self.router = router
            try:
                await router.start()
                return [router.monitor]
            except asyncio.CancelledError:
                self.router = None
                raise
            except Exception as e:  # noqa: BLE001
                self.router = None
                log.warning("app audio routing failed (%s); using system sound", e)
                self.effective = "system"
                self._notice(f"Nie udało się przechwycić dźwięku: {_app_label(s)}. Wysyłam dźwięk systemu.")
        return await self._system()

    def _notice(self, msg: str) -> None:
        if self.on_notice:
            try:
                self.on_notice(msg)
            except Exception:  # noqa: BLE001
                log.exception("on_notice failed")

    async def close(self) -> None:
        router, self.router = self.router, None
        if router is not None:
            await router.stop()


# --------------------------------------------------------------------------------------
# Icons (theme name → data URL; core must not import Gtk)
# --------------------------------------------------------------------------------------
_ICON_CACHE: dict[str, Optional[str]] = {}
_ICON_SIZES = ("48x48", "64x64", "scalable", "128x128", "256x256", "32x32", "96x96", "24x24")


def _icon_dirs() -> list[Path]:
    home = Path.home()
    data = [home / ".local/share"] + [Path(p) for p in (os.environ.get("XDG_DATA_DIRS") or
                                                        "/usr/local/share:/usr/share").split(":") if p]
    data += [Path("/var/lib/flatpak/exports/share"), home / ".local/share/flatpak/exports/share"]
    out = []
    for d in data:
        for theme in ("hicolor", "Adwaita", "ZorinBlue-Dark", "ZorinBlue-Light"):
            for size in _ICON_SIZES:
                out.append(d / "icons" / theme / size / "apps")
    out += [Path("/usr/share/pixmaps"), Path("/var/lib/snapd/desktop/icons")]
    return out


def icon_data_url(name: str) -> Optional[str]:
    """Find an icon by theme name and return it as a data URL (PNG/SVG ≤ 256 KiB), cached."""
    if not name or "/" in name or name.startswith("."):
        return None
    if name in _ICON_CACHE:
        return _ICON_CACHE[name]
    url = None
    for d in _icon_dirs():
        for ext, mime in ((".svg", "image/svg+xml"), (".png", "image/png")):
            p = d / f"{name}{ext}"
            try:
                if p.is_file() and p.stat().st_size <= 256 * 1024:
                    url = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
                    break
            except OSError:
                continue
        if url:
            break
    _ICON_CACHE[name] = url
    return url
