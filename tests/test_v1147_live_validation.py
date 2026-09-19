import os
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.calibration import probability_calibration, _select_blend_weight_oos
from app.core.live_validation import live_validation_status
from app.core import source_fusion


def _synthetic_scope_frame(seed=7):
    rng=np.random.default_rng(seed)
    rows=[]
    days=pd.date_range('2026-07-01',periods=24,freq='B').strftime('%Y-%m-%d').tolist()
    for i,d in enumerate(days):
        for j in range(45):
            mode='0DTE' if j<30 else 'WEEK'
            e=float(rng.uniform(35,95))
            # scope curve is deliberately steeper than the global population.
            if mode=='0DTE':
                p=0.10+0.80/(1+np.exp(-(e-66)/6))
            else:
                p=0.42+0.04*np.tanh((e-65)/15)
            ok=bool(rng.random()<np.clip(p,.02,.98))
            rows.append({'horizon':10,'evidence_score':e,'outcome':'T1' if ok else 'INVALIDATION',
                         'session_date':d,'expiry_mode':mode})
    return pd.DataFrame(rows),days


def test_blend_weight_is_selected_and_final_blend_has_own_metrics(monkeypatch):
    ev,days=_synthetic_scope_frame()
    gm=probability_calibration(ev,days,10,min_samples=120,min_sessions=8,min_train_samples=40,min_test_samples=20)
    sc=ev[ev.expiry_mode=='0DTE'].copy(); sdays=sorted(sc.session_date.unique())
    sm=probability_calibration(sc,sdays,10,min_samples=120,min_sessions=8,min_train_samples=40,min_test_samples=20,promote=False)
    monkeypatch.setenv('ITM_CALIB_POOL_MIN_SELECT_SAMPLES','10')
    monkeypatch.setenv('ITM_CALIB_POOL_MIN_TEST_SAMPLES','10')
    monkeypatch.setenv('ITM_CALIB_POOL_WEIGHT_STEP','0.05')
    out=_select_blend_weight_oos(ev,10,gm,sm,days,sdays)
    assert out['status'] in {'VALIDATED','FAILED OOS'}
    assert out['brier_model'] is not None and out['log_loss_model'] is not None
    assert out['test_samples'] >= 10
    assert 0 <= out['selected_weight_scope'] <= 1
    # k=100 is merely a candidate: the selected weight and its final metrics are reported separately.
    assert 'prior_formula_weight_scope' in out and 'selection_log_loss' in out


def test_live_validation_starts_at_zero_and_gate_off(tmp_path, monkeypatch):
    monkeypatch.setenv('ITM_EV_GATE_ACTIVE','0')
    v=live_validation_status(tmp_path,'DIA','AUTO',{'sample_size':0,'sessions':0,'probability_model':{}},{}, {})
    assert v['phase']=='LIVE VALIDATION'
    assert v['sessions']==0 and v['signals']==0
    assert v['ev_gate_active'] is False
    assert 'LIVE' in v['next_step']


def test_alpaca_related_breadth_uses_existing_sip(monkeypatch):
    class Dummy: pass
    monkeypatch.setattr(source_fusion.alpaca_data,'load_settings',lambda: Dummy())
    def fake(symbol='DIA', s=None):
        px={'XLI':150.0,'XLF':60.0}[symbol]
        return {'spot':px,'market_timestamp':'2026-09-04T14:00:00Z','raw':{
            'dailyBar':{'o':px-1,'h':px+1,'l':px-2,'c':px,'v':1000000,'vw':px-.2},
            'prevDailyBar':{'c':px-2},'minuteBar':{'vw':px-.1}}}
    monkeypatch.setattr(source_fusion.alpaca_data,'fetch_stock_snapshot',fake)
    r=source_fusion.alpaca_related_context(['XLI','XLF'])
    assert r['status']=='LIVE'
    assert set(r['values'])=={'XLI','XLF'}
    assert r['values']['XLI']['change_pct']>0
    assert r['values']['XLF']['source']=='ALPACA SIP'


def test_premarket_live_only_confirmations_are_not_counted_as_missing():
    from app.core.premarket_intelligence import _confirmations
    rows = _confirmations(
        "BUY",
        external={},
        dealer={"state":"COLLECTING"},
        flow={"regime":"WAITING"},
        market_state="PREMARKET",
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Flujo opciones UNKNOWN"]["status"] == "NO APLICA PREMARKET"
    assert by_name["Cobertura dealer"]["status"] == "NO APLICA PREMARKET"
    pending = sum(1 for r in rows if r.get("status") in {"SIN DATO", "DATO PARCIAL", "ESPERANDO"})
    not_app = sum(1 for r in rows if r.get("status") == "NO APLICA PREMARKET")
    assert not_app == 2
    assert pending == len(rows) - 2
