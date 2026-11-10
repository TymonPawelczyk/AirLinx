"""Playwright helpers: rasterise SVGs at exact pixel sizes and screenshot HTML pages."""
import base64
import pathlib
from contextlib import contextmanager
from playwright.sync_api import sync_playwright


@contextmanager
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(channel="chrome")
        try:
            yield b
        finally:
            b.close()


def svg_to_png(b, svg_path, size, out, bg=None):
    """Rasterise an SVG to an exact size×size PNG (transparent unless bg)."""
    svg = pathlib.Path(svg_path).read_text()
    data = base64.b64encode(svg.encode()).decode()
    page = b.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
    bgcss = bg or "transparent"
    page.set_content(f'<html><body style="margin:0;background:{bgcss}">'
                     f'<img src="data:image/svg+xml;base64,{data}" width="{size}" height="{size}" style="display:block"></body></html>')
    page.wait_for_load_state("load")
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), omit_background=bg is None, clip={"x": 0, "y": 0, "width": size, "height": size})
    page.close()


def shot(b, html_path, out, width, full_page=True, scale=1):
    page = b.new_page(viewport={"width": width, "height": 900}, device_scale_factor=scale)
    page.goto(pathlib.Path(html_path).resolve().as_uri())
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), full_page=full_page)
    page.close()


def png_data_uri(path):
    return "data:image/png;base64," + base64.b64encode(pathlib.Path(path).read_bytes()).decode()
