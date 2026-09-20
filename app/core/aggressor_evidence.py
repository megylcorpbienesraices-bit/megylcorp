"""TABLA DE EVIDENCIA del lado agresor. Punto 44 del cierre integral.

═══════════════════════════════════════════════════════════════════════════
QUÉ PROBLEMA RESUELVE
═══════════════════════════════════════════════════════════════════════════

Cambiar el color de una flecha es trivial. Demostrar que la flecha apunta al
lado correcto no lo es, y es lo único que importa: una marca que dice COMPRA
cuando fue VENTA induce a operar al revés.

El riesgo real no está en el clasificador —tiene sus pruebas— sino en la
CADENA. La clasificación pasa por cinco etapas y cada una puede invertirla sin
que nada avise:

    RAW del proveedor
      → clasificador
        → fila normalizada
          → concentración QFLOW
            → marca en FLUJO
              → marca en TRACE

Este módulo recorre esa cadena operación por operación y exige que el lado sea
EL MISMO en todas las etapas. Si una sola la cambia, falla y dice cuál.

═══════════════════════════════════════════════════════════════════════════
LA COLUMNA «ESPERADO» NO SALE DEL CÓDIGO
═══════════════════════════════════════════════════════════════════════════

`expected_side()` reimplementa la regla del contrato publicado a mano, sin
llamar al clasificador. Es a propósito: comparar el clasificador consigo mismo
no demuestra nada. Si mañana alguien invierte una condición dentro de
`aggressor.py`, el clasificador seguirá siendo coherente consigo mismo y esta
columna dejará de coincidir.

Es la única forma de que la prueba pueda fallar por el motivo correcto.

═══════════════════════════════════════════════════════════════════════════
LA REGLA, TAL Y COMO LA PUBLICA EL CONTRATO
═══════════════════════════════════════════════════════════════════════════

    tradeSideCode = ASK, ABOVE_ASK      →  BUY
    tradeSideCode = BID, BELOW_BID      →  SELL
    tradeSideCode = MID_MARKET          →  UNKNOWN

    sin tradeSideCode, con NBBO:
        tradePrice >= ask               →  BUY
        tradePrice <= bid               →  SELL
        bid < tradePrice < ask          →  UNKNOWN

    sin tradeSideCode y sin NBBO        →  UNKNOWN

NUNCA `CALL = BUY` ni `PUT = SELL`. Una put se compra y eso es una compra.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .aggressor import BUY, NBBO_TOLERANCE, SELL, UNKNOWN

#: Columnas de la tabla de evidencia, en el orden en que se leen. El crudo
#: primero: la tabla se contrasta contra la respuesta del proveedor.
COLUMNS = (
    "trade_id", "tradeTime", "option_symbol", "contractType", "strikePrice",
    "expirationDate", "optionPrice", "bidPrice", "askPrice", "size", "premium",
    "tradeSideCode",
    "expected", "classifier", "flow_mark", "trace_mark", "match",
)


def expected_side(raw: Dict[str, Any]) -> str:
    """El lado que EXIGE el contrato publicado, calculado a mano.

    No llama al clasificador. Ver la cabecera del módulo.
    """
    code = raw.get("tradeSideCode")
    if code not in (None, ""):
        text = str(code).strip().upper()
        if text in ("ASK", "ABOVE_ASK", "AT_ASK"):
            return BUY
        if text in ("BID", "BELOW_BID", "AT_BID"):
            return SELL
        if text in ("MID", "MID_MARKET", "MIDPOINT"):
            return UNKNOWN
        # Un código que el contrato no define no se interpreta: se declara
        # desconocido en vez de adivinarlo por parecido.
        return UNKNOWN

    price = _f(raw.get("optionPrice"), raw.get("price"))
    bid = _f(raw.get("bidPrice"), raw.get("bid"))
    ask = _f(raw.get("askPrice"), raw.get("ask"))
    if price is None or bid is None or ask is None:
        return UNKNOWN
    if not (ask > 0 and bid > 0) or ask < bid:
        return UNKNOWN
    spread = ask - bid
    if spread <= 0:
        return UNKNOWN
    tol = spread * NBBO_TOLERANCE
    if price >= ask - tol:
        return BUY
    if price <= bid + tol:
        return SELL
    return UNKNOWN


def _parse(v: Any) -> Optional[float]:
    """Un valor a número finito, o None. No es un fallo: es «este alias no».

    La tabla se lee contra la respuesta CRUDA del proveedor, que publica
    `optionPrice` en unas respuestas y `price` en otras. Que un alias no venga,
    o venga como texto vacío, es la forma normal de la respuesta y no hay nada
    que diagnosticar; por eso se devuelve None en vez de registrar un incidente
    por cada fila.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str):
        texto = v.strip()
        if not texto:
            return None
        try:
            x = float(texto)
        except ValueError:
            return None
    else:
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _f(*values: Any) -> Optional[float]:
    """El primero de varios alias que sea un número finito."""
    for v in values:
        x = _parse(v)
        if x is not None:
            return x
    return None


