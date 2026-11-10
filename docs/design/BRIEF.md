# AirLinx — Design Brief (shared by all design agents)

## Product
AirLinx is a Linux desktop app (Zorin OS 17 / GNOME, X11) that sends the PC's screen and media to a TV on the home LAN.
Primary TV: Samsung S85F OLED 65" "Salon" (4K, Tizen). Protocols under the hood: Google Cast (native mirroring + media), DLNA, TV built-in browser (WebRTC/MSE). The user never needs to know protocol names unless they open "Advanced".

UI language: **Polish** (all copy in Polish, with correct diacritics). Example labels: "Udostępnij ekran", "Odtwórz plik", "Zatrzymaj", "Opóźnienie", "Jakość", "Dźwięk na TV", "Szukam telewizorów…".

## Two modes
1. **Mirror ("Ekran")** — share a whole monitor or a single window, low latency, audio to TV (toggle). Shows live stats: latency (ms), FPS, bitrate, resolution, protocol (small).
   Sub-option: "Tryb gry / pracy" (lowest latency) vs "Tryb kina" (best quality, higher latency).
2. **Media ("Multimedia")** — play local files (video/music/photos) or a URL on the TV in full quality (4K/HDR when possible). Drag & drop files onto the window, a queue, transport controls (play/pause, seek, volume, next), subtitles track picker.

## Key screens / states (design all of them)
- First run / discovery: scanning animation, list of found TVs (name, model, IP, capabilities icons), "Dodaj ręcznie (IP)".
- Main window: selected TV header (name, status dot, connected/idle), mode switch (Ekran / Multimedia), primary big action.
- Source picker for mirror: monitors (thumbnails) and windows (thumbnails grid).
- Live session: big preview thumbnail, stats, stop button, quality/latency slider, audio toggle.
- Media: drop zone (empty state), queue list with thumbnails & durations, now-playing bar with controls.
- Settings: quality presets, encoder (NVENC auto), audio source, start with system, tray behavior, Advanced (protocol choice: Auto / Google Cast / DLNA / Przeglądarka TV).
- Errors: TV not found, TV asked for permission ("Zaakceptuj połączenie na telewizorze"), connection lost (reconnecting).
- Tray menu (GNOME AppIndicator — native menu, text only + icon): status line, "Udostępnij ekran → Salon", "Zatrzymaj", "Otwórz AirLinx", "Zakończ". Tray icon states: idle, streaming, error (monochrome symbolic, 16/22/24 px).

## Constraints
- Window default 1100×720, min 860×560, resizable. Rendered inside WebKit2GTK 2.4x (modern CSS ok: grid, :has, container queries, backdrop-filter — keep blur light for perf). No external network: **no CDN, no Google Fonts** — use system fonts Inter and JetBrains Mono (installed), or bundle files locally.
- Desktop app, mouse + keyboard; full keyboard navigation, visible focus, WCAG AA contrast.
- Respect dark/light: dark is primary (TV/OLED vibe), light must exist.
- Feels like a premium native app (think Apple AirPlay / Raycast / Linear quality), NOT a web landing page. No hero marketing sections, no lorem ipsum, no stock gradients/blobs ("AI slop").
- Icons: inline SVG (own or Lucide-style drawn inline), no icon fonts from CDN.

## Deliverable format for mockup agents
- Self-contained `index.html` (+ optional local css/js/svg in the same folder) that is an **interactive clickable mock**: all screens/states above reachable (tabs/buttons; a small dev state switcher in a corner is fine).
- Realistic fake data: TV "Salon" Samsung S85F 192.168.0.20; second TV "Sypialnia" LG; monitors "DP-1 2560×1440", "HDMI-1 1920×1080"; files like "zorza-2026-2160p.mkv".
- Screenshots of key states (1100×720, dark + one light) via Playwright into `shots/`.
