"""NORMALIZACIÓN POR ACTIVO · ITM QUANT v1.43.0

EL PROBLEMA QUE RESUELVE
------------------------
«Concentración importante = más de 5 M$» es una regla que sólo puede estar bien
para un activo. En SPY cinco millones de prima en un minuto es un martes
cualquiera; en DIA es un evento; en una acción mediana puede ser el día entero. Un
umbral fijo en dólares convierte el mismo código en un detector hipersensible para
unos activos y en uno sordo para otros, y ésa es exactamente la razón por la que
«funciona en DIA» no certifica nada.

LA REGLA
--------
El umbral no se escribe: se MIDE sobre el propio activo, en su propia sesión. Un
intervalo es extraordinario cuando lo es **respecto de los demás intervalos de ese
activo**, no respecto de una constante.

Se usan tres lentes a la vez porque cada una falla sola:

  * **Cuantil de sesión** — dice qué es «alto» hoy. Falla en una sesión plana:
    el percentil 97 de puro ruido sigue siendo ruido.
  * **Fracción del pico** — protege de esa sesión plana: si el mayor intervalo del
    día es pequeño, nada del día es un evento.
  * **Z robusta (mediana + MAD)** — cuántas desviaciones típicas por encima de lo
    normal está el intervalo. La mediana y el MAD no se mueven porque haya cuatro
    barras enormes, que es justo cuando hace falta que no se muevan.

Se exige la conjunción, no la disyunción: pasar una sola lente es fácil y produce
falsos eventos.

ESCALA DEL ACTIVO
-----------------
`asset_scale()` publica la unidad natural del activo —la mediana de |prima| por
intervalo— para poder expresar cualquier magnitud en múltiplos de esa unidad.
Eso permite comparar la intensidad del flujo de SPY con la de AAPL sin comparar
sus dólares, que no son comparables.

NADA DE ESTE MÓDULO CONOCE NINGÚN TICKER. Si aparece un símbolo escrito en el
código de este archivo, es un error.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

# Cuantil a partir del cual un intervalo es «alto» para su propia sesión.
DEFAULT_QUANTILE = 0.97
# Fracción mínima del mayor intervalo del día. Evita que una sesión plana marque
# como evento su propio ruido.
DEFAULT_PEAK_SHARE = 0.25
# Z robusta mínima. 3.5 MAD es el criterio habitual de atípico robusto.
DEFAULT_ROBUST_Z = 3.5
# Mínimo de observaciones para que una distribución signifique algo.
MIN_SAMPLES = 8

# Constante que convierte MAD en desviación típica equivalente bajo normalidad.
_MAD_TO_SIGMA = 1.4826


def _finite(values: Iterable[Any]) -> np.ndarray:
    out: List[float] = []
    for v in values or ():
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x):
            out.append(x)
    return np.asarray(out, dtype=float)


@dataclass(frozen=True)
class AssetScale:
    """Unidad natural del activo, medida sobre el activo.

    `unit` es la mediana de la magnitud observada por intervalo. Todo lo demás se
    expresa en múltiplos de ella, de modo que «3.2 unidades» significa lo mismo en
    cualquier activo aunque sus dólares no se parezcan en nada.
    """

    symbol: str
    samples: int
    unit: Optional[float]
    median: Optional[float]
    mad: Optional[float]
    peak: Optional[float]
    quantile_value: Optional[float]
    quantile: float
    ready: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def in_units(self, value: Any) -> Optional[float]:
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        if not self.unit or not math.isfinite(self.unit) or self.unit <= 0:
            return None
        return round(x / self.unit, 4)

    def robust_z(self, value: Any) -> Optional[float]:
        """Cuántas desviaciones robustas por encima de lo normal está `value`."""
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        if self.median is None:
            return None
        sigma = (self.mad or 0.0) * _MAD_TO_SIGMA
        if sigma <= 1e-12:
            # Sin dispersión medible no se puede afirmar que algo sea atípico. Se
            # dice que no se sabe, en vez de devolver un infinito que aguas abajo
            # marcaría TODO como evento.
            return None
        return round((x - self.median) / sigma, 4)


def asset_scale(values: Sequence[Any], *, symbol: str = "",
                quantile: float = DEFAULT_QUANTILE) -> AssetScale:
    """Estadística robusta de la magnitud observada para ESTE activo."""
    sym = str(symbol or "").upper()
    arr = _finite(values)
    arr = arr[np.isfinite(arr)]
    arr = np.abs(arr)
    arr = arr[arr > 0]
    if arr.size < MIN_SAMPLES:
        return AssetScale(sym, int(arr.size), None, None, None,
                          float(arr.max()) if arr.size else None, None, quantile, False,
                          f"se necesitan {MIN_SAMPLES} intervalos con magnitud; hay {arr.size}")
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    peak = float(arr.max())
    qv = float(np.quantile(arr, min(max(quantile, 0.5), 0.999)))
    unit = median if median > 0 else (peak or None)
    return AssetScale(sym, int(arr.size), unit, median, mad, peak, qv, quantile, True)


@dataclass(frozen=True)
class ConcentrationThreshold:
    """El umbral que de verdad se aplicó, con las tres lentes a la vista."""

    symbol: str
    ready: bool
    floor: Optional[float]
    quantile_value: Optional[float]
    peak_share_value: Optional[float]
    robust_z_value: Optional[float]
    scale: Dict[str, Any]
    method: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def concentration_threshold(values: Sequence[Any], *, symbol: str = "",
                            quantile: float = DEFAULT_QUANTILE,
                            peak_share: float = DEFAULT_PEAK_SHARE,
                            robust_z: float = DEFAULT_ROBUST_Z) -> ConcentrationThreshold:
    """Umbral de concentración medido sobre el propio activo.

    El umbral final es el MÁXIMO de las tres lentes: para ser evento hay que
    superarlas todas. Tomar el mínimo habría bastado con pasar la más laxa, que es
    como se fabrican los falsos positivos.
    """
    sc = asset_scale(values, symbol=symbol, quantile=quantile)
    if not sc.ready:
        return ConcentrationThreshold(sc.symbol, False, None, None, None, None,
                                      sc.to_dict(), "ASSET_RELATIVE_TRIPLE_GATE", sc.reason)
    qv = sc.quantile_value or 0.0
    pv = (sc.peak or 0.0) * max(0.0, min(1.0, peak_share))
    sigma = (sc.mad or 0.0) * _MAD_TO_SIGMA
    zv = (sc.median or 0.0) + robust_z * sigma if sigma > 1e-12 else 0.0
    floor = max(qv, pv, zv)
    return ConcentrationThreshold(
        sc.symbol, True, round(float(floor), 6), round(float(qv), 6),
        round(float(pv), 6), (round(float(zv), 6) if zv else None), sc.to_dict(),
        "ASSET_RELATIVE_TRIPLE_GATE",
        (f"cuantil {quantile:.2f}={qv:.4g} · {peak_share:.0%} del pico={pv:.4g}"
         + (f" · z robusta {robust_z}={zv:.4g}" if zv else " · sin dispersión medible")),
    )


def normalize_series(values: Sequence[Any], *, symbol: str = "",
                     clip: float = 1.0) -> Dict[str, Any]:
    """Serie llevada a [-clip, clip] con la escala del propio activo.

    La escala es el percentil 95 de |valor|, no el máximo: un único pico no debe
    aplastar contra cero todo el resto de la sesión, que es lo que hace que un mapa
    de calor se vea vacío cuando en realidad tiene estructura.
    """
    arr = _finite(values)
    if arr.size == 0:
        return {"ready": False, "values": [], "scale": None, "symbol": str(symbol).upper(),
                "reason": "SIN VALORES FINITOS"}
    mag = np.abs(arr)
    scale = float(np.percentile(mag, 95)) if mag.size else 0.0
    if not math.isfinite(scale) or scale <= 1e-12:
        scale = float(mag.max()) if mag.size else 0.0
    if not math.isfinite(scale) or scale <= 1e-12:
        # Todo ceros REALES: la serie normalizada es de ceros, y se declara.
        return {"ready": True, "values": [0.0] * int(arr.size), "scale": 0.0,
                "symbol": str(symbol).upper(), "normalization": "ALL_ZERO_OBSERVED",
                "reason": "todos los valores observados son cero"}
    out = np.clip(arr / scale, -abs(clip), abs(clip))
    return {"ready": True, "values": [round(float(v), 6) for v in out],
            "scale": round(scale, 6), "symbol": str(symbol).upper(),
            "normalization": "ASSET_P95_ABS"}


def normalize_matrix(matrix: Sequence[Sequence[Any]], *, symbol: str = "",
                     clip: float = 1.0) -> Dict[str, Any]:
    """Igual que `normalize_series` pero para la matriz del Interval Map.

    La escala es ÚNICA para toda la matriz. Normalizar cada columna por su cuenta
    haría que un minuto sin exposición se viera tan intenso como el máximo del día,
    y el mapa dejaría de contar cómo migra la exposición, que es para lo que está.
    """
    rows = [list(r) if isinstance(r, (list, tuple)) else [] for r in (matrix or [])]
    flat = [v for r in rows for v in r]
    norm = normalize_series(flat, symbol=symbol, clip=clip)
    if not norm.get("ready"):
        return {"ready": False, "matrix": [], "scale": None,
                "symbol": str(symbol).upper(), "reason": norm.get("reason")}
    scale = float(norm["scale"] or 0.0)
    out: List[List[float]] = []
    for r in rows:
        line: List[float] = []
        for v in r:
            try:
                x = float(v)
            except (TypeError, ValueError):
                x = 0.0
            if not math.isfinite(x):
                x = 0.0
            line.append(0.0 if scale <= 1e-12 else round(max(-abs(clip), min(abs(clip), x / scale)), 6))
        out.append(line)
    return {"ready": True, "matrix": out, "scale": round(scale, 6),
            "symbol": str(symbol).upper(),
            "normalization": norm.get("normalization", "ASSET_P95_ABS")}


__all__ = ["AssetScale", "ConcentrationThreshold", "asset_scale",
           "concentration_threshold", "normalize_series", "normalize_matrix",
           "DEFAULT_QUANTILE", "DEFAULT_PEAK_SHARE", "DEFAULT_ROBUST_Z", "MIN_SAMPLES"]
