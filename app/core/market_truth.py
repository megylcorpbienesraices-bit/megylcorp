"""Provider-neutral Market Truth Engine.

Market Truth is not a prediction engine. It answers a narrower question: what market
state is defensible *right now* from all currently observed providers, with provenance,
freshness, latency and disagreement visible per observation. Provider names never carry
a permanent rank.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import math
import statistics

from .provider_bus import PROVIDER_BUS, FEATURE_BUS
from .source_arbitration import score_source
from .temporal_causality import CAUSAL_RUNTIME
from .temporal_truth import TEMPORAL_TRUTH
from .event_time import parse_utc


def _f(v: Any) -> float|None:
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _ts(v: Any, fallback: Any=None) -> datetime | None:
    return parse_utc(v) or parse_utc(fallback)



class MarketTruthEngine:
    def snapshot(self, symbol: str) -> dict[str,Any]:
        sym=str(symbol or '').upper()
        now=datetime.now(timezone.utc)
        raw=PROVIDER_BUS.latest_events(sym)
        pricing=PROVIDER_BUS.snapshot(sym)
        features=FEATURE_BUS.snapshot(sym)
        mids=[]
        for r in raw:
            vals=r.get('values') or {}; bid=_f(vals.get('bid'));ask=_f(vals.get('ask'));px=_f(vals.get('price'))
            mid=(bid+ask)/2 if bid is not None and ask is not None and ask>=bid else px
            if mid is not None:mids.append(mid)
        med=statistics.median(mids) if mids else None
        observations=[]
        for r in raw:
            vals=r.get('values') or {};ts=_ts(r.get('timestamp'),r.get('received_at')) or now; age=max(0.0,(now-ts).total_seconds()*1000)
            bid=_f(vals.get('bid'));ask=_f(vals.get('ask'));px=_f(vals.get('price'))
            mid=(bid+ask)/2 if bid is not None and ask is not None and ask>=bid else px
            div=0.0 if med in (None,0) or mid is None else abs(mid-med)/abs(med)
            completeness=sum(1 for k in ('price','bid','ask','prev_close','day_open','day_volume') if vals.get(k) is not None)/6.0
            q=score_source({"source":r.get('source'),"channel":r.get('event_type'),"status":"LIVE" if age<=5000 else "STALE",
                "latency_ms":r.get('latency_ms'),"age_ms":age,"gap_rate":0.0,
                "sequence_ok":r.get('sequence_ok'),"cross_source_divergence":div,
                "event_time_valid":r.get('event_time_valid'),"event_time_source":r.get('event_time_source')})
            observations.append({
                "source":r.get('source'),"event_type":r.get('event_type'),"timestamp":r.get('timestamp'),
                "received_at":r.get('received_at'),"age_ms":round(age,2),"latency_ms":round(float(r.get('latency_ms',0) or 0),3),
                "sequence_ok":r.get('sequence_ok') is True,"event_time_valid":bool(r.get('event_time_valid',True)),"event_time_source":r.get('event_time_source'),
                "mid":mid,"divergence_pct":round(div*100,5),
                "field_completeness_pct":round(completeness*100,1),"quality_score":q['quality_score'],"quality_label":q['quality_label'],
            })
        usable=[o for o in observations if o['quality_score']>=50 and o['age_ms']<=5000 and o.get('event_time_valid') is True]
        provider_diversity=len({str(o.get('source')) for o in usable})
        avgq=sum(float(o['quality_score']) for o in usable)/len(usable) if usable else 0.0
        diversity_bonus=min(8.0,max(0,provider_diversity-1)*3.0)
        truth_conf=max(0.0,min(100.0,avgq+diversity_bonus))
        disagreement=max([float(o['divergence_pct']) for o in usable] or [0.0])
        return {
            "ready":bool(usable or features.get('ready')),"symbol":sym,"asof":now.isoformat(),
            "composite":{"price":pricing.get('consensus_price'),"bid":pricing.get('composite_bid'),"ask":pricing.get('composite_ask'),
                         "pricing_confidence":pricing.get('confidence'),"usable_providers":pricing.get('usable_providers') or []},
            "truth_confidence":round(truth_conf,2),"provider_diversity":provider_diversity,
            "max_price_disagreement_pct":round(disagreement,5),"observations":observations,
            "feature_state":features,"causality":CAUSAL_RUNTIME.status(sym),"temporal_truth":TEMPORAL_TRUTH.snapshot(sym),
            "policy":"NO_FIXED_PROVIDER_RANK · QUALITY_BY_OBSERVATION · COMPARABLE_FIELDS_ONLY · TEMPORAL_ELIGIBILITY",
            "authority":"MARKET_STATE_EVIDENCE_ONLY_SCANNER_REMAINS_DIRECTION_AUTHORITY",
        }


MARKET_TRUTH=MarketTruthEngine()
