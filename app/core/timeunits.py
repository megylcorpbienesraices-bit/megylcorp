"""Conversión de marcas de tiempo a epoch, independiente de la resolución (v1.27.3).

El bug que cierra
-----------------
pandas 3.0 cambió la resolución por defecto de `datetime64[ns]` a `datetime64[us]`.
El idioma habitual

    t_ms = serie_de_tiempo.astype("int64") // 1_000_000

asume nanosegundos. Con microsegundos devuelve **segundos**, es decir valores
1000 veces más grandes de lo previsto, y sin lanzar ningún error.

Impacto medido en ITM QUANT v1.27.2 (con pandas 3.0.2):

    dealer_microstructure._bucket250   250 ms  ->  250 s   (4.2 minutos)
    dealer_microstructure._bucket75     75 ms  ->   75 s   (1.25 minutos)
    institutional_research (Hawkes)     60 s   ->  16.7 horas; tau 10 s -> 10 000 s

Un sweep es un fenómeno de milisegundos entre venues. Agrupar 4.2 minutos de
flujo como un solo "sweep" produce falsos positivos masivos justo en el módulo
que alimenta el Aggression Trigger.

`requirements.txt` declaraba `pandas>=2.0` sin fijar: en la máquina de desarrollo
con pandas 2.x el código funciona, y en una instalación nueva de VPS con
pandas 3.x se rompe en silencio. Es exactamente el tipo de fallo que solo aparece
después de desplegar.

Nota importante
---------------
`pd.Timestamp.value` (escalar) SÍ devuelve nanosegundos siempre, en cualquier
resolución. Por eso `causality_engine`, `provider_flow_fabric` y
`temporal_causality`, que usan `.value`, NO estaban afectados. El problema es
exclusivo de `Series.astype("int64")`.

Estas funciones usan aritmética con `Timedelta`, que es agnóstica a la unidad y
se comporta igual en ns, us y ms (verificado en las tres resoluciones).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_EPOCH = pd.Timestamp(0)


def to_datetime(values) -> pd.Series:
    """Serie datetime UTC-naive normalizada, con no-parseables como NaT.

    La normalización de zona horaria es obligatoria: el idioma original
    `astype("int64")` funcionaba con series tz-aware y tz-naive por igual (siempre
    devolvía el epoch UTC), así que el reemplazo tiene que tolerar lo mismo. Restar
    un `Timestamp(0)` tz-naive de una serie tz-aware lanza TypeError.
    """
    s = pd.to_datetime(values, errors="coerce")
    if not isinstance(s, pd.Series):
        s = pd.Series(s)
    if isinstance(s.dtype, pd.DatetimeTZDtype):
        s = s.dt.tz_convert("UTC").dt.tz_localize(None)
    return s


# Centinela para marcas no parseables. El `astype("int64")` original convertía NaT
# al mínimo de int64, de modo que esas filas nunca coincidían con un bucket real.
# Se conserva esa semántica de forma explícita en vez de heredarla por accidente.
NAT_BUCKET = np.iinfo(np.int64).min


def epoch_ms(values) -> pd.Series:
    """Milisegundos epoch como int64. Correcto en ns, us y ms.

    Sustituye a `serie.astype("int64") // 1_000_000`.
    """
    s = to_datetime(values)
    raw = (s - _EPOCH) // pd.Timedelta(1, "ms")
    return raw.fillna(NAT_BUCKET).astype("int64")


def epoch_seconds(values) -> pd.Series:
    """Segundos epoch como float (conserva la parte fraccionaria).

    Sustituye a `serie.astype("int64") / 1e9`.
    """
    s = to_datetime(values)
    return ((s - _EPOCH) / pd.Timedelta(1, "s")).astype(float)  # NaT -> NaN


def epoch_seconds_array(values) -> np.ndarray:
    """Igual que `epoch_seconds` pero como ndarray, para cálculo numérico."""
    return epoch_seconds(values).to_numpy(dtype=float)


def bucket(values, width_ms: int) -> pd.Series:
    """Índice de ventana temporal de `width_ms` MILISEGUNDOS reales.

    Usar esto en vez de calcular el bucket a mano evita reintroducir la
    suposición de unidad en cada sitio nuevo.
    """
    if int(width_ms) <= 0:
        raise ValueError("width_ms debe ser positivo")
    return (epoch_ms(values) // int(width_ms)).astype("int64")
