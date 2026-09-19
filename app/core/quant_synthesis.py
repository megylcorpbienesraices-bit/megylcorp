"""Operational synthesis layer for ITM QUANT.

This module deliberately does **not** create a second trading signal.  The Quant
Scanner remains the only directional authority.  The purpose of the layer is to
consume the outputs that ITM QUANT already computes (Scanner, Tape, data/model
quality, Gamma/Delta context, volatility, flow, dealer research, etc.) and turn
those outputs into one compact operational answer for the dashboard.

Design rules
------------
* Direction is copied from Scanner, never re-voted here.
* Evidence strength is Scanner evidence, not a calibrated probability.
* Tape can confirm/block *timing* but cannot flip direction.
* Data Quality / Model Health can block presentation of an entry, not invent one.
* Uncalibrated model statistics are never presented as win probability.
* The detailed ingredients remain available for Auditor/Research but do not need
  to occupy the trader's primary screen.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable
import math


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _clip(value: Any, lo: float = 0.0, hi: float = 100.0, default: float = 0.0) -> float:
    x = _finite(value, default)
    return float(max(lo, min(hi, x if x is not None else default)))


def _zone(scanner: Dict[str, Any]) -> Dict[str, float | None]:
    z = scanner.get("zone") or {}
    lo = _finite(z.get("low", scanner.get("zone_low")))
    hi = _finite(z.get("high", scanner.get("zone_high")))
    center = _finite(z.get("center"))
    if center is None and lo is not None and hi is not None:
        center = (lo + hi) / 2.0
    return {"low": lo, "center": center, "high": hi}


def _location(spot: float | None, zone: Dict[str, float | None]) -> Dict[str, Any]:
    lo, hi, center = zone.get("low"), zone.get("high"), zone.get("center")
    if spot is None or lo is None or hi is None:
        return {"state": "UNKNOWN", "distance": None, "distance_pct": None}
    if lo <= spot <= hi:
        return {"state": "IN_ZONE", "distance": 0.0, "distance_pct": 0.0}
    boundary = lo if spot < lo else hi
    dist = abs(spot - boundary)
    pct = dist / max(abs(spot), 1e-12) * 100.0
    # The label is descriptive only; it is not a signal threshold.
    state = "NEAR_ZONE" if pct <= 0.12 else "AWAY_FROM_ZONE"
    return {"state": state, "distance": dist, "distance_pct": pct}


def _reason_text(items: Iterable[Dict[str, Any]] | None, limit: int = 2) -> list[str]:
    out: list[str] = []
    for item in list(items or [])[:limit]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if label and detail:
            out.append(f"{label}: {detail}")
        elif label:
            out.append(label)
        elif detail:
            out.append(detail)
    return out


def _action_state(scanner: Dict[str, Any], tape: Dict[str, Any], *,
                  data_quality: float | None, model_health: float | None,
                  location: Dict[str, Any]) -> tuple[str, str]:
    """Translate existing authorities into one timing/action label.

    This function intentionally does not calculate direction or an independent
    edge.  It only reduces existing Scanner + quality gate + Tape timing states.
    """
    if not bool(scanner.get("ready")):
        return "WAIT", "ESPERAR ESTRUCTURA"

    edge = str(scanner.get("edge_state") or "NO EDGE").upper()
    dq = _finite(data_quality)
    mh = _finite(model_health)
    if (dq is not None and dq < 50.0) or (mh is not None and mh < 60.0):
        return "BLOCKED", "BLOQUEADO · CALIDAD"
    if edge == "NO EDGE":
        return "WAIT", "ESPERAR · SIN EDGE"
    if location.get("state") not in {"IN_ZONE", "UNKNOWN"}:
        return "WAIT_ZONE", "ESPERAR ZONA"

    conf = (tape.get("confirmation") or {}) if isinstance(tape, dict) else {}
    ts = str(conf.get("state") or "WAITING").upper()
    if ts == "CONFIRMED" and edge == "ACTIONABLE":
        return "ENTER", "ENTRADA CONFIRMADA"
    if ts in {"REJECTED", "ABSORBED", "CHURN", "EXPIRED"}:
        labels = {
            "REJECTED": "NO ENTRAR · RECHAZADO",
            "ABSORBED": "NO ENTRAR · ABSORCIÓN",
            "CHURN": "NO ENTRAR · SIN GANADOR",
            "EXPIRED": "NO ENTRAR · SIN CONFIRMACIÓN",
        }
        return "BLOCKED", labels[ts]
    if ts == "ARMED":
        return "ARMED", "ARMADO · ESPERAR TAPE"
    if edge == "CAUTION":
        return "CAUTION", "CAUTELA · ESPERAR CONFIRMACIÓN"
    return "WAIT_TAPE", "ESPERAR CONFIRMACIÓN"


def build_quant_synthesis(
    scanner: Dict[str, Any] | None,
    market: Dict[str, Any] | None,
    flow: Dict[str, Any] | None,
    volatility: Dict[str, Any] | None,
    tape: Dict[str, Any] | None,
    *,
    spot: Any = None,
    data_quality: Any = None,
    model_health: Any = None,
    source_health: Dict[str, Any] | None = None,
    dealer: Dict[str, Any] | None = None,
    external: Dict[str, Any] | None = None,
    positioning: Dict[str, Any] | None = None,
    macro: Dict[str, Any] | None = None,
    state_field: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return the single compact result consumed by the primary trading screen."""
    sc = scanner or {}
    mk = market or {}
    fl = flow or {}
    vol = volatility or {}
    tp = tape or {}
    ready = bool(sc.get("ready"))
    direction = str(sc.get("direction") or "WAITING").upper() if ready else "WAITING"
    if direction not in {"BUY", "SELL"}:
        direction = "WAITING"

    z = _zone(sc)
    live_spot = _finite(spot, _finite(mk.get("spot")))
    location = _location(live_spot, z)
    dq = _finite(data_quality)
    mh = _finite(model_health)
    action_code, action_label = _action_state(sc, tp, data_quality=dq, model_health=mh, location=location)

    # The Market State Field is an internal context reducer, never a second signal.
    # A severe conflict may downgrade timing to CAUTION, but it cannot flip BUY/SELL.
    sf = state_field or {}
    sf_align = _finite(sf.get("scanner_alignment"))
    sf_conf = _finite(sf.get("confluence_index"))
    if ready and sf_align is not None and sf_align < 25.0 and action_code in {"ENTER", "ARMED", "WAIT_TAPE"}:
        action_code, action_label = "CAUTION", "CAUTELA · CONFLICTO CUANT"

    evidence = _clip(sc.get("evidence_score"), default=0.0) if ready else 0.0
    # IMPORTANT: this is the Scanner's internal structural evidence score.  It is
    # deliberately *not* transformed into a probability here.
    strength_label = "FUERZA ESTRUCTURAL"

    reasons = _reason_text(sc.get("reasons"), 2)
    contradictions = _reason_text(sc.get("contradictions"), 1)
    state_reasons: list[str] = []
    if sf:
        phase = str(sf.get("phase") or "TRANSITION")
        if sf_conf is not None:
            state_reasons.append(f"Estado {phase} · confluencia contextual {sf_conf:.0f}/100 (no prob.)")
        state_reasons.extend([str(x) for x in (sf.get("top_factors") or []) if str(x).strip()][:2])
    # Prefer the all-engine state summary when available; fall back to Scanner reasons.
    why = (state_reasons[:2] + reasons[:1]) if state_reasons else reasons
    if contradictions and len(why) < 3:
        why.append(f"Riesgo: {contradictions[0]}")
    if not why:
        why = ["Esperando suficiente estructura cuantitativa para resumir la tesis."]

    conf = (tp.get("confirmation") or {}) if isinstance(tp, dict) else {}
    tape_state = str(conf.get("state") or "WAITING").upper()
    tape_progress = _finite(conf.get("progress_pct"))

    gate = sc.get("edge_gate") or {}
    calibrated = bool(gate.get("calibration_ready"))
    p_t1 = _finite(gate.get("probability_t1_first")) if calibrated else None
    probability = p_t1 if p_t1 is not None and 0.0 <= p_t1 <= 1.0 else None

    if direction == "BUY":
        dir_text = "COMPRA"
    elif direction == "SELL":
        dir_text = "VENTA"
    else:
        dir_text = "ESPERAR"

    if direction in {"BUY", "SELL"}:
        verdict = f"{dir_text} · {action_label}"
    else:
        verdict = action_label

    # Hidden provenance: demonstrates that the primary result is the reduction of
    # the existing engine, not a new visible dashboard/module.
    processed = {
        "scanner": bool(sc),
        "gamma_delta": bool(mk),
        "flow": bool(fl),
        "volatility": bool(vol),
        "tape": bool(tp),
        "data_quality": dq is not None,
        "model_health": mh is not None,
        "source_health": bool(source_health),
        "dealer_research": bool(dealer),
        "cross_asset": bool(external),
        "positioning": bool(positioning),
        "macro": bool(macro),
        "market_state_field": bool(sf),
    }

    return {
        "ready": ready,
        "direction": direction,
        "direction_text": dir_text,
        "direction_source": "SCANNER_ONLY",
        "action_code": action_code,
        "action": action_label,
        "verdict": verdict,
        "strength": round(evidence, 1),
        "strength_label": strength_label,
        "strength_is_probability": False,
        "spot": live_spot,
        "location": location,
        "zone": z,
        "target1": _finite(sc.get("target1")),
        "target2": _finite(sc.get("target2")),
        "invalidation": _finite(sc.get("invalidation")),
        "scenario": str(sc.get("scenario_type") or "SCANNER"),
        "edge_state": str(sc.get("edge_state") or "WAITING").upper(),
        "tape_state": tape_state,
        "tape_progress_pct": tape_progress,
        "data_quality": dq,
        "model_health": mh,
        "calibration_status": "CALIBRATED" if calibrated else "COLLECTING",
        "probability_t1_first": probability,
        "why": why[:3],
        "risk_note": contradictions[0] if contradictions else None,
        "market_phase": sf.get("phase") if sf else None,
        "context_alignment": sf_align,
        "context_confluence": sf_conf,
        "context_confluence_is_probability": False,
        "state_field_role": sf.get("role") if sf else None,
        "processed_inputs": processed,
        "processed_input_count": sum(1 for v in processed.values() if v),
        "note": "SÍNTESIS INTERNA · Scanner decide; Market State Field sintetiza mecánica/flujo/riesgo; Tape temporiza. Ningún índice contextual es probabilidad.",
    }
