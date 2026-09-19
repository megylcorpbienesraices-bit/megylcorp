from pathlib import Path
import pandas as pd

from app.core.engine import EngineConfig, enrich_options
from app.core.trace_live import build_trace_pulse


def _chain():
    ts = pd.Timestamp('2026-09-08 10:00:00')
    rows=[]
    for strike, typ, oi, vol, iv in [
        (99,'call',1200,210,.24),(99,'put',600,140,.25),
        (100,'call',2600,480,.22),(100,'put',900,260,.23),
        (101,'call',1800,370,.23),(101,'put',1400,330,.24),
        (102,'call',800,110,.25),(102,'put',1900,410,.26),
    ]:
        rows.append(dict(timestamp=ts,underlying_price=100.0,strike=float(strike),dte=5.0,
                         option_type=typ,open_interest=float(oi),volume=float(vol),iv=float(iv)))
    return enrich_options(pd.DataFrame(rows), EngineConfig(symbol='DIA'))


def test_live_gamma_delta_pulse_reprices_spot_but_keeps_snapshot_fields_honest():
    enriched=_chain()
    events=pd.DataFrame([{
        'timestamp':pd.Timestamp('2026-09-08 10:01:00'),'underlying_symbol':'DIA','strike':100.0,
        'contracts':75.0,'premium':185000.0,
    }])
    pulse=build_trace_pulse({'enriched':enriched},'DIA',101.0,events,asof=pd.Timestamp('2026-09-08 10:02:00'),visual_window=5)
    assert pulse['ready'] is True
    assert pulse['source']=='LIVE_SPOT_REPRICE'
    assert abs(pulse['gamma_net_change_m'])>1e-8
    assert abs(pulse['delta_net_change_m'])>1e-8
    row=next(r for r in pulse['rows'] if r['strike']==100.0)
    snap=enriched[enriched['strike']==100.0]
    assert row['volume_snapshot']==float(snap['volume'].sum())
    assert row['oi']==float(snap['open_interest'].sum())
    assert row['opra_contracts_5m']==75.0
    assert row['opra_premium_5m']==185000.0
    assert 'IV/OI' in pulse['assumptions']


def test_trace_terminal_exposes_live_profiles_without_faking_option_volume():
    live=Path('app/core/trace_live.py').read_text(encoding='utf-8')
    js=Path('app/static/app.js').read_text(encoding='utf-8')
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    # v1.37: LIVE gamma/delta and OPRA activity are data contracts rendered by NextGen,
    # not Plotly traces in advanced_visuals.py.
    assert 'LIVE_SPOT_REPRICE' in live
    assert 'volume_snapshot' in live and 'opra_contracts_5m' in live
    assert '/api/trace/pulse' in js and 'pollTracePulse' in js
    assert 'volume_snapshot' in js and 'opra_contracts_5m' in js
    assert 'opra_contracts_5m' in ng and 'gamma_m' in ng and 'delta_m' in ng


def test_trace_terminal_has_professional_interaction_controls_and_presets():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    js=Path('app/static/app.js').read_text(encoding='utf-8')
    css=Path('app/static/app.css').read_text(encoding='utf-8')
    for token in ('data-trace-preset="pro"','data-trace-preset="sniper"','data-trace-preset="structure"','data-trace-preset="flow"','traceFullscreen','traceApplyRecommended'):
        assert token in html
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert 'ResizeObserver' in js and 'toggleTraceFullscreen' in js
    assert 'ResizeObserver' in ng and 'setFollow(false)' in ng
    assert 'viewportTimeRange' in ng and 'visibleCandles' in ng
    assert '.trace-terminal-shell.trace-fullscreen' in css


def test_trace_supports_15m_end_to_end():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    main=Path('app/main.py').read_text(encoding='utf-8')
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert 'data-trace-tf="15m"' in html
    assert 'if timeframe not in {"1m", "3m", "5m", "15m"}' in main
    assert 'if timeframe not in {"1m","3m","5m","15m"}' in main
    assert "p.timeframe||'1m'" in ng


