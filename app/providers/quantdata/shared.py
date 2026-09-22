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
from collections import deque
from typing import Any, Dict, Tuple

from ...core.endpoint_runtime import EndpointRegistry
from ...core.request_governor import HEAVY, LIGHT, RequestGovernor


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

# ─────────────────────────────────────────────────────────────────────────────
# CONTRATO OFICIAL DE QUANT DATA
#
#   240 peticiones / 60 s   en VENTANA DESLIZANTE
#    20 peticiones /  1 s   de ráfaga
#   `X-RateLimit-Reset`     segundos hasta que el cubo se rellena
#
# Son DOS límites simultáneos y hay que respetar los dos: cumplir 240/60 no
# autoriza a lanzar 30 peticiones en el mismo segundo.
#
# v1.57.3 · Esto sustituye a la heurística `VENTANA_CREIBLE_S = 300`, que trataba
# un `Reset: 60` como una señal dudosa y acotaba el ritmo con una ventana DIARIA.
# Era falso y caro: con el contrato real, 240/60 s son 4 peticiones por segundo
# sostenidas, y el carril del motor —4 peticiones cada 15 s— consume el 6,7 % del
# presupuesto. Nunca estuvo quemando el plan.
#
# Y la consecuencia que sí importa: en una ventana DESLIZANTE, `remaining = 7` no
# es «plan agotado». Es una condición TRANSITORIA que se repone sola en menos de
# 60 s según van saliendo las peticiones viejas por el otro extremo.
SUSTAINED_LIMIT = 240
SUSTAINED_WINDOW_S = 60.0
BURST_LIMIT = 20
BURST_WINDOW_S = 1.0

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


def describir_pausa(pausa: Dict[str, Any] | None) -> str:
    """Una frase que dice qué freno del CONTRATO está mordiendo y cuánto dura.

    Los cuatro son transitorios y ninguno es un fallo del proveedor ni de
    autorización. Decirlo mal —«plan agotado»— mandaba a buscar el fallo donde no
    estaba y a cambiar credenciales que funcionaban.
    """
    if not pausa:
        return ""
    p = dict(pausa)
    segundos = p.get("seconds")
    cuando = f" · se repone en {float(segundos):.0f} s" if segundos is not None else ""
    motivo = str(p.get("reason") or "")
    if motivo == "RATE_LIMITED":
        return (f"el proveedor está limitando el ritmo (429){cuando}. "
                "Se respeta Retry-After; no es el endpoint ni la autorización")
    if motivo == "RAFAGA_20_POR_SEGUNDO":
        return (f"tope de RÁFAGA del contrato: {p.get('burst_limit')} peticiones por "
                f"{p.get('burst_window_seconds', 1)} s ya usadas{cuando}. Transitorio")
    if motivo == "VENTANA_DESLIZANTE":
        return (f"VENTANA DESLIZANTE llena: {p.get('sustained_used')} de "
                f"{p.get('sustained_limit')} en los últimos "
                f"{p.get('window_seconds')} s{cuando}. Transitorio: la ventana se "
                "repone conforme salen las peticiones viejas")
    if motivo == "RESERVA_DEL_MOTOR":
        return (f"por debajo de la reserva del motor ({p.get('engine_reserve')}): el "
                f"proveedor publica {p.get('remaining')} restantes{cuando}. La "
                "estructura tiene preferencia; es transitorio, no un plan agotado")
    return f"carril de páginas en pausa ({motivo}){cuando}"


class VentanaDeslizante:
    """Contador de una ventana deslizante: `limite` peticiones por `ventana` s.

    Guarda el instante de cada petición y va soltando por el extremo viejo. No es
    un cubo que se vacía de golpe cada minuto —eso sería una ventana FIJA y
    permitiría el doble de peticiones en el cambio de cubo—: aquí una petición
    hecha hace 59,5 s todavía cuenta, y deja de contar medio segundo después.
    """

    __slots__ = ("limite", "ventana", "_sellos")

    def __init__(self, limite: int, ventana: float) -> None:
        self.limite = int(limite)
        self.ventana = float(ventana)
        self._sellos: deque[float] = deque()

    def _podar(self, ahora: float) -> None:
        corte = ahora - self.ventana
        while self._sellos and self._sellos[0] <= corte:
            self._sellos.popleft()

    def usadas(self, ahora: float) -> int:
        self._podar(ahora)
        return len(self._sellos)

    def libres(self, ahora: float) -> int:
        return max(0, self.limite - self.usadas(ahora))

    def espera_s(self, ahora: float) -> float:
        """Segundos hasta que se libere UN hueco. 0 si ya lo hay."""
        if self.libres(ahora) > 0:
            return 0.0
        return max(0.0, (self._sellos[0] + self.ventana) - ahora)

    def anotar(self, ahora: float, n: int = 1) -> None:
        for _ in range(max(0, int(n))):
            self._sellos.append(ahora)

    def reiniciar(self) -> None:
        self._sellos.clear()


