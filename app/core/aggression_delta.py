"""ITM Aggression Trigger.

Clocked 1m/3m/5m/15m delta-aggression candles built from observed trades.
The engine prefers provider-classified signed volume, then synchronized bid/ask,
then a disclosed tick-rule fallback.  It never invents an aggressor.

The visible candle color is deliberately simple:
  BUY -> cyan/blue, SELL -> magenta/red, NEUTRAL -> gray.
Internally the LIVE forming candle applies adaptive persistence/activity filters so a
single print cannot flip the trigger.  Scanner remains the only directional authority;
this module is an entry-timing confirmation layer.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
import math
import os
from statistics import median

import pandas as pd

from .provider_flow_fabric import PRICE_TICK_FABRIC


TIMEFRAMES = (1, 3, 5, 15)


def _market_now_naive() -> pd.Timestamp:
    """New York wall clock, timezone stripped to match canonical fabric timestamps."""
    return pd.Timestamp.now(tz="America/New_York").tz_localize(None)


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _ts(v: Any) -> pd.Timestamp:
    t = pd.Timestamp(v)
    if pd.isna(t):
        return _market_now_naive()
    # Fabric timestamps are intentionally local-naive.  Keep the original market clock
    # for bucket boundaries; do not reinterpret them as UTC.
    if t.tzinfo is not None:
        t = t.tz_convert("America/New_York").tz_localize(None)
    return t


def _bucket_start(ts: pd.Timestamp, minutes: int) -> pd.Timestamp:
    seconds = max(60, int(minutes) * 60)
    # Pandas floor accepts fixed-minute aliases and preserves the session clock.
    return ts.floor(f"{seconds}s")


@dataclass
class _FrameState:
    minutes: int
    current: dict[str, Any] | None = None
    closed: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=600))
    prev_ha_open: float | None = None
    prev_ha_close: float | None = None
    candidate_direction: str = "NEUTRAL"
    candidate_count: int = 0
    live_direction: str = "NEUTRAL"
    live_score: float = 0.0
    revision: int = 0


class AggressionDeltaEngine:
    def __init__(self) -> None:
        self._lock = RLock()
        self._frames: dict[str, dict[int, _FrameState]] = {}
        self._cursor: dict[str, int] = {}
        self._last_trade_px: dict[str, float] = {}
        self._last_trade_sign: dict[str, int] = {}
        self._revision = 0

    def _ensure_symbol(self, symbol: str) -> dict[int, _FrameState]:
        sym = str(symbol or "").upper()
        return self._frames.setdefault(sym, {m: _FrameState(m) for m in TIMEFRAMES})

    def reset_symbol(self, symbol: str) -> None:
        sym = str(symbol or "").upper()
        with self._lock:
            self._frames.pop(sym, None)
            self._cursor.pop(sym, None)
            self._last_trade_px.pop(sym, None)
            self._last_trade_sign.pop(sym, None)
            self._revision += 1

    def _classify(self, sym: str, row: dict[str, Any]) -> tuple[int, float, str]:
        size = max(0.0, _f(row.get("size"), 0.0) or 0.0)
        if size <= 0:
            return 0, 0.0, "NO_SIZE"
        sv = _f(row.get("signed_volume"), 0.0) or 0.0
        if abs(sv) > 0:
            return (1 if sv > 0 else -1), 1.0, "PROVIDER_SIGNED_VOLUME"
        explicit = int(_f(row.get("aggressor_sign"), 0.0) or 0)
        if explicit in (-1, 1):
            return explicit, 0.98, "PROVIDER_AGGRESSOR"
        px = _f(row.get("price")); bid = _f(row.get("bid")); ask = _f(row.get("ask"))
        if px is None:
            return 0, 0.0, "NO_PRICE"
        eps = max(abs(px) * 1e-8, 1e-8)
        if ask is not None and ask > 0 and px >= ask - eps:
            return 1, 0.93, "AT_ASK"
        if bid is not None and bid > 0 and px <= bid + eps:
            return -1, 0.93, "AT_BID"
        # Tick-rule is a lower-confidence fallback only; equal prints preserve the most
        # recent non-zero sign but remain explicitly lower confidence.
        prev = self._last_trade_px.get(sym)
        sign = 0
        if prev is not None:
            if px > prev:
                sign = 1
            elif px < prev:
                sign = -1
            else:
                sign = int(self._last_trade_sign.get(sym) or 0)
        return sign, (0.58 if sign else 0.0), ("TICK_RULE" if sign else "UNCLASSIFIED")

    @staticmethod
    def _new_bar(bucket: pd.Timestamp, minutes: int, px: float) -> dict[str, Any]:
        return {
            "bucket": bucket, "end": bucket + pd.Timedelta(minutes=minutes),
            "price_open": px, "price_high": px, "price_low": px, "price_close": px,
            "delta": 0.0, "delta_high": 0.0, "delta_low": 0.0,
            "buy_volume": 0.0, "sell_volume": 0.0, "classified_volume": 0.0,
            "unclassified_volume": 0.0, "volume": 0.0, "size_sq": 0.0,
            "trades": 0, "classified_trades": 0, "confidence_weight": 0.0,
            "recent_signs": deque(maxlen=14), "delta_samples": deque(maxlen=36),
            "complete": False,
        }

    def _baseline_volume(self, fr: _FrameState) -> float | None:
        vals = [float(x.get("volume") or 0.0) for x in list(fr.closed)[-30:] if float(x.get("volume") or 0.0) > 0]
        if len(vals) < 3:
            return None
        return float(median(vals))

    def _score_live(self, fr: _FrameState, bar: dict[str, Any], now_ts: pd.Timestamp) -> tuple[str, float, dict[str, Any]]:
        volume = float(bar.get("volume") or 0.0)
        classified = float(bar.get("classified_volume") or 0.0)
        delta = float(bar.get("delta") or 0.0)
        trades = int(bar.get("trades") or 0)
        coverage = classified / volume if volume > 0 else 0.0
        imbalance = delta / classified if classified > 0 else 0.0
        denom = math.sqrt(max(float(bar.get("size_sq") or 0.0), 0.0))
        z = delta / denom if denom > 0 else 0.0
        signs = list(bar.get("recent_signs") or [])
        nonzero = [int(s) for s in signs if int(s) in (-1, 1)]
        target = 1 if delta > 0 else -1 if delta < 0 else 0
        persistence = (sum(1 for s in nonzero if s == target) / len(nonzero)) if nonzero and target else 0.0

        baseline = self._baseline_volume(fr)
        elapsed = max(0.25, min(float(fr.minutes * 60), (now_ts - pd.Timestamp(bar["bucket"])).total_seconds() + 0.25))
        progress = max(0.08, min(1.0, elapsed / float(fr.minutes * 60)))
        expected_so_far = (baseline * progress) if baseline else None
        activity_ratio = (volume / expected_so_far) if expected_so_far and expected_so_far > 0 else 1.0

        samples = list(bar.get("delta_samples") or [])
        slope = 0.0
        if len(samples) >= 2:
            t1, d1 = samples[-1]
            # Use a roughly 2-6 second causal lookback when possible.
            prior = samples[0]
            for s in reversed(samples[:-1]):
                if (t1 - s[0]).total_seconds() >= 2.0:
                    prior = s
                    break
            dt = max(0.1, (t1 - prior[0]).total_seconds())
            slope = (float(d1) - float(prior[1])) / dt
        accel_aligned = bool(target and (slope * target) > 0)

        score = (
            min(abs(z) / 2.5, 1.0) * 34.0 +
            min(abs(imbalance) / 0.35, 1.0) * 24.0 +
            min(max(activity_ratio, 0.0) / 1.5, 1.0) * 14.0 +
            min(max(coverage, 0.0), 1.0) * 16.0 +
            min(max(persistence, 0.0), 1.0) * 8.0 +
            (4.0 if accel_aligned else 0.0)
        )
        min_score = float(os.getenv("ITM_AGG_LIVE_SCORE", "66"))
        min_z = float(os.getenv("ITM_AGG_LIVE_Z", "0.85"))
        min_imb = float(os.getenv("ITM_AGG_LIVE_IMBALANCE", "0.10"))
        min_cov = float(os.getenv("ITM_AGG_MIN_COVERAGE", "0.55"))
        min_trades = int(os.getenv("ITM_AGG_MIN_TRADES", "4"))
        qualifies = bool(target and trades >= min_trades and coverage >= min_cov and abs(z) >= min_z and abs(imbalance) >= min_imb and score >= min_score)
        candidate = "BUY" if target > 0 else "SELL" if target < 0 else "NEUTRAL"
        if not qualifies:
            candidate = "NEUTRAL"

        if candidate == fr.candidate_direction and candidate != "NEUTRAL":
            fr.candidate_count += 1
        elif candidate != "NEUTRAL":
            fr.candidate_direction = candidate
            fr.candidate_count = 1
        else:
            fr.candidate_direction = "NEUTRAL"
            fr.candidate_count = 0

        required = 3 if fr.live_direction in {"NEUTRAL", candidate} else 4
        if candidate != "NEUTRAL" and fr.candidate_count >= required:
            fr.live_direction = candidate
        elif candidate == "NEUTRAL" and score < max(48.0, min_score - 15.0):
            fr.live_direction = "NEUTRAL"
        fr.live_score = round(float(score), 1)
        details = {
            "delta": round(delta, 3), "z": round(z, 3), "imbalance": round(imbalance, 4),
            "coverage": round(coverage, 3), "relative_activity": round(activity_ratio, 3),
            "persistence": round(persistence, 3), "slope": round(slope, 3),
            "candidate": candidate, "candidate_count": fr.candidate_count,
        }
        return fr.live_direction, fr.live_score, details

    def _decorate(self, fr: _FrameState, bar: dict[str, Any], *, forming: bool, now_ts: pd.Timestamp | None = None) -> dict[str, Any]:
        delta = float(bar.get("delta") or 0.0)
        classified = float(bar.get("classified_volume") or 0.0)
        volume = float(bar.get("volume") or 0.0)
        denom = math.sqrt(max(float(bar.get("size_sq") or 0.0), 0.0))
        z = delta / denom if denom > 0 else 0.0
        coverage = classified / volume if volume > 0 else 0.0
        imbalance = delta / classified if classified > 0 else 0.0
        raw_o = 0.0
        raw_h = float(bar.get("delta_high") or 0.0)
        raw_l = float(bar.get("delta_low") or 0.0)
        raw_c = delta
        ha_c = (raw_o + raw_h + raw_l + raw_c) / 4.0
        if fr.prev_ha_open is None or fr.prev_ha_close is None:
            ha_o = (raw_o + raw_c) / 2.0
        else:
            ha_o = (fr.prev_ha_open + fr.prev_ha_close) / 2.0
        ha_h = max(raw_h, ha_o, ha_c)
        ha_l = min(raw_l, ha_o, ha_c)

        if forming:
            direction, score, details = self._score_live(fr, bar, now_ts or _market_now_naive())
        else:
            # Closed bars show the actual winning side, but only if classification quality
            # is usable. This is the simple result the user asked to see.
            if coverage < float(os.getenv("ITM_AGG_MIN_COVERAGE", "0.55")) or abs(imbalance) < 0.02:
                direction = "NEUTRAL"
            else:
                direction = "BUY" if delta > 0 else "SELL" if delta < 0 else "NEUTRAL"
            score = min(100.0, abs(z) * 24.0 + abs(imbalance) * 80.0 + coverage * 28.0)
            details = {"delta": round(delta, 3), "z": round(z, 3), "imbalance": round(imbalance, 4), "coverage": round(coverage, 3)}

        out = {
            "time": pd.Timestamp(bar["bucket"]).isoformat(),
            "end": pd.Timestamp(bar["end"]).isoformat(),
            "open": round(ha_o, 4), "high": round(ha_h, 4), "low": round(ha_l, 4), "close": round(ha_c, 4),
            "raw_open": round(raw_o, 4), "raw_high": round(raw_h, 4), "raw_low": round(raw_l, 4), "raw_close": round(raw_c, 4),
            "direction": direction, "forming": bool(forming), "score": round(float(score), 1),
            "classified": coverage >= float(os.getenv("ITM_AGG_MIN_COVERAGE", "0.55")),
            "_details": details,
        }
        return out

    def _finalize(self, fr: _FrameState, bar: dict[str, Any]) -> dict[str, Any]:
        bar = dict(bar)
        bar["complete"] = True
        dec = self._decorate(fr, bar, forming=False)
        fr.prev_ha_open = float(dec["open"])
        fr.prev_ha_close = float(dec["close"])
        # Internal metrics remain available to Sophia/audit but UI intentionally receives
        # only the compact candle unless diagnostics are explicitly requested.
        stored = {**bar, "display": dec}
        stored.pop("recent_signs", None); stored.pop("delta_samples", None)
        fr.closed.append(stored)
        fr.revision += 1
        self._revision += 1
        return dec

    def ingest_row(self, symbol: str, row: dict[str, Any]) -> bool:
        sym = str(symbol or "").upper()
        if str(row.get("event_type") or "TRADE").upper() != "TRADE":
            return False
        px = _f(row.get("price")); size = max(0.0, _f(row.get("size"), 0.0) or 0.0)
        if px is None or px <= 0 or size <= 0:
            return False
        ts = _ts(row.get("timestamp"))
        sign, conf, _method = self._classify(sym, row)
        self._last_trade_px[sym] = float(px)
        if sign:
            self._last_trade_sign[sym] = int(sign)
        frames = self._ensure_symbol(sym)
        changed = False
        for minutes, fr in frames.items():
            bucket = _bucket_start(ts, minutes)
            if fr.current is None or pd.Timestamp(fr.current["bucket"]) != bucket:
                if fr.current is not None:
                    self._finalize(fr, fr.current)
                fr.current = self._new_bar(bucket, minutes, float(px))
                fr.candidate_direction = "NEUTRAL"; fr.candidate_count = 0; fr.live_direction = "NEUTRAL"; fr.live_score = 0.0
            b = fr.current
            b["price_high"] = max(float(b["price_high"]), float(px)); b["price_low"] = min(float(b["price_low"]), float(px)); b["price_close"] = float(px)
            b["trades"] += 1; b["volume"] += size
            if sign:
                b["classified_trades"] += 1; b["classified_volume"] += size; b["confidence_weight"] += conf * size
                if sign > 0: b["buy_volume"] += size
                else: b["sell_volume"] += size
                b["delta"] += float(sign) * size
                b["size_sq"] += size * size
                b["recent_signs"].append(int(sign))
            else:
                b["unclassified_volume"] += size
                b["recent_signs"].append(0)
            b["delta_high"] = max(float(b["delta_high"]), float(b["delta"])); b["delta_low"] = min(float(b["delta_low"]), float(b["delta"]));
            b["delta_samples"].append((ts, float(b["delta"])))
            self._score_live(fr, b, ts)
            fr.revision += 1
            changed = True
        if changed:
            self._revision += 1
        return changed

    def update_from_fabric(self, symbol: str, limit: int = 8000) -> int:
        sym = str(symbol or "").upper()
        with self._lock:
            after = int(self._cursor.get(sym, 0))
        packet = PRICE_TICK_FABRIC.since(sym, after=after, limit=limit)
        rows = list(packet.get("ticks") or [])
        if not rows:
            return 0
        count = 0
        with self._lock:
            for row in rows:
                if self.ingest_row(sym, dict(row)):
                    count += 1
            self._cursor[sym] = max(after, int(rows[-1].get("seq") or after))
        return count

    def snapshot(self, symbol: str, timeframe: str | int = "1m", *, include_diagnostics: bool = False, limit: int = 80) -> dict[str, Any]:
        sym = str(symbol or "").upper()
        try:
            m = int(str(timeframe).lower().replace("m", ""))
        except Exception:
            m = 1
        if m not in TIMEFRAMES:
            m = 1
        with self._lock:
            frames = self._ensure_symbol(sym)
            fr = frames[m]
            now_market = _market_now_naive()
            if fr.current is not None and now_market >= pd.Timestamp(fr.current["end"]):
                self._finalize(fr, fr.current)
                fr.current = None
                fr.candidate_direction = "NEUTRAL"; fr.candidate_count = 0; fr.live_direction = "NEUTRAL"; fr.live_score = 0.0
            candles = [dict(x.get("display") or {}) for x in list(fr.closed)[-max(1, int(limit)):]]
            live = None
            if fr.current is not None:
                live = self._decorate(fr, fr.current, forming=True, now_ts=now_market)
                candles.append(live)
            frame_summary = {}
            for mm, ff in frames.items():
                if ff.current is not None:
                    d = self._decorate(ff, ff.current, forming=True, now_ts=_market_now_naive())
                    frame_summary[f"{mm}m"] = {"direction": d.get("direction"), "forming": True, "score": d.get("score")}
                elif ff.closed:
                    d = dict(ff.closed[-1].get("display") or {})
                    frame_summary[f"{mm}m"] = {"direction": d.get("direction"), "forming": False, "score": d.get("score")}
                else:
                    frame_summary[f"{mm}m"] = {"direction": "NEUTRAL", "forming": False, "score": 0.0}
            payload = {
                "ready": bool(candles), "symbol": sym, "timeframe": f"{m}m", "timeframes": [f"{x}m" for x in TIMEFRAMES],
                "candles": candles, "live": live, "frames": frame_summary,
                "revision": int(self._revision), "role": "ENTRY_TIMING_CONFIRMATION_ONLY",
                "scanner_authority": True, "palette": {"BUY": "CYAN", "SELL": "MAGENTA_RED", "NEUTRAL": "GRAY"},
                "note": "Color LIVE exige persistencia/actividad/calidad; velas cerradas muestran el ganador observado de la agresión.",
            }
            if not include_diagnostics:
                for c in payload["candles"]:
                    c.pop("_details", None)
                if isinstance(payload.get("live"), dict):
                    payload["live"].pop("_details", None)
            return payload

    def revision(self) -> int:
        with self._lock:
            return int(self._revision)

    def build_all_from_dataframe(self, symbol: str, ticks: pd.DataFrame, limit: int = 500) -> dict[str, Any]:
        temp = AggressionDeltaEngine()
        if ticks is not None and not ticks.empty:
            x = ticks.copy()
            x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
            x = x.dropna(subset=["timestamp"]).sort_values("timestamp")
            for i, (_, r) in enumerate(x.iterrows(), start=1):
                row = r.to_dict(); row.setdefault("event_type", "TRADE"); row["seq"] = i
                temp.ingest_row(symbol, row)
        frames=temp._ensure_symbol(str(symbol).upper())
        for fr in frames.values():
            if fr.current is not None:
                temp._finalize(fr,fr.current)
                fr.current=None
        by={f"{m}m":temp.snapshot(symbol,f"{m}m",include_diagnostics=False,limit=limit) for m in TIMEFRAMES}
        return {"ready":any(v.get("ready") for v in by.values()),"symbol":str(symbol).upper(),"by_timeframe":by,
                "frames":(by.get("1m") or {}).get("frames",{}),"role":"ENTRY_TIMING_CONFIRMATION_ONLY",
                "scanner_authority":True,"precomputed":True}

    def build_from_dataframe(self, symbol: str, ticks: pd.DataFrame, timeframe: str | int = "1m", limit: int = 500) -> dict[str, Any]:
        allp=self.build_all_from_dataframe(symbol,ticks,limit=limit)
        key=str(timeframe).lower() if str(timeframe).lower().endswith("m") else f"{timeframe}m"
        return dict((allp.get("by_timeframe") or {}).get(key) or {})


AGGRESSION_DELTA = AggressionDeltaEngine()
