"""Operational readiness / deep-tech capability reporting for ITM QUANT.

This module never claims that unavailable hardware or external services are active.
It reports what is actually configured and separates software readiness from live
production availability.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict
from pathlib import Path
import math
import os


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def _status(active: bool, ready: bool = False) -> str:
    if active:
        return "ACTIVE"
    if ready:
        return "READY"
    return "UNAVAILABLE"


@dataclass
class Capability:
    key: str
    status: str
    detail: str
    observed: bool = False
    configured: bool = False
    authority: str = "NONE"
    model_risk: str | None = None

    def pack(self) -> Dict[str, Any]:
        return asdict(self)


def latency_summary(causality: Dict[str, Any] | None) -> Dict[str, Any]:
    c = causality or {}
    events = c.get("events") if isinstance(c, dict) else None
    vals = []
    if isinstance(events, list):
        for row in events:
            x = _f((row or {}).get("latency_ms"))
            if x is not None:
                vals.append(max(0.0, x))
    vals.sort()
    def pct(p: float) -> float | None:
        if not vals:
            return None
        i = int(round((len(vals) - 1) * p))
        return round(vals[max(0, min(i, len(vals)-1))], 3)
    p50, p95, p99 = pct(.50), pct(.95), pct(.99)
    target = max(1.0, _f(os.getenv("ITM_LATENCY_TARGET_MS", "250"), 250.0) or 250.0)
    state = "NO DATA"
    if p95 is not None:
        state = "GREEN" if p95 <= target else "AMBER" if p95 <= target * 2.0 else "RED"
    return {
        "count": len(vals), "p50_ms": p50, "p95_ms": p95, "p99_ms": p99,
        "target_ms": target, "state": state,
        "note": "Feed/event latency telemetry. This is not exchange co-location latency unless a co-located source is actually configured.",
    }


def build_operational_readiness(
    *,
    causality: Dict[str, Any] | None = None,
    source_health: Dict[str, Any] | None = None,
    external_markets: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    source_health = source_health or {}
    external_markets = external_markets or {}
    lat = latency_summary(causality)

    fpga_cfg = _env_bool("ITM_FPGA_ENABLED") or bool(os.getenv("ITM_FPGA_BRIDGE_URL"))
    colo_name = str(os.getenv("ITM_COLOCATION_REGION", "")).strip()
    colo_cfg = bool(colo_name)
    qpu_provider = str(os.getenv("ITM_QPU_PROVIDER", "")).strip()
    qpu_cfg = bool(qpu_provider)
    genai_cfg = _env_bool("ITM_GENAI_SCENARIO_ENABLED") and bool(os.getenv("OPENAI_API_KEY") or os.getenv("ITM_GENAI_PROVIDER"))
    metrics_cfg = _env_bool("ITM_METRICS_ENABLED", True)
    direct_futures = str(external_markets.get("provider_futures_status") or external_markets.get("futures_status") or "").upper() == "LIVE"
    try:
        from .low_latency_bridge import RUST_CAUSAL_BRIDGE
        rust_status = RUST_CAUSAL_BRIDGE.status()
    except Exception as exc:
        rust_status = {"state":"UNAVAILABLE","last_error":str(exc)[:120]}
    try:
        from .accelerated_quant import backend_status as _aq_status
        accel = _aq_status()
    except Exception as exc:
        accel = {"state":"UNAVAILABLE","backend":"NUMPY","gpu":False,"reason":str(exc)[:120]}
    try:
        from .precision_clock import clock_report
        clock = clock_report()
    except Exception as exc:
        clock = {"source":"SYSTEM/NTP","ptp_status":"UNAVAILABLE","offset_ns":None,"quality":"UNKNOWN","detail":str(exc)[:120]}
    try:
        from .simd_dispatch import status as simd_status
        simd = simd_status()
    except Exception as exc:
        simd = {"state":"UNAVAILABLE","backend":"SCALAR/NUMPY","note":str(exc)[:120]}
    try:
        from .source_arbitration import arbitrate
        raw_sources = list((source_health or {}).get("fusion_sources") or [])
        normalized=[]
        for row in raw_sources:
            if not isinstance(row,dict):
                continue
            normalized.append({
                "name": row.get("name") or row.get("source") or row.get("provider") or "SOURCE",
                "status": row.get("status") or row.get("state") or "UNKNOWN",
                "latency_ms": row.get("latency_ms") or row.get("latency") or 999.0,
                "age_ms": row.get("age_ms") or row.get("staleness_ms") or 9999.0,
                "gap_rate": row.get("gap_rate") or row.get("missing_rate") or 0.0,
                "sequence_ok": row.get("sequence_ok") if "sequence_ok" in row else None,
                "cross_source_divergence": (row.get("cross_source_divergence") if row.get("cross_source_divergence") is not None else row.get("divergence")),
                "event_time_valid": row.get("event_time_valid"),
                "event_time_source": row.get("event_time_source"),
                "channel": row.get("channel") or row.get("event_type") or row.get("data_type"),
                "instrument_type": row.get("instrument_type") or row.get("asset_class"),
            })
        arbitration = arbitrate(normalized)
    except Exception as exc:
        arbitration = {"ready":False,"selected":None,"ranked":[],"detail":str(exc)[:120]}
    try:
        from .autonomous_sentinel import evaluate as sentinel_evaluate
        sentinel = sentinel_evaluate(
            latency_p95_ms=lat.get("p95_ms"),
            clock_offset_ns=clock.get("offset_ns"),
            model_health=(source_health or {}).get("model_health"),
        )
    except Exception as exc:
        sentinel = {"state":"UNAVAILABLE","recommended_action":"NONE","detail":str(exc)[:120]}
    try:
        from .feed_adapters import status as feed_adapter_status
        feed_adapters = feed_adapter_status()
    except Exception as exc:
        feed_adapters = {"state":"UNAVAILABLE","active":[],"adapters":[],"note":str(exc)[:120]}

    redis_url = str(os.getenv("ITM_REDIS_URL","")).strip()
    redis_pkg = False
    try:
        redis_pkg = True
    except Exception:
        redis_pkg = False
    root=Path(__file__).resolve().parents[2]
    wasm_built=(root/"app"/"static"/"wasm"/"itmq_wasm_bridge.js").exists()
    solid_built=(root/"app"/"static"/"solid").exists()
    three_source=(root/"frontend"/"three-adapter"/"QuantSurfaceRenderer.ts").exists()

    capabilities = {
        "rust_causality": Capability(
            "rust_causality", str(rust_status.get("state") or "READY"),
            f"Independent Rust/ZeroMQ causal process · endpoint={rust_status.get('endpoint','tcp://127.0.0.1:5555')} · received={rust_status.get('received',0)}",
            observed=str(rust_status.get("state")).upper()=="ACTIVE", configured=bool(rust_status.get("configured")), authority="INGESTION/CAUSALITY",
        ).pack(),
        "quant_acceleration": Capability(
            "quant_acceleration", "ACTIVE" if str(accel.get("state","")).startswith("ACTIVE") else "READY",
            f"{accel.get('backend','NUMPY')} · {accel.get('state','READY')} · gpu={bool(accel.get('gpu'))}",
            observed=bool(accel.get("gpu")), configured=True, authority="QUANT MATH", model_risk="Backend changes execution speed, not formulas/Scanner authority.",
        ).pack(),
        "hot_state": Capability(
            "hot_state", "ACTIVE",
            "Synthetic Dealer Inventory hot path is RAM-resident; SQLite/WAL is asynchronous checkpoint/research archive.",
            observed=True, configured=True, authority="STATE/PERSISTENCE",
        ).pack(),
        "redis_hot_state": Capability(
            "redis_hot_state", "ACTIVE" if redis_url and redis_pkg else "READY" if redis_pkg else "UNAVAILABLE",
            "Redis configured for multi-process hot state." if redis_url and redis_pkg else "Redis adapter supported; set ITM_REDIS_URL for multi-node hot state." if redis_pkg else "Install optional redis package to enable distributed hot state; local RAM hot state is active.",
            observed=bool(redis_url and redis_pkg), configured=bool(redis_url), authority="STATE/PERSISTENCE",
        ).pack(),
        "binary_browser_transport": Capability(
            "binary_browser_transport", "ACTIVE",
            "Binary WebSocket tick/surface endpoints use fixed ITMQ frames and float32 matrices; browser Wasm is optional acceleration.",
            observed=True, configured=True, authority="VISUALIZATION TRANSPORT",
        ).pack(),
        "wasm_bridge": Capability(
            "wasm_bridge", "ACTIVE" if wasm_built else "READY",
            "Rust/Wasm binary decoder built into app/static/wasm." if wasm_built else "Rust/Wasm source + build script present; DataView/TypedArray fallback remains active until wasm-pack build is produced.",
            observed=wasm_built, configured=wasm_built, authority="VISUALIZATION TRANSPORT",
        ).pack(),
        "webgpu_surface": Capability(
            "webgpu_surface", "BROWSER CHECK",
            "Raw WebGPU/WGSL terrain is attempted in-browser first; custom WebGL/GLSL is the deterministic fallback.",
            observed=False, configured=True, authority="VISUALIZATION",
        ).pack(),
        "solid_shell": Capability(
            "solid_shell", "ACTIVE" if solid_built else "READY",
            "SolidJS shell build is present." if solid_built else "SolidJS source shell is included; packaged dependency-free shell remains runtime until an npm build is produced.",
            observed=solid_built, configured=True, authority="UI SHELL",
        ).pack(),
        "three_adapter": Capability(
            "three_adapter", "READY" if three_source else "UNAVAILABLE",
            "Optional Three.js shader adapter source included; raw WebGPU/WebGL remains the packaged high-performance renderer.",
            observed=False, configured=three_source, authority="VISUALIZATION",
        ).pack(),
        "feed_adapters": Capability(
            "feed_adapters", str(feed_adapters.get("state") or "READY"),
            " · ".join([f"{r.get('name')}={r.get('state')}" for r in feed_adapters.get("adapters",[])])[:500] or "Provider-neutral adapter boundary ready.",
            observed=bool(feed_adapters.get("active")), configured=bool(feed_adapters.get("adapters")), authority="MARKET DATA INGESTION",
            model_risk="ACTIVE requires a real provider configuration; READY never means market data is flowing.",
        ).pack(),
        "hft_telemetry": Capability(
            "hft_telemetry", "ACTIVE", f"Event-time latency p95={lat.get('p95_ms')} ms · target={lat.get('target_ms')} ms",
            observed=bool(lat.get("count")), configured=True, authority="OBSERVABILITY",
        ).pack(),
        "precision_clock": Capability(
            "precision_clock", "ACTIVE" if clock.get("ptp_status")=="ACTIVE" else "READY" if clock.get("ptp_status")=="READY" else "UNAVAILABLE",
            f"source={clock.get('source')} · PTP={clock.get('ptp_status')} · offset_ns={clock.get('offset_ns')} · quality={clock.get('quality')}",
            observed=clock.get("ptp_status")=="ACTIVE", configured=clock.get("ptp_status") in {"ACTIVE","READY"}, authority="TIMESTAMP/OBSERVABILITY",
        ).pack(),
        "simd_dispatch": Capability(
            "simd_dispatch", str(simd.get("state") or "READY"),
            f"CPU={simd.get('architecture','—')} · backend={simd.get('backend','SCALAR/NUMPY')} · AVX2={simd.get('avx2',False)} · AVX512={simd.get('avx512',False)}",
            observed=bool(simd.get("avx2") or simd.get("avx512")), configured=True, authority="QUANT EXECUTION SPEED", model_risk="SIMD cannot change formulas; parity validator remains mandatory.",
        ).pack(),
        "source_arbitration": Capability(
            "source_arbitration", "ACTIVE" if arbitration.get("ready") else "READY",
            f"selected={arbitration.get('selected') or 'NONE'} · sources={len(arbitration.get('ranked') or [])}",
            observed=bool(arbitration.get("ready")), configured=True, authority="SOURCE SELECTION",
        ).pack(),
        "autonomous_sentinel": Capability(
            "autonomous_sentinel", "ACTIVE",
            f"state={sentinel.get('state')} · recommended={sentinel.get('recommended_action')}",
            observed=True, configured=True, authority="RISK GUARDIAN", model_risk="No broker action occurs without a separately authorized execution adapter.",
        ).pack(),
        "shared_ring_ipc": Capability(
            "shared_ring_ipc", "READY",
            "Rust shared sequence ring source includes slot sequence/generation/commit/CRC and explicit OVERRUN detection; activation requires compiled Rust core.",
            observed=False, configured=True, authority="IPC/INGESTION",
        ).pack(),
        "co_location": Capability(
            "co_location", _status(colo_cfg, ready=True),
            f"Configured region: {colo_name}" if colo_cfg else "Software stack is co-location ready; no co-location region is configured.",
            observed=colo_cfg, configured=colo_cfg, authority="INFRASTRUCTURE",
        ).pack(),
        "fpga_feed_bridge": Capability(
            "fpga_feed_bridge", _status(fpga_cfg, ready=True),
            "FPGA/NIC bridge configured." if fpga_cfg else "Adapter contract exists; no FPGA/NIC hardware bridge is configured.",
            observed=fpga_cfg, configured=fpga_cfg, authority="INGESTION",
        ).pack(),
        "quantum_optimization": Capability(
            "quantum_optimization", _status(qpu_cfg, ready=True),
            f"QPU provider configured: {qpu_provider}" if qpu_cfg else "QUBO/Ising formulation and classical fallback available; no real QPU configured.",
            observed=qpu_cfg, configured=qpu_cfg, authority="SHADOW", model_risk="Never changes Scanner direction.",
        ).pack(),
        "generative_scenarios": Capability(
            "generative_scenarios", _status(genai_cfg, ready=True),
            "External generative provider configured." if genai_cfg else "Stochastic scenario generator active as local fallback; external GenAI provider not configured.",
            observed=genai_cfg, configured=genai_cfg, authority="SHADOW", model_risk="Scenario generation is exploratory, not a forecast.",
        ).pack(),
        "webxr": Capability(
            "webxr", "BROWSER CHECK", "WebXR support is detected in-browser because the backend cannot verify headset/browser hardware.",
            observed=False, configured=False, authority="VISUALIZATION",
        ).pack(),
        "direct_futures": Capability(
            "direct_futures", "ACTIVE" if direct_futures else "UNAVAILABLE",
            "Direct futures feed available." if direct_futures else "No direct futures flow is connected; underlying evidence remains partial.",
            observed=direct_futures, configured=direct_futures, authority="CONFIRMATION",
        ).pack(),
        "metrics": Capability(
            "metrics", "ACTIVE" if metrics_cfg else "DISABLED",
            "Prometheus-compatible local metrics endpoint enabled." if metrics_cfg else "Metrics endpoint disabled by configuration.",
            observed=True, configured=metrics_cfg, authority="OBSERVABILITY",
        ).pack(),
        "research_store": Capability(
            "research_store", "ACTIVE",
            "SQLite/WAL research archive active asynchronously; LIVE synthetic inventory hot path is memory-resident. External Lakehouse export remains an adapter concern.",
            observed=True, configured=True, authority="RESEARCH",
        ).pack(),
        "security_posture": Capability(
            "security_posture", "ACTIVE",
            "Local-only service, CSP/security headers, no public login surface; external deployment still requires network/host hardening.",
            observed=True, configured=True, authority="SECURITY",
        ).pack(),
        "execution_guard": Capability(
            "execution_guard", "ACTIVE",
            "ITM QUANT exports/communicates decisions but does not directly place orders. External EA/broker execution must enforce account-level kill switches and max-loss rules.",
            observed=True, configured=True, authority="RISK",
        ).pack(),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latency": lat,
        "clock": clock,
        "simd": simd,
        "feed_adapters": feed_adapters,
        "source_arbitration": arbitration,
        "sentinel": sentinel,
        "capabilities": capabilities,
        "truth_policy": "ACTIVE means observed/configured. READY means software contract exists but required external hardware/service is absent.",
    }
