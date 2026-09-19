"""Catálogo de herramientas Quant Data · ITM QUANT v1.41.0

Cubre las páginas integradas del proveedor (Dashboard, Exposure, Flow Analysis,
Dark Pool/Equities, Statistics, Open Interest y Volatility Analysis) y normaliza
cada respuesta a una forma estable que la terminal consume sin conocer el formato
original del proveedor.

Registro canónico de rutas (v1.42.1)
------------------------------------
Cada herramienta declara UNA ruta oficial. Antes se declaraban varias candidatas y,
si todas fallaban, `path_variants()` derivaba hasta doce más (camelCase, snake_case,
sin el segmento `/options`, colgando de `/v1/tool`...). La intención era buena
—adaptarse si el proveedor renombraba algo— pero el resultado en producción fue
peor que el problema:

    dark-pool-levels → HTTP 404 en '/v1/tool/dark-pool-levels' · 6/6 rutas probadas

Esa ruta no existe en ninguna versión del proveedor: la inventó el derivador. El
diagnóstico mostraba el último intento, no el primero, así que el operador leía una
URL falsa y concluía que la herramienta no existía. Y cada herramienta ausente
gastaba seis peticiones de cuota por ciclo en URLs imaginarias.

Ahora hay una ruta y sólo una. Si devuelve 404, el estado es `ROUTE_INVALID` con la
ruta canónica nombrada, que es accionable: o el plan no la incluye, o el proveedor
la cambió y hay que actualizarla aquí. No se adivina.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List
import math
import time

# Tiempo de espera antes de volver a probar una herramienta que el proveedor
# rechazó. Evita castigar la cuota con reintentos de algo que no existe.
UNAVAILABLE_COOLDOWN_S = 1800.0
TRANSIENT_BACKOFF_BASE_S = 30.0
TRANSIENT_BACKOFF_MAX_S = 300.0

# Estado de la ruta canónica de una herramienta. `ROUTE_INVALID` es el que importa:
# dice que la URL oficial existe en el código pero el proveedor la rechaza, lo que
# sólo puede significar dos cosas —el plan no la incluye, o el proveedor la
# renombró—. Las dos son accionables. «No disponible» a secas no lo era.
ROUTE_UNKNOWN = "ROUTE_UNKNOWN"
ROUTE_OK = "ROUTE_OK"
ROUTE_INVALID = "ROUTE_INVALID"

# Cadencias por familia. El flujo cambia segundo a segundo; el interés abierto
# es estructural y se publica al cierre: pedirlo cada 15s sería quemar cuota.
CADENCE = {
    "FAST": 15.0,      # flujo, exposición, order flow
    "MEDIUM": 60.0,    # estadísticas, dark pool, prints
    "SLOW": 300.0,     # interés abierto, volatilidad, term structure
    "DAILY": 1800.0,   # news, gainers/losers
}


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _rows(payload: Any, *keys: str) -> List[Any]:
    """Extrae la lista de datos de una respuesta sin asumir un único envoltorio."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for k in keys:
        v = payload.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            inner = _rows(v, *keys)
            if inner:
                return inner
    for k in ("data", "results", "items", "rows", "values", "series", "points"):
        v = payload.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            # Se vuelve a buscar con los nombres específicos: un proveedor puede
            # anidar la lista real bajo su propio envoltorio ({"results":{"strikes":[…]}}).
            inner = _rows(v, *keys)
            if inner:
                return inner
    return []


