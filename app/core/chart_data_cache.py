from __future__ import annotations
from datetime import datetime,timezone
from threading import RLock
from typing import Any,Callable
import time

class ChartDataCache:
    """Short-lived shared payload cache for TRACE and Replay.

    The same candles/profiles/heatmap are computed once per key and reused by both
    presentation surfaces. TTL is intentionally short; live ticks continue through the
    existing event-driven stream and no stale cache may govern Scanner.
    """
    def __init__(self,ttl_seconds:float=0.75,max_items:int=48)->None:
        self.ttl=max(0.05,float(ttl_seconds));self.max_items=max(8,int(max_items));self._lock=RLock();self._items={};self.hits=0;self.misses=0
    def get_or_build(self,key:str,builder:Callable[[],dict[str,Any]])->dict[str,Any]:
        now=time.monotonic()
        with self._lock:
            rec=self._items.get(key)
            if rec and now-rec[0]<=self.ttl:
                self.hits+=1;return rec[1]
        payload=builder()
        with self._lock:
            self.misses+=1;self._items[key]=(now,payload)
            if len(self._items)>self.max_items:
                for k,_ in sorted(self._items.items(),key=lambda kv:kv[1][0])[:len(self._items)-self.max_items]:self._items.pop(k,None)
        return payload
    def clear_symbol(self,symbol:str)->None:
        s=str(symbol).upper()
        with self._lock:
            for k in [k for k in self._items if f'|{s}|' in f'|{k}|']:self._items.pop(k,None)
    def status(self)->dict[str,Any]:
        with self._lock:return {"items":len(self._items),"hits":self.hits,"misses":self.misses,"ttl_seconds":self.ttl,"authority":"PRESENTATION_CACHE_ONLY","asof":datetime.now(timezone.utc).isoformat()}

CHART_DATA_CACHE=ChartDataCache()
