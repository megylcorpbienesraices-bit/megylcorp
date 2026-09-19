"""Context-aware feature intelligence layer.

This layer explains which semantic feature channels are currently informative and why.
It does NOT silently rewrite Scanner coefficients. Until a channel has sufficient OOS
research evidence, its regime relevance remains SHADOW context.
"""

from __future__ import annotations

from typing import Any
import math


def _f(v:Any,default:float=0.0)->float:
    try:
        x=float(v);return x if math.isfinite(x) else default
    except Exception:return default


def _regime_factor(channel:str, phase:str, vol_regime:str)->float:
    c=channel.lower();p=phase.upper();v=vol_regime.upper()
    factor=1.0
    if c=='gamma':
        if p=='CONTAINMENT':factor*=1.15
        elif p=='ACCELERATION':factor*=0.90
    elif c=='flow':
        if p in {'ACCELERATION','ABSORPTION'}:factor*=1.15
        elif p=='CONTAINMENT':factor*=0.92
    elif c=='ecosystem':
        if p in {'TRANSITION','ACCELERATION'}:factor*=1.10
    elif c=='derivatives':
        factor*=1.05
    elif c=='delta':
        if p in {'TRANSITION','ACCELERATION'}:factor*=1.07
    if 'HIGH' in v or 'EXP' in v:
        if c in {'flow','delta','ecosystem'}:factor*=1.05
        if c=='gamma':factor*=0.97
    return max(0.75,min(1.30,factor))


def _historical_edge(calibration:dict[str,Any]|None, channel:str)->tuple[float|None,int]:
    # Existing calibration already computes feature diagnostics. Only exact feature-name
    # matches are used; no missing historical edge is invented.
    cal=calibration or {}; names={channel.lower(),f'provider_{channel.lower()}_confidence'}
    for row in cal.get('feature_diagnostics') or []:
        name=str(row.get('feature') or '').lower()
        if name in names:
            corr=row.get('spearman_to_resolved_move'); samples=int(row.get('samples',0) or 0)
            try:return (float(corr) if corr is not None else None),samples
            except Exception:return None,samples
    return None,0


def build_feature_intelligence(*, feature_snapshot:dict[str,Any]|None, market_state:dict[str,Any]|None,
                               regime_context:dict[str,Any]|None, calibration:dict[str,Any]|None,
                               scanner:dict[str,Any]|None=None)->dict[str,Any]:
    fs=feature_snapshot or {};ms=market_state or {};reg=regime_context or {};sc=scanner or {}
    phase=str(ms.get('phase') or 'TRANSITION').upper();vol_regime=str(reg.get('vol_regime') or reg.get('regime') or '')
    channels=[];signed_sum=0.0;weight_sum=0.0
    for name,ch in (fs.get('channels') or {}).items():
        if not isinstance(ch,dict):continue
        sign=int(ch.get('sign') or 0);conf=max(0.0,min(100.0,_f(ch.get('confidence'))))
        contributors=ch.get('contributors') or []
        qvals=[_f(c.get('quality')) for c in contributors if isinstance(c,dict)]
        quality=sum(qvals)/len(qvals) if qvals else (100.0 if conf>0 else 0.0)
        ages=[_f(c.get('age_ms')) for c in contributors if isinstance(c,dict)]
        age=max(ages) if ages else 0.0
        freshness=max(0.0,min(1.0,1.0-age/180000.0))
        regime_factor=_regime_factor(str(name),phase,vol_regime)
        hist_corr,hist_n=_historical_edge(calibration,str(name))
        # Historical effect remains neutral until at least 30 observations. Even then it is
        # bounded and used only in SHADOW/context scoring, not Scanner production weights.
        hist_factor=1.0
        if hist_corr is not None and hist_n>=30:
            hist_factor=max(0.85,min(1.15,1.0+0.15*abs(hist_corr)))
        effective=(conf/100.0)*(quality/100.0)*freshness*regime_factor*hist_factor
        if sign:
            signed_sum+=sign*effective;weight_sum+=effective
        channels.append({"channel":str(name).upper(),"direction":ch.get('direction'),"sign":sign,
            "confidence":round(conf,2),"quality":round(quality,2),"freshness":round(freshness,4),
            "regime_relevance":round(regime_factor,3),"historical_edge_spearman":hist_corr,
            "historical_edge_samples":hist_n,"effective_shadow_weight":round(effective,5),
            "contributors":contributors})
    context_ratio=signed_sum/weight_sum if weight_sum>0 else 0.0
    scanner_dir=str(sc.get('direction') or 'WAITING').upper();scanner_sign=1 if scanner_dir=='BUY' else -1 if scanner_dir=='SELL' else 0
    alignment=50.0 if scanner_sign==0 else max(0.0,min(100.0,50.0+50.0*scanner_sign*context_ratio))
    return {"ready":bool(channels),"phase":phase,"vol_regime":vol_regime,"channels":channels,
        "context_direction":"BUY" if context_ratio>0.08 else "SELL" if context_ratio<-0.08 else "NEUTRAL",
        "context_score":round(context_ratio*100.0,2),"scanner_alignment":round(alignment,2),
        "production_weight_changes":False,"mode":"SHADOW_CONTEXT_AWARE_WEIGHTING",
        "policy":"QUALITY × FRESHNESS × REGIME_RELEVANCE × VALIDATED_EDGE_IF_AVAILABLE",
        "note":"Context-aware weights explain evidence concentration. They cannot flip Scanner or auto-promote without OOS validation."}
