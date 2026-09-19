"""Deterministic causal event ordering for ITM QUANT.

The live system receives heterogeneous events (SIP trades, OPRA option prints,
quotes, snapshots and model refreshes) on different clocks.  This module keeps
three timestamps for every event and exposes a stable ordering contract:

1. ``event_time``   – when the market/source says the event happened.
2. ``receive_time`` – when ITM QUANT received the event.
3. ``process_time`` – when the event entered a deterministic engine pass.

The orderer never rewrites event_time and never sorts by receive_time alone.
That is the core requirement for causal Replay and auditability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from heapq import heappush, heappop
from itertools import count
from typing import Any, Dict, Iterable, List, Optional
import pandas as pd


_EVENT_PRIORITY = {
    # Quotes precede prints only when event-time/source-sequence tie.  This makes
    # same-timestamp NBBO classification deterministic while preserving provider
    # sequence as the stronger ordering key.
    "QUOTE": 10,
    "OPTION_QUOTE": 10,
    "TRADE": 20,
    "OPTION_TRADE": 20,
    "SNAPSHOT": 50,
    "MODEL_REFRESH": 60,
    "AUDIT": 90,
}


def _ts(value: Any, fallback: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    try:
        t = pd.Timestamp(value)
        if pd.isna(t):
            raise ValueError
    except Exception as exc:
        if fallback is None:
            raise ValueError(f"invalid event timestamp: {value!r}") from exc
        t = fallback
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC")


@dataclass(frozen=True)
class EventEnvelope:
    source: str
    symbol: str
    event_type: str
    event_time: pd.Timestamp
    receive_time: pd.Timestamp
    process_time: pd.Timestamp
    payload: Dict[str, Any] = field(default_factory=dict)
    source_seq: Optional[int] = None
    event_id: Optional[str] = None

    @classmethod
    def build(
        cls,
        *,
        source: str,
        symbol: str,
        event_type: str,
        event_time: Any,
        payload: Optional[Dict[str, Any]] = None,
        receive_time: Any = None,
        process_time: Any = None,
        source_seq: Optional[int] = None,
        event_id: Optional[str] = None,
    ) -> "EventEnvelope":
        now = pd.Timestamp.now(tz="UTC")
        et = _ts(event_time)
        rt = _ts(receive_time, now) if receive_time is not None else now
        pt = _ts(process_time, rt) if process_time is not None else rt
        return cls(
            source=str(source or "UNKNOWN").upper(),
            symbol=str(symbol or "").upper(),
            event_type=str(event_type or "UNKNOWN").upper(),
            event_time=et,
            receive_time=rt,
            process_time=pt,
            payload=dict(payload or {}),
            source_seq=int(source_seq) if source_seq is not None else None,
            event_id=str(event_id) if event_id is not None else None,
        )

    def ordering_key(self) -> tuple:
        # Market/event time is authoritative.  Source sequence is used when the
        # venue provides it; event-type priority makes ties deterministic.
        seq = self.source_seq if self.source_seq is not None else 2**63 - 1
        prio = _EVENT_PRIORITY.get(self.event_type, 70)
        return (int(self.event_time.value), int(seq), int(prio), self.source, self.event_id or "")

    def latency_ms(self) -> float:
        return max(0.0, (self.receive_time - self.event_time).total_seconds() * 1000.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "event_time": self.event_time.isoformat(),
            "receive_time": self.receive_time.isoformat(),
            "process_time": self.process_time.isoformat(),
            "source_seq": self.source_seq,
            "event_id": self.event_id,
            "latency_ms": round(self.latency_ms(), 3),
            "payload": dict(self.payload),
        }


class CausalityOrderer:
    """Small bounded event-time reorder buffer.

    ``max_lateness_ms`` is intentionally explicit.  Events are held until the
    watermark has moved beyond their event_time by that allowance.  Replay can
    use ``flush=True`` to deterministically emit everything in event-time order.
    """

    def __init__(self, max_lateness_ms: float = 350.0, max_buffer: int = 50000) -> None:
        self.max_lateness_ms = max(0.0, float(max_lateness_ms))
        self.max_buffer = max(100, int(max_buffer))
        self._heap: List[tuple] = []
        self._counter = count()
        self._max_event_time_ns: Optional[int] = None
        self.dropped_overflow = 0

    @property
    def buffered(self) -> int:
        return len(self._heap)

    def push(self, event: EventEnvelope) -> None:
        ns = int(event.event_time.value)
        self._max_event_time_ns = ns if self._max_event_time_ns is None else max(self._max_event_time_ns, ns)
        heappush(self._heap, (event.ordering_key(), next(self._counter), event))
        if len(self._heap) > self.max_buffer:
            # Bounded-memory protection: emit/drop the oldest rather than newest.
            heappop(self._heap)
            self.dropped_overflow += 1

    def extend(self, events: Iterable[EventEnvelope]) -> None:
        for event in events:
            self.push(event)

    def drain_ready(self, *, flush: bool = False) -> List[EventEnvelope]:
        if not self._heap:
            return []
        if flush or self._max_event_time_ns is None:
            cutoff = 2**63 - 1
        else:
            cutoff = self._max_event_time_ns - int(self.max_lateness_ms * 1_000_000)
        out: List[EventEnvelope] = []
        while self._heap and (flush or self._heap[0][0][0] <= cutoff):
            out.append(heappop(self._heap)[2])
        return out

    def status(self) -> Dict[str, Any]:
        return {
            "buffered": self.buffered,
            "max_lateness_ms": self.max_lateness_ms,
            "dropped_overflow": self.dropped_overflow,
            "watermark_event_time": None if self._max_event_time_ns is None else pd.Timestamp(self._max_event_time_ns, tz="UTC").isoformat(),
            "ordering": "EVENT_TIME → SOURCE_SEQ → EVENT_TYPE_PRIORITY → SOURCE",
        }


def unify_market_events(
    *,
    symbol: str,
    price_ticks: Optional[pd.DataFrame] = None,
    option_events: Optional[pd.DataFrame] = None,
    process_time: Any = None,
    max_lateness_ms: float = 350.0,
) -> Dict[str, Any]:
    """Create one deterministic stream for visualization/replay diagnostics.

    This function is deliberately side-effect free.  It is safe for LIVE UI,
    Replay and unit tests and does not alter Scanner authority.
    """
    process_ts = _ts(process_time) if process_time is not None else pd.Timestamp.now(tz="UTC")
    orderer = CausalityOrderer(max_lateness_ms=max_lateness_ms)
    sym = str(symbol or "").upper()

    if isinstance(price_ticks, pd.DataFrame) and not price_ticks.empty:
        ticks = price_ticks.copy()
        ticks["timestamp"] = pd.to_datetime(ticks.get("timestamp"), errors="coerce")
        ticks = ticks.dropna(subset=["timestamp"])
        for i, row in ticks.iterrows():
            seq = row.get("seq")
            try:
                seq = int(seq) if pd.notna(seq) else None
            except Exception:
                seq = None
            orderer.push(EventEnvelope.build(
                source=str(row.get("source") or "SIP"), symbol=sym, event_type="TRADE",
                event_time=row.get("timestamp"), receive_time=(row.get("received_at") if pd.notna(row.get("received_at")) else process_ts),
                process_time=process_ts, source_seq=seq,
                event_id=f"PX-{seq if seq is not None else i}",
                payload={"price": row.get("price"), "size": row.get("size"), "exchange": row.get("exchange")},
            ))

    if isinstance(option_events, pd.DataFrame) and not option_events.empty:
        ev = option_events.copy()
        ev["timestamp"] = pd.to_datetime(ev.get("timestamp"), errors="coerce")
        ev = ev.dropna(subset=["timestamp"])
        if "underlying_symbol" in ev.columns:
            ev = ev[ev["underlying_symbol"].astype(str).str.upper() == sym]
        for i, row in ev.iterrows():
            seq = row.get("seq")
            try:
                seq = int(seq) if pd.notna(seq) else None
            except Exception:
                seq = None
            orderer.push(EventEnvelope.build(
                source=str(row.get("flow_source") or "OPRA"), symbol=sym, event_type="OPTION_TRADE",
                event_time=row.get("timestamp"), receive_time=(row.get("received_at") if pd.notna(row.get("received_at")) else process_ts),
                process_time=process_ts, source_seq=seq,
                event_id=f"OP-{seq if seq is not None else i}",
                payload={
                    "strike": row.get("strike"), "option_type": row.get("option_type"),
                    "trade_price": row.get("trade_price"), "contracts": row.get("contracts"),
                    "premium": row.get("premium"), "direction_sign": row.get("direction_sign"),
                    "aggressor": row.get("aggressor"), "underlying_price": row.get("underlying_price"),
                },
            ))

    rows = [e.to_dict() for e in orderer.drain_ready(flush=True)]
    return {"events": rows, "count": len(rows), "status": orderer.status(), "causal": True}


def causal_sort_frame(frame: Optional[pd.DataFrame], *, timestamp_col: str = "timestamp", seq_col: str = "seq") -> pd.DataFrame:
    """Return a stable event-time ordered copy while preserving every provider column.

    This is the low-friction bridge used by existing quantitative modules while the
    canonical EventEnvelope stream is rolled through the whole stack.  It changes
    ordering only; it never changes values, classifications or Scanner authority.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    x = frame.copy()
    if timestamp_col not in x.columns:
        return x
    x["__event_time"] = pd.to_datetime(x[timestamp_col], errors="coerce", utc=True)
    x["__source_seq"] = pd.to_numeric(x.get(seq_col, pd.Series(index=x.index, dtype=float)), errors="coerce")
    x["__row_order"] = range(len(x))
    # Missing sequence falls behind sequenced events at the same event-time, then
    # original row order makes the operation stable/deterministic.
    seq_sort = x["__source_seq"].fillna(float(2**63 - 1))
    x["__seq_sort"] = seq_sort
    x = x.sort_values(["__event_time", "__seq_sort", "__row_order"], kind="mergesort", na_position="last")
    return x.drop(columns=["__event_time", "__source_seq", "__seq_sort", "__row_order"])
