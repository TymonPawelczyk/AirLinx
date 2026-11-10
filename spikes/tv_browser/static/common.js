// Wspólne: synchronizacja zegara z serwerem (NTP-style) + kod paskowy czasu.
'use strict';

function wsUrl(path) {
  return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path;
}

// ---------------------------------------------------------------- clock sync
class ClockSync {
  constructor() {
    this.offset = Date.now() - performance.now(); // przybliżenie do czasu pierwszej synchronizacji
    this.rtt = null;
    this.samples = [];
    this.synced = false;
    this.ws = null;
    this._pending = null;
  }
  // serverMs ≈ performance.now() + offset
  now() { return performance.now() + this.offset; }
  perfToServer(p) { return p + this.offset; }

  _open() {
    return new Promise((res, rej) => {
      const ws = new WebSocket(wsUrl('/ws/clock'));
      ws.onopen = () => res(ws);
      ws.onerror = (e) => rej(e);
      ws.onmessage = (ev) => {
        const t1 = performance.now();
        const m = JSON.parse(ev.data);
        if (this._pending) { const p = this._pending; this._pending = null; p({ t0: m.t0, t1, s: m.s }); }
      };
      ws.onclose = () => { this.ws = null; };
    });
  }
  _ping() {
    return new Promise((res) => {
      this._pending = res;
      this.ws.send(String(performance.now()));
      setTimeout(() => { if (this._pending === res) { this._pending = null; res(null); } }, 1000);
    });
  }
  async sync(n = 15) {
    if (!this.ws) this.ws = await this._open();
    const fresh = [];
    for (let i = 0; i < n; i++) {
      const r = await this._ping();
      if (r) fresh.push({ rtt: r.t1 - r.t0, offset: r.s - (r.t0 + r.t1) / 2 });
      await new Promise((ok) => setTimeout(ok, 20));
    }
    if (!fresh.length) return false;
    this.samples = this.samples.concat(fresh).slice(-60);
    // najlepsze 25% próbek wg RTT -> mediana offsetu
    const sorted = this.samples.slice().sort((a, b) => a.rtt - b.rtt);
    const best = sorted.slice(0, Math.max(1, Math.floor(sorted.length / 4)));
    const offs = best.map((x) => x.offset).sort((a, b) => a - b);
    this.offset = offs[Math.floor(offs.length / 2)];
    this.rtt = best[0].rtt;
    this.synced = true;
    return true;
  }
  startAuto(intervalMs = 20000) {
    const loop = async () => { try { await this.sync(8); } catch (e) { /* ignore */ } };
    return setInterval(loop, intervalMs);
  }
}

function fmtClock(ms) {
  const d = new Date(ms);
  const p = (n, w) => String(n).padStart(w, '0');
  return p(d.getHours(), 2) + ':' + p(d.getMinutes(), 2) + ':' + p(d.getSeconds(), 2) + '.' + p(d.getMilliseconds(), 3);
}

// ---------------------------------------------------------------- barcode
// 32 komórki w poprzek CAŁEJ szerokości, górne 10% wysokości ekranu:
// [1][0][24 bity: ms mod 2^24, MSB first][6 bitów sumy kontrolnej]
const BC = { CELLS: 32, HFRAC: 0.10, MOD: 16777216 };

function bcCheck(v) { return (v ^ (v >>> 6) ^ (v >>> 12) ^ (v >>> 18)) & 63; }

function bcBits(ms) {
  const v = Math.floor(ms) % BC.MOD;
  const bits = [1, 0];
  for (let i = 23; i >= 0; i--) bits.push((v >>> i) & 1);
  const c = bcCheck(v);
  for (let i = 5; i >= 0; i--) bits.push((c >>> i) & 1);
  return bits;
}

function bcDraw(ctx, W, H, ms) {
  const bits = bcBits(ms);
  const h = Math.round(H * BC.HFRAC);
  const cw = W / BC.CELLS;
  for (let i = 0; i < BC.CELLS; i++) {
    ctx.fillStyle = bits[i] ? '#fff' : '#000';
    const x0 = Math.floor(i * cw), x1 = Math.floor((i + 1) * cw);
    ctx.fillRect(x0, 0, x1 - x0, h);
  }
}

// Sampler: rysuje górny pasek źródła do małego canvasa i dekoduje.
class BarcodeReader {
  constructor() {
    this.cv = document.createElement('canvas');
    this.cv.width = BC.CELLS * 8; this.cv.height = 8;
    this.ctx = this.cv.getContext('2d', { willReadFrequently: true });
    this.lat = []; this.ok = 0; this.bad = 0; this.last = null;
  }
  // src: video/VideoFrame/canvas; sw/sh: wymiary źródła; displayServerMs: czas serwera, gdy klatka jest wyświetlana
  read(src, sw, sh, displayServerMs) {
    if (!sw || !sh) return null;
    try {
      this.ctx.drawImage(src, 0, 0, sw, Math.max(1, Math.round(sh * BC.HFRAC)), 0, 0, this.cv.width, this.cv.height);
    } catch (e) { return null; }
    const d = this.ctx.getImageData(0, 0, this.cv.width, this.cv.height).data;
    const lum = [];
    for (let i = 0; i < BC.CELLS; i++) {
      const x = i * 8 + 4, y = 4, o = (y * this.cv.width + x) * 4;
      lum.push(0.299 * d[o] + 0.587 * d[o + 1] + 0.114 * d[o + 2]);
    }
    if (lum[0] - lum[1] < 80) { this.bad++; return null; }
    const thr = (lum[0] + lum[1]) / 2;
    let v = 0, c = 0;
    for (let i = 2; i < 26; i++) v = v * 2 + (lum[i] > thr ? 1 : 0);
    for (let i = 26; i < 32; i++) c = c * 2 + (lum[i] > thr ? 1 : 0);
    if (bcCheck(v) !== c) { this.bad++; return null; }
    const now = displayServerMs;
    const nowMod = ((Math.floor(now) % BC.MOD) + BC.MOD) % BC.MOD;
    const full = Math.floor(now) - (((nowMod - v) % BC.MOD) + BC.MOD) % BC.MOD;
    const lat = now - full;
    if (lat < 0 || lat > 10000) { this.bad++; return null; }
    this.ok++;
    this.last = lat;
    this.lat.push(lat); if (this.lat.length > 120) this.lat.shift();
    return lat;
  }
  median() {
    if (!this.lat.length) return null;
    const s = this.lat.slice().sort((a, b) => a - b);
    return s[Math.floor(s.length / 2)];
  }
}

// self-test kodu paskowego (bez wideo)
function bcSelfTest() {
  const cv = document.createElement('canvas'); cv.width = 1920; cv.height = 1080;
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#333'; ctx.fillRect(0, 0, 1920, 1080);
  const t = Date.now();
  bcDraw(ctx, 1920, 1080, t - 123);
  const r = new BarcodeReader();
  const lat = r.read(cv, 1920, 1080, t);
  return { expected: 123, got: lat, pass: lat !== null && Math.abs(lat - 123) < 1 };
}

class Rolling {
  constructor(n = 120) { this.n = n; this.a = []; }
  push(v) { if (v === null || v === undefined || isNaN(v)) return; this.a.push(v); if (this.a.length > this.n) this.a.shift(); }
  avg() { return this.a.length ? this.a.reduce((x, y) => x + y, 0) / this.a.length : null; }
  med() { if (!this.a.length) return null; const s = this.a.slice().sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; }
  p95() { if (!this.a.length) return null; const s = this.a.slice().sort((x, y) => x - y); return s[Math.floor(s.length * 0.95)]; }
}
