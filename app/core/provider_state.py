"""Máquina de estados única de proveedores (v1.42.1).

EL BUG QUE CIERRA
-----------------
La terminal mostraba a la vez:

    ALPACA     LIVE
    QUANTDATA  LIVE
    ...y arriba:  «1/2 en vivo»

Dos afirmaciones contradictorias sobre el mismo hecho, en la misma pantalla. La
causa no era un error de cuenta: eran DOS definiciones distintas de «LIVE»
conviviendo. La insignia de cada fila decía una cosa (el proveedor está
configurado y responde) y el contador de arriba exigía otra (el proveedor tiene
dato fresco dentro de una ventana). Ambas razonables por separado; juntas,
indefendibles.

Un operador que ve esa contradicción deja de confiar en los dos indicadores, y con
razón: si el programa no sabe cuántos proveedores tiene vivos, ¿qué más no sabe?

LA SOLUCIÓN
-----------
Un solo vocabulario, calculado en un solo sitio. Las insignias, el contador, la
cobertura por canal y el Auditor leen EXACTAMENTE el mismo estado. No se puede
volver a discrepar porque ya no hay dos cálculos que puedan discrepar.

    DISABLED        fuera del roster de esta instalación. No es una carencia.
    NOT_CONFIGURED  en el roster, sin credenciales.
    CONNECTING      credenciales presentes, todavía sin primera respuesta.
    CONNECTED       responde, pero aún no ha entregado dato utilizable.
    LIVE            dato fresco dentro de la ventana.
    STALE           respondió, pero el último dato ya no sirve para decidir.
    MARKET_CLOSED   sano y en silencio porque no hay mercado. NO es un fallo.
    DEGRADED        entrega parte de lo suyo, con errores.
    UNAVAILABLE     configurado y sin servicio.

MARKET_CLOSED ES LA PIEZA QUE FALTABA
-------------------------------------
A la una de la madrugada, un proveedor sin ticks nuevos no está roto: el mercado
está cerrado. Contarlo como «no vivo» convertía cada noche en una falsa alarma, y
enseñaba a ignorar el indicador justo antes de la sesión en la que importa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from . import session_resolver

DISABLED = "DISABLED"
NOT_CONFIGURED = "NOT_CONFIGURED"
CONNECTING = "CONNECTING"
CONNECTED = "CONNECTED"
LIVE = "LIVE"
STALE = "STALE"
MARKET_CLOSED = "MARKET_CLOSED"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"

ORDER = (LIVE, CONNECTED, MARKET_CLOSED, STALE, DEGRADED, CONNECTING,
         UNAVAILABLE, NOT_CONFIGURED, DISABLED)

# Estados que cuentan como «el proveedor está haciendo su trabajo». MARKET_CLOSED
# entra aquí a propósito: un proveedor callado con el mercado cerrado está sano.
OPERATIONAL = frozenset({LIVE, CONNECTED, MARKET_CLOSED})

# Ventanas de frescura por tipo de canal, en segundos. Un tick de precio rancio a
# los 30 s ya no sirve para decidir; el interés abierto se publica una vez al día
# y a las seis horas sigue siendo el dato correcto.
FRESHNESS_S = {
    "PRICE": 45.0,
    "OPTION_FLOW": 180.0,
    "OPTION_CHAIN": 300.0,
    "EXPOSURE": 900.0,
    "OPEN_INTEREST": 86_400.0,
    "DEFAULT": 300.0,
}

_LABEL = {
    DISABLED: "fuera del roster de esta instalación",
    NOT_CONFIGURED: "sin credenciales configuradas",
    CONNECTING: "conectando; aún sin primera respuesta",
    CONNECTED: "responde, todavía sin dato utilizable",
    LIVE: "dato fresco",
    STALE: "el último dato ya no sirve para decidir",
    MARKET_CLOSED: "en silencio porque no hay mercado; no es un fallo",
    DEGRADED: "entrega parcial, con errores",
    UNAVAILABLE: "configurado y sin servicio",
}


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


@dataclass(frozen=True)
class ProviderState:
    provider: str
    state: str
    age_seconds: Optional[float]
    channel: str
    detail: str
    in_roster: bool
    configured: bool

    @property
    def operational(self) -> bool:
        return self.state in OPERATIONAL

    @property
    def counts_as_live(self) -> bool:
        """Lo que suma en «N/M en vivo». Un proveedor sano con el mercado cerrado
        cuenta: lo contrario convertía cada noche en una alarma falsa."""
        return self.state in OPERATIONAL

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.provider, "state": self.state, "status": self.state,
                "age_seconds": self.age_seconds, "channel": self.channel,
                "detail": self.detail, "label": _LABEL.get(self.state, ""),
                "operational": self.operational, "counts_as_live": self.counts_as_live,
                "in_roster": self.in_roster, "configured": self.configured}


def classify(provider: str, *, in_roster: bool, configured: bool,
             responding: bool = False, has_data: bool = False,
             age_seconds: Any = None, error: Any = None,
             channel: str = "DEFAULT", now: float | None = None,
             market_session: Dict[str, Any] | None = None,
             provider_signal: str | None = None) -> ProviderState:
    """El ÚNICO sitio donde se decide en qué estado está un proveedor.

    `provider_signal` es lo que el adaptador del propio proveedor concluyó sobre su
    salud. Cuando dice DEGRADED o STALE, esa evidencia es más específica que
    cualquier inferencia por antigüedad y manda: un stream que se sabe entrecortado
    no debe publicarse como LIVE sólo porque su último evento sea reciente.
    """
    name = str(provider or "").strip().upper()
    ch = str(channel or "DEFAULT").upper()
    age = _f(age_seconds)

    if not in_roster:
        return ProviderState(name, DISABLED, age, ch,
                             "Retirado del roster de esta instalación; no se le exige nada.",
                             in_roster, configured)
    if not configured:
        return ProviderState(name, NOT_CONFIGURED, age, ch,
                             "En el roster pero sin credenciales.", in_roster, configured)

    err = str(error or "").strip()
    if err and not has_data:
        return ProviderState(name, UNAVAILABLE, age, ch, err[:180], in_roster, configured)

    if not responding:
        return ProviderState(name, CONNECTING, age, ch,
                             "Credenciales presentes; esperando la primera respuesta.",
                             in_roster, configured)

    ses = market_session or session_resolver.resolve().describe()
    open_now = bool(ses.get("tradeable_now"))
    limit = FRESHNESS_S.get(ch, FRESHNESS_S["DEFAULT"])

    signal = str(provider_signal or "").upper()
    if err or signal == "DEGRADED":
        return ProviderState(name, DEGRADED, age, ch,
                             (f"Responde con datos parciales: {err[:150]}" if err else
                              "El adaptador del proveedor reporta entrega degradada."),
                             in_roster, configured)
    if signal == "STALE":
        return ProviderState(name, STALE, age, ch,
                             "El adaptador del proveedor reporta su dato como rancio.",
                             in_roster, configured)
    if signal == "CONFIGURED":
        # Configurado y respondiendo, pero su adaptador no afirma tener dato vivo.
        return ProviderState(name, CONNECTED, age, ch,
                             "Configurado y respondiendo; el adaptador no afirma dato vivo.",
                             in_roster, configured)

    if not has_data:
        if not open_now:
            return ProviderState(name, MARKET_CLOSED, age, ch,
                                 f"Sin datos nuevos porque el mercado está cerrado "
                                 f"({ses.get('phase')}). Es lo esperado.", in_roster, configured)
        return ProviderState(name, CONNECTED, age, ch,
                             "Conectado, pero todavía no ha entregado dato utilizable.",
                             in_roster, configured)

    if age is None:
        return ProviderState(name, CONNECTED, age, ch,
                             "Entrega datos pero no declara su antigüedad; no se puede "
                             "afirmar que estén frescos.", in_roster, configured)

    if age <= limit:
        return ProviderState(name, LIVE, age, ch,
                             f"Último dato hace {age:.0f}s (límite {limit:.0f}s).",
                             in_roster, configured)

    if not open_now:
        return ProviderState(name, MARKET_CLOSED, age, ch,
                             f"El último dato tiene {age:.0f}s, coherente con un mercado "
                             f"cerrado ({ses.get('phase')}).", in_roster, configured)

    return ProviderState(name, STALE, age, ch,
                         f"Último dato hace {age:.0f}s, por encima del límite de {limit:.0f}s "
                         "para este canal. Con el mercado abierto, eso sí es un problema.",
                         in_roster, configured)


def summarize(states: Iterable[ProviderState]) -> Dict[str, Any]:
    """El contador de la cabecera, derivado de los MISMOS estados que las insignias.

    Ésta es la garantía estructural: no existe una segunda definición de «vivo» que
    pueda discrepar, porque no existe un segundo cálculo.
    """
    rows = [s for s in states or []]
    in_roster = [s for s in rows if s.in_roster]
    counted = [s for s in in_roster if s.counts_as_live]
    ses = session_resolver.resolve()

    if not in_roster:
        headline = "sin proveedores en el roster"
    elif len(counted) == len(in_roster):
        headline = f"{len(counted)}/{len(in_roster)} operativos"
    else:
        headline = f"{len(counted)}/{len(in_roster)} operativos"

    return {
        "total": len(in_roster),
        "operational": len(counted),
        "headline": headline,
        "all_operational": bool(in_roster) and len(counted) == len(in_roster),
        "by_state": {st: sum(1 for s in in_roster if s.state == st)
                     for st in ORDER if any(s.state == st for s in in_roster)},
        "session": ses.describe(),
        "providers": [s.describe() for s in rows],
        "doctrine": ("Insignias y contador salen del mismo cálculo. Un proveedor sano "
                     "con el mercado cerrado cuenta como operativo: lo contrario "
                     "convertía cada noche en una alarma falsa."),
    }


def contract() -> Dict[str, Any]:
    return {"states": list(ORDER), "operational": sorted(OPERATIONAL),
            "freshness_seconds": dict(FRESHNESS_S), "labels": dict(_LABEL)}
