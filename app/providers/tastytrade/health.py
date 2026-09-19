from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from ...core.obs import note as _obs_note


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HealthState:
    configured: bool = False
    auth: str = "NOT_CONFIGURED"
    oauth: str = "NOT_CONFIGURED"
    dxlink: str = "DISCONNECTED"
    market_data: str = "IDLE"
    scope: str = "READ ONLY"
    last_event_at: str | None = None
    last_event_age_ms: float | None = None
    latency_ms: float | None = None
    reconnects: int = 0
    subscriptions: int = 0
    queue_depth: int = 0
    dropped_events: int = 0
    last_error: str = ""
    updated_at: str = ""


class TastytradeHealth:
    def __init__(self) -> None:
        self._lock = RLock()
        self._state = HealthState(updated_at=_now())

    def patch(self, **values: Any) -> None:
        with self._lock:
            for k, v in values.items():
                if hasattr(self._state, k):
                    setattr(self._state, k, v)
            self._state.updated_at = _now()

    def event(self, *, latency_ms: float | None = None, queue_depth: int | None = None) -> None:
        with self._lock:
            self._state.last_event_at = _now()
            self._state.last_event_age_ms = 0.0
            if latency_ms is not None:
                self._state.latency_ms = float(latency_ms)
            if queue_depth is not None:
                self._state.queue_depth = int(queue_depth)
            self._state.market_data = "LIVE"
            self._state.updated_at = _now()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out = asdict(self._state)
        if out.get("last_event_at"):
            try:
                ts = datetime.fromisoformat(str(out["last_event_at"]).replace("Z", "+00:00"))
                out["last_event_age_ms"] = max(0.0, (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() * 1000.0)
            except Exception as _e:
                _obs_note('health:62', _e)
        out["provider"] = "TASTYTRADE"
        out["authority"] = "MARKET_DATA_ONLY"
        return out
