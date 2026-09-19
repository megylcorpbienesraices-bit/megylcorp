from pathlib import Path
import pandas as pd

from app.core.institutional_modules import flow_unusual_pro_figure, net_drift_pro_figure

ROOT = Path(__file__).resolve().parents[1]


def test_ultra_native_flow_and_drift_have_mouse_navigation_and_persistent_view():
    js = (ROOT / 'app/static/ultra_charts.js').read_text(encoding='utf-8')
    assert "const NAVIGABLE_X=new Set(['flowProChart','netDriftChart'])" in js
    assert "addEventListener('wheel'" in js
    assert "addEventListener('pointerdown'" in js
    assert "addEventListener('dblclick'" in js
    assert 'this.viewRevision' in js
    assert 'this.spec?.layout?.uirevision' in js
    assert 'this.setXView' in js


def test_plotly_fallback_keeps_scroll_zoom_enabled():
    js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
    assert 'scrollZoom:true' in js
    assert "doubleClick:'reset+autosize'" in js


def test_flow_pro_backend_declares_pan_free_axes_and_stable_uirevision():
    fig = flow_unusual_pro_figure(
        events=pd.DataFrame(),
        history=pd.DataFrame(),
        result={},
        premarket_bars=pd.DataFrame(),
        symbol='DIA',
    )
    assert fig.layout.dragmode == 'pan'
    assert fig.layout.uirevision == 'flow-pro-DIA'
    assert fig.layout.xaxis.fixedrange is False
    assert fig.layout.yaxis.fixedrange is False


def test_net_drift_backend_declares_pan_free_axes_and_stable_uirevision():
    enr = pd.DataFrame({
        'timestamp': pd.to_datetime(['2026-09-09 09:30:00', '2026-09-09 09:31:00']),
        'option_type': ['call', 'put'],
        'option_delta_exposure_info': [1_000_000.0, -500_000.0],
        'signed_gex_proxy': [250_000.0, -125_000.0],
        'underlying_price': [528.0, 528.2],
    })
    fig = net_drift_pro_figure({'enriched': enr}, pd.DataFrame(), scope='Todas exp.', symbol='DIA')
    assert fig.layout.dragmode == 'pan'
    assert fig.layout.uirevision == 'net-drift-DIA-Todas exp.'
    assert fig.layout.xaxis.fixedrange is False
    assert fig.layout.yaxis.fixedrange is False
