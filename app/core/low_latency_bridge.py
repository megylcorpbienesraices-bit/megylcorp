"""Optional Rust/ZeroMQ causal stream bridge.

The production capture/order path can run in a separate Rust process. Python subscribes
only to already ordered ITMQ binary frames.  The bridge is intentionally transitional:
it keeps provider capture + watermark ordering outside the GIL while converting a small
normalized event envelope into the existing Pandas/Quant interfaces.

Important invariants
--------------------
* Rust is ingestion/causality authority only; it never changes Scanner direction.
* No sibling process or missing provider is represented as ACTIVE.
* OPTION_QUOTE events are consumed into a causal in-RAM NBBO cache and are not allowed
  to flood the Python event queue.
* OPTION_TRADE prints can be enriched with the most recent ordered quote before they
  enter Dealer Intelligence.  That classification remains an inference, not a fact.
* Every event preserves event/receive/process time and source sequence for Replay/Audit.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Any, Dict, List
import math
import os
import time

import pandas as pd

from .binary_protocol import BinaryEvent
from .dealer_microstructure import classify_option_trade
from .obs import note as _obs_note

try:
    import zmq
except Exception:  # pragma: no cover - optional runtime dependency
    zmq = None


@dataclass
class BridgeStats:
    received: int = 0
    decode_errors: int = 0
    quotes_received: int = 0
    option_trades_received: int = 0
    underlying_trades_received: int = 0
    ordering_violations: int = 0
    queue_overwrites: int = 0
    last_event_ns: int = 0
    last_receive_wall_ns: int = 0
    last_error: str = ""


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _ec_naive(ns: int) -> pd.Timestamp:
    return pd.Timestamp(int(ns), unit="ns", tz="UTC").tz_convert("America/Guayaquil").tz_localize(None)


def _option_direction(option_type: Any, aggressor: Any) -> int:
    call = str(option_type or "").lower().startswith("c")
    ag = str(aggressor or "").upper()
    if ag == "BUY":
        return 1 if call else -1
    if ag == "SELL":
        return -1 if call else 1
    return 0


class RustCausalSubscriber:
    def __init__(self, endpoint: str | None = None, capacity: int = 250_000) -> None:
        self.endpoint = endpoint or os.getenv("ITM_RUST_CAUSAL_ENDPOINT", "tcp://127.0.0.1:5555")
        self.capacity = max(10_000, int(capacity))
        self._rows_by_symbol: dict[str, deque[BinaryEvent]] = defaultdict(lambda: deque(maxlen=self.capacity))
        self._quotes: dict[str, dict[str, Any]] = {}
        self._latest_price: dict[str, dict[str, Any]] = {}
        self._last_trade_px: dict[str, float] = {}
        self._last_trade_sign: dict[str, int] = {}
        self._last_ordering_key: tuple | None = None
        self._lock = Lock(); self._stop = Event(); self._thread: Thread | None = None
        self.stats = BridgeStats()

    @property
    def configured(self) -> bool:
        return str(os.getenv("ITM_RUST_CAUSALITY", "0")).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def available(self) -> bool:
        return zmq is not None

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if not self.configured or not self.available:
            return False
        if self.active:
            return True
        self._stop.clear()
        self._thread = Thread(target=self._run, name="itmq-rust-causal-subscriber", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    @staticmethod
    def _event_key(ev: BinaryEvent) -> tuple:
        return (int(ev.event_time_ns), int(ev.source_seq), int(ev.priority), str(ev.source))

    @staticmethod
    def _contract(payload: Dict[str, Any]) -> str:
        return str(payload.get("contract_symbol") or payload.get("option_symbol") or payload.get("symbol") or "")

    def _enrich_option_trade(self, ev: BinaryEvent) -> BinaryEvent:
        p = dict(ev.payload or {})
        contract = self._contract(p)
        q = self._quotes.get(contract) if contract else None
        if q and int(q.get("event_time_ns", 0)) <= int(ev.event_time_ns):
            qage_ms = max(0.0, (int(ev.event_time_ns) - int(q.get("event_time_ns", ev.event_time_ns))) / 1e6)
            # Only fill missing trade fields; a direct provider may already have a richer NBBO.
            p.setdefault("bid", q.get("bid")); p.setdefault("ask", q.get("ask"))
            p.setdefault("bid_size", q.get("bid_size", 0)); p.setdefault("ask_size", q.get("ask_size", 0))
            p.setdefault("quote_timestamp", _ec_naive(int(q["event_time_ns"])).isoformat())
            p.setdefault("quote_age_ms", qage_ms)
        px = _finite(p.get("trade_price", p.get("price")))
        bid = _finite(p.get("bid")); ask = _finite(p.get("ask"))
        max_age = float(os.getenv("ITM_RUST_NBBO_MAX_AGE_MS", "1500"))
        qage = _finite(p.get("quote_age_ms"))
        if px and px > 0 and not p.get("classification_method"):
            prev_px = self._last_trade_px.get(contract) if contract else None
            prev_sign = self._last_trade_sign.get(contract, 0) if contract else 0
            cls = classify_option_trade(px, bid, ask, quote_age_ms=qage, prev_trade_price=prev_px,
                                        prev_trade_sign=prev_sign, max_quote_age_ms=max_age)
            p["aggressor"] = cls.aggressor
            p["aggressor_confidence"] = float(cls.confidence)
            p["classification_method"] = f"RUST_CAUSAL_{cls.method}"
            p["nbbo_synced"] = bool(cls.nbbo_synced)
            p["quote_quality"] = cls.quote_quality
            p["quote_age_ms"] = cls.quote_age_ms
            p["spread_position"] = cls.spread_position
            if contract:
                self._last_trade_px[contract] = float(px)
                if cls.aggressor == "BUY": self._last_trade_sign[contract] = 1
                elif cls.aggressor == "SELL": self._last_trade_sign[contract] = -1
        direction = int(p.get("direction_sign") or _option_direction(p.get("option_type"), p.get("aggressor")))
        p["direction_sign"] = direction
        contracts = _finite(p.get("contracts", p.get("size")), 0.0) or 0.0
        multiplier = _finite(p.get("multiplier"), 100.0) or 100.0
        if p.get("premium") is None and px is not None:
            p["premium"] = float(px) * contracts * multiplier
        if p.get("directional_premium") is None:
            p["directional_premium"] = direction * float(_finite(p.get("premium"), 0.0) or 0.0)
        p.setdefault("causal_transport", "RUST_ZMQ")
        p.setdefault("rust_causal", True)
        return BinaryEvent(
            event_time_ns=ev.event_time_ns, receive_time_ns=ev.receive_time_ns,
            process_time_ns=ev.process_time_ns, source_seq=ev.source_seq,
            priority=ev.priority, flags=ev.flags, symbol=ev.symbol, source=ev.source,
            event_type=ev.event_type, payload=p,
        )

    def _accept(self, ev: BinaryEvent) -> None:
        key = self._event_key(ev)
        with self._lock:
            if self._last_ordering_key is not None and key < self._last_ordering_key:
                self.stats.ordering_violations += 1
            self._last_ordering_key = max(self._last_ordering_key, key) if self._last_ordering_key is not None else key
            typ = str(ev.event_type or "").upper()
            p = dict(ev.payload or {})
            if typ in {"OPTION_QUOTE", "QUOTE_OPTION"}:
                contract = self._contract(p)
                if contract:
                    self._quotes[contract] = {
                        "event_time_ns": int(ev.event_time_ns),
                        "bid": _finite(p.get("bid", p.get("bp"))), "ask": _finite(p.get("ask", p.get("ap"))),
                        "bid_size": int(_finite(p.get("bid_size", p.get("bs")), 0) or 0),
                        "ask_size": int(_finite(p.get("ask_size", p.get("as")), 0) or 0),
                    }
                self.stats.quotes_received += 1
                return
            if typ in {"OPTION_TRADE", "OPTION_PRINT"}:
                ev = self._enrich_option_trade(ev)
                self.stats.option_trades_received += 1
            elif typ in {"TRADE", "UNDERLYING_TRADE", "FUTURE_TRADE"}:
                price = _finite(p.get("price", p.get("trade_price")))
                if price is not None:
                    self._latest_price[str(ev.symbol).upper()] = {
                        "price": price, "size": _finite(p.get("size", p.get("contracts")), 0.0) or 0.0,
                        "timestamp": _ec_naive(ev.event_time_ns), "received_at": _ec_naive(ev.receive_time_ns),
                        "seq": int(ev.source_seq), "source": str(ev.source), "transport": "RUST_ZMQ",
                    }
                self.stats.underlying_trades_received += 1
            sym = str(ev.symbol or p.get("underlying_symbol") or "").upper()
            q = self._rows_by_symbol[sym]
            before = len(q); q.append(ev)
            if len(q) == before and before == q.maxlen:
                self.stats.queue_overwrites += 1

    def _run(self) -> None:
        assert zmq is not None
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.setsockopt(zmq.RCVHWM, int(os.getenv("ITM_RUST_ZMQ_RCVHWM", "250000")))
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.connect(self.endpoint)
            poller = zmq.Poller(); poller.register(sock, zmq.POLLIN)
            while not self._stop.is_set():
                ready = dict(poller.poll(timeout=100))
                if sock not in ready:
                    continue
                try:
                    raw = sock.recv(copy=False)
                    ev = BinaryEvent.unpack(memoryview(raw.buffer))
                    self._accept(ev)
                    with self._lock:
                        self.stats.received += 1
                        self.stats.last_event_ns = ev.event_time_ns
                        self.stats.last_receive_wall_ns = time.time_ns()
                except Exception as exc:
                    with self._lock:
                        self.stats.decode_errors += 1
                        self.stats.last_error = f"{type(exc).__name__}: {exc}"[:240]
        except Exception as exc:
            with self._lock:
                self.stats.last_error = f"{type(exc).__name__}: {exc}"[:240]
        finally:
            try: sock.close(0)
            except Exception as _e:
                _obs_note('low_latency_bridge:240', _e)

    def drain(self, limit: int = 50_000, symbol: str | None = None) -> List[BinaryEvent]:
        sym = str(symbol or "").upper()
        out: List[BinaryEvent] = []
        with self._lock:
            if sym:
                q = self._rows_by_symbol.get(sym)
                if q:
                    for _ in range(min(int(limit), len(q))): out.append(q.popleft())
            else:
                left = int(limit)
                for key in sorted(self._rows_by_symbol):
                    q = self._rows_by_symbol[key]
                    for _ in range(min(left, len(q))): out.append(q.popleft())
                    left = int(limit) - len(out)
                    if left <= 0: break
        return out

    def drain_market_data(self, symbol: str, limit: int = 100_000) -> Dict[str, Any]:
        """Drain one symbol into existing Quant Core DataFrame contracts."""
        events = self.drain(limit=limit, symbol=symbol)
        option_rows: list[dict] = []
        tick_rows: list[dict] = []
        other = 0
        for ev in events:
            p = dict(ev.payload or {})
            typ = str(ev.event_type or "").upper()
            ts = _ec_naive(ev.event_time_ns); recv = _ec_naive(ev.receive_time_ns); proc = _ec_naive(ev.process_time_ns)
            if typ in {"OPTION_TRADE", "OPTION_PRINT"}:
                row = dict(p)
                row.update({
                    "timestamp": ts, "received_at": recv, "process_time": proc,
                    "underlying_symbol": str(p.get("underlying_symbol") or ev.symbol).upper(),
                    "seq": int(ev.source_seq), "source_seq": int(ev.source_seq),
                    "flow_source": str(p.get("flow_source") or f"{ev.source} · RUST CAUSAL"),
                    "causal_transport": "RUST_ZMQ", "rust_causal": True,
                })
                if "trade_price" not in row and "price" in row: row["trade_price"] = row.get("price")
                if "contracts" not in row and "size" in row: row["contracts"] = row.get("size")
                option_rows.append(row)
            elif typ in {"TRADE", "UNDERLYING_TRADE", "FUTURE_TRADE"}:
                px = _finite(p.get("price", p.get("trade_price")))
                if px is not None:
                    tick_rows.append({
                        "timestamp": ts, "received_at": recv, "process_time": proc,
                        "price": px, "size": _finite(p.get("size", p.get("contracts")), 0.0) or 0.0,
                        "signed_volume": _finite(p.get("signed_volume"), 0.0) or 0.0,
                        "exchange": p.get("exchange"), "seq": int(ev.source_seq),
                        "source": f"{ev.source} · RUST CAUSAL", "symbol": str(ev.symbol).upper(),
                    })
            else:
                other += 1
        opt = pd.DataFrame(option_rows)
        ticks = pd.DataFrame(tick_rows)
        if not opt.empty: opt = opt.sort_values(["timestamp", "seq"], kind="mergesort").reset_index(drop=True)
        if not ticks.empty: ticks = ticks.sort_values(["timestamp", "seq"], kind="mergesort").reset_index(drop=True)
        return {"option_trades": opt, "price_ticks": ticks, "events": len(events), "other": other}

    def latest_price(self, symbol: str) -> Dict[str, Any] | None:
        with self._lock:
            row = self._latest_price.get(str(symbol or "").upper())
            return dict(row) if row else None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            age_ms = None
            if self.stats.last_receive_wall_ns:
                age_ms = max(0.0, (time.time_ns() - self.stats.last_receive_wall_ns) / 1e6)
            buffered = sum(len(q) for q in self._rows_by_symbol.values())
            state = "ACTIVE" if self.active and self.stats.received else "CONNECTED" if self.active else "READY" if self.available else "UNAVAILABLE"
            if not self.configured:
                state = "READY" if self.available else "UNAVAILABLE"
            return {
                "state": state, "configured": self.configured, "endpoint": self.endpoint,
                "received": self.stats.received, "quotes_received": self.stats.quotes_received,
                "option_trades_received": self.stats.option_trades_received,
                "underlying_trades_received": self.stats.underlying_trades_received,
                "decode_errors": self.stats.decode_errors, "ordering_violations": self.stats.ordering_violations,
                "queue_overwrites": self.stats.queue_overwrites, "buffered": buffered,
                "last_event_age_ms": None if age_ms is None else round(age_ms, 3),
                "last_error": self.stats.last_error,
                "authority": "INGESTION/CAUSALITY ONLY",
                "note": "ACTIVE only when a separate Rust process is actually publishing valid ITMQ binary frames.",
            }


class RustCausalIngressPublisher:
    """Transitional Python/Alpaca -> independent Rust causal ingress.

    This is intentionally a bridge, not the final institutional capture path.  It lets
    the existing Alpaca SIP/OPRA WebSocket decoders feed normalized ITMQ binary frames
    into the isolated Rust orderer today; later a direct/PCAP provider can replace this
    publisher without changing Python Quant Core, Scanner or Replay contracts.

    The PUSH socket is non-blocking.  If Rust is down or backpressured, Python keeps its
    existing observed stream as fallback and records the drop; it never stalls market
    ingestion waiting for the Rust process.
    """
    def __init__(self, endpoint: str | None = None) -> None:
        self.endpoint = endpoint or os.getenv("ITM_RUST_INGEST_ENDPOINT", "tcp://127.0.0.1:5554")
        self._lock = Lock()
        self._ctx = None
        self._sock = None
        self.sent = 0
        self.dropped = 0
        self.errors = 0
        self.last_error = ""
        self.last_send_ns = 0

    @property
    def configured(self) -> bool:
        a = str(os.getenv("ITM_RUST_CAUSALITY", "0")).strip().lower() in {"1","true","yes","on"}
        b = str(os.getenv("ITM_RUST_PYTHON_FORWARD", "0")).strip().lower() in {"1","true","yes","on"}
        return a and b

    @property
    def available(self) -> bool:
        return zmq is not None

    def start(self) -> bool:
        if not self.configured or not self.available:
            return False
        with self._lock:
            if self._sock is not None:
                return True
            try:
                self._ctx = zmq.Context.instance()
                self._sock = self._ctx.socket(zmq.PUSH)
                self._sock.setsockopt(zmq.SNDHWM, int(os.getenv("ITM_RUST_INGEST_SNDHWM", "250000")))
                self._sock.setsockopt(zmq.LINGER, 0)
                self._sock.connect(self.endpoint)
                return True
            except Exception as exc:
                self.errors += 1; self.last_error = f"{type(exc).__name__}: {exc}"[:240]
                self._sock = None
                return False

    def stop(self) -> None:
        with self._lock:
            if self._sock is not None:
                try: self._sock.close(0)
                except Exception as _e:
                    _obs_note('low_latency_bridge:382', _e)
            self._sock = None

    @staticmethod
    def _ns(value: Any, fallback: int | None = None) -> int:
        if value is None:
            return int(fallback if fallback is not None else time.time_ns())
        try:
            t = pd.Timestamp(value)
            if pd.isna(t):
                raise ValueError("NaT timestamp")
            if t.tzinfo is None:
                t = t.tz_localize("America/Guayaquil")
            ns = int(t.tz_convert("UTC").value)
            if ns < 0:
                raise ValueError("timestamp outside unsigned wire range")
            return ns
        except Exception:
            return int(fallback if fallback is not None else time.time_ns())

    def publish(self, *, event_time: Any, receive_time: Any = None, process_time: Any = None,
                source_seq: int = 0, priority: int = 20, symbol: str, source: str,
                event_type: str, payload: Dict[str, Any], flags: int = 2) -> bool:
        if not self.configured or not self.available:
            return False
        if self._sock is None and not self.start():
            return False
        now = time.time_ns()
        ev = BinaryEvent(
            event_time_ns=self._ns(event_time, now),
            receive_time_ns=self._ns(receive_time, now),
            process_time_ns=self._ns(process_time, now),
            source_seq=max(0, int(source_seq or 0)), priority=int(priority), flags=int(flags),
            symbol=str(symbol or "").upper(), source=str(source or "PYTHON_FORWARD"),
            event_type=str(event_type or "EVENT").upper(), payload=dict(payload or {}),
        )
        try:
            with self._lock:
                if self._sock is None: return False
                self._sock.send(ev.pack(), flags=zmq.DONTWAIT)
                self.sent += 1; self.last_send_ns = now
            return True
        except Exception as exc:
            # EAGAIN/backpressure is expected to be survivable; never block Alpaca streams.
            self.dropped += 1
            if getattr(exc, "errno", None) not in {getattr(zmq, "EAGAIN", -1)}:
                self.errors += 1; self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            return False

    def status(self) -> Dict[str, Any]:
        state = "ACTIVE" if self.configured and self._sock is not None and self.sent else "CONNECTED" if self.configured and self._sock is not None else "READY" if self.available else "UNAVAILABLE"
        if not self.configured: state = "READY" if self.available else "UNAVAILABLE"
        age_ms = None if not self.last_send_ns else max(0.0, (time.time_ns()-self.last_send_ns)/1e6)
        return {
            "state":state, "configured":self.configured, "endpoint":self.endpoint,
            "sent":self.sent, "dropped":self.dropped, "errors":self.errors,
            "last_send_age_ms":None if age_ms is None else round(age_ms,3),
            "last_error":self.last_error, "mode":"TRANSITIONAL_ALPACA_TO_RUST",
            "note":"Direct/PCAP provider remains preferred; this bridge never blocks Python fallback.",
        }


RUST_CAUSAL_BRIDGE = RustCausalSubscriber()
RUST_CAUSAL_INGRESS = RustCausalIngressPublisher()
