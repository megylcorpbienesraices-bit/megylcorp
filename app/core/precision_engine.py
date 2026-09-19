"""Precision / model-health utilities for ITM QUANT v1.14.1.

The goal is to make every quantitative number explainable and auditable.  This
module improves IV/Greeks integrity, model inputs, exposure sensitivity and
health diagnostics without relabeling internal scores as probabilities.
"""

from __future__ import annotations

import functools
import json
import math
import os
from typing import Any, Dict, Optional, Tuple, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm
from app.persistence import PERSISTENT_ROOT, routed_dir
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .expiry_clock import year_fraction, year_fraction_array
from .instruments import get as get_instrument, MODEL_FUTURE

NY = ZoneInfo("America/New_York")
MACRO_CACHE = routed_dir(PERSISTENT_ROOT, "research") / "macro_dia_cache.json"

# Conservative per-asset carry fallbacks.  They are explicit model inputs, not
# live dividend forecasts.  Users can override each symbol through ENV.
ASSET_DIVIDEND_YIELD = {
    "DIA": 0.017,
    "SPY": 0.012,
    "QQQ": 0.005,
    "TQQQ": 0.000,
    "GLD": 0.000,
    "GDX": 0.014,
    "AAPL": 0.004,
    "NVDA": 0.000,
    "MSFT": 0.006,
    "META": 0.003,
    "AMZN": 0.000,
    "TSLA": 0.000,
    "VXX": 0.000,
}


@functools.lru_cache(maxsize=8)
def _macro_series(mtime: float) -> tuple[float | None, float | None]:
    """Parse the local macro cache once per file version instead of once per contract.

    v1.14: the previous implementation re-read and re-parsed macro_dia_cache.json on
    every single market_inputs() call, i.e. thousands of times per exposure/attribution
    pass. Keyed on mtime so an updated cache invalidates automatically.
    """
    try:
        payload = json.loads(MACRO_CACHE.read_text(encoding="utf-8"))
        series = payload.get("series", {}) or {}
        dff = series.get("DFF", {}).get("value")
        dgs2 = series.get("DGS2", {}).get("value")
        dff = float(dff) / 100.0 if dff is not None and math.isfinite(float(dff)) else None
        dgs2 = float(dgs2) / 100.0 if dgs2 is not None and math.isfinite(float(dgs2)) else None
        return dff, dgs2
    except Exception:
        return None, None


def _cached_macro_rate(dte: float | None = None) -> tuple[float | None, str | None]:
    """Best-effort short-rate input from the local official-data macro cache.

    For the <=30 day option horizon used by the platform, Effective Fed Funds is
    the most relevant readily available public short-rate anchor.  If it is not
    present we fall back to the 2Y Treasury.  This never makes a network call.
    """
    try:
        if not MACRO_CACHE.exists():
            return None, None
        dff, dgs2 = _macro_series(MACRO_CACHE.stat().st_mtime)
        if dff is not None and dgs2 is not None:
            # Smooth term interpolation.  At intraday/weekly DTE almost all weight
            # remains on the overnight/short-rate anchor; farther maturities lean
            # progressively toward 2Y.
            days = max(float(dte or 0.0), 0.0)
            w = float(np.clip(days / 730.0, 0.0, 1.0))
            return (1.0 - w) * dff + w * dgs2, "FRED CACHE · DFF→DGS2 TERM BLEND"
        if dff is not None:
            return dff, "FRED CACHE · EFFECTIVE FED FUNDS"
        if dgs2 is not None:
            return dgs2, "FRED CACHE · TREASURY 2Y"
    except Exception as _e:
        _obs_note('precision_engine:89', _e)
    return None, None


@functools.lru_cache(maxsize=4096)
def _market_inputs_cached(symbol: str, dte_key: float | None, env_stamp: tuple) -> Dict[str, Any]:
    return _market_inputs_impl(symbol, dte_key)


def market_inputs(symbol: str, dte: float | None = None) -> Dict[str, Any]:
    """Memoised wrapper. r/q are flat in DTE at this horizon, so quantising the DTE
    key to 1e-3 years changes nothing numerically but collapses thousands of
    identical recomputations into one lookup."""
    key = None if dte is None else round(float(dte), 3)
    sym = str(symbol or "DIA").upper()
    try:
        macro_stamp = MACRO_CACHE.stat().st_mtime if MACRO_CACHE.exists() else None
    except Exception:
        macro_stamp = None
    env_stamp = (os.getenv("ITM_RISK_FREE_RATE"), os.getenv(f"ITM_DIVIDEND_YIELD_{sym}"), macro_stamp)
    return dict(_market_inputs_cached(sym, key, env_stamp))


def _market_inputs_impl(symbol: str, dte: float | None = None) -> Dict[str, Any]:
    """Explicit model inputs used when repricing options.

    Priority for risk-free rate:
      1) explicit ENV override;
      2) local FRED cache (DFF / DGS2 term blend);
      3) conservative fallback.

    Dividend/carry is symbol-specific and can also be overridden via ENV.
    """
    env_rate = os.getenv("ITM_RISK_FREE_RATE")
    if env_rate is not None:
        try:
            r = float(env_rate); r_source = "ENV ITM_RISK_FREE_RATE"
        except Exception:
            r = 0.045; r_source = "CONFIG FALLBACK"
    else:
        cached, source = _cached_macro_rate(dte)
        if cached is not None:
            r, r_source = cached, str(source)
        else:
            r, r_source = 0.045, "CONFIG FALLBACK"
    r = float(np.clip(r, -0.02, 0.20))

    sym = str(symbol or "DIA").upper()
    q_env = os.getenv(f"ITM_DIVIDEND_YIELD_{sym}")
    try:
        q = float(q_env) if q_env is not None else float(ASSET_DIVIDEND_YIELD.get(sym, 0.0))
    except Exception:
        q = float(ASSET_DIVIDEND_YIELD.get(sym, 0.0))
    q = float(np.clip(q, -0.10, 0.30))
    return {
        "risk_free_rate": r,
        "dividend_yield": q,
        "risk_free_source": r_source,
        "dividend_source": f"ENV ITM_DIVIDEND_YIELD_{sym}" if q_env is not None else "ASSET-SPECIFIC FALLBACK",
        "dte_input": None if dte is None else float(dte),
    }


def _bs_d1_d2(S: float, K: float, T: float, sigma: float, r: float, q: float) -> Tuple[float, float]:
    S = max(float(S), 1e-12); K = max(float(K), 1e-12)
    T = max(float(T), 1e-10); sigma = max(float(sigma), 1e-8)
    root = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * root)
    return d1, d1 - sigma * root


def black_scholes_price(S: float, K: float, T: float, sigma: float, option_type: str,
                        r: float, q: float) -> float:
    d1, d2 = _bs_d1_d2(S, K, T, sigma, r, q)
    disc_q = math.exp(-q * T); disc_r = math.exp(-r * T)
    is_call = str(option_type).lower().startswith("c")
    if is_call:
        return float(S * disc_q * norm.cdf(d1) - K * disc_r * norm.cdf(d2))
    return float(K * disc_r * norm.cdf(-d2) - S * disc_q * norm.cdf(-d1))


