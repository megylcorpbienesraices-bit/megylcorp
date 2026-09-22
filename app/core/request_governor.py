"""GOBERNADOR DE PETICIONES EN VUELO · ITM QUANT v1.58.0

═══════════════════════════════════════════════════════════════════════════
POR QUÉ EL LIMITADOR DE CUOTA NO BASTA
═══════════════════════════════════════════════════════════════════════════

El contrato del proveedor son 240 peticiones en 60 s y 20 en 1 s. El guardián de
cuota (`QuotaGuard`) respeta las dos, y aun así se producen timeouts. La razón es
que **la cuota cuenta peticiones, no peticiones simultáneas**:

    20 peticiones en 1 s cumple el contrato…
    …y si las 20 son `interval-map` y `order-flow-raw`, están las 20 VIVAS a la
    vez, compitiendo por el ancho de banda del proveedor y por el pool de
    conexiones del cliente.

El resultado es una congestión que nos hacemos nosotros: cada petición tarda más
porque las otras diecinueve también están abiertas, y todas mueren por plazo al
mismo tiempo. Desde fuera se lee como «el proveedor va lento». Desde dentro es
una avalancha propia.

Un plazo más generoso **empeora** esto si no hay límite de concurrencia: los
sockets viven más, se solapan más y la congestión crece. Por eso el plazo
adaptativo y este gobernador son UNA sola medida y no dos.

═══════════════════════════════════════════════════════════════════════════
LAS TRES COSAS QUE HACE
═══════════════════════════════════════════════════════════════════════════

1 · TECHO DE PETICIONES SIMULTÁNEAS, además del de cuota. Y un techo aparte para
    las PESADAS: cuatro peticiones ligeras a la vez no se parecen a cuatro mapas
    por intervalo a la vez.

2 · ESCALONADO. Dos endpoints pesados no arrancan en el mismo milisegundo ni
    aunque haya hueco para los dos. La ráfaga de arranque puede hidratar rápido;
    lo que no puede es salir toda de golpe.

3 · MEDICIÓN DE DÓNDE SE VA EL TIEMPO:

        queue_wait_ms    esperando un hueco AQUÍ  → congestión nuestra
        request_ms       en la red y el proveedor → lentitud suya
        total_ms         lo que ve el ciclo
        timeout_budget_ms el plazo con el que se llamó

    Sin esa separación, «tardó 9 s» no distingue un proveedor lento de una cola
    propia mal dimensionada, y son dos arreglos opuestos: al primero se le da más
    plazo, al segundo MENOS concurrencia.

═══════════════════════════════════════════════════════════════════════════
AISLAMIENTO
═══════════════════════════════════════════════════════════════════════════

El gobernador reparte turnos; no conoce endpoints, símbolos ni proveedores. Un
turno agotado no borra ningún dato ni afecta al estado de ninguna herramienta:
eso vive en `endpoint_runtime` (plazo y cortacircuitos por endpoint) y en el Data
Hub (último valor bueno por herramienta).
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional

# ── clases de coste ───────────────────────────────────────────────────────
#: Una petición ligera devuelve una tabla corta; una pesada recorre la cadena
#: entera o la cinta del día. Tratarlas igual es lo que produce la avalancha.
LIGHT = "LIGHT"
HEAVY = "HEAVY"
WEIGHTS = (LIGHT, HEAVY)

#: Peticiones simultáneas totales. Con plazos de doce segundos, más de esto
#: garantiza que el ciclo termine con la mitad de las peticiones vivas.
DEFAULT_MAX_INFLIGHT = 4

#: De ésas, cuántas pueden ser pesadas a la vez.
DEFAULT_MAX_HEAVY_INFLIGHT = 2

#: Separación mínima entre arranques de peticiones pesadas.
DEFAULT_STAGGER_S = 0.35

#: Cuántas mediciones se guardan por endpoint para la vista del Auditor.
TIMING_WINDOW = 20


def _ms(seconds: float) -> float:
    return round(float(seconds) * 1000.0, 2)


class _Timing:
    """Las cuatro cifras de una petición, y su historia corta."""

    __slots__ = ("key", "weight", "samples", "count", "timeouts", "max_total_ms")

    def __init__(self, key: str, weight: str) -> None:
        self.key = key
        self.weight = weight
        self.samples: List[Dict[str, Any]] = []
        self.count = 0
        self.timeouts = 0
        self.max_total_ms = 0.0

    def add(self, fila: Dict[str, Any]) -> None:
        self.count += 1
        if fila.get("outcome") == "TIMEOUT":
            self.timeouts += 1
        self.max_total_ms = max(self.max_total_ms, float(fila.get("total_ms") or 0.0))
        self.samples.append(fila)
        if len(self.samples) > TIMING_WINDOW:
            del self.samples[0:len(self.samples) - TIMING_WINDOW]

    def describe(self) -> Dict[str, Any]:
        ultima = self.samples[-1] if self.samples else {}
        colas = [float(s.get("queue_wait_ms") or 0.0) for s in self.samples]
        peticiones = [float(s.get("request_ms") or 0.0) for s in self.samples]
        return {
            "key": self.key,
            "weight": self.weight,
            "calls": self.count,
            "timeouts": self.timeouts,
            "queue_wait_ms": ultima.get("queue_wait_ms"),
            "request_ms": ultima.get("request_ms"),
            "total_ms": ultima.get("total_ms"),
            "timeout_budget_ms": ultima.get("timeout_budget_ms"),
            "outcome": ultima.get("outcome"),
            "avg_queue_wait_ms": (round(sum(colas) / len(colas), 2) if colas else None),
            "avg_request_ms": (round(sum(peticiones) / len(peticiones), 2)
                               if peticiones else None),
            "max_total_ms": round(self.max_total_ms, 2),
            # El diagnóstico que el operador necesita de un vistazo: ¿la culpa es
            # de la cola (nuestra) o de la petición (del proveedor)?
            "bottleneck": ultima.get("bottleneck"),
        }


class _Slot:
    """Un turno concedido. Mide y libera; no puede olvidarse de liberar."""

    def __init__(self, gov: "RequestGovernor", key: str, weight: str,
                 budget_s: Optional[float], queue_wait_s: float) -> None:
        self._gov = gov
        self.key = key
        self.weight = weight
        self.budget_s = budget_s
        self.queue_wait_s = queue_wait_s
        self._t0 = time.monotonic()
        self.request_s: Optional[float] = None

    @property
    def queue_wait_ms(self) -> float:
        return _ms(self.queue_wait_s)

    def elapsed_s(self) -> float:
        return time.monotonic() - self._t0

    def done(self, outcome: str = "OK") -> Dict[str, Any]:
        """Cierra la medición. `outcome` es OK, TIMEOUT, ERROR o SKIPPED."""
        self.request_s = self.elapsed_s()
        total_s = self.queue_wait_s + self.request_s
        cola_ms, pet_ms = _ms(self.queue_wait_s), _ms(self.request_s)
        fila = {
            "key": self.key, "weight": self.weight, "outcome": str(outcome),
            "queue_wait_ms": cola_ms,
            "request_ms": pet_ms,
            "total_ms": _ms(total_s),
            "timeout_budget_ms": (None if self.budget_s is None else _ms(self.budget_s)),
            # Qué mandó en ESTA petición. Si la cola pesa más que la red, el
            # arreglo es bajar concurrencia, no subir el plazo.
            "bottleneck": ("COLA_PROPIA" if cola_ms > pet_ms else "PROVEEDOR"),
        }
        self._gov._release(self, fila)
        return fila


class RequestGovernor:
    """Techo de peticiones en vuelo, escalonado y medición. Sin estado de negocio."""

    def __init__(self, *, max_inflight: int = DEFAULT_MAX_INFLIGHT,
                 max_heavy_inflight: int = DEFAULT_MAX_HEAVY_INFLIGHT,
                 stagger_s: float = DEFAULT_STAGGER_S) -> None:
        self.max_inflight = max(1, int(max_inflight))
        self.max_heavy_inflight = max(1, min(int(max_heavy_inflight), self.max_inflight))
        self.stagger_s = max(0.0, float(stagger_s))
        self._lock = threading.Lock()
        self._sem_total: Optional[asyncio.Semaphore] = None
        self._sem_heavy: Optional[asyncio.Semaphore] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._inflight = 0
        self._inflight_heavy = 0
        self._max_observed = 0
        self._max_observed_heavy = 0
        self._next_heavy_start = 0.0
        self._timings: Dict[str, _Timing] = {}
        self._granted = 0
        self._queue_waited = 0

    # ── turnos ────────────────────────────────────────────────────────────
    def _semaforos(self):
        """Los semáforos se crean en el bucle que los va a usar.

        Un `asyncio.Semaphore` creado en otro bucle —o antes de que exista—
        bloquea para siempre en cuanto se usa desde el bucle real. El
        gobernador es un singleton de módulo, así que la creación se aplaza y
        se reconstruye si el bucle cambia (lo que pasa en las pruebas).
        """
        bucle = asyncio.get_running_loop()
        with self._lock:
            if self._sem_total is None or self._loop is not bucle:
                self._loop = bucle
                self._sem_total = asyncio.Semaphore(self.max_inflight)
                self._sem_heavy = asyncio.Semaphore(self.max_heavy_inflight)
            return self._sem_total, self._sem_heavy

    async def acquire(self, key: str, *, weight: str = LIGHT,
                      budget_s: Optional[float] = None) -> _Slot:
        """Espera un turno y devuelve el medidor. Siempre hay que cerrarlo."""
        peso = HEAVY if str(weight).upper() == HEAVY else LIGHT
        total, heavy = self._semaforos()
        t0 = time.monotonic()
        await total.acquire()
        if peso == HEAVY:
            try:
                await heavy.acquire()
            except BaseException:
                total.release()
                raise
            # Escalonado por RESERVA, no por «mirar y dormir».
            #
            # Comprobar cuánto ha pasado desde el último arranque y dormir la
            # diferencia no serializa nada: tres pesadas que entran a la vez leen
            # el MISMO instante, calculan la misma espera y despiertan juntas. La
            # avalancha se retrasa 350 ms y sigue siendo una avalancha.
            #
            # Cada una reserva su hueco sobre un reloj que sólo avanza, así que
            # los arranques quedan separados de verdad: t, t+s, t+2s…
            with self._lock:
                ahora = time.monotonic()
                mio = max(ahora, self._next_heavy_start)
                self._next_heavy_start = mio + self.stagger_s
            espera = mio - ahora
            if espera > 0:
                # La espera se cuenta como cola, que es lo que es.
                await asyncio.sleep(espera)
        espera_total = time.monotonic() - t0
        with self._lock:
            self._inflight += 1
            self._granted += 1
            if espera_total > 0.001:
                self._queue_waited += 1
            if peso == HEAVY:
                self._inflight_heavy += 1
                self._max_observed_heavy = max(self._max_observed_heavy,
                                               self._inflight_heavy)
            self._max_observed = max(self._max_observed, self._inflight)
        return _Slot(self, key, peso, budget_s, espera_total)

    def _release(self, slot: _Slot, fila: Dict[str, Any]) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if slot.weight == HEAVY:
                self._inflight_heavy = max(0, self._inflight_heavy - 1)
            t = self._timings.get(slot.key)
            if t is None:
                t = self._timings[slot.key] = _Timing(slot.key, slot.weight)
            t.add(fila)
            total, heavy = self._sem_total, self._sem_heavy
        if slot.weight == HEAVY and heavy is not None:
            heavy.release()
        if total is not None:
            total.release()

    def has_free_slot(self) -> bool:
        """¿Hay hueco AHORA? Lo consulta el presupuesto de reintento.

        Un reintento que tiene que esperar cola no es un reintento rápido: es
        otra petición compitiendo con las del ciclo siguiente.
        """
        with self._lock:
            return self._inflight < self.max_inflight

    # ── lectura ───────────────────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            filas = [t.describe() for t in self._timings.values()]
            estado = {
                "max_inflight": self.max_inflight,
                "max_heavy_inflight": self.max_heavy_inflight,
                "stagger_seconds": self.stagger_s,
                "inflight": self._inflight,
                "inflight_heavy": self._inflight_heavy,
                "max_observed_inflight": self._max_observed,
                "max_observed_heavy": self._max_observed_heavy,
                "granted": self._granted,
                "queued": self._queue_waited,
            }
        filas.sort(key=lambda f: -(f.get("total_ms") or 0.0))
        cuellos = [f["key"] for f in filas if f.get("bottleneck") == "COLA_PROPIA"]
        estado.update({
            "endpoints": filas,
            "self_congested": sorted(cuellos),
            "policy": (f"concurrencia total {self.max_inflight} · pesadas "
                       f"{self.max_heavy_inflight} · escalonado {self.stagger_s}s; "
                       "la cuota cuenta peticiones, esto cuenta peticiones VIVAS"),
        })
        return estado

    def reset(self) -> None:
        with self._lock:
            self._timings.clear()
            self._inflight = 0
            self._inflight_heavy = 0
            self._max_observed = 0
            self._max_observed_heavy = 0
            self._granted = 0
            self._queue_waited = 0
            self._next_heavy_start = 0.0
            self._sem_total = None
            self._sem_heavy = None
            self._loop = None


__all__ = ["RequestGovernor", "LIGHT", "HEAVY", "WEIGHTS",
           "DEFAULT_MAX_INFLIGHT", "DEFAULT_MAX_HEAVY_INFLIGHT",
           "DEFAULT_STAGGER_S", "TIMING_WINDOW"]
