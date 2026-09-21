from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .shared import (RAW_CACHE, QUOTA, ENGINE_FAST_JOBS, ENGINE_SLOW_JOBS,
                     ENGINE_FAST_REQUESTS, ENGINE_SLOW_EVERY_N_CYCLES)
from .client import QuantDataClient
from .settings import QuantDataSettings, load_settings
from ...core.provider_bus import FEATURE_BUS
from ...core.obs import note as _obs_note, expected as _obs_expected
from ...core.data_hub_runtime import HUB_RUNTIME


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _sign_conf(net: float, gross: float) -> tuple[int, float]:
    if not math.isfinite(net) or not math.isfinite(gross) or gross <= 0:
        return 0, 0.0
    ratio = max(-1.0, min(1.0, net / gross))
    sign = 1 if ratio > 0.015 else -1 if ratio < -0.015 else 0
    return sign, round(min(100.0, abs(ratio) * 100.0), 2)


def _exposure_summary(payload: dict[str, Any], ticker: str) -> dict[str, Any]:
    data = payload.get("data") or {}
    node = data.get(ticker) or data.get(ticker.upper()) or data.get(ticker.lower()) if isinstance(data, dict) else {}
    if not isinstance(node, dict):
        node = {}
    exposure_map = node.get("exposureMap") or {}
    stock_price = _f(node.get("stockPrice"))
    rows: list[tuple[float, float, float]] = []
    total_call = 0.0
    total_put = 0.0
    if isinstance(exposure_map, dict):
        for _, strikes in exposure_map.items():
            if not isinstance(strikes, dict):
                continue
            for strike_raw, cell in strikes.items():
                strike = _f(strike_raw)
                if strike is None or not isinstance(cell, dict):
                    continue
                call = _f(cell.get("callExposure"), 0.0) or 0.0
                put = _f(cell.get("putExposure"), 0.0) or 0.0
                total_call += call
                total_put += put
                rows.append((strike, call, put))
    net = total_call + total_put
    gross = abs(total_call) + abs(total_put)
    sign, confidence = _sign_conf(net, gross)
    by_strike: dict[float, float] = {}
    for strike, call, put in rows:
        by_strike[strike] = by_strike.get(strike, 0.0) + call + put
    ranked = sorted(by_strike.items(), key=lambda kv: abs(kv[1]), reverse=True)[:12]
    return {
        "available": bool(rows),
        "ticker": ticker,
        "stock_price": stock_price,
        "net": net,
        "gross": gross,
        "call": total_call,
        "put": total_put,
        "sign": sign,
        "confidence": confidence,
        "top_strikes": [{"strike": s, "net": v} for s, v in ranked],
    }


def _bucket_flow(payload: dict[str, Any], *, signed_put: bool = True) -> dict[str, Any]:
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data:
        return {"available": False, "sign": 0, "confidence": 0.0}
    items = []
    for key, row in data.items():
        if not isinstance(row, dict):
            continue
        call = _f(row.get("netCallPremium"), _f(row.get("callSum"), 0.0)) or 0.0
        put = _f(row.get("netPutPremium"), _f(row.get("putSum"), 0.0)) or 0.0
        directional = call + put if signed_put else call - put
        gross = abs(call) + abs(put)
        items.append((str(key), call, put, directional, gross, _f(row.get("stockPrice"))))
    if not items:
        return {"available": False, "sign": 0, "confidence": 0.0}
    # Favor the freshest six buckets while retaining accumulated direction.
    items.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    recent = items[-6:]
    net = sum(x[3] for x in recent)
    gross = sum(x[4] for x in recent)
    sign, confidence = _sign_conf(net, gross)
    last = recent[-1]
    return {
        "available": True,
        "sign": sign,
        "confidence": confidence,
        "net_recent": net,
        "gross_recent": gross,
        "last_bucket": last[0],
        "stock_price": last[5],
    }