def black_scholes_greeks_full(S: float, K: float, T: float, sigma: float, option_type: str,
                              r: float, q: float) -> Dict[str, float]:
    """Delta/Gamma plus higher-order Greeks used by the precision engine.

    Vanna = dDelta/dSigma.
    Charm uses the common calendar-time delta-decay convention (per year).
    Speed = dGamma/dS.
    """
    S=max(float(S),1e-12);K=max(float(K),1e-12);T=max(float(T),1e-10);sigma=max(float(sigma),1e-8)
    d1,d2=_bs_d1_d2(S,K,T,sigma,r,q); root=math.sqrt(T); disc_q=math.exp(-q*T); phi=norm.pdf(d1)
    is_call=str(option_type).lower().startswith("c")
    delta=disc_q*(norm.cdf(d1) if is_call else norm.cdf(d1)-1.0)
    gamma=disc_q*phi/(S*sigma*root)
    vanna=-disc_q*phi*d2/sigma
    # Calendar-time charm (delta decay).  Sign convention is disclosed in UI/manual.
    base=(2.0*(r-q)*T - d2*sigma*root)/(2.0*T*sigma*root)
    if is_call:
        charm=q*disc_q*norm.cdf(d1)-disc_q*phi*base
    else:
        charm=-q*disc_q*norm.cdf(-d1)-disc_q*phi*base
    speed=-gamma/S*(d1/(sigma*root)+1.0)
    return {"delta":float(delta),"gamma":float(gamma),"vanna":float(vanna),"charm":float(charm),"speed":float(speed)}


def black_scholes_greeks_vector(S, K, T, sigma, is_call, r, q) -> Dict[str, np.ndarray]:
    """Vectorised twin of :func:`black_scholes_greeks_full`.

    v1.23 routes the hot matrix kernel through JAX when the accelerated backend is
    enabled.  The public function still returns NumPy arrays and retains the exact
    deterministic NumPy implementation as fallback, so research/replay semantics do
    not depend on GPU availability.
    """
    try:
        from .accelerated_quant import black_scholes_batch
        return black_scholes_batch(S,K,T,sigma,is_call,r,q,prefer_accelerated=True)
    except Exception as _e:
        _obs_note('precision_engine:206', _e)
    S = np.maximum(np.asarray(S, dtype=float), 1e-12)
    K = np.maximum(np.asarray(K, dtype=float), 1e-12)
    T = np.maximum(np.asarray(T, dtype=float), 1e-10)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-8)
    r = np.asarray(r, dtype=float); q = np.asarray(q, dtype=float)
    is_call = np.asarray(is_call, dtype=bool)
    root = np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * root)
    d2 = d1 - sigma * root
    disc_q = np.exp(-q * T); phi = norm.pdf(d1)
    delta = disc_q * np.where(is_call, norm.cdf(d1), norm.cdf(d1) - 1.0)
    gamma = disc_q * phi / (S * sigma * root)
    vanna = -disc_q * phi * d2 / sigma
    base = (2.0 * (r - q) * T - d2 * sigma * root) / (2.0 * T * sigma * root)
    charm = np.where(is_call, q * disc_q * norm.cdf(d1) - disc_q * phi * base, -q * disc_q * norm.cdf(-d1) - disc_q * phi * base)
    speed = -gamma / S * (d1 / (sigma * root) + 1.0)
    return {"delta": delta, "gamma": gamma, "vanna": vanna, "charm": charm, "speed": speed}



def _b76_d1_d2(F: float, K: float, T: float, sigma: float) -> Tuple[float, float]:
    F=max(float(F),1e-12);K=max(float(K),1e-12);T=max(float(T),1e-10);sigma=max(float(sigma),1e-8)
    root=math.sqrt(T)
    d1=(math.log(F/K)+0.5*sigma*sigma*T)/(sigma*root)
    return d1,d1-sigma*root


def black_76_price(F: float, K: float, T: float, sigma: float, option_type: str, r: float) -> float:
    """Black-76 futures-option price using the observed futures price ``F``."""
    d1,d2=_b76_d1_d2(F,K,T,sigma);disc=math.exp(-float(r)*max(float(T),1e-10))
    is_call=str(option_type).lower().startswith("c")
    if is_call:
        return float(disc*(F*norm.cdf(d1)-K*norm.cdf(d2)))
    return float(disc*(K*norm.cdf(-d2)-F*norm.cdf(-d1)))


def black_76_greeks_full(F: float, K: float, T: float, sigma: float, option_type: str, r: float) -> Dict[str,float]:
    """Black-76 Delta/Gamma/Vanna/Charm/Speed with respect to the futures price.

    ``charm`` follows the same calendar-time delta-decay convention used by the
    Black-Scholes path in this module (time advances while expiry approaches).
    """
    F=max(float(F),1e-12);K=max(float(K),1e-12);T=max(float(T),1e-10);sigma=max(float(sigma),1e-8);r=float(r)
    d1,d2=_b76_d1_d2(F,K,T,sigma);root=math.sqrt(T);disc=math.exp(-r*T);phi=norm.pdf(d1)
    is_call=str(option_type).lower().startswith("c")
    delta=disc*(norm.cdf(d1) if is_call else norm.cdf(d1)-1.0)
    gamma=disc*phi/(F*sigma*root)
    vanna=-disc*phi*d2/sigma
    log_fk=math.log(F/K)
    dd1_dT=-log_fk/(2.0*sigma*(T**1.5)) + sigma/(4.0*root)
    charm=r*delta-disc*phi*dd1_dT
    speed=-gamma/F*(d1/(sigma*root)+1.0)
    return {"delta":float(delta),"gamma":float(gamma),"vanna":float(vanna),"charm":float(charm),"speed":float(speed)}


def black_76_greeks_vector(F,K,T,sigma,is_call,r) -> Dict[str,np.ndarray]:
    F=np.maximum(np.asarray(F,dtype=float),1e-12);K=np.maximum(np.asarray(K,dtype=float),1e-12)
    T=np.maximum(np.asarray(T,dtype=float),1e-10);sigma=np.maximum(np.asarray(sigma,dtype=float),1e-8)
    r=np.asarray(r,dtype=float);is_call=np.asarray(is_call,dtype=bool)
    root=np.sqrt(T);log_fk=np.log(F/K);d1=(log_fk+0.5*sigma*sigma*T)/(sigma*root);d2=d1-sigma*root
    disc=np.exp(-r*T);phi=norm.pdf(d1)
    delta=disc*np.where(is_call,norm.cdf(d1),norm.cdf(d1)-1.0)
    gamma=disc*phi/(F*sigma*root)
    vanna=-disc*phi*d2/sigma
    dd1_dT=-log_fk/(2.0*sigma*np.power(T,1.5))+sigma/(4.0*root)
    charm=r*delta-disc*phi*dd1_dT
    speed=-gamma/F*(d1/(sigma*root)+1.0)
    return {"delta":delta,"gamma":gamma,"vanna":vanna,"charm":charm,"speed":speed}


def _bs_vega(S: float, K: float, T: float, sigma: float, q: float) -> float:
    """Vega de Black-Scholes. Es la derivada del precio respecto a sigma, o sea
    EXACTAMENTE la cantidad de senal que un precio contiene sobre la volatilidad.
    Si vale cero, la IV no esta definida y no hay inversion que valga."""
    try:
        d1,_=_bs_d1_d2(S,K,T,sigma,0.0,q)
        return float(S*math.exp(-q*T)*norm.pdf(d1)*math.sqrt(T))
    except Exception:
        return 0.0


