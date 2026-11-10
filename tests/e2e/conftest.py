"""Playwright fixtures: static servers for ui/ and site/, one Chromium per session, one context per test.

Everything is opt-in (`-m e2e`). When Chromium is missing the tests skip instead of failing.
"""
import functools
import http.server
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

sync_api = pytest.importorskip("playwright.sync_api")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # keep test output clean
        pass


def _serve(directory: Path):
    handler = functools.partial(_Quiet, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="session")
def ui_url():
    srv = _serve(ROOT / "ui")
    yield f"http://127.0.0.1:{srv.server_port}/index.html"
    srv.shutdown()


@pytest.fixture(scope="session")
def site_url():
    srv = _serve(ROOT / "site")
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # browser binary missing
            pytest.skip(f"Chromium unavailable ({str(e).splitlines()[0]}); run `playwright install chromium`")
        yield b
        b.close()


class Tab:
    """A page plus everything it logged that should never happen."""

    def __init__(self, page, origin):
        self.page = page
        self.problems: list[str] = []
        self.external: list[str] = []
        page.on("console", lambda m: m.type in ("error", "warning") and self.problems.append(f"console.{m.type}: {m.text}"))
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"requestfailed: {r.url}"))
        page.on("request", lambda r: self._seen(r.url, origin))

    def _seen(self, url, origin):
        if not url.startswith((origin, "data:", "blob:", "about:")):
            self.external.append(url)


@pytest.fixture
def new_tab(browser, request):
    """new_tab(url, viewport=..., scheme='dark'|'light', reduced_motion=True) -> Tab. Contexts are closed at teardown."""
    contexts = []

    def make(url, viewport=None, scheme="dark", reduced_motion=True, permissions=None):
        ctx = browser.new_context(viewport=viewport or {"width": 1100, "height": 720}, color_scheme=scheme,
                                  reduced_motion="reduce" if reduced_motion else "no-preference",
                                  permissions=permissions)
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(8000)
        origin = url.split("/", 3)[0] + "//" + url.split("/", 3)[2]
        tab = Tab(page, origin)
        page.goto(url)
        return tab

    yield make
    for c in contexts:
        c.close()
