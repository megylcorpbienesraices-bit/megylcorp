/* ITM QUANT · Núcleo de render fluido v1.41.0
 *
 * Un solo bucle requestAnimationFrame para toda la terminal. Cada panel declara
 * un draw(ctx, view) y el núcleo se encarga de:
 *   - escalado por devicePixelRatio (nitidez real en cualquier pantalla)
 *   - ResizeObserver (el canvas nunca se queda con tamaño viejo al cambiar de sección)
 *   - interpolación de valores entre snapshots (las barras se deslizan, no saltan)
 *   - zoom/pan compartido en el eje temporal entre paneles enlazados
 *   - redibujado sólo cuando hay algo que dibujar (no quema CPU en reposo)
 *
 * No contiene matemática de mercado. Sólo presentación.
 */
(function (global) {
  'use strict';

  const PANELS = new Map();
  const RENDER_ERRORS = [];
  let rafId = null;
  let lastFrame = 0;

  /* ---------------------------------------------------------------- utils */

  const clamp = (v, lo, hi) => v < lo ? lo : (v > hi ? hi : v);
  const isNum = v => typeof v === 'number' && Number.isFinite(v);
  // Number(null) es 0 y Number('') es 0: sin este filtro un dato ausente se
  // mostraría como un cero real, que en un panel de exposición es una afirmación
  // falsa, no un hueco. Ausente y cero deben poder distinguirse.
  const num = (v, d = 0) => {
    if (v === null || v === undefined || v === '') return d;
    const x = Number(v);
    return Number.isFinite(x) ? x : d;
  };

  function lerp(a, b, t) { return a + (b - a) * t; }

  /** Interpolación exponencial independiente del framerate. */
  function approach(current, target, dt, halfLifeMs) {
    if (!isNum(current)) return target;
    if (!isNum(target)) return current;
    if (halfLifeMs <= 0) return target;
    const k = 1 - Math.pow(0.5, dt / halfLifeMs);
    const next = lerp(current, target, k);
    return Math.abs(next - target) < 1e-9 ? target : next;
  }

  /** Lo que se escribe cuando NO hay dato. Nunca un cero. */
  const NO_DATA = '—';

  /**
   * v1.46.0 · Un hueco deja de formatearse como cero.
   *
   * `num(null)` devuelve 0, así que `money(null)` escribía `$0.0` y
   * `signedCompact(null)` escribía `0.00`. Eso convierte la AUSENCIA de un dato
   * en la AFIRMACIÓN de que vale cero, que es precisamente lo que la
   * especificación prohíbe: «hoy no hubo prima direccional» y «no pudimos
   * medir la prima direccional» son dos cosas distintas y se veían igual.
   *
   * El guardia vive en el formateador, no en cada una de las treinta y tantas
   * llamadas: una regla en un sitio se sostiene, treinta y tantas no.
   */
  function compact(v, digits) {
    if (!isNum(num(v, NaN))) return NO_DATA;
    const x = num(v, 0);
    const a = Math.abs(x);
    const d = digits === undefined ? 1 : digits;
    if (a >= 1e12) return (x / 1e12).toFixed(d) + 'T';
    if (a >= 1e9) return (x / 1e9).toFixed(d) + 'B';
    if (a >= 1e6) return (x / 1e6).toFixed(d) + 'M';
    if (a >= 1e3) return (x / 1e3).toFixed(d) + 'K';
    return x.toFixed(a < 10 ? d : 0);
  }

  function money(v, digits) {
    if (!isNum(num(v, NaN))) return NO_DATA;
    const x = num(v, 0);
    return (x < 0 ? '-$' : '$') + compact(Math.abs(x), digits);
  }

  function signedCompact(v, digits) {
    if (!isNum(num(v, NaN))) return NO_DATA;
    const x = num(v, 0);
    return (x > 0 ? '+' : x < 0 ? '−' : '') + compact(Math.abs(x), digits);
  }

  /** Minutos como duración legible: 47 min, 2h 15m, 3d 4h. */
  function fmtMinutes(v) {
    const m = Math.max(0, Math.round(num(v, 0)));
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60), rm = m % 60;
    if (h < 24) return rm ? `${h}h ${rm}m` : `${h}h`;
    const d = Math.floor(h / 24), rh = h % 24;
    return rh ? `${d}d ${rh}h` : `${d}d`;
  }

  function hhmm(ts) {
    const d = ts instanceof Date ? ts : new Date(ts);
    if (Number.isNaN(d.getTime())) return '';
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  function parseTime(v) {
    if (v === null || v === undefined) return NaN;
    if (typeof v === 'number') return v;
    const t = Date.parse(v);
    if (Number.isFinite(t)) return t;
    // Marcas de tiempo sin zona horaria: el backend las emite en hora local de mercado.
    const t2 = Date.parse(String(v) + 'Z');
    return Number.isFinite(t2) ? t2 : NaN;
  }

  /** Lee un token del sistema de diseño para que el canvas siga al tema activo. */
  const themeCache = new Map();
  let themeStamp = '';
  function token(name, fallback) {
    const stamp = document.documentElement.getAttribute('data-theme') || 'dark';
    if (stamp !== themeStamp) { themeCache.clear(); themeStamp = stamp; }
    if (themeCache.has(name)) return themeCache.get(name);
    let v = '';
    try { v = getComputedStyle(document.documentElement).getPropertyValue(name).trim(); } catch (_) { v = ''; }
    const out = v || fallback;
    themeCache.set(name, out);
    return out;
  }

  /** rgba() a partir de un token hex del tema. */
  function alpha(hex, a) {
    const h = String(hex || '').trim();
    if (h.startsWith('rgb')) return h.replace(/rgba?\(([^)]+)\)/, (_, inner) => {
      const parts = inner.split(',').map(s => s.trim()).slice(0, 3);
      return `rgba(${parts.join(',')},${a})`;
    });
    const m = /^#?([0-9a-f]{6})$/i.exec(h);
    if (!m) return `rgba(148,163,184,${a})`;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  /* --------------------------------------------------------------- escalas */

  /** Escala lineal reversible. */
  function scale(d0, d1, r0, r1) {
    const span = (d1 - d0) || 1e-9;
    const f = v => r0 + (v - d0) / span * (r1 - r0);
    f.invert = p => d0 + (p - r0) / ((r1 - r0) || 1e-9) * span;
    f.domain = [d0, d1];
    f.range = [r0, r1];
    return f;
  }

  /** Ticks "redondos" para un eje de precio/valor. */
  function niceTicks(lo, hi, count) {
    if (!isNum(lo) || !isNum(hi) || hi <= lo) return [];
    const target = Math.max(2, count || 6);
    const raw = (hi - lo) / target;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Number(v.toFixed(10)));
    return out;
  }

  /* -------------------------------------------------- estado interpolado */

  /**
   * Mantiene un mapa clave->valor que se desliza hacia el objetivo.
   * Se usa para perfiles por strike: al llegar un snapshot nuevo las barras
   * viajan a su nueva longitud en lugar de parpadear.
   */
  class Glide {
    constructor(halfLifeMs) {
      this.half = halfLifeMs === undefined ? 130 : halfLifeMs;
      this.cur = new Map();
      this.tgt = new Map();
    }
    /* v1.47.0 · La PRIMERA aparición de una clave se adopta de golpe.
     *
     * Antes toda clave empezaba en 0 y subía animándose, así que el primer
     * fotograma de un panel dibujaba TODAS las barras a cero mientras el eje ya
     * mostraba la escala correcta: un panel con eje de ±4.2 B y ni una sola
     * barra. Se arreglaba solo a los pocos fotogramas… si el bucle de animación
     * seguía vivo. Un panel que entra en pantalla por el observador de
     * visibilidad dibuja UN fotograma, y ése era el que se quedaba.
     *
     * Adoptar el primer valor y animar sólo los CAMBIOS posteriores conserva la
     * transición donde tiene sentido —un valor que se mueve— y elimina la que
     * no lo tenía: la de un dato que acaba de llegar contra un cero que nunca
     * existió.
     */
    set(key, value) {
      const v = num(value, 0);
      this.tgt.set(key, v);
      if (!this.cur.has(key)) this.cur.set(key, v);
    }
    setAll(entries) {
      this.tgt = new Map();
      for (const [k, v] of entries) {
        const n = num(v, 0);
        this.tgt.set(k, n);
        if (!this.cur.has(k)) this.cur.set(k, n);
      }
      for (const k of Array.from(this.cur.keys())) if (!this.tgt.has(k)) this.tgt.set(k, 0);
    }
    get(key) { const v = this.cur.get(key); return isNum(v) ? v : 0; }
    /** @returns {boolean} true mientras siga habiendo movimiento pendiente */
    step(dt) {
      let moving = false;
      for (const [k, t] of this.tgt) {
        const c = this.cur.has(k) ? this.cur.get(k) : 0;
        const n = approach(c, t, dt, this.half);
        if (Math.abs(n - t) > 1e-7) moving = true;
        this.cur.set(k, n);
      }
      if (!moving) {
        for (const [k, t] of this.tgt) if (t === 0 && Math.abs(this.cur.get(k) || 0) < 1e-7) this.cur.delete(k);
      }
      return moving;
    }
  }

  /** Escalar que se desliza (spot, máximos de eje, etc.). */
  class GlideValue {
    constructor(halfLifeMs, initial) {
      this.half = halfLifeMs === undefined ? 160 : halfLifeMs;
      this.cur = isNum(initial) ? initial : NaN;
      this.tgt = this.cur;
    }
    set(v) { if (isNum(v)) { this.tgt = v; if (!isNum(this.cur)) this.cur = v; } }
    get() { return isNum(this.cur) ? this.cur : this.tgt; }
    step(dt) {
      if (!isNum(this.tgt)) return false;
      const n = approach(this.cur, this.tgt, dt, this.half);
      const moving = Math.abs(n - this.tgt) > 1e-7;
      this.cur = n;
      return moving;
    }
  }

  /* ----------------------------------------------------------- eje enlazado */

  /**
   * Ventana temporal compartida. TRACE y el panel de flujo miran el mismo reloj,
   * así que el zoom/pan de uno mueve al otro sin recalcular nada del motor.
   */
  class TimeLink {
    constructor() {
      this.t0 = NaN; this.t1 = NaN;
      this.follow = true;
      this.listeners = new Set();
      this.cursor = NaN;
    }
    on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
    emit() { for (const fn of this.listeners) { try { fn(this); } catch (_) { } } }
    setWindow(t0, t1, opts) {
      if (!isNum(t0) || !isNum(t1) || t1 <= t0) return;
      this.t0 = t0; this.t1 = t1;
      if (!opts || opts.silent !== true) this.emit();
    }
    setCursor(t) { this.cursor = t; this.emit(); }
    span() { return (this.t1 - this.t0) || 0; }
    zoomAt(factor, anchorT) {
      if (!isNum(this.t0) || !isNum(this.t1)) return;
      const a = isNum(anchorT) ? anchorT : (this.t0 + this.t1) / 2;
      const s = this.span();
      const ns = clamp(s * factor, 60_000, 30 * 24 * 3600_000);
      const ratio = (a - this.t0) / (s || 1);
      this.t0 = a - ns * ratio;
      this.t1 = this.t0 + ns;
      this.follow = false;
      this.emit();
    }
    panBy(ms) {
      if (!isNum(this.t0)) return;
      this.t0 += ms; this.t1 += ms;
      this.follow = false;
      this.emit();
    }
  }

  /* --------------------------------------------------------------- Panel */

  /**
   * Un panel = un <canvas> dentro de un contenedor + una función de dibujo.
   * El núcleo llama a draw(ctx, env) sólo cuando el panel está visible.
   */
  class Panel {
    constructor(host, draw, opts) {
      this.host = typeof host === 'string' ? document.getElementById(host) : host;
      if (!this.host) throw new Error('ITMQ: host de panel inexistente');
      this.opts = opts || {};
      this.draw = draw;
      this.canvas = document.createElement('canvas');
      this.canvas.className = 'itmq-canvas';
      this.host.appendChild(this.canvas);
      this.ctx = this.canvas.getContext('2d', { alpha: true, desynchronized: true });
      this.w = 0; this.h = 0; this.dpr = 1;
      this.dirty = true;
      this.animating = false;
      this.data = null;
      this.pointer = { x: NaN, y: NaN, inside: false, down: false };
      this.id = this.opts.id || (this.host.id || 'panel') + ':' + Math.random().toString(36).slice(2, 7);

      this._ro = new ResizeObserver(() => { this.resize(); });
      this._ro.observe(this.host);

      this._io = new IntersectionObserver(entries => {
        for (const e of entries) this.visible = e.isIntersecting;
        if (this.visible) { this.resize(); this.invalidate(); }
      }, { threshold: 0.01 });
      this._io.observe(this.host);
      this.visible = true;

      this._bindPointer();
      this.resize();
      PANELS.set(this.id, this);
      ensureLoop();
    }

    _bindPointer() {
      const c = this.canvas;
      const pos = ev => {
        const r = c.getBoundingClientRect();
        this.pointer.x = ev.clientX - r.left;
        this.pointer.y = ev.clientY - r.top;
      };
      c.addEventListener('pointermove', ev => {
        pos(ev); this.pointer.inside = true;
        if (this.opts.onPointer) this.opts.onPointer(this.pointer, ev, this);
        this.invalidate();
      });
      c.addEventListener('pointerleave', () => {
        this.pointer.inside = false; this.pointer.x = NaN; this.pointer.y = NaN;
        if (this.opts.onPointerLeave) this.opts.onPointerLeave(this);
        this.invalidate();
      });
      c.addEventListener('pointerdown', ev => {
        pos(ev); this.pointer.down = true;
        try { c.setPointerCapture(ev.pointerId); } catch (_) { }
        if (this.opts.onDown) this.opts.onDown(this.pointer, ev, this);
      });
      c.addEventListener('pointerup', ev => {
        this.pointer.down = false;
        try { c.releasePointerCapture(ev.pointerId); } catch (_) { }
        if (this.opts.onUp) this.opts.onUp(this.pointer, ev, this);
      });
      if (this.opts.onWheel) {
        c.addEventListener('wheel', ev => { this.opts.onWheel(ev, this); }, { passive: false });
      }
      if (this.opts.onClick) {
        c.addEventListener('click', ev => { pos(ev); this.opts.onClick(this.pointer, ev, this); });
      }
    }

    resize() {
      const r = this.host.getBoundingClientRect();
      const dpr = clamp(global.devicePixelRatio || 1, 1, 2.5);
      const w = Math.max(1, Math.round(r.width));
      const h = Math.max(1, Math.round(r.height));
      if (w === this.w && h === this.h && dpr === this.dpr) return;
      this.w = w; this.h = h; this.dpr = dpr;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.canvas.style.width = w + 'px';
      this.canvas.style.height = h + 'px';
      this.invalidate();
    }

    setData(data) { this.data = data; this.invalidate(); }
    invalidate() { this.dirty = true; ensureLoop(); }
    /** Declara que hay una transición en curso: el panel se redibuja cada frame. */
    animate(on) { this.animating = !!on; if (on) ensureLoop(); }

    destroy() {
      try { this._ro.disconnect(); } catch (_) { }
      try { this._io.disconnect(); } catch (_) { }
      try { this.canvas.remove(); } catch (_) { }
      PANELS.delete(this.id);
    }

    _frame(dt) {
      if (!this.visible || this.w <= 1 || this.h <= 1) return;
      if (!this.dirty && !this.animating) return;
      const ctx = this.ctx;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      this.dirty = false;
      try {
        const keep = this.draw(ctx, { w: this.w, h: this.h, dt, panel: this });
        this.animating = keep === true;
      } catch (err) {
        this.animating = false;
        drawFailure(ctx, this.w, this.h, err);
        // Un error dentro de draw() deja el panel congelado y sin datos en pantalla.
        // Se registra en un sitio consultable para que una regresión así no pueda
        // pasar inadvertida ni en revisión ni en las pruebas de navegador.
        RENDER_ERRORS.push({ panel: this.id, message: String((err && err.message) || err), at: Date.now() });
        if (RENDER_ERRORS.length > 50) RENDER_ERRORS.shift();
        if (global.console) console.error('[ITMQ panel]', this.id, err);
      }
    }
  }

  function drawFailure(ctx, w, h, err) {
    ctx.save();
    ctx.fillStyle = alpha(token('--danger', '#ef4444'), 0.10);
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = token('--text-dim', '#94a3b8');
    ctx.font = '11px ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.fillText('RENDER NO DISPONIBLE · ' + String((err && err.message) || err).slice(0, 60), w / 2, h / 2);
    ctx.restore();
  }

  function ensureLoop() {
    if (rafId !== null) return;
    lastFrame = performance.now();
    rafId = requestAnimationFrame(tick);
  }

  function tick(now) {
    rafId = null;
    const dt = clamp(now - lastFrame, 0, 64);
    lastFrame = now;
    let busy = false;
    for (const p of PANELS.values()) {
      p._frame(dt);
      if (p.animating || p.dirty) busy = true;
    }
    if (busy) rafId = requestAnimationFrame(tick);
  }

  /* --------------------------------------------------------- primitivas */

  function roundRect(ctx, x, y, w, h, r) {
    const rr = Math.min(Math.abs(r), Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  /** Rejilla horizontal + etiquetas de valor a la derecha. */
  function gridY(ctx, box, sy, ticks, fmt, opts) {
    const o = opts || {};
    ctx.save();
    ctx.strokeStyle = alpha(token('--grid', '#243044'), o.strong ? 0.9 : 0.55);
    ctx.lineWidth = 1;
    ctx.font = (o.font || '10px ui-monospace, monospace');
    ctx.fillStyle = token('--text-dim', '#8494ad');
    ctx.textBaseline = 'middle';
    for (const t of ticks) {
      const y = Math.round(sy(t)) + 0.5;
      if (y < box.y - 1 || y > box.y + box.h + 1) continue;
      ctx.beginPath(); ctx.moveTo(box.x, y); ctx.lineTo(box.x + box.w, y); ctx.stroke();
      if (o.labels !== false) {
        ctx.textAlign = o.labelSide === 'left' ? 'right' : 'left';
        const lx = o.labelSide === 'left' ? box.x - 6 : box.x + box.w + 6;
        ctx.fillText(fmt ? fmt(t) : String(t), lx, y);
      }
    }
    ctx.restore();
  }

  /** Eje temporal inferior con saltos adaptados al ancho disponible. */
  function axisX(ctx, box, sx, t0, t1, opts) {
    const o = opts || {};
    const span = t1 - t0;
    if (!isNum(span) || span <= 0) return;
    const targets = [60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3, 7200e3, 14400e3, 86400e3];
    const want = span / Math.max(2, Math.floor(box.w / 84));
    let step = targets[targets.length - 1];
    for (const t of targets) if (t >= want) { step = t; break; }
    ctx.save();
    ctx.font = '10px ui-monospace, monospace';
    ctx.fillStyle = token('--text-dim', '#8494ad');
    ctx.strokeStyle = alpha(token('--grid', '#243044'), 0.5);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.lineWidth = 1;
    const start = Math.ceil(t0 / step) * step;
    for (let t = start; t <= t1; t += step) {
      const x = Math.round(sx(t)) + 0.5;
      if (x < box.x || x > box.x + box.w) continue;
      if (o.grid !== false) { ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h); ctx.stroke(); }
      ctx.fillText(hhmm(t), x, box.y + box.h + 5);
    }
    ctx.restore();
  }

  /** Etiqueta tipo "chip" usada para niveles y precios. */
  function chip(ctx, x, y, text, opts) {
    const o = opts || {};
    ctx.save();
    ctx.font = o.font || '10px ui-monospace, monospace';
    const padX = o.padX === undefined ? 6 : o.padX;
    const w = ctx.measureText(text).width + padX * 2;
    const h = o.h || 16;
    const align = o.align || 'left';
    const bx = align === 'right' ? x - w : align === 'center' ? x - w / 2 : x;
    roundRect(ctx, bx, y - h / 2, w, h, o.radius === undefined ? 4 : o.radius);
    ctx.fillStyle = o.bg || alpha(token('--accent', '#38bdf8'), 0.92);
    ctx.fill();
    if (o.border) { ctx.strokeStyle = o.border; ctx.lineWidth = 1; ctx.stroke(); }
    ctx.fillStyle = o.color || '#04121f';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, bx + padX, y + 0.5);
    ctx.restore();
    return { x: bx, y: y - h / 2, w, h };
  }

  /* v1.51.0 · Rotulación de NIVELES, definida una sola vez.
   *
   * Las etiquetas de Call Wall, Put Wall, Zero Gamma y compañía se leían con
   * dificultad: 10 px de texto en una pastilla de 16 px de alto, sobre un fondo
   * con velas y campo de calor detrás. Son las referencias que se miran mil veces
   * en una sesión, así que el tamaño tiene que dar para leerlas de un vistazo.
   *
   * Y va aquí, no en cada panel: TRACE, la cinta y Net Drift dibujan los MISMOS
   * niveles, y con tres constantes repartidas volverían a divergir al primer
   * ajuste. El hueco crece con la altura para que dos muros cercanos no se pisen.
   */
  const LEVEL_FONT = '700 12px ui-monospace, monospace';
  const LEVEL_LABEL_H = 20;
  const LEVEL_LABEL_GAP = 22;
  const LEVEL_LINE_WIDTH = 1.6;

  /** Línea horizontal punteada de nivel estructural. */
  function levelLine(ctx, box, y, color, opts) {
    const o = opts || {};
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = o.width || 1;
    if (o.dash !== false) ctx.setLineDash(o.dash || [5, 4]);
    ctx.beginPath();
    ctx.moveTo(box.x, Math.round(y) + 0.5);
    ctx.lineTo(box.x + box.w, Math.round(y) + 0.5);
    ctx.stroke();
    ctx.restore();
  }

  /** Evita que las etiquetas de nivel se pisen entre sí. */
  function stackLabels(items, minGap) {
    const gap = minGap || 15;
    const sorted = items.slice().sort((a, b) => a.y - b.y);
    for (let i = 1; i < sorted.length; i++) {
      if (sorted[i].y - sorted[i - 1].y < gap) sorted[i].y = sorted[i - 1].y + gap;
    }
    return sorted;
  }

  /* ------------------------------------------------ niveles estructurales */

  /**
   * Una sola definición de los niveles del motor: color, nombre corto y
   * prioridad. TRACE y el panel de flujo la comparten para que un Call Wall se
   * vea igual en los dos sitios y no haya dos verdades sobre el mismo precio.
   */
  /* v1.53.1 · `short` — nombre corto para que TODA línea pueda llevar etiqueta.
   *
   * El renderer sólo rotulaba las primeras por prioridad, así que `target` y
   * `risk` —orden 7— se dibujaban SIN NOMBRE. Una línea sin nombre sobre un
   * gráfico de operativa es peor que no dibujarla: se ve, parece significar
   * algo, y no hay forma de saber qué.
   *
   * Y no se puede deducir por color: rojo es `put_wall` Y `risk`; verde es
   * `call_wall` Y `target`. El color agrupa `kind` distintos, así que deducir
   * de él acierta la mitad de las veces y no avisa cuando falla.
   *
   * Con nombre corto caben todas. `short` NO renombra el nivel: el nombre que
   * manda sigue siendo el que publica el motor; esto es sólo la abreviatura de
   * pantalla cuando el suyo no cabe.
   */
  const LEVELS = {
    flip:        { color: '--accent',   label: 'Zero Gamma',   short: '0Γ',   order: 1 },
    call_wall:   { color: '--pos',      label: 'Call Wall',    short: 'CW',   order: 2 },
    put_wall:    { color: '--neg',      label: 'Put Wall',     short: 'PW',   order: 2 },
    vol_trigger: { color: '--warn',     label: 'Vol Trigger',  short: 'VT',   order: 3 },
    hedge_wall:  { color: '--violet',   label: 'Hedge Wall',   short: 'HW',   order: 4 },
    gamma:       { color: '--accent-2', label: 'Γ Center',     short: 'ΓC',   order: 5 },
    delta:       { color: '--accent-2', label: 'Δ Center',     short: 'ΔC',   order: 5 },
    zone:        { color: '--text-dim', label: 'Zona',         short: 'ZONA', order: 6 },
    target:      { color: '--pos',      label: 'Objetivo',     short: 'OBJ',  order: 7 },
    risk:        { color: '--neg',      label: 'Invalidación', short: 'INVAL', order: 7 },
    // v1.54.0 · Las cuatro del PLAN del Scanner. Van con prioridad ALTA: son
    // las que el operador mira para decidir, así que nunca pueden quedarse sin
    // etiqueta por detrás de un centroide.
    scanner_entry:   { color: '--accent',   label: 'ENTRADA', short: 'ENT',  order: 0 },
    scanner_inval:   { color: '--neg',      label: 'INVAL',   short: 'INVAL', order: 0 },
    scanner_target1: { color: '--pos',      label: 'OBJ1',    short: 'OBJ1', order: 0 },
    scanner_target2: { color: '--pos',      label: 'OBJ2',    short: 'OBJ2', order: 0 },
    // v1.55.0 · Concentración de dark pool. Vive en su propia sección: comparte
    // el eje de precio con los muros de opciones y nada más, porque mide dinero
    // cruzado fuera de bolsa y no exposición.
    dark_pool_wall: { color: '--violet', label: 'Dark Pool', short: 'DP', order: 8 },
  };

  /** Niveles que el panel de flujo comparte con TRACE. */
  const FLOW_LEVEL_KINDS = ['flip', 'call_wall', 'put_wall', 'vol_trigger', 'hedge_wall', 'gamma', 'delta'];

  /**
   * v1.55.0 · Un `kind` que la tabla no conoce se DENUNCIA, no se disimula.
   *
   * Antes el respaldo devolvia `label: kind` y sin `short`, asi que la linea
   * salia con el nombre interno del motor —o sin nada— y parecia una linea mas.
   * Una linea anonima sobre un grafico de operativa es peor que no dibujarla:
   * se ve, parece significar algo, y no hay forma de saber que.
   *
   * `unidentified` permite al renderer marcarla a la vista, y el Auditor
   * publica el recuento en `level_identity_audit`.
   */
  function levelStyle(kind) {
    const k = String(kind || '');
    const hit = LEVELS[k];
    if (hit) return hit;
    return { color: '--text-dim', label: 'SIN IDENTIDAD' + (k ? ' · ' + k : ''),
             short: '?', order: 9, unidentified: true };
  }

  /* ------------------------------------------------- MARCA DE FLUJO (QFLOW)
   *
   * v1.56.0 · AUTORIDAD ÚNICA DE LA MARCA.
   *
   * Hasta aquí TRACE y FLUJO DE ÓRDENES tenían cada uno su COPIA de estas cinco
   * funciones. Eran equivalentes el día que se escribieron, y ese es justo el
   * problema: dos copias equivalentes se separan en cuanto alguien corrige una.
   * Una marca que significa COMPRA en una pantalla y otra cosa en la de al lado
   * es peor que no dibujarla.
   *
   * LA MARCA TIENE TRES PIEZAS Y CADA UNA DICE UNA COSA
   *
   *     círculo dorado   DÓNDE ocurrió, y su radio CUÁNTO pesa
   *     flecha           QUIÉN agredió   ▲ verde compra · ▼ rojo venta
   *     rombo neutro     el lado NO está demostrado
   *     cifra            la prima
   *
   * El dorado no es una dirección: marca que ahí hubo una concentración
   * importante. La dirección la dice la flecha, y sólo cuando se puede.
   */

  /** COMPRA (true), VENTA (false) o SIN LADO (null). Nunca se deduce del tipo
   *  de contrato ni del signo de la prima: sale del agresor y de nada más. */
  function flowSide(ev) {
    const agg = String((ev && ev.aggressor) || '').toUpperCase();
    if (agg === 'BUY') return true;
    if (agg === 'SELL') return false;
    return null;   // MIXED, UNKNOWN o ausente → sin lado demostrable
  }

  /** Cuánto pesa una marca, de 0 a 1, contra el pico del propio ciclo.
   *  Con una constante, un día tranquilo saldría todo diminuto. */
  function flowStrength(ev, peak) {
    const v = Math.abs(num(ev && ev.premium, NaN));
    if (!isNum(v) || !(peak > 0)) return 0.35;
    return clamp(Math.pow(v / peak, 0.5), 0.12, 1);
  }

  /** «$141.7M». Sin prima que enseñar, no se inventa una. */
  function flowAmountText(ev) {
    const v = Math.abs(num(ev && ev.premium, NaN));
    return isNum(v) ? money(v, 1) : '';
  }

  /** El círculo dorado. Devuelve su radio para colgar de él flecha y cifra. */
  function flowHalo(ctx, x, y, strength) {
    const gold = token('--gold', '#d9a441');
    const k = clamp(Number(strength) || 0, 0, 1);
    const r = 7 + 11 * k;                       // el radio dice cuánto
    ctx.save();
    // Halo exterior: separa la marca del fondo pase lo que pase debajo.
    const g = ctx.createRadialGradient(x, y, 0, x, y, r * 2.1);
    g.addColorStop(0, alpha(gold, 0.45));
    g.addColorStop(0.55, alpha(gold, 0.16));
    g.addColorStop(1, alpha(gold, 0));
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(x, y, r * 2.1, 0, Math.PI * 2); ctx.fill();
    // Dos anillos concéntricos.
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = alpha(gold, 0.95);
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.stroke();
    ctx.lineWidth = 1;
    ctx.strokeStyle = alpha(gold, 0.55);
    ctx.beginPath(); ctx.arc(x, y, r * 0.58, 0, Math.PI * 2); ctx.stroke();
    // Núcleo.
    ctx.fillStyle = alpha(gold, 0.92);
    ctx.beginPath(); ctx.arc(x, y, Math.max(2, r * 0.26), 0, Math.PI * 2); ctx.fill();
    ctx.restore();
    return r;
  }

  /** Flecha de sentido. Devuelve el color usado, para la cifra. */
  function flowArrow(ctx, x, y, up) {
    /* `up === null` significa que NO se conoce el lado agresor.
     *
     * Antes no existía ese caso: siempre se dibujaba verde o roja, así que un
     * evento sin lado salía pintado como compra o como venta según un valor por
     * defecto. Un lado desconocido se dibuja como ROMBO neutro: quien mire el
     * gráfico ve que hubo una concentración y que su dirección no está
     * confirmada, en vez de leer una dirección que nadie midió. */
    if (up === null || up === undefined) {
      const mute = token('--text-dim', '#8494ad');
      ctx.save();
      ctx.fillStyle = mute;
      ctx.beginPath();
      ctx.moveTo(x, y - 11); ctx.lineTo(x + 6, y - 5);
      ctx.lineTo(x, y + 1); ctx.lineTo(x - 6, y - 5);
      ctx.closePath(); ctx.fill();
      ctx.restore();
      return mute;
    }
    const col = up ? token('--pos', '#22c55e') : token('--neg', '#ef4444');
    const t = up ? -1 : 1;
    ctx.save();
    ctx.fillStyle = col;
    ctx.beginPath();
    // El VÉRTICE va en el extremo: hacia ARRIBA en compra, hacia ABAJO en
    // venta. Ponerlo del lado de la base dibuja la flecha invertida, que es
    // peor que no dibujarla: dice justo lo contrario.
    ctx.moveTo(x, y + t * 13);
    ctx.lineTo(x - 5.5, y + t * 6);
    ctx.lineTo(x + 5.5, y + t * 6);
    ctx.closePath(); ctx.fill();
    ctx.restore();
    return col;
  }

  /** Cifra sobre fondo propio: el texto suelto se pierde sobre el mapa. */
  function flowAmount(ctx, x, y, text, col) {
    ctx.save();
    ctx.font = '700 10px ui-monospace, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const w = ctx.measureText(text).width + 12;
    ctx.fillStyle = alpha(token('--panel-3', '#1b2436'), 0.94);
    roundRect(ctx, x - w / 2, y - 9, w, 18, 5); ctx.fill();
    ctx.strokeStyle = alpha(token('--gold', '#d9a441'), 0.8);
    ctx.lineWidth = 1;
    roundRect(ctx, x - w / 2, y - 9, w, 18, 5); ctx.stroke();
    ctx.fillStyle = col;
    ctx.fillText(text, x, y);
    ctx.restore();
  }

  /**
   * LA MARCA COMPLETA, en una sola llamada. Es lo que usan las TRES pantallas.
   *
   *   ctx, x, y   dónde va (ya resuelto por quien llama: cada panel tiene su eje)
   *   ev          el marcador de QFLOW
   *   peak        la mayor prima del ciclo, para escalar el radio
   *
   * Devuelve `{ r, up, col, dy }`: el radio del círculo, el lado, el color y el
   * desplazamiento vertical de la zona sensible del hover.
   */
  function flowMark(ctx, x, y, ev, peak, opts) {
    const o = opts || {};
    const up = flowSide(ev);
    const above = up !== false;      // sin lado, la marca va por encima
    const r = flowHalo(ctx, x, y, flowStrength(ev, peak));
    const col = flowArrow(ctx, x, above ? y - r : y + r, up);
    const label = o.label === undefined ? flowAmountText(ev) : o.label;
    if (label) flowAmount(ctx, x, above ? y - r - 22 : y + r + 22, label, col);
    return { r, up, above, col, dy: above ? -(r + 22) : (r + 22) };
  }

  /* --------------------------------------------------------------- export */

  const ITMQ = {
    Panel, Glide, GlideValue, TimeLink,
    scale, niceTicks, clamp, lerp, approach, isNum, num,
    compact, money, signedCompact, fmtMinutes, hhmm, parseTime,
    NO_DATA,
    token, alpha,
    LEVELS, FLOW_LEVEL_KINDS, levelStyle,
    // Marca de flujo: UNA implementación para TRACE, FLUJO y NET DRIFT.
    flowSide, flowStrength, flowAmountText, flowHalo, flowArrow, flowAmount, flowMark,
    LEVEL_FONT, LEVEL_LABEL_H, LEVEL_LABEL_GAP, LEVEL_LINE_WIDTH,
    roundRect, gridY, axisX, chip, levelLine, stackLabels,
    panels: PANELS,
    renderErrors: RENDER_ERRORS,
    healthy() { return RENDER_ERRORS.length === 0; },
    redrawAll() { for (const p of PANELS.values()) { p.resize(); p.invalidate(); } },
  };

  global.ITMQ = ITMQ;
})(window);
