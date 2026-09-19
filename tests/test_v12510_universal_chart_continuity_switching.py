from pathlib import Path
import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8')


def test_version_and_soft_stale_read_contract():
    assert_version_at_least('1.26.2')
    main=text('app/main.py')
    assert 'def _stale_read(' in main
    assert 'status_code=200' in main[main.index('def _stale_read'):main.index('@app.get("/api/state")')]
    for detail in ('STALE_SYMBOL_REQUEST','STALE_SYMBOL_EPOCH','STALE_CHART_RESULT','STALE_CHART_EPOCH'):
        assert f'_stale_read("{detail}"' in main
    # Expected read races must not be rendered as HTTP conflict errors in DevTools.
    charts=main[main.index('@app.get("/api/charts")'):main.index('@app.post("/api/expiry/select")')]
    assert 'status_code=409' not in charts


def test_atomic_browser_switch_pauses_reads_and_aborts_old_chart_request():
    js=text('app/static/app.js')
    assert 'let assetSwitchInProgress=false;' in js
    assert 'let chartFetchController=null;' in js
    sel=js[js.index('async function selectAsset'):js.index('function renderDealer')]
    assert 'assetSwitchInProgress=true' in sel
    assert 'chartFetchController?.abort?.()' in sel
    assert 'activeSymbolEpoch=-1' in sel
    assert "ensureTraceBootstrap?.(tf,target,'full_day')" in sel
    charts=js[js.index('async function loadCharts'):js.index('function renderCharts') if 'function renderCharts' in js else js.index('async function loadTables(){')]
    assert 'assetSwitchInProgress||assetQuantWarmup||activeSymbolEpoch<0' in charts and 'pendingCharts=true' in charts
    assert 'new AbortController()' in charts
    assert 'if(c?.stale)return' in charts
    assert "if(e?.name==='AbortError')return" in charts


def test_asset_switch_fast_path_does_not_force_rest_flow_queries():
    service=text('app/service.py')
    block=service[service.index('def set_asset'):service.index('def analyze_premarket')]
    assert 'self.refresh(False)' in block
    assert 'self.refresh(True)' not in block


def test_full_day_flow_history_merge_does_not_drop_alpaca_cache():
    service=text('app/service.py')
    charts=service[service.index('def charts('):service.index('def _replay_tables')]
    assert 'load_cached_stock_session_bars' in charts
    assert 'session_scope="full_day"' in charts
    assert 'pd.concat([flow_history,sh]' in charts
    assert 'pd.concat([history,sh]' not in charts
    assert 'pd.concat([flow_history,lp]' in charts
    assert 'large_print_chart(large_current,flow_history,self.symbol)' in charts


def test_net_drift_underlying_uses_dense_full_day_price_history():
    from app.core.institutional_modules import net_drift_pro_figure
    sf=pd.DataFrame({
        'timestamp':pd.to_datetime(['2026-09-09 09:30:00','2026-09-09 11:00:00']),
        'call_delta':[1_000_000,1_300_000],
        'put_delta':[-900_000,-1_100_000],
        'net_delta':[100_000,200_000],
        'net_gex':[2_000_000,2_400_000],
        'spot':[700.0,701.0],
    })
    ph=pd.DataFrame({
        'timestamp':pd.date_range('2026-09-09 09:30:00',periods=91,freq='min'),
        'underlying_price':[700+i/100 for i in range(91)],
    })
    fig=net_drift_pro_figure({'enriched':pd.DataFrame()},pd.DataFrame(),symbol='QQQ',session_frame=sf,price_history=ph)
    price=[t for t in fig.data if str(getattr(t,'name','')).upper()=='QQQ'][0]
    assert len(price.x)==91 and len(price.y)==91
    assert all(v is not None for v in price.y)
    assert fig.layout.meta['bridge_session_gaps'] is True
    assert fig.layout.meta['bridge_opacity'] >= 0.6


def test_native_renderer_only_segments_when_chart_explicitly_requests_time_gap_policy():
    js=text('app/static/ultra_charts.js')
    assert 'hasExplicitGap=Number.isFinite(Number(explicitGap))' in js
    assert "gapMs=this.xMode==='time'&&t.connectgaps!==true&&hasExplicitGap?Number(explicitGap):Infinity" in js
    assert 'bridgeOpacity' in js and 'bridgeDash' in js
    assert 'inferGapMs(t._x,explicitGap)' not in js


def test_nextgen_does_price_bootstrap_during_handoff_but_pauses_quant_reads():
    js=text('app/static/nextgen_terminal.js')
    block=js[js.index('async function refreshTrace'):js.index('async function refreshSurface')]
    assert 'ensureTraceBootstrap' in block
    assert 'NQ.symbolSwitching' in block
    assert 'await historyTask' in block
    assert 'if(p?.stale)' in block
    surf=js[js.index('async function refreshSurface'):]
    assert 'NQ.symbolSwitching' in surf and 'p?.stale' in surf and 'surfaceRequestSeq' in surf


def test_read_conflicts_remain_soft_but_write_conflicts_remain_real_409():
    main=text('app/main.py')
    # Mutation failure is still a real conflict, unlike expected stale GET races.
    select=main[main.index('@app.post("/api/asset/select")'):main.index('@app.get("/api/replay/sessions")')]
    assert 'status_code=409' in select
    assert '_stale_read' not in select
