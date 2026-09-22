"""Catálogo universal de activos descubiertos en los proveedores (v1.42).

QUÉ SUSTITUYE
-------------
`provider_etf_catalog` descubría exclusivamente ETF. Fue un buen puente, pero fija
en el código una frontera que el mercado no tiene: AAPL, NVDA o TSLA quedaban como
«equities conocidas a mano» dentro de `instruments.REGISTRY`, mientras 2.000 ETF
entraban solos. El resultado es que el universo dependía de dos mecanismos distintos
y sólo uno se actualizaba.

Aquí se descubre el universo COMPLETO de us_equity con una única consulta y se
clasifica en EQUITY o ETF. La clasificación no elige matemática distinta: elige
`asset_class`, y la matemática la decide `ContractSpec` a partir de ahí. Así
desaparece la necesidad de ramificar por ticker en cualquier parte del motor.

QUÉ NO HACE
-----------
No promete análisis. `options_enabled` viene del propio Alpaca (`attributes=has_options`)
y es lo único que autoriza a marcar un activo como analizable: prometer una cadena que
el proveedor no sirve es exactamente lo que dejaba a QQQ con el panel vacío. Un activo
sin cadena propia se publica igual, con precio y evidencia de mercado, y dice por qué
no tiene derivados.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.persistence import PERSISTENT_ROOT
from . import universe
from .obs import note as _obs_note
from . import alpaca_data
from .provider_etf_catalog import _looks_like_etf, _discover_quantdata_etfs

CATALOG_PATH = Path(PERSISTENT_ROOT) / "provider_asset_catalog.json"

ASSET_CLASS_EQUITY = "EQUITY"
ASSET_CLASS_ETF = "ETF"

# Techo de seguridad del universo publicado. Alpaca cataloga >11.000 símbolos activos;
# volcarlos todos en el selector no ayuda a nadie y multiplica el trabajo de arranque.
# El corte es por capacidad real: primero los que tienen cadena de opciones.
MAX_PUBLISHED = int(1200)


def _row_asset_class(name: Any, exchange: Any, *, sector: Any = None, industry: Any = None) -> str:
    return ASSET_CLASS_ETF if _looks_like_etf(name, sector=sector, industry=industry,
                                              exchange=exchange) else ASSET_CLASS_EQUITY


def discover_alpaca_universe() -> dict[str, dict[str, Any]]:
    """Universo us_equity completo con la capacidad real de cada símbolo.

    Dos llamadas, no una por símbolo: la segunda (`attributes=has_options`) es la que
    distingue «Alpaca conoce este ticker» de «Alpaca sirve su cadena», que es la
    diferencia entre un panel con datos y un panel vacío.
    """
    settings = alpaca_data.load_settings()
    if settings is None:
        return {}
    base_params = {"status": "active", "asset_class": "us_equity"}
    all_assets = alpaca_data._get_json(f"{alpaca_data.PAPER_BASE}/v2/assets", settings,
                                       dict(base_params), timeout=25.0)
    option_assets = alpaca_data._get_json(f"{alpaca_data.PAPER_BASE}/v2/assets", settings,
                                          dict(base_params, attributes="has_options"), timeout=25.0)
    optionable = {str(x.get("symbol") or "").upper()
                  for x in (option_assets or []) if isinstance(x, dict)}

    out: dict[str, dict[str, Any]] = {}
    for row in all_assets or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        name = str(row.get("name") or sym).strip()
        exchange = str(row.get("exchange") or "").upper().strip() or "US"
        out[sym] = {
            "symbol": sym,
            "name": name,
            "asset_class": _row_asset_class(name, exchange),
            "exchange": exchange,
            "status": str(row.get("status") or "active"),
            "tradable": bool(row.get("tradable", True)),
            "shortable": bool(row.get("shortable", False)),
            "easy_to_borrow": bool(row.get("easy_to_borrow", False)),
            "fractionable": bool(row.get("fractionable", False)),
            "marginable": bool(row.get("marginable", False)),
            "alpaca": True,
            "alpaca_has_options": sym in optionable,
        }
    return out


def merge_universe(alpaca: dict[str, dict[str, Any]],
                   quantdata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Fusiona capacidades, nunca valores de mercado.

    Este merge decide QUIÉN puede servir qué. No promedia precios, ni Greeks, ni
    exposición: ese reparto lo gobierna `metric_authority`, y mezclarlo aquí sería
    reintroducir por la puerta de atrás la media de proveedores que v1.42 retira.
    """
    symbols = sorted(set(alpaca) | set(quantdata))
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        a = alpaca.get(sym, {})
        q = quantdata.get(sym, {})
        name = str(a.get("name") or q.get("name") or sym)
        sector, industry = q.get("sector"), q.get("industry")
        klass = a.get("asset_class") or _row_asset_class(name, a.get("exchange"),
                                                         sector=sector, industry=industry)
        providers = sorted(p for p, ok in (("ALPACA", a.get("alpaca")),
                                           ("QUANTDATA", q.get("quantdata"))) if ok)
        rows.append({
            "symbol": sym, "name": name, "asset_class": klass,
            "exchange": str(a.get("exchange") or "US"),
            "status": str(a.get("status") or "active"),
            "providers": providers,
            "alpaca": bool(a.get("alpaca")), "quantdata": bool(q.get("quantdata")),
            "alpaca_has_options": bool(a.get("alpaca_has_options")),
            "quantdata_option_activity": bool(q.get("quantdata_option_activity")),
            "options_enabled": bool(a.get("alpaca_has_options")),
            "tradable": bool(a.get("tradable", True)),
            "shortable": bool(a.get("shortable", False)),
            "fractionable": bool(a.get("fractionable", False)),
            "marginable": bool(a.get("marginable", False)),
            "sector": sector, "industry": industry,
        })
    return rows


