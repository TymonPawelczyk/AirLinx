"""Bridge dispatch without GTK/WebKit: injected run_coro / call_in_gtk / eval_js."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re

from airlinx.bridge import Bridge, _js_literal, error_payload
from airlinx.core.models import AirlinxError, Device, MediaItem, TvBusyError


class FakeController:
    def __init__(self):
        self.listeners = []

    def subscribe(self, fn):
        self.listeners.append(fn)

    async def dispatch(self, method, params):
        if method == "devices.list":
            return [Device("d1", "Salon", "S85F", "192.0.2.10", 8009, None, {"dlna", "cast_media"})]
        if method == "echo":
            return params
        if method == "busy":
            raise TvBusyError("YouTube")
        if method == "crash":
            raise RuntimeError("secret internals")
        raise AirlinxError("unknown_method", f"Nieznana metoda: {method}")


def make():
    scripts = []

    def run_coro(coro):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    ctl = FakeController()
    b = Bridge(ctl, run_coro, lambda fn, *a: fn(*a), scripts.append)
    return b, ctl, scripts


def parse_resolve(script):
    m = re.fullmatch(r"window\.airlinx&&window\.airlinx\._resolve\((.*)\);", script, re.S)
    assert m, script
    return json.loads("[" + m.group(1) + "]")


def test_resolve_result_serialized():
    b, _, scripts = make()
    b.handle_message(json.dumps({"id": 7, "method": "devices.list", "params": {}}))
    cid, result, err = parse_resolve(scripts[-1])
    assert cid == 7 and err is None
    assert result[0]["name"] == "Salon" and result[0]["caps"] == ["cast_media", "dlna"]


def test_dict_message_and_params_passthrough():
    b, _, scripts = make()
    b.handle_message({"id": "a", "method": "echo", "params": {"x": "zażółć "}})
    assert "\\u2028" in scripts[-1]
    _, result, _ = parse_resolve(scripts[-1])
    assert result == {"x": "zażółć "}


def test_airlinx_error_mapped():
    b, _, scripts = make()
    b.handle_message({"id": 1, "method": "busy"})
    _, result, err = parse_resolve(scripts[-1])
    assert result is None and err["code"] == "tv_busy" and "YouTube" in err["message"]


def test_unexpected_error_hidden():
    b, _, scripts = make()
    b.handle_message({"id": 2, "method": "crash"})
    _, _, err = parse_resolve(scripts[-1])
    assert err == {"code": "internal", "message": "Wystąpił nieoczekiwany błąd."}


def test_bad_messages_ignored_or_rejected():
    b, _, scripts = make()
    b.handle_message("not json")
    b.handle_message(json.dumps([1, 2]))
    assert scripts == []
    b.handle_message({"id": 3})
    _, _, err = parse_resolve(scripts[-1])
    assert err["code"] == "bad_request"


def test_local_handler():
    b, _, scripts = make()
    b.local_handlers["window.minimize"] = lambda p: True
    b.handle_message({"id": 4, "method": "window.minimize"})
    assert parse_resolve(scripts[-1]) == [4, True, None]


def test_emit_event_with_bytes():
    b, ctl, scripts = make()
    item = MediaItem("i1", "/x.mp4", "x", 1.0, "video", thumbnail_png=b"\x89PNG")
    ctl.listeners[0]("media.queue", [item])
    m = re.fullmatch(r"window\.airlinx&&window\.airlinx\._emit\((.*)\);", scripts[-1], re.S)
    ev, payload = json.loads("[" + m.group(1) + "]")
    assert ev == "media.queue" and payload[0]["thumbnail_png"].startswith("data:image/png;base64,")


def test_error_payload_type_error():
    assert error_payload(TypeError("x"))["code"] == "bad_params"
    assert _js_literal(None) == "null"
