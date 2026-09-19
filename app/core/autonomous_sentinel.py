"""Autonomous Sentinel risk-state evaluator.

The module is intentionally execution-agnostic.  It can recommend HALT/CANCEL/FLATTEN,
but an actual broker execution adapter must explicitly opt in before any order action can
be taken.  This prevents a monitoring component from pretending it physically controls a
broker/exchange connection.
"""

from __future__ import annotations

from typing import Any, Dict
import math, os


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception: return default


def evaluate(*, feed_age_ms: Any=None, latency_p95_ms: Any=None, clock_offset_ns: Any=None,
             order_rate: Any=None, position_size: Any=None, daily_loss: Any=None,
             slippage_bps: Any=None, broker_health: str|None=None, model_health: Any=None) -> Dict[str, Any]:
    thresholds = {
        "feed_age_ms": float(os.getenv("ITM_SENTINEL_MAX_FEED_AGE_MS", "2500")),
        "latency_ms": float(os.getenv("ITM_SENTINEL_MAX_LATENCY_MS", "750")),
        "clock_ns": float(os.getenv("ITM_SENTINEL_MAX_CLOCK_OFFSET_NS", "2000000")),
        "order_rate": float(os.getenv("ITM_SENTINEL_MAX_ORDER_RATE", "50")),
        "position_size": float(os.getenv("ITM_SENTINEL_MAX_POSITION", "1000000000")),
        "daily_loss": float(os.getenv("ITM_SENTINEL_MAX_DAILY_LOSS", "1000000000")),
        "slippage_bps": float(os.getenv("ITM_SENTINEL_MAX_SLIPPAGE_BPS", "50")),
        "model_health_min": float(os.getenv("ITM_SENTINEL_MIN_MODEL_HEALTH", "40")),
    }
    checks=[]
    def chk(name, value, breach, severe=False):
        if value is None: return
        checks.append({"name":name,"value":value,"breach":bool(breach),"severe":bool(severe and breach)})
    fa=_f(feed_age_ms); lat=_f(latency_p95_ms); clk=_f(clock_offset_ns); rate=_f(order_rate); pos=_f(position_size); loss=_f(daily_loss); slip=_f(slippage_bps); mh=_f(model_health)
    chk("FEED AGE",fa,fa is not None and fa>thresholds["feed_age_ms"],True)
    chk("LATENCY",lat,lat is not None and lat>thresholds["latency_ms"],False)
    chk("CLOCK DRIFT",abs(clk) if clk is not None else None,clk is not None and abs(clk)>thresholds["clock_ns"],True)
    chk("ORDER RATE",rate,rate is not None and rate>thresholds["order_rate"],True)
    chk("POSITION SIZE",abs(pos) if pos is not None else None,pos is not None and abs(pos)>thresholds["position_size"],True)
    chk("DAILY LOSS",abs(loss) if loss is not None else None,loss is not None and abs(loss)>thresholds["daily_loss"],True)
    chk("SLIPPAGE",slip,slip is not None and slip>thresholds["slippage_bps"],False)
    chk("MODEL HEALTH",mh,mh is not None and mh<thresholds["model_health_min"],False)
    if broker_health and str(broker_health).upper() not in {"OK","GREEN","LIVE","CONNECTED","ACTIVE"}:
        checks.append({"name":"BROKER HEALTH","value":broker_health,"breach":True,"severe":True})
    severe=sum(1 for c in checks if c["severe"]); breaches=sum(1 for c in checks if c["breach"])
    state="GREEN"; action="NONE"
    if severe>=2: state,action="LOCK","FLATTEN_REQUIRES_EXECUTION_ADAPTER"
    elif severe==1: state,action="HALT","HALT_NEW_ORDERS"
    elif breaches>=2: state,action="CAUTION","HALT_NEW_ORDERS"
    elif breaches==1: state,action="CAUTION","WARN"
    return {"state":state,"recommended_action":action,"checks":checks,"thresholds":thresholds,"execution_connected":False,"authority":"RISK_GUARDIAN","note":"Recommendations only unless a separately authorized execution adapter is connected."}
