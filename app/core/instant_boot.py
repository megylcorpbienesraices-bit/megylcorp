"""Fast-start helpers for ITM QUANT.

The module only stores lightweight *hints* learned from previously validated live
chains. Hints never replace provider data and never become Scanner authority. They
exist so the first provider request can start near the last known adaptive strike
window instead of deliberately downloading a legacy window and then downloading the
same chain a second time.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from threading import RLock
from typing import Any
import json
import math

from ..persistence import category_dir

_LOCK = RLock()
_PATH = category_dir("system") / "instant_boot_hints.json"
_SCHEMA = 1
_MAX_AGE_DAYS = 7


def _read() -> dict[str, Any]:
    with _LOCK:
        try:
            raw = json.loads(_PATH.read_text(encoding="utf-8")) if _PATH.exists() else {}
            if not isinstance(raw, dict) or int(raw.get("schema", 0) or 0) != _SCHEMA:
                return {"schema": _SCHEMA, "windows": {}}
            raw.setdefault("windows", {})
            return raw
        except Exception:
            return {"schema": _SCHEMA, "windows": {}}


def _write(payload: dict[str, Any]) -> None:
    with _LOCK:
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(_PATH)
        except Exception:
            # A cache/hint must never block market-data startup.
            return


def _key(symbol: str, expiry_mode: str) -> str:
    return f"{str(symbol or '').upper().strip()}::{str(expiry_mode or 'AUTO').upper().strip()}"


def get_chain_window_hint(symbol: str, expiry_mode: str, fallback: float) -> tuple[float, str]:
    """Return a recent validated adaptive window or the caller fallback.

    The hint is intentionally bounded relative to the fallback. This prevents stale or
    corrupted local state from causing an accidentally gigantic first OPRA request.
    """
    fb = max(float(fallback), 0.01)
    data = _read()
    row = (data.get("windows") or {}).get(_key(symbol, expiry_mode)) or {}
    try:
        value = float(row.get("window"))
        stamp = datetime.fromisoformat(str(row.get("saved_at")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
        if not math.isfinite(value) or value <= 0 or age > timedelta(days=_MAX_AGE_DAYS):
            raise ValueError
        # Keep the first request sane even if a previous outlier was written.
        value = min(max(value, fb * 0.45), fb * 4.0)
        return float(value), "PERSISTED_SIGMA_HINT"
    except Exception:
        return fb, "LEGACY_FALLBACK"


def save_chain_window_hint(symbol: str, expiry_mode: str, window: float, *, source: str = "VALIDATED_CHAIN") -> None:
    try:
        value = float(window)
        if not math.isfinite(value) or value <= 0:
            return
    except Exception:
        return
    data = _read()
    data.setdefault("windows", {})[_key(symbol, expiry_mode)] = {
        "window": round(value, 6),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
    }
    data["schema"] = _SCHEMA
    _write(data)


def status(symbol: str, expiry_mode: str, fallback: float) -> dict[str, Any]:
    value, source = get_chain_window_hint(symbol, expiry_mode, fallback)
    return {
        "ready": source == "PERSISTED_SIGMA_HINT",
        "symbol": str(symbol or "").upper(),
        "expiry_mode": str(expiry_mode or "AUTO").upper(),
        "window": value,
        "source": source,
        "authority": "PERFORMANCE_HINT_ONLY",
    }
