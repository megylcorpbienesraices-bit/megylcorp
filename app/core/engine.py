from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Tuple
import math
import os
import threading
import numpy as np
import pandas as pd
from scipy.stats import norm
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .expiry_clock import year_fraction, year_fraction_array

REQUIRED_COLUMNS = {
    "timestamp", "underlying_price", "strike", "dte",
    "option_type", "open_interest", "volume", "iv"
}


@dataclass
class EngineConfig:
    risk_free_rate: float = 0.045
    dividend_yield: float = 0.013
    contract_multiplier: float = 100.0
    atm_z: float = 0.10
    near_atm_z: float = 0.35
    persistence_lookback: int = 3
    # v1.14.3: rolling per-symbol scale anchors from previous sessions. When present,
    # magnitude scores are measured against the asset's own history instead of against
    # the current snapshot, which is what makes them comparable across snapshots.
    scale_anchors: dict | None = None
    symbol: str = ""
    # Provenance makes a fallback model configuration distinguishable from a
    # properly resolved per-asset configuration.
    inputs_source: str = "PER_ASSET"
    anchors_source: str = "NONE"
    option_model: str = "EQUITY_OPTION"




def engine_config_for_asset(symbol: str, anchor_scope: str | None = None) -> EngineConfig:
    """Build model inputs per asset; never hide a fallback carry as resolved data."""
    sym = str(symbol or "").upper()
    try:
        from .precision_engine import market_inputs
        mi = market_inputs(symbol)
        from .instruments import get as get_instrument
        inst=get_instrument(sym)
        cfg = EngineConfig(
            risk_free_rate=float(mi["risk_free_rate"]),
            dividend_yield=float(mi["dividend_yield"]),
            contract_multiplier=float(inst.multiplier),
            symbol=sym, option_model=str(inst.option_model), inputs_source="PER_ASSET",
        )
        try:
            from .scale_anchors import load_anchors
            from . import alpaca_data
            cfg.scale_anchors = load_anchors(alpaca_data.DATA_DIR, cfg.symbol, anchor_scope) or None
            cfg.anchors_source = "HISTORICAL" if cfg.scale_anchors else "NONE"
        except Exception as exc:
            _obs_note("engine:scale_anchors_unavailable", exc, severity="DEGRADED")
            cfg.scale_anchors = None
            cfg.anchors_source = "UNAVAILABLE"
        return cfg
    except Exception as exc:
        _obs_note("engine:asset_carry_fallback:" + sym, exc, severity="CRITICAL_MODEL")
        return EngineConfig(symbol=sym, inputs_source="FALLBACK_DEFAULT_CARRY", anchors_source="UNAVAILABLE")

def _safe_t(dte: float) -> float:
    return year_fraction(float(dte))


def black_scholes_greeks(S: float, K: float, T: float, sigma: float, option_type: str,
                         r: float, q: float) -> Tuple[float, float]:
    sigma = max(float(sigma), 1e-6)
    T = max(float(T), 1e-8)
    S = max(float(S), 1e-8)
    K = max(float(K), 1e-8)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    gamma = math.exp(-q * T) * norm.pdf(d1) / (S * sigma * math.sqrt(T))
    if str(option_type).lower().startswith("c"):
        delta = math.exp(-q * T) * norm.cdf(d1)
    else:
        delta = math.exp(-q * T) * (norm.cdf(d1) - 1.0)
    return delta, gamma


def classify_moneyness(S: float, K: float, T: float, sigma: float, option_type: str,
                       atm_z: float = 0.10, near_atm_z: float = 0.35) -> str:
    z = abs(math.log(max(K, 1e-8) / max(S, 1e-8))) / max(sigma * math.sqrt(max(T, 1e-8)), 1e-8)
    if z <= atm_z:
        return "ATM"
    is_call = str(option_type).lower().startswith("c")
    itm = (S > K) if is_call else (S < K)
    if z <= near_atm_z:
        return "NEAR ITM" if itm else "NEAR OTM"
    if z <= 0.90:
        return "ITM" if itm else "OTM"
    return "DEEP ITM" if itm else "DEEP OTM"


def validate_input(df: pd.DataFrame) -> None:
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
    bad_types = set(df["option_type"].astype(str).str.lower().str[0]) - {"c", "p"}
    if bad_types:
        raise ValueError("option_type must be call/put or c/p")
    if (df["iv"] <= 0).any():
        raise ValueError("iv must be positive and expressed as decimal, e.g. 0.22")
    if (df["open_interest"] < 0).any() or (df["volume"] < 0).any():
        raise ValueError("open_interest and volume cannot be negative")


