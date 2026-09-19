from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from .alpaca_data import load_settings, _headers, DATA_BASE, DATA_DIR
from app.persistence import PERSISTENT_ROOT, routed_dir
from .frame_guards import numeric_column, text_column
from .obs import note as _obs_note
from .liquidity_zones import liquidity_zones

EC = ZoneInfo("America/Guayaquil")
NY = ZoneInfo("America/New_York")
BASE = Path(__file__).resolve().parent
STORE = routed_dir(PERSISTENT_ROOT, "research")
STORE.mkdir(parents=True, exist_ok=True)
EXCHANGE_CACHE = STORE / "alpaca_stock_exchanges.json"
_ADV_CACHE: Dict[str, tuple[datetime, Optional[float]]] = {}


def _path(d: date | None = None, symbol: str = "DIA") -> Path:
    d = d or datetime.now(EC).date()
    sym=str(symbol).upper().strip().lower()
    return STORE / f"{sym}_large_prints_{d.isoformat()}.csv"


def _get_json(url: str, params: Optional[dict] = None, timeout: float = 15.0) -> Dict[str, Any]:
    s = load_settings()
    if not s:
        raise RuntimeError("Alpaca no está configurado")
    r = requests.get(url, headers=_headers(s), params=params or {}, timeout=timeout)
    if r.status_code >= 400:
        try: msg = r.json().get("message") or r.text[:300]
        except Exception: msg = r.text[:300]
        raise RuntimeError(f"Alpaca HTTP {r.status_code}: {msg}")
    return r.json()


def _exchange_map(force: bool = False) -> Dict[str, str]:
    if not force and EXCHANGE_CACHE.exists():
        try: return json.loads(EXCHANGE_CACHE.read_text(encoding="utf-8"))
        except Exception as _e:
            _obs_note('large_prints:47', _e)
    js = _get_json(f"{DATA_BASE}/v2/stocks/meta/exchanges")
    out: Dict[str, str] = {}
    rows = js if isinstance(js, list) else js.get("exchanges", js.get("data", [])) if isinstance(js, dict) else []
    if isinstance(rows, dict):
        for k,v in rows.items(): out[str(k)] = str(v.get("name", v) if isinstance(v,dict) else v)
    else:
        for r in rows or []:
            if not isinstance(r, dict): continue
            code = r.get("code") or r.get("id") or r.get("exchange")
            name = r.get("name") or r.get("description") or code
            if code: out[str(code)] = str(name)
    try: EXCHANGE_CACHE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    except Exception as _e:
        _obs_note('large_prints:60', _e)
    return out


# Códigos de venue del SIP consolidado (CTA/UTP) que identifican una impresión
# FUERA de bolsa. `D` es FINRA ADF: por ahí se reportan las operaciones de dark pool
# y de internalizadores, y es el marcador canónico que usa todo el sector.
# El código viene en el propio trade, no depende de ningún catálogo externo.
OFF_EXCHANGE_CODES = {"D"}


def _is_off_exchange(name: str, code: str, tape: str) -> bool:
    """¿Esta impresión se ejecutó fuera de bolsa?

    v1.42.4 · ANTES ESTO DEVOLVÍA SIEMPRE FALSE
    -------------------------------------------
    La única prueba aceptada era que el NOMBRE del venue contuviera FINRA/TRF/ADF/OTC.
    Ese nombre sale de `_exchange_map()`, que consulta `/v2/stocks/meta/exchanges`. Si
    esa llamada falla, no está en el plan, o devuelve una forma que el parser no
    reconoce, el mapa queda vacío y entonces:

        name = exmap.get(code, code)   ->  name == "D"
        _is_off_exchange("D", "D", "A") -> False

    Resultado: cero prints off-exchange, en TODOS los activos, para siempre, y la
    sección Dark Pool en blanco sin un solo error que lo explicara. El dato estaba
    llegando en cada trade —el código de venue— y no se miraba.

    El código del SIP es autoritativo por sí mismo y no depende de catálogo alguno,
    así que ahora es la prueba primaria. El nombre se mantiene como confirmación
    adicional cuando el catálogo sí está disponible.
    """
    c = str(code or "").strip().upper()
    if c in OFF_EXCHANGE_CODES:
        return True
    n = (name or "").upper()
    if any(k in n for k in ("FINRA", "TRF", "ADF", "OTC", "TRADE REPORT")):
        return True
    # Señal heredada: se conserva para que la corrección sea estrictamente aditiva.
    return str(tape or "").strip().upper() == "O"


