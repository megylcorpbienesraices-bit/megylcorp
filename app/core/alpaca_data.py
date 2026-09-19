from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter, sleep
import json
import math
import os
import re

import pandas as pd
from .precision_engine import recover_iv_and_greeks
import requests
from app.persistence import PERSISTENT_ROOT, routed_dir
from .obs import note as _obs_note
from .expiry_clock import dte_days_from_expiry

BASE = Path(__file__).resolve().parent
DATA_DIR = PERSISTENT_ROOT
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = BASE.parent.parent / ".env"
GLOBAL_DIR = Path.home() / ".itm_quant_gamma"
GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
GLOBAL_ENV_FILE = GLOBAL_DIR / "alpaca.env"
EC = ZoneInfo("America/Guayaquil")
NY = ZoneInfo("America/New_York")

DATA_BASE = "https://data.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets"


@dataclass
class AlpacaSettings:
    api_key: str
    secret_key: str
    stock_feed: str = "sip"
    option_feed: str = "opra"


def _parse_env(path: Path = ENV_FILE) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        _obs_note("alpaca_data:env_unreadable", exc, severity="CRITICAL_DATA")
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _discover_previous_env() -> Optional[Path]:
    # Bounded migration helper: only inspect nearby ITM QUANT version folders, never the whole user profile.
    roots = [BASE.parent.parent, BASE.parent.parent.parent]
    patterns = [
        "ITM_QUANT_GAMMA_SUITE_v0.*/*/.env",
        "ITM_QUANT_GAMMA_SUITE_v0.*/ITM_QUANT_GAMMA_SUITE_v0.*/*/.env",
        "*ITM_QUANT_GAMMA*/*/.env",
    ]
    seen = set()
    for root in roots:
        try:
            root = root.resolve()
        except Exception as _e:
            _obs_note('alpaca_data:72', _e)
            continue
        if root in seen or not root.exists():
            continue
        seen.add(root)
        for pattern in patterns:
            try:
                candidates = list(root.glob(pattern))
            except Exception:
                candidates = []
            for cand in candidates:
                if cand == ENV_FILE or cand == GLOBAL_ENV_FILE:
                    continue
                env = _parse_env(cand)
                if env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY"):
                    return cand
    return None


