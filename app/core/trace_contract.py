"""Strict, fail-soft data contracts for TRACE payloads.

The renderer must never infer that a truthy value is an array.  This module keeps
collection/object shapes invariant across LIVE, DEMO and REPLAY transitions while
recording any upstream contract violation for telemetry.  It does not alter Scanner
direction, market data, calibration, persistent memory or quantitative calculations.
"""

from __future__ import annotations

from typing import Any, Dict

TRACE_CONTRACT_VERSION = "1.0"


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, tuple):
        return "tuple"
    return type(value).__name__


def _array(value: Any, field: str, violations: list[Dict[str, str]]) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    violations.append({"field": field, "expected": "array", "received": _kind(value)})
    return []


def _object(value: Any, field: str, violations: list[Dict[str, str]]) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    violations.append({"field": field, "expected": "object", "received": _kind(value)})
    return {}


def normalize_trace_pulse_contract(payload: Any, *, source: str = "trace_pulse") -> Dict[str, Any]:
    violations: list[Dict[str, str]] = []
    if not isinstance(payload, dict):
        violations.append({"field": "$", "expected": "object", "received": _kind(payload)})
        out: Dict[str, Any] = {"ready": False, "reason": "TRACE_CONTRACT_INVALID_ROOT"}
    else:
        out = dict(payload)

    rows = _array(out.get("rows"), "rows", violations)
    clean_rows = []
    for idx, row in enumerate(rows):
        if isinstance(row, dict):
            clean_rows.append(row)
        else:
            violations.append({"field": f"rows[{idx}]", "expected": "object", "received": _kind(row)})
    out["rows"] = clean_rows
    out["gamma_delta_interaction"] = _object(
        out.get("gamma_delta_interaction"), "gamma_delta_interaction", violations
    )

    out["contract"] = {
        "name": "TRACE_PULSE",
        "version": TRACE_CONTRACT_VERSION,
        "source": source,
        "valid": not violations,
        "violations": violations,
    }
    return out


def normalize_nextgen_trace_contract(payload: Any, *, source: str = "nextgen_trace") -> Dict[str, Any]:
    violations: list[Dict[str, str]] = []
    if not isinstance(payload, dict):
        violations.append({"field": "$", "expected": "object", "received": _kind(payload)})
        out: Dict[str, Any] = {"ready": False, "reason": "TRACE_CONTRACT_INVALID_ROOT"}
    else:
        out = dict(payload)

    for field in ("candles", "option_prints", "levels"):
        items = _array(out.get(field), field, violations)
        out[field] = [x for x in items if isinstance(x, dict)]
        if len(out[field]) != len(items):
            violations.append({"field": field, "expected": "array<object>", "received": "mixed_array"})

    profiles = normalize_trace_pulse_contract(out.get("profiles"), source=f"{source}.profiles")
    nested = (profiles.get("contract") or {}).get("violations") or []
    for item in nested:
        violations.append({
            "field": f"profiles.{item.get('field', '$')}",
            "expected": str(item.get("expected") or "valid"),
            "received": str(item.get("received") or "invalid"),
        })
    out["profiles"] = profiles

    for field in ("decision", "market_state", "quality", "dealer_intelligence", "native_options_structure", "provenance", "model_risk", "hiro", "key_levels_report"):
        out[field] = _object(out.get(field), field, violations)

    hiro = dict(out["hiro"] or {})
    hiro["points"] = _array(hiro.get("points"), "hiro.points", violations)
    out["hiro"] = hiro

    market_state = dict(out["market_state"] or {})
    market_state["top_factors"] = _array(market_state.get("top_factors"), "market_state.top_factors", violations)
    out["market_state"] = market_state

    model_risk = dict(out["model_risk"] or {})
    model_risk["scenarios"] = _array(model_risk.get("scenarios"), "model_risk.scenarios", violations)
    out["model_risk"] = model_risk

    out["contract"] = {
        "name": "TRACE_NEXTGEN",
        "version": TRACE_CONTRACT_VERSION,
        "source": source,
        "valid": not violations,
        "violations": violations,
    }
    return out