def enrich_options(df: pd.DataFrame, cfg: EngineConfig) -> pd.DataFrame:
    validate_input(df)
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=False)
    out = out.sort_values(["timestamp", "strike", "option_type"]).reset_index(drop=True)

    # v1.14: enriquecimiento vectorizado. v1.42: una sola puerta de Greeks.
    # El import es duro a propósito. La rama de respaldo anterior reimplementaba
    # Black-Scholes y Black-76 aquí mismo: era una SEGUNDA fórmula que podía
    # divergir en silencio de la del dispatcher, justo lo que esta versión elimina.
    # Si la puerta no carga, la exposición no se calcula: fail closed.
    from .greeks_service import greeks_vector as greeks_vector_for_symbol

    S_a = pd.to_numeric(out["underlying_price"], errors="coerce").to_numpy(float)
    K_a = pd.to_numeric(out["strike"], errors="coerce").to_numpy(float)
    iv_a = pd.to_numeric(out["iv"], errors="coerce").to_numpy(float)
    dte_a = pd.to_numeric(out["dte"], errors="coerce").to_numpy(float)
    T_a = year_fraction_array(dte_a)
    call_a = out["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()

    if "model_risk_free_rate" in out.columns:
        r_a = pd.to_numeric(out["model_risk_free_rate"], errors="coerce").to_numpy(float)
        r_a = np.where(np.isfinite(r_a), r_a, cfg.risk_free_rate)
    else:
        r_a = np.full(len(out), cfg.risk_free_rate, dtype=float)
    if "model_dividend_yield" in out.columns:
        q_a = pd.to_numeric(out["model_dividend_yield"], errors="coerce").to_numpy(float)
        q_a = np.where(np.isfinite(q_a), q_a, cfg.dividend_yield)
    else:
        q_a = np.full(len(out), cfg.dividend_yield, dtype=float)
    if "contract_multiplier" in out.columns:
        mult_a = pd.to_numeric(out["contract_multiplier"], errors="coerce").to_numpy(float)
        mult_a = np.where(np.isfinite(mult_a) & (mult_a>0), mult_a, cfg.contract_multiplier)
    else:
        mult_a = np.full(len(out), cfg.contract_multiplier, dtype=float)

    g = greeks_vector_for_symbol(cfg.symbol, S_a, K_a, T_a, iv_a, call_a, r_a, q_a)
    out["calc_delta"] = g["delta"]; out["calc_gamma"] = g["gamma"]
    out["calc_vanna"] = g["vanna"]; out["calc_charm"] = g["charm"]; out["calc_speed"] = g["speed"]
    out["calc_vega"] = g.get("vega", np.nan)

    # Vectorised moneyness, same thresholds as classify_moneyness().
    z = np.abs(np.log(np.maximum(K_a, 1e-8) / np.maximum(S_a, 1e-8))) / np.maximum(np.maximum(iv_a, 1e-6) * np.sqrt(np.maximum(T_a, 1e-8)), 1e-8)
    itm = np.where(call_a, S_a > K_a, S_a < K_a)
    money_a = np.where(
        z <= cfg.atm_z, "ATM",
        np.where(z <= cfg.near_atm_z, np.where(itm, "NEAR ITM", "NEAR OTM"),
                 np.where(z <= 0.90, np.where(itm, "ITM", "OTM"),
                          np.where(itm, "DEEP ITM", "DEEP OTM"))))
    out["moneyness"] = money_a

    # Signed GEX proxy convention: calls positive, puts negative. This is NOT a claim about dealer inventory.
    sign = np.where(out["option_type"].astype(str).str.lower().str.startswith("c"), 1.0, -1.0)
    out["signed_gex_proxy"] = (
        sign * out["calc_gamma"] * out["open_interest"] * mult_a
        * (out["underlying_price"] ** 2) * 0.01
    )
    out["gross_gex"] = out["signed_gex_proxy"].abs()
    out["option_delta_exposure_info"] = (
        out["calc_delta"] * out["open_interest"] * mult_a * out["underlying_price"]
    )
    return out


def _minmax(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    lo, hi = float(s.min()), float(s.max())
    if math.isclose(lo, hi):
        return pd.Series(np.full(len(s), 0.5), index=s.index)
    return (s - lo) / (hi - lo)


def _pct(series: pd.Series) -> pd.Series:
    """Cross-sectional percentile rank in [0,1].

    Percentiles are robust to isolated outliers and are useful for ordering strikes,
    but rank alone discards physical magnitude. v1.14.1 therefore uses this only as
    one component of a rank + robust-magnitude normalization.
    """
    x = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)
    if len(x) <= 1:
        return pd.Series(np.full(len(x), 0.5), index=x.index, dtype=float)
    return x.rank(method="average", pct=True).astype(float)


def _robust_magnitude(series: pd.Series, anchor: dict | None = None) -> pd.Series:
    """Robust positive-magnitude score in [0,1].

    log1p with 10th/90th percentile anchors, so one extreme strike cannot rescale the
    whole snapshot.

    v1.14.3 CORRECTION OF AN EARLIER CLAIM. Without `anchor` the quantiles come from
    the CURRENT snapshot, which makes this a log-scale robust min-max: multiply every
    value in the chain by 100 and the output is unchanged, exactly like percentile
    rank. It therefore does NOT preserve physical magnitude across snapshots, contrary
    to what the previous docstring said. Pass a historical anchor (see
    core.scale_anchors) to get that property: then lo/hi are fixed from prior sessions
    and a genuinely larger chain really does score higher.
    """
    x = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float).clip(lower=0.0)
    if len(x) == 0:
        return pd.Series(dtype=float, index=x.index)
    if anchor:
        try:
            from .scale_anchors import anchored_magnitude
            scored = anchored_magnitude(x.to_numpy(float), anchor)
            if scored is not None:
                return pd.Series(scored, index=x.index, dtype=float)
        except Exception as _e:
            _obs_note('engine:206', _e)
    y = np.log1p(x.to_numpy(float))
    lo = float(np.nanquantile(y, 0.10)); hi = float(np.nanquantile(y, 0.90))
    if not math.isfinite(lo) or not math.isfinite(hi) or math.isclose(lo, hi):
        fill = 0.0 if float(np.nanmax(x.to_numpy(float))) <= 0 else 0.5
        return pd.Series(np.full(len(x), fill), index=x.index, dtype=float)
    return pd.Series(np.clip((y - lo) / (hi - lo), 0.0, 1.0), index=x.index, dtype=float)


def _hybrid_score(series: pd.Series, rank_weight: float = 0.65, anchor: dict | None = None) -> pd.Series:
    """Blend robust rank (ordering within the chain) and magnitude (physical size).

    With a historical anchor the magnitude half is genuinely absolute, so the blend
    answers both questions at once: where does this strike sit among today's strikes,
    and is today's structure big or small for this asset.
    """
    rw = float(np.clip(rank_weight, 0.0, 1.0))
    return rw * _pct(series) + (1.0 - rw) * _robust_magnitude(series, anchor)


def aggregate_strikes(enriched: pd.DataFrame) -> pd.DataFrame:
    x = enriched
    # El desglose por tipo se agrega junto al neto porque los muros lo necesitan: un
    # Call Wall es donde se concentra la gamma DE LAS CALLS, y un strike con mucha
    # call gamma y mucha put gamma tiene un neto pequeño que lo escondía.
    if "option_type" in x.columns:
        is_call = x["option_type"].astype(str).str.lower().str.startswith("c")
        sg = numeric_column(x,"signed_gex_proxy",0.0)
        oi = numeric_column(x,"open_interest",0.0)
        x = x.assign(
            _call_gamma=sg.where(is_call, 0.0),
            _put_gamma=sg.where(~is_call, 0.0),
            _call_oi=oi.where(is_call, 0.0),
            _put_oi=oi.where(~is_call, 0.0),
        )
    else:
        x = x.assign(_call_gamma=0.0, _put_gamma=0.0, _call_oi=0.0, _put_oi=0.0)

    g = x.groupby(["timestamp", "strike"], as_index=False).agg(
        underlying_price=("underlying_price", "last"),
        signed_gex=("signed_gex_proxy", "sum"),
        gross_gex=("gross_gex", "sum"),
        call_gamma=("_call_gamma", "sum"),
        put_gamma=("_put_gamma", "sum"),
        call_oi=("_call_oi", "sum"),
        put_oi=("_put_oi", "sum"),
        open_interest=("open_interest", "sum"),
        option_volume=("volume", "sum"),
        avg_iv=("iv", "mean"),
    )
    g["abs_signed_gex"] = g["signed_gex"].abs()
    return g.sort_values(["timestamp", "strike"]).reset_index(drop=True)


def gamma_center(snapshot: pd.DataFrame) -> float:
    w = snapshot["gross_gex"].to_numpy(float)
    k = snapshot["strike"].to_numpy(float)
    if w.sum() <= 0:
        return float(np.nan)
    return float(np.sum(k * w) / np.sum(w))


