from __future__ import annotations

from .instruments import sigma_horizon as instrument_sigma_horizon

import math
import os
from datetime import datetime
from typing import Any, Dict, Optional
from pathlib import Path

import json

import numpy as np
import pandas as pd
from app.persistence import routed_dir
import plotly.graph_objects as go
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .atomic_store import append_row

BUY = "#27c7f4"
SELL = "#e44ed8"
GOLD = "#f1c84c"
GREEN = "#31d784"
RED = "#ff5f73"
BLUE = "#3c78ff"
MUTED = "#8296ac"
WHITE = "#f2f6fb"
BG = "#08111f"
GRID = "#1c2a3a"


def _finite(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _maybe(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _pct_rank(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if len(x) <= 1:
        return pd.Series(np.zeros(len(x)), index=x.index, dtype=float)
    # Percentile rank is robust to extreme SPY/QQQ values and easier to compare across assets.
    return (x.rank(method="average", pct=True) * 100.0).clip(0, 100)


def _dir_sign(label: str) -> int:
    s = str(label or "").upper()
    if s in {"BUY", "UP", "BULLISH", "ALIGNED BULLISH"}:
        return 1
    if s in {"SELL", "DOWN", "BEARISH", "ALIGNED BEARISH"}:
        return -1
    return 0


def _price_step(cur: pd.DataFrame) -> float:
    vals = np.sort(numeric_column(cur,"strike",float("nan")).dropna().unique())
    if len(vals) > 1:
        d = np.diff(vals)
        d = d[d > 1e-9]
        if len(d):
            return float(np.median(d))
    return 1.0


def _latest_flow_direction(flow: Dict[str, Any]) -> tuple[int, float]:
    return _dir_sign(flow.get("regime")), float(np.clip(_finite(flow.get("confidence"), 0), 0, 100))


def _fuse_signal_channel(internal_sign: int, internal_conf: float, provider_features: Optional[Dict[str, Any]], channel: str) -> tuple[int, float, Dict[str, Any]]:
    """Fuse model/channel evidence with provider observations without provider ranking.

    The semantic channel (Gamma, Delta or Flow) is the unit of fusion. A provider is
    never granted a global fixed weight. Agreement preserves confidence; disagreement
    reduces it instead of blindly letting either source override the other.
    """
    channels = (provider_features or {}).get("channels") or {}
    ext = channels.get(channel) if isinstance(channels, dict) else None
    ext = ext if isinstance(ext, dict) else {}
    try:
        ext_sign = int(ext.get("sign") or 0)
    except Exception:
        ext_sign = 0
    ext_sign = 1 if ext_sign > 0 else -1 if ext_sign < 0 else 0
    ext_conf = float(np.clip(_finite(ext.get("confidence"), 0), 0, 100))
    in_sign = 1 if internal_sign > 0 else -1 if internal_sign < 0 else 0
    in_conf = float(np.clip(_finite(internal_conf, 0), 0, 100))
    parts = []
    if in_sign and in_conf > 0:
        parts.append((in_sign, in_conf, "ITM_MODEL"))
    if ext_sign and ext_conf > 0:
        parts.append((ext_sign, ext_conf, "PROVIDER_FEATURE_FUSION"))
    if not parts:
        return 0, 0.0, {"channel": channel, "contributors": ext.get("contributors") or [], "agreement": "NO_DATA"}
    signed = sum(sign * conf for sign, conf, _ in parts)
    total = sum(conf for _, conf, _ in parts)
    ratio = signed / max(total, 1e-9)
    sign = 1 if ratio > 0.08 else -1 if ratio < -0.08 else 0
    # Confidence measures coherent directional evidence, not a probability.
    conf = float(np.clip(abs(ratio) * (total / len(parts)), 0, 100))
    agreement = "ALIGNED" if len(parts) > 1 and all(x[0] == parts[0][0] for x in parts) else "MIXED" if len(parts) > 1 else "SINGLE_SOURCE"
    return sign, conf, {
        "channel": channel, "internal_sign": in_sign, "internal_confidence": round(in_conf, 2),
        "provider_sign": ext_sign, "provider_confidence": round(ext_conf, 2),
        "fused_sign": sign, "fused_confidence": round(conf, 2), "agreement": agreement,
        "contributors": ext.get("contributors") or [],
    }


def _nearby_event_bonus(level: float, flow_events: pd.DataFrame, large_prints: pd.DataFrame, radius: float) -> Dict[str, float]:
    out = {"buy_flow": 0.0, "sell_flow": 0.0, "prints": 0.0, "flow_count": 0, "print_count": 0}
    if isinstance(flow_events, pd.DataFrame) and not flow_events.empty:
        e = flow_events.copy()
        px = numeric_column(e,"underlying_price",float("nan"))
        score = numeric_column(e,"flow_score",0)
        sign = numeric_column(e,"direction_sign",0)
        mask = (px - level).abs() <= radius
        if mask.any():
            z = e.loc[mask].copy()
            zs = score.loc[mask]
            zsgn = sign.loc[mask]
            out["buy_flow"] = float(np.clip(zs[zsgn > 0].max() if (zsgn > 0).any() else 0, 0, 100))
            out["sell_flow"] = float(np.clip(zs[zsgn < 0].max() if (zsgn < 0).any() else 0, 0, 100))
            out["flow_count"] = int(len(z))
    if isinstance(large_prints, pd.DataFrame) and not large_prints.empty:
        p = large_prints.copy()
        px = numeric_column(p,"price",float("nan"))
        q = numeric_column(p,"q_print",0)
        mask = (px - level).abs() <= radius
        if mask.any():
            out["prints"] = float(np.clip(q.loc[mask].max(), 0, 100))
            out["print_count"] = int(mask.sum())
    return out


def _special_refs(result: Dict[str, Any], positioning: Dict[str, Any], vol: Dict[str, Any], premarket: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    refs = {
        "Gamma Flip": _maybe(result.get("gamma_flip")),
        "Gamma Center": _maybe(result.get("gamma_center")),
        "Delta Center": _maybe(result.get("delta_center")),
        "Call OI Wall": _maybe(positioning.get("top_call_oi_strike")),
        "Put OI Wall": _maybe(positioning.get("top_put_oi_strike")),
        "Expected High": _maybe(vol.get("expected_high")),
        "Expected Low": _maybe(vol.get("expected_low")),
    }
    if isinstance(premarket, dict):
        refs.update({
            "Premarket Low": _maybe(premarket.get("low")),
            "Premarket Pivot": _maybe(premarket.get("pivot")),
            "Premarket High": _maybe(premarket.get("high")),
        })
    return refs


def _label_zone(level: float, refs: Dict[str, Optional[float]], radius: float) -> list[str]:
    names = []
    for name, val in refs.items():
        if val is not None and abs(float(val) - level) <= radius:
            names.append(name)
    return names


def _macro_risk(macro: Dict[str, Any]) -> float:
    """Read macro context without flattening away the official macro payload."""
    m=macro or {}
    for nested in (m.get("asset_context"), m.get("stress")):
        if isinstance(nested,dict):
            for k in ("stress_score","macro_stress","risk_score","score"):
                if k in nested:return float(np.clip(_finite(nested.get(k),0),0,100))
    for k in ("stress_score", "macro_stress", "risk_score", "score"):
        if k in m:return float(np.clip(_finite(m.get(k), 0), 0, 100))
    return 0.0


def _tape_aggression(live_ticks: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if not isinstance(live_ticks, pd.DataFrame) or live_ticks.empty or "signed_volume" not in live_ticks.columns:
        return {"direction": "NEUTRAL", "sign": 0, "score": 0.0, "net_signed_volume": 0.0, "gross_volume": 0.0, "price_momentum": 0.0}
    x = live_ticks.tail(1500).copy()
    sv = numeric_column(x,"signed_volume",0.0)
    sz = numeric_column(x,"size",0.0).abs()
    net = float(sv.sum()); gross = float(sz.sum())
    ratio = net / max(gross, 1.0)
    sign = 1 if ratio > 0.04 else -1 if ratio < -0.04 else 0
    score = float(np.clip(abs(ratio) * 220.0, 0, 100))
    px = numeric_column(x,"price",float("nan")).dropna()
    momentum = float(px.iloc[-1] - px.iloc[0]) if len(px) > 1 else 0.0
    return {"direction": "BUY" if sign > 0 else "SELL" if sign < 0 else "NEUTRAL", "sign": sign, "score": score, "net_signed_volume": net, "gross_volume": gross, "price_momentum": momentum}


def _current_structure_frame(result: Dict[str, Any]) -> pd.DataFrame:
    """Prefer Gamma+Delta current frame, but never let an empty placeholder mask Gamma structure.

    Some live refreshes publish ``current_delta`` before the Delta enrichment has rows.
    The Gamma/current snapshot is still valid structural evidence and is the correct
    fail-soft source for walls/zones until Delta enrichment catches up.
    """
    for key in ("current_delta", "current"):
        frame = result.get(key, pd.DataFrame()) if isinstance(result, dict) else pd.DataFrame()
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            return frame.copy()
    return pd.DataFrame()


def _scenario_zone_table(result: Dict[str, Any], flow: Dict[str, Any], vol: Dict[str, Any], positioning: Dict[str, Any],
                         flow_events: pd.DataFrame, large_prints: pd.DataFrame,
                         premarket: Optional[Dict[str, Any]], macro: Dict[str, Any], live_ticks: Optional[pd.DataFrame] = None,
                         provider_features: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    cur = _current_structure_frame(result)
    if cur.empty:
        return pd.DataFrame()
    spot = _finite(result.get("spot"), np.nan)
    if not math.isfinite(spot):
        return pd.DataFrame()
    step = _price_step(cur)
    radius = max(step * 0.42, spot * 0.00035)
    refs = _special_refs(result, positioning, vol, premarket)

    cur["strike"] = pd.to_numeric(cur["strike"], errors="coerce")
    cur = cur.dropna(subset=["strike"]).copy()
    cur["oi_pct"] = _pct_rank(cur.get("open_interest", 0))
    cur["vol_pct"] = _pct_rank(cur.get("option_volume", 0))
    cur["gamma_pct"] = _pct_rank(pd.to_numeric(cur.get("gross_gex", 0), errors="coerce").abs())
    cur["delta_pct"] = _pct_rank(pd.to_numeric(cur.get("abs_delta_exposure", 0), errors="coerce").abs())
    # v1.14: delta INTENSITY (delta exposure per contract) rather than delta MASS.
    # abs_delta_exposure is delta x OI, so delta_pct was another copy of oi_pct.
    _oi_s = pd.to_numeric(cur.get("open_interest", 0), errors="coerce")
    cur["delta_intensity_pct"] = _pct_rank(
        pd.to_numeric(cur.get("abs_delta_exposure", 0), errors="coerce").abs() / _oi_s.where(_oi_s > 0, np.nan)
    )
    _vol_s = numeric_column(cur,"option_volume",0.0)
    _oi_clean = numeric_column(cur,"open_interest",0.0)
    _positive_oi = _oi_clean[_oi_clean > 0]
    _oi_stabilizer = max(25.0, 0.10 * float(_positive_oi.median()) if len(_positive_oi) else 25.0)
    cur["vol_oi_raw"] = (_vol_s / _oi_clean.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    cur["vol_oi"] = (_vol_s / (_oi_clean + _oi_stabilizer)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    cur["vol_oi_stabilizer"] = _oi_stabilizer
    cur["unusual_pct"] = _pct_rank(cur["vol_oi"])
    cur["persistence_pct"] = (100.0 * (1.0 - numeric_column(cur,"gex_change_pct",0.0).abs().clip(0, 1))).clip(0, 100)
    cur["proximity"] = (100.0 * np.exp(-((cur["strike"] - spot).abs() / max(step * 4.0, 1e-6)))).clip(0, 100)

    flow_sign_raw, flow_conf_raw = _latest_flow_direction(flow)
    gamma_sign_raw = _dir_sign(result.get("pressure_direction"))
    gamma_conf_raw = float(np.clip(_finite(result.get("pressure_score"), 0), 0, 100))
    delta_sign_raw = _dir_sign(result.get("delta_pressure_direction"))
    delta_conf_raw = float(np.clip(_finite(result.get("delta_pressure_score"), 0), 0, 100))
    # v1.25.15: provider equality is implemented inside semantic feature channels,
    # not as a provider popularity vote. external analytics can strengthen or
    # attenuate Gamma/Delta/Flow only when their observations are fresh and valid.
    gamma_sign, gamma_conf, gamma_fusion = _fuse_signal_channel(gamma_sign_raw, gamma_conf_raw, provider_features, "gamma")
    delta_sign, delta_conf, delta_fusion = _fuse_signal_channel(delta_sign_raw, delta_conf_raw, provider_features, "delta")
    flow_sign, flow_conf, flow_fusion = _fuse_signal_channel(flow_sign_raw, flow_conf_raw, provider_features, "flow")
    # v1.25.15: ecosystem is already a cross-instrument normalized feature produced
    # from ETF/equity + index + futures observations. It has no internal raw analogue
    # here and therefore enters as contextual evidence only after normalization.
    ecosystem_sign, ecosystem_conf, ecosystem_fusion = _fuse_signal_channel(0, 0, provider_features, "ecosystem")
    derivative_sign, derivative_conf, derivative_fusion = _fuse_signal_channel(0, 0, provider_features, "derivatives")
    vol_expansion = 100.0 if str(vol.get("regime", "")).upper() == "EXPANSION" else 45.0 if str(vol.get("regime", "")).upper() == "STABLE" else 20.0
    macro_risk = _macro_risk(macro)
    # v1.15.3: TRACE/tape is execution timing ONLY. It must not participate in
    # structural zone scoring or choose/flip the Scanner direction.
    rows = []
    for _, r in cur.iterrows():
        level = float(r["strike"])
        side = -1 if level < spot - step * 0.12 else 1 if level > spot + step * 0.12 else 0
        if side < 0:
            bounce_dir, break_dir = 1, -1       # support: contain BUY / break SELL
        elif side > 0:
            bounce_dir, break_dir = -1, 1       # resistance: contain SELL / break BUY
        else:
            # v1.27.13 · exact/near-spot neutrality is a real state. The former ``or 1``
            # created a hard-coded bullish prior when Gamma/Delta/Flow were all neutral.
            # Scanner may still produce BUY/SELL from other non-neutral zones, but zero
            # directional evidence at the spot itself is never converted into +1.
            chosen = delta_sign or gamma_sign or flow_sign or 0
            bounce_dir = break_dir = chosen
        specials = _label_zone(level, refs, radius)
        special_score = min(100.0, len(specials) * 28.0)
        events = _nearby_event_bonus(level, flow_events, large_prints, radius * 1.35)
        direction_flow_bounce = events["buy_flow"] if bounce_dir > 0 else events["sell_flow"] if bounce_dir < 0 else 0.0
        direction_flow_break = events["buy_flow"] if break_dir > 0 else events["sell_flow"] if break_dir < 0 else 0.0
        # Structural direction is decided before execution timing. The former tape
        # 20% is redistributed proportionally across Gamma/Delta/Q-Flow so score
        # scale remains comparable with prior versions without letting live tape
        # create or flip the thesis.
        if (ecosystem_sign and ecosystem_conf > 0) or (derivative_sign and derivative_conf > 0):
            # Economic-role context and observed derivative structure receive bounded
            # contextual shares. These are semantic-channel weights, never provider ranks.
            # Missing contextual channels contribute zero; all raw instrument math was
            # already kept separate upstream before reaching this normalized layer.
            global_bounce = (gamma_conf if bounce_dir and gamma_sign == bounce_dir else 0) * 0.300 + (delta_conf if bounce_dir and delta_sign == bounce_dir else 0) * 0.320 + (flow_conf if bounce_dir and flow_sign == bounce_dir else 0) * 0.200 + (ecosystem_conf if bounce_dir and ecosystem_sign == bounce_dir else 0) * 0.100 + (derivative_conf if bounce_dir and derivative_sign == bounce_dir else 0) * 0.080
            global_break = (gamma_conf if break_dir and gamma_sign == break_dir else 0) * 0.290 + (delta_conf if break_dir and delta_sign == break_dir else 0) * 0.330 + (flow_conf if break_dir and flow_sign == break_dir else 0) * 0.200 + (ecosystem_conf if break_dir and ecosystem_sign == break_dir else 0) * 0.100 + (derivative_conf if break_dir and derivative_sign == break_dir else 0) * 0.080
        else:
            global_bounce = (gamma_conf if bounce_dir and gamma_sign == bounce_dir else 0) * 0.375 + (delta_conf if bounce_dir and delta_sign == bounce_dir else 0) * 0.375 + (flow_conf if bounce_dir and flow_sign == bounce_dir else 0) * 0.250
            global_break = (gamma_conf if break_dir and gamma_sign == break_dir else 0) * 0.350 + (delta_conf if break_dir and delta_sign == break_dir else 0) * 0.400 + (flow_conf if break_dir and flow_sign == break_dir else 0) * 0.250

        dominance = float(np.clip(_finite(r.get("dominance_score"), 0), 0, 100))
        contain = float(np.clip(_finite(r.get("containment_score_delta", r.get("containment_score")), 0), 0, 100))
        brk = float(np.clip(_finite(r.get("break_score_delta", r.get("break_score")), 0), 0, 100))
        # Display keeps the legacy composite; SCORING uses the dominance-free
        # residual so dominance enters the blend exactly once.
        contain_core = float(np.clip(_finite(r.get("containment_core", contain), 0), 0, 100))
        brk_core = float(np.clip(_finite(r.get("break_core", brk), 0), 0, 100))
        signed_gex = _finite(r.get("signed_gex"), 0)
        gamma_positive = 100.0 if signed_gex > 0 else 0.0
        gamma_negative = 100.0 if signed_gex < 0 else 0.0

        # v1.14 · NO DOUBLE COUNTING.
        # dominance_score already contains OI mass, gamma intensity, turnover,
        # proximity and persistence. Re-adding oi_pct / vol_pct / delta_pct /
        # persistence_pct / proximity on top of it inflated the effective weight on
        # open interest to ~0.19 while advertising 0.15, and made structural close
        # to a monotone function of OI. Structural now adds only the evidence that
        # dominance does NOT already carry.
        structural = (
            0.52 * dominance +
            0.18 * float(r["delta_intensity_pct"]) +
            0.14 * float(r["unusual_pct"]) +
            0.16 * special_score
        )
        bounce = (
            0.34 * contain_core +
            0.14 * gamma_positive +
            0.20 * structural +
            0.18 * global_bounce +
            0.08 * direction_flow_bounce +
            0.04 * events["prints"] +
            0.02 * (100.0 - min(macro_risk, 100.0))
        )
        break_score = (
            0.34 * brk_core +
            0.14 * gamma_negative +
            0.16 * structural +
            0.20 * global_break +
            0.07 * direction_flow_break +
            0.04 * events["prints"] +
            0.05 * vol_expansion
        )
        # Exact/near-spot candidate may remain NEUTRAL. If there is real directional
        # evidence, both local scenario directions inherit it without an artificial prior.
        if side == 0 and (bounce_dir != 0 or break_dir != 0):
            bounce = max(bounce, 0.50 * structural + 0.35 * global_bounce + 0.15 * contain_core)
            break_score = max(break_score, 0.45 * structural + 0.35 * global_break + 0.20 * brk_core)
        rows.append({
            "strike": level,
            "side": side,
            "bounce_direction": "BUY" if bounce_dir > 0 else "SELL" if bounce_dir < 0 else "NEUTRAL",
            "break_direction": "BUY" if break_dir > 0 else "SELL" if break_dir < 0 else "NEUTRAL",
            "structural_score": float(np.clip(structural, 0, 100)),
            "bounce_score": float(np.clip(bounce, 0, 100)),
            "break_score": float(np.clip(break_score, 0, 100)),
            "dominance": dominance,
            "containment": contain,
            "break_raw": brk,
            "oi": _finite(r.get("open_interest"), 0),
            "volume": _finite(r.get("option_volume"), 0),
            "vol_oi": float(r["vol_oi"]),
            "vol_oi_raw": float(r.get("vol_oi_raw", r["vol_oi"])),
            "vol_oi_stabilizer": float(r.get("vol_oi_stabilizer", 0.0)),
            "mass_score": 100.0 * float(r.get("mass_pct", 0.0)),
            "gamma_intensity_score": 100.0 * float(r.get("intensity_pct", 0.0)),
            "turnover_score": 100.0 * float(r.get("turnover_pct", 0.0)),
            "net_tilt_score": 100.0 * float(r.get("net_tilt_pct", 0.0)),
            "net_tilt_ratio": float(r.get("net_tilt_ratio", 0.0)),
            "signed_gex": signed_gex,
            "gross_gex": _finite(r.get("gross_gex"), 0),
            "delta_exposure": _finite(r.get("delta_exposure"), 0),
            "specials": specials,
            "special_score": special_score,
            "near_buy_flow": events["buy_flow"],
            "near_sell_flow": events["sell_flow"],
            "near_print": events["prints"],
            "oi_pct": float(r["oi_pct"]), "vol_pct": float(r["vol_pct"]), "delta_pct": float(r["delta_pct"]),
            "delta_intensity_pct": float(r["delta_intensity_pct"]),
            "containment_core": contain_core, "break_core": brk_core,
            "unusual_pct": float(r["unusual_pct"]), "persistence_pct": float(r["persistence_pct"]), "proximity": float(r["proximity"]),
            "global_bounce": float(global_bounce), "global_break": float(global_break),
            "provider_gamma_conf": float(gamma_fusion.get("provider_confidence", 0) or 0),
            "provider_delta_conf": float(delta_fusion.get("provider_confidence", 0) or 0),
            "provider_flow_conf": float(flow_fusion.get("provider_confidence", 0) or 0),
            "provider_ecosystem_conf": float(ecosystem_fusion.get("provider_confidence", 0) or 0),
            "provider_derivative_conf": float(derivative_fusion.get("provider_confidence", 0) or 0),
            "flow_bounce": float(direction_flow_bounce), "flow_break": float(direction_flow_break),
            "vol_expansion": float(vol_expansion), "macro_risk": float(macro_risk),
            "distance": abs(level - spot),
        })
    return pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def _compact_reason(name: str, value: float, positive: bool = True, detail: str = "") -> Dict[str, Any]:
    return {"label": name, "value": float(value), "positive": bool(positive), "detail": detail}


def _target_candidates(zones: pd.DataFrame, direction: int, entry: float, step: float, vol: Dict[str, Any], refs: Dict[str, Optional[float]]) -> pd.DataFrame:
    if zones.empty:
        return zones
    x = zones[zones["strike"] > entry + step * 0.35].copy() if direction > 0 else zones[zones["strike"] < entry - step * 0.35].copy()
    if x.empty:
        return x
    x["target_base"] = 0.58 * x["structural_score"] + 0.18 * x["oi"].rank(pct=True) * 100 + 0.14 * x["volume"].rank(pct=True) * 100
    # Add attraction for centers/walls/expected-move references near the candidate.
    radius = max(step * 0.45, entry * 0.00035)
    x["ref_bonus"] = [min(100.0, len(_label_zone(float(k), refs, radius)) * 25.0) for k in x["strike"]]
    x["attraction_score"] = (x["target_base"] + 0.10 * x["ref_bonus"]).clip(0, 100)
    x["travel"] = (x["strike"] - entry).abs()
    # Nearer strong zones are T1; farther exceptionally strong zones can become T2.
    x["route_score"] = x["attraction_score"] - 6.0 * (x["travel"] / max(step, 1e-6)).clip(0, 8)
    return x.sort_values("strike", ascending=(direction > 0)).reset_index(drop=True)


def _pick_targets(x: pd.DataFrame, direction: int) -> tuple[Optional[pd.Series], Optional[pd.Series]]:
    if x.empty:
        return None, None
    # T1: first level with meaningful attraction, otherwise nearest.
    strong = x[x["attraction_score"] >= 52]
    t1 = strong.iloc[0] if not strong.empty else x.iloc[0]
    after = x[x["strike"] > t1["strike"]] if direction > 0 else x[x["strike"] < t1["strike"]]
    strong2 = after[after["attraction_score"] >= 55]
    if not strong2.empty:
        t2 = strong2.iloc[0]
    elif not after.empty:
        t2 = after.iloc[0]
    else:
        t2 = None
    return t1, t2


def _vacuum(zones: pd.DataFrame, start: float, end: Optional[float], direction: int, step: float) -> Dict[str, Any]:
    if end is None or zones.empty:
        return {"active": False, "score": 0.0, "from": start, "to": end}
    lo, hi = sorted([start, end])
    mid = zones[(zones["strike"] > lo + step * 0.25) & (zones["strike"] < hi - step * 0.25)]
    if mid.empty:
        return {"active": abs(end-start) >= 1.25*step, "score": 90.0 if abs(end-start) >= 1.25*step else 40.0, "from": start, "to": end}
    avg = float(mid["structural_score"].mean())
    score = float(np.clip(100.0 - avg, 0, 100))
    return {"active": score >= 62 and abs(end-start) >= 1.25*step, "score": score, "from": start, "to": end}


def _load_probability_model(symbol: str, expiry_mode: str | None = None) -> Dict[str, Any]:
    """Latest calibration report for this symbol, including non-promoted stages."""
    try:
        from . import alpaca_data
        scope=str(expiry_mode or "").strip().lower().replace(" ","_").replace("/","_")
        suffix=f"_{scope}" if scope else ""
        f = routed_dir(Path(alpaca_data.DATA_DIR), "probability") / f"probability_model_{str(symbol).lower()}{suffix}.json"
        if not f.exists():
            return {}
        m = json.loads(f.read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except Exception as exc:
        _obs_note("scenario_engine:probability_model_unreadable", exc, severity="CRITICAL_MODEL")
        return {}


def _evidence_to_probability(evidence: float, model: Dict[str, Any]) -> tuple[float | None, str]:
    """Map an evidence score to P(T1 before invalidation).

    Falls back to the measured base rate when the isotonic fit is not usable, and to
    None when there is no history at all - in which case the EV gate stays inactive
    rather than inventing a probability out of a relevance score.
    """
    if not model:
        return None, "SIN MODELO"
    # Never expose a fitted probability as production-calibrated unless it passed the
    # OOS Brier promotion rule.  SHADOW/COLLECTING can be inspected in Research, but
    # cannot masquerade as a probability in the LIVE decision cockpit.
    if not bool(model.get("ready")):
        return None, str(model.get("stage") or model.get("status") or "SHADOW")
    knots = model.get("knots") or []
    if knots:
        xs = [float(k["evidence"]) for k in knots]; ys = [float(k["probability"]) for k in knots]
        src=str(model.get("source_label") or "ISOTONIC OOS CALIBRADO")
        return float(np.clip(np.interp(float(evidence), xs, ys, left=ys[0], right=ys[-1]), 0.01, 0.99)), src
    return None, "CALIBRADO SIN KNOTS"



def _resolve_edge_state(evidence: float, contender_gap: float, status: str,
                        ev_r: float | None, model_stage: str, model_ready: bool,
                        ev_env_enabled: bool, ev_actionable: float = 0.15,
                        ev_caution: float = 0.0) -> tuple[str, str, bool]:
    """Production actionability with an explicit calibration promotion boundary."""
    structurally_unclear = (str(status) != "PRIMARY") or float(contender_gap) < 2.5
    active = bool(ev_env_enabled and model_ready and ev_r is not None and math.isfinite(float(ev_r)))
    if active:
        mode = "EXPECTED VALUE · ACTIVE"
        if structurally_unclear or float(ev_r) <= ev_caution:
            return "NO EDGE", mode, True
        if float(ev_r) < ev_actionable or float(contender_gap) < 6.0:
            return "CAUTION", mode, True
        return "ACTIONABLE", mode, True
    mode = (f"EXPECTED VALUE · {model_stage} · SHADOW" if ev_r is not None
            else f"EVIDENCE THRESHOLD · {model_stage}")
    if structurally_unclear or evidence < 52:
        return "NO EDGE", mode, False
    if evidence < 65 or float(contender_gap) < 6.0:
        return "CAUTION", mode, False
    return "ACTIONABLE", mode, False



def _stop_risk_model(zones: pd.DataFrame, direction: int, entry: float, zone_low: float, zone_high: float,
                     spot: float, step: float, vol: Dict[str, Any], target1: float | None = None, symbol: str = "UNKNOWN") -> Dict[str, Any]:
    """Volatility-aware + nearby-structure-aware Scanner invalidation.

    LIVE uses one explicit heuristic k (default 1.25σ).  Alternative k values are
    calculated in SHADOW only so Research can later compare them without changing
    production decisions.  If ATM IV is unavailable, the legacy buffer is retained
    and labelled as a fallback rather than fabricating volatility.
    """
    try:
        horizon_min=max(1.0,float(os.getenv("ITM_STOP_HORIZON_MIN","10")))
    except Exception:
        horizon_min=10.0
    try:
        k_live=float(np.clip(float(os.getenv("ITM_STOP_K_SIGMA","1.25")),0.50,3.00))
    except Exception:
        k_live=1.25
    try:
        struct_score_min=float(np.clip(float(os.getenv("ITM_STOP_STRUCTURAL_SCORE_MIN","65")),0,100))
    except Exception:
        struct_score_min=65.0
    try:
        struct_max_steps=max(0.5,float(os.getenv("ITM_STOP_STRUCTURAL_MAX_STEPS","2.0")))
    except Exception:
        struct_max_steps=2.0
    min_buffer=max(float(step)*0.25,float(spot)*0.00015)
    legacy_buffer=max(float(step)*0.42,float(spot)*0.00045)
    clearance=max(float(step)*0.15,float(spot)*0.00010)

    iv_pct=_maybe((vol or {}).get("atm_iv"))
    sigma_h=None
    vol_buffer=None
    source="ATM_IV"
    if iv_pct is not None and iv_pct>0:
        # Same sigma formula for every asset; only the physical trading clock comes from instrument metadata.
        sigma_h=instrument_sigma_horizon(float(spot),float(iv_pct),float(horizon_min),symbol)
        if not math.isfinite(sigma_h) or sigma_h<=0:
            sigma_h=None
    if sigma_h is not None:
        vol_buffer=max(k_live*sigma_h,min_buffer)
    else:
        source="LEGACY_FALLBACK_NO_IV"
        vol_buffer=legacy_buffer

    # Use only a NEARBY strong zone on the failure side.  Farther levels belong to a
    # different thesis and must not silently drag the stop several strikes away.
    structural_ref=None
    structural_score=None
    structural_buffer=min_buffer
    if isinstance(zones,pd.DataFrame) and not zones.empty and "strike" in zones.columns:
        zz=zones.copy()
        zz["strike"]=numeric_column(zz,"strike",float("nan"))
        zz["structural_score"]=numeric_column(zz,"structural_score",0.0)
        zz=zz.dropna(subset=["strike"])
        max_dist=float(step)*struct_max_steps
        if direction>0:
            m=(zz["strike"]<zone_low-1e-9)&((zone_low-zz["strike"])<=max_dist)&(zz["structural_score"]>=struct_score_min)
            zc=zz.loc[m].sort_values("strike",ascending=False)
            if len(zc):
                rr=zc.iloc[0]; structural_ref=float(rr["strike"]); structural_score=float(rr["structural_score"])
                structural_buffer=max(min_buffer,zone_low-structural_ref+clearance)
        else:
            m=(zz["strike"]>zone_high+1e-9)&((zz["strike"]-zone_high)<=max_dist)&(zz["structural_score"]>=struct_score_min)
            zc=zz.loc[m].sort_values("strike",ascending=True)
            if len(zc):
                rr=zc.iloc[0]; structural_ref=float(rr["strike"]); structural_score=float(rr["structural_score"])
                structural_buffer=max(min_buffer,structural_ref-zone_high+clearance)

    final_buffer=max(float(vol_buffer),float(structural_buffer),float(min_buffer))
    invalidation=float(zone_low-final_buffer if direction>0 else zone_high+final_buffer)
    risk_distance=abs(float(entry)-invalidation)
    risk_sigma=(risk_distance/sigma_h) if sigma_h is not None and sigma_h>1e-12 else None

    # Which strong structures would the final invalidation cross?  This is audit-only;
    # it never changes Scanner direction or Evidence.  Multiple crossed structures are
    # a warning that the required risk belongs to a broader thesis.
    crossed=[]
    if isinstance(zones,pd.DataFrame) and not zones.empty and "strike" in zones.columns:
        for _,zr in zones.iterrows():
            ks=_maybe(zr.get("strike")); sc=_finite(zr.get("structural_score"),0)
            if ks is None or sc<struct_score_min or abs(ks-entry)<1e-9: continue
            if direction>0 and invalidation < ks < zone_low: crossed.append({"strike":round(ks,4),"score":round(sc,2)})
            if direction<0 and zone_high < ks < invalidation: crossed.append({"strike":round(ks,4),"score":round(sc,2)})
    crossed=sorted(crossed,key=lambda z:abs(float(z["strike"])-entry))[:6]

    # Shadow alternatives. They share the same structural floor; only k changes.
    shadow=[]
    for kval in (1.00,1.25,1.50,1.75):
        vb=max(kval*sigma_h,min_buffer) if sigma_h is not None else legacy_buffer
        fb=max(vb,structural_buffer,min_buffer)
        inv=float(zone_low-fb if direction>0 else zone_high+fb)
        rd=abs(entry-inv)
        rr1=(abs(float(target1)-entry)/rd) if target1 is not None and rd>1e-12 else None
        shadow.append({"k_sigma":kval,"buffer":round(float(fb),6),"invalidation":round(inv,6),
                       "rr_t1":None if rr1 is None else round(float(rr1),6),"active_live":abs(kval-k_live)<1e-9})

    status=("VOLATILITY + NEARBY STRUCTURE" if sigma_h is not None and structural_ref is not None
            else "VOLATILITY" if sigma_h is not None else
            "FALLBACK + NEARBY STRUCTURE" if structural_ref is not None else "LEGACY FALLBACK · NO IV")
    return {
        "ready":True,"status":status,"source":source,"atm_iv_pct":iv_pct,
        "horizon_minutes":round(horizon_min,3),"sigma_h":None if sigma_h is None else round(sigma_h,6),
        "k_live":round(k_live,4),"minimum_buffer":round(min_buffer,6),
        "volatility_buffer":round(float(vol_buffer),6),"structural_buffer":round(float(structural_buffer),6),
        "final_buffer":round(float(final_buffer),6),"invalidation":round(invalidation,6),
        "risk_distance":round(risk_distance,6),"risk_sigma":None if risk_sigma is None else round(float(risk_sigma),4),
        "structural_reference":None if structural_ref is None else round(structural_ref,6),
        "structural_reference_score":None if structural_score is None else round(structural_score,2),
        "crossed_structures":crossed,"structural_conflict_shadow":len(crossed)>1,
        "shadow_k":shadow,
        "note":"LIVE stop = max(ruido IV·k, piso mínimo, estructura fuerte cercana). k alternativos son SHADOW y no cambian la señal. Sin IV se usa fallback legado explícito."
    }

def build_quant_scanner(symbol: str, result: Dict[str, Any], flow: Dict[str, Any], vol: Dict[str, Any], positioning: Dict[str, Any],
                        flow_events: pd.DataFrame, large_prints: pd.DataFrame, premarket: Optional[Dict[str, Any]],
                        macro: Dict[str, Any], market_state: str = "", previous: Optional[Dict[str, Any]] = None,
                        live_ticks: Optional[pd.DataFrame] = None, regime_context: Optional[Dict[str, Any]] = None,
                        expiry_mode: str | None = None, probability_model_override: Optional[Dict[str, Any]] = None,
                        provider_features: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    zones = _scenario_zone_table(result, flow, vol, positioning, flow_events, large_prints, premarket, macro, live_ticks=live_ticks, provider_features=provider_features)
    spot = _maybe(result.get("spot"))
    if zones.empty or spot is None:
        return {"ready": False, "symbol": symbol, "state": "WAITING", "reason": "Sin estructura suficiente para construir escenario."}
    step = _price_step(_current_structure_frame(result))
    refs = _special_refs(result, positioning, vol, premarket)

    # Regime-adaptive weights are introduced in SHADOW mode by default. They are
    # visible/auditable but do not silently rewrite the production Scanner until
    # ITM_ADAPTIVE_SCANNER=1 is explicitly enabled after calibration.
    regime_context = regime_context or {}
    profile = regime_context.get("profile", {}) or {}
    calibration_ready = bool(regime_context.get("calibration_ready", False))
    adaptive_enabled = os.getenv("ITM_ADAPTIVE_SCANNER", "0") == "1" and float(regime_context.get("confidence",0) or 0) >= 55 and calibration_ready
    contain_mult = float(profile.get("containment", 1.0) or 1.0)
    break_mult = float(profile.get("break", 1.0) or 1.0)
    structure_mult = float(profile.get("structure", 1.0) or 1.0)
    flow_mult = float(profile.get("flow", 1.0) or 1.0)
    volatility_mult = float(profile.get("volatility", 1.0) or 1.0)
    zones["bounce_score_shadow"] = np.clip(zones["bounce_score"] * contain_mult, 0, 100)
    zones["break_score_shadow"] = np.clip(zones["break_score"] * break_mult, 0, 100)
    zones["structural_score_shadow"] = np.clip(zones["structural_score"] * structure_mult, 0, 100)

    # Candidate competition: the market may bounce at a strong zone or break through it.
    contenders = []
    for _, r in zones.iterrows():
        for kind in ("BOUNCE", "BREAK"):
            legacy_score = float(r["bounce_score"] if kind == "BOUNCE" else r["break_score"])
            shadow_score = float(r["bounce_score_shadow"] if kind == "BOUNCE" else r["break_score_shadow"])
            direction = r["bounce_direction"] if kind == "BOUNCE" else r["break_direction"]
            if direction not in {"BUY","SELL"}:
                continue
            aligned_flow = float(r["near_buy_flow"] if direction == "BUY" else r["near_sell_flow"])
            flow_factor = 1.0 + (flow_mult - 1.0) * float(np.clip(aligned_flow / 100.0, 0, 1))
            vol_component = float(np.clip(float(r.get("vol_expansion", 0)) / 100.0, 0, 1))
            # In expansion regimes, stronger volatility emphasis favors BREAK and penalizes BOUNCE;
            # in pinning/compression profiles the inverse occurs. This remains SHADOW unless gated active.
            vol_factor = (1.0 + (volatility_mult - 1.0) * vol_component) if kind == "BREAK" else (1.0 - (volatility_mult - 1.0) * vol_component)
            shadow_score = float(np.clip(shadow_score * flow_factor * vol_factor, 0, 100))
            score = shadow_score if adaptive_enabled else legacy_score
            proximity_bonus = 12.0 * math.exp(-float(r["distance"]) / max(step * 2.5, 1e-6))
            structural_used = float(r["structural_score_shadow"] if adaptive_enabled else r["structural_score"])
            combined = float(np.clip(0.72 * score + 0.28 * structural_used + proximity_bonus, 0, 100))
            shadow_combined = float(np.clip(0.72 * shadow_score + 0.28 * float(r["structural_score_shadow"]) + proximity_bonus, 0, 100))
            contenders.append({"kind": kind, "direction": direction, "combined": combined, "shadow_combined": shadow_combined, "row": r,
                               "shadow_flow_factor":flow_factor,"shadow_vol_factor":vol_factor})
    contenders.sort(key=lambda x: x["combined"], reverse=True)
    if not contenders:
        return {"ready":False,"symbol":symbol,"state":"NO EDGE","reason":"NO_DIRECTIONAL_EVIDENCE","direction":None,
                "edge_state":"NO EDGE","authority":"SCANNER_DIRECTION_REQUIRES_OBSERVED_STRUCTURE"}
    best = contenders[0]
    shadow_best = max(contenders, key=lambda x: x.get("shadow_combined", x["combined"]))
    r = best["row"]
    direction = 1 if best["direction"] == "BUY" else -1
    entry = float(r["strike"])
    zone_half = max(step * 0.18, spot * 0.00015)
    zone_low, zone_high = entry - zone_half, entry + zone_half

    target_df = _target_candidates(zones, direction, entry, step, vol, refs)
    t1r, t2r = _pick_targets(target_df, direction)
    t1 = None if t1r is None else float(t1r["strike"])
    t2 = None if t2r is None else float(t2r["strike"])
    stop_model = _stop_risk_model(zones, direction, entry, zone_low, zone_high, spot, step, vol, t1, symbol)
    invalidation = float(stop_model["invalidation"])
    rr1 = abs((t1-entry)/(entry-invalidation)) if t1 is not None and abs(entry-invalidation)>1e-9 else None
    rr2 = abs((t2-entry)/(entry-invalidation)) if t2 is not None and abs(entry-invalidation)>1e-9 else None
    vac = _vacuum(zones, entry, t2 or t1, direction, step)

    bounce = float(r["bounce_score"])
    brk = float(r["break_score"])
    evidence = float(best["combined"])
    current_mode = "PREMARKET MAP" if str(market_state).upper() == "PREMARKET" else "LIVE SCENARIO" if str(market_state).upper() == "REGULAR" else "STRUCTURAL SCENARIO"
    status = "PRIMARY"

    # Persistence filter: avoid flip-flopping when two scenarios are nearly tied.
    if isinstance(previous, dict) and previous.get("ready") and previous.get("symbol") == symbol:
        old_dir = previous.get("direction")
        old_ev = _finite(previous.get("evidence_score"), 0)
        if old_dir and old_dir != best["direction"] and evidence < old_ev + 8.0:
            status = "TRANSITION / WAITING CONFIRMATION"
            evidence = max(evidence - 5.0, 0.0)

    # Direction remains BUY/SELL for continuity, while actionability is separated from direction.
    # This prevents a weak forced ranking from being presented as an equally strong trade setup.
    second_score = float(contenders[1]["combined"]) if len(contenders) > 1 else 0.0
    contender_gap = max(0.0, float(best["combined"]) - second_score)

    # ------------------------------------------------------------------
    # v1.14.3 · EXPECTED-VALUE GATE
    # Actionability used to be a threshold on evidence alone, while rr_t1 was computed
    # and then discarded. That ranked a setup with evidence 70 and R/R 0.4 above one
    # with evidence 55 and R/R 2.5, even though the second wins at any plausible hit
    # rate. Direction still comes from the structure; what changes is whether the
    # platform calls the trade worth taking.
    #
    #     EV(R) = p * RR - (1 - p) - costes        breakeven p = (1 + costes)/(RR + 1)
    #
    # p is MEASURED (isotonic fit on the logged history, adopted only if it beat the
    # base rate out of sample). With no history the gate stays inactive and the legacy
    # evidence thresholds apply, so the platform never fabricates a probability.
    # ------------------------------------------------------------------
    prob_model = (dict(probability_model_override) if probability_model_override is not None
                  else _load_probability_model(symbol, expiry_mode))
    prob, prob_source = _evidence_to_probability(evidence, prob_model)
    raw_cost_r=os.getenv("ITM_BACKTEST_COST_R")
    cost_measured=bool(raw_cost_r is not None and str(raw_cost_r).strip()!="")
    cost_source="CONFIGURED_MEASURED" if cost_measured else "UNAVAILABLE"
    cost_r=None
    if cost_measured:
        try:
            parsed=float(raw_cost_r)
            if not math.isfinite(parsed) or parsed < 0:
                raise ValueError("cost_r must be finite and >=0")
            cost_r=parsed
        except Exception:
            cost_measured=False;cost_source="INVALID_CONFIG";cost_r=None
    instrument_mode = "options" if str(os.getenv("ITM_INSTRUMENT_MODE","underlying")).strip().lower().startswith("opt") else "underlying"
    rr_used = rr1 if (rr1 is not None and math.isfinite(rr1) and rr1 > 0) else None
    rr_source = "UNDERLYING_TO_INVALIDATION"
    option_rr = {"ready":False,"reason":"instrument_mode=underlying"}
    if instrument_mode == "options":
        try:
            from .calibration import option_economic_rr
            iv_dec=(float(vol.get("atm_iv"))/100.0) if _maybe(vol.get("atm_iv")) is not None else float("nan")
            dte=_maybe(vol.get("nearest_dte"))
            option_rr=option_economic_rr(symbol,best["direction"],spot,entry,t1,invalidation,iv_dec,dte,
                                         prob_model.get("resolution_time") if prob_model else None) if t1 is not None and dte is not None else {"ready":False,"reason":"faltan T1/IV/DTE"}
            if option_rr.get("ready") and _maybe(option_rr.get("rr")) is not None and float(option_rr.get("rr"))>0:
                rr_used=float(option_rr["rr"]);rr_source="OPTION_PREMIUM_MODEL_TO_INVALIDATION"
            else:
                # Never fall back silently to stock geometry in options mode.  If the
                # option economics are not measurable, EV remains unavailable.
                rr_used=None;rr_source="OPTIONS_RR_UNAVAILABLE"
        except Exception as exc:
            rr_used=None;rr_source="OPTIONS_RR_ERROR";option_rr={"ready":False,"reason":str(exc)[:160]}
    ev_r = breakeven_p = edge_over_be = None
    if prob is not None and rr_used is not None and cost_r is not None:
        ev_r = float(prob * rr_used - (1.0 - prob) - cost_r)
        breakeven_p = float((1.0 + cost_r) / (rr_used + 1.0))
        edge_over_be = float(prob - breakeven_p)

    try:
        ev_actionable = float(os.getenv("ITM_EV_ACTIONABLE_R", "0.15"))
        ev_caution = float(os.getenv("ITM_EV_CAUTION_R", "0.0"))
    except Exception:
        ev_actionable, ev_caution = 0.15, 0.0

    model_stage = str(prob_model.get("stage") or prob_model.get("status") or "COLLECTING") if prob_model else "COLLECTING"
    ev_env_enabled = os.getenv("ITM_EV_GATE_ACTIVE", "0") == "1"
    edge_state, gate_mode, ev_gate_active = _resolve_edge_state(
        evidence, contender_gap, status, ev_r, model_stage, bool(prob_model.get("ready")),
        ev_env_enabled, ev_actionable, ev_caution)

    reasons = []
    contradictions = []
    tape = _tape_aggression(live_ticks)
    if r["structural_score"] >= 65:
        reasons.append(_compact_reason("Zona estructural fuerte", r["structural_score"], True, "Gamma/OI/volumen/Delta/proximidad"))
    if r["dominance"] >= 65:
        reasons.append(_compact_reason("Gamma Dominance", r["dominance"], True, f"GEX firmado {r['signed_gex']:+.2e}"))
    if r["oi"] > 0:
        reasons.append(_compact_reason("Open Interest", min(100.0, float(zones.loc[zones['oi']<=r['oi']].shape[0]) / max(len(zones),1) * 100), True, f"OI {r['oi']:.0f}"))
    if r["volume"] > 0:
        reasons.append(_compact_reason("Volumen actual", min(100.0, float(zones.loc[zones['volume']<=r['volume']].shape[0]) / max(len(zones),1) * 100), True, f"Vol {r['volume']:.0f}"))
    if r["vol_oi"] >= 1:
        reasons.append(_compact_reason("Actividad Vol/OI", min(r["vol_oi"] * 20, 100), True, f"{r['vol_oi']:.2f}x"))
    if r["specials"]:
        reasons.append(_compact_reason("Confluencia de niveles", min(len(r["specials"])*28,100), True, ", ".join(r["specials"])))
    side_flow = r["near_buy_flow"] if direction > 0 else r["near_sell_flow"]
    opp_flow = r["near_sell_flow"] if direction > 0 else r["near_buy_flow"]
    if side_flow >= 60:
        reasons.append(_compact_reason("Flow cercano alineado", side_flow, True, "Q-Flow cerca de la zona"))
    if r["near_print"] >= 65:
        reasons.append(_compact_reason("Large Print cercano", r["near_print"], True, "Print relevante cerca de la zona"))
    # TRACE aggression is intentionally NOT added to reasons/contradictions: it is
    # stored as execution context and judged by Tape Confirmation only after the
    # price reaches the Scanner zone.
    if opp_flow >= 60:
        contradictions.append(_compact_reason("Flow contrario", opp_flow, False, "Hay agresión opuesta cerca de la zona"))
    if best["kind"] == "BOUNCE" and brk >= bounce - 8:
        contradictions.append(_compact_reason("Ruptura competitiva", brk, False, "Break y Bounce están demasiado cerca"))
    if best["kind"] == "BREAK" and bounce >= brk - 8:
        contradictions.append(_compact_reason("Contención competitiva", bounce, False, "Bounce y Break están demasiado cerca"))
    if str(vol.get("regime", "")).upper() == "EXPANSION" and best["kind"] == "BOUNCE":
        contradictions.append(_compact_reason("IV en expansión", 70, False, "La volatilidad puede dificultar la contención"))
    if stop_model.get("structural_conflict_shadow"):
        contradictions.append(_compact_reason("Riesgo estructural amplio", 68, False,
            f"Invalidación cruza {len(stop_model.get('crossed_structures') or [])} estructuras fuertes; bandera SHADOW, no cambia dirección"))

    # Take the clearest reasons first.
    reasons = sorted(reasons, key=lambda x: x["value"], reverse=True)[:8]
    contradictions = sorted(contradictions, key=lambda x: x["value"], reverse=True)[:5]

    # Secondary plan: opposite scenario at the same zone or best contender of the opposite direction.
    secondary = None
    for c in contenders[1:]:
        if c["direction"] != best["direction"]:
            sr = c["row"]
            secondary = {
                "direction": c["direction"], "kind": c["kind"], "zone": float(sr["strike"]),
                "evidence_score": float(c["combined"]),
                "trigger": float(invalidation),
            }
            break

    return {
        "ready": True,
        "symbol": symbol,
        "state": status,
        "edge_state": edge_state,
        "edge_gate": {
            "mode": gate_mode,
            "stage": "ACTIVE" if ev_gate_active else model_stage,
            "active": ev_gate_active,
            "env_enabled": ev_env_enabled,
            "calibration_ready": bool(prob_model.get("ready")) if prob_model else False,
            "calibration_expiry_mode": str(expiry_mode or "ALL"),
            "probability_t1_first": None if prob is None else round(prob, 4),
            "probability_source": prob_source,
            "rr_used": None if rr_used is None else round(rr_used, 3),
            "rr_source": rr_source,
            "instrument_mode": instrument_mode.upper(),
            "underlying_rr_t1": None if rr1 is None else round(rr1,3),
            "risk_unit": (option_rr.get("risk_unit") if instrument_mode=="options" else "UNDERLYING_TO_INVALIDATION"),
            "option_economic_rr": option_rr if instrument_mode=="options" else None,
            "pooling": {"source":prob_model.get("source_label"),"scope_weight":prob_model.get("pooling_weight_scope"),"global_weight":prob_model.get("pooling_weight_global"),"k":prob_model.get("pooling_k"),"global_samples":prob_model.get("global_samples"),"scope_samples":prob_model.get("scope_samples")},
            "resolution_time": prob_model.get("resolution_time") if prob_model else None,
            "cost_r": cost_r,
            "cost_measured":cost_measured,"cost_source":cost_source,
            "expected_value_r": None if ev_r is None else round(ev_r, 4),
            "breakeven_probability": None if breakeven_p is None else round(breakeven_p, 4),
            "edge_over_breakeven": None if edge_over_be is None else round(edge_over_be, 4),
            "thresholds_r": {"actionable": ev_actionable, "caution": ev_caution},
            "note": "EV en múltiplos de R coherentes con el instrumento. En OPTIONS el R/R usa prima modelada hasta invalidación y tiempo histórico de resolución; Evidence no es probabilidad. Sin coste R medido/configurado, EV queda UNAVAILABLE y no puede activar el gate.",
        },
        "contender_gap": round(contender_gap, 2),
        "regime_context": regime_context,
        "adaptive_mode": "ACTIVE" if adaptive_enabled else "SHADOW",
        "adaptive_gate": {"env_enabled": os.getenv("ITM_ADAPTIVE_SCANNER", "0") == "1", "calibration_ready": calibration_ready, "regime_confidence": float(regime_context.get("confidence",0) or 0)},
        "shadow_scenario": {"direction": shadow_best.get("direction"), "kind": shadow_best.get("kind"), "evidence_score": float(shadow_best.get("shadow_combined",0)), "zone": float(shadow_best.get("row",{}).get("strike", entry)) if hasattr(shadow_best.get("row",{}), "get") else entry},
        "mode": current_mode,
        "spot": spot,
        "direction": best["direction"],
        "scenario_type": "REBOTE" if best["kind"] == "BOUNCE" else "RUPTURA",
        "zone": {"center": entry, "low": zone_low, "high": zone_high},
        "target1": t1,
        "target2": t2,
        "target1_score": None if t1r is None else float(t1r["attraction_score"]),
        "target2_score": None if t2r is None else float(t2r["attraction_score"]),
        "invalidation": float(invalidation),
        "stop_model": stop_model,
        "evidence_score": evidence,
        "structural_score": float(r["structural_score"]),
        "bounce_score": bounce,
        "break_score": brk,
        "rr_t1": rr1,
        "rr_t2": rr2,
        "vacuum": vac,
        "reasons": reasons,
        "contradictions": contradictions,
        "secondary": secondary,
        "references": {k: v for k, v in refs.items() if v is not None},
        # Logged so the Calibration Lab can later simulate option-premium P&L instead
        # of the underlying move. Without these the options mode cannot be evaluated.
        "volatility_inputs": {
            "atm_iv_decimal": (float(vol.get("atm_iv"))/100.0) if _maybe(vol.get("atm_iv")) is not None else None,
            "dte": _maybe(vol.get("nearest_dte")),
            "nearest_expiry": vol.get("nearest_expiry"),
        },
        "trace_aggression": {**tape, "role": "TIMING_ONLY", "affects_scanner_score": False},
        "provider_feature_fusion": provider_features or {"ready": False, "channels": {}},
        "feature_snapshot": {
            "gamma_dominance": float(r.get("dominance",0)), "containment": float(r.get("containment",0)), "break_raw": float(r.get("break_raw",0)),
            "oi_percentile": float(r.get("oi_pct",0)), "volume_percentile": float(r.get("vol_pct",0)), "delta_percentile": float(r.get("delta_pct",0)), "delta_intensity_percentile": float(r.get("delta_intensity_pct",0)),
            "mass_score": float(r.get("mass_score",0)), "gamma_intensity_score": float(r.get("gamma_intensity_score",0)), "turnover_score": float(r.get("turnover_score",0)), "net_tilt_score": float(r.get("net_tilt_score",0)),
            "unusual_percentile": float(r.get("unusual_pct",0)), "persistence_percentile": float(r.get("persistence_pct",0)), "proximity": float(r.get("proximity",0)),
            "aligned_near_flow": float(r.get("near_buy_flow",0) if direction>0 else r.get("near_sell_flow",0)), "near_print": float(r.get("near_print",0)),
            "global_directional_evidence": float(r.get("global_bounce",0) if best["kind"]=="BOUNCE" else r.get("global_break",0)),
            "provider_gamma_confidence": float(r.get("provider_gamma_conf",0)), "provider_delta_confidence": float(r.get("provider_delta_conf",0)),
            "provider_flow_confidence": float(r.get("provider_flow_conf",0)),
            "provider_ecosystem_confidence": float(r.get("provider_ecosystem_conf",0)),
            "provider_derivative_confidence": float(r.get("provider_derivative_conf",0)),
            "vol_expansion": float(r.get("vol_expansion",0)), "macro_risk": float(r.get("macro_risk",0)), "distance": float(r.get("distance",0)),
            "shadow_flow_factor": float(best.get("shadow_flow_factor",1.0)), "shadow_vol_factor": float(best.get("shadow_vol_factor",1.0)),
            "stop_sigma_h": _finite(stop_model.get("sigma_h"), np.nan), "stop_risk_sigma": _finite(stop_model.get("risk_sigma"), np.nan),
            "stop_k_live": _finite(stop_model.get("k_live"), np.nan), "stop_volatility_buffer": _finite(stop_model.get("volatility_buffer"), np.nan),
            "stop_structural_buffer": _finite(stop_model.get("structural_buffer"), np.nan), "stop_final_buffer": _finite(stop_model.get("final_buffer"), np.nan),
        },
        "candidate_zones": zones.sort_values("structural_score", ascending=False).head(12).to_dict("records"),
        "method_note": "Scanner estructural: decide zona/dirección con Gamma/GEX, Delta/DEX, OI, volumen, Vol/OI, Flip/Centers/Walls, Q-Flow, prints, volatilidad, contexto y feature fusion de proveedores, ecosistema normalizado ETF/índice/futuro y estructura observada de derivados. Los proveedores no tienen rango fijo; freshness/calidad deciden si cada observación entra. TRACE/Tape queda fuera del score y se usa solo para timing. Los scores son evidencia interna, no probabilidades garantizadas.",
        "generated_at": datetime.now().isoformat(),
    }


def save_scanner_snapshot(scanner: Dict[str, Any]) -> Optional[Path]:
    """Persist scanner hypotheses for later calibration/backtesting.

    This is intentionally a log of model output, not a trading-performance claim.
    """
    if not scanner or not scanner.get("ready"):
        return None
    try:
        ts = pd.Timestamp(scanner.get("generated_at") or datetime.now())
        symbol = str(scanner.get("symbol") or "UNKNOWN").upper()
        from . import alpaca_data
        storage = routed_dir(Path(alpaca_data.DATA_DIR), "scanner_history")
        storage.mkdir(parents=True, exist_ok=True)
        path = storage / f"scanner_history_{symbol.lower()}_{ts.date().isoformat()}.csv"
        row = {
            "timestamp": ts.isoformat(), "symbol": symbol, "spot": scanner.get("spot"),
            "direction": scanner.get("direction"), "scenario_type": scanner.get("scenario_type"),
            "zone_low": (scanner.get("zone") or {}).get("low"), "zone_center": (scanner.get("zone") or {}).get("center"),
            "zone_high": (scanner.get("zone") or {}).get("high"), "target1": scanner.get("target1"), "target2": scanner.get("target2"),
            "invalidation": scanner.get("invalidation"), "evidence_score": scanner.get("evidence_score"),
            "atm_iv_decimal": ((scanner.get("volatility_inputs") or {}).get("atm_iv_decimal")),
            "signal_dte": ((scanner.get("volatility_inputs") or {}).get("dte")),
            "expected_value_r": ((scanner.get("edge_gate") or {}).get("expected_value_r")),
            "probability_t1_first": ((scanner.get("edge_gate") or {}).get("probability_t1_first")),
            "gate_mode": ((scanner.get("edge_gate") or {}).get("mode")),
            "structural_score": scanner.get("structural_score"), "bounce_score": scanner.get("bounce_score"),
            "break_score": scanner.get("break_score"), "rr_t1": scanner.get("rr_t1"), "rr_t2": scanner.get("rr_t2"),
            "vacuum_active": (scanner.get("vacuum") or {}).get("active"), "vacuum_score": (scanner.get("vacuum") or {}).get("score"),
            "state": scanner.get("state"), "edge_state": scanner.get("edge_state"), "contender_gap": scanner.get("contender_gap"),
            "regime": (scanner.get("regime_context") or {}).get("regime"), "regime_confidence": (scanner.get("regime_context") or {}).get("confidence"),
            "adaptive_mode": scanner.get("adaptive_mode"), "expiry_mode": ((scanner.get("expiry_window") or {}).get("mode")), "mode": scanner.get("mode"),
            "data_quality": (scanner.get("quality_gate") or {}).get("data_quality"), "model_health": (scanner.get("quality_gate") or {}).get("model_health"),
            "stop_model_status": (scanner.get("stop_model") or {}).get("status"),
            "stop_source": (scanner.get("stop_model") or {}).get("source"),
            "stop_atm_iv_pct": (scanner.get("stop_model") or {}).get("atm_iv_pct"),
            "stop_horizon_minutes": (scanner.get("stop_model") or {}).get("horizon_minutes"),
            "stop_sigma_h": (scanner.get("stop_model") or {}).get("sigma_h"),
            "stop_k_live": (scanner.get("stop_model") or {}).get("k_live"),
            "stop_minimum_buffer": (scanner.get("stop_model") or {}).get("minimum_buffer"),
            "stop_volatility_buffer": (scanner.get("stop_model") or {}).get("volatility_buffer"),
            "stop_structural_buffer": (scanner.get("stop_model") or {}).get("structural_buffer"),
            "stop_final_buffer": (scanner.get("stop_model") or {}).get("final_buffer"),
            "stop_risk_distance": (scanner.get("stop_model") or {}).get("risk_distance"),
            "stop_risk_sigma": (scanner.get("stop_model") or {}).get("risk_sigma"),
            "stop_structural_reference": (scanner.get("stop_model") or {}).get("structural_reference"),
            "stop_structural_reference_score": (scanner.get("stop_model") or {}).get("structural_reference_score"),
            "stop_structural_conflict_shadow": (scanner.get("stop_model") or {}).get("structural_conflict_shadow"),
        }
        for sh in ((scanner.get("stop_model") or {}).get("shadow_k") or []):
            try:
                tag=str(sh.get("k_sigma")).replace(".","_")
                row[f"stop_shadow_k_{tag}_invalidation"]=sh.get("invalidation")
                row[f"stop_shadow_k_{tag}_rr_t1"]=sh.get("rr_t1")
                row[f"stop_shadow_k_{tag}_buffer"]=sh.get("buffer")
            except Exception as _e:
                _obs_note('scenario_engine:941', _e)
        for k,v in (scanner.get("feature_snapshot") or {}).items():
            row[f"feature_{k}"] = v
        # v1.42.1 · Escritura atómica y de esquema estable. El `to_csv(mode="a")`
        # anterior escribía la cabecera con las columnas del PRIMER ciclo; en cuanto
        # un ciclo posterior traía una `feature_*` o una `stop_shadow_k_*` más, la
        # fila salía con más campos que la cabecera. Ésas eran las filas que el
        # lector tenía que aislar cada arranque.
        append_row(path, row)
        return path
    except Exception:
        return None


def scanner_route_figure(scanner: Dict[str, Any]) -> go.Figure:
    fig = go.Figure()
    if not scanner or not scanner.get("ready"):
        fig.add_annotation(text="SCANNER ESPERANDO ESTRUCTURA", showarrow=False, font=dict(color=MUTED, size=16))
        fig.update_layout(height=430, paper_bgcolor=BG, plot_bgcolor=BG, xaxis=dict(visible=False), yaxis=dict(visible=False))
        return fig
    spot = float(scanner["spot"])
    zone = scanner["zone"]
    direction = 1 if scanner["direction"] == "BUY" else -1
    t1 = scanner.get("target1")
    t2 = scanner.get("target2")
    inv = float(scanner["invalidation"])
    vals = [spot, zone["low"], zone["high"], inv] + ([t1] if t1 is not None else []) + ([t2] if t2 is not None else [])
    lo, hi = min(vals), max(vals)
    pad = max((hi-lo)*0.15, abs(spot)*0.001)
    lo -= pad; hi += pad

    fig.add_hrect(y0=0.34, y1=0.66, x0=float(zone["low"]), x1=float(zone["high"]), fillcolor=BUY if direction>0 else SELL, opacity=.24, line_width=0)
    fig.add_trace(go.Scatter(x=[lo, hi], y=[0.5, 0.5], mode="lines", line=dict(color="#304259", width=5), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=[spot], y=[0.5], mode="markers+text", text=[f"SPOT {spot:.2f}"], textposition="top center", marker=dict(size=18,color=WHITE,line=dict(color=BLUE,width=3)), name="Spot"))
    fig.add_trace(go.Scatter(x=[zone["center"]], y=[0.5], mode="markers+text", text=[f"ZONA {zone['center']:.2f}"], textposition="bottom center", marker=dict(size=17,color=BUY if direction>0 else SELL,symbol="diamond"), name="Zona"))
    if t1 is not None:
        fig.add_trace(go.Scatter(x=[t1], y=[0.5], mode="markers+text", text=[f"T1 {t1:.2f}"], textposition="top center", marker=dict(size=15,color=GOLD,symbol="triangle-up" if direction>0 else "triangle-down"), name="T1"))
    if t2 is not None:
        fig.add_trace(go.Scatter(x=[t2], y=[0.5], mode="markers+text", text=[f"T2 {t2:.2f}"], textposition="bottom center", marker=dict(size=17,color=GREEN,symbol="star"), name="T2"))
    fig.add_trace(go.Scatter(x=[inv], y=[0.5], mode="markers+text", text=[f"INVALIDA {inv:.2f}"], textposition="bottom center", marker=dict(size=13,color=RED,symbol="x"), name="Invalidación"))
    vac = scanner.get("vacuum") or {}
    if vac.get("active") and vac.get("to") is not None:
        fig.add_vrect(x0=min(zone["center"],vac["to"]),x1=max(zone["center"],vac["to"]),fillcolor="#7c3aed",opacity=.07,line_width=0,
                      annotation_text="VACÍO ESTRUCTURAL",annotation_position="top left",annotation_font_color="#bca7ff")
    fig.update_layout(
        height=430, template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG, margin=dict(l=35,r=35,t=50,b=45),
        title=f"STRUCTURAL PATH · {scanner.get('symbol','')} · {scanner.get('scenario_type','')} {scanner.get('direction','')}",
        xaxis=dict(range=[lo,hi],gridcolor=GRID,title="Precio"), yaxis=dict(range=[0,1],visible=False), legend=dict(orientation="h",y=1.12),
        hovermode="closest",
    )
    return fig