def load_settings() -> Optional[AlpacaSettings]:
    env = {}
    for candidate in (ENV_FILE, GLOBAL_ENV_FILE):
        e = _parse_env(candidate)
        if e.get("ALPACA_API_KEY") and e.get("ALPACA_SECRET_KEY"):
            env = e
            break
    if not env:
        prev = _discover_previous_env()
        if prev:
            env = _parse_env(prev)
            try:
                GLOBAL_ENV_FILE.write_text(prev.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception as _e:
                _obs_note('alpaca_data:103', _e)
    key = os.getenv("ALPACA_API_KEY") or env.get("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY") or env.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    return AlpacaSettings(
        api_key=key,
        secret_key=secret,
        stock_feed=(os.getenv("ALPACA_STOCK_FEED") or env.get("ALPACA_STOCK_FEED", "sip")),
        option_feed=(os.getenv("ALPACA_OPTIONS_FEED") or env.get("ALPACA_OPTIONS_FEED", "opra")),
    )


def save_settings(api_key: str, secret_key: str) -> str:
    api_key = (api_key or "").strip()
    secret_key = (secret_key or "").strip()
    if len(api_key) < 8 or len(secret_key) < 12:
        raise ValueError("La API Key o Secret Key parecen incompletas.")
    text = (
        "# ITM QUANT GAMMA - credenciales locales. NO COMPARTIR ESTE ARCHIVO.\n"
        f"ALPACA_API_KEY={api_key}\n"
        f"ALPACA_SECRET_KEY={secret_key}\n"
        "ALPACA_STOCK_FEED=sip\n"
        "ALPACA_OPTIONS_FEED=opra\n"
    )
    ENV_FILE.write_text(text, encoding="utf-8")
    GLOBAL_ENV_FILE.write_text(text, encoding="utf-8")
    for target in (ENV_FILE, GLOBAL_ENV_FILE):
        try:
            os.chmod(target, 0o600)
        except Exception as _e:
            _obs_note('alpaca_data:134', _e)
    return str(GLOBAL_ENV_FILE)


def masked_settings() -> str:
    s = load_settings()
    if not s:
        return "NO CONFIGURADO"
    key = s.api_key
    masked = (key[:4] + "••••" + key[-4:]) if len(key) >= 10 else "••••••••"
    return f"CONFIGURADO · Key {masked} · Stocks {s.stock_feed.upper()} · Options {s.option_feed.upper()}"


def _headers(s: AlpacaSettings) -> Dict[str, str]:
    return {
        "APCA-API-KEY-ID": s.api_key,
        "APCA-API-SECRET-KEY": s.secret_key,
        "Accept": "application/json",
        "User-Agent": "ITM-QUANT-GAMMA/0.8",
    }


def _get_json(url: str, s: AlpacaSettings, params: Optional[dict] = None, timeout: float = 20.0) -> Dict[str, Any]:
    """GET JSON with bounded retry for transient Alpaca edge/backend failures.

    401/403 and other deterministic client errors are never retried.  The retry budget
    only covers rate-limit/transient gateway/server failures and transport timeouts so a
    momentary 504 during multi-expiry startup cannot immediately degrade the whole Quant
    engine.  This is deliberately small and synchronous because callers already execute
    in worker threads.
    """
    try:
        attempts=max(1,min(int(os.getenv("ITM_QUANT_ALPACA_RETRY_ATTEMPTS","3")),4))
    except Exception:
        attempts=3
    transient_codes={408,425,429,500,502,503,504}
    last_exc=None
    for attempt in range(attempts):
        try:
            r = requests.get(url, headers=_headers(s), params=params or {}, timeout=timeout)
            if r.status_code >= 400:
                try:
                    body = r.json()
                    msg = body.get("message") or body.get("error") or json.dumps(body)[:400]
                except Exception:
                    msg = r.text[:400]
                err=RuntimeError(f"Alpaca HTTP {r.status_code}: {msg}")
                if r.status_code in transient_codes and attempt < attempts-1:
                    last_exc=err
                    sleep(min(1.5,0.35*(2**attempt)))
                    continue
                raise err
            data = r.json()
            break
        except requests.exceptions.RequestException as exc:
            last_exc=exc
            if attempt >= attempts-1:
                raise RuntimeError(f"Alpaca transport timeout/error: {type(exc).__name__}: {str(exc)[:180]}") from exc
            sleep(min(1.5,0.35*(2**attempt)))
    else:
        raise RuntimeError(f"Alpaca transient request failure after {attempts} attempts: {last_exc}")
    # v1.25.15 COLD archive: every market/instrument response actually requested from
    # Alpaca is persisted asynchronously. Credentials/headers are never archived.
    try:
        from .provider_data_lake import DATA_LAKE
        par = params or {}
        symbol = str(par.get("underlying_symbols") or "").upper().strip()
        if not symbol:
            m = re.search(r"/(?:stocks|snapshots)/([^/?]+)", url)
            symbol = str(m.group(1)).upper() if m else "UNIVERSE"
        evt = "REST_RESPONSE"
        if "/options/contracts" in url: evt = "OPTION_CONTRACT_CATALOG"
        elif "/options/snapshots/" in url: evt = "OPTION_SNAPSHOT"
        elif "/bars" in url: evt = "BAR"
        elif "/snapshot" in url: evt = "SNAPSHOT"
        DATA_LAKE.archive_raw(source="ALPACA_REST", symbol=symbol, event_type=evt, payload=data,
                              metadata={"path": url.split("?")[0], "params": par})
    except Exception as _e:
        _obs_note('alpaca_data:212', _e)
    return data


def _first_num(*vals, default=float("nan")) -> float:
    for v in vals:
        if v is None:
            continue
        try:
            x = float(v)
            if math.isfinite(x):
                return x
        except Exception as _e:
            _obs_note('alpaca_data:226', _e)
            continue
    return float(default)


def _get(d: Dict[str, Any], *names, default=None):
    for n in names:
        if isinstance(d, dict) and n in d:
            return d[n]
    return default


def _stock_spot_from_snapshot(js: Dict[str, Any]) -> Tuple[float, str]:
    lt = _get(js, "latestTrade", "latest_trade", default={}) or {}
    lq = _get(js, "latestQuote", "latest_quote", default={}) or {}
    mb = _get(js, "minuteBar", "minute_bar", default={}) or {}
    db = _get(js, "dailyBar", "daily_bar", default={}) or {}
    trade = _first_num(_get(lt, "p", "price"))
    bid = _first_num(_get(lq, "bp", "bid_price"))
    ask = _first_num(_get(lq, "ap", "ask_price"))
    mid = (bid + ask) / 2.0 if math.isfinite(bid) and math.isfinite(ask) and ask > 0 and bid > 0 else float("nan")
    bar = _first_num(_get(mb, "c", "close"), _get(db, "c", "close"))

    # v1.42.1 hotfix · En premarket un último trade puede llevar decenas de
    # segundos sin cambiar mientras el NBBO sigue actualizándose. La versión
    # anterior prefería siempre ``latestTrade`` y, por tanto, también heredaba su
    # timestamp antiguo: el gate de 20 s retenía perfiles/heatmap/niveles aunque
    # Alpaca siguiera entregando cotización fresca. Elegimos la observación de
    # precio MÁS RECIENTE entre trade y midpoint; en empate preservamos el trade.
    def _epoch(value: Any) -> float | None:
        try:
            ts = pd.Timestamp(value)
            if pd.isna(ts):
                return None
            return float(ts.timestamp())
        except Exception:
            return None

    candidates = []
    trade_ts = str(_get(lt, "t", "timestamp", default="") or "")
    quote_ts = str(_get(lq, "t", "timestamp", default="") or "")
    if math.isfinite(trade) and trade > 0:
        candidates.append((trade, trade_ts, _epoch(trade_ts), 1))
    if math.isfinite(mid) and mid > 0:
        candidates.append((mid, quote_ts, _epoch(quote_ts), 0))
    timestamped = [c for c in candidates if c[2] is not None]
    if timestamped:
        price, market_ts, _, _ = max(timestamped, key=lambda c: (c[2], c[3]))
        return float(price), market_ts
    # Si el proveedor omitió timestamps, se conserva la prioridad histórica:
    # trade → midpoint → barra. No se inventa frescura.
    if math.isfinite(trade) and trade > 0:
        return trade, trade_ts
    if math.isfinite(mid) and mid > 0:
        return mid, quote_ts
    if math.isfinite(bar) and bar > 0:
        return bar, str(_get(mb, "t", "timestamp", default=_get(db, "t", "timestamp", default="")))
    raise RuntimeError("Alpaca respondió el snapshot, pero no pude extraer un precio válido.")


def fetch_stock_snapshot(s: Optional[AlpacaSettings] = None, symbol: str = "DIA") -> Dict[str, Any]:
    s = s or load_settings()
    if not s:
        raise RuntimeError("Primero guarda tus credenciales Alpaca en la pestaña ALPACA LIVE.")
    symbol = str(symbol).upper().strip()
    js = _get_json(f"{DATA_BASE}/v2/stocks/{symbol}/snapshot", s, {"feed": s.stock_feed})
    spot, market_ts = _stock_spot_from_snapshot(js)
    return {"spot": spot, "market_timestamp": market_ts, "raw": js}



def _session_bar_cache_path(symbol: str, day: date, scope: str) -> Path:
    cache_dir = routed_dir(DATA_DIR, "sessions") / "price_bootstrap"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"alpaca_{str(symbol).lower()}_{day.isoformat()}_{scope}_1m.csv"

def _session_bar_cache_meta_path(symbol: str, day: date, scope: str) -> Path:
    return _session_bar_cache_path(symbol,day,scope).with_suffix(".meta.json")


def _normalize_stock_bar_frame(frame: pd.DataFrame, *, day: date, start_ny: datetime, end_ny: datetime, mins: int) -> pd.DataFrame:
    """Normalize observed Alpaca bars into the TRACE OHLC contract.

    The function never creates price observations.  Larger timeframes only aggregate
    already observed 1-minute bars.  Timestamps are returned in Ecuador local clock
    (naive) because the LIVE SIP tape uses that same terminal clock contract.
    """
    cols=["timestamp", "open", "high", "low", "close", "volume", "trades", "vwap"]
    x=frame.copy() if isinstance(frame,pd.DataFrame) else pd.DataFrame()
    if x.empty:
        return pd.DataFrame(columns=cols)
    x=x.rename(columns={"t":"timestamp","o":"open","h":"high","l":"low","c":"close","v":"volume","n":"trades","vw":"vwap"})
    x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce", utc=True)
    for col in ("open","high","low","close","volume","trades","vwap"):
        if col not in x.columns:
            x[col] = 0.0
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.dropna(subset=["timestamp","open","high","low","close"]).sort_values("timestamp")
    if x.empty:
        return pd.DataFrame(columns=cols)
    ny_ts = x["timestamp"].dt.tz_convert(NY)
    mask = (ny_ts.dt.date == day) & (ny_ts >= pd.Timestamp(start_ny)) & (ny_ts <= pd.Timestamp(end_ny))
    x = x.loc[mask].copy()
    if x.empty:
        return pd.DataFrame(columns=cols)
    x["timestamp"] = x["timestamp"].dt.tz_convert(EC).dt.tz_localize(None)
    if mins > 1:
        anchor_ec = pd.Timestamp(start_ny.astimezone(EC).replace(tzinfo=None))
        z = x.set_index("timestamp")
        rs = dict(rule=f"{mins}min", label="left", closed="left", origin=anchor_ec)
        volume=z["volume"].fillna(0)
        weighted=(z["vwap"].fillna(z["close"])*volume).resample(**rs).sum()
        volsum=volume.resample(**rs).sum()
        out = pd.DataFrame({
            "open": z["open"].resample(**rs).first(),
            "high": z["high"].resample(**rs).max(),
            "low": z["low"].resample(**rs).min(),
            "close": z["close"].resample(**rs).last(),
            "volume": volsum,
            "trades": z["trades"].fillna(0).resample(**rs).sum(),
            "vwap": weighted / volsum.replace(0, pd.NA),
        }).dropna(subset=["open","high","low","close"]).reset_index()
        x = out
    return x[cols].reset_index(drop=True)


def load_cached_stock_session_bars(
    symbol: str = "DIA",
    timeframe: str = "1m",
    session_date: Optional[date | str] = None,
    session_scope: str = "rth",
) -> pd.DataFrame:
    """Fast disk-only bootstrap used before the provider request finishes.

    This is presentation continuity only.  The cache contains previously observed
    Alpaca bars; if absent, an empty frame is returned immediately.
    """
    symbol=str(symbol or "DIA").upper().strip()
    tf=str(timeframe or "1m").lower(); mins={"1m":1,"3m":3,"5m":5,"15m":15}.get(tf,1)
    scope=str(session_scope or "rth").strip().lower()
    if scope not in {"full_day","premarket_rth","rth"}: scope="rth"
    if isinstance(session_date,str):
        try: day=date.fromisoformat(session_date[:10])
        except Exception: day=datetime.now(NY).date()
    elif isinstance(session_date,date): day=session_date
    else: day=datetime.now(NY).date()
    scope_start={"full_day":time(0,0),"premarket_rth":time(4,0),"rth":time(9,30)}[scope]
    start_ny=datetime.combine(day,scope_start,NY); now_ny=datetime.now(NY)
    if day==now_ny.date(): end_ny=now_ny
    elif scope=="full_day": end_ny=datetime.combine(day,time(23,59,59),NY)
    else: end_ny=datetime.combine(day,time(16,0),NY)
    if scope in {"rth","premarket_rth"}: end_ny=min(end_ny,datetime.combine(day,time(16,0),NY))
    path=_session_bar_cache_path(symbol,day,scope)
    if not path.exists():
        out=pd.DataFrame(columns=["timestamp","open","high","low","close","volume","trades","vwap"])
        out.attrs.update({"source":"NO_CACHE","cache_hit":False,"requested_feed":None,"used_feed":None,"fallback":False,"diagnostic":"NO_LOCAL_PRICE_BOOTSTRAP_CACHE"})
        return out
    try: raw=pd.read_csv(path)
    except Exception as exc:
        out=pd.DataFrame(columns=["timestamp","open","high","low","close","volume","trades","vwap"])
        out.attrs.update({"source":"CACHE_READ_ERROR","cache_hit":False,"requested_feed":None,"used_feed":None,"fallback":False,"diagnostic":f"CACHE_READ_ERROR: {exc}"[:220]})
        return out
    out=_normalize_stock_bar_frame(raw,day=day,start_ny=start_ny,end_ny=end_ny,mins=mins)
    meta={}
    try:
        mp=_session_bar_cache_meta_path(symbol,day,scope)
        if mp.exists(): meta=json.loads(mp.read_text(encoding="utf-8")) or {}
    except Exception: meta={}
    used=str(meta.get("used_feed") or "cached").lower()
    fallback=bool(meta.get("fallback")) or used=="iex"
    source=str(meta.get("source") or ("ALPACA_IEX_HISTORICAL_DISPLAY_FALLBACK_CACHE" if fallback else "ALPACA_LOCAL_PRICE_CACHE"))
    out.attrs.update({"source":source,"cache_hit":True,"requested_feed":meta.get("requested_feed"),"used_feed":used,"fallback":fallback,"diagnostic":f"LOCAL_CACHE {len(out)} bars · {used.upper()}"})
    return out


def fetch_stock_session_bars(
    s: Optional[AlpacaSettings] = None,
    symbol: str = "DIA",
    timeframe: str = "1m",
    session_date: Optional[date | str] = None,
    session_scope: str = "rth",
    *,
    prefer_cache: bool = False,
    allow_display_fallback: bool = True,
    request_timeout: float = 8.0,
) -> pd.DataFrame:
    """Return observed Alpaca bars for one symbol and one NY calendar day.

    Provider order is intentionally explicit:
      1. optional fast local cache (previously observed Alpaca bars),
      2. configured Alpaca stock feed (normally SIP),
      3. optional Alpaca IEX *display-only* fallback when SIP historical access is
         unavailable.  The fallback is labelled and is never promoted into Scanner,
         Risk, signal logic or the quantitative source-of-truth.

    ``session_scope`` controls only the requested/display window; it never fabricates
    an exchange session.  The request uses explicit start/end times and follows Alpaca
    pagination.  Missing bars remain missing; no synthetic candles are generated.
    """
    s = s or load_settings()
    if not s:
        raise RuntimeError("Alpaca credentials not configured")
    symbol = str(symbol or "DIA").upper().strip()
    tf = str(timeframe or "1m").lower()
    mins = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(tf, 1)
    scope = str(session_scope or "rth").strip().lower()
    if scope not in {"full_day", "premarket_rth", "rth"}:
        scope = "rth"
    if isinstance(session_date, str):
        try: day = date.fromisoformat(session_date[:10])
        except Exception: day = datetime.now(NY).date()
    elif isinstance(session_date, date): day = session_date
    else: day = datetime.now(NY).date()

    scope_start = {"full_day": time(0, 0), "premarket_rth": time(4, 0), "rth": time(9, 30)}[scope]
    start_ny = datetime.combine(day, scope_start, NY)
    now_ny = datetime.now(NY)
    if day == now_ny.date(): end_ny = now_ny
    elif scope == "full_day": end_ny = datetime.combine(day, time(23, 59, 59), NY)
    else: end_ny = datetime.combine(day, time(16, 0), NY)
    if scope in {"rth", "premarket_rth"}: end_ny = min(end_ny, datetime.combine(day, time(16, 0), NY))
    if end_ny <= start_ny:
        out=pd.DataFrame(columns=["timestamp","open","high","low","close","volume","trades","vwap"])
        out.attrs.update({"source":"EMPTY_WINDOW","cache_hit":False,"requested_feed":s.stock_feed,"used_feed":None,"fallback":False,"diagnostic":"SESSION_WINDOW_NOT_STARTED"})
        return out

    cache_path=_session_bar_cache_path(symbol,day,scope)
    if prefer_cache and cache_path.exists():
        cached=load_cached_stock_session_bars(symbol,tf,day,scope)
        if not cached.empty:
            return cached

    base_params={
        "timeframe":"1Min",
        "start":start_ny.astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
        "end":end_ny.astimezone(timezone.utc).isoformat().replace("+00:00","Z"),
        "limit":10000,
        "adjustment":"raw",
        "sort":"asc",
    }
    attempts=[]
    feeds=[]
    primary=str(s.stock_feed or "sip").lower().strip() or "sip"
    feeds.append((primary,False))
    if allow_display_fallback and primary!="iex": feeds.append(("iex",True))
    raw_frame=pd.DataFrame(); used_feed=None; fallback=False
    for feed,is_fallback in feeds:
        rows=[]; token=None; error=None
        try:
            for page in range(20):
                params=dict(base_params,feed=feed)
                if token: params["page_token"]=token
                js=_get_json(f"{DATA_BASE}/v2/stocks/{symbol}/bars",s,params,timeout=float(request_timeout))
                batch=js.get("bars") or []
                if isinstance(batch,list): rows.extend(batch)
                token=js.get("next_page_token") or js.get("page_token")
                if not token: break
            attempts.append({"feed":feed,"ok":bool(rows),"rows":len(rows),"error":None})
        except Exception as exc:
            error=str(exc)[:220]
            attempts.append({"feed":feed,"ok":False,"rows":0,"error":error})
        if rows:
            raw_frame=pd.DataFrame(rows); used_feed=feed; fallback=is_fallback; break
    if not raw_frame.empty:
        source="ALPACA_SIP_HISTORICAL" if used_feed=="sip" else f"ALPACA_{str(used_feed).upper()}_HISTORICAL_DISPLAY_FALLBACK"
        try:
            # Keep the canonical cache as raw observed 1-minute bars so any timeframe can
            # be reconstructed later without information loss, plus a tiny provenance sidecar.
            raw_frame.to_csv(cache_path,index=False)
            _session_bar_cache_meta_path(symbol,day,scope).write_text(json.dumps({"source":source,"requested_feed":primary,"used_feed":used_feed,"fallback":bool(fallback),"saved_at":datetime.now(timezone.utc).isoformat()},ensure_ascii=False,indent=2),encoding="utf-8")
        except Exception as _e:
            _obs_note('alpaca_data:466', _e)
        out=_normalize_stock_bar_frame(raw_frame,day=day,start_ny=start_ny,end_ny=end_ny,mins=mins)
        out.attrs.update({
            "source":source,"cache_hit":False,"requested_feed":primary,"used_feed":used_feed,
            "fallback":bool(fallback),"attempts":attempts,"diagnostic":f"{source} {len(out)} bars",
            "start_ny":start_ny.isoformat(),"end_ny":end_ny.isoformat(),
        })
        return out

    # Provider failed or returned no observations.  A previous Alpaca cache is still a
    # truthful visual fallback and should not be erased by a transient provider error.
    if cache_path.exists():
        cached=load_cached_stock_session_bars(symbol,tf,day,scope)
        if not cached.empty:
            cached.attrs.update({"source":"ALPACA_LOCAL_PRICE_CACHE_AFTER_PROVIDER_FAILURE","requested_feed":primary,"used_feed":"cached","fallback":True,"attempts":attempts,"diagnostic":f"PROVIDER_UNAVAILABLE · CACHE {len(cached)} bars"})
            return cached
    detail="; ".join(f"{a['feed']}:{a.get('error') or 'NO_BARS'}" for a in attempts)[:420]
    out=pd.DataFrame(columns=["timestamp","open","high","low","close","volume","trades","vwap"])
    out.attrs.update({"source":"ALPACA_HISTORICAL_UNAVAILABLE","cache_hit":False,"requested_feed":primary,"used_feed":None,"fallback":False,"attempts":attempts,"diagnostic":detail or "NO_BARS"})
    return out

def _expiry_window(days: int) -> Tuple[str, str]:
    today_ny = datetime.now(NY).date()
    return today_ny.isoformat(), (today_ny + timedelta(days=max(int(days), 1))).isoformat()


def fetch_contracts(s: AlpacaSettings, spot: float, strike_window: float, expiry_days: int, symbol: str = "DIA") -> pd.DataFrame:
    start, end = _expiry_window(expiry_days)
    params = {
        "underlying_symbols": str(symbol).upper().strip(),
        "status": "active",
        "expiration_date_gte": start,
        "expiration_date_lte": end,
        "strike_price_gte": round(max(0.01, spot - float(strike_window)), 2),
        "strike_price_lte": round(spot + float(strike_window), 2),
        "limit": 10000,
    }
    rows = []
    token = None
    for _ in range(10):
        p = dict(params)
        if token:
            p["page_token"] = token
        js = _get_json(f"{PAPER_BASE}/v2/options/contracts", s, p)
        rows.extend(js.get("option_contracts", js.get("contracts", [])) or [])
        token = js.get("page_token") or js.get("next_page_token")
        if not token:
            break
    if not rows:
        raise RuntimeError(f"No encontré contratos {str(symbol).upper()} con los filtros actuales. Amplía el rango de strikes o vencimientos.")
    df = pd.DataFrame(rows)
    return df


def fetch_chain(s: AlpacaSettings, spot: float, strike_window: float, expiry_days: int, symbol: str = "DIA") -> Dict[str, Any]:
    start, end = _expiry_window(expiry_days)
    base_params = {
        "feed": s.option_feed,
        "limit": 1000,
        "strike_price_gte": round(max(0.01, spot - float(strike_window)), 2),
        "strike_price_lte": round(spot + float(strike_window), 2),
        "expiration_date_gte": start,
        "expiration_date_lte": end,
    }
    all_snaps: Dict[str, Any] = {}
    token = None
    for _ in range(12):
        p = dict(base_params)
        if token:
            p["page_token"] = token
        js = _get_json(f"{DATA_BASE}/v1beta1/options/snapshots/{str(symbol).upper().strip()}", s, p)
        snaps = js.get("snapshots", {}) or {}
        if isinstance(snaps, list):
            for item in snaps:
                sym = item.get("symbol")
                if sym:
                    all_snaps[sym] = item
        else:
            all_snaps.update(snaps)
        token = js.get("next_page_token") or js.get("page_token")
        if not token:
            break
    if not all_snaps:
        raise RuntimeError(f"OPRA no devolvió snapshots de opciones {str(symbol).upper()} con los filtros actuales.")
    return all_snaps


def _fractional_dte(expiration: str, now_ny: Optional[datetime] = None) -> float:
    now_ny = now_ny or datetime.now(NY)
    exp_date = date.fromisoformat(str(expiration)[:10])
    exp_dt = datetime.combine(exp_date, time(16, 0), tzinfo=NY)
    return dte_days_from_expiry(exp_dt, now_ny)


def _option_snapshot_values(snap: Dict[str, Any]) -> Dict[str, float]:
    iv = _first_num(_get(snap, "impliedVolatility", "implied_volatility", "iv"))
    greeks = _get(snap, "greeks", default={}) or {}
    delta = _first_num(_get(greeks, "delta"))
    gamma = _first_num(_get(greeks, "gamma"))
    bar = _get(snap, "dailyBar", "daily_bar", default={}) or {}
    volume = _first_num(_get(bar, "v", "volume"), default=0.0)
    quote = _get(snap, "latestQuote", "latest_quote", default={}) or {}
    trade = _get(snap, "latestTrade", "latest_trade", default={}) or {}
    bid = _first_num(_get(quote, "bp", "bid_price"))
    ask = _first_num(_get(quote, "ap", "ask_price"))
    last = _first_num(_get(trade, "p", "price"))
    # A snapshot may contain an old last trade and a much fresher NBBO quote.
    # The structural freshness clock must follow the freshest observed option
    # market event; preferring ``latestTrade`` unconditionally made a live OPRA
    # chain appear 90+ seconds stale during quiet contracts and blanked TRACE.
    trade_ts = _get(trade, "t", "timestamp", default="")
    quote_ts = _get(quote, "t", "timestamp", default="")
    ts = _freshest_market_timestamp(trade_ts, quote_ts)
    return {"iv": iv, "provider_delta": delta, "provider_gamma": gamma, "volume": volume, "bid": bid, "ask": ask, "last": last, "option_market_timestamp": ts}


def _freshest_market_timestamp(*values: Any) -> str:
    """Return the newest valid market-event timestamp without trusting string order.

    Alpaca normally emits RFC3339 timestamps, but parsing explicitly keeps this
    robust to fractional-second width and avoids a stale trade winning over a
    fresher quote merely because both fields exist.
    """
    best_ts = None
    best_raw = ""
    for value in values:
        if value in (None, ""):
            continue
        ts = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        if best_ts is None or ts > best_ts:
            best_ts = ts
            best_raw = str(value)
    return best_raw




def fetch_contract_catalog(symbol: str = "DIA", expiry_days: int = 365) -> pd.DataFrame:
    """Fetch active Alpaca option contract definitions without a strike filter.

    This is a COLD-library operation. It is never used on the click/LIVE path and may be
    paginated over a much wider expiration horizon than the tactical Quant chain.
    """
    symbol = str(symbol).upper().strip()
    s = load_settings()
    if not s:
        raise RuntimeError("Credenciales Alpaca no configuradas.")
    start, end = _expiry_window(expiry_days)
    base = {
        "underlying_symbols": symbol, "status": "active",
        "expiration_date_gte": start, "expiration_date_lte": end, "limit": 10000,
    }
    rows: list[dict[str, Any]] = []
    token = None
    for _ in range(50):
        params = dict(base)
        if token:
            params["page_token"] = token
        js = _get_json(f"{PAPER_BASE}/v2/options/contracts", s, params)
        batch = js.get("option_contracts", js.get("contracts", [])) or []
        if isinstance(batch, list):
            rows.extend(x for x in batch if isinstance(x, dict))
        token = js.get("page_token") or js.get("next_page_token")
        if not token:
            break
    return pd.DataFrame(rows)


def fetch_full_option_snapshot_library(symbol: str = "DIA", expiry_days: int = 30) -> Dict[str, Any]:
    """Fetch a broad OPRA snapshot library without a strike filter.

    The expiration horizon is independently configurable because full snapshots are much
    heavier than contract definitions. Pagination is bounded and runs only in WARM/COLD.
    """
    symbol = str(symbol).upper().strip()
    s = load_settings()
    if not s:
        raise RuntimeError("Credenciales Alpaca no configuradas.")
    start, end = _expiry_window(expiry_days)
    base = {
        "feed": s.option_feed, "limit": 1000,
        "expiration_date_gte": start, "expiration_date_lte": end,
    }
    out: Dict[str, Any] = {}
    token = None
    for _ in range(50):
        params = dict(base)
        if token:
            params["page_token"] = token
        js = _get_json(f"{DATA_BASE}/v1beta1/options/snapshots/{symbol}", s, params)
        snaps = js.get("snapshots", {}) or {}
        if isinstance(snaps, list):
            for item in snaps:
                if isinstance(item, dict) and item.get("symbol"):
                    out[str(item["symbol"])] = item
        elif isinstance(snaps, dict):
            out.update(snaps)
        token = js.get("next_page_token") or js.get("page_token")
        if not token:
            break
    if not out:
        raise RuntimeError(f"OPRA no devolvió snapshots de biblioteca para {symbol}.")
    try:
        from .provider_data_lake import DATA_LAKE
        DATA_LAKE.observe("ALPACA", "OPTION_SNAPSHOT", symbol=symbol, count=len(out))
    except Exception as _e:
        _obs_note('alpaca_data:647', _e)
    return out

def fetch_asset_options_snapshot(symbol: str = "DIA", strike_window: float = 12.0, expiry_days: int = 21) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    symbol = str(symbol).upper().strip()
    s = load_settings()
    if not s:
        raise RuntimeError("Primero guarda tus credenciales Alpaca en la pestaña ALPACA LIVE.")
    t0 = perf_counter()
    stock = fetch_stock_snapshot(s, symbol)
    spot = float(stock["spot"])
    stock_ms = (perf_counter() - t0) * 1000.0

    # Once Spot is known, contract definitions and OPRA snapshots are independent.
    # Fetch them concurrently so startup pays the slower network branch, not both.
    t_pair = perf_counter()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="itmq-alpaca-chain") as pool:
        f_contracts = pool.submit(fetch_contracts, s, spot, strike_window, expiry_days, symbol)
        f_chain = pool.submit(fetch_chain, s, spot, strike_window, expiry_days, symbol)
        contracts = f_contracts.result()
        chain = f_chain.result()
    pair_ms = (perf_counter() - t_pair) * 1000.0
    now_ec = datetime.now(EC).replace(microsecond=0)
    now_ny = now_ec.astimezone(NY)

    rows = []
    matched_snapshots = 0
    provider_iv_count = 0
    fallback_iv_count = 0
    unusable_iv_count = 0
    greeks_dislocation_count = 0
    matched_0dte_count = 0
    usable_0dte_count = 0
    for c in contracts.to_dict("records"):
        sym = str(c.get("symbol", ""))
        snap = chain.get(sym)
        if not sym or not snap:
            continue
        vals = _option_snapshot_values(snap)
        matched_snapshots += 1
        try:
            strike = float(c.get("strike_price"))
            oi = float(c.get("open_interest") or 0)
            expiration = str(c.get("expiration_date"))
            typ = str(c.get("type") or "").lower()
            if typ not in {"call", "put"}:
                typ = "call" if re.search(r"C\d{8}$", sym) else "put"
            dte = _fractional_dte(expiration, now_ny)
            if str(expiration)[:10] == now_ny.date().isoformat():
                matched_0dte_count += 1
        except Exception as _e:
            _obs_note('alpaca_data:700', _e)
            continue
        recovered = recover_iv_and_greeks(
            provider_iv=vals["iv"], provider_delta=vals["provider_delta"], provider_gamma=vals["provider_gamma"],
            bid=vals["bid"], ask=vals["ask"], last=vals["last"], S=spot, K=strike, dte=dte,
            option_type=typ, symbol=symbol,
        )
        iv = recovered["iv"]
        if not math.isfinite(iv) or iv <= 0:
            unusable_iv_count += 1
            continue
        if recovered.get("iv_source") == "ALPACA": provider_iv_count += 1
        elif recovered.get("iv_source") == "ITM_QUANT": fallback_iv_count += 1
        if recovered.get("greeks_dislocation"): greeks_dislocation_count += 1
        if str(expiration)[:10] == now_ny.date().isoformat():
            usable_0dte_count += 1
        rows.append({
            "timestamp": now_ec.replace(tzinfo=None),
            "underlying_symbol": symbol,
            "underlying_price": spot,
            "strike": strike,
            "dte": dte,
            "option_type": typ,
            "open_interest": max(oi, 0.0),
            "volume": max(vals["volume"], 0.0) if math.isfinite(vals["volume"]) else 0.0,
            "iv": iv,
            "iv_source": recovered.get("iv_source"),
            "greeks_source": recovered.get("greeks_source"),
            "fallback_delta": recovered.get("calc_delta"),
            "fallback_gamma": recovered.get("calc_gamma"),
            "calc_vanna": recovered.get("calc_vanna"),
            "calc_charm": recovered.get("calc_charm"),
            "calc_speed": recovered.get("calc_speed"),
            "greeks_dislocation": recovered.get("greeks_dislocation", False),
            "provider_delta_diff": recovered.get("provider_delta_diff"),
            "provider_gamma_diff_pct": recovered.get("provider_gamma_diff_pct"),
            "quote_source": recovered.get("quote_source"),
            "model_risk_free_rate": recovered.get("risk_free_rate"),
            "model_risk_free_source": recovered.get("risk_free_source"),
            "model_dividend_yield": recovered.get("dividend_yield"),
            "model_dividend_source": recovered.get("dividend_source"),
            "contract_symbol": sym,
            "expiration_date": expiration,
            "provider_delta": vals["provider_delta"],
            "provider_gamma": vals["provider_gamma"],
            "bid": vals["bid"],
            "ask": vals["ask"],
            "last": vals["last"],
            "open_interest_date": c.get("open_interest_date", ""),
            "option_market_timestamp": vals["option_market_timestamp"],
            "stock_market_timestamp": stock["market_timestamp"],
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"Recibí contratos y OPRA para {symbol}, pero no pude obtener IV utilizable ni del proveedor ni del fallback ITM QUANT.")
    df = df.sort_values(["strike", "option_type", "expiration_date"]).reset_index(drop=True)
    oi_dates = sorted({str(x) for x in df.get("open_interest_date", pd.Series(dtype=str)).dropna().astype(str) if str(x)})
    option_ts = [str(x) for x in df.get("option_market_timestamp", pd.Series(dtype=str)).dropna().astype(str) if str(x)]
    market_state = "CLOSED"
    if now_ny.weekday() < 5:
        t = now_ny.time()
        if time(9, 30) <= t < time(16, 0):
            market_state = "REGULAR"
        elif time(4, 0) <= t < time(9, 30):
            market_state = "PREMARKET"
        elif time(16, 0) <= t < time(20, 0):
            market_state = "AFTERHOURS"
    meta = {
        "source": "ALPACA ALGO TRADER PLUS",
        "stock_feed": s.stock_feed.upper(),
        "option_feed": s.option_feed.upper(),
        "symbol": str(symbol).upper(),
        "spot": spot,
        "contracts": int(len(df)),
        "matched_snapshots": int(matched_snapshots),
        "provider_iv_count": int(provider_iv_count),
        "fallback_iv_count": int(fallback_iv_count),
        "unusable_iv_count": int(unusable_iv_count),
        "greeks_dislocation_count": int(greeks_dislocation_count),
        "matched_0dte_count": int(matched_0dte_count),
        "usable_0dte_count": int(usable_0dte_count),
        "zero_dte_coverage_pct": round(100.0 * usable_0dte_count / max(matched_0dte_count, 1), 1) if matched_0dte_count else None,
        "contract_definitions": int(len(contracts)),
        "iv_coverage_pct": round(100.0 * len(df) / max(matched_snapshots, 1), 1),
        "unique_strikes": int(df["strike"].nunique()),
        "expirations": int(df["expiration_date"].nunique()),
        "expiration_list": sorted(df["expiration_date"].astype(str).unique().tolist()),
        "oi_dates": oi_dates,
        "snapshot_ec": now_ec.isoformat(sep=" "),
        "boot_timing_ms": {
            "stock": round(stock_ms, 2),
            "contracts_plus_chain_parallel": round(pair_ms, 2),
            "total": round((perf_counter() - t0) * 1000.0, 2),
        },
        "contracts_chain_parallel": True,
        "stock_market_timestamp": stock["market_timestamp"],
        "latest_option_market_timestamp": _freshest_market_timestamp(*option_ts),
        "market_state": market_state,
        "strike_window": float(strike_window),
        "expiry_days": int(expiry_days),
    }
    return df, meta



def fetch_dia_options_snapshot(strike_window: float = 12.0, expiry_days: int = 21) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    return fetch_asset_options_snapshot("DIA", strike_window, expiry_days)


def load_history(day: Optional[date] = None, symbol: str = "DIA") -> pd.DataFrame:
    p = history_path(day, symbol)
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, low_memory=False)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df

