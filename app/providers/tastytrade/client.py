from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from typing import Any

import httpx

from .auth import TastytradeAuthenticator
from .health import TastytradeHealth
from .settings import TastytradeSettings
from ...core.provider_data_lake import DATA_LAKE
from ...core.obs import note as _obs_note


class TastytradeClient:
    def __init__(self, settings: TastytradeSettings, auth: TastytradeAuthenticator, health: TastytradeHealth) -> None:
        self.settings = settings
        self.auth = auth
        self.health = health
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        self._quote_token: dict[str, Any] = {}
        self._quote_token_expires_monotonic: float = 0.0
        self._quote_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, params: Any = None, json: Any = None, retry: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(max(1, retry)):
            token = await self.auth.get_token(force=False)
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json",
            }
            try:
                r = await self._client.request(method.upper(), f"{self.settings.base_url}{path}", params=params, json=json, headers=headers)
                if r.status_code == 401 and attempt == 0:
                    self.auth.invalidate()
                    continue
                if r.status_code == 429:
                    delay = min(8.0, max(0.5, float(r.headers.get("Retry-After") or (2 ** attempt))))
                    await asyncio.sleep(delay)
                    continue
                r.raise_for_status()
                data = r.json() if r.content else {}
                # Archive market/instrument REST data only. Quote tokens and any future
                # account/private endpoints are deliberately excluded from the data lake.
                safe_prefixes = ("/instruments/", "/option-chains/", "/futures-option-chains/", "/market-data/by-type", "/market-metrics")
                if any(str(path).startswith(x) for x in safe_prefixes):
                    try:
                        symbol = "UNIVERSE"
                        if isinstance(params, (list, tuple)):
                            pdict = {str(k): v for k, v in params}
                            symbol = str(pdict.get("equity") or pdict.get("future") or pdict.get("index") or pdict.get("equity-option") or pdict.get("future-option") or "UNIVERSE").upper()
                        elif isinstance(params, dict):
                            symbol = str(params.get("equity") or params.get("future") or params.get("index") or "UNIVERSE").upper()
                        if symbol == "UNIVERSE":
                            pieces = [x for x in str(path).split("/") if x]
                            if pieces:
                                symbol = str(pieces[-1]).upper()
                        DATA_LAKE.archive_raw(source="TASTYTRADE_REST", symbol=symbol, event_type="REST_RESPONSE", payload=data, metadata={"path": str(path)})
                    except Exception as _e:
                        _obs_note('client:65', _e)
                return data
            except httpx.HTTPError as exc:
                last = exc
                if attempt + 1 < retry:
                    await asyncio.sleep(min(6.0, 0.5 * (2 ** attempt)))
                    continue
                break
        raise ConnectionError(f"Tastytrade REST request failed: {type(last).__name__ if last else 'unknown'}") from last

    async def get(self, path: str, *, params: Any = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    @staticmethod
    def _future_expiry(item: dict[str, Any]) -> tuple[int, str]:
        active = 0 if item.get("active-month") else 1
        return active, str(item.get("expiration-date") or item.get("expires-at") or "9999-12-31")

    async def front_future(self, product_code: str) -> dict[str, Any] | None:
        p = [("only-active-futures", "true"), ("product-code[]", str(product_code).lstrip("/"))]
        raw = await self.get("/instruments/futures", params=p)
        items = (((raw or {}).get("data") or {}).get("items") or [])
        rows = [x for x in items if isinstance(x, dict) and x.get("active", True)]
        if not rows:
            return None
        rows.sort(key=self._future_expiry)
        return rows[0]

    async def equity_instrument(self, symbol: str) -> dict[str, Any] | None:
        raw = await self.get(f"/instruments/equities/{symbol}")
        data = (raw or {}).get("data")
        return data if isinstance(data, dict) else None

    async def option_chain(self, symbol: str) -> dict[str, Any]:
        return await self.get(f"/option-chains/{symbol}/nested")

    async def equity_option_instruments(self, symbol: str) -> list[dict[str, Any]]:
        """Detailed chain with provider-issued streamer symbols and strikes."""
        raw = await self.get(f"/option-chains/{symbol}")
        return [x for x in ((((raw or {}).get("data") or {}).get("items") or [])) if isinstance(x, dict)]

    async def futures_option_chain(self, product_code: str) -> dict[str, Any]:
        # tastytrade documents this path by FUTURE PRODUCT CODE (e.g. ES), not by
        # hand-built front-month streamer symbol.
        code = str(product_code).lstrip("/").upper()
        return await self.get(f"/futures-option-chains/{code}/nested")

    async def futures_option_instruments(self, product_code: str) -> list[dict[str, Any]]:
        code = str(product_code).lstrip("/").upper()
        raw = await self.get(f"/futures-option-chains/{code}")
        return [x for x in ((((raw or {}).get("data") or {}).get("items") or [])) if isinstance(x, dict)]

    async def quote_token(self, *, force: bool = False) -> dict[str, Any]:
        """Return a cached DXLink quote token until shortly before expiry.

        tastytrade quote tokens currently live for 24 hours and the documented
        response does not require an ``expires-at`` field.  The monotonic fallback
        avoids refetching a quote token on every asset switch while still refreshing
        conservatively at 23 hours.
        """
        async with self._quote_lock:
            now_mono = time.monotonic()
            if not force and self._quote_token and now_mono < self._quote_token_expires_monotonic:
                return dict(self._quote_token)
            raw = await self.get("/api-quote-tokens")
            data = (raw or {}).get("data") or {}
            if not data.get("token") or not (data.get("dxlink-url") or data.get("websocket-url")):
                raise RuntimeError("Tastytrade quote-token response incomplete")
            ttl_seconds = 23 * 60 * 60
            exp = str(data.get("expires-at") or "")
            if exp:
                try:
                    dt = datetime.fromisoformat(exp.replace("Z", "+00:00")).astimezone(timezone.utc)
                    ttl_seconds = max(300, int((dt - datetime.now(timezone.utc)).total_seconds()) - 300)
                except Exception as _e:
                    _obs_note('client:140', _e)
            self._quote_token = dict(data)
            self._quote_token_expires_monotonic = now_mono + float(ttl_seconds)
            return dict(self._quote_token)

    async def market_data_by_type(self, instrument_type: str, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return []
        if len(symbols) > 100:
            raise ValueError("Tastytrade market-data snapshot limit is 100 symbols per request")
        typ=str(instrument_type or "equity").lower().replace("_","-")
        # Official API accepts instrument-type arrays; one request may contain <=100 symbols.
        raw = await self.get("/market-data/by-type", params=[(f"{typ}[]", str(sym)) for sym in symbols])
        return list((((raw or {}).get("data") or {}).get("items") or []))

    async def market_metrics(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Read-only IV/liquidity snapshot for underlyings; never polled on the hot stream."""
        clean=[str(x).upper() for x in symbols if str(x).strip()]
        if not clean:
            return []
        raw=await self.get("/market-metrics",params={"symbols":",".join(clean[:100])})
        data=(raw or {}).get("data") or {}
        return [x for x in (data.get("items") or []) if isinstance(x,dict)]
