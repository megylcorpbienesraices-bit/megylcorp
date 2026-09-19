"""CERTIFICACIÓN v1.46.0 · DARK POOL POR CARRILES + CORRECCIÓN GLOBAL MULTI-ACTIVO

Qué se certifica aquí, y por qué cada cosa estaba mal antes:

  1. El cuerpo de `dark-pool-levels` no hereda campos de otras herramientas.
     Un cuerpo con campos de más es tan inválido como uno con campos de menos.
  2. Un 400 se lee ENTERO —`errors[].field`, `errors[].message`— y el campo
     rechazado se conserva. Truncar el mensaje borraba la parte accionable.
  3. Un 400 no entra en el ciclo de reintentos. 400 ≠ 422 ≠ 5xx: tres causas,
     tres tratamientos. Confundirlos dejó `dark-pool-levels` reintentando
     indefinidamente un cuerpo que nunca iba a ser aceptado.
  4. El normalizador CONSERVA los campos oficiales, incluidos los que todavía no
     tiene nombrados y el precio de referencia que viaja a nivel de respuesta.
  5. Los tres carriles son independientes: que uno rechace el cuerpo no puede
     tumbar a los otros dos.
  6. El tri-estado fuera de bolsa se mantiene: ausente ≠ False.
  7. Ocho estados internos con causa y remedio; la pantalla del analista sigue
     diciendo SIN DATOS y el Auditor conserva cuál de los ocho fue.
  8. `verify_live_quantdata.py` mide los tres carriles sobre una cesta
     multi-activo, no sólo sobre DIA.
  9. Multi-activo: sin ticker, umbral, venue ni distancia escritos a mano.
 10. Ningún cero fabricado donde hubo un fallo técnico.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ─────────────────────────────────────────────── 1 · CUERPO SIN HERENCIAS

def test_dark_pool_levels_sends_only_its_own_contract():
    """El cuerpo mínimo, sin un solo campo heredado de otra herramienta."""
    from app.providers.quantdata.tools import build_catalog, INHERITED_FIELD_BLOCKLIST

    tool = build_catalog()["dark_pool_levels"]
    body = tool.request_body("SPY")
    assert body == {"filter": {"ticker": "SPY"}}, body
    for forbidden in INHERITED_FIELD_BLOCKLIST:
        assert forbidden not in body
    # Los campos que el usuario señaló uno a uno.
    for forbidden in ("sessionDate", "timeRange", "snapshotTime", "aggregationPeriod",
                      "filterExpression", "pagination", "projection"):
        assert forbidden not in body, f"{forbidden} no pertenece a dark-pool-levels"


def test_inherited_fields_are_stripped_even_if_someone_adds_them_later():
    """La defensa no es la disciplina del que edita el catálogo: es el código."""
    from app.providers.quantdata.tools import strip_inherited_fields

    dirty = {"filter": {"ticker": "QQQ"}, "sessionDate": "2026-09-18",
             "pagination": {"limit": 50}, "projection": ["price"]}
    clean, removed = strip_inherited_fields(dirty)
    assert clean == {"filter": {"ticker": "QQQ"}}
    assert sorted(removed) == ["pagination", "projection", "sessionDate"]


# ─────────────────────────────────────────────── 2 · EL 400, ENTERO

class _Resp:
    """Una respuesta HTTP con el cuerpo que el proveedor devuelve en un 400."""

    def __init__(self, body, status=400):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_a_validation_error_keeps_every_named_field():
    """`errors[].field` y `errors[].message`, no la primera línea del mensaje."""
    from app.providers.quantdata.client import _describe_error

    headline, fields = _describe_error(_Resp({
        "detail": "Request validation failed", "errors": [
            {"field": "lookBackPeriod", "message": "field required"},
            {"field": "filter.ticker", "message": "must be uppercase"},
        ]}))
    assert "lookBackPeriod" in headline and "filter.ticker" in headline
    assert any("lookBackPeriod" in f and "field required" in f for f in fields)
    assert any("filter.ticker" in f for f in fields)


def test_pydantic_shaped_errors_are_read_too():
    """El proveedor puede usar `loc`/`msg`; el diagnóstico no depende del dialecto."""
    from app.providers.quantdata.client import _describe_error

    headline, fields = _describe_error(_Resp(
        {"detail": [{"loc": ["body", "aggregationPeriod"], "msg": "field required"}]}))
    assert "aggregationPeriod" in headline
    assert fields and "aggregationPeriod" in fields[0]


# ─────────────────────────────────────────────── 3 · 400 ≠ 422 ≠ 5xx

def test_five_failure_classes_five_treatments():
    from app.providers.quantdata.client import QuantDataError
    from app.providers.quantdata.tools import (
        classify_provider_failure, STATUS_REQUEST_INVALID, STATUS_NO_DATA,
        STATUS_MISSING_TOOL, STATUS_PROVIDER_ERROR, STATUS_TRANSIENT)

    def err(code):
        e = QuantDataError(f"HTTP {code}"); e.status_code = code
        return e

    assert classify_provider_failure(err(400)) == STATUS_REQUEST_INVALID
    assert classify_provider_failure(err(422)) == STATUS_NO_DATA
    assert classify_provider_failure(err(404)) == STATUS_MISSING_TOOL
    assert classify_provider_failure(err(500)) == STATUS_PROVIDER_ERROR
    assert classify_provider_failure(err(503)) == STATUS_PROVIDER_ERROR
    assert classify_provider_failure(err(429)) == STATUS_TRANSIENT
    assert classify_provider_failure(asyncio.TimeoutError()) == STATUS_TRANSIENT


def test_an_unrepairable_400_stops_instead_of_looping():
    """Un 400 que no sabemos corregir se DICE. No se reintenta indefinidamente."""
    from app.providers.quantdata.intelligence import QuantDataIntelligence
    from app.providers.quantdata.client import QuantDataError
    from app.core.data_hub_runtime import HUB_RUNTIME

    calls = []

    class Rejecting:
        async def post(self, path, body):
            calls.append(dict(body))
            e = QuantDataError("Quant Data HTTP 400: Request validation failed "
                               "· campoDesconocidoQueNoSabemosCorregir: required")
            e.status_code = 400
            e.validation_fields = ["campoDesconocidoQueNoSabemosCorregir: field required"]
            raise e

    async def go():
        HUB_RUNTIME.reset()
        qi = QuantDataIntelligence(); qi.client = Rejecting(); qi._symbol = "SPY"
        tool = qi.catalog["dark_pool_levels"]
        await qi._fetch(tool)
        return tool, qi.get("dark_pool_levels")

    tool, block = asyncio.run(go())
    assert len(calls) == 1, (
        f"un 400 sin corrección conocida debe parar en el primer intento, "
        f"no en el {len(calls)}º")
    assert tool.validation_error and "campoDesconocido" in tool.validation_error
    assert block.get("lane_status") == "REQUEST_INVALID"
    assert any("campoDesconocido" in f for f in (block.get("lane_fields") or []))


def test_a_422_is_not_a_fault_and_does_not_mark_the_tool_broken():
    """Petición válida, sin datos. No es un fallo del programa ni del proveedor."""
    from app.providers.quantdata.intelligence import QuantDataIntelligence
    from app.providers.quantdata.client import QuantDataError
    from app.core.data_hub_runtime import HUB_RUNTIME

    class NoData:
        async def post(self, path, body):
            e = QuantDataError("Quant Data HTTP 422: no data for requested window")
            e.status_code = 422
            raise e

    async def go():
        HUB_RUNTIME.reset()
        qi = QuantDataIntelligence(); qi.client = NoData(); qi._symbol = "QQQ"
        tool = qi.catalog["dark_pool_levels"]
        await qi._fetch(tool)
        return tool, qi.get("dark_pool_levels")

    tool, block = asyncio.run(go())
    assert tool.last_error is None, "un 422 no marca la herramienta como averiada"
    assert tool.transient_failures == 0, "un 422 no entra en el backoff de reintentos"
    assert block.get("state") == "NO_PROVIDER_DATA"
    assert "no tiene datos" in str(block.get("detail"))


def test_a_5xx_backs_off_and_says_so():
    from app.providers.quantdata.intelligence import QuantDataIntelligence
    from app.providers.quantdata.client import QuantDataError
    from app.core.data_hub_runtime import HUB_RUNTIME

    class Down:
        async def post(self, path, body):
            e = QuantDataError("Quant Data HTTP 503: upstream unavailable")
            e.status_code = 503
            raise e

    async def go():
        HUB_RUNTIME.reset()
        qi = QuantDataIntelligence(); qi.client = Down(); qi._symbol = "DIA"
        tool = qi.catalog["dark_flow"]
        await qi._fetch(tool)
        return tool, qi.get("dark_flow")

    tool, block = asyncio.run(go())
    assert tool.transient_failures >= 1, "un 5xx sí programa reintento"
    assert block.get("lane_status") == "PROVIDER_ERROR"


# ─────────────────────────────────────────────── 4 · EL 200 CONSERVA LOS CAMPOS

RAW_LEVELS = {
    "latestStockPrice": 663.21,
    "levels": [
        {"price": 660.0, "notional": 1.2e8, "shares": 181000, "tradeCount": 412,
         "darkVolume": 181000, "litVolume": 900000, "percentOfVolume": 16.7,
         "venue": "TRF"},
        {"price": 662.5, "notional": 8.0e7, "size": 120000, "trades": 300},
    ],
}


def test_the_normalizer_preserves_every_official_field():
    from app.providers.quantdata.tools import norm_levels

    out = norm_levels(RAW_LEVELS)
    assert out["ready"] and out["count"] == 2
    first = out["rows"][0]
    assert first["price"] == 660.0
    assert first["notional"] == 1.2e8
    assert first["shares"] == 181000
    assert first["prints"] == 412, "el recuento de operaciones llega como tradeCount"
    assert first["dark_volume"] == 181000 and first["lit_volume"] == 900000
    assert first["pct_of_volume"] == 16.7
    # El precio de referencia del subyacente viaja a nivel de RESPUESTA, no por
    # fila. Leer sólo `rows` lo perdía.
    assert out["latest_stock_price"] == 663.21
    # Un campo oficial que este normalizador todavía no nombra no se descarta en
    # silencio: descartar es indistinguible, desde la pantalla, de no recibirlo.
    assert first["extra"] == {"venue": "TRF"}


def test_alternative_field_names_still_map():
    from app.providers.quantdata.tools import norm_levels

    second = norm_levels(RAW_LEVELS)["rows"][1]
    assert second["shares"] == 120000 and second["prints"] == 300


def test_raw_to_normalizer_to_hub_to_frontend_stays_direct_provider():
    """La cadena entera, con el modo de fuente al final: DIRECT_PROVIDER."""
    from datetime import datetime, timezone
    from app.providers.quantdata.tools import norm_levels
    from app.core import quant_data_hub as HUB
    from app.core.data_lineage import LINEAGE, DIRECT_PROVIDER

    normalized = norm_levels(RAW_LEVELS)
    intel = {"dark_pool_levels": {**normalized, "symbol": "SPY",
                                  "path": "/v1/equities/tool/dark-pool-levels",
                                  "fetched_at": datetime.now(timezone.utc).isoformat()}}
    out = HUB.dark_pool("SPY", intel)
    assert out["ready"] is True
    assert out["levels"][0]["price"] == 660.0, "el valor sobrevive los cuatro tramos"
    assert out["lanes"]["dark_pool_levels"]["state"] == "DIRECT_PROVIDER_OK"

    record = next(r for r in LINEAGE.audit("SPY")["records"]
                  if r["metric"] == "QD_DARK_POOL_LEVELS")
    assert record["source_mode"] == DIRECT_PROVIDER
    assert record["state"] == "DATA_OK"

    from app.core.data_lineage import certification_chain
    stages = certification_chain("QD_DARK_POOL_LEVELS", "SPY")["stages"]
    names = [s["stage"] for s in stages]
    assert names[0] == "RAW_PROVIDER" and names[-1] == "FRONTEND"


# ─────────────────────────────────────────────── 5 · CARRILES INDEPENDIENTES

def _live(rows):
    from datetime import datetime, timezone
    return {"ready": True, "rows": rows, "count": len(rows),
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def test_a_broken_levels_lane_does_not_take_down_dark_flow():
    from app.core import quant_data_hub as HUB

    intel = {
        "dark_flow": _live([{"dark_volume": 1e6, "total_volume": 4e6,
                             "dark_notional": 5.2e8}]),
        "dark_pool_levels": {"ready": False, "rows": [],
                             "lane_status": "REQUEST_INVALID",
                             "lane_detail": "campo no reconocido: sessionDate",
                             "lane_fields": ["sessionDate"]},
        "equity_prints": _live([{"off_exchange": True, "notional": 3e6,
                                 "price": 500.0, "size": 6000}]),
    }
    out = HUB.dark_pool("SPY", intel)
    assert out["ready"] is True, "dos carriles sanos bastan para tener sección"
    assert out["notional"] == 5.2e8
    assert out["dark_share_pct"] == 25.0
    lanes = out["lanes"]
    assert lanes["dark_flow"]["state"] == "DIRECT_PROVIDER_OK"
    assert lanes["equity_prints"]["state"] == "DIRECT_PROVIDER_OK"
    assert lanes["dark_pool_levels"]["state"] == "REQUEST_INVALID"
    # Y la degradación se declara: media verdad sobre la cobertura es peor que
    # ninguna.
    assert out["diagnosis"]["degraded"] is True
    assert out["diagnosis"]["lanes_broken"] == ["dark_pool_levels"]


def test_each_lane_is_its_own_channel_in_the_hub_runtime():
    """Aislamiento real, no una promesa en un comentario."""
    from app.core.data_hub_runtime import DataHubRuntime

    async def go():
        rt = DataHubRuntime()

        async def boom():
            raise RuntimeError("dark-pool-levels caído")

        async def fine():
            return {"rows": [1, 2, 3]}

        bad = await rt.fetch("dark_pool_levels", "SPY", boom, timeout_s=1.0)
        good = await rt.fetch("dark_flow", "SPY", fine, timeout_s=1.0)
        return bad, good, rt.snapshot()

    bad, good, snap = asyncio.run(go())
    assert bad.get("ready") is False
    assert good.get("ready") is True, "el fallo de un canal no contagia al vecino"
    names = {c["channel"] for c in ((snap.get("channels") or {}).get("channels") or [])}
    assert {"dark_pool_levels", "dark_flow"} <= names


# ─────────────────────────────────────────────── 6 · TRI-ESTADO FUERA DE BOLSA

def test_a_missing_off_exchange_flag_is_unknown_not_false():
    from app.providers.quantdata.tools import norm_prints

    out = norm_prints({"prints": [
        {"t": "1", "price": 100.0, "size": 500, "venue": "TRF"},     # fuera de bolsa
        {"t": "2", "price": 100.1, "size": 300, "venue": "NASDAQ"},  # en bolsa
        {"t": "3", "price": 100.2, "size": 900},                     # sin señal
    ]})
    rows = out["rows"]
    assert len(rows) == 3, "un print sin `venue` no se descarta"
    assert rows[0]["off_exchange"] is True
    assert rows[1]["off_exchange"] is False
    assert rows[2]["off_exchange"] is None, (
        "ausente ≠ False: convertirlo en False afirma que se ejecutó en bolsa")
    assert out["off_exchange_unknown"] == 1
    assert out["off_exchange_confirmed"] == 1


def test_prints_that_cannot_be_classified_are_not_a_healthy_lane():
    from app.core import quant_data_hub as HUB

    intel = {"equity_prints": _live([{"price": 100.0, "size": 900,
                                      "off_exchange": None}])}
    out = HUB.dark_pool("AMD", intel)
    assert out["lanes"]["equity_prints"]["state"] == "NO_CLASIFICABLE"
    assert out["prints_unclassified"] == 1
    assert "sin clasificar" in out["coverage_reason"]


# ─────────────────────────────────────────────── 7 · OCHO ESTADOS, UNA PANTALLA

def test_the_eight_internal_states_exist_with_cause_and_remedy():
    from app.core import dark_pool_state as S

    assert set(S.DARK_POOL_STATES) == {
        "DIRECT_PROVIDER_OK", "SIN_DATOS_REALES", "MARKET_CLOSED", "STALE",
        "REQUEST_INVALID", "PROVIDER_ERROR", "PARSER_ERROR", "NO_CLASIFICABLE"}
    for state in S.DARK_POOL_STATES:
        if state == S.DIRECT_PROVIDER_OK:
            continue
        assert S.REMEDY[state], f"{state} sin remedio declarado no sirve de nada"


def test_the_analyst_screen_never_reads_a_provider_error_code():
    from app.core import dark_pool_state as S

    assert S.screen_label(S.REQUEST_INVALID) == "SIN DATOS"
    assert S.screen_label(S.PROVIDER_ERROR) == "SIN DATOS"
    assert S.screen_label(S.PARSER_ERROR) == "SIN DATOS"
    assert S.screen_label(S.MARKET_CLOSED) == "MERCADO CERRADO"
    # Y cada uno de los ocho tiene texto de pantalla: ninguno se escapa en crudo.
    for state in S.DARK_POOL_STATES:
        assert S.screen_label(state) is not None


def test_the_exact_cause_reaches_the_auditor_and_not_the_main_screen():
    js = _read("app/static/itmq_app.js")
    html = _read("app/templates/terminal.html")
    # La tabla de carriles vive en la vista de AUDITOR, no en DARK POOL.
    assert 'id="tblDarkLanes"' in html
    auditor_view = html.split('<section class="view" data-view="fuentes">')[1].split("</section>")[0]
    assert 'id="tblDarkLanes"' in auditor_view, (
        "el diagnóstico por carril pertenece al Auditor, no a la pantalla de análisis")
    # Se lee del BUNDLE que llega en el render, no del estado local de la app:
    # `state` es la vista y el símbolo activos, no la respuesta del servidor.
    assert "(d.auditor || {}).dark_pool" in js
    # Si algún estado interno alcanzara la pantalla, se lee como análisis.
    assert "REQUEST_INVALID:" in js and "MARKET_CLOSED:" in js


def test_every_lane_carries_its_own_state_to_the_bundle():
    from app.core import quant_data_hub as HUB

    out = HUB.dark_pool("XLF", {})
    assert set(out["lanes"]) == {"dark_flow", "dark_pool_levels", "equity_prints"}
    assert [r["lane"] for r in out["lane_rows"]] == [
        "dark_flow", "dark_pool_levels", "equity_prints"]
    for row in out["lane_rows"]:
        assert row["state"] in HUB.dark_pool_state.DARK_POOL_STATES
        assert "remedy" in row and "screen" in row


# ─────────────────────────────────────────────── 8 · VERIFICACIÓN MULTI-ACTIVO

def test_the_live_script_checks_three_lanes_across_a_basket():
    src = _read("scripts/verify_live_quantdata.py")
    assert "DARK_POOL_LANES" in src and "DARK_POOL_BASKET" in src
    assert "--dark-pool" in src
    for lane in ("dark_flow", "dark_pool_levels", "equity_prints"):
        assert lane in src
    # No sólo DIA: la cesta incluye ETF de escalas distintas y equities líquidos.
    basket = re.search(r"DARK_POOL_BASKET[^=]*=\s*\(([^)]*)\)", src).group(1)
    names = re.findall(r'"([A-Z]+)"', basket)
    assert {"DIA", "SPY", "QQQ"} <= set(names)
    assert len(names) >= 6, "una cesta de tres ETF no distingue activo de carril"


def test_the_live_script_sends_the_same_body_production_sends():
    src = _read("scripts/verify_live_quantdata.py")
    assert "tool.request_body(ticker)" in src
    assert "client.post(path, tool.body(ticker))" not in src, (
        "verificar con un cuerpo distinto al de producción verifica otra cosa")


def test_the_live_script_keeps_the_rejected_field_not_the_first_line():
    src = _read("scripts/verify_live_quantdata.py")
    assert "validation_fields" in src and "status_code" in src
    assert "rejected_fields" in src


# ─────────────────────────────────────────────── 9 · MULTI-ACTIVO DE VERDAD

DARK_POOL_PATH = ("app/core/dark_pool_state.py", "app/core/quant_data_hub.py",
                  "app/providers/quantdata/tools.py")


def test_no_ticker_is_written_into_the_dark_pool_path():
    """Ni un solo símbolo escrito a mano en la ruta que produce la sección."""
    ticker = re.compile(r'"(DIA|SPY|QQQ|IWM|AAPL|TSLA|NVDA|AMD|XLF|SOFI)"')
    for rel in DARK_POOL_PATH:
        src = _read(rel)
        # Las líneas de ejemplo de la documentación interna no cuentan como código.
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        hits = ticker.findall(code)
        assert not hits, f"{rel} contiene tickers escritos a mano: {sorted(set(hits))}"


def test_the_chain_window_is_proportional_for_every_discovered_asset():
    """v1.46.0 · Doce dólares no son la misma ventana en SPY que en un valor de $9."""
    from app.core.assets import chain_window_for, UNIVERSAL_CHAIN_WINDOW_PCT

    for symbol, spot in (("SPY", 600.0), ("QQQ", 480.0), ("XLF", 48.4), ("SOFI", 9.52)):
        out = chain_window_for(symbol, spot=spot)
        assert out["universal"] is True
        band = out["window"]
        pct = 100.0 * band / spot
        assert abs(pct - UNIVERSAL_CHAIN_WINDOW_PCT) < 1e-6, (
            f"{symbol}: ±{pct:.1f}% del precio, no proporcional")


def test_the_order_flow_scale_has_no_absolute_floor():
    """Un suelo de $1.000 comprimía los activos pequeños y no los grandes."""
    js = _read("app/static/itmq_orderflow.js")
    code = "\n".join(l for l in js.splitlines()
                     if not l.strip().startswith(("//", "*", "/*")))
    assert "Math.max(max / 400, 1000)" not in code, (
        "un umbral absoluto en dólares no es multi-activo")
    assert "Number.MIN_VALUE" in code


def test_the_heatmap_normalization_is_rank_based_for_every_asset():
    """Un p95 lineal daba 45% de relleno en un activo y 97% en otro."""
    import numpy as np
    from app.core.asset_normalization import normalize_matrix

    fills = []
    for scale in (1e9, 1e7, 1e5, 1e3):
        rng = np.random.default_rng(7)
        m = rng.pareto(1.2, size=(40, 30)) * scale
        out = normalize_matrix(m, symbol="X")
        assert out["normalization"] == "ASSET_RANK_PERCENTILE"
        fills.append(out["filled_ratio"])
    spread = max(fills) - min(fills)
    assert spread < 0.05, (
        f"el relleno varía {spread:.0%} entre escalas: la normalización sigue "
        f"dependiendo del tamaño del activo")


# ─────────────────────────────────────────────── 10 · NINGÚN CERO FABRICADO

def test_a_technical_failure_never_becomes_a_zero():
    from app.core import quant_data_hub as HUB

    intel = {"dark_pool_levels": {"ready": False, "rows": [],
                                  "lane_status": "REQUEST_INVALID",
                                  "lane_detail": "cuerpo rechazado"}}
    out = HUB.dark_pool("IWM", intel)
    assert out["ready"] is False
    assert out["notional"] is None, "un fallo de datos no es un nocional de cero"
    assert out["shares"] is None
    assert out["trades"] is None
    assert out["dark_share_pct"] is None
    # Y se sabe por qué.
    assert out["diagnosis"]["state"] == "REQUEST_INVALID"
    assert out["diagnosis"]["remedy"]


def test_the_terminal_publishes_the_cause_for_the_auditor_only():
    from app.terminal_api import build_terminal_bundle

    bundle = build_terminal_bundle(
        state={"active_symbol": "SPY", "ready": True, "spot": 600.0},
        trace={}, intelligence={"dark_pool_levels": {
            "ready": False, "rows": [], "lane_status": "REQUEST_INVALID",
            "lane_detail": "lookBackPeriod: field required",
            "lane_fields": ["lookBackPeriod: field required"]}})
    dp = bundle["dark_pool"]
    assert dp["notional"] is None and dp["count"] is None
    assert dp["display_reason"] in ("SIN DATOS", "MERCADO CERRADO")
    auditor = bundle["auditor"]["dark_pool"]
    assert auditor["diagnosis"]["state"] == "REQUEST_INVALID"
    rejected = auditor["lanes"][1]["rejected_fields"]
    assert any("lookBackPeriod" in f for f in rejected)
