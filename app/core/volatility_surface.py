"""SVI volatility-surface research/scenario layer - arbitrage hardened in v1.27.12.

Observed IV remains primary evidence. SVI can fill sparse cells and create explicit
stress surfaces, but a slice is usable only when it passes both the legacy parameter /
Lee checks and a hard Durrleman butterfly-arbitrage check.  The optimizer receives a
soft arbitrage penalty; the post-fit hard check is the authority.
"""
from __future__ import annotations

from typing import Any, Dict
import math
import os
import numpy as np
import pandas as pd
from scipy.optimize import least_squares, brentq
from scipy.stats import norm

from .frame_guards import numeric_column
from .expiry_clock import year_fraction


def svi_total_variance(k, a, b, rho, m, sigma):
    k=np.asarray(k,float)
    return a+b*(rho*(k-m)+np.sqrt((k-m)**2+sigma**2))


def svi_w_prime(k, a, b, rho, m, sigma):
    k=np.asarray(k,float); z=k-m; den=np.sqrt(z*z+sigma*sigma)
    return b*(rho+z/np.maximum(den,1e-15))


def svi_w_double_prime(k, a, b, rho, m, sigma):
    k=np.asarray(k,float); z=k-m
    return b*sigma*sigma/np.maximum((z*z+sigma*sigma)**1.5,1e-15)


def durrleman_g(k, a, b, rho, m, sigma) -> np.ndarray:
    """Durrleman density condition. ``g(k) >= 0`` is required for butterfly no-arbitrage."""
    kk=np.asarray(k,float)
    w=np.maximum(svi_total_variance(kk,a,b,rho,m,sigma),1e-12)
    w1=svi_w_prime(kk,a,b,rho,m,sigma); w2=svi_w_double_prime(kk,a,b,rho,m,sigma)
    return (1.0-kk*w1/(2.0*w))**2-(w1*w1/4.0)*(1.0/w+0.25)+w2/2.0


def forward_price(spot: float, r: float, q: float, maturity_years: float) -> float:
    return float(spot)*math.exp((float(r)-float(q))*max(float(maturity_years),0.0))


def svi_basic_checks(params: Dict[str, float], k_domain=None, tol: float = 1e-10) -> Dict[str, Any]:
    """Legacy necessary checks. Kept separately for diagnostics/backward compatibility."""
    try:
        a=float(params["a"]); b=float(params["b"]); rho=float(params["rho"]); m=float(params["m"]); sig=float(params["sigma"])
    except Exception:
        return {"pass":False,"state":"FAIL","reason":"INVALID_PARAMS","checks":{}}
    domain=np.asarray(k_domain if k_domain is not None else np.linspace(-2.0,2.0,401),float)
    if domain.size < 2: domain=np.linspace(-2.0,2.0,401)
    vals=svi_total_variance(domain,a,b,rho,m,sig)
    left_slope=b*(1.0-rho); right_slope=b*(1.0+rho)
    checks={
        "b_nonnegative":bool(b>=-tol),
        "sigma_positive":bool(sig>tol),
        "rho_inside_unit":bool(abs(rho)<1.0),
        "total_variance_nonnegative_observed_domain":bool(np.isfinite(vals).all() and float(np.nanmin(vals))>=-tol),
        "lee_left_wing_slope_le_2":bool(left_slope<=2.0+1e-8),
        "lee_right_wing_slope_le_2":bool(right_slope<=2.0+1e-8),
    }
    ok=all(checks.values())
    return {"pass":ok,"state":"PASS" if ok else "CAUTION","checks":checks,
            "min_total_variance":None if not np.isfinite(vals).any() else float(np.nanmin(vals)),
            "left_wing_slope":float(left_slope),"right_wing_slope":float(right_slope),
            "scope":"BASIC_STATIC_CHECKS"}


