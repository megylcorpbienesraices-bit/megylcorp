"""Resolución única de sesión de mercado (v1.42.1).

LA CAUSA RAÍZ QUE CIERRA
------------------------
A la 1 de la madrugada del 18 de septiembre, ITM QUANT pedía datos de la sesión
del 18 de septiembre. Esa sesión todavía no existía. El proveedor respondía
correctamente —200, cero filas— y el programa lo mostraba como `SIN DATOS`, que
se lee igual que una avería.

De ahí venían, a la vez:

    TRACE · velas        sip:NO_BARS · iex:NO_BARS
    stock-price-over-time  SIN_DATOS
    equity-prints          SIN_DATOS
    exposure-by-*          SIN_DATOS

No eran cuatro fallos. Era uno: preguntar por una sesión que aún no ha ocurrido.

LA REGLA
--------
Ningún módulo vuelve a construir una fecha de sesión por su cuenta. Se pide aquí,
y aquí se responde con la sesión que de verdad tiene datos:

    ¿ya empezó la sesión de hoy?  → sesión de hoy
    ¿todavía no?                  → última sesión COMPLETADA

Y se devuelve, además, el motivo: `MARKET_CLOSED` no es `NO_DATA`, y
`SESSION_NOT_STARTED` tampoco. Esa diferencia es la que evita que el analista
busque una avería donde sólo hay un mercado cerrado.

QUÉ NO HACE
-----------
No decide qué se pide ni con qué cadencia. Sólo responde a «¿qué sesión es la
buena ahora mismo?», que es una pregunta de calendario, no de estrategia.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo

from .backtest_calendar import market_holidays, trading_days

NY = ZoneInfo("America/New_York")

# Horario del mercado de acciones y opciones de EE. UU.
PREMARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
# OPRA cierra a las 16:00 para equity/ETF; los índices siguen hasta 16:15.
OPTIONS_INDEX_CLOSE = time(16, 15)
AFTERHOURS_CLOSE = time(20, 0)

# Media sesión: el mercado cierra a las 13:00. Son fechas fijas conocidas y se
# tratan aparte porque pedir datos de 15:30 de un día de media sesión devuelve
# vacío legítimamente.
def _half_days(year: int) -> set[date]:
    out: set[date] = set()
    # Víspera de Independencia, día después de Acción de Gracias y Nochebuena.
    for d in (date(year, 7, 3), date(year, 12, 24)):
        if d.weekday() < 5:
            out.add(d)
    # Viernes posterior a Acción de Gracias (4.º jueves de noviembre).
    nov1 = date(year, 11, 1)
    thursday = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    out.add(thursday + timedelta(days=22))
    return out


# ── fases ───────────────────────────────────────────────────────────────────────
PHASE_CLOSED = "MARKET_CLOSED"
PHASE_PREMARKET = "PREMARKET"
PHASE_REGULAR = "REGULAR"
PHASE_AFTERHOURS = "AFTERHOURS"
PHASE_HOLIDAY = "HOLIDAY"
PHASE_WEEKEND = "WEEKEND"

# ── motivos publicables ─────────────────────────────────────────────────────────
REASON_SESSION_NOT_STARTED = "SESSION_NOT_STARTED"
REASON_MARKET_CLOSED = "MARKET_CLOSED"
REASON_WAITING_FOR_PRINTS = "WAITING_FOR_PRINTS"
REASON_LIVE = "LIVE"


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in market_holidays(day.year)


def previous_trading_day(day: date) -> date:
    d = day - timedelta(days=1)
    for _ in range(12):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


def close_time(day: date, *, index: bool = False) -> time:
    if day in _half_days(day.year):
        return time(13, 0)
    return OPTIONS_INDEX_CLOSE if index else REGULAR_CLOSE


@dataclass(frozen=True)
class MarketSession:
    """Qué sesión usar, en qué fase estamos y por qué puede no haber datos."""

    session_date: date           # la sesión cuyos datos hay que pedir
    is_current: bool             # True si es la de hoy y ya ha empezado
    phase: str
    reason: str
    now_ny: datetime
    last_completed: date
    note: str = ""

    @property
    def iso(self) -> str:
        return self.session_date.isoformat()

    @property
    def tradeable_now(self) -> bool:
        return self.phase == PHASE_REGULAR

    def describe(self) -> Dict[str, Any]:
        return {
            "session_date": self.iso,
            "is_current_session": self.is_current,
            "phase": self.phase,
            "reason": self.reason,
            "now_ny": self.now_ny.isoformat(timespec="seconds"),
            "last_completed_session": self.last_completed.isoformat(),
            "tradeable_now": self.tradeable_now,
            "note": self.note,
        }


def _phase(now: datetime) -> str:
    day = now.date()
    if day.weekday() >= 5:
        return PHASE_WEEKEND
    if day in market_holidays(day.year):
        return PHASE_HOLIDAY
    t = now.time()
    if t < PREMARKET_OPEN:
        return PHASE_CLOSED
    if t < REGULAR_OPEN:
        return PHASE_PREMARKET
    if t < close_time(day):
        return PHASE_REGULAR
    if t < AFTERHOURS_CLOSE:
        return PHASE_AFTERHOURS
    return PHASE_CLOSED


def resolve(now: datetime | None = None, *, require_completed: bool = False) -> MarketSession:
    """La sesión que de verdad tiene datos ahora mismo.

    `require_completed=True` fuerza la última sesión CERRADA, que es lo que quiere
    un módulo que necesita interés abierto o estadísticas de cierre: durante la
    sesión en curso esas cifras todavía no son definitivas.
    """
    ref = (now or datetime.now(NY))
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=NY)
    else:
        ref = ref.astimezone(NY)

    today = ref.date()
    phase = _phase(ref)
    last_completed = today if (is_trading_day(today) and ref.time() >= close_time(today)) \
        else previous_trading_day(today)

    if require_completed:
        return MarketSession(
            last_completed, False, phase, REASON_MARKET_CLOSED, ref, last_completed,
            "Se pidió explícitamente la última sesión cerrada: las cifras de cierre "
            "no son definitivas hasta que la sesión termina.")

    if phase in (PHASE_WEEKEND, PHASE_HOLIDAY):
        note = ("Fin de semana." if phase == PHASE_WEEKEND else "Festivo de mercado.")
        return MarketSession(last_completed, False, phase, REASON_MARKET_CLOSED, ref,
                             last_completed, note + " Se usa la última sesión completada.")

    if phase == PHASE_CLOSED and ref.time() < PREMARKET_OPEN:
        # Madrugada: la sesión de HOY todavía no ha empezado. Éste es el caso que
        # dejaba TRACE sin velas y cuatro herramientas en SIN_DATOS.
        return MarketSession(
            last_completed, False, phase, REASON_SESSION_NOT_STARTED, ref, last_completed,
            f"La sesión del {today.isoformat()} aún no ha comenzado; se usa la del "
            f"{last_completed.isoformat()}, que sí tiene datos.")

    if phase == PHASE_CLOSED:
        return MarketSession(today if is_trading_day(today) else last_completed, False,
                             phase, REASON_MARKET_CLOSED, ref, last_completed,
                             "Sesión terminada. Los datos publicados son los del cierre.")

    if phase == PHASE_PREMARKET:
        return MarketSession(
            today, False, phase, REASON_WAITING_FOR_PRINTS, ref, last_completed,
            "Premarket: hay cinta de equity, pero las opciones aún no imprimen. "
            "La estructura se apoya en el cierre anterior hasta la apertura.")

    if phase == PHASE_AFTERHOURS:
        return MarketSession(today, True, phase, REASON_MARKET_CLOSED, ref, last_completed,
                             "After-hours: la sesión regular ya cerró.")

    return MarketSession(today, True, phase, REASON_LIVE, ref, last_completed,
                         "Sesión regular en curso.")


def bootstrap_window(now: datetime | None = None, *, sessions: int = 3) -> Dict[str, Any]:
    """Ventana histórica para arrancar un gráfico SIN depender de la sesión de hoy.

    TRACE no puede quedarse en blanco porque el mercado esté cerrado. Se piden las
    últimas `sessions` sesiones completadas y con eso ya hay velas que dibujar; el
    stream LIVE continúa a partir de ahí cuando abra.
    """
    s = resolve(now)
    end = s.session_date
    start = end
    for _ in range(max(int(sessions), 1) - 1):
        start = previous_trading_day(start)
    days = trading_days(start, end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": [d.isoformat() for d in days],
        "session": s.describe(),
        "note": ("Ventana de arranque: son sesiones ya completadas, así que el gráfico "
                 "tiene velas desde el primer instante aunque el mercado esté cerrado."),
    }


def empty_reason(now: datetime | None = None, *, channel: str = "DATOS",
                 kind: str = "OPTION") -> Dict[str, Any]:
    """Por qué un panel está vacío, distinguiendo cerrado de averiado.

    `kind` importa más de lo que parece. En premarket la cinta de EQUITY sí imprime
    —hay velas, hay prints— mientras las OPCIONES todavía no. Decir «premarket, las
    opciones no imprimen» sobre un gráfico de velas sería falso, y una explicación
    falsa es peor que ninguna: manda al analista a buscar donde no hay nada.
    """
    s = resolve(now)
    is_equity = str(kind).upper().startswith("EQ")
    if s.phase in (PHASE_WEEKEND, PHASE_HOLIDAY):
        state, text = REASON_MARKET_CLOSED, f"{channel}: mercado cerrado ({s.phase.lower()})."
    elif s.reason == REASON_SESSION_NOT_STARTED:
        state, text = REASON_SESSION_NOT_STARTED, (
            f"{channel}: la sesión de hoy aún no ha comenzado. No es un fallo: "
            f"se muestra la del {s.last_completed.isoformat()}.")
    elif s.phase == PHASE_PREMARKET:
        if is_equity:
            state, text = "NO_DATA", (
                f"{channel}: es premarket y la cinta de equity SÍ imprime a esta hora, "
                "así que un panel vacío aquí sí requiere revisión.")
        else:
            state, text = REASON_WAITING_FOR_PRINTS, (
                f"{channel}: premarket. Las opciones todavía no imprimen.")
    elif s.phase in (PHASE_AFTERHOURS, PHASE_CLOSED):
        state, text = REASON_MARKET_CLOSED, f"{channel}: sesión regular cerrada."
    else:
        state, text = "NO_DATA", (
            f"{channel}: el mercado está abierto y aun así no llegan datos. "
            "Esto sí requiere revisión.")
    return {"state": state, "detail": text, "session": s.describe(),
            "is_failure": state == "NO_DATA"}
