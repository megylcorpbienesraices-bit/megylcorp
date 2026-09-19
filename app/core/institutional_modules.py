from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from .frame_guards import numeric_column

from .expiry_window import is_zero_dte
from .flow_intelligence import flow_session_phase
from .trace_analytics import structural_walls
from .contract_spec import multiplier_series
import plotly.graph_objects as go
from plotly.subplots import make_subplots


BUY_CYAN = "#27c7f4"
SELL_FUCHSIA = "#e44ed8"
GOLD = "#d9a63c"
POS_GREEN = "#15b86a"
NEG_RED = "#ef4444"
PRICE_BLUE = "#5369da"
WHITE = "#f7f8fb"
GRID = "#e5e8ee"
DARK_BG = "#08111f"
DARK_PANEL = "#0b1626"


def _num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _price_series(history: pd.DataFrame, premarket_bars: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    pieces = []
    if isinstance(premarket_bars, pd.DataFrame) and not premarket_bars.empty:
        p = premarket_bars.copy()
        p["timestamp"] = pd.to_datetime(p["timestamp"], errors="coerce")
        p = p.dropna(subset=["timestamp"])
        if "close" in p.columns:
            p = p[["timestamp", "close"]].rename(columns={"close": "price"})
            pieces.append(p)
    if isinstance(history, pd.DataFrame) and not history.empty:
        h = history.copy()
        h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
        h = h.dropna(subset=["timestamp"])
        if "underlying_price" in h.columns:
            h["price"] = pd.to_numeric(h["underlying_price"], errors="coerce")
            h = h[["timestamp", "price"]].dropna().groupby("timestamp", as_index=False)["price"].last()
            pieces.append(h)
    if not pieces:
        return pd.DataFrame(columns=["timestamp", "price"])
    out = pd.concat(pieces, ignore_index=True).dropna(subset=["timestamp", "price"])
    out = out.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    return out


def _segmented_time_series(x, y, min_gap_seconds: float = 90.0):
    """Insert explicit None breaks across temporal holes for presentation only."""
    tx=pd.to_datetime(pd.Series(list(x)),errors="coerce");yy=pd.to_numeric(pd.Series(list(y)),errors="coerce")
    good=tx.notna();tx=tx[good].reset_index(drop=True);yy=yy[good].reset_index(drop=True)
    dif=tx.diff().dt.total_seconds();pos=dif[dif.gt(0)].sort_values();expected=(float(pos.iloc[:max(1,len(pos)//2)].median()) if len(pos) else 0.0)
    threshold=max(float(min_gap_seconds),expected*4.0 if expected>0 else float(min_gap_seconds));xo=[];yo=[]
    for i,(t,v) in enumerate(zip(tx,yy)):
        if i and (t-tx.iloc[i-1]).total_seconds()>threshold:xo.append(None);yo.append(None)
        xo.append(t);yo.append(None if not math.isfinite(float(v)) else float(v))
    return xo,yo,threshold


def _nearest_price(px: pd.DataFrame, ts, fallback=np.nan):
    if px is None or px.empty:
        return fallback
    t = pd.Timestamp(ts)
    idx = (px["timestamp"] - t).abs().idxmin()
    return _num(px.loc[idx, "price"], fallback)


def _zone_label(px: pd.DataFrame, ts, price: float, gamma_flip=None, key_gamma=None) -> str:
    if px is None or px.empty:
        return "ZONA INTERMEDIA"
    t = pd.Timestamp(ts)
    phase = flow_session_phase(t)
    labels = [phase]
    p = float(price)
    if gamma_flip is not None and math.isfinite(_num(gamma_flip, np.nan)) and abs(p - float(gamma_flip)) <= 0.20:
        labels.append("CERCA FLIP")
    if key_gamma is not None and math.isfinite(_num(key_gamma, np.nan)) and abs(p - float(key_gamma)) <= 0.20:
        labels.append("KEY GAMMA")
    hist = px[px["timestamp"] <= t].copy()
    if not hist.empty:
        hist["_phase"] = [flow_session_phase(x) for x in hist["timestamp"]]
        if phase in {"LONDON", "PREMARKET"}:
            h = hist[hist["_phase"].isin(["LONDON", "PREMARKET"])]
            if not h.empty:
                lo = float(h["price"].min()); hi = float(h["price"].max())
                if abs(p - lo) <= 0.15: labels.append("LOW PRESESSION")
                elif abs(p - hi) <= 0.15: labels.append("HIGH PRESESSION")
        elif phase == "NEW YORK":
            h = hist[hist["_phase"] == "NEW YORK"]
            if not h.empty:
                opening = h.head(30)
                if not opening.empty:
                    lo = float(opening["price"].min()); hi = float(opening["price"].max())
                    if abs(p - lo) <= 0.15: labels.append("OPENING LOW")
                    elif abs(p - hi) <= 0.15: labels.append("OPENING HIGH")
                if len(labels) == 1:
                    lo = float(h["price"].min()); hi = float(h["price"].max())
                    if abs(p - lo) <= 0.15: labels.append("LOW INTRADÍA")
                    elif abs(p - hi) <= 0.15: labels.append("HIGH INTRADÍA")
    if len(labels) == 1:
        labels.append("ZONA INTERMEDIA")
    return " · ".join(labels)


def _key_gamma(result: Dict[str, Any]):
    cur = pd.DataFrame()
    if isinstance(result, dict):
        for key in ("current_delta", "current"):
            candidate = result.get(key, pd.DataFrame())
            if isinstance(candidate, pd.DataFrame) and not candidate.empty:
                cur = candidate
                break
    if cur.empty:
        return None
    if "gross_gex" in cur.columns:
        row = cur.loc[pd.to_numeric(cur["gross_gex"], errors="coerce").abs().idxmax()]
        return _num(row.get("strike"), None)
    return None


def flow_unusual_pro_figure(events: pd.DataFrame, history: pd.DataFrame,
                            result: Optional[Dict[str, Any]] = None,
                            premarket_bars: Optional[pd.DataFrame] = None,
                            symbol: str = "UNKNOWN") -> go.Figure:
    """Reference-style unusual-flow terminal with one causal synchronized clock.

    Presentation contract (v1.40.6):
      1. PRICE + exact observed event bubbles + native CALL WALL / GAMMA FLIP / PUT WALL
      2. AGGRESSOR signed premium strip
      3. TOTAL observed premium bars
      4. NET FLOW signed premium bars

    The terminal does not create option events, does not interpolate missing flow and
    does not own direction.  Scanner remains the sole directional authority.  Native
    walls are derived from the same structural chain math already used by ITM QUANT;
    Gamma Flip is published only when ``gamma_flip_crossing`` is true.
    """
    sym = str(symbol or "UNKNOWN").upper()
    result = result if isinstance(result, dict) else {}
    px = _price_series(history, premarket_bars)

    # Native structural levels.  The function consumes current_delta only; if the
    # structural snapshot is unavailable the level remains absent rather than guessed.
    spot = _num(result.get("spot"), np.nan)
    if not math.isfinite(spot) and not px.empty:
        spot = _num(px["price"].iloc[-1], np.nan)
    cur = pd.DataFrame()
    for _key in ("current_delta", "current"):
        _candidate = result.get(_key, pd.DataFrame())
        if isinstance(_candidate, pd.DataFrame) and not _candidate.empty:
            cur = _candidate.copy(); break
    _enr = result.get("enriched")
    _enr = _enr if isinstance(_enr, pd.DataFrame) and not _enr.empty else None
    walls = structural_walls(cur, spot, enriched=_enr) if not cur.empty and math.isfinite(spot) else {}
    call_wall = walls.get("call_wall")
    put_wall = walls.get("put_wall")
    gamma_flip = result.get("gamma_flip") if bool(result.get("gamma_flip_crossing", False)) else None
    key_gamma = walls.get("key_gamma") or _key_gamma(result)

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=.018,
        row_heights=[.62, .07, .17, .14],
        subplot_titles=("PRECIO", "AGRESOR", "TOTAL", "NET FLOW"),
    )

    # Price is always trace zero so continuity/audit consumers have a stable contract.
    if not px.empty:
        px_x, px_y, px_gap = _segmented_time_series(px["timestamp"], px["price"], min_gap_seconds=90.0)
        fig.add_trace(go.Scatter(
            x=px_x, y=px_y, mode="lines", name=sym, connectgaps=False,
            line=dict(color="#7d8fe8", width=2.0), opacity=.98,
            hovertemplate=f"%{{x|%H:%M}}<br>{sym} %{{y:.2f}}<extra></extra>",
            meta={"gap_threshold_seconds": round(float(px_gap), 2), "role": "UNDERLYING_PRICE"},
        ), row=1, col=1)
    else:
        # Stable placeholder trace keeps renderers deterministic without faking price.
        fig.add_trace(go.Scatter(x=[], y=[], mode="lines", name=sym, connectgaps=False,
                                 meta={"role":"UNDERLYING_PRICE","gap_threshold_seconds":90.0}), row=1, col=1)

    rows: list[dict[str, Any]] = []
    if isinstance(events, pd.DataFrame) and not events.empty:
        e = events.copy()
        e["timestamp"] = pd.to_datetime(e.get("timestamp"), errors="coerce")
        e = e.dropna(subset=["timestamp"]).sort_values("timestamp")
        for c in ["premium", "directional_premium", "flow_score", "direction_sign", "underlying_price", "contracts", "open_interest", "daily_volume", "trade_price", "bid", "ask", "iv", "provider_gamma", "provider_delta"]:
            if c in e.columns:
                e[c] = pd.to_numeric(e[c], errors="coerce")
        for _, r in e.iterrows():
            sign = 1 if _num(r.get("direction_sign")) > 0 else -1 if _num(r.get("direction_sign")) < 0 else 0
            if sign == 0:
                continue
            price = _nearest_price(px, r["timestamp"], _num(r.get("underlying_price"), np.nan))
            rows.append({
                "timestamp": r["timestamp"], "source_family": "OPTIONS", "source": str(r.get("flow_source") or "OPTIONS"),
                "sign": sign, "amount": abs(_num(r.get("premium"))), "score": _num(r.get("flow_score")),
                "price": price, "strike": _num(r.get("strike"), np.nan), "option_type": str(r.get("option_type", "")),
                "expiration_date": str(r.get("expiration_date", "")), "dte": _num(r.get("dte"), np.nan),
                "contracts": _num(r.get("contracts"), np.nan), "open_interest": _num(r.get("open_interest"), np.nan),
                "daily_volume": _num(r.get("daily_volume"), np.nan), "trade_price": _num(r.get("trade_price"), np.nan),
                "bid": _num(r.get("bid"), np.nan), "ask": _num(r.get("ask"), np.nan), "iv": _num(r.get("iv"), np.nan),
                "gamma": _num(r.get("provider_gamma"), np.nan), "delta": _num(r.get("provider_delta"), np.nan),
                "aggressor": str(r.get("aggressor") or "UNKNOWN"), "classification_method": str(r.get("classification_method") or ""),
                "zone": _zone_label(px, r["timestamp"], price, gamma_flip, key_gamma),
            })

    # Same-instrument activity is allowed as explicitly labelled causal activity.  It
    # is never relabelled as OPRA/options flow and is excluded from option-only totals.
    if isinstance(premarket_bars, pd.DataFrame) and not premarket_bars.empty and "signed_notional_proxy" in premarket_bars.columns:
        pb = premarket_bars.copy()
        pb["timestamp"] = pd.to_datetime(pb.get("timestamp"), errors="coerce")
        pb["signed"] = numeric_column(pb,"signed_notional_proxy",0.0)
        score_src = (pb["activity_score"] if "activity_score" in pb.columns
                     else pd.Series(0.0, index=pb.index, dtype=float))
        pb["score"] = pd.to_numeric(score_src, errors="coerce").fillna(0.0)
        ready = pb.get("activity_ready", pd.Series(False, index=pb.index)).fillna(False).astype(bool)
        hot = pb[ready & (pb["score"] >= 70)].copy()
        for _, r in hot.iterrows():
            sign = 1 if r["signed"] > 0 else -1 if r["signed"] < 0 else 0
            if sign == 0:
                continue
            price = _num(r.get("close"), _nearest_price(px, r["timestamp"], np.nan))
            phase = str(r.get("session_phase") or flow_session_phase(r["timestamp"]))
            reason = str(r.get("activity_reason") or "ACTIVITY")
            rows.append({
                "timestamp": r["timestamp"], "source_family": "UNDERLYING", "source": f"UNDERLYING · {phase} · {reason}",
                "sign": sign, "amount": abs(_num(r.get("signed"))), "score": _num(r.get("score")), "price": price,
                "strike": np.nan, "option_type": f"{sym} UNDERLYING", "expiration_date": "", "dte": np.nan,
                "contracts": np.nan, "open_interest": np.nan, "daily_volume": np.nan, "trade_price": np.nan,
                "bid": np.nan, "ask": np.nan, "iv": np.nan, "gamma": np.nan, "delta": np.nan,
                "aggressor": "UNDERLYING", "classification_method": "CAUSAL_UNDERLYING_ACTIVITY",
                "zone": _zone_label(px, r["timestamp"], price, gamma_flip, key_gamma),
            })

    ev = pd.DataFrame(rows)
    if not ev.empty:
        ev = ev.sort_values("timestamp").reset_index(drop=True)
        ev["minute"] = ev["timestamp"].dt.floor("min")
        ev["signed_amount"] = pd.to_numeric(ev["amount"], errors="coerce").fillna(0.0) * pd.to_numeric(ev["sign"], errors="coerce").fillna(0.0)

        # Exact event bubbles on price.  Top events are selected by observed magnitude
        # and score only for visual collision control; the full minute totals below use
        # every observed event.
        priced = ev[np.isfinite(pd.to_numeric(ev["price"], errors="coerce"))].copy()
        top = priced.sort_values(["amount", "score"], ascending=False).head(20).sort_values("timestamp")
        if not top.empty:
            labels=[]; hovers=[]; sizes=[]; colors=[]
            for _, r in top.iterrows():
                amt=float(r["amount"]); lbl=f"${amt/1e9:.2f}B" if amt>=1e9 else f"${amt/1e6:.1f}M" if amt>=1e6 else f"${amt/1e3:.0f}K"
                arrow="▲" if int(r["sign"])>0 else "▼"
                labels.append(f"{arrow} {lbl}")
                hovers.append([
                    lbl, "BUY" if int(r["sign"])>0 else "SELL", str(r["source_family"]), str(r["option_type"]),
                    None if pd.isna(r["strike"]) else float(r["strike"]), str(r["expiration_date"]),
                    None if pd.isna(r["dte"]) else float(r["dte"]), None if pd.isna(r["contracts"]) else float(r["contracts"]),
                    None if pd.isna(r["open_interest"]) else float(r["open_interest"]), None if pd.isna(r["daily_volume"]) else float(r["daily_volume"]),
                    None if pd.isna(r["trade_price"]) else float(r["trade_price"]), None if pd.isna(r["bid"]) else float(r["bid"]),
                    None if pd.isna(r["ask"]) else float(r["ask"]), None if pd.isna(r["iv"]) else float(r["iv"]),
                    None if pd.isna(r["gamma"]) else float(r["gamma"]), None if pd.isna(r["delta"]) else float(r["delta"]),
                    str(r["aggressor"]), str(r["zone"]), str(r["source"]), float(r["score"]),
                ])
                sizes.append(float(np.clip(9.0 + 4.0 * math.log10(max(1.0, amt) / 10_000.0 + 1.0), 9.0, 27.0)))
                colors.append("#27c76f" if int(r["sign"])>0 else "#ef4f66")
            fig.add_trace(go.Scatter(
                x=top["timestamp"], y=top["price"], mode="markers+text", name="Punto exacto de entrada",
                text=labels, textposition="top center", textfont=dict(color=GOLD, size=10),
                marker=dict(size=sizes, color="rgba(217,166,60,.28)", line=dict(color=GOLD, width=2.2)),
                customdata=hovers,
                hovertemplate=("%{customdata[0]} · %{customdata[1]}<br>%{x|%H:%M:%S} · precio %{y:.2f}<br>"
                               "%{customdata[2]} · %{customdata[3]} · strike %{customdata[4]} · exp %{customdata[5]}<br>"
                               "contratos %{customdata[7]} · OI %{customdata[8]} · vol %{customdata[9]}<br>"
                               "agresor %{customdata[16]} · %{customdata[17]}<extra></extra>"),
                meta={"role":"FLOW_EVENT","label":"UNUSUAL_EVENT","direction_colors":colors},
            ), row=1, col=1)

        # Option-only minute aggregation.  Underlying activity stays visible as an exact
        # marker but cannot inflate options premium totals.
        opt = ev[ev["source_family"] == "OPTIONS"].copy()
        if not opt.empty:
            minute = opt.groupby("minute", as_index=False).agg(
                net=("signed_amount", "sum"), total=("amount", "sum"), max_score=("score", "max"), events=("amount", "size")
            ).sort_values("minute")
            minute["net_m"] = minute["net"] / 1e6
            minute["total_m"] = minute["total"] / 1e6
            # Aggressor is a compact signed intensity strip normalized to [-1,+1].
            denom = minute["total"].replace(0.0, np.nan)
            minute["aggressor"] = (minute["net"] / denom).fillna(0.0).clip(-1.0, 1.0)
            signed_colors = np.where(minute["net"] >= 0, "#18c77a", "#ef4f66")
            fig.add_trace(go.Bar(x=minute["minute"], y=minute["aggressor"], name="AGRESOR", marker_color=signed_colors,
                                 hovertemplate="%{x|%H:%M}<br>Agresor %{y:.2f}<extra></extra>",
                                 meta={"role":"AGGRESSOR_BAR","unit":"SIGNED_RATIO"}), row=2, col=1)
            _positive_total = minute.loc[minute["total_m"] > 0, "total_m"]
            _label_cut = float(_positive_total.quantile(.80)) if len(_positive_total) >= 3 else (float(_positive_total.min()) if len(_positive_total) else float("inf"))
            fig.add_trace(go.Bar(x=minute["minute"], y=minute["total_m"], name="TOTAL", marker_color=GOLD, opacity=.92,
                                 text=[f"${v:.1f}M" if v>0 and v>=_label_cut else "" for v in minute["total_m"]], textposition="outside",
                                 hovertemplate="%{x|%H:%M}<br>Total $%{y:.2f}M<extra></extra>",
                                 meta={"role":"TOTAL_BAR","unit":"USD_M"}), row=3, col=1)
            fig.add_trace(go.Bar(x=minute["minute"], y=minute["net_m"], name="NET FLOW", marker_color=signed_colors,
                                 hovertemplate="%{x|%H:%M}<br>Net flow $%{y:.2f}M<extra></extra>",
                                 meta={"role":"NET_FLOW_BAR","unit":"USD_M"}), row=4, col=1)

    # Native walls and true gamma crossing.  LEVEL_LINE is consumed by the owned
    # Lightweight renderer as a price-line with compact CW/GF/PW labels.
    if not px.empty:
        x0, x1 = px["timestamp"].min(), px["timestamp"].max()
        levels = [
            ("CALL WALL", "CW", call_wall, "#1f9a72"),
            ("GAMMA FLIP", "GF", gamma_flip, "#3284a8"),
            ("PUT WALL", "PW", put_wall, "#d24b6a"),
        ]
        for full, short, value, color in levels:
            v = _num(value, np.nan)
            if not math.isfinite(v):
                continue
            fig.add_trace(go.Scatter(
                x=[x0, x1], y=[v, v], mode="lines", name=full,
                line=dict(color=color, width=1.4, dash="dash"), hoverinfo="skip",
                meta={"role":"LEVEL_LINE","level_type":full,"level_short":short,"level_value":float(v)},
            ), row=1, col=1)

    if ev.empty:
        fig.add_annotation(text="DETECTOR ACTIVO · ESPERANDO FLUJO INUSUAL OBSERVADO",
                           x=.5, y=.96, xref="paper", yref="paper", showarrow=False,
                           font=dict(color="#718a9d", size=12), bgcolor="rgba(8,17,31,.72)")

    fig.update_layout(
        template="plotly_dark", height=900, showlegend=False,
        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_PANEL,
        font=dict(color="#b8c9d6", family="Inter, Segoe UI"),
        margin=dict(l=58, r=72, t=48, b=42),
        title=dict(text=f"FLUJO INUSUAL · REFERENCE TERMINAL · {sym}", x=.01, font=dict(size=15, color="#e8f1f7")),
        hovermode="x unified", dragmode="pan", uirevision=f"flow-pro-{sym}", bargap=.18,
        meta={
            "renderer_intent":"UNUSUAL_FLOW_REFERENCE_TERMINAL", "bar_traces":True,
            "panes":["PRICE","AGGRESSOR","TOTAL","NET_FLOW"],
            "levels":{"call_wall":call_wall,"gamma_flip":gamma_flip,"put_wall":put_wall,
                      "gamma_flip_crossing":bool(result.get("gamma_flip_crossing",False))},
            "session_continuity":"MERGE_DEDUPE_GAP_SEGMENTATION",
            "bridge_session_gaps":True, "unusual_flow_start":"LONDON_OPEN_DST_AWARE", "multi_asset":True,
            "scanner_authority":"SOLE_DIRECTIONAL_AUTHORITY", "synthetic_options_events":False,
            "empty_state":"DETECTOR ACTIVO · SIN EVENTOS INUSUALES",
            "empty_detail":"Precio y niveles nativos permanecen visibles; barras y burbujas aparecen solo con eventos observados.",
        },
    )
    for r in range(1,5):
        fig.update_xaxes(gridcolor="#142235", fixedrange=False, rangeslider_visible=False, row=r, col=1)
    fig.update_yaxes(title_text=sym, gridcolor="#1c2b3e", fixedrange=False, row=1, col=1)
    fig.update_yaxes(title_text="AGG", range=[-1.05,1.05], gridcolor="#1c2b3e", fixedrange=False, row=2, col=1)
    fig.update_yaxes(title_text="$M", gridcolor="#1c2b3e", fixedrange=False, row=3, col=1)
    fig.update_yaxes(title_text="$M", zeroline=True, zerolinecolor="#64748b", gridcolor="#1c2b3e", fixedrange=False, row=4, col=1)
    return fig

def flow_pro_summary(events: pd.DataFrame, history: pd.DataFrame, result: Dict[str, Any],
                     premarket_bars: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    px = _price_series(history, premarket_bars)
    gamma_flip = result.get("gamma_flip") if isinstance(result, dict) else None
    key_gamma = _key_gamma(result or {})
    candidates = []
    if isinstance(events, pd.DataFrame) and not events.empty:
        e = events.copy(); e["timestamp"] = pd.to_datetime(e["timestamp"], errors="coerce"); e = e.dropna(subset=["timestamp"])
        for _, r in e.iterrows():
            sign = int(np.sign(_num(r.get("direction_sign"))))
            if sign == 0: continue
            price = _nearest_price(px, r["timestamp"], _num(r.get("underlying_price"), np.nan))
            candidates.append({
                "timestamp": r["timestamp"], "sign": sign, "amount": abs(_num(r.get("premium"))),
                "score": _num(r.get("flow_score")), "price": price, "strike": _num(r.get("strike"), None),
                "source": "OPTIONS", "zone": _zone_label(px, r["timestamp"], price, gamma_flip, key_gamma),
            })
    if isinstance(premarket_bars, pd.DataFrame) and not premarket_bars.empty and "signed_notional_proxy" in premarket_bars.columns:
        pb = premarket_bars.copy(); pb["timestamp"] = pd.to_datetime(pb["timestamp"], errors="coerce")
        pb["signed"] = pd.to_numeric(pb["signed_notional_proxy"], errors="coerce").fillna(0.0)
        score_src=(pb["activity_score"] if "activity_score" in pb.columns
                   else pd.Series(0.0,index=pb.index,dtype=float))
        pb["score"] = pd.to_numeric(score_src,errors="coerce").fillna(0.0)
        ready=pb.get("activity_ready",pd.Series(False,index=pb.index)).fillna(False).astype(bool)
        for _,r in pb[ready & (pb["score"]>=70)].iterrows():
            sign=int(np.sign(r["signed"])); price=_num(r.get("close"),np.nan)
            if sign==0: continue
            phase=str(r.get("session_phase") or flow_session_phase(r["timestamp"]))
            candidates.append({"timestamp":r["timestamp"],"sign":sign,"amount":abs(_num(r["signed"])),"score":_num(r["score"]),
                               "price":price,"strike":None,"source":f"UNDERLYING · {phase}","zone":_zone_label(px,r["timestamp"],price,gamma_flip,key_gamma)})
    if not candidates:
        return {"state":"WAITING","latest":None,"largest":None,"bull_total":0.0,"bear_total":0.0,"net":0.0}
    c = pd.DataFrame(candidates).sort_values("timestamp")
    bull=float(c.loc[c["sign"]>0,"amount"].sum()); bear=float(c.loc[c["sign"]<0,"amount"].sum())
    def pack(r):
        ts=pd.Timestamp(r["timestamp"]); sess=flow_session_phase(ts)
        return {"timestamp":ts.isoformat(),"side":"BUY" if r["sign"]>0 else "SELL","session":sess,
                "amount":float(r["amount"]),"score":float(r["score"]),"price":float(r["price"]),
                "strike":None if pd.isna(r.get("strike")) else float(r.get("strike")),"source":str(r["source"]),"zone":str(r["zone"])}
    latest=pack(c.iloc[-1]); largest=pack(c.sort_values(["score","amount"],ascending=False).iloc[0])
    return {"state":"ACTIVE","latest":latest,"largest":largest,"bull_total":bull,"bear_total":bear,"net":bull-bear}


def quantdata_net_drift_figure(values: Dict[str, Any] | None, symbol: str = "UNKNOWN", price_history: pd.DataFrame | None = None) -> go.Figure:
    """Quant Data observed Net Drift terminal.

    Quant Data is the *only* source of the visible drift series in this view.  The
    provider returns signed call/put premium per bucket; the cumulative lines are the
    ordered cumsum of those buckets.  Native GEX/DEX drift remains a separate internal
    structural feature and is never substituted into this chart.
    """
    sym=str(symbol or "UNKNOWN").upper()
    series=(values or {}).get("net_drift_series") or {}
    buckets=series.get("buckets") or [] if isinstance(series,dict) else []
    rows=[]
    for row in buckets:
        if not isinstance(row,dict):
            continue
        ts=pd.to_datetime(row.get("timestamp"),errors="coerce",utc=True)
        if pd.isna(ts):
            continue
        px=_num(row.get("stock_price"),np.nan)
        if not math.isfinite(px) or px<=0:
            px=np.nan
        rows.append({
            "timestamp":ts,"price":px,
            "call":_num(row.get("net_call_premium"),0.0)/1e6,
            "put":_num(row.get("net_put_premium"),0.0)/1e6,
            "call_cum":_num(row.get("cum_call_premium"),0.0)/1e6,
            "put_cum":_num(row.get("cum_put_premium"),0.0)/1e6,
            "net_cum":_num(row.get("cum_net_premium"),0.0)/1e6,
        })
    q=pd.DataFrame(rows).sort_values("timestamp") if rows else pd.DataFrame()

    fig=make_subplots(
        rows=3,cols=1,shared_xaxes=True,vertical_spacing=.028,
        row_heights=[.48,.34,.18],specs=[[{}],[{}],[{}]],
        subplot_titles=(f"PRECIO · {sym}","NET DRIFT ACUMULADO · QUANT DATA","PREMIUM NETO POR BUCKET"),
    )

    # Price context: prefer the provider's own stockPrice buckets, but supplement the
    # visual session context from the selected instrument's direct market history.  Zero
    # and negative placeholders are rejected in both paths so the axis can never jump to 0.
    provider_price=pd.DataFrame()
    if not q.empty:
        provider_price=q[["timestamp","price"]].dropna(subset=["price"])
        provider_price=provider_price[pd.to_numeric(provider_price["price"],errors="coerce").gt(0)]
    direct_price=pd.DataFrame()
    if isinstance(price_history,pd.DataFrame):
        # Session frames commonly expose OHLC `close`, while quantitative history uses
        # `underlying_price`. Accept both representations without ever fabricating price.
        if "underlying_price" in price_history.columns:
            direct_price=_price_series(price_history,pd.DataFrame())
        elif "close" in price_history.columns:
            direct_price=_price_series(pd.DataFrame(),price_history)
    if not direct_price.empty:
        direct_price=direct_price.copy()
        direct_price["timestamp"]=pd.to_datetime(direct_price["timestamp"],errors="coerce",utc=True)
        direct_price["price"]=pd.to_numeric(direct_price["price"],errors="coerce")
        direct_price=direct_price.dropna(subset=["timestamp","price"])
        direct_price=direct_price[direct_price["price"].gt(0)]
    price_frame=provider_price if len(provider_price)>=2 else direct_price
    if not price_frame.empty:
        fig.add_trace(go.Scatter(
            x=price_frame["timestamp"],y=price_frame["price"],mode="lines",name=sym,
            line=dict(color=PRICE_BLUE,width=2.2),connectgaps=False,
            hovertemplate=f"%{{x|%H:%M}} · {sym} %{{y:.2f}}<extra></extra>",
            meta={"role":"QUANTDATA_UNDERLYING_PRICE" if len(provider_price)>=2 else "DIRECT_UNDERLYING_PRICE_CONTEXT"},
        ),row=1,col=1)
        last_px=float(price_frame["price"].iloc[-1])
        fig.add_hline(y=last_px,line_width=1,line_dash="dot",line_color=PRICE_BLUE,opacity=.55,row=1,col=1)
        fig.add_annotation(x=1.0,y=last_px,xref="x domain",yref="y",text=f"{sym} {last_px:.2f}",showarrow=False,
                           xanchor="right",yanchor="bottom",font=dict(size=11,color=WHITE),bgcolor=DARK_PANEL,bordercolor=GRID,row=1,col=1)

    if not q.empty:
        # Cumulative provider drift. Calls and puts retain provider signs; NET is simply
        # CALL + PUT. No step interpolation and no GEX/DEX-derived replacement series.
        fig.add_trace(go.Scatter(x=q["timestamp"],y=q["call_cum"],mode="lines",name="CALL DRIFT",
                                 line=dict(color=POS_GREEN,width=2.0),connectgaps=False,
                                 hovertemplate="CALL DRIFT $%{y:.2f}M<extra></extra>",
                                 meta={"role":"QUANTDATA_NET_CALL_CUM"}),row=2,col=1)
        fig.add_trace(go.Scatter(x=q["timestamp"],y=q["put_cum"],mode="lines",name="PUT DRIFT",
                                 line=dict(color=NEG_RED,width=2.0),connectgaps=False,
                                 hovertemplate="PUT DRIFT $%{y:.2f}M<extra></extra>",
                                 meta={"role":"QUANTDATA_NET_PUT_CUM"}),row=2,col=1)
        fig.add_trace(go.Scatter(x=q["timestamp"],y=q["net_cum"],mode="lines",name="NET DRIFT",
                                 line=dict(color=WHITE,width=2.8),connectgaps=False,
                                 hovertemplate="NET DRIFT $%{y:.2f}M<extra></extra>",
                                 meta={"role":"QUANTDATA_NET_TOTAL_CUM"}),row=2,col=1)
        fig.add_hline(y=0,line_width=1,line_color=GRID,opacity=.8,row=2,col=1)

        # End-of-line badges make the chart readable without chasing a legend.
        latest=q.iloc[-1]
        for key,label,color,shift in (
            ("call_cum","CALL",POS_GREEN,14),("put_cum","PUT",NEG_RED,-14),("net_cum","NET",WHITE,0)
        ):
            val=float(latest[key])
            fig.add_annotation(x=q["timestamp"].iloc[-1],y=val,text=f"{label} {'+' if val>=0 else ''}{val:.2f}M",showarrow=False,
                               xanchor="left",xshift=10,yshift=shift,font=dict(size=10,color=color),
                               bgcolor=DARK_PANEL,bordercolor=color,borderwidth=1,row=2,col=1)

        # Per-bucket signed premium. Positive/negative signs are preserved as delivered
        # by Quant Data, so the zero line is meaningful at a glance.
        fig.add_trace(go.Bar(x=q["timestamp"],y=q["call"],name="CALL / BUCKET",
                             marker_color=POS_GREEN,opacity=.78,
                             hovertemplate="CALL BUCKET $%{y:.2f}M<extra></extra>",
                             meta={"role":"QUANTDATA_NET_CALL_BUCKET"}),row=3,col=1)
        fig.add_trace(go.Bar(x=q["timestamp"],y=q["put"],name="PUT / BUCKET",
                             marker_color=NEG_RED,opacity=.78,
                             hovertemplate="PUT BUCKET $%{y:.2f}M<extra></extra>",
                             meta={"role":"QUANTDATA_NET_PUT_BUCKET"}),row=3,col=1)
        fig.add_hline(y=0,line_width=1,line_color=WHITE,opacity=.45,row=3,col=1)
    else:
        fig.add_annotation(text="QUANT DATA NET DRIFT · ESPERANDO BUCKETS OBSERVADOS",xref="paper",yref="paper",
                           x=.5,y=.43,showarrow=False,font=dict(color=WHITE,size=13))

    fig.update_layout(
        template="plotly_dark",height=760,paper_bgcolor=DARK_BG,plot_bgcolor=DARK_PANEL,
        margin=dict(l=68,r=110,t=52,b=46),showlegend=False,hovermode="x unified",dragmode="pan",barmode="relative",
        uirevision=f"quantdata-net-drift-{sym}",
        meta={"renderer_intent":"QUANTDATA_NET_DRIFT_OBSERVED","provider":"QUANTDATA",
              "native_dex_gex_substitution":False,"price_zero_rejected":True,"interpolation":"NONE"},
    )
    fig.update_xaxes(gridcolor=GRID,fixedrange=False,rangeslider_visible=False,showspikes=True,spikemode="across",spikesnap="cursor")
    fig.update_yaxes(title_text="PRECIO",gridcolor=GRID,fixedrange=False,row=1,col=1)
    fig.update_yaxes(title_text="$M ACUM.",gridcolor=GRID,fixedrange=False,zeroline=False,row=2,col=1)
    fig.update_yaxes(title_text="$M / BUCKET",gridcolor=GRID,fixedrange=False,zeroline=False,row=3,col=1)
    return fig


def net_drift_pro_figure(result: Dict[str, Any], events: pd.DataFrame, scope: str = "Todas exp.", symbol: str = "UNKNOWN", session_frame: pd.DataFrame | None = None, price_history: pd.DataFrame | None = None) -> go.Figure:
    """Single-clock line terminal for Call/Put/Delta/Gamma drift.

    Every component remains a line.  Q-Flow is represented as cumulative signed
    observed premium rather than a bar histogram, and the underlying price is kept on
    an independent right scale.  Long restart holes are explicitly segmented.
    """
    sym=str(symbol or "UNKNOWN").upper()
    fig=make_subplots(rows=1,cols=1,specs=[[{"secondary_y":True}]])
    enr=result.get("enriched",pd.DataFrame()).copy() if isinstance(result,dict) else pd.DataFrame()
    cur=pd.DataFrame()
    if not enr.empty:
        enr["timestamp"]=pd.to_datetime(enr.get("timestamp"),errors="coerce");enr=enr.dropna(subset=["timestamp"])
        if scope=="0DTE": enr=enr[is_zero_dte(enr)]
        if not enr.empty:
            is_call=enr["option_type"].astype(str).str.lower().str.startswith("c")
            enr["call_dex"]=np.where(is_call,numeric_column(enr,"option_delta_exposure_info",0),np.nan)
            enr["put_dex"]=np.where(~is_call,numeric_column(enr,"option_delta_exposure_info",0),np.nan)
            cur=enr.groupby("timestamp",as_index=False).agg(
                call_dex=("call_dex",lambda q:q.sum(min_count=1)),
                put_dex=("put_dex",lambda q:q.sum(min_count=1)),
                net_dex=("option_delta_exposure_info","sum"),net_gex=("signed_gex_proxy","sum"),
                price=("underlying_price","last"),
            ).sort_values("timestamp")
    hist=pd.DataFrame()
    if isinstance(session_frame,pd.DataFrame) and not session_frame.empty:
        h=session_frame.copy();h["timestamp"]=pd.to_datetime(h.get("timestamp"),errors="coerce");h=h.dropna(subset=["timestamp"])
        if not h.empty:
            hist=pd.DataFrame({
                "timestamp":h["timestamp"],"call_dex":numeric_column(h,"call_delta",float("nan")),
                "put_dex":numeric_column(h,"put_delta",float("nan")),"net_dex":numeric_column(h,"net_delta",float("nan")),
                "net_gex":numeric_column(h,"net_gex",float("nan")),"price":numeric_column(h,"spot",float("nan"))})
    agg=pd.concat([hist,cur],ignore_index=True,sort=False) if not hist.empty or not cur.empty else pd.DataFrame()

    def segmented(x,y,min_gap_seconds=300.0):
        tx=pd.to_datetime(x,errors="coerce");yy=pd.to_numeric(y,errors="coerce")
        good=tx.notna();tx=tx[good].reset_index(drop=True);yy=yy[good].reset_index(drop=True)
        valid=yy.notna();vt=tx[valid].reset_index(drop=True);dif=vt.diff().dt.total_seconds();pos=dif[dif.gt(0)]
        expected=float(pos.median()) if len(pos) else 0.0
        threshold=max(float(min_gap_seconds),expected*6.0 if expected>0 else float(min_gap_seconds));threshold=min(threshold,15*60.0)
        xo=[];yo=[];last_valid_t=None
        for t,v in zip(tx,yy):
            finite=pd.notna(v) and math.isfinite(float(v))
            if finite and last_valid_t is not None and (t-last_valid_t).total_seconds()>threshold:
                xo.append(None);yo.append(None)
            if finite:
                xo.append(t);yo.append(float(v));last_valid_t=t
        return xo,yo,threshold

    continuity_threshold=300.0;observed_counts={}
    if not agg.empty:
        agg=agg.sort_values("timestamp").reset_index(drop=True)
        def _last_observed(q):
            z=pd.to_numeric(q,errors="coerce").dropna();return float(z.iloc[-1]) if len(z) else np.nan
        agg=agg.groupby("timestamp",as_index=False).agg(
            call_dex=("call_dex",_last_observed),put_dex=("put_dex",_last_observed),
            net_dex=("net_dex",_last_observed),net_gex=("net_gex",_last_observed),price=("price",_last_observed),
        ).sort_values("timestamp").reset_index(drop=True)
        for c in ["call_dex","put_dex","net_dex","net_gex"]:
            q=pd.to_numeric(agg.get(c),errors="coerce");valid=q.dropna();agg[c]=(q-float(valid.iloc[0]))/1e6 if len(valid) else np.nan
        agg["price"]=numeric_column(agg,"price",float("nan"))
        lines=[("call_dex","CALL DRIFT",BUY_CYAN,2.0),("put_dex","PUT DRIFT",SELL_FUCHSIA,2.0),("net_dex","NET DELTA DRIFT",WHITE,2.6),("net_gex","GAMMA DRIFT",POS_GREEN,2.6)]
        for key,name,color,width in lines:
            xx,yy,continuity_threshold=segmented(agg["timestamp"],agg[key]);observed_counts[key]=int(pd.to_numeric(agg[key],errors="coerce").notna().sum())
            fig.add_trace(go.Scatter(x=xx,y=yy,mode="lines+markers",name=name,line=dict(color=color,width=width),marker=dict(color=color,size=3.5,opacity=.55),connectgaps=False,
                                     meta={"gap_threshold_seconds":round(float(continuity_threshold),2),"role":"DRIFT_LINE"}),secondary_y=False)

    # Q-Flow becomes another line in the same temporal terminal, never a bar panel.
    if isinstance(events,pd.DataFrame) and not events.empty:
        e=events.copy();e["timestamp"]=pd.to_datetime(e.get("timestamp"),errors="coerce");e=e.dropna(subset=["timestamp"])
        if scope=="0DTE": e=e[is_zero_dte(e)]
        if not e.empty:
            e["premium"]=numeric_column(e,"premium",0.0);e["direction_sign"]=numeric_column(e,"direction_sign",0.0)
            e["minute"]=e["timestamp"].dt.floor("min");e["signed"]=e["premium"]*e["direction_sign"]
            m=e.groupby("minute",as_index=False).agg(signed=("signed","sum"));m["flow_cum_m"]=m["signed"].cumsum()/1e6
            fx,fy,_=segmented(m["minute"],m["flow_cum_m"],min_gap_seconds=180.0)
            fig.add_trace(go.Scatter(x=fx,y=fy,mode="lines",name="Q-FLOW CUM",line=dict(color=GOLD,width=2.0,dash="dot"),connectgaps=False,
                                     meta={"role":"FLOW_LINE"}),secondary_y=False)

    # Dense underlying price uses the full observed session, independent of sparse drift snapshots.
    pp=_price_series(price_history,pd.DataFrame()) if isinstance(price_history,pd.DataFrame) and not price_history.empty else pd.DataFrame()
    if not pp.empty:
        px,py,_=segmented(pp["timestamp"],pp["price"],min_gap_seconds=90.0)
        fig.add_trace(go.Scatter(x=px,y=py,mode="lines",name=sym,line=dict(color="#8f9bb3",width=1.5,dash="dot"),connectgaps=False,
                                 meta={"role":"UNDERLYING_PRICE"}),secondary_y=True)
    elif not agg.empty:
        px,py,_=segmented(agg["timestamp"],agg["price"])
        fig.add_trace(go.Scatter(x=px,y=py,mode="lines",name=sym,line=dict(color="#8f9bb3",width=1.5,dash="dot"),connectgaps=False,
                                 meta={"role":"UNDERLYING_PRICE"}),secondary_y=True)

    if agg.empty:
        fig.add_annotation(text="NET DRIFT · ACUMULANDO SNAPSHOTS ESTRUCTURALES",xref="paper",yref="paper",x=.5,y=.92,showarrow=False,font=dict(color="#718a9d",size=12))

    fig.update_layout(template="plotly_dark",height=680,paper_bgcolor=DARK_BG,plot_bgcolor=DARK_PANEL,
                      margin=dict(l=62,r=72,t=55,b=46),title=f"NET DRIFT · LINE TERMINAL · {sym} · {scope}",
                      legend=dict(orientation="h",y=1.07,x=0),hovermode="x unified",dragmode="pan",uirevision=f"net-drift-{sym}-{scope}",
                      meta={"renderer_intent":"LINE_TERMINAL_TRACE_STYLE","bar_traces":False,"session_continuity":"MERGE_DEDUPE_GAP_SEGMENTATION","gap_threshold_seconds":round(float(continuity_threshold),2),"connectgaps":False,"bridge_session_gaps":True,"bridge_max_gap_ms":64800000,"bridge_opacity":0.72,"bridge_dash":[6,4],"drift_observations":observed_counts,"sparse_snapshots_visible":True,"empty_state":"NET DRIFT · SIN HISTORIAL SUFICIENTE","empty_detail":"Los snapshots reales se conservan; huecos largos se cortan sin interpolar datos sintéticos."})
    fig.update_xaxes(gridcolor="#142235",fixedrange=False,rangeslider_visible=False)
    fig.update_yaxes(title_text="Drift / Flow acumulado ($M)",gridcolor="#1c2b3e",fixedrange=False,secondary_y=False)
    fig.update_yaxes(title_text=sym,showgrid=False,fixedrange=False,secondary_y=True)
    return fig

def exposure_by_strike_pro_figure(result: Dict[str, Any], metric: str = "Gamma", symbol: str = "UNKNOWN") -> go.Figure:
    cur = result.get("current_delta", pd.DataFrame()).copy() if isinstance(result, dict) else pd.DataFrame()
    enr = result.get("enriched", pd.DataFrame()).copy() if isinstance(result, dict) else pd.DataFrame()
    if cur.empty:
        return go.Figure().add_annotation(text="Sin exposición por strike",showarrow=False)
    spot=_num(result.get("spot"),np.nan); flip=result.get("gamma_flip")
    cur["strike"]=pd.to_numeric(cur["strike"],errors="coerce")
    if metric=="Gamma":
        y=numeric_column(cur,"signed_gex",0)/1e6; title="NET GEX POR STRIKE"; unit="$M GEX"
    elif metric=="Delta":
        y=numeric_column(cur,"delta_exposure",0)/1e6; title="NET DELTA EXPOSURE POR STRIKE"; unit="$M DEX"
    else:
        e=enr.copy();e["timestamp"]=pd.to_datetime(e["timestamp"],errors="coerce");e=e[e["timestamp"]==e["timestamp"].max()].copy()
        is_call=e["option_type"].astype(str).str.lower().str.startswith("c")
        col="open_interest" if metric=="Net OI" else "volume"
        e["v"]=pd.to_numeric(e.get(col,0),errors="coerce").fillna(0)
        e["signed"]=np.where(is_call,e["v"],-e["v"])
        a=e.groupby("strike",as_index=False)["signed"].sum(); cur=cur[["strike"]].merge(a,on="strike",how="left").fillna({"signed":0})
        y=cur["signed"]; title="NET OI POR STRIKE" if metric=="Net OI" else "VOLUMEN NETO POR STRIKE"; unit="Contratos"
    cur["value"]=np.asarray(y,dtype=float)
    cur=cur.sort_values("strike")
    colors=np.where(cur["value"]>=0,POS_GREEN,NEG_RED)
    fig=go.Figure(go.Bar(x=cur["strike"],y=cur["value"],marker_color=colors,name=metric,
                         hovertemplate="Strike %{x:.2f}<br>Valor %{y:,.2f}<extra></extra>"))
    fig.add_hline(y=0,line_color="#7f8a9d",line_width=1)
    if math.isfinite(spot):
        fig.add_vline(x=spot,line_color="#e8edf5",line_width=1.6,line_dash="dot",annotation_text=f"{str(symbol).upper()} {spot:.2f}",annotation_position="top")
    if flip is not None and math.isfinite(_num(flip,np.nan)):
        fig.add_vline(x=float(flip),line_color="#6d72ff",line_width=1.4,line_dash="dash",annotation_text=f"Flip {float(flip):.2f}",annotation_position="bottom")
    fig.update_layout(template="plotly_dark",height=650,paper_bgcolor=DARK_BG,plot_bgcolor=DARK_PANEL,
                      margin=dict(l=65,r=45,t=60,b=95),title=f"EXPOSICIÓN POR STRIKE PRO · {title}",
                      xaxis_title=f"Strike {str(symbol).upper()}",yaxis_title=unit,bargap=.22,hovermode="x")
    fig.update_xaxes(tickangle=-45,gridcolor="#142235");fig.update_yaxes(gridcolor="#1c2b3e")
    return fig


def exposure_summary(result: Dict[str, Any], metric: str = "Gamma") -> Dict[str, Any]:
    cur=result.get("current_delta",pd.DataFrame()).copy() if isinstance(result,dict) else pd.DataFrame()
    if cur.empty:return {}
    spot=_num(result.get("spot"),np.nan);cur["strike"]=pd.to_numeric(cur["strike"],errors="coerce")
    if metric=="Gamma": vals=numeric_column(cur,"signed_gex",0)/1e6
    elif metric=="Delta": vals=numeric_column(cur,"delta_exposure",0)/1e6
    else:return {"spot":spot,"flip":result.get("gamma_flip")}
    cur["v"]=vals
    p=cur.sort_values("v",ascending=False).iloc[0];n=cur.sort_values("v",ascending=True).iloc[0]
    near=cur.iloc[(cur["strike"]-spot).abs().argsort()[:1]].iloc[0] if math.isfinite(spot) else cur.iloc[0]
    return {"positive_strike":float(p["strike"]),"positive_value":float(p["v"]),"negative_strike":float(n["strike"]),"negative_value":float(n["v"]),
            "nearest_strike":float(near["strike"]),"nearest_value":float(near["v"]),"spot":spot,"flip":result.get("gamma_flip")}


def net_drift_summary(result: Dict[str, Any], scope: str = "Todas exp.") -> Dict[str, Any]:
    enr=result.get("enriched",pd.DataFrame()).copy() if isinstance(result,dict) else pd.DataFrame()
    if enr.empty:return {}
    enr["timestamp"]=pd.to_datetime(enr["timestamp"],errors="coerce");enr=enr.dropna(subset=["timestamp"])
    if scope=="0DTE":enr=enr[is_zero_dte(enr)]
    if enr.empty:return {}
    is_call=enr["option_type"].astype(str).str.lower().str.startswith("c")
    enr["call_dex"]=np.where(is_call,numeric_column(enr,"option_delta_exposure_info",0),0.0)
    enr["put_dex"]=np.where(~is_call,numeric_column(enr,"option_delta_exposure_info",0),0.0)
    a=enr.groupby("timestamp",as_index=False).agg(call=("call_dex","sum"),put=("put_dex","sum"),net=("option_delta_exposure_info","sum"),gamma=("signed_gex_proxy","sum")).sort_values("timestamp")
    if len(a)<1:return {}
    out={}
    for src,dst in [("call","call"),("put","put"),("net","net"),("gamma","gamma")]:
        out[dst]=float((a[src].iloc[-1]-a[src].iloc[0])/1e6)
    return out


def _gex_matrix_timestamp_to_ny(ts):
    """Interpret stored naive timestamps as Ecuador local, then convert to New York."""
    from zoneinfo import ZoneInfo
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize(ZoneInfo("America/Guayaquil"))
    return t.tz_convert(ZoneInfo("America/New_York"))


def _gex_open_reference(timestamps, latest_ts, tolerance_minutes: float = 12.0):
    """Return (reference_ts, label) without falsely calling a late first snapshot 'open'."""
    if not timestamps:
        return None, "SIN REFERENCIA"
    try:
        latest_ny = _gex_matrix_timestamp_to_ny(latest_ts)
        day = latest_ny.date()
        same = [t for t in timestamps if _gex_matrix_timestamp_to_ny(t).date() == day and pd.Timestamp(t) < pd.Timestamp(latest_ts)]
        if not same:
            return None, "SIN REFERENCIA"
        target = pd.Timestamp(f"{day.isoformat()} 09:30:00", tz="America/New_York")
        ref = min(same, key=lambda t: abs((_gex_matrix_timestamp_to_ny(t) - target).total_seconds()))
        gap = abs((_gex_matrix_timestamp_to_ny(ref) - target).total_seconds()) / 60.0
        if gap <= float(tolerance_minutes):
            return ref, "APERTURA"
        return min(same), "PRIMER SNAPSHOT"
    except Exception:
        prior = [t for t in timestamps if pd.Timestamp(t) < pd.Timestamp(latest_ts)]
        return (min(prior), "PRIMER SNAPSHOT") if prior else (None, "SIN REFERENCIA")


def _auto_matrix_label_threshold(vals: np.ndarray) -> float:
    """Relative visual label threshold: no fixed $ amount shared by every asset."""
    a = np.abs(np.asarray(vals, dtype=float))
    a = a[np.isfinite(a) & (a > 0)]
    if not len(a):
        return float("inf")
    # Keep roughly the strongest quartile, with a small relative floor so dense/noisy
    # matrices do not fill every cell with text. This only changes labels, never data.
    q = float(np.nanquantile(a, 0.75))
    rel = float(np.nanmax(a)) * 0.08
    return max(q, rel, 1e-12)


def gex_matrix_figure(result: Dict[str, Any], exp_count: int = 4, mode: str = "GEX",
                      baseline: str = "OPEN", text_threshold_m: Any = "AUTO") -> go.Figure:
    """GEX / ΔGEX matrix with strict cross-snapshot comparability.

    GEX is the current signed structural exposure proxy.
    ΔGEX is *change in calculated exposure*, not observed dealer migration. A delta exists
    only when the same (strike, expiration) cell exists in both snapshots. Newly listed,
    newly loaded, or out-of-window cells are never subtracted from zero.
    """
    enr = result.get("enriched", pd.DataFrame()).copy() if isinstance(result, dict) else pd.DataFrame()
    if enr.empty:
        return go.Figure().add_annotation(text="Sin datos para GEX Matrix", showarrow=False)
    enr["timestamp"] = pd.to_datetime(enr.get("timestamp"), errors="coerce")
    enr["strike"] = numeric_column(enr,"strike",float("nan"))
    enr = enr.dropna(subset=["timestamp", "strike"])
    if enr.empty:
        return go.Figure().add_annotation(text="Sin datos para GEX Matrix", showarrow=False)
    if "expiration_date" not in enr.columns:
        return go.Figure().add_annotation(
            text="Sin expiration_date: la matriz no infiere vencimientos desde DTE fraccional",
            showarrow=False)
    enr["expiration_date"] = enr["expiration_date"].astype(str)
    timestamps = sorted(enr["timestamp"].dropna().unique())
    latest_ts = timestamps[-1]
    latest_raw = enr[enr["timestamp"] == latest_ts].copy()
    exps = sorted(latest_raw["expiration_date"].dropna().unique().tolist())[:max(1, int(exp_count))]
    latest_raw = latest_raw[latest_raw["expiration_date"].isin(exps)].copy()
    latest_raw["gex_m"] = pd.to_numeric(latest_raw.get("signed_gex_proxy", 0), errors="coerce") / 1e6
    latest_cells = latest_raw.groupby(["strike", "expiration_date"], dropna=False)["gex_m"].sum(min_count=1)

    is_delta = str(mode or "GEX").upper().replace("DELTA", "Δ").startswith("Δ")
    new_cells = []
    gone_cells = []
    new_exps = []
    gone_exps = []
    if is_delta:
        base = str(baseline or "OPEN").strip().upper()
        if base == "PREVIOUS":
            ref_ts = timestamps[-2] if len(timestamps) >= 2 else None
            ref_label = "SNAPSHOT PREVIO"
        else:
            ref_ts, ref_label = _gex_open_reference(timestamps, latest_ts)
        if ref_ts is None:
            pv = pd.DataFrame()
            subtitle = "Δ no calculable · aún sin snapshot de referencia"
        else:
            prev_raw = enr[enr["timestamp"] == ref_ts].copy()
            prev_raw["gex_m"] = pd.to_numeric(prev_raw.get("signed_gex_proxy", 0), errors="coerce") / 1e6
            prev_cells = prev_raw.groupby(["strike", "expiration_date"], dropna=False)["gex_m"].sum(min_count=1)
            latest_keys = set(latest_cells.index.tolist())
            prev_keys = set(prev_cells.index.tolist())
            common_keys = sorted(latest_keys & prev_keys)
            new_cells = sorted(latest_keys - prev_keys)
            gone_cells = sorted(prev_keys - latest_keys)
            latest_exp_set = {str(k[1]) for k in latest_keys}
            prev_exp_set = {str(k[1]) for k in prev_keys}
            new_exps = sorted(latest_exp_set - prev_exp_set)
            gone_exps = sorted(prev_exp_set - latest_exp_set)
            if not common_keys:
                pv = pd.DataFrame()
                subtitle = f"Δ no calculable · sin celdas comparables con {ref_label.lower()}"
            else:
                delta = pd.Series({k: float(latest_cells.loc[k] - prev_cells.loc[k]) for k in common_keys})
                delta.index = pd.MultiIndex.from_tuples(delta.index, names=["strike", "expiration_date"])
                pv = delta.unstack("expiration_date")  # missing cross-products stay NaN, never zero
                gap_min = max(0.0, (pd.Timestamp(latest_ts) - pd.Timestamp(ref_ts)).total_seconds()/60.0)
                subtitle = f"Δ vs {ref_label.lower()} · {pd.Timestamp(ref_ts).strftime('%H:%M:%S')} ({gap_min:.0f} min antes)"
                if new_cells:
                    subtitle += f" · {len(new_cells)} celdas NUEVAS sin Δ"
                if gone_cells:
                    subtitle += f" · {len(gone_cells)} celdas SALIERON sin Δ"
                if new_exps:
                    subtitle += f" · {len(new_exps)} venc. NUEVO"
                if gone_exps:
                    subtitle += f" · {len(gone_exps)} venc. SALIDA"
        title = "ΔGEX MATRIX · CAMBIO DE EXPOSICIÓN · STRIKE × VENCIMIENTO"
        hover_label = "ΔGEX"
    else:
        pv = latest_cells.unstack("expiration_date")
        subtitle = f"exposición estructural proxy · {pd.Timestamp(latest_ts).strftime('%H:%M:%S')}"
        title = "GEX MATRIX · STRIKE × VENCIMIENTO"
        hover_label = "GEX"

    if pv is None or pv.empty:
        fig = go.Figure()
        fig.add_annotation(text=subtitle, showarrow=False)
        fig.update_layout(template="plotly_dark", height=520, title=title)
        return fig
    pv = pv.sort_index(ascending=False)
    # Keep expiry order chronological and only current selected expiries where possible.
    ordered_cols = [c for c in exps if c in pv.columns] + [c for c in pv.columns if c not in exps]
    pv = pv.reindex(columns=ordered_cols)
    vals = pv.to_numpy(dtype=float)
    finite = np.abs(vals[np.isfinite(vals)])
    raw_max = max(float(np.nanmax(finite)) if finite.size else 0.0, 1e-9)
    # Robust display scaling: the exact values stay untouched in customdata/hover,
    # while a single extreme cell is not allowed to flatten the rest of the matrix.
    # This is presentation-only and never changes Scanner/GEX math.
    if finite.size >= 4:
        ordered = np.sort(finite)
        trimmed = ordered[:-1] if ordered[-1] > ordered[-2] * 1.20 else ordered
        robust_cap = max(float(np.nanquantile(trimmed, 0.97)), float(np.nanquantile(ordered, 0.85)), 1e-9)
    else:
        robust_cap = raw_max
    z = np.clip(vals / robust_cap, -1.0, 1.0)
    mvc = None
    if finite.size:
        flat = np.where(np.isfinite(vals), np.abs(vals), -np.inf)
        ii, jj = np.unravel_index(int(np.argmax(flat)), vals.shape)
        mvc = {"strike": float(pv.index[ii]), "expiration": str(pv.columns[jj]), "value_m": float(vals[ii, jj]),
               "display_saturated": bool(abs(float(vals[ii, jj])) > robust_cap)}
    if isinstance(text_threshold_m, str) and text_threshold_m.strip().upper() == "AUTO":
        # Keep labels relative to matrix magnitude to avoid illegible text walls.
        # Every strike level remains explicit on the Y axis and every cell remains
        # exact in hover regardless of whether its compact label is suppressed.
        thr = _auto_matrix_label_threshold(vals)
        threshold_note = "AUTO · RELATIVO"
    else:
        try:
            thr = max(float(text_threshold_m), 0.0)
        except Exception:
            thr = _auto_matrix_label_threshold(vals)
        threshold_note = f"{thr:.2f}M"
    text = np.empty(vals.shape, dtype=object)
    for i in range(vals.shape[0]):
        for j in range(vals.shape[1]):
            v = vals[i, j]
            if np.isfinite(v) and abs(v) >= thr:
                av = abs(float(v))
                text[i, j] = f"{v:.3f}M" if av < 0.1 else f"{v:.2f}M" if av < 1 else f"{v:.1f}M"
            else:
                text[i, j] = ""

    col_labels = [str(c)[5:] if len(str(c)) == 10 else str(c) for c in pv.columns]
    row_labels = [f"{float(v):g}" for v in pv.index]
    fig = go.Figure(go.Heatmap(
        z=z, x=col_labels, y=row_labels, text=text, texttemplate="%{text}",
        colorscale=[[0.0,"#7f1020"],[0.32,"#d82939"],[0.49,"#111827"],[0.51,"#111827"],
                    [0.68,"#0b6e3e"],[1.0,"#19db66"]],
        zmin=-1, zmax=1, zmid=0, showscale=False, customdata=vals, xgap=2, ygap=1,
        hovertemplate=f"Strike %{{y}}<br>Venc. %{{x}}<br>{hover_label} %{{customdata:.2f}}M<extra></extra>",
    ))
    if mvc is not None:
        _mx = str(mvc["expiration"]); _mx = _mx[5:] if len(_mx) == 10 else _mx
        fig.add_trace(go.Scatter(x=[_mx], y=[f"{float(mvc['strike']):g}"], mode="markers",
                                 marker=dict(symbol="square-open", size=22, color="#f1c84c", line=dict(width=2, color="#f1c84c")),
                                 name="MVC · mayor |exposición|", hovertemplate=f"MVC<br>Strike %{{y}}<br>Venc. %{{x}}<br>{hover_label} {mvc['value_m']:.2f}M<extra></extra>"))
    spot = _num(result.get("spot"), np.nan)
    if math.isfinite(spot) and len(pv.index):
        nearest = min([float(v) for v in pv.index], key=lambda k: abs(k - spot))
        ylab = f"{nearest:g}"
        # Scatter overlay works with a categorical axis in real Plotly; add_hrect with
        # string categories can raise while computing annotation centers.
        if col_labels:
            fig.add_trace(go.Scatter(x=col_labels, y=[ylab]*len(col_labels), mode="lines",
                                     line=dict(color="#3fa7ff", width=2), opacity=.85,
                                     hoverinfo="skip", showlegend=False, name="Spot row"))
            fig.add_annotation(x=col_labels[0], y=ylab, text=f"SPOT {spot:.2f}", showarrow=False,
                               xanchor="left", yshift=13, font=dict(color="#3fa7ff"))
    subtitle += f" · etiquetas {threshold_note}"
    matrix_h = int(min(1180, max(720, 150 + 18 * len(row_labels))))
    fig.update_layout(template="plotly_dark", height=matrix_h, paper_bgcolor=DARK_BG, plot_bgcolor=DARK_PANEL,
                      meta={"renderer_intent":"MATRIX_EXACT_VALUES_ROBUST_SCALE","robust_scale_cap_m":robust_cap,"raw_max_m":raw_max,"mvc":mvc,"exact_values_in_hover":True,"expiration_map":dict(zip(col_labels,[str(c) for c in pv.columns]))},
                      margin=dict(l=82, r=35, t=86, b=68), title=f"{title}<br><sup>{subtitle}</sup>",
                      xaxis_title="Vencimiento", yaxis_title="Strike", font=dict(color="#eef4ff"))
    fig.update_xaxes(type="category", tickmode="array", tickvals=col_labels, ticktext=col_labels, tickangle=0)
    fig.update_yaxes(type="category", tickmode="array", tickvals=row_labels, ticktext=row_labels,
                     tickfont=dict(size=9), automargin=True)
    return fig

def net_positioning_by_strike_figure(result: Dict[str, Any], mode: str = "Delta-adjusted") -> go.Figure:
    """Transparent positioning proxy by strike.

    It does NOT claim customer/dealer inventory. It uses only the chain available
    to ITM QUANT: OI, calculated Delta and signed Gamma proxies.
    """
    enr = result.get("enriched", pd.DataFrame()).copy() if isinstance(result, dict) else pd.DataFrame()
    if enr.empty:
        return go.Figure().add_annotation(text="Sin datos de posicionamiento", showarrow=False)
    enr["timestamp"] = pd.to_datetime(enr.get("timestamp"), errors="coerce")
    enr = enr.dropna(subset=["timestamp"])
    x = enr[enr["timestamp"] == enr["timestamp"].max()].copy()
    is_call = x["option_type"].astype(str).str.lower().str.startswith("c")
    oi = numeric_column(x,"open_interest",0)
    mode_norm = str(mode).lower()
    if mode_norm.startswith("oi"):
        x["pos"] = np.where(is_call, oi, -oi)
        title = "NET OI PROXY · CALL OI - PUT OI"
        unit = "Contratos"
    elif mode_norm.startswith("gamma"):
        x["pos"] = numeric_column(x,"signed_gex_proxy",0) / 1e6
        title = "GAMMA-ADJUSTED POSITIONING PROXY"
        unit = "$M GEX"
    else:
        delta = pd.to_numeric(x.get("calc_delta", x.get("provider_delta", 0)), errors="coerce").fillna(0)
        x["pos"] = delta * oi * multiplier_series(x)
        title = "DELTA-ADJUSTED POSITIONING PROXY"
        unit = "Delta shares eq."
    a = x.groupby("strike", as_index=False)["pos"].sum().sort_values("strike")
    colors = np.where(a["pos"] >= 0, "#2f7df6", "#f04455")
    fig = go.Figure(go.Bar(x=a["strike"], y=a["pos"], marker_color=colors,
                           hovertemplate="Strike %{x:.2f}<br>Posición proxy %{y:,.2f}<extra></extra>"))
    fig.add_hline(y=0, line_color="#718096", line_width=1)
    spot = _num(result.get("spot"), np.nan)
    if math.isfinite(spot):
        fig.add_vline(x=spot, line_color="#e5e7eb", line_width=1.5, line_dash="dot", annotation_text=f"SPOT {spot:.2f}", annotation_position="top")
    fig.update_layout(template="plotly_dark",height=560,paper_bgcolor=DARK_BG,plot_bgcolor=DARK_PANEL,
                      margin=dict(l=70,r=35,t=65,b=90),title=title,xaxis_title="Strike",yaxis_title=unit,bargap=.18)
    fig.update_xaxes(tickangle=-45,gridcolor="#142235");fig.update_yaxes(gridcolor="#1c2b3e")
    return fig


def volume_by_strike_figure(result: Dict[str, Any], mode: str = "Calls vs Puts") -> go.Figure:
    """Options volume view by strike for the selected asset.

    Calls vs Puts: call volume positive/cyan and put volume negative/fuchsia.
    Volumen Total: total contracts traded at each strike.
    Vol/OI: total volume divided by total open interest at each strike.
    """
    enr = result.get("enriched", pd.DataFrame()).copy() if isinstance(result, dict) else pd.DataFrame()
    if enr.empty:
        return go.Figure().add_annotation(text="Sin datos de volumen", showarrow=False)
    enr["timestamp"] = pd.to_datetime(enr.get("timestamp"), errors="coerce")
    enr = enr.dropna(subset=["timestamp"])
    if enr.empty:
        return go.Figure().add_annotation(text="Sin datos de volumen", showarrow=False)
    x = enr[enr["timestamp"] == enr["timestamp"].max()].copy()
    x["strike"] = numeric_column(x,"strike",float("nan"))
    x["volume"] = numeric_column(x,"volume",0)
    x["open_interest"] = numeric_column(x,"open_interest",0)
    x = x.dropna(subset=["strike"])
    is_call = x["option_type"].astype(str).str.lower().str.startswith("c")
    x["call_vol"] = np.where(is_call, x["volume"], 0.0)
    x["put_vol"] = np.where(~is_call, x["volume"], 0.0)
    a = x.groupby("strike", as_index=False).agg(call_vol=("call_vol","sum"), put_vol=("put_vol","sum"), total_vol=("volume","sum"), oi=("open_interest","sum")).sort_values("strike")
    fig = go.Figure()
    m = str(mode or "Calls vs Puts").lower()
    if m.startswith("calls"):
        fig.add_trace(go.Bar(x=a["strike"], y=a["call_vol"], name="CALL VOLUME", marker_color=BUY_CYAN, hovertemplate="Strike %{x:.2f}<br>Call volume %{y:,.0f}<extra></extra>"))
        fig.add_trace(go.Bar(x=a["strike"], y=-a["put_vol"], name="PUT VOLUME", marker_color=SELL_FUCHSIA, hovertemplate="Strike %{x:.2f}<br>Put volume %{customdata:,.0f}<extra></extra>", customdata=a["put_vol"]))
        title="VOLUMEN DE OPCIONES · CALLS ARRIBA / PUTS ABAJO"; unit="Contratos"; barmode="relative"
    elif m.startswith("volumen total"):
        colors=np.where(a["total_vol"]>=a["total_vol"].quantile(.85) if len(a)>2 else True, GOLD, BUY_CYAN)
        fig.add_trace(go.Bar(x=a["strike"], y=a["total_vol"], name="VOLUMEN TOTAL", marker_color=colors, hovertemplate="Strike %{x:.2f}<br>Volumen total %{y:,.0f}<extra></extra>"))
        title="VOLUMEN TOTAL DE OPCIONES POR STRIKE"; unit="Contratos"; barmode="group"
    else:
        a["vol_oi"] = np.where(a["oi"]>0, a["total_vol"]/a["oi"], np.nan)
        vals=a["vol_oi"].fillna(0)
        colors=np.where(vals>=1.0, GOLD, np.where(vals>=0.5, POS_GREEN, "#4b6b91"))
        fig.add_trace(go.Bar(x=a["strike"], y=vals, name="VOL/OI", marker_color=colors, customdata=np.c_[a["total_vol"],a["oi"]], hovertemplate="Strike %{x:.2f}<br>Vol/OI %{y:.2f}<br>Vol %{customdata[0]:,.0f}<br>OI %{customdata[1]:,.0f}<extra></extra>"))
        fig.add_hline(y=1.0,line_color=GOLD,line_dash="dot",line_width=1.2,annotation_text="Vol = OI")
        title="ACTIVIDAD DE VOLUMEN · VOL/OI POR STRIKE"; unit="Ratio Vol/OI"; barmode="group"
    spot = _num(result.get("spot"), np.nan)
    if math.isfinite(spot):
        fig.add_vline(x=spot,line_color=WHITE,line_width=1.5,line_dash="dot",annotation_text=f"SPOT {spot:.2f}",annotation_position="top")
    flip = _num(result.get("gamma_flip"), np.nan)
    if math.isfinite(flip):
        fig.add_vline(x=flip,line_color="#6d72ff",line_width=1.2,line_dash="dash",annotation_text=f"FLIP {flip:.2f}",annotation_position="bottom")
    fig.add_hline(y=0,line_color="#718096",line_width=1)
    fig.update_layout(template="plotly_dark",height=620,paper_bgcolor=DARK_BG,plot_bgcolor=DARK_PANEL,margin=dict(l=70,r=35,t=65,b=90),title=title,xaxis_title="Strike",yaxis_title=unit,bargap=.20,barmode=barmode,legend=dict(orientation="h",y=1.04),hovermode="x")
    fig.update_xaxes(tickangle=-45,gridcolor="#142235"); fig.update_yaxes(gridcolor="#1c2b3e")
    return fig


def volume_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    enr = result.get("enriched", pd.DataFrame()).copy() if isinstance(result, dict) else pd.DataFrame()
    if enr.empty: return {}
    enr["timestamp"] = pd.to_datetime(enr.get("timestamp"), errors="coerce"); enr=enr.dropna(subset=["timestamp"])
    if enr.empty: return {}
    timestamps=sorted(enr["timestamp"].unique())
    x=enr[enr["timestamp"]==timestamps[-1]].copy()
    x["volume"]=numeric_column(x,"volume",0); x["open_interest"]=numeric_column(x,"open_interest",0); x["strike"]=numeric_column(x,"strike",float("nan"))
    is_call=x["option_type"].astype(str).str.lower().str.startswith("c")
    cv=float(x.loc[is_call,"volume"].sum()); pv=float(x.loc[~is_call,"volume"].sum()); tv=cv+pv
    calls=x.loc[is_call].groupby("strike")["volume"].sum(); puts=x.loc[~is_call].groupby("strike")["volume"].sum()
    top_call=None if calls.empty else float(calls.idxmax()); top_put=None if puts.empty else float(puts.idxmax())
    a=x.groupby("strike",as_index=False).agg(volume=("volume","sum"),oi=("open_interest","sum")); a["ratio"]=np.where(a["oi"]>0,a["volume"]/a["oi"],np.nan)
    hot=None
    ratios=pd.to_numeric(a["ratio"],errors="coerce").replace([np.inf,-np.inf],np.nan).dropna()
    if len(ratios):
        med=float(ratios.median()); mad=float((ratios-med).abs().median()); scale=max(1.4826*mad,1e-9)
        aa=a.dropna(subset=["ratio"]).copy(); aa["robust_z"]=(aa["ratio"]-med)/scale
        r=aa.sort_values(["robust_z","ratio"],ascending=False).iloc[0]
        hot={"strike":float(r["strike"]),"ratio":float(r["ratio"]),"robust_z":float(r["robust_z"]),"median_ratio":med}
    totals=[]
    for ts in timestamps[-4:]:
        g=enr[enr["timestamp"]==ts]; totals.append(float(numeric_column(g,"volume",0).sum()))
    latest_increment=float("nan"); acceleration=float("nan")
    if len(totals)>=2: latest_increment=max(0.0,totals[-1]-totals[-2])
    if len(totals)>=3:
        prev_increment=max(0.0,totals[-2]-totals[-3]); acceleration=100.0*(latest_increment-prev_increment)/max(prev_increment,1.0)
    return {"call_volume":cv,"put_volume":pv,"total_volume":tv,"put_call_volume_ratio":pv/max(cv,1.0),"top_call_strike":top_call,"top_put_strike":top_put,"unusual":hot,
            "latest_volume_increment":latest_increment,"volume_acceleration_pct":acceleration,
            "note":"Hotspot Z usa una mediana/MAD robusta dentro del snapshot actual. La aceleración compara incrementos entre snapshots; no se presenta como RVOL histórico hasta acumular sesiones comparables."}
