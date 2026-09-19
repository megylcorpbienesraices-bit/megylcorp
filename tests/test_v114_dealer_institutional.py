import math
from pathlib import Path

import pandas as pd

from app.core.dealer_intelligence import (
    opening_closing_likelihood,
    event_hedge_impact,
    hedge_pressure,
    SyntheticInventoryBook,
)
from app.core.external_markets import external_market_context, provider_redundancy
from app.core.research_store import ResearchStore
from app.core.calibration import calibration_report


def _call_buy(ts="2026-09-04 10:00:00", strike=535.0, contracts=20):
    return {
        "timestamp":ts,"underlying_symbol":"DIA","contract_symbol":"DIA260904C00535000",
        "strike":strike,"expiration_date":"2026-09-04","option_type":"call","contracts":contracts,
        "underlying_price":535.0,"trade_price":1.25,"aggressor":"BUY","aggressor_confidence":1.0,
        "open_interest":1000,"daily_volume":120,"fallback_delta":0.52,"fallback_gamma":0.035,
        "calc_vanna":0.08,"calc_charm":-0.12,"exchange":"C",
    }


def test_opening_likelihood_is_bounded_and_explicitly_uncertain():
    x=opening_closing_likelihood(_call_buy())
    assert 20 <= x["opening_probability"] <= 80
    assert x["label"] in {"OPENING-LIKELY","CLOSING-LIKELY","UNCERTAIN"}
    assert "Inferencia" in x["note"]


def test_call_buy_dealer_counterparty_implies_buy_hedge_when_opening_likely():
    r=_call_buy(contracts=200)
    r["open_interest"]=300
    r["daily_volume"]=20
    h=event_hedge_impact(r,counterparty_share=1.0)
    assert h["opening_probability"] > 50
    assert h["estimated_dealer_contract_change"] < 0
    assert h["estimated_hedge_notional"] > 0


def test_hedge_pressure_has_multiple_windows_and_top_strikes():
    ev=pd.DataFrame([_call_buy(f"2026-09-04 10:0{i}:00",strike=535+i*.5,contracts=50+i*5) for i in range(6)])
    hp=hedge_pressure(ev)
    assert hp["state"] == "ACTIVE"
    assert set(hp["windows"]) == {"1","3","5","15"}
    assert hp["direction"] in {"BUY","SELL","NEUTRAL"}
    assert hp["top_strikes"]


def test_synthetic_inventory_is_idempotent_and_reconciles_to_chain(tmp_path: Path):
    book=SyntheticInventoryBook(tmp_path/"dealer.sqlite")
    ev=pd.DataFrame([_call_buy(contracts=500)])
    first=book.ingest(ev,1.0);second=book.ingest(ev,1.0)
    assert first["ingested"] == 1 and second["skipped"] == 1
    chain=pd.DataFrame([{"contract_symbol":"DIA260904C00535000","open_interest":10}])
    book.reconcile(chain,"DIA")
    snap=book.snapshot("DIA",535.0)
    assert not snap.empty
    assert abs(float(snap.iloc[0]["dealer_contracts"])) <= 12.5 + 1e-9


def test_external_market_layer_has_no_removed_bridges(tmp_path: Path):
    x=external_market_context("DIA",tmp_path)
    assert x["futures"] == {}
    assert x["direct_index"] == {}
    assert x["futures_status"] == "REMOVED"
    assert x["index_status"] == "REMOVED"


def test_provider_health_is_primary_alpaca_only(tmp_path: Path):
    ex=external_market_context("DIA",tmp_path)
    out=provider_redundancy({"spot":535.0,"stock_feed":"SIP","matched_snapshots":100},{"connected":True},{"connected":True,"contracts":10},ex,tmp_path)
    assert out["secondary_configured"] is False
    assert out["data_disagreement"] is False
    assert {r["name"] for r in out["sources"]} == {"ALPACA SIP","ALPACA OPRA REST","ALPACA OPRA WS"}


