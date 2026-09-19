from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pandas as pd

from app.core.provider_bus import PROVIDER_BUS
from app.core.market_truth import MARKET_TRUTH
from app.core.temporal_causality import TemporalCausalityRuntime
from app.core.feature_intelligence import build_feature_intelligence
from app.core.decision_intelligence import build_decision_intelligence
from app.core.research_validation import build_research_validation
from app.core.nextgen_terminal import temporal_heatmap_history
from conftest import assert_version_at_least, assert_marker_version_at_least


ROOT=Path(__file__).resolve().parents[1]
def test_version_12516():
    assert_version_at_least('1.26.2')
def test_temporal_causality_reorders_with_watermark():
    rt=TemporalCausalityRuntime(max_lateness_ms=50,max_buffer=1000,recent=100)
    base=datetime.now(timezone.utc)-timedelta(seconds=1)
    rt.ingest(source='A',symbol='ZZZ',event_type='TRADE',values={'price':2},event_time=base+timedelta(milliseconds=100),source_seq=2)
    rt.ingest(source='B',symbol='ZZZ',event_type='TRADE',values={'price':1},event_time=base+timedelta(milliseconds=80),source_seq=1)
    rt.ingest(source='A',symbol='ZZZ',event_type='TRADE',values={'price':3},event_time=base+timedelta(milliseconds=200),source_seq=3)
    rows=[r for r in rt.recent('ZZZ') if r.get('causal_status')=='ORDERED']
    assert len(rows)>=2
    assert [r['payload']['price'] for r in rows[:2]] == [1,2]
    st=rt.status('ZZZ')
    assert 'WATERMARK' in st['policy']
    assert st['ordering'].startswith('EVENT_TIME')


def test_provider_bus_records_sequence_and_market_truth_has_no_fixed_rank():
    sym='T'+uuid.uuid4().hex[:8].upper()
    now=datetime.now(timezone.utc)
    PROVIDER_BUS.ingest(source='ALPACA_TEST',symbol=sym,event_type='QUOTE',values={'bid':100.0,'ask':100.02},timestamp=now,received_at=now,source_seq=10,event_id='a10')
    PROVIDER_BUS.ingest(source='TASTY_TEST',symbol=sym,event_type='QUOTE',values={'bid':100.01,'ask':100.03},timestamp=now,received_at=now,source_seq=20,event_id='t20')
    report=MARKET_TRUTH.snapshot(sym)
    assert report['ready'] is True
    assert report['provider_diversity'] >= 2
    assert report['composite']['price'] is not None
    assert report['policy'].startswith('NO_FIXED_PROVIDER_RANK')
    ev=PROVIDER_BUS.latest_events(sym)
    assert any(x.get('source_seq')==10 and x.get('event_id')=='a10' for x in ev)


def test_feature_intelligence_is_shadow_and_cannot_change_scanner():
    fs={'channels':{'gamma':{'sign':1,'direction':'BUY','confidence':90,'contributors':[{'quality':95,'age_ms':100}]},'flow':{'sign':-1,'direction':'SELL','confidence':50,'contributors':[{'quality':90,'age_ms':200}]}}}
    out=build_feature_intelligence(feature_snapshot=fs,market_state={'phase':'CONTAINMENT'},regime_context={},calibration={},scanner={'direction':'BUY'})
    assert out['ready'] is True
    assert out['production_weight_changes'] is False
    assert out['mode']=='SHADOW_CONTEXT_AWARE_WEIGHTING'
    assert 0 <= out['scanner_alignment'] <= 100


def test_research_validation_never_invents_probability_without_gate():
    cal={'status':'COLLECTING','sample_size':5,'sessions':1,'horizons':{'30':{'samples':5,'t1_pct':40,'avg_mfe':1.2,'avg_mae':-.5}}}
    out=build_research_validation(cal,{}, {'edge_gate':{'calibration_ready':False,'probability_t1_first':.8}})
    assert out['current_gate']['probability_t1_first'] is None
    assert 'FUTURE_PATH' in out['causal_policy']