def svi_full_checks(params: Dict[str, float], k_domain=None, tol: float = 1e-8) -> Dict[str, Any]:
    """Fail-closed raw-SVI validation including hard Durrleman butterfly checks."""
    domain=np.asarray(k_domain if k_domain is not None else np.linspace(-2.0,2.0,801),float)
    if domain.size < 5: domain=np.linspace(-2.0,2.0,801)
    basic=svi_basic_checks(params,domain,tol=min(tol,1e-10))
    if not basic.get("pass"):
        return {"pass":False,"state":"BASIC_STATIC_ARBITRAGE","reason":"BASIC_CHECK_FAILED","basic":basic,
                "butterfly_pass":False,"min_durrleman_g":None,"negative_fraction":None}
    try:
        g=durrleman_g(domain,**{k:float(params[k]) for k in ("a","b","rho","m","sigma")})
        finite=np.isfinite(g)
        if not finite.all():
            return {"pass":False,"state":"BUTTERFLY_ARBITRAGE","reason":"NONFINITE_DURRLEMAN","basic":basic,
                    "butterfly_pass":False,"min_durrleman_g":None,"negative_fraction":None}
        min_g=float(np.min(g)); neg=float(np.mean(g < -float(tol)))
        butterfly=bool(min_g >= -float(tol))
        return {"pass":bool(basic.get("pass") and butterfly),
                "state":"PASS" if butterfly else "BUTTERFLY_ARBITRAGE",
                "reason":None if butterfly else "NEGATIVE_RISK_NEUTRAL_DENSITY",
                "basic":basic,"butterfly_pass":butterfly,"min_durrleman_g":min_g,
                "negative_fraction":neg,"negative_pct":100.0*neg,
                "durrleman_tolerance":float(tol),"domain":[float(domain.min()),float(domain.max())],
                "scope":"HARD_DURRLEMAN_POST_FIT"}
    except Exception as exc:
        return {"pass":False,"state":"BUTTERFLY_ARBITRAGE","reason":f"DURRLEMAN_ERROR:{type(exc).__name__}",
                "basic":basic,"butterfly_pass":False,"min_durrleman_g":None,"negative_fraction":None}


def fit_svi_slice(strikes, iv, forward, maturity_years) -> Dict[str, Any]:
    K=np.asarray(strikes,float); vol=np.asarray(iv,float); T=max(float(maturity_years),1e-12); F=max(float(forward),1e-9)
    mask=np.isfinite(K)&np.isfinite(vol)&(K>0)&(vol>0)
    K=K[mask];vol=vol[mask]
    if len(K)<5:
        return {"ready":False,"reason":"NEED_5_STRIKES","model":"SVI"}
    k=np.log(K/F); w=(vol**2)*T
    a0=max(1e-10,float(np.nanmin(w))*0.8); b0=max(1e-6,float(np.nanstd(w))*2+0.001)
    x0=np.array([a0,b0,0.0,0.0,0.15])
    lo=np.array([0.0,1e-10,-0.999,-3.0,1e-5]); hi=np.array([max(5.0,float(np.nanmax(w))*5+1),10.0,0.999,3.0,5.0])
    arb_grid=np.linspace(-2.0,2.0,321)
    try: penalty=max(0.0,float(os.getenv("ITM_SVI_DURRLEMAN_PENALTY","12")))
    except Exception: penalty=12.0
    scale=max(float(np.nanmedian(w)),1e-5)
    def resid(x):
        model=svi_total_variance(k,*x)
        weights=1.0/(1.0+np.abs(k)*1.5)
        data=(model-w)*weights
        if penalty<=0:return data
        gv=durrleman_g(arb_grid,*x)
        neg=np.minimum(gv,0.0)*scale*math.sqrt(penalty)
        return np.concatenate([data,neg])
    try:
        res=least_squares(resid,x0,bounds=(lo,hi),loss="soft_l1",f_scale=max(float(np.nanmedian(w))*0.05,1e-8),max_nfev=1200)
        p=res.x; pred=svi_total_variance(k,*p); rmse=float(np.sqrt(np.mean((pred-w)**2)))
        params={"a":float(p[0]),"b":float(p[1]),"rho":float(p[2]),"m":float(p[3]),"sigma":float(p[4])}
        basic=svi_basic_checks(params,k_domain=np.linspace(float(np.nanmin(k)),float(np.nanmax(k)),max(101,len(k)*8)))
        full=svi_full_checks(params,k_domain=np.linspace(-2.0,2.0,801))
        ready=bool(res.success and full.get("pass"))
        reason=None if ready else (full.get("state") if res.success else "FIT_NOT_CONVERGED")
        return {"ready":ready,"fit_converged":bool(res.success),"params":params,"rmse_total_variance":rmse,
                "n":len(K),"model":"SVI","basic_no_arbitrage":basic,"arbitrage_checks":full,
                "butterfly_state":full.get("state"),"reason":reason,
                "optimizer_arbitrage_penalty":penalty,"hard_validation":"DURRLEMAN_FAIL_CLOSED"}
    except Exception as exc:
        return {"ready":False,"reason":f"{type(exc).__name__}: {exc}"[:180],"model":"SVI"}


