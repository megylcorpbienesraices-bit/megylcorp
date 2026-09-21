"""Identidad REAL de cada línea dibujada en TRACE.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ
═══════════════════════════════════════════════════════════════════════════

En el gráfico había líneas sin etiqueta. Una línea sin nombre sobre un
gráfico de operativa es peor que no dibujarla: se ve, parece significar algo,
y no hay forma de saber qué.

La tentación es deducir el nombre por el color —«la roja debe de ser un put
wall»— y eso es exactamente lo que no se puede hacer: el color sale de una
tabla de estilo que agrupa varios `kind` distintos bajo el mismo token. Rojo
es `put_wall` Y `risk`; verde es `call_wall` Y `target`. Deducir por color
acierta la mitad de las veces y no avisa cuando falla.

Este módulo NO calcula ningún nivel y NO renombra ninguno. Sólo adjunta a
cada uno la identidad que ya tiene en el motor, para que se pueda leer:

    price        el valor, tal cual lo publicó el cálculo
    type         el `kind` del motor, sin traducir
    source       la FUNCIÓN y el CAMPO exactos que lo produjeron
    method       el cálculo con su nombre real
    magnitude    la fuerza del nivel, cuando el cálculo publica una
    persistence  cuántos ciclos seguidos lleva en pie, y desde cuándo
    timestamp    el instante en que se calculó

`source` y `method` son literales atados a la función del motor, no
descripciones. Si mañana `structure_levels` deja de producir un nivel, la
entrada desaparece del registro en vez de quedarse describiendo algo que ya
no existe.

═══════════════════════════════════════════════════════════════════════════
PERSISTENCIA
═══════════════════════════════════════════════════════════════════════════

Un nivel que aparece y desaparece cada ciclo no se lee igual que uno que
lleva dos horas clavado en el mismo precio, y el gráfico los dibuja idénticos.
Se cuentan los ciclos consecutivos en los que el nivel se ha publicado
«en el mismo sitio», con una tolerancia relativa al precio del subyacente
—un céntimo es mucho en un ETF de 40 y nada en un índice de 5.800—.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: Qué produce cada `kind`, con el nombre REAL de la función del motor y el
#: campo del que sale el número. No son descripciones: son la procedencia.
#:
#: `function` es la función que construye el registro del nivel.
#: `field` es la clave de la que toma el valor.
#: `method` es el cálculo que produjo ese valor, donde el motor lo nombra.
LEVEL_ORIGIN: Dict[str, Dict[str, str]] = {
    "flip": {
        "function": "nextgen_terminal.structure_levels",
        "field": "gd['gamma_flip']",
        "method": "trace_analytics.gamma_flip · cruce por cero de NetGEX(S)",
        "label": "Zero Gamma",
    },
    "gamma": {
        "function": "nextgen_terminal.structure_levels",
        "field": "gd['gamma_center']",
        "method": "trace_analytics · centroide ponderado por gamma",
        "label": "Γ Center",
    },
    "delta": {
        "function": "nextgen_terminal.structure_levels",
        "field": "gd['delta_center']",
        "method": "trace_analytics · centroide ponderado por delta",
        "label": "Δ Center",
    },
    "zone": {
        "function": "nextgen_terminal.structure_levels",
        "field": "scanner['zone']['low'|'high']",
        "method": "scanner · zona operativa (no es un nivel de exposición)",
        "label": "Zona",
    },
    "target": {
        "function": "nextgen_terminal.structure_levels",
        "field": "scanner['target1'|'target2']",
        "method": "scanner · objetivo de la tesis direccional",
        "label": "Objetivo",
    },
    "risk": {
        "function": "nextgen_terminal.structure_levels",
        "field": "scanner['invalidation']",
        "method": "scanner · precio que invalida la tesis direccional",
        "label": "Invalidación",
    },
    "call_wall": {
        "function": "wall_engine.walls_from_hub",
        "field": "walls['call_wall']",
        "method": "wall_engine · gamma ITM por lado, sobre el snapshot del Hub",
        "label": "Call Wall",
    },
    "put_wall": {
        "function": "wall_engine.walls_from_hub",
        "field": "walls['put_wall']",
        "method": "wall_engine · gamma ITM por lado, sobre el snapshot del Hub",
        "label": "Put Wall",
    },
    "vol_trigger": {
        "function": "nextgen_terminal.structure_levels",
        "field": "structural_walls()['volatility_trigger']",
        "method": "trace_analytics.structural_walls · gatillo de volatilidad",
        "label": "Vol Trigger",
    },
    "hedge_wall": {
        "function": "nextgen_terminal.structure_levels",
        "field": "structural_walls()['hedge_wall']",
        "method": "trace_analytics.structural_walls · máximo de exposición firmada",
        "label": "Hedge Wall",
    },
    # v1.55.0 · Concentración de dark pool como LÍNEA. Es un precio con tamaño
    # detrás, igual que un muro, pero mide otra cosa: dinero cruzado fuera de
    # bolsa, no exposición de opciones. Comparten eje de precio y nada más.
    "dark_pool_wall": {
        "function": "dark_pool_view._dark_walls",
        "field": "dark_pool_levels['notionalValue'] por precio",
        "method": "dark_pool_view · concentración off-exchange por nivel de precio",
        "label": "Dark Pool",
    },
    # v1.54.0 · Las cuatro líneas del PLAN del Scanner. TRACE las representa;
    # el Scanner sigue siendo la única autoridad direccional.
    "scanner_entry": {
        "function": "scanner_plan.build",
        "field": "scanner['entry'] · scanner['zone']['center']",
        "method": "scanner · precio de entrada de la tesis activa",
        "label": "ENTRADA",
    },
    "scanner_inval": {
        "function": "scanner_plan.build",
        "field": "scanner['invalidation']",
        "method": "scanner · precio que invalida la tesis activa",
        "label": "INVAL",
    },
    "scanner_target1": {
        "function": "scanner_plan.build",
        "field": "scanner['target1']",
        "method": "scanner · primer objetivo de la tesis activa",
        "label": "OBJ1",
    },
    "scanner_target2": {
        "function": "scanner_plan.build",
        "field": "scanner['target2']",
        "method": "scanner · segundo objetivo de la tesis activa",
        "label": "OBJ2",
    },
}

#: Campos donde un nivel puede traer su fuerza. El primero que exista manda.
_MAGNITUDE_FIELDS = ("score", "magnitude", "gamma", "notional", "strength", "secondary")

#: Tolerancia para considerar que un nivel sigue "en el mismo sitio", como
#: fracción del propio precio. Relativa, no en dólares.
SAME_LEVEL_TOLERANCE = 0.0015

_LOCK = threading.Lock()
#: (symbol, kind) → {price, cycles, first_seen, last_seen}
_HISTORY: Dict[tuple, Dict[str, Any]] = {}


def _f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _persistence(symbol: str, kind: str, price: float, now: datetime) -> Dict[str, Any]:
    """Ciclos consecutivos que este nivel lleva publicado en el mismo sitio."""
    key = (str(symbol or "").upper(), str(kind or ""))
    tol = max(abs(price) * SAME_LEVEL_TOLERANCE, 1e-9)
    with _LOCK:
        prev = _HISTORY.get(key)
        prev_price = _f((prev or {}).get("price"))
        moved = prev_price is None or abs(prev_price - price) > tol
        if prev is None or moved:
            entry = {"price": price, "cycles": 1,
                     "first_seen": now.isoformat(), "last_seen": now.isoformat()}
        else:
            entry = {"price": price, "cycles": int(prev.get("cycles", 0)) + 1,
                     "first_seen": prev.get("first_seen") or now.isoformat(),
                     "last_seen": now.isoformat()}
        _HISTORY[key] = entry
    held = None
    try:
        first = datetime.fromisoformat(entry["first_seen"])
        held = round(max(0.0, (now - first).total_seconds() / 60.0), 1)
    except (ValueError, TypeError):
        held = None
    return {"cycles": entry["cycles"], "first_seen": entry["first_seen"],
            "held_minutes": held,
            "moved_this_cycle": bool(prev is not None and moved)}


#: Lo que se publica cuando una linea dibujada NO tiene identidad en el motor.
#: No es una etiqueta decorativa: es una DENUNCIA. Una linea sin nombre sobre un
#: grafico de operativa se ve, parece significar algo y no hay forma de saber
#: que. Mientras esto aparezca en el Auditor, hay un defecto sin cerrar.
UNIDENTIFIED = "UNIDENTIFIED_LEVEL"
IDENTIFIED = "IDENTIFIED"


def describe(levels: List[Dict[str, Any]], *, symbol: str,
             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Identidad de cada nivel publicado. NO calcula ni renombra nada.

    Devuelve una fila por nivel visible, en el mismo orden en que llegó, con
    la procedencia real y la persistencia medida entre ciclos.
    """
    ref = now or datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for lv in levels or []:
        if not isinstance(lv, dict):
            continue
        price = _f(lv.get("price"))
        if price is None:
            continue
        kind = str(lv.get("kind") or "")
        origin = LEVEL_ORIGIN.get(kind)
        magnitude = None
        magnitude_field = None
        for f in _MAGNITUDE_FIELDS:
            v = _f(lv.get(f))
            if v is not None:
                magnitude, magnitude_field = v, f
                break
        out.append({
            # v1.56.1 · `level_id` faltaba. Sin un identificador ESTABLE, dos
            # líneas del mismo tipo a precios distintos —dos objetivos, dos
            # zonas— no se pueden referenciar ni seguir entre ciclos: el
            # Auditor sólo podía decir «hay un `target`», no CUÁL.
            #
            # Se compone de tipo y precio a propósito: el mismo nivel en el
            # mismo sitio conserva su id entre ciclos, y uno que se mueve
            # estrena identidad, que es lo que hace que `persistence` signifique
            # algo.
            "level_id": f"{kind or 'SIN_KIND'}@{price:.4f}",
            "price": price,
            "type": kind or "SIN_KIND",
            # El nombre que la pantalla enseña: el del motor si lo publica, el
            # del registro si no. `engine_name` sigue viajando crudo al lado
            # para poder distinguir uno del otro.
            "name": lv.get("name") or (origin or {}).get("label") or kind or "SIN NOMBRE",
            # El nombre que el motor le puso, sin tocar. Si viene vacío, se dice.
            "engine_name": lv.get("name") or None,
            "source": (lv.get("authority") or (origin or {}).get("function")
                       or "DESCONOCIDO · el nivel no declara de qué función sale"),
            "source_field": (origin or {}).get("field"),
            "method": ((origin or {}).get("method")
                       or "DESCONOCIDO · este `kind` no está en LEVEL_ORIGIN"),
            "magnitude": magnitude,
            "magnitude_field": magnitude_field,
            "persistence": _persistence(symbol, kind, price, ref),
            "timestamp": ref.isoformat(),
            "source_mode": lv.get("source_mode"),
            "fallback_used": bool(lv.get("fallback_used")) if lv.get("fallback_used") is not None else None,
            # Un `kind` que el renderer no conoce se pinta con el color por
            # defecto y sin nombre. Es exactamente el caso que hay que ver.
            "known_to_registry": origin is not None,
            # DENUNCIA explicita. Una linea es identificable cuando el motor
            # dice de donde sale (registro) O ella misma declara su autoridad.
            # Las dos cosas ausentes = linea anonima sobre un grafico de dinero.
            "identity_status": (IDENTIFIED if (origin is not None or lv.get("authority"))
                                else UNIDENTIFIED),
        })
    return out


def audit(levels: List[Dict[str, Any]], *, symbol: str,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """Identidad de cada linea MAS el recuento de las que no la tienen.

    El recuento es lo que convierte esto en un guardia: `describe` solo describe,
    y una lista larga de filas correctas esconde bien las dos que no lo son.
    """
    rows = describe(levels, symbol=symbol, now=now)
    sin_identidad = [r for r in rows if r["identity_status"] == UNIDENTIFIED]
    return {
        "rows": rows,
        "total": len(rows),
        "unidentified": len(sin_identidad),
        "unidentified_kinds": sorted({r["type"] for r in sin_identidad}),
        "ok": not sin_identidad,
        "detail": ("todas las lineas dibujadas tienen identidad del motor"
                   if not sin_identidad else
                   f"{len(sin_identidad)} linea(s) sin identidad: "
                   + ", ".join(sorted({r['type'] or 'SIN_KIND' for r in sin_identidad}))),
        "symbol": str(symbol or "").upper(),
    }


def reset() -> None:
    """Olvida la historia de persistencia. Para pruebas."""
    with _LOCK:
        _HISTORY.clear()
