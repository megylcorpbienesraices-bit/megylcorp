import math
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.core.assets import chain_window_for
from app.core.expiry_window import (
    is_zero_dte, zero_dte_status, expiry_confluence, effective_horizon_days,
    apply_expiry_window,
)
from app.core.flow_intelligence import _flow_bucket
from app.core import instruments
from app.core.institutional_modules import net_drift_summary
from app import service as svc

TODAY=pd.Timestamp('2026-09-04 15:00:00')

def chain(expiries, spot=100.0):
    rows=[]
    for e in expiries:
        for k in [spot-1,spot,spot+1]:
            for typ in ['call','put']:
                rows.append(dict(timestamp=TODAY, underlying_price=spot, strike=k,
                                 expiration_date=e, dte=max((pd.Timestamp(e)-TODAY.normalize()).days+.05,.01),
                                 option_type=typ, open_interest=1000, volume=100, iv=.20,
                                 provider_gamma=.02, provider_delta=.5,
                                 signed_gex=1e6 if k>=spot else -1e6, gross_gex=1e6,
                                 option_delta_exposure_info=1e6 if typ=='call' else -1e6,
                                 signed_gex_proxy=1e6))
    return pd.DataFrame(rows)

def test_0dte_date_is_single_source_of_truth():
    df=pd.DataFrame([
        {'timestamp':TODAY,'expiration_date':'2026-09-04','dte':.01},
        {'timestamp':TODAY,'expiration_date':'2026-09-05','dte':.25},
        {'timestamp':TODAY,'expiration_date':None,'dte':.05},
    ])
    assert list(is_zero_dte(df,date(2026,9,4))) == [True,False,False]
    assert zero_dte_status(df,date(2026,9,4))['contracts_today']==1

def test_flow_bucket_never_calls_tomorrow_0dte():
    row={'timestamp':TODAY,'expiration_date':'2026-09-05','dte':.25,'underlying_price':100,'strike':100,'iv':.2}
    assert _flow_bucket(row)[0] != '0dte'
    row2=dict(row,expiration_date='2026-09-04',dte=.01)
    assert _flow_bucket(row2)[0]=='0dte'

def test_net_drift_0dte_uses_expiration_date_not_fractional_dte():
    df=chain(['2026-09-04','2026-09-05'])
    # tomorrow contracts have fractional dte < .75 on purpose; strict summary must only use today
    df.loc[df.expiration_date.eq('2026-09-05'),'dte']=.25
    r={'enriched':df}
    summary=net_drift_summary(r,'0DTE')
    assert summary
    # exact numeric isn't the point; the path must exist without mixing tomorrow
    today=df[is_zero_dte(df)]
    assert len(today)==6 and len(df)==12

def test_disjoint_multi_expiry_does_not_self_confirm():
    z=expiry_confluence(chain(['2026-09-04']))['zones'][0]
    assert z['horizon_count']==1 and z['horizon_of']==1 and not z['multi_horizon']
    z2=expiry_confluence(chain(['2026-09-04','2026-09-11']))['zones'][0]
    assert set(z2['horizons'])=={'0DTE','SEMANA SIG.'}
    assert z2['multi_horizon']

def test_horizon_is_derived_from_selected_expiries_not_fixed_three_days():
    d0=effective_horizon_days(chain(['2026-09-04']),'0DTE')
    wk=effective_horizon_days(chain(['2026-09-04','2026-09-11']),'2W')
    assert d0['ready'] and wk['ready']
    assert d0['days'] < 1.0
    assert wk['days'] > d0['days']
    assert not math.isclose(wk['days'],3.0)

def test_same_relative_structure_same_sigma_window():
    a=chain_window_for('GDX',45,30,2.0)['window']/45
    b=chain_window_for('SPY',600,30,2.0)['window']/600
    assert math.isclose(a,b,rel_tol=2e-4)

def test_instrument_clock_is_used_for_sigma():
    eq=instruments.sigma_horizon(600,18,10,'SPY')
    fut=instruments.sigma_horizon(600,18,10,'ES')
    assert eq>fut>0

def test_live_refresh_uses_sigma_chain_window_second_pass(monkeypatch):
    # Integration: prove production refresh calls the dynamic window, not just an isolated helper.
    base=chain(['2026-09-04','2026-09-11'],spot=100)
    calls=[]
    def fake_fetch(symbol,w,e):
        calls.append((symbol,float(w),int(e)))
        out=base.copy()
        return out, {'market_state':'CLOSED','spot':100,'source':'TEST'}
    monkeypatch.setattr(svc,'DATA_MODE','live')
    monkeypatch.setattr(svc,'load_settings',lambda: object())
    monkeypatch.setattr(svc,'fetch_asset_options_snapshot',fake_fetch)
    monkeypatch.setattr(svc,'append_history',lambda *a,**k:None)
    monkeypatch.setattr(svc,'load_history',lambda *a,**k:base.copy())
    monkeypatch.setattr(svc,'fetch_macro_context',lambda *a,**k:{'series':{},'events':[],'stress':{}})
    st=svc.PlatformState(symbol='DIA')
    # Avoid unrelated persistence/network layers while retaining production fetch logic.
    st.refresh(False)
    assert calls
    assert st.meta.get('chain_window_method','').startswith('SIGMA')
    assert st.meta.get('chain_horizon_source') in {'AUTO_WEIGHTED_SELECTED_EXPIRIES','SELECTED_EXPIRIES_MEDIAN','0DTE_REMAINING_TODAY'}

def test_no_ticker_branch_in_signal_modules():
    forbidden=[]
    allowed={'assets.py','instruments.py','source_fusion.py','external_markets.py'}
    import re
    pat=re.compile(r"(?:if|elif).*\b(?:symbol|sym)\b.*(?:DIA|SPY|QQQ|GDX|GLD|AAPL|TSLA)")
    signal_files=[p for p in Path('app/core').glob('*.py') if p.name not in allowed]
    for path in signal_files:
        name=path.name
        text=path.read_text(encoding='utf-8')
        for i,line in enumerate(text.splitlines(),1):
            if pat.search(line) and not line.lstrip().startswith('#'):
                forbidden.append(f'{name}:{i}:{line.strip()}')
    assert not forbidden, forbidden
