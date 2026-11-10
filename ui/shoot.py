"""Zrzuty UI z mockiem bridge'a (zwykła przeglądarka, Playwright), 1100×720.

Każdy stan jest osiągany prawdziwą interakcją z interfejsem (kliknięcia, mock.* do symulacji TV),
więc skrypt jest też testem dymnym przepływów. Użycie:
    .venv/bin/python ui/shoot.py            # wszystkie zrzuty do ui/shots/
    .venv/bin/python ui/shoot.py audio      # tylko źródło dźwięku (settings-audio*, main-audio)
Kończy się kodem 1, jeśli strona zgłosi błąd JS.
"""
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(os.environ.get("SHOTS_DIR") or HERE / "shots")
OUT.mkdir(exist_ok=True)
URL = (HERE / "index.html").as_uri()
errors: list[str] = []


def page(b, query="", reduced=False):
    pg = b.new_page(viewport={"width": 1100, "height": 720}, reduced_motion="reduce" if reduced else "no-preference")
    pg.on("pageerror", lambda e: errors.append(f"{query}: {e}"))
    pg.on("console", lambda m: m.type == "error" and errors.append(f"{query}: console {m.text}"))
    pg.goto(URL + query)
    return pg


def shot(pg, name, wait=350):
    pg.wait_for_timeout(wait)
    pg.screenshot(path=str(OUT / f"{name}.png"))
    print("ok", name)


def ready(pg):
    pg.wait_for_function("document.querySelector('.app').dataset.screen === 'work'", timeout=8000)
    pg.wait_for_function("document.querySelector('#preview-img').getAttribute('src')", timeout=8000)


def settle_stats(pg, details=False):
    """Mock rozrzuca opóźnienie wokół zmierzonych 111 ms. Zrzut łapie próbkę równą pomiarowi z README i strony."""
    sels = ["#s-lat", "#d-lat", "#budget-total"] if details else ["#s-lat"]
    pg.wait_for_function("s => s.every(q => parseInt(document.querySelector(q).textContent, 10) === 111)", arg=sels, timeout=90000)


def go_live(pg):
    pg.click("#share-btn")
    pg.wait_for_function("document.querySelector('.app').dataset.session === 'live'", timeout=8000)


def run(theme=""):
    q = f"?theme={theme}" if theme else ""
    sfx = f"light-" if theme == "light" else ""
    with sync_playwright() as p:
        b = p.chromium.launch()

        pg = page(b, q)
        if not theme:
            shot(pg, "discovery-scan", 250)
        ready(pg)
        shot(pg, f"{sfx}main")
        if not theme:
            pg.click(".tv-pill"); shot(pg, "devices-menu", 300)
            pg.click("#m-devices [data-act=rescan]"); pg.wait_for_timeout(1900); shot(pg, "discovery")
            pg.click("[data-act=close-disc]")
            pg.click(".r-idle [data-act=picker]"); shot(pg, "source-picker", 700)
            pg.click("[data-ptab=window]"); shot(pg, "source-picker-windows", 300)
            pg.keyboard.press("Escape")
            pg.click("#share-btn"); shot(pg, "connecting", 120)
            pg.wait_for_function("document.querySelector('.app').dataset.session === 'permission'"); shot(pg, "waiting-tv", 150)
            pg.wait_for_function("document.querySelector('.app').dataset.session === 'live'", timeout=8000)
        else:
            go_live(pg)
        pg.wait_for_timeout(1300); settle_stats(pg); shot(pg, f"{sfx}live", 0)
        pg.click("[data-act=details]")
        pg.wait_for_timeout(7000)
        settle_stats(pg, details=True); shot(pg, f"{sfx}live-details", 0)
        if not theme:
            pg.focus("[data-act=details]"); settle_stats(pg, details=True); shot(pg, "focus-keyboard", 0)
            pg.click("[data-act=details]")
            pg.evaluate("airlinx.mock.dropConnection()"); shot(pg, "error-lost", 250)
            pg.wait_for_timeout(1800)
            pg.evaluate("airlinx.mock.mirrorError('Telewizor zamknął sesję (CLOSE od odbiornika).')"); shot(pg, "error-failed", 300)
            pg.evaluate("airlinx.mock.setTvBusy(true)")
            pg.click(".f-failed [data-act=share]"); shot(pg, "tv-busy", 500)
            pg.click("[data-act=confirm-yes]")   # retry.method(retry.params) z force=True
            pg.wait_for_function("document.querySelector('.app').dataset.session === 'live'", timeout=8000)
            print("ok retry -> live")
            pg.click(".r-live [data-act=stop]")
            pg.wait_for_function("document.querySelector('.app').dataset.session === 'idle'", timeout=5000)
        else:
            pg.click(".r-live [data-act=stop]")
        pg.click("[data-mode-btn=media]"); shot(pg, f"{sfx}media-empty", 300) if not theme else None
        pg.click(".r-media [data-act=pick-files]")
        pg.wait_for_function("document.querySelectorAll('#queue .q-item').length > 0", timeout=5000)
        pg.click("#queue .q-item >> nth=0")
        pg.wait_for_function("document.querySelector('.app').dataset.paused === 'false'", timeout=6000)
        shot(pg, f"{sfx}media", 1200)
        if not theme:
            pg.click("#subs-btn"); shot(pg, "media-subtitles", 300); pg.keyboard.press("Escape")
        pg.click("[data-act=settings]")
        if not theme:
            shot(pg, "settings-general", 300)
            pg.click("[data-tab=obraz]"); shot(pg, "settings-quality", 300)
        pg.click("[data-tab=zaawansowane]"); shot(pg, f"{sfx}settings", 300)
        pg.close()

        if not theme:
            pg = page(b, "?mock=nodevices")
            pg.wait_for_function("document.querySelector('.app').dataset.screen === 'notfound'", timeout=8000)
            pg.click("[data-act=manual]"); shot(pg, "error-notfound", 300)
            pg.close()

            pg = page(b, "?mock=wayland")        # sesja Wayland: stała nota w trybie Ekran
            ready(pg); shot(pg, "main-wayland")
            pg.close()

            pg = page(b, "?mock=cpu")            # bez NVENC: Ustawienia nazywają koder programowy
            ready(pg)
            pg.click("[data-act=settings]"); pg.click("[data-tab=obraz]"); shot(pg, "settings-quality-cpu", 300)
            pg.close()

            pg = page(b, "?dev=1")
            ready(pg)
            pg.click("[data-act=dev]"); shot(pg, "dev-tools", 200)
            pg.click("[data-what=tray]"); pg.click("[data-act=dev]"); shot(pg, "tray-icons", 200)
            pg.close()

            pg = page(b, "", reduced=True)
            ready(pg); pg.click("#share-btn"); shot(pg, "reduced-motion-connecting", 150)
            pg.close()
        b.close()


