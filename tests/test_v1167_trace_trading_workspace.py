from pathlib import Path
import numpy as np
import pandas as pd

from app.core.trace_analytics import price_view_range, time_view_range


def _quiet(points=1800, travel=.60, centre=530.0, seed=4):
    r=np.random.default_rng(seed)
    p=np.cumsum(r.normal(0,.012,points))
    return centre+(p-p.mean())/(p.max()-p.min())*travel


def test_auto_price_is_not_governed_by_strike_ladder_or_nearby_wall():
    px=_quiet()
    a=price_view_range(px,[],expected_move=2.4,strike_min=520,strike_max=540,mode='auto')
    b=price_view_range(px,[529,530,540],expected_move=2.4,strike_min=520,strike_max=540,mode='auto')
    legacy=price_view_range(px,[529,530,540],expected_move=2.4,strike_min=520,strike_max=540,mode='strikes')
    assert a['range']==b['range']
    assert a['price_share_pct']>30
    assert legacy['price_share_pct']<5
    assert 540.0 in b['levels_dropped']


def test_structure_mode_can_expand_to_nearby_levels_but_not_far_levels():
    px=_quiet()
    auto=price_view_range(px,[529.0,530.0,540.0],expected_move=2.4,mode='auto')
    struct=price_view_range(px,[529.0,530.0,540.0],expected_move=2.4,mode='structure')
    assert struct['span']>=auto['span']
    assert 540.0 in struct['levels_dropped']
    assert struct['range'][1] < 540


def test_time_window_is_defined_from_price_timestamps():
    ts=pd.date_range('2026-09-08 06:00',periods=7201,freq='s')
    h=time_view_range(ts,tail_minutes=60)
    q=time_view_range(ts,tail_minutes=15)
    assert 59 <= h['span_minutes'] <= 61
    assert 14 <= q['span_minutes'] <= 16
    assert h['end']==q['end']


def test_trace_legacy_plotly_functions_are_removed_and_nextgen_is_symbol_agnostic():
    src=Path('app/core/advanced_visuals.py').read_text(encoding='utf-8')
    for name in ('trace_pro','trace_fusion','trace_2d'):
        assert f'def {name}(' not in src
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert '/api/nextgen/trace?' in ng
    assert 'Strike / DIA' not in ng and '<br>DIA ' not in ng and 'PRE DIA tape proxy' not in ng


def test_trace_workspace_controls_are_connected_to_nextgen():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    js=Path('app/static/app.js').read_text(encoding='utf-8')
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    main=Path('app/main.py').read_text(encoding='utf-8')
    for token in ('traceTimeWindow','traceYFrame','traceFlowExpanded','traceFollowToggle','traceCenterPrice','traceBackLive','traceTemporalHeatmap','traceKeyLevels','traceExpectedMove'):
        assert token in html
    assert '/api/nextgen/trace?timeframe=' in ng
    assert 'tail_minutes=' in ng and '&window=' in ng
    assert "drawExpectedMove" in ng and "traceKeyLevels" in ng
    for legacy in ('trace_y_frame','trace_tail_minutes','trace_flow_expanded','trace_fusion_view','trace_forward_minutes'):
        assert legacy not in main
    assert 'Velas japonesas' in html and 'AUTO PRECIO' in html and 'ESTRUCTURA' in html
    assert 'Velas de agresión' not in html


def test_live_has_no_local_plotly_trace_replay_and_replay_uses_global_timeline():
    src=Path('app/core/advanced_visuals.py').read_text(encoding='utf-8')
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    css=Path('app/static/app.css').read_text(encoding='utf-8')
    assert 'def trace_pro(' not in src and 'def trace_fusion(' not in src
    assert 'replayTimeline' in html and 'replayStepBack' in html and 'replayStepForward' in html
    assert 'body.replay-active .replay-timeline' in css


def test_trace_layout_is_nextgen_price_dominant_and_flow_is_collapsible():
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    css=Path('app/static/app.css').read_text(encoding='utf-8')
    assert 'class TraceRenderer' in ng and 'drawCandles' in ng and 'drawPriceLine' in ng
    assert 'id="traceFlowExpanded"' in html
    assert '#section-trace #traceChart' in css and 'trace-chart-stage' in css and '72vh' in css


def test_nextgen_trace_has_one_renderer_authority_not_fusion_duplicate():
    app=Path('app/static/app.js').read_text(encoding='utf-8')
    ng=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    service=Path('app/service.py').read_text(encoding='utf-8')
    assert 'function activeTraceRenderer()' in ng
    assert 'if(c.trace)' not in app
    assert 'TRACE rendering is exclusively NextGen Canvas' in service
    assert 'trace_fusion' not in service


def test_design_system_still_owns_workspace_css_colors():
    css=Path('app/static/app.css').read_text(encoding='utf-8')
    tail=css[css.index('/* v1.18.0 · QUANT TERMINAL'):]
    assert 'var(--pos-600)' in tail and 'var(--neg-300)' in tail and 'var(--info-600)' in tail
    assert '.trace-terminal-toolbar' in tail and '.trace-terminal-shell' in tail
