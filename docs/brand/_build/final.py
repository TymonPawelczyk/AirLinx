#!/usr/bin/env python3
"""Production kit for the chosen direction C „Styk” → docs/brand/final/.

Run (venv with fonttools, uharfbuzz, skia-pathops, playwright):
    python final.py
"""
import json
import math
import pathlib
import shutil
from geom import *  # noqa
from marks import _chevron, SQ3
from wordmarks import Font, FONT_DIR_JB, SIZE

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIN = ROOT / "final"

# ======================================================================== palette (neutrals aligned with UI „OLED Cinema”)
DARK = {
    "bg": "#000000", "surface": "#0D0D0D", "surface-2": "#161616", "border": "#2A2A2A",
    "text": "#F3F2EF", "text-2": "#A3A29E", "text-3": "#7E7D7A",
    "accent": "#3DF2A0", "accent-hover": "#6CF7B8", "on-accent": "#00160B", "accent-text": "#3DF2A0",
    "success": "#86D69A", "warning": "#F5B544", "error": "#FF7A6B", "info": "#7DB8FF", "focus": "#6CF7B8",
}
LIGHT = {
    "bg": "#F2F2F0", "surface": "#FFFFFF", "surface-2": "#E9E9E6", "border": "#D6D6D2",
    "text": "#0E0E0D", "text-2": "#575754", "text-3": "#6B6B67",
    "accent": "#007A47", "accent-hover": "#00663B", "on-accent": "#FFFFFF", "accent-text": "#007A47",
    "success": "#1F8F43", "warning": "#8A5300", "error": "#C2392B", "info": "#1D63B8", "focus": "#007A47",
}
ROLES = {
    "bg": "tło okna i ekranu TV (OLED: czysta czerń)", "surface": "arkusze, menu, karty unoszące się nad tłem",
    "surface-2": "pola, hover, studzienki", "border": "linie podziału i obrysy",
    "text": "tekst podstawowy, szewrony znaku", "text-2": "tekst drugorzędny, etykiety",
    "text-3": "podpowiedzi, tekst nieaktywny (min. 4,5:1)",
    "accent": "Fosfor: romb w znaku, aktywna transmisja, akcja główna (wypełnienie)",
    "accent-hover": "hover / wciśnięcie akcji", "on-accent": "tekst i ikony na wypełnieniu Fosfor",
    "accent-text": "tekst i linki w kolorze akcentu", "success": "połączono",
    "warning": "uwaga: słaby sygnał, wymagana zgoda na TV (bursztyn z UI)", "error": "błąd, utracone połączenie",
    "info": "informacja, podpowiedź", "focus": "pierścień fokusu klawiatury",
}
PAIRS = [("text", "bg", 4.5), ("text", "surface", 4.5), ("text-2", "bg", 4.5), ("text-2", "surface", 4.5),
         ("text-3", "bg", 4.5), ("accent-text", "bg", 4.5), ("accent-text", "surface", 4.5),
         ("accent", "bg", 3.0), ("on-accent", "accent", 4.5), ("success", "bg", 3.0), ("warning", "bg", 3.0),
         ("error", "bg", 3.0), ("info", "bg", 3.0), ("focus", "bg", 3.0)]
INK_D, INK_L, FOSFOR, FOSFOR_L = DARK["text"], LIGHT["text"], DARK["accent"], LIGHT["accent-text"]
SYM = "#2e3436"

# ======================================================================== symbol with optical corrections
W, A, HX, CLEAR = 36, 72, 17.6, 12   # stroke, half-height, rhombus half-width (+3.5 % vs. round 1), clearance
BLUNT = 0.06                         # tip blunt as share of stroke


def symbol(reversed_=False, small=False):
    """Optical corrections vs. round 1:
    - rhombus +3.5 %: a solid point-ended shape reads smaller than its box;
    - miter tips of the chevrons blunted by 6 % of the stroke so they do not look like they pierce the gap
      and stay clean when antialiased;
    - reversed (light-on-dark) version thinned 3 % against irradiation.
    """
    w, a, hx, clear = (44, 68, 20.5, 15) if small else (W, A, HX, CLEAR)
    if reversed_:
        w *= 0.97
    hy = hx * SQ3
    tip = 128 - hx - clear / math.sin(math.radians(60))
    vx = tip - w
    left = _chevron(vx, a, w, 1)
    left = inter(left, box(-1000, -1000, tip - BLUNT * w, 1000))
    return {"left": left, "right": mirror_x(left),
            "frame": poly([(128 - hx, 128), (128, 128 - hy), (128 + hx, 128), (128, 128 + hy)])}