def audio_shots():
    """Źródło dźwięku: ustawienia (aplikacja, mikrofon, wyłączony dźwięk) i podsumowanie w szynie."""
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = page(b)
        ready(pg)
        pg.click(".r-idle [data-act=audio-settings]")
        pg.wait_for_function("document.querySelector('.app').dataset.screen === 'settings'")
        pg.wait_for_function("document.querySelector('#app-list').getAttribute('aria-busy') === 'false'")
        pg.click("[data-group=asrc] [data-val=app]")
        pg.wait_for_selector("#app-list .app-opt[aria-checked=true]")
        pg.focus("#app-list .app-opt[aria-checked=true]")
        pg.keyboard.press("ArrowDown")            # klawiatura: strzałki zmieniają aplikację
        pg.keyboard.press("ArrowUp")
        pg.evaluate("document.activeElement.blur(); document.querySelector('.s-body').scrollTop = 150")
        shot(pg, "settings-audio", 300)
        pg.click("[data-group=asrc] [data-val=system_mic]")
        pg.select_option("#mic-sel", index=1)
        pg.evaluate("document.activeElement.blur(); document.querySelector('.s-body').scrollTop = 150")
        shot(pg, "settings-audio-mic", 300)
        pg.click("[data-group=asrc] [data-val=app]")
        pg.click("#app-list .app-opt >> nth=0")
        pg.click("[data-act=back]")
        shot(pg, "main-audio", 300)
        go_live(pg)
        pg.evaluate("airlinx.mock.setAudioApps([])")
        pg.click("[data-act=settings]")
        pg.click("[data-tab=dzwiek]")
        pg.wait_for_function("document.querySelector('#app-list').getAttribute('aria-busy') === 'false'")
        pg.click("[data-group=asrc] [data-val=mic]")     # zmiana w trakcie: Cast przełącza na żywo
        pg.click("[data-group=asrc] [data-val=app]")     # Firefox już nie gra → dźwięk systemu + ostrzeżenie
        pg.evaluate("document.querySelector('.s-body').scrollTop = 400")
        shot(pg, "settings-audio-live-fallback", 400)
        pg.click("[data-act=back]")
        shot(pg, "main-audio-live", 400)
        pg.click(".r-live [data-act=stop]")
        pg.click(".r-idle [data-opt=audio]")
        pg.click(".r-idle [data-act=audio-settings]")
        shot(pg, "settings-audio-off", 300)
        pg.close()
        b.close()


if sys.argv[1:] == ["audio"]:
    audio_shots()
    print("errors:", errors)
    sys.exit(1 if errors else 0)

run()
run("light")
audio_shots()
print("errors:", errors)
sys.exit(1 if errors else 0)
