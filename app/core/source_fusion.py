"""Provider-neutral source context for ITM QUANT v1.26.0.

Configured providers are first-class participants in the same data architecture.
There is no fixed primary/secondary/tertiary provider rank. Each observation is
accepted, degraded or excluded according to freshness, latency, integrity and
semantic comparability. Alpaca SIP/OPRA, tastytrade/DXLink, Quant Data options
intelligence and official macro sources participate wherever their actual entitlements
and data contracts provide valid information.

This module never fabricates unavailable feeds and never averages incompatible
instruments or contract semantics. ETF/index/future/options observations are kept
separate until they have a mathematically comparable representation.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
import math
import os

try:
    from . import alpaca_data
except Exception:  # pragma: no cover
    alpaca_data = None

try:
    from .provider_bus import FEATURE_BUS
except Exception:  # pragma: no cover
    FEATURE_BUS = None

# ASSET_ECOSYSTEMS se re-exporta a proposito: hay consumidores que lo leen como
# source_fusion.ASSET_ECOSYSTEMS. No es una importacion muerta.
from .asset_ecosystems import ASSET_ECOSYSTEMS, ecosystem_for, related_equity_symbols, public_summary  # noqa: F401
from .obs import note as _obs_note

@dataclass
class SourceRecord:
    name: str
    status: str
    role: str
    source_type: str
    timestamp: str | None = None
    age_seconds: float | None = None
    detail: str = ""
    values: Dict[str, Any] | None = None
    confidence: float | None = None
    observed: bool = True
    licensed: bool | None = None
    error: str | None = None

    def pack(self) -> Dict[str, Any]:
        d = asdict(self)
        d["values"] = d.get("values") or {}
        return d


def _finite(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(ts: Any) -> float | None:
    if ts in {None, ""}:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def quantdata_context(symbol: str) -> Dict[str, Any]:
    """Expose fresh Quant Data options intelligence already normalized on FEATURE_BUS.

    This function never performs network I/O. Desde v1.41.0 Quant Data participa como par
    de pleno derecho en los canales de opciones (ver core/provider_parity.py): puede
    ser la fuente seleccionada de un canal si su observación es la mejor del ciclo.
    La matemática nativa de ITM QUANT sigue siendo la que calcula la estructura; lo
    que cambia es que la observación externa ya no está limitada a confirmar.
    """
    sym = str(symbol).upper()
    if FEATURE_BUS is None:
        return SourceRecord("QUANTDATA", "UNAVAILABLE", "OPTIONS INTELLIGENCE", "REST FEATURE SOURCE", observed=False).pack()
    snap = FEATURE_BUS.snapshot(sym) or {}
    rows = []
    for row in snap.get("providers") or []:
        if str(row.get("source") or "").upper() != "QUANTDATA":
            continue
        if str(row.get("feature_group") or "").upper() != "OPTIONS_INTELLIGENCE":
            continue
        rows.append(row)
    usable = [r for r in rows if not r.get("stale") and float(((r.get("quality") or {}).get("quality_score") or 0)) >= 50]
    row = usable[-1] if usable else (rows[-1] if rows else None)
    if not row:
        return SourceRecord(
            "QUANTDATA", "WAITING", "OPTIONS INTELLIGENCE", "REST FEATURE SOURCE",
            detail="Sin snapshot Quant Data fresco en FEATURE_BUS en este ciclo; el canal lo gana otro par.",
            observed=False, licensed=None,
        ).pack()
    values = dict(row.get("values") or {})
    status = "LIVE" if row in usable else "STALE"
    return SourceRecord(
        "QUANTDATA", status, "OPTIONS INTELLIGENCE", "REST FEATURE SOURCE",
        timestamp=row.get("timestamp"), age_seconds=_finite(row.get("age_ms")) / 1000.0 if _finite(row.get("age_ms")) is not None else None,
        detail="Par de opciones en igualdad: compite por cada canal según la calidad observada del paquete.",
        values=values, confidence=_finite(row.get("confidence")), observed=bool(usable), licensed=None,
    ).pack()


def alpaca_related_context(symbols: Iterable[str]) -> Dict[str, Any]:
    """Use the existing Alpaca SIP entitlement for related ETF/stock breadth."""
    syms = [str(x).upper() for x in symbols if x]
    if not syms:
        return SourceRecord("ALPACA RELATED", "NOT APPLICABLE", "BREADTH", "SIP MARKET DATA", detail="Sin breadth relacionado para este activo.").pack()
    if alpaca_data is None or not alpaca_data.load_settings():
        return SourceRecord("ALPACA RELATED", "NOT CONFIGURED", "BREADTH", "SIP MARKET DATA", detail="Configura Alpaca SIP/OPRA.").pack()

    rows: Dict[str, Any] = {}
    errors: list[str] = []

    def num(d: Dict[str, Any], *keys: str) -> float | None:
        for k in keys:
            try:
                v = d.get(k) if isinstance(d, dict) else None
                if v is not None and math.isfinite(float(v)):
                    return float(v)
            except Exception as _e:
                _obs_note('source_fusion:146', _e)
        return None

    for sym in syms:
        try:
            snap = alpaca_data.fetch_stock_snapshot(symbol=sym)
            raw = snap.get("raw") or {}
            daily = raw.get("dailyBar") or raw.get("daily_bar") or {}
            prev = raw.get("prevDailyBar") or raw.get("prev_daily_bar") or {}
            minute = raw.get("minuteBar") or raw.get("minute_bar") or {}
            px = _finite(snap.get("spot"))
            prev_close = num(prev, "c", "close")
            ch = None if px is None or not prev_close else (px - prev_close) / prev_close * 100.0
            ts = snap.get("market_timestamp")
            rows[sym] = {
                "symbol": sym, "price": px, "previous_close": prev_close,
                "open": num(daily, "o", "open"), "high": num(daily, "h", "high"),
                "low": num(daily, "l", "low"), "change_pct": ch,
                "volume": num(daily, "v", "volume"),
                "vwap": num(minute, "vw", "vwap") or num(daily, "vw", "vwap"),
                "timestamp": ts, "source": "ALPACA SIP",
            }
        except Exception as exc:
            errors.append(f"{sym}: {str(exc)[:100]}")

    status = "LIVE" if rows else "ERROR" if errors else "NO DATA"
    return SourceRecord(
        "ALPACA RELATED", status, "BREADTH", "SIP MARKET DATA", timestamp=_now_iso(),
        detail=f"{len(rows)} mercados relacionados por Alpaca SIP." + (f" Errores: {'; '.join(errors[:2])}" if errors else ""),
        values=rows, confidence=90.0 if rows else None, licensed=True,
        error="; ".join(errors[:3]) if errors and not rows else None,
    ).pack()


def _public_source(name: str, detail: str) -> Dict[str, Any]:
    return SourceRecord(name, "BUILT-IN", "MACRO / EVENT CONTEXT", "PUBLIC OFFICIAL", detail=detail, licensed=True).pack()


def source_context(symbol: str, storage_dir: Path, spot: float | None = None, on_demand: bool = False) -> Dict[str, Any]:
    """Return only the source stack intentionally retained by the user."""
    del storage_dir, spot  # kept in signature for backward compatibility
    sym = str(symbol).upper()
    eco = ecosystem_for(sym)
    breadth = related_equity_symbols(sym)
    sources: Dict[str, Any] = {}
    sources["alpaca_related"] = alpaca_related_context(breadth) if on_demand else SourceRecord(
        "ALPACA RELATED", "READY" if (alpaca_data and alpaca_data.load_settings()) else "NOT CONFIGURED",
        "BREADTH", "SIP MARKET DATA", detail="ETFs/acciones relacionadas se consultan con la misma conexión Alpaca SIP.",
    ).pack()
    sources["quantdata"] = quantdata_context(sym)
    sources["fred"] = _public_source("FRED", "Series macro públicas consultadas por Macro Context; no requieren clave en esta integración.")
    sources["bls"] = _public_source("BLS", "Calendario oficial de publicaciones económicas consultado por Macro Context.")
    sources["federal_reserve"] = _public_source("FEDERAL RESERVE", "Calendario/eventos oficiales de la Fed consultados por Macro Context.")

    health = [
        {"key": key, "name": r.get("name"), "status": r.get("status"), "role": r.get("role"),
         "detail": r.get("detail"), "age_seconds": r.get("age_seconds"), "error": r.get("error")}
        for key, r in sources.items()
    ]
    usable = sum(1 for r in sources.values() if r.get("status") in {"LIVE", "OK", "PARTIAL", "BUILT-IN"})
    return {
        "symbol": sym, "ecosystem": public_summary(sym), "sources": sources, "related_bridge": {},
        "usable_sources": usable, "health": health, "generated_at": _now_iso(),
        "note": "Solo observaciones frescas y semánticamente comparables participan. Alpaca, tastytrade y Quant Data compiten en igualdad en cada canal de opciones; ninguno está limitado a confirmar a otro. Macro oficial aporta contexto.",
    }


def model_agreement(primary_levels: Dict[str, Any], fusion: Dict[str, Any]) -> Dict[str, Any]:
    """Audit external structural corroboration without creating another vote.

    Los strikes de mayor Gamma de cada par se comparan como evidencia neutral.
    Los niveles publicados los calcula ITM QUANT.
    """
    primary = [_finite(v) for v in primary_levels.values() if _finite(v) is not None]
    if not primary:
        return {"status": "NO ITM LEVELS", "matches": [], "checked": 0}
    step = max(0.25, float(os.getenv("ITM_MODEL_LEVEL_TOLERANCE", "0.60")))
    qd = (((fusion or {}).get("sources") or {}).get("quantdata") or {}).get("values") or {}
    gamma = qd.get("gamma") or {}
    refs = []
    for row in gamma.get("top_strikes") or []:
        if not isinstance(row, dict):
            continue
        v = _finite(row.get("strike"))
        if v is not None:
            refs.append(("QUANTDATA", "gamma_top_strike", v))
    matches = []
    for name, metric, v in refs[:12]:
        near = min(primary, key=lambda p: abs(p - v))
        matches.append({"source": name, "metric": metric, "value": v, "nearest_itm": near, "distance": abs(v-near), "agrees": abs(v-near) <= step})
    agree = sum(1 for x in matches if x["agrees"])
    return {
        "status": "STRONG" if matches and agree/len(matches) >= 0.67 else "MIXED" if matches else "NO COMPARABLE PROVIDER LEVELS",
        "matches": matches, "checked": len(matches), "agreed": agree,
        "agreement_pct": None if not matches else round(100*agree/len(matches), 1),
        "note": "Concordancia entre pares en igualdad; la estructura publicada la calcula ITM QUANT.",
    }


def enrich_external_market_context(external: Dict[str, Any], fusion: Dict[str, Any]) -> Dict[str, Any]:
    """Attach only Alpaca-related ETF breadth to the premarket context."""
    out = {"futures": {}, "direct_index": {}, "related": {}, "futures_status": "REMOVED", "index_status": "REMOVED"}
    if isinstance(external, dict):
        out.update({k: v for k, v in external.items() if k not in {"related"}})
        out["related"] = dict(external.get("related") or {})
    ar = ((((fusion or {}).get("sources") or {}).get("alpaca_related") or {}).get("values") or {})
    for name, rv in ar.items():
        if isinstance(rv, dict) and rv.get("price") is not None:
            out["related"][name] = {
                "symbol": name, "configured": True, "valid": True, "price": rv.get("price"),
                "timestamp": rv.get("timestamp"), "age_seconds": _age_seconds(rv.get("timestamp")),
                "source": "ALPACA SIP", "previous_close": rv.get("previous_close"),
                "open": rv.get("open"), "high": rv.get("high"), "low": rv.get("low"),
                "change_pct": rv.get("change_pct"), "volume": rv.get("volume"),
                "open_interest": None, "vwap": rv.get("vwap"),
            }
    out["source_fusion_attached"] = True
    return out


def chain_validation(primary_report: Dict[str, Any], fusion: Dict[str, Any]) -> Dict[str, Any]:
    """Explain chain provenance without inventing a provider hierarchy.

    Contract-level observations are only fused after symbol/expiry/strike/right semantics
    are proven equivalent. Provider analytics and market observations may still contribute
    through their corresponding data/feature fabrics.
    """
    del primary_report
    qd = (((fusion or {}).get("sources") or {}).get("quantdata") or {})
    return {
        "status": "NORMALIZED CHAIN FABRIC", "cboe": {}, "chartexchange": {},
        "quantdata_status": qd.get("status"),
        "native_structure_authority": "ITM_QUANT_NATIVE_OPTIONS_STRUCTURE",
        "note": "Contract chains are never merged blindly. Provider observations enter only after semantic normalization; native ITM QUANT chain math retains structural authority.",
    }
