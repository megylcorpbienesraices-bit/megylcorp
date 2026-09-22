"""ESTADOS DEL PROGRAMADOR · ITM QUANT v1.59.0

═══════════════════════════════════════════════════════════════════════════
«AÚN SIN TURNO» NO ES UN ESTADO
═══════════════════════════════════════════════════════════════════════════

Una herramienta que no está sirviendo se resolvía en un cajón genérico: «sin
intentos», «ciclo no iniciado». Eso junta cosas que no se parecen en nada y que
se arreglan de maneras opuestas:

    · su cadencia todavía no vence          → no pasa nada, es el ritmo
    · la cuota está llena                   → hay que esperar segundos
    · le falta un dato del que depende      → hay que arreglar OTRA cosa
    · viene de fallar y está en enfriamiento→ hay que esperar el backoff
    · está llamando ahora mismo             → hay que esperar milisegundos
    · lleva exigible diez minutos sin que
      nadie la llame                        → ESO es una anomalía del programador

El último caso es el único que es un defecto, y era el que quedaba escondido
entre los otros cinco. Con un solo cajón, un fallo real del programador se lee
igual que una herramienta que simplemente no tocaba todavía.

═══════════════════════════════════════════════════════════════════════════
LOS NUEVE ESTADOS
═══════════════════════════════════════════════════════════════════════════

    SCHEDULED            declarada, esperando a que venza su cadencia
    WAITING_RATE_LIMIT   exigible, pero el presupuesto de cuota está agotado
    WAITING_DEPENDENCY   exigible, pero le falta un dato del que depende
    COOLDOWN             en enfriamiento tras un fallo, con su backoff
    RUNNING              petición en vuelo AHORA
    LIVE                 sirviendo dato fresco
    NO_DATA              respondió bien y no hay datos (mercado cerrado, p. ej.)
    STALE                sirve su último valor bueno, con la edad declarada
    PROVIDER_ERROR       el proveedor falla y no hay valor bueno que servir

Y una anomalía aparte: `NUNCA_LLAMADA`, para la herramienta exigible que pasó
el calentamiento sin un solo intento. No es un estado: es un defecto, y se
nombra como tal.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

SCHEDULED = "SCHEDULED"
WAITING_RATE_LIMIT = "WAITING_RATE_LIMIT"
WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
COOLDOWN = "COOLDOWN"
RUNNING = "RUNNING"
LIVE = "LIVE"
NO_DATA = "NO_DATA"
STALE = "STALE"
PROVIDER_ERROR = "PROVIDER_ERROR"

STATES = (SCHEDULED, WAITING_RATE_LIMIT, WAITING_DEPENDENCY, COOLDOWN,
          RUNNING, LIVE, NO_DATA, STALE, PROVIDER_ERROR)

#: Estados en los que la herramienta está entregando algo utilizable.
SERVING = frozenset({LIVE, STALE})

#: Estados de espera: no son fallos y no deben pintarse como tales.
WAITING = frozenset({SCHEDULED, WAITING_RATE_LIMIT, WAITING_DEPENDENCY,
                     COOLDOWN, RUNNING})

#: Cuánto se le concede a una herramienta exigible antes de considerar que el
#: programador la está ignorando. Tres ciclos de cadencia rápida: por debajo de
#: eso, un turno perdido es normal; por encima, alguien no la está llamando.
WARMUP_S = 60.0

#: Y cuánto tiempo exigible sin un solo intento se considera anomalía dura.
NUNCA_LLAMADA_S = 180.0

ANOMALY_NEVER_CALLED = "NUNCA_LLAMADA"

_EXPLICACION = {
    SCHEDULED: "su cadencia aún no vence; no es un fallo, es el ritmo declarado",
    WAITING_RATE_LIMIT: "exigible, pero el presupuesto de cuota está agotado ahora",
    WAITING_DEPENDENCY: "exigible, pero le falta un dato del que depende",
    COOLDOWN: "en enfriamiento tras un fallo: el programador no la llamará hasta que venza",
    RUNNING: "petición en vuelo ahora mismo",
    LIVE: "sirviendo dato fresco del proveedor",
    NO_DATA: "el proveedor respondió bien y no hay datos para esta ventana",
    STALE: "sirve su último valor bueno; la edad va declarada",
    PROVIDER_ERROR: "el proveedor falla y no hay último valor bueno que servir",
}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def classify(*, due: Optional[bool], in_flight: bool = False,
             cooldown_seconds: Any = 0.0,
             quota_paused: Optional[Mapping[str, Any]] = None,
             missing_dependencies: Optional[List[str]] = None,
             rows: Any = 0, provider_status: str = "",
             lkg_rows: Any = 0, lkg_age_seconds: Any = None,
             fresh: Optional[bool] = None,
             ever_attempted: bool = True) -> Dict[str, Any]:
    """El estado REAL de una herramienta, con su causa.

    El orden de las comprobaciones importa y no es arbitrario: se pregunta
    primero por lo que está pasando AHORA (una petición en vuelo), después por
    lo que impide llamar (enfriamiento, cuota, dependencia), y sólo al final por
    el resultado de la última llamada. Al revés, una herramienta que está
    llamando ahora mismo se declararía con el estado de su intento anterior.
    """
    dependencias = [str(d) for d in (missing_dependencies or []) if str(d)]
    enfriando = _f(cooldown_seconds)
    filas = int(_f(rows))
    filas_lkg = int(_f(lkg_rows))
    status = str(provider_status or "").upper()

    def salida(estado: str, detalle: str = "", **extra: Any) -> Dict[str, Any]:
        return {
            "state": estado,
            "detail": detalle or _EXPLICACION.get(estado, ""),
            "serving": estado in SERVING,
            "waiting": estado in WAITING,
            "is_failure": estado == PROVIDER_ERROR,
            **extra,
        }

    if in_flight:
        return salida(RUNNING)
    if enfriando > 0:
        return salida(COOLDOWN,
                      f"en enfriamiento {enfriando:.0f} s tras un fallo anterior",
                      cooldown_seconds=round(enfriando, 2))
    if dependencias:
        return salida(WAITING_DEPENDENCY,
                      "le falta " + ", ".join(dependencias),
                      missing=dependencias)
    pausa = dict(quota_paused or {})
    if pausa and due:
        segundos = _f(pausa.get("seconds"))
        return salida(WAITING_RATE_LIMIT,
                      f"{pausa.get('reason') or 'cuota agotada'}"
                      + (f"; se repone en {segundos:.0f} s" if segundos else ""),
                      quota=pausa)
    if due is False:
        return salida(SCHEDULED)
    if not ever_attempted and filas <= 0 and filas_lkg <= 0 and not status:
        # Exigible y sin un solo intento: NO es «respondió y no hay datos». Nadie
        # ha preguntado todavía. Decir NO_DATA aquí atribuye al proveedor un
        # silencio que es nuestro, y es justo el error que este bloque quita.
        return salida(SCHEDULED,
                      "declarada y exigible, aún sin turno: el presupuesto del "
                      "ciclo no ha llegado a ella",
                      never_attempted=True)

    # Ya se llamó: manda el resultado.
    if status in ("PROVIDER_ERROR", "TRANSIENT", "MISSING_TOOL", "TIMEOUT"):
        if filas_lkg > 0:
            # v1.59.0 · UN FALLO ACTUAL NO BORRA EL ÚLTIMO DATO BUENO.
            return salida(STALE,
                          f"el refresco falló ({status}); se sirven {filas_lkg} "
                          f"filas del último ciclo bueno",
                          rows=filas_lkg,
                          age_seconds=(None if lkg_age_seconds is None
                                       else round(_f(lkg_age_seconds), 2)))
        return salida(PROVIDER_ERROR, f"el proveedor falla ({status}) y no hay "
                                      f"último valor bueno que servir")
    if filas > 0 and (fresh is not False):
        return salida(LIVE, rows=filas)
    if filas > 0:
        return salida(STALE, f"{filas} filas, pero el dato ya no es fresco",
                      rows=filas,
                      age_seconds=(None if lkg_age_seconds is None
                                   else round(_f(lkg_age_seconds), 2)))
    if filas_lkg > 0:
        return salida(STALE, f"sin filas nuevas; se sirven {filas_lkg} del "
                             f"último ciclo bueno", rows=filas_lkg,
                      age_seconds=(None if lkg_age_seconds is None
                                   else round(_f(lkg_age_seconds), 2)))
    return salida(NO_DATA)


def anomaly(*, state: str, due: Optional[bool], attempts: int,
            eligible_seconds: Any, quota_paused: Optional[Mapping[str, Any]] = None,
            warmup_seconds: float = WARMUP_S) -> Optional[Dict[str, Any]]:
    """¿Es esto un defecto del programador, y no una espera legítima?

    Una herramienta EXIGIBLE que pasó el calentamiento sin un solo intento no
    está esperando: nadie la está llamando. Es el defecto que el cajón genérico
    escondía —26 de 36 herramientas sin un intento en 200 ciclos—, y por eso
    tiene que salir como ANOMALÍA y no como estado.

    No lo es cuando la cuota está agotada (eso es una espera con causa) ni
    cuando su cadencia no vence (eso es el ritmo).
    """
    esperado = _f(eligible_seconds)
    if int(_f(attempts)) > 0:
        return None
    if due is False:
        # Su cadencia no vence: eso es el ritmo, no un defecto. Lo que NO exime
        # es el estado `SCHEDULED` con la herramienta EXIGIBLE: ésa es
        # exactamente la que nadie está llamando.
        return None
    if quota_paused:
        return None
    if esperado < max(0.0, float(warmup_seconds)):
        return None
    return {
        "anomaly": ANOMALY_NEVER_CALLED,
        "severity": ("CRITICAL" if esperado >= NUNCA_LLAMADA_S else "WARN"),
        "eligible_seconds": round(esperado, 1),
        "detail": (f"exigible desde hace {esperado:.0f} s, con cuota libre y "
                   f"sin un solo intento: no es una espera, es que el "
                   f"programador no la está llamando"),
        "action": ("revisar el orden del lote y el presupuesto por ciclo: la "
                   "herramienta no entra nunca en `batch`"),
    }


def summarize(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Recuento por estado y las anomalías, para el Auditor."""
    conteo: Dict[str, int] = {e: 0 for e in STATES}
    anomalias: List[Dict[str, Any]] = []
    for fila in rows or []:
        estado = str(fila.get("state") or "")
        if estado in conteo:
            conteo[estado] += 1
        an = fila.get("anomaly")
        if an:
            anomalias.append({"key": fila.get("key"), **dict(an)})
    return {
        "contract": "ITMQ_SCHEDULER_STATES_V1",
        "states": STATES,
        "counts": conteo,
        "serving": conteo[LIVE] + conteo[STALE],
        "waiting": sum(conteo[e] for e in WAITING),
        "failing": conteo[PROVIDER_ERROR],
        "anomalies": anomalias,
        "policy": ("un estado dice qué le pasa a la herramienta; una anomalía "
                   "dice que el programador tiene un defecto. No son lo mismo y "
                   "no comparten cajón"),
    }


__all__ = [
    "classify", "anomaly", "summarize", "STATES", "SERVING", "WAITING",
    "SCHEDULED", "WAITING_RATE_LIMIT", "WAITING_DEPENDENCY", "COOLDOWN",
    "RUNNING", "LIVE", "NO_DATA", "STALE", "PROVIDER_ERROR",
    "ANOMALY_NEVER_CALLED", "WARMUP_S", "NUNCA_LLAMADA_S",
]
