from __future__ import annotations
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
import math
import re
import threading
import pandas as pd
import numpy as np
import requests

from .alpaca_data import load_settings, DATA_BASE, DATA_DIR, _headers
from app.persistence import routed_dir
from .dealer_microstructure import enrich_option_packages
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .expiry_clock import year_fraction
from .contract_spec import row_multiplier, infer_symbol

EC=ZoneInfo('America/Guayaquil')
NY=ZoneInfo('America/New_York')
LONDON=ZoneInfo('Europe/London')
_SESSION_ACTIVITY_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}


def _chunks(seq, n=100):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def _safe(v, default=float('nan')):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


# El proveedor nombra en el propio mensaje de error los parámetros que no reconoce.
# Aprovecharlo convierte un 400 repetido cada ciclo en una petición que se corrige
# sola en el segundo intento, en vez de dejar el flujo de opciones sin datos toda
# la sesión mientras el log repite la misma línea.
_UNEXPECTED_PARAM_RE = re.compile(r'unexpected query parameter\(s\):\s*([A-Za-z0-9_,\s]+)', re.I)

# Parámetros que este proceso ya descubrió que un endpoint rechaza. Evita repetir
# el 400 en cada ciclo: la segunda llamada ya sale limpia.
_REJECTED_PARAMS: dict[str, set] = {}
_REJECTED_LOCK = threading.Lock()


def _endpoint_key(url: str) -> str:
    return str(url).split('?')[0]


def _drop_known_rejected(url, params):
    with _REJECTED_LOCK:
        bad = set(_REJECTED_PARAMS.get(_endpoint_key(url)) or ())
    return ({k: v for k, v in params.items() if k not in bad}, bad) if bad else (params, set())


def _remember_rejected(url, names):
    with _REJECTED_LOCK:
        _REJECTED_PARAMS.setdefault(_endpoint_key(url), set()).update(names)


def _request_json(url, params, timeout=20):
    s=load_settings()
    if not s:
        raise RuntimeError('Alpaca no está configurado.')
    params, _ = _drop_known_rejected(url, dict(params or {}))
    for attempt in (0, 1):
        r=requests.get(url,headers=_headers(s),params=params,timeout=timeout)
        if r.status_code<400:
            return r.json()
        try: msg=(r.json().get('message') or r.text)[:300]
        except Exception: msg=r.text[:300]
        m = _UNEXPECTED_PARAM_RE.search(msg or '') if r.status_code==400 else None
        names = {n.strip() for n in m.group(1).split(',') if n.strip()} if m else set()
        names &= set(params)
        if attempt==0 and names:
            _remember_rejected(url, names)
            _obs_note('flow_intelligence:unexpected_query_param',
                      RuntimeError(f'{_endpoint_key(url)} rechaza {sorted(names)}; se reintenta sin ellos'),
                      severity='INFO')
            params = {k: v for k, v in params.items() if k not in names}
            continue
        raise RuntimeError(f'Alpaca HTTP {r.status_code}: {msg}')
    raise RuntimeError(f'Alpaca HTTP 400: {url}')


def _flow_path(day=None, symbol="DIA"):
    day=day or datetime.now(EC).date()
    sym=str(symbol).upper().strip().lower()
    return routed_dir(DATA_DIR, 'sessions')/f'option_flow_events_{sym}_{day.isoformat()}.csv'


