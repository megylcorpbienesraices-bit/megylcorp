from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.monte_carlo import simulate_gbm_paths, monte_carlo_level_report

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
JS = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
SERVICE = (ROOT / "app/service.py").read_text(encoding="utf-8")


def test_release_identity_at_least_v1360():
    assert_version_at_least("1.36.0")
    assert_marker_version_at_least("1.36.0")


# --------------------------------------------------------------- simulate_gbm_paths

def test_gbm_paths_shape_and_start_at_spot():
    paths = simulate_gbm_paths(100.0, 0.045, 0.017, 0.20, 5.0, n_sims=500, seed=1)
    assert paths.shape[0] == 500
    assert np.allclose(paths[:, 0], 100.0)


def test_gbm_paths_are_positive_and_finite():
    paths = simulate_gbm_paths(100.0, 0.045, 0.017, 0.35, 30.0, n_sims=1000, seed=2)
    assert np.all(np.isfinite(paths))
    assert np.all(paths > 0)


def test_gbm_terminal_mean_matches_risk_neutral_drift_within_monte_carlo_error():
    """E[S_T] under risk-neutral GBM = S0 * exp((r-q)*T). With enough sims and a
    fixed seed this must hold within a generous Monte Carlo error tolerance --
    this is the standard correctness check for a risk-neutral simulator."""
    import math
    from app.core.expiry_clock import year_fraction
    spot, r, q, sigma, dte = 100.0, 0.05, 0.01, 0.25, 30.0
    paths = simulate_gbm_paths(spot, r, q, sigma, dte, n_sims=200_000, seed=7)
    terminal = paths[:, -1]
    expected = spot * math.exp((r - q) * year_fraction(dte))
    assert float(terminal.mean()) == pytest.approx(expected, rel=0.02)


# ------------------------------------------------------- monte_carlo_level_report

def _sample_levels():
    return {"zero_gamma": 101.0, "call_wall": 102.0, "put_wall": 98.0, "vol_trigger": 97.0, "max_pain": 100.0}


def test_monte_carlo_report_ready_and_schema():
    report = monte_carlo_level_report(100.0, 0.045, 0.017, 20.0, 5.0, _sample_levels(), n_sims=5000, seed=3)
    assert report["ready"] is True
    assert set(report["levels"].keys()) == set(_sample_levels().keys())
    for lv in report["levels"].values():
        assert 0.0 <= lv["prob_finish_beyond_pct"] <= 100.0
        assert 0.0 <= lv["prob_touch_by_expiry_pct"] <= 100.0


def test_monte_carlo_touch_probability_never_below_finish_probability():
    """Touching a level at any point up to expiry is a weaker condition than
    finishing beyond it -- touch probability must always be >= finish probability
    for the same side."""
    report = monte_carlo_level_report(100.0, 0.045, 0.017, 25.0, 10.0, _sample_levels(), n_sims=8000, seed=4)
    for lv in report["levels"].values():
        assert lv["prob_touch_by_expiry_pct"] >= lv["prob_finish_beyond_pct"] - 1e-9


def test_monte_carlo_higher_vol_increases_touch_probability_of_a_fixed_level():
    low_vol = monte_carlo_level_report(100.0, 0.045, 0.017, 10.0, 20.0, {"call_wall": 105.0}, n_sims=8000, seed=5)
    high_vol = monte_carlo_level_report(100.0, 0.045, 0.017, 40.0, 20.0, {"call_wall": 105.0}, n_sims=8000, seed=5)
    assert high_vol["levels"]["call_wall"]["prob_touch_by_expiry_pct"] > low_vol["levels"]["call_wall"]["prob_touch_by_expiry_pct"]


def test_monte_carlo_degrades_without_raising_on_bad_inputs():
    assert monte_carlo_level_report(0.0, 0.045, 0.017, 20.0, 5.0, {})["ready"] is False
    assert monte_carlo_level_report(100.0, 0.045, 0.017, 20.0, 0.0, {})["ready"] is False
    assert monte_carlo_level_report(100.0, 0.045, 0.017, 20.0, 5.0, {"bad": "x"})["ready"] is True


def test_monte_carlo_cone_widens_with_horizon():
    """A GBM cone's spread must grow with sqrt(time); the p90-p10 gap at the end of
    the horizon must exceed the gap near day 0."""
    report = monte_carlo_level_report(100.0, 0.045, 0.017, 25.0, 20.0, {}, n_sims=8000, seed=6)
    cone = report["cone_percentiles"]
    early_spread = cone[90][1] - cone[10][1]
    late_spread = cone[90][-1] - cone[10][-1]
    assert late_spread > early_spread


# ------------------------------------------------------- backend wiring checks

def test_service_has_equity_hub_functions():
    for name in ("_equity_hub_gamma_model_figure", "_monte_carlo_current", "_monte_carlo_figure", "need_equity_hub"):
        assert name in SERVICE, name


def test_public_state_exposes_gamma_regime_for_equity_hub():
    assert '"gamma_regime"' in SERVICE


# ------------------------------------------------------- frontend wiring checks

def test_dashboard_has_equity_hub_section_and_nav():
    assert 'data-section="equityhub"' in HTML
    assert 'id="section-equityhub"' in HTML
    for hero_id in ("ehSpot", "ehZeroGamma", "ehCallWall", "ehPutWall", "ehVolTrigger",
                   "ehMaxPain", "ehExpectedMove", "ehAtmIv", "ehSkew", "ehRegime", "ehSqueeze", "ehScanner"):
        assert f'id="{hero_id}"' in HTML
    assert 'id="equityHubGammaModel"' in HTML
    assert 'id="equityHubMonteCarlo"' in HTML
    assert 'id="ehMonteCarloLevels"' in HTML


def test_app_js_wires_equity_hub_charts_and_render():
    assert "'section-equityhub':'equity_hub'" in JS
    assert "equity_hub_gamma_model" in JS
    assert "equity_hub_monte_carlo" in JS
    assert "function renderEquityHub" in JS
    assert "function renderMonteCarloLevels" in JS
    assert "'section-equityhub'" in JS  # periodic refresh list
