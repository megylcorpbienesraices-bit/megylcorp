"""Low-latency TRACE profile repricing.

This module makes Gamma/Delta visually responsive between slower structural option
snapshots without pretending that OI, IV or official option volume update on every
underlying trade.  Only spot and remaining time are repriced continuously; OPRA
WebSocket contracts are shown as a separate observed-activity layer.
"""

from __future__ import annotations

from typing import Any, Dict
import math
import numpy as np
import pandas as pd
from .frame_guards import numeric_column

from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
from .expiry_clock import year_fraction_array, dte_days_from_expiry


def _safe_float(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _latest_snapshot(enriched: pd.DataFrame) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    if enriched is None or enriched.empty:
        return pd.DataFrame(), None
    x = enriched.copy()
    x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
    x = x.dropna(subset=["timestamp"])
    if x.empty:
        return pd.DataFrame(), None
    ts = x["timestamp"].max()
    return x[x["timestamp"] == ts].copy(), pd.Timestamp(ts)


def build_trace_pulse(result: Dict[str, Any] | None, symbol: str, live_spot: float | None,
                      option_events: pd.DataFrame | None = None, asof: Any | None = None,
                      visual_window: float = 12.0, contract_multiplier: float = 100.0) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {"ready": False, "reason": "NO_STRUCTURE"}
    enriched = result.get("enriched")
    if not isinstance(enriched, pd.DataFrame) or enriched.empty:
        return {"ready": False, "reason": "NO_CHAIN"}

    snap, snapshot_ts = _latest_snapshot(enriched)
    if snap.empty or snapshot_ts is None:
        return {"ready": False, "reason": "NO_SNAPSHOT"}

    base_spot_series = numeric_column(snap,"underlying_price",float("nan")).dropna()
    if base_spot_series.empty:
        return {"ready": False, "reason": "NO_SPOT"}
    base_spot = float(base_spot_series.iloc[-1])
    spot = _safe_float(live_spot, base_spot) or base_spot

    now = pd.Timestamp(asof) if asof is not None else snapshot_ts
    # Keep an aware NY clock for actual expiry reconstruction before normalizing the
    # arithmetic clock to Ecuador-local-naive (the historical convention in TRACE).
    # Naive market timestamps in ITM QUANT are Ecuador-local.
    _snapshot_clock = pd.Timestamp(snapshot_ts)
    if _snapshot_clock.tzinfo is None:
        _snapshot_ny = _snapshot_clock.tz_localize("America/Guayaquil").tz_convert("America/New_York")
    else:
        _snapshot_ny = _snapshot_clock.tz_convert("America/New_York")
    if now.tzinfo is not None:
        now = now.tz_convert("America/Guayaquil").tz_localize(None)
    if snapshot_ts.tzinfo is not None:
        snapshot_ts = snapshot_ts.tz_convert("America/Guayaquil").tz_localize(None)
    elapsed_days = max(0.0, (now - snapshot_ts).total_seconds() / 86400.0)

    K = numeric_column(snap,"strike",float("nan")).to_numpy(float)
    iv = numeric_column(snap,"iv",float("nan")).to_numpy(float)
    # copy=True es obligatorio: NumPy >= 2 devuelve una vista de solo lectura cuando
    # no necesita convertir, y la reconstrucción de 0DTE de más abajo escribe en dte[i].
    dte = numeric_column(snap,"dte",float("nan")).to_numpy(dtype=float, copy=True)
    # 0DTE is valid until the actual settlement instant. Providers commonly report
    # integer dte=0 throughout expiration day; rejecting dte==0 made GEX/DEX disappear
    # from TRACE exactly when intraday traders need them most. Reconstruct remaining
    # ACT/365 days from expiration_date when available, otherwise let the canonical
    # expiry clock apply its sub-second numerical floor.
    if "expiration_date" in snap.columns:
        exp = snap["expiration_date"].astype(str).to_numpy()
        # Reconstruct from the snapshot clock, not the LIVE clock, because elapsed_days
        # is subtracted below. This prevents double-aging 0DTE contracts.
        snapshot_ny_dt = _snapshot_ny.to_pydatetime()
        for i in range(len(dte)):
            if np.isfinite(dte[i]) and dte[i] <= 0 and exp[i] and exp[i].lower() not in {"nan","nat","none"}:
                try: dte[i] = dte_days_from_expiry(exp[i], now=snapshot_ny_dt)
                except Exception: dte[i] = max(float(dte[i]), 0.0)
    oi = numeric_column(snap, "open_interest", 0.0).to_numpy(float)
    volume = pd.to_numeric(snap.get("volume", snap.get("option_volume", 0)), errors="coerce").fillna(0.0).to_numpy(float)
    is_call = snap.get("option_type", pd.Series("call", index=snap.index)).astype(str).str.lower().str.startswith("c").to_numpy()
    valid = np.isfinite(K) & np.isfinite(iv) & np.isfinite(dte) & (K > 0) & (iv > 0) & (dte >= 0)
    if not valid.any():
        return {"ready": False, "reason": "NO_VALID_GREEKS_INPUTS"}

    K = K[valid]; iv = iv[valid]; dte = np.maximum(dte[valid] - elapsed_days, 0.0)
    oi = oi[valid]; volume = volume[valid]; is_call = is_call[valid]
    r, q = model_inputs_vector(symbol, dte)
    S = np.full_like(K, float(spot), dtype=float)
    greeks = _greeks_vector(symbol, S, K, year_fraction_array(dte), iv, is_call, r, q)
    sign = np.where(is_call, 1.0, -1.0)
    gamma_live = sign * greeks["gamma"] * oi * float(contract_multiplier) * (S ** 2) * 0.01
    delta_live = greeks["delta"] * oi * float(contract_multiplier) * S

    # Higher-order local mechanics. These are transparent scenario sensitivities,
    # not observed dealer inventory or a new directional signal.
    vanna_1vol = sign * greeks["vanna"] * 0.01 * oi * float(contract_multiplier) * S
    speed_1pct = sign * greeks["speed"] * (0.01 * S) * oi * float(contract_multiplier) * (S ** 2) * 0.01
    dte10 = np.maximum(dte - 10.0 / 1440.0, 0.0)
    r10, q10 = model_inputs_vector(symbol, dte10)
    g10 = _greeks_vector(symbol, S, K, year_fraction_array(dte10), iv, is_call, r10, q10)
    charm_10m = sign * (g10["delta"] - greeks["delta"]) * oi * float(contract_multiplier) * S
    gamma_10m = sign * g10["gamma"] * oi * float(contract_multiplier) * (S ** 2) * 0.01
    color_10m = gamma_10m - gamma_live

    raw = snap.loc[snap.index[valid]].copy()
    raw["gamma_live"] = gamma_live
    raw["delta_live"] = delta_live
    raw["vanna_1vol"] = vanna_1vol
    raw["charm_10m"] = charm_10m
    raw["speed_1pct"] = speed_1pct
    raw["color_10m"] = color_10m
    raw["volume_snapshot"] = volume
    raw["oi_snapshot"] = oi
    # Universal per-strike call/put decomposition for presentation controls.
    # This is derived from the same chain snapshot for every supported underlying;
    # it does not create a DIA-specific path or alter Scanner authority.
    raw["call_oi_snapshot"] = np.where(is_call, oi, 0.0)
    raw["put_oi_snapshot"] = np.where(is_call, 0.0, oi)
    raw["call_volume_snapshot"] = np.where(is_call, volume, 0.0)
    raw["put_volume_snapshot"] = np.where(is_call, 0.0, volume)
    # SpotGamma-style Gamma/Delta Model split: call-side and put-side exposure kept
    # separate (never netted) so the strike ladder can show both bars, not just the
    # net. gamma_live/delta_live already carry the correct sign per side (calls
    # positive, puts negative), so the split below is a pure decomposition -- no new
    # math, same convention as signed_gex_proxy everywhere else in the engine.
    raw["call_gamma_snapshot"] = np.where(is_call, gamma_live, 0.0)
    raw["put_gamma_snapshot"] = np.where(is_call, 0.0, gamma_live)
    raw["call_delta_snapshot"] = np.where(is_call, delta_live, 0.0)
    raw["put_delta_snapshot"] = np.where(is_call, 0.0, delta_live)
    raw["gamma_base"] = numeric_column(raw, "signed_gex_proxy", 0.0)
    raw["delta_base"] = numeric_column(raw, "option_delta_exposure_info", 0.0)

    agg = raw.groupby("strike", as_index=False).agg(
        gamma=("gamma_live", "sum"), delta=("delta_live", "sum"),
        vanna_1vol=("vanna_1vol", "sum"), charm_10m=("charm_10m", "sum"),
        speed_1pct=("speed_1pct", "sum"), color_10m=("color_10m", "sum"),
        gamma_base=("gamma_base", "sum"), delta_base=("delta_base", "sum"),
        oi=("oi_snapshot", "sum"), volume_snapshot=("volume_snapshot", "sum"),
        call_oi=("call_oi_snapshot", "sum"), put_oi=("put_oi_snapshot", "sum"),
        call_volume=("call_volume_snapshot", "sum"), put_volume=("put_volume_snapshot", "sum"),
        call_gamma=("call_gamma_snapshot", "sum"), put_gamma=("put_gamma_snapshot", "sum"),
        call_delta=("call_delta_snapshot", "sum"), put_delta=("put_delta_snapshot", "sum"),
    )
    agg["net_oi"] = agg["call_oi"] - agg["put_oi"]
    agg["net_volume"] = agg["call_volume"] - agg["put_volume"]

    # Separate observed OPRA trade activity.  Do not add it to official daily volume:
    # the latest snapshot may already include part of those trades.
    observed = pd.DataFrame(columns=["strike", "opra_contracts_5m", "opra_premium_5m",
                                     "opra_net_contracts_5m", "opra_directional_premium_5m"])
    if isinstance(option_events, pd.DataFrame) and not option_events.empty:
        ev = option_events.copy()
        ev["timestamp"] = pd.to_datetime(ev.get("timestamp"), errors="coerce")
        ev = ev.dropna(subset=["timestamp"])
        if not ev.empty:
            cutoff = now - pd.Timedelta(minutes=5)
            ev = ev[ev["timestamp"] >= cutoff]
            if "underlying_symbol" in ev.columns:
                ev = ev[ev["underlying_symbol"].astype(str).str.upper() == str(symbol).upper()]
            if not ev.empty:
                ev["strike"] = numeric_column(ev,"strike",float("nan"))
                ev["contracts"] = numeric_column(ev,"contracts",0.0)
                ev["premium"] = numeric_column(ev,"premium",0.0)
                ev["direction_sign"] = pd.to_numeric(ev.get("direction_sign", pd.Series(0.0,index=ev.index)), errors="coerce").fillna(0.0).clip(-1, 1)
                ev["directional_premium"] = pd.to_numeric(ev.get("directional_premium", ev["direction_sign"] * ev["premium"]), errors="coerce").fillna(0.0)
                ev["signed_contracts"] = ev["direction_sign"] * ev["contracts"]
                observed = ev.dropna(subset=["strike"]).groupby("strike", as_index=False).agg(
                    opra_contracts_5m=("contracts", "sum"), opra_premium_5m=("premium", "sum"),
                    opra_net_contracts_5m=("signed_contracts", "sum"),
                    opra_directional_premium_5m=("directional_premium", "sum"),
                )
    agg = agg.merge(observed, on="strike", how="left")
    agg["opra_contracts_5m"] = pd.to_numeric(agg.get("opra_contracts_5m", 0), errors="coerce").fillna(0.0)
    agg["opra_premium_5m"] = pd.to_numeric(agg.get("opra_premium_5m", 0), errors="coerce").fillna(0.0)
    agg["opra_net_contracts_5m"] = pd.to_numeric(agg.get("opra_net_contracts_5m", 0), errors="coerce").fillna(0.0)
    agg["opra_directional_premium_5m"] = pd.to_numeric(agg.get("opra_directional_premium_5m", 0), errors="coerce").fillna(0.0)
    agg["turnover_snapshot"] = agg["volume_snapshot"] / np.maximum(agg["oi"], 1.0)
    # Liquidity is displayed separately from exposure. It controls profile emphasis,
    # never the sign/direction of Gamma or Delta. Log scaling keeps one giant strike
    # from visually crushing the rest of the ladder.
    _liq_raw=np.log1p(np.maximum(agg["oi"].to_numpy(float),0.0) + 2.0*np.maximum(agg["volume_snapshot"].to_numpy(float),0.0) + 4.0*np.maximum(agg["opra_contracts_5m"].to_numpy(float),0.0))
    _lo=float(np.nanpercentile(_liq_raw,10)) if len(_liq_raw) else 0.0; _hi=float(np.nanpercentile(_liq_raw,90)) if len(_liq_raw) else 1.0
    if not math.isfinite(_hi) or _hi<=_lo+1e-12: agg["liquidity_score"]=50.0
    else: agg["liquidity_score"]=100.0*np.clip((_liq_raw-_lo)/(_hi-_lo),0.0,1.0)
    agg["gamma_change"] = agg["gamma"] - agg["gamma_base"]
    agg["delta_change"] = agg["delta"] - agg["delta_base"]

    w = max(float(visual_window or 12.0), 3.0)
    agg = agg[(agg["strike"] >= spot - w) & (agg["strike"] <= spot + w)].copy()
    agg = agg.sort_values("strike").reset_index(drop=True)
    if agg.empty:
        return {"ready": False, "reason": "NO_STRIKES_IN_WINDOW"}

    # TRACE v1.24.2 · Gamma+Delta interaction is descriptive only.  It measures
    # whether the two exposure fields at the SAME strike are coherent/opposed and
    # how concentrated that interaction is.  It never creates or flips Scanner
    # direction and it never re-labels OPRA activity as exposure.
    _g = pd.to_numeric(agg["gamma"], errors="coerce").fillna(0.0).to_numpy(float)
    _d = pd.to_numeric(agg["delta"], errors="coerce").fillna(0.0).to_numpy(float)
    _joint = np.sqrt(np.abs(_g * _d))
    _joint_pos = _joint[np.isfinite(_joint) & (_joint > 0)]
    _joint_scale = float(np.nanpercentile(_joint_pos, 90)) if len(_joint_pos) else 1.0
    if not math.isfinite(_joint_scale) or _joint_scale <= 1e-12:
        _joint_scale = max(float(np.nanmax(_joint)) if len(_joint) else 0.0, 1.0)
    agg["gamma_delta_joint_score"] = 100.0 * np.clip(_joint / _joint_scale, 0.0, 1.0)

    _eps_g = max(float(np.nanmax(np.abs(_g))) * 1e-8 if len(_g) else 0.0, 1e-12)
    _eps_d = max(float(np.nanmax(np.abs(_d))) * 1e-8 if len(_d) else 0.0, 1e-12)
    _states: list[str] = []
    for gv, dv in zip(_g, _d):
        if abs(gv) <= _eps_g or abs(dv) <= _eps_d:
            _states.append("NEUTRAL")
        elif gv * dv < 0:
            _states.append("OPPOSED")
        elif gv > 0 and dv > 0:
            _states.append("SAME_SIGN_POS")
        else:
            _states.append("SAME_SIGN_NEG")
    agg["gamma_delta_state"] = _states

    # TRACE v1.24.6 · STRIKE GRAVITY. This is a deterministic relevance score,
    # not a claim that price must travel to a strike. It combines current
    # structural concentration, same-strike Gamma+Delta interaction, observed
    # OPRA activity, liquidity and distance from spot. No random particles and
    # no directional authority are introduced here.
    def _robust_unit(values: np.ndarray) -> np.ndarray:
        a = np.abs(np.asarray(values, dtype=float))
        good = a[np.isfinite(a) & (a > 0)]
        scale = float(np.nanpercentile(good, 90)) if len(good) else 1.0
        if not math.isfinite(scale) or scale <= 1e-12:
            scale = max(float(np.nanmax(a)) if len(a) else 0.0, 1.0)
        return np.clip(a / scale, 0.0, 1.0)

    _g_strength = _robust_unit(_g)
    _d_strength = _robust_unit(_d)
    _opra_strength = _robust_unit(pd.to_numeric(agg.get("opra_contracts_5m", 0), errors="coerce").fillna(0.0).to_numpy(float))
    _liq_strength = np.clip(numeric_column(agg,"liquidity_score",50.0).to_numpy(float) / 100.0, 0.0, 1.0)
    _joint_strength = np.clip(numeric_column(agg,"gamma_delta_joint_score",0.0).to_numpy(float) / 100.0, 0.0, 1.0)
    _distance_scale = max(float(visual_window or 12.0) * 0.35, 0.50)
    _distance_strength = np.exp(-np.abs(pd.to_numeric(agg["strike"], errors="coerce").to_numpy(float) - float(spot)) / _distance_scale)
    _gravity = 100.0 * np.clip(
        0.28 * _g_strength +
        0.22 * _d_strength +
        0.18 * _liq_strength +
        0.16 * _joint_strength +
        0.10 * _opra_strength +
        0.06 * _distance_strength,
        0.0, 1.0
    )
    agg["gravity_score"] = _gravity

    _joint_total = float(np.nansum(_joint))
    _coherence_index = (
        100.0 * float(np.nansum(np.sign(_g * _d) * _joint)) / _joint_total
        if _joint_total > 1e-12 else 0.0
    )
    _coherence_index = float(np.clip(_coherence_index, -100.0, 100.0))
    _interaction_label = "COHERENT" if _coherence_index >= 25.0 else "OPPOSED" if _coherence_index <= -25.0 else "MIXED"
    _liq = numeric_column(agg,"liquidity_score",50.0).to_numpy(float) / 100.0
    _interaction_weight = _joint * (0.65 + 0.35 * np.clip(_liq, 0.0, 1.0))
    _active_i = int(np.nanargmax(_interaction_weight)) if len(_interaction_weight) and np.isfinite(_interaction_weight).any() else 0
    _active_row = agg.iloc[_active_i]
    _interaction_summary = {
        "label": _interaction_label,
        "coherence_index": round(_coherence_index, 1),
        "active_strike": float(_active_row["strike"]),
        "active_state": str(_active_row["gamma_delta_state"]),
        "active_score": round(float(_active_row["gamma_delta_joint_score"]), 1),
        "authority": "DESCRIPTIVE_ONLY_SCANNER_REMAINS_AUTHORITY",
    }

    def center(col: str) -> float | None:
        weights = pd.to_numeric(agg[col], errors="coerce").abs().fillna(0.0).to_numpy(float)
        strikes = pd.to_numeric(agg["strike"], errors="coerce").to_numpy(float)
        total = float(weights.sum())
        return float(np.sum(strikes * weights) / total) if total > 0 else None

    net_g = float(agg["gamma"].sum())
    net_d = float(agg["delta"].sum())
    base_g = float(agg["gamma_base"].sum())
    base_d = float(agg["delta_base"].sum())
    snapshot_age = max(0.0, (now - snapshot_ts).total_seconds())

    rows = []
    for r0 in agg.to_dict("records"):
        rows.append({
            "strike": float(r0["strike"]),
            "gamma_m": float(r0["gamma"]) / 1e6,
            "delta_m": float(r0["delta"]) / 1e6,
            "gamma_base_m": float(r0["gamma_base"]) / 1e6,
            "delta_base_m": float(r0["delta_base"]) / 1e6,
            "gamma_change_m": float(r0["gamma_change"]) / 1e6,
            "delta_change_m": float(r0["delta_change"]) / 1e6,
            "vanna_1vol_m": float(r0["vanna_1vol"]) / 1e6,
            "charm_10m_m": float(r0["charm_10m"]) / 1e6,
            "speed_1pct_m": float(r0["speed_1pct"]) / 1e6,
            "color_10m_m": float(r0["color_10m"]) / 1e6,
            "oi": float(r0["oi"]),
            "call_oi": float(r0.get("call_oi", 0.0)),
            "put_oi": float(r0.get("put_oi", 0.0)),
            "net_oi": float(r0.get("net_oi", 0.0)),
            # SpotGamma-style split: call/put kept apart, never netted. Sign already
            # correct (calls positive, puts negative) -- same convention as gamma_m/delta_m.
            "call_gamma_m": float(r0.get("call_gamma", 0.0)) / 1e6,
            "put_gamma_m": float(r0.get("put_gamma", 0.0)) / 1e6,
            "call_delta_m": float(r0.get("call_delta", 0.0)) / 1e6,
            "put_delta_m": float(r0.get("put_delta", 0.0)) / 1e6,
            "volume_snapshot": float(r0["volume_snapshot"]),
            "call_volume": float(r0.get("call_volume", 0.0)),
            "put_volume": float(r0.get("put_volume", 0.0)),
            "net_volume": float(r0.get("net_volume", 0.0)),
            "turnover_snapshot": float(r0["turnover_snapshot"]),
            "liquidity_score": float(r0.get("liquidity_score",50.0)),
            "opra_contracts_5m": float(r0["opra_contracts_5m"]),
            "opra_premium_5m": float(r0["opra_premium_5m"]),
            "opra_net_contracts_5m": float(r0["opra_net_contracts_5m"]),
            "opra_directional_premium_5m": float(r0["opra_directional_premium_5m"]),
            "gamma_delta_joint_score": float(r0["gamma_delta_joint_score"]),
            "gamma_delta_state": str(r0["gamma_delta_state"]),
            "gravity_score": float(r0.get("gravity_score", 0.0)),
        })

    _grav_i = int(np.nanargmax(_gravity)) if len(_gravity) and np.isfinite(_gravity).any() else 0
    _grav_row = agg.iloc[_grav_i]
    _gravity_summary = {
        "active_strike": float(_grav_row["strike"]),
        "score": round(float(_grav_row.get("gravity_score", 0.0)), 1),
        "state": "ACTIVE" if float(_grav_row.get("gravity_score", 0.0)) >= 55.0 else "WATCH",
        "meaning": "STRUCTURAL_RELEVANCE_NOT_PRICE_PREDICTION",
        "authority": "DESCRIPTIVE_ONLY_SCANNER_REMAINS_AUTHORITY",
    }

    return {
        "ready": True,
        "symbol": str(symbol).upper(),
        "spot": float(spot),
        "snapshot_spot": float(base_spot),
        "snapshot_time": snapshot_ts.isoformat(),
        "asof": now.isoformat(),
        "snapshot_age_seconds": round(snapshot_age, 1),
        "source": "LIVE_SPOT_REPRICE" if abs(float(spot) - float(base_spot)) > 1e-10 or elapsed_days > 0 else "STRUCTURAL_SNAPSHOT",
        "assumptions": "Spot/time repriced LIVE; IV/OI and official option volume remain frozen until next structural snapshot. OPRA 5m activity is observed separately. Vanna/Charm/Speed/Color are local scenario sensitivities, not observed dealer intent.",
        "gamma_net_m": net_g / 1e6,
        "delta_net_m": net_d / 1e6,
        "gamma_net_change_m": (net_g - base_g) / 1e6,
        "delta_net_change_m": (net_d - base_d) / 1e6,
        "vanna_1vol_m": float(agg["vanna_1vol"].sum()) / 1e6,
        "charm_10m_m": float(agg["charm_10m"].sum()) / 1e6,
        "speed_1pct_m": float(agg["speed_1pct"].sum()) / 1e6,
        "color_10m_m": float(agg["color_10m"].sum()) / 1e6,
        "gamma_center": center("gamma"),
        "delta_center": center("delta"),
        "opra_contracts_5m": float(agg["opra_contracts_5m"].sum()),
        "opra_premium_5m": float(agg["opra_premium_5m"].sum()),
        "opra_net_contracts_5m": float(agg["opra_net_contracts_5m"].sum()),
        "opra_directional_premium_5m": float(agg["opra_directional_premium_5m"].sum()),
        "gamma_delta_interaction": _interaction_summary,
        "strike_gravity": _gravity_summary,
        "rows": rows,
    }
