"""Compact research/validation state derived from the existing causal logs and calibration."""

from __future__ import annotations

from typing import Any
import math


def _f(v:Any)->float|None:
    try:
        x=float(v);return x if math.isfinite(x) else None
    except Exception:return None


def build_research_validation(calibration:dict[str,Any]|None, research_storage:dict[str,Any]|None,
                              scanner:dict[str,Any]|None)->dict[str,Any]:
    cal=calibration or {};rs=research_storage or {};sc=scanner or {};gate=sc.get('edge_gate') or {}
    horizons=[]
    for h,row in (cal.get('horizons') or {}).items():
        if not isinstance(row,dict):continue
        horizons.append({"minutes":int(float(h)),"samples":int(row.get('samples',0) or 0),
            "t1_pct":_f(row.get('t1_pct')),"invalidation_pct":_f(row.get('invalidation_pct')),
            "avg_mfe":_f(row.get('avg_mfe')),"avg_mae":_f(row.get('avg_mae')),
            "expectancy":_f(row.get('expectancy')),"profit_factor":_f(row.get('profit_factor'))})
    pm=cal.get('probability_model') or {}
    return {"ready":bool(cal.get('ready')),"status":cal.get('status','COLLECTING'),
        "samples":int(cal.get('sample_size',0) or 0),"sessions":int(cal.get('sessions',0) or 0),
        "walk_forward":cal.get('walk_forward') or {},"probability_model":{"ready":bool(pm.get('ready')),
            "status":pm.get('status') or pm.get('stage'),"brier_model":pm.get('brier_model'),
            "brier_base_rate":pm.get('brier_base_rate'),"brier_skill_score":pm.get('brier_skill_score'),
            "log_loss_model":pm.get('log_loss_model'),"log_loss_base_rate":pm.get('log_loss_base_rate'),
            "beats_base_rate":pm.get('beats_base_rate')},
        "current_gate":{"calibration_ready":bool(gate.get('calibration_ready')),
            "probability_t1_first":gate.get('probability_t1_first') if gate.get('calibration_ready') else None,
            "expected_value_r":gate.get('expected_value_r') if gate.get('calibration_ready') else None,
            "resolution_time":gate.get('resolution_time') if gate.get('calibration_ready') else None},
        "horizons":sorted(horizons,key=lambda x:x['minutes']),"research_store":rs,
        "causal_policy":"SIGNAL_STATE_IS_FROZEN_AT_DECISION_TIME · FUTURE_PATH_ONLY_USED_LATER_FOR_EVALUATION",
        "authority":"RESEARCH_AND_CALIBRATION_ONLY"}
