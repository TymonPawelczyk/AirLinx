"""Background asyncio loop + helpers to hop between GTK main thread and asyncio.

GTK runs on the main thread. All networking / ffmpeg lives in one asyncio loop
running in a daemon thread (see docs/ARCHITECTURE.md, "Współbieżność").
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Awaitable, Callable, Coroutine, Optional

log = logging.getLogger(__name__)

_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _run(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    asyncio.set_event_loop(loop)
    loop.call_soon(ready.set)
    try:
        loop.run_forever()
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # pragma: no cover - best effort
            log.exception("error while finalizing asyncio loop")
        loop.close()


def start() -> asyncio.AbstractEventLoop:
    """Start the background loop (idempotent) and return it."""
    global _loop, _thread
    with _lock:
        if _loop is not None and _thread is not None and _thread.is_alive():
            return _loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        thread = threading.Thread(target=_run, args=(loop, ready), name="airlinx-asyncio", daemon=True)
        thread.start()
        ready.wait(5)
        _loop, _thread = loop, thread
        return loop


def loop() -> asyncio.AbstractEventLoop:
    return _loop if _loop is not None else start()


def in_loop_thread() -> bool:
    return _thread is not None and threading.current_thread() is _thread


def run_coro(coro: Coroutine[Any, Any, Any] | Awaitable[Any]) -> concurrent.futures.Future:
    """Schedule a coroutine on the background loop; returns a concurrent Future."""
    return asyncio.run_coroutine_threadsafe(coro, loop())  # type: ignore[arg-type]


def call_soon(fn: Callable[..., Any], *args: Any) -> None:
    """Run a plain callable on the asyncio thread."""
    loop().call_soon_threadsafe(fn, *args)


def call_in_gtk(fn: Callable[..., Any], *args: Any) -> None:
    """Run fn(*args) on the GTK main thread (GLib.idle_add), exactly once."""
    from gi.repository import GLib

    def _cb() -> bool:
        try:
            fn(*args)
        except Exception:
            log.exception("call_in_gtk callback failed")
        return False

    GLib.idle_add(_cb)


def stop(timeout: float = 5.0) -> None:
    """Stop the loop (cancels remaining tasks) and join the thread."""
    global _loop, _thread
    with _lock:
        lp, th = _loop, _thread
        _loop = _thread = None
    if lp is None:
        return
    if lp.is_running():
        lp.call_soon_threadsafe(lp.stop)
    if th is not None and th is not threading.current_thread():
        th.join(timeout)
