from pathlib import Path
import time
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8')


def test_version_and_progressive_switch_contract():
    assert_version_at_least('1.26.2')
    service=text('app/service.py')
    assert 'def begin_asset_switch' in service
    assert 'def make_asset_warm_worker' in service
    assert 'def adopt_asset_warm_worker' in service
    assert 'asset_warmup' in service
    assert 'NO_SIGNAL_UNTIL_READY' in service


def test_asset_endpoint_does_not_wait_for_full_refresh():
    main=text('app/main.py')
    block=main[main.index('@app.post("/api/asset/select")'):main.index('@app.get("/api/replay/sessions")')]
    assert 'STATE.begin_asset_switch(target)' in block
    assert '_schedule_asset_warmup(target, epoch)' in block
    assert 'await asyncio.to_thread(STATE.set_asset' not in block
    assert 'worker.refresh, False, True' in main


def test_background_loop_does_not_duplicate_heavy_warmup():
    main=text('app/main.py')
    assert 'if warm.get("active")' in main
    assert 'Do not launch a second heavy refresh' in main


def test_browser_releases_dashboard_before_quant_hydration():
    js=text('app/static/app.js')
    block=js[js.index('async function selectAsset'):js.index('function renderDealer')]
    assert 'hydrateSelectedAsset(target,activeSymbolEpoch,switchSeq)' in block
    assert 'setAssetLoading(false)' in block
    assert 'pendingCharts=true;flushPendingQuantUI()' in js and 'Promise.allSettled([loadTraceDates(),loadTablesIfNeeded()])' in js
    assert 'assetQuantWarmup' in js
    assert 'PRECIO LIVE · ANÁLISIS ${pct}%' in js
    assert 'ANÁLISIS EN SEGUNDO PLANO' in js


def test_surface_controls_use_fast_single_panel_endpoint():
    js=text('app/static/app.js')
    assert 'async function loadSurfaceSliceFast' in js
    assert '/api/charts/surface-slice?' in js
    listeners=js[js.index("el('chainMetric').addEventListener"):js.index("const nds=el('netDriftScope')")]
    assert "surfaceSliceMetric','surfaceSliceRender" in listeners
    assert "loadSurfaceSliceFast('slice')" in listeners
    assert "loadSurfaceSliceFast('main')" in listeners
    main=text('app/main.py')
    assert '@app.get("/api/charts/surface-slice")' in main


def test_isolated_worker_does_not_own_shared_opra_universe():
    service=text('app/service.py')
    assert 'def refresh(self, fetch_flow: bool = True, isolated: bool = False, core_ready_callback=None)' in service
    assert 'if not isolated:' in service
    assert 'OPTION_STREAM.set_universe(snapshot)' in service
    assert 'ws_events = OPTION_FLOW_FABRIC.dataframe(self.symbol, 30)' in service


def test_periodic_state_refresh_finishes_late_quant_hydration():
    js=text('app/static/app.js')
    block=js[js.index('async function refreshState'):js.index('async function fullRefresh')]
    assert 'const wasWarm=assetQuantWarmup' in block
    assert 'wasWarm||pendingCharts||pendingSurfaceMain||pendingSurfaceSlice' in block and 's?.ready' in block
    assert 'flushPendingQuantUI()' in block and 'Promise.allSettled([loadTraceDates(),loadTablesIfNeeded()])' in block
    assert 'setAssetLoading(false)' in block


def test_asset_switch_commit_has_no_synchronous_quant_refresh():
    service=text('app/service.py')
    start=service.index('def begin_asset_switch')
    end=service.index('def make_asset_warm_worker', start)
    block=service[start:end]
    assert 'self.refresh(' not in block
    assert 'symbol_epoch' in block
    assert 'WARMING' in block
