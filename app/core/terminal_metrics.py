from __future__ import annotations

from typing import Any, Dict
import math
import time

_started = time.time()
_counters: Dict[str, float] = {"http_requests":0.0, "refresh_success":0.0, "refresh_error":0.0}
_gauges: Dict[str, float] = {}


def inc(name: str, value: float = 1.0) -> None:
    _counters[name] = _counters.get(name, 0.0) + float(value)


def set_gauge(name: str, value: Any) -> None:
    if value is None or value == '':
        return
    try:
        x=float(value)
    except (TypeError, ValueError):
        return
    if math.isfinite(x):
        _gauges[name]=x


def snapshot() -> Dict[str, Any]:
    return {"uptime_seconds":time.time()-_started,"counters":dict(_counters),"gauges":dict(_gauges)}


def prometheus_text() -> str:
    lines=["# ITM QUANT local metrics"]
    lines.append(f"itmq_uptime_seconds {time.time()-_started:.3f}")
    for k,v in sorted(_counters.items()): lines.append(f"itmq_{k} {v:.6f}")
    for k,v in sorted(_gauges.items()): lines.append(f"itmq_{k} {v:.6f}")
    return "\n".join(lines)+"\n"
