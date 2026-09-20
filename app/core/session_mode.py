"""Modo de sesión: LONDON_MONITOR → NEW_YORK, en hora de Ecuador.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ
═══════════════════════════════════════════════════════════════════════════

A las 04:00 de Ecuador el mercado europeo ya lleva horas moviendo el
subyacente, y la terminal estaba dormida hasta la apertura de Nueva York.
Todo lo que ocurría antes —el recorrido del precio, el volumen, el flujo que
sí hubiera— se perdía, y a las 09:30 se empezaba a medir desde cero como si
la sesión naciera ahí.

Desde las 04:00 se monitoriza y se acumula por separado, de modo que después
se pueda preguntar «¿cuánto se movió en Londres?» y «¿qué hizo Nueva York
sobre eso?» sin que una respuesta contamine la otra.

═══════════════════════════════════════════════════════════════════════════
LA ZONA HORARIA ES PARTE DE LA REGLA
═══════════════════════════════════════════════════════════════════════════

`America/Guayaquil` con inicio a las 04:00, NO una hora UTC fija. Ecuador no
aplica horario de verano y Nueva York sí, así que el desfase entre los dos
cambia dos veces al año. Una hora UTC escrita a mano acertaría medio año y
fallaría el otro medio sin que nada avisara.

═══════════════════════════════════════════════════════════════════════════
LO QUE ESTE MÓDULO NO HACE
═══════════════════════════════════════════════════════════════════════════

No borra nada al cambiar de sesión. Cuando empieza Nueva York, Londres se
CIERRA —se sella su acumulado— y se conserva para comparar. No pone ceros y
no inventa actividad: si a las 04:00 no hay operaciones de opciones, las
opciones dicen que esperan flujo y la estructura previa sigue como último
valor bueno.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

#: La sesión se define en hora local de Ecuador, no en UTC.
EC = ZoneInfo("America/Guayaquil")
NY = ZoneInfo("America/New_York")

#: A esta hora de Ecuador arranca la monitorización de Londres.
LONDON_START = time(4, 0)

#: Apertura del regular de Nueva York, en HORA DE NUEVA YORK. Se convierte a
#: Ecuador en cada fecha, que es lo que hace que el cambio de horario de verano
#: no rompa el corte.
NY_OPEN = time(9, 30)
NY_CLOSE = time(16, 0)

# Modos.
PRE_LONDON = "PRE_LONDON"
LONDON_MONITOR = "LONDON_MONITOR"
NEW_YORK = "NEW_YORK"
POST_MARKET = "POST_MARKET"

_LOCK = threading.Lock()
#: (symbol, trading_date, session) → acumulado
_ACC: Dict[tuple, Dict[str, Any]] = {}


def _ec(now: Optional[datetime] = None) -> datetime:
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.astimezone(EC)


def ny_open_in_ec(day: date) -> datetime:
    """La apertura de Nueva York de ese día, expresada en hora de Ecuador.

    Se construye en hora de NUEVA YORK y se convierte. Al revés —fijar una hora
    de Ecuador— el corte se desplazaría una hora cada vez que Nueva York entra
    o sale del horario de verano.
    """
    return datetime.combine(day, NY_OPEN, tzinfo=NY).astimezone(EC)


def ny_close_in_ec(day: date) -> datetime:
    return datetime.combine(day, NY_CLOSE, tzinfo=NY).astimezone(EC)


def resolve(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Modo de sesión actual, con su ventana y la fecha de negociación."""
    ref = _ec(now)
    day = ref.date()
    london_start = datetime.combine(day, LONDON_START, tzinfo=EC)
    ny_open = ny_open_in_ec(day)
    ny_close = ny_close_in_ec(day)

    if ref < london_start:
        mode, since, until = PRE_LONDON, None, london_start
    elif ref < ny_open:
        mode, since, until = LONDON_MONITOR, london_start, ny_open
    elif ref < ny_close:
        mode, since, until = NEW_YORK, ny_open, ny_close
    else:
        mode, since, until = POST_MARKET, ny_close, None

    return {
        "mode": mode,
        "trading_date": day.isoformat(),
        "now_ec": ref.isoformat(),
        "timezone": "America/Guayaquil",
        "session_since": since.isoformat() if since else None,
        "session_until": until.isoformat() if until else None,
        "london_start": london_start.isoformat(),
        "ny_open": ny_open.isoformat(),
        "ny_close": ny_close.isoformat(),
        "weekend": day.weekday() >= 5,
        # La sesión que hay que SELLAR al pasar a la siguiente.
        "closes_on_transition": LONDON_MONITOR if mode == NEW_YORK else None,
    }


