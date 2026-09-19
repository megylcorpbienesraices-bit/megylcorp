import pandas as pd

from app.core.engine import EngineConfig, enrich_options
from app.core.market_state_field import build_market_state_field
from app.core.quant_synthesis import build_quant_synthesis
from app.core.trace_live import build_trace_pulse


def _chain():
    ts = pd.Timestamp('2026-09-08 10:00:00')
    rows=[]
    for strike, typ, oi, vol, iv in [
        (99,'call',1200,210,.24),(99,'put',600,140,.25),
        (100,'call',2600,480,.22),(100,'put',900,260,.23),
        (101,'call',1800,370,.23),(101,'put',1400,330,.24),
        (102,'call',800,110,.25),(102,'put',1900,410,.26),
    ]:
        rows.append(dict(timestamp=ts,underlying_price=100.0,strike=float(strike),dte=5.0,
                         option_type=typ,open_interest=float(oi),volume=float(vol),iv=float(iv)))
    return enrich_options(pd.DataFrame(rows), EngineConfig(symbol='DIA'))


def _market(gex_sign=1.0):
    e=_chain()
    gross=float(pd.to_numeric(e['gross_gex'],errors='coerce').abs().sum())
    gross_delta=float(pd.to_numeric(e['option_delta_exposure_info'],errors='coerce').abs().sum())
    return {
        'enriched':e,'spot':100.0,
        'total_signed_gex':float(gex_sign)*gross*.55,'total_gross_gex':gross,
        'net_delta_exposure':gross_delta*.45,
    }


def test_gamma_is_friction_not_direction_vote():
    scanner={'ready':True,'direction':'BUY','zone':{'low':99.8,'high':100.2}}
    flow={'bull':800000,'bear':200000,'microstructure':{'premium_acceleration_pct':40,'persistence_pct':80}}
    pos=build_market_state_field(scanner,_market(+1),flow,{'regime':'NORMAL'},{},symbol='DIA')
    neg=build_market_state_field(scanner,_market(-1),flow,{'regime':'NORMAL'},{},symbol='DIA')
    # Flipping only Gamma changes stability, not the directional context equation.
    assert pos['directional_context'] == neg['directional_context']
    assert pos['stability'] > neg['stability']
    assert pos['options']['gamma_regime']=='POSITIVE_FRICTION'
    assert neg['options']['gamma_regime']=='NEGATIVE_FEEDBACK'
    assert pos['direction_authority']=='SCANNER_ONLY'
    assert pos['confluence_is_probability'] is False


def test_higher_order_state_is_computed_and_disclosed_as_proxy():
    out=build_market_state_field({'ready':True,'direction':'BUY','zone':{'low':99.8,'high':100.2}},
                                 _market(+1),{'bull':1,'bear':0},{'regime':'NORMAL'},{},symbol='DIA')
    h=out['options']['higher_order']
    for k in ('vanna_1vol_m','charm_10m_m','speed_1pct_m','color_10m_m'):
        assert isinstance(h[k],float)
    assert h['contracts']>0
    assert 'PROXY' in h['method']


def test_internal_state_can_downgrade_timing_but_never_flip_scanner():
    scanner={
        'ready':True,'direction':'SELL','edge_state':'ACTIONABLE','evidence_score':84,
        'zone':{'low':99.8,'center':100.0,'high':100.2},'target1':99.0,'target2':98.5,'invalidation':100.7,
        'scenario_type':'REBOTE','reasons':[{'label':'Estructura','detail':'zona fuerte','value':90}],
        'contradictions':[],'edge_gate':{'calibration_ready':False,'active':False},
    }
    tape={'confirmation':{'state':'CONFIRMED','progress_pct':80}}
    sf={'role':'INTERNAL_CONTEXT_ONLY','phase':'ACCELERATION','scanner_alignment':10.0,'confluence_index':35.0,
        'top_factors':['Delta contextual contradice','Flujo OPRA cinético contradice']}
    out=build_quant_synthesis(scanner,{'spot':100.0},{},{},tape,spot=100.0,data_quality=90,model_health=90,state_field=sf)
    assert out['direction']=='SELL'
    assert out['direction_source']=='SCANNER_ONLY'
    assert out['action_code']=='CAUTION'
    assert out['market_phase']=='ACCELERATION'
    assert out['context_confluence']==35.0
    assert out['context_confluence_is_probability'] is False


def test_trace_pulse_contains_signed_opra_and_higher_order_mechanics():
    enriched=_chain()
    events=pd.DataFrame([
        {'timestamp':pd.Timestamp('2026-09-08 10:01:00'),'underlying_symbol':'DIA','strike':100.0,
         'contracts':75.0,'premium':185000.0,'direction_sign':1,'directional_premium':185000.0},
        {'timestamp':pd.Timestamp('2026-09-08 10:01:20'),'underlying_symbol':'DIA','strike':101.0,
         'contracts':25.0,'premium':70000.0,'direction_sign':-1,'directional_premium':-70000.0},
    ])
    pulse=build_trace_pulse({'enriched':enriched},'DIA',101.0,events,asof=pd.Timestamp('2026-09-08 10:02:00'),visual_window=5)
    assert pulse['ready'] is True
    assert pulse['opra_contracts_5m']==100.0
    assert pulse['opra_net_contracts_5m']==50.0
    assert pulse['opra_directional_premium_5m']==115000.0
    for k in ('vanna_1vol_m','charm_10m_m','speed_1pct_m','color_10m_m'):
        assert isinstance(pulse[k],float)
    row=next(r for r in pulse['rows'] if r['strike']==100.0)
    assert row['opra_net_contracts_5m']==75.0
    assert row['opra_directional_premium_5m']==185000.0


def test_no_new_visible_data_quant_section_was_added():
    from pathlib import Path
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8').lower()
    assert 'id="section-dataquant"' not in html
    assert 'data-section="dataquant"' not in html
    service=Path('app/service.py').read_text(encoding='utf-8')
    assert '"market_state_field":self.market_state_field' in service
