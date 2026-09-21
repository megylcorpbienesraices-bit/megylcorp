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
from typing import Any, Dict

from .client import QuantDataClient, QuantDataError
from .settings import load_settings, QuantDataSettings
from ...core import session_resolver
from .tools import (build_catalog, is_missing_tool_error, is_validation_error,
                    _validation_detail, repair_body, classify_provider_failure,
                    STATUS_REQUEST_INVALID, STATUS_NO_DATA, STATUS_MISSING_TOOL,
                    TOOL_FORBIDDEN_FIELDS,
                    STATUS_TRANSIENT, CADENCE, PAGES, QuantDataTool,
                    ROUTE_OK, ROUTE_INVALID, route_diagnostic)
from .shared import RAW_CACHE, QUOTA, ENGINE_SHARED_KEYS
from ...core.obs import note as _obs_note, expected as _obs_expected
from ...core.data_hub_runtime import HUB_RUNTIME

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
BURST_CONCURRENCY = 5
BURST_CYCLE_SECONDS = 1.2

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
    "contract_statistics": 3, "trade_side_statistics": 3, "market_share": 3,
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


class QuantDataIntelligence:
    """Recolector de páginas integradas del proveedor."""

    def __init__(self) -> None:
        self.settings: QuantDataSettings = load_settings()
        self.catalog: Dict[str, QuantDataTool] = build_catalog()
        self.client: QuantDataClient | None = None
        self._symbol = "DIA"
        self._task: asyncio.Task | None = None
        self._stop = False
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._fetched_at: Dict[str, float] = {}
        self._running = False
        self._cycle = 0
        self._burst_until = 0.0
        # Época del símbolo: avanza en cada cambio de activo y ata cada respuesta
        # al ticker que la pidió.
        self._epoch = 0

    # ------------------------------------------------------------- ciclo

    @property
    def configured(self) -> bool:
        return self.settings.configured

    async def start(self, symbol: str) -> None:
        self.settings = load_settings()
        self._symbol = str(symbol or "DIA").upper().strip()
        if not self.settings.configured:
            return
        if self.client is None:
            self.client = QuantDataClient(self.settings)
            await self.client.start()
        self._stop = False
        if self._task is None or self._task.done():
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
        # Se limpia la caché de los DOS símbolos: la del anterior porque ya no
        # describe nada que vaya a mostrarse, y la del nuevo porque pudo quedar
        # sembrada por una consulta previa y ser más vieja que este cambio.
        RAW_CACHE.clear_symbol(previous)
        RAW_CACHE.clear_symbol(sym)
        # Ráfaga de arranque: lo que dibuja la pantalla, ya.
        self._burst_until = time.monotonic() + BURST_SECONDS
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
                priority_due = [t for t in remaining_due
                                if _PRIORITY.get(t.key, _PRIORITY_DEFAULT) <= BURST_MAX_PRIORITY]
                if priority_due:
                    remaining_due = priority_due
                    allowed = max(allowed, len(priority_due))
                else:
                    # Ya está servido lo que importa: la ráfaga se apaga sola sin
                    # esperar a que se cumpla su plazo.
                    self._burst_until = 0.0
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
                           key=lambda t: (effective_priority(
                                              t.key, now - self._fetched_at.get(t.key, 0.0)),
                                          self._fetched_at.get(t.key, 0.0)))[:allowed]
            if batch:
                # Las páginas son corroboración, no autoridad del motor. Lanzar ocho
                # POST pesados a la vez contra una cuenta pequeña aumenta timeouts y
                # puede hacer parecer degradado al proveedor aunque el carril del motor
                # siga sano. Dos en paralelo mantiene la UI diligente sin martillar la
                # API ni consumir conexiones innecesarias.
                sem = asyncio.Semaphore(BURST_CONCURRENCY if burst else PAGE_MAX_CONCURRENCY)

                async def _bounded(tool: QuantDataTool) -> None:
                    async with sem:
                        await self._fetch(tool)

                results = await asyncio.gather(*[_bounded(t) for t in batch], return_exceptions=True)
                for tool, res in zip(batch, results):
                    if isinstance(res, Exception):
                        _obs_note(f"quantdata_intelligence:{tool.key}", res, severity="DEGRADED")
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

        for path in tool.candidates():
            # Como máximo tres correcciones por ciclo. Un 400 que sobrevive a tres
            # correcciones dictadas por el propio proveedor no se arregla probando
            # una cuarta: se arregla leyendo el diagnóstico.
            for _attempt in range(3):
                body = tool.request_body(ticker)
                QUOTA.spend(1)
                outcome, detail = await self._attempt(tool, path, body, ticker, epoch)
                if outcome == "OK":
                    return
                if outcome == "REPAIRED":
                    continue
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
            gate = await HUB_RUNTIME.fetch(
                tool.key, ticker,
                lambda: _client.post(path, body),
                timeout_s=self.settings.request_timeout_seconds + 1.0,
                accept_stale=False)
            if not gate.get("ready"):
                inner = gate.get("exception")
                if isinstance(inner, QuantDataError):
                    raise inner
                detail = str(gate.get("detail") or "canal no disponible")
                tool.note_attempt(path, detail)
                tool.provider_status = STATUS_TRANSIENT
                tool.mark_transient(detail)
                self._note_fault(tool, STATUS_TRANSIENT, detail, body=body)
                self._fetched_at[tool.key] = time.time()
                return "STOP", detail
            response = gate["payload"]
        except QuantDataError as exc:
            msg = str(exc)
            status = classify_provider_failure(exc)
            tool.provider_status = status
            tool.note_attempt(path, msg)

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
            normalized = tool.normalize(response.payload)
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

    def coverage(self) -> Dict[str, Any]:
        """Cobertura real por página integrada del proveedor."""
        now = time.time()
        tools = []
        for key, tool in self.catalog.items():
            data = self._data.get(key) or {}
            last = tool.last_success
            route = route_diagnostic(tool)
            # v1.42.1 · Un vacío por mercado cerrado NO es lo mismo que un vacío con
            # el mercado abierto. Antes los dos decían SIN_DATOS y el operador tenía
            # que adivinar cuál de los dos estaba mirando.
            empty = session_resolver.empty_reason(channel=tool.title)
            if route["state"] == ROUTE_INVALID:
                state = "RUTA_INVALIDA"
            elif data.get("ready"):
                state = "LIVE"
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
            tools.append({
                "key": key,
                "page": tool.page,
                "title": tool.title,
                "state": state,
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
            })

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

        return {
            "configured": self.settings.configured,
            "running": self._running,
            "symbol": self._symbol,
            "cycle": self._cycle,
            "tools": sorted(tools, key=lambda t: (t["page"], t["title"])),
            "pages": pages,
            "live_tools": sum(1 for t in tools if t["state"] == "LIVE"),
            "total_tools": len(tools),
            "shared_with_engine": sum(1 for t in tools if t.get("source") == "ENGINE_LANE_SHARED"),
            "quota": QUOTA.snapshot(),
            "raw_cache": RAW_CACHE.stats(),
        }


QUANTDATA_INTELLIGENCE = QuantDataIntelligence()
