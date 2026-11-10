/* AirLinx JS bridge.
 *
 *   airlinx.call(method, params) -> Promise<result>   (rejects with Error{code, message})
 *   airlinx.on(event, cb)        -> unsubscribe()
 *   airlinx.native               -> true inside the GTK app (WebKit message handler present)
 *
 * Native: window.webkit.messageHandlers.airlinx.postMessage({id, method, params});
 *         Python answers via airlinx._resolve(id, result, error) and pushes airlinx._emit(event, payload).
 * Browser: a MOCK backend with realistic fake data (same JSON shapes as Python's to_json),
 *          so the UI can be developed standalone. Mock knobs: URL ?mock=nodevices|multi|busy|slow|noapps|nomic|unverified|cpu|wayland,
 *          or window.airlinx.mock.{setTvBusy(b), setCastUnverified(b), failNext(method, code), setDevices([...]),
 *          setAudioApps([...])}.
 *
 * Contract: docs/ARCHITECTURE.md. Payload field names are snake_case (Python dataclasses).
 */
(function () {
  'use strict';
  if (window.airlinx && window.airlinx.__bridge) return; // idempotent

  const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.airlinx;
  const listeners = new Map();
  const pending = new Map();
  let seq = 0;

  function makeError(err) {
    const e = new Error((err && err.message) || 'Wystąpił nieoczekiwany błąd.');
    e.code = (err && err.code) || 'internal';
    return e;
  }

  function emit(event, payload) {
    const set = listeners.get(event);
    if (set) for (const cb of [...set]) { try { cb(payload); } catch (e) { console.error('[airlinx] listener', event, e); } }
    const any = listeners.get('*');
    if (any) for (const cb of [...any]) { try { cb(event, payload); } catch (e) { console.error(e); } }
  }

  const api = {
    __bridge: true,
    native: !!handler,
    call(method, params) {
      const id = ++seq;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject, method });
        if (handler) {
          try { handler.postMessage({ id, method, params: params || {} }); }
          catch (e) { pending.delete(id); reject(makeError({ code: 'bridge', message: String(e) })); }
        } else {
          mock.call(method, params || {}).then(
            r => api._resolve(id, r === undefined ? null : JSON.parse(JSON.stringify(r)), null),
            e => api._resolve(id, null, { code: e.code || 'internal', message: e.message }));
        }
      });
    },
    on(event, cb) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event).add(cb);
      return () => api.off(event, cb);
    },
    off(event, cb) { const s = listeners.get(event); if (s) s.delete(cb); },
    _resolve(id, result, error) {
      const p = pending.get(id);
      if (!p) return;
      pending.delete(id);
      if (error) p.reject(makeError(error)); else p.resolve(result);
    },
    _emit(event, payload) { emit(event, payload); },
  };
  window.airlinx = api;

  // Native window helpers for an HTML title bar (no-ops in the browser).
  // Usage: header.addEventListener('mousedown', e => e.button === 0 && !e.target.closest('button') && airlinx.startDrag(e));
  api.startDrag = (e) => handler && api.call('window.startDrag', { button: (e && e.button + 1) || 1 }).catch(() => {});
  api.minimize = () => handler && api.call('window.minimize').catch(() => {});
  api.toggleMaximize = () => handler && api.call('window.toggleMaximize').catch(() => {});
  api.close = () => handler ? api.call('window.close').catch(() => {}) : api.call('window.minimizeToTray');

  if (handler) return; // ------------------------------------------------ native done

  /* ================================================================== MOCK ======== */
  const q = new URLSearchParams(location.search);
  const flags = new Set((q.get('mock') || '').split(',').filter(Boolean));
  const SLOW = flags.has('slow') ? 3 : 1;
  const sleep = ms => new Promise(r => setTimeout(r, ms * SLOW));
  const rnd = (a, b) => a + Math.random() * (b - a);
  const uid = () => Math.random().toString(16).slice(2, 14);
  const clone = o => JSON.parse(JSON.stringify(o));
  const err = (code, message) => Object.assign(new Error(message), { code });

  // One TV by default (screenshots); ?mock=multi adds a second one for device-switching tests.
  const DEVICES = [
    { id: 'cast-5f1c0e2a9b7d4c11', name: 'Salon', model: 'Samsung S85F', ip: '192.168.0.20', cast_port: 8009,
      dlna_location: 'http://192.168.0.20:9197/dmr', caps: ['cast_media', 'cast_mirror', 'dlna'], online: true },
  ];
  if (flags.has('multi')) DEVICES.push(
    { id: 'cast-a03e77c1d2f84e90', name: 'Sypialnia', model: 'Chromecast with Google TV', ip: '192.168.0.23',
      cast_port: 8009, dlna_location: null, caps: ['cast_media', 'cast_mirror'], online: true });

  function thumb(label, w, h, hue) {
    try {
      const c = document.createElement('canvas');
      const tw = 320, th = Math.round(320 * h / w);
      c.width = tw; c.height = th;
      const g = c.getContext('2d');
      const grd = g.createLinearGradient(0, 0, tw, th);
      grd.addColorStop(0, `hsl(${hue},45%,18%)`); grd.addColorStop(1, `hsl(${(hue + 50) % 360},55%,8%)`);
      g.fillStyle = grd; g.fillRect(0, 0, tw, th);
      g.fillStyle = 'rgba(255,255,255,.08)';
      for (let i = 0; i < 6; i++) g.fillRect(16 + i * 48, 20, 36, th - 40);
      g.fillStyle = 'rgba(230,238,234,.85)'; g.font = '600 18px Inter, sans-serif'; g.fillText(label, 14, th - 14);
      return c.toDataURL('image/png');
    } catch (_) { return null; }
  }

  const SOURCES = [
    { id: 'monitor:DP-4', kind: 'monitor', name: 'DP-4 · 4096×2304 (główny)', geometry: [0, 420, 4096, 2304], xid: null, _hue: 152 },
    { id: 'monitor:DP-2.8', kind: 'monitor', name: 'DP-2.8 · 5120×2880', geometry: [4096, 0, 5120, 2880], xid: null, _hue: 200 },
    { id: 'window:0x3a00007', kind: 'window', name: 'Firefox — YouTube', geometry: [120, 560, 2400, 1400], xid: 0x3a00007, _hue: 18 },
    { id: 'window:0x4c00003', kind: 'window', name: 'Visual Studio Code — AirLinx', geometry: [4200, 80, 3000, 2000], xid: 0x4c00003, _hue: 220 },
    { id: 'window:0x5e0000a', kind: 'window', name: 'Steam — Biblioteka', geometry: [600, 900, 2000, 1200], xid: 0x5e0000a, _hue: 260 },
  ];

  const DEFAULT_OPTS = { preset: 'game', max_height: 1080, fps: 60, bitrate: 12000000, audio: true, target_delay_ms: 100,
    audio_source: 'system', audio_mic: 'default', audio_app: null, audio_mic_name: null, audio_app_name: null };
  const MIRROR_KEYS = ['preset', 'max_height', 'fps', 'bitrate', 'audio', 'target_delay_ms'];
  const settings = {
    version: 2, protocol: 'auto', last_device_id: DEVICES[0].id, manual_devices: [], close_to_tray: true,
    start_hidden: false, autostart: false, notifications: true,
    mirror: Object.fromEntries(MIRROR_KEYS.map(k => [k, DEFAULT_OPTS[k]])),
    audio: { source: 'system', mic: 'default', mic_name: null, app: null, app_name: null },
    last_source_id: 'monitor:DP-4', media: { volume: 0.8, auto_advance: true }, ui: {},
  };

  // core/audio.py list_devices(): microphones (no *.monitor) and apps playing right now
  const appIcon = (bg, glyph) => 'data:image/svg+xml;base64,' + btoa(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="11" fill="${bg}"/><text x="24" y="31" font-family="Inter,sans-serif" font-size="20" font-weight="600" text-anchor="middle" fill="#fff">${glyph}</text></svg>`);
  const MICS = flags.has('nomic') ? [] : [
    { name: 'alsa_input.usb-Blue_Microphones_Yeti_Stereo_Microphone-00.analog-stereo', label: 'Blue Yeti', default: true },
    { name: 'alsa_input.pci-0000_09_00.4.analog-stereo', label: 'Wbudowany mikrofon (karta dźwiękowa)', default: false },
  ];
  let APPS = flags.has('noapps') ? [] : [
    { key: 'firefox', name: 'Firefox', icon: 'firefox', icon_url: appIcon('#E66000', 'F'), streams: 2 },
    { key: 'spotify', name: 'Spotify', icon: 'spotify-client', icon_url: appIcon('#1DB954', 'S'), streams: 1 },
    { key: 'mpv', name: 'mpv', icon: null, icon_url: null, streams: 1 },
  ];
  const audioSpec = () => ({ source: settings.audio.source, mic: settings.audio.mic, mic_name: settings.audio.mic_name,
    app: settings.audio.app, app_name: settings.audio.app_name });
  const audioLabel = a => a.source === 'mic' ? `Mikrofon: ${a.mic_name || 'domyślny'}`
    : a.source === 'system_mic' ? 'System + mikrofon' : a.source === 'app' ? `Tylko ${a.app_name || a.app || 'aplikacja'}` : 'Dźwięk systemu';
  function resolveAudio(a) {     // controller._resolve_audio(): fall back to system sound with a notice
    let note = null, eff = a.source;
    if (a.source === 'app' && !APPS.some(x => x.key === a.app)) { eff = 'system'; note = `${a.app_name || a.app} nic teraz nie odtwarza. Wysyłam dźwięk systemu.`; }
    if ((a.source === 'mic' || a.source === 'system_mic') && !(a.mic === 'default' ? MICS.length : MICS.some(m => m.name === a.mic))) {
      eff = 'system'; note = MICS.length ? `Mikrofon „${a.mic_name || a.mic}” jest niedostępny. Wysyłam dźwięk systemu.` : 'Nie znaleziono mikrofonu. Wysyłam dźwięk systemu.';
    }
    return { source: eff, requested: a.source, label: audioLabel(eff === a.source ? a : { source: 'system' }), note };
  }

  const S = {
    devices: [], selected_device_id: null, discovering: true, mode: 'idle', protocol: 'auto',
    mirror: { state: 'idle', detail: null, device_id: null, source_id: null, protocol: null,
              options: clone(DEFAULT_OPTS), stats: null, started_at: null, audio: null },
    queue: [], playback: { state: 'idle', position: 0, duration: null, volume: 0.8, muted: false, item_id: null, error: null },
    media_protocol: null, settings, version: '0.1.0',
    encoder: flags.has('cpu') ? 'libx264' : 'h264_nvenc',   // controller: capture.probe_encoders()
    wayland: flags.has('wayland'),                          // controller: sources.is_wayland_session()
  };

  let tvBusy = flags.has('busy');
  let castUnverified = flags.has('unverified');   // pinned TV, Cast does not answer → ask before DLNA
  const UNVERIFIED_PL = 'Nie udało się potwierdzić tożsamości telewizora przez Google Cast. Wysłać przez DLNA bez weryfikacji?';
  const unverified = (method, params) => {
    emit('error', { code: 'cast_unverified', message: UNVERIFIED_PL, retry: { method, params: Object.assign({}, params, { protocol: 'dlna' }) } });
    return { ok: false, code: 'cast_unverified', message: UNVERIFIED_PL };
  };
  const failures = new Map();
  let statsTimer = null, playTimer = null, mirrorSeq = 0;

  const state = () => { S.settings = settings; S.protocol = settings.protocol; return clone(S); };
  const push = () => emit('state', state());
  const pushMirror = () => { emit('mirror.state', clone(S.mirror)); push(); };
  const pushQueue = () => { emit('media.queue', clone(S.queue)); push(); };
  const pushPlayback = () => emit('media.playback', clone(S.playback));
  const toast = m => emit('toast', { message: m });
  const device = () => {
    const d = S.devices.find(x => x.id === S.selected_device_id);
    if (!d) throw err(S.selected_device_id ? 'tv_not_found' : 'no_device',
      S.selected_device_id ? 'Nie znaleziono telewizora w sieci.' : 'Najpierw wybierz telewizor.');
    return d;
  };
  const source = id => {
    const s = id ? SOURCES.find(x => x.id === id) : SOURCES[0];
    if (!s) throw err('source_not_found', 'Nie znaleziono wybranego ekranu lub okna.');
    return s;
  };
  const pubSource = (s, thumbs) => ({ id: s.id, kind: s.kind, name: s.name, geometry: s.geometry, xid: s.xid,
    thumbnail_png: thumbs ? thumb(s.name.split(' ·')[0], s.geometry[2], s.geometry[3], s._hue) : null });

  function discover(delay) {
    S.discovering = true; push();
    setTimeout(() => {
      if (!flags.has('nodevices')) S.devices = clone(DEVICES);
      if (!S.selected_device_id && S.devices.some(d => d.id === settings.last_device_id)) S.selected_device_id = settings.last_device_id;
      S.discovering = false;
      emit('devices', clone(S.devices)); push();
    }, delay * SLOW);
  }
  discover(1200);

  function stopStats() { clearInterval(statsTimer); statsTimer = null; }
  function startStats() {
    stopStats();
    statsTimer = setInterval(() => {
      if (S.mirror.state !== 'live') return;
      const o = S.mirror.options;
      const h = Math.min(o.max_height, 2160), w = Math.round(h * 16 / 9);
      // Gra / praca as measured on the dev TV: ≈ 111 ms, 60 kl./s, ≈ 12 Mb/s, RTT ≈ 3 ms.
      // Kino: the receiver buffer is ≥ 400 ms (core/cast/mirror.py effective_options).
      const cap = rnd(7.5, 9), enc = rnd(4.5, 6), net = rnd(2.5, 4), tv = o.preset === 'cinema' ? rnd(400, 410) : rnd(93, 97);
      const st = {
        latency_ms: Math.round((cap + enc + net + tv) * 10) / 10, fps: Math.round(rnd(o.fps - 0.4, o.fps) * 10) / 10,
        bitrate: Math.round(o.bitrate * rnd(0.97, 1.01)), width: w, height: h,
        rtt_ms: Math.round(rnd(2.6, 3.4) * 10) / 10, loss: 0,
        protocol: S.mirror.protocol === 'dlna' ? 'dlna-ts' : 'cast-mirroring',
        encoder: S.encoder === 'libx264' ? 'libx264' : 'h264_nvenc + scale_cuda',   // capture.encoder_label
        stages: { capture: +cap.toFixed(1), encode: +enc.toFixed(1), network: +net.toFixed(1), tv: +tv.toFixed(1) },
      };
      S.mirror.stats = st;
      emit('mirror.stats', st);
    }, 500);
  }

  function setMirror(patch) { Object.assign(S.mirror, patch); pushMirror(); }

  async function mirrorStart(p) {
    const dev = device();
    const src = source(p.sourceId);
    if (S.mode === 'media') await mediaStop();
    if (S.mirror.state !== 'idle') mirrorStop();
    const opts = Object.assign(clone(S.mirror.options), p.options || {});
    if (tvBusy && !p.force) {
      emit('error', { code: 'tv_busy', message: 'Na telewizorze działa YouTube. Przerwać?', app: 'YouTube',
        retry: { method: 'mirror.start', params: { sourceId: src.id, options: opts, force: true } } });
      return { ok: false, code: 'tv_busy', message: 'Na telewizorze działa YouTube. Przerwać?' };
    }
    tvBusy = false;
    const castFirst = !p.protocol && settings.protocol === 'auto' && dev.caps.includes('cast_mirror') && dev.caps.includes('dlna');
    if (castUnverified && castFirst) {
      if (S.mode === 'mirror') S.mode = 'idle';
      return unverified('mirror.start', { sourceId: src.id, options: opts });
    }
    const my = ++mirrorSeq;
    const proto = p.protocol || (settings.protocol === 'dlna' || !dev.caps.includes('cast_mirror') ? 'dlna' : 'cast');
    S.mode = 'mirror';
    const audio = opts.audio ? resolveAudio(audioSpec()) : null;
    if (audio && audio.note) toast(audio.note);
    setMirror({ state: 'connecting', detail: null, device_id: dev.id, source_id: src.id, protocol: proto, options: opts, stats: null, started_at: null, audio });
    settings.last_source_id = src.id; MIRROR_KEYS.forEach(k => { settings.mirror[k] = opts[k]; });
    setTimeout(() => { if (my === mirrorSeq && S.mirror.state === 'connecting') setMirror({ state: 'waiting_tv' }); }, 350 * SLOW);
    setTimeout(() => {
      if (my !== mirrorSeq || S.mirror.state !== 'waiting_tv') return;
      setMirror({ state: 'live', started_at: Date.now() / 1000 });
      startStats();
    }, 1300 * SLOW);
    return { ok: true, protocol: proto };
  }

  function mirrorStop() {
    mirrorSeq++; stopStats();
    if (S.mode === 'mirror') S.mode = 'idle';
    setMirror({ state: 'idle', detail: null, stats: null, started_at: null, audio: null });
    return { ok: true };
  }

  const FAKE_FILES = [          // fictional names only: these end up in public screenshots
    { path: '/home/user/Wideo/wakacje-2026.mkv', duration: 2712 },
    { path: '/home/user/Wideo/spacer-nad-morzem.mp4', duration: 412 },
    { path: '/home/user/Muzyka/koncert-w-ogrodzie.flac', duration: 345 },
    { path: '/home/user/Obrazy/zorza.jpg', duration: null },
  ];
  const kindOf = p => /\.(mp3|flac|m4a|aac|ogg|opus|wav)$/i.test(p) ? 'audio' : /\.(jpe?g|png|gif|webp)$/i.test(p) ? 'image' : 'video';
  const titleOf = p => decodeURIComponent(p.split('/').pop() || p).replace(/\.[a-z0-9]{2,4}$/i, '');
  function mkItem(path, duration) {
    const kind = kindOf(path);
    return { id: uid(), path_or_url: path, title: titleOf(path),
      duration: duration !== undefined ? duration : (kind === 'image' ? null : Math.round(rnd(150, 3600))),
      kind, thumbnail_png: null, subtitles: kind === 'video' && /wakacje-2026/.test(path) ? [path.replace(/\.\w+$/, '.pl.srt'), path.replace(/\.\w+$/, '.en.srt')] : [],
      subtitle_track: null };
  }

  function stopPlayTimer() { clearInterval(playTimer); playTimer = null; }
  function startPlayTimer() {
    stopPlayTimer();
    let n = 0;
    playTimer = setInterval(() => {
      const pb = S.playback;
      if (pb.state !== 'playing') return;
      pb.position = Math.min((pb.position || 0) + 0.25, pb.duration || Infinity);
      if (pb.duration && pb.position >= pb.duration) {
        pb.state = 'ended'; pushPlayback(); stopPlayTimer();
        const idx = S.queue.findIndex(x => x.id === pb.item_id);
        if (settings.media.auto_advance && idx >= 0 && idx + 1 < S.queue.length) setTimeout(() => playItem(S.queue[idx + 1]), 400);
        else { S.mode = 'idle'; push(); }
        return;
      }
      if (++n % 2 === 0) pushPlayback();
    }, 250);
  }

  async function playItem(item, force, protocol) {
    const dev = device();
    if (S.mirror.state !== 'idle') mirrorStop();
    if (tvBusy && !force) {
      emit('error', { code: 'tv_busy', message: 'Na telewizorze działa Netflix. Przerwać?', app: 'Netflix',
        retry: { method: 'media.play', params: { id: item.id, force: true } } });
      return { ok: false, code: 'tv_busy', message: 'Na telewizorze działa Netflix. Przerwać?' };
    }
    tvBusy = false;
    if (castUnverified && !protocol && settings.protocol === 'auto' && dev.caps.includes('cast_media') && dev.caps.includes('dlna')) {
      return unverified('media.play', { id: item.id });
    }
    const proto = protocol || (settings.protocol === 'dlna' || !dev.caps.includes('cast_media') ? 'dlna' : 'cast');
    S.mode = 'media'; S.media_protocol = proto;
    S.playback = { state: 'loading', position: 0, duration: item.duration, volume: S.playback.volume, muted: false, item_id: item.id, error: null };
    pushPlayback(); push();
    await sleep(500);
    if (S.playback.item_id !== item.id) return { ok: true, protocol: proto, itemId: item.id };
    S.playback.state = item.kind === 'image' ? 'paused' : 'buffering'; pushPlayback();
    await sleep(300);
    if (S.playback.item_id !== item.id) return { ok: true, protocol: proto, itemId: item.id };
    if (item.kind !== 'image') { S.playback.state = 'playing'; startPlayTimer(); }
    pushPlayback(); push();
    return { ok: true, protocol: proto, itemId: item.id };
  }

  async function mediaStop() {
    stopPlayTimer();
    S.playback = Object.assign(S.playback, { state: 'idle', position: 0, item_id: null, error: null });
    S.media_protocol = null; if (S.mode === 'media') S.mode = 'idle';
    pushPlayback(); push();
    return { ok: true };
  }

  const cur = () => S.queue.findIndex(x => x.id === S.playback.item_id);
  const need = () => { if (S.playback.state === 'idle') throw err('not_playing', 'Nic teraz nie jest odtwarzane.'); };

  const M = {
    'app.getState': async () => state(),
    'app.quit': async () => { console.info('[airlinx mock] app.quit'); return { ok: true }; },
    'window.minimizeToTray': async () => { console.info('[airlinx mock] minimize to tray'); return { ok: true }; },
    'window.isNative': async () => false,
    'devices.list': async () => clone(S.devices),
    'devices.rescan': async () => { discover(1500); return { ok: true }; },
    'devices.addManual': async ({ ip }) => {
      await sleep(700);
      if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip || '')) throw err('tv_not_found', `Nie znaleziono telewizora w sieci. (${ip || 'brak IP'})`);
      let d = S.devices.find(x => x.ip === ip);
      if (!d) {
        d = { id: 'dlna-' + ip.replace(/\./g, ''), name: `TV ${ip}`, model: 'DLNA MediaRenderer', ip, cast_port: null,
              dlna_location: `http://${ip}:9197/dmr`, caps: ['dlna'], online: true };
        S.devices.push(d); settings.manual_devices.push(ip);
      }
      S.selected_device_id = d.id; settings.last_device_id = d.id;
      emit('devices', clone(S.devices)); push();
      return clone(d);
    },
    'devices.select': async ({ id }) => {
      if (!S.devices.some(d => d.id === id)) throw err('tv_not_found', 'Nie znaleziono telewizora w sieci.');
      S.selected_device_id = id; settings.last_device_id = id; push(); return state();
    },
    'devices.trustAgain': async ({ id }) => {
      const d = S.devices.find(x => x.id === id || x.ip === id);
      toast(`Zaufano ponownie: ${d ? d.name : 'telewizor'}. Połącz się jeszcze raz.`);
      return { ok: true, removed: true };
    },
    'sources.list': async ({ withThumbnails = true } = {}) => { await sleep(250); return SOURCES.map(s => pubSource(s, withThumbnails)); },
    'mirror.start': mirrorStart,
    'mirror.stop': async () => mirrorStop(),
    'mirror.update': async ({ options }) => {
      Object.assign(S.mirror.options, options || {}); MIRROR_KEYS.forEach(k => { settings.mirror[k] = S.mirror.options[k]; });
      if (S.mirror.state !== 'idle') S.mirror.audio = S.mirror.options.audio ? (S.mirror.audio || resolveAudio(audioSpec())) : null;
      pushMirror(); return clone(S.mirror);
    },
    'mirror.switchSource': async ({ sourceId }) => {
      const s = source(sourceId); settings.last_source_id = s.id; setMirror({ source_id: s.id }); return clone(S.mirror);
    },
    'media.add': async ({ paths = [], urls = [] } = {}) => {
      const items = [...paths, ...urls].filter(Boolean).map(p => mkItem(p));
      S.queue.push(...items); if (items.length) pushQueue(); return clone(items);
    },
    'media.pickFiles': async () => {
      await sleep(600);
      const items = FAKE_FILES.map(f => mkItem(f.path, f.duration));
      S.queue.push(...items); pushQueue(); return clone(items);
    },
    'media.remove': async ({ id }) => {
      if (!S.queue.some(x => x.id === id)) throw err('item_not_found', 'Nie ma takiej pozycji w kolejce.');
      if (S.playback.item_id === id) await mediaStop();
      S.queue = S.queue.filter(x => x.id !== id); pushQueue(); return clone(S.queue);
    },
    'media.reorder': async ({ ids = [] }) => {
      const by = new Map(S.queue.map(x => [x.id, x]));
      const next = ids.map(i => by.get(i)).filter(Boolean);
      S.queue = next.concat(S.queue.filter(x => !ids.includes(x.id))); pushQueue(); return clone(S.queue);
    },
    'media.play': async ({ id, force, protocol } = {}) => {
      const item = id ? S.queue.find(x => x.id === id) : S.queue[0];
      if (!item) throw err(id ? 'item_not_found' : 'queue_empty', id ? 'Nie ma takiej pozycji w kolejce.' : 'Kolejka jest pusta.');
      return playItem(item, force, protocol);
    },
    'media.pause': async () => { need(); S.playback.state = 'paused'; pushPlayback(); push(); return { ok: true }; },
    'media.resume': async () => { need(); S.playback.state = 'playing'; startPlayTimer(); pushPlayback(); push(); return { ok: true }; },
    'media.seek': async ({ seconds }) => {
      need(); S.playback.position = Math.max(0, Math.min(+seconds || 0, S.playback.duration || +seconds));
      if (S.playback.state === 'playing') { S.playback.state = 'buffering'; pushPlayback(); await sleep(250); S.playback.state = 'playing'; }
      pushPlayback(); return { ok: true };
    },
    'media.volume': async ({ value }) => {
      const v = Math.max(0, Math.min(1, +value)); S.playback.volume = v; settings.media.volume = v; pushPlayback(); return { ok: true, volume: v };
    },
    'media.next': async () => {
      const i = cur(); const item = S.queue[i + 1] || (i < 0 ? S.queue[0] : null);
      if (!item) throw err('queue_end', 'To już ostatnia pozycja w kolejce.'); return playItem(item);
    },
    'media.prev': async () => {
      if (S.playback.position > 5 && S.playback.state !== 'idle') { S.playback.position = 0; pushPlayback(); return { ok: true }; }
      const item = S.queue[cur() - 1];
      if (!item) throw err('queue_start', 'To pierwsza pozycja w kolejce.'); return playItem(item);
    },
    'media.stop': mediaStop,
    'media.subtitles': async ({ id, track }) => {
      const it = S.queue.find(x => x.id === id);
      if (!it) throw err('item_not_found', 'Nie ma takiej pozycji w kolejce.');
      if (typeof track === 'number') track = it.subtitles[track] || null;
      if (track && !it.subtitles.includes(track)) it.subtitles.push(track);
      it.subtitle_track = track || null; pushQueue(); return clone(it);
    },
    'settings.get': async () => clone(settings),
    'settings.set': async ({ patch = {} }) => {
      for (const [k, v] of Object.entries(patch)) {
        if (k === 'version') continue;
        if (v && typeof v === 'object' && !Array.isArray(v) && settings[k] && typeof settings[k] === 'object') Object.assign(settings[k], v);
        else settings[k] = v;
      }
      if (patch.mirror) Object.assign(S.mirror.options, patch.mirror);
      if (patch.audio) {
        const a = audioSpec();
        Object.assign(S.mirror.options, { audio_source: a.source, audio_mic: a.mic, audio_app: a.app, audio_mic_name: a.mic_name, audio_app_name: a.app_name });
        if (S.mirror.state !== 'idle' && S.mirror.options.audio) {
          if (S.mirror.protocol === 'dlna') {        // DLNA: no live switch, applies at the next start
            S.mirror.audio = Object.assign({}, S.mirror.audio, { pending: true });
            toast('Nowe źródło dźwięku zadziała przy następnym udostępnianiu.');
          } else {
            S.mirror.audio = resolveAudio(a);
            if (S.mirror.audio.note) toast(S.mirror.audio.note);
          }
        }
        emit('mirror.state', clone(S.mirror));
      }
      push(); return clone(settings);
    },
    'audio.list': async () => {
      await sleep(rnd(120, 380));
      const d = MICS.find(m => m.default);
      return clone({ mics: MICS, apps: APPS, default_mic: d ? d.label : null });
    },
  };

  const mock = {
    async call(method, params) {
      await sleep(rnd(15, 60));
      if (failures.has(method)) {
        const code = failures.get(method); failures.delete(method);
        throw err(code, code === 'permission_pending' ? 'Zaakceptuj połączenie na telewizorze.' : `Symulowany błąd (${code}).`);
      }
      const fn = M[method];
      if (!fn) throw err('unknown_method', `Nieznana metoda: ${method}`);
      return fn(params);
    },
  };

  api.mock = {
    state: () => state(),
    setTvBusy(b = true) { tvBusy = !!b; },
    setCastUnverified(b = true) { castUnverified = !!b; },
    failNext(method, code = 'internal') { failures.set(method, code); },
    setDevices(list) { S.devices = clone(list || []); emit('devices', clone(S.devices)); push(); },
    dropConnection() { if (S.mirror.state === 'live') { setMirror({ state: 'reconnecting', detail: 'Utracono pakiety' }); setTimeout(() => setMirror({ state: 'live' }), 1500); } },
    certChanged() {
      const d = S.devices.find(x => x.id === S.selected_device_id) || DEVICES[0];
      emit('error', { code: 'tv_cert_changed', message: 'Certyfikat telewizora się zmienił — możliwe podszywanie się. Potwierdź w ustawieniach, jeśli wymieniono TV.',
        device_id: d.id, device_name: d.name, retry: { method: 'devices.trustAgain', params: { id: d.id } } });
    },
    setAudioApps(list) { APPS = clone(list || []); },
    mirrorError(detail = 'Telewizor zamknął sesję.') { stopStats(); S.mode = 'idle'; setMirror({ state: 'error', detail, stats: null }); emit('error', { code: 'mirror_failed', message: detail }); },
    toast,
  };
  console.info('[airlinx] mock backend active (no WebKit message handler). Knobs: airlinx.mock');
})();
