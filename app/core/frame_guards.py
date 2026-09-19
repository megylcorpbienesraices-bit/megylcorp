"""Accesores de columna que de verdad toleran que la columna no esté (v1.42.3).

EL PATRÓN QUE NO PROTEGE
------------------------
Por todo el motor aparece esta forma, escrita con intención defensiva:

    pd.to_numeric(frame.get("volume", 0), errors="coerce").fillna(0.0)

El `0` de respaldo dice «si no está la columna, trátala como cero». Pero
``DataFrame.get(name, 0)`` devuelve el **escalar** ``0`` cuando la columna falta,
``pd.to_numeric`` sobre un escalar devuelve otro escalar, y un escalar no tiene
``.fillna`` — así que la línea escrita para no fallar es exactamente la que lanza
``AttributeError: 'int' object has no attribute 'fillna'``.

No es hipotético: ``nextgen_terminal._ticks_frame`` lo hacía con ``signed_volume``,
y una fuente de ticks sin esa columna tumbaba la construcción del payload de TRACE
entero. El mismo fallo ya se había corregido a mano en el heatmap (v1.27.1) y
sobrevivió en el resto de sitios, que es lo que pasa cuando se arregla el caso en
vez del patrón.

LA REGLA
--------
Una columna ausente es un estado válido y se degrada a un valor declarado. Una
columna presente con basura dentro también. Lo que nunca es válido es que el
accesor decida por su cuenta romper la sección.
"""
from __future__ import annotations


import pandas as pd


def numeric_column(frame: pd.DataFrame, name: str, default: float = 0.0) -> pd.Series:
    """Serie float alineada a ``frame``, exista o no la columna ``name``.

    Los valores no convertibles pasan a ``default``, nunca a NaN suelto: un NaN
    comparado con un umbral devuelve False en silencio, que es la forma más
    discreta que tiene un dato ausente de apagar una condición.
    """
    if not isinstance(frame, pd.DataFrame):
        return pd.Series(dtype=float)
    if name not in frame.columns:
        return pd.Series(float(default), index=frame.index, dtype=float)
    return pd.to_numeric(frame[name], errors="coerce").fillna(float(default)).astype(float)


def first_numeric_column(frame: pd.DataFrame, *names: str, default: float = 0.0) -> pd.Series:
    """La primera de ``names`` que exista; si no hay ninguna, ``default``.

    Para campos que distintos proveedores nombran distinto (``volume`` frente a
    ``option_volume``) sin que eso deba costar una rama en cada llamador.
    """
    for name in names:
        if isinstance(frame, pd.DataFrame) and name in frame.columns:
            return numeric_column(frame, name, default)
    if not isinstance(frame, pd.DataFrame):
        return pd.Series(dtype=float)
    return pd.Series(float(default), index=frame.index, dtype=float)


def text_column(frame: pd.DataFrame, name: str, default: str = "") -> pd.Series:
    """Equivalente para columnas de texto (``option_type``, ``aggressor``…)."""
    if not isinstance(frame, pd.DataFrame):
        return pd.Series(dtype=object)
    if name not in frame.columns:
        return pd.Series(str(default), index=frame.index, dtype=object)
    return frame[name].astype(str).fillna(str(default))


__all__ = ["numeric_column", "first_numeric_column", "text_column"]
