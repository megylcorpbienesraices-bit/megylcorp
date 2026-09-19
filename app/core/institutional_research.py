"""Institutional Research Labs (v1.16.6).

Everything in this module is SHADOW/RESEARCH. None of these outputs may alter Scanner
production direction, Evidence, Tape confirmation or EV Gate. The point is to collect and
measure candidates before promotion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import math
import numpy as np
import pandas as pd

from . import timeunits
from app.persistence import routed_dir

from .instruments import get as instrument_get
from .asset_ecosystems import ecosystem_for, related_equity_symbols
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .safe_stats import correlation as safe_correlation


def _finite(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except Exception:return None


def _minute_returns(ticks: pd.DataFrame | None) -> pd.Series:
    if not isinstance(ticks,pd.DataFrame) or ticks.empty or "price" not in ticks.columns:return pd.Series(dtype=float)
    x=ticks.copy();x["timestamp"]=pd.to_datetime(x.get("timestamp"),errors="coerce");x["price"]=numeric_column(x,"price",float("nan"))
    x=x.dropna(subset=["timestamp","price"]).sort_values("timestamp")
    if len(x)<3:return pd.Series(dtype=float)
    close=x.set_index("timestamp")["price"].resample("1min").last().dropna()
    return np.log(close).diff().replace([np.inf,-np.inf],np.nan).dropna()


def _garch11_forecast(r: pd.Series) -> Dict[str,Any]:
    """Small QMLE GARCH(1,1) fit using scipy; research-only and bounded for stability."""
    if len(r)<120:return {"available":False,"reason":f"{len(r)}/120 retornos de 1 min"}
    y=np.asarray(r.tail(4000),dtype=float); y=y[np.isfinite(y)]
    if len(y)<120:return {"available":False,"reason":"muestra insuficiente"}
    # scale improves optimizer conditioning; output is rescaled back.
    scale=max(float(np.std(y)),1e-8); z=y/scale
    try:
        from scipy.optimize import minimize
        def nll(theta):
            omega,alpha,beta=theta
            if omega<=1e-8 or alpha<0 or beta<0 or alpha+beta>=.995:return 1e12
            h=np.empty(len(z));h[0]=max(np.var(z),1e-6)
            for i in range(1,len(z)):h[i]=omega+alpha*z[i-1]**2+beta*h[i-1]
            h=np.maximum(h,1e-9)
            return .5*float(np.sum(np.log(h)+z*z/h))
        res=minimize(nll,[.02,.08,.88],bounds=[(1e-6,2),(.001,.4),(.05,.98)],method="L-BFGS-B")
        if not res.success:return {"available":False,"reason":"optimizador no convergió"}
        omega,alpha,beta=[float(v) for v in res.x]
        h=max(float(np.var(z)),1e-6)
        for i in range(1,len(z)):h=omega+alpha*z[i-1]**2+beta*h
        nxt=(omega+alpha*z[-1]**2+beta*h)*(scale**2)
        return {"available":True,"variance_1m":float(max(nxt,0)),"alpha":alpha,"beta":beta,"persistence":alpha+beta,"samples":int(len(z))}
    except Exception as exc:return {"available":False,"reason":str(exc)[:120]}


def volatility_forecast_lab(symbol: str, ticks: pd.DataFrame | None, vol: Dict[str,Any] | None=None) -> Dict[str,Any]:
    r=_minute_returns(ticks); inst=instrument_get(symbol); ann=float(inst.minutes_per_year)
    out={"role":"SHADOW_ONLY","affects_scanner":False,"symbol":str(symbol).upper(),"state":"COLLECTING","models":[],"note":"Forecasts de volatilidad se comparan contra volatilidad realizada futura antes de cualquier promoción."}
    if len(r)<30:
        out["reason"]=f"Se requieren retornos intradía; disponibles {len(r)}."
        return out
    rv=float(r.tail(min(60,len(r))).std(ddof=1)*math.sqrt(ann)*100) if len(r)>1 else None
    lam=.94; v=float(np.var(r))
    for x in np.asarray(r.tail(1500),dtype=float):v=lam*v+(1-lam)*x*x
    ewma=math.sqrt(max(v,0)*ann)*100
    out["models"].append({"name":"REALIZED","forecast_vol_pct":rv,"status":"DIAGNOSTIC","samples":int(len(r))})
    out["models"].append({"name":"EWMA","forecast_vol_pct":ewma,"status":"SHADOW","lambda":lam})
    g=_garch11_forecast(r)
    if g.get("available"):
        out["models"].append({"name":"GARCH(1,1)","forecast_vol_pct":math.sqrt(g["variance_1m"]*ann)*100,"status":"SHADOW","alpha":g["alpha"],"beta":g["beta"],"persistence":g["persistence"],"samples":g["samples"]})
    else:out["models"].append({"name":"GARCH(1,1)","forecast_vol_pct":None,"status":"COLLECTING","reason":g.get("reason")})
    iv=_finite((vol or {}).get("atm_iv"))
    if iv is not None:out["models"].append({"name":"IV ATM","forecast_vol_pct":iv,"status":"MARKET INPUT"})
    fv=_finite((vol or {}).get("forward_volatility_pct"))
    if fv is not None:out["models"].append({"name":"FORWARD VOL","forecast_vol_pct":fv,"status":"MARKET INPUT"})
    out["state"]="SHADOW";out["samples"]=int(len(r));out["annual_minutes"]=ann
    return out


def microstructure_lab(ticks: pd.DataFrame | None) -> Dict[str,Any]:
    out={"role":"TIMING_CONTEXT_SHADOW","affects_scanner":False,"state":"COLLECTING","l2_status":"WAITING FOR L2","note":"L1/Tape se mide ahora. L5/L10, cancelaciones, colas y resiliencia quedan bloqueados hasta profundidad real."}
    if not isinstance(ticks,pd.DataFrame) or ticks.empty:return out
    x=ticks.copy()
    for c in ("price","size","bid","ask","bid_size","ask_size","signed_volume"):
        if c in x.columns:x[c]=pd.to_numeric(x[c],errors="coerce")
    x["timestamp"]=pd.to_datetime(x.get("timestamp"),errors="coerce");x=x.dropna(subset=["timestamp","price"]).sort_values("timestamp")
    if x.empty:return out
    last=x.iloc[-1]; bid=_finite(last.get("bid"));ask=_finite(last.get("ask"));bs=_finite(last.get("bid_size"));a_s=_finite(last.get("ask_size"))
    imb=None;micro=None
    if bs is not None and a_s is not None and bs+a_s>0:
        imb=(bs-a_s)/(bs+a_s)
    if bid is not None and ask is not None and bs is not None and a_s is not None and bs+a_s>0 and ask>=bid:
        # opposite-side weighting: more bid size pulls microprice toward ask.
        micro=(ask*bs+bid*a_s)/(bs+a_s)
    sv=pd.to_numeric(x.get("signed_volume",pd.Series(dtype=float)),errors="coerce").fillna(0)
    vol=pd.to_numeric(x.get("size",pd.Series(dtype=float)),errors="coerce").fillna(0)
    ofi=float(sv.tail(min(500,len(sv))).sum()); denom=float(vol.tail(min(500,len(vol))).sum())
    ofi_ratio=ofi/denom if denom>0 else None
    # Hawkes-style excitation proxy: exponential sum of recent trade arrivals / baseline.
    ts=pd.to_datetime(x["timestamp"],errors="coerce").dropna()
    hawkes=None
    if len(ts)>=30:
        # v1.27.3 BUGFIX: `/1e9` asumía nanosegundos. Con la resolución de
        # microsegundos de pandas 3.0 daba milésimas de segundo, así que la
        # ventana de "60 s" abarcaba 16.7 horas y tau=10 s valía 10 000 s.
        secs=timeunits.epoch_seconds_array(ts);now=secs[-1];tau=10.0
        excitation=float(np.exp(-(now-secs[secs>=now-60])/tau).sum())
        duration=max(now-secs[0],1.0);base=len(secs)/duration*tau
        hawkes=excitation/max(base,1e-9)
    out.update({"state":"SHADOW","samples":int(len(x)),"microprice":micro,"midprice":((bid+ask)/2 if bid is not None and ask is not None else None),"book_imbalance_l1":imb,"signed_flow":ofi,"signed_flow_ratio":ofi_ratio,"hawkes_excitation_proxy":hawkes,
                "l2_metrics":{"depth_l5_l10":None,"cancellation_pressure":None,"queue_pressure":None,"resiliency":None,"status":"WAITING FOR L2"}})
    return out


def _daily_close(storage: Path, symbol: str, max_sessions: int=80) -> pd.Series:
    files=sorted(routed_dir(Path(storage), "sessions").glob(f"alpaca_{str(symbol).lower()}_history_*.csv"))[-max_sessions:]
    vals={}
    for f in files:
        try:
            df=pd.read_csv(f,usecols=lambda c:c in {"timestamp","underlying_price"})
            if df.empty:continue
            t=pd.to_datetime(df.get("timestamp"),errors="coerce");p=numeric_column(df,"underlying_price",float("nan"))
            z=pd.DataFrame({"t":t,"p":p}).dropna().sort_values("t")
            if len(z):vals[z["t"].iloc[-1].date().isoformat()]=float(z["p"].iloc[-1])
        except Exception as _e:
            _obs_note('institutional_research:133', _e)
            continue
    return pd.Series(vals,dtype=float).sort_index()


def _kalman_beta(y: np.ndarray, x: np.ndarray) -> tuple[float,float]:
    beta=1.0;P=1.0;Q=1e-4;R=max(float(np.var(y-x)),1e-6)
    for yi,xi in zip(y,x):
        P=P+Q;H=float(xi);S=H*P*H+R
        if S<=0:continue
        K=P*H/S;beta=beta+K*(yi-H*beta);P=(1-K*H)*P
    resid=float(y[-1]-beta*x[-1]) if len(y) else float("nan")
    return float(beta),resid


def _adf_residual_t(resid: np.ndarray) -> float | None:
    if len(resid)<12:return None
    y=np.diff(resid); lag=resid[:-1];X=np.column_stack([np.ones(len(lag)),lag])
    try:
        b=np.linalg.lstsq(X,y,rcond=None)[0];e=y-X@b;dof=max(len(y)-2,1);s2=float(e@e/dof);cov=s2*np.linalg.inv(X.T@X);se=math.sqrt(max(float(cov[1,1]),1e-12));return float(b[1]/se)
    except Exception:return None


def cross_asset_lab(storage: Path, symbol: str) -> Dict[str,Any]:
    sym=str(symbol).upper();eco=ecosystem_for(sym);related=related_equity_symbols(sym)
    out={"role":"SHADOW_ONLY","affects_scanner":False,"state":"COLLECTING","symbol":sym,"family":eco.get("family"),"related":related,"note":"PCA, beta dinámica y cointegración son contexto cross-asset; nunca votan Scanner por sí solos."}
    base=_daily_close(Path(storage),sym)
    series={sym:base}
    for r in related:
        sr=_daily_close(Path(storage),r)
        if not sr.empty:
            series[r]=sr
    # Missing peers must not erase valid peers. Only compare assets that actually have
    # common observations; absent feeds stay absent rather than becoming zero or NaN votes.
    df=pd.DataFrame(series).dropna(axis=1,how="all").dropna()
    if len(df)<8 or df.shape[1]<2:
        out["reason"]=f"Se requieren ≥8 sesiones comunes de al menos 2 activos; disponibles {len(df)}."
        return out
    rets=np.log(df).diff().dropna();cols=list(rets.columns);cov=np.cov(rets.to_numpy().T)
    vals,vecs=np.linalg.eigh(cov);order=np.argsort(vals)[::-1];vals=vals[order];vecs=vecs[:,order]
    explained=float(vals[0]/max(vals.sum(),1e-12))
    peer=next(c for c in cols if c!=sym);y=rets[sym].to_numpy();x=rets[peer].to_numpy();beta,resid_last=_kalman_beta(y,x)
    # Si uno de los dos activos no se movió en la ventana, no hay correlación que
    # medir. Publicar None es honesto; publicar 0 afirmaría independencia.
    corr=safe_correlation(y,x).or_none()
    levels=pd.DataFrame({"y":np.log(df[sym]),"x":np.log(df[peer])}).dropna();X=np.column_stack([np.ones(len(levels)),levels["x"].to_numpy()]);b=np.linalg.lstsq(X,levels["y"].to_numpy(),rcond=None)[0];res=levels["y"].to_numpy()-X@b;adf=_adf_residual_t(res)
    z=float((res[-1]-np.mean(res))/max(np.std(res,ddof=1),1e-12)) if len(res)>2 else None
    out.update({"state":"SHADOW","common_sessions":int(len(df)),"pca":{"first_component_explained_pct":explained*100,"loadings":{c:float(vecs[i,0]) for i,c in enumerate(cols)}},
                "pair":{"peer":peer,"rolling_correlation":corr,"kalman_beta":beta,"latest_residual":resid_last,"residual_z":z,"cointegration_adf_t_proxy":adf,"cointegration_status":"DIAGNOSTIC ONLY"}})
    return out


def ml_research_readiness(calibration: Dict[str,Any] | None) -> Dict[str,Any]:
    cal=calibration or {};n=int(cal.get("sample_size",0) or 0);sessions=int(cal.get("sessions",0) or 0)
    # Intentionally stricter than the probability gate; ML has more degrees of freedom.
    if n>=500 and sessions>=20:state="TRAINABLE"
    else:state="COLLECTING"
    return {"state":state,"samples":n,"sessions":sessions,"minimum_samples":500,"minimum_sessions":20,
            "candidates":["Gradient Boosted Trees","LightGBM/XGBoost compatible feature set"],"production_enabled":False,"affects_scanner":False,
            "note":"No se entrena ni promueve un modelo ML hasta tener muestra LIVE suficiente y validación OOS/walk-forward."}


def institutional_research_snapshot(storage: Path, symbol: str, ticks: pd.DataFrame | None,
                                    vol: Dict[str,Any] | None, calibration: Dict[str,Any] | None) -> Dict[str,Any]:
    return {"role":"RESEARCH_SHADOW","vol_forecast":volatility_forecast_lab(symbol,ticks,vol),
            "microstructure":microstructure_lab(ticks),"cross_asset":cross_asset_lab(Path(storage),symbol),
            "ml":ml_research_readiness(calibration),
            "authority":"Scanner LIVE unchanged. These labs collect/measure; Calibration is the promotion gate."}
