"""v1.41.4 · Cuota declarada, cobertura QuantData, Dark Pool y secciones nuevas.

Las cuatro secciones nuevas (MACRO, MONTE CARLO, EXPOSURE FORECAST, INTERVAL MAP)
no añaden matemática: leen lo que el motor ya calculaba y nunca se publicaba. Las
pruebas fijan ese contrato, porque un cambio de nombre de clave río arriba deja la
pantalla en blanco sin que nada falle.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture()
def quota(monkeypatch):
    """QuotaGuard limpio por prueba: el módulo mantiene una instancia global."""
    import app.providers.quantdata.shared as shared
    monkeypatch.delenv("QUANTDATA_PLAN_REQUESTS", raising=False)
    monkeypatch.delenv("QUANTDATA_PLAN_WINDOW_SECONDS", raising=False)
    return shared.QuotaGuard()


# ────────────────────────── cuota: presupuesto declarado y ventana aprendida

def test_declared_plan_sets_the_cadence(monkeypatch):
    """240 peticiones al día con 4 endpoints por ciclo son 1920 s entre ciclos.
    Pedir cada 15 s agota el plan en minutos y deja la sesión entera sin datos."""
    monkeypatch.setenv("QUANTDATA_PLAN_REQUESTS", "240")
    monkeypatch.setenv("QUANTDATA_PLAN_WINDOW_SECONDS", "86400")
    import app.providers.quantdata.shared as shared
    importlib.reload(shared)
    try:
        q = shared.QuotaGuard()
        interval = q.recommended_interval(4)
        assert interval == pytest.approx(4 / (240 / 86400 * 0.75), rel=0.02)
        # 4 peticiones cada `interval` segundos durante un día entero.
        assert (86400 / interval) * 4 <= 240
    finally:
        monkeypatch.delenv("QUANTDATA_PLAN_REQUESTS", raising=False)
        monkeypatch.delenv("QUANTDATA_PLAN_WINDOW_SECONDS", raising=False)
        importlib.reload(shared)


def test_declared_plan_beats_the_provider_headers(monkeypatch):
    """El plan contratado manda: la cabecera Reset a veces trae el tiempo hasta
    el próximo minuto y creerla convertía un plan diario en uno por minuto."""
    monkeypatch.setenv("QUANTDATA_PLAN_REQUESTS", "240")
    monkeypatch.setenv("QUANTDATA_PLAN_WINDOW_SECONDS", "86400")
    import app.providers.quantdata.shared as shared
    importlib.reload(shared)
    try:
        q = shared.QuotaGuard()
        q.note(remaining=200, limit=240, reset_seconds=30.0)
        assert q.recommended_interval(4) > 600.0
    finally:
        monkeypatch.delenv("QUANTDATA_PLAN_REQUESTS", raising=False)
        monkeypatch.delenv("QUANTDATA_PLAN_WINDOW_SECONDS", raising=False)
        importlib.reload(shared)


def test_window_is_learned_from_the_counter_resetting(quota):
    """Sin plan declarado, la ventana se mide viendo cuándo el contador sube."""
    import time as _t
    quota.note(remaining=100, limit=240, reset_seconds=None)
    quota.note(remaining=60, limit=240, reset_seconds=None)
    quota._last_reset_at = _t.time() - 3600.0      # el reinicio anterior fue hace una hora
    quota.note(remaining=240, limit=240, reset_seconds=None)
    assert quota.observed_window_s == pytest.approx(3600.0, rel=0.05)
    assert quota.window_samples == 1
    assert quota.snapshot()["window_source"] == "MEDIDA"


def test_a_small_dip_is_not_mistaken_for_a_reset(quota):
    """`remaining` puede subir en uno por una petición que el proveedor no contó.
    Tomarlo por un reinicio inventaría una ventana cortísima y quemaría el plan."""
    quota.note(remaining=100, limit=240, reset_seconds=None)
    quota.note(remaining=101, limit=240, reset_seconds=None)
    assert quota.observed_window_s is None
    assert quota.window_samples == 0


def test_without_any_telemetry_the_daily_window_is_assumed(quota):
    """Equivocarse por lento cuesta frescura; por rápido cuesta la sesión entera."""
    quota.note(remaining=None, limit=240, reset_seconds=None)
    assert quota.recommended_interval(4) > 900.0
    assert quota.snapshot()["window_source"] == "ASUMIDA_DIARIA"


def test_recommended_interval_is_bounded(quota):
    quota.note(remaining=1_000_000, limit=1_000_000, reset_seconds=1.0)
    assert quota.recommended_interval(4, floor_s=15.0) == 15.0
    quota2 = type(quota)()
    quota2.note(remaining=1, limit=1, reset_seconds=10_000_000.0)
    assert quota2.recommended_interval(4, ceil_s=3600.0) <= 3600.0


def test_snapshot_does_not_deadlock(quota):
    """recommended_interval() toma el mismo lock y threading.Lock no es reentrante:
    calcularlo dentro de la sección crítica colgaba el proceso entero."""
    import threading
    quota.note(remaining=120, limit=240, reset_seconds=600.0)
    done = threading.Event()
    threading.Thread(target=lambda: (quota.snapshot(), done.set()), daemon=True).start()
    assert done.wait(timeout=5.0), "snapshot() se quedó bloqueado sobre su propio lock"


def test_plan_variables_are_documented():
    env = text(".env.example")
    assert "QUANTDATA_PLAN_REQUESTS" in env
    assert "QUANTDATA_PLAN_WINDOW_SECONDS" in env


# ────────────────────────── cobertura por canal con QuantData

def _coverage(state="LIVE", age=20.0):
    return {
        "configured": True,
        "tools": [
            {"key": "gex_by_strike", "page": "Exposure", "state": state, "age_seconds": age},
            {"key": "dex_by_strike", "page": "Exposure", "state": state, "age_seconds": age},
            {"key": "net_flow", "page": "Flow Analysis", "state": state, "age_seconds": age},
            {"key": "oi_by_strike", "page": "Open Interest", "state": state, "age_seconds": age},
            {"key": "iv_rank", "page": "Volatility Analysis", "state": state, "age_seconds": age},
            {"key": "dark_pool_levels", "page": "Dark Pool / Equities", "state": state, "age_seconds": age},
        ],
    }


def test_quantdata_appears_in_the_channel_coverage():
    """Quant Data es REST de analítica de opciones: no publica quotes y por eso
    jamás salía en una tabla alimentada sólo por la fabric de PRECIO. Parecía que
    no participaba en ningún canal."""
    from app.core.provider_parity import parity_report
    rep = parity_report(alpaca_configured=True, tastytrade_status={}, quantdata_status={"configured": True},
                        consensus={}, options_coverage=_coverage())
    channels = {r["channel"] for r in rep["channels"] if r["provider"] == "QUANTDATA"}
    assert {"EXPOSURE", "OPTION_FLOW", "OPEN_INTEREST", "IMPLIED_VOLATILITY", "DARK_POOL"} <= channels


def test_channel_quality_reflects_live_tools_and_freshness():
    from app.core.provider_parity import parity_report
    fresh = parity_report(alpaca_configured=True, tastytrade_status={}, quantdata_status={"configured": True},
                          consensus={}, options_coverage=_coverage("LIVE", 10.0))
    stale = parity_report(alpaca_configured=True, tastytrade_status={}, quantdata_status={"configured": True},
                          consensus={}, options_coverage=_coverage("LIVE", 890.0))

    def q(rep):
        return next(r["quality"] for r in rep["channels"]
                    if r["provider"] == "QUANTDATA" and r["channel"] == "EXPOSURE")

    assert q(fresh) > q(stale) > 0.0


def test_dead_tools_do_not_claim_a_channel():
    from app.core.provider_parity import parity_report
    rep = parity_report(alpaca_configured=True, tastytrade_status={}, quantdata_status={"configured": True},
                        consensus={}, options_coverage=_coverage("NO_DISPONIBLE", 20.0))
    rows = [r for r in rep["channels"] if r["provider"] == "QUANTDATA"]
    assert rows and all(r["quality"] == 0.0 and r["selected"] is False for r in rows)


def test_coverage_is_optional_and_never_breaks_the_report():
    from app.core.provider_parity import parity_report
    rep = parity_report(alpaca_configured=True, tastytrade_status={}, quantdata_status={}, consensus={})
    assert rep["ready"] is not None and isinstance(rep["channels"], list)


def test_main_feeds_the_real_coverage_into_parity():
    src = text("app/main.py")
    assert "options_coverage=QUANTDATA_INTELLIGENCE.coverage()" in src


# ────────────────────────── dark pool

def test_dark_pool_publishes_the_real_zone_fields():
    """Cada zona publica lo que su fuente sabe, y deja vacío lo que no sabe.

    Hasta v1.42.6 `shares` se rellenaba desde una clave que las zonas propias nunca
    publican, así que la columna ACCIONES mostraba 0 en todas las filas —un 0 que se
    leía como "no hubo acciones" cuando en realidad era "no hay dato"—. Desde
    v1.42.7 la fuente primaria es `dark-pool-levels` de Quant Data, que SÍ publica
    ese campo; las zonas propias siguen sin tenerlo y viajan con None.
    """
    from app.terminal_api import _dark_pool
    state = {"spot": 100.0, "large_prints": {"off_exchange_liquidity_zones": {"zones": [
        {"price": 99.5, "low": 99.0, "high": 100.0, "notional": 4.2e6,
         "prints": 12, "concentration": 0.31, "type": "ACUMULACION", "side": "BUY", "score": 71.0},
    ]}}}
    dp = _dark_pool(state, {"candles": [], "option_prints": []}, {})
    lv = dp["levels"][0]
    assert lv["shares"] is None, "sin dato se publica None, nunca 0"
    assert lv["source"] == "ITM_QUANT_CONFIRMED_OFF_EXCHANGE"
    assert lv["low"] == 99.0 and lv["high"] == 100.0
    assert lv["concentration"] == 0.31 and lv["zone_type"] == "ACUMULACION"
    assert lv["distance_pct"] == pytest.approx(-0.5, abs=0.01)


def test_dark_pool_levels_take_their_shares_from_the_provider():
    """Cuando Quant Data entrega `dark-pool-levels`, manda él y trae ACCIONES."""
    from app.terminal_api import _dark_pool
    intel = {"dark_pool_levels": {"ready": True, "rows": [
        {"price": 99.5, "notional": 8.4e6, "shares": 84_000.0, "prints": 31},
    ]}}
    dp = _dark_pool({"spot": 100.0}, {"candles": [], "option_prints": []}, intel)
    lv = dp["levels"][0]
    assert lv["source"] == "QUANTDATA_DARK_POOL_LEVELS"
    assert lv["shares"] == 84_000.0
    assert dp["sources"]["levels"] == "QUANTDATA_DARK_POOL_LEVELS"


def test_dark_pool_table_matches_the_published_fields():
    html = text("app/templates/terminal.html")
    head = html[html.find('id="tblDarkPool"'):]
    head = head[:head.find("</thead>")]
    # ACCIONES dejó de ser una columna fantasma el día que una fuente real la llenó.
    assert "ACCIONES" in head
    for col in ("RANGO", "TIPO", "CONCENTRACIÓN", "ORIGEN"):
        assert col in head, col
    app = text("app/static/itmq_app.js")
    # Y se pinta sólo si hay número: nunca un 0 fabricado.
    assert "Q.isNum(sh) && sh > 0 ? Q.compact(sh, 0) : '—'" in app


def test_dark_pool_declares_its_price_and_print_coverage():
    """Un panel con velas de una ventana y prints de otra se ve 'raro' sin que
    nada falle: publicar los dos rangos hace visible el desajuste."""
    from app.terminal_api import _dark_pool
    dp = _dark_pool(
        {"spot": 100.0},
        {"candles": [{"t": "2026-09-17T13:30:00", "c": 100.0, "o": 100.0, "h": 100.0, "l": 100.0}],
         "option_prints": []},
        {},
    )
    assert "coverage" in dp and "candle_source" in dp


# ────────────────────────── MACRO

def _macro_state(**over):
    state = {
        "spot": 420.0,
        "volatility": {"atm_iv": 18.0},
        "meta": {"market_state": "REGULAR"},
        "scanner": {"direction": "ALCISTA"},
        "positioning": {"net_gex": 3.2e9},
        "flow": {"net": 1.4e6},
        "macro": {
            "updated_ec": "2026-09-17T09:00:00",
            "series": {
                "DGS2": {"label": "Treasury 2Y", "value": 3.81, "change_1": 0.02, "z": 0.4, "date": "2026-09-16",
                         "history": [{"date": "2026-09-15", "value": 3.79}, {"date": "2026-09-16", "value": 3.81}]},
                "DGS10": {"label": "Treasury 10Y", "value": 4.11, "z": 0.2, "date": "2026-09-16", "history": []},
                "DGS30": {"label": "Treasury 30Y", "value": 4.52, "z": 0.1, "date": "2026-09-16", "history": []},
                "T10Y2Y": {"value": 0.30, "date": "2026-09-16",
                           "history": [{"date": "2026-09-16", "value": 0.30}]},
                "BAMLH0A0HYM2": {"value": 3.12, "z": -0.4, "date": "2026-09-16",
                                 "history": [{"date": "2026-09-16", "value": 3.12}]},
            },
            "events": [{"source": "FED", "title": "FOMC Statement", "time_ny": "2026-09-17T14:00:00",
                        "time_ec": "2026-09-17T13:00:00", "impact": "HIGH"}],
            "stress": {"score": 44.0, "label": "MODERADO",
                       "next_high_event": {"title": "FOMC Statement", "time_ny": "2026-09-17T14:00:00",
                                           "source": "FED", "impact": "HIGH"},
                       "minutes_to_next_high": 95.0},
            "asset_context": {"score": 51.3, "regime": "PINNING",
                              "components": {"credit": 10.0, "rates": 30.0},
                              "weights": {"credit": 0.28, "rates": 0.18}},
        },
    }
    state.update(over)
    return state


def test_macro_publishes_rates_curve_credit_and_calendar():
    from app.terminal_api import _macro
    m = _macro(_macro_state())
    assert m["ready"] is True
    assert [r["id"] for r in m["rates"]] == ["DGS2", "DGS10", "DGS30"]
    assert m["curve"]["value"] == 0.30 and m["credit"]["value"] == 3.12
    assert m["events"][0]["title"] == "FOMC Statement"
    assert m["events"][0]["importance"] == "HIGH"
    assert m["stress_score"] == 44.0 and m["asset_score"] == 51.3


def test_macro_history_is_normalised_for_the_chart():
    """FRED publica {date, value}; los paneles dibujan {t, v}. Sin traducir, las
    series llegaban a la interfaz y no se veía ninguna línea."""
    from app.terminal_api import _macro
    m = _macro(_macro_state())
    hist = m["rates"][0]["history"]
    assert hist and set(hist[0]) == {"t", "v"}
    assert hist[-1]["v"] == 3.81


def test_macro_estimates_the_new_york_session_range_from_traded_iv():
    """El rango no es una opinión: es lo que la volatilidad implícita ATM ya está
    cobrando, escalada al tiempo que queda de sesión."""
    import math
    from app.terminal_api import _macro, SESSION_MINUTES, TRADING_DAYS
    m = _macro(_macro_state())
    se = m["session"]
    t = se["session_minutes_left"] / (TRADING_DAYS * SESSION_MINUTES)
    base = 420.0 * (18.0 / 100.0) * math.sqrt(t)
    # El estrés macro ensancha la varianza; nunca la estrecha ni inclina el signo.
    assert base <= se["expected_move"] <= base * 1.36
    assert se["low"] < 420.0 < se["high"]
    assert se["low_2s"] < se["low"] and se["high_2s"] > se["high"]


def test_session_range_is_absent_rather_than_invented_without_iv():
    from app.terminal_api import _macro
    m = _macro(_macro_state(volatility={}))
    assert m["session"]["low"] is None and m["session"]["expected_move"] is None


def test_session_bias_is_a_vote_of_measured_factors():
    from app.terminal_api import _macro
    m = _macro(_macro_state())
    se = m["session"]
    factors = {v["factor"] for v in se["votes"]}
    assert {"Scanner", "GEX neto", "Flujo neto", "Curva 10Y−2Y", "Crédito HY"} <= factors
    assert se["bias"] == "POSITIVO" and se["priority"] == "COMPRAS"


def test_net_gex_is_a_regime_not_a_direction():
    """Gamma positiva no significa que el mercado suba: significa que la cobertura
    va CONTRA el movimiento, sea cual sea su sentido. Hacerla votar dirección era
    un error de modelo, no de código."""
    from app.terminal_api import _macro
    se = _macro(_macro_state())["session"]
    gex = next(v for v in se["votes"] if v["factor"] == "GEX neto")
    assert gex["kind"] == "REGIMEN" and gex["sign"] == 0 and gex["weight"] == 0.0
    assert se["hedging_regime"] == "AMORTIGUA"          # net_gex positivo
    assert se["hedging_regime"] != se["bias"]
    negativa = _macro(_macro_state(positioning={"net_gex": -2.0e9}))["session"]
    assert negativa["hedging_regime"] == "AMPLIFICA"


def test_scanner_direction_is_recognised_in_both_languages():
    """El motor publica la dirección en español y en inglés según el módulo que la
    escriba. Reconocer sólo una forma dejaba a Scanner como NEUTRAL con una
    dirección declarada delante."""
    from app.terminal_api import _macro

    def scanner_sign(direction):
        se = _macro(_macro_state(scanner={"direction": direction}))["session"]
        return next(v for v in se["votes"] if v["factor"] == "Scanner")["sign"]

    for alcista in ("ALCISTA", "BUY", "LONG", "BULLISH", "COMPRA"):
        assert scanner_sign(alcista) == 1, alcista
    for bajista in ("BAJISTA", "SELL", "SHORT", "BEARISH", "VENTA"):
        assert scanner_sign(bajista) == -1, bajista
    assert scanner_sign("NEUTRAL") == 0


def test_a_single_factor_is_not_a_bias():
    """Con Scanner sin dirección y un solo factor con signo, declarar sesgo con
    'acuerdo 100%' es contar un dato como si fuera un consenso."""
    from app.terminal_api import _macro
    se = _macro({"spot": 420.0, "volatility": {"atm_iv": 18.0},
                 "scanner": {"direction": "NEUTRAL"}, "flow": {"net": -5.0e5}})["session"]
    assert se["bias_factors"] == 1
    assert se["bias"] == "MIXTO"


def test_session_bias_flips_with_the_evidence():
    from app.terminal_api import _macro
    m = _macro(_macro_state(scanner={"direction": "BAJISTA"}, positioning={"net_gex": -2.1e9},
                            flow={"net": -8.0e5}))
    assert m["session"]["bias"] == "NEGATIVO"
    assert m["session"]["priority"] == "VENTAS"


def test_session_without_agreement_does_not_pick_a_side():
    from app.terminal_api import _macro
    m = _macro(_macro_state(scanner={"direction": "NEUTRAL"}, positioning={"net_gex": 1.0e9},
                            flow={"net": -1.0e6}))
    assert m["session"]["bias"] == "MIXTO"
    assert "SIN PRIORIDAD" in m["session"]["priority"]


def test_macro_never_invents_an_analyst_consensus():
    """No hay fuente de consenso conectada: las columnas PREVIO/ESPERADO/REAL
    viajan vacías en lugar de rellenarse con una estimación plausible."""
    from app.terminal_api import _macro
    ev = _macro(_macro_state())["events"][0]
    assert ev["previous"] is None and ev["forecast"] is None and ev["actual"] is None
    note = _macro(_macro_state())["session"]["note"]
    assert "consenso de analistas" in note.lower()


def test_macro_degrades_with_a_reason_instead_of_a_blank_panel():
    from app.terminal_api import _macro
    assert _macro({})["reason"] == "MACRO_NO_CARGADO"
    assert _macro({"macro": {"status": "UNAVAILABLE_IN_CAUSAL_REPLAY"}})["ready"] is False


# ────────────────────────── el estrés macro sí llega al escenario

def test_asset_stress_reads_the_key_the_engine_publishes():
    """_asset_macro_context publica `score`. Se leía `stress_score`, que nunca
    existió: el Monte Carlo recibía None y no se ensanchaba jamás."""
    from app.service import _asset_stress
    assert _asset_stress({"asset_context": {"score": 72.4}}) == 72.4
    assert _asset_stress({"stress": {"score": 41.0}}) == 41.0
    assert _asset_stress({}) == 0.0
    assert _asset_stress(None) == 0.0


def test_scenario_lab_is_called_with_the_measured_stress():
    import re
    src = text("app/service.py")
    calls = re.findall(r"macro_stress=[^,)\n]+", src)
    assert calls and all(c.startswith("macro_stress=_asset_stress(") for c in calls), calls
    assert len(calls) == 2


def test_macro_stress_widens_the_distribution():
    from app.core.scenario_lab import build_scenario_lab
    calm = build_scenario_lab(symbol="DIA", spot=420.0, atm_iv_pct=18.0, macro_stress=0.0)
    tense = build_scenario_lab(symbol="DIA", spot=420.0, atm_iv_pct=18.0, macro_stress=100.0)
    assert tense["effective_iv_pct"] > calm["effective_iv_pct"]


# ────────────────────────── MONTE CARLO

def _lab():
    return {
        "ready": True, "state": "SHADOW", "authority": "NONE", "is_forecast": False,
        "engine": "STOCHASTIC_GENERATIVE_FALLBACK", "spot": 420.0, "atm_iv_pct": 18.0,
        "effective_iv_pct": 20.0, "horizon_minutes": 90, "path_count": 1500,
        "quantiles": {"p05": 414.0, "p16": 417.0, "p50": 420.1, "p84": 423.2, "p95": 426.0},
        "representative_paths": [[420.0, 420.4, 419.8]] * 40,
        "iv_scenarios": {"BASE": {"low": 417.0, "median": 420.0, "high": 423.0, "iv_pct": 18.0},
                         "IV_PLUS": {"low": 415.0, "median": 420.0, "high": 425.0, "iv_pct": 21.6},
                         "IV_MINUS": {"low": 418.0, "median": 420.0, "high": 422.0, "iv_pct": 14.4}},
        "model_risk": "SCENARIO GENERATOR · NOT A FORECAST",
    }


def test_montecarlo_surfaces_the_quantiles_the_engine_already_computed():
    from app.terminal_api import _montecarlo
    mc = _montecarlo({"scenario_lab": _lab(), "spot": 420.0})
    assert mc["ready"] and mc["path_count"] == 1500
    keys = [q["key"] for q in mc["quantiles"]]
    assert keys == ["p05", "p16", "p50", "p84", "p95"]
    p50 = next(q for q in mc["quantiles"] if q["key"] == "p50")
    assert p50["delta"] == pytest.approx(0.1, abs=1e-6)
    assert p50["delta_pct"] == pytest.approx(0.024, abs=0.002)


def test_montecarlo_keeps_its_shadow_authority_visible():
    """Es un generador de escenarios, no un pronóstico: la interfaz tiene que
    poder decirlo, así que la autoridad viaja con el payload."""
    from app.terminal_api import _montecarlo
    mc = _montecarlo({"scenario_lab": _lab()})
    assert mc["authority"] == "NONE" and mc["is_forecast"] is False
    assert "NOT A FORECAST" in mc["model_risk"]


def test_montecarlo_caps_the_paths_sent_to_the_browser():
    from app.terminal_api import _montecarlo
    mc = _montecarlo({"scenario_lab": _lab()})
    assert len(mc["paths"]) == 30


def test_iv_scenarios_are_ordered_from_low_to_high_vol():
    from app.terminal_api import _montecarlo
    mc = _montecarlo({"scenario_lab": _lab()})
    assert [r["name"] for r in mc["iv_scenarios"]] == ["IV_MINUS", "BASE", "IV_PLUS"]


def test_montecarlo_says_why_when_it_cannot_simulate():
    from app.terminal_api import _montecarlo
    mc = _montecarlo({"scenario_lab": {"ready": False, "reason": "SPOT_OR_IV_MISSING"}})
    assert mc["ready"] is False and mc["reason"] == "SPOT_OR_IV_MISSING"


# ────────────────────────── EXPOSURE FORECAST

def _scen():
    return {"ready": True, "structural_gex": 2.0e9, "flow_adjusted_gex_proxy": 2.1e9,
            "flow_gamma_adjustment_proxy": 1.0e8, "flow_events_used": 14,
            "sensitivity": [
                {"spot_shift_pct": -1.0, "spot": 415.8, "gex": -1.0e9, "change_vs_now": -3.0e9},
                {"spot_shift_pct": 0.0, "spot": 420.0, "gex": 2.0e9, "change_vs_now": 0.0},
                {"spot_shift_pct": 1.0, "spot": 424.2, "gex": 4.0e9, "change_vs_now": 2.0e9},
            ],
            "note": "Sensitivity reprices Gamma at shifted spot."}


def _trace_profile():
    return {"profiles": {"rows": [
        {"strike": 410.0, "gamma_m": 120.0}, {"strike": 418.0, "gamma_m": 300.0},
        {"strike": 422.0, "gamma_m": -80.0}, {"strike": 430.0, "gamma_m": 40.0},
    ]}}


def test_exposure_forecast_splits_gamma_around_each_scenario_price():
    """Lo que dice hacia dónde empuja la cobertura no es el neto, es el reparto:
    el dealer se apoya en la gamma que le queda debajo y vende contra la de arriba."""
    from app.terminal_api import _exposure_forecast
    ef = _exposure_forecast(_trace_profile(), {"exposure_scenarios": _scen(), "spot": 420.0})
    spot_row = next(s for s in ef["scenarios"] if s["label"] == "SPOT")
    assert spot_row["gex_below"] == pytest.approx((120.0 + 300.0) * 1e6)
    assert spot_row["gex_above"] == pytest.approx((-80.0 + 40.0) * 1e6)


def test_exposure_forecast_labels_the_hedging_regime():
    from app.terminal_api import _exposure_forecast
    ef = _exposure_forecast(_trace_profile(), {"exposure_scenarios": _scen(), "spot": 420.0})
    by = {s["label"]: s["regime"] for s in ef["scenarios"]}
    assert by["SPOT"] == "AMORTIGUA"        # gamma neta positiva frena el movimiento
    assert by["-1.00%"] == "AMPLIFICA"      # gamma neta negativa lo acelera


def test_exposure_forecast_projects_the_price_where_hedging_flips():
    from app.terminal_api import _exposure_forecast
    ef = _exposure_forecast(_trace_profile(), {"exposure_scenarios": _scen(), "spot": 420.0})
    # Interpolación lineal entre -1e9 en 415.8 y +2e9 en 420.0.
    assert ef["projected_flip"] == pytest.approx(415.8 + 4.2 * (1 / 3), abs=0.05)


def test_exposure_forecast_scenarios_are_ordered_by_shift():
    from app.terminal_api import _exposure_forecast
    ef = _exposure_forecast(_trace_profile(), {"exposure_scenarios": _scen(), "spot": 420.0})
    shifts = [s["delta_pct"] for s in ef["scenarios"]]
    assert shifts == sorted(shifts)


def test_exposure_forecast_without_a_profile_leaves_the_split_empty():
    """Sin perfil por strike el reparto no se puede calcular. Se publica vacío en
    vez de cero: un hueco es información, un cero es una afirmación falsa."""
    from app.terminal_api import _exposure_forecast
    ef = _exposure_forecast({}, {"exposure_scenarios": _scen(), "spot": 420.0})
    assert ef["ready"] is True
    assert all(s["gex_below"] is None and s["gex_above"] is None for s in ef["scenarios"])


def test_exposure_forecast_reports_why_it_is_empty():
    from app.terminal_api import _exposure_forecast
    ef = _exposure_forecast({}, {"exposure_scenarios": {"ready": False, "reason": "Sin exposición"}})
    assert ef["ready"] is False and ef["reason"] == "Sin exposición"


# ────────────────────────── INTERVAL MAP

def test_interval_map_normalises_a_list_of_cells():
    from app.terminal_api import _interval_map
    intel = {"options_heat_map": {"ready": True, "age_seconds": 31.0, "raw": {"data": [
        {"strike": 418.0, "time": "09:30", "gamma": 1.0},
        {"strike": 418.0, "time": "09:35", "gamma": 2.0},
        {"strike": 420.0, "time": "09:30", "gamma": -1.5},
    ]}}}
    im = _interval_map(intel)
    assert im["ready"] and im["strikes"] == [418.0, 420.0] and im["times"] == ["09:30", "09:35"]
    assert im["matrix"] == [[1.0, 2.0], [-1.5, 0.0]]


def test_interval_map_handles_series_nested_under_each_strike():
    """El proveedor cambia el envoltorio entre cuentas; fijar una ruta única
    dejaba la herramienta como NO DISPONIBLE con el dato delante."""
    from app.terminal_api import _interval_map
    intel = {"options_heat_map": {"ready": True, "raw": {"result": {"strikes": [
        {"strike": 415.0, "values": [{"t": "09:30", "value": 3.0}, {"t": "09:35", "value": 4.0}]},
        {"strike": 425.0, "values": [{"t": "09:35", "value": -2.0}]},
    ]}}}}
    im = _interval_map(intel)
    assert im["strikes"] == [415.0, 425.0]
    assert im["matrix"][1] == [0.0, -2.0]


def test_interval_map_is_trimmed_to_what_fits_on_screen():
    from app.terminal_api import _interval_map
    raw = {"data": [{"strike": float(k), "time": f"{i:04d}", "gamma": 1.0}
                    for k in range(200) for i in range(3)]}
    im = _interval_map({"options_heat_map": {"ready": True, "raw": raw}})
    assert len(im["strikes"]) == 90
    assert all(len(row) == len(im["times"]) for row in im["matrix"])


def test_interval_map_reports_an_unreadable_payload():
    from app.terminal_api import _interval_map
    assert "SIN HISTORIA" in _interval_map({})["reason"]
    im = _interval_map({"options_heat_map": {"ready": True, "raw": {"message": "ok"}}})
    assert im["ready"] is False and "CELDAS" in im["reason"]
    # La forma del payload viaja para poder corregir el normalizador sin adivinar.
    assert im["payload_keys"] == {"message": "str"}


# ────────────────────────── contrato del bundle y de la interfaz

def test_bundle_exposes_the_four_new_sections():
    from app.terminal_api import build_terminal_bundle
    b = build_terminal_bundle(state=_macro_state(scenario_lab=_lab(), exposure_scenarios=_scen()),
                              trace=_trace_profile(),
                              intelligence={}, parity={}, coverage={})
    for key in ("macro", "montecarlo", "exposure_forecast", "interval_map"):
        assert key in b, key
    assert b["macro"]["ready"] is True
    assert b["montecarlo"]["ready"] is True
    assert b["exposure_forecast"]["ready"] is True


def test_bundle_survives_a_completely_empty_state():
    """El arranque llega aquí antes de que el motor haya calculado nada: ninguna
    sección puede lanzar, todas deben decir por qué están vacías."""
    from app.terminal_api import build_terminal_bundle
    b = build_terminal_bundle(state={}, trace={}, intelligence={}, parity={}, coverage={})
    for key in ("macro", "montecarlo", "exposure_forecast", "interval_map"):
        assert b[key]["ready"] is False
        assert b[key].get("reason")


def test_every_new_section_has_a_view_and_a_nav_button():
    html = text("app/templates/terminal.html")
    for view in ("macro", "escenarios"):
        assert f'<button data-view="{view}">' in html, view
        assert f'<section class="view" data-view="{view}">' in html, view


def test_new_panels_are_rendered_on_every_tick():
    app = text("app/static/itmq_app.js")
    block = app[app.find("renderResumen(d);"):app.find("renderFuentes(d);")]
    assert "renderMacro(d);" in block and "renderEscenarios(d);" in block


def test_every_id_the_renderer_writes_exists_in_the_template():
    """Un id mal escrito no lanza: set() sale en silencio y el KPI se queda en —.
    Esta prueba es la única forma de verlo sin abrir el navegador."""
    import re
    app = text("app/static/itmq_app.js")
    html = text("app/templates/terminal.html")
    block = app[app.find("function renderMacro"):app.find("function renderFuentes")]
    ids = set(re.findall(r"(?:set|pill|chart|fillTable)\('([A-Za-z0-9_]+)'", block))
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"ids sin elemento en la plantilla: {missing}"


def test_the_heatmap_panel_exists_for_the_interval_map():
    panels = text("app/static/itmq_panels.js")
    assert "function heatmap(host, opts)" in panels
    assert "heatmap };" in panels or "heatmap }" in panels
    # Rejilla de puntos: el radio y la opacidad codifican la magnitud, el color el
    # signo. Un degradado continuo escondía la exposición concentrada en pocos strikes.
    assert "ctx.arc(" in panels and "Math.pow(a," in panels
    # Y el precio se superpone sobre los mismos intervalos.
    assert "price.length > 1" in panels


def test_duration_formatter_is_shared_not_reinvented():
    core = text("app/static/itmq_core.js")
    assert "function fmtMinutes(v)" in core
    assert "fmtMinutes," in core          # exportado


def test_exposure_forecast_explains_that_the_split_is_not_a_breakdown_of_the_net():
    """GEX DEBAJO + GEX ENCIMA no suma GEX NETO, y no debe: el neto reprecia la
    cadena entera y el reparto divide el perfil visible. Sin decirlo, las tres
    columnas se leen como un descuadre."""
    app = text("app/static/itmq_app.js")
    assert "set('efNote'" in app
    assert "no suman a él" in app
    assert 'id="efNote"' in text("app/templates/terminal.html")


def test_projected_flip_reaches_the_screen():
    app = text("app/static/itmq_app.js")
    assert "ef.projected_flip" in app
    assert "flip proyectado" in app
