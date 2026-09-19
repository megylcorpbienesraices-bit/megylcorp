"""Stochastic / generative scenario lab (SHADOW only).

The local engine produces reproducible GBM/vol-shock scenario envelopes. It is a
scenario generator, not a price forecast, and has no Scanner authority. A future
external GenAI provider can explain/summarize scenarios through the same contract.
"""

from __future__ import annotations

from typing import Any, Dict
import hashlib
import math
import numpy as np


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _seed(symbol: str, spot: float, iv: float, horizon: int) -> int:
    raw = f"{symbol}|{spot:.8f}|{iv:.8f}|{horizon}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "little") % (2**32 - 1)


def build_scenario_lab(
    *, symbol: str, spot: Any, atm_iv_pct: Any, horizon_minutes: int = 90,
    paths: int = 1500, drift_annual: float = 0.0, macro_stress: Any = None,
) -> Dict[str, Any]:
    s = _f(spot)
    iv_pct = _f(atm_iv_pct)
    if s is None or s <= 0 or iv_pct is None or iv_pct <= 0:
        return {"ready": False, "state": "UNAVAILABLE", "reason": "SPOT_OR_IV_MISSING", "authority": "SHADOW"}
    horizon_minutes = max(5, min(int(horizon_minutes), 390))
    paths = max(250, min(int(paths), 10000))
    sigma = iv_pct / 100.0
    # 252 trading days * 390 regular-session minutes. This is only a scenario clock.
    t = horizon_minutes / (252.0 * 390.0)
    stress = _f(macro_stress, 0.0) or 0.0
    # Macro stress can widen the scenario variance but does not set direction.
    stress_mult = 1.0 + min(1.0, abs(stress) / 100.0) * 0.35
    sigma_eff = sigma * stress_mult
    rng = np.random.default_rng(_seed(str(symbol).upper(), s, sigma_eff, horizon_minutes))
    z = rng.standard_normal(paths)
    terminal = s * np.exp((drift_annual - 0.5 * sigma_eff**2) * t + sigma_eff * math.sqrt(t) * z)
    qs = np.quantile(terminal, [0.05, 0.16, 0.50, 0.84, 0.95])
    # 25 deterministic representative paths for visualization.
    steps = min(90, max(15, horizon_minutes))
    dt = t / steps
    zz = rng.standard_normal((25, steps))
    increments = (drift_annual - .5*sigma_eff**2)*dt + sigma_eff*math.sqrt(dt)*zz
    sim = s*np.exp(np.cumsum(increments, axis=1))
    sim = np.concatenate([np.full((25,1),s),sim],axis=1)
    def band_for(mult: float):
        sig = sigma_eff * mult
        std = sig * math.sqrt(t)
        # lognormal ±1 sigma around drift-neutral median
        med = s * math.exp((drift_annual - .5*sig**2)*t)
        return {"low": float(med*math.exp(-std)), "median": float(med), "high": float(med*math.exp(std)), "iv_pct": float(sig*100.0)}
    return {
        "ready": True, "state": "SHADOW", "authority": "NONE", "is_forecast": False,
        "engine": "STOCHASTIC_GENERATIVE_FALLBACK", "symbol": str(symbol).upper(),
        "spot": s, "atm_iv_pct": iv_pct, "effective_iv_pct": sigma_eff*100.0,
        "horizon_minutes": horizon_minutes, "path_count": paths,
        "quantiles": {"p05": float(qs[0]), "p16": float(qs[1]), "p50": float(qs[2]), "p84": float(qs[3]), "p95": float(qs[4])},
        "representative_paths": [[float(v) for v in row] for row in sim],
        "iv_scenarios": {"IV_MINUS": band_for(.80), "BASE": band_for(1.0), "IV_PLUS": band_for(1.20)},
        "model_risk": "SCENARIO GENERATOR · NOT A FORECAST · GBM/local IV assumptions · no Scanner authority",
    }