def _b76_vega(F: float, K: float, T: float, sigma: float, r: float) -> float:
    try:
        d1,_=_b76_d1_d2(F,K,T,sigma)
        return float(math.exp(-r*T)*F*norm.pdf(d1)*math.sqrt(T))
    except Exception:
        return 0.0


# Una vega por debajo de esto no distingue una sigma de otra: el precio es el mismo
# numero en coma flotante para todo un rango de volatilidades.
_IV_MIN_VEGA = 1e-9


def implied_volatility_black_76(price: float,F: float,K: float,T: float,option_type: str,r: float) -> float:
    try:
        price=float(price);F=float(F);K=float(K);T=max(float(T),1e-8);r=float(r)
    except Exception:
        return float("nan")
    if not all(map(math.isfinite,(price,F,K,T,r))) or price<=0 or F<=0 or K<=0:
        return float("nan")
    disc=math.exp(-r*T);is_call=str(option_type).lower().startswith("c")
    lb=disc*max(0.0,(F-K) if is_call else (K-F));ub=disc*(F if is_call else K)
    eps=max(1e-6,F*1e-9)
    if price<lb-eps or price>ub+eps:return float("nan")
    def f(sig:float)->float:return black_76_price(F,K,T,sig,option_type,r)-price
    try:
        lo,hi=.005,5.0;flo,fhi=f(lo),f(hi)
        # Un "acierto" en el extremo del bracket solo es una IV si ahi hay vega. Sin
        # vega, f() es plana y f(lo)==0 significa "este precio es el de CUALQUIER
        # sigma pequena", no "la sigma es 0.005". Devolver el extremo publicaba un
        # numero inventado con la misma pinta que uno medido.
        if flo==0:return lo if _b76_vega(F,K,T,lo,r)>_IV_MIN_VEGA else float("nan")
        if fhi==0:return hi if _b76_vega(F,K,T,hi,r)>_IV_MIN_VEGA else float("nan")
        if flo*fhi>0:return float("nan")
        return float(brentq(f,lo,hi,xtol=1e-8,rtol=1e-8,maxiter=150))
    except Exception:return float("nan")


def greeks_full_for_symbol(symbol: str,S: float,K: float,T: float,sigma: float,option_type: str,r: float,q: float) -> Dict[str,float]:
    if get_instrument(symbol).option_model==MODEL_FUTURE:
        return black_76_greeks_full(S,K,T,sigma,option_type,r)
    return black_scholes_greeks_full(S,K,T,sigma,option_type,r,q)


def greeks_vector_for_symbol(symbol: str,S,K,T,sigma,is_call,r,q) -> Dict[str,np.ndarray]:
    if get_instrument(symbol).option_model==MODEL_FUTURE:
        return black_76_greeks_vector(S,K,T,sigma,is_call,r)
    return black_scholes_greeks_vector(S,K,T,sigma,is_call,r,q)


def option_price_for_symbol(symbol: str,S: float,K: float,T: float,sigma: float,option_type: str,r: float,q: float) -> float:
    if get_instrument(symbol).option_model==MODEL_FUTURE:
        return black_76_price(S,K,T,sigma,option_type,r)
    return black_scholes_price(S,K,T,sigma,option_type,r,q)


def implied_volatility_for_symbol(symbol: str,price: float,S: float,K: float,T: float,option_type: str,r: float,q: float) -> float:
    if get_instrument(symbol).option_model==MODEL_FUTURE:
        return implied_volatility_black_76(price,S,K,T,option_type,r)
    return implied_volatility_from_price(price,S,K,T,option_type,r,q)


def model_inputs_vector(symbol: str, dte_array) -> Tuple[np.ndarray, np.ndarray]:
    """Per-contract r/q as arrays, in O(1) calls instead of O(contracts).

    The rate curve used by market_inputs is (1-w)*DFF + w*DGS2 with w = dte/730
    clipped to [0,1], i.e. exactly linear in DTE below 730 days. So evaluating two
    anchor points and interpolating reproduces the scalar path bit-for-bit while
    avoiding one dict build per contract. q does not depend on DTE at all.
    """
    dte = np.nan_to_num(np.asarray(dte_array, dtype=float), nan=0.0)
    q_val = float(market_inputs(symbol, 0.0)["dividend_yield"])
    q_out = np.full(dte.shape, q_val, dtype=float)
    if dte.size == 0:
        return np.zeros_like(dte), q_out
    lo, hi = float(np.min(dte)), float(np.max(dte))
    r_lo = float(market_inputs(symbol, lo)["risk_free_rate"])
    if hi - lo < 1e-9:
        return np.full(dte.shape, r_lo, dtype=float), q_out
    r_hi = float(market_inputs(symbol, hi)["risk_free_rate"])
    if abs(r_hi - r_lo) < 1e-15:
        return np.full(dte.shape, r_lo, dtype=float), q_out
    r_out = r_lo + (r_hi - r_lo) * (dte - lo) / (hi - lo)
    return r_out, q_out


def black_scholes_delta_gamma(S: float, K: float, T: float, sigma: float, option_type: str,
                              r: float, q: float) -> Tuple[float, float]:
    g=black_scholes_greeks_full(S,K,T,sigma,option_type,r,q)
    return g["delta"],g["gamma"]


def _iv_via_otm_twin(price: float, S: float, K: float, T: float, option_type: str,
                     r: float, q: float) -> float:
    """Ultimo recurso para contratos MUY dentro de dinero: invertir el gemelo OTM.

    Una call y una put del mismo strike comparten vega EXACTAMENTE, pero su
    condicionamiento numerico no tiene nada que ver. En una call profundamente ITM
    casi todo el precio es valor intrinseco: el valor temporal -- lo unico que
    depende de sigma -- queda por debajo de la precision de doble, la funcion
    objetivo se aplana y brentq no encuentra ni cambio de signo. Ahi devolviamos NaN
    y se perdia el contrato entero.

    La paridad put-call es una identidad exacta, no un modelo:

        C - P = S*e^(-qT) - K*e^(-rT)

    asi que el precio del gemelo OTM se deduce sin suponer nada, y ese si es puro
    valor temporal: la inversion vuelve a estar bien planteada. Es la misma razon
    por la que las superficies de volatilidad institucionales se construyen con
    cotizaciones OTM. Si el gemelo tampoco es invertible, se devuelve NaN.
    """
    try:
        disc_q=math.exp(-q*T);disc_r=math.exp(-r*T)
        is_call=str(option_type).lower().startswith("c")
        parity=S*disc_q-K*disc_r
        forward_itm=(K<S*math.exp((r-q)*T)) if is_call else (K>S*math.exp((r-q)*T))
        if not forward_itm:
            return float("nan")
        twin_price=price-parity if is_call else price+parity
        twin_type="put" if is_call else "call"
        if not math.isfinite(twin_price) or twin_price<=0:
            return float("nan")
        # Suelo de identificabilidad. El precio del gemelo ES toda la senal sobre
        # sigma; por debajo de la resolucion representable del precio original no
        # queda informacion, solo ruido de coma flotante. Devolver aqui el extremo
        # del bracket (0.005) seria PEOR que devolver NaN: publicaria una IV
        # inventada con la misma apariencia que una medida.
        noise_floor=max(4.0*float(np.finfo(float).eps)*max(abs(float(price)),S,K),1e-12)
        if twin_price<=noise_floor:
            return float("nan")
        def g(sig:float)->float:return black_scholes_price(S,K,T,sig,twin_type,r,q)-twin_price
        lo,hi=.005,5.0
        glo,ghi=g(lo),g(hi)
        # Sin cambio de signo no hay raiz. No se aceptan "aciertos" en los extremos
        # del bracket: en un gemelo aplanado por underflow g(lo) vale 0 sin que
        # 0.005 sea la volatilidad del contrato.
        if not (math.isfinite(glo) and math.isfinite(ghi)) or glo*ghi>=0:
            return float("nan")
        sigma=float(brentq(g,lo,hi,xtol=1e-8,rtol=1e-8,maxiter=150))
        # Verificacion final contra el precio REAL cotizado, no contra el gemelo:
        # cierra el circulo de la paridad y descarta cualquier raiz espuria.
        reprice=black_scholes_price(S,K,T,sigma,option_type,r,q)
        if not math.isfinite(reprice) or abs(reprice-float(price))>max(1e-6,abs(float(price))*1e-6):
            return float("nan")
        return sigma
    except Exception as _e:
        _obs_note('precision_engine:iv_otm_twin', _e)
        return float("nan")