def _net_drift_series(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Quant Data Net Drift buckets without changing provider semantics.

    The API returns per-bucket call/put premium and stockPrice.  We preserve the
    signed values exactly as delivered and also compute cumulative curves locally,
    which is the construction documented by Quant Data for its Net Drift chart.
    """
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data:
        return {"available": False, "buckets": [], "source": "QUANTDATA_NET_DRIFT"}
    rows: list[dict[str, Any]] = []
    # Arrancan en `None`: un acumulado en cero es una AFIRMACION («no se movio
    # nada») y aqui todavia no se ha medido nada. Solo lo medido lo convierte
    # en numero.
    call_cum: float | None = None
    put_cum: float | None = None
    call_vol_cum: float | None = None
    put_vol_cum: float | None = None

    def _acc(total: float | None, value: float | None) -> float | None:
        if value is None:
            return total
        return value if total is None else total + value

    def _add(a: float | None, b: float | None) -> float | None:
        return None if (a is None and b is None) else (a or 0.0) + (b or 0.0)
    def _key(item: tuple[Any, Any]) -> int:
        try:
            return int(str(item[0]))
        except Exception:
            return 0
    for key, row in sorted(data.items(), key=_key):
        if not isinstance(row, dict):
            continue
        try:
            ts_ms = int(str(key))
        except Exception as exc:
            _obs_note("runtime:net_drift_timestamp", exc)
            continue
        # Campo ausente -> `None`, no cero. El bucket queda como hueco y el
        # grafico dibuja una discontinuidad en vez de una barra a cero que
        # afirmaria que ese minuto no hubo prima.
        call = _f(row.get("netCallPremium"))
        put = _f(row.get("netPutPremium"))
        call_vol = _f(row.get("netCallVolume"))
        put_vol = _f(row.get("netPutVolume"))
        call_cum = _acc(call_cum, call)
        put_cum = _acc(put_cum, put)
        call_vol_cum = _acc(call_vol_cum, call_vol)
        put_vol_cum = _acc(put_vol_cum, put_vol)
        px = _f(row.get("stockPrice"))
        if px is not None and px <= 0:
            px = None
        rows.append({
            "timestamp_ms": ts_ms,
            "timestamp": datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat(),
            "net_call_premium": call,
            "net_put_premium": put,
            "net_call_volume": call_vol,
            "net_put_volume": put_vol,
            "mid_call_premium": _f(row.get("midMarketCallPremium")),
            "mid_put_premium": _f(row.get("midMarketPutPremium")),
            "stock_price": px,
            "cum_call_premium": call_cum,
            "cum_put_premium": put_cum,
            "cum_net_premium": _add(call_cum, put_cum),
            "cum_call_volume": call_vol_cum,
            "cum_put_volume": put_vol_cum,
        })
    return {
        "available": bool(rows),
        "source": "QUANTDATA_NET_DRIFT",
        "provider": "QUANTDATA",
        "aggregation": "1m",
        "buckets": rows,
        "latest": rows[-1] if rows else None,
    }


def _interval_gamma_migration(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    if not isinstance(data, dict) or len(data) < 2:
        return {"available": False}
    keys = sorted(data.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)

    def flatten(bucket: Any) -> dict[float, float]:
        out: dict[float, float] = {}
        if not isinstance(bucket, dict):
            return out
        for _, strikes in bucket.items():
            if not isinstance(strikes, dict):
                continue
            for strike_raw, cell in strikes.items():
                strike = _f(strike_raw)
                if strike is None or not isinstance(cell, dict):
                    continue
                v = (_f(cell.get("CALL"), 0.0) or 0.0) + (_f(cell.get("PUT"), 0.0) or 0.0)
                out[strike] = out.get(strike, 0.0) + v
        return out

    prev, cur = flatten(data[keys[-2]]), flatten(data[keys[-1]])
    strikes = set(prev) | set(cur)
    diffs = {s: cur.get(s, 0.0) - prev.get(s, 0.0) for s in strikes}
    if not diffs:
        return {"available": False}
    top = sorted(diffs.items(), key=lambda kv: abs(kv[1]), reverse=True)[:12]
    total_change = sum(diffs.values())
    gross_change = sum(abs(v) for v in diffs.values())
    sign, confidence = _sign_conf(total_change, gross_change)
    return {
        "available": True,
        "previous_bucket": keys[-2],
        "latest_bucket": keys[-1],
        "net_change": total_change,
        "gross_change": gross_change,
        "sign": sign,
        "confidence": confidence,
        "top": [{"strike": s, "change": v} for s, v in top],
    }


def _iv_rank_summary(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data:
        return {"available": False}
    keys = sorted(data.keys())
    row = data.get(keys[-1]) or {}
    if not isinstance(row, dict):
        return {"available": False}
    legs = row.get("contractTypeToIVData") or {}

    def rank(leg: str) -> float | None:
        cell = legs.get(leg) if isinstance(legs, dict) else None
        if not isinstance(cell, dict):
            return None
        last = _f(cell.get("lastIv"))
        lo = _f(cell.get("windowMinIv"))
        hi = _f(cell.get("windowMaxIv"))
        if last is None or lo is None or hi is None or hi <= lo:
            return None
        return max(0.0, min(100.0, 100.0 * (last - lo) / (hi - lo)))

    return {
        "available": True,
        "session_date": keys[-1],
        "expiration_date": row.get("expirationDate"),
        "stock_price": _f(row.get("stockPrice")),
        "call_rank": rank("CALL"),
        "put_rank": rank("PUT"),
    }


@dataclass
class QuantDataTarget:
    active_symbol: str
    query_ticker: str
    direct: bool


class QuantDataRuntime:
    """Background-only Quant Data REST lane.

    The runtime never owns price/TRACE and never blocks the Quant refresh. It hydrates
    the provider-neutral feature fabric; Scanner consumes only the latest fresh semantic
    observations already present in memory.
    """

    def __init__(self) -> None:
        self.settings: QuantDataSettings = load_settings()
        self.client: QuantDataClient | None = None
        self._symbol = "DIA"
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stop = False
        self._lock = asyncio.Lock()
        self._status: dict[str, Any] = {
            "configured": self.settings.configured,
            "running": False,
            "last_success": None,
            "last_error": None,
            "query_ticker": None,
            "remaining": None,
            "limit": None,
            "reset_seconds": None,
        }
        self._last_payload: dict[str, Any] = {}
        self._cycle = 0

    @property
    def configured(self) -> bool:
        return self.settings.configured

    # Instrumentos cuya matemática es propia y que NO tienen cadena en Quant Data:
    # se consulta DIA como contexto normalizado del ecosistema Dow, nunca como proxy
    # crudo de su exposición. Es la única razón legítima para consultar un ticker
    # distinto del seleccionado.
    _ECOSYSTEM_PROXY = {"YM": "DIA", "MYM": "DIA", "DJX": "DIA", "VIX": "DIA", "VXD": "DIA"}

    @staticmethod
    def _target(symbol: str) -> QuantDataTarget:
        """Qué ticker se le pide a Quant Data y si la respuesta es del instrumento.

        v1.42.6 · La lista blanca `{DIA, XLI, XLF}` más un catálogo de ETFs dejaba a
        TODA acción (AAPL, NVDA, TSLA, MSFT…) cayendo al `return` final con
        ``direct=False``, pese a consultar su PROPIO ticker. Y `direct=False` está
        documentado para el caso contrario: cuando se consulta DIA como contexto de
        un futuro. El resultado era que la evidencia de esas acciones llegaba
        degradada a canal "ecosystem" en vez de alimentar Gamma/Delta, y secciones
        como Flujo/QFLOW quedaban vacías fuera de DIA.

        La regla no necesita catálogo ni lista: **la evidencia es directa cuando se
        consulta el ticker del propio instrumento**. Sólo deja de serlo cuando se
        pide otro a propósito, que es exactamente lo que declara `_ECOSYSTEM_PROXY`.
        """
        sym = str(symbol or "DIA").upper().strip()
        proxy = QuantDataRuntime._ECOSYSTEM_PROXY.get(sym)
        if proxy:
            return QuantDataTarget(sym, proxy, False)
        return QuantDataTarget(sym, sym, True)

    async def start(self, symbol: str) -> None:
        self.settings = load_settings()
        self._status["configured"] = self.settings.configured
        self._symbol = str(symbol or "DIA").upper()
        if not self.settings.configured:
            return
        if self.client is None:
            self.client = QuantDataClient(self.settings)
            await self.client.start()
        self._stop = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="itmq-quantdata-options-intelligence")
        self._wake.set()

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                _obs_expected("quantdata.stop.cancelled")
            except Exception as exc:
                _obs_note("quantdata:stop", exc)
            self._task = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        self._status["running"] = False

    async def select_asset(self, symbol: str) -> None:
        self._symbol = str(symbol or "DIA").upper().strip()
        if self.settings.configured:
            self._wake.set()

    def status(self) -> dict[str, Any]:
        # Never expose the key.
        return dict(self._status)

    def latest(self) -> dict[str, Any]:
        return dict(self._last_payload)

    async def _loop(self) -> None:
        self._status["running"] = True
        while not self._stop:
            started = time.monotonic()
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status["last_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                _obs_note("quantdata:refresh", exc, severity="DEGRADED")
            elapsed = time.monotonic() - started
            # El ritmo lo dicta la cuota real del plan, no una constante: con un
            # plan pequeño se espacia solo en lugar de agotarlo y quedarse mudo.
            interval = max(self.settings.refresh_seconds,
                           QUOTA.recommended_interval(ENGINE_FAST_REQUESTS))
            delay = max(1.0, interval - elapsed)
            remaining = self._status.get("remaining")
            reset = self._status.get("reset_seconds")
            if isinstance(remaining, int) and remaining < 18 and isinstance(reset, (int, float)):
                delay = max(delay, float(reset) + 1.0)
            last_error = str(self._status.get("last_error") or "")
            if "authorization failed" in last_error.lower():
                delay = max(delay, 120.0)
            elif last_error and not self._status.get("last_success"):
                delay = max(delay, 30.0)
            try:
                self._wake.clear()
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                _obs_expected("quantdata.loop.clock_timeout")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        assert self.client is not None
        response = await self.client.post(path, body)
        self._status["remaining"] = response.remaining
        self._status["limit"] = response.limit
        self._status["reset_seconds"] = response.reset_seconds
        # v1.57.3 · La cuota ya la anota `QuantDataClient.post`, que es por donde
        # pasan LOS DOS carriles. Repetirlo aquí contaba dos veces cada petición
        # del motor y dejaba el presupuesto de páginas más apretado de lo real.
        return response.payload

    async def refresh_once(self) -> dict[str, Any]:
        if not self.settings.configured:
            return {"ready": False, "reason": "NOT_CONFIGURED"}
        async with self._lock:
            if self.client is None:
                self.client = QuantDataClient(self.settings)
                await self.client.start()
            target = self._target(self._symbol)
            ticker = target.query_ticker
            base_filter = {"filter": {"ticker": ticker}}

            # Los endpoints se escalonan por velocidad de cambio real. Pedir los
            # nueve cada ciclo consumía 2160 peticiones/hora contra un plan que
            # puede ser de 240: la cuota se agotaba en minutos y el proveedor
            # quedaba DEGRADED con todo envejeciendo en pantalla.
            requests = {
                "gamma": ("/v1/options/tool/exposure-by-strike", {"greekMode": "GAMMA", "representationMode": "PER_ONE_PERCENT_MOVE", **base_filter}),
                "delta": ("/v1/options/tool/exposure-by-strike", {"greekMode": "DELTA", "representationMode": "PER_ONE_DOLLAR_MOVE", **base_filter}),
                "net_drift": ("/v1/options/tool/net-drift", {"aggregationPeriod": "1m", **base_filter}),
                "net_flow": ("/v1/options/tool/net-flow", {"dataMode": "NET_PREMIUM", "aggregationPeriod": "1m", **base_filter}),
                "vanna": ("/v1/options/tool/exposure-by-strike", {"greekMode": "VANNA", "representationMode": "RAW", **base_filter}),
                "charm": ("/v1/options/tool/exposure-by-strike", {"greekMode": "CHARM", "representationMode": "RAW", **base_filter}),
                "interval_gamma": ("/v1/options/tool/interval-map", {"greekMode": "GAMMA", "aggregationPeriod": "5m", **base_filter}),
                "max_pain": ("/v1/options/tool/max-pain-over-time", base_filter),
                "iv_rank": ("/v1/options/tool/iv-rank", {"filter": {"ticker": ticker}, "lookBackPeriod": self.settings.iv_lookback_days, "maturity": self.settings.iv_maturity_days}),
            }
            slow_due = (self._cycle % max(1, ENGINE_SLOW_EVERY_N_CYCLES)) == 0
            due_names = list(ENGINE_FAST_JOBS) + (list(ENGINE_SLOW_JOBS) if slow_due else [])
            self._cycle += 1

            # v1.44.0 · Cada endpoint es un CANAL AISLADO.
            #
            # Antes esto era un `gather` desnudo: el ciclo terminaba cuando
            # terminaba el más lento, así que un `dark-flow` de nueve segundos
            # retrasaba nueve segundos la estructura que sostiene TRACE. Ahora cada
            # canal lleva su propio timeout y su propio cortocircuito, la respuesta
            # que llega tarde alimenta el Last Known Good, y las peticiones
            # duplicadas entre carriles se funden en una.
            async def _channel(name: str):
                path, payload_body = requests[name]
                gate = await HUB_RUNTIME.fetch(
                    name, target.active_symbol,
                    lambda: self._post(path, payload_body),
                    timeout_s=self.settings.request_timeout_seconds + 1.0)
                if gate.get("ready") and isinstance(gate.get("payload"), dict):
                    return gate["payload"]
                raise RuntimeError(str(gate.get("detail") or "canal no disponible"))

            jobs = {n: _channel(n) for n in due_names if n in requests}
            names = list(jobs)
            results = await asyncio.gather(*jobs.values(), return_exceptions=True)
            payloads: dict[str, dict[str, Any]] = {}
            errors: dict[str, str] = {}
            for name, result in zip(names, results):
                if isinstance(result, Exception):
                    errors[name] = f"{type(result).__name__}: {str(result)[:160]}"
                elif isinstance(result, dict):
                    payloads[name] = result

            # Los bloques estructurales que no tocaban en este ciclo se reutilizan
            # del último payload válido: son datos lentos, no huecos.
            for name in requests:
                if name in payloads:
                    continue
                cached = RAW_CACHE.get(name, target.active_symbol, max_age_s=3600.0)
                if cached is not None:
                    payloads[name] = cached
                    errors.pop(name, None)

            # Se comparten los payloads crudos para que el carril de páginas no
            # vuelva a pedir los mismos nueve endpoints. Es la mitad del consumo
            # de cuota del release anterior.
            for _name, _payload in payloads.items():
                RAW_CACHE.put(_name, target.active_symbol, _payload)

            gamma = _exposure_summary(payloads.get("gamma", {}), ticker)
            delta = _exposure_summary(payloads.get("delta", {}), ticker)
            vanna = _exposure_summary(payloads.get("vanna", {}), ticker)
            charm = _exposure_summary(payloads.get("charm", {}), ticker)
            drift = _bucket_flow(payloads.get("net_drift", {}), signed_put=True)
            drift_series = _net_drift_series(payloads.get("net_drift", {}))
            net_flow = _bucket_flow(payloads.get("net_flow", {}), signed_put=False)
            migration = _interval_gamma_migration(payloads.get("interval_gamma", {}))
            iv_rank = _iv_rank_summary(payloads.get("iv_rank", {}))
            max_pain_data = (payloads.get("max_pain", {}) or {}).get("data") or {}

            flow_sign = drift.get("sign") or net_flow.get("sign") or 0
            flow_conf = max(float(drift.get("confidence") or 0), float(net_flow.get("confidence") or 0))
            components = [
                (int(delta.get("sign") or 0), float(delta.get("confidence") or 0), 0.34),
                (int(flow_sign), flow_conf, 0.26),
                (int(gamma.get("sign") or 0), float(gamma.get("confidence") or 0), 0.16),
                (int(vanna.get("sign") or 0), float(vanna.get("confidence") or 0), 0.12),
                (int(charm.get("sign") or 0), float(charm.get("confidence") or 0), 0.12),
            ]
            weighted = sum(s * c * w for s, c, w in components if s)
            gross_w = sum(c * w for s, c, w in components if s)
            composite_sign = 1 if weighted > 4 else -1 if weighted < -4 else 0
            composite_conf = min(100.0, abs(weighted) / max(sum(w for _, _, w in components), 1e-9)) if gross_w > 0 else 0.0

            directional: dict[str, Any]
            if target.direct:
                directional = {
                    "gamma": {"sign": gamma.get("sign", 0), "confidence": gamma.get("confidence", 0)},
                    "delta": {"sign": delta.get("sign", 0), "confidence": delta.get("confidence", 0)},
                    "vanna": {"sign": vanna.get("sign", 0), "confidence": vanna.get("confidence", 0)},
                    "charm": {"sign": charm.get("sign", 0), "confidence": charm.get("confidence", 0)},
                    "flow": {"sign": flow_sign, "confidence": flow_conf},
                    "derivatives": {"sign": composite_sign, "confidence": round(composite_conf, 2)},
                }
            else:
                # Context only for YM/MYM/DJX/VIX/VXD. Never feeds direct Gamma/Delta
                # channels because DIA strikes/exposures are not the same instrument.
                directional = {
                    "ecosystem": {"sign": composite_sign, "confidence": round(min(72.0, composite_conf), 2)},
                    "derivatives": {"sign": composite_sign, "confidence": round(min(60.0, composite_conf), 2)},
                }

            now = datetime.now(timezone.utc).isoformat()
            values = {
                "event_time_valid": True,
                "event_time_source": "QUANTDATA_REST_SNAPSHOT",
                "active_symbol": target.active_symbol,
                "query_ticker": ticker,
                "direct_instrument_evidence": target.direct,
                "directional": directional,
                "gamma": gamma,
                "delta": delta,
                "vanna": vanna,
                "charm": charm,
                "net_drift": drift,
                "net_drift_series": drift_series,
                "net_flow": net_flow,
                "gamma_migration": migration,
                "iv_rank": iv_rank,
                "max_pain_by_expiration": max_pain_data if isinstance(max_pain_data, dict) else {},
                "errors": errors,
            }
            available = sum(1 for x in (gamma, delta, vanna, charm, drift, net_flow, migration, iv_rank) if x.get("available"))
            evidence_conf = min(92.0, 35.0 + available * 7.0)
            FEATURE_BUS.ingest(
                source="QUANTDATA",
                symbol=target.active_symbol,
                feature_group="OPTIONS_INTELLIGENCE",
                values=values,
                timestamp=now,
                confidence=evidence_conf,
                ttl_ms=max(25000.0, self.settings.refresh_seconds * 3000.0),
            )
            self._last_payload = {"ready": available > 0, "symbol": target.active_symbol, "ticker": ticker, **values}
            self._status.update({
                "configured": True,
                "running": True,
                "last_success": now if available > 0 else self._status.get("last_success"),
                "last_error": None if available > 0 else ("; ".join(errors.values())[:220] if errors else "NO_DATA"),
                "query_ticker": ticker,
                "available_components": available,
                "direct": target.direct,
            })
            return self.latest()


QUANTDATA = QuantDataRuntime()
