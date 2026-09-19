"""Observed option microstructure helpers for ITM QUANT.

The functions in this module are deliberately conservative.  They classify what
can be observed (trade price, synchronized NBBO, venue, size, timing) and label
anything that cannot be known from OPRA as a *candidate* or *estimate*.

No function here identifies a market-maker, customer capacity, or true opening /
closing status.  Those require private/clearing data that standard OPRA does not
provide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
import math
import numpy as np
import pandas as pd
from .frame_guards import numeric_column

from . import timeunits

from .time_normalization import utc_ns


def _f(v: Any, default: float = float("nan")) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return float(np.clip(float(v), lo, hi))


@dataclass(frozen=True)
class AggressorResult:
    aggressor: str
    confidence: float
    method: str
    spread_position: Optional[float]
    quote_age_ms: Optional[float]
    nbbo_synced: bool
    quote_quality: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "aggressor": self.aggressor,
            "aggressor_confidence": round(self.confidence, 4),
            "classification_method": self.method,
            "spread_position": None if self.spread_position is None else round(self.spread_position, 5),
            "quote_age_ms": None if self.quote_age_ms is None else round(self.quote_age_ms, 3),
            "nbbo_synced": bool(self.nbbo_synced),
            "quote_quality": self.quote_quality,
        }


def classify_option_trade(
    price: Any,
    bid: Any,
    ask: Any,
    *,
    quote_age_ms: Any = None,
    prev_trade_price: Any = None,
    prev_trade_sign: int = 0,
    max_quote_age_ms: float = 1500.0,
) -> AggressorResult:
    """Classify an option print using a *causal* quote, then tick-rule fallback.

    ``quote_age_ms`` must refer to a quote whose event time is <= trade event time.
    Callers that only possess a non-synchronized snapshot should pass ``None`` and
    cap the result separately; this function will never call that quote synchronized.
    """
    p, b, a = _f(price), _f(bid), _f(ask)
    age = _f(quote_age_ms)
    valid_quote = (
        math.isfinite(p) and math.isfinite(b) and math.isfinite(a)
        and b > 0 and a >= b and math.isfinite(age) and age >= 0 and age <= float(max_quote_age_ms)
    )
    if valid_quote:
        spread = max(a - b, 1e-12)
        pos = _clip((p - b) / spread, -0.5, 1.5)
        if p >= a - 0.10 * spread:
            return AggressorResult("BUY", 0.98 if age <= 500 else 0.90, "CAUSAL_NBBO_ASK", pos, age, True, "HIGH" if age <= 500 else "GOOD")
        if p <= b + 0.10 * spread:
            return AggressorResult("SELL", 0.98 if age <= 500 else 0.90, "CAUSAL_NBBO_BID", pos, age, True, "HIGH" if age <= 500 else "GOOD")
        # Lee-Ready midpoint classification.  Confidence decays near the midpoint.
        dist = abs(pos - 0.5) * 2.0
        if pos > 0.5 + 1e-9:
            return AggressorResult("BUY", 0.48 + 0.30 * dist, "CAUSAL_NBBO_MID", pos, age, True, "GOOD")
        if pos < 0.5 - 1e-9:
            return AggressorResult("SELL", 0.48 + 0.30 * dist, "CAUSAL_NBBO_MID", pos, age, True, "GOOD")

    prev = _f(prev_trade_price)
    if math.isfinite(p) and math.isfinite(prev):
        if p > prev:
            return AggressorResult("BUY", 0.40, "TICK_RULE", None, None, False, "FALLBACK")
        if p < prev:
            return AggressorResult("SELL", 0.40, "TICK_RULE", None, None, False, "FALLBACK")
        if int(prev_trade_sign or 0) > 0:
            return AggressorResult("BUY", 0.30, "TICK_RULE_ZERO", None, None, False, "FALLBACK")
        if int(prev_trade_sign or 0) < 0:
            return AggressorResult("SELL", 0.30, "TICK_RULE_ZERO", None, None, False, "FALLBACK")
    return AggressorResult("UNKNOWN", 0.10, "UNCLASSIFIED", None, None, False, "INSUFFICIENT")



def attach_causal_underlying(events: Optional[pd.DataFrame], ticks: Optional[pd.DataFrame], max_age_ms: float = 5000.0) -> pd.DataFrame:
    """Attach the last underlying trade at/before each option print.

    The option-chain snapshot's underlying price is preserved as a fallback, but the
    synthetic dealer inventory prefers a causal SIP/market trade when one is available.
    No future underlying tick is permitted to leak into an option event.
    """
    if not isinstance(events, pd.DataFrame) or events.empty:
        return events.copy() if isinstance(events, pd.DataFrame) else pd.DataFrame()
    e=events.copy();e["timestamp"]=utc_ns(e.get("timestamp"));e=e.dropna(subset=["timestamp"]).sort_values("timestamp")
    if e.empty:return e
    e["underlying_price_snapshot"]=pd.to_numeric(e.get("underlying_price",pd.Series(np.nan,index=e.index)),errors="coerce")
    if not isinstance(ticks,pd.DataFrame) or ticks.empty:
        e["underlying_price_source"]="OPTION_SNAPSHOT";e["underlying_sync"]=False;e["underlying_tick_age_ms"]=np.nan
        return e.reset_index(drop=True)
    t=ticks.copy();t["timestamp"]=utc_ns(t.get("timestamp"));t["_underlying_trade_price"]=numeric_column(t,"price",float("nan"));t=t.dropna(subset=["timestamp","_underlying_trade_price"]).sort_values("timestamp")
    if t.empty:
        e["underlying_price_source"]="OPTION_SNAPSHOT";e["underlying_sync"]=False;e["underlying_tick_age_ms"]=np.nan
        return e.reset_index(drop=True)
    right=t[["timestamp","_underlying_trade_price"]].rename(columns={"timestamp":"_underlying_tick_time"})
    # merge_asof requires the same key name; retain a duplicate for age/provenance.
    right["timestamp"]=right["_underlying_tick_time"]
    m=pd.merge_asof(e.sort_values("timestamp"),right.sort_values("timestamp"),on="timestamp",direction="backward",tolerance=pd.Timedelta(milliseconds=float(max_age_ms)))
    age=(m["timestamp"]-m["_underlying_tick_time"]).dt.total_seconds()*1000.0
    ok=m["_underlying_trade_price"].notna() & age.ge(0) & age.le(float(max_age_ms))
    m["underlying_price"]=m["underlying_price_snapshot"]
    m.loc[ok,"underlying_price"]=m.loc[ok,"_underlying_trade_price"]
    m["underlying_price_source"]=np.where(ok,"SIP_CAUSAL_TRADE","OPTION_SNAPSHOT")
    m["underlying_sync"]=ok.astype(bool);m["underlying_tick_age_ms"]=age.where(ok)
    return m.drop(columns=["_underlying_trade_price","_underlying_tick_time"],errors="ignore").reset_index(drop=True)

def _robust_z(values: pd.Series) -> pd.Series:
    x = pd.to_numeric(values, errors="coerce")
    med = float(x.median()) if x.notna().any() else 0.0
    mad = float((x - med).abs().median()) if x.notna().any() else 0.0
    scale = 1.4826 * mad
    if not math.isfinite(scale) or scale <= 1e-12:
        std = float(x.std(ddof=0)) if x.notna().sum() > 1 else 0.0
        scale = std if math.isfinite(std) and std > 1e-12 else 1.0
    return (x - med) / scale


def enrich_option_packages(events: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Add conservative BLOCK/SWEEP/MULTI-LEG candidate labels.

    OPRA does not expose universal package IDs/customer capacity to this application.
    Therefore these are *candidate* labels derived from event-time proximity, contract,
    venue and size symmetry. They are never treated as confirmed strategy identity.
    """
    if not isinstance(events, pd.DataFrame) or events.empty:
        return events.copy() if isinstance(events, pd.DataFrame) else pd.DataFrame()
    x = events.copy()
    x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
    x = x.dropna(subset=["timestamp"]).sort_values(["timestamp", "seq"] if "seq" in x.columns else ["timestamp"]).reset_index(drop=True)
    if x.empty:
        return x
    x["contracts"] = numeric_column(x,"contracts",0.0).clip(lower=0.0)
    x["premium"] = numeric_column(x,"premium",0.0).clip(lower=0.0)
    x["_size_z"] = _robust_z(x["contracts"])
    x["_premium_z"] = _robust_z(x["premium"])
    x["package_id"] = [f"SINGLE-{i}" for i in range(len(x))]
    x["package_type"] = "SINGLE"
    x["package_confidence"] = 0.0
    x["package_leg_count"] = 1
    x["package_venue_count"] = 1

    # BLOCK candidate: robust tail on either contracts or notional premium.  The
    # absolute floors prevent a tiny thin-sample print becoming a 'block'.
    block = ((x["_size_z"] >= 6.0) & (x["contracts"] >= 50)) | ((x["_premium_z"] >= 6.0) & (x["premium"] >= 100_000))
    x.loc[block, "package_type"] = "BLOCK_CANDIDATE"
    x.loc[block, "package_confidence"] = np.clip(0.60 + 0.04 * np.maximum(x.loc[block, "_size_z"], x.loc[block, "_premium_z"]), 0.0, 0.92)

    # SWEEP candidate: same contract + aggressor, short event-time burst over >=2 venues.
    # v1.27.3 BUGFIX: `astype("int64")` devuelve la unidad de almacenamiento, que
    # en pandas 3.0 es MICROsegundos, no nanosegundos. El `// 1_000_000` producía
    # SEGUNDOS, así que este bucket de "250 ms" abarcaba en realidad 250 s (4.2 min)
    # y agrupaba flujo sin relación como un solo sweep entre venues.
    x["_bucket250"] = timeunits.bucket(x["timestamp"], 250)
    group_cols = [c for c in ["underlying_symbol", "contract_symbol", "aggressor", "_bucket250"] if c in x.columns]
    if "contract_symbol" in group_cols and "_bucket250" in group_cols:
        for _, g in x.groupby(group_cols, dropna=False, sort=False):
            venues = g.get("exchange", pd.Series(index=g.index, dtype=str)).astype(str).replace("", np.nan).dropna().nunique()
            if len(g) >= 2 and venues >= 2 and str(g.get("aggressor", pd.Series(["UNKNOWN"])).iloc[0]).upper() in {"BUY", "SELL"}:
                conf = min(0.94, 0.62 + 0.06 * min(len(g), 4) + 0.05 * min(venues, 3))
                pid = f"SWEEP-{int(g['_bucket250'].iloc[0])}-{str(g.get('contract_symbol', pd.Series([''])).iloc[0])}"
                idx = g.index
                # MULTI_LEG below has priority; for now mark sweep.
                x.loc[idx, ["package_id", "package_type", "package_confidence", "package_leg_count", "package_venue_count"]] = [pid, "SWEEP_CANDIDATE", conf, len(g), venues]

    # MULTI-LEG candidate: distinct contracts within 75ms, same underlying, with
    # approximately symmetric size or explicit opposite aggressor legs. This catches
    # common vertical/calendar combinations without claiming strategy identity.
    # v1.27.3 BUGFIX: mismo problema de unidad; "75 ms" valía 75 s (1.25 min).
    x["_bucket75"] = timeunits.bucket(x["timestamp"], 75)
    multi_cols = [c for c in ["underlying_symbol", "_bucket75"] if c in x.columns]
    if "_bucket75" in multi_cols:
        for _, g in x.groupby(multi_cols, dropna=False, sort=False):
            if len(g) < 2 or "contract_symbol" not in g.columns or g["contract_symbol"].astype(str).nunique() < 2:
                continue
            sizes = g["contracts"].to_numpy(float)
            positive = sizes[sizes > 0]
            if len(positive) < 2:
                continue
            size_ratio = float(np.min(positive) / max(np.max(positive), 1e-9))
            ags = set(g.get("aggressor", pd.Series(index=g.index, dtype=str)).astype(str).str.upper())
            same_exp = g.get("expiration_date", pd.Series(index=g.index, dtype=str)).astype(str).nunique() == 1
            candidate = size_ratio >= 0.55 or ({"BUY", "SELL"}.issubset(ags) and size_ratio >= 0.35)
            if not candidate:
                continue
            conf = min(0.90, 0.50 + 0.25 * size_ratio + (0.08 if same_exp else 0.03) + 0.02 * min(len(g), 4))
            pid = f"MLEG-{int(g['_bucket75'].iloc[0])}-{g.index.min()}"
            venues = g.get("exchange", pd.Series(index=g.index, dtype=str)).astype(str).replace("", np.nan).dropna().nunique()
            x.loc[g.index, ["package_id", "package_type", "package_confidence", "package_leg_count", "package_venue_count"]] = [pid, "MULTI_LEG_CANDIDATE", conf, len(g), max(1, venues)]

    return x.drop(columns=[c for c in ["_size_z", "_premium_z", "_bucket250", "_bucket75"] if c in x.columns])


