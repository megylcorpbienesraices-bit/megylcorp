"""Dynamic ETF universe discovered from configured providers.

The catalog is presentation/discovery infrastructure. It never fuses instruments or
changes Scanner authority. Provider lists change over time, so the app persists a
small normalized cache instead of hard-coding a stale ETF list into a release.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.persistence import PERSISTENT_ROOT
from .obs import note as _obs_note
from . import alpaca_data
from ..providers.quantdata.client import QuantDataClient
from ..providers.quantdata.settings import load_settings as load_quantdata_settings

CATALOG_PATH = Path(PERSISTENT_ROOT) / "provider_etf_catalog.json"

_ETF_TERMS = (
    " ETF", "ETF ", "EXCHANGE TRADED", "ISHARES", "SPDR", "VANGUARD", "PROSHARES",
    "DIREXION", "GLOBAL X", "FIRST TRUST", "WISDOMTREE", "INVESCO", "VANECK",
    "ARK ", "SCHWAB", "FLEXSHARES", "AMPLIFY", "ROUNDHILL", "DEFIANCE",
    "BETABUILDERS", "DIMENSIONAL", "INNOVATOR", "SIMPLIFY", "TIDAL",
)


def _looks_like_etf(name: Any, *, sector: Any = None, industry: Any = None, exchange: Any = None) -> bool:
    n = re.sub(r"\s+", " ", str(name or "").upper()).strip()
    sec = str(sector or "").upper().replace(" ", "_")
    ind = str(industry or "").upper().replace(" ", "_")
    # Quant Data/FMP places many funds in NOT_APPLICABLE sector with either
    # NOT_APPLICABLE or an ASSET_MANAGEMENT subtype. Keep both so the provider
    # universe is not artificially reduced to names containing literal "ETF".
    if sec == "NOT_APPLICABLE" and n and (ind == "NOT_APPLICABLE" or ind.startswith("ASSET_MANAGEMENT")):
        return True
    if any(term in n for term in _ETF_TERMS):
        return True
    # Common legal names that omit literal "ETF".
    if " FUND" in n and str(exchange or "").upper() in {"ARCA", "NYSEARCA", "BATS", "NASDAQ", "AMEX"}:
        return True
    if "QQQ TRUST" in n or ("SPDR" in n and "TRUST" in n):
        return True
    return False


def load_cached_provider_etfs() -> list[dict[str, Any]]:
    if not CATALOG_PATH.is_file():
        return []
    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        rows = payload.get("assets") if isinstance(payload, dict) else []
        return [dict(x) for x in rows if isinstance(x, dict) and x.get("symbol")]
    except Exception as exc:
        _obs_note("provider_etf_catalog:cache_read", exc)
        return []


def _discover_alpaca_etfs() -> dict[str, dict[str, Any]]:
    settings = alpaca_data.load_settings()
    if settings is None:
        return {}
    all_assets = alpaca_data._get_json(  # bounded provider helper; secrets remain in headers only
        f"{alpaca_data.PAPER_BASE}/v2/assets", settings,
        {"status": "active", "asset_class": "us_equity"}, timeout=18.0,
    )
    option_assets = alpaca_data._get_json(
        f"{alpaca_data.PAPER_BASE}/v2/assets", settings,
        {"status": "active", "asset_class": "us_equity", "attributes": "has_options"}, timeout=18.0,
    )
    optionable = {str(x.get("symbol") or "").upper() for x in (option_assets or []) if isinstance(x, dict)}
    out: dict[str, dict[str, Any]] = {}
    for row in all_assets or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        name = str(row.get("name") or sym).strip()
        exchange = str(row.get("exchange") or "").upper().strip()
        if not sym or not _looks_like_etf(name, exchange=exchange):
            continue
        out[sym] = {
            "symbol": sym, "name": name, "exchange": exchange or "US",
            "alpaca": True, "alpaca_has_options": sym in optionable,
            "tradable": bool(row.get("tradable", True)),
            "fractionable": bool(row.get("fractionable", False)),
        }
    return out


async def _discover_quantdata_etfs() -> dict[str, dict[str, Any]]:
    settings = load_quantdata_settings()
    if not settings.configured:
        return {}
    client = QuantDataClient(settings)
    try:
        await client.start()
        market_task = client.post("/v1/equities/tool/market-map", {})
        options_task = client.post("/v1/options/tool/gainers-losers", {})
        market_res, options_res = await asyncio.gather(market_task, options_task, return_exceptions=True)
        market = market_res.payload if hasattr(market_res, "payload") else {}
        option_activity = options_res.payload if hasattr(options_res, "payload") else {}
        optionable = {str(k).upper() for k in ((option_activity or {}).get("data") or {}).keys()}
        out: dict[str, dict[str, Any]] = {}
        for sym_raw, row in (((market or {}).get("data") or {}).items()):
            if not isinstance(row, dict):
                continue
            sym = str(sym_raw or "").upper().strip()
            name = str(row.get("companyName") or sym).strip()
            sector = row.get("sector"); industry = row.get("industry")
            if not sym or not _looks_like_etf(name, sector=sector, industry=industry):
                continue
            out[sym] = {
                "symbol": sym, "name": name, "quantdata": True,
                "quantdata_option_activity": sym in optionable,
                "sector": sector, "industry": industry,
            }
        return out
    except Exception as exc:
        _obs_note("provider_etf_catalog:quantdata", exc, severity="DEGRADED")
        return {}
    finally:
        try:
            await client.close()
        except Exception as exc:
            _obs_note("provider_etf_catalog:quantdata_close", exc)


def _merge(alpaca: dict[str, dict[str, Any]], quantdata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    symbols = sorted(set(alpaca) | set(quantdata))
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        a = alpaca.get(sym, {}); q = quantdata.get(sym, {})
        name = str(q.get("name") or a.get("name") or sym)
        rows.append({
            "symbol": sym, "name": name,
            "exchange": str(a.get("exchange") or "US"),
            "providers": sorted([p for p, ok in (("ALPACA", a.get("alpaca")), ("QUANTDATA", q.get("quantdata"))) if ok]),
            "alpaca": bool(a.get("alpaca")), "quantdata": bool(q.get("quantdata")),
            "alpaca_has_options": bool(a.get("alpaca_has_options")),
            "quantdata_option_activity": bool(q.get("quantdata_option_activity")),
            "tradable": bool(a.get("tradable", True)), "fractionable": bool(a.get("fractionable", False)),
            "sector": q.get("sector"), "industry": q.get("industry"),
        })
    return rows


async def sync_provider_etf_catalog() -> dict[str, Any]:
    """Refresh the ETF catalog once in the background and register it in the runtime."""
    alpaca_task = asyncio.to_thread(_discover_alpaca_etfs)
    qd_task = _discover_quantdata_etfs()
    alpaca, quantdata = await asyncio.gather(alpaca_task, qd_task)
    rows = _merge(alpaca, quantdata)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assets": rows,
        "counts": {"total": len(rows), "alpaca": len(alpaca), "quantdata": len(quantdata)},
        "policy": "DYNAMIC_PROVIDER_ETF_UNIVERSE · NO CROSS-INSTRUMENT MATH",
    }
    try:
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CATALOG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(CATALOG_PATH)
    except Exception as exc:
        _obs_note("provider_etf_catalog:cache_write", exc)
    from .assets import register_provider_etfs
    register_provider_etfs(rows)
    return payload