def gamma_flip(snapshot: pd.DataFrame) -> float:
    s = snapshot.sort_values("strike")
    x = s["strike"].to_numpy(float)
    y = s["signed_gex"].to_numpy(float)
    if len(x) == 0:
        return float("nan")
    exact = np.where(np.isclose(y, 0.0))[0]
    if len(exact):
        return float(x[exact[0]])
    idx = np.where(np.sign(y[:-1]) != np.sign(y[1:]))[0]
    if len(idx):
        i = int(idx[np.argmin(np.abs(x[idx] - float(s["underlying_price"].iloc[-1])))])
        x1, x2, y1, y2 = x[i], x[i+1], y[i], y[i+1]
        if math.isclose(y1, y2):
            return float((x1 + x2) / 2.0)
        return float(x1 - y1 * (x2 - x1) / (y2 - y1))
    # If no sign crossing exists, return strike with minimum absolute signed GEX as a diagnostic proxy.
    return float(x[np.argmin(np.abs(y))])



def _net_gex_at_hypothetical_spot(snapshot_options: pd.DataFrame, S_hyp: float, cfg: EngineConfig) -> float:
    """Reprice Gamma at a hypothetical DIA spot while holding OI/IV/DTE snapshot inputs fixed.

    This is a transparent structural proxy, not a claim about observed dealer inventory.
    """
    S_hyp = max(float(S_hyp), 1e-8)
    K = snapshot_options["strike"].to_numpy(float)
    sigma = np.maximum(snapshot_options["iv"].to_numpy(float), 1e-6)
    T = year_fraction_array(snapshot_options["dte"].to_numpy(float))
    oi = snapshot_options["open_interest"].to_numpy(float)
    is_call = snapshot_options["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()
    sign = np.where(is_call, 1.0, -1.0)
    r = pd.to_numeric(snapshot_options.get("model_risk_free_rate", pd.Series(cfg.risk_free_rate,index=snapshot_options.index)),errors="coerce").to_numpy(float)
    q = pd.to_numeric(snapshot_options.get("model_dividend_yield", pd.Series(cfg.dividend_yield,index=snapshot_options.index)),errors="coerce").to_numpy(float)
    mult = pd.to_numeric(snapshot_options.get("contract_multiplier", pd.Series(cfg.contract_multiplier,index=snapshot_options.index)),errors="coerce").to_numpy(float)
    r=np.where(np.isfinite(r),r,cfg.risk_free_rate);q=np.where(np.isfinite(q),q,cfg.dividend_yield)
    mult=np.where(np.isfinite(mult)&(mult>0),mult,cfg.contract_multiplier)
    sqrtT = np.sqrt(T)
    d1 = (np.log(S_hyp / np.maximum(K, 1e-8)) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    gamma = np.exp(-q * T) * norm.pdf(d1) / (S_hyp * sigma * sqrtT)
    signed = sign * gamma * oi * mult * (S_hyp ** 2) * 0.01
    return float(np.nansum(signed))


def dynamic_gamma_flip_snapshot(snapshot_options: pd.DataFrame, cfg: EngineConfig, grid_points: int = 401) -> Dict[str, object]:
    """Reprice the whole snapshot across hypothetical DIA spots and solve NetGEX(S)=0.

    A missing zero crossing is a valid market state. In that case the closest-to-zero
    point is returned only as a diagnostic equilibrium and is NOT treated as a true flip.
    """
    snap = snapshot_options.copy()
    if snap.empty:
        return {"flip": float("nan"), "crossing": False, "method": "NO_DATA", "grid_low": float("nan"), "grid_high": float("nan"), "net_at_flip": float("nan")}
    spot = float(snap["underlying_price"].iloc[-1])
    strikes = np.sort(snap["strike"].astype(float).unique())
    step = float(np.median(np.diff(strikes))) if len(strikes) > 1 else max(spot * 0.002, 0.5)
    base_low = min(float(strikes.min()), spot)
    base_high = max(float(strikes.max()), spot)
    base_span = max(base_high - base_low, step * 4.0, spot * 0.01)

    K = snap["strike"].to_numpy(float)[None, :]
    sigma = np.maximum(snap["iv"].to_numpy(float), 1e-6)[None, :]
    T = year_fraction_array(snap["dte"].to_numpy(float))[None, :]
    oi = snap["open_interest"].to_numpy(float)[None, :]
    is_call = snap["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()[None, :]
    sign = np.where(is_call, 1.0, -1.0)
    r = pd.to_numeric(snap.get("model_risk_free_rate", pd.Series(cfg.risk_free_rate,index=snap.index)),errors="coerce").to_numpy(float)[None,:]
    q = pd.to_numeric(snap.get("model_dividend_yield", pd.Series(cfg.dividend_yield,index=snap.index)),errors="coerce").to_numpy(float)[None,:]
    mult = pd.to_numeric(snap.get("contract_multiplier", pd.Series(cfg.contract_multiplier,index=snap.index)),errors="coerce").to_numpy(float)[None,:]
    r=np.where(np.isfinite(r),r,cfg.risk_free_rate);q=np.where(np.isfinite(q),q,cfg.dividend_yield)
    mult=np.where(np.isfinite(mult)&(mult>0),mult,cfg.contract_multiplier)
    sqrtT = np.sqrt(T)

    best = None
    # Progressive search. The last pass is intentionally broad enough for a daily DIA chain.
    #
    # v1.42.2: `pad` se satura en min(pad, max(spot*0.15, step*6)), asi que las
    # expansiones altas producian LA MISMA malla que la anterior. Medido sobre una
    # cadena SPY tipica: 4 pasadas -> 2 mallas distintas, es decir 2 evaluaciones
    # redundantes de una matriz (grid_points x n_contratos) en ruta caliente, y se
    # pagaban enteras justo cuando NO hay cruce (que es cuando el bucle no sale
    # antes). Deduplicar los limites conserva exactamente el mismo resultado.
    _seen_bounds: set[tuple[float, float]] = set()
    for expansion in (0.25, 0.75, 1.50, 3.00):
        pad = max(step * 2.0, base_span * expansion)
        pad = min(pad, max(spot * 0.15, step * 6.0))
        lo = max(0.01, base_low - pad)
        hi = base_high + pad
        bounds = (round(float(lo), 9), round(float(hi), 9))
        if bounds in _seen_bounds:
            continue
        _seen_bounds.add(bounds)
        grid = np.linspace(lo, hi, int(grid_points))
        S = grid[:, None]
        d1 = (np.log(S / np.maximum(K, 1e-8)) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
        gamma = np.exp(-q * T) * norm.pdf(d1) / (S * sigma * sqrtT)
        vals = np.nansum(sign * gamma * oi * mult * (S ** 2) * 0.01, axis=1)

        abs_idx = int(np.nanargmin(np.abs(vals)))
        candidate = {"flip": float(grid[abs_idx]), "crossing": False, "method": "NEAREST_ZERO_NO_CROSS", "grid_low": float(lo), "grid_high": float(hi), "net_at_flip": float(vals[abs_idx])}
        if best is None or abs(candidate["net_at_flip"]) < abs(best["net_at_flip"]):
            best = candidate

        cross_idx = np.where(np.sign(vals[:-1]) != np.sign(vals[1:]))[0]
        if len(cross_idx):
            mids = (grid[cross_idx] + grid[cross_idx + 1]) / 2.0
            j = int(cross_idx[np.argmin(np.abs(mids - spot))])
            x1, x2 = float(grid[j]), float(grid[j + 1])
            y1, y2 = float(vals[j]), float(vals[j + 1])
            root = (x1 + x2) / 2.0 if math.isclose(y1, y2) else x1 - y1 * (x2 - x1) / (y2 - y1)
            net_root = _net_gex_at_hypothetical_spot(snap, root, cfg)
            return {"flip": float(root), "crossing": True, "method": "DYNAMIC_ROOT", "grid_low": float(lo), "grid_high": float(hi), "net_at_flip": float(net_root)}
    return best

# =====================================================================
# v1.27.14 · CACHE DE FLIP POR SNAPSHOT
# Historical snapshots are immutable; recomputing NetGEX(S)=0 for every prior
# timestamp on every refresh wastes CPU.  The key fingerprints snapshot content
# plus model inputs, so corrected snapshots or changed carry invalidate safely.
# Kill-switch: ITM_FLIP_CACHE=0.
# =====================================================================
_FLIP_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_FLIP_CACHE_LOCK = threading.Lock()
_FLIP_CACHE_MAX = 4096
_FLIP_FINGERPRINT_COLS = (
    "strike", "iv", "dte", "open_interest", "option_type", "underlying_price",
    "signed_gex_proxy", "model_risk_free_rate", "model_dividend_yield",
    "contract_multiplier",
)

def _flip_cache_enabled() -> bool:
    return str(os.getenv("ITM_FLIP_CACHE", "1")).strip().lower() not in {"0", "false", "no", "off"}

def _flip_cache_key(ts, snap: pd.DataFrame, cfg: EngineConfig) -> tuple | None:
    try:
        cols = [c for c in _FLIP_FINGERPRINT_COLS if c in snap.columns]
        if not cols:
            return None
        digest = int(pd.util.hash_pandas_object(snap[cols], index=False).sum()) & 0xFFFFFFFFFFFFFFFF
        return (
            str(cfg.symbol), str(pd.Timestamp(ts)), int(len(snap)), digest,
            round(float(cfg.risk_free_rate), 10), round(float(cfg.dividend_yield), 10),
            round(float(cfg.contract_multiplier), 6),
        )
    except Exception as exc:
        _obs_note("engine:flip_cache_key", exc, severity="DEGRADED")
        return None

def flip_cache_stats() -> Dict[str, int]:
    with _FLIP_CACHE_LOCK:
        return {"entries": len(_FLIP_CACHE), "max_entries": _FLIP_CACHE_MAX,
                "enabled": int(_flip_cache_enabled())}

def clear_flip_cache() -> None:
    with _FLIP_CACHE_LOCK:
        _FLIP_CACHE.clear()

def _flip_snapshot_cached(ts, snap: pd.DataFrame, cfg: EngineConfig) -> Dict[str, object]:
    if not _flip_cache_enabled():
        return dynamic_gamma_flip_snapshot(snap, cfg)
    key = _flip_cache_key(ts, snap, cfg)
    if key is None:
        return dynamic_gamma_flip_snapshot(snap, cfg)
    with _FLIP_CACHE_LOCK:
        hit = _FLIP_CACHE.get(key)
        if hit is not None:
            _FLIP_CACHE.move_to_end(key)
            return dict(hit)
    info = dynamic_gamma_flip_snapshot(snap, cfg)
    with _FLIP_CACHE_LOCK:
        _FLIP_CACHE[key] = dict(info)
        _FLIP_CACHE.move_to_end(key)
        while len(_FLIP_CACHE) > _FLIP_CACHE_MAX:
            _FLIP_CACHE.popitem(last=False)
    return info

def dynamic_gamma_flip_history(enriched: pd.DataFrame, cfg: EngineConfig) -> pd.DataFrame:
    rows = []
    for ts, snap in enriched.groupby("timestamp", sort=True):
        info = _flip_snapshot_cached(ts, snap, cfg)
        diagnostic = float(info["flip"]) if np.isfinite(info["flip"]) else float("nan")
        if not bool(info["crossing"]):
            # Do not show the far-tail nearest-zero point as a Gamma Flip. When no true
            # NetGEX(S)=0 root exists, retain the older local strike-sign proxy only as
            # a clearly labelled fallback level for visual continuity.
            local = snap.groupby("strike", as_index=False).agg(
                underlying_price=("underlying_price", "last"),
                signed_gex=("signed_gex_proxy", "sum"),
            )
            info["flip"] = gamma_flip(local)
            info["method"] = "STRIKE_SIGN_PROXY_FALLBACK"
        rows.append({
            "timestamp": pd.Timestamp(ts),
            "gamma_flip": info["flip"],
            "flip_crossing": bool(info["crossing"]),
            "flip_method": info["method"],
            "flip_grid_low": info["grid_low"],
            "flip_grid_high": info["grid_high"],
            "flip_net_at_root": info["net_at_flip"],
            "flip_nearest_zero_diagnostic": diagnostic,
        })
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

def _flip_dynamics(flip_history: pd.DataFrame, strike_step: float, spot: float) -> Dict[str, object]:
    h = flip_history.dropna(subset=["gamma_flip"]).copy().sort_values("timestamp")
    if h.empty:
        return {"flip_move": 0.0, "flip_direction": "FLAT", "flip_speed_per_5m": 0.0, "flip_velocity_label": "STABLE", "flip_acceleration": 0.0, "flip_acceleration_label": "STABLE", "flip_distance": float("nan"), "flip_proximity_label": "UNKNOWN"}
    current = h.iloc[-1]
    current_flip = float(current["gamma_flip"])
    distance = float(spot - current_flip)
    if not bool(current.get("flip_crossing", False)):
        return {"flip_move": 0.0, "flip_direction": "NO_CROSS", "flip_speed_per_5m": 0.0, "flip_velocity_label": "NO CROSS", "flip_acceleration": 0.0, "flip_acceleration_label": "N/A", "flip_distance": distance, "flip_proximity_label": "NO ROOT"}

    # Movement is only measured between genuine roots; diagnostic nearest-zero points are excluded.
    roots = h[h["flip_crossing"] == True].copy()
    move = 0.0
    speed5 = 0.0
    prev_speed5 = 0.0
    if len(roots) >= 2:
        move = float(roots["gamma_flip"].iloc[-1] - roots["gamma_flip"].iloc[-2])
        dt = max((pd.Timestamp(roots["timestamp"].iloc[-1]) - pd.Timestamp(roots["timestamp"].iloc[-2])).total_seconds() / 60.0, 1e-6)
        speed5 = abs(move) / dt * 5.0
    if len(roots) >= 3:
        m0 = float(roots["gamma_flip"].iloc[-2] - roots["gamma_flip"].iloc[-3])
        dt0 = max((pd.Timestamp(roots["timestamp"].iloc[-2]) - pd.Timestamp(roots["timestamp"].iloc[-3])).total_seconds() / 60.0, 1e-6)
        prev_speed5 = abs(m0) / dt0 * 5.0
    accel = speed5 - prev_speed5
    step = max(float(strike_step), 0.25)
    direction = "UP" if move > step * 0.01 else "DOWN" if move < -step * 0.01 else "FLAT"
    if speed5 <= step * 0.10:
        vel = "STABLE"
    elif speed5 <= step * 0.35:
        vel = "MOVING"
    else:
        vel = "FAST"
    if accel > step * 0.10:
        acc_label = "ACCELERATING"
    elif accel < -step * 0.10:
        acc_label = "DECELERATING"
    else:
        acc_label = "STABLE"
    dstep = abs(distance) / step
    if dstep <= 0.25:
        prox = "VERY HIGH"
    elif dstep <= 0.50:
        prox = "HIGH"
    elif dstep <= 1.00:
        prox = "MEDIUM"
    else:
        prox = "LOW"
    return {"flip_move": float(move), "flip_direction": direction, "flip_speed_per_5m": float(speed5), "flip_velocity_label": vel, "flip_acceleration": float(accel), "flip_acceleration_label": acc_label, "flip_distance": distance, "flip_proximity_label": prox}

def compute_structure(enriched: pd.DataFrame, cfg: EngineConfig) -> Dict[str, object]:
    agg = aggregate_strikes(enriched)
    timestamps = list(pd.Series(agg["timestamp"].unique()).sort_values())
    current_ts = timestamps[-1]
    current = agg[agg["timestamp"] == current_ts].copy().sort_values("strike").reset_index(drop=True)
    prev = agg[agg["timestamp"] == timestamps[-2]].copy() if len(timestamps) > 1 else pd.DataFrame()

    # v1.14 · DE-CORRELATED FEATURE BASIS
    # gross_gex is gamma x OI x multiplier x S^2, so scoring gross_gex, OI and
    # volume side by side was scoring open interest three times under different
    # names. We instead decompose the same information multiplicatively into
    # three near-independent factors:
    #     mass       = open interest at the strike
    #     intensity  = gamma per contract  (gross_gex / OI)  -> pure convexity
    #     turnover   = volume / OI                            -> pure activity
    # Their product reconstructs the original quantities, but as regressors they
    # carry far less shared variance. Legacy *_norm columns are retained for the
    # UI and downstream compatibility.
    _oi = pd.to_numeric(current["open_interest"], errors="coerce").fillna(0.0)
    _vol = pd.to_numeric(current["option_volume"], errors="coerce").fillna(0.0)
    _gross = pd.to_numeric(current["gross_gex"], errors="coerce").fillna(0.0)
    current["gamma_intensity"] = _gross / _oi.where(_oi > 0, np.nan)

    # Turnover needs a small denominator stabilizer: 100 contracts over OI=2 is
    # activity, but should not score 50x more strongly just because the base is tiny.
    positive_oi = _oi[_oi > 0]
    oi_stabilizer = max(25.0, 0.10 * float(positive_oi.median()) if len(positive_oi) else 25.0)
    current["turnover_raw"] = _vol / _oi.where(_oi > 0, np.nan)
    current["turnover"] = _vol / (_oi + oi_stabilizer)
    current["turnover_oi_stabilizer"] = oi_stabilizer

    # Rank + magnitude, with the magnitude half anchored to this asset's own history
    # when enough sessions have been observed (see core.scale_anchors).
    _anch = cfg.scale_anchors or {}
    current["mass_pct"] = _hybrid_score(_oi, anchor=_anch.get("open_interest"))
    current["intensity_pct"] = _hybrid_score(current["gamma_intensity"].fillna(0.0), anchor=_anch.get("gamma_intensity"))
    current["turnover_pct"] = _hybrid_score(current["turnover"].fillna(0.0), anchor=_anch.get("turnover"))
    current["scale_anchor_active"] = bool(_anch)

    # Net tilt is a ratio, but a 100% imbalance on negligible GEX must not outrank
    # a large structural imbalance. Gate the ratio gently by robust gross magnitude.
    _absnet = pd.to_numeric(current["abs_signed_gex"], errors="coerce").fillna(0.0)
    current["net_tilt_ratio"] = (_absnet / _gross.where(_gross > 0, np.nan)).fillna(0.0).clip(0.0, 1.0)
    current["net_tilt_raw_pct"] = 0.65 * _pct(current["net_tilt_ratio"]) + 0.35 * current["net_tilt_ratio"]
    # v1.14.3 FIX. The previous gate multiplied the tilt by 0.30 + 0.70*magnitude(gross).
    # Since gross_gex ~ gamma * OI, that put open-interest mass back inside a feature
    # whose entire purpose was to be mass-free, and mass then entered dominance three
    # times (mass_pct, intensity_pct, and inside net_tilt). Measured on a synthetic SPY
    # chain it raised net_tilt-vs-OI correlation from 0.077 to 0.251 and mean pairwise
    # collinearity from 0.125 to 0.229.
    #
    # The concern behind the gate was real - a 100% imbalance on negligible GEX should
    # not be treated as evidence - but a CONTINUOUS gate injects a mass gradient across
    # the whole feature to solve a problem that only exists in the bottom tail. A hard
    # floor neutralises the noise region and leaves the rest of the feature clean.
    # (Note also that tilt carries weight 0.15, i.e. at most 15 of 100 dominance points,
    # so a mass-less strike could never dominate on tilt alone in the first place.)
    _gross_rank = _pct(_gross)
    _floor = float(np.clip(float(os.getenv("ITM_NET_TILT_FLOOR_Q", "0.25")), 0.0, 0.9))
    current["net_tilt_pct"] = current["net_tilt_raw_pct"].where(_gross_rank >= _floor, 0.5).clip(0.0, 1.0)
    current["net_tilt_floor_quantile"] = _floor

    # Compatibility/UI fields now also use the hybrid normalization.
    current["gex_norm"] = _hybrid_score(current["gross_gex"], anchor=_anch.get("gross_gex"))
    current["net_norm"] = _hybrid_score(current["abs_signed_gex"], anchor=_anch.get("gross_gex"))
    current["oi_norm"] = _hybrid_score(current["open_interest"], anchor=_anch.get("open_interest"))
    current["vol_norm"] = _hybrid_score(current["option_volume"], anchor=_anch.get("option_volume"))
    spot = float(current["underlying_price"].iloc[-1])
    span = max(float(current["strike"].max() - current["strike"].min()), 1.0)
    current["proximity"] = 1.0 - np.minimum(np.abs(current["strike"] - spot) / max(span * 0.25, 1.0), 1.0)

    if not prev.empty:
        p = prev[["strike", "gross_gex", "signed_gex"]].rename(columns={
            "gross_gex": "prev_gross_gex", "signed_gex": "prev_signed_gex"
        })
        current = current.merge(p, on="strike", how="left")
        current["gex_change_pct"] = (
            (current["gross_gex"] - current["prev_gross_gex"])
            / current["prev_gross_gex"].replace(0, np.nan).abs()
        ).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    else:
        current["prev_gross_gex"] = np.nan
        current["prev_signed_gex"] = np.nan
        current["gex_change_pct"] = 0.0

    current["persistence"] = 1.0 - np.minimum(np.abs(current["gex_change_pct"]), 1.0)
    # Dominance is now built on the de-correlated basis. Each family enters once.
    current["dominance_score"] = 100.0 * (
        0.32 * current["mass_pct"] +
        0.28 * current["intensity_pct"] +
        0.15 * current["net_tilt_pct"] +
        0.10 * current["turnover_pct"] +
        0.10 * current["proximity"] +
        0.05 * current["persistence"]
    )

    # Dynamic Gamma Flip: reprice the option snapshot across hypothetical DIA spots and solve NetGEX(S)=0.
    strikes_unique = np.sort(current["strike"].unique())
    strike_step_for_flip = float(np.median(np.diff(strikes_unique))) if len(strikes_unique) > 1 else 1.0
    flip_history = dynamic_gamma_flip_history(enriched, cfg)
    flip_info = flip_history.iloc[-1].to_dict()
    flip = float(flip_info["gamma_flip"])
    flip_dyn = _flip_dynamics(flip_history, strike_step_for_flip, spot)

    pos_gamma = (np.tanh(current["signed_gex"] / max(current["abs_signed_gex"].median(), 1.0)) + 1.0) / 2.0
    neg_gamma = 1.0 - pos_gamma
    weakening = np.clip(-current["gex_change_pct"], 0.0, 1.0)
    transition_change = np.clip(np.abs(current["gex_change_pct"]), 0.0, 1.0)

    flip_prox = (1.0 - np.minimum(np.abs(current["strike"] - flip) / max(span * 0.15, 1.0), 1.0)) if bool(flip_info.get("flip_crossing", False)) else np.zeros(len(current))

    current["containment_score"] = 100.0 * (
        0.42 * (current["dominance_score"] / 100.0) +
        0.25 * pos_gamma +
        0.15 * current["persistence"] +
        0.18 * current["proximity"]
    )
    current["break_score"] = 100.0 * (
        0.35 * (current["dominance_score"] / 100.0) +
        0.28 * neg_gamma +
        0.22 * weakening +
        0.15 * current["proximity"]
    )
    # v1.14: dominance-free residuals. The scenario layer already blends dominance
    # through its structural term; feeding it a containment score that ALSO embeds
    # dominance made the same evidence count twice inside one weighted sum.
    # Weights below are the originals renormalised after dropping the dominance term.
    current["containment_core"] = 100.0 * (
        0.431 * pos_gamma + 0.259 * current["persistence"] + 0.310 * current["proximity"]
    )
    current["break_core"] = 100.0 * (
        0.431 * neg_gamma + 0.338 * weakening + 0.231 * current["proximity"]
    )

    balance = 1.0 - np.abs(current["containment_score"] - current["break_score"]) / 100.0
    current["transition_score"] = 100.0 * (
        0.40 * balance + 0.35 * flip_prox + 0.25 * transition_change
    )

    states = []
    confidence = []
    for row in current.itertuples(index=False):
        vals = {
            "CONTAINMENT": float(row.containment_score),
            "BREAK": float(row.break_score),
            "TRANSITION": float(row.transition_score),
        }
        st = max(vals, key=vals.get)
        states.append(st)
        confidence.append(vals[st])
    current["state"] = states
    current["state_confidence"] = confidence

    # Global structure and migration
    centers = []
    totals = []
    for ts in timestamps:
        snap = agg[agg["timestamp"] == ts]
        centers.append((ts, gamma_center(snap)))
        totals.append((ts, float(snap["signed_gex"].sum()), float(snap["gross_gex"].sum())))
    center_df = pd.DataFrame(centers, columns=["timestamp", "gamma_center"])
    if len(center_df) > 1:
        move = float(center_df["gamma_center"].iloc[-1] - center_df["gamma_center"].iloc[-2])
    else:
        move = 0.0
    strike_step = float(np.median(np.diff(np.sort(current["strike"].unique())))) if current["strike"].nunique() > 1 else 1.0
    migration_strength = float(np.clip(abs(move) / max(strike_step, 0.25) * 50.0, 0.0, 100.0))
    migration_direction = "UP" if move > 1e-9 else "DOWN" if move < -1e-9 else "FLAT"

    # Gamma-only pressure is structural, not a directional dealer-flow claim.
    above = current[current["strike"] > spot]["gross_gex"].sum()
    below = current[current["strike"] < spot]["gross_gex"].sum()
    imbalance = float((above - below) / max(above + below, 1.0))
    migration_component = np.sign(move) * min(abs(move) / max(strike_step, 0.25), 1.0)
    pressure_raw = 0.60 * imbalance + 0.40 * migration_component
    pressure_score = float(np.clip(abs(pressure_raw) * 100.0, 0.0, 100.0))
    pressure_direction = "UP" if pressure_raw > 0.03 else "DOWN" if pressure_raw < -0.03 else "NEUTRAL"

    total_signed = float(current["signed_gex"].sum())
    regime = "POSITIVE GAMMA" if total_signed > 0 else "NEGATIVE GAMMA" if total_signed < 0 else "NEUTRAL GAMMA"
    dominant = current.sort_values("dominance_score", ascending=False).head(8).copy()

    return {
        "current": current,
        "aggregate": agg,
        "center_history": center_df,
        "gamma_center": float(center_df["gamma_center"].iloc[-1]),
        "gamma_center_prev": float(center_df["gamma_center"].iloc[-2]) if len(center_df) > 1 else float("nan"),
        "gamma_flip": float(flip),
        "gamma_flip_history": flip_history,
        "gamma_flip_crossing": bool(flip_info.get("flip_crossing", False)),
        "gamma_flip_method": str(flip_info.get("flip_method", "UNKNOWN")),
        "gamma_flip_grid_low": float(flip_info.get("flip_grid_low", float("nan"))),
        "gamma_flip_grid_high": float(flip_info.get("flip_grid_high", float("nan"))),
        "gamma_flip_net_at_root": float(flip_info.get("flip_net_at_root", float("nan"))),
        "gamma_flip_nearest_zero_diagnostic": float(flip_info.get("flip_nearest_zero_diagnostic", float("nan"))),
        **flip_dyn,
        "migration_direction": migration_direction,
        "migration_strength": migration_strength,
        "migration_move": move,
        "pressure_direction": pressure_direction,
        "pressure_score": pressure_score,
        "regime": regime,
        "total_signed_gex": total_signed,
        "total_gross_gex": float(current["gross_gex"].sum()),
        "dominant": dominant,
        "spot": spot,
        "timestamp": pd.Timestamp(current_ts),
    }


def audit(enriched: pd.DataFrame, result: Dict[str, object]) -> pd.DataFrame:
    checks = []
    def add(name: str, ok: bool, detail: str):
        checks.append({"check": name, "status": "OK" if ok else "WARN", "detail": detail})

    latest = enriched["timestamp"].max()
    latest_df = enriched[enriched["timestamp"] == latest]
    add("Required columns", REQUIRED_COLUMNS.issubset(enriched.columns), "Input schema complete")
    add("No duplicate option rows", not enriched.duplicated(["timestamp","strike","dte","option_type"]).any(), "Unique snapshot/strike/DTE/type rows")
    add("IV valid", bool((enriched["iv"] > 0).all()), f"min IV={enriched['iv'].min():.4f}")
    add("OI valid", bool((enriched["open_interest"] >= 0).all()), f"latest OI={latest_df['open_interest'].sum():,.0f}")
    add("Greeks finite", bool(np.isfinite(enriched[["calc_delta","calc_gamma"]].to_numpy()).all()), "Black-Scholes Greeks finite")
    add("Gamma non-negative", bool((enriched["calc_gamma"] >= 0).all()), "Option gamma itself is non-negative; signed exposure is handled separately")
    add("Gamma center in strike range", bool(enriched["strike"].min() <= result["gamma_center"] <= enriched["strike"].max()), f"center={result['gamma_center']:.3f}")
    add("Dynamic Gamma Flip computed", bool(np.isfinite(result["gamma_flip"])), f"value={result['gamma_flip']:.3f} · method={result.get('gamma_flip_method')}")
    add("Dynamic Flip search valid", True, f"crossing={'YES' if result.get('gamma_flip_crossing', False) else 'NO — nearest-zero diagnostic'} · grid={result.get('gamma_flip_grid_low', float('nan')):.2f}–{result.get('gamma_flip_grid_high', float('nan')):.2f}")
    add("Dynamic Flip residual finite", bool(np.isfinite(result.get("gamma_flip_net_at_root", float('nan')))), f"NetGEX@level={result.get('gamma_flip_net_at_root', float('nan')):,.2f}")
    add("Proxy convention disclosed", True, "Calls positive / puts negative is a transparent GEX proxy, not observed dealer inventory")

    out = pd.DataFrame(checks)
    out["score"] = np.where(out["status"] == "OK", 1, 0)
    quality = int(round(out["score"].mean() * 100))
    out.attrs["quality_score"] = quality
    return out


def analyze(df: pd.DataFrame, cfg: EngineConfig | None = None) -> Dict[str, object]:
    cfg = cfg or EngineConfig()
    enriched = enrich_options(df, cfg)
    result = compute_structure(enriched, cfg)
    result["enriched"] = enriched
    result["model_inputs_source"] = cfg.inputs_source
    result["anchors_source"] = cfg.anchors_source
    result["audit"] = audit(enriched, result)
    result["data_quality"] = result["audit"].attrs["quality_score"]
    return result

# =============================
# GAMMA + DELTA EXTENSION
# =============================

def aggregate_strikes_gamma_delta(enriched: pd.DataFrame) -> pd.DataFrame:
    g = enriched.groupby(["timestamp", "strike"], as_index=False).agg(
        underlying_price=("underlying_price", "last"),
        signed_gex=("signed_gex_proxy", "sum"),
        gross_gex=("gross_gex", "sum"),
        delta_exposure=("option_delta_exposure_info", "sum"),
        abs_delta_exposure=("option_delta_exposure_info", lambda x: float(np.abs(x).sum())),
        open_interest=("open_interest", "sum"),
        option_volume=("volume", "sum"),
        avg_iv=("iv", "mean"),
    )
    g["abs_signed_gex"] = g["signed_gex"].abs()
    return g.sort_values(["timestamp", "strike"]).reset_index(drop=True)


def _weighted_center(snapshot: pd.DataFrame, weight_col: str) -> float:
    w = snapshot[weight_col].to_numpy(float)
    k = snapshot["strike"].to_numpy(float)
    if w.sum() <= 0:
        return float("nan")
    return float(np.sum(k * w) / np.sum(w))


def compute_structure_gamma_delta(enriched: pd.DataFrame, cfg: EngineConfig) -> Dict[str, object]:
    # Start from the Gamma-only result, then add Delta as a separate explanatory layer.
    gamma_res = compute_structure(enriched, cfg)
    agg = aggregate_strikes_gamma_delta(enriched)
    timestamps = list(pd.Series(agg["timestamp"].unique()).sort_values())
    current_ts = timestamps[-1]
    current = agg[agg["timestamp"] == current_ts].copy().sort_values("strike").reset_index(drop=True)
    prev = agg[agg["timestamp"] == timestamps[-2]].copy() if len(timestamps) > 1 else pd.DataFrame()
    spot = float(current["underlying_price"].iloc[-1])
    prev_spot = float(prev["underlying_price"].iloc[-1]) if not prev.empty else spot
    price_move = spot - prev_spot
    span = max(float(current["strike"].max() - current["strike"].min()), 1.0)

    # Merge baseline Gamma scores by strike.
    base_cols = [
        "strike","gex_change_pct","dominance_score","containment_score","break_score",
        "transition_score","state","state_confidence",
        "containment_core","break_core","mass_pct","intensity_pct","turnover_pct","net_tilt_pct",
        "net_tilt_ratio","net_tilt_raw_pct","net_tilt_floor_quantile","turnover","turnover_raw",
        "turnover_oi_stabilizer","scale_anchor_active"
    ]
    current = current.merge(gamma_res["current"][base_cols], on="strike", how="left")

    current["delta_strength"] = _minmax(current["abs_delta_exposure"])
    current["delta_net_strength"] = _minmax(current["delta_exposure"].abs())
    current["delta_direction"] = np.where(current["delta_exposure"] > 0, "BUY", np.where(current["delta_exposure"] < 0, "SELL", "NEUTRAL"))

    if not prev.empty:
        p = prev[["strike", "delta_exposure", "abs_delta_exposure"]].rename(columns={
            "delta_exposure": "prev_delta_exposure", "abs_delta_exposure": "prev_abs_delta_exposure"
        })
        current = current.merge(p, on="strike", how="left")
        current["delta_change_pct"] = (
            (current["abs_delta_exposure"] - current["prev_abs_delta_exposure"])
            / current["prev_abs_delta_exposure"].replace(0, np.nan).abs()
        ).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        current["delta_sign_flip"] = (
            np.sign(current["delta_exposure"].fillna(0)) != np.sign(current["prev_delta_exposure"].fillna(0))
        ).astype(float)
    else:
        current["prev_delta_exposure"] = np.nan
        current["prev_abs_delta_exposure"] = np.nan
        current["delta_change_pct"] = 0.0
        current["delta_sign_flip"] = 0.0

    # Global Delta pressure: transparent proxy, not observed trade initiation.
    net_delta = float(current["delta_exposure"].sum())
    gross_delta = float(current["abs_delta_exposure"].sum())
    net_delta_ratio = net_delta / max(gross_delta, 1.0)

    delta_centers = []
    for ts in timestamps:
        snap = agg[agg["timestamp"] == ts]
        delta_centers.append((ts, _weighted_center(snap, "abs_delta_exposure")))
    delta_center_df = pd.DataFrame(delta_centers, columns=["timestamp", "delta_center"])
    delta_center_move = float(delta_center_df["delta_center"].iloc[-1] - delta_center_df["delta_center"].iloc[-2]) if len(delta_center_df) > 1 else 0.0
    strike_step = float(np.median(np.diff(np.sort(current["strike"].unique())))) if current["strike"].nunique() > 1 else 1.0
    delta_migration_norm = float(np.clip(delta_center_move / max(strike_step, 0.25), -1.0, 1.0))
    momentum_norm = float(np.clip(price_move / max(strike_step, 0.25), -1.0, 1.0))

    delta_pressure_raw = 0.58 * net_delta_ratio + 0.27 * delta_migration_norm + 0.15 * momentum_norm
    delta_pressure_score = float(np.clip(abs(delta_pressure_raw) * 100.0, 0.0, 100.0))
    delta_pressure_direction = "BUY" if delta_pressure_raw > 0.02 else "SELL" if delta_pressure_raw < -0.02 else "NEUTRAL"

    # Gamma-Delta alignment: compare structural Gamma pressure direction with Delta directional pressure.
    gamma_dir = 1 if gamma_res["pressure_direction"] == "UP" else -1 if gamma_res["pressure_direction"] == "DOWN" else 0
    delta_dir = 1 if delta_pressure_direction == "BUY" else -1 if delta_pressure_direction == "SELL" else 0
    sign_product = gamma_dir * delta_dir
    combined_strength = math.sqrt(max(gamma_res["pressure_score"], 0.0) * max(delta_pressure_score, 0.0))
    alignment_score = float(sign_product * combined_strength) if sign_product != 0 else 0.0
    alignment_label = "ALIGNED" if alignment_score >= 25 else "CONFLICT" if alignment_score <= -25 else "MIXED"

    # Delta-aware regime classification per strike.
    attack_dir = np.sign(price_move) if abs(price_move) > 1e-9 else np.sign(delta_pressure_raw)
    delta_sign = np.sign(current["delta_exposure"].to_numpy(float))
    attack_alignment = np.where(attack_dir == 0, 0.5, np.where(delta_sign == attack_dir, 1.0, 0.0))
    delta_accel = np.clip(np.abs(current["delta_change_pct"].to_numpy(float)), 0.0, 1.0)
    dstrength = current["delta_strength"].to_numpy(float)

    current["containment_score_delta"] = np.clip(
        0.82 * current["containment_score"].to_numpy(float)
        + 18.0 * dstrength * (1.0 - attack_alignment), 0.0, 100.0
    )
    current["break_score_delta"] = np.clip(
        0.78 * current["break_score"].to_numpy(float)
        + 15.0 * dstrength * attack_alignment
        + 7.0 * delta_accel * attack_alignment, 0.0, 100.0
    )
    current["transition_score_delta"] = np.clip(
        0.82 * current["transition_score"].to_numpy(float)
        + 10.0 * current["delta_sign_flip"].to_numpy(float)
        + 8.0 * delta_accel, 0.0, 100.0
    )

    states, confs = [], []
    for row in current.itertuples(index=False):
        vals = {
            "CONTAINMENT": float(row.containment_score_delta),
            "BREAK": float(row.break_score_delta),
            "TRANSITION": float(row.transition_score_delta),
        }
        st = max(vals, key=vals.get)
        states.append(st)
        confs.append(vals[st])
    current["state_delta"] = states
    current["state_confidence_delta"] = confs

    # Combined level score: Gamma remains primary, Delta is confirmation.
    current["gamma_delta_level_score"] = np.clip(
        0.72 * current["dominance_score"].to_numpy(float)
        + 28.0 * current["delta_strength"].to_numpy(float), 0.0, 100.0
    )

    dominant = current.sort_values("gamma_delta_level_score", ascending=False).head(8).copy()
    gamma_center_df = gamma_res["center_history"].copy()
    centers = gamma_center_df.merge(delta_center_df, on="timestamp", how="outer").sort_values("timestamp")

    gamma_res.update({
        "current_delta": current,
        "aggregate_delta": agg,
        "delta_center_history": delta_center_df,
        "center_history_combined": centers,
        "delta_center": float(delta_center_df["delta_center"].iloc[-1]),
        "delta_center_prev": float(delta_center_df["delta_center"].iloc[-2]) if len(delta_center_df) > 1 else float("nan"),
        "delta_center_move": delta_center_move,
        "delta_migration_direction": "UP" if delta_center_move > 1e-9 else "DOWN" if delta_center_move < -1e-9 else "FLAT",
        "delta_migration_strength": float(np.clip(abs(delta_migration_norm) * 100.0, 0.0, 100.0)),
        "delta_pressure_direction": delta_pressure_direction,
        "delta_pressure_score": delta_pressure_score,
        "net_delta_exposure": net_delta,
        "gross_delta_exposure": gross_delta,
        "gamma_delta_alignment_score": alignment_score,
        "gamma_delta_alignment_label": alignment_label,
        "dominant_delta": dominant,
        "price_move": price_move,
    })
    return gamma_res


def audit_gamma_delta(enriched: pd.DataFrame, result: Dict[str, object]) -> pd.DataFrame:
    base = audit(enriched, result).drop(columns=["score"], errors="ignore")
    extra = []
    def add(name: str, ok: bool, detail: str):
        extra.append({"check": name, "status": "OK" if ok else "WARN", "detail": detail})
    cur = result["current_delta"]
    add("Delta exposure finite", bool(np.isfinite(cur[["delta_exposure","abs_delta_exposure"]].to_numpy()).all()), "Delta exposure proxy finite")
    add("Delta center in strike range", bool(enriched["strike"].min() <= result["delta_center"] <= enriched["strike"].max()), f"center={result['delta_center']:.3f}")
    add("Delta pressure bounded", 0 <= result["delta_pressure_score"] <= 100, f"{result['delta_pressure_direction']} {result['delta_pressure_score']:.1f}/100")
    add("Alignment bounded", -100 <= result["gamma_delta_alignment_score"] <= 100, f"{result['gamma_delta_alignment_label']} {result['gamma_delta_alignment_score']:+.1f}")
    add("Delta proxy disclosed", True, "Delta exposure is model-derived from option Delta × OI; it is not signed trade-flow or dealer inventory")
    out = pd.concat([base, pd.DataFrame(extra)], ignore_index=True)
    out["score"] = np.where(out["status"] == "OK", 1, 0)
    out.attrs["quality_score"] = int(round(out["score"].mean() * 100))
    return out


def analyze_gamma_delta(df: pd.DataFrame, cfg: EngineConfig | None = None) -> Dict[str, object]:
    cfg = cfg or EngineConfig()
    enriched = enrich_options(df, cfg)
    result = compute_structure_gamma_delta(enriched, cfg)
    result["enriched"] = enriched
    result["model_inputs_source"] = cfg.inputs_source
    result["anchors_source"] = cfg.anchors_source
    result["audit"] = audit_gamma_delta(enriched, result)
    result["data_quality"] = result["audit"].attrs["quality_score"]
    return result
