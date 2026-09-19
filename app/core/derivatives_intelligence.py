"""Observed-vs-model derivatives flow intelligence for v1.26.0.

No proprietary provider formula is reverse engineered. Provider-native GEX/DEX/convexity
flow, when explicitly supplied, is labelled OBSERVED. ITM model flow is derived causally
from consecutive comparable chain snapshots and labelled MODEL/PROXY.
"""

from __future__ import annotations

from typing import Any
import math
import pandas as pd
from .contract_spec import multiplier_series


def _f(v: Any) -> float | None:
    try:
        x=float(v);return x if math.isfinite(x) else None
    except Exception:return None


def _latest_two(history: pd.DataFrame) -> tuple[pd.DataFrame,pd.DataFrame] | None:
    if history is None or history.empty or "timestamp" not in history.columns:return None
    x=history.copy();x["timestamp"]=pd.to_datetime(x["timestamp"],errors="coerce");x=x.dropna(subset=["timestamp"])
    times=sorted(x["timestamp"].unique())
    if len(times)<2:return None
    return x[x["timestamp"]==times[-2]].copy(),x[x["timestamp"]==times[-1]].copy()


def _aggregate_snapshot(x: pd.DataFrame) -> dict[str,float]:
    def s(c): return pd.to_numeric(x[c],errors="coerce").fillna(0.0) if c in x.columns else pd.Series(0.0,index=x.index)
    oi=s("open_interest");mult=multiplier_series(x);spot=s("underlying_price").replace(0,1.0)
    return {"gex":float(s("signed_gex_proxy").sum()),"dex":float(s("option_delta_exposure_info").sum()),
            "vanna":float((s("calc_vanna")*oi*mult*spot).sum()),"charm":float((s("calc_charm")*oi*mult*spot).sum()),
            "gross_gamma":float(s("signed_gex_proxy").abs().sum())}


def _sgn(v: float | None, dead: float = 1e-12) -> int:
    return 1 if v is not None and v>dead else -1 if v is not None and v<-dead else 0


def build_derivatives_intelligence(history: pd.DataFrame, *, observed: dict[str,Any] | None = None) -> dict[str,Any]:
    pair=_latest_two(history); model={"ready":False,"reason":"NEED_TWO_CAUSAL_SNAPSHOTS"}
    if pair:
        prev,cur=pair;a=_aggregate_snapshot(prev);b=_aggregate_snapshot(cur)
        gross=max(abs(a["gross_gamma"]),abs(b["gross_gamma"]),1.0)
        gex_flow=b["gex"]-a["gex"];dex_flow=b["dex"]-a["dex"];vanna_flow=b["vanna"]-a["vanna"];charm_flow=b["charm"]-a["charm"]
        # Explicit bounded proxy: change in net gamma relative to gross gamma.  This is
        # NOT a provider's proprietary convexity-flow formula.
        convexity_proxy=max(-1.0,min(1.0,gex_flow/gross))
        model={"ready":True,"gex_flow_model":gex_flow,"dex_flow_model":dex_flow,"vanna_flow_model":vanna_flow,"charm_flow_model":charm_flow,
               "convexity_flow_proxy":convexity_proxy,"directional":{"gex":_sgn(gex_flow),"dex":_sgn(dex_flow),"convexity":_sgn(convexity_proxy)},
               "definition":"CAUSAL_CHANGE_BETWEEN_COMPARABLE_CHAIN_SNAPSHOTS","convexity_definition":"DELTA_NET_GEX_DIVIDED_BY_GROSS_GAMMA_BOUNDED_-1_1","authority":"ITM_MODEL"}
    obs=observed or {}; native=(obs.get("provider_native_structure") or {}) if isinstance(obs,dict) else {}
    _native_values={
        "gex_flow":_f(obs.get("gex_flow")), "dex_flow":_f(obs.get("dex_flow")),
        "convexity_flow":_f(obs.get("order_flow_convexity")), "net_vanna":_f(native.get("net_vanna")),
        "net_charm":_f(native.get("net_charm")), "net_gex":_f(native.get("net_gex")), "net_dex":_f(native.get("net_dex")),
    }
    # Structural context is not provider-native live flow. Mark OBSERVED ready only
    # when a supported native
    # numeric field is actually present; never turn `available=True` into fake flow.
    _native_ready=any(v is not None for v in _native_values.values())
    observed_block={"ready":_native_ready, **_native_values,
                    "source":obs.get("source") or "UNSPECIFIED_PROVIDER",
                    "authority":"OBSERVED_PROVIDER_NATIVE" if _native_ready else "NO_PROVIDER_NATIVE_FLOW",
                    "structural_context_only":bool(obs.get("authority")=="STRUCTURAL_CONTEXT_SOURCE")}
    divergence={}
    if model.get("ready") and observed_block.get("ready"):
        for k, mk, ok in (("gex","gex_flow_model","gex_flow"),("dex","dex_flow_model","dex_flow"),("convexity","convexity_flow_proxy","convexity_flow")):
            ms=_sgn(_f(model.get(mk)));os=_sgn(_f(observed_block.get(ok)))
            divergence[k]={"model_sign":ms,"observed_sign":os,"sign_agreement":None if not ms or not os else ms==os}
    votes=[x for x in (model.get("directional") or {}).values() if x]
    model_sign=1 if sum(votes)>0 else -1 if sum(votes)<0 else 0
    return {"ready":bool(model.get("ready") or observed_block.get("ready")),"model":model,"observed":observed_block,"divergence":divergence,
            "directional":{"sign":model_sign,"direction":"BUY" if model_sign>0 else "SELL" if model_sign<0 else "NEUTRAL","confidence":min(100,len(votes)*25) if votes else 0},
            "policy":"OBSERVED_AND_MODEL_SEPARATE · NO_PROPRIETARY_FORMULA_CLONING · NO_RAW_MAGNITUDE_FUSION_ACROSS_INCOMPATIBLE_UNITS",
            "authority":"SHADOW_DERIVATIVES_INTELLIGENCE_UNTIL_OOS_VALIDATED"}
