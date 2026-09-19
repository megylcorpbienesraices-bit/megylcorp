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
from .tools import (build_catalog, is_missing_tool_error, CADENCE, PAGES, QuantDataTool,
                    ROUTE_OK, ROUTE_INVALID, route_diagnostic)
from .shared import RAW_CACHE, QUOTA, ENGINE_SHARED_KEYS
from ...core.obs import note as _obs_note, expected as _obs_expected
from ...core.data_hub_runtime import HUB_RUNTIME

PAGE_MAX_CONCURRENCY = 2

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
        if self.settings.configured:
            self._wake.set()

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
            # v1.44.0 · Los datos CRÍTICOS primero.
            #
            # Con presupuesto corto, ordenar sólo por antigüedad hacía que tras un
            # cambio de activo se gastara el turno en noticias y gainers/losers
            # mientras la exposición —que es la que dibuja TRACE— esperaba al ciclo
            # siguiente. La prioridad la fija para qué sirve cada herramienta, no
            # cuánto lleva sin refrescarse.
            batch = sorted(remaining_due,
                           key=lambda t: (_PRIORITY.get(t.key, _PRIORITY_DEFAULT),
                                          self._fetched_at.get(t.key, 0.0)))[:allowed]
            if batch:
                # Las páginas son corroboración, no autoridad del motor. Lanzar ocho
                # POST pesados a la vez contra una cuenta pequeña aumenta timeouts y
                # puede hacer parecer degradado al proveedor aunque el carril del motor
                # siga sano. Dos en paralelo mantiene la UI diligente sin martillar la
                # API ni consumir conexiones innecesarias.
                sem = asyncio.Semaphore(PAGE_MAX_CONCURRENCY)

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

    async def _fetch(self, tool: QuantDataTool) -> None:
        assert self.client is not None
        ticker = self._symbol
        epoch = self._epoch
        QUOTA.spend(1)
        body = tool.body(ticker)
        last_err: str | None = None

        for path in tool.candidates():
            try:
                # v1.44.0 · La petición pasa por el runtime del Data Hub:
                #   · deduplicación en vuelo — dos secciones que piden lo mismo a la
                #     vez comparten UNA petición en lugar de gastar dos de cuota;
                #   · aislamiento por canal — si esta herramienta tarda, el ciclo
                #     sigue sin ella y las demás no se contagian;
                #   · Last Known Good — lo que llegue tarde alimenta el respaldo.
                # El precio no pasa por aquí: viaja por otro carril y no puede
                # quedarse esperando a un endpoint de opciones.
                _client = self.client
                gate = await HUB_RUNTIME.fetch(
                    tool.key, ticker,
                    lambda: _client.post(path, body),
                    timeout_s=self.settings.request_timeout_seconds + 1.0,
                    accept_stale=False)
                if not gate.get("ready"):
                    detail = str(gate.get("detail") or "canal no disponible")
                    tool.note_attempt(path, detail)
                    tool.mark_transient(detail)
                    self._fetched_at[tool.key] = time.time()
                    return
                response = gate["payload"]
            except QuantDataError as exc:
                msg = str(exc)
                last_err = msg
                tool.note_attempt(path, msg)
                if is_missing_tool_error(msg):
                    # v1.42.1 · La ruta es canónica, así que un 404 aquí no significa
                    # «probemos otra»: significa que esta cuenta no sirve la
                    # herramienta o que el proveedor la renombró. Ambas cosas son
                    # accionables; adivinar una URL alternativa no lo era.
                    tool.route_state = ROUTE_INVALID
                    continue
                # Error transitorio (timeout, rate limit): se reintenta en el próximo ciclo
                # sin descartar la ruta, que puede ser perfectamente válida.
                tool.mark_transient(msg)
                self._fetched_at[tool.key] = time.time()
                return
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                tool.note_attempt(path, last_err)
                tool.mark_transient(last_err)
                self._fetched_at[tool.key] = time.time()
                return

            tool.note_attempt(path, None)
            tool.route_state = ROUTE_OK
            tool.resolved_path = path
            tool.mark_success()
            tool.last_success = time.time()
            self._fetched_at[tool.key] = tool.last_success
            try:
                normalized = tool.normalize(response.payload)
            except Exception as exc:
                normalized = {"ready": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
                _obs_note(f"quantdata_intelligence:normalize:{tool.key}", exc, severity="DEGRADED")
            if epoch != self._epoch:
                # Llegó tarde: el usuario ya cambió de activo. El dato es válido
                # para SU ticker, no para el que está en pantalla, así que no se
                # publica. Mezclarlos sería el defecto más difícil de detectar de
                # todos: números correctos bajo el símbolo equivocado.
                _obs_expected("quantdata.intelligence.stale_symbol_write")
                return
            self._data[tool.key] = {
                **normalized,
                "symbol": ticker,
                "path": path,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            return

        # Ninguna ruta candidata existe en esta cuenta/plan.
        tool.mark_unavailable(last_err or "NO_CANDIDATE_PATH")
        self._fetched_at[tool.key] = time.time()

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
