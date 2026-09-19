from __future__ import annotations

from dataclasses import dataclass
import os
from ...version import APP_VERSION


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except Exception:
        return int(default)


@dataclass(frozen=True)
class TastytradeSettings:
    client_id: str
    client_secret: str
    refresh_token: str
    base_url: str = "https://api.tastyworks.com"
    user_agent: str = f"ITM-QUANT/{APP_VERSION}"
    enabled: bool = True
    read_only: bool = True
    # Derivatives hydrate after the underlying/future connection. They never block a
    # symbol switch and are bounded so one chain cannot flood the browser or event loop.
    stream_derivatives: bool = True
    derivative_max_dte: int = 14
    derivative_expirations: int = 3
    derivative_strikes_per_expiry: int = 24
    derivative_max_contracts: int = 180
    catalog_enabled: bool = True
    catalog_refresh_minutes: int = 60
    catalog_asset_delay_ms: int = 150

    @property
    def configured(self) -> bool:
        return bool(self.client_secret and self.refresh_token and self.base_url)


def _in_roster() -> bool:
    """Si tastytrade forma parte del roster de proveedores de esta instalación.

    Estando fuera del roster no basta con no mostrarlo: hay que no CONECTARLO. Un
    DXLink que se reconecta cada minuto consume hilos, llena el log de
    `reconnecting after TimeoutError` y hace parecer que el motor está degradado
    cuando en realidad nadie está esperando ese dato.
    """
    try:
        from app.core.provider_parity import peer_enabled
        return bool(peer_enabled("TASTYTRADE"))
    except Exception:
        # Si la política no se puede leer, no se asume acceso por defecto.
        return False


def load_settings() -> TastytradeSettings | None:
    # El roster MANDA, y no admite anulación. Antes TASTYTRADE_ENABLED=1 podía
    # colarlo de vuelta, así que un .env heredado de una instalación anterior
    # seguía levantando el DXLink aunque el proveedor estuviera retirado: el log
    # se llenaba de reconexiones y el operador veía justo lo que había pedido
    # quitar. Para reactivarlo se añade a ITM_OPTIONS_PEERS, que es el sitio
    # donde se declara con qué proveedores trabaja esta instalación.
    if not _in_roster():
        return None
    enabled = _bool("TASTYTRADE_ENABLED", True)
    client_id = str(os.getenv("TASTYTRADE_CLIENT_ID", "")).strip()
    client_secret = str(os.getenv("TASTYTRADE_CLIENT_SECRET", "")).strip()
    refresh_token = str(os.getenv("TASTYTRADE_REFRESH_TOKEN", "")).strip()
    base_url = str(os.getenv("TASTYTRADE_BASE_URL", "https://api.tastyworks.com")).strip().rstrip("/")
    user_agent = str(os.getenv("TASTYTRADE_USER_AGENT", f"ITM-QUANT/{APP_VERSION}")).strip() or f"ITM-QUANT/{APP_VERSION}"
    if not enabled or not client_secret or not refresh_token:
        return None
    return TastytradeSettings(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        base_url=base_url,
        user_agent=user_agent,
        enabled=enabled,
        read_only=True,
        stream_derivatives=_bool("TASTYTRADE_STREAM_DERIVATIVES", True),
        derivative_max_dte=_int("TASTYTRADE_DERIVATIVE_MAX_DTE", 14, 0, 60),
        derivative_expirations=_int("TASTYTRADE_DERIVATIVE_EXPIRATIONS", 3, 1, 8),
        derivative_strikes_per_expiry=_int("TASTYTRADE_DERIVATIVE_STRIKES_PER_EXPIRY", 24, 4, 100),
        derivative_max_contracts=_int("TASTYTRADE_DERIVATIVE_MAX_CONTRACTS", 180, 20, 2000),
        catalog_enabled=_bool("TASTYTRADE_CATALOG_ENABLED", True),
        catalog_refresh_minutes=_int("TASTYTRADE_CATALOG_REFRESH_MINUTES", 60, 10, 1440),
        catalog_asset_delay_ms=_int("TASTYTRADE_CATALOG_ASSET_DELAY_MS", 150, 0, 5000),
    )
