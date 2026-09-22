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

  // v1.44.0 · Estados de dato traducidos a lenguaje de ANÁLISIS.
  //
  // `NO_PROVIDER_DATA` es un código interno perfecto para el Auditor y pésimo en
  // pantalla: no dice si el mercado está tranquilo o si faltan datos, que es lo
  // único que el operador necesita decidir en ese momento.
  const ESTADO_DATO = {
    DATA_OK: 'CON DATOS',
    NO_PROVIDER_DATA: 'SIN DATOS',
    FILTERED_ALL: 'SIN DATOS ÚTILES',
    PROVIDER_ERROR: 'SIN DATOS',
    PARSER_ERROR: 'SIN DATOS',
    STALE: 'DATO ANTIGUO',
  };

  /* -------------------------------------------- legibilidad de los carriles
   *
   * Los cuatro histogramas inferiores usaban `Math.max(1, …)` para el grosor y
   * para la altura, y alfa 0.62. A la densidad real de una sesión —390 buckets de
   * un minuto en un carril de 1500 px— eso daba barras de uno o dos píxeles a
   * media opacidad: existían en el dato y no en la pantalla.
   *
   * Los mismos suelos que `itmq_panels.js`, aquí porque este módulo dibuja a mano
   * sobre su propio lienzo. Son suelos de VISIBILIDAD: un cero sigue midiendo
   * cero, y la magnitud real sigue en el eje y en el tooltip.
   */
  /* v1.47.0 · El grosor de los carriles lo sirve el componente común.
   *
   * `laneBarWidth` era el mismo defecto que en los paneles: un suelo de 3 px
   * aplicado al grosor y un 0.82 sobre un hueco que, con una sesión entera a un
   * minuto en 330 px, valía 0.85 px. El resultado eran rayitas solapadas — que
   * es exactamente como se veían AGRESOR, VOLUMEN, TOTAL y el flujo direccional.
   *
   * Ahora, cuando un minuto no da para una barra con presencia, se agrupa el
   * INTERVALO —2 m, 3 m, 5 m…— y no la posición: el carril sigue alineado con
   * las velas de TRACE porque cada contenedor ocupa su tramo real del eje.
   */
  const AB = global.ITMQBars;

  /* ------------------------------------------------- etiqueta de carril
   *
   * v1.61.0 · El nombre del carril, en una PLACA y no suelto sobre el lienzo.
   *
   * Escrito directamente sobre el fondo, el rótulo compite con las barras y con
   * la rejilla: en cuanto una barra le pasa por detrás deja de leerse. Con
   * fondo propio y borde se lee siempre y separa visualmente un carril del
   * siguiente, que es lo que hace la referencia del operador.
   */
  function laneTag(ctx, box, text, opts) {
    const t = String(text || '');
    if (!t) return;
    const o = opts || {};
    const x = box.x + (o.dx || 0);
    const y = (o.y !== undefined) ? o.y : box.y + 3;
    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    const w = ctx.measureText(t).width + 12;
    const h = 15;
    ctx.fillStyle = Q.alpha(Q.token('--panel-2', '#141b28'), 0.92);
    ctx.strokeStyle = Q.token('--border', '#243044');
    ctx.lineWidth = 1;
    if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x, y, w, h, 3); ctx.fill(); ctx.stroke(); }
    else { ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h); }
    ctx.fillStyle = o.color || Q.token('--text-dim', '#8494ad');
    ctx.fillText(t, x + 6, y + h / 2 + 0.5);
    ctx.restore();
  }

  function laneExtent(px, value, axisPx) {
    return AB ? AB.extent(px, value, axisPx)
              : (!Number.isFinite(value) || value === 0 ? 0 : Math.max(3, Math.abs(px)));
  }

  /** Contenedores temporales del carril, con el ancho real del panel. */
  function laneBins(box, win) {
    return AB.timeBins(S.buckets, win.t0, win.t1 + S.bucketMs, S.bucketMs, box.w);
  }

  function estadoDato(state) {
    return ESTADO_DATO[String(state || '')] || 'SIN DATOS';
  }

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
    deltaMin: null,
    // FlowViewModel: politica UNICA de frescura por carril. Cuando viaja, las
    // tarjetas leen de el; si no, se recalculan de la cinta local como antes.
    flowView: null,
    // De dónde sale el eje de precio del panel de Net Drift. Ver drawDrift.
    driftPriceSource: null,
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
    // Qué intervalos se marcan en oro en el carril TOTAL de Net Drift.
    goldRule: 'top3',
    hover: NaN,
    _netMax: new Q.GlideValue(300),
    _totMax: new Q.GlideValue(300),
  };

  /* ------------------------------------------------------ escala simlog */

  /** Comprime rangos de 5 órdenes de magnitud sin perder el signo ni el cero.
   *
   * `linthresh` es el punto donde la escala pasa de lineal a logarítmica, y tiene
   * que ser RELATIVO al activo: un umbral en dólares hace que el mismo carril se
   * comporte distinto según lo que valga el subyacente. El defecto de 1e6 que había
   * aquí como defecto por omisión no llegaba a morder —todas las llamadas pasan su
   * propio umbral— pero era una trampa esperando a la primera llamada que lo
   * olvidara, así que se elimina.
   */
  function symlog(v, linthresh) {
    const c = Number.isFinite(linthresh) && linthresh > 0 ? linthresh : 1e-9;
    const x = Q.num(v, 0);
    const s = x < 0 ? -1 : 1;
    return s * Math.log10(1 + Math.abs(x) / c);
  }

  /* ---------------------------------------------------------- agregación */

  /**
   * v1.56.0 · Por qué un carril de OPCIONES está vacío, con el motivo real.
   *
   * «SIN PRIMA OBSERVADA» es una conclusión: dice que hoy no se negoció prima.
   * Sólo vale cuando NO hay dato actual y TAMPOCO último valor bueno. Si el
   * último ciclo vino vacío pero hay histórico, lo que toca es enseñar el
   * histórico y decir de cuándo es.
   */
  function laneEmpty(dataset, porDefecto) {
    const vm = S.flowView;
    const lane = vm && vm[dataset];
    if (!lane) return porDefecto;
    if (lane.status === 'NO_DATA') return porDefecto;
    if (lane.status === 'PROVIDER_ERROR') return 'EL PROVEEDOR FALLÓ EN ESTE CICLO';
    if (lane.screen_note) return String(lane.screen_note).toUpperCase();
    return porDefecto;
  }

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

    const nuevo = k => ({ t: k, net: 0, total: 0, buy: 0, sell: 0, unknown: 0,
                          underlyingNet: 0, prints: [], vol: 0 });

    /* v1.56.0 · LAS BARRAS DE OPCIONES SALEN DEL FlowViewModel.
     *
     * Antes se construían aquí a partir de `S.prints`, que viene de
     * `trace.option_prints`. Cuando ese campo llegaba vacío —y llegaba vacío
     * aunque `order-flow` hubiera devuelto cientos de operaciones— los carriles
     * de AGRESOR y PRIMA se quedaban en «SIN FLUJO DIRECCIONAL» y «SIN PRIMA
     * OBSERVADA», mientras el de VOLUMEN SUBYACENTE, que se alimenta de las
     * velas, seguía lleno. En pantalla eso se lee como «no hubo flujo de
     * opciones»: una conclusión sobre el mercado donde había una ruta rota.
     *
     * El modelo trae las barras ya agrupadas, de la MISMA cinta que las
     * tarjetas y que QFLOW, y con LKG por carril: un ciclo vacío ya no las
     * borra. La cinta local queda como respaldo si el modelo no viaja.
     */
    const barras = (S.flowView && S.flowView.aggressor_bars
                    && Array.isArray(S.flowView.aggressor_bars.current))
      ? S.flowView.aggressor_bars.current : null;

    const floor = effectiveMinPremium();
    if (barras) {
      for (const r of barras) {
        const k = key(Q.num(r.t, NaN));
        if (!Q.isNum(k)) continue;
        const b = byBucket.get(k) || nuevo(k);
        b.buy += Q.num(r.buy_premium, 0);
        b.sell += Q.num(r.sell_premium, 0);
        b.unknown += Q.num(r.unknown_premium, 0);
        b.total += Q.num(r.total_premium, 0);
        byBucket.set(k, b);
      }
      // Los prints individuales siguen siendo del trace: son el DETALLE de un
      // intervalo, no la barra. Que falten no puede vaciar la barra.
      for (const p of S.prints) {
        const t = Q.parseTime(p.t);
        const prem = Math.abs(Q.num(p.premium, 0));
        if (!Q.isNum(t) || prem < floor) continue;
        const b = byBucket.get(key(t));
        if (b) b.prints.push(p);
      }
    } else {
      for (const p of S.prints) {
        const t = Q.parseTime(p.t);
        const prem = Math.abs(Q.num(p.premium, 0));
        if (!Q.isNum(t) || prem <= 0) continue;
        const k = key(t);
        const b = byBucket.get(k) || nuevo(k);
        b.total += prem;
        const dir = Q.num(p.direction, 0);
        // La prima sin agresor identificado se contabiliza aparte. Sin esto, un carril
        // de flujo neto plano con millones negociados delante parecía una avería
        // cuando en realidad la cinta no traía cotización con la que clasificarla.
        if (dir > 0) b.buy += prem; else if (dir < 0) b.sell += prem; else b.unknown += prem;
        if (prem >= floor) b.prints.push(p);
        byBucket.set(k, b);
      }
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
    // v1.46.0 · Un ciclo sin prima da pico 0, no 1. El «1» era un dólar: en un
    // activo de 10⁸ no se nota y en uno de 10² es la escala entera.
    S._netMax.set(netMax > 0 ? netMax : 0);
    S._totMax.set(totMax > 0 ? totMax : 0);
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
      Q.levelLine(ctx, box, y, Q.alpha(Q.token(st.color, '#8494ad'), 0.72),
                  { width: Q.LEVEL_LINE_WIDTH });
    }
    // Las etiquetas se apilan para que dos niveles cercanos no se tapen, con la
    // MISMA tipografía que TRACE y Net Drift: tres tamaños para el mismo muro
    // harían que la pantalla no pareciera del mismo programa.
    for (const it of Q.stackLabels(drawn, Q.LEVEL_LABEL_GAP)) {
      Q.chip(ctx, box.x + 6,
        Q.clamp(it.y, box.y + Q.LEVEL_LABEL_H / 2, box.y + box.h - Q.LEVEL_LABEL_H / 2),
        `${it.name} ${it.price.toFixed(it.price >= 1000 ? 0 : 2)}`,
        { bg: Q.alpha(it.color, 0.9), color: '#06101c',
          font: Q.LEVEL_FONT, h: Q.LEVEL_LABEL_H, padX: 7 });
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
      // Referencia común del ciclo: sin ella, dos marcas del mismo tamaño
      // podrían significar cosas muy distintas.
      let qPeak = 0;
      for (const ev of qEvents.slice(0, 8)) {
        const v = Math.abs(Q.num(ev.premium, NaN));
        if (Q.isNum(v)) qPeak = Math.max(qPeak, v);
      }
      for (const ev of qEvents.slice(0, 8)) {
        const t = Q.parseTime(ev.t), pxv = Q.num(ev.price, NaN);
        if (!Q.isNum(t) || !Q.isNum(pxv)) continue;
        const x = sx(t), y = sy(pxv);
        if (x < box.x || x > box.x + box.w || y < box.y || y > box.y + box.h) continue;
        /* v1.50.0 · Círculo dorado en el sitio, flecha de sentido, cantidad.
         *
         * El círculo marca DÓNDE ocurrió y su radio dice cuánto, así que dos
         * concentraciones se comparan sin leer las cifras. La flecha dice el
         * sentido —verde compra, rojo venta—: tiene forma propia, así que el
         * color ya no se confunde con el de la línea de precio, que es lo que
         * pasaba cuando el color era lo único que distinguía CALL de PUT.
         */
        // `null` = lado agresor DESCONOCIDO. Se dibuja arriba, como una
        // compra, pero con rombo neutro: la posición no afirma nada, la forma sí.
        // UNA sola implementación, la misma que TRACE y que Net Drift.
        Q.flowMark(ctx, x, y, ev, qPeak, { label: markerLabel(ev) });
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
    // La banda usa los MISMOS contenedores que las barras de abajo: si el carril
    // agrupa de cinco en cinco minutos, la atribución comprador/vendedor tiene
    // que describir ese mismo tramo o las dos lecturas se contradicen.
    const bins = laneBins(box, win);
    let peak = 0;
    for (const r of bins.rows) {
      const t = AB.reduceBin(r, b => Q.num(b.buy, 0) + Q.num(b.sell, 0));
      peak = Math.max(peak, t.sum);
    }
    if (peak <= 0) peak = 1;

    ctx.save();
    ctx.fillStyle = Q.alpha(Q.token('--panel-2', '#141b28'), 1);
    ctx.fillRect(box.x, box.y, box.w, box.h);
    for (const r of bins.rows) {
      const bu = AB.reduceBin(r, b => Q.num(b.buy, 0)).sum;
      const se = AB.reduceBin(r, b => Q.num(b.sell, 0)).sum;
      const tot = bu + se;
      if (tot <= 0) continue;
      const x0 = sx(r.t), x1 = sx(r.tEnd);
      const net = (bu - se) / tot;                 // −1 vendedor · +1 comprador
      const intensity = Q.clamp(Math.pow(tot / peak, 0.5), 0.08, 1);
      ctx.fillStyle = Q.alpha(net >= 0 ? buy : sell, intensity);
      ctx.fillRect(x0, box.y, Math.max(1, x1 - x0), box.h);
    }
    ctx.restore();

    // v1.56.0 · Tres datasets DISTINTOS, con nombres que no se confunden:
    // el agresor y la prima son de OPCIONES; el volumen gris es del SUBYACENTE.
    laneTag(ctx, box, 'AGRESOR · OPCIONES', { dx: 4, y: 2 });

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
        : laneEmpty('aggressor_bars', 'SIN FLUJO DIRECCIONAL'));
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
    // El pico suavizado puede no existir todavía (ningún `set` aún): entonces
    // manda el pico real de la ventana. `Math.max` con un NaN devuelve NaN, así
    // que el valor se sanea antes de compararlo, no después.
    const smoothed = Q.num(S._netMax.get(), 0);
    const max = Math.max(smoothed, actualPeak, Number.MIN_VALUE);
    // v1.46.0 · El umbral es una FRACCIÓN del pico, sin suelo en dólares.
    //
    // Antes: `Math.max(max / 400, 1000)`. Por encima de ~400 K$ de pico mandaba
    // `max/400` y el carril se leía igual en todos los activos; por debajo mandaba
    // el suelo de MIL DÓLARES y el carril se iba aplanando según lo que valiera el
    // subyacente. Medido sobre un bucket al 10 % del pico:
    //
    //     pico 12 M  →  62 %      pico 40 K  →  43 %
    //     pico 900 K →  62 %      pico  5 K  →  23 %      pico 800 →  13 %
    //
    // Un ETF grande y una acción pequeña con la MISMA forma de sesión se dibujaban
    // distinto, y la diferencia la fijaba una constante en dólares que nadie había
    // decidido para esos activos. Ahora los cinco dan 62 %.
    const lin = Math.max(max / 400, Number.MIN_VALUE);
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
    const netBins = laneBins(box, win);
    for (const r of netBins.rows) {
      // Flujo neto: SUMA con signo dentro del intervalo. Es lo que significa
      // «prima direccional de estos cinco minutos».
      const agg = AB.reduceBin(r, b => Q.num(b.net, 0));
      if (!agg.sum) continue;
      const x0 = sx(r.t), x1 = sx(r.tEnd);
      const bw = Math.min(netBins.thickness, Math.max(1, x1 - x0));
      const y = sy(symlog(agg.sum, lin));
      const h = laneExtent(y - y0, agg.sum, box.h);
      const bx = x0 + (x1 - x0 - bw) / 2;
      ctx.fillStyle = Q.alpha(agg.sum >= 0 ? posC : negC, 0.95);
      ctx.fillRect(bx, agg.sum >= 0 ? y0 - h : y0, bw, h);
      if (agg.peak_dominates) {
        // Los signos del intervalo se compensan y la suma queda pequeña, pero
        // dentro hubo un evento grande. Se marca su alcance para no esconderlo.
        const py = sy(symlog(agg.peak, lin));
        ctx.fillStyle = Q.alpha(agg.peak >= 0 ? posC : negC, 0.5);
        ctx.fillRect(bx, py - 1, bw, 2);
      }
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
    laneTag(ctx, box, 'FLUJO NETO · PRIMA DIRECCIONAL', { dx: 4, y: 2 });
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
    if (!win || !S.buckets.length) { empty(ctx, env, laneEmpty('premium_bars', 'SIN PRIMA OBSERVADA')); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);
    let actualPeak = 0;
    for (const b of S.buckets) {
      if (b.t < win.t0 - S.bucketMs || b.t > win.t1) continue;
      actualPeak = Math.max(actualPeak, Number(b.total) || 0);
    }
    const max = Math.max(Q.num(S._totMax.get(), 0), actualPeak, Number.MIN_VALUE);
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
    const sumas = [];
    let painted = 0;
    const totalBins = laneBins(box, win);
    for (const r of totalBins.rows) {
      // Prima total: SUMA. El pico del intervalo decide si el contenedor se
      // pinta como «grande», para que un print relevante no se diluya entre
      // vecinos pequeños al agrupar.
      const agg = AB.reduceBin(r, b => Q.num(b.total, 0));
      if (agg.sum <= 0) continue;
      const prints = r.members.reduce((n, b) => n + (b.prints ? b.prints.length : 0), 0);
      const x0 = sx(r.t), x1 = sx(r.tEnd);
      const bw = Math.min(totalBins.thickness, Math.max(1, x1 - x0));
      const bx = x0 + (x1 - x0 - bw) / 2;
      const y = sy(agg.sum);
      const big = prints > 0 || agg.peak >= floor;
      const h = laneExtent(y0 - y, agg.sum, box.h);
      // El contraste del bucket «pequeño» sube de 0.35 a 0.55: a 0.35 sobre el
      // fondo del panel la barra se perdía, y entonces el carril parecía vacío
      // cuando en realidad estaba lleno de actividad normal.
      ctx.fillStyle = big ? gold : Q.alpha(dim, 0.55);
      ctx.fillRect(bx, y0 - h, bw, h);
      painted += 1;
      if (big) tall.push({ x: bx + bw / 2, y, total: agg.sum });
      sumas.push(agg.sum);
    }

    /* v1.57.0 · LA MEDIA, para saber contra qué destaca una barra.
     *
     * Sin referencia, una barra alta sólo dice «ésta es la más alta de lo que
     * se ve». Con la media dibujada dice cuántas veces la supera, que es la
     * pregunta real. Misma línea y misma pastilla que en Net Drift: el mismo
     * significado se lee igual en las dos pantallas.
     */
    if (sumas.length) {
      const media = sumas.reduce((a, b) => a + b, 0) / sumas.length;
      if (media > 0) {
        const my = Math.round(sy(media)) + 0.5;
        if (my > box.y && my < box.y + box.h) {
          const avgC = Q.token('--info-600', '#3b82f6');
          ctx.setLineDash([4, 3]);
          ctx.strokeStyle = Q.alpha(avgC, 0.85); ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(box.x, my); ctx.lineTo(box.x + box.w, my); ctx.stroke();
          ctx.setLineDash([]);
          const txt = `Prom ${Q.money(media, 0)}`;
          ctx.font = '700 9px ui-monospace, monospace';
          const pw = ctx.measureText(txt).width + 12;
          const px0 = box.x + box.w - pw - 2;
          ctx.fillStyle = Q.alpha(avgC, 0.9);
          Q.roundRect(ctx, px0, my - 7, pw, 14, 4); ctx.fill();
          ctx.fillStyle = Q.token('--panel', '#0d131c');
          ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
          ctx.fillText(txt, px0 + pw / 2, my);
        }
      }
    }
    // Ejes dibujados pero ningun contenedor con prima: el carril quedaria en
    // blanco sin decir por que. Un carril vacio sin causa es indistinguible de
    // un fallo de render, asi que se declara.
    if (!painted) { ctx.restore(); empty(ctx, env, laneEmpty('premium_bars', 'SIN PRIMA OBSERVADA')); return false; }
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
    laneTag(ctx, box, 'PRIMA · OPCIONES', { dx: 4, y: 2 });
    
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
  /* La flecha dice el SENTIDO y la cifra la CANTIDAD; el color es siempre oro.
   *
   * Codificar el sentido en el color obligaba a usar el mismo verde y el mismo
   * rojo que las velas, y una marca sobre una vela de su color desaparecía
   * dentro de ella. La flecha no se confunde con nada.
   */
  /* ¿COMPRA, VENTA o ni una cosa ni otra?
   *
   * v1.52.0 · Esta función devolvía «compra» cuando el evento traía CALL y
   * «venta» cuando traía PUT, y eso era FALSO de dos maneras a la vez:
   *
   *   · `side` es DOMINANCIA DE PRIMA por tipo de contrato, no dirección. Un
   *     intervalo dominado por calls puede estar formado íntegramente por calls
   *     VENDIDAS —venta de volatilidad, lectura bajista o neutra— y la pantalla
   *     dibujaba una flecha verde de compra encima.
   *   · Comprar una PUT es una COMPRA. Marcarla como venta por ser put invierte
   *     el sentido de la operación que se está señalando.
   *
   * El único campo que habla de dirección es `aggressor`, que el motor resuelve
   * desde la cinta de order-flow. Y cuando no se conoce, NO se elige un lado:
   * se devuelve null y la marca se dibuja neutra. Una flecha inventada sobre un
   * gráfico de operativa puede costar dinero; un rombo que dice «no sé» no.
   */
  /* v1.56.0 · Autoridad ÚNICA de la marca, en `itmq_core`. Ver allí por qué
   * no puede haber dos copias de esto.
   *
   * v1.57.0 · Aquí había cuatro alias locales —`flowSide`, `flowArrow`,
   * `flowAmount`, `evStrength`— que apuntaban a `Q.*` y que ya no usaba nadie:
   * las marcas se dibujan con `Q.flowMark`. Un alias muerto con el nombre exacto
   * de la función duplicada que se elimino es justo la trampa contra la que
   * avisa el comentario de arriba: alguien lo "corrige" aqui, no cambia nada en
   * pantalla, y se pasa la tarde buscando por que. */

  function markerLabel(ev) {
    if (!ev) return '';
    if (ev.label) return String(ev.label);
    // v1.50.0 · Sólo la cantidad. El sentido lo dice la flecha dibujada, y
    // repetirlo en el texto ocupaba sitio sin añadir nada.
    const amount = Q.money(Math.abs(Q.num(ev.premium, NaN)), 1);
    return amount === '—' ? '' : amount;
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

    const volBins = laneBins(box, win);
    const bw = volBins.thickness;
    const pos = Q.token('--pos', '#22c55e'), neg = Q.token('--neg', '#ef4444'), flat = Q.token('--text-dim', '#8494ad');
    for (const r of volBins.rows) {
      // Volumen: SUMA. El signo lo decide la cinta agregada del intervalo.
      const v = Math.abs(AB.reduceBin(r, b => Math.abs(Q.num(b.vol, 0))).sum);
      if (!(v > 0)) continue;
      const signed = AB.reduceBin(r, b => Q.num(b.underlyingNet, 0)).sum;
      const x0 = sx(r.t), x1 = sx(r.tEnd);
      const x = x0 + (x1 - x0) / 2;
      const y = sy(v);
      const h = laneExtent((box.y + box.h) - y, v, box.h);
      ctx.fillStyle = Q.alpha(signed > 0 ? pos : signed < 0 ? neg : flat, 0.9);
      ctx.fillRect(Math.round(x - bw / 2), Math.round(box.y + box.h - h),
                   Math.max(1, Math.round(bw)), Math.max(1, Math.round(h)));
    }

    ctx.globalAlpha = 0.72;
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    laneTag(ctx, box, 'VOLUMEN SUBYACENTE · SIP · COLOR = SIGNO DE CINTA', { dx: 6, y: 2 });
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
      // v1.55.0 · Un bucket sin acumulado es un HUECO, no un cero. Con `0` la
      // curva caia al eje y afirmaba que la sesion se habia vaciado.
      vis.push({ t, call: Q.num(p.cum_call, NaN), put: Q.num(p.cum_put, NaN),
        net: Q.num(p.cum_net, NaN), mid: Q.num(p.cum_mid_net, NaN),
        price: Q.num(p.price, NaN), open: !!p.open });
    }
    if (!vis.length) { empty(ctx, env, 'SIN DATOS EN LA VENTANA'); return false; }

    // Eje izquierdo: prima acumulada. Se incluye el cero SIEMPRE porque el signo
    // es la lectura: una curva que no muestra su cruce por cero no dice nada.
    let lo = 0, hi = 0;
    for (const v of vis) {
      for (const x of [v.call, v.put, v.net, v.mid]) {
        if (!Q.isNum(x)) continue;   // un hueco no estira ni encoge la escala
        lo = Math.min(lo, x); hi = Math.max(hi, x);
      }
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
      // El hueco PARTE el trazo. Interpolar por encima de un bucket sin dato
      // dibujaria una recta que nadie midio.
      ctx.beginPath();
      let started = false;
      for (const v of vis) {
        const val = v[key];
        if (!Q.isNum(val)) { started = false; continue; }
        const x = sx(v.t), y = sy(val);
        if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
      }
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = 'round'; ctx.stroke();
      ctx.restore();
    };
    /* v1.57.0 · CUATRO CURVAS, y cada una dice algo distinto.
     *
     * El NETO pasa a blanco sólido: es la que se lee primero y con trazo
     * discontinuo y apagado competía con la rejilla.
     *
     * La cuarta —prima A MEDIO— es dato del proveedor que se recibía y no se
     * dibujaba. Va en azul y más fina que las otras: acompaña a la de prima
     * pagada, no compite con ella. Su distancia a la blanca ES la lectura.
     */
    const midC = Q.token('--info-600', '#3b82f6');
    line('mid', Q.alpha(midC, 0.9), 1.5);
    line('net', Q.token('--text', '#e6edf7'), 1.8);
    line('call', posC, 1.8);
    line('put', negC, 1.8);

    /* v1.50.0 · El valor de cada curva, en su extremo.
     *
     * Tres curvas superpuestas y una leyenda arriba obligan a seguir cada
     * trazo con la vista hasta la escala para saber cuánto vale. La cifra en
     * el extremo responde «¿cuánto llevamos?» sin recorrer nada, que es la
     * pregunta que se le hace a una curva acumulada.
     */
    const tail = vis[vis.length - 1];
    if (tail) {
      const marks = [
        ['net', Q.token('--text', '#e6edf7')],
        ['call', posC], ['put', negC],
        ['mid', Q.token('--info-600', '#3b82f6')],
      ].map(([k, c]) => ({ v: Q.num(tail[k], NaN), col: c, y: sy(Q.num(tail[k], NaN)) }))
       .filter(m => Q.isNum(m.v))   // sin valor no hay cifra que rotular
       .sort((a, b) => a.y - b.y);
      // Sin separarlas, dos curvas cercanas dejan sus etiquetas una encima de
      // la otra y no se lee ninguna.
      for (let i = 1; i < marks.length; i++) {
        if (marks[i].y - marks[i - 1].y < 17) marks[i].y = marks[i - 1].y + 17;
      }
      ctx.save();
      ctx.font = '700 10px ui-monospace, monospace';
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      for (const m of marks) {
        const txt = Q.money(m.v, 1);
        const w = ctx.measureText(txt).width + 12;
        const y = Q.clamp(m.y, box.y + 9, box.y + box.h - 9);
        const x = box.x + box.w - 4;
        ctx.fillStyle = Q.alpha(m.col, 0.16);
        Q.roundRect(ctx, x - w, y - 8, w, 16, 4); ctx.fill();
        ctx.strokeStyle = Q.alpha(m.col, 0.85); ctx.lineWidth = 1;
        Q.roundRect(ctx, x - w, y - 8, w, 16, 4); ctx.stroke();
        ctx.fillStyle = m.col;
        ctx.fillText(txt, x - 6, y);
      }
      ctx.restore();
    }

    // El último bucket sigue ABIERTO: se marca hueco para que no se lea como un
    // valor consolidado. Es el único punto de la curva que aún puede cambiar.
    const last = vis[vis.length - 1];
    if (last && last.open) {
      for (const [key, col] of [['call', posC], ['put', negC]]) {
        if (!Q.isNum(last[key])) continue;
        ctx.beginPath(); ctx.arc(sx(last.t), sy(last[key]), 3.2, 0, Math.PI * 2);
        ctx.fillStyle = Q.token('--panel', '#0d131c'); ctx.fill();
        ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.stroke();
      }
    }

    /* Eje derecho: precio del subyacente, en el MISMO eje temporal.
     *
     * v1.51.0 · Aquí cuelgan ahora los MUROS y las MARCAS DE FLUJO.
     *
     * El eje izquierdo de este carril mide PRIMA ACUMULADA en dólares y Call Wall
     * es un PRECIO DE STRIKE. Colgarlos del eje izquierdo pintaría 534 dólares de
     * prima donde hay un muro en 534 de precio: dos magnitudes compartiendo una
     * regla, que es como se fabrica una lectura falsa.
     *
     * Y van en ESTE eje, no en uno nuevo: dos escalas de precio con dominios
     * distintos en el mismo gráfico es peor que no dibujar los muros, porque las
     * dos parecen válidas y sólo una sitúa bien la línea.
     */
    /* v1.56.0 · EL EJE DE PRECIO NO PUEDE DEPENDER DE UN CAMPO OPCIONAL.
     *
     * Este eje se construía SÓLO con `stockPrice` de los buckets de Net Drift.
     * Cuando el proveedor no publica ese campo —y no siempre lo publica— el
     * bloque entero se saltaba, y con él se iban el recorrido del precio, CALL
     * WALL, PUT WALL y las marcas de QFLOW. En pantalla parecía que los muros
     * «no existían» cuando lo que faltaba era una columna del otro dataset.
     *
     * El subyacente es el MISMO en las velas, que este panel ya comparte por el
     * TimeLink. Se usan como respaldo declarado: mismo activo, mismo reloj,
     * misma magnitud. Lo que no se hace nunca es abrir un segundo eje.
     */
    let px = vis.filter(v => Q.isNum(v.price));
    let priceSource = 'NET_DRIFT_STOCK_PRICE';
    if (px.length <= 1 && S.candles.length > 1) {
      px = S.candles.map(c => ({ t: Q.parseTime(c.t), price: Q.num(c.c, NaN) }))
        .filter(v => Q.isNum(v.t) && Q.isNum(v.price));
      priceSource = 'CANDLES_FALLBACK';
    }
    S.driftPriceSource = px.length > 1 ? priceSource : null;
    if (px.length > 1) {
      let plo = Infinity, phi = -Infinity;
      for (const v of px) { plo = Math.min(plo, v.price); phi = Math.max(phi, v.price); }
      // Los muros entran en el dominio: uno fuera del encuadre no se ve, y «no se
      // ve» y «no existe» se confunden. Se acota para que un nivel muy lejano no
      // aplaste el recorrido del precio contra una línea.
      const span0 = Math.max(phi - plo, 0.02);
      for (const l of (S.levels || [])) {
        const lp = Q.num(l && l.price, NaN);
        if (!Q.isNum(lp)) continue;
        if (lp < plo - span0 * 3 || lp > phi + span0 * 3) continue;
        plo = Math.min(plo, lp); phi = Math.max(phi, lp);
      }
      const ppad = Math.max((phi - plo) * 0.18, 0.02);
      const psy = Q.scale(plo - ppad, phi + ppad, box.y + box.h, box.y);
      const priceC = Q.token('--price', '#7aa2f7');
      ctx.beginPath();
      px.forEach((v, i) => { const x = sx(v.t), y = psy(v.price); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.strokeStyle = Q.alpha(priceC, 0.85); ctx.lineWidth = 1.2; ctx.stroke();

      // Muros y niveles, con la MISMA definición y la MISMA tipografía que TRACE
      // y la cinta: un Call Wall no puede verse de tres formas distintas.
      const wmarks = [];
      for (const l of (S.levels || [])) {
        const lp = Q.num(l && l.price, NaN);
        if (!Q.isNum(lp)) continue;
        const y = psy(lp);
        if (y < box.y || y > box.y + box.h) continue;
        const st = Q.levelStyle(l.kind);
        const col = Q.token(st.color, '#8494ad');
        Q.levelLine(ctx, box, y, Q.alpha(col, 0.8), { width: Q.LEVEL_LINE_WIDTH });
        wmarks.push({ y, col, txt: `${st.label || l.kind} ${lp.toFixed(2)}` });
      }
      if (wmarks.length) {
        ctx.save();
        ctx.font = Q.LEVEL_FONT;
        ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
        for (const m of Q.stackLabels(wmarks, Q.LEVEL_LABEL_GAP)) {
          const w = ctx.measureText(m.txt).width + 14;
          const y = Q.clamp(m.y, box.y + 10, box.y + box.h - 10);
          ctx.fillStyle = Q.alpha(m.col, 0.2);
          Q.roundRect(ctx, box.x + 4, y - Q.LEVEL_LABEL_H / 2, w, Q.LEVEL_LABEL_H, 4); ctx.fill();
          ctx.strokeStyle = Q.alpha(m.col, 0.9); ctx.lineWidth = 1;
          Q.roundRect(ctx, box.x + 4, y - Q.LEVEL_LABEL_H / 2, w, Q.LEVEL_LABEL_H, 4); ctx.stroke();
          ctx.fillStyle = m.col;
          ctx.fillText(m.txt, box.x + 11, y);
        }
        ctx.restore();
      }

      /* Marcas de flujo sobre el precio: círculo dorado donde ocurrió, flecha de
       * sentido e importe. Las MISMAS funciones que TRACE y la cinta — dibujarlas
       * aquí de otra forma haría que la misma marca significara dos cosas según
       * la pantalla. */
      const evs = (S.qflow && S.qflow.ready && Array.isArray(S.qflow.events)) ? S.qflow.events : [];
      if (evs.length) {
        // El pico del CICLO: con una constante, un día tranquilo saldría todo
        // diminuto y el tamaño dejaría de informar.
        const peak = evs.reduce((m, e) => Math.max(m, Math.abs(Q.num(e.premium, 0))), 0);
        const used = [];
        for (const ev of evs) {
          const t = Q.parseTime(ev.t);
          const pr = Q.num(ev.price, NaN);
          if (!Q.isNum(t) || !Q.isNum(pr)) continue;
          const x = sx(t);
          if (x < box.x || x > box.x + box.w) continue;
          const y = psy(pr);
          if (y < box.y - 6 || y > box.y + box.h + 6) continue;
          // La MISMA marca que dibuja TRACE, con la misma implementación.
          const conCifra = !used.some(u => Math.abs(u - x) < 58);
          if (conCifra) used.push(x);
          Q.flowMark(ctx, x, y, ev, peak, { label: conCifra ? markerLabel(ev) : '' });
        }
      }
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
    ctx.fillText('NET DRIFT · QUANT DATA (OFICIAL)'
      + (S.driftPriceSource === 'CANDLES_FALLBACK' ? ' · PRECIO DE VELAS' : ''),
      box.x + box.w - 6, box.y + 4);
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
  /* ── Carril TOTAL de Net Drift: prima por intervalo, con las puntas en oro ──
   *
   * La curva de arriba responde «¿hacia dónde va la sesión?». Este carril
   * responde «¿EN QUÉ MINUTO se pagó?», que es lo que se busca en cuanto la
   * curva cambia de pendiente de golpe.
   *
   * Marcar todos los intervalos sería no marcar ninguno: se marcan los que
   * destacan, con un criterio que se ELIGE y se declara —las tres mayores, o
   * las que superan diez veces la media—, y la referencia de esa media se
   * dibuja para que la comparación se vea.
   */
  function drawDriftTotal(ctx, env) {
    const box = driftBox(env);
    box.y = 12; box.h = env.h - 26;
    if (box.w <= 8 || box.h <= 8) return false;
    const rows = driftRows();
    const win = window_();
    if (!rows.length || !win) { empty(ctx, env, 'SIN PRIMA POR INTERVALO'); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    const vis = [];
    for (const p of rows) {
      const t = driftT(p);
      if (!Q.isNum(t) || t < win.t0 - S.bucketMs || t > win.t1) continue;
      // Prima del intervalo: lo que se pagó en ese minuto, no el acumulado.
      const v = Math.abs(Q.num(p.total_premium, NaN));
      const val = Q.isNum(v) ? v
        : Math.abs(Q.num(p.call_premium, 0)) + Math.abs(Q.num(p.put_premium, 0));
      if (!(val > 0)) continue;
      vis.push({ t, v: val, net: Q.num(p.net_premium, 0) });
    }
    if (!vis.length) { empty(ctx, env, 'SIN PRIMA POR INTERVALO'); return false; }

    const peak = Math.max.apply(null, vis.map(r => r.v));
    const mean = vis.reduce((a, r) => a + r.v, 0) / vis.length;
    const sy = Q.scale(0, peak * 1.22, box.y + box.h, box.y);

    // Qué se marca en oro. El criterio es explícito y cambiable.
    const gold = new Set();
    if (S.goldRule === 'x10') {
      for (const r of vis) if (mean > 0 && r.v >= mean * 10) gold.add(r.t);
    } else {
      vis.slice().sort((a, b) => b.v - a.v).slice(0, 3).forEach(r => gold.add(r.t));
    }

    const bins = AB.timeBins(vis, win.t0, win.t1 + S.bucketMs, S.bucketMs, box.w);
    const bw = bins.thickness;
    const dim = Q.token('--text-dim', '#8494ad');
    const goldC = Q.token('--gold', '#d9a441');

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y - 12, box.w, box.h + 24); ctx.clip();
    const labels = [];
    for (const r of bins.rows) {
      const agg = AB.reduceBin(r, p => Q.num(p.v, 0));
      if (!(agg.sum > 0)) continue;
      const isGold = r.members.some(p => gold.has(p.t));
      const x0 = sx(r.t), x1 = sx(r.tEnd);
      const x = x0 + (x1 - x0) / 2;
      const y = sy(agg.sum);
      const h = laneExtent((box.y + box.h) - y, agg.sum, box.h);
      ctx.fillStyle = isGold ? goldC : Q.alpha(dim, 0.5);
      ctx.fillRect(Math.round(x - bw / 2), Math.round(box.y + box.h - h),
                   Math.max(1, Math.round(bw)), Math.max(1, Math.round(h)));
      if (isGold) labels.push({ x, y, v: agg.sum });
    }

    // La media, como referencia de contra qué destacan.
    //
    // v1.57.0 · Su cifra iba en la cabecera, lejos de la línea que la
    // representa. Leer «cuánto destaca esta barra» obligaba a saltar de la
    // línea al encabezado y volver. La cifra va AHORA sobre la propia línea,
    // en su extremo, que es donde la vista ya está.
    if (mean > 0) {
      const my = Math.round(sy(mean)) + 0.5;
      const avgC = Q.token('--info-600', '#3b82f6');
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = Q.alpha(avgC, 0.85);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(box.x, my); ctx.lineTo(box.x + box.w, my); ctx.stroke();
      ctx.setLineDash([]);

      const txt = `Prom ${Q.money(mean, 0)}`;
      ctx.font = '700 9px ui-monospace, monospace';
      const pw = ctx.measureText(txt).width + 12;
      const py = Q.clamp(my, box.y + 7, box.y + box.h - 7);
      const px0 = box.x + box.w - pw - 2;
      ctx.fillStyle = Q.alpha(avgC, 0.9);
      Q.roundRect(ctx, px0, py - 7, pw, 14, 4); ctx.fill();
      ctx.fillStyle = Q.token('--panel', '#0d131c');
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(txt, px0 + pw / 2, py);
    }
    ctx.restore();

    ctx.save();
    ctx.font = '700 10px ui-monospace, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
    ctx.fillStyle = goldC;
    for (const l of labels) ctx.fillText(Q.money(l.v, 1), l.x, Math.max(box.y + 10, l.y - 4));
    ctx.restore();

    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.alpha(dim, 0.85);
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    laneTag(ctx, box, 'TOTAL · PRIMA POR INTERVALO', { dx: 4, y: 2 });
    ctx.restore();
    return false;
  }

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

    // Mismo tratamiento que los demás carriles: si un intervalo no da para una
    // barra con presencia, se agrupa el intervalo y se SUMAN los volúmenes.
    const driftBins = AB.timeBins(vis, win.t0, win.t1 + S.bucketMs, S.bucketMs, box.w);
    const bw = driftBins.thickness;
    const posC = Q.token('--pos', '#22c55e'), negC = Q.token('--neg', '#ef4444');
    // El volumen de puts llega YA firmado por el proveedor. No se le cambia el
    // signo: se dibuja donde cae, que es la lectura que publica Quant Data.
    for (const r of driftBins.rows) {
      const x0 = sx(r.t), x1 = sx(r.tEnd);
      const x = x0 + (x1 - x0) / 2;
      const cv = AB.reduceBin(r, p => Q.num(p.cv, 0)).sum;
      const pv = AB.reduceBin(r, p => Q.num(p.pv, 0)).sum;
      for (const [val, col] of [[cv, posC], [pv, negC]]) {
        if (!val) continue;
        const y0 = sy(0), y1 = sy(val);
        const h = laneExtent(y1 - y0, val, box.h);
        ctx.fillStyle = Q.alpha(col, 0.9);
        ctx.fillRect(Math.round(x - bw / 2), Math.round(val >= 0 ? y0 - h : y0),
          Math.max(1, Math.round(bw)), Math.max(1, Math.round(h)));
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
    laneTag(ctx, box, 'VOLUMEN NETO CALL / PUT · QUANT DATA', { dx: 6, y: 2 });
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
  /**
   * NOTIONAL / MIN · cuánto nocional se movió en cada minuto.
   *
   * v1.57.0 · Este carril faltaba. Net Drift publicaba la curva acumulada y la
   * prima por intervalo, pero no el RITMO: cuántos dólares por minuto está
   * absorbiendo el mercado ahora mismo.
   *
   * No es lo mismo que el carril TOTAL de arriba. TOTAL dibuja barras y sirve
   * para encontrar EL minuto en que se pagó algo grande. Éste va en línea
   * continua y sirve para ver el PULSO: si la sesión se está acelerando o
   * apagando. Una barra aislada y una meseta alta son lecturas distintas, y con
   * barras las dos se ven igual.
   *
   * Nocional del intervalo = |prima call| + |prima put|. Los dos lados suman en
   * valor absoluto a propósito: mide actividad, no dirección. La dirección ya la
   * dicen las curvas de arriba, y mezclarlas aquí restaría un lado del otro.
   */
  function drawDriftNotional(ctx, env) {
    const box = driftBox(env);
    box.y = 12; box.h = env.h - 26;
    if (box.w <= 8 || box.h <= 8) return false;
    const rows = driftRows();
    const win = window_();
    if (!rows.length || !win) { empty(ctx, env, 'SIN NOCIONAL POR MINUTO'); return false; }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    const vis = [];
    for (const p of rows) {
      const t = driftT(p);
      if (!Q.isNum(t) || t < win.t0 - S.bucketMs || t > win.t1) continue;
      const c = Q.num(p.call, NaN), pu = Q.num(p.put, NaN);
      // Un minuto SIN dato no es un minuto de cero nocional: se salta.
      if (!Q.isNum(c) && !Q.isNum(pu)) continue;
      vis.push({ t, v: Math.abs(Q.isNum(c) ? c : 0) + Math.abs(Q.isNum(pu) ? pu : 0) });
    }
    if (!vis.length) { empty(ctx, env, 'SIN NOCIONAL POR MINUTO'); return false; }

    const peak = Math.max.apply(null, vis.map(r => r.v));
    if (!(peak > 0)) { empty(ctx, env, 'SIN NOCIONAL POR MINUTO'); return false; }
    const sy = Q.scale(0, peak * 1.15, box.y + box.h, box.y);
    const col = Q.token('--info-600', '#3b82f6');

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y - 6, box.w, box.h + 12); ctx.clip();

    // Relleno tenue bajo la curva: da cuerpo al pulso sin taparlo.
    ctx.beginPath();
    ctx.moveTo(sx(vis[0].t), box.y + box.h);
    for (const r of vis) ctx.lineTo(sx(r.t), sy(r.v));
    ctx.lineTo(sx(vis[vis.length - 1].t), box.y + box.h);
    ctx.closePath();
    ctx.fillStyle = Q.alpha(col, 0.12); ctx.fill();

    ctx.beginPath();
    vis.forEach((r, i) => { const x = sx(r.t), y = sy(r.v); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.lineJoin = 'round'; ctx.stroke();
    ctx.restore();

    // Escala: sólo el máximo, que es la referencia que se busca aquí.
    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.alpha(Q.token('--text-dim', '#8494ad'), 0.85);
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    laneTag(ctx, box, 'NOTIONAL / MIN', { dx: 4, y: 2 });
    ctx.textAlign = 'right';
    ctx.fillText(Q.money(peak, 1), box.x + box.w - 4, box.y - 10);
    ctx.restore();
    return false;
  }

  /**
   * DELTA / MIN · presión delta del DEALER por minuto.
   *
   * Barra positiva = el dealer queda LARGO de delta y tiene que VENDER
   * subyacente para cubrirse. Negativa = queda CORTO y tiene que COMPRAR.
   *
   * El eje va en DÓLARES —delta-acciones × spot—, que es la magnitud con la
   * que se compara contra cualquier otra cifra de la pantalla. Las
   * delta-acciones, la cobertura y el método viajan en el modelo y se leen en
   * el Auditor: aquí el gráfico va limpio.
   *
   * Un minuto sin operaciones NO se dibuja. No es un minuto de cero presión.
   */
  function drawDeltaMin(ctx, env) {
    const box = driftBox(env);
    box.y = 12; box.h = env.h - 26;
    if (box.w <= 8 || box.h <= 8) return false;

    const d = S.deltaMin;
    const win = window_();
    if (!d || !d.ready || !Array.isArray(d.series) || !d.series.length || !win) {
      empty(ctx, env, (d && d.detail) ? String(d.detail).toUpperCase() : 'SIN PRESIÓN DELTA MEDIBLE');
      return false;
    }
    const sx = Q.scale(win.t0, win.t1, box.x, box.x + box.w);

    const vis = [];
    for (const p of d.series) {
      const t = Q.isNum(Q.num(p.timestamp_ms, NaN)) ? Q.num(p.timestamp_ms) : Q.parseTime(p.t);
      if (!Q.isNum(t) || t < win.t0 - S.bucketMs || t > win.t1) continue;
      // Sin dólares para ese minuto no se dibuja la barra: convertir con el
      // spot de otro instante sería inventar el precio.
      const v = Q.num(p.dealer_delta_dollars, NaN);
      if (!Q.isNum(v)) continue;
      vis.push({ t, v });
    }
    if (!vis.length) { empty(ctx, env, 'SIN PRESIÓN DELTA EN LA VENTANA'); return false; }

    let mag = 0;
    for (const r of vis) mag = Math.max(mag, Math.abs(r.v));
    if (!(mag > 0)) { empty(ctx, env, 'SIN PRESIÓN DELTA EN LA VENTANA'); return false; }
    // Eje simétrico: comprar y vender presión tienen que medirse con la misma
    // vara o una de las dos parecería mayor de lo que es.
    const sy = Q.scale(-mag * 1.15, mag * 1.15, box.y + box.h, box.y);
    const zero = Math.round(sy(0)) + 0.5;

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y - 6, box.w, box.h + 12); ctx.clip();

    const bins = AB.timeBins(vis, win.t0, win.t1 + S.bucketMs, S.bucketMs, box.w);
    const bw = bins.thickness;
    const posC = Q.token('--pos-600', '#22c55e');
    const negC = Q.token('--neg-600', '#ef4444');
    for (const r of bins.rows) {
      const agg = AB.reduceBin(r, p => Q.num(p.v, 0));
      if (!Q.isNum(agg.sum) || agg.sum === 0) continue;
      const x0 = sx(r.t), x1 = sx(r.tEnd);
      const x = x0 + (x1 - x0) / 2;
      const y = sy(agg.sum);
      const h = laneExtent(Math.abs(y - zero), agg.sum, box.h);
      ctx.fillStyle = agg.sum > 0 ? posC : negC;
      ctx.fillRect(Math.round(x - bw / 2), Math.round(agg.sum > 0 ? zero - h : zero),
                   Math.max(1, Math.round(bw)), Math.max(1, Math.round(h)));
    }

    ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.9); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(box.x, zero); ctx.lineTo(box.x + box.w, zero); ctx.stroke();
    ctx.restore();

    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.alpha(Q.token('--text-dim', '#8494ad'), 0.85);
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    laneTag(ctx, box, 'DELTA / MIN', { dx: 4, y: 2 });
    ctx.textAlign = 'right';
    ctx.fillText(`±${Q.money(mag, 1)}`, box.x + box.w - 4, box.y - 10);
    ctx.restore();
    return false;
  }

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
      set('ndState', estadoDato(d && d.state));
      set('ndStateDetail', (d && d.detail) ? String(d.detail) : 'sin deriva neta en este ciclo');
      return;
    }
    /* v1.51.0 · `Q.num(x, 0)` convertia un agregado AUSENTE en un cero duro, y
     * `Q.money(0)` lo escribia como «$0.0»: la afirmacion «hoy no se acumulo
     * prima call», que es falsa cuando lo que pasa es que el proveedor publico la
     * serie pero no sus totales. El formateador ya devuelve «—» ante un hueco;
     * lo que sobraba era el cero por defecto que se lo tapaba. */
    set('ndCall', Q.money(d.cum_call_premium, 1));
    set('ndPut', Q.money(d.cum_put_premium, 1));
    set('ndNet', Q.money(d.cum_net_premium, 1));
    set('ndVolume', `${Q.compact(d.cum_call_volume, 1)} / ${Q.compact(d.cum_put_volume, 1)}`);
    set('ndState', estadoDato(d.state));
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
    S.panels.driftTotal = ids.driftTotal
      ? new Q.Panel(ids.driftTotal, drawDriftTotal, driftOpts('flow:drifttotal')) : null;
    S.panels.driftNotional = ids.driftNotional
      ? new Q.Panel(ids.driftNotional, drawDriftNotional, driftOpts('flow:driftnotional')) : null;
    S.panels.deltaMin = ids.deltaMin
      ? new Q.Panel(ids.deltaMin, drawDeltaMin, driftOpts('flow:deltamin')) : null;
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
  /** DELTA / MIN del bundle. Autoridad propia: no se mezcla con Net Drift. */
  function applyDeltaMin(d) {
    S.deltaMin = (d && typeof d === 'object') ? d : null;
    if (S.panels.deltaMin) S.panels.deltaMin.invalidate();
  }

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
    /* v1.56.2 · LA UI NO LEE `option_prints` DEL TRACE.
     *
     * Punto 6 del cierre: «si esas estructuras todavía son necesarias para el
     * motor, pueden quedarse internamente, pero la UI no puede consumirlas».
     * Eso incluía este respaldo, que yo había dejado a propósito y que era
     * exactamente la UI consumiéndolas.
     *
     * Los prints salen ahora del carril `prints` del FlowViewModel, que es la
     * MISMA cinta de la que salen las tarjetas, las barras y QFLOW. Una sola
     * ruta: si el modelo no viaja, la sección lo dice en vez de rellenarse por
     * otro camino y parecer sana. */
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

  /**
   * v1.55.0 · Las tarjetas leen el FlowViewModel cuando existe.
   *
   * El defecto que esto elimina: la seccion tenia un estado GLOBAL, asi que un
   * ciclo sin prints la vaciaba entera y en pantalla convivian
   *
   *     ESTADO            DATO ANTIGUO · 405 buckets · ultimo hace 3610 min
   *     PRIMA TOTAL       SIN DATOS
   *
   * dos afirmaciones contrarias sobre el mismo dato. El modelo trae cada carril
   * con SU ultimo valor bueno y SU edad, asi que una tarjeta puede decir «$1,2M
   * · ultimo dato 15:42» mientras otra sigue en vivo. «No llego nada nuevo» y
   * «no hay nada» dejan de dibujarse igual.
   *
   * La cinta local sigue siendo el calculo cuando el modelo no viaja: no se
   * pierde nada si el backend es antiguo.
   */
  function renderSummaryFromModel(vm) {
    const set = (id, v) => { const x = document.getElementById(id); if (x) x.textContent = v; };
    const P = vm.premiums || {};
    // Un carril SIN_DATOS escribe SIN DATOS; uno viejo escribe su valor, porque
    // un valor de hace diez minutos sigue siendo informacion y un hueco no.
    const val = (lane) => {
      if (!lane) return 'SIN DATOS';
      if (lane.current == null) return 'SIN DATOS';
      return Q.money(lane.current, 1);
    };
    const buy = P.buy || null, sell = P.sell || null;
    set('ofTotalPremium', val(P.total));
    set('ofBuyPremium', val(buy));
    set('ofSellPremium', val(sell));
    const neto = (buy && buy.current != null) || (sell && sell.current != null)
      ? Q.money((buy && buy.current || 0) - (sell && sell.current || 0), 1) : 'SIN DATOS';
    set('ofNetPremium', neto);
    // La nota de cada tarjeta dice si lo que se ve es de ahora o de antes.
    const nota = (lane, porDefecto) => (lane && lane.screen_note) ? lane.screen_note : porDefecto;
    set('ofBuyNote', nota(buy, 'agresor en ask'));
    set('ofSellNote', nota(sell, 'agresor en bid'));

    const cov = Q.num(vm.aggressor_coverage_pct, NaN);
    const unk = P.unclassified && P.unclassified.current;
    const tape = vm.tape || {};
    const n = (tape.meta && tape.meta.count) || 0;
    set('ofPrintCount', n > 0
      ? `${n} prints${Q.isNum(cov) ? ` · ${cov.toFixed(0)}% con agresor` : ''}`
        + (unk ? ` · ${Q.money(unk, 1)} sin clasificar` : '')
        + (tape.screen_note ? ` · ${tape.screen_note}` : '')
      : (tape.screen_note || 'sin cinta observada'));

    const big = P.largest_print && P.largest_print.current;
    set('ofBiggest', big ? Q.money(Math.abs(Q.num(big.premium, 0)), 1) : '—');
    set('ofBiggestDetail', big
      ? `${Q.hhmm(Q.parseTime(big.t))} · ${String(big.option_type || '').toUpperCase()} ${Q.num(big.strike) || ''} · ${big.aggressor || ''}`
      : '—');

    // El sesgo se mide sobre la prima CLASIFICADA. Dividir por el total diluia
    // el sesgo hacia EQUILIBRADO justo cuando la cinta llega sin cotizacion.
    const b = (buy && buy.current) || 0, sl = (sell && sell.current) || 0;
    const clasificada = b + sl;
    const bias = clasificada > 0 ? (b - sl) / clasificada : 0;
    set('ofBias', clasificada <= 0
      ? ((P.total && P.total.current) ? 'SIN CLASIFICAR' : '—')
      : (bias > 0.12 ? 'COMPRADOR' : bias < -0.12 ? 'VENDEDOR' : 'EQUILIBRADO'));
  }

  function applyFlowView(vm) {
    S.flowView = (vm && typeof vm === 'object' && vm.premiums) ? vm : null;
    // ÚNICA fuente de los prints de la sección. Con LKG: un ciclo vacío no los
    // borra, igual que no borra las barras.
    const lane = S.flowView && S.flowView.prints;
    S.prints = (lane && Array.isArray(lane.current)) ? lane.current : [];
    // Las barras de opciones salen del modelo, así que al llegar hay que
    // reconstruirlas: si sólo se repintaran las tarjetas, los carriles de
    // AGRESOR y PRIMA seguirían enseñando lo que hubiera calculado la cinta
    // local, que es justo la ruta que se está sustituyendo.
    rebuild();
    renderSummary();
    invalidateAll();
    return S.flowView;
  }

  /** Cuántas barras dibuja de verdad este cliente. Cierra el recuento de la
   *  cadena que el modelo publica en `pipeline`: si el proveedor trae filas y
   *  aquí salen cero, el fallo es de integración y se puede señalar. */
  function renderedBarCount() {
    return S.buckets.filter(b => (Q.num(b.total, 0) > 0)).length;
  }

  /**
   * v1.56.2 · UNA sola ruta. La rama que recalculaba las tarjetas desde la
   * cinta local desaparece.
   *
   * Existía como respaldo por si el modelo no viajaba, y era justo lo que el
   * punto 6 prohíbe: dos fuentes para la misma pantalla. Peor aún, el respaldo
   * hacía que un bundle roto se viera SANO —las tarjetas se rellenaban por el
   * otro camino— y un fallo que se disimula solo es un fallo que nadie arregla.
   *
   * Sin modelo, la sección lo dice.
   */
  function renderSummary() {
    if (S.flowView) { renderSummaryFromModel(S.flowView); return; }
    const set = (id, v) => { const x = document.getElementById(id); if (x) x.textContent = v; };
    for (const id of ['ofTotalPremium', 'ofBuyPremium', 'ofSellPremium', 'ofNetPremium'])
      set(id, 'SIN DATOS');
    set('ofPrintCount', 'el modelo de flujo no llegó en este ciclo');
    set('ofBuyNote', 'agresor en ask');
    set('ofSellNote', 'agresor en bid');
    set('ofBiggest', '—');
    set('ofBiggestDetail', '—');
    set('ofBias', '—');
  }

  function setMinPremium(v) { S.minPremium = Math.max(0, Q.num(v, 0)); rebuild(); invalidateAll(); }
  function setMode(m) { S.mode = m === 'line' ? 'line' : 'area'; invalidateAll(); }
  function setFollow(on) {
    S.link.follow = !!on;
    // Sin `option_prints`: los prints son del modelo y no se reinyectan por la
    // puerta de atrás al reencuadrar.
    if (on) applyTrace({ symbol: S.symbol, candles: S.candles, levels: S.levels,
                         bar_interval_ms: S.bucketMs });
  }

  /** Criterio de marcado en oro del carril TOTAL. */
  function setGoldRule(rule) {
    S.goldRule = rule === 'x10' ? 'x10' : 'top3';
    for (const p of Object.values(S.panels)) if (p) p.invalidate();
  }

  global.ITMQFlow = { mount, applyTrace, applyQflow, applyNetDrift, applyDeltaMin, applyFlowView, renderedBarCount, setPanel, setGoldRule,
    setMinPremium, setMode, setFollow, state: S };
})(window);