def evaluate_svi(strikes, forward, maturity_years, params):
    K=np.asarray(strikes,float); F=max(float(forward),1e-9);T=max(float(maturity_years),1e-12)
    k=np.log(np.maximum(K,1e-9)/F)
    w=np.maximum(svi_total_variance(k,params["a"],params["b"],params["rho"],params["m"],params["sigma"]),1e-12)
    return np.sqrt(w/T)


def _calendar_diagnostics(slices: list[dict]) -> Dict[str, Any]:
    ready=[s for s in slices if s.get("ready") and s.get("params")]
    if len(ready)<2:
        return {"ready":False,"state":"COLLECTING","violations":0,"pairs":0,"note":"Need >=2 hard-valid SVI maturities."}
    ready=sorted(ready,key=lambda s:float(s.get("dte",0) or 0))
    grid=np.linspace(-0.20,0.20,41); violations=0;pairs=0;worst=0.0
    prev=None
    for s in ready:
        w=svi_total_variance(grid,**s["params"])
        if prev is not None:
            diff=np.asarray(w)-np.asarray(prev)
            bad=diff < -1e-8; violations+=int(np.sum(bad));pairs+=len(diff)
            if bad.any(): worst=min(worst,float(np.nanmin(diff)))
        prev=w
    state="PASS" if violations==0 else "CAUTION"
    return {"ready":True,"state":state,"violations":violations,"pairs":pairs,
            "violation_pct":round(100.0*violations/max(pairs,1),3),"worst_total_variance_step":float(worst),
            "scope":"COMMON_LOG_MONEYNESS_CALENDAR_DIAGNOSTIC"}


def _slice_rates(g: pd.DataFrame, symbol: str, dte: float, r: float | None, q: float | None) -> tuple[float,float,str]:
    rr=numeric_column(g,"model_risk_free_rate",float("nan")).dropna() if "model_risk_free_rate" in g else pd.Series(dtype=float)
    qq=numeric_column(g,"model_dividend_yield",float("nan")).dropna() if "model_dividend_yield" in g else pd.Series(dtype=float)
    if r is not None and q is not None:return float(r),float(q),"EXPLICIT"
    if not rr.empty and not qq.empty:return float(rr.median()),float(qq.median()),"CHAIN_MODEL_INPUTS"
    try:
        from .precision_engine import market_inputs
        mi=market_inputs(symbol or "DIA",dte)
        return float(mi["risk_free_rate"]),float(mi["dividend_yield"]),"PRECISION_ENGINE"
    except Exception:
        # Fail visibly to spot-forward rather than silently injecting arbitrary carry.
        return 0.0,0.0,"CARRY_UNAVAILABLE_SPOT_FORWARD"


