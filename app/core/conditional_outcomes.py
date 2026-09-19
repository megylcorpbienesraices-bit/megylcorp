"""Conditional evidence layer on top of ITM QUANT's existing calibrated Scanner model.

This module does not create a new probability model.  It summarizes the calibrated
model, sample sufficiency, regime evidence and resolution-time distributions already
produced by the audited calibration engine.  Scanner direction remains unchanged.
"""
from __future__ import annotations
from typing import Any
import math


def _f(v,default=None):
    try:
        x=float(v);return x if math.isfinite(x) else default
    except Exception:return default


def conditional_outcome_report(scanner: dict[str,Any] | None, calibration: dict[str,Any] | None,
                               regime: dict[str,Any] | None = None) -> dict[str,Any]:
    sc=scanner or {};cal=calibration or {};reg=regime or {}
    pm=cal.get("probability_model") or {}
    samples=int(pm.get("scope_samples") or pm.get("samples") or cal.get("sample_size") or 0)
    sessions=int(pm.get("test_sessions") or pm.get("final_oos_session_count") or 0)
    calibrated=bool(pm.get("ready"))
    if calibrated and samples>=120:quality="STRONG"
    elif calibrated and samples>=40:quality="MODERATE"
    elif samples>=20:quality="LOW_SAMPLE"
    else:quality="NO_CALIBRATED_VERDICT"
    p=None
    prob=sc.get("probability")
    if isinstance(prob,dict):p=_f(prob.get("p_target_before_invalidation") or prob.get("probability"))
    else:p=_f(prob)
    rt=pm.get("resolution_time") or {}
    return {
        "ready":bool(sc.get("ready")),"direction":sc.get("direction"),"evidence_score":_f(sc.get("evidence_score",sc.get("strength"))),
        "p_t1_before_invalidation":p,"statistical_evidence":quality,"calibrated":calibrated,"samples":samples,"oos_sessions":sessions,
        "brier_skill":_f(pm.get("brier_skill_score")),"log_loss":_f(pm.get("log_loss_model")),"base_rate":_f(pm.get("base_rate")),
        "resolution_time":rt,"regime":reg.get("label") or reg.get("regime") or reg.get("state"),
        "note":"Dirección = Scanner. Esta capa solo informa calidad estadística/calibración y no puede voltear BUY/SELL.",
        "authority":"EVIDENCE_ONLY",
    }
