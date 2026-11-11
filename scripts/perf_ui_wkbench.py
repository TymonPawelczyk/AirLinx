"""UI benchmark in a real WebKit2GTK WebView (same WebView settings as airlinx/app.py) on the mock bridge.

Measures rAF frame pacing, main-thread stalls (a 10 ms timer's drift) and the CPU of the WebKit processes
while the UI is idle, live (statistics + clock ticking) and live with the details panel, plus a scripted
"anim" sequence (go live, picker, menu, settings). Results: docs/perf/ui.md.

    DISPLAY=:0 .venv/bin/python scripts/perf_ui_wkbench.py <repo_root> <label> <out.json> [always|demand|never] [WxH,WxH]

Env: SCEN=idle,live,details,anim (default: the first three), CSS_INJECT / JS_INJECT / JS_POST (inject a
stylesheet / script before the scenario, to bisect a cost), WK_SET="enable-webgl=0,..." (WebKit settings).
The window opens on the real display for ~10 s per case. Sizes are logical pixels: on a 4K monitor with
scale 2 an "2048x1152" window is 4096x2304 device pixels. Needs PyGObject with WebKit2 4.1 and nvidia-smi
is optional (GPU load column).
"""
import json, os, sys, time, subprocess, pathlib
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib, Gdk

ROOT = pathlib.Path(sys.argv[1]); LABEL = sys.argv[2]; OUT = sys.argv[3]
POLICY = sys.argv[4] if len(sys.argv) > 4 else "always"
SIZES = [tuple(map(int, s.split("x"))) for s in (sys.argv[5] if len(sys.argv) > 5 else "1100x720,2560x1440,4096x2304").split(",")]
SCEN = os.environ.get("SCEN", "idle,live,details").split(",")
DUR = 6000

INJ = os.environ.get("CSS_INJECT", "")
JINJ = os.environ.get("JS_INJECT", "")
JPOST = os.environ.get("JS_POST", "")
JS = r"""
(async () => {
  %INJ%
  %JINJ%
  const sc = %SC%, dur = %DUR%;
  const $ = s => document.querySelector(s), sleep = ms => new Promise(r => setTimeout(r, ms));
  const waitFor = async (f, t=10000) => { const e = Date.now()+t; while (!f()) { if (Date.now()>e) throw new Error('timeout'); await sleep(50);} };
  try {
  await waitFor(() => $('.app').dataset.screen === 'work' && $('#preview-img').getAttribute('src'));
  if (sc === 'anim') {
    const fr0 = [], t00 = performance.now(); let last0 = t00, run0 = true;
    const tk = t => { fr0.push(t-last0); last0 = t; if (run0) requestAnimationFrame(tk); }; requestAnimationFrame(tk);
    await sleep(400); fr0.length = 0; window.__start = true;
    const segs = {};
    const seg = async (name, fn, ms) => { const i0 = fr0.length; await fn(); await sleep(ms); segs[name] = fr0.slice(i0+1); };
    await seg('go_live(power-on+glow)', async () => { $('#share-btn').click(); await waitFor(() => $('.app').dataset.session === 'live'); }, 1800);
    await seg('open_picker', async () => { $('.r-live [data-act=picker], [data-act=picker]').click(); }, 900);
    await seg('close_picker', async () => { document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true})); }, 500);
    await seg('open_menu', async () => { $('.tv-pill').click(); }, 600);
    await seg('close_menu', async () => { document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true})); }, 400);
    await seg('open_settings', async () => { $('[data-act=settings]').click(); }, 900);
    await seg('settings_tab', async () => { const t = [...document.querySelectorAll('.s-nav button')][1]; t && t.click(); }, 700);
    run0 = false;
    const out = {};
    for (const [k, f] of Object.entries(segs)) { const s = f.slice().sort((a,b)=>a-b); out[k] = {fps: +(f.length/(f.reduce((a,b)=>a+b,0)/1000)).toFixed(1), p95: +s[Math.min(s.length-1, Math.floor(s.length*.95))].toFixed(0), max: +Math.max(...f).toFixed(0), over33: f.filter(x=>x>33.4).length, n: f.length}; }
    window.webkit.messageHandlers.bench.postMessage(JSON.stringify({anim: out, fps: 0, frame_med: 0, frame_p95: 0, frame_p99: 0, frame_max: 0, over33: 0, frames: 0, stalls50: 0, stall_ms: 0, stall_max: 0, el: 0}));
    return;
  }
  if (sc !== 'idle') { $('#share-btn').click(); await waitFor(() => $('.app').dataset.session === 'live'); await sleep(2500);
     if (sc === 'details') { $('[data-act="details"]').click(); await sleep(1200); } }
  const fr = [], st = []; let last = performance.now(), run = true, lastT = performance.now();
  const tick = t => { fr.push(t-last); last = t; if (run) requestAnimationFrame(tick); }; requestAnimationFrame(tick);
  const iv = setInterval(() => { const n = performance.now(); st.push(n-lastT-10); lastT = n; }, 10);
  await sleep(500); fr.length = 0; st.length = 0;
  %JPOST%
  window.__start = true; await sleep(300);   // signal for host CPU sampling window start
  const t0 = performance.now(); await sleep(dur); const el = performance.now()-t0;
  run = false; clearInterval(iv);
  const f = fr.slice(1), s = f.slice().sort((a,b)=>a-b), q = p => s[Math.min(s.length-1, Math.floor(s.length*p))];
  const stalls = st.filter(x => x > 50);
  window.webkit.messageHandlers.bench.postMessage(JSON.stringify({fps: +(f.length/(f.reduce((a,b)=>a+b,0)/1000)).toFixed(1),
    frame_med:+q(.5).toFixed(1), frame_p95:+q(.95).toFixed(1), frame_p99:+q(.99).toFixed(1), frame_max:+Math.max(...f).toFixed(1),
    over33:f.filter(x=>x>33.4).length, frames:f.length, stalls50:stalls.length, stall_ms:Math.round(stalls.reduce((a,b)=>a+b,0)), stall_max:Math.round(Math.max(0,...st)), el:Math.round(el)}));
  } catch(e) { window.webkit.messageHandlers.bench.postMessage(JSON.stringify({error:String(e)})); }
})();
"""