def latest_history_file() -> Optional[Path]:
    files = sorted(routed_dir(DATA_DIR, "sessions").glob("alpaca_*_history_*.csv"))
    return files[-1] if files else None

def history_path(day: Optional[date] = None, symbol: str = "DIA") -> Path:
    day = day or datetime.now(EC).date()
    sym = str(symbol).upper().strip().lower()
    return routed_dir(DATA_DIR, "sessions") / f"alpaca_{sym}_history_{day.isoformat()}.csv"


def append_history(snapshot: pd.DataFrame, symbol: str | None = None) -> Path:
    if snapshot.empty:
        raise ValueError("Snapshot vacío")
    if symbol is None:
        symbol = str(snapshot.get("underlying_symbol", pd.Series(["DIA"])).iloc[-1] if "underlying_symbol" in snapshot.columns else "DIA")
    p = history_path(pd.Timestamp(snapshot["timestamp"].iloc[-1]).date(), symbol)
    if p.exists():
        old = pd.read_csv(p, low_memory=False)
        full = pd.concat([old, snapshot], ignore_index=True, sort=False)
    else:
        full = snapshot.copy()
    key_cols = [c for c in ["timestamp", "contract_symbol"] if c in full.columns]
    if key_cols:
        full = full.drop_duplicates(key_cols, keep="last")
    full.to_csv(p, index=False)
    return p