def test_research_store_persists_cycles(tmp_path: Path):
    r=ResearchStore(tmp_path/"research.sqlite")
    r.append_cycle({"timestamp":"2026-09-04T10:00:00","symbol":"DIA","mode":"LIVE","expiry_mode":"AUTO","spot":535,"gamma_center":535.2,"payload":{"x":1}})
    st=r.status("DIA")
    assert st["backend"] == "SQLITE/WAL"
    assert st["cycles"] == 1 and st["sessions"] == 1


def test_calibration_uses_purge_gap_and_cost_assumptions(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ITM_BACKTEST_SLIPPAGE_BPS","1")
    monkeypatch.setenv("ITM_BACKTEST_COST_BPS","2")
    for d in range(1,8):
        day=f"2026-09-{d:02d}"
        pd.DataFrame([{"timestamp":f"{day} 09:30:00","symbol":"DIA","spot":535.0,"direction":"BUY","scenario_type":"REBOTE","zone_center":535.0,"target1":536.0,"target2":537.0,"invalidation":534.0,"evidence_score":70,"structural_score":75,"state":"PRIMARY","mode":"LIVE","edge_state":"ACTIONABLE","regime":"PINNING","expiry_mode":"AUTO"}]).to_csv(tmp_path/f"scanner_history_dia_{day}.csv",index=False)
        px=[535.0+i*.08 for i in range(22)]
        pd.DataFrame({"timestamp":pd.date_range(f"{day} 09:30:00",periods=len(px),freq="min"),"underlying_price":px}).to_csv(tmp_path/f"alpaca_dia_history_{day}.csv",index=False)
    c=calibration_report(tmp_path,"DIA",horizons=(20,),min_samples=1)
    assert c["walk_forward"]["purged_sessions"] == 1
    assert math.isclose(c["execution_assumptions"]["slippage_bps"],1.0)
    assert c["cost_aware"]["cost_bps"] == 3.0


def test_counterparty_uncertainty_scales_hedge_pressure():
    ev=pd.DataFrame([_call_buy('2026-09-04 10:00:00',contracts=200),_call_buy('2026-09-04 10:01:00',contracts=200)])
    lo=hedge_pressure(ev,counterparty_share=.30)
    hi=hedge_pressure(ev,counterparty_share=1.0)
    assert lo['state']=='ACTIVE' and hi['state']=='ACTIVE'
    assert abs(hi['net_15m']) > abs(lo['net_15m'])
    ratio=abs(hi['net_15m']/lo['net_15m'])
    assert 3.0 < ratio < 3.5


def test_dow_universe_assets_are_self_contained():
    """v1.27.1: reemplaza a `test_phase2_liquid_equities_are_selectable_full_assets`.

    v1.27.0 estrechó el universo a instrumentos directos del Dow (NVDA/MSFT/META/
    AMZN/TSLA salieron). La afirmación útil ya no es 'estas equities existen' sino
    'todo activo `full` tiene cadena PROPIA y no hereda la de DIA'. El contrato
    completo del alcance está en tests/test_v1271_dow_scope_contract.py.
    """
    from app.core.assets import ASSETS
    full = [s for s, c in ASSETS.items() if c.get('full')]
    assert full, "la build debe tener al menos un activo con cadena propia"
    for sym in full:
        assert ASSETS[sym]['quant_provider'], f"{sym} es full sin quant_provider propio"


def test_source_health_exposes_logical_components(tmp_path: Path, monkeypatch):
    ex=external_market_context('DIA',tmp_path)
    out=provider_redundancy({'spot':535.0,'stock_feed':'SIP','matched_snapshots':100},{'connected':True},{'connected':True,'contracts':10},ex,tmp_path)
    names={r['component'] for r in out.get('components',[])}
    assert {'DATA SERVICE','LIVE ENGINE','QUANT ENGINE','RESEARCH ENGINE','FRONTEND'} <= names
