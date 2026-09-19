from __future__ import annotations

from typing import Any, Dict
import numpy as np


def classify_regime(result: Dict[str, Any], vol: Dict[str, Any], flow: Dict[str, Any]) -> Dict[str, Any]:
    """Transparent regime classifier used to adapt scanner emphasis.

    It does not learn weights by itself. Calibration Lab is responsible for validating
    whether a regime-specific weighting scheme improves out-of-sample results.
    """
    gamma_regime=str(result.get("regime","")).upper()
    iv_regime=str(vol.get("regime","")).upper()
    align=str(result.get("gamma_delta_alignment_label","")).upper()
    gp=str(result.get("pressure_direction","")).upper(); dp=str(result.get("delta_pressure_direction","")).upper()
    flow_dir=str(flow.get("direction",flow.get("bias",flow.get("regime","")))).upper()
    gscore=float(result.get("pressure_score",0) or 0); dscore=float(result.get("delta_pressure_score",0) or 0)
    migration=max(float(result.get("migration_strength",0) or 0),float(result.get("delta_migration_strength",0) or 0))
    flip_dist=abs(float(result.get("spot",0) or 0)-float(result.get("gamma_flip",result.get("spot",0)) or 0))
    cur=result.get("current_delta")
    try:
        step=float(np.median(np.diff(np.sort(cur["strike"].astype(float).unique())))) if cur is not None and cur["strike"].nunique()>1 else 1.0
    except Exception:step=1.0
    flip_near=flip_dist<=max(step*1.2,0.25)

    scores={"PINNING":0.0,"TREND EXPANSION":0.0,"VOLATILITY EXPANSION":0.0,"MEAN REVERSION":0.0,"BREAKOUT":0.0,"TRANSITION":0.0}
    if "POSITIVE" in gamma_regime:
        scores["PINNING"]+=35;scores["MEAN REVERSION"]+=30
    if "NEGATIVE" in gamma_regime:
        scores["TREND EXPANSION"]+=30;scores["BREAKOUT"]+=28
    if iv_regime=="EXPANSION":
        scores["VOLATILITY EXPANSION"]+=45;scores["BREAKOUT"]+=20
    elif iv_regime=="COMPRESSION":
        scores["PINNING"]+=20;scores["MEAN REVERSION"]+=18
    if align=="ALIGNED":
        scores["TREND EXPANSION"]+=min(30,(gscore+dscore)/6);scores["BREAKOUT"]+=min(18,(gscore+dscore)/10)
    elif align=="CONFLICT":
        scores["TRANSITION"]+=40
    if migration>=55:
        scores["TREND EXPANSION"]+=20;scores["BREAKOUT"]+=10
    if flip_near:
        scores["TRANSITION"]+=28;scores["BREAKOUT"]+=12
    if gp in {"UP","DOWN"} and dp in {"BUY","SELL"}:
        aligned=(gp=="UP" and dp=="BUY") or (gp=="DOWN" and dp=="SELL")
        scores["TREND EXPANSION" if aligned else "TRANSITION"]+=18
    if any(x in flow_dir for x in ["BUY","BULL"]):
        if dp=="BUY":scores["TREND EXPANSION"]+=8
    if any(x in flow_dir for x in ["SELL","BEAR"]):
        if dp=="SELL":scores["TREND EXPANSION"]+=8
    winner=max(scores,key=scores.get); raw=scores[winner]; total=sum(scores.values())
    # v1.14: the old formula was 45 + 55*raw/total, which has a hard floor at 45 and
    # returned ~54 for a perfectly uniform (i.e. maximally uncertain) classification.
    # Confidence is now genuinely zero at uniformity and 100 only when one regime
    # takes the whole mass, combining concentration with the margin over the runner-up.
    K=len(scores)
    if total<=0:
        confidence=0.0
    else:
        share=raw/total
        concentration=float(np.clip((share-1.0/K)/(1.0-1.0/K),0.0,1.0))
        ordered=sorted(scores.values(),reverse=True)
        second=ordered[1] if len(ordered)>1 else 0.0
        margin=float(np.clip((raw-second)/max(raw,1e-9),0.0,1.0))
        confidence=float(np.clip(100.0*(0.60*concentration+0.40*margin),0.0,100.0))
    # Multipliers modify emphasis gently; no regime can erase a core evidence family.
    profiles={
        "PINNING":{"containment":1.12,"break":0.90,"flow":0.92,"structure":1.08,"volatility":0.90},
        "MEAN REVERSION":{"containment":1.10,"break":0.92,"flow":0.95,"structure":1.06,"volatility":0.92},
        "TREND EXPANSION":{"containment":0.92,"break":1.10,"flow":1.08,"structure":1.00,"volatility":1.05},
        "VOLATILITY EXPANSION":{"containment":0.88,"break":1.14,"flow":1.05,"structure":0.98,"volatility":1.12},
        "BREAKOUT":{"containment":0.90,"break":1.12,"flow":1.08,"structure":1.00,"volatility":1.08},
        "TRANSITION":{"containment":0.96,"break":0.96,"flow":0.92,"structure":1.04,"volatility":1.00},
    }
    return {"regime":winner,"confidence":round(confidence,1),"scores":{k:round(v,1) for k,v in scores.items()},"profile":profiles[winner],
            "note":"Régimen heurístico transparente; los multiplicadores requieren validación walk-forward antes de interpretarse como mejora estadística."}