def pixel_symbol(N, th, a, c, hx, cy=None, frame=True):
    """Pixel-grid drawing for 16–24 px: 2:1 slopes (63.4°) render crisply; horizontal stroke `th`."""
    cy = N / 2 if cy is None else cy
    tip = N / 2 - hx - c
    x0 = tip - th - a / 2
    left = poly([(x0, cy - a), (x0 + th, cy - a), (tip, cy), (x0 + th, cy + a), (x0, cy + a), (x0 + a / 2, cy)])
    parts = {"left": left, "right": mirror_x(left, N / 2)}
    if frame:
        parts["frame"] = poly([(N / 2 - hx, cy), (N / 2, cy - 2 * hx), (N / 2 + hx, cy), (N / 2, cy + 2 * hx)])
    return parts


# tray / small-icon grids: (th, a, c, hx)
PIX = {16: (2, 5, 1, 1.5), 22: (2.5, 7, 1.25, 2), 24: (3, 8, 1.5, 2)}


def tray(N, state):
    th, a, c, hx = PIX[N]
    p = pixel_symbol(N, th, a, c, hx)
    shape = union(p["left"], p["right"])
    if state == "streaming":
        shape = union(shape, p["frame"])
    elif state == "error":
        bw = 2 if N < 24 else 2.5
        cx = N / 2
        y0, y1 = N / 2 - a, N / 2 + a
        dot = box(cx - bw / 2, y1 - bw, cx + bw / 2, y1)
        bar = box(cx - bw / 2, y0, cx + bw / 2, y1 - bw - (1.5 if N < 22 else 2))
        shape = union(shape, bar, dot)
    return shape


# ======================================================================== wordmark (optically tuned)
def wordmark():
    """JetBrains Mono ExtraBold, lowercase. Corrections: rhombus tittles sized to ~1.2× the area of the
    original dot (a pointed shape of equal area reads smaller), lifted 2 % to keep the x-height gap; tracking
    −4 % with the „rl” and „nx” pairs opened 1 % (mono shapes clash at tight tracking)."""
    F = Font(FONT_DIR_JB + "JetBrainsMono-ExtraBold.ttf")
    xh = F.xheight / F.upem * SIZE

    def dia_i(x, base, size, adv):
        stem = F.glyph("dotlessi", x, base, size)
        full = F.glyph("i", x, base, size)
        dot = diff(full, box(-1e5, base - xh - 2, 1e5, 1e5))
        dx0, dy0, dx1, dy1 = dot.bounds
        cx, cy = (stem.bounds[0] + stem.bounds[2]) / 2, (dy0 + dy1) / 2 - 0.02 * size
        # centre on the stem (JB Mono i has a left-leaning top serif, the dot sits on the stem)
        sb = inter(stem, box(-1e5, base - xh * 0.5, 1e5, base - xh * 0.4)).bounds
        cx = (sb[0] + sb[2]) / 2
        r = (dy1 - dy0) * 0.70
        return union(stem, poly([(cx - r * 0.86, cy), (cx, cy - r), (cx + r * 0.86, cy), (cx, cy + r)]))

    glyphs, end = F.shape("airlinx", SIZE, tracking=-0.04, replace={1: dia_i, 4: dia_i},
                          kern_adjust={2: 0.01, 5: 0.01})
    return union(*glyphs.values()), xh


# ======================================================================== helpers
def paint(parts, colors):
    return [(p, colors.get(k, colors.get("*"))) for k, p in parts.items()]


def write(rel, text):
    p = FIN / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


COL = {
    "black": {"*": "#000000"}, "white": {"*": "#FFFFFF"},
    "dark": {"left": INK_D, "right": INK_D, "frame": FOSFOR, "word": INK_D},
    "light": {"left": INK_L, "right": INK_L, "frame": FOSFOR_L, "word": INK_L},
}


def unit_X(sym_parts):
    """Clear-space unit X = width of the rhombus."""
    b = sym_parts["frame"].bounds
    return b[2] - b[0]


