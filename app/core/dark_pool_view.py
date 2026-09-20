"""DarkPoolViewModel · la ÚNICA salida que consume la pantalla Dark Pool.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ EXISTE
═══════════════════════════════════════════════════════════════════════════

El Auditor mostraba, en la misma pantalla y al mismo tiempo:

    Dark Flow         DATO DIRECTO · 608 filas
    Dark Pool Levels  DATO DIRECTO · 349 filas

y los paneles de la sección decían:

    DARK POOL · off-exchange   SIN DATOS
    DARK POOL · niveles        SIN DATOS

Dos causas, y las dos eran de arquitectura, no del proveedor:

1. **La heurística de campos.** `dark-flow` publica las acciones en `size`, y
   el normalizador intentaba "descubrir" el campo buscando nombres que
   contuvieran «dark» u «offExchange». `size` no encaja en ninguno, así que
   las 608 filas llegaban con `dark_volume = None`. Corregido en
   `norm_dark_flow` con el mapeo determinista del contrato.

2. **La vista no leía al proveedor.** Los paneles colgaban de
   `EQUITY_TAPE + VENUE_CONFIRMED` y de `ITM_QUANT_LIQUIDITY_ZONES`, que son
   capas DERIVADAS: infieren el dark pool del campo `venue` de otra cinta.
   Mientras esas capas no confirmaran, la sección decía SIN DATOS aunque el
   dato directo estuviera descargado. Una fuente derivada no puede bloquear a
   la fuente directa.

Este módulo corta las dos: arma un único modelo desde los tres carriles del
proveedor, y la pantalla no mira a ningún otro sitio.

═══════════════════════════════════════════════════════════════════════════
LA REGLA DE CERO Y HUECO
═══════════════════════════════════════════════════════════════════════════

    valor observado que vale cero   →  0
    campo ausente o respuesta corta →  None, con estado UNAVAILABLE
                                       o SCHEMA_MISMATCH

Nunca `value or 0.0` en el camino del proveedor. Un cero afirma «no hubo
volumen oscuro»; un hueco dice «no lo hemos medido», y no son lo mismo.

═══════════════════════════════════════════════════════════════════════════
QUÉ SALE DE DÓNDE
═══════════════════════════════════════════════════════════════════════════

    NOTIONAL OFF-EXCHANGE   Σ notionalValue   de Dark Flow
    VOLUMEN OSCURO          Σ size            de Dark Flow
    PRINTS OSCUROS          Σ tradeCount      de Dark Flow
    NIVEL DOMINANTE         max notionalValue de Dark Pool Levels
    PRINT MAYOR             max notionalValue de Equity Prints DARK_POOL
    VWAP DARK POOL          Σ(precio × size) / Σ(size) sobre prints DARK_POOL
    % FUERA DE BOLSA        sólo con universo dark + lit completo

El porcentaje fuera de bolsa **no se estima**. `dark-flow` entrega sólo lo
off-exchange, así que su denominador no existe ahí; sin un universo completo
de Equity Prints dark + lit, el KPI va en None y la pantalla dice SIN DATOS.
Publicar un porcentaje sobre un denominador parcial daría una cifra que se
leería como cuota de mercado sin serlo.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Estados de un carril. Se corresponden con los de `data_lineage`, más el de
#: contrato incumplido, que es distinto de «no vino nada».
DATA_OK = "DATA_OK"
NO_PROVIDER_DATA = "NO_PROVIDER_DATA"
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
PROVIDER_ERROR = "PROVIDER_ERROR"
MARKET_CLOSED = "MARKET_CLOSED"
UNAVAILABLE = "UNAVAILABLE"


def _f(v: Any) -> Optional[float]:
    """Número finito, o None. Nunca un cero por defecto."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _sum(rows: List[Dict[str, Any]], key: str) -> Tuple[Optional[float], int]:
    """Suma sólo lo MEDIDO, y dice sobre cuántas filas se midió.

    Devolver el recuento junto a la suma es lo que permite distinguir «sumó
    cero porque todo valía cero» de «sumó cero porque no había nada que sumar».
    """
    total = 0.0
    seen = 0
    for r in rows:
        v = _f(r.get(key))
        if v is None:
            continue
        total += v
        seen += 1
    return (total if seen else None), seen


