"""RUNTIME POR ENDPOINT · un canal lento no puede arrastrar a los demás.

═══════════════════════════════════════════════════════════════════════════
LOS TRES DEFECTOS QUE ESTO CIERRA
═══════════════════════════════════════════════════════════════════════════

1 · UN SOLO PLAZO PARA TODOS

    `QUANTDATA_TIMEOUT_SECONDS` valía lo mismo para las treinta y seis
    herramientas. Un plazo fijo se equivoca en las DOS direcciones a la vez:

      · demasiado paciente con un endpoint que contesta en 200 ms —se esperan
        diez segundos para enterarse de que está muerto, y ese turno se lo
        quitas a los que sí iban a contestar—;
      · demasiado impaciente con uno que legítimamente tarda ocho segundos
        —se le corta cuando estaba a punto de responder, se marca como fallo y
        se reintenta, gastando cuota dos veces para no obtener nada—.

    El plazo se MIDE ahora. Cada endpoint acumula sus latencias reales y su
    plazo sale del percentil 95 con margen. Hasta tener muestra suficiente se
    usa el configurado: medir con dos datos es peor que no medir.

2 · BACKOFF SIN JITTER

    `delay = base * 2**n`, idéntico para todos. Cuando media docena de
    endpoints falla en el mismo ciclo —que es lo que pasa cuando la red hipa—
    los seis reintentan EN EL MISMO INSTANTE, contra la misma cuenta. Eso
    reproduce exactamente la congestión que causó el fallo.

    Con jitter completo el reintento cae en un punto al azar de la ventana, así
    que la manada se dispersa sola.

3 · SIN CORTACIRCUITOS

    Había un `unavailable_until` plano: se espera y se vuelve a intentar, para
    siempre. Un endpoint caído sigue consumiendo un turno de cada ronda —turno
    que necesitan los sanos— y su fallo se repite idéntico cada vez.

    Un cortacircuitos deja de LLAMAR, no de MOSTRAR. Es la distinción que
    importa: el último valor bueno se sigue publicando con su edad mientras el
    canal está abierto.

═══════════════════════════════════════════════════════════════════════════
AISLAMIENTO
═══════════════════════════════════════════════════════════════════════════

El estado es POR ENDPOINT y no hay estructura compartida que pueda
contaminarse. Que `dark_flow` tenga el cortacircuitos abierto no cambia ni un
byte del estado de `gex_by_strike`. Hay una prueba que lo comprueba en las
treinta y seis a la vez.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# ── plazo calibrado ────────────────────────────────────────────────────────
#: Cuántas latencias se guardan por endpoint. Suficientes para que el p95
#: signifique algo y pocas para que el plazo siga a un cambio real de la API.
LATENCY_WINDOW = 40

#: Por debajo de esto no se calibra: se usa el plazo configurado. Un p95 con
#: tres muestras es una anécdota, no una medición.
MIN_SAMPLES_TO_CALIBRATE = 8

#: Margen sobre el p95. 1,5 deja sitio a la variación normal sin premiar a un
#: endpoint que se está degradando.
TIMEOUT_SAFETY_FACTOR = 1.5

#: Suelo y techo del plazo calibrado. El suelo impide que un endpoint muy
#: rápido se corte a sí mismo en cuanto hipe la red; el techo impide que uno
#: que se degrada poco a poco acabe bloqueando la ronda entera.
TIMEOUT_FLOOR_S = 2.0
TIMEOUT_CEILING_S = 20.0

# ── warm start · v1.58.0 ───────────────────────────────────────────────────
#: Plazo de LECTURA de un endpoint SIN historia. Es una política del producto,
#: no una variable de entorno.
#:
#: Hasta v1.57.2 este valor salía de `QUANTDATA_TIMEOUT_SECONDS`, y con el 5 que
#: reparte el instalador TODO endpoint sin muestras empezaba en cinco segundos.
#: El plazo «adaptativo» no gobernaba nada: los endpoints pesados morían a 5.0 s
#: en cada ciclo y, como un timeout sólo aporta UNA muestra y el cortacircuitos
#: abre a los cuatro fallos seguidos, calibrar tardaba minutos. La consola de
#: producción lo demostró: `delta`, `gamma`, `net_flow`, `net_drift`,
#: `market_share`, `contract_trade_side_statistics` y los dos order flow, todos
#: cortados exactamente en 5.0 s.
#:
#: Doce segundos es el arranque seguro: cabe en el ciclo de 15 s con su holgura y
#: da margen a `interval-map`, `order-flow-raw` y la exposición por vencimiento.
#: En cuanto hay muestras manda el p95 medido, que baja solo a los rápidos.
WARM_START_READ_S = 12.0

#: Plazo de CONEXIÓN, independiente del de lectura.
#:
#: Son dos fallos distintos y cada uno tiene su escala: establecer la conexión es
#: un ida y vuelta de red y leer la respuesta depende de cuánto tarde el
#: proveedor en calcularla. Un solo número para los dos obliga a elegir: con
#: cinco segundos se corta al endpoint que iba a responder en nueve; con doce se
#: esperan doce a un host que no está.
#:
#: v1.60.0 · ESTE MÓDULO YA NO ES SU AUTORIDAD. El plazo de conexión lo gobierna
#: el HOST —`transport_runtime`—, con el p95 del handshake MEDIDO y escalada
#: cuando se agota. Aquí queda sólo el valor de arranque, y se lee de allí para
#: que no haya dos números distintos diciendo ser el mismo plazo. El 4.0 fijo
#: que produjo `connect timed out after 4.0s` en cuatro endpoints a la vez ya no
#: existe en ningún sitio.
from .transport_runtime import CONNECT_WARM_START_S as _CONNECT_WARM_START

CONNECT_TIMEOUT_S = _CONNECT_WARM_START

#: Peso de la última muestra en la EWMA. La EWMA NO fija el plazo: sirve para
#: detectar que un endpoint se está degradando antes de que el p95 lo note.
EWMA_ALPHA = 0.3

#: Cuánto tiene que superar la EWMA al p95 para declarar deriva.
DRIFT_FACTOR = 1.5

# ── backoff ────────────────────────────────────────────────────────────────
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 300.0

# ── cortacircuitos ─────────────────────────────────────────────────────────
CLOSED = "CLOSED"        # se llama con normalidad
OPEN = "OPEN"            # no se llama; se sirve el último valor bueno
HALF_OPEN = "HALF_OPEN"  # se deja pasar UNA llamada de prueba

BREAKER_STATES = (CLOSED, OPEN, HALF_OPEN)

#: Fallos consecutivos que abren el circuito.
FAILURE_THRESHOLD = 4

#: Cuánto permanece abierto la primera vez. Cada reapertura lo duplica hasta el
#: techo: un endpoint que lleva media hora caído no merece un intento por minuto.
OPEN_SECONDS = 30.0
OPEN_SECONDS_MAX = 600.0


class Deadline:
    """Plazo de una petición, con sus CUATRO fases separadas.

    Viaja como objeto y no como número porque son cuatro plazos con cuatro
    causas distintas, y cada una se arregla al revés que las otras:

        connect  alcanzar al host      → red, DNS, TLS. Autoridad: el HOST
        read     que el proveedor
                 termine de contestar  → cálculo del endpoint. Autoridad: el p95
                                          POR ENDPOINT
        write    terminar de enviar    → enlace de subida
        pool     encontrar hueco en
                 NUESTRO pool          → congestión PROPIA, nunca del proveedor

    `connect` y `read` no comparten autoridad a propósito: un handshake no
    pertenece a ninguna herramienta —es del host— y una respuesta lenta no dice
    nada sobre la red. v1.60.0.

    `write` y `pool` admiten `None`: entonces los pone el transporte, que es
    quien conoce el tamaño del pool.
    """

    __slots__ = ("connect", "read", "write", "pool", "source")

    def __init__(self, *, connect: float, read: float, source: str = "",
                 write: Optional[float] = None,
                 pool: Optional[float] = None) -> None:
        self.connect = max(0.5, float(connect))
        self.read = max(TIMEOUT_FLOOR_S, float(read))
        self.write = (None if write is None else max(0.5, float(write)))
        self.pool = (None if pool is None else max(0.5, float(pool)))
        self.source = str(source)

    @property
    def total(self) -> float:
        """Lo máximo que puede vivir la petición: conectar y después leer."""
        return self.connect + self.read

    def with_transport(self, *, connect: float, write: Optional[float] = None,
                       pool: Optional[float] = None,
                       source: str = "") -> "Deadline":
        """El mismo plazo de LECTURA, con las fases que decide el transporte."""
        return Deadline(connect=connect, read=self.read,
                        write=(self.write if write is None else write),
                        pool=(self.pool if pool is None else pool),
                        source=(source or self.source))

    def as_dict(self) -> Dict[str, Any]:
        return {"connect_s": round(self.connect, 3), "read_s": round(self.read, 3),
                "write_s": (None if self.write is None else round(self.write, 3)),
                "pool_s": (None if self.pool is None else round(self.pool, 3)),
                "total_s": round(self.total, 3), "source": self.source}

    def __repr__(self) -> str:   # pragma: no cover - diagnóstico
        return (f"Deadline(connect={self.connect:.2f}s, read={self.read:.2f}s, "
                f"source={self.source!r})")

    def __eq__(self, other: Any) -> bool:
        return (isinstance(other, Deadline) and self.connect == other.connect
                and self.read == other.read)


def _percentile(valores: List[float], q: float) -> Optional[float]:
    """Percentil por interpolación lineal. `None` sin muestras."""
    if not valores:
        return None
    xs = sorted(valores)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * max(0.0, min(1.0, q))
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return float(xs[lo] * (1.0 - frac) + xs[hi] * frac)


# ── presupuesto de reintento · v1.58.0 ────────────────────────────────────
#: Clases de fallo que un reintento PUEDE arreglar: el mismo cuerpo, la misma
#: ruta y las mismas credenciales, con otro momento.
RETRYABLE = frozenset({"TIMEOUT", "TRANSIENT", "PROVIDER_ERROR", "RATE_LIMITED"})

#: Clases que un reintento NO arregla nunca. Reintentar un 400 es repetir un
#: cuerpo que el proveedor ya rechazó; reintentar un 401/403 es volver a
#: presentar la misma credencial; un 404 es una ruta que no existe. Los tres
#: gastan cuota para obtener el mismo error, y encima ensucian el diagnóstico.
NEVER_RETRY = frozenset({"REQUEST_INVALID", "UNAUTHORIZED", "FORBIDDEN",
                         "MISSING_TOOL", "NO_DATA"})

#: Un reintento por endpoint y por ciclo. Dos reintentos del mismo endpoint en el
#: mismo ciclo son tres peticiones para un dato: es la congestión que el
#: gobernador existe para impedir.
MAX_RETRIES_PER_CYCLE = 1


def retry_plan(runtime: "EndpointRuntime", *, status: str, in_burst: bool,
               cycle_remaining_s: Optional[float],
               deadline: Optional[Deadline] = None,
               free_slot: bool = True,
               rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """¿Se reintenta? Devuelve la decisión, el motivo y la espera.

    ═══════════════════════════════════════════════════════════════════════
    UN REINTENTO SIN PRESUPUESTO ES OTRA PETICIÓN, NO UNA CORRECCIÓN
    ═══════════════════════════════════════════════════════════════════════

    Reintentar un timeout duplica la petición contra la misma cuenta: dos
    sockets vivos, dos unidades de cuota y dos veces la latencia, para un dato.
    Cuando el proveedor va lento —que es cuando hay timeouts— eso es exactamente
    la avalancha que provoca los siguientes timeouts.

    Así que el reintento exige las CINCO condiciones a la vez, y cuando falta
    una se dice cuál:

        1. el fallo es de una clase que un reintento pueda arreglar;
        2. el cortacircuitos está CERRADO —si está abierto, el endpoint ya está
           declarado caído y el reintento es ruido—;
        3. no estamos en la ráfaga de arranque —ahí la prioridad es cubrir la
           pantalla entera una vez, no insistir en un endpoint—;
        4. queda tiempo de ciclo para que el reintento termine dentro;
        5. hay hueco de concurrencia AHORA; un reintento que espera cola compite
           con el ciclo siguiente.
    """
    st = str(status or "").upper()
    espera = 0.0
    def _no(motivo: str) -> Dict[str, Any]:
        return {"retry": False, "reason": motivo, "delay_seconds": 0.0,
                "status": st}

    if st in NEVER_RETRY:
        return _no(f"{st} no se arregla reintentando")
    if st not in RETRYABLE:
        return _no(f"clase de fallo no reintentable: {st or 'DESCONOCIDA'}")
    if in_burst:
        return _no("ráfaga de arranque: primero se cubre la pantalla entera")
    estado = runtime.snapshot()["breaker"]
    if estado != CLOSED:
        return _no(f"cortacircuitos {estado}: el endpoint ya está declarado caído")
    if not free_slot:
        return _no("sin hueco de concurrencia: el reintento haría cola")
    if runtime.retried_this_cycle():
        return _no(f"ya se reintentó en este ciclo (máximo {MAX_RETRIES_PER_CYCLE})")
    espera = backoff_delay(1, rng=rng)
    necesario = espera + (deadline.total if deadline is not None else 0.0)
    if cycle_remaining_s is not None and cycle_remaining_s < necesario:
        return _no(f"no cabe en el ciclo: hacen falta {necesario:.1f} s y quedan "
                   f"{float(cycle_remaining_s):.1f} s")
    return {"retry": True, "reason": "presupuesto disponible",
            "delay_seconds": round(espera, 3), "status": st}


def backoff_delay(failures: int, *, base: float = BACKOFF_BASE_S,
                  cap: float = BACKOFF_CAP_S,
                  rng: Optional[random.Random] = None) -> float:
    """Espera exponencial con JITTER COMPLETO.

    `random(0, min(cap, base * 2**(n-1)))`, el esquema de jitter completo.

    Sin el azar, media docena de endpoints que falla en el mismo ciclo reintenta
    en el mismo instante contra la misma cuenta, que es justo la congestión que
    provocó el fallo. Con él, la manada se dispersa sola.
    """
    n = max(0, int(failures))
    if n <= 0:
        return 0.0
    techo = min(float(cap), float(base) * (2 ** (n - 1)))
    r = rng or random
    return float(r.uniform(0.0, techo))


class EndpointRuntime:
    """Plazo, reintento y cortacircuitos de UN endpoint. Nada compartido."""

    __slots__ = ("key", "default_timeout", "_lat", "_failures", "_state",
                 "_open_until", "_open_for", "_probe_in_flight", "_last_error",
                 "_last_status", "_last_success_at", "_last_attempt_at",
                 "_opens", "_timeouts", "_ewma", "_connect", "_retried_at",
                 "_lock")

    def __init__(self, key: str, *, default_timeout: float = WARM_START_READ_S,
                 connect_timeout: float = CONNECT_TIMEOUT_S) -> None:
        self.key = str(key)
        # `default_timeout` es el WARM START de lectura: el plazo mientras no hay
        # muestras. Nunca por debajo del suelo de política.
        self.default_timeout = max(TIMEOUT_FLOOR_S, float(default_timeout))
        self._connect = max(0.5, float(connect_timeout))
        self._lat: List[float] = []
        self._failures = 0
        self._state = CLOSED
        self._open_until = 0.0
        self._open_for = OPEN_SECONDS
        self._probe_in_flight = False
        self._last_error: Optional[str] = None
        self._last_status: Optional[str] = None
        self._last_success_at: Optional[float] = None
        self._last_attempt_at: Optional[float] = None
        self._opens = 0
        self._timeouts = 0
        self._ewma: Optional[float] = None
        self._retried_at: Optional[float] = None
        self._lock = threading.Lock()

    # ── plazo ──────────────────────────────────────────────────────────────
    def timeout(self) -> float:
        """Plazo de LECTURA vigente: el p95 medido, o el warm start si no hay.

        La autoridad es el **p95**, no la EWMA. Son dos preguntas distintas: el
        p95 dice «cuánto tarda este endpoint cuando va bien, con margen» y es
        estable; la EWMA dice «cómo va AHORA» y se mueve con cada muestra. Un
        plazo gobernado por la EWMA oscila, y un plazo que oscila corta
        peticiones sanas en cuanto hay una racha lenta. La EWMA se publica y se
        usa para declarar DERIVA —ver `drift()`—, nunca para fijar el plazo.
        """
        with self._lock:
            if len(self._lat) < MIN_SAMPLES_TO_CALIBRATE:
                return self.default_timeout
            p95 = _percentile(self._lat, 0.95)
        if p95 is None or p95 <= 0:
            return self.default_timeout
        return max(TIMEOUT_FLOOR_S, min(TIMEOUT_CEILING_S, p95 * TIMEOUT_SAFETY_FACTOR))

    def deadline(self) -> Deadline:
        """El plazo completo de una petición: conexión y lectura, por separado."""
        with self._lock:
            calibrado = len(self._lat) >= MIN_SAMPLES_TO_CALIBRATE
            conexion = self._connect
        return Deadline(connect=conexion, read=self.timeout(),
                        source=("MEASURED_P95" if calibrado else "WARM_START"))

    def drift(self) -> Dict[str, Any]:
        """¿Se está degradando? EWMA contra p95, sin tocar el plazo.

        Detectar la degradación y reaccionar a ella son cosas distintas: esto la
        NOMBRA para que el Auditor la enseñe y el operador decida, en vez de
        dejar que el plazo la persiga y acabe cortando a todos.
        """
        with self._lock:
            ewma = self._ewma
            p50 = _percentile(self._lat, 0.50)
            p95 = _percentile(self._lat, 0.95)
            muestras = len(self._lat)
        # La deriva se mide contra la MEDIANA, no contra el p95. El p95 de una
        # ventana de cuarenta muestras salta en cuanto dos van lentas —su cola es
        # el 5 %, o sea dos muestras—, así que comparar la EWMA con él no avisa
        # de nada: los dos suben juntos. La mediana aguanta un par de valores
        # atípicos, y eso es justo lo que permite ver el deterioro EMPEZANDO.
        derivando = bool(ewma is not None and p50 and muestras >= MIN_SAMPLES_TO_CALIBRATE
                         and ewma > p50 * DRIFT_FACTOR)
        return {
            "ewma_seconds": None if ewma is None else round(ewma, 4),
            "latency_p50": None if p50 is None else round(p50, 4),
            "latency_p95": None if p95 is None else round(p95, 4),
            "drifting": derivando,
            "factor": DRIFT_FACTOR,
            "compared_against": "latency_p50",
            "detail": ("la latencia reciente supera la mediana con margen: el endpoint "
                       "se está degradando" if derivando else ""),
        }

    def calibrated(self) -> bool:
        with self._lock:
            return len(self._lat) >= MIN_SAMPLES_TO_CALIBRATE

    # ── permiso de llamada ─────────────────────────────────────────────────
    def allow(self, now: Optional[float] = None) -> Tuple[bool, str]:
        """¿Se puede llamar ahora? Devuelve el permiso y el motivo.

        El motivo viaja siempre: un «no» sin causa no se puede diagnosticar.
        """
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            if self._state == CLOSED:
                return True, CLOSED
            if self._state == OPEN:
                if t < self._open_until:
                    return False, OPEN
                # Se agotó la apertura: se deja pasar UNA prueba.
                self._state = HALF_OPEN
                self._probe_in_flight = True
                return True, HALF_OPEN
            # HALF_OPEN: sólo una prueba en vuelo. Mandar varias convertiría la
            # prueba en la avalancha que el cortacircuitos existe para evitar.
            if self._probe_in_flight:
                return False, HALF_OPEN
            self._probe_in_flight = True
            return True, HALF_OPEN

    # ── resultados ─────────────────────────────────────────────────────────
    def record_success(self, latency_seconds: float,
                       now: Optional[float] = None) -> None:
        t = time.monotonic() if now is None else float(now)
        lat = float(latency_seconds)
        with self._lock:
            self._last_attempt_at = t
            self._last_success_at = t
            self._last_error = None
            self._last_status = "OK"
            self._failures = 0
            self._probe_in_flight = False
            self._state = CLOSED
            self._open_until = 0.0
            self._open_for = OPEN_SECONDS      # la próxima apertura vuelve a empezar corta
            if lat == lat and lat >= 0:
                self._ewma = (lat if self._ewma is None
                              else EWMA_ALPHA * lat + (1.0 - EWMA_ALPHA) * self._ewma)
                self._lat.append(lat)
                if len(self._lat) > LATENCY_WINDOW:
                    del self._lat[0:len(self._lat) - LATENCY_WINDOW]

    def record_failure(self, reason: str, *, status: str = "TRANSIENT",
                       now: Optional[float] = None) -> None:
        """Un fallo de llamada. NO borra las latencias medidas.

        Las latencias describen cómo se comporta el endpoint cuando funciona.
        Tirarlas en cada fallo haría que el plazo volviera al configurado justo
        cuando más falta hace conocerlo.
        """
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            self._last_attempt_at = t
            self._last_error = str(reason)[:240]
            self._last_status = str(status)
            self._probe_in_flight = False
            if self._state == HALF_OPEN:
                # La prueba falló: se vuelve a abrir, y más tiempo.
                self._opens += 1
                self._open_for = min(OPEN_SECONDS_MAX, self._open_for * 2.0)
                self._state = OPEN
                self._open_until = t + self._open_for
                return
            self._failures += 1
            if self._failures >= FAILURE_THRESHOLD:
                self._opens += 1
                self._state = OPEN
                self._open_until = t + self._open_for

    def record_timeout(self, limit_seconds: float, *,
                       now: Optional[float] = None) -> None:
        """Un plazo agotado: cuenta como fallo Y como COTA INFERIOR de latencia.

        v1.58.1 · EL RUNTIME NO PODÍA APRENDER DE SUS PROPIOS TIMEOUTS.

        `record_failure` no toca las latencias a propósito: un 500 o un 404 no
        dicen nada sobre cuánto tarda el endpoint cuando funciona. Pero un
        TIMEOUT sí dice algo, y era justo lo que se tiraba.

        El resultado era un pozo sin salida. Un endpoint que necesita doce
        segundos, llamado con cinco, no dejaba NUNCA una muestra —porque no
        llegaba a responder—, así que nunca alcanzaba las ocho que hacen falta
        para calibrar, así que se le seguía llamando con cinco. Para siempre. El
        techo de veinte segundos que este módulo publica era inalcanzable por
        construcción, y en pantalla se leía como un proveedor caído.

        Un timeout no dice cuánto tarda el endpoint; dice que tarda MÁS que el
        plazo. Eso es una cota inferior y como tal se guarda: el plazo siguiente
        sube —acotado por `TIMEOUT_CEILING_S`— hasta que el endpoint contesta y
        sus latencias reales lo vuelven a bajar, o hasta que el cortacircuitos
        deja de llamarlo. Lo que no puede pasar es que el plazo se quede clavado
        donde se sabe que no alcanza.
        """
        lim = float(limit_seconds)
        if lim == lim and lim > 0:
            with self._lock:
                self._timeouts += 1
                self._lat.append(lim)
                if len(self._lat) > LATENCY_WINDOW:
                    del self._lat[0:len(self._lat) - LATENCY_WINDOW]
        # El fallo se cuenta igual: un endpoint que sólo da timeouts tiene que
        # acabar abriendo el circuito, no subiendo el plazo indefinidamente.
        self.record_failure(f"plazo agotado a los {lim:.1f}s",
                            status="TIMEOUT", now=now)

    def mark_retry(self, now: Optional[float] = None) -> None:
        """Anota que este endpoint ya gastó su reintento del ciclo."""
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            self._retried_at = t

    def retried_this_cycle(self) -> bool:
        with self._lock:
            return self._retried_at is not None

    def start_cycle(self) -> None:
        """Devuelve el reintento al endpoint. Lo llama el programador por ciclo."""
        with self._lock:
            self._retried_at = None

    def next_retry_in(self, rng: Optional[random.Random] = None) -> float:
        with self._lock:
            n = self._failures
        return backoff_delay(n, rng=rng)

    # ── lectura ────────────────────────────────────────────────────────────
    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Todo lo que hace falta para diagnosticar este endpoint sin abrir el código."""
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            p50 = _percentile(self._lat, 0.50)
            p95 = _percentile(self._lat, 0.95)
            calibrado = len(self._lat) >= MIN_SAMPLES_TO_CALIBRATE
            plazo = (max(TIMEOUT_FLOOR_S, min(TIMEOUT_CEILING_S, p95 * TIMEOUT_SAFETY_FACTOR))
                     if (calibrado and p95) else self.default_timeout)
            return {
                "key": self.key,
                "breaker": self._state,
                "open_seconds_remaining": (round(max(0.0, self._open_until - t), 2)
                                           if self._state == OPEN else 0.0),
                "open_window_seconds": round(self._open_for, 2),
                "times_opened": self._opens,
                "timeouts": self._timeouts,
                "consecutive_failures": self._failures,
                "failure_threshold": FAILURE_THRESHOLD,
                "timeout_seconds": round(plazo, 3),
                "connect_timeout_seconds": round(self._connect, 3),
                "read_timeout_seconds": round(plazo, 3),
                "timeout_budget_ms": round((plazo + self._connect) * 1000.0, 1),
                "timeout_source": ("MEASURED_P95" if calibrado else "WARM_START"),
                "warm_start_seconds": round(self.default_timeout, 3),
                "ewma_seconds": (None if self._ewma is None else round(self._ewma, 4)),
                "retried_this_cycle": self._retried_at is not None,
                "samples": len(self._lat),
                "samples_needed": MIN_SAMPLES_TO_CALIBRATE,
                "latency_p50": None if p50 is None else round(p50, 4),
                "latency_p95": None if p95 is None else round(p95, 4),
                "last_error": self._last_error,
                "last_status": self._last_status,
                "seconds_since_success": (None if self._last_success_at is None
                                          else round(t - self._last_success_at, 2)),
                "serving_last_known_good": self._state == OPEN,
            }


