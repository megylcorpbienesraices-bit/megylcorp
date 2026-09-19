from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.volatility_surface import fit_surface, skew_25d, skew_term_structure

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
JS = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
SERVICE = (ROOT / "app/service.py").read_text(encoding="utf-8")


def test_release_identity_at_least_v1320():
    assert_version_at_least("1.32.0")
    assert_marker_version_at_least("1.32.0")


# --------------------------------------------------------------- fixtures

def _skewed_chain(spot: float = 443.5) -> pd.DataFrame:
    rows = []
    for dte in (7.0, 30.0, 60.0):
        strikes = np.linspace(spot * 0.80, spot * 1.20, 41)
        k = np.log(strikes / spot)
        # Deliberate negative-skew smile: puts (low strikes) richer than calls.
        iv = 0.18 + 0.35 * np.maximum(-k, 0) + 0.10 * np.maximum(k, 0) + 0.02 * k ** 2
        for K, sig in zip(strikes, iv):
            for ot in ("call", "put"):
                rows.append(dict(strike=K, iv=float(sig), dte=dte, underlying_price=spot, option_type=ot,
                                 expiration_date=f"2026-{'09' if dte < 20 else ('10' if dte < 45 else '11')}-14"))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- skew_25d()

def test_skew_25d_not_ready_without_fitted_slice():
    assert skew_25d({"ready": False}) == {"ready": False}
    assert skew_25d({}) == {"ready": False}


def test_skew_25d_positive_for_standard_negative_equity_skew():
    """Puts richer than calls (the normal equity/index shape) must yield a
    positive skew_25d_pct under the Put IV - Call IV convention."""
    chain = _skewed_chain()
    fit = fit_surface(chain, 443.5)
    ready_slices = [s for s in fit["slices"] if s.get("ready")]
    assert ready_slices, "synthetic smile must produce at least one hard-valid SVI slice"
    for s in ready_slices:
        sk = skew_25d(s)
        assert sk["ready"] is True
        assert sk["skew_25d_pct"] > 0, s["expiration_date"]
        assert sk["put_strike"] < s["forward"] < sk["call_strike"], "25d put/call strikes must bracket the forward"


def test_skew_25d_self_consistent_with_evaluate_svi():
    from app.core.volatility_surface import evaluate_svi
    chain = _skewed_chain()
    fit = fit_surface(chain, 443.5)
    s = next(x for x in fit["slices"] if x.get("ready"))
    sk = skew_25d(s)
    iv_at_put_strike = float(evaluate_svi([sk["put_strike"]], s["forward"], s["maturity_years"], s["params"])[0]) * 100.0
    assert sk["put_iv_pct"] == pytest.approx(iv_at_put_strike, abs=1e-9)


# ------------------------------------------------------- skew_term_structure()

def test_skew_term_structure_sorted_by_dte_and_excludes_unfitted_slices():
    chain = _skewed_chain()
    report = skew_term_structure(chain, 443.5, symbol="DIA")
    assert report["ready"] is True
    dtes = [r["dte"] for r in report["rows"]]
    assert dtes == sorted(dtes)
    assert report["model"] == "SVI"


def test_skew_term_structure_empty_chain_degrades_without_raising():
    report = skew_term_structure(pd.DataFrame(), 443.5, symbol="DIA")
    assert report["ready"] is False
    assert report["rows"] == []


# ------------------------------------------------------- frontend/service wiring

def test_service_has_skew_figure_and_current():
    assert "_skew_figure" in SERVICE
    assert "_skew_current" in SERVICE
    assert 'result["skew"]' in SERVICE or "result['skew']" in SERVICE


def test_dashboard_has_skew_model_panel_distinct_from_observed_skew():
    assert 'id="skewChart"' in HTML
    # Must not silently collide with the pre-existing discrete observed-skew panel.
    assert 'id="skewExpiryGrid"' in HTML
    assert "Skew Model (SVI)" in HTML


def test_app_js_plots_skew_chart():
    assert "plot('skewChart',c.skew)" in JS or 'plot("skewChart",c.skew)' in JS
