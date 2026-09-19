"""v1.15.5 · volatility-aware + structure-aware Scanner invalidation."""
import math
from pathlib import Path

import pandas as pd

from app.core.scenario_engine import _stop_risk_model


def _zones(include_near=True):
    rows=[{"strike":600.0,"structural_score":90.0}]
    if include_near:
        rows.append({"strike":599.0,"structural_score":82.0})
    rows.append({"strike":596.0,"structural_score":95.0})  # too far for the same thesis
    return pd.DataFrame(rows)


def test_higher_iv_widens_stop_and_changes_rr_geometry(monkeypatch):
    monkeypatch.setenv("ITM_STOP_HORIZON_MIN","10")
    monkeypatch.setenv("ITM_STOP_K_SIGMA","1.25")
    # No nearby structural floor: isolate volatility response.
    z=pd.DataFrame([{"strike":600.0,"structural_score":90.0}])
    low=_stop_risk_model(z,1,600.0,599.82,600.18,600.0,1.0,{"atm_iv":12.0},601.0)
    high=_stop_risk_model(z,1,600.0,599.82,600.18,600.0,1.0,{"atm_iv":30.0},601.0)
    assert low["source"] == high["source"] == "ATM_IV"
    assert high["sigma_h"] > low["sigma_h"]
    assert high["final_buffer"] > low["final_buffer"]
    assert high["invalidation"] < low["invalidation"]
    live_low=[x for x in low["shadow_k"] if x["active_live"]][0]
    live_high=[x for x in high["shadow_k"] if x["active_live"]][0]
    assert live_high["rr_t1"] < live_low["rr_t1"]


def test_nearby_strong_structure_can_widen_invalidation(monkeypatch):
    monkeypatch.setenv("ITM_STOP_HORIZON_MIN","10")
    monkeypatch.setenv("ITM_STOP_K_SIGMA","1.0")
    r=_stop_risk_model(_zones(True),1,600.0,599.82,600.18,600.0,1.0,{"atm_iv":5.0},601.0)
    assert r["structural_reference"] == 599.0
    assert r["structural_reference_score"] == 82.0
    assert r["structural_buffer"] > r["volatility_buffer"]
    assert r["invalidation"] < 599.0
    assert "STRUCTURE" in r["status"]


def test_far_structure_does_not_drag_stop_into_different_thesis(monkeypatch):
    monkeypatch.setenv("ITM_STOP_STRUCTURAL_MAX_STEPS","2.0")
    r=_stop_risk_model(_zones(False),1,600.0,599.82,600.18,600.0,1.0,{"atm_iv":5.0},601.0)
    assert r["structural_reference"] is None
    assert r["invalidation"] > 596.0


def test_missing_iv_is_explicit_fallback_not_fabricated_sigma():
    r=_stop_risk_model(pd.DataFrame([{"strike":600.0,"structural_score":90.0}]),1,
                       600.0,599.82,600.18,600.0,1.0,{},601.0)
    assert r["source"] == "LEGACY_FALLBACK_NO_IV"
    assert r["sigma_h"] is None
    assert "NO IV" in r["status"]
    assert math.isclose(r["volatility_buffer"],0.42,rel_tol=0,abs_tol=1e-9)


def test_shadow_k_is_recorded_but_only_live_k_is_active(monkeypatch):
    monkeypatch.setenv("ITM_STOP_K_SIGMA","1.25")
    r=_stop_risk_model(pd.DataFrame([{"strike":600.0,"structural_score":90.0}]),1,
                       600.0,599.82,600.18,600.0,1.0,{"atm_iv":25.0},603.0)
    assert [x["k_sigma"] for x in r["shadow_k"]] == [1.0,1.25,1.5,1.75]
    active=[x for x in r["shadow_k"] if x["active_live"]]
    assert len(active)==1 and active[0]["k_sigma"]==1.25
    inv=[x["invalidation"] for x in r["shadow_k"]]
    assert inv == sorted(inv, reverse=True)  # BUY: larger k -> lower invalidation
