from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Dict
from zoneinfo import ZoneInfo

import pandas as pd

from .alpaca_data import load_settings
from .low_latency_bridge import RUST_CAUSAL_INGRESS
from .provider_bus import PROVIDER_BUS
from .provider_flow_fabric import PRICE_TICK_FABRIC
from .provider_data_lake import DATA_LAKE
from .obs import note as _obs_note
from .session_expectations import activity_expectation

EC = ZoneInfo("America/Guayaquil")
WS_URL = "wss://stream.data.alpaca.markets/v2/sip"

def _price_timeout_should_warn(symbol: str, now=None) -> bool:
    """Only a missing SIP event during an expected-live price session is actionable."""
    return bool(activity_expectation(symbol, now).get("expected_price_live"))


class LivePriceStream:
    """Small local SIP trade stream for the currently selected equity/ETF/ETN.

    The option-chain model is intentionally refreshed on its own slower cycle.
    This stream exists so the PRICE in TRACE can move on each reported SIP trade,
    similar to a normal live chart, without pretending Gamma/OI update every tick.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._symbol = "DIA"
        self._connected = False
        self._last_error = ""
        self._seq = 0
        self._causal_seq = 0
        self._ticks: deque[Dict[str, Any]] = deque(maxlen=30000)
        self._last_quote: Dict[str, Any] = {}
        self._last_trade_price: float | None = None
        self._last_trade_sign: int = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._last_timeout_note = 0.0

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="itm-quant-sip-price", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._reconnect.set()

    def set_symbol(self, symbol: str) -> None:
        symbol = str(symbol or "DIA").upper().strip()
        with self._lock:
            if symbol == self._symbol:
                return
            self._symbol = symbol
            self._ticks.clear()
            self._last_quote = {}
            self._last_trade_price = None
            self._last_trade_sign = 0
            self._seq = 0
            self._causal_seq = 0
            self._connected = False
        self._reconnect.set()

    @property
    def symbol(self) -> str:
        with self._lock:
            return self._symbol

    def status(self) -> Dict[str, Any]:
        with self._lock:
            last = self._ticks[-1] if self._ticks else None
            return {
                "symbol": self._symbol,
                "connected": self._connected,
                "last_error": self._last_error,
                "last_seq": self._seq,
                "last_tick": dict(last) if last else None,
            }

    def since(self, seq: int = 0, limit: int = 1500) -> Dict[str, Any]:
        with self._lock:
            rows = [dict(x) for x in self._ticks if int(x.get("seq", 0)) > int(seq)]
            if len(rows) > limit:
                rows = rows[-limit:]
            return {
                "symbol": self._symbol,
                "connected": self._connected,
                "last_error": self._last_error,
                "last_seq": self._seq,
                "ticks": rows,
            }

    def dataframe(self, symbol: str | None = None) -> pd.DataFrame:
        with self._lock:
            if symbol and str(symbol).upper() != self._symbol:
                return pd.DataFrame(columns=["timestamp", "price", "size", "exchange", "conditions", "seq"])
            rows = [dict(x) for x in self._ticks]
        if not rows:
            return pd.DataFrame(columns=["timestamp", "price", "size", "exchange", "conditions", "seq"])
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        return df.dropna(subset=["timestamp", "price"]).sort_values("seq").reset_index(drop=True)

    def _next_causal_seq(self) -> int:
        with self._lock:
            self._causal_seq += 1
            return self._causal_seq

    def _set_conn(self, connected: bool, err: str = "") -> None:
        with self._lock:
            self._connected = bool(connected)
            if err:
                self._last_error = str(err)[:500]
            elif connected:
                self._last_error = ""

    def _add_quote(self, msg: Dict[str, Any]) -> None:
        try:
            if str(msg.get("S", "")).upper() != self._symbol:
                return
            bid = float(msg.get("bp")) if msg.get("bp") is not None else None
            ask = float(msg.get("ap")) if msg.get("ap") is not None else None
            bs = int(msg.get("bs") or 0)
            a_s = int(msg.get("as") or 0)
            raw_ts = pd.Timestamp(msg.get("t"))
            if raw_ts.tzinfo is None:
                raw_ts = raw_ts.tz_localize("UTC")
            local_ts = raw_ts.tz_convert(EC).tz_localize(None)
        except Exception:
            return
        with self._lock:
            self._last_quote = {"bid": bid, "ask": ask, "bid_size": bs, "ask_size": a_s, "timestamp": local_ts.isoformat()}
        received = pd.Timestamp.now(tz="UTC")
        try:
            DATA_LAKE.archive_raw(source="ALPACA_SIP", symbol=self._symbol, event_type="QUOTE", payload=dict(msg), event_time=raw_ts, receive_time=received)
        except Exception as _e:
            _obs_note('live_price:145', _e)
        try:
            PROVIDER_BUS.ingest(
                source="ALPACA_SIP", symbol=self._symbol, event_type="QUOTE",
                values={"bid":bid,"ask":ask,"bid_size":bs,"ask_size":a_s,"exchange":str(msg.get("x") or "")},
                timestamp=raw_ts, received_at=received, sequence_ok=True,
            )
        except Exception as _e:
            _obs_note('live_price:153', _e)
        try:
            RUST_CAUSAL_INGRESS.publish(event_time=local_ts, receive_time=received,
                source_seq=self._next_causal_seq(), priority=10, symbol=self._symbol, source="ALPACA_SIP_PY_FORWARD",
                event_type="QUOTE", payload={"bid":bid,"ask":ask,"bid_size":bs,"ask_size":a_s,"exchange":str(msg.get("x") or "")})
        except Exception as _e:
            _obs_note('live_price:159', _e)

    def _add_trade(self, msg: Dict[str, Any]) -> None:
        try:
            price = float(msg.get("p"))
            size = int(msg.get("s") or 0)
            raw_ts = pd.Timestamp(msg.get("t"))
            if raw_ts.tzinfo is None:
                raw_ts = raw_ts.tz_localize("UTC")
            local_ts = raw_ts.tz_convert(EC).tz_localize(None)
        except Exception:
            return
        with self._lock:
            if str(msg.get("S", "")).upper() != self._symbol:
                return
            q = dict(self._last_quote or {})
            bid, ask = q.get("bid"), q.get("ask")
            sign = 0
            try:
                eps = max(1e-6, price * 1e-7)
                if ask is not None and float(ask) > 0 and price >= float(ask) - eps:
                    sign = 1
                elif bid is not None and float(bid) > 0 and price <= float(bid) + eps:
                    sign = -1
            except Exception:
                sign = 0
            # Tick-rule fallback for trades inside the spread. Preserve the last non-zero sign on equal prints.
            if sign == 0:
                if self._last_trade_price is not None:
                    if price > self._last_trade_price:
                        sign = 1
                    elif price < self._last_trade_price:
                        sign = -1
                    else:
                        sign = self._last_trade_sign
            if sign != 0:
                self._last_trade_sign = sign
            self._last_trade_price = price
            self._seq += 1
            row={
                "seq": self._seq, "timestamp": local_ts.isoformat(), "price": price, "size": size,
                "bid": bid, "ask": ask, "bid_size": q.get("bid_size", 0), "ask_size": q.get("ask_size", 0),
                "aggressor_sign": sign, "signed_volume": int(sign) * int(size), "exchange": str(msg.get("x") or ""),
                "conditions": list(msg.get("c") or []), "tape": str(msg.get("z") or ""),
            }
            self._ticks.append(row)
        received = pd.Timestamp.now(tz="UTC")
        # v1.40.2 · FAST PRICE LANE: publish the observed trade to the provider-neutral
        # display fabric before archival/bookkeeping.  The exact same event still reaches
        # DATA_LAKE, PROVIDER_BUS and the causal ingress below; only presentation latency
        # is removed from the critical path.
        try:
            PRICE_TICK_FABRIC.ingest(
                source="ALPACA_SIP", symbol=self._symbol, timestamp=raw_ts, price=price, size=size,
                signed_volume=int(sign)*int(size), event_type="TRADE", bid=bid, ask=ask,
                exchange=str(msg.get("x") or ""), metadata={"aggressor_sign":sign,"conditions":list(msg.get("c") or []),"tape":str(msg.get("z") or "")},
            )
        except Exception as _e:
            _obs_note('live_price:fast_price_fabric', _e)
        try:
            DATA_LAKE.archive_raw(source="ALPACA_SIP", symbol=self._symbol, event_type="TRADE", payload=dict(msg), event_time=raw_ts, receive_time=received)
        except Exception as _e:
            _obs_note('live_price:208', _e)
        try:
            PROVIDER_BUS.ingest(
                source="ALPACA_SIP", symbol=self._symbol, event_type="TRADE",
                values={"price":price,"size":size,"bid":bid,"ask":ask,"bid_size":q.get("bid_size",0),
                        "ask_size":q.get("ask_size",0),"aggressor_sign":sign,"signed_volume":int(sign)*int(size),
                        "exchange":str(msg.get("x") or "")},
                timestamp=raw_ts, received_at=received, sequence_ok=True,
            )
        except Exception as _e:
            _obs_note('live_price:218', _e)
        try:
            RUST_CAUSAL_INGRESS.publish(event_time=local_ts, receive_time=received,
                source_seq=self._next_causal_seq(), priority=20, symbol=self._symbol, source="ALPACA_SIP_PY_FORWARD",
                event_type="TRADE", payload={"price":price,"size":size,"bid":bid,"ask":ask,
                    "bid_size":q.get("bid_size",0),"ask_size":q.get("ask_size",0),"aggressor_sign":sign,
                    "signed_volume":int(sign)*int(size),"exchange":str(msg.get("x") or ""),
                    "conditions":list(msg.get("c") or []),"tape":str(msg.get("z") or "")})
        except Exception as _e:
            _obs_note('live_price:235', _e)

    def _run(self) -> None:
        # Import here so the rest of the app still starts if an old venv lacks websockets.
        try:
            from websockets.sync.client import connect
        except Exception as exc:
            self._set_conn(False, f"WebSocket package unavailable: {exc}")
            return

        backoff = 2.0
        while not self._stop.is_set():
            s = load_settings()
            if not s:
                self._set_conn(False, "Alpaca credentials not configured")
                time.sleep(5)
                continue
            symbol = self.symbol
            self._reconnect.clear()
            try:
                with connect(WS_URL, open_timeout=15, close_timeout=5, ping_interval=20, ping_timeout=20) as ws:
                    ws.send(json.dumps({"action": "auth", "key": s.api_key, "secret": s.secret_key}))
                    # Wait for authentication response.
                    authed = False
                    deadline = time.time() + 12
                    while time.time() < deadline and not self._stop.is_set():
                        raw = ws.recv(timeout=5)
                        msgs = json.loads(raw) if isinstance(raw, str) else []
                        if isinstance(msgs, dict): msgs = [msgs]
                        for m in msgs:
                            if m.get("T") == "success" and m.get("msg") == "authenticated":
                                authed = True
                            if m.get("T") == "error":
                                raise RuntimeError(m.get("msg") or str(m))
                        if authed: break
                    if not authed:
                        raise RuntimeError("Alpaca SIP websocket authentication timeout")
                    ws.send(json.dumps({"action": "subscribe", "trades": [symbol], "quotes": [symbol]}))
                    self._set_conn(True)
                    backoff = 2.0
                    while not self._stop.is_set() and not self._reconnect.is_set() and symbol == self.symbol:
                        try:
                            raw = ws.recv(timeout=8)
                        except TimeoutError as _e:
                            # Silence is normal outside the symbol's expected LIVE clock.
                            # During an active price session, keep the diagnostic but rate-limit
                            # it so a quiet 8-second window does not flood the operator console.
                            now_mono = time.monotonic()
                            if _price_timeout_should_warn(symbol) and now_mono - self._last_timeout_note >= 60.0:
                                self._last_timeout_note = now_mono
                                _obs_note('live_price:timeout_expected_live', _e, severity='DEGRADED')
                            continue
                        if not isinstance(raw, str):
                            continue
                        msgs = json.loads(raw)
                        if isinstance(msgs, dict): msgs = [msgs]
                        for m in msgs:
                            if m.get("T") == "t":
                                self._add_trade(m)
                            elif m.get("T") == "q":
                                self._add_quote(m)
                            elif m.get("T") == "error":
                                raise RuntimeError(m.get("msg") or str(m))
            except Exception as exc:
                self._set_conn(False, str(exc))
                if self._stop.is_set():
                    break
                time.sleep(backoff)
                backoff = min(20.0, backoff * 1.5)
            finally:
                self._set_conn(False, self._last_error)


PRICE_STREAM = LivePriceStream()
