"""QFLOW · capa analítica sobre el flujo neto de opciones (v1.42.6).

QUÉ ES Y QUÉ NO ES
------------------
Quant Data publica `net-flow` (prima neta por intervalo, con `callSum`, `putSum` y
`stockPrice`) y `order-flow` (cada print, con su premium y su lado). Lo que NO
publica es un "QFLOW": ese nombre no existe en su documentación y no hay fórmula
oficial para un nivel horizontal. **El dato es del proveedor; el nivel lo calcula
ITM QUANT.** Decirlo al revés sería atribuirle a Quant Data un número que no emite.

Tres salidas distintas, que no hay que confundir:

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

3. **Atribución de la concentración** — qué OPERACIONES la produjeron. `net-flow`
   da la serie agregada por intervalo, y con eso solo no se puede responder «¿qué
   pasó en ese minuto?». La respuesta está en `order-flow/consolidated` y
   `order-flow/unconsolidated`, que traen cada operación con su CALL/PUT,
   BUY/SELL, strike, vencimiento, DTE, prima, agresor y tipo de ejecución
   (BLOCK · SWEEP · SPLIT). Sin ese cruce, QFLOW se quedaba en Net Flow y una
   concentración era un pico anónimo.

NORMALIZACIÓN POR ACTIVO
------------------------
No hay ningún umbral en dólares. «Concentración importante» se mide contra la
propia distribución del activo en su propia sesión (`asset_normalization`), con
tres lentes simultáneas —cuantil, fracción del pico y z robusta—, así que la misma
regla vale para un ETF enorme y para una acción mediana sin tocar una constante.

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

from .asset_normalization import concentration_threshold
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
        # La forma no cambia entre éxito y fallo: un consumidor que lea `markers`
        # sólo en el camino feliz tendría que distinguir «no hubo» de «no vino».
        "markers": [], "attribution": {"ready": False, "rows": 0},
        "threshold": None, "asset_scale": None,
    }


def build_qflow(rows: Any, *, symbol: str, now: Optional[datetime] = None,
                provider_error: Optional[str] = None,
                order_flow: Any = None,
                order_flow_tool: str = "") -> Dict[str, Any]:
    """Serie, nivel y CONCENTRACIONES ATRIBUIDAS de QFLOW.

    `rows` es la lista normalizada por `norm_time_series` de `net-flow`: cada fila
    con `t`, `value` (prima neta), `call`, `put` y `stock_price`. Es la serie base.

    `order_flow` son las filas normalizadas de `order-flow/consolidated` o
    `order-flow/unconsolidated`. Sirven para responder QUÉ operaciones produjeron
    cada concentración: CALL/PUT, BUY/SELL, strike, vencimiento, DTE, prima,
    agresor, BLOCK/SWEEP/SPLIT y número de operaciones. Sin ellas QFLOW se queda
    en Net Flow y un pico es un pico anónimo.

    No se asume ningún ticker: el símbolo sólo viaja en la salida y como escala de
    normalización del propio activo.
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
    threshold = concentration_threshold([c["gross"] for c in clean], symbol=sym)
    events = _concentration_events(clean, threshold)
    attribution = attribute_events(events, order_flow, tool=order_flow_tool)
    markers = _markers(events)

    call_total = sum(c["call"] for c in clean if c["call"] is not None) or None
    put_total = sum(c["put"] for c in clean if c["put"] is not None) or None

    state = STALE if age_min > STALE_AFTER_MINUTES else DATA_OK
    detail = (f"último bucket hace {age_min:.0f} min" if state == STALE
              else f"{len(clean)} buckets de 1m")
    return {
        "ready": True, "state": state, "detail": detail, "symbol": sym,
        "series": series, "level": level, "events": events,
        "markers": markers, "attribution": attribution,
        "threshold": threshold.to_dict(), "asset_scale": threshold.scale,
        "net_premium": round(float(cum[-1]), 4),
        "call_premium": None if call_total is None else round(float(call_total), 4),
        "put_premium": None if put_total is None else round(float(put_total), 4),
        "buckets": len(clean),
        "last_bucket": last_ts.isoformat(),
        "age_minutes": round(age_min, 2),
        "method": ("QFLOW · ITM QUANT sobre net-flow de Quant Data (NET_PREMIUM · 1m), "
                   "concentraciones atribuidas con order-flow del mismo proveedor"),
        "source_mode": "DERIVED",
        "inputs": ["QD_NET_FLOW", "QD_ORDER_FLOW_CONSOLIDATED",
                   "QD_ORDER_FLOW_UNCONSOLIDATED"],
        "normalization": "ASSET_RELATIVE_TRIPLE_GATE",
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


def _concentration_events(clean: List[Dict[str, Any]], threshold) -> List[Dict[str, Any]]:
    """Intervalos con una concentración excepcional PARA ESTE ACTIVO.

    El umbral no está escrito en dólares: lo mide `asset_normalization` sobre la
    propia distribución del activo con tres lentes a la vez —cuantil de sesión,
    fracción del pico y z robusta—. Así la misma regla detecta lo mismo en un ETF
    enorme y en una acción mediana, que es lo que hace que «funciona en DIA» deje
    de ser una certificación.
    """
    if not threshold.ready or threshold.floor is None:
        return []
    peak = threshold.scale.get("peak") or 0.0
    out = []
    for c in clean:
        if c["gross"] < threshold.floor or c["gross"] <= 0:
            continue
        out.append({
            "t": c["ts"].isoformat(),
            "premium": round(c["gross"], 2),
            "net": round(c["net"], 2),
            "side": "CALL" if c["net"] > 0 else "PUT" if c["net"] < 0 else "MIXED",
            "price": c["price"],
            "share_of_peak": round(c["gross"] / peak, 4) if peak > 0 else None,
            "asset_units": threshold.scale.get("unit") and round(
                c["gross"] / threshold.scale["unit"], 3),
            "robust_z": _robust_z(c["gross"], threshold.scale),
        })
    out.sort(key=lambda e: -e["premium"])
    return out[:20]


def _robust_z(value: float, scale: Dict[str, Any]) -> Optional[float]:
    median = scale.get("median")
    mad = scale.get("mad")
    if median is None or not mad:
        return None
    sigma = float(mad) * 1.4826
    if sigma <= 1e-12:
        return None
    return round((float(value) - float(median)) / sigma, 3)


def _markers(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Marcas para el gráfico de precio y para el panel de flujo.

    Una concentración de calls se marca ▲ y una de puts ▼, con la prima en
    millones. Es la misma marca en los dos gráficos a propósito: si el panel de
    flujo dice que hubo 4.2 M$ en calls a las 10:14, el gráfico de precio tiene que
    enseñar dónde estaba el precio en ese instante, o la lectura no se puede
    cerrar.
    """
    out = []
    for e in events:
        side = e.get("side")
        arrow = "▲" if side == "CALL" else "▼" if side == "PUT" else "◆"
        premium = float(e.get("premium") or 0.0)
        out.append({
            "t": e.get("t"), "price": e.get("price"), "side": side,
            "arrow": arrow, "premium": premium,
            "label": f"{arrow} ${premium / 1e6:.1f}M",
            "share_of_peak": e.get("share_of_peak"),
            "robust_z": e.get("robust_z"),
        })
    return out


# Ventana alrededor del instante del bucket dentro de la cual una operación se
# considera parte de esa concentración. Un bucket de net-flow es de 1 minuto; se
# admite medio minuto de holgura a cada lado porque los relojes del agregado y de
# la cinta no tienen por qué coincidir al milisegundo.
ATTRIBUTION_WINDOW_SECONDS = 90.0


def attribute_events(events: List[Dict[str, Any]], order_flow: Any,
                     *, tool: str = "") -> Dict[str, Any]:
    """QUÉ operaciones produjeron cada concentración.

    Cruza los instantes marcados por la serie de `net-flow` con las impresiones de
    `order-flow`. Para cada concentración devuelve las operaciones que cayeron en
    su ventana, con el contrato entero —CALL/PUT, BUY/SELL, strike, vencimiento,
    DTE, prima, agresor y tipo de ejecución (BLOCK · SWEEP · SPLIT)— y cuántas
    fueron.

    Si no hay order flow, se dice: `ready=False` con el motivo. No se rellena con
    las barras agregadas, que no saben de strikes ni de agresores.
    """
    if not events:
        return {"ready": False, "rows": 0, "tool": tool or None,
                "detail": "no hubo concentraciones que atribuir"}
    rows = [r for r in (order_flow or []) if isinstance(r, dict)]
    if not rows:
        return {"ready": False, "rows": 0, "tool": tool or None,
                "detail": ("sin cinta de opciones en este ciclo; la concentración queda "
                           "SIN ATRIBUIR en vez de atribuida a operaciones que "
                           "nadie vio")}

    parsed: List[Dict[str, Any]] = []
    for r in rows:
        ts = _parse_ts(r.get("t") or r.get("timestamp"))
        if ts is None:
            continue
        parsed.append({"ts": ts, "row": r})
    if not parsed:
        return {"ready": False, "rows": len(rows), "tool": tool or None,
                "detail": f"{len(rows)} operaciones recibidas, ninguna con instante utilizable"}
    parsed.sort(key=lambda x: x["ts"])

    detail: List[Dict[str, Any]] = []
    for e in events:
        et = _parse_ts(e.get("t"))
        if et is None:
            continue
        window = [p for p in parsed
                  if abs((p["ts"] - et).total_seconds()) <= ATTRIBUTION_WINDOW_SECONDS]
        if not window:
            detail.append({"t": e.get("t"), "trades": 0, "matched": False,
                           "detail": "sin operaciones en la ventana del bucket"})
            continue
        trades = [_trade(p["row"]) for p in window]
        trades.sort(key=lambda x: -(x.get("premium") or 0.0))
        calls = [t for t in trades if t["option_type"].startswith("C")]
        puts = [t for t in trades if t["option_type"].startswith("P")]
        buys = [t for t in trades if t["direction"] > 0]
        sells = [t for t in trades if t["direction"] < 0]
        by_exec: Dict[str, int] = {}
        for t in trades:
            tag = _execution_tag(t.get("execution"))
            if tag:
                by_exec[tag] = by_exec.get(tag, 0) + 1
        by_strike: Dict[float, float] = {}
        for t in trades:
            if t["strike"] is None:
                continue
            by_strike[t["strike"]] = by_strike.get(t["strike"], 0.0) + (t["premium"] or 0.0)
        dominant = max(by_strike.items(), key=lambda kv: abs(kv[1])) if by_strike else None
        detail.append({
            "t": e.get("t"), "matched": True, "trades": len(trades),
            "premium": round(sum(t["premium"] or 0.0 for t in trades), 2),
            "call_premium": round(sum(t["premium"] or 0.0 for t in calls), 2),
            "put_premium": round(sum(t["premium"] or 0.0 for t in puts), 2),
            "buy_premium": round(sum(t["premium"] or 0.0 for t in buys), 2),
            "sell_premium": round(sum(t["premium"] or 0.0 for t in sells), 2),
            "calls": len(calls), "puts": len(puts),
            "buys": len(buys), "sells": len(sells),
            "executions": by_exec,
            "dominant_strike": (None if dominant is None else
                                {"strike": dominant[0], "premium": round(dominant[1], 2)}),
            "top_trades": trades[:8],
        })

    matched = sum(1 for d in detail if d.get("matched"))
    return {
        "ready": bool(matched), "rows": len(parsed), "events": detail,
        "matched": matched, "tool": tool or None,
        "window_seconds": ATTRIBUTION_WINDOW_SECONDS,
        "source": "QUANTDATA_ORDER_FLOW",
        "detail": "" if matched else "ninguna concentración tuvo operaciones en su ventana",
    }


def _trade(r: Dict[str, Any]) -> Dict[str, Any]:
    """Una operación con el contrato entero, para poder explicar la concentración."""
    premium = _f(r.get("premium"))
    if premium is None:
        price, size = _f(r.get("price")), _f(r.get("size"))
        if price is not None and size is not None:
            premium = price * size * 100.0
    side = str(r.get("side") or "").upper()
    direction = r.get("direction")
    if not isinstance(direction, (int, float)):
        direction = 1 if side.startswith(("BUY", "ASK", "A")) else -1 if side.startswith(("SELL", "BID", "B")) else 0
    return {
        "t": r.get("t"),
        "option_type": str(r.get("option_type") or "").upper(),
        "strike": _f(r.get("strike")),
        "expiration": str(r.get("expiration") or ""),
        "dte": _f(r.get("dte")),
        "premium": premium,
        "size": _f(r.get("size")),
        "price": _f(r.get("price")),
        "side": side or "UNKNOWN",
        "direction": int(direction),
        "aggressor": ("BUY" if direction > 0 else "SELL" if direction < 0 else "UNKNOWN"),
        "execution": str(r.get("execution") or "").upper(),
        "trades": _f(r.get("trades")),
    }


_EXECUTION_TAGS = ("BLOCK", "SWEEP", "SPLIT", "CROSS", "MULTI")


def _execution_tag(raw: Any) -> Optional[str]:
    """BLOCK · SWEEP · SPLIT reconocidos dentro del texto que publique el proveedor.

    El campo llega con formas distintas según la cuenta (`SWEEP`, `INTERMARKET_SWEEP`,
    `Block Trade`…). Buscar la etiqueta DENTRO del texto reconoce las tres variantes
    sin fijar un vocabulario exacto que el proveedor no se ha comprometido a mantener.
    """
    text = str(raw or "").upper()
    if not text:
        return None
    for tag in _EXECUTION_TAGS:
        if tag in text:
            return tag
    return None


__all__ = ["build_qflow", "attribute_events", "DATA_OK", "NO_PROVIDER_DATA",
           "FILTERED_ALL", "PROVIDER_ERROR", "PARSER_ERROR", "STALE",
           "ATTRIBUTION_WINDOW_SECONDS"]
