from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ...core.provider_bus import PROVIDER_BUS, FEATURE_BUS
from ...core.provider_data_lake import DATA_LAKE

from .settings import load_settings
from .health import TastytradeHealth
from .auth import TastytradeAuthenticator
from .client import TastytradeClient
from .market_data import TastytradeMarketData
from .dxlink import DXLinkManager
from .symbols import ecosystem_subscription_plan, derivative_subscription_plan
from ...core.asset_ecosystems import all_unique_equity_roots, all_unique_index_roots, all_unique_future_products
from ...core.provider_flow_fabric import OPTION_FLOW_FABRIC, PRICE_TICK_FABRIC
from ...core.obs import note as _obs_note


class TastytradeRuntime:
    def __init__(self) -> None:
        self.health = TastytradeHealth()
        self.settings = None
        self.auth = None
        self.client = None
        self.market_data = TastytradeMarketData(self.health)
        self.dxlink = None
        self._start_lock = asyncio.Lock()
        self._active_symbol = "DIA"
        self._active_plan: dict[str, Any] = {}
        self._generation = 0
        self._derivative_task: asyncio.Task | None = None
        self._catalog_task: asyncio.Task | None = None
        self._feature_task: asyncio.Task | None = None
        self._rest_context_task: asyncio.Task | None = None
        self._catalog_status: dict[str, Any] = {"running": False, "cycles": 0, "last_error": "", "last_completed_at": None}

    @property
    def configured(self) -> bool:
        return load_settings() is not None

    async def start(self, symbol: str = "DIA") -> bool:
        # Record the latest UI choice before doing network work. If the user switches
        # symbols while OAuth is still warming, startup must hydrate the newest symbol,
        # never the stale symbol that happened to be active at process launch.
        self._active_symbol = str(symbol or self._active_symbol).upper()
        async with self._start_lock:
            if self.dxlink:
                await self.select_asset(self._active_symbol)
                return True
            s = load_settings()
            if not s:
                self.health.patch(configured=False, auth="NOT_CONFIGURED", oauth="NOT_CONFIGURED", dxlink="DISCONNECTED", market_data="IDLE", scope="READ ONLY")
                return False
            self.settings = s
            self.health.patch(configured=True, auth="PENDING", oauth="PENDING", dxlink="DISCONNECTED", market_data="WARMING", scope="READ ONLY", last_error="")
            self.auth = TastytradeAuthenticator(s, self.health)
            self.client = TastytradeClient(s, self.auth, self.health)
            self.dxlink = DXLinkManager(self.client, self.market_data, self.health)
            try:
                # Validate OAuth first, then start the persistent streamer. This coroutine
                # itself is launched in the background by FastAPI lifespan.
                await self.auth.get_token()
                self.dxlink.start()
                await self.select_asset(self._active_symbol)
                if self.settings and self.settings.catalog_enabled and not (self._catalog_task and not self._catalog_task.done()):
                    self._catalog_task = asyncio.create_task(self._catalog_loop(), name="itmq-tastytrade-cold-catalog")
                if not (self._feature_task and not self._feature_task.done()):
                    self._feature_task = asyncio.create_task(self._derivative_feature_loop(), name="itmq-tastytrade-derivative-feature")
                return True
            except Exception as exc:
                self.health.patch(auth="ERROR", oauth="ERROR", dxlink="DISCONNECTED", market_data="DEGRADED", last_error=f"{type(exc).__name__}: {str(exc)[:160]}")
                # Provider isolation: tastytrade failure must never take TRACE/Scanner down.
                return False

    async def stop(self) -> None:
        if self._rest_context_task:
            self._rest_context_task.cancel()
            try: await self._rest_context_task
            except asyncio.CancelledError:
                self._rest_context_task = None
            except Exception as _e:
                _obs_note('runtime:rest_context_stop', _e)
            self._rest_context_task = None
        if self._feature_task:
            self._feature_task.cancel()
            try: await self._feature_task
            except asyncio.CancelledError:
                self._feature_task = None
            except Exception as _e:
                _obs_note('runtime:feature_stop', _e)
            self._feature_task = None
        if self._catalog_task:
            self._catalog_task.cancel()
            try: await self._catalog_task
            except asyncio.CancelledError:
                self._catalog_task = None
            except Exception as _e:
                _obs_note('runtime:catalog_stop', _e)
            self._catalog_task = None
        if self._derivative_task:
            self._derivative_task.cancel()
            try: await self._derivative_task
            except asyncio.CancelledError:
                self._derivative_task = None
            except Exception as _e:
                _obs_note('runtime:derivative_stop', _e)
            self._derivative_task = None
        if self.dxlink:
            await self.dxlink.stop()
        if self.client:
            await self.client.close()
        if self.auth:
            await self.auth.close()
        self.dxlink = None; self.client = None; self.auth = None

    async def select_asset(self, symbol: str) -> dict[str, Any]:
        self._active_symbol = str(symbol).upper()
        self._generation += 1
        generation = self._generation
        if self._derivative_task:
            self._derivative_task.cancel()
            self._derivative_task = None
        if self._rest_context_task:
            self._rest_context_task.cancel()
            self._rest_context_task = None
        if not self.dxlink or not self.client:
            return {"ready":False,"symbol":self._active_symbol,"reason":"NOT_CONFIGURED"}
        try:
            # Phase A: tiny underlying/future universe. This is the fast path.
            plan = await ecosystem_subscription_plan(self._active_symbol, self.client)
            if generation != self._generation or self._active_symbol != str(symbol).upper():
                return {"ready":False,"symbol":str(symbol).upper(),"reason":"STALE_ASSET_SELECTION"}
            self._active_plan = dict(plan)
            # Index fallback snapshots keep their own canonical index symbol.  If a
            # provider-issued DXLink streamer was resolved above, the live stream takes
            # over automatically and these REST observations simply age out.
            for r in (plan.get("index_snapshot") or []):
                if not isinstance(r, dict):
                    continue
                try:
                    idx = str(r.get("canonical_index") or r.get("symbol") or "").upper()
                    if not idx:
                        continue
                    px=r.get("lastExt") or r.get("last-ext") or r.get("last") or r.get("lastMkt") or r.get("last-mkt") or r.get("mark") or r.get("mid")
                    PROVIDER_BUS.ingest(
                        source="TASTYTRADE_REST", symbol=idx, event_type="SNAPSHOT",
                        values={"price":px,"bid":r.get("bid"),"ask":r.get("ask"),"volume":r.get("volume"),
                                "prev_close":r.get("prev-close") or r.get("previous-close"),
                                "provider_symbol":r.get("provider_index") or r.get("symbol"),
                                "parent_symbol":self._active_symbol,"role":r.get("role") or "INDEX"},
                        timestamp=r.get("updated-at"), received_at=None, sequence_ok=True,
                    )
                except Exception as _e:
                    _obs_note('runtime:143', _e)
            self.dxlink.set_subscriptions(set())
            base_items = []
            for item in plan.get("streamers", []):
                base_items.append({
                    "streamer_symbol": item["streamer_symbol"],
                    "events": tuple(item.get("events") or ("Quote","Trade","Summary")),
                    "canonical_symbol": item.get("canonical_symbol") or self._active_symbol,
                    "underlying_symbol": self._active_symbol,
                    "role": item.get("role"),
                    "instrument_type": item.get("instrument_type") or "EQUITY",
                    "metadata": {
                        "parent_symbol": self._active_symbol,
                        "provider_symbol": item.get("provider_symbol"),
                        "product": item.get("product"),
                        "preferred": item.get("preferred"),
                        "polarity": item.get("polarity", 1),
                    },
                })
            self.dxlink.add_symbols(base_items)
            # Historical+live 1m Candles are intentionally limited to the selected instrument
            # and a few preferred futures; they supply chart continuity without rebuilding charts.
            candle_added=0
            for item in base_items:
                role=str(item.get("role") or "").upper()
                if role=="UNDERLYING" or (str(item.get("instrument_type") or "").upper()=="FUTURE" and bool((item.get("metadata") or {}).get("preferred"))):
                    self.dxlink.add_candle(item.get("streamer_symbol"),canonical_symbol=item.get("canonical_symbol") or self._active_symbol,underlying_symbol=self._active_symbol,role=f"{role}_CANDLE",instrument_type=item.get("instrument_type") or "EQUITY",period="1m",lookback_minutes=1440)
                    candle_added+=1
                    if candle_added>=4: break
            # One REST context snapshot per symbol switch: extended-hours price + IV/liquidity
            # metrics. Streaming remains the hot path; this endpoint is never polled continuously.
            self._rest_context_task=asyncio.create_task(self._refresh_rest_context(self._active_symbol,generation,dict(plan)),name=f"itmq-tasty-rest-context-{self._active_symbol}-{generation}")
            # Phase B: option/Greeks hydration is intentionally detached from the click.
            if self.settings and self.settings.stream_derivatives:
                self._derivative_task = asyncio.create_task(
                    self._hydrate_derivatives(self._active_symbol, generation, dict(plan)),
                    name=f"itmq-tasty-derivatives-{self._active_symbol}-{generation}",
                )
            return {**plan,"derivatives":"HYDRATING" if self._derivative_task else "DISABLED"}
        except Exception as exc:
            self.health.patch(last_error=f"Asset subscription: {type(exc).__name__}: {str(exc)[:140]}")
            # Equity streamer symbols are normally their equity ticker; preserve a basic
            # price path even if futures/index discovery is temporarily unavailable.
            self.dxlink.set_subscriptions(set())
            self.dxlink.add_symbol(self._active_symbol, canonical_symbol=self._active_symbol, underlying_symbol=self._active_symbol, role="UNDERLYING", instrument_type="EQUITY")
            return {"ready":False,"symbol":self._active_symbol,"reason":str(exc)[:160],"fallback":"UNDERLYING_ONLY"}

    @staticmethod
    def _first(row: dict[str, Any], *keys: str):
        for k in keys:
            if row.get(k) is not None: return row.get(k)
        return None

    async def _refresh_rest_context(self, symbol: str, generation: int, base_plan: dict[str, Any]) -> None:
        """Fetch one non-blocking tastytrade REST context snapshot for PREMARKET/volatility."""
        try:
            if not self.client: return
            eco=(base_plan.get("ecosystem") or {}) if isinstance(base_plan,dict) else {}
            ptype=str(eco.get("primary_type") or "EQUITY").upper()
            # tastytrade classifies listed ETFs/ETNs in its market-data endpoint as equities.
            type_map={"EQUITY":"equity","ETF":"equity","ETN":"equity","INDEX":"index","FUTURE":"future","CRYPTOCURRENCY":"cryptocurrency"}
            typ=type_map.get(ptype,"equity")
            quote_symbol=symbol
            if typ=="future":
                # REST future quotes require the provider contract symbol (/YMU6 etc.), not product root YM.
                primary=next((x for x in (base_plan.get("streamers") or []) if str(x.get("role") or "").upper()=="UNDERLYING" and str(x.get("instrument_type") or "").upper()=="FUTURE"),None)
                quote_symbol=str((primary or {}).get("provider_symbol") or symbol)
            quote_task=self.client.market_data_by_type(typ,[quote_symbol])
            metric_task=self.client.market_metrics([symbol])
            qres,mres=await asyncio.gather(quote_task,metric_task,return_exceptions=True)
            if generation!=self._generation or symbol!=self._active_symbol: return
            quote=(qres[0] if isinstance(qres,list) and qres else {}) if not isinstance(qres,Exception) else {}
            metric=(mres[0] if isinstance(mres,list) and mres else {}) if not isinstance(mres,Exception) else {}
            if isinstance(quote,dict) and quote:
                px=self._first(quote,"lastExt","last-ext","last_ext","last","lastMkt","last-mkt","mark","mid")
                ts=self._first(quote,"updatedAt","updated-at")
                vals={"price":px,"last_ext":self._first(quote,"lastExt","last-ext","last_ext"),"last":quote.get("last"),"bid":quote.get("bid"),"ask":quote.get("ask"),"bid_size":self._first(quote,"bidSize","bid-size"),"ask_size":self._first(quote,"askSize","ask-size"),"volume":quote.get("volume"),"open":quote.get("open"),"high":self._first(quote,"dayHighPrice","day-high-price","dayHigh"),"low":self._first(quote,"dayLowPrice","day-low-price","dayLow"),"prev_close":self._first(quote,"prevClose","prev-close","prevDayClose"),"extended_hours_capable":True}
                PROVIDER_BUS.ingest(source="TASTYTRADE_REST",symbol=symbol,event_type="EXTENDED_SNAPSHOT",values=vals,timestamp=ts,received_at=None,sequence_ok=True)
                try:
                    fpx=float(px)
                    if fpx>0:
                        _rest_has_event_time = bool(ts)
                        PRICE_TICK_FABRIC.ingest(
                            source="TASTYTRADE_REST", symbol=symbol,
                            timestamp=ts or datetime.now(timezone.utc), price=fpx, size=0.0,
                            event_type="EXTENDED_SNAPSHOT", bid=quote.get("bid"), ask=quote.get("ask"),
                            metadata={"role":"REST_CONTEXT", "presentation_only":True,
                                      "event_time_valid":_rest_has_event_time,
                                      "event_time_source":"PROVIDER" if _rest_has_event_time else "RECEIVE_PROXY"})
                except Exception as _e:
                    _obs_note('runtime:225', _e)
            if isinstance(metric,dict) and metric:
                fv={"iv_index":self._first(metric,"implied-volatility-index","impliedVolatilityIndex"),"iv_rank":self._first(metric,"implied-volatility-rank","impliedVolatilityRank"),"iv_percentile":self._first(metric,"implied-volatility-percentile","impliedVolatilityPercentile"),"iv_index_5d_change":self._first(metric,"implied-volatility-index-5-day-change","impliedVolatilityIndex5DayChange"),"liquidity":metric.get("liquidity"),"liquidity_rank":self._first(metric,"liquidity-rank","liquidityRank"),"liquidity_rating":self._first(metric,"liquidity-rating","liquidityRating"),"expiration_iv":self._first(metric,"option-expiration-implied-volatilities","optionExpirationImpliedVolatilities") or []}
                FEATURE_BUS.ingest(source="TASTYTRADE_REST",symbol=symbol,feature_group="VOLATILITY_METRICS",values=fv,timestamp=datetime.now(timezone.utc),received_at=datetime.now(timezone.utc),confidence=100.0,ttl_ms=300000.0)
            self._active_plan={**self._active_plan,"rest_market_snapshot":quote,"market_metrics":metric,"rest_context":"READY"}
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if generation==self._generation:
                self._active_plan={**self._active_plan,"rest_context":"DEGRADED","rest_context_error":f"{type(exc).__name__}: {str(exc)[:140]}"}

    async def _hydrate_derivatives(self, symbol: str, generation: int, base_plan: dict[str, Any]) -> None:
        try:
            if not self.client or not self.dxlink or not self.settings:
                return
            plan = await derivative_subscription_plan(symbol, self.client, self.settings, base_plan)
            if generation != self._generation or symbol != self._active_symbol:
                return
            derivative_items = []
            for item in (plan.get("equity_options") or []) + (plan.get("index_options") or []) + (plan.get("future_options") or []):
                streamer=str(item.get("streamer_symbol") or "")
                if not streamer:
                    continue
                derivative_items.append({
                    "streamer_symbol": streamer,
                    "events": tuple(item.get("events") or ("Quote","Trade","Greeks","Summary")),
                    "canonical_symbol": item.get("canonical_symbol") or streamer,
                    "underlying_symbol": symbol,
                    "role": item.get("role"),
                    "instrument_type": item.get("instrument_type"),
                    "metadata": {
                        "parent_symbol": symbol, "component_symbol": item.get("component_symbol"),
                        "strike": item.get("strike"), "dte": item.get("dte"),
                        "expiration": item.get("expiration"), "option_type": item.get("option_type"),
                        "contract_multiplier": item.get("contract_multiplier"),
                        "provider_symbol": item.get("provider_symbol"),
                    },
                })
            self.dxlink.add_symbols(derivative_items)
            try:
                OPTION_FLOW_FABRIC.register_universe("TASTYTRADE_DXLINK",symbol,len(derivative_items))
            except Exception as _e:
                _obs_note('runtime:266', _e)
            if generation == self._generation:
                self._active_plan = {**base_plan,"derivative_plan":plan,"derivatives":"READY"}
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if generation == self._generation:
                self._active_plan = {**base_plan,"derivatives":"DEGRADED","derivative_error":f"{type(exc).__name__}: {str(exc)[:140]}"}
                self.health.patch(last_error=f"Derivative hydration: {type(exc).__name__}: {str(exc)[:140]}")

    async def _derivative_feature_loop(self) -> None:
        """Publish normalized observed derivative structure without blocking DXLink."""
        try:
            while self.dxlink:
                try:
                    feat=self.market_data.derivative_feature(self._active_symbol)
                    if int(feat.get("contracts_ready") or 0)>0:
                        FEATURE_BUS.ingest(
                            source="TASTYTRADE_DERIVATIVES", symbol=self._active_symbol,
                            feature_group="DERIVATIVES", values=feat,
                            confidence=float(((feat.get("directional") or {}).get("derivatives") or {}).get("confidence") or 0.0),
                            ttl_ms=20_000.0,
                        )
                except Exception as _e:
                    _obs_note('runtime:290', _e)
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return

    async def _catalog_loop(self) -> None:
        """Build a COLD instrument library for every ITM QUANT supported ecosystem.

        This never changes the LIVE DXLink subscription set. It only calls REST instrument
        discovery sequentially, archives complete returned definitions, then sleeps.
        """
        try:
            await asyncio.sleep(8.0)
            while self.client and self.settings and self.settings.catalog_enabled:
                try:
                    await self._catalog_sweep_once()
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    self._catalog_status["last_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                await asyncio.sleep(max(600.0, float(self.settings.catalog_refresh_minutes) * 60.0))
        except asyncio.CancelledError:
            return

    async def _catalog_sweep_once(self) -> dict[str, Any]:
        if not self.client or not self.settings:
            return {"ready": False, "reason": "NOT_CONFIGURED"}
        self._catalog_status["running"] = True
        self._catalog_status["last_error"] = ""
        result: dict[str, Any] = {"equities": {}, "futures": {}, "indexes": {}}
        future_products: set[str] = set()
        index_symbols: set[str] = set()
        delay = max(0.0, float(self.settings.catalog_asset_delay_ms) / 1000.0)
        try:
            for sym in all_unique_equity_roots():
                sym = str(sym).upper()
                try:
                    eq = await self.client.equity_instrument(sym)
                    if eq:
                        DATA_LAKE.archive_catalog(source="TASTYTRADE", symbol=sym, catalog_type="UNDERLYING_INSTRUMENT", items=[eq], metadata={"scope": "ITM_QUANT_ECOSYSTEM_UNIVERSE"})
                        DATA_LAKE.observe("TASTYTRADE", "UNDERLYING_INSTRUMENT", symbol=sym)
                except Exception as exc:
                    result["equities"].setdefault(sym, {})["instrument_error"] = type(exc).__name__
                try:
                    chain = await self.client.equity_option_instruments(sym)
                    DATA_LAKE.archive_catalog(source="TASTYTRADE", symbol=sym, catalog_type="EQUITY_OPTION_CATALOG", items=chain, metadata={"scope": "FULL_PROVIDER_RETURN_FOR_ECOSYSTEM_SYMBOL", "selection_applied": False})
                    result["equities"].setdefault(sym, {})["option_contracts"] = len(chain)
                except Exception as exc:
                    result["equities"].setdefault(sym, {})["options_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
                if delay:
                    await asyncio.sleep(delay)

            future_products = set(all_unique_future_products())
            index_symbols = set(all_unique_index_roots())
            for product in sorted(future_products):
                try:
                    front = await self.client.front_future(product)
                    if front:
                        DATA_LAKE.archive_catalog(source="TASTYTRADE", symbol=product, catalog_type="FUTURE_INSTRUMENT", items=[front], metadata={"role": "FRONT_FUTURE"})
                        DATA_LAKE.observe("TASTYTRADE", "FUTURE_INSTRUMENT", symbol=product)
                        result["futures"].setdefault(product, {})["front"] = front.get("symbol")
                except Exception as exc:
                    result["futures"].setdefault(product, {})["front_error"] = type(exc).__name__
                try:
                    fchain = await self.client.futures_option_instruments(product)
                    DATA_LAKE.archive_catalog(source="TASTYTRADE", symbol=product, catalog_type="FUTURE_OPTION_CATALOG", items=fchain, metadata={"scope": "FULL_PROVIDER_RETURN_FOR_PRODUCT", "selection_applied": False})
                    result["futures"].setdefault(product, {})["option_contracts"] = len(fchain)
                except Exception as exc:
                    result["futures"].setdefault(product, {})["options_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
                if delay:
                    await asyncio.sleep(delay)

            for idx in sorted(index_symbols):
                try:
                    rows = await self.client.market_data_by_type("index", [idx])
                    DATA_LAKE.archive_catalog(source="TASTYTRADE", symbol=idx, catalog_type="INDEX_SNAPSHOT", items=rows, metadata={"scope": "SUPPORTED_ECOSYSTEM_INDEX"})
                    if rows:
                        DATA_LAKE.observe("TASTYTRADE", "INDEX_SNAPSHOT", symbol=idx)
                    result["indexes"].setdefault(idx, {})["snapshot_rows"] = len(rows)
                except Exception as exc:
                    result["indexes"].setdefault(idx, {})["snapshot_error"] = f"{type(exc).__name__}"
                try:
                    chain = await self.client.equity_option_instruments(idx)
                    DATA_LAKE.archive_catalog(source="TASTYTRADE", symbol=idx, catalog_type="INDEX_OPTION_CATALOG", items=chain, metadata={"scope": "FULL_PROVIDER_RETURN_FOR_INDEX_ROOT", "selection_applied": False})
                    result["indexes"].setdefault(idx, {})["option_contracts"] = len(chain)
                    if chain:
                        DATA_LAKE.observe("TASTYTRADE", "INDEX_OPTION_CATALOG", symbol=idx)
                except Exception as exc:
                    result["indexes"].setdefault(idx, {})["options_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
                if delay:
                    await asyncio.sleep(delay)

            self._catalog_status["cycles"] = int(self._catalog_status.get("cycles") or 0) + 1
            self._catalog_status["last_completed_at"] = datetime.now(timezone.utc).isoformat()
            return {"ready": True, **result}
        finally:
            self._catalog_status["running"] = False

    def status(self) -> dict[str, Any]:
        out = self.health.snapshot(); out["active_symbol"] = self._active_symbol
        out["ecosystem"] = {k:v for k,v in (self._active_plan or {}).items() if k not in {"streamers","front_future","derivative_plan"}}
        out["derivatives"] = self.market_data.derivative_summary(self._active_symbol)
        dp=(self._active_plan or {}).get("derivative_plan") or {}
        out["derivative_contracts_selected"] = int(dp.get("contracts") or 0)
        out["catalog"] = dict(self._catalog_status)
        return out


TASTYTRADE = TastytradeRuntime()
