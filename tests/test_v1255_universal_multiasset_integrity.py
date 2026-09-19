from pathlib import Path
import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8')


def test_version_and_universal_contract_metadata():
    assert_version_at_least('1.26.2')
    # v1.27.1: era un chequeo sobre el TEXTO crudo del JSON, así que rompía en cada
    # release igual que los asserts de igualdad de versión. Se compara el valor
    # parseado contra VERSION.txt, que es el invariante que de verdad importa.
    assert_marker_version_at_least('1.26.2')
    html=text('app/templates/dashboard.html')
    assert 'SERVER READY · v{{ app_version }}' in html
    assert 'TRUE X SCALE' in html
    assert 'DIA conserva retícula' not in html


def test_alpaca_full_day_bootstrap_uses_explicit_clock_range_and_no_bar_count_semantics(monkeypatch,tmp_path):
    import app.core.alpaca_data as ad
    from app.core.alpaca_data import AlpacaSettings
    seen={}
    def fake_get(url,s,params,**kwargs):
        seen.update(params)
        return {'bars':[
            {'t':'2026-09-09T08:00:00Z','o':700,'h':701,'l':699,'c':700.5,'v':100,'n':10,'vw':700.2},
            {'t':'2026-09-09T13:30:00Z','o':710,'h':711,'l':709,'c':710.5,'v':200,'n':20,'vw':710.2},
        ]}
    monkeypatch.setattr(ad,'_get_json',fake_get)
    monkeypatch.setattr(ad,'DATA_DIR',tmp_path)
    out=ad.fetch_stock_session_bars(AlpacaSettings('k','s','sip','opra'),symbol='QQQ',timeframe='1m',session_date='2026-09-09',session_scope='full_day')
    assert len(out)==2
    assert seen['feed']=='sip' and seen['timeframe']=='1Min'
    assert seen['start'].startswith('2026-09-09T04:00:00')  # 00:00 New York during EDT
    assert seen['end'] > seen['start'] and seen['end'].endswith('Z')
    assert seen['limit']==10000  # transport ceiling only; start/end define the contract
    assert (tmp_path/'price_bootstrap'/'alpaca_qqq_2026-09-09_full_day_1m.csv').exists()


def test_trace_bootstrap_can_load_qqq_without_mutating_active_dia(monkeypatch):
    import app.service as svc
    state=svc.PlatformState(symbol='DIA',mode='LIVE')
    fake=pd.DataFrame([{'timestamp':pd.Timestamp('2026-09-09 04:00:00'),'open':700,'high':701,'low':699,'close':700.5,'volume':10,'trades':2,'vwap':700.2}])
    monkeypatch.setattr(svc.alpaca_data,'fetch_stock_session_bars',lambda **kw: fake.copy())
    # v1.27.1: QQQ salió del universo en v1.27.0, así que el bootstrap lo rechaza
    # con ASSET_NOT_SELECTABLE. El invariante VALIOSO de este test —cargar otro
    # símbolo NO debe mutar el activo en curso— se conserva usando DJX, que sí
    # está soportado. Contrato del alcance en tests/test_v1271_dow_scope_contract.py.
    rejected = state.trace_session_bootstrap(symbol='QQQ',timeframe='1m',session_scope='full_day',force=True)
    assert rejected['ready'] is False and rejected['reason'] == 'ASSET_NOT_SELECTABLE'
    assert state.symbol=='DIA', "un símbolo rechazado no puede mutar el activo activo"

    # DJX está soportado pero exige velas propias de tastytrade: con solo el mock
    # de Alpaca responde NO_OBSERVED_TASTYTRADE_CANDLES en vez de fabricarlas desde
    # DIA. Ese rechazo es la conducta correcta y el invariante que importa sigue
    # siendo la NO MUTACIÓN del activo en curso.
    out=state.trace_session_bootstrap(symbol='DJX',timeframe='1m',session_scope='full_day',force=True)
    assert state.symbol=='DIA'
    assert out['symbol']=='DJX' and out['scope']=='full_day'
    assert out['ready'] is False and out['reason']=='NO_OBSERVED_TASTYTRADE_CANDLES'

    # El activo en curso sí bootstrapea con su propio proveedor.
    own=state.trace_session_bootstrap(symbol='DIA',timeframe='1m',session_scope='full_day',force=True)
    assert own['symbol']=='DIA' and own['ready']
    assert 'QUALITY_AWARE_COMPARABLE_DATA' in out['source_priority']
    assert 'REFERENCE_WINDOW_ONLY' in {r['role'] for r in out['reference_segments']}
    assert 'missing bars remain missing' in out['segment_disclosure']


