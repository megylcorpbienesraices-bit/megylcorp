"""Adaptive LIVE cadence for expensive structural refreshes.

The market feeds remain event-driven.  This scheduler only decides when the heavier
snapshot/chain refresh should run.  It never fabricates ticks, OPRA events, OI or IV.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Dict


def _f(value: Any, fallback: float = 0.0) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else float(fallback)
    except Exception:
        return float(fallback)


@dataclass(frozen=True)
class SchedulerCadence:
    structural_active: float = 5.0
    structural_normal: float = 10.0
    structural_quiet: float = 20.0
    flow_active: float = 10.0
    flow_normal: float = 20.0
    flow_quiet: float = 45.0
    premarket: float = 45.0
    london: float = 20.0
    closed: float = 90.0
    wake: float = 1.0


class AdaptiveLiveScheduler:
    """Small deterministic scheduler driven by observed SIP/OPRA event activity.

    ACTIVE is held briefly after a burst so one quiet second does not immediately
    de-escalate the cadence.  NORMAL is held longer.  All expensive refreshes are
    still bounded by the configured minimum interval.
    """

    def __init__(self, cadence: SchedulerCadence | None = None) -> None:
        self.cadence = cadence or SchedulerCadence()
        self._last_observe = time.monotonic()
        self._last_price_seq = 0
        self._last_option_seq = 0
        self._last_price: float | None = None
        self._last_structural = 0.0
        self._last_flow = 0.0
        self._active_until = 0.0
        self._normal_until = 0.0
        self._state = "QUIET"
        self._price_rate = 0.0
        self._option_rate = 0.0
        self._price_move_bps = 0.0

    @staticmethod
    def _seq(status: Dict[str, Any] | None) -> int:
        try:
            return max(0, int((status or {}).get("last_seq") or 0))
        except Exception:
            return 0

    @staticmethod
    def _last_price_value(status: Dict[str, Any] | None) -> float | None:
        tick = (status or {}).get("last_tick") or {}
        try:
            p = float(tick.get("price"))
            return p if math.isfinite(p) and p > 0 else None
        except Exception:
            return None

    def observe(
        self,
        price_status: Dict[str, Any] | None,
        option_status: Dict[str, Any] | None,
        *,
        market_mode: str = "LIVE",
        market_state: str = "REGULAR",
        now: float | None = None,
    ) -> str:
        now = time.monotonic() if now is None else float(now)
        dt = max(0.05, now - self._last_observe)
        pseq = self._seq(price_status)
        oseq = self._seq(option_status)
        dp = max(0, pseq - self._last_price_seq) if pseq >= self._last_price_seq else 0
        do = max(0, oseq - self._last_option_seq) if oseq >= self._last_option_seq else 0
        self._price_rate = dp / dt
        self._option_rate = do / dt

        px = self._last_price_value(price_status)
        self._price_move_bps = 0.0
        if px is not None and self._last_price not in (None, 0):
            self._price_move_bps = abs(px / float(self._last_price) - 1.0) * 10_000.0
        if px is not None:
            self._last_price = px

        mode = str(market_mode or "").upper()
        state = str(market_state or "").upper()
        if mode != "LIVE":
            self._state = "QUIET"
        elif state == "LONDON":
            self._state = "LONDON"
        elif state == "PREMARKET":
            self._state = "PREMARKET"
        elif state != "REGULAR":
            self._state = "CLOSED"
        else:
            # OPRA bursts are deliberately more sensitive than SIP trade bursts.
            burst = self._option_rate >= 0.25 or self._price_rate >= 8.0 or self._price_move_bps >= 1.5
            any_activity = dp > 0 or do > 0
            if burst:
                self._active_until = max(self._active_until, now + 8.0)
                self._normal_until = max(self._normal_until, now + 20.0)
            elif any_activity:
                self._normal_until = max(self._normal_until, now + 15.0)
            if now < self._active_until:
                self._state = "ACTIVE"
            elif now < self._normal_until:
                self._state = "NORMAL"
            else:
                self._state = "QUIET"

        self._last_price_seq = pseq
        self._last_option_seq = oseq
        self._last_observe = now
        return self._state

    def structural_interval(self) -> float:
        c = self.cadence
        return {
            "ACTIVE": c.structural_active,
            "NORMAL": c.structural_normal,
            "QUIET": c.structural_quiet,
            "PREMARKET": c.premarket,
            "LONDON": c.london,
            "CLOSED": c.closed,
        }.get(self._state, c.structural_normal)

    def flow_interval(self) -> float:
        c = self.cadence
        return {
            "ACTIVE": c.flow_active,
            "NORMAL": c.flow_normal,
            "QUIET": c.flow_quiet,
            "PREMARKET": max(c.premarket, c.flow_quiet),
            "LONDON": c.london,
            "CLOSED": c.closed,
        }.get(self._state, c.flow_normal)

    def due(self, *, now: float | None = None) -> tuple[bool, bool]:
        now = time.monotonic() if now is None else float(now)
        structural = self._last_structural <= 0 or now - self._last_structural >= self.structural_interval()
        flow = self._last_flow <= 0 or now - self._last_flow >= self.flow_interval()
        return structural, flow

    def mark_refresh(self, *, structural: bool = True, flow: bool = False, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        if structural:
            self._last_structural = now
        if flow:
            self._last_flow = now

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "state": self._state,
            "structural_interval_s": round(self.structural_interval(), 3),
            "flow_interval_s": round(self.flow_interval(), 3),
            "wake_interval_s": round(self.cadence.wake, 3),
            "sip_trade_rate_eps": round(self._price_rate, 3),
            "opra_trade_rate_eps": round(self._option_rate, 3),
            "last_price_move_bps": round(self._price_move_bps, 4),
            "policy": "EVENT_DRIVEN_MULTI_PROVIDER + LONDON_SESSION_AWARE_ADAPTIVE_REFRESH",
            "authority": "SCHEDULING_ONLY",
        }


LIVE_REFRESH_SCHEDULER = AdaptiveLiveScheduler()
