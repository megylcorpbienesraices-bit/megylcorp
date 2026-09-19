"""CERTIFICACIÓN v1.44.0 · Muros, migración visible, anclaje y resiliencia del Hub.

Qué defiende cada bloque
------------------------
1 · **Autoridad única de Wall** — el defecto que cierra esta release era que
    `structural_walls()` se llamaba desde tres sitios con tres frames distintos,
    así que el Call Wall de TRACE podía no ser el de RESUMEN y nada lo detectaba.
2 · **Mismas Walls en TRACE y en FLUJO** — no recalculadas, consumidas.
3 · **Gamma Migration anclada al strike real**, no resumida en una tarjeta.
4 · **QFLOW anclado a la vela exacta**, no flotando cerca del gráfico.
5 · **Marcas derivadas aún no aprobadas invisibles** en TRACE.
6 · **Resiliencia del Data Hub** — las cuatro piezas que faltaban.
7 · **Cambio de símbolo transaccional** — números correctos bajo el ticker
    equivocado es el defecto más difícil de ver de todos.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


@pytest.fixture(autouse=True)
def _clean():
    from app.core.wall_engine import WALLS
    from app.core.data_lineage import LINEAGE
    WALLS.reset()
    LINEAGE.reset()
    yield
    WALLS.reset()
    LINEAGE.reset()


def _chain(base: float, magnitude: float = 1e8):
    """Cadena con un muro de calls sembrado arriba y uno de puts abajo.

    El paso entre strikes es PROPORCIONAL al precio, no un dólar fijo. Un ±3 $ es
    medio por ciento en un índice de 600 y un 31 % en una acción de 9.5, o sea
    fuera de la cadena operable: usar el mismo paso para los tres activos sería
    cometer en la prueba justo el error de escala que el programa evita.
    """
    step = base * 0.01
    exposure = [
        {"strike": base - 2 * step, "gex": -magnitude * 3, "put_gex": magnitude * 9},
        {"strike": base - step, "gex": -magnitude, "put_gex": magnitude},
        {"strike": base + step, "gex": magnitude, "call_gex": magnitude},
        {"strike": base + 3 * step, "gex": magnitude * 3, "call_gex": magnitude * 8},
    ]
    oi = [
        {"strike": base - 2 * step, "value": 40000, "put": 38000},
        {"strike": base - step, "value": 3000, "put": 2500},
        {"strike": base + step, "value": 4000, "call": 3500},
        {"strike": base + 3 * step, "value": 52000, "call": 50000},
    ]
    return exposure, oi


# ═══════════════════════════════════════════ 1 · AUTORIDAD ÚNICA DE WALL

@pytest.mark.parametrize("symbol,base", [("SPY", 600.0), ("XLF", 48.0), ("SOFI", 9.5)])
def test_the_wall_engine_combines_exposure_and_open_interest(symbol, base):
    from app.core.wall_engine import resolve_walls
    exposure, oi = _chain(base)
    w = resolve_walls(symbol, exposure_rows=exposure, oi_rows=oi, spot=base)
    step = base * 0.01
    assert w["call_wall"]["strike"] == pytest.approx(base + 3 * step)
    assert w["put_wall"]["strike"] == pytest.approx(base - 2 * step)
    assert w["call_wall"]["method"] == "geometric-exposure-x-open-interest"
    assert w["call_wall"]["oi_available"] is True


def test_a_wall_too_far_from_the_price_is_not_the_wall_of_this_session():
    """El tope de distancia es relativo al precio, no una cantidad de dólares.

    Un strike a 3 $ del precio es medio por ciento en un índice de 600 y un 31 %
    en una acción de 9.5. Sin tope relativo, la cola de la cadena de un activo
    barato se presentaría como su muro.
    """
    from app.core.wall_engine import resolve_walls, MAX_DISTANCE_PCT
    far = 9.5 * (1 + (MAX_DISTANCE_PCT + 5) / 100.0)
    near = 9.5 * 1.02
    w = resolve_walls("SMALL",
                      exposure_rows=[{"strike": far, "call_gex": 9e9},
                                     {"strike": near, "call_gex": 1e6}],
                      oi_rows=[{"strike": far, "call": 90000},
                               {"strike": near, "call": 100}], spot=9.5)
    assert w["call_wall"]["strike"] == pytest.approx(near), "ganó la cola de la cadena"
    # El candidato lejano no se oculta: se publica, pero no como muro vigente.
    strikes = [c["strike"] for c in w["call_wall"]["candidates"]]
    assert any(abs(k - far) < 1e-9 for k in strikes)


def test_call_and_put_wall_are_derived_itm_quant_never_provider_data():
    """Las entradas son del proveedor; la conclusión es nuestra."""
    from app.core.wall_engine import resolve_walls
    from app.core.data_lineage import LINEAGE, METRIC_PLAN, DERIVED
    exposure, oi = _chain(100.0)
    w = resolve_walls("TEST", exposure_rows=exposure, oi_rows=oi, spot=100.0)
    assert w["call_wall"]["source_mode"] == DERIVED
    assert w["put_wall"]["source_mode"] == DERIVED
    assert w["source_mode"] == DERIVED
    for metric in ("ITMQ_CALL_WALL", "ITMQ_PUT_WALL"):
        rec = LINEAGE.for_metric(metric, "TEST")
        assert rec is not None and rec["source_mode"] == DERIVED
        assert rec["provider"] == "ITM_QUANT"
    # Y las entradas siguen siendo del proveedor.
    assert METRIC_PLAN["QD_GEX"].mode == "DIRECT_PROVIDER"
    assert METRIC_PLAN["QD_OPEN_INTEREST_BY_STRIKE"].mode == "DIRECT_PROVIDER"


def test_a_call_wall_is_never_placed_below_the_price():
    """Un muro de calls por debajo del precio ya fue atravesado: es historia."""
    from app.core.wall_engine import resolve_walls
    w = resolve_walls("X", exposure_rows=[{"strike": 90.0, "call_gex": 9e9}],
                      oi_rows=[], spot=100.0)
    assert w["call_wall"]["ready"] is False
    w2 = resolve_walls("X", exposure_rows=[{"strike": 110.0, "put_gex": 9e9}],
                       oi_rows=[], spot=100.0)
    assert w2["put_wall"]["ready"] is False


def test_the_wall_does_not_jump_on_noise_but_does_move_on_structure():
    """Una línea que salta en cada refresco no se puede operar."""
    from app.core.wall_engine import resolve_walls, HYSTERESIS_MARGIN
    oi = [{"strike": 103.0, "call": 50000}, {"strike": 105.0, "call": 50000}]
    first = resolve_walls("H", exposure_rows=[{"strike": 103.0, "call_gex": 1e9},
                                              {"strike": 105.0, "call_gex": 5e8}],
                          oi_rows=oi, spot=100.0)
    assert first["call_wall"]["strike"] == 103.0

    # Mejora marginal: NO mueve la línea.
    noise = resolve_walls("H", exposure_rows=[{"strike": 103.0, "call_gex": 1e9},
                                              {"strike": 105.0, "call_gex": 1.05e9}],
                          oi_rows=oi, spot=100.0)
    assert noise["call_wall"]["strike"] == 103.0
    assert noise["call_wall"]["change_reason"] == "se mantiene por histéresis"

    # Mejora estructural: SÍ la mueve, y declara qué nivel retira.
    real = resolve_walls("H", exposure_rows=[{"strike": 103.0, "call_gex": 1e8},
                                             {"strike": 105.0, "call_gex": 9e9}],
                         oi_rows=oi, spot=100.0)
    assert real["call_wall"]["strike"] == 105.0
    assert real["call_wall"]["retired"] is not None
    assert HYSTERESIS_MARGIN > 0


def test_a_crossed_wall_is_retired_immediately_without_hysteresis():
    """La histéresis protege del ruido, no de la realidad."""
    from app.core.wall_engine import resolve_walls
    resolve_walls("C", exposure_rows=[{"strike": 103.0, "call_gex": 1e9}],
                  oi_rows=[{"strike": 103.0, "call": 1000}], spot=100.0)
    after = resolve_walls("C", exposure_rows=[{"strike": 108.0, "call_gex": 1e8}],
                          oi_rows=[{"strike": 108.0, "call": 900}], spot=105.0)
    assert after["call_wall"]["strike"] == 108.0
    assert "atravesó" in after["call_wall"]["change_reason"]


def test_missing_open_interest_does_not_zero_a_strike():
    """Multiplicar por un dato ausente es inventar un veredicto."""
    from app.core.wall_engine import resolve_walls
    w = resolve_walls("NOI", exposure_rows=[{"strike": 11.0, "call_gex": 5e5}],
                      oi_rows=[], spot=10.0)
    assert w["call_wall"]["ready"] is True
    assert w["call_wall"]["oi_available"] is False
    assert w["call_wall"]["method"] == "exposure-only-no-open-interest"


def test_the_engine_fallback_enters_declared_never_disguised():
    from app.core.wall_engine import resolve_walls
    w = resolve_walls("F", exposure_rows=[], spot=100.0,
                      fallback={"call_wall": 105.0, "put_wall": 95.0, "method": "barrier"})
    assert w["call_wall"]["strike"] == 105.0
    assert w["call_wall"]["fallback_used"] is True
    # Y un respaldo que viole la regla de lado se rechaza igual que la vía principal.
    bad = resolve_walls("F2", exposure_rows=[], spot=100.0,
                        fallback={"call_wall": 90.0, "put_wall": 110.0})
    assert bad["call_wall"]["ready"] is False and bad["put_wall"]["ready"] is False


def test_there_is_exactly_one_wall_authority_for_the_whole_terminal():
    """El defecto de fondo: tres llamadas, tres frames, tres muros posibles."""
    main = text("app/main.py")
    api = text("app/terminal_api.py")
    assert "_resolve_walls(" in main
    assert "walls_from_hub" in main
    # RESUMEN ya no lee los muros de `key_levels_report`.
    assert 'walls = (trace or {}).get("walls")' in api
    assert 'wall_override' in api


def test_resumen_and_trace_cannot_disagree_about_the_wall():
    from app.terminal_api import build_terminal_bundle
    trace = {"walls": {"call_wall": {"strike": 520.0, "ready": True},
                       "put_wall": {"strike": 516.0, "ready": True}},
             "levels": [{"kind": "call_wall", "price": 520.0},
                        {"kind": "put_wall", "price": 516.0}]}
    # `key_levels_report` trae valores DISTINTOS a propósito: no deben ganar.
    b = build_terminal_bundle(
        state={"symbol": "SPY", "ready": True, "spot": 517.0,
               "key_levels_report": {"call_wall": 999.0, "put_wall": 1.0}},
        trace=trace, intelligence={})
    assert b["resumen"]["call_wall"] == 520.0
    assert b["resumen"]["put_wall"] == 516.0
    rows = {r["name"]: r["price"] for r in b["resumen"]["levels"]}
    assert rows["Call Wall"] == 520.0 and rows["Put Wall"] == 516.0
    assert b["walls"]["call_wall"]["strike"] == 520.0


# ═══════════════════════════════════════════ 2 · WALLS EN LAS DOS SECCIONES

def test_flow_consumes_the_same_levels_as_trace_without_recomputing():
    flow = text("app/static/itmq_orderflow.js")
    # El panel de flujo filtra los niveles del MISMO payload; no tiene cálculo propio.
    assert "Q.FLOW_LEVEL_KINDS.indexOf(l.kind)" in flow
    assert "structural_walls" not in flow
    assert "wall_engine" not in flow
    core = text("app/static/itmq_core.js")
    assert "'call_wall'" in core and "'put_wall'" in core


def test_the_wall_levels_carry_the_engine_authority_tag():
    main = text("app/main.py")
    assert '"authority": "ITMQ_WALL_ENGINE"' in main
    # Y los niveles de la sección se SUSTITUYEN, no conviven con los del motor.
    assert 'if not (isinstance(lv, dict) and lv.get("kind") in ("call_wall", "put_wall"))' in main


# ═══════════════════════════════════════════ 3 · GAMMA MIGRATION SOBRE EL STRIKE

def test_gamma_migration_publishes_the_strikes_it_moved_between():
    import app.main as M
    from app.providers.quantdata.tools import norm_interval_map
    # La exposición se va del strike bajo y aparece en el alto.
    data = {}
    for i in range(6):
        strikes = {"515": {"CALL": 100.0 - i * 15, "PUT": 0.0},
                   "520": {"CALL": 10.0 + i * 20, "PUT": 0.0}}
        data[str(1758205800000 + i * 300000)] = {"2026-09-25": strikes}
    im = norm_interval_map({"data": data}, "GAMMA")
    mig = M._resolve_gamma_migration({"interval_map": im}, {}, "SPY")
    assert mig["ready"] is True
    assert mig["from_strike"] == 515.0
    assert mig["to_strike"] == 520.0
    assert mig["kind"] == "MIGRATION"
    assert mig["label"] == "Γ MIG 515 → 520"


def test_gamma_migration_is_drawn_on_the_price_axis_not_in_a_card():
    js = text("app/static/itmq_trace.js")
    assert "function drawGammaMigration(" in js
    # Se ancla al strike, con el eje de precio: `sy(anchor)`.
    body = js[js.index("function drawGammaMigration("):]
    body = body[:body.index("\n  function ")]
    assert "sy(anchor)" in body
    assert "mig.to_strike" in body and "mig.from_strike" in body
    # No hay tarjeta ni panel nuevos para ella.
    html = text("app/templates/terminal.html")
    assert "gammaMigrationCard" not in html and "migPanel" not in html


def test_the_interval_map_stays_provider_data_while_migration_is_derived():
    from app.core.data_lineage import METRIC_PLAN
    assert METRIC_PLAN["QD_INTERVAL_MAP"].mode == "DIRECT_PROVIDER"
    assert METRIC_PLAN["ITMQ_GAMMA_MIGRATION"].mode == "DERIVED"


# ═══════════════════════════════════════════ 4 · QFLOW ANCLADO A LA VELA

def test_qflow_markers_resolve_to_the_candle_that_contains_the_event():
    js = text("app/static/itmq_trace.js")
    assert "function candleAt(" in js
    body = js[js.index("function candleAt("):]
    body = body[:body.index("\n  /** Concentraciones")]
    # Contención estricta en [t, t+bar): un evento fuera de su vela NO se asigna
    # a la anterior, se declara huérfano.
    assert "t - ct >= bar" in body
    marker = js[js.index("function drawQflowMarkers("):]
    marker = marker[:marker.index("\n  /** Marca QFLOW")]
    assert "const hit = candleAt(t);" in marker
    # Centro de la vela, y altura tomada del máximo/mínimo de ESA vela.
    assert "hit.t + hit.bar / 2" in marker
    assert "hit.candle.h" in marker and "hit.candle.l" in marker
    # Sin vela, la marca se dibuja atenuada y declarada, no como si estuviera anclada.
    assert "anchored = false" in marker


def test_the_enriched_order_flow_detail_lives_in_the_hover():
    js = text("app/static/itmq_trace.js")
    assert "function drawQflowTooltip(" in js
    assert "function qflowUnderPointer(" in js
    tip = js[js.index("function drawQflowTooltip("):]
    tip = tip[:tip.index("\n  /** Atribución")]
    for field in ("call", "put", "compra", "venta", "strike", "vto"):
        assert field in tip, field


# ═══════════════════════════════════════════ 5 · MARCAS AÚN NO APROBADAS

def test_the_unapproved_derived_marks_are_not_drawn_in_trace():
    """Se calculan y viajan para el motor, pero NO se pintan todavía."""
    js = text("app/static/itmq_trace.js")
    visible = re.sub(r"//[^\n]*", "", js)
    visible = re.sub(r"/\*.*?\*/", "", visible, flags=re.S)
    for forbidden in ("structural_state", "flow_confluence", "CONTAINMENT",
                      "TRANSITION", "BREAK", "divergence", "persistence"):
        assert forbidden not in visible, forbidden
    # Lo que SÍ se dibuja.
    for drawn in ("drawQflowMarkers", "drawQflowLevel", "drawGammaMigration", "drawLevels"):
        assert drawn in js, drawn


def test_the_engine_still_computes_them_internally():
    from app.core import itmq_intelligence as IQ
    out = IQ.structural_state("X", spot=100.0,
                              levels=[{"price": 100.2, "kind": "call_wall"}],
                              migration={"ready": False}, price_series=[99.9, 100.1])
    assert out["ready"] is True and out["state"] in (IQ.BREAK, IQ.CONTAINMENT,
                                                     IQ.TRANSITION, IQ.UNDEFINED)
    conf = IQ.flow_confluence("X", net_drift=1e6, net_flow=2e6)
    assert conf["ready"] is True


# ═══════════════════════════════════════════ 6 · RESILIENCIA DEL DATA HUB

def test_concurrent_requests_for_the_same_dataset_become_one():
    from app.core.data_hub_runtime import DataHubRuntime

    async def go():
        rt = DataHubRuntime()
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return {"rows": [1]}

        await asyncio.gather(*[rt.fetch("gex", "SPY", fetch) for _ in range(6)])
        return calls["n"], rt.inflight.stats()["saved_requests"]

    n, saved = asyncio.run(go())
    assert n == 1, "seis peticiones simultáneas salieron a la red por separado"
    assert saved == 5


def test_a_slow_channel_does_not_hold_the_cycle_nor_the_other_channels():
    from app.core.data_hub_runtime import DataHubRuntime
    import time as _t

    async def go():
        rt = DataHubRuntime()

        async def slow():
            await asyncio.sleep(3.0)
            return {"rows": [1]}

        async def quick():
            return {"rows": [2]}

        t0 = _t.monotonic()
        slow_res, quick_res = await asyncio.gather(
            rt.fetch("dark_flow", "SPY", slow, timeout_s=0.15),
            rt.fetch("gex", "SPY", quick))
        return _t.monotonic() - t0, slow_res, quick_res

    elapsed, slow_res, quick_res = asyncio.run(go())
    assert elapsed < 1.0, "el canal lento retuvo el ciclo"
    assert slow_res["ready"] is False
    assert quick_res["ready"] is True and quick_res["source"] == "LIVE"


def test_a_late_answer_still_feeds_the_last_known_good():
    """Lo que llega tarde vale para el ciclo siguiente; tirarlo cuesta otra cuota."""
    from app.core.data_hub_runtime import DataHubRuntime

    async def go():
        rt = DataHubRuntime()

        async def late():
            await asyncio.sleep(0.2)
            return {"rows": ["tarde"]}

        first = await rt.fetch("oi", "SPY", late, timeout_s=0.05)
        await asyncio.sleep(0.35)
        second = await rt.fetch("oi", "SPY", late, timeout_s=0.001)
        return first, second

    first, second = asyncio.run(go())
    assert first["ready"] is False
    assert second["ready"] is True
    assert second["source"] == "LAST_KNOWN_GOOD"
    assert second["payload"] == {"rows": ["tarde"]}


def test_the_breaker_opens_after_repeated_failures_and_stops_burning_quota():
    from app.core.data_hub_runtime import DataHubRuntime, BREAKER_THRESHOLD

    async def go():
        rt = DataHubRuntime()
        calls = {"n": 0}

        async def boom():
            calls["n"] += 1
            raise RuntimeError("HTTP 500")

        for _ in range(BREAKER_THRESHOLD + 3):
            await rt.fetch("dark_flow", "SPY", boom)
        return calls["n"], rt.channels.allows("dark_flow"), rt.snapshot()

    n, allows, snap = asyncio.run(go())
    assert n == BREAKER_THRESHOLD, "se siguió llamando a un canal ya descartado"
    assert allows is False
    assert "dark_flow" in snap["channels"]["open"]


def test_the_last_known_good_degrades_by_age_instead_of_vanishing():
    from app.core.data_hub_runtime import (LastKnownGood, FRESH, DEGRADED, STALE,
                                           LKG_DEGRADED_AFTER_S, LKG_STALE_AFTER_S)
    lkg = LastKnownGood()
    lkg.put("gex", "SPY", {"rows": [1]})
    entry = lkg.get("gex", "SPY")
    assert entry.freshness() == FRESH
    entry.stored_at -= (LKG_DEGRADED_AFTER_S + 5)
    assert entry.freshness() == DEGRADED
    assert lkg.read("gex", "SPY")["ready"] is True   # degradado sigue siendo utilizable
    entry.stored_at -= LKG_STALE_AFTER_S
    assert entry.freshness() == STALE
    assert lkg.read("gex", "SPY")["ready"] is False  # obsoleto ya no


def test_incremental_merge_neither_duplicates_the_open_bucket_nor_loses_history():
    from app.core.data_hub_runtime import merge_time_series
    session = [{"t": "09:30", "v": 1}, {"t": "09:31", "v": 2}, {"t": "09:32", "v": 3}]
    # El proveedor republica sólo la cola, con el último bucket ya crecido.
    tail = [{"t": "09:32", "v": 9}, {"t": "09:33", "v": 4}]
    merged = merge_time_series(session, tail)
    assert [r["t"] for r in merged] == ["09:30", "09:31", "09:32", "09:33"]
    assert merged[2]["v"] == 9, "el bucket abierto no se actualizó"
    assert len(merged) == 4, "se duplicó un instante o se perdió historia"


def test_no_section_repeats_a_request_the_hub_already_holds():
    """El Hub es la única puerta: las secciones leen el snapshot, no la red."""
    hub = text("app/core/quant_data_hub.py")
    assert "import httpx" not in hub and "await " not in hub
    api = text("app/terminal_api.py")
    assert "httpx" not in api
    # Los dos carriles del proveedor pasan por el runtime compartido.
    assert "HUB_RUNTIME.fetch" in text("app/providers/quantdata/runtime.py")
    assert "HUB_RUNTIME.fetch" in text("app/providers/quantdata/intelligence.py")


# ═══════════════════════════════════════════ 7 · CAMBIO DE SÍMBOLO

def test_a_late_answer_never_lands_under_the_new_ticker():
    """Números correctos bajo el símbolo equivocado es el defecto que nadie ve."""
    src = text("app/providers/quantdata/intelligence.py")
    assert "self._epoch += 1" in src
    assert "epoch = self._epoch" in src
    assert "if epoch != self._epoch:" in src
    assert "stale_symbol_write" in src
    assert "stale_symbol_adopt" in src


def test_switching_symbol_invalidates_every_per_symbol_layer():
    import app.main as M
    from app.core.wall_engine import WALLS, resolve_walls
    from app.core.data_lineage import LINEAGE
    from app.core.data_hub_runtime import HUB_RUNTIME

    resolve_walls("DIA", exposure_rows=[{"strike": 101.0, "call_gex": 1e9}],
                  oi_rows=[{"strike": 101.0, "call": 500}], spot=100.0)
    HUB_RUNTIME.lkg.put("gex", "DIA", {"rows": [1]})
    assert WALLS.get("DIA", "call_wall") is not None
    assert LINEAGE.for_metric("ITMQ_CALL_WALL", "DIA") is not None

    M._invalidate_symbol_state("DIA", "AAPL")

    assert WALLS.get("DIA", "call_wall") is None, "el muro del activo anterior sobrevivió"
    assert LINEAGE.for_metric("ITMQ_CALL_WALL", "DIA") is None
    assert HUB_RUNTIME.lkg.get("gex", "DIA") is None


def test_the_new_symbol_is_cleared_too_not_only_the_previous_one():
    """Un dato sembrado por una consulta anterior es más viejo que este cambio."""
    import app.main as M
    from app.core.data_hub_runtime import HUB_RUNTIME
    HUB_RUNTIME.lkg.put("gex", "AAPL", {"rows": ["viejo"]})
    M._invalidate_symbol_state("DIA", "AAPL")
    assert HUB_RUNTIME.lkg.get("gex", "AAPL") is None
    src = text("app/providers/quantdata/intelligence.py")
    assert "RAW_CACHE.clear_symbol(previous)" in src
    assert "RAW_CACHE.clear_symbol(sym)" in src


def test_critical_datasets_load_before_secondary_context():
    from app.providers.quantdata.intelligence import _PRIORITY
    critical = ("gex_by_strike", "dex_by_strike", "oi_by_strike", "interval_map_gamma")
    flow = ("net_flow", "net_drift")
    secondary = ("news", "gainers_losers")
    for k in critical:
        assert _PRIORITY[k] == 0, k
    for k in flow:
        assert _PRIORITY[k] == 1, k
    for k in secondary:
        assert _PRIORITY[k] > max(_PRIORITY[c] for c in critical + flow), k


def test_the_asset_switch_does_not_fail_when_invalidation_does():
    """La invalidación es higiene, no una precondición del cambio de activo."""
    import app.main as M
    out = M._invalidate_symbol_state("", "")
    assert isinstance(out, dict)
    src = text("app/main.py")
    block = src[src.index("def _invalidate_symbol_state("):]
    block = block[:block.index("\n@app.get")]
    assert "except Exception as exc:" in block
    assert "_obs_note" in block