def lockup_h(variant):
    sym = symbol(reversed_=(variant in ("dark", "white")))
    word, xh = wordmark()
    b = bounds(sym.values())
    S = 110
    s = S / (b[3] - b[1])
    X = unit_X(sym) * s
    pad = 2 * X
    sym = place(sym, s, pad - b[0] * s, pad - b[1] * s)
    sb = bounds(sym.values())
    k = 0.62 * S / xh
    wb = word.bounds
    gap = 1.25 * X
    base = (sb[1] + sb[3]) / 2 + xh * k / 2
    word = xform(word, k, 0, 0, k, sb[2] + gap - wb[0] * k, base)
    allb = bounds(list(sym.values()) + [word])
    dy = pad - allb[1]
    sym = place(sym, 1, 0, dy)
    word = xform(word, 1, 0, 0, 1, 0, dy)
    allb = bounds(list(sym.values()) + [word])
    return sym, word, allb[2] + pad, allb[3] + pad, X, allb


def lockup_v(variant):
    sym = symbol(reversed_=(variant in ("dark", "white")))
    word, xh = wordmark()
    wb = word.bounds
    ww = wb[2] - wb[0]
    b = bounds(sym.values())
    s = 0.62 * ww / (b[2] - b[0])
    X = unit_X(sym) * s
    pad = 2 * X
    width = ww + 2 * pad
    sw = (b[2] - b[0]) * s
    sym = place(sym, s, (width - sw) / 2 - b[0] * s, pad - b[1] * s)
    sb = bounds(sym.values())
    gap = 1.5 * X
    word = xform(word, 1, 0, 0, 1, pad - wb[0], sb[3] + gap - wb[1])
    allb = bounds(list(sym.values()) + [word])
    return sym, word, width, allb[3] + pad, X, allb


def write_lockups():
    for name, fn in (("horizontal", lockup_h), ("vertical", lockup_v)):
        for variant in ("black", "white", "dark", "light"):
            sym, word, Wd, Hd, X, _ = fn(variant)
            c = COL[variant]
            write(f"logo/airlinx-lockup-{name}-{variant}.svg",
                  svg_doc(Wd, Hd, paint(sym, c) + [(word, c.get("word", c.get("*")))],
                          f"AirLinx — logo {'poziome' if name == 'horizontal' else 'pionowe'} ({variant})"))


def write_marks():
    for variant in ("black", "white", "dark", "light"):
        sym, _ = fit(symbol(reversed_=(variant in ("dark", "white"))), 24, 24, 232, 232)
        write(f"logo/airlinx-symbol-{variant}.svg", svg_doc(256, 256, paint(sym, COL[variant]), f"AirLinx — znak ({variant})"))
    sm, _ = fit(symbol(small=True), 16, 16, 240, 240)
    write("logo/airlinx-symbol-small-black.svg", svg_doc(256, 256, paint(sm, COL["black"]), "AirLinx — znak, wersja do małych rozmiarów"))
    word, xh = wordmark()
    b = word.bounds
    pad = 40
    word = xform(word, 1, 0, 0, 1, pad - b[0], pad - b[1])
    for variant, col in (("black", "#000000"), ("white", "#FFFFFF"), ("dark", INK_D), ("light", INK_L)):
        write(f"logo/airlinx-wordmark-{variant}.svg",
              svg_doc(b[2] - b[0] + 2 * pad, b[3] - b[1] + 2 * pad, [(word, col)], f"AirLinx — logotyp ({variant})"))


def write_clearspace():
    """Construction / clear-space sheet for the horizontal and vertical lockup."""
    out = []
    for name, fn in (("horizontal", lockup_h), ("vertical", lockup_v)):
        sym, word, Wd, Hd, X, ab = fn("light")
        layers = [f'<rect width="{fmt(Wd)}" height="{fmt(Hd)}" fill="#FFFFFF"/>',
                  f'<rect x="{fmt(ab[0]-2*X)}" y="{fmt(ab[1]-2*X)}" width="{fmt(ab[2]-ab[0]+4*X)}" height="{fmt(ab[3]-ab[1]+4*X)}" fill="#E7F8EF"/>',
                  f'<rect x="{fmt(ab[0])}" y="{fmt(ab[1])}" width="{fmt(ab[2]-ab[0])}" height="{fmt(ab[3]-ab[1])}" fill="#FFFFFF" stroke="#00A862" stroke-width="1" stroke-dasharray="4 3"/>']
        # X markers (rhombi) in the four margins
        fb = sym["frame"].bounds
        rw, rh = fb[2] - fb[0], fb[3] - fb[1]
        for (cx, cy) in ((ab[0] - X, (ab[1] + ab[3]) / 2), (ab[2] + X, (ab[1] + ab[3]) / 2),
                         ((ab[0] + ab[2]) / 2, ab[1] - X), ((ab[0] + ab[2]) / 2, ab[3] + X)):
            layers.append(f'<path fill="#00A862" fill-opacity=".55" d="{to_d(poly([(cx - rw/2, cy), (cx, cy - rh/2), (cx + rw/2, cy), (cx, cy + rh/2)]))}"/>')
        layers += paint(sym, COL["light"]) + [(word, INK_L)]
        out.append(write(f"logo/construction-clearspace-{name}.svg",
                         svg_doc(Wd, Hd, layers, f"AirLinx — pole ochronne 2X ({name}); X = szerokość rombu")))
    return out