def implied_volatility_from_price(price: float, S: float, K: float, T: float, option_type: str,
                                  r: float, q: float) -> float:
    """Robust bounded IV inversion. Returns NaN when quote violates simple no-arbitrage bounds."""
    try:
        price=float(price);S=float(S);K=float(K);T=max(float(T),1e-8)
    except Exception:
        return float("nan")
    if not all(map(math.isfinite,(price,S,K,T))) or price<=0 or S<=0 or K<=0:
        return float("nan")
    disc_q=math.exp(-q*T);disc_r=math.exp(-r*T)
    if str(option_type).lower().startswith("c"):
        intrinsic_lb=max(0.0,S*disc_q-K*disc_r);ub=S*disc_q
    else:
        intrinsic_lb=max(0.0,K*disc_r-S*disc_q);ub=K*disc_r
    eps=max(1e-6,S*1e-9)
    if price<intrinsic_lb-eps or price>ub+eps:return float("nan")
    def f(sig:float)->float:return black_scholes_price(S,K,T,sig,option_type,r,q)-price
    lo,hi=.005,5.0
    try:
        flo,fhi=f(lo),f(hi)
        if flo==0 and _bs_vega(S,K,T,lo,q)>_IV_MIN_VEGA:return lo
        if fhi==0 and _bs_vega(S,K,T,hi,q)>_IV_MIN_VEGA:return hi
        if flo!=0 and fhi!=0 and flo*fhi<0:
            return float(brentq(f,lo,hi,xtol=1e-8,rtol=1e-8,maxiter=150))
    except Exception as _e:
        _obs_note('precision_engine:iv_direct', _e)
    return _iv_via_otm_twin(price,S,K,T,option_type,r,q)


def american_option_price_crr(S: float, K: float, T: float, sigma: float, option_type: str,
                              r: float, q: float, steps: int = 120) -> float:
    """Cox-Ross-Rubinstein American option diagnostic.

    It is intentionally used as a validation layer, not the main full-chain engine,
    because the platform needs fast intraday recalculation.
    """
    try:
        S=float(S);K=float(K);T=float(T);sigma=float(sigma);steps=max(25,min(int(steps),400))
        if not all(map(math.isfinite,(S,K,T,sigma,r,q))) or min(S,K,T,sigma)<=0:return float("nan")
        dt=T/steps;u=math.exp(sigma*math.sqrt(dt));d=1.0/u
        growth=math.exp((r-q)*dt);p=(growth-d)/(u-d)
        if not (0.0<=p<=1.0):return float("nan")
        disc=math.exp(-r*dt);is_call=str(option_type).lower().startswith("c")
        j=np.arange(steps+1);spots=S*(u**j)*(d**(steps-j))
        values=np.maximum(spots-K,0.0) if is_call else np.maximum(K-spots,0.0)
        for n in range(steps-1,-1,-1):
            values=disc*(p*values[1:]+(1-p)*values[:-1])
            j=np.arange(n+1);spots=S*(u**j)*(d**(n-j))
            exercise=np.maximum(spots-K,0.0) if is_call else np.maximum(K-spots,0.0)
            values=np.maximum(values,exercise)
        return float(values[0])
    except Exception:return float("nan")


def recover_iv_and_greeks(*, provider_iv: float, provider_delta: float, provider_gamma: float,
                          bid: float, ask: float, last: float, S: float, K: float, dte: float,
                          option_type: str, symbol: str, provider_name: str = "ALPACA") -> Dict[str, Any]:
    """Recover/validate IV and Greeks without silently dropping usable 0DTE contracts.

    Priority:
      1) provider IV when finite;
      2) invert a valid provider bid/ask midpoint;
      3) last trade only as a clearly-labelled fallback.

    Delta/Gamma/Vanna/Charm/Speed are always recomputed with the exact model inputs so
    the chain has one internally consistent Greek surface. Provider Delta/Gamma are kept
    as diagnostics and can trigger a GREEKS DISLOCATION flag; they never silently replace
    the model values inside ITM QUANT.
    """
    mi=market_inputs(symbol,dte);r=mi["risk_free_rate"];q=mi["dividend_yield"]
    T=year_fraction(float(dte))

    def fnum(v):
        try:
            x=float(v);return x if math.isfinite(x) else float("nan")
        except Exception:return float("nan")

    p_iv=fnum(provider_iv);pdlt=fnum(provider_delta);pgam=fnum(provider_gamma)
    b=fnum(bid);a=fnum(ask);l=fnum(last)
    provider=str(provider_name or "PROVIDER").upper()
    model_name="BLACK_76" if get_instrument(symbol).option_model==MODEL_FUTURE else "BLACK_SCHOLES"
    itm_label="ITM_QUANT_BLACK_76" if model_name=="BLACK_76" else "ITM_QUANT"
    used_price=float("nan");quote_source="NONE";iv_source=provider;iv=p_iv if p_iv>0 else float("nan")
    if not math.isfinite(iv):
        if math.isfinite(b) and math.isfinite(a) and b>0 and a>=b:
            used_price=(b+a)/2.0;quote_source=f"{provider} MID"
        elif math.isfinite(l) and l>0:
            used_price=l;quote_source="LAST TRADE FALLBACK"
        if math.isfinite(used_price):
            iv=implied_volatility_for_symbol(symbol,used_price,S,K,T,option_type,r,q)
            if math.isfinite(iv):iv_source=itm_label
    if not math.isfinite(iv) or iv<=0:
        return {"iv":float("nan"),"iv_source":"UNAVAILABLE","quote_source":quote_source,
                "calc_delta":float("nan"),"calc_gamma":float("nan"),"calc_vanna":float("nan"),
                "calc_charm":float("nan"),"calc_speed":float("nan"),"vanna":float("nan"),
                "charm":float("nan"),"speed":float("nan"),"greeks_source":"UNAVAILABLE",
                "provider_delta_gap":float("nan"),"provider_delta_diff":float("nan"),
                "provider_gamma_gap_pct":float("nan"),"provider_gamma_diff_pct":float("nan"),
                "greeks_dislocation":False,"pricing_model":model_name,"quote_price_used":used_price,**mi}

    g=greeks_full_for_symbol(symbol,S,K,T,iv,option_type,r,q)
    provider_ok=math.isfinite(pdlt) and math.isfinite(pgam)
    if model_name=="BLACK_SCHOLES":
        if provider_ok:
            greek_source=f"{provider} + ITM QUANT CHECK" if iv_source==provider else "ITM_QUANT"
        else:
            greek_source=f"ITM_QUANT · {provider} IV" if iv_source==provider else "ITM_QUANT"
    else:
        if provider_ok:
            greek_source=f"{provider} + ITM_QUANT BLACK_76 CHECK" if iv_source==provider else "ITM_QUANT BLACK_76"
        else:
            greek_source=f"ITM_QUANT BLACK_76 · {provider} IV" if iv_source==provider else "ITM_QUANT BLACK_76"
    delta_gap=g["delta"]-pdlt if math.isfinite(pdlt) else float("nan")
    gamma_gap_pct=100.0*(g["gamma"]-pgam)/max(abs(pgam),1e-12) if math.isfinite(pgam) else float("nan")
    # Diagnostic thresholds intentionally conservative. A flag means model/provider
    # disagree materially; it does NOT decide BUY/SELL by itself.
    dislocation=bool((math.isfinite(delta_gap) and abs(delta_gap)>=0.08) or
                     (math.isfinite(gamma_gap_pct) and abs(gamma_gap_pct)>=30.0))
    return {"iv":float(iv),"iv_source":iv_source,"quote_source":quote_source,
            "calc_delta":g["delta"],"calc_gamma":g["gamma"],"calc_vanna":g["vanna"],
            "calc_charm":g["charm"],"calc_speed":g["speed"],"vanna":g["vanna"],
            "charm":g["charm"],"speed":g["speed"],"provider_delta_gap":delta_gap,
            "provider_delta_diff":delta_gap,"provider_gamma_gap_pct":gamma_gap_pct,
            "provider_gamma_diff_pct":gamma_gap_pct,"greeks_dislocation":dislocation,
            "greeks_source":greek_source,"pricing_model":model_name,"quote_price_used":used_price,**mi}