def _load(d: date | None = None, symbol: str = "DIA") -> pd.DataFrame:
    p = _path(d, symbol)
    if not p.exists(): return pd.DataFrame()
    try:
        df = pd.read_csv(p)
        if "timestamp" in df: df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        return df
    except Exception: return pd.DataFrame()


def _save_append(new: pd.DataFrame, symbol: str = "DIA") -> pd.DataFrame:
    if new.empty: return _score_events(_load(symbol=symbol), symbol)
    old = _load(symbol=symbol)
    all_df = pd.concat([old, new], ignore_index=True) if not old.empty else new.copy()
    key = "trade_id" if "trade_id" in all_df.columns else None
    if key: all_df = all_df.drop_duplicates(key, keep="last")
    else: all_df = all_df.drop_duplicates(["timestamp","price","size","exchange"], keep="last")
    all_df = all_df.sort_values("timestamp")
    all_df.to_csv(_path(symbol=symbol), index=False)
    return all_df


def _score_events(df: pd.DataFrame, symbol: str = "DIA", anchor: Dict[str, Any] | None = None) -> pd.DataFrame:
    """v1.15.7 Large Prints score: historical anchor + intraday context, 0–100.

    Early-session ranks are shrunk toward neutral so five prints cannot manufacture a
    percentile-100 event. When >=3 completed sessions exist, the historical notional
    anchor dominates early and the current-session rank gains weight gradually.
    """
    if df.empty:return df
    x=df.copy();x["notional"]=numeric_column(x,"notional",0).clip(lower=0)
    # Keep the former score for audit/comparison.
    logn=np.log1p(x["notional"]);med=float(logn.median());mad=float((logn-med).abs().median())
    if mad>1e-9:z=0.67448975*(logn-med)/mad
    else:
        sd=float(logn.std(ddof=0));z=(logn-float(logn.mean()))/(sd if sd>1e-9 else 1.0)
    x["z_notional"]=z.clip(-10,10)
    rank=x["notional"].rank(pct=True).fillna(.5)
    off_flag=x.get("off_exchange_confirmed",pd.Series(False,index=x.index))
    if off_flag.dtype!=bool:off_flag=off_flag.astype(str).str.lower().isin({"true","1","yes"})
    off_num=off_flag.astype(float).to_numpy()
    x["off_exchange_confirmed"]=off_flag
    legacy=45*rank+25*np.clip(np.maximum(x["z_notional"],0)/4,0,1)+20*off_num
    # `conditions` es opcional: no todos los tapes la traen, y su ausencia no puede
    # tumbar la sección DARK POOL entera.
    cond=text_column(x,"conditions","");special=cond.str.contains("B|C|P|W|Z",regex=True)
    legacy+=np.where(special,3,8)
    x["q_print_legacy"]=np.clip(legacy,0,100)

    if anchor is None:
        try:
            from .scale_anchors import load_anchors
            anchor=(load_anchors(DATA_DIR,str(symbol).upper(),"LARGE_PRINTS") or {}).get("large_print_notional")
        except Exception:anchor=None
    hist=None
    try:
        from .scale_anchors import anchored_magnitude
        a=anchored_magnitude(x["notional"].to_numpy(float),anchor)
        if a is not None:hist=np.asarray(a,dtype=float)
    except Exception:hist=None
    n=max(int(len(x)),1);session_conf=min(n/30.0,1.0)
    rank_shrunk=.5+session_conf*(rank.to_numpy(float)-.5)
    if hist is not None and len(hist)==len(x):
        # 70% history / 30% current at the open -> 30% history / 70% current later.
        w_session=.30+.40*min(n/60.0,1.0)
        magnitude=(1.0-w_session)*hist+w_session*rank.to_numpy(float)
        anchor_ready=True;anchor_status="HISTORICAL + INTRADAY"
    else:
        magnitude=rank_shrunk;anchor_ready=False;anchor_status="COLLECTING · INTRADAY SHRUNK"
        w_session=session_conf
    z_s=np.clip(np.maximum(x["z_notional"].to_numpy(float),0)/4,0,1)
    quality=np.where(special.to_numpy(),.35,1.0)
    # Exactly 100 theoretical points: magnitude 55 + robust-z 20 + venue 20 + quality 5.
    q=55*magnitude+20*z_s+20*off_num+5*quality
    x["q_print"]=np.clip(q,0,100)
    x["q_print_anchor_score"]=hist if hist is not None and len(hist)==len(x) else np.nan
    x["q_print_intraday_rank"]=rank.to_numpy(float)
    x["q_print_intraday_weight"]=float(w_session)
    x["q_print_anchor_ready"]=bool(anchor_ready)
    x["q_print_status"]=anchor_status
    x["class"]=np.where(x["off_exchange_confirmed"],"OFF-EXCHANGE CONFIRMADO","LARGE PRINT")
    return x


