from pathlib import Path
import sqlite3
import pandas as pd

from app.core.dealer_microstructure import attach_causal_underlying, classify_option_trade, enrich_option_packages, microstructure_quality
from app.core.hedge_confirmation import hedge_flow_confirmation
from app.core.dealer_intelligence import SyntheticInventoryBook, dealer_intelligence
from app.core.engine import EngineConfig, enrich_options
from app.core.nextgen_terminal import build_quant_surface_payload, observed_option_prints


def _chain():
    ts=pd.Timestamp('2026-09-08 10:00:00')
    rows=[]
    for dte,exp in [(1.0,'2026-09-09'),(5.0,'2026-09-13')]:
        for strike,typ,oi,vol,iv in [(99,'call',1000,30,.24),(100,'call',2000,50,.22),(100,'put',900,40,.23),(101,'put',1500,45,.25)]:
            rows.append({'timestamp':ts,'underlying_symbol':'DIA','underlying_price':100.0,'strike':float(strike),'dte':dte,
                         'expiration_date':exp,'option_type':typ,'open_interest':float(oi),'volume':float(vol),'iv':float(iv),
                         'contract_symbol':f'DIA{exp.replace("-","")}{typ[0].upper()}{int(strike*1000):08d}'})
    return enrich_options(pd.DataFrame(rows),EngineConfig(symbol='DIA'))


def _trade(ts='2026-09-08 10:01:00', contract='DIA20260909C00100000', exchange='CBOE', aggressor='BUY', contracts=120):
    return {'timestamp':pd.Timestamp(ts),'received_at':pd.Timestamp(ts)+pd.Timedelta(milliseconds=40),'underlying_symbol':'DIA',
            'contract_symbol':contract,'expiration_date':'2026-09-09','strike':100.0,'option_type':'call',
            'trade_price':1.20,'bid':1.18,'ask':1.20,'contracts':float(contracts),'premium':float(contracts)*120.0,
            'aggressor':aggressor,'direction_sign':1 if aggressor=='BUY' else -1,'aggressor_confidence':.98,
            'classification_method':'CAUSAL_NBBO_ASK' if aggressor=='BUY' else 'CAUSAL_NBBO_BID','nbbo_synced':True,
            'quote_age_ms':20.0,'quote_quality':'HIGH','exchange':exchange,'open_interest':1000.0,'daily_volume':40.0,
            'underlying_price':100.0,'model_delta':.55,'model_gamma':.06,'calc_vanna':.1,'calc_charm':-.02,'seq':1}


def test_causal_underlying_uses_last_trade_before_option_print_only():
    t=pd.Timestamp('2026-09-08 10:00:00')
    ev=pd.DataFrame([_trade(t)])
    ticks=pd.DataFrame([
        {'timestamp':t-pd.Timedelta(milliseconds=25),'price':99.95,'size':2},
        {'timestamp':t+pd.Timedelta(milliseconds=1),'price':100.25,'size':2},
    ])
    out=attach_causal_underlying(ev,ticks)
    assert out.loc[0,'underlying_price']==99.95
    assert out.loc[0,'underlying_price_source']=='SIP_CAUSAL_TRADE'
    assert bool(out.loc[0,'underlying_sync']) is True


def test_causal_nbbo_classification_and_stale_fallback():
    r=classify_option_trade(1.20,1.18,1.20,quote_age_ms=25,prev_trade_price=1.19)
    assert r.aggressor=='BUY' and r.nbbo_synced is True and r.method=='CAUSAL_NBBO_ASK' and r.confidence>=.9
    stale=classify_option_trade(1.20,1.18,1.20,quote_age_ms=5000,prev_trade_price=1.19)
    assert stale.aggressor=='BUY' and stale.method=='TICK_RULE' and stale.nbbo_synced is False and stale.confidence<=.4


def test_package_candidate_detection_is_explicit_not_ground_truth():
    base=pd.Timestamp('2026-09-08 10:00:00')
    rows=[]
    # Same contract, same aggressor, multiple venues within 250ms -> sweep candidate.
    for ms,venue in [(0,'CBOE'),(100,'ISE')]:
        r=_trade(base+pd.Timedelta(milliseconds=ms),exchange=venue,contracts=60);rows.append(r)
    # Two distinct, similar-size legs within 75ms -> multi-leg candidate.
    a=_trade(base+pd.Timedelta(seconds=1),contract='DIA20260909C00100000',exchange='CBOE',contracts=80)
    b=_trade(base+pd.Timedelta(seconds=1,milliseconds=40),contract='DIA20260909C00101000',exchange='CBOE',aggressor='SELL',contracts=75);b['strike']=101.0
    rows += [a,b]
    out=enrich_option_packages(pd.DataFrame(rows))
    assert 'SWEEP_CANDIDATE' in set(out['package_type'])
    assert 'MULTI_LEG_CANDIDATE' in set(out['package_type'])
    assert all('CANDIDATE' in x or x=='SINGLE' for x in out['package_type'])


def test_microstructure_quality_rewards_synced_nbbo_and_never_calls_it_probability():
    df=pd.DataFrame([_trade(),_trade('2026-09-08 10:01:01',exchange='ISE')])
    q=microstructure_quality(df)
    assert q['quote_sync_coverage_pct']==100.0
    assert q['high_conf_aggressor_pct']==100.0
    assert 0 <= q['score'] <= 100
    assert 'not trade probability' in q.get('note','').lower()


