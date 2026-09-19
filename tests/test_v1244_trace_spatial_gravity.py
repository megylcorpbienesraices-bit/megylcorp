from pathlib import Path
import ast
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]


def test_v1244_static_contract_and_layout():
    js=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    css=(ROOT/'app/static/app.css').read_text(encoding='utf-8')
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    py=(ROOT/'app/core/trace_live.py').read_text(encoding='utf-8')
    assert 'STRIKE GRAVITY' in html
    assert 'trace-spatial-grid' in html and 'trace-spatial-hud' in html
    assert 'grid-template-columns:minmax(0,5.45fr)' in css
    assert 'price_grid_step' in js
    assert 'labelY' in js and 'gap=16' in js
    assert 'gravity_score' in py
    assert 'Math.random' not in js[js.find('class TraceRenderer'):js.find('function mat4Identity')]
    assert 'SCANNER_ONLY' in (ROOT/'app/core/nextgen_terminal.py').read_text(encoding='utf-8')


def test_v1244_python_syntax():
    for rel in ['app/core/trace_live.py','app/core/nextgen_terminal.py']:
        ast.parse((ROOT/rel).read_text(encoding='utf-8'))


def test_v1244_gravity_is_bounded_and_dia_grid_is_half_point():
    from app.core.trace_live import build_trace_pulse
    from app.core.nextgen_terminal import _trace_display_grid_step
    ts=pd.Timestamp('2026-09-08 10:00:00')
    rows=[]
    for strike in [527.0,527.5,528.0,528.5,529.0,530.0,531.0,532.0,533.0]:
        for typ in ['call','put']:
            rows.append(dict(timestamp=ts,underlying_price=528.0,strike=strike,iv=.22,dte=2.0,
                             open_interest=1200+(strike-527)*180,volume=140+(strike-527)*25,
                             option_type=typ,signed_gex_proxy=0.0,option_delta_exposure_info=0.0))
    gd={'enriched':pd.DataFrame(rows)}
    out=build_trace_pulse(gd,'DIA',528.1,option_events=pd.DataFrame(),asof=ts,visual_window=12)
    assert out['ready'] is True
    scores=[r['gravity_score'] for r in out['rows']]
    assert scores and all(0 <= x <= 100 for x in scores)
    assert out['strike_gravity']['active_strike'] in [r['strike'] for r in out['rows']]
    assert _trace_display_grid_step('DIA',528.0)==0.50