def large_print_anchor_samples(df: pd.DataFrame) -> Dict[str,list[float]]:
    if df is None or df.empty:return {}
    vals=numeric_column(df,"notional",float("nan")).replace([np.inf,-np.inf],np.nan).dropna()
    vals=vals[vals>0]
    return {"large_print_notional":vals.tolist()} if len(vals) else {}


def fetch_recent_large_prints(lookback_seconds: int = 120, min_notional: float = 750_000.0,
                              min_shares: int = 2500, symbol: str = "DIA") -> pd.DataFrame:
    s = load_settings()
    if not s: return _load(symbol=symbol)
    now = datetime.now(tz=NY)
    start = now - timedelta(seconds=max(30, int(lookback_seconds)))
    params = {
        "start": start.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00","Z"),
        "end": now.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00","Z"),
        "limit": 10000, "feed": s.stock_feed, "sort": "asc",
    }
    rows = []
    token = None
    for _ in range(6):
        p = dict(params)
        if token: p["page_token"] = token
        js = _get_json(f"{DATA_BASE}/v2/stocks/{str(symbol).upper().strip()}/trades", p)
        rows.extend(js.get("trades", []) if isinstance(js, dict) else [])
        token = js.get("next_page_token") if isinstance(js, dict) else None
        if not token: break
    if not rows: return _score_events(_load(symbol=symbol), symbol)
    exmap = _exchange_map()
    out = []
    for r in rows:
        price = float(r.get("p", r.get("price", 0)) or 0)
        size = int(r.get("s", r.get("size", 0)) or 0)
        if price <= 0 or size <= 0: continue
        notional = price*size
        if notional < min_notional and size < min_shares: continue
        code = str(r.get("x", r.get("exchange", "")) or "")
        tape = str(r.get("z", r.get("tape", "")) or "")
        name = exmap.get(code, code or "UNKNOWN")
        conditions = r.get("c", r.get("conditions", [])) or []
        if isinstance(conditions, str): conditions = [conditions]
        out.append({
            "timestamp": r.get("t", r.get("timestamp")),
            "underlying_symbol": str(symbol).upper(),
            "trade_id": r.get("i", r.get("id")),
            "price": price, "size": size, "notional": notional,
            "exchange": code, "exchange_name": name, "tape": tape,
            "conditions": ",".join(map(str, conditions)),
            "off_exchange_confirmed": _is_off_exchange(name, code, tape),
        })
    new = pd.DataFrame(out)
    if new.empty: return _score_events(_load(symbol=symbol), symbol)
    new["timestamp"] = pd.to_datetime(new["timestamp"], errors="coerce", utc=True).dt.tz_convert(EC)
    all_df = _save_append(new, symbol)
    all_df = _score_events(all_df, symbol)
    all_df.to_csv(_path(symbol=symbol), index=False)
    return all_df



