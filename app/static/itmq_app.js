/* ITM QUANT · Terminal de analista v1.41.0
 *
 * Orquestación de la interfaz: navegación, ciclo de datos y pintado de secciones.
 * Aquí no hay matemática de mercado; todo lo que se muestra lo calculó el motor
 * y llega por /api/terminal/bundle y /api/nextgen/trace.
 */
(function () {
  'use strict';

  const Q = window.ITMQ;
  const P = window.ITMQPanels;
  const Trace = window.ITMQTrace;
  const Flow = window.ITMQFlow;

  const el = id => document.getElementById(id);
  const set = (id, v) => { const x = el(id); if (x) x.textContent = (v === null || v === undefined || v === '') ? '—' : v; };

  // v1.43.0 · Motivo por el que una sección está vacía, en lenguaje de ANÁLISIS.
  //
  // La pantalla principal no lleva nombres de proveedor, rutas ni códigos de error:
  // eso vive en el Auditor. Lo que el operador necesita saber aquí es si mira un
  // mercado tranquilo o un hueco de datos, y eso se puede decir sin nombrar a nadie.
  const MOTIVO_VACIO = {
    QUANT_DATA_SIN_RESPUESTA: 'sin datos de estructura off-exchange en este ciclo',
    NO_PRINTS: 'la sesión todavía no ha dejado impresiones',
    SIN_OFF_EXCHANGE_CONFIRMADO: 'sin ejecución fuera de bolsa confirmada',
    NO_PROVIDER_DATA: 'sin datos en este ciclo',
    PROVIDER_ERROR: 'datos no disponibles en este ciclo',
    PARSER_ERROR: 'datos no disponibles en este ciclo',
    FILTERED_ALL: 'ninguna observación superó la validación',
    STALE: 'el último dato es demasiado antiguo para operar con él',
    // v1.46.0 · Los ocho estados internos de DARK POOL. Existen para el Auditor;
    // si alguno alcanza la pantalla del analista, se lee como análisis y no como
    // un código de error del proveedor. REQUEST_INVALID es un defecto NUESTRO:
    // decirle al operador «petición inválida» sería informarle de nuestro bug.
    DIRECT_PROVIDER_OK: '',
    SIN_DATOS_REALES: 'sin actividad fuera de bolsa en esta ventana',
    MARKET_CLOSED: 'mercado cerrado: no hay sesión que medir',
    REQUEST_INVALID: 'datos no disponibles en este ciclo',
    NO_CLASIFICABLE: 'las impresiones llegaron sin centro de ejecución utilizable',
  };

  function motivoVacio(reason, fallback) {
    return MOTIVO_VACIO[String(reason || '')] || fallback;
  }


  const state = {
    view: 'resumen',
    symbol: 'DIA',
    bundle: null,
    trace: null,
    timeframe: '1m',
    tailMinutes: 390,
    intervalGreek: 'GAMMA',
    // Generacion publicada por el bundle: «SIMBOLO#epoca». Ver pullBundle.
    generation: null,
    // Huella del CONTENIDO del último snapshot publicado. Ver pullBundle.
    cycle: null,
    pendingGeneration: null,
    // Vista del Interval Map: 'field' (mapa continuo, lectura) | 'raw' (puntos, diagnostico)
    intervalRender: 'field',
    intervalRenderApplied: null,
    expView: 'bars',
    catalog: [],
    calendar: null,
    charts: {},
    busyTrace: false,
    busyBundle: false,
    busyTick: false,
    livePrice: null,
    failures: 0,
  };

  /* ------------------------------------------------------------- fetch */

  async function api(path) {
    const r = await fetch(path, { headers: { 'accept': 'application/json' }, credentials: 'same-origin' });
    if (r.status === 401 || r.status === 403) { location.replace('/login'); throw new Error('auth'); }
    if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
    return r.json();
  }

  function toast(msg) {
    const t = el('toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast._h);
    toast._h = setTimeout(() => t.classList.remove('show'), 2600);
  }

  function engineState(s, text) {
    const x = el('engineState');
    if (x) x.dataset.state = s;
    set('engineStateText', text);
  }

  /* -------------------------------------------------------- navegación */

  function navigate(view) {
    state.view = view;
    for (const b of document.querySelectorAll('#nav button')) b.classList.toggle('active', b.dataset.view === view);
    for (const s of document.querySelectorAll('section.view')) s.classList.toggle('active', s.dataset.view === view);
    // Los canvas ocultos no reciben ResizeObserver útil: al mostrarse hay que
    // recalcular tamaño antes del primer frame o saldrían con el tamaño anterior.
    requestAnimationFrame(() => requestAnimationFrame(() => Q.redrawAll()));
    if (view === 'trace' || view === 'flujo') pullTrace();
    if (view === 'backtest' && !state.calendar) pullCalendar();
    if (view === 'fuentes') pullDiagnostics();
    try { localStorage.setItem('itmq-view', view); } catch (_) { }
  }

  /* ------------------------------------------------------ ciclo de datos */

  async function pullTrace() {
    if (state.busyTrace) return;
    state.busyTrace = true;
    try {
      const d = await api(`/api/nextgen/trace?timeframe=${state.timeframe}&tail_minutes=${state.tailMinutes}&window=12`);
      if (d && d.stale_read) return;              // respuesta de otro símbolo
      state.trace = d;
      // Un fallo de presentación en un panel no puede impedir que el otro reciba
      // sus datos, ni detener el ciclo de refresco.
      try { Trace.applyData(d); } catch (err) { console.error('[TRACE] applyData', err); }
      try { Flow.applyTrace(d); } catch (err) { console.error('[FLOW] applyTrace', err); }
      const age = Q.num((d.profiles || {}).snapshot_age_seconds, NaN);
      const pill = el('traceSnapPill');
      if (pill) {
        const held = !!(d.blocked || d.context_only || d.structure_stale);
        pill.textContent = Q.isNum(age)
          ? `${held ? 'RETENIDO' : 'SNAPSHOT'} ${age.toFixed(0)}s`
          : (held ? 'RETENIDO' : 'SNAPSHOT —');
        pill.className = 'pill ' + (held ? 'warn' : (!Q.isNum(age) ? 'off' : age < 90 ? 'live' : age < 600 ? 'warn' : 'down'));
      }
    } catch (err) {
      if (String(err.message) !== 'auth') console.warn('[trace]', err.message);
    } finally {
      state.busyTrace = false;
    }
  }

  /**
   * Precio en vivo aplicado a la vela en formación.
   *
   * El trace pesado se pide cada 2,5 s: reconstruye estructura, perfiles y niveles.
   * Entre una petición y la siguiente la última vela se quedaba quieta, y eso es el
   * retraso que se ve en pantalla. Aquí sólo se pide el precio —una lectura, sin
   * cálculo— y se extiende la vela abierta: cierre al precio actual, máximo y
   * mínimo ampliados si los rompe. No se inventan velas nuevas ni se toca el
   * volumen: eso sigue llegando del motor con su propia cadencia.
   */
  async function pullTick() {
    if (state.busyTick || !state.trace || replay.active) return;
    state.busyTick = true;
    try {
      const t = await api('/api/terminal/tick');
      // En replay el reloj lo manda el usuario: adelantar la vela falsearía la sesión.
      if (!t || t.replay || t.price == null) return;
      if (String(t.symbol || '') !== String(state.trace.symbol || '')) return;
      const p = Q.num(t.price, NaN);
      if (!Q.isNum(p) || p <= 0) return;

      const candles = state.trace.candles || [];
      if (!candles.length) return;
      const last = candles[candles.length - 1];
      const c = Q.num(last.c, NaN);
      if (!Q.isNum(c) || Math.abs(p - c) < 1e-9) return;

      last.c = p;
      last.h = Math.max(Q.num(last.h, p), p);
      last.l = Math.min(Q.num(last.l, p), p);
      state.livePrice = p;

      try { Trace.applyData(state.trace); } catch (err) { console.error('[TRACE] tick', err); }
      try { Flow.applyTrace(state.trace); } catch (err) { console.error('[FLOW] tick', err); }
      set('symPrice', p.toFixed(2));
    } catch (err) {
      if (String(err.message) !== 'auth') console.warn('[tick]', err.message);
    } finally {
      state.busyTick = false;
    }
  }

  /* ------------------------------------------------ reproducción de sesión

     El backtesting de esta terminal no simula nada: recorre los instantes que el
     motor realmente observó ese día. Cada marca del reloj es un `asof` que el
     backend resuelve con la misma causalidad con la que ocurrió, así que ningún
     panel puede ver algo que en ese instante todavía no existía.

     Al cambiar el `asof` se vuelven a pedir bundle y trace enteros: TRACE, FLUJO,
     EXPOSICIÓN, VOLATILIDAD, MACRO y el resto se reproducen a la vez, porque
     todas leen del mismo contexto histórico global del motor. */

  const replay = {
    active: false, date: null, marks: [], index: 0,
    playing: false, timer: null, stepMinutes: 1, speedMs: 1000, busy: false,
  };

  function replayUI() {
    const bar = el('replayBar');
    if (bar) bar.hidden = !replay.active;
    document.body.classList.toggle('replaying', replay.active && replay.marks.length > 0);
    el('replayBtn')?.classList.toggle('on', replay.active);
    const play = el('rpPlay');
    if (play) play.textContent = replay.playing ? '⏸' : '▶';
    const scrub = el('rpScrub');
    if (scrub) {
      scrub.max = String(Math.max(0, replay.marks.length - 1));
      scrub.value = String(replay.index);
      scrub.disabled = !replay.marks.length;
    }
    const at = replay.marks[replay.index];
    set('rpClock', at ? String(at).replace('T', ' ').slice(0, 19) : '—');
    set('rpProgress', replay.marks.length
      ? `${replay.index + 1} / ${replay.marks.length} marcas · paso ${replay.stepMinutes} min`
      : (replay.date ? 'sin marcas archivadas para esa fecha' : 'elige una sesión'));
  }

  async function replayLoadDates() {
    try {
      const d = await api('/api/replay/sessions');
      const sel = el('rpDate');
      if (!sel) return;
      const rows = (d.sessions || []).slice().sort((a, b) => String(b.date || b).localeCompare(String(a.date || a)));
      sel.innerHTML = '<option value="">— elegir fecha —</option>' + rows.map(r => {
        const day = String(r.date || r);
        // La cobertura dice si esa sesión tiene cinta suficiente para reproducirse.
        const n = Q.num(r.rows ?? r.ticks ?? r.observations, NaN);
        return `<option value="${esc(day)}">${esc(day)}${Q.isNum(n) ? ` · ${Q.compact(n, 0)} obs` : ''}</option>`;
      }).join('');
      if (!rows.length) sel.innerHTML = '<option value="">sin sesiones archivadas</option>';
    } catch (err) {
      if (String(err.message) !== 'auth') console.warn('[replay] sesiones', err.message);
    }
  }

  async function replayOpenDate(day) {
    replayStop();
    replay.date = day || null;
    replay.marks = []; replay.index = 0;
    replayUI();
    if (!day) return;
    try {
      const c = await api(`/api/replay/clock?date=${encodeURIComponent(day)}&step_minutes=${replay.stepMinutes}`);
      if (!c || !c.ready) {
        toast(`Sin reloj archivado para ${day}: ${c && c.reason ? c.reason : 'sesión incompleta'}`);
        replayUI();
        return;
      }
      replay.marks = Array.isArray(c.marks) ? c.marks : [];
      replay.index = 0;
      await replaySeek(0);
    } catch (err) {
      if (String(err.message) !== 'auth') toast(`No pude abrir la sesión: ${err.message}`);
      replayUI();
    }
  }

  /** Coloca el motor en una marca concreta y repinta todas las secciones. */
  async function replaySeek(i) {
    if (!replay.date || !replay.marks.length || replay.busy) return;
    replay.index = Q.clamp(Math.round(i), 0, replay.marks.length - 1);
    replay.busy = true;
    replayUI();
    try {
      const at = replay.marks[replay.index];
      const r = await fetch(`/api/replay/set?date=${encodeURIComponent(replay.date)}&asof=${encodeURIComponent(at)}`,
        { method: 'POST' });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        toast(`Replay rechazado: ${body.detail || r.status}`);
        replayStop();
        return;
      }
      replay.active = true;
      // Un solo contexto histórico: pedir bundle y trace reproduce TODAS las secciones.
      await Promise.all([pullBundle(), pullTrace()]);
    } catch (err) {
      if (String(err.message) !== 'auth') console.warn('[replay] seek', err.message);
    } finally {
      replay.busy = false;
      replayUI();
    }
  }

  function replayStep(delta) {
    const next = replay.index + delta;
    if (next < 0 || next >= replay.marks.length) { replayStop(); return; }
    replaySeek(next);
  }

  function replayPlay() {
    if (!replay.marks.length || replay.playing) return;
    replay.playing = true;
    const tick = async () => {
      if (!replay.playing) return;
      if (replay.index >= replay.marks.length - 1) { replayStop(); return; }
      await replaySeek(replay.index + 1);
      // Se encadena tras completar el paso: si el motor tarda, la reproducción
      // espera en vez de acumular peticiones encima de las anteriores.
      if (replay.playing) replay.timer = setTimeout(tick, replay.speedMs);
    };
    replay.timer = setTimeout(tick, replay.speedMs);
    replayUI();
  }

  function replayStop() {
    replay.playing = false;
    if (replay.timer) { clearTimeout(replay.timer); replay.timer = null; }
    replayUI();
  }

  async function replayExitToLive() {
    replayStop();
    try { await fetch('/api/replay/live', { method: 'POST' }); } catch (_) { }
    replay.active = false; replay.date = null; replay.marks = []; replay.index = 0;
    const sel = el('rpDate'); if (sel) sel.value = '';
    replayUI();
    await Promise.all([pullBundle(), pullTrace()]);
    toast('De vuelta en el mercado en vivo');
  }

  function bindReplay() {
    el('replayBtn')?.addEventListener('click', async () => {
      const bar = el('replayBar');
      if (!bar) return;
      bar.hidden = !bar.hidden;
      if (!bar.hidden && !el('rpDate')?.options?.length) await replayLoadDates();
      if (!bar.hidden && el('rpDate') && el('rpDate').options.length <= 1) await replayLoadDates();
    });
    el('rpDate')?.addEventListener('change', e => replayOpenDate(e.target.value));
    el('rpStep')?.addEventListener('change', e => {
      replay.stepMinutes = Number(e.target.value) || 1;
      if (replay.date) replayOpenDate(replay.date);
    });
    el('rpSpeed')?.addEventListener('change', e => { replay.speedMs = Number(e.target.value) || 1000; });
    el('rpPlay')?.addEventListener('click', () => (replay.playing ? replayStop() : replayPlay()));
    el('rpPrev')?.addEventListener('click', () => { replayStop(); replayStep(-1); });
    el('rpNext')?.addEventListener('click', () => { replayStop(); replayStep(1); });
    el('rpScrub')?.addEventListener('change', e => { replayStop(); replaySeek(Number(e.target.value)); });
    el('rpLive')?.addEventListener('click', replayExitToLive);
  }

  async function pullDiagnostics() {
    try {
      state.diagnostics = await api(`/api/terminal/diagnostics?timeframe=${state.timeframe}&tail_minutes=${state.tailMinutes}`);
      if (state.bundle) renderFuentes(state.bundle);
    } catch (err) {
      if (String(err.message) !== 'auth') console.warn('[diagnostics]', err.message);
    }
  }

  async function pullBundle() {
    if (state.busyBundle) return;
    state.busyBundle = true;
    try {
      const d = await api(`/api/terminal/bundle?timeframe=${state.timeframe}&tail_minutes=${state.tailMinutes}&interval_greek=${state.intervalGreek}`);

      /* v1.55.0 · EL CAMBIO DE ACTIVO ES UNA TRANSACCIÓN.
       *
       * O la pantalla entera es del activo nuevo, o sigue siendo del anterior.
       * Media pantalla de cada es lo que pasaba cuando una respuesta lenta del
       * símbolo viejo llegaba DESPUÉS de la del nuevo: se pintaba encima y
       * dejaba KPI de QQQ sobre un TRACE de SPY, sin que nada avisara.
       *
       * `generation_id` es «símbolo#época» y viaja en el bundle. Una respuesta
       * de otra generación se descarta entera; no se mezcla ni se aprovecha «lo
       * que sirva», porque lo que sirve y lo que no es indistinguible después.
       */
      const gen = String(d.generation_id || '');
      const esperado = state.switching ? String(state.switching).toUpperCase() : null;
      const llega = String(d.symbol || '').toUpperCase();
      if (esperado && llega && llega !== esperado) {
        console.debug('[BUNDLE] generacion descartada', gen, '≠', esperado);
        return;                       // el bundle del activo viejo no se pinta
      }

      /* v1.56.0 · COMMIT TRANSACCIONAL DEL SNAPSHOT.
       *
       * Descartar la generación ajena no basta. Durante la hidratación del
       * activo nuevo, el bundle empieza a llegar con SU símbolo correcto pero
       * con secciones a medio llenar, y pintarlo produce media pantalla de cada:
       *
       *     Net Drift de QQQ  +  GEX todavía de DIA  +  muros antiguos
       *
       * Eso no es un estado intermedio inocente: son tres lecturas de tres
       * momentos distintos presentadas como una sola foto del mercado.
       *
       * Mientras se está cambiando de activo, el snapshot sólo se PUBLICA
       * cuando los datasets críticos ya son del activo nuevo. Hasta entonces la
       * cabecera dice HIDRATANDO y la pantalla anterior se queda quieta, que es
       * honesto: lo que se ve es de antes y se dice.
       */
      if (esperado) {
        const criticos = [
          ['walls', d.walls && (d.walls.call_wall || d.walls.put_wall)],
          ['exposicion', d.exposicion && (d.exposicion.by_strike || []).length],
          ['interval_map', d.interval_map && d.interval_map.ready],
        ];
        const faltan = criticos.filter(([, ok]) => !ok).map(([k]) => k);
        if (faltan.length) {
          console.debug('[BUNDLE] snapshot incompleto, sin commit:', faltan.join(', '));
          engineState('WAIT', `HIDRATANDO ${esperado}`);
          state.pendingGeneration = gen;
          return;
        }
      }
      state.generation = gen || state.generation;
      state.cycle = String(d.cycle_id || '') || state.cycle;
      state.pendingGeneration = null;
      state.bundle = d;
      state.symbol = d.symbol || state.symbol;
      state.failures = 0;
      // La capa QFLOW viaja en el bundle (viene de Quant Data), no en /trace, y
      // alimenta tres cosas del panel de flujo: la línea acumulada, el nivel de
      // precio y los eventos de concentración.
      try { Flow.applyQflow(d.qflow); } catch (err) { console.error('[FLOW] applyQflow', err); }
      // NET DRIFT OFICIAL de Quant Data. Va aparte de QFLOW a propósito: son dos
      // magnitudes distintas, de dos endpoints distintos, y no se sustituyen.
      try { Flow.applyNetDrift(d.net_drift); } catch (err) { console.error('[FLOW] applyNetDrift', err); }
      // v1.55.0 · FlowViewModel: el estado y la edad de CADA carril de FLUJO DE
      // ORDENES. Sin el, un ciclo sin prints vaciaba la seccion entera y las
      // tarjetas decian SIN DATOS al lado de un estado que contaba 405 buckets.
      try { Flow.applyFlowView((d.flujo_ordenes || {}).view_model); } catch (err) { console.error('[FLOW] applyFlowView', err); }
      renderAll(d);
      const q = Q.num(d.data_quality, NaN);
      if (state.switching) {
        engineState('WAIT', `CAMBIANDO A ${state.switching}`);
      } else if (d.publication_blocked) {
        // La estructura está retenida por frescura; el precio observado sí se
        // publica. Decirlo en la cabecera evita interpretar un hueco como un cero.
        engineState('DEGRADED', 'ESTRUCTURA RETENIDA');
        const t = el('engineState');
        if (t) t.title = 'Publicación retenida · ' + (d.publication_blocked_motive || 'frescura no verificada');
      } else {
        engineState(d.ready ? (Q.isNum(q) && q < 60 ? 'DEGRADED' : 'LIVE') : 'WAIT',
          d.ready ? (d.mode || 'LIVE') : 'HIDRATANDO');
        const t = el('engineState');
        if (t) t.title = '';
      }
    } catch (err) {
      if (String(err.message) === 'auth') return;
      state.failures++;
      if (state.failures >= 3) engineState('DOWN', 'SIN CONEXIÓN');
      console.warn('[bundle]', err.message);
    } finally {
      state.busyBundle = false;
    }
  }

  /* ----------------------------------------------------------- render */

  function renderAll(d) {
    renderHeader(d);
    renderScannerBar(d);
    renderResumen(d);
    renderExposicion(d);
    renderOpenInterest(d);
    renderVolatilidad(d);
    renderEstadisticas(d);
    renderDarkPool(d);
    renderMacro(d);
    renderEscenarios(d);
    renderBacktest(d);
    renderFuentes(d);
    renderArquitectura(d);
  }

  function renderHeader(d) {
    set('symName', d.symbol || '—');
    const spot = Q.num(d.spot, NaN);
    set('symPrice', Q.isNum(spot) ? spot.toFixed(2) : '—');
    const r = d.resumen || {};
    const ch = el('symChange');
    if (ch) {
      const zl = Q.num(r.zone_low, NaN), zh = Q.num(r.zone_high, NaN);
      ch.textContent = (Q.isNum(zl) && Q.isNum(zh)) ? `${zl.toFixed(2)}–${zh.toFixed(2)}` : '—';
      ch.className = 'chg';
    }
  }

  function renderResumen(d) {
    const r = d.resumen || {};
    set('kDirection', r.direction || '—');
    set('kEdge', r.edge_state || '—');
    const zl = Q.num(r.zone_low, NaN), zh = Q.num(r.zone_high, NaN);
    set('kZone', (Q.isNum(zl) && Q.isNum(zh)) ? `${zl.toFixed(2)} – ${zh.toFixed(2)}` : '—');
    const t1 = Q.num(r.target1, NaN), t2 = Q.num(r.target2, NaN);
    set('kZoneDetail', Q.isNum(t1) ? `T1 ${t1.toFixed(2)}${Q.isNum(t2) ? ' · T2 ' + t2.toFixed(2) : ''}` : '—');
    set('kPhase', r.phase || '—');
    set('kStability', Q.isNum(Q.num(r.stability, NaN)) ? `estabilidad ${Q.num(r.stability).toFixed(0)}` : '—');
    set('kEvidence', Q.isNum(Q.num(r.evidence, NaN)) ? Q.num(r.evidence).toFixed(0) : '—');
    set('kConfluence', Q.isNum(Q.num(r.confluence, NaN)) ? `confluencia ${Q.num(r.confluence).toFixed(0)}` : '—');

    set('kGex', Q.signedCompact(r.net_gex, 2));
    set('kDex', Q.signedCompact(r.net_delta, 2));
    const flip = Q.num(r.zero_gamma, NaN), spot = Q.num(d.spot, NaN);
    set('kFlip', Q.isNum(flip) ? flip.toFixed(2) : '—');
    set('kFlipDist', (Q.isNum(flip) && Q.isNum(spot)) ? `${(flip - spot >= 0 ? '+' : '')}${(flip - spot).toFixed(2)} vs spot` : '—');
    const mp = Q.num(r.max_pain, NaN);
    set('kMaxPain', Q.isNum(mp) ? mp.toFixed(2) : '—');
    set('kMaxPainDetail', (Q.isNum(mp) && Q.isNum(spot)) ? `${(mp - spot >= 0 ? '+' : '')}${(mp - spot).toFixed(2)} vs spot` : '—');

    // Flujo de la sesión a partir de las velas del trace (volumen firmado real).
    const candles = (state.trace || {}).candles || [];
    chart('chartNetFlow', () => P.tbars(el('chartNetFlow'), {
      fmt: v => Q.compact(v, 1), bucketMs: Q.num((state.trace || {}).bar_interval_ms, 60000), empty: 'SIN FLUJO OBSERVADO',
    })).set(candles.map(c => ({ t: c.t, v: Q.num(c.sv, 0) })));

    /* v1.56.2 · El contador de prints sale del FlowViewModel, no del trace.
     *
     * Punto 6: la UI no puede consumir las estructuras internas del motor. Este
     * `pill` leía `trace.option_prints` —la ruta antigua— mientras la sección
     * de FLUJO ya leía el modelo, así que la cabecera podía contar una cosa y
     * el panel de al lado enseñar otra sin que nada avisara. */
    const laneP = ((state.bundle || {}).flujo_ordenes || {}).view_model;
    const prints = (laneP && laneP.prints && Array.isArray(laneP.prints.current))
      ? laneP.prints.current : [];

    // ── NET DRIFT · OFICIAL DE QUANT DATA ────────────────────────────────────
    //
    // Esta tarjeta se llamaba NET DRIFT y dibujaba el acumulado de la CINTA PROPIA
    // de opciones, no Net Drift. Dos cosas mal a la vez:
    //
    //   1. Reconstruía con datos ajenos al endpoint una magnitud que sólo publica
    //      el proveedor, y la presentaba con su nombre. En pantalla era
    //      indistinguible de la real.
    //   2. `premium * (direction || 1)` contaba como COMPRA toda la prima sin
    //      agresor clasificado, porque `0 || 1` vale 1. Cuando la cinta llegaba sin
    //      cotización —que es justo cuando peor se lee— la curva se inclinaba a
    //      comprador sola.
    //
    // Ahora la fuente es la misma que la del panel de FLUJO: `d.net_drift`, que
    // viene de POST /v1/options/tool/net-drift. Sin dato se escribe SIN DATOS.
    const nd = d.net_drift || null;
    const ndRows = (nd && nd.ready && Array.isArray(nd.series)) ? nd.series : [];
    chart('chartNetDrift', () => P.lines(el('chartNetDrift'), {
      fmt: v => Q.money(v, 1),
      // El código de estado en crudo es diagnóstico y vive en el Auditor; aquí va
      // el motivo en lenguaje de análisis, que es lo que el operador necesita para
      // distinguir un mercado tranquilo de un hueco de datos.
      empty: nd && !nd.ready ? `SIN DATOS · ${motivoVacio(nd.state, 'sin datos en este ciclo')}` : 'SIN DATOS',
    })).set([
      // v1.55.0 · `Q.num(x, 0)` mandaba al eje CERO todo bucket sin dato. Un
      // acumulado de prima en cero afirma que la sesion no ha movido nada, y lo
      // que habia era un hueco. Con NaN el trazo se parte y el hueco se ve.
      { name: 'CALL', color: Q.token('--pos', '#22c55e'), points: ndRows.map(x => ({ t: x.t, v: Q.num(x.cum_call, NaN) })) },
      { name: 'PUT', color: Q.token('--neg', '#ef4444'), points: ndRows.map(x => ({ t: x.t, v: Q.num(x.cum_put, NaN) })) },
      { name: 'NET', color: Q.token('--accent', '#38bdf8'), width: 2, points: ndRows.map(x => ({ t: x.t, v: Q.num(x.cum_net, NaN) })) },
    ]);

    pill('pillFlow', candles.length ? 'live' : 'off', candles.length ? `${candles.length} barras` : 'esperando');
    pill('pillDrift', ndRows.length ? 'live' : 'off',
      // El estado de dato sí es útil en pantalla (dice si hay hueco o mercado
      // tranquilo); el nombre del proveedor no lo es y vive en el Auditor.
      ndRows.length ? `${ndRows.length} buckets` : motivoVacio(nd && nd.state, 'SIN DATOS'));
    pill('pillPrints', prints.length ? 'live' : 'off', prints.length ? `${prints.length}` : '0');

    fillTable('tblLevels', r.levels || [], lv => [
      lv.name,
      Q.num(lv.price).toFixed(2),
      cell(Q.isNum(Q.num(lv.distance, NaN)) ? `${Q.num(lv.distance) >= 0 ? '+' : ''}${Q.num(lv.distance).toFixed(2)}` : '—',
        Q.num(lv.distance, 0) >= 0 ? 'pos' : 'neg'),
    ], 'Sin niveles publicados todavía');

    fillTable('tblPrints', (r.prints || []).slice(0, 30), p => [
      Q.hhmm(Q.parseTime(p.t)),
      `${String(p.option_type || '').toUpperCase().slice(0, 1)} ${Q.isNum(Q.num(p.strike, NaN)) ? Q.num(p.strike).toFixed(2) : '—'}`,
      cell(Q.money(p.premium, 1), Q.num(p.direction, 0) >= 0 ? 'pos' : 'neg'),
      Q.compact(p.contracts, 0),
      p.aggressor || '—',
    ], 'Sin prints de opciones observados en la sesión');
  }

  function renderExposicion(d) {
    const ex = d.exposicion || {};
    const metric = (el('expMetric') || {}).value || 'GEX';
    const axis = (document.querySelector('#expAxis button.active') || {}).dataset?.axis || 'strike';
    const field = metric.toLowerCase();

    set('expTitle', axis === 'strike' ? `EXPOSICIÓN ${metric} POR STRIKE` : `EXPOSICIÓN ${metric} POR VENCIMIENTO`);

    // La forma del perfil, no el total: eso ya está en RESUMEN. Lo que decide si un
    // muro aguanta o se atraviesa es cómo está repartida la exposición.
    const conc = Q.num(ex.concentration_pct, NaN);
    set('exConc', Q.isNum(conc) ? `${conc.toFixed(0)}%` : '—');
    set('exConcDetail', Q.isNum(conc)
      ? (conc >= 60 ? 'concentrada · los muros mandan' : conc >= 35 ? 'repartida' : 'difusa · sin muro dominante')
      : (ex.shape_reason || 'top 3 strikes sobre el total'));

    const dk = Q.num(ex.dominant_strike, NaN), dd = Q.num(ex.dominant_distance_pct, NaN);
    set('exDom', Q.isNum(dk) ? dk.toFixed(2) : '—');
    set('exDomDetail', Q.isNum(dd)
      ? `${Q.signedCompact(ex.dominant_gex, 2)} · ${dd >= 0 ? '+' : ''}${dd.toFixed(2)}% del precio`
      : 'mayor exposición absoluta');

    const bal = Q.num(ex.gex_balance_pct, NaN);
    set('exBalance', Q.isNum(bal) ? `${bal >= 0 ? '+' : ''}${bal.toFixed(0)}` : '—');
    // +100 = toda la gamma por encima del precio; −100 = toda por debajo.
    set('exBalanceDetail', Q.isNum(bal)
      ? `${Q.signedCompact(ex.gex_below, 1)} debajo · ${Q.signedCompact(ex.gex_above, 1)} encima`
      : 'dónde pesa la cobertura');

    set('exCount', Q.isNum(Q.num(ex.strikes_counted, NaN)) ? Q.num(ex.strikes_counted).toFixed(0) : '—');
    set('exCountDetail', Q.isNum(Q.num(ex.spot, NaN)) ? `spot ${Q.num(ex.spot).toFixed(2)}` : 'ventana visible');

    const rowsK = ex.by_strike || [];
    const rowsE = ex.by_expiration || [];
    pill('expPill', rowsK.length ? 'live' : 'off', rowsK.length ? `${rowsK.length} strikes` : 'esperando estructura');

    // v1.48.0 · Una barra por strike, con grosor real. El panel crece hasta lo
    // que haga falta y el contenedor hace scroll; agrupar tres strikes en una
    // barra escondia justo el dato que se estaba buscando.
    const expRows = axis === 'strike'
      ? rowsK.map(r => ({ label: Q.num(r.strike).toFixed(2), value: Q.num(r[field], 0), key: r.strike }))
      : rowsE.map(r => ({ label: String(r.expiration).slice(5), value: Q.num(r[field], 0), key: r.expiration }));

    growForRows('chartExposure', expRows.length);
    const main = chart('chartExposure', () => P.hbars(el('chartExposure'), {
      fmt: v => Q.signedCompact(v, 2), empty: 'SIN ESTRUCTURA', targetBar: EXP_BAR_PX,
    }));
    main.set(expRows);

    // El relieve lee EL MISMO perfil: dos vistas de un dato, nunca dos datos.
    // Y crece igual que las barras: una fila por strike, sin saltarse ninguno.
    // El alto extra es el de la fuga isométrica, que no cabe dentro de las filas.
    growForRows('chartExposureRelief', expRows.length, RELIEF_ROW_PX, 140);
    const expRelief = chart('chartExposureRelief', () => P.relief(el('chartExposureRelief'), {
      fmt: v => Q.signedCompact(v, 2), empty: 'SIN ESTRUCTURA', aggregate: 'none',
    }));
    expRelief.set(expRows);
    applyExpView();

    chart('chartExposureExp', () => P.bars(el('chartExposureExp'), { fmt: v => Q.signedCompact(v, 1), empty: 'SIN DESGLOSE POR VENCIMIENTO' }))
      .set(rowsE.map(r => ({ label: String(r.expiration).slice(5), value: Q.num(r[field], 0), key: r.expiration })));

    fillTable('tblExposure', rowsK.slice().sort((a, b) => Math.abs(Q.num(b[field])) - Math.abs(Q.num(a[field]))).slice(0, 40), r => [
      Q.num(r.strike).toFixed(2),
      cell(Q.signedCompact(r.gex, 2), Q.num(r.gex) >= 0 ? 'pos' : 'neg'),
      cell(Q.signedCompact(r.dex, 2), Q.num(r.dex) >= 0 ? 'pos' : 'neg'),
      Q.signedCompact(r.vex, 2),
      Q.signedCompact(r.chex, 2),
      Q.compact(r.oi, 0),
    ], 'Sin cadena de opciones cargada');
  }

  function renderOpenInterest(d) {
    const oi = d.open_interest || {};
    const mp = Q.num(oi.max_pain, NaN), spot = Q.num(oi.spot, NaN);
    set('oiMaxPain', Q.isNum(mp) ? mp.toFixed(2) : '—');
    set('oiMaxPainDist', (Q.isNum(mp) && Q.isNum(spot)) ? `${(mp - spot >= 0 ? '+' : '')}${(mp - spot).toFixed(2)} vs spot` : '—');
    set('oiTotal', Q.compact(oi.total_oi, 1));
    set('oiCall', Q.compact(oi.call_oi, 1));
    set('oiPut', Q.compact(oi.put_oi, 1));
    set('oiCallPct', Q.isNum(Q.num(oi.call_pct, NaN)) ? `${Q.num(oi.call_pct).toFixed(1)}% del total` : '—');
    set('oiPutPct', Q.isNum(Q.num(oi.put_pct, NaN)) ? `${Q.num(oi.put_pct).toFixed(1)}% del total` : '—');

    const rows = oi.by_strike || [];
    // v1.47.0 · Cuando hay agregados pero no desglose, el panel DICE por qué en
    // vez de contradecir a la cabecera. «OI TOTAL 37.1K» encima de «SIN INTERÉS
    // ABIERTO» son dos verdades que juntas se leen como un fallo.
    const oiEmpty = oi.breakdown_reason || 'SIN INTERÉS ABIERTO';
    // Call arriba y put abajo del cero: el desequilibrio se lee de un vistazo.
    growForRows('chartOiStrike', rows.length);
    const oiStrike = chart('chartOiStrike', () => P.hbars(el('chartOiStrike'), {
      fmt: v => Q.signedCompact(v, 1), empty: oiEmpty, targetBar: EXP_BAR_PX,
    }));
    if (oiStrike.setEmpty) oiStrike.setEmpty(oiEmpty);
    oiStrike.set(rows.map(r => ({ label: Q.num(r.strike).toFixed(2), value: Q.num(r.net_oi, 0), key: r.strike })));

    chart('chartMaxPainTime', () => P.lines(el('chartMaxPainTime'), { fmt: v => v.toFixed(2), zeroLine: false, empty: 'MAX PAIN / TIEMPO NO DISPONIBLE' }))
      .set([{ name: 'MAX PAIN', color: Q.token('--gold', '#d9a441'), points: (oi.max_pain_over_time || []).map(r => ({ t: r.t, v: r.value })) }]);

    chart('chartOiExp', () => P.bars(el('chartOiExp'), { fmt: v => Q.compact(v, 1), zeroCenter: false, empty: 'SIN DESGLOSE POR VENCIMIENTO' }))
      .set((oi.by_expiration || []).map(r => ({ label: String(r.expiration).slice(5), value: Q.num(r.oi, 0), key: r.expiration })));

    fillTable('tblOiChange', rows.slice().sort((a, b) => Q.num(b.oi) - Q.num(a.oi)).slice(0, 40), r => [
      Q.num(r.strike).toFixed(2),
      Q.compact(r.call_oi, 0),
      Q.compact(r.put_oi, 0),
      cell(Q.signedCompact(r.net_oi, 0), Q.num(r.net_oi) >= 0 ? 'pos' : 'neg'),
      Q.compact(r.volume, 0),
      Q.isNum(Q.num(r.vol_oi, NaN)) ? Q.num(r.vol_oi).toFixed(2) : '—',
    ], oiEmpty);
  }

  function renderVolatilidad(d) {
    const v = d.volatilidad || {};
    set('volAtm', Q.isNum(Q.num(v.atm_iv, NaN)) ? `${Q.num(v.atm_iv).toFixed(2)}%` : '—');
    set('volRank', Q.isNum(Q.num(v.iv_rank, NaN)) ? Q.num(v.iv_rank).toFixed(0) : '—');
    // La procedencia va junto al número: un IV Rank del motor y uno del proveedor
    // miden ventanas distintas y no significan lo mismo.
    set('volRankDetail', v.iv_rank_source
      ? `${v.iv_rank_source === 'ITM_QUANT' ? 'motor · historia propia' : 'proveedor'} · ${v.regime || '—'}`
      : (v.iv_rank_reason || v.regime || '—'));

    const rkNative = Q.num(v.iv_rank_native, NaN), rkProv = Q.num(v.iv_rank_provider, NaN);
    pill('volRankPill', v.iv_rank_source === 'ITM_QUANT' ? 'live' : v.iv_rank_source ? 'warn' : 'off',
      v.iv_rank_source === 'ITM_QUANT' ? `motor · ${Q.num(v.iv_samples, 0).toFixed(0)} observaciones`
        : v.iv_rank_source === 'QUANTDATA' ? 'proveedor · motor sin historia suficiente'
        : (v.iv_rank_reason || 'sin IV rank'));
    const pc = Q.num(v.iv_percentile, NaN);
    set('volPct', Q.isNum(pc) ? `${pc.toFixed(0)}%` : '—');
    // Un percentil de 100% junto a una amplitud de 0.00 pp decia «nunca ha estado
    // mas alta» cuando lo cierto es «no ha estado en ningun otro sitio». Sin
    // amplitud no hay percentil, y la razon se escribe.
    set('volPctDetail', Q.isNum(pc)
      ? `${Q.num(v.iv_samples, 0).toFixed(0)} observaciones · ${Q.fmtMinutes(Q.num(v.iv_window_minutes, 0))}`
      : (v.iv_window_degenerate ? (v.iv_window_reason || 'rango observado sin amplitud')
        : 'observaciones por debajo'));
    const lo = Q.num(v.iv_low, NaN), hi = Q.num(v.iv_high, NaN);
    set('volRange', (Q.isNum(lo) && Q.isNum(hi)) ? `${lo.toFixed(2)} – ${hi.toFixed(2)}%` : '—');
    set('volRangeDetail', (Q.isNum(lo) && Q.isNum(hi)) ? `amplitud ${(hi - lo).toFixed(2)} pp` : 'mín – máx de la ventana');
    set('volMedian', Q.isNum(Q.num(v.iv_median, NaN)) ? `${Q.num(v.iv_median).toFixed(2)}%` : '—');
    const dv = Q.num(v.iv_rank_divergence, NaN);
    set('volDiverge', Q.isNum(dv) ? dv.toFixed(0) : '—');
    // No se promedian: miden ventanas distintas. Una separación grande no es un
    // error, es un aviso de que las dos ventanas no describen lo mismo.
    set('volDivergeDetail', Q.isNum(dv)
      ? `motor ${rkNative.toFixed(0)} · proveedor ${rkProv.toFixed(0)}${dv >= 25 ? ' · ventanas distintas' : ''}`
      : (Q.isNum(rkNative) ? 'sin lectura del proveedor' : 'sin lectura del motor'));
    set('volRankNote', v.iv_rank_note
      || 'El IV Rank se publica en cuanto el motor acumula historia propia suficiente; hasta entonces lo aporta el proveedor.');
    set('volSkew', Q.isNum(Q.num(v.skew_25d, NaN)) ? `${Q.num(v.skew_25d) >= 0 ? '+' : ''}${Q.num(v.skew_25d).toFixed(2)} pp` : '—');
    // La deriva sólo se escribe si se MIDIÓ. Un cero sin medición decía
    // «+0.000 pp» junto a un panel que decía «NO DISPONIBLE».
    const drift = Q.num(v.iv_change_pp, NaN);
    set('volDrift', (v.iv_change_measured !== false && Q.isNum(drift))
      ? `${drift >= 0 ? '+' : ''}${drift.toFixed(3)} pp` : '—');
    set('volDriftDetail', (v.iv_change_measured === false)
      ? 'sin dos observaciones con las que medir el cambio' : 'cambio en sesión');

    chart('chartSkew', () => P.curve(el('chartSkew'), {
      fmtY: y => y.toFixed(2), fmtX: x => x < 1 ? x.toFixed(2) + 'd' : x.toFixed(0) + 'd', empty: 'SIN SKEW PUBLICADO',
    })).set([
      { name: 'SKEW 25Δ', color: Q.token('--accent', '#38bdf8'), points: (v.skew_curve || []).map(r => ({ x: r.x, y: r.y })) },
      { name: 'CALL 25Δ IV', color: Q.token('--pos', '#22c55e'), points: (v.skew_curve || []).filter(r => r.call_iv != null).map(r => ({ x: r.x, y: r.call_iv })) },
      { name: 'PUT 25Δ IV', color: Q.token('--neg', '#ef4444'), points: (v.skew_curve || []).filter(r => r.put_iv != null).map(r => ({ x: r.x, y: r.put_iv })) },
    ]);

    chart('chartTerm', () => P.curve(el('chartTerm'), {
      fmtY: y => y.toFixed(1) + '%', fmtX: x => x < 1 ? x.toFixed(2) + 'd' : x.toFixed(0) + 'd', empty: 'SIN ESTRUCTURA TEMPORAL',
    })).set([{ name: 'IV POR VENCIMIENTO', color: Q.token('--violet', '#a78bfa'), points: (v.term_structure || []).map(r => ({ x: r.x, y: r.y })) }]);

    chart('chartVolDrift', () => P.lines(el('chartVolDrift'), { fmt: y => y.toFixed(2), zeroLine: false, empty: 'DERIVA DE VOLATILIDAD NO DISPONIBLE' }))
      .set([{ name: 'IV', color: Q.token('--warn', '#f59e0b'), points: (v.drift || []).map(r => ({ t: r.t, v: r.value })) }]);
  }

  function renderEstadisticas(d) {
    const s = d.estadisticas || {};
    set('stContracts', Q.compact(s.contracts, 0));
    // Un cero sin explicación se lee como "el mercado no negoció". Decir de dónde
    // sale la cifra distingue eso de "todavía no he visto ninguna impresión".
    set('stContractsDetail', s.volume_source === 'PRINTS_OBSERVADOS' ? `${s.print_count || 0} prints observados`
      : s.volume_source === 'CADENA_OFICIAL' ? 'volumen oficial de la cadena · sin cinta'
      : s.volume_source === 'AGREGADO_DEL_MOTOR' ? 'agregado del motor · sin desglose por strike'
      : (s.volume_reason || 'sesión observada'));
    // `Q.money(null)` ya devuelve «—»; aquí se añade POR QUÉ, que es lo que
    // distingue «no hubo prima» de «la prima no se puede medir sin cinta».
    set('stPremium', Q.money(s.premium, 1));
    set('stPremiumDetail', s.premium_available === false
      ? (s.premium_reason || 'sin impresiones sobre las que medir prima')
      : 'prima total observada');
    set('stPcr', Q.isNum(Q.num(s.put_call_volume_ratio, NaN)) ? Q.num(s.put_call_volume_ratio).toFixed(2) : '—');
    set('stPcrDetail', `C ${Q.compact(s.call_volume, 0)} · P ${Q.compact(s.put_volume, 0)}`);
    set('stAvgSize', Q.isNum(Q.num(s.avg_size, NaN)) ? Q.num(s.avg_size).toFixed(1) : '—');

    chart('chartTradeSide', () => P.bars(el('chartTradeSide'), { fmt: v => Q.money(v, 1), zeroCenter: false, empty: 'SIN PRINTS CLASIFICADOS' }))
      .set((s.trade_side || []).map((r, i) => ({
        label: r.label, value: Q.num(r.value, 0), key: r.label,
        color: i === 0 ? Q.token('--pos', '#22c55e') : i === 1 ? Q.token('--neg', '#ef4444') : Q.token('--text-mute', '#5b6880'),
      })));

    chart('chartMarketShare', () => P.bars(el('chartMarketShare'), { fmt: v => Q.compact(v, 1), zeroCenter: false, empty: 'SIN CUOTA POR VENCIMIENTO' }))
      .set((s.market_share || []).map(r => ({
        label: String(r.label || '').slice(5) || String(r.label || ''),
        value: Q.num(r.contracts != null ? r.contracts : r.premium, 0), key: r.label,
      })));

    // Las filas de cadena oficial no tienen prima, operaciones ni sesgo: ahí no hay
    // cinta que clasificar. Se muestran con guion, nunca con un cero inventado.
    fillTable('tblContracts', s.contract_rows || [], r => [
      r.label,
      r.premium == null ? '—' : Q.money(r.premium, 1),
      Q.compact(r.contracts, 0),
      r.trades == null ? '—' : String(r.trades || 0),
      cell(Q.isNum(Q.num(r.bias, NaN)) ? `${Q.num(r.bias) >= 0 ? '+' : ''}${Q.num(r.bias).toFixed(0)}%` : '—',
        Q.num(r.bias, 0) >= 0 ? 'pos' : 'neg'),
      cell(esc(r.source === 'CADENA_OFICIAL' ? 'CADENA' : 'CINTA'), r.source === 'CADENA_OFICIAL' ? 'dim' : ''),
    ], 'Sin prints de opciones ni volumen de cadena');
  }

  function renderDarkPool(d) {
    const dp = d.dark_pool || {};
    /* v1.52.0 · La sección lee el DarkPoolViewModel y nada más.
     *
     * El Auditor decía «Dark Flow 608 filas · Dark Pool Levels 349 filas» y los
     * paneles decían SIN DATOS al mismo tiempo, porque los KPI colgaban de la
     * clasificación por venue y de las zonas de liquidez propias: capas
     * DERIVADAS bloqueando a la fuente DIRECTA. Ahora los tres carriles del
     * proveedor llegan ya sumados en `view_model`, y las capas propias siguen
     * existiendo sólo en `audit`.
     */
    const vm = dp.view_model || {};
    const k = vm.kpis || {};
    const st = vm.status || {};

    const cnt = Q.num(k.dark_print_count, NaN);
    const not = Q.num(k.dark_notional, NaN);
    set('dpCount', Q.isNum(cnt) ? Q.compact(cnt, 0) : 'SIN DATOS');
    set('dpNotional', Q.isNum(not) ? Q.money(not, 1) : 'SIN DATOS');
    set('dpLevel', Q.isNum(Q.num(k.dominant_level, NaN)) ? Q.num(k.dominant_level).toFixed(2) : '—');
    set('dpLevelDetail', Q.isNum(Q.num(k.dominant_notional, NaN))
      ? Q.money(k.dominant_notional, 1)
      : ((st.dark_pool_levels || {}).detail || motivoVacio(dp.reason || dp.state, '—')));

    const candles = dp.candles || [];
    /* v1.56.1 · PERÍODO REALMENTE OBSERVADO, por carril.
     *
     * Los tres no cubren la misma ventana: `dark-flow` llega por intervalos de
     * toda la sesión y `equity-prints` como una cola reciente. Enseñar el
     * notional de sesión al lado de un print de los últimos minutos sin decirlo
     * invita a dividir uno entre otro, y esa división no significa nada. */
    const sc = vm.temporal_scope || {};
    const ventana = (l) => {
      const w = sc[l] || {};
      if (!w.first || !w.last) return null;
      return `${Q.hhmm(Q.parseTime(w.first))}–${Q.hhmm(Q.parseTime(w.last))}`;
    };
    const vFlow = ventana('dark_flow'), vPrints = ventana('equity_prints');
    const sesion = (vm.session || {}).resolved || dp.session_date || '';
    set('dpPeriodo', sesion ? `SESIÓN ${sesion}` : (vFlow || 'SIN DATOS'));
    set('dpPeriodoDetail', (vFlow || vPrints)
      ? [vFlow ? `flujo ${vFlow}` : null,
         vPrints ? `prints ${vPrints}` : null,
         'niveles: foto acumulada'].filter(Boolean).join(' · ')
      : 'ventana realmente observada por carril');

    const last = candles.length ? Q.num(candles[candles.length - 1].c, NaN) : NaN;
    // VWAP oscuro: Σ(precio × acciones) / Σ(acciones) sobre prints DARK_POOL. Si
    // no hay prints clasificados, cae al VWAP de las velas, declarado como tal.
    const vwDark = Q.num(k.dark_vwap, NaN);
    const vw = Q.isNum(vwDark) ? vwDark : Q.num(dp.vwap, NaN);
    set('dpVwap', Q.isNum(vw) ? vw.toFixed(2) : '—');
    set('dpVwapDetail', !Q.isNum(vw) ? 'sin prints oscuros con los que ponderar'
      : `${Q.isNum(last) ? (last >= vw ? 'precio sobre VWAP' : 'precio bajo VWAP') : 'VWAP oscuro'}`
        + (Q.isNum(vwDark) ? ' · prints DARK_POOL' : ' · velas (respaldo)'));

    // Sin denominador, el notional off-exchange no dice nada: 40 M$ es mucho o poco
    // según si el total de large prints fueron 60 M$ o 4.000 M$.
    // v1.42.7 · La proporción puede venir de dos vías distintas y NO significan lo
    // mismo: Quant Data la mide sobre todo el volumen de la sesión; la
    // clasificación por venue, sólo sobre los large prints que la cinta dejó ver.
    // Publicar el número sin decir cuál es lo hace inutilizable.
    // El porcentaje NO se estima. `dark-flow` sólo trae lo off-exchange, así que
    // ahí no existe denominador; hace falta el universo completo de prints
    // dark + lit. Sin él va en SIN DATOS, nunca en 0%.
    const share = Q.num(k.dark_share_pct, NaN);
    set('dpShare', Q.isNum(share) ? `${share.toFixed(1)}%` : 'SIN DATOS');
    set('dpShareDetail', Q.isNum(share)
      ? `${String(k.dark_share_basis || '').toLowerCase().replace(/_/g, ' ')}`
      : (k.dark_share_reason || 'sin volumen con el que comparar'));

    // Auditoría: las dos medidas, una al lado de la otra. Dos vías independientes
    // que coinciden valen más que una sola; si no coinciden, eso es información.
    const dv = Q.num(k.dark_volume, NaN);
    const flowSt = st.dark_flow || {};
    set('dpDarkVolume', Q.isNum(dv) ? `${Q.compact(dv, 1)} acc` : 'SIN DATOS');
    set('dpDarkVolumeDetail', Q.isNum(dv)
      ? `${Q.num((vm.flow || {}).count, 0).toFixed(0)} intervalos observados`
      // SCHEMA_MISMATCH es distinto de «no vino nada»: el proveedor respondió y
      // el contrato no encaja. Decirlo con ese nombre es lo que permite arreglarlo.
      : (flowSt.detail || motivoVacio(dp.reason || dp.state, 'sin volumen oscuro en este ciclo')));

    // Cobertura por vía, en lenguaje de análisis: el nombre de la herramienta
    // del proveedor y la causa exacta viven en el Auditor, no aquí.
    // Cobertura desde el ESTADO de cada carril del proveedor, no desde las capas
    // derivadas: un carril con filas cuenta como cubierto aunque la clasificación
    // por venue no haya confirmado nada.
    const VIA = { dark_flow: 'flujo oscuro', dark_pool_levels: 'niveles', equity_prints: 'impresiones' };
    const live = [], broken = [];
    for (const key of ['dark_flow', 'dark_pool_levels', 'equity_prints']) {
      const lane = st[key] || {};
      (lane.state === 'DATA_OK' ? live : broken).push(VIA[key] || key);
    }
    set('dpCoverage', vm.coverage || '—');
    set('dpCoverageDetail', live.length
      ? (broken.length ? `con dato: ${live.join(' · ')} · sin dato: ${broken.join(' · ')}`
                       : `con dato: ${live.join(' · ')}`)
      : ((st.dark_flow || {}).detail || 'ninguna vía con dato en este ciclo'));

    const audit = dp.audit || {};
    const dpp = Q.num(audit.delta_pp, NaN);
    set('dpAudit', Q.isNum(dpp) ? `${dpp >= 0 ? '+' : ''}${dpp.toFixed(1)} pp` : '—');
    set('dpAuditDetail', Q.isNum(dpp)
      ? `estructura ${Q.num(audit.provider_dark_share_pct, 0).toFixed(1)}% · cinta ${Q.num(audit.venue_classification_share_pct, 0).toFixed(1)}%`
      : 'sólo una de las dos vías tiene dato');

    const big = k.largest_print || dp.off_exchange_largest || null;
    const bigNot = big ? Q.num(big.notional, NaN) : NaN;
    set('dpBiggest', Q.isNum(bigNot) ? Q.money(bigNot, 1) : '—');
    set('dpBiggestDetail', big
      ? `${Q.isNum(Q.num(big.price, NaN)) ? Q.num(big.price).toFixed(2) : '—'} · ${Q.compact(Q.num(big.shares !== undefined ? big.shares : big.size, 0), 0)} acc · ${esc(String(big.venue || big.exchange_name || big.exchange || '—'))}`
      : ((st.equity_prints || {}).detail || '—'));

    // Los niveles del proveedor mandan; los propios sólo si aquéllos no vinieron.
    const vmLevels = (vm.levels || []).length ? vm.levels : (dp.levels || []);
    chart('chartDarkPool', () => P.pricePrints(el('chartDarkPool'), { empty: 'SIN OFF-EXCHANGE CONFIRMADO EN LA SESIÓN' }))
      .set(candles, (vm.prints || []).length ? vm.prints : (dp.prints || []),
           vmLevels.slice(0, 6).map(l => ({ name: 'DP', price: l.price })));

    fillTable('tblDarkPoolPrints', dp.off_exchange_top || [], p => {
      const px = Q.num(p.price, NaN), vwapRef = Q.num(dp.vwap, NaN);
      const rel = (Q.isNum(px) && Q.isNum(vwapRef)) ? px - vwapRef : NaN;
      // El print puede venir de Quant Data (`t`, `venue`) o de la clasificación por
      // venue propia (`timestamp`, `exchange_name`). Se leen las dos formas para que
      // la tabla no dependa de cuál de las dos fuentes esté sosteniendo la vista.
      const when = String(p.t || p.time || p.timestamp || '');
      return [
        (when.length > 11 ? when.slice(11, 19) : when) || '—',
        Q.isNum(px) ? px.toFixed(2) : '—',
        Q.compact(Q.num(p.size, 0), 0),
        Q.money(p.notional, 1),
        esc(String(p.venue || p.exchange_name || p.exchange || '—')),
        cell(Q.isNum(rel) ? `${rel >= 0 ? '+' : ''}${rel.toFixed(2)}` : '—', Q.isNum(rel) ? (rel >= 0 ? 'pos' : 'neg') : 'dim'),
      ];
    }, 'Sin prints off-exchange confirmados en la sesión todavía');

    fillTable('tblDarkPool', dp.levels || [], l => {
      const lo = Q.num(l.low, NaN), hi = Q.num(l.high, NaN);
      const conc = Q.num(l.concentration, NaN);
      // `shares` sólo lo publica dark-pool-levels de Quant Data. Las zonas propias
      // no lo tienen, y ahí va vacío: un 0 diría que no hubo acciones, que es falso.
      const sh = Q.num(l.shares, NaN);
      // Origen en lenguaje de análisis: qué MIDE cada vía, no quién la sirve.
      const origin = String(l.source || '') === 'QUANTDATA_DARK_POOL_LEVELS' ? 'ESTRUCTURA' : 'CINTA';
      return [
        Q.num(l.price).toFixed(2),
        (Q.isNum(lo) && Q.isNum(hi)) ? `${lo.toFixed(2)}–${hi.toFixed(2)}` : '—',
        Q.money(l.notional, 1),
        Q.isNum(sh) && sh > 0 ? Q.compact(sh, 0) : '—',
        String(l.prints || 0),
        esc(l.zone_type || '—'),
        Q.isNum(conc) ? `${(conc * 100).toFixed(0)}%` : '—',
        cell(Q.isNum(Q.num(l.distance_pct, NaN)) ? `${Q.num(l.distance_pct) >= 0 ? '+' : ''}${Q.num(l.distance_pct).toFixed(2)}%` : '—',
          Q.num(l.distance_pct, 0) >= 0 ? 'pos' : 'neg'),
        origin,
      ];
    }, motivoVacio(dp.reason, 'Sin zonas off-exchange publicadas'));
  }


  function renderMacro(d) {
    const m = d.macro || {};
    set('mcStress', Q.isNum(Q.num(m.stress_score, NaN)) ? Q.num(m.stress_score).toFixed(1) : '—');
    set('mcStressLabel', m.stress_label || m.reason || '—');
    set('mcAsset', Q.isNum(Q.num(m.asset_score, NaN)) ? Q.num(m.asset_score).toFixed(1) : '—');
    set('mcRegime', m.regime || '—');

    const cu = m.curve || {}, cr = m.credit || {};
    const cv = Q.num(cu.value, NaN);
    set('mcCurve', Q.isNum(cv) ? `${cv >= 0 ? '+' : ''}${cv.toFixed(2)} pp` : '—');
    // Una curva invertida es la señal de bonos que más pesa en la sesión.
    set('mcCurveDetail', Q.isNum(cv) ? (cv < 0 ? 'INVERTIDA · recesión descontada' : 'PENDIENTE POSITIVA') : '—');
    set('mcCredit', Q.isNum(Q.num(cr.value, NaN)) ? `${Q.num(cr.value).toFixed(2)}%` : '—');
    set('mcCreditDetail', cr.as_of ? `al ${String(cr.as_of).slice(0, 10)}` : 'prima de riesgo');

    const rates = m.rates || [];
    pill('mcRatesPill', rates.length ? 'live' : 'off', rates.length ? `${rates.length} series` : (m.reason || 'sin datos'));
    const palette = [Q.token('--accent', '#38bdf8'), Q.token('--violet', '#a78bfa'), Q.token('--warn', '#f59e0b')];
    chart('chartRates', () => P.lines(el('chartRates'), {
      fmt: v => v.toFixed(2) + '%', zeroLine: false, empty: 'SIN SERIES DE TIPOS',
    })).set(rates.map((r, i) => ({
      name: r.label, color: palette[i % palette.length],
      points: (r.history || []).map(h => ({ t: h.t, v: h.v })),
    })));

    chart('chartCurve', () => P.lines(el('chartCurve'), {
      fmt: v => v.toFixed(2), zeroLine: true, empty: 'SIN CURVA NI CRÉDITO',
    })).set([
      { name: 'CURVA 10Y−2Y', color: Q.token('--accent', '#38bdf8'), points: (cu.history || []).map(h => ({ t: h.t, v: h.v })) },
      { name: 'HY OAS', color: Q.token('--neg', '#ef4444'), points: (cr.history || []).map(h => ({ t: h.t, v: h.v })) },
    ]);

    // Impacto sobre la sesión operativa de Nueva York.
    const se = m.session || {};
    set('mcEvent', se.event_title || 'SIN EVENTO DE ALTO IMPACTO');
    const mins = Q.num(se.minutes_to_event, NaN);
    set('mcEventWhen', se.event_when
      ? `${String(se.event_when).replace('T', ' ').slice(0, 16)}${Q.isNum(mins) ? ` · en ${Q.fmtMinutes(mins)}` : ''}`
      : 'calendario sin eventos marcados');
    pill('mcSessionPill', se.in_session ? 'warn' : se.event_title ? 'live' : 'off',
      se.in_session ? 'DENTRO DE LA SESIÓN' : se.event_title ? 'FUERA DE LA SESIÓN' : 'sin evento');

    const lo = Q.num(se.low, NaN), hi = Q.num(se.high, NaN);
    set('mcRange', (Q.isNum(lo) && Q.isNum(hi)) ? `${lo.toFixed(2)} – ${hi.toFixed(2)}` : '—');
    const em = Q.num(se.expected_move, NaN), emp = Q.num(se.expected_move_pct, NaN);
    set('mcRangeDetail', Q.isNum(em)
      ? `±${em.toFixed(2)} (${emp.toFixed(2)}%) · IV ${Q.num(se.atm_iv, 0).toFixed(1)}% · ${Q.fmtMinutes(Q.num(se.session_minutes_left, 0))} de sesión`
      : 'sin IV ATM publicada');

    set('mcBias', se.bias || '—');
    const agree = Q.num(se.bias_agreement, NaN);
    const nf = Q.num(se.bias_factors, 0);
    set('mcBiasDetail', nf > 0
      ? `acuerdo ${(agree * 100).toFixed(0)}% entre ${nf} factor${nf === 1 ? '' : 'es'} direccional${nf === 1 ? '' : 'es'}`
      : 'sin factores direccionales medidos');
    // El régimen es una lectura aparte: dice si el movimiento se frena o se
    // acelera, no hacia dónde va.
    set('mcRegime2', se.hedging_regime || '—');
    set('mcRegimeDetail', se.hedging_regime === 'AMORTIGUA' ? 'gamma positiva · rango contenido'
      : se.hedging_regime === 'AMPLIFICA' ? 'gamma negativa · movimientos extendidos'
      : 'sin GEX neto publicado');
    set('mcPriority', se.priority || '—');
    set('mcPriorityDetail', se.bias === 'MIXTO'
      ? 'los factores no apuntan al mismo lado'
      : `${nf} factor${nf === 1 ? '' : 'es'} con signo`);
    set('mcSessionNote', se.note || '');

    fillTable('tblSessionVotes', se.votes || [], v => [
      esc(v.factor),
      cell(esc(v.kind || 'DIRECCIONAL'), v.kind === 'REGIMEN' ? 'dim' : ''),
      typeof v.value === 'number' ? Q.signedCompact(v.value, 2) : esc(String(v.value ?? '—')),
      esc(v.detail || '—'),
      cell(v.kind === 'REGIMEN' ? 'NO VOTA'
        : `${v.sign > 0 ? 'POSITIVO' : v.sign < 0 ? 'NEGATIVO' : 'NEUTRO'}${
          Q.isNum(Q.num(v.weight, NaN)) ? ` · peso ${Q.num(v.weight).toFixed(2)}` : ''}`,
        v.kind === 'REGIMEN' ? 'dim' : v.sign > 0 ? 'pos' : v.sign < 0 ? 'neg' : ''),
    ], 'Sin factores medidos para la sesión');

    const ev = m.events || [];
    pill('mcEventsPill', ev.length ? 'live' : 'off', ev.length ? `${ev.length} eventos` : 'sin calendario');
    fillTable('tblMacroEvents', ev, e => [
      e.title || '—',
      e.when ? String(e.when).replace('T', ' ').slice(0, 16) : '—',
      esc(e.importance || '—'),
      e.previous ?? '—', e.forecast ?? '—', e.actual ?? '—',
    ], m.reason || 'Sin eventos publicados para la sesión');

    const comps = m.asset_components || {}, weights = m.asset_weights || {};
    fillTable('tblMacroComponents', Object.keys(comps).map(k => ({ k, v: comps[k], w: weights[k] })), r => [
      esc(r.k),
      Q.isNum(Q.num(r.v, NaN)) ? Q.num(r.v).toFixed(3) : '—',
      Q.isNum(Q.num(r.w, NaN)) ? `${(Q.num(r.w) * 100).toFixed(0)}%` : '—',
      cell(Q.isNum(Q.num(r.v, NaN)) && Q.isNum(Q.num(r.w, NaN))
        ? (Q.num(r.v) * Q.num(r.w)).toFixed(3) : '—',
        Q.num(r.v, 0) >= 0 ? 'pos' : 'neg'),
    ], 'Sin componentes macro');
  }

  function renderEscenarios(d) {
    const mc = d.montecarlo || {};
    set('mcPaths', Q.isNum(Q.num(mc.path_count, NaN)) ? Q.compact(mc.path_count, 0) : '—');
    set('mcHorizon', Q.isNum(Q.num(mc.horizon_minutes, NaN)) ? `horizonte ${Q.num(mc.horizon_minutes).toFixed(0)} min` : '—');
    const by = {};
    for (const b of mc.quantiles || []) by[b.key] = b;
    const fmtQ = k => by[k] ? Q.num(by[k].price).toFixed(2) : '—';
    const fmtD = k => by[k] && Q.isNum(Q.num(by[k].delta, NaN))
      ? `${Q.num(by[k].delta) >= 0 ? '+' : ''}${Q.num(by[k].delta).toFixed(2)} vs spot` : '—';
    set('mcMedian', fmtQ('p50')); set('mcMedianDelta', fmtD('p50'));
    set('mcP84', fmtQ('p84')); set('mcP84Delta', fmtD('p84'));
    set('mcP16', fmtQ('p16')); set('mcP16Delta', fmtD('p16'));
    pill('mcPill', mc.ready ? 'live' : 'off',
      mc.ready ? `${mc.engine || 'simulación'} · ${mc.is_forecast ? 'proyección' : 'escenario'}` : (mc.reason || 'sin simulación'));

    // Las trayectorias comparten un índice temporal sintético: lo que se lee es la
    // dispersión, no una hora concreta.
    const series = (mc.paths || []).slice(0, 30).map((path, i) => ({
      name: '', color: Q.alpha(Q.token('--accent', '#38bdf8'), 0.18),
      points: path.map((v, j) => ({ t: j * 60000, v })), width: 1,
    }));
    for (const b of mc.quantiles || []) {
      if (b.key !== 'p50') continue;
      series.push({ name: 'MEDIANA', color: Q.token('--text', '#e6edf7'), width: 2,
        points: [{ t: 0, v: b.price }, { t: ((mc.paths || [])[0] || []).length * 60000 || 60000, v: b.price }] });
    }
    chart('chartMonteCarlo', () => P.lines(el('chartMonteCarlo'), {
      fmt: v => v.toFixed(2), zeroLine: false, empty: 'SIN TRAYECTORIAS SIMULADAS',
    })).set(series);

    fillTable('tblQuantiles', mc.quantiles || [], b => [
      b.label,
      Q.num(b.price).toFixed(2),
      cell(Q.isNum(Q.num(b.delta, NaN)) ? `${Q.num(b.delta) >= 0 ? '+' : ''}${Q.num(b.delta).toFixed(2)}` : '—',
        Q.num(b.delta, 0) >= 0 ? 'pos' : 'neg'),
      Q.isNum(Q.num(b.delta_pct, NaN)) ? `${Q.num(b.delta_pct).toFixed(2)}%` : '—',
    ], mc.reason || 'Sin cuantiles');

    fillTable('tblIvScenarios', mc.iv_scenarios || [], r => [
      esc(r.name),
      Q.isNum(Q.num(r.iv_pct, NaN)) ? `${Q.num(r.iv_pct).toFixed(2)}%` : '—',
      Q.isNum(Q.num(r.low, NaN)) ? Q.num(r.low).toFixed(2) : '—',
      Q.isNum(Q.num(r.median, NaN)) ? Q.num(r.median).toFixed(2) : '—',
      Q.isNum(Q.num(r.high, NaN)) ? Q.num(r.high).toFixed(2) : '—',
    ], 'Sin escenarios de volatilidad');

    const ef = d.exposure_forecast || {};
    const flip = Q.num(ef.projected_flip, NaN);
    pill('efPill', ef.ready ? 'live' : 'off', ef.ready
      ? `${(ef.scenarios || []).length} escenarios${Q.isNum(flip) ? ` · flip proyectado ${flip.toFixed(2)}` : ''}`
      : (ef.reason || 'sin perfil'));
    // Las dos mitades de la tabla no salen del mismo cálculo y no tienen por qué
    // sumar: decirlo evita que se lea como un descuadre.
    set('efNote', ef.ready
      ? `GEX NETO lo calcula el motor repreciando la cadena entera al precio del escenario, con IV y OI congelados. `
        + `GEX DEBAJO y GEX ENCIMA reparten el perfil por strike visible (${Q.num(ef.profile_strikes, 0)} strikes) alrededor de ese precio: `
        + `dicen hacia dónde empuja la cobertura, no son un desglose del neto y no suman a él. `
        + (Q.isNum(flip) ? `Flip proyectado en ${flip.toFixed(2)}: ahí la cobertura cambia de signo.` : 'Sin cruce de signo en el rango simulado.')
      : '');
    chart('chartExposureForecast', () => P.bars(el('chartExposureForecast'), {
      fmt: v => Q.signedCompact(v, 2), empty: 'SIN PROYECCIÓN DE EXPOSICIÓN',
    })).set((ef.scenarios || []).map(sc => ({
      label: `${sc.label}\n${Q.num(sc.price).toFixed(0)}`,
      value: Q.num(sc.gex_net, 0), key: sc.label,
    })));

    fillTable('tblExposureForecast', ef.scenarios || [], sc => [
      esc(sc.label),
      Q.num(sc.price).toFixed(2),
      Q.isNum(Q.num(sc.delta_pct, NaN)) ? `${Q.num(sc.delta_pct) >= 0 ? '+' : ''}${Q.num(sc.delta_pct).toFixed(2)}%` : '—',
      Q.signedCompact(sc.gex_below, 2),
      Q.signedCompact(sc.gex_above, 2),
      cell(Q.signedCompact(sc.gex_net, 2), Q.num(sc.gex_net, 0) >= 0 ? 'pos' : 'neg'),
      cell(esc(sc.regime), sc.regime === 'AMORTIGUA' ? 'pos' : sc.regime === 'AMPLIFICA' ? 'neg' : ''),
    ], ef.reason || 'Sin escenarios de exposición');

    const im = d.interval_map || {};
    set('imTitle', `INTERVAL MAP ${im.label || 'GEX'} · EXPOSICIÓN POR STRIKE Y TIEMPO`);
    pill('imPill', im.ready ? 'live' : 'off',
      im.ready
        ? `${im.source === 'ITM_QUANT' ? 'motor' : 'proveedor'} · ${(im.strikes || []).length} strikes × ${(im.times || []).length} intervalos`
        : (im.reason || 'no disponible'));
    // El mapa crece con los strikes, igual que el perfil: con noventa filas en
    // un panel fijo la celda mide cuatro píxeles y el diámetro deja de informar.
    growForRows('chartIntervalMap', (im.strikes || []).length, 9, 46);

    /* v1.55.0 · UNA rejilla canónica, DOS propósitos, y el principal es el mapa.
     *
     * El Interval Map mide un CAMPO: la exposición varía de forma continua entre
     * strikes vecinos y entre intervalos vecinos. Dibujarlo como una nube de
     * puntos obliga a leer celda por celda justo lo que hay que leer como zona
     * —dónde está la concentración, qué forma tiene y hacia dónde migra— y con
     * noventa strikes el punto mide cuatro píxeles y su diámetro deja de decir
     * nada.
     *
     * Los puntos NO desaparecen: pasan a ser la vista de diagnóstico, rotulada
     * RAW, donde cada celda es un valor crudo comprobable uno a uno. Las dos
     * leen la MISMA rejilla (`im.strikes` × `im.times` × `im.matrix`), así que
     * no puede haber dos mapas distintos con el mismo nombre.
     */
    const imFmtX = v => {
      const s = String(v);
      // Marcas ISO completas o etiquetas ya cortas del proveedor.
      const m = /T(\d{2}:\d{2})/.exec(s);
      return m ? m[1] : s.slice(0, 5);
    };
    const imRaw = state.intervalRender === 'raw';
    // Cambiar de vista cambia el TIPO de panel, así que el anterior se suelta:
    // dos paneles sobre el mismo lienzo se pisarían el bitmap.
    if (state.intervalRenderApplied !== state.intervalRender) {
      dropChart('chartIntervalMap');
      state.intervalRenderApplied = state.intervalRender;
    }
    chart('chartIntervalMap', () => (imRaw ? P.dotmap : P.heatmap)(el('chartIntervalMap'), {
      fmtY: v => v.toFixed(2),
      fmtX: imFmtX,
      empty: 'INTERVAL MAP NO DISPONIBLE',
    })).set(im.strikes || [], im.times || [], im.matrix || [], im.price || []);
    // El mapa se describe por lo que MIDE, no por quién lo sirve. La forma del
    // payload y el nombre del proveedor son diagnóstico y viven en el Auditor.
    set('imNote', im.ready
      ? `${im.label || 'exposición'} por intervalo · ${im.cells || 0} celdas`
        + (imRaw ? ' · VISTA RAW: una celda = un valor crudo del proveedor' : '')
        + (im.source_mode === 'FALLBACK' ? ' · estructura propia (respaldo)' : '')
      : 'Sin mapa de intervalos disponible para este activo en este ciclo.');
  }

  /**
   * Por qué una herramienta del proveedor no tiene ruta resuelta, en una línea
   * accionable: la ruta que falló y lo que contestó el servidor.
   */
  function qdDiagnosis(t) {
    const tries = t.attempts || [];
    const failed = tries.filter(a => !a.ok);
    if (!tries.length) {
      const cand = (t.candidates || []).length;
      return `<span class="dim">sin intentos${cand ? ` · ${cand} rutas candidatas` : ''}</span>`;
    }
    const last = failed[failed.length - 1] || tries[tries.length - 1];
    const msg = String(last.error || 'sin detalle').slice(0, 70);
    return `<span class="neg" title="${esc((t.candidates || []).join(' · '))}">`
      + `${esc(String(last.path || '').split('/').pop())} → ${esc(msg)}</span>`
      + `<br><span class="dim">${failed.length}/${tries.length} rutas probadas</span>`;
  }


  // ── ARQUITECTURA ────────────────────────────────────────────────────────────
  // Publica los contratos de v1.42 leídos del sistema en marcha. Un contrato que
  // sólo vive en la documentación se desincroniza del código en dos versiones.
  function renderArquitectura(d) {
    const a = d.arquitectura || {};
    const au = a.auditoria || {};
    const head = au.headline || {};

    const grade = (v) => {
      const s = String(v || '—');
      if (s === 'GOOD' || s === 'ACCIONABLE') return 'pos';
      if (s === 'DEGRADED' || s === 'NO_ACCIONABLE') return 'neg';
      if (s === 'SIN_EVIDENCIA') return 'dim';
      return 'warn';
    };
    const put = (id, whyId, value, n) => {
      set(id, String(value || '—').replace(/_/g, ' '));
      const node = el(id);
      if (node) node.dataset.tone = grade(value);
      set(whyId, n ? `${n} control(es) sin superar` : 'sin controles fallidos');
    };
    const countFor = (dim) => (au.failures || [])
      .filter(f => String(f.dimension || '') === dim).length;
    put('arqModel', 'arqModelWhy', head.MODEL_QUALITY, countFor('MODEL'));
    put('arqData', 'arqDataWhy', head.DATA_QUALITY, countFor('DATA'));
    put('arqDecision', 'arqDecisionWhy', head.DECISION_QUALITY, countFor('DECISION'));
    set('arqDoctrine', a.doctrina || '');

    const c = (a.contrato || {});
    const spec = c.contrato_del_instrumento || {};
    const disp = c.despacho_de_valoracion || {};
    set('arqModelName', String(disp.option_model || '—').replace(/_/g, ' '));
    set('arqModelWhyLong', disp.rationale || '');
    set('arqMult', Q.isNum(Q.num(spec.multiplier, NaN)) ? Q.num(spec.multiplier).toString() : '—');
    set('arqMultSrc', spec.multiplier_source
      ? `origen: ${String(spec.multiplier_source).toLowerCase()}` : '');
    set('arqStyle', spec.exercise_style || '—');
    set('arqSettle', spec.settlement ? `liquidación ${String(spec.settlement).toLowerCase()}` : '');
    set('arqClass', spec.asset_class || '—');
    set('arqSupported', spec.supported === false
      ? (spec.notes || 'sin modelo autorizado') : 'modelo autorizado');

    const pol = ((a.autoridad_por_metrica || {}).policies) || {};
    const fallbackLabel = v => ({
      fail: 'BLOQUEAR MÉTRICA',
      unavailable: 'MOSTRAR N/D',
      degraded: 'CONTINUAR DEGRADADO',
      peer_if_policy_allows: 'USAR PAR SI CONTRATO',
    }[String(v || '').toLowerCase()] || String(v || '—').replace(/_/g, ' ').toUpperCase());
    fillTable('tblAuthority', Object.values(pol), p => [
      esc(p.metric), cell(esc(p.authority), 'accent'),
      esc((p.validation || []).join(', ') || '—'),
      cell(fallbackLabel(p.fallback), 'dim'),
      esc(p.kind || ''),
      cell(p.fusion_allowed ? 'permitida' : 'prohibida', p.fusion_allowed ? 'dim' : 'pos'),
    ], 'Sin políticas de métrica declaradas');

    fillTable('tblAudit', au.failures || [], f => [
      esc(f.area), esc(f.invariant),
      cell(esc(f.severity), f.severity === 'CRITICAL' ? 'neg' : f.severity === 'WARN' ? 'warn' : 'dim'),
      esc(f.detail || ''),
    ], 'Todos los invariantes se cumplen en este ciclo');

    const units = ((a.unidades || {}).units) || {};
    fillTable('tblUnits', Object.entries(units), ([k, v]) => [
      cell(esc(k), 'accent'), esc(v.greek), esc(String(v.rep || '').replace(/_/g, ' ')),
      esc(v.dim), esc(v.label),
    ], 'Sin registro de unidades');
  }

  function renderBacktest(d) {
    const b = d.backtest || {};
    set('btStage', b.stage || '—');
    // La autoridad va junto a la etapa: un modelo que no ha superado su test fuera
    // de muestra no decide nada, y eso no puede quedar en letra pequeña.
    set('btAuthority', b.promoted ? 'validado · sigue sin autoridad direccional'
      : (b.reason || 'SHADOW · no decide nada'));

    const sc = b.scores || {};
    const imp = Q.num(sc.improvement_pct, NaN);
    set('btImprove', Q.isNum(imp) ? `${imp >= 0 ? '+' : ''}${imp.toFixed(1)}%` : '—');
    set('btBrier', (Q.isNum(Q.num(sc.brier_model, NaN)) && Q.isNum(Q.num(sc.brier_base_rate, NaN)))
      ? `Brier ${Q.num(sc.brier_model).toFixed(3)} vs base ${Q.num(sc.brier_base_rate).toFixed(3)}`
      : 'sin evaluación fuera de muestra');

    set('btGates', `${b.gates_passed || 0} / ${b.gates_total || 0}`);
    set('btCalibrator', b.calibrator ? `calibrador ${b.calibrator}` : 'sin calibrador elegido');
    set('btSessions', Q.isNum(Q.num(b.sessions, NaN)) ? Q.num(b.sessions).toFixed(0) : '—');
    const sp = b.split || {};
    set('btSplit', Q.isNum(Q.num(sp.train_sessions, NaN))
      ? `${Q.num(sp.train_sessions).toFixed(0)} tren · ${Q.num(sp.purged_sessions, 0).toFixed(0)} purga · ${Q.num(sp.test_sessions, 0).toFixed(0)} test`
      : 'sin bloques de validación');
    set('btNote', b.note || '');

    fillTable('tblGates', b.gates || [], g => [
      esc(g.gate), esc(g.detail || '—'),
      cell(g.passed === null ? 'SIN MEDIR' : g.passed ? 'SUPERADA' : 'NO SUPERADA',
        g.passed === null ? 'dim' : g.passed ? 'pos' : 'neg'),
    ], b.reason || 'Sin validación acumulada todavía');

    // Fiabilidad: en un modelo bien calibrado los puntos caen sobre la diagonal.
    //
    // v1.47.0 · La diagonal de referencia se dibujaba SIEMPRE, también sin una
    // sola observación. Un panel con una recta trazada de esquina a esquina se
    // lee como un resultado, y lo que había era una recolección en curso: cero
    // sesiones medidas. La referencia sólo tiene sentido junto a algo que
    // comparar con ella.
    const bins = b.reliability || [];
    const relEmpty = bins.length ? 'SIN CURVA DE FIABILIDAD'
      : `RECOLECTANDO · ${Q.num(b.sessions, 0).toFixed(0)} sesiones medidas · aún no hay curva que calibrar`;
    const rel = chart('chartReliability', () => P.lines(el('chartReliability'), {
      fmt: v => v.toFixed(2), zeroLine: false, empty: relEmpty,
    }));
    if (rel.setEmpty) rel.setEmpty(relEmpty);
    rel.set(bins.length ? [
      { name: 'OBSERVADO', color: Q.token('--accent', '#38bdf8'),
        points: bins.map(r => ({ t: Q.num(r.predicted ?? r.p ?? r.bin) * 1000, v: Q.num(r.observed ?? r.rate) })) },
      { name: 'PERFECTO', color: Q.token('--text-mute', '#5b6880'), width: 1,
        points: [{ t: 0, v: 0 }, { t: 1000, v: 1 }] },
    ] : []);
  }

  /* ------------------------------------------------ calendario de backtesting */

  async function pullCalendar() {
    const host = el('btCalendar');
    if (!host) return;
    const days = Number((el('btRange') || {}).value || 45);
    try {
      const end = new Date();
      const start = new Date(end.getTime() - days * 86400000);
      const iso = dt => dt.toISOString().slice(0, 10);
      const c = await api(`/api/backtest/calendar?start=${iso(start)}&end=${iso(end)}`);
      state.calendar = c;
      const counts = c.counts || {};
      pill('btCalPill', counts.CAUSAL ? 'live' : 'warn',
        `${counts.CAUSAL || 0} completos · ${counts.PRECIO || 0} sólo precio`);
      host.innerHTML = (c.days || []).map(d => {
        const cls = d.level === 'CAUSAL' ? 'causal' : d.level === 'PRECIO' ? 'precio' : 'nodata';
        const tag = d.level === 'CAUSAL' ? 'COMPLETO' : d.level === 'PRECIO' ? (d.hydrated ? 'PRECIO' : 'DESCARGAR') : '—';
        return `<button class="d ${cls}" data-day="${esc(d.date)}" data-level="${esc(d.level)}"
                 title="${esc(d.reason || 'Sesión archivada: reproduce estructura completa')}">
                 ${esc(d.date.slice(5))}<small>${tag}</small></button>`;
      }).join('') || '<div class="empty">Sin días de mercado en el rango</div>';
      set('btCalNote', c.note || '');
    } catch (err) {
      if (String(err.message) !== 'auth') console.warn('[calendario]', err.message);
    }
  }

  async function calendarPick(day, level) {
    if (level === 'NO_DISPONIBLE') return;
    if (level === 'PRECIO') {
      toast(`Descargando barras históricas de ${day}…`);
      try {
        const r = await fetch(`/api/backtest/hydrate?date=${encodeURIComponent(day)}`, { method: 'POST' });
        const body = await r.json().catch(() => ({}));
        if (!body.ok) { toast(`No pude descargar ${day}: ${body.reason || r.status}`); return; }
        // Se dice lo que ese día PUEDE y lo que NO puede reproducir, antes de abrirlo.
        toast(`${day}: ${body.bars} barras. Reproduce precio, no estructura de opciones.`);
        await pullCalendar();
      } catch (err) { toast(`Descarga fallida: ${err.message}`); return; }
    }
    const bar = el('replayBar');
    if (bar) bar.hidden = false;
    const sel = el('rpDate');
    if (sel && ![...sel.options].some(o => o.value === day)) {
      sel.insertAdjacentHTML('afterbegin', `<option value="${esc(day)}">${esc(day)}</option>`);
    }
    if (sel) sel.value = day;
    await replayOpenDate(day);
  }

  function bindCalendar() {
    el('btRange')?.addEventListener('change', pullCalendar);
    el('btCalendar')?.addEventListener('click', e => {
      const btn = e.target.closest('button.d');
      if (btn) calendarPick(btn.dataset.day, btn.dataset.level);
    });
  }

  /**
   * Detalle del strike fijado en TRACE.
   *
   * Con CALL+PUT activo se ve la barra partida, pero los NÚMEROS de esa barra no
   * estaban en ninguna parte. Aquí sale todo lo que el motor publica de ese strike:
   * el desglose call/put de GEX y DEX con su total, el interés abierto y el volumen
   * partidos, la liquidez observada de la zona y cuánto se ha movido la exposición
   * en vivo desde el último snapshot de cadena.
   */
  function renderStrikeCard(r) {
    const card = el('strikeCard');
    if (!card) return;
    if (!r) { card.hidden = true; return; }
    card.hidden = false;
    set('skStrike', Q.num(r.strike).toFixed(2));
    const dp = Q.num(r.distance_pct, NaN);
    set('skDist', Q.isNum(dp)
      ? `${Q.num(r.distance) >= 0 ? '+' : ''}${Q.num(r.distance).toFixed(2)} · ${dp >= 0 ? '+' : ''}${dp.toFixed(2)}% del precio`
      : '');

    const sc = v => Q.signedCompact(v, 2);
    const cp = v => Q.compact(v, 1);
    const rows = [
      ['head', 'GEX · EXPOSICIÓN GAMMA'],
      ['r', 'Call', sc(r.gex_call), r.gex_call >= 0 ? 'pos' : 'neg'],
      ['r', 'Put', sc(r.gex_put), r.gex_put >= 0 ? 'pos' : 'neg'],
      ['r', 'Total', sc(r.gex), r.gex >= 0 ? 'pos' : 'neg'],
      ['r', 'Δ en vivo', sc(r.gex_live), r.gex_live >= 0 ? 'pos' : 'neg'],
      ['head', 'DEX · EXPOSICIÓN DELTA'],
      ['r', 'Call', sc(r.dex_call), r.dex_call >= 0 ? 'pos' : 'neg'],
      ['r', 'Put', sc(r.dex_put), r.dex_put >= 0 ? 'pos' : 'neg'],
      ['r', 'Total', sc(r.dex), r.dex >= 0 ? 'pos' : 'neg'],
      ['r', 'Δ en vivo', sc(r.dex_live), r.dex_live >= 0 ? 'pos' : 'neg'],
      ['head', 'VEX / CHEX'],
      ['r', 'Vanna', sc(r.vex), ''],
      ['r', 'Charm', sc(r.chex), ''],
      ['head', 'INTERÉS ABIERTO'],
      ['r', 'Call', cp(r.call_oi), 'pos'],
      ['r', 'Put', cp(r.put_oi), 'neg'],
      ['r', 'Total', cp(r.oi), ''],
      ['r', 'Neto', Q.signedCompact(r.net_oi, 1), r.net_oi >= 0 ? 'pos' : 'neg'],
      ['head', 'VOLUMEN'],
      ['r', 'Call', cp(r.call_volume), 'pos'],
      ['r', 'Put', cp(r.put_volume), 'neg'],
      ['r', 'Total', cp(r.volume), ''],
      ['head', 'ZONA'],
      ['r', 'Liquidez', Q.num(r.liquidity).toFixed(0), ''],
      ['r', 'Gravedad', Q.num(r.gravity).toFixed(0), ''],
      ['r', 'Γ+Δ', `${esc(r.state || '—')} · ${Q.num(r.joint).toFixed(0)}`, ''],
      ['r', 'OPRA 5m', `${cp(r.opra_contracts_5m)} · ${Q.money(r.opra_premium_5m, 1)}`, ''],
    ];
    const host = el('skRows');
    if (host) {
      host.innerHTML = rows.map(row => row[0] === 'head'
        ? `<div class="r head">${esc(row[1])}</div>`
        : `<div class="r"><span>${esc(row[1])}</span><b class="${row[3] || ''}">${row[2]}</b></div>`).join('');
    }
  }

  /* ------------------------------------------------------------ Sophia

     Asistente cuantitativa local. No consume créditos, tokens ni mensualidad:
     responde desde el estado del propio motor y, si hay un LLM local configurado,
     lo usa por loopback. Scanner conserva la autoridad direccional.

     La regla que pidió el usuario: se responde POR EL MISMO CANAL por el que se
     pregunta. Escribir devuelve texto; hablar devuelve voz además del texto. */

  // `stt`/`tts` en null = todavía no se ha consultado la capacidad. En false, el
  // micrófono queda deshabilitado y no se envía ninguna transcripción.
  const sophia = { open: false, busy: false, rec: null, chunks: [], recording: false,
                   stt: null, tts: null, sttReason: '' };

  function sophiaSay(who, text, opts) {
    const log = el('sophiaLog');
    if (!log) return;
    const d = document.createElement('div');
    d.className = `msg ${who}${(opts && opts.voice) ? ' voice' : ''}`;
    d.textContent = String(text || '');
    if (opts && opts.meta) {
      const m = document.createElement('small');
      m.textContent = opts.meta;
      d.appendChild(m);
    }
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
  }

  async function sophiaAsk(message, { spoken = false } = {}) {
    const text = String(message || '').trim();
    if (!text || sophia.busy) return;
    sophia.busy = true;
    sophiaSay('me', text);
    const input = el('sophiaInput');
    if (input) input.value = '';
    try {
      const r = await fetch('/api/sophia/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, timeframe: state.timeframe }),
      });
      const body = await r.json().catch(() => ({}));
      const reply = body.reply || body.text || 'No pude responder a eso.';
      // El origen de la respuesta importa: no es lo mismo un dato leído del motor
      // que una interpretación del modelo local.
      const meta = [body.source, body.billing === 'LOCAL_NO_CREDITS' ? 'local · sin coste' : body.billing]
        .filter(Boolean).join(' · ');
      sophiaSay('her', reply, { voice: spoken, meta: meta || undefined });
      // Si preguntó hablando, se le responde hablando.
      if (spoken) await sophiaSpeak(reply);
    } catch (err) {
      sophiaSay('her', `No pude consultar a Sophia: ${err.message}`);
    } finally {
      sophia.busy = false;
    }
  }

  async function sophiaSpeak(text) {
    try {
      if (sophia.tts === false) return;   // sin TTS no se pide audio que no existe
      const r = await fetch('/api/sophia/voice/synthesize', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: String(text || '').slice(0, 1200) }),
      });
      if (!r.ok) { pill('sophiaPill', 'warn', 'voz no disponible en este equipo'); return; }
      const buf = await r.arrayBuffer();
      const url = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
      const audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      await audio.play();
    } catch (err) {
      pill('sophiaPill', 'warn', `voz: ${String(err.message).slice(0, 40)}`);
    }
  }

  async function sophiaToggleMic() {
    const btn = el('sophiaMic');
    if (sophia.recording) {
      sophia.recording = false;
      btn?.classList.remove('rec');
      try { sophia.rec?.stop(); } catch (_) { }
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      pill('sophiaPill', 'off', 'este navegador no permite grabar');
      return;
    }
    // Capacidad antes de intentar. Sin motor de voz a texto, el POST sólo podía
    // devolver un error, y el cliente lo repetía: tres líneas rojas en consola por
    // cada intento de hablar. Preguntar primero cuesta una llamada y evita todas.
    if (sophia.stt === false) {
      pill('sophiaPill', 'off', sophia.sttReason || 'dictado no instalado · escriba a Sophia');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      sophia.chunks = [];
      sophia.rec = new MediaRecorder(stream);
      sophia.rec.ondataavailable = e => { if (e.data && e.data.size) sophia.chunks.push(e.data); };
      sophia.rec.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        pill('sophiaPill', 'warn', 'transcribiendo…');
        try {
          const fd = new FormData();
          fd.append('file', new Blob(sophia.chunks, { type: 'audio/webm' }), 'pregunta.webm');
          const r = await fetch('/api/sophia/voice/transcribe', { method: 'POST', body: fd });
          const body = await r.json().catch(() => ({}));
          if (!r.ok || !body.text) {
            // 501 = esta instalación no tiene STT. No es un fallo transitorio, así
            // que se deshabilita el micrófono en vez de dejar que se reintente.
            if (r.status === 501) {
              sophia.stt = false;
              sophia.sttReason = body.detail || 'dictado no instalado';
              sophiaApplyVoiceCapability();
            }
            pill('sophiaPill', 'off', body.detail || 'no pude transcribir');
            return;
          }
          pill('sophiaPill', 'live', 'local · sin coste');
          await sophiaAsk(body.text, { spoken: true });
        } catch (err) {
          pill('sophiaPill', 'off', `transcripción: ${String(err.message).slice(0, 40)}`);
        }
      };
      sophia.rec.start();
      sophia.recording = true;
      btn?.classList.add('rec');
      pill('sophiaPill', 'warn', 'escuchando… pulsa otra vez para enviar');
    } catch (err) {
      pill('sophiaPill', 'off', 'permiso de micrófono denegado');
    }
  }

  // Deshabilita visiblemente el micrófono cuando no hay dictado, en vez de dejar un
  // botón que sólo puede fallar.
  function sophiaApplyVoiceCapability() {
    const mic = el('sophiaMic');
    if (!mic) return;
    const off = sophia.stt === false;
    mic.disabled = off;
    mic.classList.toggle('off', off);
    mic.title = off ? (sophia.sttReason || 'dictado no instalado en esta instalación')
      : 'dictar a Sophia';
  }

  async function sophiaStatus() {
    try {
      // La capacidad manda: dice qué hay instalado ANTES de ofrecerlo.
      const cap = await api('/api/sophia/capabilities');
      sophia.stt = cap.stt === true;
      sophia.tts = cap.tts === true;
      sophia.sttReason = cap.stt_reason || '';
      sophiaApplyVoiceCapability();

      const st = await api('/api/sophia/status').catch(() => ({}));
      const bits = [];
      if (st.local_llm || st.llm) bits.push('modelo local');
      bits.push(sophia.stt ? 'escucha' : 'sólo texto');
      if (sophia.tts) bits.push('habla');
      pill('sophiaPill', 'live', `${bits.join(' · ')} · sin coste`);
    } catch (_) {
      pill('sophiaPill', 'off', 'no disponible');
    }
  }

  function bindSophia() {
    // La capacidad se consulta al arrancar, no al abrir el panel. Si se esperara a
    // abrirlo, el micrófono aparecería habilitado hasta que el usuario lo pulsara,
    // que es justo el intento que no debe llegar a ocurrir.
    sophiaStatus().catch(() => { });
    el('sophiaBtn')?.addEventListener('click', async () => {
      const p = el('sophiaPanel');
      if (!p) return;
      p.hidden = !p.hidden;
      el('sophiaBtn')?.classList.toggle('on', !p.hidden);
      if (!p.hidden && !sophia.open) {
        sophia.open = true;
        if (sophia.stt === null) await sophiaStatus();
        sophiaSay('her', 'Soy Sophia. Pregúntame por niveles, exposición, flujo o por el propio programa. '
          + 'Si me hablas, te respondo hablando.');
      }
    });
    el('sophiaClose')?.addEventListener('click', () => {
      const p = el('sophiaPanel'); if (p) p.hidden = true;
      el('sophiaBtn')?.classList.remove('on');
    });
    el('sophiaSend')?.addEventListener('click', () => sophiaAsk((el('sophiaInput') || {}).value));
    el('sophiaInput')?.addEventListener('keydown', e => {
      // Enter envía; Shift+Enter hace salto de línea.
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sophiaAsk(e.target.value); }
    });
    el('sophiaMic')?.addEventListener('click', sophiaToggleMic);
  }

  /* Identidad REAL de cada línea de TRACE, tal y como sale del motor.
   *
   * No deduce el nombre por el color ni renombra nada: pinta la procedencia que
   * el nivel ya trae. Si un `kind` no está en el registro, se dice con esas
   * palabras en vez de inventarle un origen.
   */
  /* La barra del PLAN del Scanner, dentro del encabezado de TRACE.
   *
   * El Scanner es la unica autoridad direccional: aqui solo se REPRESENTA su
   * salida. Sin tesis lista no se inventa entrada, objetivos ni invalidacion —
   * se dice ESPERANDO y se explica que falta.
   */
  function renderScannerBar(d) {
    const p = ((state.trace || {}).scanner_plan) || {};
    const dir = p.direction || '—';
    const el2 = el('sbDirection');
    if (el2) { el2.textContent = dir; el2.dataset.dir = dir; }
    const st = Q.num(p.strength, NaN);
    set('sbStrength', Q.isNum(st) ? `${st.toFixed(0)}/100` : '—');
    const px = v => { const x = Q.num(v, NaN); return Q.isNum(x) ? x.toFixed(x >= 1000 ? 2 : 2) : '—'; };
    set('sbEntry', px(p.entry));
    set('sbInval', px(p.invalidation));
    set('sbObj1', px(p.target1));
    set('sbObj2', px(p.target2));
    const stEl = el('sbState');
    const estado = p.state || 'ESPERANDO';
    if (stEl) { stEl.textContent = estado; stEl.dataset.state = estado; }
    set('sbDetail', p.detail || '');
  }

  function renderLevelIdentity(d) {
    const rows = ((state.trace || {}).level_identity) || [];
    const unknown = rows.filter(r => r.known_to_registry === false).length;
    pill('liPill', rows.length ? (unknown ? 'warn' : 'live') : 'off',
      rows.length ? `${rows.length} líneas${unknown ? ` · ${unknown} sin origen declarado` : ''}`
                  : 'sin niveles en este ciclo');
    fillTable('tblLevelIdentity',
      rows.slice().sort((a, b) => Q.num(b.price, 0) - Q.num(a.price, 0)), r => {
        const p = Q.num(r.price, NaN);
        const pers = r.persistence || {};
        const mag = Q.num(r.magnitude, NaN);
        return [
          Q.isNum(p) ? p.toFixed(p >= 1000 ? 2 : 2) : '—',
          esc(String(r.type || '—')),
          esc(String(r.engine_name || '— (el motor no le puso nombre)')),
          esc(String(r.source || '—')),
          esc(String(r.source_field || '—')),
          esc(String(r.method || '—')),
          // Sin magnitud NO se escribe un cero: el cálculo no publica ninguna.
          Q.isNum(mag) ? `${Q.compact(mag, 2)} · ${esc(String(r.magnitude_field || ''))}`
                       : 'el cálculo no publica magnitud',
          `${Q.num(pers.cycles, 0).toFixed(0)} ciclos`
            + (Q.isNum(Q.num(pers.held_minutes, NaN)) ? ` · ${Q.fmtMinutes(pers.held_minutes)}` : '')
            + (pers.moved_this_cycle ? ' · se movió' : ''),
          esc(String(r.timestamp || '—')).replace('T', ' ').slice(0, 19),
        ];
      }, 'Sin niveles publicados por el motor en este ciclo');
    set('liNote', rows.length
      ? 'La identidad sale del motor, no del color: rojo es `put_wall` Y `risk`, verde es '
        + '`call_wall` Y `target`. Cada fila dice qué función y qué campo produjeron el número.'
      : 'Cuando el motor publique niveles, aquí aparece la procedencia de cada línea.');
  }

  function renderFuentes(d) {
    renderLevelIdentity(d);
    const f = d.fuentes || {};
    const parity = f.parity || {};
    const host = el('providerList');
    const providers = parity.providers || [];

    // El contador lee el MISMO resumen de estados que las insignias de cada fila.
    // Antes eran dos cálculos distintos y podían contradecirse en pantalla.
    const sum = parity.state_summary || {};
    const allOk = sum.all_operational !== false;
    pill('parityPill', !parity.equal_weight ? 'off' : allOk ? 'live' : 'warn',
      parity.equal_weight
        ? `${sum.headline || `${parity.live_count || 0}/${parity.configured_count || 0} operativos`} · pares en igualdad`
        : 'sin proveedores');

    if (host) {
      if (!providers.length) {
        host.innerHTML = '<div class="empty">Sin proveedores configurados</div>';
      } else {
        host.innerHTML = providers.map(p => {
          // Mismo vocabulario que provider_state.py; un proveedor en silencio con
          // el mercado cerrado no se pinta como avería.
          const cls = p.operational ? (p.status === 'LIVE' ? 'live' : 'dim')
            : (p.status === 'DEGRADED' || p.status === 'STALE') ? 'warn'
            : (p.status === 'NOT_CONFIGURED' || p.status === 'DISABLED') ? 'off' : '';
          const q = p.status === 'LIVE' ? 100 : p.status === 'MARKET_CLOSED' ? 70
            : p.status === 'CONNECTED' ? 60 : p.status === 'DEGRADED' ? 55
            : p.status === 'STALE' ? 35 : p.configured ? 30 : 0;
          const age = Q.isNum(Q.num(p.age_seconds, NaN)) ? `${Q.num(p.age_seconds).toFixed(0)}s` : '—';
          return `<div class="provider-row">
            <span class="name">${esc(p.provider)}</span>
            <span class="pill ${cls}">${esc(p.status)}</span>
            <span><span class="qbar"><i style="width:${q}%"></i></span>
              <small class="mute">${esc(p.contributes || '')}</small></span>
            <span class="right dim" title="${esc(p.state_detail || '')}">${esc(p.role)} · ${age}</span>
          </div>`;
        }).join('');
      }
    }

    fillTable('tblChannels', parity.channels || [], c => {
      // v1.45.0 · El rol se lee del ALCANCE que declara el backend, que a su vez lo
      // deriva de la política de métricas. Antes la condición era
      // `scope === 'EXTERNAL_CORROBORATION'` sobre un alcance fijado a mano, así
      // que Quant Data salía como CONTRASTE ACTIVO en canales donde ya era la
      // autoridad primaria. La etiqueta mentía, no el comportamiento.
      const isAuthority = c.authority_is_quantdata === true || c.scope === 'PRIMARY_AUTHORITY';
      const isCorroboration = c.provider === 'QUANTDATA' && !isAuthority;
      const liveN = Q.num(c.tools_live, 0), totalN = Q.num(c.tools_total, 0);
      let role;
      if (isAuthority) {
        role = liveN > 0
          ? '<span class="pos">AUTORIDAD PRIMARIA</span>'
          : `<span class="warn">AUTORIDAD SIN DATOS</span>`;
      } else if (isCorroboration) {
        role = liveN > 0 ? '<span class="pos">CONTRASTE ACTIVO</span>' : '<span class="dim">SIN CONTRASTE</span>';
      } else {
        role = c.selected ? '<span class="pos">AUTORIDAD ACTIVA</span>' : '<span class="dim">PAR</span>';
      }
      let coverage;
      if (isAuthority || isCorroboration) {
        coverage = liveN > 0
          ? `<span class="pos">QD ${liveN}/${totalN}</span>${Q.isNum(Q.num(c.quality, NaN)) ? ` · ${Q.num(c.quality).toFixed(1)}` : ''}`
          : (isAuthority
            // Sin datos de la autoridad, lo que sostiene la vista es un RESPALDO
            // declarado. Llamarlo «núcleo activo» sugería que era lo normal.
            ? `<span class="warn">QD 0/${totalN} · respaldo ${esc(c.fallback_authority || 'ITM')}</span>`
            : `<span class="dim">QD 0/${totalN} · núcleo ${esc(c.native_authority || 'ITM')} activo</span>`);
      } else {
        coverage = Q.isNum(Q.num(c.quality, NaN)) ? Q.num(c.quality).toFixed(1)
          : cell(esc(c.quality_state || 'N/A'), c.quality_state === 'MARKET_CLOSED' ? 'dim' : 'warn');
      }
      return [c.channel, c.provider, role, coverage];
    }, 'Sin arbitraje por canal publicado todavía', true);

    const cov = f.quantdata_coverage || {};
    const q = cov.quota || {};
    const pagesPaused = q.remaining !== null && q.remaining !== undefined
      && Q.num(q.remaining, 0) <= Q.num(q.engine_reserve, 0);
    pill('qdPill', cov.configured ? (cov.live_tools ? 'live' : 'warn') : 'off',
      cov.configured
        ? `${cov.live_tools || 0}/${cov.total_tools || 0}${pagesPaused ? ' · páginas pausadas' : ' herramientas'}`
        : 'sin API key');

    fillTable('tblQdTools', cov.tools || [], t => [
      t.title, t.page,
      `<span class="pill ${t.state === 'LIVE' ? 'live' : t.state === 'DEGRADADO' ? 'warn' : t.state === 'NO_DISPONIBLE' ? 'down' : 'off'}">${esc(t.state)}</span>`,
      // v1.45.0 · Dos columnas, porque son dos preguntas distintas y mezclarlas
      // hacía que un dato del proveedor pareciera calculado por el motor:
      //
      //   PROCEDENCIA — QUIÉN produjo el dato. Si la herramienta responde, es del
      //                 proveedor, y lo sigue siendo aunque el motor lo consuma
      //                 después para derivar inteligencia.
      //   CARRIL      — QUÉ lane hizo la petición. Es transporte y ahorro de
      //                 cuota, no autoría. «MOTOR» a secas se leía como autoría.
      t.state === 'LIVE'
        ? '<span class="pos">DIRECT_PROVIDER · QUANTDATA</span>'
        : `<span class="dim">${esc(t.source_mode || '—')}</span>`,
      t.source === 'ENGINE_LANE_SHARED'
        ? '<span class="dim" title="el carril del motor ya la trajo; no gasta cuota propia">compartido</span>'
        : '<span class="dim">páginas</span>',
      Q.isNum(Q.num(t.age_seconds, NaN)) ? `${Q.num(t.age_seconds).toFixed(0)}s` : '—',
      // Ruta resuelta, o el diagnóstico de por qué no hay ninguna. «No disponible»
      // a secas no se puede accionar; saber qué ruta se probó y qué contestó, sí.
      t.path
        ? `<code class="mute">${esc(t.path)}</code>${Q.isNum(Q.num(t.retry_in_seconds, NaN)) && Q.num(t.retry_in_seconds) > 0 ? `<small class="dim"> · reintento ${Q.num(t.retry_in_seconds).toFixed(0)}s</small>` : ''}`
        : qdDiagnosis(t),
    ], cov.configured ? 'Primer ciclo de recolección en curso' : 'Configura QUANTDATA_API_KEY para activar las páginas del proveedor', true);

    pill('quotaPill', q.rate_limited ? 'down' : (Q.num(q.remaining, 999) < 20 ? 'warn' : 'live'),
      q.rate_limited ? `CUOTA AGOTADA · ${Q.num(q.rate_limited_for_seconds, 0).toFixed(0)}s`
        : (q.remaining === null || q.remaining === undefined ? 'cuota —'
          : `cuota ${q.remaining}${q.limit ? '/' + q.limit : ''}`));

    // v1.46.0 · DARK POOL, carril por carril. Un 400 (hay que corregir el cuerpo),
    // un 5xx (hay que esperar al proveedor) y un mercado cerrado (no hay nada que
    // corregir) dejaban la sección igual de vacía y se veían igual. Aquí no.
    const darkLanes = ((d.auditor || {}).dark_pool || {}).lanes || [];
    fillTable('tblDarkLanes', darkLanes, l => [
      esc(l.title || l.lane || '—'),
      l.state === 'DIRECT_PROVIDER_OK'
        ? '<span class="pos">DATO DIRECTO</span>'
        : (l.is_failure ? `<span class="neg">${esc(l.state)}</span>`
                        : `<span class="dim">${esc(l.state)}</span>`),
      Q.isNum(Q.num(l.rows, NaN)) ? String(Q.num(l.rows)) : '—',
      (l.rejected_fields || []).length
        ? `<code class="mute">${esc((l.rejected_fields || []).join(' · '))}</code>`
        : '<span class="dim">—</span>',
      esc(l.detail || '—'),
      esc(l.remedy || '—'),
    ], 'Sin ciclo de dark pool todavía', true);

    // v1.49.0 · El 400 desglosado. El proveedor dice exactamente qué campo
    // rechaza; enseñarlo convierte «HTTP 400» en una corrección de una línea.
    const rej = [];
    for (const l of darkLanes) {
      const e = l.error || {};
      for (const it of (e.errors || [])) {
        rej.push({ lane: l.title || l.lane, status: e.status, type: e.type,
                   field: it.field, message: it.message });
      }
      if (!(e.errors || []).length && e.detail) {
        rej.push({ lane: l.title || l.lane, status: e.status, type: e.type,
                   field: null, message: e.detail });
      }
    }
    fillTable('tblDarkErrors', rej, r => [
      esc(r.lane),
      `<span class="neg">${esc(String(r.status || '—'))}</span>`,
      esc(r.type || '—'),
      r.field ? `<code class="mute">${esc(r.field)}</code>` : '<span class="dim">—</span>',
      esc(r.message || '—'),
    ], 'Ningún carril de dark pool ha sido rechazado', true);

    // v1.48.0 · Con qué campo se leyó cada magnitud del flujo oscuro. Un carril
    // que responde con seiscientos intervalos y volumen cero es indistinguible
    // de un mercado sin actividad hasta que se ve ESTO.
    const ff = ((d.auditor || {}).dark_pool || {}).flow_fields || {};
    const ffRows = Object.entries(ff.map || {}).map(([k, v]) => ({ magnitud: k, campo: v }));
    if (!ffRows.length && (ff.intervals || 0) > 0) {
      ffRows.push({ magnitud: 'volumen oscuro', campo: null });
    }
    fillTable('tblDarkFields', ffRows, r => [
      esc(r.magnitud),
      r.campo ? `<code class="mute">${esc(r.campo)}</code>`
              : '<span class="neg">ningún campo de la respuesta sirve</span>',
      `${Q.num(ff.intervals_with_volume, 0)} / ${Q.num(ff.intervals, 0)}`,
      (ff.observed || []).length
        ? `<code class="mute">${esc((ff.observed || []).slice(0, 12).join(' · '))}</code>`
        : '<span class="dim">—</span>',
    ], 'Sin respuesta de flujo oscuro en este ciclo', true);

    renderRootCause((state.diagnostics || {}).root_cause);

    fillTable('tblDiag', (state.diagnostics || {}).checks || [], c => [
      c.panel,
      c.ok ? '<span class="pos">CON DATOS</span>' : '<span class="neg">SIN DATOS</span>',
      String(c.count ?? '—'),
      esc(c.source || '—'),
      esc(c.reason || '—'),
    ], 'Diagnóstico no disponible', true);
  }

  /**
   * La CAUSA, encima de la tabla de síntomas.
   *
   * v1.57.0 · Con la cadena caída, el Auditor enseñaba diez paneles en SIN
   * DATOS con diez motivos distintos y ninguno era la causa: los diez colgaban
   * del mismo eslabón. Quien lo leía se llevaba diez investigaciones en vez de
   * una. Esto dice cuál es el eslabón, qué cuelga de él y qué mirar.
   */
  function renderRootCause(rc) {
    const box = document.getElementById('diagRootCause');
    if (!box) return;
    if (!rc || !rc.detected) { box.hidden = true; box.innerHTML = ''; return; }
    const paneles = (rc.panels || []).map(p => `<li>${esc(p)}</li>`).join('');
    const mirar = (rc.what_to_check || []).map(p => `<li>${esc(p)}</li>`).join('');
    box.innerHTML =
      `<div class="rc-title">CAUSA RAÍZ · ${esc(rc.link || 'ESLABÓN DESCONOCIDO')}</div>`
      + `<div class="rc-detail">${esc(rc.detail || '')}</div>`
      + (paneles ? `<ul class="rc-list">${paneles}</ul>` : '')
      + (rc.engine_error
          ? `<div class="rc-detail" style="margin-top:6px">Error del motor: `
            + `<code>${esc(rc.engine_error)}</code></div>` : '')
      + (mirar ? `<div class="rc-detail" style="margin-top:6px">Qué mirar:</div>`
                 + `<ul class="rc-list">${mirar}</ul>` : '');
    box.hidden = false;
  }


  /* --------------------------------------------------------- helpers UI */

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function cell(text, cls) { return `<span class="${cls}">${esc(text)}</span>`; }

  function pill(id, cls, text) {
    const x = el(id);
    if (!x) return;
    x.className = 'pill ' + (cls || '');
    x.textContent = text;
  }

  /** `html` indica que las celdas ya vienen escapadas y pueden llevar marcado. */
  function fillTable(id, rows, mapper, emptyMsg, html) {
    const t = el(id);
    if (!t) return;
    const body = t.querySelector('tbody');
    if (!body) return;
    if (!rows || !rows.length) {
      const cols = t.querySelectorAll('thead th').length || 1;
      body.innerHTML = `<tr><td colspan="${cols}" class="mute" style="text-align:center;padding:16px">${esc(emptyMsg || 'Sin datos')}</td></tr>`;
      return;
    }
    body.innerHTML = rows.map(r => {
      const cells = mapper(r).map(c => {
        const s = String(c === null || c === undefined ? '—' : c);
        // Las celdas con marcado propio ya vienen de cell()/plantillas controladas.
        return `<td>${(html || s.startsWith('<span')) ? s : esc(s)}</td>`;
      }).join('');
      return `<tr>${cells}</tr>`;
    }).join('');
  }

  /** Crea el panel la primera vez y lo reutiliza después. */
  /* --------------------------------------- perfil por strike: una barra cada uno
   *
   * El alto del panel deja de ser una constante de CSS y pasa a derivarse del
   * numero de observaciones. Es la otra mitad de `aggregate: 'none'`: si cada
   * strike tiene que llevar su barra, alguien tiene que reservarle sitio.
   */
  // v1.51.0 · 11 px dejaba el paso por debajo del alto de una etiqueta, asi que el
  // eje se saltaba un strike de cada dos. Con 13 cabe el numero en cada fila.
  const EXP_BAR_PX = 13;

  // El relieve necesita más paso que una barra plana: cada prisma tiene cara
  // superior además de frontal, y con el paso de las barras las caras se pisan.
  const RELIEF_ROW_PX = 19;

  function growForRows(id, count, rowPx, extra) {
    const host = el(id);
    if (!host || !window.ITMQBars) return;
    const need = window.ITMQBars.extentFor(count, { targetThickness: rowPx || EXP_BAR_PX });
    // Nunca por debajo del alto de tarjeta: con pocos strikes el panel no debe
    // encogerse hasta parecer roto.
    host.style.height = Math.max(300, need + (extra === undefined ? 42 : extra)) + 'px';
  }

  /** Barras o relieve. El dato es el mismo; cambia la pregunta que responde. */
  function applyExpView() {
    const scroll = el('chartExposureScroll');
    // El relieve vive ahora en su propio contenedor con scroll: crece con los
    // strikes igual que las barras, así que el que se muestra u oculta es el
    // contenedor, no el lienzo.
    const relief = el('chartExposureReliefScroll') || el('chartExposureRelief');
    if (!scroll || !relief) return;
    const bars = state.expView !== 'relief';
    scroll.style.display = bars ? '' : 'none';
    relief.style.display = bars ? 'none' : '';
    for (const btn of document.querySelectorAll('#expView button')) {
      btn.classList.toggle('active', (btn.dataset.expview === 'relief') === !bars);
    }
    const r = state.charts['chartExposureRelief'];
    if (!bars && r) { r.panel.resize(); r.panel.invalidate(); }
  }

  /** Suelta un panel para poder montar otro TIPO sobre el mismo lienzo.
   *
   * Sin esto, cambiar el Interval Map de mapa continuo a puntos dejaba el panel
   * anterior vivo sobre el mismo host y los dos se pisaban el bitmap. */
  function dropChart(id) {
    const c = state.charts[id];
    if (!c) return;
    try { c.panel && c.panel.destroy && c.panel.destroy(); } catch (err) { console.debug('[CHART] drop', id, err); }
    const host = el(id);
    if (host) host.innerHTML = '';
    delete state.charts[id];
  }

  function chart(id, factory) {
    if (!state.charts[id]) {
      const host = el(id);
      if (!host) return { set() { } };
      state.charts[id] = factory();
    }
    return state.charts[id];
  }

  /* ---------------------------------------------------------- controles */

  function bindControls() {
    for (const b of document.querySelectorAll('#nav button')) {
      b.addEventListener('click', () => navigate(b.dataset.view));
    }

    el('themeBtn')?.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'dark';
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('itmq-theme', next); } catch (_) { }
      Q.redrawAll();
      if (state.trace) Trace.applyData(state.trace);   // rehace el bitmap del heatmap
    });

    el('refreshBtn')?.addEventListener('click', async () => {
      toast('Recalculando…');
      try { await fetch('/api/refresh', { method: 'POST', credentials: 'same-origin' }); } catch (_) { }
      pullBundle(); pullTrace();
    });

    // TRACE
    el('traceLeftMetric')?.addEventListener('change', e => Trace.setMetric('left', e.target.value));
    el('traceRightMetric')?.addEventListener('change', e => Trace.setMetric('right', e.target.value));
    el('traceHeatField')?.addEventListener('change', e => Trace.setHeatField(e.target.value));
    el('traceHeatOpacity')?.addEventListener('input', e => Trace.setHeatOpacity(Number(e.target.value) / 100));
    el('traceShowQflow')?.addEventListener('change', e => Trace.toggle('showQflow', e.target.checked));
    el('traceTf')?.addEventListener('change', e => { state.timeframe = e.target.value; pullTrace(); pullBundle(); });
    el('traceWindow')?.addEventListener('change', e => { state.tailMinutes = Number(e.target.value); pullTrace(); });

    segment('tracePriceMode', b => Trace.setPriceMode(b.dataset.mode));
    segment('traceBreakdown', b => {
      const parts = b.dataset.mode === 'parts';
      Trace.setBreakdown(parts);
      if (parts && !(Trace.hasBreakdown('left') || Trace.hasBreakdown('right'))) {
        toast('Esta métrica no publica desglose call/put; se mantiene el neto');
      }
    });
    segment('traceExpiry', async b => {
      try { await fetch('/api/expiry/select', { method: 'POST', headers: { 'content-type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify({ mode: b.dataset.exp }) }); } catch (_) { }
      pullTrace(); pullBundle();
    });

    const follow = el('traceFollow');
    follow?.addEventListener('click', () => {
      const on = follow.dataset.on !== '1';
      follow.dataset.on = on ? '1' : '0';
      follow.textContent = on ? '● SEGUIR' : '○ LIBRE';
      Trace.setFollow(on);
    });

    el('tracePerspective')?.addEventListener('change', e => {
      // MM = exposición tal y como la soporta el creador de mercado. TRADER invierte
      // el signo mostrado porque el tomador está al otro lado del mismo contrato.
      const mm = e.target.value === 'MM';
      document.documentElement.dataset.perspective = mm ? 'MM' : 'TRADER';
      toast(mm ? 'Perspectiva creador de mercado' : 'Perspectiva tomador');
      if (state.trace) Trace.applyData(state.trace);
    });

    // FLUJO
    el('ofThreshold')?.addEventListener('change', e => Flow.setMinPremium(Number(e.target.value)));
    segment('ofMode', b => Flow.setMode(b.dataset.mode));
    segment('ofPanel', b => {
      Flow.setPanel(b.dataset.panel);
      // El criterio de marcado en oro sólo tiene sentido en Net Drift.
      const g = el('ofGoldGroup');
      if (g) g.hidden = b.dataset.panel !== 'drift';
    });
    segment('ofGold', b => Flow.setGoldRule(b.dataset.gold));

    // EXPOSICIÓN
    el('expMetric')?.addEventListener('change', () => { if (state.bundle) renderExposicion(state.bundle); });
    // La griega del Interval Map la resuelve el backend: cambiarla pide bundle nuevo.
    el('imGreek')?.addEventListener('change', e => { state.intervalGreek = e.target.value; pullBundle(); });
    // La vista NO viaja al servidor: es la misma rejilla dibujada de dos maneras.
    el('imRender')?.addEventListener('change', e => {
      state.intervalRender = e.target.value === 'raw' ? 'raw' : 'field';
      if (state.bundle) renderEscenarios(state.bundle);
    });
    segment('expAxis', () => { if (state.bundle) renderExposicion(state.bundle); });
    // Barras o relieve: no pide datos nuevos, sólo cambia qué se dibuja con los
    // que ya hay.
    segment('expView', b => { state.expView = b.dataset.expview; applyExpView(); });

    // Búsqueda de instrumento
    el('symbolPill')?.addEventListener('click', openSymbolSearch);
    el('symbolClose')?.addEventListener('click', () => el('symbolModal').hidden = true);
    el('symbolModal')?.addEventListener('click', e => { if (e.target.id === 'symbolModal') el('symbolModal').hidden = true; });
    el('symbolInput')?.addEventListener('input', renderSymbolResults);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') el('symbolModal').hidden = true; });
  }

  function segment(id, fn) {
    const host = el(id);
    if (!host) return;
    host.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      for (const x of host.querySelectorAll('button')) x.classList.toggle('active', x === b);
      fn(b);
    });
  }

  /* --------------------------------------------------- búsqueda símbolo */

  async function openSymbolSearch() {
    const m = el('symbolModal');
    if (!m) return;
    m.hidden = false;
    el('symbolInput').value = '';
    el('symbolInput').focus();
    if (!state.catalog.length) {
      try {
        const d = await api('/api/assets/catalog');
        state.catalog = d.assets || d.catalog || [];
      } catch (err) { console.warn('[catalog]', err.message); }
    }
    renderSymbolResults();
  }

  function renderSymbolResults() {
    const host = el('symbolResults');
    if (!host) return;
    const q = String((el('symbolInput') || {}).value || '').trim().toUpperCase();
    const rows = state.catalog
      .filter(a => !q || String(a.symbol || '').includes(q) || String(a.name || '').toUpperCase().includes(q))
      .slice(0, 60);
    if (!rows.length) { host.innerHTML = '<div class="empty">Sin coincidencias</div>'; return; }
    host.innerHTML = rows.map(a => {
      const full = a.full !== false && a.selectable !== false;
      const tag = full ? '' : '<span class="pill warn">SIN CADENA PROPIA</span>';
      return `<button data-symbol="${esc(a.symbol)}" title="${esc(a.name || '')}">
        <span><b>${esc(a.symbol)}</b> <small>${esc(a.name || a.kind || '')}</small></span>
        <span>${tag}<small>${esc(a.kind || '')}</small></span>
      </button>`;
    }).join('');
    for (const b of host.querySelectorAll('button')) {
      b.addEventListener('click', () => selectSymbol(b.dataset.symbol));
    }
  }

  async function selectSymbol(symbol) {
    const target = String(symbol || '').toUpperCase().trim();
    if (!target) return;
    el('symbolModal').hidden = true;
    toast(`Cargando ${target}…`);
    engineState('WAIT', 'CAMBIANDO ACTIVO');
    state.switching = target;
    try {
      // El endpoint declara `symbol` como parámetro de consulta, no como cuerpo.
      // Enviarlo en el body devuelve 422 y el cambio fallaba en silencio: la
      // cabecera se quedaba en CAMBIANDO ACTIVO y el activo volvía al anterior.
      const r = await fetch(`/api/asset/select?symbol=${encodeURIComponent(target)}`, {
        method: 'POST', credentials: 'same-origin',
      });
      let payload = null;
      try { payload = await r.json(); } catch (_) { payload = null; }

      if (!r.ok || (payload && payload.ok === false)) {
        // El motor rechaza el cambio con un motivo concreto (activo no operable,
        // cadena ausente, conmutación en curso). Mostrarlo evita el bucle mudo.
        const why = (payload && (payload.detail || payload.reason || payload.motivo))
          || `HTTP ${r.status}`;
        const kept = (payload && payload.kept_symbol) || state.symbol;
        toast(`No se pudo cambiar a ${target}: ${String(why).slice(0, 110)}`);
        engineState('DEGRADED', `SIGUE EN ${kept}`);
        state.switching = null;
        return;
      }

      state.symbol = target;
      state.trace = null;
      // La conmutación es progresiva: el motor hidrata la cadena nueva en segundo
      // plano. Se espera a que el bundle confirme el símbolo antes de declararlo
      // cambiado, en vez de asumirlo y quedarse con la cabecera colgada.
      const deadline = Date.now() + 45000;
      let confirmed = false;
      while (Date.now() < deadline) {
        await pullBundle();
        const active = String((state.bundle || {}).symbol || '').toUpperCase();
        if (active === target) { confirmed = true; break; }
        await new Promise(res => setTimeout(res, 1200));
      }
      await pullTrace();
      pullDiagnostics();
      state.switching = null;
      if (confirmed) toast(`${target} activo`);
      else {
        toast(`${target} aún hidratando · los paneles se llenarán solos`);
        engineState('WAIT', 'HIDRATANDO');
      }
    } catch (err) {
      state.switching = null;
      if (String(err.message) === 'auth') return;
      toast(`No se pudo cambiar a ${target}`);
      engineState('DEGRADED', 'CAMBIO FALLIDO');
      console.error('[select]', err);
    }
  }

  /* -------------------------------------------------------------- boot */

  function boot() {
    try {
      const th = localStorage.getItem('itmq-theme');
      if (th) document.documentElement.setAttribute('data-theme', th);
    } catch (_) { }

    Trace.mount({ left: el('traceLeft'), main: el('traceMain'), right: el('traceRight') });
    Flow.mount({ price: el('ofPrice'), aggressor: el('ofAggressor'), net: el('ofNet'),
      volume: el('ofVolume'), total: el('ofTotal'),
      drift: el('ofDrift'), driftTotal: el('ofDriftTotal'),
      driftVolume: el('ofDriftVolume') });

    bindControls();

    let start = 'resumen';
    try { start = localStorage.getItem('itmq-view') || 'resumen'; } catch (_) { }
    if (!document.querySelector(`section.view[data-view="${start}"]`)) start = 'resumen';
    navigate(start);

    pullBundle();
    pullTrace();
    pullDiagnostics();

    // Dos cadencias: el trace sigue al mercado, el resto acompaña al motor.
    bindReplay();
    bindSophia();
    Trace.onStrike(renderStrikeCard);
    el('skClose')?.addEventListener('click', () => { Trace.clearStrike(); renderStrikeCard(null); });
    bindCalendar();
    // Durante una reproducción el reloj lo manda el usuario: el ciclo en vivo no
    // puede pisar el instante que está mirando ni adelantar la vela.
    setInterval(() => { if (document.visibilityState === 'visible' && !replay.active) pullTrace(); }, 2500);
    // El precio es una lectura barata: puede ir mucho más rápido que la estructura.
    setInterval(() => { if (document.visibilityState === 'visible' && !replay.active) pullTick(); }, 350);
    setInterval(() => { if (document.visibilityState === 'visible' && !replay.active) pullBundle(); }, 6000);
    setInterval(() => { if (document.visibilityState === 'visible' && state.view === 'fuentes') pullDiagnostics(); }, 15000);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') { if (!replay.active) { pullTrace(); pullBundle(); } Q.redrawAll(); }
    });
    window.addEventListener('resize', () => Q.redrawAll());
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