def test_atomic_symbol_switch_has_epoch_and_clears_symbol_scoped_state():
    s=text('app/service.py')
    block=s[s.index('def set_asset'):s.index('def analyze_premarket')]
    assert 'symbol_epoch' in block
    for token in ('self.macro={}','self.gamma_delta={}','self.flow_events=pd.DataFrame()','self.expiry_info={}','self.dealer_intelligence_report={}'): assert token in block
    assert 'trace_session_cache={}' in block
    main=text('app/main.py')
    sel=main[main.index('@app.post("/api/asset/select")'):main.index('@app.get("/api/replay/sessions")')]
    assert sel.index('PRICE_STREAM.set_symbol(target)') < sel.index('STATE.begin_asset_switch(target)')
    assert 'PRICE_STREAM.set_symbol(old_symbol)' in sel


def test_browser_switch_is_progressive_symbol_scoped_and_discards_stale_responses():
    app=text('app/static/app.js'); ng=text('app/static/nextgen_terminal.js')
    sel=app[app.index('async function selectAsset'):app.index('function renderDealer')]
    assert "'full_day'" in sel and 'ensureTraceBootstrap' in sel
    assert 'prepareSymbolBootstrap' in sel and 'beginSymbolSwitch' in sel
    assert 'tracePulseLast=null' in sel
    assert 'expected_symbol' in app and 'expected_epoch' in app
    assert 'STALE_' in app
    assert 'beginSymbolSwitch' in ng and 'prepareSymbolBootstrap' in ng
    assert "active&&batch&&active!==batch" in ng
    assert "tsym&&tsym!==active" in ng
    assert "e.detail?.symbol" in ng


def test_trace_pulse_filters_opra_to_active_underlying_and_has_epoch_guards():
    service=text('app/service.py'); main=text('app/main.py'); app=text('app/static/app.js')
    block=service[service.index('def trace_pulse'):service.index('@staticmethod\n    def _session_reference_segments')]
    assert 'underlying_symbol' in block and '== str(self.symbol).upper()' in block
    assert 'pulse["symbol"]' in block and 'pulse["symbol_epoch"]' in block
    ep=main[main.index('@app.get("/api/trace/pulse")'):main.index('@app.get("/api/live/ticks")')]
    assert 'expected_symbol' in ep and 'expected_epoch' in ep and 'STALE_SYMBOL_EPOCH' in ep
    poll=app[app.index('async function pollTracePulse'):app.index('async function loadCharts')]
    assert 'expected_symbol' in poll and 'expected_epoch' in poll


def test_itm_structural_flow_is_symbol_scoped_context_not_external_orderflow(tmp_path):
    from app.core.session_memory import structural_flow_frame
    from app.persistence import routed_dir
    p=routed_dir(tmp_path,'sessions')/'session_metrics_qqq_2026-09-09.csv'
    p.parent.mkdir(parents=True,exist_ok=True)
    pd.DataFrame([
        {'timestamp':'2026-09-09 10:00:00','symbol':'QQQ','expiry_mode':'ALL','spot':710,'net_gex':10_000_000,'net_delta':20_000_000,'gamma_center':710.5,'delta_center':709.5},
        {'timestamp':'2026-09-09 10:01:00','symbol':'QQQ','expiry_mode':'ALL','spot':711,'net_gex':13_000_000,'net_delta':18_000_000,'gamma_center':711.0,'delta_center':710.0},
    ]).to_csv(p,index=False)
    import app.core.session_memory as sm
    old=sm._path
    sm._path=lambda storage,symbol,day=None:p
    try:
        out=structural_flow_frame(tmp_path,'QQQ','ALL')
    finally:
        sm._path=old
    assert out['symbol']=='QQQ' and out['authority']=='PRESENTATION_CONTEXT_ONLY'
    assert out['ready'] and out['latest']['gex_flow'] is not None and out['latest']['dex_flow'] is not None
    assert -1 <= out['latest']['convexity_pressure'] <= 1
    assert 'not probability' in out['convexity_definition']



def test_internal_structural_flow_native_structure_and_opra_diagnostics_are_visibly_separate():
    html=text('app/templates/dashboard.html'); js=text('app/static/nextgen_terminal.js')
    for ident in ('opItmStructuralFlowCard','opItmGexFlow','opItmDexFlow','opItmConvexity','opItmFlowCanvas','opOpraDiagnosticCard','opOpraStatus','surfaceItmGexFlow','nativeStructureStatus','nativeStructureDetail'):
        assert f'id="{ident}"' in html
    assert 'INTERNO · CAMBIO ESTRUCTURAL POR SNAPSHOT' in html
    assert 'ESTRUCTURA NATIVA' in html
    assert 'function updateItmStructuralFlowUI' in js and 'function updateOpraDiagnostics' in js
    assert 'OPRA LIVE · 0 PRINTS' in js

def test_universal_trace_scale_not_replaced_by_fixed_plus_minus_four_percent():
    ng=text('app/static/nextgen_terminal.js')
    assets=text('app/core/instruments.py')
    assert 'priceRange' in ng or 'priceBounds' in ng or 'visible' in ng
    assert 'trace_grid_step' in assets
    assert 'spot * 0.96' not in ng and 'spot*0.96' not in ng
    assert 'spot * 1.04' not in ng and 'spot*1.04' not in ng
