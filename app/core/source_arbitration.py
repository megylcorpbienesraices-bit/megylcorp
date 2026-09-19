"""Observation-quality arbitration for multi-provider ITM QUANT.

v1.27.12 keeps provider neutrality but removes the one-size-fits-all quality clock.
Policies are selected by DATA CHANNEL (trade, quote, option chain, futures, OI, macro)
and switching uses hysteresis so near-equal feeds do not flap every refresh.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable
import math
import os
import time


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x=float(v); return x if math.isfinite(x) else default
    except Exception:
        return default


# Scales are saturation points, not fixed provider ranks. A provider still wins only
# from its current observation quality inside the relevant channel.
_CHANNEL_POLICIES: dict[str, dict[str,float]] = {
    "DEFAULT":      {"latency_ms":500.0, "freshness_ms":5_000.0, "divergence":0.010, "w_latency":.27,"w_fresh":.27,"w_gaps":.18,"w_seq":.13,"w_div":.10,"w_live":.05},
    "EQUITY_TRADE": {"latency_ms":250.0, "freshness_ms":1_500.0, "divergence":0.004, "w_latency":.30,"w_fresh":.30,"w_gaps":.16,"w_seq":.14,"w_div":.07,"w_live":.03},
    "EQUITY_QUOTE": {"latency_ms":300.0, "freshness_ms":2_000.0, "divergence":0.005, "w_latency":.28,"w_fresh":.30,"w_gaps":.16,"w_seq":.13,"w_div":.10,"w_live":.03},
    "OPTION_QUOTE": {"latency_ms":750.0, "freshness_ms":5_000.0, "divergence":0.020, "w_latency":.22,"w_fresh":.30,"w_gaps":.18,"w_seq":.12,"w_div":.13,"w_live":.05},
    "OPTION_CHAIN": {"latency_ms":1_500.0,"freshness_ms":15_000.0,"divergence":0.025,"w_latency":.14,"w_fresh":.30,"w_gaps":.20,"w_seq":.10,"w_div":.20,"w_live":.06},
    "FUTURES_TICK": {"latency_ms":200.0, "freshness_ms":1_000.0, "divergence":0.003, "w_latency":.32,"w_fresh":.30,"w_gaps":.16,"w_seq":.15,"w_div":.05,"w_live":.02},
    # OI is structural/EOD; millisecond latency is mostly irrelevant while session
    # freshness and provenance matter. 36h tolerates normal overnight publication.
    "OPEN_INTEREST":{"latency_ms":60_000.0,"freshness_ms":129_600_000.0,"divergence":0.050,"w_latency":.03,"w_fresh":.35,"w_gaps":.22,"w_seq":.08,"w_div":.24,"w_live":.08},
    "MACRO":        {"latency_ms":300_000.0,"freshness_ms":86_400_000.0,"divergence":0.050,"w_latency":.03,"w_fresh":.30,"w_gaps":.20,"w_seq":.07,"w_div":.30,"w_live":.10},
    # v1.41.0 · canales de opciones que antes caían en DEFAULT. Sin política propia,
    # un paquete de flujo competía con el reloj genérico y ningún proveedor podía
    # ganar el canal por sus propios méritos: todos quedaban igualados por abajo.
    "OPTION_FLOW":  {"latency_ms":1_200.0, "freshness_ms":8_000.0,  "divergence":0.030,"w_latency":.24,"w_fresh":.32,"w_gaps":.18,"w_seq":.12,"w_div":.09,"w_live":.05},
    "EXPOSURE":     {"latency_ms":5_000.0, "freshness_ms":45_000.0, "divergence":0.040,"w_latency":.10,"w_fresh":.34,"w_gaps":.20,"w_seq":.08,"w_div":.22,"w_live":.06},
    "IMPLIED_VOLATILITY":{"latency_ms":5_000.0,"freshness_ms":60_000.0,"divergence":0.035,"w_latency":.10,"w_fresh":.32,"w_gaps":.18,"w_seq":.10,"w_div":.24,"w_live":.06},
    # Dark pool y prints de equity se publican con retraso reglamentario: penalizar
    # latencia aquí descartaría datos correctos por llegar cuando deben llegar.
    "DARK_POOL":    {"latency_ms":30_000.0,"freshness_ms":300_000.0,"divergence":0.020,"w_latency":.06,"w_fresh":.34,"w_gaps":.22,"w_seq":.10,"w_div":.22,"w_live":.06},
    "EQUITY_PRINT": {"latency_ms":2_000.0, "freshness_ms":20_000.0, "divergence":0.008,"w_latency":.22,"w_fresh":.32,"w_gaps":.20,"w_seq":.14,"w_div":.08,"w_live":.04},
}


def _channel(row: Dict[str,Any]) -> str:
    raw=str(row.get("channel") or row.get("data_type") or row.get("event_type") or "DEFAULT").upper().strip()
    instrument=str(row.get("instrument_type") or row.get("asset_class") or "").upper().strip()
    # Generic QUOTE/TRADE labels are insufficient for policy selection: a YM quote
    # must never inherit an equity clock just because both packets are called QUOTE.
    if raw in {"QUOTE","TRADE","TICK"} and ("FUTURE" in instrument or instrument in {"YM","MYM"}):
        raw="FUTURES_TICK"
    elif raw=="QUOTE" and "OPTION" in instrument:
        raw="OPTION_QUOTE"
    elif raw in {"SUMMARY","OPEN_INTEREST"} and bool(row.get("open_interest_channel")):
        raw="OPEN_INTEREST"
    aliases={"TRADE":"EQUITY_TRADE","QUOTE":"EQUITY_QUOTE","OPTIONS":"OPTION_CHAIN","OPTION":"OPTION_QUOTE",
             "OI":"OPEN_INTEREST","OPENINTEREST":"OPEN_INTEREST","FUTURE":"FUTURES_TICK","FUTURES":"FUTURES_TICK",
             "FLOW":"OPTION_FLOW","OPTIONS_FLOW":"OPTION_FLOW","OPTION_ORDER_FLOW":"OPTION_FLOW",
             "NET_FLOW":"OPTION_FLOW","NET_DRIFT":"OPTION_FLOW","OPRA":"OPTION_FLOW",
             "GEX":"EXPOSURE","DEX":"EXPOSURE","VEX":"EXPOSURE","CHEX":"EXPOSURE","GREEK_EXPOSURE":"EXPOSURE",
             "IV":"IMPLIED_VOLATILITY","VOLATILITY":"IMPLIED_VOLATILITY","IV_RANK":"IMPLIED_VOLATILITY",
             "DARKPOOL":"DARK_POOL","DARK_POOL_LEVELS":"DARK_POOL",
             "PRINT":"EQUITY_PRINT","PRINTS":"EQUITY_PRINT","EQUITY_PRINTS":"EQUITY_PRINT"}
    raw=aliases.get(raw,raw)
    return raw if raw in _CHANNEL_POLICIES else "DEFAULT"


def channel_policy(channel: str | None) -> Dict[str,float]:
    key=str(channel or "DEFAULT").upper().strip()
    key=key if key in _CHANNEL_POLICIES else "DEFAULT"
    return dict(_CHANNEL_POLICIES[key])


def score_source(row: Dict[str, Any]) -> Dict[str, Any]:
    ch=_channel(row); p=_CHANNEL_POLICIES[ch]
    latency=max(0.0,_f(row.get("latency_ms"),p["latency_ms"]*2))
    freshness=max(0.0,_f(row.get("age_ms"),p["freshness_ms"]*2))
    gaps=max(0.0,_f(row.get("gap_rate"),1.0))
    # Missing quality telemetry is unknown, never perfect. The old defaults (0 divergence,
    # sequence_ok=True) rewarded a silent provider by 20-32 quality points.
    div_reported=row.get("cross_source_divergence") is not None
    seq_reported=row.get("sequence_ok") is not None
    divergence=max(0.0,_f(row.get("cross_source_divergence"),p["divergence"]))
    seq_ok=1.0 if row.get("sequence_ok") is True else 0.0
    live=1.0 if str(row.get("status","")).upper() in {"LIVE","ACTIVE","CONNECTED","GOOD"} else 0.0
    s_latency=max(0.0,1.0-min(latency/max(p["latency_ms"],1e-9),1.0))
    s_fresh=max(0.0,1.0-min(freshness/max(p["freshness_ms"],1e-9),1.0))
    s_gaps=max(0.0,1.0-min(gaps,1.0))
    s_div=max(0.0,1.0-min(divergence/max(p["divergence"],1e-12),1.0))
    score=100.0*(p["w_latency"]*s_latency+p["w_fresh"]*s_fresh+p["w_gaps"]*s_gaps+
                 p["w_seq"]*seq_ok+p["w_div"]*s_div+p["w_live"]*live)
    # Receive-time proxies are useful for liveness, but they are not exchange/provider
    # event time. Penalise rather than silently granting the same clock quality.
    time_valid=row.get("event_time_valid")
    time_source=str(row.get("event_time_source") or "").upper()
    if time_valid is False:
        score *= 0.65 if time_source=="RECEIVE_PROXY" else 0.0
    score=max(0.0,min(100.0,score))
    return {**row,"channel_policy":ch,"quality_score":round(score,2),
            "quality_label":"HIGH" if score>=90 else "GOOD" if score>=75 else "DEGRADED" if score>=50 else "POOR",
            "metrics_reported":f"{int(div_reported)+int(seq_reported)}/2",
            "event_time_quality":"VALID" if time_valid is not False else time_source or "INVALID"}



def arbitrate_scored(sources: Iterable[Dict[str, Any]], previous: str | None = None,
                     *, previous_selected_at: float | None = None, now: float | None = None,
                     switch_margin: float | None = None, min_dwell_ms: float | None = None) -> Dict[str, Any]:
    """Hysteresis for lanes whose domain-specific quality was already computed.

    This lets option-flow/price fabrics share the same switching discipline without
    forcing their richer domain metrics through the generic latency/divergence formula.
    """
    ranked=sorted((dict(x) for x in sources if x),
                  key=lambda r:(-float(r.get("quality_score") or 0.0),str(r.get("name") or r.get("source") or "")))
    best=ranked[0] if ranked else None
    candidate=str((best or {}).get("name") or (best or {}).get("source") or "") or None
    selected=candidate; switch_reason="BEST_QUALITY" if candidate else "NO_SOURCE"
    margin=float(os.getenv("ITM_SOURCE_SWITCH_MARGIN","2.5")) if switch_margin is None else float(switch_margin)
    dwell=float(os.getenv("ITM_SOURCE_MIN_DWELL_MS","1500")) if min_dwell_ms is None else float(min_dwell_ms)
    by_name={str(r.get("name") or r.get("source") or ""):r for r in ranked}
    prev=by_name.get(str(previous or "")); improvement=None
    if previous and prev is not None and candidate and candidate!=previous:
        improvement=float((best or {}).get("quality_score") or 0.0)-float(prev.get("quality_score") or 0.0)
        now_s=time.time() if now is None else float(now)
        dwell_ok=True if previous_selected_at is None else (now_s-float(previous_selected_at))*1000.0>=max(0.0,dwell)
        prev_usable=float(prev.get("quality_score") or 0.0)>=25.0
        if prev_usable and (improvement<max(0.0,margin) or not dwell_ok):
            selected=str(previous);switch_reason="HYSTERESIS_RETAIN_PREVIOUS"
        else:
            switch_reason="QUALITY_IMPROVEMENT_EXCEEDS_HYSTERESIS"
    return {"ready":bool(ranked),"selected":selected,"candidate":candidate,"previous":previous,
            "switched":bool(selected and previous and selected!=previous),"switch_reason":switch_reason,
            "candidate_improvement":improvement,"switch_margin":max(0.0,margin),"min_dwell_ms":max(0.0,dwell),
            "ranked":ranked,"authority":"SOURCE_SELECTION_ONLY"}

def arbitrate(sources: Iterable[Dict[str, Any]], previous: str | None = None,
              *, previous_selected_at: float | None = None, now: float | None = None,
              switch_margin: float | None = None, min_dwell_ms: float | None = None) -> Dict[str, Any]:
    ranked=sorted((score_source(dict(x)) for x in sources if x),
                  key=lambda r:(-r["quality_score"],str(r.get("name") or r.get("source") or "")))
    best=ranked[0] if ranked else None
    candidate=str((best or {}).get("name") or (best or {}).get("source") or "") or None
    selected=candidate; switch_reason="BEST_QUALITY" if candidate else "NO_SOURCE"
    margin=float(os.getenv("ITM_SOURCE_SWITCH_MARGIN","2.5")) if switch_margin is None else float(switch_margin)
    dwell=float(os.getenv("ITM_SOURCE_MIN_DWELL_MS","1500")) if min_dwell_ms is None else float(min_dwell_ms)
    by_name={str(r.get("name") or r.get("source") or ""):r for r in ranked}
    prev=by_name.get(str(previous or ""))
    improvement=None
    if previous and prev is not None and candidate and candidate!=previous:
        improvement=float(best["quality_score"]-prev["quality_score"])
        now_s=time.time() if now is None else float(now)
        dwell_ok=True if previous_selected_at is None else (now_s-float(previous_selected_at))*1000.0>=max(0.0,dwell)
        margin_ok=improvement>=max(0.0,margin)
        # A catastrophically bad previous feed is never protected by hysteresis.
        prev_usable=float(prev["quality_score"])>=25.0
        if prev_usable and (not margin_ok or not dwell_ok):
            selected=str(previous)
            switch_reason="HYSTERESIS_RETAIN_PREVIOUS"
        else:
            switch_reason="QUALITY_IMPROVEMENT_EXCEEDS_HYSTERESIS"
    switched=bool(selected and previous and selected!=previous)
    return {"ready":bool(ranked),"selected":selected,"candidate":candidate,"previous":previous,
            "switched":switched,"switch_reason":switch_reason,"candidate_improvement":improvement,
            "switch_margin":max(0.0,margin),"min_dwell_ms":max(0.0,dwell),"ranked":ranked,
            "authority":"SOURCE_SELECTION_ONLY",
            "note":"Channel-specific observation-quality selection with hysteresis; no fixed provider hierarchy and no synthesized market data."}
