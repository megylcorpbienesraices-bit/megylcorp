from __future__ import annotations

"""v1.42.7 · Net Drift OFICIAL, Dark Pool directo y Quant Data como proveedor.

Tres fronteras que estos tests fijan y que no deben volver a borrarse:

1. **Net Drift sólo puede venir de `POST /v1/options/tool/net-drift`.** No de GEX, ni
   de DEX, ni de Net Flow, ni de QFLOW, ni de una fórmula propia. El acumulado que
   publica ITM QUANT tiene que ser matemáticamente idéntico a sumar la respuesta
   cruda; si no lo es, la curva es una invención con aspecto de dato.

2. **La ausencia de datos es SIN DATOS, nunca una curva de ceros.** Una recta en cero
   y una sesión realmente equilibrada se dibujan igual, y esa ambigüedad es peor que
   un hueco declarado.

3. **Quant Data no crea secciones nuevas: alimenta las que ya existen.** Net Drift
   vive dentro de FLUJO DE ÓRDENES y Dark Pool toma al proveedor como fuente directa.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.net_drift import (build_net_drift, certify_against_raw, NO_DATA_LABEL,
                                INDEPENDENT_OF, DATA_OK, NO_PROVIDER_DATA,
                                FILTERED_ALL, PROVIDER_ERROR, PARSER_ERROR, STALE)
from app.providers.quantdata.tools import (build_catalog, norm_net_drift, norm_time_series,
                                           norm_dark_flow, norm_exposure_by_strike,
                                           norm_exposure_by_expiration)
from app.terminal_api import _net_drift, _dark_pool

ROOT = Path(__file__).resolve().parents[1]
BASE_MS = 1_758_205_800_000          # 2025-09-18T14:30:00Z
TICKERS = ("DIA", "SPY", "QQQ", "IWM", "AAPL")


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def raw_payload(n: int = 60, *, seed: int = 11) -> dict:
    """Respuesta cruda con la forma REAL del proveedor: mapa {época_ms: fila}."""
    import random
    rnd = random.Random(seed)
    data = {}
    for i in range(n):
        data[str(BASE_MS + i * 60_000)] = {
            "netCallPremium": round(rnd.uniform(-3_000_000, 5_000_000), 2),
            "netPutPremium": round(rnd.uniform(-4_000_000, 2_000_000), 2),
            "netCallVolume": round(rnd.uniform(-900, 1500), 1),
            "netPutVolume": round(rnd.uniform(-1200, 700), 1),
            "midMarketCallPremium": round(rnd.uniform(-3_000_000, 5_000_000), 2),
            "midMarketPutPremium": round(rnd.uniform(-4_000_000, 2_000_000), 2),
            "stockPrice": round(460 + rnd.uniform(-3, 3), 2),
        }
    return {"data": data}


def built(n: int = 60, *, symbol: str = "SPY", now_offset_min: float = 0.0, seed: int = 11):
    payload = raw_payload(n, seed=seed)
    rows = norm_net_drift(payload)["rows"]
    now = datetime.fromtimestamp((BASE_MS + (n - 1) * 60_000) / 1000.0, tz=timezone.utc) \
        + timedelta(minutes=now_offset_min)
    return payload, rows, build_net_drift(rows, symbol=symbol, now=now)


# ═════════════════════════════ 1 · el normalizador no pierde campos oficiales

def test_the_normalizer_keeps_every_field_the_endpoint_publishes():
    """Los siete campos del endpoint llegan enteros.

    `norm_time_series` colapsa la fila a `value`/`call`/`put` y tira los dos
    volúmenes netos y las dos primas a precio medio. Sin volúmenes no hay subgráfico
    de volumen neto; sin primas a precio medio no hay con qué contrastar lo pagado
    contra el punto medio del mercado.
    """
    row = norm_net_drift(raw_payload(1))["rows"][0]
    for field in ("net_call_premium", "net_put_premium", "net_call_volume",
                  "net_put_volume", "mid_call_premium", "mid_put_premium", "stock_price"):
        assert row[field] is not None, field
    assert row["t"].startswith("2025-09-18T14:30:00")
    assert row["timestamp_ms"] == BASE_MS


def test_the_reader_understands_the_map_shape_the_provider_actually_returns():
    """Quant Data devuelve un MAPA {época: fila}, no una lista.

    `_rows` sólo recorría listas, así que devolvía `[]` ante toda respuesta válida.
    Una lista vacía es indistinguible de "el proveedor no tiene datos": por eso Net
    Drift y Net Flow —y con él QFLOW— publicaban SIN DATOS con la respuesta completa
    delante. El fallo no estaba en el proveedor ni en la red, estaba en el lector.
    """
    assert norm_net_drift(raw_payload(3))["count"] == 3
    flow = norm_time_series({"data": {str(BASE_MS): {
        "callSum": 5.0, "putSum": 2.0, "stockPrice": 461.0}}}, ("netPremium",))
    assert flow["count"] == 1 and flow["rows"][0]["call"] == 5.0
    # La época en milisegundos se convierte a ISO: dejarla cruda hacía que
    # `datetime.fromisoformat` descartara la fila en silencio aguas abajo.
    assert flow["rows"][0]["t"].startswith("2025-09-18T14:30:00")


def test_the_list_shape_still_works_exactly_as_before():
    """El camino de lista es el primero y no cambia de comportamiento."""
    out = norm_net_drift({"buckets": [
        {"timestamp": "2026-09-18T14:30:00Z", "netCallPremium": 10.0, "netPutPremium": -4.0},
    ]})
    assert out["count"] == 1 and out["rows"][0]["net_premium"] == 6.0


# ═════════════════════════════ 2 · certificación contra la respuesta cruda

def test_the_published_curve_is_mathematically_identical_to_summing_the_raw_response():
    """La comprobación que exige la certificación.

    No basta con que la curva parezca razonable: el acumulado de cada punto tiene que
    ser exactamente la suma de los buckets crudos hasta ese instante.
    """
    payload, rows, out = built(90)
    report = certify_against_raw(rows, out)
    assert report["ok"], report
    assert report["points"] == 90
    # Y el desvío que queda es el cuanto de redondeo de la publicación, no cálculo.
    assert report["worst_absolute_deviation"] <= report["rounding_atol"]


def test_several_points_match_a_hand_computed_cumulative():
    """Varios puntos comparados a mano contra el crudo, no sólo el último."""
    payload, rows, out = built(40)
    data = payload["data"]
    keys = sorted(data, key=int)
    for idx in (0, 1, 7, 19, 39):
        expected_call = sum(data[k]["netCallPremium"] for k in keys[:idx + 1])
        expected_put = sum(data[k]["netPutPremium"] for k in keys[:idx + 1])
        point = out["series"][idx]
        assert point["cum_call"] == pytest.approx(expected_call, abs=1e-4)
        assert point["cum_put"] == pytest.approx(expected_put, abs=1e-4)
        assert point["cum_net"] == pytest.approx(expected_call + expected_put, abs=1e-4)


def test_the_put_sign_is_the_providers_and_is_not_flipped():
    """`netPutPremium` llega YA firmado. El neto es call + put, no call − put.

    Restar un número que ya es negativo invertiría la dirección de la sesión entera:
    una sesión que el proveedor describe como vendedora se dibujaría compradora.
    """
    rows = norm_net_drift({"data": {str(BASE_MS): {
        "netCallPremium": 1_000_000.0, "netPutPremium": -400_000.0}}})["rows"]
    out = build_net_drift(rows, symbol="QQQ",
                          now=datetime.fromtimestamp(BASE_MS / 1000.0, tz=timezone.utc))
    assert out["series"][0]["cum_net"] == pytest.approx(600_000.0)
    assert out["cum_put_premium"] == pytest.approx(-400_000.0)


def test_buckets_are_ordered_by_time_whatever_order_they_arrive_in():
    """Un mapa no garantiza orden. Acumular desordenado da una curva falsa."""
    payload = raw_payload(12)
    shuffled = {k: payload["data"][k] for k in reversed(list(payload["data"]))}
    rows = norm_net_drift({"data": shuffled})["rows"]
    out = build_net_drift(rows, symbol="IWM",
                          now=datetime.fromtimestamp((BASE_MS + 11 * 60_000) / 1000.0, tz=timezone.utc))
    stamps = [p["timestamp_ms"] for p in out["series"]]
    assert stamps == sorted(stamps)
    assert certify_against_raw(rows, out)["ok"]


# ═════════════════════════════ 3 · el bucket todavía abierto

def test_the_open_bucket_is_marked_and_the_closed_total_excludes_it():
    """El último bucket sigue formándose y el proveedor lo republica creciendo.

    Marcarlo permite distinguir lo consolidado de lo que aún puede cambiar; el
    acumulado cerrado da el número que ya no se moverá.
    """
    payload, rows, out = built(30, now_offset_min=0.0)
    assert out["open_bucket"] is True
    assert out["series"][-1]["open"] is True
    assert all(not p["open"] for p in out["series"][:-1])
    assert out["closed"]["cum_net"] == pytest.approx(out["series"][-2]["cum_net"])
    assert out["closed"]["cum_net"] != pytest.approx(out["cum_net_premium"])


def test_a_session_whose_last_bucket_already_closed_declares_it():
    payload, rows, out = built(30, now_offset_min=5.0)
    assert out["open_bucket"] is False
    assert out["series"][-1]["open"] is False
    assert out["closed"]["cum_net"] == pytest.approx(out["cum_net_premium"])


def test_a_repeated_timestamp_replaces_instead_of_adding():
    """El bucket abierto reenviado NO se suma dos veces.

    Ésta es la forma concreta en que una curva acumulada se corrompe: si cada
    refresco sumara el bucket abierto sobre lo ya acumulado, la sesión terminaría
    con un múltiplo del valor real.
    """
    rows = norm_net_drift({"buckets": [
        {"timestamp": str(BASE_MS), "netCallPremium": 100.0, "netPutPremium": 0.0},
        {"timestamp": str(BASE_MS), "netCallPremium": 250.0, "netPutPremium": 0.0},
    ]})["rows"]
    out = build_net_drift(rows, symbol="DIA",
                          now=datetime.fromtimestamp(BASE_MS / 1000.0, tz=timezone.utc) + timedelta(minutes=9))
    assert out["buckets"] == 1
    assert out["cum_call_premium"] == pytest.approx(250.0), "gana el último, no la suma"


# ═════════════════════════════ 4 · sin datos es SIN DATOS

@pytest.mark.parametrize("rows, expected", [
    (None, NO_PROVIDER_DATA),
    ([], NO_PROVIDER_DATA),
    ({"malformado": True}, PARSER_ERROR),
    ([{"sin": "instante"}], FILTERED_ALL),
])
def test_every_absence_declares_its_own_reason(rows, expected):
    out = build_net_drift(rows, symbol="AAPL")
    assert out["state"] == expected
    assert out["ready"] is False
    assert out["label"] == NO_DATA_LABEL
    assert out["series"] == []
    # Lo que NO puede pasar: publicar ceros que se lean como una sesión plana.
    for key in ("cum_call_premium", "cum_put_premium", "cum_net_premium"):
        assert out[key] is None, f"{key} debe ir vacío, no a cero"


def test_a_provider_failure_is_not_a_flat_session():
    out = build_net_drift(None, symbol="SPY", provider_error="HTTP 503 upstream")
    assert out["state"] == PROVIDER_ERROR
    assert "503" in out["detail"]
    assert out["cum_net_premium"] is None


def test_a_stale_curve_says_so_instead_of_passing_as_live():
    payload, rows, out = built(20, now_offset_min=45.0)
    assert out["state"] == STALE
    assert out["ready"] is True, "hay datos: están viejos, no ausentes"
    assert "45 min" in out["detail"]


def test_a_fresh_curve_is_data_ok():
    _, _, out = built(20, now_offset_min=2.0)
    assert out["state"] == DATA_OK


# ═════════════════════════════ 5 · universalidad multi-activo

def test_the_same_response_gives_the_same_curve_for_every_ticker():
    """Ningún activo recibe trato especial. El ticker es trazabilidad, no un parámetro.

    Si un símbolo tomara otra rama, DIA funcionaría y el resto no —que es exactamente
    el defecto que este release viene a cerrar—.
    """
    payload = raw_payload(25)
    rows = norm_net_drift(payload)["rows"]
    now = datetime.fromtimestamp((BASE_MS + 24 * 60_000) / 1000.0, tz=timezone.utc)
    curves = {t: build_net_drift(rows, symbol=t, now=now) for t in TICKERS}
    ref = curves["DIA"]
    for t, out in curves.items():
        assert out["symbol"] == t
        assert out["state"] == ref["state"]
        assert out["cum_net_premium"] == ref["cum_net_premium"]
        assert [p["cum_call"] for p in out["series"]] == [p["cum_call"] for p in ref["series"]]


def test_no_ticker_is_written_into_the_net_drift_path():
    """Ni un solo símbolo fijado en código en el camino de Net Drift."""
    src = text("app/core/net_drift.py")
    # Se ignoran las palabras del texto explicativo buscando sólo literales de cadena.
    literals = re.findall(r'"([A-Z]{2,5})"', src) + re.findall(r"'([A-Z]{2,5})'", src)
    forbidden = {"DIA", "SPY", "QQQ", "IWM", "AAPL", "TSLA", "NVDA"}
    assert not (set(literals) & forbidden), sorted(set(literals) & forbidden)


def test_the_bundle_takes_the_ticker_from_the_state():
    for t in TICKERS:
        out = _net_drift({"active_symbol": t}, {"net_drift": {"ready": True,
                         "rows": norm_net_drift(raw_payload(4))["rows"]}})
        assert out["symbol"] == t
        assert out["tool"] == "net_drift"


def test_the_bundle_reports_the_reason_when_the_provider_block_is_missing():
    assert _net_drift({"active_symbol": "SPY"}, {})["state"] == NO_PROVIDER_DATA
    err = _net_drift({"active_symbol": "SPY"},
                     {"net_drift": {"ready": False, "error": "HTTP 404"}})
    assert err["state"] == PROVIDER_ERROR and "404" in err["detail"]


# ═════════════════════════════ 6 · separación de otras magnitudes

def test_net_drift_declares_and_keeps_its_independence():
    """Net Drift no comparte serie, escala ni estado con las demás magnitudes."""
    _, _, out = built(10)
    assert out["source"] == "QUANTDATA_NET_DRIFT_OFFICIAL"
    assert out["endpoint"] == "POST /v1/options/tool/net-drift"
    assert out["reconstructed"] is False
    for magnitude in ("GEX", "DEX", "NET_FLOW", "QFLOW", "GAMMA_EXPOSURE", "DELTA_EXPOSURE"):
        assert magnitude in out["independent_of"]
    assert set(INDEPENDENT_OF) == set(out["independent_of"])


def test_the_net_drift_module_never_imports_any_other_exposure_magnitude():
    """La garantía estructural: no puede mezclarse con lo que no importa."""
    src = text("app/core/net_drift.py")
    body = "\n".join(l for l in src.splitlines() if l.startswith(("import ", "from ")))
    for forbidden in ("qflow", "engine", "trace_analytics", "precision_engine"):
        assert forbidden not in body, forbidden


def test_the_bundle_publishes_net_drift_and_qflow_as_separate_blocks():
    api = text("app/terminal_api.py")
    assert '"qflow": _qflow(state, intel),' in api
    assert '"net_drift": _net_drift(state, intel),' in api
    # Y Net Drift no se rellena con el bloque de Net Flow si falta.
    i = api.index("def _net_drift(")
    body = api[i:api.index("def _qflow(")]
    assert 'intel.get("net_drift")' in body
    assert "net_flow" not in body


# ═════════════════════════════ 7 · la interfaz, dentro de la sección existente

def test_net_drift_lives_inside_the_existing_order_flow_section():
    """No se crea una sección nueva: se modifica FLUJO DE ÓRDENES."""
    html = text("app/templates/terminal.html")
    views = re.findall(r'<section class="view[^"]*" data-view="([a-z_]+)"', html)
    assert "netdrift" not in views and "net_drift" not in views
    # El locator apunta a la SECCIÓN, no al botón del menú que lleva el mismo
    # atributo: buscar sólo `data-view="flujo"` encontraba la pestaña de navegación.
    i = html.index('<section class="view fixed" data-view="flujo">')
    block = html[i:html.index("</section>", i)]
    for el_id in ("ofDrift", "ofDriftVolume", "tblDriftTrades", "ofDriftPickLabel", "ofPanel"):
        assert f'id="{el_id}"' in block, el_id
    # La cinta sigue entera en la misma sección: se añade, no se sustituye.
    for el_id in ("ofPrice", "ofAggressor", "ofNet", "ofVolume", "ofTotal"):
        assert f'id="{el_id}"' in block, el_id


def test_the_renderer_draws_the_four_required_elements():
    js = text("app/static/itmq_orderflow.js")
    i = js.index("function drawDrift(ctx, env)")
    body = js[i:js.index("function drawDriftCursor")]
    assert "line('call'" in body, "línea CALL acumulada"
    assert "line('put'" in body, "línea PUT acumulada"
    assert "psy(v.price)" in body, "precio sobre el mismo eje temporal"
    vol = js[js.index("function drawDriftVolume"):]
    assert "p.cv" in vol and "p.pv" in vol, "subgráfico de volumen neto CALL/PUT"


def test_the_two_panels_share_one_time_axis():
    """Mismo `TimeLink` que la cinta: es el mismo reloj, no dos que coinciden."""
    js = text("app/static/itmq_orderflow.js")
    body = js[js.index("function drawDrift(ctx, env)"):js.index("function drawDriftCursor")]
    assert "window_()" in body, "la ventana temporal es la compartida"
    assert "S.link.setWindow" not in body, "Net Drift no impone su propia ventana"


def test_selecting_a_point_lists_the_trades_of_that_interval():
    js = text("app/static/itmq_orderflow.js")
    assert "function pickDriftAt(" in js
    # La resolución del intervalo vive en `driftTradesAt`, que es quien elige entre
    # la cinta del proveedor y la propia; `renderDriftPick` sólo la pinta.
    body = js[js.index("function driftTradesAt("):]
    assert "S.buckets.find(b => b.t === t0)" in body, "el bucket de la cinta del mismo minuto"
    assert "bucket.prints" in body, "los prints que produjeron el movimiento"


def test_the_interface_says_sin_datos_and_never_draws_a_zero_line():
    js = text("app/static/itmq_orderflow.js")
    assert "SIN DATOS" in js
    body = js[js.index("function drawDrift(ctx, env)"):js.index("function drawDriftCursor")]
    assert "if (!rows.length) { empty(ctx, env, driftEmptyMessage()); return false; }" in body
    html = text("app/templates/terminal.html")
    i = html.index('id="ofDriftKpis"')
    assert html[i:i + 900].count("SIN DATOS") >= 4, "los KPIs arrancan vacíos, no en cero"


def test_the_bundle_feeds_the_renderer_separately_from_qflow():
    app = text("app/static/itmq_app.js")
    assert "Flow.applyQflow(d.qflow)" in app
    assert "Flow.applyNetDrift(d.net_drift)" in app


# ═════════════════════════════ 8 · Dark Pool con Quant Data como fuente directa

def test_the_catalog_declares_the_dark_flow_tool_with_its_canonical_route():
    catalog = build_catalog()
    assert "dark_flow" in catalog
    tool = catalog["dark_flow"]
    assert tool.paths == ("/v1/equities/tool/dark-flow",)
    assert tool.page == "Dark Pool / Equities"


def test_dark_flow_normalizes_volume_and_share():
    out = norm_dark_flow({"data": {str(BASE_MS): {
        "darkVolume": 40_000.0, "litVolume": 160_000.0, "stockPrice": 461.0}}})
    row = out["rows"][0]
    assert row["dark_volume"] == 40_000.0
    assert row["total_volume"] == 200_000.0
    assert row["dark_share_pct"] == pytest.approx(20.0)


def test_the_provider_share_beats_the_venue_classification_when_both_exist():
    """La proporción del proveedor mide sobre TODO el volumen; la del venue, sólo
    sobre los large prints que la cinta dejó ver. No son la misma pregunta, así que
    la sección dice cuál está publicando."""
    intel = {"dark_flow": {"ready": True, "rows": norm_dark_flow({"data": {
        str(BASE_MS): {"darkVolume": 40_000.0, "litVolume": 160_000.0},
        str(BASE_MS + 60_000): {"darkVolume": 60_000.0, "litVolume": 140_000.0},
    }})["rows"]}}
    state = {"spot": 461.0, "large_prints": {
        "off_exchange_notional": 1_000_000.0, "total_notional": 4_000_000.0}}
    dp = _dark_pool(state, {"candles": []}, intel)
    assert dp["off_exchange_share_source"] == "QUANTDATA_DARK_FLOW"
    assert dp["off_exchange_share_pct"] == pytest.approx(25.0)
    # La vía por venue no desaparece: queda como auditoría, con su discrepancia.
    assert dp["audit"]["venue_classification_share_pct"] == pytest.approx(25.0)
    assert dp["audit"]["delta_pp"] == pytest.approx(0.0)
    assert dp["sources"]["primary"] == "QUANT_DATA"


def test_the_venue_classification_still_holds_the_view_alone():
    """Si el proveedor no responde, la vía propia sigue sosteniendo la sección."""
    state = {"spot": 100.0, "large_prints": {
        "off_exchange_notional": 2_000_000.0, "total_notional": 5_000_000.0,
        "off_exchange_top": [{"timestamp": "2026-09-18T14:31:00", "price": 100.2,
                              "size": 9000, "notional": 901_800.0, "side": "BUY"}]}}
    dp = _dark_pool(state, {"candles": []}, {})
    assert dp["ready"] is True
    assert dp["sources"]["prints"] == "ITM_QUANT_VENUE_CLASSIFICATION"
    assert dp["off_exchange_share_source"] == "ITM_QUANT_VENUE_CLASSIFICATION"
    assert dp["off_exchange_share_pct"] == pytest.approx(40.0)


def test_equity_prints_from_the_provider_take_precedence_over_inferred_ones():
    intel = {"equity_prints": {"ready": True, "rows": [
        {"t": "2026-09-18T14:31:00", "price": 100.2, "size": 9000.0,
         "notional": 901_800.0, "side": "BUY", "venue": "FINRA ADF", "off_exchange": True},
        {"t": "2026-09-18T14:32:00", "price": 100.3, "size": 500.0,
         "notional": 50_150.0, "side": "SELL", "venue": "NYSE", "off_exchange": False},
    ]}}
    dp = _dark_pool({"spot": 100.0, "large_prints": {}}, {"candles": []}, intel)
    assert dp["sources"]["prints"] == "QUANTDATA_EQUITY_PRINTS"
    assert len(dp["prints"]) == 1, "sólo las ejecuciones marcadas fuera de bolsa"
    assert dp["prints"][0]["source"] == "QUANTDATA_EQUITY_PRINTS"


def test_dark_pool_never_turns_an_absent_provider_into_a_zero():
    dp = _dark_pool({"spot": 100.0, "large_prints": {}}, {"candles": []}, {})
    assert dp["ready"] is False
    assert dp["reason"] == "QUANT_DATA_SIN_RESPUESTA"
    assert dp["off_exchange_share_pct"] is None
    assert dp["dark_volume"] is None


# ═════════════════════════════ 9 · Exposure alimenta EXPOSICIÓN

def test_the_exposure_page_finally_reads_the_shape_the_provider_returns():
    """`exposure-by-strike` devuelve `data[TICKER].exposureMap[vto][strike]`.

    `norm_by_strike` sólo entendía listas, así que la página Exposure no alimentaba a
    EXPOSICIÓN y su cobertura se leía como "sin datos" cuando era "sin leer".
    """
    raw = {"data": {"SPY": {"stockPrice": 601.2, "exposureMap": {
        "2026-09-18": {"600": {"callExposure": 1.2e6, "putExposure": -8.0e5},
                       "605": {"callExposure": 4.0e5, "putExposure": -1.0e5}},
        "2026-09-19": {"600": {"callExposure": 3.0e5, "putExposure": -2.0e5}}}}}}
    by_strike = norm_exposure_by_strike(raw, ("gamma",))
    assert by_strike["count"] == 2
    first = by_strike["rows"][0]
    assert first["strike"] == 600.0
    assert first["call"] == pytest.approx(1.5e6), "suma sobre todos los vencimientos"
    assert first["value"] == pytest.approx(5.0e5), "call + put, con el signo del proveedor"

    by_exp = norm_exposure_by_expiration(raw, ("gamma",))
    assert [r["expiration"] for r in by_exp["rows"]] == ["2026-09-18", "2026-09-19"]
    assert by_exp["rows"][0]["value"] == pytest.approx(7.0e5)


def test_the_exposure_tools_are_wired_to_the_new_reader():
    catalog = build_catalog()
    for key in ("gex_by_strike", "dex_by_strike", "vex_by_strike", "chex_by_strike"):
        raw = {"data": {"X": {"exposureMap": {"2026-09-18": {
            "600": {"callExposure": 10.0, "putExposure": -4.0}}}}}}
        assert catalog[key].normalize(raw)["count"] == 1, key
    # Y el camino de lista, que es el que usan las pruebas antiguas, sigue intacto.
    assert catalog["gex_by_strike"].normalize(
        {"data": [{"strike": 534.0, "gamma": 12.5}]})["rows"][0]["strike"] == 534.0


def test_quant_data_adds_no_sections_only_feeds_the_existing_ones():
    """El mapa página del proveedor → sección de ITM QUANT, sin secciones nuevas."""
    from app.providers.quantdata.tools import PAGES
    html = text("app/templates/terminal.html")
    views = set(re.findall(r'<section class="view[^"]*" data-view="([a-z_]+)"', html))
    expected = {"Dashboard": "resumen", "Flow Analysis": "flujo", "Exposure": "exposicion",
                "Dark Pool / Equities": "darkpool", "Statistics": "stats",
                "Open Interest": "oi", "Volatility Analysis": "vol"}
    for page, view in expected.items():
        assert page in PAGES, page
        assert view in views, f"{page} debe alimentar la sección existente {view}"
    # Ninguna página del proveedor tiene una sección propia con su nombre.
    for page in PAGES:
        assert page.lower().replace(" ", "") not in views


# ═════════════════════════════ 10 · distribución visual de todas las secciones

def test_every_kpi_grid_in_every_section_fills_its_rows():
    """Ninguna sección deja una fila de tarjetas a medias.

    Una rejilla con la última fila incompleta se lee como si faltara algo: el ojo
    busca la tarjeta que no está. La comprobación recorre las dieciséis secciones de
    la terminal, no sólo la que motivó el arreglo, porque el defecto es de reparto y
    puede reaparecer en cualquiera al añadir un KPI.
    """
    from html.parser import HTMLParser

    class Reader(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.section, self.grids = [], None, []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            cls = a.get("class", "").split()
            if tag == "section" and "view" in cls:
                self.section = a.get("data-view")
            node = {"tag": tag, "kpis": 0, "cols": None, "section": self.section}
            if tag == "div":
                if "grid" in cls:
                    for c in cls:
                        if c.startswith("c") and c[1:].isdigit():
                            node["cols"] = int(c[1:])
                if "kpi" in cls:
                    for parent in reversed(self.stack):
                        if parent["cols"]:
                            parent["kpis"] += 1
                            break
            self.stack.append(node)
            if tag in ("br", "hr", "input", "img", "meta", "link"):
                self.stack.pop()

        def handle_endtag(self, tag):
            while self.stack:
                node = self.stack.pop()
                if node["tag"] == tag:
                    if node["cols"] and node["kpis"]:
                        self.grids.append(node)
                    break

    reader = Reader()
    reader.feed(text("app/templates/terminal.html"))
    assert len(reader.grids) >= 14, "se esperaban rejillas de KPIs en casi todas las secciones"
    unbalanced = [(g["section"], g["cols"], g["kpis"]) for g in reader.grids
                  if g["kpis"] % g["cols"] != 0]
    assert not unbalanced, f"rejillas con la última fila a medias: {unbalanced}"


def test_every_declared_grid_width_degrades_on_narrow_screens():
    """Una rejilla sin punto de ruptura aplasta sus tarjetas en pantallas estrechas."""
    css = text("app/static/itmq_terminal.css")
    declared = set(re.findall(r"\.grid\.c(\d)\s*\{", css))
    narrow = css[css.index("@media (max-width: 860px)"):]
    for cols in sorted(declared):
        if int(cols) <= 1:
            continue
        assert f".grid.c{cols}" in narrow, f".grid.c{cols} no degrada en pantalla estrecha"


# ═════════════════════════════ 11 · el punto seleccionado usa el Order Flow del proveedor

def test_the_option_order_flow_keeps_the_whole_contract():
    """Una impresión sin strike, sin vencimiento y sin prima es una fila anónima.

    `norm_prints` es genérico —lo comparte con la cinta de equity— y se queda en
    precio, tamaño y venue. Con eso no se puede relacionar un movimiento de la curva
    con las operaciones que lo produjeron, que es justo lo que pide la selección de
    punto.
    """
    from app.providers.quantdata.tools import norm_option_order_flow
    row = norm_option_order_flow({"prints": [{
        "timestamp": str(BASE_MS), "ticker": "spy", "optionType": "CALL", "strike": 600,
        "expirationDate": "2026-09-18", "price": 2.35, "size": 400, "side": "ASK",
        "executionType": "SWEEP", "stockPrice": 601.2}]})["rows"][0]
    assert row["ticker"] == "SPY" and row["option_type"] == "CALL"
    assert row["strike"] == 600.0 and row["expiration"] == "2026-09-18"
    # La prima de un contrato es precio × tamaño × multiplicador. Sin el
    # multiplicador saldría cien veces por debajo.
    assert row["premium"] == pytest.approx(2.35 * 400 * 100)
    assert row["direction"] == 1, "agresor en ask"
    assert row["execution"] == "SWEEP"


def test_net_drift_carries_the_providers_order_flow_and_says_which_tape_it_is():
    """La cinta sin consolidar manda: es la impresión individual, que es la pregunta."""
    rows = [{"t": "2026-09-18T14:30:10", "strike": 600.0, "premium": 94_000.0,
             "option_type": "CALL", "side": "ASK", "direction": 1, "execution": "SWEEP"}]
    intel = {"net_drift": {"ready": True, "rows": norm_net_drift(raw_payload(3))["rows"]},
             "options_order_flow_raw": {"ready": True, "rows": rows},
             "options_order_flow": {"ready": True, "rows": []}}
    out = _net_drift({"active_symbol": "SPY"}, intel)
    flow = out["order_flow"]
    assert flow["ready"] is True
    assert flow["tool"] == "options_order_flow_raw"
    assert flow["consolidated"] is False
    assert flow["rows"][0]["strike"] == 600.0


def test_an_absent_provider_order_flow_names_its_fallback_instead_of_going_silent():
    out = _net_drift({"active_symbol": "IWM"},
                     {"net_drift": {"ready": True, "rows": norm_net_drift(raw_payload(2))["rows"]}})
    flow = out["order_flow"]
    assert flow["ready"] is False
    assert flow["rows"] == []
    assert flow["fallback"] == "cinta propia de opciones"


@pytest.mark.parametrize("intel", [
    {},
    {"net_drift": {"ready": False, "error": "HTTP 503"}},
    {"net_drift": {"ready": False}},
])
def test_the_order_flow_block_travels_even_when_there_is_no_curve(intel):
    """Que falte Net Drift no implica que falte la cinta.

    Publicar la clave sólo en el camino feliz obliga al consumidor a distinguir «no
    hay cinta» de «la clave no vino», que son cosas distintas.
    """
    out = _net_drift({"active_symbol": "SPY"}, intel)
    assert out["ready"] is False
    assert "order_flow" in out, "el bloque de cinta debe viajar en todos los caminos"


def test_the_renderer_prefers_the_providers_tape_and_names_the_source():
    js = text("app/static/itmq_orderflow.js")
    body = js[js.index("function driftTradesAt("):js.index("function renderDriftPick()")]
    assert "S.drift.order_flow" in body, "primero la cinta del proveedor"
    assert "S.buckets.find" in body, "la cinta propia queda como respaldo"
    assert "'QUANT DATA'" in body and "'CINTA PROPIA'" in body
    pick = js[js.index("function renderDriftPick()"):]
    assert "found.source" in pick, "la cabecera declara de qué cinta son los trades"


# ═════════════════════════════ 12 · las herramientas descargadas se consumen

def test_the_exposure_page_backs_the_section_when_the_engine_has_no_profile():
    """Las cuatro `*_by_strike` se pedían cada ciclo y nadie las consumía.

    Gastaban cuota y, si el motor propio no tenía perfil, la sección quedaba vacía
    teniendo el dato del proveedor ya descargado en memoria.
    """
    from app.terminal_api import _exposicion
    intel = {
        "gex_by_strike": {"ready": True, "rows": [{"strike": 600.0, "value": 1.2e6}]},
        "dex_by_strike": {"ready": True, "rows": [{"strike": 600.0, "value": 3.4e5}]},
    }
    out = _exposicion({"profiles": {"rows": []}}, {"spot": 601.0}, intel)
    assert out["ready"] is True
    assert out["by_strike_source"] == "QUANTDATA"
    row = out["by_strike"][0]
    assert row["strike"] == 600.0 and row["gex"] == 1.2e6 and row["dex"] == 3.4e5
    # Lo que el proveedor no publica va vacío, nunca a cero: un 0 en OI diría
    # "no hay interés abierto", que es una afirmación falsa sobre el mercado.
    assert row["oi"] is None and row["volume"] is None


def test_the_provider_profile_now_wins_over_the_engine():
    """v1.43.0 invierte la autoridad de EXPOSICIÓN.

    Hasta v1.42.7 el perfil del motor ganaba siempre y el del proveedor entraba
    sólo si el motor no tenía nada, SIN dejar rastro de cuál se estaba viendo.
    Quant Data es ahora la fuente primaria de GEX/DEX/VEX/CHEX; el cálculo propio
    no se borra —viaja en `audit` para contrastar las dos construcciones— pero ya
    no puede taparlo en silencio.
    """
    from app.terminal_api import _exposicion
    trace = {"profiles": {"rows": [{"strike": 600.0, "gamma_m": 2.0, "oi": 1234}]}}
    intel = {"gex_by_strike": {"ready": True, "rows": [{"strike": 999.0, "value": 9.9e9}]}}
    out = _exposicion(trace, {"spot": 601.0}, intel)
    assert out["by_strike_source"] == "QUANTDATA"
    assert out["source_mode"] == "DIRECT_PROVIDER"
    assert [r["strike"] for r in out["by_strike"]] == [999.0]
    # El cálculo propio sigue ahí, para auditoría, nunca promediado con el otro.
    assert out["audit"]["engine_rows"] == 1
    assert [r["strike"] for r in out["audit"]["engine_by_strike"]] == [600.0]


def test_the_engine_profile_is_a_declared_fallback_not_a_silent_one():
    """Sin proveedor, el motor sostiene la vista, pero va etiquetado FALLBACK."""
    from app.terminal_api import _exposicion
    trace = {"profiles": {"rows": [{"strike": 600.0, "gamma_m": 2.0, "oi": 1234}]}}
    out = _exposicion(trace, {"spot": 601.0}, {})
    assert out["by_strike_source"] == "ITM_QUANT"
    assert out["source_mode"] == "FALLBACK" and out["fallback_used"] is True
    assert [r["strike"] for r in out["by_strike"]] == [600.0]


def test_open_interest_falls_back_to_the_provider_by_strike():
    from app.terminal_api import _open_interest
    intel = {
        "oi_by_strike": {"ready": True, "rows": [
            {"strike": 600.0, "value": 12_000.0, "call": 8_000.0, "put": 4_000.0}]},
        "oi_change": {"ready": True, "rows": [{"strike": 600.0, "value": -900.0}]},
    }
    out = _open_interest({"profiles": {"rows": []}}, {"spot": 601.0}, intel)
    assert out["by_strike_source"] == "QUANTDATA"
    assert out["by_strike_source_mode"] == "DIRECT_PROVIDER"
    row = out["by_strike"][0]
    assert row["oi"] == 12_000.0 and row["call_oi"] == 8_000.0
    assert row["oi_change"] == -900.0
    assert row["volume"] is None, "open-interest-by-strike no publica volumen"
    # El OI jamás se reconstruye con volumen.
    assert out["reconstruction"] == "NEVER_FROM_VOLUME"


def test_statistics_uses_the_providers_contract_and_side_tables_when_there_is_no_tape():
    """Sin cinta, tres barras a cero se leen como «no se negoció nada»."""
    from app.terminal_api import _estadisticas
    intel = {
        "contract_statistics": {"ready": True, "rows": [
            {"label": "C 600", "premium": 4.2e6, "contracts": 1800.0, "trades": 42,
             "ask_side": 3.0e6, "bid_side": 1.2e6}]},
        "contract_trade_side_statistics": {"ready": True, "rows": [
            {"label": "total", "premium": 4.2e6, "ask_side": 3.0e6, "bid_side": 1.2e6}]},
    }
    out = _estadisticas({"option_prints": [], "profiles": {"rows": []}}, {}, intel)
    assert out["contract_rows"][0]["source"] == "QUANTDATA_CONTRACT_STATISTICS"
    assert out["contract_rows"][0]["bias"] == pytest.approx(42.9, abs=0.1)
    assert out["trade_side_source"] == "QUANTDATA_TRADE_SIDE"
    values = {r["label"]: r["value"] for r in out["trade_side"]}
    assert values["Comprador (ask)"] == 3.0e6 and values["Vendedor (bid)"] == 1.2e6


def test_every_catalogued_tool_has_a_consumer():
    """Una herramienta que se descarga y nadie lee es cuota quemada.

    Cada petición por ciclo cuenta contra un plan que puede ser de 240 peticiones a
    la hora. Si el catálogo la declara, alguna sección tiene que consumirla —o el
    catálogo no debería declararla—.
    """
    from app.providers.quantdata.tools import build_catalog
    consumers = "\n".join(text(f) for f in
                          ("app/terminal_api.py", "app/service.py", "app/main.py",
                           "app/core/quant_data_hub.py",
                           "app/providers/quantdata/shared.py"))
    missing = [k for k in build_catalog()
               if f'"{k}"' not in consumers and f"'{k}'" not in consumers]
    assert not missing, f"herramientas descargadas que nadie consume: {sorted(missing)}"


def test_the_dashboard_context_reaches_resumen_labelled_as_context():
    """Noticias y movers son CONTEXTO, nunca señal.

    No entran en el Scanner ni en la autoridad direccional; van a RESUMEN
    etiquetados como del proveedor para que no se confundan con evidencia propia.
    """
    from app.terminal_api import _resumen
    intel = {
        "news": {"ready": True, "rows": [
            {"title": "Titular", "t": "2026-09-18T14:00:00", "source": "X", "url": "u"}]},
        "gainers_losers": {"ready": True, "rows": [
            {"symbol": "SPY", "change_pct": 1.4, "price": 601.0, "volume": 9.0e6}]},
    }
    out = _resumen({"spot": 601.0}, {}, intel)
    assert out["news"][0]["title"] == "Titular"
    assert out["movers"][0]["symbol"] == "SPY"
    assert out["context_source"] == "QUANTDATA_DASHBOARD"
    # Sin proveedor no se inventa nada: listas vacías, no ceros ni marcadores.
    assert _resumen({"spot": 601.0}, {})["news"] == []


def test_open_interest_publishes_the_providers_history():
    from app.terminal_api import _open_interest
    intel = {"oi_over_time": {"ready": True, "rows": [
        {"t": "2026-09-17", "value": 1.1e6}, {"t": "2026-09-18", "value": 1.3e6}]}}
    out = _open_interest({"profiles": {"rows": []}}, {}, intel)
    assert out["oi_history_source"] == "QUANTDATA_OI_OVER_TIME"
    assert [r["value"] for r in out["oi_history"]] == [1.1e6, 1.3e6]


def test_dark_pool_borrows_the_providers_price_series_when_the_tape_has_no_candles():
    """El panel PRECIO / TIEMPO quedaba en blanco teniendo la serie descargada."""
    intel = {"stock_price_over_time": {"ready": True, "rows": [
        {"t": "2026-09-18T14:30:00", "value": 461.0},
        {"t": "2026-09-18T14:31:00", "value": 461.4}]}}
    dp = _dark_pool({"spot": 461.2, "large_prints": {}}, {"candles": []}, intel)
    assert dp["candle_source"] == "QUANTDATA_STOCK_PRICE_OVER_TIME"
    assert len(dp["candles"]) == 2
    # La serie del proveedor sólo trae cierre: apertura/máximo/mínimo van vacíos en
    # lugar de repetir el cierre, que dibujaría velas planas inexistentes.
    assert dp["candles"][0]["o"] is None and dp["candles"][0]["h"] is None


def test_the_local_tape_still_wins_over_the_providers_price_series():
    intel = {"stock_price_over_time": {"ready": True, "rows": [
        {"t": "2026-09-18T14:30:00", "value": 999.0}]}}
    trace = {"candles": [{"t": "2026-09-18T14:30:00", "c": 461.0, "v": 1000}],
             "candle_source": "SIP"}
    dp = _dark_pool({"spot": 461.0, "large_prints": {}}, trace, intel)
    assert dp["candle_source"] == "SIP"
    assert dp["candles"][0]["c"] == 461.0


# ═════════════════════════════ 13 · ninguna sección reconstruye Net Drift

def test_the_summary_card_stops_reconstructing_net_drift_from_the_local_tape():
    """La tarjeta de RESUMEN se llamaba NET DRIFT y dibujaba otra cosa.

    Acumulaba la cinta propia de opciones y la presentaba con el nombre de una
    magnitud que sólo publica el proveedor: en pantalla era indistinguible de la
    real. Arrastraba además un error de signo —`premium * (direction || 1)` cuenta
    como COMPRA toda la prima sin agresor clasificado, porque `0 || 1` vale 1—, así
    que la curva se inclinaba sola a comprador justo cuando la cinta llegaba sin
    cotización, que es cuando peor se lee.
    """
    app = text("app/static/itmq_app.js")
    i = app.index("// ── NET DRIFT · OFICIAL DE QUANT DATA")
    body = app[i:app.index("pill('pillFlow'", i)]
    assert "d.net_drift" in body, "la fuente es el bloque oficial del bundle"
    assert "x.cum_call" in body and "x.cum_put" in body and "x.cum_net" in body
    assert "option_prints" not in body, "no puede volver a reconstruirse con la cinta propia"
    assert "direction, 0) || 1" not in app, "el signo fabricado no puede reaparecer"
    assert "SIN DATOS" in body


def test_no_section_derives_net_drift_from_another_magnitude():
    """La garantía estructural del lado del servidor.

    Lo que importa no es que la palabra «GEX» no aparezca —la propia explicación la
    nombra para decir que no se usa—, sino que la función **no lea** ningún bloque
    del proveedor que no sea `net_drift`. Por eso se comprueban las lecturas reales,
    no el texto.
    """
    api = text("app/terminal_api.py")
    body = api[api.index("def _net_drift("):api.index("def _net_drift_order_flow(")]
    reads = set(re.findall(r'intel\.get\(\s*"([a-z_]+)"', body))
    reads |= set(re.findall(r'_qd_rows\(\s*intel\s*,\s*"([a-z_]+)"', body))
    assert reads == {"net_drift"}, f"Net Drift sólo puede leer su propio bloque, lee {sorted(reads)}"
    # Y ninguna función de otra magnitud se invoca desde aquí.
    assert "_qflow(" not in body and "_exposicion(" not in body


def test_the_curve_builder_reads_nothing_but_the_rows_it_is_given():
    """`build_net_drift` no consulta estado global ni otro proveedor."""
    src = text("app/core/net_drift.py")
    body = src[src.index("def build_net_drift("):src.index("def certify_against_raw(")]
    for forbidden in ("import ", "FEATURE_BUS", "STATE", "requests", "httpx"):
        assert forbidden not in body, forbidden
