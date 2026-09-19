"""Provider-neutral, quality-aware composite market state.

Every configured provider may contribute observations. No provider receives a fixed
primary/secondary rank. Observations are first scored for freshness, latency, sequence
integrity and cross-source divergence. Only then are comparable prices fused.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any
import math
import statistics
import time

from .source_arbitration import score_source, arbitrate
from .event_time import parse_utc, resolve_event_time
from .provider_data_lake import DATA_LAKE
from .temporal_causality import CAUSAL_RUNTIME
from .obs import note as _obs_note


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _ts(v: Any, fallback: Any = None) -> datetime | None:
    """Strict parser used by diagnostics; invalid values never become ``now``."""
    return parse_utc(v) or parse_utc(fallback)



class UnifiedProviderBus:
    def __init__(self) -> None:
        self._lock = RLock()
        self._latest: dict[str, dict[str, dict[str, Any]]] = {}
        # symbol -> (selected source, wall-clock selection time). This is runtime
        # hysteresis state, not a fixed provider hierarchy.
        self._selected: dict[str, tuple[str | None, float]] = {}

    def ingest(self, *, source: str, symbol: str, event_type: str, values: dict[str, Any], timestamp: Any = None, received_at: Any = None, sequence_ok: bool = True, source_seq: int | None = None, event_id: str | None = None) -> None:
        sym = str(symbol or "").upper(); src = str(source or "UNKNOWN").upper(); now = datetime.now(timezone.utc)
        vals=dict(values or {})
        explicit_valid=vals.get("event_time_valid") if "event_time_valid" in vals else None
        explicit_source=vals.get("event_time_source")
        clk=resolve_event_time(timestamp, received_at=received_at, explicit_valid=explicit_valid, explicit_source=explicit_source, now=now)
        evt=clk.event_time or clk.received_at; rec=clk.received_at
        latency=max(0.0,(rec-evt).total_seconds()*1000.0) if clk.event_time_valid else None
        row = {"source":src,"symbol":sym,"event_type":str(event_type).upper(),"values":vals,"timestamp":evt.isoformat(),"received_at":rec.isoformat(),
               "event_time_valid":bool(clk.event_time_valid),"event_time_source":clk.event_time_source,"raw_event_time":None if timestamp is None else str(timestamp),
               "latency_ms":latency,"sequence_ok":bool(sequence_ok),"source_seq":source_seq,"event_id":event_id,"updated_at":now.isoformat()}
        # Keep one slot per provider + semantic event type. A fast GREEKS/TRADE event
        # must never overwrite that provider's latest QUOTE and make it disappear from
        # composite pricing. This is especially important for DXLink multiplexed streams.
        slot = f"{src}:{str(event_type).upper()}"
        with self._lock:
            self._latest.setdefault(sym, {})[slot] = row
        try:
            if clk.event_time_valid:
                CAUSAL_RUNTIME.ingest(source=src, symbol=sym, event_type=str(event_type).upper(), values=vals,
                                      event_time=evt, receive_time=rec, source_seq=source_seq, event_id=event_id)
        except Exception as _e:
            _obs_note('provider_bus:57', _e)
        try:
            DATA_LAKE.archive_normalized(
                source=src, symbol=sym, event_type=str(event_type).upper(), values=vals,
                event_time=evt, receive_time=rec, latency_ms=row["latency_ms"],
                metadata={"sequence_ok": bool(sequence_ok), "source_seq": source_seq, "event_id": event_id, "bus": "MARKET",
                          "event_time_valid":bool(clk.event_time_valid),"event_time_source":clk.event_time_source},
            )
        except Exception as _e:
            _obs_note('provider_bus:65', _e)

    def latest_events(self, symbol: str) -> list[dict[str, Any]]:
        """Return the latest semantic event slots for diagnostics/ecosystem fusion.

        This is an in-memory read only; no network or disk I/O occurs.
        """
        sym = str(symbol or "").upper()
        with self._lock:
            return [dict(x) for x in self._latest.get(sym, {}).values()]

    def snapshot(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol).upper(); now = datetime.now(timezone.utc)
        with self._lock: all_rows = [dict(x) for x in self._latest.get(sym, {}).values()]
        # Pricing fusion uses one best comparable price observation per provider.
        # QUOTE is preferred, then TRADE/SNAPSHOT. Non-price events remain stored but
        # cannot accidentally contaminate BBO/consensus.
        by_source: dict[str, list[dict[str, Any]]] = {}
        for row in all_rows:
            by_source.setdefault(str(row.get("source") or "UNKNOWN"), []).append(row)
        rows: list[dict[str, Any]] = []
        priority = {"QUOTE": 0, "TRADE": 1, "SNAPSHOT": 2, "BAR": 3, "CANDLE": 3}
        for src, candidates in by_source.items():
            usable_candidates = []
            for row in candidates:
                vals = row.get("values") or {}
                has_quote = _f(vals.get("bid")) is not None and _f(vals.get("ask")) is not None
                has_price = _f(vals.get("price")) is not None
                if has_quote or has_price:
                    usable_candidates.append(row)
            if not usable_candidates:
                continue
            usable_candidates.sort(key=lambda r: (priority.get(str(r.get("event_type") or "").upper(), 9), -(_ts(r.get("timestamp"), r.get("received_at")) or now).timestamp()))
            rows.append(usable_candidates[0])
        prices = []
        for r in rows:
            vals = r.get("values") or {}; ts = _ts(r.get("timestamp"), r.get("received_at")) or now; age_ms = max(0.0,(now-ts).total_seconds()*1000.0)
            px = _f(vals.get("price")); bid = _f(vals.get("bid")); ask = _f(vals.get("ask")); mid = (bid+ask)/2.0 if bid is not None and ask is not None and ask>=bid else px
            if mid is not None: prices.append(mid)
        med = statistics.median(prices) if prices else None
        scored=[]
        for r in rows:
            vals=r.get("values") or {}; ts=_ts(r.get("timestamp"),r.get("received_at")) or now; age_ms=max(0.0,(now-ts).total_seconds()*1000.0);bid=_f(vals.get("bid"));ask=_f(vals.get("ask"));px=_f(vals.get("price"));mid=(bid+ask)/2.0 if bid is not None and ask is not None and ask>=bid else px
            div=0.0 if med in {None,0} or mid is None else abs(mid-med)/abs(med)
            q=score_source({"source":r.get("source"),"channel":r.get("event_type"),"instrument_type":vals.get("instrument_type"),
                            "status":"LIVE" if age_ms<5000 else "STALE","latency_ms":r.get("latency_ms"),"age_ms":age_ms,"gap_rate":0.0,
                            "sequence_ok":r.get("sequence_ok"),"cross_source_divergence":div,
                            "event_time_valid":r.get("event_time_valid"),"event_time_source":r.get("event_time_source")})
            scored.append({**r,"quality":q,"mid":mid,"age_ms":age_ms,"divergence":div})
        prelim=[r for r in scored if r["quality"]["quality_score"]>=50 and r["age_ms"]<=5000 and r["mid"] is not None]
        # v1.26.0 TIME-SYNC: price consensus may only combine observations inside the
        # same bounded event-time cohort. recv-order alone is never treated as simultaneity.
        sync_window_ms=350.0
        frontier=max(((_ts(r.get("timestamp"),r.get("received_at")) or now) for r in prelim),default=None)
        usable=[]
        for r in prelim:
            rt=_ts(r.get("timestamp"),r.get("received_at")) or now
            delta_ms=abs((frontier-rt).total_seconds()*1000.0) if frontier else 0.0
            r["sync_delta_ms"]=delta_ms;r["synchronized"]=bool(delta_ms<=sync_window_ms)
            if r["synchronized"]:usable.append(r)
        # Runtime source selection uses the same channel-aware arbitration + hysteresis
        # that is exposed in diagnostics. Composite fusion can still use every synchronized
        # usable observation, but the canonical source no longer flaps on near ties.
        with self._lock:
            prev_src, prev_at = self._selected.get(sym, (None, 0.0))
        arb_rows=[]
        for r in scored:
            vals=r.get("values") or {}
            arb_rows.append({
                "name":r.get("source"),"source":r.get("source"),"channel":r.get("event_type"),"instrument_type":vals.get("instrument_type"),
                "status":"LIVE" if float(r.get("age_ms") or 0.0)<5000 else "STALE","latency_ms":r.get("latency_ms"),"age_ms":r.get("age_ms"),
                "gap_rate":0.0,"sequence_ok":r.get("sequence_ok"),"cross_source_divergence":r.get("divergence"),
                "event_time_valid":r.get("event_time_valid"),"event_time_source":r.get("event_time_source"),
            })
        arb=arbitrate(arb_rows,previous=prev_src,previous_selected_at=prev_at if prev_src else None) if arb_rows else {"ready":False,"selected":None,"ranked":[]}
        selected_src=arb.get("selected")
        if selected_src != prev_src:
            with self._lock:
                self._selected[sym]=(selected_src,time.time())
        elif prev_src:
            with self._lock:
                self._selected[sym]=(prev_src,prev_at)

        # Equal opportunity, quality-dependent influence: there are no fixed provider weights.
        weight_sum=sum(max(1.0,float(r["quality"]["quality_score"])) for r in usable)
        consensus=None
        if usable and weight_sum>0:
            consensus=sum(float(r["mid"])*max(1.0,float(r["quality"]["quality_score"])) for r in usable)/weight_sum
        bids=[_f((r.get("values") or {}).get("bid")) for r in usable]; asks=[_f((r.get("values") or {}).get("ask")) for r in usable]
        bids=[x for x in bids if x is not None];asks=[x for x in asks if x is not None]
        cbid=max(bids) if bids else None; cask=min(asks) if asks else None
        if cbid is not None and cask is not None and cask<cbid:
            # Crossed provider quotes often indicate timestamp skew; do not publish a false NBBO.
            cbid=cask=None
        confidence = 0.0 if not usable else min(100.0, sum(float(r["quality"]["quality_score"]) for r in usable)/len(usable))
        return {"ready":bool(usable),"symbol":sym,"providers":scored,"usable_providers":[r.get("source") for r in usable],
                "selected_provider":selected_src,"source_arbitration":arb,
                "consensus_price":consensus,"composite_bid":cbid,"composite_ask":cask,"confidence":round(confidence,2),
                "event_time_frontier":frontier.isoformat() if frontier else None,"sync_window_ms":sync_window_ms,"synchronized_providers":[r.get("source") for r in usable],
                "authority":"MARKET_DATA_FUSION_ONLY","weighting":"QUALITY_DYNAMIC_NOT_FIXED_PROVIDER_RANK","time_sync":"BOUNDED_EVENT_TIME_COHORT",
                "note":"Comparable, synchronized observations only; futures/index exposures require instrument normalization before feature fusion."}


class UnifiedFeatureBus:
    """Provider-neutral fusion for non-price quantitative observations.

    This is the companion to :class:`UnifiedProviderBus`.  It is intentionally
    feature-centric rather than provider-centric: Gamma-like, Delta-like and Flow-like
    observations are fused within their own semantic channel.  A provider therefore
    never receives a fixed global weight or rank merely because of its name.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._latest: dict[str, dict[str, dict[str, Any]]] = {}
        # symbol -> (selected source, wall-clock selection time). This is runtime
        # hysteresis state, not a fixed provider hierarchy.
        self._selected: dict[str, tuple[str | None, float]] = {}

    def ingest(self, *, source: str, symbol: str, feature_group: str,
               values: dict[str, Any], timestamp: Any = None, received_at: Any = None,
               confidence: float | None = None, ttl_ms: float = 180000.0,
               sequence_ok: bool = True) -> None:
        sym = str(symbol or "").upper()
        src = str(source or "UNKNOWN").upper()
        grp = str(feature_group or "GENERAL").upper()
        now = datetime.now(timezone.utc)
        vals=dict(values or {})
        clk=resolve_event_time(timestamp, received_at=received_at, explicit_valid=vals.get("event_time_valid") if "event_time_valid" in vals else None, explicit_source=vals.get("event_time_source"), now=now)
        evt=clk.event_time or clk.received_at; rec=clk.received_at
        row = {
            "source": src, "symbol": sym, "feature_group": grp,
            "values": vals, "timestamp": evt.isoformat(),
            "received_at": rec.isoformat(),
            "event_time_valid":bool(clk.event_time_valid),"event_time_source":clk.event_time_source,
            "latency_ms": max(0.0, (rec - evt).total_seconds() * 1000.0) if clk.event_time_valid else None,
            "confidence": max(0.0, min(100.0, float(confidence if confidence is not None else 100.0))),
            "ttl_ms": max(1000.0, float(ttl_ms)), "sequence_ok": bool(sequence_ok),
            "updated_at": now.isoformat(),
        }
        with self._lock:
            self._latest.setdefault(sym, {})[f"{src}:{grp}"] = row
        try:
            DATA_LAKE.archive_normalized(
                source=src, symbol=sym, event_type="FEATURE", values=vals,
                event_time=evt, receive_time=rec, latency_ms=row["latency_ms"],
                metadata={"feature_group": grp, "confidence": row["confidence"], "ttl_ms": row["ttl_ms"], "bus": "FEATURE",
                          "event_time_valid":bool(clk.event_time_valid),"event_time_source":clk.event_time_source},
            )
        except Exception as _e:
            _obs_note('provider_bus:180', _e)

    def latest_events(self, symbol: str) -> list[dict[str, Any]]:
        """Latest feature slots for per-channel health/time-sync diagnostics."""
        sym = str(symbol or "").upper()
        with self._lock:
            return [dict(x) for x in self._latest.get(sym, {}).values()]

    @staticmethod
    def _channel_obs(row: dict[str, Any], channel: str) -> tuple[int, float] | None:
        vals = row.get("values") or {}
        directional = vals.get("directional") or {}
        ch = directional.get(channel) if isinstance(directional, dict) else None
        if not isinstance(ch, dict):
            return None
        try:
            sign = int(ch.get("sign") or 0)
        except Exception:
            sign = 0
        sign = 1 if sign > 0 else -1 if sign < 0 else 0
        if sign == 0:
            return None
        try:
            conf = float(ch.get("confidence") if ch.get("confidence") is not None else row.get("confidence", 0.0))
        except Exception:
            conf = float(row.get("confidence", 0.0) or 0.0)
        return sign, max(0.0, min(100.0, conf))

    def snapshot(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "").upper()
        now = datetime.now(timezone.utc)
        with self._lock:
            rows = [dict(x) for x in self._latest.get(sym, {}).values()]
        scored: list[dict[str, Any]] = []
        for r in rows:
            ts = _ts(r.get("timestamp"),r.get("received_at")) or now
            age_ms = max(0.0, (now - ts).total_seconds() * 1000.0)
            ttl = float(r.get("ttl_ms") or 180000.0)
            q = score_source({
                "source": r.get("source"),
                "status": "LIVE" if age_ms <= ttl else "STALE",
                "latency_ms": r.get("latency_ms"),
                "age_ms": age_ms,
                "gap_rate": 0.0,
                "sequence_ok": r.get("sequence_ok"),
                "cross_source_divergence": None,
                "event_time_valid":r.get("event_time_valid"),"event_time_source":r.get("event_time_source"),
            })
            scored.append({**r, "quality": q, "age_ms": age_ms, "stale": age_ms > ttl})
        usable = [r for r in scored if not r["stale"] and float((r.get("quality") or {}).get("quality_score", 0)) >= 50]

        channels: dict[str, Any] = {}
        for channel in ("gamma", "delta", "flow", "ecosystem", "derivatives", "vanna", "charm", "convexity", "expiry", "structural"):
            contributions = []
            signed_weight = 0.0
            total_weight = 0.0
            for r in usable:
                obs = self._channel_obs(r, channel)
                if obs is None:
                    continue
                sign, conf = obs
                quality = float((r.get("quality") or {}).get("quality_score", 0.0))
                w = max(0.0, conf) * max(0.0, quality) / 100.0
                if w <= 0:
                    continue
                signed_weight += sign * w
                total_weight += w
                contributions.append({
                    "source": r.get("source"), "feature_group": r.get("feature_group"),
                    "sign": sign, "confidence": round(conf, 2),
                    "quality": round(quality, 2), "weight": round(w, 4),
                    "age_ms": round(float(r.get("age_ms", 0.0)), 2),
                })
            if total_weight > 0:
                ratio = signed_weight / total_weight
                sign = 1 if ratio > 0.08 else -1 if ratio < -0.08 else 0
                conf = min(100.0, abs(ratio) * 100.0)
            else:
                sign = 0
                conf = 0.0
            channels[channel] = {
                "sign": sign,
                "direction": "BUY" if sign > 0 else "SELL" if sign < 0 else "NEUTRAL",
                "confidence": round(conf, 2),
                "contributors": contributions,
            }
        return {
            "ready": bool(usable), "symbol": sym, "providers": scored,
            "usable_providers": sorted({str(r.get("source")) for r in usable}),
            "channels": channels,
            "authority": "FEATURE_FUSION_INPUT",
            "weighting": "QUALITY_DYNAMIC_BY_OBSERVATION_NOT_PROVIDER_RANK",
            "note": "Providers are equal participants; only semantically comparable features are fused inside the same channel. Ecosystem and derivatives are normalized semantic channels, never raw-price/Gamma/OI addition across incompatible instruments.",
        }


PROVIDER_BUS = UnifiedProviderBus()
FEATURE_BUS = UnifiedFeatureBus()
