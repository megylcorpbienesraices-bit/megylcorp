"""v1.27.13 behavioural/numeric guards for truth + decision hardening.

These tests protect invariants that can affect money or deployment safety.  They do
not claim alpha or LIVE profitability.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import math

import numpy as np
import pandas as pd
import pytest

from app.core import net_guard
from app.core.calibration import expected_value
from app.core.causality_engine import EventEnvelope
from app.core.engine import EngineConfig, _net_gex_at_hypothetical_spot
from app.core.provider_bus import UnifiedProviderBus
from app.core.scenario_engine import _scenario_zone_table, build_quant_scanner
from app.core.source_arbitration import score_source
from app.providers.tastytrade.normalizer import normalize_compact


def _neutral_row(spot: float = 600.0) -> pd.DataFrame:
    return pd.DataFrame({
        "strike":[spot], "open_interest":[1000.0], "option_volume":[100.0],
        "gross_gex":[0.0], "abs_delta_exposure":[0.0], "gex_change_pct":[0.0],
        "dominance_score":[0.0], "containment_score_delta":[0.0], "break_score_delta":[0.0],
        "containment_core":[0.0], "break_core":[0.0], "signed_gex":[0.0],
        "delta_exposure":[0.0], "mass_pct":[0.0], "intensity_pct":[0.0],
        "turnover_pct":[0.0], "net_tilt_pct":[0.0], "net_tilt_ratio":[0.0],
    })


def _rich_result(direction: str = "BUY") -> dict:
    strikes=np.arange(596.0,605.0,1.0); n=len(strikes)
    sign=1 if direction=="BUY" else -1
    cur=pd.DataFrame({
        "strike":strikes,
        "open_interest":np.linspace(3000,9000,n),
        "option_volume":np.linspace(800,2600,n),
        "gross_gex":np.linspace(1.0e7,4.0e7,n),
        "abs_delta_exposure":np.linspace(5.0e5,1.2e6,n),
        "gex_change_pct":np.linspace(.05,.15,n),
        "dominance_score":np.linspace(45,88,n),
        "containment_score_delta":np.linspace(55,82,n),
        "break_score_delta":np.linspace(42,72,n),
        "containment_core":np.linspace(50,78,n),
        "break_core":np.linspace(40,68,n),
        "signed_gex":np.where(strikes<600,-2.5e7,3.2e7),
        "delta_exposure":np.linspace(-7e5,9e5,n),
        "mass_pct":np.linspace(.2,.9,n),
        "intensity_pct":np.linspace(.3,.85,n),
        "turnover_pct":np.linspace(.15,.75,n),
        "net_tilt_pct":np.linspace(.25,.8,n),
        "net_tilt_ratio":np.linspace(-.2,.4,n),
    })
    return {"spot":600.05,"current_delta":cur,
            "pressure_direction":direction,"pressure_score":76,
            "delta_pressure_direction":direction,"delta_pressure_score":72,
            "gamma_flip":599.5,"gamma_center":600.0,"delta_center":600.2}


def _scanner(prob_model: dict | None = None) -> dict:
    return build_quant_scanner(
        "DIA", _rich_result(), {"regime":"BUY","confidence":61},
        {"regime":"STABLE","expected_move":3.0,"expected_high":603.0,"expected_low":597.0,
         "atm_iv":22.0,"nearest_dte":3.0,"nearest_expiry":"2026-09-18"},
        {}, pd.DataFrame(), pd.DataFrame(), None, {}, "OPEN",
        regime_context={}, expiry_mode="WEEK", probability_model_override=prob_model,
    )


def test_v12713_truth_decision_hardening_survives_descendant_releases():
    """Protect the v1.27.13 invariants without freezing the product at v1.27.13.

    Historical behavioural tests must survive later releases.  Release identity itself is
    guarded by the current release test (v1.27.19); this test only proves that descendants
    keep the truth/decision hardening introduced in v1.27.13.
    """
    import json
    version=Path("VERSION.txt").read_text().strip()
    meta=json.loads(Path(".itm_quant_product.json").read_text())
    parts=tuple(int(x) for x in version.split("."))
    assert parts >= (1,27,13)
    assert meta["version"]==version
    assert meta["scope"]=="MULTI_ASSET"
    # Current release identity is intentionally allowed to advance; the behavioural
    # guards below are what certify that the v1.27.13 hardening still exists.


def test_invalid_event_time_never_becomes_valid_now_in_provider_bus():
    bus=UnifiedProviderBus()
    rec=datetime(2026,9,12,20,0,tzinfo=timezone.utc)
    bus.ingest(source="TEST",symbol="DIA",event_type="QUOTE",
               values={"bid":600.0,"ask":600.1,"instrument_type":"EQUITY"},
               timestamp="definitely-not-a-time",received_at=rec)
    row=bus.latest_events("DIA")[0]
    assert row["event_time_valid"] is False
    assert row["event_time_source"]=="RECEIVE_PROXY"
    assert row["latency_ms"] is None
    assert row["timestamp"]==rec.isoformat()


def test_causality_envelope_rejects_invalid_market_event_time():
    with pytest.raises(ValueError, match="invalid event timestamp"):
        EventEnvelope.build(source="TEST",symbol="DIA",event_type="TRADE",
                            event_time="bad-clock",payload={"price":600})


def test_tasty_quote_receive_time_is_explicit_proxy_not_observed_event_time():
    rec=datetime(2026,9,12,20,0,tzinfo=timezone.utc)
    e=normalize_compact("Quote",["Quote","DIA",599.9,600.1,10,12],canonical_symbol="DIA",received_at=rec)
    assert e.payload["event_time_valid"] is False
    assert e.payload["event_time_source"]=="RECEIVE_PROXY"
    assert e.event_time.to_pydatetime()==rec


def test_tasty_candle_provider_clock_keeps_real_provenance():
    rec=datetime(2026,9,12,20,0,tzinfo=timezone.utc)
    event_ms=int(datetime(2026,9,12,19,59,tzinfo=timezone.utc).timestamp()*1000)
    row=["Candle","DIA",0,1,event_ms,1,1,599,601,598,600,1000,600,500,500,.22,1200]
    e=normalize_compact("Candle",row,canonical_symbol="DIA",received_at=rec)
    assert e.payload["event_time_valid"] is True
    assert e.payload["event_time_source"]=="PROVIDER_CANDLE_TIME"
    assert e.event_time.to_pydatetime()==datetime(2026,9,12,19,59,tzinfo=timezone.utc)


def test_silent_source_no_longer_beats_source_that_reports_bad_metrics():
    common={"channel":"OPTION_CHAIN","latency_ms":200,"age_ms":1000,"gap_rate":0.0,"status":"LIVE"}
    honest=score_source({**common,"name":"HONEST","cross_source_divergence":0.012,"sequence_ok":False})
    silent=score_source({**common,"name":"SILENT"})
    assert honest["metrics_reported"]=="2/2"
    assert silent["metrics_reported"]=="0/2"
    assert silent["quality_score"] < honest["quality_score"]


def test_generic_futures_quote_uses_futures_policy_not_equity_policy():
    scored=score_source({"name":"YM","channel":"QUOTE","instrument_type":"FUTURE",
                         "latency_ms":50,"age_ms":100,"gap_rate":0,"sequence_ok":True,
                         "cross_source_divergence":0.001,"status":"LIVE"})
    assert scored["channel_policy"]=="FUTURES_TICK"


def test_receive_proxy_clock_is_penalized_by_arbitration():
    base={"channel":"EQUITY_QUOTE","latency_ms":50,"age_ms":100,"gap_rate":0,
          "sequence_ok":True,"cross_source_divergence":0.001,"status":"LIVE"}
    observed=score_source({**base,"name":"OBS","event_time_valid":True,"event_time_source":"PROVIDER_EVENT_TIME"})
    proxy=score_source({**base,"name":"PROXY","event_time_valid":False,"event_time_source":"RECEIVE_PROXY"})
    assert proxy["quality_score"] < observed["quality_score"]


def test_neutral_atm_zone_stays_neutral_instead_of_default_buy():
    result={"spot":600.0,"current_delta":_neutral_row(),
            "pressure_direction":"NEUTRAL","pressure_score":0,
            "delta_pressure_direction":"NEUTRAL","delta_pressure_score":0}
    z=_scenario_zone_table(result,{"regime":"NEUTRAL","confidence":0},{},{},pd.DataFrame(),pd.DataFrame(),None,{})
    assert len(z)==1
    assert z.iloc[0]["bounce_direction"]=="NEUTRAL"
    assert z.iloc[0]["break_direction"]=="NEUTRAL"


def test_scanner_fails_closed_when_only_zone_has_no_directional_evidence():
    result={"spot":600.0,"current_delta":_neutral_row(),
            "pressure_direction":"NEUTRAL","pressure_score":0,
            "delta_pressure_direction":"NEUTRAL","delta_pressure_score":0}
    out=build_quant_scanner("DIA",result,{"regime":"NEUTRAL","confidence":0},{},{},
                            pd.DataFrame(),pd.DataFrame(),None,{},"OPEN")
    assert out["ready"] is False
    assert out["reason"]=="NO_DIRECTIONAL_EVIDENCE"
    assert out["direction"] is None


def test_expected_value_helper_requires_explicit_execution_cost():
    assert expected_value(.50,2.0)["ev_r"] is None
    assert expected_value(.50,2.0,0.0)["ev_r"]==pytest.approx(.5)
    assert expected_value(.50,2.0,.25)["ev_r"]==pytest.approx(.25)


def test_scanner_ev_is_unavailable_when_execution_cost_not_measured(monkeypatch):
    monkeypatch.delenv("ITM_BACKTEST_COST_R",raising=False)
    monkeypatch.setenv("ITM_EV_GATE_ACTIVE","1")
    model={"ready":True,"stage":"ELIGIBLE_FOR_PROMOTION","knots":[
        {"evidence":0,"probability":.55},{"evidence":100,"probability":.55}],"source_label":"TEST"}
    out=_scanner(model)
    assert out["ready"]
    gate=out["edge_gate"]
    assert gate["cost_measured"] is False
    assert gate["cost_r"] is None
    assert gate["expected_value_r"] is None
    assert gate["active"] is False


def test_scanner_explicit_zero_cost_is_measured_not_implicit(monkeypatch):
    monkeypatch.setenv("ITM_BACKTEST_COST_R","0")
    monkeypatch.setenv("ITM_EV_GATE_ACTIVE","1")
    model={"ready":True,"stage":"ELIGIBLE_FOR_PROMOTION","knots":[
        {"evidence":0,"probability":.55},{"evidence":100,"probability":.55}],"source_label":"TEST"}
    out=_scanner(model); gate=out["edge_gate"]
    assert gate["cost_measured"] is True
    assert gate["cost_r"]==0.0
    assert gate["expected_value_r"] is not None


def test_legacy_master_token_cookie_is_forced_off_in_production(monkeypatch):
    monkeypatch.setenv("ITM_ALLOW_LEGACY_COOKIE","1")
    monkeypatch.setenv("ITM_PRODUCTION","1")
    assert net_guard.allow_legacy_master_token_cookie() is False
    monkeypatch.setenv("ITM_PRODUCTION","0")
    monkeypatch.setenv("ITM_ENV","production")
    assert net_guard.allow_legacy_master_token_cookie() is False


def _flip_chain() -> pd.DataFrame:
    ts=pd.Timestamp("2026-09-12 15:30:00")
    return pd.DataFrame([
        dict(timestamp=ts,underlying_price=600.0,strike=595.0,dte=5.0,option_type="call",open_interest=1200,volume=20,iv=.22,model_risk_free_rate=.041,model_dividend_yield=.012,contract_multiplier=100.0),
        dict(timestamp=ts,underlying_price=600.0,strike=600.0,dte=5.0,option_type="call",open_interest=1000,volume=20,iv=.21,model_risk_free_rate=.042,model_dividend_yield=.013,contract_multiplier=100.0),
        dict(timestamp=ts,underlying_price=600.0,strike=600.0,dte=5.0,option_type="put",open_interest=1100,volume=20,iv=.23,model_risk_free_rate=.042,model_dividend_yield=.013,contract_multiplier=100.0),
        dict(timestamp=ts,underlying_price=600.0,strike=605.0,dte=5.0,option_type="put",open_interest=1500,volume=20,iv=.24,model_risk_free_rate=.043,model_dividend_yield=.014,contract_multiplier=100.0),
    ])


def test_hypothetical_gex_uses_per_contract_r_q_multiplier_not_cfg_defaults():
    chain=_flip_chain()
    a=_net_gex_at_hypothetical_spot(chain,601.0,EngineConfig(risk_free_rate=.01,dividend_yield=.0,contract_multiplier=10))
    b=_net_gex_at_hypothetical_spot(chain,601.0,EngineConfig(risk_free_rate=.25,dividend_yield=.20,contract_multiplier=1000))
    assert a==pytest.approx(b,rel=1e-12,abs=1e-9)


def test_hypothetical_gex_is_linear_in_per_contract_multiplier():
    chain=_flip_chain(); cfg=EngineConfig()
    a=_net_gex_at_hypothetical_spot(chain,601.0,cfg)
    doubled=chain.copy(); doubled["contract_multiplier"]*=2
    b=_net_gex_at_hypothetical_spot(doubled,601.0,cfg)
    assert b==pytest.approx(2*a,rel=1e-12,abs=1e-8)


def test_dockerignore_excludes_secret_env_and_keeps_example():
    txt=Path(".dockerignore").read_text()
    assert ".env\n" in txt
    assert ".env.*" in txt
    assert "!.env.example" in txt
    assert "app/storage/" in txt


def test_rust_pipeline_source_is_bounded_and_has_hard_heap_guard():
    main=Path("rust/causality_engine/src/main.rs").read_text()
    orderer=Path("rust/causality_engine/src/orderer.rs").read_text()
    metrics=Path("rust/causality_engine/src/metrics.rs").read_text()
    assert "unbounded::<EventEnvelope>()" not in main
    assert "bounded::<EventEnvelope>" in main
    assert "heap.len()>=cfg.heap_capacity.max(1)" in orderer
    assert "heap_overflow_dropped" in metrics
    assert "outbound_backpressure" in metrics
    assert "outbound_timeout_dropped" in metrics
    assert "send_timeout" in orderer
