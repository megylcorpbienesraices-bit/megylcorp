"""Provider-neutral market-feed boundary for ITM QUANT v1.24.

This module does **not** pretend that Alpaca WebSocket, Databento, OPRA multicast,
CME MDP or a replay spool use the same wire protocol.  Each provider adapter owns
its native transport/decoder and emits one stable ITMQ EventEnvelope contract.

Only adapters with a real configured endpoint/credential may report ACTIVE.  READY
means the software boundary exists but the external provider/hardware is not connected.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, Optional
import os
import pandas as pd

from .causality_engine import EventEnvelope


@dataclass(frozen=True)
class AdapterDescriptor:
    key: str
    name: str
    transport: str
    state: str
    configured: bool
    direct: bool
    detail: str
    authority: str = "MARKET_DATA_SOURCE"

    def pack(self) -> Dict[str, Any]:
        return asdict(self)


def _truthy(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _configured(*names: str) -> bool:
    return any(bool(str(os.getenv(n, "")).strip()) for n in names)


def descriptors() -> list[Dict[str, Any]]:
    """Expose only the provider stack actually used by this DOW-specialized build."""
    try:
        from .alpaca_data import load_settings
        alpaca_cfg = load_settings() is not None
    except Exception:
        alpaca_cfg = False

    tasty_cfg = _configured("TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN")
    rows = [
        AdapterDescriptor(
            "alpaca_ws", "AlpacaWebSocketAdapter", "WEBSOCKET/HTTPS",
            "ACTIVE" if alpaca_cfg else "READY", alpaca_cfg, False,
            "SIP + OPRA market-data boundary for DIA/XLI/XLF and entitled options.",
        ),
        AdapterDescriptor(
            "tastytrade_dxlink", "TastytradeDXLinkAdapter", "WEBSOCKET/HTTPS",
            "ACTIVE" if tasty_cfg else "READY", tasty_cfg, False,
            "Read-only OAuth2 + DXLink market-data boundary for entitled futures/index/derivatives data.",
        ),
        AdapterDescriptor(
            "official_macro", "OfficialMacroSources", "HTTPS/REST",
            "ACTIVE", True, False,
            "Official macro context boundary (Federal Reserve/BLS/FRED modules when observations are available).",
            authority="MACRO_CONTEXT_SOURCE",
        ),
        AdapterDescriptor(
            "replay", "ReplayAdapter", "ITMQ_WIRE/FILE",
            "ACTIVE" if _configured("ITM_RUST_REPLAY_WIRE") else "READY",
            _configured("ITM_RUST_REPLAY_WIRE"), False,
            "Internal deterministic replay spool for causal backtesting; not an external market-data provider.",
            authority="INTERNAL_REPLAY",
        ),
    ]
    return [r.pack() for r in rows]


def status() -> Dict[str, Any]:
    rows = descriptors()
    active = [r for r in rows if r["state"] == "ACTIVE"]
    return {
        "state": "ACTIVE" if active else "READY",
        "active": [r["key"] for r in active],
        "adapters": rows,
        "contract": "NATIVE_PROVIDER → EventEnvelope(event/receive/process time) → QUALITY ARBITRATION → CAUSALITY",
        "note": "READY is software readiness only. No provider is presented as LIVE without a real configured source.",
    }


def normalize_event(
    *,
    source: str,
    symbol: str,
    event_type: str,
    event_time: Any,
    payload: Optional[Dict[str, Any]] = None,
    receive_time: Any = None,
    source_seq: Optional[int] = None,
    event_id: Optional[str] = None,
) -> EventEnvelope:
    """Normalize a decoded provider event into the stable causal contract.

    This function expects the provider-specific adapter to have already decoded the
    native packet.  It never guesses missing market values.
    """
    now = pd.Timestamp.now(tz="UTC")
    return EventEnvelope.build(
        source=source,
        symbol=symbol,
        event_type=event_type,
        event_time=event_time,
        receive_time=receive_time if receive_time is not None else now,
        process_time=now,
        payload=dict(payload or {}),
        source_seq=source_seq,
        event_id=event_id,
    )


def arbitration_rows(source_health: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Convert provider telemetry to the Source Arbitration Engine input schema."""
    out=[]
    for r in source_health or []:
        if not isinstance(r, dict):
            continue
        out.append({
            "name": r.get("name") or r.get("source") or r.get("provider") or "SOURCE",
            "status": r.get("status") or r.get("state") or "UNKNOWN",
            "latency_ms": r.get("latency_ms") if r.get("latency_ms") is not None else 999.0,
            "age_ms": r.get("age_ms") if r.get("age_ms") is not None else r.get("staleness_ms", 9999.0),
            "gap_rate": r.get("gap_rate") if r.get("gap_rate") is not None else r.get("missing_rate", 1.0),
            "sequence_ok": r.get("sequence_ok") if "sequence_ok" in r else None,
            "cross_source_divergence": r.get("cross_source_divergence") if r.get("cross_source_divergence") is not None else r.get("divergence"),
            "event_time_valid": r.get("event_time_valid"),
            "event_time_source": r.get("event_time_source"),
            "channel": r.get("channel") or r.get("event_type") or r.get("data_type"),
            "instrument_type": r.get("instrument_type") or r.get("asset_class"),
        })
    return out
