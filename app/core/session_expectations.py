"""Session-aware provider expectations for ITM QUANT.

Presentation/health policy only. It never fabricates prices and never changes Scanner
or quantitative direction. Its purpose is to distinguish an expected quiet market from
a provider failure so red alerts are reserved for genuine missing data while a feed
*should* be active.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from typing import Any

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")

_FUTURES = {"YM", "MYM", "ES", "MES", "NQ", "MNQ", "RTY", "M2K"}
_EQUITY_LIKE = {"DIA", "SPY", "QQQ", "TQQQ", "XLF", "XLI", "XLK", "GLD", "GDX", "AAPL", "VXX"}
_INDEX_RTH = {"DJX", "SPX", "NDX", "VIX", "VXD"}


def _dt(value: Any = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).astimezone(NY)
    if isinstance(value, datetime):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            d = datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(NY)


def _london_open_ny(day) -> datetime:
    # London 08:00 cash open, DST-aware, projected onto the NY calendar day.
    noon_ny = datetime.combine(day, time(12, 0), tzinfo=NY)
    london_day = noon_ny.astimezone(LONDON).date()
    return datetime.combine(london_day, time(8, 0), tzinfo=LONDON).astimezone(NY)


def _future_open(ny: datetime) -> bool:
    # CME equity-index futures: Sunday 18:00 ET through Friday 17:00 ET with the
    # normal daily 17:00-18:00 maintenance break. This is an expectation clock,
    # not an exchange entitlement claim.
    wd = ny.weekday()  # Mon=0
    t = ny.timetz().replace(tzinfo=None)
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday
        return t >= time(18, 0)
    if wd == 4 and t >= time(17, 0):  # Friday close
        return False
    if time(17, 0) <= t < time(18, 0):
        return False
    return True


def activity_expectation(symbol: str, now: Any = None) -> dict[str, Any]:
    """Return price/options expectations and a user-facing session state."""
    sym = str(symbol or "").upper().strip().lstrip("/")
    ny = _dt(now)
    day = ny.date(); wd = ny.weekday(); t = ny.timetz().replace(tzinfo=None)
    london_open = _london_open_ny(day)
    prem = datetime.combine(day, time(4, 0), tzinfo=NY)
    rth = datetime.combine(day, time(9, 30), tzinfo=NY)
    close = datetime.combine(day, time(16, 0), tzinfo=NY)
    after = datetime.combine(day, time(20, 0), tzinfo=NY)

    if sym in _FUTURES or sym.startswith(("YM", "MYM", "ES", "MES", "NQ", "MNQ", "RTY", "M2K")):
        opened = _future_open(ny)
        return {
            "symbol": sym, "asset_clock": "FUTURES", "session_phase": "FUTURES_SESSION" if opened else "FUTURES_BREAK",
            "price_expectation": "LIVE_REQUIRED" if opened else "EXPECTED_IDLE",
            "options_expectation": "LIVE_REQUIRED" if opened else "STRUCTURAL",
            "expected_price_live": opened, "expected_option_flow_live": opened,
            "display_state": "LIVE" if opened else "EXPECTED_IDLE",
            "detail": "Futures feed should be live" if opened else "Scheduled futures maintenance/weekend; silence is expected",
        }

    weekend = wd >= 5
    if weekend:
        return {"symbol":sym,"asset_clock":"US_CASH","session_phase":"WEEKEND","price_expectation":"EXPECTED_IDLE","options_expectation":"STRUCTURAL","expected_price_live":False,"expected_option_flow_live":False,"display_state":"EXPECTED_IDLE","detail":"US cash/options closed; last valid structure is expected"}

    if london_open <= ny < prem:
        phase = "LONDON"
    elif prem <= ny < rth:
        phase = "PREMARKET"
    elif rth <= ny < close:
        phase = "NEW_YORK"
    elif close <= ny < after:
        phase = "AFTER_HOURS"
    else:
        phase = "OFF_SESSION"

    if sym in _INDEX_RTH:
        price_live = phase == "NEW_YORK"
        price_expectation = "LIVE_REQUIRED" if price_live else "STRUCTURAL"
    else:  # ETFs/equities and unknown US cash symbols
        # RTH and premarket are expected to produce a live underlying feed.  After-hours
        # prints can legitimately be sparse for DIA/sector ETFs; treat them as
        # opportunistic context rather than demanding a trade every 20 seconds.
        # Incoming after-hours ticks are still ingested immediately; this clock only
        # controls health/freshness severity.
        price_live = phase in {"PREMARKET", "NEW_YORK"}
        price_expectation = "LIVE_REQUIRED" if price_live else ("LIVE_IF_ACTIVE" if phase == "AFTER_HOURS" else "EXPECTED_IDLE")

    option_live = phase == "NEW_YORK"
    option_expectation = "LIVE_REQUIRED" if option_live else "STRUCTURAL"
    if price_live:
        display = "LIVE"
        detail = f"{phase}: fresh underlying price expected"
    elif phase == "LONDON":
        display = "STRUCTURAL"
        detail = "London context: US cash feed may be legitimately idle; use last valid structure until premarket"
    elif phase == "AFTER_HOURS":
        display = "STRUCTURAL"
        detail = "After-hours context: trades are opportunistic/sparse; silence alone is not a provider failure"
    else:
        display = "EXPECTED_IDLE"
        detail = f"{phase}: live US cash ticks are not required"
    return {
        "symbol":sym,"asset_clock":"US_CASH","session_phase":phase,
        "price_expectation":price_expectation,"options_expectation":option_expectation,
        "expected_price_live":price_live,"expected_option_flow_live":option_live,
        "display_state":display,"detail":detail,
    }