def american_model_check(snapshot: pd.DataFrame, symbol: str, max_contracts: int = 8) -> Dict[str, Any]:
    """Validate near-ATM quotes against the instrument's pricing convention.

    Futures options are not run through the equity CRR diagnostic: their production
    structural model is Black-76 on the observed future. Mixing the two would reintroduce
    exactly the spot/future model error this release removes.
    """
    if snapshot is None or snapshot.empty:return {"ready":False,"reason":"Sin cadena"}
    if get_instrument(symbol).option_model==MODEL_FUTURE:
        return {"ready":True,"pricing_model":"BLACK_76","contracts":int(min(len(snapshot),max_contracts)),
                "note":"FUTURE_OPTION: Black-76 sobre F. CRR equity/American no se usa como validación cruzada."}
    x=snapshot.copy();spot=float(numeric_column(x,"underlying_price",float("nan")).dropna().iloc[-1])
    x["strike"]=numeric_column(x,"strike",float("nan"));x["dte"]=numeric_column(x,"dte",float("nan"));x["iv"]=numeric_column(x,"iv",float("nan"))
    x=x.dropna(subset=["strike","dte","iv"]);x=x[x["iv"]>0]
    if x.empty:return {"ready":False,"reason":"Sin IV utilizable"}
    nearest=float(x["dte"].min());x=x[(x["dte"]-nearest).abs()<0.25].copy();x["dist"]=(x["strike"]-spot).abs();x=x.sort_values("dist").head(max_contracts)
    rows=[]
    for r0 in x.itertuples(index=False):
        dte=float(r0.dte);mi=market_inputs(symbol,dte);T=year_fraction(dte);iv=float(r0.iv);typ=str(r0.option_type);K=float(r0.strike)
        euro=black_scholes_price(spot,K,T,iv,typ,mi["risk_free_rate"],mi["dividend_yield"])
        amer=american_option_price_crr(spot,K,T,iv,typ,mi["risk_free_rate"],mi["dividend_yield"])
        bid=getattr(r0,"bid",np.nan);ask=getattr(r0,"ask",np.nan)
        try:mid=(float(bid)+float(ask))/2 if float(bid)>0 and float(ask)>=float(bid) else float("nan")
        except Exception:mid=float("nan")
        rows.append({"strike":K,"option_type":typ,"european":euro,"american":amer,"american_premium":amer-euro if math.isfinite(amer) else float("nan"),"market_mid":mid})
    d=pd.DataFrame(rows)
    if d.empty:return {"ready":False,"reason":"Sin contratos comparables"}
    prem=pd.to_numeric(d["american_premium"],errors="coerce").abs()
    return {"ready":True,"contracts":int(len(d)),"max_american_premium":float(prem.max()) if prem.notna().any() else float("nan"),
            "avg_american_premium":float(prem.mean()) if prem.notna().any() else float("nan"),"rows":rows,
            "note":"CRR American es un diagnóstico de early-exercise/dividend sensitivity; Black-Scholes sigue siendo el motor intradía principal por velocidad."}


