from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8',errors='replace')


def _sample():
    ts=pd.date_range('2026-09-16 09:30',periods=6,freq='min')
    hist=pd.DataFrame({'timestamp':ts,'underlying_price':[500.0,500.2,500.1,500.4,500.3,500.5]})
    ev=pd.DataFrame({
        'timestamp':[ts[1],ts[3],ts[4]],'premium':[7_100_000,54_800_000,5_100_000],
        'direction_sign':[-1,-1,1],'flow_score':[88,96,83],'underlying_price':[500.2,500.4,500.3],
        'strike':[501,499,500],'option_type':['call','put','call'],'contracts':[100,250,80],
        'open_interest':[500,300,450],'daily_volume':[1000,800,700],'aggressor':['ASK','BID','ASK'],
    })
    cur=pd.DataFrame({'strike':[498,499,501,502],'signed_gex':[-2e6,-4e6,5e6,2e6],'gross_gex':[2e6,4e6,5e6,2e6]})
    return ts,hist,ev,cur


def test_v1407_flow_reference_has_price_bubbles_four_panels_and_native_levels():
    from app.core.institutional_modules import flow_unusual_pro_figure
    _,hist,ev,cur=_sample()
    fig=flow_unusual_pro_figure(ev,hist,{'spot':500.5,'current_delta':cur,'gamma_flip':500.0,'gamma_flip_crossing':True},None,'DIA')
    meta=fig.layout.meta
    assert meta['renderer_intent']=='UNUSUAL_FLOW_REFERENCE_TERMINAL'
    assert list(meta['panes'])==['PRICE','AGGRESSOR','TOTAL','NET_FLOW']
    assert meta['scanner_authority']=='SOLE_DIRECTIONAL_AUTHORITY'
    assert meta['synthetic_options_events'] is False
    roles=[str((t.meta or {}).get('role','')) for t in fig.data]
    for role in ('FLOW_EVENT','AGGRESSOR_BAR','TOTAL_BAR','NET_FLOW_BAR'):
        assert role in roles
    levels={t.name for t in fig.data if str((t.meta or {}).get('role',''))=='LEVEL_LINE'}
    assert levels=={'CALL WALL','GAMMA FLIP','PUT WALL'}


def test_v1406_gamma_flip_is_not_published_without_real_crossing():
    from app.core.institutional_modules import flow_unusual_pro_figure
    _,hist,ev,cur=_sample()
    fig=flow_unusual_pro_figure(ev,hist,{'spot':500.5,'current_delta':cur,'gamma_flip':500.0,'gamma_flip_crossing':False},None,'DIA')
    levels={t.name for t in fig.data if str((t.meta or {}).get('role',''))=='LEVEL_LINE'}
    assert 'GAMMA FLIP' not in levels
    assert {'CALL WALL','PUT WALL'} <= levels
    assert fig.layout.meta['levels']['gamma_flip'] is None


def test_v1407_owned_renderer_has_reference_panes_markers_and_price_lines():
    js=text('app/static/market_line_terminal.js')
    for token in ("label:'PRECIO'","label:'AGRESOR'","label:'TOTAL'","label:'NET FLOW'","createSeriesMarkers","addPriceLine","arrowUp","arrowDown","fuera"):
        assert token in js
    assert "'flowProChart','traceFlowProChart'" in js
    assert 'subscribeVisibleTimeRangeChange' in js and 'setVisibleRange' in js


def test_v1406_flow_ui_is_full_width_and_removes_legacy_rail():
    html=text('app/templates/dashboard.html')
    flow=html.split('id="section-flow"',1)[1].split('id="section-netdrift"',1)[0]
    assert 'flow-reference-terminal' in flow
    assert 'institutional-chart-rail' not in flow
    assert 'flow-reference-terminal' in flow
    assert 'flow-reference-toolbar' not in flow
    assert 'flow-event-table-compat' in flow and 'hidden' in flow


def test_v1407_anomalies_always_active_immediate_and_still_observational():
    main=text('app/main.py'); env=text('.env.example'); html=text('app/templates/dashboard.html'); js=text('app/static/return_anomalies.js')
    assert 'ITM_SECCION_ANOMALIAS' not in main
    assert 'ITM_SECCION_ANOMALIAS' not in env
    assert 'id="returnAnomalyLab" open' in html
    assert 'ACTIVE / COLLECTING' in html
    # La sección sigue arrancando sola y con cadencia de 3 s; desde v1.42.4 el
    # intervalo además se puede detener, en vez de quedar vivo el resto de la sesión
    # sin forma de pararlo.
    assert 'load();' in js
    assert 'setInterval(load, 3000)' in js or 'setInterval(load,3000)' in js
    assert 'clearInterval' in js and 'startPolling' in js
    assert "safeText(el.summary,'OFF')" not in js
    for forbidden in ('/order','/orders','SET_TACTICAL_ALERT'):
        assert forbidden not in js
    assert 'direccion": None' in main or '"direccion": None' in main
