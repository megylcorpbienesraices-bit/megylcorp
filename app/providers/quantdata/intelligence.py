"""Lane de páginas integradas de Quant Data · ITM QUANT v1.41.0

Carril independiente del que alimenta el motor. Recorre el catálogo completo de
herramientas (`tools.py`) con cadencias distintas por familia y publica el
resultado normalizado para la terminal.

Está deliberadamente separado de `QuantDataRuntime`:
  * el carril del motor no puede quedarse esperando por una herramienta de
    presentación, ni gastar su cuota en ella;
  * si una herramienta del catálogo falla, la estructura que consume el motor
    sigue intacta.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from .client import QuantDataClient, QuantDataError, QuantDataTimeout
from ...core.endpoint_runtime import retry_plan
from .settings import load_settings, QuantDataSettings
from ...core import session_resolver
from .tools import (FaltaRequisito, build_catalog, is_missing_tool_error, is_validation_error,
                    _validation_detail, repair_body, classify_provider_failure,
                    STATUS_REQUEST_INVALID, STATUS_NO_DATA, STATUS_MISSING_TOOL,
                    TOOL_FORBIDDEN_FIELDS,
                    STATUS_TRANSIENT, CADENCE, PAGES, QuantDataTool,
                    ROUTE_OK, ROUTE_INVALID, route_diagnostic)
from .shared import (RAW_CACHE, QUOTA, ENGINE_SHARED_KEYS, describir_pausa,
                     BURST_LIMIT, BURST_WINDOW_S, ENGINE_RESERVE,
                     ENDPOINT_RUNTIME, GOVERNOR, weight_of, EXPIRY_SELECTION)
from ...core.obs import note as _obs_note, expected as _obs_expected
from ...core.obs import get_logger as _get_logger
from ...core.data_hub_runtime import HUB_RUNTIME, CHANNEL_SLACK_S
from ...core import consumer_contracts as CC
from ...core import scheduler_states as SS
from ...core.wall_snapshot import SNAPSHOTS as WALL_SNAPSHOTS, LKG_FRESCO_S as _WALL_FRESCO_S

_LOG = _get_logger("quantdata.intelligence")

#: Conservado para compatibilidad de lectura: la concurrencia real la gobierna
#: `shared.GOVERNOR`, que cuenta peticiones VIVAS y no peticiones hechas.
PAGE_MAX_CONCURRENCY = 2

# ── Arranque tras un cambio de activo ────────────────────────────────────────
#
# Al cambiar de símbolo se vacía todo, así que las ~30 herramientas quedan
# vencidas a la vez. En régimen permanente se sirven con presupuesto corto
# —4 por ciclo cuando no hay telemetría de cuota fiable—, concurrencia 2 y
# 15 s entre ciclos: unos DOS MINUTOS hasta tener la pantalla entera.
#
# Ese ritmo es el correcto en régimen permanente, donde la pantalla ya está
# dibujada y machacar la API no aporta nada. No lo es justo después de un
# cambio: ahí NO HAY NADA en pantalla, y el coste de una ráfaga está
# justificado porque es la diferencia entre ver el activo nuevo al momento o
# mirar una pantalla vacía dos minutos.
#
# La ráfaga es acotada por los tres lados: sólo cubre lo que DIBUJA la
# pantalla (prioridad 0 y 1), dura un número de segundos fijo, y se apaga sola
# en cuanto esa prioridad está servida. Nunca toca la reserva del motor.
BURST_SECONDS = 25.0
BURST_MAX_PRIORITY = 1
# v1.57.4 · Ceñidos al contrato, no a una corazonada. El tope de ráfaga son 20
# peticiones por segundo y la reserva del motor son 12: al carril de páginas le
# quedan OCHO por segundo. Con ciclos de un segundo —la ventana de ráfaga— eso
# es exactamente el máximo que el contrato permite sin rozarlo.
BURST_CONCURRENCY = BURST_LIMIT - ENGINE_RESERVE
BURST_CYCLE_SECONDS = BURST_WINDOW_S

# Orden de carga tras un cambio de activo. Número más bajo = se pide antes.
#
#   0 · lo que dibuja el gráfico principal y sus niveles
#   1 · flujo de la sesión
#   2 · estructura de vencimientos y volatilidad
#   3 · contexto secundario, que puede esperar al ciclo siguiente sin que se note
_PRIORITY: dict[str, int] = {
    "gex_by_strike": 0, "dex_by_strike": 0, "oi_by_strike": 0,
    "interval_map_gamma": 0,
    "net_flow": 1, "net_drift": 1, "options_order_flow_raw": 1,
    "options_order_flow": 1, "dark_flow": 1, "dark_pool_levels": 1,
    "vex_by_strike": 2, "chex_by_strike": 2, "oi_change": 2,
    "interval_map_delta": 2, "interval_map_vanna": 2, "interval_map_charm": 2,
    "iv_rank": 2, "volatility_skew": 2, "term_structure": 2, "volatility_drift": 2,
    "max_pain": 2, "max_pain_over_time": 2,
    "gex_by_expiration": 3, "dex_by_expiration": 3, "vex_by_expiration": 3,
    "chex_by_expiration": 3, "oi_by_expiration": 3, "oi_over_time": 3,
    "equity_prints": 3, "stock_price_over_time": 3,
    "contract_statistics": 3, "contract_trade_side_statistics": 3, "market_share": 3,
    "gainers_losers": 4, "news": 4,
}
_PRIORITY_DEFAULT = 3

#: Cada cuántos segundos de espera una herramienta gana un puesto de prioridad.
#:
#: v1.57.0 · LA COLA SE MORÍA DE HAMBRE.
#:
#: El lote se elegía así:
#:
#:     batch = sorted(due, key=lambda t: (_PRIORITY[t.key], ultimo_fetch))[:presupuesto]
#:
#: Con cuota corta, el presupuesto no da para todas. Y como el orden es estricto
#: por prioridad, una herramienta de la cola NO entra mientras quede una de
#: cabeza pendiente. Con refresco rápido, las de cabeza vencen en cada ciclo, así
#: que las de cola no entran NUNCA.
#:
#: Medido sobre el catálogo real con presupuesto de 6 páginas por ciclo:
#: 26 de 36 herramientas sin un solo intento en 200 ciclos. Eso es lo que el
#: Auditor enseñaba como «sin intentos · 1 rutas candidatas» y como 15/34
#: herramientas vivas: no era un fallo del proveedor ni de autorización, era el
#: programador que jamás las llamaba.
#:
#: El envejecimiento lo arregla sin tocar el orden cuando importa: una
#: herramienta recién servida conserva su prioridad —así que tras un cambio de
#: activo la exposición sigue yendo primero—, y una que lleva esperando sube de
#: puesto hasta que le toca. Nadie se queda fuera para siempre.
AGING_SECONDS = 45.0

#: El runtime POR ENDPOINT se importa de `shared`, con la caché y la cuota: los
#: dos carriles hablan con los mismos endpoints, así que el plazo medido de cada
#: uno tiene que ser el mismo para los dos. Ver `shared.ENDPOINT_RUNTIME`.

#: Nada sube por encima de la clase crítica por envejecer: la exposición que
#: dibuja el gráfico principal no puede perder su turno frente a las noticias.
AGING_FLOOR = 0


def effective_priority(key: str, waited_seconds: float) -> int:
    """Prioridad con la espera descontada. Más bajo = antes.

    Pura y sin estado a propósito: es la regla que decide quién entra en el lote
    y tiene que poder probarse sola.
    """
    base = _PRIORITY.get(key, _PRIORITY_DEFAULT)
    if base <= AGING_FLOOR:
        return base
    espera = max(0.0, float(waited_seconds or 0.0))
    ascensos = int(espera // AGING_SECONDS)
    return max(AGING_FLOOR, base - ascensos)


def in_burst_class(key: str, waited_seconds: float) -> bool:
    """¿Entra esta herramienta en la ráfaga de arranque?

    Una sola regla, compartida con el orden del lote. Cuando la ráfaga filtraba
    por la prioridad BASE y el lote ordenaba por la EFECTIVA, el envejecimiento
    quedaba a medias: una herramienta ya ascendida a la clase que dibuja la
    pantalla seguía sin entrar en la ventana de arranque.
    """
    return effective_priority(key, waited_seconds) <= BURST_MAX_PRIORITY


class QuantDataIntelligence:
    """Recolector de páginas integradas del proveedor."""

    def __init__(self) -> None:
        self.settings: QuantDataSettings = load_settings()
        self.catalog: Dict[str, QuantDataTool] = build_catalog()
        #: Las cuatro cifras de la última petición de cada herramienta, para el
        #: Auditor: cola propia, red del proveedor, total y presupuesto.
        self._timings: Dict[str, Dict[str, Any]] = {}
        #: Cuándo debe acabar este ciclo. Lo necesita el presupuesto de
        #: reintento: un reintento que no cabe en el ciclo no es un reintento,
        #: es una petición que compite con el ciclo siguiente.
        self._cycle_ends_at: float = 0.0
        #: Clase del último fallo por herramienta. Un timeout y un 400 no se
        #: reintentan igual, y `provider_status` no distingue el timeout del
        #: resto de lo transitorio.
        self._fail_class: Dict[str, str] = {}
        #: Última decisión de reintento por herramienta, con su motivo, para que
        #: el Auditor pueda enseñar por qué NO se reintentó.
        self._retry_notes: Dict[str, Dict[str, Any]] = {}
        self.client: QuantDataClient | None = None
        self._symbol = "DIA"
        self._task: asyncio.Task | None = None
        self._stop = False
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._fetched_at: Dict[str, float] = {}
        #: Claves con petición VIVA ahora mismo. No es telemetría: es el estado
        #: `RUNNING` del programador, que antes no existía.
        self._en_vuelo: set[str] = set()
        self._running = False
        self._cycle = 0
        self._burst_until = 0.0
        # ¿Es la primera ráfaga del proceso? En frío no hay nada en pantalla y la
        # ráfaga cubre el catálogo entero; en un cambio de activo, sólo lo que
        # dibuja. Ver `refresh_due`.
        self._arranque_en_frio = False
        # v1.57.1 · Instante desde el que una herramienta NUNCA SERVIDA lleva
        # esperando. Sin esta referencia, `now - self._fetched_at.get(key, 0.0)`
        # medía la espera desde 1970: toda herramienta sin servir envejecía de
        # golpe hasta el suelo y la clase de prioridad dejaba de existir.
        self._eligible_since = time.time()
        # Época del símbolo: avanza en cada cambio de activo y ata cada respuesta
        # al ticker que la pidió.
        self._epoch = 0

    # ------------------------------------------------------------- ciclo

    @property
    def configured(self) -> bool:
        return self.settings.configured

    async def start(self, symbol: str) -> None:
        self.settings = load_settings()
        # v1.58.0 · El WARM START entra con su suelo de política: un `.env` con
        # un plazo más corto ya no puede rebajarlo, que es lo que hacía que todo
        # endpoint sin historia muriera a los cinco segundos.
        ENDPOINT_RUNTIME.set_warm_start(self.settings.read_warm_start_seconds)
        GOVERNOR.max_inflight = max(1, int(self.settings.max_inflight))
        GOVERNOR.max_heavy_inflight = max(
            1, min(int(self.settings.max_heavy_inflight), GOVERNOR.max_inflight))
        if self.settings.legacy_timeout_ignored:
            # Se dice UNA vez, al arrancar, en vez de dejar que el operador
            # descubra por qué su `.env` no manda.
            _obs_expected("quantdata.timeout.legacy_ignored")
            _LOG.info(
                "QUANTDATA_TIMEOUT_SECONDS=%s ignorado: por debajo del warm start "
                "de politica (%.0f s). El plazo lo fija el p95 medido por "
                "endpoint; no hace falta editar el .env.",
                self.settings.legacy_timeout_value,
                self.settings.read_warm_start_seconds)
        self._symbol = str(symbol or "DIA").upper().strip()
        if not self.settings.configured:
            return
        if self.client is None:
            self.client = QuantDataClient(self.settings)
            await self.client.start()
        self._stop = False
        # v1.57.4 · LA RÁFAGA TAMBIÉN AL ARRANCAR.
        #
        # Estaba armada sólo en `select_asset`, así que al abrir el programa no
        # había ninguna: el carril de páginas salía con presupuesto de régimen
        # —4 por ciclo— y ciclos de 15 s. Treinta y seis herramientas a ese ritmo
        # son NUEVE CICLOS: más de dos minutos de pantalla a medias en el peor
        # momento posible, que es el único en el que el operador está mirando.
        #
        # Al abrir no hay nada en pantalla: es exactamente el caso para el que se
        # escribió la ráfaga. El contrato la paga de sobra (240/60 s).
        if self._task is None or self._task.done():
            self._burst_until = time.monotonic() + BURST_SECONDS
            self._arranque_en_frio = True
            self._task = asyncio.create_task(self._loop(), name="itmq-quantdata-builtin-pages")
        self._wake.set()

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                _obs_expected("quantdata.intelligence.stop.cancelled")
            except Exception as exc:
                _obs_note("quantdata_intelligence:stop", exc)
            self._task = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        self._running = False

    async def select_asset(self, symbol: str) -> None:
        sym = str(symbol or "DIA").upper().strip()
        if sym == self._symbol:
            return
        previous = self._symbol
        # v1.44.0 · El cambio de símbolo es TRANSACCIONAL.
        #
        # La época avanza ANTES de tocar nada: cualquier respuesta que siga en
        # vuelo del símbolo anterior llegará con una época caducada y se
        # descartará en `_fetch` en vez de escribirse bajo el ticker nuevo. Sin
        # esto, una petición lenta de DIA podía aterrizar en la pantalla de AAPL
        # con los números de DIA y nada lo habría señalado.
        self._epoch += 1
        self._symbol = sym
        self._data.clear()
        self._fetched_at.clear()
        # Nadie ha esperado nada todavía para el activo nuevo: la espera se
        # cuenta desde AQUÍ, no desde el arranque del proceso.
        self._eligible_since = time.time()
        # Se limpia la caché de los DOS símbolos: la del anterior porque ya no
        # describe nada que vaya a mostrarse, y la del nuevo porque pudo quedar
        # sembrada por una consulta previa y ser más vieja que este cambio.
        RAW_CACHE.clear_symbol(previous)
        RAW_CACHE.clear_symbol(sym)
        # Ráfaga de cambio de activo: lo que dibuja la pantalla, ya. A diferencia
        # del arranque en frío, aquí la pantalla ya tiene forma.
        self._burst_until = time.monotonic() + BURST_SECONDS
        self._arranque_en_frio = False
        if self.settings.configured:
            self._wake.set()

    def _bursting(self) -> bool:
        """¿Estamos en la ventana de arranque tras un cambio de activo?"""
        return time.monotonic() < getattr(self, "_burst_until", 0.0)

    async def _loop(self) -> None:
        self._running = True
        while not self._stop:
            started = time.monotonic()
            try:
                await self.refresh_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _obs_note("quantdata_intelligence:refresh", exc, severity="DEGRADED")
            # Durante la ráfaga el ciclo se acorta: con 15 s entre rondas y 4
            # herramientas por ronda, ver el activo nuevo entero costaba minutos.
            if self._bursting():
                delay = BURST_CYCLE_SECONDS
            else:
                delay = max(5.0, CADENCE["FAST"] - (time.monotonic() - started))
            try:
                self._wake.clear()
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                _obs_expected("quantdata.intelligence.clock_timeout")

    # ---------------------------------------------------------- descarga

    def _waited(self, key: str, now: float) -> float:
        """Segundos que esta herramienta lleva sin servirse, para ESTE activo.

        Una herramienta nunca servida no ha esperado desde el epoch: ha esperado
        desde que pasó a ser exigible —arranque o cambio de activo—. Medirlo de
        otro modo hace que el envejecimiento la hunda al suelo de prioridad en el
        primer ciclo y anula el orden de carga.
        """
        base = self._fetched_at.get(key)
        if base is None:
            base = getattr(self, "_eligible_since", now)
        return max(0.0, float(now) - float(base))

    def _due(self, tool: QuantDataTool, now: float) -> bool:
        if not tool.available(now):
            return False
        last = self._fetched_at.get(tool.key)
        if last is None:
            return True
        return (now - last) >= CADENCE.get(tool.cadence, 60.0)

    async def refresh_due(self) -> Dict[str, Any]:
        if not self.settings.configured:
            return {"ready": False, "reason": "NOT_CONFIGURED"}
        async with self._lock:
            if self.client is None:
                self.client = QuantDataClient(self.settings)
                await self.client.start()
            now = time.time()
            # Nuevo ciclo: cada endpoint recupera su reintento y se fija cuándo
            # termina el turno, que es lo que acota el presupuesto.
            ENDPOINT_RUNTIME.start_cycle()
            self._cycle_ends_at = time.monotonic() + (
                BURST_CYCLE_SECONDS if self._bursting() else CADENCE["FAST"])
            due = [t for t in self.catalog.values() if self._due(t, now)]
            if not due:
                return {"ready": True, "skipped": True}

            # 1 · Lo que el carril del motor ya pidió se reutiliza tal cual. Son
            #     nueve endpoints por ciclo que dejan de gastar cuota y que quedan
            #     LIVE al instante con el mismo dato que consume la estructura.
            reused = 0
            remaining_due = []
            for tool in due:
                shared_key = ENGINE_SHARED_KEYS.get(tool.key)
                payload = RAW_CACHE.get(shared_key, self._symbol) if shared_key else None
                if payload is None:
                    remaining_due.append(tool)
                    continue
                self._adopt(tool, payload, source="ENGINE_LANE_SHARED", epoch=self._epoch)
                reused += 1

            # 2 · El resto se pide dentro del presupuesto que deja el motor.
            allowed = QUOTA.budget_for_pages(len(remaining_due))
            burst = self._bursting()
            if burst:
                # Sólo lo que dibuja la pantalla entra en la ráfaga. Las noticias
                # y los gainers/losers esperan al ciclo normal: no hay ninguna
                # prisa en ellos y gastarían el turno de la exposición.
                # v1.57.1 · La MISMA regla que ordena el lote. Filtrar aquí por la
                # prioridad BASE mientras el lote ordena por la EFECTIVA dejaba el
                # envejecimiento a medias: una herramienta que ya había ascendido a
                # la clase que dibuja la pantalla seguía excluida de la ráfaga.
                # En un cambio de activo la pantalla ya tiene forma y sólo urge lo
                # que dibuja. En el ARRANQUE no hay nada, así que no hay a quién
                # ceder el turno: entran todas, ordenadas por prioridad, y el
                # contrato pone el techo.
                priority_due = (list(remaining_due) if self._arranque_en_frio else
                                [t for t in remaining_due
                                 if in_burst_class(t.key, self._waited(t.key, now))])
                if priority_due:
                    remaining_due = priority_due
                    # v1.57.2 · La ráfaga adelanta el RITMO, nunca cruza el LÍMITE.
                    # `max(allowed, ...)` a secas se saltaba el guardián de cuota
                    # entero y podía comerse la reserva del motor.
                    allowed = min(max(allowed, len(priority_due)),
                                  QUOTA.burst_ceiling(len(priority_due)))
                else:
                    # Ya está servido lo que importa: la ráfaga se apaga sola sin
                    # esperar a que se cumpla su plazo.
                    self._burst_until = 0.0
                    self._arranque_en_frio = False
                    burst = False
            # v1.44.0 · Los datos CRÍTICOS primero.
            #
            # Con presupuesto corto, ordenar sólo por antigüedad hacía que tras un
            # cambio de activo se gastara el turno en noticias y gainers/losers
            # mientras la exposición —que es la que dibuja TRACE— esperaba al ciclo
            # siguiente. La prioridad la fija para qué sirve cada herramienta, no
            # cuánto lleva sin refrescarse.
            # v1.57.0 · Con el tiempo esperado descontado, para que la cola no
            # se muera de hambre. Ver `effective_priority`.
            batch = sorted(remaining_due,
                           key=lambda t: (effective_priority(t.key, self._waited(t.key, now)),
                                          self._fetched_at.get(t.key, self._eligible_since)))[:allowed]
            if batch:
                # v1.58.0 · LA CONCURRENCIA LA GOBIERNA EL GOBERNADOR, NO ESTE LOTE.
                #
                # Aquí había un semáforo local por carril, y el del motor tenía el
                # suyo: dos techos independientes para un mismo proveedor no son
                # un techo. Ahora el límite de peticiones VIVAS es uno y es
                # compartido, con un tope aparte para las pesadas y escalonado
                # entre ellas. Este lote sólo decide QUIÉN entra —eso es cuota y
                # prioridad—; CUÁNTAS a la vez es del gobernador.
                #
                # La ráfaga sigue hidratando rápido: entra más gente en el lote,
                # pero sale escalonada en vez de toda en el mismo milisegundo.
                results = await asyncio.gather(*[self._fetch(t) for t in batch],
                                               return_exceptions=True)
                for tool, res in zip(batch, results):
                    if isinstance(res, Exception):
                        # v1.59.0 · La severidad no la decide el endpoint: la
                        # decide si ALGÚN consumidor declarado exige este dato.
                        _obs_note(f"quantdata_intelligence:{tool.key}", res,
                                  severity=CC.severity_for(tool.key))
            self._cycle += 1
            return {"ready": True, "fetched": len(batch), "reused": reused,
                    "deferred": max(0, len(remaining_due) - len(batch)), "cycle": self._cycle}

    def _adopt(self, tool: QuantDataTool, payload: dict[str, Any], *, source: str,
               epoch: int | None = None) -> None:
        """Normaliza un payload que ya obtuvo otro carril, sin gastar cuota."""
        tool.mark_success()
        tool.last_success = time.time()
        self._fetched_at[tool.key] = tool.last_success
        try:
            normalized = tool.normalize(payload)
        except Exception as exc:
            normalized = {"ready": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
            _obs_note(f"quantdata_intelligence:normalize:{tool.key}", exc, severity="DEGRADED")
        if epoch is not None and epoch != self._epoch:
            _obs_expected("quantdata.intelligence.stale_symbol_adopt")
            return
        self._data[tool.key] = {
            **normalized,
            "symbol": self._symbol,
            "path": tool.resolved_path or (tool.paths[0] if tool.paths else None),
            "source": source,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _note_fault(self, tool: QuantDataTool, status: str, detail: str,
                    *, fields: Any = (), body: Any = None,
                    detail_obj: Any = None) -> None:
        """Deja escrita la CAUSA del fallo sin borrar el último dato bueno.

        v1.46.0 · Cuando una herramienta fallaba, no se escribía nada: el bloque
        quedaba como estaba y el Auditor no podía distinguir «el proveedor
        rechazó el cuerpo» de «todavía no se ha llamado». Ahora la causa viaja
        SIEMPRE, y si había datos anteriores se conservan —degradados por edad,
        que es lo que hace `classify`—, porque un fallo de refresco no es razón
        para tirar lo que ya teníamos.
        """
        previous = self._data.get(tool.key)
        block = dict(previous) if isinstance(previous, dict) else {
            "ready": False, "rows": [], "count": 0,
        }
        block.update({
            "symbol": self._symbol,
            "lane_status": status,
            "lane_detail": str(detail or "")[:240],
            "lane_fields": [str(f) for f in (fields or [])][:8],
            "lane_failed_at": datetime.now(timezone.utc).isoformat(),
        })
        if body is not None:
            block["request_body"] = body
        if detail_obj is not None:
            # El 400 entero, desglosado: `type`, `detail` y cada campo con su
            # mensaje. Es lo único que convierte «HTTP 400» en una corrección.
            block["lane_error"] = detail_obj
        if not block.get("path"):
            block["path"] = tool.resolved_path or (tool.paths[0] if tool.paths else None)
        self._data[tool.key] = block

    async def _fetch(self, tool: QuantDataTool) -> None:
        """Marca la petición como VIVA mientras dura, y la descarga.

        v1.59.0 · `RUNNING` es uno de los nueve estados del programador y no se
        puede deducir de nada de lo que quedaba escrito: una herramienta que está
        llamando AHORA se leía con el resultado de su intento anterior.
        """
        self._en_vuelo.add(tool.key)
        try:
            await self._fetch_one(tool)
        finally:
            self._en_vuelo.discard(tool.key)

    async def _fetch_one(self, tool: QuantDataTool) -> None:
        """Descarga una herramienta, reparando el cuerpo con lo que el proveedor dicta.

        Cinco clases de fallo, cinco tratamientos. Confundirlas fue lo que dejó
        `dark-pool-levels` en DEGRADADO reintentando indefinidamente un cuerpo que
        nunca iba a ser aceptado:

          400 REQUEST_INVALID  la ruta existe y el cuerpo está mal. Se lee QUÉ campo
                               nombró el proveedor y se corrige: añadir el que falta,
                               quitar el que sobra, cambiar el valor inválido. Si no
                               sabemos corregirlo, se DICE el campo y se para.
                               Nunca entra en el ciclo de reintentos.
          422 NO_DATA          la petición era válida y no hay datos. No es un fallo.
          404 MISSING_TOOL     el plan no la incluye o el proveedor la renombró.
          5xx PROVIDER_ERROR   fallo temporal suyo → reintento con backoff.
          --- TRANSIENT        red, timeout, rate limit → reintento con backoff.
        """
        assert self.client is not None
        ticker = self._symbol
        epoch = self._epoch
        last_err = None

        # v1.57.0 · El cortacircuitos decide SI se llama. No decide qué se
        # muestra: mientras está abierto, el último valor bueno se sigue
        # publicando con su edad. Dejar de llamar a un endpoint caído libera el
        # turno para los que sí van a contestar.
        rt = ENDPOINT_RUNTIME.get(tool.key)
        permitido, motivo = rt.allow()
        if not permitido:
            snap = rt.snapshot()
            detalle = (f"circuito {motivo}: {snap['consecutive_failures']} fallos seguidos · "
                       f"reabre en {snap['open_seconds_remaining']:.0f} s · "
                       f"último error: {snap['last_error'] or 'sin detalle'}")
            tool.provider_status = STATUS_TRANSIENT
            self._note_fault(tool, STATUS_TRANSIENT, detalle)
            self._fetched_at[tool.key] = time.time()
            return

        for path in tool.candidates():
            # Como máximo tres correcciones por ciclo. Un 400 que sobrevive a tres
            # correcciones dictadas por el propio proveedor no se arregla probando
            # una cuarta: se arregla leyendo el diagnóstico.
            for _attempt in range(3):
                try:
                    body = tool.request_body(ticker)
                except FaltaRequisito as falta:
                    # v1.57.7 · El contrato exige un campo que todavía no tenemos.
                    # No es el proveedor ni la ruta: mandar la petición igualmente
                    # devuelve un 400 que se lee como endpoint roto, y rellenar el
                    # campo a ojo produce un número creíble y FALSO.
                    self._data[tool.key] = {
                        "ready": False, "rows": [], "count": 0,
                        "state": "REQUISITO_AUSENTE", "symbol": ticker,
                        "path": path, "request_body": None,
                        "lane_status": "REQUISITO_AUSENTE",
                        "lane_fields": [falta.campo],
                        "lane_detail": falta.detalle[:240],
                        "detail": falta.detalle,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self._fetched_at[tool.key] = time.time()
                    _obs_expected(f"quantdata.{tool.key}.requisito_ausente")
                    return
                # La cuota la anota el cliente, que es el único sitio por donde
                # pasan los dos carriles. Ver `QuantDataClient.post`.
                outcome, detail = await self._attempt(tool, path, body, ticker, epoch)
                if outcome == "OK":
                    return
                if outcome == "REPAIRED":
                    continue
                if outcome == "ROUTE_INVALID":
                    last_err = detail
                    break
                # v1.58.0 · UN REINTENTO, Y SÓLO CON PRESUPUESTO.
                #
                # Reintentar un timeout duplica la petición contra la misma
                # cuenta: dos sockets vivos y dos unidades de cuota para un
                # dato. Cuando el proveedor va lento —que es cuando hay
                # timeouts— eso es justo la avalancha que provoca los
                # siguientes. Las cinco condiciones, y el motivo cuando falta
                # alguna, están en `endpoint_runtime.retry_plan`.
                rt = ENDPOINT_RUNTIME.get(tool.key)
                plan = retry_plan(
                    rt,
                    status=self._fail_class.get(tool.key, ""),
                    in_burst=self._bursting(),
                    cycle_remaining_s=max(0.0, self._cycle_ends_at - time.monotonic()),
                    deadline=rt.deadline(),
                    free_slot=GOVERNOR.has_free_slot())
                self._retry_notes[tool.key] = plan
                if not plan["retry"]:
                    return
                rt.mark_retry()
                _obs_expected(f"quantdata.{tool.key}.retry")
                await asyncio.sleep(plan["delay_seconds"])
                outcome, detail = await self._attempt(tool, path, body, ticker, epoch)
                if outcome == "OK":
                    return
                if outcome == "ROUTE_INVALID":
                    last_err = detail
                    break
                return
            else:
                # Se agotaron las correcciones sin que el proveedor aceptara.
                tool.mark_unavailable(
                    f"el proveedor sigue rechazando el cuerpo · {tool.validation_error or ''}")
                self._note_fault(tool, STATUS_REQUEST_INVALID,
                                 tool.validation_error or "cuerpo rechazado tres veces")
                self._fetched_at[tool.key] = time.time()
                return

        tool.mark_unavailable(last_err or "NO_CANDIDATE_PATH")
        self._note_fault(tool, STATUS_MISSING_TOOL,
                         last_err or "ninguna ruta declarada respondió")
        self._fetched_at[tool.key] = time.time()

    async def _attempt(self, tool: QuantDataTool, path: str, body: dict[str, Any],
                       ticker: str, epoch: int) -> tuple[str, str]:
        """Un intento. Devuelve (qué hacer, detalle)."""
        assert self.client is not None
        try:
            # La petición pasa por el runtime del Data Hub: deduplicación en vuelo,
            # aislamiento por canal y Last Known Good. Cada herramienta es su propio
            # canal, así que un fallo de `dark-pool-levels` no tumba a `dark-flow`.
            _client = self.client
            # v1.57.0 · PLAZO MEDIDO, no uno fijo para las treinta y seis.
            #
            # Un plazo único se equivoca en las dos direcciones: demasiado
            # paciente con el que contesta en 200 ms —se esperan diez segundos
            # para saber que está muerto, y ese turno se lo quitas a los sanos—
            # y demasiado impaciente con el que legítimamente tarda ocho.
            #
            # Hasta tener muestra suficiente se usa el configurado: calibrar con
            # tres datos es peor que no calibrar.
            _rt = ENDPOINT_RUNTIME.get(tool.key)
            # v1.58.0 · EL PLAZO LO FIJA EL p95 MEDIDO; EL WARM START ES POLÍTICA.
            #
            # Hasta v1.57.2 el plazo de un endpoint sin historia salía de
            # `QUANTDATA_TIMEOUT_SECONDS`. Con el `=5` del instalador, TODO
            # endpoint sin muestras moría a los cinco segundos y el plazo
            # «adaptativo» no gobernaba nada: ocho endpoints cortados
            # exactamente en 5.0 s en la consola de producción.
            #
            # Ahora el warm start son doce segundos de política —no se puede
            # rebajar desde el entorno— y el plazo trae la CONEXIÓN separada de
            # la LECTURA, que son dos fallos distintos.
            _plazo = _rt.deadline()
            _peso = weight_of(tool.key)

            async def _pedir_payload() -> Dict[str, Any]:
                # v1.57.6 · LO QUE ENTRA EN EL HUB ES SIEMPRE EL PAYLOAD CRUDO:
                # los dos carriles meten el MISMO tipo en la ranura, así que la
                # fusión de peticiones no puede mentir sobre lo que devuelve.
                #
                # v1.58.0 · Y pasa por el GOBERNADOR, que limita cuántas
                # peticiones están VIVAS a la vez —la cuota sólo cuenta cuántas
                # se hacen— y mide dónde se va el tiempo: la cola es nuestra, la
                # petición es del proveedor, y son dos arreglos opuestos.
                turno = await GOVERNOR.acquire(tool.key, weight=_peso,
                                               budget_s=_plazo.total)
                try:
                    respuesta = await _client.post(path, body, timeout=_plazo)
                except QuantDataTimeout:
                    turno.done("TIMEOUT")
                    raise
                except BaseException:
                    turno.done("ERROR")
                    raise
                fila = turno.done("OK")
                self._timings[tool.key] = fila
                # La latencia que calibra el plazo es la de la PETICIÓN, sin la
                # cola: sumarle nuestra espera haría que el plazo persiguiera
                # nuestra propia congestión y creciera sin motivo.
                _rt.record_success(fila["request_ms"] / 1000.0)
                return respuesta.payload

            gate = await HUB_RUNTIME.fetch(
                tool.key, ticker,
                _pedir_payload,
                # El ciclo espera un pelo más que la petición —conexión y lectura
                # más la holgura— para recogerla clasificada en vez de contar el
                # mismo fallo dos veces.
                timeout_s=_plazo.total + CHANNEL_SLACK_S,
                accept_stale=False)
            if not gate.get("ready"):
                inner = gate.get("exception")
                if isinstance(inner, QuantDataError):
                    raise inner
                detail = str(gate.get("detail") or "canal no disponible")
                tool.note_attempt(path, detail)
                tool.provider_status = STATUS_TRANSIENT
                tool.mark_transient(detail)
                _rt.record_failure(detail, status=STATUS_TRANSIENT)
                self._fail_class[tool.key] = STATUS_TRANSIENT
                self._note_fault(tool, STATUS_TRANSIENT, detail, body=body)
                self._fetched_at[tool.key] = time.time()
                return "STOP", detail
            payload = gate["payload"]
            if not isinstance(payload, dict):
                # Cinturón: si algo vuelve a meter otro tipo en la ranura, se dice
                # con el tipo exacto en vez de reventar con un AttributeError a
                # cincuenta líneas de distancia.
                detail = (f"el hub devolvió {type(payload).__name__} en vez del "
                          f"payload del proveedor para {tool.key}")
                tool.note_attempt(path, detail)
                tool.provider_status = STATUS_TRANSIENT
                tool.mark_transient(detail)
                _rt.record_failure(detail, status=STATUS_TRANSIENT)
                self._fail_class[tool.key] = STATUS_TRANSIENT
                self._note_fault(tool, STATUS_TRANSIENT, detail, body=body)
                self._fetched_at[tool.key] = time.time()
                return "STOP", detail
        except QuantDataError as exc:
            msg = str(exc)
            status = classify_provider_failure(exc)
            tool.provider_status = status
            tool.note_attempt(path, msg)
            # Un 400 es culpa del cuerpo, no del canal: no cuenta para abrir el
            # circuito, porque reintentarlo menos no lo arregla.
            if isinstance(exc, QuantDataTimeout):
                # Un plazo agotado es la única clase de fallo que dice algo sobre
                # cuánto tarda el endpoint: que tarda MÁS que el plazo con el que
                # se le llamó. Se guarda como cota inferior para que el siguiente
                # intento le dé el tiempo que pide, en vez de repetir el corte.
                #
                # v1.58.0 · Sólo cuenta como cota si expiró la LECTURA. Un plazo
                # de conexión agotado no dice nada sobre lo que tarda el endpoint
                # en calcular: subir la lectura por eso sería perseguir el
                # síntoma equivocado.
                self._fail_class[tool.key] = "TIMEOUT"
                if str(getattr(exc, "phase", "READ") or "READ").upper() == "READ":
                    ENDPOINT_RUNTIME.get(tool.key).record_timeout(
                        getattr(exc, "limit_seconds", None)
                        or ENDPOINT_RUNTIME.get(tool.key).timeout())
                else:
                    ENDPOINT_RUNTIME.get(tool.key).record_failure(
                        msg, status="CONNECT_TIMEOUT")
            elif status != STATUS_REQUEST_INVALID:
                self._fail_class[tool.key] = status
                ENDPOINT_RUNTIME.get(tool.key).record_failure(msg, status=status)
            else:
                self._fail_class[tool.key] = STATUS_REQUEST_INVALID

            if status == STATUS_REQUEST_INVALID:
                fields = getattr(exc, "validation_fields", None) or []
                tool.note_validation_failure(_validation_detail(exc))
                tool.variants_tried += 1
                # La reparación no puede añadir un campo que ESTA herramienta
                # rechaza, aunque sea legítimo en otra.
                repaired, note = repair_body(
                    body, fields, TOOL_FORBIDDEN_FIELDS.get(tool.key, ()))
                if repaired is None:
                    # El proveedor rechaza algo que no sabemos corregir. Lo correcto
                    # es decir QUÉ campo, no seguir probando formas.
                    tool.mark_unavailable(
                        f"cuerpo rechazado y sin corrección conocida · "
                        f"{tool.validation_error or msg}")
                    self._note_fault(tool, STATUS_REQUEST_INVALID,
                                     tool.validation_error or msg,
                                     fields=fields, body=body,
                                     detail_obj=getattr(exc, "error_detail", None))
                    self._fetched_at[tool.key] = time.time()
                    _obs_note(f"quantdata:{tool.key}:unrepairable", exc, severity="DEGRADED")
                    return "STOP", msg
                tool.learn_repair(body, repaired, note)
                _obs_expected(f"quantdata.{tool.key}.body_repaired")
                return "REPAIRED", note

            if status == STATUS_NO_DATA:
                # Petición válida, sin datos. No es un fallo del programa ni del
                # proveedor, y no debe marcar la herramienta como averiada.
                tool.mark_success()
                tool.last_success = time.time()
                self._fetched_at[tool.key] = tool.last_success
                if epoch == self._epoch:
                    self._data[tool.key] = {
                        "ready": False, "rows": [], "count": 0,
                        "state": "NO_PROVIDER_DATA", "symbol": ticker, "path": path,
                        "request_body": body,
                        "lane_status": STATUS_NO_DATA, "lane_fields": [],
                        "detail": "la petición es válida; el proveedor no tiene datos",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                return "STOP", msg

            if status == STATUS_MISSING_TOOL:
                tool.route_state = ROUTE_INVALID
                self._note_fault(tool, STATUS_MISSING_TOOL, msg, body=body)
                return "ROUTE_INVALID", msg

            tool.mark_transient(msg)
            self._note_fault(tool, status, msg, body=body)
            self._fetched_at[tool.key] = time.time()
            return "STOP", msg
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            tool.note_attempt(path, detail)
            tool.provider_status = STATUS_TRANSIENT
            tool.mark_transient(detail)
            self._note_fault(tool, STATUS_TRANSIENT, detail, body=body)
            self._fetched_at[tool.key] = time.time()
            return "STOP", detail

        tool.note_attempt(path, None)
        tool.route_state = ROUTE_OK
        tool.resolved_path = path
        tool.provider_status = None
        tool.validation_error = None
        tool.mark_success()
        tool.last_success = time.time()
        self._fetched_at[tool.key] = tool.last_success
        try:
            normalized = tool.normalize(payload)
        except Exception as exc:
            normalized = {"ready": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
            _obs_note(f"quantdata_intelligence:normalize:{tool.key}", exc, severity="DEGRADED")
        if epoch != self._epoch:
            # Llegó tarde: el usuario ya cambió de activo. Números correctos bajo el
            # símbolo equivocado es el defecto más difícil de detectar de todos.
            _obs_expected("quantdata.intelligence.stale_symbol_write")
            return "STOP", "época caducada"
        self._data[tool.key] = {
            **normalized,
            "symbol": ticker,
            "path": path,
            "request_body": body,
            "lane_status": (None if normalized.get("ready") or not normalized.get("error")
                            else "PARSER_ERROR"),
            "lane_detail": str(normalized.get("error") or "")[:240],
            "lane_fields": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        return "OK", ""

    # ------------------------------------------------------------ lectura

    def get(self, key: str) -> Dict[str, Any]:
        return dict(self._data.get(key) or {})

    def snapshot(self) -> Dict[str, Any]:
        return {k: dict(v) for k, v in self._data.items()}

    #: Los SIETE veredictos posibles. Uno por remedio distinto: confundirlos
    #: cuesta una tarde buscando en el sitio equivocado.
    VERDICTS = (
        "LIVE_OK",              # sirve dato de este ciclo
        "NO_DATA",              # respondió bien y no hubo actividad en la ventana
        "SIN_INTENTAR",         # todavía no le ha tocado turno
        "NO_AUTORIZADO",        # 401/403 · el plan no la incluye
        "ENDPOINT_NO_EXISTE",   # 404 · no existe o la renombraron. No inventar sustituto
        "REQUEST_INVALID",      # 400/422 · el cuerpo está mal. Reintentarlo no lo arregla
        "PROVIDER_ERROR",       # 5xx, timeout, red · falla y se reintenta con backoff
    )

    @staticmethod
    def _pending_diagnosis(tool: Any, state: str, runtime: Dict[str, Any],
                           scheduler: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Por qué esta herramienta NO está sirviendo, clasificado y accionable.

        v1.57.0 · PARA NO TENER QUE BUSCARLO A MANO.

        El Auditor ya enseñaba el error crudo y las rutas probadas, pero dejaba
        al lector la parte difícil: decidir si una herramienta en PENDIENTE es
        un endpoint que NO EXISTE, uno que existe y NO ESTÁ AUTORIZADO, uno que
        existe y FALLA, o uno que simplemente no se ha llamado todavía.

        Son cuatro remedios distintos y confundirlos cuesta una tarde. Aquí se
        decide con lo que el proveedor contestó, no con una suposición.
        """
        if state == "LIVE":
            return {"verdict": "LIVE_OK", "action": "", "evidence": ""}

        intentos = list(getattr(tool, "attempts", None) or [])
        err = str(getattr(tool, "last_error", "") or "")
        status = str(getattr(tool, "provider_status", "") or "")
        low = err.lower()

        if not intentos and not err:
            # v1.57.1 · «Sin intentar» no es un diagnóstico: es la ausencia de uno.
            # El Auditor tiene que decir POR QUÉ el programador no la ha llamado,
            # que son tres causas distintas con tres remedios distintos.
            sch = dict(scheduler or {})
            enfriando = float(sch.get("cooldown_seconds") or 0.0)
            exigible = sch.get("due")
            espera = sch.get("waited_seconds")
            prio = sch.get("effective_priority")
            base = sch.get("base_priority")
            if enfriando > 0.0:
                accion = (f"en enfriamiento {enfriando:.0f} s tras un fallo anterior: "
                          "el programador no la llamará hasta que venza")
            elif exigible is False:
                accion = ("su cadencia todavía no vence: no es un fallo, es el ritmo "
                          "declarado de la herramienta")
            else:
                pausa = dict(sch.get("pages_paused") or {})
                if pausa:
                    accion = describir_pausa(pausa)
                else:
                    accion = ("exigible y aún sin turno: es PRESUPUESTO DE CUOTA por ciclo, "
                              "no el proveedor. Si persiste con cuota libre, es el programador")
            detalle = f"{len(list(tool.candidates()))} ruta(s) declarada(s), 0 intentos"
            if espera is not None:
                detalle += f"; espera {espera:.0f} s"
            if base is not None and prio is not None:
                detalle += f"; prioridad {base}→{prio}"
            return {"verdict": "SIN_INTENTAR", "action": accion, "evidence": detalle}
        if "401" in err or "403" in err or "unauthor" in low or "forbidden" in low:
            return {"verdict": "NO_AUTORIZADO",
                    "action": "el plan no incluye esta herramienta: no se puede arreglar desde aquí",
                    "evidence": err[:200]}
        if status == STATUS_MISSING_TOOL or "404" in err or "not found" in low:
            return {"verdict": "ENDPOINT_NO_EXISTE",
                    "action": ("ninguna ruta declarada respondió: la herramienta no existe o "
                               "el proveedor la renombró. NO inventar un sustituto"),
                    "evidence": err[:200]}
        if status == STATUS_REQUEST_INVALID or "400" in err or "422" in err:
            campos = list(getattr(tool, "stripped_fields", None) or [])
            return {"verdict": "REQUEST_INVALID",
                    "action": ("el endpoint existe y rechaza el cuerpo: hay que corregir "
                               "request/schema, no reintentarlo"),
                    "evidence": (getattr(tool, "validation_error", "") or err)[:200],
                    "fields": campos[:8]}
        if runtime.get("breaker") == "OPEN":
            return {"verdict": "PROVIDER_ERROR",
                    "action": (f"circuito abierto tras {runtime.get('consecutive_failures')} "
                               f"fallos; reabre en {runtime.get('open_seconds_remaining')} s"),
                    "evidence": (runtime.get("last_error") or err)[:200]}
        if err:
            return {"verdict": "PROVIDER_ERROR",
                    "action": "el endpoint responde y falla: corregir parsing o esperar al proveedor",
                    "evidence": err[:200]}
        return {"verdict": "NO_DATA",
                "action": "el proveedor respondió bien y no había actividad en la ventana",
                "evidence": ""}

    def coverage(self) -> Dict[str, Any]:
        """Cobertura real por página integrada del proveedor."""
        now = time.time()
        pausa_cuota = QUOTA.pages_paused_reason()
        tools = []
        #: Estado de cada dato, para evaluar a los CONSUMIDORES (bloque 3). Sólo
        #: entra lo que este carril mide de verdad: lo que no se mide se queda
        #: fuera del mapa y el consumidor sale `UNKNOWN`, que es distinto de
        #: salir bloqueado.
        disponibilidad: Dict[str, str] = {}
        for key, tool in self.catalog.items():
            data = self._data.get(key) or {}
            esperado = self._waited(key, now)
            scheduler = {
                "waited_seconds": round(esperado, 1),
                "base_priority": _PRIORITY.get(key, _PRIORITY_DEFAULT),
                "effective_priority": effective_priority(key, esperado),
                "due": bool(self._due(tool, now)),
                "cooldown_seconds": round(max(0.0, float(tool.unavailable_until) - now), 1),
                "never_fetched": key not in self._fetched_at,
                # La causa que está POR ENCIMA del programador: si el plan está
                # agotado, ninguna prioridad sirve de nada.
                "pages_paused": pausa_cuota,
            }
            last = tool.last_success
            route = route_diagnostic(tool)
            # v1.42.1 · Un vacío por mercado cerrado NO es lo mismo que un vacío con
            # el mercado abierto. Antes los dos decían SIN_DATOS y el operador tenía
            # que adivinar cuál de los dos estaba mirando.
            empty = session_resolver.empty_reason(channel=tool.title)
            if route["state"] == ROUTE_INVALID:
                state = "RUTA_INVALIDA"
            elif data.get("ready") and not tool.last_error:
                state = "LIVE"
            # v1.59.0 · Antes bastaba con que quedara dato anterior publicado para
            # seguir diciendo LIVE aunque el último intento hubiera fallado. Eso
            # es servir el ciclo anterior con la etiqueta del actual: lo que se
            # está sirviendo es el último valor bueno, y se dice.
            elif tool.last_error and tool.transient_failures > 0:
                # Timeout/red: DEGRADED con reintento programado, nunca se presenta
                # como ruta inexistente ni se martilla cada 15 segundos.
                state = "DEGRADADO"
            elif tool.last_error and not tool.available(now):
                state = "NO_DISPONIBLE"
            elif tool.last_error:
                state = "DEGRADADO"
            elif last and not empty["is_failure"]:
                state = empty["state"]          # MARKET_CLOSED / SESSION_NOT_STARTED / …
            elif last:
                state = "SIN_DATOS"
            else:
                state = "PENDIENTE"
            # ═══════════════════════════════════════════════════════════
            # v1.59.0 · BLOQUE 7 · EL ESTADO REAL, NO EL CAJÓN GENÉRICO
            # ═══════════════════════════════════════════════════════════
            #
            # `state` conserva el vocabulario que ya consume la interfaz.
            # `lifecycle` dice cuál de los NUEVE estados del programador es, con
            # su causa, y separa lo que se está sirviendo AHORA de lo que se
            # sirve del último ciclo bueno. La distinción importa: un refresco
            # que falla no borra el dato anterior, pero tampoco lo convierte en
            # dato de este ciclo.
            fallo_actual = bool(tool.last_error)
            filas_publicadas = int(data.get("count") or 0)
            sirviendo_lkg = bool(data.get("ready")) and fallo_actual
            lifecycle = SS.classify(
                due=scheduler["due"],
                in_flight=(key in self._en_vuelo),
                cooldown_seconds=scheduler["cooldown_seconds"],
                quota_paused=pausa_cuota,
                missing_dependencies=list(data.get("lane_fields") or [])
                if str(data.get("lane_status") or "") == "REQUISITO_AUSENTE" else [],
                rows=(0 if fallo_actual else filas_publicadas),
                provider_status=(tool.provider_status or
                                 (STATUS_TRANSIENT if fallo_actual else "")),
                lkg_rows=(filas_publicadas if sirviendo_lkg else 0),
                lkg_age_seconds=(round(now - last, 1) if last else None),
                fresh=(not fallo_actual),
                ever_attempted=bool(tool.attempts or last or tool.last_error))
            anomalia = SS.anomaly(
                state=lifecycle["state"], due=scheduler["due"],
                attempts=len(tool.attempts or []), eligible_seconds=esperado,
                quota_paused=pausa_cuota)
            criticidad = CC.criticality(key)
            disponibilidad[key] = lifecycle["state"]
            tools.append({
                "key": key,
                "page": tool.page,
                "title": tool.title,
                "state": state,
                # Los nueve estados del programador, con su causa.
                "lifecycle": lifecycle,
                # Y la anomalía, que NO es un estado: es un defecto del
                # programador —exigible, con cuota libre y sin un solo intento—.
                "anomaly": anomalia,
                # Para quién es obligatorio este dato y para quién opcional. De
                # ahí sale la severidad de su fallo, no del endpoint.
                "criticality": criticidad,
                "path": tool.resolved_path,
                "age_seconds": round(now - last, 1) if last else None,
                "rows": data.get("count"),
                "error": tool.last_error,
                "source": data.get("source") or "PAGES_LANE",
                # Procedencia del DATO, distinta del carril que lo trajo. Si la
                # herramienta respondió, el dato es del proveedor: que el motor lo
                # consuma después para derivar inteligencia no lo convierte en
                # cálculo propio.
                "source_mode": ("DIRECT_PROVIDER" if data.get("ready")
                                else "UNAVAILABLE"),
                "data_provider": "QUANTDATA",
                "request_body": data.get("request_body"),
                "cadence_seconds": CADENCE.get(tool.cadence, 60.0),
                "retry_in_seconds": (round(max(0.0, tool.unavailable_until - now), 1)
                                     if tool.transient_failures > 0 else None),
                "transient_failures": int(tool.transient_failures),
                # Qué rutas se probaron y qué contestó el proveedor en cada una. Es lo
                # que convierte un NO_DISPONIBLE en algo sobre lo que se puede actuar.
                "attempts": [
                    {"path": a.get("path"), "ok": a.get("ok"), "error": a.get("error")}
                    for a in (tool.attempts or [])[-6:]
                ],
                "candidates": list(tool.candidates()),
                # Ruta canónica y su veredicto. Sustituye a la lista de seis URLs
                # derivadas que sólo servía para confundir.
                "route": route,
                "empty_reason": None if state in ("LIVE", "PENDIENTE") else empty["detail"],
                "session": empty["session"]["session_date"],
                # Plazo calibrado, latencia medida y cortacircuitos de ESTE endpoint.
                "runtime": ENDPOINT_RUNTIME.get(key).snapshot(),
                # El veredicto ya clasificado: existe / no existe / no autorizado /
                # existe y falla / sin intentar, con el remedio de cada uno.
                # Estado del PROGRAMADOR para esta herramienta: cuánto lleva
                # esperando, a qué prioridad ha ascendido y si es exigible ahora.
                # Es lo que convierte un «sin intentos» en una causa concreta.
                "scheduler": scheduler,
                "diagnosis": self._pending_diagnosis(
                    tool, state, ENDPOINT_RUNTIME.get(key).snapshot(), scheduler),
            })

        # ═══════════════════════════════════════════════════════════════
        # v1.59.0 · BLOQUE 3 · LO QUE NO MIDE ESTE CARRIL
        # ═══════════════════════════════════════════════════════════════
        #
        # `expiry_selection` y `underlying_price` no son herramientas del
        # catálogo: las resuelve la terminal. Aquí sólo se declara lo que de
        # verdad se puede comprobar desde este proceso, y lo que no, se deja
        # FUERA del mapa. Un consumidor al que le falta una medición sale
        # `UNKNOWN`, no `BLOCKED`: decir que las Walls están bloqueadas porque
        # nadie preguntó por el precio sería un falso negativo.
        sym = str(self._symbol or "").upper()
        if sym:
            # El registro de vencimientos SÍ es consultable, y su ausencia es
            # una medición: sin vencimiento en pantalla no hay muro con fecha.
            disponibilidad["expiry_selection"] = (
                "DATA_OK" if EXPIRY_SELECTION.principal(sym) else "NO_DATA")
            # Del precio sólo se afirma lo observado: si hay un snapshot de
            # muros reciente, el precio entró en él. Sin snapshot no se afirma
            # nada —ni que hay precio ni que falta—, porque este carril no lo ve.
            edad_precio = WALL_SNAPSHOTS.age_seconds(sym)
            if edad_precio is not None and edad_precio <= _WALL_FRESCO_S:
                disponibilidad["underlying_price"] = (
                    "LIVE" if edad_precio <= 60.0 else "STALE")

        pages = []
        by_key = {t["key"]: t for t in tools}
        for page, keys in PAGES.items():
            states = [by_key[k]["state"] for k in keys if k in by_key]
            live = sum(1 for s in states if s == "LIVE")
            pages.append({
                "page": page,
                "tools": len(states),
                "live": live,
                "state": "LIVE" if live == len(states) and states else "PARCIAL" if live else "PENDIENTE",
            })

        por_veredicto: Dict[str, List[str]] = {}
        for t in tools:
            v = str((t.get("diagnosis") or {}).get("verdict") or "LIVE_OK")
            if v != "LIVE_OK":
                por_veredicto.setdefault(v, []).append(t["key"])

        return {
            "configured": self.settings.configured,
            "running": self._running,
            "symbol": self._symbol,
            "cycle": self._cycle,
            # Resumen accionable: qué hay que hacer y con cuántas herramientas.
            "pending_by_verdict": {k: sorted(v) for k, v in sorted(por_veredicto.items())},
            # v1.59.0 · QUIÉN SE QUEDA SIN QUÉ. La criticidad es del PAR
            # (dato, consumidor): el mismo fallo es opcional para quien tiene
            # respaldo y bloqueante para quien no lo tiene.
            "consumers": CC.evaluate_all(disponibilidad),
            # Y los nueve estados del programador, con las anomalías aparte:
            # una herramienta exigible, con cuota libre y sin un solo intento no
            # está esperando, es que nadie la llama.
            "scheduler_states": SS.summarize(
                [{"key": t["key"], "state": (t.get("lifecycle") or {}).get("state"),
                  "anomaly": t.get("anomaly")} for t in tools]),
            "endpoint_runtime": ENDPOINT_RUNTIME.snapshot(),
            # v1.58.0 · DÓNDE SE VA EL TIEMPO, por endpoint.
            #
            # `queue_wait_ms` es congestión NUESTRA y `request_ms` es lentitud
            # del PROVEEDOR. Sin separarlas, «tardó nueve segundos» no distingue
            # las dos y los arreglos son opuestos: al proveedor lento se le da
            # más plazo; a la cola propia, menos concurrencia.
            "governor": GOVERNOR.snapshot(),
            "timings": [dict(v, key=k) for k, v in sorted(self._timings.items())],
            "timeout_policy": self.settings.timeout_policy(),
            # Por qué NO se reintentó, herramienta a herramienta. Un «no» sin
            # causa no se puede diagnosticar.
            "retry_decisions": [dict(v, key=k)
                                for k, v in sorted(self._retry_notes.items())],
            "drift": [dict(ENDPOINT_RUNTIME.get(k).drift(), key=k)
                      for k in sorted(self._timings)],
            "tools": sorted(tools, key=lambda t: (t["page"], t["title"])),
            "pages": pages,
            "live_tools": sum(1 for t in tools if t["state"] == "LIVE"),
            "total_tools": len(tools),
            "shared_with_engine": sum(1 for t in tools if t.get("source") == "ENGINE_LANE_SHARED"),
            "quota": QUOTA.snapshot(),
            "raw_cache": RAW_CACHE.stats(),
        }


QUANTDATA_INTELLIGENCE = QuantDataIntelligence()
