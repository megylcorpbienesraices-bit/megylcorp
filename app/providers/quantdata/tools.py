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
from typing import Any, Callable, Dict, List, Optional
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
def es_payload(p: Any) -> bool:
    """¿Esto es un payload del proveedor, o cualquier otra cosa que venía llena?

    v1.57.6 · `bool(p)` daba `True` para CUALQUIER objeto no vacío, así que un
    `QuantDataResponse` colado por la ranura compartida del hub se publicaba como
    `ready: True` con el objeto entero en `raw`. La herramienta parecía viva y lo
    que servía no era del proveedor.

    Un payload es un diccionario con algo dentro. Nada más cuenta.
    """
    return isinstance(p, dict) and bool(p)


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


class FaltaRequisito(Exception):
    """El endpoint exige un campo que ahora mismo NO tenemos.

    v1.57.7 · No es un fallo del proveedor ni de la ruta: es que el contrato pide
    un dato que la terminal todavía no ha resuelto. Mandar la petición igualmente
    produce un 400 que se lee como si el endpoint estuviera roto, y rellenar el
    campo a ojo produce un número creíble y falso. Se dice qué falta y se espera.
    """

    def __init__(self, campo: str, detalle: str = "") -> None:
        self.campo = campo
        self.detalle = detalle or f"el endpoint exige {campo} y no hay valor real"
        super().__init__(self.detalle)


def _cuerpo_max_pain(ticker: str) -> Dict[str, Any]:
    """Contrato oficial de `/v1/options/tool/max-pain`.

    Obligatorios: `filter.ticker` y `filter.expirationDate`. `sessionDate` es
    opcional. NADA MÁS: el endpoint rechaza campos ajenos.

    El vencimiento sale de los que la terminal tiene en pantalla. Si no hay, se
    levanta `FaltaRequisito` en vez de mandar un cuerpo que el proveedor va a
    rechazar. Para el max pain de TODOS los vencimientos existe otro endpoint,
    `max-pain-over-time`, que sólo pide el ticker; es el que usa el carril del
    motor y el que sirve `max_pain_over_time`.
    """
    from .shared import EXPIRY_SELECTION

    symbol = str(ticker or "").strip().upper()
    vencimiento = EXPIRY_SELECTION.principal(symbol)
    if not vencimiento:
        raise FaltaRequisito(
            "filter.expirationDate",
            "max-pain exige un vencimiento y la terminal aún no ha resuelto la "
            "ventana de vencimientos de este activo. Para todos los vencimientos "
            "está max-pain-over-time, que no lo necesita")
    # `sessionDate` es OPCIONAL en este endpoint y está en la lista de campos
    # heredados que `strip_inherited_fields` quita del catálogo entero: se metía
    # de una herramienta en otra y provocaba 400 donde no tocaba. Como es
    # opcional, no se manda: sólo lo obligatorio, que es lo que pide el contrato.
    return {"filter": {"ticker": symbol, "expirationDate": vencimiento}}


def _cuerpo_trade_side(ticker: str) -> Dict[str, Any]:
    """Contrato oficial de `/v1/options/tool/contract-trade-side-statistics`.

    `dataMode` es OBLIGATORIO y sólo admite PREMIUM, TRADE_COUNT o VOLUME.
    Se pide PREMIUM porque es lo que consume el panel: reparto de PRIMA entre
    lado comprador y lado vendedor. Pedir TRADE_COUNT y dibujarlo como prima
    sería mezclar dos magnitudes distintas bajo la misma barra.
    """
    # `sessionDate` es opcional y está en la lista de heredados (ver
    # `_cuerpo_max_pain`): no se manda. Obligatorios, `dataMode` y el ticker.
    return {"dataMode": TRADE_SIDE_DATA_MODE,
            "filter": {"ticker": str(ticker or "").strip().upper()}}