def test_migration_policy_never_scans_siblings_and_has_explicit_helper():
    src=Path('app/persistence.py').read_text(encoding='utf-8')
    helper=Path('migrate_data.py').read_text(encoding='utf-8')
    assert 'Never scan sibling directories' in src
    assert 'explicit_source' in src
    assert 'PRODUCT_ID' in src and 'PRODUCT_MARKER_NAME' in src
    assert 'Escribe MIGRAR' in helper


def test_old_benchmark_cmp_script_is_not_shipped():
    assert not Path('cmp.py').exists()


def test_plotly_legend_itemwidth_valid():
    """Any remaining Plotly layout must not ship an invalid legend.itemwidth."""
    from pathlib import Path
    src = Path("app/core/advanced_visuals.py").read_text(encoding="utf-8")
    import re
    vals = [int(x) for x in re.findall(r"itemwidth\s*=\s*(\d+)", src)]
    assert all(v >= 30 for v in vals)
    # TRACE itself is Canvas/NextGen in v1.37, so no Plotly legend is required.
    assert 'def trace_pro(' not in src and 'def trace_fusion(' not in src


def test_quant_synthesis_is_internal_reducer_not_second_direction_engine():
    from app.core.quant_synthesis import build_quant_synthesis
    scanner={
        'ready':True,'direction':'SELL','edge_state':'ACTIONABLE','evidence_score':82,
        'zone':{'low':99.8,'center':100.0,'high':100.2},'target1':99.0,'target2':98.4,'invalidation':100.6,
        'scenario_type':'REBOTE','reasons':[{'label':'Gamma Dominance','detail':'estructura firmada','value':91}],
        'contradictions':[{'label':'Flow contrario','detail':'oposición menor','value':44}],
        'edge_gate':{'calibration_ready':False,'active':False,'probability_t1_first':0.91},
    }
    tape={'confirmation':{'state':'REJECTED','progress_pct':-64}}
    out=build_quant_synthesis(scanner,{'spot':100.0},{'regime':'BUY'},{'regime':'EXPANSION'},tape,
                              spot=100.0,data_quality=88,model_health=91,
                              source_health={'ok':True},dealer={'state':'LONG'},external={'ready':True},
                              positioning={'pcr':1.1},macro={'ready':True})
    assert out['direction']=='SELL'              # Flow/dealer cannot flip Scanner.
    assert out['direction_source']=='SCANNER_ONLY'
    assert out['action_code']=='BLOCKED'        # Tape can block timing only.
    assert out['strength']==82.0                 # Scanner evidence, not invented probability.
    assert out['strength_is_probability'] is False
    assert out['probability_t1_first'] is None   # uncalibrated probability is not published.
    assert out['processed_input_count']>=10


def test_operativa_has_one_compact_quant_result_and_no_new_data_quant_section():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    assert 'RESULTADO QUANT · SÍNTESIS INTERNA' in html
    assert 'id="quantWhy"' in html and 'id="quantStrength"' in html
    assert 'data-section="dataquant"' not in html.lower()
    assert 'id="section-dataquant"' not in html.lower()


def test_trace_chart_is_before_diagnostic_cards_and_focus_mode_compacts_global_docks():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    css=Path('app/static/app.css').read_text(encoding='utf-8')
    js=Path('app/static/app.js').read_text(encoding='utf-8')
    chart=html.index('id="traceTerminalShell"')
    tape=html.index('id="traceTapePanel"')
    assert chart < tape
    assert 'DIAGNÓSTICO CUANT / CAPAS / TAPE' in html
    assert 'body.trace-focus .asset-dock' in css and 'body.trace-focus .expiry-dock' in css
    assert "document.body.classList.toggle('trace-focus',section==='trace')" in js


def test_any_manual_plotly_axis_zoom_disables_follow():
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert "this.setFollow(false)" in ng
    assert "addEventListener('wheel'" in ng
    assert "addEventListener('pointerdown'" in ng
