"""QFLOW · capa analítica sobre el flujo neto de opciones (v1.42.6).

QUÉ ES Y QUÉ NO ES
------------------
Quant Data publica `net-flow` (prima neta por intervalo, con `callSum`, `putSum` y
`stockPrice`) y `order-flow` (cada print, con su premium y su lado). Lo que NO
publica es un "QFLOW": ese nombre no existe en su documentación y no hay fórmula
oficial para un nivel horizontal. **El dato es del proveedor; el nivel lo calcula
ITM QUANT.** Decirlo al revés sería atribuirle a Quant Data un número que no emite.

Dos salidas distintas, que no hay que confundir:

1. **Serie QFLOW** — prima neta direccional ACUMULADA a lo largo de la sesión. Las
   barras que ya existen en el panel muestran el flujo *del intervalo*; la serie
   acumulada muestra hacia dónde se ha ido inclinando la sesión entera. Son
   preguntas distintas y por eso conviven: barra = ahora, línea = memoria.

2. **Nivel QFLOW** — el precio donde se concentra ese flujo. Cada intervalo trae su
   `stockPrice`, así que la prima negociada tiene una coordenada de precio. El nivel
   es la media de esos precios ponderada por |prima|, con más peso cuanto más
   reciente. No es el strike dominante (eso es estructura de cadena), ni el Net
   Drift, ni el Gamma Center, ni el Zero Gamma: es dónde estaba el SUBYACENTE
   mientras se pagaba la prima.

POR QUÉ PONDERAR POR |PRIMA| Y NO POR PRIMA NETA
-----------------------------------------------
El nivel responde "¿a qué precio se negoció el grueso del dinero?". Un intervalo con
2 M$ en calls y 2 M$ en puts tiene neto cero pero es un precio donde hubo enorme
actividad. Ponderar por el neto lo borraría; ponderar por la magnitud lo conserva.
La dirección ya la cuenta la serie acumulada.

ESTADOS
-------
Un fallo de datos nunca se convierte en 0.0. Cada salida declara por qué está vacía:

    DATA_OK           hay buckets utilizables
    NO_PROVIDER_DATA  el proveedor respondió sin filas
    FILTERED_ALL      había filas pero ninguna sobrevivió a la validación
    PROVIDER_ERROR    el proveedor falló o no está configurado
    PARSER_ERROR      la respuesta no tiene la forma esperada
    STALE             el último bucket es demasiado viejo para operar con él
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from .obs import note as _obs_note

DATA_OK = "DATA_OK"
NO_PROVIDER_DATA = "NO_PROVIDER_DATA"
FILTERED_ALL = "FILTERED_ALL"
PROVIDER_ERROR = "PROVIDER_ERROR"
PARSER_ERROR = "PARSER_ERROR"
STALE = "STALE"

# Un bucket de 1 minuto deja de ser accionable mucho antes que uno estructural.
STALE_AFTER_MINUTES = 20.0
# Una concentración es "extraordinaria" cuando supera este cuantil de la sesión…
EVENT_QUANTILE = 0.97
# …y además pesa al menos esta fracción del mayor intervalo del día. Sin el segundo
# filtro, una sesión plana marcaría como evento su propio ruido.
EVENT_MIN_SHARE = 0.25


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _parse_ts(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    raw = str(v or "").strip()
    if not raw:
        return None
    # `fromisoformat` ya acepta las formas que publica Quant Data (con T o con
    # espacio, con o sin segundos, con Z o con desfase), así que no hace falta una
    # cascada de formatos: una sola conversión y, si no encaja, se dice.
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        _obs_note("qflow:parse_ts", exc)
        return None
    # Un instante sin zona horaria comparado con uno que sí la tiene lanza
    # TypeError en tiempo de ejecución. El proveedor publica UTC.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _empty(state: str, detail: str, symbol: str) -> Dict[str, Any]:
    return {
        "ready": False, "state": state, "detail": detail,
        "symbol": str(symbol or "").upper(),
        "series": [], "level": None, "events": [],
        "net_premium": None, "call_premium": None, "put_premium": None,
        "buckets": 0, "last_bucket": None,
    }


def build_qflow(rows: Any, *, symbol: str, now: Optional[datetime] = None,
                provider_error: Optional[str] = None) -> Dict[str, Any]:
    """Serie y nivel QFLOW a partir de los buckets de `net-flow` de Quant Data.

    `rows` es la lista normalizada por `norm_time_series`: cada fila con `t`,
    `value` (prima neta), `call`, `put` y `stock_price`. No se asume ningún ticker:
    el símbolo sólo viaja en la salida para trazabilidad.
    """
    sym = str(symbol or "").upper()
    if provider_error:
        return _empty(PROVIDER_ERROR, str(provider_error)[:200], sym)
    if rows is None:
        return _empty(NO_PROVIDER_DATA, "el proveedor no devolvió serie", sym)
    if not isinstance(rows, (list, tuple)):
        return _empty(PARSER_ERROR, f"se esperaba una lista de buckets, llegó {type(rows).__name__}", sym)
    if not rows:
        return _empty(NO_PROVIDER_DATA, "el proveedor respondió sin buckets", sym)

    try:
        clean: List[Dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = _parse_ts(r.get("t") or r.get("timestamp"))
            if ts is None:
                continue
            net = _f(r.get("value"))
            call = _f(r.get("call"))
            put = _f(r.get("put"))
            if net is None and (call is not None or put is not None):
                net = (call or 0.0) - (put or 0.0)
            if net is None:
                continue
            gross = (abs(call) if call is not None else 0.0) + (abs(put) if put is not None else 0.0)
            if gross <= 0:
                gross = abs(net)
            clean.append({"ts": ts, "net": float(net), "call": call, "put": put,
                          "gross": float(gross), "price": _f(r.get("stock_price"))})
    except Exception as exc:
        _obs_note("qflow:parse", exc, severity="DEGRADED")
        return _empty(PARSER_ERROR, f"{type(exc).__name__}: {exc}"[:200], sym)

    if not clean:
        return _empty(FILTERED_ALL, f"{len(rows)} buckets recibidos, ninguno con instante y prima utilizables", sym)

    clean.sort(key=lambda x: x["ts"])
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    last_ts = clean[-1]["ts"]
    age_min = (ref - last_ts).total_seconds() / 60.0

    nets = np.array([c["net"] for c in clean], dtype=float)
    cum = np.cumsum(nets)
    series = [{"t": c["ts"].isoformat(), "net": round(c["net"], 4),
               "cumulative": round(float(v), 4), "price": c["price"]}
              for c, v in zip(clean, cum)]

    level = _dominant_level(clean)
    events = _concentration_events(clean)

    call_total = sum(c["call"] for c in clean if c["call"] is not None) or None
    put_total = sum(c["put"] for c in clean if c["put"] is not None) or None

    state = STALE if age_min > STALE_AFTER_MINUTES else DATA_OK
    detail = (f"último bucket hace {age_min:.0f} min" if state == STALE
              else f"{len(clean)} buckets de 1m")
    return {
        "ready": True, "state": state, "detail": detail, "symbol": sym,
        "series": series, "level": level, "events": events,
        "net_premium": round(float(cum[-1]), 4),
        "call_premium": None if call_total is None else round(float(call_total), 4),
        "put_premium": None if put_total is None else round(float(put_total), 4),
        "buckets": len(clean),
        "last_bucket": last_ts.isoformat(),
        "age_minutes": round(age_min, 2),
        "method": "QFLOW · ITM QUANT sobre net-flow de Quant Data (NET_PREMIUM · 1m)",
    }


def _dominant_level(clean: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Precio donde se concentra la prima, ponderado por magnitud y recencia.

    Peso = |prima del intervalo| × decaimiento exponencial hacia atrás. El
    decaimiento evita que el nivel quede clavado en la primera hora de la sesión
    cuando el dinero ya se mudó de precio; la media ponderada evita que un único
    intervalo lo secuestre.
    """
    pts = [(c["price"], c["gross"], c["ts"]) for c in clean
           if c["price"] is not None and c["price"] > 0 and c["gross"] > 0]
    if not pts:
        return None
    last_ts = pts[-1][2]
    # Vida media de 90 minutos: el flujo de hace hora y media pesa la mitad.
    half_life = 90.0
    prices = np.array([p for p, _, _ in pts], dtype=float)
    mass = np.array([g for _, g, _ in pts], dtype=float)
    ages = np.array([max(0.0, (last_ts - t).total_seconds() / 60.0) for _, _, t in pts], dtype=float)
    weights = mass * np.power(0.5, ages / half_life)
    total = float(weights.sum())
    if not (total > 0):
        return None
    price = float(np.sum(prices * weights) / total)
    # Dispersión: si la prima se repartió por todo el rango, el nivel es débil y hay
    # que decirlo en vez de publicarlo con la misma autoridad que uno concentrado.
    var = float(np.sum(weights * (prices - price) ** 2) / total)
    sd = math.sqrt(max(var, 0.0))
    rng = float(prices.max() - prices.min())
    concentration = 1.0 - min(1.0, (sd / rng)) if rng > 1e-9 else 1.0
    return {
        "price": round(price, 4),
        "concentration": round(concentration, 4),
        "dispersion": round(sd, 4),
        "price_low": round(float(prices.min()), 4),
        "price_high": round(float(prices.max()), 4),
        "weighted_premium": round(total, 2),
        "samples": int(len(pts)),
        "half_life_minutes": half_life,
        "note": ("Media de los precios del subyacente ponderada por |prima| de cada "
                 "intervalo, con decaimiento de 90 min. No es strike dominante, ni "
                 "Net Drift, ni Gamma Center, ni Zero Gamma."),
    }


def _concentration_events(clean: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Intervalos con una concentración de prima excepcional para la sesión."""
    gross = np.array([c["gross"] for c in clean], dtype=float)
    pos = gross[gross > 0]
    if pos.size < 5:
        return []
    cut = float(np.quantile(pos, EVENT_QUANTILE))
    peak = float(pos.max())
    floor = max(cut, peak * EVENT_MIN_SHARE)
    out = []
    for c in clean:
        if c["gross"] < floor or c["gross"] <= 0:
            continue
        out.append({
            "t": c["ts"].isoformat(),
            "premium": round(c["gross"], 2),
            "net": round(c["net"], 2),
            "side": "CALL" if c["net"] > 0 else "PUT" if c["net"] < 0 else "MIXED",
            "price": c["price"],
            "share_of_peak": round(c["gross"] / peak, 4) if peak > 0 else None,
        })
    out.sort(key=lambda e: -e["premium"])
    return out[:20]


__all__ = ["build_qflow", "DATA_OK", "NO_PROVIDER_DATA", "FILTERED_ALL",
           "PROVIDER_ERROR", "PARSER_ERROR", "STALE"]
