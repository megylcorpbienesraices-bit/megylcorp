"""v1.27.19 · Final PRE-VPS truth/integrity contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.core.provider_flow_fabric import UnifiedPriceTickFabric
from app.core.volatility_surface import fit_surface
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]


def test_tasty_rest_missing_event_time_is_receive_proxy_and_presentation_only():
    src = (ROOT / "app/providers/tastytrade/runtime.py").read_text(encoding="utf-8")
    assert '"presentation_only":True' in src
    assert '"event_time_valid":_rest_has_event_time' in src
    assert '"event_time_source":"PROVIDER" if _rest_has_event_time else "RECEIVE_PROXY"' in src


def test_presentation_only_ticks_can_never_become_operational_canonical_lane():
    f = UnifiedPriceTickFabric(capacity_per_lane=5_000)
    now = pd.Timestamp.now(tz="UTC")
    assert f.ingest(source="TASTYTRADE_REST", symbol="DIA", timestamp=now, price=500.0,
                    event_type="EXTENDED_SNAPSHOT",
                    metadata={"presentation_only": True, "event_time_valid": False,
                              "event_time_source": "RECEIVE_PROXY"})
    assert f.canonical_source("DIA") is None
    assert f.dataframe("DIA").empty
    h = f.health("DIA")
    assert h["canonical_source"] is None and h["state"] in {"NO_DATA", "STALE"}

    assert f.ingest(source="ALPACA_SIP", symbol="DIA", timestamp=now, price=500.1,
                    event_type="TRADE", size=100,
                    metadata={"event_time_valid": True, "event_time_source": "EXCHANGE"})
    assert f.canonical_source("DIA") == "ALPACA_SIP"
    df = f.dataframe("DIA")
    assert len(df) == 1 and not bool(df.iloc[0].get("presentation_only", False))


def test_calibration_readiness_uses_same_conservative_default_everywhere():
    calibration = (ROOT / "app/core/calibration.py").read_text(encoding="utf-8")
    live = (ROOT / "app/core/live_validation.py").read_text(encoding="utf-8")
    assert 'ITM_CALIB_MIN_SESSIONS","40"' in calibration
    assert 'ITM_CALIB_MIN_SESSIONS","40"' in live


def test_fallback_default_carry_blocks_scanner_publication_authority():
    service = (ROOT / "app/service.py").read_text(encoding="utf-8")
    assert 'FALLBACK_DEFAULT_CARRY' in service
    assert 'MODEL_INPUTS_FALLBACK_CARRY' in service
    assert 'scanner["publication_blocked"] = True' in service


def test_svi_exposes_slice_and_global_calendar_readiness_separately():
    # One maturity is enough for a valid slice, but not enough to prove a global
    # multi-expiry calendar surface.  The two states must never be conflated.
    strikes = [90, 95, 100, 105, 110, 115, 120]
    frame = pd.DataFrame({
        "strike": strikes,
        "iv": [0.26, 0.23, 0.21, 0.215, 0.23, 0.255, 0.285],
        "dte": [30.0] * len(strikes),
        "underlying_price": [100.0] * len(strikes),
        "expiration_date": ["2026-10-13"] * len(strikes),
        "underlying_symbol": ["DIA"] * len(strikes),
    })
    rep = fit_surface(frame, spot=100.0, r=0.04, q=0.01, symbol="DIA")
    assert "slice_ready" in rep and "surface_operational_ready" in rep
    assert "surface_calendar_arb_free" in rep and "calendar_state" in rep
    if rep["slice_ready"] and rep["calendar_state"] == "COLLECTING":
        assert rep["surface_operational_ready"] is False


def test_dealer_heuristic_is_labeled_score_not_calibrated_probability():
    from app.core.dealer_intelligence import opening_closing_likelihood
    row = {"open_interest": 1000, "volume": 100, "contracts": 20, "aggressor_confidence": 0.7}
    heuristic = opening_closing_likelihood(row, model=None)
    assert heuristic["model_source"] == "HEURISTIC PRIOR"
    assert heuristic["opening_metric_type"] == "HEURISTIC_SCORE"
    assert heuristic["opening_probability_calibrated"] is False
    assert heuristic["opening_score"] == heuristic["opening_probability"]


def test_no_skips_are_hidden_as_release_policy():
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert "pytest.skip" not in conftest
    rescued = (ROOT / "tests/test_v12717_rescued_invariants.py").read_text(encoding="utf-8")
    assert "test_only_scanner_has_directional_authority" in rescued


def test_release_identity_is_single_and_at_least_v12719():
    assert_version_at_least("1.27.19")
    assert_marker_version_at_least("1.27.19")
    version = (ROOT / "VERSION.txt").read_text().strip()
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    manifest_path = ROOT / f"RELEASE_MANIFEST_v{version}.json"
    assert manifest_path.is_file(), f"manifest activo ausente para v{version}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == version == marker["version"]
    assert manifest["tests"]["skipped"] == 0
    assert manifest["release"] == marker["release"]


def test_descriptive_backtest_never_labels_unknown_cost_as_net(monkeypatch):
    from app.core import calibration as cal
    monkeypatch.delenv("ITM_BACKTEST_SLIPPAGE_BPS", raising=False)
    monkeypatch.delenv("ITM_BACKTEST_COST_BPS", raising=False)
    a = cal._cost_assumptions()
    assert a["bps_measured"] is False
    assert a["slippage_bps"] is None and a["roundtrip_cost_bps"] is None
    sample = pd.DataFrame({"resolved_move": [1.0, -0.5, 0.25], "spot": [500.0, 500.0, 500.0]})
    out = cal._cost_adjust(sample, a)
    assert out["gross_expectancy"] is not None
    assert out["net_expectancy"] is None
    assert out["net_profit_factor"] is None
    assert out["reason"] == "EXECUTION_BPS_UNAVAILABLE"


def test_vps_env_example_uses_conservative_release_defaults():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "APP_NAME=ITM QUANT MULTI ASSET" in env
    assert "ITM_CALIB_MIN_SESSIONS=40" in env
    assert "ITM_CALIB_SCOPE_MIN_SESSIONS=20" in env
    assert "ITM_BACKTEST_SLIPPAGE_BPS=\n" in env
    assert "ITM_BACKTEST_COST_BPS=\n" in env
    assert "ITM_ENV=production" in env and "ITM_PRODUCTION=1" in env
    assert "ITM_REQUIRE_TOKEN=1" in env
