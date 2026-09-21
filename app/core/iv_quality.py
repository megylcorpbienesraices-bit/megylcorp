"""Puertas de identificabilidad de la volatilidad implícita (v1.42).

EL PROBLEMA
-----------
Brent converge casi siempre. Eso no significa que el resultado signifique algo.

Una call con 3 DTE, strike 40 % por debajo del spot y prima igual a su valor
intrínseco más un centavo tiene vega ≈ 0. La inversión devolverá *un* número —
y ese número es basura numérica: mover la prima un tick cambia la IV varios puntos.
Publicarla con tres decimales es conceder una precisión que no existe, y peor, esa
IV entra luego en la superficie, en los Greeks y en el GEX.

Lo mismo ocurre con:
  - una prima fuera de los límites de no arbitraje (el quote está roto o cruzado);
  - un contrato sin valor extrínseco (todo es intrínseco: no hay opcionalidad que medir);
  - un spread tan ancho que bid y ask implican IV distintas en varios puntos.

LA SALIDA
---------
En vez de un float, un estado:

    OBSERVED_PROVIDER      el proveedor la publicó y pasa las comprobaciones
    SOLVED_HIGH_CONFIDENCE resuelta, con vega suficiente y spread sano
    SOLVED_LOW_CONFIDENCE  resuelta, pero el intervalo bid/ask es ancho
    UNIDENTIFIABLE         matemáticamente indeterminable (vega ~ 0, sin extrínseco)
    INVALID                el precio viola no-arbitraje; el dato está mal

Quien consume una IV puede entonces decidir. La superficie puede excluir
UNIDENTIFIABLE en vez de ajustar contra ruido, y el Auditor puede contar cuánta
cadena es realmente identificable, que es una métrica de calidad de datos honesta.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .greeks_service import greeks, implied_vol

OBSERVED_PROVIDER = "OBSERVED_PROVIDER"
SOLVED_HIGH_CONFIDENCE = "SOLVED_HIGH_CONFIDENCE"
SOLVED_LOW_CONFIDENCE = "SOLVED_LOW_CONFIDENCE"
UNIDENTIFIABLE = "UNIDENTIFIABLE"
INVALID = "INVALID"

# Vega mínima (por 1.0 de vol) para que un punto de IV sea distinguible del ruido
# de un tick. Con vega = 0.01 por vol completa, un centavo de prima mueve la IV
# 100 puntos: cualquier cifra publicada ahí es ficción.
MIN_VEGA_PER_VOL = 0.05
# Valor extrínseco mínimo, en dólares de prima. Por debajo, la opción es un forward.
MIN_EXTRINSIC = 0.01
# Anchura relativa de spread a partir de la cual la confianza baja.
WIDE_SPREAD_RATIO = 0.35
# Diferencia de IV entre bid y ask (en puntos) a partir de la cual baja la confianza.
WIDE_IV_BAND_PTS = 4.0


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


@dataclass(frozen=True)
class IVAssessment:
    state: str
    iv: Optional[float]                 # en fracción (0.18 = 18 %)
    confidence: float                   # 0..1
    vega_per_vol: Optional[float]
    extrinsic: Optional[float]
    iv_bid: Optional[float] = None
    iv_ask: Optional[float] = None
    iv_band_points: Optional[float] = None
    spread_ratio: Optional[float] = None
    reason: str = ""

    @property
    def usable_for_surface(self) -> bool:
        """Sólo lo identificable entra al ajuste de la superficie."""
        return self.state in (OBSERVED_PROVIDER, SOLVED_HIGH_CONFIDENCE, SOLVED_LOW_CONFIDENCE)

    def describe(self) -> Dict[str, Any]:
        return {"state": self.state, "iv": self.iv, "iv_pct": None if self.iv is None else round(self.iv * 100.0, 4),
                "confidence": round(self.confidence, 3), "vega_per_vol": self.vega_per_vol,
                "extrinsic": self.extrinsic, "iv_bid": self.iv_bid, "iv_ask": self.iv_ask,
                "iv_band_points": self.iv_band_points, "spread_ratio": self.spread_ratio,
                "usable_for_surface": self.usable_for_surface, "reason": self.reason}


def _no_arbitrage_bounds(S: float, K: float, T: float, r: float, q: float,
                         is_call: bool, is_future: bool) -> tuple[float, float]:
    """Cotas de no arbitraje europeas para el precio de la opción."""
    if is_future:
        disc = math.exp(-r * T)
        lo = max(0.0, disc * (S - K)) if is_call else max(0.0, disc * (K - S))
        hi = disc * S if is_call else disc * K
        return lo, hi
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    if is_call:
        return max(0.0, S * dq - K * dr), S * dq
    return max(0.0, K * dr - S * dq), K * dr


def assess(*, symbol: Any, S: float, K: float, T: float, option_type: str,
           r: float, q: float, mid: Optional[float] = None,
           bid: Optional[float] = None, ask: Optional[float] = None,
           provider_iv: Optional[float] = None) -> IVAssessment:
    """Evalúa si la IV de este contrato significa algo y con cuánta confianza."""
    from .greeks_service import model_for
    from . import instruments

    Sv, Kv, Tv = _f(S), _f(K), _f(T)
    if Sv is None or Kv is None or Tv is None or Sv <= 0 or Kv <= 0 or Tv <= 0:
        return IVAssessment(INVALID, None, 0.0, None, None,
                            reason="entradas no finitas o vencimiento no positivo")

    is_call = str(option_type).lower().startswith("c")
    is_future = model_for(symbol) == instruments.MODEL_FUTURE
    rv, qv = _f(r) or 0.0, _f(q) or 0.0

    b, a = _f(bid), _f(ask)
    # El cruce se comprueba ANTES de derivar el mid: un libro cruzado o bloqueado
    # no es una prima ruidosa, es un dato inválido, y su media sería un precio que
    # nadie cotizó.
    if b is not None and a is not None and a < b:
        return IVAssessment(INVALID, None, 0.0, None, None,
                            reason="mercado cruzado: ask por debajo de bid")
    m = _f(mid)
    if m is None and b is not None and a is not None and a >= b >= 0:
        m = 0.5 * (a + b)
    if m is None or m <= 0:
        return IVAssessment(INVALID, None, 0.0, None, None,
                            reason="sin prima utilizable (ni mid ni bid/ask válidos)")

    lo, hi = _no_arbitrage_bounds(Sv, Kv, Tv, rv, qv, is_call, is_future)
    eps = max(1e-6, Sv * 1e-9)
    if m < lo - eps or m > hi + eps:
        return IVAssessment(INVALID, None, 0.0, None, None,
                            reason=f"prima {m:.4f} fuera de las cotas de no arbitraje "
                                   f"[{lo:.4f}, {hi:.4f}]")

    extrinsic = m - lo
    if extrinsic < MIN_EXTRINSIC:
        return IVAssessment(UNIDENTIFIABLE, None, 0.0, None, round(extrinsic, 6),
                            reason=f"valor extrínseco {extrinsic:.4f} < {MIN_EXTRINSIC}: "
                                   "la prima es intrínseco puro, no hay opcionalidad que medir")

    iv = _f(implied_vol(symbol, m, Sv, Kv, Tv, option_type, rv, qv))
    if iv is None or iv <= 0:
        return IVAssessment(UNIDENTIFIABLE, None, 0.0, None, round(extrinsic, 6),
                            reason="la inversión no converge dentro de los límites admitidos")

    g = greeks(symbol, Sv, Kv, Tv, iv, option_type, rv, qv)
    vega = _f(g.get("vega"))
    if vega is None or vega < MIN_VEGA_PER_VOL:
        return IVAssessment(UNIDENTIFIABLE, None, 0.0,
                            None if vega is None else round(vega, 6), round(extrinsic, 6),
                            reason=f"vega {vega if vega is None else round(vega, 4)} por debajo de "
                                   f"{MIN_VEGA_PER_VOL}: la IV no es identificable, un tick de prima "
                                   "la movería varios puntos")

    iv_b = iv_a = band_pts = spread_ratio = None
    if b is not None and a is not None and a > b >= 0:
        spread_ratio = (a - b) / max(m, 1e-9)
        iv_b = _f(implied_vol(symbol, b, Sv, Kv, Tv, option_type, rv, qv))
        iv_a = _f(implied_vol(symbol, a, Sv, Kv, Tv, option_type, rv, qv))
        if iv_b is not None and iv_a is not None:
            band_pts = abs(iv_a - iv_b) * 100.0

    # Confianza: vega alta y banda estrecha suben; spread ancho baja.
    conf = 1.0
    if band_pts is not None:
        conf *= float(max(0.15, 1.0 - min(band_pts / (2.0 * WIDE_IV_BAND_PTS), 0.85)))
    if spread_ratio is not None:
        conf *= float(max(0.20, 1.0 - min(spread_ratio / (2.0 * WIDE_SPREAD_RATIO), 0.80)))
    conf *= float(min(1.0, 0.35 + 0.65 * min(vega / (10.0 * MIN_VEGA_PER_VOL), 1.0)))

    wide = ((band_pts is not None and band_pts > WIDE_IV_BAND_PTS) or
            (spread_ratio is not None and spread_ratio > WIDE_SPREAD_RATIO))
    state = SOLVED_LOW_CONFIDENCE if wide else SOLVED_HIGH_CONFIDENCE
    reason = ("banda bid/ask ancha: la IV está dentro de un intervalo, no en un punto"
              if wide else "vega suficiente y spread sano")

    piv = _f(provider_iv)
    if piv is not None and piv > 0 and not wide:
        # El proveedor puede publicarla; se acepta como OBSERVED sólo si coincide con
        # la inversión propia. Si no coincide, manda la nuestra y se dice por qué.
        if abs(piv - iv) * 100.0 <= 1.0:
            return IVAssessment(OBSERVED_PROVIDER, piv, min(1.0, conf + 0.05),
                                round(vega, 6), round(extrinsic, 6), iv_b, iv_a,
                                None if band_pts is None else round(band_pts, 3),
                                None if spread_ratio is None else round(spread_ratio, 4),
                                "IV del proveedor confirmada por la inversión propia")
        reason = (f"IV del proveedor ({piv * 100:.2f}) difiere de la inversión propia "
                  f"({iv * 100:.2f}); se publica la resuelta contra el precio observado")

    return IVAssessment(state, iv, float(min(max(conf, 0.0), 1.0)), round(vega, 6),
                        round(extrinsic, 6), iv_b, iv_a,
                        None if band_pts is None else round(band_pts, 3),
                        None if spread_ratio is None else round(spread_ratio, 4), reason)


def _campo(a: Any, nombre: str, por_defecto: Any = None) -> Any:
    """Lee la evaluación venga como objeto o como su `describe()`.

    v1.57.0 · La evaluación viaja ahora hasta el Auditor a través del estado
    público, y el estado se serializa a JSON. Un `IVAssessment` no es
    serializable, así que por ahí llega como diccionario. El resumen tiene que
    funcionar con las dos formas o habría dos caminos que dan dos números.
    """
    if isinstance(a, dict):
        return a.get(nombre, por_defecto)
    return getattr(a, nombre, por_defecto)


def chain_quality(assessments) -> Dict[str, Any]:
    """Resumen de identificabilidad para el Auditor y para la superficie."""
    rows = list(assessments or [])
    n = len(rows)
    if not n:
        return {"ready": False, "reason": "sin contratos evaluados"}
    counts: Dict[str, int] = {}
    for a in rows:
        estado = str(_campo(a, "state", UNIDENTIFIABLE))
        counts[estado] = counts.get(estado, 0) + 1
    usable = sum(1 for a in rows if bool(_campo(a, "usable_for_surface", False)))
    return {
        "ready": True, "contracts": n, "states": counts,
        "identifiable": usable,
        "identifiable_pct": round(100.0 * usable / n, 2),
        "mean_confidence": round(
            sum(float(_campo(a, "confidence", 0.0) or 0.0) for a in rows) / n, 3),
        "note": ("Sólo los contratos identificables entran al ajuste de superficie. "
                 "Ajustar contra IV no identificable es ajustar contra ruido."),
    }
