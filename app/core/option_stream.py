from __future__ import annotations

import json
import msgpack
import math
import threading
import time
from collections import deque
from typing import Any, Dict

import numpy as np
import pandas as pd

from .alpaca_data import load_settings
from .dealer_microstructure import classify_option_trade
from .low_latency_bridge import RUST_CAUSAL_INGRESS
from .provider_data_lake import DATA_LAKE
from .provider_flow_fabric import OPTION_FLOW_FABRIC
from .provider_bus import PROVIDER_BUS
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .session_expectations import activity_expectation
from .contract_spec import row_multiplier

WS_BASE = "wss://stream.data.alpaca.markets/v1beta1"

def _option_timeout_should_warn(symbol: str, now=None) -> bool:
    """Only missing OPRA traffic during an expected-live options session is actionable."""
    return bool(activity_expectation(symbol, now).get("expected_option_flow_live"))


def _finite_or(value, fallback=0.0):
    try:
        v=float(value)
        return v if math.isfinite(v) else float(fallback)
    except Exception:
        return float(fallback)


class LiveOptionFlowStream:
    """OPRA trades + quotes for the most relevant contracts in the current chain.

    This stream improves trade classification by pairing a trade with the latest quote
    observed by the same stream. It is an optional accelerator: if OPRA WebSocket access
    fails, the existing REST polling path remains active.
    """
    def __init__(self) -> None:
        self._lock=threading.RLock();self._stop=threading.Event();self._reconnect=threading.Event()
        self._thread: threading.Thread|None=None;self._connected=False;self._last_error="";self._seq=0;self._causal_seq=0
        self._contracts: list[str]=[];self._meta: Dict[str,Dict[str,Any]]={};self._quotes: Dict[str,Dict[str,Any]]={}
        self._quote_history: Dict[str,deque[Dict[str,Any]]]={};self._last_trade_px: Dict[str,float]={};self._last_trade_sign: Dict[str,int]={}
        self._events: deque[Dict[str,Any]]=deque(maxlen=20000);self._underlying="DIA";self._last_timeout_note=0.0

    def start(self)->None:
        with self._lock:
            if self._thread and self._thread.is_alive():return
            self._stop.clear();self._thread=threading.Thread(target=self._run,name="itm-quant-opra-flow",daemon=True);self._thread.start()

    def stop(self)->None:
        self._stop.set();self._reconnect.set()

    def set_universe(self,snapshot:pd.DataFrame,max_contracts:int=120)->None:
        if snapshot is None or snapshot.empty or "contract_symbol" not in snapshot.columns:return
        x=snapshot.copy();spot=float(pd.to_numeric(x["underlying_price"],errors="coerce").dropna().iloc[-1])
        oi=numeric_column(x,"open_interest",0);vol=numeric_column(x,"volume",0);strike=pd.to_numeric(x.get("strike",spot),errors="coerce").fillna(spot)
        x["_rank"]=(oi+1).map(np.log1p)+(vol+1).map(np.log1p)-0.45*(strike-spot).abs()
        x=x.sort_values("_rank",ascending=False).drop_duplicates("contract_symbol").head(int(max_contracts))
        contracts=x["contract_symbol"].astype(str).tolist();meta=x.set_index("contract_symbol").to_dict("index")
        under=str(x.get("underlying_symbol",pd.Series(["DIA"])).iloc[-1]).upper()
        with self._lock:
            changed=contracts!=self._contracts
            self._contracts=contracts;self._meta=meta;self._underlying=under
            # Keep quotes only for active contracts.
            self._quotes={k:v for k,v in self._quotes.items() if k in meta}
            self._quote_history={k:v for k,v in self._quote_history.items() if k in meta}
            self._last_trade_px={k:v for k,v in self._last_trade_px.items() if k in meta}
            self._last_trade_sign={k:v for k,v in self._last_trade_sign.items() if k in meta}
        try: OPTION_FLOW_FABRIC.register_universe("ALPACA_OPRA", under, len(contracts))
        except Exception as _e:
            _obs_note('option_stream:71', _e)
        if changed:self._reconnect.set()

    def status(self)->Dict[str,Any]:
        with self._lock:
            last=self._events[-1] if self._events else None
            return {"connected":self._connected,"last_error":self._last_error,"contracts":len(self._contracts),"underlying":self._underlying,"last_seq":self._seq,"last_event":dict(last) if last else None}

    def dataframe(self,minutes:int=30)->pd.DataFrame:
        with self._lock:rows=[dict(x) for x in self._events]
        if not rows:return pd.DataFrame()
        df=pd.DataFrame(rows);df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce");df=df.dropna(subset=["timestamp"])
        if minutes>0 and not df.empty:
            cutoff=df["timestamp"].max()-pd.Timedelta(minutes=int(minutes));df=df[df["timestamp"]>=cutoff]
        return df.sort_values("timestamp").reset_index(drop=True)

    def _next_causal_seq(self)->int:
        with self._lock:
            self._causal_seq += 1
            return self._causal_seq

    def _set_conn(self,v:bool,err:str=""):
        with self._lock:
            self._connected=bool(v)
            if err:self._last_error=str(err)[:500]
            elif v:self._last_error=""

    @staticmethod
    def _utc_to_local_naive(v:Any)->pd.Timestamp:
        t=pd.Timestamp(v)
        if t.tzinfo is None:t=t.tz_localize("UTC")
        return t.tz_convert("America/Guayaquil").tz_localize(None)

    def _quote(self,m:Dict[str,Any])->None:
        sym=str(m.get("S") or "")
        if not sym:return
        recv=pd.Timestamp.now(tz="UTC").tz_convert("America/Guayaquil").tz_localize(None)
        try:
            q={"bid":float(m.get("bp")),"ask":float(m.get("ap")),"bid_size":int(m.get("bs") or 0),"ask_size":int(m.get("as") or 0),
               "timestamp":self._utc_to_local_naive(m.get("t")),"received_at":recv}
        except Exception:return
        accepted=False
        with self._lock:
            if sym in self._meta:
                self._quotes[sym]=q
                hist=self._quote_history.setdefault(sym,deque(maxlen=256));hist.append(q);accepted=True
        if accepted:
            qvalues={"contract_symbol":sym,"option_bid":q.get("bid"),"option_ask":q.get("ask"),"option_bid_size":q.get("bid_size",0),"option_ask_size":q.get("ask_size",0),"exchange":str(m.get("x") or "")}
            try:
                DATA_LAKE.archive_raw(source="ALPACA_OPRA", symbol=self._underlying, event_type="OPTION_QUOTE", payload=dict(m), event_time=q["timestamp"], receive_time=q["received_at"], metadata={"contract_symbol": sym})
                DATA_LAKE.archive_normalized(source="ALPACA_OPRA", symbol=self._underlying, event_type="OPTION_QUOTE", values={"contract_symbol":sym,"bid":q.get("bid"),"ask":q.get("ask"),"bid_size":q.get("bid_size",0),"ask_size":q.get("ask_size",0),"exchange":str(m.get("x") or "")}, event_time=q["timestamp"], receive_time=q["received_at"])
            except Exception as _e:
                _obs_note('option_stream:122', _e)
            try:
                # Parent-symbol diagnostics without contaminating underlying price consensus:
                # option bid/ask fields are deliberately namespaced.
                PROVIDER_BUS.ingest(source="ALPACA_OPRA",symbol=self._underlying,event_type="OPTION_QUOTE",values=qvalues,timestamp=q["timestamp"],received_at=q["received_at"])
                OPTION_FLOW_FABRIC.ingest_quote(source="ALPACA_OPRA",underlying_symbol=self._underlying,contract_symbol=sym,timestamp=q["timestamp"],values={"bid":q.get("bid"),"ask":q.get("ask"),"bid_size":q.get("bid_size",0),"ask_size":q.get("ask_size",0)})
            except Exception as _e:
                _obs_note('option_stream:129', _e)
            try:
                RUST_CAUSAL_INGRESS.publish(event_time=q["timestamp"], receive_time=q["received_at"],
                    source_seq=self._next_causal_seq(), priority=10, symbol=self._underlying,
                    source="ALPACA_OPRA_PY_FORWARD", event_type="OPTION_QUOTE",
                    payload={"contract_symbol":sym,"underlying_symbol":self._underlying,
                             "bid":q.get("bid"),"ask":q.get("ask"),"bid_size":q.get("bid_size",0),
                             "ask_size":q.get("ask_size",0),"exchange":str(m.get("x") or "")})
            except Exception as _e:
                _obs_note('option_stream:138', _e)

    def _causal_quote(self,sym:str,trade_ts:pd.Timestamp)->Dict[str,Any]:
        """Latest NBBO whose event-time is not after the trade event-time."""
        with self._lock:hist=list(self._quote_history.get(sym,()))
        for q in reversed(hist):
            try:
                if pd.Timestamp(q.get("timestamp"))<=trade_ts:return dict(q)
            except Exception as _e:
                _obs_note('option_stream:149', _e)
                continue
        return {}

    def _trade(self,m:Dict[str,Any])->None:
        sym=str(m.get("S") or "")
        recv=pd.Timestamp.now(tz="UTC").tz_convert("America/Guayaquil").tz_localize(None)
        try:px=float(m.get("p"));size=float(m.get("s") or 0);ts=self._utc_to_local_naive(m.get("t"))
        except Exception:return
        if not (px>0 and size>0):return
        with self._lock:
            meta=dict(self._meta.get(sym,{ }));prev_trade=self._last_trade_px.get(sym);prev_sign=self._last_trade_sign.get(sym,0)
        if not meta:return
        q=self._causal_quote(sym,ts);bid=q.get("bid");ask=q.get("ask");qts=q.get("timestamp")
        qage=None if qts is None else max(0.0,(ts-pd.Timestamp(qts)).total_seconds()*1000.0)
        cls=classify_option_trade(px,bid,ask,quote_age_ms=qage,prev_trade_price=prev_trade,prev_trade_sign=prev_sign,max_quote_age_ms=1500.0)
        ag=cls.aggressor;conf=cls.confidence;method=cls.method
        with self._lock:
            self._last_trade_px[sym]=px
            if ag=="BUY":self._last_trade_sign[sym]=1
            elif ag=="SELL":self._last_trade_sign[sym]=-1
        is_call=str(meta.get("option_type","")).lower().startswith("c")
        direction=(1 if is_call else -1) if ag=="BUY" else (-1 if is_call else 1) if ag=="SELL" else 0
        mult=row_multiplier(meta,self._underlying)
        premium=px*size*mult
        row={"timestamp":ts,"received_at":recv,"process_time":pd.Timestamp.now(tz="UTC").tz_convert("America/Guayaquil").tz_localize(None),
             "underlying_symbol":self._underlying,"contract_symbol":sym,"strike":meta.get("strike"),"expiration_date":meta.get("expiration_date"),
             "option_type":meta.get("option_type"),"dte":meta.get("dte"),"underlying_price":meta.get("underlying_price"),"trade_price":px,"contracts":size,
             "premium":premium,"bid":bid,"ask":ask,"bid_size":q.get("bid_size",0),"ask_size":q.get("ask_size",0),"quote_timestamp":qts,
             "quote_received_at":q.get("received_at"),"quote_age_ms":cls.quote_age_ms,"nbbo_synced":cls.nbbo_synced,"quote_quality":cls.quote_quality,
             "spread_position":cls.spread_position,"open_interest":meta.get("open_interest",0),"daily_volume":meta.get("volume",0),"iv":meta.get("iv"),
             "provider_gamma":_finite_or(meta.get("provider_gamma"), _finite_or(meta.get("fallback_gamma"),0)),
             "provider_delta":_finite_or(meta.get("provider_delta"), _finite_or(meta.get("fallback_delta"),0)),
             "greeks_source":meta.get("greeks_source","ALPACA"),"iv_source":meta.get("iv_source","ALPACA"),"aggressor":ag,"aggressor_confidence":conf,
             "classification_method":method,"direction_sign":direction,"directional_premium":direction*premium,"exchange":str(m.get("x") or ""),
             "conditions":list(m.get("c") or []),"flow_source":"OPRA WEBSOCKET · CAUSAL NBBO"}
        oi=max(float(meta.get("open_interest",0) or 0),0);vol=max(float(meta.get("volume",0) or 0),0);spot=max(float(meta.get("underlying_price",1) or 1),1e-9);strike=float(meta.get("strike",spot) or spot);dte=max(float(meta.get("dte",30) or 30),0);gamma=abs(_finite_or(meta.get("provider_gamma"), _finite_or(meta.get("fallback_gamma"),0)));delta=abs(_finite_or(meta.get("provider_delta"), _finite_or(meta.get("fallback_delta"),0)))
        premium_s=np.clip((math.log10(premium+1)-3.0)/4.0,0,1);sizeoi_s=np.clip(math.log1p((size/(oi+1))*10.0)/math.log(11.0),0,1);volshare_s=np.clip(size/(vol+1.0),0,1);prox_s=math.exp(-abs(strike-spot)/2.5);expiry_s=math.exp(-dte/14.0);greek_s=np.clip((gamma*120.0+delta*.20),0,1)
        row["flow_score"]=float(np.clip(100*(.34*premium_s+.20*sizeoi_s+.14*conf+.10*prox_s+.08*expiry_s+.08*greek_s+.06*volshare_s),0,100))
        try:
            DATA_LAKE.archive_raw(source="ALPACA_OPRA", symbol=self._underlying, event_type="OPTION_TRADE", payload=dict(m), event_time=ts, receive_time=recv, metadata={"contract_symbol": sym})
            DATA_LAKE.archive_normalized(source="ALPACA_OPRA", symbol=self._underlying, event_type="OPTION_TRADE", values={k:v for k,v in row.items() if k not in {"timestamp","received_at","process_time","quote_timestamp","quote_received_at"}}, event_time=ts, receive_time=recv)
        except Exception as _e:
            _obs_note('option_stream:186', _e)
        causal_seq=self._next_causal_seq()
        try:
            wire_payload={k:v for k,v in row.items() if k not in {"timestamp","received_at","process_time","quote_timestamp","quote_received_at"}}
            for k in ("conditions",):
                if k in wire_payload and not isinstance(wire_payload[k],list): wire_payload[k]=list(wire_payload[k] or [])
            RUST_CAUSAL_INGRESS.publish(event_time=ts, receive_time=recv, process_time=row.get("process_time"),
                source_seq=causal_seq, priority=20, symbol=self._underlying, source="ALPACA_OPRA_PY_FORWARD",
                event_type="OPTION_TRADE", payload=wire_payload)
        except Exception as _e:
            _obs_note('option_stream:196', _e)
        try:
            # Publish to the provider-neutral option-flow fabric.  Consumers select one
            # dynamic raw-tape lane, so adding tastytrade does not double-count OPRA.
            OPTION_FLOW_FABRIC.ingest_trade(source="ALPACA_OPRA",underlying_symbol=self._underlying,row=row)
            PROVIDER_BUS.ingest(source="ALPACA_OPRA",symbol=self._underlying,event_type="OPTION_TRADE",values={
                "contract_symbol":sym,"option_trade_price":px,"contracts":size,"direction_sign":direction,
                "flow_score":row.get("flow_score"),"strike":row.get("strike"),"option_type":row.get("option_type")
            },timestamp=ts,received_at=recv)
        except Exception as _e:
            _obs_note('option_stream:205', _e)
        with self._lock:
            self._seq+=1;row["seq"]=self._seq;self._events.append(row)

    @staticmethod
    def _decode_messages(raw: Any) -> list[Dict[str, Any]]:
        """Decode Alpaca option-stream MsgPack frames (JSON only as defensive fallback)."""
        try:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                obj = msgpack.unpackb(bytes(raw), raw=False, strict_map_key=False)
            elif isinstance(raw, str):
                obj = json.loads(raw)
            else:
                obj = raw
        except Exception:
            return []
        if isinstance(obj, dict):
            return [obj]
        return [m for m in (obj or []) if isinstance(m, dict)] if isinstance(obj, list) else []

    @staticmethod
    def _pack_command(payload: Dict[str, Any]) -> bytes:
        return msgpack.packb(payload, use_bin_type=True)

    def _run(self)->None:
        try:
            from websockets.sync.client import connect
        except Exception as exc:
            self._set_conn(False,f"websockets unavailable: {exc}");return
        backoff=2.0
        while not self._stop.is_set():
            s=load_settings()
            with self._lock:contracts=list(self._contracts)
            if not s or not contracts:
                self._set_conn(False,"Waiting for credentials/option universe");time.sleep(3);continue
            self._reconnect.clear();url=f"{WS_BASE}/{s.option_feed.lower()}"
            try:
                # Alpaca's option stream is MsgPack-only. The Content-Type header is required
                # or the server can return error 412. REST polling remains the fallback path.
                with connect(url,additional_headers={"Content-Type":"application/msgpack"},open_timeout=15,close_timeout=5,ping_interval=20,ping_timeout=20,max_size=8*1024*1024) as ws:
                    ws.send(self._pack_command({"action":"auth","key":s.api_key,"secret":s.secret_key}))
                    authed=False;deadline=time.time()+12
                    while time.time()<deadline and not self._stop.is_set():
                        raw=ws.recv(timeout=5);msgs=self._decode_messages(raw)
                        for m in msgs:
                            if m.get("T")=="success" and m.get("msg")=="authenticated":authed=True
                            elif m.get("T")=="error":raise RuntimeError(m.get("msg") or str(m))
                        if authed:break
                    if not authed:raise RuntimeError("OPRA websocket auth timeout")
                    ws.send(self._pack_command({"action":"subscribe","trades":contracts,"quotes":contracts}))
                    self._set_conn(True);backoff=2
                    while not self._stop.is_set() and not self._reconnect.is_set():
                        try:raw=ws.recv(timeout=8)
                        except TimeoutError as _e:
                            now_mono=time.monotonic()
                            if _option_timeout_should_warn(self._underlying) and now_mono-self._last_timeout_note>=60.0:
                                self._last_timeout_note=now_mono
                                _obs_note('option_stream:timeout_expected_live', _e, severity='DEGRADED')
                            continue
                        for m in self._decode_messages(raw):
                            if m.get("T")=="q":self._quote(m)
                            elif m.get("T")=="t":self._trade(m)
                            elif m.get("T")=="error":raise RuntimeError(m.get("msg") or str(m))
            except Exception as exc:
                self._set_conn(False,str(exc));time.sleep(backoff);backoff=min(20.0,backoff*1.6)

OPTION_STREAM=LiveOptionFlowStream()
