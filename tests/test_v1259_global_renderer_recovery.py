from pathlib import Path
import pandas as pd
from app.core.institutional_modules import net_drift_pro_figure, flow_unusual_pro_figure
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8')

def test_version_and_global_renderer_contract():
    assert_version_at_least('1.26.2')
    js=text('app/static/nextgen_terminal.js')
    for token in ['hideTip(){','drawInteractionHUD(','drawSurfaceDiagnostics(','renderLayer(name,paint)','renderLayerErrorSeen']:
        assert token in js
    assert 'GLOBAL_RENDERER_RECOVERY_1259' in text('app/static/ultra_charts.js')

def test_missing_interaction_methods_can_no_longer_abort_compose():
    js=text('app/static/nextgen_terminal.js')
    assert "catch(e){const key=`${this.canvas?.id||'trace'}:${name}" in js
    assert 'this.composeLayers();' in js

def test_mini_panels_have_explicit_empty_states_instead_of_blank_canvas():
    js=text('app/static/nextgen_terminal.js')
    assert 'miniEmptySpec(' in js
    assert 'OPRA LIVE · 0 PRINTS' in js
    assert 'NET DRIFT · SIN HISTORIAL SUFICIENTE' in js
    assert 'DEALER · ESPERANDO PULSOS' in js

def test_native_time_series_support_honest_session_gap_bridges():
    js=text('app/static/ultra_charts.js')
    assert 'bridge_session_gaps' in js
    assert 'bridges.push' in js
    assert 'bridgeDash' in js and 'bridgeOpacity' in js

def test_net_drift_figure_marks_bridge_policy():
    sf=pd.DataFrame({
        'timestamp':pd.to_datetime(['2026-09-09 09:30:00','2026-09-09 09:30:15','2026-09-09 11:00:00']),
        'call_delta':[1e6,1.1e6,1.4e6],'put_delta':[-.8e6,-.9e6,-1.1e6],
        'net_delta':[.2e6,.2e6,.3e6],'net_gex':[2e6,2.1e6,2.4e6],'spot':[525,525.2,526]
    })
    fig=net_drift_pro_figure({'enriched':pd.DataFrame()},pd.DataFrame(),symbol='QQQ',session_frame=sf)
    assert fig.layout.meta['bridge_session_gaps'] is True
    assert fig.layout.meta['connectgaps'] is False

def test_flow_figure_marks_bridge_policy():
    h=pd.DataFrame({'timestamp':pd.to_datetime(['2026-09-09 09:30:00','2026-09-09 09:31:00']),'underlying_price':[525,525.2]})
    fig=flow_unusual_pro_figure(pd.DataFrame(),h,{},None,'QQQ')
    assert fig.layout.meta['bridge_session_gaps'] is True