class QuotaGuard:
    """Presupuesto común, ceñido al CONTRATO PUBLICADO por Quant Data.

    v1.57.3 · Antes esto era una adivinanza sobre cuál sería la ventana del plan.
    Ya no hace falta adivinar nada: el contrato dice 240/60 s deslizante y 20/1 s
    de ráfaga, y `X-RateLimit-Reset` son los segundos hasta que el cubo se rellena.

    De modo que aquí hay DOS autoridades, y las dos mandan a la vez:

      * CONTADOR LOCAL — lo que ITM QUANT ya ha pedido, en las dos ventanas. Es
        el que impide pasarse ANTES de que el proveedor tenga que decirnos que
        nos hemos pasado. Un 429 evitado no cuesta nada; uno recibido cuesta el
        ciclo entero y arrastra a los dos carriles.
      * CABECERAS — `X-RateLimit-Limit`, `-Remaining`, `-Reset` y `Retry-After`.
        Son la verdad del servidor y se leen en cada respuesta, incluidas las de
        error. Si el proveedor cambia el plan, el límite se adopta solo.

    El mínimo de las dos es el presupuesto. Ninguna puede relajar a la otra.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.remaining: int | None = None
        self.limit: int | None = None
        self.reset_seconds: float | None = None
        self.updated_at: float = 0.0
        self.rate_limited_until: float = 0.0
        self.retry_after_s: float | None = None
        # Las dos ventanas del contrato. El límite sostenido se re-ajusta solo si
        # el proveedor publica otro en `X-RateLimit-Limit`.
        self._sostenida = VentanaDeslizante(SUSTAINED_LIMIT, SUSTAINED_WINDOW_S)
        self._rafaga = VentanaDeslizante(BURST_LIMIT, BURST_WINDOW_S)
        # Ventana del plan aprendida observando cuándo se repone el contador.
        # Con el contrato conocido es corroboración, no la base del ritmo.
        self.observed_window_s: float | None = None
        self.window_samples: int = 0
        self._last_reset_at: float | None = None
        # Presupuesto declarado por el operador. Cuando existe, manda sobre todo.
        self.declared_requests: float | None = _env_float("QUANTDATA_PLAN_REQUESTS", None)
        self.declared_window_s: float | None = _env_float("QUANTDATA_PLAN_WINDOW_SECONDS", None)

    # ------------------------------------------------------------ telemetría

    def note(self, *, remaining: int | None, limit: int | None,
             reset_seconds: float | None) -> None:
        """Adopta las cabeceras de una respuesta. Se llama SIEMPRE, también en error."""
        now = time.time()
        with self._lock:
            prev = self.remaining
            if remaining is not None:
                # Un salto hacia ARRIBA es el cubo reponiéndose. Medir ese periodo
                # corrobora la ventana declarada en el contrato.
                if prev is not None and int(remaining) > prev + 1:
                    if self._last_reset_at:
                        observed = now - self._last_reset_at
                        if 5.0 <= observed <= 172_800.0:
                            self.observed_window_s = observed
                            self.window_samples += 1
                    self._last_reset_at = now
                elif self._last_reset_at is None:
                    self._last_reset_at = now
                self.remaining = int(remaining)
                # El servidor dice que hay hueco: se levanta la retención.
                if int(remaining) > 0 and now >= self.rate_limited_until:
                    self.rate_limited_until = 0.0
            if limit is not None and int(limit) > 0:
                self.limit = int(limit)
                # El contrato puede cambiar sin avisarnos: se adopta el límite que
                # publica el servidor en vez de conservar la constante compilada.
                self._sostenida.limite = int(limit)
            if reset_seconds is not None:
                self.reset_seconds = float(reset_seconds)
            self.updated_at = now

    def note_rate_limited(self, retry_after_s: float | None = None) -> None:
        """429. `Retry-After` manda; si no viene, se usa `Reset`; si tampoco, 1 s.

        v1.57.3 · El suelo era de 30 s, heredado de cuando se creía que la ventana
        podía ser diaria. Con una ventana deslizante de 60 s, esperar 30 s por un
        429 tira media ventana a la basura: el hueco se abre según van saliendo
        las peticiones viejas, no de golpe.
        """
        with self._lock:
            reset = self.reset_seconds
        if retry_after_s is not None:
            wait = max(0.0, float(retry_after_s))
        elif reset is not None and reset > 0:
            wait = float(reset)
        else:
            wait = BURST_WINDOW_S
        wait = max(BURST_WINDOW_S, min(wait, SUSTAINED_WINDOW_S))
        with self._lock:
            self.retry_after_s = wait
            self.rate_limited_until = time.time() + wait
            self.remaining = 0

    def spend(self, n: int = 1) -> None:
        """Anota lo pedido en las DOS ventanas. Se llama antes de la petición."""
        now = time.time()
        with self._lock:
            self._sostenida.anotar(now, n)
            self._rafaga.anotar(now, n)
            if self.remaining is not None:
                self.remaining = max(0, self.remaining - int(n))

    # -------------------------------------------------------- presupuestos

    def _margen(self, now: float, reserva: int) -> int:
        """Peticiones que caben AHORA, por el más restrictivo de los tres frenos.

        Sin lock: lo toma quien llama.
        """
        libre = min(self._sostenida.libres(now), self._rafaga.libres(now))
        stale = (now - self.updated_at) > 120.0 if self.updated_at else True
        if self.remaining is not None and not stale:
            libre = min(libre, int(self.remaining))
        return max(0, libre - int(reserva))

    def headroom(self, *, reserve: int = 0) -> int:
        """Cuántas peticiones caben ahora mismo, dejando `reserve` sin tocar."""
        now = time.time()
        with self._lock:
            if now < self.rate_limited_until:
                return 0
            return self._margen(now, reserve)

    def pages_paused_reason(self) -> Dict[str, Any] | None:
        """Por qué el carril de páginas no puede pedir nada. `None` = puede.

        La misma condición que aplica `budget_for_pages`, expuesta para que el
        Auditor no tenga que deducirla ni el operador adivinarla.

        v1.57.3 · Ya no existe el veredicto «PLAN_AGOTADO». En una ventana
        DESLIZANTE de 60 s no hay plan que agotar: `remaining = 7` significa que
        las 233 peticiones anteriores siguen dentro de la ventana, y se reponen
        solas conforme van saliendo. Es una espera de SEGUNDOS, y decirlo como un
        agotamiento hacía buscar el fallo donde no estaba.
        """
        now = time.time()
        with self._lock:
            if now < self.rate_limited_until:
                return {"reason": "RATE_LIMITED",
                        "seconds": round(self.rate_limited_until - now, 1),
                        "retry_after_s": self.retry_after_s,
                        "remaining": self.remaining, "limit": self.limit}
            if self._margen(now, ENGINE_RESERVE) > 0:
                return None
            # Qué freno concreto es el que muerde, y cuántos segundos dura.
            espera_r = self._rafaga.espera_s(now)
            espera_s = self._sostenida.espera_s(now)
            stale = (now - self.updated_at) > 120.0 if self.updated_at else True
            servidor = None if (self.remaining is None or stale) else int(self.remaining)
            if self._rafaga.libres(now) <= 0:
                freno, espera = "RAFAGA_20_POR_SEGUNDO", espera_r
            elif self._sostenida.libres(now) <= 0:
                freno, espera = "VENTANA_DESLIZANTE", espera_s
            else:
                freno = "RESERVA_DEL_MOTOR"
                espera = max(espera_s, self.reset_seconds or 0.0)
            return {
                "reason": freno,
                "transient": True,
                "seconds": round(float(espera), 1),
                "remaining": servidor,
                "limit": self.limit,
                "engine_reserve": ENGINE_RESERVE,
                "window_seconds": self._sostenida.ventana,
                "sustained_used": self._sostenida.usadas(now),
                "sustained_limit": self._sostenida.limite,
                "burst_used": self._rafaga.usadas(now),
                "burst_limit": self._rafaga.limite,
            }

    def burst_ceiling(self, requested: int) -> int:
        """Tope DURO de la ráfaga de arranque. No es el ritmo: es el límite.

        La ráfaga existe para llenar la pantalla del activo nuevo sin esperar al
        ritmo de régimen, y por eso puede saltarse el paso corto de
        `budget_for_pages`. Lo que NO puede saltarse es el contrato: ni las 20
        por segundo, ni las 240 de la ventana, ni la reserva del motor.
        """
        now = time.time()
        with self._lock:
            if now < self.rate_limited_until:
                return 0
            return min(max(0, int(requested)), self._margen(now, ENGINE_RESERVE))

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
            margen = self._margen(now, ENGINE_RESERVE)
            stale = (now - self.updated_at) > 120.0 if self.updated_at else True
            sin_telemetria = self.remaining is None or stale
            nunca_vista = not self.updated_at
        if sin_telemetria:
            # v1.57.4 · NUNCA VISTA ≠ ENVEJECIDA. Son dos situaciones distintas y
            # tratarlas igual estrangulaba el arranque.
            #
            #   · Nunca vista (`updated_at == 0`): es el primer ciclo del proceso.
            #     No hay nada que hayan podido gastar «mientras no mirábamos»
            #     porque no ha habido un mientras. El contador local ya aplica el
            #     contrato entero, y la primera respuesta trae las cabeceras.
            #   · Envejecida: llevamos dos minutos sin saber nada del servidor. Ahí
            #     sí se avanza despacio, porque otra instancia de la misma cuenta
            #     pudo gastar sin que nos enteráramos.
            if nunca_vista:
                return max(0, min(int(requested), margen))
            return max(0, min(int(requested), margen, 4))
        return max(0, min(int(requested), margen))

    def recommended_interval(self, requests_per_cycle: int, *, share: float = 0.75,
                             floor_s: float = 15.0, ceil_s: float = 3600.0) -> float:
        """Segundos mínimos entre ciclos para vivir dentro del contrato.

        v1.57.3 · La base ya no es una conjetura sobre la ventana: es el contrato
        publicado, 240/60 s, y la cabecera `Limit`/`Reset` cuando el proveedor las
        manda. Con 4 peticiones por ciclo salen 15 s —el suelo—, que son 16
        peticiones por minuto: el 6,7 % del presupuesto sostenido.
        """
        n = max(1, int(requests_per_cycle))
        with self._lock:
            limit = self.limit
            reset = self.reset_seconds
            declared_n = self.declared_requests
            declared_w = self.declared_window_s
            observed_w = self.observed_window_s

        # 1 · Presupuesto declarado por el operador: el plan que se contrató.
        if declared_n and declared_w:
            per_second = float(declared_n) / float(declared_w)
        # 2 · Cabeceras del proveedor: tope y ventana, tal cual vienen.
        elif limit and limit > 0 and reset and reset > 0:
            per_second = float(limit) / float(reset)
        # 3 · Ventana medida observando cómo se repone el contador.
        elif limit and limit > 0 and observed_w:
            per_second = float(limit) / float(observed_w)
        # 4 · El CONTRATO. Ya no se cae a una ventana diaria inventada: el
        #     proveedor publica 240/60 s y eso es lo que se aplica.
        else:
            per_second = float(limit or SUSTAINED_LIMIT) / SUSTAINED_WINDOW_S

        budget = max(per_second * max(0.05, min(1.0, share)), 1e-9)
        interval = max(floor_s, min(ceil_s, n / budget))

        # Y un freno que no depende del ritmo sino del saldo: con menos de la
        # reserva en la mano no se puede ciclar más rápido de lo que tarda el cubo
        # en reponerse, por mucho que el ritmo sostenido lo permitiera. En la
        # ventana deslizante del contrato eso son segundos; con una ventana larga
        # son los que el proveedor diga en `Reset`.
        with self._lock:
            restantes = self.remaining
            reset_s = self.reset_seconds
            fresca = bool(self.updated_at) and (time.time() - self.updated_at) <= 120.0
        if fresca and restantes is not None and int(restantes) <= ENGINE_RESERVE:
            interval = max(interval, float(reset_s or SUSTAINED_WINDOW_S))
        return min(ceil_s, interval)

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        # Fuera de la sección crítica a propósito: vuelven a tomar el lock.
        interval = round(self.recommended_interval(ENGINE_FAST_REQUESTS), 1)
        pausa = self.pages_paused_reason()
        with self._lock:
            return {
                "remaining": self.remaining,
                "limit": self.limit,
                "reset_seconds": self.reset_seconds,
                "retry_after_seconds": self.retry_after_s,
                "engine_reserve": ENGINE_RESERVE,
                "pages_paused": pausa,
                "rate_limited": now < self.rate_limited_until,
                "rate_limited_for_seconds": max(0.0, round(self.rate_limited_until - now, 1)),
                "telemetry_age_seconds": None if not self.updated_at else round(now - self.updated_at, 1),
                "engine_interval_seconds": interval,
                # El contrato, publicado tal cual, y lo que llevamos gastado de él.
                "contract": {
                    "sustained_limit": self._sostenida.limite,
                    "sustained_window_seconds": self._sostenida.ventana,
                    "sustained_used": self._sostenida.usadas(now),
                    "burst_limit": self._rafaga.limite,
                    "burst_window_seconds": self._rafaga.ventana,
                    "burst_used": self._rafaga.usadas(now),
                },
                "window_seconds": (self.declared_window_s or self.reset_seconds
                                   or self.observed_window_s or SUSTAINED_WINDOW_S),
                "window_source": ("DECLARADA" if self.declared_window_s
                                  else "CABECERA" if self.reset_seconds
                                  else "MEDIDA" if self.observed_window_s
                                  else "CONTRATO"),
                "window_samples": self.window_samples,
                "declared_plan": (None if not self.declared_requests else
                                  f"{int(self.declared_requests)}/{int(self.declared_window_s or 0)}s"),
            }


# Endpoints del carril del motor escalonados por velocidad de cambio real.
# Gamma/Delta y el flujo se mueven tick a tick; vanna, charm, max pain e IV rank
# son estructurales y pedirlos al mismo ritmo sólo quemaba cuota.
ENGINE_FAST_JOBS = ("gamma", "delta", "net_drift", "net_flow")
ENGINE_SLOW_JOBS = ("vanna", "charm", "interval_gamma", "max_pain", "iv_rank")
ENGINE_FAST_REQUESTS = len(ENGINE_FAST_JOBS)
# Cada cuántos ciclos rápidos entra el bloque estructural.
ENGINE_SLOW_EVERY_N_CYCLES = 10

class VencimientoSeleccionado:
    """Los vencimientos que la terminal tiene REALMENTE en pantalla, por activo.

    v1.57.7 · `/v1/options/tool/max-pain` exige `filter.expirationDate`. No hay
    forma de construir un cuerpo válido sin un vencimiento, y no vale inventarlo:
    un max pain del vencimiento equivocado es un número creíble y falso, que es
    peor que no tener número.

    De modo que el vencimiento sale de donde ya estaba: la ventana de vencimiento
    que la terminal aplica a la cadena. Esto es sólo el sitio donde se publica
    para que el carril de páginas pueda leerlo sin importar `service`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._por_simbolo: Dict[str, Tuple[float, list]] = {}

    def publicar(self, symbol: str, fechas) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        limpias = []
        for f in (fechas or []):
            texto = str(f or "").strip()[:10]
            # ISO o nada. Un formato distinto es un error de quien publica, no
            # algo que este registro deba adivinar.
            if len(texto) == 10 and texto[4] == "-" and texto[7] == "-":
                limpias.append(texto)
        with self._lock:
            if limpias:
                self._por_simbolo[sym] = (time.time(), sorted(set(limpias)))
            else:
                self._por_simbolo.pop(sym, None)

    def principal(self, symbol: str, *, max_age_s: float = 900.0) -> str | None:
        """El vencimiento más cercano de los que están en pantalla. `None` = no hay.

        Caduca: un vencimiento publicado hace un cuarto de hora puede ser de otra
        sesión o de otro activo ya cerrado. Sin dato fresco se devuelve `None` y
        quien pregunte tendrá que decir que le falta el requisito.
        """
        sym = str(symbol or "").strip().upper()
        with self._lock:
            entrada = self._por_simbolo.get(sym)
        if not entrada:
            return None
        cuando, fechas = entrada
        if (time.time() - cuando) > float(max_age_s) or not fechas:
            return None
        return fechas[0]

    def todos(self, symbol: str) -> list:
        sym = str(symbol or "").strip().upper()
        with self._lock:
            entrada = self._por_simbolo.get(sym)
        return list(entrada[1]) if entrada else []

    def clear_symbol(self, symbol: str) -> None:
        with self._lock:
            self._por_simbolo.pop(str(symbol or "").strip().upper(), None)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {k: {"expirations": v[1], "age_seconds": round(time.time() - v[0], 1)}
                    for k, v in self._por_simbolo.items()}


