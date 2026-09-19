"""v1.14.3 · expected-value gate, probability calibration and options P&L."""
import math
import os

import numpy as np
import pandas as pd

from app.core.calibration import (probability_calibration, expected_value,
                                  _pava, _isotonic_predict, _eval_signal)
from app.core.scale_anchors import update_anchors, load_anchors, anchored_magnitude


def _synthetic_signals(n=1400, seed=3):
    rng = np.random.default_rng(seed)
    sessions = pd.date_range("2026-06-01", periods=45, freq="B").strftime("%Y-%m-%d").tolist()
    ev = rng.uniform(35, 95, n)
    p = np.clip(0.18 + 0.55 / (1 + np.exp(-(ev - 70) / 7)), 0, 1)
    ok = rng.random(n) < p
    return sessions, pd.DataFrame({"horizon": 10, "evidence_score": ev,
                                   "outcome": np.where(ok, "T1", "INVALIDATION"),
                                   "session_date": rng.choice(sessions, n)})


def test_isotonic_is_monotone_and_pooled():
    x = np.array([1., 2., 3., 4., 5.]); y = np.array([0., 1., 0., 1., 1.])
    kx, ky = _pava(x, y, np.ones_like(y))
    assert np.all(np.diff(ky) >= -1e-9)
    p = _isotonic_predict(kx, ky, x)
    assert np.all(np.diff(p) >= -1e-9)


def test_probability_calibration_beats_base_rate_and_is_honest_when_it_does_not():
    sessions, frame = _synthetic_signals()
    m = probability_calibration(frame, sessions, 10)
    assert m["ready"] and m["beats_base_rate"]
    assert m["brier_model"] < m["brier_base_rate"]

    # Pure noise: evidence carries nothing. The fit must NOT be adopted.
    rng = np.random.default_rng(5)
    noise = frame.copy()
    noise["outcome"] = np.where(rng.random(len(noise)) < 0.4, "T1", "INVALIDATION")
    noise["evidence_score"] = rng.uniform(35, 95, len(noise))
    m2 = probability_calibration(noise, sessions, 10)
    assert (not m2["ready"]) or m2["brier_model"] < m2["brier_base_rate"]


def test_expected_value_ranks_by_edge_not_by_evidence():
    """The case the old gate got wrong."""
    high_ev_low_rr = expected_value(0.54, 0.4, 0.0)      # evidence alta, R/R pobre
    low_ev_high_rr = expected_value(0.45, 2.5, 0.0)      # evidence menor, R/R buena
    assert high_ev_low_rr["ev_r"] < 0
    assert low_ev_high_rr["ev_r"] > 0
    assert math.isclose(expected_value(0.5, 1.0, 0.0)["breakeven_probability"], 0.5, rel_tol=1e-9)
    assert expected_value(0.9, float("nan"), 0.0)["ev_r"] is None


def test_options_mode_measures_premium_not_underlying_move():
    t0 = pd.Timestamp("2026-09-04 14:00:00")
    px = pd.DataFrame({"timestamp": [t0 + pd.Timedelta(minutes=i) for i in range(21)],
                       "price": [600 + 0.06 * i for i in range(21)]})
    r = pd.Series({"timestamp": t0, "direction": "BUY", "spot": 600.0, "target1": 601.0,
                   "target2": 602.0, "invalidation": 599.4, "zone_center": 600.0,
                   "symbol": "SPY", "atm_iv_decimal": 0.22, "signal_dte": 0.30})
    prev = os.environ.get("ITM_INSTRUMENT_MODE")
    try:
        os.environ["ITM_INSTRUMENT_MODE"] = "underlying"
        under = _eval_signal(r, px, 20)
        os.environ["ITM_INSTRUMENT_MODE"] = "options"
        opts = _eval_signal(r, px, 20)
    finally:
        if prev is None: os.environ.pop("ITM_INSTRUMENT_MODE", None)
        else: os.environ["ITM_INSTRUMENT_MODE"] = prev
    assert opts["option_ready"]
    # Theta plus the round-trip spread must make the option R strictly worse than the
    # stock R on the same favourable path. If it is not, the simulation is wrong.
    assert opts["r_multiple"] < under["r_multiple"]
    assert opts["option_type"] == "call"


def test_options_mode_refuses_to_invent_missing_inputs():
    t0 = pd.Timestamp("2026-09-04 14:00:00")
    px = pd.DataFrame({"timestamp": [t0 + pd.Timedelta(minutes=i) for i in range(6)],
                       "price": [600 + 0.1 * i for i in range(6)]})
    r = pd.Series({"timestamp": t0, "direction": "BUY", "spot": 600.0, "target1": 601.0,
                   "target2": 602.0, "invalidation": 599.4})   # sin atm_iv / dte
    prev = os.environ.get("ITM_INSTRUMENT_MODE")
    os.environ["ITM_INSTRUMENT_MODE"] = "options"
    try:
        out = _eval_signal(r, px, 5)
    finally:
        if prev is None: os.environ.pop("ITM_INSTRUMENT_MODE", None)
        else: os.environ["ITM_INSTRUMENT_MODE"] = prev
    assert out["option_ready"] is False and out["option_reason"]


def test_historical_anchor_actually_preserves_physical_magnitude(tmp_path):
    base = np.random.default_rng(2).lognormal(6.5, 1.2, 200)
    for _ in range(5):
        update_anchors(tmp_path, "SPY", {"open_interest": base.tolist()})
    a = load_anchors(tmp_path, "SPY")["open_interest"]
    small = float(np.mean(anchored_magnitude(base * 0.1, a)))
    same = float(np.mean(anchored_magnitude(base, a)))
    big = float(np.mean(anchored_magnitude(base * 10.0, a)))
    assert small < same < big, "el ancla histórica debe responder al tamaño físico"


def test_anchor_is_ignored_until_enough_sessions(tmp_path):
    base = np.random.default_rng(4).lognormal(6.0, 1.0, 100)
    update_anchors(tmp_path, "QQQ", {"open_interest": base.tolist()})
    a = load_anchors(tmp_path, "QQQ")["open_interest"]
    assert anchored_magnitude(base, a) is None   # n=1 < mínimo de sesiones
