(() => {
  'use strict';
  const BUY = '#27d7f4';
  const SELL = '#ef4d9a';
  const NEUTRAL = '#697386';
  const VALID = new Set(['1m','3m','5m','15m']);
  let tf = '1m';
  let ws = null;
  let reconnectTimer = null;
  let lastPayload = null;

  const $ = (id) => document.getElementById(id);
  const stateEl = () => $('aggTriggerState');
  const tfEl = () => $('aggTriggerTf');
  const strip = () => $('aggressionTriggerStrip');
  const canvas = () => $('aggressionDeltaCanvas');

  function deviceScale() { return Math.max(1, Math.min(2, window.devicePixelRatio || 1)); }

  function resizeCanvas(c) {
    const rect = c.getBoundingClientRect();
    const dpr = deviceScale();
    const w = Math.max(240, Math.floor(rect.width * dpr));
    const h = Math.max(54, Math.floor(rect.height * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    return { w, h, dpr };
  }

  function draw(payload) {
    lastPayload = payload || lastPayload;
    const c = canvas();
    if (!c || !lastPayload) return;
    const { w, h, dpr } = resizeCanvas(c);
    const ctx = c.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    const candles = (lastPayload.candles || []).slice(-30);
    if (!candles.length) {
      ctx.fillStyle = 'rgba(148,163,184,.55)';
      ctx.font = `${11*dpr}px system-ui, sans-serif`;
      ctx.fillText('Esperando agresión observada…', 10*dpr, h/2);
      updateState(lastPayload);
      return;
    }
    let min = Infinity, max = -Infinity;
    for (const x of candles) {
      min = Math.min(min, Number(x.low)||0, Number(x.open)||0, Number(x.close)||0);
      max = Math.max(max, Number(x.high)||0, Number(x.open)||0, Number(x.close)||0);
    }
    if (!Number.isFinite(min) || !Number.isFinite(max)) { min=-1; max=1; }
    if (Math.abs(max-min) < 1e-9) { min -= 1; max += 1; }
    const padY = 7*dpr;
    const y = v => padY + (max - v) / (max - min) * (h - 2*padY);
    const step = w / Math.max(candles.length, 12);
    const bodyW = Math.max(3*dpr, Math.min(10*dpr, step*.56));
    // Zero reference, intentionally subtle.
    if (min < 0 && max > 0) {
      ctx.strokeStyle = 'rgba(148,163,184,.12)'; ctx.lineWidth = dpr;
      ctx.beginPath(); ctx.moveTo(0,y(0)); ctx.lineTo(w,y(0)); ctx.stroke();
    }
    candles.forEach((bar, i) => {
      const dir = String(bar.direction || 'NEUTRAL').toUpperCase();
      const col = dir === 'BUY' ? BUY : dir === 'SELL' ? SELL : NEUTRAL;
      const x = step*i + step*.5;
      const yo = y(Number(bar.open)||0), yc = y(Number(bar.close)||0);
      const yh = y(Number(bar.high)||0), yl = y(Number(bar.low)||0);
      ctx.save();
      if (bar.forming && dir !== 'NEUTRAL') { ctx.shadowColor = col; ctx.shadowBlur = 9*dpr; }
      ctx.strokeStyle = col; ctx.lineWidth = Math.max(dpr, 1.2*dpr);
      ctx.beginPath(); ctx.moveTo(x,yh); ctx.lineTo(x,yl); ctx.stroke();
      const top = Math.min(yo,yc), bh = Math.max(2*dpr, Math.abs(yc-yo));
      ctx.fillStyle = col;
      ctx.globalAlpha = bar.forming ? .78 : .94;
      ctx.fillRect(x-bodyW/2, top, bodyW, bh);
      if (bar.forming) {
        ctx.globalAlpha = .95; ctx.setLineDash([3*dpr,2*dpr]); ctx.strokeRect(x-bodyW/2-1*dpr, top-1*dpr, bodyW+2*dpr, bh+2*dpr);
      }
      ctx.restore();
    });
    updateState(lastPayload);
  }

  function updateState(payload) {
    const frames = payload.frames || {};
    const cur = frames[tf] || (payload.live || {}) || {};
    const dir = String(cur.direction || 'NEUTRAL').toUpperCase();
    const el = stateEl(); if (el) el.textContent = dir === 'BUY' ? 'COMPRA' : dir === 'SELL' ? 'VENTA' : 'NEUTRAL';
    if (tfEl()) tfEl().textContent = `${tf} LIVE`;
    if (strip()) strip().dataset.state = dir;
  }

  async function restOnce() {
    try {
      const r = await fetch(`/api/aggression-delta?timeframe=${encodeURIComponent(tf)}&limit=96`, {cache:'no-store'});
      if (r.ok) draw(await r.json());
    } catch (_) {}
  }

  function connect() {
    if (!canvas()) return;
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      ws = new WebSocket(`${proto}//${location.host}/ws/aggression-delta?timeframe=${encodeURIComponent(tf)}`);
      ws.onmessage = ev => { try { draw(JSON.parse(ev.data)); } catch (_) {} };
      ws.onclose = () => { ws=null; reconnectTimer=setTimeout(connect, 1200); };
      ws.onerror = () => { try { ws.close(); } catch (_) {} };
    } catch (_) { reconnectTimer=setTimeout(connect, 1500); }
    restOnce();
  }

  function setTimeframe(value) {
    const v = String(value || '').toLowerCase();
    if (!VALID.has(v) || v === tf) return;
    tf = v;
    document.querySelectorAll('[data-trace-tf]').forEach(b => b.classList.toggle('active', b.dataset.traceTf === tf));
    const sel = $('traceCandle'); if (sel && sel.value !== tf) sel.value=tf;
    if (tfEl()) tfEl().textContent = `${tf} LIVE`;
    connect();
  }

  function init() {
    if (!canvas()) return;
    document.querySelectorAll('[data-trace-tf]').forEach(btn => btn.addEventListener('click', () => setTimeframe(btn.dataset.traceTf)));
    const sel = $('traceCandle'); if (sel) sel.addEventListener('change', () => setTimeframe(sel.value));
    window.addEventListener('resize', () => draw(lastPayload), {passive:true});
    connect();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true}); else init();
})();