class EndpointRegistry:
    """Un runtime por clave, creado al vuelo. Nada se comparte entre endpoints."""

    def __init__(self, *, default_timeout: float = WARM_START_READ_S) -> None:
        self.default_timeout = float(default_timeout)
        self._by_key: Dict[str, EndpointRuntime] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> EndpointRuntime:
        k = str(key)
        with self._lock:
            rt = self._by_key.get(k)
            if rt is None:
                rt = self._by_key[k] = EndpointRuntime(k, default_timeout=self.default_timeout)
            return rt

    def start_cycle(self) -> None:
        """Nuevo ciclo: cada endpoint recupera su reintento."""
        with self._lock:
            runtimes = list(self._by_key.values())
        for rt in runtimes:
            rt.start_cycle()

    def set_warm_start(self, seconds: float) -> float:
        """Fija el warm start de lectura, con SUELO de política.

        v1.58.0 · Aquí estaba el defecto: cualquier valor entraba, incluido el
        `QUANTDATA_TIMEOUT_SECONDS=5` del instalador, y con él todo endpoint sin
        historia arrancaba en cinco segundos. Ahora el suelo lo pone el producto
        —`WARM_START_READ_S`— y una configuración más CORTA no puede bajarlo. Una
        más LARGA sí se respeta: quien pide más paciencia la tiene.
        """
        pedido = float(seconds)
        efectivo = max(WARM_START_READ_S, pedido)
        with self._lock:
            self.default_timeout = efectivo
            for rt in self._by_key.values():
                rt.default_timeout = efectivo
        return efectivo

    def set_default_timeout(self, seconds: float) -> None:
        """Fija el plazo configurado, también en los runtimes YA creados.

        El plazo configurado sale de `QUANTDATA_TIMEOUT_SECONDS` y el registro se
        construye al importar el módulo, antes de que haya settings. Sin esto el
        registro se quedaba con su propio valor por defecto y cada llamador
        recalculaba el suyo por su cuenta: dos autoridades para un mismo plazo,
        que es como se deslizan las discrepancias.
        """
        # v1.58.0 · Alias compatible de `set_warm_start`, con su mismo suelo: un
        # llamador antiguo ya no puede rebajar el warm start por debajo de la
        # política. Es lo que hacía el carril de páginas con el valor del `.env`.
        self.set_warm_start(float(seconds))

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            rts = list(self._by_key.values())
        filas = [rt.snapshot(now) for rt in rts]
        abiertos = [f for f in filas if f["breaker"] == OPEN]
        return {
            "endpoints": sorted(filas, key=lambda f: f["key"]),
            "count": len(filas),
            "open": sorted(f["key"] for f in abiertos),
            "half_open": sorted(f["key"] for f in filas if f["breaker"] == HALF_OPEN),
            "calibrated": sorted(f["key"] for f in filas
                                 if f["timeout_source"] == "MEASURED_P95"),
            "policy": (f"plazo = p95 x {TIMEOUT_SAFETY_FACTOR} acotado a "
                       f"[{TIMEOUT_FLOOR_S}, {TIMEOUT_CEILING_S}] s con "
                       f"{MIN_SAMPLES_TO_CALIBRATE}+ muestras; reintento con jitter "
                       f"completo; circuito abre a los {FAILURE_THRESHOLD} fallos "
                       f"seguidos y el circuito abierto NO vacía la sección"),
        }

    def reset(self) -> None:
        with self._lock:
            self._by_key.clear()


__all__ = [
    "EndpointRuntime", "EndpointRegistry", "backoff_delay", "Deadline",
    "retry_plan", "RETRYABLE", "NEVER_RETRY", "MAX_RETRIES_PER_CYCLE",
    "WARM_START_READ_S", "CONNECT_TIMEOUT_S", "EWMA_ALPHA", "DRIFT_FACTOR",
    "CLOSED", "OPEN", "HALF_OPEN", "BREAKER_STATES",
    "FAILURE_THRESHOLD", "OPEN_SECONDS", "OPEN_SECONDS_MAX",
    "LATENCY_WINDOW", "MIN_SAMPLES_TO_CALIBRATE", "TIMEOUT_SAFETY_FACTOR",
    "TIMEOUT_FLOOR_S", "TIMEOUT_CEILING_S", "BACKOFF_BASE_S", "BACKOFF_CAP_S",
]
