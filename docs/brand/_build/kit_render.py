#!/usr/bin/env python3
"""Render the final kit: hicolor PNGs, TV splash, motion demo, kit preview + screenshots."""
import json
import pathlib
import re
import shutil
from render import browser, svg_to_png, shot, png_data_uri
from final import connecting_svg

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIN = ROOT / "final"
SHOTS = FIN / "shots"
FONTS = {
    "Inter-Regular.ttf": "/usr/share/fonts/truetype/inter-zorin-os/Inter-Regular.ttf",
    "Inter-Medium.ttf": "/usr/share/fonts/truetype/inter-zorin-os/Inter-Medium.ttf",
    "JetBrainsMono-Medium.ttf": "/usr/share/fonts/truetype/jetbrains-mono-zorin-os/JetBrainsMono-Medium.ttf",
    "JetBrainsMono-ExtraBold.ttf": "/usr/share/fonts/truetype/jetbrains-mono-zorin-os/JetBrainsMono-ExtraBold.ttf",
}


def inline(path, cls="", style=""):
    s = pathlib.Path(path).read_text()
    s = re.sub(r"<title>.*?</title>", "", s, flags=re.S)
    s = re.sub(r'<svg ([^>]*?)width="[^"]+" height="[^"]+"', r"<svg \1", s, count=1)
    return s.replace("<svg ", f'<svg class="{cls}" style="{style}" aria-hidden="true" ', 1)


def copy_fonts():
    d = FIN / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    for name, src in FONTS.items():
        shutil.copy(src, d / name)
    shutil.copy("/usr/share/doc/fonts-inter-zorin-os/copyright", d / "LICENSE-Inter-OFL-1.1.txt")
    shutil.copy("/usr/share/doc/fonts-jetbrains-mono-zorin-os/copyright", d / "LICENSE-JetBrainsMono-OFL-1.1.txt")


FONTFACE = """
@font-face{font-family:"AX Inter";src:local("Inter"),url("../fonts/Inter-Regular.ttf") format("truetype");font-weight:400}
@font-face{font-family:"AX Inter";src:local("Inter Medium"),url("../fonts/Inter-Medium.ttf") format("truetype");font-weight:500}
@font-face{font-family:"AX Mono";src:local("JetBrains Mono Medium"),url("../fonts/JetBrainsMono-Medium.ttf") format("truetype");font-weight:500}
@font-face{font-family:"AX Mono";src:local("JetBrains Mono ExtraBold"),url("../fonts/JetBrainsMono-ExtraBold.ttf") format("truetype");font-weight:800}
"""


def splash_html():
    anim = connecting_svg("dark", "sp").replace("<svg ", '<svg class="mark" aria-hidden="true" ', 1)
    word = inline(FIN / "logo" / "airlinx-wordmark-dark.svg", "word")
    return f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirLinx — ekran TV</title>
<!-- Ekran powitalny dla odbiornika w przeglądarce TV (Tizen) i dla podglądu Cast.
     Parametry URL: ?tv=Salon&state=connecting|ready|waiting-permission|lost
     Fonty dołączone z ../fonts (SIL OFL 1.1). Brak zależności sieciowych. -->
<style>{FONTFACE}
:root{{--bg:#000;--ink:#F3F2EF;--ink2:#A3A29E;--ink3:#7E7D7A;--acc:#3DF2A0;--warn:#F5B544;--err:#FF7A6B;
 --ease:cubic-bezier(.65,0,.35,1)}}
