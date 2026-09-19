"""v1.41.7 · Roster de proveedores, calendario de backtesting y auditoría numérica.

Tres cosas:

* El roster pasa a ser Alpaca + Quant Data. tastytrade no se muestra Y NO SE
  CONECTA: un DXLink que se reconectaba cada minuto llenaba el log y hacía parecer
  degradado un motor que no esperaba ese dato.
* Calendario de backtesting con NIVELES, no sólo fechas: un día del que sólo hay
  precio no puede medir un backtest de estructura de opciones.
* Cada sección nueva contrastada contra su propia definición matemática.
"""
from __future__ import annotations

import importlib
import math
import pathlib
from datetime import date

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture()
def roster(monkeypatch):
    """Recarga la política de roster con lo que la prueba declare."""
    import app.core.provider_parity as P

    def _set(value=None):
        if value is None:
            monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
        else:
            monkeypatch.setenv("ITM_OPTIONS_PEERS", value)
        importlib.reload(P)
        return P

    yield _set
    monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
    importlib.reload(P)


# ────────────────────────── roster de proveedores

def test_the_default_roster_is_alpaca_and_quantdata(roster):
    P = roster(None)
    assert P._configured_peers() == ("ALPACA", "QUANTDATA")
    assert P.peer_enabled("ALPACA") and P.peer_enabled("QUANTDATA")
    assert not P.peer_enabled("TASTYTRADE")


def test_the_roster_is_configurable_without_touching_code(roster):
    P = roster("ALPACA, TASTYTRADE , QUANTDATA")
    assert set(P._configured_peers()) == {"ALPACA", "TASTYTRADE", "QUANTDATA"}
    assert P.peer_enabled("tastytrade") is True


def test_a_mistyped_roster_never_leaves_the_terminal_without_providers(roster):
    """Una variable mal escrita no puede dejar la pantalla sin ninguna fuente."""
    for bad in ("basura", "", "   ", "FOO,BAR"):
        assert roster(bad)._configured_peers() == ("ALPACA", "QUANTDATA"), bad


def test_transport_names_resolve_to_their_provider(roster):
    P = roster("ALPACA,TASTYTRADE")
    assert P.peer_enabled("DXLINK") and P.peer_enabled("SIP") and P.peer_enabled("OPRA")


def test_a_retired_provider_disappears_from_the_report(roster):
    P = roster(None)
    rep = P.parity_report(alpaca_configured=True,
                          tastytrade_status={"configured": True, "dxlink": "CONNECTED"},
                          quantdata_status={"configured": True}, consensus={})
    assert {p["provider"] for p in rep["providers"]} == {"ALPACA", "QUANTDATA"}
    assert rep["roster"] == ["ALPACA", "QUANTDATA"]
    assert rep["roster_source"] == "POR_DEFECTO"


def test_a_retired_provider_is_not_reported_as_a_gap(roster):
    """Mostrarlo en rojo enseñaría a ignorar los avisos de verdad."""
    P = roster(None)
    rep = P.parity_report(alpaca_configured=True,
                          tastytrade_status={"configured": True, "dxlink": "DISCONNECTED",
                                             "last_error": "TimeoutError"},
                          quantdata_status={"configured": True}, consensus={})
    assert not any(p["provider"] == "TASTYTRADE" for p in rep["providers"])
    assert all(p["status"] != "DEGRADED" or p["provider"] != "TASTYTRADE" for p in rep["providers"])


def test_channels_of_a_retired_provider_are_filtered_too(roster):
    P = roster(None)
    rep = P.parity_report(
        alpaca_configured=True, tastytrade_status={}, quantdata_status={},
        consensus={"sources": [
            {"source": "ALPACA", "quality": {"channel_policy": "EQUITY_TRADE", "quality_score": 71.5}},
            {"source": "DXLINK", "quality": {"channel_policy": "OPTION_QUOTE", "quality_score": 60.0}},
        ]})
    assert "TASTYTRADE" not in {c["provider"] for c in rep["channels"]}


