"""ITM QUANT internal Market State Field.

This module is intentionally *not* a second signal engine.  It reduces the
mechanical option field, observed flow kinetics, related-market breadth,
volatility, macro stress and the explicitly synthetic dealer layer into a
compact state vector used by Quant Synthesis/Auditor.

Design discipline
-----------------
* Scanner remains the only directional authority.
* Positive/negative Gamma is treated mainly as stability/friction, not as a
  simplistic BUY/SELL vote.
* Higher-order Greeks are scenario sensitivities, never observed dealer intent.
* OPRA aggressor flow is observed; dealer inventory/hedge pressure is estimated
  and is weighted by its disclosed confidence.
* Scores are bounded state indices, NOT win probabilities.
"""

from __future__ import annotations

from typing import Any, Dict
import math
import numpy as np
import pandas as pd

from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
from .contract_spec import multiplier_for
from .field_normalization import normalize_related_markets
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .expiry_clock import year_fraction_array


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _clip(v: Any, lo: float = -1.0, hi: float = 1.0, default: float = 0.0) -> float:
    x = _f(v, default)
    return float(max(lo, min(hi, x if x is not None else default)))


def _tanh(v: Any, scale: float = 1.0) -> float:
    x = _f(v, 0.0) or 0.0
    return float(math.tanh(x / max(abs(float(scale)), 1e-12)))


def _latest_chain(market: Dict[str, Any]) -> pd.DataFrame:
    x = market.get("enriched") if isinstance(market, dict) else None
    if not isinstance(x, pd.DataFrame) or x.empty:
        return pd.DataFrame()
    x = x.copy()
    x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
    x = x.dropna(subset=["timestamp"])
    if x.empty:
        return pd.DataFrame()
    return x[x["timestamp"] == x["timestamp"].max()].copy()


