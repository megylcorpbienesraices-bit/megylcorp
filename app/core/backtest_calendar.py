"""Calendario de backtesting · ITM QUANT v1.41.7

Qué se puede reproducir de cada día, y con qué fidelidad. La distinción importa
más que la lista de fechas, porque no todos los días archivados valen para lo
mismo:

* ``CAUSAL``      — el motor estuvo corriendo y archivó la sesión: cadena de
  opciones, interés abierto, IV por strike, flujo OPRA y estructura. Se reproduce
  ENTERA: GEX, DEX, Gamma Flip, muros, flujo de órdenes.
* ``PRECIO``      — el día no está archivado, pero Alpaca conserva sus barras
  históricas. Se reproduce el recorrido del precio y nada más.
* ``NO_DISPONIBLE`` — ni archivo local ni histórico del proveedor.

Esta separación no es un detalle técnico: un backtest de estructura de opciones
sobre un día del que sólo hay precio no mide lo que dice medir. El interés abierto
y la IV por strike de una fecha pasada NO se pueden recuperar de un endpoint de
snapshot —los snapshots describen el presente—, así que un día PRECIO nunca podrá
ascender a CAUSAL a posteriori. Se declara en lugar de disimularse.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from .obs import note as _obs_note

NY = ZoneInfo("America/New_York")

# Festivos de NYSE que caen en día laborable. Un día de mercado cerrado no es un
# hueco de datos: ofrecerlo en el calendario haría perder el tiempo al analista.
_FIXED_HOLIDAYS = {
    (1, 1), (6, 19), (7, 4), (12, 25),
}

LEVELS = ("CAUSAL", "PRECIO", "NO_DISPONIBLE")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, 1) + timedelta(days=32)
    d = date(d.year, d.month, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def market_holidays(year: int) -> set[date]:
    """Festivos recurrentes de NYSE. Las excepciones puntuales no se inventan."""
    out: set[date] = set()
    for m, d in _FIXED_HOLIDAYS:
        fixed = date(year, m, d)
        # Cuando cae en fin de semana el mercado observa el viernes o el lunes.
        if fixed.weekday() == 5:
            fixed -= timedelta(days=1)
        elif fixed.weekday() == 6:
            fixed += timedelta(days=1)
        out.add(fixed)
    out.add(_nth_weekday(year, 1, 0, 3))    # Martin Luther King Jr.
    out.add(_nth_weekday(year, 2, 0, 3))    # Presidents' Day
    out.add(_last_weekday(year, 5, 0))      # Memorial Day
    out.add(_nth_weekday(year, 9, 0, 1))    # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))   # Thanksgiving
    # Viernes Santo: se deriva de la Pascua (algoritmo de Gauss/Meeus).
    a, b, c = year % 19, year // 100, year % 100
    d_, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d_ - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    out.add(date(year, month, day) - timedelta(days=2))
    return out


def trading_days(start: date, end: date) -> List[date]:
    """Días de mercado entre dos fechas, sin fines de semana ni festivos."""
    if end < start:
        start, end = end, start
    holidays: dict[int, set[date]] = {}
    out: List[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            hs = holidays.setdefault(d.year, market_holidays(d.year))
            if d not in hs:
                out.append(d)
        d += timedelta(days=1)
    return out


def build_calendar(storage: Path, symbol: str, start: Any, end: Any,
                   *, archived: Any = None, cached_price: Any = None,
                   provider_configured: bool = True,
                   today: date | None = None) -> Dict[str, Any]:
    """Un nivel de reproducción por cada día de mercado del rango.

    ``archived`` y ``cached_price`` se inyectan para que esta función sea pura y
    verificable: quien la llama sabe qué hay en disco, aquí sólo se clasifica.
    """
    try:
        a = start if isinstance(start, date) else date.fromisoformat(str(start)[:10])
        b = end if isinstance(end, date) else date.fromisoformat(str(end)[:10])
    except Exception as exc:
        _obs_note("backtest_calendar:range", exc)
        return {"ready": False, "reason": "RANGO DE FECHAS INVÁLIDO", "days": []}

    ref = today or datetime.now(NY).date()
    if b > ref:
        b = ref
    if a > b:
        return {"ready": False, "reason": "RANGO SIN DÍAS DE MERCADO", "days": []}

    arch = {str(x)[:10] for x in (archived or ())}
    cached = {str(x)[:10] for x in (cached_price or ())}

    days: List[Dict[str, Any]] = []
    for d in trading_days(a, b):
        iso = d.isoformat()
        if iso in arch:
            level, why = "CAUSAL", None
        elif iso in cached:
            level, why = "PRECIO", "Sin cadena archivada: sólo recorrido del precio."
        elif provider_configured:
            level, why = "PRECIO", "Barras históricas del proveedor, pendientes de descargar."
        else:
            level, why = "NO_DISPONIBLE", "Sin archivo local y sin proveedor configurado."
        days.append({
            "date": iso,
            "weekday": d.weekday(),
            "level": level,
            "reason": why,
            "hydrated": iso in arch or iso in cached,
            # Un día CAUSAL reproduce estructura; uno de PRECIO no puede, y decirlo
            # evita medir un backtest de gamma sobre un día que no tiene gamma.
            "replays_structure": level == "CAUSAL",
        })

    counts = {lv: sum(1 for x in days if x["level"] == lv) for lv in LEVELS}
    return {
        "ready": bool(days),
        "symbol": str(symbol).upper(),
        "start": a.isoformat(), "end": b.isoformat(),
        "days": days,
        "counts": counts,
        "trading_days": len(days),
        "note": ("CAUSAL reproduce la sesión entera con su cadena de opciones. PRECIO "
                 "reproduce sólo el recorrido del precio: el interés abierto y la IV "
                 "por strike de una fecha pasada no se pueden recuperar de un endpoint "
                 "de snapshot, así que un día de PRECIO nunca asciende a CAUSAL."),
    }
