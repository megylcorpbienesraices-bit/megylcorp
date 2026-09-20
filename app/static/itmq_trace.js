/* ITM QUANT · TRACE v1.41.0 — tres paneles con eje de precio compartido
 *
 *   [ perfil izquierdo ]  [ heatmap + precio + niveles ]  [ perfil derecho ]
 *
 * Los tres paneles comparten el mismo eje vertical de precio/strike, así que una
 * barra de la izquierda está siempre a la altura exacta del strike al que
 * pertenece en el gráfico central.
 *
 * Todo lo que se dibuja viene de /api/nextgen/trace. Este archivo no calcula
 * exposición ni dirección: sólo presenta lo que el motor ya resolvió.
 */
(function (global) {
  'use strict';

  const Q = global.ITMQ;
  if (!Q) { console.error('[TRACE] falta itmq_core.js'); return; }

  /* ---------------------------------------------------------- métricas */

  // value(row) devuelve unidades absolutas (no millones) para poder formatear en B/M.
  const METRICS = {
    GEX: { label: 'GEX', value: r => Q.num(r.gamma_m) * 1e6, fmt: v => Q.signedCompact(v, 2),
           parts: r => ({ call: Q.num(r.call_gamma_m) * 1e6, put: Q.num(r.put_gamma_m) * 1e6 }),
           help: 'Exposición gamma por strike' },
    DEX: { label: 'DEX', value: r => Q.num(r.delta_m) * 1e6, fmt: v => Q.signedCompact(v, 2),
           parts: r => ({ call: Q.num(r.call_delta_m) * 1e6, put: Q.num(r.put_delta_m) * 1e6 }),
           help: 'Exposición delta por strike' },
    VEX: { label: 'VEX', value: r => Q.num(r.vanna_1vol_m) * 1e6, fmt: v => Q.signedCompact(v, 2), help: 'Exposición vanna por strike' },
    CHEX: { label: 'CHEX', value: r => Q.num(r.charm_10m_m) * 1e6, fmt: v => Q.signedCompact(v, 2), help: 'Exposición charm por strike' },
    OI: { label: 'OI', value: r => Q.num(r.oi), fmt: v => Q.compact(v, 1), signed: false,
          parts: r => ({ call: Q.num(r.call_oi), put: -Q.num(r.put_oi) }),
          help: 'Interés abierto total' },
    NET_OI: { label: 'NET OI', value: r => Q.num(r.net_oi), fmt: v => Q.signedCompact(v, 1), help: 'Interés abierto call − put' },
    VOLUME: { label: 'VOLUMEN', value: r => Q.num(r.volume_snapshot), fmt: v => Q.compact(v, 1), signed: false,
              parts: r => ({ call: Q.num(r.call_volume), put: -Q.num(r.put_volume) }),
              help: 'Volumen de opciones del snapshot' },
    NET_VOLUME: { label: 'NET VOL', value: r => Q.num(r.net_volume), fmt: v => Q.signedCompact(v, 1), help: 'Volumen call − put' },
    FLOW: { label: 'FLUJO 5M', value: r => Q.num(r.opra_directional_premium_5m), fmt: v => Q.money(v, 1), help: 'Prima direccional observada en 5 minutos' },
    GRAVITY: { label: 'GRAVEDAD', value: r => Q.num(r.gravity_score), fmt: v => v.toFixed(0), signed: false, help: 'Relevancia estructural del strike' },
    // La liquidez de la zona no cambia CUÁNTA gamma hay en un strike: cambia cuánto
    // pesa ese strike. Por eso entra como métrica propia y en GRAVEDAD, no dentro
    // del GEX. Interpolar OI entre snapshots sería inventar contratos.
    LIQUIDITY: { label: 'LIQUIDEZ', value: r => Q.num(r.liquidity_score), fmt: v => v.toFixed(0), signed: false,
                 help: 'Liquidez observada de la zona: OI, volumen y contratos OPRA de 5 min' },
    // Cuánto se ha movido la exposición desde el último snapshot de cadena por la
    // revalorización contra el precio en vivo. Es la prueba visible de que GEX y
    // DEX se mueven con el mercado y no sólo al refrescar la cadena.
    GEX_LIVE: { label: 'Δ GEX VIVO', value: r => Q.num(r.gamma_change_m) * 1e6, fmt: v => Q.signedCompact(v, 2),
                help: 'Movimiento del GEX desde el snapshot, por revalorización contra el precio en vivo' },
    DEX_LIVE: { label: 'Δ DEX VIVO', value: r => Q.num(r.delta_change_m) * 1e6, fmt: v => Q.signedCompact(v, 2),
                help: 'Movimiento del DEX desde el snapshot, por revalorización contra el precio en vivo' },
  };

  /* Campos del fondo dinámico.
   *
   * v1.43.0 · Los cuatro primeros vienen del INTERVAL MAP de Quant Data
   * (`interval_maps`): eje X tiempo, eje Y strike, intensidad = magnitud de
   * exposición. No es un heat map estático de fondo: cada columna es un intervalo
   * real de la sesión, así que se ve cómo la exposición aparece, crece, se reduce
   * y MIGRA entre strikes durante el día.
   *
   * Los campos `engine:` siguen saliendo de `heatmap_history` del motor, que es lo
   * único que sabe de OI neto y volumen neto por intervalo: son preguntas que el
   * Interval Map no responde, no un duplicado de las griegas.
   */
  const HEATFIELDS = {
    gamma: { greek: 'GAMMA', label: 'GAMMA' },
    delta: { greek: 'DELTA', label: 'DELTA' },
    vanna: { greek: 'VANNA', label: 'VANNA' },
    charm: { greek: 'CHARM', label: 'CHARM' },
    joint: { key: 'joint_coherence', label: 'Γ + Δ', engine: true },
    net_oi: { key: 'net_oi_intensity', label: 'OI NETO', engine: true },
    net_volume: { key: 'net_volume_intensity', label: 'VOLUMEN NETO', engine: true },
  };

  // Griegas del Interval Map, en el orden en que se alternan en la interfaz.
  const INTERVAL_GREEKS = ['GAMMA', 'DELTA', 'VANNA', 'CHARM'];

  /* Ajuste del campo de fondo. Son propiedades de LECTURA, no de activo: el mismo
   * suavizado y las mismas isolíneas valen para un ETF de 40 y para un índice de
   * 5.800, porque la normalización ya es relativa al propio activo. */
  const HEAT_BLUR = 1.9;              // celdas · manchas grandes, no granuladas
  const HEAT_LEVELS = [0.68, 0.80, 0.90, 0.96];
  const HEAT_ISO_ALPHA = 0.30;

  // Los estilos de nivel viven en el núcleo para que TRACE y el panel de flujo
  // dibujen el mismo precio con el mismo color y el mismo nombre.
  const LEVEL_STYLE = Q.LEVELS;

  /* ------------------------------------------------------------ estado */

  const S = {
    data: null,
    heat: null,            // bitmap precalculado
    heatKey: '',
    rows: [],
    strikes: [],
    spot: new Q.GlideValue(150),
    priceLo: new Q.GlideValue(220),
    priceHi: new Q.GlideValue(220),
    left: { metric: 'DEX', glide: new Q.Glide(140), max: new Q.GlideValue(260),
            call: new Q.Glide(140), put: new Q.Glide(140) },
    right: { metric: 'GEX', glide: new Q.Glide(140), max: new Q.GlideValue(260),
             call: new Q.Glide(140), put: new Q.Glide(140) },
    breakdown: false,        // NETO (false) · CALL+PUT (true)
    heatField: 'gamma',
    heatReason: '',
    heatOpacity: 0.55,
    perspective: 'MM',
    expiry: 'ALL',
    timeframe: '1m',
    windowMin: 390,
    hoverStrike: NaN,
    // Strike fijado con un clic: sobrevive a soltar el ratón, que es lo que permite
    // leer sus números sin que desaparezcan.
    pinnedStrike: NaN,
    onStrike: null,
    hoverTime: NaN,
    link: new Q.TimeLink(),
    panels: {},
    priceMode: 'candles',
    showLevels: true,
    showPrints: true,
    showQflow: true,
    showGammaMigration: true,
    qflowHits: [],
  };

  const GUT = { left: 48, right: 48 };     // carriles de etiquetas de precio
  const PAD = { top: 12, bottom: 26 };

  /* ------------------------------------------------- dominio del eje Y */

  function computePriceDomain() {
    const d = S.data || {};
    const vals = [];
    for (const c of d.candles || []) { vals.push(Q.num(c.h), Q.num(c.l)); }
    const spot = Q.num((d.profiles || {}).spot, NaN);
    if (Q.isNum(spot)) vals.push(spot);
    // Los strikes con exposición relevante tienen que caber en pantalla; si no,
    // el perfil lateral mostraría barras sin su strike visible.
    const rows = S.rows;
    if (rows.length) {
      const m = METRICS[S.left.metric], m2 = METRICS[S.right.metric];
      let peak = 0;
      for (const r of rows) peak = Math.max(peak, Math.abs(m.value(r)), Math.abs(m2.value(r)));
      for (const r of rows) {
        if (Math.abs(m.value(r)) > peak * 0.12 || Math.abs(m2.value(r)) > peak * 0.12) vals.push(Q.num(r.strike));
      }
    }
    const fin = vals.filter(Q.isNum);
    if (!fin.length) return null;
    let lo = Math.min.apply(null, fin), hi = Math.max.apply(null, fin);

    // La escalera de strikes puede extenderse mucho más que el recorrido del precio.
    // Sin límite, la vela queda comprimida en unos pocos píxeles; con un límite
    // demasiado estrecho desaparecen los strikes del perfil lateral. Se acota la
    // ventana a lo que sea mayor entre el recorrido del precio y siete escalones de
    // strike, de forma que ambas lecturas caben en cualquier régimen.
    const pv = [];
    for (const c of d.candles || []) { pv.push(Q.num(c.h), Q.num(c.l)); }
    const pf = pv.filter(Q.isNum);
    const anchor = Q.isNum(spot) ? spot : (pf.length ? (Math.min.apply(null, pf) + Math.max.apply(null, pf)) / 2 : NaN);
    if (Q.isNum(anchor)) {
      const pSpan = pf.length ? Math.max(Math.max.apply(null, pf) - Math.min.apply(null, pf), 0) : 0;
      const step = strikeStep();
      const half = Math.max(pSpan * 1.6, step * 7, Math.abs(anchor) * 0.0015);
      lo = Math.max(lo, anchor - half);
      hi = Math.min(hi, anchor + half);
    }
    const pad = Math.max((hi - lo) * 0.08, Math.abs(hi) * 0.0006, 0.05);
    return { lo: lo - pad, hi: hi + pad };
  }

  /** ¿El eje de precio compartido sigue en transición? No lo hace avanzar. */
  function priceAxisSettling() {
    for (const g of [S.priceLo, S.priceHi, S.spot]) {
      if (Q.isNum(g.tgt) && Q.isNum(g.cur) && Math.abs(g.cur - g.tgt) > 1e-7) return true;
    }
    return false;
  }

  function priceScale(box) {
    const lo = S.priceLo.get(), hi = S.priceHi.get();
    if (!Q.isNum(lo) || !Q.isNum(hi) || hi <= lo) return null;
    return Q.scale(lo, hi, box.y + box.h, box.y);
  }

  function priceTicks() {
    const lo = S.priceLo.get(), hi = S.priceHi.get();
    if (!Q.isNum(lo) || !Q.isNum(hi)) return [];
    return Q.niceTicks(lo, hi, 12);
  }

  /* --------------------------------------------------- heatmap bitmap */

  /**
   * El heatmap se rasteriza una sola vez por snapshot en un canvas del tamaño
   * exacto de la matriz. Al pintarlo escalado con suavizado se obtienen las
   * manchas continuas, y el coste por frame es un único drawImage.
   */
  /** De dónde sale la matriz del fondo: Interval Map del proveedor o motor. */
  /* v1.52.0 · El motor tiene intensidad propia para algunas griegas. Se usa como
   * RESPALDO DECLARADO cuando el proveedor no sirve ese mapa para el activo, en
   * vez de dejar el fondo en blanco sin decir nada. VANNA no tiene respaldo: el
   * motor no la calcula, y eso se dice con esas palabras. */
  const ENGINE_FALLBACK = {
    GAMMA: 'gamma_intensity',
    DELTA: 'delta_intensity',
    CHARM: 'charm_intensity',
  };

  function heatSource() {
    const d = S.data || {};
    const field = HEATFIELDS[S.heatField];
    if (!field) { S.heatReason = ''; return null; }

    const h = d.heatmap_history;
    const engineMatrix = key => {
      if (!h || h.ready !== true) return null;
      const m = h[key];
      return (Array.isArray(m) && m.length) ? m : null;
    };

    if (field.greek) {
      const maps = d.interval_maps || {};
      const im = maps[field.greek];
      if (im && im.ready === true && Array.isArray(im.intensity) && im.intensity.length
          && (im.strikes || []).length && (im.times || []).length) {
        S.heatReason = '';
        return {
          matrix: im.intensity, strikes: im.strikes, times: im.times,
          // `source` y `source_mode` viajan para el HUD; no se pinta el endpoint.
          origin: im.source || 'QUANTDATA', mode: im.source_mode || 'DIRECT_PROVIDER',
          stamp: (im.times || []).slice(-1)[0] || '',
        };
      }
      // Respaldo del motor, DECLARADO. Antes esto era un `return null` mudo y el
      // fondo se quedaba en blanco sin que nada explicara por qué.
      const fb = ENGINE_FALLBACK[field.greek];
      const m = fb && engineMatrix(fb);
      if (m) {
        S.heatReason = '';
        return {
          matrix: m, strikes: h.strikes || [], times: h.times || [],
          origin: 'ITM_QUANT', mode: 'FALLBACK', stamp: h.incremental_key || '',
        };
      }
      S.heatReason = (im && im.reason)
        ? `${field.label}: ${im.reason}`
        : (fb ? `${field.label}: sin mapa del proveedor y sin historia propia suficiente`
              : `${field.label}: el proveedor no sirve este mapa para este activo y el motor no lo calcula`);
      return null;
    }

    const m = engineMatrix(field.key);
    if (!m) {
      S.heatReason = (!h || h.ready !== true)
        ? `${field.label}: ${(h && h.reason) || 'sin historia estructural todavía'}`
        : `${field.label}: la historia estructural no incluye esta capa`;
      return null;
    }
    S.heatReason = '';
    return {
      matrix: m, strikes: h.strikes || [], times: h.times || [],
      origin: 'ITM_QUANT', mode: 'DERIVED', stamp: h.incremental_key || '',
    };
  }

  /* Margen alrededor de la ventana visible al recortar el campo, como fracción
   * de esa ventana. Sin margen, la zona del borde se queda sin vecinas con las
   * que suavizar y el campo se corta en seco justo donde se está mirando. */
  const HEAT_MARGIN = 0.35;

  function buildHeatBitmap() {
    const src = heatSource();
    if (!src) { S.heat = null; S.heatKey = ''; return; }
    let m = src.matrix;
    let strikes = src.strikes;
    const times = src.times;
    if (!strikes.length || !times.length) { S.heat = null; return; }

    /* v1.52.1 · RECORTE AL RANGO VISIBLE. Ésta es la causa de que el mapa
     * saliera como una masa maciza en QQQ, SPY y el resto.
     *
     * El Interval Map del proveedor cubre TODO el libro: noventa strikes que en
     * QQQ van de 454 a 547. La ventana de precio de TRACE son unos pocos
     * dólares alrededor del spot: 708–736. Es decir, lo que se ve en pantalla es
     * una franja estrecha de una matriz muchísimo más ancha.
     *
     * La normalización es por rango-percentil sobre TODA la matriz, así que el
     * percentil de una celda se calcula contra strikes que ni siquiera están en
     * pantalla. Los strikes muy lejanos concentran la exposición extrema, de
     * modo que las celdas visibles caían todas en el mismo tramo del percentil:
     * arriba nada, abajo todo saturado. Con más datos, MENOS contraste — que es
     * lo contrario de lo que debería pasar.
     *
     * Recortar primero y normalizar después hace que el percentil se calcule
     * entre lo que se está mirando, que es contra lo que el ojo compara.
     */
    const vLo = Q.num(S.priceLo.get(), NaN), vHi = Q.num(S.priceHi.get(), NaN);
    let clipped = 0;
    if (Q.isNum(vLo) && Q.isNum(vHi) && vHi > vLo) {
      const pad = (vHi - vLo) * HEAT_MARGIN;
      const lo = vLo - pad, hi = vHi + pad;
      const keep = [];
      for (let i = 0; i < strikes.length; i++) {
        const k = Q.num(strikes[i], NaN);
        if (Q.isNum(k) && k >= lo && k <= hi) keep.push(i);
      }
      // Con menos de cuatro filas dentro no hay campo que suavizar: se deja la
      // matriz entera antes que dibujar tres franjas interpoladas.
      if (keep.length >= 4 && keep.length < strikes.length) {
        const rowsAre = m.length === strikes.length;
        clipped = strikes.length - keep.length;
        if (rowsAre) {
          m = keep.map(i => m[i]);
        } else {
          m = m.map(col => keep.map(i => (col || [])[i]));
        }
        strikes = keep.map(i => strikes[i]);
      }
    }

    const key = [S.heatField, src.origin, src.stamp, times.length, strikes.length,
                 Q.isNum(vLo) ? vLo.toFixed(2) : '', Q.isNum(vHi) ? vHi.toFixed(2) : ''].join('|');
    if (key === S.heatKey && S.heat) return;
    S.heatKey = key;
    S.heatClipped = clipped;

    // Matriz esperada: [strike][time] o [time][strike]. Se detecta por dimensiones.
    const rowsAreStrikes = m.length === strikes.length;
    const W = times.length, H = strikes.length;
    const cv = document.createElement('canvas');
    cv.width = W; cv.height = H;
    const ictx = cv.getContext('2d');
    const img = ictx.createImageData(W, H);

    const pos = hexRGB(Q.token('--heat-pos', '#22c55e'));
    const neg = hexRGB(Q.token('--heat-neg', '#ef4444'));

    /* v1.48.0 · EL MISMO campo que el Interval Map de la sección.
     *
     * TRACE tenía aquí su propio tratamiento —umbral y curva propios— así que
     * el mapa de fondo de TRACE y el INTERVAL MAP de la sección se veían
     * distintos con el MISMO dato. Dos tratamientos del mismo dato es lo que
     * hace que dos pantallas del mismo programa no se parezcan, y obliga a
     * corregir dos veces cada ajuste visual.
     *
     * Ahora los dos pasan por `ITMQBars.field`: huecos rellenados desde sus
     * vecinas, suavizado en celdas y normalización por rango. El resultado es
     * una superficie continua con zonas, no una rejilla de celdas sueltas.
     */
    const AB = window.ITMQBars;
    // La matriz que espera el campo es [strike][tiempo]; si llega traspuesta se
    // endereza antes, no dentro del bucle de píxeles.
    const upright = rowsAreStrikes ? m
      : Array.from({ length: H }, (_, si) => Array.from({ length: W }, (_, xi) => (m[xi] || [])[si]));
    const f = AB ? AB.field(upright, { blur: HEAT_BLUR }) : null;

    /* v1.51.0 · El mapa salía como un BLOQUE saturado.
     *
     * La normalización es por rango-percentil, así que la celda mediana vale
     * siempre 0.5 exacto y la mitad superior del panel se iba al tope de opacidad
     * pasara lo que pasara. Con un suelo de 0.55 y una curva de 1.9 el resultado
     * era verde macizo arriba, rojo macizo abajo y nada legible en medio.
     *
     * Tres cambios, y los tres importan:
     *
     *   NOISE sube a 0.62 → banda NEUTRA ancha alrededor del cero. La zona donde
     *         no pasa nada tiene que verse vacía, no teñida.
     *   ALPHA_MAX 0.70    → el color nunca llega a opaco. Un campo pastel deja
     *         ver las velas y las isolíneas por encima; uno opaco las tapa, y
     *         entonces el mapa compite con el precio en vez de acompañarlo.
     *   GAMMA baja a 1.35 → con el tope puesto, una curva dura ya no hace falta y
     *         sólo servía para aplanar todo el rango medio contra el techo.
     */
    const NOISE = 0.62, GAMMA = 1.35, ALPHA_MAX = 0.70;
    for (let yi = 0; yi < H; yi++) {
      // fila 0 del bitmap = strike más alto (el eje de precio crece hacia arriba)
      const si = H - 1 - yi;
      for (let xi = 0; xi < W; xi++) {
        const v = f ? f.values[si * W + xi] : Q.num((upright[si] || [])[xi], 0);
        const c = v >= 0 ? pos : neg;
        const o = (yi * W + xi) * 4;
        const rank = f ? f.intensity(v) : Math.min(1, Math.abs(v));
        const a = (rank - NOISE) / (1 - NOISE);
        img.data[o] = c[0]; img.data[o + 1] = c[1]; img.data[o + 2] = c[2];
        img.data[o + 3] = a <= 0 ? 0
          : Math.round(255 * ALPHA_MAX * Math.min(1, Math.pow(a, GAMMA)));
      }
    }
    ictx.putImageData(img, 0, 0);
    // Isolíneas: el mismo cálculo que usa el mapa de la sección. Se guardan en
    // coordenadas de celda y se escalan al dibujar, así el grosor de la línea no
    // depende de cuántas celdas tenga la matriz.
    const iso = [];
    if (f && AB.contours) {
      for (const level of HEAT_LEVELS) {
        iso.push({ level, segs: AB.contours(f, level) });
      }
    }
    S.heat = { canvas: cv, times, strikes, origin: src.origin, mode: src.mode, iso,
               cols: W, rows: H,
               t0: Q.parseTime(times[0]), t1: Q.parseTime(times[times.length - 1]) };
  }

  function hexRGB(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
    if (!m) return [148, 163, 184];
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  /* ------------------------------------------------- panel de perfiles */

  function drawProfile(ctx, env, side) {
    const cfg = S[side];
    const box = {
      x: side === 'left' ? GUT.left : 0,
      y: PAD.top,
      w: env.w - GUT.left,
      h: env.h - PAD.top - PAD.bottom,
    };
    if (side === 'right') { box.x = 0; box.w = env.w - GUT.right; }
    if (box.w <= 4 || box.h <= 4) return false;

    const sy = priceScale(box);
    if (!sy) { emptyPanel(ctx, env, 'SIN ESTRUCTURA'); return false; }

    const metric = METRICS[cfg.metric] || METRICS.GEX;
    const signed = metric.signed !== false;

    // Escala X del perfil: se desliza hacia el nuevo máximo para que el eje no salte.
    let peak = 0;
    const parts = S.breakdown && typeof metric.parts === 'function' ? metric.parts : null;
    for (const r of S.rows) {
      peak = Math.max(peak, Math.abs(metric.value(r)));
      if (parts) {
        const p = parts(r);
        peak = Math.max(peak, Math.abs(Q.num(p.call)), Math.abs(Q.num(p.put)));
      }
    }
    // v1.46.0 · El pico sin dato es 0, no 1: un dólar es una escala arbitraria
    // que sólo pasa desapercibida en activos grandes.
    cfg.max.set(peak > 0 ? peak : 0);
    const mx = Math.max(Q.num(cfg.max.get(), 0), 1e-9);

    // El eje cero va al centro para métricas con signo y al borde interior si no.
    const zeroX = signed ? box.x + box.w * 0.5 : (side === 'left' ? box.x + box.w : box.x);
    const half = signed ? box.w * 0.5 : box.w;
    const sx = v => zeroX + (side === 'left' && !signed ? -1 : 1) * (v / mx) * half * 0.92;

    // Rejilla de precio + etiquetas en el borde exterior.
    const ticks = priceTicks();
    Q.gridY(ctx, box, sy, ticks, t => t.toFixed(t >= 1000 ? 0 : 2), {
      labelSide: side === 'left' ? 'left' : 'right',
    });
    if (side === 'right') {
      // segundo carril de etiquetas: extremo derecho de la terminal
      ctx.save();
      ctx.font = '10px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      for (const t of ticks) ctx.fillText(t.toFixed(t >= 1000 ? 0 : 2), box.x + box.w + 6, sy(t));
      ctx.restore();
    }

    // Eje cero
    ctx.save();
    ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 1);
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(zeroX) + 0.5, box.y); ctx.lineTo(Math.round(zeroX) + 0.5, box.y + box.h); ctx.stroke();
    ctx.restore();

    // Barras por strike. La altura se deriva del espaciado real entre strikes
    // para que no se solapen ni dejen huecos al cambiar el zoom vertical.
    const step = strikeStep();
    const barH = Math.max(2, Math.min(18, Math.abs(sy(0) - sy(step)) * 0.74));
    const posC = Q.token('--pos', '#22c55e');
    const negC = Q.token('--neg', '#ef4444');

    const showParts = S.breakdown && hasBreakdown(side);
    const callC = Q.token('--call', '#2dd4bf');
    const putC = Q.token('--put', '#f472b6');

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();
    for (const r of S.rows) {
      const k = Q.num(r.strike);
      const y = sy(k);
      if (y < box.y - barH || y > box.y + box.h + barH) continue;
      const hovered = Q.isNum(S.hoverStrike) && Math.abs(S.hoverStrike - k) < step * 0.5;

      if (showParts) {
        // Dos barritas por strike: arriba lo que aportan las calls, abajo lo que
        // aportan las puts. Juntas suman el neto, pero se ve de qué está hecho.
        const half = Math.max(1, barH / 2 - 0.5);
        for (const [val, col, dy] of [[cfg.call.get(k), callC, -half / 2 - 0.5],
                                      [cfg.put.get(k), putC, half / 2 + 0.5]]) {
          if (Math.abs(val) < mx * 0.0015) continue;
          const x = Q.clamp(sx(val), box.x, box.x + box.w);
          ctx.fillStyle = Q.alpha(col, hovered ? 1 : 0.88);
          ctx.fillRect(Math.min(zeroX, x), y + dy - half / 2, Math.max(1, Math.abs(x - zeroX)), half);
        }
        if (hovered) {
          ctx.strokeStyle = Q.alpha(Q.token('--text', '#e6edf7'), 0.7);
          ctx.lineWidth = 1;
          ctx.strokeRect(box.x + 0.5, Math.round(y - barH / 2) + 0.5, box.w - 1, barH);
        }
        continue;
      }

      const v = cfg.glide.get(k);
      if (Math.abs(v) < mx * 0.0015) continue;
      const x = Q.clamp(sx(v), box.x, box.x + box.w);
      ctx.fillStyle = Q.alpha(v >= 0 ? posC : negC, hovered ? 1 : 0.86);
      const x0 = Math.min(zeroX, x), w = Math.abs(x - zeroX);
      ctx.fillRect(x0, y - barH / 2, Math.max(1, w), barH);
      if (hovered) {
        ctx.strokeStyle = Q.token('--text', '#e6edf7');
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.round(x0) + 0.5, Math.round(y - barH / 2) + 0.5, Math.max(1, w), barH);
      }
    }
    ctx.restore();

    if (showParts) {
      ctx.save();
      ctx.font = '9px ui-monospace, monospace';
      ctx.textBaseline = 'top';
      ctx.textAlign = side === 'left' ? 'left' : 'right';
      const lx = side === 'left' ? box.x + 3 : box.x + box.w - 3;
      ctx.fillStyle = callC; ctx.fillText('CALL', lx, box.y + 2);
      ctx.fillStyle = putC; ctx.fillText('PUT', lx, box.y + 12);
      ctx.restore();
    }

    // Línea del spot para anclar visualmente los tres paneles.
    const spot = S.spot.get();
    if (Q.isNum(spot)) {
      Q.levelLine(ctx, box, sy(spot), Q.alpha(Q.token('--text-dim', '#8494ad'), 0.55), { dash: [2, 3] });
    }

    // Eje X inferior con los topes de magnitud.
    ctx.save();
    ctx.font = '9px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textBaseline = 'top';
    const ay = box.y + box.h + 6;
    if (signed) {
      ctx.textAlign = 'left'; ctx.fillText(metric.fmt(-mx), box.x + 2, ay);
      ctx.textAlign = 'center'; ctx.fillText('0', zeroX, ay);
      ctx.textAlign = 'right'; ctx.fillText(metric.fmt(mx), box.x + box.w - 2, ay);
    } else {
      ctx.textAlign = side === 'left' ? 'left' : 'right';
      ctx.fillText(metric.fmt(mx), side === 'left' ? box.x + 2 : box.x + box.w - 2, ay);
    }
    ctx.restore();

    const gliding = cfg.glide.step(env.dt);
    const calling = cfg.call.step(env.dt);
    const putting = cfg.put.step(env.dt);
    const scaling = cfg.max.step(env.dt);
    return gliding || calling || putting || scaling || priceAxisSettling();
  }

  /** Carga en el panel la métrica activa y, si existe, su desglose call/put. */
  function applyMetric(side) {
    const cfg = S[side];
    const m = METRICS[cfg.metric] || METRICS.GEX;
    cfg.glide.setAll(S.rows.map(r => [Q.num(r.strike), m.value(r)]));
    if (typeof m.parts === 'function') {
      cfg.call.setAll(S.rows.map(r => [Q.num(r.strike), m.parts(r).call]));
      cfg.put.setAll(S.rows.map(r => [Q.num(r.strike), m.parts(r).put]));
    } else {
      cfg.call.setAll([]);
      cfg.put.setAll([]);
    }
  }

  /** ¿La métrica activa puede desglosarse en call y put? */
  function hasBreakdown(side) {
    const m = METRICS[S[side].metric];
    return !!(m && typeof m.parts === 'function');
  }

  function strikeStep() {
    const s = S.strikes;
    if (s.length < 2) return 1;
    let min = Infinity;
    for (let i = 1; i < s.length; i++) min = Math.min(min, Math.abs(s[i] - s[i - 1]));
    return Q.isNum(min) && min > 0 ? min : 1;
  }

  function emptyPanel(ctx, env, msg) {
    ctx.save();
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.font = '11px ui-monospace, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(msg, env.w / 2, env.h / 2);
    ctx.restore();
  }

  /* ---------------------------------------------------- panel central */

  function drawMain(ctx, env) {
    const box = { x: GUT.left, y: PAD.top, w: env.w - GUT.left - GUT.right, h: env.h - PAD.top - PAD.bottom };
    if (box.w <= 8 || box.h <= 8) return false;

    const d = S.data;
    if (!d) { emptyPanel(ctx, env, 'CARGANDO TRACE…'); return false; }

    const sy = priceScale(box);
    if (!sy) { emptyPanel(ctx, env, 'SIN DATOS DE PRECIO'); return false; }

    const candles = d.candles || [];
    let t0 = S.link.t0, t1 = S.link.t1;
    const bounds = timeBounds(candles);
    // La ventana temporal es COMPARTIDA con Flujo de Órdenes, y el TRACE la aceptaba
    // sin condición. Si el otro panel la fijaba más ancha que las velas del TRACE
    // —su tape de acciones arranca en premarket y el suyo no—, el precio y el heatmap
    // se comprimían contra un lado y sobraba la mitad del lienzo. Siguiendo en vivo el
    // TRACE manda sobre su propio eje; en cuanto el usuario arrastra o hace zoom
    // (follow=false) vuelve a respetar la ventana compartida, que es lo que permite
    // comparar paneles.
    if (S.link.follow && bounds) {
      t0 = bounds.t0; t1 = bounds.t1;
      S.link.setWindow(t0, t1, { silent: true });
    } else if (!Q.isNum(t0) || !Q.isNum(t1)) {
      if (!bounds) { emptyPanel(ctx, env, 'SIN VELAS'); return false; }
      t0 = bounds.t0; t1 = bounds.t1;
      S.link.setWindow(t0, t1, { silent: true });
    }
    if (!Q.isNum(t0) || !Q.isNum(t1) || t1 <= t0) { emptyPanel(ctx, env, 'SIN VELAS'); return false; }
    const sx = Q.scale(t0, t1, box.x, box.x + box.w);

    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();

    // 1 · heatmap estructural de fondo
    if (S.heat && S.heatOpacity > 0.01) {
      const hm = S.heat;
      const lo = Math.min.apply(null, hm.strikes), hi = Math.max.apply(null, hm.strikes);
      const step = hm.strikes.length > 1 ? (hi - lo) / (hm.strikes.length - 1) : 1;
      const yTop = sy(hi + step / 2), yBot = sy(lo - step / 2);
      const tSpan = hm.t1 - hm.t0;
      const cellT = hm.times.length > 1 ? tSpan / (hm.times.length - 1) : 60_000;
      const xL = sx(hm.t0 - cellT / 2), xR = sx(hm.t1 + cellT / 2);
      if (xR > xL && yBot > yTop) {
        ctx.save();
        ctx.globalAlpha = S.heatOpacity;
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(hm.canvas, xL, yTop, xR - xL, yBot - yTop);
        ctx.restore();

        /* Isolíneas encima del campo.
         *
         * Son lo que convierte una mancha en una lectura: dicen DÓNDE ACABA una
         * concentración. El campo solo es un degradado, y un degradado no tiene
         * borde; con la isolínea se ve el contorno de la zona y se puede decir si
         * el precio está dentro o fuera de ella.
         *
         * Se dibujan con la tinta del texto y muy finas: tienen que leerse sobre
         * el campo sin competir con las velas.
         */
        if (Array.isArray(hm.iso) && hm.iso.length && hm.cols > 1 && hm.rows > 1) {
          const cw = (xR - xL) / hm.cols, ch = (yBot - yTop) / hm.rows;
          ctx.save();
          ctx.lineWidth = 1;
          ctx.lineCap = 'round';
          for (const band of hm.iso) {
            ctx.strokeStyle = Q.alpha(Q.token('--text', '#e6edf7'),
                                      HEAT_ISO_ALPHA * S.heatOpacity *
                                      (0.55 + 0.45 * band.level));
            ctx.beginPath();
            for (const g of band.segs) {
              // El campo tiene la fila 0 abajo y la pantalla la tiene arriba.
              ctx.moveTo(xL + (g[0] + 0.5) * cw, yBot - (g[1] + 0.5) * ch);
              ctx.lineTo(xL + (g[2] + 0.5) * cw, yBot - (g[3] + 0.5) * ch);
            }
            ctx.stroke();
          }
          ctx.restore();
        }
      }
    }

    // 2 · rejilla
    const ticks = priceTicks();
    Q.gridY(ctx, box, sy, ticks, t => t.toFixed(t >= 1000 ? 0 : 2), { labelSide: 'left' });
    ctx.save();
    ctx.font = '10px ui-monospace, monospace';
    ctx.fillStyle = Q.token('--text-dim', '#8494ad');
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    for (const t of ticks) ctx.fillText(t.toFixed(t >= 1000 ? 0 : 2), box.x + box.w + 6, sy(t));
    ctx.restore();
    Q.axisX(ctx, box, sx, t0, t1, { grid: true });

    // 3 · precio
    if (S.priceMode === 'line') drawPriceLine(ctx, box, sx, sy, candles);
    else drawCandles(ctx, box, sx, sy, candles, d.bar_interval_ms);

    // 4 · prints de opciones observados
    if (S.showPrints) drawPrints(ctx, box, sx, sy, d.option_prints || []);

    ctx.restore();

    // 5 · niveles estructurales con etiquetas encadenadas
    if (S.showLevels) drawLevels(ctx, box, sy, d.levels || []);

    // 5b · QFLOW: nivel estructural y concentraciones, sobre el mismo eje temporal
    if (S.showQflow) {
      drawQflowLevel(ctx, box, sy, d.qflow);
      ctx.save();
      ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();
      drawQflowMarkers(ctx, box, sx, sy, d.qflow);
      ctx.restore();
    }

    // 5c · migración de gamma sobre el strike donde se confirmó
    if (S.showGammaMigration) drawGammaMigration(ctx, box, sy, d.gamma_migration);

    // 6 · precio actual en ambos bordes (como en la referencia)
    const spot = S.spot.get();
    if (Q.isNum(spot)) {
      const y = sy(spot);
      if (y >= box.y && y <= box.y + box.h) {
        Q.levelLine(ctx, box, y, Q.alpha(Q.token('--accent', '#38bdf8'), 0.8), { dash: [4, 3] });
        const txt = '$' + spot.toFixed(spot >= 1000 ? 2 : 2);
        Q.chip(ctx, box.x - 4, y, txt, { align: 'right', bg: Q.token('--accent', '#38bdf8'), color: '#04121f' });
        Q.chip(ctx, box.x + box.w + 4, y, txt, { align: 'left', bg: Q.token('--accent', '#38bdf8'), color: '#04121f' });
      }
    }

    // 7 · crosshair
    if (S.panels.main && S.panels.main.pointer.inside) {
      drawCrosshair(ctx, box, sx, sy);
      // Y la identidad de la línea bajo el cursor, si hay una.
      drawLevelHover(ctx, box, sy, S.panels.main.pointer.y);
    }

    let moving = S.spot.step(env.dt);
    moving = S.priceLo.step(env.dt) || moving;
    moving = S.priceHi.step(env.dt) || moving;
    return moving;
  }

  function timeBounds(candles) {
    if (!candles.length) return null;
    const first = Q.parseTime(candles[0].t);
    const last = Q.parseTime(candles[candles.length - 1].t);
    if (!Q.isNum(first) || !Q.isNum(last)) return null;
    const pad = Math.max((last - first) * 0.06, 120_000);
    return { t0: first, t1: last + pad };
  }

  function drawCandles(ctx, box, sx, sy, candles, intervalMs) {
    if (!candles.length) return;
    const iv = Q.num(intervalMs, 60_000);
    const bw = Math.max(2.2, Math.min(14, (sx(iv) - sx(0)) * 0.62));
    const posC = Q.token('--pos', '#22c55e');
    const negC = Q.token('--neg', '#ef4444');
    // TRACE comparte deliberadamente el eje Y con strikes/niveles. Cuando el precio
    // se mueve pocos centavos y el mapa cubre 8–15 strikes, un cuerpo OHLC real puede
    // medir <1 px y desaparecer aunque existan 100+ velas. La geometría vertical
    // mínima es SÓLO de rasterización: el centro sigue exactamente en el precio real,
    // no cambia el eje ni se inventa movimiento.
    const MIN_BODY_PX = 3.0;
    const MIN_WICK_PX = 5.0;
    ctx.save();
    ctx.lineWidth = 1.35;
    for (const c of candles) {
      const t = Q.parseTime(c.t);
      if (!Q.isNum(t)) continue;
      const x = sx(t + iv / 2);
      if (x < box.x - bw || x > box.x + box.w + bw) continue;
      const o = Q.num(c.o, NaN), h = Q.num(c.h, NaN), l = Q.num(c.l, NaN), cl = Q.num(c.c, NaN);
      if (![o, h, l, cl].every(Q.isNum)) continue;
      const up = cl >= o;
      const col = up ? posC : negC;

      let wickTop = Math.min(sy(h), sy(l));
      let wickBot = Math.max(sy(h), sy(l));
      if ((wickBot - wickTop) < MIN_WICK_PX) {
        const mid = (wickTop + wickBot) / 2;
        wickTop = mid - MIN_WICK_PX / 2;
        wickBot = mid + MIN_WICK_PX / 2;
      }
      ctx.strokeStyle = col;
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, wickTop);
      ctx.lineTo(Math.round(x) + 0.5, wickBot);
      ctx.stroke();

      let bodyTop = Math.min(sy(o), sy(cl));
      let bodyBot = Math.max(sy(o), sy(cl));
      if ((bodyBot - bodyTop) < MIN_BODY_PX) {
        const mid = (bodyTop + bodyBot) / 2;
        bodyTop = mid - MIN_BODY_PX / 2;
        bodyBot = mid + MIN_BODY_PX / 2;
      }
      ctx.fillStyle = col;
      ctx.fillRect(x - bw / 2, bodyTop, bw, bodyBot - bodyTop);
    }
    ctx.restore();
  }

  function drawPriceLine(ctx, box, sx, sy, candles) {
    if (!candles.length) return;
    ctx.save();
    ctx.strokeStyle = Q.token('--price', '#7aa2f7');
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    let started = false;
    for (const c of candles) {
      const t = Q.parseTime(c.t);
      const v = Q.num(c.c, NaN);
      if (!Q.isNum(t) || !Q.isNum(v)) continue;
      const x = sx(t), y = sy(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawPrints(ctx, box, sx, sy, prints) {
    if (!prints.length) return;
    let peak = 0;
    for (const p of prints) peak = Math.max(peak, Math.abs(Q.num(p.premium)));
    if (peak <= 0) return;
    ctx.save();
    for (const p of prints) {
      const t = Q.parseTime(p.t), px = Q.num(p.price, NaN), prem = Math.abs(Q.num(p.premium));
      if (!Q.isNum(t) || !Q.isNum(px) || prem <= 0) continue;
      const x = sx(t), y = sy(px);
      if (x < box.x - 10 || x > box.x + box.w + 10) continue;
      const rel = prem / peak;
      if (rel < 0.08) continue;
      const r = 2 + rel * 6;
      const dir = Q.num(p.direction, 0);
      const col = dir > 0 ? Q.token('--pos', '#22c55e') : dir < 0 ? Q.token('--neg', '#ef4444') : Q.token('--gold', '#d9a441');
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = Q.alpha(col, 0.55); ctx.fill();
      ctx.strokeStyle = Q.alpha(col, 0.95); ctx.lineWidth = 1; ctx.stroke();
    }
    ctx.restore();
  }

  /** Nivel QFLOW sobre el gráfico principal.
   *
   * Es dónde estaba el SUBYACENTE mientras se pagaba el grueso de la prima. No es
   * el strike dominante, ni el Net Drift, ni el Gamma Center, ni el Zero Gamma, y
   * por eso se dibuja con su propio estilo en vez de mezclarse con los niveles
   * estructurales: leerlo como si fuera un muro sería un error de interpretación.
   *
   * Sólo se dibuja cuando la concentración es ESTRUCTURALMENTE relevante. Un nivel
   * calculado sobre prima repartida por todo el rango no señala nada, y dibujarlo
   * igual lo convertiría en ruido con aspecto de señal.
   */
  const QFLOW_MIN_CONCENTRATION = 0.55;

  function drawQflowLevel(ctx, box, sy, qflow) {
    const lvl = qflow && qflow.level;
    if (!lvl) return;
    const price = Q.num(lvl.price, NaN);
    const conc = Q.num(lvl.concentration, 0);
    if (!Q.isNum(price) || conc < QFLOW_MIN_CONCENTRATION) return;
    const y = sy(price);
    if (y < box.y - 2 || y > box.y + box.h + 2) return;
    const col = Q.token('--gold', '#d9a441');
    Q.levelLine(ctx, box, y, Q.alpha(col, 0.75), { dash: [2, 4] });
    Q.chip(ctx, box.x + box.w - 8, Q.clamp(y, box.y + 9, box.y + box.h - 9),
      `QFLOW ${price.toFixed(price >= 1000 ? 0 : 2)}`,
      { align: 'right', bg: Q.alpha(col, 0.92), color: '#06101c' });
  }

  /** La vela de TRACE que CONTIENE un instante dado.
   *
   * Es la diferencia entre anclar y aproximar. `m.price` es el precio de referencia
   * del bucket de 1 min del proveedor de opciones; la vela es del feed del
   * subyacente, con otro reloj y otra granularidad. Dibujar la marca a la altura de
   * `m.price` la deja flotando cerca del gráfico pero sin pertenecer a ninguna vela,
   * y con el eje comprimido eso son varios píxeles de mentira.
   *
   * Se busca la vela cuyo intervalo [t, t+bar) contiene el instante. Búsqueda
   * binaria porque esto corre por frame y por marca.
   */
  function candleAt(t) {
    const d = S.data || {};
    const cs = d.candles || [];
    if (!cs.length || !Q.isNum(t)) return null;
    const bar = Q.num(d.bar_interval_ms, 60_000);
    let lo = 0, hi = cs.length - 1, found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const ct = Q.parseTime(cs[mid].t);
      if (!Q.isNum(ct)) { lo = mid + 1; continue; }
      if (ct <= t) { found = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (found < 0) return null;
    const c = cs[found];
    const ct = Q.parseTime(c.t);
    // Fuera del intervalo de su propia vela: el evento cae en un hueco de la serie
    // (mercado cerrado, vela ausente). Se declara en vez de asignarlo a la anterior.
    if (!Q.isNum(ct) || t - ct >= bar) return null;
    return { candle: c, index: found, t: ct, bar };
  }

  /** Concentraciones QFLOW ancladas a su vela exacta: ▲ $X.XM / ▼ $X.XM.
   *
   * La marca se coloca en el CENTRO temporal de la vela que contiene el evento y a
   * la altura de su máximo (concentración de calls) o su mínimo (de puts), no a un
   * precio aproximado de otra fuente. Así la marca pertenece visiblemente a una
   * vela concreta y se puede leer «esto pasó AQUÍ».
   *
   * Cuando el evento no cae en ninguna vela se dibuja con menos opacidad y sobre el
   * precio del bucket, declarado: una marca huérfana es información, pero no debe
   * parecer tan firme como una anclada.
   */

  /* ── Marca de flujo: círculo dorado + flecha de sentido + cantidad ────────
   *
   * v1.50.0 · Tres piezas, cada una con un trabajo distinto:
   *
   *   CÍRCULO DORADO   marca el SITIO. Anillos concéntricos con un núcleo
   *                    brillante, para que se vea encima de un mapa de calor
   *                    saturado sin taparlo. El radio crece con la magnitud,
   *                    así que dos concentraciones se comparan de un vistazo.
   *   FLECHA           el SENTIDO, y aquí sí en verde compra / rojo venta: la
   *                    flecha tiene forma propia, así que el color no se
   *                    confunde con el de la línea de precio como pasaba
   *                    cuando el color era lo único que distinguía CALL de PUT.
   *   CIFRA            la CANTIDAD, al lado de la flecha.
   *
   * Se dibuja en el punto exacto en que ocurrió, sobre el precio.
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
  /* v1.56.0 · Las cinco funciones de la marca vivían DUPLICADAS aquí y en
   * FLUJO DE ÓRDENES. Eran equivalentes el día que se escribieron, y ese es el
   * problema: dos copias equivalentes se separan en cuanto alguien corrige una,
   * y una marca que significa COMPRA en una pantalla y otra cosa en la de al
   * lado es peor que no dibujarla. Ahora hay UNA, en `itmq_core`. */
  const flowSide = Q.flowSide;
  const flowArrow = Q.flowArrow;
  const flowAmount = Q.flowAmount;
  const markerStrength = ev => Q.flowStrength(ev, S.qflowPeak);
  const markerAmount = Q.flowAmountText;

  function drawQflowMarkers(ctx, box, sx, sy, qflow) {
    const marks = (qflow && qflow.markers) || [];
    if (!marks.length) return;
    S.qflowHits = [];
    // El radio compara dentro del ciclo: sin una referencia común, dos marcas
    // del mismo tamaño podrían significar cosas muy distintas.
    S.qflowPeak = 0;
    for (const mk of marks) {
      const v = Math.abs(Q.num(mk.premium, NaN));
      if (Q.isNum(v)) S.qflowPeak = Math.max(S.qflowPeak, v);
    }
    ctx.save();
    ctx.font = '600 10px ui-monospace, monospace';
    ctx.textAlign = 'center';
    for (const m of marks) {
      const t = Q.parseTime(m.t);
      if (!Q.isNum(t)) continue;
      // v1.52.0 · La flecha dice COMPRA o VENTA sólo cuando el AGRESOR se conoce.
      // `null` = no se midió el lado, y entonces se dibuja rombo neutro: una
      // flecha inventada sobre un gráfico de operativa puede costar dinero.
      const up = flowSide(m);
      const above = up !== false;
      const hit = candleAt(t);

      let x, y, anchored;
      if (hit) {
        // Centro de la vela: la marca pertenece a la vela, no al borde entre dos.
        x = sx(hit.t + hit.bar / 2);
        const hi = Q.num(hit.candle.h, NaN), lo = Q.num(hit.candle.l, NaN);
        const close = Q.num(hit.candle.c, NaN);
        y = sy(above ? (Q.isNum(hi) ? hi : close) : (Q.isNum(lo) ? lo : close));
        anchored = true;
      } else {
        const px = Q.num(m.price, NaN);
        if (!Q.isNum(px)) continue;
        x = sx(t);
        y = sy(px);
        anchored = false;
      }
      if (!Q.isNum(x) || !Q.isNum(y)) continue;
      if (x < box.x - 20 || x > box.x + box.w + 20) continue;

      /* v1.50.0 · Círculo dorado en el sitio, flecha de sentido, cantidad.
       *
       * El círculo marca DÓNDE ocurrió y su radio dice cuánto, así que dos
       * concentraciones se comparan sin leer las cifras. La flecha dice el
       * sentido —verde compra, rojo venta— y ahora sí puede llevar color,
       * porque tiene forma propia y no se confunde con la línea de precio.
       */
      const a = anchored ? 1 : 0.5;
      ctx.save();
      ctx.globalAlpha = a;
      // UNA sola llamada, la misma que usan FLUJO y NET DRIFT.
      const marca = Q.flowMark(ctx, x, y, m, S.qflowPeak, { label: m.label || undefined });
      ctx.restore();
      const dy = marca.dy;

      // Zona sensible para el hover: el detalle enriquecido del Order Flow no se
      // pinta encima del gráfico, se pide al pasar por la marca.
      S.qflowHits.push({ x, y: y + dy, marker: m, anchored });
    }
    ctx.restore();
  }

  /** Marca QFLOW bajo el cursor, si la hay. */
  function qflowUnderPointer(px, py) {
    for (const h of (S.qflowHits || [])) {
      if (Math.abs(h.x - px) <= 14 && Math.abs(h.y - py) <= 16) return h;
    }
    return null;
  }

  /** Migración de gamma sobre el STRIKE donde se confirmó.
   *
   * No es una tarjeta ni un panel: es una marca en el eje de precio, a la altura
   * del strike que GANÓ exposición, con una flecha desde el que la perdió. Leerla
   * contra el precio es el objetivo; resumirla en un número aparte la desconecta
   * justo de lo que explica.
   *
   * `QD_INTERVAL_MAP` es dato del proveedor; esta lectura es DERIVED y se dibuja
   * con el estilo tenue que la distingue de un nivel estructural.
   */
  function drawGammaMigration(ctx, box, sy, mig) {
    if (!mig || mig.ready !== true || !mig.label) return;
    const to = Q.num(mig.to_strike, NaN);
    const from = Q.num(mig.from_strike, NaN);
    const anchor = Q.isNum(to) ? to : from;
    if (!Q.isNum(anchor)) return;
    const y = sy(anchor);
    if (y < box.y - 2 || y > box.y + box.h + 2) return;

    const col = Q.token('--gold', '#d9a441');
    ctx.save();
    // Flecha desde el strike que perdió exposición hasta el que la ganó: la
    // dirección de la migración es la mitad del mensaje.
    if (Q.isNum(to) && Q.isNum(from)) {
      const y0 = sy(from);
      if (Q.isNum(y0)) {
        const x = box.x + box.w - 92;
        ctx.strokeStyle = Q.alpha(col, 0.55);
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y); ctx.stroke();
        ctx.setLineDash([]);
        const dir = y < y0 ? -1 : 1;
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x - 3, y - dir * 5);
        ctx.lineTo(x + 3, y - dir * 5);
        ctx.closePath();
        ctx.fillStyle = Q.alpha(col, 0.7);
        ctx.fill();
      }
    }
    Q.chip(ctx, box.x + box.w - 86, Q.clamp(y, box.y + 9, box.y + box.h - 9),
      mig.label, { align: 'left', bg: Q.alpha(col, 0.85), color: '#06101c' });
    ctx.restore();
  }

  function drawLevels(ctx, box, sy, levels) {
    /* v1.52.1 · Un nivel FUERA de la ventana ya no desaparece.
     *
     * Antes se descartaba, y con QQQ en 722 el PUT WALL de 700 no se dibujaba en
     * ninguna parte: el KPI de abajo lo publicaba y el gráfico no lo enseñaba,
     * así que parecía que no existía. Y no se puede resolver estirando la
     * ventana: un muro a veinte dólares aplastaría las velas contra una línea.
     *
     * Se ancla al BORDE con una punta de flecha que dice hacia dónde queda, y la
     * etiqueta lleva la distancia. Así el muro está siempre, sin deformar el
     * gráfico, y se distingue de uno que sí cae dentro.
     */
    const items = [];
    const spotNow = Q.num((S.data.profiles || {}).spot, NaN);
    for (const lv of levels) {
      const p = Q.num(lv.price, NaN);
      if (!Q.isNum(p)) continue;
      const y = sy(p);
      // v1.55.0 · Por `Q.levelStyle`, que DENUNCIA el `kind` desconocido en vez
      // de devolver un estilo neutro con el que la linea pasa por una mas.
      const st = Q.levelStyle(lv.kind);
      const above = y < box.y - 2, below = y > box.y + box.h + 2;
      const off = above ? -1 : below ? 1 : 0;
      items.push({
        y: off ? (above ? box.y + 1 : box.y + box.h - 1) : y,
        price: p, off,
        // El nombre que manda es el que publica el MOTOR. `short` sólo entra
        // cuando el largo no cabe, y nunca sustituye a la identidad.
        name: lv.name || st.label || lv.kind,
        short: st.short || lv.name || lv.kind,
        // Una linea sin identidad se dibuja MARCADA, para que se vea que
        // sobra o que falta registrarla. No se disimula con el color por
        // defecto: ese es justo el defecto que la hacia invisible.
        unidentified: !!st.unidentified,
        color: Q.token(st.color, '#8494ad'),
        order: st.order || 9,
        away: Q.isNum(spotNow) ? p - spotNow : NaN,
      });
    }
    if (!items.length) return;
    items.sort((a, b) => a.order - b.order);
    // La línea se dibuja para todos los niveles; la etiqueta sólo para los seis más
    // relevantes, porque con más el bloque de texto tapa la zona del precio.
    // v1.51.0 · La línea sube de 1 a 1.6 px y la opacidad de 0.45 a 0.72: sobre el
    // campo de calor y las velas, una línea de 1 px al 45% se perdía justo en las
    // zonas densas, que son las que hay que leer.
    for (const it of items) {
      // El nivel fuera de ventana lleva línea más tenue: está ahí, pero no es
      // un precio que las velas estén tocando.
      Q.levelLine(ctx, box, it.y, Q.alpha(it.color, it.off ? 0.42 : 0.72),
                  { dash: it.unidentified ? [1, 4] : (it.off ? [3, 4] : [6, 5]),
                    width: Q.LEVEL_LINE_WIDTH });
    }
    // Los de dentro primero; un muro fuera de ventana no puede quitarle el sitio
    // a uno que el precio está tocando.
    /* v1.53.1 · TODA línea visible lleva etiqueta.
     *
     * El cupo dejaba sin nombre a `target` y `risk` —los últimos por prioridad—
     * y ésas son justo las dos líneas sin identificar del gráfico: la roja es
     * `Invalidación` y la verde es un objetivo `T1`/`T2`. Se dibujaban, se
     * veían, y no había forma de saber qué eran.
     *
     * No se puede deducir por color: rojo es `put_wall` Y `risk`, verde es
     * `call_wall` Y `target`. El color agrupa `kind` distintos.
     *
     * Si no caben todas con su nombre largo, las de menor prioridad pasan al
     * nombre CORTO antes que quedarse mudas: una abreviatura identifica; una
     * línea anónima no.
     */
    const ordered = items.slice().sort((a, b) => (a.off ? 1 : 0) - (b.off ? 1 : 0) || a.order - b.order);
    const room = Math.max(1, Math.floor(box.h / Q.LEVEL_LABEL_GAP));
    const LONG = 6;
    const placed = Q.stackLabels(ordered.slice(0, room).map((it, i) => ({ ...it, rank: i })),
                                 Q.LEVEL_LABEL_GAP);
    for (const it of placed) {
      const digits = it.price >= 1000 ? 0 : 2;
      const arrow = it.off < 0 ? '▲ ' : it.off > 0 ? '▼ ' : '';
      const dist = (it.off && Q.isNum(it.away))
        ? `  ${it.away >= 0 ? '+' : ''}${it.away.toFixed(digits)}` : '';
      // Las primeras conservan el nombre que publica el MOTOR; el resto pasan
      // al corto. `short` no renombra: sólo abrevia en pantalla.
      const label = it.rank < LONG ? it.name : it.short;
      Q.chip(ctx, box.x + 8,
        Q.clamp(it.y, box.y + Q.LEVEL_LABEL_H / 2, box.y + box.h - Q.LEVEL_LABEL_H / 2),
        `${arrow}${label} ${it.price.toFixed(digits)}${dist}`,
        { bg: Q.alpha(it.color, it.off ? 0.62 : 0.92), color: '#06101c',
          font: Q.LEVEL_FONT, h: Q.LEVEL_LABEL_H, padX: 7 });
    }
  }

  /* v1.54.0 · Al pasar por una línea, QUÉ es.
   *
   * Sale del registro de identidad que el motor ya publica —nombre, precio,
   * dirección, fuerza, fuente y timestamp—, no de una tabla paralela en el
   * cliente. Sin esto, una línea a la que el precio reacciona sigue siendo
   * anónima mientras no se abra el Auditor.
   */
  const HOVER_HIT_PX = 6;

  function levelUnderPointer(box, sy, py) {
    const d = S.data || {};
    const ident = d.level_identity || [];
    const lvls = d.levels || [];
    let best = null, bestD = Infinity;
    for (const lv of lvls) {
      const p = Q.num(lv.price, NaN);
      if (!Q.isNum(p)) continue;
      const y = sy(p);
      if (y < box.y || y > box.y + box.h) continue;
      const dist = Math.abs(py - y);
      if (dist < bestD && dist <= HOVER_HIT_PX) { bestD = dist; best = lv; }
    }
    if (!best) return null;
    const id = ident.find(r => Math.abs(Q.num(r.price, NaN) - Q.num(best.price, 0)) < 1e-9
                               && String(r.type || '') === String(best.kind || ''));
    return { level: best, identity: id || null };
  }

  function drawLevelHover(ctx, box, sy, py) {
    const hit = levelUnderPointer(box, sy, py);
    if (!hit) return;
    const lv = hit.level, id = hit.identity || {};
    const st = Q.levelStyle(lv.kind);
    const col = Q.token(st.color, '#8494ad');
    const price = Q.num(lv.price, 0);
    const lines = [
      `${lv.name || st.label || lv.kind}   ${price.toFixed(price >= 1000 ? 2 : 2)}`,
    ];
    // Dirección y fuerza sólo si el nivel es del Scanner: en un muro no
    // significan nada y escribirlos sugeriría que sí.
    if (lv.direction) lines.push(`dirección ${lv.direction}`);
    if (Q.isNum(Q.num(lv.strength, NaN))) lines.push(`fuerza ${Q.num(lv.strength).toFixed(0)}/100`);
    lines.push(`fuente ${lv.authority || id.source || '—'}`);
    if (id.timestamp) lines.push(String(id.timestamp).replace('T', ' ').slice(0, 19));
    ctx.save();
    ctx.font = '11px ui-monospace, monospace';
    let w = 0;
    for (const t of lines) w = Math.max(w, ctx.measureText(t).width);
    const bw = w + 18, bh = lines.length * 15 + 10;
    const bx = Q.clamp(box.x + box.w * 0.5, box.x + 4, box.x + box.w - bw - 4);
    const by = Q.clamp(sy(price) - bh - 10, box.y + 4, box.y + box.h - bh - 4);
    Q.roundRect(ctx, bx, by, bw, bh, 5);
    ctx.fillStyle = Q.alpha(Q.token('--panel-3', '#1b2436'), 0.97); ctx.fill();
    ctx.strokeStyle = Q.alpha(col, 0.9); ctx.lineWidth = 1;
    Q.roundRect(ctx, bx, by, bw, bh, 5); ctx.stroke();
    ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    lines.forEach((t, i) => {
      ctx.fillStyle = i === 0 ? col : Q.token('--text-dim', '#8494ad');
      ctx.fillText(t, bx + 9, by + 6 + i * 15);
    });
    ctx.restore();
  }

  function drawCrosshair(ctx, box, sx, sy) {
    const p = S.panels.main.pointer;
    if (!Q.isNum(p.x) || !Q.isNum(p.y)) return;
    if (p.x < box.x || p.x > box.x + box.w || p.y < box.y || p.y > box.y + box.h) return;
    ctx.save();
    ctx.strokeStyle = Q.alpha(Q.token('--text-dim', '#8494ad'), 0.6);
    ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(box.x, Math.round(p.y) + 0.5); ctx.lineTo(box.x + box.w, Math.round(p.y) + 0.5); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(Math.round(p.x) + 0.5, box.y); ctx.lineTo(Math.round(p.x) + 0.5, box.y + box.h); ctx.stroke();
    ctx.restore();
    const price = sy.invert(p.y);
    Q.chip(ctx, box.x + box.w + 4, p.y, price.toFixed(price >= 1000 ? 1 : 2),
      { align: 'left', bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7') });
    const t = sx.invert(p.x);
    Q.chip(ctx, p.x, box.y + box.h + 12, Q.hhmm(t),
      { align: 'center', bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7') });

    // El detalle enriquecido del Order Flow vive en el hover, no pintado encima del
    // gráfico: lo que la marca tiene que decir de un vistazo es «▲ 4.2M aquí»; lo
    // que la explica —cuántas calls, de quién, en qué strike, BLOCK o SWEEP— sólo
    // interesa cuando se pregunta por ella, y pintarlo siempre taparía las velas.
    const hover = qflowUnderPointer(p.x, p.y);
    if (hover) drawQflowTooltip(ctx, box, hover);
  }

  /** Qué operaciones produjeron esta concentración, al pasar por encima. */
  function drawQflowTooltip(ctx, box, hit) {
    const m = hit.marker || {};
    const att = attributionAt(m.t);
    const lines = [m.label || '—'];
    lines.push(hit.anchored ? Q.hhmm(Q.parseTime(m.t)) : Q.hhmm(Q.parseTime(m.t)) + ' · sin vela');

    /* v1.55.0 · POR QUE esta flecha dice lo que dice.
     *
     * La flecha sale del AGRESOR, no del tipo de contrato (una put COMPRADA es
     * una compra). Pero un veredicto sin su soporte no se puede contrastar: al
     * pasar el raton tiene que verse cuanta prima fue de compra, cuanta de
     * venta, cuanta se quedo SIN LADO, y sobre que porcentaje de la prima de la
     * ventana se esta afirmando.
     *
     * Dos casos que antes se veian identicos y no lo son:
     *   COMPRA 96% dominancia · 91% con agresor   -> veredicto solido
     *   COMPRA 96% dominancia ·  7% con agresor   -> tres prints de noventa
     */
    const verdicto = { BUY: 'COMPRA', SELL: 'VENTA', MIXED: 'REPARTIDO' }[String(m.aggressor || '').toUpperCase()] || 'SIN LADO';
    const dom = Q.num(m.aggressor_dominance_pct, NaN);
    const cov = Q.num(m.aggressor_coverage_pct, NaN);
    lines.push(verdicto
      + (Q.isNum(dom) ? ` · ${dom.toFixed(0)}% dominancia` : '')
      + (Q.isNum(cov) ? ` · ${cov.toFixed(0)}% con agresor` : ''));

    /* v1.56.0 · El reparto, en PORCENTAJE sobre TODA la prima de la ventana.
     *
     * No sobre lo clasificado: si el 40 % no tiene lado, aquí tiene que verse
     * ese 40 %. Repartirlo entre compra y venta para que las cifras sumen 100
     * es inventar dos tercios de la lectura. */
    const bp = Q.num(m.buy_pct, NaN), sp = Q.num(m.sell_pct, NaN), up = Q.num(m.unknown_pct, NaN);
    if (Q.isNum(bp) || Q.isNum(sp) || Q.isNum(up)) {
      if (Q.isNum(bp)) lines.push(`BUY      ${bp.toFixed(0)} %`);
      if (Q.isNum(sp)) lines.push(`SELL     ${sp.toFixed(0)} %`);
      if (Q.isNum(up)) lines.push(`UNKNOWN  ${up.toFixed(0)} %`);
    }
    // De qué campo salió la mayoría de los lados. «68 % BUY» no significa lo
    // mismo desde el campo oficial del proveedor que desde medir el NBBO.
    const FUENTE = {
      PROVIDER_TRADE_SIDE_CODE: 'tradeSideCode',
      PROVIDER_FIELD: 'campo del proveedor',
      NBBO: 'NBBO del instante',
    };
    const fuente = FUENTE[String(m.classification_source || '')];
    if (fuente) lines.push('Clasificación principal: ' + fuente);

    const desglose = [];
    if (Q.isNum(Q.num(m.buy_premium, NaN))) desglose.push('C ' + Q.money(Q.num(m.buy_premium), 1));
    if (Q.isNum(Q.num(m.sell_premium, NaN))) desglose.push('V ' + Q.money(Q.num(m.sell_premium), 1));
    if (Q.num(m.unknown_premium, 0) > 0) desglose.push('? ' + Q.money(Q.num(m.unknown_premium), 1));
    if (desglose.length) lines.push(desglose.join(' · '));
    if (!Q.isNum(cov) && m.aggressor_detail) lines.push(String(m.aggressor_detail).slice(0, 52));

    if (att) {
      const parts = [];
      if (att.calls) parts.push(att.calls + ' call');
      if (att.puts) parts.push(att.puts + ' put');
      if (parts.length) lines.push(parts.join(' · '));
      const side = [];
      if (att.buys) side.push(att.buys + ' compra');
      if (att.sells) side.push(att.sells + ' venta');
      if (att.unknowns) side.push(att.unknowns + ' sin lado');
      if (side.length) lines.push(side.join(' · '));
      const tags = Object.keys(att.executions || {});
      if (tags.length) lines.push(tags.join(' · '));
      if (att.dominant_strike && Q.isNum(Q.num(att.dominant_strike.strike, NaN))) {
        lines.push('strike ' + Q.num(att.dominant_strike.strike).toFixed(0)
          + ' · ' + Q.money(Q.num(att.dominant_strike.premium, 0), 1));
      }
      const top = (att.top_trades || [])[0];
      if (top && top.expiration) {
        lines.push('vto ' + String(top.expiration)
          + (Q.isNum(Q.num(top.dte, NaN)) ? ' · ' + Q.num(top.dte).toFixed(0) + ' DTE' : ''));
      }
    } else {
      lines.push('sin operaciones atribuidas');
    }

    ctx.save();
    ctx.font = '10px ui-monospace, monospace';
    let w = 0;
    for (const l of lines) w = Math.max(w, ctx.measureText(l).width);
    w += 14;
    const h = lines.length * 13 + 10;
    let x = hit.x + 12, y = hit.y - h / 2;
    if (x + w > box.x + box.w) x = hit.x - 12 - w;
    y = Q.clamp(y, box.y + 2, box.y + box.h - h - 2);
    Q.roundRect(ctx, x, y, w, h, 4);
    ctx.fillStyle = Q.alpha(Q.token('--panel-3', '#1b2436'), 0.96);
    ctx.fill();
    ctx.strokeStyle = Q.alpha(Q.token('--gold', '#d9a441'), 0.6);
    ctx.lineWidth = 1; ctx.stroke();
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    lines.forEach((l, i) => {
      ctx.fillStyle = i === 0 ? Q.token('--text', '#e6edf7') : Q.token('--text-dim', '#8494ad');
      ctx.fillText(l, x + 7, y + 5 + i * 13);
    });
    ctx.restore();
  }

  /** Atribución del Order Flow para el instante exacto de una marca. */
  function attributionAt(t) {
    const att = ((S.data || {}).qflow || {}).attribution;
    if (!att || att.ready !== true || !Array.isArray(att.events)) return null;
    const target = Q.parseTime(t);
    if (!Q.isNum(target)) return null;
    for (const e of att.events) {
      if (Q.parseTime(e.t) === target && e.matched) return e;
    }
    return null;
  }

  /* -------------------------------------------------------- interacción */

  function onMainWheel(ev, panel) {
    ev.preventDefault();
    const box = { x: GUT.left, w: panel.w - GUT.left - GUT.right };
    if (!Q.isNum(S.link.t0)) return;
    const sx = Q.scale(S.link.t0, S.link.t1, box.x, box.x + box.w);
    const anchor = sx.invert(panel.pointer.x);
    S.link.zoomAt(ev.deltaY > 0 ? 1.12 : 0.89, anchor);
  }

  let dragX = NaN;
  function onMainDown(pointer) { dragX = pointer.x; }
  function onMainMove(pointer, ev, panel) {
    // strike bajo el cursor: sincroniza el resaltado de los tres paneles
    const box = { y: PAD.top, h: panel.h - PAD.top - PAD.bottom };
    const sy = priceScale(box);
    if (sy) S.hoverStrike = nearestStrike(sy.invert(pointer.y));
    if (pointer.down && Q.isNum(dragX)) {
      const box2 = { x: GUT.left, w: panel.w - GUT.left - GUT.right };
      const perPx = S.link.span() / Math.max(1, box2.w);
      S.link.panBy(-(pointer.x - dragX) * perPx);
      dragX = pointer.x;
    }
    invalidateAll();
  }
  function onMainUp() { dragX = NaN; }

  function onSideMove(side) {
    return function (pointer, ev, panel) {
      const box = { y: PAD.top, h: panel.h - PAD.top - PAD.bottom };
      const sy = priceScale(box);
      if (sy) S.hoverStrike = nearestStrike(sy.invert(pointer.y));
      invalidateAll();
    };
  }

  /**
   * Un clic FIJA el strike. Pasar el ratón por encima ya resaltaba la fila, pero
   * para leer sus números hay que poder soltar el ratón sin que desaparezcan.
   * Volver a hacer clic en el mismo strike lo suelta.
   */
  function onSideClick(side) {
    return function (pointer, ev, panel) {
      const box = { y: PAD.top, h: panel.h - PAD.top - PAD.bottom };
      const sy = priceScale(box);
      if (!sy) return;
      const k = nearestStrike(sy.invert(pointer.y));
      S.pinnedStrike = (Q.isNum(S.pinnedStrike) && Math.abs(S.pinnedStrike - k) < 1e-9) ? NaN : k;
      emitStrike();
      invalidateAll();
    };
  }

  function emitStrike() {
    if (typeof S.onStrike !== 'function') return;
    const k = Q.isNum(S.pinnedStrike) ? S.pinnedStrike : NaN;
    try { S.onStrike(Q.isNum(k) ? strikeRow(k) : null); }
    catch (err) { console.error('[TRACE] onStrike', err); }
  }

  /** Todo lo que el motor publica de un strike, con su desglose call/put. */
  function strikeRow(k) {
    const row = (S.rows || []).find(r => Math.abs(Q.num(r.strike) - Q.num(k)) < 1e-9);
    if (!row) return null;
    // S.spot es un GlideValue (animación del eje), no un número: su valor se lee
    // con get(). El spot publicado por el motor es la referencia correcta.
    const spot = Q.num((S.data && S.data.profiles || {}).spot,
      (S.spot && typeof S.spot.get === 'function') ? S.spot.get() : NaN);
    return {
      strike: Q.num(row.strike),
      distance: Q.isNum(spot) ? Q.num(row.strike) - spot : null,
      distance_pct: Q.isNum(spot) && spot ? (Q.num(row.strike) - spot) / spot * 100 : null,
      gex: Q.num(row.gamma_m) * 1e6,
      gex_call: Q.num(row.call_gamma_m) * 1e6,
      gex_put: Q.num(row.put_gamma_m) * 1e6,
      gex_live: Q.num(row.gamma_change_m) * 1e6,
      dex: Q.num(row.delta_m) * 1e6,
      dex_call: Q.num(row.call_delta_m) * 1e6,
      dex_put: Q.num(row.put_delta_m) * 1e6,
      dex_live: Q.num(row.delta_change_m) * 1e6,
      vex: Q.num(row.vanna_1vol_m) * 1e6,
      chex: Q.num(row.charm_10m_m) * 1e6,
      oi: Q.num(row.oi), call_oi: Q.num(row.call_oi), put_oi: Q.num(row.put_oi),
      net_oi: Q.num(row.net_oi),
      volume: Q.num(row.volume_snapshot),
      call_volume: Q.num(row.call_volume), put_volume: Q.num(row.put_volume),
      net_volume: Q.num(row.net_volume),
      liquidity: Q.num(row.liquidity_score),
      gravity: Q.num(row.gravity_score),
      state: row.gamma_delta_state,
      joint: Q.num(row.gamma_delta_joint_score),
      opra_contracts_5m: Q.num(row.opra_contracts_5m),
      opra_premium_5m: Q.num(row.opra_premium_5m),
    };
  }

  function nearestStrike(price) {
    if (!Q.isNum(price) || !S.strikes.length) return NaN;
    let best = NaN, bd = Infinity;
    for (const k of S.strikes) { const d = Math.abs(k - price); if (d < bd) { bd = d; best = k; } }
    return best;
  }

  function invalidateAll() { for (const k in S.panels) S.panels[k].invalidate(); }

  /* ------------------------------------------------------------- datos */

  function applyData(payload) {
    if (!payload || typeof payload !== 'object') return;
    const prof = payload.profiles || {};
    const lastCandle = (payload.candles || []).slice(-1)[0] || {};
    // La última vela recibe ticks entre snapshots estructurales; para relaciones
    // espaciales (Walls vs precio) manda ese spot observado más reciente.
    const spot = Q.num(lastCandle.c, Q.num(prof.spot, NaN));
    // Invariante de presentación: una Call Wall nunca se dibuja por debajo (o
    // encima ya atravesada) del spot LIVE y una Put Wall nunca por encima. Si el
    // precio cruza un muro entre dos snapshots estructurales, ocultamos ese nivel
    // hasta que el motor recalcule; jamás lo "movemos" ni inventamos otro.
    const safeLevels = (Array.isArray(payload.levels) ? payload.levels : []).filter(l => {
      const p = Q.num(l && l.price, NaN);
      if (!Q.isNum(p) || !Q.isNum(spot)) return Q.isNum(p);
      if (l.kind === 'call_wall') return p > spot;
      if (l.kind === 'put_wall') return p < spot;
      return true;
    });
    S.data = Object.assign({}, payload, { levels: safeLevels });

    S.rows = Array.isArray(prof.rows) ? prof.rows.filter(r => Q.isNum(Q.num(r.strike, NaN))) : [];
    S.strikes = S.rows.map(r => Q.num(r.strike)).sort((a, b) => a - b);

    if (Q.isNum(spot)) S.spot.set(spot);

    const dom = computePriceDomain();
    if (dom) { S.priceLo.set(dom.lo); S.priceHi.set(dom.hi); }

    for (const side of ['left', 'right']) applyMetric(side);

    try { buildHeatBitmap(); }
    catch (err) { S.heat = null; S.heatKey = ''; console.warn('[TRACE] heatmap', err); }

    // Ventana temporal: en modo seguimiento se re-ancla a la última vela.
    const b = timeBounds(payload.candles || []);
    if (b) {
      if (!Q.isNum(S.link.t0) || S.link.follow) S.link.setWindow(b.t0, b.t1, { silent: true });
      else if (b.t1 > S.link.t1) { /* histórico en pantalla: no se mueve solo */ }
    }

    for (const k in S.panels) { S.panels[k].animate(true); S.panels[k].invalidate(); }
    try { renderHud(S.data); }
    catch (err) { console.warn('[TRACE] hud', err); }
    // El detalle del strike fijado se refresca con cada ciclo de datos.
    if (Q.isNum(S.pinnedStrike)) emitStrike();
  }

  function renderHud(d) {
    const set = (id, v) => { const x = document.getElementById(id); if (x) x.textContent = v === null || v === undefined || v === '' ? '—' : v; };
    const prof = d.profiles || {};
    const lastCandle = (d.candles || []).slice(-1)[0] || {};
    const liveSpot = Q.num(lastCandle.c, Q.num(prof.spot, NaN));
    const dec = d.decision || {};
    const ms = d.market_state || {};
    set('traceSpot', Q.isNum(liveSpot) ? liveSpot.toFixed(2) : '—');
    // v1.46.0 · `Q.num(null)` devuelve 0, así que un perfil ausente se publicaba
    // como «0.00» y «$0.0»: la especificación prohíbe exactamente eso. Un cero es
    // una AFIRMACIÓN —hoy la exposición neta es nula— y un hueco no lo es.
    const netM = (v) => {
      const x = Q.num(v, NaN);
      return Q.isNum(x) ? Q.signedCompact(x * 1e6, 2) : null;
    };
    set('traceGammaNet', netM(prof.gamma_net_m));
    set('traceDeltaNet', netM(prof.delta_net_m));
    set('traceGammaCenter', Q.isNum(Q.num(prof.gamma_center, NaN)) ? Q.num(prof.gamma_center).toFixed(2) : '—');
    set('traceDeltaCenter', Q.isNum(Q.num(prof.delta_center, NaN)) ? Q.num(prof.delta_center).toFixed(2) : '—');
    set('traceDirection', dec.direction || '—');
    set('traceEdge', dec.edge_state || '—');
    set('tracePhase', ms.phase || '—');
    const flow5m = Q.num(prof.opra_directional_premium_5m, NaN);
    set('traceFlow5m', Q.isNum(flow5m) ? Q.money(flow5m, 1) : null);
    const lv = {};
    for (const l of d.levels || []) lv[l.kind] = l.price;
    set('traceCallWall', fmtLevel(lv.call_wall));
    set('tracePutWall', fmtLevel(lv.put_wall));
    set('traceFlip', fmtLevel(lv.flip));
    set('traceVolTrigger', fmtLevel(lv.vol_trigger));
    const age = Q.num(prof.snapshot_age_seconds, NaN);
    set('traceSnapshotAge', Q.isNum(age) ? age.toFixed(0) + 's' : '—');
    // Origen del fondo, en lenguaje de análisis: nunca el nombre del endpoint.
    const hf = HEATFIELDS[S.heatField];
    set('traceHeatFieldLabel', hf ? hf.label : '—');
    // Sin mapa NO se escribe «SIN DATOS» a secas: se escribe POR QUÉ. Un fondo en
    // blanco sin causa es indistinguible de un fallo de render, y con ocho
    // opciones en el selector hay que poder saber cuál de ellas no tiene dato.
    set('traceHeatSource', S.heat
      ? (S.heat.mode === 'DIRECT_PROVIDER' ? 'estructura de opciones'
         : S.heat.mode === 'FALLBACK' ? 'estructura propia (respaldo)' : 'modelo propio')
      : (S.heatField === 'off' ? 'mapa apagado' : (S.heatReason || 'SIN DATOS')));
    const ql = (d.qflow || {}).level;
    set('traceQflowLevel', ql && Q.isNum(Q.num(ql.price, NaN))
      ? Q.num(ql.price).toFixed(Q.num(ql.price) >= 1000 ? 0 : 2) : '—');
  }

  function fmtLevel(v) {
    const x = Q.num(v, NaN);
    return Q.isNum(x) ? x.toFixed(x >= 1000 ? 0 : 2) : '—';
  }

  /* ------------------------------------------------------------- setup */

  function mount(ids) {
    const mk = (host, draw, opts) => host ? new Q.Panel(host, draw, opts) : null;
    S.panels.left = mk(ids.left, (ctx, env) => drawProfile(ctx, env, 'left'), {
      id: 'trace:left', onPointer: onSideMove('left'), onClick: onSideClick('left'),
      onPointerLeave: () => { S.hoverStrike = NaN; invalidateAll(); },
    });
    S.panels.main = mk(ids.main, drawMain, {
      id: 'trace:main', onPointer: onMainMove, onDown: onMainDown, onUp: onMainUp, onWheel: onMainWheel,
      onPointerLeave: () => { S.hoverStrike = NaN; invalidateAll(); },
    });
    S.panels.right = mk(ids.right, (ctx, env) => drawProfile(ctx, env, 'right'), {
      id: 'trace:right', onPointer: onSideMove('right'), onClick: onSideClick('right'),
      onPointerLeave: () => { S.hoverStrike = NaN; invalidateAll(); },
    });
    S.link.on(() => invalidateAll());
    return S;
  }

  function setMetric(side, metric) {
    if (!METRICS[metric] || (side !== 'left' && side !== 'right')) return;
    S[side].metric = metric;
    if (S.data) {
      applyMetric(side);
      const dom = computePriceDomain();
      if (dom) { S.priceLo.set(dom.lo); S.priceHi.set(dom.hi); }
    }
    for (const k in S.panels) { S.panels[k].animate(true); S.panels[k].invalidate(); }
  }

  /** NETO ↔ CALL+PUT en los dos perfiles laterales. */
  function setBreakdown(on) {
    S.breakdown = !!on;
    if (S.data) { applyMetric('left'); applyMetric('right'); }
    for (const k in S.panels) { S.panels[k].animate(true); S.panels[k].invalidate(); }
  }

  function setHeatField(field) {
    if (!HEATFIELDS[field] && field !== 'off') return;
    S.heatField = field;
    if (field === 'off') { S.heat = null; S.heatKey = ''; }
    else buildHeatBitmap();
    // El HUD sólo se redibujaba al llegar datos nuevos, así que tras cambiar de
    // griega seguía anunciando la anterior hasta el siguiente ciclo. Con cadencias
    // de varios segundos eso significaba leer «GAMMA» mirando el mapa de DELTA,
    // que es peor que no rotular nada.
    if (S.data) { try { renderHud(S.data); } catch (err) { console.warn('[TRACE] hud', err); } }
    invalidateAll();
  }

  function setHeatOpacity(v) { S.heatOpacity = Q.clamp(Q.num(v, 0.55), 0, 1); invalidateAll(); }
  function setPriceMode(mode) { S.priceMode = mode === 'line' ? 'line' : 'candles'; invalidateAll(); }
  function setFollow(on) {
    S.link.follow = !!on;
    if (on && S.data) { const b = timeBounds(S.data.candles || []); if (b) S.link.setWindow(b.t0, b.t1); }
    invalidateAll();
  }
  function toggle(flag, on) { S[flag] = !!on; invalidateAll(); }

  /** Alterna la griega del Interval Map: GAMMA · DELTA · VANNA · CHARM. */
  function setIntervalGreek(greek) {
    const g = String(greek || '').toUpperCase();
    if (INTERVAL_GREEKS.indexOf(g) < 0) return;
    setHeatField(g.toLowerCase());
  }

  function intervalGreeks() { return INTERVAL_GREEKS.slice(); }

  /**
   * v1.55.0 · ¿Los tres paneles comparten DE VERDAD el eje de precio?
   *
   * La escala es una sola —`priceScale`, sobre `S.priceLo`/`S.priceHi`— pero se
   * aplica sobre la ALTURA DEL LIENZO de cada panel. Si un lienzo mide cuatro
   * píxeles más que otro, el mismo precio cae en una fila distinta en cada uno y
   * la barra de DEX del strike 517 no se apoya en la línea de 517 del centro.
   *
   * Nadie ve esos cuatro píxeles mirando la cabecera; se ven leyendo un muro
   * contra su barra, que es para lo que existe esta pantalla. Por eso se mide en
   * marcha y se publica: una alineación rota deja de ser invisible.
   */
  function alignment() {
    const h = id => { const n = document.getElementById(id); return n ? Math.round(n.clientHeight) : null; };
    const left = h('traceLeft'), main = h('traceMain'), right = h('traceRight');
    const alturas = [left, main, right].filter(v => v !== null);
    const ok = alturas.length === 3 && new Set(alturas).size === 1;
    return {
      ok, left, main, right,
      max_delta_px: alturas.length ? Math.max.apply(null, alturas) - Math.min.apply(null, alturas) : null,
      price_lo: S.priceLo.get(), price_hi: S.priceHi.get(),
      detail: ok ? 'los tres paneles comparten el mismo eje de precio'
        : 'los lienzos no miden lo mismo: el mismo precio cae en filas distintas',
    };
  }

  global.ITMQTrace = {
    mount, applyData, setMetric, setBreakdown, hasBreakdown, setHeatField, setHeatOpacity, setPriceMode, setFollow, toggle,
    // Expuesto para poder VERIFICAR el recorte del campo contra una matriz
    // ancha como la del proveedor, que en demo no existe.
    buildHeatBitmapForTest: buildHeatBitmap,
    setIntervalGreek, intervalGreeks, alignment,
    strikeRow, onStrike(cb) { S.onStrike = cb; },
    clearStrike() { S.pinnedStrike = NaN; invalidateAll(); },
    state: S, METRICS, HEATFIELDS,
  };
})(window);
