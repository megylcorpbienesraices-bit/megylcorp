"""Live observation-time causality diagnostics for ITM QUANT v1.26.0.

This runtime is deliberately additive. It mirrors normalized provider events into the
same EventEnvelope contract used by Replay and the Rust causal engine, maintains a
bounded event-time reorder buffer and exposes watermark/late-event diagnostics. It does
not replace provider transports and never changes Scanner direction.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any
import math

from .causality_engine import CausalityOrderer, EventEnvelope


def _to_float(v: Any, default: float = 350.0) -> float:
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


class TemporalCausalityRuntime:
    def __init__(self, max_lateness_ms: float = 350.0, max_buffer: int = 50_000, recent: int = 4_000) -> None:
        self.max_lateness_ms=max(0.0,float(max_lateness_ms))
        self.max_buffer=max(100,int(max_buffer))
        self.recent_capacity=max(100,int(recent))
        self._lock=RLock()
        self._orderers: dict[str,CausalityOrderer] = {}
        self._recent: dict[str,deque] = defaultdict(lambda: deque(maxlen=self.recent_capacity))
        self._last_emitted_ns: dict[str,int] = {}
        self._ingested=0
        self._emitted=0
        self._late_outside_window=0
        self._sequence_missing=0
        self._invalid_event_time=0
        self._last_event_at: str|None=None

    def _orderer(self, symbol: str) -> CausalityOrderer:
        sym=str(symbol or "").upper()
        if sym not in self._orderers:
            self._orderers[sym]=CausalityOrderer(self.max_lateness_ms,self.max_buffer)
        return self._orderers[sym]

    def ingest(self, *, source: str, symbol: str, event_type: str, values: dict[str,Any],
               event_time: Any=None, receive_time: Any=None, source_seq: int|None=None,
               event_id: str|None=None) -> None:
        sym=str(symbol or "").upper()
        if not sym:
            return
        if event_time is None:
            with self._lock:self._invalid_event_time+=1
            return
        try:
            ev=EventEnvelope.build(source=source,symbol=sym,event_type=event_type,
                event_time=event_time, receive_time=receive_time,
                payload=dict(values or {}), source_seq=source_seq,event_id=event_id)
        except Exception:
            with self._lock:self._invalid_event_time+=1
            return
        with self._lock:
            orderer=self._orderer(sym)
            # If the event arrives older than the already-public causal frontier, keep it in
            # diagnostics but never pretend it can be inserted into past LIVE state.
            last=self._last_emitted_ns.get(sym)
            if last is not None and int(ev.event_time.value) < last:
                self._late_outside_window += 1
                self._recent[sym].append({**ev.to_dict(),"causal_status":"LATE_AFTER_WATERMARK"})
                self._ingested += 1
                self._last_event_at=datetime.now(timezone.utc).isoformat()
                return
            if source_seq is None:
                self._sequence_missing += 1
            orderer.push(ev)
            self._ingested += 1
            self._last_event_at=datetime.now(timezone.utc).isoformat()
            for ready in orderer.drain_ready(flush=False):
                ns=int(ready.event_time.value)
                self._last_emitted_ns[sym]=ns
                self._recent[sym].append({**ready.to_dict(),"causal_status":"ORDERED"})
                self._emitted += 1

    def recent(self, symbol: str, limit: int=200) -> list[dict[str,Any]]:
        sym=str(symbol or "").upper()
        with self._lock:
            return list(self._recent.get(sym,()))[-max(1,min(int(limit),2000)):]

    def status(self, symbol: str|None=None) -> dict[str,Any]:
        with self._lock:
            symbols=[str(symbol).upper()] if symbol else sorted(set(self._orderers)|set(self._recent))
            per={}
            for sym in symbols:
                o=self._orderers.get(sym)
                per[sym]={
                    "buffered":o.buffered if o else 0,
                    "watermark_event_time":(o.status().get("watermark_event_time") if o else None),
                    "last_emitted_event_time":(None if sym not in self._last_emitted_ns else datetime.fromtimestamp(self._last_emitted_ns[sym]/1e9,tz=timezone.utc).isoformat()),
                    "recent_events":len(self._recent.get(sym,())),
                }
            return {
                "ready":self._ingested>0,"ingested":self._ingested,"ordered_emitted":self._emitted,
                "late_after_watermark":self._late_outside_window,"sequence_missing":self._sequence_missing,"invalid_event_time":self._invalid_event_time,
                "max_lateness_ms":self.max_lateness_ms,"last_event_at":self._last_event_at,
                "ordering":"EVENT_TIME → SOURCE_SEQ → EVENT_TYPE_PRIORITY → SOURCE",
                "policy":"BOUNDED_REORDER_BUFFER_WITH_WATERMARK · LATE_EVENTS_QUARANTINED_FROM_PUBLIC_FRONTIER",
                "authority":"CAUSALITY_AND_AUDIT_ONLY","symbols":per,
            }


CAUSAL_RUNTIME=TemporalCausalityRuntime()
