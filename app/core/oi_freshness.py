"""Open-interest structural freshness diagnostics for ITM QUANT.

OI is not an intraday quote.  Treating it like a 90-second market-data field is
conceptually wrong, but treating any historical OI as permanently fresh is just
as dangerous because GEX/DEX structure depends on it.  This module therefore
uses a *session-aware* structural policy:

* the latest expected business-session OI date is ``STRUCTURAL_VALID``;
* older OI is ``STRUCTURAL_STALE``;
* missing/unparseable/future-dated OI is ``STRUCTURAL_UNKNOWN``;
* the result is SHADOW-only in v1.27.9: it informs Data Quality and the model
  audit but does not by itself trip the hard publication breaker until real
  provider timestamp behaviour has been observed on the VPS.

The goal is epistemic hygiene: structural OI and live prices have different
clocks, and the UI/model should say so explicitly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")

STRUCTURAL_VALID = "STRUCTURAL_VALID"
STRUCTURAL_STALE = "STRUCTURAL_STALE"
STRUCTURAL_UNKNOWN = "STRUCTURAL_UNKNOWN"
BYPASS_REPLAY = "BYPASS_REPLAY"


def _as_date(value: Any) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        return ts.date()
    except Exception:
        return None


def previous_business_day(day: date) -> date:
    """Return the previous Mon-Fri calendar day.

    This intentionally does not pretend to be an exchange holiday calendar.
    Holiday-aware promotion is deferred until LIVE provider timestamps are
    observed and a single authoritative trading calendar is selected.
    """
    out = day - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def expected_oi_session(as_of: Any | None = None) -> date:
    """Latest business-session date whose EOD OI should normally be available."""
    if as_of is None:
        now = datetime.now(tz=NY)
    elif isinstance(as_of, datetime):
        now = as_of if as_of.tzinfo else as_of.replace(tzinfo=NY)
        now = now.astimezone(NY)
    else:
        ts = pd.Timestamp(as_of)
        if ts.tzinfo is None:
            ts = ts.tz_localize(NY)
        now = ts.tz_convert(NY).to_pydatetime()
    day = now.date()
    # Weekend: Friday is both the previous business day and latest plausible OI.
    if day.weekday() >= 5:
        d = day
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    return previous_business_day(day)


def assess_oi_structural_freshness(
    oi_dates: Iterable[Any] | None,
    *,
    as_of: Any | None = None,
    is_replay: bool = False,
) -> dict[str, Any]:
    """Assess OI age without pretending OI is a live quote.

    v1.27.9 ships this as SHADOW_ONLY.  ``would_block_gex_if_promoted`` records
    the future hard-gate decision so LIVE sessions can validate whether the
    provider date semantics match the model before promotion.
    """
    if is_replay:
        return {
            "estado": BYPASS_REPLAY,
            "mode": "SHADOW_ONLY",
            "latest_oi_date": None,
            "expected_session": None,
            "lag_business_sessions": 0,
            "would_block_gex_if_promoted": False,
            "note": "Replay usa la fecha histórica del paquete; la edad contra el reloj LIVE no aplica.",
        }

    parsed = sorted({d for d in (_as_date(x) for x in (oi_dates or [])) if d is not None})
    expected = expected_oi_session(as_of)
    if not parsed:
        return {
            "estado": STRUCTURAL_UNKNOWN,
            "mode": "SHADOW_ONLY",
            "latest_oi_date": None,
            "expected_session": expected.isoformat(),
            "lag_business_sessions": None,
            "would_block_gex_if_promoted": True,
            "note": "OI sin fecha verificable; no se declara fresco por defecto.",
        }

    latest = parsed[-1]
    # Future dates are not evidence of freshness; they are evidence of a clock/
    # semantic mismatch and therefore UNKNOWN until inspected.
    today = (pd.Timestamp(as_of).tz_localize(NY) if as_of is not None and pd.Timestamp(as_of).tzinfo is None
             else pd.Timestamp(as_of).tz_convert(NY) if as_of is not None else pd.Timestamp.now(tz=NY)).date()
    if latest > today:
        return {
            "estado": STRUCTURAL_UNKNOWN,
            "mode": "SHADOW_ONLY",
            "latest_oi_date": latest.isoformat(),
            "expected_session": expected.isoformat(),
            "lag_business_sessions": None,
            "would_block_gex_if_promoted": True,
            "note": "Fecha OI futura; posible desincronización o semántica de proveedor no validada.",
        }

    if latest >= expected:
        state = STRUCTURAL_VALID
        lag = 0
    else:
        state = STRUCTURAL_STALE
        # Count Mon-Fri business days only; exchange-holiday semantics are
        # deliberately not invented before LIVE validation.
        lag = 0
        d = latest
        while d < expected:
            d += timedelta(days=1)
            if d.weekday() < 5:
                lag += 1

    return {
        "estado": state,
        "mode": "SHADOW_ONLY",
        "latest_oi_date": latest.isoformat(),
        "expected_session": expected.isoformat(),
        "lag_business_sessions": int(lag),
        "would_block_gex_if_promoted": state != STRUCTURAL_VALID,
        "note": (
            "OI estructural válido para la última sesión esperada."
            if state == STRUCTURAL_VALID
            else "OI estructural anterior a la última sesión esperada; validar timestamps LIVE antes de promover a hard gate."
        ),
    }
