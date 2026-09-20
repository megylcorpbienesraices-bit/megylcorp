"""Clasificación del LADO AGRESOR de una operación. Autoridad única.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ ESTE MÓDULO EXISTE
═══════════════════════════════════════════════════════════════════════════

Una marca de flujo que dice COMPRA cuando fue VENTA induce a operar al revés.
Es el único sitio del programa donde un error de una línea sale directamente
en dinero, así que la regla vive en un solo fichero, se lee entera de una vez
y tiene pruebas propias.

Hasta v1.51.0 la clasificación estaba repartida y usaba prefijos de UNA letra:

    direction = 1 if side.startswith(("BUY", "ASK", "A")) else \\
                -1 if side.startswith(("SELL", "BID", "B")) else 0

Ese `"A"` suelto es el fallo. Los proveedores publican el lado como posición
respecto al NBBO, y dos de los valores más frecuentes son:

    AT_ASK      el comprador cruzó el spread   →  COMPRA
    AT_BID      el vendedor cruzó el spread    →  VENTA

`"AT_BID"` empieza por `"A"`, así que la regla anterior lo clasificaba como
COMPRA. Es exactamente la inversión que no puede ocurrir. Lo mismo con
`"ABOVE_BID"` (venta agresiva por encima del bid, pero empieza por A).

═══════════════════════════════════════════════════════════════════════════
LA REGLA
═══════════════════════════════════════════════════════════════════════════

Se busca por SUBCADENA y en orden de especificidad, nunca por prefijo corto:

  1. Palabras explícitas de dirección:  BOUGHT/BUY → compra, SOLD/SELL → venta.
  2. Posición respecto al NBBO:         …ASK… → compra, …BID… → venta.
     Vale para AT_ASK, ABOVE_ASK, BELOW_BID, AT_BID y sus variantes.
  3. Convenios numéricos:               +1/-1, o el signo de un número.
  4. Todo lo demás —MID, MIDPOINT, BETWEEN, NO_SIDE, vacío, CALL, PUT—
     es DESCONOCIDO, y se publica como tal.

NO se adivina desde el tipo de contrato. CALL y PUT no son direcciones: una
put se compra y eso es una compra. Un valor que sólo diga CALL o PUT se
clasifica como DESCONOCIDO, no como compra ni como venta.

NO se adivina desde el signo de la prima. La prima neta de un intervalo es
call menos put, que tampoco es una dirección.

═══════════════════════════════════════════════════════════════════════════
DESCONOCIDO NO ES NEUTRO
═══════════════════════════════════════════════════════════════════════════

`UNKNOWN` significa «no se midió», y la interfaz lo dibuja como rombo neutro.
`MID` significa «se ejecutó en el medio, sin agresor claro», que es una
observación real y también se publica sin inventar un lado. Ninguno de los dos
se convierte en compra por defecto.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

BUY = "BUY"
SELL = "SELL"
UNKNOWN = "UNKNOWN"

#: El tipo de contrato NO es una dirección. Si el campo sólo dice esto, no hay lado.
_CONTRACT_WORDS = ("CALL", "PUT", "C", "P")

#: Ejecutado en el medio: no hubo agresor que cruzara el spread. Es una
#: observación real, distinta de «no lo sabemos».
_MID_WORDS = ("MID", "MIDPOINT", "BETWEEN", "NBBO_MID")

#: Campos donde los proveedores publican el lado agresor, en orden de confianza.
#: `aggressor` y `tradeSide` son inequívocos; `side` es ambiguo —en algunas
#: respuestas es el tipo de contrato— y por eso va el último.
AGGRESSOR_FIELDS: Tuple[str, ...] = (
    "aggressor", "aggressorSide", "tradeSide", "trade_side",
    "executionSide", "execution_side", "sentiment", "direction",
    "tradeDirection", "flowSide", "side",
)

#: Precios del NBBO en el instante de la operación, con sus alias.
_ASK_FIELDS: Tuple[str, ...] = ("ask", "askPrice", "nbboAsk", "askAtTrade", "bestAsk")
_BID_FIELDS: Tuple[str, ...] = ("bid", "bidPrice", "nbboBid", "bidAtTrade", "bestBid")
_PRICE_FIELDS: Tuple[str, ...] = ("price", "tradePrice", "executionPrice", "optionPrice")

#: Holgura al comparar el precio contra el NBBO, como fracción del spread.
#: Sin ella, un redondeo de un céntimo deja fuera una operación que cruzó.
NBBO_TOLERANCE = 0.05


def classify(raw: Any) -> str:
    """Devuelve BUY, SELL o UNKNOWN a partir del valor crudo del proveedor."""
    if raw is None:
        return UNKNOWN

    # Convenio numérico: +1 comprador, −1 vendedor, 0 sin clasificar.
    if isinstance(raw, bool):
        return UNKNOWN
    if isinstance(raw, (int, float)):
        if raw > 0:
            return BUY
        if raw < 0:
            return SELL
        return UNKNOWN

    text = str(raw).strip().upper()
    if not text:
        return UNKNOWN

    # 1 · Palabras explícitas de dirección. Van primero porque son inequívocas.
    if "BOUGHT" in text or "BUY" in text:
        return BUY
    if "SOLD" in text or "SELL" in text:
        return SELL

    # 2 · Posición respecto al NBBO. Se busca la subcadena, NUNCA un prefijo
    #     corto: `AT_BID` empieza por A y no por eso es una compra.
    has_ask = "ASK" in text or "OFFER" in text
    has_bid = "BID" in text
    if has_ask and not has_bid:
        return BUY
    if has_bid and not has_ask:
        return SELL
    if has_ask and has_bid:
        # Un valor con las dos palabras no dice de qué lado fue.
        return UNKNOWN

    # 3 · Sólo el tipo de contrato: no es una dirección.
    if text in _CONTRACT_WORDS:
        return UNKNOWN

    # 4 · Ejecutado en el medio: sin agresor. No es compra.
    if any(w in text for w in _MID_WORDS):
        return UNKNOWN

    return UNKNOWN


def direction(raw: Any) -> int:
    """+1 compra, −1 venta, 0 sin clasificar. El mismo convenio en todo el programa."""
    verdict = classify(raw)
    return 1 if verdict == BUY else -1 if verdict == SELL else 0


def _num(row: Dict[str, Any], names: Tuple[str, ...]) -> Optional[float]:
    for key in names:
        if key in row:
            v = _as_float(row.get(key))
            if v is not None:
                return v
    return None


def _as_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def from_nbbo(row: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Lado agresor DEDUCIDO del precio contra el NBBO del momento.

    v1.52.1 · Esta vía existe porque la anterior no bastaba en producción: con
    datos reales TODAS las marcas salían neutras, y una marca neutra no informa
    de nada aunque sea honesta. La causa es que el proveedor no siempre publica
    un campo de lado; lo que sí publica en la cinta de opciones es el precio de
    la operación y el mejor bid/ask del instante.

    Comparar los dos NO es adivinar: es la definición operativa del agresor.
    Quien paga el ask está cruzando el spread hacia arriba —comprador agresivo—
    y quien vende al bid lo cruza hacia abajo. Es el mismo criterio que usa
    cualquier cinta profesional.

        precio >= ask − holgura   →  COMPRA
        precio <= bid + holgura   →  VENTA
        entre medias              →  UNKNOWN (ejecutado en el medio, sin agresor)

    La holgura es una fracción del spread, no un número en dólares: un céntimo
    es mucho en una opción de 0.05 y nada en una de 40. Sin ella, un redondeo
    dejaba fuera operaciones que sí cruzaron.

    Nunca sustituye al campo declarado: sólo se consulta cuando no hay campo, y
    el resultado viaja etiquetado como `NBBO` para que se sepa de dónde salió.
    """
    if not isinstance(row, dict):
        return UNKNOWN, None
    price = _num(row, _PRICE_FIELDS)
    ask = _num(row, _ASK_FIELDS)
    bid = _num(row, _BID_FIELDS)
    if price is None or ask is None or bid is None:
        return UNKNOWN, None
    if not (ask > 0 and bid > 0) or ask < bid:
        return UNKNOWN, None
    spread = ask - bid
    tol = spread * NBBO_TOLERANCE
    if spread <= 0:
        # Mercado cruzado o bloqueado: el precio no distingue lados.
        return UNKNOWN, None
    if price >= ask - tol:
        return BUY, "NBBO"
    if price <= bid + tol:
        return SELL, "NBBO"
    return UNKNOWN, "NBBO"


def from_row(row: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Lado agresor de una fila del proveedor, y de QUÉ CAMPO salió.

    Devolver el campo usado no es un adorno: cuando el proveedor cambie de
    convenio, lo que se necesita saber es qué clave se estaba leyendo, y sin
    eso hay que volver a adivinar desde cero.

    Orden: primero el campo DECLARADO por el proveedor; si ninguno resuelve, el
    NBBO, que es medición y no heurística. Si tampoco, UNKNOWN con la clave que
    sí existía, para poder diagnosticar sin recapturar la respuesta entera.
    """
    if not isinstance(row, dict):
        return UNKNOWN, None
    for key in AGGRESSOR_FIELDS:
        if key not in row:
            continue
        verdict = classify(row.get(key))
        if verdict != UNKNOWN:
            return verdict, key
    verdict, how = from_nbbo(row)
    if verdict != UNKNOWN:
        return verdict, how
    seen = next((k for k in AGGRESSOR_FIELDS if k in row), None)
    return UNKNOWN, seen or how
