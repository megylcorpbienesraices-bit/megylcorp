import math
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.precision_engine import (
    black_scholes_price,
    market_inputs,
    recover_iv_and_greeks,
    build_data_quality,
)
from app.core.regime_engine import classify_regime
from app.core.calibration import calibration_report
from app.core.option_stream import LiveOptionFlowStream
from app.core.engine import analyze_gamma_delta, engine_config_for_asset
from app.core.expiry_window import apply_expiry_window
from app.service import _demo_history, _volatility_metrics


def test_0dte_quote_recovers_iv_and_greeks_without_provider():
    S = K = 535.0
    dte = 0.22
    sigma = 0.24
    mi = market_inputs("DIA")
    T = dte / 365.0
    mid = black_scholes_price(S, K, T, sigma, "call", mi["risk_free_rate"], mi["dividend_yield"])
    out = recover_iv_and_greeks(
        provider_iv=float("nan"), provider_delta=float("nan"), provider_gamma=float("nan"),
        bid=mid * 0.99, ask=mid * 1.01, last=mid, S=S, K=K, dte=dte,
        option_type="call", symbol="DIA",
    )
    assert out["iv_source"] == "ITM_QUANT"
    assert out["greeks_source"] == "ITM_QUANT"
    assert math.isfinite(out["iv"]) and abs(out["iv"] - sigma) < 0.02
    assert math.isfinite(out["calc_delta"])
    assert math.isfinite(out["calc_gamma"]) and out["calc_gamma"] > 0


def test_unusable_quote_does_not_invent_iv():
    out = recover_iv_and_greeks(
        provider_iv=float("nan"), provider_delta=None, provider_gamma=None,
        bid=0.0, ask=0.0, last=0.0, S=535, K=535, dte=0.1,
        option_type="put", symbol="DIA",
    )
    assert out["iv_source"] == "UNAVAILABLE"
    assert math.isnan(out["iv"])


def test_data_quality_closed_market_does_not_penalize_stale_clock():
    df = pd.DataFrame({
        "bid":[1.0, 1.2], "ask":[1.05, 1.25], "iv":[.2,.22],
    })
    q = build_data_quality(df, {"market_state":"CLOSED","matched_snapshots":2,"provider_iv_count":2}, {}, {"mode":"0DTE","count":1})
    assert q["components"]["freshness"] == 100.0
    assert 0 <= q["score"] <= 100


def test_regime_classifier_is_bounded_and_transparent():
    cur = pd.DataFrame({"strike":[534,535,536]})
    r = classify_regime({
        "regime":"NEGATIVE GAMMA", "gamma_delta_alignment_label":"ALIGNED",
        "pressure_direction":"UP", "delta_pressure_direction":"BUY",
        "pressure_score":80, "delta_pressure_score":78, "migration_strength":70,
        "delta_migration_strength":68, "spot":535.1, "gamma_flip":535.0,
        "current_delta":cur,
    }, {"regime":"EXPANSION"}, {"direction":"BULLISH"})
    assert r["regime"] in {"PINNING","TREND EXPANSION","VOLATILITY EXPANSION","MEAN REVERSION","BREAKOUT","TRANSITION"}
    assert 0 <= r["confidence"] <= 100
    assert r["profile"]


def test_calibration_lab_evaluates_mfe_mae_targets(tmp_path: Path):
    day = "2026-09-04"
    sig = pd.DataFrame([{
        "timestamp":f"{day} 09:30:00", "symbol":"DIA", "spot":535.0,
        "direction":"BUY", "scenario_type":"REBOTE", "zone_center":535.0,
        "target1":536.0, "target2":537.0, "invalidation":534.0,
        "evidence_score":72, "structural_score":80, "state":"PRIMARY",
    }])
    sig.to_csv(tmp_path / f"scanner_history_dia_{day}.csv", index=False)
    px = pd.DataFrame({
        "timestamp":pd.date_range(f"{day} 09:30:00", periods=22, freq="min"),
        "underlying_price":[535.0 + i*0.11 for i in range(22)],
    })
    px.to_csv(tmp_path / f"alpaca_dia_history_{day}.csv", index=False)
    report = calibration_report(tmp_path, "DIA", horizons=(5,10,20), min_samples=1)
    assert report["ready"] is True
    assert report["sample_size"] == 1
    assert report["horizons"]["20"]["t1_hit_pct"] == 100.0
    assert report["horizons"]["20"]["avg_mfe"] > 0


