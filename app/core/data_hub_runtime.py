"""RESILIENCIA DEL DATA HUB · ITM QUANT v1.44.0

QUÉ FALTABA Y QUÉ NO
--------------------
v1.43.0 ya traía tres de las siete piezas, y no se rehacen aquí:

    caché por ticker/dataset      `providers.quantdata.shared.RawCache`
    backpressure por cuota        `providers.quantdata.shared.QuotaGuard`
    reintento con backoff         `QuantDataTool.mark_transient`

Este módulo añade las cuatro que faltaban, y sólo ésas:

    1. DEDUPLICACIÓN EN VUELO
       `RawCache` deduplica DESPUÉS de que la primera petición termine. Si TRACE y
       EXPOSICIÓN piden GEX en el mismo instante —que es lo que pasa al abrir la
       terminal— las dos salen a la red: la caché todavía está vacía. El
       coalescedor hace que la segunda ESPERE a la primera en vez de duplicarla.

    2. LAST KNOWN GOOD
       La caché caduca a los 90 s y a partir de ahí devuelve `None`, que aguas
       abajo es indistinguible de «el proveedor no tiene datos». Pero un dato de
       hace tres minutos no es un hueco: es un dato viejo, y decirlo permite
       seguir dibujando la estructura mientras se marca su edad. El LKG no caduca
       nunca; se degrada y lo declara.

    3. ACTUALIZACIÓN INCREMENTAL
       Las series por intervalo se republican enteras en cada ciclo, con el último
       bucket todavía abierto y creciendo. Sustituir la serie entera pierde la
       historia si una respuesta llega recortada; acumular a ciegas cuenta el
       bucket abierto tantas veces como refrescos haya. El merge por clave
       temporal resuelve las dos cosas: gana el valor más reciente de cada
       instante y la historia se conserva.

    4. AISLAMIENTO POR CANAL
       Un `asyncio.gather` sobre nueve endpoints termina cuando termina el más
       lento. Si `dark-flow` tarda 9 s, el ciclo entero tarda 9 s y con él la
       estructura que sostiene TRACE. Cada canal lleva ahora su propio timeout y
       su propio cortocircuito: el lento se degrada SOLO.

LA REGLA QUE GOBIERNA TODO EL MÓDULO
------------------------------------
**Ningún endpoint de opciones puede bloquear el precio.** El precio viene de otro
proveedor por otro carril y no pasa por aquí; lo que este módulo garantiza es que
tampoco un canal lento de Quant Data bloquee a los demás canales de Quant Data.
"""
from __future__ import annotations

import asyncio
import logging as _logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .obs import note as _obs_note, expected as _obs_expected

# Un dato servido desde el Last Known Good deja de ser «fresco» aquí…
LKG_DEGRADED_AFTER_S = 120.0
# …y a partir de aquí sólo sirve como contexto, nunca como lectura operable.
LKG_STALE_AFTER_S = 900.0

# Tiempo máximo que un canal puede tardar antes de que se le dé por degradado en
# ESTE ciclo. No cancela la petición subyacente: la deja terminar para que alimente
# el LKG, pero el ciclo no la espera.
DEFAULT_CHANNEL_TIMEOUT_S = 6.0

# Holgura del plazo del CICLO sobre el plazo de la PETICIÓN.
#
# v1.58.1 · Los dos plazos existen por motivos distintos y hay que ordenarlos.
# Cuando el ciclo se rendía ANTES que la petición —lo que pasaba en cuanto el
# plazo calibrado bajaba del configurado—, el mismo hecho se contaba dos veces:
# el canal anotaba un timeout, la petición huérfana seguía viva ocupando
# conexión y cuota, y al morir soltaba un segundo aviso `:late`. El registro se
# llenaba de degradaciones que eran una sola.
#
# Con la holgura, la petición SIEMPRE muere primero y con su causa real —plazo
# agotado, 400, 500—, el ciclo la recoge clasificada, y el camino del huérfano
# queda para lo que se diseñó: una respuesta que llega tarde y alimenta el LKG.
CHANNEL_SLACK_S = 1.0

