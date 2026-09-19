"""Por qué DARK POOL está como está — v1.46.0.

La pantalla del analista puede decir «SIN DATOS». Lo que no puede pasar es que
*nosotros* no sepamos por qué. Hasta v1.45.0 los tres carriles de dark pool
—Dark Flow, Dark Pool Levels, Equity Prints— colapsaban en una sola frase
genérica cuando alguno fallaba, así que un cuerpo rechazado (que se arregla
corrigiendo el payload), un mercado cerrado (que se arregla esperando) y un
proveedor caído (que se arregla reintentando) se veían exactamente igual.

Ocho estados, ocho causas distintas, ocho acciones distintas:

    DIRECT_PROVIDER_OK  el proveedor entregó y el dato es suyo
    SIN_DATOS_REALES    respondió bien y no hay nada que mostrar
    MARKET_CLOSED       no hay sesión: el vacío es correcto
    STALE               hubo dato, pero es viejo (Last Known Good degradado)
    REQUEST_INVALID     400 · el cuerpo que enviamos está mal → se corrige el payload
    PROVIDER_ERROR      5xx/red · fallo suyo → se reintenta con backoff
    PARSER_ERROR        respondió y no supimos leerlo → se corrige el normalizador
    NO_CLASIFICABLE     llegaron impresiones sin poder decidir dentro/fuera de bolsa

No confundir con `dark_pool_taxonomy`, que responde a otra pregunta: allí se
clasifica CADA IMPRESIÓN por su nivel de evidencia (confirmada fuera de bolsa,
print grande en bolsa, zona derivada). Aquí se clasifica POR QUÉ UN CARRIL no
tiene datos. Una responde «¿qué es esto que veo?»; la otra, «¿por qué no veo
nada?».

Cada carril mantiene su propio estado. Que `dark-pool-levels` esté en
REQUEST_INVALID no dice nada sobre `dark-flow`, y presentarlos juntos fue lo
que hacía parecer que la sección entera estaba rota teniendo dos de tres
carriles sanos.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.data_lineage import (
    DATA_OK, FILTERED_ALL, NO_PROVIDER_DATA, PARSER_ERROR as LINEAGE_PARSER_ERROR,
    PROVIDER_ERROR as LINEAGE_PROVIDER_ERROR, STALE as LINEAGE_STALE,
)

# ── Estados internos (Auditor) ────────────────────────────────────────────────
DIRECT_PROVIDER_OK = "DIRECT_PROVIDER_OK"
SIN_DATOS_REALES = "SIN_DATOS_REALES"
MARKET_CLOSED = "MARKET_CLOSED"
STALE = "STALE"
REQUEST_INVALID = "REQUEST_INVALID"
PROVIDER_ERROR = "PROVIDER_ERROR"
PARSER_ERROR = "PARSER_ERROR"
NO_CLASIFICABLE = "NO_CLASIFICABLE"

DARK_POOL_STATES = (
    DIRECT_PROVIDER_OK, SIN_DATOS_REALES, MARKET_CLOSED, STALE,
    REQUEST_INVALID, PROVIDER_ERROR, PARSER_ERROR, NO_CLASIFICABLE,
)

#: Los tres carriles, con el nombre de la herramienta del proveedor.
DARK_POOL_LANES = ("dark_flow", "dark_pool_levels", "equity_prints")

LANE_TITLES = {
    "dark_flow": "Dark Flow",
    "dark_pool_levels": "Dark Pool Levels",
    "equity_prints": "Equity Prints",
}

#: Qué hacer con cada estado. Un estado sin acción asociada es un estado inútil.
REMEDY = {
    DIRECT_PROVIDER_OK: "",
    SIN_DATOS_REALES: "no hay actividad fuera de bolsa publicada en esta ventana",
    MARKET_CLOSED: "sin sesión abierta: el vacío es el resultado correcto",
    STALE: "se muestra el último dato bueno; el carril no se ha refrescado a tiempo",
    REQUEST_INVALID: "el proveedor rechaza el cuerpo: hay que corregir el payload, no reintentarlo",
    PROVIDER_ERROR: "fallo del proveedor: reintento programado con backoff",
    PARSER_ERROR: "el proveedor respondió y el normalizador no supo leerlo",
    NO_CLASIFICABLE: "llegaron impresiones sin señal de centro de ejecución utilizable",
}

#: Un estado que el analista no debe leer en crudo. La pantalla dice SIN DATOS;
#: el Auditor conserva cuál de los ocho es.
SCREEN_TEXT = {
    DIRECT_PROVIDER_OK: "",
    MARKET_CLOSED: "MERCADO CERRADO",
    STALE: "DATO ANTERIOR",
}


def is_failure(state: str) -> bool:
    """¿El vacío se debe a un fallo nuestro o del proveedor, y no al mercado?"""
    return state in (REQUEST_INVALID, PROVIDER_ERROR, PARSER_ERROR)


def screen_label(state: str) -> str:
    """Lo que ve el analista. Nunca un código de error del proveedor."""
    return SCREEN_TEXT.get(state, "SIN DATOS")


def _lineage_state(state: str) -> str:
    """Traducción al vocabulario de `data_lineage`, que es el que registra el Auditor."""
    return {
        DIRECT_PROVIDER_OK: DATA_OK,
        SIN_DATOS_REALES: NO_PROVIDER_DATA,
        MARKET_CLOSED: NO_PROVIDER_DATA,
        STALE: LINEAGE_STALE,
        REQUEST_INVALID: LINEAGE_PROVIDER_ERROR,
        PROVIDER_ERROR: LINEAGE_PROVIDER_ERROR,
        PARSER_ERROR: LINEAGE_PARSER_ERROR,
        NO_CLASIFICABLE: FILTERED_ALL,
    }.get(state, NO_PROVIDER_DATA)


def lane_state(block: Any, classification: Dict[str, Any], *,
               market_open: Optional[bool] = None,
               unclassified: int = 0, classified: int = 0) -> Dict[str, Any]:
    """Estado de UN carril, con su causa y su remedio.

    `block` es lo que publicó el carril de páginas; `classification` lo que
    devolvió `quant_data_hub.classify()`. El estado del proveedor (`lane_status`)
    manda sobre el estado genérico porque distingue lo que `classify` no puede
    distinguir: un 400 y un 503 dejan el bloque igual de vacío y no se arreglan
    de la misma manera.
    """
    block = block if isinstance(block, dict) else {}
    cls = classification if isinstance(classification, dict) else {}
    generic = str(cls.get("state") or NO_PROVIDER_DATA)
    rows = int(cls.get("rows") or 0)
    provider_status = str(block.get("lane_status") or "")
    detail = str(block.get("lane_detail") or cls.get("detail") or "")
    fields = list(block.get("lane_fields") or [])

    if provider_status == "REQUEST_INVALID":
        state = REQUEST_INVALID
    elif provider_status in ("PROVIDER_ERROR", "TRANSIENT", "MISSING_TOOL"):
        state = PROVIDER_ERROR
    elif generic == LINEAGE_PARSER_ERROR:
        state = PARSER_ERROR
    elif generic == LINEAGE_PROVIDER_ERROR:
        state = PROVIDER_ERROR
    elif generic == LINEAGE_STALE:
        state = STALE
    elif generic == DATA_OK and rows:
        state = DIRECT_PROVIDER_OK
    elif unclassified and not classified:
        state = NO_CLASIFICABLE
    elif market_open is False:
        state = MARKET_CLOSED
    else:
        state = SIN_DATOS_REALES

    if state == DIRECT_PROVIDER_OK and unclassified and not classified:
        # Respondió, trae filas, y ninguna se pudo clasificar: eso no es un OK.
        state = NO_CLASIFICABLE

    return {
        "state": state,
        "lineage_state": _lineage_state(state),
        "is_failure": is_failure(state),
        "screen": screen_label(state),
        "detail": detail[:240],
        "remedy": REMEDY.get(state, ""),
        "rejected_fields": fields[:8],
        # v1.49.0 · El 400 entero: `type`, `detail` y cada `errors[].field` con
        # su mensaje. Sin esto, «HTTP 400» no se puede corregir.
        "error": block.get("lane_error") or {},
        "rows": rows,
        "age_seconds": cls.get("age_seconds"),
        "endpoint": block.get("path"),
        "request_body": block.get("request_body"),
        "unclassified": int(unclassified),
    }


def section_state(lanes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Estado de la SECCIÓN a partir de sus tres carriles, sin fusionarlos.

    Un carril sano basta para que la sección tenga dato. Un carril roto se
    declara aunque los otros dos funcionen: media verdad sobre la cobertura es
    peor que ninguna.
    """
    rows = [v for v in lanes.values() if isinstance(v, dict)]
    live = [k for k, v in lanes.items() if v.get("state") == DIRECT_PROVIDER_OK]
    broken = [k for k, v in lanes.items() if v.get("is_failure")]
    if live:
        state = DIRECT_PROVIDER_OK
    elif broken:
        order = {REQUEST_INVALID: 0, PARSER_ERROR: 1, PROVIDER_ERROR: 2}
        worst = sorted(broken, key=lambda k: order.get(lanes[k].get("state"), 9))[0]
        state = lanes[worst]["state"]
    elif any(v.get("state") == STALE for v in rows):
        state = STALE
    elif any(v.get("state") == NO_CLASIFICABLE for v in rows):
        state = NO_CLASIFICABLE
    elif rows and all(v.get("state") == MARKET_CLOSED for v in rows):
        state = MARKET_CLOSED
    else:
        state = SIN_DATOS_REALES
    detail = ""
    for key in DARK_POOL_LANES:
        v = lanes.get(key) or {}
        if v.get("state") == state and v.get("detail"):
            detail = v["detail"]
            break
    return {
        "state": state,
        "screen": screen_label(state),
        "detail": detail,
        "remedy": REMEDY.get(state, ""),
        "lanes_live": sorted(live),
        "lanes_broken": sorted(broken),
        "degraded": bool(broken) and bool(live),
    }


def lane_rows(lanes: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Las tres filas que pinta el Auditor, en orden fijo."""
    out = []
    for key in DARK_POOL_LANES:
        v = lanes.get(key)
        if not isinstance(v, dict):
            continue
        out.append({"lane": key, "title": LANE_TITLES.get(key, key), **v})
    return out
