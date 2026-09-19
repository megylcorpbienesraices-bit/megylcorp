"""Canonical event-time provenance for ITM QUANT v1.27.13.

A market observation has at least two physically distinct clocks:

* ``event_time``: when the provider/exchange says the market event occurred;
* ``received_at``: when ITM QUANT received/decoded the observation.

The critical invariant is that a missing/corrupt event timestamp is NEVER silently
relabelled as a valid market event at ``now``.  Receive-time may be retained as an
explicit proxy for ordering/diagnostics, but provenance stays visible and quality
arbitration can penalise it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class EventTimeResolution:
    event_time: datetime | None
    received_at: datetime
    event_time_valid: bool
    event_time_source: str
    raw_event_time: Any = None


def parse_utc(value: Any) -> datetime | None:
    """Parse one timestamp strictly; invalid/missing values return ``None``."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, datetime):
            d = value
        else:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def resolve_event_time(
    event_time: Any,
    *,
    received_at: Any = None,
    explicit_valid: bool | None = None,
    explicit_source: str | None = None,
    now: datetime | None = None,
) -> EventTimeResolution:
    """Resolve clocks without fabricating provider event-time truth.

    ``event_time`` may still be a receive-time proxy supplied by an adapter.  When an
    adapter explicitly says ``event_time_valid=False``, that provenance wins even if
    the proxy timestamp itself parses correctly.
    """
    now_utc = parse_utc(now) or datetime.now(timezone.utc)
    rec = parse_utc(received_at) or now_utc
    evt = parse_utc(event_time)

    if explicit_valid is False:
        source = str(explicit_source or "RECEIVE_PROXY").upper()
        return EventTimeResolution(evt or rec, rec, False, source, event_time)
    if evt is not None:
        source = str(explicit_source or "PROVIDER_EVENT_TIME").upper()
        return EventTimeResolution(evt, rec, True, source, event_time)

    # Keep a usable ordering clock, but make it impossible to confuse with a real
    # provider/exchange event timestamp.
    return EventTimeResolution(rec, rec, False, "RECEIVE_PROXY", event_time)


def event_time_quality_factor(valid: bool, source: str | None) -> float:
    """Bounded quality multiplier for timestamp provenance."""
    if valid:
        return 1.0
    src = str(source or "").upper()
    if src == "RECEIVE_PROXY":
        return 0.65
    return 0.0