def test_a_retired_provider_is_not_even_connected(roster):
    """No basta con no mostrarlo. Un DXLink que se reconecta cada minuto consume
    hilos y llena el log de TimeoutError aunque nadie espere ese dato."""
    import app.providers.tastytrade.settings as S
    roster(None)
    importlib.reload(S)
    assert S._in_roster() is False


def test_the_roster_lets_it_back_in_without_code_changes(roster, monkeypatch):
    import app.providers.tastytrade.settings as S
    roster("ALPACA,TASTYTRADE,QUANTDATA")
    importlib.reload(S)
    assert S._in_roster() is True


# ────────────────────────── calendario de backtesting

def test_weekends_and_holidays_are_not_offered_as_sessions():
    """Un día de mercado cerrado no es un hueco de datos: ofrecerlo haría perder el
    tiempo al analista buscando una sesión que nunca existió."""
    from app.core.backtest_calendar import trading_days, market_holidays
    days = trading_days(date(2026, 12, 21), date(2026, 12, 28))
    assert all(d.weekday() < 5 for d in days)
    assert date(2026, 12, 25) not in days            # Navidad
    assert date(2026, 12, 26) not in days            # sábado


@pytest.mark.parametrize("year,expected", [
    (2026, {(1, 1), (1, 19), (2, 16), (5, 25), (7, 3), (9, 7), (11, 26), (12, 25)}),
])
def test_recurring_nyse_holidays_are_derived_not_hardcoded(year, expected):
    from app.core.backtest_calendar import market_holidays
    got = {(d.month, d.day) for d in market_holidays(year)}
    assert expected <= got, sorted(expected - got)


def test_good_friday_moves_with_easter():
    """Es el único festivo del calendario que no cae en fecha fija."""
    from app.core.backtest_calendar import market_holidays
    assert date(2026, 4, 3) in market_holidays(2026)
    assert date(2027, 3, 26) in market_holidays(2027)


def test_a_holiday_on_a_weekend_is_observed_on_the_adjacent_weekday():
    from app.core.backtest_calendar import market_holidays
    # 4 de julio de 2026 cae en sábado: se observa el viernes 3.
    assert date(2026, 7, 3) in market_holidays(2026)
    assert date(2026, 7, 4) not in market_holidays(2026)


def test_each_day_declares_what_it_can_actually_replay():
    """La distinción importa más que la lista: un backtest de estructura sobre un
    día del que sólo hay precio no mide lo que dice medir."""
    from app.core.backtest_calendar import build_calendar
    cal = build_calendar(pathlib.Path("/tmp"), "DIA", date(2026, 9, 14), date(2026, 9, 18),
                         archived=["2026-09-17"], cached_price=["2026-09-16"],
                         provider_configured=True, today=date(2026, 9, 18))
    by = {d["date"]: d for d in cal["days"]}
    assert by["2026-09-17"]["level"] == "CAUSAL" and by["2026-09-17"]["replays_structure"] is True
    assert by["2026-09-16"]["level"] == "PRECIO" and by["2026-09-16"]["replays_structure"] is False
    assert by["2026-09-16"]["hydrated"] is True
    assert by["2026-09-14"]["hydrated"] is False       # descargable, aún no descargado


def test_without_a_provider_unarchived_days_are_unavailable_not_promised():
    from app.core.backtest_calendar import build_calendar
    cal = build_calendar(pathlib.Path("/tmp"), "DIA", date(2026, 9, 14), date(2026, 9, 18),
                         archived=["2026-09-17"], provider_configured=False,
                         today=date(2026, 9, 18))
    levels = {d["date"]: d["level"] for d in cal["days"]}
    assert levels["2026-09-14"] == "NO_DISPONIBLE"
    assert levels["2026-09-17"] == "CAUSAL"


