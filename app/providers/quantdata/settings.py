from __future__ import annotations

import os
from dataclasses import dataclass

from ...core.endpoint_runtime import CONNECT_TIMEOUT_S, WARM_START_READ_S
from ...core.request_governor import (DEFAULT_MAX_HEAVY_INFLIGHT,
                                      DEFAULT_MAX_INFLIGHT)


@dataclass(frozen=True)
class QuantDataSettings:
    enabled: bool
    api_key: str
    base_url: str
    refresh_seconds: float
    request_timeout_seconds: float
    iv_lookback_days: int
    iv_maturity_days: int
    connect_timeout_seconds: float = CONNECT_TIMEOUT_S
    read_warm_start_seconds: float = WARM_START_READ_S
    legacy_timeout_value: float | None = None
    legacy_timeout_ignored: bool = False
    max_inflight: int = DEFAULT_MAX_INFLIGHT
    max_heavy_inflight: int = DEFAULT_MAX_HEAVY_INFLIGHT

    @property
    def configured(self) -> bool:
        key = (self.api_key or "").strip()
        return bool(self.enabled and key.startswith("qd_") and len(key) == 35)

    def timeout_policy(self) -> dict:
        """Qué plazo rige y de dónde sale, para que el Auditor lo publique.

        Incluye la MIGRACIÓN de `QUANTDATA_TIMEOUT_SECONDS`: si el `.env` del
        operador trae un valor más corto que el warm start de política, se
        ignora y se dice. No hace falta que nadie edite su `.env`.
        """
        return {
            "connect_timeout_s": self.connect_timeout_seconds,
            "read_warm_start_s": self.read_warm_start_seconds,
            "authority": "p95 medido por endpoint; el warm start solo sin muestras",
            "legacy_env": "QUANTDATA_TIMEOUT_SECONDS",
            "legacy_value": self.legacy_timeout_value,
            "legacy_ignored": self.legacy_timeout_ignored,
            "legacy_note": (
                "ignorado: era más corto que el warm start de política y era la "
                "causa de que todo endpoint sin historia muriera a ese plazo"
                if self.legacy_timeout_ignored else
                ("respetado como warm start porque es más largo que la política"
                 if self.legacy_timeout_value else "no definido")),
            "max_inflight": self.max_inflight,
            "max_heavy_inflight": self.max_heavy_inflight,
        }


def _legacy_timeout() -> tuple[float | None, bool, float]:
    """Lee `QUANTDATA_TIMEOUT_SECONDS` y decide qué significa ahora.

    v1.58.0 · DEJA DE SER LA AUTORIDAD DEL ARRANQUE EN FRÍO.

    El instalador reparte `=5`, y con ese valor gobernando el warm start TODO
    endpoint sin muestras moría a los cinco segundos: el plazo adaptativo no
    gobernaba nada. La consola de producción lo demostró con ocho endpoints
    cortados exactamente en 5.0 s.

    La variable no se elimina —hay `.env` en marcha con ella— pero cambia de
    significado y se declara:

      · valor MÁS LARGO que el warm start  → se respeta (alguien pide paciencia)
      · valor MÁS CORTO                    → se IGNORA, y el Auditor lo dice

    Así nadie tiene que editar su `.env` para que el producto se comporte bien.
    """
    crudo = os.getenv("QUANTDATA_TIMEOUT_SECONDS", "").strip()
    if not crudo:
        return None, False, WARM_START_READ_S
    try:
        valor = float(crudo)
    except ValueError:
        return None, False, WARM_START_READ_S
    if valor >= WARM_START_READ_S:
        return valor, False, valor
    return valor, True, WARM_START_READ_S


def load_settings() -> QuantDataSettings:
    legacy, ignorado, warm = _legacy_timeout()
    return QuantDataSettings(
        enabled=os.getenv("QUANTDATA_ENABLED", "1") == "1",
        api_key=os.getenv("QUANTDATA_API_KEY", "").strip(),
        base_url=(os.getenv("QUANTDATA_BASE_URL", "https://api.quantdata.us").strip().rstrip("/") or "https://api.quantdata.us"),
        refresh_seconds=max(8.0, float(os.getenv("QUANTDATA_REFRESH_SECONDS", "15"))),
        # Se conserva por compatibilidad de la estructura: vale el warm start
        # EFECTIVO, no lo que diga el `.env`. Ver `_legacy_timeout`.
        request_timeout_seconds=warm,
        iv_lookback_days=max(5, int(os.getenv("QUANTDATA_IV_LOOKBACK_DAYS", "30"))),
        iv_maturity_days=max(1, int(os.getenv("QUANTDATA_IV_MATURITY_DAYS", "30"))),
        connect_timeout_seconds=max(0.5, float(
            os.getenv("QUANTDATA_CONNECT_TIMEOUT_SECONDS", str(CONNECT_TIMEOUT_S)))),
        read_warm_start_seconds=warm,
        legacy_timeout_value=legacy,
        legacy_timeout_ignored=ignorado,
        max_inflight=max(1, int(
            os.getenv("QUANTDATA_MAX_INFLIGHT", str(DEFAULT_MAX_INFLIGHT)))),
        max_heavy_inflight=max(1, int(
            os.getenv("QUANTDATA_MAX_HEAVY_INFLIGHT", str(DEFAULT_MAX_HEAVY_INFLIGHT)))),
    )
