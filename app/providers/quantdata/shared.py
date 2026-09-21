"""Recursos compartidos entre los dos carriles de Quant Data · v1.41.1

Los dos carriles (el que hidrata el motor y el que sirve las páginas integradas)
hablan con la misma cuenta y, por tanto, con la misma cuota. Hasta v1.41.0 cada
uno la consumía por su cuenta y, además, pedían los mismos nueve endpoints:
exposición GAMMA/DELTA/VANNA/CHARM por strike, net-flow, net-drift, interval-map,
max-pain-over-time e iv-rank. El resultado era el doble de peticiones de las
necesarias, rate limit del proveedor y herramientas envejeciendo en pantalla.

Este módulo resuelve las dos mitades del problema:

* ``RAW_CACHE``  — el carril del motor publica el payload crudo de cada endpoint
  que ya pidió; el carril de páginas lo lee en lugar de repetir la petición.
  Nueve peticiones por ciclo desaparecen y esas herramientas quedan LIVE al
  instante, con exactamente el mismo dato que consume el motor.

* ``QUOTA``      — presupuesto común alimentado por las cabeceras de rate limit
  del proveedor. El carril del motor tiene prioridad: si queda poco margen, el
  de páginas cede el turno en vez de robarle el ciclo a la estructura.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Tuple


def _env_float(name: str, default: float | None) -> float | None:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except Exception:
        return default

# Margen que se reserva siempre para el carril del motor. Por debajo de este
# número de peticiones restantes, el carril de páginas no pide nada.
ENGINE_RESERVE = 12

#: Por debajo de esta ventana, la cabecera `Reset` describe un CUBO DE RITMO y no
#: el plan contratado. Cinco minutos: ningún plan se renueva más rápido, y todo
#: limitador de ritmo que hemos visto cabe por debajo.
VENTANA_CREIBLE_S = 300.0

# Un payload compartido más viejo que esto ya no representa el ciclo actual y se
# vuelve a pedir en lugar de mostrarse como si fuera fresco.
DEFAULT_MAX_AGE_S = 90.0


class RawCache:
    """Payloads crudos ya obtenidos por el carril del motor, por símbolo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}

    def put(self, key: str, symbol: str, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict) or not payload:
            return
        with self._lock:
            self._data[(str(key), str(symbol).upper())] = (time.time(), payload)

    def get(self, key: str, symbol: str, max_age_s: float = DEFAULT_MAX_AGE_S) -> Dict[str, Any] | None:
        with self._lock:
            hit = self._data.get((str(key), str(symbol).upper()))
        if not hit:
            return None
        ts, payload = hit
        if (time.time() - ts) > max(0.0, float(max_age_s)):
            return None
        return payload

    def age(self, key: str, symbol: str) -> float | None:
        with self._lock:
            hit = self._data.get((str(key), str(symbol).upper()))
        return None if not hit else max(0.0, time.time() - hit[0])

    def clear_symbol(self, symbol: str) -> None:
        sym = str(symbol).upper()
        with self._lock:
            for k in [k for k in self._data if k[1] == sym]:
                self._data.pop(k, None)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            return {
                "entries": len(self._data),
                "keys": sorted({k[0] for k in self._data}),
                "oldest_age_seconds": round(max((now - v[0] for v in self._data.values()), default=0.0), 1),
            }