def load_flow_events(day=None, symbol="DIA"):
    p=_flow_path(day, symbol)
    if not p.exists(): return pd.DataFrame()
    df=pd.read_csv(p)
    if 'timestamp' in df.columns:
        df['timestamp']=pd.to_datetime(df['timestamp'],errors='coerce')
        df=df.dropna(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
    return df


def save_flow_events(df, symbol=None):
    if df is None or df.empty: return _flow_path(symbol=symbol or "UNKNOWN")
    if symbol is None:
        symbol=str(df.get("underlying_symbol",pd.Series(["UNKNOWN"])).iloc[-1] if "underlying_symbol" in df.columns else "UNKNOWN")
    p=_flow_path(pd.Timestamp(df['timestamp'].iloc[-1]).date(), symbol)
    old=load_flow_events(pd.Timestamp(df['timestamp'].iloc[-1]).date(), symbol)
    if old.empty:
        full=df.copy()
    else:
        # pandas 2.2 warns when dtype inference sees empty/all-NA columns during concat.
        # Remove only per-frame all-NA columns for the concat operation, then restore the
        # union schema so no field is silently lost.
        all_cols=list(dict.fromkeys([*old.columns.tolist(),*df.columns.tolist()]))
        left=old.dropna(axis=1,how="all")
        right=df.dropna(axis=1,how="all")
        full=pd.concat([left,right],ignore_index=True,sort=False)
        for col in all_cols:
            if col not in full.columns: full[col]=pd.NA
        full=full.reindex(columns=all_cols)
    key=['contract_symbol','timestamp','trade_price','contracts']
    present=[c for c in key if c in full.columns]
    if present: full=full.drop_duplicates(present,keep='last')
    full=full.sort_values('timestamp').reset_index(drop=True)
    full=enrich_option_packages(full)
    full.to_csv(p,index=False)
    return p


def _classify_aggressor(price,bid,ask):
    price=_safe(price); bid=_safe(bid); ask=_safe(ask)
    if not (math.isfinite(price) and math.isfinite(bid) and math.isfinite(ask) and bid>0 and ask>0 and ask>=bid):
        return 'UNKNOWN',0.15
    spread=max(ask-bid,1e-9); mid=(ask+bid)/2
    if price>=ask-0.12*spread: return 'BUY',1.0
    if price<=bid+0.12*spread: return 'SELL',1.0
    if price>mid: return 'BUY',0.55
    if price<mid: return 'SELL',0.55
    return 'MID',0.25


def _tick_rule(price, prev_price):
    """Regla del tick: el respaldo estándar cuando no hay NBBO del instante.

    Si el precio sube respecto a la operación anterior del MISMO contrato, el
    agresor compró; si baja, vendió. Es menos preciso que comparar contra bid/ask
    —por eso su confianza es menor— pero clasifica la gran mayoría de la cinta en
    lugar de dejarla en UNKNOWN, que es lo que vaciaba el carril de flujo neto
    aunque hubiera millones de dólares de prima negociada.
    """
    p0, p1 = _safe(prev_price), _safe(price)
    if not (math.isfinite(p0) and math.isfinite(p1)) or p0 <= 0 or p1 <= 0 or p0 == p1:
        return None, 0.0
    return ('BUY' if p1 > p0 else 'SELL'), 0.30


def _direction(option_type,aggressor):
    is_call=str(option_type).lower().startswith('c')
    if aggressor=='BUY': return 1 if is_call else -1
    if aggressor=='SELL': return -1 if is_call else 1
    return 0


def _score_event(row):
    premium=max(_safe(row.get('premium'),0),0)
    oi=max(_safe(row.get('open_interest'),0),0)
    size=max(_safe(row.get('contracts'),0),0)
    vol=max(_safe(row.get('daily_volume'),0),0)
    spot=max(_safe(row.get('underlying_price'),0),1e-9)
    strike=_safe(row.get('strike'),spot)
    dte=max(_safe(row.get('dte'),30),0)
    conf=max(min(_safe(row.get('aggressor_confidence'),0),1),0)
    gamma=abs(_safe(row.get('provider_gamma'),0))
    delta=abs(_safe(row.get('provider_delta'),0))
    # Stable absolute calibrations; Q-Flow is relevance, not a probability.
    premium_s=np.clip((math.log10(premium+1)-3.0)/4.0,0,1)  # ~1K -> 0, ~10M -> 1
    sizeoi_s=np.clip(math.log1p((size/(oi+1))*10.0)/math.log(11.0),0,1)
    volshare_s=np.clip(size/(vol+1.0),0,1)
    prox_s=math.exp(-abs(strike-spot)/2.5)
    expiry_s=math.exp(-dte/14.0)
    greek_s=np.clip((gamma*120.0 + delta*0.20),0,1)
    score=100*(.34*premium_s+.20*sizeoi_s+.14*conf+.10*prox_s+.08*expiry_s+.08*greek_s+.06*volshare_s)
    return float(np.clip(score,0,100))




def _iv_decimal(row) -> float:
    """Return IV as a decimal, accepting either decimal or percent inputs."""
    iv=_safe(row.get('iv'), float('nan'))
    if not math.isfinite(iv) or iv<=0:
        return float('nan')
    return iv/100.0 if iv>3.0 else iv


def _proximity_in_sigma(row) -> tuple[float, float, bool]:
    """Distance strike↔spot in option-horizon sigmas; no fixed-dollar kernel."""
    spot=max(_safe(row.get('underlying_price'),0),1e-9)
    strike=_safe(row.get('strike'),spot)
    dte=max(_safe(row.get('dte'),float('nan')),0.0)
    iv=_iv_decimal(row)
    if not (math.isfinite(strike) and math.isfinite(dte) and math.isfinite(iv) and iv>0):
        return float('nan'), 0.5, False
    # DTE is fractional calendar days to 16:00 NY and already bottoms at one minute.
    sigma_move=spot*iv*math.sqrt(year_fraction(dte))
    if not math.isfinite(sigma_move) or sigma_move<=1e-12:
        return float('nan'), 0.5, False
    z=abs(strike-spot)/sigma_move
    return float(z), float(math.exp(-z)), True


def _flow_bucket(row) -> tuple[str,str]:
    """Expiry + relative-moneyness bucket for Greek anchors. 0DTE is date-strict."""
    dte=max(_safe(row.get('dte'),30),0.0)
    exp=row.get('expiration_date') if hasattr(row,'get') else None
    ts=row.get('timestamp') if hasattr(row,'get') else None
    dte_b='unscoped'
    try:
        ex=pd.to_datetime(exp,errors='coerce')
        tt=pd.to_datetime(ts,errors='coerce')
        if pd.notna(ex) and pd.notna(tt):
            if ex.date()==tt.date(): dte_b='0dte'
            elif dte<=7: dte_b='1_7d'
            elif dte<=30: dte_b='8_30d'
            else: dte_b='31d_plus'
        elif pd.notna(ex):
            # Expiry exists but event timestamp does not: scope is unknown rather than guessed.
            dte_b='unscoped'
    except Exception:
        dte_b='unscoped'
    z,_,ready=_proximity_in_sigma(row)
    if ready and math.isfinite(z):
        mon='atm' if z<=0.75 else 'near' if z<=1.75 else 'far'
    else:
        spot=max(_safe(row.get('underlying_price'),0),1e-9); strike=_safe(row.get('strike'),spot)
        pct=abs(strike-spot)/spot
        mon='atm' if pct<=0.005 else 'near' if pct<=0.015 else 'far'
    return dte_b,mon


def _anchored_scalar(value: float, anchors: dict | None, keys: list[str]) -> tuple[float, str | None, bool]:
    try:
        from .scale_anchors import anchored_magnitude
    except Exception:
        return 0.5,None,False
    if not math.isfinite(float(value)) or float(value)<0:
        return 0.5,None,False
    for key in keys:
        a=(anchors or {}).get(key)
        z=anchored_magnitude([float(value)],a)
        if z is not None and len(z):
            return float(z[0]),key,True
    return 0.5,None,False


def _score_event_normalized(row, anchors: dict | None = None) -> dict:
    """v1.15.7 SHADOW Q-Flow normalized across asset/volatility scales.

    Important: this score is *relevance*, never a probability.  It does not replace
    `flow_score` LIVE yet. Absolute dollar/gamma constants are removed from the SHADOW
    path and correlated size/OI + size/volume inputs are one PARTICIPATION family.
    """
    premium=max(_safe(row.get('premium'),0),0)
    oi=max(_safe(row.get('open_interest'),0),0)
    size=max(_safe(row.get('contracts'),0),0)
    vol=max(_safe(row.get('daily_volume'),0),0)
    conf=float(np.clip(_safe(row.get('aggressor_confidence'),0),0,1))
    gamma=abs(_safe(row.get('provider_gamma'),0)); delta=abs(_safe(row.get('provider_delta'),0))
    dte=max(_safe(row.get('dte'),30),0)
    dte_b,mon=_flow_bucket(row)

    premium_s,premium_src,premium_ready=_anchored_scalar(premium,anchors,['flow_premium'])
    ratio_oi=size/(oi+1.0); ratio_vol=size/(vol+1.0)
    sizeoi_s,sizeoi_src,sizeoi_ready=_anchored_scalar(ratio_oi,anchors,['flow_size_oi_ratio'])
    volshare_s,volshare_src,volshare_ready=_anchored_scalar(ratio_vol,anchors,['flow_size_volume_ratio'])
    # One family, not two independent confirmations.
    participation_s=float(np.mean([sizeoi_s,volshare_s]))
    participation_ready=bool(sizeoi_ready or volshare_ready)

    prox_z,prox_s,prox_ready=_proximity_in_sigma(row)
    gamma_s,gamma_src,gamma_ready=_anchored_scalar(gamma,anchors,[
        f'flow_gamma_{dte_b}_{mon}',f'flow_gamma_{dte_b}_all','flow_gamma_all'])
    delta_s,delta_src,delta_ready=_anchored_scalar(delta,anchors,[
        f'flow_delta_{dte_b}_{mon}',f'flow_delta_{dte_b}_all','flow_delta_all'])
    # Greeks are a SENSITIVITY family. Gamma gets more weight for convexity; Delta is
    # kept visible separately and never treated as an independent source confirmation.
    sensitivity_s=0.70*gamma_s+0.30*delta_s
    sensitivity_ready=bool(gamma_ready or delta_ready)
    expiry_s=float(math.exp(-dte/14.0))

    components={
        'magnitude':premium_s,
        'participation':participation_s,
        'location':prox_s,
        'sensitivity':sensitivity_s,
        'execution':conf,
        'expiry':expiry_s,
    }
    weights={'magnitude':.32,'participation':.20,'location':.16,'sensitivity':.14,'execution':.12,'expiry':.06}
    score=100.0*sum(weights[k]*components[k] for k in weights)
    ready=bool(premium_ready and participation_ready and sensitivity_ready and prox_ready)
    return {
        'flow_score_normalized':float(np.clip(score,0,100)),
        'flow_score_normalized_ready':ready,
        'flow_score_normalized_status':'SHADOW · READY' if ready else 'SHADOW · COLLECTING',
        'flow_proximity_sigma':None if not math.isfinite(prox_z) else float(prox_z),
        'flow_proximity_score':float(prox_s),
        'flow_premium_anchor_score':float(premium_s),
        'flow_participation_score':float(participation_s),
        'flow_gamma_anchor_score':float(gamma_s),
        'flow_delta_anchor_score':float(delta_s),
        'flow_sensitivity_score':float(sensitivity_s),
        'flow_premium_anchor_source':premium_src,
        'flow_gamma_anchor_source':gamma_src,
        'flow_delta_anchor_source':delta_src,
        'flow_anchor_ready_count':int(sum([premium_ready,participation_ready,gamma_ready,delta_ready])),
        'flow_anchor_required_count':4,
        'flow_normalized_role':'SHADOW_ONLY',
        'flow_normalized_note':'Q-Flow normalizado por activo/volatilidad; no altera Scanner hasta validación LIVE/OOS.',
    }


def apply_normalized_flow_scores(events: pd.DataFrame, anchors: dict | None = None) -> pd.DataFrame:
    """Attach the normalized SHADOW score while preserving LIVE legacy flow_score."""
    if events is None or events.empty:
        return events.copy() if isinstance(events,pd.DataFrame) else pd.DataFrame()
    x=events.copy()
    if 'flow_score' not in x.columns:
        x['flow_score']=[_score_event(r) for r in x.to_dict('records')]
    x['flow_score_legacy']=pd.to_numeric(x['flow_score'],errors='coerce')
    vals=[_score_event_normalized(r,anchors) for r in x.to_dict('records')]
    if vals:
        add=pd.DataFrame(vals,index=x.index)
        for c in add.columns:x[c]=add[c]
    return x


def flow_anchor_samples(events: pd.DataFrame) -> dict[str,list[float]]:
    """Current-session samples staged for tomorrow; no same-session self-normalisation."""
    out: dict[str,list[float]]={}
    if events is None or events.empty:return out
    def push(k,v):
        try:
            f=float(v)
            if math.isfinite(f) and f>0:out.setdefault(k,[]).append(f)
        except Exception as _e:
            _obs_note('flow_intelligence:278', _e)
    for r in events.to_dict('records'):
        premium=max(_safe(r.get('premium'),0),0);size=max(_safe(r.get('contracts'),0),0)
        oi=max(_safe(r.get('open_interest'),0),0);vol=max(_safe(r.get('daily_volume'),0),0)
        gamma=abs(_safe(r.get('provider_gamma'),0));delta=abs(_safe(r.get('provider_delta'),0))
        dte_b,mon=_flow_bucket(r)
        push('flow_premium',premium);push('flow_size_oi_ratio',size/(oi+1.0));push('flow_size_volume_ratio',size/(vol+1.0))
        for base,val in [('flow_gamma',gamma),('flow_delta',delta)]:
            push(f'{base}_all',val);push(f'{base}_{dte_b}_all',val);push(f'{base}_{dte_b}_{mon}',val)
    return out

def fetch_recent_option_trades(snapshot: pd.DataFrame, lookback_seconds=35, max_contracts=500):
    _snap_underlying=infer_symbol(snapshot)
    if snapshot is None or snapshot.empty or 'contract_symbol' not in snapshot.columns:
        return pd.DataFrame()
    snap=snapshot.copy()
    # Keep DIA-centric universe; prefer near spot + active/OI but do not hard-limit to only top 5 levels.
    spot=float(pd.to_numeric(snap['underlying_price'],errors='coerce').dropna().iloc[-1])
    snap['_rank']=(pd.to_numeric(snap.get('open_interest',0),errors='coerce').fillna(0)+1).map(np.log1p) \
                  +(pd.to_numeric(snap.get('volume',0),errors='coerce').fillna(0)+1).map(np.log1p) \
                  -0.35*(pd.to_numeric(snap['strike'],errors='coerce')-spot).abs()
    snap=snap.sort_values('_rank',ascending=False).head(int(max_contracts))
    symbols=snap['contract_symbol'].astype(str).dropna().unique().tolist()
    if not symbols:return pd.DataFrame()
    now=datetime.now(EC)
    start=(now-timedelta(seconds=max(int(lookback_seconds),10))).astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00','Z')
    end=now.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00','Z')
    rows=[]
    # Última operación vista por contrato, para la regla del tick.
    _last_px: dict = {}
    lookup=snap.set_index('contract_symbol').to_dict('index')
    for batch in _chunks(symbols,100):
        token=None
        for _ in range(4):
            # /v1beta1/options/trades NO acepta `feed`: enviarlo devuelve
            # 400 unexpected query parameter(s): feed y la petición entera se pierde.
            # El tape de OPRA es el único que sirve este endpoint, así que no hay nada que elegir.
            params={'symbols':','.join(batch),'start':start,'end':end,'limit':10000,'sort':'asc'}
            if token: params['page_token']=token
            js=_request_json(f'{DATA_BASE}/v1beta1/options/trades',params)
            trades=js.get('trades',{}) or {}
            if isinstance(trades,list):
                # fallback shape
                trades_by={}
                for item in trades:
                    sym=item.get('S') or item.get('symbol')
                    if sym: trades_by.setdefault(sym,[]).append(item)
                trades=trades_by
            for sym,items in trades.items():
                meta=lookup.get(sym,{})
                for t in items or []:
                    px=_safe(t.get('p',t.get('price'))); sz=_safe(t.get('s',t.get('size')),0)
                    if not (math.isfinite(px) and px>0 and sz>0): continue
                    ag,conf=_classify_aggressor(px,meta.get('bid'),meta.get('ask'))
                    # REST trades are paired with a chain snapshot quote, not the event-time NBBO.
                    # Preserve the directional hint but explicitly downgrade certainty.
                    conf=min(float(conf),0.35)
                    # Sin cotización utilizable, la regla del tick clasifica contra la
                    # operación anterior del mismo contrato. Marcada como tal: es una
                    # inferencia más débil, pero rescata la cinta que si no quedaría
                    # entera en UNKNOWN y dejaría el flujo neto plano.
                    method='QUOTE' if ag in ('BUY','SELL','MID') else 'UNCLASSIFIED'
                    if ag=='UNKNOWN':
                        tick_ag,tick_conf=_tick_rule(px,_last_px.get(sym))
                        if tick_ag:
                            ag,conf,method=tick_ag,tick_conf,'TICK_RULE'
                    _last_px[sym]=px
                    direction=_direction(meta.get('option_type',''),ag)
                    ts=t.get('t',t.get('timestamp'))
                    try:
                        ts_ec=pd.Timestamp(ts).tz_convert(EC).tz_localize(None) if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts)
                    except Exception:
                        ts_ec=pd.Timestamp(now.replace(tzinfo=None))
                    und=str(meta.get('underlying_symbol') or _snap_underlying or '').upper()
                    # El tamano del contrato decide la prima en dolares. Escribir 100
                    # aqui convertia cualquier future/contrato ajustado en una cifra
                    # monetaria plausible y equivocada.
                    mult=row_multiplier({**meta,'contract_symbol':sym},und)
                    row={
                        'timestamp':ts_ec,'underlying_symbol':und or 'DIA','contract_symbol':sym,'strike':_safe(meta.get('strike')),
                        'expiration_date':str(meta.get('expiration_date','')),'option_type':str(meta.get('option_type','')),
                        'dte':_safe(meta.get('dte')),'underlying_price':spot,'trade_price':px,'contracts':sz,
                        'premium':px*sz*mult,'contract_multiplier':mult,'bid':_safe(meta.get('bid')),'ask':_safe(meta.get('ask')),
                        'open_interest':_safe(meta.get('open_interest'),0),'daily_volume':_safe(meta.get('volume'),0),
                        'aggressor_method':method,
                        'iv':_safe(meta.get('iv')),'provider_gamma':_safe(meta.get('provider_gamma'), _safe(meta.get('fallback_gamma'),0)),
                        'provider_delta':_safe(meta.get('provider_delta'), _safe(meta.get('fallback_delta'),0)),
                        'greeks_source':meta.get('greeks_source','ALPACA'),'iv_source':meta.get('iv_source','ALPACA'),
                        'aggressor':ag,'aggressor_confidence':conf,'classification_method':'REST_SNAPSHOT_QUOTE_UNSYNCED',
                        'nbbo_synced':False,'quote_quality':'UNSYNCED','quote_age_ms':None,'spread_position':None,
                        'received_at':pd.Timestamp.now(tz='UTC').tz_convert(EC).tz_localize(None),
                        'direction_sign':direction,'directional_premium':direction*px*sz*mult,
                        'exchange':str(t.get('x',t.get('exchange',''))),'conditions':str(t.get('c',t.get('conditions',''))),
                        'flow_source':'OPRA REST · UNSYNCED SNAPSHOT QUOTE',
                    }
                    row['flow_score']=_score_event(row)
                    rows.append(row)
            token=js.get('next_page_token') or js.get('page_token')
            if not token: break
    df=pd.DataFrame(rows)
    if not df.empty:
        df=df.sort_values('timestamp').reset_index(drop=True)
        df=enrich_option_packages(df)
        save_flow_events(df)
    return df