def test_the_calendar_never_offers_the_future():
    from app.core.backtest_calendar import build_calendar
    cal = build_calendar(pathlib.Path("/tmp"), "DIA", date(2026, 9, 14), date(2026, 12, 31),
                         today=date(2026, 9, 18))
    assert max(d["date"] for d in cal["days"]) <= "2026-09-18"


def test_an_invalid_range_reports_instead_of_raising():
    from app.core.backtest_calendar import build_calendar
    out = build_calendar(pathlib.Path("/tmp"), "DIA", "no-es-fecha", "tampoco")
    assert out["ready"] is False and out["days"] == []


def test_the_calendar_states_that_price_never_becomes_causal():
    """El OI y la IV por strike de una fecha pasada no se pueden recuperar de un
    endpoint de snapshot. Decirlo evita prometer una fidelidad imposible."""
    from app.core.backtest_calendar import build_calendar
    cal = build_calendar(pathlib.Path("/tmp"), "DIA", date(2026, 9, 17), date(2026, 9, 18),
                         today=date(2026, 9, 18))
    assert "nunca asciende a CAUSAL" in cal["note"]


def test_hydration_declares_that_it_brings_price_not_structure():
    src = text("app/main.py")
    block = src[src.find('@app.post("/api/backtest/hydrate")'):]
    block = block[:block.find("@app.get", 10)]
    assert '"replays_structure": False' in block
    assert "fetch_stock_session_bars" in block
    assert "FECHA EN EL FUTURO" in block


# ────────────────────────── modelo aprendido · disciplina visible

def _calib(**over):
    wf = {"stage": "CALIBRATED", "ready": True, "train_sessions": 30, "validation_sessions": 8,
          "purged_sessions": 4, "test_sessions": 10, "brier_model": 0.198, "brier_base_rate": 0.241,
          "log_loss_model": 0.58, "log_loss_base_rate": 0.67, "brier_skill_score": 0.178,
          "brier_skill_ci_low": 0.041, "brier_skill_ci_high": 0.302, "beats_base_rate": True,
          "statistical_promotion_pass": True, "final_oos_session_gate_pass": True,
          "minimum_promotion_sessions": 40, "minimum_final_oos_sessions": 8,
          "selected_calibrator": "isotonic"}
    wf.update(over)
    return {"calibration": {"sample_size": 420, "sessions": 52, "status": "CALIBRATED", "walk_forward": wf}}


def test_the_backtest_shows_its_out_of_sample_validation():
    """Un backtest que no enseña su validación es una promesa, no una medición."""
    from app.terminal_api import _backtest
    b = _backtest(_calib())
    assert b["promoted"] is True and b["gates_passed"] == b["gates_total"] == 5
    names = {g["gate"] for g in b["gates"]}
    assert {"Bloque fuera de muestra", "Purga entre bloques", "Intervalo bootstrap"} <= names


def test_improvement_is_measured_against_the_base_rate():
    from app.terminal_api import _backtest
    sc = _backtest(_calib())["scores"]
    assert sc["improvement_pct"] == pytest.approx((0.241 - 0.198) / 0.241 * 100, abs=0.01)


def test_a_model_worse_than_the_base_rate_shows_a_negative_improvement():
    """Peor que no modelar nada tiene que poder verse, no esconderse."""
    from app.terminal_api import _backtest
    sc = _backtest(_calib(brier_model=0.30, beats_base_rate=False))["scores"]
    assert sc["improvement_pct"] < 0


def test_an_unpromoted_model_decides_nothing():
    from app.terminal_api import _backtest
    b = _backtest(_calib(ready=False, eligible_for_activation=False,
                         beats_base_rate=False, statistical_promotion_pass=False))
    assert b["promoted"] is False and b["authority"] == "SHADOW"
    assert b["gates_passed"] < b["gates_total"]
    assert "no decide nada" in b["note"] or "SHADOW" in b["note"]


