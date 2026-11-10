/* AirLinx UI: widok podpięty do bridge'a (ui/bridge.js).
 * Stan aplikacji przychodzi wyłącznie z Pythona (app.getState + eventy); tutaj trzymamy tylko
 * stan czysto widokowy (otwarty arkusz, zakładka, rozwinięte szczegóły, bufor wykresów).
 * Kontrakt: docs/ARCHITECTURE.md („Bridge JS ⇄ Python”, „Rozszerzenia kontraktu”). */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const A = window.airlinx;
  const app = $('.app');
  const root = document.documentElement;
  const params = new URLSearchParams(location.search);
  const DEV = params.get('dev') === '1';
  const THEME_OVERRIDE = params.get('theme');

  /* ---------------------------------------------------------------- stan */
  let S = null;                 // ostatni AppState (snake_case, z Pythona)
  let sources = [];             // sources.list
  let sourcesLoading = false;
  const ui = {
    mode: 'ekran',              // zakładka Ekran / Multimedia
    screen: null,               // jawny ekran: 'settings' | 'discovery' | 'tray' | null (wynika ze stanu)
    details: false,
    failedDismissed: false,
    sourceSel: null,            // źródło wybrane w UI (przed startem)
    pickSel: null,              // zaznaczenie w arkuszu
    pickerTab: 'monitor',
    drag: 0,
    seeking: false,
    waitSince: null,
    lastMode: 'idle',
    lastMirrorState: 'idle',
    retry: null,                // {method, params} dla tv_busy
    series: [],                 // [{t, lat, br}] z mirror.stats, ostatnie 60 s
  };

  // Balans i Kino: eksperymentalne (na części telewizorów obraz po chwili się zrywa); opóźnienia nie zmierzono,
  // a Kino ma bufor odbiornika ≥ 400 ms (core/cast/mirror.py effective_options), więc bez liczb w ms.
  const UNSTABLE = 'Wyższa rozdzielczość, na części telewizorów obraz może się zrywać.';
  const PROFILES = {
    gra: { name: 'Gra / praca', short: '1080p · 60 kl./s · około 110 ms', opts: { preset: 'game', max_height: 1080, fps: 60, bitrate: 12000000 },
      desc: '1080p · 60 kl./s · około 110 ms. Do pracy, prezentacji i spokojnych gier.', badges: ['1080p', '60 kl./s'] },
    zrownowazony: { name: 'Balans', short: '1440p · 60 kl./s, profil eksperymentalny', opts: { preset: 'game', max_height: 1440, fps: 60, bitrate: 18000000 },
      desc: '1440p · 60 kl./s. ' + UNSTABLE, badges: ['1440p', '60 kl./s'], experimental: true },
    kino: { name: 'Kino', short: '4K · 30 kl./s, profil eksperymentalny', opts: { preset: 'cinema', max_height: 2160, fps: 30, bitrate: 28000000 },
      desc: '4K · 30 kl./s. ' + UNSTABLE, badges: ['4K', '30 kl./s'], experimental: true },
  };
  const EXP_TAG = '<span class="badge exp">eksperymentalny</span>';
  const profileOf = o => !o ? 'gra' : o.preset === 'cinema' ? 'kino' : (o.max_height || 1080) > 1080 ? 'zrownowazony' : 'gra';

  /* ---------------------------------------------------------------- pomocnicze */
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const dec = (n, d = 1) => (Math.round(n * 10 ** d) / 10 ** d).toFixed(d).replace('.', ',');
  const hms = s => {
    if (s == null || !isFinite(s)) return '—';
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`;
  };
  const clock = s => [Math.floor(s / 3600), Math.floor(s % 3600 / 60), Math.floor(s % 60)].map(n => String(n).padStart(2, '0')).join(':');
  const pl = (n, one, few, many) => n === 1 ? one : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) ? few : many;
  const basename = p => decodeURIComponent(String(p || '').split(/[\\/]/).pop() || p || '');
  const call = (method, p) => A.call(method, p).catch(e => { if (e.code !== 'tv_busy' && e.code !== 'tv_cert_changed' && e.code !== 'cast_unverified') toast(e.message, 'err'); throw e; });
  const quiet = pr => pr.catch(() => {});

  const device = () => S && S.devices.find(d => d.id === S.selected_device_id);
  const tvName = () => (device() || {}).name || 'telewizor';
  const srcById = id => sources.find(s => s.id === id);
  const mirrorActive = () => S && ['connecting', 'waiting_tv', 'live', 'reconnecting'].includes(S.mirror.state);
  function currentSourceId() {
    if (S && mirrorActive() && S.mirror.source_id) return S.mirror.source_id;
    const want = ui.sourceSel || (S && S.settings && S.settings.last_source_id);
    if (want && (srcById(want) || !sources.length)) return want;
    const mon = sources.find(s => s.kind === 'monitor') || sources[0];
    return mon ? mon.id : want || null;
  }
  const sourceLabel = s => {
    if (!s) return { name: 'Wybierz źródło', desc: '' };
    const [w, h] = [s.geometry[2], s.geometry[3]];
    if (s.kind === 'monitor') {
      const name = s.name.split(' · ')[0];
      return { name: `Monitor ${name}`, desc: `${w}×${h}${/główny/.test(s.name) ? ', główny' : ''}` };
    }
    const parts = s.name.split(' — ');
    return { name: parts.length > 1 ? parts[0] : s.name, desc: parts.length > 1 ? parts.slice(1).join(' — ') : 'Okno' };
  };
  const protoName = p => p === 'cast' ? 'Google Cast' : p === 'dlna' ? 'DLNA' : '';
  const statsProto = st => /dlna/.test(st.protocol || '') ? 'DLNA · strumień MPEG-TS' : 'Google Cast · kopia ekranu';
  const codecOf = enc => /hevc|265/.test(enc || '') ? 'H.265' : /264/.test(enc || '') ? 'H.264' : (enc || '');
  const encoderLabel = enc => !enc ? '' : `${codecOf(enc)} · ${/nvenc/.test(enc) ? 'NVENC' : /vaapi/.test(enc) ? 'VA-API' : 'procesor'}`;

  /* ---------------------------------------------------------------- toast */
  function toast(message, kind) {
    if (!message) return;
    const box = $('#toasts');
    const el = document.createElement('div');
    el.className = 'toast' + (kind === 'err' ? ' err' : '');
    el.textContent = message;
    box.appendChild(el);
    while (box.children.length > 3) box.firstElementChild.remove();
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 260); }, 4500);
  }

  /* ---------------------------------------------------------------- render */
  function sessionOf() {
    switch (S.mirror.state) {
      case 'connecting': return 'connecting';
      case 'waiting_tv': return 'permission';
      case 'live': return 'live';
      case 'reconnecting': return 'lost';
      case 'error': return ui.failedDismissed ? 'idle' : 'failed';
      default: return 'idle';
    }
  }
  function screenOf() {
    if (ui.screen) {
      if (ui.screen === 'discovery' && !S.devices.length && !S.discovering) return 'notfound';
      return ui.screen;
    }
    if (device() || mirrorActive() || S.mode === 'media') return 'work';
    if (S.devices.length || S.discovering) return 'discovery';
    return 'notfound';
  }
  function mediaView() {
    if (ui.drag) return 'drag';
    return S.queue.length || S.playback.state !== 'idle' ? 'playing' : 'empty';
  }

  function render() {
    if (!S) return;
    // automatyczne przełączenie zakładki, gdy zmienia się tryb pracy aplikacji
    if (S.mode !== ui.lastMode) {
      if (S.mode === 'mirror') ui.mode = 'ekran';
      if (S.mode === 'media') ui.mode = 'media';
      ui.lastMode = S.mode;
    }
    if (S.mirror.state !== ui.lastMirrorState) {
      if (S.mirror.state === 'waiting_tv') ui.waitSince = Date.now();
      if (S.mirror.state === 'error') ui.failedDismissed = false;
      if (S.mirror.state === 'live' && ui.lastMirrorState !== 'live' && ui.lastMirrorState !== 'reconnecting') { ui.series = []; powerOn(); }
      ui.lastMirrorState = S.mirror.state;
    }
    const session = sessionOf();
    Object.assign(app.dataset, {
      screen: screenOf(), disc: S.devices.length ? 'found' : 'scanning', mode: ui.mode, session,
      media: mediaView(), paused: String(S.playback.state !== 'playing'), details: ui.details ? 'open' : 'closed',
      wayland: String(!!S.wayland),
    });
    applyTheme();
    renderHeader(session);
    renderDiscovery();
    renderWork(session);
    renderMedia();
    renderSettings();
    renderAudio();
    renderMenus();
  }

  function applyTheme() {
    const pref = THEME_OVERRIDE || ((S.settings.ui || {}).theme) || 'dark';
    root.dataset.theme = pref === 'auto' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : pref;
  }

  function renderHeader(session) {
    $$('[data-mode-btn]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.modeBtn === ui.mode)));
    placeModeIndicator();
    const d = device();
    const pb = S.playback.state;
    let st = ['ok', d ? (d.online === false ? 'Niedostępny' : 'Gotowy') : ''];
    if (!d) st = ['busy', S.discovering ? 'Szukam…' : ''];
    else if (d.online === false) st = ['err', 'Niedostępny'];
    if (session === 'connecting') st = ['busy', 'Łączenie…'];
    else if (session === 'permission') st = ['wait', 'Czeka na TV'];
    else if (session === 'live') st = ['live', 'Na żywo'];
    else if (session === 'lost') st = ['wait', 'Ponawiam…'];
    else if (session === 'failed') st = ['err', 'Błąd'];
    else if (S.mode === 'media') {
      if (pb === 'playing') st = ['live', 'Odtwarza'];
      else if (pb === 'paused') st = ['wait', 'Pauza'];
      else if (pb === 'loading' || pb === 'buffering') st = ['busy', 'Wczytuje…'];
    }
    $('#tv-dot').dataset.s = st[0];
    $('#tv-name').textContent = d ? d.name : 'Wybierz telewizor';
    $('#tv-status').textContent = st[1];
    $('.tv-pill').setAttribute('aria-label', `Telewizor: ${d ? d.name : 'nie wybrano'}${st[1] ? ', ' + st[1] : ''}`);
    $$('.tv-n').forEach(e => e.textContent = d ? d.name : 'telewizora');
  }

  function placeModeIndicator() {
    const b = $(`[data-mode-btn="${ui.mode}"]`), ind = $('.modes-ind');
    if (!b || !b.offsetWidth) return;
    ind.style.width = b.offsetWidth + 'px';
    ind.style.transform = `translateX(${b.offsetLeft - 3}px)`;
  }

  const CAP_ICON = (on, icon, title) => `<span class="cap${on ? '' : ' off'}" title="${esc(title)}"><svg class="i i-s"><use href="#${icon}"/></svg></span>`;
  let tvListKey = '';
  function renderDiscovery() {
    const devs = S.devices;
    $('#found-count').textContent = devs.length ? `Znalezione: ${devs.length}` : '';
    $('#scan-note').hidden = !S.discovering;
    $('.back-to-work').hidden = !(ui.screen === 'discovery' && device());
    const key = JSON.stringify(devs.map(d => [d.id, d.name, d.model, d.ip, d.caps, d.online])) + S.selected_device_id;
    if (key === tvListKey) return;
    tvListKey = key;
    $('#tv-list').innerHTML = devs.map((d, i) => {
      const caps = new Set(d.caps || []);
      const cast = caps.has('cast_mirror') || caps.has('cast_media');
      const capsLabel = ['Google Cast', 'DLNA'].filter((_, k) => [cast, caps.has('dlna')][k]).join(', ') || 'brak';
      const current = d.id === S.selected_device_id;
      return `<button class="tv-row${i && !current ? ' second' : ''}" role="listitem" data-act="pick-tv" data-id="${esc(d.id)}" style="animation-delay:${i * 90}ms">
        <span class="tv-ico"><svg class="i i-l"><use href="#i-tv"/></svg></span>
        <span style="min-width:0"><span class="t-name">${esc(d.name)}</span>
          <span class="t-meta">${esc(d.model || 'Telewizor')} <span class="mono faint">${esc(d.ip)}</span>${d.online === false ? ' <span class="badge">offline</span>' : ''}</span></span>
        <span class="caps" aria-label="Obsługuje: ${esc(capsLabel)}">
          ${CAP_ICON(cast, 'i-cast', cast ? 'Google Cast' : 'Bez Google Cast')}${CAP_ICON(caps.has('dlna'), 'i-dlna', caps.has('dlna') ? 'DLNA' : 'Bez DLNA')}
          ${caps.has('cast_mirror') ? '' : '<span class="badge" title="Kopia ekranu przez DLNA (większe opóźnienie)">bez Cast</span>'}
        </span>
        <span class="go">${current ? 'Wybrany' : 'Połącz'}</span>
      </button>`;
    }).join('');
  }

  function renderWork(session) {
    const src = srcById(currentSourceId());
    const lab = sourceLabel(src || (currentSourceId() ? { kind: 'monitor', name: currentSourceId().replace(/^\w+:/, ''), geometry: [0, 0, 0, 0] } : null));
    $$('.src-name').forEach(e => e.textContent = lab.name);
    $$('.src-desc').forEach(e => e.textContent = lab.desc);
    $('#src-title').textContent = lab.name;
    const thumb = src && src.thumbnail_png;
    $$('.src-thumb').forEach(img => { if (thumb) img.src = thumb; else img.removeAttribute('src'); img.hidden = !thumb; });
    setImg($('#preview-img'), thumb); setImg($('#glow-src'), thumb);
    $('#preview-empty').hidden = !!thumb;
    $('#preview-empty-name').textContent = sourcesLoading ? 'Zbieram podgląd…' : lab.name;

    const opts = S.mirror.options || {};
    const prof = profileOf(opts);
    $$('[data-group="profile"], [data-group="profile-card"]').forEach(g => setRadio(g, prof));
    $$('.q-val').forEach(e => e.textContent = PROFILES[prof].name);
    $$('.profile-desc').forEach(e => { e.innerHTML = (PROFILES[prof].experimental ? EXP_TAG + ' ' : '') + esc(PROFILES[prof].desc); });
    $$('[data-opt="audio"]').forEach(b => b.setAttribute('aria-checked', String(opts.audio !== false)));
    const d = device();
    $('#idle-desc').textContent = d
      ? `${PROFILES[prof].short}. Obraz trafi na ${d.name}${d.caps && d.caps.includes('cast_mirror') ? ' przez Google Cast' : ' przez DLNA'}.`
      : 'Najpierw wybierz telewizor.';
    $('#idle-badges').innerHTML = PROFILES[prof].badges.map(b => `<span class="badge">${b}</span>`).join('')
      + (PROFILES[prof].experimental ? EXP_TAG : '');
    $('#share-under').textContent = d ? `na ${d.name}${d.model ? ', ' + d.model : ''}` : 'Nie wybrano telewizora';
    $('#share-btn').disabled = !d;
    $('#connecting-desc').textContent = `Uruchamiam odbiornik na ${tvName()} i uzgadniam format obrazu.`;
    $('#lost-desc').textContent = `${S.mirror.detail ? S.mirror.detail + '. ' : ''}Łączę ponownie, obraz wróci sam.`;
    $('#failed-desc').textContent = S.mirror.detail || 'Telewizor zamknął sesję.';
    tickClock();
    renderStats();
  }

  function setImg(img, src) {
    if (!img) return;
    if (src) { if (img.getAttribute('src') !== src) img.src = src; img.hidden = false; }
    else { img.removeAttribute('src'); img.hidden = true; }
  }

  function tickClock() {
    const m = S && S.mirror;
    if (!m) return;
    const t = m.started_at ? Date.now() / 1000 - m.started_at : 0;
    $('#live-time').textContent = clock(Math.max(0, t));
    if (ui.waitSince && m.state === 'waiting_tv') $('#wait-count').textContent = `Czekam ${Math.round((Date.now() - ui.waitSince) / 1000)} s`;
    else $('#wait-count').textContent = '';
  }

  /* ---------------------------------------------------------------- statystyki i telemetria */
  function onStats(st) {
    if (!st) return;
    const now = Date.now();
    ui.series.push({ t: now, lat: st.latency_ms, br: (st.bitrate || 0) / 1e6 });
    while (ui.series.length && ui.series[0].t < now - 60000) ui.series.shift();
    if (S) { S.mirror.stats = st; renderStats(); }
  }

  function renderStats() {
    const st = S.mirror.stats;
    const has = !!st;
    $('#s-lat').textContent = has && st.latency_ms != null ? Math.round(st.latency_ms) : '—';
    $('#s-fps').textContent = has ? Math.round(st.fps) : '—';
    $('#s-br').textContent = has ? dec((st.bitrate || 0) / 1e6) : '—';
    $('#s-res').textContent = has ? (st.height >= 2100 ? '4K' : `${st.height}p`) : '—';
    $('#s-res-k').textContent = has ? `${st.width}×${st.height}${st.encoder ? ' · ' + codecOf(st.encoder) : ''}` : '';
    $('#s-proto').textContent = has ? statsProto(st) + (st.rtt_ms != null ? ` · RTT ${dec(st.rtt_ms)} ms` : '') : protoName(S.mirror.protocol);
    $('[data-act="details"]').setAttribute('aria-expanded', String(ui.details));
    if (!ui.details) return;
    const ser = ui.series;
    const lats = ser.map(x => x.lat).filter(v => v != null);
    $('#d-lat').textContent = lats.length ? Math.round(lats[lats.length - 1]) : '—';
    $('#lat-range').textContent = lats.length ? `min ${Math.round(Math.min(...lats))} · maks ${Math.round(Math.max(...lats))}` : 'zbieram dane…';
    const brs = ser.map(x => x.br);
    $('#d-br').textContent = brs.length ? dec(brs[brs.length - 1]) : '—';
    const target = (S.mirror.options.bitrate || 0) / 1e6;
    $('#br-target').textContent = target ? `cel ${dec(target, 0)} Mb/s` : '';
    drawSpark($('#spark-lat'), ser.map(x => x.lat), lats.length ? Math.min(...lats) - 8 : 0, lats.length ? Math.max(...lats) + 8 : 1, null);
    drawSpark($('#spark-br'), brs, target ? target * .4 : 0, Math.max(target * 1.25, ...brs, 1), target || null);
    const stages = (st && st.stages) || {};
    const keys = ['capture', 'encode', 'network', 'tv'];
    keys.forEach(k => { const el = $(`[data-hop="${k}"]`); el.textContent = stages[k] != null ? `${Math.round(stages[k])} ms` : '—'; });
    const total = keys.reduce((a, k) => a + (stages[k] || 0), 0);
    $('#budget-total').textContent = total ? `${Math.round(total)} ms` : '—';
    $('#budget-bar').innerHTML = total ? keys.map(k => `<i style="flex:${Math.max(stages[k] || 0, .5)}" class="${k === 'tv' ? 'major' : ''}"></i>`).join('') : '';
    const src = srcById(S.mirror.source_id);
    $('#hop-cap').textContent = src ? `Przechwytywanie ${sourceLabel(src).name.replace(/^Monitor /, '')}` : 'Przechwytywanie';
    $('#hop-enc').textContent = st ? encoderLabel(st.encoder) : 'NVENC';
    $('#hop-net').textContent = st && st.loss ? `Utracone pakiety ${dec(st.loss * 100)}%` : (st && st.rtt_ms != null ? `RTT ${dec(st.rtt_ms)} ms` : 'Wi-Fi / LAN');
    $('#details-note').textContent = st && /dlna/.test(st.protocol || '')
      ? 'Najwięcej czasu zajmuje bufor odtwarzacza DLNA w telewizorze. To stała cecha protokołu, a nie problem z siecią.'
      : 'Najwięcej czasu zajmuje bufor odtwarzania odbiornika Google Cast w telewizorze. To stała część protokołu, a nie problem z siecią.';
  }

  function drawSpark(svg, d, lo, hi, target) {
    const W = 240, H = 56;
    if (d.length < 2) { svg.innerHTML = ''; return; }
    const span = (hi - lo) || 1;
    const x = i => (i * W / (d.length - 1)).toFixed(1);
    const y = v => (H - 4 - ((v == null ? lo : v) - lo) / span * (H - 10)).toFixed(1);
    const line = d.map((v, i) => (i ? 'L' : 'M') + x(i) + ' ' + y(v)).join('');
    svg.innerHTML = (target != null ? `<line class="target" x1="0" x2="${W}" y1="${y(target)}" y2="${y(target)}"/>` : '') +
      `<path class="area" d="${line}L${W} ${H}L0 ${H}Z"/><path class="line" d="${line}"/>`;
  }

  /* ---------------------------------------------------------------- multimedia */
  const KIND = { video: ['i-film', 'Wideo'], audio: ['i-music', 'Muzyka'], image: ['i-image', 'Zdjęcie'] };
  function currentItem() {
    return S.queue.find(x => x.id === S.playback.item_id) || S.queue[0] || null;
  }
  let queueKey = '';
  function renderMedia() {
    const pb = S.playback, item = currentItem();
    const key = JSON.stringify(S.queue.map(i => [i.id, i.title, i.duration, i.subtitle_track, !!i.thumbnail_png])) + pb.item_id + pb.state;
    if (key !== queueKey) {
      queueKey = key;
      $('#queue').innerHTML = S.queue.map(it => {
        const cur = it.id === pb.item_id && pb.state !== 'idle';
        const k = KIND[it.kind] || KIND.video;
        return `<li class="q-row"><button class="q-item" data-act="play-item" data-id="${esc(it.id)}" aria-current="${cur}">
          <span class="qt">${it.thumbnail_png ? `<img src="${esc(it.thumbnail_png)}" alt="">` : `<span class="qt-ico"><svg class="i i-s"><use href="#${k[0]}"/></svg></span>`}${cur ? '<span class="now"><svg class="i i-s"><use href="#i-eq"/></svg></span>' : ''}</span>
          <span style="min-width:0"><div class="qn">${esc(it.title)}</div><div class="qm">${k[1]}${it.subtitles && it.subtitles.length ? ' · napisy' : ''}</div></span>
          <span class="qd">${it.kind === 'image' ? '' : hms(it.duration)}</span>
        </button><button class="q-del icon-btn" data-act="remove-item" data-id="${esc(it.id)}" aria-label="Usuń z kolejki: ${esc(it.title)}" title="Usuń z kolejki"><svg class="i i-s"><use href="#i-x"/></svg></button></li>`;
      }).join('');
      const total = S.queue.reduce((a, b) => a + (b.duration || 0), 0);
      const h = Math.floor(total / 3600), m = Math.round(total % 3600 / 60);
      $('#q-sum').textContent = S.queue.length ? `${S.queue.length} ${pl(S.queue.length, 'pozycja', 'pozycje', 'pozycji')}${total ? `, ${h ? h + ' godz. ' : ''}${m} min` : ''}` : '';
    }
    $('#dz-desc').textContent = `Filmy, muzyka i zdjęcia trafią do kolejki i zagrają na ${tvName()} w pełnej jakości.`;
    if (!item) return;
    $('#np-title').textContent = item.title;
    $('#np-file').textContent = basename(item.path_or_url);
    const k = KIND[item.kind] || KIND.video;
    $('#np-badges').innerHTML = [k[1], S.media_protocol ? protoName(S.media_protocol) : ''].filter(Boolean).map((b, i) => `<span class="badge${i === 1 ? ' hot' : ''}">${esc(b)}</span>`).join('');
    setImg($('#media-img'), item.thumbnail_png); setImg($('#glow-media'), item.thumbnail_png);
    $('#media-poster').hidden = !!item.thumbnail_png;
    $('#poster-ico').setAttribute('href', '#' + k[0]);
    $('#poster-title').textContent = item.title;
    const status = { idle: pb.item_id ? '' : 'Gotowe do odtworzenia', loading: 'Wczytuję…', buffering: 'Buforuję…', ended: 'Koniec', error: pb.error || 'Nie udało się odtworzyć pliku.' }[pb.state] || '';
    const ms = $('#media-status'); ms.textContent = status; ms.hidden = !status; ms.classList.toggle('err', pb.state === 'error');
    const dur = pb.duration || item.duration || 0;
    const seek = $('#np-seek');
    seek.max = Math.max(1, Math.round(dur)); seek.disabled = !dur || pb.state === 'idle';
    if (!ui.seeking) { seek.value = Math.round(pb.position || 0); setFill(seek); $('#np-pos').textContent = hms(pb.position || 0); }
    $('#np-dur').textContent = dur ? hms(dur) : '—';
    const playing = ['playing', 'buffering', 'loading'].includes(pb.state);
    $('#play-ico').setAttribute('href', playing ? '#i-pause' : '#i-play');
    $('#play-btn').setAttribute('aria-label', playing ? 'Pauza' : 'Odtwórz');
    const vol = $('#vol');
    if (document.activeElement !== vol) { vol.value = Math.round((pb.volume != null ? pb.volume : 1) * 100); setFill(vol); }
    const subs = item.subtitles || [];
    $('#subs-btn').hidden = item.kind !== 'video';
    $('#subs-btn').disabled = !subs.length;
    $('#subs-label').textContent = !subs.length ? 'Brak napisów' : item.subtitle_track ? `Napisy: ${subLabel(item.subtitle_track)}` : 'Napisy: wyłączone';
  }
  const subLabel = p => {
    const n = basename(p);
    const m = n.match(/\.([a-z]{2,3})\.(srt|vtt|ass|ssa|sub)$/i);
    const L = { pl: 'polski', en: 'angielski', de: 'niemiecki', fr: 'francuski', es: 'hiszpański', it: 'włoski' };
    return m ? (L[m[1].toLowerCase()] || m[1]) : n;
  };

  /* ---------------------------------------------------------------- ustawienia */
  // AppState.encoder (Python: capture.probe_encoders) → [etykieta, opis]
  const ENCODERS = {
    h264_nvenc: ['NVENC, wybierany automatycznie', 'Sprzętowy koder karty graficznej odciąża procesor.'],
    libx264: ['libx264, wybierany automatycznie', 'Nie znaleziono działającego kodera NVENC, więc obraz koduje procesor. To większe obciążenie komputera.'],
    '': ['Brak kodera H.264', 'ffmpeg nie ma ani NVENC, ani libx264, więc kopia ekranu nie zadziała.'],
    probing: ['Sprawdzam…', 'Wykrywam koder wideo w ffmpeg.'],
  };
  const getPath = (o, p) => p.split('.').reduce((a, k) => (a == null ? a : a[k]), o);
  const patchFor = (p, v) => p.split('.').reduceRight((acc, k) => ({ [k]: acc }), v);
  function renderSettings() {
    const st = S.settings || {};
    $$('[data-setting]').forEach(el => {
      const v = getPath(st, el.dataset.setting);
      if (el.getAttribute('role') === 'switch') el.setAttribute('aria-checked', String(!!v));
      else if (el.tagName === 'SELECT' && document.activeElement !== el) el.value = String(v);
    });
    const br = $('[data-mirror="bitrate"]');
    if (br && document.activeElement !== br) {
      const v = String((S.mirror.options || {}).bitrate || 12000000);
      if (![...br.options].some(o => o.value === v)) br.add(new Option(`${dec(+v / 1e6, 0)} Mb/s`, v));
      br.value = v;
    }
    setRadio($('[data-group="proto"]'), st.protocol || 'auto');
    setRadio($('[data-group="theme"]'), (st.ui || {}).theme || 'dark');
    const now = mirrorActive() ? `Teraz: ${protoName(S.mirror.protocol)} · kopia ekranu`
      : S.media_protocol ? `Teraz: ${protoName(S.media_protocol)} · multimedia` : 'Teraz: nic nie jest przesyłane';
    $('#proto-now').textContent = now;
    $('#proto-dot').dataset.s = mirrorActive() || S.media_protocol ? 'live' : 'busy';
    const enc = S.mirror.stats && S.mirror.stats.encoder;
    const det = ENCODERS[S.encoder == null ? 'probing' : S.encoder] || ENCODERS.probing;
    $('#encoder-info').textContent = enc ? `${encoderLabel(enc)} (${enc})` : det[0];
    $('#encoder-desc').textContent = det[1];
    $('#app-version').textContent = `AirLinx ${S.version || ''}`;
  }

  /* ---------------------------------------------------------------- źródło dźwięku */
  const audioPick = { list: null, loading: false, at: 0 };   // audio.list (mikrofony + grające aplikacje)
  const ASRC = ['system', 'mic', 'system_mic', 'app'];
  const audioCfg = () => Object.assign({ source: 'system', mic: 'default', mic_name: null, app: null, app_name: null }, (S && S.settings.audio) || {});
  function audioLabel(a) {
    if (a.source === 'mic') return `Mikrofon: ${a.mic_name || (a.mic === 'default' ? 'domyślny' : a.mic)}`;
    if (a.source === 'system_mic') return 'System + mikrofon';
    if (a.source === 'app') return a.app ? `Tylko ${a.app_name || a.app}` : 'Wybierz aplikację';
    return 'Dźwięk systemu';
  }
  // co naprawdę słychać na TV: {text, warn, off, title}
  function audioSummary() {
    const opts = S.mirror.options || {}, cfg = audioCfg(), live = S.mirror.audio;
    if (opts.audio === false) return { text: 'Wyłączony', off: true };
    if (mirrorActive() && live) {
      if (live.pending) return { text: `${live.label} · zmiana od następnego startu`, title: `Wybrane: ${audioLabel(cfg)}` };
      if (live.note && live.source !== live.requested) {
        const why = live.requested === 'app' ? `${cfg.app_name || cfg.app || 'aplikacja'} nie gra` : 'brak mikrofonu';
        return { text: `System · ${why}`, warn: true, title: live.note };
      }
      return { text: live.label, warn: !!live.note, title: live.note || '' };
    }
    return { text: audioLabel(cfg), warn: cfg.source === 'app' && !cfg.app };
  }
  function renderAudio() {
    const opts = S.mirror.options || {}, cfg = audioCfg(), sum = audioSummary(), on = opts.audio !== false;
    $$('.a-sum').forEach(b => {
      b.querySelector('.a-sum-t').textContent = sum.text;
      b.title = sum.title || '';
      b.toggleAttribute('data-warn', !!sum.warn);
      b.toggleAttribute('data-off', !!sum.off);
      b.setAttribute('aria-label', `Źródło dźwięku: ${sum.text}${sum.title ? '. ' + sum.title : ''}. Zmień w ustawieniach`);
    });
    const box = $('#asrc');
    box.toggleAttribute('data-off', !on);
    $('#asrc-off').hidden = on;
    const g = $('[data-group="asrc"]');
    setRadio(g, ASRC.includes(cfg.source) ? cfg.source : 'system');
    $$('[role="radio"]', g).forEach(c => { if (on) c.removeAttribute('aria-disabled'); else c.setAttribute('aria-disabled', 'true'); });
    $('#asrc-mic').hidden = !(cfg.source === 'mic' || cfg.source === 'system_mic');
    $('#asrc-app').hidden = cfg.source !== 'app';
    $('#mic-sel').disabled = !on;
    renderMicSelect(cfg);
    renderAppList(cfg, on);
    // stan „teraz”
    const live = S.mirror.audio;
    let t = 'Teraz: nic nie jest przesyłane', warn = false, dot = 'busy';
    if (mirrorActive()) {
      dot = 'live';
      if (!on) t = 'Teraz: telewizor dostaje sam obraz';
      else if (live && live.pending) { t = `Teraz: ${live.label}. Nowe źródło zadziała przy następnym udostępnianiu.`; warn = true; }
      else if (live && live.note) { t = `Teraz: ${live.note}`; warn = true; }
      else if (live) t = `Teraz: ${live.label}`;
    }
    $('#asrc-now-t').textContent = t;
    $('#asrc-now').toggleAttribute('data-warn', warn);
    $('#asrc-dot').dataset.s = warn ? 'wait' : dot;
  }
  function renderMicSelect(cfg) {
    const sel = $('#mic-sel');
    if (document.activeElement === sel) return;
    const list = audioPick.list ? audioPick.list.mics : [];
    const def = audioPick.list && audioPick.list.default_mic;
    const opts = [['default', def ? `Domyślny mikrofon (${def})` : 'Domyślny mikrofon']].concat(list.map(m => [m.name, m.label]));
    if (cfg.mic !== 'default' && !list.some(m => m.name === cfg.mic)) {
      opts.push([cfg.mic, `${cfg.mic_name || cfg.mic} (${audioPick.list ? 'niedostępny' : 'sprawdzam…'})`]);
    }
    const sig = JSON.stringify(opts);
    if (sel.dataset.sig !== sig) {
      sel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
      sel.dataset.sig = sig;
    }
    sel.value = cfg.mic || 'default';
  }
  function renderAppList(cfg, on) {
    const box = $('#app-list');
    box.setAttribute('aria-busy', String(audioPick.loading));
    $('#audio-refresh').disabled = audioPick.loading || !on;
    const apps = audioPick.list ? audioPick.list.apps.slice() : [];
    if (cfg.app && !apps.some(a => a.key === cfg.app)) apps.push({ key: cfg.app, name: cfg.app_name || cfg.app, icon_url: null, streams: 0, gone: true });
    if (!apps.length) {
      box.innerHTML = `<div class="app-empty">${audioPick.list ? 'Żadna aplikacja nic teraz nie odtwarza. Włącz odtwarzanie i kliknij „Odśwież”.' : 'Sprawdzam, co gra…'}</div>`;
      return;
    }
    const focusKey = box.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    const sel = cfg.source === 'app' ? cfg.app : null;
    box.innerHTML = apps.map((a, i) => {
      const checked = a.key === sel;
      const ico = a.icon_url ? `<img src="${esc(a.icon_url)}" alt="">` : esc((a.name || '?').trim().charAt(0).toUpperCase());
      const meta = a.gone ? '<span class="a-s gone">nie gra teraz</span>'
        : `<span class="a-s">${a.streams} ${pl(a.streams, 'strumień', 'strumienie', 'strumieni')}</span>`;
      return `<button class="app-opt" role="radio" aria-checked="${checked}" tabindex="${checked || (!sel && i === 0) ? 0 : -1}" data-act="pick-app" data-key="${esc(a.key)}" data-name="${esc(a.name)}"${on ? '' : ' aria-disabled="true"'}>`
        + `<span class="app-ico" aria-hidden="true">${ico}</span><span class="a-n">${esc(a.name)}</span>${meta}<span class="radio" aria-hidden="true"></span></button>`;
    }).join('');
    if (focusKey) { const f = $$('.app-opt', box).find(b => b.dataset.key === focusKey); if (f) f.focus(); }
  }
  async function refreshAudio(force) {
    if (audioPick.loading || (!force && audioPick.list && Date.now() - audioPick.at < 1500)) return;
    audioPick.loading = true;
    if (S) renderAudio();
    try { audioPick.list = await A.call('audio.list'); audioPick.at = Date.now(); }
    catch (e) { toast(e.message, 'err'); if (!audioPick.list) audioPick.list = { mics: [], apps: [], default_mic: null }; }
    audioPick.loading = false;
    if (S) renderAudio();
  }
  function setAudio(patch) {
    if (S) { S.settings.audio = Object.assign(audioCfg(), patch); render(); }
    return quiet(call('settings.set', { patch: { audio: patch } }));
  }
  function openAudioSettings() {
    ui.screen = 'settings'; setTab('dzwiek'); render();
    setTimeout(() => { const c = $('[data-group="asrc"] [aria-checked="true"]') || $('[data-tab="dzwiek"]'); if (c) c.focus(); }, 0);
  }

  /* ---------------------------------------------------------------- menu */
  function renderMenus() {
    $('#m-devices-list').innerHTML = S.devices.length ? S.devices.map(d =>
      `<button class="m-i" role="menuitemradio" aria-checked="${d.id === S.selected_device_id}" data-act="pick-tv" data-id="${esc(d.id)}">
        <svg class="i"><use href="#i-tv"/></svg><span><div>${esc(d.name)}</div><div class="m-d">${esc(d.model || '')} · ${esc(d.ip)}</div></span>
        <svg class="i i-s check"><use href="#i-check"/></svg></button>`).join('')
      : `<div class="m-h">${S.discovering ? 'Szukam…' : 'Nie znaleziono telewizorów'}</div>`;
    const it = currentItem();
    const subs = (it && it.subtitles) || [];
    $('#m-subs-list').innerHTML = subs.map(p =>
      `<button class="m-i" role="menuitemradio" aria-checked="${it.subtitle_track === p}" data-act="pick-sub" data-track="${esc(p)}">${esc(subLabel(p))} <span class="m-d">${esc(basename(p))}</span><svg class="i i-s check"><use href="#i-check"/></svg></button>`).join('') +
      `<button class="m-i" role="menuitemradio" aria-checked="${!(it && it.subtitle_track)}" data-act="pick-sub" data-track="">Wyłączone<svg class="i i-s check"><use href="#i-check"/></svg></button>`;
  }

  let openMenu = null, menuOpener = null;
  function showMenu(id, opener) {
    closeMenu();
    const m = $(id), r = opener.getBoundingClientRect();
    m.classList.add('open');
    if (m.classList.contains('up')) { m.style.left = Math.max(12, r.left) + 'px'; m.style.top = Math.max(8, r.top - m.offsetHeight - 8) + 'px'; }
    else { m.style.left = Math.max(8, r.right - m.offsetWidth) + 'px'; m.style.top = (r.bottom + 8) + 'px'; }
    opener.setAttribute('aria-expanded', 'true');
    openMenu = m; menuOpener = opener;
    const first = m.querySelector('.m-i[aria-checked="true"]') || m.querySelector('.m-i');
    first && first.focus();
  }
  function closeMenu(restore) {
    if (!openMenu) return;
    openMenu.classList.remove('open');
    menuOpener && menuOpener.setAttribute('aria-expanded', 'false');
    if (restore && menuOpener) menuOpener.focus();
    openMenu = menuOpener = null;
  }

  /* ---------------------------------------------------------------- dialogi */
  let dialogOpener = null;
  function openDialog(el, focusSel) {
    closeMenu();
    dialogOpener = document.activeElement;
    el.classList.add('open'); el.setAttribute('aria-hidden', 'false');
    setTimeout(() => { const f = $(focusSel, el); f && f.focus(); }, 30);
  }
  function closeDialog(el) {
    if (!el.classList.contains('open')) return;
    el.classList.remove('open'); el.setAttribute('aria-hidden', 'true');
    if (dialogOpener && dialogOpener.focus) dialogOpener.focus();
  }
  const openDialogEl = () => $$('.scrim.open').pop();

  async function loadSources() {
    sourcesLoading = true; renderPicker(); if (S) renderWork(sessionOf());
    try { sources = (await A.call('sources.list', { withThumbnails: true })) || []; }
    catch (e) { toast(e.message, 'err'); }
    sourcesLoading = false;
    renderPicker(); if (S) render();
  }
  function renderPicker() {
    const loading = sourcesLoading && !sources.length;
    $('#sources-loading').hidden = !loading;
    const sel = ui.pickSel || currentSourceId();
    for (const kind of ['monitor', 'window']) {
      const list = sources.filter(s => s.kind === kind);
      $(`#count-${kind}`).textContent = list.length;
      $(`[data-ppanel="${kind}"]`).innerHTML = list.map(s => {
        const l = sourceLabel(s);
        return `<button class="tile" role="radio" aria-checked="${s.id === sel}" data-act="pick-src" data-id="${esc(s.id)}" tabindex="${s.id === sel ? 0 : -1}">
          <div class="tt">${s.thumbnail_png ? `<img src="${esc(s.thumbnail_png)}" alt="">` : `<span class="tt-empty"><svg class="i i-l"><use href="#${kind === 'monitor' ? 'i-monitor' : 'i-window'}"/></svg></span>`}<span class="tick"><svg class="i"><use href="#i-check"/></svg></span></div>
          <div class="tl"><svg class="i i-s"><use href="#${kind === 'monitor' ? 'i-monitor' : 'i-window'}"/></svg><b>${esc(l.name)}</b><span>${esc(l.desc)}</span></div>
        </button>`;
      }).join('') || (loading ? '' : `<p class="faint" style="grid-column:1/-1">${kind === 'monitor' ? 'Nie znaleziono monitorów.' : 'Brak okien do udostępnienia.'}</p>`);
    }
    $$('[data-ptab]').forEach(b => { const on = b.dataset.ptab === ui.pickerTab; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
    $$('[data-ppanel]').forEach(p => p.hidden = p.dataset.ppanel !== ui.pickerTab || loading);
    const live = S && mirrorActive();
    $('#apply-src').textContent = live ? 'Przełącz' : 'Udostępnij';
    $('#apply-src').disabled = !sel || (!live && !device());
    $('#picker-title').textContent = `Co pokazać na ${tvName()}?`;
    $('#picker-note').textContent = live ? 'Obraz przełączy się bez przerywania sesji.' : '';
  }
  function openPicker() {
    ui.pickSel = currentSourceId();
    const s = srcById(ui.pickSel); ui.pickerTab = s ? s.kind : 'monitor';
    renderPicker();
    openDialog($('#picker'), `[data-ptab="${ui.pickerTab}"]`);
    loadSources();
  }

  /* ---------------------------------------------------------------- akcje */
  function powerOn() {
    const scr = $('#screen');
    scr.classList.remove('powering'); void scr.offsetWidth; scr.classList.add('powering');
    setTimeout(() => scr.classList.remove('powering'), 950);
  }

  function currentOpts() { return Object.assign({}, S.mirror.options || {}); }
  async function share() {
    if (!device()) { ui.screen = 'discovery'; render(); return; }
    const sourceId = currentSourceId();
    if (!sourceId) { openPicker(); return; }
    ui.failedDismissed = false;
    try { await call('mirror.start', { sourceId, options: currentOpts() }); } catch (_) { /* toast pokazany */ }
  }
  async function playpause() {
    const pb = S.playback.state;
    if (['playing', 'buffering', 'loading'].includes(pb)) return quiet(call('media.pause'));
    if (pb === 'paused') return quiet(call('media.resume'));
    const it = currentItem();
    if (it) return quiet(call('media.play', { id: it.id }));
  }
  async function addUrl(url) {
    try {
      const items = await call('media.add', { urls: [url] });
      ui.mode = 'media';
      if (items && items[0]) await call('media.play', { id: items[0].id });
      $('#url-in').value = '';
    } catch (_) {}
  }
  function setOptions(patch) { return quiet(call('mirror.update', { options: patch })); }
  function setSetting(path, value) { return quiet(call('settings.set', { patch: patchFor(path, value) })); }

  function setRadio(group, val) {
    if (!group) return;
    $$('[role="radio"]', group).forEach(b => { const on = b.dataset.val === val; b.setAttribute('aria-checked', String(on)); b.tabIndex = on ? 0 : -1; });
  }
  function setFill(r) {
    const v = (r.value - r.min) / ((r.max - r.min) || 1) * 100;
    r.style.setProperty('--v', v + '%');
  }

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-act], [data-mode-btn], [data-win], [role="radio"], [role="switch"], [data-tab], [data-ptab]');
    if (openMenu && !e.target.closest('.menu') && !(el && el === menuOpener)) closeMenu();
    if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return;

    if (el.dataset.win) { ({ min: A.minimize, max: A.toggleMaximize, close: A.close })[el.dataset.win](); return; }
    if (el.dataset.modeBtn) { ui.mode = el.dataset.modeBtn; ui.screen = null; render(); return; }
    if (el.dataset.tab) { setTab(el.dataset.tab); return; }
    if (el.dataset.ptab) { ui.pickerTab = el.dataset.ptab; renderPicker(); return; }

    if (el.getAttribute('role') === 'switch') {
      const v = el.getAttribute('aria-checked') !== 'true';
      el.setAttribute('aria-checked', String(v));
      if (el.dataset.opt) setOptions({ [el.dataset.opt]: v });
      else if (el.dataset.setting) setSetting(el.dataset.setting, v);
      return;
    }
    if (el.getAttribute('role') === 'radio' && !el.dataset.act) {
      const g = el.closest('[role="radiogroup"]');
      const v = el.dataset.val;
      setRadio(g, v);
      if (g.dataset.group === 'profile' || g.dataset.group === 'profile-card') setOptions(PROFILES[v].opts);
      else if (g.dataset.group === 'proto') setSetting('protocol', v);
      else if (g.dataset.group === 'asrc') {
        const patch = { source: v };
        if (v === 'app' && !audioCfg().app && audioPick.list && audioPick.list.apps.length) {
          const a0 = audioPick.list.apps[0]; patch.app = a0.key; patch.app_name = a0.name;
        }
        setAudio(patch);
        if (v !== 'system') refreshAudio();
      }
      else if (g.dataset.group === 'theme') { if (S) { S.settings.ui = Object.assign({}, S.settings.ui, { theme: v }); applyTheme(); } setSetting('ui.theme', v); }
      return;
    }

    const id = el.dataset.id;
    switch (el.dataset.act) {
      case 'pick-tv':
        closeMenu();
        quiet(call('devices.select', { id }).then(() => { ui.screen = null; render(); }));
        break;
      case 'rescan': closeMenu(); ui.screen = 'discovery'; quiet(call('devices.rescan')); render(); break;
      case 'open-manual': closeMenu(); ui.screen = 'discovery'; render(); toggleManual(true); break;
      case 'manual': toggleManual(); break;
      case 'close-disc': ui.screen = null; render(); break;
      case 'devices': openMenu === $('#m-devices') ? closeMenu(true) : showMenu('#m-devices', el); break;
      case 'settings': ui.screen = 'settings'; render(); setTimeout(() => { const t = $('.s-nav [aria-selected="true"]'); t && t.focus(); }, 0); break;
      case 'back': ui.screen = null; render(); break;
      case 'picker': openPicker(); break;
      case 'close-picker': closeDialog($('#picker')); break;
      case 'pick-src':
        ui.pickSel = id;
        $$('.tile').forEach(t => { const on = t.dataset.id === id; t.setAttribute('aria-checked', String(on)); t.tabIndex = on ? 0 : -1; });
        $('#apply-src').disabled = false;
        break;
      case 'apply-src': {
        const sid = ui.pickSel; closeDialog($('#picker'));
        if (!sid) break;
        ui.sourceSel = sid;
        if (mirrorActive()) quiet(call('mirror.switchSource', { sourceId: sid }));
        else share();
        render();
        break;
      }
      case 'audio-settings': openAudioSettings(); break;
      case 'audio-refresh': refreshAudio(true); break;
      case 'pick-app':
        setAudio({ source: 'app', app: el.dataset.key, app_name: el.dataset.name });
        break;
      case 'share': share(); break;
      case 'stop': quiet(call('mirror.stop')); break;
      case 'dismiss-failed': ui.failedDismissed = true; render(); break;
      case 'details': ui.details = !ui.details; render(); break;
      case 'confirm-yes': {
        const r = ui.retry; ui.retry = null; closeDialog($('#confirm'));
        if (r && r.method) quiet(call(r.method, r.params || {}));
        break;
      }
      case 'confirm-no': ui.retry = null; closeDialog($('#confirm')); break;
      case 'pick-files': ui.mode = 'media'; quiet(call('media.pickFiles')); break;
      case 'play-item': quiet(call('media.play', { id })); break;
      case 'remove-item': quiet(call('media.remove', { id })); break;
      case 'playpause': playpause(); break;
      case 'next': quiet(call('media.next')); break;
      case 'prev': quiet(call('media.prev')); break;
      case 'media-stop': quiet(call('media.stop')); break;
      case 'subs': openMenu === $('#m-subs') ? closeMenu(true) : showMenu('#m-subs', el); break;
      case 'pick-sub': {
        const it = currentItem(); closeMenu(true);
        if (it) quiet(call('media.subtitles', { id: it.id, track: el.dataset.track || null }));
        break;
      }
      case 'dev': { const p = $('#dev-panel'); p.hidden = !p.hidden; el.setAttribute('aria-expanded', String(!p.hidden)); break; }
      case 'dev-act': devAction(el.dataset.what); break;
    }
  });

  function toggleManual(force) {
    const f = $('#manual-form'), open = force != null ? force : f.hidden;
    f.hidden = !open; $('#manual-hint').hidden = !open;
    $('[data-act="manual"]').setAttribute('aria-expanded', String(open));
    if (open) setTimeout(() => $('#manual-ip').focus(), 20);
  }
  $('#manual-form').addEventListener('submit', async e => {
    e.preventDefault();
    const ip = $('#manual-ip').value.trim();
    const hint = $('#manual-hint');
    if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) { hint.textContent = 'Wpisz adres IP w postaci 192.168.0.20.'; hint.classList.add('bad'); return; }
    const btn = $('#manual-go'); btn.disabled = true; hint.classList.remove('bad'); hint.textContent = `Sprawdzam ${ip}…`;
    try {
      const d = await A.call('devices.addManual', { ip });
      if (d && d.id) await A.call('devices.select', { id: d.id });
      ui.screen = null; toggleManual(false); render();
    } catch (err) { hint.textContent = err.message; hint.classList.add('bad'); }
    btn.disabled = false;
  });
  $('#url-form').addEventListener('submit', e => {
    e.preventDefault();
    const v = $('#url-in').value.trim();
    if (v) addUrl(v); else $('#url-in').focus();
  });

  function setTab(t) {
    $$('.s-nav [role="tab"]').forEach(b => { const on = b.dataset.tab === t; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
    $$('.s-panel').forEach(p => p.classList.toggle('on', p.dataset.panel === t));
    if (t === 'dzwiek') refreshAudio(true);          // lista mikrofonów i grających aplikacji przy każdym otwarciu
  }

  // listy wyboru w ustawieniach
  document.addEventListener('change', e => {
    const el = e.target;
    if (el.matches('select[data-setting]')) setSetting(el.dataset.setting, el.dataset.type === 'bool' ? el.value === 'true' : el.value);
    if (el.matches('select[data-mirror]')) setOptions({ [el.dataset.mirror]: el.dataset.type === 'int' ? parseInt(el.value, 10) : el.value });
    if (el.id === 'mic-sel') {
      const m = audioPick.list && audioPick.list.mics.find(x => x.name === el.value);
      setAudio({ mic: el.value, mic_name: el.value === 'default' ? null : (m ? m.label : audioCfg().mic_name) });
    }
  });

  // suwaki
  const seek = $('#np-seek'), vol = $('#vol');
  seek.addEventListener('input', () => { ui.seeking = true; setFill(seek); $('#np-pos').textContent = hms(+seek.value); });
  seek.addEventListener('change', () => { quiet(call('media.seek', { seconds: +seek.value })).finally(() => { ui.seeking = false; }); });
  vol.addEventListener('input', () => setFill(vol));
  vol.addEventListener('change', () => quiet(call('media.volume', { value: +vol.value / 100 })));

  // pasek tytułu: przeciąganie i maksymalizacja
  const bar = $('#titlebar');
  bar.addEventListener('mousedown', e => { if (e.button === 0 && !e.target.closest('button, a, input, select')) A.startDrag(e); });
  bar.addEventListener('dblclick', e => { if (!e.target.closest('button, a, input, select')) A.toggleMaximize(); });

  /* ---------------------------------------------------------------- przeciąganie plików */
  const hasFiles = e => e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files');
  window.addEventListener('dragenter', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    ui.drag++;
    if (ui.drag === 1 && S && screenOf() === 'work') {
      ui.mode = 'media';
      const n = e.dataTransfer.items ? e.dataTransfer.items.length : 0;
      $('#dz-drag-count').textContent = n ? `${n} ${pl(n, 'plik', 'pliki', 'plików')}` : '';
      render();
    }
  });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('dragleave', e => { if (!hasFiles(e)) return; if (--ui.drag <= 0) { ui.drag = 0; render(); } });
  window.addEventListener('drop', e => {
    e.preventDefault();
    const files = e.dataTransfer ? [...e.dataTransfer.files] : [];
    ui.drag = 0; ui.mode = 'media'; render();
    if (A.native) return;                         // w oknie GTK pliki dodaje Python (media.add → media.queue)
    if (files.length) quiet(call('media.add', { paths: files.map(f => f.path || f.name) }));
  });

  /* ---------------------------------------------------------------- klawiatura */
  document.addEventListener('keydown', e => {
    const dlg = openDialogEl();
    if (e.key === 'Escape') {
      if (openMenu) { closeMenu(true); return; }
      if (dlg) { if (dlg.id === 'confirm') ui.retry = null; closeDialog(dlg); return; }
      if (ui.screen === 'settings' || (ui.screen === 'discovery' && device())) { ui.screen = null; render(); return; }
    }
    if (e.key === 'Tab' && dlg) {
      const f = $$('button, input, select, [tabindex="0"]', dlg).filter(x => x.offsetParent && !x.disabled && x.tabIndex >= 0);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    if (openMenu && ['ArrowDown', 'ArrowUp'].includes(e.key)) {
      e.preventDefault();
      const items = $$('.m-i', openMenu), i = items.indexOf(document.activeElement);
      items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
      return;
    }
    const arrows = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'];
    const fwd = e.key === 'ArrowRight' || e.key === 'ArrowDown';
    const group = e.target.closest && e.target.closest('[role="radiogroup"], [role="tablist"]');
    if (group && arrows.includes(e.key)) {
      const role = group.getAttribute('role') === 'tablist' ? 'tab' : 'radio';
      const items = $$(`[role="${role}"]`, group).filter(x => !x.disabled);
      const i = items.indexOf(e.target);
      if (i < 0) return;
      e.preventDefault();
      const n = items[(i + (fwd ? 1 : items.length - 1)) % items.length];
      n.focus(); n.click();
      return;
    }
    if (e.key === ' ' && ui.mode === 'media' && screenOf() === 'work' && !dlg && S && S.queue.length && !e.target.closest('button, input, select, textarea')) {
      e.preventDefault(); playpause();
    }
  });

  window.addEventListener('resize', placeModeIndicator);

  /* ---------------------------------------------------------------- bridge: eventy */
  A.on('state', st => { S = st; render(); });
  A.on('devices', devs => { if (S) { S.devices = devs; render(); } });
  A.on('mirror.state', m => { if (S) { S.mirror = m; render(); } });
  A.on('mirror.stats', onStats);
  A.on('media.queue', q => { if (S) { S.queue = q; render(); } });
  A.on('media.playback', pb => { if (S) { S.playback = pb; render(); } });
  A.on('toast', p => toast(p && p.message));
  A.on('error', p => {
    if (!p) return;
    if (p.code === 'tv_cert_changed') {             // TOFU: certyfikat TV inny niż zapamiętany
      ui.retry = p.retry || null;
      $('#confirm-title').textContent = 'Telewizor ma nowy certyfikat';
      $('#confirm-msg').textContent = (p.message || 'Certyfikat telewizora się zmienił.') + ' Zaufaj ponownie tylko, jeśli to na pewno Twój telewizor (np. po resecie lub wymianie).';
      $('#confirm-yes').textContent = 'Zaufaj ponownie temu telewizorowi';
      openDialog($('#confirm'), '[data-act="confirm-no"]');
      return;
    }
    if (p.code === 'cast_unverified') {             // TV z przypiętą tożsamością Cast nie odpowiada przez Cast
      ui.retry = p.retry || null;
      $('#confirm-title').textContent = 'Wysłać bez weryfikacji?';
      $('#confirm-msg').textContent = (p.message || 'Nie udało się potwierdzić tożsamości telewizora przez Google Cast.') +
        ' Wybierz to tylko, jeśli to na pewno Twój telewizor.';
      $('#confirm-yes').textContent = 'Wyślij przez DLNA';
      openDialog($('#confirm'), '[data-act="confirm-no"]');   // bezpieczna odpowiedź domyślnie
      return;
    }
    if (p.code === 'tv_busy') {
      ui.retry = p.retry || null;
      $('#confirm-title').textContent = 'Przerwać to, co leci na TV?';
      $('#confirm-msg').textContent = p.message || 'Na telewizorze działa inna aplikacja.';
      $('#confirm-yes').textContent = p.retry && p.retry.method === 'media.play' ? 'Przerwij i odtwórz' : 'Przerwij i pokaż';
      openDialog($('#confirm'), '#confirm-yes');
      return;
    }
    if (p.code === 'mirror_failed') return;          // pokazujemy w stopce sceny (f-failed)
    toast(p.message, 'err');
  });

  setInterval(() => { if (S && (S.mirror.state === 'live' || S.mirror.state === 'waiting_tv')) tickClock(); }, 1000);

  /* ---------------------------------------------------------------- narzędzia deweloperskie (?dev=1) */
  function devAction(what) {
    const M = A.mock;
    switch (what) {
      case 'theme': { const t = root.dataset.theme === 'dark' ? 'light' : 'dark'; root.dataset.theme = t; break; }
      case 'tray': ui.screen = ui.screen === 'tray' ? null : 'tray'; render(); break;
      case 'busy': M && M.setTvBusy(true); toast('Następny start trafi na zajęty telewizor.'); break;
      case 'drop': M && M.dropConnection(); break;
      case 'fail': M && M.mirrorError(); break;
      case 'nodev': M && M.setDevices([]); ui.screen = null; break;
      case 'toast': toast('Przykładowy komunikat.'); break;
    }
  }
  if (DEV) {
    $('#dev').hidden = false;
    const acts = [['theme', 'Przełącz motyw'], ['tray', 'Podgląd zasobnika'], ['toast', 'Komunikat']];
    if (!A.native) acts.push(['busy', 'Mock: TV zajęty'], ['drop', 'Mock: zerwij połączenie'], ['fail', 'Mock: błąd sesji'], ['nodev', 'Mock: brak telewizorów']);
    $('#dev-panel').innerHTML = '<div class="dh">Narzędzia</div>' + acts.map(([k, l]) => `<button class="st" data-act="dev-act" data-what="${k}">${l}</button>`).join('');
  }

  /* ---------------------------------------------------------------- start */
  if (THEME_OVERRIDE) root.dataset.theme = THEME_OVERRIDE;
  A.call('app.getState').then(st => { if (!S) { S = st; ui.lastMode = st.mode; ui.lastMirrorState = st.mirror.state; } render(); })
    .catch(e => toast(e.message, 'err'));
  loadSources();
  document.fonts && document.fonts.ready.then(placeModeIndicator);
})();
