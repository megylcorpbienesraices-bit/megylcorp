"""v1.15.4 · Multitimeframe tape context is causal, normalized and context-only."""
import math
import os
import numpy as np
import pandas as pd

from app.core.trace_analytics import time_aggression_bars, multi_timeframe_aggression


def _ticks(minutes=20, seed=7, buy_prob=.50):
    r=np.random.default_rng(seed); t0=pd.Timestamp('2026-09-04 09:30'); rows=[]
    for m in range(minutes):
        # deliberately U-shaped-ish activity and heterogeneous trade sizes
        activity=1.0+2.5*np.exp(-m/6.0)+1.4*np.exp(-(minutes-m)/7.0)
        n=max(8,int(28*activity))
        for k in range(n):
            rows.append({
                'timestamp':t0+pd.Timedelta(minutes=m,seconds=60*k/n),
                'price':600+r.normal(0,.035),
                'size':float(max(1,r.lognormal(2.5,.8))),
                'aggressor_sign':1 if r.random()<buy_prob else -1,
            })
    return pd.DataFrame(rows)


def test_delta_z_uses_trade_size_variance_not_volume_power():
    t0=pd.Timestamp('2026-09-04 09:30')
    rows=[]
    sizes=[1.,2.,3.,4.]; signs=[1,-1,1,-1]
    for i,(sz,sgn) in enumerate(zip(sizes,signs)):
        rows.append({'timestamp':t0+pd.Timedelta(seconds=10*i),'price':600.0,'size':sz,'aggressor_sign':sgn})
    # one tick in the next bucket makes the first bucket complete
    rows.append({'timestamp':t0+pd.Timedelta(seconds=61),'price':600.0,'size':1.0,'aggressor_sign':1})
    b=time_aggression_bars(pd.DataFrame(rows),1.0,min_ready_bars=0)
    first=b.iloc[0]
    expected=(1-2+3-4)/math.sqrt(1+4+9+16)
    assert abs(float(first['delta_z_raw'])-expected)<1e-4
    assert bool(first['z_ready']) is True


def test_time_context_is_strictly_causal_including_absorption_baseline():
    tk=_ticks(24,seed=11,buy_prob=.64)
    cut=pd.Timestamp('2026-09-04 09:42:30')
    partial=time_aggression_bars(tk[tk['timestamp']<=cut],1.0)
    full=time_aggression_bars(tk,1.0)
    # Ignore the partial view's final forming bucket. Everything before it must be invariant.
    common=partial[partial['complete']].copy()
    got=full.set_index('bucket').loc[common['bucket']]
    a=common.set_index('bucket')
    assert np.allclose(pd.to_numeric(a['delta_z_raw']),pd.to_numeric(got['delta_z_raw']),equal_nan=True)
    assert a['absorbed'].astype(bool).tolist()==got['absorbed'].astype(bool).tolist()


def test_missing_frame_is_collecting_not_balanced():
    mt=multi_timeframe_aggression(_ticks(4,seed=3,buy_prob=.80),intervals=(1,3,5))
    assert mt['ready'] is False
    assert mt['status']=='COLLECTING'
    assert mt['aligned'] is False
    assert any(v['control']=='COLLECTING' for v in mt['frames'].values())


def test_multiframe_is_one_context_family_and_never_a_scanner_or_trigger_vote():
    mt=multi_timeframe_aggression(_ticks(35,seed=9,buy_prob=.78))
    assert mt['family']=='MULTITIMEFRAME_TAPE_CONTEXT'
    assert mt['role']=='CONTEXT_ONLY'
    assert mt['affects_scanner_direction'] is False
    assert mt['affects_scanner_score'] is False
    assert mt['affects_timing_trigger'] is False
    assert mt['dead_band_status'].startswith('SHADOW')


def test_dead_band_is_shadow_configurable(monkeypatch):
    monkeypatch.setenv('ITM_TIMEFLOW_DEAD_BAND','1.90')
    mt=multi_timeframe_aggression(_ticks(30,seed=13,buy_prob=.70),dead_band=None)
    assert abs(mt['dead_band']-1.90)<1e-9
    assert mt['dead_band_status']=='SHADOW · CALIBRABLE'
