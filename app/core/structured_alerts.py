"""Small stateful structured alert detector for ITM QUANT runtime events.

It emits state transitions, not trading directions.  Consumers may show these events or
let Sophia explain them.  Scanner remains the only directional authority.
"""
from __future__ import annotations
from typing import Any
from collections import deque
from threading import RLock
from datetime import datetime, timezone
import math


def _f(v):
    try:
        x=float(v);return x if math.isfinite(x) else None
    except Exception:return None


class StructuredAlertEngine:
    def __init__(self, keep:int=200):
        self._lock=RLock();self._last={};self._events=deque(maxlen=max(50,int(keep)))
    def _emit(self,code:str,symbol:str,severity:str,detail:str,context:dict[str,Any]|None=None):
        ev={"timestamp":datetime.now(timezone.utc).isoformat(),"code":code,"symbol":symbol,"severity":severity,"detail":detail,"context":context or {},"authority":"CONTEXT_ONLY"}
        self._events.append(ev);return ev
    def evaluate(self,state:dict[str,Any])->list[dict[str,Any]]:
        sym=str(state.get("active_symbol") or state.get("symbol") or "").upper();key=sym or "GLOBAL";out=[]
        sc=state.get("scanner") or {};direction=str(sc.get("direction") or "WAIT").upper();edge=str(sc.get("edge_state") or "").upper()
        prev=self._last.get(key,{})
        if prev.get("direction") and direction in {"BUY","SELL"} and direction!=prev.get("direction"):
            out.append(self._emit("SCANNER_DIRECTION_CHANGE",sym,"INFO",f"Scanner cambió {prev.get('direction')} → {direction}",{"edge_state":edge}))
        flow=(state.get("flow_pro") or {}).get("latest") or {};score=_f(flow.get("score"))
        if score is not None and score>=80 and (prev.get("flow_signature")!=(flow.get("timestamp"),flow.get("side"),flow.get("strike"))):
            out.append(self._emit("UNUSUAL_FLOW_HIGH",sym,"INFO",f"Flujo inusual score {score:.0f}",{"side":flow.get("side"),"strike":flow.get("strike")}))
        qflags=state.get("tool_quality_flags") or []
        blocked=any(str(f.get("code"))=="CONTEXT_ONLY" for f in qflags if isinstance(f,dict))
        if blocked!=bool(prev.get("blocked")):
            out.append(self._emit("PUBLICATION_CONTEXT_ONLY" if blocked else "PUBLICATION_REARMED",sym,"WARN" if blocked else "INFO","Contexto no accionable por frescura" if blocked else "Publicación cuantitativa rearmada"))
        self._last[key]={"direction":direction,"edge":edge,"flow_signature":(flow.get("timestamp"),flow.get("side"),flow.get("strike")),"blocked":blocked}
        return out
    def recent(self,limit:int=50)->list[dict[str,Any]]:
        with self._lock:return list(self._events)[-max(1,min(int(limit),200)):]

STRUCTURED_ALERTS=StructuredAlertEngine()