def _publication_rank(row: dict[str, Any]) -> tuple:
    """Orden de publicación: primero lo que de verdad se puede analizar.

    (1) cadena de opciones confirmada, (2) corroborado por el segundo proveedor,
    (3) negociable, (4) alfabético para que el corte sea determinista entre arranques.
    """
    return (0 if row.get("options_enabled") else 1,
            0 if len(row.get("providers") or ()) > 1 else 1,
            0 if row.get("tradable") else 1,
            str(row.get("symbol") or ""))


def select_published(rows: list[dict[str, Any]], limit: int = MAX_PUBLISHED) -> list[dict[str, Any]]:
    """Lo que se publica y se cachea del descubrimiento.

    v1.58.0 · El cerrojo del universo se aplica AQUÍ además de en el registro, y
    a propósito: así la caché en disco tampoco engorda con miles de símbolos que
    nadie va a mirar. Dos cerrojos para la misma regla no es duplicar lógica; es
    que el catálogo no llegue a escribirse con lo que el registro va a rechazar.
    """
    permitidos = [r for r in rows if universe.is_allowed(r.get("symbol"))]
    return sorted(permitidos, key=_publication_rank)[: max(int(limit), 0)]


def load_cached_universe() -> list[dict[str, Any]]:
    if not CATALOG_PATH.is_file():
        # Compatibilidad hacia atrás: un despliegue que venga de v1.41 sólo tiene la
        # caché de ETF. Se reutiliza en vez de arrancar con el universo vacío.
        from .provider_etf_catalog import load_cached_provider_etfs
        legacy = load_cached_provider_etfs()
        for row in legacy:
            row.setdefault("asset_class", ASSET_CLASS_ETF)
            row.setdefault("options_enabled", bool(row.get("alpaca_has_options")))
        return legacy
    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        rows = payload.get("assets") if isinstance(payload, dict) else []
        # Una caché escrita por una versión anterior puede traer el universo
        # entero. Se filtra al leerla, no sólo al escribirla.
        return [dict(x) for x in rows
                if isinstance(x, dict) and universe.is_allowed(x.get("symbol"))]
    except Exception as exc:
        _obs_note("provider_asset_catalog:cache_read", exc)
        return []


async def sync_provider_asset_catalog() -> dict[str, Any]:
    """Descubre el universo una vez en segundo plano y lo registra en el runtime."""
    alpaca_task = asyncio.to_thread(discover_alpaca_universe)
    qd_task = _discover_quantdata_etfs()
    alpaca, quantdata = await asyncio.gather(alpaca_task, qd_task)
    rows = merge_universe(alpaca, quantdata)
    published = select_published(rows)

    counts = {
        "discovered": len(rows),
        "published": len(published),
        "equity": sum(1 for r in published if r["asset_class"] == ASSET_CLASS_EQUITY),
        "etf": sum(1 for r in published if r["asset_class"] == ASSET_CLASS_ETF),
        "optionable": sum(1 for r in published if r.get("options_enabled")),
        "alpaca": len(alpaca), "quantdata": len(quantdata),
    }
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": published, "counts": counts,
        "policy": "UNIVERSAL_EQUITY_ETF_UNIVERSE · SEPARATE INSTRUMENT MATH · CAPABILITY GATED",
        "limit": MAX_PUBLISHED,
    }
    try:
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CATALOG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(CATALOG_PATH)
    except Exception as exc:
        _obs_note("provider_asset_catalog:cache_write", exc)

    from .assets import register_provider_assets
    payload["registered"] = register_provider_assets(published)
    return payload
