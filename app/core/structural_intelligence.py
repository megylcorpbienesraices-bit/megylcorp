"""Migration, tilt, velocity, acceleration and structural proximity intelligence."""

from __future__ import annotations

from collections import defaultdict, deque
from threading import RLock
from typing import Any
import math
import numpy as np
import pandas as pd
from .frame_guards import numeric_column
from .obs import note as _obs_note


def _series(df: pd.DataFrame, c: str) -> pd.Series:
    return pd.to_numeric(df[c],errors="coerce").fillna(0.0) if c in df.columns else pd.Series(0.0,index=df.index)


def _metric_state(x: pd.DataFrame, field: str, spot: float) -> dict[str,Any]:
    if field not in x.columns or "strike" not in x.columns:return {"ready":False}
    d=x.copy();d["strike"]=pd.to_numeric(d["strike"],errors="coerce");d[field]=pd.to_numeric(d[field],errors="coerce").fillna(0.0);d=d.dropna(subset=["strike"])
    if d.empty:return {"ready":False}
    g=d.groupby("strike",as_index=False)[field].sum().sort_values("strike");w=np.abs(g[field].to_numpy(float));ks=g["strike"].to_numpy(float);den=w.sum()
    center=float((ks*w).sum()/den) if den>0 else float(np.median(ks));key=float(g.iloc[int(np.argmax(w))]["strike"]) if len(g) else None
    below=float(np.abs(g.loc[g["strike"]<spot,field]).sum());above=float(np.abs(g.loc[g["strike"]>spot,field]).sum());tilt=(above-below)/max(above+below,1e-12)
    step=float(np.median(np.diff(np.sort(np.unique(ks))))) if len(np.unique(ks))>1 else 1.0
    return {"ready":True,"center":center,"key_strike":key,"tilt":float(np.clip(tilt,-1,1)),"gross":float(den),"step":max(abs(step),1e-6)}


class StructuralIntelligence:
    def __init__(self, keep:int=300)->None:
        self._lock=RLock();self._hist=defaultdict(lambda:deque(maxlen=max(30,int(keep))))

    def update(self, symbol:str, history:pd.DataFrame, *, spot:float|None=None, extra_levels:dict[str,Any]|None=None)->dict[str,Any]:
        sym=str(symbol or '').upper();extra=extra_levels or {}
        if history is None or history.empty:return {"ready":False,"symbol":sym,"reason":"NO_HISTORY"}
        x=history.copy();x["timestamp"]=pd.to_datetime(x.get("timestamp"),errors="coerce");x=x.dropna(subset=["timestamp"])
        if x.empty:return {"ready":False,"symbol":sym,"reason":"NO_TIMESTAMPS"}
        t=x["timestamp"].max();cur=x[x["timestamp"]==t].copy()
        if spot is None:
            try:spot=float(numeric_column(cur,"underlying_price",float("nan")).dropna().iloc[-1])
            except Exception:spot=0.0
        cur["_oi"]=_series(cur,"open_interest")
        states={"gamma":_metric_state(cur,"signed_gex_proxy",float(spot)),"delta":_metric_state(cur,"option_delta_exposure_info",float(spot)),"oi":_metric_state(cur,"_oi",float(spot))}
        rec={"timestamp":pd.Timestamp(t).isoformat(),"spot":float(spot),"states":states}
        with self._lock:
            h=self._hist[sym];h.append(rec);rows=list(h)
        for name,state in states.items():
            if not state.get("ready"):continue
            vals=[]
            for r in rows[-4:]:
                s=(r.get("states") or {}).get(name) or {}
                if s.get("ready"): vals.append((pd.Timestamp(r["timestamp"]),float(s["center"])))
            vel=acc=0.0
            if len(vals)>=2:
                dt=max((vals[-1][0]-vals[-2][0]).total_seconds()/60.0,1e-6);vel=(vals[-1][1]-vals[-2][1])/dt/state["step"]
            if len(vals)>=3:
                dt1=max((vals[-2][0]-vals[-3][0]).total_seconds()/60.0,1e-6);v0=(vals[-2][1]-vals[-3][1])/dt1/state["step"];dt2=max((vals[-1][0]-vals[-2][0]).total_seconds()/60.0,1e-6);acc=(vel-v0)/dt2
            state["migration_direction"]="UP" if vel>0.05 else "DOWN" if vel<-0.05 else "FLAT";state["velocity_strikes_per_min"]=round(vel,5);state["acceleration_strikes_per_min2"]=round(acc,5);state["tilt_label"]="UPPER_STRIKES" if state["tilt"]>0.08 else "LOWER_STRIKES" if state["tilt"]<-0.08 else "BALANCED"
        levels=[]
        for name,state in states.items():
            if state.get("ready") and state.get("key_strike") is not None: levels.append({"name":f"KEY_{name.upper()}","price":float(state["key_strike"]),"source":"ITM_PROFILE"})
        for name,val in extra.items():
            try:
                p=float(val)
                if math.isfinite(p):levels.append({"name":str(name).upper(),"price":p,"source":"ITM_STATE"})
            except Exception as _e:
                _obs_note('structural_intelligence:66', _e)
        prox=[]
        for lv in levels:
            dist=abs(lv["price"]-float(spot))/max(abs(float(spot)),1e-9)*100.0
            prox.append({**lv,"distance_pct":round(dist,5),"distance_abs":round(abs(lv["price"]-float(spot)),6)})
        prox=sorted(prox,key=lambda r:r["distance_pct"])
        near=[r for r in prox if r["distance_pct"]<=1.0]
        density=min(100.0,len(near)*14.0+max(0.0,30.0-(near[0]["distance_pct"]*30.0 if near else 30.0)))
        return {"ready":True,"symbol":sym,"asof":pd.Timestamp(t).isoformat(),"spot":float(spot),"metrics":states,
                "proximity":{"levels":prox[:16],"within_1pct":near[:12],"confluence_count":len(near),"structural_density":round(density,1)},
                "history_tail":rows[-120:],"authority":"STRUCTURAL_CONTEXT_ONLY","scanner_rule":"EVIDENCE_INPUT_NOT_INDEPENDENT_DIRECTION"}

    def snapshot(self,symbol:str)->list[dict[str,Any]]:
        with self._lock:return list(self._hist.get(str(symbol or '').upper(),()))


STRUCTURAL_INTELLIGENCE=StructuralIntelligence()
