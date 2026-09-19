"""v1.16.0 · TRACE Fusion / Flow Bars / 3D selectable views."""
import math
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.trace_analytics import strike_profile, exposure_cube, flow_bars, flow_bar_anchor_samples


def _options_history():
    rows=[]
    times=pd.date_range('2026-09-04 14:30',periods=4,freq='5min')
    for ti,ts in enumerate(times):
        for k in (599.0,600.0,601.0):
            for typ in ('call','put'):
                sgn=1 if typ=='call' else -1
                rows.append({
                    'timestamp':ts,'underlying_price':600.0+0.05*ti,'strike':k,
                    'expiration_date':'2026-09-04','option_type':typ,'dte':0.25,
                    'signed_gex_proxy':sgn*(1+ti)*(4-abs(k-600))*1e6,
                    'option_delta_exposure_info':(0.55 if typ=='call' else -0.45)*(1+ti)*1e6,
                    'gross_gex':(1+ti)*(4-abs(k-600))*1e6,
                    'open_interest':1000+100*ti,'volume':100+10*ti,'iv':0.20,
                    'calc_vanna':0.01*sgn,'calc_charm':0.02*sgn,'calc_speed':0.001*sgn,
                })
    return pd.DataFrame(rows)


def _result():
    h=_options_history();latest=h['timestamp'].max()
    cur=h[h['timestamp'].eq(latest)].groupby('strike',as_index=False).agg(
        underlying_price=('underlying_price','last'),signed_gex=('signed_gex_proxy','sum'),
        gross_gex=('gross_gex','sum'),delta_exposure=('option_delta_exposure_info','sum'),
        open_interest=('open_interest','sum'),option_volume=('volume','sum'))
    return {'enriched':h,'current_delta':cur,'spot':float(h.loc[h['timestamp'].eq(latest),'underlying_price'].iloc[-1])}


def _flow_events(n=30):
    t0=pd.Timestamp('2026-09-04 14:30')
    rows=[]
    for i in range(n):
        rows.append({'timestamp':t0+pd.Timedelta(seconds=20*i),'premium':10000+1000*i,
                     'contracts':10+i%4,'open_interest':1000,'daily_volume':3000,
                     'aggressor':'BUY' if i%2==0 else 'SELL','aggressor_confidence':0.8,
                     'option_type':'call' if i%2==0 else 'put','provider_delta':0.5 if i%2==0 else -0.5,
                     'underlying_price':600.0,'strike':600.0,'expiration_date':'2026-09-04'})
    return pd.DataFrame(rows)


def test_session_wick_uses_same_calls_view_and_respects_asof():
    h=_options_history();t=h['timestamp'].sort_values().unique()[2];cur=h[h['timestamp'].eq(t)]
    calls=strike_profile(cur,'gex','Calls',h,asof=t)
    assert not calls.empty
    # Calls are positive in the proxy and the wick excludes later/future snapshots.
    assert (calls['value']>0).all()
    k600=calls.loc[calls['strike'].eq(600.0)].iloc[0]
    expected=h[(h['timestamp']<=t)&h['strike'].eq(600.0)&h['option_type'].eq('call')].groupby('timestamp')['signed_gex_proxy'].sum().max()
    assert math.isclose(float(k600['wick_high']),float(expected),rel_tol=1e-12)


def test_dex_profile_never_falls_back_to_gamma():
    h=_options_history().drop(columns=['option_delta_exposure_info'])
    cur=h[h['timestamp'].eq(h['timestamp'].max())]
    assert strike_profile(cur,'dex','Net',h).empty


def test_exposure_cube_filters_calls_puts_and_missing_metric_is_unavailable():
    h=_options_history()
    net=exposure_cube(h,'Gamma','Net');calls=exposure_cube(h,'Gamma','Calls');puts=exposure_cube(h,'Gamma','Puts')
    assert net['ready'] and calls['ready'] and puts['ready']
    # Net call+put proxy cancels here while side views do not.
    assert np.allclose(np.asarray(net['z']),np.asarray(calls['z'])+np.asarray(puts['z']))
    bad=exposure_cube(h.drop(columns=['option_delta_exposure_info']),'Delta','Net')
    assert bad['ready'] is False and 'UNAVAILABLE' in bad['reason']


def test_flow_bars_are_causal_and_use_historical_anchor_when_ready():
    e=_flow_events(60)
    # log anchors with >=3 historical sessions, same contract used by scale_anchors.
    anchors={'flow_bar_premium_1min':{'lo':float(np.log1p(10000)),'hi':float(np.log1p(200000)),'n':5}}
    full=flow_bars(e,'1min',anchors=anchors)
    cut=flow_bars(e[e['timestamp']<=e['timestamp'].iloc[35]],'1min',anchors=anchors)
    n=min(len(cut),len(full))
    # Earlier causal stats cannot change after future events are appended.
    for c in ('premium','premium_avg_prior','premium_z_session','premium_anchor_score','premium_anomaly_score'):
        a=pd.to_numeric(full[c].iloc[:n],errors='coerce').to_numpy(float)
        b=pd.to_numeric(cut[c].iloc[:n],errors='coerce').to_numpy(float)
        assert np.allclose(a,b,equal_nan=True)
    assert full['premium_anchor_score'].notna().any()


def test_flow_bar_anchor_samples_are_bucket_magnitudes_for_tomorrow():
    s=flow_bar_anchor_samples(_flow_events(30),'1min')
    assert 'flow_bar_premium_1min' in s and len(s['flow_bar_premium_1min'])>=1
    assert all(v>0 for v in s['flow_bar_premium_1min'])


def test_legacy_trace_and_surface_plotly_renderers_are_removed():
    src=Path('app/core/advanced_visuals.py').read_text(encoding='utf-8')
    for name in ('trace_pro','trace_fusion','exposure_3d','trace_2d','surface_3d','surface_3d_filtered'):
        assert f'def {name}(' not in src
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert '/api/nextgen/trace?' in ng
    assert '/api/nextgen/surface?' in ng


def test_dashboard_has_grouped_navigation_and_nextgen_controls():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    assert html.count('class="nav-btn')==11
    assert '>EQUITY HUB<' in html and '>FLUJO<' in html and '>ESTRUCTURA<' in html
    for token in ('data-lean-go="netdrift"','data-lean-go="prints"','data-lean-go="exposure"','data-lean-go="gexmatrix"','data-lean-go="positioning"','data-lean-go="surface"'):
        assert token in html
    for token in ('id="traceTemporalHeatmap"','id="traceKeyLevels"','id="traceExpectedMove"','id="surfaceRenderStyle"','id="surfaceOptionView"'):
        assert token in html
    for removed in ('id="traceMode"','id="traceFusionView"','id="traceLens"','id="traceHeatScale"','id="traceForwardMinutes"','id="traceDealerFlow"'):
        assert removed not in html
    js=Path('app/static/app.js').read_text(encoding='utf-8')
    for arg in ('trace_mode=','trace_fusion_view=','trace_lens=','trace_strike_metric=','trace_forward_minutes='):
        assert arg not in js
    for arg in ('surface_render_style=','surface_option_view='):
        assert arg in js
