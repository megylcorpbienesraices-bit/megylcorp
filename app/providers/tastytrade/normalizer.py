from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import math

from ...core.feed_adapters import normalize_event


FIELDS = {
    "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
    "Trade": ["eventType", "eventSymbol", "price", "dayVolume", "size"],
    "Greeks": ["eventType", "eventSymbol", "volatility", "delta", "gamma", "theta", "rho", "vega"],
    "Summary": ["eventType", "eventSymbol", "openInterest", "dayOpenPrice", "dayHighPrice", "dayLowPrice", "prevDayClosePrice"],
    "Candle": ["eventType", "eventSymbol", "eventFlags", "index", "time", "sequence", "count", "open", "high", "low", "close", "volume", "vwap", "bidVolume", "askVolume", "impVolatility", "openInterest"],
}


def _finite(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def compact_row(event_type: str, row: list[Any]) -> dict[str, Any]:
    names = FIELDS.get(str(event_type), [])
    return {names[i]: row[i] for i in range(min(len(names), len(row)))}


def normalize_compact(event_type: str, row: list[Any], *, canonical_symbol: str | None = None, received_at: datetime | None = None, source_seq: int | None = None):
    d = compact_row(event_type, row)
    streamer = str(d.get("eventSymbol") or canonical_symbol or "")
    symbol = str(canonical_symbol or streamer).upper()
    received = received_at or datetime.now(timezone.utc)
    payload: dict[str, Any] = {"streamer_symbol": streamer, "provider": "TASTYTRADE", "observed": True,
                               "event_time_valid": False, "event_time_source": "RECEIVE_PROXY"}
    et = str(event_type)
    if et == "Quote":
        payload.update({"bid": _finite(d.get("bidPrice")), "ask": _finite(d.get("askPrice")), "bid_size": _finite(d.get("bidSize")), "ask_size": _finite(d.get("askSize"))})
        out_type = "QUOTE"
    elif et == "Trade":
        payload.update({"price": _finite(d.get("price")), "day_volume": _finite(d.get("dayVolume")), "size": _finite(d.get("size"))})
        out_type = "TRADE"
    elif et == "Greeks":
        payload.update({"iv": _finite(d.get("volatility")), "delta": _finite(d.get("delta")), "gamma": _finite(d.get("gamma")), "theta": _finite(d.get("theta")), "rho": _finite(d.get("rho")), "vega": _finite(d.get("vega")), "greeks_source": "TASTYTRADE_DXLINK_OBSERVED"})
        out_type = "GREEKS"
    elif et == "Summary":
        payload.update({"open_interest": _finite(d.get("openInterest")), "day_open": _finite(d.get("dayOpenPrice")), "day_high": _finite(d.get("dayHighPrice")), "day_low": _finite(d.get("dayLowPrice")), "prev_close": _finite(d.get("prevDayClosePrice")), "oi_semantics": "PROVIDER_SNAPSHOT_EVENT"})
        out_type = "SUMMARY"
    elif et == "Candle":
        payload.update({"open":_finite(d.get("open")),"high":_finite(d.get("high")),"low":_finite(d.get("low")),"close":_finite(d.get("close")),"volume":_finite(d.get("volume")),"vwap":_finite(d.get("vwap")),"bid_volume":_finite(d.get("bidVolume")),"ask_volume":_finite(d.get("askVolume")),"open_interest":_finite(d.get("openInterest")),"candle_index":d.get("index"),"count":_finite(d.get("count")),"historical_aggregate":True})
        out_type = "CANDLE"
    else:
        payload.update(d)
        out_type = et.upper()
    event_time=received
    if et == "Candle" and _finite(d.get("time")) is not None:
        try:
            event_time=datetime.fromtimestamp(float(d.get("time"))/1000.0,tz=timezone.utc)
            payload["event_time_valid"]=True;payload["event_time_source"]="PROVIDER_CANDLE_TIME"
        except Exception:
            event_time=received
            payload["event_time_valid"]=False;payload["event_time_source"]="RECEIVE_PROXY"
    return normalize_event(source="TASTYTRADE_DXLINK", symbol=symbol, event_type=out_type, event_time=event_time, receive_time=received, source_seq=source_seq, payload=payload)