def flow_microstructure_summary(events: pd.DataFrame, lookback_minutes: int = 15):
    """Detect bursts, repeated strikes, multi-expiry clusters and flow acceleration.

    These labels describe observed OPRA activity; they are not predictions and do
    not assume the identity of the counterparty/dealer.
    """
    if events is None or events.empty:
        return {"state":"WAITING","burst":None,"repeated_strike":None,"multi_expiry_cluster":None,"premium_acceleration_pct":0.0,"persistence_pct":0.0,"exhaustion":"NO DATA"}
    e=events.copy(); e['timestamp']=pd.to_datetime(e.get('timestamp'),errors='coerce'); e=e.dropna(subset=['timestamp']).sort_values('timestamp')
    if e.empty:return {"state":"WAITING"}
    end=e['timestamp'].max(); e=e[e['timestamp']>=end-pd.Timedelta(minutes=int(lookback_minutes))].copy()
    for c in ['premium','directional_premium','contracts','strike']:
        e[c]=pd.to_numeric(e.get(c,0),errors='coerce').fillna(0)
    # Burst: 10-second buckets by strike/type.
    z=e.copy(); z['bucket10']=z['timestamp'].dt.floor('10s')
    bg=z.groupby(['bucket10','strike','option_type'],as_index=False).agg(events=('premium','size'),premium=('premium','sum'),directional=('directional_premium','sum'),contracts=('contracts','sum'))
    burst=None
    if not bg.empty:
        r=bg.sort_values(['events','premium'],ascending=False).iloc[0]
        burst={"timestamp":r['bucket10'].isoformat(),"strike":float(r['strike']),"option_type":str(r['option_type']),"events":int(r['events']),"premium":float(r['premium']),"direction":"BUY" if r['directional']>0 else "SELL" if r['directional']<0 else "MIXED"}
    rs=e.groupby('strike',as_index=False).agg(events=('premium','size'),premium=('premium','sum'),directional=('directional_premium','sum'),expiries=('expiration_date',lambda v: pd.Series(v).astype(str).nunique())) if 'expiration_date' in e.columns else e.groupby('strike',as_index=False).agg(events=('premium','size'),premium=('premium','sum'),directional=('directional_premium','sum'))
    repeated=None
    if not rs.empty:
        cand=rs[rs['events']>=3].sort_values(['events','premium'],ascending=False)
        if not cand.empty:
            r=cand.iloc[0]; repeated={"strike":float(r['strike']),"events":int(r['events']),"premium":float(r['premium']),"direction":"BUY" if r['directional']>0 else "SELL" if r['directional']<0 else "MIXED"}
    cluster=None
    if 'expiries' in rs.columns:
        cand=rs[rs['expiries']>=2].sort_values(['expiries','premium'],ascending=False)
        if not cand.empty:
            r=cand.iloc[0]; cluster={"strike":float(r['strike']),"expiries":int(r['expiries']),"events":int(r['events']),"premium":float(r['premium']),"direction":"BUY" if r['directional']>0 else "SELL" if r['directional']<0 else "MIXED"}
    # Premium acceleration: latest 60s vs previous 60s.
    latest=e[e['timestamp']>end-pd.Timedelta(seconds=60)]['premium'].sum(); prev=e[(e['timestamp']<=end-pd.Timedelta(seconds=60))&(e['timestamp']>end-pd.Timedelta(seconds=120))]['premium'].sum()
    accel=100.0*(float(latest)-float(prev))/max(float(prev),1.0)
    # Persistence: proportion of recent one-minute buckets aligned with aggregate net direction.
    m=e.copy();m['minute']=m['timestamp'].dt.floor('min');mg=m.groupby('minute',as_index=False)['directional_premium'].sum().tail(6); net=float(mg['directional_premium'].sum()) if not mg.empty else 0.0; sgn=np.sign(net)
    pers=float((np.sign(mg['directional_premium'])==sgn).mean()*100.0) if len(mg) and sgn!=0 else 0.0
    prev3=e[(e['timestamp']<=end-pd.Timedelta(minutes=1))&(e['timestamp']>end-pd.Timedelta(minutes=4))].copy(); prev_avg=float(prev3.groupby(prev3['timestamp'].dt.floor('min'))['premium'].sum().mean()) if not prev3.empty else 0.0
    exhaustion="ACTIVE"
    if prev_avg>0 and latest<0.45*prev_avg: exhaustion="POSSIBLE EXHAUSTION"
    elif accel>75: exhaustion="ACCELERATING"
    state="FLOW ACCELERATION" if accel>75 else "FLOW DECELERATION" if accel<-50 else "FLOW STABLE"
    return {"state":state,"burst":burst,"repeated_strike":repeated,"multi_expiry_cluster":cluster,"premium_acceleration_pct":round(float(accel),1),"persistence_pct":round(float(pers),1),"exhaustion":exhaustion,"lookback_minutes":int(lookback_minutes),"events":int(len(e)),"note":"Microstructure uses observed OPRA trades/quotes; labels describe activity, not guaranteed future direction."}

