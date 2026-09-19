from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .settings import QuantDataSettings
from ...version import APP_VERSION


class QuantDataError(RuntimeError):
    pass


@dataclass
class QuantDataResponse:
    payload: dict[str, Any]
    status_code: int
    remaining: int | None = None
    limit: int | None = None
    reset_seconds: float | None = None


class QuantDataClient:
    def __init__(self, settings: QuantDataSettings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.base_url,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": f"ITM-QUANT/{APP_VERSION}",
                },
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                follow_redirects=False,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _int_header(headers: httpx.Headers, name: str) -> int | None:
        try:
            return int(headers.get(name, ""))
        except Exception:
            return None

    @staticmethod
    def _float_header(headers: httpx.Headers, name: str) -> float | None:
        try:
            return float(headers.get(name, ""))
        except Exception:
            return None

    async def post(self, path: str, body: dict[str, Any]) -> QuantDataResponse:
        if not self.settings.configured:
            raise QuantDataError("Quant Data is not configured")
        await self.start()
        assert self._client is not None
        try:
            response = await self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise QuantDataError("Quant Data request timed out") from exc
        except httpx.HTTPError as exc:
            raise QuantDataError(f"Quant Data transport error: {type(exc).__name__}") from exc

        remaining = self._int_header(response.headers, "X-RateLimit-Remaining")
        limit = self._int_header(response.headers, "X-RateLimit-Limit")
        reset = self._float_header(response.headers, "X-RateLimit-Reset")

        if response.status_code == 429:
            retry_after = self._float_header(response.headers, "Retry-After")
            wait = retry_after if retry_after is not None else reset
            # El límite es de la cuenta: frenar sólo al carril que recibió el 429
            # dejaría al otro gastando peticiones que ya se sabe que van a fallar.
            from .shared import QUOTA
            QUOTA.note_rate_limited(wait)
            raise QuantDataError(f"Quant Data rate limited; retry_after={wait}")
        if response.status_code in {401, 403}:
            raise QuantDataError(f"Quant Data authorization failed ({response.status_code})")
        if response.status_code >= 400:
            detail = ""
            try:
                obj = response.json()
                detail = str(obj.get("detail") or obj.get("title") or "")[:180]
            except Exception:
                detail = response.text[:180]
            raise QuantDataError(f"Quant Data HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except Exception as exc:
            raise QuantDataError("Quant Data returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise QuantDataError("Quant Data returned a non-object payload")
        return QuantDataResponse(payload=payload, status_code=response.status_code, remaining=remaining, limit=limit, reset_seconds=reset)
