"""Canonical option-expiry clocks for ITM QUANT v1.27.12.

Two clocks are intentionally separate:

* ``bs_year_fraction`` / ``year_fraction`` use exact ACT/365 calendar seconds to
  the option expiry/settlement instant.  This is the clock for Black-Scholes,
  IV inversion and Greeks.  It removes the old 0.01-day / 1-minute / 1e-10-year
  floors that made the same 0DTE contract have different Gamma in different
  modules.
* ``trading_year_fraction`` is a session-time clock for realised-volatility,
  momentum and other market-session statistics.  It must never be substituted
  for the Black-Scholes clock merely because the market is closed overnight.

The only numerical floor in the BS clock is sub-second and exists solely to keep
singular near-expiry formulae finite.  It is configurable and is reported by
``describe`` so the convention is auditable.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
import math
import os
from typing import Any

import numpy as np

from .obs import note as _obs_note

NY = ZoneInfo("America/New_York")
SECONDS_PER_DAY = 86_400.0
DAYS_PER_YEAR = 365.0
SECONDS_PER_YEAR = DAYS_PER_YEAR * SECONDS_PER_DAY
DEFAULT_SETTLEMENT_TIME = dtime(16, 0)
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
SESSION_MINUTES = 390.0
SESSIONS_PER_YEAR = 252.0


def _min_bs_seconds() -> float:
    try:
        return max(0.001, float(os.getenv("ITM_BS_MIN_SECONDS", "0.5")))
    except Exception:
        return 0.5


def _as_ny(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(NY)
    if value.tzinfo is None:
        return value.replace(tzinfo=NY)
    return value.astimezone(NY)


def _coerce_expiry(expiry_at: Any, settlement_time: dtime = DEFAULT_SETTLEMENT_TIME) -> datetime:
    if isinstance(expiry_at, datetime):
        return _as_ny(expiry_at)
    if isinstance(expiry_at, date):
        return datetime.combine(expiry_at, settlement_time, tzinfo=NY)
    raw = str(expiry_at or "").strip()
    if not raw:
        raise ValueError("expiry_at vacio")
    # Date-only option metadata is interpreted at the configured settlement time.
    try:
        if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-" and "T" not in raw and " " not in raw:
            return datetime.combine(date.fromisoformat(raw[:10]), settlement_time, tzinfo=NY)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return _as_ny(parsed)
    except Exception as exc:
        raise ValueError(f"expiry_at invalido: {raw!r}") from exc


def bs_year_fraction(*, expiry_at: Any | None = None, now: datetime | None = None,
                     dte_days: float | None = None,
                     settlement_time: dtime = DEFAULT_SETTLEMENT_TIME) -> float:
    """Exact ACT/365 year fraction for option pricing/Greeks.

    Prefer ``expiry_at`` when available. ``dte_days`` is retained for already
    normalized chain rows; it is interpreted literally as calendar days, with no
    one-minute or 0.01-day expansion.
    """
    floor_seconds = _min_bs_seconds()
    if expiry_at is not None:
        seconds = (_coerce_expiry(expiry_at, settlement_time) - _as_ny(now)).total_seconds()
    else:
        try:
            d = float(dte_days)
        except Exception:
            d = float("nan")
        if not math.isfinite(d):
            return float("nan")
        seconds = d * SECONDS_PER_DAY
    return max(float(seconds), floor_seconds) / SECONDS_PER_YEAR


def year_fraction(dte_days: float) -> float:
    """Compatibility scalar entry point: calendar-day DTE -> ACT/365 T."""
    return bs_year_fraction(dte_days=dte_days)


def year_fraction_array(dte_days) -> np.ndarray:
    """Vectorized ACT/365 conversion with the same sub-second floor."""
    d = np.asarray(dte_days, dtype=float)
    floor_days = _min_bs_seconds() / SECONDS_PER_DAY
    finite = np.isfinite(d)
    out = np.full(d.shape, np.nan, dtype=float)
    out[finite] = np.maximum(d[finite], floor_days) / DAYS_PER_YEAR
    return out


def dte_days_from_expiry(expiry_at: Any, now: datetime | None = None,
                         settlement_time: dtime = DEFAULT_SETTLEMENT_TIME) -> float:
    """Calendar DTE with sub-second floor, derived from an actual expiry timestamp."""
    seconds = (_coerce_expiry(expiry_at, settlement_time) - _as_ny(now)).total_seconds()
    return max(seconds, _min_bs_seconds()) / SECONDS_PER_DAY


# ---------------------------------------------------------------- trading-time clock
# Kept separate from BS.  This calendar covers the regular recurring XNYS closures;
# exceptional one-off closures can be supplied with ITM_NYSE_EXTRA_HOLIDAYS.

def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19*a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*l) // 451
    month = (h + l - 7*m + 114) // 31
    day = ((h + l - 7*m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n-1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def nyse_regular_holidays(year: int) -> set[date]:
    h = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),   # MLK
        _nth_weekday(year, 2, 0, 3),   # Presidents Day
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),     # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        h.add(_observed(date(year, 6, 19)))  # Juneteenth
    # New Year's Day of the following calendar year can be observed on Dec 31
    # (e.g. 2022-01-01 Saturday -> 2021-12-31). Keep the session calendar exact
    # across that year boundary rather than checking holidays year-by-year in isolation.
    next_new_year=_observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        h.add(next_new_year)
    raw = os.getenv("ITM_NYSE_EXTRA_HOLIDAYS", "")
    for item in raw.split(","):
        item = item.strip()
        if item:
            try: h.add(date.fromisoformat(item))
            except Exception as _e: _obs_note("expiry_clock:extra_holiday", _e)
    return h


def is_regular_session_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_regular_holidays(day.year)


def trading_minutes_between(start: datetime, end: datetime) -> float:
    """Regular-session minutes between two instants (holiday aware, no overnight time)."""
    start = _as_ny(start); end = _as_ny(end)
    if end <= start:
        return 0.0
    total = 0.0
    day = start.date()
    while day <= end.date():
        if is_regular_session_day(day):
            op = datetime.combine(day, MARKET_OPEN, tzinfo=NY)
            cl = datetime.combine(day, MARKET_CLOSE, tzinfo=NY)
            lo = max(op, start); hi = min(cl, end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        day += timedelta(days=1)
    return total


def trading_year_fraction(start: datetime, end: datetime) -> float:
    return trading_minutes_between(start, end) / (SESSION_MINUTES * SESSIONS_PER_YEAR)


def describe() -> dict[str, Any]:
    return {
        "bs_convention": "ACT_365_EXACT_CALENDAR_SECONDS",
        "bs_floor_seconds": _min_bs_seconds(),
        "trading_convention": "XNYS_REGULAR_SESSION_252",
        "trading_session_minutes": SESSION_MINUTES,
        "trading_sessions_per_year": SESSIONS_PER_YEAR,
        "settlement_default_et": DEFAULT_SETTLEMENT_TIME.strftime("%H:%M:%S"),
        "authority": "BS_CLOCK_AND_TRADING_CLOCK_SEPARATE",
    }
