import math
import os
import numpy as np
import pandas as pd

from app.core.calibration import (
    option_economic_rr, hierarchical_probability_calibration,
    probability_calibration, _eval_signal,
)


def _hier_frame(n_global=1200, n_scope=80, seed=44):
    rng=np.random.default_rng(seed)
    sessions=pd.date_range("2026-06-01",periods=45,freq="B").strftime("%Y-%m-%d").tolist()
    rows=[]
    # global diversified history
    for i in range(n_global):
        ev=float(rng.uniform(35,95)); p=float(np.clip(.18+.60/(1+np.exp(-(ev-68)/7)),.05,.95)); ok=rng.random()<p
        day=sessions[i%len(sessions)]
        mode=["AUTO","WEEK","2W","MONTH"][i%4]
        rows.append({"horizon":10,"evidence_score":ev,"outcome":"T1" if ok else "INVALIDATION",
                     "session_date":day,"expiry_mode":mode,
                     "t1_minutes":float(rng.uniform(4,22)) if ok else np.nan,
                     "invalidation_minutes":float(rng.uniform(3,18)) if not ok else np.nan})
    # sparse but informative 0DTE scope
    for i in range(n_scope):
        ev=float(rng.uniform(40,95)); p=float(np.clip(.12+.70/(1+np.exp(-(ev-70)/6)),.05,.95)); ok=rng.random()<p
        day=sessions[i%len(sessions)]
        rows.append({"horizon":10,"evidence_score":ev,"outcome":"T1" if ok else "INVALIDATION",
                     "session_date":day,"expiry_mode":"0DTE",
                     "t1_minutes":float(rng.uniform(2,14)) if ok else np.nan,
                     "invalidation_minutes":float(rng.uniform(2,12)) if not ok else np.nan})
    return pd.DataFrame(rows)


def test_option_economic_rr_uses_premium_and_time_not_stock_geometry():
    rt={"source":"EXPIRY","t1":{"n":50,"p25":5.0,"median":20.0,"p75":45.0},
        "invalidation":{"n":40,"p25":4.0,"median":18.0,"p75":40.0}}
    r=option_economic_rr("SPY","BUY",600,600,601,599,0.22,5.0,rt,half_spread_pct=2.5)
    assert r["ready"]
    assert r["risk_unit"]=="MODEL_TO_INVALIDATION"
    assert r["rr"]>0 and not math.isclose(r["rr"],1.0,rel_tol=1e-3)
    assert r["scenarios"]["fast"]["rr"] != r["scenarios"]["slow"]["rr"]


def test_option_backtest_r_is_not_full_premium_anymore():
    t0=pd.Timestamp("2026-09-04 14:00:00")
    px=pd.DataFrame({"timestamp":[t0+pd.Timedelta(minutes=i) for i in range(8)],
                     "price":[600,600.3,600.7,601.1,601.2,601.0,600.8,600.7]})
    row=pd.Series({"timestamp":t0,"direction":"BUY","spot":600.0,"target1":601.0,"target2":603.0,
                   "invalidation":599.0,"zone_center":600.0,"symbol":"SPY","atm_iv_decimal":0.22,"signal_dte":1.0})
    prev=os.environ.get("ITM_INSTRUMENT_MODE");os.environ["ITM_INSTRUMENT_MODE"]="options"
    try: out=_eval_signal(row,px,10)
    finally:
        if prev is None: os.environ.pop("ITM_INSTRUMENT_MODE",None)
        else: os.environ["ITM_INSTRUMENT_MODE"]=prev
    assert out["option_ready"]
    assert out["option_risk_unit"]=="MODEL_TO_INVALIDATION_AT_RESOLUTION_TIME"
    assert out["option_model_risk_premium"]>0
    expected=out["premium_pnl"]/out["option_model_risk_premium"]
    assert math.isclose(out["r_multiple"],round(expected,4),rel_tol=1e-9)


def test_probability_calibration_reports_log_loss_oos():
    df=_hier_frame(1400,0)
    sessions=sorted(df.session_date.unique())
    m=probability_calibration(df,sessions,10)
    assert "log_loss_model" in m and "log_loss_base_rate" in m
    assert m["log_loss_model"]>=0 and m["log_loss_base_rate"]>=0


def test_partial_pooling_backs_off_to_global_and_uses_sparse_scope(monkeypatch):
    monkeypatch.setenv("ITM_CALIB_POOLING_K","100")
    df=_hier_frame(1400,80)
    m=hierarchical_probability_calibration(df,"0DTE",10)
    assert m["ready"], m
    assert m["global_samples"]>=120
    assert 0 <= m["pooling_weight_scope"] < 1
    assert m["pooling_weight_global"]>0
    assert m["resolution_time"]["source"] in {"EXPIRY","GLOBAL BACKOFF"}
    assert m["source_label"] in {"GLOBAL + EXPIRY · w OOS","GLOBAL BACKOFF","EXPIRY ONLY"}


def test_partial_pooling_scope_weight_grows_with_scope_sample(monkeypatch):
    monkeypatch.setenv("ITM_CALIB_POOLING_K","100")
    small=hierarchical_probability_calibration(_hier_frame(1400,60,61),"0DTE",10)
    big=hierarchical_probability_calibration(_hier_frame(1400,350,61),"0DTE",10)
    assert big["pooling_weight_scope"] >= small["pooling_weight_scope"]


def test_missing_resolution_time_refuses_option_rr():
    r=option_economic_rr("SPY","BUY",600,600,601,599,0.22,1.0,{"source":"UNAVAILABLE"})
    assert not r["ready"] and "tiempos" in r["reason"]
