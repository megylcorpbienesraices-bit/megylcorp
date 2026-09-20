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
    "executionSide", "execution_side", "sentiment", "side",
)


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


def from_row(row: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Lado agresor de una fila del proveedor, y de QUÉ CAMPO salió.

    Devolver el campo usado no es un adorno: cuando el proveedor cambie de
    convenio, lo que se necesita saber es qué clave se estaba leyendo, y sin
    eso hay que volver a adivinar desde cero.
    """
    if not isinstance(row, dict):
        return UNKNOWN, None
    for key in AGGRESSOR_FIELDS:
        if key not in row:
            continue
        verdict = classify(row.get(key))
        if verdict != UNKNOWN:
            return verdict, key
    # Ningún campo resolvió el lado. Se declara el que existía, para poder
    # diagnosticar sin tener que capturar la respuesta entera otra vez.
    seen = next((k for k in AGGRESSOR_FIELDS if k in row), None)
    return UNKNOWN, seen
