"""Ecosystem registry (v1.42: MULTI_ASSET).

El ecosistema Dow sigue existiendo como FAMILIA de instrumentos con confluencia
propia. Lo que deja de existir es la idea de que el programa entero sea "Dow
specialized": la matemática es por instrumento y el universo es Equity + ETF.

The runtime keeps each instrument mathematically separate. DIA, DJX, YM and MYM do not
borrow raw price/Gamma/OI levels from one another. XLI/XLF/VIX/VXD are contextual inputs
only when their own observed data is available.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable


def _fut(product: str, role: str = "PRIMARY_FUTURE", *, preferred: bool = True) -> dict[str, Any]:
    return {"product": product.upper(), "role": role, "preferred": bool(preferred)}


def _eq(symbol: str, role: str, *, polarity: int = 1) -> dict[str, Any]:
    return {"symbol": symbol.upper(), "role": role, "polarity": 1 if int(polarity) >= 0 else -1}


def _idx(symbol: str, role: str = "BENCHMARK_INDEX", *, aliases: Iterable[str] = ()) -> dict[str, Any]:
    return {"symbol": symbol.upper(), "role": role, "aliases": [str(x).upper() for x in aliases]}


ASSET_ECOSYSTEMS: dict[str, dict[str, Any]] = {
    "DIA": {
        "family":"DOW","primary_type":"ETF",
        "related_equities":[_eq("XLI","SECTOR_CONFIRMATION"),_eq("XLF","SECTOR_CONFIRMATION")],
        "indices":[_idx("DJX","DOW_INDEX")],
        "futures":[_fut("YM"),_fut("MYM","MICRO_FUTURE",preferred=False)],
        "derivatives":{"equity_options":["DIA"],"index_options":["DJX"],"future_options":["YM","MYM"]},
    },
    "YM": {
        "family":"DOW","primary_type":"FUTURE",
        "related_equities":[_eq("DIA","REFERENCE_ETF"),_eq("XLI","SECTOR_CONFIRMATION"),_eq("XLF","SECTOR_CONFIRMATION")],
        "indices":[_idx("DJX","DOW_INDEX")],
        "futures":[_fut("YM"),_fut("MYM","MICRO_FUTURE",preferred=False)],
        "derivatives":{"equity_options":["DIA"],"index_options":["DJX"],"future_options":["YM","MYM"]},
    },
    "MYM": {
        "family":"DOW","primary_type":"FUTURE",
        "related_equities":[_eq("DIA","REFERENCE_ETF"),_eq("XLI","SECTOR_CONFIRMATION"),_eq("XLF","SECTOR_CONFIRMATION")],
        "indices":[_idx("DJX","DOW_INDEX")],
        "futures":[_fut("MYM"),_fut("YM","REFERENCE_FUTURE",preferred=False)],
        "derivatives":{"equity_options":["DIA"],"index_options":["DJX"],"future_options":["MYM","YM"]},
    },
    "DJX": {
        "family":"DOW","primary_type":"INDEX",
        "related_equities":[_eq("DIA","REFERENCE_ETF"),_eq("XLI","SECTOR_CONFIRMATION"),_eq("XLF","SECTOR_CONFIRMATION")],
        "indices":[_idx("DJX","DOW_INDEX")],
        "futures":[_fut("YM"),_fut("MYM","MICRO_FUTURE",preferred=False)],
        "derivatives":{"equity_options":["DIA"],"index_options":["DJX"],"future_options":["YM","MYM"]},
    },
    "XLI": {
        "family":"DOW_CONTEXT","primary_type":"ETF",
        "related_equities":[_eq("DIA","DOW_REFERENCE_ETF"),_eq("XLF","PEER_CONFIRMATION")],
        "indices":[_idx("DJX","DOW_INDEX")],"futures":[_fut("YM","DOW_FUTURE")],
        "derivatives":{"equity_options":["XLI","DIA"],"index_options":["DJX"],"future_options":["YM"]},
    },
    "XLF": {
        "family":"DOW_CONTEXT","primary_type":"ETF",
        "related_equities":[_eq("DIA","DOW_REFERENCE_ETF"),_eq("XLI","PEER_CONFIRMATION")],
        "indices":[_idx("DJX","DOW_INDEX")],"futures":[_fut("YM","DOW_FUTURE")],
        "derivatives":{"equity_options":["XLF","DIA"],"index_options":["DJX"],"future_options":["YM"]},
    },
    "VIX": {
        "family":"DOW_CONTEXT_VOLATILITY","primary_type":"INDEX",
        "related_equities":[_eq("DIA","DOW_REFERENCE_ETF",polarity=-1)],
        "indices":[_idx("VIX","BROAD_VOLATILITY_INDEX"),_idx("VXD","DOW_VOLATILITY_INDEX")],
        "futures":[_fut("YM","DOW_FUTURE")],
        "derivatives":{"equity_options":["DIA"],"index_options":[],"future_options":["YM"]},
    },
    "VXD": {
        "family":"DOW_VOLATILITY","primary_type":"INDEX",
        "related_equities":[_eq("DIA","DOW_REFERENCE_ETF",polarity=-1)],
        "indices":[_idx("VXD","DOW_VOLATILITY_INDEX"),_idx("DJX","DOW_INDEX"),_idx("VIX","BROAD_VOLATILITY_INDEX")],
        "futures":[_fut("YM","DOW_FUTURE")],
        "derivatives":{"equity_options":["DIA"],"index_options":["DJX"],"future_options":["YM"]},
    },
}

PROVIDER_COMPONENT_CAPABILITIES: dict[str, set[str]] = {
    "ALPACA": {"PRIMARY_EQUITY", "RELATED_EQUITY", "EQUITY_OPTIONS"},
    "TASTYTRADE": {"INDEX", "FUTURE", "INDEX_OPTIONS", "FUTURE_OPTIONS"},
    "QUANTDATA": {"EQUITY_OPTIONS_ANALYTICS", "OPTIONS_FLOW_ANALYTICS", "OPTIONS_HISTORY"},
    "OFFICIAL_MACRO": {"MACRO_CONTEXT"},
}


def ecosystem_for(symbol: str) -> dict[str, Any]:
    sym=str(symbol or "").upper().strip()
    if sym not in ASSET_ECOSYSTEMS:
        return {"symbol":sym,"family":"UNSUPPORTED","primary_type":"UNKNOWN","primary":{"symbol":sym,"role":"PRIMARY","instrument_type":"UNKNOWN"},"related_equities":[],"indices":[],"benchmarks":[],"futures":[],"derivatives":{"equity_options":[],"index_options":[],"future_options":[]}}
    base=deepcopy(ASSET_ECOSYSTEMS[sym])
    base["symbol"]=sym
    base["primary"]={"symbol":sym,"role":"PRIMARY","instrument_type":base.get("primary_type","UNKNOWN")}
    base.setdefault("benchmarks",[]); base.setdefault("related_equities",[]); base.setdefault("indices",[]); base.setdefault("futures",[])
    return base


def related_equity_symbols(symbol: str, *, include_primary: bool = False) -> list[str]:
    eco=ecosystem_for(symbol)
    out=[str(x.get("symbol") or "").upper() for x in eco.get("related_equities",[]) if x.get("symbol")]
    if include_primary and eco.get("primary_type") not in {"INDEX","FUTURE","UNKNOWN"}: out.insert(0,str(symbol).upper())
    return list(dict.fromkeys(out))


def index_symbols(symbol: str) -> list[str]:
    return list(dict.fromkeys(str(x.get("symbol") or "").upper() for x in ecosystem_for(symbol).get("indices",[]) if x.get("symbol")))


def future_products(symbol: str, *, preferred_only: bool = False) -> list[str]:
    rows=ecosystem_for(symbol).get("futures",[])
    if preferred_only: rows=[x for x in rows if bool(x.get("preferred",False))]
    return list(dict.fromkeys(str(x.get("product") or "").upper() for x in rows if x.get("product")))


def derivative_roots(symbol: str) -> dict[str,list[str]]:
    raw=ecosystem_for(symbol).get("derivatives") or {}
    return {k:list(dict.fromkeys(str(x).upper() for x in (raw.get(k) or []) if x)) for k in ("equity_options","index_options","future_options")}


def component_descriptors(symbol: str) -> list[dict[str,Any]]:
    eco=ecosystem_for(symbol); out=[dict(eco["primary"])]
    for x in eco.get("related_equities",[]): out.append({"symbol":str(x.get("symbol") or "").upper(),"role":x.get("role") or "RELATED_EQUITY","instrument_type":"EQUITY","polarity":int(x.get("polarity") or 1)})
    for x in eco.get("indices",[]): out.append({"symbol":str(x.get("symbol") or "").upper(),"role":x.get("role") or "INDEX","instrument_type":"INDEX","aliases":list(x.get("aliases") or [])})
    for x in eco.get("futures",[]): out.append({"symbol":str(x.get("product") or "").upper(),"product":str(x.get("product") or "").upper(),"role":x.get("role") or "FUTURE","instrument_type":"FUTURE","preferred":bool(x.get("preferred",False))})
    return [x for x in out if x.get("symbol")]


def public_summary(symbol: str) -> dict[str,Any]:
    eco=ecosystem_for(symbol); d=derivative_roots(symbol)
    return {"symbol":eco["symbol"],"family":eco.get("family"),"primary_type":eco.get("primary_type"),"related_etfs_equities":related_equity_symbols(symbol),"indices":index_symbols(symbol),"futures":future_products(symbol),"derivatives":d,"architecture":"MULTI_ASSET · SEPARATE INSTRUMENT MATH","fusion_rule":"SEPARATE_INSTRUMENT_MATH_THEN_NORMALIZED_FEATURE_FUSION","provider_rule":"OBSERVATION_QUALITY_FRESHNESS_PROVENANCE"}


def all_unique_equity_roots() -> list[str]:
    out=[]
    for sym in ASSET_ECOSYSTEMS: out.extend(related_equity_symbols(sym,include_primary=True)); out.extend(derivative_roots(sym).get("equity_options",[]))
    return list(dict.fromkeys(x for x in out if x))


def all_unique_index_roots() -> list[str]:
    out=[]
    for sym in ASSET_ECOSYSTEMS: out.extend(index_symbols(sym)); out.extend(derivative_roots(sym).get("index_options",[]))
    return list(dict.fromkeys(x for x in out if x))


def all_unique_future_products() -> list[str]:
    out=[]
    for sym in ASSET_ECOSYSTEMS: out.extend(future_products(sym)); out.extend(derivative_roots(sym).get("future_options",[]))
    return list(dict.fromkeys(x for x in out if x))
