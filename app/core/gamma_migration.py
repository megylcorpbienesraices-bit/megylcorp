"""Causal Gamma migration map for ITM QUANT.

VALUE shows observed calculated exposure at each timestamp/strike.
DIFFERENCE shows change versus the immediately previous comparable snapshot.  It is
never labelled as observed dealer inventory migration: it is a change in ITM's
calculated exposure using the same cell-comparability rule as ΔGEX Matrix.
"""
from __future__ import annotations

from typing import Any
import math
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from .frame_guards import numeric_column
from .obs import note as _obs_note


def _empty(note: str) -> go.Figure:
    f=go.Figure();f.add_annotation(text=note,showarrow=False,x=.5,y=.5,xref="paper",yref="paper")
    f.update_layout(template="plotly_dark",height=620,paper_bgcolor="#080b10",plot_bgcolor="#0c1118")
    return f


def migration_frame(result: dict[str, Any], mode: str = "DIFFERENCE", max_strikes: int = 17) -> tuple[pd.DataFrame, dict[str, Any]]:
    e=result.get("enriched",pd.DataFrame()).copy() if isinstance(result,dict) else pd.DataFrame()
    if e.empty:return pd.DataFrame(),{"ready":False,"reason":"NO_ENRICHED_CHAIN"}
    e["timestamp"]=pd.to_datetime(e.get("timestamp"),errors="coerce");e["strike"]=numeric_column(e,"strike",float("nan"))
    e["gex"]=numeric_column(e,"signed_gex_proxy",float("nan"))
    e=e.dropna(subset=["timestamp","strike","gex"])
    if e.empty:return pd.DataFrame(),{"ready":False,"reason":"NO_COMPARABLE_GEX"}
    g=e.groupby(["timestamp","strike"],as_index=False)["gex"].sum(min_count=1).sort_values(["timestamp","strike"])
    times=sorted(g["timestamp"].unique())
    if not times:return pd.DataFrame(),{"ready":False,"reason":"NO_TIMESTAMPS"}
    spot=float(result.get("spot") or np.nan)
    latest=g[g["timestamp"]==times[-1]].copy();latest["abs"]=latest["gex"].abs()
    if math.isfinite(spot):
        step=float(np.nanmedian(np.diff(np.sort(latest["strike"].unique())))) if latest["strike"].nunique()>1 else 1.0
        near=latest[(latest["strike"]>=spot-step*max_strikes/2)&(latest["strike"]<=spot+step*max_strikes/2)]
        candidates=near if len(near)>=min(7,max_strikes) else latest.nlargest(max_strikes,"abs")
    else:candidates=latest.nlargest(max_strikes,"abs")
    strikes=sorted(float(x) for x in candidates["strike"].unique())[:max_strikes]
    g=g[g["strike"].isin(strikes)].copy()
    mode=str(mode or "DIFFERENCE").upper()
    if mode=="VALUE":
        g["value"]=g["gex"];comparable=np.ones(len(g),dtype=bool)
    else:
        wide=g.pivot(index="timestamp",columns="strike",values="gex").sort_index()
        diff=wide.diff()
        comparable=wide.notna() & wide.shift(1).notna()
        d=diff.rename_axis(index="timestamp",columns="strike").reset_index().melt(id_vars="timestamp",var_name="strike",value_name="value")
        c=comparable.rename_axis(index="timestamp",columns="strike").reset_index().melt(id_vars="timestamp",var_name="strike",value_name="comparable")
        g=d.merge(c,on=["timestamp","strike"],how="left");g=g[g["comparable"].fillna(False)].copy()
    if g.empty:return pd.DataFrame(),{"ready":False,"reason":"NEED_PREVIOUS_COMPARABLE_SNAPSHOT","mode":mode}
    a=np.abs(pd.to_numeric(g["value"],errors="coerce").to_numpy(float));finite=a[np.isfinite(a)]
    cap=float(np.nanquantile(finite,.95)) if len(finite)>=4 else float(np.nanmax(finite)) if len(finite) else 1.0
    cap=max(cap,1e-9);g["bubble"]=1.0+19.0*np.clip(np.abs(g["value"].astype(float))/cap,0,1)
    meta={"ready":True,"mode":mode,"timestamps":len(times),"strikes":strikes,"scale_cap":cap,"unit":"$ GEX proxy",
          "interpretation":"CHANGE_IN_CALCULATED_EXPOSURE" if mode!="VALUE" else "CALCULATED_EXPOSURE_VALUE",
          "dealer_inventory_claim":False}
    return g.sort_values(["timestamp","strike"]),meta


def gamma_migration_figure(result: dict[str, Any], mode: str = "DIFFERENCE", max_strikes: int = 17) -> go.Figure:
    g,meta=migration_frame(result,mode,max_strikes)
    if g.empty:return _empty("GAMMA MIGRATION · acumulando snapshots comparables")
    val=pd.to_numeric(g["value"],errors="coerce")/1e6
    colors=np.where(val>=0,"#23d18b","#ff4d6d")
    custom=np.column_stack([val.to_numpy(float),pd.to_numeric(g["bubble"],errors="coerce").to_numpy(float)])
    fig=go.Figure(go.Scatter(
        x=g["timestamp"],y=g["strike"],mode="markers",name="Gamma Migration",
        marker=dict(size=g["bubble"],color=colors,opacity=.72,line=dict(width=.8,color="#d5e2ee")),
        customdata=custom,
        hovertemplate="%{x|%H:%M:%S}<br>Strike %{y:g}<br>Valor %{customdata[0]:+.3f}M<br>Bubble %{customdata[1]:.1f}<extra></extra>",
    ))
    spot=result.get("spot")
    if spot is not None:
        try:fig.add_hline(y=float(spot),line_color="#55aaff",line_width=1.3,line_dash="dot",annotation_text=f"SPOT {float(spot):.2f}")
        except Exception as _e:_obs_note("gamma_migration:spot_line",_e)
    title="GAMMA MIGRATION · VALUE" if str(mode).upper()=="VALUE" else "GAMMA MIGRATION · DIFFERENCE vs SNAPSHOT PREVIO"
    fig.update_layout(template="plotly_dark",height=650,title=title,paper_bgcolor="#080b10",plot_bgcolor="#0c1118",hovermode="closest",dragmode="pan",margin=dict(l=65,r=35,t=58,b=45),uirevision=f"gamma-migration-{str(mode).upper()}",meta=meta)
    fig.update_xaxes(title="Tiempo",gridcolor="#142235",rangeslider_visible=False);fig.update_yaxes(title="Strike",gridcolor="#1c2b3e",dtick=None)
    return fig


def migration_digest(result: dict[str, Any], mode: str = "DIFFERENCE") -> dict[str, Any]:
    g,meta=migration_frame(result,mode)
    if g.empty:return meta
    latest=g[g["timestamp"]==g["timestamp"].max()].copy();latest["abs"]=latest["value"].abs()
    top=latest.nlargest(5,"abs")
    pos=latest.loc[latest["value"]>0,"value"];neg=latest.loc[latest["value"]<0,"value"]
    return {**meta,"latest_timestamp":pd.Timestamp(g["timestamp"].max()).isoformat(),
            "points":int(len(g)),"strike_count":int(g["strike"].nunique()),
            "max_positive_m":float(pos.max()/1e6) if len(pos) else None,
            "max_negative_m":float(neg.min()/1e6) if len(neg) else None,
            "top":[{"strike":float(r.strike),"value_m":float(r.value)/1e6,"bubble":float(r.bubble)} for r in top.itertuples()]}
