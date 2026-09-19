from pathlib import Path
import ast
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version
ROOT=Path(__file__).resolve().parents[1]

def test_trace_flow_dock_exists_and_is_native():
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    css=(ROOT/'app/static/app.css').read_text(encoding='utf-8')
    ultra=(ROOT/'app/static/ultra_charts.js').read_text(encoding='utf-8')
    for token in ('section-flow','section-netdrift','section-dealer'): assert token in html
    assert 'traceFlowDock' not in html  # mini dock retired; full modules remain
    assert 'trace-flow-dock-v1250' in html and 'trace-flow-dock' in css
    assert 'const MINI=new Set' in ultra
    for token in ('traceFlowMiniChart','traceDriftMiniChart','traceDealerMiniChart'): assert token in ultra

def test_trace_linked_strike_is_hover_driven_and_crosses_all_lanes():
    js=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert "source:'TRACE_HOVER'" in js
    assert 'this.hoverStrike!=null' in js
    assert 'r.strikeRight-4' in js
    assert "LINKED STRIKE ${s.toFixed(2)}${hover?' · HOVER':' · PINNED'}" in js

def test_trace_flow_dock_uses_existing_live_pipeline_not_second_tick_socket():
    js=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    app=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
    binary=(ROOT/'app/static/binary_transport.js').read_text(encoding='utf-8')
    assert 'flowMiniSpecFromTrace' in js and 'updateTraceMiniDock' in js
    assert "NQ.lastPayload=p;withActiveTrace" in js
    assert "NQ.trace?.setPayload(p);NQ.opTrace?.setPayload(p)" not in js
    assert "window.ITMQNextGen?.setMiniSpecs?.({netDrift:c.net_drift})" in app
    assert '/ws/nextgen/ticks-bin' in binary
    assert 'new WebSocket' not in js

def test_dealer_mini_is_explicitly_estimated_and_data_backed():
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    js=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert 'DEALER FIELD · EST.' in html
    assert 'inventory_shift' in js and 'dealer_state_score' in js and 'dealerHistory' in js

def test_v1250_backend_contract_and_version_without_memory_reset():
    py=(ROOT/'app/core/nextgen_terminal.py').read_text(encoding='utf-8')
    assert '"flow_dock": True' in py and '"linked_strike_hover": True' in py
    assert_version_at_least('1.26.2')
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in (ROOT/'app/config.py').read_text(encoding='utf-8')

def test_python_syntax_v1250():
    for rel in ('app/core/nextgen_terminal.py','app/main.py','app/service.py'): ast.parse((ROOT/rel).read_text(encoding='utf-8'))
