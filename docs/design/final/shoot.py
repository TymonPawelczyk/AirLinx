"""Zrzuty ekranu finalnej makiety (1100×720). Użycie: python shoot.py [nazwa ...]"""
import sys, pathlib
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
SHOTS = [
    ("discovery", "discovery"), ("discovery-scan", "discovery-scan"), ("notfound", "error-notfound"),
    ("main", "main"), ("picker", "source-picker"), ("picker-win", "source-picker-windows"),
    ("devices", "devices-menu"), ("permission", "error-permission"), ("live", "live"), ("live-details", "live-details"),
    ("live-kino", "live-kino"), ("lost", "error-lost"), ("media-empty", "media-empty"),
    ("media-drag", "media-drag"), ("media", "media"), ("subs", "media-subtitles"),
    ("settings", "settings"), ("settings-obraz", "settings-quality"), ("tray", "tray"),
    ("main/light", "light-main"), ("live/light", "light-live"), ("live-details/light", "light-live-details"), ("media/light", "light-media"),
    ("settings/light", "light-settings"), ("discovery/light", "light-discovery"),
]
only = set(sys.argv[1:])
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1100, "height": 720})
    for state, name in SHOTS:
        if only and name not in only:
            continue
        pg.goto(f"{(HERE / 'index.html').as_uri()}#{state}/shot")
        pg.reload()
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(1300)
        pg.screenshot(path=str(HERE / "shots" / f"{name}.png"))
        print("ok", name)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto((HERE / 'index.html').as_uri()); pg.wait_for_timeout(500)
    print("errors:", errs)
    b.close()
