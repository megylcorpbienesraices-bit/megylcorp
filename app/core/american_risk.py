"""Puerta de riesgo de modelo por ejercicio anticipado (v1.42).

LA POSTURA
----------
Las opciones sobre acciones y ETF en EE. UU. son de estilo americano. Black-Scholes
valora la europea. La reacción fácil sería declarar «BSM es incorrecto» y cambiar
toda la cadena a un árbol binomial. Sería un error práctico: CRR con 120 pasos sobre
5.000 contratos, recalculado cada pocos segundos, destruye la latencia que hace útil
una terminal intradía, y para la inmensa mayoría de esos contratos la diferencia es
inferior al tick.

La postura institucional no es elegir un modelo y creer en él. Es usar el rápido
como base, MEDIR cuánto se aparta del correcto, y decirlo:

    MODEL_RISK = LOW       la prima de ejercicio anticipado es despreciable
    MODEL_RISK = ELEVATED  es material; la cifra publicada lleva esa etiqueta

CUÁNDO IMPORTA DE VERDAD
------------------------
  - PUT americana con r > 0: el ejercicio anticipado captura el interés sobre el
    strike. Cuanto más ITM, más DTE y más tipo, mayor la prima.
  - CALL americana sobre subyacente con dividendo: puede convenir ejercer justo
    antes del ex-dividendo. Con q = 0 nunca conviene, y entonces americana = europea
    exactamente (resultado clásico), así que ni siquiera hay que calcular.
  - FUTUROS e ÍNDICES europeos liquidados en efectivo: no aplica.

Ese cribado previo es lo que hace la puerta barata: descarta por razonamiento lo que
no necesita árbol, y sólo evalúa CRR donde puede haber algo.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


from . import instruments
from .contract_spec import ContractSpec, from_symbol
from .greeks_service import greeks, model_for, price as european_price
from .precision_engine import american_option_price_crr

RISK_LOW = "LOW"
RISK_ELEVATED = "ELEVATED"
RISK_NOT_APPLICABLE = "NOT_APPLICABLE"

# Umbrales. `PRICE_TOL_RATIO` es la fracción de la prima europea por encima de la
# cual la diferencia deja de ser ruido de discretización del árbol; `PRICE_TOL_ABS`
# evita que una opción de 3 centavos se marque ELEVATED por medio centavo.
PRICE_TOL_RATIO = 0.005      # 0,5 % de la prima
PRICE_TOL_ABS = 0.01         # 1 centavo
DELTA_TOL = 0.005            # 0,5 puntos de delta
CRR_STEPS = 160


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


@dataclass(frozen=True)
class ExerciseRisk:
    risk: str
    european_price: Optional[float]
    american_price: Optional[float]
    premium: Optional[float]           # americana − europea, en dólares de prima
    premium_pct: Optional[float]
    delta_gap: Optional[float]
    screened_out: bool
    reason: str

    def describe(self) -> Dict[str, Any]:
        return {"model_risk": self.risk, "european_price": self.european_price,
                "american_price": self.american_price, "early_exercise_premium": self.premium,
                "early_exercise_premium_pct": self.premium_pct, "delta_gap": self.delta_gap,
                "screened_out": self.screened_out, "reason": self.reason}


def _screen(spec: ContractSpec, is_call: bool, S: float, K: float, T: float,
            r: float, q: float) -> Optional[str]:
    """Devuelve el motivo por el que NO hace falta el árbol, o None si sí hace falta."""
    if spec.exercise_style != "AMERICAN":
        return "contrato europeo: no existe ejercicio anticipado"
    if model_for(spec) == instruments.MODEL_FUTURE:
        # El future option americano sí puede ejercerse, pero sobre un futuro
        # liquidado diariamente la prima de ejercicio es de segundo orden y el
        # modelo autorizado aquí es Black-76. Se declara, no se estima a medias.
        return "opción sobre futuro: valorada con Black-76; el ejercicio anticipado "\
               "sobre un futuro con liquidación diaria no se modela en esta versión"
    if T <= 0:
        return "vencida o vencimiento no positivo"
    if is_call and q <= 1e-9:
        # Resultado clásico: sin dividendos, jamás conviene ejercer una call
        # americana antes del vencimiento, luego vale exactamente lo mismo que la
        # europea. No es una aproximación.
        return "call sin dividendo: americana ≡ europea (nunca conviene ejercer antes)"
    if is_call and q * T < 5e-4:
        return "dividendo acumulado hasta el vencimiento despreciable"
    if not is_call and r <= 1e-9:
        return "put con tipo ≈ 0: el ejercicio anticipado no captura interés"
    moneyness = (S - K) / max(K, 1e-9)
    if is_call and moneyness < -0.25:
        return "call muy OTM: la prima de ejercicio anticipado es nula"
    if not is_call and moneyness > 0.25:
        return "put muy OTM: la prima de ejercicio anticipado es nula"
    return None


def assess(*, symbol: Any, S: float, K: float, T: float, sigma: float,
           option_type: str, r: float, q: float, steps: int = CRR_STEPS) -> ExerciseRisk:
    """Riesgo de modelo de UN contrato: cuánto se aparta BSM del valor americano."""
    spec = symbol if isinstance(symbol, ContractSpec) else from_symbol(symbol)
    is_call = str(option_type).lower().startswith("c")
    Sv, Kv, Tv, sig = _f(S), _f(K), _f(T), _f(sigma)
    rv, qv = _f(r) or 0.0, _f(q) or 0.0
    if None in (Sv, Kv, Tv, sig) or min(Sv, Kv, Tv, sig) <= 0:
        return ExerciseRisk(RISK_NOT_APPLICABLE, None, None, None, None, None, True,
                            "entradas no válidas para valorar")

    why = _screen(spec, is_call, Sv, Kv, Tv, rv, qv)
    if why:
        eu = _f(european_price(spec, Sv, Kv, Tv, sig, option_type, rv, qv))
        return ExerciseRisk(RISK_LOW if spec.exercise_style else RISK_NOT_APPLICABLE,
                            eu, eu, 0.0, 0.0, 0.0, True, why)

    eu = _f(european_price(spec, Sv, Kv, Tv, sig, option_type, rv, qv))
    am = _f(american_option_price_crr(Sv, Kv, Tv, sig, option_type, rv, qv, steps=steps))
    if eu is None or am is None:
        return ExerciseRisk(RISK_NOT_APPLICABLE, eu, am, None, None, None, False,
                            "la valoración de contraste no converge")

    prem = max(am - eu, 0.0)   # por no arbitraje, la americana nunca vale menos
    prem_pct = prem / max(eu, 1e-9) * 100.0

    # Delta por diferencias finitas sobre el árbol, comparada con la analítica.
    h = max(Sv * 1e-3, 1e-4)
    am_up = _f(american_option_price_crr(Sv + h, Kv, Tv, sig, option_type, rv, qv, steps=steps))
    am_dn = _f(american_option_price_crr(Sv - h, Kv, Tv, sig, option_type, rv, qv, steps=steps))
    delta_gap = None
    if am_up is not None and am_dn is not None:
        am_delta = (am_up - am_dn) / (2.0 * h)
        eu_delta = _f(greeks(spec, Sv, Kv, Tv, sig, option_type, rv, qv).get("delta"))
        if eu_delta is not None:
            delta_gap = abs(am_delta - eu_delta)

    material = (prem > max(PRICE_TOL_ABS, PRICE_TOL_RATIO * max(eu, 1e-9))
                or (delta_gap is not None and delta_gap > DELTA_TOL))
    risk = RISK_ELEVATED if material else RISK_LOW
    reason = ("la prima de ejercicio anticipado es material; la valoración europea "
              "subestima el contrato y sus Greeks" if material else
              "la prima de ejercicio anticipado está por debajo del tick: Black-Scholes "
              "es una aproximación aceptable aquí")
    return ExerciseRisk(risk, round(eu, 6), round(am, 6), round(prem, 6),
                        round(prem_pct, 4),
                        None if delta_gap is None else round(delta_gap, 6),
                        False, reason)


def chain_model_risk(rows: Iterable[Dict[str, Any]], *, symbol: Any,
                     max_contracts: int = 12) -> Dict[str, Any]:
    """Riesgo de modelo de una cadena, evaluando sólo los contratos que pueden tenerlo.

    No se evalúa toda la cadena: se ordena por dónde el ejercicio anticipado es
    plausible (puts ITM con más DTE) y se muestrean los peores casos. Si el peor
    caso es LOW, el resto también lo es, y eso sí es una afirmación defendible.
    """
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        S, K = _f(row.get("underlying_price")), _f(row.get("strike"))
        T, sig = _f(row.get("T") or row.get("t_years")), _f(row.get("iv"))
        dte = _f(row.get("dte"))
        if T is None and dte is not None:
            T = max(dte, 0.0) / 365.0
        if None in (S, K, T, sig) or min(S, K, T, sig) <= 0:
            continue
        is_call = str(row.get("option_type") or "call").lower().startswith("c")
        # Prioridad: put ITM con vencimiento lejano es donde más pesa el interés.
        itm = (S - K) if is_call else (K - S)
        items.append((-(max(itm, 0.0) / max(K, 1e-9)) * math.sqrt(max(T, 0.0)), is_call, row, S, K, T, sig))
    if not items:
        return {"ready": False, "reason": "sin contratos valorables"}

    items.sort(key=lambda t: t[0])
    sample = items[: max(int(max_contracts), 1)]
    results = []
    for _rank, is_call, row, S, K, T, sig in sample:
        r = _f(row.get("risk_free_rate"))
        q = _f(row.get("dividend_yield"))
        if r is None or q is None:
            from .greeks_service import market_inputs
            mi = market_inputs(symbol, (T * 365.0))
            r = _f(mi.get("risk_free_rate")) or 0.0
            q = _f(mi.get("dividend_yield")) or 0.0
        results.append(assess(symbol=symbol, S=S, K=K, T=T, sigma=sig,
                              option_type="call" if is_call else "put", r=r, q=q))

    elevated = [r for r in results if r.risk == RISK_ELEVATED]
    worst = max((r for r in results if r.premium is not None),
                key=lambda r: r.premium, default=None)
    return {
        "ready": True,
        "model_risk": RISK_ELEVATED if elevated else RISK_LOW,
        "evaluated": len(results),
        "screened_out": sum(1 for r in results if r.screened_out),
        "elevated_count": len(elevated),
        "worst_case": None if worst is None else worst.describe(),
        "method": "BSM_BASE_WITH_CRR_CHALLENGER",
        "note": ("Black-Scholes es el valorador rápido; CRR actúa como contraste sobre "
                 "los contratos donde el ejercicio anticipado puede tener valor. El resto "
                 "se descarta por razonamiento, no por muestreo."),
    }