def test_missing_validation_is_unmeasured_not_failed():
    """Sin historial no hay puertas suspendidas: hay puertas sin medir. Marcarlas
    como falladas haría parecer roto un motor que sólo está acumulando."""
    from app.terminal_api import _backtest
    b = _backtest({"calibration": {"status": "COLLECTING"}})
    assert all(g["passed"] in (None, False) for g in b["gates"])
    assert any(g["passed"] is None for g in b["gates"])


def test_the_backtest_section_reaches_the_screen():
    html = text("app/templates/terminal.html")
    assert '<button data-view="backtest">' in html
    assert '<section class="view" data-view="backtest">' in html
    for el_id in ("btStage", "btImprove", "btGates", "btCalendar", "tblGates", "chartReliability"):
        assert f'id="{el_id}"' in html, el_id
    app = text("app/static/itmq_app.js")
    assert "renderBacktest(d);" in app and "function pullCalendar()" in app


def test_the_calendar_says_what_a_price_day_cannot_do_before_opening_it():
    app = text("app/static/itmq_app.js")
    block = app[app.find("async function calendarPick"):app.find("function bindCalendar")]
    assert "Reproduce precio, no estructura de opciones." in block


# ────────────────────────── auditoría numérica de las secciones nuevas

def test_the_session_range_is_the_traded_iv_scaled_to_the_remaining_time():
    from app.terminal_api import _session_outlook, SESSION_MINUTES, TRADING_DAYS
    se = _session_outlook({"spot": 420.0, "volatility": {"atm_iv": 18.0}}, {}, {})
    t = se["session_minutes_left"] / (TRADING_DAYS * SESSION_MINUTES)
    assert se["expected_move"] == pytest.approx(420.0 * 0.18 * math.sqrt(t), abs=1e-4)
    assert se["low"] == pytest.approx(420.0 - se["expected_move"], abs=1e-4)


def test_the_two_sigma_band_is_exactly_double_the_published_one():
    """Quien compare las dos cifras en pantalla tiene razón en esperar que cuadren."""
    from app.terminal_api import _session_outlook
    se = _session_outlook({"spot": 420.0, "volatility": {"atm_iv": 18.0}}, {}, {})
    assert se["high_2s"] - 420.0 == pytest.approx(2 * (se["high"] - 420.0), abs=1e-9)
    assert 420.0 - se["low_2s"] == pytest.approx(2 * (420.0 - se["low"]), abs=1e-9)


def test_macro_stress_widens_the_range_by_exactly_the_declared_factor():
    from app.terminal_api import _session_outlook
    st = {"spot": 420.0, "volatility": {"atm_iv": 18.0}}
    calm = _session_outlook(st, {}, {"score": 0.0})["expected_move"]
    tense = _session_outlook(st, {}, {"score": 100.0})["expected_move"]
    assert tense / calm == pytest.approx(1.35, abs=1e-3)


def test_the_projected_flip_is_the_zero_of_the_interpolated_line():
    from app.terminal_api import _exposure_forecast
    scen = {"ready": True, "sensitivity": [
        {"spot_shift_pct": -1.0, "spot": 415.8, "gex": -1e9},
        {"spot_shift_pct": 0.0, "spot": 420.0, "gex": 2e9}]}
    flip = _exposure_forecast({}, {"exposure_scenarios": scen, "spot": 420.0})["projected_flip"]
    # Evaluar la recta en el flip debe dar cero, salvo el redondeo del propio precio.
    gex_at = -1e9 + (2e9 - (-1e9)) * (flip - 415.8) / (420.0 - 415.8)
    assert abs(gex_at) / 1e9 < 1e-9


def test_interval_map_scales_millions_to_units_and_keeps_the_sign():
    from app.terminal_api import _interval_map
    im = _interval_map({}, {"heatmap_history": {
        "ready": True, "strikes": [530.0, 531.0], "times": ["09:30", "09:35"],
        "gamma_m": [[1.0, 2.0], [-0.5, 0.25]]}})
    assert im["matrix"][0][0] == pytest.approx(1e6)
    assert im["matrix"][1][0] == pytest.approx(-0.5e6)
    assert im["cells"] == 4