def _average_daily_volume(symbol: str, sessions: int = 20) -> Optional[float]:
    """Average daily share volume from recent completed SIP sessions. Cached for 30 min.

    This is contextual sizing only; failure never blocks Large Prints.
    """
    sym=str(symbol).upper().strip(); now=datetime.now(tz=NY)
    cached=_ADV_CACHE.get(sym)
    if cached and (now-cached[0]).total_seconds()<1800:
        return cached[1]
    try:
        start=(now.date()-timedelta(days=max(40,sessions*3))).isoformat()
        end=(now.date()-timedelta(days=1)).isoformat()
        sett=load_settings()
        params={"timeframe":"1Day","start":start,"end":end,"limit":1000,"adjustment":"raw"}
        if sett: params["feed"]=sett.stock_feed
        js=_get_json(f"{DATA_BASE}/v2/stocks/{sym}/bars",params)
        vals=[]
        for b in (js.get("bars",[]) if isinstance(js,dict) else []):
            try:
                v=float(b.get("v",b.get("volume",0)) or 0)
                if v>0: vals.append(v)
            except Exception as _e:
                _obs_note('large_prints:233', _e)
        adv=float(np.mean(vals[-int(sessions):])) if vals else None
    except Exception:
        adv=None
    _ADV_CACHE[sym]=(now,adv)
    return adv


def _repeated_price_zones(df: pd.DataFrame, max_zones: int = 8) -> list[Dict[str, Any]]:
    if df is None or df.empty:return []
    x=df.copy();x["price"]=numeric_column(x,"price",float("nan"));x["notional"]=numeric_column(x,"notional",0);x=x.dropna(subset=["price"])
    if x.empty:return []
    med=float(x["price"].median());step=max(0.05, med*0.0002)
    x["_zone"]=(x["price"]/step).round()*step
    g=x.groupby("_zone",as_index=False).agg(events=("price","size"),notional=("notional","sum"),largest=("notional","max"),last_time=("timestamp","max"))
    g=g[g["events"]>=2].sort_values(["events","notional"],ascending=False).head(int(max_zones))
    return [{"price_zone":round(float(r["_zone"]),4),"events":int(r["events"]),"notional":float(r["notional"]),"largest":float(r["largest"]),"last_time":pd.Timestamp(r["last_time"]).isoformat() if pd.notna(r["last_time"]) else None,"zone_width":round(step,4)} for _,r in g.iterrows()]

def load_large_prints(d: date | None = None, symbol: str = "DIA") -> pd.DataFrame:
    raw=_load(d,symbol)
    # Replay must never be rescored with anchors learned after that historical date.
    # If the file already contains the score created in its own session, preserve it.
    if d is not None and d < datetime.now(EC).date() and "q_print" in raw.columns:
        return raw
    return _score_events(raw,symbol,anchor={} if d is not None and d < datetime.now(EC).date() else None)