def test_option_stream_pairs_trade_with_live_quote_and_fallback_greeks():
    st = LiveOptionFlowStream()
    snap = pd.DataFrame([{
        "contract_symbol":"DIA260906C00535000", "underlying_symbol":"DIA",
        "underlying_price":535.0, "strike":535.0, "expiration_date":"2026-09-06",
        "option_type":"call", "dte":0.2, "open_interest":2500, "volume":900,
        "iv":.25, "provider_gamma":np.nan, "provider_delta":np.nan,
        "fallback_gamma":.035, "fallback_delta":.53, "greeks_source":"ITM_QUANT",
        "iv_source":"ITM_QUANT",
    }])
    st.set_universe(snap, max_contracts=10)
    st._quote({"S":"DIA260906C00535000","bp":1.00,"ap":1.10,"bs":10,"as":10,"t":"2026-09-06T14:00:00Z"})
    st._trade({"S":"DIA260906C00535000","p":1.10,"s":5,"t":"2026-09-06T14:00:01Z","x":"C","c":[]})
    ev = st.dataframe(0)
    assert len(ev) == 1
    assert ev.iloc[0]["aggressor"] == "BUY"
    assert ev.iloc[0]["greeks_source"] == "ITM_QUANT"
    assert math.isfinite(float(ev.iloc[0]["flow_score"]))


def test_demo_volatility_exposes_model_and_market_implied_move():
    h = _demo_history("DIA")
    w, _ = apply_expiry_window(h, "0DTE")
    gd = analyze_gamma_delta(w, engine_config_for_asset("DIA"))
    v = _volatility_metrics(gd)
    assert v["model_expected_move"] > 0
    assert v["market_implied_move"] is not None and v["market_implied_move"] > 0
    assert isinstance(v.get("skew_by_expiry"), list)


def test_provider_greeks_dislocation_is_explicitly_flagged():
    S = K = 535.0
    mi = market_inputs("DIA")
    T = 2/365.0
    mid = black_scholes_price(S, K, T, .22, "call", mi["risk_free_rate"], mi["dividend_yield"])
    out = recover_iv_and_greeks(
        provider_iv=.22, provider_delta=.05, provider_gamma=.001,
        bid=mid*.99, ask=mid*1.01, last=mid, S=S, K=K, dte=2,
        option_type="call", symbol="DIA",
    )
    assert out["greeks_dislocation"] is True
    assert out["greeks_source"] == "ALPACA + ITM QUANT CHECK"
    assert math.isfinite(out["provider_delta_gap"])


def test_calibration_respects_first_resolution_t1_before_later_invalidation(tmp_path: Path):
    day="2026-09-04"
    pd.DataFrame([{
        "timestamp":f"{day} 09:30:00","symbol":"DIA","spot":535.0,"direction":"BUY",
        "scenario_type":"REBOTE","zone_center":535.0,"target1":536.0,"target2":537.0,
        "invalidation":534.0,"evidence_score":75,"structural_score":80,"state":"PRIMARY",
    }]).to_csv(tmp_path/f"scanner_history_dia_{day}.csv",index=False)
    prices=[535.0,535.4,536.1,535.7,535.0,534.6,533.9]+[534.0]*15
    pd.DataFrame({"timestamp":pd.date_range(f"{day} 09:30:00",periods=len(prices),freq="min"),"underlying_price":prices}).to_csv(tmp_path/f"alpaca_dia_history_{day}.csv",index=False)
    report=calibration_report(tmp_path,"DIA",horizons=(20,),min_samples=1)
    r=report["horizons"]["20"]
    assert r["t1_hit_pct"] == 100.0
    assert r["invalidation_pct"] == 0.0
    assert r["expectancy"] > 0


def test_exposure_scenarios_are_finite_and_include_spot_shocks():
    from app.core.precision_engine import exposure_scenarios
    h=_demo_history("DIA")
    w,_=apply_expiry_window(h,"WEEK")
    gd=analyze_gamma_delta(w,engine_config_for_asset("DIA"))
    out=exposure_scenarios(gd["enriched"],pd.DataFrame(),"DIA")
    assert out["ready"] is True
    assert math.isfinite(out["structural_gex"])
    shifts={float(r["spot_shift_pct"]) for r in out["sensitivity"]}
    assert {-1.0,-0.5,-0.25,0.0,0.25,0.5,1.0}.issubset(shifts)