def accumulate(symbol: str, *, price: Optional[float] = None,
               volume: Optional[float] = None,
               buy_premium: Optional[float] = None,
               sell_premium: Optional[float] = None,
               prints: Optional[int] = None,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """Suma una observación al acumulado de la sesión en curso.

    Cada magnitud es opcional y `None` NO cuenta: un ciclo sin volumen no
    significa volumen cero. El acumulado sólo crece con lo medido.
    """
    ses = resolve(now)
    mode = ses["mode"]
    if mode not in (LONDON_MONITOR, NEW_YORK):
        return {"accumulated": False, "reason": f"fuera de sesión ({mode})", **ses}

    key = (str(symbol or "").upper(), ses["trading_date"], mode)
    with _LOCK:
        acc = _ACC.get(key)
        if acc is None:
            acc = {"symbol": key[0], "trading_date": key[1], "session": mode,
                   "opened_at": ses["now_ec"], "open_price": price,
                   "high": price, "low": price, "last": price,
                   "volume": None, "buy_premium": None, "sell_premium": None,
                   "prints": None, "vwap_num": None, "vwap_den": None,
                   "observations": 0, "closed": False, "closed_at": None}
            _ACC[key] = acc

        if price is not None:
            acc["last"] = price
            if acc["open_price"] is None:
                acc["open_price"] = price
            acc["high"] = price if acc["high"] is None else max(acc["high"], price)
            acc["low"] = price if acc["low"] is None else min(acc["low"], price)
        for field, value in (("volume", volume), ("buy_premium", buy_premium),
                             ("sell_premium", sell_premium), ("prints", prints)):
            if value is None:
                continue
            acc[field] = value if acc[field] is None else acc[field] + value
        if price is not None and volume is not None and volume > 0:
            acc["vwap_num"] = (acc["vwap_num"] or 0.0) + price * volume
            acc["vwap_den"] = (acc["vwap_den"] or 0.0) + volume
        acc["observations"] += 1
        acc["updated_at"] = ses["now_ec"]
        return {"accumulated": True, **ses, "snapshot": _view(acc)}


def _view(acc: Dict[str, Any]) -> Dict[str, Any]:
    den = acc.get("vwap_den")
    vwap = (acc["vwap_num"] / den) if (den and acc.get("vwap_num") is not None) else None
    move = None
    if acc.get("open_price") is not None and acc.get("last") is not None:
        move = round(acc["last"] - acc["open_price"], 4)
    net = None
    if acc.get("buy_premium") is not None or acc.get("sell_premium") is not None:
        net = (acc.get("buy_premium") or 0.0) - (acc.get("sell_premium") or 0.0)
    return {
        "symbol": acc["symbol"], "trading_date": acc["trading_date"],
        "session": acc["session"], "opened_at": acc.get("opened_at"),
        "updated_at": acc.get("updated_at"), "closed": bool(acc.get("closed")),
        "closed_at": acc.get("closed_at"),
        "open": acc.get("open_price"), "high": acc.get("high"),
        "low": acc.get("low"), "last": acc.get("last"), "move": move,
        "vwap": vwap, "volume": acc.get("volume"),
        "buy_premium": acc.get("buy_premium"), "sell_premium": acc.get("sell_premium"),
        "net_premium": net, "prints": acc.get("prints"),
        "observations": acc.get("observations", 0),
    }


def snapshot(symbol: str, session: str, trading_date: Optional[str] = None,
             now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    day = trading_date or resolve(now)["trading_date"]
    with _LOCK:
        acc = _ACC.get((str(symbol or "").upper(), day, session))
        return _view(acc) if acc else None


def close_session(symbol: str, session: str, trading_date: Optional[str] = None,
                  now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Sella un acumulado. NO lo borra: se conserva para comparar."""
    ses = resolve(now)
    day = trading_date or ses["trading_date"]
    with _LOCK:
        acc = _ACC.get((str(symbol or "").upper(), day, session))
        if acc is None:
            return None
        if not acc.get("closed"):
            acc["closed"] = True
            acc["closed_at"] = ses["now_ec"]
        return _view(acc)


def both(symbol: str, trading_date: Optional[str] = None,
         now: Optional[datetime] = None) -> Dict[str, Any]:
    """Londres y Nueva York, lado a lado. Ninguna sustituye a la otra."""
    ses = resolve(now)
    day = trading_date or ses["trading_date"]
    return {
        "session": ses,
        "london": snapshot(symbol, LONDON_MONITOR, day, now),
        "new_york": snapshot(symbol, NEW_YORK, day, now),
    }


def reset() -> None:
    with _LOCK:
        _ACC.clear()
