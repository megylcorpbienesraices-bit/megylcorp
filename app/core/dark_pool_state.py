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
#: v1.62.0 · El NOVENO: todavía no se ha ejecutado. No es un fallo y no es
#: «sin datos»: es que la petición aún no se ha hecho. Decir «el proveedor no
#: devolvió filas» de una llamada que nunca salió le atribuye al proveedor un
#: silencio que es nuestro.
ESPERANDO = "ESPERANDO"

DARK_POOL_STATES = (
    DIRECT_PROVIDER_OK, SIN_DATOS_REALES, MARKET_CLOSED, STALE,
    REQUEST_INVALID, PROVIDER_ERROR, PARSER_ERROR, NO_CLASIFICABLE,
    ESPERANDO,
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
    ESPERANDO: "la petición de este ciclo todavía no se ha hecho: no es un vacío del proveedor",
}

#: Un estado que el analista no debe leer en crudo. La pantalla dice SIN DATOS;
#: el Auditor conserva cuál de los ocho es.
SCREEN_TEXT = {
    DIRECT_PROVIDER_OK: "",
    MARKET_CLOSED: "MERCADO CERRADO",
    STALE: "DATO ANTERIOR",
    ESPERANDO: "EN COLA",
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


def _current_status(provider_status: str, generic: str, rows: int) -> str:
    """Qué pasó en ESTE refresco, sin mirar lo que hubiera guardado antes.

    Los tres significados que no se pueden mezclar:

        LIVE            respondió y trajo filas
        NO_DATA         respondió BIEN y no hay filas (no es una avería)
        PROVIDER_ERROR  timeout o 5xx: no se sabe si hay filas

    Distinguir el segundo del tercero es lo que evita mandar a nadie a buscar
    una avería en un mercado que simplemente no tiene actividad fuera de bolsa.
    """
    st = str(provider_status or "").upper()
    if st in ("PROVIDER_ERROR", "TRANSIENT", "MISSING_TOOL", "TIMEOUT"):
        return PROVIDER_ERROR
    if st == "REQUEST_INVALID":
        return REQUEST_INVALID
    if str(generic or "") == LINEAGE_PROVIDER_ERROR:
        return PROVIDER_ERROR
    if int(rows or 0) > 0:
        return DIRECT_PROVIDER_OK
    return NO_DATOS_ACTUAL


#: Un refresco que respondió bien y no trajo filas. No es una avería y no puede
#: compartir etiqueta con un timeout.
NO_DATOS_ACTUAL = "NO_DATA"

#: Un refresco que falló habiendo dato bueno guardado.
STALE_LKG = "STALE_LKG"


def lane_state(block: Any, classification: Dict[str, Any], *,
               market_open: Optional[bool] = None,
               unclassified: int = 0, classified: int = 0,
               truth: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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

    # ═══════════════════════════════════════════════════════════════════════
    # v1.62.0 · LA VERDAD MANDA. ESTO YA NO LA DEDUCE.
    # ═══════════════════════════════════════════════════════════════════════
    #
    # Lo que este carril publica sobre la EJECUCIÓN —qué pasó, qué se sirve y
    # cuántas filas ve el operador— sale del registro que escribió quien hizo la
    # llamada (`lane_truth`), no de mirar el bloque publicado. Deducirlo desde
    # el bloque era leer una huella: un refresco fallido CONSERVA el bloque
    # anterior —y debe conservarlo—, así que `ready` seguía en `True` y `rows`
    # seguía trayendo 382, y quien mirara eso concluía «CON DATOS» sobre una
    # llamada muerta por plazo.
    #
    # Los OCHO estados de esta sección siguen aquí porque dicen cosas que la
    # autoridad genérica no distingue —`NO_CLASIFICABLE`, `MARKET_CLOSED`—,
    # pero ya no pueden contradecirla: se derivan de ella.
    # `known == False` significa que NADIE ha escrito la verdad de este carril.
    # Tomarla por buena afirmaría una espera que nadie midió, que es el mismo
    # error que afirmar un vacío que nadie midió. Sin registro se cae a lo que
    # el bloque publicado permita deducir, que es lo que había antes.
    verdad = dict(truth or {})
    if verdad and verdad.get("known") is False:
        verdad = {}
    detail = str(block.get("lane_detail") or cls.get("detail") or "")
    fields = list(block.get("lane_fields") or [])

    # v1.57.0 · UN FALLO DE LLAMADA CON DATO BUENO GUARDADO ES «VIEJO», NO «ROTO».
    #
    # Un carril que ya había traído 216 filas y luego agota su plazo se
    # declaraba PROVIDER_ERROR, y eso rompía la SECCIÓN entera aunque el dato
    # estuviera ahí y fuera bueno. En pantalla salía el error; al lado, las 216
    # filas. Dos afirmaciones contradictorias sobre el mismo carril.
    #
    # El reintento con backoff sigue programado y el error se conserva entero en
    # `last_error` para que nadie lo pierda de vista. Lo que cambia es la
    # conclusión: hay dato real, de un ciclo que sí funcionó, y se dice que es
    # viejo en vez de tirarlo.
    #
    # `REQUEST_INVALID` NO entra aquí: un 400 significa que el cuerpo está mal
    # y reintentarlo no lo va a arreglar. Ése sí es un fallo nuestro y se
    # declara aunque haya filas antiguas.
    if verdad:
        estado_ejecucion = str(verdad.get("current_status") or "")
        fallo_de_llamada = bool(verdad.get("is_failure"))
        hay_dato_bueno = str(verdad.get("serving") or "") == "LKG"
        rows = int(verdad.get("served_rows") or 0)
    else:
        estado_ejecucion = ""
        fallo_de_llamada = (provider_status in ("PROVIDER_ERROR", "TRANSIENT", "MISSING_TOOL")
                            or generic == LINEAGE_PROVIDER_ERROR)
        hay_dato_bueno = bool(rows) and bool(cls.get("has_payload", rows > 0))

    if estado_ejecucion == "REQUEST_INVALID" or provider_status == "REQUEST_INVALID":
        state = REQUEST_INVALID
    elif estado_ejecucion in ("WAITING_SCHEDULED", "WAITING_RATE_LIMIT",
                              "WAITING_DEPENDENCY", "WAITING_COOLDOWN", "RUNNING"):
        # Esperar NO es un fallo y no se puede pintar como tal. Si mientras
        # espera hay último valor bueno, se sirve y se dice que es viejo.
        state = STALE if hay_dato_bueno else ESPERANDO
    elif estado_ejecucion == "PARSER_ERROR":
        state = PARSER_ERROR
    elif estado_ejecucion == "NO_DATA" and not hay_dato_bueno:
        state = MARKET_CLOSED if market_open is False else SIN_DATOS_REALES
    elif estado_ejecucion in ("PROVIDER_ERROR", "MISSING_TOOL") and not hay_dato_bueno:
        # Un fallo SIN último valor bueno es un fallo, y tiene que decirlo. Caer
        # aquí en «sin datos reales» fue lo que dejaba a `equity_prints`
        # declarando un vacío de mercado sobre una llamada muerta por plazo.
        state = PROVIDER_ERROR
    elif fallo_de_llamada and hay_dato_bueno:
        state = STALE
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
        # El fallo de la llamada NO desaparece porque el carril tenga dato
        # bueno guardado. Se conserva entero para que el reintento y su causa
        # se puedan seguir desde el Auditor.
        "last_error": (str(cls.get("detail") or "")[:240]
                       if fallo_de_llamada else ""),
        "call_failed": bool(fallo_de_llamada),
        "capability": cls.get("capability"),
        "rows": rows,
        "age_seconds": cls.get("age_seconds"),
        # ═══════════════════════════════════════════════════════════════════
        # v1.59.0 · ESTADO ACTUAL Y ÚLTIMO VALOR BUENO, SIN MEZCLAR
        # ═══════════════════════════════════════════════════════════════════
        #
        # `state` y `rows` respondían a dos preguntas distintas con un solo par
        # de campos, y eso obligaba a elegir cuál contar. Un timeout de ahora
        # con cien filas del ciclo anterior se leía como SIN_DATOS —tirando un
        # dato bueno— o como OK —escondiendo que el refresco falló—. Las dos
        # lecturas son falsas.
        #
        # Son CINCO hechos independientes y cada uno tiene su campo:
        #
        #   current_status   qué pasó en ESTE refresco
        #   current_rows     filas que trajo ESTE refresco (0 si falló)
        #   lkg_rows         filas del último refresco que SÍ funcionó
        #   lkg_age          cuántos segundos tiene ese último bueno
        #   last_success_at  cuándo fue
        #
        # Con eso, los tres significados dejan de confundirse:
        #   HTTP 200 con cero filas   → current_status NO_DATA, y el carril
        #                               declara SIN_DATOS_REALES: no hay avería
        #   timeout/5xx con LKG       → current_status PROVIDER_ERROR y
        #                               `serving` STALE_LKG: el refresco falló y
        #                               lo que se ve es el último ciclo bueno
        #   timeout/5xx sin LKG       → current_status PROVIDER_ERROR y no hay
        #                               nada que servir
        # Los diez campos canónicos NO se recalculan aquí: se copian de la
        # autoridad. Si algún día vuelven a divergir, es que alguien volvió a
        # deducirlos, y la regresión de consistencia lo caza.
        "current_status": (verdad.get("current_status")
                           if verdad else _current_status(provider_status, generic, rows)),
        "current_rows": (int(verdad.get("current_rows") or 0) if verdad
                         else (rows if not fallo_de_llamada else 0)),
        "serving": (verdad.get("serving") if verdad else
                    ("LIVE" if (not fallo_de_llamada and rows > 0) else
                     (STALE_LKG if (fallo_de_llamada and hay_dato_bueno) else "NONE"))),
        "served_rows": (int(verdad.get("served_rows") or 0) if verdad else rows),
        "lkg_rows": (int(verdad.get("lkg_rows") or 0) if verdad else
                     (rows if hay_dato_bueno or not fallo_de_llamada else 0)),
        "lkg_age": (verdad.get("lkg_age") if verdad else cls.get("age_seconds")),
        "last_success_at": (verdad.get("last_success_at") if verdad else
                            (block.get("fetched_at") or cls.get("last_success_at"))),
        "refresh_error": (verdad.get("refresh_error") if verdad else
                          (str(cls.get("detail") or "")[:400] if fallo_de_llamada else "")),
        "refresh_error_phase": (verdad.get("refresh_error_phase") if verdad else ""),
        "as_of": verdad.get("as_of") if verdad else None,
        "request_id": verdad.get("request_id") if verdad else "",
        "cycle_id": verdad.get("cycle_id") if verdad else "",
        # La etiqueta que ve el operador sale de la MISMA función para todas las
        # pantallas. Que una dijera «CON DATOS» y otra «STALE» sobre la misma
        # ejecución es justo lo que esto impide.
        "truth_screen": verdad.get("screen") if verdad else "",
        "fresh": bool(verdad.get("fresh")) if verdad else (not fallo_de_llamada and rows > 0),
        "waiting_since": verdad.get("waiting_since") if verdad else None,
        "waiting_seconds": verdad.get("waiting_seconds") if verdad else None,
        "waiting_reason": verdad.get("waiting_reason") if verdad else "",
        "next_eligible_at": verdad.get("next_eligible_at") if verdad else None,
        "anomaly": verdad.get("anomaly") if verdad else None,
        "timing": dict(verdad.get("timing") or {}) if verdad else {},
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
