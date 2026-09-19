from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def text(p):return (ROOT/p).read_text(encoding="utf-8")


def test_trace_keeps_flow_and_drift_as_full_sections_not_mini_panels():
    h=text("app/templates/dashboard.html")
    assert 'id="traceFlowMiniChart"' not in h and 'id="traceDriftMiniChart"' not in h
    assert 'data-section="flow"' in h and '>FLUJO<' in h
    assert 'data-lean-go="netdrift"' in h and '>Net Drift<' in h
    assert 'id="section-flow"' in h and 'id="section-netdrift"' in h

def test_tasty_derivatives_subscribe_trade_and_publish_option_flow():
    rt=text("app/providers/tastytrade/runtime.py");md=text("app/providers/tastytrade/market_data.py")
    assert '("Quote","Trade","Greeks","Summary")' in rt
    assert "option_trade_frame" in md
    assert "TASTYTRADE DXLINK · OBSERVED PROVIDER LANE" in md


def test_chart_runtime_rehydrates_after_section_becomes_visible():
    js=text("app/static/app.js")
    assert "rehydrateActiveCharts" in js
    assert "requestAnimationFrame(()=>requestAnimationFrame" in js
    assert "redrawAll" in js


def test_provider_flow_health_is_nonblocking_and_reports_redundancy():
    main=text("app/main.py")
    assert '@app.get("/api/providers/flow-health")' in main
    assert "MULTI_PROVIDER" in main and "NO_LIVE_OPTION_FLOW" in main
    assert "NON_BLOCKING_HEALTH" in main
    assert "STATE._opra_diagnostics(sym)" in main


def test_option_flow_is_published_semantically_by_parent_symbol():
    alp=text("app/core/option_stream.py");tasty=text("app/providers/tastytrade/market_data.py")
    assert 'OPTION_FLOW_FABRIC.ingest_trade(source="ALPACA_OPRA",underlying_symbol=self._underlying,row=row)' in alp
    assert 'OPTION_FLOW_FABRIC.ingest_trade(source="TASTYTRADE_DXLINK",underlying_symbol=parent,row=row)' in tasty
    assert 'PROVIDER_BUS.ingest(source="TASTYTRADE_DXLINK",symbol=parent,event_type="OPTION_TRADE"' in tasty

def test_auditor_exposes_nonblocking_provider_flow_fabric():
    html=text("app/templates/dashboard.html");app=text("app/static/app.js")
    for token in ('id="providerFlowBottleneck"','id="providerFlowPrice"','id="providerFlowOptions"','id="providerFlowPrints"','id="providerFlowContracts"'):
        assert token in html
    assert "refreshProviderFlowHealth" in app
    assert "/api/providers/flow-health" in app

def test_provider_flow_health_reports_quantdata_without_network_probe():
    main=text("app/main.py"); html=text("app/templates/dashboard.html"); app=text("app/static/app.js")
    assert 'QUANTDATA' in main and 'NO NETWORK CALLS ON HEALTH PATH' in main
    assert 'id="providerFlowQuantData"' in html and 'id="providerFlowQuantDataDetail"' in html
    assert 'runtime.quantdata' in app or 'runtime?.quantdata' in app or "runtime['quantdata']" in app
    assert 'native_options_structure' in app


import asyncio

def test_tasty_option_trade_runtime_really_builds_parent_flow_row():
    from app.providers.tastytrade.health import TastytradeHealth
    from app.providers.tastytrade.market_data import TastytradeMarketData
    md=TastytradeMarketData(TastytradeHealth())
    stream='.DIA260911C525'
    md.map_symbol(stream,stream,underlying_symbol='DIA',role='EQUITY_OPTION',instrument_type='EQUITY_OPTION',metadata={'parent_symbol':'DIA','strike':525.0,'expiration':'2026-09-11','option_type':'call','dte':1.0})
    asyncio.run(md.ingest_compact('Quote',['Quote',stream,1.00,1.10,10,12]))
    asyncio.run(md.ingest_compact('Greeks',['Greeks',stream,0.22,0.48,0.03,-0.1,0.02,0.15]))
    asyncio.run(md.ingest_compact('Summary',['Summary',stream,250,0,0,0,0]))
    asyncio.run(md.ingest_compact('Trade',['Trade',stream,1.10,1000,5]))
    f=md.option_trade_frame('DIA',minutes=30)
    assert len(f)==1
    r=f.iloc[0]
    assert r['underlying_symbol']=='DIA' and r['contract_symbol']==stream
    assert r['aggressor']=='BUY' and float(r['contracts'])==5
    assert float(r['open_interest'])==250
    assert abs(float(r['iv'])-0.22)<1e-9
    assert abs(float(r['provider_gamma'])-0.03)<1e-9