*{{box-sizing:border-box}}
html,body{{margin:0;height:100%;background:var(--bg);color:var(--ink);overflow:hidden;cursor:none}}
body{{font-family:"AX Inter",Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased}}
.stage{{position:fixed;inset:0;display:grid;place-items:center;
 background:radial-gradient(60vmax 40vmax at 50% 46%,rgba(61,242,160,.06),transparent 70%),#000}}
.stack{{display:flex;flex-direction:column;align-items:center}}
.mark{{display:block}}
.mark{{width:15vh;height:15vh}}
.route{{margin-top:4.2vh;display:flex;align-items:baseline;gap:1.6vh;font-family:"AX Mono","JetBrains Mono",monospace;
 font-weight:800;letter-spacing:-.03em;font-size:6vh;line-height:1}}
.route .from{{color:var(--ink2)}}
.route .arrow{{color:var(--acc);font-weight:500}}
.status{{margin-top:2.8vh;font-size:2.4vh;color:var(--ink2);letter-spacing:.01em;min-height:3vh}}
.meta{{margin-top:1.2vh;font-family:"AX Mono","JetBrains Mono",monospace;font-weight:500;font-size:1.6vh;color:var(--ink3);letter-spacing:.06em;text-transform:uppercase}}
.foot{{position:fixed;left:0;right:0;bottom:5.5vh;display:flex;justify-content:center;opacity:.55}}
.word{{height:2.6vh;width:auto}}
.foot .word path{{fill:var(--ink3)}}
body[data-state=ready] .status{{color:var(--acc)}}
body[data-state=waiting-permission] .status{{color:var(--warn)}}
body[data-state=lost] .status{{color:var(--err)}}
body[data-state=lost] .mark .frame,body[data-state=waiting-permission] .mark .frame{{animation:none;opacity:.25}}
body[data-state=ready] .mark .frame{{animation:none;opacity:1}}
.stack{{animation:in .9s cubic-bezier(.22,1,.36,1) both}}
@keyframes in{{from{{opacity:0;transform:translateY(1.5vh)}}to{{opacity:1;transform:none}}}}
@media (prefers-reduced-motion: reduce){{.stack{{animation:none}}}}
</style></head>
<body data-state="connecting">
<main class="stage"><div class="stack">
  {anim}
  <div class="route" aria-label="Salon odbiera z AirLinx"><span class="to" id="tv">Salon</span><span class="arrow">←</span><span class="from">AirLinx</span></div>
  <div class="status" id="status" role="status" aria-live="polite">Łączenie z komputerem…</div>
  <div class="meta" id="meta">Odbiornik gotowy · 4K · HDR</div>
</div></main>
<div class="foot">{word}</div>
<script>
(function(){{
  var q=new URLSearchParams(location.search), st=q.get('state')||'connecting', tv=q.get('tv');
  var copy={{connecting:'Łączenie z komputerem…',ready:'Połączono. Obraz za chwilę.',
    'waiting-permission':'Zaakceptuj połączenie pilotem.',lost:'Utracono sygnał. Ponawianie…'}};
  if(tv) document.getElementById('tv').textContent=tv;
  document.body.dataset.state=copy[st]?st:'connecting';
  document.getElementById('status').textContent=copy[document.body.dataset.state];
}})();
</script>
</body></html>
"""


def motion_demo():
    d = connecting_svg("dark", "live")
    l = connecting_svg("light", "livel")
    frames = ""
    for i, t in enumerate((0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.45)):
        frames += f'<figure>{connecting_svg("dark", f"f{i}", paused_at=t)}<figcaption>{int(t*1000)} ms</figcaption></figure>'
    return f"""<!doctype html><html lang="pl"><head><meta charset="utf-8"><title>AirLinx — animacja „łączenie”</title>
<style>body{{margin:0;background:#000;color:#A3A29E;font:13px Inter,system-ui,sans-serif;padding:32px}}
h1{{color:#F3F2EF;font:600 18px Inter;margin:0 0 6px}} p{{margin:0 0 20px}}
.row{{display:flex;gap:24px;align-items:center;flex-wrap:wrap}} svg{{width:140px;height:140px}}
.light{{background:#F2F2F0;border-radius:14px;padding:12px}}
figure{{margin:0;text-align:center}} figure svg{{width:120px;height:120px}} figcaption{{font:11px 'JetBrains Mono',monospace}}
code{{color:#3DF2A0}}</style></head><body>
<h1>Animacja „łączenie”</h1><p>Romb wylatuje z lewego szewronu, przechodzi przez szczelinę i znika w prawym. Szewrony delikatnie „podają” klatkę. Pętla 1,6 s, <code>cubic-bezier(.65,0,.35,1)</code>. Przy <code>prefers-reduced-motion: reduce</code> romb stoi w miejscu i tylko powoli pulsuje (opacity).</p>
<div class="row">{d}<div class="light">{l}</div></div>
<h1 style="margin-top:28px">Klatki</h1><div class="row">{frames}</div>
</body></html>"""


STATE_PL = {"idle": "bezczynny", "streaming": "transmisja", "error": "błąd"}


def preview(pngs):
    L = FIN / "logo"
    tray_rows = ""
    for N in (16, 22, 24):
        for mode, fg, bg in (("d", "#fff", "#242424"), ("l", "#2e3436", "#f6f5f4")):
            cells = ""
            for st in STATE_PL:
                svg = (FIN / "icons/hicolor" / f"{N}x{N}" / "status" / f"airlinx-{st}-symbolic.svg").read_text().replace('fill="#2e3436"', f'fill="{fg}"')
                svg = re.sub(r"<title>.*?</title>", "", svg)
                big = svg.replace(f'width="{N}" height="{N}"', f'width="{N*5}" height="{N*5}" shape-rendering="crispEdges"')
                cells += f'<div class="tc" style="background:{bg}">{svg}<span class="z">{big}</span></div>'
            tray_rows += f'<div class="trow"><div class="n">{N if mode=="d" else ""}</div>{cells}</div>'
    tray_png = {N: {st: png_data_uri(pngs[(N, st)]) for st in STATE_PL} for N in (16, 22, 24)}
    pix = "".join(f'<div class="tc" style="background:#242424"><img src="{tray_png[16][st]}" width="16"><img class="px" src="{tray_png[16][st]}" width="80"></div>' for st in STATE_PL)
    ladder = "".join(f'<figure><img src="{png_data_uri(FIN / "icons/hicolor" / f"{n}x{n}" / "apps" / "airlinx.png")}" width="{min(n,128)}"><figcaption>{n}</figcaption></figure>'
                     for n in (512, 256, 128, 64, 48, 32, 24, 22, 16))
    px16 = "".join(f'<img class="px" src="{png_data_uri(FIN / "icons/hicolor" / f"{n}x{n}" / "apps" / "airlinx.png")}" width="{n*6}">' for n in (16, 22, 24))
    return f"""<!doctype html><html lang="pl"><head><meta charset="utf-8"><title>AirLinx — kit produkcyjny</title><style>
body{{margin:0;background:#0A0A0A;color:#F3F2EF;font:14px/1.5 Inter,system-ui,sans-serif;padding:44px}}
h1{{font:800 30px 'JetBrains Mono',monospace;letter-spacing:-.03em;margin:0 0 4px}} .sub{{color:#A3A29E;margin:0 0 10px}}
h2{{font:600 11px 'JetBrains Mono',monospace;letter-spacing:.14em;text-transform:uppercase;color:#7E7D7A;margin:34px 0 12px}}
.g2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .g4{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
.p{{border-radius:14px;padding:24px;display:flex;align-items:center;justify-content:center;min-height:150px}}
.p svg{{max-width:100%;max-height:150px;width:auto;height:auto}}
.k{{background:#000;box-shadow:inset 0 0 0 1px #222}} .w{{background:#fff}} .lg{{background:#F2F2F0}}
.ladder{{display:flex;gap:18px;align-items:flex-end;padding:22px;border-radius:14px;background:radial-gradient(120% 140% at 20% 0%,#2e3240,#121318 60%,#0b0b0e)}}
figure{{margin:0;display:flex;flex-direction:column;align-items:center;gap:6px}} figcaption{{font:10px 'JetBrains Mono',monospace;color:#8b8b95}}
.px{{image-rendering:pixelated}}
.trow{{display:grid;grid-template-columns:40px repeat(3,1fr);gap:10px;margin-bottom:8px}}
.trow .n{{font:11px 'JetBrains Mono',monospace;color:#7E7D7A;align-self:center}}
.tc{{border-radius:10px;padding:10px 14px;display:flex;align-items:center;gap:18px}}
.th{{display:grid;grid-template-columns:40px repeat(3,1fr);gap:10px;font:11px 'JetBrains Mono',monospace;color:#7E7D7A;margin-bottom:6px}}
img.tv{{border-radius:10px;width:100%;background:#000;box-shadow:0 0 0 6px #111,0 0 0 7px #2a2a2a}}
.note{{color:#7E7D7A;font-size:12px;margin-top:8px}}
</style></head><body>
<h1>airlinx · kit produkcyjny</h1><p class="sub">Kierunek C „Styk”. Znak, logotypy, pole ochronne, ikony hicolor, tray symbolic, ekran TV i animacja.</p>
<h2>Znak · jeden kolor · negatyw · kolor</h2>
<div class="g4"><div class="p w">{inline(L/'airlinx-symbol-black.svg')}</div><div class="p k">{inline(L/'airlinx-symbol-white.svg')}</div>
<div class="p k">{inline(L/'airlinx-symbol-dark.svg')}</div><div class="p lg">{inline(L/'airlinx-symbol-light.svg')}</div></div>
<h2>Wersje poziome i pionowe</h2>
<div class="g2"><div class="p k">{inline(L/'airlinx-lockup-horizontal-dark.svg')}</div><div class="p lg">{inline(L/'airlinx-lockup-horizontal-light.svg')}</div>
<div class="p k" style="min-height:260px"><div style="height:240px;display:flex">{inline(L/'airlinx-lockup-vertical-dark.svg', style='max-height:240px')}</div></div>
<div class="p lg" style="min-height:260px"><div style="height:240px;display:flex">{inline(L/'airlinx-lockup-vertical-light.svg', style='max-height:240px')}</div></div></div>
<h2>Pole ochronne · 2X, X = szerokość rombu</h2>
<div class="g2"><div class="p w">{inline(L/'construction-clearspace-horizontal.svg')}</div><div class="p w" style="min-height:260px"><div style="height:240px;display:flex">{inline(L/'construction-clearspace-vertical.svg', style='max-height:240px')}</div></div></div>
<h2>Ikona aplikacji · hicolor 512 → 16 (prawdziwe piksele)</h2><div class="ladder">{ladder}</div>
<div class="ladder" style="margin-top:10px;gap:28px">{px16}</div>
<p class="note">16, 22 i 24 px są ręcznie dopasowane do siatki pikseli (skosy 2:1). 32 i 48 px używają wersji small cut, a od 64 px wzwyż jest master.</p>
<h2>Tray symbolic · bezczynny / transmisja / błąd · 1× i 5×</h2>
<div class="th"><div></div><div>bezczynny</div><div>transmisja</div><div>błąd</div></div>{tray_rows}
<p class="note">Rasteryzacja 16 px (PNG, 5× bez wygładzania):</p><div class="trow"><div></div>{pix}</div>
<h2>Ekran TV · splash/tv-splash.html (connecting / waiting-permission)</h2>
<div class="g2"><img class="tv" src="{png_data_uri(SHOTS / 'tv-splash-connecting.png')}"><img class="tv" src="{png_data_uri(SHOTS / 'tv-splash-waiting-permission.png')}"></div>
<h2>Animacja „łączenie” · motion/</h2><img src="{png_data_uri(SHOTS / 'connecting-frames.png')}" style="width:100%;border-radius:12px">
</body></html>"""


def main():
    copy_fonts()
    write = lambda rel, t: (FIN / rel).parent.mkdir(parents=True, exist_ok=True) or (FIN / rel).write_text(t)  # noqa
    write("splash/tv-splash.html", splash_html())
    write("motion/connecting-demo.html", motion_demo())
    src = json.load(open(ROOT / "_build" / ".icon_sources.json"))
    tmp = ROOT / "_build" / "tmp"
    pngs = {}
    with browser() as b:
        for n, s in src.items():
            n = int(n)
            svg_to_png(b, ROOT / s, n, FIN / "icons" / "hicolor" / f"{n}x{n}" / "apps" / "airlinx.png")
        for N in (16, 22, 24):
            for st in STATE_PL:
                p = tmp / f"t{N}{st}.svg"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text((FIN / "icons/hicolor" / f"{N}x{N}" / "status" / f"airlinx-{st}-symbolic.svg").read_text().replace('fill="#2e3436"', 'fill="#ffffff"'))
                svg_to_png(b, p, N, tmp / f"t{N}{st}.png", bg="#242424")
                pngs[(N, st)] = tmp / f"t{N}{st}.png"
        shot(b, FIN / "motion" / "connecting-demo.html", SHOTS / "connecting-frames.png", 1200)
        import subprocess
        subprocess.run(["convert", str(SHOTS / "connecting-frames.png"), "-crop", "1200x620+0+0", "+repage",
                        str(SHOTS / "connecting-frames.png")], check=False)
        for st in ("connecting", "waiting-permission"):
            page = b.new_page(viewport={"width": 1920, "height": 1080})
            page.goto((FIN / "splash" / "tv-splash.html").as_uri() + f"?state={st}")
            page.wait_for_timeout(2400)
            page.screenshot(path=str(SHOTS / f"tv-splash-{st}.png"))
            page.close()
        page = b.new_page(viewport={"width": 1920, "height": 1080}, reduced_motion="reduce")
        page.goto((FIN / "splash" / "tv-splash.html").as_uri())
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "tv-splash-reduced-motion.png"))
        page.close()
        write("preview.html", preview(pngs))
        shot(b, FIN / "preview.html", SHOTS / "kit-preview.png", 1400)
    shutil.rmtree(tmp, ignore_errors=True)
    print("rendered")


if __name__ == "__main__":
    main()
