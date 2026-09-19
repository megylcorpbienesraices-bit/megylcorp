"""GreeksService — la única puerta autorizada a precio, Greeks e IV (v1.42).

EL DEFECTO QUE CIERRA
---------------------
En v1.41 existía `precision_engine.greeks_vector_for_symbol`, que despacha
correctamente Black-Scholes o Black-76 según el instrumento. Pero sólo `engine.py`
lo usaba. `trace_analytics`, `trace_live`, `market_state_field`, `nextgen_terminal`
y `dealer_intelligence` importaban `black_scholes_greeks_vector` directamente.

Para un ETF da igual: el despacho elige Black-Scholes de todos modos. Para un
future option NO da igual. Black-76 descuenta el forward y no aplica dividendo;
Black-Scholes sobre el precio del futuro tratado como spot produce delta y gamma
distintas. Es decir: la Gamma de TRACE y la Gamma del Scanner eran, para YM/MYM,
dos números diferentes presentados como el mismo concepto.

Nadie lo vio porque el activo por defecto es un ETF. Esa es precisamente la clase
de error que sólo aparece cuando el programa deja de ser mono-activo.

LA REGLA
--------
Ninguna sección importa `black_scholes_*` ni `black_76_*`. Todas entran por aquí.
Hay un test de regresión (`test_v1420_*`) que falla si vuelve a aparecer un import
directo fuera de `precision_engine` (la implementación) y de este módulo (la puerta).

QUÉ AÑADE SOBRE EL DESPACHO CRUDO
---------------------------------
  - `vega`, que el vector no devolvía y que la puerta de identificabilidad de IV
    necesita para saber si una IV resuelta significa algo.
  - contabilidad de llamadas, para que el Auditor pueda demostrar —no suponer— que
    todas las secciones pasaron por el mismo modelo en el mismo ciclo.
  - `ContractSpec` como entrada aceptada, para que el multiplicador y el modelo
    viajen juntos en vez de por separado.
"""

from __future__ import annotations

import threading
from typing import Any, Dict

import numpy as np
from scipy.stats import norm

from . import instruments, precision_engine
from .contract_spec import ContractSpec, from_symbol
from .quant_errors import PricingFailure

MODEL_EQUITY = instruments.MODEL_EQUITY
MODEL_INDEX = instruments.MODEL_INDEX
MODEL_FUTURE = instruments.MODEL_FUTURE
MODEL_UNSUPPORTED = instruments.MODEL_UNSUPPORTED

_LOCK = threading.Lock()
_USAGE: Dict[str, int] = {}


def _count(model: str, kind: str) -> None:
    key = f"{model}:{kind}"
    with _LOCK:
        _USAGE[key] = _USAGE.get(key, 0) + 1


def usage_stats() -> Dict[str, int]:
    """Cuántas veces se usó cada modelo. El Auditor lo publica como evidencia."""
    with _LOCK:
        return dict(_USAGE)


def reset_usage() -> None:
    with _LOCK:
        _USAGE.clear()


def _spec(target: Any) -> ContractSpec:
    if isinstance(target, ContractSpec):
        return target
    return from_symbol(target)


def _require_supported(spec: ContractSpec) -> str:
    model = spec.option_model
    if model == MODEL_UNSUPPORTED or not spec.supported:
        raise PricingFailure(
            f"{spec.underlying}: sin modelo de valoración autorizado. {spec.notes}".strip(),
            underlying=spec.underlying, model=model)
    return model


# ── vega (no estaba en el vector de v1.41) ──────────────────────────────────────

def _bs_vega_vector(S, K, T, sigma, r, q):
    """dPrecio/dSigma por 1.0 de vol (no por punto). Dividir entre 100 para punto."""
    S = np.maximum(np.asarray(S, dtype=float), 1e-12)
    K = np.maximum(np.asarray(K, dtype=float), 1e-12)
    T = np.maximum(np.asarray(T, dtype=float), 1e-10)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-8)
    r = np.asarray(r, dtype=float); q = np.asarray(q, dtype=float)
    root = np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * root)
    return S * np.exp(-q * T) * norm.pdf(d1) * root


def _b76_vega_vector(F, K, T, sigma, r):
    F = np.maximum(np.asarray(F, dtype=float), 1e-12)
    K = np.maximum(np.asarray(K, dtype=float), 1e-12)
    T = np.maximum(np.asarray(T, dtype=float), 1e-10)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-8)
    r = np.asarray(r, dtype=float)
    root = np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * root)
    return np.exp(-r * T) * F * norm.pdf(d1) * root


def _bs_vega_scalar(S, K, T, sigma, r, q) -> float:
    return float(_bs_vega_vector(np.asarray([S]), np.asarray([K]), np.asarray([T]),
                                 np.asarray([sigma]), np.asarray([r]), np.asarray([q]))[0])