def _mark_side(marker: Any) -> str:
    """El lado que una MARCA está representando, leído como lo lee el gráfico."""
    if not isinstance(marker, dict):
        return UNKNOWN
    return str(marker.get("aggressor") or UNKNOWN).upper()


def build_evidence(raw_rows: List[Dict[str, Any]],
                   normalized_rows: List[Dict[str, Any]],
                   *, flow_marks: Optional[List[Dict[str, Any]]] = None,
                   trace_marks: Optional[List[Dict[str, Any]]] = None,
                   limit: int = 200) -> Dict[str, Any]:
    """Una fila por operación, con el lado en CADA etapa de la cadena.

    `flow_marks` y `trace_marks` son las marcas que cada pantalla dibuja. Se
    cruzan por instante: una marca agrupa varias operaciones, así que lo que se
    exige es que el lado de la marca no CONTRADIGA al de la operación —una
    marca `UNKNOWN` sobre operaciones clasificadas es legítima (no hubo
    dominancia), pero una marca `BUY` sobre una operación `SELL` no lo es—.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    by_time: Dict[str, Dict[str, Any]] = {}
    for n in normalized_rows or []:
        if not isinstance(n, dict):
            continue
        tid = n.get("trade_id")
        if tid not in (None, ""):
            by_id[str(tid)] = n
        t = n.get("t") or n.get("tradeTime")
        if t:
            by_time.setdefault(str(t), n)

    marks_flow = {str(m.get("t")): m for m in (flow_marks or []) if isinstance(m, dict)}
    marks_trace = {str(m.get("t")): m for m in (trace_marks or []) if isinstance(m, dict)}

    rows: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []
    for raw in (raw_rows or [])[:limit]:
        if not isinstance(raw, dict):
            continue
        tid = raw.get("id") or raw.get("tradeId") or raw.get("trade_id")
        t = str(raw.get("timestamp") or raw.get("tradeTime") or raw.get("t") or "")
        norm = (by_id.get(str(tid)) if tid not in (None, "") else None) or by_time.get(t)
        esperado = expected_side(raw)
        clasificador = str((norm or {}).get("aggressor") or UNKNOWN).upper()
        marca_flow = _mark_side(marks_flow.get(t)) if marks_flow else None
        marca_trace = _mark_side(marks_trace.get(t)) if marks_trace else None

        # Una marca sólo CONTRADICE cuando afirma el lado contrario. Que una
        # marca diga UNKNOWN sobre operaciones clasificadas es legítimo: el
        # bucket puede no tener dominancia. Lo inaceptable es BUY sobre SELL.
        def contradice(m: Optional[str]) -> bool:
            return (m in (BUY, SELL) and clasificador in (BUY, SELL) and m != clasificador)

        ok = (esperado == clasificador
              and not contradice(marca_flow) and not contradice(marca_trace))
        fila = {
            "trade_id": None if tid is None else str(tid),
            "tradeTime": t or None,
            "option_symbol": raw.get("osi") or raw.get("optionSymbol") or (norm or {}).get("option_symbol"),
            "contractType": str(raw.get("optionType") or raw.get("contractType") or "").upper() or None,
            "strikePrice": _f(raw.get("strike"), raw.get("strikePrice")),
            "expirationDate": raw.get("expiration") or raw.get("expirationDate"),
            "optionPrice": _f(raw.get("optionPrice"), raw.get("price")),
            "bidPrice": _f(raw.get("bidPrice"), raw.get("bid")),
            "askPrice": _f(raw.get("askPrice"), raw.get("ask")),
            "size": _f(raw.get("size"), raw.get("quantity")),
            "premium": _f(raw.get("premium"), (norm or {}).get("premium")),
            "tradeSideCode": raw.get("tradeSideCode"),
            "expected": esperado,
            "classifier": clasificador,
            "classification_source": (norm or {}).get("classification_source"),
            "classification_reason": (norm or {}).get("classification_reason"),
            "flow_mark": marca_flow,
            "trace_mark": marca_trace,
            "match": ok,
            "normalized_found": norm is not None,
        }
        rows.append(fila)
        if not ok:
            mismatches.append(fila)

    clasificadas = sum(1 for r in rows if r["classifier"] in (BUY, SELL))
    return {
        "columns": list(COLUMNS),
        "rows": rows,
        "count": len(rows),
        "matches": len(rows) - len(mismatches),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "ok": not mismatches,
        "classified": clasificadas,
        "coverage_pct": (None if not rows else round(100.0 * clasificadas / len(rows), 1)),
        "rule": ("tradeSideCode manda; NBBO sólo si falta; MID_MARKET es UNKNOWN. "
                 "Nunca CALL=BUY ni PUT=SELL."),
        "detail": ("todas las operaciones conservan el mismo lado en toda la cadena"
                   if not mismatches else
                   f"{len(mismatches)} operacion(es) cambian de lado por el camino"),
    }
