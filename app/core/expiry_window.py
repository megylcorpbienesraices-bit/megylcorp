from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
import math
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd
from .frame_guards import numeric_column
from .obs import note as _obs_note

WINDOW_AUTO = "AUTO"
WINDOW_0DTE = "0DTE"
WINDOW_WEEK = "WEEK"
WINDOW_2W = "2W"
WINDOW_MONTH = "MONTH"
WINDOW_ALL = "ALL"
WINDOWS = (WINDOW_AUTO, WINDOW_0DTE, WINDOW_WEEK, WINDOW_2W, WINDOW_MONTH, WINDOW_ALL)

LABELS = {
    WINDOW_AUTO: "AUTO · MULTI-EXPIRY INTRADAY",
    WINDOW_0DTE: "0DTE",
    WINDOW_WEEK: "SEMANA ACTUAL",
    WINDOW_2W: "2 SEMANAS",
    WINDOW_MONTH: "MES ACTUAL",
    WINDOW_ALL: "TODOS CARGADOS",
}

ALIASES = {
    "AUTO": WINDOW_AUTO, "AUTO · INTRADAY": WINDOW_AUTO, "AUTO · MULTI-EXPIRY INTRADAY": WINDOW_AUTO, "INTRADAY": WINDOW_AUTO,
    "0DTE": WINDOW_0DTE, "HOY": WINDOW_0DTE,
    "WEEK": WINDOW_WEEK, "SEMANA": WINDOW_WEEK, "SEMANA ACTUAL": WINDOW_WEEK,
    "2W": WINDOW_2W, "2 SEMANAS": WINDOW_2W, "DOS SEMANAS": WINDOW_2W,
    "MONTH": WINDOW_MONTH, "MES": WINDOW_MONTH, "MES ACTUAL": WINDOW_MONTH,
    "ALL": WINDOW_ALL, "TODOS": WINDOW_ALL, "TODOS CARGADOS": WINDOW_ALL,
}


def normalize_window(value: str | None) -> str:
    key = str(value or WINDOW_AUTO).strip().upper().replace("  ", " ")
    return ALIASES.get(key, WINDOW_AUTO)


# v1.16.2 · SEMÁNTICA ESTRICTA DE VENCIMIENTO
# 0DTE se define por FECHA, nunca por DTE fraccional. Un contrato con dte=0.9 que vence
# mañana NO es 0DTE, y uno con dte=0.01 que vence hoy SÍ lo es. Toda la plataforma debe
# preguntar aquí en lugar de reimplementar la regla con umbrales numéricos.
EXPIRY_UNKNOWN = "UNKNOWN"


def is_zero_dte(df: pd.DataFrame, asof: date | None = None) -> pd.Series:
    """Máscara booleana: expiration_date == fecha de mercado. Sin excepciones."""
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    ref = asof or reference_date(df)
    if "expiration_date" not in df.columns:
        # Sin fecha no se puede afirmar 0DTE. Nunca se infiere desde el DTE fraccional.
        return pd.Series(False, index=df.index)
    exp = pd.to_datetime(df["expiration_date"], errors="coerce").dt.date
    return (exp == ref).fillna(False)


def zero_dte_status(df: pd.DataFrame, asof: date | None = None) -> Dict[str, Any]:
    """¿Existe hoy un vencimiento para este activo? Se pregunta a la cadena, no a una lista.

    Los calendarios de expiración cambian por producto y con el tiempo, así que codificar
    "SPY tiene 0DTE, GDX no" envejece mal. Preguntar a la cadena funciona para cualquier
    activo nuevo sin tocar código.
    """
    if df is None or df.empty:
        return {"available": False, "expirations_today": 0, "label": "SIN CADENA", "asof": None}
    ref = asof or reference_date(df)
    mask = is_zero_dte(df, ref)
    n = int(mask.sum())
    unknown = int(df["expiration_date"].isna().sum()) if "expiration_date" in df.columns else len(df)
    return {"available": n > 0, "expirations_today": 1 if n else 0, "contracts_today": n,
            "unscoped_contracts": unknown, "asof": str(ref),
            "label": "0DTE DISPONIBLE" if n else "SIN 0DTE HOY"}


def _expiration_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="datetime64[ns]")
    if "expiration_date" in df.columns:
        exp = pd.to_datetime(df["expiration_date"], errors="coerce").dt.normalize()
    else:
        # v1.16.2: ya NO se reconstruye la expiración con ceil(dte). Con dte=0.25 un lunes,
        # ceil daba martes cuando el contrato vencía ese mismo lunes, contaminando
        # justamente el análisis 0DTE. Sin fecha de la fuente, el contrato queda sin
        # ámbito en lugar de recibir una fecha inventada.
        exp = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    return exp


