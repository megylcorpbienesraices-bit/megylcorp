"""ITM QUANT v1.26.0 Temporal Truth + per-channel Data Health.

This layer never ranks providers by name.  It evaluates each observation independently,
keeps market/feature channels separate, measures freshness/latency/sequence integrity,
and exposes synchronization cohorts around a bounded event-time window.  It is an
eligibility/audit layer; Scanner remains the only direction authority.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
import math
import statistics

from .provider_bus import PROVIDER_BUS, FEATURE_BUS
from .source_arbitration import score_source
from .temporal_causality import CAUSAL_RUNTIME
from .event_time import parse_utc

EVENT_TTL_MS = {
    "QUOTE": 5_000.0,
    "TRADE": 5_000.0,
    "SNAPSHOT": 20_000.0,
    "BAR": 75_000.0,
    "CANDLE": 75_000.0,
    "GREEKS": 20_000.0,
    "SUMMARY": 600_000.0,
    "OPEN_INTEREST": 129_600_000.0,  # structural/EOD: 36h observation window
    "FEATURE": 180_000.0,
}


def _dt(v: Any) -> datetime | None:
    return parse_utc(v)



def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _health_row(row: dict[str, Any], now: datetime, *, feature: bool = False) -> dict[str, Any]:
    event_type = "FEATURE" if feature else str(row.get("event_type") or "UNKNOWN").upper()
    received_at = _dt(row.get("received_at") or row.get("updated_at")) or now
    parsed_event = _dt(row.get("timestamp"))
    event_valid = bool(row.get("event_time_valid", parsed_event is not None)) and parsed_event is not None
    event_source = str(row.get("event_time_source") or ("PROVIDER_EVENT_TIME" if event_valid else "RECEIVE_PROXY")).upper()
    # A receive-time proxy is retained for liveness diagnostics, never promoted to
    # exchange/provider event-time truth.
    event_time = parsed_event or received_at
    age_ms = max(0.0, (now - event_time).total_seconds() * 1000.0)
    receive_age_ms = max(0.0, (now - received_at).total_seconds() * 1000.0)
    ttl_ms = _f(row.get("ttl_ms"), EVENT_TTL_MS.get(event_type, 30_000.0)) if feature else EVENT_TTL_MS.get(event_type, 30_000.0)
    raw_latency=row.get("latency_ms")
    latency_ms = None if raw_latency is None else max(0.0, _f(raw_latency, 0.0))
    sequence_ok = row.get("sequence_ok") is True
    status = "LIVE" if age_ms <= ttl_ms else "STALE"
    q = score_source({
        "source": row.get("source"), "channel": event_type, "status": status, "latency_ms": latency_ms,
        "age_ms": age_ms, "gap_rate": 0.0, "sequence_ok": row.get("sequence_ok"),
        "cross_source_divergence": None, "event_time_valid":event_valid,"event_time_source":event_source,
    })
    eligible = bool(status == "LIVE" and event_valid and sequence_ok and float(q.get("quality_score", 0.0)) >= 50.0)
    return {
        "source": str(row.get("source") or "UNKNOWN").upper(),
        "channel": str(row.get("feature_group") if feature else event_type).upper(),
        "kind": "FEATURE" if feature else "MARKET_EVENT",
        "event_time": event_time.isoformat(), "received_at": received_at.isoformat(),
        "event_time_valid":event_valid,"event_time_source":event_source,
        "age_ms": round(age_ms, 3), "receive_age_ms": round(receive_age_ms, 3),
        "latency_ms": None if latency_ms is None else round(latency_ms, 3), "ttl_ms": round(ttl_ms, 3),
        "sequence_ok": sequence_ok, "source_seq": row.get("source_seq"),
        "event_id": row.get("event_id"), "status": status,
        "quality_score": float(q.get("quality_score", 0.0)), "quality_label": q.get("quality_label"),
        "eligible": eligible,
    }


class TemporalTruthEngine:
    def __init__(self, sync_window_ms: float = 350.0) -> None:
        self.sync_window_ms = max(10.0, float(sync_window_ms))

    def snapshot(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").upper()
        now = datetime.now(timezone.utc)
        market_rows = [_health_row(r, now, feature=False) for r in PROVIDER_BUS.latest_events(sym)]
        feature_rows = [_health_row(r, now, feature=True) for r in FEATURE_BUS.latest_events(sym)]
        rows = market_rows + feature_rows

        grouped: dict[str, dict[str, Any]] = defaultdict(dict)
        for row in rows:
            grouped[row["source"]][row["channel"]] = row

        provider_health = []
        for source, channels in sorted(grouped.items()):
            vals = list(channels.values())
            live = [x for x in vals if x["eligible"]]
            provider_health.append({
                "source": source,
                "status": "LIVE" if live else "STALE" if vals else "NO_DATA",
                "live_channels": len(live), "channels": channels,
                "worst_age_ms": max([x["age_ms"] for x in vals] or [0.0]),
                "quality_mean": round(statistics.fmean([x["quality_score"] for x in vals]), 2) if vals else 0.0,
            })

        # Synchronization cohort for market observations.  The freshest eligible event time
        # defines the frontier; peers inside the bounded window are synchronized with it.
        eligible_market = [x for x in market_rows if x["eligible"]]
        ref_times=[_dt(x["event_time"]) for x in eligible_market]; ref_times=[x for x in ref_times if x is not None]
        ref_time = max(ref_times, default=None)
        cohort = []
        for row in eligible_market:
            rt=_dt(row["event_time"])
            skew = abs((ref_time - rt).total_seconds() * 1000.0) if ref_time and rt else float("inf")
            cohort.append({**row, "delta_to_frontier_ms": round(skew, 3), "synchronized": bool(skew <= self.sync_window_ms)})
        synchronized = [x for x in cohort if x["synchronized"]]

        # Cross-provider event-time skew estimate.  This is deliberately named an estimate:
        # exchange clocks and provider timestamp semantics can differ.
        source_latest: dict[str, datetime] = {}
        for row in eligible_market:
            d = _dt(row["event_time"])
            if d is not None and (row["source"] not in source_latest or d > source_latest[row["source"]]):
                source_latest[row["source"]] = d
        skew_rows = []
        if source_latest:
            med_ts = statistics.median([d.timestamp() for d in source_latest.values()])
            for src, d in sorted(source_latest.items()):
                skew_rows.append({"source": src, "event_clock_skew_vs_provider_median_ms": round((d.timestamp() - med_ts) * 1000.0, 3)})

        critical_stale = [x for x in market_rows if x["channel"] in {"QUOTE", "TRADE"} and not x["eligible"]]
        causal = CAUSAL_RUNTIME.status(sym)
        return {
            "ready": bool(rows), "symbol": sym, "asof": now.isoformat(),
            "sync_window_ms": self.sync_window_ms,
            "frontier_event_time": ref_time.isoformat() if ref_time else None,
            "synchronized_market_observations": len(synchronized),
            "market_observations": len(market_rows), "feature_observations": len(feature_rows),
            "provider_health": provider_health, "sync_cohort": cohort,
            "clock_skew_estimate": skew_rows,
            "critical_stale_channels": critical_stale,
            "causality": causal,
            "decision_safe": bool(synchronized),
            "policy": "EVENT_TIME + RECEIVE_TIME + SOURCE_SEQ + FRESHNESS + QUALITY + WATERMARK",
            "authority": "TEMPORAL_ELIGIBILITY_AND_AUDIT_ONLY",
            "note": "Provider names carry no rank. Eligibility is decided per channel/observation; stale data is excluded without disabling healthy channels from the same provider.",
        }


TEMPORAL_TRUTH = TemporalTruthEngine()
