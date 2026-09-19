import os
import numpy as np
import pandas as pd

from app.core.scale_anchors import stage_session_anchors, promote_pending_anchors, load_anchors
from app.core.calibration import _eval_signal
from app.core.scenario_engine import _resolve_edge_state


def test_scale_anchor_current_session_is_not_used(tmp_path):
    x=np.random.default_rng(7).lognormal(6,1,100)
    stage_session_anchors(tmp_path,'SPY',{'open_interest':x},'2026-09-06')
    assert load_anchors(tmp_path,'SPY') == {}
    promote_pending_anchors(tmp_path,'SPY','2026-09-06')
    assert load_anchors(tmp_path,'SPY') == {}
    promote_pending_anchors(tmp_path,'SPY','2026-09-07')
    a=load_anchors(tmp_path,'SPY')
    assert a['open_interest']['n']==1
    assert a['open_interest']['last_session']=='2026-09-06'


def test_ev_gate_needs_explicit_activation_even_when_calibrated():
    # Strong structure + negative EV: while disabled, legacy evidence remains production.
    edge,mode,active=_resolve_edge_state(82,10,'PRIMARY',-0.2,'CALIBRATED',True,False)
    assert edge=='ACTIONABLE' and not active and 'SHADOW' in mode
    # Once explicitly enabled, the same negative-EV setup is blocked.
    edge2,mode2,active2=_resolve_edge_state(82,10,'PRIMARY',-0.2,'CALIBRATED',True,True)
    assert edge2=='NO EDGE' and active2 and 'ACTIVE' in mode2


def test_option_pnl_resolves_at_structural_event_not_horizon():
    t0=pd.Timestamp('2026-09-04 14:00:00')
    # reaches T1 at minute 2, then reverses hard; theoretical option exit must stay at T1 event.
    prices=[600.0,600.6,601.1,600.0,599.0]
    px=pd.DataFrame({'timestamp':[t0+pd.Timedelta(minutes=i) for i in range(len(prices))], 'price':prices})
    r=pd.Series({'timestamp':t0,'direction':'BUY','spot':600.0,'target1':601.0,'target2':603.0,
                 'invalidation':599.4,'zone_center':600.0,'symbol':'SPY','atm_iv_decimal':0.22,'signal_dte':0.30})
    prev=os.environ.get('ITM_INSTRUMENT_MODE');os.environ['ITM_INSTRUMENT_MODE']='options'
    try:
        out=_eval_signal(r,px,10)
    finally:
        if prev is None: os.environ.pop('ITM_INSTRUMENT_MODE',None)
        else: os.environ['ITM_INSTRUMENT_MODE']=prev
    assert out['option_ready'] and out['t1_hit']
    assert out['option_resolution_minutes']==2.0
    assert out['option_pnl_model']=='THEORETICAL_IV_CONSTANT'

def test_scale_anchors_are_separate_by_expiry_scope(tmp_path):
    x=np.random.default_rng(9).lognormal(6,1,100)
    stage_session_anchors(tmp_path,'DIA',{'open_interest':x},'2026-09-06','0DTE')
    promote_pending_anchors(tmp_path,'DIA','2026-09-07','0DTE')
    assert 'open_interest' in load_anchors(tmp_path,'DIA','0DTE')
    assert load_anchors(tmp_path,'DIA','WEEK') == {}