def fetch_and_store(strike_window: float = 12.0, expiry_days: int = 21, symbol: str = "DIA") -> Tuple[Path, Dict[str, Any]]:
    df, meta = fetch_asset_options_snapshot(symbol, strike_window, expiry_days)
    p = append_history(df, symbol)
    meta["history_file"] = str(p)
    return p, meta


def test_connection(strike_window: float = 3.0, expiry_days: int = 3, symbol: str = "DIA") -> Dict[str, Any]:
    s = load_settings()
    if not s:
        raise RuntimeError("Credenciales no configuradas.")
    stock = fetch_stock_snapshot(s, symbol)
    spot = float(stock["spot"])
    contracts = fetch_contracts(s, spot, strike_window, expiry_days, symbol)
    chain = fetch_chain(s, spot, strike_window, expiry_days, symbol)
    oi_valid = int(pd.to_numeric(contracts.get("open_interest", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0).sum()) if not contracts.empty else 0
    iv_count = 0
    for snap in chain.values():
        if _option_snapshot_values(snap)["iv"] > 0:
            iv_count += 1
    return {
        "spot": spot,
        "stock_feed": s.stock_feed.upper(),
        "option_feed": s.option_feed.upper(),
        "contracts": int(len(contracts)),
        "opra_snapshots": int(len(chain)),
        "contracts_with_oi": oi_valid,
        "snapshots_with_iv": iv_count,
        "stock_market_timestamp": stock["market_timestamp"],
    }
