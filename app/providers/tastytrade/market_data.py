from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import RLock
from typing import Any

import pandas as pd

from ...core.provider_bus import PROVIDER_BUS
from ...core.provider_data_lake import DATA_LAKE
from ...core.provider_flow_fabric import OPTION_FLOW_FABRIC, PRICE_TICK_FABRIC
from ...core.dealer_microstructure import classify_option_trade
from .health import TastytradeHealth
from .normalizer import normalize_compact
from ...core.obs import note as _obs_note
from ...core.expiry_clock import dte_days_from_expiry


class TastytradeMarketData:
    def __init__(self, health: TastytradeHealth, capacity: int = 100_000) -> None:
        self.health = health
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max(1000, int(capacity)))
        self._recent = deque(maxlen=20_000)
        self._lock = RLock()
        self._seq = 0
        self._stream_to_canonical: dict[str, str] = {}
        self._stream_meta: dict[str, dict[str, Any]] = {}
        # Latest derivative event state keyed by parent -> contract -> event type.
        # This avoids rescanning the raw recent deque on every quantitative cycle.
        self._derivative_state: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        # Same-instrument DXLink Candle history. Kept separate from the live price tape so
        # historical aggregates can never be mistaken for a new real-time trade.
        self._candles: dict[str, deque] = {}

    def map_symbol(self, streamer_symbol: str, canonical_symbol: str, *, underlying_symbol: str | None = None, role: str | None = None, instrument_type: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        if streamer_symbol:
            key=str(streamer_symbol)
            self._stream_to_canonical[key] = str(canonical_symbol).upper()
            self._stream_meta[key] = {
                "canonical_symbol": str(canonical_symbol).upper(),
                "underlying_symbol": str(underlying_symbol or canonical_symbol).upper(),
                "role": str(role or "INSTRUMENT").upper(),
                "instrument_type": str(instrument_type or "UNKNOWN").upper(),
                **dict(metadata or {}),
            }

    async def ingest_compact(self, event_type: str, row: list[Any]) -> None:
        self._seq += 1
        streamer = str(row[1]) if len(row) > 1 else ""
        canonical = self._stream_to_canonical.get(streamer) or streamer
        now = datetime.now(timezone.utc)
        try:
            DATA_LAKE.archive_raw(
                source="TASTYTRADE_DXLINK", symbol=canonical, event_type=str(event_type).upper(),
                payload=list(row), receive_time=now, metadata=dict(self._stream_meta.get(streamer) or {}),
            )
        except Exception as _e:
            _obs_note('market_data:57', _e)
        env = normalize_compact(event_type, row, canonical_symbol=canonical, received_at=now, source_seq=self._seq)
        packed = env.to_dict()
        meta = dict(self._stream_meta.get(streamer) or {})
        payload = dict(packed.get("payload") or {})
        payload.update({k:v for k,v in meta.items() if v is not None})
        packed["payload"] = payload
        role = str(payload.get("role") or "").upper()
        etype_now=str(packed.get("event_type") or event_type).upper()
        if etype_now=="CANDLE" and not (role.endswith("OPTION") or str(payload.get("instrument_type") or "").upper().endswith("OPTION")):
            try:
                c={"timestamp":packed.get("event_time"),"open":payload.get("open"),"high":payload.get("high"),"low":payload.get("low"),"close":payload.get("close"),"volume":payload.get("volume"),"vwap":payload.get("vwap"),"period":payload.get("candle_period") or meta.get("candle_period") or "1m","source":"TASTYTRADE_DXLINK_CANDLE"}
                if all(self._num(c.get(k)) is not None for k in ("open","high","low","close")):
                    with self._lock:
                        q=self._candles.setdefault(str(canonical).upper(),deque(maxlen=5000));q.append(c)
            except Exception as _e:
                _obs_note('market_data:73', _e)
        if role.endswith("OPTION") or str(payload.get("instrument_type") or "").upper().endswith("OPTION"):
            parent = str(payload.get("parent_symbol") or payload.get("underlying_symbol") or "").upper()
            contract = str(canonical or "").upper()
            if parent and contract:
                etype=str(packed.get("event_type") or event_type).upper()
                with self._lock:
                    self._derivative_state.setdefault(parent, {}).setdefault(contract, {})[etype] = packed
                try:
                    if etype=="QUOTE":
                        OPTION_FLOW_FABRIC.ingest_quote(source="TASTYTRADE_DXLINK",underlying_symbol=parent,contract_symbol=contract,timestamp=packed.get("event_time"),values={"bid":payload.get("bid"),"ask":payload.get("ask"),"bid_size":payload.get("bid_size"),"ask_size":payload.get("ask_size")})
                    elif etype=="TRADE":
                        self._publish_option_trade(contract,packed,{**meta,**payload})
                except Exception as _e:
                    # Provider isolation: flow enrichment can never stop DXLink consumption.
                    _obs_note('market_data:87', _e)
        # Underlying/index/future price lane. Derivative contracts never enter the
        # parent price tape. Trade events are preferred; quote midpoints are retained as
        # lower-authority failover observations if a provider exposes quotes but no trades.
        try:
            etype=str(packed.get("event_type") or event_type).upper()
            is_option=role.endswith("OPTION") or str(payload.get("instrument_type") or "").upper().endswith("OPTION")
            if not is_option and etype in {"TRADE","QUOTE"}:
                px=self._num(payload.get("price"))
                if px is None and etype=="QUOTE":
                    b=self._num(payload.get("bid")); a=self._num(payload.get("ask"))
                    px=(b+a)/2.0 if b is not None and a is not None and b>0 and a>0 else b if b is not None and b>0 else a
                if px is not None and px>0:
                    PRICE_TICK_FABRIC.ingest(
                        source="TASTYTRADE_DXLINK", symbol=str(canonical).upper(),
                        timestamp=packed.get("event_time"), price=px,
                        size=self._num(payload.get("size"),0.0) or 0.0, signed_volume=0.0,
                        event_type=etype, bid=payload.get("bid"), ask=payload.get("ask"),
                        event_id=packed.get("event_id"), metadata={"streamer_symbol":streamer,"role":role,
                            "instrument_type":payload.get("instrument_type"),"event_time_valid":bool(payload.get("event_time_valid",False)),
                            "event_time_source":payload.get("event_time_source") or "RECEIVE_PROXY"},
                    )
        except Exception as _e:
            _obs_note('market_data:109', _e)
        try:
            PROVIDER_BUS.ingest(source="TASTYTRADE", symbol=canonical, event_type=str(packed.get("event_type") or event_type), values=payload, timestamp=packed.get("event_time"), received_at=packed.get("receive_time"), source_seq=self._seq, event_id=packed.get("event_id"), sequence_ok=True)
        except Exception as _e:
            _obs_note('market_data:113', _e)
        try:
            self.queue.put_nowait(packed)
        except asyncio.QueueFull:
            try:
                _ = self.queue.get_nowait()
                self.queue.task_done()
                self.queue.put_nowait(packed)
                h = self.health.snapshot()
                self.health.patch(dropped_events=int(h.get("dropped_events") or 0) + 1)
            except Exception as _e:
                _obs_note('market_data:124', _e)
        with self._lock:
            self._recent.append(packed)
        self.health.event(latency_ms=packed.get("latency_ms"), queue_depth=self.queue.qsize())


    @staticmethod
    def _num(v: Any, default: float | None = None) -> float | None:
        try:
            x=float(v)
            return x if x==x and abs(x)!=float("inf") else default
        except Exception:
            return default

    def _publish_option_trade(self, canonical: str, packed: dict[str, Any], meta: dict[str, Any]) -> None:
        """Translate one observed DXLink option Trade into ITM's common flow schema.

        DXLink and Alpaca are kept as separate raw lanes.  The provider-flow fabric
        dynamically chooses the healthiest lane for Scanner/TRACE so two subscriptions
        improve resilience without blindly doubling the same exchange tape.
        """
        payload=dict(packed.get("payload") or {})
        parent=str(meta.get("parent_symbol") or meta.get("underlying_symbol") or payload.get("underlying_symbol") or "").upper()
        role=str(meta.get("role") or payload.get("role") or "").upper()
        itype=str(meta.get("instrument_type") or payload.get("instrument_type") or "").upper()
        if not parent or not (role.endswith("OPTION") or itype.endswith("OPTION")):
            return
        contract=str(canonical or "").upper()
        with self._lock:
            state=dict(self._derivative_state.get(parent,{}).get(contract,{ }))
        q=dict((state.get("QUOTE") or {}).get("payload") or {})
        g=dict((state.get("GREEKS") or {}).get("payload") or {})
        sm=dict((state.get("SUMMARY") or {}).get("payload") or {})
        trade_px=self._num(payload.get("price")); size=self._num(payload.get("size"),0.0) or 0.0
        if trade_px is None or trade_px<=0 or size<=0:
            return
        bid=self._num(q.get("bid")); ask=self._num(q.get("ask"))
        trade_ts=packed.get("event_time") or datetime.now(timezone.utc).isoformat()
        quote_ts=(state.get("QUOTE") or {}).get("event_time")
        quote_age_ms=None
        if quote_ts:
            try:
                qt=datetime.fromisoformat(str(quote_ts).replace("Z","+00:00")); tt=datetime.fromisoformat(str(trade_ts).replace("Z","+00:00"))
                if qt.tzinfo is None: qt=qt.replace(tzinfo=timezone.utc)
                if tt.tzinfo is None: tt=tt.replace(tzinfo=timezone.utc)
                quote_age_ms=max(0.0,(tt-qt).total_seconds()*1000.0)
            except Exception:
                quote_age_ms=None
        cls=classify_option_trade(trade_px,bid,ask,quote_age_ms=quote_age_ms,max_quote_age_ms=1500.0)
        opt_type=str(meta.get("option_type") or payload.get("option_type") or "").lower()
        is_call=opt_type.startswith("c")
        direction=(1 if is_call else -1) if cls.aggressor=="BUY" else (-1 if is_call else 1) if cls.aggressor=="SELL" else 0
        multiplier=self._num(meta.get("contract_multiplier"))
        if multiplier is None and itype in {"EQUITY_OPTION","INDEX_OPTION"}:
            multiplier=100.0
        elif multiplier is None and itype=="FUTURE_OPTION":
            # Future options do not share the equity 100x convention. If DXLink
            # metadata omits the contract multiplier, fall back only to the selected
            # OWN futures product registry (YM=5, MYM=0.5); never guess 100.
            try:
                from ...core.instruments import get as _get_instrument
                component=str(meta.get("component_symbol") or parent).upper()
                candidate=float(_get_instrument(component).multiplier)
                multiplier=candidate if candidate>0 else None
            except Exception as exc:
                _obs_note("market_data:future_option_multiplier", exc, severity="DEGRADED")
                multiplier=None
        premium=trade_px*size*multiplier if multiplier and multiplier>0 else None
        try:
            cons=PROVIDER_BUS.snapshot(parent)
            under_px=self._num(cons.get("consensus_price")) if cons.get("ready") else None
        except Exception:
            under_px=None
        row={
            "timestamp":trade_ts,"received_at":packed.get("receive_time"),"underlying_symbol":parent,
            "contract_symbol":contract,"strike":self._num(meta.get("strike")),"expiration_date":meta.get("expiration"),
            "option_type":opt_type,"dte":self._num(meta.get("dte")),"underlying_price":under_px,
            "trade_price":trade_px,"contracts":size,"premium":premium,
            "premium_semantics":"PRICE_X_CONTRACTS_X_MULTIPLIER" if premium is not None else "UNSCALED_PRICE_X_CONTRACTS",
            "bid":bid,"ask":ask,"bid_size":self._num(q.get("bid_size"),0.0),"ask_size":self._num(q.get("ask_size"),0.0),
            "quote_timestamp":quote_ts,"quote_age_ms":cls.quote_age_ms,"nbbo_synced":cls.nbbo_synced,
            "quote_quality":cls.quote_quality,"spread_position":cls.spread_position,
            "open_interest":self._num(sm.get("open_interest"),0.0) or 0.0,"daily_volume":self._num(payload.get("day_volume"),0.0) or 0.0,
            "iv":self._num(g.get("iv")),"provider_gamma":self._num(g.get("gamma"),0.0) or 0.0,
            "provider_delta":self._num(g.get("delta"),0.0) or 0.0,"greeks_source":"TASTYTRADE_DXLINK_OBSERVED",
            "iv_source":"TASTYTRADE_DXLINK_OBSERVED","aggressor":cls.aggressor,"aggressor_confidence":cls.confidence,
            "classification_method":cls.method,"direction_sign":direction,
            "directional_premium":direction*premium if premium is not None else None,
            "flow_score":None,"flow_source":"TASTYTRADE DXLINK · OBSERVED OPTION TRADE",
            "instrument_type":itype,"component_symbol":meta.get("component_symbol"),
            "event_time_valid":bool(payload.get("event_time_valid",False)),
            "event_time_source":payload.get("event_time_source") or "RECEIVE_PROXY",
        }
        OPTION_FLOW_FABRIC.ingest_trade(source="TASTYTRADE_DXLINK",underlying_symbol=parent,row=row)
        # Parent observation is namespaced so option prices can never contaminate the
        # underlying composite price in PROVIDER_BUS.snapshot().
        PROVIDER_BUS.ingest(source="TASTYTRADE_DXLINK",symbol=parent,event_type="OPTION_TRADE",values={
            "contract_symbol":contract,"option_trade_price":trade_px,"contracts":size,"strike":row.get("strike"),
            "option_type":opt_type,"direction_sign":direction,"aggressor":cls.aggressor,
            "event_time_valid":row.get("event_time_valid"),"event_time_source":row.get("event_time_source")
        },timestamp=trade_ts,received_at=packed.get("receive_time"),source_seq=self._seq,event_id=packed.get("event_id"),sequence_ok=True)



    def option_trade_frame(self, underlying_symbol: str, minutes: int = 30) -> pd.DataFrame:
        """Provider-specific diagnostic view of observed tastytrade option trades.

        This is a read-only view over the shared OPTION_FLOW_FABRIC lane; it does not
        create a second tape or duplicate events. Flow/TRACE still consume the one
        dynamically selected canonical lane.
        """
        out=OPTION_FLOW_FABRIC.dataframe(str(underlying_symbol or "").upper(), minutes=max(0,int(minutes)), source="TASTYTRADE_DXLINK")
        if isinstance(out,pd.DataFrame) and not out.empty:
            out=out.copy(); out.attrs["authority"]="TASTYTRADE DXLINK · OBSERVED PROVIDER LANE"
        return out

    def candle_frame(self, symbol: str, period: str = "1m") -> pd.DataFrame:
        """Return provider-observed Candle history without feeding it back as live trades."""
        sym=str(symbol or "").upper();want=str(period or "1m").lower()
        with self._lock:
            rows=[dict(x) for x in self._candles.get(sym,())]
        if not rows:
            return pd.DataFrame(columns=["timestamp","open","high","low","close","volume","source"])
        df=pd.DataFrame(rows)
        if "period" in df.columns:
            chosen=df[df["period"].astype(str).str.lower()==want]
            if not chosen.empty: df=chosen
        df["timestamp"]=pd.to_datetime(df.get("timestamp"),errors="coerce",utc=True)
        for c in ("open","high","low","close","volume"):
            df[c]=pd.to_numeric(df.get(c),errors="coerce")
        return df.dropna(subset=["timestamp","open","high","low","close"]).sort_values("timestamp").drop_duplicates("timestamp",keep="last").reset_index(drop=True)

    def structural_chain_frame(self, underlying_symbol: str, *, strike_window: float = 25.0, expiry_days: int = 21) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Return a normalized OWN-instrument structural option snapshot from DXLink memory.

        This is a failover/confirmation path, not a cross-asset proxy. Only contracts whose
        ``component_symbol`` equals the selected underlying are eligible. FUTURE_OPTION rows
        are accepted only for their own selected future and are priced downstream with Black-76.
        No network call or disk I/O occurs here; it only reads already-streaming Quote/Trade/
        Greeks/Summary events, so it cannot become a refresh-path bottleneck.
        """
        under=str(underlying_symbol or "").upper().strip()
        if not under:
            return pd.DataFrame(), {"ready":False,"source":"TASTYTRADE_DXLINK","reason":"EMPTY_SYMBOL"}
        with self._lock:
            states={k:dict(v) for k,v in self._derivative_state.get(under, {}).items()}
        stock_market_ts=None
        try:
            pxdf=PRICE_TICK_FABRIC.dataframe(under)
            if isinstance(pxdf,pd.DataFrame) and not pxdf.empty:
                last_px=pxdf.iloc[-1]
                spot=self._num(last_px.get("price"))
                raw_ts=pd.Timestamp(last_px.get("timestamp"))
                if not pd.isna(raw_ts):
                    if raw_ts.tzinfo is None:
                        raw_ts=raw_ts.tz_localize("America/Guayaquil")
                    stock_market_ts=raw_ts.tz_convert("UTC").isoformat()
            else:
                spot=None
        except Exception:
            spot=None
        if spot is None:
            try:
                snap=PROVIDER_BUS.snapshot(under)
                spot=self._num(snap.get("consensus_price")) if snap.get("ready") else None
                stock_market_ts=stock_market_ts or snap.get("event_time_frontier")
            except Exception:
                spot=None
        if spot is None or spot<=0:
            return pd.DataFrame(), {"ready":False,"source":"TASTYTRADE_DXLINK","symbol":under,"reason":"NO_OWN_UNDERLYING_PRICE"}
        from ...core.precision_engine import recover_iv_and_greeks
        now=datetime.now(timezone.utc);rows=[];ages=[];provider_iv=0;fallback_iv=0
        for contract,events in states.items():
            if not isinstance(events,dict):
                continue
            packs=[x for x in events.values() if isinstance(x,dict)]
            if not packs:
                continue
            payloads=[dict(x.get("payload") or {}) for x in packs]
            meta={}
            for pp in payloads:meta.update({k:v for k,v in pp.items() if v is not None})
            component=str(meta.get("component_symbol") or "").upper().strip()
            itype=str(meta.get("instrument_type") or "").upper()
            # Never turn a related ETF/index/future chain into the selected instrument.
            if component!=under:
                continue
            if itype not in {"EQUITY_OPTION","INDEX_OPTION","FUTURE_OPTION"}:
                continue
            q=dict((events.get("QUOTE") or {}).get("payload") or {})
            g=dict((events.get("GREEKS") or {}).get("payload") or {})
            sm=dict((events.get("SUMMARY") or {}).get("payload") or {})
            tr=dict((events.get("TRADE") or {}).get("payload") or {})
            strike=self._num(meta.get("strike"));dte=self._num(meta.get("dte"));opt=str(meta.get("option_type") or "").lower()
            exp=str(meta.get("expiration") or "")
            if exp:
                try:
                    dte=dte_days_from_expiry(exp, now)
                except Exception as _e:
                    _obs_note("market_data:expiry_dte", _e)
            if strike is None or dte is None or dte<0 or dte>float(expiry_days) or not opt:
                continue
            if abs(strike-spot)>float(strike_window):
                continue
            bid=self._num(q.get("bid"));ask=self._num(q.get("ask"));last=self._num(tr.get("price"))
            piv=self._num(g.get("iv"));pdelta=self._num(g.get("delta"));pgamma=self._num(g.get("gamma"))
            rec=recover_iv_and_greeks(provider_iv=piv,provider_delta=pdelta,provider_gamma=pgamma,bid=bid,ask=ask,last=last,S=spot,K=strike,dte=max(float(dte),0.0),option_type=opt,symbol=under,provider_name="TASTYTRADE_DXLINK")
            iv=self._num(rec.get("iv"))
            if iv is None or iv<=0:
                continue
            if piv is not None and piv>0:provider_iv+=1
            else:fallback_iv+=1
            event_times=[];market_event_times=[]
            for event_name,pack in events.items():
                if not isinstance(pack,dict):
                    continue
                try:
                    tt=datetime.fromisoformat(str(pack.get("event_time") or pack.get("receive_time") or "").replace("Z","+00:00"))
                    if tt.tzinfo is None:tt=tt.replace(tzinfo=timezone.utc)
                    tt=tt.astimezone(timezone.utc);event_times.append(tt)
                    # Summary/OI can update on a structural/EOD clock.  It must never
                    # make a stale live quote look fresh during RTH.
                    if str(event_name).upper() in {"QUOTE","TRADE","GREEKS"}:
                        market_event_times.append(tt)
                except Exception as _e:
                    _obs_note('market_data:308', _e)
            latest=max(event_times) if event_times else now
            latest_market=max(market_event_times) if market_event_times else latest
            ages.append(max(0.0,(now-latest_market).total_seconds()))
            latest_ec=latest.astimezone(ZoneInfo("America/Guayaquil")).replace(tzinfo=None)
            rows.append({
                "timestamp":latest_ec,"underlying_symbol":under,"underlying_price":float(spot),
                "strike":float(strike),"dte":max(float(dte),0.0),"option_type":"call" if opt.startswith("c") else "put",
                "open_interest":max(self._num(sm.get("open_interest"),0.0) or 0.0,0.0),
                "volume":max(self._num(tr.get("day_volume"),0.0) or 0.0,0.0),
                "iv":float(iv),"iv_source":rec.get("iv_source"),
                "greeks_source":rec.get("greeks_source"),"pricing_model":rec.get("pricing_model"),
                "fallback_delta":rec.get("calc_delta"),"fallback_gamma":rec.get("calc_gamma"),
                "calc_vanna":rec.get("calc_vanna"),"calc_charm":rec.get("calc_charm"),"calc_speed":rec.get("calc_speed"),
                "greeks_dislocation":rec.get("greeks_dislocation",False),"provider_delta_diff":rec.get("provider_delta_diff"),"provider_gamma_diff_pct":rec.get("provider_gamma_diff_pct"),
                "quote_source":"TASTYTRADE DXLINK","provider_delta":pdelta,"provider_gamma":pgamma,
                "bid":bid,"ask":ask,"last":last,"contract_symbol":str(contract).upper(),"expiration_date":exp,
                "open_interest_date":sm.get("open_interest_date"),"open_interest_date_source":"PROVIDER" if sm.get("open_interest_date") else "UNKNOWN",
                "option_market_timestamp":latest_market.isoformat(),
                "stock_market_timestamp":stock_market_ts,"contract_multiplier":self._num(meta.get("contract_multiplier")),
            })
        df=pd.DataFrame(rows)
        if df.empty:
            return df,{"ready":False,"source":"TASTYTRADE_DXLINK","symbol":under,"reason":"NO_OWN_STRUCTURAL_CONTRACTS_IN_MEMORY","contracts_seen":len(states)}
        df=df.sort_values(["strike","option_type","expiration_date"]).drop_duplicates(["contract_symbol","expiration_date","strike","option_type"]).reset_index(drop=True)
        oi_cov=100.0*float((pd.to_numeric(df["open_interest"],errors="coerce").fillna(0)>0).mean()) if len(df) else 0.0
        max_age=max(ages) if ages else None;min_age=min(ages) if ages else None
        option_times=[pd.Timestamp(x) for x in df.get("option_market_timestamp",pd.Series(dtype=object)).dropna().tolist() if str(x)]
        latest_option_ts=max(option_times).isoformat() if option_times else None
        oi_dates=sorted({str(x) for x in df.get("open_interest_date",pd.Series(dtype=object)).dropna().tolist() if str(x)})
        now_ny=now.astimezone(ZoneInfo("America/New_York"))
        if under in {"YM","MYM"}:
            try:
                st=pd.Timestamp(stock_market_ts) if stock_market_ts else None
                if st is not None and st.tzinfo is None: st=st.tz_localize("UTC")
                stock_age=(pd.Timestamp(now)-st.tz_convert("UTC")).total_seconds() if st is not None else None
            except Exception:
                stock_age=None
            market_state="FUTURES_SESSION" if stock_age is not None and 0 <= stock_age <= 30.0 else "CLOSED"
        else:
            market_state="CLOSED"
            if now_ny.weekday()<5:
                nt=now_ny.time()
                from datetime import time as _clock_time
                if _clock_time(9,30)<=nt<_clock_time(16,0): market_state="REGULAR"
                elif _clock_time(4,0)<=nt<_clock_time(9,30): market_state="PREMARKET"
                elif _clock_time(16,0)<=nt<_clock_time(20,0): market_state="AFTERHOURS"
        meta={"ready":True,"source":"TASTYTRADE_DXLINK LIVE MEMORY","symbol":under,"spot":float(spot),"contracts":int(len(df)),
              "provider_iv_count":provider_iv,"fallback_iv_count":fallback_iv,"iv_coverage_pct":100.0,"oi_coverage_pct":round(oi_cov,2),
              "unique_strikes":int(df.strike.nunique()),"expirations":int(df.expiration_date.nunique()),
              "expiration_list":sorted(str(x) for x in df.expiration_date.dropna().unique().tolist()),
              "oi_dates":oi_dates,"latest_option_market_timestamp":latest_option_ts,
              "stock_market_timestamp":stock_market_ts,"quality_clock":now.isoformat(),"market_state":market_state,
              "freshest_event_age_seconds":None if min_age is None else round(min_age,3),"oldest_event_age_seconds":None if max_age is None else round(max_age,3),
              "strike_window":float(strike_window),"expiry_days":int(expiry_days),
              "authority":"OWN_INSTRUMENT_DXLINK_STREAM_NO_PROXY","network_calls":0}
        return df,meta

    def derivative_feature(self, underlying_symbol: str) -> dict[str, Any]:
        """Observed derivative structure from DXLink Greeks + Summary/OI.

        This is deliberately *not* dealer GEX/DEX. It is an observed OI-weighted Delta
        balance and Gamma-intensity diagnostic. Dealer-side assumptions remain the job
        of ITM QUANT's own model.
        """
        under=str(underlying_symbol or "").upper()
        with self._lock:
            states={k:dict(v) for k,v in self._derivative_state.get(under, {}).items()}
        rows=[]
        for contract, events in states.items():
            g=events.get("GREEKS") or {}; sm=events.get("SUMMARY") or {}
            gp=dict(g.get("payload") or {}); sp=dict(sm.get("payload") or {})
            meta={**sp, **gp}
            try: oi=float(sp.get("open_interest") or 0.0)
            except Exception: oi=0.0
            try: delta=float(gp.get("delta")) if gp.get("delta") is not None else None
            except Exception: delta=None
            try: gamma=float(gp.get("gamma")) if gp.get("gamma") is not None else None
            except Exception: gamma=None
            if oi <= 0 or delta is None:
                continue
            typ=str(meta.get("option_type") or "").lower()
            family=str(meta.get("instrument_type") or "OPTION").upper()
            rows.append({"contract":contract,"option_type":typ,"family":family,"oi":oi,"delta":delta,"gamma":gamma,"delta_oi":delta*oi})
        total_abs=sum(abs(float(x["delta_oi"])) for x in rows)
        signed=sum(float(x["delta_oi"]) for x in rows)
        ratio=signed/total_abs if total_abs>0 else 0.0
        sign=1 if ratio>0.06 else -1 if ratio<-0.06 else 0
        completeness=min(1.0,len(rows)/max(1,len(states))) if states else 0.0
        confidence=min(100.0,abs(ratio)*70.0+completeness*30.0) if rows else 0.0
        call_oi=sum(float(x["oi"]) for x in rows if str(x["option_type"]).startswith("c"))
        put_oi=sum(float(x["oi"]) for x in rows if str(x["option_type"]).startswith("p"))
        families={}
        for fam in sorted({str(x["family"]) for x in rows}):
            rr=[x for x in rows if x["family"]==fam]; den=sum(abs(float(x["delta_oi"])) for x in rr)
            families[fam]={"contracts":len(rr),"open_interest":sum(float(x["oi"]) for x in rr),"delta_oi_balance":(sum(float(x["delta_oi"]) for x in rr)/den if den else 0.0),"gamma_oi_intensity":sum(abs(float(x.get("gamma") or 0.0))*float(x["oi"]) for x in rr)}
        return {
            "directional":{"derivatives":{"sign":sign,"confidence":round(confidence,2)}},
            "contracts_ready":len(rows),"contracts_seen":len(states),"completeness":round(completeness*100.0,2),
            "observed_delta_oi_balance":round(ratio,6),"call_oi":call_oi,"put_oi":put_oi,
            "put_call_oi_ratio":(put_oi/call_oi if call_oi>0 else None),"families":families,
            "authority":"OBSERVED_DERIVATIVE_STRUCTURE_NOT_DEALER_POSITION_ASSUMPTION",
            "math_rule":"CONTRACT_GREEKS_AND_OI_STAY_SEPARATE_BY_FAMILY_BEFORE_NORMALIZED_BALANCE",
        }

    def derivative_summary(self, underlying_symbol: str) -> dict[str, Any]:
        under=str(underlying_symbol or "").upper()
        with self._lock:
            rows=list(self._recent)
        rows=[r for r in rows if str(((r.get("payload") or {}).get("underlying_symbol") or "")).upper()==under]
        contracts={str(r.get("symbol") or "") for r in rows if str(((r.get("payload") or {}).get("role") or "")).endswith("OPTION")}
        greeks=sum(1 for r in rows if str(r.get("event_type") or "").upper()=="GREEKS")
        summaries=sum(1 for r in rows if str(r.get("event_type") or "").upper()=="SUMMARY")
        return {"underlying":under,"events":len(rows),"contracts":len(contracts),"greeks_events":greeks,"summary_events":summaries,"authority":"OBSERVED_PROVIDER_DATA_ONLY"}

    def recent(self, symbol: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sym = str(symbol or "").upper()
        with self._lock:
            rows = list(self._recent)
        if sym:
            rows = [r for r in rows if str(r.get("symbol") or "").upper() == sym]
        return rows[-max(1, int(limit)):]