def descendants(pid):
    out = subprocess.run(["ps", "-o", "pid=,ppid=,comm=", "-e"], capture_output=True, text=True).stdout.split("\n")
    kids = {}
    for l in out:
        p = l.split(None, 2)
        if len(p) == 3: kids.setdefault(int(p[1]), []).append((int(p[0]), p[2]))
    res = []; st = [pid]
    while st:
        x = st.pop()
        for c, n in kids.get(x, []): res.append((c, n)); st.append(c)
    return res

def cpu_ticks(pid):
    tot = {}
    for p, n in [(pid, "ui")] + descendants(pid):
        try:
            f = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
            tot[n] = tot.get(n, 0) + int(f[11]) + int(f[12])
        except Exception: pass
    return tot

def gpu_util():
    try: return int(subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip())
    except Exception: return -1

results = []
HZ = os.sysconf("SC_CLK_TCK")
queue = [(w, h, sc) for (w, h) in SIZES for sc in SCEN]

def next_case():
    if not queue:
        pathlib.Path(OUT).write_text(json.dumps(results, indent=1)); Gtk.main_quit(); return
    w, h, sc = queue.pop(0)
    win = Gtk.Window(); win.set_default_size(w, h); win.move(0, 0) 
    ucm = WebKit2.UserContentManager(); wv = WebKit2.WebView.new_with_user_content_manager(ucm)
    wv.set_background_color(Gdk.RGBA(0, 0, 0, 1)); s = wv.get_settings()
    s.set_allow_file_access_from_file_urls(True)
    s.set_hardware_acceleration_policy({"always": WebKit2.HardwareAccelerationPolicy.ALWAYS, "demand": WebKit2.HardwareAccelerationPolicy.ON_DEMAND, "never": WebKit2.HardwareAccelerationPolicy.NEVER}[POLICY])
    for kv in os.environ.get("WK_SET", "").split(","):
        if kv: k, v = kv.split("="); s.set_property(k, v == "1")
    state = {"cpu0": None, "t0": None, "gpu": []}
    ucm.register_script_message_handler("bench")
    def on_msg(_u, r):
        d = json.loads(r.get_js_value().to_string()) if hasattr(r, "get_js_value") else {}
        t1 = time.time(); c1 = cpu_ticks(os.getpid()); c0 = state["cpu0"] or {}
        cpu = {k: round((c1.get(k, 0) - c0.get(k, 0)) / HZ / (t1 - state["t0"]) * 100, 1) for k in c1}
        d.update(w=w, h=h, scenario=sc, policy=POLICY, cpu_pct=cpu, gpu_util=sorted(state["gpu"])[len(state["gpu"])//2] if state["gpu"] else -1)
        state["done"] = True
        print(json.dumps(d), flush=True); results.append(d); pathlib.Path(OUT).write_text(json.dumps(results, indent=1))
        win.destroy(); GLib.timeout_add(600, lambda: (next_case(), False)[1])
    ucm.connect("script-message-received::bench", on_msg)
    def poll():
        wv.run_javascript("window.__start === true", None, lambda src, res, _: None, None)
        return True
    def start_sampling():
        state["cpu0"] = cpu_ticks(os.getpid()); state["t0"] = time.time()
        for i in range(4): GLib.timeout_add(1200 * (i + 1), lambda: (state["gpu"].append(gpu_util()), False)[1])
        return False
    def on_load(wv_, ev):
        if ev == WebKit2.LoadEvent.FINISHED:
            wv.run_javascript(JS.replace("%INJ%", "const __s=document.createElement('style');__s.textContent="+json.dumps(INJ)+";document.head.appendChild(__s);" if INJ else "").replace("%JINJ%", JINJ).replace("%JPOST%", JPOST).replace("%SC%", json.dumps(sc)).replace("%DUR%", str(DUR)), None, None, None)
            # the in-page script waits ~(waits) before the measurement window; sample CPU over the same window
            lead = {"anim": 400, "idle": 600, "live": 600 + 2500 + 500, "details": 600 + 2500 + 1200 + 500}[sc] + 800
            GLib.timeout_add(lead, start_sampling)
    wv.connect("load-changed", on_load)
    win.add(wv); win.show_all()
    wv.load_uri((ROOT / "ui/index.html").as_uri() + "?mock")
    def guard():
        if state.get("done"): return False
        print("TIMEOUT", w, h, sc, flush=True); results.append(dict(w=w, h=h, scenario=sc, policy=POLICY, timeout=True))
        pathlib.Path(OUT).write_text(json.dumps(results, indent=1)); win.destroy(); GLib.timeout_add(600, lambda: (next_case(), False)[1]); return False
    GLib.timeout_add(120000, guard)

GLib.idle_add(lambda: (next_case(), False)[1])
Gtk.main()
"""WebKit2GTK UI bench: real WebView (same settings as airlinx/app.py), mock bridge, in-page probes + /proc CPU.
usage: wkbench.py <repo_root> <label> <out.json> [policy=always|demand|never] [sizes=WxH,WxH]"""
import json, os, sys, time, subprocess, pathlib
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib, Gdk

ROOT = pathlib.Path(sys.argv[1]); LABEL = sys.argv[2]; OUT = sys.argv[3]
POLICY = sys.argv[4] if len(sys.argv) > 4 else "always"
SIZES = [tuple(map(int, s.split("x"))) for s in (sys.argv[5] if len(sys.argv) > 5 else "1100x720,2560x1440,4096x2304").split(",")]
SCEN = os.environ.get("SCEN", "idle,live,details").split(",")
DUR = 6000

INJ = os.environ.get("CSS_INJECT", "")
JINJ = os.environ.get("JS_INJECT", "")
JPOST = os.environ.get("JS_POST", "")
JS = r"""
(async () => {
  %INJ%
  %JINJ%
  const sc = %SC%, dur = %DUR%;
  const $ = s => document.querySelector(s), sleep = ms => new Promise(r => setTimeout(r, ms));
  const waitFor = async (f, t=10000) => { const e = Date.now()+t; while (!f()) { if (Date.now()>e) throw new Error('timeout'); await sleep(50);} };
  try {
  await waitFor(() => $('.app').dataset.screen === 'work' && $('#preview-img').getAttribute('src'));
  if (sc === 'anim') {
    const fr0 = [], t00 = performance.now(); let last0 = t00, run0 = true;
    const tk = t => { fr0.push(t-last0); last0 = t; if (run0) requestAnimationFrame(tk); }; requestAnimationFrame(tk);
    await sleep(400); fr0.length = 0; window.__start = true;
    const segs = {};
    const seg = async (name, fn, ms) => { const i0 = fr0.length; await fn(); await sleep(ms); segs[name] = fr0.slice(i0+1); };
    await seg('go_live(power-on+glow)', async () => { $('#share-btn').click(); await waitFor(() => $('.app').dataset.session === 'live'); }, 1800);
    await seg('open_picker', async () => { $('.r-live [data-act=picker], [data-act=picker]').click(); }, 900);
    await seg('close_picker', async () => { document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true})); }, 500);
    await seg('open_menu', async () => { $('.tv-pill').click(); }, 600);
    await seg('close_menu', async () => { document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true})); }, 400);
    await seg('open_settings', async () => { $('[data-act=settings]').click(); }, 900);
    await seg('settings_tab', async () => { const t = [...document.querySelectorAll('.s-nav button')][1]; t && t.click(); }, 700);
    run0 = false;
    const out = {};
    for (const [k, f] of Object.entries(segs)) { const s = f.slice().sort((a,b)=>a-b); out[k] = {fps: +(f.length/(f.reduce((a,b)=>a+b,0)/1000)).toFixed(1), p95: +s[Math.min(s.length-1, Math.floor(s.length*.95))].toFixed(0), max: +Math.max(...f).toFixed(0), over33: f.filter(x=>x>33.4).length, n: f.length}; }
    window.webkit.messageHandlers.bench.postMessage(JSON.stringify({anim: out, fps: 0, frame_med: 0, frame_p95: 0, frame_p99: 0, frame_max: 0, over33: 0, frames: 0, stalls50: 0, stall_ms: 0, stall_max: 0, el: 0}));
    return;
  }
  if (sc !== 'idle') { $('#share-btn').click(); await waitFor(() => $('.app').dataset.session === 'live'); await sleep(2500);
     if (sc === 'details') { $('[data-act="details"]').click(); await sleep(1200); } }
  const fr = [], st = []; let last = performance.now(), run = true, lastT = performance.now();
  const tick = t => { fr.push(t-last); last = t; if (run) requestAnimationFrame(tick); }; requestAnimationFrame(tick);
  const iv = setInterval(() => { const n = performance.now(); st.push(n-lastT-10); lastT = n; }, 10);
  await sleep(500); fr.length = 0; st.length = 0;
  %JPOST%
  window.__start = true; await sleep(300);   // signal for host CPU sampling window start
  const t0 = performance.now(); await sleep(dur); const el = performance.now()-t0;
  run = false; clearInterval(iv);
  const f = fr.slice(1), s = f.slice().sort((a,b)=>a-b), q = p => s[Math.min(s.length-1, Math.floor(s.length*p))];
  const stalls = st.filter(x => x > 50);
  window.webkit.messageHandlers.bench.postMessage(JSON.stringify({fps: +(f.length/(f.reduce((a,b)=>a+b,0)/1000)).toFixed(1),
    frame_med:+q(.5).toFixed(1), frame_p95:+q(.95).toFixed(1), frame_p99:+q(.99).toFixed(1), frame_max:+Math.max(...f).toFixed(1),
    over33:f.filter(x=>x>33.4).length, frames:f.length, stalls50:stalls.length, stall_ms:Math.round(stalls.reduce((a,b)=>a+b,0)), stall_max:Math.round(Math.max(0,...st)), el:Math.round(el)}));
  } catch(e) { window.webkit.messageHandlers.bench.postMessage(JSON.stringify({error:String(e)})); }
})();
"""

def descendants(pid):
    out = subprocess.run(["ps", "-o", "pid=,ppid=,comm=", "-e"], capture_output=True, text=True).stdout.split("\n")
    kids = {}
    for l in out:
        p = l.split(None, 2)
        if len(p) == 3: kids.setdefault(int(p[1]), []).append((int(p[0]), p[2]))
    res = []; st = [pid]
    while st:
        x = st.pop()
        for c, n in kids.get(x, []): res.append((c, n)); st.append(c)
    return res

def cpu_ticks(pid):
    tot = {}
    for p, n in [(pid, "ui")] + descendants(pid):
        try:
            f = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
            tot[n] = tot.get(n, 0) + int(f[11]) + int(f[12])
        except Exception: pass
    return tot

def gpu_util():
    try: return int(subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip())
    except Exception: return -1

results = []
HZ = os.sysconf("SC_CLK_TCK")
queue = [(w, h, sc) for (w, h) in SIZES for sc in SCEN]

def next_case():
    if not queue:
        pathlib.Path(OUT).write_text(json.dumps(results, indent=1)); Gtk.main_quit(); return
    w, h, sc = queue.pop(0)
    win = Gtk.Window(); win.set_default_size(w, h); win.move(0, 0) 
    ucm = WebKit2.UserContentManager(); wv = WebKit2.WebView.new_with_user_content_manager(ucm)
    wv.set_background_color(Gdk.RGBA(0, 0, 0, 1)); s = wv.get_settings()
    s.set_allow_file_access_from_file_urls(True)
    s.set_hardware_acceleration_policy({"always": WebKit2.HardwareAccelerationPolicy.ALWAYS, "demand": WebKit2.HardwareAccelerationPolicy.ON_DEMAND, "never": WebKit2.HardwareAccelerationPolicy.NEVER}[POLICY])
    for kv in os.environ.get("WK_SET", "").split(","):
        if kv: k, v = kv.split("="); s.set_property(k, v == "1")
    state = {"cpu0": None, "t0": None, "gpu": []}
    ucm.register_script_message_handler("bench")
    def on_msg(_u, r):
        d = json.loads(r.get_js_value().to_string()) if hasattr(r, "get_js_value") else {}
        t1 = time.time(); c1 = cpu_ticks(os.getpid()); c0 = state["cpu0"] or {}
        cpu = {k: round((c1.get(k, 0) - c0.get(k, 0)) / HZ / (t1 - state["t0"]) * 100, 1) for k in c1}
        d.update(w=w, h=h, scenario=sc, policy=POLICY, cpu_pct=cpu, gpu_util=sorted(state["gpu"])[len(state["gpu"])//2] if state["gpu"] else -1)
        state["done"] = True
        print(json.dumps(d), flush=True); results.append(d); pathlib.Path(OUT).write_text(json.dumps(results, indent=1))
        win.destroy(); GLib.timeout_add(600, lambda: (next_case(), False)[1])
    ucm.connect("script-message-received::bench", on_msg)
    def poll():
        wv.run_javascript("window.__start === true", None, lambda src, res, _: None, None)
        return True
    def start_sampling():
        state["cpu0"] = cpu_ticks(os.getpid()); state["t0"] = time.time()
        for i in range(4): GLib.timeout_add(1200 * (i + 1), lambda: (state["gpu"].append(gpu_util()), False)[1])
        return False
    def on_load(wv_, ev):
        if ev == WebKit2.LoadEvent.FINISHED:
            wv.run_javascript(JS.replace("%INJ%", "const __s=document.createElement('style');__s.textContent="+json.dumps(INJ)+";document.head.appendChild(__s);" if INJ else "").replace("%JINJ%", JINJ).replace("%JPOST%", JPOST).replace("%SC%", json.dumps(sc)).replace("%DUR%", str(DUR)), None, None, None)
            # the in-page script waits ~(waits) before the measurement window; sample CPU over the same window
            lead = {"anim": 400, "idle": 600, "live": 600 + 2500 + 500, "details": 600 + 2500 + 1200 + 500}[sc] + 800
            GLib.timeout_add(lead, start_sampling)
    wv.connect("load-changed", on_load)
    win.add(wv); win.show_all()
    wv.load_uri((ROOT / "ui/index.html").as_uri() + "?mock")
    def guard():
        if state.get("done"): return False
        print("TIMEOUT", w, h, sc, flush=True); results.append(dict(w=w, h=h, scenario=sc, policy=POLICY, timeout=True))
        pathlib.Path(OUT).write_text(json.dumps(results, indent=1)); win.destroy(); GLib.timeout_add(600, lambda: (next_case(), False)[1]); return False
    GLib.timeout_add(120000, guard)

GLib.idle_add(lambda: (next_case(), False)[1])
Gtk.main()
