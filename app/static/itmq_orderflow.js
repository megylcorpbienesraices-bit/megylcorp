/* ITM QUANT · Flujo de órdenes v1.41.0
 *
 * Cuatro carriles apilados sobre el mismo reloj:
 *
 *   1. PRECIO      línea/área con los prints grandes marcados sobre la curva
 *   2. AGRESOR     banda continua de presión compradora/vendedora
 *   3. FLUJO NETO  barras firmadas con escala simlog (24M y 2.8B en el mismo eje)
 *   4. TOTAL       barras de prima por intervalo, con etiqueta en los picos
 *
 * Los cuatro comparten zoom y cursor: mover uno mueve los otros tres.
 */
(function (global) {
  'use strict';

  const Q = global.ITMQ;
  if (!Q) { console.error('[ORDERFLOW] falta itmq_core.js'); return; }

  const PAD = { left: 26, right: 62, top: 10, bottom: 20 };

  const S = {
    candles: [],
    prints: [],
    levels: [],
    qflow: null,   // capa QFLOW: serie acumulada + nivel + eventos
    // NET DRIFT oficial de Quant Data. Estado propio y separado de `qflow`,
    // `buckets` y de cualquier magnitud de exposición: no comparte serie ni escala.
    drift: null,
    driftPick: NaN,   // instante seleccionado sobre la curva
    panel: 'tape',    // tape | drift

    buckets: [],          // [{t, net, total, buy, sell, unknown, underlyingNet, prints:[]}]
    symbol: '',
    spot: new Q.GlideValue(140),
    link: new Q.TimeLink(),
    panels: {},
    mode: 'area',          // area | line
    minPremium: 0,         // 0 = automático
    bucketMs: 60_000,
    hover: NaN,
    _netMax: new Q.GlideValue(300, 1),
    _totMax: new Q.GlideValue(300, 1),
  };

  /* ------------------------------------------------------ escala simlog */

  /** Comprime rangos de 5 órdenes de magnitud sin perder el signo ni el cero. */
  function symlog(v, linthresh) {
    const c = linthresh || 1e6;
    const x = Q.num(v, 0);
    const s = x < 0 ? -1 : 1;
    return s * Math.log10(1 + Math.abs(x) / c);
  }

  /* ---------------------------------------------------------- agregación */

  function rebuild() {
    const byBucket = new Map();
    const bw = S.bucketMs;
    const key = t => Math.floor(t / bw) * bw;

    for (const c of S.candles) {
      const t = Q.parseTime(c.t);
      if (!Q.isNum(t)) continue;
      const k = key(t);
      const b = byBucket.get(k) || { t: k, net: 0, total: 0, buy: 0, sell: 0, unknown: 0, underlyingNet: 0, prints: [], vol: 0 };
      // El volumen firmado del subyacente es contexto de tape, NO prima de opciones.
      // Mezclarlo con BUY/SELL premium dejaba el carril central en otra unidad y lo
      // podía aplanar aunque arriba hubiera $21K BUY y $90K SELL observados.
      b.underlyingNet += Q.num(c.sv, 0);
      b.vol += Q.num(c.v, 0);
      byBucket.set(k, b);
    }

    const floor = effectiveMinPremium();
    for (const p of S.prints) {
      const t = Q.parseTime(p.t);
      const prem = Math.abs(Q.num(p.premium, 0));
      if (!Q.isNum(t) || prem <= 0) continue;
      const k = key(t);
      const b = byBucket.get(k) || { t: k, net: 0, total: 0, buy: 0, sell: 0, unknown: 0, underlyingNet: 0, prints: [], vol: 0 };
      b.total += prem;
      const dir = Q.num(p.direction, 0);
      // La prima sin agresor identificado se contabiliza aparte. Sin esto, un carril
      // de flujo neto plano con millones negociados delante parecía una avería
      // cuando en realidad la cinta no traía cotización con la que clasificarla.
      if (dir > 0) b.buy += prem; else if (dir < 0) b.sell += prem; else b.unknown += prem;
      if (prem >= floor) b.prints.push(p);
      byBucket.set(k, b);
    }

    S.buckets = Array.from(byBucket.values()).sort((a, b) => a.t - b.t);

    let netMax = 0, totMax = 0;
    for (const b of S.buckets) {
      // Una sola unidad en el carril central: prima compradora − prima vendedora.
      // BUY queda arriba de cero; SELL debajo. El subyacente no contamina la escala.
      b.net = Q.num(b.buy, 0) - Q.num(b.sell, 0);
      netMax = Math.max(netMax, Math.abs(b.net));
      totMax = Math.max(totMax, b.total);
    }
    S._netMax.set(netMax > 0 ? netMax : 1);
    S._totMax.set(totMax > 0 ? totMax : 1);
  }

  /** Umbral de "print grande": explícito o percentil 92 de la sesión. */
  function effectiveMinPremium() {
    if (S.minPremium > 0) return S.minPremium;
    const vals = S.prints.map(p => Math.abs(Q.num(p.premium, 0))).filter(v => v > 0).sort((a, b) => a - b);
    if (!vals.length) return Infinity;
    return vals[Math.min(vals.length - 1, Math.floor(vals.length * 0.92))];
  }

  function window_() {
    let t0 = S.link.t0, t1 = S.link.t1;
    if (Q.isNum(t0) && Q.isNum(t1)) return { t0, t1 };
    const ts = S.candles.map(c => Q.parseTime(c.t)).filter(Q.isNum);
    if (!ts.length) return null;
    const a = Math.min.apply(null, ts), b = Math.max.apply(null, ts);
    const pad = Math.max((b - a) * 0.05, 120_000);
    S.link.setWindow(a, b + pad, { silent: true });
    return { t0: a, t1: b + pad };
  }

  function plotBox(env) {
    return { x: PAD.left, y: PAD.top, w: env.w - PAD.left - PAD.right, h: env.h - PAD.top - PAD.bottom };
  }

  /* ------------------------------------------------------ carril precio */

  function drawPrice(ctx, env) {
    const box = plotBox(env);
    if (box.w <= 8 || box.h <= 8) return false;
    const win = window_();
    if (!win) { empty(ctx, env, 'SIN PRECIO OBSERVADO'); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    const pts = [];
    for (const c of S.candles) {
      const t = Q.parseTime(c.t), v = Q.num(c.c, NaN);
      if (Q.isNum(t) && Q.isNum(v) && t >= win.t0 - S.bucketMs && t <= win.t1) pts.push({ t, v });
    }
    if (!pts.length) { empty(ctx, env, 'SIN PRECIO EN LA VENTANA'); return false; }

    let lo = Infinity, hi = -Infinity;
    for (const p of pts) { lo = Math.min(lo, p.v); hi = Math.max(hi, p.v); }
    for (const lv of S.levels) {
      const p = Q.num(lv.price, NaN);
      // Un nivel muy lejano no debe aplastar la curva de precio.
      if (Q.isNum(p) && p > lo - (hi - lo) * 1.2 && p < hi + (hi - lo) * 1.2) { lo = Math.min(lo, p); hi = Math.max(hi, p); }
    }
    const pad = Math.max((hi - lo) * 0.12, 0.02);
    const sy = Q.scale(lo - pad, hi + pad, box.y + box.h, box.y);

    Q.gridY(ctx, box, sy, Q.niceTicks(lo - pad, hi + pad, 6), t => t.toFixed(t >= 1000 ? 1 : 2), { labelSide: 'right' });

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();

    // curva
    const priceC = Q.token('--price', '#7aa2f7');
    ctx.beginPath();
    pts.forEach((p, i) => { const x = sx(p.t), y = sy(p.v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    if (S.mode === 'area') {
      ctx.save();
      ctx.lineTo(sx(pts[pts.length - 1].t), box.y + box.h);
      ctx.lineTo(sx(pts[0].t), box.y + box.h);
      ctx.closePath();
      const g = ctx.createLinearGradient(0, box.y, 0, box.y + box.h);
      g.addColorStop(0, Q.alpha(priceC, 0.30));
      g.addColorStop(1, Q.alpha(priceC, 0.02));
      ctx.fillStyle = g; ctx.fill();
      ctx.restore();
      ctx.beginPath();
      pts.forEach((p, i) => { const x = sx(p.t), y = sy(p.v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    }
    ctx.strokeStyle = priceC; ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.stroke();

    // Prints grandes: se agregan por bucket temporal y se anclan a la curva REAL
    // del subyacente. Dibujar cada contrato en su propio option-price Y producía una
    // nube de puntos dorados sin relación visual con la línea del DIA.
    const floor = effectiveMinPremium();
    const markBuckets = new Map();
    for (const p of S.prints) {
      const t = Q.parseTime(p.t), prem = Math.abs(Q.num(p.premium, 0));
      if (!Q.isNum(t) || prem < floor || t < win.t0 || t > win.t1) continue;
      const k = Math.floor(t / S.bucketMs) * S.bucketMs;
      const m = markBuckets.get(k) || { t: k, prem: 0, signed: 0, count: 0, weightedT: 0 };
      const dir = Q.num(p.direction, 0);
      m.prem += prem;
      m.signed += dir * prem;
      m.count += 1;
      m.weightedT += t * prem;
      markBuckets.set(k, m);
    }
    function nearestPrice(t) {
      let best = pts[0];
      for (const p of pts) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
      return best;
    }
    const marks = Array.from(markBuckets.values()).map(m => {
      const t = m.prem > 0 ? m.weightedT / m.prem : m.t;
      const near = nearestPrice(t);
      return { x: sx(t), y: sy(near.v), prem: m.prem,
        dir: m.signed > 0 ? 1 : m.signed < 0 ? -1 : 0, count: m.count };
    });
    const gold = Q.token('--gold', '#d9a441');
    for (const m of marks) {
      ctx.beginPath(); ctx.arc(m.x, m.y, 8, 0, Math.PI * 2);
      ctx.fillStyle = Q.alpha(gold, 0.13); ctx.fill();
      ctx.beginPath(); ctx.arc(m.x, m.y, 5, 0, Math.PI * 2);
      ctx.strokeStyle = Q.alpha(gold, 0.58); ctx.lineWidth = 1; ctx.stroke();
      ctx.beginPath(); ctx.arc(m.x, m.y, 2.6, 0, Math.PI * 2);
      ctx.fillStyle = gold; ctx.fill();
    }
    ctx.restore();

    // Sólo los cinco buckets de mayor prima llevan texto. El resto conserva el
    // marcador ordenado sobre precio, sin tapar la lectura.
    const posC = Q.token('--pos', '#22c55e'), negC = Q.token('--neg', '#ef4444');
    const labeled = marks.slice().sort((a, b) => b.prem - a.prem).slice(0, 5);
    const placed = [];
    ctx.save();
    ctx.font = '10px ui-monospace, monospace';
    for (const m of labeled) {
      let ly = m.y - 20;
      // desplaza la etiqueta si chocaría con otra ya colocada
      for (let guard = 0; guard < 8; guard++) {
        const hit = placed.some(p => Math.abs(p.x - m.x) < 62 && Math.abs(p.y - ly) < 14);
        if (!hit) break;
        ly -= 15;
      }
      ly = Q.clamp(ly, box.y + 8, box.y + box.h - 8);
      placed.push({ x: m.x, y: ly });
      const up = m.dir > 0;
      const col = m.dir === 0 ? gold : (up ? posC : negC);
      ctx.fillStyle = col;
      ctx.beginPath();
      if (up) { ctx.moveTo(m.x - 9, ly + 3); ctx.lineTo(m.x - 4, ly - 4); ctx.lineTo(m.x + 1, ly + 3); }
      else { ctx.moveTo(m.x - 9, ly - 3); ctx.lineTo(m.x - 4, ly + 4); ctx.lineTo(m.x + 1, ly - 3); }
      ctx.closePath(); ctx.fill();
      ctx.fillStyle = Q.token('--text', '#e6edf7');
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(`${Q.money(m.prem, 1)}${m.count > 1 ? ' · ×' + m.count : ''}`, m.x + 4, ly);
    }
    ctx.restore();

    // ── NIVEL QFLOW ──────────────────────────────────────────────────────────
    // Precio donde se concentra la prima negociada, ponderado por |prima| de cada
    // intervalo y con decaimiento temporal. NO es strike dominante, ni Net Drift,
    // ni Gamma Center, ni Zero Gamma: es dónde estaba el subyacente mientras se
    // pagaba el dinero. Lo calcula ITM QUANT; Quant Data no publica este nivel.
    const qflowLevel = (S.qflow && S.qflow.ready && S.qflow.level) ? S.qflow.level : null;

    // niveles estructurales, con el mismo color y nombre que en TRACE
    const drawn = [];
    if (qflowLevel && Q.isNum(Q.num(qflowLevel.price, NaN))) {
      const qp = Q.num(qflowLevel.price);
      const qy = sy(qp);
      if (qy >= box.y && qy <= box.y + box.h) {
        const qcol = Q.token('--qflow', '#f5c663');
        drawn.push({ y: qy, price: qp, name: 'QFLOW', color: qcol });
        Q.levelLine(ctx, box, qy, Q.alpha(qcol, 0.85));
      }
    }
    for (const lv of S.levels.slice(0, 7)) {
      const p = Q.num(lv.price, NaN);
      if (!Q.isNum(p)) continue;
      const y = sy(p);
      if (y < box.y || y > box.y + box.h) continue;
      const st = Q.levelStyle(lv.kind);
      drawn.push({ y, price: p, name: lv.name || st.label, color: Q.token(st.color, '#8494ad') });
      Q.levelLine(ctx, box, y, Q.alpha(Q.token(st.color, '#8494ad'), 0.7));
    }
    // Las etiquetas se apilan para que dos niveles cercanos no se tapen.
    for (const it of Q.stackLabels(drawn, 15)) {
      Q.chip(ctx, box.x + 6, Q.clamp(it.y, box.y + 9, box.y + box.h - 9),
        `${it.name} ${it.price.toFixed(it.price >= 1000 ? 0 : 2)}`,
        { bg: Q.alpha(it.color, 0.88), color: '#06101c' });
    }

    // ── eventos de concentración sobre el precio ─────────────────────────────
    // Marca los intervalos con prima excepcional para la sesión, en el precio al
    // que ocurrieron. El mismo evento se refleja en el carril TOTAL, de modo que
    // la cadena queda cerrada: evento -> QFLOW -> precio -> nivel QFLOW.
    const qEvents = (S.qflow && S.qflow.ready && Array.isArray(S.qflow.events)) ? S.qflow.events : [];
    if (qEvents.length) {
      ctx.save();
      ctx.font = '700 9px ui-monospace, monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      for (const ev of qEvents.slice(0, 8)) {
        const t = Q.parseTime(ev.t), pxv = Q.num(ev.price, NaN);
        if (!Q.isNum(t) || !Q.isNum(pxv)) continue;
        const x = sx(t), y = sy(pxv);
        if (x < box.x || x > box.x + box.w || y < box.y || y > box.y + box.h) continue;
        const up = String(ev.side) === 'CALL';
        const col = Q.token(up ? '--pos' : '--neg', up ? '#22c55e' : '#ef4444');
        ctx.fillStyle = col;
        ctx.beginPath();
        if (up) { ctx.moveTo(x, y - 12); ctx.lineTo(x - 4, y - 5); ctx.lineTo(x + 4, y - 5); }
        else { ctx.moveTo(x, y + 12); ctx.lineTo(x - 4, y + 5); ctx.lineTo(x + 4, y + 5); }
        ctx.closePath(); ctx.fill();
        // v1.43.0 · La marca lleva la flecha Y la magnitud: «▲ $4.2M». Un triángulo
        // suelto dice que pasó algo; la etiqueta dice cuánto, que es lo que permite
        // comparar dos concentraciones de un vistazo sin abrir el panel.
        ctx.fillText(markerLabel(ev), x, up ? y - 14 : y + 24);
      }
      ctx.restore();
    }

    // último precio en el carril derecho
    const last = pts[pts.length - 1];
    S.spot.set(last.v);
    const sv = S.spot.get();
    const yS = sy(sv);
    Q.levelLine(ctx, box, yS, Q.alpha(priceC, 0.5), { dash: [3, 3] });
    Q.chip(ctx, box.x + box.w + 4, yS, sv.toFixed(sv >= 1000 ? 1 : 2), { align: 'left', bg: priceC, color: '#06101c' });

    // tooltip del cursor
    if (S.panels.price && S.panels.price.pointer.inside) {
      const px = S.panels.price.pointer.x;
      if (px >= box.x && px <= box.x + box.w) {
        const t = sx.invert(px);
        let near = pts[0];
        for (const p of pts) if (Math.abs(p.t - t) < Math.abs(near.t - t)) near = p;
        crosshairX(ctx, box, px);
        Q.chip(ctx, Q.clamp(px + 8, box.x, box.x + box.w - 150), box.y + 12,
          `${Q.hhmm(near.t)} · ${S.symbol} ${near.v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
          { bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 20, padX: 8 });
      }
    }

    Q.axisX(ctx, box, sx, win.t0, win.t1, { grid: false });
    return S.spot.step(env.dt);
  }

  /* ---------------------------------------------------- carril agresor */

  function drawAggressor(ctx, env) {
    const box = plotBox(env);
    box.y = 2; box.h = env.h - 4;
    const win = window_();
    if (!win || !S.buckets.length) return false;
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    const buy = Q.token('--aggr-buy', '#2dd4bf');
    const sell = Q.token('--aggr-sell', '#a78bfa');
    let peak = 0;
    for (const b of S.buckets) peak = Math.max(peak, b.buy + b.sell);
    if (peak <= 0) peak = 1;

    ctx.save();
    ctx.fillStyle = Q.alpha(Q.token('--panel-2', '#141b28'), 1);
    ctx.fillRect(box.x, box.y, box.w, box.h);
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      const x0 = sx(b.t), x1 = sx(b.t + S.bucketMs);
      const tot = b.buy + b.sell;
      if (tot <= 0) continue;
      const net = (b.buy - b.sell) / tot;          // −1 vendedor · +1 comprador
      const intensity = Q.clamp(Math.pow(tot / peak, 0.5), 0.08, 1);
      ctx.fillStyle = Q.alpha(net >= 0 ? buy : sell, intensity);
      ctx.fillRect(x0, box.y, Math.max(1, x1 - x0), box.h);
    }
    ctx.restore();

    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.fillText('AGRESOR', box.x + 4, box.y + 3);
    ctx.restore();

    if (S.panels.aggr && S.panels.aggr.pointer.inside) crosshairX(ctx, box, S.panels.aggr.pointer.x);
    return false;
  }

  /* -------------------------------------------------- carril flujo neto */

  function drawNetFlow(ctx, env) {
    const box = plotBox(env);
    box.bottom = 0;
    const win = window_();
    if (!win || !S.buckets.length) { empty(ctx, env, 'SIN FLUJO'); return false; }
    // Toda la prima sin clasificar: el neto no puede dibujarse y hay que decir por qué,
    // no dejar un eje vacío que se lee como avería.
    let _cls = 0, _unk = 0;
    for (const b of S.buckets) { _cls += b.buy + b.sell; _unk += (b.unknown || 0); }
    if (_cls <= 0) {
      empty(ctx, env, _unk > 0
        ? `PRIMA SIN AGRESOR IDENTIFICADO · ${Q.money(_unk, 1)} sin clasificar`
        : 'SIN FLUJO DIRECCIONAL');
      return false;
    }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    // GlideValue suaviza el eje, pero en el primer frame de un salto grande puede
    // quedarse muy por debajo del dato real. Entonces la barra queda fuera del
    // clip y el carril parece vacío aunque el resumen superior tenga BUY/SELL.
    // El eje nunca puede ser menor que el pico actualmente visible.
    let actualPeak = 0;
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      actualPeak = Math.max(actualPeak, Math.abs(Number(b.net) || 0));
    }
    const max = Math.max(S._netMax.get(), actualPeak, 1);
    const lin = Math.max(max / 400, 1000);
    const top = symlog(max, lin);
    const sy = Q.scale(-top, top, box.y + box.h, box.y);

    // Eje simlog: una década por etiqueta para que 24M y 2.8B convivan.
    ctx.save();
    ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.5);
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;
    const decades = [];
    for (let d = 0; d <= 12; d++) {
      const v = Math.pow(10, d) * lin;
      if (v > max * 1.05) break;
      if (d % 2 === 0 || d === 0) decades.push(v);
    }
    for (const v of decades.concat([0])) {
      for (const s of (v === 0 ? [0] : [1, -1])) {
        const y = Math.round(sy(symlog(v * s, lin))) + 0.5;
        if (y < box.y || y > box.y + box.h) continue;
        ctx.beginPath(); ctx.moveTo(box.x, y); ctx.lineTo(box.x + box.w, y); ctx.stroke();
        ctx.fillText(v === 0 ? '0' : (s > 0 ? '' : '-') + Q.compact(v, 1), box.x + box.w + 6, y);
      }
    }
    ctx.restore();

    const posC = Q.token('--pos', '#22c55e'), negC = Q.token('--neg', '#ef4444');
    const y0 = sy(0);
    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      if (!b.net) continue;
      const x0 = sx(b.t), x1 = sx(b.t + S.bucketMs);
      const bw = Math.max(1, (x1 - x0) * 0.7);
      const y = sy(symlog(b.net, lin));
      ctx.fillStyle = Q.alpha(b.net >= 0 ? posC : negC, 0.9);
      ctx.fillRect(x0 + (x1 - x0 - bw) / 2, Math.min(y, y0), bw, Math.max(1, Math.abs(y - y0)));
    }
    ctx.restore();

    // ── QFLOW · prima neta ACUMULADA de la sesión ────────────────────────────
    // Las barras responden "¿qué pasó en este minuto?"; la línea responde "¿hacia
    // dónde se ha inclinado la sesión entera?". Son preguntas distintas y por eso
    // conviven: la línea no sustituye a las barras ni comparte su escala.
    const qf = S.qflow;
    const qSeries = (qf && qf.ready && Array.isArray(qf.series)) ? qf.series : null;
    if (qSeries && qSeries.length > 1) {
      let qMax = 0;
      for (const pt of qSeries) qMax = Math.max(qMax, Math.abs(Q.num(pt.cumulative, 0)));
      if (qMax > 0) {
        // Escala propia, centrada en cero, independiente del eje simlog de las barras.
        const qy = Q.scale(-qMax * 1.08, qMax * 1.08, box.y + box.h, box.y);
        ctx.save();
        ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();
        ctx.strokeStyle = Q.token('--qflow', '#f5c663');
        ctx.lineWidth = 1.6; ctx.globalAlpha = 0.95;
        ctx.beginPath();
        let started = false;
        for (const pt of qSeries) {
          const t = Q.parseTime(pt.t); if (!Q.isNum(t)) continue;
          const x = sx(t), y = qy(Q.num(pt.cumulative, 0));
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        }
        if (started) ctx.stroke();
        ctx.restore();

        ctx.save();
        ctx.font = '700 9px ui-monospace, monospace';
        ctx.fillStyle = Q.token('--qflow', '#f5c663');
        ctx.textAlign = 'right'; ctx.textBaseline = 'top';
        ctx.fillText(`QFLOW ${Q.money(Q.num(qf.net_premium, 0), 1)}`, box.x + box.w - 6, box.y + 3);
        ctx.restore();
      }
    }

    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.fillText('FLUJO NETO · PRIMA DIRECCIONAL', box.x + 4, box.y + 3);
    // Un estado degradado se declara aquí, no se disfraza de serie plana.
    if (qf && !qf.ready && qf.state && qf.state !== 'DATA_OK') {
      ctx.fillStyle = Q.alpha(Q.token('--text-dim', '#8494ad'), 0.85);
      ctx.textAlign = 'right';
      ctx.fillText(`QFLOW · ${String(qf.state)}`, box.x + box.w - 6, box.y + 3);
    }
    ctx.restore();

    if (S.panels.net && S.panels.net.pointer.inside) crosshairX(ctx, box, S.panels.net.pointer.x);
    return S._netMax.step(env.dt);
  }

  /* ------------------------------------------------------- carril total */

  function drawTotal(ctx, env) {
    const box = plotBox(env);
    const win = window_();
    if (!win || !S.buckets.length) { empty(ctx, env, 'SIN PRIMA OBSERVADA'); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);
    let actualPeak = 0;
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      actualPeak = Math.max(actualPeak, Number(b.total) || 0);
    }
    const max = Math.max(S._totMax.get(), actualPeak, 1);
    const sy = Q.scale(0, max * 1.25, box.y + box.h, box.y);

    ctx.save();
    ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.5);
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    for (const t of Q.niceTicks(0, max * 1.25, 3)) {
      if (t <= 0) continue;
      const y = Math.round(sy(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(box.x, y); ctx.lineTo(box.x + box.w, y); ctx.stroke();
      ctx.fillText(Q.compact(t, 1), box.x + box.w + 6, y);
    }
    ctx.restore();

    const gold = Q.token('--gold', '#d9a441');
    const dim = Q.token('--text-dim', '#8494ad');
    const floor = effectiveMinPremium();
    const y0 = box.y + box.h;

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y - 14, box.w, box.h + 14); ctx.clip();
    const tall = [];
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      if (b.total <= 0) continue;
      const x0 = sx(b.t), x1 = sx(b.t + S.bucketMs);
      const bw = Math.max(1, (x1 - x0) * 0.62);
      const bx = x0 + (x1 - x0 - bw) / 2;
      const y = sy(b.total);
      const big = b.prints.length > 0 || b.total >= floor;
      ctx.fillStyle = big ? gold : Q.alpha(dim, 0.35);
      ctx.fillRect(bx, y, bw, Math.max(1, y0 - y));
      if (big) tall.push({ x: bx + bw / 2, y, total: b.total });
    }
    // sólo los picos llevan cifra: etiquetar todo haría ilegible el carril
    tall.sort((a, b) => b.total - a.total);
    ctx.font = '10px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text', '#e6edf7');
    ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
    const used = [];
    for (const t of tall.slice(0, 6)) {
      if (used.some(u => Math.abs(u - t.x) < 54)) continue;
      used.push(t.x);
      ctx.fillText(Q.money(t.total, 1), t.x, Math.max(box.y + 10, t.y - 3));
    }
    ctx.restore();

    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.fillText('TOTAL', box.x + 4, box.y - 8);
    
    // Los mismos eventos de concentración que se marcan sobre el precio, aquí en
    // su magnitud. Ver el pico y saber a qué precio ocurrió es lo que une los dos
    // carriles; marcarlo sólo en uno obliga a cruzarlos a ojo.
    const qEv = (S.qflow && S.qflow.ready && Array.isArray(S.qflow.events)) ? S.qflow.events : [];
    if (qEv.length) {
      ctx.save();
      ctx.strokeStyle = Q.alpha(Q.token('--qflow', '#f5c663'), 0.9);
      ctx.lineWidth = 1;
      for (const ev of qEv.slice(0, 8)) {
        const t = Q.parseTime(ev.t); if (!Q.isNum(t)) continue;
        const x = sx(t);
        if (x < box.x || x > box.x + box.w) continue;
        ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + 6); ctx.stroke();
        const att = attributionFor(ev.t);
        if (att) {
          ctx.save();
          ctx.font = '9px ui-monospace, monospace';
          ctx.fillStyle = Q.alpha(Q.token('--text-dim', '#8494ad'), 0.9);
          ctx.textAlign = 'center'; ctx.textBaseline = 'top';
          ctx.fillText(attributionText(att), x, box.y + 8);
          ctx.restore();
        }
      }
      ctx.restore();
    }
    ctx.restore();

    Q.axisX(ctx, box, sx, win.t0, win.t1, { grid: false });
    if (S.panels.total && S.panels.total.pointer.inside) crosshairX(ctx, box, S.panels.total.pointer.x);
    return S._totMax.step(env.dt);
  }


  /** «▲ $4.2M» / «▼ $1.8M» · la misma etiqueta que dibuja TRACE sobre el precio. */
  function markerLabel(ev) {
    if (!ev) return '';
    if (ev.label) return String(ev.label);
    const up = String(ev.side) === 'CALL';
    const arrow = up ? '▲' : String(ev.side) === 'PUT' ? '▼' : '◆';
    return arrow + ' ' + Q.money(Q.num(ev.premium, 0), 1);
  }

  /** Qué operaciones produjeron una concentración, en una línea legible.
   *
   * La atribución viene de `order-flow` del mismo proveedor que publicó la serie.
   * Si no hay cinta, se dice: una concentración sin atribuir es mejor que una
   * atribuida a operaciones que nadie vio.
   */
  function attributionFor(t) {
    const att = (S.qflow && S.qflow.attribution) || null;
    if (!att || !att.ready || !Array.isArray(att.events)) return null;
    const target = Q.parseTime(t);
    if (!Q.isNum(target)) return null;
    for (const e of att.events) {
      if (Q.parseTime(e.t) === target && e.matched) return e;
    }
    return null;
  }

  function attributionText(e) {
    if (!e) return '';
    const parts = [];
    if (e.calls) parts.push(e.calls + ' call');
    if (e.puts) parts.push(e.puts + ' put');
    if (e.buys) parts.push(e.buys + ' compra');
    if (e.sells) parts.push(e.sells + ' venta');
    const tags = Object.keys(e.executions || {});
    if (tags.length) parts.push(tags.join('·'));
    if (e.dominant_strike && Q.isNum(Q.num(e.dominant_strike.strike, NaN))) {
      parts.push('strike ' + Q.num(e.dominant_strike.strike).toFixed(0));
    }
    return parts.join(' · ');
  }

  /* ------------------------------------------------------------- común */

  function crosshairX(ctx, box, x) {
    if (!Q.isNum(x) || x < box.x || x > box.x + box.w) return;
    ctx.save();
    ctx.strokeStyle = Q.alpha(Q.token('--text-dim', '#8494ad'), 0.55);
    ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, box.y); ctx.lineTo(Math.round(x) + 0.5, box.y + box.h); ctx.stroke();
    ctx.restore();
  }

  function empty(ctx, env, msg) {
    ctx.save();
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.font = '11px ui-monospace, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(msg, env.w / 2, env.h / 2);
    ctx.restore();
  }

  function invalidateAll() { for (const k in S.panels) S.panels[k].invalidate(); }

  let dragX = NaN;
  function sharedWheel(ev, panel) {
    ev.preventDefault();
    const box = plotBox({ w: panel.w, h: panel.h });
    const win = window_(); if (!win) return;
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);
    S.link.zoomAt(ev.deltaY > 0 ? 1.12 : 0.89, sx.invert(panel.pointer.x));
  }
  function sharedDown(p) { dragX = p.x; }
  function sharedMove(p, ev, panel) {
    if (p.down && Q.isNum(dragX)) {
      const box = plotBox({ w: panel.w, h: panel.h });
      S.link.panBy(-(p.x - dragX) * (S.link.span() / Math.max(1, box.w)));
      dragX = p.x;
    }
    invalidateAll();
  }
  function sharedUp() { dragX = NaN; }

  const sharedOpts = id => ({
    id, onWheel: sharedWheel, onDown: sharedDown, onPointer: sharedMove, onUp: sharedUp,
    onPointerLeave: invalidateAll,
  });

  /**
   * Carril de VOLUMEN del subyacente por bucket, con el signo de la cinta.
   * Los buckets ya acumulaban `vol` (volumen SIP) y `underlyingNet` (volumen
   * firmado) y nadie los dibujaba: el flujo de opciones se leía sin saber si el
   * subyacente lo acompañaba o lo contradecía, que es justo la confirmación que
   * un operador busca al mirar prima y cinta a la vez.
   */
  function drawVolume(ctx, env) {
    const box = plotBox(env);
    const win = window_();
    if (!win || !S.buckets.length) { empty(ctx, env, 'SIN VOLUMEN OBSERVADO'); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);
    let peak = 0;
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      peak = Math.max(peak, Math.abs(Q.num(b.vol, 0)));
    }
    if (!(peak > 0)) { empty(ctx, env, 'SIN VOLUMEN OBSERVADO'); return false; }
    const sy = Q.scale(0, peak * 1.2, box.y + box.h, box.y);

    ctx.save();
    ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.5);
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    for (const t of Q.niceTicks(0, peak * 1.2, 2)) {
      if (t <= 0) continue;
      const y = Math.round(sy(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(box.x, y); ctx.lineTo(box.x + box.w, y); ctx.stroke();
      ctx.fillText(Q.compact(t, 1), box.x + box.w + 6, y);
    }

    const bw = Math.max(1, Math.min(9, (box.w / Math.max(1, (win.t1 - win.t0) / S.bucketMs)) * 0.62));
    const pos = Q.token('--pos', '#22c55e'), neg = Q.token('--neg', '#ef4444'), flat = Q.token('--text-dim', '#8494ad');
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      const v = Math.abs(Q.num(b.vol, 0));
      if (!(v > 0)) continue;
      const signed = Q.num(b.underlyingNet, 0);
      const x = sx(b.t), y = sy(v), h = Math.max(1, (box.y + box.h) - y);
      ctx.fillStyle = Q.alpha(signed > 0 ? pos : signed < 0 ? neg : flat, 0.62);
      ctx.fillRect(Math.round(x - bw / 2), Math.round(y), Math.max(1, Math.round(bw)), Math.round(h));
    }

    ctx.globalAlpha = 0.72;
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.fillText('VOLUMEN SUBYACENTE · SIP · COLOR = SIGNO DE CINTA', box.x + 6, box.y + 4);
    ctx.restore();
    return true;
  }

  /* ═══════════════════════════════════════════════════ NET DRIFT OFICIAL ═══
   *
   * FUENTE ÚNICA: POST /v1/options/tool/net-drift de Quant Data.
   *
   * Aquí NO se reconstruye Net Drift con GEX, DEX, Net Flow, QFLOW ni ninguna
   * fórmula propia. El backend (`app/core/net_drift.py`) ordena los buckets del
   * proveedor y acumula; este renderizador sólo dibuja lo que recibe. Si el
   * proveedor no entrega, se escribe SIN DATOS: una curva plana en cero sería
   * indistinguible de una sesión realmente equilibrada.
   *
   * Cuatro elementos, tal y como los publica el proveedor:
   *   1. línea CALL  = Net Call Premium ACUMULADO
   *   2. línea PUT   = Net Put Premium ACUMULADO
   *   3. precio del subyacente sobre el MISMO eje temporal (eje derecho)
   *   4. subgráfico de volumen neto CALL/PUT
   *
   * El eje temporal es el `TimeLink` compartido con la cinta, así que mover el
   * zoom en cualquier carril mueve también Net Drift: es el mismo reloj, no dos
   * relojes que casualmente coinciden.
   */

  // Las etiquetas del eje izquierdo son primas ($12.3M), no precios de dos
  // dígitos: con el margen de 26px de la cinta se salían del lienzo.
  const DRIFT_PAD = { left: 58, right: 62, top: 10, bottom: 20 };
  function driftBox(env) {
    return { x: DRIFT_PAD.left, y: DRIFT_PAD.top,
      w: env.w - DRIFT_PAD.left - DRIFT_PAD.right,
      h: env.h - DRIFT_PAD.top - DRIFT_PAD.bottom };
  }

  /** Buckets de Net Drift visibles, en orden. */
  function driftRows() {
    const d = S.drift;
    if (!d || !d.ready || !Array.isArray(d.series)) return [];
    return d.series;
  }

  function driftT(p) { return Q.isNum(Q.num(p.timestamp_ms, NaN)) ? Q.num(p.timestamp_ms) : Q.parseTime(p.t); }

  /** Estado legible cuando no hay curva. Nunca un cero disfrazado de dato. */
  function driftEmptyMessage() {
    const d = S.drift;
    if (!d) return 'SIN DATOS · NET DRIFT NO PUBLICADO';
    if (d.ready) return 'SIN DATOS EN LA VENTANA';
    const st = String(d.state || '');
    const detail = String(d.detail || '');
    return `SIN DATOS · ${st}${detail ? ' · ' + detail.toUpperCase() : ''}`;
  }

  function drawDrift(ctx, env) {
    const box = driftBox(env);
    if (box.w <= 8 || box.h <= 8) return false;
    const rows = driftRows();
    if (!rows.length) { empty(ctx, env, driftEmptyMessage()); return false; }
    const win = window_();
    if (!win) { empty(ctx, env, driftEmptyMessage()); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    const vis = [];
    for (const p of rows) {
      const t = driftT(p);
      if (!Q.isNum(t) || t < win.t0 - S.bucketMs || t > win.t1) continue;
      vis.push({ t, call: Q.num(p.cum_call, 0), put: Q.num(p.cum_put, 0),
        net: Q.num(p.cum_net, 0), price: Q.num(p.price, NaN), open: !!p.open });
    }
    if (!vis.length) { empty(ctx, env, 'SIN DATOS EN LA VENTANA'); return false; }

    // Eje izquierdo: prima acumulada. Se incluye el cero SIEMPRE porque el signo
    // es la lectura: una curva que no muestra su cruce por cero no dice nada.
    let lo = 0, hi = 0;
    for (const v of vis) {
      lo = Math.min(lo, v.call, v.put, v.net);
      hi = Math.max(hi, v.call, v.put, v.net);
    }
    const span = Math.max(hi - lo, 1);
    const sy = Q.scale(lo - span * 0.08, hi + span * 0.08, box.y + box.h, box.y);
    Q.gridY(ctx, box, sy, Q.niceTicks(lo - span * 0.08, hi + span * 0.08, 5),
      t => Q.money(t, 1), { labelSide: 'left' });

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();

    // línea de cero
    const zy = Math.round(sy(0)) + 0.5;
    ctx.beginPath(); ctx.moveTo(box.x, zy); ctx.lineTo(box.x + box.w, zy);
    ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.9); ctx.lineWidth = 1; ctx.stroke();

    const posC = Q.token('--pos', '#22c55e'), negC = Q.token('--neg', '#ef4444');
    const line = (key, color, width, dash) => {
      ctx.save();
      ctx.setLineDash(dash || []);
      ctx.beginPath();
      vis.forEach((v, i) => { const x = sx(v.t), y = sy(v[key]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = 'round'; ctx.stroke();
      ctx.restore();
    };
    line('net', Q.alpha(Q.token('--text-dim', '#8494ad'), 0.75), 1, [4, 3]);
    line('call', posC, 1.8);
    line('put', negC, 1.8);

    // El último bucket sigue ABIERTO: se marca hueco para que no se lea como un
    // valor consolidado. Es el único punto de la curva que aún puede cambiar.
    const last = vis[vis.length - 1];
    if (last && last.open) {
      for (const [key, col] of [['call', posC], ['put', negC]]) {
        ctx.beginPath(); ctx.arc(sx(last.t), sy(last[key]), 3.2, 0, Math.PI * 2);
        ctx.fillStyle = Q.token('--panel', '#0d131c'); ctx.fill();
        ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.stroke();
      }
    }

    // Eje derecho: precio del subyacente, en el MISMO eje temporal.
    const px = vis.filter(v => Q.isNum(v.price));
    if (px.length > 1) {
      let plo = Infinity, phi = -Infinity;
      for (const v of px) { plo = Math.min(plo, v.price); phi = Math.max(phi, v.price); }
      const ppad = Math.max((phi - plo) * 0.18, 0.02);
      const psy = Q.scale(plo - ppad, phi + ppad, box.y + box.h, box.y);
      const priceC = Q.token('--price', '#7aa2f7');
      ctx.beginPath();
      px.forEach((v, i) => { const x = sx(v.t), y = psy(v.price); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = Q.alpha(priceC, 0.85); ctx.lineWidth = 1.2; ctx.stroke();
      ctx.restore();
      // Sólo etiquetas: una segunda rejilla desalineada con la de prima haría
      // ilegibles las dos. La rejilla la manda el eje izquierdo.
      ctx.save();
      ctx.font = '10px ui-monospace, monospace';
      ctx.fillStyle = Q.alpha(priceC, 0.85);
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      for (const t of Q.niceTicks(plo - ppad, phi + ppad, 4)) {
        const y = psy(t);
        if (y < box.y - 1 || y > box.y + box.h + 1) continue;
        ctx.fillText(t.toFixed(t >= 1000 ? 1 : 2), box.x + box.w + 6, y);
      }
      ctx.restore();
      ctx.save();
      ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();
    }

    // Punto seleccionado: la línea vertical es la que enlaza con los trades.
    drawDriftCursor(ctx, box, sx);
    ctx.restore();

    ctx.save();
    ctx.font = '10px ui-monospace, monospace';
    ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    ctx.fillStyle = posC; ctx.fillText('CALL ACUM', box.x + 6, box.y + 4);
    ctx.fillStyle = negC; ctx.fillText('PUT ACUM', box.x + 74, box.y + 4);
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.fillText('NETO', box.x + 140, box.y + 4);
    ctx.fillStyle = Q.alpha(Q.token('--price', '#7aa2f7'), 0.9);
    ctx.fillText('PRECIO', box.x + 182, box.y + 4);
    ctx.globalAlpha = 0.7;
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'right';
    ctx.fillText('NET DRIFT · QUANT DATA (OFICIAL)', box.x + box.w - 6, box.y + 4);
    ctx.restore();
    return true;
  }

  function drawDriftCursor(ctx, box, sx) {
    if (!Q.isNum(S.driftPick)) return;
    const x = Math.round(sx(S.driftPick)) + 0.5;
    if (x < box.x || x > box.x + box.w) return;
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x, box.y); ctx.lineTo(x, box.y + box.h);
    ctx.strokeStyle = Q.alpha(Q.token('--gold', '#d9a441'), 0.9); ctx.lineWidth = 1; ctx.stroke();
    ctx.restore();
  }

  /** Subgráfico de VOLUMEN NETO CALL/PUT, tal y como lo firma el proveedor. */
  function drawDriftVolume(ctx, env) {
    const box = driftBox(env);
    if (box.w <= 8 || box.h <= 8) return false;
    const rows = driftRows();
    const win = window_();
    if (!rows.length || !win) { empty(ctx, env, driftEmptyMessage()); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    const vis = [];
    let peak = 0;
    for (const p of rows) {
      const t = driftT(p);
      if (!Q.isNum(t) || t < win.t0 - S.bucketMs || t > win.t1) continue;
      const cv = Q.num(p.call_volume, 0), pv = Q.num(p.put_volume, 0);
      peak = Math.max(peak, Math.abs(cv), Math.abs(pv));
      vis.push({ t, cv, pv });
    }
    if (!vis.length || !(peak > 0)) { empty(ctx, env, 'SIN VOLUMEN NETO EN LA VENTANA'); return false; }
    const sy = Q.scale(-peak * 1.15, peak * 1.15, box.y + box.h, box.y);

    ctx.save();
    const zy = Math.round(sy(0)) + 0.5;
    ctx.beginPath(); ctx.moveTo(box.x, zy); ctx.lineTo(box.x + box.w, zy);
    ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.9); ctx.stroke();

    const bw = Math.max(1, Math.min(9, (box.w / Math.max(1, (win.t1 - win.t0) / S.bucketMs)) * 0.62));
    const posC = Q.token('--pos', '#22c55e'), negC = Q.token('--neg', '#ef4444');
    // El volumen de puts llega YA firmado por el proveedor. No se le cambia el
    // signo: se dibuja donde cae, que es la lectura que publica Quant Data.
    for (const v of vis) {
      const x = sx(v.t);
      for (const [val, col] of [[v.cv, posC], [v.pv, negC]]) {
        if (!val) continue;
        const y0 = sy(0), y1 = sy(val);
        ctx.fillStyle = Q.alpha(col, 0.62);
        ctx.fillRect(Math.round(x - bw / 2), Math.round(Math.min(y0, y1)),
          Math.max(1, Math.round(bw)), Math.max(1, Math.round(Math.abs(y1 - y0))));
      }
    }
    drawDriftCursor(ctx, box, sx);

    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    for (const t of Q.niceTicks(-peak, peak, 3)) {
      if (!t) continue;
      ctx.fillText(Q.compact(t, 1), box.x + box.w + 6, sy(t));
    }
    ctx.globalAlpha = 0.72;
    ctx.textBaseline = 'top';
    ctx.fillText('VOLUMEN NETO CALL / PUT · QUANT DATA', box.x + 6, box.y + 4);
    ctx.restore();
    Q.axisX(ctx, box, sx, win.t0, win.t1);
    return true;
  }

  /* ---------------------------------------- selección de punto → trades */

  /**
   * Al seleccionar un punto de la curva se muestran los trades que lo produjeron.
   *
   * El enlace es directo porque las dos series viven en el mismo reloj: el bucket
   * de Net Drift es de un minuto y los prints del Order Flow ya están agregados
   * en buckets del mismo tamaño. Se busca el bucket de la cinta que contiene el
   * instante elegido y se listan sus impresiones ordenadas por prima.
   */
  function pickDriftAt(clientX, panel) {
    const rows = driftRows();
    if (!rows.length) return;
    const box = driftBox({ w: panel.w, h: panel.h });
    const win = window_(); if (!win) return;
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);
    const t = sx.invert(clientX);
    let best = null;
    for (const p of rows) {
      const pt = driftT(p);
      if (!Q.isNum(pt)) continue;
      if (!best || Math.abs(pt - t) < Math.abs(best - t)) best = pt;
    }
    if (best === null) return;
    S.driftPick = best;
    renderDriftPick();
    invalidateAll();
  }

  /**
   * Impresiones del intervalo seleccionado.
   *
   * Se prefiere el Order Flow de QUANT DATA —el mismo proveedor que publicó la
   * curva—, porque la pregunta es «qué operaciones produjeron ESTE punto» y sólo
   * tiene respuesta limpia si las dos series vienen de la misma cinta. La cinta
   * propia es el respaldo, y la cabecera dice siempre cuál se está mostrando:
   * mezclarlas en silencio haría imposible saber qué se está mirando.
   */
  function driftTradesAt(t0, t1) {
    const qd = (S.drift && S.drift.order_flow && S.drift.order_flow.ready)
      ? S.drift.order_flow.rows : null;
    if (Array.isArray(qd) && qd.length) {
      const hit = qd.filter(p => { const t = Q.parseTime(p.t); return Q.isNum(t) && t >= t0 && t < t1; });
      if (hit.length) return { rows: hit, source: 'QUANT DATA' };
    }
    const bucket = S.buckets.find(b => b.t === t0) || null;
    const own = (bucket && Array.isArray(bucket.prints)) ? bucket.prints : [];
    return { rows: own, source: own.length ? 'CINTA PROPIA' : null };
  }

  function renderDriftPick() {
    const body = document.querySelector('#tblDriftTrades tbody');
    const head = document.getElementById('ofDriftPickLabel');
    if (!body) return;
    if (!Q.isNum(S.driftPick)) {
      body.innerHTML = '<tr><td colspan="7">Selecciona un punto de la curva para ver los trades del intervalo.</td></tr>';
      if (head) head.textContent = '—';
      return;
    }
    const point = driftRows().find(p => driftT(p) === S.driftPick) || null;
    const k = Math.floor(S.driftPick / S.bucketMs) * S.bucketMs;
    const found = driftTradesAt(k, k + S.bucketMs);
    if (head) {
      head.textContent = (point
        ? `${Q.hhmm(S.driftPick)} · CALL ${Q.money(Q.num(point.call, 0), 1)} · PUT ${Q.money(Q.num(point.put, 0), 1)}`
          + `${point.open ? ' · BUCKET ABIERTO' : ''}`
        : Q.hhmm(S.driftPick)) + (found.source ? ` · ${found.source}` : '');
    }
    const prints = found.rows.slice()
      .sort((a, b) => Math.abs(Q.num(b.premium, 0)) - Math.abs(Q.num(a.premium, 0))).slice(0, 60);
    if (!prints.length) {
      body.innerHTML = `<tr><td colspan="7">Sin impresiones del Order Flow en ${Q.hhmm(S.driftPick)}.</td></tr>`;
      return;
    }
    body.innerHTML = prints.map(p => {
      const dir = Q.num(p.direction, 0);
      const cls = dir > 0 ? 'pos' : dir < 0 ? 'neg' : '';
      return `<tr><td>${Q.hhmm(Q.parseTime(p.t))}</td>`
        + `<td>${String(p.option_type || '').toUpperCase() || '—'}</td>`
        + `<td>${Q.num(p.strike) || '—'}</td>`
        + `<td>${p.expiration || '—'}</td>`
        + `<td class="${cls}">${Q.money(Math.abs(Q.num(p.premium, 0)), 1)}</td>`
        + `<td>${p.aggressor || p.side || '—'}</td>`
        + `<td>${p.execution || '—'}</td></tr>`;
    }).join('');
  }

  function renderDriftSummary() {
    const set = (id, v) => { const x = document.getElementById(id); if (x) x.textContent = v; };
    const d = S.drift;
    if (!d || !d.ready) {
      for (const id of ['ndCall', 'ndPut', 'ndNet', 'ndVolume']) set(id, 'SIN DATOS');
      set('ndState', d ? String(d.state || 'SIN DATOS') : 'SIN DATOS');
      set('ndStateDetail', d ? String(d.detail || '') : 'Quant Data no publicó Net Drift');
      return;
    }
    set('ndCall', Q.money(Q.num(d.cum_call_premium, 0), 1));
    set('ndPut', Q.money(Q.num(d.cum_put_premium, 0), 1));
    set('ndNet', Q.money(Q.num(d.cum_net_premium, 0), 1));
    set('ndVolume', `${Q.compact(Q.num(d.cum_call_volume, 0), 1)} / ${Q.compact(Q.num(d.cum_put_volume, 0), 1)}`);
    set('ndState', String(d.state || ''));
    set('ndStateDetail', `${d.buckets || 0} buckets · ${d.detail || ''}`);
  }

  /* -------------------------------------------------------------- API */

  function mount(ids) {
    const mk = (host, draw, id) => host ? new Q.Panel(host, draw, sharedOpts(id)) : null;
    S.panels.price = mk(ids.price, drawPrice, 'flow:price');
    S.panels.aggr = mk(ids.aggressor, drawAggressor, 'flow:aggr');
    S.panels.net = mk(ids.net, drawNetFlow, 'flow:net');
    S.panels.volume = mk(ids.volume, drawVolume, 'flow:volume');
    S.panels.total = mk(ids.total, drawTotal, 'flow:total');
    // Net Drift comparte el TimeLink con la cinta: es el MISMO eje temporal, no dos
    // ejes que coinciden por casualidad. Por eso también comparte zoom y arrastre.
    const driftOpts = id => {
      const o = sharedOpts(id);
      const down = o.onDown;
      let downX = NaN;
      o.onDown = (pt, ev, panel) => { downX = pt.x; if (down) down(pt, ev, panel); };
      o.onUp = (pt, ev, panel) => {
        sharedUp();
        // Un clic es un clic; un arrastre de más de 3px es una panorámica y no
        // debe cambiar el punto seleccionado.
        if (Q.isNum(downX) && Math.abs(pt.x - downX) <= 3) pickDriftAt(pt.x, panel);
        downX = NaN;
      };
      return o;
    };
    S.panels.drift = ids.drift ? new Q.Panel(ids.drift, drawDrift, driftOpts('flow:drift')) : null;
    S.panels.driftVolume = ids.driftVolume
      ? new Q.Panel(ids.driftVolume, drawDriftVolume, driftOpts('flow:driftvol')) : null;
    for (const k in S.panels) if (!S.panels[k]) delete S.panels[k];
    S.link.on(invalidateAll);
    renderDriftPick();
    return S;
  }

  /**
   * Net Drift OFICIAL del bundle de terminal.
   *
   * Llega ya acumulado por `app/core/net_drift.py` desde la respuesta cruda de
   * `POST /v1/options/tool/net-drift`. Este módulo no lo recalcula, no lo mezcla
   * con QFLOW y no lo sustituye por Net Flow si falta.
   */
  function applyNetDrift(d) {
    S.drift = (d && typeof d === 'object') ? d : null;
    // Si el instante seleccionado ya no existe en la nueva serie, se suelta: dejar
    // un cursor apuntando a un bucket inexistente mostraría trades de otro momento.
    if (Q.isNum(S.driftPick) && !driftRows().some(p => driftT(p) === S.driftPick)) S.driftPick = NaN;
    renderDriftSummary();
    renderDriftPick();
    invalidateAll();
    return S.drift;
  }

  /** Alterna entre la CINTA y NET DRIFT dentro de la MISMA sección. */
  function setPanel(which) {
    S.panel = which === 'drift' ? 'drift' : 'tape';
    const tape = document.getElementById('ofTapeStack');
    const drift = document.getElementById('ofDriftPanel');
    if (tape) tape.hidden = S.panel !== 'tape';
    if (drift) drift.hidden = S.panel !== 'drift';
    const kt = document.getElementById('ofTapeKpis');
    const kd = document.getElementById('ofDriftKpis');
    if (kt) kt.hidden = S.panel !== 'tape';
    if (kd) kd.hidden = S.panel !== 'drift';
    invalidateAll();
    return S.panel;
  }

  /** Capa QFLOW del bundle de terminal. Independiente de /api/nextgen/trace. */
  function applyQflow(q) {
    S.qflow = (q && typeof q === 'object') ? q : null;
    invalidateAll();
    return S.qflow;
  }

  /** Alimenta el panel con el mismo payload de /api/nextgen/trace. */
  function applyTrace(payload) {
    if (!payload || typeof payload !== 'object') return;
    S.symbol = String(payload.symbol || '');
    S.candles = Array.isArray(payload.candles) ? payload.candles : [];
    S.prints = Array.isArray(payload.option_prints) ? payload.option_prints : [];
    const last = S.candles.length ? S.candles[S.candles.length - 1] : null;
    const spot = Q.num(last && last.c, Q.num(payload?.profiles?.spot, NaN));
    // Los mismos niveles estructurales que TRACE: leer el flujo contra Call Wall,
    // Put Wall y Zero Gamma es justo lo que dice si la agresión está empujando
    // hacia una pared o rebotando en ella.
    S.levels = (Array.isArray(payload.levels) ? payload.levels : [])
      .filter(l => Q.FLOW_LEVEL_KINDS.indexOf(l.kind) >= 0)
      .filter(l => {
        const p = Q.num(l && l.price, NaN);
        if (!Q.isNum(p) || !Q.isNum(spot)) return Q.isNum(p);
        if (l.kind === 'call_wall') return p > spot;
        if (l.kind === 'put_wall') return p < spot;
        return true;
      })
      .sort((a, b) => Q.levelStyle(a.kind).order - Q.levelStyle(b.kind).order);
    S.bucketMs = Math.max(30_000, Q.num(payload.bar_interval_ms, 60_000));
    rebuild();
    if (S.link.follow) {
      const ts = S.candles.map(c => Q.parseTime(c.t)).filter(Q.isNum);
      if (ts.length) {
        const a = Math.min.apply(null, ts), b = Math.max.apply(null, ts);
        S.link.setWindow(a, b + Math.max((b - a) * 0.05, 120_000), { silent: true });
      }
    }
    for (const k in S.panels) { S.panels[k].animate(true); S.panels[k].invalidate(); }
    renderSummary();
    // Los trades del intervalo seleccionado salen de la cinta: si la cinta se
    // renueva, la lista tiene que renovarse con ella.
    renderDriftPick();
  }

  function renderSummary() {
    const set = (id, v) => { const x = document.getElementById(id); if (x) x.textContent = v; };
    let buy = 0, sell = 0, total = 0, biggest = null;
    let unknown = 0;
    for (const b of S.buckets) { buy += b.buy; sell += b.sell; total += b.total; unknown += (b.unknown || 0); }
    for (const p of S.prints) {
      const prem = Math.abs(Q.num(p.premium, 0));
      if (!biggest || prem > Math.abs(Q.num(biggest.premium, 0))) biggest = p;
    }
    // v1.43.0 · Sin cinta observada NO se publica `$0.0`.
    //
    // Un cero aquí afirma «hoy no se negoció prima», y eso es una conclusión, no un
    // hueco. Cuando la sesión todavía no ha dejado ningún print —arranque, mercado
    // cerrado, proveedor caído— el panel ya dice SIN FLUJO DIRECCIONAL debajo; que
    // los KPIs de arriba dijeran `$0.0` a la vez era contradecirse en la misma
    // pantalla. Un cero sólo se muestra cuando hubo prints y su suma es realmente
    // cero, que es lo que distingue un dato de un vacío.
    const conCinta = S.prints.length > 0 || total > 0;
    const prima = (v) => (conCinta ? Q.money(v, 1) : 'SIN DATOS');
    set('ofTotalPremium', prima(total));
    set('ofBuyPremium', prima(buy));
    set('ofSellPremium', prima(sell));
    set('ofNetPremium', prima(buy - sell));
    // La cobertura de clasificación explica el sesgo: un 8% clasificado no sostiene
    // la misma lectura que un 95%, y eso tiene que verse junto al número.
    const cov = total > 0 ? (buy + sell) / total * 100 : 0;
    set('ofPrintCount', total > 0
      ? `${S.prints.length} prints · ${cov.toFixed(0)}% con agresor`
        + (unknown > 0 ? ` · ${Q.money(unknown, 1)} sin clasificar` : '')
      : (conCinta ? String(S.prints.length) : 'sin cinta observada'));
    set('ofBiggest', biggest ? Q.money(Math.abs(Q.num(biggest.premium)), 1) : '—');
    set('ofBiggestDetail', biggest
      ? `${Q.hhmm(Q.parseTime(biggest.t))} · ${String(biggest.option_type || '').toUpperCase()} ${Q.num(biggest.strike) || ''} · ${biggest.aggressor || ''}`
      : '—');
    // El sesgo se mide sobre la prima CLASIFICADA, no sobre el total: dividir por
    // prima sin agresor diluía el sesgo hacia EQUILIBRADO siempre que la cinta
    // llegara sin cotización, que es justo cuando peor se lee.
    const clasificada = buy + sell;
    const bias = clasificada > 0 ? (buy - sell) / clasificada : 0;
    set('ofBias', clasificada <= 0
      ? (total > 0 ? 'SIN CLASIFICAR' : '—')
      : (bias > 0.12 ? 'COMPRADOR' : bias < -0.12 ? 'VENDEDOR' : 'EQUILIBRADO'));
  }

  function setMinPremium(v) { S.minPremium = Math.max(0, Q.num(v, 0)); rebuild(); invalidateAll(); }
  function setMode(m) { S.mode = m === 'line' ? 'line' : 'area'; invalidateAll(); }
  function setFollow(on) { S.link.follow = !!on; if (on) applyTrace({ symbol: S.symbol, candles: S.candles, option_prints: S.prints, levels: S.levels, bar_interval_ms: S.bucketMs }); }

  global.ITMQFlow = { mount, applyTrace, applyQflow, applyNetDrift, setPanel,
    setMinPremium, setMode, setFollow, state: S };
})(window);