def _ts_iso(value: Any) -> str | None:
    """Instante en ISO-8601 UTC, venga como época en ms, en s, o ya como texto.

    La clave de los mapas de Quant Data es una época en milisegundos
    (`"1758205800000"`). Publicarla tal cual obliga a cada consumidor a adivinar la
    unidad, y `datetime.fromisoformat("1758205800000")` no falla de forma ruidosa:
    descarta la fila. Un único punto de conversión evita que la ambigüedad viaje.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        v = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return v.isoformat()
    raw = str(value).strip()
    if not raw:
        return None
    digits = raw[1:] if raw[:1] == "-" else raw
    if digits.isdigit():
        try:
            n = float(raw)
        except ValueError:
            return raw
        # >1e11 sólo puede ser milisegundos: 1e11 s son el año 5138.
        seconds = n / 1000.0 if abs(n) > 1e11 else n
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return raw
    return raw


def _epoch_ms(value: Any) -> int | None:
    """Época en milisegundos cuando la clave del proveedor ya lo es; None si no."""
    raw = str(value or "").strip()
    digits = raw[1:] if raw[:1] == "-" else raw
    if not digits.isdigit():
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if abs(n) > 1e11 else n * 1000


def _keyed_rows(payload: Any, key_as: str) -> List[Dict[str, Any]]:
    """Filas de una respuesta publicada como MAPA {clave: fila}, no como lista.

    Quant Data no entrega sus series como `[{...}, {...}]`. Entrega un objeto cuya
    CLAVE es la propia coordenada —el instante en milisegundos para las series
    temporales, el strike para los perfiles— y cuyo valor es la fila:

        {"data": {"1758205800000": {"netCallPremium": 1.2e6, ...}, ...}}

    `_rows` recorre listas; ante este objeto devolvía `[]`, y una lista vacía es
    indistinguible de "el proveedor no tiene datos". Por eso Net Drift, Net Flow y
    con ellos QFLOW publicaban SIN DATOS aunque la respuesta viniera completa: el
    fallo no estaba en el proveedor ni en la red, estaba en el lector.

    La clave se reinyecta como `key_as` para que el normalizador la lea igual que
    leería un campo cualquiera de la fila. Si la fila ya trae ese campo, gana la
    fila: la clave es un índice, no un dato mejor que el propio dato.
    """
    node = payload
    if isinstance(node, dict) and isinstance(node.get("data"), dict):
        node = node["data"]
    if not isinstance(node, dict):
        return []
    out: List[Dict[str, Any]] = []
    for k, row in node.items():
        if isinstance(row, dict):
            out.append({key_as: k, **row})
    return out


def _pick(row: Any, *names: str) -> Any:
    if not isinstance(row, dict):
        return None
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    lowered = {str(k).lower().replace("_", ""): v for k, v in row.items()}
    for n in names:
        key = n.lower().replace("_", "")
        if key in lowered and lowered[key] is not None:
            return lowered[key]
    return None


def _tf(ticker: str) -> Dict[str, Any]:
    """Filtro canónico de ticker para las herramientas Quant Data.

    v1.42.1 hotfix: el catálogo quedó refactorizado para reutilizar este cuerpo,
    pero la función se perdió durante la consolidación de rutas. Todas las
    herramientas que la referenciaban fallaban al construir el request con
    ``NameError: _tf is not defined`` pese a que la conexión del proveedor estaba
    viva. Centralizarlo aquí vuelve imposible que cada herramienta invente una
    forma distinta del filtro.
    """
    symbol = str(ticker or "").strip().upper()
    return {"filter": {"ticker": symbol}}


# ---------------------------------------------------------------- normalizadores

def norm_by_strike(payload: Dict[str, Any], value_names: tuple[str, ...]) -> Dict[str, Any]:
    """[{strike, value, call, put}] ordenado por strike."""
    out = []
    for r in _rows(payload, "strikes", "byStrike", "exposure", "openInterest"):
        k = _f(_pick(r, "strike", "strikePrice", "k"))
        if k is None:
            continue
        v = _f(_pick(r, *value_names), 0.0) or 0.0
        out.append({
            "strike": k,
            "value": v,
            "call": _f(_pick(r, "call", "callValue", "calls", "callOpenInterest"), None),
            "put": _f(_pick(r, "put", "putValue", "puts", "putOpenInterest"), None),
        })
    out.sort(key=lambda x: x["strike"])
    return {"ready": bool(out), "rows": out, "count": len(out)}


def norm_by_expiration(payload: Dict[str, Any], value_names: tuple[str, ...]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "expirations", "byExpiration", "exposure"):
        e = _pick(r, "expiration", "expirationDate", "expiry", "date")
        if e is None:
            continue
        out.append({
            "expiration": str(e),
            "value": _f(_pick(r, *value_names), 0.0) or 0.0,
            "call": _f(_pick(r, "call", "callValue", "calls"), None),
            "put": _f(_pick(r, "put", "putValue", "puts"), None),
        })
    out.sort(key=lambda x: x["expiration"])
    return {"ready": bool(out), "rows": out, "count": len(out)}


def norm_time_series(payload: Dict[str, Any], value_names: tuple[str, ...]) -> Dict[str, Any]:
    out = []
    # v1.42.7 · La forma real del proveedor es un mapa {instante_ms: fila}. Se
    # intenta primero la lista (formato documentado y el que usan las pruebas) y se
    # cae al mapa sólo si no hay lista, de modo que ninguna respuesta que hoy
    # funciona cambia de comportamiento.
    rows = _rows(payload, "buckets", "series", "points", "timeline") or _keyed_rows(payload, "timestamp")
    for r in rows:
        t = _ts_iso(_pick(r, "timestamp", "time", "t", "bucket", "date"))
        if t is None:
            continue
        # v1.42.6 · Quant Data publica cada bucket de net-flow como `callSum`,
        # `putSum` y `stockPrice`. El normalizador sólo buscaba `netCallPremium` /
        # `netPutPremium`, así que call/put llegaban en None, y `stockPrice` se
        # descartaba entero — que es justo el precio de referencia de cada intervalo
        # y sin él no hay forma de situar el flujo sobre el eje de precio.
        call = _f(_pick(r, "callSum", "netCallPremium", "callPremium", "call", "calls"), None)
        put = _f(_pick(r, "putSum", "netPutPremium", "putPremium", "put", "puts"), None)
        value = _pick(r, *value_names)
        if value is None and (call is not None or put is not None):
            # Sin campo neto explícito, el neto ES call - put. Devolver 0.0 aquí
            # convertía una respuesta válida en una serie plana.
            value = (call or 0.0) - (put or 0.0)
        out.append({
            "t": t,
            "value": _f(value, 0.0) or 0.0,
            "call": call,
            "put": put,
            "stock_price": _f(_pick(r, "stockPrice", "underlyingPrice", "spot", "price"), None),
        })
    # El mapa del proveedor no garantiza orden cronológico y una serie temporal sin
    # ordenar produce curvas acumuladas falsas aguas abajo.
    out.sort(key=lambda x: x["t"])
    return {"ready": bool(out), "rows": out, "count": len(out)}


def _exposure_map(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """`data[TICKER].exposureMap[vencimiento][strike] = {callExposure, putExposure}`.

    Ésta es la forma REAL de `exposure-by-strike` y `exposure-by-expiration`. No es
    una lista de filas, así que `norm_by_strike` devolvía `[]` para toda respuesta
    válida del proveedor y la página Exposure no alimentaba a EXPOSICIÓN: la sección
    vivía sólo del motor propio y la cobertura del proveedor se leía como "sin datos"
    cuando en realidad era "sin leer".
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    # La clave superior es el ticker consultado. Se toma el primer nodo con
    # `exposureMap` en vez de exigir el nombre exacto: así no hay que conocer aquí
    # cómo escribió el proveedor el símbolo, ni hay ningún ticker fijado en código.
    for node in data.values():
        if isinstance(node, dict) and isinstance(node.get("exposureMap"), dict):
            return node["exposureMap"]
    return {}


def norm_exposure_by_strike(payload: Dict[str, Any], value_names: tuple[str, ...]) -> Dict[str, Any]:
    """Exposición por strike, sumada sobre todos los vencimientos."""
    rows = _rows(payload, "strikes", "byStrike", "exposure", "openInterest")
    if rows:
        return norm_by_strike(payload, value_names)
    agg: Dict[float, Dict[str, float]] = {}
    for _expiry, strikes in _exposure_map(payload).items():
        if not isinstance(strikes, dict):
            continue
        for strike_raw, cell in strikes.items():
            k = _f(strike_raw)
            if k is None or not isinstance(cell, dict):
                continue
            slot = agg.setdefault(k, {"call": 0.0, "put": 0.0})
            slot["call"] += _f(cell.get("callExposure"), 0.0) or 0.0
            slot["put"] += _f(cell.get("putExposure"), 0.0) or 0.0
    out = [{"strike": k, "call": v["call"], "put": v["put"],
            # callExposure y putExposure llegan YA firmadas. El neto es la suma,
            # no la resta: restar una magnitud negativa duplicaría el signo.
            "value": v["call"] + v["put"]}
           for k, v in sorted(agg.items())]
    return {"ready": bool(out), "rows": out, "count": len(out),
            "source": "QUANTDATA_EXPOSURE_MAP"}


def norm_exposure_by_expiration(payload: Dict[str, Any], value_names: tuple[str, ...]) -> Dict[str, Any]:
    """Exposición por vencimiento, sumada sobre todos los strikes."""
    rows = _rows(payload, "expirations", "byExpiration", "exposure")
    if rows:
        return norm_by_expiration(payload, value_names)
    out = []
    for expiry, strikes in _exposure_map(payload).items():
        if not isinstance(strikes, dict):
            continue
        call = put = 0.0
        for cell in strikes.values():
            if not isinstance(cell, dict):
                continue
            call += _f(cell.get("callExposure"), 0.0) or 0.0
            put += _f(cell.get("putExposure"), 0.0) or 0.0
        out.append({"expiration": str(expiry), "call": call, "put": put, "value": call + put})
    out.sort(key=lambda x: x["expiration"])
    return {"ready": bool(out), "rows": out, "count": len(out),
            "source": "QUANTDATA_EXPOSURE_MAP"}