def flow_session_summary(events: pd.DataFrame):
    if events is None or events.empty:
        return {'regime':'WAITING','net':0.0,'bull':0.0,'bear':0.0,'largest':None,'bull_top':None,'bear_top':None,'confidence':0.0}
    e=events.copy()
    e['directional_premium']=pd.to_numeric(e['directional_premium'],errors='coerce').fillna(0.0)
    bull=float(e.loc[e['directional_premium']>0,'directional_premium'].sum())
    bear=float(-e.loc[e['directional_premium']<0,'directional_premium'].sum())
    net=bull-bear; total=bull+bear
    ratio=net/max(total,1.0)
    regime='BULLISH' if ratio>0.18 else 'BEARISH' if ratio<-0.18 else 'MIXED'
    conf=min(abs(ratio)*100,100)
    largest=e.iloc[int(pd.to_numeric(e['premium'],errors='coerce').fillna(0).argmax())]
    pos=e[e['directional_premium']>0]; neg=e[e['directional_premium']<0]
    bull_top=None if pos.empty else pos.iloc[int(pd.to_numeric(pos['premium'],errors='coerce').fillna(0).argmax())]
    bear_top=None if neg.empty else neg.iloc[int(pd.to_numeric(neg['premium'],errors='coerce').fillna(0).argmax())]
    micro=flow_microstructure_summary(e)
    norm={}
    if 'flow_score_normalized' in e.columns:
        ns=pd.to_numeric(e['flow_score_normalized'],errors='coerce'); nr=e.get('flow_score_normalized_ready',pd.Series(False,index=e.index)).fillna(False).astype(bool)
        norm={'role':'SHADOW_ONLY','samples':int(ns.notna().sum()),'ready_samples':int(nr.sum()),
              'mean_score':None if not ns.notna().any() else round(float(ns.mean()),2),
              'largest_score':None if not ns.notna().any() else round(float(ns.max()),2),
              'status':'SHADOW · READY' if int(nr.sum())>0 else 'SHADOW · COLLECTING'}
    return {'regime':regime,'net':net,'bull':bull,'bear':bear,'largest':largest,'bull_top':bull_top,'bear_top':bear_top,'confidence':conf,'microstructure':micro,'normalized_shadow':norm}