def large_print_summary(df: pd.DataFrame, symbol: str = "DIA", adv_shares: Optional[float] = None) -> Dict[str, Any]:
    if df is None or df.empty:
        return {"count":0,"off_exchange_count":0,"total_notional":0.0,"off_exchange_notional":0.0,
                "largest":None,"top":[],"off_exchange_largest":None,"off_exchange_top":[],
                "adv_shares":None,"repeated_zones":[],
                "liquidity_zones":{"ready":False,"zones":[],"reason":"NO_PRINTS"},
                "off_exchange_liquidity_zones":{"ready":False,"zones":[],"reason":"NO_OFF_EXCHANGE_PRINTS"}}
    x = (df.copy() if "q_print" in df.columns else _score_events(df,symbol)).sort_values("notional",ascending=False)
    try:
        adv=float(adv_shares) if adv_shares is not None and math.isfinite(float(adv_shares)) and float(adv_shares)>0 else None
    except Exception:
        adv=None
    if adv is None:
        adv=_average_daily_volume(symbol)
    def pack(r):
        size=int(r["size"]);pct_adv=(100.0*size/adv) if adv and adv>0 else None
        return {
            "timestamp": pd.Timestamp(r["timestamp"]).isoformat() if pd.notna(r["timestamp"]) else None,
            "price": float(r["price"]), "size": size, "notional": float(r["notional"]),
            "q_print": float(r.get("q_print",0)), "q_print_legacy": float(r.get("q_print_legacy",0)), "z_notional": float(r.get("z_notional",0)),
            "q_print_status": str(r.get("q_print_status","COLLECTING")), "q_print_anchor_ready": bool(r.get("q_print_anchor_ready",False)),
            "q_print_anchor_score": None if pd.isna(r.get("q_print_anchor_score")) else float(r.get("q_print_anchor_score")),
            "q_print_intraday_rank": float(r.get("q_print_intraday_rank",0.5)),
            "session_percentile": float((x["notional"]<=float(r["notional"])).mean()*100), "pct_adv": None if pct_adv is None else float(pct_adv),
            "exchange": str(r.get("exchange","")), "exchange_name": str(r.get("exchange_name","")),
            "tape": str(r.get("tape","")), "conditions": str(r.get("conditions","")),
            "off_exchange_confirmed": bool(r.get("off_exchange_confirmed",False)), "class": str(r.get("class","LARGE PRINT")),
        }
    off = x[x["off_exchange_confirmed"] == True]
    try:
        _spot=float(numeric_column(x,"price",float("nan")).dropna().iloc[-1]) if len(x) else None
    except Exception:
        _spot=None
    lz=liquidity_zones(x,_spot,max_each_side=3)
    off_lz=(liquidity_zones(off,_spot,max_each_side=3) if len(off)
            else {"ready":False,"zones":[],"reason":"NO_OFF_EXCHANGE_PRINTS",
                  "authority":"CONFIRMED_OFF_EXCHANGE_ONLY"})
    return {
        "count": int(len(x)), "off_exchange_count": int(len(off)),
        "total_notional": float(x["notional"].sum()), "off_exchange_notional": float(off["notional"].sum()) if len(off) else 0.0,
        "adv_shares": None if adv is None else float(adv), "repeated_zones": _repeated_price_zones(x), "liquidity_zones": lz,
        "off_exchange_liquidity_zones": off_lz,
        "largest": pack(x.iloc[0]) if len(x) else None,
        "top": [pack(r) for _,r in x.head(20).iterrows()],
        "off_exchange_largest": pack(off.iloc[0]) if len(off) else None,
        "off_exchange_top": [pack(r) for _,r in off.head(20).iterrows()],
        "source_note": f"SIP trades de {str(symbol).upper()}. Q-Print combina ancla histórica de notional con ranking intradía que gana peso conforme crece la muestra; sin ancla, el ranking temprano se encoge hacia neutral. Tamaño relativo usa ADV cuando Alpaca lo permite. Liquidity Zones agrupa prints observados con ancho adaptativo propio de ITM; BLOCK/CARPET describe concentración, no identidad ni intención. 'Off-exchange confirmado' solo se usa cuando exchange/tape lo identifica.",
    }