# ======================================================================== app icons (hicolor)
TILE_TOP, TILE_BOT = "#1A1A1A", "#000000"


def app_icon_master():
    sym, _ = fit(symbol(reversed_=True), 100, 140, 412, 372, dy=-4)
    defs = (f'<linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{TILE_TOP}"/><stop offset="1" stop-color="{TILE_BOT}"/></linearGradient>'
            '<linearGradient id="h" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".2"/><stop offset=".45" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    layers = [(rrect(32, 40, 448, 448, 100), '#000" fill-opacity=".22'), (rrect(32, 32, 448, 448, 100), "url(#g)"),
              '<rect x="33" y="33" width="446" height="446" rx="99" fill="none" stroke="url(#h)" stroke-width="2"/>']
    layers += paint(sym, {"left": "#F3F2EF", "right": "#F3F2EF", "frame": FOSFOR})
    return svg_doc(512, 512, layers, "AirLinx", extra_defs=defs)


def app_icon_small():
    sym, _ = fit(symbol(small=True, reversed_=True), 9, 14, 55, 50)
    layers = [(rrect(2, 2, 60, 60, 14), "#0B0B0B")] + paint(sym, {"left": "#F3F2EF", "right": "#F3F2EF", "frame": FOSFOR})
    return svg_doc(64, 64, layers, "AirLinx")


def app_icon_pixel(N):
    """Hand-hinted 16/22/24 px app icons: full-bleed tile, 2:1 slopes on the pixel grid."""
    spec = {16: dict(th=2, a=4, c=1, hx=1, r=3.5, cy=8), 22: dict(th=2.5, a=6, c=1, hx=1.5, r=4.5, cy=11),
            24: dict(th=2.5, a=6, c=1.25, hx=1.5, r=5, cy=12)}[N]
    p = pixel_symbol(N, spec["th"], spec["a"], spec["c"], spec["hx"], cy=spec["cy"])
    layers = [(rrect(0, 0, N, N, spec["r"]), "#0B0B0B")] + paint(p, {"left": "#F3F2EF", "right": "#F3F2EF", "frame": FOSFOR})
    return svg_doc(N, N, layers, "AirLinx")


def write_icons():
    base = "icons/hicolor"
    write(f"{base}/scalable/apps/airlinx.svg", app_icon_master())
    sym, _ = fit(symbol(small=True), 1, 4, 15, 12)
    write(f"{base}/symbolic/apps/airlinx-symbolic.svg", svg_doc(16, 16, [(union(*sym.values()), SYM)], "airlinx-symbolic"))
    for N in (16, 22, 24):
        for st in ("idle", "streaming", "error"):
            write(f"{base}/{N}x{N}/status/airlinx-{st}-symbolic.svg", svg_doc(N, N, [(tray(N, st), SYM)], f"airlinx-{st}-symbolic"))
            if N == 16:
                write(f"{base}/scalable/status/airlinx-{st}-symbolic.svg", svg_doc(16, 16, [(tray(16, st), SYM)], f"airlinx-{st}-symbolic"))
    src = FIN / "icons" / "src"
    write("icons/src/airlinx-512.svg", app_icon_master())
    write("icons/src/airlinx-small.svg", app_icon_small())
    for N in (16, 22, 24):
        write(f"icons/src/airlinx-{N}.svg", app_icon_pixel(N))
    return {**{n: src / "airlinx-512.svg" for n in (512, 256, 128, 64)},
            **{n: src / "airlinx-small.svg" for n in (48, 32)},
            **{n: src / f"airlinx-{n}.svg" for n in (24, 22, 16)}}


