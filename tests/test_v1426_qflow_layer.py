from __future__ import annotations

"""v1.42.6 · Capa QFLOW y universalidad multi-activo.

Quant Data publica `net-flow` (prima neta por intervalo con `callSum`, `putSum` y
`stockPrice`) y `order-flow`. **No publica un "QFLOW"**: ese nombre no está en su
documentación y no hay fórmula oficial para un nivel horizontal. El dato es del
proveedor; el nivel lo calcula ITM QUANT. Estos tests fijan esa frontera.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.qflow import (build_qflow, DATA_OK, NO_PROVIDER_DATA, FILTERED_ALL,
                            PROVIDER_ERROR, PARSER_ERROR, STALE)
from app.providers.quantdata.tools import norm_time_series
from app.providers.quantdata.runtime import QuantDataRuntime
from app.terminal_api import _qflow

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)


def _rows(n=90, price_from=518.0, price_to=515.8):
    out = []
    for i in range(n):
        px = price_from + (price_to - price_from) * (i / max(1, n - 1))
        call, put = (900_000 if i % 7 == 0 else 40_000), (700_000 if i % 11 == 0 else 30_000)
        out.append({"t": (BASE + timedelta(minutes=i)).isoformat(), "value": call - put,
                    "call": call, "put": put, "stock_price": px})
    return out


# ───────────────────────────── normalizador · campos reales del proveedor

def test_the_normalizer_reads_the_fields_quant_data_actually_publishes():
    """El normalizador buscaba `netCallPremium`/`netPutPremium` y Quant Data publica
    `callSum`/`putSum`; además descartaba `stockPrice` entero, que es justo el precio
    de referencia de cada intervalo y sin él no hay nivel QFLOW posible.
    """
    out = norm_time_series({"buckets": [
        {"timestamp": "2026-09-18T14:30:00Z", "callSum": 180_000, "putSum": 95_000, "stockPrice": 515.4},
    ]}, ("netPremium", "net", "value", "premium"))
    assert out["ready"] is True
    row = out["rows"][0]
    assert row["call"] == 180_000.0 and row["put"] == 95_000.0
    assert row["stock_price"] == 515.4
    # sin campo neto explícito, el neto ES call - put; devolver 0.0 convertía una
    # respuesta válida en una serie plana
    assert row["value"] == 85_000.0


def test_an_explicit_net_field_still_wins_over_the_derived_one():
    out = norm_time_series({"buckets": [
        {"timestamp": "2026-09-18T14:30:00Z", "netPremium": 1234.0,
         "callSum": 180_000, "putSum": 95_000, "stockPrice": 515.4},
    ]}, ("netPremium", "net", "value", "premium"))
    assert out["rows"][0]["value"] == 1234.0


# ───────────────────────────── serie y nivel

def test_qflow_series_is_cumulative_not_a_copy_of_the_bars():
    """Las barras responden «¿qué pasó en este minuto?»; la serie QFLOW responde
    «¿hacia dónde se ha inclinado la sesión?». Si fueran lo mismo, la línea sobraría.
    """
    q = build_qflow(_rows(), symbol="DIA", now=BASE + timedelta(minutes=91))
    assert q["ready"] is True and q["state"] == DATA_OK
    cum = [p["cumulative"] for p in q["series"]]
    nets = [p["net"] for p in q["series"]]
    assert cum != nets
    assert cum[-1] == pytest.approx(sum(nets), rel=1e-9)
    assert q["net_premium"] == pytest.approx(cum[-1], rel=1e-9)


def test_the_level_is_a_price_weighted_by_traded_premium():
    """El nivel responde «¿a qué precio se negoció el grueso del dinero?», así que
    pondera por |prima|, no por prima neta: un intervalo con 2M en calls y 2M en
    puts tiene neto cero pero es un precio donde hubo enorme actividad."""
    q = build_qflow(_rows(), symbol="DIA", now=BASE + timedelta(minutes=91))
    lvl = q["level"]
    assert lvl is not None
    assert lvl["price_low"] <= lvl["price"] <= lvl["price_high"]
    assert 0.0 <= lvl["concentration"] <= 1.0
    assert lvl["samples"] > 0


def test_recent_flow_pulls_the_level_more_than_old_flow():
    """Sin decaimiento el nivel queda clavado en la primera hora aunque el dinero se
    haya mudado de precio."""
    rows = []
    for i in range(120):
        px = 520.0 if i < 60 else 510.0          # el dinero se muda a mitad de sesión
        rows.append({"t": (BASE + timedelta(minutes=i)).isoformat(), "value": 0,
                     "call": 500_000, "put": 500_000, "stock_price": px})
    q = build_qflow(rows, symbol="SPY", now=BASE + timedelta(minutes=121))
    # con pesos iguales sería 515.0; el decaimiento debe acercarlo al precio reciente
    assert q["level"]["price"] < 514.0


def test_a_level_needs_prices_and_says_so_when_there_are_none():
    """Sin `stockPrice` no hay dónde situar el flujo: se publica serie sin nivel,
    no un nivel inventado."""
    rows = [{"t": (BASE + timedelta(minutes=i)).isoformat(), "value": 1000,
             "call": 5000, "put": 4000, "stock_price": None} for i in range(30)]
    q = build_qflow(rows, symbol="QQQ", now=BASE + timedelta(minutes=31))
    assert q["ready"] is True and q["level"] is None


def test_concentration_events_are_exceptional_not_routine():
    q = build_qflow(_rows(), symbol="DIA", now=BASE + timedelta(minutes=91))
    assert q["events"], "debería detectar las ráfagas sembradas"
    assert len(q["events"]) < len(q["series"]) / 3, "un evento en cada barra no es un evento"
    for ev in q["events"]:
        assert ev["side"] in {"CALL", "PUT", "MIXED"}
        assert ev["premium"] > 0


# ───────────────────────────── estados: nunca un 0.0 silencioso

@pytest.mark.parametrize("rows,err,expected", [
    (None, None, NO_PROVIDER_DATA),
    ([], None, NO_PROVIDER_DATA),
    ({"a": 1}, None, PARSER_ERROR),
    ([{"t": None, "value": None}], None, FILTERED_ALL),
    (None, "HTTP 503", PROVIDER_ERROR),
])
def test_every_failure_mode_is_named_instead_of_zeroed(rows, err, expected):
    q = build_qflow(rows, symbol="IWM", provider_error=err)
    assert q["state"] == expected
    assert q["ready"] is False
    assert q["net_premium"] is None, "un fallo de datos no puede publicarse como 0.0"
    assert q["detail"], "cada estado debe explicar por qué"


def test_old_data_is_marked_stale_instead_of_passing_as_live():
    q = build_qflow(_rows(), symbol="DIA", now=BASE + timedelta(minutes=400))
    assert q["state"] == STALE and q["ready"] is True


# ───────────────────────────── multi-activo · sin excepción para DIA

@pytest.mark.parametrize("symbol", ["DIA", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "MSFT", "GLD"])
def test_every_supported_asset_queries_its_own_ticker_as_direct_evidence(symbol):
    """La lista blanca `{DIA, XLI, XLF}` más un catálogo de ETFs dejaba a TODA acción
    cayendo a `direct=False`, pese a consultar su PROPIO ticker — y `direct=False`
    está documentado para el caso contrario (pedir DIA como contexto de un futuro).
    La evidencia es directa cuando se consulta el ticker del propio instrumento.
    """
    t = QuantDataRuntime._target(symbol)
    assert t.query_ticker == symbol
    assert t.direct is True


@pytest.mark.parametrize("symbol", ["YM", "MYM", "DJX", "VIX", "VXD"])
def test_dow_futures_and_indices_stay_declared_as_context_not_proxy(symbol):
    """La única razón legítima para consultar otro ticker, y queda declarada."""
    t = QuantDataRuntime._target(symbol)
    assert t.query_ticker == "DIA" and t.direct is False


@pytest.mark.parametrize("symbol", ["DIA", "SPY", "QQQ", "IWM", "AAPL"])
def test_qflow_reaches_every_asset_with_the_same_code_path(symbol):
    """El ticker sale del ESTADO, nunca de una constante."""
    out = _qflow({"active_symbol": symbol},
                 {"net_flow": {"ready": True, "rows": _rows(), "route": "/v1/options/tool/net-flow"}})
    assert out["symbol"] == symbol
    assert out["buckets"] == 90
    assert out["level"] is not None


def test_the_backend_never_hardcodes_a_ticker_for_qflow():
    src = (ROOT / "app/terminal_api.py").read_text(encoding="utf-8")
    block = src[src.index("def _qflow("):src.index("def build_diagnostics(")]
    assert '"DIA"' not in block and "'DIA'" not in block


# ───────────────────────────── frontend · capa separada, no sustitución

def test_the_qflow_line_coexists_with_the_existing_bars():
    """La capa se AÑADE: las barras verdes/rojas de flujo neto no se sustituyen."""
    js = (ROOT / "app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    i = js.index("function drawNetFlow(")
    block = js[i:js.index("/* ---", i)]
    assert "QFLOW" in block, "la línea debe identificarse visualmente"
    assert "cumulative" in block, "la línea es la serie acumulada"
    assert "b.net" in block, "las barras del intervalo siguen dibujándose"


def test_the_qflow_price_level_is_drawn_like_the_other_levels():
    js = (ROOT / "app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    i = js.index("function drawPrice(")
    block = js[i:js.index("function drawAggressor(")]   # drawPrice tiene funciones internas
    assert "qflowLevel" in block and "'QFLOW'" in block
    assert "Q.levelLine" in block, "mismo trazo que Delta Center / Gamma Center / Zero Gamma"


def test_concentration_events_are_marked_on_price_and_mirrored_in_total():
    js = (ROOT / "app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    price = js[js.index("function drawPrice("):js.index("function drawAggressor(")]
    assert "qEvents" in price, "el evento debe marcarse sobre el precio"
    total = js[js.index("function drawTotal("):js.index("/* ------", js.index("function drawTotal("))]
    assert "S.qflow" in total, "el mismo evento debe reflejarse en TOTAL"


def test_the_qflow_layer_is_wired_from_the_bundle():
    assert "applyQflow" in (ROOT / "app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    assert "Flow.applyQflow(d.qflow)" in (ROOT / "app/static/itmq_app.js").read_text(encoding="utf-8")