#: Los tres únicos valores que el contrato admite en `dataMode`.
TRADE_SIDE_DATA_MODES = ("PREMIUM", "TRADE_COUNT", "VOLUME")
TRADE_SIDE_DATA_MODE = "PREMIUM"


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
        # v1.51.0 · `_f(value, 0.0) or 0.0` convertia un hueco en un CERO DURO.
        #
        # Para prima neta un cero puede ser una lectura legitima —ese minuto no se
        # pago nada—, pero este normalizador sirve tambien a la IV, al max pain, al
        # interes abierto y al precio, y en esos cuatro el cero es IMPOSIBLE: la IV
        # de un subyacente no es cero, el max pain no esta en el strike 0 y el
        # precio no es 0 dolares. Un bucket sin el campo se publicaba igualmente
        # con valor cero, y la curva de DERIVA DE VOLATILIDAD caia al suelo y
        # volvia a subir en sierra, cuando lo que habia era un hueco.
        #
        # Un hueco viaja como None y cada consumidor decide: la curva lo salta, la
        # suma lo ignora. Lo que no puede es afirmar un cero que nadie midio.
        measured = _f(value, None)
        out.append({
            "t": t,
            "value": measured,
            "value_measured": measured is not None,
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
        # v1.55.0 · AUSENTE NO ES CERO.
        #
        # `_f(..., 0.0) or 0.0` convertia un campo que el proveedor no publica en
        # una prima neta de 0 $. Ese cero viaja hasta la curva acumulada y hasta
        # los KPI, donde se lee como «este minuto no entro dinero» — una
        # afirmacion sobre el mercado que nadie midio. Es el mismo defecto que
        # dibujaba la sierra de la deriva de volatilidad.
        #
        # Un cero que SI viene en la respuesta se conserva como 0.0 y se publica
        # como 0.0: lo que desaparece es el cero inventado.
        call = _f(_pick(r, "netCallPremium", "callPremium", "callSum"), None)
        put = _f(_pick(r, "netPutPremium", "putPremium", "putSum"), None)
        out.append({
            "t": t,
            "timestamp_ms": _epoch_ms(raw_t),
            "net_call_premium": call,
            "net_put_premium": put,
            # El neto del intervalo existe si al menos un lado se midio.
            "net_premium": None if (call is None and put is None) else (call or 0.0) + (put or 0.0),
            "net_call_volume": _f(_pick(r, "netCallVolume", "callVolume"), None),
            "net_put_volume": _f(_pick(r, "netPutVolume", "putVolume"), None),
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
        # v1.55.0 · Sin `or 0.0`: un print cuyo tamano no llega no es un print de
        # cero contratos. Con el cero, la prima derivada salia 0 $ y la cinta
        # contaba una operacion que no movio dinero.
        size = _f(_pick(r, "size", "quantity", "volume", "contracts"))
        premium = _f(_pick(r, "premium", "notional", "value", "totalPremium"), None)
        if premium is None and size is not None:
            # La prima de un contrato es precio × tamaño × multiplicador. Publicar
            # precio × tamaño a secas la dejaría cien veces por debajo.
            premium = price * size * 100.0
        # v1.52.0 · El lado agresor lo resuelve `app.core.aggressor`, autoridad
        # única. Aquí se comparaba el PREFIJO del valor contra una letra suelta,
        # y por eso `AT_BID` —el vendedor cruzando el spread— se clasificaba como
        # COMPRA: empieza por «A». Esa inversión sale directamente en una flecha
        # verde sobre el gráfico de operativa.
        # v1.56.0 · Veredicto FORENSE. Devuelve el lado, de qué campo salió, la
        # procedencia y —cuando no hay lado— el motivo exacto, todo junto. Sin
        # esto, «esta marca dice compra» no se puede contrastar con nada.
        from ...core.aggressor import classify_trade as _agg_classify
        veredicto = _agg_classify(r)
        verdict = veredicto["aggressor"]
        # `aggressor_field` responde «¿QUÉ se leyó?»: el nombre del campo cuando
        # lo hubo, y `NBBO` cuando el lado salió de medir el precio contra
        # bid/ask. Dejarlo en None ahí perdía la mitad de la respuesta.
        agg_field = veredicto["side_field"] or (
            "NBBO" if veredicto["classification_source"] == "NBBO" else None)
        side = str(_pick(r, "side", "aggressor", "direction", "tradeSide") or "UNKNOWN").upper()
        out.append({
            "t": t,
            # ── LOS CAMPOS QUE PERMITEN AUDITAR EL LADO ─────────────────────
            # No son metadatos: son la prueba. Una tabla de evidencia con
            # tradeSideCode, bid, ask y optionPrice al lado del veredicto es lo
            # único que demuestra que la flecha verde no está invertida.
            "trade_id": (lambda v: None if v is None else str(v))(
                _pick(r, "id", "tradeId", "trade_id", "executionId")),
            "option_symbol": (lambda v: None if v is None else str(v))(
                _pick(r, "osi", "optionSymbol", "option_symbol", "contract", "symbol")),
            "trade_side_code": veredicto["trade_side_code"],
            "classification_source": veredicto["classification_source"],
            "classification_reason": veredicto["why"],
            "ticker": str(_pick(r, "ticker", "underlying", "symbol") or "").upper(),
            "option_type": str(_pick(r, "optionType", "type", "right", "putCall") or "").upper()[:4],
            "strike": _f(_pick(r, "strike", "strikePrice")),
            "expiration": str(_pick(r, "expiration", "expirationDate", "expiry") or ""),
            "price": price,
            "size": size,
            "premium": premium,
            # `side` es el valor CRUDO del proveedor, tal cual, para diagnóstico.
            # No se usa para decidir la dirección.
            "side": side,
            "aggressor": verdict,
            "aggressor_field": agg_field,
            # El NBBO del instante viaja para poder AUDITAR la clasificación:
            # sin él, «esta marca dice compra» no se puede contrastar con nada.
            "bid": _f(_pick(r, "bid", "bidPrice", "nbboBid", "bestBid")),
            "ask": _f(_pick(r, "ask", "askPrice", "nbboAsk", "bestAsk")),
            # Los nombres del contrato publicado, tal cual, junto a los internos.
            # La tabla de evidencia se lee contra la respuesta del proveedor, y
            # traducir los nombres por el camino obliga a un mapeo mental que es
            # justo donde se cuelan los errores.
            "bidPrice": _f(_pick(r, "bidPrice", "bid", "nbboBid", "bestBid")),
            "askPrice": _f(_pick(r, "askPrice", "ask", "nbboAsk", "bestAsk")),
            "optionPrice": price,
            "tradeTime": t,
            # +1 comprador en ask, −1 vendedor en bid, 0 sin clasificar. Es el
            # mismo convenio en todo el programa, resuelto en un solo sitio.
            "direction": 1 if verdict == "BUY" else -1 if verdict == "SELL" else 0,
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
    # COBERTURA del lado agresor. Sin esto, «todas las marcas salen neutras» no
    # se puede diagnosticar: no se sabe si el proveedor no manda el campo, si lo
    # manda con otro nombre o si son operaciones ejecutadas en el medio.
    sided = sum(1 for r in out if r["aggressor"] in ("BUY", "SELL"))
    by_field: Dict[str, int] = {}
    for r in out:
        key = r.get("aggressor_field") or "NINGUNO"
        by_field[key] = by_field.get(key, 0) + 1

    # v1.56.0 · CONTADORES POR PROCEDENCIA Y POR MOTIVO.
    #
    # «68 % con lado» no dice si el otro 32 % son ejecuciones al medio —una cinta
    # sana— o filas a las que les falta el campo —una cinta rota—. Son dos
    # averías distintas con arreglos distintos, y hasta ahora se contaban juntas.
    from ...core.aggressor import (SRC_FIELD, SRC_NBBO, SRC_NONE, SRC_PRIMARY,
                                   WHY_MID, WHY_NBBO_MISSING, WHY_NO_SIDE_FIELD)
    por_fuente: Dict[str, int] = {}
    por_motivo: Dict[str, int] = {}
    for r in out:
        f = str(r.get("classification_source") or SRC_NONE)
        por_fuente[f] = por_fuente.get(f, 0) + 1
        w = str(r.get("classification_reason") or "")
        if w:
            por_motivo[w] = por_motivo.get(w, 0) + 1
    con_lado = [r for r in out if r["aggressor"] in ("BUY", "SELL")]
    return {"ready": bool(out), "rows": out, "count": len(out),
            "source": "QUANTDATA_OPTIONS_ORDER_FLOW",
            "aggressor_coverage": {
                "with_side": sided,
                "total": len(out),
                "pct": round(100.0 * sided / len(out), 1) if out else None,
                "by_field": by_field,
                # Los contadores que exige la auditoría, con estos nombres.
                "tape_rows": len(out),
                "explicit_side_rows": sum(1 for r in con_lado
                                          if r.get("classification_source") in (SRC_PRIMARY, SRC_FIELD)),
                "primary_field_rows": por_fuente.get(SRC_PRIMARY, 0),
                "nbbo_classified_rows": sum(1 for r in con_lado
                                            if r.get("classification_source") == SRC_NBBO),
                "unknown_rows": len(out) - sided,
                "mid_trade_rows": por_motivo.get(WHY_MID, 0),
                "nbbo_missing_rows": por_motivo.get(WHY_NBBO_MISSING, 0),
                "without_side_field_rows": por_motivo.get(WHY_NO_SIDE_FIELD, 0),
                "buy_rows": sum(1 for r in out if r["aggressor"] == "BUY"),
                "sell_rows": sum(1 for r in out if r["aggressor"] == "SELL"),
                "by_source": por_fuente,
                "by_reason": por_motivo,
            }}


#: Sufijos que, en el nombre de un campo, identifican una MAGNITUD de volumen.
#: Se usan sólo cuando ninguno de los nombres declarados aparece en la respuesta.
_VOLUME_HINTS = ("volume", "shares", "size", "qty", "quantity")


def resolve_numeric_field(row: Dict[str, Any], names: tuple, *, contains: tuple = (),
                          hints: tuple = ()) -> tuple[Optional[float], str]:
    """Valor numérico de un campo, y CÓMO se encontró.

    v1.48.0 · Un normalizador con una lista fija de nombres falla en silencio
    cuando el proveedor usa otro: devuelve 0.0 y la sección publica «0.0 acc»
    teniendo seiscientos intervalos descargados. Un cero ahí afirma «no hubo
    volumen oscuro», que es una conclusión sobre el mercado y no un hueco.

    Aquí se intenta primero por NOMBRE DECLARADO, que es lo correcto cuando el
    contrato se conoce. Si ninguno aparece, se DERIVA de la propia respuesta:
    el campo numérico cuyo nombre contiene a la vez el concepto y una pista de
    magnitud. No es adivinar un valor —el valor lo manda el proveedor—; es
    descubrir bajo qué nombre lo manda, y se DECLARA cuál se usó para que el
    Auditor pueda enseñarlo y el contrato quede fijado después.
    """
    if not isinstance(row, dict):
        return None, ""
    direct = _pick(row, *names)
    v = _f(direct)
    if v is not None:
        for n in names:
            if n in row and row[n] is not None:
                return v, n
        return v, names[0]
    if not contains:
        return None, ""
    want = tuple(c.lower() for c in contains)
    pistas = tuple(h.lower() for h in (hints or _VOLUME_HINTS))
    best: Optional[tuple[float, str]] = None
    for key, raw in row.items():
        k = str(key).lower()
        if not any(c in k for c in want):
            continue
        if pistas and not any(h in k for h in pistas):
            continue
        n = _f(raw)
        if n is None:
            continue
        # Se prefiere el nombre más corto: `darkVolume` antes que
        # `darkVolumeFiveDayAverage`, que mide otra cosa.
        if best is None or len(key) < len(best[1]):
            best = (n, str(key))
    return best if best else (None, "")


def norm_dark_flow(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Dark Flow de Quant Data, con el MAPEO DEL CONTRATO, sin heurística.

    ── El fallo que esto elimina ────────────────────────────────────────────

    v1.45.0 resolvía el volumen oscuro "descubriendo" qué campo lo representaba:

        resolve_numeric_field(r, ("darkVolume", "darkPoolVolume",
                                  "offExchangeVolume", "volume"),
                              contains=("dark", "offexchange", "off_exchange"))

    El contrato publicado de `dark-flow` define el bucket con cuatro campos:

        notionalValue   dólares off-exchange del intervalo
        size            ACCIONES off-exchange del intervalo
        tradeCount      número de impresiones
        stockPrice      precio de referencia del intervalo

    `size` no estaba en la lista de nombres y no contiene «dark» ni
    «offExchange», así que NINGUNA de las dos vías lo encontraba. Con 608
    intervalos reales descargados, `dark_volume` salía `None` en los 608 y la
    sección mostraba SIN DATOS teniendo el dato delante.

    La heurística era el error de fondo, no la lista: cuando existe contrato
    publicado, adivinar el campo sólo puede acertar por casualidad. Aquí el
    mapeo es determinista.

    ── Qué pasa si el contrato no se cumple ─────────────────────────────────

    Si una respuesta que debería traer `size` no lo trae, eso NO es cero
    acciones: es un desajuste de esquema, y se declara `SCHEMA_MISMATCH` con
    los campos que sí llegaron. Un cero afirmaría que no hubo volumen oscuro.

    Se aceptan además los alias históricos como RESPALDO DECLARADO —una cuenta
    con una versión anterior del endpoint sigue funcionando—, pero el campo que
    acabó sirviendo viaja siempre en `field_map`.
    """
    rows = _rows(payload, "buckets", "series", "points", "timeline", "flow") or _keyed_rows(payload, "timestamp")
    out: List[Dict[str, Any]] = []
    field_map: Dict[str, str] = {}
    observed_fields: set = set()
    schema_misses = 0

    def _contract(r: Dict[str, Any], canonical: str, *aliases: str) -> tuple[Optional[float], Optional[str]]:
        """El campo del contrato primero; los alias sólo como respaldo declarado."""
        for key in (canonical, *aliases):
            if key in r:
                v = _f(r.get(key), None)
                if v is not None:
                    return v, key
        return None, None

    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _ts_iso(_pick(r, "timestamp", "time", "t", "bucket", "date"))
        if t is None:
            continue
        observed_fields.update(str(k) for k in r)

        # size = ACCIONES off-exchange. Es el campo del contrato, no un alias.
        shares, shares_key = _contract(r, "size", "darkVolume", "darkPoolVolume",
                                       "offExchangeVolume", "shares")
        notional, notional_key = _contract(r, "notionalValue", "darkNotional",
                                           "notional", "dollarVolume")
        prints, prints_key = _contract(r, "tradeCount", "trades", "printCount", "count")
        price, price_key = _contract(r, "stockPrice", "price", "underlyingPrice")
        if price is not None and price <= 0:
            price = None

        if shares_key:
            field_map["dark_volume"] = shares_key
        if notional_key:
            field_map["dark_notional"] = notional_key
        if prints_key:
            field_map["dark_prints"] = prints_key
        if price_key:
            field_map["stock_price"] = price_key
        if shares is None:
            schema_misses += 1

        # El contrato de dark-flow NO publica volumen total ni lit: todo lo que
        # entrega es off-exchange. Derivar un porcentaje sobre un total que no
        # existe sería inventarlo, así que viaja en None y la sección lo dice.
        total, total_key = _contract(r, "totalVolume", "litAndDarkVolume", "consolidatedVolume")
        lit, lit_key = _contract(r, "litVolume", "exchangeVolume")
        if total is None and lit is not None and shares is not None:
            total = lit + shares
        if total_key:
            field_map["total_volume"] = total_key

        out.append({
            "t": t,
            # `None` y no 0.0: un intervalo sin el campo NO es un intervalo sin
            # volumen oscuro.
            "dark_volume": shares,
            "dark_shares": shares,
            "lit_volume": lit,
            "total_volume": total,
            "dark_notional": notional,
            "dark_prints": None if prints is None else int(prints),
            "dark_share_pct": (round(100.0 * shares / total, 4)
                               if (shares is not None and total and total > 0) else None),
            "stock_price": price,
        })

    out.sort(key=lambda x: x["t"])
    measured = sum(1 for r in out if r.get("dark_volume") is not None)
    # Filas que llegaron pero ninguna con el campo de acciones: el contrato no se
    # cumple y hay que decirlo con ese nombre, nunca con un cero.
    schema_mismatch = bool(out) and measured == 0
    return {
        "ready": bool(out),
        "rows": out,
        "count": len(out),
        "source": "QUANTDATA_DARK_FLOW",
        "contract": "notionalValue|size|tradeCount|stockPrice",
        "field_map": dict(field_map),
        "observed_fields": sorted(observed_fields)[:40],
        "intervals_with_volume": measured,
        "intervals_without_volume": schema_misses,
        "volume_field_resolved": bool(field_map.get("dark_volume")),
        "schema_state": "SCHEMA_MISMATCH" if schema_mismatch else "CONTRACT_OK",
        "schema_detail": (
            f"{len(out)} intervalos sin `size`; campos recibidos: "
            f"{', '.join(sorted(observed_fields)[:12])}" if schema_mismatch else ""),
    }


# Códigos de centro de ejecución que identifican una operación FUERA de bolsa.
# Los TRF/ADF de FINRA son donde se reportan las ejecuciones off-exchange de EE.UU.
_OFF_EXCHANGE_VENUES = ("TRF", "FINRA", "ADF", "OTC", "DARK", "D ", "FNRA")


def _off_exchange_flag(row: Dict[str, Any], venue: str) -> tuple[Optional[bool], str]:
    """¿Fue esta operación fuera de bolsa? True / False / None (no se sabe).

    v1.45.0 · Antes esto era:

        "off_exchange": bool(_pick(r, "offExchange", "darkPool", "isDarkPool") or False)

    y ahí estaba el defecto que dejaba DARK POOL en SIN DATOS con cientos de prints
    descargados: si la cuenta no publica esos tres campos exactos, `_pick` devuelve
    None, `bool(None or False)` es False, y **todas** las impresiones quedaban
    marcadas como ejecutadas en bolsa. Un dato ausente se convertía en una
    afirmación negativa, que es la forma más silenciosa de perder una sección
    entera.

    Ahora son tres estados. Cuando el proveedor no lo declara se intenta deducir del
    centro de ejecución —que es la vía de auditoría— y si tampoco hay venue, se
    admite que NO SE SABE en vez de decir que no.
    """
    declared = _pick(row, "offExchange", "darkPool", "isDarkPool", "off_exchange",
                     "isOffExchange", "dark")
    if isinstance(declared, bool):
        return declared, "PROVIDER_FLAG"
    if isinstance(declared, (int, float)):
        return bool(declared), "PROVIDER_FLAG"
    if isinstance(declared, str) and declared.strip():
        v = declared.strip().lower()
        if v in ("true", "yes", "y", "1", "dark", "off", "offexchange"):
            return True, "PROVIDER_FLAG"
        if v in ("false", "no", "n", "0", "lit", "on", "onexchange"):
            return False, "PROVIDER_FLAG"
    code = str(venue or "").strip().upper()
    if code:
        if any(tag in code for tag in _OFF_EXCHANGE_VENUES):
            return True, "VENUE_CODE"
        return False, "VENUE_CODE"
    return None, "UNKNOWN"


def norm_prints(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    by_method: Dict[str, int] = {}
    for r in _rows(payload, "prints", "trades", "executions"):
        t = _pick(r, "timestamp", "time", "t", "executedAt")
        price = _f(_pick(r, "price", "tradePrice", "executionPrice"))
        if t is None or price is None:
            continue
        size = _f(_pick(r, "size", "shares", "quantity", "volume"), 0.0) or 0.0
        venue = str(_pick(r, "venue", "exchange", "market") or "")
        off, method = _off_exchange_flag(r, venue)
        by_method[method] = by_method.get(method, 0) + 1
        out.append({
            "t": str(t), "price": price, "size": size,
            "notional": _f(_pick(r, "notional", "value", "premium"), price * size) or (price * size),
            "side": str(_pick(r, "side", "aggressor", "direction") or "UNKNOWN").upper(),
            "venue": venue,
            # Tres estados. `None` = el proveedor no lo dice y no hay venue que
            # interpretar; tratarlo como False era inventar una clasificación.
            "off_exchange": off,
            "off_exchange_method": method,
        })
    out.sort(key=lambda x: x["t"])
    confirmed = sum(1 for r in out if r["off_exchange"] is True)
    unknown = sum(1 for r in out if r["off_exchange"] is None)
    return {"ready": bool(out), "rows": out, "count": len(out),
            # Con esto la sección puede distinguir «no hubo dark pool» de «no
            # supimos clasificar», que no son lo mismo y antes se veían igual.
            "off_exchange_confirmed": confirmed,
            "off_exchange_unknown": unknown,
            "classification": by_method,
            "classification_note": ("`off_exchange` es tri-estado: True/False/None. "
                                    "None significa que no se pudo clasificar, no "
                                    "que la operación fuera en bolsa.")}


def _level_entries(payload: Any) -> List[tuple]:
    """Pares (precio, datos) de `dark-pool-levels`, venga como venga.

    v1.49.0 · El proveedor documenta la respuesta como un MAPA por nivel de
    precio —la clave ES el precio— y el extractor genérico de filas sólo sabía
    leer listas. Con un mapa devolvía cero niveles, así que un 200 perfectamente
    válido se publicaba como «SIN DATOS» y era indistinguible de una sesión sin
    actividad fuera de bolsa.

    Se admiten las dos formas: el mapa del contrato y una lista de objetos, por
    si el envoltorio cambia. En el mapa, la clave manda como precio; en la lista,
    manda el campo.
    """
    if not isinstance(payload, (dict, list)):
        return []
    # El bloque de niveles puede venir en la raíz o bajo un envoltorio.
    block = payload
    if isinstance(payload, dict):
        for key in ("levels", "darkPoolLevels", "priceLevels", "zones", "data"):
            inner = payload.get(key)
            if isinstance(inner, (dict, list)) and inner:
                block = inner
                break

    out: List[tuple] = []
    if isinstance(block, dict):
        for key, val in block.items():
            if not isinstance(val, dict):
                continue
            # La CLAVE es el nivel de precio. Si no lo es, se busca dentro.
            price = _f(key)
            if price is None:
                price = _f(_pick(val, "priceLevel", "price", "level"))
            if price is not None:
                out.append((price, val))
    elif isinstance(block, list):
        for r in block:
            if not isinstance(r, dict):
                continue
            price = _f(_pick(r, "priceLevel", "price", "level"))
            if price is not None:
                out.append((price, r))
    return out


#: Campos que `norm_levels` ya expone con nombre propio. Lo que no esté aquí y
#: venga en la respuesta se conserva en `extra`: un normalizador que descarta en
#: silencio campos oficiales es indistinguible de un proveedor que no los manda.
_LEVEL_MAPPED = {
    "price", "level", "pricelevel", "notional", "notionalvalue", "value",
    "dollarvolume", "shares", "size", "volume", "prints", "count", "trades",
    "tradecount",
    "darkvolume", "litvolume", "percentofvolume", "pctofvolume",
}


def _level_extra(row: Dict[str, Any]) -> Dict[str, Any]:
    """Los campos oficiales que este normalizador todavía no nombra."""
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if str(k).lower().replace("_", "") in _LEVEL_MAPPED:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
    return out


def norm_levels(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Niveles de precio con volumen fuera de bolsa.

    v1.46.0 · Antes se quedaban cuatro campos y el resto se perdía, incluido el
    precio de referencia del subyacente que el proveedor publica a nivel de
    respuesta. Ahora se preservan: nivel de precio, nocional, acciones, número de
    operaciones, volumen oscuro y su porcentaje, y **todo campo escalar que el
    proveedor mande y aquí no tenga nombre**, bajo `extra`. Quedarse sin un campo
    porque el normalizador no lo conocía es indistinguible, desde la pantalla, de
    que el proveedor no lo haya enviado.
    """
    out = []
    for p, r in _level_entries(payload):
        if not isinstance(r, dict) or p is None:
            continue
        row = {
            "price": p,
            # Nombres del contrato publicado primero; los alias detrás, para
            # respuestas antiguas. `notionalValue`, `size` y `tradeCount` son los
            # que documenta el proveedor.
            # v1.55.0 · Sin `or 0.0`: un nivel cuyo nocional el proveedor no
            # publica NO vale 0 $. Con el cero se colaba al final de la tabla
            # con una cifra inventada; ahora sale SIN DATO y se ve que falta.
            "notional": _f(_pick(r, "notionalValue", "notional", "value", "dollarVolume")),
            "shares": _f(_pick(r, "size", "shares", "volume")),
            "prints": (lambda n: None if n is None else int(n))(
                _f(_pick(r, "tradeCount", "prints", "count", "trades"))),
            "dark_volume": _f(_pick(r, "darkVolume")),
            "lit_volume": _f(_pick(r, "litVolume")),
            "pct_of_volume": _f(_pick(r, "percentOfVolume", "pctOfVolume")),
        }
        extra = _level_extra(r)
        if extra:
            row["extra"] = extra
        out.append(row)
    # Los niveles SIN nocional no pueden ordenarse por tamano, asi que van al
    # final en vez de fingir que valen cero y colarse entre los pequenos.
    out.sort(key=lambda x: (x["notional"] is None, -(x["notional"] or 0.0)))
    # Precio del subyacente publicado con la respuesta, no por nivel. Es lo que
    # permite situar los niveles respecto al último precio SIN mezclar la fuente
    # del subyacente con la del dark pool.
    latest = None
    if isinstance(payload, dict):
        latest = _f(_pick(payload, "latestStockPrice", "stockPrice", "underlyingPrice",
                          "lastPrice", "spot"))
        if latest is None:
            inner = payload.get("data")
            if isinstance(inner, dict):
                latest = _f(_pick(inner, "latestStockPrice", "stockPrice"))
    block: Dict[str, Any] = {"ready": bool(out), "rows": out, "count": len(out)}
    if latest is not None:
        block["latest_stock_price"] = latest
    return block


def norm_stats(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = []
    for r in _rows(payload, "contracts", "statistics", "stats", "rows"):
        if not isinstance(r, dict):
            continue
        out.append({
            "label": str(_pick(r, "contract", "symbol", "name", "label", "expiration") or ""),
            # v1.55.0 · Sin `or 0.0`: un contrato cuya prima el proveedor no
            # publica no ha negociado 0 $. Con el cero se ordenaba el ultimo con
            # una cifra que nadie midio y la tabla lo daba por bueno.
            "premium": _f(_pick(r, "premium", "notional", "totalPremium")),
            "contracts": _f(_pick(r, "contracts", "volume", "size")),
            "trades": (lambda n: None if n is None else int(n))(
                _f(_pick(r, "trades", "count", "executions"))),
            "bid_side": _f(_pick(r, "bidSide", "sellPremium", "askSidePremium"), None),
            "ask_side": _f(_pick(r, "askSide", "buyPremium", "bidSidePremium"), None),
        })
    # Lo que no tiene prima no se puede ordenar por prima: va al final.
    out.sort(key=lambda x: (x["premium"] is None, -abs(x["premium"] or 0.0)))
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
    # Variantes del cuerpo que se prueban si el proveedor rechaza la validación.
    # `None` = la herramienta no admite reparación y un 400 es definitivo.
    body_variants: Callable[[str], List[Dict[str, Any]]] | None = field(default=None)
    accepted_variant: int | None = field(default=None)
    validation_error: str | None = field(default=None)
    variants_tried: int = field(default=0)
    # Correcciones DICTADAS por el proveedor en un 400 anterior, no adivinadas.
    # Se conservan, así que el descubrimiento se paga una sola vez.
    repair_added: Dict[str, Any] = field(default_factory=dict)
    repair_removed: tuple = field(default=())
    repair_log: List[str] = field(default_factory=list)
    stripped_fields: List[str] = field(default_factory=list)
    provider_status: str | None = field(default=None)

    def available(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.unavailable_until

    def request_body(self, ticker: str) -> Dict[str, Any]:
        """Cuerpo vigente: el mínimo de la herramienta más lo que el proveedor dictó.

        No hay lista de formas candidatas. Se parte del cuerpo mínimo, se quitan los
        campos que no pertenecen a esta herramienta, y se aplican las correcciones
        aprendidas de errores anteriores del propio proveedor.
        """
        base = dict(self.body(ticker))
        base, dropped = strip_inherited_fields(base)
        # Y lo que esta herramienta concreta rechaza, aunque otra lo acepte.
        forbidden = TOOL_FORBIDDEN_FIELDS.get(self.key, ())
        extra = [k for k in base if k in forbidden]
        if extra:
            base = {k: v for k, v in base.items() if k not in forbidden}
            dropped = sorted(set(list(dropped) + extra))
        if dropped:
            self.stripped_fields = sorted(set(list(self.stripped_fields) + dropped))
        for key in self.repair_removed:
            base.pop(key, None)
        base.update(self.repair_added)
        return base

    def request_bodies(self, ticker: str) -> List[Dict[str, Any]]:
        """Compatibilidad con el carril antiguo: un solo cuerpo, el vigente."""
        return [self.request_body(ticker)]

    def learn_repair(self, before: Dict[str, Any], after: Dict[str, Any], note: str = "") -> None:
        """Recuerda la corrección que el proveedor acaba de dictar."""
        for key in before:
            if key not in after:
                self.repair_removed = tuple(sorted(set(self.repair_removed) | {key}))
                self.repair_added.pop(key, None)
        for key, value in after.items():
            if before.get(key) != value:
                self.repair_added[key] = value
                self.repair_removed = tuple(k for k in self.repair_removed if k != key)
        if note:
            self.repair_log.append(note[:120])
            del self.repair_log[:-8]

    def note_validation_failure(self, detail: str) -> None:
        self.validation_error = str(detail)[:400]

    def note_variant_accepted(self, index: int) -> None:
        self.accepted_variant = int(index)
        self.validation_error = None

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


# Campos que NINGUNA herramienta debe heredar de otra. Si aparecen en un cuerpo
# es porque se copiaron de un endpoint vecino, y el proveedor los rechaza.
INHERITED_FIELD_BLOCKLIST = (
    "sessionDate", "timeRange", "snapshotTime", "filterExpression",
    "pagination", "projection", "sort", "orderBy", "cursor", "offset",
)

#: Campos que un endpoint concreto rechaza aunque sean válidos en otros.
#:
#: v1.49.0 · La lista general no basta. `aggregationPeriod` es legítimo en
#: `dark-flow` y está en la tabla de reparaciones, así que un 400 mal leído
#: podía hacer que se lo añadiéramos a `dark-pool-levels`, que lo rechaza. La
#: prohibición tiene que ser de la HERRAMIENTA, no del catálogo.
TOOL_FORBIDDEN_FIELDS: Dict[str, tuple] = {
    "dark_pool_levels": (
        "sessionDate", "timeRange", "snapshotTime", "aggregationPeriod",
        "filterExpression", "projection", "pagination",
    ),
}


def last_valid_session_date(now=None) -> str:
    """Fecha de la última sesión con datos, en `YYYY-MM-DD`.

    Un sábado no es una sesión. Mandar la fecha de hoy sin comprobarlo produce
    o un 400 o un 200 vacío según cómo lo trate el proveedor, y las dos cosas se
    leen en pantalla como «no hay dark pool» cuando lo que pasa es que se pidió
    un día que no existe.
    """
    try:
        from app.core import session_resolver
        return session_resolver.resolve(now, require_completed=True).last_completed.isoformat()
    except Exception:
        from datetime import date, timedelta
        d = (now.date() if hasattr(now, "date") else date.today())
        while d.weekday() >= 5:                      # sábado=5, domingo=6
            d -= timedelta(days=1)
        return d.isoformat()


def _dark_pool_levels_body(ticker: str, now=None) -> Dict[str, Any]:
    """Cuerpo de `dark-pool-levels`, según el contrato publicado.

    v1.49.0 · El contrato oficial exige DOS cosas en el nivel superior:

        sessionDateRange.startDate   obligatorio
        filter.ticker                obligatorio
        sessionDateRange.endDate     opcional

    Y este endpoint NO acepta `sessionDate`, `timeRange`, `snapshotTime`,
    `aggregationPeriod`, `filterExpression`, `projection` ni `pagination`.

    Ahí estaba el 400. La versión anterior enviaba el cuerpo mínimo —sólo
    `filter.ticker`— razonando que un cuerpo con campos de más es tan inválido
    como uno con campos de menos. Es cierto, y aun así estaba incompleto: le
    faltaba un campo obligatorio que ninguna otra herramienta usa.

    La confusión de fondo es que `dark-flow` sí acepta `sessionDate`/`timeRange`
    mientras que `dark-pool-levels` usa EXCLUSIVAMENTE `sessionDateRange`. Son
    dos nombres parecidos para dos contratos distintos, y por eso partir del
    cuerpo de la herramienta vecina no podía funcionar por mucho que se
    recortara.

    La fecha se resuelve a la última sesión válida: en fin de semana o con el
    mercado cerrado, pedir el día de hoy es pedir un día que no existe.
    """
    day = last_valid_session_date(now)
    return {"sessionDateRange": {"startDate": day, "endDate": day}, **_tf(ticker)}


def strip_inherited_fields(body: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """Quita campos que no pertenecen a esta herramienta. Devuelve (cuerpo, quitados)."""
    removed = [k for k in body if k in INHERITED_FIELD_BLOCKLIST]
    if not removed:
        return body, []
    return {k: v for k, v in body.items() if k not in INHERITED_FIELD_BLOCKLIST}, removed


# Valores por omisión para campos que el proveedor puede declarar obligatorios.
# No se envían nunca por iniciativa propia: sólo cuando el 400 los NOMBRA.
_FIELD_DEFAULTS: Dict[str, Any] = {
    "limit": 100,
    "aggregationPeriod": "1d",
    "lookBackPeriod": 1,
    "greekMode": "GAMMA",
    "dataMode": "NET_PREMIUM",
    "representationMode": "RAW",
    "maturity": 30,
}

_MISSING_HINTS = ("field required", "is required", "missing", "required property",
                  "required field", "must be provided")
_UNKNOWN_HINTS = ("unknown field", "extra fields not permitted", "not permitted",
                  "unexpected", "additional properties", "not allowed", "unrecognized")


def classify_validation_errors(fields: List[str]) -> Dict[str, List[str]]:
    """Qué pide y qué sobra, leído de lo que el proveedor dijo.

    Cada entrada llega como `"body.lookBackPeriod: field required"`. Se separa el
    nombre del campo del motivo, y el motivo decide la corrección:

        falta   → añadir el campo con un valor por omisión conocido
        sobra   → quitarlo del cuerpo
        inválido→ probar otro valor conocido para ese campo

    Esto es reparación GUIADA POR EL CONTRATO que el proveedor acaba de comunicar,
    no una búsqueda a ciegas por el espacio de cuerpos posibles.
    """
    out: Dict[str, List[str]] = {"missing": [], "unknown": [], "invalid": []}
    for raw in fields or []:
        text = str(raw)
        name, _, reason = text.partition(":")
        # `body.lookBackPeriod` → `lookBackPeriod`; se ignoran los prefijos de ruta.
        leaf = name.strip().split(".")[-1].strip().strip("'\"[]")
        if not leaf:
            continue
        low = (reason or text).lower()
        if any(h in low for h in _UNKNOWN_HINTS):
            out["unknown"].append(leaf)
        elif any(h in low for h in _MISSING_HINTS):
            out["missing"].append(leaf)
        else:
            out["invalid"].append(leaf)
    return out


def repair_body(body: Dict[str, Any], fields: List[str],
                forbidden: tuple = ()) -> tuple[Dict[str, Any] | None, str]:
    """Siguiente cuerpo a probar, derivado del error. `None` si no hay corrección.

    Devolver `None` es una respuesta legítima y necesaria: significa que el
    proveedor rechaza algo que no sabemos corregir, y entonces lo que corresponde
    es DECIRLO con el campo exacto, no seguir probando formas al azar.
    """
    buckets = classify_validation_errors(fields)
    nxt = dict(body)
    notes: List[str] = []

    for name in buckets["unknown"]:
        if name in nxt:
            nxt.pop(name, None)
            notes.append(f"−{name}")
    for name in buckets["missing"]:
        if forbidden and name in forbidden:
            # El proveedor no puede pedir un campo que su propio contrato
            # prohíbe: si el diagnóstico dice eso, es que se leyó mal.
            continue
        if name in _FIELD_DEFAULTS and name not in nxt:
            nxt[name] = _FIELD_DEFAULTS[name]
            notes.append(f"+{name}={_FIELD_DEFAULTS[name]!r}")
    for name in buckets["invalid"]:
        # Un valor inválido sólo se corrige si conocemos otro para ese campo;
        # inventar valores es exactamente lo que no queremos hacer.
        if name in _FIELD_DEFAULTS and nxt.get(name) != _FIELD_DEFAULTS[name]:
            nxt[name] = _FIELD_DEFAULTS[name]
            notes.append(f"~{name}={_FIELD_DEFAULTS[name]!r}")

    if not notes or nxt == body:
        return None, ""
    return nxt, " ".join(notes)


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
            _cuerpo_max_pain,
            lambda p: {"ready": es_payload(p), "value": _f(_pick(p, "maxPain", "value", "strike")), "raw": p}, "SLOW"),
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
            lambda p: {"ready": es_payload(p), "raw": p}, "SLOW"),
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
            # v1.57.7 · La ruta era incorrecta: `/v1/options/tool/trade-side-statistics`
            # devuelve 404 porque no existe. La oficial es
            # `/v1/options/tool/contract-trade-side-statistics`, y exige `dataMode`.
            "contract_trade_side_statistics", "Statistics", "Estadística por lado",
            ("/v1/options/tool/contract-trade-side-statistics",),
            _cuerpo_trade_side,
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
            # v1.52.0 · `sessionDate` explícito, resuelto a la última sesión
            # VÁLIDA. Sin él, fuera de horario el proveedor devuelve vacío y la
            # sección lo publicaba como MARKET_CLOSED con cero prints, que se lee
            # como «no hubo dark pool» cuando lo que pasa es que no se le pidió
            # ninguna sesión concreta. Un fin de semana tiene la sesión del
            # viernes, y esa sí tiene prints.
            lambda t: {"limit": 250, "sessionDate": last_valid_session_date(), **_tf(t)},
            norm_prints, "MEDIUM"),
        # v1.42.7 · El flujo de dark pool del proveedor es una fuente DIRECTA. Antes
        # la sección sólo podía inferirlo del campo `venue` de la cinta de otro
        # proveedor, que es una vía indirecta y frágil.
        QuantDataTool(
            "dark_flow", "Dark Pool / Equities", "Flujo de dark pool",
            ("/v1/equities/tool/dark-flow",),
            lambda t: {"aggregationPeriod": "1m", **_tf(t)},
            norm_dark_flow, "MEDIUM"),
        # v1.45.0 · `dark-pool-levels` devolvía HTTP 400 «Request validation failed»
        # con el cuerpo mínimo `{"filter": {"ticker": …}}`, que es el que aceptan
        # otras herramientas. El proveedor exige algo más en ESTA, y su documentación
        # no es accesible desde el entorno de desarrollo.
        #
        # En vez de adivinar una forma y dejarla fija —que es como se llegó al 400—,
        # la herramienta prueba en orden las formas que sus HERMANAS de la misma
        # familia ya tienen aceptadas (`equity-prints` usa `limit`, `dark-flow` usa
        # `aggregationPeriod`), se queda con la primera que el proveedor acepta y la
        # recuerda. El diagnóstico publica qué campo rechazó y qué forma funcionó,
        # así que si ninguna encaja, el Auditor dice exactamente qué pide.
        QuantDataTool(
            "dark_pool_levels", "Dark Pool / Equities", "Niveles de dark pool",
            ("/v1/equities/tool/dark-pool-levels",),
            _dark_pool_levels_body,
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
    "Statistics": ["contract_statistics", "contract_trade_side_statistics", "market_share"],
    "Open Interest": ["max_pain", "max_pain_over_time", "oi_change", "oi_by_strike", "oi_by_expiration", "oi_over_time"],
    "Volatility Analysis": ["volatility_drift", "iv_rank", "volatility_skew", "term_structure"],
}


def is_missing_tool_error(message: str) -> bool:
    """¿El error significa "esta herramienta no existe" y no un fallo transitorio?"""
    m = str(message or "").lower()
    if "http 404" in m or "http 400" in m or "http 405" in m or "http 422" in m:
        return True
    return "not found" in m or "unknown tool" in m or "no such" in m


# Estados de una herramienta frente al proveedor. Confundirlos fue lo que metió a
# `dark-pool-levels` en un ciclo de reintentos de algo que nunca iba a cambiar.
STATUS_REQUEST_INVALID = "REQUEST_INVALID"   # 400 · el cuerpo está mal → reparable
STATUS_NO_DATA = "NO_DATA"                   # 422 · petición válida, sin datos
STATUS_MISSING_TOOL = "MISSING_TOOL"         # 404 · la herramienta no existe aquí
STATUS_PROVIDER_ERROR = "PROVIDER_ERROR"     # 5xx · fallo temporal del proveedor
STATUS_TRANSIENT = "TRANSIENT"               # red, timeout, rate limit


def classify_provider_failure(exc):
    """Qué clase de fallo es, que decide qué hacer con él.

    v1.46.0 · Antes un 400 y un 422 caían en el mismo cajón que un timeout, así que
    una petición mal formada entraba en el ciclo de reintentos y se repetía
    indefinidamente sin que nada cambiara. Un 400 no se arregla esperando: se
    arregla corrigiendo el cuerpo, y si no se sabe corregir, diciéndolo.

    Y un 422 no es un fallo: la petición era válida y el proveedor no tiene datos
    para ella. Tratarlo como error hacía parecer rota una herramienta que funciona.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc or "").lower()
    if status == 400 or "http 400" in text:
        return STATUS_REQUEST_INVALID
    if status == 422 or "http 422" in text:
        return STATUS_NO_DATA
    if status in (404, 405) or "http 404" in text or "http 405" in text:
        return STATUS_MISSING_TOOL
    if isinstance(status, int) and 500 <= status < 600:
        return STATUS_PROVIDER_ERROR
    if "http 50" in text or "http 51" in text:
        return STATUS_PROVIDER_ERROR
    if "validation failed" in text or "request validation" in text:
        return STATUS_REQUEST_INVALID
    return STATUS_TRANSIENT


def is_validation_error(exc: Any) -> bool:
    """¿El proveedor rechaza el CUERPO, no la ruta?

    Un 400/422 de validación dice «la herramienta existe, tu petición no vale», que
    es reparable. Un 404 dice «no existe», que no lo es. Tratarlos igual —como se
    hacía— convertía un cuerpo corregible en una herramienta permanentemente
    DEGRADADA.
    """
    return classify_provider_failure(exc) == STATUS_REQUEST_INVALID


def _validation_detail(exc: Any) -> str:
    """Los campos que el proveedor nombró, o el mensaje si no nombró ninguno."""
    fields = getattr(exc, "validation_fields", None)
    if fields:
        return " · ".join(str(f) for f in fields[:6])[:400]
    return str(exc or "")[:400]


def route_diagnostic(tool: "QuantDataTool") -> Dict[str, Any]:
    """Qué le pasa a esta herramienta, en términos sobre los que se puede actuar."""
    canonical = tool.paths[0] if tool.paths else "—"
    if tool.route_state == ROUTE_OK or tool.last_success:
        detail = "ruta canónica confirmada por el proveedor"
        if tool.accepted_variant:
            detail += (f" · cuerpo reparado: se aceptó la variante "
                       f"#{tool.accepted_variant}")
        return {"state": ROUTE_OK, "route": tool.resolved_path or canonical,
                "detail": detail, "accepted_variant": tool.accepted_variant}
    if tool.validation_error and tool.accepted_variant is None:
        return {"state": "BODY_REJECTED", "route": canonical,
                "detail": (f"La ruta existe pero el proveedor rechaza el cuerpo: "
                           f"{tool.validation_error}. Se probaron "
                           f"{tool.variants_tried} formas."),
                "validation_error": tool.validation_error,
                "variants_tried": tool.variants_tried}
    if tool.route_state == ROUTE_INVALID:
        return {"state": ROUTE_INVALID, "route": canonical,
                "detail": (f"El proveedor rechaza {canonical}. O su plan no incluye esta "
                           "herramienta, o el proveedor la renombró y hay que actualizar "
                           "el registro canónico. No se prueban rutas alternativas.")}
    return {"state": ROUTE_UNKNOWN, "route": canonical,
            "detail": "todavía no se ha llamado a esta herramienta en este ciclo"}
