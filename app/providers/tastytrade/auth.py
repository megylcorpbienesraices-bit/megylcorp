from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from .settings import TastytradeSettings
from .health import TastytradeHealth

logger = logging.getLogger("ITM_QUANT.TastytradeAuth")


class TastytradeAuthenticator:
    """OAuth2 access-token manager.

    Secrets remain in environment variables and are never included in logs or
    exceptions. tastytrade access tokens are short lived; refresh is serialized so
    concurrent consumers cannot create a token-refresh storm.
    """

    def __init__(self, settings: TastytradeSettings, health: TastytradeHealth) -> None:
        self.settings = settings
        self.health = health
        self._access_token: Optional[str] = None
        self._expires_monotonic = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=8.0))

    async def close(self) -> None:
        await self._client.aclose()

    def invalidate(self) -> None:
        self._access_token = None
        self._expires_monotonic = 0.0

    def _valid(self) -> bool:
        # Refresh with a 90-second safety margin. Current access tokens are 15 min.
        return bool(self._access_token and time.monotonic() + 90.0 < self._expires_monotonic)

    async def get_token(self, *, force: bool = False) -> str:
        if not force and self._valid():
            return self._access_token or ""
        async with self._lock:
            if not force and self._valid():
                return self._access_token or ""
            return await self._refresh()

    async def _refresh(self) -> str:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self.settings.refresh_token,
            "client_secret": self.settings.client_secret,
        }
        if self.settings.client_id:
            body["client_id"] = self.settings.client_id
        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            r = await self._client.post(f"{self.settings.base_url}/oauth/token", json=body, headers=headers)
            if r.status_code in {401, 403}:
                self.health.patch(auth="ERROR", oauth=f"HTTP_{r.status_code}", last_error="OAuth permission/credential failure")
                raise PermissionError(f"Tastytrade OAuth rejected request ({r.status_code})")
            r.raise_for_status()
            data = r.json() if r.content else {}
            token = str(data.get("access_token") or "")
            if not token:
                raise RuntimeError("Tastytrade OAuth response did not contain access_token")
            expires_in = max(60, int(data.get("expires_in") or 900))
            self._access_token = token
            self._expires_monotonic = time.monotonic() + float(expires_in)
            self.health.patch(configured=True, auth="OK", oauth="OK", last_error="")
            logger.info("[TASTYTRADE AUTH] OAuth access token refreshed successfully; token value suppressed")
            return token
        except (httpx.HTTPError, ValueError) as exc:
            self.health.patch(auth="ERROR", oauth="ERROR", last_error=f"{type(exc).__name__}: {str(exc)[:160]}")
            raise ConnectionError(f"Tastytrade OAuth network/protocol error: {type(exc).__name__}") from exc