def fit_surface(chain: pd.DataFrame, spot: float | None = None, *, r: float | None = None,
                q: float | None = None, symbol: str | None = None) -> Dict[str, Any]:
    if not isinstance(chain,pd.DataFrame) or chain.empty:
        return {"ready":False,"reason":"NO_CHAIN","slices":[],"model":"SVI"}
    x=chain.copy()
    for c in ("strike","iv","dte","underlying_price"):
        x[c]=pd.to_numeric(x.get(c),errors="coerce")
    x=x.dropna(subset=["strike","iv","dte"])
    if x.empty:return {"ready":False,"reason":"NO_VALID_IV","slices":[],"model":"SVI"}
    if "expiration_date" not in x.columns:
        x["expiration_date"]=x["dte"].round(8).astype(str)
    series=numeric_column(x,"underlying_price",float("nan")).dropna()
    S=float(spot) if spot is not None and float(spot)>0 else (float(series.median()) if not series.empty else 0.0)
    if S<=0:return {"ready":False,"reason":"NO_SPOT","slices":[],"model":"SVI"}
    sym=str(symbol or (x.get("underlying_symbol",pd.Series(["DIA"])).dropna().astype(str).iloc[0] if "underlying_symbol" in x and x["underlying_symbol"].notna().any() else "DIA")).upper()
    slices=[]
    for exp,g in x.groupby(x["expiration_date"].astype(str)):
        gg=g.groupby("strike",as_index=False).agg(iv=("iv","mean"),dte=("dte","median"))
        dte=float(gg["dte"].median()); T=year_fraction(dte)
        rr,qq,rate_source=_slice_rates(g,sym,dte,r,q)
        F=forward_price(S,rr,qq,T)
        fit=fit_svi_slice(gg["strike"].to_numpy(),gg["iv"].to_numpy(),F,T)
        fit.update({"expiration_date":str(exp),"dte":dte,"maturity_years":T,"forward":F,
                    "spot":S,"risk_free_rate":rr,"dividend_yield":qq,"forward_source":rate_source})
        slices.append(fit)
    ready=[s for s in slices if s.get("ready")]
    cal=_calendar_diagnostics(slices)
    calendar_state=str(cal.get("state") or "COLLECTING").upper()
    calendar_arb_free=calendar_state == "PASS"
    # `ready` preserves backwards compatibility: at least one hard-Durrleman-valid slice
    # exists for diagnostics/interpolation.  `surface_operational_ready` is the stronger
    # v1.27.19 contract: a multi-expiry surface is not promoted as globally coherent
    # until the calendar diagnostic itself passes.  COLLECTING remains explicit rather
    # than being silently equated with PASS.
    surface_operational_ready=bool(ready) and calendar_arb_free
    quality="PASS" if surface_operational_ready else "COLLECTING" if ready and calendar_state=="COLLECTING" else "CAUTION" if ready else "UNAVAILABLE"
    return {"ready":bool(ready),"slice_ready":bool(ready),"surface_operational_ready":surface_operational_ready,
            "surface_calendar_arb_free":calendar_arb_free,"calendar_state":calendar_state,
            "model":"SVI","slices":slices,"ready_slices":len(ready),"total_slices":len(slices),
            "calendar_diagnostics":cal,"quality_state":quality,
            "butterfly_policy":"DURRLEMAN_HARD_REJECT","calendar_policy":"GLOBAL_STATUS_FAIL_CLOSED_FOR_PROMOTION",
            "forward_policy":"S_EXP_R_MINUS_Q_T",
            "model_risk":"SVI is a smoothing/stress layer. Slice usability requires hard Durrleman; global operational promotion additionally requires calendar PASS."}


