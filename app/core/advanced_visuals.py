from __future__ import annotations
import numpy as np
import pandas as pd
import plotly.graph_objects as go


def _latest_enriched(history: pd.DataFrame, enrich_func, cfg):
    if history is None or history.empty:return pd.DataFrame(),None
    h=history.copy(); h['timestamp']=pd.to_datetime(h['timestamp'],errors='coerce'); h=h.dropna(subset=['timestamp'])
    if h.empty:return pd.DataFrame(),None
    ts=h['timestamp'].max(); snap=h[h['timestamp']==ts].copy()
    if snap.empty:return pd.DataFrame(),None
    enr=enrich_func(snap,cfg)
    if 'expiration_date' not in enr.columns:
        enr['expiration_date']=(pd.Timestamp(ts)+pd.to_timedelta(pd.to_numeric(enr['dte'],errors='coerce').fillna(0),unit='D')).dt.date.astype(str)
    enr['expiration_date']=enr['expiration_date'].astype(str)
    return enr,{'timestamp':ts,'spot':float(enr['underlying_price'].iloc[-1])}


def _norm01(s):
    s=pd.to_numeric(s,errors='coerce').fillna(0.0).astype(float)
    if len(s)==0:return s
    lo=float(s.min()); hi=float(s.max())
    if abs(hi-lo)<1e-12:return pd.Series(np.full(len(s),0.5),index=s.index)
    return (s-lo)/(hi-lo)


def add_quant_score(enr: pd.DataFrame, delta_mode=True):
    x=enr.copy(); spot=float(x['underlying_price'].iloc[-1])
    x['gamma_component']=_norm01(np.log1p(pd.to_numeric(x['gross_gex'],errors='coerce').fillna(0).clip(lower=0)))
    x['delta_component']=_norm01(np.log1p(pd.to_numeric(x['option_delta_exposure_info'],errors='coerce').fillna(0).abs()))
    x['oi_component']=_norm01(np.log1p(pd.to_numeric(x['open_interest'],errors='coerce').fillna(0).clip(lower=0)))
    x['volume_component']=_norm01(np.log1p(pd.to_numeric(x['volume'],errors='coerce').fillna(0).clip(lower=0)))
    x['activity_ratio']=pd.to_numeric(x['volume'],errors='coerce').fillna(0)/(pd.to_numeric(x['open_interest'],errors='coerce').fillna(0)+1)
    x['activity_component']=_norm01(np.log1p(x['activity_ratio'].clip(lower=0)))
    span=max(float(x['strike'].max()-x['strike'].min()),1.0)
    x['proximity_component']=(1-(x['strike'].astype(float)-spot).abs()/max(span/2,1)).clip(0,1)
    x['expiry_component']=np.exp(-pd.to_numeric(x['dte'],errors='coerce').fillna(30)/14).clip(0,1)
    if delta_mode:
        x['quant_score']=100*(.28*x['gamma_component']+.18*x['delta_component']+.20*x['oi_component']+.14*x['volume_component']+.08*x['activity_component']+.07*x['proximity_component']+.05*x['expiry_component'])
    else:
        x['quant_score']=100*(.35*x['gamma_component']+.23*x['oi_component']+.16*x['volume_component']+.10*x['activity_component']+.10*x['proximity_component']+.06*x['expiry_component'])
    x['quant_score']=x['quant_score'].clip(0,100)
    return x


def _nice_threshold(v: float) -> int:
    v=max(float(v or 0),1.0)
    steps=[50,100,250,500,1000,2500,5000,10000,25000,50000,100000]
    return min(steps,key=lambda x:abs(x-v))

def _bootstrap_threshold(ticks: pd.DataFrame | None) -> int:
    """Cold-start threshold derived from observed trade size, never a ticker table."""
    try:
        if ticks is None or ticks.empty or 'size' not in ticks.columns:return 250
        sz=pd.to_numeric(ticks['size'],errors='coerce').dropna(); sz=sz[sz>0]
        if len(sz)<20:return 250
        return _nice_threshold(float(sz.median())*40.0)
    except Exception:return 250

def _aggression_threshold(ticks: pd.DataFrame, requested='AUTO', symbol='UNKNOWN') -> int:
    req=str(requested or 'AUTO').upper().replace(',','').strip()
    fixed={'250':250,'500':500,'1K':1000,'1000':1000,'2.5K':2500,'2500':2500,'5K':5000,'5000':5000}
    if req in fixed:return fixed[req]
    floor=_bootstrap_threshold(ticks)
    if ticks is None or ticks.empty or 'size' not in ticks.columns:return floor
    x=ticks.copy(); x['timestamp']=pd.to_datetime(x.get('timestamp'),errors='coerce'); x=x.dropna(subset=['timestamp'])
    if x.empty:return floor
    x=x.sort_values('timestamp').tail(12000)
    size=pd.to_numeric(x['size'],errors='coerce').fillna(0).clip(lower=0)
    sign=pd.to_numeric(x.get('aggressor_sign',0),errors='coerce').fillna(0)
    if not sign.abs().sum():
        px=pd.to_numeric(x.get('price'),errors='coerce'); sign=np.sign(px.diff()).replace(0,np.nan).ffill().fillna(0)
    x['_sv']=size*sign
    try:
        sec=x.set_index('timestamp')['_sv'].resample('15s').sum().abs(); sec=sec[sec>0]
        raw=max(float(sec.median())*1.6,float(sec.quantile(.70))*1.15) if len(sec)>=4 else float(size[size>0].median() if (size>0).any() else floor)*6
    except Exception:raw=floor
    return max(floor,_nice_threshold(raw))


