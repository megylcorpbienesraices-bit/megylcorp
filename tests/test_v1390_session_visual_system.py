from pathlib import Path
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]

def text(rel): return (ROOT/rel).read_text(encoding='utf-8',errors='replace')

def test_london_cash_is_structural_not_red_failure():
    from app.core.session_expectations import activity_expectation
    # 2026-09-14 07:30 UTC = 03:30 NY / 08:30 London.
    x=activity_expectation('DIA', datetime(2026,9,14,7,30,tzinfo=timezone.utc))
    assert x['session_phase']=='LONDON'
    assert x['expected_price_live'] is False
    assert x['display_state']=='STRUCTURAL'
    assert x['options_expectation']=='STRUCTURAL'

def test_london_futures_still_require_live_price():
    from app.core.session_expectations import activity_expectation
    x=activity_expectation('YM', datetime(2026,9,14,7,30,tzinfo=timezone.utc))
    assert x['session_phase']=='FUTURES_SESSION'
    assert x['expected_price_live'] is True
    assert x['price_expectation']=='LIVE_REQUIRED'

def test_weekend_safe_underlying_closed_clock_covers_friday_to_monday():
    from app.core.freshness import UNDERLYING_POLICY, assess
    now=datetime(2026,9,14,7,30,tzinfo=timezone.utc).timestamp()
    friday=datetime(2026,9,11,20,0,tzinfo=timezone.utc).timestamp() # 16:00 NY Fri
    out=assess(friday,UNDERLYING_POLICY,market_open=False,now=now)
    assert out['estado'] in {'FRESH','WARN'}
    assert out['limite_s'] >= 120*3600

def test_provider_health_code_distinguishes_expected_idle_from_stale():
    js=text('app/static/app.js'); py=text('app/core/provider_flow_fabric.py')
    assert 'EXPECTED_IDLE' in py and 'STRUCTURAL' in py
    assert 'SESSION_AWARE_HEALTH' in py
    assert 'providerFlowBottleneck' in js and "dataset.health=systemState" in js
    assert "systemState==='STRUCTURAL'" in js

def test_institutional_chart_system_covers_requested_workspaces():
    html=text('app/templates/dashboard.html'); js=text('app/static/institutional_terminal.js')
    for section in ('equityhub','scanner','flow','chain','exposure','gexmatrix','positioning','vol','macro'):
        frag=html.split(f'id="section-{section}"',1)[1].split('>',1)[0]
        assert 'institutional-workspace' in frag
    for chart in ('scannerChart','flowProChart','equityHubGammaModel','equityHubMonteCarlo','volChart','skewChart','macroChart'):
        assert chart in js
    assert 'normalizeSpec' in js

def test_3d_links_reuse_surface_engine_and_iv_is_supported():
    html=text('app/templates/dashboard.html'); app=text('app/static/app.js'); ng=text('app/static/nextgen_terminal.js')
    assert 'data-quant3d="Gamma"' in html
    assert 'data-quant3d="GEX"' in html
    assert 'data-quant3d="IV"' in html
    assert "navigateSection('surface')" in app
    surface=html.split('id="surfaceMetric"',1)[1].split('</select>',1)[0]
    assert '<option>IV</option>' in surface
    assert "['IV','Gamma'" in ng

def test_flow_unusual_is_full_width_reference_terminal_without_legacy_rail():
    html=text('app/templates/dashboard.html')
    flow=html.split('id="section-flow"',1)[1].split('id="section-netdrift"',1)[0]
    assert 'flow-reference-terminal' in flow
    assert 'institutional-chart-rail' not in flow
    assert 'flow-reference-terminal' in flow
    assert 'institutional-chart-rail' not in flow
    assert 'flow-reference-toolbar' not in flow

def test_release_identity_is_current_and_at_least_1390():
    import json
    from conftest import assert_version_at_least, assert_marker_version_at_least
    assert_version_at_least('1.39.0'); assert_marker_version_at_least('1.39.0')
    v=text('VERSION.txt').strip(); prod=json.loads(text('.itm_quant_product.json'))
    assert prod['version']==v
    assert (ROOT/f'CHANGELOG_v{v}.md').exists()
    assert (ROOT/f'RELEASE_MANIFEST_v{v}.json').exists()
