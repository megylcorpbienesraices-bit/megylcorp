"""NET DRIFT OFICIAL · Quant Data · ITM QUANT v1.42.7

AUTORIDAD DEL DATO
------------------
La única fuente de Net Drift es el endpoint oficial del proveedor:

    POST /v1/options/tool/net-drift

Aquí NO se reconstruye Net Drift a partir de GEX, DEX, Net Flow, QFLOW, Gamma,
Delta Exposure ni de ninguna fórmula propia. Ninguna de esas magnitudes entra en
este módulo, ni siquiera como corrección o suavizado. Si el proveedor no entrega,
la salida dice SIN DATOS; no se rellena con una aproximación, porque una curva
aproximada es indistinguible de una real en pantalla y eso convierte un hueco de
datos en una señal falsa.

QUÉ ENTREGA LA API Y QUÉ CONSTRUYE ITM QUANT
--------------------------------------------
La API publica valores POR INTERVALO, no acumulados. Cada bucket trae:

    netCallPremium  netPutPremium  netCallVolume  netPutVolume
    midMarketCallPremium  midMarketPutPremium  stockPrice

ITM QUANT los ordena por instante y construye la curva ACUMULADA de la sesión.
Eso es todo lo que añade: una suma corrida. El valor de cada bucket se conserva
exactamente como llegó, con su signo.

SIGNO
-----
`netPutPremium` y `netPutVolume` llegan YA firmados por el proveedor. No se les
aplica ningún signo adicional. La prima neta del intervalo es `call + put`, no
`call - put`: restar un número que ya es negativo invertiría la dirección de la
sesión entera.

EL ÚLTIMO BUCKET
----------------
El bucket más reciente sigue ABIERTO mientras el intervalo no termina, y el
proveedor lo republica con valores mayores en cada refresco. Por eso la curva se
reconstruye ENTERA en cada ciclo desde la respuesta cruda, en lugar de ir sumando
lo nuevo sobre lo ya acumulado: sumar incrementalmente contaría el bucket abierto
tantas veces como refrescos hubiera. Dentro de una misma respuesta, si un instante
aparece repetido gana la última aparición, por el mismo motivo.

El bucket abierto se marca (`open`) y se publica además el acumulado CERRADO —el
que excluye ese bucket— para que se pueda distinguir lo consolidado de lo que
todavía se está formando.

SEPARACIÓN
----------
Net Drift no comparte estado, escala ni serie con GEX, DEX, Net Flow, QFLOW,
Gamma ni Delta Exposure. La salida lo declara explícitamente en `independent_of`.

ESTADOS
-------
Un fallo nunca se convierte en una curva de ceros:

    DATA_OK           hay buckets utilizables
    NO_PROVIDER_DATA  el proveedor respondió sin buckets  → SIN DATOS
    FILTERED_ALL      había filas, ninguna con instante utilizable
    PROVIDER_ERROR    el proveedor falló o no está configurado
    PARSER_ERROR      la respuesta no tiene la forma esperada
    STALE             el último bucket es demasiado viejo para operar con él
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .obs import note as _obs_note

DATA_OK = "DATA_OK"
NO_PROVIDER_DATA = "NO_PROVIDER_DATA"
FILTERED_ALL = "FILTERED_ALL"
PROVIDER_ERROR = "PROVIDER_ERROR"
PARSER_ERROR = "PARSER_ERROR"
STALE = "STALE"

# Etiqueta única que la interfaz muestra cuando no hay dato. Nunca un cero.
NO_DATA_LABEL = "SIN DATOS"

# Un bucket de 1 minuto deja de ser operable mucho antes que uno estructural.
STALE_AFTER_MINUTES = 20.0
# Ventana del intervalo. Mientras el último bucket esté dentro, sigue abierto.
BUCKET_SECONDS = 60.0

# Magnitudes con las que Net Drift NO se mezcla ni se compara en la misma escala.
INDEPENDENT_OF = ("GEX", "DEX", "NET_FLOW", "QFLOW", "GAMMA_EXPOSURE", "DELTA_EXPOSURE")

SOURCE = "QUANTDATA_NET_DRIFT_OFFICIAL"
ENDPOINT = "POST /v1/options/tool/net-drift"


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    raw = str(v or "").strip()
    if not raw:
        return None
    digits = raw[1:] if raw[:1] == "-" else raw
    if digits.isdigit():
        n = float(raw)
        try:
            return datetime.fromtimestamp(n / 1000.0 if abs(n) > 1e11 else n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            _obs_note("net_drift:epoch", exc)
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        _obs_note("net_drift:parse_ts", exc)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _empty(state: str, detail: str, symbol: str) -> Dict[str, Any]:
    """Salida vacía EXPLÍCITA: dice por qué no hay curva en vez de dibujar ceros."""
    return {
        "ready": False,
        "state": state,
        "detail": detail,
        "label": NO_DATA_LABEL,
        "symbol": str(symbol or "").upper(),
        "series": [],
        "buckets": 0,
        "last_bucket": None,
        "open_bucket": None,
        "cum_call_premium": None,
        "cum_put_premium": None,
        "cum_net_premium": None,
        "cum_call_volume": None,
        "cum_put_volume": None,
        "closed": None,
        "price_range": None,
        "source": SOURCE,
        "endpoint": ENDPOINT,
        "provider": "QUANT_DATA",
        "reconstructed": False,
        "independent_of": list(INDEPENDENT_OF),
    }


def build_net_drift(rows: Any, *, symbol: str, now: Optional[datetime] = None,
                    provider_error: Optional[str] = None) -> Dict[str, Any]:
    """Curva acumulada de sesión a partir de los buckets oficiales de Net Drift.

    `rows` es la lista que devuelve `norm_net_drift`. El ticker viaja sólo para
    trazabilidad: no condiciona ningún cálculo, ningún umbral y ninguna rama. Cualquier
    activo que el proveedor sirva se procesa por este mismo camino.
    """
    sym = str(symbol or "").upper()
    if provider_error:
        return _empty(PROVIDER_ERROR, str(provider_error)[:200], sym)
    if rows is None:
        return _empty(NO_PROVIDER_DATA, "el proveedor no devolvió serie de Net Drift", sym)
    if not isinstance(rows, (list, tuple)):
        return _empty(PARSER_ERROR, f"se esperaba una lista de buckets, llegó {type(rows).__name__}", sym)
    if not rows:
        return _empty(NO_PROVIDER_DATA, "el proveedor respondió sin buckets de Net Drift", sym)

    try:
        # Mapa instante -> bucket. Un instante repetido dentro de la misma respuesta
        # es el bucket abierto reenviado: gana el último, nunca se suman los dos.
        by_ts: Dict[datetime, Dict[str, Any]] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = _parse_ts(r.get("t") or r.get("timestamp") or r.get("timestamp_ms"))
            if ts is None:
                continue
            call = _f(r.get("net_call_premium")) or 0.0
            put = _f(r.get("net_put_premium")) or 0.0
            price = _f(r.get("stock_price"))
            if price is not None and price <= 0:
                price = None
            by_ts[ts] = {
                "ts": ts,
                "call": call,
                "put": put,
                "net": call + put,
                "call_volume": _f(r.get("net_call_volume")) or 0.0,
                "put_volume": _f(r.get("net_put_volume")) or 0.0,
                "mid_call": _f(r.get("mid_call_premium")),
                "mid_put": _f(r.get("mid_put_premium")),
                "price": price,
            }
    except Exception as exc:  # pragma: no cover - defensa de forma
        _obs_note("net_drift:parse", exc, severity="DEGRADED")
        return _empty(PARSER_ERROR, f"{type(exc).__name__}: {exc}"[:200], sym)

    if not by_ts:
        return _empty(FILTERED_ALL,
                      f"{len(rows)} buckets recibidos, ninguno con instante utilizable", sym)

    clean = [by_ts[k] for k in sorted(by_ts)]
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    last_ts = clean[-1]["ts"]
    age_s = (ref - last_ts).total_seconds()
    # El último bucket sigue formándose mientras no ha transcurrido su intervalo.
    # Un instante futuro (reloj adelantado del proveedor) también está abierto.
    open_bucket = age_s < BUCKET_SECONDS

    call_cum = put_cum = call_vol_cum = put_vol_cum = 0.0
    series: List[Dict[str, Any]] = []
    for i, c in enumerate(clean):
        call_cum += c["call"]
        put_cum += c["put"]
        call_vol_cum += c["call_volume"]
        put_vol_cum += c["put_volume"]
        series.append({
            "t": c["ts"].isoformat(),
            "timestamp_ms": int(c["ts"].timestamp() * 1000),
            "call": round(c["call"], 4),
            "put": round(c["put"], 4),
            "net": round(c["net"], 4),
            "call_volume": round(c["call_volume"], 4),
            "put_volume": round(c["put_volume"], 4),
            "mid_call": c["mid_call"],
            "mid_put": c["mid_put"],
            "price": c["price"],
            "cum_call": round(call_cum, 4),
            "cum_put": round(put_cum, 4),
            "cum_net": round(call_cum + put_cum, 4),
            "cum_call_volume": round(call_vol_cum, 4),
            "cum_put_volume": round(put_vol_cum, 4),
            "open": bool(open_bucket and i == len(clean) - 1),
        })

    # Acumulado consolidado: el mismo cálculo sin el bucket todavía abierto.
    closed = series[-2] if (open_bucket and len(series) > 1) else (None if open_bucket else series[-1])

    prices = [c["price"] for c in clean if c["price"] is not None]
    age_min = age_s / 60.0
    state = STALE if age_min > STALE_AFTER_MINUTES else DATA_OK
    detail = (f"último bucket hace {age_min:.0f} min" if state == STALE
              else f"{len(clean)} buckets de 1m · {'último abierto' if open_bucket else 'todos cerrados'}")

    return {
        "ready": True,
        "state": state,
        "detail": detail,
        "label": None,
        "symbol": sym,
        "series": series,
        "buckets": len(clean),
        "last_bucket": last_ts.isoformat(),
        "age_minutes": round(age_min, 2),
        "open_bucket": bool(open_bucket),
        "cum_call_premium": round(call_cum, 4),
        "cum_put_premium": round(put_cum, 4),
        "cum_net_premium": round(call_cum + put_cum, 4),
        "cum_call_volume": round(call_vol_cum, 4),
        "cum_put_volume": round(put_vol_cum, 4),
        # Acumulado sin el bucket abierto: lo que ya no puede cambiar.
        "closed": None if closed is None else {
            "t": closed["t"],
            "cum_call": closed["cum_call"],
            "cum_put": closed["cum_put"],
            "cum_net": closed["cum_net"],
        },
        "price_range": ({"low": min(prices), "high": max(prices),
                         "first": clean[0]["price"], "last": clean[-1]["price"]}
                        if prices else None),
        "price_points": len(prices),
        "source": SOURCE,
        "endpoint": ENDPOINT,
        "provider": "QUANT_DATA",
        "aggregation": "1m",
        "reconstructed": False,
        "independent_of": list(INDEPENDENT_OF),
        "method": ("Suma corrida de los buckets oficiales de Quant Data. "
                   "El valor por intervalo se conserva con su signo tal y como llega; "
                   "ITM QUANT sólo ordena y acumula."),
    }


def certify_against_raw(raw_rows: Any, built: Dict[str, Any], *,
                        tolerance: float = 1e-9) -> Dict[str, Any]:
    """¿El acumulado publicado coincide EXACTAMENTE con la respuesta cruda?

    Recalcula la suma desde `raw_rows` —las filas del proveedor, sin pasar por la
    curva— y la contrasta punto a punto contra lo que se publicó. Es la comprobación
    que exige la certificación: no basta con que la curva parezca razonable, tiene
    que ser matemáticamente idéntica a sumar el crudo.

    Devuelve el peor desvío encontrado y, si lo hay, el punto donde ocurre.
    """
    series = built.get("series") or []
    if not series:
        return {"ok": False, "reason": "sin serie publicada", "points": 0}

    by_ts: Dict[Any, Dict[str, Any]] = {}
    for r in raw_rows or []:
        if not isinstance(r, dict):
            continue
        ts = _parse_ts(r.get("t") or r.get("timestamp") or r.get("timestamp_ms"))
        if ts is not None:
            by_ts[ts] = r

    # La serie se publica redondeada a 4 decimales, así que un desvío de hasta medio
    # cuanto de redondeo (5e-5) es la propia publicación, no un error de cálculo.
    round_atol = 5e-5
    call = put = cvol = pvol = 0.0
    worst = 0.0
    worst_abs = 0.0
    worst_at = None
    checked = 0
    for ts in sorted(by_ts):
        r = by_ts[ts]
        call += _f(r.get("net_call_premium")) or 0.0
        put += _f(r.get("net_put_premium")) or 0.0
        cvol += _f(r.get("net_call_volume")) or 0.0
        pvol += _f(r.get("net_put_volume")) or 0.0
        point = next((p for p in series if p["t"] == ts.isoformat()), None)
        if point is None:
            return {"ok": False, "reason": f"la curva no publica el instante {ts.isoformat()}",
                    "points": checked}
        checked += 1
        for expected, key in ((call, "cum_call"), (put, "cum_put"), (call + put, "cum_net"),
                              (cvol, "cum_call_volume"), (pvol, "cum_put_volume")):
            got = _f(point.get(key)) or 0.0
            # El desvío se mide en términos relativos a la propia magnitud para que
            # la comparación signifique lo mismo con primas de 10 $ que de 10 M$, y
            # se admite además el cuanto de redondeo de la publicación.
            diff = abs(got - expected)
            dev = diff / max(1.0, abs(expected))
            if diff > worst_abs:
                worst_abs = diff
            if dev > worst:
                worst, worst_at = dev, f"{ts.isoformat()}·{key}"
    if checked != len(series):
        return {"ok": False, "reason": f"la curva publica {len(series)} puntos y el crudo tiene {checked}",
                "points": checked}
    ok = worst <= tolerance or worst_abs <= round_atol
    return {"ok": ok, "worst_relative_deviation": worst,
            "worst_absolute_deviation": worst_abs,
            "worst_at": None if ok else worst_at, "points": checked,
            "tolerance": tolerance, "rounding_atol": round_atol}


__all__ = ["build_net_drift", "certify_against_raw", "NO_DATA_LABEL", "INDEPENDENT_OF",
           "DATA_OK", "NO_PROVIDER_DATA", "FILTERED_ALL", "PROVIDER_ERROR",
           "PARSER_ERROR", "STALE"]
