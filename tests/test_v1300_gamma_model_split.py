from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.trace_live import build_trace_pulse

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")


def test_release_identity_at_least_v1300():
    assert_version_at_least("1.30.0")
    assert_marker_version_at_least("1.30.0")


# --------------------------------------------------------------- fixtures

def _synthetic_chain(now: pd.Timestamp) -> pd.DataFrame:
    strikes = np.array([440.0, 441.0, 442.0, 443.0, 444.0, 445.0, 446.0])
    rows = []
    for k in strikes:
        for ot in ("call", "put"):
            rows.append(dict(
                timestamp=now, strike=k, iv=0.18, dte=5.0, open_interest=1000.0, volume=200.0,
                option_type=ot, underlying_price=443.5,
                signed_gex_proxy=0.0, option_delta_exposure_info=0.0,
            ))
    return pd.DataFrame(rows)


# ------------------------------------------------- build_trace_pulse() schema

def test_pulse_rows_carry_call_put_split_never_only_net():
    now = pd.Timestamp("2026-09-14 10:00:00")
    result = {"enriched": _synthetic_chain(now), "spot": 443.5}
    pulse = build_trace_pulse(result, "DIA", 443.5, None, asof=now, visual_window=12.0)
    assert pulse["ready"] is True
    assert pulse["rows"], "pulse must produce rows for a valid synthetic chain"
    required = {"gamma_m", "delta_m", "call_gamma_m", "put_gamma_m", "call_delta_m", "put_delta_m"}
    for row in pulse["rows"]:
        assert required.issubset(row.keys())


def test_call_gamma_is_never_negative_and_put_gamma_never_positive():
    """Same convention as signed_gex_proxy everywhere else: calls +, puts -."""
    now = pd.Timestamp("2026-09-14 10:00:00")
    result = {"enriched": _synthetic_chain(now), "spot": 443.5}
    pulse = build_trace_pulse(result, "DIA", 443.5, None, asof=now, visual_window=12.0)
    for row in pulse["rows"]:
        assert row["call_gamma_m"] >= -1e-9, row
        assert row["put_gamma_m"] <= 1e-9, row
        assert row["call_delta_m"] >= -1e-9, row
        assert row["put_delta_m"] <= 1e-9, row


def test_call_plus_put_gamma_reconstructs_net_gamma():
    """The split must never silently drop or double-count exposure vs the
    pre-existing net gamma_m/delta_m fields other panels already depend on."""
    now = pd.Timestamp("2026-09-14 10:00:00")
    result = {"enriched": _synthetic_chain(now), "spot": 443.5}
    pulse = build_trace_pulse(result, "DIA", 443.5, None, asof=now, visual_window=12.0)
    for row in pulse["rows"]:
        assert row["call_gamma_m"] + row["put_gamma_m"] == pytest.approx(row["gamma_m"], abs=1e-9)
        assert row["call_delta_m"] + row["put_delta_m"] == pytest.approx(row["delta_m"], abs=1e-9)


def test_empty_chain_never_raises():
    now = pd.Timestamp("2026-09-14 10:00:00")
    result = {"enriched": pd.DataFrame(), "spot": 443.5}
    pulse = build_trace_pulse(result, "DIA", 443.5, None, asof=now, visual_window=12.0)
    assert pulse["ready"] is False


# ------------------------------------------------------- frontend wiring checks

def test_nextgen_js_has_spotgamma_split_bars():
    assert "SPLIT_PROFILE_KEYS" in JS
    assert "call_gamma_m" in JS and "put_gamma_m" in JS
    assert "call_delta_m" in JS and "put_delta_m" in JS
    assert "splitProfilePair" in JS


def test_nextgen_js_renames_lanes_to_spotgamma_models():
    assert "GAMMA MODEL" in JS
    assert "DELTA MODEL" in JS
    assert "laneTitle" in JS
