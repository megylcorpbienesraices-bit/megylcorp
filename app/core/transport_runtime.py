"""TRANSPORTE COMPARTIDO POR HOST · ITM QUANT v1.60.0

═══════════════════════════════════════════════════════════════════════════
`connect timed out after 4.0s`, UNA Y OTRA VEZ, EN `net_drift`, `net_flow`,
`dark_flow`, `gamma`…
═══════════════════════════════════════════════════════════════════════════

v1.58.0 separó el plazo de LECTURA del de CONEXIÓN y calibró el primero con el
p95 medido por endpoint. Eso arregló los cortes a 5.0 s por lectura. Y dejó el
otro lado intacto: la CONEXIÓN seguía siendo una constante —4.0 s— aplicada por
petición, sin medir nada y sin salida posible.

El registro de producción en Windows enseña la consecuencia: no es *un* endpoint
el que no conecta, son `net_drift`, `net_flow`, `dark_flow` y `gamma` a la vez,
al mismo plazo exacto, ciclo tras ciclo. Cuando el fallo es idéntico en
endpoints que no comparten nada salvo el host, el defecto **no está en el
endpoint**: está en el transporte.

═══════════════════════════════════════════════════════════════════════════
LAS TRES CAUSAS, Y NINGUNA SE ARREGLA SUBIENDO EL NÚMERO
═══════════════════════════════════════════════════════════════════════════

**1 · Dos pools, ninguno reutilizado.** El carril del motor y el de páginas
construían cada uno su `httpx.AsyncClient`. Dos pools contra el mismo host, con
el `keepalive_expiry` por defecto de httpx: **5 segundos**. El ciclo de páginas
son 15 s y el del motor más. Cada ciclo encontraba **todas** las conexiones
caducadas, así que cada ciclo era un handshake TCP + TLS nuevo por herramienta.

**2 · Y todos a la vez.** Con el lote saliendo junto, eso es una *estampida de
conexión*: seis, ocho handshakes simultáneos contra el mismo host, compitiendo
por el mismo enlace. En un enlace doméstico con antivirus o proxy corporativo
de por medio, el handshake TLS se va por encima de los cuatro segundos sin que
el proveedor tenga nada que ver.

**3 · Y aprender era imposible por construcción.** Ésta es la misma trampa que
v1.58.1 cerró para la lectura, intacta para la conexión: si el plazo de conexión
es fijo y el handshake real necesita más, **nunca se completa un handshake**;
sin handshake completo no hay muestra; sin muestras no hay p95; y el plazo se
queda en 4.0 s para siempre. El sistema no podía salir de ahí ni con una hora
de tráfico.

═══════════════════════════════════════════════════════════════════════════
LO QUE HACE ESTE MÓDULO
═══════════════════════════════════════════════════════════════════════════

    UN pool por host, compartido por los dos carriles, con keep-alive que
    sobrevive al ciclo · el plazo de CONEXIÓN gobernado por el p95 del
    handshake MEDIDO, a nivel de HOST y no de endpoint · escalada cuando el
    plazo se agota, para que la falta de muestras deje de ser una trampa ·
    `connect`, `read`, `write` y `pool` como cuatro plazos distintos ·
    cortacircuitos DE TRANSPORTE para lo que falla a nivel de host · un solo
    reintento de conexión, con presupuesto, backoff con jitter y nunca durante
    una ráfaga.

Lo que NO hace: decidir el plazo de LECTURA —eso sigue siendo del
`endpoint_runtime`, p95 por endpoint— ni tocar ninguna matemática.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ EL NÚMERO TAMBIÉN CAMBIA, Y POR QUÉ NO ES «EL ARREGLO»
═══════════════════════════════════════════════════════════════════════════

El warm start de conexión pasa de 4.0 s a 8.0 s. No porque ocho funcione mejor
que cuatro, sino porque el razonamiento que sostenía el cuatro —«si la conexión
no se establece en cuatro segundos, no va a establecerse»— **es falso con la
evidencia delante**: un handshake TLS con inspección de por medio tarda más. Un
supuesto que el campo contradice se corrige.

Pero subir el número no arregla nada por sí solo, y por eso no es lo que hace
este módulo. Con un pool compartido y keep-alive largo, **el handshake deja de
ocurrir en cada ciclo**: se paga una vez y se reutiliza. Ésa es la diferencia
entre no cortar la conexión y no tener que abrirla.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# ── fases del transporte ────────────────────────────────────────────────────
#
# Tres fallos con tres causas y tres remedios. Mezclarlos fue lo que hizo que
# «timeout» significara cuatro cosas distintas en el mismo registro.
CONNECT = "CONNECT_TIMEOUT"   # no se alcanzó al host: red, DNS, TLS
POOL = "POOL_TIMEOUT"         # el host se alcanza, pero NUESTRO pool está lleno
READ = "READ_TIMEOUT"         # se conectó y el proveedor no terminó de contestar
WRITE = "WRITE_TIMEOUT"       # no se pudo terminar de enviar la petición
PHASES = (CONNECT, POOL, READ, WRITE)

#: Suelo del plazo de conexión. Por debajo de esto no se mide un handshake: se
#: corta uno que estaba en marcha.
CONNECT_FLOOR_S = 3.0

#: Techo. Por encima, esperar más no informa: si el host no respondió en quince
#: segundos, el problema no es el plazo.
CONNECT_CEILING_S = 15.0

#: Arranque en frío del plazo de conexión, sin muestras. Ver la cabecera: el
#: número cambia porque el supuesto que lo sostenía era falso, no porque subirlo
#: sea el arreglo.
CONNECT_WARM_START_S = 8.0

#: El plazo es el p95 del handshake por este factor. Un handshake tiene una cola
#: larga —retransmisiones TCP, una CRL que se consulta— y cortar al p95 exacto
#: mataría una de cada veinte conexiones sanas.
CONNECT_P95_FACTOR = 3.0

#: Muestras necesarias para que el p95 mande sobre el warm start.
MIN_CONNECT_SAMPLES = 6

#: Ventana de muestras de handshake por host.
CONNECT_WINDOW = 40

#: Cuando un plazo de conexión se agota, el siguiente intento sube por este
#: factor. ES LA SALIDA DE LA TRAMPA: sin esto, un plazo corto impide completar
#: el handshake, la falta de handshake impide medir, y la falta de medidas
#: mantiene el plazo corto. Para siempre.
CONNECT_ESCALATION = 1.6

#: Y baja sola cuando vuelve a haber handshakes buenos, para no quedarse
#: esperando quince segundos a un host que ya conecta en 300 ms.
CONNECT_DECAY = 0.85

#: Keep-alive: tiene que sobrevivir al ciclo más largo, o cada ciclo vuelve a
#: pagar el handshake. El ciclo de páginas son 15 s; el del motor, hasta 60.
KEEPALIVE_EXPIRY_S = 90.0

#: Plazo de escritura. Los cuerpos son JSON pequeño: si no se puede enviar en
#: diez segundos, el enlace está roto.
WRITE_TIMEOUT_S = 10.0

#: Fallos de HOST consecutivos que abren el cortacircuitos del transporte.
#: Es distinto del de endpoint: aquí un 500 no cuenta —eso es el endpoint—;
#: cuentan los fallos que impiden llegar, que afectan a TODAS las herramientas.
HOST_FAILURE_THRESHOLD = 5
HOST_OPEN_SECONDS = 20.0
HOST_OPEN_SECONDS_MAX = 180.0

#: Presupuesto de reintentos de CONEXIÓN por ciclo, para todo el host. Un
#: reintento por petición multiplicaría por dos la estampida que causa el fallo.
CONNECT_RETRY_BUDGET = 2

#: Espera base del reintento de conexión, con jitter. Reintentar al instante
#: contra un host que acaba de rechazar la conexión repite el mismo fallo.
CONNECT_RETRY_BACKOFF_S = 0.75
CONNECT_RETRY_JITTER_S = 0.5

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"


def _percentil(valores: List[float], q: float) -> Optional[float]:
    if not valores:
        return None
    xs = sorted(valores)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * max(0.0, min(1.0, q))
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def _ms(seconds: Optional[float]) -> Optional[float]:
    return None if seconds is None else round(float(seconds) * 1000.0, 2)


class TransportTrace:
    """Dónde se fue el tiempo de UNA petición, medido en el transporte.

    Se engancha al `trace` de httpcore, que es el único sitio donde se puede
    separar «esperar sitio en el pool» de «abrir el socket» de «negociar TLS»
    de «esperar la respuesta». Sin esa separación, un `connect timed out` y una
    cola propia llena se leen igual desde fuera y se arreglan al revés.
    """

    __slots__ = ("_t0", "_marks", "pool_wait_s", "connect_s", "tls_s",
                 "write_s", "read_s", "total_s", "reused")

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._marks: Dict[str, float] = {}
        self.pool_wait_s: Optional[float] = None
        self.connect_s: Optional[float] = None
        self.tls_s: Optional[float] = None
        self.write_s: Optional[float] = None
        self.read_s: Optional[float] = None
        self.total_s: Optional[float] = None
        #: Conexión REUTILIZADA: no hubo handshake. Es la señal de que el
        #: keep-alive está haciendo su trabajo, y la que demuestra que el
        #: arreglo no fue subir el plazo.
        self.reused: bool = True

    # httpcore llama a esto con (nombre_del_evento, info)
    async def __call__(self, name: str, info: Dict[str, Any]) -> None:
        ahora = time.perf_counter()
        self._marks[name] = ahora
        if name == "connection.connect_tcp.started":
            self.reused = False
        elif name == "connection.connect_tcp.complete":
            inicio = self._marks.get("connection.connect_tcp.started")
            if inicio is not None:
                self.connect_s = max(0.0, ahora - inicio)
        elif name == "connection.start_tls.complete":
            inicio = self._marks.get("connection.start_tls.started")
            if inicio is not None:
                self.tls_s = max(0.0, ahora - inicio)
        elif name == "http11.send_request_body.complete":
            inicio = self._marks.get("http11.send_request_headers.started")
            if inicio is not None:
                self.write_s = max(0.0, ahora - inicio)
        elif name == "http11.receive_response_headers.complete":
            inicio = (self._marks.get("http11.send_request_body.complete")
                      or self._marks.get("http11.send_request_headers.started"))
            if inicio is not None:
                self.read_s = max(0.0, ahora - inicio)

    def finish(self) -> "TransportTrace":
        self.total_s = max(0.0, time.perf_counter() - self._t0)
        # La espera en el pool es lo que pasó ANTES de tocar la red. Con conexión
        # reutilizada no hay handshake que descontar, así que es lo que va desde
        # que se pidió hasta que se empezó a enviar.
        inicio_envio = self._marks.get("http11.send_request_headers.started")
        if inicio_envio is not None:
            previo = (self._marks.get("connection.connect_tcp.started")
                      or inicio_envio)
            self.pool_wait_s = max(0.0, previo - self._t0)
        return self

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pool_wait_ms": _ms(self.pool_wait_s),
            "connect_ms": _ms(self.connect_s),
            "tls_ms": _ms(self.tls_s),
            "write_ms": _ms(self.write_s),
            "read_ms": _ms(self.read_s),
            "request_ms": _ms(self.total_s),
            "connection_reused": self.reused,
        }


class HostTransport:
    """El transporte de UN host: plazo de conexión, cortacircuitos y cuentas.

    La unidad es el HOST y no el endpoint, y ésa es la corrección de fondo. Un
    handshake no es de `net_drift` ni de `gamma`: es del host. Medirlo por
    endpoint reparte treinta y seis veces la misma muestra y hace falta treinta
    y seis veces más tráfico para aprender lo mismo.
    """

    def __init__(self, host: str, *, warm_start: float = CONNECT_WARM_START_S) -> None:
        self.host = str(host)
        self._lock = threading.RLock()
        self._warm_start = max(CONNECT_FLOOR_S, min(CONNECT_CEILING_S, float(warm_start)))
        self._connect_samples: List[float] = []
        self._tls_samples: List[float] = []
        self._pool_waits: List[float] = []
        #: Suelo que sube cuando un plazo se agota y baja cuando vuelve a haber
        #: handshakes buenos. Es lo que impide que la falta de muestras sea una
        #: trampa cerrada.
        self._escalado = 0.0
        self._connect_timeouts = 0
        self._pool_timeouts = 0
        self._host_failures = 0
        self._consecutive = 0
        self._handshakes = 0
        self._reused = 0
        self._state = CLOSED
        self._open_until = 0.0
        self._open_seconds = HOST_OPEN_SECONDS
        self._retries_left = CONNECT_RETRY_BUDGET
        self._retries_used = 0
        self._last_error = ""
        self._last_phase = ""

    # ── medición ────────────────────────────────────────────────────────────

    def note_trace(self, trace: "TransportTrace") -> None:
        """Una petición que llegó a hablar con el host."""
        with self._lock:
            if trace.connect_s is not None:
                self._connect_samples.append(float(trace.connect_s))
                del self._connect_samples[:-CONNECT_WINDOW]
                self._handshakes += 1
            if trace.tls_s is not None:
                self._tls_samples.append(float(trace.tls_s))
                del self._tls_samples[:-CONNECT_WINDOW]
            if trace.pool_wait_s is not None:
                self._pool_waits.append(float(trace.pool_wait_s))
                del self._pool_waits[:-CONNECT_WINDOW]
            if trace.reused:
                self._reused += 1

    def note_success(self) -> None:
        """Se alcanzó el host. El cortacircuitos de transporte se cierra."""
        with self._lock:
            self._consecutive = 0
            if self._state != CLOSED:
                self._state = CLOSED
                self._open_seconds = HOST_OPEN_SECONDS
            # Y el suelo escalado se relaja: si vuelve a conectar bien, no hay
            # razón para seguir concediendo quince segundos.
            if self._escalado > 0.0:
                self._escalado = max(0.0, self._escalado * CONNECT_DECAY)
                if self._escalado <= CONNECT_FLOOR_S:
                    self._escalado = 0.0

    def note_connect_timeout(self, limit_seconds: float) -> None:
        """El plazo de conexión se agotó: SUBE el suelo del siguiente intento.

        Sin esto no hay forma de salir: el plazo corto impide completar el
        handshake, la falta de handshake impide medir, y la falta de medidas
        mantiene el plazo corto.
        """
        with self._lock:
            self._connect_timeouts += 1
            self._host_failures += 1
            self._consecutive += 1
            self._last_phase = CONNECT
            self._last_error = f"connect timeout a {float(limit_seconds):.1f}s"
            base = max(float(limit_seconds), self._escalado, self._warm_start)
            self._escalado = min(CONNECT_CEILING_S, base * CONNECT_ESCALATION)
            self._trip_if_needed()

    def note_pool_timeout(self) -> None:
        """Nuestro pool estaba lleno. NO es el host, y no abre su circuito."""
        with self._lock:
            self._pool_timeouts += 1
            self._last_phase = POOL
            self._last_error = "sin hueco en el pool propio"

    def note_host_failure(self, detail: str = "") -> None:
        """Fallo que impide llegar al host: DNS, red, TLS rechazado."""
        with self._lock:
            self._host_failures += 1
            self._consecutive += 1
            self._last_phase = CONNECT
            self._last_error = str(detail or "fallo de red hacia el host")[:180]
            self._trip_if_needed()

    def _trip_if_needed(self) -> None:
        if self._consecutive >= HOST_FAILURE_THRESHOLD and self._state != OPEN:
            self._state = OPEN
            self._open_until = time.monotonic() + self._open_seconds
            self._open_seconds = min(HOST_OPEN_SECONDS_MAX, self._open_seconds * 2.0)

    # ── decisiones ──────────────────────────────────────────────────────────

    def connect_timeout(self) -> float:
        """El plazo de conexión vigente para este host, y de dónde sale."""
        return self.connect_decision()["seconds"]

    def connect_decision(self) -> Dict[str, Any]:
        with self._lock:
            p95 = _percentil(self._connect_samples, 0.95)
            muestras = len(self._connect_samples)
            if p95 is not None and muestras >= MIN_CONNECT_SAMPLES:
                valor = p95 * CONNECT_P95_FACTOR
                fuente = "MEASURED_P95_HANDSHAKE"
            else:
                valor = self._warm_start
                fuente = "WARM_START"
            if self._escalado > valor:
                valor = self._escalado
                fuente = "ESCALATED_AFTER_TIMEOUT"
            segundos = max(CONNECT_FLOOR_S, min(CONNECT_CEILING_S, valor))
            return {
                "seconds": round(segundos, 3),
                "source": fuente,
                "samples": muestras,
                "p95_handshake_ms": _ms(p95),
                "escalated_floor_s": (round(self._escalado, 3)
                                      if self._escalado else None),
                "detail": {
                    "MEASURED_P95_HANDSHAKE": (
                        f"p95 del handshake × {CONNECT_P95_FACTOR:g}, medido "
                        f"sobre {muestras} conexiones de este host"),
                    "WARM_START": (
                        f"arranque sin muestras suficientes "
                        f"({muestras}/{MIN_CONNECT_SAMPLES})"),
                    "ESCALATED_AFTER_TIMEOUT": (
                        "un plazo anterior se agotó: se concede más para que el "
                        "handshake pueda completarse y dejar una muestra"),
                }[fuente],
            }

    def allows(self) -> Dict[str, Any]:
        """¿Se puede intentar llegar a este host ahora?"""
        with self._lock:
            if self._state != OPEN:
                return {"allowed": True, "state": self._state, "reason": ""}
            restante = self._open_until - time.monotonic()
            if restante > 0:
                return {
                    "allowed": False, "state": OPEN,
                    "seconds_remaining": round(restante, 2),
                    "reason": (f"transporte hacia {self.host} en cortocircuito "
                               f"tras {self._consecutive} fallos de host; "
                               f"reabre en {restante:.0f} s"),
                }
            self._state = HALF_OPEN
            return {"allowed": True, "state": HALF_OPEN,
                    "reason": "una petición de prueba"}

    def start_cycle(self) -> None:
        """Nuevo ciclo: se repone el presupuesto de reintentos de conexión."""
        with self._lock:
            self._retries_left = CONNECT_RETRY_BUDGET

    def retry_connect(self, *, burst: bool = False,
                      deadline_left_s: Optional[float] = None) -> Dict[str, Any]:
        """¿Se concede UN reintento de conexión, y con cuánta espera?

        Cuatro condiciones, y las cuatro tienen su motivo:

          · presupuesto por ciclo  → un reintento por petición duplicaría la
                                     estampida que causó el fallo;
          · nunca durante ráfaga   → la ráfaga ya está usando todo el enlace;
          · circuito cerrado       → si el host está caído, reintentar es ruido;
          · con tiempo de sobra    → reintentar para morir igual no ayuda.
        """
        with self._lock:
            if burst:
                return {"retry": False, "reason":
                        "ráfaga en curso: reintentar ahora agrava la estampida "
                        "de conexión que es la causa del fallo"}
            if self._retries_left <= 0:
                return {"retry": False, "reason":
                        f"presupuesto de reintentos de conexión agotado en este "
                        f"ciclo ({CONNECT_RETRY_BUDGET})"}
            if self._state == OPEN:
                return {"retry": False, "reason":
                        "circuito de transporte abierto: el host no está "
                        "respondiendo y reintentar sólo añade ruido"}
            espera = CONNECT_RETRY_BACKOFF_S + random.uniform(0.0, CONNECT_RETRY_JITTER_S)
            if deadline_left_s is not None and deadline_left_s <= espera:
                return {"retry": False, "reason":
                        f"no cabe el reintento en lo que queda de ciclo "
                        f"({deadline_left_s:.1f}s)"}
            self._retries_left -= 1
            self._retries_used += 1
            return {"retry": True, "sleep_seconds": round(espera, 3),
                    "budget_left": self._retries_left,
                    "reason": "un reintento de conexión con backoff y jitter"}

    # ── telemetría ──────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            decision = self.connect_decision()
            total = self._handshakes + self._reused
            return {
                "host": self.host,
                "connect_timeout_s": decision["seconds"],
                "connect_timeout_source": decision["source"],
                "connect_timeout_detail": decision["detail"],
                "connect_p95_ms": decision["p95_handshake_ms"],
                "connect_samples": decision["samples"],
                "escalated_floor_s": decision["escalated_floor_s"],
                "tls_p95_ms": _ms(_percentil(self._tls_samples, 0.95)),
                "pool_wait_p95_ms": _ms(_percentil(self._pool_waits, 0.95)),
                "handshakes": self._handshakes,
                "reused_connections": self._reused,
                # La cifra que dice si el keep-alive está funcionando: con el
                # pool compartido y 90 s de keep-alive tiene que subir hacia el
                # 90 % y quedarse ahí. Si baja, cada ciclo vuelve a pagar TLS.
                "reuse_pct": (round(100.0 * self._reused / total, 2) if total else None),
                "connect_timeouts": self._connect_timeouts,
                "pool_timeouts": self._pool_timeouts,
                "host_failures": self._host_failures,
                "consecutive_failures": self._consecutive,
                "breaker": self._state,
                "open_seconds_remaining": (round(max(0.0, self._open_until - time.monotonic()), 2)
                                           if self._state == OPEN else 0.0),
                "connect_retries_used": self._retries_used,
                "connect_retry_budget": CONNECT_RETRY_BUDGET,
                "last_phase": self._last_phase,
                "last_error": self._last_error,
            }

    def reset(self) -> None:
        with self._lock:
            self.__init__(self.host, warm_start=self._warm_start)   # noqa: PLC2801


class TransportRegistry:
    """Los hosts, y el pool compartido de cada uno.

    UN `httpx.AsyncClient` por host para TODO el proceso. Que cada carril
    tuviera el suyo significaba dos pools, dos juegos de conexiones y el doble
    de handshakes contra el mismo sitio, sin que ninguno de los dos pudiera
    aprovechar lo que el otro ya tenía abierto.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hosts: Dict[str, HostTransport] = {}
        self._clients: Dict[str, Any] = {}
        self._refs: Dict[str, int] = {}

    def host(self, base_url: str,
             warm_start: Optional[float] = None) -> HostTransport:
        """El transporte de este host, creándolo la primera vez.

        `warm_start` sólo se aplica al crearlo: una vez hay medidas, quien manda
        es el p95 del handshake, y dejar que una variable de entorno lo pisara a
        mitad de sesión devolvería el problema que este módulo cierra.
        """
        clave = host_key(base_url)
        with self._lock:
            ht = self._hosts.get(clave)
            if ht is None:
                ht = HostTransport(clave, warm_start=(CONNECT_WARM_START_S
                                                      if warm_start is None
                                                      else float(warm_start)))
                self._hosts[clave] = ht
            return ht

    # ── pool compartido ─────────────────────────────────────────────────────

    def acquire(self, base_url: str, factory) -> Any:
        """El cliente compartido de este host, creándolo la primera vez."""
        clave = host_key(base_url)
        with self._lock:
            cliente = self._clients.get(clave)
            if cliente is None:
                cliente = factory()
                self._clients[clave] = cliente
            self._refs[clave] = self._refs.get(clave, 0) + 1
            return cliente

    def release(self, base_url: str) -> Any:
        """Suelta una referencia. Devuelve el cliente a cerrar, o `None`.

        Con refcuenta porque los dos carriles comparten el pool: que el primero
        en parar cerrara el cliente dejaría al otro sin transporte a mitad de
        ciclo.
        """
        clave = host_key(base_url)
        with self._lock:
            if clave not in self._refs:
                return None
            self._refs[clave] -= 1
            if self._refs[clave] > 0:
                return None
            self._refs.pop(clave, None)
            return self._clients.pop(clave, None)

    def start_cycle(self) -> None:
        with self._lock:
            hosts = list(self._hosts.values())
        for h in hosts:
            h.start_cycle()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            hosts = [h.snapshot() for h in self._hosts.values()]
            abiertos = sorted(self._refs.keys())
        return {
            "contract": "ITMQ_TRANSPORT_V1",
            "hosts": sorted(hosts, key=lambda h: h["host"]),
            "shared_clients": abiertos,
            "policy": {
                "keepalive_expiry_s": KEEPALIVE_EXPIRY_S,
                "connect_floor_s": CONNECT_FLOOR_S,
                "connect_ceiling_s": CONNECT_CEILING_S,
                "connect_warm_start_s": CONNECT_WARM_START_S,
                "connect_p95_factor": CONNECT_P95_FACTOR,
                "write_timeout_s": WRITE_TIMEOUT_S,
                "note": ("el plazo de CONEXIÓN es del HOST y el de LECTURA del "
                         "ENDPOINT: un handshake no pertenece a ninguna "
                         "herramienta, y una respuesta lenta no dice nada sobre "
                         "la red"),
            },
        }

    def reset(self) -> None:
        with self._lock:
            self._hosts.clear()
            self._clients.clear()
            self._refs.clear()


