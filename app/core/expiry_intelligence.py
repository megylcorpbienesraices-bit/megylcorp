"""Actual-date expiry intelligence. Never assumes provider bucket zero==0DTE or one==1DTE."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
import pandas as pd
from .contract_spec import multiplier_series


def _latest(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    if "timestamp" in x.columns:
        ts = pd.to_datetime(x["timestamp"], errors="coerce")
        if ts.notna().any(): x = x[ts == ts.max()].copy()
    return x


def _exp_col(x: pd.DataFrame) -> pd.Series:
    c = "expiration_date" if "expiration_date" in x.columns else "expiration" if "expiration" in x.columns else None
    return pd.to_datetime(x[c], errors="coerce").dt.date if c else pd.Series(pd.NaT, index=x.index)


def _metric_totals(x: pd.DataFrame) -> dict[str, float]:
    def s(name: str) -> pd.Series:
        return pd.to_numeric(x[name], errors="coerce").fillna(0.0) if name in x.columns else pd.Series(0.0, index=x.index)
    oi=s("open_interest");vol=s("volume");mult=multiplier_series(x);spot=s("underlying_price").replace(0,1.0)
    return {
        "gamma": float(s("signed_gex_proxy").sum()), "gamma_abs": float(s("signed_gex_proxy").abs().sum()),
        "delta": float(s("option_delta_exposure_info").sum()), "delta_abs": float(s("option_delta_exposure_info").abs().sum()),
        "vanna": float((s("calc_vanna")*oi*mult*spot).sum()), "charm": float((s("calc_charm")*oi*mult*spot).sum()),
        "oi": float(oi.sum()), "volume": float(vol.sum()), "contracts": int(len(x)),
    }


def build_expiry_intelligence(snapshot: pd.DataFrame, *, asof: date | None = None) -> dict[str, Any]:
    if snapshot is None or snapshot.empty:
        return {"ready": False, "reason": "NO_CHAIN"}
    x=_latest(snapshot); exp=_exp_col(x); x=x.assign(_exp=exp).dropna(subset=["_exp"])
    if x.empty: return {"ready":False,"reason":"NO_EXPIRATION_DATES"}
    ref=asof or (pd.to_datetime(x.get("timestamp"),errors="coerce").dropna().max().date() if "timestamp" in x.columns and pd.to_datetime(x.get("timestamp"),errors="coerce").notna().any() else date.today())
    dates=sorted({d for d in x["_exp"] if d>=ref})
    if not dates:return {"ready":False,"reason":"NO_FUTURE_EXPIRATIONS"}
    next_exp=dates[0];second_exp=dates[1] if len(dates)>1 else None
    friday=ref+timedelta(days=(4-ref.weekday())%7); next_friday=friday+timedelta(days=7)
    month_end=(date(ref.year+1,1,1)-timedelta(days=1)) if ref.month==12 else (date(ref.year,ref.month+1,1)-timedelta(days=1))
    groups={
        "0DTE REAL": x["_exp"]==ref,
        "PRÓXIMO VENCIMIENTO": x["_exp"]==next_exp,
        "2.º VENCIMIENTO": x["_exp"]==second_exp if second_exp else pd.Series(False,index=x.index),
        "SEMANA ACTUAL": (x["_exp"]>=ref)&(x["_exp"]<=friday),
        "PRÓXIMA SEMANA": (x["_exp"]>friday)&(x["_exp"]<=next_friday),
        "MES": (x["_exp"]>=ref)&(x["_exp"]<=month_end),
        "FULL": pd.Series(True,index=x.index),
    }
    per_exp=[]
    for d, sub in x.groupby("_exp"):
        per_exp.append({"expiration":d.isoformat(),"dte":int((d-ref).days),**_metric_totals(sub)})
    gamma_den=sum(abs(r["gamma"]) for r in per_exp) or 1.0;delta_den=sum(abs(r["delta"]) for r in per_exp) or 1.0
    for r in per_exp:
        r["gamma_concentration_pct"]=round(abs(r["gamma"])/gamma_den*100.0,2);r["delta_concentration_pct"]=round(abs(r["delta"])/delta_den*100.0,2)
    buckets=[]
    for name,mask in groups.items():
        sub=x[mask.fillna(False)]; buckets.append({"bucket":name,"expirations":sorted({d.isoformat() for d in sub["_exp"]}),**_metric_totals(sub)})
    top_gamma=max(per_exp,key=lambda r:abs(r["gamma"])) if per_exp else None;top_delta=max(per_exp,key=lambda r:abs(r["delta"])) if per_exp else None
    return {"ready":True,"asof":ref.isoformat(),"actual_expirations":[d.isoformat() for d in dates],"buckets":buckets,"per_expiration":per_exp,
            "top_gamma_expiration":top_gamma,"top_delta_expiration":top_delta,
            "provider_bucket_rule":"NEVER_HARDCODE_ZERO_AS_0DTE_OR_ONE_AS_1DTE",
            "authority":"EXPIRY_STRUCTURE_CONTEXT"}


def resolve_provider_near_expiry_buckets(*, min_dte: int | None, second_min_dte: int | None) -> dict[str, Any]:
    return {"provider_bucket_zero":{"actual_dte":min_dte,"label":f"PRIMER VENCIMIENTO · {min_dte}DTE" if min_dte is not None else "UNRESOLVED"},
            "provider_bucket_one":{"actual_dte":second_min_dte,"label":f"SEGUNDO VENCIMIENTO · {second_min_dte}DTE" if second_min_dte is not None else "UNRESOLVED"},
            "warning":"Provider bucket names are aliases, not calendar truth."}
