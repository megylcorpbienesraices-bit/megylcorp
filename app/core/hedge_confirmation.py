"""Observed underlying/futures flow confirmation for estimated option hedge pressure."""

from __future__ import annotations

from typing import Any, Dict, Optional
import math
import numpy as np
import pandas as pd
from .frame_guards import numeric_column


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x=float(v);return x if math.isfinite(x) else float(default)
    except Exception:return float(default)


def _signed_flow(ticks: Optional[pd.DataFrame], minutes: int = 5) -> Dict[str, Any]:
    if not isinstance(ticks,pd.DataFrame) or ticks.empty:
        return {"ready":False,"signed_volume":0.0,"gross_volume":0.0,"imbalance":0.0,"events":0}
    x=ticks.copy();x["timestamp"]=pd.to_datetime(x.get("timestamp"),errors="coerce");x=x.dropna(subset=["timestamp"])
    if x.empty:return {"ready":False,"signed_volume":0.0,"gross_volume":0.0,"imbalance":0.0,"events":0}
    end=x["timestamp"].max();x=x[x["timestamp"]>=end-pd.Timedelta(minutes=int(minutes))]
    size=numeric_column(x,"size",0.0).clip(lower=0.0)
    if "signed_volume" in x.columns:
        signed=pd.to_numeric(x["signed_volume"],errors="coerce").fillna(0.0)
    else:
        sign=numeric_column(x,"aggressor_sign",0.0).clip(-1,1);signed=sign*size
    sv=float(signed.sum());gv=float(size.sum());imb=sv/max(gv,1.0)
    return {"ready":True,"signed_volume":round(sv,2),"gross_volume":round(gv,2),"imbalance":round(float(np.clip(imb,-1,1)),6),"events":int(len(x))}


def hedge_flow_confirmation(
    estimated_hedge_notional: Any,
    *,
    underlying_ticks: Optional[pd.DataFrame] = None,
    futures_ticks: Optional[pd.DataFrame] = None,
    related_ticks: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """Compare estimated hedge sign with *observed* signed flow.

    Futures flow is strongest when available. Underlying SIP tape is a partial
    confirmation only. Related-market flow is context. None is attributed to a dealer.
    """
    h=_f(estimated_hedge_notional,0.0);hs=0 if abs(h)<=1e-12 else (1 if h>0 else -1)
    u=_signed_flow(underlying_ticks,5);f=_signed_flow(futures_ticks,5);r=_signed_flow(related_ticks,5)
    components=[]
    if f["ready"]:components.append(("FUTURES",f["imbalance"],0.60))
    if u["ready"]:components.append(("UNDERLYING",u["imbalance"],0.28 if f["ready"] else 0.65))
    if r["ready"]:components.append(("RELATED",r["imbalance"],0.12 if f["ready"] else 0.20))
    if not components or hs==0:
        return {"state":"UNAVAILABLE" if not components else "NEUTRAL","score":0.0,"alignment":50.0,
                "estimated_hedge_direction":"BUY" if hs>0 else "SELL" if hs<0 else "NEUTRAL",
                "futures_available":bool(f["ready"]),"underlying":u,"futures":f,"related":r,
                "note":"No se atribuye ningún trade observado a un dealer; solo se compara consistencia de signo."}
    den=sum(w for _,_,w in components) or 1.0;obs=sum(v*w for _,v,w in components)/den
    signed=float(np.clip(hs*obs,-1,1));alignment=50+50*signed
    state="CONFIRMS" if alignment>=62 else "CONTRADICTS" if alignment<=38 else "MIXED"
    quality=100.0*(0.85 if f["ready"] else 0.55)
    return {"state":state,"score":round(obs*100,1),"alignment":round(alignment,1),"confidence":round(quality,1),
            "estimated_hedge_direction":"BUY" if hs>0 else "SELL","futures_available":bool(f["ready"]),
            "underlying":u,"futures":f,"related":r,
            "note":"Flow confirmation = consistencia entre hedge requerido estimado y flujo observado; no prueba que el flujo pertenezca al dealer."}
