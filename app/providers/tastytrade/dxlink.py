from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets

from .client import TastytradeClient
from .health import TastytradeHealth
from .market_data import TastytradeMarketData
from ...core.obs import note as _obs_note, expected as _obs_expected

logger = logging.getLogger("ITM_QUANT.TastytradeDXLink")


class DXLinkManager:
    """Persistent DXLink market-data connection with one multiplexed feed channel."""

    CHANNEL = 3
    EVENT_FIELDS = {
        "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
        "Trade": ["eventType", "eventSymbol", "price", "dayVolume", "size"],
        "Greeks": ["eventType", "eventSymbol", "volatility", "delta", "gamma", "theta", "rho", "vega"],
        "Summary": ["eventType", "eventSymbol", "openInterest", "dayOpenPrice", "dayHighPrice", "dayLowPrice", "prevDayClosePrice"],
        "Candle": ["eventType", "eventSymbol", "eventFlags", "index", "time", "sequence", "count", "open", "high", "low", "close", "volume", "vwap", "bidVolume", "askVolume", "impVolatility", "openInterest"],
    }

    def __init__(self, client: TastytradeClient, market_data: TastytradeMarketData, health: TastytradeHealth) -> None:
        self.client = client
        self.market_data = market_data
        self.health = health
        self._desired: set[tuple[str, str]] = set()
        self._applied: set[tuple[str, str]] = set()
        self._from_time_ms: dict[tuple[str, str], int] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ws = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="itmq-tastytrade-dxlink")

    async def stop(self) -> None:
        self._stop.set(); self._wake.set()
        if self._ws:
            try: await self._ws.close()
            except Exception as _e:
                _obs_note('dxlink:52', _e)
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError:
                logger.debug('DXLink task cancelled during orderly shutdown')
            except Exception as _e:
                _obs_note('dxlink:stop', _e)
        self._task = None

    def set_subscriptions(self, rows: set[tuple[str, str]]) -> None:
        # Never exceed provider limits due to a UI bug. Practical platform usage is far lower.
        self._desired = set(list(rows)[:20_000])
        self.health.patch(subscriptions=len(self._desired))
        self._wake.set()

    def add_symbol(self, streamer_symbol: str, *, event_types: tuple[str, ...] = ("Quote", "Trade", "Summary"), canonical_symbol: str | None = None, underlying_symbol: str | None = None, role: str | None = None, instrument_type: str | None = None) -> None:
        self.add_symbols([{
            "streamer_symbol": streamer_symbol,
            "events": event_types,
            "canonical_symbol": canonical_symbol,
            "underlying_symbol": underlying_symbol,
            "role": role,
            "instrument_type": instrument_type,
        }])

    def add_candle(self, streamer_symbol: str, *, canonical_symbol: str, underlying_symbol: str | None = None, role: str = "CANDLE", instrument_type: str = "EQUITY", period: str = "1m", lookback_minutes: int = 1440) -> None:
        """Subscribe to provider historical+live Candle aggregates for a small hot universe."""
        base=str(streamer_symbol or "").strip()
        if not base:
            return
        candle_symbol=base if "{=" in base else f"{base}{{={str(period or '1m')}}}"
        self.market_data.map_symbol(candle_symbol,str(canonical_symbol),underlying_symbol=underlying_symbol or canonical_symbol,role=role,instrument_type=instrument_type,metadata={"candle_period":str(period or "1m"),"base_streamer_symbol":base})
        key=("Candle",candle_symbol)
        # fromTime is Unix epoch milliseconds per tastytrade DXLink documentation.
        self._from_time_ms[key]=int((time.time()-max(15,int(lookback_minutes))*60)*1000)
        self._desired.add(key)
        # Candle has a provider-specific 100-subscription cap; ITM uses only selected/related hot lanes.
        candle_keys=[x for x in self._desired if x[0]=="Candle"]
        if len(candle_keys)>24:
            for stale in candle_keys[:-24]:
                self._desired.discard(stale);self._from_time_ms.pop(stale,None)
        self.health.patch(subscriptions=len(self._desired),candle_subscriptions=len([x for x in self._desired if x[0]=="Candle"]))
        self._wake.set()

    def add_symbols(self, items: list[dict[str, Any]]) -> None:
        """Batch subscriptions so a chain hydration causes one DXLink wake/sync.

        A large option chain must not wake the socket once per contract.  The desired
        set is updated atomically in-process and the streamer is notified once.
        """
        changed = False
        for item in items or []:
            streamer_symbol = str(item.get("streamer_symbol") or "")
            if not streamer_symbol:
                continue
            canonical_symbol = item.get("canonical_symbol")
            if canonical_symbol:
                self.market_data.map_symbol(
                    streamer_symbol, str(canonical_symbol),
                    underlying_symbol=item.get("underlying_symbol"),
                    role=item.get("role"), instrument_type=item.get("instrument_type"),
                    metadata=item.get("metadata") or {},
                )
            for typ in tuple(item.get("events") or ("Quote", "Trade", "Summary")):
                key = (str(typ), streamer_symbol)
                if key not in self._desired:
                    self._desired.add(key); changed = True
        # Practical guard below the documented hard ceiling; avoids accidental runaway
        # subscriptions caused by a malformed UI/chain response.
        if len(self._desired) > 20_000:
            self._desired = set(list(self._desired)[:20_000])
            changed = True
        self.health.patch(subscriptions=len(self._desired))
        if changed:
            self._wake.set()

    async def _send(self, obj: dict[str, Any]) -> None:
        if self._ws:
            await self._ws.send(json.dumps(obj, separators=(",", ":")))

    async def _handshake(self, quote: dict[str, Any]) -> None:
        await self._send({"type":"SETUP","channel":0,"version":"0.1-DXF-JS/0.3.0","keepaliveTimeout":60,"acceptKeepaliveTimeout":60})
        authorized = False; channel_open = False; configured = False
        deadline = asyncio.get_running_loop().time() + 15.0
        while asyncio.get_running_loop().time() < deadline and not configured:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=8.0)
            msg = json.loads(raw)
            typ = str(msg.get("type") or "")
            if typ == "AUTH_STATE" and str(msg.get("state") or "").upper() == "UNAUTHORIZED":
                await self._send({"type":"AUTH","channel":0,"token":quote["token"]})
            elif typ == "AUTH_STATE" and str(msg.get("state") or "").upper() == "AUTHORIZED":
                authorized = True
                await self._send({"type":"CHANNEL_REQUEST","channel":self.CHANNEL,"service":"FEED","parameters":{"contract":"AUTO"}})
            elif typ == "CHANNEL_OPENED" and int(msg.get("channel", -1)) == self.CHANNEL:
                channel_open = True
                await self._send({"type":"FEED_SETUP","channel":self.CHANNEL,"acceptAggregationPeriod":0.1,"acceptDataFormat":"COMPACT","acceptEventFields":self.EVENT_FIELDS})
            elif typ == "FEED_CONFIG" and int(msg.get("channel", -1)) == self.CHANNEL:
                configured = True
        if not (authorized and channel_open and configured):
            raise RuntimeError("DXLink handshake incomplete")
        self.health.patch(dxlink="CONNECTED", market_data="CONNECTED", last_error="")
        self._applied.clear()
        await self._sync_subscriptions(reset=True)

    async def _sync_subscriptions(self, *, reset: bool = False) -> None:
        if not self._ws:
            return
        desired = set(self._desired)
        if reset:
            add = desired; remove = set()
        else:
            add = desired - self._applied; remove = self._applied - desired
        if not add and not remove:
            return
        frame: dict[str, Any] = {"type":"FEED_SUBSCRIPTION","channel":self.CHANNEL,"reset":bool(reset)}
        if add:
            rows=[]
            for t,sym in sorted(add):
                rec={"type":t,"symbol":sym}
                if (t,sym) in self._from_time_ms: rec["fromTime"]=int(self._from_time_ms[(t,sym)])
                rows.append(rec)
            frame["add"] = rows
        if remove: frame["remove"] = [{"type":t,"symbol":s} for t,s in sorted(remove)]
        await self._send(frame)
        self._applied = desired

    async def _keepalive(self) -> None:
        while self._ws and not self._stop.is_set():
            await asyncio.sleep(25.0)
            try: await self._send({"type":"KEEPALIVE","channel":0})
            except Exception: return

    async def _consume_feed_data(self, msg: dict[str, Any]) -> None:
        data = msg.get("data") or []
        if not isinstance(data, list) or len(data) < 2:
            return
        # COMPACT frames can contain one or more event-type labels followed by rows.
        # Keep the parser tolerant so a mixed frame never assigns a Quote layout to a
        # Trade/Greeks row merely because the first label in the frame was different.
        event_type: str | None = None
        for item in data:
            if isinstance(item, str):
                event_type = item
            elif isinstance(item, list) and event_type:
                await self.market_data.ingest_compact(event_type, item)

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                quote = await self.client.quote_token(force=False)
                url = quote.get("dxlink-url") or quote.get("websocket-url")
                async with websockets.connect(str(url), open_timeout=12, close_timeout=5, ping_interval=None, max_queue=8192) as ws:
                    self._ws = ws
                    await self._handshake(quote)
                    backoff = 1.0
                    keep = asyncio.create_task(self._keepalive())
                    try:
                        while not self._stop.is_set():
                            recv_task = asyncio.create_task(ws.recv())
                            wake_task = asyncio.create_task(self._wake.wait())
                            done, pending = await asyncio.wait({recv_task, wake_task}, return_when=asyncio.FIRST_COMPLETED, timeout=35.0)
                            for p in pending: p.cancel()
                            if wake_task in done:
                                self._wake.clear(); await self._sync_subscriptions()
                            if recv_task in done:
                                raw = recv_task.result(); msg = json.loads(raw)
                                if str(msg.get("type") or "") == "FEED_DATA":
                                    await self._consume_feed_data(msg)
                                elif str(msg.get("type") or "") == "AUTH_STATE" and str(msg.get("state") or "").upper() != "AUTHORIZED":
                                    raise RuntimeError("DXLink authorization lost")
                    finally:
                        keep.cancel()
                        try: await keep
                        except asyncio.CancelledError:
                            logger.debug('DXLink keepalive cancelled')
                        except Exception as _e:
                            _obs_note('dxlink:keepalive_stop', _e)
            except asyncio.CancelledError:
                _obs_expected('dxlink:run_cancelled')
                break
            except Exception as exc:
                snap = self.health.snapshot()
                self.health.patch(dxlink="DISCONNECTED", market_data="DEGRADED", reconnects=int(snap.get("reconnects") or 0)+1, last_error=f"{type(exc).__name__}: {str(exc)[:160]}")
                logger.warning("[TASTYTRADE DXLINK] reconnecting after %s", type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.8)
            finally:
                self._ws = None
                self._applied.clear()
        self.health.patch(dxlink="DISCONNECTED")