# Fallos consecutivos tras los cuales un canal queda abierto (cortocircuito).
BREAKER_THRESHOLD = 3
# Cuánto permanece abierto antes de dejar pasar una petición de prueba.
BREAKER_COOLDOWN_S = 60.0

FRESH = "FRESH"
DEGRADED = "DEGRADED"
STALE = "STALE"
MISSING = "MISSING"


def _now() -> float:
    return time.time()


# ═══════════════════════════════════════════════════ 1 · deduplicación en vuelo

def _swallow_unretrieved(fut: asyncio.Future) -> None:
    """Marca como recogida la excepción de un futuro que nadie llegó a esperar.

    El llamador original SIEMPRE recibe el error —se re-lanza en `run()`—; este
    futuro es sólo el canal para los que se hubieran unido. Sin joiners, dejarlo
    sin leer produce un aviso de asyncio que no corresponde a ningún fallo nuevo.
    """
    if fut.cancelled():
        return
    try:
        fut.exception()
    except asyncio.CancelledError:
        _obs_expected("data_hub.inflight.cancelled")


class InFlight:
    """Una sola petición por (dataset, símbolo), por muchos que la pidan.

    Sin esto, abrir la terminal dispara la misma consulta desde cada sección que
    la necesita: la caché aún está vacía, así que ninguna se ahorra. Con una cuota
    de 240 peticiones/hora, arrancar cuatro veces puede costar el presupuesto de
    media sesión en datos idénticos.

    El futuro compartido se retira del registro en cuanto termina, de modo que un
    fallo no queda cacheado como «ya se está pidiendo» para siempre.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pending: Dict[Tuple[str, str], asyncio.Future] = {}
        self._joined = 0
        self._started = 0

    async def run(self, dataset: str, symbol: str,
                  factory: Callable[[], Awaitable[Any]]) -> Any:
        key = (str(dataset), str(symbol).upper())
        async with self._lock:
            pending = self._pending.get(key)
            if pending is not None and not pending.done():
                self._joined += 1
                fut = pending
                join = True
            else:
                fut = asyncio.get_running_loop().create_future()
                # Si nadie llega a esperar este futuro compartido, su excepción
                # quedaría sin recoger y asyncio la anunciaría por consola. Ese
                # ruido es justo el que esconde los fallos de verdad, así que se
                # consume aquí: el error ya viaja al llamador por la vía normal.
                fut.add_done_callback(_swallow_unretrieved)
                self._pending[key] = fut
                self._started += 1
                join = False

        if join:
            # `await` sobre un futuro compartido: si la original falla, esta
            # llamada recibe la MISMA excepción, no un éxito falso.
            return await asyncio.shield(fut)

        try:
            result = await factory()
        except BaseException as exc:   # noqa: BLE001 — se re-lanza intacta
            async with self._lock:
                self._pending.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)
            raise
        async with self._lock:
            self._pending.pop(key, None)
        if not fut.done():
            fut.set_result(result)
        return result

    def stats(self) -> Dict[str, Any]:
        return {"started": self._started, "joined": self._joined,
                "in_flight": len(self._pending),
                "saved_requests": self._joined}


# ═══════════════════════════════════════════════════ 2 · Last Known Good

@dataclass
class LKGEntry:
    payload: Any
    stored_at: float
    symbol: str
    dataset: str
    updates: int = 0

    def age(self, now: Optional[float] = None) -> float:
        return max(0.0, (now or _now()) - self.stored_at)

    def freshness(self, now: Optional[float] = None) -> str:
        a = self.age(now)
        if a <= LKG_DEGRADED_AFTER_S:
            return FRESH
        if a <= LKG_STALE_AFTER_S:
            return DEGRADED
        return STALE


class LastKnownGood:
    """El último dato válido de cada dataset, que NO caduca a `None`.

    Diferencia con la caché: la caché responde «¿tengo esto fresco?» y calla si no.
    El LKG responde «esto es lo último que vi y hace cuánto», que es una respuesta
    distinta y utilizable. Con ella la estructura sigue en pantalla durante un
    hueco del proveedor, marcada por su edad, en vez de desaparecer y reaparecer.

    Se guarda por SÍMBOLO: al cambiar de activo, el LKG del anterior no puede
    servir de respaldo para el nuevo. Eso sería mezclar dos mercados.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[Tuple[str, str], LKGEntry] = {}

    def put(self, dataset: str, symbol: str, payload: Any) -> None:
        if payload is None:
            return
        key = (str(dataset), str(symbol).upper())
        with self._lock:
            prev = self._data.get(key)
            self._data[key] = LKGEntry(payload=payload, stored_at=_now(),
                                       symbol=key[1], dataset=key[0],
                                       updates=(prev.updates + 1 if prev else 1))

    def get(self, dataset: str, symbol: str) -> Optional[LKGEntry]:
        with self._lock:
            return self._data.get((str(dataset), str(symbol).upper()))

    def read(self, dataset: str, symbol: str,
             *, accept: Tuple[str, ...] = (FRESH, DEGRADED)) -> Dict[str, Any]:
        """Lectura declarada: qué hay, de cuándo, y si se puede operar con ello."""
        entry = self.get(dataset, symbol)
        if entry is None:
            return {"ready": False, "freshness": MISSING, "payload": None,
                    "age_seconds": None, "dataset": str(dataset),
                    "symbol": str(symbol).upper(),
                    "detail": "nunca se recibió este conjunto de datos"}
        fresh = entry.freshness()
        return {
            "ready": fresh in accept, "freshness": fresh,
            "payload": entry.payload if fresh in accept else None,
            "age_seconds": round(entry.age(), 2),
            "dataset": entry.dataset, "symbol": entry.symbol,
            "updates": entry.updates,
            "detail": ("" if fresh == FRESH else
                       f"último dato válido hace {entry.age():.0f} s"),
        }

    def clear_symbol(self, symbol: str) -> int:
        sym = str(symbol).upper()
        with self._lock:
            keys = [k for k in self._data if k[1] == sym]
            for k in keys:
                self._data.pop(k, None)
        return len(keys)

    def reset(self) -> None:
        with self._lock:
            self._data.clear()

    def snapshot(self) -> Dict[str, Any]:
        now = _now()
        with self._lock:
            rows = [{"dataset": e.dataset, "symbol": e.symbol,
                     "age_seconds": round(e.age(now), 2),
                     "freshness": e.freshness(now), "updates": e.updates}
                    for e in self._data.values()]
        return {"entries": sorted(rows, key=lambda r: (r["symbol"], r["dataset"])),
                "count": len(rows)}


