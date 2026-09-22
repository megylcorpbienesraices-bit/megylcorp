from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class QuantDataSettings:
    enabled: bool
    api_key: str
    base_url: str
    refresh_seconds: float
    request_timeout_seconds: float
    iv_lookback_days: int
    iv_maturity_days: int

    @property
    def configured(self) -> bool:
        key = (self.api_key or "").strip()
        return bool(self.enabled and key.startswith("qd_") and len(key) == 35)


def load_settings() -> QuantDataSettings:
    return QuantDataSettings(
        enabled=os.getenv("QUANTDATA_ENABLED", "1") == "1",
        api_key=os.getenv("QUANTDATA_API_KEY", "").strip(),
        base_url=(os.getenv("QUANTDATA_BASE_URL", "https://api.quantdata.us").strip().rstrip("/") or "https://api.quantdata.us"),
        refresh_seconds=max(8.0, float(os.getenv("QUANTDATA_REFRESH_SECONDS", "15"))),
        # Plazo INICIAL por petición. El definitivo lo calibra
        # `core.endpoint_runtime` con la latencia medida de cada endpoint.
        request_timeout_seconds=max(2.0, float(os.getenv("QUANTDATA_TIMEOUT_SECONDS", "12"))),
        iv_lookback_days=max(5, int(os.getenv("QUANTDATA_IV_LOOKBACK_DAYS", "30"))),
        iv_maturity_days=max(1, int(os.getenv("QUANTDATA_IV_MATURITY_DAYS", "30"))),
    )