class QuotaGuard:
    """Presupuesto de peticiones común, alimentado por las cabeceras del proveedor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.remaining: int | None = None
        self.limit: int | None = None
        self.reset_seconds: float | None = None
        self.updated_at: float = 0.0
        self.rate_limited_until: float = 0.0
        # Ventana del plan aprendida observando cuándo se reinicia el contador.
        self.observed_window_s: float | None = None
        self.window_samples: int = 0
        self._last_reset_at: float | None = None
        # Presupuesto declarado por el operador. Cuando existe, manda sobre
        # cualquier inferencia: es el dato que el proveedor no siempre publica bien.
        self.declared_requests: float | None = _env_float("QUANTDATA_PLAN_REQUESTS", None)
        self.declared_window_s: float | None = _env_float("QUANTDATA_PLAN_WINDOW_SECONDS", None)

    def note(self, *, remaining: int | None, limit: int | None, reset_seconds: float | None) -> None:
        now = time.time()
        with self._lock:
            prev = self.remaining
            if remaining is not None:
                # Un salto hacia ARRIBA sólo ocurre cuando el proveedor reinicia la
                # ventana. Medir ese periodo es la única forma fiable de saber si el
                # plan es por minuto, por hora o por día: la cabecera Reset no siempre
                # lo dice, y adivinarlo mal deja la terminal sin datos toda la sesión.
                if prev is not None and int(remaining) > prev + 1:
                    if self._last_reset_at:
                        observed = now - self._last_reset_at
                        if 30.0 <= observed <= 172_800.0:
                            self.observed_window_s = observed
                            self.window_samples += 1
                    self._last_reset_at = now
                elif self._last_reset_at is None:
                    self._last_reset_at = now
                self.remaining = int(remaining)
            if limit is not None:
                self.limit = int(limit)
            if reset_seconds is not None:
                self.reset_seconds = float(reset_seconds)
            self.updated_at = now

    def note_rate_limited(self, retry_after_s: float | None = None) -> None:
        wait = 30.0 if retry_after_s is None else max(5.0, float(retry_after_s))
        with self._lock:
            self.rate_limited_until = time.time() + wait
            self.remaining = 0

    def spend(self, n: int = 1) -> None:
        with self._lock:
            if self.remaining is not None:
                self.remaining = max(0, self.remaining - int(n))

    def pages_paused_reason(self) -> Dict[str, Any] | None:
        """Por qué el carril de páginas no puede pedir nada. `None` = puede.

        Es la MISMA condición que aplica `budget_for_pages`, expuesta para que el
        Auditor no tenga que deducirla ni el operador adivinarla.
        """
        now = time.time()
        with self._lock:
            if now < self.rate_limited_until:
                return {"reason": "RATE_LIMITED",
                        "seconds": round(self.rate_limited_until - now, 1),
                        "remaining": self.remaining, "limit": self.limit}
            remaining = self.remaining
            limit = self.limit
            stale = (now - self.updated_at) > 120.0 if self.updated_at else True
        if remaining is None or stale:
            return None
        if int(remaining) - ENGINE_RESERVE <= 0:
            return {"reason": "PLAN_AGOTADO", "remaining": int(remaining),
                    "limit": None if limit is None else int(limit),
                    "engine_reserve": ENGINE_RESERVE}
        return None

    def burst_ceiling(self, requested: int) -> int:
        """Tope DURO de la ráfaga de arranque. No es el ritmo: es el límite.

        v1.57.2 · La ráfaga hacía `allowed = max(allowed, len(priority_due))` y se
        saltaba `budget_for_pages` ENTERA: con el plan en las últimas seguía
        pidiendo, y podía comerse la reserva del motor —que es la que sostiene la
        estructura— para dibujar tablas de presentación.

        Aquí la ráfaga conserva su motivo de ser (llenar la pantalla del activo
        nuevo sin esperar al ritmo de régimen) pero no puede cruzar la reserva ni
        pedir con el proveedor limitando.
        """
        now = time.time()
        with self._lock:
            if now < self.rate_limited_until:
                return 0
            remaining = self.remaining
            stale = (now - self.updated_at) > 120.0 if self.updated_at else True
        if remaining is None or stale:
            return max(0, int(requested))
        return max(0, min(int(requested), int(remaining) - ENGINE_RESERVE))

    def budget_for_pages(self, requested: int) -> int:
        """Cuántas peticiones puede hacer el carril de páginas en este ciclo.

        El carril del motor nunca pregunta: su hidratación es la que sostiene la
        estructura y no puede quedarse sin turno porque una tabla de presentación
        haya vaciado la cuota antes.
        """
        now = time.time()
        with self._lock:
            if now < self.rate_limited_until:
                return 0
            remaining = self.remaining
            stale = (now - self.updated_at) > 120.0 if self.updated_at else True
        if remaining is None or stale:
            # Sin telemetría fiable se avanza despacio en lugar de a ciegas.
            return max(1, min(int(requested), 4))
        usable = int(remaining) - ENGINE_RESERVE
        if usable <= 0:
            return 0
        return max(0, min(int(requested), usable))

    def recommended_interval(self, requests_per_cycle: int, *, share: float = 0.75,
                             floor_s: float = 15.0, ceil_s: float = 3600.0) -> float:
        """Segundos mínimos entre ciclos para que el plan aguante toda la sesión.

        Un plan de 240 peticiones no soporta 9 endpoints cada 15 s: son 2160 por
        hora y la cuota se agota en minutos, que es exactamente lo que dejaba al
        proveedor DEGRADED con las herramientas envejeciendo en pantalla.

        El ritmo sostenible se deriva de lo que el propio proveedor reporta
        (`remaining` y `reset_seconds`), no de una constante fija, así que funciona
        igual si la ventana del plan es por minuto, por hora o por día.
        """
        n = max(1, int(requests_per_cycle))
        with self._lock:
            remaining = self.remaining
            limit = self.limit
            reset = self.reset_seconds
            declared_n = self.declared_requests
            declared_w = self.declared_window_s
            observed_w = self.observed_window_s

        # 1 · Presupuesto declarado por el operador: la fuente más fiable, porque
        #     es el plan que realmente se contrató. Manda sola.
        if declared_n and declared_w:
            per_second = float(declared_n) / float(declared_w)
        # 2 · Ventana MEDIDA observando los reinicios reales del contador. Es una
        #     medición, no una suposición, así que también manda sola.
        elif limit and limit > 0 and observed_w:
            per_second = float(limit) / float(observed_w)
        else:
            # 3 · Sin plan declarado y sin ventana medida sólo quedan CONJETURAS.
            #
            #     v1.57.2 · Aquí estaba el agujero que vaciaba el plan. La cabecera
            #     Reset se tomaba como si describiera la VENTANA DEL PLAN, y muchas
            #     veces describe un CUBO DE RITMO: «60 s» no significa que el tope
            #     contratado se renueve cada minuto. Con `limit = 240` y
            #     `reset = 60` salían 4 req/s, el intervalo caía al suelo de 15 s y
            #     el carril del motor se comía las 240 peticiones en QUINCE MINUTOS.
            #     Lo que se ve después es la terminal entera muda con `remaining`
            #     de un solo dígito y treinta y cuatro herramientas «sin intentos».
            #
            #     Una ventana corta no es prueba del plan: es un limitador de
            #     ritmo. Por debajo de este umbral la conjetura se acota con la
            #     ventana DIARIA, que es la más restrictiva.
            conjeturas = []
            if remaining and reset and reset > 0:
                conjeturas.append(float(remaining) / float(reset))
                if float(reset) >= VENTANA_CREIBLE_S and limit and limit > 0:
                    # Reset largo: sí describe el plan. Se respeta tal cual.
                    per_second = conjeturas[0]
                    budget = max(per_second * max(0.05, min(1.0, share)), 1e-9)
                    return max(floor_s, min(ceil_s, n / budget))
            if limit and limit > 0:
                # Equivocarse por lento sólo cuesta frescura; equivocarse por
                # rápido agota el plan en minutos y deja la sesión entera sin datos.
                conjeturas.append(float(limit) / 86_400.0)
            if not conjeturas:
                return max(floor_s, 60.0)
            per_second = min(conjeturas)

        budget = max(per_second * max(0.05, min(1.0, share)), 1e-9)
        return max(floor_s, min(ceil_s, n / budget))

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        # Fuera de la sección crítica a propósito: recommended_interval() toma este
        # mismo lock y threading.Lock no es reentrante.
        interval = round(self.recommended_interval(ENGINE_FAST_REQUESTS), 1)
        # Igual que `interval`: fuera del lock, porque vuelve a tomarlo.
        pausa = self.pages_paused_reason()
        with self._lock:
            return {
                "remaining": self.remaining,
                "limit": self.limit,
                "reset_seconds": self.reset_seconds,
                "engine_reserve": ENGINE_RESERVE,
                "pages_paused": pausa,
                "rate_limited": now < self.rate_limited_until,
                "rate_limited_for_seconds": max(0.0, round(self.rate_limited_until - now, 1)),
                "telemetry_age_seconds": None if not self.updated_at else round(now - self.updated_at, 1),
                "engine_interval_seconds": interval,
                "window_seconds": self.declared_window_s or self.observed_window_s,
                "window_source": ("DECLARADA" if self.declared_window_s
                                  else "MEDIDA" if self.observed_window_s
                                  else "ASUMIDA_DIARIA"),
                "window_samples": self.window_samples,
                "declared_plan": (None if not self.declared_requests else
                                  {"requests": self.declared_requests, "window_seconds": self.declared_window_s}),
            }


# Endpoints del carril del motor escalonados por velocidad de cambio real.
# Gamma/Delta y el flujo se mueven tick a tick; vanna, charm, max pain e IV rank
# son estructurales y pedirlos al mismo ritmo sólo quemaba cuota.
ENGINE_FAST_JOBS = ("gamma", "delta", "net_drift", "net_flow")
ENGINE_SLOW_JOBS = ("vanna", "charm", "interval_gamma", "max_pain", "iv_rank")
ENGINE_FAST_REQUESTS = len(ENGINE_FAST_JOBS)
# Cada cuántos ciclos rápidos entra el bloque estructural.
ENGINE_SLOW_EVERY_N_CYCLES = 10

RAW_CACHE = RawCache()
QUOTA = QuotaGuard()


# Herramienta de página -> clave con la que el carril del motor publica su payload.
# Cada entrada aquí es una petición por ciclo que deja de hacerse.
ENGINE_SHARED_KEYS: Dict[str, str] = {
    "gex_by_strike": "gamma",
    "dex_by_strike": "delta",
    "vex_by_strike": "vanna",
    "chex_by_strike": "charm",
    "net_flow": "net_flow",
    "net_drift": "net_drift",
    "options_heat_map": "interval_gamma",
    # v1.43.0 · El mapa de GAMMA lo pide ya el carril del motor cada ciclo. El
    # carril de páginas lo ADOPTA en vez de repetir la petición, de modo que la
    # griega que TRACE muestra por defecto queda LIVE sin gastar cuota extra. Las
    # otras tres se piden aparte porque el motor no las necesita.
    "interval_map_gamma": "interval_gamma",
    "max_pain_over_time": "max_pain",
    "iv_rank": "iv_rank",
}