# ═══════════════════════════════════════════════════ 3 · merge incremental

def merge_time_series(previous: Any, incoming: Any, *, key: str = "t",
                      max_points: int = 2000) -> List[Dict[str, Any]]:
    """Fusiona dos versiones de una serie por instante, sin duplicar ni perder.

    Dos fallos opuestos que este merge evita:

      * **Sustituir la serie entera** pierde la historia cuando una respuesta llega
        recortada —el proveedor a veces devuelve sólo la última hora—, y la curva
        acumulada de la sesión se reinicia sola a media mañana.
      * **Acumular a ciegas** cuenta el último bucket, que sigue ABIERTO y crece en
        cada refresco, tantas veces como refrescos haya.

    La regla es: gana el valor MÁS RECIENTE de cada instante. El bucket abierto se
    corrige solo en cada ciclo y los cerrados no se tocan.
    """
    out: Dict[Any, Dict[str, Any]] = {}
    for row in (previous or []):
        if isinstance(row, dict) and row.get(key) is not None:
            out[row[key]] = dict(row)
    replaced = 0
    added = 0
    for row in (incoming or []):
        if not isinstance(row, dict) or row.get(key) is None:
            continue
        k = row[key]
        if k in out:
            replaced += 1
        else:
            added += 1
        out[k] = dict(row)
    merged = [out[k] for k in sorted(out)]
    if len(merged) > max_points:
        merged = merged[-max_points:]
    return merged


