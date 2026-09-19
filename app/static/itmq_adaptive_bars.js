/* ITM QUANT · AdaptiveBarProfile — renderizado adaptativo de barras (v1.47.0)
 *
 * EL ERROR QUE ESTE MÓDULO ELIMINA
 * --------------------------------
 * Hasta v1.46.0, cada panel de barras resolvía la densidad **adelgazando**:
 *
 *     barWidth = max(MIN_BAR_PX, slot * 0.82)
 *
 * Con un suelo de 3 px y 390 buckets en 300 px, `fitBars` agrupaba hasta dejar
 * 100 barras… de 3 px de PASO. El 0.82 se aplicaba después, así que la barra
 * medía 2.46 px: por debajo del suelo que se creía estar garantizando. El suelo
 * nunca se alcanzaba porque se aplicaba al sitio equivocado.
 *
 * Y el enfoque en sí no puede funcionar. Hay dos situaciones distintas:
 *
 *     20 barras en 300 px   →  caben gruesas, con holgura
 *     390 barras en 300 px  →  a 4 px serían 1560 px de contenido en 300
 *
 * No existe un grosor fijo correcto para las dos. La única salida es:
 *
 *     pocas barras  →  gruesas directamente
 *     muchas barras →  AGRUPAR visualmente  →  y entonces gruesas
 *
 * nunca «cada vez más finas hasta desaparecer».
 *
 * CÓMO SE CALCULA
 * ---------------
 * Del espacio de pantalla, no de constantes por sección ni por activo:
 *
 *     pitchMínimo = MIN_BAR_PX / FILL        paso que hace falta para que una
 *                                            barra legible quepa CON su hueco
 *     binsMáximos = floor(extentPx / pitchMínimo)
 *     si n > binsMáximos → se agrupa en binsMáximos contenedores visuales
 *     pitch     = extentPx / bins
 *     grosor    = clamp(pitch * FILL, MIN_BAR_PX, maxThickness)
 *
 * El grosor sale del sitio disponible y de la densidad. No hay ningún `if
 * (symbol === …)`, ningún número por sección y ningún píxel escrito a mano en
 * los paneles que lo usan.
 *
 * DATO ANALÍTICO ≠ REPRESENTACIÓN VISUAL
 * --------------------------------------
 * La agrupación es EXCLUSIVAMENTE una optimización de renderizado. El dataset
 * original no se toca, y cada contenedor conserva lo necesario para que no se
 * pierda nada al mirarlo:
 *
 *     rango (de … a …) · magnitud · signo · extremo · cuántos agrupa · miembros
 *
 * El `hover` inspecciona los valores originales. Si en pantalla pone «514…516»,
 * ahí siguen 514, 515 y 516 por separado.
 *
 * POR QUÉ NO UNA MEDIA
 * --------------------
 * Una media simple cancela: un +8 y un −8 vecinos dan 0 y la concentración
 * desaparece justo donde importa. Dos modos, según lo que signifique el eje:
 *
 *     'extreme'  perfiles por strike. El contenedor muestra el valor MÁS GRANDE
 *                en magnitud, que es un valor que existió de verdad. Una
 *                concentración extrema sigue viéndose aunque caiga dentro de un
 *                grupo.
 *     'sum'      histogramas temporales. Volumen, prima, flujo neto y conteos se
 *                SUMAN, porque «lo que pasó en estos tres minutos» es la suma y
 *                no el máximo. Se conserva además el pico del grupo, y se marca
 *                cuando el pico domina a la suma, para que una anomalía no
 *                quede escondida dentro de su intervalo.
 */