def microstructure_quality(events: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if not isinstance(events, pd.DataFrame) or events.empty:
        return {
            "state": "WAITING", "score": 0.0, "quote_sync_coverage_pct": 0.0,
            "high_conf_aggressor_pct": 0.0, "greeks_coverage_pct": 0.0, "oi_coverage_pct": 0.0,
            "underlying_sync_coverage_pct": 0.0, "package_candidate_pct": 0.0, "note": "Sin prints OPRA suficientes para medir calidad microestructural.",
        }
    x = events.copy(); n = max(len(x), 1)
    def col(name: str, default: Any = np.nan) -> pd.Series:
        return x[name] if name in x.columns else pd.Series(default, index=x.index)
    sync = col("nbbo_synced", False).fillna(False).astype(bool)
    conf = pd.to_numeric(col("aggressor_confidence", 0.0), errors="coerce").fillna(0.0)
    d = pd.to_numeric(col("provider_delta"), errors="coerce")
    g = pd.to_numeric(col("provider_gamma"), errors="coerce")
    # Model/fallback Greeks are still usable for the synthetic inventory, but their
    # provenance remains model-derived rather than observed provider fields.
    d = d.fillna(pd.to_numeric(col("model_delta"), errors="coerce")).fillna(pd.to_numeric(col("fallback_delta"), errors="coerce"))
    g = g.fillna(pd.to_numeric(col("model_gamma"), errors="coerce")).fillna(pd.to_numeric(col("fallback_gamma"), errors="coerce"))
    greek = d.notna() & g.notna()
    oi = pd.to_numeric(col("open_interest", 0.0), errors="coerce").fillna(0.0) > 0
    package = col("package_type", "SINGLE").astype(str).ne("SINGLE")
    underlying_sync = col("underlying_sync", False).fillna(False).astype(bool)
    quote_pct = 100.0 * float(sync.mean())
    high_pct = 100.0 * float((conf >= 0.75).mean())
    greek_pct = 100.0 * float(greek.mean())
    oi_pct = 100.0 * float(oi.mean())
    underlying_pct = 100.0 * float(underlying_sync.mean())
    # Package detection is diagnostic coverage, not required for every print.  We only
    # give a small bonus when candidate packages exist, never punish a quiet session.
    package_pct = 100.0 * float(package.mean())
    score = 0.34 * quote_pct + 0.24 * high_pct + 0.18 * greek_pct + 0.14 * oi_pct + 0.10 * underlying_pct
    score = float(np.clip(score + min(package_pct, 10.0) * 0.25, 0.0, 100.0))
    state = "HIGH" if score >= 80 else "GOOD" if score >= 65 else "DEGRADED" if score >= 40 else "LOW"
    return {
        "state": state, "score": round(score, 1),
        "quote_sync_coverage_pct": round(quote_pct, 1),
        "high_conf_aggressor_pct": round(high_pct, 1),
        "greeks_coverage_pct": round(greek_pct, 1),
        "oi_coverage_pct": round(oi_pct, 1),
        "underlying_sync_coverage_pct": round(underlying_pct, 1),
        "package_candidate_pct": round(package_pct, 1),
        "events": int(n),
        "note": "Quality score measures observability/classification quality; it is not trade probability.",
    }


def package_summary(events: Optional[pd.DataFrame], lookback_minutes: int = 15) -> Dict[str, Any]:
    if not isinstance(events, pd.DataFrame) or events.empty:
        return {"state": "WAITING", "counts": {}, "top": []}
    x = enrich_option_packages(events)
    if x.empty:
        return {"state": "WAITING", "counts": {}, "top": []}
    end = pd.to_datetime(x["timestamp"], errors="coerce").max()
    if pd.notna(end):
        x = x[pd.to_datetime(x["timestamp"], errors="coerce") >= end - pd.Timedelta(minutes=int(lookback_minutes))]
    counts = x["package_type"].value_counts().to_dict()
    grp = x.groupby(["package_id", "package_type"], as_index=False).agg(
        timestamp=("timestamp", "min"), contracts=("contracts", "sum"), premium=("premium", "sum"),
        legs=("contract_symbol", "nunique"), confidence=("package_confidence", "max")
    )
    grp = grp.sort_values(["premium", "contracts"], ascending=False).head(8)
    return {
        "state": "ACTIVE", "counts": {str(k): int(v) for k, v in counts.items()},
        "top": [
            {"package_id": str(r.package_id), "type": str(r.package_type), "timestamp": pd.Timestamp(r.timestamp).isoformat(),
             "contracts": round(float(r.contracts), 1), "premium": round(float(r.premium), 2),
             "legs": int(r.legs), "confidence": round(float(r.confidence) * 100.0, 1)}
            for r in grp.itertuples()
        ],
        "note": "BLOCK/SWEEP/MULTI-LEG are candidates inferred from OPRA timing/venue/size, not exchange-confirmed strategy IDs.",
    }
