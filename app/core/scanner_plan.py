"""El PLAN del Scanner, tal y como TRACE lo va a representar.

═══════════════════════════════════════════════════════════════════════════
LA REGLA QUE ESTE MÓDULO PROTEGE
═══════════════════════════════════════════════════════════════════════════

**El Scanner es la única autoridad direccional.** TRACE representa su
resultado; no lo recalcula, no lo corrige y no lo completa.

Eso suena obvio y es justo lo que se rompe solo. En cuanto una pantalla
necesita un precio de entrada y el Scanner no lo publica, la tentación es
derivarlo —«pues el centro de la zona»— y a partir de ahí hay dos motores
direccionales: el que decide y el que dibuja. Cuando discrepan, nadie sabe
cuál mirar.

Aquí no se deriva nada. Si el Scanner no publica entrada, objetivos o
invalidación, el plan sale `ESPERANDO` y el gráfico no dibuja esas líneas.

═══════════════════════════════════════════════════════════════════════════
TRANSACCIONAL
═══════════════════════════════════════════════════════════════════════════

Cuando la tesis cambia, TODAS las líneas cambian juntas. Un plan mitad viejo
y mitad nuevo —entrada de la señal anterior con objetivos de la nueva— es
peor que no tener plan: parece coherente y no lo es.

Por eso el plan lleva `thesis_id`: un huella del conjunto (dirección +
entrada + invalidación + objetivos). Si cualquiera cambia, el id cambia, y el
renderer sustituye el bloque entero en vez de actualizar línea a línea.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Estados del plan.
ACTIVE = "ACTIVO"
WAITING = "ESPERANDO"
INVALIDATED = "INVALIDADO"
TARGET_HIT = "OBJETIVO ALCANZADO"

#: Direcciones que el Scanner publica y que TRACE está autorizado a mostrar.
_BUY = {"BUY", "COMPRA", "LONG"}
_SELL = {"SELL", "VENTA", "SHORT"}


def _f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _direction(raw: Any) -> Optional[str]:
    """COMPRA, VENTA o None. Nunca se deduce de otra cosa."""
    t = str(raw or "").strip().upper()
    if t in _BUY:
        return "COMPRA"
    if t in _SELL:
        return "VENTA"
    return None


def build(scanner: Dict[str, Any], *, spot: Optional[float] = None,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """El plan que TRACE dibuja. Sólo lee; no calcula ninguna dirección.

    `scanner` es la salida del motor tal cual. Lo único que se hace aquí es
    seleccionar, nombrar y decidir el estado — y declarar qué falta cuando
    falta.
    """
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    sc = scanner if isinstance(scanner, dict) else {}

    direction = _direction(sc.get("direction"))
    strength = _f(sc.get("evidence_score"))
    if strength is None:
        strength = _f(sc.get("evidence"))
    entry = _f(sc.get("entry"))
    if entry is None:
        # El Scanner publica la zona operativa; su centro ES la entrada que el
        # propio Scanner declara, no una derivación nuestra. Si tampoco lo
        # publica, no hay entrada y se dice.
        entry = _f((sc.get("zone") or {}).get("center"))
    inval = _f(sc.get("invalidation"))
    t1 = _f(sc.get("target1"))
    t2 = _f(sc.get("target2"))
    if t1 is None or t2 is None:
        tg = sc.get("targets")
        if isinstance(tg, (list, tuple)):
            vals = [_f(x) for x in tg]
            vals = [v for v in vals if v is not None]
            if t1 is None and len(vals) > 0:
                t1 = vals[0]
            if t2 is None and len(vals) > 1:
                t2 = vals[1]

    missing = [name for name, v in (("dirección", direction), ("entrada", entry),
                                    ("invalidación", inval), ("objetivo 1", t1))
               if v is None]

    # Estado. `edge_state` del Scanner manda cuando dice algo concluyente.
    edge = str(sc.get("edge_state") or "").upper()
    if missing or not sc.get("ready"):
        state = WAITING
    elif "INVALID" in edge:
        state = INVALIDATED
    elif "TARGET" in edge or "OBJETIVO" in edge:
        state = TARGET_HIT
    else:
        state = ACTIVE

    # Si el precio ya cruzó la invalidación, el plan está invalidado aunque el
    # Scanner aún no lo haya recalculado. NO es recalcular dirección: es leer
    # el nivel que el propio Scanner publicó.
    s = _f(spot)
    crossed = None
    if state == ACTIVE and s is not None and inval is not None and direction:
        crossed = (s <= inval) if direction == "COMPRA" else (s >= inval)
        if crossed:
            state = INVALIDATED

    lines = []
    if state != WAITING:
        for name, price, kind in (("ENTRADA", entry, "scanner_entry"),
                                  ("INVAL", inval, "scanner_inval"),
                                  ("OBJ1", t1, "scanner_target1"),
                                  ("OBJ2", t2, "scanner_target2")):
            if price is None:
                continue
            lines.append({
                "name": name, "price": price, "kind": kind,
                "direction": direction, "strength": strength,
                "source": "SCANNER", "timestamp": ref.isoformat(),
                "distance": None if s is None else round(price - s, 4),
            })

    thesis_id = hashlib.sha256("|".join([
        str(direction), f"{entry}", f"{inval}", f"{t1}", f"{t2}",
    ]).encode("utf-8")).hexdigest()[:12]

    return {
        "ready": state != WAITING,
        "state": state,
        "direction": direction,
        "strength": None if strength is None else round(max(0.0, min(100.0, strength)), 0),
        "entry": entry, "invalidation": inval, "target1": t1, "target2": t2,
        "lines": lines,
        # Huella del CONJUNTO. Si cualquier pieza cambia, cambia el id y el
        # renderer sustituye el bloque entero: nunca media tesis vieja.
        "thesis_id": thesis_id,
        "missing": missing,
        "detail": ("" if state != WAITING else
                   ("el Scanner no publica " + ", ".join(missing)) if missing
                   else "el Scanner todavía no tiene una tesis lista"),
        "invalidation_crossed": crossed,
        "source": "SCANNER",
        "authority": "SCANNER_IS_THE_ONLY_DIRECTIONAL_AUTHORITY",
        "timestamp": ref.isoformat(),
    }
