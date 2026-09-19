"""Non-blocking active-asset ecosystem fusion for ITM QUANT v1.26.0.

The tactical symbol remains the Scanner anchor.  Related ETFs/equities, indices and
futures are observed independently through every connected provider and only then
converted to a comparable directional-return feature.  Raw prices, Gamma, OI and
notional from different instruments are never added together.
"""

from __future__ import annotations

import asyncio
import math
import os
import statistics
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from . import alpaca_data
from .asset_ecosystems import component_descriptors, ecosystem_for, public_summary, related_equity_symbols
from .provider_bus import PROVIDER_BUS, FEATURE_BUS
from .obs import note as _obs_note, expected as _obs_expected


def _num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d if isinstance(d, dict) else {}
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            return cur.get(k)
    return default


def _median(values: list[float]) -> float | None:
    x = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return statistics.median(x) if x else None


class AssetEcosystemRuntime:
    """Maintains a lightweight normalized ecosystem signal for the active symbol.

    Provider equality is preserved: no provider has a fixed rank.  Cross-instrument
    weights below describe *economic roles* (future/index/related ETF), not providers.
    Missing groups are renormalized away rather than replaced with fabricated data.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._active_symbol = "DIA"
        self._cycles = 0
        self._last_run_at: str | None = None
        self._last_error = ""
        self._last_feature: dict[str, Any] = {}

    @property
    def interval_seconds(self) -> float:
        try:
            return max(2.0, min(30.0, float(os.getenv("ITM_ECOSYSTEM_REFRESH_SECONDS", "4"))))
        except Exception:
            return 4.0

    def start(self, symbol: str) -> None:
        self.select_asset(symbol)
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="itmq-asset-ecosystem-runtime")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Normal orderly shutdown.
                return
            except Exception as _e:
                _obs_note('ecosystem_runtime:stop', _e)
            self._task = None

    def select_asset(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol or "DIA").upper().strip()
        ecosystem_for(sym)  # validate/fallback registry access
        with self._lock:
            self._active_symbol = sym
            self._last_feature = {}
        self._wake.set()
        return public_summary(sym)

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    sym = self._active_symbol
                try:
                    await self._sample_alpaca_related(sym)
                    feature = self._build_feature(sym)
                    if feature:
                        FEATURE_BUS.ingest(
                            source="ECOSYSTEM_FUSION",
                            symbol=sym,
                            feature_group="ECOSYSTEM",
                            values=feature,
                            confidence=float(feature.get("confidence") or 0.0),
                            ttl_ms=max(15_000.0, self.interval_seconds * 3500.0),
                        )
                    with self._lock:
                        if self._active_symbol == sym:
                            self._last_feature = feature
                            self._last_run_at = datetime.now(timezone.utc).isoformat()
                            self._cycles += 1
                            self._last_error = ""
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    _obs_expected('ecosystem_runtime:cadence_tick')
                    continue
        except asyncio.CancelledError:
            return

    async def _sample_alpaca_related(self, parent: str) -> None:
        """Sample only equity/ETF components Alpaca actually supports.

        Futures/indices are intentionally not requested from Alpaca because this build's
        Alpaca adapter is an equities/options adapter.  Those roles are filled by other
        providers when available.
        """
        if not alpaca_data.load_settings():
            return
        eco = ecosystem_for(parent)
        role_map = {str(x.get("symbol") or "").upper(): x for x in eco.get("related_equities", [])}
        for sym in related_equity_symbols(parent):
            try:
                snap = await asyncio.to_thread(alpaca_data.fetch_stock_snapshot, None, sym)
                raw = snap.get("raw") or {}
                q = _get(raw, "latestQuote", "latest_quote", default={}) or {}
                t = _get(raw, "latestTrade", "latest_trade", default={}) or {}
                day = _get(raw, "dailyBar", "daily_bar", default={}) or {}
                prev = _get(raw, "prevDailyBar", "prev_daily_bar", default={}) or {}
                bid = _num(_get(q, "bp", "bid_price")); ask = _num(_get(q, "ap", "ask_price"))
                px = _num(snap.get("spot")) or _num(_get(t, "p", "price"))
                meta = role_map.get(sym, {})
                ts = snap.get("market_timestamp") or _get(t, "t", "timestamp") or _get(q, "t", "timestamp") or datetime.now(timezone.utc)
                PROVIDER_BUS.ingest(
                    source="ALPACA_REST", symbol=sym, event_type="SNAPSHOT",
                    values={
                        "price": px, "bid": bid, "ask": ask,
                        "prev_close": _num(_get(prev, "c", "close")),
                        "day_open": _num(_get(day, "o", "open")),
                        "day_volume": _num(_get(day, "v", "volume")),
                        "parent_symbol": parent, "component_role": meta.get("role", "RELATED_EQUITY"),
                        "polarity": int(meta.get("polarity", 1) or 1),
                    },
                    timestamp=ts, received_at=datetime.now(timezone.utc), sequence_ok=True,
                )
            except Exception as _e:
                # One unsupported/temporarily unavailable component must not poison the set.
                _obs_note('ecosystem_runtime:170', _e)
                continue

    @staticmethod
    def _baseline_for(symbol: str) -> float | None:
        vals: list[float] = []
        try:
            for row in PROVIDER_BUS.latest_events(symbol):
                v = row.get("values") or {}
                for key in ("prev_close", "prevDayClosePrice", "prev_day_close", "day_open", "dayOpenPrice"):
                    n = _num(v.get(key))
                    if n and n > 0:
                        vals.append(n)
                        break
        except Exception as _e:
            _obs_note('ecosystem_runtime:184', _e)
        return _median(vals)

    def _component_observation(self, desc: dict[str, Any]) -> dict[str, Any] | None:
        sym = str(desc.get("symbol") or "").upper()
        if not sym:
            return None
        snap = PROVIDER_BUS.snapshot(sym)
        price = _num(snap.get("consensus_price"))
        baseline = self._baseline_for(sym)
        if not price or not baseline or baseline <= 0:
            return None
        ret_pct = (price / baseline - 1.0) * 100.0
        polarity = int(desc.get("polarity", 1) or 1)
        ret_pct *= 1 if polarity >= 0 else -1
        quality = float(snap.get("confidence") or 0.0)
        # Saturate around a 0.60% intraday move; tiny noise under ~1bp has little weight.
        strength = min(100.0, max(0.0, abs(ret_pct) / 0.60 * 100.0))
        confidence = min(100.0, quality * 0.65 + strength * 0.35)
        sign = 1 if ret_pct > 0.01 else -1 if ret_pct < -0.01 else 0
        return {
            "symbol": sym, "role": str(desc.get("role") or "COMPONENT").upper(),
            "instrument_type": str(desc.get("instrument_type") or "UNKNOWN").upper(),
            "preferred": bool(desc.get("preferred", False)), "polarity": polarity,
            "price": price, "baseline": baseline, "return_pct": round(ret_pct, 5),
            "sign": sign, "confidence": round(confidence, 2),
            "market_data_confidence": round(quality, 2),
            "providers": snap.get("usable_providers") or [],
        }

    def _build_feature(self, parent: str) -> dict[str, Any]:
        descs = component_descriptors(parent)
        observations = [x for x in (self._component_observation(d) for d in descs) if x]
        # The primary itself anchors execution and is already modeled elsewhere.  The
        # ecosystem channel measures corroborating cross-instrument structure only.
        observations = [x for x in observations if x.get("role") != "PRIMARY"]

        groups: dict[str, list[dict[str, Any]]] = {"FUTURE": [], "INDEX": [], "RELATED": []}
        for o in observations:
            typ = str(o.get("instrument_type") or "")
            role = str(o.get("role") or "")
            if typ == "FUTURE":
                groups["FUTURE"].append(o)
            elif typ == "INDEX" or "INDEX" in role:
                groups["INDEX"].append(o)
            elif typ in {"ETF", "EQUITY"} or "ETF" in role or "EQUITY" in role:
                groups["RELATED"].append(o)

        # Prevent major+micro duplicates from becoming two votes. Prefer the provider-
        # resolved major future, use micro only as fallback.
        if groups["FUTURE"]:
            preferred = [x for x in groups["FUTURE"] if x.get("preferred")]
            groups["FUTURE"] = preferred[:1] if preferred else groups["FUTURE"][:1]

        group_weights = {"FUTURE": 0.40, "INDEX": 0.35, "RELATED": 0.25}
        group_state: dict[str, Any] = {}
        signed = 0.0; total = 0.0
        for name, obs in groups.items():
            active = [x for x in obs if int(x.get("sign") or 0) != 0]
            if not active:
                continue
            wsum = sum(max(1.0, float(x.get("confidence") or 0.0)) for x in active)
            ratio = sum(int(x["sign"]) * max(1.0, float(x.get("confidence") or 0.0)) for x in active) / max(wsum, 1e-9)
            gsign = 1 if ratio > 0.08 else -1 if ratio < -0.08 else 0
            gconf = min(100.0, abs(ratio) * (wsum / len(active)))
            group_state[name.lower()] = {"sign": gsign, "confidence": round(gconf, 2), "components": active}
            if gsign:
                ew = group_weights[name]
                signed += gsign * ew * gconf
                total += ew * gconf
        if total > 0:
            ratio = signed / total
            sign = 1 if ratio > 0.08 else -1 if ratio < -0.08 else 0
            conf = min(100.0, abs(ratio) * min(100.0, total / max(sum(group_weights.values()), 1e-9) * 1.8))
        else:
            sign = 0; conf = 0.0
        return {
            "directional": {"ecosystem": {"sign": sign, "confidence": round(conf, 2)}},
            "confidence": round(conf, 2), "groups": group_state, "components": observations,
            "ecosystem": public_summary(parent),
            "fusion_rule": "SEPARATE_INSTRUMENT_RETURNS_THEN_ROLE_NORMALIZED_FUSION",
            "provider_rule": "NO_FIXED_PROVIDER_RANK_QUALITY_BY_OBSERVATION",
            "authority": "CONTEXTUAL_DIRECTIONAL_FEATURE_NOT_STANDALONE_SIGNAL",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            sym = self._active_symbol
            return {
                "running": bool(self._task and not self._task.done()), "active_symbol": sym,
                "refresh_seconds": self.interval_seconds, "cycles": self._cycles,
                "last_run_at": self._last_run_at, "last_error": self._last_error,
                "ecosystem": public_summary(sym), "latest_feature": dict(self._last_feature or {}),
                "policy": "ETF_INDEX_FUTURE_DERIVATIVES_SEPARATE_MATH_THEN_NORMALIZED_FUSION",
            }


ECOSYSTEM_RUNTIME = AssetEcosystemRuntime()