def test_option_stream_tick_rule_is_low_confidence_fallback():
    st=LiveOptionFlowStream()
    sym="DIA260906C00535000"
    snap=pd.DataFrame([{"contract_symbol":sym,"underlying_symbol":"DIA","underlying_price":535.0,"strike":535.0,"expiration_date":"2026-09-06","option_type":"call","dte":.2,"open_interest":1000,"volume":200,"iv":.25,"provider_gamma":.03,"provider_delta":.5}])
    st.set_universe(snap,max_contracts=10)
    # First trade establishes the tick-rule reference. Both trades are exactly at quote midpoint.
    st._quote({"S":sym,"bp":1.00,"ap":1.10,"bs":10,"as":10,"t":"2026-09-06T14:00:00Z"})
    st._trade({"S":sym,"p":1.05,"s":1,"t":"2026-09-06T14:00:01Z","x":"C","c":[]})
    st._quote({"S":sym,"bp":1.05,"ap":1.15,"bs":10,"as":10,"t":"2026-09-06T14:00:02Z"})
    st._trade({"S":sym,"p":1.10,"s":1,"t":"2026-09-06T14:00:03Z","x":"C","c":[]})
    ev=st.dataframe(0)
    assert ev.iloc[-1]["classification_method"] == "TICK_RULE"
    assert ev.iloc[-1]["aggressor"] == "BUY"
    assert 0 < float(ev.iloc[-1]["aggressor_confidence"]) < 0.6


def test_large_print_summary_detects_repeated_zone_and_relative_size():
    from app.core.large_prints import large_print_summary
    ts=pd.date_range("2026-09-04 10:00",periods=4,freq="min")
    df=pd.DataFrame({
        "timestamp":ts,"price":[535.00,535.04,535.02,536.0],"size":[5000,4200,3900,8000],
        "notional":[2675000,2247168,2086580,4288000],"exchange":["N"]*4,"exchange_name":["NYSE"]*4,"tape":["A"]*4,
        "conditions":[""]*4,"off_exchange_confirmed":[False]*4,
    })
    out=large_print_summary(df,"DIA",adv_shares=10_000_000)
    assert out["largest"]["pct_adv"] == 0.08
    assert out["repeated_zones"] and out["repeated_zones"][0]["events"] >= 2


def test_delta_gex_matrix_uses_previous_structural_snapshot():
    from app.core.institutional_modules import gex_matrix_figure
    t0=pd.Timestamp("2026-09-04 10:00:00")
    t1=pd.Timestamp("2026-09-04 10:00:15")
    enr=pd.DataFrame([
        {"timestamp":t0,"strike":535.0,"expiration_date":"2026-09-04","signed_gex_proxy":10_000_000},
        {"timestamp":t1,"strike":535.0,"expiration_date":"2026-09-04","signed_gex_proxy":13_500_000},
    ])
    fig=gex_matrix_figure({"enriched":enr,"spot":535.1},exp_count=5,mode="ΔGEX")
    assert "ΔGEX MATRIX" in str(fig.layout.title.text)
    vals=np.asarray(fig.data[0].customdata,dtype=float)
    assert np.isclose(vals.max(),3.5)


def test_session_memory_reports_intraday_center_migration(tmp_path: Path):
    from app.core.session_memory import session_memory_summary, _path
    p=_path(tmp_path,"DIA")
    ts=pd.date_range("2026-09-06 09:30:00",periods=5,freq="15min")
    pd.DataFrame({
        "timestamp":ts,"symbol":["DIA"]*5,"expiry_mode":["AUTO"]*5,
        "gamma_center":[534.8,535.0,535.2,535.45,535.7],
        "delta_center":[534.7,534.9,535.1,535.3,535.5],
        "gamma_flip":[533.9,534.0,534.15,534.25,534.4],
        "direction":["BUY"]*5,"edge_state":["ACTIONABLE"]*5,
    }).to_csv(p,index=False)
    out=session_memory_summary(tmp_path,"DIA","AUTO")
    assert out["ready"] is True and out["observations"] == 5
    assert out["gamma_center_run"]["direction"] == "UP"
    assert out["gamma_center_run"]["minutes"] >= 45


def test_macro_context_is_asset_aware_and_scanner_reads_nested_score():
    from app.service import _asset_macro_context
    from app.core.scenario_engine import _macro_risk
    macro={"stress":{"components":{"hy_z":1.0,"nfci_z":0.6,"treasury_10y_change_5":0.18,"curve_10y2y":-0.1,"event_risk":80}}}
    qqq=_asset_macro_context(macro,"QQQ","VOLATILITY EXPANSION")
    dia=_asset_macro_context(macro,"DIA","PINNING")
    assert qqq["weights"]["rates"] > dia["weights"]["rates"]
    assert _macro_risk({"asset_context":qqq}) == qqq["score"]
