from pathlib import Path
import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding='utf-8')


def test_version_1256_and_product_badge():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))


def test_alpaca_sip_failure_falls_back_to_iex_display_only(monkeypatch,tmp_path):
    import app.core.alpaca_data as ad
    from app.core.alpaca_data import AlpacaSettings
    calls=[]
    def fake_get(url,s,params,timeout=20.0):
        calls.append((params.get('feed'),timeout))
        if params.get('feed')=='sip':
            raise RuntimeError('Alpaca HTTP 403: recent SIP historical not entitled')
        return {'bars':[{'t':'2026-09-09T13:30:00Z','o':710,'h':711,'l':709,'c':710.5,'v':200,'n':20,'vw':710.2}]}
    monkeypatch.setattr(ad,'_get_json',fake_get)
    monkeypatch.setattr(ad,'DATA_DIR',tmp_path)
    out=ad.fetch_stock_session_bars(AlpacaSettings('k','s','sip','opra'),symbol='QQQ',timeframe='1m',session_date='2026-09-09',session_scope='full_day',request_timeout=3.5)
    assert len(out)==1
    assert calls[0][0]=='sip' and calls[1][0]=='iex'
    assert out.attrs['used_feed']=='iex'
    assert out.attrs['fallback'] is True
    assert 'DISPLAY_FALLBACK' in out.attrs['source']
    # Provenance survives restart through the local-cache sidecar.
    cached=ad.load_cached_stock_session_bars('QQQ','1m','2026-09-09','full_day')
    assert len(cached)==1 and cached.attrs['fallback'] is True
    assert cached.attrs['used_feed']=='iex'


def test_cache_only_bootstrap_is_immediate_and_does_not_require_quant_state(monkeypatch,tmp_path):
    import app.service as svc
    import app.core.alpaca_data as ad
    state=svc.PlatformState(symbol='YM',mode='LIVE')
    frame=pd.DataFrame([{'timestamp':pd.Timestamp('2026-09-09 08:30:00'),'open':710,'high':711,'low':709,'close':710.5,'volume':20,'trades':3,'vwap':710.2}])
    frame.attrs.update({'source':'ALPACA_LOCAL_PRICE_CACHE','cache_hit':True,'used_feed':'sip','fallback':False,'diagnostic':'LOCAL_CACHE 1 bars'})
    monkeypatch.setattr(ad,'load_cached_stock_session_bars',lambda **kw: frame.copy())
    out=state.trace_session_bootstrap(symbol='DIA',timeframe='1m',session_scope='full_day',cache_only=True)
    assert out['ready'] and out['bootstrap_status']=='CACHE_READY'
    assert out['authority']=='PRICE_PRESENTATION_ONLY'
    assert out['symbol']=='DIA' and state.symbol=='YM'


def test_failed_provider_refresh_does_not_clear_visible_bootstrap_and_symbol_switch_resets():
    ng=text('app/static/nextgen_terminal.js')
    assert 'A transient historical-provider failure must NEVER erase bars' in ng
    assert 'RETAINED_HISTORY_PROVIDER_WARN' in ng
    assert 'resetSymbolContext(symbol)' in ng
    block=ng[ng.index('function beginSymbolSwitch'):ng.index('function prepareSymbolBootstrap')]
    assert 'resetSymbolContext?.(sym)' in block


def test_initial_trace_starts_history_before_heavy_quant_request():
    ng=text('app/static/nextgen_terminal.js')
    block=ng[ng.index('async function refreshTrace()'):ng.index('async function refreshSurface')]
    hist=block.index("const historyTask=ensureTraceBootstrap(tf,sym,'full_day')")
    quant=block.index('/api/nextgen/trace?')
    assert hist < quant
    assert 'if(!p?.ready){historyTask.catch(()=>{});return;}' in block


def test_nextgen_bootstrap_is_cache_first_then_provider_refresh_and_short_retry():
    ng=text('app/static/nextgen_terminal.js')
    block=ng[ng.index("async function ensureTraceBootstrap"):ng.index('function beginSymbolSwitch')]
    assert 'cache_only=true' in block
    assert 'force=true&allow_display_fallback=true' in block
    assert 'Date.now()+3000' in block
    assert block.index('cache_only=true') < block.index('force=true&allow_display_fallback=true')


def test_api_exposes_cache_only_and_display_fallback_contract():
    main=text('app/main.py')
    block=main[main.index('@app.get("/api/trace/session-bootstrap")'):main.index('@app.get("/api/nextgen/surface")')]
    assert 'cache_only: bool = False' in block
    assert 'allow_display_fallback: bool = True' in block


def test_no_synthetic_candles_and_bootstrap_diagnostic_is_visible():
    ng=text('app/static/nextgen_terminal.js')
    assert 'NO VELAS SINTÉTICAS' in ng
    assert 'bootstrap_status' in ng and 'diagnostics?.detail' in ng
    assert 'ALPACA_IEX_HISTORICAL_DISPLAY_FALLBACK' in text('app/core/alpaca_data.py')