def fetch_premarket_bars(symbol="DIA"):
    s=load_settings()
    if not s: raise RuntimeError('Alpaca no está configurado.')
    now_ny=datetime.now(NY)
    day=now_ny.date()
    start=datetime.combine(day,time(4,0),tzinfo=NY)
    end=min(now_ny,datetime.combine(day,time(9,30),tzinfo=NY))
    if end<=start:return pd.DataFrame()
    params={'timeframe':'1Min','start':start.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00','Z'),
            'end':end.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00','Z'),'feed':s.stock_feed,'limit':10000,'adjustment':'raw'}
    js=_request_json(f'{DATA_BASE}/v2/stocks/{str(symbol).upper().strip()}/bars',params)
    bars=js.get('bars',[]) or []
    df=pd.DataFrame(bars)
    if df.empty:return df
    ren={'t':'timestamp','o':'open','h':'high','l':'low','c':'close','v':'volume','vw':'vwap'}
    df=df.rename(columns=ren)
    df['timestamp']=pd.to_datetime(df['timestamp'],utc=True).dt.tz_convert(EC).dt.tz_localize(None)
    for c in ['open','high','low','close','volume','vwap']:
        if c in df: df[c]=pd.to_numeric(df[c],errors='coerce')
    df['vwap']=df['vwap'].fillna((df['high']+df['low']+df['close'])/3)
    # Signed notional proxy from minute-bar direction; explicitly a proxy, not aggressor-tagged tape.
    sign=np.sign(df['close']-df['open'])
    sign=sign.where(sign!=0,np.sign(df['close'].diff()).fillna(0))
    df['signed_notional_proxy']=sign*df['volume'].fillna(0)*df['vwap'].fillna(df['close'])
    return df




def london_open_ny(day: date | None = None) -> datetime:
    """Return the DST-aware London cash-session open mapped to a NY calendar day.

    This is a session clock only. It never claims that the selected instrument trades on
    the London exchange; consumers must still use only observations from authorized feeds.
    """
    day = day or datetime.now(NY).date()
    noon_ny = datetime.combine(day, time(12, 0), tzinfo=NY)
    london_day = noon_ny.astimezone(LONDON).date()
    return datetime.combine(london_day, time(8, 0), tzinfo=LONDON).astimezone(NY)


def flow_session_phase(ts=None) -> str:
    """Session label for the global unusual-activity detector.

    Naive timestamps in ITM QUANT historical frames are Ecuador-local by convention.
    """
    t = pd.Timestamp(ts if ts is not None else datetime.now(EC))
    if t.tzinfo is None:
        t = t.tz_localize(EC)
    else:
        t = t.tz_convert(EC)
    ny = t.tz_convert(NY)
    day = ny.date()
    lo = pd.Timestamp(london_open_ny(day))
    prem = pd.Timestamp(datetime.combine(day, time(4, 0), tzinfo=NY))
    rth = pd.Timestamp(datetime.combine(day, time(9, 30), tzinfo=NY))
    close = pd.Timestamp(datetime.combine(day, time(16, 0), tzinfo=NY))
    after = pd.Timestamp(datetime.combine(day, time(20, 0), tzinfo=NY))
    if lo <= ny < prem:
        return "LONDON"
    if max(lo, prem) <= ny < rth:
        return "PREMARKET"
    if rth <= ny < close:
        return "NEW YORK"
    if close <= ny < after:
        return "AFTER HOURS"
    return "OFF SESSION"


def _activity_scores(frame: pd.DataFrame) -> pd.DataFrame:
    """Causal underlying-activity anomaly score from prior bars only.

    It is deliberately not options flow. It gives Flujo Inusual Pro a valid detector from
    London onward even when the US option market has not printed a new OPRA/DXLink trade.
    """
    if frame is None or frame.empty:
        return pd.DataFrame()
    x = frame.copy().sort_values("timestamp").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume", "vwap", "signed_notional_proxy"):
        if c in x:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    x["volume"] = numeric_column(x, "volume", 0.0)
    x["vwap"] = x.get("vwap", x.get("close")).fillna(x.get("close"))
    x["abs_notional"] = numeric_column(x, "signed_notional_proxy", 0.0).abs()
    prev = x["close"].shift(1)
    x["return_bps"] = ((x["close"] / prev - 1.0).abs() * 10000.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    x["range_bps"] = ((x["high"] - x["low"]).abs() / x["close"].replace(0, np.nan) * 10000.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    def causal_z(series: pd.Series) -> pd.Series:
        s = pd.to_numeric(series, errors="coerce").fillna(0.0)
        mean = s.rolling(30, min_periods=5).mean().shift(1)
        std = s.rolling(30, min_periods=5).std(ddof=0).shift(1)
        # A flat prior baseline is still informative: a sudden non-zero jump must not
        # become "undefined" merely because historical variance was exactly zero.
        scale = pd.concat([std.abs(), mean.abs()*0.05, pd.Series(1e-9, index=s.index)], axis=1).max(axis=1)
        return ((s - mean) / scale).replace([np.inf, -np.inf], np.nan).clip(lower=0, upper=8)

    x["z_volume"] = causal_z(x["volume"])
    x["z_notional"] = causal_z(x["abs_notional"])
    x["z_return"] = causal_z(x["return_bps"])
    x["z_range"] = causal_z(x["range_bps"])
    ready = x[["z_volume", "z_notional", "z_return", "z_range"]].notna().any(axis=1)
    blend = (0.30*x["z_volume"].fillna(0) + 0.30*x["z_notional"].fillna(0) +
             0.25*x["z_return"].fillna(0) + 0.15*x["z_range"].fillna(0))
    x["activity_score"] = np.where(ready, np.clip(50.0 + 10.0*blend, 0, 100), 0.0)
    zcols = ["z_volume", "z_notional", "z_return", "z_range"]
    reasons = {"z_volume":"VOLUME", "z_notional":"NOTIONAL", "z_return":"VELOCITY", "z_range":"RANGE"}
    x["activity_reason"] = [reasons.get(max(zcols, key=lambda c: float(r.get(c) or 0)), "COLLECTING") if bool(rd) else "COLLECTING" for r,rd in zip(x.to_dict("records"), ready)]
    x["session_phase"] = [flow_session_phase(t) for t in x["timestamp"]]
    x["activity_ready"] = ready
    return x


def fetch_london_activity_bars(symbol="DIA", live_ticks: pd.DataFrame | None = None, cache_seconds: float = 30.0) -> pd.DataFrame:
    """Observed same-instrument bars from London open onward.

    Alpaca historical bars are used when that exact symbol is entitled there; the
    provider-neutral live tick fabric can be supplied for any instrument. Unsupported
    provider/symbol combinations stay empty rather than falling back to a proxy asset.
    """
    sym = str(symbol or "").upper().strip()
    now_ny = datetime.now(NY)
    day = now_ny.date()
    start = london_open_ny(day)
    end = min(now_ny, datetime.combine(day, time(20, 0), tzinfo=NY))
    if end <= start:
        return pd.DataFrame()
    pieces = []
    import time as _time
    cached = _SESSION_ACTIVITY_CACHE.get(sym)
    if cached and (_time.monotonic() - cached[0]) <= max(float(cache_seconds), 1.0):
        base = cached[1].copy()
        if not base.empty:
            pieces.append(base)
    else:
        base = pd.DataFrame()
        if load_settings():
            try:
                params={'timeframe':'1Min','start':start.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00','Z'),
                        'end':end.astimezone(ZoneInfo('UTC')).isoformat().replace('+00:00','Z'),'feed':load_settings().stock_feed,'limit':10000,'adjustment':'raw'}
                js=_request_json(f'{DATA_BASE}/v2/stocks/{sym}/bars',params)
                base=pd.DataFrame(js.get('bars',[]) or [])
                if not base.empty:
                    base=base.rename(columns={'t':'timestamp','o':'open','h':'high','l':'low','c':'close','v':'volume','vw':'vwap'})
                    base['timestamp']=pd.to_datetime(base['timestamp'],utc=True).dt.tz_convert(EC).dt.tz_localize(None)
                    base=base[[c for c in ('timestamp','open','high','low','close','volume','vwap') if c in base.columns]]
            except Exception:
                base=pd.DataFrame()
        _SESSION_ACTIVITY_CACHE[sym]=(_time.monotonic(), base.copy())
        if not base.empty:
            pieces.append(base)

    if isinstance(live_ticks, pd.DataFrame) and not live_ticks.empty and {"timestamp","price"}.issubset(live_ticks.columns):
        t=live_ticks.copy(); t['timestamp']=pd.to_datetime(t['timestamp'],errors='coerce',utc=True)
        t=t.dropna(subset=['timestamp','price'])
        if not t.empty:
            t['timestamp']=t['timestamp'].dt.tz_convert(EC)
            t=t.set_index('timestamp')
            p=pd.to_numeric(t['price'],errors='coerce')
            ohlc=p.resample('1min').ohlc()
            size=pd.to_numeric(t.get('size',pd.Series(0,index=t.index)),errors='coerce').fillna(0.0)
            vol=size.resample('1min').sum().rename('volume')
            lb=ohlc.join(vol).dropna(subset=['close']).reset_index()
            lb['timestamp']=lb['timestamp'].dt.tz_localize(None)
            lb['vwap']=lb['close']
            pieces.append(lb)
    if not pieces:
        return pd.DataFrame()
    df=pd.concat(pieces,ignore_index=True,sort=False)
    df['timestamp']=pd.to_datetime(df['timestamp'],errors='coerce')
    df=df.dropna(subset=['timestamp']).sort_values('timestamp').drop_duplicates('timestamp',keep='last').reset_index(drop=True)
    for c in ['open','high','low','close','volume','vwap']:
        if c not in df: df[c]=np.nan if c!='volume' else 0.0
        df[c]=pd.to_numeric(df[c],errors='coerce')
    df['vwap']=df['vwap'].fillna((df['high']+df['low']+df['close'])/3).fillna(df['close'])
    sign=np.sign(df['close']-df['open'])
    sign=sign.where(sign!=0,np.sign(df['close'].diff()).fillna(0))
    df['signed_notional_proxy']=sign*df['volume'].fillna(0)*df['vwap'].fillna(df['close'])
    return _activity_scores(df)


def london_activity_tape_summary(symbol="DIA", live_ticks: pd.DataFrame | None = None) -> dict:
    df=fetch_london_activity_bars(symbol, live_ticks=live_ticks)
    if df.empty:
        return {'state':'NO_DATA','session_phase':flow_session_phase(),'pressure':'NEUTRAL','score':0,'net':0,'volume':0,'last':float('nan'),'unusual_events':0,'bars':df,
                'note':'Detector activo desde Londres; sin observaciones propias del instrumento todavía.'}
    net=float(df['signed_notional_proxy'].sum()); gross=float(df['signed_notional_proxy'].abs().sum()); ratio=net/max(gross,1.0)
    pressure='BUY' if ratio>0.12 else 'SELL' if ratio<-0.12 else 'NEUTRAL'
    ready_src=(df['activity_ready'] if 'activity_ready' in df.columns else pd.Series(False,index=df.index,dtype=bool))
    score_src=(df['activity_score'] if 'activity_score' in df.columns else pd.Series(0.0,index=df.index,dtype=float))
    ready=ready_src.fillna(False).astype(bool)
    scores=pd.to_numeric(score_src,errors='coerce').fillna(0.0)
    hot=df[ready & (scores>=70)]
    score=float(pd.to_numeric(hot.get('activity_score',pd.Series(dtype=float)),errors='coerce').max()) if not hot.empty else 0.0
    return {'state':'ACTIVE','session_phase':flow_session_phase(),'pressure':pressure,'score':round(score,2),'net':net,'volume':float(df['volume'].sum()),'last':float(df['close'].iloc[-1]),
            'unusual_events':int(len(hot)),'bars':df,'source':'SAME_INSTRUMENT_SESSION_ACTIVITY','starts_at':'LONDON_OPEN_DST_AWARE',
            'note':'Actividad inusual del underlying desde Londres; OPTION FLOW solo se añade cuando existen trades reales de opciones.'}

def fetch_dia_premarket_bars():
    return fetch_premarket_bars("DIA")

def premarket_tape_summary(symbol="DIA"):
    df=fetch_premarket_bars(symbol)
    if df.empty:return {'state':'NO_DATA','pressure':'NEUTRAL','score':0,'net':0,'volume':0,'last':float('nan'),'bars':df}
    net=float(df['signed_notional_proxy'].sum()); gross=float(df['signed_notional_proxy'].abs().sum()); ratio=net/max(gross,1.0)
    pressure='BUY' if ratio>0.12 else 'SELL' if ratio<-0.12 else 'NEUTRAL'
    score=min(abs(ratio)*100,100)
    return {'state':'OK','pressure':pressure,'score':score,'net':net,'volume':float(df['volume'].sum()),'last':float(df['close'].iloc[-1]),'bars':df}
