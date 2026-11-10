"""JS ⇄ Python RPC over a WebKit2 4.1 script message handler named "airlinx".

JS → Py : window.webkit.messageHandlers.airlinx.postMessage({id, method, params})
Py → JS : window.airlinx._resolve(id, result|null, error|null)
          window.airlinx._emit(event, payload)
Errors  : {code, message} (AirlinxError.code / message_pl).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
from typing import Any, Callable, Optional

from .core.models import AirlinxError, to_json

log = logging.getLogger(__name__)

HANDLER_NAME = "airlinx"
QUIET_METHODS = {"app.getState", "sources.list", "devices.list", "settings.get"}


def _js_literal(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    # JSON is a JS subset except for these two line separators.
    return s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def error_payload(exc: BaseException) -> dict:
    if isinstance(exc, AirlinxError):
        return {"code": exc.code, "message": exc.message_pl}
    if isinstance(exc, TypeError):
        return {"code": "bad_params", "message": "Nieprawidłowe parametry wywołania."}
    return {"code": "internal", "message": "Wystąpił nieoczekiwany błąd."}


class Bridge:
    """Transport-agnostic core + optional WebKit attachment.

    `run_coro(coro) -> concurrent.futures.Future`, `call_in_gtk(fn, *a)` and
    `eval_js(script)` are injectable so dispatch can be tested without GTK.
    """

    def __init__(self, controller: Any, run_coro: Callable[[Any], concurrent.futures.Future],
                 call_in_gtk: Callable[..., None], eval_js: Optional[Callable[[str], None]] = None,
                 debug: bool = False):
        self.controller = controller
        self.run_coro = run_coro
        self.call_in_gtk = call_in_gtk
        self.eval_js = eval_js or (lambda script: None)
        self.debug = debug
        self.webview: Any = None
        self.local_handlers: dict[str, Callable[[dict], Any]] = {}
        controller.subscribe(self.emit)

    # ------------------------------------------------------------------ WebKit
    def attach(self, webview: Any) -> None:
        """Register the "airlinx" handler on the webview's UserContentManager."""
        self.webview = webview
        ucm = webview.get_user_content_manager()
        ucm.connect(f"script-message-received::{HANDLER_NAME}", self._on_script_message)
        if not ucm.register_script_message_handler(HANDLER_NAME):
            log.error("failed to register script message handler %r", HANDLER_NAME)

        def _eval(script: str) -> None:
            wv = self.webview
            if wv is None:
                return
            if hasattr(wv, "evaluate_javascript"):
                wv.evaluate_javascript(script, -1, None, None, None, None, None)
            else:  # pragma: no cover - older WebKit
                wv.run_javascript(script, None, None, None)

        self.eval_js = _eval

    def _on_script_message(self, _ucm: Any, js_result: Any) -> None:
        try:
            value = js_result.get_js_value() if hasattr(js_result, "get_js_value") else js_result
            raw = value.to_json(0)
        except Exception:
            log.exception("cannot decode script message")
            return
        self.handle_message(raw)

    # ------------------------------------------------------------------ RPC
    def handle_message(self, raw: Any) -> None:
        """Entry point for a message from JS (JSON string or already-decoded dict)."""
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(msg, dict):
                raise ValueError("message is not an object")
        except Exception as e:
            log.warning("bad bridge message %r: %s", raw, e)
            return
        call_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(method, str):
            self._resolve(call_id, None, {"code": "bad_request", "message": "Brak nazwy metody."})
            return
        if self.debug and method not in QUIET_METHODS:
            log.debug("rpc → %s %s", method, params)

        local = self.local_handlers.get(method)
        if local is not None:  # GTK-thread handlers (rare)
            try:
                self._resolve(call_id, to_json(local(params)), None)
            except Exception as e:
                self._fail(call_id, method, e)
            return

        try:
            fut = self.run_coro(self.controller.dispatch(method, params))
        except Exception as e:
            self._fail(call_id, method, e)
            return
        fut.add_done_callback(lambda f: self._done(call_id, method, f))

    def _done(self, call_id: Any, method: str, fut: concurrent.futures.Future) -> None:
        try:
            result = fut.result()
        except BaseException as e:  # noqa: BLE001 - every failure must reach JS
            self._fail(call_id, method, e)
            return
        try:
            payload = to_json(result)
            self.call_in_gtk(self._resolve, call_id, payload, None)
        except Exception as e:
            self._fail(call_id, method, e)

    def _fail(self, call_id: Any, method: str, exc: BaseException) -> None:
        if isinstance(exc, AirlinxError):
            log.info("rpc %s failed: %s", method, exc)
        else:
            log.error("rpc %s crashed", method, exc_info=exc)
        self.call_in_gtk(self._resolve, call_id, None, error_payload(exc))

    def _resolve(self, call_id: Any, result: Any, error: Optional[dict]) -> None:
        if call_id is None:
            return
        self.eval_js(
            f"window.airlinx&&window.airlinx._resolve({_js_literal(call_id)},"
            f"{_js_literal(result)},{_js_literal(error)});"
        )

    # ------------------------------------------------------------------ events
    def emit(self, event: str, payload: Any = None) -> None:
        """Thread-safe: serialize now, evaluate on the GTK thread."""
        try:
            script = (f"window.airlinx&&window.airlinx._emit({_js_literal(event)},"
                      f"{_js_literal(to_json(payload))});")
        except Exception:
            log.exception("cannot serialize event %s", event)
            return
        self.call_in_gtk(self.eval_js, script)
