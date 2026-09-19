"""ITM QUANT tool contracts and compact structured digests.

This module is intentionally presentation/runtime focused.  It does not calculate a
new BUY/SELL direction and cannot override Scanner.  A Tool contract describes how a
module is read, rendered, refreshed and exposed to Sophia without coupling the UI to
backend implementation details.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
from datetime import datetime, timezone
import hashlib
import json
import math


@dataclass(frozen=True)
class ToolSpec:
    id: str
    label: str
    section: str
    renderer: str
    snapshot_source: str
    live_channel: str | None = None
    worker_jobs: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    digest_fields: tuple[str, ...] = ()
    authority: str = "CONTEXT_ONLY"
    priority: int = 2


_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("command", "Operativa", "command", "FINANCIAL_MULTI_PANE", "STATE", "HOT_STATE",
             capabilities=("SNAPSHOT","LIVE","DIGEST","REPLAY"), authority="CONTEXT_ONLY", priority=0),
    ToolSpec("scanner", "Quant Scanner", "scanner", "FINANCIAL_PATH", "STATE", "SCANNER_STATE",
             capabilities=("SNAPSHOT","LIVE","DIGEST","REPLAY","QUALITY_FLAGS"),
             digest_fields=("direction","evidence_score","edge_state","zone","target1","target2","invalidation"),
             authority="SOLE_DIRECTIONAL_AUTHORITY", priority=0),
    ToolSpec("trace", "TRACE PRO", "trace", "TRACE_NATIVE", "NEXTGEN_TRACE", "TICKS_BIN",
             worker_jobs=("TRACE_VISIBLE_RANGE",), filters=("timeframe","price_mode","profiles","heatmap"),
             capabilities=("SNAPSHOT","LIVE","DIGEST","REPLAY","BINARY","COALESCED"), priority=0),
    ToolSpec("flow", "Flujo Inusual", "flow", "FINANCIAL_MULTI_PANE", "CHARTS", "FLOW_EVENTS",
             worker_jobs=("CUMULATIVE_SERIES",), filters=("expiry_window","moneyness","trade_side"),
             capabilities=("SNAPSHOT","LIVE","DIGEST","DRILLDOWN","COALESCED"), priority=1),
    ToolSpec("netdrift", "Net Drift · Quant Data", "netdrift", "FINANCIAL_MULTI_PANE", "CHARTS", "DRIFT_EVENTS",
             worker_jobs=("CUMULATIVE_SERIES","DERIVATIVE_SERIES"), filters=("expiry_window",),
             capabilities=("SNAPSHOT","LIVE","DIGEST","DRILLDOWN","COALESCED"), priority=1),
    ToolSpec("chain", "Cadena cuantitativa", "chain", "INSTITUTIONAL_2D", "CHARTS", "CHAIN_STATE",
             worker_jobs=("ROBUST_SCALE",), filters=("metric","expiry_window"),
             capabilities=("SNAPSHOT","DIGEST","QUALITY_FLAGS"), priority=2),
    ToolSpec("exposure", "Exposure by Strike", "exposure", "INSTITUTIONAL_2D", "CHARTS", "STRUCTURE_STATE",
             worker_jobs=("ROBUST_SCALE",), filters=("metric","expiry_window"),
             capabilities=("SNAPSHOT","DIGEST","DRILLDOWN","SURFACE_LINK"), priority=1),
    ToolSpec("gexmatrix", "GEX Matrix", "gexmatrix", "MATRIX", "CHARTS", "STRUCTURE_STATE",
             worker_jobs=("ROBUST_SCALE","MIGRATION_DIFFERENCE"), filters=("metric","baseline","label_threshold"),
             capabilities=("SNAPSHOT","DIGEST","DRILLDOWN","SURFACE_LINK","MIGRATION"), priority=1),
    ToolSpec("gamma_migration", "Gamma Migration", "gexmatrix", "INTERVAL_MAP", "STRUCTURE_STATE", "STRUCTURE_STATE",
             worker_jobs=("MIGRATION_DIFFERENCE","VISIBLE_BUBBLE_SCALE"), filters=("mode","expiry_window"),
             capabilities=("SNAPSHOT","LIVE","DIGEST","DRILLDOWN","VALUE_DIFFERENCE"), priority=1),
    ToolSpec("positioning", "Positioning", "positioning", "INSTITUTIONAL_2D", "CHARTS", "STRUCTURE_STATE",
             worker_jobs=("ROBUST_SCALE",), filters=("metric","expiry_window"), capabilities=("SNAPSHOT","DIGEST")),
    ToolSpec("volatility", "Volatility Engine", "vol", "INSTITUTIONAL_2D", "CHARTS", "VOL_STATE",
             worker_jobs=("ROBUST_SCALE",), filters=("expiry_window",), capabilities=("SNAPSHOT","DIGEST","SURFACE_LINK")),
    ToolSpec("surface", "Surface 3D", "surface", "WEBGPU_WEBGL", "NEXTGEN_SURFACE", "SURFACE_BIN",
             worker_jobs=("SURFACE_NORMALIZE",), filters=("field","option_view","render_style"),
             capabilities=("SNAPSHOT","LIVE","DIGEST","BINARY","GPU","COALESCED"), priority=1),
    ToolSpec("prints", "Large Prints", "prints", "FINANCIAL_MULTI_PANE", "CHARTS", "PRINT_EVENTS",
             worker_jobs=("ROBUST_SCALE",), capabilities=("SNAPSHOT","LIVE","DIGEST","DRILLDOWN","LIQUIDITY_ZONES"), priority=1),
    ToolSpec("macro", "Macro", "macro", "INSTITUTIONAL_2D", "CHARTS", "MACRO_STATE",
             capabilities=("SNAPSHOT","DIGEST","QUALITY_FLAGS"), priority=3),
    ToolSpec("equity_hub", "Equity Hub", "equity_hub", "INSTITUTIONAL_2D", "CHARTS", "STRUCTURE_STATE",
             worker_jobs=("ROBUST_SCALE",), capabilities=("SNAPSHOT","DIGEST","SURFACE_LINK")),
    ToolSpec("conditional_outcomes", "Conditional Outcomes", "scanner", "ANALYTIC_CARDS", "STATE", "SCANNER_STATE",
             capabilities=("SNAPSHOT","DIGEST","QUALITY_FLAGS","CALIBRATION"), authority="EVIDENCE_ONLY", priority=1),
)

TOOL_REGISTRY: dict[str, ToolSpec] = {s.id: s for s in _SPECS}


def registry_payload() -> dict[str, Any]:
    return {
        "schema": "ITMQ_TOOL_REGISTRY_V1",
        "authority_rule": "SCANNER_SOLE_DIRECTIONAL_AUTHORITY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tools": [asdict(s) for s in _SPECS],
    }


def _finite(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _compact(value: Any, *, depth: int = 0, max_items: int = 18) -> Any:
    if depth > 3:
        return None
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:max_items]:
            if v is None:
                continue
            out[str(k)] = _compact(v, depth=depth + 1, max_items=max_items)
        return out
    if isinstance(value, (list, tuple)):
        return [_compact(v, depth=depth + 1, max_items=max_items) for v in list(value)[:max_items]]
    if isinstance(value, (str, bool, int)):
        return value
    x = _finite(value, None)
    return x if x is not None else str(value)[:160]


def quality_flags(state: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    gate = state.get("publication_gate") or ((state.get("data_quality_report") or {}).get("circuito_frescura") or {})
    if isinstance(gate, dict) and gate.get("publicar_permitido") is not True:
        flags.append({"code":"CONTEXT_ONLY","severity":"WARN","detail":str(gate.get("motivo") or gate.get("reason") or "frescura no habilitada")[:180]})
    dq = state.get("data_quality_report") or state.get("data_quality") or {}
    for code, src in (
        ("OI_FRESHNESS", (dq.get("oi_structural_freshness") if isinstance(dq,dict) else None)),
        ("GREEKS_PROVENANCE", (dq.get("greeks_provenance") if isinstance(dq,dict) else None)),
    ):
        if isinstance(src, dict):
            status = str(src.get("status") or src.get("state") or "").upper()
            if status and status not in {"OK","LIVE","READY","FRESH"}:
                flags.append({"code":code,"severity":"INFO" if status in {"EXPECTED_IDLE","STRUCTURAL"} else "WARN","detail":status})
    return flags[:12]


def _scanner_digest(state: dict[str, Any]) -> dict[str, Any]:
    sc = state.get("scanner") or {}
    return {
        "ready": bool(sc.get("ready")), "direction": sc.get("direction"),
        "strength": _finite(sc.get("strength", sc.get("evidence_score"))), "edge_state": sc.get("edge_state"),
        "zone": _compact(sc.get("zone") or {}), "entry": _finite(sc.get("entry")),
        "target1": _finite(sc.get("target1")), "target2": _finite(sc.get("target2")),
        "invalidation": _finite(sc.get("invalidation")), "probability": _compact(sc.get("probability") or {}),
        "authority": "SOLE_DIRECTIONAL_AUTHORITY",
    }


def build_tool_digest(tool_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded structured digest for UI/Sophia/alerts.

    It intentionally reads cached state only; no provider/network work is allowed here.
    """
    tool_id = str(tool_id or "").lower().strip()
    spec = TOOL_REGISTRY.get(tool_id)
    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    base: dict[str, Any] = {
        "tool_id": tool_id, "symbol": symbol, "as_of": state.get("as_of") or state.get("last_refresh_ec"),
        "quality_flags": quality_flags(state), "authority": spec.authority if spec else "CONTEXT_ONLY",
    }
    if tool_id in {"scanner","conditional_outcomes"}:
        base["scanner"] = _scanner_digest(state)
        base["calibration"] = _compact(state.get("calibration") or {})
        base["decision_intelligence"] = _compact(state.get("decision_intelligence") or {})
    elif tool_id in {"trace","command"}:
        base["scanner"] = _scanner_digest(state)
        base["trace_orderflow"] = _compact(state.get("trace_orderflow") or {})
        base["market_truth"] = _compact(state.get("market_truth") or {})
    elif tool_id == "flow":
        base["flow"] = _compact(state.get("flow_pro") or state.get("flow_summary") or {})
    elif tool_id == "netdrift":
        base["net_drift"] = _compact(state.get("net_drift_summary") or {})
        base["flow"] = _compact(state.get("flow_pro") or {})
    elif tool_id in {"gexmatrix","gamma_migration","exposure","chain","positioning","equity_hub"}:
        base["spot"] = _finite(state.get("spot"))
        base["gamma"] = _compact({
            "center": state.get("gamma_center"), "flip": state.get("gamma_flip"),
            "migration": state.get("gamma_migration"), "pressure": state.get("gamma_pressure"),
        })
        base["delta"] = _compact({"center":state.get("delta_center"),"migration":state.get("delta_migration")})
        base["structural_intelligence"] = _compact(state.get("structural_intelligence") or {})
    elif tool_id == "volatility":
        base["volatility"] = _compact(state.get("volatility") or state.get("vol") or {})
    elif tool_id == "prints":
        base["large_prints"] = _compact(state.get("large_prints") or {})
        base["liquidity_zones"] = _compact(state.get("liquidity_zones") or {})
    elif tool_id == "macro":
        base["macro"] = _compact(state.get("macro") or {})
    elif tool_id == "surface":
        base["surface"] = _compact(state.get("surface_digest") or state.get("structural_intelligence") or {})
    else:
        base["state"] = _compact(state)
    return base


def digest_signature(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",",":"), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=12).hexdigest()


def section_tool(section_id: str) -> str | None:
    s = str(section_id or "").replace("section-", "").lower()
    alias = {"vol":"volatility","gexmatrix":"gexmatrix","netdrift":"netdrift","prints":"prints","flow":"flow","trace":"trace","command":"command","scanner":"scanner","chain":"chain","exposure":"exposure","positioning":"positioning","surface":"surface","macro":"macro","equityhub":"equity_hub"}
    return alias.get(s)
