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

═══════════════════════════════════════════════════════════════════════════
ALCANCE TEMPORAL · v1.55.0
═══════════════════════════════════════════════════════════════════════════

Los tres carriles NO cubren la misma ventana. `dark-flow` llega por intervalos
de toda la sesión; `equity-prints` llega como una cola reciente. Presentar

    NOTIONAL OFF-EXCHANGE   412 M$        ← toda la sesión
    PRINT MAYOR             1,2 M$        ← últimos minutos

uno al lado del otro sin decirlo invita a dividir el segundo entre el primero,
y esa división no significa nada. Cada carril publica ahora su ventana medida
—primer y último instante observados— y cada KPI dice de qué carril sale y qué
período resume.

═══════════════════════════════════════════════════════════════════════════
IDENTIDAD DEL CICLO · v1.55.0
═══════════════════════════════════════════════════════════════════════════

`cycle_id` es la huella del CONJUNTO: símbolo, sesión, número de filas por
carril y último instante de cada uno. Si el ciclo trae lo mismo, el id es el
mismo; en cuanto cambia cualquier pieza, cambia el id.

Sirve para lo que no se puede ver mirando la pantalla: distinguir «esto es lo
de hace diez minutos» de «esto acaba de llegar y resulta que es idéntico». Sin
él, una sección congelada por un fallo de refresco es indistinguible de un
mercado sin actividad nueva.

═══════════════════════════════════════════════════════════════════════════
MUROS DE DARK POOL · v1.55.0
═══════════════════════════════════════════════════════════════════════════

Los niveles ya venían como tabla. Un nivel de concentración oscura es un PRECIO
con un tamaño detrás, igual que un muro de gamma, y se lee contra el precio: se
publican por tanto como LÍNEAS dibujables, con `kind`, `name` y `authority`
propios, encima y debajo del spot. Lo que NO se hace es mezclarlos con los muros
de opciones: miden cosas distintas y comparten eje de precio, nada más.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
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


#: Cuántas líneas de concentración se publican a cada lado del precio. Más de
#: tres por lado deja de ser «dónde está el peso» y pasa a ser la tabla otra vez.
DARK_WALLS_PER_SIDE = 3

#: Prefijo de `kind` de las líneas de dark pool. Distinto de los muros de
#: opciones a propósito: comparten eje de precio y nada más.
DARK_WALL_KIND = "dark_pool_wall"


def _window(rows: List[Dict[str, Any]], field: str = "t") -> Dict[str, Any]:
    """Primer y último instante REALMENTE observados en un carril.

    No es la ventana que se pidió: es la que llegó. Son cosas distintas cuando
    el proveedor recorta, y la que importa para leer un KPI es la segunda.
    """
    marcas = [str(r.get(field)) for r in rows or [] if r.get(field)]
    marcas = sorted(m for m in marcas if m)
    if not marcas:
        return {"first": None, "last": None, "points": 0}
    return {"first": marcas[0], "last": marcas[-1], "points": len(marcas)}


def _cycle_id(symbol: str, session: Dict[str, Any],
              lanes: Dict[str, Dict[str, Any]]) -> str:
    """Huella del CONJUNTO del ciclo. Ver la cabecera del módulo."""
    piezas = [str(symbol or "").upper(), str((session or {}).get("resolved") or "")]
    for name in sorted(lanes):
        lane = lanes[name] or {}
        piezas.append(f"{name}:{lane.get('rows')}:{lane.get('last')}")
    return hashlib.sha256("|".join(piezas).encode("utf-8")).hexdigest()[:12]


def _dark_walls(levels: List[Dict[str, Any]], spot: Optional[float]) -> List[Dict[str, Any]]:
    """Las concentraciones oscuras como LÍNEAS, encima y debajo del precio.

    Un nivel de dark pool es un precio con tamaño detrás y se lee contra el
    precio, igual que un muro. Sin spot no hay «encima» ni «debajo», así que se
    publican los mayores sin lado en vez de inventarse una referencia.
    """
    con_tamano = [lv for lv in levels if lv.get("notional") is not None]
    if not con_tamano:
        return []
    if spot is None:
        elegidos = [(lv, None) for lv in con_tamano[:DARK_WALLS_PER_SIDE * 2]]
    else:
        arriba = [lv for lv in con_tamano if lv["price"] > spot][:DARK_WALLS_PER_SIDE]
        abajo = [lv for lv in con_tamano if lv["price"] < spot][:DARK_WALLS_PER_SIDE]
        elegidos = [(lv, "ABOVE_SPOT") for lv in arriba] + [(lv, "BELOW_SPOT") for lv in abajo]
    mayor = max((lv["notional"] for lv, _ in elegidos), default=None)
    out = []
    for rank, (lv, side) in enumerate(sorted(elegidos, key=lambda x: -(x[0]["notional"] or 0.0)), 1):
        out.append({
            "kind": DARK_WALL_KIND,
            "name": f"DP {lv['price']:g}",
            "price": lv["price"],
            "notional": lv["notional"],
            "shares": lv.get("shares"),
            "prints": lv.get("prints"),
            "side": side,
            "rank": rank,
            # Fuerza RELATIVA al mayor del propio activo: sin umbrales en dólares,
            # igual que el resto del programa.
            "strength": (None if not mayor else round(100.0 * (lv["notional"] or 0.0) / mayor, 1)),
            "distance_pct": lv.get("distance_pct"),
            "authority": "QUANTDATA_DARK_POOL_LEVELS",
            "source_mode": "DIRECT_PROVIDER",
        })
    return out


