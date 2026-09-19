from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def test_v1407_anomalies_has_no_feature_flag_or_off_collecting_ui():
    main = _text("app/main.py")
    env = _text(".env.example")
    html = _text("app/templates/dashboard.html")
    js = _text("app/static/return_anomalies.js")
    assert "ITM_SECCION_ANOMALIAS" not in main
    assert "ITM_SECCION_ANOMALIAS" not in env
    assert 'id="returnAnomalyLab" open' in html
    assert "OFF / COLLECTING" not in html
    assert "Activa ITM_SECCION_ANOMALIAS" not in html
    assert "ACTIVE / COLLECTING" in html
    assert "setWaiting" in js
    assert "safeText(el.summary,'OFF')" not in js


def test_v1407_stale_feed_warning_and_non_actionable_badge_are_removed_forever():
    app = _text("app/static/app.js")
    css = _text("app/static/app.css")
    html = _text("app/templates/dashboard.html")
    all_ui = "\n".join((app, css, html))
    assert "LIVE NO ACCIONABLE" not in all_ui
    assert "NO ACCIONABLE · CONTEXTO VISIBLE" not in all_ui
    assert "freshnessGateBanner" not in html
    # Internal fail-closed state is still computed; only the analyst-facing overlay is gone.
    assert "publicationBlocked" in app
    assert "old.remove()" in app
    assert "#freshnessGateBanner,.freshness-gate-banner{display:none!important}" in css


def test_v1407_scanner_uses_nonempty_current_when_current_delta_placeholder_is_empty():
    from app.core.scenario_engine import _scenario_zone_table

    cur = pd.DataFrame({
        "strike": [498., 499., 500., 501., 502.],
        "open_interest": [100, 500, 1000, 600, 200],
        "option_volume": [20, 100, 300, 120, 40],
        "gross_gex": [1e6, 3e6, 5e6, 4e6, 2e6],
        "signed_gex": [-1e6, -3e6, 1e6, 4e6, 2e6],
        "abs_delta_exposure": [2e6, 4e6, 8e6, 5e6, 2e6],
        "delta_exposure": [-1e6, -2e6, 3e6, 2e6, 1e6],
        "gex_change_pct": [0, .1, .2, .1, 0],
        "dominance_score": [30, 60, 85, 70, 40],
        "containment_score": [30, 50, 70, 55, 40],
        "break_score": [40, 45, 50, 60, 70],
        "containment_core": [30, 50, 70, 55, 40],
        "break_core": [40, 45, 50, 60, 70],
        "mass_pct": [.2, .5, .9, .6, .3],
        "intensity_pct": [.2, .5, .8, .6, .3],
        "turnover_pct": [.2, .5, .8, .6, .3],
        "net_tilt_pct": [.2, .5, .8, .6, .3],
        "net_tilt_ratio": [0, 0, 0, 0, 0],
        "turnover": [.1, .2, .3, .2, .1],
    })
    result = {
        "spot": 500., "current_delta": pd.DataFrame(), "current": cur,
        "pressure_direction": "UP", "pressure_score": 60,
        "delta_pressure_direction": "BUY", "delta_pressure_score": 55,
    }
    zones = _scenario_zone_table(
        result, {"regime": "BUY", "confidence": 50}, {"regime": "STABLE"}, {},
        pd.DataFrame(), pd.DataFrame(), None, {},
    )
    assert not zones.empty
    assert set(zones["strike"]) == {498., 499., 500., 501., 502.}


def test_v1407_0dte_trace_keeps_live_gex_and_dex_until_actual_expiry():
    from app.core.trace_live import build_trace_pulse

    ts = pd.Timestamp("2026-09-17 10:00:00")  # Ecuador local; 11:00 ET in September.
    rows = []
    for strike, typ, oi, vol in [
        (518, "call", 1000, 200), (519, "put", 800, 180),
        (520, "call", 1200, 240), (521, "put", 900, 190),
    ]:
        rows.append({
            "timestamp": ts, "underlying_price": 519.2, "strike": float(strike),
            "iv": .25, "dte": 0.0, "expiration_date": "2026-09-17",
            "option_type": typ, "open_interest": oi, "volume": vol,
            "signed_gex_proxy": 0.0, "option_delta_exposure_info": 0.0,
        })
    pulse = build_trace_pulse(
        {"enriched": pd.DataFrame(rows)}, "DIA", 519.3,
        asof=pd.Timestamp("2026-09-17 10:05:00"), visual_window=5,
    )
    assert pulse["ready"] is True
    assert len(pulse["rows"]) == 4
    assert np.isfinite(pulse["gamma_net_m"])
    assert np.isfinite(pulse["delta_net_m"])
    assert any(abs(float(r["gamma_m"])) > 0 for r in pulse["rows"])
    assert any(abs(float(r["delta_m"])) > 0 for r in pulse["rows"])


def test_v1407_structural_flow_shows_current_gex_dex_before_two_history_snapshots(tmp_path):
    from app.core.session_memory import structural_flow_frame

    cur = pd.DataFrame({
        "strike": [518., 519., 520.],
        "signed_gex": [10.0, -3.0, 5.0],
        "delta_exposure": [100.0, -40.0, 25.0],
    })
    out = structural_flow_frame(tmp_path, "DIA", "AUTO", current_gd={"current_delta": cur})
    assert out["ready"] is True
    assert out["status"] == "CURRENT_STRUCTURE_ONLY"
    assert out["current_gex"] == 12.0
    assert out["current_dex"] == 85.0


def test_v1407_unusual_flow_matches_reference_layout_and_forces_owned_plotly_renderer():
    html = _text("app/templates/dashboard.html")
    js = _text("app/static/market_line_terminal.js")
    css = _text("app/static/app.css")
    flow = html.split('id="section-flow"', 1)[1].split('id="section-netdrift"', 1)[0]
    assert 'id="flowProChart"' in flow
    assert "institutional-chart-rail" not in flow
    assert "Cómo leerlo" not in flow
    assert "flow-reference-terminal" in flow
    for token in ("'PRECIO'", "'AGRESOR'", "'TOTAL'", "'NET FLOW'"):
        assert token in js
    assert "id==='flowProChart'||id==='traceFlowProChart'" in js
    assert "cleanPlotlyFallback(id,spec)" in js
    assert "displayModeBar:!(id==='flowProChart'||id==='traceFlowProChart')" in js
    assert "flow-reference-terminal" in css
