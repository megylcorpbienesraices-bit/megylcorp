from __future__ import annotations

import pandas as pd

from app.core.engine import analyze_gamma_delta, EngineConfig
from app.core.native_options_structure import build_native_options_structure


def _chain() -> pd.DataFrame:
    rows=[]
    ts=pd.Timestamp("2026-09-16 13:30:00")
    spot=100.0
    for strike in (96.0,98.0,100.0,102.0,104.0):
        for typ in ("call","put"):
            rows.append({
                "timestamp":ts,"underlying_price":spot,"strike":strike,"dte":1.0,
                "option_type":typ,"iv":0.22,"open_interest":int(100+abs(strike-100)*25),
                "volume":int(30+abs(strike-100)*8),"contract_multiplier":100.0,
            })
    return pd.DataFrame(rows)


def test_native_structure_replaces_structural_level_contract():
    gd=analyze_gamma_delta(_chain(), EngineConfig(symbol="DIA"))
    out=build_native_options_structure(gd)
    assert out["ready"] is True
    assert out["authority"] == "ITM_QUANT_NATIVE_OPTIONS_STRUCTURE"
    assert out["provider_dependency"] == "NONE"
    for field in ("major_pos_oi","major_neg_oi","major_pos_vol","major_neg_vol","net_gex_oi","net_gex_volume"):
        assert field in out
    assert isinstance(out["top_oi_gex_strikes"], list) and out["top_oi_gex_strikes"]
    assert isinstance(out["top_volume_gex_strikes"], list) and out["top_volume_gex_strikes"]


def test_zero_gamma_is_not_promoted_when_engine_reports_no_crossing():
    gd=analyze_gamma_delta(_chain(), EngineConfig(symbol="DIA"))
    gd["gamma_flip_crossing"] = False
    gd["gamma_flip"] = 101.25
    out=build_native_options_structure(gd)
    assert out["zero_gamma"] is None
    assert out["gamma_flip"] == 101.25
    assert out["gamma_flip_diagnostic_only"] is True
