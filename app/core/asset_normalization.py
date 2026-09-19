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

from .obs import expected as _obs_expected

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
            # Una fila sin magnitud numérica es un hueco del proveedor, no un
            # fallo: se descarta del cálculo de escala y se deja constancia.
            _obs_expected("asset_normalization.non_numeric_sample")
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


# Por debajo de este percentil la celda se considera fondo y no se pinta. Es un
# suelo de RUIDO, no un umbral de magnitud: se expresa en percentil para que valga
# igual en un activo con cola pesada y en uno con el campo repartido.
NOISE_PERCENTILE = 0.35


def normalize_matrix(matrix: Sequence[Sequence[Any]], *, symbol: str = "",
                     clip: float = 1.0, mode: str = "RANK") -> Dict[str, Any]:
    """Matriz del Interval Map llevada a [-1, 1] de forma legible en CUALQUIER activo.

    POR QUÉ NO BASTA DIVIDIR POR EL PERCENTIL 95
    --------------------------------------------
    La versión lineal (`x / p95`) funciona cuando el campo está repartido. El campo
    de exposición NO lo está: tiene cola pesadísima —unos pocos strikes concentran
    casi todo— así que la inmensa mayoría de las celdas caía por debajo del suelo
    de opacidad del renderizador y el mapa se veía VACÍO. No le faltaban datos: le
    sobraba dinámica para una escala lineal.

    Y el defecto no era igual en todos los activos: cuanto más concentrada la
    cadena, más vacío el mapa. Eso es precisamente un parámetro implícito por
    ticker, que es lo que no queremos.

    LA CORRECCIÓN: NORMALIZACIÓN POR RANGO
    --------------------------------------
    Cada celda se sustituye por el PERCENTIL que ocupa |valor| dentro de la matriz,
    conservando el signo. La salida se reparte por construcción entre 0 y 1
    independientemente de la forma de la distribución, así que un activo con cola
    pesada y otro con el campo plano producen mapas igual de legibles sin tocar
    ninguna constante.

    Lo que se conserva y lo que no: el ORDEN de las celdas es exacto —si A tiene
    más exposición que B, se ve más intensa— pero la intensidad ya no es
    proporcional a la magnitud. Para eso está la matriz cruda, que viaja aparte, y
    el tooltip. Un mapa de calor sirve para ver DÓNDE y CÓMO SE MUEVE la
    concentración; leer magnitudes en él nunca fue fiable.

    `mode="LINEAR"` conserva el comportamiento anterior para quien necesite
    proporcionalidad estricta.
    """
    # `matrix or []` fallaba con un `ndarray`: NumPy no define el valor de verdad
    # de un array de más de un elemento. Quien pase una matriz de NumPy —el arnés
    # visual, una prueba, un carril futuro— recibía un ValueError en lugar de un
    # mapa. Se normaliza la ENTRADA antes de normalizar los valores.
    if matrix is None:
        rows: List[List[Any]] = []
    elif hasattr(matrix, "tolist"):
        raw = matrix.tolist()
        rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in raw]
    else:
        rows = [list(r) if isinstance(r, (list, tuple)) else
                (list(r) if hasattr(r, "tolist") or hasattr(r, "__iter__") else [])
                for r in matrix]
    flat: List[float] = []
    for r in rows:
        for v in r:
            try:
                x = float(v)
            except (TypeError, ValueError):
                x = 0.0
            flat.append(x if math.isfinite(x) else 0.0)

    if not flat:
        return {"ready": False, "matrix": [], "scale": None,
                "symbol": str(symbol).upper(), "reason": "MATRIZ VACÍA"}

    arr = np.asarray(flat, dtype=float)
    mag = np.abs(arr)
    nonzero = mag[mag > 0]
    if nonzero.size == 0:
        # Todo ceros REALES. Se declara en vez de fabricar contraste.
        return {"ready": True, "matrix": [[0.0] * len(r) for r in rows], "scale": 0.0,
                "symbol": str(symbol).upper(), "normalization": "ALL_ZERO_OBSERVED",
                "reason": "todas las celdas observadas son cero"}

    if str(mode).upper() == "LINEAR":
        scale = float(np.percentile(nonzero, 95)) or float(nonzero.max())
        norm = np.clip(arr / max(scale, 1e-12), -abs(clip), abs(clip))
        normalization = "ASSET_P95_ABS"
    else:
        # Percentil de cada celda dentro de las celdas NO nulas. Las nulas quedan en
        # cero: son ausencia de exposición, no el extremo inferior de la escala.
        order = np.argsort(np.argsort(nonzero))
        ranks = (order + 0.5) / nonzero.size          # (0, 1), sin 0 ni 1 exactos
        lookup = dict(zip(nonzero.tolist(), ranks.tolist()))
        graded = np.array([lookup.get(m, 0.0) for m in mag], dtype=float)
        # Se reescala el rango útil por encima del suelo de ruido para que la parte
        # baja de la distribución no ocupe la mitad de la paleta.
        graded = np.clip((graded - NOISE_PERCENTILE) / (1.0 - NOISE_PERCENTILE), 0.0, 1.0)
        norm = np.sign(arr) * graded * abs(clip)
        scale = float(np.percentile(nonzero, 95))
        normalization = "ASSET_RANK_PERCENTILE"

    out: List[List[float]] = []
    i = 0
    for r in rows:
        line: List[float] = []
        for _ in r:
            line.append(round(float(norm[i]), 6))
            i += 1
        out.append(line)
    filled = float(np.mean(np.abs(norm) > 0.02)) if norm.size else 0.0
    return {"ready": True, "matrix": out, "scale": round(float(scale), 6),
            "symbol": str(symbol).upper(), "normalization": normalization,
            # Proporción de celdas que de verdad se van a ver. Si es ~0, el mapa se
            # verá vacío y hay que saberlo AQUÍ, no descubrirlo mirando la pantalla.
            "filled_ratio": round(filled, 4),
            "cells": int(norm.size)}


__all__ = ["AssetScale", "ConcentrationThreshold", "asset_scale",
           "concentration_threshold", "normalize_series", "normalize_matrix",
           "DEFAULT_QUANTILE", "DEFAULT_PEAK_SHARE", "DEFAULT_ROBUST_Z", "MIN_SAMPLES",
           "NOISE_PERCENTILE"]
