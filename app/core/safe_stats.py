"""Estadísticos con guarda: correlación indefinida ≠ correlación cero (v1.42.1).

EL AVISO QUE LLENABA LA TERMINAL
--------------------------------
    ConstantInputWarning: An input array is constant;
    the correlation coefficient is not defined.

No tumbaba nada, y por eso llevaba tiempo ahí. Pero la matemática que hay detrás sí
importa. La correlación de Pearson es

    corr(x, y) = cov(x, y) / (σx · σy)

Si una de las series es constante, su σ es 0 y el cociente no está definido. NumPy
devuelve `nan` y avisa. El problema no es el aviso: es lo que pasa después con ese
`nan` si nadie lo mira. Se propaga a Market State, al Scanner y a la calibración, y
un `nan` comparado con un umbral devuelve `False` en silencio — es decir, apaga una
condición sin que nadie se entere.

Y hay algo peor que el `nan`: sustituirlo por 0. «No se puede medir la relación»
y «no hay relación» son afirmaciones distintas. Un activo cuyo precio no se movió
en la ventana no está descorrelacionado del mercado; simplemente no hay información.

LA REGLA
--------
Se comprueba ANTES de llamar a la función. Si no se puede medir, se dice:

    UNDEFINED_CONSTANT_SERIES   una de las series no varía
    INSUFFICIENT_SAMPLE         no hay observaciones suficientes
    UNDEFINED_NON_FINITE        quedan menos de dos pares finitos

Nunca un 0 disfrazado, y nunca un `nan` suelto circulando por el motor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

OK = "OK"
UNDEFINED_CONSTANT = "UNDEFINED_CONSTANT_SERIES"
INSUFFICIENT = "INSUFFICIENT_SAMPLE"
UNDEFINED_NON_FINITE = "UNDEFINED_NON_FINITE"

MIN_SAMPLES = 3
# Una serie cuya desviación típica relativa está por debajo de esto es, a efectos
# numéricos, constante: el cociente amplificaría ruido de coma flotante.
EPS_RELATIVE = 1e-12


@dataclass(frozen=True)
class Correlation:
    value: Optional[float]
    state: str
    samples: int
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state == OK and self.value is not None

    def describe(self) -> Dict[str, Any]:
        return {"correlation": None if self.value is None else round(self.value, 6),
                "state": self.state, "samples": self.samples, "usable": self.usable,
                "detail": self.detail}

    def or_none(self) -> Optional[float]:
        """Para llamadores que sólo quieren el número. Devuelve None, nunca 0 ni nan."""
        return self.value if self.usable else None


def _clean_pairs(x: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(x, dtype=float).ravel()
    b = np.asarray(y, dtype=float).ravel()
    n = min(a.size, b.size)
    a, b = a[:n], b[:n]
    ok = np.isfinite(a) & np.isfinite(b)
    return a[ok], b[ok]


def _is_constant(v: np.ndarray) -> bool:
    if v.size < 2:
        return True
    sd = float(np.std(v))
    scale = max(float(np.mean(np.abs(v))), 1.0)
    return sd <= EPS_RELATIVE * scale


def correlation(x: Any, y: Any, *, method: str = "pearson",
                min_samples: int = MIN_SAMPLES) -> Correlation:
    """Correlación que NO llama a la función cuando el resultado no existe."""
    a, b = _clean_pairs(x, y)
    n = int(a.size)
    if n < 2:
        return Correlation(None, UNDEFINED_NON_FINITE, n,
                           "menos de dos pares finitos tras descartar NaN/inf")
    if n < int(min_samples):
        return Correlation(None, INSUFFICIENT, n,
                           f"{n} observaciones; se exigen {int(min_samples)}")

    if str(method).lower().startswith("spear"):
        # El rango de una serie constante también es constante, así que la guarda
        # se aplica sobre los rangos, no sobre los valores.
        a = _rank(a)
        b = _rank(b)

    if _is_constant(a) or _is_constant(b):
        which = "la primera" if _is_constant(a) else "la segunda"
        return Correlation(None, UNDEFINED_CONSTANT, n,
                           f"{which} serie no varía: σ = 0 y el coeficiente no está "
                           "definido. No es correlación cero; es ausencia de medida.")

    num = float(np.sum((a - a.mean()) * (b - b.mean())))
    den = float(math.sqrt(np.sum((a - a.mean()) ** 2) * np.sum((b - b.mean()) ** 2)))
    if den <= 0.0 or not math.isfinite(den):
        return Correlation(None, UNDEFINED_CONSTANT, n,
                           "el denominador se anula pese a la guarda previa")
    r = num / den
    if not math.isfinite(r):
        return Correlation(None, UNDEFINED_NON_FINITE, n, "resultado no finito")
    return Correlation(float(max(-1.0, min(1.0, r))), OK, n,
                       f"{method} sobre {n} observaciones")


def _rank(v: np.ndarray) -> np.ndarray:
    """Rangos con empates promediados, como hace Spearman."""
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(v.size, dtype=float)
    ranks[order] = np.arange(1, v.size + 1, dtype=float)
    # promediar empates
    uniq, inv, counts = np.unique(v, return_inverse=True, return_counts=True)
    if counts.max() > 1:
        sums = np.zeros(uniq.size, dtype=float)
        np.add.at(sums, inv, ranks)
        ranks = (sums / counts)[inv]
    return ranks


def safe_corrcoef(x: Any, y: Any, *, min_samples: int = MIN_SAMPLES) -> Optional[float]:
    """Reemplazo directo de `np.corrcoef(x, y)[0, 1]` que nunca emite avisos."""
    return correlation(x, y, min_samples=min_samples).or_none()


def safe_std(v: Any) -> Optional[float]:
    a = np.asarray(v, dtype=float).ravel()
    a = a[np.isfinite(a)]
    if a.size < 2:
        return None
    sd = float(np.std(a, ddof=1))
    return sd if math.isfinite(sd) else None
