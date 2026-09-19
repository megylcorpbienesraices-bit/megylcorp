"""Native options-structure analytics for ITM QUANT.

This module is the provider-independent replacement for legacy third-party
structural Gamma levels.  It consumes the already-normalized/enriched option
chain produced by :mod:`app.core.engine` and exposes transparent ITM QUANT
levels only.  No provider-specific proprietary state is reproduced or claimed.
"""
from __future__ import annotations

from typing import Any, Dict
import math
import numpy as np
import pandas as pd


def _finite(value: Any) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _latest_enriched(gd: Dict[str, Any] | None) -> pd.DataFrame:
    if not isinstance(gd, dict):
        return pd.DataFrame()
    frame = gd.get("enriched")
    if not isinstance(frame, pd.DataFrame) or frame.empty or "timestamp" not in frame.columns:
        return pd.DataFrame()
    out = frame.copy()
    ts = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.loc[ts.notna()].copy()
    if out.empty:
        return out
    out["timestamp"] = ts.loc[out.index]
    latest = out["timestamp"].max()
    return out[out["timestamp"] == latest].copy()


def build_native_options_structure(gd: Dict[str, Any] | None, *, top_n: int = 8) -> Dict[str, Any]:
    """Return native ITM QUANT structural levels from an analyzed chain.

    Definitions are deliberately explicit:
    * OI GEX uses ``signed_gex_proxy`` from the engine.
    * Volume GEX uses the same Gamma/sign/multiplier/spot convention, replacing
      open interest with observed option volume.
    * Zero Gamma is the engine's dynamic full-chain repricing root.  When there
      is no sign crossing, the diagnostic nearest-zero point is disclosed as a
      diagnostic and is not promoted to a true flip.
    """
    snap = _latest_enriched(gd)
    if snap.empty:
        return {
            "ready": False,
            "status": "NO_CHAIN",
            "authority": "ITM_QUANT_NATIVE_OPTIONS_STRUCTURE",
            "provider_dependency": "NONE",
        }

    required = {"strike", "option_type", "calc_gamma", "open_interest", "volume", "underlying_price", "signed_gex_proxy"}
    if not required.issubset(snap.columns):
        return {
            "ready": False,
            "status": "INCOMPLETE_CHAIN",
            "missing": sorted(required - set(snap.columns)),
            "authority": "ITM_QUANT_NATIVE_OPTIONS_STRUCTURE",
            "provider_dependency": "NONE",
        }

    spot = _finite(pd.to_numeric(snap["underlying_price"], errors="coerce").dropna().iloc[-1])
    if spot is None:
        return {"ready": False, "status": "NO_SPOT", "authority": "ITM_QUANT_NATIVE_OPTIONS_STRUCTURE", "provider_dependency": "NONE"}

    opt_type = snap["option_type"].astype(str).str.lower()
    sign = np.where(opt_type.str.startswith("c"), 1.0, -1.0)
    gamma = pd.to_numeric(snap["calc_gamma"], errors="coerce").fillna(0.0).to_numpy(float)
    volume = pd.to_numeric(snap["volume"], errors="coerce").fillna(0.0).clip(lower=0.0).to_numpy(float)
    if "contract_multiplier" in snap.columns:
        mult = pd.to_numeric(snap["contract_multiplier"], errors="coerce").fillna(100.0).to_numpy(float)
        mult = np.where(np.isfinite(mult) & (mult > 0), mult, 100.0)
    else:
        mult = np.full(len(snap), 100.0, dtype=float)

    snap = snap.copy()
    snap["signed_volume_gex_proxy"] = sign * gamma * volume * mult * (spot ** 2) * 0.01

    by_strike = snap.groupby("strike", as_index=False).agg(
        signed_gex_oi=("signed_gex_proxy", "sum"),
        gross_gex_oi=("signed_gex_proxy", lambda x: float(np.abs(pd.to_numeric(x, errors="coerce").fillna(0.0)).sum())),
        signed_gex_volume=("signed_volume_gex_proxy", "sum"),
        gross_gex_volume=("signed_volume_gex_proxy", lambda x: float(np.abs(pd.to_numeric(x, errors="coerce").fillna(0.0)).sum())),
        open_interest=("open_interest", "sum"),
        option_volume=("volume", "sum"),
    ).sort_values("strike").reset_index(drop=True)

    def _extreme(column: str, which: str) -> float | None:
        vals = pd.to_numeric(by_strike[column], errors="coerce")
        if not vals.notna().any():
            return None
        idx = vals.idxmax() if which == "max" else vals.idxmin()
        return _finite(by_strike.loc[idx, "strike"])

    oi_ranked = by_strike.assign(_abs=by_strike["signed_gex_oi"].abs()).sort_values("_abs", ascending=False).head(max(1, int(top_n)))
    vol_ranked = by_strike.assign(_abs=by_strike["signed_gex_volume"].abs()).sort_values("_abs", ascending=False).head(max(1, int(top_n)))

    flip = _finite((gd or {}).get("gamma_flip"))
    crossing = bool((gd or {}).get("gamma_flip_crossing", False))
    method = str((gd or {}).get("gamma_flip_method") or "UNKNOWN")

    return {
        "ready": True,
        "status": "OK",
        "authority": "ITM_QUANT_NATIVE_OPTIONS_STRUCTURE",
        "provider_dependency": "NONE",
        "methodology": "TRANSPARENT_CHAIN_MATH",
        "spot": spot,
        "timestamp": str(snap["timestamp"].max()),
        "zero_gamma": flip if crossing else None,
        "gamma_flip": flip,
        "gamma_flip_crossing": crossing,
        "gamma_flip_method": method,
        "gamma_flip_diagnostic_only": not crossing,
        "major_pos_oi": _extreme("signed_gex_oi", "max"),
        "major_neg_oi": _extreme("signed_gex_oi", "min"),
        "major_pos_vol": _extreme("signed_gex_volume", "max"),
        "major_neg_vol": _extreme("signed_gex_volume", "min"),
        "net_gex_oi": _finite(by_strike["signed_gex_oi"].sum()),
        "gross_gex_oi": _finite(by_strike["gross_gex_oi"].sum()),
        "net_gex_volume": _finite(by_strike["signed_gex_volume"].sum()),
        "gross_gex_volume": _finite(by_strike["gross_gex_volume"].sum()),
        "gamma_center": _finite((gd or {}).get("gamma_center")),
        "gamma_migration_direction": (gd or {}).get("migration_direction"),
        "gamma_migration_strength": _finite((gd or {}).get("migration_strength")),
        "gamma_pressure_direction": (gd or {}).get("pressure_direction"),
        "gamma_pressure_score": _finite((gd or {}).get("pressure_score")),
        "delta_pressure_direction": (gd or {}).get("delta_pressure_direction"),
        "delta_pressure_score": _finite((gd or {}).get("delta_pressure_score")),
        "top_oi_gex_strikes": [
            {"strike": _finite(r.strike), "net": _finite(r.signed_gex_oi), "gross": _finite(r.gross_gex_oi), "open_interest": _finite(r.open_interest)}
            for r in oi_ranked.itertuples(index=False)
        ],
        "top_volume_gex_strikes": [
            {"strike": _finite(r.strike), "net": _finite(r.signed_gex_volume), "gross": _finite(r.gross_gex_volume), "option_volume": _finite(r.option_volume)}
            for r in vol_ranked.itertuples(index=False)
        ],
        "disclosure": "Calls positive / puts negative is an ITM QUANT structural exposure proxy, not observed dealer inventory.",
    }
