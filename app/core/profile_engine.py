"""Universal per-strike Profile Engine for Gamma/GEX/Delta/DEX/Vanna/Charm/OI/Volume.

The engine standardizes presentation/state shape, not the mathematics. Each metric keeps
its own explicit definition and units. No silent Gamma fallback is permitted.
"""

from __future__ import annotations

from typing import Any
import math
import numpy as np
import pandas as pd
from .frame_guards import numeric_column
from .contract_spec import multiplier_series


def _series(df: pd.DataFrame, name: str, default: float = 0.0) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype=float)


def _metric_values(df: pd.DataFrame, metric: str) -> tuple[pd.Series, str, str, bool]:
    m = str(metric or "Gamma").strip().lower()
    oi = _series(df, "open_interest")
    vol = _series(df, "volume")
    mult = multiplier_series(df)
    spot = _series(df, "underlying_price", 1.0).replace(0, 1.0)
    call_sign = pd.Series(np.where(df.get("option_type", pd.Series("", index=df.index)).astype(str).str.lower().str.startswith("c"), 1.0, -1.0), index=df.index)
    if m in {"gamma", "gex"}:
        return _series(df, "signed_gex_proxy"), "M$", "SIGNED_GEX_PROXY", True
    if m in {"delta", "dex"}:
        return _series(df, "option_delta_exposure_info"), "M$", "OPTION_DELTA_EXPOSURE", True
    if m == "vanna":
        return _series(df, "calc_vanna") * oi * mult * spot, "$-proxy", "ITM_MODEL_VANNA_X_OI_X_MULT_X_SPOT", True
    if m == "charm":
        return _series(df, "calc_charm") * oi * mult * spot, "$-proxy/day", "ITM_MODEL_CHARM_X_OI_X_MULT_X_SPOT", True
    if m in {"oi", "open interest", "open_interest"}:
        return oi, "contracts", "OBSERVED_OPEN_INTEREST", False
    if m in {"net oi", "net_oi"}:
        return call_sign * oi, "contracts", "CALL_OI_MINUS_PUT_OI", True
    if m in {"volume", "volumen"}:
        return vol, "contracts", "OBSERVED_VOLUME", False
    if m in {"net volume", "net_volume", "volumen neto"}:
        return call_sign * vol, "contracts", "CALL_VOLUME_MINUS_PUT_VOLUME", True
    raise KeyError(f"unsupported profile metric: {metric}")


def build_profile(snapshot: pd.DataFrame, metric: str = "Gamma", *, view: str = "Net", spot: float | None = None) -> dict[str, Any]:
    out = {"ready": False, "metric": str(metric), "view": str(view).upper(), "rows": [], "reason": None}
    if snapshot is None or snapshot.empty or "strike" not in snapshot.columns:
        out["reason"] = "NO_CHAIN"; return out
    x = snapshot.copy()
    if "timestamp" in x.columns:
        ts = pd.to_datetime(x["timestamp"], errors="coerce")
        if ts.notna().any(): x = x[ts == ts.max()].copy()
    v = str(view or "Net").strip().lower()
    if v.startswith("call") and "option_type" in x.columns:
        x = x[x["option_type"].astype(str).str.lower().str.startswith("c")]
    elif v.startswith("put") and "option_type" in x.columns:
        x = x[x["option_type"].astype(str).str.lower().str.startswith("p")]
    if x.empty:
        out["reason"] = "NO_CONTRACTS_FOR_VIEW"; return out
    try:
        values, unit, definition, signed = _metric_values(x, metric)
    except KeyError as exc:
        out["reason"] = str(exc); return out
    x["_value"] = values; x["strike"] = pd.to_numeric(x["strike"], errors="coerce"); x = x.dropna(subset=["strike"])
    if x.empty:
        out["reason"] = "NO_VALID_STRIKES"; return out
    g = x.groupby("strike", as_index=False)["_value"].sum().sort_values("strike")
    arr = g["_value"].to_numpy(float)
    scale = float(np.nanmax(np.abs(arr))) if len(arr) else 0.0
    scale = scale if math.isfinite(scale) and scale > 0 else 1.0
    divisor = 1e6 if str(unit).startswith("M$") else 1.0
    rows = []
    for strike, raw_value in zip(g["strike"].to_numpy(float), g["_value"].to_numpy(float)):
        raw = float(raw_value); display = raw / divisor
        rows.append({"strike": float(strike), "value": display, "raw_value": raw,
                     "sign": 1 if raw > 0 else -1 if raw < 0 else 0,
                     "intensity": min(1.0, abs(raw) / scale)})
    if spot is None:
        try: spot = float(numeric_column(x,"underlying_price",float("nan")).dropna().iloc[-1])
        except Exception: spot = None
    return {**out, "ready": True, "rows": rows, "spot": spot, "unit": unit,
            "signed": signed, "definition": definition,
            "render_contract": "STRIKE_VALUE_SIGN_INTENSITY", "authority": "PROFILE_PRESENTATION_AND_STRUCTURE"}


def build_profile_bundle(snapshot: pd.DataFrame, *, view: str = "Net", spot: float | None = None) -> dict[str, Any]:
    metrics = ["Gamma", "Delta", "Vanna", "Charm", "OI", "Net OI", "Volume", "Net Volume"]
    return {"ready": bool(snapshot is not None and not snapshot.empty), "view": str(view).upper(),
            "profiles": {m: build_profile(snapshot, m, view=view, spot=spot) for m in metrics},
            "single_compute_contract": "ONE_CHAIN_STATE_MULTIPLE_PROFILE_CONSUMERS",
            "authority": "PRESENTATION_STRUCTURE_ONLY"}
