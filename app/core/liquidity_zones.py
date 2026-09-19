"""Adaptive liquidity-zone clustering for observed equity prints.

The algorithm is ITM-owned and deliberately avoids copying fixed thresholds from any
external platform.  Zone width adapts to the observed price grid and spot; strength is
relative to the current observed sample and therefore portable across assets.
"""
from __future__ import annotations
from typing import Any
import math
import numpy as np
import pandas as pd
from .frame_guards import numeric_column


def _weighted_median(x: np.ndarray, w: np.ndarray) -> float:
    order=np.argsort(x);x=x[order];w=w[order];cs=np.cumsum(w);cut=.5*float(w.sum())
    return float(x[min(int(np.searchsorted(cs,cut,side="left")),len(x)-1)])


def liquidity_zones(df: pd.DataFrame, spot: float | None = None, max_each_side: int = 3) -> dict[str, Any]:
    if df is None or df.empty:return {"ready":False,"zones":[],"reason":"NO_PRINTS"}
    x=df.copy();x["price"]=numeric_column(x,"price",float("nan"));x["notional"]=numeric_column(x,"notional",float("nan"))
    x=x.dropna(subset=["price","notional"]);x=x[(x["price"]>0)&(x["notional"]>0)].sort_values("price")
    if x.empty:return {"ready":False,"zones":[],"reason":"NO_VALID_PRINTS"}
    px=float(spot) if spot is not None else float(x["price"].median())
    if not math.isfinite(px):px=float(x["price"].median())
    dif=np.diff(np.sort(x["price"].unique()));grid=float(np.nanmedian(dif[dif>0])) if np.any(dif>0) else 0.01
    width=max(grid*2.5,px*0.00022,0.02)
    clusters=[];cur=[];last=None
    for r in x.itertuples(index=False):
        p=float(r.price)
        if last is None or p-last<=width:
            cur.append(r)
        else:
            clusters.append(cur);cur=[r]
        last=p
    if cur:clusters.append(cur)
    totals=np.asarray([sum(float(r.notional) for r in c) for c in clusters],dtype=float)
    positive=totals[totals>0];q50=float(np.nanmedian(positive)) if len(positive) else 1.0;q90=float(np.nanquantile(positive,.9)) if len(positive)>=2 else max(q50,1.0)
    zones=[]
    for c,total in zip(clusters,totals):
        if len(c)<2 and total<q50:continue
        prices=np.asarray([float(r.price) for r in c]);weights=np.asarray([float(r.notional) for r in c]);center=_weighted_median(prices,weights)
        order=np.argsort(weights)[::-1];top_n=float(weights[order[:min(3,len(order))]].sum());concentration=top_n/max(float(total),1e-9)
        score=100.0*np.clip(.55*(total/max(q90,1e-9))+.25*min(len(c)/4,1)+.20*concentration,0,1)
        zones.append({"low":float(prices.min()),"high":float(prices.max()),"center":center,"notional":float(total),"prints":int(len(c)),"concentration":round(concentration,4),"score":round(float(score),1),"type":"BLOCK" if concentration>=.62 else "CARPET","side":"ABOVE" if center>px else "BELOW" if center<px else "AT_SPOT"})
    zones=sorted(zones,key=lambda z:(z["side"],-z["score"],-z["notional"]))
    final=[]
    for side in ("ABOVE","BELOW","AT_SPOT"):
        final.extend([z for z in zones if z["side"]==side][:max_each_side])
    return {"ready":bool(final),"spot":px,"adaptive_cluster_width":round(width,6),"zones":final,"method":"ADAPTIVE_PRINT_CLUSTERING","authority":"CONTEXT_ONLY"}
