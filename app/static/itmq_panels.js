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
      // v1.48.0 · Por defecto, UNA barra por strike. El strike es la unidad de
      // lectura de un perfil de exposición: una barra que dice «516…518» obliga
      // a abrir el hover para saber cuál de los tres tiene el muro, que es
      // justo lo que se estaba mirando. El panel crece —`ITMQBars.extentFor`—
      // y el contenedor hace scroll, así que no hay que elegir entre resolución
      // y grosor.
      const fit = fitBars(data, b.h, { aggregate: o.aggregate || 'none',
                                       targetThickness: o.targetBar,
                                       maxThickness: o.maxBar || 26 });
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

  /* --------------------------------------------- perfil en relieve (3D) */

  /**
   * El MISMO perfil por strike, en proyección isométrica.
   *
   * No sustituye a las barras: responde a otra pregunta. Las barras comparan
   * magnitudes con precisión —cuál es mayor, cuánto—; el relieve enseña la
   * FORMA de la estructura: dónde está la masa, cómo de abrupto es el borde y
   * en qué strike cambia el signo. Con ciento sesenta strikes eso no se lee en
   * una lista de barras aunque cada una sea legible.
   *
   * Es una proyección, no una simulación: no hay cámara libre ni perspectiva
   * que deforme magnitudes. Dos ejes reales —strike y valor— y una profundidad
   * constante que sólo da volumen, así que una barra el doble de larga sigue
   * midiendo el doble en pantalla.
   *
   * data: [{label, value, color?}]
   */
  function relief(host, opts) {
    const o = Object.assign({ fmt: v => Q.signedCompact(v, 2), depth: 0.34 }, opts || {});
    let data = [];
    const maxG = new Q.GlideValue(280);

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!data.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin || { l: 58, r: 20, t: 16, b: 26 });

      /* El relieve SÍ agrupa, y por una razón distinta a la de las barras.
       *
       * Las barras responden «cuánto vale este strike» y por eso conservan uno
       * por uno, con el panel creciendo. El relieve responde «qué forma tiene
       * la estructura», y para eso no hace falta —ni cabe— una cara por
       * strike: con ciento sesenta y seis en un panel fijo cada una mediría dos
       * píxeles y la forma se perdería en el ruido. Se agrupa conservando el
       * EXTREMO, así que una cresta sigue siendo una cresta.
       */
      const plan = AB.bin(data, b.h - Math.min(b.h * o.depth, b.w * 0.20),
                          { aggregate: 'extreme', targetThickness: o.targetBar || 9 });
      const rowsR = plan.rows;
      const values = rowsR.map(d => Q.num(d.value));
      const scale = robustPeak(values);
      maxG.set(scale.peak);
      const mx = Math.max(Q.num(maxG.get(), 0), 1e-9);

      // Profundidad total reservada al eje isométrico. El resto del lienzo es
      // el plano de strike × valor, que es donde se miden las magnitudes.
      const dz = Math.min(b.h * o.depth, b.w * 0.20);
      const plotH = b.h - dz;
      const plotW = b.w - dz;
      const n = rowsR.length;
      const pitch = plotH / n;
      const bh = Math.max(1.5, pitch * AB.FILL);
      const zero = b.x + plotW * 0.5;
      const sx = v => zero + Q.clamp(v / mx, -1, 1) * (plotW * 0.5);

      // Suelo: la referencia sin la que un relieve flota y no se puede situar.
      ctx.save();
      ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.85);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(b.x, b.y + plotH);
      ctx.lineTo(b.x + plotW, b.y + plotH);
      ctx.lineTo(b.x + plotW + dz, b.y + plotH - dz);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(Math.round(zero) + 0.5, b.y);
      ctx.lineTo(Math.round(zero) + 0.5, b.y + plotH);
      ctx.lineTo(Math.round(zero + dz) + 0.5, b.y + plotH - dz);
      ctx.stroke();
      ctx.restore();

      // De atrás hacia delante: el strike más alto se dibuja primero para que
      // el de delante lo tape, que es lo que produce la sensación de relieve.
      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      for (let i = n - 1; i >= 0; i--) {
        const d = rowsR[i];
        const v = Q.num(d.value);
        if (!v) continue;
        // El desplazamiento isométrico crece con el índice: los strikes altos
        // quedan al fondo y los bajos al frente.
        const t = i / Math.max(1, n - 1);
        const ox = dz * t, oy = -dz * t;
        const y = b.y + plotH - (i + 1) * pitch + oy;
        const x0 = Math.min(zero, sx(v)) + ox;
        const w = Math.max(1.5, Math.abs(sx(v) - zero));
        const col = d.color || colorFor(v, o);

        // Cara superior y lateral: dan el volumen sin inventar perspectiva.
        const lift = Math.min(bh * 0.55, dz * 0.14);
        ctx.fillStyle = Q.alpha(col, 0.22);
        ctx.beginPath();
        ctx.moveTo(x0, y); ctx.lineTo(x0 + w, y);
        ctx.lineTo(x0 + w + lift, y - lift); ctx.lineTo(x0 + lift, y - lift);
        ctx.closePath(); ctx.fill();
        ctx.fillStyle = Q.alpha(col, 0.92);
        ctx.fillRect(x0, y, w, bh);
        ctx.strokeStyle = Q.alpha(col, 1);
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.round(x0) + 0.5, Math.round(y) + 0.5,
                       Math.max(1, Math.round(w) - 1), Math.max(1, Math.round(bh) - 1));
        if (scale.clipped && Math.abs(v) > mx) {
          clipMark(ctx, v >= 0 ? b.x + plotW - 2 : b.x + 8, y, 1.5, bh, false, col);
        }
      }
      ctx.restore();

      // Ejes: sólo las etiquetas que caben, sobre el strike real.
      ctx.save();
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      const skip = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(plotH / 14))));
      for (let i = 0; i < n; i++) {
        if (i % skip) continue;
        const t = i / Math.max(1, n - 1);
        // La etiqueta se queda en el margen: seguir el desplazamiento
        // isométrico la metía dentro del gráfico y tapaba las propias barras.
        // Sólo sigue la altura, que es lo que la ata a su fila.
        const lbl = rowsR[i];
        ctx.fillText(String(lbl.grouped > 1 ? lbl.from : lbl.label),
                     b.x - 6, b.y + plotH - (i + 0.5) * pitch - dz * t);
      }
      ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      ctx.fillText(o.fmt(-mx), b.x, b.y + plotH + 6);
      ctx.textAlign = 'right';
      ctx.fillText(o.fmt(mx), b.x + plotW, b.y + plotH + 6);
      ctx.restore();

      if (panel.pointer.inside) {
        // El puntero se mapea deshaciendo el desplazamiento isométrico, así que
        // señala el strike que se ve, no el que estaría sin relieve.
        let hit = -1, bestD = Infinity;
        for (let i = 0; i < n; i++) {
          const t = i / Math.max(1, n - 1);
          const cy = b.y + plotH - (i + 0.5) * pitch - dz * t;
          const d = Math.abs(panel.pointer.y - cy);
          if (d < bestD) { bestD = d; hit = i; }
        }
        if (hit >= 0 && bestD <= Math.max(pitch, 6)) {
          const d = rowsR[hit];
          Q.chip(ctx, Q.clamp(panel.pointer.x + 10, b.x, b.x + b.w - 190),
                 Q.clamp(panel.pointer.y - 10, b.y + 8, b.y + b.h - 8),
                 AB.describe(d, o.fmt),
                 { bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 18 });
        }
      }
      return maxG.step(env.dt);
    }, { id: (host.id || 'relief') + ':relief',
         onPointer: () => panel.invalidate(), onPointerLeave: () => panel.invalidate() });

    return {
      panel,
      set(rows) { data = Array.isArray(rows) ? rows : []; panel.animate(true); panel.invalidate(); },
      setEmpty(msg) { o.empty = msg || o.empty; panel.invalidate(); },
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
  /** '#rrggbb' → [r,g,b], con respaldo si el token no es hexadecimal. */
  function _rgb(hex, fallback) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
    if (!m) return fallback;
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function heatmap(host, opts) {
    const o = Object.assign({ fmtY: v => v.toFixed(2), fmtX: v => String(v) }, opts || {});
    let rows = [], cols = [], matrix = [], price = [], peak = 0;
    let fieldCanvas = null, fieldCtx = null, fieldImage = null;

    function rescale() {
      peak = 0;
      for (const r of matrix) for (const v of r) peak = Math.max(peak, Math.abs(Q.num(v)));
    }

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!rows.length || !cols.length || peak <= 0) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin || { l: 62, r: 52, t: 12, b: 26 });

      /* v1.48.0 · Superficie continua, no una tabla pintada.
       *
       * La exposición por strike y tiempo es un CAMPO: varía de forma continua
       * entre strikes vecinos y entre intervalos vecinos. Dibujarla como una
       * rejilla de celdas duras con huecos negros entre ellas es pintar una
       * tabla, y obliga a leer celda por celda justo lo que hay que leer como
       * zona: dónde está la concentración, qué forma tiene y hacia dónde se
       * mueve.
       *
       * El campo se prepara en `ITMQBars.field` —huecos rellenados desde sus
       * vecinas, suavizado gaussiano y normalización por rango con signo— y se
       * pinta aquí con un lienzo a resolución de celda que el propio navegador
       * escala con interpolación bilineal. Así la superficie es continua a
       * cualquier tamaño sin dibujar una celda por píxel.
       */
      const f = AB.field(matrix, { blur: o.blur, fillRadius: o.fillRadius });
      if (!f.w || !f.h) { noData(ctx, env, o.empty); return false; }
      // Percentil por debajo del cual el campo es fondo, no zona.
      const noiseFloor = o.noiseFloor === undefined ? 0.55 : o.noiseFloor;
      const gammaCurve = o.gamma === undefined ? 1.9 : o.gamma;

      const posC = Q.token('--pos', '#22c55e');
      const negC = Q.token('--neg', '#ef4444');
      const rgbPos = _rgb(posC, [34, 197, 94]);
      const rgbNeg = _rgb(negC, [239, 68, 68]);

      // Lienzo del campo, una muestra por celda. Se reutiliza entre fotogramas:
      // reasignarlo en cada uno dispara el recolector sesenta veces por segundo.
      if (!fieldCanvas || fieldCanvas.width !== f.w || fieldCanvas.height !== f.h) {
        fieldCanvas = document.createElement('canvas');
        fieldCanvas.width = f.w; fieldCanvas.height = f.h;
        fieldCtx = fieldCanvas.getContext('2d');
        fieldImage = fieldCtx.createImageData(f.w, f.h);
      }
      const px = fieldImage.data;
      for (let y = 0; y < f.h; y++) {
        // La fila 0 del dato es el strike más bajo y el eje Y crece hacia
        // arriba, así que el lienzo se llena invertido.
        const srcRow = (f.h - 1 - y) * f.w;
        for (let x = 0; x < f.w; x++) {
          const v = f.values[srcRow + x];
          const i = (y * f.w + x) * 4;
          const c = v >= 0 ? rgbPos : rgbNeg;
          px[i] = c[0]; px[i + 1] = c[1]; px[i + 2] = c[2];
          /* Suelo de ruido y rampa: el mapa tiene que estar MAYORMENTE vacío.
           *
           * La normalización por rango reparte los percentiles de forma
           * uniforme, así que sin suelo la mitad del lienzo sale con media
           * opacidad y el resultado es un bloque macizo de verde y rojo donde
           * no se distingue ninguna concentración: lo contrario de un mapa.
           *
           * Por debajo de `floor` no se pinta nada —es el fondo del campo, no
           * una zona— y por encima la rampa es rápida, así que sólo los
           * percentiles altos llegan a saturar. Lo que queda saturado ES la
           * concentración, y por eso se ve dónde está.
           */
          const a = (f.intensity(v) - noiseFloor) / (1 - noiseFloor);
          px[i + 3] = a <= 0 ? 0
            : Math.round(255 * Math.min(1, Math.pow(a, gammaCurve)));
        }
      }
      fieldCtx.putImageData(fieldImage, 0, 0);

      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(fieldCanvas, 0, 0, f.w, f.h, b.x, b.y, b.w, b.h);
      ctx.restore();

      // Contornos de las zonas dominantes. Marcan el borde de una concentración
      // sin taparla, que es lo que convierte una mancha en una lectura.
      if (o.contours !== false) {
        ctx.save();
        ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
        ctx.lineWidth = 1;
        const cw0 = b.w / f.w, ch0 = b.h / f.h;
        for (const level of (o.contourLevels || [0.80, 0.93])) {
          ctx.strokeStyle = Q.alpha(Q.token('--text', '#e6edf7'), level >= 0.8 ? 0.22 : 0.12);
          /* Cruces en LOS DOS ejes.
           *
           * Barriendo sólo en X se obtienen segmentos verticales sueltos: la
           * isolínea no se cierra y el resultado parece ruido en vez de un
           * borde. Con los cruces horizontales y verticales el contorno rodea
           * la zona, que es lo que hace legible dónde empieza y dónde acaba
           * una concentración.
           */
          const at = (x, y) => f.intensity(f.values[(f.h - 1 - y) * f.w + x]);
          ctx.beginPath();
          for (let y = 0; y < f.h; y++) {
            for (let x = 0; x < f.w - 1; x++) {
              const a0 = at(x, y), a1 = at(x + 1, y);
              if ((a0 < level) === (a1 < level)) continue;
              const t = (level - a0) / ((a1 - a0) || 1e-9);
              const xx = b.x + (x + 0.5 + t) * cw0;
              const yy = b.y + (y + 0.5) * ch0;
              ctx.moveTo(xx, yy - ch0 * 0.5);
              ctx.lineTo(xx, yy + ch0 * 0.5);
            }
          }
          for (let x = 0; x < f.w; x++) {
            for (let y = 0; y < f.h - 1; y++) {
              const a0 = at(x, y), a1 = at(x, y + 1);
              if ((a0 < level) === (a1 < level)) continue;
              const t = (level - a0) / ((a1 - a0) || 1e-9);
              const yy = b.y + (y + 0.5 + t) * ch0;
              const xx = b.x + (x + 0.5) * cw0;
              ctx.moveTo(xx - cw0 * 0.5, yy);
              ctx.lineTo(xx + cw0 * 0.5, yy);
            }
          }
          ctx.stroke();
        }
        ctx.restore();
      }

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
      // Los ejes recorren el dato ORIGINAL: el campo es continuo, así que cada
      // etiqueta cae en su strike y en su intervalo reales, sin agrupaciones
      // que traducir.
      const chY = b.h / rows.length;
      const cwX = b.w / cols.length;
      const skipY = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(b.h / 16))));
      for (let i = 0; i < rows.length; i++) {
        if (i % skipY) continue;
        ctx.fillText(o.fmtY(Q.num(rows[i])), b.x - 6, b.y + b.h - (i + 0.5) * chY);
      }
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      const skipX = Math.max(1, Math.ceil(cols.length / Math.max(1, Math.floor(b.w / 64))));
      for (let i = 0; i < cols.length; i++) {
        if (i % skipX) continue;
        ctx.fillText(o.fmtX(cols[i]), b.x + (i + 0.5) * cwX, b.y + b.h + 6);
      }
      ctx.restore();

      if (panel.pointer.inside) {
        const ix = Q.clamp(Math.floor((panel.pointer.x - b.x) / cwX), 0, cols.length - 1);
        const iy = Q.clamp(rows.length - 1 - Math.floor((panel.pointer.y - b.y) / chY),
                           0, rows.length - 1);
        // Se enseña el valor MEDIDO de esa celda, no el suavizado: el suavizado
        // sirve para ver la forma, no para leer magnitudes.
        const v = Q.num((matrix[iy] || [])[ix]);
        const measured = f.measured(ix, iy);
        // Retícula fina sobre la celda apuntada: sin ella, en un campo continuo
        // no se sabe qué celda se está leyendo.
        ctx.save();
        ctx.strokeStyle = Q.alpha(Q.token('--text', '#e6edf7'), 0.35);
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.round(b.x + ix * cwX) + 0.5,
                       Math.round(b.y + b.h - (iy + 1) * chY) + 0.5,
                       Math.max(2, Math.round(cwX)), Math.max(2, Math.round(chY)));
        ctx.restore();
        Q.chip(ctx, Q.clamp(panel.pointer.x + 8, b.x, b.x + b.w - 210), b.y + 12,
          `${o.fmtY(Q.num(rows[iy]))} · ${o.fmtX(cols[ix])} · ` +
          (measured ? Q.signedCompact(v, 2) : 'sin observación'),
          { bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 18 });
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

  global.ITMQPanels = { bars, hbars, lines, tbars, curve, pricePrints, heatmap, relief };
})(window);