def ensure_expiration_date(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    if x.empty:
        return x
    exp = _expiration_series(x)
    x["expiration_date"] = exp.dt.date.astype("string")
    return x


def reference_date(df: pd.DataFrame) -> date:
    if df is not None and not df.empty and "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], errors="coerce").dropna()
        if not ts.empty:
            return ts.max().date()
    return pd.Timestamp.now().date()


def _bounds(asof: date) -> Dict[str, date]:
    friday = asof + timedelta(days=(4 - asof.weekday()) % 7)
    next_friday = friday + timedelta(days=7)
    month_end = date(asof.year, asof.month, monthrange(asof.year, asof.month)[1])
    return {"today": asof, "friday": friday, "next_friday": next_friday, "month_end": month_end}


def _dates_for_window(all_dates: list[date], mode: str, asof: date) -> list[date]:
    mode = normalize_window(mode); b = _bounds(asof)
    ds = sorted(d for d in all_dates if d >= asof)
    if mode == WINDOW_0DTE:
        return [d for d in ds if d == asof]
    if mode == WINDOW_WEEK:
        return [d for d in ds if d <= b["friday"]]
    if mode in {WINDOW_2W, WINDOW_AUTO}:
        out = [d for d in ds if d <= b["next_friday"]]
        # Some ETFs do not have a listed expiry in the strict interval in demos/holidays.
        return out or ds[: min(3, len(ds))]
    if mode == WINDOW_MONTH:
        return [d for d in ds if d <= b["month_end"]]
    return ds


def _minmax(values: pd.Series) -> pd.Series:
    s = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if s.empty: return s
    lo, hi = float(s.min()), float(s.max())
    if math.isclose(lo, hi): return pd.Series(np.ones(len(s))*0.5, index=s.index)
    return (s-lo)/(hi-lo)


def auto_expiry_weights(df: pd.DataFrame, selected_dates: list[date], asof: date, lam: float = 0.12) -> Dict[str, float]:
    """Adaptive intraday weights: DTE decay corrected by observed structure/activity/liquidity.

    The weight is not a probability. It is a transparent relative contribution factor.
    """
    if df is None or df.empty or not selected_dates:
        return {}
    x = ensure_expiration_date(df)
    x["_exp"] = pd.to_datetime(x["expiration_date"], errors="coerce").dt.date
    if "timestamp" in x.columns:
        ts = pd.to_datetime(x["timestamp"], errors="coerce")
        if ts.notna().any(): x = x[ts == ts.max()].copy()
    x = x[x["_exp"].isin(selected_dates)].copy()
    if x.empty: return {}
    x["open_interest"] = numeric_column(x,"open_interest",0).clip(lower=0)
    x["volume"] = numeric_column(x,"volume",0).clip(lower=0)
    x["dte"] = numeric_column(x,"dte",0).clip(lower=0)
    gamma_src = x["provider_gamma"] if "provider_gamma" in x.columns else pd.Series(np.nan, index=x.index)
    gamma = pd.to_numeric(gamma_src, errors="coerce").abs()
    # Fallback structural gamma proxy when provider gamma is unavailable.
    fallback = 1.0 / np.sqrt(np.maximum(x["dte"].to_numpy(float), 0.05))
    x["_gamma_proxy"] = np.where(np.isfinite(gamma), gamma, fallback) * x["open_interest"]
    x["_voloi"] = x["volume"] / (x["open_interest"] + 1.0)
    bid = pd.to_numeric(x["bid"] if "bid" in x.columns else pd.Series(np.nan,index=x.index), errors="coerce")
    ask = pd.to_numeric(x["ask"] if "ask" in x.columns else pd.Series(np.nan,index=x.index), errors="coerce")
    mid = (bid+ask)/2.0
    spread_pct = (ask-bid).abs()/mid.replace(0,np.nan)
    x["_liq"] = np.where(np.isfinite(spread_pct), 1.0/(1.0+25.0*spread_pct.clip(lower=0)), 0.0)
    g = x.groupby("_exp", as_index=False).agg(
        dte=("dte","median"), gamma=("_gamma_proxy","sum"), oi=("open_interest","sum"),
        volume=("volume","sum"), voloi=("_voloi","mean"), liquidity=("_liq","mean")
    )
    for c in ["gamma","oi","volume","voloi","liquidity"]:
        g[c+"_n"] = _minmax(np.log1p(g[c].clip(lower=0)) if c != "liquidity" else g[c])
    g["activity"] = .30*g["gamma_n"] + .24*g["oi_n"] + .20*g["volume_n"] + .14*g["voloi_n"] + .12*g["liquidity_n"]
    # 0DTE receives the maximum time priority; farther expiries can regain weight through activity.
    g["decay"] = np.exp(-float(lam)*g["dte"].clip(lower=0))
    g["raw_weight"] = g["decay"] * (0.62 + 0.78*g["activity"])
    mx = max(float(g["raw_weight"].max()), 1e-9)
    g["weight"] = (g["raw_weight"]/mx).clip(lower=0.08, upper=1.0)
    return {d.isoformat(): float(w) for d,w in zip(g["_exp"],g["weight"])}


def apply_expiry_window(df: pd.DataFrame, mode: str = WINDOW_AUTO) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    mode = normalize_window(mode)
    if df is None or df.empty:
        return pd.DataFrame(), {"mode":mode,"label":LABELS[mode],"expirations":[],"count":0,"weights":{},"weighted":mode==WINDOW_AUTO}
    x = ensure_expiration_date(df)
    exp = pd.to_datetime(x["expiration_date"], errors="coerce")
    all_dates = sorted(set(d.date() for d in exp.dropna()))
    asof = reference_date(x)
    selected = _dates_for_window(all_dates, mode, asof)
    mask = exp.dt.date.isin(selected)
    y = x.loc[mask].copy()
    weights = auto_expiry_weights(y, selected, asof) if mode == WINDOW_AUTO else {d.isoformat():1.0 for d in selected}
    if not y.empty:
        y["expiry_weight"] = pd.to_datetime(y["expiration_date"], errors="coerce").dt.date.map(lambda d: weights.get(d.isoformat(),1.0) if pd.notna(d) else 1.0)
        y["raw_open_interest"] = numeric_column(y,"open_interest",0)
        y["raw_volume"] = numeric_column(y,"volume",0)
        if mode == WINDOW_AUTO:
            y["open_interest"] = y["raw_open_interest"] * y["expiry_weight"]
            y["volume"] = y["raw_volume"] * y["expiry_weight"]
    return y, {
        "mode": mode, "label": LABELS[mode], "recommended": mode==WINDOW_AUTO,
        "asof": asof.isoformat(), "expirations": [d.isoformat() for d in selected], "count": len(selected),
        "all_loaded_expirations": [d.isoformat() for d in all_dates], "all_loaded_count": len(all_dates),
        "weights": weights, "weighted": mode==WINDOW_AUTO,
        "rule": "AUTO pondera DTE + Gamma + OI + volumen + Vol/OI + liquidez; los demás modos filtran sin ponderar.",
    }



def effective_horizon_days(df: pd.DataFrame, mode: str = WINDOW_AUTO, asof: date | None = None) -> Dict[str, Any]:
    """Effective option horizon for strike-window sigma sizing. No fixed per-ticker days.

    Uses the actual expirations selected by the expiry window. AUTO uses its adaptive expiry
    weights; strict modes use the median of their selected expirations. 0DTE uses the
    remaining fraction of the current market day when timestamp is available.
    """
    mode=normalize_window(mode)
    if df is None or df.empty:
        return {"ready":False,"days":None,"source":"NO_CHAIN"}
    x=ensure_expiration_date(df)
    ref=asof or reference_date(x)
    exp=pd.to_datetime(x.get("expiration_date"),errors="coerce").dt.date
    dates=sorted({d for d in exp.dropna() if d>=ref})
    selected=_dates_for_window(dates,mode,ref)
    if not selected:
        return {"ready":False,"days":None,"source":"NO_SELECTED_EXPIRY","expirations":[]}
    # Fraction of calendar day remaining for a same-day expiry. This value only sizes the
    # strike request; pricing Greeks continues to use the contract's own fractional DTE.
    frac_today=0.5
    try:
        if "timestamp" in x.columns:
            ts=pd.to_datetime(x["timestamp"],errors="coerce").dropna()
            if not ts.empty:
                t=ts.max()
                # option/equity close proxy 16:00 local market time; clamp prevents zero window
                frac_today=max((16.0-(t.hour+t.minute/60.0+t.second/3600.0))/24.0, 1.0/(24.0*60.0))
                frac_today=min(frac_today,1.0)
    except Exception as _e:
        _obs_note('expiry_window:242', _e)
    day_map={d:max(float((d-ref).days)+ (frac_today if d==ref else 0.5), 1.0/(24.0*60.0)) for d in selected}
    if mode==WINDOW_AUTO:
        weights=auto_expiry_weights(x,selected,ref)
        pairs=[(day_map[d],float(weights.get(d.isoformat(),0.0))) for d in selected]
        denom=sum(w for _,w in pairs)
        days=(sum(v*w for v,w in pairs)/denom) if denom>0 else float(np.median([day_map[d] for d in selected]))
        source="AUTO_WEIGHTED_SELECTED_EXPIRIES"
    else:
        days=float(np.median([day_map[d] for d in selected]))
        source="SELECTED_EXPIRIES_MEDIAN" if mode!=WINDOW_0DTE else "0DTE_REMAINING_TODAY"
    return {"ready":True,"days":float(max(days,1.0/(24.0*60.0))),"source":source,
            "expirations":[d.isoformat() for d in selected]}

def filter_events(events: pd.DataFrame, info: Dict[str, Any]) -> pd.DataFrame:
    if events is None or events.empty: return pd.DataFrame() if events is None else events.copy()
    allowed = set(info.get("expirations") or [])
    if not allowed: return events.iloc[0:0].copy()
    e=events.copy()
    col="expiration_date" if "expiration_date" in e.columns else "expiration" if "expiration" in e.columns else None
    if col is None:
        # If the event source lacks expiry metadata, keep it rather than pretending we know its horizon.
        return e
    vals=pd.to_datetime(e[col], errors="coerce").dt.date.astype("string")
    return e[vals.isin(allowed)].copy()


def expiry_confluence(df: pd.DataFrame, spot: float | None = None) -> Dict[str, Any]:
    """Multi-expiry relevance by individual expiry plus 0DTE/WEEK/2W confirmation.

    `Expiry Confluence 4/5` means the strike is relevant in four of five loaded expiry
    groups. `MULTI-EXPIRY CONFIRMATION` separately checks the 0DTE / week / 2-week
    horizons requested for intraday confirmation. Neither metric is a probability.
    """
    if df is None or df.empty:
        return {"status":"NONE","zones":[],"horizons":{},"expiry_groups":[]}
    from .engine import enrich_options, engine_config_for_asset
    x=ensure_expiration_date(df)
    ts=pd.to_datetime(x.get("timestamp"), errors="coerce")
    if ts.notna().any(): x=x[ts==ts.max()].copy()
    if x.empty:return {"status":"NONE","zones":[],"horizons":{},"expiry_groups":[]}
    try:
        sym = str(x["underlying_symbol"].dropna().iloc[-1]).upper() if "underlying_symbol" in x.columns and x["underlying_symbol"].notna().any() else "UNKNOWN"
        enr=enrich_options(x,engine_config_for_asset(sym))
    except Exception: return {"status":"NONE","zones":[],"horizons":{},"expiry_groups":[]}

    def score_strikes(sub: pd.DataFrame, top_n: int = 8):
        if sub.empty:return []
        sub=sub.copy();sub["_gex_abs"]=numeric_column(sub,"signed_gex_proxy",0).abs()
        sub["_oi"]=pd.to_numeric(sub.get("raw_open_interest",sub.get("open_interest",0)),errors="coerce").fillna(0)
        sub["_vol"]=pd.to_numeric(sub.get("raw_volume",sub.get("volume",0)),errors="coerce").fillna(0)
        a=sub.groupby("strike",as_index=False).agg(gex=("_gex_abs","sum"),oi=("_oi","sum"),vol=("_vol","sum"))
        a["voloi"]=a["vol"]/(a["oi"]+1)
        for c in ["gex","oi","vol","voloi"]:a[c+"n"]=_minmax(np.log1p(a[c].clip(lower=0)))
        a["score"]=100*(.42*a["gexn"]+.28*a["oin"]+.20*a["voln"]+.10*a["voloin"])
        if spot is not None and math.isfinite(float(spot)) and not a.empty:
            step=max(float(np.nanmedian(np.diff(np.sort(a["strike"].unique())))) if a["strike"].nunique()>1 else 1.0,0.25)
            a["score"] += 8*np.exp(-abs(a["strike"]-float(spot))/(3*step))
        a["score"]=a["score"].clip(0,100)
        relevant=a.sort_values("score",ascending=False).head(min(top_n,len(a)))
        relevant=relevant[relevant["score"]>=55]
        return [{"strike":float(r.strike),"score":float(r.score)} for r in relevant.itertuples()]

    # Individual expiration groups: this drives the intuitive "4/5" metric.
    expiry_groups=[]
    for exp,sub in enr.groupby("expiration_date"):
        expiry_groups.append({"expiration":str(exp),"relevant":score_strikes(sub,6)})
    total_exp=max(len(expiry_groups),1)

    # Horizon confirmation requested for intraday use.
    horizons={}
    # v1.16.2 · BUCKETS MUTUAMENTE EXCLUYENTES.
    # Antes se comparaban 0DTE, WEEK y 2W, pero 0DTE ⊂ WEEK ⊂ 2W por construcción, así que
    # CUALQUIER strike relevante hoy disparaba los tres horizontes. Medido sobre cadena
    # sintética con UN SOLO vencimiento, el resultado era "3/3 · FUERTE": el indicador no
    # podía dar otra cosa y por tanto no medía nada. Ahora los tres tramos son disjuntos y
    # una confirmación 3/3 significa que tres estructuras independientes coinciden.
    ref=reference_date(enr); b=_bounds(ref)
    exp_all=pd.to_datetime(enr.get("expiration_date"),errors="coerce").dt.date
    tramos={
        "0DTE":        exp_all==ref,
        "RESTO SEMANA":(exp_all>ref)&(exp_all<=b["friday"]),
        "SEMANA SIG.": (exp_all>b["friday"])&(exp_all<=b["next_friday"]),
    }
    for mode,mask in tramos.items():
        sub=enr[mask.fillna(False)]
        horizons[mode]={"expirations":sorted({str(x) for x in exp_all[mask.fillna(False)].dropna()}),
                        "relevant":score_strikes(sub,8) if not sub.empty else [],
                        "contracts":int(mask.fillna(False).sum())}

    strikes=sorted(set(round(z["strike"],6) for g in expiry_groups for z in g["relevant"]) | set(round(z["strike"],6) for h in horizons.values() for z in h["relevant"]))
    zones=[]
    for k in strikes:
        exp_hits=[];exp_scores=[]
        for g in expiry_groups:
            hit=next((z for z in g["relevant"] if abs(z["strike"]-k)<1e-6),None)
            if hit:exp_hits.append(g["expiration"]);exp_scores.append(hit["score"])
        horizon_hits=[];horizon_scores=[]
        for mode,h in horizons.items():
            hit=next((z for z in h["relevant"] if abs(z["strike"]-k)<1e-6),None)
            if hit:horizon_hits.append(mode);horizon_scores.append(hit["score"])
        ec=len(exp_hits);hc=len(horizon_hits)
        if not (ec or hc):continue
        # Solo cuentan los tramos que EXISTEN: si el activo no lista vencimientos la
        # semana que viene, 2/2 es confluencia completa y exigir 3/3 lo penalizaría por
        # una ausencia del calendario, no por debilidad estructural.
        active_h=sum(1 for h in horizons.values() if h.get("contracts",0)>0)
        full_h=active_h if active_h else 3
        strength=("FUERTE" if (hc>=full_h and full_h>=2) or ec>=3
                  else "MEDIA" if hc>=2 or ec>=2 else "DÉBIL")
        scores=exp_scores+horizon_scores
        zones.append({
            "strike":float(k),"count":ec,"of":total_exp,"label":f"Expiry Confluence {ec}/{total_exp}",
            "strength":strength,"expirations":exp_hits,"horizon_count":hc,"horizon_of":full_h,
            "horizons":horizon_hits,"multi_horizon":bool(hc>=full_h and full_h>=2),
            "score":float(np.mean(scores)) if scores else 0.0,
        })
    zones=sorted(zones,key=lambda z:(z["multi_horizon"],z["count"],z["horizon_count"],z["score"]),reverse=True)
    overall=zones[0]["strength"] if zones else "NONE"
    return {"status":overall,"zones":zones[:16],"horizons":horizons,"expiry_groups":expiry_groups,
            "note":"Expiry Confluence cuenta vencimientos individuales. Multi-Expiry Confirmation "
                   "compara tramos DISJUNTOS (0DTE · resto de semana · semana siguiente): un mismo "
                   "vencimiento ya no puede confirmarse tres veces a sí mismo. Scores internos, no probabilidades."}
