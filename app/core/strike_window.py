"""Qué strikes importan — v1.50.0

EL PROBLEMA
-----------
`exposure-by-strike` devuelve la cadena entera. En DIA a 516 $ eso incluye el
433, el 445 y el 473: strikes a un 16 % del precio, con exposición
prácticamente nula, que ocupan la mitad del panel y **empujan hacia arriba la
zona que de verdad se está mirando**. El perfil se lee alrededor del precio; un
strike al que el mercado no puede llegar en la vida del contrato no es
información, es relleno.

Recortar por un número fijo de strikes no sirve —la separación entre strikes
cambia por activo: 0,50 $ en DIA, 5 $ en SPX, 1 $ en QQQ— y recortar por
dólares tampoco, que es el error que esta serie ya corrigió en la ventana del
mapa de calor.

LA REGLA
--------
Dos criterios, y basta con cumplir uno:

    1 · DISTANCIA.  El strike cae dentro de una banda PORCENTUAL alrededor del
        precio. El porcentaje sale del activo, no de un catálogo de tickers.

    2 · MATERIALIDAD.  El strike lleva exposición comparable a la de la zona
        central aunque esté lejos. Un muro real fuera de la banda tiene que
        seguir viéndose: es precisamente el caso que más importa, y recortarlo
        por distancia sería esconder la respuesta.

Lo que se descarta se CUENTA y se declara. Un panel que recorta en silencio es
indistinguible de un proveedor que no envió esos strikes.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Banda por defecto alrededor del precio, en porcentaje. Es más ancha que la
#: del mapa de calor a propósito: el mapa busca la estructura que gobierna el
#: movimiento inmediato y el perfil, dónde están los muros de la sesión.
DEFAULT_BAND_PCT = 6.0

#: Un strike fuera de la banda se conserva si su magnitud llega a esta fracción
#: del máximo de la zona central. Por debajo, es ruido lejano.
MATERIAL_FRACTION = 0.18

#: …y además tiene que DESTACAR entre los lejanos. Sin esta segunda condición,
#: un perfil plano hace que todos parezcan materiales —todos valen lo mismo que
#: el máximo— y no se recorta nada, que es justo el caso en el que el recorte
#: hace más falta: cuando no hay estructura, lo único que queda es la distancia.
MATERIAL_STANDOUT = 3.0

#: Nunca se recorta por debajo de esto: con pocos strikes, el recorte no ayuda
#: y sí puede dejar el panel sin forma.
MIN_ROWS = 12


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def band_pct_for(symbol: str = "", spot: Optional[float] = None,
                 declared: Optional[float] = None) -> float:
    """Ancho de la banda, en porcentaje del precio.

    Sale del propio activo. No hay ninguna lista de tickers aquí ni la puede
    haber: un porcentaje del precio ya es comparable entre un ETF de 9 $ y un
    índice de 7.500.
    """
    if declared is not None:
        d = _f(declared)
        if d is not None and d > 0:
            return d
    return DEFAULT_BAND_PCT


def magnitude(row: Dict[str, Any], keys: Sequence[str]) -> float:
    """Cuánta exposición lleva un strike, mirando todas las griegas a la vez.

    Se toma el máximo de las magnitudes normalizadas por su propia columna y no
    la suma: GEX y CHEX viven en escalas distintas —millones frente a miles— y
    sumarlas dejaría que la columna más grande decidiera sola.
    """
    best = 0.0
    for k in keys:
        v = _f(row.get(k))
        if v is not None:
            best = max(best, abs(v))
    return best


def relevant_rows(rows: Iterable[Dict[str, Any]], spot: Optional[float], *,
                  symbol: str = "", value_keys: Sequence[str] = ("gex",),
                  band_pct: Optional[float] = None,
                  strike_key: str = "strike") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Los strikes que importan, y qué se dejó fuera.

    Devuelve `(filas, meta)`. `meta` dice cuántas se descartaron y por qué, para
    que el panel pueda declararlo en vez de recortar en silencio.
    """
    src = [r for r in (rows or []) if isinstance(r, dict) and _f(r.get(strike_key)) is not None]
    px = _f(spot)
    pct = band_pct_for(symbol, px, band_pct)
    meta: Dict[str, Any] = {
        "band_pct": pct, "spot": px, "total": len(src), "kept": len(src),
        "dropped": 0, "kept_far": 0, "reason": "",
        "rule": "DISTANCIA_PORCENTUAL_O_MATERIALIDAD",
    }
    if not src or px is None or px <= 0 or len(src) <= MIN_ROWS:
        # Sin precio no hay banda que aplicar, y con pocos strikes el recorte no
        # ayuda: se devuelve todo y se dice por qué.
        meta["reason"] = ("sin precio de referencia" if px is None or px <= 0
                          else "" if len(src) <= MIN_ROWS else "")
        return list(src), meta

    band = px * (pct / 100.0)
    near, far = [], []
    for r in src:
        k = _f(r.get(strike_key))
        (near if abs(k - px) <= band else far).append(r)

    if not near:
        # La banda no atrapó nada: el precio está fuera de la cadena publicada.
        # Se devuelve todo antes que dejar el panel vacío.
        meta["reason"] = "el precio queda fuera de la cadena publicada"
        return list(src), meta

    # Un muro real fuera de la banda tiene que seguir viéndose: es el caso que
    # más importa, y recortarlo por distancia sería esconder la respuesta.
    #
    # Dos condiciones, y hacen falta las dos. La primera —comparable con la zona
    # central— por sí sola no basta: con un perfil PLANO todos los strikes valen
    # lo mismo que el máximo, así que todos pasarían y no se recortaría nada,
    # justo cuando el recorte hace más falta. La segunda pide que además
    # DESTAQUE entre los lejanos, que es lo que distingue un muro de un campo
    # uniforme sin estructura.
    ref = max((magnitude(r, value_keys) for r in near), default=0.0)
    floor = ref * MATERIAL_FRACTION
    far_mags = sorted(magnitude(r, value_keys) for r in far)
    far_ref = far_mags[len(far_mags) // 2] if far_mags else 0.0
    standout = far_ref * MATERIAL_STANDOUT
    kept_far = [r for r in far
                if floor > 0 and magnitude(r, value_keys) >= max(floor, standout)]

    out = near + kept_far
    out.sort(key=lambda r: _f(r.get(strike_key)) or 0.0)
    dropped = len(src) - len(out)
    meta.update({
        "kept": len(out), "dropped": dropped, "kept_far": len(kept_far),
        "low": round(px - band, 4), "high": round(px + band, 4),
        "reason": (f"{dropped} strikes a más de ±{pct:g}% del precio y sin exposición "
                   f"comparable a la de la zona central" if dropped else ""),
    })
    return out, meta
