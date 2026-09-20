"""v1.52.0 · Lado agresor y cadena de Dark Pool.

La primera sección es la más delicada del programa: una marca que dice COMPRA
cuando fue VENTA induce a operar al revés. Por eso se prueba valor a valor,
incluyendo los que el código anterior clasificaba al contrario.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════════
# 1 · El lado agresor
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    # Los dos que el prefijo de una letra invertía. `AT_BID` empieza por «A» y
    # la regla anterior lo daba por COMPRA cuando es el vendedor cruzando.
    ("AT_BID", "SELL"),
    ("ABOVE_BID", "SELL"),
    ("BELOW_BID", "SELL"),
    ("AT_ASK", "BUY"),
    ("ABOVE_ASK", "BUY"),
    ("BELOW_ASK", "BUY"),
    # Palabras explícitas.
    ("BUY", "BUY"), ("BOUGHT", "BUY"), ("buy_to_open", "BUY"),
    ("SELL", "SELL"), ("SOLD", "SELL"), ("sell_to_close", "SELL"),
    # NBBO a secas.
    ("ASK", "BUY"), ("BID", "SELL"), ("OFFER", "BUY"),
    # Sin agresor: ejecutado en el medio. No es compra.
    ("MID", "UNKNOWN"), ("MIDPOINT", "UNKNOWN"), ("BETWEEN", "UNKNOWN"),
    # El tipo de contrato NO es una dirección: una put se COMPRA.
    ("CALL", "UNKNOWN"), ("PUT", "UNKNOWN"), ("C", "UNKNOWN"), ("P", "UNKNOWN"),
    # Convenio numérico.
    (1, "BUY"), (-1, "SELL"), (0, "UNKNOWN"), (2.5, "BUY"), (-0.5, "SELL"),
    # Nada utilizable.
    (None, "UNKNOWN"), ("", "UNKNOWN"), ("NO_SIDE", "UNKNOWN"), ("   ", "UNKNOWN"),
])
def test_the_aggressor_is_classified_exactly(raw, expected):
    from app.core.aggressor import classify
    assert classify(raw) == expected, raw


def test_a_value_naming_both_sides_is_not_a_side():
    """`ASK_AND_BID` no dice de qué lado fue; inventar uno sería peor que callar."""
    from app.core.aggressor import classify
    assert classify("ASK_AND_BID") == "UNKNOWN"


def test_the_contract_type_never_becomes_a_direction():
    """Comprar una PUT es una COMPRA. Derivar la dirección del tipo de contrato
    invierte el sentido de la mitad de las operaciones que se señalan."""
    from app.core.aggressor import classify, direction
    for word in ("CALL", "PUT", "CALLS", "PUTS"):
        assert classify(word) == "UNKNOWN", word
        assert direction(word) == 0, word


def test_the_unambiguous_field_wins_over_the_ambiguous_one():
    """`side` es ambiguo —en algunas respuestas es el tipo de contrato— y por eso
    va el último. `aggressor` es inequívoco."""
    from app.core.aggressor import from_row
    verdict, field = from_row({"side": "PUT", "aggressor": "AT_ASK"})
    assert verdict == "BUY" and field == "aggressor"
    # Y si sólo está el ambiguo y no resuelve, se declara sin inventar lado.
    verdict, field = from_row({"side": "PUT"})
    assert verdict == "UNKNOWN" and field == "side"


def test_no_module_classifies_the_side_on_its_own():
    """Una regla repartida diverge. La autoridad es `app/core/aggressor.py`."""
    for rel in ("app/providers/quantdata/tools.py", "app/core/qflow.py"):
        src = _read(rel)
        assert 'startswith(("BUY", "ASK", "A"))' not in src, rel
        assert 'startswith(("SELL", "BID", "B"))' not in src, rel


def test_the_provider_row_carries_the_verdict_and_the_field_it_came_from():
    """Cuando el proveedor cambie de convenio hay que saber qué clave se leía."""
    from app.providers.quantdata.tools import norm_option_order_flow
    out = norm_option_order_flow({"prints": [
        {"timestamp": "2026-09-19T14:30:00Z", "price": 1.25, "size": 10,
         "optionType": "PUT", "strike": 515, "side": "AT_BID"},
        {"timestamp": "2026-09-19T14:31:00Z", "price": 2.50, "size": 4,
         "optionType": "CALL", "strike": 520, "side": "AT_ASK"},
        {"timestamp": "2026-09-19T14:32:00Z", "price": 1.10, "size": 7,
         "optionType": "CALL", "strike": 521, "side": "MID"},
    ]})
    rows = out["rows"]
    assert [r["aggressor"] for r in rows] == ["SELL", "BUY", "UNKNOWN"]
    assert [r["direction"] for r in rows] == [-1, 1, 0]
    assert all(r["aggressor_field"] == "side" for r in rows)


# ════════════════════════════════════════════════════════════════════════════
# 2 · La concentración: dominancia de prima ≠ dirección
# ════════════════════════════════════════════════════════════════════════════

def test_call_dominance_is_not_a_buy():
    """Un intervalo dominado por calls puede ser calls VENDIDAS. La pantalla lo
    marcaba como compra con una flecha verde."""
    from app.core.qflow import apply_aggressor
    events = [{"t": "2026-09-19T14:30:00+00:00", "side": "CALL",
               "premium_side": "CALL_DOMINANT", "aggressor": "UNKNOWN"}]
    attribution = {"events": [{
        "t": "2026-09-19T14:30:00+00:00", "matched": True, "trades": 40,
        "buy_premium": 120_000.0, "sell_premium": 980_000.0,
    }]}
    apply_aggressor(events, attribution)
    assert events[0]["aggressor"] == "SELL", "dominada por calls, pero VENDIDAS"
    assert events[0]["premium_side"] == "CALL_DOMINANT"


def test_a_balanced_bucket_is_mixed_not_a_side():
    from app.core.qflow import apply_aggressor
    events = [{"t": "T", "aggressor": "UNKNOWN"}]
    apply_aggressor(events, {"events": [{"t": "T", "matched": True, "trades": 12,
                                         "buy_premium": 500.0, "sell_premium": 480.0}]})
    assert events[0]["aggressor"] == "MIXED"


def test_an_unattributed_concentration_declares_that_it_has_no_side():
    """Sin cinta en la ventana no se elige lado. Se dice por qué."""
    from app.core.qflow import apply_aggressor
    events = [{"t": "T", "aggressor": "UNKNOWN"}]
    apply_aggressor(events, {"events": []})
    assert events[0]["aggressor"] == "UNKNOWN"
    assert events[0]["aggressor_source"] == "UNATTRIBUTED"
    assert "sin operaciones" in events[0]["aggressor_detail"]


def test_trades_without_a_declared_side_are_not_counted_as_balanced():
    """Cero prima agredida es «no medido», no «repartido»."""
    from app.core.qflow import apply_aggressor
    events = [{"t": "T", "aggressor": "UNKNOWN"}]
    apply_aggressor(events, {"events": [{"t": "T", "matched": True, "trades": 9,
                                         "buy_premium": 0.0, "sell_premium": 0.0}]})
    assert events[0]["aggressor"] == "UNKNOWN"
    assert "sin lado agresor" in events[0]["aggressor_detail"]


def test_the_marker_arrow_follows_the_aggressor_not_the_contract():
    from app.core.qflow import _markers
    marks = _markers([
        {"t": "T1", "premium": 1e6, "aggressor": "BUY", "side": "PUT"},
        {"t": "T2", "premium": 1e6, "aggressor": "SELL", "side": "CALL"},
        {"t": "T3", "premium": 1e6, "aggressor": "UNKNOWN", "side": "CALL"},
        {"t": "T4", "premium": 1e6, "aggressor": "MIXED", "side": "PUT"},
    ])
    # Una PUT comprada lleva flecha de COMPRA; una CALL vendida, de VENTA.
    assert [m["arrow"] for m in marks] == ["▲", "▼", "◆", "◆"]


def test_the_frontend_draws_no_arrow_without_a_known_side():
    """Una flecha inventada sobre un gráfico de operativa puede costar dinero."""
    for rel in ("app/static/itmq_trace.js", "app/static/itmq_orderflow.js"):
        js = _read(rel)
        assert "function flowSide(" in js, rel
        assert "function flowIsBuy(" not in js, rel
        body = js[js.index("function flowSide("):]
        body = body[:body.index("\n  }") + 4]
        # Sólo el agresor decide. Ni el tipo de contrato ni el signo de la prima.
        assert "ev.aggressor" in body, rel
        assert "'CALL'" not in body and "'PUT'" not in body, rel
        assert "premium" not in body, rel
        assert "return null;" in body, rel
        # Y el rombo neutro existe en el dibujo.
        assert "up === null || up === undefined" in js, rel


# ════════════════════════════════════════════════════════════════════════════
# 3 · Dark Pool · la cadena entera, valor a valor
# ════════════════════════════════════════════════════════════════════════════

RAW_DARK_FLOW = {"data": {
    "1758297600000": {"notionalValue": 35_842_110.0, "size": 69428, "tradeCount": 412, "stockPrice": 516.20},
    "1758297660000": {"notionalValue": 12_004_500.0, "size": 23255, "tradeCount": 188, "stockPrice": 516.11},
    "1758297720000": {"notionalValue": 48_990_004.0, "size": 94903, "tradeCount": 651, "stockPrice": 516.44},
}}

RAW_LEVELS = {"latestStockPrice": 516.20, "data": {
    "515.00": {"notionalValue": 120_400_000.0, "size": 233_500, "tradeCount": 1_204},
    "516.00": {"notionalValue": 88_100_000.0, "size": 170_600, "tradeCount": 903},
    "517.00": {"notionalValue": 41_250_000.0, "size": 79_800, "tradeCount": 455},
}}


def test_dark_flow_maps_the_contract_without_guessing():
    """`size` es el share count del contrato. La heurística no lo encontraba
    —no contiene «dark» ni «offExchange»— y 608 filas salían con volumen None."""
    from app.providers.quantdata.tools import norm_dark_flow
    out = norm_dark_flow(RAW_DARK_FLOW)
    assert out["schema_state"] == "CONTRACT_OK"
    assert out["field_map"] == {
        "dark_volume": "size", "dark_notional": "notionalValue",
        "dark_prints": "tradeCount", "stock_price": "stockPrice",
    }
    r = out["rows"][0]
    assert r["dark_volume"] == pytest.approx(69428)
    assert r["dark_notional"] == pytest.approx(35_842_110.0)
    assert r["dark_prints"] == 412
    assert r["stock_price"] == pytest.approx(516.20)


def test_a_response_without_size_is_a_schema_mismatch_not_a_zero():
    """Un cero afirmaría que no hubo volumen oscuro."""
    from app.providers.quantdata.tools import norm_dark_flow
    out = norm_dark_flow({"data": {"1758297600000": {"notionalValue": 1.0, "tradeCount": 2}}})
    assert out["schema_state"] == "SCHEMA_MISMATCH"
    assert out["rows"][0]["dark_volume"] is None
    assert "size" in out["schema_detail"]


def test_the_chain_carries_every_value_unchanged_to_the_view_model():
    """RAW → NORMALIZED → VIEWMODEL. El único cambio permitido es el formato."""
    from app.providers.quantdata.tools import norm_dark_flow, norm_levels
    from app.core import dark_pool_view as DPV

    flow_block = norm_dark_flow(RAW_DARK_FLOW)
    levels_block = norm_levels(RAW_LEVELS)
    vm = DPV.build(flow_block=flow_block, levels_block=levels_block,
                   prints_block={"ready": False}, spot=516.20)

    # Tres buckets reales, comprobados uno a uno.
    assert [b["shares"] for b in vm["flow"]["buckets"]] == [69428.0, 23255.0, 94903.0]
    assert [b["prints"] for b in vm["flow"]["buckets"]] == [412.0, 188.0, 651.0]
    # Y los agregados son la suma exacta de lo anterior.
    assert vm["kpis"]["dark_volume"] == pytest.approx(69428 + 23255 + 94903)
    assert vm["kpis"]["dark_notional"] == pytest.approx(35_842_110.0 + 12_004_500.0 + 48_990_004.0)
    assert vm["kpis"]["dark_print_count"] == 412 + 188 + 651

    # Tres niveles reales, con el dominante por notional.
    assert vm["kpis"]["dominant_level"] == pytest.approx(515.00)
    assert vm["kpis"]["dominant_notional"] == pytest.approx(120_400_000.0)
    by_price = {lv["price"]: lv for lv in vm["levels"]}
    assert by_price[516.00]["shares"] == pytest.approx(170_600)
    assert by_price[517.00]["prints"] == pytest.approx(455)
    # La distancia al spot se deriva, no se inventa.
    assert by_price[517.00]["distance_pct"] == pytest.approx((517.0 - 516.2) / 516.2 * 100, abs=1e-3)


def test_a_derived_layer_can_no_longer_block_the_direct_source():
    """608 filas del proveedor y la pantalla decía SIN DATOS porque esperaba a la
    clasificación por venue. El modelo se arma sin ella."""
    from app.core import dark_pool_view as DPV
    from app.providers.quantdata.tools import norm_dark_flow
    vm = DPV.build(flow_block=norm_dark_flow(RAW_DARK_FLOW),
                   levels_block=None, prints_block=None)
    assert vm["ready"] is True
    assert vm["status"]["dark_flow"]["state"] == "DATA_OK"
    assert vm["coverage"] == "1/3"
    assert vm["authority"] == "QUANT_DATA_DIRECT"
    # Y no lee ninguna capa derivada.
    src = _read("app/core/dark_pool_view.py")
    for word in ("large_prints", "off_exchange_liquidity_zones", "venue_classification"):
        assert word not in src, word


def test_every_lane_declares_its_state_with_a_cause():
    from app.core import dark_pool_view as DPV
    vm = DPV.build(flow_block={"ready": True, "rows": [], "schema_state": "CONTRACT_OK"},
                   levels_block={"ready": False, "reason": "422 sin datos"},
                   prints_block={"ready": True, "rows": [], "error": "HTTP 500"})
    assert vm["status"]["dark_flow"]["state"] == "NO_PROVIDER_DATA"
    assert vm["status"]["dark_pool_levels"]["detail"] == "422 sin datos"
    assert vm["status"]["equity_prints"]["state"] == "PROVIDER_ERROR"
    assert vm["coverage"] == "0/3"


def test_the_off_exchange_share_is_never_estimated():
    """`dark-flow` sólo trae lo oscuro: ahí no hay denominador. Sin universo
    completo el KPI va en None con su causa, nunca en 0%."""
    from app.core import dark_pool_view as DPV
    from app.providers.quantdata.tools import norm_dark_flow
    vm = DPV.build(flow_block=norm_dark_flow(RAW_DARK_FLOW),
                   levels_block=None, prints_block=None)
    assert vm["kpis"]["dark_share_pct"] is None
    assert "universo completo" in vm["kpis"]["dark_share_reason"]

    # Con prints dark + lit sí se puede medir.
    prints = {"ready": True, "rows": [
        {"t": "T1", "price": 516.0, "size": 4_000.0, "notional": 2_064_000.0, "off_exchange": True},
        {"t": "T2", "price": 516.1, "size": 6_000.0, "notional": 3_096_600.0, "off_exchange": False},
    ]}
    vm2 = DPV.build(flow_block=None, levels_block=None, prints_block=prints)
    assert vm2["kpis"]["dark_share_pct"] == pytest.approx(40.0)
    assert vm2["kpis"]["dark_share_basis"] == "EQUITY_PRINTS_DARK_PLUS_LIT"


def test_the_dark_vwap_weights_by_shares():
    from app.core import dark_pool_view as DPV
    prints = {"ready": True, "rows": [
        {"t": "T1", "price": 100.0, "size": 300.0, "notional": 30_000.0, "off_exchange": True},
        {"t": "T2", "price": 110.0, "size": 100.0, "notional": 11_000.0, "off_exchange": True},
    ]}
    vm = DPV.build(flow_block=None, levels_block=None, prints_block=prints)
    assert vm["kpis"]["dark_vwap"] == pytest.approx((100 * 300 + 110 * 100) / 400)
    assert vm["kpis"]["largest_print"]["notional"] == pytest.approx(30_000.0)


def test_equity_prints_asks_for_a_real_session():
    """Fuera de horario el proveedor devuelve vacío si no se le pide sesión, y eso
    se publicaba como «mercado cerrado, cero prints»."""
    from app.providers.quantdata.tools import build_catalog
    body = build_catalog()["equity_prints"].body("DIA")
    assert "sessionDate" in body
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["sessionDate"])
    # Un fin de semana resuelve al viernes.
    from app.providers.quantdata.tools import last_valid_session_date
    from datetime import datetime, timezone
    sat = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    sun = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    assert last_valid_session_date(sat) == last_valid_session_date(sun)


def test_the_section_reads_the_view_model_and_not_the_derived_layers():
    api = _read("app/terminal_api.py")
    assert '"view_model": view_model' in api
    assert "dark_pool_view as _DPV" in api
    app = _read("app/static/itmq_app.js")
    body = app[app.index("function renderDarkPool("):]
    body = body[:body.index("function renderMacro(")] if "function renderMacro(" in body else body
    assert "dp.view_model" in body
    # Los KPI ya no salen de la clasificación por venue.
    assert "dp.off_exchange_share_pct" not in body
    assert "k.dark_share_pct" in body
    assert "k.dark_notional" in body and "k.dark_volume" in body


# ════════════════════════════════════════════════════════════════════════════
# 4 · Cambio de activo · la ráfaga de arranque
# ════════════════════════════════════════════════════════════════════════════

def test_a_symbol_change_opens_a_bounded_burst():
    """Tras cambiar de activo no hay NADA en pantalla; el ritmo de régimen
    permanente tardaba minutos en llenarla."""
    import time as _time
    from app.providers.quantdata.intelligence import QuantDataIntelligence
    qi = QuantDataIntelligence()
    assert qi._bursting() is False, "sin cambio de activo no hay ráfaga"
    qi._burst_until = _time.monotonic() + 5.0
    assert qi._bursting() is True
    qi._burst_until = _time.monotonic() - 1.0
    assert qi._bursting() is False, "la ráfaga caduca sola"


def test_the_burst_only_covers_what_draws_the_screen():
    """Las noticias y los gainers/losers no pueden gastar el turno de la
    exposición, que es la que dibuja TRACE."""
    from app.providers.quantdata.intelligence import (
        _PRIORITY, _PRIORITY_DEFAULT, BURST_MAX_PRIORITY)
    for key in ("gex_by_strike", "dex_by_strike", "oi_by_strike",
                "interval_map_gamma", "net_flow", "net_drift",
                "dark_flow", "dark_pool_levels"):
        assert _PRIORITY.get(key, _PRIORITY_DEFAULT) <= BURST_MAX_PRIORITY, key
    for key in ("news", "gainers_losers", "equity_prints", "oi_over_time"):
        assert _PRIORITY.get(key, _PRIORITY_DEFAULT) > BURST_MAX_PRIORITY, key


def test_the_burst_is_bounded_on_all_three_sides():
    """Una ráfaga sin límites sería martillear la API. Tiene plazo, alcance y
    una salida anticipada cuando lo prioritario ya está servido."""
    src = _read("app/providers/quantdata/intelligence.py")
    assert "BURST_SECONDS" in src and "BURST_MAX_PRIORITY" in src
    assert "BURST_CONCURRENCY" in src and "BURST_CYCLE_SECONDS" in src
    assert "self._burst_until = 0.0" in src, "se apaga sola al terminar"
    # Y nunca toca la reserva del motor: el presupuesto sigue saliendo de QUOTA.
    assert "QUOTA.budget_for_pages(len(remaining_due))" in src


def test_the_symbol_change_is_still_transactional():
    """La ráfaga no puede debilitar la época: una respuesta en vuelo del símbolo
    anterior tiene que seguir descartándose."""
    src = _read("app/providers/quantdata/intelligence.py")
    body = src[src.index("async def select_asset("):src.index("def _bursting(")]
    assert "self._epoch += 1" in body
    assert body.index("self._epoch += 1") < body.index("self._symbol = sym")
    assert "self._burst_until" in body


# ════════════════════════════════════════════════════════════════════════════
# 5 · Mapa dinámico · las ocho opciones
# ════════════════════════════════════════════════════════════════════════════

def test_every_dynamic_map_option_has_a_destination():
    import re
    html = _read("app/templates/terminal.html")
    block = re.search(r'id="traceHeatField".*?</select>', html, re.S).group(0)
    options = re.findall(r'value="([^"]*)"', block)
    assert len(options) == 8, options
    js = _read("app/static/itmq_trace.js")
    fields = js[js.index("const HEATFIELDS = {"):js.index("const INTERVAL_GREEKS")]
    for val in options:
        if val == "off":
            continue   # apagar el mapa no necesita fuente
        assert f"{val}:" in fields, val


def test_a_missing_map_declares_its_cause_instead_of_going_blank():
    """Con ocho opciones en el selector, un fondo en blanco sin causa no deja
    saber cuál de ellas no tiene dato."""
    js = _read("app/static/itmq_trace.js")
    assert "S.heatReason" in js
    assert "ENGINE_FALLBACK" in js, "el respaldo del motor es declarado, no silencioso"
    body = js[js.index("function heatSource("):js.index("function buildHeatBitmap(")]
    # `engineMatrix` es un buscador interno: su `return null` significa «esa clave
    # no está», no «no hay mapa». Se excluye para mirar sólo las SALIDAS de la
    # función, que son las que dejan el fondo en blanco.
    helper_start = body.index("const engineMatrix = key =>")
    helper_end = body.index("};", helper_start) + 2
    exits = body[:helper_start] + body[helper_end:]
    for chunk in exits.split("return null;")[:-1]:
        assert "S.heatReason" in chunk, chunk[-120:]
    hud = js[js.index("set('traceHeatSource'"):]
    hud = hud[:hud.index(");") + 2]
    assert "S.heatReason" in hud and "mapa apagado" in hud


# ════════════════════════════════════════════════════════════════════════════
# 6 · v1.52.1 · El agresor en produccion, y los conteos por etapa
# ════════════════════════════════════════════════════════════════════════════

def test_the_aggressor_falls_back_to_the_nbbo_when_no_field_declares_it():
    """En producción TODAS las marcas salían neutras: el proveedor no publica
    siempre un campo de lado. Comparar el precio con el NBBO no es adivinar, es
    la definición operativa del agresor."""
    from app.core.aggressor import from_row
    assert from_row({"price": 2.50, "bid": 2.40, "ask": 2.50}) == ("BUY", "NBBO")
    assert from_row({"price": 2.40, "bid": 2.40, "ask": 2.50}) == ("SELL", "NBBO")
    # En el medio no hubo agresor: no se inventa uno.
    assert from_row({"price": 2.45, "bid": 2.40, "ask": 2.50})[0] == "UNKNOWN"
    # Y el campo declarado SIEMPRE gana al NBBO.
    assert from_row({"price": 2.50, "bid": 2.40, "ask": 2.50, "side": "AT_BID"}) == ("SELL", "side")


def test_the_nbbo_tolerance_is_relative_to_the_spread():
    """Un céntimo es mucho en una opción de 0.05 y nada en una de 40."""
    from app.core.aggressor import from_row
    # Spread ancho: un céntimo por debajo del ask sigue siendo compra.
    assert from_row({"price": 39.98, "bid": 38.00, "ask": 40.00})[0] == "BUY"
    # Spread estrecho: el mismo céntimo ya no cruza.
    assert from_row({"price": 2.44, "bid": 2.40, "ask": 2.45})[0] == "UNKNOWN"


def test_a_crossed_or_locked_market_gives_no_side():
    from app.core.aggressor import from_row
    assert from_row({"price": 2.45, "bid": 2.50, "ask": 2.40})[0] == "UNKNOWN"
    assert from_row({"price": 2.45, "bid": 2.45, "ask": 2.45})[0] == "UNKNOWN"


def test_the_tape_publishes_its_aggressor_coverage():
    """«Todas las marcas salen neutras» no se podía diagnosticar sin saber si el
    proveedor no manda el campo, lo manda con otro nombre, o son ejecuciones en
    el medio."""
    from app.providers.quantdata.tools import norm_option_order_flow
    out = norm_option_order_flow({"prints": [
        {"timestamp": "2026-09-19T14:30:00Z", "price": 2.50, "size": 10, "bid": 2.40, "ask": 2.50},
        {"timestamp": "2026-09-19T14:31:00Z", "price": 2.40, "size": 5, "bid": 2.40, "ask": 2.50},
        {"timestamp": "2026-09-19T14:32:00Z", "price": 2.45, "size": 3, "bid": 2.40, "ask": 2.50},
    ]})
    cov = out["aggressor_coverage"]
    assert cov["with_side"] == 2 and cov["total"] == 3
    assert cov["pct"] == pytest.approx(66.7, abs=0.1)
    assert cov["by_field"] == {"NBBO": 3}
    # Y el NBBO viaja en la fila para poder AUDITAR la clasificación.
    assert out["rows"][0]["bid"] == pytest.approx(2.40)
    assert out["rows"][0]["ask"] == pytest.approx(2.50)


def test_a_dominant_side_is_no_longer_called_mixed():
    """65/35 ES una concentración compradora; llamarla repartida esconde
    información real. El corte baja de 2:1 a 60/40."""
    from app.core.qflow import apply_aggressor, AGGRESSOR_DOMINANCE_SHARE
    assert AGGRESSOR_DOMINANCE_SHARE == pytest.approx(0.60)
    for buy, sell, expected in [(650, 350, "BUY"), (610, 390, "BUY"),
                                (550, 450, "MIXED"), (200, 800, "SELL")]:
        e = [{"t": "T", "aggressor": "UNKNOWN"}]
        apply_aggressor(e, {"events": [{"t": "T", "matched": True, "trades": 20,
                                        "buy_premium": buy, "sell_premium": sell}]})
        assert e[0]["aggressor"] == expected, (buy, sell)
    # La confianza viaja: un 61 % y un 95 % no se leen igual aunque digan COMPRA.
    e = [{"t": "T", "aggressor": "UNKNOWN"}]
    apply_aggressor(e, {"events": [{"t": "T", "matched": True, "trades": 20,
                                    "buy_premium": 950, "sell_premium": 50}]})
    assert e[0]["aggressor_confidence"] == pytest.approx(0.95)


def test_no_row_is_lost_between_the_provider_and_the_view_model():
    """Criterio de cierre: 608 en el proveedor son 608 en el modelo. Una etapa
    que recorte tiene que declarar el filtro."""
    from app.core import dark_pool_view as DPV
    flow = {"ready": True, "schema_state": "CONTRACT_OK", "rows": [
        {"t": f"T{i}", "dark_volume": 100.0 + i, "dark_notional": 1000.0 + i,
         "dark_prints": 5, "stock_price": 516.0} for i in range(608)]}
    levels = {"ready": True, "rows": [
        {"price": 500.0 + i * 0.1, "notional": 1e6 - i, "shares": 1000, "prints": 10}
        for i in range(349)]}
    vm = DPV.build(flow_block=flow, levels_block=levels, prints_block=None, spot=516.0)
    assert vm["flow"]["row_count"] == 608
    assert vm["levels_block"]["row_count"] == 349
    lin = vm["lineage"]
    assert lin["dark_flow"] == {"provider": 608, "view_model": 608, "dropped": 0}
    assert lin["dark_pool_levels"]["provider"] == 349
    assert lin["dark_pool_levels"]["dropped"] == 0
    # Y `equity_prints` en MARKET_CLOSED no puede vaciar a los otros dos.
    assert vm["status"]["dark_flow"]["state"] == "DATA_OK"
    assert vm["status"]["dark_pool_levels"]["state"] == "DATA_OK"
    assert vm["ready"] is True


def test_the_diagnosis_names_the_provider_lane_not_the_derived_one():
    """Un diagnóstico que no mira la misma fuente que la vista no diagnostica."""
    api = _read("app/terminal_api.py")
    # Se mira la LISTA de filas del diagnóstico, no los comentarios que explican
    # de dónde se viene.
    checks = api[api.index("    checks = ["):api.index("    blocked = state.get(")]
    assert "EQUITY_TAPE" not in checks
    assert "ITM_QUANT_LIQUIDITY_ZONES" not in checks
    assert "_dark_pool_rows(row, intel)" in checks
    assert "def _dark_pool_rows(" in api
    body = api[api.index("def _dark_pool_rows("):api.index("def build_diagnostics(")]
    for tool in ("QUANTDATA_DARK_FLOW", "QUANTDATA_DARK_POOL_LEVELS",
                 "QUANTDATA_EQUITY_PRINTS"):
        assert tool in body, tool
    main = _read("app/main.py")
    assert "intel=QUANTDATA_INTELLIGENCE.snapshot()" in main


def test_a_level_outside_the_window_is_anchored_not_dropped():
    """QQQ en 722 publicaba PUT WALL 700 en el KPI y no lo dibujaba: parecía no
    existir. Estirar la ventana aplastaría las velas, así que se ancla al borde
    con su distancia."""
    js = _read("app/static/itmq_trace.js")
    body = js[js.index("function drawLevels("):]
    body = body[:body.index("\n  function ")]
    assert "const off = above ? -1 : below ? 1 : 0;" in body
    assert "'▲ '" in body and "'▼ '" in body
    # Y el nivel de dentro conserva prioridad de etiqueta sobre el de fuera.
    assert "(a.off ? 1 : 0) - (b.off ? 1 : 0)" in body


def test_the_heat_field_is_normalised_over_what_is_on_screen():
    """El Interval Map cubre todo el libro; la ventana de TRACE son unos dólares.
    Normalizar sobre la matriz entera calculaba el percentil contra strikes que
    ni se ven, y por eso salía una masa maciza en QQQ, SPY y los demás."""
    js = _read("app/static/itmq_trace.js")
    body = js[js.index("function buildHeatBitmap("):js.index("function hexRGB(")]
    assert "HEAT_MARGIN" in body, "margen para que el campo no se corte en seco"
    assert "S.priceLo.get()" in body and "S.priceHi.get()" in body
    assert "keep.length >= 4" in body, "con muy pocas filas no se recorta"
    # El recorte respeta las dos orientaciones de la matriz.
    assert "const rowsAre = m.length === strikes.length;" in body
    assert "m.map(col => keep.map(i => (col || [])[i]))" in body
