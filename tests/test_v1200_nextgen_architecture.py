from pathlib import Path
import pandas as pd

from app.core.causality_engine import EventEnvelope, CausalityOrderer, unify_market_events
from app.core.field_normalization import normalize_related_markets
from app.core.engine import EngineConfig, enrich_options
from app.core.nextgen_terminal import build_nextgen_trace_payload, build_quant_surface_payload, candles_from_ticks


def _ticks():
    base = pd.Timestamp('2026-09-08 10:00:00')
    return pd.DataFrame([
        {'timestamp': base + pd.Timedelta(seconds=i*20), 'price': 100 + i*.05, 'size': 10+i, 'signed_volume': (10+i)*(1 if i%2==0 else -1), 'seq': i+1, 'source':'SIP'}
        for i in range(12)
    ])


def _chain():
    ts = pd.Timestamp('2026-09-08 10:00:00')
    rows=[]
    for dte in (1.0, 5.0):
        for strike, typ, oi, vol, iv in [
            (99,'call',1200,210,.24),(99,'put',600,140,.25),
            (100,'call',2600,480,.22),(100,'put',900,260,.23),
            (101,'call',1800,370,.23),(101,'put',1400,330,.24),
            (102,'call',800,110,.25),(102,'put',1900,410,.26),
        ]:
            rows.append(dict(timestamp=ts,underlying_price=100.0,strike=float(strike),dte=dte,
                             option_type=typ,open_interest=float(oi),volume=float(vol),iv=float(iv)))
    return enrich_options(pd.DataFrame(rows), EngineConfig(symbol='DIA'))


def _events():
    return pd.DataFrame([
        {'timestamp':pd.Timestamp('2026-09-08 10:01:00'),'received_at':pd.Timestamp('2026-09-08 10:01:00.300'),
         'underlying_symbol':'DIA','strike':100.0,'contracts':75.0,'premium':185000.0,'direction_sign':1,'aggressor':'ASK','seq':9},
        {'timestamp':pd.Timestamp('2026-09-08 10:00:40'),'received_at':pd.Timestamp('2026-09-08 10:01:00.400'),
         'underlying_symbol':'DIA','strike':101.0,'contracts':25.0,'premium':70000.0,'direction_sign':-1,'aggressor':'BID','seq':8},
    ])


def test_causality_orderer_uses_event_time_not_arrival_order():
    late = EventEnvelope.build(source='OPRA',symbol='DIA',event_type='OPTION_TRADE',event_time='2026-09-08T10:00:02Z',receive_time='2026-09-08T10:00:05Z',source_seq=2)
    early = EventEnvelope.build(source='SIP',symbol='DIA',event_type='TRADE',event_time='2026-09-08T10:00:01Z',receive_time='2026-09-08T10:00:06Z',source_seq=1)
    o=CausalityOrderer();o.push(late);o.push(early)
    out=o.drain_ready(flush=True)
    assert [x.event_time for x in out] == sorted([x.event_time for x in out])
    assert out[0].source == 'SIP'
    assert out[1].source == 'OPRA'


def test_unified_stream_keeps_three_clocks_and_is_deterministic():
    a=unify_market_events(symbol='DIA',price_ticks=_ticks(),option_events=_events(),process_time='2026-09-08T10:02:00Z')
    b=unify_market_events(symbol='DIA',price_ticks=_ticks().sample(frac=1,random_state=7),option_events=_events().iloc[::-1],process_time='2026-09-08T10:02:00Z')
    assert a['causal'] is True
    assert [(x['event_time'],x['source_seq'],x['event_type']) for x in a['events']] == [(x['event_time'],x['source_seq'],x['event_type']) for x in b['events']]
    assert all({'event_time','receive_time','process_time','latency_ms'} <= set(x) for x in a['events'])


def test_cross_asset_normalization_prevents_raw_scale_domination():
    out=normalize_related_markets([
        {'symbol':'LOWVOL','change_pct':0.5,'sigma_pct':0.5},
        {'symbol':'HIGHVOL','change_pct':2.0,'sigma_pct':4.0},
    ])
    rows={r['symbol']:r for r in out['rows']}
    assert rows['LOWVOL']['field'] > rows['HIGHVOL']['field']  # 1 sigma > 0.5 sigma despite smaller raw move
    assert -1 <= out['field'] <= 1
    assert 'OWN_SIGMA' in {r['normalization'] for r in out['rows']}