def merge_dataset(previous: Any, incoming: Any, *, key: str = "t") -> Dict[str, Any]:
    """`merge_time_series` con el recuento de lo que cambió, para el Auditor."""
    prev_rows = (previous or {}).get("rows") if isinstance(previous, dict) else previous
    new_rows = (incoming or {}).get("rows") if isinstance(incoming, dict) else incoming
    merged = merge_time_series(prev_rows, new_rows, key=key)
    base = dict(incoming) if isinstance(incoming, dict) else {}
    base["rows"] = merged
    base["count"] = len(merged)
    base["merge"] = {"previous": len(prev_rows or []), "incoming": len(new_rows or []),
                     "result": len(merged), "strategy": "LATEST_WINS_BY_TIMESTAMP"}
    return base


# ═══════════════════════════════════════════════════ 4 · aislamiento por canal

@dataclass
class ChannelHealth:
    name: str
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""
    last_success: Optional[float] = None
    last_latency_s: Optional[float] = None
    timeouts: int = 0
    calls: int = 0

    def is_open(self, now: Optional[float] = None) -> bool:
        return (now or _now()) < self.opened_until

    def describe(self) -> Dict[str, Any]:
        now = _now()
        return {
            "channel": self.name, "calls": self.calls, "failures": self.failures,
            "timeouts": self.timeouts, "open": self.is_open(now),
            "reopens_in_seconds": max(0.0, round(self.opened_until - now, 1)) if self.is_open(now) else 0.0,
            "last_error": self.last_error[:180],
            "last_latency_seconds": self.last_latency_s,
            "seconds_since_success": (None if self.last_success is None
                                      else round(now - self.last_success, 1)),
        }


