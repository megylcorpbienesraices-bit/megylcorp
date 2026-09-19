"""Auditable decision-intelligence projection for the existing Scanner decision.

The Scanner remains the only direction authority. This module exposes uncertainty,
calibrated probability/EV when available, time-to-resolution, MFE/MAE research context,
and a small delta-of-state explanation. It never turns Evidence Score into a fake win
probability.
"""

from __future__ import annotations

from typing import Any
import math


def _f(v:Any)->float|None:
    try:
        x=float(v);return x if math.isfinite(x) else None
    except Exception:return None


def build_decision_intelligence(*, scanner:dict[str,Any]|None, quant_synthesis:dict[str,Any]|None,
                                market_truth:dict[str,Any]|None, feature_intelligence:dict[str,Any]|None,
                                research_validation:dict[str,Any]|None, market_state:dict[str,Any]|None,
                                previous:dict[str,Any]|None=None)->dict[str,Any]:
    sc=scanner or {};syn=quant_synthesis or {};truth=market_truth or {};fi=feature_intelligence or {};rv=research_validation or {};ms=market_state or {};prev=previous or {}
    direction=str(sc.get('direction') or 'WAITING').upper();gate=sc.get('edge_gate') or {};cal=bool(gate.get('calibration_ready'))
    p=_f(gate.get('probability_t1_first')) if cal else None
    ev=_f(gate.get('expected_value_r')) if cal else None
    # This is scenario-resolution probability, not generic next-tick P(up)/P(down).
    directional={"p_up":None,"p_down":None,"status":"DIRECTIONAL_MODEL_NOT_CALIBRATED"}
    scenario_prob={"p_target_before_invalidation":p,"status":"CALIBRATED" if p is not None else "COLLECTING",
                   "meaning":"Probability that Scanner T1 resolves before invalidation for the calibrated scope; not generic P(up)/P(down)."}
    hrows=rv.get('horizons') or []
    nearest=hrows[0] if hrows else {}
    rt=gate.get('resolution_time') if cal else None
    changed=[]
    prev_conf=_f(prev.get('data_confidence'));cur_conf=_f(truth.get('truth_confidence'))
    if prev_conf is not None and cur_conf is not None and abs(cur_conf-prev_conf)>=2:
        changed.append(f"Data confidence {prev_conf:.0f}→{cur_conf:.0f}")
    prev_align=_f(prev.get('feature_alignment'));cur_align=_f(fi.get('scanner_alignment'))
    if prev_align is not None and cur_align is not None and abs(cur_align-prev_align)>=5:
        changed.append(f"Feature alignment {prev_align:.0f}→{cur_align:.0f}")
    prev_ev=_f(prev.get('expected_value_r'))
    if prev_ev is not None and ev is not None and abs(ev-prev_ev)>=0.03:
        changed.append(f"EV {prev_ev:+.2f}R→{ev:+.2f}R")
    return {"ready":bool(sc.get('ready')),"direction":direction,"direction_source":"SCANNER_ONLY",
        "action":syn.get('action'),"edge_state":sc.get('edge_state'),"evidence_score":sc.get('evidence_score'),
        "data_confidence":truth.get('truth_confidence'),"feature_alignment":fi.get('scanner_alignment'),
        "market_phase":ms.get('phase'),"scenario_probability":scenario_prob,"directional_probability":directional,
        "expected_value_r":ev,"time_to_resolution":rt,"expected_excursion_research":{
            "horizon_minutes":nearest.get('minutes'),"avg_mfe":nearest.get('avg_mfe'),"avg_mae":nearest.get('avg_mae'),
            "samples":nearest.get('samples'),"status":"MEASURED" if nearest else "COLLECTING"},
        "what_changed":changed[:4],"snapshot_for_next_compare":{"data_confidence":cur_conf,
            "feature_alignment":cur_align,"expected_value_r":ev,"direction":direction},
        "uncertainty":{"market_truth":100.0-float(cur_conf or 0.0),"calibration_ready":cal,
            "model_health":syn.get('model_health'),"data_quality":syn.get('data_quality')},
        "note":"Decision Intelligence explains/quantifies the Scanner decision. It never creates an independent BUY/SELL vote."}
