"""Universo operativo de ITM QUANT · v1.58.0

UNA SOLA LISTA, Y NADA DE LÓGICA POR ACTIVO.

Esto NO es una tabla de comportamiento: no hay aquí ventanas, ni proveedores, ni
griegas, ni nada que el motor tenga que consultar para calcular. El motor sigue
siendo genérico y universal: la matemática de NVDA es la misma que la de SPY y la
que tendría cualquier símbolo que se añadiera mañana. Lo único que esta lista
decide es **qué símbolos existen para el programa**.

Por qué hace falta un cerrojo y no una preferencia:

* El catálogo dinámico registraba TODO lo que el proveedor catalogaba —miles de
  símbolos—. Cada uno entraba en el selector, en el scanner, en la precarga y,
  sobre todo, en las peticiones al proveedor. El contrato de Quant Data son 240
  peticiones por 60 s: gastarlas en símbolos que nadie mira es gastarlas en nada.
* Un universo que crece solo tampoco se puede certificar. «Funciona en todos los
  activos» no es comprobable sobre una lista que cambia cada vez que el proveedor
  publica su catálogo.

De modo que el universo queda cerrado a 37 símbolos, y el cierre se aplica en el
punto de entrada del catálogo: lo que no está aquí no se registra, y lo que no se
registra no se ofrece, no se pide y no se precarga.
"""
from __future__ import annotations

from typing import Iterable

#: Acciones. El orden es el de presentación, no una prioridad de cálculo.
EQUITIES: tuple[str, ...] = (
    "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "GOOGL", "AMD", "AVGO",
    "NFLX", "JPM", "BAC", "XOM", "PLTR", "COIN",
)

#: ETFs. Índices amplios, sectoriales SPDR, semiconductores, metales, renta fija,
#: energía y emergentes.
ETFS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLI", "XLY", "XLP", "XLE", "XLV", "XLU",
    "SMH", "SOXX", "GLD", "GDX", "TLT", "HYG", "USO", "EEM", "FXI",
)

#: El universo operativo completo. Congelado a propósito: nadie lo muta en runtime.
ALLOWED: frozenset[str] = frozenset(EQUITIES + ETFS)

#: Tamaño declarado. Si alguien añade o quita sin querer, la suite lo dice.
#:
#: NOTA · El encargo decía «Total: 37 activos», pero la lista enumerada suma 36:
#: 15 acciones y 21 ETFs. Se respeta la ENUMERACIÓN, que es el dato concreto, y
#: no el total, que es el resumen. Añadir un símbolo a ojo para cuadrar la cuenta
#: sería meter en el universo un activo que nadie pidió.
EXPECTED_SIZE = 36

#: Orden estable para cualquier lista visible: acciones primero, después ETFs.
ORDERED: tuple[str, ...] = EQUITIES + ETFS


def normalize(symbol: object) -> str:
    """Forma canónica de un símbolo. Mayúsculas y sin espacios, nada más."""
    return str(symbol or "").strip().upper()


def is_allowed(symbol: object) -> bool:
    """¿Pertenece este símbolo al universo operativo?"""
    return normalize(symbol) in ALLOWED


def filter_allowed(symbols: Iterable[object]) -> list[str]:
    """Los símbolos permitidos de una secuencia, en el orden en que llegan.

    Sin duplicados: una lista de presentación con el mismo símbolo dos veces es
    un defecto visible, y aquí es el sitio barato de impedirlo.
    """
    vistos: set[str] = set()
    out: list[str] = []
    for s in symbols or ():
        sym = normalize(s)
        if sym and sym in ALLOWED and sym not in vistos:
            vistos.add(sym)
            out.append(sym)
    return out


def rejected(symbols: Iterable[object]) -> list[str]:
    """Los que quedan fuera. Para poder DECIR cuántos se filtraron, no sólo hacerlo."""
    vistos: set[str] = set()
    out: list[str] = []
    for s in symbols or ():
        sym = normalize(s)
        if sym and sym not in ALLOWED and sym not in vistos:
            vistos.add(sym)
            out.append(sym)
    return out


def describe() -> dict:
    """El universo, tal cual, para el Auditor y para los informes."""
    return {
        "size": len(ALLOWED),
        "equities": list(EQUITIES),
        "etfs": list(ETFS),
        "ordered": list(ORDERED),
        "policy": ("UNIVERSO CERRADO · el motor sigue siendo genérico; "
                   "lo único acotado es qué símbolos existen para el programa"),
    }