(function (global) {
  'use strict';

  const Q = global.ITMQ;
  if (!Q) { console.error('[ADAPTIVE-BARS] falta itmq_core.js'); return; }

  /* ── Constantes de LEGIBILIDAD, no de activo ──────────────────────────────
   *
   * Son propiedades de la vista humana —a qué grosor una barra deja de leerse
   * como barra—, no de ningún ticker, y por eso son las mismas para todos. Lo
   * que varía por activo y por panel es cuántas barras caben, y eso se DERIVA.
   */

  // Fracción del paso que ocupa la barra. El resto es el hueco que la separa de
  // su vecina; sin hueco, cuarenta barras son un bloque sólido.
  const FILL = 0.76;

  // Por debajo de esto una barra deja de tener presencia: se lee como una raya.
  // Cuando no cabe, NO se baja de aquí — se agrupa.
  const MIN_BAR_PX = 5;

  // Grosor al que una barra se distingue cómodamente sin acercarse a la
  // pantalla. Es un objetivo, no un valor impuesto: si caben más finas pero por
  // encima del mínimo, se respeta la resolución del dato.
  const TARGET_BAR_PX = 8;

  // Tope para que veinte observaciones en un panel ancho no produzcan bloques.
  const MAX_BAR_PX = 34;

  // Suelo de VISIBILIDAD de un valor pequeño pero real. Dice «aquí hay algo»,
  // nunca «aquí hay mucho»: por eso está acotado también como fracción del eje,
  // para que no pueda parecerse a una barra grande en un panel bajo.
  const MIN_EXTENT_PX = 3;
  const MAX_EXTENT_FRACTION = 0.06;

  function _num(v) { const x = Number(v); return Number.isFinite(x) ? x : 0; }

  /**
   * Cuántos contenedores visuales caben, y de qué grosor.
   *
   * @param {number} count      observaciones del dataset
   * @param {number} extentPx   píxeles disponibles en el eje de categorías
   * @param {object} opts       {maxThickness, minThickness, fill}
   */
  function layout(count, extentPx, opts) {
    const o = opts || {};
    const fill = o.fill || FILL;
    const minPx = o.minThickness || MIN_BAR_PX;
    const maxPx = o.maxThickness || MAX_BAR_PX;
    const span = Math.max(1, _num(extentPx));
    const n = Math.max(0, Math.floor(count));
    if (!n) return { bins: 0, group: 1, pitch: span, thickness: minPx, gap: 0, aggregated: false };

    /* Se elige la agrupación cuyo grosor queda MÁS CERCA del objetivo, nunca por
     * debajo del mínimo legible.
     *
     * Aplicar el suelo al PASO y no al grosor es la corrección de fondo: antes
     * el 0.76 se aplicaba después, así que la barra acababa por debajo del
     * mínimo que el código creía estar garantizando.
     *
     * Y la agrupación no puede ser «el primer grupo que alcanza el objetivo»,
     * porque `ceil(n/g)` da saltos: con 30 observaciones en 300 px, no agrupar
     * da 7.6 px —perfectamente legible— y agrupar de dos en dos salta a 15.2 px,
     * que desperdicia la mitad de la resolución para ganar un grosor que no
     * hacía falta. Elegir por cercanía al objetivo conserva la resolución
     * siempre que el resultado ya se lea bien, y agrupa cuando de verdad hace
     * falta: 45 strikes en 300 px pasan de 5.07 px a 9.9 px.
     */
    const target = Math.max(minPx, o.targetThickness || TARGET_BAR_PX);
    const thicknessFor = g => (span / Math.ceil(n / g)) * fill;
    let group = 1;
    let best = Infinity;
    for (let g = 1; g <= n; g++) {
      const t = thicknessFor(g);
      // Por debajo del mínimo no es candidato: ahí es donde hay que agrupar más.
      if (t < minPx && g < n) continue;
      const d = Math.abs(t - target);
      if (d < best) { best = d; group = g; }
      // `thicknessFor` crece con `g`, así que una vez pasado el objetivo la
      // distancia sólo puede empeorar.
      if (t >= target) break;
    }
    const bins = Math.ceil(n / group);
    const pitch = span / bins;
    const thickness = Q.clamp(pitch * fill, Math.min(minPx, pitch), maxPx);
    return {
      bins, group, pitch, thickness,
      gap: Math.max(0, pitch - thickness),
      aggregated: group > 1,
      // Para el Auditor y las pruebas: por qué se agrupó y cuánto se perdió de
      // resolución. Un ajuste visual que no se puede explicar no es auditable.
      reason: group > 1
        ? `${n} observaciones no caben a ${minPx}px en ${Math.round(span)}px: se agrupan de ${group} en ${group}`
        : '',
      target_px: o.targetThickness || TARGET_BAR_PX,
    };
  }

  /**
   * Contenedores visuales a partir de las filas del dataset.
   *
   * Cada contenedor conserva rango, magnitud, signo, extremo, cuántos agrupa y
   * los índices originales para el `hover`. El dataset de entrada no se modifica.
   *
   * @param {Array}  rows      [{label, value, color?, key?}]
   * @param {number} extentPx
   * @param {object} opts      {aggregate: 'extreme'|'sum', ...layout}
   */
  function bin(rows, extentPx, opts) {
    const o = opts || {};
    const mode = o.aggregate === 'sum' ? 'sum' : 'extreme';
    const src = Array.isArray(rows) ? rows : [];
    const plan = layout(src.length, extentPx, o);
    if (!src.length) return Object.assign({ rows: [] }, plan);
    if (plan.group <= 1) {
      // Sin agrupar, cada contenedor ES su observación: se anota igualmente para
      // que el `hover` y las pruebas lean siempre la misma forma.
      return Object.assign({
        rows: src.map((d, i) => ({
          label: d.label, value: _num(d.value), color: d.color,
          key: d.key !== undefined ? d.key : i,
          from: d.label, to: d.label,
          count: 1, peak: _num(d.value), sum: _num(d.value),
          members: [i], grouped: 1,
        })),
      }, plan);
    }

    const out = [];
    for (let i = 0; i < src.length; i += plan.group) {
      const chunk = src.slice(i, i + plan.group);
      let peak = 0, sum = 0, peakRow = chunk[0];
      const members = [];
      for (let k = 0; k < chunk.length; k++) {
        const v = _num(chunk[k].value);
        sum += v;
        if (Math.abs(v) > Math.abs(peak)) { peak = v; peakRow = chunk[k]; }
        members.push(i + k);
      }
      // 'extreme' publica el valor extremo —que alguien observó— y 'sum' la
      // suma. Ni uno ni otro es una media: una media puede cancelar un +8 con un
      // −8 y hacer desaparecer la concentración justo donde hay que verla.
      const value = mode === 'sum' ? sum : peak;
      out.push({
        label: chunk.length > 1
          ? `${chunk[0].label}…${chunk[chunk.length - 1].label}`
          : String(chunk[0].label),
        value,
        color: mode === 'sum' ? undefined : peakRow.color,
        key: `g${i}`,
        from: chunk[0].label, to: chunk[chunk.length - 1].label,
        count: chunk.length, peak, sum, members, grouped: chunk.length,
        // Se marca cuando el pico domina a la suma: en un grupo con signos
        // mezclados la suma puede ser pequeña y esconder un evento grande.
        peak_dominates: mode === 'sum' && Math.abs(peak) > Math.abs(sum) * 1.6,
      });
    }
    return Object.assign({ rows: out }, plan);
  }

  /**
   * Extensión visible de un valor: suelo para que exista, tope para que no
   * mienta. Un cero exacto sigue midiendo cero.
   */
  function extent(px, value, axisPx) {
    if (!Number.isFinite(value) || value === 0) return 0;
    const raw = Math.abs(_num(px));
    const cap = Math.max(1, _num(axisPx) * MAX_EXTENT_FRACTION);
    return Math.max(Math.min(MIN_EXTENT_PX, cap), raw);
  }

  /** Texto del `hover` de un contenedor: rango, valor, extremo y cuántos agrupa. */
  function describe(row, fmt) {
    const f = fmt || (v => Q.compact(v, 1));
    if (!row) return '';
    if (!row.grouped || row.grouped <= 1) return `${row.label} · ${f(row.value)}`;
    let txt = `${row.from}…${row.to} · ${f(row.value)} · ${row.grouped} agrupados`;
    if (row.peak_dominates) txt += ` · pico ${f(row.peak)}`;
    return txt;
  }

  /* ── Agregación TEMPORAL ──────────────────────────────────────────────────
   *
   * Los carriles de FLUJO comparten eje de tiempo con las velas de TRACE, así
   * que sus barras no se pueden colocar por índice: tienen que seguir cayendo
   * en su instante real. Aquí la agrupación es del INTERVALO, no de la posición:
   * si un minuto da barras de 1 px, se agrupan en 2 m, 3 m, 5 m… hasta que cada
   * barra tiene presencia, y el intervalo agrupado sigue ocupando su sitio
   * exacto en el eje.
   *
   * El paso resultante es dinámico según el ancho REAL del panel, no una
   * temporalidad fija: el mismo carril más estrecho agrupa más.
   */
  function timeLayout(t0, t1, bucketMs, widthPx, opts) {
    const o = opts || {};
    const fill = o.fill || FILL;
    const minPx = o.minThickness || MIN_BAR_PX;
    const maxPx = o.maxThickness || MAX_BAR_PX;
    const target = Math.max(minPx, o.targetThickness || TARGET_BAR_PX);
    const span = Math.max(1, _num(widthPx));
    const ms = Math.max(1, _num(bucketMs));
    const slots = Math.max(1, Math.ceil((_num(t1) - _num(t0)) / ms));
    const slotPx = span / slots;
    const needed = target / fill;
    const step = slotPx >= needed ? 1 : Math.max(1, Math.ceil(needed / slotPx));
    const groupMs = ms * step;
    const bins = Math.max(1, Math.ceil((_num(t1) - _num(t0)) / groupMs));
    const pitch = span / bins;
    return {
      step, groupMs, bins, pitch,
      thickness: Q.clamp(pitch * fill, Math.min(minPx, pitch), maxPx),
      aggregated: step > 1,
      reason: step > 1
        ? `${slots} intervalos en ${Math.round(span)}px dan ${slotPx.toFixed(2)}px por barra: se agrupan de ${step} en ${step}`
        : '',
    };
  }

  /**
   * Contenedores temporales alineados al inicio de la ventana.
   *
   * Devuelve, por contenedor, su instante inicial, su final y los buckets
   * originales que contiene. Cada carril reduce los campos que le interesan
   * —volumen, prima, flujo neto, conteo— con una SUMA, y conserva su pico.
   */
  function timeBins(buckets, t0, t1, bucketMs, widthPx, opts) {
    const plan = timeLayout(t0, t1, bucketMs, widthPx, opts);
    const src = Array.isArray(buckets) ? buckets : [];
    const start = _num(t0);
    const byBin = new Map();
    for (const b of src) {
      const t = _num(b && b.t);
      if (!Number.isFinite(t) || t < start - plan.groupMs || t > _num(t1)) continue;
      const k = Math.floor((t - start) / plan.groupMs);
      let entry = byBin.get(k);
      if (!entry) {
        entry = { t: start + k * plan.groupMs, tEnd: start + (k + 1) * plan.groupMs,
                  members: [], count: 0 };
        byBin.set(k, entry);
      }
      entry.members.push(b);
      entry.count += 1;
    }
    const rows = Array.from(byBin.values()).sort((a, b) => a.t - b.t);
    return Object.assign({ rows }, plan);
  }

  /** Suma con signo de un campo dentro de un contenedor, y su valor extremo. */
  function reduceBin(binRow, pick) {
    let sum = 0, peak = 0;
    for (const m of (binRow.members || [])) {
      const v = _num(pick(m));
      sum += v;
      if (Math.abs(v) > Math.abs(peak)) peak = v;
    }
    return { sum, peak, count: binRow.count || 0,
             peak_dominates: Math.abs(peak) > Math.abs(sum) * 1.6 };
  }

  /* ── Mapa de calor: tiempo × strike × magnitud ────────────────────────────
   *
   * El mismo problema de densidad, en dos dimensiones a la vez. Con 90 strikes
   * y 81 intervalos en un panel de 250 px de alto cada celda medía 2.8 px, se
   * dibujaba como un CÍRCULO de radio `min(cw,ch)*0.46` —1.3 px— y encima con
   * intensidad `|v| / max`, una normalización lineal que con una cola pesada
   * deja casi todo por debajo del umbral y sin pintar. El resultado eran puntos
   * diminutos y dispersos donde tenía que haber zonas.
   *
   * Aquí se agrupan filas y columnas hasta que la celda tiene tamaño suficiente
   * —conservando el EXTREMO de cada bloque, no su media— y la intensidad se
   * calcula por RANGO dentro de la matriz visible, que es lo que hace que un
   * activo con cola pesada y otro con el campo plano se lean igual.
   */
  const MIN_CELL_PX = 4;

  function grid(matrix, widthPx, heightPx, opts) {
    const o = opts || {};
    const minCell = o.minCell || MIN_CELL_PX;
    const src = Array.isArray(matrix) ? matrix : [];
    const nRows = src.length;
    const nCols = nRows ? (Array.isArray(src[0]) ? src[0].length : 0) : 0;
    if (!nRows || !nCols) {
      return { cells: [], rows: 0, cols: 0, cw: 0, ch: 0, rowGroup: 1, colGroup: 1,
               aggregated: false };
    }
    const colGroup = Math.max(1, Math.ceil(nCols / Math.max(1, Math.floor(_num(widthPx) / minCell))));
    const rowGroup = Math.max(1, Math.ceil(nRows / Math.max(1, Math.floor(_num(heightPx) / minCell))));
    const rows = Math.ceil(nRows / rowGroup);
    const cols = Math.ceil(nCols / colGroup);

    // Extremo con signo de cada bloque. Una media cancelaría un +8 con un −8 y
    // borraría la concentración justo donde hay que verla.
    const cells = [];
    const flat = [];
    for (let y = 0; y < rows; y++) {
      const line = new Array(cols).fill(0);
      for (let x = 0; x < cols; x++) {
        let peak = 0;
        for (let dy = 0; dy < rowGroup; dy++) {
          const r = src[y * rowGroup + dy];
          if (!r) continue;
          for (let dx = 0; dx < colGroup; dx++) {
            const v = _num(r[x * colGroup + dx]);
            if (Math.abs(v) > Math.abs(peak)) peak = v;
          }
        }
        line[x] = peak;
        if (peak !== 0) flat.push(Math.abs(peak));
      }
      cells.push(line);
    }

    // Intensidad por RANGO: el percentil que ocupa |valor| dentro de lo visible.
    // Se reparte por construcción entre 0 y 1 sea cual sea la forma de la
    // distribución, así que la escala deja de depender del tamaño del activo.
    flat.sort((a, b) => a - b);
    const rank = v => {
      const a = Math.abs(v);
      if (!(a > 0) || !flat.length) return 0;
      let lo = 0, hi = flat.length;
      while (lo < hi) { const m = (lo + hi) >> 1; if (flat[m] < a) lo = m + 1; else hi = m; }
      return (lo + 0.5) / flat.length;
    };

    return {
      cells, rows, cols, rowGroup, colGroup,
      cw: _num(widthPx) / cols, ch: _num(heightPx) / rows,
      aggregated: rowGroup > 1 || colGroup > 1,
      intensity: rank,
      normalization: 'VISIBLE_RANK_PERCENTILE',
    };
  }

  global.ITMQBars = {
    layout, bin, extent, describe, timeLayout, timeBins, reduceBin, grid, MIN_CELL_PX,
    FILL, MIN_BAR_PX, TARGET_BAR_PX, MAX_BAR_PX,
    MIN_EXTENT_PX, MAX_EXTENT_FRACTION,
  };
})(window);