EXPIRY_SELECTION = VencimientoSeleccionado()

RAW_CACHE = RawCache()
QUOTA = QuotaGuard()

#: Runtime POR ENDPOINT: plazo calibrado con latencia medida, reintento con
#: jitter y cortacircuitos. Nada se comparte entre herramientas, así que
#: `dark_flow` con el circuito abierto no cambia un byte de `gex_by_strike`.
#:
#: v1.58.1 · VIVE AQUÍ, con la caché y la cuota, porque los DOS carriles hablan
#: con los mismos endpoints. Estaba en `intelligence`, el carril de páginas, así
#: que el carril del motor —el que hidrata la estructura, y el que más timeouts
#: acumulaba en el registro— no tenía plazo medido ni podía aprender de ellos.
#: El plazo configurado se inyecta con `set_default_timeout` al arrancar: el
#: registro se crea al importar, cuando todavía no hay settings.
ENDPOINT_RUNTIME = EndpointRegistry()

# ── clases de coste · v1.58.0 ─────────────────────────────────────────────
#
# Respetar 240/60 s y 20/1 s no basta: veinte peticiones en un segundo cumplen
# el contrato y, si las veinte son mapas por intervalo, están las veinte VIVAS a
# la vez compitiendo entre ellas. La congestión la provocamos nosotros y se lee
# como lentitud del proveedor.
#
# PESADAS son las que recorren la cadena entera, la cinta del día o una serie
# temporal completa. El gobernador limita cuántas de éstas pueden estar en vuelo
# a la vez y las escalona.
HEAVY_TOOLS = frozenset({
    # exposición por strike y por vencimiento: la cadena entera, greek a greek
    "gex_by_strike", "dex_by_strike", "vex_by_strike", "chex_by_strike",
    "gex_by_expiration", "dex_by_expiration", "vex_by_expiration",
    "chex_by_expiration", "oi_by_strike", "oi_by_expiration",
    # mapas por intervalo: serie temporal × strike
    "interval_map_gamma", "interval_map_delta", "interval_map_vanna",
    "interval_map_charm",
    # la cinta completa del día
    "options_order_flow", "options_order_flow_raw",
    # estadísticas y series largas
    "market_share", "contract_statistics", "contract_trade_side_statistics",
    "max_pain", "max_pain_over_time", "oi_over_time", "oi_change",
    "stock_price_over_time", "equity_prints",
    # dark pool: ventana completa de intervalos
    "dark_flow", "dark_pool_levels",
    # volatilidad con histórico
    "term_structure", "volatility_skew", "iv_rank", "volatility_drift",
})

#: Los nombres del carril del MOTOR, que no coinciden con las claves de página.
HEAVY_ENGINE_JOBS = frozenset({
    "gamma", "delta", "vanna", "charm", "interval_gamma", "max_pain", "iv_rank",
})


def weight_of(key: str) -> str:
    """LIGHT o HEAVY. Una sola tabla, para los dos carriles."""
    k = str(key or "")
    if k.startswith("engine:"):
        return HEAVY if k.split(":", 1)[1] in HEAVY_ENGINE_JOBS else LIGHT
    if k in HEAVY_TOOLS:
        return HEAVY
    # El nombre DESNUDO del carril del motor también cuenta: `gamma`, `delta` y
    # `max_pain` son los nombres con los que el Data Hub los publica y con los
    # que aparecieron en la consola. Clasificarlos ligeros por no llevar prefijo
    # dejaría fuera del techo de pesadas justo a los que se ahogaban entre ellos.
    return HEAVY if k in HEAVY_ENGINE_JOBS else LIGHT


#: Gobernador de peticiones en vuelo, COMPARTIDO por los dos carriles: el techo
#: de concurrencia no serviría de nada si cada carril tuviera el suyo.
GOVERNOR = RequestGovernor()


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