def build(*, flow_block: Any, levels_block: Any, prints_block: Any,
          spot: Optional[float] = None,
          symbol: str = "",
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

    # ── ALCANCE TEMPORAL POR CARRIL ──────────────────────────────────────────
    # Los tres NO cubren la misma ventana, y presentarlos juntos sin decirlo
    # invita a dividir un KPI de los ultimos minutos entre uno de toda la sesion.
    scope = {
        "dark_flow": _window(buckets),
        "dark_pool_levels": {"first": None, "last": None, "points": len(levels),
                             "note": "sin eje temporal: es una foto acumulada de la sesion"},
        "equity_prints": _window(prints),
    }

    walls = _dark_walls(levels, spot)

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
    cycle = _cycle_id(symbol, session or {}, {
        "dark_flow": {"rows": len(buckets), "last": scope["dark_flow"]["last"]},
        "dark_pool_levels": {"rows": len(levels),
                             "last": None if not levels else f"{levels[0]['price']}:{levels[0]['notional']}"},
        "equity_prints": {"rows": len(prints), "last": scope["equity_prints"]["last"]},
    })

    return {
        "ready": covered > 0,
        "symbol": str(symbol or "").upper() or None,
        # Huella del CONJUNTO: distingue «esto es lo de hace diez minutos» de
        # «esto acaba de llegar y resulta que es identico».
        "cycle_id": cycle,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "temporal_scope": scope,
        # Las concentraciones como LINEAS dibujables, con identidad propia.
        "walls": walls,
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
        # ── QUE MIDE CADA KPI Y SOBRE QUE PERIODO ────────────────────────────
        #
        # v1.55.0 · «NOTIONAL OFF-EXCHANGE» no dice de donde sale ni que ventana
        # resume, y al lado de «PRINT MAYOR» —que viene de otro carril y de otra
        # ventana— invita a dividir uno entre otro. El nombre de pantalla se
        # queda corto a proposito; la definicion viaja aqui y el Auditor la
        # muestra entera.
        "kpi_meta": {
            "dark_notional": {
                "label": "NOTIONAL FUERA DE BOLSA",
                "measures": "suma del valor negociado fuera de bolsa, en dolares",
                "formula": "Sigma notionalValue de Dark Flow",
                "lane": "dark_flow", "window": scope["dark_flow"],
                "unit": "USD",
            },
            "dark_volume": {
                "label": "VOLUMEN OSCURO",
                "measures": "acciones negociadas fuera de bolsa",
                "formula": "Sigma size de Dark Flow",
                "lane": "dark_flow", "window": scope["dark_flow"],
                "unit": "acciones",
            },
            "dark_print_count": {
                "label": "OPERACIONES OSCURAS",
                "measures": "numero de operaciones fuera de bolsa",
                "formula": "Sigma tradeCount de Dark Flow",
                "lane": "dark_flow", "window": scope["dark_flow"],
                "unit": "operaciones",
            },
            "dominant_level": {
                "label": "PRECIO DE MAYOR CONCENTRACION",
                "measures": "el precio donde mas dinero se cruzo fuera de bolsa",
                "formula": "argmax notionalValue de Dark Pool Levels",
                "lane": "dark_pool_levels", "window": scope["dark_pool_levels"],
                "unit": "precio",
            },
            "largest_print": {
                "label": "OPERACION MAYOR",
                "measures": "la mayor operacion oscura individual observada",
                "formula": "max notionalValue de Equity Prints DARK_POOL",
                "lane": "equity_prints", "window": scope["equity_prints"],
                "unit": "USD",
                # La advertencia que evita la division sin sentido.
                "caveat": ("viene de la cola reciente de prints, no de toda la sesion: "
                           "no se puede dividir entre el notional fuera de bolsa"),
            },
            "dark_vwap": {
                "label": "PRECIO MEDIO OSCURO",
                "measures": "precio medio ponderado por acciones de las operaciones oscuras",
                "formula": "Sigma(precio x size) / Sigma(size) sobre Equity Prints DARK_POOL",
                "lane": "equity_prints", "window": scope["equity_prints"],
                "unit": "precio",
            },
            "dark_share_pct": {
                "label": "CUOTA FUERA DE BOLSA",
                "measures": "que parte del volumen observado se cruzo fuera de bolsa",
                "formula": "acciones dark / (acciones dark + acciones lit), solo con universo completo",
                "lane": "equity_prints", "window": scope["equity_prints"],
                "unit": "%",
                "caveat": ("no se estima: sin universo dark + lit completo va en hueco, "
                           "porque una cuota sobre denominador parcial se lee como cuota de mercado"),
            },
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
