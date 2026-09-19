from __future__ import annotations

from datetime import datetime, timedelta, timezone, date
from pathlib import Path
import json

import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least


ROOT = Path(__file__).resolve().parents[1]
def _chain(ts: str, exp1: str = "2026-09-10", exp2: str = "2026-09-18", shift: float = 0.0) -> pd.DataFrame:
    rows=[]
    for exp in (exp1, exp2):
        for strike in (530.0, 532.0, 534.0):
            for typ, sg, dd in (("call", 1.0, 1.0),("put", -1.0, -1.0)):
                rows.append({
                    "timestamp":ts,"expiration_date":exp,"strike":strike,"option_type":typ,
                    "open_interest":1000+(strike-530)*50,"volume":250+(strike-530)*10,
                    "underlying_price":532.0,"contract_multiplier":100.0,
                    "signed_gex_proxy":sg*(1000000.0+(strike-532.0+shift)*120000.0),
                    "option_delta_exposure_info":dd*(700000.0+(strike-532.0+shift)*85000.0),
                    "calc_vanna":sg*(0.015+shift*0.001),"calc_charm":dd*(0.008+shift*0.001),
                })
    return pd.DataFrame(rows)


def test_version_and_product_marker_are_12517():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
def test_provider_bus_uses_bounded_event_time_cohort_not_arrival_order():
    from app.core.provider_bus import UnifiedProviderBus
    b=UnifiedProviderBus();now=datetime.now(timezone.utc);sym="SYNC17"
    b.ingest(source="ALPACA",symbol=sym,event_type="QUOTE",values={"bid":100.00,"ask":100.02},timestamp=now,received_at=now+timedelta(milliseconds=20),source_seq=10)
    b.ingest(source="TASTYTRADE",symbol=sym,event_type="QUOTE",values={"bid":99.98,"ask":100.00},timestamp=now-timedelta(seconds=1),received_at=now+timedelta(milliseconds=25),source_seq=11)
    s=b.snapshot(sym)
    assert s["time_sync"]=="BOUNDED_EVENT_TIME_COHORT"
    assert s["sync_window_ms"]==350.0
    assert "ALPACA" in s["synchronized_providers"]
    assert "TASTYTRADE" not in s["synchronized_providers"]
    assert abs(s["consensus_price"]-100.01)<1e-9


def test_temporal_truth_reports_health_per_provider_and_channel():
    from app.core.provider_bus import PROVIDER_BUS, FEATURE_BUS
    from app.core.temporal_truth import TEMPORAL_TRUTH
    now=datetime.now(timezone.utc);sym="HEALTH17"
    PROVIDER_BUS.ingest(source="TASTYTRADE",symbol=sym,event_type="QUOTE",values={"bid":50,"ask":50.02},timestamp=now,received_at=now+timedelta(milliseconds=15),source_seq=1)
    PROVIDER_BUS.ingest(source="TASTYTRADE",symbol=sym,event_type="GREEKS",values={"delta":.5,"gamma":.02},timestamp=now,received_at=now+timedelta(milliseconds=18),source_seq=2)
    FEATURE_BUS.ingest(source="QUANTDATA",symbol=sym,feature_group="OPTIONS_INTELLIGENCE",values={"directional":{"gamma":{"sign":1,"confidence":80}}},timestamp=now,received_at=now+timedelta(milliseconds=12))
    r=TEMPORAL_TRUTH.snapshot(sym)
    assert r["policy"].startswith("EVENT_TIME") and r["decision_safe"]
    tasty=next(x for x in r["provider_health"] if x["source"]=="TASTYTRADE")
    assert "QUOTE" in tasty["channels"] and "GREEKS" in tasty["channels"]
    assert tasty["channels"]["QUOTE"]["eligible"] is True


def test_expiry_intelligence_uses_actual_dates_and_never_zero_equals_0dte():
    from app.core.expiry_intelligence import build_expiry_intelligence, resolve_provider_near_expiry_buckets
    df=_chain("2026-09-10T14:30:00+00:00", "2026-09-12", "2026-09-19")
    r=build_expiry_intelligence(df,asof=date(2026,9,10))
    assert r["ready"] and r["provider_bucket_rule"]=="NEVER_HARDCODE_ZERO_AS_0DTE_OR_ONE_AS_1DTE"
    zero=next(x for x in r["buckets"] if x["bucket"]=="0DTE REAL")
    assert zero["contracts"]==0
    alias=resolve_provider_near_expiry_buckets(min_dte=2,second_min_dte=9)
    assert alias["provider_bucket_zero"]["actual_dte"]==2 and alias["provider_bucket_one"]["actual_dte"]==9


def test_profile_engine_is_universal_and_does_not_silently_fallback_to_gamma():
    from app.core.profile_engine import build_profile_bundle, build_profile
    df=_chain("2026-09-10T14:30:00+00:00")
    b=build_profile_bundle(df)
    assert b["ready"]
    for m in ["Gamma","Delta","Vanna","Charm","OI","Net OI","Volume","Net Volume"]:
        assert b["profiles"][m]["ready"], m
    bad=build_profile(df,"NOT_A_METRIC")
    assert bad["ready"] is False and "unsupported profile metric" in bad["reason"]


def test_derivatives_intelligence_separates_observed_and_model_proxy():
    from app.core.derivatives_intelligence import build_derivatives_intelligence
    h=pd.concat([_chain("2026-09-10T14:30:00+00:00",shift=0),_chain("2026-09-10T14:31:00+00:00",shift=.4)],ignore_index=True)
    obs={"available":True,"source":"EXTERNAL_TEST","gex_flow":2.0,"dex_flow":-1.0,"order_flow_convexity":.2,
         "provider_native_structure":{"net_vanna":3.0,"net_charm":-2.0,"net_gex":4.0,"net_dex":-5.0}}
    r=build_derivatives_intelligence(h,observed=obs)
    assert r["model"]["ready"] and r["model"]["authority"]=="ITM_MODEL"
    assert r["observed"]["authority"]=="OBSERVED_PROVIDER_NATIVE"
    assert r["observed"]["net_vanna"]==3.0 and r["observed"]["net_charm"]==-2.0
    assert "NO_PROPRIETARY_FORMULA_CLONING" in r["policy"]
    assert "convexity_flow_proxy" in r["model"]