def _strike_for_forward_delta(target_delta: float, forward: float, T: float, params: Dict[str, float],
                              *, is_call: bool) -> float | None:
    """Strike whose Black-76 forward delta equals target_delta, self-consistent
    with the SVI-implied vol AT that strike (the vol itself depends on the strike,
    so this is solved, not looked up from a flat vol assumption).
    """
    F = max(float(forward), 1e-9); T = max(float(T), 1e-12)
    def _delta_at(k_strike: float) -> float:
        sigma = max(float(evaluate_svi([k_strike], F, T, params)[0]), 1e-6)
        d1 = (math.log(F / max(k_strike, 1e-9)) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
        return float(norm.cdf(d1)) if is_call else float(norm.cdf(d1) - 1.0)
    lo, hi = F * 0.05, F * 8.0
    try:
        f_lo, f_hi = _delta_at(lo) - target_delta, _delta_at(hi) - target_delta
        if f_lo * f_hi > 0:
            return None
        return float(brentq(lambda k: _delta_at(k) - target_delta, lo, hi, maxiter=100, xtol=1e-6))
    except Exception:
        return None


def skew_25d(slice_fit: Dict[str, Any], *, target_delta: float = 0.25) -> Dict[str, Any]:
    """25-delta skew for one already-fitted SVI expiry slice.

    Skew := IV(put, delta=-target_delta) - IV(call, delta=+target_delta), expressed
    in vol points. This is the standard "risk reversal" skew definition used across
    equity/index vol markets (a negative index skew -- puts richer than calls -- is
    the normal, expected sign; it is not itself a signal).

    Strikes are solved with the forward (Black-76-style) delta convention, standard
    for quoting 25-delta skew, and are self-consistent with the SVI smile: the
    solver re-evaluates SVI at each candidate strike rather than assuming one flat
    vol for the whole search.
    """
    out = {"ready": False}
    if not isinstance(slice_fit, dict) or not slice_fit.get("ready") or not slice_fit.get("params"):
        return out
    F = slice_fit.get("forward"); T = slice_fit.get("maturity_years"); params = slice_fit["params"]
    if F is None or T is None:
        return out
    k_put = _strike_for_forward_delta(-abs(target_delta), F, T, params, is_call=False)
    k_call = _strike_for_forward_delta(abs(target_delta), F, T, params, is_call=True)
    if k_put is None or k_call is None:
        return {"ready": False, "reason": "DELTA_STRIKE_NOT_BRACKETED"}
    iv_put = float(evaluate_svi([k_put], F, T, params)[0])
    iv_call = float(evaluate_svi([k_call], F, T, params)[0])
    iv_atm = float(evaluate_svi([F], F, T, params)[0])
    return {
        "ready": True, "target_delta": float(abs(target_delta)),
        "put_strike": k_put, "call_strike": k_call,
        "put_iv_pct": iv_put * 100.0, "call_iv_pct": iv_call * 100.0, "atm_iv_pct": iv_atm * 100.0,
        "skew_25d_pct": (iv_put - iv_call) * 100.0,
        "convention": "FORWARD_DELTA_SELF_CONSISTENT_SVI",
        "model_risk": "Skew derivado de la superficie SVI ajustada, no observado punto a punto en el mercado.",
    }


def skew_term_structure(chain: pd.DataFrame, spot: float | None = None, *, r: float | None = None,
                        q: float | None = None, symbol: str | None = None,
                        target_delta: float = 0.25) -> Dict[str, Any]:
    """25-delta skew by expiry -- the SpotGamma-style "Skew Model" term structure.

    Reuses fit_surface() exactly as-is (same hard Durrleman validation); a slice
    that fails the arbitrage check is excluded here just as it is everywhere else
    this surface is consumed.
    """
    fit = fit_surface(chain, spot, r=r, q=q, symbol=symbol)
    rows = []
    for s in fit.get("slices", []):
        sk = skew_25d(s, target_delta=target_delta)
        if sk.get("ready"):
            rows.append({"expiration_date": s.get("expiration_date"), "dte": s.get("dte"), **sk})
    rows.sort(key=lambda r: float(r.get("dte") or 0.0))
    return {
        "ready": bool(rows), "rows": rows, "model": "SVI", "target_delta": float(abs(target_delta)),
        "surface_quality_state": fit.get("quality_state"),
        "model_risk": "Serie de skew derivada de superficies SVI por vencimiento; requiere validación Durrleman por slice.",
    }


def surface_matrix(chain: pd.DataFrame, strikes, expiries, spot: float, *, scenario_iv_shift: float = 0.0):
    """Return IV matrix with explicit OBSERVED/model provenance and hard-valid SVI only."""
    strikes=[float(s) for s in strikes];expiries=[str(e) for e in expiries]
    x=chain.copy();x["expiration_date"]=x["expiration_date"].astype(str)
    raw=x.pivot_table(index="strike",columns="expiration_date",values="iv",aggfunc="mean").reindex(index=strikes,columns=expiries)
    raw_mat=raw.to_numpy(float)
    fit=fit_surface(x,spot)
    by={s.get("expiration_date"):s for s in fit.get("slices",[]) if s.get("ready")}
    mat=np.array(raw_mat,copy=True);source=np.full(mat.shape,"MISSING",dtype=object)
    source[np.isfinite(mat)]="OBSERVED"
    scenario=abs(float(scenario_iv_shift))>1e-15
    for j,e in enumerate(expiries):
        s=by.get(e); vals=None
        if s:
            vals=evaluate_svi(strikes,float(s["forward"]),float(s["maturity_years"]),s["params"])
        for i in range(len(strikes)):
            obs=raw_mat[i,j]
            model=float(vals[i]) if vals is not None and math.isfinite(float(vals[i])) else float("nan")
            if scenario:
                if math.isfinite(model):
                    mat[i,j]=max(model+float(scenario_iv_shift),1e-4);source[i,j]="SVI_SCENARIO"
                elif math.isfinite(obs):
                    mat[i,j]=max(float(obs)+float(scenario_iv_shift),1e-4);source[i,j]="OBSERVED_SHIFT_FALLBACK"
            elif not math.isfinite(obs) and math.isfinite(model):
                mat[i,j]=model;source[i,j]="SVI_FILL"
    fallback=float(np.nanmedian(raw_mat)) if np.isfinite(raw_mat).any() else 0.20
    missing=~np.isfinite(mat)
    mat=np.where(missing,fallback,mat);source[missing]="MEDIAN_FALLBACK"
    mat=np.maximum(mat,1e-4)
    fit["observed_cells_preserved"] = int(np.sum((source=="OBSERVED"))) if not scenario else 0
    fit["scenario_mode"] = bool(scenario);fit["scenario_iv_shift"] = float(scenario_iv_shift)
    return mat,source,fit