def _options_field(market: Dict[str, Any], volatility: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    chain = _latest_chain(market)
    net_gex = _f(market.get("total_signed_gex"), 0.0) or 0.0
    gross_gex = abs(_f(market.get("total_gross_gex"), 0.0) or 0.0)
    net_delta = _f(market.get("net_delta_exposure"), 0.0) or 0.0
    gross_delta = 0.0
    if not chain.empty:
        gross_delta = float(numeric_column(chain,"option_delta_exposure_info",0.0).abs().sum())
        if gross_gex <= 0:
            gross_gex = float(numeric_column(chain,"gross_gex",0.0).abs().sum())
        if abs(net_gex) <= 1e-12:
            net_gex = float(numeric_column(chain,"signed_gex_proxy",0.0).sum())
        if abs(net_delta) <= 1e-12:
            net_delta = float(numeric_column(chain,"option_delta_exposure_info",0.0).sum())

    # v1.56.0 · UN RATIO DE CERO NO ES LO MISMO QUE NO HABER MEDIDO NADA.
    #
    # Sin exposición, `gamma_ratio` salía 0.0, y de ahí `stability = 50.0` y
    # `gamma_regime = "TRANSITION"`. Eso se lee como «el mercado está en
    # transición con gamma neutra», que es una afirmación sobre el libro de
    # opciones hecha sin haber medido una sola posición.
    #
    # Los números se conservan —el campo aguas abajo los necesita— pero se
    # declara si se midieron. Una pantalla que enseñe TRANSITION sin saber que
    # nadie midió nada está mintiendo con un número correcto.
    gamma_measured = gross_gex > 0
    delta_measured = gross_delta > 0
    gamma_ratio = _clip(net_gex / max(gross_gex, 1e-12)) if gamma_measured else 0.0
    delta_ratio = _clip(net_delta / max(gross_delta, 1e-12)) if delta_measured else 0.0

    higher = {"vanna_1vol_m": 0.0, "charm_10m_m": 0.0, "speed_1pct_m": 0.0, "color_10m_m": 0.0,
              "contracts": 0, "method": "NO_CHAIN"}
    if not chain.empty:
        try:
            K = numeric_column(chain,"strike",float("nan")).to_numpy(float)
            S0 = numeric_column(chain,"underlying_price",float("nan")).to_numpy(float)
            iv = numeric_column(chain,"iv",float("nan")).to_numpy(float)
            dte = numeric_column(chain,"dte",float("nan")).to_numpy(float)
            oi = numeric_column(chain,"open_interest",0.0).to_numpy(float)
            call = chain.get("option_type", pd.Series("call", index=chain.index)).astype(str).str.lower().str.startswith("c").to_numpy()
            ok = np.isfinite(K) & np.isfinite(S0) & np.isfinite(iv) & np.isfinite(dte) & (K > 0) & (S0 > 0) & (iv > 0) & (dte > 0) & (oi > 0)
            if ok.any():
                K, S0, iv, dte, oi, call = K[ok], S0[ok], iv[ok], dte[ok], oi[ok], call[ok]
                r, q = model_inputs_vector(symbol, dte)
                T = year_fraction_array(dte)
                g0 = _greeks_vector(symbol, S0, K, T, iv, call, r, q)
                dealer_proxy_sign = np.where(call, 1.0, -1.0)
                # El tamaño económico sale del contrato, nunca de un literal: un futuro
                # Dow no vale 100 x F y un contrato ajustado no entrega 100 acciones.
                mult = multiplier_for(symbol)
                # Delta-notional change for a +1 vol-point IV shock.
                vanna_1vol = dealer_proxy_sign * g0["vanna"] * 0.01 * oi * mult * S0
                # Exact 10-minute delta-decay repricing, keeping spot/IV/OI fixed.
                dte1 = np.maximum(dte - 10.0 / 1440.0, 0.0)
                r1, q1 = model_inputs_vector(symbol, dte1)
                g1 = _greeks_vector(symbol, S0, K, year_fraction_array(dte1), iv, call, r1, q1)
                charm10 = dealer_proxy_sign * (g1["delta"] - g0["delta"]) * oi * mult * S0
                # Change in dollar-GEX for a +1% spot shock using Speed (local derivative).
                dS = 0.01 * S0
                speed_1pct = dealer_proxy_sign * g0["speed"] * dS * oi * mult * (S0 ** 2) * 0.01
                # Color proxy = actual 10-minute change in dollar-GEX from time decay.
                gex0 = dealer_proxy_sign * g0["gamma"] * oi * mult * (S0 ** 2) * 0.01
                gex1 = dealer_proxy_sign * g1["gamma"] * oi * mult * (S0 ** 2) * 0.01
                color10 = gex1 - gex0
                higher = {
                    "vanna_1vol_m": float(np.nansum(vanna_1vol)) / 1e6,
                    "charm_10m_m": float(np.nansum(charm10)) / 1e6,
                    "speed_1pct_m": float(np.nansum(speed_1pct)) / 1e6,
                    "color_10m_m": float(np.nansum(color10)) / 1e6,
                    "contracts": int(ok.sum()),
                    "method": "BSM LOCAL SENSITIVITY · CALL+/PUT- POSITIONING PROXY",
                }
        except Exception as _e:
            _obs_note('market_state_field:118', _e)

    regime = str((volatility or {}).get("regime") or "").upper()
    vol_expansion = 1.0 if "EXP" in regime or "HIGH" in regime else 0.55 if "ELEV" in regime else 0.0
    # +Gamma raises friction/stability; -Gamma and expanding vol increase instability.
    stability = float(np.clip(50.0 + 38.0 * gamma_ratio - 22.0 * vol_expansion, 0.0, 100.0))
    instability = 100.0 - stability
    return {
        "gamma_ratio": round(gamma_ratio, 6),
        "delta_ratio": round(delta_ratio, 6),
        "net_gex": net_gex,
        "gross_gex": gross_gex,
        "net_delta": net_delta,
        "gross_delta": gross_delta,
        "stability": round(stability, 1),
        "instability": round(instability, 1),
        "gamma_regime": ("NO_EXPOSURE_DATA" if not gamma_measured else
                         "POSITIVE_FRICTION" if gamma_ratio > 0.08 else
                         "NEGATIVE_FEEDBACK" if gamma_ratio < -0.08 else "TRANSITION"),
        # Si esto es falso, los ratios y la estabilidad de arriba son el valor
        # por defecto, no una lectura del mercado.
        "gamma_measured": bool(gamma_measured),
        "delta_measured": bool(delta_measured),
        "measured": bool(gamma_measured or delta_measured),
        "higher_order": higher,
        "note": "Gamma controls friction/stability; Delta contributes direction. Higher Greeks are scenario sensitivities, not observed dealer intent.",
    }


def _flow_field(flow: Dict[str, Any], tape: Dict[str, Any]) -> Dict[str, Any]:
    bull = max(_f((flow or {}).get("bull"), 0.0) or 0.0, 0.0)
    bear = max(_f((flow or {}).get("bear"), 0.0) or 0.0, 0.0)
    net_ratio = _clip((bull - bear) / max(bull + bear, 1.0))
    micro = (flow or {}).get("microstructure") or {}
    accel = _tanh(_f(micro.get("premium_acceleration_pct"), 0.0), 100.0)
    persistence = _clip((_f(micro.get("persistence_pct"), 0.0) or 0.0) / 100.0, 0.0, 1.0)
    if abs(net_ratio) < 1e-9:
        persistence_signed = 0.0
    else:
        persistence_signed = math.copysign(persistence, net_ratio)
    kinetic = _clip(0.58 * net_ratio + 0.24 * accel + 0.18 * persistence_signed)

    conf = (tape or {}).get("confirmation") or {}
    tstate = str(conf.get("state") or "WAITING").upper()
    progress = _clip((_f(conf.get("progress_pct"), 0.0) or 0.0) / 100.0)
    if tstate in {"REJECTED", "ABSORBED", "CHURN", "EXPIRED"}:
        progress = -abs(progress if abs(progress) > 0 else 0.5)
    elif tstate == "CONFIRMED":
        progress = abs(progress if abs(progress) > 0 else 0.7)
    return {
        "net_ratio": round(net_ratio, 6), "acceleration": round(accel, 6),
        "persistence": round(persistence_signed, 6), "kinetic": round(kinetic, 6),
        "tape_alignment": round(progress, 6), "tape_state": tstate,
        "observed": bool(bull + bear > 0),
        "note": "Kinetic field uses observed directional OPRA premium + acceleration/persistence; Tape remains timing only.",
    }


def _dealer_field(dealer: Dict[str, Any]) -> Dict[str, Any]:
    state = str((dealer or {}).get("state") or "COLLECTING").upper()
    hp = (dealer or {}).get("hedge_pressure") or {}
    h15 = _f(hp.get("net_15m"), _f((dealer or {}).get("hedge_to_neutral_notional"), 0.0)) or 0.0
    conf = _clip((_f((dealer or {}).get("confidence"), 0.0) or 0.0) / 100.0, 0.0, 1.0)
    state_score=_f((dealer or {}).get("dealer_state_score"))
    if state_score is not None:
        pressure=_clip(state_score/100.0)
    else:
        pressure = 0.0 if abs(h15) <= 1e-12 else math.copysign(conf, h15)
    return {"state": state, "dealer_field":str((dealer or {}).get("dealer_field") or "UNKNOWN"),
            "pressure": round(pressure, 6), "confidence": round(conf * 100.0, 1),
            "hedge_notional_15m": h15,"inventory_shift":(dealer or {}).get("inventory_shift") or {},
            "flow_confirmation":(dealer or {}).get("flow_confirmation") or {},
            "microstructure_quality":(dealer or {}).get("microstructure_quality") or {},
            "estimated": state == "ESTIMATED",
            "note": "Dealer State combines estimated inventory/GEX, hedge pressure and observed-flow consistency. It remains synthetic and cannot flip Scanner."}


def _breadth_field(external: Dict[str, Any]) -> Dict[str, Any]:
    rel = (external or {}).get("related") or {}
    rows = []
    for sym, r in rel.items():
        if not isinstance(r, dict):
            continue
        ch = _f(r.get("change_pct"))
        if ch is None:
            continue
        rows.append({
            "symbol": str(sym),
            "change_pct": ch,
            "sigma_pct": _f(r.get("sigma_pct"), _f(r.get("realized_vol_pct"))),
        })
    normalized = normalize_related_markets(rows)
    markets = []
    for r in normalized.get("rows", []):
        markets.append({
            "symbol": r.get("symbol"), "change_pct": r.get("change_pct"),
            "z_sigma": round(_f(r.get("z_sigma"), 0.0) or 0.0, 4),
            "scaled": round(_f(r.get("field"), 0.0) or 0.0, 6),
            "normalization": r.get("normalization"),
        })
    breadth = _clip(normalized.get("field", 0.0))
    return {
        "breadth": round(breadth, 6), "markets": markets, "observed": bool(markets),
        "normalization": normalized.get("method"),
        "note": "Related-market breadth is sigma-normalized when possible and robust-MAD normalized otherwise; context only.",
    }


def _positioning_field(positioning: Dict[str, Any]) -> Dict[str, Any]:
    pcr_oi = _f((positioning or {}).get("put_call_oi_ratio"))
    pcr_vol = _f((positioning or {}).get("put_call_volume_ratio"))
    # Center around parity and saturate smoothly. This is positioning pressure, not a
    # contrarian rule and not a prediction.
    vals = []
    for x in (pcr_oi, pcr_vol):
        if x is not None and x > 0:
            vals.append(-_tanh(math.log(x), 0.55))
    return {"pressure": round(float(np.mean(vals)) if vals else 0.0, 6),
            "put_call_oi_ratio": pcr_oi, "put_call_volume_ratio": pcr_vol,
            "observed": bool(vals), "note": "Put/call pressure is normalized context, not an automatic contrarian signal."}


def _gamma_squeeze_score(market: Dict[str, Any], gamma_ratio: float, instability: float) -> Dict[str, Any]:
    """Composite, descriptive-only Gamma Squeeze indicator (SpotGamma parity, v1.33).

    Reuses two things that already exist and are already computed elsewhere,
    never recomputed here: the instability/gamma_ratio this same function already
    derives from market_state_field's own stability model, and the dynamic Zero
    Gamma flip's proximity/velocity/acceleration from engine.py's
    dynamic_gamma_flip_history()/_flip_dynamics(). This function only combines
    them into one score; it introduces no new market-data calculation.

    A negative-gamma regime near a fast-moving Zero Gamma flip is the classic
    setup for dealer hedging to amplify rather than dampen a move -- this score
    describes how close conditions are to that setup. It does not predict that a
    squeeze will occur and it never feeds Scanner direction.
    """
    regime = str(market.get("regime") or "").upper()
    negative_gamma = "NEGATIVE" in regime
    crossing = bool(market.get("gamma_flip_crossing", False))
    proximity_label = str(market.get("flip_proximity_label") or "UNKNOWN").upper()
    velocity_label = str(market.get("flip_velocity_label") or "UNKNOWN").upper()
    acceleration_label = str(market.get("flip_acceleration_label") or "UNKNOWN").upper()
    if crossing:
        proximity_score = {"VERY HIGH": 100.0, "HIGH": 70.0, "MEDIUM": 40.0, "LOW": 15.0}.get(proximity_label, 30.0)
        velocity_score = {"FAST": 100.0, "MOVING": 55.0, "STABLE": 15.0}.get(velocity_label, 25.0)
        accel_bonus = 10.0 if acceleration_label == "ACCELERATING" else 0.0
        flip_basis = "DYNAMIC_ROOT"
    else:
        # No genuine NetGEX(S)=0 root: the flip level is a diagnostic fallback, not
        # a tradable level (same distinction engine.py itself makes), so it must
        # never contribute at full weight here either.
        proximity_score, velocity_score, accel_bonus = 20.0, 10.0, 0.0
        flip_basis = "DIAGNOSTIC_NO_TRUE_ROOT"
    score = float(np.clip(
        0.38 * float(np.clip(instability, 0.0, 100.0)) +
        (18.0 if negative_gamma else 0.0) +
        0.26 * proximity_score + 0.18 * velocity_score + accel_bonus,
        0.0, 100.0))
    label = ("ACTIVE SQUEEZE RISK" if score >= 75 else "ELEVADO" if score >= 55
             else "WATCH" if score >= 35 else "BAJO")
    return {
        "ready": True, "score": round(score, 1), "label": label,
        "components": {"instability": round(float(instability), 1), "negative_gamma_regime": negative_gamma,
                       "gamma_ratio": round(float(gamma_ratio), 4), "flip_proximity": proximity_label,
                       "flip_velocity": velocity_label, "flip_acceleration": acceleration_label,
                       "flip_basis": flip_basis},
        "authority": "DESCRIPTIVE_ONLY_SCANNER_REMAINS_AUTHORITY",
        "model_risk": ("Score compuesto de inestabilidad + dinámica del Zero Gamma dinámico. Describe "
                      "condiciones asociadas a squeezes de gamma; no predice que uno vaya a ocurrir ni "
                      "cambia dirección o autoridad del Scanner."),
    }


def build_market_state_field(
    scanner: Dict[str, Any] | None,
    market: Dict[str, Any] | None,
    flow: Dict[str, Any] | None,
    volatility: Dict[str, Any] | None,
    tape: Dict[str, Any] | None,
    *,
    symbol: str = "DIA",
    dealer: Dict[str, Any] | None = None,
    external: Dict[str, Any] | None = None,
    positioning: Dict[str, Any] | None = None,
    macro: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a bounded, auditable state vector. No field can flip Scanner direction."""
    sc = scanner or {}; mk = market or {}; fl = flow or {}; vol = volatility or {}; tp = tape or {}
    opt = _options_field(mk, vol, symbol)
    kin = _flow_field(fl, tp)
    deal = _dealer_field(dealer or {})
    breadth = _breadth_field(external or {})
    pos = _positioning_field(positioning or {})

    # Directional context deliberately excludes Gamma sign. Gamma is friction/stability.
    dir_components = {
        "delta": (opt["delta_ratio"], 0.30),
        "opra_kinetic": (kin["kinetic"], 0.30),
        "breadth": (breadth["breadth"], 0.18 if breadth["observed"] else 0.0),
        "dealer_pressure": (deal["pressure"], 0.12 if deal["estimated"] else 0.0),
        "positioning": (pos["pressure"], 0.10 if pos["observed"] else 0.0),
    }
    denom = sum(w for _, w in dir_components.values()) or 1.0
    directional = _clip(sum(v * w for v, w in dir_components.values()) / denom)

    macro_stress = _clip((_f((((macro or {}).get("asset_context") or {}).get("score")),
                            _f(((macro or {}).get("stress") or {}).get("score"), 0.0)) or 0.0) / 100.0,
                         0.0, 1.0)
    vol_regime = str((vol or {}).get("regime") or "").upper()
    vol_risk = 1.0 if "EXP" in vol_regime or "HIGH" in vol_regime else 0.5 if "ELEV" in vol_regime else 0.15
    stability = float(np.clip(opt["stability"] - 14.0 * macro_stress, 0.0, 100.0))
    instability = 100.0 - stability

    d = str(sc.get("direction") or "WAITING").upper()
    sign = 1.0 if d == "BUY" else -1.0 if d == "SELL" else 0.0
    alignment = float(np.clip(50.0 + 50.0 * sign * directional, 0.0, 100.0)) if sign else 50.0

    z = sc.get("zone") or {}
    spot = _f(mk.get("spot")); lo = _f(z.get("low")); hi = _f(z.get("high"))
    em = _f((vol or {}).get("expected_move"))
    if spot is not None and lo is not None and hi is not None:
        dist = 0.0 if lo <= spot <= hi else min(abs(spot - lo), abs(spot - hi))
        scale = max((em or 0.0) * 0.20, abs(spot) * 0.0010, 1e-6)
        proximity = float(np.clip(math.exp(-dist / scale) * 100.0, 0.0, 100.0))
    else:
        proximity = 35.0

    tape_alignment = kin["tape_alignment"]
    tape_score = float(np.clip(50.0 + 50.0 * tape_alignment, 0.0, 100.0))
    risk_penalty = float(np.clip(0.55 * macro_stress + 0.45 * vol_risk, 0.0, 1.0))
    # Hyper-confluence index: measures state co-location/alignment. It is emphatically
    # not a calibrated chance of profit.
    confluence = float(np.clip(
        0.42 * alignment + 0.24 * proximity + 0.18 * tape_score + 0.16 * (100.0 * (1.0 - risk_penalty)),
        0.0, 100.0))

    if str(kin.get("tape_state")) == "ABSORBED":
        phase = "ABSORPTION"
    elif opt["gamma_ratio"] > 0.10 and instability < 48:
        phase = "CONTAINMENT"
    elif opt["gamma_ratio"] < -0.10 and (abs(kin["kinetic"]) > 0.22 or instability >= 60):
        phase = "ACCELERATION"
    elif abs(directional) < 0.12:
        phase = "BALANCED"
    else:
        phase = "TRANSITION"

    # Pick only the strongest explanatory facts. These are fed into the existing
    # compact synthesis; no new dashboard section is created.
    factors = [
        (abs(opt["delta_ratio"]), f"Delta contextual {'acompaña' if sign*opt['delta_ratio']>=0 else 'contradice'} al Scanner ({opt['delta_ratio']:+.2f})" if sign else f"Delta contextual {opt['delta_ratio']:+.2f}"),
        (abs(kin["kinetic"]), f"Flujo OPRA cinético {'acompaña' if sign*kin['kinetic']>=0 else 'contradice'} ({kin['kinetic']:+.2f})" if sign else f"Flujo OPRA cinético {kin['kinetic']:+.2f}"),
        (abs(opt["gamma_ratio"]), f"Gamma {'amortigua/contiene' if opt['gamma_ratio']>0 else 'aumenta inestabilidad'} ({opt['gamma_ratio']:+.2f})"),
        (abs(breadth["breadth"]), f"Breadth relacionado {'acompaña' if sign*breadth['breadth']>=0 else 'contradice'} ({breadth['breadth']:+.2f})" if breadth["observed"] and sign else ""),
        (abs(deal["pressure"]), f"Hedge-pressure estimado {'acompaña' if sign*deal['pressure']>=0 else 'contradice'} · conf {deal['confidence']:.0f}%" if deal["estimated"] and sign else ""),
    ]
    top = [txt for mag, txt in sorted(factors, key=lambda x: x[0], reverse=True) if txt][:3]
    gamma_squeeze = _gamma_squeeze_score(mk, opt["gamma_ratio"], instability)

    return {
        "ready": bool(sc.get("ready") or mk),
        "role": "INTERNAL_CONTEXT_ONLY",
        "direction_authority": "SCANNER_ONLY",
        "directional_context": round(directional * 100.0, 1),
        "scanner_alignment": round(alignment, 1),
        "confluence_index": round(confluence, 1),
        "confluence_is_probability": False,
        "phase": phase,
        "stability": round(stability, 1),
        "instability": round(instability, 1),
        "macro_stress": round(macro_stress * 100.0, 1),
        "zone_proximity": round(proximity, 1),
        "options": opt,
        "flow": kin,
        "dealer": deal,
        "breadth": breadth,
        "positioning": pos,
        "gamma_squeeze": gamma_squeeze,
        "top_factors": top,
        "formula": "STATE = directional(Delta+OPRA+Breadth+Dealer+Positioning) + friction(Gamma) + higher-order(Vanna/Charm/Speed/Color) + risk(Vol+Macro); Scanner remains authority.",
        "note": "Índices internos de estado/confluencia; NO son probabilidad calibrada ni garantía de resultado.",
    }
