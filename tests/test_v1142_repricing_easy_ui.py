from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from app.core.dealer_intelligence import SyntheticInventoryBook, gex_reconciliation
from app.service import _volatility_metrics


def test_dealer_inventory_greeks_are_repriced_to_live_spot(tmp_path):
    db = tmp_path / "dealer.sqlite"
    book = SyntheticInventoryBook(db)
    now = pd.Timestamp("2026-09-06T14:30:00Z")
    with sqlite3.connect(db) as c:
        c.execute(
            """INSERT INTO dealer_inventory(symbol,contract_symbol,strike,expiration_date,option_type,
               dealer_contracts,last_delta,last_gamma,last_vanna,last_charm,last_spot,confidence,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("DIA", "DIA_TEST_C100", 100.0, "2026-09-06", "call", 10.0,
             0.5, 0.50, 0.0, 0.0, 100.0, 80.0, now.isoformat()),
        )
    chain = pd.DataFrame({
        "contract_symbol": ["DIA_TEST_C100"],
        "strike": [100.0],
        "expiration_date": ["2026-09-06"],
        "option_type": ["call"],
        "iv": [0.20],
        "dte": [0.25],
    })
    snap = book.snapshot("DIA", 101.0, chain)
    assert not snap.empty
    assert snap.loc[0, "greeks_valuation"] == "REPRICED AT LIVE SPOT"
    assert snap.loc[0, "greeks_repriced_pct"] == 100.0
    # Stored gamma 0.50 is intentionally unrealistic. Live repricing must replace it.
    assert abs(float(snap.loc[0, "model_gamma"]) - 0.50) > 0.05
    assert np.isfinite(float(snap.loc[0, "dealer_gex"]))


def test_gex_reconciliation_keeps_models_separate():
    chain = pd.DataFrame({
        "contract_symbol": ["a", "b"],
        "signed_gex_proxy": [2_000_000.0, 1_000_000.0],
    })
    r = gex_reconciliation(chain, dealer_gex=-900_000.0, dealer_confidence=62.0, inventory_contracts=1)
    assert r["structural_gex"] == 3_000_000.0
    assert r["estimated_dealer_gex"] == -900_000.0
    assert r["agreement"] == "CONFLICT"
    assert r["flow_inventory_coverage_pct"] == 50.0
    assert "distintos" in r["note"]



def test_gex_reconciliation_can_value_raw_chain_without_precomputed_gex():
    chain = pd.DataFrame({
        "underlying_price": [100.0, 100.0],
        "strike": [100.0, 100.0], "dte": [1.0, 1.0],
        "iv": [0.20, 0.20], "option_type": ["call", "put"],
        "open_interest": [1200.0, 800.0],
    })
    r = gex_reconciliation(chain, dealer_gex=0.0, dealer_confidence=0.0, inventory_contracts=0, symbol="DIA")
    assert r["structural_gex"] is not None
    assert np.isfinite(r["structural_gex"])


def test_volatility_uses_parkinson_and_atm_forward_term_structure():
    # 80 one-minute bars with small ranges are enough for the new RV gate.
    ts = pd.date_range("2026-09-04 14:30:00", periods=80, freq="min", tz="UTC")
    base = 100 + np.linspace(0, 0.6, len(ts))
    bars = pd.DataFrame({
        "timestamp": ts,
        "open": base,
        "high": base * 1.0015,
        "low": base * 0.9985,
        "close": base * 1.0003,
    })
    rows = []
    for dte, exp, atm_iv in [(1.0, "2026-09-07", 0.20), (8.0, "2026-09-14", 0.24)]:
        for strike, wing in [(90, 0.08), (95, 0.03), (100, 0.0), (105, 0.03), (110, 0.08)]:
            for typ, delta in [("call", 0.5 if strike == 100 else 0.25), ("put", -0.5 if strike == 100 else -0.25)]:
                rows.append({
                    "timestamp": ts[-1], "underlying_price": 100.0,
                    "expiration_date": exp, "dte": dte, "strike": strike,
                    "option_type": typ, "iv": atm_iv + wing,
                    "calc_delta": delta, "bid": 1.0, "ask": 1.2,
                })
    enr = pd.DataFrame(rows)
    out = _volatility_metrics({"enriched": enr, "spot": 100.0, "symbol": "DIA"}, bars)
    assert str(out["realized_vol_method"]).startswith("PARKINSON")
    assert out["realized_vol_samples"] >= 60
    assert len(out["term_structure"]) == 2
    assert all("mean_iv_all_strikes" in r for r in out["term_structure"])
    # ATM-forward should be below the all-strike mean because wings are richer in this fixture.
    assert all(r["iv"] < r["mean_iv_all_strikes"] for r in out["term_structure"])


def test_easy_interface_preserves_all_sections():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app/templates/dashboard.html").read_text(encoding="utf-8")
    js = (root / "app/static/app.js").read_text(encoding="utf-8")
    # v1.15.8 intentionally merges Net Positioning + Volumen Pro into Positioning & Volumen.
    for section in ["command","premarket","scanner","trace","dealer","chain","flow","netdrift","exposure","gexmatrix","vol","positioning","macro","prints","surface","auditor","infrastructure"]:
        assert f'id="section-{section}"' in html
    assert 'id="section-backtest"' not in html
    assert 'RESEARCH &amp; CALIBRATION' in html
    assert 'id="section-netposition"' not in html
    assert 'id="section-volume"' not in html
    # Content is preserved inside the merged section.
    assert 'id="netPositioningChart"' in html and 'id="positioningMetric"' in html
    assert 'id="volumeChart"' in html and 'id="volumeMetric"' in html
    assert 'class="easy-ui"' in html
    assert 'MODO FÁCIL · ON' in html
    assert 'data-workspace="trader"' in html
    assert 'data-workspace="quant"' not in html  # one DOW workspace; no duplicated quant shell
    assert 'data-workspace="research"' not in html  # consolidated DOW workspace
    assert 'renderDecisionCockpit' in js
    assert "applyWorkspace('trader'" in js
