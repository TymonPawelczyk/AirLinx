/* AirLinx — wariant A, logika klikalnej makiety */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const app = $('.app');
  const root = document.documentElement;

  const parts = location.hash.replace('#', '').split('/').filter(Boolean);
  const SHOT = parts.includes('shot');
  if (SHOT) document.body.classList.add('shot');

  const S = {
    screen: 'discovery', disc: 'found', mode: 'ekran', session: 'idle', media: 'empty',
    paused: false, permitted: false, details: false, elapsed: 767, q: 12, smode: 'gra', tv: 'Salon',
    playing: 0, pos: 4328, lostIn: 4, permLeft: 24,
  };

  const QUEUE_DEMO = [
    { t: 'Zorza', f: 'zorza-2026-2160p.mkv', sec: 10143, b: ['4K HDR10', '5.1'], thumb: 'scene-movie', vb: '0 0 1600 900' },
    { t: 'Wydmy', f: 'wydmy-2026-2160p-hdr.mkv', sec: 9982, b: ['4K HDR10', 'Atmos'], thumb: 'q-dune', vb: '0 0 64 36' },
    { t: 'Wakacje w Chorwacji', f: 'Wakacje_Chorwacja_2025.mp4', sec: 761, b: ['1080p'], thumb: 'q-sea', vb: '0 0 64 36' },
    { t: 'Tatry, wrzesień', f: '24 zdjęcia, HEIC', sec: 0, label: '24 zdj.', b: ['Pokaz slajdów'], thumb: 'q-tatry', vb: '0 0 64 36' },
    { t: 'Koncert w ogrodzie', f: 'koncert-w-ogrodzie.flac', sec: 381, b: ['FLAC 24 bit'], thumb: 'q-music', vb: '0 0 64 36' },
  ];
  let queue = QUEUE_DEMO.map(x => ({ ...x }));

  /* ---------- formatowanie ---------- */
  const hms = s => {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`;
  };
  const clock = s => [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map(n => String(n).padStart(2, '0')).join(':');
  const pl = (n, one, few, many) => n === 1 ? one : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) ? few : many;
  const dec = n => n.toFixed(1).replace('.', ',');
  const esc = v => String(v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  /* ---------- profil jakości (natywny mirroring Google Cast) ----------
     Opóźnienie = przechwytywanie + NVENC + Wi-Fi + bufor odbiornika Cast i dekodowanie w TV.
     Bufor odbiornika dominuje (ok. 80–120 ms) i nie zależy od AirLinx. */
  function profile(q) {
    if (q < 34) return { key: 'gra', name: 'Gra / praca', res: '1080p', k: '1920×1080 · H.264', enc: 'H.264 · RTX 3070', fps: 60,
      stages: [6, 5, 9, 88 + Math.round(q * .2)], br: 10.6 + q * .04, target: 12, desc: '1080p · 60 kl./s · około 110 ms' };
    if (q < 67) return { key: 'zrownowazony', name: 'Zrównoważony', res: '1440p', k: '2560×1440 · H.264', enc: 'H.264 · RTX 3070', fps: 60,
      stages: [6, 7, 11, 98 + Math.round((q - 34) * .2)], br: 16.5 + (q - 34) * .1, target: 18, desc: '1440p · 60 kl./s · około 125 ms' };
    return { key: 'kino', name: 'Kino', res: '4K', k: '3840×2160 · H.265', enc: 'H.265 · RTX 3070', fps: 30,
      stages: [7, 9, 14, 112 + Math.round((q - 67) * .25)], br: 25.5 + (q - 67) * .12, target: 28, desc: '4K · 30 kl./s · około 150 ms' };
  }
  const latOf = p => p.stages.reduce((a, b) => a + b, 0);

  /* ---------- telemetria: 60 próbek (1/s), deterministyczny szum dla powtarzalnych zrzutów ---------- */
  function rng(seed) { let s = seed; return () => ((s = (s * 9301 + 49297) % 233280) / 233280); }
  const T = { lat: [], br: [], key: null };
  function seedSeries(p) {
    const r = rng(7), base = latOf(p);
    T.lat = Array.from({ length: 60 }, (_, i) => base + (r() - .5) * 10 + (i === 38 ? 21 : i === 39 ? 9 : 0));
    T.br = Array.from({ length: 60 }, (_, i) => p.br + (r() - .5) * p.br * .14 - (i === 38 ? p.br * .3 : 0));
    T.key = p.key;
  }
  function drawSpark(svg, d, lo, hi, target) {
    const W = 240, H = 56, x = i => (i * W / (d.length - 1)).toFixed(1), y = v => (H - 4 - (v - lo) / (hi - lo) * (H - 10)).toFixed(1);
    const line = d.map((v, i) => (i ? 'L' : 'M') + x(i) + ' ' + y(v)).join('');
    svg.innerHTML = (target != null ? `<line class="target" x1="0" x2="${W}" y1="${y(target)}" y2="${y(target)}"/>` : '') +
      `<path class="area" d="${line}L${W} ${H}L0 ${H}Z"/><path class="line" d="${line}"/>`;
  }
  function renderDetails() {
    const p = profile(S.q);
    if (T.key !== p.key) seedSeries(p);
    const lat = T.lat, br = T.br;
    $('#d-lat').textContent = Math.round(lat[59]);
    $('#d-br').textContent = dec(br[59]);
    $('#lat-range').textContent = `min ${Math.round(Math.min(...lat))} · maks ${Math.round(Math.max(...lat))}`;
    $('#br-target').textContent = `cel ${p.target} Mb/s`;
    drawSpark($('#spark-lat'), lat, Math.min(...lat) - 8, Math.max(...lat) + 8, null);
    drawSpark($("#spark-br"), br, p.target * .45, p.target * 1.25, p.target);
    $('#hop-enc').textContent = p.enc;
    $$('[data-hop]').forEach((el, i) => el.textContent = p.stages[i] + ' ms');
    $('#budget-bar').innerHTML = p.stages.map((s, i) => `<i style="flex:${s}" class="${i === 3 ? 'major' : ''}"></i>`).join('');
    $('#budget-total').textContent = `${latOf(p)} ms`;
    $('[data-act="details"]').setAttribute('aria-expanded', String(S.details));
    app.dataset.details = S.details ? 'open' : 'closed';
  }

  /* ---------- render ---------- */
  function render() {
    Object.assign(app.dataset, { screen: S.screen, disc: S.disc, mode: S.mode, session: S.session, media: S.media, paused: String(S.paused) });

    // przełącznik trybu
    $$('[data-mode-btn]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.modeBtn === S.mode)));
    placeModeIndicator();

    // status telewizora w pasku
    const st = statusOf();
    $('#tv-dot').dataset.s = st[0];
    $('#tv-status').textContent = st[1];
    $('.tv-pill .name').textContent = S.tv;
    $('.tv-pill').setAttribute('aria-label', `Telewizor: ${S.tv}, ${st[1]}`);

    // ekran: opis przed udostępnieniem
    const p = profile(S.q);
    const idleP = $('.f-idle p'), idleB = $('.f-idle .badges');
    if (S.smode === 'kino') {
      idleP.textContent = 'Wyślę obraz 4K w 30 kl./s przez Google Cast. Najwyższa ostrość, opóźnienie około 150 ms.';
      idleB.innerHTML = '<span class="badge">4K</span><span class="badge">30 kl./s</span>';
    } else {
      idleP.textContent = 'Wyślę obraz 1080p w 60 kl./s przez Google Cast. Opóźnienie około 110 ms.';
      idleB.innerHTML = '<span class="badge">1080p</span><span class="badge">60 kl./s</span>';
    }
    $$('.smode-desc').forEach(d => d.textContent = S.smode === 'kino'
      ? 'Najwyższa ostrość, 4K w 30 kl./s. Opóźnienie około 150 ms, do filmów i zdjęć.'
      : 'Najniższe opóźnienie, około 110 ms. Do pracy, prezentacji i spokojnych gier.');
    $('.r-idle .under').textContent = `na ${S.tv}, ${S.tv === 'Salon' ? 'Samsung S85F' : 'LG OLED55C3'}`;

    // suwak jakości
    const qr = $('#q-range');
    qr.value = S.q; setFill(qr);
    $('#q-val').textContent = p.name;
    $('#q-desc').textContent = p.desc;
    $('#s-res').textContent = p.res;
    $('#s-res-k').textContent = p.k;
    $('#s-proto').textContent = 'Google Cast · kopia ekranu · Wi-Fi 5 GHz';
    tickStats(true);
    renderDetails();

    $('#live-time').textContent = clock(S.elapsed);
    $('#perm-count').textContent = `Czekam jeszcze ${S.permLeft} s`;
    $('#lost-try').textContent = `Próba 2/5 · następna za ${S.lostIn} s.`;

    renderQueue();
    renderPlayer();
  }

  function statusOf() {
    switch (S.session) {
      case 'connecting': return ['busy', 'Łączenie…'];
      case 'permission': return ['wait', 'Czeka na zgodę'];
      case 'live': return ['live', 'Na żywo'];
      case 'lost': return ['err', 'Ponawiam…'];
    }
    if (S.mode === 'media' && S.media === 'playing') return S.paused ? ['wait', 'Pauza'] : ['live', 'Odtwarza'];
    return ['ok', 'Gotowy'];
  }

  function placeModeIndicator() {
    const b = $(`[data-mode-btn="${S.mode}"]`), ind = $('.modes-ind');
    if (!b.offsetWidth) return;
    ind.style.width = b.offsetWidth + 'px';
    ind.style.transform = `translateX(${b.offsetLeft - 3}px)`;
  }

  function setFill(r) {
    const v = (r.value - r.min) / (r.max - r.min) * 100;
    r.style.setProperty('--v', v + '%');
  }

  function tickStats(stable) {
    const p = profile(S.q), j = stable ? 0 : 1;
    if (T.key !== p.key) seedSeries(p);
    if (!stable && j) {
      T.lat.push(latOf(p) + (Math.random() - .5) * 10); T.lat.shift();
      T.br.push(p.br + (Math.random() - .5) * p.br * .14); T.br.shift();
      if (S.details) renderDetails();
    }
    $('#s-lat').textContent = Math.round(T.lat[59]);
    $('#s-fps').textContent = stable ? p.fps : (Math.random() < .92 ? p.fps : p.fps - 1);
    $('#s-br').textContent = dec(T.br[59]);
  }

  /* ---------- kolejka i odtwarzacz ---------- */
  function renderQueue() {
    const ul = $('#queue');
    ul.innerHTML = queue.map((it, i) => `
      <li><button class="q-item" data-qi="${i}" aria-current="${S.media === 'playing' && i === S.playing}">
        <span class="qt"><svg viewBox="${it.vb}" preserveAspectRatio="xMidYMid slice"><use href="#${it.thumb}"/></svg>${S.media === 'playing' && i === S.playing ? `<span class="now"><svg class="i i-s"><use href="#i-eq"/></svg></span>` : ''}</span>
        <span style="min-width:0"><div class="qn">${esc(it.t)}</div><div class="qm">${esc(it.b[0])}</div></span>
        <span class="qd">${it.label || hms(it.sec)}</span>
      </button></li>`).join('');
    const total = queue.reduce((a, b) => a + b.sec, 0);
    const h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60);
    $('#q-sum').textContent = `${queue.length} ${pl(queue.length, 'pozycja', 'pozycje', 'pozycji')}, ${h ? h + ' godz. ' : ''}${m} min`;
  }

  function renderPlayer() {
    const it = queue[S.playing];
    if (!it) return;
    $('#np-title').textContent = it.t;
    $('#np-file').textContent = it.f;
    $('.np .badges').innerHTML = it.b.map((x, i) => `<span class="badge ${i === 0 && /4K|HDR/.test(x) ? 'hot' : ''}">${esc(x)}</span>`).join('');
    const seek = $('#np-seek');
    seek.max = it.sec || 240;
    seek.value = Math.min(S.pos, seek.max);
    setFill(seek);
    $('#np-pos').textContent = hms(seek.value);
    $('#np-dur').textContent = hms(seek.max);
    $$('.l-movie use, .g-movie use').forEach(u => u.setAttribute('href', '#' + it.thumb));
    $$('.l-movie svg, .g-movie svg').forEach(s => s.setAttribute('viewBox', it.vb));
    $('#play-ico').setAttribute('href', S.paused ? '#i-play' : '#i-pause');
    $('#play-btn').setAttribute('aria-label', S.paused ? 'Odtwórz' : 'Pauza');
  }

  function play(i) {
    S.playing = i; S.pos = i === 0 ? 4328 : 0; S.paused = false;
    const was = S.media;
    S.media = 'playing';
    render();
    if (was !== 'playing') powerOn();
  }

  function addFiles(files) {
    const items = [...files].map(f => {
      const isA = /audio/.test(f.type) || /\.(flac|mp3|ogg|wav|m4a)$/i.test(f.name);
      const isI = /image/.test(f.type) || /\.(jpe?g|png|heic|webp)$/i.test(f.name);
      return { t: f.name.replace(/\.[^.]+$/, '').replace(/[._]/g, ' '), f: f.name, sec: isI ? 0 : 1800, label: isI ? 'zdjęcie' : null,
        b: [isA ? 'Audio' : isI ? 'Zdjęcie' : 'Wideo'], thumb: isA ? 'q-music' : isI ? 'q-tatry' : 'q-sea', vb: '0 0 64 36' };
    });
    if (!items.length) return;
    const start = S.media === 'playing' ? null : queue.length;
    if (S.media !== 'playing') queue = [];
    queue.push(...items);
    S.mode = 'media';
    if (start !== null) play(0); else { S.media = 'playing'; render(); }
  }

  /* ---------- ruch: włączenie ekranu ---------- */
  function powerOn() {
    const scr = $('#screen');
    scr.classList.remove('powering'); void scr.offsetWidth; scr.classList.add('powering');
    setTimeout(() => scr.classList.remove('powering'), 950);
  }

  /* ---------- przepływ udostępniania ---------- */
  let flowTimer = null;
  function clearFlow() { clearTimeout(flowTimer); flowTimer = null; }

  function share() {
    clearFlow();
    S.mode = 'ekran';
    if (!S.permitted) { S.session = 'permission'; S.permLeft = 24; render(); flowTimer = setTimeout(() => { S.permitted = true; connect(); }, 3200); }
    else connect();
  }
  function connect() {
    clearFlow();
    S.session = 'connecting'; render();
    flowTimer = setTimeout(goLive, 900);
  }
  function goLive() {
    clearFlow();
    S.session = 'live'; S.elapsed = 0; render(); powerOn();
  }
  function stop() { clearFlow(); S.session = 'idle'; render(); }

  /* ---------- zegar ---------- */
  setInterval(() => {
    if (SHOT) return;
    if (S.session === 'live') { S.elapsed++; $('#live-time').textContent = clock(S.elapsed); if (S.screen === 'work') tickStats(false); }
    if (S.session === 'permission' && S.permLeft > 0) { S.permLeft--; $('#perm-count').textContent = `Czekam jeszcze ${S.permLeft} s`; }
    if (S.session === 'lost') {
      S.lostIn--;
      if (S.lostIn <= 0) { S.lostIn = 4; connect(); return; }
      $('#lost-try').textContent = `Próba 2/5 · następna za ${S.lostIn} s.`;
    }
    if (S.media === 'playing' && !S.paused) {
      const seek = $('#np-seek');
      S.pos = Math.min(+seek.max, S.pos + 1);
      seek.value = S.pos; setFill(seek); $('#np-pos').textContent = hms(S.pos);
    }
  }, 1000);

  /* ---------- odkrywanie ---------- */
  let discTimer = null;
  function scan() {
    clearTimeout(discTimer);
    S.screen = 'discovery'; S.disc = 'scanning'; render();
    if (!SHOT) discTimer = setTimeout(() => { S.disc = 'found'; render(); }, 2200);
  }

  /* ---------- popovery ---------- */
  let openMenu = null, menuOpener = null;
  function showMenu(id, opener) {
    closeMenu();
    const m = $(id), r = opener.getBoundingClientRect();
    m.classList.add('open');
    if (m.classList.contains('up')) {
      m.style.left = Math.max(12, r.left) + 'px';
      m.style.top = (r.top - m.offsetHeight - 8) + 'px';
    } else {
      m.style.left = (r.right - m.offsetWidth) + 'px';
      m.style.top = (r.bottom + 8) + 'px';
    }
    opener.setAttribute('aria-expanded', 'true');
    openMenu = m; menuOpener = opener;
    const first = m.querySelector('.m-i[aria-checked="true"]') || m.querySelector('.m-i');
    if (!SHOT) first && first.focus();
  }
  function closeMenu(restore) {
    if (!openMenu) return;
    openMenu.classList.remove('open');
    menuOpener && menuOpener.setAttribute('aria-expanded', 'false');
    if (restore && menuOpener) menuOpener.focus();
    openMenu = menuOpener = null;
  }

  /* ---------- arkusz wyboru źródła ---------- */
  const picker = $('#picker');
  let pickerOpener = null;
  function openPicker(tab = 'mon', opener) {
    closeMenu();
    pickerOpener = opener || document.activeElement;
    setPickerTab(tab);
    picker.classList.add('open');
    picker.setAttribute('aria-hidden', 'false');
    if (!SHOT) setTimeout(() => $(`[data-ptab="${tab}"]`).focus(), 30);
  }
  function closePicker() {
    if (!picker.classList.contains('open')) return;
    picker.classList.remove('open');
    picker.setAttribute('aria-hidden', 'true');
    pickerOpener && pickerOpener.focus && pickerOpener.focus();
  }
  function setPickerTab(t) {
    $$('[data-ptab]').forEach(b => { const on = b.dataset.ptab === t; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
    $$('[data-ppanel]').forEach(p => p.hidden = p.dataset.ppanel !== t);
  }
  function applySource() {
    const t = $('.tile[aria-checked="true"]');
    if (t) {
      const [name, desc, sym] = t.dataset.src.split('|');
      $$('.src-name').forEach(e => e.textContent = name);
      $$('.src-desc').forEach(e => e.textContent = desc);
      $('#src-title').textContent = name;
      const vb = sym.startsWith('w-') ? '0 0 160 90' : '0 0 1600 900';
      $$('.l-preview svg, .g-desktop svg, .src .thumb svg').forEach(s => s.setAttribute('viewBox', vb));
      $$('#preview-use, .g-desktop use, .src-thumb-use').forEach(u => u.setAttribute('href', '#' + sym));
    }
    closePicker();
    if (S.session === 'idle') share(); else if (S.session === 'live') powerOn();
  }

  /* ---------- grupy radiowe (wspólna obsługa klawiatury) ---------- */
  function setRadio(group, btn) {
    $$('[role="radio"]', group).forEach(b => { b.setAttribute('aria-checked', String(b === btn)); b.tabIndex = b === btn ? 0 : -1; });
  }
  function initRadios() {
    $$('[role="radiogroup"]').forEach(g => {
      const radios = $$('[role="radio"]', g);
      radios.forEach(b => b.tabIndex = b.getAttribute('aria-checked') === 'true' ? 0 : -1);
      g.addEventListener('keydown', e => {
        const k = e.key, i = radios.indexOf(document.activeElement);
        if (i < 0 || !['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(k)) return;
        e.preventDefault();
        const n = radios[(i + (k === 'ArrowRight' || k === 'ArrowDown' ? 1 : radios.length - 1)) % radios.length];
        n.focus(); n.click();
      });
    });
  }

  function setSmode(v) {
    S.smode = v;
    S.q = v === 'kino' ? 88 : 12;
    $$('[data-group="smode"]').forEach(g => setRadio(g, $(`[data-val="${v}"]`, g)));
    render();
  }

  function setTheme(v) {
    const t = v === 'auto' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : v;
    root.dataset.theme = t;
    const g = $('[data-group="theme"]');
    setRadio(g, $(`[data-val="${v}"]`, g));
    $$('.dev .theme button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.theme === t)));
  }

  /* ---------- ustawienia ---------- */
  function setTab(t) {
    $$('.s-nav [role="tab"]').forEach(b => { const on = b.dataset.tab === t; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
    $$('.s-panel').forEach(p => p.classList.toggle('on', p.dataset.panel === t));
  }

  /* ---------- kliknięcia ---------- */
  document.addEventListener('click', e => {
    const el = e.target.closest('[data-act], [data-mode-btn], [role="radio"], [role="switch"], [data-qi], [data-tab], [data-ptab], [data-sub], .st, .win-ctl button');
    if (openMenu && !e.target.closest('.menu') && !(el && el === menuOpener)) closeMenu();
    if (!el) return;

    if (el.matches('.win-ctl button')) { if (el.getAttribute('aria-label').startsWith('Zamknij')) apply('tray'); return; }
    if (el.dataset.modeBtn) { S.mode = el.dataset.modeBtn; if (S.screen !== 'work') S.screen = 'work'; render(); return; }
    if (el.dataset.qi) { play(+el.dataset.qi); return; }
    if (el.dataset.tab) { setTab(el.dataset.tab); return; }
    if (el.dataset.ptab) { setPickerTab(el.dataset.ptab); return; }
    if (el.dataset.sub) {
      $$('[data-sub]').forEach(b => b.setAttribute('aria-checked', String(b === el)));
      $('#subs-label').textContent = el.dataset.sub === 'wyłączone' ? 'Napisy: wyłączone' : `Napisy: ${el.dataset.sub}`;
      closeMenu(true); return;
    }
    if (el.matches('.st')) { apply(el.dataset.state); return; }

    if (el.getAttribute('role') === 'switch') {
      const v = el.getAttribute('aria-checked') !== 'true';
      (el.dataset.sync ? $$(`[data-sync="${el.dataset.sync}"]`) : [el]).forEach(s => s.setAttribute('aria-checked', String(v)));
      return;
    }
    if (el.getAttribute('role') === 'radio' && !el.dataset.act) {
      if (el.classList.contains('tile')) { $$('.tile').forEach(t => { t.setAttribute('aria-checked', String(t === el)); }); return; }
      const g = el.closest('[role="radiogroup"]');
      if (g.dataset.group === 'smode') { setSmode(el.dataset.val); return; }
      if (g.dataset.group === 'theme') { setTheme(el.dataset.val); return; }
      setRadio(g, el);
      return;
    }

    switch (el.dataset.act) {
      case 'pick-tv':
        S.tv = el.dataset.tv || 'Salon';
        $$('#m-devices [data-tv]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.tv === S.tv)));
        closeMenu(); S.screen = 'work'; render(); break;
      case 'manual': {
        const f = $('#manual-form'), open = f.hidden;
        f.hidden = !open; $('#manual-hint').hidden = !open; el.setAttribute('aria-expanded', String(open));
        if (open) f.querySelector('input').focus();
        break;
      }
      case 'rescan': closeMenu(); scan(); break;
      case 'devices': openMenu === $('#m-devices') ? closeMenu(true) : showMenu('#m-devices', el); break;
      case 'subs': openMenu === $('#m-subs') ? closeMenu(true) : showMenu('#m-subs', el); break;
      case 'settings': S.screen = 'settings'; render(); setTimeout(() => $('.s-nav [aria-selected="true"]').focus(), 0); break;
      case 'back': S.screen = 'work'; render(); break;
      case 'picker': openPicker('mon', el); break;
      case 'close-picker': closePicker(); break;
      case 'apply-src': applySource(); break;
      case 'share': share(); break;
      case 'cancel': case 'stop': stop(); break;
      case 'retry': connect(); break;
      case 'choose-files': $('#file-in').click(); break;
      case 'play-url': {
        const v = $('#url-in').value.trim();
        if (!v) { $('#url-in').focus(); break; }
        let host = v; try { host = new URL(v).hostname; } catch (_) {}
        queue = [{ t: host, f: v, sec: 2700, b: ['Strumień'], thumb: 'q-sea', vb: '0 0 64 36' }];
        play(0); break;
      }
      case 'details': S.details = !S.details; renderDetails(); break;
      case 'playpause': S.paused = !S.paused; render(); break;
      case 'next': play((S.playing + 1) % queue.length); break;
      case 'prev': play((S.playing - 1 + queue.length) % queue.length); break;
      case 'clear': queue = []; S.media = 'empty'; render(); break;
      case 'resume': queue = QUEUE_DEMO.map(x => ({ ...x })); S.mode = 'media'; play(+el.dataset.i); if (+el.dataset.i === 0) { S.pos = 4328; render(); } break;
      case 'dev': {
        const p = $('#dev-panel'); p.hidden = !p.hidden; el.setAttribute('aria-expanded', String(!p.hidden)); break;
      }
    }
  });

  $('#file-in').addEventListener('change', e => addFiles(e.target.files));
  $$('.range').forEach(r => { setFill(r); r.addEventListener('input', () => setFill(r)); });
  $('#q-range').addEventListener('input', e => {
    S.q = +e.target.value;
    const v = S.q >= 67 ? 'kino' : S.q < 34 ? 'gra' : null;
    if (v && v !== S.smode) { S.smode = v; $$('[data-group="smode"]').forEach(g => setRadio(g, $(`[data-val="${v}"]`, g))); }
    if (!v) $$('[data-group="smode"] [role="radio"]').forEach(b => b.setAttribute('aria-checked', 'false'));
    render();
  });
  $('#np-seek').addEventListener('input', e => { S.pos = +e.target.value; $('#np-pos').textContent = hms(S.pos); });

  /* ---------- przeciąganie plików ---------- */
  let dragDepth = 0;
  window.addEventListener('dragenter', e => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files') || S.screen !== 'work') return;
    dragDepth++;
    if (S.media !== 'playing') { S.mode = 'media'; S.media = 'drag'; render(); }
  });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('dragleave', () => { if (--dragDepth <= 0 && S.media === 'drag') { dragDepth = 0; S.media = 'empty'; render(); } });
  window.addEventListener('drop', e => { e.preventDefault(); dragDepth = 0; if (S.media === 'drag') S.media = 'empty'; addFiles(e.dataTransfer.files); render(); });

  /* ---------- klawiatura ---------- */
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (openMenu) { closeMenu(true); return; }
      if (picker.classList.contains('open')) { closePicker(); return; }
      if (S.screen === 'settings') { S.screen = 'work'; render(); $('[data-act="settings"]').focus(); return; }
    }
    // pułapka fokusu w arkuszu
    if (e.key === 'Tab' && picker.classList.contains('open')) {
      const f = $$('button, input, [tabindex="0"]', picker).filter(x => x.offsetParent && x.tabIndex >= 0);
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    // strzałki w menu i listach zakładek
    if (openMenu && ['ArrowDown', 'ArrowUp'].includes(e.key)) {
      e.preventDefault();
      const items = $$('.m-i', openMenu), i = items.indexOf(document.activeElement);
      items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
    }
    const tl = e.target.closest && e.target.closest('[role="tablist"]');
    if (tl && ['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
      e.preventDefault();
      const tabs = $$('[role="tab"]', tl), i = tabs.indexOf(e.target);
      const n = tabs[(i + (['ArrowDown', 'ArrowRight'].includes(e.key) ? 1 : tabs.length - 1)) % tabs.length];
      n.focus(); n.click();
    }
    // spacja: pauza w Multimediach (poza polami)
    if (e.key === ' ' && S.mode === 'media' && S.media === 'playing' && S.screen === 'work' && !e.target.closest('button, input, select')) {
      e.preventDefault(); S.paused = !S.paused; render();
    }
  });

  window.addEventListener('resize', placeModeIndicator);

  /* ---------- panel stanów ---------- */
  const STATES = [
    ['discovery-scan', 'Odkrywanie: szukam'],
    ['discovery', 'Odkrywanie: znalezione'],
    ['notfound', 'Błąd: brak telewizora'],
    ['main', 'Ekran: gotowy'],
    ['picker', 'Źródło: monitory'],
    ['picker-win', 'Źródło: okna'],
    ['devices', 'Menu telewizorów'],
    ['permission', 'Zgoda na telewizorze'],
    ['connecting', 'Łączenie'],
    ['live', 'Na żywo'],
    ['live-details', 'Na żywo: szczegóły'],
    ['live-kino', 'Na żywo, tryb kina'],
    ['lost', 'Błąd: utracono połączenie'],
    ['media-empty', 'Multimedia: pusto'],
    ['media-drag', 'Multimedia: upuszczanie'],
    ['media', 'Multimedia: odtwarzanie'],
    ['subs', 'Menu napisów'],
    ['settings', 'Ustawienia: zaawansowane'],
    ['settings-obraz', 'Ustawienia: obraz'],
    ['tray', 'Zasobnik systemowy'],
  ];
  $('#dev-panel').innerHTML =
    '<div class="dh">Stan</div>' + STATES.map(([k, l]) => `<button class="st" data-state="${k}">${l}</button>`).join('') +
    '<div class="dh">Motyw</div><div class="theme"><button class="btn btn-sm" data-act="theme-dark" data-theme="dark">Ciemny</button><button class="btn btn-sm" data-act="theme-light" data-theme="light">Jasny</button></div>';
  $$('.dev .theme button').forEach(b => b.addEventListener('click', () => setTheme(b.dataset.theme)));

  function apply(name) {
    clearFlow(); clearTimeout(discTimer); closeMenu();
    if (picker.classList.contains('open')) { picker.classList.remove('open'); picker.setAttribute('aria-hidden', 'true'); }
    const base = { screen: 'work', mode: 'ekran', session: 'idle', disc: 'found' };
    const media = () => { if (!queue.length) queue = QUEUE_DEMO.map(x => ({ ...x })); };
    switch (name) {
      case 'discovery-scan': Object.assign(S, { screen: 'discovery', disc: 'scanning' }); if (!SHOT) { render(); scan(); return; } break;
      case 'discovery': Object.assign(S, { screen: 'discovery', disc: 'found' }); break;
      case 'notfound': Object.assign(S, { screen: 'notfound', disc: 'found' }); { const f = $('#manual-form'); f.hidden = false; $('#manual-hint').hidden = false; } break;
      case 'main': Object.assign(S, base); break;
      case 'picker': Object.assign(S, base); render(); openPicker('mon', $('[data-act="picker"]')); break;
      case 'picker-win': Object.assign(S, base); render(); $$('.tile').forEach(t => t.setAttribute('aria-checked', String(t.dataset.src.startsWith('Firefox')))); openPicker('win', $('[data-act="picker"]')); break;
      case 'devices': Object.assign(S, base); render(); showMenu('#m-devices', $('.tv-pill')); break;
      case 'permission': Object.assign(S, base, { session: 'permission', permLeft: 24 }); break;
      case 'connecting': Object.assign(S, base, { session: 'connecting' }); break;
      case 'live': Object.assign(S, base, { session: 'live', elapsed: 767, smode: 'gra', q: 12, details: false }); break;
      case 'live-details': Object.assign(S, base, { session: 'live', elapsed: 767, smode: 'gra', q: 12, details: true }); break;
      case 'live-kino': Object.assign(S, base, { session: 'live', elapsed: 767, smode: 'kino', q: 88, details: false }); break;
      case 'lost': Object.assign(S, base, { session: 'live', elapsed: 767 }); S.session = 'lost'; S.lostIn = 4; break;
      case 'media-empty': Object.assign(S, base, { mode: 'media', media: 'empty' }); queue = []; break;
      case 'media-drag': Object.assign(S, base, { mode: 'media', media: 'drag' }); queue = []; break;
      case 'media': Object.assign(S, base, { mode: 'media', media: 'playing', playing: 0, pos: 4328, paused: false }); media(); break;
      case 'subs': Object.assign(S, base, { mode: 'media', media: 'playing', playing: 0, pos: 4328 }); media(); render(); showMenu('#m-subs', $('[data-act="subs"]')); break;
      case 'settings': Object.assign(S, { screen: 'settings' }); setTab('zaawansowane'); break;
      case 'settings-obraz': Object.assign(S, { screen: 'settings' }); setTab('obraz'); break;
      case 'tray': Object.assign(S, { screen: 'tray' }); break;
    }
    $$('[data-group="smode"]').forEach(g => setRadio(g, $(`[data-val="${S.smode}"]`, g)));
    $$('.st').forEach(b => b.setAttribute('aria-current', String(b.dataset.state === name)));
    if (!['picker', 'picker-win', 'devices', 'subs'].includes(name)) render();
    else { const m = openMenu; render(); if (m) { /* przelicz pozycję po renderze */ const o = menuOpener; closeMenu(); showMenu('#' + m.id, o); } }
  }

  /* ---------- start ---------- */
  initRadios();
  const themeArg = parts.find(p => p === 'light' || p === 'dark');
  setTheme(themeArg || 'dark');
  const stateArg = parts.find(p => STATES.some(s => s[0] === p));
  render();
  apply(stateArg || 'discovery-scan');
  document.fonts && document.fonts.ready.then(placeModeIndicator);
})();
