"""Provider-neutral LIVE option-flow fabric for ITM QUANT v1.26.2.

The platform can observe the same option market through more than one authorized feed
(Alpaca OPRA, tastytrade/dxFeed, and future adapters).  Consumers must not be hard-wired
to one provider, and raw prints from two feeds must not simply be added together because
that can double-count the same exchange trade.

This module therefore keeps one bounded in-memory lane per provider and symbol, measures
freshness/completeness from *observed* events, and dynamically selects the healthiest lane
for the canonical tape used by Flow/TRACE/Dealer Intelligence.  Other live providers remain
available as confirmation/diagnostic inputs.  Provider names never receive fixed weights.
"""

from __future__ import annotations

from collections import defaultdict, deque
from itertools import islice
from datetime import datetime, timezone
from threading import Condition, RLock
from typing import Any
import math
import time

import pandas as pd

from .source_arbitration import arbitrate_scored
from .session_expectations import activity_expectation

EC = "America/Guayaquil"


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _event_ts(v: Any) -> pd.Timestamp | None:
    """Strict event-time parser. Invalid clocks are rejected, never replaced with now."""
    if v is None or v == "":
        return None
    try:
        t = pd.Timestamp(v)
    except Exception:
        return None
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        # Existing OPRA rows are local Ecuador-naive. Preserve that documented convention.
        try:
            return t.tz_localize(EC).tz_convert("UTC")
        except Exception:
            try:return t.tz_localize("UTC")
            except Exception:return None
    try:return t.tz_convert("UTC")
    except Exception:return None


def _local_naive(v: Any) -> pd.Timestamp:
    t = _event_ts(v)
    return pd.NaT if t is None else t.tz_convert(EC).tz_localize(None)


