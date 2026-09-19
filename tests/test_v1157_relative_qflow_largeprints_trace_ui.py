"""v1.15.7 · relative Q-Flow, historical Large Prints and TRACE control hierarchy."""
import math
from pathlib import Path
import numpy as np
import pandas as pd

from app.core.flow_intelligence import (_proximity_in_sigma, _score_event, _score_event_normalized,
                                        apply_normalized_flow_scores, flow_anchor_samples)
from app.core.large_prints import _score_events, large_print_anchor_samples


def _a(lo,hi,n=5):
    return {"lo":float(np.log1p(lo)),"hi":float(np.log1p(hi)),"n":n}


def _flow_row(spot=600.0, iv=.20, dte=5.0, z=1.0, premium=500_000, gamma=.03, delta=.55):
    sigma=spot*iv*math.sqrt(dte/365.0)
    return {"premium":premium,"open_interest":5000,"contracts":250,"daily_volume":1800,
            "underlying_price":spot,"strike":spot+z*sigma,"dte":dte,"iv":iv,
            "aggressor_confidence":.9,"provider_gamma":gamma,"provider_delta":delta}


def _anchors():
    return {
        "flow_premium":_a(50_000,2_000_000),
        "flow_size_oi_ratio":_a(.001,.20),
        "flow_size_volume_ratio":_a(.005,.60),
        "flow_gamma_all":_a(.005,.20),
        "flow_delta_all":_a(.10,.95),
    }


def test_proximity_kernel_is_sigma_relative_not_fixed_dollars():
    a=_flow_row(spot=600,z=1.0); b=_flow_row(spot=45,z=1.0)
    za,pa,ra=_proximity_in_sigma(a); zb,pb,rb=_proximity_in_sigma(b)
    assert ra and rb
    assert abs(za-1.0)<1e-9 and abs(zb-1.0)<1e-9
    assert abs(pa-pb)<1e-9


def test_normalized_qflow_is_shadow_and_does_not_replace_legacy():
    r=_flow_row(); legacy=_score_event(r)
    x=pd.DataFrame([{**r,"flow_score":legacy}])
    out=apply_normalized_flow_scores(x,_anchors())
    assert float(out.iloc[0]["flow_score"])==legacy
    assert float(out.iloc[0]["flow_score_legacy"])==legacy
    assert out.iloc[0]["flow_normalized_role"]=="SHADOW_ONLY"
    assert bool(out.iloc[0]["flow_score_normalized_ready"])
    assert 0<=float(out.iloc[0]["flow_score_normalized"])<=100


def test_greek_component_no_longer_saturates_at_tiny_absolute_gamma():
    lo=_score_event_normalized(_flow_row(gamma=.02),_anchors())
    hi=_score_event_normalized(_flow_row(gamma=.15),_anchors())
    assert hi["flow_gamma_anchor_score"]>lo["flow_gamma_anchor_score"]
    assert hi["flow_sensitivity_score"]>lo["flow_sensitivity_score"]


def test_flow_anchor_samples_include_relative_families_and_bucketed_greeks():
    rows=pd.DataFrame([_flow_row(z=.2+i*.08,gamma=.01+i*.005,delta=.3+i*.02,premium=50_000+i*20_000) for i in range(8)])
    s=flow_anchor_samples(rows)
    assert len(s["flow_premium"])==8
    assert len(s["flow_size_oi_ratio"])==8
    assert "flow_gamma_all" in s and any(k.startswith("flow_gamma_unscoped_") for k in s)


def _prints(n=5):
    vals=np.linspace(800_000,2_000_000,n)
    return pd.DataFrame({"timestamp":pd.date_range("2026-09-04 09:30",periods=n,freq="min"),
                         "underlying_symbol":"DIA","price":600.0,"size":vals/600.0,
                         "notional":vals,"exchange":"N","exchange_name":"NYSE","tape":"A",
                         "conditions":"","off_exchange_confirmed":False})


def test_large_print_early_session_rank_is_shrunk_and_score_scale_is_true_100():
    x=_score_events(_prints(5),"DIA",anchor=None)
    top=x.sort_values("notional").iloc[-1]
    assert float(top["q_print_intraday_rank"])==1.0
    assert float(top["q_print"])<100.0
    assert float(x["q_print"].max())<=100.0
    assert "q_print_legacy" in x.columns
    assert str(top["q_print_status"]).startswith("COLLECTING")


def test_large_print_historical_anchor_dominates_early_then_is_auditable():
    a=_a(500_000,10_000_000,n=5)
    x=_score_events(_prints(5),"DIA",anchor=a)
    assert x["q_print_anchor_ready"].all()
    assert x["q_print_anchor_score"].notna().all()
    assert np.allclose(x["q_print_intraday_weight"],x["q_print_intraday_weight"].iloc[0])
    assert 0.30 <= float(x["q_print_intraday_weight"].iloc[0]) < .40


def test_large_print_anchor_samples_are_notional_only():
    s=large_print_anchor_samples(_prints(7))
    assert list(s)==["large_print_notional"] and len(s["large_print_notional"])==7


def test_trace_ui_exposes_only_current_nextgen_presentation_controls():
    html=Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "CONTROLES NEXTGEN" in html and "PRESENTACIÓN" in html
    for i in ("traceExpectedMove","traceTemporalHeatmap","traceKeyLevels","tracePriceStyle","traceTimeWindow","traceYFrame"):
        assert f'id="{i}"' in html
    for i in ("traceHeatScale","traceForwardMinutes","traceDealerFlow","traceTimeFlow"):
        assert f'id="{i}"' not in html


def test_scanner_flow_bonus_uses_legacy_not_shadow_score():
    from app.core.scenario_engine import _nearby_event_bonus
    e=pd.DataFrame([{"underlying_price":600.0,"direction_sign":1,"flow_score":12.0,"flow_score_normalized":99.0}])
    out=_nearby_event_bonus(600.0,e,pd.DataFrame(),.5)
    assert out["buy_flow"]==12.0