# ======================================================================== motion
def connecting_svg(theme="dark", scope="ax", paused_at=None):
    """Self-animating „łączenie” mark. All class and keyframe names are prefixed with `scope`, so several
    copies can be inlined in one HTML document without their <style> rules colliding.
    paused_at: seconds into the 1.6 s loop -> a static frame (for film strips)."""
    sym, _ = fit(symbol(reversed_=(theme == "dark")), 8, 8, 248, 248)
    ink = INK_D if theme == "dark" else INK_L
    acc = FOSFOR if theme == "dark" else LIGHT["accent"]
    lb, rb = sym["left"].bounds, sym["right"].bounds
    travel = (rb[0] - lb[2]) / 2 - 4  # half the gap between the blunted tips
    p = scope
    hold = f"animation-play-state:paused;animation-delay:-{paused_at}s;" if paused_at is not None else ""
    css = f"""
.{p}-c{{fill:{ink}}}
.{p}-f{{fill:{acc};transform-box:fill-box;transform-origin:center;animation:{p}-fly 1.6s cubic-bezier(.65,0,.35,1) infinite;{hold}}}
.{p}-l{{animation:{p}-pl 1.6s cubic-bezier(.65,0,.35,1) infinite;{hold}}}
.{p}-r{{animation:{p}-pr 1.6s cubic-bezier(.65,0,.35,1) infinite;{hold}}}
@keyframes {p}-fly{{0%{{transform:translateX(-{fmt(travel)}px) scale(.3);opacity:0}}20%{{opacity:1}}
 50%{{transform:translateX(0) scale(1);opacity:1}}80%{{opacity:1}}100%{{transform:translateX({fmt(travel)}px) scale(.3);opacity:0}}}}
@keyframes {p}-pl{{0%,100%{{transform:translateX(0)}}12%{{transform:translateX(-3px)}}30%{{transform:translateX(0)}}}}
@keyframes {p}-pr{{0%,62%,100%{{transform:translateX(0)}}88%{{transform:translateX(3px)}}}}
@keyframes {p}-br{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
@media (prefers-reduced-motion: reduce){{
 .{p}-f{{animation:{p}-br 2.4s ease-in-out infinite;transform:none}}
 .{p}-l,.{p}-r{{animation:none}}
}}"""
    body = (f'<path class="{p}-c {p}-l" d="{to_d(sym["left"])}"/><path class="{p}-c {p}-r" d="{to_d(sym["right"])}"/>'
            f'<path class="{p}-f frame" d="{to_d(sym["frame"])}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256" role="img" aria-label="Łączenie…">'
            f"<title>AirLinx — łączenie</title><style>{css}</style>{body}</svg>\n")


# ======================================================================== tokens
def contrast_table(p):
    return {f"{f} on {b}": {"ratio": round(contrast(p[f], p[b]), 2), "min": m, "pass": contrast(p[f], p[b]) >= m}
            for f, b, m in PAIRS}


TYPE = {
    "family": {
        "display": {"$value": "\"JetBrains Mono\", ui-monospace, monospace", "use": "logotyp, nagłówki ekranu TV, wszystkie liczby",
                    "license": "SIL Open Font License 1.1", "source": "https://www.jetbrains.com/lp/mono/"},
        "ui": {"$value": "\"Inter\", \"Cantarell\", system-ui, sans-serif", "use": "tekst interfejsu i opisy",
               "license": "SIL Open Font License 1.1", "source": "https://rsms.me/inter/"},
        "mono": {"$value": "\"JetBrains Mono\", ui-monospace, monospace", "use": "statystyki na żywo (ms, kl./s, Mb/s), IP",
                 "license": "SIL Open Font License 1.1", "source": "https://www.jetbrains.com/lp/mono/"},
    },
    "weight": {"regular": 400, "medium": 500, "semibold": 600, "display": 800},
    "size": {"xs": "11px", "sm": "12px", "base": "14px", "md": "16px", "lg": "20px", "xl": "28px", "tv-title": "64px", "tv-body": "28px"},
    "tracking": {"display": "-0.04em", "title": "-0.01em", "body": "0", "caps": "0.12em"},
    "line-height": {"tight": 1.15, "normal": 1.45},
}
RADIUS = {"s": "8px", "m": "12px", "l": "18px", "icon-tile": "22%"}
MOTION = {"ease-out": "cubic-bezier(.22, 1, .36, 1)", "ease-io": "cubic-bezier(.65, 0, .35, 1)",
          "duration-fast": "140ms", "duration-base": "220ms", "connecting-loop": "1600ms"}