def norm_net_drift(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Net Drift OFICIAL de Quant Data, sin reconstruir nada.

    `POST /v1/options/tool/net-drift` es la única autoridad de este dato. Aquí no se
    deriva de GEX, ni de DEX, ni de Net Flow, ni de una fórmula propia: se lee lo que
    el proveedor publica por intervalo y se conserva con su signo.

    El normalizador compartido `norm_time_series` no sirve para esta herramienta
    porque colapsa la fila a `value`/`call`/`put` y tira cuatro de los siete campos
    oficiales —los dos volúmenes netos y las dos primas a precio medio—. Sin los
    volúmenes no hay subgráfico de volumen neto; sin las primas a precio medio no
    hay forma de contrastar la prima pagada contra el punto medio del mercado.

    Signo: `netPutPremium` llega YA firmado por el proveedor (negativo cuando el
    dinero se va a puts). No se le aplica ningún signo extra. Por eso la prima neta
    de un intervalo es `call + put`, no `call - put`; invertirlo duplicaría la
    dirección y la curva acumulada de la sesión saldría al revés.

    Aquí NO se acumula. La acumulación es de `app/core/net_drift.py`, que es quien
    conoce el instante de referencia y el bucket todavía abierto. Mezclar las dos
    cosas haría imposible comparar la salida contra la respuesta cruda.
    """
    rows = _rows(payload, "buckets", "series", "points", "timeline") or _keyed_rows(payload, "timestamp")
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        raw_t = _pick(r, "timestamp", "time", "t", "bucket", "date")
        t = _ts_iso(raw_t)
        if t is None:
            continue
        price = _f(_pick(r, "stockPrice", "underlyingPrice", "spot", "price"), None)
        if price is not None and price <= 0:
            price = None
        call = _f(_pick(r, "netCallPremium", "callPremium", "callSum"), 0.0) or 0.0
        put = _f(_pick(r, "netPutPremium", "putPremium", "putSum"), 0.0) or 0.0
        out.append({
            "t": t,
            "timestamp_ms": _epoch_ms(raw_t),
            "net_call_premium": call,
            "net_put_premium": put,
            "net_premium": call + put,
            "net_call_volume": _f(_pick(r, "netCallVolume", "callVolume"), 0.0) or 0.0,
            "net_put_volume": _f(_pick(r, "netPutVolume", "putVolume"), 0.0) or 0.0,
            "mid_call_premium": _f(_pick(r, "midMarketCallPremium", "midCallPremium"), None),
            "mid_put_premium": _f(_pick(r, "midMarketPutPremium", "midPutPremium"), None),
            "stock_price": price,
        })
    out.sort(key=lambda x: x["t"])
    return {"ready": bool(out), "rows": out, "count": len(out),
            "source": "QUANTDATA_NET_DRIFT_OFFICIAL", "aggregation": "1m"}


def norm_interval_map(payload: Dict[str, Any], greek: str = "GAMMA") -> Dict[str, Any]:
    """INTERVAL MAP de Quant Data: exposición por INSTANTE y por STRIKE.

    Ésta es la fuente del mapa de calor dinámico de TRACE. No es un heat map
    estático de fondo: cada columna es un intervalo real de la sesión, así que se
    ve cómo la exposición APARECE, CRECE, SE REDUCE y MIGRA entre strikes a lo
    largo del día. Un perfil por strike sólo sabe decir dónde está la exposición
    AHORA; el interval map dice de dónde viene.

    Forma real del proveedor (la misma que ya leía el carril del motor para
    detectar migración de gamma):

        {"data": {"<instante_ms>": {"<vencimiento>": {"<strike>": {"CALL": x, "PUT": y}}}}}

    La clave superior es la época en milisegundos; se convierte a ISO-8601 UTC en
    un único punto para que ningún consumidor tenga que adivinar la unidad. Los
    vencimientos se SUMAN dentro de cada instante: el mapa es del subyacente, no
    de un vencimiento concreto, y separarlos aquí obligaría a la interfaz a
    reagregarlos.

    Se publican tres matrices —neta, call y put— porque la pregunta «¿el muro es
    de calls o de puts?» no se puede responder desde el neto, y responderla es
    justo lo que distingue un techo de un suelo.

    Matriz orientada [strike][instante]: la fila es el eje Y del gráfico (precio)
    y la columna es el eje X (tiempo), que es como se dibuja.
    """
    key = str(greek or "GAMMA").upper()
    data = payload.get("data") if isinstance(payload, dict) else None

    cells: Dict[float, Dict[str, Dict[str, float]]] = {}
    times_ms: Dict[str, int] = {}

    def _add(strike: Any, t_iso: str, call: float, put: float) -> None:
        k = _f(strike)
        if k is None:
            return
        slot = cells.setdefault(k, {}).setdefault(t_iso, {"call": 0.0, "put": 0.0})
        slot["call"] += call
        slot["put"] += put

    if isinstance(data, dict) and data:
        for raw_t, bucket in data.items():
            t_iso = _ts_iso(raw_t)
            if t_iso is None or not isinstance(bucket, dict):
                continue
            ms = _epoch_ms(raw_t)
            if ms is not None:
                times_ms[t_iso] = ms
            for _expiry, strikes in bucket.items():
                if not isinstance(strikes, dict):
                    continue
                for strike_raw, cell in strikes.items():
                    if not isinstance(cell, dict):
                        continue
                    call = _f(_pick(cell, "CALL", "call", "callExposure", "calls"), 0.0) or 0.0
                    put = _f(_pick(cell, "PUT", "put", "putExposure", "puts"), 0.0) or 0.0
                    if call == 0.0 and put == 0.0:
                        # Algunas cuentas publican un único valor ya neto en vez del
                        # desglose. Se conserva como neto y el desglose queda vacío,
                        # que es honesto: no se inventa un reparto call/put.
                        net = _f(_pick(cell, "value", "exposure", "gamma", "delta",
                                       "vanna", "charm", "net"), None)
                        if net is None:
                            continue
                        _add(strike_raw, t_iso, float(net), 0.0)
                        continue
                    _add(strike_raw, t_iso, call, put)

    if not cells:
        # El envoltorio del proveedor cambia entre cuentas y versiones. La forma
        # canónica es el mapa {instante: {vencimiento: {strike: {CALL, PUT}}}}, pero
        # hay cuentas que publican ejes paralelos con la matriz aparte, y otras una
        # lista plana de celdas o una serie colgando de cada strike. Reconocer las
        # tres formas evita que una herramienta viva se lea como NO DISPONIBLE con
        # el dato delante; inventarse una ruta única fue lo que dejó el mapa vacío.
        alt = _interval_alt_shapes(payload)
        if alt is not None:
            return {**alt, "greek": key, "source": "QUANTDATA_INTERVAL_MAP"}
        return {"ready": False, "greek": key, "strikes": [], "times": [], "matrix": [],
                "call_matrix": [], "put_matrix": [], "cells": 0,
                "reason": "PAYLOAD DE INTERVAL MAP SIN CELDAS RECONOCIBLES",
                "payload_keys": _payload_shape(payload),
                "source": "QUANTDATA_INTERVAL_MAP"}

    strikes = sorted(cells)
    times = sorted({t for row in cells.values() for t in row})

    def _grid(field: str) -> List[List[float]]:
        return [[round(float(cells.get(k, {}).get(t, {}).get(field, 0.0)), 6) for t in times]
                for k in strikes]

    call_grid = _grid("call")
    put_grid = _grid("put")
    # callExposure y putExposure llegan YA firmadas: el neto es la SUMA. Restar una
    # magnitud que ya es negativa duplicaría el signo y el mapa saldría invertido.
    net_grid = [[round(c + p, 6) for c, p in zip(cr, pr)] for cr, pr in zip(call_grid, put_grid)]

    return {
        "ready": True, "greek": key, "strikes": strikes, "times": times,
        "times_ms": [times_ms.get(t) for t in times],
        "matrix": net_grid, "call_matrix": call_grid, "put_matrix": put_grid,
        "cells": sum(len(r) for r in cells.values()),
        "rows": len(strikes), "columns": len(times),
        "count": len(strikes) * len(times),
        "strike_low": strikes[0], "strike_high": strikes[-1],
        "orientation": "MATRIX_STRIKE_BY_TIME",
        "expiration_aggregation": "SUM_ACROSS_EXPIRATIONS",
        "source": "QUANTDATA_INTERVAL_MAP",
    }



def _interval_alt_shapes(raw: Any) -> Dict[str, Any] | None:
    """Ejes paralelos, celdas sueltas o series por strike."""
    axes = _interval_axes(raw)
    if axes:
        return {**axes, "ready": True, "call_matrix": [], "put_matrix": [],
                "times_ms": [], "orientation": "MATRIX_STRIKE_BY_TIME"}
    flat: Dict[float, Dict[str, float]] = {}
    _interval_collect(raw, flat)
    if not flat:
        return None
    strikes = sorted(flat)
    times = sorted({t for row in flat.values() for t in row})
    return {
        "ready": True, "strikes": strikes, "times": times, "times_ms": [],
        "matrix": [[flat.get(k, {}).get(t, 0.0) for t in times] for k in strikes],
        "call_matrix": [], "put_matrix": [],
        "cells": sum(len(r) for r in flat.values()),
        "rows": len(strikes), "columns": len(times),
        "count": len(strikes) * len(times),
        "strike_low": strikes[0], "strike_high": strikes[-1],
        "orientation": "MATRIX_STRIKE_BY_TIME",
    }


def _interval_axes(raw: Any, depth: int = 0) -> Dict[str, Any] | None:
    """Payload con `strikes`, `times` y una matriz en paralelo."""
    if depth > 5 or not isinstance(raw, dict):
        if isinstance(raw, list) and depth <= 5:
            for item in raw[:12]:
                hit = _interval_axes(item, depth + 1)
                if hit:
                    return hit
        return None
    ks = next((raw[k] for k in ("strikes", "strikePrices", "y", "rows") if isinstance(raw.get(k), list)), None)
    ts = next((raw[k] for k in ("times", "timestamps", "intervals", "x", "columns") if isinstance(raw.get(k), list)), None)
    mx = next((raw[k] for k in ("matrix", "values", "data", "z", "grid") if isinstance(raw.get(k), list)), None)
    if ks and ts and mx and isinstance(mx[0], list):
        strikes = [_f(k) for k in ks if _f(k) is not None]
        times = [str(t) for t in ts if t is not None]
        matrix = []
        for row in mx[:len(strikes)]:
            vals = [_f(v, 0.0) or 0.0 for v in (row if isinstance(row, list) else [])][:len(times)]
            vals += [0.0] * (len(times) - len(vals))
            matrix.append(vals)
        if strikes and times and matrix:
            return {"strikes": strikes, "times": times, "matrix": matrix,
                    "cells": len(strikes) * len(times),
                    "rows": len(strikes), "columns": len(times),
                    "count": len(strikes) * len(times),
                    "strike_low": min(strikes), "strike_high": max(strikes)}
    for child in raw.values():
        if isinstance(child, (dict, list)):
            hit = _interval_axes(child, depth + 1)
            if hit:
                return hit
    return None


def _interval_collect(node: Any, cells: Dict[float, Dict[str, float]],
                      strike: Any = None, depth: int = 0) -> None:
    """Celdas sueltas, con los nombres que el proveedor usa en cada variante."""
    if depth > 7 or node is None:
        return
    if isinstance(node, list):
        for item in node[:6000]:
            _interval_collect(item, cells, strike, depth + 1)
        return
    if not isinstance(node, dict):
        return
    k = _f(_pick(node, "strike", "strikePrice", "strike_price", "y", "price"))
    if k is None:
        k = _f(strike)
    t = _pick(node, "time", "timestamp", "t", "interval", "x", "bucket", "date")
    v = _f(_pick(node, "gamma", "value", "gex", "netGamma", "exposure", "z", "v",
                 "delta", "charm", "vanna"))
    if k is not None and t is not None and v is not None:
        cells.setdefault(k, {})[str(t)] = v
        return
    for child in node.values():
        if isinstance(child, (list, dict)):
            _interval_collect(child, cells, k if k is not None else strike, depth + 1)


def _payload_shape(raw: Any, depth: int = 0) -> Any:
    """Esqueleto del payload: claves y tipos, sin volcar los datos.

    Es lo que convierte un «no se entiende la respuesta» en algo accionable: se ve
    qué publicó esta cuenta y se corrige el normalizador sin tener que adivinarlo.
    """
    if depth > 3:
        return "…"
    if isinstance(raw, dict):
        return {k: _payload_shape(v, depth + 1) for k, v in list(raw.items())[:12]}
    if isinstance(raw, list):
        return [f"lista[{len(raw)}]", _payload_shape(raw[0], depth + 1)] if raw else "lista[0]"
    return type(raw).__name__


def norm_option_order_flow(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Order Flow de OPCIONES de Quant Data, con el contrato entero.

    `norm_prints` es genérico —lo comparte con la cinta de equity— y por eso se queda
    en precio, tamaño y venue. Una impresión de opciones sin strike, sin vencimiento,
    sin tipo y sin prima no se puede relacionar con nada: es una fila anónima. Y
    relacionar un movimiento de la curva con los trades que lo produjeron es
    exactamente lo que hay que poder hacer al seleccionar un punto de Net Drift.

    Se conserva además el tipo de ejecución (`BLOCK`, `SWEEP`, `SPLIT`…), que es lo
    que distingue una orden negociada de una barrida agresiva por varias bolsas.
    """
    rows = _rows(payload, "prints", "trades", "executions", "orderFlow") or _keyed_rows(payload, "timestamp")
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _ts_iso(_pick(r, "timestamp", "time", "t", "executedAt", "executionTime"))
        price = _f(_pick(r, "price", "tradePrice", "executionPrice", "optionPrice"))
        if t is None or price is None:
            continue
        size = _f(_pick(r, "size", "quantity", "volume", "contracts"), 0.0) or 0.0
        premium = _f(_pick(r, "premium", "notional", "value", "totalPremium"), None)
        if premium is None:
            # La prima de un contrato es precio × tamaño × multiplicador. Publicar
            # precio × tamaño a secas la dejaría cien veces por debajo.
            premium = price * size * 100.0
        side = str(_pick(r, "side", "aggressor", "direction", "tradeSide") or "UNKNOWN").upper()
        out.append({
            "t": t,
            "ticker": str(_pick(r, "ticker", "underlying", "symbol") or "").upper(),
            "option_type": str(_pick(r, "optionType", "type", "right", "putCall") or "").upper()[:4],
            "strike": _f(_pick(r, "strike", "strikePrice")),
            "expiration": str(_pick(r, "expiration", "expirationDate", "expiry") or ""),
            "price": price,
            "size": size,
            "premium": premium,
            "side": side,
            # +1 comprador en ask, −1 vendedor en bid, 0 sin clasificar. Es el mismo
            # convenio que usa el carril de agresión, para que no haya dos lenguajes.
            "direction": 1 if side.startswith(("BUY", "ASK", "A")) else -1 if side.startswith(("SELL", "BID", "B")) else 0,
            "execution": str(_pick(r, "executionType", "execution", "tradeType", "condition") or "").upper(),
            "spot": _f(_pick(r, "stockPrice", "underlyingPrice", "spot")),
            # v1.43.0 · Greeks POR CONTRATO tal y como los publica el proveedor.
            # Quant Data es la fuente primaria de los Greeks; recalcularlos aquí
            # cuando ya vienen en la fila sería duplicar trabajo y, peor, producir
            # dos Deltas distintos para el mismo contrato en la misma pantalla.
            # Ausente ≠ cero: lo que no venga queda en None.
            "greeks": {
                "delta": _f(_pick(r, "delta", "optionDelta")),
                "gamma": _f(_pick(r, "gamma", "optionGamma")),
                "theta": _f(_pick(r, "theta", "optionTheta")),
                "vega": _f(_pick(r, "vega", "optionVega")),
                "rho": _f(_pick(r, "rho", "optionRho")),
                "vanna": _f(_pick(r, "vanna")),
                "charm": _f(_pick(r, "charm")),
                "vomma": _f(_pick(r, "vomma")),
                "veta": _f(_pick(r, "veta")),
                "speed": _f(_pick(r, "speed")),
                "color": _f(_pick(r, "color")),
                "ultima": _f(_pick(r, "ultima")),
            },
            "implied_volatility": _f(_pick(r, "impliedVolatility", "iv")),
            "dte": _f(_pick(r, "dte", "daysToExpiration", "daysToExpiry")),
            "open_interest": _f(_pick(r, "openInterest", "oi")),
            "trades": _f(_pick(r, "trades", "tradeCount", "count", "executions")),
        })
    out.sort(key=lambda x: x["t"])
    return {"ready": bool(out), "rows": out, "count": len(out),
            "source": "QUANTDATA_OPTIONS_ORDER_FLOW"}


def norm_dark_flow(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Flujo de dark pool tal y como lo publica Quant Data.

    `POST /v1/equities/tool/dark-flow` entrega el volumen ejecutado fuera de bolsa
    por intervalo. Es una fuente DIRECTA: el proveedor ya sabe qué ejecución fue
    off-exchange. Inferirlo del campo `venue` de la cinta de otro proveedor es una
    aproximación que depende de que ese proveedor publique el venue y de que el
    código de venue se interprete bien; sirve como auditoría, no como única vía.
    """
    rows = _rows(payload, "buckets", "series", "points", "timeline", "flow") or _keyed_rows(payload, "timestamp")
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _ts_iso(_pick(r, "timestamp", "time", "t", "bucket", "date"))
        if t is None:
            continue
        price = _f(_pick(r, "stockPrice", "price", "underlyingPrice"), None)
        if price is not None and price <= 0:
            price = None
        dark = _f(_pick(r, "darkVolume", "darkPoolVolume", "offExchangeVolume", "volume"), 0.0) or 0.0
        total = _f(_pick(r, "totalVolume", "litAndDarkVolume", "consolidatedVolume"), None)
        lit = _f(_pick(r, "litVolume", "exchangeVolume"), None)
        if total is None and lit is not None:
            total = lit + dark
        out.append({
            "t": t,
            "dark_volume": dark,
            "lit_volume": lit,
            "total_volume": total,
            "dark_notional": _f(_pick(r, "darkNotional", "notional", "dollarVolume"), None),
            "dark_share_pct": (round(100.0 * dark / total, 4) if total and total > 0 else None),
            "stock_price": price,
        })
    out.sort(key=lambda x: x["t"])
    return {"ready": bool(out), "rows": out, "count": len(out), "source": "QUANTDATA_DARK_FLOW"}


def norm_prints(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "prints", "trades", "executions"):
        t = _pick(r, "timestamp", "time", "t", "executedAt")
        price = _f(_pick(r, "price", "tradePrice", "executionPrice"))
        if t is None or price is None:
            continue
        size = _f(_pick(r, "size", "shares", "quantity", "volume"), 0.0) or 0.0
        out.append({
            "t": str(t), "price": price, "size": size,
            "notional": _f(_pick(r, "notional", "value", "premium"), price * size) or (price * size),
            "side": str(_pick(r, "side", "aggressor", "direction") or "UNKNOWN").upper(),
            "venue": str(_pick(r, "venue", "exchange", "market") or ""),
            "off_exchange": bool(_pick(r, "offExchange", "darkPool", "isDarkPool") or False),
        })
    out.sort(key=lambda x: x["t"])
    return {"ready": bool(out), "rows": out, "count": len(out)}


def norm_levels(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "levels", "darkPoolLevels", "zones"):
        p = _f(_pick(r, "price", "level", "priceLevel"))
        if p is None:
            continue
        out.append({
            "price": p,
            "notional": _f(_pick(r, "notional", "value", "dollarVolume"), 0.0) or 0.0,
            "shares": _f(_pick(r, "shares", "size", "volume"), 0.0) or 0.0,
            "prints": int(_f(_pick(r, "prints", "count", "trades"), 0) or 0),
        })
    out.sort(key=lambda x: -x["notional"])
    return {"ready": bool(out), "rows": out, "count": len(out)}


def norm_stats(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "contracts", "statistics", "stats", "rows"):
        if not isinstance(r, dict):
            continue
        out.append({
            "label": str(_pick(r, "contract", "symbol", "name", "label", "expiration") or ""),
            "premium": _f(_pick(r, "premium", "notional", "totalPremium"), 0.0) or 0.0,
            "contracts": _f(_pick(r, "contracts", "volume", "size"), 0.0) or 0.0,
            "trades": int(_f(_pick(r, "trades", "count", "executions"), 0) or 0),
            "bid_side": _f(_pick(r, "bidSide", "sellPremium", "askSidePremium"), None),
            "ask_side": _f(_pick(r, "askSide", "buyPremium", "bidSidePremium"), None),
        })
    out.sort(key=lambda x: -abs(x["premium"]))
    return {"ready": bool(out), "rows": out, "count": len(out)}


def norm_curve(payload: Dict[str, Any], x_names: tuple[str, ...], y_names: tuple[str, ...]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "points", "curve", "skew", "termStructure", "strikes", "expirations"):
        x = _f(_pick(r, *x_names))
        y = _f(_pick(r, *y_names))
        if x is None or y is None:
            continue
        out.append({"x": x, "y": y, "label": str(_pick(r, "expiration", "label", "strike") or "")})
    out.sort(key=lambda p: p["x"])
    return {"ready": bool(out), "rows": out, "count": len(out)}


def norm_movers(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "gainers", "losers", "movers", "tickers"):
        s = _pick(r, "ticker", "symbol")
        if not s:
            continue
        out.append({
            "symbol": str(s).upper(),
            "change_pct": _f(_pick(r, "changePercent", "changePct", "percentChange"), 0.0) or 0.0,
            "price": _f(_pick(r, "price", "last", "close"), None),
            "volume": _f(_pick(r, "volume", "size"), None),
        })
    out.sort(key=lambda x: -abs(x["change_pct"]))
    return {"ready": bool(out), "rows": out, "count": len(out)}


def norm_news(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "news", "articles", "headlines"):
        title = _pick(r, "title", "headline", "text")
        if not title:
            continue
        out.append({
            "title": str(title)[:220],
            "t": str(_pick(r, "timestamp", "publishedAt", "time", "date") or ""),
            "source": str(_pick(r, "source", "publisher", "provider") or ""),
            "url": str(_pick(r, "url", "link") or ""),
        })
    return {"ready": bool(out), "rows": out[:40], "count": len(out)}


# ------------------------------------------------------------------- catálogo

@dataclass
class QuantDataTool:
    key: str
    page: str
    title: str
    paths: tuple[str, ...]
    body: Callable[[str], Dict[str, Any]]
    normalize: Callable[[Dict[str, Any]], Dict[str, Any]]
    cadence: str = "MEDIUM"
    # Estado de descubrimiento, mutado por el runtime.
    resolved_path: str | None = field(default=None)
    unavailable_until: float = field(default=0.0)
    last_error: str | None = field(default=None)
    last_success: float | None = field(default=None)
    route_state: str = field(default=ROUTE_UNKNOWN)
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    transient_failures: int = field(default=0)

    def available(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.unavailable_until

    def mark_unavailable(self, reason: str, now: float | None = None) -> None:
        self.unavailable_until = (now or time.time()) + UNAVAILABLE_COOLDOWN_S
        self.last_error = reason[:180]

    def mark_transient(self, reason: str, now: float | None = None) -> None:
        """Backoff exponencial para timeout/red sin convertirlo en ruta inválida.

        Reintentar un endpoint FAST cada 15 s cuando tarda >5 s sólo quema cuota y
        vuelve a bloquearlo. El primer reintento espera 30 s y escala hasta 5 min;
        cualquier éxito reinicia el contador.
        """
        self.transient_failures = min(16, int(self.transient_failures) + 1)
        delay = min(TRANSIENT_BACKOFF_MAX_S,
                    TRANSIENT_BACKOFF_BASE_S * (2 ** max(0, self.transient_failures - 1)))
        self.unavailable_until = (now or time.time()) + delay
        self.last_error = reason[:180]

    def mark_success(self) -> None:
        self.transient_failures = 0
        self.unavailable_until = 0.0
        self.last_error = None

    def candidates(self) -> tuple[str, ...]:
        """Las rutas declaradas, nada más. Sin variantes derivadas.

        `paths` sigue admitiendo más de una entrada porque algunas herramientas
        tienen de verdad dos formas oficiales según el plan contratado. Lo que
        desaparece es inventar rutas que el proveedor nunca publicó.
        """
        if self.resolved_path:
            return (self.resolved_path,)
        return tuple(self.paths)

    def note_attempt(self, path: str, error: str | None) -> None:
        """Deja constancia de qué ruta se probó y qué respondió el proveedor.

        Sin esto, una herramienta NO_DISPONIBLE sólo decía que no estaba, y no había
        forma de saber si la ruta era otra, si el plan no la incluye o si el fallo
        era de red. El analista no puede actuar sobre 'no disponible'.
        """
        self.attempts = [a for a in (self.attempts or []) if a.get("path") != path][-7:]
        self.attempts.append({"path": path, "error": (error or "")[:160] or None,
                              "ok": error is None, "at": time.time()})


def path_variants(paths: tuple[str, ...], limit: int = 12) -> tuple[str, ...]:
    """RETIRADO en v1.42.1. Se conserva la firma para no romper llamadores externos.

    Derivaba hasta doce rutas por herramienta (camelCase, snake_case, sin el
    segmento `/options`, colgando de `/v1/tool`...). Ninguna de esas derivaciones
    existe en la API del proveedor: producían 404 reales, gastaban cuota y, al
    reportarse el ÚLTIMO intento, mostraban al operador una URL inventada como si
    fuera la que la herramienta necesita.

    Devuelve las rutas tal cual, sin derivar.
    """
    return tuple(dict.fromkeys(p for p in paths if p))


def build_catalog() -> Dict[str, QuantDataTool]:
    """Catálogo completo. Las rutas ya verificadas en producción van primero."""
    T: List[QuantDataTool] = [
        # ── Exposure ────────────────────────────────────────────────────────
        QuantDataTool(
            "gex_by_strike", "Exposure", "GEX por strike",
            ("/v1/options/tool/exposure-by-strike",),
            lambda t: {"greekMode": "GAMMA", "representationMode": "PER_ONE_PERCENT_MOVE", **_tf(t)},
            lambda p: norm_exposure_by_strike(p, ("gamma", "exposure", "value", "gex")), "FAST"),
        QuantDataTool(
            "dex_by_strike", "Exposure", "DEX por strike",
            ("/v1/options/tool/exposure-by-strike",),
            lambda t: {"greekMode": "DELTA", "representationMode": "PER_ONE_DOLLAR_MOVE", **_tf(t)},
            lambda p: norm_exposure_by_strike(p, ("delta", "exposure", "value", "dex")), "FAST"),
        QuantDataTool(
            "vex_by_strike", "Exposure", "VEX por strike",
            ("/v1/options/tool/exposure-by-strike",),
            lambda t: {"greekMode": "VANNA", "representationMode": "RAW", **_tf(t)},
            lambda p: norm_exposure_by_strike(p, ("vanna", "exposure", "value", "vex")), "MEDIUM"),
        QuantDataTool(
            "chex_by_strike", "Exposure", "CHEX por strike",
            ("/v1/options/tool/exposure-by-strike",),
            lambda t: {"greekMode": "CHARM", "representationMode": "RAW", **_tf(t)},
            lambda p: norm_exposure_by_strike(p, ("charm", "exposure", "value", "chex")), "MEDIUM"),
        QuantDataTool(
            "gex_by_expiration", "Exposure", "GEX por vencimiento",
            ("/v1/options/tool/exposure-by-expiration",),
            lambda t: {"greekMode": "GAMMA", "representationMode": "PER_ONE_PERCENT_MOVE", **_tf(t)},
            lambda p: norm_exposure_by_expiration(p, ("gamma", "exposure", "value", "gex")), "MEDIUM"),
        QuantDataTool(
            "dex_by_expiration", "Exposure", "DEX por vencimiento",
            ("/v1/options/tool/exposure-by-expiration",),
            lambda t: {"greekMode": "DELTA", "representationMode": "PER_ONE_DOLLAR_MOVE", **_tf(t)},
            lambda p: norm_exposure_by_expiration(p, ("delta", "exposure", "value", "dex")), "MEDIUM"),
        QuantDataTool(
            "vex_by_expiration", "Exposure", "VEX por vencimiento",
            ("/v1/options/tool/exposure-by-expiration",),
            lambda t: {"greekMode": "VANNA", "representationMode": "RAW", **_tf(t)},
            lambda p: norm_exposure_by_expiration(p, ("vanna", "exposure", "value", "vex")), "SLOW"),
        QuantDataTool(
            "chex_by_expiration", "Exposure", "CHEX por vencimiento",
            ("/v1/options/tool/exposure-by-expiration",),
            lambda t: {"greekMode": "CHARM", "representationMode": "RAW", **_tf(t)},
            lambda p: norm_exposure_by_expiration(p, ("charm", "exposure", "value", "chex")), "SLOW"),

        # ── Flow Analysis + Dashboard ───────────────────────────────────────
        QuantDataTool(
            "net_flow", "Flow Analysis", "Net Flow",
            ("/v1/options/tool/net-flow",),
            lambda t: {"dataMode": "NET_PREMIUM", "aggregationPeriod": "1m", **_tf(t)},
            lambda p: norm_time_series(p, ("netPremium", "net", "value", "premium")), "FAST"),
        QuantDataTool(
            "net_drift", "Flow Analysis", "Net Drift",
            ("/v1/options/tool/net-drift",),
            lambda t: {"aggregationPeriod": "1m", **_tf(t)},
            norm_net_drift, "FAST"),
        # v1.43.0 · El Interval Map deja de ser un payload crudo guardado «por si
        # acaso» y pasa a ser la FUENTE del mapa de calor dinámico de TRACE, con una
        # herramienta por griega. Antes sólo se pedía GAMMA y se publicaba sin
        # normalizar, así que DELTA, VANNA y CHARM no existían como mapa y el fondo
        # de TRACE tenía que salir del cálculo propio del motor.
        QuantDataTool(
            "interval_map_gamma", "Flow Analysis", "Interval Map · GAMMA",
            ("/v1/options/tool/interval-map",),
            lambda t: {"greekMode": "GAMMA", "aggregationPeriod": "5m", **_tf(t)},
            lambda p: {**norm_interval_map(p, "GAMMA"), "raw": p}, "FAST"),
        QuantDataTool(
            "interval_map_delta", "Flow Analysis", "Interval Map · DELTA",
            ("/v1/options/tool/interval-map",),
            lambda t: {"greekMode": "DELTA", "aggregationPeriod": "5m", **_tf(t)},
            lambda p: {**norm_interval_map(p, "DELTA"), "raw": p}, "MEDIUM"),
        QuantDataTool(
            "interval_map_vanna", "Flow Analysis", "Interval Map · VANNA",
            ("/v1/options/tool/interval-map",),
            lambda t: {"greekMode": "VANNA", "aggregationPeriod": "5m", **_tf(t)},
            lambda p: {**norm_interval_map(p, "VANNA"), "raw": p}, "MEDIUM"),
        QuantDataTool(
            "interval_map_charm", "Flow Analysis", "Interval Map · CHARM",
            ("/v1/options/tool/interval-map",),
            lambda t: {"greekMode": "CHARM", "aggregationPeriod": "5m", **_tf(t)},
            lambda p: {**norm_interval_map(p, "CHARM"), "raw": p}, "MEDIUM"),
        # Alias del mapa de GAMMA. La clave antigua sigue viva para no romper a
        # ningún consumidor que todavía la lea, pero ya publica la matriz normalizada
        # en vez del payload crudo.
        QuantDataTool(
            "options_heat_map", "Flow Analysis", "Options Heat Map",
            ("/v1/options/tool/interval-map",),
            lambda t: {"greekMode": "GAMMA", "aggregationPeriod": "5m", **_tf(t)},
            lambda p: {**norm_interval_map(p, "GAMMA"), "raw": p}, "MEDIUM"),
        QuantDataTool(
            "options_order_flow", "Dashboard", "Options Order Flow",
            ("/v1/options/tool/order-flow/consolidated",),
            lambda t: {"limit": 250, **_tf(t)},
            norm_option_order_flow, "FAST"),
        # La cinta sin consolidar es el print individual; la consolidada agrega por
        # contrato. Son materias primas distintas y por eso son dos herramientas:
        # esconder una detrás de la otra obliga a adivinar qué se está mirando.
        QuantDataTool(
            "options_order_flow_raw", "Dashboard", "Order Flow sin consolidar",
            ("/v1/options/tool/order-flow/unconsolidated",),
            lambda t: {"limit": 250, **_tf(t)},
            norm_option_order_flow, "FAST"),

        # ── Open Interest ───────────────────────────────────────────────────
        QuantDataTool(
            "max_pain_over_time", "Open Interest", "Max Pain / Tiempo",
            ("/v1/options/tool/max-pain-over-time",),
            lambda t: _tf(t),
            lambda p: norm_time_series(p, ("maxPain", "value", "strike")), "SLOW"),
        QuantDataTool(
            "max_pain", "Open Interest", "Max Pain",
            ("/v1/options/tool/max-pain",),
            lambda t: _tf(t),
            lambda p: {"ready": bool(p), "value": _f(_pick(p, "maxPain", "value", "strike")), "raw": p}, "SLOW"),
        QuantDataTool(
            "oi_by_strike", "Open Interest", "OI por strike",
            ("/v1/options/tool/open-interest-by-strike",),
            lambda t: _tf(t),
            lambda p: norm_by_strike(p, ("openInterest", "oi", "value")), "SLOW"),
        QuantDataTool(
            "oi_by_expiration", "Open Interest", "OI por vencimiento",
            ("/v1/options/tool/open-interest-by-expiration",),
            lambda t: _tf(t),
            lambda p: norm_by_expiration(p, ("openInterest", "oi", "value")), "SLOW"),
        QuantDataTool(
            "oi_over_time", "Open Interest", "OI / Tiempo",
            ("/v1/options/tool/open-interest-over-time",),
            lambda t: {"aggregationPeriod": "1d", **_tf(t)},
            lambda p: norm_time_series(p, ("openInterest", "oi", "value")), "SLOW"),
        QuantDataTool(
            "oi_change", "Open Interest", "Cambio de OI",
            ("/v1/options/tool/open-interest-change",),
            lambda t: _tf(t),
            # El CAMBIO de OI entre sesiones es un dato del proveedor. Nunca se
            # reconstruye restando snapshots de volumen: el volumen no dice cuántos
            # contratos quedaron abiertos.
            lambda p: {**norm_by_strike(p, ("change", "oiChange", "openInterestChange",
                                            "delta", "value")),
                       "semantics": "OPEN_INTEREST_CHANGE_BETWEEN_SESSIONS",
                       "source": "QUANTDATA_OPEN_INTEREST_CHANGE"}, "SLOW"),

        # ── Volatility Analysis ─────────────────────────────────────────────
        QuantDataTool(
            "iv_rank", "Volatility Analysis", "IV Rank",
            ("/v1/options/tool/iv-rank",),
            lambda t: {"filter": {"ticker": t}, "lookBackPeriod": 30, "maturity": 30},
            lambda p: {"ready": bool(p), "raw": p}, "SLOW"),
        QuantDataTool(
            "volatility_skew", "Volatility Analysis", "Skew de volatilidad",
            ("/v1/options/tool/volatility-skew",),
            lambda t: _tf(t),
            lambda p: norm_curve(p, ("strike", "delta", "moneyness"), ("iv", "impliedVolatility", "value")), "SLOW"),
        QuantDataTool(
            "term_structure", "Volatility Analysis", "Estructura temporal",
            ("/v1/options/tool/term-structure",),
            lambda t: _tf(t),
            lambda p: norm_curve(p, ("dte", "daysToExpiration", "maturity"), ("iv", "impliedVolatility", "value")), "SLOW"),
        QuantDataTool(
            "volatility_drift", "Volatility Analysis", "Deriva de volatilidad",
            ("/v1/options/tool/volatility-drift",),
            lambda t: {"aggregationPeriod": "5m", **_tf(t)},
            lambda p: norm_time_series(p, ("iv", "impliedVolatility", "drift", "value")), "MEDIUM"),

        # ── Statistics ──────────────────────────────────────────────────────
        QuantDataTool(
            "contract_statistics", "Statistics", "Estadística de contratos",
            ("/v1/options/tool/contract-statistics",),
            lambda t: {"limit": 100, **_tf(t)},
            norm_stats, "MEDIUM"),
        QuantDataTool(
            "trade_side_statistics", "Statistics", "Estadística por lado",
            ("/v1/options/tool/trade-side-statistics",),
            lambda t: _tf(t),
            norm_stats, "MEDIUM"),
        QuantDataTool(
            "market_share", "Statistics", "Cuota de mercado",
            ("/v1/options/tool/market-share",),
            lambda t: _tf(t),
            norm_stats, "MEDIUM"),

        # ── Dark Pool / Equities ────────────────────────────────────────────
        QuantDataTool(
            "equity_prints", "Dark Pool / Equities", "Prints de equity",
            ("/v1/equities/tool/equity-prints",),
            lambda t: {"limit": 250, **_tf(t)},
            norm_prints, "MEDIUM"),
        # v1.42.7 · El flujo de dark pool del proveedor es una fuente DIRECTA. Antes
        # la sección sólo podía inferirlo del campo `venue` de la cinta de otro
        # proveedor, que es una vía indirecta y frágil.
        QuantDataTool(
            "dark_flow", "Dark Pool / Equities", "Flujo de dark pool",
            ("/v1/equities/tool/dark-flow",),
            lambda t: {"aggregationPeriod": "1m", **_tf(t)},
            norm_dark_flow, "MEDIUM"),
        QuantDataTool(
            "dark_pool_levels", "Dark Pool / Equities", "Niveles de dark pool",
            ("/v1/equities/tool/dark-pool-levels",),
            lambda t: _tf(t),
            norm_levels, "MEDIUM"),
        QuantDataTool(
            "stock_price_over_time", "Dark Pool / Equities", "Precio / Tiempo",
            ("/v1/equities/tool/stock-price-over-time",),
            lambda t: {"aggregationPeriod": "1m", **_tf(t)},
            lambda p: norm_time_series(p, ("price", "close", "value")), "MEDIUM"),

        # ── Dashboard (contexto) ────────────────────────────────────────────
        QuantDataTool(
            "gainers_losers", "Dashboard", "Ganadores / Perdedores",
            ("/v1/options/tool/gainers-losers",),
            lambda t: {"limit": 20},
            norm_movers, "DAILY"),
        QuantDataTool(
            "news", "Dashboard", "Noticias",
            ("/v1/news/tool/news-articles",),
            lambda t: {"limit": 25, **_tf(t)},
            norm_news, "DAILY"),
    ]
    return {tool.key: tool for tool in T}


# Páginas integradas del proveedor, para que la interfaz pueda mostrar cobertura
# real por página en lugar de una lista plana de herramientas.
PAGES: Dict[str, List[str]] = {
    "Dashboard": ["options_order_flow", "options_order_flow_raw", "net_flow", "net_drift",
                  "equity_prints", "dark_flow", "dark_pool_levels", "news", "gainers_losers"],
    "Exposure": ["gex_by_strike", "vex_by_strike", "dex_by_strike", "chex_by_strike",
                 "gex_by_expiration", "vex_by_expiration", "dex_by_expiration", "chex_by_expiration"],
    "Flow Analysis": ["net_flow", "net_drift", "interval_map_gamma", "interval_map_delta",
                      "interval_map_vanna", "interval_map_charm", "options_order_flow_raw"],
    "Dark Pool / Equities": ["dark_flow", "dark_pool_levels", "equity_prints", "stock_price_over_time"],
    "Statistics": ["contract_statistics", "trade_side_statistics", "market_share"],
    "Open Interest": ["max_pain", "max_pain_over_time", "oi_change", "oi_by_strike", "oi_by_expiration", "oi_over_time"],
    "Volatility Analysis": ["volatility_drift", "iv_rank", "volatility_skew", "term_structure"],
}


def is_missing_tool_error(message: str) -> bool:
    """¿El error significa "esta herramienta no existe" y no un fallo transitorio?"""
    m = str(message or "").lower()
    if "http 404" in m or "http 400" in m or "http 405" in m or "http 422" in m:
        return True
    return "not found" in m or "unknown tool" in m or "no such" in m


def route_diagnostic(tool: "QuantDataTool") -> Dict[str, Any]:
    """Qué le pasa a esta herramienta, en términos sobre los que se puede actuar."""
    canonical = tool.paths[0] if tool.paths else "—"
    if tool.route_state == ROUTE_OK or tool.last_success:
        return {"state": ROUTE_OK, "route": tool.resolved_path or canonical,
                "detail": "ruta canónica confirmada por el proveedor"}
    if tool.route_state == ROUTE_INVALID:
        return {"state": ROUTE_INVALID, "route": canonical,
                "detail": (f"El proveedor rechaza {canonical}. O su plan no incluye esta "
                           "herramienta, o el proveedor la renombró y hay que actualizar "
                           "el registro canónico. No se prueban rutas alternativas.")}
    return {"state": ROUTE_UNKNOWN, "route": canonical,
            "detail": "todavía no se ha llamado a esta herramienta en este ciclo"}
