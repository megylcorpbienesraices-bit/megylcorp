"""QUANT DATA HUB · ITM QUANT v1.43.0

LA RUTA
-------
    Quant Data → Data Hub → interfaz

Sin esperar a que el motor vuelva a procesar nada. Los datos oficiales del
proveedor —GEX, DEX, Vanna, Charm, Interés Abierto, Interval Map, Net Flow, Net
Drift, Order Flow, Dark Flow, Dark Pool Levels, IV/Skew/Term Structure— llegan a
la pantalla por este módulo, y **en paralelo** al motor cuantitativo, que recibe
exactamente los mismos datos como ENTRADA de su inteligencia propia.

POR QUÉ EN PARALELO Y NO EN SERIE
---------------------------------
Hasta v1.42.7 varias secciones sólo se pintaban con lo que el motor hubiera
terminado de calcular, y el dato del proveedor —ya descargado, ya normalizado—
esperaba su turno o no se usaba nunca. Eso producía dos síntomas a la vez: la
sección tardaba de más en tener números, y cuando el motor tenía su propia
versión la publicaba SIN DECIR que estaba tapando la del proveedor.

Aquí el reparto es explícito:

    QD_*    dato oficial del proveedor        → DIRECT_PROVIDER
    ITMQ_*  conclusión propia de ITM QUANT    → DERIVED

y el respaldo, cuando hace falta, va etiquetado FALLBACK y nunca disfrazado de
dato directo. Si Quant Data está sano para una métrica primaria, ningún cálculo
interno puede sustituirla en silencio: `data_lineage.guard_primary_source` lo
impide y deja constancia.

QUÉ NO HACE ESTE MÓDULO
-----------------------
No pide nada por red —los carriles de `providers/quantdata` ya lo hicieron—, no
inventa valores, no rellena huecos con ceros y no publica nombres de proveedor ni
de endpoint en la pantalla principal: esa información viaja en el bloque
`lineage`, que consume el Auditor.

MULTI-ACTIVO
------------
No hay ningún ticker escrito en este archivo. El símbolo activo entra por
parámetro y recorre siempre el mismo camino:

    ticker → capability check → Quant Data → normalización → motor → frontend
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import data_lineage as DL
from . import dark_pool_state
from .asset_normalization import normalize_matrix, asset_scale
from .data_lineage import (LINEAGE, DIRECT_PROVIDER, DERIVED, FALLBACK, UNAVAILABLE,
                           CAPABILITY_AVAILABLE, CAPABILITY_UNAVAILABLE,
                           DATA_OK, NO_PROVIDER_DATA, FILTERED_ALL, PROVIDER_ERROR,
                           PARSER_ERROR, STALE, NO_DATA_LABEL, QUANTDATA, ITM_QUANT)

# Un bloque del proveedor más viejo que esto ya no describe el mercado actual.
STALE_AFTER_SECONDS = {
    "FAST": 180.0,
    "MEDIUM": 900.0,
    "SLOW": 3600.0,
}

# Griegas del Interval Map que la interfaz puede alternar. GAMMA es el defecto
# porque es la que gobierna la cobertura, pero las cuatro son de primera clase.
INTERVAL_GREEKS: Tuple[str, ...] = ("GAMMA", "DELTA", "VANNA", "CHARM")

_INTERVAL_TOOL = {
    "GAMMA": ("interval_map_gamma", "QD_INTERVAL_MAP", "GEX"),
    "DELTA": ("interval_map_delta", "QD_INTERVAL_MAP", "DEX"),
    "VANNA": ("interval_map_vanna", "QD_INTERVAL_MAP", "VEX"),
    "CHARM": ("interval_map_charm", "QD_INTERVAL_MAP", "CHEX"),
}

# Herramienta del proveedor ↔ métrica canónica del registro de procedencia.
TOOL_METRIC: Dict[str, str] = {
    "gex_by_strike": "QD_GEX",
    "dex_by_strike": "QD_DEX",
    "vex_by_strike": "QD_VEX",
    "chex_by_strike": "QD_CHEX",
    "gex_by_expiration": "QD_GEX_BY_EXPIRATION",
    "dex_by_expiration": "QD_DEX_BY_EXPIRATION",
    "vex_by_expiration": "QD_VEX_BY_EXPIRATION",
    "chex_by_expiration": "QD_CHEX_BY_EXPIRATION",
    "net_flow": "QD_NET_FLOW",
    "net_drift": "QD_NET_DRIFT",
    "options_order_flow": "QD_ORDER_FLOW_CONSOLIDATED",
    "options_order_flow_raw": "QD_ORDER_FLOW_UNCONSOLIDATED",
    "oi_by_strike": "QD_OPEN_INTEREST_BY_STRIKE",
    "oi_by_expiration": "QD_OPEN_INTEREST_BY_EXPIRATION",
    "oi_change": "QD_OPEN_INTEREST_CHANGE",
    "oi_over_time": "QD_OPEN_INTEREST_OVER_TIME",
    "max_pain": "QD_MAX_PAIN",
    "max_pain_over_time": "QD_MAX_PAIN_OVER_TIME",
    "iv_rank": "QD_IV_RANK",
    "volatility_skew": "QD_VOLATILITY_SKEW",
    "term_structure": "QD_TERM_STRUCTURE",
    "volatility_drift": "QD_VOLATILITY_DRIFT",
    "dark_flow": "QD_DARK_FLOW",
    "dark_pool_levels": "QD_DARK_POOL_LEVELS",
    "equity_prints": "QD_EQUITY_PRINTS",
    "contract_statistics": "QD_CONTRACT_STATISTICS",
    "contract_trade_side_statistics": "QD_TRADE_SIDE_STATISTICS",
    "market_share": "QD_MARKET_SHARE",
    "gainers_losers": "QD_GAINERS_LOSERS",
    "interval_map_gamma": "QD_INTERVAL_MAP",
    "interval_map_delta": "QD_INTERVAL_MAP",
    "interval_map_vanna": "QD_INTERVAL_MAP",
    "interval_map_charm": "QD_INTERVAL_MAP",
}


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _age_seconds(block: Dict[str, Any]) -> Optional[float]:
    direct = _f(block.get("age_seconds"))
    if direct is not None:
        return direct
    raw = block.get("fetched_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def classify(block: Any, *, cadence: str = "MEDIUM",
             rows_key: str = "rows") -> Dict[str, Any]:
    """En qué estado está un bloque del proveedor, sin convertirlo nunca en cero.

    Ésta es la función que impide el síntoma que la especificación prohíbe
    expresamente: `$0.0` en pantalla cuando lo que hubo fue un fallo de datos. Un
    cero legítimo exige `DATA_OK`; cualquier otro estado publica SIN DATOS.

    ═══════════════════════════════════════════════════════════════════════
    v1.57.0 · UN FALLO DE LLAMADA NO BORRA LAS FILAS QUE SÍ ESTÁN
    ═══════════════════════════════════════════════════════════════════════

    La rama de error devolvía `rows: 0` SIEMPRE. Literalmente:

        if err:
            return {"state": PROVIDER_ERROR, ..., "rows": 0, ...}

    Así que un canal que había traído 216 filas y luego agotó su plazo en el
    ciclo siguiente se publicaba como «PROVIDER_ERROR · 0 filas», y quien
    mirara el Auditor veía a la vez el error y las 216 filas contadas en otro
    sitio: dos afirmaciones contradictorias sobre el mismo carril.

    Peor que la contradicción: al decir cero, ningún consumidor aguas abajo
    podía decidir usar el último valor bueno, porque desde su punto de vista no
    había nada que usar.

    Ahora el recuento es SIEMPRE el real. El estado dice qué pasó en esta
    llamada; las filas dicen qué hay. Son dos hechos distintos y no se tapan
    uno al otro.

    ═══════════════════════════════════════════════════════════════════════
    CAPACIDAD ≠ EJECUCIÓN
    ═══════════════════════════════════════════════════════════════════════

    `capability` responde «¿existe este endpoint y estamos autorizados?».
    `state` responde «¿qué pasó en ESTA llamada?». Un endpoint perfectamente
    disponible puede fallar ahora mismo, y un fallo de ahora no lo convierte en
    inexistente.
    """
    if block is None:
        return {"state": NO_PROVIDER_DATA, "detail": "el proveedor no publicó este bloque",
                "rows": 0, "age_seconds": None,
                "capability": CAPABILITY_UNAVAILABLE, "has_payload": False}
    if not isinstance(block, dict):
        return {"state": PARSER_ERROR,
                "detail": f"se esperaba un objeto, llegó {type(block).__name__}",
                "rows": 0, "age_seconds": None,
                "capability": CAPABILITY_UNAVAILABLE, "has_payload": False}
    age = _age_seconds(block)
    rows = block.get(rows_key)
    n = len(rows) if isinstance(rows, (list, tuple)) else None

    err = block.get("error")
    if err:
        # El endpoint existe y respondía: lo que falló es ESTA llamada. Las
        # filas que hubiera se cuentan, para que aguas abajo se pueda decidir
        # mostrar el último valor bueno en vez de vaciar la sección.
        return {"state": PROVIDER_ERROR, "detail": str(err)[:200],
                "rows": n or 0, "age_seconds": age,
                "capability": CAPABILITY_AVAILABLE, "has_payload": bool(n)}

    if not block.get("ready"):
        reason = block.get("reason") or block.get("detail")
        if n:
            return {"state": FILTERED_ALL,
                    "detail": str(reason or f"{n} filas recibidas, ninguna utilizable")[:200],
                    "rows": n, "age_seconds": age,
                    "capability": CAPABILITY_AVAILABLE, "has_payload": True}
        # El proveedor respondió BIEN y no había actividad. Eso no es un fallo
        # suyo ni nuestro: es un mercado sin operaciones en esa ventana.
        return {"state": NO_PROVIDER_DATA,
                "detail": str(reason or "el proveedor respondió sin filas")[:200],
                "rows": 0, "age_seconds": age,
                "capability": CAPABILITY_AVAILABLE, "has_payload": False}

    limit = STALE_AFTER_SECONDS.get(str(cadence).upper(), STALE_AFTER_SECONDS["MEDIUM"])
    if age is not None and age > limit:
        return {"state": STALE, "detail": f"último dato hace {age:.0f} s (límite {limit:.0f} s)",
                "rows": n or 0, "age_seconds": age,
                "capability": CAPABILITY_AVAILABLE, "has_payload": bool(n)}
    return {"state": DATA_OK, "detail": "", "rows": n if n is not None else 0,
            "age_seconds": age,
            "capability": CAPABILITY_AVAILABLE, "has_payload": bool(n)}


_CLOSED_PHASES = ("WEEKEND", "HOLIDAY", "MARKET_CLOSED")


def _market_is_open() -> Optional[bool]:
    """¿Hay actividad bursátil posible ahora? `None` si el calendario no responde.

    El `None` es deliberado. Afirmar «mercado cerrado» sin saberlo convertiría un
    fallo técnico en una explicación tranquilizadora y falsa, que es justo lo que
    esta versión persigue eliminar. Premarket y afterhours cuentan como abierto:
    la cinta de equity imprime fuera de bolsa a esas horas.
    """
    try:
        from . import session_resolver
        return session_resolver.resolve().phase not in _CLOSED_PHASES
    except Exception:
        from .obs import note as _obs_note
        _obs_note("quant_data_hub:session_calendar", None, severity="DEGRADED")
        return None


def _rows(intel: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    block = intel.get(key) if isinstance(intel, dict) else None
    if not isinstance(block, dict) or not block.get("ready"):
        return []
    rows = block.get("rows")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _block(intel: Dict[str, Any], key: str) -> Dict[str, Any]:
    b = intel.get(key) if isinstance(intel, dict) else None
    return b if isinstance(b, dict) else {}


def _envelope(metric: str, symbol: str, *, tool: str, block: Dict[str, Any],
              cadence: str, source_mode: str, payload: Dict[str, Any],
              fallback_used: bool = False, derivation: str = "") -> Dict[str, Any]:
    """Sobre común: dato + estado + procedencia, con la procedencia fuera de la vista.

    `lineage` no se pinta en la pantalla principal. Viaja para que el Auditor pueda
    responder «¿de dónde salió este número?» sin que la terminal se llene de
    nombres de endpoints.
    """
    cls = classify(block, cadence=cadence)
    state = cls["state"] if source_mode != DERIVED else (
        DATA_OK if payload.get("ready") else cls["state"])
    plan = DL.plan_for(metric)
    LINEAGE.record(metric, symbol, source_mode=source_mode, state=state,
                   provider=(plan.provider if plan else QUANTDATA),
                   endpoint=(block.get("path") or (plan.endpoint if plan else "")),
                   raw_value={"tool": tool, "rows": cls["rows"]},
                   normalized_value=cls["rows"],
                   final_value=payload.get("summary", payload.get("ready")),
                   fallback_used=fallback_used, derivation=derivation,
                   detail=cls["detail"], age_seconds=cls["age_seconds"],
                   rows=cls["rows"])
    out = dict(payload)
    out.update({
        "ready": bool(payload.get("ready")),
        "state": state,
        "source_mode": source_mode,
        "fallback_used": bool(fallback_used),
        "detail": cls["detail"] or payload.get("detail") or "",
        "display": None if payload.get("ready") else NO_DATA_LABEL,
        "lineage": {
            "metric": metric, "symbol": str(symbol).upper(),
            "provider": (plan.provider if plan else QUANTDATA),
            "endpoint": block.get("path") or (plan.endpoint if plan else ""),
            "source_mode": source_mode, "state": state,
            "age_seconds": cls["age_seconds"], "rows": cls["rows"],
            "fallback_used": bool(fallback_used), "derivation": derivation,
        },
    })
    return out


# ═══════════════════════════════════════════════════════ capability check

def capabilities(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    """Qué sirve Quant Data PARA ESTE activo, antes de pedirle nada más.

    El mismo pipeline tiene que valer para cualquier activo soportado, y eso
    empieza por no asumir que todas las herramientas existen para todos. Un ETF
    muy líquido puede tener Interval Map y Dark Flow; un subyacente pequeño puede
    no tener ninguno de los dos. Saberlo por adelantado es la diferencia entre
    «esta sección no aplica a este activo» y «esta sección está rota».
    """
    sym = str(symbol or "").upper()
    caps: Dict[str, Any] = {}
    for tool, metric in TOOL_METRIC.items():
        block = _block(intel, tool)
        cls = classify(block)
        caps[tool] = {
            "metric": metric,
            "available": cls["state"] == DATA_OK,
            "state": cls["state"],
            "rows": cls["rows"],
            "age_seconds": cls["age_seconds"],
        }
    available = sorted(k for k, v in caps.items() if v["available"])
    return {
        "symbol": sym,
        "tools": caps,
        "available": available,
        "unavailable": sorted(k for k, v in caps.items() if not v["available"]),
        "coverage_pct": round(100.0 * len(available) / max(1, len(caps)), 1),
        "provider_healthy": bool(available),
        "note": ("Comprobación de capacidad por activo. El mismo pipeline recorre "
                 "cualquier símbolo; lo que cambia es qué herramientas responden."),
    }


def provider_healthy_for(intel: Dict[str, Any], *tools: str) -> bool:
    """¿Está sana la fuente primaria de estas herramientas AHORA MISMO?

    Es la condición que decide si un respaldo propio sería un fallback legítimo o
    una sustitución silenciosa.
    """
    for tool in tools:
        if classify(_block(intel, tool))["state"] == DATA_OK:
            return True
    return False


# ═══════════════════════════════════════════════════════ INTERVAL MAP · TRACE

def interval_map(symbol: str, intel: Dict[str, Any], greek: str = "GAMMA", *,
                 engine_heatmap: Optional[Dict[str, Any]] = None,
                 price: Optional[List[Dict[str, Any]]] = None,
                 max_strikes: int = 90, max_times: int = 160) -> Dict[str, Any]:
    """Mapa de calor DINÁMICO de TRACE: eje X tiempo, eje Y strike, intensidad exposición.

    Fuente primaria: `interval-map` de Quant Data, con una matriz por griega
    (GAMMA · DELTA · VANNA · CHARM) que la interfaz puede alternar. Es lo que
    permite ver cómo las exposiciones aparecen, aumentan, disminuyen y migran
    durante la sesión; un perfil por strike no puede contar eso.

    El cálculo propio del motor (`heatmap_history`) se conserva como FALLBACK
    declarado para cuando el proveedor no sirve la herramienta de ese activo.
    Nunca tapa al proveedor cuando el proveedor está sano: eso queda prohibido por
    `guard_primary_source`, que falla en vez de avisar.

    La intensidad se normaliza con la escala del PROPIO activo (percentil 95 de
    |valor| sobre toda la matriz), así que el mapa se lee igual en cualquier
    símbolo sin ningún umbral en dólares escrito en el código.
    """
    sym = str(symbol or "").upper()
    key = str(greek or "GAMMA").upper()
    if key not in _INTERVAL_TOOL:
        key = "GAMMA"
    tool, metric, label = _INTERVAL_TOOL[key]
    block = _block(intel, tool)
    if not block and key == "GAMMA":
        # La clave antigua sigue viva para cuentas que aún la publiquen.
        block = _block(intel, "options_heat_map")
        tool = "options_heat_map"
    cls = classify(block, cadence="MEDIUM")

    # Un bloque puede llegar con el payload crudo y sin ejes normalizados: es lo que
    # ocurre con las cuentas que todavía publican la clave antigua, y con cualquier
    # consumidor que guarde la respuesta antes de pasarla por el normalizador. Se
    # normaliza aquí en vez de declarar SIN DATOS con el dato delante.
    if block and not block.get("strikes") and isinstance(block.get("raw"), (dict, list)):
        from ..providers.quantdata.tools import norm_interval_map
        try:
            block = {**block, **norm_interval_map(block["raw"], key)}
        except Exception as exc:  # payload con una forma que el normalizador no cubre
            from .obs import note as _obs_note
            _obs_note("quant_data_hub:interval_map_normalize", exc, severity="DEGRADED")
            block = {**block, "ready": False, "reason": f"{type(exc).__name__}"}
        cls = classify(block, cadence="MEDIUM")

    if cls["state"] == DATA_OK and block.get("strikes") and block.get("times"):
        strikes = [float(k) for k in block["strikes"] if _f(k) is not None]
        times = [str(t) for t in block["times"] if t]
        matrix = block.get("matrix") or []
        call_m = block.get("call_matrix") or []
        put_m = block.get("put_matrix") or []
        strikes, times, matrix, call_m, put_m = _trim(
            strikes, times, matrix, call_m, put_m,
            max_strikes=max_strikes, max_times=max_times)
        norm = normalize_matrix(matrix, symbol=sym)
        payload = {
            "ready": bool(strikes and times),
            "greek": key, "label": label, "source": "QUANTDATA",
            "source_detail": "QUANTDATA_INTERVAL_MAP",
            "strikes": strikes, "times": times,
            "times_ms": _trim_times_ms(block.get("times_ms"), block.get("times"), times),
            "matrix": matrix, "call_matrix": call_m, "put_matrix": put_m,
            "intensity": norm.get("matrix") or [],
            "intensity_scale": norm.get("scale"),
            "normalization": norm.get("normalization"),
            "available_greeks": list(INTERVAL_GREEKS),
            "price": list(price or []),
            "cells": len(strikes) * len(times),
            "axis": {"x": "TIME", "y": "STRIKE", "intensity": "EXPOSURE_MAGNITUDE"},
            "summary": {"strikes": len(strikes), "times": len(times)},
            "note": ("Interval Map del proveedor: cómo se mueve la exposición durante "
                     "la sesión, no una foto del final."),
        }
        # Se conserva como última sesión válida: es lo que sostiene el fondo de
        # TRACE cuando el mercado cierra.
        try:
            from .data_hub_runtime import HUB_RUNTIME
            HUB_RUNTIME.lkg.put(f"interval_map:{key}", sym, payload)
        except Exception as exc:   # noqa: BLE001 — guardar el respaldo nunca falla la vista
            from .obs import note as _obs_note
            _obs_note("quant_data_hub:interval_map_lkg", exc, severity="DEGRADED")
        return _envelope(metric, sym, tool=tool, block=block, cadence="MEDIUM",
                         source_mode=DIRECT_PROVIDER, payload=payload)

    # ── sin dato fresco del proveedor: dos respaldos, en este orden ──────────
    #
    # 1 · La matriz del MOTOR, si tiene historia de ESTA sesión. Es un cálculo
    #     propio —va marcado FALLBACK— pero describe lo que está pasando ahora.
    # 2 · La ÚLTIMA SESIÓN VÁLIDA del proveedor. Mercado cerrado o fin de semana
    #     explica que no haya datos nuevos; no explica que TRACE se quede sin
    #     fondo. El posicionamiento del viernes sigue describiendo el libro del
    #     sábado, y va marcado por su edad.
    #
    # El orden importa: un dato propio de ahora vale más que uno ajeno de hace dos
    # días, y al revés cuando no hay nada de ahora. Lo que no vale nunca es dejar
    # el gráfico vacío teniendo cualquiera de los dos.
    healthy = cls["state"] == DATA_OK
    DL.guard_primary_source(metric, sym, publishing_mode=FALLBACK,
                            provider_healthy=healthy,
                            detail="interval map", strict=False)
    engine = _engine_interval_map(sym, engine_heatmap or {}, key, label, price or [])
    if engine.get("ready"):
        return _envelope(metric, sym, tool=tool, block=block, cadence="MEDIUM",
                         source_mode=FALLBACK, payload=engine, fallback_used=True,
                         derivation="heatmap_history del motor (respaldo declarado)")

    if cls["state"] in (STALE, NO_PROVIDER_DATA, PROVIDER_ERROR):
        from .data_hub_runtime import HUB_RUNTIME
        lkg = HUB_RUNTIME.lkg.read(f"interval_map:{key}", sym,
                                   accept=("FRESH", "DEGRADED", "STALE"))
        cached = lkg.get("payload") if lkg.get("payload") else None
        if isinstance(cached, dict) and cached.get("ready") and cached.get("strikes"):
            payload = dict(cached)
            payload["last_known_good"] = True
            payload["age_seconds"] = lkg.get("age_seconds")
            payload["session_note"] = ("última sesión válida · el dato no es de ahora "
                                       "y se marca como tal")
            return _envelope(metric, sym, tool=tool, block=block, cadence="MEDIUM",
                             source_mode=DIRECT_PROVIDER, payload=payload,
                             fallback_used=False,
                             derivation="última sesión válida del proveedor")

    empty = {
        "ready": False, "greek": key, "label": label, "strikes": [], "times": [],
        "matrix": [], "call_matrix": [], "put_matrix": [], "intensity": [],
        "price": list(price or []), "available_greeks": list(INTERVAL_GREEKS),
        # El motivo canónico es lo que consume la interfaz; el detalle del
        # proveedor viaja aparte para no perderlo.
        "reason": "SIN INTERVAL MAP DEL PROVEEDOR NI HISTORIA DEL MOTOR",
        "provider_detail": cls["detail"] or None,
        "payload_keys": block.get("payload_keys"),
        "summary": None,
    }
    return _envelope(metric, sym, tool=tool, block=block, cadence="MEDIUM",
                     source_mode=UNAVAILABLE, payload=empty)


_ENGINE_FIELD = {"GAMMA": ("gamma_m", "gamma_intensity"),
                 "DELTA": ("delta_m", "delta_intensity"),
                 "CHARM": ("charm_m", "charm_intensity"),
                 # El motor no publica una matriz de VANNA en su heatmap: decirlo es
                 # mejor que devolver la de gamma con otra etiqueta.
                 "VANNA": (None, None)}


def _engine_interval_map(symbol: str, hh: Dict[str, Any], greek: str, label: str,
                         price: List[Dict[str, Any]]) -> Dict[str, Any]:
    field, intensity_field = _ENGINE_FIELD.get(greek, (None, None))
    if not field or not isinstance(hh, dict) or hh.get("ready") is not True:
        return {"ready": False}
    strikes = [float(k) for k in (hh.get("strikes") or []) if _f(k) is not None]
    times = [str(t) for t in (hh.get("times") or []) if t]
    grid = hh.get(field)
    if not (strikes and times and isinstance(grid, list) and grid):
        return {"ready": False}
    matrix: List[List[float]] = []
    for row in grid[:len(strikes)]:
        vals = [(_f(v, 0.0) or 0.0) * 1e6 for v in (row if isinstance(row, list) else [])][:len(times)]
        vals += [0.0] * (len(times) - len(vals))
        matrix.append(vals)
    while len(matrix) < len(strikes):
        matrix.append([0.0] * len(times))
    intensity = hh.get(intensity_field)
    if not (isinstance(intensity, list) and intensity):
        intensity = normalize_matrix(matrix, symbol=symbol).get("matrix") or []
    return {
        "ready": True, "greek": greek, "label": label,
        "source": "ITM_QUANT", "source_detail": "ITM_QUANT_ENGINE_HEATMAP",
        "strikes": strikes, "times": times,
        "times_ms": [], "matrix": matrix, "call_matrix": [], "put_matrix": [],
        "intensity": intensity, "price": price,
        "available_greeks": list(INTERVAL_GREEKS),
        "cells": len(strikes) * len(times),
        "axis": {"x": "TIME", "y": "STRIKE", "intensity": "EXPOSURE_MAGNITUDE"},
        "summary": {"strikes": len(strikes), "times": len(times)},
        "note": ("Respaldo declarado: matriz del motor propio porque el proveedor no "
                 "sirve el Interval Map de este activo ahora mismo."),
    }


def _trim(strikes: List[float], times: List[str], *grids: Any,
          max_strikes: int, max_times: int):
    """Recorta a lo que cabe en pantalla SIN desalinear ninguna fila.

    Los strikes se recortan por el centro (donde está el precio) y los instantes
    por la cola (lo reciente). Recortar una matriz por un lado y sus ejes por otro
    es la forma silenciosa de que el mapa mienta.
    """
    si, sj = 0, len(strikes)
    if len(strikes) > max_strikes:
        mid = len(strikes) // 2
        half = max_strikes // 2
        si, sj = max(0, mid - half), max(0, mid - half) + max_strikes
    ti = max(0, len(times) - max_times)
    out_grids = []
    for g in grids:
        rows = g if isinstance(g, list) else []
        out_grids.append([[*(r[ti:] if isinstance(r, list) else [])] for r in rows[si:sj]])
    return strikes[si:sj], times[ti:], *out_grids


def _trim_times_ms(times_ms: Any, all_times: Any, kept: List[str]) -> List[Any]:
    if not isinstance(times_ms, list) or not isinstance(all_times, list):
        return []
    lookup = {str(t): ms for t, ms in zip(all_times, times_ms)}
    return [lookup.get(t) for t in kept]


# ═══════════════════════════════════════════════════════ EXPOSICIÓN

_EXPOSURE_STRIKE = (("gex_by_strike", "gex", "QD_GEX"),
                    ("dex_by_strike", "dex", "QD_DEX"),
                    ("vex_by_strike", "vex", "QD_VEX"),
                    ("chex_by_strike", "chex", "QD_CHEX"))

_EXPOSURE_EXPIRY = (("gex_by_expiration", "gex", "QD_GEX_BY_EXPIRATION"),
                    ("dex_by_expiration", "dex", "QD_DEX_BY_EXPIRATION"),
                    ("vex_by_expiration", "vex", "QD_VEX_BY_EXPIRATION"),
                    ("chex_by_expiration", "chex", "QD_CHEX_BY_EXPIRATION"))


def exposure_by_strike(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    """GEX · DEX · VEX · CHEX por strike, del proveedor.

    `Exposure by Strike` con `greekMode` GAMMA/DELTA/VANNA/CHARM es la autoridad de
    estas cuatro magnitudes. El perfil del motor no desaparece: queda disponible
    para auditoría y como respaldo declarado, pero ya no puede taparlo.
    """
    sym = str(symbol or "").upper()
    merged: Dict[float, Dict[str, Any]] = {}
    present: List[str] = []
    for tool, field, metric in _EXPOSURE_STRIKE:
        block = _block(intel, tool)
        cls = classify(block, cadence="FAST")
        rows = _rows(intel, tool)
        LINEAGE.record(metric, sym, source_mode=(DIRECT_PROVIDER if rows else UNAVAILABLE),
                       state=cls["state"], endpoint=block.get("path"),
                       raw_value={"tool": tool}, normalized_value=len(rows),
                       final_value=len(rows), detail=cls["detail"],
                       age_seconds=cls["age_seconds"], rows=len(rows))
        if not rows:
            continue
        present.append(field)
        for r in rows:
            k = _f(r.get("strike"))
            if k is None:
                continue
            slot = merged.setdefault(k, {"strike": k, "source": "QUANTDATA"})
            slot[field] = _f(r.get("value"), 0.0)
            call, put = _f(r.get("call")), _f(r.get("put"))
            if call is not None:
                slot[f"call_{field}"] = call
            if put is not None:
                slot[f"put_{field}"] = put

    rows_out = sorted(merged.values(), key=lambda x: x["strike"])
    payload = {
        "ready": bool(rows_out), "rows": rows_out, "fields": present,
        "count": len(rows_out), "source": "QUANTDATA_EXPOSURE_BY_STRIKE",
        "summary": {"strikes": len(rows_out), "fields": present},
    }
    ref = _block(intel, "gex_by_strike")
    return _envelope("QD_GEX", sym, tool="gex_by_strike", block=ref, cadence="FAST",
                     source_mode=(DIRECT_PROVIDER if rows_out else UNAVAILABLE),
                     payload=payload)


def exposure_by_expiration(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    sym = str(symbol or "").upper()
    merged: Dict[str, Dict[str, Any]] = {}
    present: List[str] = []
    for tool, field, metric in _EXPOSURE_EXPIRY:
        rows = _rows(intel, tool)
        block = _block(intel, tool)
        cls = classify(block, cadence="MEDIUM")
        LINEAGE.record(metric, sym, source_mode=(DIRECT_PROVIDER if rows else UNAVAILABLE),
                       state=cls["state"], endpoint=block.get("path"),
                       normalized_value=len(rows), final_value=len(rows),
                       detail=cls["detail"], rows=len(rows))
        if not rows:
            continue
        present.append(field)
        for r in rows:
            exp = str(r.get("expiration") or "")
            if not exp:
                continue
            slot = merged.setdefault(exp, {"expiration": exp, "source": "QUANTDATA"})
            slot[field] = _f(r.get("value"), 0.0)
    rows_out = sorted(merged.values(), key=lambda x: x["expiration"])
    payload = {"ready": bool(rows_out), "rows": rows_out, "fields": present,
               "count": len(rows_out), "source": "QUANTDATA_EXPOSURE_BY_EXPIRATION",
               "summary": {"expirations": len(rows_out)}}
    return _envelope("QD_GEX_BY_EXPIRATION", sym, tool="gex_by_expiration",
                     block=_block(intel, "gex_by_expiration"), cadence="MEDIUM",
                     source_mode=(DIRECT_PROVIDER if rows_out else UNAVAILABLE),
                     payload=payload)


# ═══════════════════════════════════════════════════════ INTERÉS ABIERTO

def open_interest(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    """OI por strike, por vencimiento, su cambio, su historia y Max Pain.

    Todo del proveedor. El interés abierto NO se reconstruye con volumen ni con
    aproximaciones: el volumen dice cuántos contratos cambiaron de manos, no
    cuántos quedaron abiertos, y confundirlos produce muros que no existen.
    """
    sym = str(symbol or "").upper()
    by_strike = _rows(intel, "oi_by_strike")
    by_expiry = _rows(intel, "oi_by_expiration")
    change = _rows(intel, "oi_change")
    over_time = _rows(intel, "oi_over_time")
    mp_block = _block(intel, "max_pain")
    mp_series = _rows(intel, "max_pain_over_time")

    for tool, metric in (("oi_by_strike", "QD_OPEN_INTEREST_BY_STRIKE"),
                         ("oi_by_expiration", "QD_OPEN_INTEREST_BY_EXPIRATION"),
                         ("oi_change", "QD_OPEN_INTEREST_CHANGE"),
                         ("oi_over_time", "QD_OPEN_INTEREST_OVER_TIME"),
                         ("max_pain", "QD_MAX_PAIN"),
                         ("max_pain_over_time", "QD_MAX_PAIN_OVER_TIME")):
        block = _block(intel, tool)
        cls = classify(block, cadence="SLOW")
        n = cls["rows"]
        LINEAGE.record(metric, sym,
                       source_mode=(DIRECT_PROVIDER if cls["state"] == DATA_OK else UNAVAILABLE),
                       state=cls["state"], endpoint=block.get("path"),
                       normalized_value=n, final_value=n, detail=cls["detail"],
                       age_seconds=cls["age_seconds"], rows=n)

    max_pain = _f(mp_block.get("value")) if mp_block.get("ready") else None
    payload = {
        "ready": bool(by_strike or by_expiry or change or max_pain is not None),
        "by_strike": by_strike, "by_expiration": by_expiry,
        "change": change, "over_time": over_time,
        "max_pain": max_pain,
        "max_pain_over_time": mp_series,
        "source": "QUANTDATA_OPEN_INTEREST",
        "reconstruction": "NEVER_FROM_VOLUME",
        "summary": {"strikes": len(by_strike), "expirations": len(by_expiry),
                    "changed_strikes": len(change), "max_pain": max_pain},
    }
    return _envelope("QD_OPEN_INTEREST_BY_STRIKE", sym, tool="oi_by_strike",
                     block=_block(intel, "oi_by_strike"), cadence="SLOW",
                     source_mode=(DIRECT_PROVIDER if payload["ready"] else UNAVAILABLE),
                     payload=payload)


# ═══════════════════════════════════════════════════════ VOLATILIDAD

def volatility(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    """Volatility Drift · IV Rank · Skew · Term Structure, dentro de la sección que ya existe."""
    sym = str(symbol or "").upper()
    drift = _rows(intel, "volatility_drift")
    skew = _rows(intel, "volatility_skew")
    term = _rows(intel, "term_structure")
    iv_block = _block(intel, "iv_rank")

    for tool, metric in (("volatility_drift", "QD_VOLATILITY_DRIFT"),
                         ("volatility_skew", "QD_VOLATILITY_SKEW"),
                         ("term_structure", "QD_TERM_STRUCTURE"),
                         ("iv_rank", "QD_IV_RANK")):
        block = _block(intel, tool)
        cls = classify(block, cadence="SLOW")
        LINEAGE.record(metric, sym,
                       source_mode=(DIRECT_PROVIDER if cls["state"] == DATA_OK else UNAVAILABLE),
                       state=cls["state"], endpoint=block.get("path"),
                       normalized_value=cls["rows"], final_value=cls["rows"],
                       detail=cls["detail"], age_seconds=cls["age_seconds"], rows=cls["rows"])

    payload = {
        "ready": bool(drift or skew or term or iv_block.get("ready")),
        "drift": drift, "skew": skew, "term_structure": term,
        "iv_rank": (iv_block.get("raw") if iv_block.get("ready") else None),
        "source": "QUANTDATA_VOLATILITY",
        "summary": {"drift": len(drift), "skew": len(skew), "term": len(term)},
    }
    return _envelope("QD_VOLATILITY_DRIFT", sym, tool="volatility_drift",
                     block=_block(intel, "volatility_drift"), cadence="SLOW",
                     source_mode=(DIRECT_PROVIDER if payload["ready"] else UNAVAILABLE),
                     payload=payload)


# ═══════════════════════════════════════════════════════ DARK POOL

def dark_pool(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    """Dark Flow · Dark Pool Levels · Equity Prints, del proveedor.

    Es la fuente PRINCIPAL. La clasificación por `venue` de otra cinta sigue
    existiendo como auditoría —dos medidas independientes que coinciden valen más
    que una—, pero ya no es la única vía, que es lo que dejaba la sección en cero
    cuando el venue no llegaba o no se interpretaba bien.
    """
    sym = str(symbol or "").upper()
    flow = _rows(intel, "dark_flow")
    levels = _rows(intel, "dark_pool_levels")
    prints = _rows(intel, "equity_prints")

    # Tri-estado de clasificación, necesario ANTES de decidir el estado del carril
    # de prints: unas impresiones que llegan y no se pueden clasificar no son un
    # carril sano, y tampoco son un mercado sin actividad fuera de bolsa.
    _classified = sum(1 for r in prints if r.get("off_exchange") is not None)
    _unclassified = sum(1 for r in prints if r.get("off_exchange") is None)
    market_open = _market_is_open()

    lanes: Dict[str, Dict[str, Any]] = {}
    for tool, metric in (("dark_flow", "QD_DARK_FLOW"),
                         ("dark_pool_levels", "QD_DARK_POOL_LEVELS"),
                         ("equity_prints", "QD_EQUITY_PRINTS")):
        block = _block(intel, tool)
        cls = classify(block, cadence="MEDIUM")
        lane = dark_pool_state.lane_state(
            block, cls, market_open=market_open,
            unclassified=(_unclassified if tool == "equity_prints" else 0),
            classified=(_classified if tool == "equity_prints" else 0))
        lanes[tool] = lane
        # Se registra el estado del CARRIL, no el genérico: un 400 y un 503 dejan el
        # bloque igual de vacío y no se corrigen igual, y el Auditor existe
        # precisamente para poder distinguirlos.
        LINEAGE.record(metric, sym,
                       source_mode=(DIRECT_PROVIDER
                                    if lane["state"] == dark_pool_state.DIRECT_PROVIDER_OK
                                    else UNAVAILABLE),
                       state=lane["lineage_state"], endpoint=block.get("path"),
                       normalized_value=cls["rows"], final_value=cls["rows"],
                       detail=(lane["detail"] or cls["detail"]),
                       age_seconds=cls["age_seconds"], rows=cls["rows"])

    # v1.48.0 · Un intervalo SIN volumen publicado no suma cero: no suma. Antes
    # `_f(..., 0.0) or 0.0` convertía seiscientos intervalos sin campo de
    # volumen en «0.0 acc», que afirma que no hubo actividad fuera de bolsa
    # cuando lo cierto es que no se pudo leer.
    _dark_vals = [_f(r.get("dark_volume")) for r in flow]
    _dark_vals = [v for v in _dark_vals if v is not None]
    _total_vals = [_f(r.get("total_volume")) for r in flow]
    _total_vals = [v for v in _total_vals if v is not None]
    dark_vol = sum(_dark_vals) if _dark_vals else None
    total_vol = sum(_total_vals) if _total_vals else None
    flow_block = _block(intel, "dark_flow")
    notional = 0.0
    notional_seen = False
    for r in flow:
        n = _f(r.get("dark_notional"))
        if n is not None:
            notional += n
            notional_seen = True
    if not notional_seen:
        # La prima no se inventa: si el flujo no la trae, se suma la de los prints
        # que sí la declaran, y si tampoco los hay queda en None, no en cero.
        for r in prints:
            if r.get("off_exchange"):
                n = _f(r.get("notional"))
                if n is not None:
                    notional += n
                    notional_seen = True

    # Tri-estado: confirmadas fuera de bolsa, confirmadas en bolsa, y las que no se
    # pudieron clasificar. Las terceras NO son cero dark pool: son desconocidas, y
    # contarlas como ceros es lo que dejaba la sección vacía teniendo prints.
    dark_prints = [r for r in prints if r.get("off_exchange") is True]
    unknown_prints = [r for r in prints if r.get("off_exchange") is None]
    level_notional = sum(_f(r.get("notional"), 0.0) or 0.0 for r in levels)
    if not notional_seen and level_notional > 0:
        notional = level_notional
        notional_seen = True

    # Un carril que responde pero del que no se puede leer ninguna magnitud no
    # deja la sección «lista»: deja constancia de que respondió y de por qué no
    # sirve todavía.
    flow_usable = bool(flow) and (dark_vol is not None or notional_seen)
    ready = bool(flow_usable or levels or dark_prints)
    payload = {
        "ready": ready,
        "flow": flow, "levels": levels, "prints": dark_prints,
        "notional": round(notional, 2) if notional_seen else None,
        "shares": round(dark_vol, 2) if dark_vol is not None else None,
        "trades": (len(dark_prints) or None),
        "dark_share_pct": (round(100.0 * dark_vol / total_vol, 2)
                           if (dark_vol is not None and total_vol) else None),
        "level_count": len(levels),
        "prints_total": len(prints),
        "prints_unclassified": len(unknown_prints),
        # Por qué la sección está como está, en términos que distinguen un mercado
        # sin dark pool de una clasificación que no pudimos hacer.
        "coverage_reason": (
            "" if dark_prints or flow or levels
            else ("las impresiones llegaron sin clasificar: el proveedor no declara "
                  "off-exchange y no hay centro de ejecución que interpretar"
                  if unknown_prints else
                  "no llegaron impresiones en este ciclo" if not prints else
                  "todas las impresiones se ejecutaron en bolsa")),
        # v1.46.0 · Los tres carriles, cada uno con su causa. La pantalla del
        # analista sigue diciendo «SIN DATOS»; el Auditor conserva cuál de los ocho
        # estados internos lo produjo y qué hay que hacer con él.
        "lanes": lanes,
        "lane_rows": dark_pool_state.lane_rows(lanes),
        # Con qué campo se leyó cada magnitud y qué campos trajo el proveedor.
        # Es lo que convierte «608 intervalos · 0.0 acc» en algo corregible.
        "flow_fields": {
            "map": flow_block.get("field_map") or {},
            "observed": flow_block.get("observed_fields") or [],
            "intervals": len(flow),
            "intervals_with_volume": int(flow_block.get("intervals_with_volume") or 0),
            "volume_field_resolved": bool(flow_block.get("volume_field_resolved")),
        },
        "diagnosis": dark_pool_state.section_state(lanes),
        "source": "QUANTDATA_DARK_POOL",
        "audit_channel": "ITM_QUANT_VENUE_CLASSIFICATION",
        "summary": {"levels": len(levels), "prints": len(dark_prints),
                    "notional": (round(notional, 2) if notional_seen else None)},
    }
    return _envelope("QD_DARK_FLOW", sym, tool="dark_flow",
                     block=_block(intel, "dark_flow"), cadence="MEDIUM",
                     source_mode=(DIRECT_PROVIDER if ready else UNAVAILABLE),
                     payload=payload)


# ═══════════════════════════════════════════════════════ ESTADÍSTICAS

def statistics(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    """Contract Statistics · Trade Side · Market Share · Gainers/Losers.

    Contexto SECUNDARIO por diseño: ayudan a situar, no desplazan en importancia a
    flujo, exposición, dark pool, interés abierto ni volatilidad.
    """
    sym = str(symbol or "").upper()
    out: Dict[str, Any] = {}
    for tool, metric, name in (("contract_statistics", "QD_CONTRACT_STATISTICS", "contracts"),
                               ("contract_trade_side_statistics", "QD_TRADE_SIDE_STATISTICS", "trade_side"),
                               ("market_share", "QD_MARKET_SHARE", "market_share"),
                               ("gainers_losers", "QD_GAINERS_LOSERS", "movers")):
        rows = _rows(intel, tool)
        block = _block(intel, tool)
        cls = classify(block, cadence="MEDIUM")
        LINEAGE.record(metric, sym,
                       source_mode=(DIRECT_PROVIDER if rows else UNAVAILABLE),
                       state=cls["state"], endpoint=block.get("path"),
                       normalized_value=len(rows), final_value=len(rows),
                       detail=cls["detail"], rows=len(rows))
        out[name] = rows
    payload = {"ready": any(out.values()), **out, "priority": "SECONDARY_CONTEXT",
               "source": "QUANTDATA_STATISTICS",
               "summary": {k: len(v) for k, v in out.items()}}
    return _envelope("QD_CONTRACT_STATISTICS", sym, tool="contract_statistics",
                     block=_block(intel, "contract_statistics"), cadence="MEDIUM",
                     source_mode=(DIRECT_PROVIDER if payload["ready"] else UNAVAILABLE),
                     payload=payload)


# ═══════════════════════════════════════════════════════ FLUJO

def flow(symbol: str, intel: Dict[str, Any]) -> Dict[str, Any]:
    """Net Flow · Net Drift · Order Flow, con su procedencia registrada.

    v1.45.0 · Estos tres datasets se consumían desde `terminal_api` sin pasar por
    el Hub, así que **no dejaban registro de procedencia**: el Auditor no podía
    decir si el Net Flow en pantalla venía del proveedor o de un respaldo, que es
    justo la pregunta para la que existe el registro. Los valores eran correctos;
    lo que faltaba era la trazabilidad.
    """
    sym = str(symbol or "").upper()
    out: Dict[str, Any] = {}
    for tool, metric, name, cadence in (
            ("net_flow", "QD_NET_FLOW", "net_flow", "FAST"),
            ("net_drift", "QD_NET_DRIFT", "net_drift", "FAST"),
            ("options_order_flow", "QD_ORDER_FLOW_CONSOLIDATED", "order_flow_consolidated", "FAST"),
            ("options_order_flow_raw", "QD_ORDER_FLOW_UNCONSOLIDATED", "order_flow_unconsolidated", "FAST")):
        block = _block(intel, tool)
        cls = classify(block, cadence=cadence)
        rows = _rows(intel, tool)
        LINEAGE.record(metric, sym,
                       source_mode=(DIRECT_PROVIDER if rows else UNAVAILABLE),
                       state=cls["state"], endpoint=block.get("path"),
                       raw_value={"tool": tool}, normalized_value=len(rows),
                       final_value=len(rows), detail=cls["detail"],
                       age_seconds=cls["age_seconds"], rows=len(rows))
        out[name] = {"ready": bool(rows), "rows": rows, "count": len(rows),
                     "state": cls["state"], "tool": tool,
                     "source_mode": (DIRECT_PROVIDER if rows else UNAVAILABLE)}
    payload = {"ready": any(v["ready"] for v in out.values()), **out,
               "source": "QUANTDATA_FLOW",
               "summary": {k: v["count"] for k, v in out.items()}}
    return _envelope("QD_NET_FLOW", sym, tool="net_flow",
                     block=_block(intel, "net_flow"), cadence="FAST",
                     source_mode=(DIRECT_PROVIDER if payload["ready"] else UNAVAILABLE),
                     payload=payload)


# ═══════════════════════════════════════════════════════ GREEKS POR CONTRATO

def contract_greeks(symbol: str, intel: Dict[str, Any], *, limit: int = 400) -> Dict[str, Any]:
    """Greeks POR CONTRATO tal y como los publica Quant Data.

    Delta, Gamma, Theta, Vega, Rho y los de orden superior (Vanna, Charm, Vomma,
    Veta, Speed, Color, Ultima) viajan en las filas del order flow. ITM QUANT
    puede usarlos como entrada de sus modelos, pero no vuelve a calcularlos
    cuando el proveedor ya los entrega válidos: dos Deltas distintos para el mismo
    contrato en la misma pantalla es peor que no tener ninguno.
    """
    sym = str(symbol or "").upper()
    rows: List[Dict[str, Any]] = []
    tool_used = None
    for tool in ("options_order_flow_raw", "options_order_flow"):
        src = _rows(intel, tool)
        if not src:
            continue
        tool_used = tool
        for r in src[:limit]:
            g = r.get("greeks") if isinstance(r.get("greeks"), dict) else {}
            if not any(v is not None for v in g.values()):
                continue
            rows.append({
                "t": r.get("t"), "strike": r.get("strike"),
                "expiration": r.get("expiration"), "option_type": r.get("option_type"),
                "dte": r.get("dte"), "implied_volatility": r.get("implied_volatility"),
                "open_interest": r.get("open_interest"), **g,
            })
        if rows:
            break

    present = sorted({k for r in rows for k, v in r.items()
                      if v is not None and k in ("delta", "gamma", "theta", "vega", "rho",
                                                 "vanna", "charm", "vomma", "veta",
                                                 "speed", "color", "ultima")})
    for greek in ("delta", "gamma", "theta", "vega", "rho", "vanna", "charm"):
        metric = f"QD_GREEK_{greek.upper()}"
        LINEAGE.record(metric, sym,
                       source_mode=(DIRECT_PROVIDER if greek in present else UNAVAILABLE),
                       state=(DATA_OK if greek in present else NO_PROVIDER_DATA),
                       normalized_value=len(rows), final_value=len(rows),
                       rows=len(rows),
                       detail="" if greek in present else "el proveedor no publicó esta griega")

    payload = {"ready": bool(rows), "rows": rows, "count": len(rows),
               "greeks_present": present, "tool": tool_used,
               "source": "QUANTDATA_CONTRACT_GREEKS",
               "authority": "QUANT_DATA_IS_PRIMARY_FOR_OPTION_GREEKS",
               "summary": {"contracts": len(rows), "greeks": present}}
    return _envelope("QD_GREEK_DELTA", sym, tool=(tool_used or "options_order_flow_raw"),
                     block=_block(intel, tool_used or "options_order_flow_raw"),
                     cadence="FAST",
                     source_mode=(DIRECT_PROVIDER if rows else UNAVAILABLE),
                     payload=payload)


# ═══════════════════════════════════════════════════════ vista de conjunto

def hub_snapshot(symbol: str, intel: Dict[str, Any], *,
                 engine_heatmap: Optional[Dict[str, Any]] = None,
                 price: Optional[List[Dict[str, Any]]] = None,
                 greek: str = "GAMMA") -> Dict[str, Any]:
    """Todo lo que el Data Hub publica para un símbolo, en una sola lectura.

    Es la entrada que consumen a la vez la interfaz y el motor cuantitativo. Que
    sea la MISMA hace imposible que la pantalla y el motor describan dos mercados
    distintos.
    """
    sym = str(symbol or "").upper()
    caps = capabilities(sym, intel)
    return {
        "symbol": sym,
        "capabilities": caps,
        "interval_map": interval_map(sym, intel, greek, engine_heatmap=engine_heatmap,
                                     price=price),
        "exposure_by_strike": exposure_by_strike(sym, intel),
        "exposure_by_expiration": exposure_by_expiration(sym, intel),
        "open_interest": open_interest(sym, intel),
        "volatility": volatility(sym, intel),
        "dark_pool": dark_pool(sym, intel),
        "statistics": statistics(sym, intel),
        "contract_greeks": contract_greeks(sym, intel),
        "flow": flow(sym, intel),
        "route": "QUANT_DATA -> DATA_HUB -> INTERFACE",
        "engine_route": "QUANT_DATA -> DATA_HUB -> ENGINE (paralelo, no en serie)",
        "contract": "ITMQ_QUANT_DATA_HUB_V1",
    }


__all__ = ["capabilities", "provider_healthy_for", "classify", "interval_map", "flow",
           "exposure_by_strike", "exposure_by_expiration", "open_interest",
           "volatility", "dark_pool", "statistics", "contract_greeks",
           "hub_snapshot", "INTERVAL_GREEKS", "TOOL_METRIC", "STALE_AFTER_SECONDS"]
