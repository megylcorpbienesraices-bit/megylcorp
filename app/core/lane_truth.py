"""LA VERDAD DE UN CARRIL · AUTORIDAD ÚNICA DE ESTADO · ITM QUANT v1.62.0

═══════════════════════════════════════════════════════════════════════════
TRES PANTALLAS, TRES VEREDICTOS, UNA SOLA EJECUCIÓN
═══════════════════════════════════════════════════════════════════════════

De las capturas LIVE, sobre el MISMO ciclo y el MISMO carril:

    Herramientas Quant Data      equity_prints   PROVIDER_ERROR (tardó >11 s)
    Diagnóstico de paneles       equity_prints   SIN DATOS · EL_PROVEEDOR_NO_DEVOLVIÓ_FILAS

    Dark Pool · estado por carril  dark_pool_levels   STALE · 382
    Pantalla del analista          dark_pool_levels   CON DATOS · 382

Las dos contradicciones son imposibles para una misma ejecución, y las dos
tienen la misma causa: **cada pantalla volvía a deducir el estado por su
cuenta**, desde el bloque publicado, con una regla distinta:

    lane_state()       miraba `lane_status` y la clasificación del hub
    _dark_pool_rows()  miraba `bool(rows)` y, si no había, inventaba el motivo
    la tabla           miraba `ready` y contaba filas

Un fallo de refresco conserva el bloque anterior —que es correcto: no se tira
el último dato bueno—, así que `ready` seguía en `True` y `rows` seguía trayendo
382. Quien mirara `bool(rows)` concluía CON DATOS. Quien mirara `lane_status`
concluía STALE. Ninguno de los dos mentía sobre lo que miraba; los dos miraban
la sombra del hecho en vez del hecho.

Y cuando no quedaba bloque anterior, el diagnóstico rellenaba el hueco con
`EL_PROVEEDOR_NO_DEVOLVIÓ_FILAS`, que es una causa INVENTADA: el proveedor no
devolvió nada porque la petición murió por plazo, no porque no tuviera filas.

═══════════════════════════════════════════════════════════════════════════
LO QUE HACE ESTE MÓDULO
═══════════════════════════════════════════════════════════════════════════

Un objeto canónico por carril, **escrito en el momento de la ejecución** —que
es el único instante en que se sabe qué pasó de verdad— y leído sin reinterpretar
por todas las pantallas, el Auditor y el diagnóstico.

Deducir el estado desde el bloque publicado es leer una huella. Este registro
es el pie.

    current_status      qué pasó en ESTA ejecución
    current_rows        filas que trajo ESTA ejecución
    serving             qué se está sirviendo: LIVE · LKG · NONE
    served_rows         cuántas filas ve el operador, vengan de donde vengan
    lkg_rows            filas del último ciclo que SÍ funcionó
    lkg_age             su edad en segundos
    last_success_at     cuándo fue
    refresh_error       el error de ESTE refresco, entero
    refresh_error_phase qué fase expiró: CONNECT · POOL · READ · WRITE
    as_of               instante de esta verdad
    request_id          identificador de ESTA llamada
    cycle_id            ciclo al que pertenece

Con `request_id` y `cycle_id`, dos pantallas que discrepan dejan de ser una
discusión: o leen la misma ejecución, o una de las dos está leyendo otra cosa y
se ve en el identificador.

**Ninguna pantalla puede recalcular ni reinterpretar estos estados.** Para eso
está `screen_label()`: una sola función que traduce la verdad a lo que se
imprime, y todas la usan.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

CONTRACT = "ITMQ_LANE_TRUTH_V1"

# ── qué pasó en la EJECUCIÓN ────────────────────────────────────────────────
LIVE = "LIVE"                       # respuesta válida y filas > 0
NO_DATA = "NO_DATA"                 # respuesta válida y CERO filas: no es avería
PROVIDER_ERROR = "PROVIDER_ERROR"   # timeout, 5xx, red: no se sabe si hay filas
REQUEST_INVALID = "REQUEST_INVALID" # 400: el cuerpo está mal. No se arregla esperando
MISSING_TOOL = "MISSING_TOOL"       # 404: la herramienta no existe aquí
PARSER_ERROR = "PARSER_ERROR"       # respondió y no supimos leerlo

#: Y los estados de ESPERA, que NO son fallos. Una herramienta que todavía no
#: ha corrido no tiene resultado, y fingirle uno —`NO_DATA`— es afirmar algo
#: del proveedor que nadie ha comprobado.
WAITING_SCHEDULED = "WAITING_SCHEDULED"
WAITING_RATE_LIMIT = "WAITING_RATE_LIMIT"
WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
WAITING_COOLDOWN = "WAITING_COOLDOWN"
RUNNING = "RUNNING"

WAITING_STATES = frozenset({WAITING_SCHEDULED, WAITING_RATE_LIMIT,
                            WAITING_DEPENDENCY, WAITING_COOLDOWN, RUNNING})
FAILURE_STATES = frozenset({PROVIDER_ERROR, REQUEST_INVALID, MISSING_TOOL,
                            PARSER_ERROR})
EXECUTED_STATES = frozenset({LIVE, NO_DATA}) | FAILURE_STATES

# ── qué se está SIRVIENDO ───────────────────────────────────────────────────
SERVING_LIVE = "LIVE"    # dato de este ciclo
SERVING_LKG = "LKG"      # último valor bueno, con su edad declarada
SERVING_NONE = "NONE"    # no hay nada que enseñar

#: Etiqueta que ve el operador. UNA por estado, y la misma en todas partes.
#: Que una pantalla dijera «CON DATOS» y otra «STALE» sobre la misma ejecución
#: es exactamente lo que este diccionario impide.
SCREEN = {
    LIVE: "EN VIVO",
    NO_DATA: "SIN ACTIVIDAD",
    PROVIDER_ERROR: "PROVEEDOR CAÍDO",
    REQUEST_INVALID: "PETICIÓN RECHAZADA",
    MISSING_TOOL: "HERRAMIENTA INEXISTENTE",
    PARSER_ERROR: "RESPUESTA ILEGIBLE",
    WAITING_SCHEDULED: "EN COLA",
    WAITING_RATE_LIMIT: "ESPERANDO CUOTA",
    WAITING_DEPENDENCY: "ESPERANDO UN DATO",
    WAITING_COOLDOWN: "EN ENFRIAMIENTO",
    RUNNING: "LLAMANDO AHORA",
}

#: Y la etiqueta cuando se sirve el último valor bueno. No se puede llamar «EN
#: VIVO» a un dato de hace cuarenta segundos por mucho que haya filas.
SCREEN_LKG = "DATO ANTERIOR"

#: Máximo que una herramienta puede permanecer esperando sin que sea anomalía.
#: Esperar no es fallar, pero esperar para siempre sí es un defecto.
MAX_WAITING_S = 180.0

_CONTADOR = itertools.count(1)
_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  tz=timezone.utc).isoformat()


def next_request_id(tool: str) -> str:
    """Identificador de UNA llamada. Es lo que permite demostrar que dos
    pantallas están mirando la misma ejecución, o que no."""
    with _LOCK:
        n = next(_CONTADOR)
    return f"{str(tool or 'tool')}#{n}"


@dataclass(frozen=True)
class LaneTruth:
    """La verdad de un carril en un instante, escrita por quien la vivió."""

    tool: str
    symbol: str = ""
    current_status: str = WAITING_SCHEDULED
    current_rows: int = 0
    serving: str = SERVING_NONE
    served_rows: int = 0
    lkg_rows: int = 0
    lkg_age: Optional[float] = None
    last_success_at: Optional[str] = None
    refresh_error: str = ""
    refresh_error_phase: str = ""
    as_of: str = ""
    as_of_ts: float = 0.0
    request_id: str = ""
    cycle_id: str = ""
    #: Lo que separa esperar de fallar, publicado SIEMPRE que se espera.
    waiting_since: Optional[str] = None
    waiting_seconds: Optional[float] = None
    waiting_reason: str = ""
    next_eligible_at: Optional[str] = None
    #: Dónde se fue el tiempo de la llamada que falló.
    timing: Dict[str, Any] = field(default_factory=dict)
    endpoint: str = ""
    rejected_fields: tuple = ()

    # ── lecturas derivadas, calculadas AQUÍ y en ningún otro sitio ──────────

    @property
    def is_failure(self) -> bool:
        return self.current_status in FAILURE_STATES

    @property
    def is_waiting(self) -> bool:
        return self.current_status in WAITING_STATES

    @property
    def executed(self) -> bool:
        return self.current_status in EXECUTED_STATES

    @property
    def screen(self) -> str:
        """Lo que se imprime. Una sola vez, para todas las pantallas."""
        if self.serving == SERVING_LKG:
            edad = "" if self.lkg_age is None else f" · {float(self.lkg_age):.0f}s"
            return f"{SCREEN_LKG}{edad}"
        return SCREEN.get(self.current_status, self.current_status)

    @property
    def fresh(self) -> bool:
        """¿Lo que se ve es de ESTE ciclo? Nunca se deduce contando filas."""
        return self.serving == SERVING_LIVE

    def anomaly(self, *, max_waiting_s: float = MAX_WAITING_S) -> Optional[Dict[str, Any]]:
        """Esperar no es fallar. Esperar sin fin sí es un defecto.

        No cambia la severidad de nada ni convierte la espera en error: añade
        una anomalía APARTE, que es la forma de decir «esto lleva demasiado»
        sin mentir sobre lo que está pasando.
        """
        if not self.is_waiting:
            return None
        esperado = float(self.waiting_seconds or 0.0)
        if esperado < max(0.0, float(max_waiting_s)):
            return None
        return {
            "anomaly": "ESPERA_EXCESIVA",
            "tool": self.tool,
            "state": self.current_status,
            "waiting_seconds": round(esperado, 1),
            "limit_seconds": float(max_waiting_s),
            "reason": self.waiting_reason,
            "next_eligible_at": self.next_eligible_at,
            "detail": (f"lleva {esperado:.0f} s en {self.current_status} "
                       f"—el máximo declarado son {float(max_waiting_s):.0f} s—: "
                       f"esperar es legítimo, esperar sin fin es un defecto"),
        }

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.update({
            "contract": CONTRACT,
            "known": True,
            "is_failure": self.is_failure,
            "is_waiting": self.is_waiting,
            "executed": self.executed,
            "screen": self.screen,
            "fresh": self.fresh,
            "anomaly": self.anomaly(),
        })
        return d


def executed(tool: str, *, symbol: str = "", rows: int = 0,
             status: str = LIVE, error: str = "", phase: str = "",
             lkg_rows: int = 0, lkg_age: Optional[float] = None,
             last_success_at: Optional[str] = None,
             request_id: str = "", cycle_id: str = "",
             timing: Optional[Mapping[str, Any]] = None,
             endpoint: str = "", rejected_fields: tuple = ()) -> LaneTruth:
    """La verdad de una llamada que SÍ se ejecutó.

    Las cuatro reglas, y no hay una quinta:

        respuesta válida + filas > 0   → LIVE, sirviendo LIVE
        respuesta válida + 0 filas     → NO_DATA, sin nada que servir
        fallo + LKG válido             → PROVIDER_ERROR, sirviendo LKG
        fallo + sin LKG                → PROVIDER_ERROR, sin nada que servir

    El estado de la EJECUCIÓN y lo que se SIRVE son dos hechos distintos y se
    publican por separado: un refresco fallido no borra el dato anterior, y
    tampoco lo convierte en dato de este ciclo.
    """
    filas = max(0, int(rows or 0))
    filas_lkg = max(0, int(lkg_rows or 0))
    st = str(status or LIVE).upper()
    if st not in EXECUTED_STATES:
        st = PROVIDER_ERROR if error else (LIVE if filas else NO_DATA)
    elif st == LIVE and filas <= 0:
        # Nadie puede declarar LIVE sin filas: LIVE significa que llegó dato.
        st = NO_DATA

    fallo = st in FAILURE_STATES
    if fallo and filas_lkg > 0:
        serving, servidas = SERVING_LKG, filas_lkg
    elif not fallo and filas > 0:
        serving, servidas = SERVING_LIVE, filas
    else:
        serving, servidas = SERVING_NONE, 0

    ahora = _now()
    return LaneTruth(
        tool=str(tool), symbol=str(symbol or "").upper(),
        current_status=st,
        current_rows=(0 if fallo else filas),
        serving=serving, served_rows=servidas,
        lkg_rows=filas_lkg,
        lkg_age=(None if lkg_age is None else round(float(lkg_age), 2)),
        last_success_at=last_success_at,
        refresh_error=str(error or "")[:400],
        refresh_error_phase=str(phase or ""),
        as_of=_iso(ahora), as_of_ts=ahora,
        request_id=str(request_id or ""), cycle_id=str(cycle_id or ""),
        timing=dict(timing or {}), endpoint=str(endpoint or ""),
        rejected_fields=tuple(rejected_fields or ()),
    )


def waiting(tool: str, *, symbol: str = "", state: str = WAITING_SCHEDULED,
            reason: str = "", waiting_since: Optional[float] = None,
            next_eligible_at: Optional[float] = None,
            lkg_rows: int = 0, lkg_age: Optional[float] = None,
            last_success_at: Optional[str] = None,
            cycle_id: str = "") -> LaneTruth:
    """La verdad de un carril que TODAVÍA NO se ha ejecutado.

    Esto no es un fallo y no se puede presentar como tal. Pero tampoco es
    `NO_DATA`: decir «el proveedor no devolvió filas» de una petición que nunca
    se hizo atribuye al proveedor un silencio que es nuestro.

    Mientras espera, si hay último valor bueno se sigue sirviendo —con su edad—,
    porque una cadencia que aún no vence no es razón para vaciar la pantalla.
    """
    st = str(state or WAITING_SCHEDULED).upper()
    if st not in WAITING_STATES:
        st = WAITING_SCHEDULED
    ahora = _now()
    esperando = None if waiting_since is None else max(0.0, ahora - float(waiting_since))
    filas_lkg = max(0, int(lkg_rows or 0))
    return LaneTruth(
        tool=str(tool), symbol=str(symbol or "").upper(),
        current_status=st, current_rows=0,
        serving=(SERVING_LKG if filas_lkg > 0 else SERVING_NONE),
        served_rows=filas_lkg,
        lkg_rows=filas_lkg,
        lkg_age=(None if lkg_age is None else round(float(lkg_age), 2)),
        last_success_at=last_success_at,
        as_of=_iso(ahora), as_of_ts=ahora, cycle_id=str(cycle_id or ""),
        waiting_since=(None if waiting_since is None else _iso(float(waiting_since))),
        waiting_seconds=(None if esperando is None else round(esperando, 1)),
        waiting_reason=str(reason or ""),
        next_eligible_at=(None if next_eligible_at is None
                          else _iso(float(next_eligible_at))),
    )


class LaneTruthStore:
    """Donde vive la verdad de cada carril. UNA por (herramienta, activo).

    No es una caché: es el registro de lo que pasó. Quien quiera saber el estado
    de un carril lo LEE de aquí; nadie lo vuelve a deducir.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._por_clave: Dict[str, LaneTruth] = {}

    def put(self, truth: LaneTruth) -> LaneTruth:
        with self._lock:
            self._por_clave[str(truth.tool)] = truth
        return truth

    def get(self, tool: str) -> Optional[LaneTruth]:
        with self._lock:
            return self._por_clave.get(str(tool))

    def read(self, tool: str) -> Dict[str, Any]:
        """La verdad publicada, o una declaración de que NO HAY NINGUNA.

        `known` es la diferencia entre «sé que está esperando» y «nadie ha
        escrito nada sobre este carril», y no se pueden confundir: afirmar una
        espera que nadie midió es el mismo pecado que afirmar un vacío que nadie
        midió. Quien lee esto con `known == False` debe caer a lo que sepa por
        su cuenta, no tomar este estado por bueno.
        """
        t = self.get(tool)
        if t is not None:
            return {**t.as_dict(), "known": True}
        sin_registro = waiting(
            tool, reason="ningún ciclo ha escrito todavía la verdad de este carril")
        return {**sin_registro.as_dict(), "known": False}

    def clear_symbol(self, symbol: str) -> int:
        sym = str(symbol or "").upper()
        with self._lock:
            claves = [k for k, v in self._por_clave.items() if v.symbol == sym]
            for k in claves:
                self._por_clave.pop(k, None)
        return len(claves)

    def reset(self) -> None:
        with self._lock:
            self._por_clave.clear()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            filas = [v.as_dict() for v in self._por_clave.values()]
        filas.sort(key=lambda r: r["tool"])
        anomalias = [r["anomaly"] for r in filas if r.get("anomaly")]
        return {
            "contract": CONTRACT,
            "lanes": filas,
            "anomalies": anomalias,
            "max_waiting_s": MAX_WAITING_S,
            "policy": ("el estado de un carril se ESCRIBE en la ejecución y se LEE "
                       "sin reinterpretar: ninguna pantalla puede recalcularlo"),
        }


