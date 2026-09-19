from pathlib import Path
import pandas as pd

from app.core.engine import EngineConfig, enrich_options
from app.core.trace_live import build_trace_pulse
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT=Path(__file__).resolve().parents[1]

def text(rel): return (ROOT/rel).read_text(encoding='utf-8')

def _chain(symbol='QQQ'):
    ts=pd.Timestamp('2026-09-09 10:00:00')
    rows=[]
    for strike,typ,oi,vol,iv in [
        (714,'call',1200,210,.24),(714,'put',600,140,.25),
        (715,'call',2600,480,.22),(715,'put',900,260,.23),
        (716,'call',1800,370,.23),(716,'put',1400,330,.24),
    ]:
        rows.append({'timestamp':ts,'underlying_price':715.5,'strike':float(strike),'dte':5.0,'option_type':typ,'open_interest':float(oi),'volume':float(vol),'iv':float(iv)})
    return enrich_options(pd.DataFrame(rows),EngineConfig(symbol=symbol))

def test_version_and_universal_controls_present():
    assert_version_at_least('1.26.2')
    html=text('app/templates/dashboard.html')
    for token in ['traceLeftProfile','traceRightProfile','traceValueField','traceUnusualThreshold','themeToggle']:
        assert token in html
    for label in ['NET OI','NET VOL','FLOW NET']:
        assert label in html

def test_trace_profiles_expose_call_put_net_oi_and_volume_for_any_symbol():
    for symbol in ['DIA','QQQ','SPY','AAPL']:
        pulse=build_trace_pulse({'enriched':_chain(symbol)},symbol,715.5,option_events=pd.DataFrame(),asof=pd.Timestamp('2026-09-09 10:01:00'),visual_window=5)
        assert pulse['ready'] is True
        assert pulse['symbol']==symbol
        assert pulse['rows']
        for row in pulse['rows']:
            for key in ['call_oi','put_oi','net_oi','call_volume','put_volume','net_volume']:
                assert key in row
            assert row['net_oi']==row['call_oi']-row['put_oi']
            assert row['net_volume']==row['call_volume']-row['put_volume']

def test_frontend_has_generic_profile_value_map_unusual_flow_and_theme_engine():
    js=text('app/static/nextgen_terminal.js')
    for token in ['profileMode=', 'profileValue=', 'drawValueField(', 'unusualThreshold=', 'OPRA UNUSUAL', 'redrawTrace:']:
        assert token in js
    # New chart logic is data-driven and must not special-case DIA.
    block=js[js.index('const profileMode='):js.index('// TRACE DATA CONTRACT')]
    assert "symbol==='DIA'" not in block and 'symbol === "DIA"' not in block
    app=text('app/static/app.js')
    assert 'applyInstitutionalTheme' in app
    assert "localStorage.setItem('itmq-theme'" in app
    ultra=text('app/static/ultra_charts.js')
    assert 'GLOBAL_RENDERER_RECOVERY_1259' in ultra
    assert 'redrawAll:' in ultra

def test_full_available_opra_print_window_is_sent_to_trace_visual_layer():
    py=text('app/core/nextgen_terminal.py')
    assert 'observed_option_prints(option_events, tx, symbol, 0, max_points=1200)' in py
    assert '"institutional_visual_system": True' in py
    assert '"unusual_flow_overlay": "OPRA_OBSERVED_ONLY"' in py
