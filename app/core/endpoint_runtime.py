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
                 "_opens", "_lock")

    def __init__(self, key: str, *, default_timeout: float = 10.0) -> None:
        self.key = str(key)
        self.default_timeout = max(TIMEOUT_FLOOR_S, float(default_timeout))
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
        self._lock = threading.Lock()

    # ── plazo ──────────────────────────────────────────────────────────────
    def timeout(self) -> float:
        """Plazo vigente: medido si hay muestra, configurado si no."""
        with self._lock:
            if len(self._lat) < MIN_SAMPLES_TO_CALIBRATE:
                return self.default_timeout
            p95 = _percentile(self._lat, 0.95)
        if p95 is None or p95 <= 0:
            return self.default_timeout
        return max(TIMEOUT_FLOOR_S, min(TIMEOUT_CEILING_S, p95 * TIMEOUT_SAFETY_FACTOR))

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
                "consecutive_failures": self._failures,
                "failure_threshold": FAILURE_THRESHOLD,
                "timeout_seconds": round(plazo, 3),
                "timeout_source": "MEASURED_P95" if calibrado else "CONFIGURED_DEFAULT",
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

    def __init__(self, *, default_timeout: float = 10.0) -> None:
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
    "EndpointRuntime", "EndpointRegistry", "backoff_delay",
    "CLOSED", "OPEN", "HALF_OPEN", "BREAKER_STATES",
    "FAILURE_THRESHOLD", "OPEN_SECONDS", "OPEN_SECONDS_MAX",
    "LATENCY_WINDOW", "MIN_SAMPLES_TO_CALIBRATE", "TIMEOUT_SAFETY_FACTOR",
    "TIMEOUT_FLOOR_S", "TIMEOUT_CEILING_S", "BACKOFF_BASE_S", "BACKOFF_CAP_S",
]