class UnifiedOptionFlowFabric:
    """Bounded, non-blocking fan-in for observed option trades.

    The hot path performs only in-memory work under a short lock.  No network or disk I/O
    happens here.  Each source keeps an independent lane; ``dataframe()`` chooses the lane
    with the best current observation quality so adding a second feed improves resilience
    instead of multiplying every print.
    """

    def __init__(self, capacity_per_lane: int = 40_000) -> None:
        self._lock = RLock()
        self._capacity = max(2_000, int(capacity_per_lane))
        self._trades: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(dict)
        self._quotes: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._universes: dict[str, dict[str, int]] = defaultdict(dict)
        self._stats: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._seen: dict[tuple[str, str], deque[str]] = defaultdict(lambda: deque(maxlen=20_000))
        self._seen_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._global_seq = 0
        self._canonical_cache: dict[str, tuple[str | None, float]] = {}

    @staticmethod
    def _src(source: str) -> str:
        return str(source or "UNKNOWN").upper().strip()

    @staticmethod
    def _sym(symbol: str) -> str:
        return str(symbol or "").upper().strip()

    def register_universe(self, source: str, symbol: str, contracts: int) -> None:
        src, sym = self._src(source), self._sym(symbol)
        if not sym:
            return
        with self._lock:
            self._universes[sym][src] = max(0, int(contracts or 0))
            st = self._stats[sym].setdefault(src, {})
            st["contracts"] = max(0, int(contracts or 0))
            st["universe_updated_at"] = datetime.now(timezone.utc).isoformat()

    def _lane(self, sym: str, src: str) -> deque[dict[str, Any]]:
        lane = self._trades[sym].get(src)
        if lane is None:
            lane = deque(maxlen=self._capacity)
            self._trades[sym][src] = lane
        return lane

    def _remember(self, sym: str, src: str, key: str) -> bool:
        k = (sym, src)
        seen = self._seen_sets[k]
        if key in seen:
            return False
        q = self._seen[k]
        if len(q) == q.maxlen and q:
            old = q[0]
            seen.discard(old)
        q.append(key)
        seen.add(key)
        return True

    def ingest_quote(self, *, source: str, underlying_symbol: str, contract_symbol: str,
                     timestamp: Any, values: dict[str, Any]) -> None:
        src, sym, contract = self._src(source), self._sym(underlying_symbol), str(contract_symbol or "").upper()
        if not sym or not contract:
            return
        t = _event_ts(timestamp)
        if t is None:
            with self._lock:
                st=self._stats[sym].setdefault(src,{})
                st["invalid_event_time"] = int(st.get("invalid_event_time") or 0) + 1
            return
        with self._lock:
            self._quotes[sym][f"{src}:{contract}"] = {
                "source": src, "contract_symbol": contract, "timestamp": t.isoformat(), **dict(values or {})
            }
            st = self._stats[sym].setdefault(src, {})
            st["last_quote_at"] = t.isoformat()
            st["quotes_total"] = int(st.get("quotes_total") or 0) + 1

    def ingest_trade(self, *, source: str, underlying_symbol: str, row: dict[str, Any]) -> bool:
        src, sym = self._src(source), self._sym(underlying_symbol)
        if not sym:
            return False
        x = dict(row or {})
        contract = str(x.get("contract_symbol") or x.get("symbol") or "").upper().strip()
        ts = _event_ts(x.get("timestamp") or x.get("event_time"))
        if ts is None:
            with self._lock:
                st=self._stats[sym].setdefault(src,{})
                st["invalid_event_time"] = int(st.get("invalid_event_time") or 0) + 1
            return False
        px = _f(x.get("trade_price"), _f(x.get("price")))
        size = _f(x.get("contracts"), _f(x.get("size"), 0.0)) or 0.0
        # Exact-source de-duplication only. Cross-provider lanes are not blindly summed;
        # dataframe() selects one dynamic canonical lane instead.
        eid = str(x.get("event_id") or "")
        key = eid or f"{contract}|{ts.value}|{px}|{size}|{x.get('exchange','')}"
        with self._lock:
            if not self._remember(sym, src, key):
                st = self._stats[sym].setdefault(src, {})
                st["duplicates_suppressed"] = int(st.get("duplicates_suppressed") or 0) + 1
                return False
            self._global_seq += 1
            x["timestamp"] = _local_naive(ts)
            # A successfully parsed direct timestamp is valid unless the adapter
            # explicitly marked it as a receive-time proxy.  This preserves legacy
            # direct fabric callers while keeping Tasty RECEIVE_PROXY provenance.
            if "event_time_valid" not in x:
                x["event_time_valid"] = True
            if not x.get("event_time_source"):
                x["event_time_source"] = "OBSERVED_EVENT_TIME" if x.get("event_time_valid") is True else "RECEIVE_PROXY"
            x["underlying_symbol"] = sym
            x["contract_symbol"] = contract
            x["trade_price"] = px
            x["contracts"] = size
            x["provider_source"] = src
            x.setdefault("flow_source", src)
            x["fabric_seq"] = self._global_seq
            self._lane(sym, src).append(x)
            st = self._stats[sym].setdefault(src, {})
            st["last_trade_at"] = ts.isoformat()
            st["trades_total"] = int(st.get("trades_total") or 0) + 1
            st["contracts_seen"] = len({str(r.get("contract_symbol") or "") for r in self._lane(sym, src) if r.get("contract_symbol")})
        return True

    @staticmethod
    def _coverage_score(rows: list[dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        fields = ("strike", "option_type", "trade_price", "contracts", "bid", "ask", "open_interest", "iv", "provider_gamma", "provider_delta")
        acc = 0.0
        for r in rows[-min(250, len(rows)):]:
            acc += sum(1.0 for k in fields if r.get(k) is not None) / len(fields)
        return 100.0 * acc / min(250, len(rows))

    @staticmethod
    def _sync_score(rows: list[dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        recent = rows[-min(250, len(rows)):]
        vals = [1.0 if bool(r.get("nbbo_synced")) else 0.0 for r in recent if r.get("nbbo_synced") is not None]
        return 100.0 * (sum(vals) / len(vals)) if vals else 35.0

    def _lane_metrics(self, sym: str, src: str, now: pd.Timestamp) -> dict[str, Any]:
        with self._lock:
            rows = list(self._trades.get(sym, {}).get(src, ()))
            st = dict(self._stats.get(sym, {}).get(src, {}))
            contracts = int(self._universes.get(sym, {}).get(src, st.get("contracts") or 0) or 0)
        if rows:
            last = _event_ts(rows[-1].get("timestamp"))
            age = max(0.0, (now - last).total_seconds()) if last is not None else None
            cutoff60 = now - pd.Timedelta(seconds=60)
            trades60 = sum(1 for r in rows if (_event_ts(r.get("timestamp")) is not None and _event_ts(r.get("timestamp")) >= cutoff60))
        else:
            age, trades60 = None, 0
        freshness = 0.0 if age is None else max(0.0, 100.0 - min(100.0, age * 2.0))
        activity = min(100.0, trades60 * 4.0)
        coverage = self._coverage_score(rows)
        sync = self._sync_score(rows)
        recent_clock=rows[-min(250,len(rows)):] if rows else []
        valid_clock=sum(1 for r in recent_clock if r.get("event_time_valid") is True)
        proxy_clock=sum(1 for r in recent_clock if r.get("event_time_source")=="RECEIVE_PROXY")
        clock_factor=(valid_clock+0.65*proxy_clock)/max(1,len(recent_clock)) if recent_clock else 0.0
        # No fixed provider-name weight. Freshness dominates failover; completeness and
        # quote synchronization decide between simultaneously live feeds. Receive-time
        # proxies stay usable but cannot score as highly as true provider event time.
        quality = (0.42 * freshness + 0.22 * coverage + 0.20 * sync + 0.16 * activity) * clock_factor
        if contracts <= 0 and not rows:
            quality = 0.0
        return {
            "source": src, "contracts": contracts, "trades_total": len(rows), "trades_60s": trades60,
            "last_trade_age_seconds": None if age is None else round(age, 3),
            "freshness_score": round(freshness, 2), "coverage_score": round(coverage, 2),
            "quote_sync_score": round(sync, 2), "activity_score": round(activity, 2),
            "event_time_quality_factor":round(clock_factor,3),"valid_event_time_events":valid_clock,"receive_proxy_events":proxy_clock,
            "quality_score": round(quality, 2), "observed": bool(rows),
            **{k:v for k,v in st.items() if k not in {"contracts"}},
        }

    def health(self, symbol: str) -> dict[str, Any]:
        sym = self._sym(symbol)
        now = pd.Timestamp.now(tz="UTC")
        with self._lock:
            sources = sorted(set(self._trades.get(sym, {})) | set(self._universes.get(sym, {})) | set(self._stats.get(sym, {})))
        metrics = [self._lane_metrics(sym, s, now) for s in sources]
        with self._lock:
            previous, selected_at = self._canonical_cache.get(sym,(None,0.0))
        arb=arbitrate_scored([m for m in metrics if float(m.get("quality_score") or 0)>0],previous=previous,
                             previous_selected_at=selected_at if previous else None,now=time.monotonic())
        selected=arb.get("selected")
        with self._lock:
            if selected != previous:self._canonical_cache[sym] = (selected, time.monotonic())
            elif previous:self._canonical_cache[sym] = (previous, selected_at)
        live = [r["source"] for r in metrics if r.get("last_trade_age_seconds") is not None and float(r["last_trade_age_seconds"]) <= 120.0]
        universes = [r["source"] for r in metrics if int(r.get("contracts") or 0) > 0]
        clock = activity_expectation(sym)
        if not universes:
            state, bottleneck = "NO_UNIVERSE", "NO_OPTION_CONTRACT_UNIVERSE"
        elif not live and not clock.get("expected_option_flow_live"):
            state, bottleneck = "STRUCTURAL", None
        elif not live:
            state, bottleneck = "WAITING", "NO_RECENT_OPTION_TRADES"
        elif len(live) == 1:
            state, bottleneck = "LIVE_SINGLE_SOURCE", None
        else:
            state, bottleneck = "LIVE_REDUNDANT", None
        return {
            "symbol": sym, "state": state, "selected_source": selected, "providers": metrics,
            "providers_with_universe": universes, "live_trade_sources": live,
            "redundancy": len(live), "bottleneck": bottleneck,
            "selection_policy": "DYNAMIC_OBSERVATION_QUALITY_WITH_HYSTERESIS_NO_FIXED_PROVIDER_RANK",
            "source_arbitration":arb,
            "double_count_policy": "ONE_CANONICAL_RAW_TAPE_LANE_AT_A_TIME; OTHER_PROVIDERS_CONFIRM/FAILOVER",
            "authority": "OBSERVED_OPTION_FLOW_ONLY",
            "session_expectation": clock,
        }

    def canonical_source(self, symbol: str) -> str | None:
        sym=self._sym(symbol);now=time.monotonic()
        with self._lock:
            cached=self._canonical_cache.get(sym)
        if cached is not None and now-float(cached[1]) <= 0.75:
            return cached[0]
        return self.health(sym).get("selected_source")

    def latest(self, symbol: str, *, source: str | None = None) -> dict[str, Any] | None:
        """Return the latest observed option trade from the current canonical lane.

        Pure in-memory read for event-driven consumers such as Sophia Watch Rules.
        """
        sym = self._sym(symbol)
        src = self._src(source) if source else self.canonical_source(sym)
        if not src:
            return None
        with self._lock:
            lane = self._trades.get(sym, {}).get(src)
            row = dict(lane[-1]) if lane else None
        if row is not None:
            row["canonical_source"] = src
        return row

    def since(self, symbol: str, after: int = 0, limit: int = 1200, *, source: str | None = None) -> dict[str, Any]:
        """Incremental canonical option trades for event-driven local consumers.

        This is in-memory only: no provider request, no disk I/O and no duplicate summing
        across parallel feeds. ``fabric_seq`` is globally monotonic inside this process.
        """
        sym = self._sym(symbol)
        src = self._src(source) if source else self.canonical_source(sym)
        with self._lock:
            rows = [dict(x) for x in self._trades.get(sym, {}).get(src, ()) if int(x.get("fabric_seq") or 0) > int(after)] if src else []
            fabric_seq = int(self._global_seq)
        if len(rows) > max(1, int(limit)):
            rows = rows[-max(1, int(limit)):]
        last = int(rows[-1].get("fabric_seq") or after) if rows else int(after)
        return {"symbol":sym,"source":src,"connected":bool(src),"last_seq":last,"fabric_seq":fabric_seq,"events":rows}

    def dataframe(self, symbol: str, minutes: int = 30, *, source: str | None = None) -> pd.DataFrame:
        sym = self._sym(symbol)
        src = self._src(source) if source else self.canonical_source(sym)
        if not src:
            return pd.DataFrame()
        with self._lock:
            rows = [dict(x) for x in self._trades.get(sym, {}).get(src, ())]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df.get("timestamp"), errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        if minutes > 0 and not df.empty:
            cutoff = df["timestamp"].max() - pd.Timedelta(minutes=int(minutes))
            df = df[df["timestamp"] >= cutoff]
        df.attrs["canonical_source"] = src
        return df.reset_index(drop=True)

    def scheduler_status(self, symbol: str) -> dict[str, Any]:
        """Status adapter compatible with AdaptiveLiveScheduler's sequence contract."""
        sym = self._sym(symbol)
        health = self.health(sym)
        src = health.get("selected_source")
        with self._lock:
            rows = list(self._trades.get(sym, {}).get(str(src), ())) if src else []
            seq = int(rows[-1].get("fabric_seq") or 0) if rows else 0
            last = dict(rows[-1]) if rows else None
        return {
            "connected": bool(health.get("live_trade_sources")), "last_seq": seq, "last_event": last,
            "source": src, "providers": health.get("live_trade_sources"), "state": health.get("state"),
        }


OPTION_FLOW_FABRIC = UnifiedOptionFlowFabric()


class UnifiedPriceTickFabric:
    """Provider-neutral price/tape fabric with dynamic source failover.

    Each provider keeps an independent bounded lane so parallel feeds improve resilience
    without double-counting the same market trade.  One canonical lane is selected from
    observed freshness/activity/completeness. Selection has quality hysteresis to avoid
    flip-flopping between two healthy feeds. No provider name receives a fixed rank.
    """

    def __init__(self, capacity_per_lane: int = 60_000) -> None:
        self._lock = RLock()
        self._capacity = max(5_000, int(capacity_per_lane))
        self._ticks: dict[str, dict[str, deque[dict[str, Any]]]] = defaultdict(dict)
        self._stats: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._selected: dict[str, str] = {}
        self._seen: dict[tuple[str, str], deque[str]] = defaultdict(lambda: deque(maxlen=30_000))
        self._seen_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._seq = 0
        # v1.40.2 · event-driven display wakeup.  The condition shares the fabric
        # lock so provider threads can wake WebSocket consumers immediately after an
        # observed market event without polling the Quant engine or changing source policy.
        self._condition = Condition(self._lock)
        self._symbol_signal_seq: dict[str, int] = defaultdict(int)
        # Bounded quality window and very short canonical-source cache.  The hot WebSocket
        # path must never rescan tens of thousands of historical ticks per market event.
        self._quality_window: dict[tuple[str, str], deque[tuple[int, bool, bool, str]]] = defaultdict(lambda: deque(maxlen=8_000))
        self._choice_cache: dict[str, tuple[float, str | None, list[dict[str, Any]]]] = {}

    @staticmethod
    def _src(source: str) -> str:
        return str(source or "UNKNOWN").upper().strip()

    @staticmethod
    def _sym(symbol: str) -> str:
        return str(symbol or "").upper().strip()

    def _lane(self, sym: str, src: str) -> deque[dict[str, Any]]:
        lane = self._ticks[sym].get(src)
        if lane is None:
            lane = deque(maxlen=self._capacity)
            self._ticks[sym][src] = lane
        return lane

    def _remember(self, sym: str, src: str, key: str) -> bool:
        k = (sym, src)
        ss = self._seen_sets[k]
        if key in ss:
            return False
        q = self._seen[k]
        if len(q) == q.maxlen and q:
            ss.discard(q[0])
        q.append(key); ss.add(key)
        return True

    def ingest(self, *, source: str, symbol: str, timestamp: Any, price: Any,
               size: Any = 0.0, signed_volume: Any = 0.0, event_type: str = "TRADE",
               bid: Any = None, ask: Any = None, exchange: str = "",
               event_id: str | None = None, metadata: dict[str, Any] | None = None) -> bool:
        src, sym = self._src(source), self._sym(symbol)
        px = _f(price); sz = _f(size, 0.0) or 0.0; sv = _f(signed_volume, 0.0) or 0.0
        et = str(event_type or "TRADE").upper()
        if not sym or px is None or px <= 0:
            return False
        ts = _event_ts(timestamp)
        if ts is None:
            with self._lock:
                st=self._stats[sym].setdefault(src,{})
                st["invalid_event_time"] = int(st.get("invalid_event_time") or 0) + 1
            return False
        key = str(event_id or f"{et}|{ts.value}|{px:.10g}|{sz:.10g}|{exchange}")
        with self._lock:
            if not self._remember(sym, src, key):
                st = self._stats[sym].setdefault(src, {})
                st["duplicates_suppressed"] = int(st.get("duplicates_suppressed") or 0) + 1
                return False
            self._seq += 1
            meta = dict(metadata or {})
            event_time_valid = meta.get("event_time_valid") if "event_time_valid" in meta else True
            event_time_source = str(meta.get("event_time_source") or ("OBSERVED_EVENT_TIME" if event_time_valid is True else "RECEIVE_PROXY"))
            row = {
                "seq": self._seq, "timestamp": _local_naive(ts), "price": float(px),
                "size": float(sz), "signed_volume": float(sv), "source": src,
                "event_type": et, "bid": _f(bid), "ask": _f(ask),
                "exchange": str(exchange or ""), **meta,
                "event_time_valid": bool(event_time_valid), "event_time_source": event_time_source,
            }
            self._lane(sym, src).append(row)
            operational = not bool(meta.get("presentation_only"))
            if operational:
                self._quality_window[(sym, src)].append((int(ts.value), et == "TRADE", bool(event_time_valid), event_time_source))
                # If a different provider just produced a fresh event, force the next
                # canonical lookup to arbitrate immediately.  Healthy selected lanes keep
                # the 100 ms cache; challengers never have to wait for cache expiry.
                if self._selected.get(sym) != src:
                    self._choice_cache.pop(sym, None)
            st = self._stats[sym].setdefault(src, {})
            st["last_event_at"] = ts.isoformat()
            if operational:
                st["last_operational_event_ns"] = int(ts.value)
            st["last_trade_at"] = ts.isoformat() if et == "TRADE" and operational else st.get("last_trade_at")
            st["events_total"] = int(st.get("events_total") or 0) + 1
            st["trades_total"] = int(st.get("trades_total") or 0) + (1 if et == "TRADE" else 0)
            self._symbol_signal_seq[sym] = self._seq
            self._condition.notify_all()
        return True

    def signal_seq(self, symbol: str) -> int:
        """Monotonic per-symbol wakeup sequence for the presentation transport."""
        sym = self._sym(symbol)
        with self._lock:
            return int(self._symbol_signal_seq.get(sym, 0))

    def wait_for_update(self, symbol: str, after_signal: int = 0, timeout: float = 0.25) -> int:
        """Block until *symbol* receives a new observed event or timeout expires.

        This is transport signalling only.  It never changes canonical-provider selection,
        Scanner state, calculations, or market data.  FastAPI calls it through
        ``asyncio.to_thread`` so the event loop remains non-blocking.
        """
        sym = self._sym(symbol)
        after = int(after_signal or 0)
        deadline = time.monotonic() + max(0.001, float(timeout or 0.25))
        with self._condition:
            while int(self._symbol_signal_seq.get(sym, 0)) <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            return int(self._symbol_signal_seq.get(sym, 0))

    def _metrics(self, sym: str, src: str, now: pd.Timestamp) -> dict[str, Any]:
        """Quality metrics from a bounded rolling window, never a full tape scan."""
        now_ns = int(now.value)
        cut_ns = now_ns - 10_000_000_000
        with self._lock:
            lane = self._ticks.get(sym, {}).get(src)
            st = dict(self._stats.get(sym, {}).get(src, {}))
            qrows = list(self._quality_window.get((sym, src), ()))
            clock_rows = [r for r in islice(reversed(lane), 0, 300) if not bool(r.get("presentation_only"))][:250] if lane else []
        if not lane or not qrows:
            return {"source":src,"observed":False,"quality_score":0.0,"last_event_age_seconds":None,
                    "events_10s":0,"trades_10s":0,"events_total":0,"trades_total":0}
        # Presentation-only REST context is not inserted into the quality window for a
        # winning operational lane.  Filter defensively in case a provider does so.
        recent=[r for r in qrows if r[0] >= cut_ns]
        events_10s=len(recent);trades=sum(1 for r in recent if r[1])
        last_ns=int(st.get("last_operational_event_ns") or (qrows[-1][0] if qrows else 0))
        age=max(0.0,(now_ns-last_ns)/1_000_000_000.0) if last_ns else 999.0
        freshness=max(0.0,100.0-min(100.0,age*20.0))
        activity=min(100.0,events_10s*5.0)
        trade_ratio=(trades/max(1,events_10s)) if events_10s else 0.0
        trade_quality=100.0*trade_ratio
        valid_clock=sum(1 for r in clock_rows if r.get("event_time_valid") is True)
        proxy_clock=sum(1 for r in clock_rows if r.get("event_time_source")=="RECEIVE_PROXY")
        clock_factor=(valid_clock+0.65*proxy_clock)/max(1,len(clock_rows))
        quality=(0.58*freshness+0.22*activity+0.20*trade_quality)*clock_factor
        return {**st,"source":src,"observed":True,"quality_score":round(quality,2),
                "event_time_quality_factor":round(clock_factor,3),"valid_event_time_events":valid_clock,"receive_proxy_events":proxy_clock,
                "freshness_score":round(freshness,2),"activity_score":round(activity,2),
                "trade_ratio_pct":round(100*trade_ratio,2),"last_event_age_seconds":round(age,3),
                "events_10s":events_10s,"trades_10s":trades,
                "events_total":int(st.get("events_total") or 0),"trades_total":int(st.get("trades_total") or 0)}

    def _choose(self, sym: str, now: pd.Timestamp | None = None) -> tuple[str | None, list[dict[str, Any]]]:
        now = now if now is not None else pd.Timestamp.now(tz="UTC")
        with self._lock:
            sources=sorted(set(self._ticks.get(sym, {}))|set(self._stats.get(sym, {})))
            current=self._selected.get(sym)
        metrics=[self._metrics(sym,s,now) for s in sources]
        ranked=sorted(metrics,key=lambda r:(float(r.get("quality_score") or 0),int(r.get("trades_10s") or 0),int(r.get("events_10s") or 0)),reverse=True)
        challenger=ranked[0]["source"] if ranked and float(ranked[0].get("quality_score") or 0)>0 else None
        if current and challenger and current != challenger:
            cm=next((r for r in metrics if r["source"]==current),None)
            bm=ranked[0]
            current_age=float((cm or {}).get("last_event_age_seconds") or 999.0)
            current_q=float((cm or {}).get("quality_score") or 0.0)
            # Stay on a healthy lane unless the challenger is materially better. This
            # prevents chart jitter while still failing over quickly on stale/dead feeds.
            if current_age <= 2.5 and current_q >= float(bm.get("quality_score") or 0)-12.0:
                challenger=current
        with self._lock:
            if challenger:self._selected[sym]=challenger
            elif sym in self._selected:self._selected.pop(sym,None)
            self._choice_cache[sym]=(time.monotonic(),challenger,[dict(r) for r in ranked])
        return challenger, ranked

    def _choose_cached(self, sym: str, max_age: float = 0.10) -> tuple[str | None, list[dict[str, Any]]]:
        """Fast canonical lookup for the display hot path.

        The arbitration algorithm is unchanged; only its recomputation frequency is bounded.
        A 100 ms cache is far below human/chart refresh cadence while avoiding O(history)
        work on every market event.
        """
        now_mono=time.monotonic()
        with self._lock:
            cached=self._choice_cache.get(sym)
        if cached and now_mono-float(cached[0])<=max(0.0,float(max_age)):
            return cached[1],[dict(r) for r in cached[2]]
        return self._choose(sym)

    def canonical_source(self, symbol: str) -> str | None:
        return self._choose(self._sym(symbol))[0]

    def dataframe(self, symbol: str) -> pd.DataFrame:
        sym=self._sym(symbol); src,_=self._choose(sym)
        if not src:
            return pd.DataFrame(columns=["seq","timestamp","price","size","signed_volume","source","event_type"])
        with self._lock: rows=[dict(x) for x in self._ticks.get(sym,{}).get(src,()) if not bool(x.get("presentation_only"))]
        if not rows:
            return pd.DataFrame(columns=["seq","timestamp","price","size","signed_volume","source","event_type"])
        df=pd.DataFrame(rows);df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce")
        return df.dropna(subset=["timestamp","price"]).sort_values("seq").reset_index(drop=True)

    def since(self, symbol: str, after: int = 0, limit: int = 1800) -> dict[str, Any]:
        sym=self._sym(symbol);src,ranked=self._choose_cached(sym)
        after_i=int(after);lim=max(1,int(limit));rev=[]
        with self._lock:
            lane=self._ticks.get(sym,{}).get(src,()) if src else ()
            # Sequence numbers are monotonic across the fabric. Walk backward from the
            # newest row and stop as soon as the cursor is reached: O(new ticks), not
            # O(session history).
            for x in reversed(lane):
                seq=int(x.get("seq") or 0)
                if seq<=after_i:break
                if bool(x.get("presentation_only")):continue
                rev.append(dict(x))
                if len(rev)>=lim:break
            global_seq=self._seq
        rows=list(reversed(rev))
        last=int(rows[-1].get("seq") or after_i) if rows else after_i
        return {"symbol":sym,"connected":bool(src),"source":src,"last_seq":last,"fabric_seq":global_seq,
                "ticks":rows,"providers":[r.get("source") for r in ranked if r.get("observed")],
                "source_quality":ranked}

    def health(self, symbol: str) -> dict[str, Any]:
        sym=self._sym(symbol);src,ranked=self._choose(sym)
        live=[r["source"] for r in ranked if r.get("last_event_age_seconds") is not None and float(r["last_event_age_seconds"])<=5.0]
        clock=activity_expectation(sym)
        expected_live=bool(clock.get("expected_price_live"))
        if src is None and expected_live:state,bottleneck,severity="NO_DATA","NO_OBSERVED_PRICE_PROVIDER","ERROR"
        elif src is None:state,bottleneck,severity="NO_DATA",None,"INFO"
        elif not live and expected_live:state,bottleneck,severity="STALE","ALL_OBSERVED_PRICE_FEEDS_STALE","ERROR"
        elif not live:state,bottleneck,severity=clock.get("display_state","EXPECTED_IDLE"),None,"INFO"
        else:state,bottleneck,severity="LIVE","CLEAR" if len(live)>1 else "SINGLE_LIVE_PRICE_PATH","OK"
        return {"symbol":sym,"state":state,"canonical_source":src,"providers_live":live,"redundancy":len(live),
                "bottleneck":bottleneck,"severity":severity,"sources":ranked,"session_expectation":clock,
                "policy":"DYNAMIC_QUALITY_FAILOVER_NO_FIXED_PROVIDER_RANK · SESSION_AWARE_HEALTH"}

    def scheduler_status(self, symbol: str) -> dict[str, Any]:
        sym=self._sym(symbol);src,_=self._choose(sym)
        with self._lock:
            lane=[dict(x) for x in self._ticks.get(sym,{}).get(src,()) if not bool(x.get("presentation_only"))] if src else []
        last=dict(lane[-1]) if lane else None
        return {"symbol":sym,"connected":bool(src),"source":src,"last_seq":int(last.get("seq") or 0) if last else 0,"last_tick":last}


PRICE_TICK_FABRIC = UnifiedPriceTickFabric()