def large_print_chart(df: pd.DataFrame, price_history: pd.DataFrame | None = None, symbol: str = "UNKNOWN"):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[.64,.36], vertical_spacing=.08,
                        subplot_titles=(f"{str(symbol).upper()} + LARGE PRINTS / OFF-EXCHANGE", "NOTIONAL DE PRINTS"))
    if price_history is not None and not price_history.empty:
        px = price_history[["timestamp","underlying_price"]].copy()
        px["timestamp"] = pd.to_datetime(px["timestamp"], errors="coerce")
        px = px.dropna().drop_duplicates("timestamp").sort_values("timestamp")
        fig.add_trace(go.Scatter(x=px["timestamp"],y=px["underlying_price"],mode="lines",name=str(symbol).upper(),line=dict(width=2.3,color="#dce7f3"),meta={"role":"UNDERLYING_PRICE"}),row=1,col=1)
    if df is not None and not df.empty:
        sym=str(df.get("underlying_symbol",pd.Series(["DIA"])).iloc[-1] if "underlying_symbol" in df.columns else "DIA")
        x = df.copy() if "q_print" in df.columns else _score_events(df.copy(),sym)
        x["timestamp"] = pd.to_datetime(x["timestamp"], errors="coerce")
        x = x.dropna(subset=["timestamp"])
        if not x.empty:
            label = np.where(x["off_exchange_confirmed"], "OFF-EX", "PRINT")
            txt = [f"{a} ${n/1e6:.1f}M · QP{q:.0f}" for a,n,q in zip(label,x["notional"],x["q_print"])]
            custom=np.array([[lab, float(n)/1e6, float(q)] for lab,n,q in zip(label,x["notional"],x["q_print"])],dtype=object)
            fig.add_trace(go.Scatter(x=x["timestamp"],y=x["price"],mode="markers",name="Prints",
                                     customdata=custom,hovertemplate="%{customdata[0]}<br>Precio %{y:.2f}<br>Notional $%{customdata[1]:.2f}M<br>Q-Print %{customdata[2]:.0f}<extra></extra>",
                                     meta={"role":"PRINT_EVENT"},
                                     marker=dict(size=np.clip(8+x["q_print"]/7,9,18),color=np.where(x["off_exchange_confirmed"],"#f1c84c","#36b6ff"),symbol=np.where(x["off_exchange_confirmed"],"diamond","circle"))),row=1,col=1)
            fig.add_trace(go.Bar(x=x["timestamp"],y=x["notional"]/1e6,name="Notional $M",meta={"role":"PRINT_NOTIONAL"},marker_color=np.where(x["off_exchange_confirmed"],"#f1c84c","#36b6ff")),row=2,col=1)
            try:
                _spot=float(px["underlying_price"].dropna().iloc[-1]) if price_history is not None and not price_history.empty and 'px' in locals() and not px.empty else float(x["price"].iloc[-1])
                _lz=liquidity_zones(x,_spot,max_each_side=3)
                for z in (_lz.get("zones") or []):
                    lo=float(z["low"]);hi=float(z["high"]);center=float(z["center"]);score=float(z.get("score",0))
                    fill="rgba(35,209,139,.10)" if center>=_spot else "rgba(255,77,109,.10)"
                    line="#23d18b" if center>=_spot else "#ff4d6d"
                    fig.add_hrect(y0=lo,y1=hi,fillcolor=fill,line_width=0,row=1,col=1)
                    fig.add_hline(y=center,line_color=line,line_width=1,line_dash="dot",annotation_text=f"{z.get('type')} {center:.2f} · {score:.0f}",annotation_position="right",row=1,col=1)
            except Exception as _e:
                _obs_note('large_prints:liquidity_zones_chart', _e)
    fig.update_layout(template="plotly_dark",height=650,hovermode="x unified",dragmode="pan",uirevision=f"large-prints-{str(symbol).upper()}",paper_bgcolor="#080b10",plot_bgcolor="#0c1118",margin=dict(l=55,r=30,t=65,b=45),legend=dict(orientation="h"),meta={"renderer_intent":"LINE_TERMINAL_TRACE_STYLE","liquidity_zones":True})
    fig.update_yaxes(title_text=str(symbol).upper(),row=1,col=1);fig.update_yaxes(title_text="$M",row=2,col=1)
    return fig