TRUTH = LaneTruthStore()


def screen_label(truth: Any) -> str:
    """Lo que se imprime, para cualquier consumidor. Una sola traducción.

    Acepta el objeto o su diccionario, porque las pantallas leen el JSON
    publicado y el motor lee el objeto: los dos tienen que decir lo mismo.
    """
    if isinstance(truth, LaneTruth):
        return truth.screen
    d = dict(truth or {})
    if str(d.get("serving") or "") == SERVING_LKG:
        edad = d.get("lkg_age")
        return f"{SCREEN_LKG}" + ("" if edad is None else f" · {float(edad):.0f}s")
    return SCREEN.get(str(d.get("current_status") or ""), str(d.get("current_status") or ""))


def same_semantics(a: Any, b: Any) -> bool:
    """¿Dos vistas dicen lo MISMO del mismo carril?

    Es la comprobación que la regresión de consistencia aplica entre el Auditor,
    el diagnóstico, el estado por carril y la pantalla del analista. No compara
    textos: compara el hecho —qué pasó y qué se sirve—.
    """
    da = a.as_dict() if isinstance(a, LaneTruth) else dict(a or {})
    db = b.as_dict() if isinstance(b, LaneTruth) else dict(b or {})
    return (str(da.get("current_status")) == str(db.get("current_status"))
            and str(da.get("serving")) == str(db.get("serving"))
            and int(da.get("served_rows") or 0) == int(db.get("served_rows") or 0))


__all__ = [
    "TRUTH", "LaneTruth", "LaneTruthStore", "executed", "waiting",
    "screen_label", "same_semantics", "next_request_id", "CONTRACT",
    "LIVE", "NO_DATA", "PROVIDER_ERROR", "REQUEST_INVALID", "MISSING_TOOL",
    "PARSER_ERROR", "WAITING_SCHEDULED", "WAITING_RATE_LIMIT",
    "WAITING_DEPENDENCY", "WAITING_COOLDOWN", "RUNNING",
    "WAITING_STATES", "FAILURE_STATES", "EXECUTED_STATES",
    "SERVING_LIVE", "SERVING_LKG", "SERVING_NONE", "SCREEN", "SCREEN_LKG",
    "MAX_WAITING_S",
]