# ── API pública ─────────────────────────────────────────────────────────────────

def greeks_vector(target: Any, S, K, T, sigma, is_call, r, q, *,
                  with_vega: bool = True) -> Dict[str, np.ndarray]:
    """Greeks de una cadena completa, con el modelo que exige el instrumento.

    `S` es el precio del subyacente para equity/ETF/índice y el precio del FUTURO
    para un future option: son la misma columna del snapshot, pero entran a
    ecuaciones distintas, y esa distinción es exactamente lo que se perdía cuando
    cada sección llamaba a Black-Scholes por su cuenta.
    """
    spec = _spec(target)
    model = _require_supported(spec)
    if model == MODEL_FUTURE:
        out = dict(precision_engine.black_76_greeks_vector(S, K, T, sigma, is_call, r))
        if with_vega:
            out["vega"] = _b76_vega_vector(S, K, T, sigma, r)
    else:
        out = dict(precision_engine.black_scholes_greeks_vector(S, K, T, sigma, is_call, r, q))
        if with_vega:
            out["vega"] = _bs_vega_vector(S, K, T, sigma, r, q)
    _count(model, "vector")
    return out


def greeks(target: Any, S: float, K: float, T: float, sigma: float, option_type: str,
           r: float, q: float, *, with_vega: bool = True) -> Dict[str, float]:
    """Greeks de UN contrato."""
    spec = _spec(target)
    model = _require_supported(spec)
    if model == MODEL_FUTURE:
        out = dict(precision_engine.black_76_greeks_full(S, K, T, sigma, option_type, r))
        if with_vega:
            out["vega"] = float(_b76_vega_vector(np.asarray([S]), np.asarray([K]),
                                                 np.asarray([T]), np.asarray([sigma]),
                                                 np.asarray([r]))[0])
    else:
        out = dict(precision_engine.black_scholes_greeks_full(S, K, T, sigma, option_type, r, q))
        if with_vega:
            out["vega"] = _bs_vega_scalar(S, K, T, sigma, r, q)
    _count(model, "scalar")
    return out


def price(target: Any, S: float, K: float, T: float, sigma: float, option_type: str,
          r: float, q: float) -> float:
    spec = _spec(target)
    model = _require_supported(spec)
    _count(model, "price")
    if model == MODEL_FUTURE:
        return precision_engine.black_76_price(S, K, T, sigma, option_type, r)
    return precision_engine.black_scholes_price(S, K, T, sigma, option_type, r, q)


def implied_vol(target: Any, observed_price: float, S: float, K: float, T: float,
                option_type: str, r: float, q: float) -> float:
    spec = _spec(target)
    model = _require_supported(spec)
    _count(model, "iv")
    if model == MODEL_FUTURE:
        return precision_engine.implied_volatility_black_76(observed_price, S, K, T, option_type, r)
    return precision_engine.implied_volatility_from_price(observed_price, S, K, T, option_type, r, q)


def model_inputs_vector(symbol: Any, dte_array):
    """Re-export deliberado: las secciones no deben importar `precision_engine`."""
    sym = symbol.underlying if isinstance(symbol, ContractSpec) else symbol
    return precision_engine.model_inputs_vector(sym, dte_array)


def market_inputs(symbol: Any, dte: float | None = None) -> Dict[str, Any]:
    sym = symbol.underlying if isinstance(symbol, ContractSpec) else symbol
    return precision_engine.market_inputs(sym, dte)


def model_for(target: Any) -> str:
    return _spec(target).option_model


def describe_dispatch(target: Any) -> Dict[str, Any]:
    """Qué modelo se aplicará y por qué. Esto es lo que TRACE publica como metadata."""
    spec = _spec(target)
    model = spec.option_model
    why = {
        MODEL_EQUITY: "Opción sobre acción/ETF: Black-Scholes con dividendo continuo sobre el spot.",
        MODEL_INDEX: "Opción de índice europea liquidada en efectivo: Black-Scholes sobre el nivel del índice.",
        MODEL_FUTURE: "Opción sobre futuro: Black-76 sobre F. El futuro ya incorpora acarreo; "
                      "aplicarle dividendo o tratarlo como spot sesgaría delta y gamma.",
        MODEL_UNSUPPORTED: spec.notes or "Sin modelo autorizado para este instrumento.",
    }.get(model, "modelo desconocido")
    return {"underlying": spec.underlying, "asset_class": spec.asset_class,
            "option_model": model, "exercise_style": spec.exercise_style,
            "settlement": spec.settlement, "multiplier": spec.multiplier,
            "supported": spec.supported, "rationale": why}
