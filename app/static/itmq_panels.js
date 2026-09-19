/* ITM QUANT · Paneles genéricos v1.41.0
 *
 * Tipos de gráfico reutilizables por el resto de secciones. Todos usan el mismo
 * bucle de render del núcleo, así que comparten nitidez, redimensionado y
 * transiciones suaves sin código duplicado.
 *
 *   ITMQPanels.bars(host, opts)      barras verticales por categoría
 *   ITMQPanels.hbars(host, opts)     barras horizontales (perfil por strike)
 *   ITMQPanels.lines(host, opts)     una o varias series temporales
 *   ITMQPanels.tbars(host, opts)     barras firmadas sobre eje temporal
 *   ITMQPanels.curve(host, opts)     curva sobre eje numérico (skew, term)
 *   ITMQPanels.pricePrints(host,o)   precio + marcadores de operaciones
 */
(function (global) {
  'use strict';

  const Q = global.ITMQ;
  if (!Q) { console.error('[PANELS] falta itmq_core.js'); return; }

  const M = { l: 52, r: 16, t: 14, b: 26 };

  function box(env, m) {
    const mm = m || M;
    return { x: mm.l, y: mm.t, w: Math.max(1, env.w - mm.l - mm.r), h: Math.max(1, env.h - mm.t - mm.b) };
  }

  function noData(ctx, env, msg) {
    ctx.save();
    ctx.fillStyle = Q.token('--text-mute', '#5b6880');
    ctx.font = '11px ui-monospace, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(msg || 'SIN DATOS', env.w / 2, env.h / 2);
    ctx.restore();
  }

  function colorFor(v, opts) {
    if (opts && opts.color) return typeof opts.color === 'function' ? opts.color(v) : opts.color;
    return v >= 0 ? Q.token('--pos', '#22c55e') : Q.token('--neg', '#ef4444');
  }

  /* ---------------------------------------- legibilidad de las barras
   *
   * Tres defectos compartidos por `bars` y `hbars`, universales para todos los
   * activos, que hacían que una barra «existiera» sin verse:
   *
   *   1 · GROSOR MÍNIMO 1 px.  `Math.max(1, slot * 0.66)` deja slivers de un píxel
   *       en cuanto hay muchas categorías. Una cadena de 60 strikes en un panel de
   *       300 px daba 3.3 px de hueco y ~2 px de barra; a alta densidad, 1 px.
   *
   *   2 · ALTURA MÍNIMA 1 px.  Un valor real pero pequeño se dibujaba con un píxel,
   *       indistinguible de la línea de cero. El dato estaba; no se veía.
   *
   *   3 · ESCALA POR EL MÁXIMO.  `peak = max(|v|)`: un único strike dominante —que
   *       es lo NORMAL en un perfil de exposición— aplastaba el resto del perfil
   *       contra cero. Se veía una barra enorme y cuarenta invisibles, cuando la
   *       forma del perfil es justamente lo que hay que leer.
   *
   * La corrección es de PRESENTACIÓN y no toca el dato: la escala se comprime sólo
   * cuando la distribución está dominada por un atípico, y la barra que se sale se
   * marca como recortada en lugar de truncarse en silencio.
   */

  // v1.47.0 · El grosor, la agrupación y el suelo de visibilidad ya NO se
  // resuelven aquí. Vivían en tres sitios —`bars`, `hbars` y los carriles de
  // flujo— con tres constantes distintas, así que cada corrección había que
  // hacerla tres veces y sólo se hacía en dos. Ahora los sirve un único
  // componente, `ITMQBars`, y una corrección futura beneficia a todos los
  // paneles de barras del programa a la vez.
  const AB = global.ITMQBars;
  if (!AB) { console.error('[PANELS] falta itmq_adaptive_bars.js'); return; }

  // A partir de esta razón máx/p90, la distribución está dominada por un atípico y
  // escalar por el máximo esconde todo lo demás.
  const OUTLIER_RATIO = 8.0;

  function quantileAbs(values, q) {
    const v = values.filter(x => Number.isFinite(x)).map(Math.abs).sort((a, b) => a - b);
    if (!v.length) return 0;
    const i = Q.clamp((v.length - 1) * q, 0, v.length - 1);
    const lo = Math.floor(i), hi = Math.ceil(i);
    return lo === hi ? v[lo] : v[lo] + (v[hi] - v[lo]) * (i - lo);
  }

  /** Escala robusta: respeta el máximo salvo que un atípico aplaste al resto. */
  function robustPeak(values) {
    const finite = values.filter(Number.isFinite).map(Math.abs);
    const peak = finite.length ? Math.max.apply(null, finite) : 0;
    if (!(peak > 0)) return { peak: 1, clipped: false, raw: peak };
    const p90 = quantileAbs(finite, 0.90);
    if (p90 > 0 && peak / p90 > OUTLIER_RATIO) {
      // Se deja headroom sobre el p90 para que el perfil se lea, y lo que se sale
      // se marca. Comprimir sin avisar sería mentir sobre la magnitud.
      return { peak: p90 * 2.2, clipped: true, raw: peak };
    }
    return { peak, clipped: false, raw: peak };
  }

  /* La agrupación, el grosor y el suelo de visibilidad los sirve `ITMQBars`.
   *
   * Lo que había aquí era el defecto de raíz: `fitBars` agrupaba hasta dejar
   * barras de MIN_BAR_PX de PASO, y `barThickness` aplicaba después un 0.82
   * sobre ese paso. El resultado medía menos que el mínimo que el código creía
   * estar garantizando, y con 390 buckets en 330 px salían rayitas de 2.46 px.
   *
   * El suelo se aplica ahora al PASO —que es lo que tiene que caber— y la
   * agrupación se dispara cuando el grosor OBJETIVO no cabe, no cuando ya no
   * cabe el mínimo.
   */
  function fitBars(data, extentPx, opts) {
    const plan = AB.bin(data, extentPx, opts);
    return { rows: plan.rows, grouped: plan.aggregated ? plan.group : 0, plan };
  }

  /** Marca de barra recortada: dice que el valor se sale de la escala. */
  function clipMark(ctx, x, y, w, h, vertical, color) {
    ctx.save();
    ctx.fillStyle = Q.alpha(color, 0.95);
    if (vertical) {
      for (let i = 0; i < 3; i++) ctx.fillRect(x, y + i * 3, w, 1.5);
    } else {
      for (let i = 0; i < 3; i++) ctx.fillRect(x - i * 3 - 1.5, y, 1.5, h);
    }
    ctx.restore();
  }

  /* ------------------------------------------------ barras verticales */

  /** data: [{label, value, color?}] */
  function bars(host, opts) {
    const o = Object.assign({ fmt: v => Q.compact(v, 1), zeroCenter: true }, opts || {});
    const glide = new Q.Glide(150);
    // v1.46.0 · Sin valor inicial. `GlideValue` sin inicial adopta el PRIMER pico
    // real de golpe y sólo anima los cambios posteriores; con un inicial de `1` el
    // primer fotograma se dibujaba siempre contra una escala de un dólar, así que
    // todas las barras salían saturadas hasta que la animación convergía. En un
    // panel que alcanza a dibujar un solo fotograma —quince paneles compitiendo
    // por el mismo bucle— esa escala falsa era lo único que se veía, y el efecto
    // dependía del tamaño del activo: un `1` es invisible junto a 10⁷ y es TODA
    // la escala junto a 10².
    const maxG = new Q.GlideValue(280);
    let data = [];

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!data.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin);
      // Se agrupa ANTES de escalar: si no, la escala se calcula sobre puntos que
      // no se van a dibujar y el eje no corresponde a lo que se ve.
      // Histograma temporal: los contenedores SUMAN. «Lo que pasó en estos tres
      // minutos» es la suma, no el máximo; el pico del grupo se conserva aparte
      // para que una anomalía no quede escondida dentro de su intervalo.
      const fit = fitBars(data, b.w, { aggregate: o.aggregate || 'sum', maxThickness: o.maxBar });
      const draw = fit.rows;
      let hasNeg = false;
      const values = draw.map(d => { const v = Q.num(d.value); if (v < 0) hasNeg = true; return v; });
      const scale = robustPeak(values);
      maxG.set(scale.peak);
      const mx = Math.max(maxG.get(), 1e-9);
      const centered = o.zeroCenter && hasNeg;
      const sy = centered ? Q.scale(-mx * 1.1, mx * 1.1, b.y + b.h, b.y) : Q.scale(0, mx * 1.12, b.y + b.h, b.y);

      Q.gridY(ctx, b, sy, Q.niceTicks(sy.domain[0], sy.domain[1], 5), o.fmt, { labelSide: 'left' });

      const y0 = sy(0);
      const slot = fit.plan.pitch;
      const bw = fit.plan.thickness;

      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      draw.forEach((d, i) => {
        // Los grupos no llevan transición: su clave cambia con el zoom y animar
        // entre agrupaciones distintas produce un barrido que no significa nada.
        const v = fit.grouped ? Q.num(d.value) : glide.get(d.key !== undefined ? d.key : i);
        const cx = b.x + slot * (i + 0.5);
        const y = sy(v);
        const col = d.color || colorFor(v, o);
        const h = AB.extent(y - y0, v, b.h);
        const top = v >= 0 ? y0 - h : y0;
        // Relleno más opaco y un borde del mismo color: a grosor pequeño el borde
        // es lo que separa una barra de la de al lado.
        ctx.fillStyle = Q.alpha(col, 0.95);
        ctx.fillRect(cx - bw / 2, top, bw, h);
        if (bw >= 4) {
          ctx.strokeStyle = Q.alpha(col, 1);
          ctx.lineWidth = 1;
          ctx.strokeRect(Math.round(cx - bw / 2) + 0.5, Math.round(top) + 0.5,
                         Math.round(bw) - 1, Math.max(1, Math.round(h) - 1));
        }
        if (scale.clipped && Math.abs(v) > mx) {
          clipMark(ctx, cx - bw / 2, v >= 0 ? b.y + 2 : b.y + b.h - 10, bw, 1.5, true, col);
        }
        if (d.peak_dominates) {
          // El grupo suma poco porque sus signos se compensan, pero dentro hubo
          // un evento grande. Se marca su alcance para no esconderlo.
          const py = sy(Q.num(d.peak));
          ctx.fillStyle = Q.alpha(colorFor(Q.num(d.peak), o), 0.55);
          ctx.fillRect(cx - bw / 2, py - 1, bw, 2);
        }
      });
      ctx.restore();

      // etiquetas del eje X, saltando las que no caben
      ctx.save();
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      const skip = Math.max(1, Math.ceil(draw.length / Math.max(1, Math.floor(b.w / 46))));
      draw.forEach((d, i) => {
        if (i % skip) return;
        ctx.fillText(String(d.label), b.x + slot * (i + 0.5), b.y + b.h + 6);
      });
      ctx.restore();

      // valor bajo el cursor
      if (panel.pointer.inside) {
        const i = Math.floor((panel.pointer.x - b.x) / slot);
        if (i >= 0 && i < draw.length) {
          const d = draw[i];
          ctx.save();
          ctx.fillStyle = Q.alpha(Q.token('--text', '#e6edf7'), 0.07);
          ctx.fillRect(b.x + slot * i, b.y, slot, b.h);
          ctx.restore();
          Q.chip(ctx, Q.clamp(b.x + slot * (i + .5), b.x + 60, b.x + b.w - 60), b.y + 10,
            AB.describe(d, o.fmt),
            { align: 'center', bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 18 });
        }
      }
      const a = glide.step(env.dt), b2 = maxG.step(env.dt);
      return a || b2;
    }, { id: (host.id || 'bars') + ':bars', onPointer: () => panel.invalidate(), onPointerLeave: () => panel.invalidate() });

    return {
      panel,
      set(rows) {
        data = Array.isArray(rows) ? rows : [];
        glide.setAll(data.map((d, i) => [d.key !== undefined ? d.key : i, Q.num(d.value)]));
        panel.animate(true); panel.invalidate();
      },
      // v1.47.0 · El motivo de un panel vacío puede cambiar entre ciclos —«aún no
      // hay sesión» y «hay agregados pero no desglose» no son lo mismo— y el
      // caché de paneles congelaba el texto del primer render.
      setEmpty(msg) { o.empty = msg || o.empty; panel.invalidate(); },
    };
  }

  /* ---------------------------------------------- barras horizontales */

  /** data: [{label, value}] — perfil por strike con el eje de valor abajo. */
  function hbars(host, opts) {
    const o = Object.assign({ fmt: v => Q.signedCompact(v, 1) }, opts || {});
    const glide = new Q.Glide(150);
    // v1.46.0 · Sin valor inicial. `GlideValue` sin inicial adopta el PRIMER pico
    // real de golpe y sólo anima los cambios posteriores; con un inicial de `1` el
    // primer fotograma se dibujaba siempre contra una escala de un dólar, así que
    // todas las barras salían saturadas hasta que la animación convergía. En un
    // panel que alcanza a dibujar un solo fotograma —quince paneles compitiendo
    // por el mismo bucle— esa escala falsa era lo único que se veía, y el efecto
    // dependía del tamaño del activo: un `1` es invisible junto a 10⁷ y es TODA
    // la escala junto a 10².
    const maxG = new Q.GlideValue(280);
    let data = [];

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!data.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin || { l: 62, r: 16, t: 10, b: 24 });
      // Perfil por strike: el contenedor muestra el valor EXTREMO del grupo, que
      // es un valor que existió de verdad. Una media podría cancelar un +8 con
      // un −8 vecinos y hacer desaparecer la concentración justo donde importa.
      const fit = fitBars(data, b.h, { aggregate: o.aggregate || 'extreme', maxThickness: o.maxBar || 22 });
      const draw = fit.rows;
      let hasNeg = false;
      const values = draw.map(d => { const v = Q.num(d.value); if (v < 0) hasNeg = true; return v; });
      const scale = robustPeak(values);
      maxG.set(scale.peak);
      const mx = Math.max(maxG.get(), 1e-9);
      const sx = hasNeg ? Q.scale(-mx * 1.05, mx * 1.05, b.x, b.x + b.w) : Q.scale(0, mx * 1.05, b.x, b.x + b.w);
      const zero = sx(0);
      const slot = fit.plan.pitch;
      const bh = fit.plan.thickness;

      ctx.save();
      ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.9);
      ctx.beginPath(); ctx.moveTo(Math.round(zero) + .5, b.y); ctx.lineTo(Math.round(zero) + .5, b.y + b.h); ctx.stroke();
      ctx.restore();

      const skip = Math.max(1, Math.ceil(draw.length / Math.max(1, Math.floor(b.h / 15))));
      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      draw.forEach((d, i) => {
        const cy = b.y + slot * (i + 0.5);
        const v = fit.grouped ? Q.num(d.value) : glide.get(d.key !== undefined ? d.key : i);
        const x = Q.clamp(sx(v), b.x, b.x + b.w);
        const col = d.color || colorFor(v, o);
        const w = AB.extent(x - zero, v, b.w);
        const left = v >= 0 ? zero : zero - w;
        ctx.fillStyle = Q.alpha(col, 0.95);
        ctx.fillRect(left, cy - bh / 2, w, bh);
        if (bh >= 4) {
          ctx.strokeStyle = Q.alpha(col, 1);
          ctx.lineWidth = 1;
          ctx.strokeRect(Math.round(left) + 0.5, Math.round(cy - bh / 2) + 0.5,
                         Math.max(1, Math.round(w) - 1), Math.round(bh) - 1);
        }
        if (scale.clipped && Math.abs(v) > mx) {
          clipMark(ctx, v >= 0 ? b.x + b.w - 2 : b.x + 8, cy - bh / 2, 1.5, bh, false, col);
        }
      });
      ctx.restore();
      ctx.save();
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      draw.forEach((d, i) => {
        if (i % skip) return;
        ctx.fillText(String(d.label), b.x - 6, b.y + slot * (i + 0.5));
      });
      ctx.restore();

      ctx.save();
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textBaseline = 'top';
      ctx.textAlign = 'left'; ctx.fillText(o.fmt(sx.domain[0]), b.x, b.y + b.h + 5);
      ctx.textAlign = 'right'; ctx.fillText(o.fmt(sx.domain[1]), b.x + b.w, b.y + b.h + 5);
      ctx.restore();

      if (panel.pointer.inside) {
        const i = Math.floor((panel.pointer.y - b.y) / slot);
        if (i >= 0 && i < draw.length) {
          const d = draw[i];
          // Si en pantalla pone «514…516», el hover dice cuántos strikes agrupa y
          // cuál es el extremo: la agrupación es visual, el dato sigue entero.
          Q.chip(ctx, b.x + b.w - 4, b.y + slot * (i + .5), AB.describe(d, o.fmt),
            { align: 'right', bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7') });
        }
      }
      const a = glide.step(env.dt), b2 = maxG.step(env.dt);
      return a || b2;
    }, { id: (host.id || 'hbars') + ':hbars', onPointer: () => panel.invalidate(), onPointerLeave: () => panel.invalidate() });

    return {
      panel,
      set(rows) {
        data = Array.isArray(rows) ? rows : [];
        glide.setAll(data.map((d, i) => [d.key !== undefined ? d.key : i, Q.num(d.value)]));
        panel.animate(true); panel.invalidate();
      },
      // v1.47.0 · El motivo de un panel vacío puede cambiar entre ciclos —«aún no
      // hay sesión» y «hay agregados pero no desglose» no son lo mismo— y el
      // caché de paneles congelaba el texto del primer render.
      setEmpty(msg) { o.empty = msg || o.empty; panel.invalidate(); },
    };
  }

  /* ---------------------------------------------------- series temporales */

  /** series: [{name, color, points:[{t, v}]}] */
  function lines(host, opts) {
    const o = Object.assign({ fmt: v => Q.compact(v, 1), fill: false, zeroLine: true }, opts || {});
    let series = [];

    const panel = new Q.Panel(host, (ctx, env) => {
      const usable = series.filter(s => (s.points || []).length);
      if (!usable.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin);

      let t0 = Infinity, t1 = -Infinity, lo = Infinity, hi = -Infinity;
      for (const s of usable) for (const p of s.points) {
        const t = Q.parseTime(p.t), v = Q.num(p.v, NaN);
        if (!Q.isNum(t) || !Q.isNum(v)) continue;
        t0 = Math.min(t0, t); t1 = Math.max(t1, t); lo = Math.min(lo, v); hi = Math.max(hi, v);
      }
      if (!Q.isNum(t0) || !Q.isNum(t1)) { noData(ctx, env, o.empty); return false; }
      // Una única observación sigue siendo un dato real. Antes t0===t1 se trataba
      // como «NO DISPONIBLE», por lo que Max Pain quedaba vacío durante el primer
      // ciclo aunque el KPI de arriba ya tuviera valor. Se abre una ventana visual
      // alrededor del instante sin inventar un segundo valor.
      if (t1 <= t0) { t0 -= 30_000; t1 += 30_000; }
      if (o.zeroLine) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      const pad = Math.max((hi - lo) * 0.1, Math.abs(hi) * 1e-4, 1e-9);
      const sx = Q.scale(t0, t1, b.x, b.x + b.w);
      const sy = Q.scale(lo - pad, hi + pad, b.y + b.h, b.y);

      Q.gridY(ctx, b, sy, Q.niceTicks(lo - pad, hi + pad, 5), o.fmt, { labelSide: 'left' });
      Q.axisX(ctx, b, sx, t0, t1, { grid: true });

      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      for (const s of usable) {
        const col = s.color || Q.token('--accent', '#38bdf8');
        const pts = s.points.map(p => ({ x: sx(Q.parseTime(p.t)), y: sy(Q.num(p.v, 0)) }))
          .filter(p => Q.isNum(p.x) && Q.isNum(p.y));
        if (!pts.length) continue;
        if (o.fill || s.fill) {
          ctx.beginPath();
          pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
          ctx.lineTo(pts[pts.length - 1].x, sy(0));
          ctx.lineTo(pts[0].x, sy(0));
          ctx.closePath();
          ctx.fillStyle = Q.alpha(col, 0.16); ctx.fill();
        }
        ctx.beginPath();
        pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
        ctx.strokeStyle = col; ctx.lineWidth = s.width || 1.5; ctx.lineJoin = 'round'; ctx.stroke();
        if (pts.length === 1) {
          ctx.beginPath(); ctx.arc(pts[0].x, pts[0].y, 3.2, 0, Math.PI * 2);
          ctx.fillStyle = col; ctx.fill();
        }
      }
      ctx.restore();

      // leyenda
      ctx.save();
      ctx.font = '10px ui-monospace, monospace';
      ctx.textBaseline = 'middle';
      let lx = b.x + 4;
      for (const s of usable) {
        if (!s.name) continue;
        const col = s.color || Q.token('--accent', '#38bdf8');
        ctx.fillStyle = col; ctx.fillRect(lx, b.y + 2, 8, 3);
        ctx.fillStyle = Q.token('--text-dim', '#8494ad');
        ctx.textAlign = 'left';
        ctx.fillText(s.name, lx + 12, b.y + 4);
        lx += ctx.measureText(s.name).width + 26;
      }
      ctx.restore();

      if (panel.pointer.inside && panel.pointer.x >= b.x && panel.pointer.x <= b.x + b.w) {
        const t = sx.invert(panel.pointer.x);
        ctx.save();
        ctx.strokeStyle = Q.alpha(Q.token('--text-dim', '#8494ad'), 0.5);
        ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(Math.round(panel.pointer.x) + .5, b.y); ctx.lineTo(Math.round(panel.pointer.x) + .5, b.y + b.h); ctx.stroke();
        ctx.restore();
        const parts = [Q.hhmm(t)];
        for (const s of usable) {
          let near = null, bd = Infinity;
          for (const p of s.points) { const d = Math.abs(Q.parseTime(p.t) - t); if (d < bd) { bd = d; near = p; } }
          if (near) parts.push(`${s.name || ''} ${o.fmt(Q.num(near.v))}`.trim());
        }
        Q.chip(ctx, Q.clamp(panel.pointer.x + 8, b.x, b.x + b.w - 190), b.y + 14, parts.join('  ·  '),
          { bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 18 });
      }
      return false;
    }, { id: (host.id || 'lines') + ':lines', onPointer: () => panel.invalidate(), onPointerLeave: () => panel.invalidate() });

    return {
      panel,
      set(s) { series = Array.isArray(s) ? s : []; panel.invalidate(); },
      // El motivo de un panel vacío puede cambiar entre ciclos; el caché de
      // paneles congelaba el texto del primer render.
      setEmpty(msg) { o.empty = msg || o.empty; panel.invalidate(); },
    };
  }

  /* ------------------------------------------- barras sobre eje temporal */

  /** points: [{t, v}] */
  function tbars(host, opts) {
    const o = Object.assign({ fmt: v => Q.compact(v, 1), bucketMs: 60_000 }, opts || {});
    let points = [];
    // v1.46.0 · Sin valor inicial. `GlideValue` sin inicial adopta el PRIMER pico
    // real de golpe y sólo anima los cambios posteriores; con un inicial de `1` el
    // primer fotograma se dibujaba siempre contra una escala de un dólar, así que
    // todas las barras salían saturadas hasta que la animación convergía. En un
    // panel que alcanza a dibujar un solo fotograma —quince paneles compitiendo
    // por el mismo bucle— esa escala falsa era lo único que se veía, y el efecto
    // dependía del tamaño del activo: un `1` es invisible junto a 10⁷ y es TODA
    // la escala junto a 10².
    const maxG = new Q.GlideValue(280);

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!points.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin);
      let t0 = Infinity, t1 = -Infinity, peak = 0;
      for (const p of points) {
        const t = Q.parseTime(p.t), v = Q.num(p.v, 0);
        if (!Q.isNum(t)) continue;
        t0 = Math.min(t0, t); t1 = Math.max(t1, t); peak = Math.max(peak, Math.abs(v));
      }
      if (!Q.isNum(t0)) { noData(ctx, env, o.empty); return false; }
      maxG.set(peak > 0 ? peak : 1);
      const mx = Math.max(maxG.get(), 1e-9);
      const sx = Q.scale(t0, t1 + o.bucketMs, b.x, b.x + b.w);
      const sy = Q.scale(-mx * 1.1, mx * 1.1, b.y + b.h, b.y);

      Q.gridY(ctx, b, sy, Q.niceTicks(-mx * 1.1, mx * 1.1, 5), o.fmt, { labelSide: 'left' });
      Q.axisX(ctx, b, sx, t0, t1 + o.bucketMs, { grid: false });

      const y0 = sy(0);
      // v1.47.0 · `(sx(t0+bucket) - sx(t0)) * 0.7` es el mismo error que tenían
      // los otros paneles: con una sesión entera a un minuto el paso vale menos
      // de un píxel y las barras se solapan en una masa sólida. El componente
      // común agrupa temporalmente hasta que cada barra tiene presencia, y suma
      // dentro de cada intervalo.
      const rows = [];
      for (const p of points) {
        const t = Q.parseTime(p.t);
        if (!Q.isNum(t)) continue;
        rows.push({ label: Q.hhmm(t), value: Q.num(p.v, 0), t });
      }
      rows.sort((a, c) => a.t - c.t);
      const plan = AB.bin(rows, b.w, { aggregate: 'sum', maxThickness: o.maxBar });
      const bw = plan.thickness;
      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      plan.rows.forEach((d, i) => {
        const v = Q.num(d.value, 0);
        if (!v) return;
        const x = b.x + plan.pitch * (i + 0.5), y = sy(v);
        ctx.fillStyle = Q.alpha(colorFor(v, o), 0.88);
        ctx.fillRect(x - bw / 2, Math.min(y, y0), bw, AB.extent(y - y0, v, b.h));
      });
      ctx.restore();
      return maxG.step(env.dt);
    }, { id: (host.id || 'tbars') + ':tbars' });

    return { panel, set(p) { points = Array.isArray(p) ? p : []; panel.animate(true); panel.invalidate(); } };
  }

  /* ------------------------------------------- curva sobre eje numérico */

  /** series: [{name, color, points:[{x, y}]}] — skew, term structure */
  function curve(host, opts) {
    const o = Object.assign({ fmtY: v => v.toFixed(1), fmtX: v => String(v), dots: true }, opts || {});
    let series = [];

    const panel = new Q.Panel(host, (ctx, env) => {
      const usable = series.filter(s => (s.points || []).length);
      if (!usable.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin);
      let x0 = Infinity, x1 = -Infinity, lo = Infinity, hi = -Infinity;
      for (const s of usable) for (const p of s.points) {
        const x = Q.num(p.x, NaN), y = Q.num(p.y, NaN);
        if (!Q.isNum(x) || !Q.isNum(y)) continue;
        x0 = Math.min(x0, x); x1 = Math.max(x1, x); lo = Math.min(lo, y); hi = Math.max(hi, y);
      }
      if (!Q.isNum(x0) || x1 <= x0) { noData(ctx, env, o.empty); return false; }
      const pad = Math.max((hi - lo) * 0.14, 1e-6);
      const sx = Q.scale(x0, x1, b.x, b.x + b.w);
      const sy = Q.scale(lo - pad, hi + pad, b.y + b.h, b.y);

      Q.gridY(ctx, b, sy, Q.niceTicks(lo - pad, hi + pad, 5), o.fmtY, { labelSide: 'left' });

      ctx.save();
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.5);
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      for (const t of Q.niceTicks(x0, x1, 6)) {
        const x = Math.round(sx(t)) + .5;
        if (x < b.x || x > b.x + b.w) continue;
        ctx.beginPath(); ctx.moveTo(x, b.y); ctx.lineTo(x, b.y + b.h); ctx.stroke();
        ctx.fillText(o.fmtX(t), x, b.y + b.h + 5);
      }
      ctx.restore();

      for (const s of usable) {
        const col = s.color || Q.token('--accent', '#38bdf8');
        const pts = s.points.slice().sort((a, c) => Q.num(a.x) - Q.num(c.x))
          .map(p => ({ x: sx(Q.num(p.x)), y: sy(Q.num(p.y)) }));
        ctx.save();
        ctx.beginPath();
        pts.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
        ctx.strokeStyle = col; ctx.lineWidth = 1.7; ctx.lineJoin = 'round'; ctx.stroke();
        if (o.dots) {
          ctx.fillStyle = col;
          for (const p of pts) { ctx.beginPath(); ctx.arc(p.x, p.y, 2.4, 0, Math.PI * 2); ctx.fill(); }
        }
        ctx.restore();
      }

      ctx.save();
      ctx.font = '10px ui-monospace, monospace';
      ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
      let lx = b.x + 4;
      for (const s of usable) {
        if (!s.name) continue;
        ctx.fillStyle = s.color || Q.token('--accent', '#38bdf8');
        ctx.fillRect(lx, b.y + 2, 8, 3);
        ctx.fillStyle = Q.token('--text-dim', '#8494ad');
        ctx.fillText(s.name, lx + 12, b.y + 4);
        lx += ctx.measureText(s.name).width + 26;
      }
      ctx.restore();
      return false;
    }, { id: (host.id || 'curve') + ':curve' });

    return {
      panel,
      set(s) { series = Array.isArray(s) ? s : []; panel.invalidate(); },
      // El motivo de un panel vacío puede cambiar entre ciclos; el caché de
      // paneles congelaba el texto del primer render.
      setEmpty(msg) { o.empty = msg || o.empty; panel.invalidate(); },
    };
  }

  /* --------------------------------------------- precio + operaciones */

  /** candles: [{t,o,h,l,c}] · marks: [{t, price, size, side}] */
  function pricePrints(host, opts) {
    const o = Object.assign({ fmt: v => v.toFixed(2) }, opts || {});
    let candles = [], marks = [], levels = [];

    const panel = new Q.Panel(host, (ctx, env) => {
      // Las operaciones observadas valen por sí solas: si la curva de precio aún
      // no ha llegado, se dibujan igual en vez de dejar el panel en blanco.
      if (!candles.length && !marks.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin || { l: 16, r: 58, t: 12, b: 24 });
      const ts = candles.map(c => Q.parseTime(c.t)).filter(Q.isNum)
        .concat(marks.map(m => Q.parseTime(m.t)).filter(Q.isNum));
      if (!ts.length) { noData(ctx, env, o.empty); return false; }
      let t0 = Math.min.apply(null, ts), t1 = Math.max.apply(null, ts);
      if (t1 - t0 < 60_000) { t0 -= 300_000; t1 += 300_000; }
      let lo = Infinity, hi = -Infinity;
      for (const c of candles) { lo = Math.min(lo, Q.num(c.l, Q.num(c.c))); hi = Math.max(hi, Q.num(c.h, Q.num(c.c))); }
      for (const m of marks) { const p = Q.num(m.price, NaN); if (Q.isNum(p)) { lo = Math.min(lo, p); hi = Math.max(hi, p); } }
      for (const lv of levels) { const p = Q.num(lv.price, NaN); if (Q.isNum(p)) { lo = Math.min(lo, p); hi = Math.max(hi, p); } }
      const pad = Math.max((hi - lo) * 0.1, 0.02);
      const sx = Q.scale(t0, t1, b.x, b.x + b.w);
      const sy = Q.scale(lo - pad, hi + pad, b.y + b.h, b.y);

      Q.gridY(ctx, b, sy, Q.niceTicks(lo - pad, hi + pad, 6), o.fmt, { labelSide: 'right' });
      Q.axisX(ctx, b, sx, t0, t1, { grid: false });

      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      if (candles.length) {
        const col = Q.token('--price', '#7aa2f7');
        ctx.beginPath();
        candles.forEach((c, i) => {
          const x = sx(Q.parseTime(c.t)), y = sy(Q.num(c.c));
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        });
        ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.stroke();
      }

      let peak = 0;
      for (const m of marks) peak = Math.max(peak, Math.abs(Q.num(m.size)));
      for (const m of marks) {
        const t = Q.parseTime(m.t), p = Q.num(m.price, NaN), sz = Math.abs(Q.num(m.size));
        if (!Q.isNum(t) || !Q.isNum(p) || peak <= 0) continue;
        const r = 2 + (sz / peak) * 6;
        const c2 = m.side === 'BUY' ? Q.token('--pos', '#22c55e') : m.side === 'SELL' ? Q.token('--neg', '#ef4444') : Q.token('--gold', '#d9a441');
        ctx.beginPath(); ctx.arc(sx(t), sy(p), r, 0, Math.PI * 2);
        ctx.fillStyle = Q.alpha(c2, 0.5); ctx.fill();
        ctx.strokeStyle = Q.alpha(c2, 0.9); ctx.lineWidth = 1; ctx.stroke();
      }
      ctx.restore();

      for (const lv of levels.slice(0, 6)) {
        const p = Q.num(lv.price, NaN);
        if (!Q.isNum(p)) continue;
        const y = sy(p);
        if (y < b.y || y > b.y + b.h) continue;
        Q.levelLine(ctx, b, y, Q.alpha(Q.token('--gold', '#d9a441'), 0.55));
        Q.chip(ctx, b.x + 6, y, `${lv.name || ''} ${p.toFixed(2)}`.trim(),
          { bg: Q.alpha(Q.token('--gold', '#d9a441'), 0.85), color: '#06101c' });
      }
      return false;
    }, { id: (host.id || 'pp') + ':pp' });

    return {
      panel,
      set(c, m, lv) {
        candles = Array.isArray(c) ? c : [];
        marks = Array.isArray(m) ? m : [];
        levels = Array.isArray(lv) ? lv : [];
        panel.invalidate();
      },
    };
  }

  /* ------------------------------------------------ mapa de intervalos 2D */

  /**
   * matrix[fila][columna] con filas = strikes (eje Y) y columnas = intervalos (eje X).
   * Se dibuja como rejilla de puntos: el radio y la opacidad codifican la magnitud y
   * el color el signo. Un punto por celda se lee mejor que un degradado continuo
   * cuando la exposición está concentrada en pocos strikes, que es el caso normal.
   * `price` superpone el recorrido del precio sobre los mismos intervalos: sin él
   * el mapa dice dónde estaba la exposición, pero no por qué zonas pasó el mercado.
   */
  function heatmap(host, opts) {
    const o = Object.assign({ fmtY: v => v.toFixed(2), fmtX: v => String(v) }, opts || {});
    let rows = [], cols = [], matrix = [], price = [], peak = 0;

    function rescale() {
      peak = 0;
      for (const r of matrix) for (const v of r) peak = Math.max(peak, Math.abs(Q.num(v)));
    }

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!rows.length || !cols.length || peak <= 0) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin || { l: 62, r: 52, t: 12, b: 26 });

      /* v1.47.0 · Zonas, no puntos.
       *
       * Antes cada celda era un CÍRCULO de radio `min(cw,ch)*0.46`. Con 90
       * strikes en 250 px eso da 1.3 px, y la intensidad era `|v| / max`: una
       * normalización lineal que, con una cadena concentrada, deja casi todo por
       * debajo del umbral de 0.02 y sin pintar. El mapa se veía como puntos
       * diminutos y dispersos teniendo 7.290 observaciones.
       *
       * Ahora la celda se agrupa hasta tener tamaño real y se pinta RELLENA, así
       * que una concentración se lee como una zona continua y no como una nube.
       * La intensidad es por rango dentro de lo visible, que es lo que hace que
       * el mapa se lea igual en un ETF enorme y en una acción pequeña.
       */
      const g = AB.grid(matrix, b.w, b.h);
      const cw = g.cw, ch = g.ch;
      const pos = Q.token('--pos', '#22c55e');
      const neg = Q.token('--neg', '#ef4444');
      ctx.save();
      for (let y = 0; y < g.rows; y++) {
        const src = g.cells[y] || [];
        // La fila 0 es el strike más bajo: el eje Y crece hacia arriba.
        const cy = b.y + b.h - (y + 1) * ch;
        for (let x = 0; x < g.cols; x++) {
          const v = Q.num(src[x]);
          if (!v) continue;
          const a = g.intensity(v);
          if (a < 0.12) continue;
          ctx.globalAlpha = 0.14 + 0.86 * Math.pow(a, 0.75);
          ctx.fillStyle = v >= 0 ? pos : neg;
          ctx.fillRect(b.x + x * cw, cy, Math.max(1, cw), Math.max(1, ch));
        }
      }
      ctx.globalAlpha = 1;
      ctx.restore();

      // Precio sobre los mismos strikes, en la escala del eje Y.
      const lo = Q.num(rows[0]), hi = Q.num(rows[rows.length - 1]);
      if (price.length > 1 && hi > lo) {
        const yFor = v => b.y + b.h - ((Q.num(v) - lo) / (hi - lo)) * b.h;
        ctx.save();
        ctx.strokeStyle = Q.token('--accent', '#38bdf8');
        ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
        ctx.beginPath();
        price.forEach((p, i) => {
          const x = b.x + (i / Math.max(1, price.length - 1)) * b.w;
          const y = Q.clamp(yFor(p.v), b.y, b.y + b.h);
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        });
        ctx.stroke();
        const last = price[price.length - 1];
        Q.chip(ctx, b.x + b.w + 4, Q.clamp(yFor(last.v), b.y + 8, b.y + b.h - 8),
          Q.num(last.v).toFixed(2),
          { bg: Q.token('--accent', '#38bdf8'), color: '#04121f', h: 15 });
        ctx.restore();
      }

      ctx.save();
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      // Los ejes recorren las celdas DIBUJADAS y etiquetan con el valor original
      // del primer elemento de cada bloque: si la celda agrupa tres strikes, la
      // etiqueta es la del primero y el hover dice el rango completo.
      const skipY = Math.max(1, Math.ceil(g.rows / Math.max(1, Math.floor(b.h / 16))));
      for (let i = 0; i < g.rows; i++) {
        if (i % skipY) continue;
        const src = rows[i * g.rowGroup];
        if (src === undefined) continue;
        ctx.fillText(o.fmtY(Q.num(src)), b.x - 6, b.y + b.h - (i + 0.5) * ch);
      }
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      const skipX = Math.max(1, Math.ceil(g.cols / Math.max(1, Math.floor(b.w / 64))));
      for (let i = 0; i < g.cols; i++) {
        if (i % skipX) continue;
        const src = cols[i * g.colGroup];
        if (src === undefined) continue;
        ctx.fillText(o.fmtX(src), b.x + (i + 0.5) * cw, b.y + b.h + 6);
      }
      ctx.restore();

      if (panel.pointer.inside) {
        const ix = Math.floor((panel.pointer.x - b.x) / cw);
        const iy = g.rows - 1 - Math.floor((panel.pointer.y - b.y) / ch);
        if (ix >= 0 && ix < g.cols && iy >= 0 && iy < g.rows) {
          // El valor que se enseña es el EXTREMO del bloque —el que se dibujó—
          // y se dice de cuántas observaciones originales sale.
          const v = Q.num((g.cells[iy] || [])[ix]);
          const y0 = rows[iy * g.rowGroup], y1 = rows[Math.min(rows.length - 1, (iy + 1) * g.rowGroup - 1)];
          const x0 = cols[ix * g.colGroup], x1 = cols[Math.min(cols.length - 1, (ix + 1) * g.colGroup - 1)];
          const yTxt = g.rowGroup > 1 ? `${o.fmtY(Q.num(y0))}…${o.fmtY(Q.num(y1))}` : o.fmtY(Q.num(y0));
          const xTxt = g.colGroup > 1 ? `${o.fmtX(x0)}…${o.fmtX(x1)}` : o.fmtX(x0);
          const n = g.rowGroup * g.colGroup;
          Q.chip(ctx, Q.clamp(panel.pointer.x + 8, b.x, b.x + b.w - 210), b.y + 12,
            `${yTxt} · ${xTxt} · ${Q.signedCompact(v, 2)}${n > 1 ? ` · máx de ${n}` : ''}`,
            { bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 18 });
        }
      }
      return false;
    }, { id: (host.id || 'heat') + ':heat', onPointer: () => panel.invalidate(), onPointerLeave: () => panel.invalidate() });

    return {
      panel,
      set(y, x, m, p) {
        rows = Array.isArray(y) ? y : [];
        cols = Array.isArray(x) ? x : [];
        matrix = Array.isArray(m) ? m : [];
        price = Array.isArray(p) ? p : [];
        rescale();
        panel.invalidate();
      },
    };
  }

  global.ITMQPanels = { bars, hbars, lines, tbars, curve, pricePrints, heatmap };
})(window);
