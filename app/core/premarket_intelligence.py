"""Premarket intelligence synthesis for ITM QUANT.

The engine intentionally keeps the visible report compact: it scans the full
selected option universe, but exposes only the nearby strikes and situations
that can change a trading decision.  Every asset is reported in its own native price. Related markets are contextual
confirmation inputs and are never converted into synthetic levels of another instrument.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import math
from typing import Any, Dict

import numpy as np
import pandas as pd
from .frame_guards import numeric_column

from .trace_analytics import max_pain as _max_pain, structural_walls as _structural_walls

EC = ZoneInfo("America/Guayaquil")


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _pct_rank(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce").fillna(0.0)
    if len(x) <= 1:
        return pd.Series(np.ones(len(x)), index=x.index)
    return x.rank(method="average", pct=True)


def _latest_chain(result: Dict[str, Any]) -> pd.DataFrame:
    x = result.get("enriched", pd.DataFrame()) if isinstance(result, dict) else pd.DataFrame()
    if not isinstance(x, pd.DataFrame) or x.empty:
        return pd.DataFrame()
    x = x.copy()
    x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
    x = x.dropna(subset=["timestamp"])
    if x.empty:
        return x
    return x[x["timestamp"] == x["timestamp"].max()].copy()


def _aggregate_strikes(result: Dict[str, Any]) -> tuple[pd.DataFrame, Dict[str, float]]:
    x = _latest_chain(result)
    if x.empty:
        return pd.DataFrame(), {}
    for c in ["strike", "open_interest", "volume", "signed_gex_proxy", "gross_gex", "option_delta_exposure_info", "iv"]:
        x[c] = pd.to_numeric(x.get(c), errors="coerce").fillna(0.0)
    typ = x.get("option_type", pd.Series([""] * len(x))).astype(str).str.lower()
    x["is_call"] = typ.str.startswith("c")
    x["call_oi"] = np.where(x["is_call"], x["open_interest"], 0.0)
    x["put_oi"] = np.where(~x["is_call"], x["open_interest"], 0.0)
    x["call_volume"] = np.where(x["is_call"], x["volume"], 0.0)
    x["put_volume"] = np.where(~x["is_call"], x["volume"], 0.0)
    x["call_gex"] = np.where(x["is_call"], x["signed_gex_proxy"], 0.0)
    x["put_gex"] = np.where(~x["is_call"], x["signed_gex_proxy"], 0.0)
    x["call_delta"] = np.where(x["is_call"], x["option_delta_exposure_info"], 0.0)
    x["put_delta"] = np.where(~x["is_call"], x["option_delta_exposure_info"], 0.0)
    agg = x.groupby("strike", as_index=False).agg(
        call_oi=("call_oi", "sum"), put_oi=("put_oi", "sum"), total_oi=("open_interest", "sum"),
        call_volume=("call_volume", "sum"), put_volume=("put_volume", "sum"), total_volume=("volume", "sum"),
        call_gex=("call_gex", "sum"), put_gex=("put_gex", "sum"), net_gex=("signed_gex_proxy", "sum"), gross_gex=("gross_gex", "sum"),
        call_delta=("call_delta", "sum"), put_delta=("put_delta", "sum"), net_delta=("option_delta_exposure_info", "sum"),
        avg_iv=("iv", "mean"), expiry_count=("expiration_date", "nunique"),
    )
    abs_delta = x.assign(abs_delta=x["option_delta_exposure_info"].abs()).groupby("strike", as_index=False)["abs_delta"].sum()
    agg = agg.merge(abs_delta, on="strike", how="left")
    current = result.get("current_delta", pd.DataFrame())
    if isinstance(current, pd.DataFrame) and not current.empty:
        cols = [c for c in ["strike", "gamma_delta_level_score", "state_delta", "state_confidence_delta", "turnover", "turnover_raw"] if c in current.columns]
        agg = agg.merge(current[cols].drop_duplicates("strike"), on="strike", how="left")
    totals = {
        "call_oi": float(x["call_oi"].sum()), "put_oi": float(x["put_oi"].sum()), "total_oi": float(x["open_interest"].sum()),
        "call_volume": float(x["call_volume"].sum()), "put_volume": float(x["put_volume"].sum()), "total_volume": float(x["volume"].sum()),
        "net_gex": float(x["signed_gex_proxy"].sum()), "gross_gex": float(x["gross_gex"].sum()),
        "net_delta": float(x["option_delta_exposure_info"].sum()), "gross_delta": float(x["option_delta_exposure_info"].abs().sum()),
        "expiries": int(x.get("expiration_date", pd.Series(dtype=str)).astype(str).nunique()),
    }
    return agg, totals


def _joint_reading(strike: float, spot: float, gex: float, delta: float, state: str) -> tuple[str, str, str]:
    g = "POSITIVA" if gex > 0 else "NEGATIVA" if gex < 0 else "NEUTRAL"
    d = "POSITIVA" if delta > 0 else "NEGATIVA" if delta < 0 else "NEUTRAL"
    below = strike <= spot
    st = str(state or "").upper()
    if gex > 0 and delta > 0:
        role = "SOPORTE / COMPRA" if below else "IMPULSO ALCISTA / IMÁN"
        joint = "COMPRA FUERTE" if below else "ALCISTA"
    elif gex < 0 and delta < 0:
        role = "ACELERACIÓN BAJISTA" if below else "RESISTENCIA / VENTA"
        joint = "VENTA FUERTE" if not below else "BAJISTA"
    elif gex > 0 and delta < 0:
        role = "CONTENCIÓN CON DELTA VENDEDORA"
        joint = "MIXTO"
    elif gex < 0 and delta > 0:
        role = "RUPTURA / CONVEXIDAD INESTABLE"
        joint = "MIXTO"
    else:
        role = "NIVEL DE TRANSICIÓN"
        joint = "NEUTRAL"
    if st == "BREAK" and "RUPTURA" not in role and abs(gex) > 0:
        role += " · RIESGO BREAK"
    return g, d, f"{joint} · {role}"


def _nearby_rank(agg: pd.DataFrame, spot: float, expected_move: float | None, count: int = 8) -> pd.DataFrame:
    if agg.empty:
        return agg
    z = agg.copy().sort_values("strike")
    strikes = np.sort(z["strike"].dropna().unique())
    step = float(np.median(np.diff(strikes))) if len(strikes) > 1 else max(abs(spot) * 0.002, 0.5)
    em = _f(expected_move, 0.0) or 0.0
    radius = min(max(4.0 * step, 1.15 * em), 7.0 * step)
    z["distance"] = (z["strike"] - spot).abs()
    near = z[z["distance"] <= radius + 1e-9].copy()
    # Always provide 6-8 strikes if the chain has them, but never jump to a far strike
    # solely because its OI is enormous.
    if len(near) < min(6, len(z)):
        near = z.sort_values("distance").head(min(max(6, count), len(z))).copy()
    # Importance only competes inside the nearby pool.
    near["importance_score"] = 100.0 * (
        0.28 * _pct_rank(near["total_oi"]) +
        0.17 * _pct_rank(near["total_volume"]) +
        0.22 * _pct_rank(near["gross_gex"]) +
        0.13 * _pct_rank(near["abs_delta"]) +
        0.15 * _pct_rank(numeric_column(near,"gamma_delta_level_score",0.0)) +
        0.05 * (1.0 - np.minimum(near["distance"] / max(radius, step), 1.0))
    )
    chosen = near.sort_values(["importance_score", "distance"], ascending=[False, True]).head(min(count, len(near))).copy()
    return chosen.sort_values("strike").reset_index(drop=True)


def _concentrations(agg: pd.DataFrame) -> Dict[str, Any]:
    if agg.empty:
        return {}
    def top(col: str, ascending: bool = False) -> Dict[str, float] | None:
        x = agg.sort_values(col, ascending=ascending).iloc[0]
        return {"strike": float(x["strike"]), "value": float(x[col])}
    posg = agg[agg["net_gex"] > 0]
    negg = agg[agg["net_gex"] < 0]
    posd = agg[agg["net_delta"] > 0]
    negd = agg[agg["net_delta"] < 0]
    stab = max(float(agg["total_oi"].quantile(0.25)), 1.0)
    tmp = agg.assign(activity=agg["total_volume"] / (agg["total_oi"] + stab))
    return {
        "max_total_oi": top("total_oi"), "max_call_oi": top("call_oi"), "max_put_oi": top("put_oi"),
        "max_positive_gamma": None if posg.empty else {"strike": float(posg.sort_values("net_gex", ascending=False).iloc[0]["strike"]), "value": float(posg["net_gex"].max())},
        "max_negative_gamma": None if negg.empty else {"strike": float(negg.sort_values("net_gex").iloc[0]["strike"]), "value": float(negg["net_gex"].min())},
        "max_positive_delta": None if posd.empty else {"strike": float(posd.sort_values("net_delta", ascending=False).iloc[0]["strike"]), "value": float(posd["net_delta"].max())},
        "max_negative_delta": None if negd.empty else {"strike": float(negd.sort_values("net_delta").iloc[0]["strike"]), "value": float(negd["net_delta"].min())},
        "max_activity": {"strike": float(tmp.sort_values("activity", ascending=False).iloc[0]["strike"]), "value": float(tmp["activity"].max()), "volume": float(tmp.sort_values("activity", ascending=False).iloc[0]["total_volume"]), "oi": float(tmp.sort_values("activity", ascending=False).iloc[0]["total_oi"])},
    }


def _bridge_confirmation(name: str, item: Dict[str, Any] | None, direction: str, inverse: bool = False) -> Dict[str, Any]:
    if not item or not item.get("valid"):
        hints={
            "XLI":"XLI se consulta por Alpaca SIP cuando las credenciales LIVE están configuradas.",
            "XLF":"XLF se consulta por Alpaca SIP cuando las credenciales LIVE están configuradas.",
        }
        return {"name": name, "status": "SIN DATO", "score": None, "basis": hints.get(name,"Fuente no conectada o dato no válido; no pesa en la confirmación."), "values": {}}
    ch = _f(item.get("change_pct"))
    px = _f(item.get("price")); vwap = _f(item.get("vwap")); vol = _f(item.get("volume")); oi = _f(item.get("open_interest"))
    wanted = 1 if direction == "BUY" else -1 if direction == "SELL" else 0
    if inverse:
        wanted *= -1
    signals = []
    score = 50.0
    if ch is not None:
        aligned = (ch > 0 and wanted > 0) or (ch < 0 and wanted < 0) or (wanted == 0 and abs(ch) < 0.15)
        score += 24.0 if aligned else -24.0
        signals.append(f"cambio {ch:+.2f}%")
    if px is not None and vwap is not None:
        aligned = (px >= vwap and wanted > 0) or (px <= vwap and wanted < 0)
        score += 16.0 if aligned else -16.0
        signals.append(f"precio {px:.2f} vs VWAP {vwap:.2f}")
    if ch is None and vwap is None:
        return {"name": name, "status": "DATO PARCIAL", "score": None, "basis": "Hay precio, pero faltan cambio/VWAP para inferir confirmación direccional sin inventar.", "values": {"price": px, "volume": vol, "open_interest": oi}}
    score = float(np.clip(score, 0, 100))
    status = "CONFIRMA" if score >= 62 else "CONTRADICE" if score <= 38 else "NEUTRAL"
    return {"name": name, "status": status, "score": round(score, 1), "basis": " · ".join(signals), "values": {"price": px, "change_pct": ch, "vwap": vwap, "volume": vol, "open_interest": oi, "source": item.get("source")}}


def _confirmations(direction: str, external: Dict[str, Any], dealer: Dict[str, Any], flow: Dict[str, Any], market_state: str = "", symbol: str = "UNKNOWN") -> list[Dict[str, Any]]:
    out = []
    # Native option-flow confirmation for the active symbol, only when actual premarket/live flow exists.
    freg=str((flow or {}).get("regime","WAITING")).upper(); fconf=_f((flow or {}).get("confidence"),0.0) or 0.0; fnet=_f((flow or {}).get("net"),0.0) or 0.0
    if freg not in {"WAITING","MIXED","NEUTRAL",""} and fconf>0:
        fdir="BUY" if freg in {"BUY","BULLISH","BULL"} or fnet>0 else "SELL" if freg in {"SELL","BEARISH","BEAR"} or fnet<0 else "NEUTRAL"
        same=fdir==direction
        out.append({"name":f"Flujo opciones {symbol}","status":"CONFIRMA" if same else "CONTRADICE" if fdir in {"BUY","SELL"} else "NEUTRAL","score":round(fconf if same else 100-fconf,1),"basis":f"Flujo neto {fnet:+.0f} · régimen {freg} · confianza {fconf:.0f}/100","values":{"net_flow":fnet,"confidence":fconf}})
    else:
        if str(market_state).upper()=="PREMARKET":
            out.append({"name":f"Flujo opciones {symbol}","status":"NO APLICA PREMARKET","score":None,"basis":"Antes de la apertura regular no se exige flujo OPRA del activo actual; esta capa se activa en LIVE y no se cuenta como dato faltante del premarket.","values":{}})
        else:
            out.append({"name":f"Flujo opciones {symbol}","status":"ESPERANDO","score":None,"basis":"Sin flujo live suficiente; no pesa todavía.","values":{}})
    # Related breadth is one combined contextual family, regardless of ticker. The actual
    # related symbols come from the asset ecosystem metadata; no family gets a special vote.
    rel = external.get("related") or {}
    related_rows=[]
    for name,val in rel.items():
        c=_bridge_confirmation(str(name),val,direction,False)
        if c.get("score") is not None: related_rows.append((str(name),c))
    if related_rows:
        vals=[float(c["score"]) for _,c in related_rows if c.get("score") is not None]
        sc=float(np.mean(vals)); status="CONFIRMA" if sc>=62 else "CONTRADICE" if sc<=38 else "NEUTRAL"
        basis=" · ".join(f"{name} {c['status']}" for name,c in related_rows)
        out.append({"name":"Mercados relacionados","status":status,"score":round(sc,1),"basis":basis,"values":{name:c.get("values") for name,c in related_rows}})
    else:
        out.append({"name":"Mercados relacionados","status":"SIN DATO","score":None,"basis":"Ecosistema relacionado sin datos utilizables; no pesa.","values":{}})
    # Dealer/Hedge is an internal inferred confirmation and remains explicit.
    hp = (dealer or {}).get("hedge_pressure") or {}
    hp_dir = str(hp.get("direction", "NEUTRAL")).upper(); hp_conf = _f(hp.get("confidence"), 0.0) or 0.0
    if str((dealer or {}).get("state", "")).upper() not in {"", "COLLECTING"} and hp_conf > 0:
        same = hp_dir == direction
        out.append({"name": "Cobertura dealer", "status": "CONFIRMA" if same else "CONTRADICE" if hp_dir in {"BUY", "SELL"} else "NEUTRAL", "score": round(hp_conf if same else 100-hp_conf,1), "basis": f"Hedge Pressure {hp_dir} · confianza {hp_conf:.0f}/100", "values": {"net_15m": hp.get("net_15m")}, "inferred": True})
    else:
        if str(market_state).upper()=="PREMARKET":
            out.append({"name": "Cobertura dealer", "status": "NO APLICA PREMARKET", "score": None, "basis": "Dealer/Hedge necesita prints OPRA LIVE de la sesión; antes de 9:30 NY queda fuera del voto premarket y se activa después de la apertura.", "values": {}, "inferred": True})
        else:
            out.append({"name": "Cobertura dealer", "status": "ESPERANDO", "score": None, "basis": "Sin flujo OPRA LIVE suficiente; no se fabrica confirmación dealer.", "values": {}, "inferred": True})
    return out


def _situations(agg: pd.DataFrame, totals: Dict[str, float], volatility: Dict[str, Any], expiry: Dict[str, Any], dealer: Dict[str, Any], data_quality: Dict[str, Any]) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    gross = max(float(totals.get("gross_gex", 0.0) or 0.0), 1.0); net = float(totals.get("net_gex", 0.0) or 0.0)
    tilt = abs(net) / gross
    if net < 0 and tilt >= 0.05:
        out.append({"title":"Gamma neta negativa relevante","severity":"ALTA" if tilt>=0.12 else "MEDIA","metrics":{"net_gex":net,"gross_gex":gross,"net_to_gross_pct":tilt*100},"reading":"Mayor sensibilidad a aceleraciones/rupturas; no implica venta por sí sola."})
    if not agg.empty:
        r=agg.sort_values("total_oi",ascending=False).iloc[0]; share=float(r["total_oi"])/max(float(totals.get("total_oi",0) or 0),1.0)
        if share >= 0.12:
            out.append({"title":f"Concentración fuerte de OI en {float(r['strike']):.2f}","severity":"ALTA" if share>=.18 else "MEDIA","metrics":{"call_oi":float(r['call_oi']),"put_oi":float(r['put_oi']),"oi_total":float(r['total_oi']),"share_pct":share*100},"reading":"Strike con masa estructural superior al resto; puede actuar como defensa, imán o zona de decisión según Gamma+Delta."})
        stab=max(float(agg["total_oi"].quantile(.25)),1.0); a=agg.assign(activity=agg["total_volume"]/(agg["total_oi"]+stab)).sort_values("activity",ascending=False).iloc[0]
        if float(a["activity"]) >= .45:
            out.append({"title":f"Actividad relativa elevada en {float(a['strike']):.2f}","severity":"MEDIA","metrics":{"volume_total":float(a['total_volume']),"oi_total":float(a['total_oi']),"activity_ratio":float(a['activity'])},"reading":"Volumen grande respecto a la masa existente; merece atención como posible posicionamiento nuevo, sin afirmar opening/closing."})
    ivrv=_f((volatility or {}).get("iv_minus_rv_pp"))
    if ivrv is not None and abs(ivrv)>=4:
        out.append({"title":"Desalineación IV vs volatilidad realizada","severity":"MEDIA","metrics":{"iv_minus_rv_pp":ivrv,"atm_iv_pct":_f(volatility.get("atm_iv")),"realized_vol_pct":_f(volatility.get("realized_volatility_pct"))},"reading":"Las opciones descuentan un movimiento materialmente distinto al observado; afecta Expected Move y economía de opciones."})
    if str((expiry or {}).get("status","")).upper() not in {"","FUERTE"}:
        out.append({"title":"Vencimientos no totalmente alineados","severity":"MEDIA","metrics":{"status":expiry.get("status")},"reading":"Los tramos disjuntos de vencimiento no señalan la misma concentración; reduce confianza del nivel."})
    rec=(dealer or {}).get("gex_reconciliation") or {}
    if str(rec.get("agreement","")).upper()=="CONFLICT" and float(rec.get("flow_inventory_coverage_pct",0) or 0)>=20:
        out.append({"title":"Structural GEX vs Dealer GEX en conflicto","severity":"MEDIA","metrics":{"structural_gex":rec.get("structural_gex"),"dealer_gex":rec.get("estimated_dealer_gex"),"coverage_pct":rec.get("flow_inventory_coverage_pct")},"reading":"Dos modelos diferentes discrepan con cobertura suficiente; requiere más cautela, no elegir uno por fuerza."})
    dq=_f((data_quality or {}).get("score"))
    if dq is not None and dq<70:
        out.append({"title":"Calidad de datos reducida","severity":"ALTA" if dq<55 else "MEDIA","metrics":{"data_quality":dq},"reading":"El motor conserva la lectura, pero la confianza debe reducirse por datos incompletos o degradados."})
    return out[:5]


def build_premarket_report(*, symbol: str, result: Dict[str, Any], positioning: Dict[str, Any], volatility: Dict[str, Any], scanner: Dict[str, Any], expiry_confluence: Dict[str, Any], flow: Dict[str, Any], dealer: Dict[str, Any], external: Dict[str, Any], source_health: Dict[str, Any], source_fusion: Dict[str, Any] | None = None, macro: Dict[str, Any], data_quality: Dict[str, Any], model_health: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    sym=str(symbol).upper(); spot=_f(result.get("spot"))
    if spot is None:
        return {"ready":False,"symbol":sym,"reason":"Sin spot válido"}
    agg, totals = _aggregate_strikes(result)
    if agg.empty:
        return {"ready":False,"symbol":sym,"reason":"Sin cadena de opciones seleccionada"}
    nearby = _nearby_rank(agg, spot, _f((volatility or {}).get("expected_move")), 8)
    rows=[]
    for _,r in nearby.iterrows():
        g,d,joint=_joint_reading(float(r["strike"]),spot,float(r["net_gex"]),float(r["net_delta"]),str(r.get("state_delta","")))
        raw_turn=_f(r.get("turnover_raw")); activity=raw_turn if raw_turn is not None else float(r["total_volume"])/max(float(r["total_oi"]),1.0)
        rows.append({
            "strike":float(r["strike"]),"distance":float(r["strike"]-spot),"importance_score":float(r.get("importance_score",0)),
            "call_oi":float(r["call_oi"]),"put_oi":float(r["put_oi"]),"total_oi":float(r["total_oi"]),
            "call_volume":float(r["call_volume"]),"put_volume":float(r["put_volume"]),"total_volume":float(r["total_volume"]),
            "call_gex":float(r["call_gex"]),"put_gex":float(r["put_gex"]),"net_gex":float(r["net_gex"]),"gross_gex":float(r["gross_gex"]),
            "call_delta":float(r["call_delta"]),"put_delta":float(r["put_delta"]),"net_delta":float(r["net_delta"]),"gross_delta":float(r.get("abs_delta",0)),
            "avg_iv_pct":float(r["avg_iv"])*100.0 if abs(float(r["avg_iv"]))<=3 else float(r["avg_iv"]),"activity_ratio":float(activity),"expiry_count":int(r.get("expiry_count",0) or 0),
            "gamma_reading":g,"delta_reading":d,"joint_reading":joint,"state":str(r.get("state_delta","—")),"level_score":_f(r.get("gamma_delta_level_score"),0.0),
        })
    direction=str(scanner.get("direction") or result.get("delta_pressure_direction") or result.get("pressure_direction") or "NEUTRAL").upper()
    confirmations=_confirmations(direction, external or {}, dealer or {}, flow or {}, (meta or {}).get("market_state",""), symbol=sym)
    scored=[c for c in confirmations if c.get("score") is not None and c.get("status") in {"CONFIRMA","CONTRADICE","NEUTRAL"}]
    conf_count=sum(1 for c in scored if c.get("status")=="CONFIRMA"); contra=sum(1 for c in scored if c.get("status")=="CONTRADICE")
    not_applicable=sum(1 for c in confirmations if c.get("status")=="NO APLICA PREMARKET")
    pending=sum(1 for c in confirmations if c.get("status") in {"SIN DATO","DATO PARCIAL","ESPERANDO"})
    conf_score=float(np.mean([float(c["score"]) for c in scored])) if scored else None
    conc=_concentrations(agg)
    maxpain=_max_pain(_latest_chain(result))
    # Una sola semántica de Wall en todo ITM QUANT: concentración Gamma por lado.
    # Los máximos de OI siguen siendo útiles, pero se publican con su nombre propio
    # para que nunca puedan contradecir visualmente a Call Wall / Put Wall.
    wall_frame = agg.copy()
    if not wall_frame.empty:
        wall_frame["signed_gex"] = numeric_column(wall_frame,"net_gex",0.0)
    walls = _structural_walls(wall_frame, spot)
    call_wall=walls.get("call_wall"); put_wall=walls.get("put_wall")
    max_call_oi=(conc.get("max_call_oi") or {}).get("strike")
    max_put_oi=(conc.get("max_put_oi") or {}).get("strike")
    zone=scanner.get("zone") or {}; main_zone={"low":_f(zone.get("low")),"high":_f(zone.get("high")),"center":_f(zone.get("center"))}
    alt=scanner.get("secondary") or {}
    evidence=_f(scanner.get("evidence_score"),0.0) or 0.0
    dq=_f((data_quality or {}).get("score"), _f(result.get("data_quality"),0.0)) or 0.0; mh=_f((model_health or {}).get("score"),0.0) or 0.0
    base_strength=float(np.clip(0.72*evidence + 0.14*dq + 0.14*mh,0,100))
    # External/related markets can strengthen/weaken confidence, but never create the active asset direction by themselves. The adjustment is capped at ±8 points and requires
    # at least two usable confirmations.
    confirmation_adjustment=0.0
    if len(scored) >= 2 and conf_score is not None:
        confirmation_adjustment=float(np.clip((conf_score-50.0)*0.16,-8.0,8.0))
    strength=float(np.clip(base_strength+confirmation_adjustment,0,100))
    key_level=main_zone.get("center") or (_f((conc.get("max_total_oi") or {}).get("strike")))
    dir_es="COMPRA" if direction=="BUY" else "VENTA" if direction=="SELL" else "MIXTO"
    keyrow=min(rows,key=lambda q:abs(float(q["strike"])-float(key_level))) if rows and key_level is not None else (rows[0] if rows else None)
    pieces=[]
    if keyrow:
        pieces.append(f"El nivel {keyrow['strike']:.2f} concentra {keyrow['total_oi']:.0f} contratos OI y {keyrow['total_volume']:.0f} de volumen; Gamma+Delta se lee {keyrow['joint_reading'].lower()}.")
    if conf_count or contra:
        pieces.append(f"De {len(scored)} confirmaciones externas/relacionadas utilizables, {conf_count} confirman y {contra} contradicen.")
    if (volatility or {}).get("expected_low") is not None and (volatility or {}).get("expected_high") is not None:
        pieces.append(f"El rango esperado del modelo es {float(volatility['expected_low']):.2f}–{float(volatility['expected_high']):.2f} {sym}.")
    if not pieces:
        pieces=["La conclusión resume la estructura de opciones disponible; las fuentes faltantes no se sustituyen con estimaciones ocultas."]
    report={
        "ready":True,"version":"1.14.8","symbol":sym,"native_price_only":True,"generated_at_ec":datetime.now(EC).isoformat(),"spot":spot,
        "summary":{"bias":dir_es,"bias_raw":direction,"strength":round(strength,1),"evidence":round(evidence,1),"data_quality":round(dq,1),"model_health":round(mh,1),"market_state":(meta or {}).get("market_state"),"regime":(scanner or {}).get("regime_context",{}).get("regime") or result.get("regime")},
        "chain_totals":{**totals,"put_call_oi_ratio":float(totals["put_oi"])/max(float(totals["call_oi"]),1.0),"put_call_volume_ratio":float(totals["put_volume"])/max(float(totals["call_volume"]),1.0)},
        "key_strikes":rows,"concentrations":conc,
        "structure":{"gamma_flip":_f(result.get("gamma_flip")),"max_pain":maxpain,
                     "call_wall":call_wall,"put_wall":put_wall,"wall_method":walls.get("method"),
                     "max_call_oi_strike":max_call_oi,"max_put_oi_strike":max_put_oi,
                     # compatibilidad API: estos nombres antiguos siguen significando OI
                     "call_wall_oi":max_call_oi,"put_wall_oi":max_put_oi,
                     "atm_iv_pct":_f((volatility or {}).get("atm_iv")),"expected_move":_f((volatility or {}).get("expected_move")),"expected_low":_f((volatility or {}).get("expected_low")),"expected_high":_f((volatility or {}).get("expected_high")),"gamma_regime":result.get("regime"),"net_gex":totals.get("net_gex"),"net_delta":totals.get("net_delta")},
        "expiry":{"status":(expiry_confluence or {}).get("status"),"zones":((expiry_confluence or {}).get("zones") or [])[:8],"horizons":{k:{"expirations":v.get("expirations",[]),"top":(v.get("relevant") or [])[:3]} for k,v in ((expiry_confluence or {}).get("horizons") or {}).items()}},
        "confirmations":confirmations,"confirmation_summary":{"confirmed":conf_count,"contradicted":contra,"usable":len(scored),"pending":pending,"not_applicable_premarket":not_applicable,"score":None if conf_score is None else round(conf_score,1),"base_strength":round(base_strength,1),"confirmation_adjustment":round(confirmation_adjustment,1)},
        "source_fusion":{"usable_sources":int((source_fusion or {}).get("usable_sources",0) or 0),"ecosystem":(source_fusion or {}).get("ecosystem",{}),"health":(source_fusion or {}).get("health",[])},
        "situations":_situations(agg,totals,volatility or {},expiry_confluence or {},dealer or {},data_quality or {}),
        "main_scenario":{"direction":dir_es,"zone":main_zone,"target1":_f(scanner.get("target1")),"target2":_f(scanner.get("target2")),"invalidation":_f(scanner.get("invalidation")),"scenario_type":scanner.get("scenario_type"),"evidence":round(evidence,1),"edge_state":scanner.get("edge_state")},
        "alternative_scenario":None if not alt else {"direction":"COMPRA" if str(alt.get("direction")).upper()=="BUY" else "VENTA" if str(alt.get("direction")).upper()=="SELL" else str(alt.get("direction","MIXTO")),"type":alt.get("kind"),"zone":_f(alt.get("zone")),"trigger":_f(alt.get("trigger")),"evidence":_f(alt.get("evidence_score"))},
        "conclusion":{"bias":dir_es,"strength":round(strength,1),"key_level":key_level,"best_zone":main_zone,"primary_target":_f(scanner.get("target1")),"secondary_target":_f(scanner.get("target2")),"invalidation":_f(scanner.get("invalidation")),"reading":" ".join(pieces)},
        "method_note":"El motor escanea toda la cadena seleccionada pero muestra solo 6–8 strikes cercanos al spot. No existe jerarquía fija entre proveedores: datos comparables se fusionan por calidad/frescura y Quant Data participa como corroboración de inteligencia de opciones cuando hay observaciones válidas; la estructura base se calcula nativamente. ETF, índice, futuro y opciones mantienen su identidad hasta normalización. FRED/BLS/Federal Reserve aportan contexto macro oficial. La tabla de confirmaciones es auditiva/explicativa; la síntesis direccional ocurre dentro del Scanner.",
    }
    return report