def test_hedge_confirmation_uses_real_futures_when_present_and_does_not_attribute_dealer():
    base=pd.Timestamp('2026-09-08 10:00:00')
    fut=pd.DataFrame([{'timestamp':base+pd.Timedelta(seconds=i),'price':40000+i,'size':10,'signed_volume':10} for i in range(20)])
    und=pd.DataFrame([{'timestamp':base+pd.Timedelta(seconds=i),'price':100+i*.01,'size':5,'signed_volume':5} for i in range(20)])
    r=hedge_flow_confirmation(1_000_000,underlying_ticks=und,futures_ticks=fut)
    assert r['futures_available'] is True and r['state']=='CONFIRMS'
    assert 'no prueba' in r['note'].lower() or 'no se atribuye' in r['note'].lower()


def test_old_dealer_events_schema_migrates_without_reset(tmp_path):
    db=tmp_path/'legacy.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE dealer_events(event_id TEXT PRIMARY KEY, symbol TEXT, contract_symbol TEXT, timestamp TEXT, contracts REAL, aggressor TEXT, open_probability REAL, inventory_change REAL, hedge_notional REAL, confidence REAL)')
    SyntheticInventoryBook(db)
    with sqlite3.connect(db) as c:
        cols={r[1] for r in c.execute('PRAGMA table_info(dealer_events)').fetchall()}
    assert {'dealer_delta_change_shares','package_id','package_type','classification_method','nbbo_synced'} <= cols


def test_dealer_intelligence_returns_estimated_field_quality_and_inventory_shift(tmp_path):
    chain=_chain(); ev=pd.DataFrame([_trade()])
    ticks=pd.DataFrame([{'timestamp':pd.Timestamp('2026-09-08 10:00:30')+pd.Timedelta(seconds=i),'price':100+i*.01,'size':10,'signed_volume':10} for i in range(30)])
    out=dealer_intelligence(chain,ev,'DIA',tmp_path,100.0,underlying_ticks=ticks)
    assert out['state'] in {'ESTIMATED','COLLECTING'}
    assert out['dealer_field'] in {'LONG_GAMMA','SHORT_GAMMA','NEUTRAL','UNKNOWN'}
    assert out['flow_confirmation']['futures_available'] is False
    assert 'microstructure_quality' in out and 'packages' in out and 'inventory_shift' in out
    assert out['microstructure_quality']['underlying_sync_coverage_pct'] == 100.0
    assert out.get('dealer_state_is_probability',False) is False
    assert 'ESTIMATED DEALER INVENTORY' in out.get('note','')


def test_hedge_surface_prefers_persistent_inventory_surface():
    gd={'enriched':_chain(),'spot':100.0}
    dealer={'confidence':80,'hedge_pressure':{'net_15m':999999},'inventory_surface':[
        {'strike':100.0,'expiration_date':'2026-09-09','hedge_to_neutral_notional':-123456.0},
        {'strike':101.0,'expiration_date':'2026-09-13','hedge_to_neutral_notional':65432.0},
    ]}
    p=build_quant_surface_payload(symbol='DIA',gd=gd,dealer=dealer,calibration={})
    assert p['ready'] and p['q_is_probability'] is False
    flat=[v for row in p['fields']['Hedge'] for v in row]
    assert -123456.0 in flat and 65432.0 in flat


def test_trace_option_print_provenance_reaches_renderer():
    ev=pd.DataFrame([dict(_trade(),package_type='SWEEP_CANDIDATE',package_id='SW-1')])
    ticks=pd.DataFrame([{'timestamp':pd.Timestamp('2026-09-08 10:00:59'),'price':100.0,'size':1,'signed_volume':1}])
    out=observed_option_prints(ev,ticks,'DIA',tail_minutes=0)
    assert out and out[0]['package_type']=='SWEEP_CANDIDATE'
    assert out[0]['classification_method'].startswith('CAUSAL_NBBO')
    assert out[0]['nbbo_synced'] is True and out[0]['quote_quality']=='HIGH'


def test_institutional_ui_and_dealer_docs_are_shipped():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    css=Path('app/static/app.css').read_text(encoding='utf-8')
    js=Path('app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert 'institutional-state-grid' in html and 'dealer-quality-strip' in html
    assert 'DEALER FIELD · EST.' in html and 'INVENTORY SHIFT' in html and 'FLOW CONFIRMATION' in html
    assert 'v1.21.0 · INSTITUTIONAL TERMINAL RECONSTRUCTION' in css
    assert 'wireBuf' in js and 'GPU mesh' in js
    spec=Path('docs/DEALER_INTELLIGENCE_SPEC.md').read_text(encoding='utf-8')
    bridge=Path('docs/INSTITUTIONAL_DATA_BRIDGE.md').read_text(encoding='utf-8')
    assert 'ESTIMATED DEALER INVENTORY' in spec and 'causal NBBO' in spec
    assert 'OPRA trade + NBBO causal pairing' in bridge and 'Dealer-private data boundary' in bridge


def test_research_payload_persists_dealer_intelligence_context():
    service=Path('app/service.py').read_text(encoding='utf-8')
    assert '"dealer_intelligence":{"state":dealer_diag.get("state")' in service
    assert '"microstructure_quality":dealer_diag.get("microstructure_quality")' in service
    assert '"packages":dealer_diag.get("packages")' in service

def test_option_stream_pairs_trade_only_with_preceding_event_time_quote():
    from app.core.option_stream import LiveOptionFlowStream
    s=LiveOptionFlowStream(); sym='DIA_OPT'
    t=pd.Timestamp('2026-09-08 10:00:00')
    s._quote_history[sym]=__import__('collections').deque([
        {'timestamp':t-pd.Timedelta(milliseconds=20),'bid':1.0,'ask':1.1},
        {'timestamp':t+pd.Timedelta(milliseconds=5),'bid':1.1,'ask':1.2},
    ],maxlen=256)
    q=s._causal_quote(sym,t)
    assert pd.Timestamp(q['timestamp']) == t-pd.Timedelta(milliseconds=20)
    assert q['ask']==1.1