def _lane_status(block: Any, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Estado de un carril, con su causa. Un carril mudo no es auditable."""
    if not isinstance(block, dict):
        return {"state": NO_PROVIDER_DATA, "rows": 0,
                "detail": "el proveedor no devolvió este carril en el ciclo"}
    err = block.get("error") or block.get("lane_error")
    if err:
        return {"state": PROVIDER_ERROR, "rows": len(rows), "detail": str(err)[:240]}
    schema = str(block.get("schema_state") or "")
    if schema == "SCHEMA_MISMATCH":
        return {"state": SCHEMA_MISMATCH, "rows": len(rows),
                "detail": block.get("schema_detail") or "el contrato no se cumple",
                "observed_fields": block.get("observed_fields") or []}
    if not block.get("ready"):
        return {"state": NO_PROVIDER_DATA, "rows": len(rows),
                "detail": block.get("reason") or block.get("detail")
                or "el carril no está listo en este ciclo"}
    if not rows:
        return {"state": NO_PROVIDER_DATA, "rows": 0,
                "detail": block.get("reason") or "respuesta válida sin filas"}
    return {"state": DATA_OK, "rows": len(rows),
            "detail": f"{len(rows)} filas del proveedor",
            "field_map": block.get("field_map") or {}}


def build(*, flow_block: Any, levels_block: Any, prints_block: Any,
          spot: Optional[float] = None,
          session: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Arma el modelo desde los TRES carriles del proveedor y nada más.

    No recibe la cinta de equity propia, ni las zonas de liquidez derivadas, ni
    la clasificación por venue. Eso es deliberado: esas capas siguen existiendo
    para la auditoría, pero no pueden decidir si esta pantalla tiene datos.
    """
    flow_rows = _rows_of(flow_block)
    level_rows = _rows_of(levels_block)
    print_rows = _rows_of(prints_block)

    status = {
        "dark_flow": _lane_status(flow_block, flow_rows),
        "dark_pool_levels": _lane_status(levels_block, level_rows),
        "equity_prints": _lane_status(prints_block, print_rows),
    }

    # ── Dark Flow ────────────────────────────────────────────────────────────
    buckets: List[Dict[str, Any]] = []
    for r in flow_rows:
        buckets.append({
            "t": r.get("t"),
            "notional": _f(r.get("dark_notional")),
            "shares": _f(r.get("dark_volume")),
            "prints": _f(r.get("dark_prints")),
            "price": _f(r.get("stock_price")),
        })
    dark_notional, _n1 = _sum(buckets, "notional")
    dark_volume, _n2 = _sum(buckets, "shares")
    dark_prints, _n3 = _sum(buckets, "prints")

    # ── Dark Pool Levels ─────────────────────────────────────────────────────
    levels: List[Dict[str, Any]] = []
    for r in level_rows:
        price = _f(r.get("price"))
        if price is None:
            continue
        levels.append({
            "price": price,
            "notional": _f(r.get("notional")),
            "shares": _f(r.get("shares")),
            "prints": _f(r.get("prints")),
            "distance_pct": (None if not spot else
                             round((price - spot) / spot * 100.0, 3)),
        })
    levels.sort(key=lambda x: -(x["notional"] or 0.0))
    dominant = levels[0] if levels and levels[0]["notional"] is not None else None

    # ── Equity Prints ────────────────────────────────────────────────────────
    # Sólo los clasificados DARK_POOL por el propio proveedor. Un print sin
    # clasificación no se cuenta como oscuro «por si acaso».
    dark_prints_rows = [r for r in print_rows if r.get("off_exchange") is True]
    prints: List[Dict[str, Any]] = []
    for r in dark_prints_rows:
        prints.append({
            "t": r.get("t"),
            "price": _f(r.get("price")),
            "shares": _f(r.get("size")),
            "notional": _f(r.get("notional")),
            "venue": r.get("venue"),
        })
    largest = None
    with_notional = [p for p in prints if p["notional"] is not None]
    if with_notional:
        largest = max(with_notional, key=lambda p: p["notional"])

    # VWAP oscuro: Σ(precio × acciones) / Σ(acciones) sobre prints DARK_POOL.
    num = den = 0.0
    for p in prints:
        px, sh = p["price"], p["shares"]
        if px is None or sh is None or sh <= 0:
            continue
        num += px * sh
        den += sh
    dark_vwap = (num / den) if den > 0 else None

    # ── % fuera de bolsa ─────────────────────────────────────────────────────
    # Requiere el universo COMPLETO: dark + lit. `dark-flow` sólo trae lo
    # oscuro, así que su denominador no existe. Con Equity Prints completos sí.
    lit_shares = 0.0
    lit_seen = 0
    for r in print_rows:
        if r.get("off_exchange") is not False:
            continue
        v = _f(r.get("size"))
        if v is None:
            continue
        lit_shares += v
        lit_seen += 1
    dark_share = None
    dark_share_basis = None
    if lit_seen and prints:
        dark_only, _ = _sum(prints, "shares")
        if dark_only is not None and (dark_only + lit_shares) > 0:
            dark_share = round(100.0 * dark_only / (dark_only + lit_shares), 2)
            dark_share_basis = "EQUITY_PRINTS_DARK_PLUS_LIT"

    covered = sum(1 for s in status.values() if s["state"] == DATA_OK)
    # CONTEO POR ETAPA. El criterio de cierre exige que el numero de filas no se
    # pierda por el camino: 608 en el proveedor tienen que seguir siendo 608 en
    # el modelo. Publicarlo aqui es lo que permite comprobarlo sin instrumentar
    # nada: si una etapa recorta, se ve donde y se puede exigir el filtro
    # explicito que lo justifique.
    lineage = {
        "dark_flow": {"provider": len(flow_rows), "view_model": len(buckets),
                      "dropped": len(flow_rows) - len(buckets)},
        "dark_pool_levels": {"provider": len(level_rows), "view_model": len(levels),
                             "dropped": len(level_rows) - len(levels),
                             "drop_reason": ("niveles sin precio utilizable"
                                             if len(level_rows) != len(levels) else None)},
        "equity_prints": {"provider": len(print_rows), "view_model": len(prints),
                          "dropped": len(print_rows) - len(prints),
                          "drop_reason": ("prints no clasificados DARK_POOL por el proveedor"
                                          if len(print_rows) != len(prints) else None)},
    }
    return {
        "ready": covered > 0,
        "flow": {"rows": buckets, "buckets": buckets, "count": len(buckets),
                 "row_count": len(buckets), "status": status["dark_flow"]},
        "levels": levels,
        "levels_block": {"rows": levels, "row_count": len(levels),
                         "status": status["dark_pool_levels"]},
        "prints": prints,
        "prints_block": {"rows": prints, "row_count": len(prints),
                         "status": status["equity_prints"]},
        "lineage": lineage,
        "kpis": {
            "dark_notional": dark_notional,
            "dark_volume": dark_volume,
            "dark_print_count": None if dark_prints is None else int(dark_prints),
            "dominant_level": None if dominant is None else dominant["price"],
            "dominant_notional": None if dominant is None else dominant["notional"],
            "largest_print": largest,
            "dark_vwap": dark_vwap,
            "dark_share_pct": dark_share,
            "dark_share_basis": dark_share_basis,
            # Sin denominador completo, el KPI no es un cero: es un hueco con causa.
            "dark_share_reason": (None if dark_share is not None else
                                  "sin universo completo de prints dark + lit"),
        },
        "status": status,
        "coverage": f"{covered}/3",
        "coverage_count": covered,
        "session": session or {},
        "latest_stock_price": (levels_block or {}).get("latest_stock_price")
        if isinstance(levels_block, dict) else None,
        "authority": "QUANT_DATA_DIRECT",
        "note": ("Los tres carriles salen del proveedor. La clasificación por venue "
                 "y las zonas de liquidez propias siguen existiendo para auditoría, "
                 "pero no deciden si esta sección tiene datos."),
    }


def _rows_of(block: Any) -> List[Dict[str, Any]]:
    if not isinstance(block, dict):
        return []
    rows = block.get("rows")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
