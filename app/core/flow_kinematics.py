"""Observed-flow kinematics for ITM QUANT.

This module deliberately separates option sensitivities (Delta/Gamma/Speed) from
*time derivatives of observed market flow*.  The names velocity/acceleration/jerk
are mathematical analogies only; they never claim to identify a participant or to
measure literal physical motion/inertia.

Authority: SHADOW_CONTEXT_ONLY.  Scanner direction is never changed here.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import math
import numpy as np
import pandas as pd
from .frame_guards import numeric_column

from .time_normalization import utc_ns


def _finite(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _robust_z(s: pd.Series, clip: float = 6.0) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce").astype(float)
    if x.notna().sum() < 3:
        return pd.Series(np.zeros(len(x)), index=x.index, dtype=float)
    med = float(x.median())
    mad = float((x - med).abs().median())
    scale = 1.4826 * mad
    if not math.isfinite(scale) or scale <= 1e-12:
        sd = float(x.std(ddof=0))
        scale = sd if math.isfinite(sd) and sd > 1e-12 else 1.0
    return ((x - med) / scale).clip(-clip, clip).fillna(0.0)


def _directional_premium(events: pd.DataFrame) -> pd.Series:
    if "directional_premium" in events.columns:
        return pd.to_numeric(events["directional_premium"], errors="coerce").fillna(0.0)
    premium = numeric_column(events,"premium",0.0)
    sign = numeric_column(events,"direction_sign",0.0)
    return premium * sign




def _directional_permutation_test(events: pd.DataFrame, *, permutations: int = 199, seed: int = 20260912) -> Dict[str, Any]:
    """Prueba SHADOW de estructura direccional contra un nulo de signos aleatorios.

    Conserva timestamps y magnitudes de premium; solo aleatoriza el signo. El
    estadístico es el desequilibrio absoluto de premium direccional en la ventana.
    No se usa para cambiar el estado CURRENT ni el Scanner.
    """
    if not isinstance(events, pd.DataFrame) or events.empty:
        return {"ready": False, "p_value": None, "significant": None, "observed_side": "NEUTRAL"}
    signed = pd.to_numeric(_directional_premium(events), errors="coerce").dropna().to_numpy(dtype=float)
    signed = signed[np.isfinite(signed)]
    if signed.size < 8:
        return {"ready": False, "p_value": None, "significant": None, "observed_side": "NEUTRAL",
                "reason": "INSUFFICIENT_EVENTS"}
    mag = np.abs(signed)
    gross = float(np.sum(mag))
    if not math.isfinite(gross) or gross <= 0:
        return {"ready": False, "p_value": None, "significant": None, "observed_side": "NEUTRAL",
                "reason": "ZERO_GROSS_PREMIUM"}
    observed_net = float(np.sum(signed))
    observed = abs(observed_net) / gross
    rng = np.random.default_rng(int(seed))
    nperm = int(max(49, permutations))
    # Rademacher null: sin preferencia direccional, mismas magnitudes y mismo reloj.
    null = np.empty(nperm, dtype=float)
    for i in range(nperm):
        signs = rng.choice(np.array([-1.0, 1.0]), size=mag.size, replace=True)
        null[i] = abs(float(np.sum(mag * signs))) / gross
    p_value = (1.0 + float(np.sum(null >= observed))) / (float(nperm) + 1.0)
    significant = bool(p_value <= 0.05)
    return {
        "ready": True,
        "method": "PERMUTATION_DIRECTIONAL_PREMIUM_NULL",
        "permutations": nperm,
        "statistic": round(observed, 6),
        "p_value": round(p_value, 6),
        "significant": significant,
        "observed_side": "BUY" if observed_net > 0 else "SELL" if observed_net < 0 else "NEUTRAL",
        "alpha": 0.05,
        "authority": "CANDIDATE_SHADOW_ONLY",
    }


def _price_response(price_history: Optional[pd.DataFrame], buckets: pd.Series) -> pd.Series:
    """Return causal price changes aligned to the flow buckets.

    Price is context only.  No interpolation through missing market observations is
    performed; merge_asof uses the last observed price at or before each bucket.
    """
    if not isinstance(price_history, pd.DataFrame) or price_history.empty:
        return pd.Series(np.nan, index=buckets.index, dtype=float)
    p = price_history.copy()
    p["timestamp"] = utc_ns(p.get("timestamp"))
    price_col = "price" if "price" in p.columns else "close" if "close" in p.columns else "underlying_price" if "underlying_price" in p.columns else None
    if price_col is None:
        return pd.Series(np.nan, index=buckets.index, dtype=float)
    p["_price"] = pd.to_numeric(p.get(price_col), errors="coerce")
    p = p.dropna(subset=["timestamp", "_price"]).sort_values("timestamp")[["timestamp", "_price"]]
    if p.empty:
        return pd.Series(np.nan, index=buckets.index, dtype=float)
    left = pd.DataFrame({"timestamp": utc_ns(pd.Series(buckets))}).sort_values("timestamp")
    m = pd.merge_asof(left, p, on="timestamp", direction="backward", tolerance=pd.Timedelta(minutes=5))
    out = pd.to_numeric(m["_price"], errors="coerce").diff()
    out.index = left.index
    return out.reindex(buckets.index)


def build_flow_kinematics(
    events: Optional[pd.DataFrame],
    price_history: Optional[pd.DataFrame] = None,
    *,
    symbol: str = "UNKNOWN",
    bucket_seconds: int = 5,
    lookback_minutes: int = 30,
    max_points: int = 720,
) -> Dict[str, Any]:
    """Build time-derivative diagnostics from observed directional option flow.

    Velocity is the bucketed directional premium rate (USD/s), equivalent to the
    derivative of cumulative signed premium. Acceleration and jerk are finite
    differences of that observed rate using event time.  Scores are robustly
    normalized per session/window so the engine is multi-asset without hard-coded
    dollar thresholds.
    """
    authority = "SHADOW_CONTEXT_ONLY"
    base = {
        "ready": False,
        "symbol": str(symbol or "UNKNOWN").upper(),
        "authority": authority,
        "semantics": "OBSERVED_FLOW_DYNAMICS_NOT_PHYSICAL_INSTITUTIONAL_INERTIA",
        "bucket_seconds": int(max(1, bucket_seconds)),
        "series": [],
    }
    if not isinstance(events, pd.DataFrame) or events.empty:
        return {**base, "status": "WAITING_FLOW", "reason": "NO_OBSERVED_FLOW_EVENTS"}

    e = events.copy()
    e["timestamp"] = pd.to_datetime(e.get("timestamp"), errors="coerce")
    e = e.dropna(subset=["timestamp"]).sort_values("timestamp")
    if e.empty:
        return {**base, "status": "WAITING_FLOW", "reason": "NO_VALID_EVENT_TIME"}
    e["directional_premium"] = _directional_premium(e)
    e["premium"] = pd.to_numeric(e.get("premium", e["directional_premium"].abs()), errors="coerce").fillna(0.0)
    end = e["timestamp"].max()
    if lookback_minutes > 0:
        e = e[e["timestamp"] >= end - pd.Timedelta(minutes=int(lookback_minutes))].copy()
    if e.empty:
        return {**base, "status": "WAITING_FLOW", "reason": "NO_EVENTS_IN_LOOKBACK"}

    bucket_seconds = int(max(1, min(int(bucket_seconds), 60)))
    rule = f"{bucket_seconds}s"
    e["bucket"] = e["timestamp"].dt.floor(rule)
    b = e.groupby("bucket", as_index=False).agg(
        net_flow_usd=("directional_premium", "sum"),
        gross_flow_usd=("premium", "sum"),
        event_count=("directional_premium", "size"),
    ).sort_values("bucket")
    if b.empty:
        return {**base, "status": "WAITING_FLOW", "reason": "NO_BUCKETS"}

    # Keep actual event-time spacing. Missing buckets are missing; we do not fill them
    # with fake zero-flow samples because that would manufacture acceleration/jerk.
    dt = b["bucket"].diff().dt.total_seconds()
    dt = dt.where(dt > 0, float(bucket_seconds))
    b["flow_velocity_usd_s"] = b["net_flow_usd"] / float(bucket_seconds)
    b["flow_acceleration_usd_s2"] = b["flow_velocity_usd_s"].diff() / dt
    b["flow_jerk_usd_s3"] = b["flow_acceleration_usd_s2"].diff() / dt
    b["cumulative_net_flow_usd"] = b["net_flow_usd"].cumsum()

    # Robust dimensionless diagnostics for comparing different symbols.
    zv = _robust_z(b["flow_velocity_usd_s"])
    za = _robust_z(b["flow_acceleration_usd_s2"])
    b["momentum_score"] = 100.0 * np.tanh((0.72 * zv + 0.28 * za) / 2.2)

    signs = np.sign(pd.to_numeric(b["net_flow_usd"], errors="coerce").fillna(0.0))
    recent_n = max(3, min(12, len(b)))
    recent = signs.tail(recent_n)
    latest_sign = int(np.sign(float(b["net_flow_usd"].tail(min(3, len(b))).sum())))
    aligned = (recent == latest_sign) if latest_sign else pd.Series(False, index=recent.index)
    persistence_pct = float(aligned.mean() * 100.0) if len(recent) and latest_sign else 0.0
    gross = pd.to_numeric(b["gross_flow_usd"], errors="coerce").fillna(0.0)
    gross_z = _robust_z(gross)
    b["impulse_score"] = 100.0 * np.tanh((0.60 * zv + 0.25 * za + 0.15 * gross_z) / 2.4)

    # Microstructure absorption/friction proxy: unusually large flow accompanied by a
    # small observed price response. It is explicitly not physical friction/dealer intent.
    price_delta = _price_response(price_history, b["bucket"])
    b["price_delta"] = price_delta.to_numpy() if len(price_delta) == len(b) else np.nan
    abs_move = pd.to_numeric(b["price_delta"], errors="coerce").abs()
    move_scale = float(abs_move[abs_move > 0].median()) if (abs_move > 0).any() else float("nan")
    if math.isfinite(move_scale) and move_scale > 0:
        flow_intensity = np.clip(np.abs(gross_z.to_numpy()) / 3.0, 0.0, 1.0)
        # Fail-closed: un bucket sin precio observado NO recibe una respuesta ficticia
        # de 0.5. Sin precio, la absorción de ese bucket queda desconocida (NaN).
        abs_arr = abs_move.to_numpy(dtype=float)
        response_small = np.where(
            np.isfinite(abs_arr),
            1.0 / (1.0 + (abs_arr / move_scale)),
            np.nan,
        )
        b["absorption_score"] = np.clip(100.0 * flow_intensity * response_small, 0.0, 100.0)
    else:
        b["absorption_score"] = np.nan

    lv = _finite(b["flow_velocity_usd_s"].iloc[-1])
    la = _finite(b["flow_acceleration_usd_s2"].iloc[-1])
    lj = _finite(b["flow_jerk_usd_s3"].iloc[-1])
    mom = _finite(b["momentum_score"].iloc[-1])
    imp = _finite(b["impulse_score"].iloc[-1])
    abs_score = b["absorption_score"].iloc[-1] if "absorption_score" in b else np.nan
    abs_score = None if not math.isfinite(_finite(abs_score, float("nan"))) else float(abs_score)
    price_coverage = float(pd.to_numeric(b["price_delta"], errors="coerce").notna().mean()) if len(b) else 0.0

    # CANDIDATE: prueba estadística separada del clasificador legacy/CURRENT.
    # Usa solo la ventana observada ya disponible y nunca cambia `state`.
    recent_start = b["bucket"].tail(min(12, len(b))).min()
    e_candidate = e[e["bucket"] >= recent_start].copy() if len(b) else e.iloc[0:0].copy()
    candidate = _directional_permutation_test(e_candidate)
    candidate["absorption_score"] = None if abs_score is None else round(abs_score, 2)
    candidate["absorption_price_coverage"] = round(price_coverage, 3)
    candidate["state"] = (
        "DIRECTIONAL_STRUCTURE_SIGNIFICANT" if candidate.get("significant") is True
        else "DIRECTIONAL_STRUCTURE_NOT_SIGNIFICANT" if candidate.get("significant") is False
        else "COLLECTING"
    )

    # Exhaustion is a state label, not a forecast. Require at least two derivatives.
    s_v, s_a, s_j = np.sign(lv), np.sign(la), np.sign(lj)
    if len(b) < 3:
        state = "COLLECTING_DERIVATIVES"
    elif s_v != 0 and s_a == s_v and s_j == s_v:
        state = "FLOW_ACCELERATING_BUY" if s_v > 0 else "FLOW_ACCELERATING_SELL"
    elif s_v != 0 and s_a == -s_v and s_j == -s_v:
        state = "POSSIBLE_EXHAUSTION_BUY" if s_v > 0 else "POSSIBLE_EXHAUSTION_SELL"
    elif abs_score is not None and abs_score >= 65:
        state = "HIGH_ABSORPTION_PROXY"
    else:
        state = "FLOW_MIXED_OR_STABLE"

    keep = b.tail(max(20, int(max_points))).copy()
    series = []
    for r in keep.to_dict("records"):
        def n(v):
            try:
                x=float(v); return x if math.isfinite(x) else None
            except Exception:return None
        series.append({
            "timestamp": pd.Timestamp(r["bucket"]).isoformat(),
            "net_flow_usd": n(r.get("net_flow_usd")),
            "gross_flow_usd": n(r.get("gross_flow_usd")),
            "cumulative_net_flow_usd": n(r.get("cumulative_net_flow_usd")),
            "flow_velocity_usd_s": n(r.get("flow_velocity_usd_s")),
            "flow_acceleration_usd_s2": n(r.get("flow_acceleration_usd_s2")),
            "flow_jerk_usd_s3": n(r.get("flow_jerk_usd_s3")),
            "momentum_score": n(r.get("momentum_score")),
            "impulse_score": n(r.get("impulse_score")),
            "absorption_score": n(r.get("absorption_score")),
            "price_delta": n(r.get("price_delta")),
            "event_count": int(r.get("event_count") or 0),
        })

    return {
        **base,
        "ready": True,
        "status": state,
        "observed_events": int(len(e)),
        "observed_buckets": int(len(b)),
        "lookback_minutes": int(lookback_minutes),
        "candidate": candidate,
        "latest": {
            "flow_velocity_usd_s": lv,
            "flow_acceleration_usd_s2": la,
            "flow_jerk_usd_s3": lj,
            "persistence_pct": round(persistence_pct, 1),
            "momentum_score": round(mom, 2),
            "impulse_score": round(imp, 2),
            "absorption_score": None if abs_score is None else round(abs_score, 2),
            "direction": "BUY" if lv > 0 else "SELL" if lv < 0 else "NEUTRAL",
            "state": state,
        },
        "series": series,
        "units": {
            "velocity": "USD_DIRECTIONAL_PREMIUM_PER_SECOND",
            "acceleration": "USD_DIRECTIONAL_PREMIUM_PER_SECOND_SQUARED",
            "jerk": "USD_DIRECTIONAL_PREMIUM_PER_SECOND_CUBED",
            "scores": "ROBUST_DIMENSIONLESS_-100_TO_100_EXCEPT_ABSORPTION_0_TO_100",
        },
        "disclosure": "Velocity/acceleration/jerk are time derivatives of observed signed option premium, not Delta/Gamma/Speed and not literal institutional inertia. Missing time buckets are not fabricated. Candidate permutation significance is SHADOW and does not replace CURRENT.",
    }