def write_tokens():
    tok = {
        "$description": "AirLinx — tokeny marki (kierunek C „Styk”). Dark-first; neutrale zgodne z UI „OLED Cinema”.",
        "color": {m: {k: {"$value": v, "$type": "color", "role": ROLES[k]} for k, v in p.items()}
                  for m, p in (("dark", DARK), ("light", LIGHT))},
        "contrast": {"dark": contrast_table(DARK), "light": contrast_table(LIGHT)},
        "logo": {"ink-dark": INK_D, "ink-light": INK_L, "frame-dark": FOSFOR, "frame-light": FOSFOR_L,
                 "clearspace": "2X, X = szerokość rombu", "min-size": {"symbol": "16px", "lockup-horizontal": "20px wysokości", "lockup-vertical": "56px wysokości"}},
        "font": TYPE, "radius": RADIUS, "motion": MOTION,
    }
    write("tokens.json", json.dumps(tok, ensure_ascii=False, indent=2) + "\n")

    def block(p):
        return "\n".join(f"  --ax-{k}: {v};" for k, v in p.items())
    alias = """  /* Aliasy pod nazwy zmiennych z UI „OLED Cinema” (ui/tokens.css) */
  --bg: var(--ax-bg);
  --ink: var(--ax-text);
  --raise: var(--ax-surface);
  --accent: var(--ax-accent);
  --accent-ink: var(--ax-on-accent);
  --accent-text: var(--ax-accent-text);
  --ok: var(--ax-success);
  --danger: var(--ax-error);
  --font: var(--ax-font-ui);
  --mono: var(--ax-font-mono);"""
    css = f"""/* AirLinx — tokeny marki (kierunek C „Styk”). Wygenerowane przez docs/brand/_build/final.py.
   Fonty: Inter i JetBrains Mono, oba SIL Open Font License 1.1 (zainstalowane w Zorin OS 17;
   do paczki aplikacji można dołączyć pliki .ttf/.woff2 razem z plikiem OFL.txt). */

:root {{
  color-scheme: dark;
{block(DARK)}

  --ax-font-display: {TYPE['family']['display']['$value']};
  --ax-font-ui: {TYPE['family']['ui']['$value']};
  --ax-font-mono: {TYPE['family']['mono']['$value']};
  --ax-weight-regular: 400; --ax-weight-medium: 500; --ax-weight-semibold: 600; --ax-weight-display: 800;
{chr(10).join(f'  --ax-size-{k}: {v};' for k, v in TYPE['size'].items())}
  --ax-tracking-display: -0.04em; --ax-tracking-title: -0.01em; --ax-tracking-caps: 0.12em;
  --ax-radius-s: 8px; --ax-radius-m: 12px; --ax-radius-l: 18px;
  --ax-ease-out: {MOTION['ease-out']}; --ax-ease-io: {MOTION['ease-io']};
  --ax-duration-fast: 140ms; --ax-duration-base: 220ms; --ax-connecting-loop: 1600ms;

{alias}
}}

@media (prefers-color-scheme: light) {{
  :root:not([data-theme="dark"]) {{
    color-scheme: light;
{chr(10).join('  ' + l for l in block(LIGHT).splitlines())}
  }}
}}
:root[data-theme="light"] {{
  color-scheme: light;
{block(LIGHT)}
}}

@media (prefers-reduced-motion: reduce) {{
  :root {{ --ax-duration-fast: 0ms; --ax-duration-base: 0ms; }}
}}
"""
    write("tokens.css", css)
    return tok


def main():
    if FIN.exists():
        shutil.rmtree(FIN)
    write_marks()
    write_lockups()
    write_clearspace()
    sources = write_icons()
    write("motion/airlinx-connecting.svg", connecting_svg("dark"))
    write("motion/airlinx-connecting-light.svg", connecting_svg("light"))
    tok = write_tokens()
    bad = [(m, k) for m in ("dark", "light") for k, v in tok["contrast"][m].items() if not v["pass"]]
    print("contrast failures:", bad)
    json.dump({str(k): str(pathlib.Path(v).resolve().relative_to(ROOT)) for k, v in sources.items()},
              open(ROOT / "_build" / ".icon_sources.json", "w"))


if __name__ == "__main__":
    main()
