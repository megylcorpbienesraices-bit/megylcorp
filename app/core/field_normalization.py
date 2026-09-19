"""Robust cross-asset normalization helpers for ITM QUANT state fields."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List
import math
import numpy as np


def _finite(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def robust_z(values: Iterable[Any], clip: float = 4.0) -> List[float]:
    arr = np.asarray([_finite(v, np.nan) for v in values], dtype=float)
    if arr.size == 0:
        return []
    finite = np.isfinite(arr)
    if not finite.any():
        return [0.0] * len(arr)
    med = float(np.nanmedian(arr))
    mad = float(np.nanmedian(np.abs(arr - med)))
    # 1.4826 converts MAD to a Gaussian-consistent sigma estimate.
    scale = max(1.4826 * mad, float(np.nanstd(arr)), 1e-9)
    out = np.zeros_like(arr)
    out[finite] = np.clip((arr[finite] - med) / scale, -abs(float(clip)), abs(float(clip)))
    return [float(x) for x in out]


def sigma_scaled_return(change_pct: Any, sigma_pct: Any = None, fallback_scale_pct: float = 1.0) -> float:
    ch = _finite(change_pct, 0.0) or 0.0
    sig = abs(_finite(sigma_pct, fallback_scale_pct) or fallback_scale_pct)
    sig = max(sig, 0.05)
    return float(np.clip(ch / sig, -4.0, 4.0))


def normalize_related_markets(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize heterogeneous related-market returns to a comparable field.

    Preferred scale is each market's own supplied 1-sigma/realized-vol percentage.
    When that is absent, a robust cross-sectional MAD scale is used.  Output is
    bounded through tanh so one volatile name cannot dominate an index family.
    """
    clean: List[Dict[str, Any]] = []
    no_sigma_idx: List[int] = []
    raw_missing: List[float] = []
    for row in rows or []:
        ch = _finite(row.get("change_pct"))
        if ch is None:
            continue
        sig = _finite(row.get("sigma_pct"), _finite(row.get("realized_vol_pct")))
        rec = dict(row)
        rec["change_pct"] = ch
        if sig is not None and abs(sig) >= 0.05:
            rec["z_sigma"] = sigma_scaled_return(ch, sig)
            rec["normalization"] = "OWN_SIGMA"
        else:
            no_sigma_idx.append(len(clean)); raw_missing.append(ch)
            rec["z_sigma"] = None
            rec["normalization"] = "ROBUST_MAD"
        clean.append(rec)

    if no_sigma_idx:
        zs = robust_z(raw_missing)
        for idx, z in zip(no_sigma_idx, zs):
            clean[idx]["z_sigma"] = z

    for rec in clean:
        z = _finite(rec.get("z_sigma"), 0.0) or 0.0
        rec["field"] = float(math.tanh(z / 1.5))

    vals = [r["field"] for r in clean]
    return {
        "rows": clean,
        "field": float(np.mean(vals)) if vals else 0.0,
        "count": len(vals),
        "method": "OWN_SIGMA_WHEN_AVAILABLE · ROBUST_MAD_FALLBACK · TANH_BOUNDED",
    }
