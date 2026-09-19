"""Optional Redis hot-state mirror for multi-process/institutional deployments.

Local RAM is always the primary low-latency state. Redis is a distribution/recovery
layer and never sits synchronously in front of the event processor. Writes are queued
onto a daemon worker so a slow Redis node cannot block OPRA/SIP processing.
"""

from __future__ import annotations

from collections import deque
from threading import Thread, Event, Lock
from typing import Any, Dict
import json, os, time

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None


class DistributedHotState:
    def __init__(self) -> None:
        self.url=str(os.getenv("ITM_REDIS_URL","")).strip(); self.client=None
        self._q=deque(maxlen=10000);self._lock=Lock();self._stop=Event();self._thread=None;self._error="";self._writes=0
        if self.url and redis is not None:
            try:
                self.client=redis.Redis.from_url(self.url,decode_responses=False,socket_timeout=.05,socket_connect_timeout=.2)
                self.client.ping(); self._thread=Thread(target=self._run,name="itmq-redis-hotstate",daemon=True);self._thread.start()
            except Exception as exc:self._error=f"{type(exc).__name__}: {exc}"[:180];self.client=None
    @property
    def active(self):return self.client is not None and self._thread is not None and self._thread.is_alive()
    def put(self,key:str,payload:Any,ttl:int=3600)->bool:
        if not self.active:return False
        try:data=json.dumps(payload,ensure_ascii=False,separators=(',',':'),default=str).encode('utf-8')
        except Exception:return False
        with self._lock:self._q.append((f"itmq:{key}",data,int(ttl)))
        return True
    def _run(self):
        while not self._stop.is_set():
            item=None
            with self._lock:
                if self._q:item=self._q.popleft()
            if item is None:time.sleep(.01);continue
            try:self.client.set(item[0],item[1],ex=item[2]);self._writes+=1
            except Exception as exc:self._error=f"{type(exc).__name__}: {exc}"[:180];time.sleep(.05)
    def status(self)->Dict[str,Any]:
        return {"state":"ACTIVE" if self.active else "READY" if redis is not None else "UNAVAILABLE","configured":bool(self.url),"queued":len(self._q),"writes":self._writes,"last_error":self._error,"role":"ASYNC HOT-STATE MIRROR · NEVER EVENT-PATH AUTHORITY"}

HOT_STATE_REDIS=DistributedHotState()