def test_trace_payload_separates_structure_reprice_and_observed_flow():
    gd={'enriched':_chain(),'spot':100.0,'gamma_flip':100.5,'gamma_center':100.0,'delta_center':100.25}
    scanner={'ready':True,'direction':'SELL','edge_state':'ACTIONABLE','evidence_score':84,'zone':{'low':100.1,'center':100.2,'high':100.3},'target1':99.5,'target2':99.0,'invalidation':100.7}
    state={'phase':'ACCELERATION','stability':31,'directional_context':-62,'confluence_index':82,'top_factors':['Delta acompaña','OPRA acompaña']}
    p=build_nextgen_trace_payload(symbol='DIA',gd=gd,scanner=scanner,market_state=state,ticks=_ticks(),option_events=_events(),timeframe='1m',tail_minutes=0,asof=pd.Timestamp('2026-09-08 10:02:00'))
    assert p['renderer']=='ITM_QUANT_CANVAS_2D'
    assert p['model_risk']['visible'] is True
    assert p['market_state']['probability'] is False
    assert p['profiles']['ready'] is True
    assert p['option_prints'] and all('premium' in x for x in p['option_prints'])
    assert p['causality']['ordering'].startswith('EVENT_TIME')
    assert len(p['candles']) >= 1


def test_trace_all_window_does_not_collapse_to_15_minutes():
    ticks=[];base=pd.Timestamp('2026-09-08 09:00:00')
    for i in range(121):
        ticks.append({'timestamp':base+pd.Timedelta(minutes=i),'price':100+i*.01,'size':1,'signed_volume':1,'seq':i})
    bars=candles_from_ticks(pd.DataFrame(ticks),'15m',0)
    assert len(bars) >= 8  # full two-hour input, not a hidden 15m clamp


def test_surface_payload_has_multi_field_webgl_and_q_shadow_disclosure():
    gd={'enriched':_chain(),'spot':100.0}
    p=build_quant_surface_payload(symbol='DIA',gd=gd,dealer={'confidence':70,'hedge_pressure':{'net_15m':-2.0}},calibration={})
    assert p['ready'] is True and p['renderer']=='ITM_QUANT_WEBGL'
    for key in ('IV','Gamma','Delta','Vanna','Charm','Speed','Color','GEX','DEX','Hedge','Q'):
        assert key in p['fields']
    assert p['q_is_probability'] is False
    assert p['q_production_authority'] is False
    assert 'SHADOW' in p['weights_source']
    assert p['model_risk']['visible'] is True


def test_nextgen_ui_is_native_first_and_simplified():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    js=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    appjs=Path('app/static/app.js').read_text(encoding='utf-8')
    assert html.index('/static/nextgen_terminal.js') < html.index('/static/app.js')
    assert 'ITM_QUANT_CANVAS_2D' in html and 'surfaceWebglCanvas' in html
    assert 'Gamma' in html and 'Vanna' in html and 'Charm' in html and 'Hedge' in html
    assert 'data-section="dataquant"' not in html.lower()
    assert "['traceChart','operativaTraceChart','surfaceChart']" in appjs
    assert 'class RingBuffer' in js and 'class TraceRenderer' in js and 'class SurfaceRenderer' in js
    assert 'webgl2' in js.lower()


def test_model_risk_is_explicit_in_ui_and_docs():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    policy=Path('docs/MODEL_RISK_POLICY.md').read_text(encoding='utf-8')
    assert 'IV constante · spread fijo · no es precio proyectado' in html
    assert 'MODEL RISK' in html
    assert 'IV CONSTANTE · SPREAD FIJO · NO ES PRECIO PROYECTADO' in policy


def test_required_nextgen_architecture_docs_are_shipped():
    required=['INSTITUTIONAL_DATA_BRIDGE.md','QUANT_MATH_SPEC.md','CAUSALITY_REPLAY_SPEC.md','MODEL_RISK_POLICY.md','MULTI_ASSET_SCHEMA.md','GPU_RENDERING_ARCHITECTURE.md']
    for name in required:
        p=Path('docs')/name
        assert p.exists() and p.stat().st_size > 300
    bridge=(Path('docs')/'INSTITUTIONAL_DATA_BRIDGE.md').read_text(encoding='utf-8')
    assert 'event_time' in bridge and 'receive_time' in bridge and 'process_time' in bridge
    assert 'LIVE' in bridge and 'DEGRADED' in bridge and 'FALLBACK' in bridge


def test_causality_ordering_is_in_the_live_flow_path_not_only_visualization():
    service=Path('app/service.py').read_text(encoding='utf-8')
    assert 'events=causal_sort_frame(events)' in service
    assert 'selected_events = causal_sort_frame(filter_events(events, expiry_info))' in service
    assert 'unify_market_events(symbol=self.symbol' in service
    assert '"causality": self.causality_report' in service