def test_structural_intelligence_has_migration_tilt_velocity_acceleration_and_proximity():
    from app.core.structural_intelligence import StructuralIntelligence
    s=StructuralIntelligence();base=datetime(2026,9,10,14,30,tzinfo=timezone.utc)
    out=None
    for i,shift in enumerate((0.0,.25,.6)):
        df=_chain((base+timedelta(minutes=i)).isoformat(),shift=shift)
        # Accumulate independent snapshots as the service history does.
        hist=pd.concat([_chain((base+timedelta(minutes=j)).isoformat(),shift=(0.0,.25,.6)[j]) for j in range(i+1)],ignore_index=True)
        out=s.update("STR17",hist,spot=532.0,extra_levels={"VWAP":532.2,"POC":531.8})
    assert out and out["ready"] and out["authority"]=="STRUCTURAL_CONTEXT_ONLY"
    for name in ("gamma","delta","oi"):
        m=out["metrics"][name]
        assert "tilt" in m and "migration_direction" in m and "velocity_strikes_per_min" in m and "acceleration_strikes_per_min2" in m
    assert out["proximity"]["confluence_count"]>=2


def test_versioned_state_changes_only_changed_component_versions():
    from app.core.versioned_market_state import VersionedMarketState
    v=VersionedMarketState();a=v.publish("V17",{"gamma":{"x":1},"delta":{"x":2}});b=v.publish("V17",{"gamma":{"x":1},"delta":{"x":3}})
    assert b["state_id"]>a["state_id"]
    assert b["component_versions"]["gamma"]==a["component_versions"]["gamma"]
    assert b["component_versions"]["delta"]==a["component_versions"]["delta"]+1
    assert b["changed_components"]==["delta"]
    assert b["consumer_contract"]=="SINGLE_COMPUTE_MULTI_CONSUMER"


def test_transport_is_benchmark_driven_and_keeps_ndjson_and_binary_hot_paths():
    from app.core.transport_benchmark import benchmark_transport, transport_policy
    p=transport_policy();assert p["control"]=="JSON" and p["large_history"]=="NDJSON_STREAM" and p["ticks"]=="ITMT_FIXED_BINARY" and p["surface"]=="ITMS_FLOAT32_BINARY"
    r=benchmark_transport(100)
    assert r["ready"] and {x["codec"] for x in r["results"]}=={"JSON","MSGPACK","ITMQ_BINARY_MSGPACK"}
    assert r["selected_generic_live"] in {"MSGPACK","ITMQ_BINARY_MSGPACK"}


def test_ndjson_route_is_progressive_cache_provider_then_binary_live_handoff():
    src=(ROOT/"app/main.py").read_text()
    assert '/api/nextgen/trace-history.ndjson' in src and 'StreamingResponse' in src
    assert '"phase":"CACHE"' in src and '"phase":"PROVIDER"' in src and 'CACHE→PROVIDER→LIVE_BINARY' in src
    assert 'application/x-ndjson' in src


def test_service_publishes_one_versioned_state_for_multiple_consumers():
    src=(ROOT/"app/service.py").read_text()
    for x in ("TEMPORAL_TRUTH.snapshot","build_expiry_intelligence","build_profile_bundle","build_derivatives_intelligence","STRUCTURAL_INTELLIGENCE.update","VERSIONED_MARKET_STATE.publish"):
        assert x in src
    for x in ('"profile_bundle"','"expiry_intelligence"','"structural_intelligence"','"derivatives_intelligence"','"versioned_market_state"'):
        assert x in src


def test_scanner_authority_remains_final_and_new_layers_are_context_only():
    d=(ROOT/"app/core/decision_intelligence.py").read_text();s=(ROOT/"app/core/structural_intelligence.py").read_text();di=(ROOT/"app/core/derivatives_intelligence.py").read_text()
    assert "SCANNER" in d.upper()
    assert "STRUCTURAL_CONTEXT_ONLY" in s and "EVIDENCE_INPUT_NOT_INDEPENDENT_DIRECTION" in s
    assert "SHADOW_DERIVATIVES_INTELLIGENCE_UNTIL_OOS_VALIDATED" in di


def test_dow_ecosystems_have_own_primary_and_dow_futures_only():
    from app.core.asset_ecosystems import ecosystem_for
    expected={"DIA":"YM","YM":"YM","MYM":"MYM","DJX":"YM","VXD":"YM"}
    for sym,fut in expected.items():
        e=ecosystem_for(sym)
        assert e["primary"]["symbol"]==sym
        assert fut in {x["product"] for x in e["futures"]}, (sym,fut)
    for retired in ("SPY","QQQ","GLD","AAPL","NVDA"):
        e=ecosystem_for(retired)
        assert e["family"]=="UNSUPPORTED" and e["futures"]==[]

def test_provider_coverage_page_exposes_channel_health_and_temporal_truth():
    main=(ROOT/"app/main.py").read_text();html=(ROOT/"app/templates/provider_coverage.html").read_text()
    assert '"temporal_truth": TEMPORAL_TRUTH.snapshot' in main and '"transport_policy": transport_policy()' in main
    assert "SALUD POR PROVEEDOR + CANAL" in html and "Temporal Truth" in html and "NO FIXED PROVIDER RANK" in html