class ChannelIsolator:
    """Cada canal falla y se degrada SOLO.

    Dos mecanismos distintos y complementarios:

      * **Timeout por canal** — el ciclo no espera más de `timeout` por un canal.
        La petición NO se cancela: se la deja terminar en segundo plano para que
        alimente el Last Known Good, porque un dato que llega tarde sigue siendo
        mejor que ninguno en el ciclo siguiente.
      * **Cortocircuito** — tras `BREAKER_THRESHOLD` fallos seguidos el canal se
        abre y deja de intentarse durante `BREAKER_COOLDOWN_S`. Reintentar cada
        ciclo un endpoint que el plan no incluye sólo quema cuota.

    Nada de esto toca el precio: el precio no pasa por este carril.
    """

    def __init__(self, *, timeout_s: float = DEFAULT_CHANNEL_TIMEOUT_S,
                 threshold: int = BREAKER_THRESHOLD,
                 cooldown_s: float = BREAKER_COOLDOWN_S) -> None:
        self.timeout_s = float(timeout_s)
        self.threshold = int(threshold)
        self.cooldown_s = float(cooldown_s)
        self._lock = threading.RLock()
        self._health: Dict[str, ChannelHealth] = {}
        self._orphans: List[asyncio.Task] = []

    def health(self, channel: str) -> ChannelHealth:
        with self._lock:
            return self._health.setdefault(str(channel), ChannelHealth(name=str(channel)))

    def allows(self, channel: str) -> bool:
        return not self.health(channel).is_open()

    def note_success(self, channel: str, latency_s: Optional[float] = None) -> None:
        h = self.health(channel)
        with self._lock:
            h.failures = 0
            h.opened_until = 0.0
            h.last_error = ""
            h.last_success = _now()
            h.last_latency_s = (None if latency_s is None else round(latency_s, 3))

    def note_failure(self, channel: str, error: str, *, timeout: bool = False) -> None:
        h = self.health(channel)
        with self._lock:
            h.failures += 1
            h.last_error = str(error)
            if timeout:
                h.timeouts += 1
            if h.failures >= self.threshold:
                h.opened_until = _now() + self.cooldown_s

    async def call(self, channel: str, factory: Callable[[], Awaitable[Any]],
                   *, timeout_s: Optional[float] = None,
                   on_late: Optional[Callable[[Any], None]] = None) -> Dict[str, Any]:
        """Ejecuta un canal sin que su lentitud contagie a los demás."""
        h = self.health(channel)
        if h.is_open():
            return {"ok": False, "channel": channel, "skipped": True,
                    "reason": "CIRCUITO_ABIERTO", "payload": None,
                    "detail": f"{h.failures} fallos seguidos; reintento en "
                              f"{max(0.0, h.opened_until - _now()):.0f} s"}
        with self._lock:
            h.calls += 1
        limit = float(timeout_s if timeout_s is not None else self.timeout_s)
        task = asyncio.ensure_future(factory())
        task.add_done_callback(_swallow_unretrieved)
        started = time.monotonic()
        try:
            payload = await asyncio.wait_for(asyncio.shield(task), timeout=limit)
        except asyncio.TimeoutError:
            # La tarea sigue viva a propósito: que termine y alimente el LKG.
            self.note_failure(channel, f"timeout > {limit:.1f}s", timeout=True)
            self._adopt(task, channel, on_late)
            return {"ok": False, "channel": channel, "timeout": True, "payload": None,
                    "reason": "TIMEOUT",
                    "detail": f"el canal tardó más de {limit:.1f} s; el ciclo sigue sin él"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # noqa: BLE001 — se clasifica, no se traga
            self.note_failure(channel, f"{type(exc).__name__}: {exc}")
            _obs_note(f"data_hub:{channel}", exc, severity="DEGRADED")
            # La EXCEPCIÓN viaja en el resultado, no sólo su texto.
            #
            # Sin esto el llamador sólo sabía «falló», y perdía la diferencia entre
            # un 404 (la herramienta no existe), un 400 de validación (el cuerpo es
            # reparable) y un timeout. Esa diferencia es justo la que decide si hay
            # algo que arreglar o no, y aplanarla a una cadena la destruía.
            return {"ok": False, "channel": channel, "payload": None,
                    "reason": "ERROR", "exception": exc,
                    "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
        latency = time.monotonic() - started
        self.note_success(channel, latency)
        return {"ok": True, "channel": channel, "payload": payload,
                "latency_seconds": round(latency, 3)}

    def _adopt(self, task: asyncio.Task, channel: str,
               on_late: Optional[Callable[[Any], None]] = None) -> None:
        """Adopta una tarea que el ciclo ya no espera, SIN perder su resultado.

        El ciclo siguió adelante sin ella, pero la respuesta que llega tarde sigue
        siendo el dato más reciente que existe de ese canal: se entrega al Last
        Known Good por `on_late`. Descartarla obligaría a volver a pedirla y a
        pagar otra vez la cuota por el mismo dato.
        """
        def _done(t: asyncio.Task) -> None:
            with self._lock:
                if t in self._orphans:
                    self._orphans.remove(t)
            if t.cancelled():
                _obs_expected(f"data_hub.{channel}.late_cancelled")
                return
            exc = t.exception()
            if exc is not None:
                # v1.58.1 · NO ES UNA SEGUNDA DEGRADACIÓN: es la misma.
                #
                # El ciclo ya contó este fallo al agotarse el plazo del canal
                # —`note_failure`, que alimenta el cortacircuitos y la salud del
                # canal—. Volver a contarlo aquí como DEGRADED duplicaba cada
                # incidencia en /health y en el registro, y hacía leer como una
                # avería del proveedor lo que era una petición que el ciclo ya
                # había dado por perdida a propósito.
                #
                # El texto del error se conserva —es donde se ve QUÉ plazo
                # expiró— pero al nivel de lo esperado, no al de lo averiado.
                _obs_note(f"data_hub:{channel}:late", exc,
                          level=_logging.INFO, severity="OPTIONAL")
                return
            if on_late is not None:
                try:
                    on_late(t.result())
                except Exception as cb_exc:   # noqa: BLE001
                    _obs_note(f"data_hub:{channel}:late_store", cb_exc, severity="DEGRADED")

        with self._lock:
            self._orphans.append(task)
        task.add_done_callback(_done)

    def pending_late(self) -> int:
        with self._lock:
            return len(self._orphans)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            rows = [h.describe() for h in self._health.values()]
        return {"channels": sorted(rows, key=lambda r: r["channel"]),
                "open": [r["channel"] for r in rows if r["open"]],
                "late_pending": self.pending_late(),
                "timeout_seconds": self.timeout_s,
                "breaker_threshold": self.threshold}

    def reset(self) -> None:
        with self._lock:
            self._health.clear()


# ═══════════════════════════════════════════════════ fachada

class DataHubRuntime:
    """Las cuatro piezas juntas, con una sola vista de estado para el Auditor."""

    def __init__(self) -> None:
        self.inflight = InFlight()
        self.lkg = LastKnownGood()
        self.channels = ChannelIsolator()

    async def fetch(self, dataset: str, symbol: str,
                    factory: Callable[[], Awaitable[Any]],
                    *, timeout_s: Optional[float] = None,
                    accept_stale: bool = True) -> Dict[str, Any]:
        """Deduplicar → aislar → guardar LKG → responder con procedencia.

        Cuando el canal falla o tarda, se responde con el Last Known Good marcado
        por su edad en vez de con un hueco: la estructura sigue en pantalla y su
        antigüedad queda a la vista, que es la respuesta honesta.
        """
        sym = str(symbol).upper()
        res = await self.channels.call(
            dataset, lambda: self.inflight.run(dataset, sym, factory),
            timeout_s=timeout_s,
            # Lo que llegue tarde alimenta el LKG: el ciclo ya no lo espera, pero
            # el dato sigue siendo válido para el ciclo siguiente.
            on_late=lambda payload: self.lkg.put(dataset, sym, payload))
        if res.get("ok") and res.get("payload") is not None:
            self.lkg.put(dataset, sym, res["payload"])
            return {"ready": True, "payload": res["payload"], "freshness": FRESH,
                    "dataset": dataset, "symbol": sym, "source": "LIVE",
                    "latency_seconds": res.get("latency_seconds")}
        if not accept_stale:
            return {"ready": False, "payload": None, "freshness": MISSING,
                    "dataset": dataset, "symbol": sym, "source": "NONE",
                    "exception": res.get("exception"),
                    "detail": res.get("detail") or res.get("reason")}
        lkg = self.lkg.read(dataset, sym, accept=(FRESH, DEGRADED, STALE))
        return {
            "exception": res.get("exception"),
            "ready": bool(lkg["ready"] and lkg["payload"] is not None),
            "payload": lkg.get("payload"),
            "freshness": lkg["freshness"],
            "age_seconds": lkg.get("age_seconds"),
            "dataset": dataset, "symbol": sym,
            "source": ("LAST_KNOWN_GOOD" if lkg.get("payload") is not None else "NONE"),
            "detail": res.get("detail") or res.get("reason") or lkg.get("detail"),
        }

    def clear_symbol(self, symbol: str) -> Dict[str, Any]:
        dropped = self.lkg.clear_symbol(symbol)
        return {"symbol": str(symbol).upper(), "lkg_dropped": dropped}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "inflight": self.inflight.stats(),
            "last_known_good": self.lkg.snapshot(),
            "channels": self.channels.snapshot(),
            "policy": {
                "lkg_degraded_after_s": LKG_DEGRADED_AFTER_S,
                "lkg_stale_after_s": LKG_STALE_AFTER_S,
                "channel_timeout_s": self.channels.timeout_s,
                "note": ("Ningún canal de opciones puede bloquear al resto ni al "
                         "precio: el precio viaja por otro carril y cada canal lleva "
                         "su propio timeout y su propio cortocircuito."),
            },
        }

    def reset(self) -> None:
        self.lkg.reset()
        self.channels.reset()


HUB_RUNTIME = DataHubRuntime()

__all__ = ["InFlight", "LastKnownGood", "LKGEntry", "ChannelIsolator", "ChannelHealth",
           "DataHubRuntime", "HUB_RUNTIME", "merge_time_series", "merge_dataset",
           "FRESH", "DEGRADED", "STALE", "MISSING",
           "LKG_DEGRADED_AFTER_S", "LKG_STALE_AFTER_S", "DEFAULT_CHANNEL_TIMEOUT_S",
           "BREAKER_THRESHOLD", "BREAKER_COOLDOWN_S"]
