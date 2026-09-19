from pathlib import Path
import numpy as np
import pandas as pd

from app.core.trace_analytics import session_landscape
from app.core.advanced_visuals import trace_landscape_3d


def _landscape_frame():
    rows=[]
    times=pd.date_range('2026-09-07 09:30',periods=4,freq='5min')
    for j,t in enumerate(times):
        for k in [599.0,600.0,601.0]:
            # Gamma flips around 600 and decays through time.
            g={599.0:-2.0e6,600.0:0.5e6,601.0:2.0e6}[k]*(1-.12*j)
            d={599.0:-5.0e6,600.0:1.0e6,601.0:7.0e6}[k]*(1-.05*j)
            rows.append({
                'timestamp':t,'strike':k,'underlying_price':600.0,
                'signed_gex_proxy':g,'option_delta_exposure_info':d,
                'iv':0.20,'dte':1.0,'open_interest':1000.0,'option_type':'call',
            })
    return pd.DataFrame(rows)


def test_landscape_height_is_real_millions_and_scale_only_changes_color():
    h=_landscape_frame()
    a=session_landscape(h,lens='Gamma',scale_mode='session',spot=600,symbol='SPY')
    b=session_landscape(h,lens='Gamma',scale_mode='column',spot=600,symbol='SPY')
    assert a['ready'] and b['ready']
    assert np.allclose(a['z'],b['z'])  # physical height must not be normalized
    assert not np.allclose(a['z_scaled'],b['z_scaled'])
    assert a['height_unit']=='M$ SIGNED EXPOSURE'


def test_gamma_flip_is_gamma_even_when_surface_lens_is_delta():
    h=_landscape_frame()
    g=session_landscape(h,lens='Gamma',scale_mode='session',spot=600,symbol='SPY')
    d=session_landscape(h,lens='Delta',scale_mode='session',spot=600,symbol='SPY')
    assert g['ready'] and d['ready']
    assert np.allclose(g['gamma_flip_line'],d['gamma_flip_line'],equal_nan=True)
    assert not np.allclose(g['z'],d['z'])


def test_requested_lens_never_silently_falls_back_to_gamma():
    h=_landscape_frame().drop(columns=['option_delta_exposure_info'])
    d=session_landscape(h,lens='Delta',scale_mode='session',spot=600,symbol='SPY')
    assert d['ready'] is False
    assert 'DELTA UNAVAILABLE' in d['reason']


def test_trace_landscape_plot_smoke():
    fig=trace_landscape_3d(_landscape_frame(),spot=600,lens='Delta',scale_mode='session',symbol='SPY')
    assert len(fig.data)>=2
    assert 'ALTURA REAL M$' in str(fig.layout.title.text)
    assert any('Gamma Flip' in str(getattr(t,'name','')) for t in fig.data)


def test_v1158_ui_has_18_tabs_and_no_artificial_conviction():
    root=Path(__file__).resolve().parents[1]
    html=(root/'app/templates/dashboard.html').read_text(encoding='utf-8')
    js=(root/'app/static/app.js').read_text(encoding='utf-8')
    assert html.count('class="nav-btn')==11  # v1.37: Equity Hub stays top-level while related analytics are grouped
    assert 'data-section="netposition"' not in html
    assert 'data-section="volume"' not in html
    assert 'Positioning & Volumen' in html
    assert 'id="netPositioningChart"' in html and 'id="volumeChart"' in html
    assert 'id="operativaTraceChart"' in html
    assert 'id="opActionability"' in html
    assert 'opConviction' not in html and 'opConviction' not in js
    assert 'id="surfaceView"' in html and 'id="traceLandscapeLens"' in html