def test_decision_intelligence_scanner_only_and_no_fake_p_up_down():
    out=build_decision_intelligence(scanner={'ready':True,'direction':'BUY','edge_gate':{'calibration_ready':False}},quant_synthesis={'action':'WAIT'},market_truth={'truth_confidence':92},feature_intelligence={'scanner_alignment':75},research_validation={},market_state={'phase':'TRANSITION'})
    assert out['direction']=='BUY'
    assert out['direction_source']=='SCANNER_ONLY'
    assert out['directional_probability']['p_up'] is None
    assert out['directional_probability']['p_down'] is None
    assert out['scenario_probability']['p_target_before_invalidation'] is None


def test_decision_intelligence_uses_calibrated_target_probability_only_when_ready():
    out=build_decision_intelligence(scanner={'ready':True,'direction':'SELL','edge_gate':{'calibration_ready':True,'probability_t1_first':.64,'expected_value_r':.18,'resolution_time':{'median_minutes':22}}},quant_synthesis={},market_truth={'truth_confidence':88},feature_intelligence={'scanner_alignment':81},research_validation={},market_state={})
    assert out['scenario_probability']['p_target_before_invalidation']==.64
    assert out['expected_value_r']==.18
    assert out['directional_probability']['p_up'] is None


def test_temporal_heatmap_preserves_separate_gamma_delta_matrices():
    t0=pd.Timestamp('2026-09-09 09:30:00')
    rows=[]
    for j,t in enumerate([t0,t0+pd.Timedelta(minutes=1),t0+pd.Timedelta(minutes=2)]):
        for strike in [99.,100.,101.]:
            rows.append({'timestamp':t,'strike':strike,'signed_gex_proxy':(strike-100)*1_000_000+j*100_000,'option_delta_exposure_info':(100-strike)*2_000_000+j*50_000})
    hm=temporal_heatmap_history({'enriched':pd.DataFrame(rows),'spot':100.},100.,visual_window=5)
    assert hm['ready'] is True
    assert len(hm['gamma_m'])==len(hm['strikes'])
    assert len(hm['gamma_m'][0])==len(hm['times'])
    assert hm['combined_semantics']=='SIGN_COHERENCE_GEOMETRIC_INTENSITY_NOT_RAW_ADDITION'
    assert hm['gamma_m'] != hm['delta_m']


def test_trace_receives_temporal_heatmap_without_removing_existing_value_map():
    js=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    assert 'drawTemporalHeatmap' in js
    assert 'this.drawTemporalHeatmap(x,r,pr)' in js
    assert 'this.drawValueField(x,r,pr)' in js
    assert 'id="traceTemporalHeatmap"' in html
    assert 'id="traceHeatmapOpacity"' in html


def test_tastytrade_passes_source_sequence_into_provider_bus():
    src=(ROOT/'app/providers/tastytrade/market_data.py').read_text(encoding='utf-8')
    assert 'source_seq=self._seq' in src


def test_research_cycle_persists_nextgen_intelligence_state():
    src=(ROOT/'app/service.py').read_text(encoding='utf-8')
    for key in ['"market_truth":self.market_truth_report','"feature_intelligence":self.feature_intelligence_report','"decision_intelligence":self.decision_intelligence_report','"research_validation":self.research_validation_report']:
        assert key in src


def test_low_latency_health_exposes_causality_and_chart_cache():
    src=(ROOT/'app/main.py').read_text(encoding='utf-8')
    assert 'live_causality' in src
    assert 'chart_data_cache' in src
    assert '/api/nextgen/causality-live' in src


def test_operativa_exposes_nextgen_intelligence_and_tasty_runtime_without_new_network_probe():
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    js=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
    main=(ROOT/'app/main.py').read_text(encoding='utf-8')
    for token in ['id="opTruthNext"','id="opCausalityNext"','id="opFeatureNext"','id="opProbNext"','id="opEvNext"','id="opChangeNext"']:
        assert token in html
    assert 'id="infraTasty"' in html
    assert 'function renderNextgenIntelligence(s)' in js
    assert 'provider_runtime' in main
    assert 'TASTYTRADE.status()' in main
    assert 'this endpoint never performs a blocking network probe' in main
