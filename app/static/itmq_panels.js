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
  // Alto minimo que necesita una etiqueta de 9 px para no tocar a la vecina.
  const LABEL_MIN_PX = 11;

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

      /* v1.51.0 · La etiqueta se saltaba un strike de cada dos.
       *
       * El salto se calculaba contra un paso FIJO de 15 px, pero el panel de
       * strikes CRECE para dar a cada strike su propia fila, y ese paso es el que
       * manda. Con filas de 13 px y un divisor de 15 el resultado era siempre
       * saltar una: en pantalla se veian las barras de todos los strikes y los
       * numeros de la mitad, que es justo lo que obliga a contar a ojo.
       *
       * Ahora el salto sale del paso REAL: si la fila da para escribir, se escribe.
       * Sigue habiendo salto cuando el panel no crece —un panel fijo y apretado—,
       * porque ahi solapar los numeros seria peor que espaciarlos. */
      const skip = Math.max(1, Math.ceil(LABEL_MIN_PX / Math.max(1, slot)));
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
        /* v1.51.0 · `sy(Q.num(p.v, 0))` mandaba al SUELO del eje todo punto sin
         * valor. En la deriva de volatilidad eso dibujaba una sierra que bajaba a
         * 0.00 y volvia a subir: una IV de cero es imposible, y lo que habia en
         * esos instantes era un hueco, no una lectura.
         *
         * Un hueco parte el trazo. La linea vuelve a empezar despues, y se ve que
         * falta un tramo en vez de leerse como un desplome. */
        const segs = [];
        let cur = [];
        for (const p of s.points) {
          const x = sx(Q.parseTime(p.t));
          const v = Q.num(p.v, NaN);
          if (!Q.isNum(x) || !Q.isNum(v)) { if (cur.length) { segs.push(cur); cur = []; } continue; }
          cur.push({ x, y: sy(v) });
        }
        if (cur.length) segs.push(cur);
        if (!segs.length) continue;
        for (const pts of segs) {
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
          // Un tramo de un solo punto no tiene linea que dibujar: se marca.
          if (pts.length === 1) {
            ctx.beginPath(); ctx.arc(pts[0].x, pts[0].y, 3.2, 0, Math.PI * 2);
            ctx.fillStyle = col; ctx.fill();
          }
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
        const v = Q.num(p.v, NaN);
        // Un hueco no es una barra de altura cero: no se apila en el contenedor,
        // porque una barra de cero sigue dibujando su presencia minima y se leeria
        // como «aqui se midio y salio cero».
        if (!Q.isNum(t) || !Q.isNum(v)) continue;
        rows.push({ label: Q.hhmm(t), value: v, t });
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
  /* Relieve isométrico del perfil por strike.
   *
   * v1.51.0 · La versión anterior era ilegible y hay que decir por qué, porque el
   * mismo error es fácil de repetir: dibujaba caras de dos o tres píxeles —el paso
   * de fila salía del alto del panel dividido entre TODAS las filas— y remataba
   * con dos polilíneas de suelo que, al no cerrar ninguna superficie, se leían
   * como rayas sueltas cruzando el gráfico.
   *
   * Un relieve se entiende cuando tiene tres cosas, y las tres se construyen aquí:
   *
   *   1. VOLUMEN REAL. Cada strike es un prisma con cara frontal, cara superior y
   *      tapa lateral, sombreadas desde UNA fuente de luz. Sin las tres caras no
   *      hay volumen: hay una barra con un borde.
   *   2. SUELO. Un plano en fuga con sus líneas de valor. Sin suelo el relieve
   *      flota y no se puede situar a qué altura está cada cresta.
   *   3. PASO SUFICIENTE. Se agrupa hasta que cada prisma mide lo bastante para
   *      verse; con ciento sesenta strikes en un panel fijo no cabe uno por fila,
   *      y forzarlo es lo que producía las láminas de dos píxeles. El extremo se
   *      conserva al agrupar, así que una cresta sigue siendo una cresta.
   *
   * La profundidad se acota en PÍXELES ABSOLUTOS además de en fracción: es la
   * fracción sin tope lo que, en un panel alto, mandaba la fuga fuera del lienzo.
   */
  const RELIEF_MIN_DEPTH = 26;
  const RELIEF_MAX_DEPTH = 96;
  const RELIEF_ROW_PX = 17;      // paso objetivo entre prismas
  const RELIEF_SKEW = 0.58;      // componente vertical de la fuga

  function relief(host, opts) {
    const o = Object.assign({ fmt: v => Q.signedCompact(v, 2), depth: 0.26 }, opts || {});
    let data = [];
    const maxG = new Q.GlideValue(280);

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!data.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin || { l: 62, r: 26, t: 18, b: 30 });
      if (b.w < 80 || b.h < 70) { noData(ctx, env, 'PANEL DEMASIADO PEQUEÑO'); return false; }

      // Profundidad: fracción del panel, pero acotada en píxeles por los dos lados.
      const dz = Q.clamp(Math.min(b.h * o.depth, b.w * 0.18),
                         RELIEF_MIN_DEPTH, RELIEF_MAX_DEPTH);
      const dy = dz * RELIEF_SKEW;
      const plotH = b.h - dy;
      const plotW = b.w - dz;
      if (plotH < 40 || plotW < 60) { noData(ctx, env, 'PANEL DEMASIADO PEQUEÑO'); return false; }

      /* v1.52.0 · UNA FILA POR STRIKE, igual que las barras.
       *
       * El relieve agrupaba «porque para ver la forma no hacen falta todas las
       * caras». Es cierto para la forma y falso para leer: con 62 strikes
       * agrupados de dos en dos el eje numeraba 540.00, 537.50, 535.00… y hay
       * que contar a ojo para saber en qué strike está cada cresta. El strike es
       * la unidad de lectura aquí también.
       *
       * Cuando el llamador hace crecer el panel —`aggregate: 'none'`— cada
       * strike tiene su fila y su etiqueta. Si el panel es fijo y no cabe, se
       * agrupa conservando el EXTREMO, porque láminas de dos píxeles serían
       * peor que agrupar. */
      const plan = AB.bin(data, plotH, {
        aggregate: o.aggregate || 'extreme',
        targetThickness: RELIEF_ROW_PX,
      });
      const rowsR = plan.rows;
      const n = rowsR.length;
      if (!n) { noData(ctx, env, o.empty); return false; }

      const values = rowsR.map(d => Q.num(d.value));
      const scale = robustPeak(values);
      maxG.set(scale.peak);
      const mx = Math.max(Q.num(maxG.get(), 0), 1e-9);

      const pitch = plotH / n;
      const bh = Math.max(4, pitch * 0.66);
      const zero = b.x + plotW * 0.5;
      const sx = v => zero + Q.clamp(v / mx, -1, 1) * (plotW * 0.5);
      // Fuga por fila: la de abajo al frente, la de arriba al fondo.
      const ox = i => dz * (i / Math.max(1, n - 1));
      const oy = i => -dy * (i / Math.max(1, n - 1));
      const rowY = i => b.y + plotH - (i + 1) * pitch + oy(i);

      /* ── 1 · Suelo en fuga ────────────────────────────────────────────── */
      const grid = Q.token('--grid', '#243044');
      ctx.save();
      const ticks = Q.niceTicks(-mx, mx, 5).filter(t => Math.abs(t) <= mx);
      ctx.strokeStyle = Q.alpha(grid, 0.5);
      ctx.lineWidth = 1;
      for (const t of ticks) {
        const x = sx(t);
        ctx.beginPath();
        ctx.moveTo(x, b.y + plotH);
        ctx.lineTo(x + dz, b.y + plotH - dy);
        ctx.stroke();
      }
      // Bordes del plano: frontal y del fondo, unidos por los laterales. Cerrar el
      // cuadrilátero es lo que hace que se lea como un SUELO y no como dos rayas.
      ctx.strokeStyle = Q.alpha(grid, 0.95);
      ctx.beginPath();
      ctx.moveTo(b.x, b.y + plotH);
      ctx.lineTo(b.x + plotW, b.y + plotH);
      ctx.lineTo(b.x + plotW + dz, b.y + plotH - dy);
      ctx.lineTo(b.x + dz, b.y + plotH - dy);
      ctx.closePath();
      ctx.stroke();
      // Plano del cero: la referencia de signo, vertical y en fuga.
      ctx.strokeStyle = Q.alpha(Q.token('--text-mute', '#5b6880'), 0.85);
      ctx.beginPath();
      ctx.moveTo(zero, b.y + plotH);
      ctx.lineTo(zero + dz, b.y + plotH - dy);
      ctx.stroke();
      ctx.restore();

      /* ── 2 · Prismas, del fondo al frente ─────────────────────────────── */
      ctx.save();
      ctx.beginPath(); ctx.rect(b.x - 2, b.y - 2, b.w + 4, b.h + 4); ctx.clip();
      const lift = Math.min(bh * 0.9, dz * 0.30);
      const liftY = lift * RELIEF_SKEW;
      for (let i = n - 1; i >= 0; i--) {
        const d = rowsR[i];
        const v = Q.num(d.value);
        if (!v) continue;
        const dx = ox(i);
        const y = rowY(i);
        const xa = Math.min(zero, sx(v)) + dx;
        const w = Math.max(2, Math.abs(sx(v) - zero));
        const col = d.color || colorFor(v, o);

        // Cara SUPERIOR: la más clara. Es la que da la sensación de altura.
        ctx.fillStyle = Q.alpha(col, 0.55);
        ctx.beginPath();
        ctx.moveTo(xa, y);
        ctx.lineTo(xa + w, y);
        ctx.lineTo(xa + w + lift, y - liftY);
        ctx.lineTo(xa + lift, y - liftY);
        ctx.closePath(); ctx.fill();

        // Tapa LATERAL del extremo: la más oscura, en el lado hacia el que crece.
        const capX = v >= 0 ? xa + w : xa;
        ctx.fillStyle = Q.alpha(col, 0.30);
        ctx.beginPath();
        ctx.moveTo(capX, y);
        ctx.lineTo(capX + lift, y - liftY);
        ctx.lineTo(capX + lift, y - liftY + bh);
        ctx.lineTo(capX, y + bh);
        ctx.closePath(); ctx.fill();

        // Cara FRONTAL: la que se mide contra la escala.
        ctx.fillStyle = Q.alpha(col, 0.95);
        ctx.fillRect(xa, y, w, bh);
        ctx.strokeStyle = Q.alpha(col, 1);
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.round(xa) + 0.5, Math.round(y) + 0.5,
                       Math.max(1, Math.round(w) - 1), Math.max(1, Math.round(bh) - 1));

        if (scale.clipped && Math.abs(v) > mx) {
          clipMark(ctx, v >= 0 ? b.x + plotW - 2 + dx : b.x + 8 + dx, y, 1.5, bh, false, col);
        }
      }
      ctx.restore();

      /* ── 3 · Ejes ─────────────────────────────────────────────────────── */
      ctx.save();
      ctx.font = '10px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      // El salto sale del PASO REAL, no de un divisor fijo: si la fila da para
      // escribir el número, se escribe. Con el panel crecido eso es siempre.
      const step = Math.max(1, Math.ceil(LABEL_MIN_PX / Math.max(1, pitch)));
      for (let i = 0; i < n; i++) {
        if (i % step) continue;
        const lbl = rowsR[i];
        // La etiqueta se queda en el margen y sólo sigue la ALTURA de su fila:
        // seguir también la fuga la metía dentro del gráfico tapando los prismas.
        ctx.fillText(String(lbl.grouped > 1 ? lbl.from : lbl.label), b.x - 8, rowY(i) + bh / 2);
      }
      // Escala de valor sobre el borde frontal del suelo.
      ctx.textBaseline = 'top';
      for (const t of ticks) {
        ctx.textAlign = t === ticks[0] ? 'left' : (t === ticks[ticks.length - 1] ? 'right' : 'center');
        ctx.fillText(o.fmt(t), sx(t), b.y + plotH + 8);
      }
      ctx.restore();

      /* ── 4 · Puntero ──────────────────────────────────────────────────── */
      if (panel.pointer.inside) {
        let hit = -1, bestD = Infinity;
        for (let i = 0; i < n; i++) {
          const cy = rowY(i) + bh / 2;
          const d = Math.abs(panel.pointer.y - cy);
          if (d < bestD) { bestD = d; hit = i; }
        }
        if (hit >= 0 && bestD <= Math.max(pitch, 7)) {
          const d = rowsR[hit];
          Q.chip(ctx, Q.clamp(panel.pointer.x + 12, b.x, b.x + b.w - 200),
                 Q.clamp(panel.pointer.y - 10, b.y + 10, b.y + b.h - 10),
                 AB.describe(d, o.fmt),
                 { bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 19 });
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

  /* Ajuste del campo. Los MISMOS valores que usa el fondo del TRACE: el campo es
   * el mismo dato y tiene que leerse igual en las dos pantallas. Son propiedades
   * de lectura, no de activo — la normalización ya es relativa al propio activo. */
  const HEAT_NOISE = 0.62;
  const HEAT_GAMMA = 1.35;
  const HEAT_ALPHA_MAX = 0.70;
  const HEAT_LEVELS = [0.68, 0.80, 0.90, 0.96];
  const HEAT_BLUR = 1.9;

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
      const f = AB.field(matrix, { blur: o.blur === undefined ? HEAT_BLUR : o.blur, fillRadius: o.fillRadius });
      if (!f.w || !f.h) { noData(ctx, env, o.empty); return false; }
      // Percentil por debajo del cual el campo es fondo, no zona.
      const noiseFloor = o.noiseFloor === undefined ? HEAT_NOISE : o.noiseFloor;
      const gammaCurve = o.gamma === undefined ? HEAT_GAMMA : o.gamma;

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
            : Math.round(255 * HEAT_ALPHA_MAX * Math.min(1, Math.pow(a, gammaCurve)));
        }
      }
      fieldCtx.putImageData(fieldImage, 0, 0);

      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(fieldCanvas, 0, 0, f.w, f.h, b.x, b.y, b.w, b.h);
      ctx.restore();

      /* Isolíneas del campo.
       *
       * v1.51.0 · Antes esto barría cada eje por separado y pintaba un palito
       * suelto por cruce, sin unirlo con el de la celda vecina: una nube de
       * rayitas que se leía como suciedad. Ahora sale de `AB.contours`, marching
       * squares, que devuelve segmentos que se encuentran en los bordes
       * compartidos y forman una curva cerrada alrededor de la zona.
       *
       * Es el MISMO cálculo que usa el fondo del TRACE. Dos tratamientos del
       * mismo dato es lo que hacía que las dos pantallas no se parecieran.
       */
      if (o.contours !== false && AB.contours) {
        ctx.save();
        ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
        ctx.lineWidth = 1;
        ctx.lineCap = 'round';
        const cw0 = b.w / f.w, ch0 = b.h / f.h;
        for (const level of (o.contourLevels || HEAT_LEVELS)) {
          ctx.strokeStyle = Q.alpha(Q.token('--text', '#e6edf7'),
                                    0.30 * (0.55 + 0.45 * level));
          ctx.beginPath();
          for (const g of AB.contours(f, level)) {
            // El campo tiene la fila 0 abajo; el panel la tiene arriba.
            ctx.moveTo(b.x + (g[0] + 0.5) * cw0, b.y + b.h - (g[1] + 0.5) * ch0);
            ctx.lineTo(b.x + (g[2] + 0.5) * cw0, b.y + b.h - (g[3] + 0.5) * ch0);
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

  /* ------------------------------------------- mapa de intervalos por PUNTOS */

  /**
   * INTERVAL MAP con el diseño de la herramienta original: una rejilla de puntos,
   * uno por strike e intervalo, con el recorrido del precio encima.
   *
   * v1.51.0 · Esta sección tenía el mismo campo continuo que el fondo del TRACE, y
   * ahí el campo es la representación equivocada. Las dos pantallas no responden
   * la misma pregunta:
   *
   *   TRACE  · el mapa es FONDO. Va debajo de las velas y lo que hace falta es la
   *            FORMA de la zona: dónde empieza y dónde acaba la concentración.
   *            Un campo continuo con isolíneas es exactamente eso, y al ser
   *            translúcido deja ver el precio por encima.
   *   SECCIÓN· el mapa es el SUJETO. Aquí se viene a leer celda a celda: cuánto
   *            hay en ESTE strike en ESTE intervalo. Un degradado no permite eso
   *            —interpola entre vecinas y no se sabe dónde acaba una celda—,
   *            mientras que un punto por celda sí: su diámetro ES la magnitud y
   *            su hueco separa una celda de la siguiente.
   *
   * El dato es el mismo y la normalización también. Cambia la pregunta.
   */
  function dotmap(host, opts) {
    const o = Object.assign({
      fmtY: v => v.toFixed(2), fmtX: v => String(v),
      empty: 'SIN MAPA DE INTERVALOS',
    }, opts || {});
    let rows = [], cols = [], matrix = [], price = [];

    const panel = new Q.Panel(host, (ctx, env) => {
      if (!rows.length || !cols.length || !matrix.length) { noData(ctx, env, o.empty); return false; }
      const b = box(env, o.margin || { l: 62, r: 16, t: 14, b: 26 });
      if (b.w < 60 || b.h < 50) { noData(ctx, env, 'PANEL DEMASIADO PEQUEÑO'); return false; }

      const H = rows.length, W = cols.length;
      // La matriz puede llegar [strike][tiempo] o traspuesta: se detecta por
      // dimensiones, igual que en el resto del programa.
      const rowsAreStrikes = matrix.length === H;
      const val = (si, xi) => {
        const v = rowsAreStrikes ? (matrix[si] || [])[xi] : (matrix[xi] || [])[si];
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
      };

      // Se reutiliza el ranker del componente común SIN suavizar ni rellenar: aquí
      // cada celda tiene que seguir siendo ella misma.
      const grid2 = Array.from({ length: H }, (_, si) =>
        Array.from({ length: W }, (_, xi) => val(si, xi)));
      const f = AB.field(grid2, { blur: 0, fillRadius: 0 });

      const cw = b.w / W, ch = b.h / H;
      /* v1.52.1 · El punto necesita tamaño para que el DIÁMETRO signifique algo.
       *
       * Con 90 strikes × 81 intervalos en un panel de 420 px la celda mide 4.6 px
       * y el radio máximo salía en 2: todos los puntos parecían iguales y el
       * mapa se leía como una nube de motas. Si el diámetro es la magnitud,
       * hace falta rango de diámetros.
       *
       * El suelo de 2.6 px se aplica al MÁXIMO, no a cada punto: los pequeños
       * siguen siendo pequeños. Y el llamador hace crecer el panel, que es la
       * otra mitad —la misma solución que las barras por strike—. */
      const maxR = Math.max(2.6, Math.min(cw, ch) * 0.46);
      const pos = Q.token('--heat-pos', '#22c55e');
      const neg = Q.token('--heat-neg', '#ef4444');

      // Rejilla de fondo: sin ella los puntos flotan y no se sabe a qué fila van.
      ctx.save();
      ctx.strokeStyle = Q.alpha(Q.token('--grid', '#243044'), 0.35);
      ctx.lineWidth = 1;
      const yStep = Math.max(1, Math.ceil(H / Math.max(1, Math.floor(b.h / 26))));
      for (let si = 0; si < H; si += yStep) {
        const y = Math.round(b.y + b.h - (si + 0.5) * ch) + 0.5;
        ctx.beginPath(); ctx.moveTo(b.x, y); ctx.lineTo(b.x + b.w, y); ctx.stroke();
      }
      ctx.restore();

      ctx.save();
      ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
      for (let si = 0; si < H; si++) {
        const cy = b.y + b.h - (si + 0.5) * ch;
        for (let xi = 0; xi < W; xi++) {
          const v = val(si, xi);
          // Un hueco no se dibuja. Un cero MEDIDO sí: es una afirmación.
          if (v === null) continue;
          const rank = f.intensity(v);
          const r = Math.max(0.9, maxR * Math.pow(rank, 0.72));
          ctx.fillStyle = Q.alpha(v >= 0 ? pos : neg, 0.30 + 0.70 * rank);
          ctx.beginPath();
          ctx.arc(b.x + (xi + 0.5) * cw, cy, r, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      ctx.restore();

      // Precio sobre los MISMOS intervalos: el mapa dice dónde estaba la
      // exposición, y el precio por qué zonas pasó el mercado. Sin él hay que
      // cruzar dos pantallas a ojo.
      const lo = Q.num(rows[0]), hi = Q.num(rows[rows.length - 1]);
      if (price.length > 1 && hi > lo) {
        const yFor = v => b.y + b.h - ((Q.num(v) - lo) / (hi - lo)) * b.h;
        ctx.save();
        ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
        ctx.strokeStyle = Q.token('--accent', '#38bdf8');
        ctx.lineWidth = 1.8; ctx.lineJoin = 'round';
        ctx.beginPath();
        price.forEach((p, i) => {
          const x = b.x + ((i + 0.5) / Math.max(1, price.length)) * b.w;
          const y = yFor(p.v !== undefined ? p.v : p);
          if (!Q.isNum(y)) return;
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        });
        ctx.stroke();
        ctx.restore();
      }

      // Ejes
      ctx.save();
      ctx.font = '10px ui-monospace, monospace';
      ctx.fillStyle = Q.token('--text-dim', '#8494ad');
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      for (let si = 0; si < H; si += yStep) {
        ctx.fillText(o.fmtY(Q.num(rows[si])), b.x - 7, b.y + b.h - (si + 0.5) * ch);
      }
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      const xStep = Math.max(1, Math.ceil(W / Math.max(1, Math.floor(b.w / 64))));
      for (let xi = 0; xi < W; xi += xStep) {
        ctx.fillText(o.fmtX(cols[xi]), b.x + (xi + 0.5) * cw, b.y + b.h + 7);
      }
      ctx.restore();

      if (panel.pointer.inside && panel.pointer.x >= b.x && panel.pointer.x <= b.x + b.w
          && panel.pointer.y >= b.y && panel.pointer.y <= b.y + b.h) {
        const xi = Q.clamp(Math.floor((panel.pointer.x - b.x) / cw), 0, W - 1);
        const si = Q.clamp(Math.floor((b.y + b.h - panel.pointer.y) / ch), 0, H - 1);
        const v = val(si, xi);
        ctx.save();
        ctx.strokeStyle = Q.alpha(Q.token('--text', '#e6edf7'), 0.45);
        ctx.strokeRect(Math.round(b.x + xi * cw) + 0.5, Math.round(b.y + b.h - (si + 1) * ch) + 0.5,
                       Math.max(2, Math.round(cw)), Math.max(2, Math.round(ch)));
        ctx.restore();
        Q.chip(ctx, Q.clamp(panel.pointer.x + 9, b.x, b.x + b.w - 215), b.y + 12,
          `${o.fmtY(Q.num(rows[si]))} · ${o.fmtX(cols[xi])} · ` +
          (v === null ? 'sin observación' : Q.signedCompact(v, 2)),
          { bg: Q.token('--panel-3', '#1b2436'), color: Q.token('--text', '#e6edf7'), h: 18 });
      }
      return false;
    }, { id: (host.id || 'dots') + ':dotmap',
         onPointer: () => panel.invalidate(), onPointerLeave: () => panel.invalidate() });

    return {
      panel,
      set(y, x, m, p) {
        rows = Array.isArray(y) ? y : [];
        cols = Array.isArray(x) ? x : [];
        matrix = Array.isArray(m) ? m : [];
        price = Array.isArray(p) ? p : [];
        panel.invalidate();
      },
      setEmpty(msg) { o.empty = msg || o.empty; panel.invalidate(); },
    };
  }

  global.ITMQPanels = { bars, hbars, lines, tbars, curve, pricePrints, heatmap, dotmap, relief };
})(window);