def test_iv_rank_and_percentile_match_their_definitions(tmp_path):
    import pandas as pd
    from app.core.session_memory import iv_rank_native, _path
    ivs = list(np.linspace(16.0, 24.0, 60))
    p = _path(tmp_path, "DIA"); p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"timestamp": (pd.Timestamp("2026-09-17 09:30") + pd.Timedelta(minutes=i)).isoformat(),
                   "symbol": "DIA", "expiry_mode": "AUTO", "atm_iv": v}
                  for i, v in enumerate(ivs)]).to_csv(p, index=False)
    r = iv_rank_native(tmp_path, "DIA", "AUTO", 20.0)
    assert r["rank"] == pytest.approx((20.0 - 16.0) / (24.0 - 16.0) * 100.0)
    assert r["percentile"] == pytest.approx(float(np.mean(np.array(ivs) <= 20.0)) * 100.0, abs=0.01)
    assert r["median"] == pytest.approx(float(np.median(ivs)))


# ────────────────────────── las barras GEX/DEX se mueven de verdad

def _profile_exposure(spot):
    from app.core.precision_engine import black_scholes_greeks_vector
    from app.core.expiry_clock import year_fraction_array
    strikes = np.arange(525.0, 545.5, 1.0)
    iv = np.full(len(strikes), 0.20)
    oi = np.linspace(500, 5000, len(strikes))
    is_call = np.array([i % 2 == 0 for i in range(len(strikes))])
    sign = np.where(is_call, 1.0, -1.0)
    S = np.full(len(strikes), float(spot))
    T = year_fraction_array(np.full(len(strikes), 0.6))
    g = black_scholes_greeks_vector(S, strikes, T, iv, is_call,
                                    np.full(len(strikes), 0.042), np.full(len(strikes), 0.013))
    return (sign * g["gamma"] * oi * 100.0 * (S ** 2) * 0.01,
            g["delta"] * oi * 100.0 * S, strikes)


def test_every_gex_and_dex_bar_moves_when_the_price_moves():
    """No se comprueba leyendo el código: se reprecian los mismos Greeks contra
    spots distintos y se mide cuánto cambia cada barra."""
    g0, d0, _ = _profile_exposure(534.20)
    g1, d1, _ = _profile_exposure(534.25)          # cinco céntimos
    assert np.all(np.abs(g1 - g0) > np.max(np.abs(g0)) * 1e-6)
    assert np.all(np.abs(d1 - d0) > np.max(np.abs(d0)) * 1e-6)


def test_a_single_tick_already_moves_the_exposure():
    """Si un tick no moviera nada, la pantalla se vería congelada con razón."""
    g0, _, _ = _profile_exposure(534.20)
    g1, _, _ = _profile_exposure(534.21)
    assert np.max(np.abs(g1 - g0)) / np.max(np.abs(g0)) > 1e-4


def test_the_gamma_center_travels_with_the_price():
    g0, _, k = _profile_exposure(534.20)
    g1, _, _ = _profile_exposure(536.20)
    center = lambda v: float(np.sum(k * np.abs(v)) / np.sum(np.abs(v)))
    assert center(g1) - center(g0) > 0.5


def test_a_tick_never_flips_the_sign_of_a_bar():
    """Un cambio de signo por mover el spot un céntimo indicaría inestabilidad
    numérica, no mercado."""
    g0, _, _ = _profile_exposure(534.20)
    g1, _, _ = _profile_exposure(534.21)
    assert np.array_equal(np.sign(g0), np.sign(g1))


def test_the_engine_publishes_the_live_drift_of_each_bar():
    src = text("app/core/trace_live.py")
    assert 'agg["gamma_change"] = agg["gamma"] - agg["gamma_base"]' in src
    trace = text("app/static/itmq_trace.js")
    assert "GEX_LIVE: {" in trace and "DEX_LIVE: {" in trace
