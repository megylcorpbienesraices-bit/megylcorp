from pathlib import Path

import numpy as np
import pandas as pd

from app.core.scenario_engine import build_quant_scanner
from app.core.research_store import ResearchStore
from app.core.trace_analytics import tape_confirmation


def _result():
    strikes=np.arange(596.0,605.0,1.0)
    n=len(strikes)
    cur=pd.DataFrame({
        "strike":strikes,
        "open_interest":np.linspace(3000,9000,n),
        "option_volume":np.linspace(800,2600,n),
        "gross_gex":np.linspace(1.0e7,4.0e7,n),
        "abs_delta_exposure":np.linspace(5.0e5,1.2e6,n),
        "gex_change_pct":np.linspace(.05,.15,n),
        "dominance_score":np.linspace(45,88,n),
        "containment_score_delta":np.linspace(55,82,n),
        "break_score_delta":np.linspace(42,72,n),
        "containment_core":np.linspace(50,78,n),
        "break_core":np.linspace(40,68,n),
        "signed_gex":np.where(strikes<600,-2.5e7,3.2e7),
        "delta_exposure":np.linspace(-7e5,9e5,n),
        "mass_pct":np.linspace(.2,.9,n),
        "intensity_pct":np.linspace(.3,.85,n),
        "turnover_pct":np.linspace(.15,.75,n),
        "net_tilt_pct":np.linspace(.25,.8,n),
        "net_tilt_ratio":np.linspace(-.2,.4,n),
    })
    return {
        "spot":600.05,
        "current_delta":cur,
        "pressure_direction":"BUY","pressure_score":76,
        "delta_pressure_direction":"BUY","delta_pressure_score":72,
        "gamma_flip":599.5,"gamma_center":600.0,"delta_center":600.2,
    }


def _ticks(sign):
    t0=pd.Timestamp("2026-09-07 10:00:00")
    size=np.full(200,8.0)
    px=600.05+np.cumsum(np.full(200,0.001*sign))
    return pd.DataFrame({
        "timestamp":[t0+pd.Timedelta(milliseconds=200*i) for i in range(200)],
        "price":px,"size":size,"signed_volume":size*sign,"aggressor_sign":sign,
    })


def _scanner(ticks):
    return build_quant_scanner(
        "DIA",_result(),{"regime":"BUY","confidence":61},
        {"regime":"STABLE","expected_move":3.0,"expected_high":603.0,"expected_low":597.0},
        {},pd.DataFrame(),pd.DataFrame(),None,{},"OPEN",live_ticks=ticks,
        regime_context={},expiry_mode="WEEK",
    )


def test_scanner_structure_is_invariant_to_opposite_live_tape():
    buy=_scanner(_ticks(1)); sell=_scanner(_ticks(-1))
    assert buy["ready"] and sell["ready"]
    for key in ("direction","scenario_type","evidence_score","bounce_score","break_score","structural_score","zone"):
        assert buy[key] == sell[key]
    assert buy["trace_aggression"]["direction"] == "BUY"
    assert sell["trace_aggression"]["direction"] == "SELL"
    assert buy["trace_aggression"]["role"] == "TIMING_ONLY"
    assert buy["trace_aggression"]["affects_scanner_score"] is False
    assert not any("TRACE" in str(x.get("label","")) for x in buy.get("reasons",[])+buy.get("contradictions",[]))


def test_tape_confirmation_exposes_visit_identity_for_research():
    tk=_ticks(1)
    now=tk["timestamp"].iloc[-1]
    c=tape_confirmation(tk,599.0,601.0,"BUY",250,confirm_fraction=.5,budget_seconds=180,now=now)
    assert c["in_zone"] is True
    assert c["zone_entry_time"] is not None
    assert c["asof_timestamp"] is not None


def test_research_store_dedupes_same_tape_state_transition(tmp_path: Path):
    rs=ResearchStore(tmp_path/"research.sqlite")
    row={
        "event_key":"DIA|2026-09-07T10:00:00|BUY|600.0000|ARMED",
        "timestamp":"2026-09-07T10:00:15","symbol":"DIA","expiry_mode":"WEEK",
        "scanner_direction":"BUY","edge_state":"ACTIONABLE","evidence":73.2,
        "zone_low":599.8,"zone_center":600.0,"zone_high":600.2,"target1":601.0,"invalidation":599.2,
        "tape_state":"ARMED","zone_entry_time":"2026-09-07T10:00:00","progress_pct":42.0,
        "signed_volume":52,"volume":210,"buy_pct":61,"seconds_in_zone":15,"seconds_remaining":165,"trades":33,
        "payload":{"role":"TIMING_ONLY"},
    }
    assert rs.append_tape_event(row) is True
    assert rs.append_tape_event(row) is False
    terminal=dict(row,event_key="DIA|2026-09-07T10:00:00|BUY|600.0000|CONFIRMED",tape_state="CONFIRMED")
    assert rs.append_tape_event(terminal) is True
    st=rs.status("DIA")
    assert st["tape_events"] == 2
    assert st["tape_terminal_events"] == 1
