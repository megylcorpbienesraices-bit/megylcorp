"""CERTIFICACIÓN · Quant Data como autoridad · ITM QUANT v1.43.0

Qué certifica este fichero y qué NO
-----------------------------------
La especificación dice, con razón, que una integración no está terminada porque
el endpoint responda, el código compile o el lint marque cero errores. Lo que se
ejercita aquí es lo que sí demuestra algo:

  * **Autoridad de fuente** — con Quant Data sano, ITM QUANT USA Quant Data. No
    se comprueba que «funcione»: se comprueba que el valor publicado es el del
    proveedor y no el del cálculo propio.
  * **Fallback honesto** — sin proveedor, el respaldo sostiene la vista pero va
    etiquetado FALLBACK. Ningún fallback se hace pasar por dato directo.
  * **Cadena de custodia** — RAW → NORMALIZER → ENGINE → API INTERNA → FRONTEND,
    demostrando que el valor y su significado se conservan.
  * **Estados de dato** — un fallo nunca sale en pantalla como `$0.0`.
  * **Multi-activo** — el mismo pipeline con varios activos de comportamiento
    distinto y escalas incomparables. Nada calibrado para un símbolo concreto.
  * **Separación DIRECT_PROVIDER / DERIVED** — la inteligencia propia jamás se
    presenta como dato del proveedor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


# Tres activos con escalas deliberadamente incomparables: un índice enorme, un
# ETF mediano y una acción pequeña. Si algo estuviera calibrado para uno, los
# otros dos lo delatarían. Ninguno es el que se usó para desarrollar.
ASSETS = [
    ("SPY", 600.0, 1e9),
    ("XLF", 48.0, 5e6),
    ("SOFI", 9.5, 2e4),
]


# ───────────────────────────────────────────── utilidades de fixture

def _interval_payload(base_strike: float, magnitude: float, buckets: int = 8):
    """Payload del Interval Map con la forma REAL del proveedor."""
    start = 1758205800000
    data = {}
    for i in range(buckets):
        strikes = {}
        for j in range(5):
            strikes[str(base_strike + j)] = {
                "CALL": magnitude * (j + 1) * (1 + 0.1 * i),
                "PUT": -magnitude * (5 - j) * (1 + 0.05 * i),
            }
        data[str(start + i * 300000)] = {"2026-09-25": strikes}
    return {"data": data}


def _intel(symbol: str, base: float, magnitude: float):
    from app.providers.quantdata.tools import norm_interval_map
    rows_k = [{"strike": base + i, "value": magnitude * (i - 2), "call": magnitude * i,
               "put": -magnitude} for i in range(5)]
    t0 = datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc)
    flow = [{"t": (t0 + timedelta(minutes=i)).isoformat(),
             "call": magnitude * (6 if i == 30 else 0.2),
             "put": -magnitude * 0.05,
             "value": None, "stock_price": base + i * 0.01} for i in range(60)]
    return {
        "gex_by_strike": {"ready": True, "rows": rows_k, "path": "/v1/options/tool/exposure-by-strike"},
        "dex_by_strike": {"ready": True, "rows": rows_k},
        "vex_by_strike": {"ready": True, "rows": rows_k},
        "chex_by_strike": {"ready": True, "rows": rows_k},
        "oi_by_strike": {"ready": True, "rows": [{"strike": base + i, "value": 1000 * (i + 1),
                                                  "call": 600 * (i + 1), "put": 400 * (i + 1)}
                                                 for i in range(5)]},
        "oi_change": {"ready": True, "rows": [{"strike": base, "value": -380}]},
        "net_flow": {"ready": True, "rows": flow},
        "interval_map_gamma": norm_interval_map(_interval_payload(base, magnitude), "GAMMA"),
        "interval_map_delta": norm_interval_map(_interval_payload(base, magnitude), "DELTA"),
        "interval_map_vanna": norm_interval_map(_interval_payload(base, magnitude), "VANNA"),
        "interval_map_charm": norm_interval_map(_interval_payload(base, magnitude), "CHARM"),
        "dark_flow": {"ready": True, "rows": [
            {"t": "2026-09-18T14:30:00+00:00", "dark_volume": 120000, "total_volume": 500000,
             "dark_notional": magnitude * 40, "dark_share_pct": 24.0, "stock_price": base}]},
        "dark_pool_levels": {"ready": True, "rows": [
            {"price": base + 0.5, "notional": magnitude * 30, "shares": 180000, "prints": 12}]},
        "volatility_skew": {"ready": True, "rows": [{"x": 1.0, "y": 0.31, "label": "a"}]},
        "term_structure": {"ready": True, "rows": [{"x": 7.0, "y": 22.0, "label": "b"}]},
        "iv_rank": {"ready": True, "raw": {"ivRank": 41.0}},
        "options_order_flow_raw": {"ready": True, "rows": [{
            "t": (t0 + timedelta(minutes=30, seconds=8)).isoformat(),
            "option_type": "CALL", "strike": base + 4, "expiration": "2026-09-25",
            "dte": 7.0, "premium": magnitude * 3, "size": 400, "price": 8.0,
            "side": "BUY", "direction": 1, "execution": "INTERMARKET_SWEEP",
            "greeks": {"delta": 0.62, "gamma": 0.04, "theta": -0.18, "vega": 0.11,
                       "rho": 0.02, "vanna": 0.007, "charm": -0.003},
            "implied_volatility": 0.24, "open_interest": 4200.0}]},
    }


@pytest.fixture(autouse=True)
def _clean_lineage():
    from app.core.data_lineage import LINEAGE
    LINEAGE.reset()
    yield
    LINEAGE.reset()


# ═══════════════════════════════════════════ 1 · AUTORIDAD DE FUENTE

@pytest.mark.parametrize("symbol,base,magnitude", ASSETS)
def test_with_quant_data_healthy_the_terminal_really_uses_quant_data(symbol, base, magnitude):
    """La prueba que la especificación pide por su nombre.

    Con el proveedor sano, el número publicado tiene que ser el SUYO. Se le da al
    motor un perfil propio en un strike que el proveedor no publica: si el valor
    de ese strike apareciera, el motor estaría ganando.
    """
    from app.terminal_api import _exposicion
    intel = _intel(symbol, base, magnitude)
    trace = {"profiles": {"rows": [{"strike": base + 999, "gamma_m": 7.0}], "spot": base}}
    out = _exposicion(trace, {"symbol": symbol, "spot": base}, intel)

    assert out["by_strike_source"] == "QUANTDATA"
    assert out["source_mode"] == "DIRECT_PROVIDER"
    assert out["fallback_used"] is False
    strikes = [r["strike"] for r in out["by_strike"]]
    assert base + 999 not in strikes, "el perfil del motor tapó al del proveedor"
    assert strikes == [base + i for i in range(5)]


@pytest.mark.parametrize("symbol,base,magnitude", ASSETS)
def test_the_dynamic_map_of_trace_comes_from_the_interval_map(symbol, base, magnitude):
    """TRACE no puede seguir dibujando un mapa estático del motor."""
    from app.terminal_api import _interval_map
    intel = _intel(symbol, base, magnitude)
    engine = {"ready": True, "strikes": [base], "times": ["09:30", "09:35"],
              "gamma_m": [[9.0, 9.0]]}
    im = _interval_map(intel, {"heatmap_history": engine, "symbol": symbol})
    assert im["ready"] and im["source"] == "QUANTDATA"
    assert im["source_mode"] == "DIRECT_PROVIDER"
    assert im["axis"] == {"x": "TIME", "y": "STRIKE", "intensity": "EXPOSURE_MAGNITUDE"}
    assert len(im["times"]) == 8, "el mapa tiene que cubrir la sesión, no un instante"
    assert len(im["strikes"]) == 5


@pytest.mark.parametrize("greek,label", [("GAMMA", "GEX"), ("DELTA", "DEX"),
                                         ("VANNA", "VEX"), ("CHARM", "CHEX")])
def test_the_four_greeks_of_the_map_are_selectable(greek, label):
    from app.terminal_api import _interval_map
    intel = _intel("SPY", 600.0, 1e9)
    im = _interval_map(intel, {"symbol": "SPY"}, greek)
    assert im["ready"] and im["greek"] == greek and im["label"] == label
    assert im["source_mode"] == "DIRECT_PROVIDER"


def test_open_interest_is_never_rebuilt_from_volume():
    from app.terminal_api import _open_interest
    intel = _intel("SPY", 600.0, 1e9)
    out = _open_interest({"profiles": {"rows": []}}, {"symbol": "SPY"}, intel)
    assert out["by_strike_source"] == "QUANTDATA"
    assert out["reconstruction"] == "NEVER_FROM_VOLUME"
    assert out["oi_change"], "Open Interest Change es del proveedor, no una resta propia"


def test_net_drift_comes_only_from_the_official_endpoint():
    """Ni GEX, ni DEX, ni Net Flow pueden rellenar una curva de Net Drift."""
    from app.terminal_api import _net_drift
    intel = _intel("SPY", 600.0, 1e9)          # trae net_flow pero NO net_drift
    out = _net_drift({"symbol": "SPY"}, intel)
    assert out["ready"] is False
    assert out["state"] == "NO_PROVIDER_DATA"
    assert not out.get("series"), "se rellenó Net Drift con otra magnitud"
    src = text("app/core/net_drift.py")
    assert "POST /v1/options/tool/net-drift" in src


# ═══════════════════════════════════════════ 2 · FALLBACK HONESTO

def test_no_fallback_passes_itself_off_as_direct_data():
    from app.terminal_api import _exposicion, _interval_map, _open_interest
    trace = {"profiles": {"rows": [{"strike": 600.0, "gamma_m": 2.0, "oi": 1234}], "spot": 600.0}}
    exp = _exposicion(trace, {"symbol": "SPY", "spot": 600.0}, {})
    assert exp["source_mode"] == "FALLBACK" and exp["fallback_used"] is True

    im = _interval_map({}, {"heatmap_history": {
        "ready": True, "strikes": [600.0], "times": ["09:30", "09:35"],
        "gamma_m": [[1.0, 2.0]]}, "symbol": "SPY"})
    assert im["ready"] and im["source_mode"] == "FALLBACK" and im["fallback_used"] is True

    oi = _open_interest(trace, {"symbol": "SPY"}, {})
    assert oi["by_strike_source_mode"] == "FALLBACK"


def test_a_healthy_primary_source_cannot_be_covered_in_silence():
    """La regla dura del registro de procedencia, ejercitada."""
    from app.core.data_lineage import guard_primary_source, SilentSubstitution, DERIVED, FALLBACK
    with pytest.raises(SilentSubstitution):
        guard_primary_source("QD_GEX", "SPY", publishing_mode=DERIVED, provider_healthy=True)
    # Un FALLBACK declarado sí está permitido: va etiquetado y el Auditor lo ve.
    guard_primary_source("QD_GEX", "SPY", publishing_mode=FALLBACK, provider_healthy=True)
    # Sin proveedor sano, nada que tapar.
    guard_primary_source("QD_GEX", "SPY", publishing_mode=DERIVED, provider_healthy=False)


# ═══════════════════════════════════════════ 3 · CADENA DE CUSTODIA

@pytest.mark.parametrize("symbol,base,magnitude", ASSETS)
def test_the_chain_raw_to_frontend_preserves_value_and_meaning(symbol, base, magnitude):
    """RAW PROVIDER → NORMALIZER → ENGINE → API INTERNA → FRONTEND."""
    from app.core import quant_data_hub as HUB
    from app.core.data_lineage import certification_chain

    intel = _intel(symbol, base, magnitude)
    hub = HUB.hub_snapshot(symbol, intel)
    assert hub["exposure_by_strike"]["ready"] is True

    chain = certification_chain("QD_GEX", symbol)
    assert chain["ready"] is True, chain.get("broken_at")
    assert chain["record"]["source_mode"] == "DIRECT_PROVIDER"
    assert chain["meaning_preserved"] is True
    assert [s["stage"] for s in chain["stages"]] == [
        "RAW_PROVIDER", "NORMALIZER", "ENGINE", "INTERNAL_API", "FRONTEND"]


def test_the_raw_value_survives_the_normalizer_unchanged():
    """El neto de una celda es call + put, con el signo tal y como llega."""
    from app.providers.quantdata.tools import norm_interval_map
    payload = {"data": {"1758205800000": {"2026-09-25": {"600": {"CALL": 10.0, "PUT": -4.0}}}}}
    m = norm_interval_map(payload, "GAMMA")
    assert m["call_matrix"][0][0] == 10.0
    assert m["put_matrix"][0][0] == -4.0
    # SUMA, no resta: putExposure llega ya firmada.
    assert m["matrix"][0][0] == 6.0


# ═══════════════════════════════════════════ 4 · ESTADOS DE DATO

@pytest.mark.parametrize("block,expected", [
    (None, "NO_PROVIDER_DATA"),
    ({"ready": False}, "NO_PROVIDER_DATA"),
    ({"ready": False, "rows": [1, 2, 3]}, "FILTERED_ALL"),
    ({"ready": False, "error": "HTTP 500"}, "PROVIDER_ERROR"),
    ("no soy un objeto", "PARSER_ERROR"),
    ({"ready": True, "rows": [1], "age_seconds": 99999.0}, "STALE"),
    ({"ready": True, "rows": [1]}, "DATA_OK"),
])
def test_every_failure_has_its_own_state(block, expected):
    from app.core.quant_data_hub import classify
    assert classify(block)["state"] == expected


def test_a_data_failure_never_reaches_the_screen_as_zero():
    """`$0.0` con un fallo detrás es una afirmación falsa, no un hueco."""
    from app.terminal_api import _dark_pool
    out = _dark_pool({"symbol": "AAPL", "large_prints": {}}, {"candles": []}, {})
    assert out["notional"] is None and out["count"] is None
    assert out["display"] == "SIN DATOS"
    assert out["state"] in ("NO_PROVIDER_DATA", "PROVIDER_ERROR", "PARSER_ERROR")


def test_a_real_zero_is_only_a_zero_when_there_were_valid_data():
    from app.core.data_lineage import real_zero, DATA_OK, PROVIDER_ERROR
    assert real_zero(0.0, DATA_OK) is True
    assert real_zero(0.0, PROVIDER_ERROR) is False
    assert real_zero(None, DATA_OK) is False


def test_dark_pool_uses_quant_data_directly_not_only_the_venue_field():
    from app.terminal_api import _dark_pool
    intel = _intel("SPY", 600.0, 1e9)
    out = _dark_pool({"symbol": "SPY", "large_prints": {}}, {"candles": []}, intel)
    assert out["ready"] is True and out["state"] == "DATA_OK"
    assert out["notional"] and out["notional"] > 0
    assert out["sources"]["primary"] == "QUANT_DATA"
    assert out["sources"]["secondary"] == "ITM_QUANT_VENUE_CLASSIFICATION"


# ═══════════════════════════════════════════ 5 · QFLOW ENRIQUECIDO

def test_qflow_does_not_stop_at_net_flow():
    """Una concentración sin atribuir es un pico anónimo."""
    from app.terminal_api import _flujo_ordenes
    intel = _intel("SPY", 600.0, 1e9)
    out = _flujo_ordenes({"symbol": "SPY"}, intel)
    conc = out["qflow_concentration"]
    assert conc["events"], "no se detectó la concentración inyectada"
    att = conc["attribution"]
    assert att["ready"] is True and att["matched"] >= 1
    ev = att["events"][0]
    assert ev["calls"] == 1 and ev["buys"] == 1
    assert "SWEEP" in ev["executions"]
    assert ev["dominant_strike"]["strike"] == 604.0
    assert ev["top_trades"][0]["expiration"] == "2026-09-25"
    assert ev["top_trades"][0]["dte"] == 7.0
    assert ev["top_trades"][0]["aggressor"] == "BUY"


def test_the_concentration_marker_carries_arrow_and_magnitude():
    from app.terminal_api import _qflow
    out = _qflow({"symbol": "SPY"}, _intel("SPY", 600.0, 1e9))
    assert out["markers"], "sin marcas no hay nada que dibujar sobre el precio"
    label = out["markers"][0]["label"]
    assert label.startswith(("▲", "▼", "◆")) and "$" in label and label.endswith("M")


def test_the_flow_section_carries_the_six_pieces_without_creating_another():
    from app.terminal_api import _flujo_ordenes
    out = _flujo_ordenes({"symbol": "SPY"}, _intel("SPY", 600.0, 1e9))
    for key in ("net_flow", "net_drift", "order_flow_consolidated",
                "order_flow_unconsolidated", "qflow", "qflow_concentration"):
        assert key in out, key
    assert out["section"] == "FLUJO_DE_ORDENES"


# ═══════════════════════════════════════════ 6 · NORMALIZACIÓN POR ACTIVO

def test_the_concentration_threshold_is_not_a_fixed_dollar_limit():
    """El mismo umbral para SPY y para una acción pequeña detectaría en uno y
    sería sordo en el otro. Por eso se mide sobre el propio activo."""
    from app.core.asset_normalization import concentration_threshold
    import random
    random.seed(7)
    small = [random.uniform(1e4, 5e4) for _ in range(60)]
    big = [random.uniform(1e6, 5e6) for _ in range(60)]
    t_small = concentration_threshold(small, symbol="A")
    t_big = concentration_threshold(big, symbol="B")
    assert t_small.ready and t_big.ready
    assert t_big.floor > t_small.floor * 10, "el umbral no se adapta a la escala del activo"
    assert t_small.method == "ASSET_RELATIVE_TRIPLE_GATE"


def test_no_ticker_is_hardcoded_in_the_new_pipeline():
    """«Funciona en DIA» no certifica nada si DIA está escrito en el código."""
    import re
    for rel in ("app/core/asset_normalization.py", "app/core/quant_data_hub.py",
                "app/core/itmq_intelligence.py", "app/core/qflow.py",
                "app/core/data_lineage.py"):
        src = text(rel)
        # Se buscan tickers como literales de cadena, no palabras en prosa.
        found = re.findall(r'["\'](DIA|SPY|QQQ|AAPL|IWM|TSLA|NVDA)["\']', src)
        assert not found, f"{rel} tiene tickers fijados en código: {sorted(set(found))}"


@pytest.mark.parametrize("symbol,base,magnitude", ASSETS)
def test_the_same_pipeline_runs_for_assets_with_incomparable_scales(symbol, base, magnitude):
    from app.terminal_api import build_terminal_bundle
    intel = _intel(symbol, base, magnitude)
    trace = {"profiles": {"rows": [], "spot": base}, "candles": [
        {"t": "2026-09-18T14:30:00+00:00", "c": base}], "levels": [
        {"price": base + 1, "kind": "call_wall"}], "symbol": symbol}
    b = build_terminal_bundle(state={"symbol": symbol, "spot": base, "ready": True},
                              trace=trace, intelligence=intel)
    assert b["exposicion"]["by_strike_source"] == "QUANTDATA"
    assert b["open_interest"]["by_strike_source"] == "QUANTDATA"
    assert b["dark_pool"]["ready"] is True
    assert b["interval_map"]["source_mode"] == "DIRECT_PROVIDER"
    assert b["volatilidad"]["skew_source"] == "QUANTDATA"
    assert b["capacidades"]["provider_healthy"] is True


# ═══════════════════════════════════════════ 7 · SEPARACIÓN DE MODOS

def test_provider_data_and_own_intelligence_never_share_a_namespace():
    from app.core.data_lineage import (METRIC_PLAN, PRIMARY_PROVIDER_METRICS,
                                       DERIVED_METRICS, DIRECT_PROVIDER, DERIVED)
    for m in PRIMARY_PROVIDER_METRICS:
        assert METRIC_PLAN[m].mode == DIRECT_PROVIDER
        assert not m.startswith("ITMQ_"), f"{m} es propio y se declara como del proveedor"
    for m in DERIVED_METRICS:
        assert METRIC_PLAN[m].mode == DERIVED
        assert m.startswith("ITMQ_"), f"{m} es una conclusión propia sin prefijo ITMQ_"
    # Los ejemplos que la especificación nombra, tal cual.
    assert METRIC_PLAN["QD_GEX"].mode == DIRECT_PROVIDER
    assert METRIC_PLAN["QD_DEX"].mode == DIRECT_PROVIDER
    assert METRIC_PLAN["QD_NET_DRIFT"].mode == DIRECT_PROVIDER
    assert METRIC_PLAN["ITMQ_GAMMA_PRESSURE"].mode == DERIVED
    assert METRIC_PLAN["ITMQ_FLOW_CONFLUENCE"].mode == DERIVED
    assert METRIC_PLAN["ITMQ_STRUCTURAL_SCORE"].mode == DERIVED


def test_every_lineage_record_carries_the_required_fields():
    from app.core.data_lineage import LINEAGE, publish, DIRECT_PROVIDER
    publish("QD_GEX", "SPY", 1.2e9, source_mode=DIRECT_PROVIDER,
            raw_value={"rows": 5}, normalized_value=1.2e9, rows=5)
    rec = LINEAGE.for_metric("QD_GEX", "SPY")
    for field in ("metric", "symbol", "provider", "endpoint", "source_mode", "timestamp",
                  "raw_value", "normalized_value", "final_value", "fallback_used",
                  "derivation"):
        assert field in rec, field
    assert rec["provider"] == "QUANTDATA"
    assert rec["endpoint"].startswith("POST /v1/options/tool/exposure-by-strike")


def test_the_engine_reads_the_hub_instead_of_rebuilding_provider_metrics():
    from app.core import itmq_intelligence as IQ
    from app.core import quant_data_hub as HUB
    intel = _intel("SPY", 600.0, 1e9)
    hub = HUB.hub_snapshot("SPY", intel)
    out = IQ.analyze("SPY", hub, spot=602.0,
                     levels=[{"price": 602.5, "kind": "call_wall"}],
                     price_series=[600, 601, 602, 602.4],
                     net_drift=4.2e6, net_flow=3.1e6, qflow_net=2.0e6,
                     drift_series=[1, 2, 3, 4, 5, 6])
    assert out["source_mode"] == "DERIVED"
    for key in ("gamma_pressure", "gamma_migration", "flow_confluence",
                "dominant_strikes", "persistence", "structural_state",
                "structural_score", "regime"):
        assert key in out, key
        if out[key].get("ready"):
            assert out[key]["source_mode"] == "DERIVED", key
    assert out["gamma_migration"]["ready"] is True
    assert "QD_INTERVAL_MAP" in out["gamma_migration"]["inputs"]


def test_break_containment_transition_are_all_reachable():
    from app.core import itmq_intelligence as IQ
    levels = [{"price": 100.0, "kind": "call_wall"}]
    # TRANSITION gana sobre BREAK: un break de un nivel que ya no existe es falso.
    t = IQ.structural_state("X", spot=100.0, levels=levels,
                            migration={"ready": True, "strength": 90.0},
                            price_series=[99, 100.5, 99.5])
    assert t["state"] == IQ.TRANSITION
    b = IQ.structural_state("X", spot=100.0, levels=levels,
                            migration={"ready": True, "strength": 5.0},
                            price_series=[99, 100.5, 99.8])
    assert b["state"] == IQ.BREAK
    c = IQ.structural_state("X", spot=100.5, levels=levels,
                            migration={"ready": False}, price_series=[100.4, 100.5])
    assert c["state"] == IQ.CONTAINMENT


def test_a_divergence_is_reported_not_averaged_away():
    from app.core import itmq_intelligence as IQ
    out = IQ.flow_confluence("X", net_drift=5e6, net_flow=4e6, dex=-9e6, qflow=3e6)
    assert out["ready"] is True
    assert "dex" in out["divergent"]
    assert out["reading"] == "DIVERGENCIA"
    assert out["unanimous"] is False


def test_a_missing_stream_weighs_zero_instead_of_counting_as_neutral():
    from app.core import itmq_intelligence as IQ
    full = IQ.flow_confluence("X", net_drift=5e6, net_flow=4e6, dex=2e6,
                              qflow=3e6, dark_pool=1e6)
    partial = IQ.flow_confluence("X", net_drift=5e6, net_flow=4e6)
    assert full["strength"] == pytest.approx(100.0)
    assert partial["strength"] == pytest.approx(100.0)
    assert partial["missing"] == ["dark_pool", "dex", "qflow"]


# ═══════════════════════════════════════════ 8 · LA PANTALLA NO ES UN PANEL TÉCNICO

def test_provenance_travels_to_the_auditor_never_to_the_main_screen():
    from app.terminal_api import build_terminal_bundle
    b = build_terminal_bundle(state={"symbol": "SPY", "ready": True, "spot": 600.0},
                              trace={"candles": [], "levels": []},
                              intelligence=_intel("SPY", 600.0, 1e9))
    assert b["auditor"]["visibility"] == "AUDITOR_ONLY_NEVER_MAIN_SCREEN"
    assert "plan" in b["auditor"] and "records" in b["auditor"]


def test_the_screen_does_not_print_provider_names_or_endpoints():
    """La pantalla principal muestra análisis, no diagnósticos del proveedor.

    Se comprueba sobre el marcado VISIBLE: un comentario HTML que documenta de qué
    endpoint sale un panel no llega nunca al usuario, y exigir que desaparezca
    empujaría a borrar justo la anotación que hace el código legible.
    """
    import re
    html = re.sub(r"<!--.*?-->", "", text("app/templates/terminal.html"), flags=re.S)
    for forbidden in ("/v1/options/tool/", "/v1/equities/tool/", "api.quantdata.us",
                      "QUANTDATA_", "PROVIDER_ERROR", "PARSER_ERROR"):
        assert forbidden not in html, forbidden
    trace_js = re.sub(r"/\*.*?\*/", "", text("app/static/itmq_trace.js"), flags=re.S)
    assert "/v1/options/tool/" not in trace_js


def test_trace_offers_the_four_greeks_of_the_dynamic_map():
    html = text("app/templates/terminal.html")
    for value in ('value="gamma"', 'value="delta"', 'value="vanna"', 'value="charm"'):
        assert value in html, value
    js = text("app/static/itmq_trace.js")
    assert "interval_maps" in js
    assert "INTERVAL_GREEKS" in js
    assert "drawQflowMarkers" in js and "drawQflowLevel" in js
