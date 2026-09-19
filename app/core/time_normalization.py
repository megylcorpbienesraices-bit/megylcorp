"""Canonical timestamp helpers used at provider/analytics merge boundaries.

Pandas 2.x can preserve the physical resolution supplied by a provider
(datetime64[us] vs datetime64[ns]).  ``merge_asof`` requires identical dtypes,
so every causal market-data join passes through ``utc_ns`` first.
"""

from __future__ import annotations

from typing import Any
import pandas as pd


def utc_ns(values: Any):
    """Return timezone-aware UTC timestamps forced to nanosecond resolution.

    The helper deliberately preserves the container type (Series/Index-like)
    produced by ``pd.to_datetime`` while normalizing both timezone and unit.
    Invalid timestamps become NaT.
    """
    out = pd.to_datetime(values, errors="coerce", utc=True)
    try:
        return out.astype("datetime64[ns, UTC]")
    except (TypeError, ValueError, AttributeError):
        idx = pd.DatetimeIndex(out).as_unit("ns")
        if isinstance(values, pd.Series):
            return pd.Series(idx, index=values.index, name=values.name)
        return idx
