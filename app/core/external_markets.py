"""Provider-neutral related-market context for ITM QUANT v1.26.0.

Comparable live provider observations enter through the UnifiedProviderBus. Cross-
instrument futures/index values remain separate until explicitly normalized; related
ETF breadth is still enriched by the existing source-fusion layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def external_market_context(symbol: str, storage_dir: Path) -> Dict[str, Any]:
    """Provider-neutral related-market context without reviving removed legacy bridges.

    Historical ``futures`` / ``direct_index`` keys remain intentionally REMOVED so
    old CSV/proxy bridge semantics cannot silently return.  New live observations
    arrive through ``provider_futures`` / ``provider_index`` from UnifiedProviderBus.
    They are context only until instrument multipliers/volatility/exposures are
    normalized by the feature layer.
    """
    del storage_dir
    from .provider_bus import PROVIDER_BUS
    sym = str(symbol).upper()
    fut = PROVIDER_BUS.snapshot(f"{sym}:FUTURE")
    idx = PROVIDER_BUS.snapshot(f"{sym}:INDEX")

    provider_futures: Dict[str, Any] = {}
    if fut.get("ready"):
        provider_futures = {
            "symbol": f"{sym}:FUTURE", "valid": True,
            "price": fut.get("consensus_price"), "bid": fut.get("composite_bid"),
            "ask": fut.get("composite_ask"), "confidence": fut.get("confidence"),
            "sources": fut.get("usable_providers"), "source": "UNIFIED PROVIDER BUS",
            "normalization_required": True,
        }

    provider_index: Dict[str, Any] = {}
    if idx.get("ready"):
        provider_index = {
            "symbol": f"{sym}:INDEX", "valid": True,
            "price": idx.get("consensus_price"), "bid": idx.get("composite_bid"),
            "ask": idx.get("composite_ask"), "confidence": idx.get("confidence"),
            "sources": idx.get("usable_providers"), "source": "UNIFIED PROVIDER BUS",
            "normalization_required": True,
        }

    return {
        "symbol": sym,
        # Backward-compatible tombstones for the removed file/proxy bridge.
        "futures": {}, "direct_index": {},
        "futures_status": "REMOVED", "index_status": "REMOVED", "bridge_path": None,
        # v1.26.0 provider-neutral observations.
        "provider_futures": provider_futures, "provider_index": provider_index,
        "provider_futures_status": "LIVE" if fut.get("ready") else "WAITING",
        "provider_index_status": "LIVE" if idx.get("ready") else "WAITING",
        "related": {},
        "note": "Legacy futures/index bridges remain removed. Live provider futures/index observations use UnifiedProviderBus and require cross-instrument normalization before feature fusion.",
    }


def provider_redundancy(meta: Dict[str,Any], price_stream: Dict[str,Any], option_stream: Dict[str,Any], external: Dict[str,Any], storage_dir: Path) -> Dict[str,Any]:
    """Provider-neutral source health.

    Legacy response keys are retained for UI/test compatibility, but no provider is
    labeled primary/secondary.  Comparable live prices are evaluated by the shared
    quality-aware provider bus; structural OPRA availability remains separately visible.
    """
    del external, storage_dir
    from .provider_bus import PROVIDER_BUS
    sym = str(meta.get("symbol") or price_stream.get("symbol") or "").upper()
    consensus = PROVIDER_BUS.snapshot(sym) if sym else {"ready":False,"providers":[],"usable_providers":[]}
    sources = [
        {"name":"ALPACA SIP","status":"OK" if price_stream.get("connected") or meta.get("stock_feed") not in {None,"DEMO"} else "WAITING","detail":meta.get("stock_feed")},
        {"name":"ALPACA OPRA REST","status":"OK" if int(meta.get("matched_snapshots",0) or 0)>0 else "WAITING","detail":f"{meta.get('matched_snapshots',0)} snapshots"},
        {"name":"ALPACA OPRA WS","status":"OK" if option_stream.get("connected") else "DEGRADED" if option_stream.get("last_error") else "WAITING","detail":option_stream.get("last_error") or f"{option_stream.get('contracts',0)} contracts"},
    ]
    # Add provider-bus observations without assigning a fixed hierarchy.
    seen={str(x.get("name") or "").upper() for x in sources}
    for row in consensus.get("providers",[]) or []:
        name=str(row.get("source") or "UNKNOWN").upper()
        if name in seen or name=="ALPACA_SIP":
            continue
        q=row.get("quality") or {}; age=float(row.get("age_ms") or 0)
        status="OK" if q.get("quality_score",0)>=75 and age<=5000 else "DEGRADED" if q.get("quality_score",0)>=50 else "WAITING"
        sources.append({"name":name,"status":status,"detail":f"quality {q.get('quality_score',0):.0f}/100 · age {age:.0f} ms"})
        seen.add(name)
    critical = sum(1 for x in sources if x["status"] in {"DEGRADED","WAITING"})
    usable=list(consensus.get("usable_providers") or [])
    # Divergence is descriptive.  The bus already rejects stale/poor observations before
    # computing consensus, so disagreement cannot silently contaminate the engine.
    divs=[float(x.get("divergence") or 0.0) for x in (consensus.get("providers") or []) if x.get("mid") is not None]
    max_div=max(divs) if divs else 0.0
    disagreement=bool(len(usable)>=2 and max_div>0.0015)  # >15 bp among comparable observations
    components = [
        {"component":"DATA SERVICE","status":"OK" if consensus.get("ready") or price_stream.get("connected") else "DEGRADED","detail":"Provider-neutral bus + SIP/OPRA ingestion + strict JSON sanitation"},
        {"component":"LIVE ENGINE","status":"OK" if option_stream.get("connected") or int(meta.get("matched_snapshots",0) or 0)>0 else "WAITING","detail":"Event-driven live feeds with structural snapshot fallback"},
        {"component":"QUANT ENGINE","status":"OK","detail":"Gamma/Delta/Greeks/Scanner isolated from UI rendering"},
        {"component":"RESEARCH ENGINE","status":"OK","detail":"SQLite/WAL + CSV session histories; no DEMO efficacy contamination"},
        {"component":"FRONTEND","status":"OK","detail":"FastAPI state/charts/tables separated from model source layers"},
    ]
    return {
        "sources": sources, "components": components,
        # Legacy compatibility only. Semantically this means more than one live provider,
        # not a provider rank.
        "secondary_configured": len(usable) > 1,
        "spot_disagreement_pct": round(max_div*100.0,5) if divs else None,
        "data_disagreement": disagreement,
        "primary_critical_degraded": critical,
        "provider_consensus": consensus,
        "provider_count": len(consensus.get("providers") or []),
        "usable_provider_count": len(usable),
        "provider_policy": "EQUAL_OPPORTUNITY_QUALITY_DYNAMIC_NO_FIXED_RANK",
        "note": "Providers have no fixed primary/secondary rank. Freshness, latency, sequence integrity and divergence determine whether a comparable observation is usable.",
    }