def exposure_scenarios(enriched: pd.DataFrame, flow_events: pd.DataFrame | None, symbol: str,
                       shifts_pct: Iterable[float]=(-1.0,-0.5,-0.25,0.0,0.25,0.5,1.0),
                       contract_multiplier: float=100.0) -> Dict[str, Any]:
    """Structural, flow-adjusted proxy and spot-sensitivity GEX scenarios."""
    if enriched is None or enriched.empty:return {"ready":False,"reason":"Sin exposición"}
    x=enriched.copy();x["timestamp"]=pd.to_datetime(x.get("timestamp"),errors="coerce");x=x.dropna(subset=["timestamp"])
    if x.empty:return {"ready":False,"reason":"Sin timestamp"}
    x=x[x["timestamp"]==x["timestamp"].max()].copy();spot=float(numeric_column(x,"underlying_price",float("nan")).dropna().iloc[-1])
    sign=np.where(x["option_type"].astype(str).str.lower().str.startswith("c"),1.0,-1.0)
    gamma=numeric_column(x,"calc_gamma",0);oi=numeric_column(x,"open_interest",0)
    structural=float(np.nansum(sign*gamma*oi*contract_multiplier*(spot**2)*0.01))
    # v1.14: vectorised. The previous version repriced every contract through a
    # scalar Black-Scholes call inside a Python loop, once per spot shift.
    K_arr=numeric_column(x,"strike",float("nan")).to_numpy(float)
    iv_arr=numeric_column(x,"iv",float("nan")).to_numpy(float)
    dte_arr=numeric_column(x,"dte",float("nan")).to_numpy(float)
    oi_arr=numeric_column(x,"open_interest",0).to_numpy(float)
    call_arr=x["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()
    sgn_arr=np.where(call_arr,1.0,-1.0)
    ok=np.isfinite(K_arr)&np.isfinite(iv_arr)&np.isfinite(dte_arr)&(iv_arr>0)
    K_arr,iv_arr,dte_arr,oi_arr,call_arr,sgn_arr=(v[ok] for v in (K_arr,iv_arr,dte_arr,oi_arr,call_arr,sgn_arr))
    r_arr,q_arr=model_inputs_vector(symbol,dte_arr)
    T_arr=year_fraction_array(dte_arr)
    sens=[]
    for shift in shifts_pct:
        S2=spot*(1.0+float(shift)/100.0)
        if len(K_arr):
            gam=black_scholes_greeks_vector(S2,K_arr,T_arr,iv_arr,call_arr,r_arr,q_arr)["gamma"]
            total=float(np.nansum(sgn_arr*gam*oi_arr*contract_multiplier*(S2**2)*0.01))
        else:
            total=0.0
        sens.append({"spot_shift_pct":float(shift),"spot":S2,"gex":float(total),"change_vs_now":float(total-structural)})
    flow_adj=0.0;flow_count=0
    if isinstance(flow_events,pd.DataFrame) and not flow_events.empty:
        ev=flow_events.copy();ev["timestamp"]=pd.to_datetime(ev.get("timestamp"),errors="coerce");ev=ev.dropna(subset=["timestamp"])
        if not ev.empty:
            cutoff=ev["timestamp"].max()-pd.Timedelta(minutes=15);ev=ev[ev["timestamp"]>=cutoff]
            for rr in ev.itertuples(index=False):
                try:
                    g=abs(float(getattr(rr,"provider_gamma",0) or 0));contracts=float(getattr(rr,"contracts",0) or 0);ag=str(getattr(rr,"aggressor","UNKNOWN")).upper();typ=str(getattr(rr,"option_type",""))
                    if ag not in {"BUY","SELL"} or g<=0 or contracts<=0:continue
                    base=1.0 if typ.lower().startswith("c") else -1.0;side=1.0 if ag=="BUY" else -1.0
                    flow_adj+=base*side*g*contracts*contract_multiplier*(spot**2)*0.01;flow_count+=1
                except Exception as _e:
                    _obs_note('precision_engine:440', _e)
    return {"ready":True,"structural_gex":structural,"flow_gamma_adjustment_proxy":float(flow_adj),"flow_adjusted_gex_proxy":float(structural+flow_adj),
            "flow_events_used":int(flow_count),"sensitivity":sens,
            "note":"Flow-adjusted GEX is a transparent customer-side activity proxy; it is not observed dealer inventory. Sensitivity reprices Gamma at shifted spot with IV/OI held constant."}


def _parse_ts(v: Any) -> Optional[pd.Timestamp]:
    if v is None or str(v).strip()=="":return None
    try:
        t=pd.Timestamp(v)
        if t.tzinfo is None:t=t.tz_localize(NY)
        return t.tz_convert("UTC")
    except Exception:return None


def build_greeks_provenance(snapshot: pd.DataFrame, meta: Dict[str, Any], *,
                            option_age_seconds: float | None = None,
                            stock_age_seconds: float | None = None,
                            oi_structural: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Describe where the visible Greeks came from and how observable the inputs are.

    This is an audit/quality payload, not a probability of correctness or trade success.
    It is intentionally additive so existing pricing math and Scanner weights do not
    change merely because provenance is now visible.
    """
    df = snapshot if isinstance(snapshot, pd.DataFrame) else pd.DataFrame()
    total = max(len(df), 1)
    def finite_pair(a: str, b: str) -> float:
        if a not in df.columns or b not in df.columns or df.empty:
            return 0.0
        x = pd.to_numeric(df[a], errors="coerce")
        y = pd.to_numeric(df[b], errors="coerce")
        return float((np.isfinite(x) & np.isfinite(y)).sum()) / total
    provider_cov = finite_pair("provider_delta", "provider_gamma")
    model_cov = finite_pair("fallback_delta", "fallback_gamma")
    if model_cov == 0.0:
        model_cov = finite_pair("calc_delta", "calc_gamma")
    disloc = 0.0
    if "greeks_dislocation" in df.columns and len(df):
        disloc = float(df["greeks_dislocation"].fillna(False).astype(bool).mean())
    oi_state = str((oi_structural or {}).get("estado") or "STRUCTURAL_UNKNOWN")
    support = 0.45 * max(provider_cov, model_cov) + 0.25 * model_cov + 0.15 * (1.0 - min(disloc, 1.0))
    support += 0.15 if oi_state in {"STRUCTURAL_VALID", "BYPASS_REPLAY"} else 0.0
    label = "HIGH" if support >= .80 else "MEDIUM" if support >= .55 else "LOW"
    return {
        "provider_greeks_coverage_pct": round(provider_cov * 100.0, 1),
        "model_greeks_coverage_pct": round(model_cov * 100.0, 1),
        "greeks_dislocation_pct": round(disloc * 100.0, 1),
        "option_age_seconds": None if option_age_seconds is None else round(float(option_age_seconds), 3),
        "underlying_age_seconds": None if stock_age_seconds is None else round(float(stock_age_seconds), 3),
        "oi_structural_state": oi_state,
        "input_support_score": round(float(np.clip(support, 0.0, 1.0)) * 100.0, 1),
        "input_support_label": label,
        "note": "Provenance de inputs/Greeks; mide observabilidad y soporte de entrada, no probabilidad de acierto.",
    }


def build_data_quality(snapshot: pd.DataFrame, meta: Dict[str, Any], result: Dict[str, Any], expiry_info: Dict[str, Any],
                       *, is_replay: bool | None = None) -> Dict[str, Any]:
    """Data-input quality only. This score is deliberately NOT a trading score."""
    df=snapshot.copy() if isinstance(snapshot,pd.DataFrame) else pd.DataFrame()
    _clock=_parse_ts((meta or {}).get("quality_clock"))
    now=_clock if _clock is not None else pd.Timestamp.now(tz="UTC")
    matched=max(int(meta.get("matched_snapshots",0) or 0),len(df),1);usable=int(len(df))
    definitions=max(int(meta.get("contract_definitions",matched) or matched),matched)
    iv_cov=float(np.clip(usable/max(matched,1),0,1));chain_cov=float(np.clip(matched/max(definitions,1),0,1))
    provider_iv=int(meta.get("provider_iv_count",0) or 0);fallback_iv=int(meta.get("fallback_iv_count",0) or 0);fallback_share=fallback_iv/max(usable,1)

    quote_cov=0.0;spread_quality=0.0;greeks_cov=0.0;provider_greeks_cov=0.0;disloc_pct=0.0
    if not df.empty:
        if {"bid","ask"}.issubset(df.columns):
            b=pd.to_numeric(df["bid"],errors="coerce");a=pd.to_numeric(df["ask"],errors="coerce");valid=b.gt(0)&a.ge(b)
            quote_cov=float(valid.mean()) if len(valid) else 0.0
            mid=(a+b)/2.0;spr=((a-b)/mid.replace(0,np.nan)).where(valid)
            med=float(spr.median()) if spr.notna().any() else 1.0
            spread_quality=float(np.clip(1.0-med/0.30,0,1))
        calc_d=pd.to_numeric(df.get("fallback_delta",df.get("calc_delta",pd.Series(index=df.index,dtype=float))),errors="coerce")
        calc_g=pd.to_numeric(df.get("fallback_gamma",df.get("calc_gamma",pd.Series(index=df.index,dtype=float))),errors="coerce")
        greeks_cov=float((np.isfinite(calc_d)&np.isfinite(calc_g)).mean()) if len(df) else 0.0
        pdlt=pd.to_numeric(df.get("provider_delta",pd.Series(index=df.index,dtype=float)),errors="coerce")
        pgam=pd.to_numeric(df.get("provider_gamma",pd.Series(index=df.index,dtype=float)),errors="coerce")
        provider_greeks_cov=float((np.isfinite(pdlt)&np.isfinite(pgam)).mean()) if len(df) else 0.0
        if "greeks_dislocation" in df.columns:
            disloc_pct=100.0*float(df["greeks_dislocation"].fillna(False).astype(bool).mean())

    def age_of(v):
        t=_parse_ts(v)
        return None if t is None else max(0.0,float((now-t).total_seconds()))
    option_age=age_of(meta.get("latest_option_market_timestamp"));stock_age=age_of(meta.get("stock_market_timestamp"))
    ages=[x for x in [option_age,stock_age] if x is not None];age_s=max(ages) if ages else None
    market_state=str(meta.get("market_state","" )).upper()
    if market_state in {"CLOSED","DEMO"}: freshness=1.0
    elif age_s is None:freshness=.45
    else:freshness=float(np.exp(-age_s/120.0))

    oi_age_days=None;oi_quality=1.0
    oi_dates=meta.get("oi_dates") or []
    try:
        parsed=[pd.Timestamp(x).date() for x in oi_dates if str(x)]
        if parsed:
            today=pd.Timestamp.now(tz=NY).date();oi_age_days=min(max((today-max(parsed)).days,0),30)
            # OI is normally end-of-day structural data; weekends/holidays make 2-3 calendar days normal.
            oi_quality=1.0 if oi_age_days<=3 else float(np.clip(1.0-(oi_age_days-3)/7.0,0,1))
    except Exception as _e:
        _obs_note('precision_engine:500', _e)

    # v1.27.9 · OI tiene reloj estructural/EOD, no reloj de quote. Se evalúa en
    # SHADOW contra la última sesión hábil esperada; todavía no abre el hard gate
    # hasta observar semántica real de timestamps Alpaca/tastytrade en el VPS.
    try:
        from .oi_freshness import assess_oi_structural_freshness
        oi_structural = assess_oi_structural_freshness(oi_dates, as_of=now, is_replay=bool(is_replay))
    except Exception as _e:
        _obs_note('precision_engine:oi_structural_freshness', _e, severity="DEGRADED")
        oi_structural = {"estado": "STRUCTURAL_UNKNOWN", "mode": "SHADOW_ONLY",
                         "would_block_gex_if_promoted": True,
                         "note": "evaluación OI no disponible; permanece UNKNOWN en SHADOW"}

    # v1.27.7 · Gate duro y STATEFUL de frescura. La evaluacion vive en un
    # cortacircuitos por simbolo con histeresis real. Cualquier fallo del propio
    # gate es fail-closed: nunca se publica una cifra si no podemos probar frescura.
    try:
        from .freshness import evaluate_publication_gate
        circuito=evaluate_publication_gate(meta,market_state=market_state,now=float(now.timestamp()),is_replay=is_replay)
    except Exception as _e:
        _obs_note('precision_engine:freshness_gate', _e, severity="CRITICAL_DATA")
        circuito={"estado":"UNKNOWN","publicar_permitido":False,"motivo":"gate de frescura no evaluable; publicacion bloqueada"}

    exp_count=int((expiry_info or {}).get("count",0) or 0);mode=str((expiry_info or {}).get("mode",""))
    expiry_quality=(1.0 if exp_count>=1 else 0.0) if mode=="0DTE" else min(1.0,exp_count/3.0)
    zero_meta=meta.get("zero_dte_coverage_pct")
    if zero_meta is None:
        zero_quality=1.0 if mode!="0DTE" else (1.0 if exp_count else 0.0);zero_pct=None
    else:
        zero_pct=float(np.clip(float(zero_meta),0,100));zero_quality=zero_pct/100.0

    cur=result.get("current_delta",result.get("current",pd.DataFrame())) if isinstance(result,dict) else pd.DataFrame();model_finite=1.0
    if isinstance(cur,pd.DataFrame) and not cur.empty:
        numeric=[c for c in ["signed_gex","delta_exposure","gamma_delta_level_score"] if c in cur.columns]
        if numeric:model_finite=float(np.isfinite(cur[numeric].to_numpy(float)).mean())

    weights={"iv_coverage":.17,"greeks_coverage":.12,"quote_coverage":.12,"spread_quality":.10,"freshness":.16,"chain_coverage":.09,"expiry_coverage":.07,"oi_age":.06,"model_finite":.06,"zero_dte":.05}
    vals={"iv_coverage":iv_cov,"greeks_coverage":greeks_cov,"quote_coverage":quote_cov,"spread_quality":spread_quality,"freshness":freshness,"chain_coverage":chain_cov,"expiry_coverage":expiry_quality,"oi_age":oi_quality,"model_finite":model_finite,"zero_dte":zero_quality}
    score=100.0*sum(weights[k]*vals[k] for k in weights);status="GOOD" if score>=85 else "USABLE" if score>=70 else "CAUTION" if score>=50 else "POOR"
    greeks_provenance = build_greeks_provenance(df, meta, option_age_seconds=option_age,
                                                stock_age_seconds=stock_age, oi_structural=oi_structural)
    return {"score":round(score,1),"status":status,"usable_contracts":usable,"matched_snapshots":matched,"contract_definitions":definitions,
            "iv_coverage_pct":round(iv_cov*100,1),"greeks_coverage_pct":round(greeks_cov*100,1),"provider_greeks_coverage_pct":round(provider_greeks_cov*100,1),
            "provider_iv_count":provider_iv,"fallback_iv_count":fallback_iv,"fallback_iv_share_pct":round(fallback_share*100,1),"zero_dte_usable_pct":None if zero_pct is None else round(zero_pct,1),
            "quote_coverage_pct":round(quote_cov*100,1),"spread_quality_pct":round(spread_quality*100,1),"chain_coverage_pct":round(chain_cov*100,1),
            "stock_age_seconds":None if stock_age is None else round(stock_age,1),"option_age_seconds":None if option_age is None else round(option_age,1),"data_age_seconds":None if age_s is None else round(age_s,1),
            "oi_age_days":oi_age_days,"oi_structural_freshness":oi_structural,"greeks_dislocation_pct":round(disloc_pct,1),"expiry_count":exp_count,
            "greeks_provenance":greeks_provenance,
            "components":{k:round(v*100,1) for k,v in vals.items()},"circuito_frescura":circuito,"note":"Data Quality mide cobertura, frescura y calidad de inputs. No es probabilidad de acierto."}


def build_model_health(history: pd.DataFrame, result: Dict[str, Any], scanner: Dict[str, Any], expiry_info: Dict[str, Any],
                       snapshot: pd.DataFrame | None = None, greeks_diag: Dict[str, Any] | None = None,
                       american_diag: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cur=result.get("current_delta",result.get("current",pd.DataFrame())) if isinstance(result,dict) else pd.DataFrame();checks=[]
    def add(name:str,ok:bool,detail:str,weight:float=1.0):checks.append({"name":name,"ok":bool(ok),"detail":detail,"weight":weight})
    if isinstance(cur,pd.DataFrame) and not cur.empty:
        lo,hi=float(cur["strike"].min()),float(cur["strike"].max());gc=float(result.get("gamma_center",np.nan));dc=float(result.get("delta_center",np.nan));spot=float(result.get("spot",np.nan))
        add("Gamma Center dentro del rango",math.isfinite(gc) and lo<=gc<=hi,f"{gc:.2f} dentro de {lo:.2f}-{hi:.2f}")
        add("Delta Center dentro del rango",math.isfinite(dc) and lo<=dc<=hi,f"{dc:.2f} dentro de {lo:.2f}-{hi:.2f}")
        edge=min(abs(spot-lo),abs(hi-spot))/max(hi-lo,1e-9) if math.isfinite(spot) else 0;add("Cadena no truncada junto al spot",edge>=.10,f"margen relativo {edge:.2f}",1.2)
    ts_count=0
    if isinstance(history,pd.DataFrame) and not history.empty and "timestamp" in history.columns:ts_count=int(pd.to_datetime(history["timestamp"],errors="coerce").nunique())
    add("Memoria estructural suficiente",ts_count>=4,f"{ts_count} snapshots disponibles",1.0)
    add("Dynamic Flip explícito",bool(result.get("gamma_flip_crossing",False)) or str(result.get("gamma_flip_method","" )).startswith("NEAREST"),str(result.get("gamma_flip_method","UNKNOWN")),.8)
    evidence=float(scanner.get("evidence_score",0) or 0);add("Scanner calculable",bool(scanner.get("ready")),f"Evidence interno {evidence:.1f}/100",1.0)
    add("Expiry Window con datos",int((expiry_info or {}).get("count",0) or 0)>0,f"{(expiry_info or {}).get('count',0)} vencimientos",1.0)
    if isinstance(snapshot,pd.DataFrame) and not snapshot.empty and "greeks_dislocation" in snapshot.columns:
        rate=100.0*float(snapshot["greeks_dislocation"].fillna(False).astype(bool).mean());add("Greeks proveedor vs modelo",rate<=20.0,f"{rate:.1f}% contratos con dislocación diagnóstica",.9)
    if greeks_diag:
        gap=greeks_diag.get("median_abs_provider_gamma_gap_pct")
        try:gap=float(gap);ok=(not math.isfinite(gap)) or gap<=30
        except Exception:ok=True;gap=float("nan")
        add("Gamma provider/model estable",ok,"sin muestra comparable" if not math.isfinite(gap) else f"mediana gap {gap:.1f}%",.7)
    if american_diag and american_diag.get("ready"):
        ap=float(american_diag.get("max_american_premium",0) or 0)
        add("Sensibilidad ejercicio americano monitorizada",True,f"CRR vs europeo · premium máx absoluto {ap:.4f}",.35)
    denom=sum(c["weight"] for c in checks);score=100.0*sum(c["weight"] for c in checks if c["ok"])/max(denom,1e-9)
    return {"score":round(score,1),"status":"HEALTHY" if score>=85 else "WATCH" if score>=65 else "DEGRADED","checks":checks,
            "note":"Model Health mide estabilidad/coherencia del motor, no efectividad de trading."}

def what_changed(current: Dict[str, Any], previous: Optional[Dict[str, Any]], scanner: Dict[str, Any], previous_scanner: Optional[Dict[str, Any]]) -> list[Dict[str, Any]]:
    if not previous:return [{"label":"BASELINE","detail":"Primer snapshot de la comparación; aún no hay cambio anterior.","severity":"INFO"}]
    rows=[]
    for label,key in [("Gamma Center","gamma_center"),("Delta Center","delta_center"),("Net GEX","total_signed_gex"),("Delta Exposure","net_delta_exposure")]:
        a=previous.get(key);b=current.get(key)
        try:
            a=float(a);b=float(b)
            if math.isfinite(a) and math.isfinite(b):
                d=b-a;rows.append({"label":label,"from":a,"to":b,"change":d,"direction":"UP" if d>0 else "DOWN" if d<0 else "FLAT","severity":"INFO"})
        except Exception as _e:
            _obs_note('precision_engine:566', _e)
    if previous_scanner:
        old_dir=str(previous_scanner.get("direction",""));new_dir=str(scanner.get("direction",""));old_ev=float(previous_scanner.get("evidence_score",0) or 0);new_ev=float(scanner.get("evidence_score",0) or 0)
        rows.append({"label":"Scanner","from":old_dir,"to":new_dir,"change":round(new_ev-old_ev,1),"direction":"CHANGED" if old_dir!=new_dir else "SAME","detail":f"Evidence {old_ev:.1f} -> {new_ev:.1f}","severity":"WARN" if old_dir and new_dir and old_dir!=new_dir else "INFO"})
    return rows[:8]


def exposure_attribution(history: pd.DataFrame, symbol: str, contract_multiplier: float = 100.0) -> Dict[str, Any]:
    """Explain last structural-GEX change with a transparent sequential decomposition."""
    if not isinstance(history,pd.DataFrame) or history.empty or "timestamp" not in history.columns:return {"ready":False,"reason":"Sin historial"}
    h=history.copy();h["timestamp"]=pd.to_datetime(h["timestamp"],errors="coerce");h=h.dropna(subset=["timestamp"]);times=sorted(h["timestamp"].unique())
    if len(times)<2:return {"ready":False,"reason":"Se requieren al menos dos snapshots"}
    p=h[h["timestamp"]==times[-2]].copy();c=h[h["timestamp"]==times[-1]].copy();keys=[k for k in ["contract_symbol","expiration_date","strike","option_type"] if k in p.columns and k in c.columns] or ["strike","option_type"]
    cols=keys+[z for z in ["underlying_price","iv","dte","open_interest"] if z in p.columns]
    pp=p[cols].copy().rename(columns={z:f"p_{z}" for z in ["underlying_price","iv","dte","open_interest"] if z in cols});cc=c[cols].copy().rename(columns={z:f"c_{z}" for z in ["underlying_price","iv","dte","open_interest"] if z in cols});m=pp.merge(cc,on=keys,how="inner")
    if m.empty:return {"ready":False,"reason":"No hay contratos comparables"}
    # v1.14: vectorised. Was 5 full passes x N contracts of scalar Black-Scholes.
    _K=pd.to_numeric(m["strike"],errors="coerce").to_numpy(float)
    _call=m["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()
    _sgn=np.where(_call,1.0,-1.0)
    def _col(name):return pd.to_numeric(m[name],errors="coerce").to_numpy(float)
    _ps,_cs=_col("p_underlying_price"),_col("c_underlying_price")
    _piv,_civ=_col("p_iv"),_col("c_iv")
    _pdt,_cdt=_col("p_dte"),_col("c_dte")
    _poi,_coi=_col("p_open_interest"),_col("c_open_interest")
    def total(stage:str)->float:
        S=_cs if stage in {"spot","iv","time","oi"} else _ps
        iv=_civ if stage in {"iv","time","oi"} else _piv
        dte=_cdt if stage in {"time","oi"} else _pdt
        oi=_coi if stage=="oi" else _poi
        if not len(_K):return 0.0
        r_a,q_a=model_inputs_vector(symbol,dte)
        with np.errstate(all="ignore"):
            g=black_scholes_greeks_vector(S,_K,year_fraction_array(dte),iv,_call,r_a,q_a)["gamma"]
            vals=_sgn*g*oi*contract_multiplier*(S**2)*.01
        return float(np.nansum(vals))
    base=total("base");s1=total("spot");s2=total("iv");s3=total("time");s4=total("oi");effects={"spot_repricing":s1-base,"iv_change":s2-s1,"time_decay":s3-s2,"oi_structure":s4-s3,"total_change":s4-base};denom=sum(abs(v) for k,v in effects.items() if k!="total_change") or 1.0;shares={k:100.0*abs(v)/denom for k,v in effects.items() if k!="total_change"};dominant=max(shares,key=shares.get) if shares else "none"
    return {"ready":True,"from":pd.Timestamp(times[-2]).isoformat(),"to":pd.Timestamp(times[-1]).isoformat(),"effects":effects,"shares_pct":shares,"dominant_driver":dominant,
            "note":"Atribución secuencial diagnóstica; no demuestra entrada/salida real de liquidez ni hedge-flow observado."}