def host_key(base_url: str) -> str:
    """El host, sin esquema ni ruta. Es la unidad del transporte."""
    texto = str(base_url or "").strip()
    for prefijo in ("https://", "http://"):
        if texto.lower().startswith(prefijo):
            texto = texto[len(prefijo):]
            break
    return texto.split("/", 1)[0].strip().lower() or "desconocido"


def pool_limits(max_inflight: int) -> Dict[str, Any]:
    """Un pool COHERENTE con el techo de concurrencia, y no un número suelto.

    Si el pool admite menos conexiones que peticiones vivas permite el
    gobernador, la diferencia se convierte en espera en el pool —que se lee como
    lentitud del proveedor y no lo es—. Y si admite muchas más, el techo de
    concurrencia deja de ser un techo: la estampida vuelve por el otro lado.

    Por eso el pool se dimensiona DESDE el techo: exactamente las que pueden
    estar vivas, más dos de holgura para el relevo entre ciclos.
    """
    techo = max(1, int(max_inflight))
    return {
        "max_connections": techo + 2,
        "max_keepalive_connections": techo + 2,
        "keepalive_expiry": KEEPALIVE_EXPIRY_S,
        # Esperar sitio en el pool más de esto significa que el techo de
        # concurrencia no se está respetando aguas arriba: es un defecto
        # NUESTRO, y tiene que salir como tal y no como lentitud del proveedor.
        "pool_timeout_s": round(max(2.0, min(10.0, techo * 1.5)), 2),
    }


TRANSPORT = TransportRegistry()

__all__ = [
    "TRANSPORT", "TransportRegistry", "HostTransport", "TransportTrace",
    "host_key", "pool_limits",
    "CONNECT", "POOL", "READ", "WRITE", "PHASES",
    "CONNECT_FLOOR_S", "CONNECT_CEILING_S", "CONNECT_WARM_START_S",
    "CONNECT_P95_FACTOR", "CONNECT_ESCALATION", "MIN_CONNECT_SAMPLES",
    "KEEPALIVE_EXPIRY_S", "WRITE_TIMEOUT_S", "CONNECT_RETRY_BUDGET",
    "HOST_FAILURE_THRESHOLD", "CLOSED", "OPEN", "HALF_OPEN",
]