def trace_landscape_3d(history, spot=None, lens='Gamma', window_strikes=21,
                       scale_mode='session', scale_anchors=None, symbol='UNKNOWN'):
    """TRACE 3D · strike × time × signed exposure.

    Surface HEIGHT remains physical M$ exposure. Session/anchor normalization is mapped
    only to surface color. The yellow line is the true Gamma flip even when the surface
    lens is Delta or Charm. Missing requested lenses fail visibly instead of falling back.
    """
    from .trace_analytics import session_landscape
    ln=str(lens or 'Gamma').strip().lower()
    key='delta_exposure' if ln.startswith('delta') else 'charm_exposure' if ln.startswith('charm') else 'gross_gex'
    L=session_landscape(history,lens=ln,window_strikes=window_strikes,scale_mode=scale_mode,
                        anchor=(scale_anchors or {}).get(key),spot=spot,symbol=symbol)
    fig=go.Figure()
    if not L.get('ready'):
        fig.update_layout(template='plotly_dark',paper_bgcolor='#080b10',plot_bgcolor='#080b10',height=620,
                          annotations=[dict(text=f"TRACE 3D · {L.get('reason','sin datos')}",showarrow=False,
                                            font=dict(color='#7b8496',size=14))],
                          title=f"TRACE 3D · {symbol} · {str(lens).upper()}")
        return fig
    z=np.asarray(L['z'],dtype=float)
    sc=np.asarray(L['z_scaled'],dtype=float)
    strikes=L['strikes'];times=L['times'];x=list(range(len(times)))
    custom=np.empty((len(strikes),len(times),2),dtype=object)
    labels=[pd.Timestamp(t).strftime('%H:%M') for t in times]
    for i,k in enumerate(strikes):
        for j,t in enumerate(labels): custom[i,j]=[t,k]
    fig.add_trace(go.Surface(x=x,y=strikes,z=z,surfacecolor=sc,
        colorscale=[[0.0,'#d85a5a'],[0.5,'#0e1117'],[1.0,'#5ca868']],cmin=-100,cmax=100,
        opacity=.96,name='Exposición',customdata=custom,
        colorbar=dict(title='INTENSIDAD',thickness=12,len=.58,x=1.02),
        hovertemplate='Hora %{customdata[0]}<br>Strike %{y:.2f}<br>Exposición %{z:.2f}M<extra></extra>'))
    flips=np.asarray(L.get('gamma_flip_line') or [],dtype=float)
    finite_z=z[np.isfinite(z)]
    z_floor=float(np.nanmin(finite_z)) if finite_z.size else 0.0
    if len(flips)==len(x) and np.isfinite(flips).any():
        fig.add_trace(go.Scatter3d(x=x,y=flips,z=[z_floor]*len(x),mode='lines',
            line=dict(color='#e1b800',width=7),name='Gamma Flip (siempre Gamma)',
            hovertemplate='Gamma Flip %{y:.2f}<extra></extra>'))
    price=np.asarray(L.get('price_line') or [],dtype=float)
    z_top=float(np.nanmax(finite_z)) if finite_z.size else 0.0
    if len(price)==len(x) and np.isfinite(price).any():
        fig.add_trace(go.Scatter3d(x=x,y=price,z=[z_top]*len(x),mode='lines+markers',
            line=dict(color='#f2f5f9',width=5),marker=dict(size=2,color='#f2f5f9'),name='Precio',
            hovertemplate='Precio %{y:.2f}<extra></extra>'))
    step=max(1,len(labels)//8)
    fig.update_layout(template='plotly_dark',paper_bgcolor='#080b10',plot_bgcolor='#080b10',height=660,
        margin=dict(l=0,r=0,t=52,b=0),
        title=dict(text=f"TRACE 3D · {symbol} · {str(lens).upper()} · ALTURA REAL M$ · COLOR {L.get('denominator_source','')}",font=dict(size=13,color='#c9d2e0')),
        scene=dict(xaxis=dict(title='Hora',tickvals=x[::step],ticktext=labels[::step],backgroundcolor='#080b10',gridcolor='#1b2130',color='#7b8496'),
                   yaxis=dict(title='Strike',backgroundcolor='#080b10',gridcolor='#1b2130',color='#7b8496'),
                   zaxis=dict(title='Exposición firmada (M$)',backgroundcolor='#080b10',gridcolor='#1b2130',color='#7b8496'),
                   camera=dict(eye=dict(x=1.7,y=-1.5,z=.85)),aspectratio=dict(x=1.6,y=1.0,z=.6)),
        legend=dict(orientation='h',y=1.05,x=0,font=dict(size=10,color='#7b8496')))
    return fig

# =============================================================================
# v1.16.0 · TRACE FUSION + selectable 3D exposure styles
# =============================================================================


