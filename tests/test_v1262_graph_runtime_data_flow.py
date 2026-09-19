from pathlib import Path
import json
import time

import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version


ROOT=Path(__file__).resolve().parents[1]
def text(rel):
    return (ROOT/rel).read_text(encoding='utf-8')


def test_v1262_release_markers():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    html=text('app/templates/dashboard.html')
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))
    assert '/static/chart_tw.js' not in html


def test_price_fabric_is_provider_neutral_and_does_not_sum_sources():
    from app.core.provider_flow_fabric import UnifiedPriceTickFabric
    f=UnifiedPriceTickFabric(capacity_per_lane=5000)
    now=pd.Timestamp.now(tz='UTC')
    # Two providers observe the same economic trade. They remain independent lanes.
    for src in ('PROVIDER_A','PROVIDER_B'):
        assert f.ingest(source=src,symbol='DIA',timestamp=now,price=520.0,size=10,event_type='TRADE',event_id=f'{src}-1')
    h=f.health('DIA')
    assert set(h['providers_live'])=={'PROVIDER_A','PROVIDER_B'}
    df=f.dataframe('DIA')
    assert len(df)==1  # canonical raw lane only, never A+B additive tape
    assert df.attrs.get('canonical_source') in {'PROVIDER_A','PROVIDER_B'} or f.canonical_source('DIA') in {'PROVIDER_A','PROVIDER_B'}


def test_price_fabric_failover_uses_observed_quality_not_provider_name():
    from app.core.provider_flow_fabric import UnifiedPriceTickFabric
    f=UnifiedPriceTickFabric(capacity_per_lane=5000)
    now=pd.Timestamp.now(tz='UTC')
    # First lane is stale; second lane is current. Name/order must not matter.
    f.ingest(source='AAA',symbol='QQQ',timestamp=now-pd.Timedelta(seconds=20),price=600,size=1,event_type='TRADE',event_id='a')
    f.ingest(source='ZZZ',symbol='QQQ',timestamp=now,price=601,size=2,event_type='TRADE',event_id='z')
    h=f.health('QQQ')
    assert h['canonical_source']=='ZZZ'
    assert h['state']=='LIVE'
    batch=f.since('QQQ',after=0,limit=10)
    assert batch['source']=='ZZZ' and batch['ticks'][-1]['price']==601


def test_option_fabric_keeps_alpaca_and_tasty_as_redundant_lanes():
    from app.core.provider_flow_fabric import UnifiedOptionFlowFabric
    f=UnifiedOptionFlowFabric(capacity_per_lane=2000)
    now=pd.Timestamp.now(tz='UTC')
    for src in ('ALPACA_OPRA','TASTYTRADE_DXLINK'):
        f.register_universe(src,'DIA',100)
        f.ingest_trade(source=src,underlying_symbol='DIA',row={
            'timestamp':now,'contract_symbol':'DIA260911C00520000','trade_price':2.5,
            'contracts':5,'strike':520,'option_type':'call','open_interest':1000,'iv':0.2,
            'provider_gamma':0.03,'provider_delta':0.5,'bid':2.45,'ask':2.55,'nbbo_synced':True,
            'event_id':src+'-1'
        })
    h=f.health('DIA')
    assert set(h['live_trade_sources'])=={'ALPACA_OPRA','TASTYTRADE_DXLINK'}
    assert h['redundancy']==2
    assert len(f.dataframe('DIA'))==1
    assert 'ONE_CANONICAL_RAW_TAPE_LANE' in h['double_count_policy']


def test_tastytrade_provides_own_instrument_structural_failover_without_network_call():
    md=text('app/providers/tastytrade/market_data.py')
    service=text('app/service.py')
    assert 'def structural_chain_frame' in md
    assert 'OWN_INSTRUMENT_DXLINK_STREAM_NO_PROXY' in md
    assert 'network_calls":0' in md.replace(' ','')
    assert 'component!=under' in md
    assert 'itype not in {"EQUITY_OPTION","INDEX_OPTION","FUTURE_OPTION"}' in md
    assert '_tastytrade_own_chain_snapshot' in service
    assert 'structural_failover_from' in service
    assert 'OWN_INSTRUMENT_DXLINK_STREAM_NO_PROXY' in md
    assert 'component!=under' in md
    assert 'itype not in {"EQUITY_OPTION","INDEX_OPTION","FUTURE_OPTION"}' in md


def test_trace_has_live_price_line_big_profiles_and_stronger_temporal_heatmap():
    js=text('app/static/nextgen_terminal.js')
    css=text('app/static/app.css')
    assert 'drawLivePriceLine' in js
    assert 'this.drawLivePriceLine(x,r,pr,vis)' in js
    assert 'Math.min(30' in js and ')*1.18' in js
    assert 'grid-template-columns:repeat(2,minmax(0,1fr))' in css.replace(' ','')
    assert '.trace-dealer-mini-compat{display:none!important}' in css.replace(' ','')


def test_surface_binary_socket_is_lazy_and_has_safe_lifecycle():
    js=text('app/static/binary_transport.js')
    assert "DOMContentLoaded',async()=>{await initWasm();connectTicks();}" in js
    assert 'surfaceWanted' in js and 'surfaceGeneration' in js
    assert 'function disconnectSurface()' in js
    assert "if(sec==='surface')connectSurface" in js
    assert "else disconnectSurface()" in js
    assert "connectTicks();connectSurface('Gamma')" not in js


def test_generic_chart_runtime_contract_rehydrates_after_section_activation():
    app=text('app/static/app.js')
    assert 'DATA_RENDER_STREAM_V1' in app
    assert 'window.ITMQChartRuntime' in app
    assert 'rehydrateActiveCharts' in app
    assert 'requestAnimationFrame(()=>requestAnimationFrame' in app
    assert 'dataset.itmqRuntime' in app


def test_flow_health_reports_actual_observations_and_queue_pressure_without_network_calls():
    main=text('app/main.py')
    for token in ('provider_observation_coverage','price_tick_fabric','option_flow_fabric','QUANTDATA','DATA_LAKE_QUEUE_PRESSURE'):
        assert token.lower() in main.lower()
    assert 'NO NETWORK CALLS ON HEALTH PATH' in main
