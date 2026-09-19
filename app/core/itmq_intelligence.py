"""INTELIGENCIA PROPIA DE ITM QUANT · v1.43.0

EL CAMBIO DE PAPEL DEL MOTOR
----------------------------
El motor deja de ser un generador redundante de métricas que Quant Data ya
entrega. GEX, DEX, Vanna, Charm, Interés Abierto, Interval Map, Net Flow, Net
Drift, Order Flow, Dark Flow, Dark Pool Levels e IV/Skew/Term Structure llegan a
la interfaz por su propia ruta —`Quant Data → Data Hub → interfaz`— sin esperar a
que el motor los vuelva a procesar.

El motor recibe **los mismos datos en paralelo** y los usa como ENTRADA para
producir lo único que ningún proveedor puede dar: la lectura de cómo interactúan.

    Alpaca      → qué está haciendo el precio.
    Quant Data  → cómo está posicionada la estructura de opciones.
    ITM QUANT   → qué significa todo eso junto.

QUÉ SE ELIMINA Y QUÉ NO
-----------------------
Se elimina la DUPLICACIÓN: volver a construir una métrica oficial que el
proveedor ya entrega bien. No se elimina la matemática propia que aporta
inteligencia —confluencia, divergencia, persistencia, migración, presión,
régimen, niveles estructurales—, que es precisamente lo que este módulo produce.

SEPARACIÓN ESTRICTA
-------------------
Todo lo que sale de aquí es `DERIVED` y lleva prefijo `ITMQ_`. Ningún resultado de
este módulo puede presentarse internamente como dato directo del proveedor, y el
registro de procedencia lo deja escrito métrica a métrica.

    QD_GEX                  = DIRECT_PROVIDER
    QD_NET_DRIFT            = DIRECT_PROVIDER
    ITMQ_GAMMA_PRESSURE     = DERIVED
    ITMQ_FLOW_CONFLUENCE    = DERIVED
    ITMQ_STRUCTURAL_SCORE   = DERIVED

MULTI-ACTIVO
------------
Ningún ticker aparece en este archivo. Las escalas salen de la distribución del
propio activo (`asset_normalization`), no de constantes, así que la misma lectura
funciona para un ETF enorme y para una acción mediana.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import data_lineage as DL
from .asset_normalization import asset_scale, concentration_threshold
from .data_lineage import LINEAGE, DERIVED, DATA_OK, UNAVAILABLE, NO_PROVIDER_DATA

# Régimen estructural del precio contra la exposición.
BREAK = "BREAK"                # el precio atraviesa la estructura
CONTAINMENT = "CONTAINMENT"    # la estructura contiene el precio
TRANSITION = "TRANSITION"      # la estructura se está reorganizando
UNDEFINED = "UNDEFINED"

# Fuerza mínima para afirmar algo. Por debajo, la respuesta honesta es «no sé».
MIN_STRENGTH = 12.0


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _sign(x: Optional[float], dead: float = 0.0) -> int:
    if x is None:
        return 0
    return 1 if x > dead else -1 if x < -dead else 0


def _strength(value: Optional[float], scale: Optional[float]) -> float:
    """Fuerza 0–100 de una magnitud contra la escala de su propio activo.

    Se satura con una tangente hiperbólica en vez de recortar linealmente: un
    valor tres veces la escala típica y otro treinta veces son los dos
    «extremos», y distinguirlos con dos decimales sería precisión inventada.
    """
    v, s = _f(value), _f(scale)
    if v is None or not s or s <= 0:
        return 0.0
    return round(min(100.0, 100.0 * math.tanh(abs(v) / (2.0 * s))), 2)


# ═══════════════════════════════════════════════════════ GAMMA PRESSURE

def gamma_pressure(symbol: str, exposure_rows: Sequence[Dict[str, Any]],
                   spot: Optional[float]) -> Dict[str, Any]:
    """Hacia dónde empuja la cobertura, leído sobre el GEX del proveedor.

    No recalcula GEX: lo consume. Lo que añade es el reparto por encima y por
    debajo del precio, que es lo que dice si la cobertura frena o acelera, y el
    centro de masa de la gamma, que es dónde tiende a anclarse el precio.

    Gamma NETA positiva sobre el precio y negativa por debajo describe un libro
    que amortigua; la combinación inversa describe uno que amplifica. Publicar
    sólo el total no distingue los dos casos.
    """
    sym = str(symbol or "").upper()
    rows = [r for r in (exposure_rows or []) if isinstance(r, dict)]
    gex = [(_f(r.get("strike")), _f(r.get("gex"))) for r in rows]
    gex = [(k, v) for k, v in gex if k is not None and v is not None]
    px = _f(spot)
    if not gex or px is None:
        return _unavailable(sym, "ITMQ_GAMMA_PRESSURE",
                            "hace falta perfil de GEX por strike y precio del subyacente")

    above = sum(v for k, v in gex if k > px)
    below = sum(v for k, v in gex if k < px)
    net = above + below
    gross = sum(abs(v) for _k, v in gex)
    scale = asset_scale([abs(v) for _k, v in gex], symbol=sym)

    mass = sum(abs(v) for _k, v in gex)
    center = (sum(k * abs(v) for k, v in gex) / mass) if mass > 0 else None

    # Signo: +1 el libro amortigua (gamma neta positiva), −1 amplifica.
    regime_sign = _sign(net)
    balance = (round(100.0 * (above - below) / gross, 2) if gross > 0 else None)
    out = {
        "ready": True, "symbol": sym, "metric": "ITMQ_GAMMA_PRESSURE",
        "source_mode": DERIVED,
        "net_gex": round(net, 4), "gross_gex": round(gross, 4),
        "gex_above": round(above, 4), "gex_below": round(below, 4),
        "balance_pct": balance,
        "gamma_center": (round(center, 4) if center is not None else None),
        "center_distance_pct": (None if center is None or not px
                                else round((center - px) / px * 100.0, 3)),
        "sign": regime_sign,
        "regime": ("AMORTIGUA" if regime_sign > 0 else "AMPLIFICA" if regime_sign < 0 else "NEUTRO"),
        "strength": _strength(net, scale.unit),
        "asset_scale": scale.to_dict(),
        "inputs": ["QD_GEX", "UNDERLYING_PRICE"],
        "derivation": "reparto del GEX del proveedor alrededor del precio observado",
    }
    _record("ITMQ_GAMMA_PRESSURE", sym, out, rows=len(gex))
    return out


# ═══════════════════════════════════════════════════════ GAMMA MIGRATION

def gamma_migration(symbol: str, interval_map: Dict[str, Any], *,
                    window: int = 3) -> Dict[str, Any]:
    """Cómo se está MOVIENDO la exposición entre strikes durante la sesión.

    Ésta es la pregunta que sólo el Interval Map puede responder y que un perfil
    por strike no responde jamás: el perfil dice dónde está la exposición ahora,
    no de dónde viene. Se compara la ventana reciente contra la anterior sobre la
    MISMA matriz del proveedor.

    Se promedian varias columnas por ventana en vez de comparar dos instantes
    sueltos: dos columnas consecutivas pueden diferir por el ruido de un solo
    intervalo, y eso produciría una «migración» distinta en cada refresco.
    """
    sym = str(symbol or "").upper()
    im = interval_map if isinstance(interval_map, dict) else {}
    strikes = [_f(k) for k in (im.get("strikes") or [])]
    matrix = im.get("matrix") or []
    times = im.get("times") or []
    if not im.get("ready") or not strikes or len(times) < 2 or not matrix:
        return _unavailable(sym, "ITMQ_GAMMA_MIGRATION",
                            "hace falta un Interval Map con al menos dos intervalos")

    w = max(1, min(int(window), len(times) // 2))
    arr = np.asarray([[(_f(v, 0.0) or 0.0) for v in (row if isinstance(row, list) else [])]
                      for row in matrix], dtype=float)
    if arr.ndim != 2 or arr.shape[1] < 2 * w:
        w = 1
    if arr.shape[1] < 2:
        return _unavailable(sym, "ITMQ_GAMMA_MIGRATION", "matriz sin profundidad temporal")

    recent = arr[:, -w:].mean(axis=1)
    prior = arr[:, -2 * w:-w].mean(axis=1) if arr.shape[1] >= 2 * w else arr[:, :-w].mean(axis=1)
    delta = recent - prior

    ks = [k for k in strikes if k is not None]
    n = min(len(ks), delta.shape[0])
    pairs = list(zip(ks[:n], delta[:n].tolist()))
    if not pairs:
        return _unavailable(sym, "ITMQ_GAMMA_MIGRATION", "no hay strikes comparables")

    gross = sum(abs(d) for _k, d in pairs)
    net = sum(d for _k, d in pairs)

    def _center(vals: np.ndarray) -> Optional[float]:
        m = np.abs(vals[:n])
        total = float(m.sum())
        return float((np.asarray(ks[:n]) * m).sum() / total) if total > 0 else None

    c_prior, c_recent = _center(prior), _center(recent)
    drift = (None if (c_prior is None or c_recent is None) else round(c_recent - c_prior, 4))
    top = sorted(pairs, key=lambda kv: -abs(kv[1]))[:10]
    scale = asset_scale([abs(d) for _k, d in pairs], symbol=sym)

    out = {
        "ready": True, "symbol": sym, "metric": "ITMQ_GAMMA_MIGRATION",
        "source_mode": DERIVED,
        "net_change": round(net, 6), "gross_change": round(gross, 6),
        "center_prior": (round(c_prior, 4) if c_prior is not None else None),
        "center_recent": (round(c_recent, 4) if c_recent is not None else None),
        "center_drift": drift,
        "direction": ("ARRIBA" if drift and drift > 0 else "ABAJO" if drift and drift < 0 else "ESTABLE"),
        "sign": _sign(drift),
        "strength": _strength(gross, scale.unit),
        "top_strikes": [{"strike": k, "change": round(d, 6)} for k, d in top],
        "window_intervals": w,
        "from": times[-2 * w] if len(times) >= 2 * w else times[0],
        "to": times[-1],
        "inputs": ["QD_INTERVAL_MAP"],
        "derivation": "diferencia de ventanas consecutivas del Interval Map del proveedor",
    }
    _record("ITMQ_GAMMA_MIGRATION", sym, out, rows=len(pairs))
    return out


# ═══════════════════════════════════════════════════════ FLOW CONFLUENCE

_CONFLUENCE_WEIGHTS = (
    ("net_drift", 0.30),
    ("net_flow", 0.22),
    ("dex", 0.20),
    ("qflow", 0.16),
    ("dark_pool", 0.12),
)


def flow_confluence(symbol: str, *, net_drift: Optional[float] = None,
                    net_flow: Optional[float] = None,
                    dex: Optional[float] = None,
                    qflow: Optional[float] = None,
                    dark_pool: Optional[float] = None) -> Dict[str, Any]:
    """¿Apuntan todas las corrientes al mismo sitio, o se contradicen?

    Cinco señales direccionales independientes. Lo que se mide NO es su suma
    —tienen unidades distintas y sumarlas no significa nada— sino su ACUERDO: el
    promedio ponderado de sus SIGNOS. Un acuerdo de cinco sobre cinco con poca
    magnitud vale más que una sola corriente enorme en contra de las otras
    cuatro, y una divergencia es información, no un fallo que haya que promediar
    hasta hacerla desaparecer.

    Una señal ausente pesa cero; no se cuenta como neutra. Contar un hueco como
    «neutro» rebaja artificialmente la confluencia de las que sí llegaron.
    """
    sym = str(symbol or "").upper()
    values = {"net_drift": _f(net_drift), "net_flow": _f(net_flow), "dex": _f(dex),
              "qflow": _f(qflow), "dark_pool": _f(dark_pool)}
    present = {k: v for k, v in values.items() if v is not None}
    if not present:
        return _unavailable(sym, "ITMQ_FLOW_CONFLUENCE",
                            "ninguna corriente de flujo disponible")

    weights = dict(_CONFLUENCE_WEIGHTS)
    total_w = sum(weights[k] for k in present)
    signed = sum(weights[k] * _sign(v) for k, v in present.items())
    agreement = (signed / total_w) if total_w > 0 else 0.0

    signs = [_sign(v) for v in present.values() if _sign(v) != 0]
    unanimous = bool(signs) and len(set(signs)) == 1
    divergent = sorted(k for k, v in present.items()
                       if _sign(v) != 0 and _sign(v) != _sign(agreement))

    strength = round(min(100.0, abs(agreement) * 100.0), 2)
    out = {
        "ready": True, "symbol": sym, "metric": "ITMQ_FLOW_CONFLUENCE",
        "source_mode": DERIVED,
        "sign": _sign(agreement, dead=0.08),
        "agreement": round(agreement, 4),
        "strength": strength,
        "unanimous": unanimous,
        "components": {k: {"value": v, "sign": _sign(v), "weight": weights[k]}
                       for k, v in present.items()},
        "missing": sorted(k for k in values if k not in present),
        "divergent": divergent,
        "reading": ("CONFLUENCIA" if unanimous and strength >= MIN_STRENGTH
                    else "DIVERGENCIA" if divergent else "MIXTO"),
        "inputs": ["QD_NET_DRIFT", "QD_NET_FLOW", "QD_DEX", "ITMQ_QFLOW_CONCENTRATION",
                   "QD_DARK_FLOW"],
        "derivation": "acuerdo ponderado de SIGNOS, nunca suma de magnitudes distintas",
    }
    _record("ITMQ_FLOW_CONFLUENCE", sym, out, rows=len(present))
    return out


# ═══════════════════════════════════════════════════════ PERSISTENCIA

def persistence(symbol: str, series: Sequence[Any], *, metric: str = "ITMQ_PERSISTENCE") -> Dict[str, Any]:
    """¿El movimiento se sostiene o se deshace?

    Se mide como la fracción de la ventana en que el signo se mantuvo igual al
    actual, combinada con la estabilidad de la magnitud. Un movimiento que cambia
    de signo cada dos intervalos es ruido aunque su último valor sea grande, y
    tratarlo como señal es lo que produce entradas contra la corriente real.
    """
    sym = str(symbol or "").upper()
    vals = [_f(v) for v in (series or [])]
    vals = [v for v in vals if v is not None]
    if len(vals) < 4:
        return _unavailable(sym, metric, f"hacen falta 4 observaciones; hay {len(vals)}")

    arr = np.asarray(vals, dtype=float)
    current = _sign(float(arr[-1]))
    if current == 0:
        same = 0.0
    else:
        same = float(np.mean(np.sign(arr) == current))

    # Estabilidad de magnitud: 1 − dispersión relativa, acotada.
    mag = np.abs(arr)
    mean = float(mag.mean())
    stability = 0.0 if mean <= 1e-12 else max(0.0, 1.0 - float(mag.std()) / mean)

    # Racha final con el signo actual.
    streak = 0
    for v in arr[::-1]:
        if _sign(float(v)) != current or current == 0:
            break
        streak += 1

    score = round(min(100.0, 100.0 * (0.65 * same + 0.35 * min(1.0, stability))), 2)
    out = {
        "ready": True, "symbol": sym, "metric": metric, "source_mode": DERIVED,
        "sign": current, "same_sign_fraction": round(same, 4),
        "magnitude_stability": round(min(1.0, stability), 4),
        "streak": streak, "observations": len(vals),
        "strength": score,
        "reading": ("PERSISTENTE" if score >= 60 else "INTERMITENTE" if score >= 35 else "RUIDO"),
        "derivation": "fracción de signo coincidente y estabilidad de magnitud",
    }
    _record(metric, sym, out, rows=len(vals))
    return out


# ═══════════════════════════════════════════════════════ STRIKES DOMINANTES

def dominant_strikes(symbol: str, exposure_rows: Sequence[Dict[str, Any]],
                     oi_rows: Sequence[Dict[str, Any]] | None = None,
                     spot: Optional[float] = None, *, top: int = 8) -> Dict[str, Any]:
    """Qué strikes mandan de verdad, combinando exposición e interés abierto.

    Un strike con mucha gamma pero sin contratos abiertos es una cifra sin libro
    detrás. Uno con mucho OI y poca gamma es peso muerto. Los que mandan son los
    que puntúan alto en las dos cosas a la vez, y por eso el score es el producto
    normalizado, no la suma: la suma deja pasar a los que sólo destacan en una.
    """
    sym = str(symbol or "").upper()
    rows = [r for r in (exposure_rows or []) if isinstance(r, dict)]
    if not rows:
        return _unavailable(sym, "ITMQ_DOMINANT_STRIKE", "sin perfil de exposición por strike")

    oi_by_k: Dict[float, float] = {}
    for r in (oi_rows or []):
        if not isinstance(r, dict):
            continue
        k, v = _f(r.get("strike")), _f(r.get("oi") if r.get("oi") is not None else r.get("value"))
        if k is not None and v is not None:
            oi_by_k[k] = abs(v)

    items: List[Tuple[float, float, float]] = []
    for r in rows:
        k = _f(r.get("strike"))
        g = abs(_f(r.get("gex"), 0.0) or 0.0)
        if k is None:
            continue
        items.append((k, g, oi_by_k.get(k, 0.0)))
    if not items:
        return _unavailable(sym, "ITMQ_DOMINANT_STRIKE", "sin strikes utilizables")

    gmax = max(g for _k, g, _o in items) or 1.0
    omax = max(o for _k, _g, o in items) or 0.0
    px = _f(spot)
    out_rows = []
    for k, g, o in items:
        gn = g / gmax
        on = (o / omax) if omax > 0 else None
        # Sin OI no se penaliza al strike: se puntúa sólo con la exposición y se
        # declara. Multiplicar por cero un dato ausente sería inventar un veredicto.
        score = (gn * on) ** 0.5 if on is not None else gn
        out_rows.append({
            "strike": k, "gex": round(g, 4), "oi": (round(o, 2) if omax > 0 else None),
            "score": round(100.0 * score, 2),
            "oi_available": on is not None,
            "distance_pct": (None if not px else round((k - px) / px * 100.0, 3)),
        })
    out_rows.sort(key=lambda r: -r["score"])
    top_rows = out_rows[:max(1, int(top))]

    out = {
        "ready": True, "symbol": sym, "metric": "ITMQ_DOMINANT_STRIKE",
        "source_mode": DERIVED, "rows": top_rows,
        "dominant": top_rows[0] if top_rows else None,
        "strength": top_rows[0]["score"] if top_rows else 0.0,
        "oi_available": omax > 0,
        "inputs": ["QD_GEX", "QD_OPEN_INTEREST_BY_STRIKE", "UNDERLYING_PRICE"],
        "derivation": "media geométrica de exposición normalizada e interés abierto normalizado",
    }
    _record("ITMQ_DOMINANT_STRIKE", sym, out, rows=len(out_rows))
    return out


# ═══════════════════════════════════════════════════════ BREAK / CONTAINMENT

def structural_state(symbol: str, *, spot: Optional[float],
                     levels: Sequence[Dict[str, Any]],
                     migration: Optional[Dict[str, Any]] = None,
                     price_series: Sequence[Any] = ()) -> Dict[str, Any]:
    """BREAK · CONTAINMENT · TRANSITION contra los niveles estructurales.

    CONTAINMENT: el precio se mueve dentro de la estructura y la estructura no se
    mueve. BREAK: el precio atraviesa un nivel relevante y sigue. TRANSITION: la
    estructura misma se está reorganizando —la gamma migra— y por tanto los
    niveles de hace media hora ya no describen el libro de ahora.

    El orden importa: TRANSITION se evalúa ANTES que BREAK, porque un «BREAK» de
    un nivel que ya no existe es una lectura falsa, y es el error que más caro
    sale cuando la estructura rota rápido.
    """
    sym = str(symbol or "").upper()
    px = _f(spot)
    lv = [(_f(l.get("price")), str(l.get("kind") or l.get("name") or ""))
          for l in (levels or []) if isinstance(l, dict)]
    lv = [(p, k) for p, k in lv if p is not None]
    if px is None or not lv:
        return _unavailable(sym, "ITMQ_BREAK_CONTAINMENT",
                            "hacen falta precio y al menos un nivel estructural")

    nearest = min(lv, key=lambda pk: abs(pk[0] - px))
    distance_pct = abs(nearest[0] - px) / px * 100.0 if px else None

    prices = [_f(v) for v in (price_series or [])]
    prices = [v for v in prices if v is not None]
    crossed = False
    if len(prices) >= 3:
        lo, hi = min(prices[-12:]), max(prices[-12:])
        crossed = lo < nearest[0] < hi

    mig = migration if isinstance(migration, dict) else {}
    mig_strength = _f(mig.get("strength"), 0.0) or 0.0
    reorganising = bool(mig.get("ready")) and mig_strength >= 55.0

    if reorganising:
        state, why = TRANSITION, ("la exposición está migrando entre strikes: los niveles "
                                  "previos ya no describen el libro actual")
        strength = round(mig_strength, 2)
    elif crossed and distance_pct is not None and distance_pct < 0.35:
        state, why = BREAK, f"el precio atraviesa {nearest[1] or 'el nivel'} y se mantiene al otro lado"
        strength = round(min(100.0, 100.0 - distance_pct * 100.0), 2)
    elif distance_pct is not None and distance_pct < 0.8:
        state, why = CONTAINMENT, f"el precio respeta {nearest[1] or 'el nivel'} sin atravesarlo"
        strength = round(min(100.0, (0.8 - distance_pct) / 0.8 * 100.0), 2)
    else:
        state, why = UNDEFINED, "el precio está lejos de cualquier nivel relevante"
        strength = 0.0

    out = {
        "ready": True, "symbol": sym, "metric": "ITMQ_BREAK_CONTAINMENT",
        "source_mode": DERIVED, "state": state, "reason": why,
        "nearest_level": {"price": nearest[0], "kind": nearest[1]},
        "distance_pct": (round(distance_pct, 4) if distance_pct is not None else None),
        "crossed_recently": crossed, "structure_reorganising": reorganising,
        "strength": strength,
        "inputs": ["ITMQ_STRUCTURAL_LEVELS", "ITMQ_GAMMA_MIGRATION", "UNDERLYING_PRICE"],
        "derivation": "posición del precio contra los niveles, corregida por migración de exposición",
    }
    _record("ITMQ_BREAK_CONTAINMENT", sym, out)
    return out


# ═══════════════════════════════════════════════════════ STRUCTURAL SCORE

def structural_score(symbol: str, *, pressure: Optional[Dict[str, Any]] = None,
                     migration: Optional[Dict[str, Any]] = None,
                     confluence: Optional[Dict[str, Any]] = None,
                     dominance: Optional[Dict[str, Any]] = None,
                     persistence_block: Optional[Dict[str, Any]] = None,
                     structure: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Métrica propietaria 0–100: cuánto pesa la estructura ahora mismo.

    Es una agregación de conclusiones propias, no de datos del proveedor: cada
    componente ya es `DERIVED`. Se pondera sobre los componentes PRESENTES y se
    declara cuáles faltan, porque un score calculado con dos de seis piezas no
    significa lo mismo que uno con las seis, y presentarlos igual sería la forma
    más silenciosa de mentir con un número redondo.
    """
    sym = str(symbol or "").upper()
    parts = {
        "gamma_pressure": (pressure, 0.24),
        "gamma_migration": (migration, 0.18),
        "flow_confluence": (confluence, 0.24),
        "dominant_strike": (dominance, 0.14),
        "persistence": (persistence_block, 0.10),
        "structural_state": (structure, 0.10),
    }
    present: Dict[str, Dict[str, Any]] = {}
    for name, (block, weight) in parts.items():
        if isinstance(block, dict) and block.get("ready"):
            present[name] = {"strength": _f(block.get("strength"), 0.0) or 0.0,
                             "sign": int(block.get("sign") or 0), "weight": weight}
    if not present:
        return _unavailable(sym, "ITMQ_STRUCTURAL_SCORE", "ningún componente disponible")

    total_w = sum(p["weight"] for p in present.values())
    score = sum(p["strength"] * p["weight"] for p in present.values()) / total_w
    signed = sum(p["sign"] * p["weight"] for p in present.values()) / total_w
    coverage = round(100.0 * total_w / sum(w for _b, w in parts.values()), 1)

    out = {
        "ready": True, "symbol": sym, "metric": "ITMQ_STRUCTURAL_SCORE",
        "source_mode": DERIVED,
        "score": round(score, 2), "strength": round(score, 2),
        "sign": _sign(signed, dead=0.08), "bias": round(signed, 4),
        "components": present,
        "missing": sorted(k for k in parts if k not in present),
        "coverage_pct": coverage,
        # Un score con menos de la mitad de los componentes no es accionable, y
        # decirlo vale más que publicar el número como si lo fuera.
        "actionable": bool(coverage >= 50.0 and score >= MIN_STRENGTH),
        "derivation": "agregación ponderada de las conclusiones propias presentes",
    }
    _record("ITMQ_STRUCTURAL_SCORE", sym, out, rows=len(present))
    return out


# ═══════════════════════════════════════════════════════ RÉGIMEN

def regime(symbol: str, *, pressure: Optional[Dict[str, Any]] = None,
           confluence: Optional[Dict[str, Any]] = None,
           migration: Optional[Dict[str, Any]] = None,
           iv_rank: Optional[float] = None) -> Dict[str, Any]:
    """Régimen de mercado leído sobre estructura, flujo y volatilidad.

    Cuatro estados, cada uno con una consecuencia operativa distinta:

        SUPRESIÓN     gamma que amortigua + flujo sin dirección → rango
        EXPANSIÓN     gamma que amplifica + flujo direccional   → tendencia
        REORGANIZACIÓN la estructura migra                      → esperar
        INDEFINIDO    no hay evidencia suficiente para afirmar
    """
    sym = str(symbol or "").upper()
    p = pressure if isinstance(pressure, dict) else {}
    c = confluence if isinstance(confluence, dict) else {}
    m = migration if isinstance(migration, dict) else {}
    if not (p.get("ready") or c.get("ready")):
        return _unavailable(sym, "ITMQ_REGIME", "sin presión de gamma ni confluencia de flujo")

    damping = int(p.get("sign") or 0)
    flow_strength = _f(c.get("strength"), 0.0) or 0.0
    mig_strength = _f(m.get("strength"), 0.0) or 0.0

    if m.get("ready") and mig_strength >= 60.0:
        state = "REORGANIZACION"
        strength = round(mig_strength, 2)
        why = "la exposición está migrando: la estructura de referencia se está rehaciendo"
    elif damping > 0 and flow_strength < 45.0:
        state = "SUPRESION"
        strength = round(min(100.0, (_f(p.get("strength"), 0.0) or 0.0) * 0.7 + (45.0 - flow_strength)), 2)
        why = "la cobertura amortigua y el flujo no empuja en una dirección clara"
    elif damping < 0 and flow_strength >= 45.0:
        state = "EXPANSION"
        strength = round(min(100.0, (_f(p.get("strength"), 0.0) or 0.0) * 0.5 + flow_strength * 0.5), 2)
        why = "la cobertura amplifica y el flujo empuja en una dirección"
    else:
        state = "INDEFINIDO"
        strength = round(max(0.0, min(flow_strength, _f(p.get("strength"), 0.0) or 0.0)), 2)
        why = "la evidencia no alcanza para afirmar un régimen"

    out = {
        "ready": True, "symbol": sym, "metric": "ITMQ_REGIME", "source_mode": DERIVED,
        "state": state, "reason": why, "strength": strength,
        "sign": int(c.get("sign") or 0),
        "iv_rank": _f(iv_rank),
        "inputs": ["ITMQ_GAMMA_PRESSURE", "ITMQ_FLOW_CONFLUENCE", "ITMQ_GAMMA_MIGRATION",
                   "QD_IV_RANK"],
        "derivation": "combinación de signo de gamma, fuerza del flujo y migración",
    }
    _record("ITMQ_REGIME", sym, out)
    return out


# ═══════════════════════════════════════════════════════ ENTRADA ÚNICA

def analyze(symbol: str, hub: Dict[str, Any], *, spot: Optional[float] = None,
            levels: Sequence[Dict[str, Any]] = (),
            price_series: Sequence[Any] = (),
            net_drift: Optional[float] = None,
            net_flow: Optional[float] = None,
            qflow_net: Optional[float] = None,
            dark_pool_bias: Optional[float] = None,
            drift_series: Sequence[Any] = ()) -> Dict[str, Any]:
    """Toda la inteligencia propia a partir del snapshot del Data Hub.

    El motor NO vuelve a pedir nada ni recalcula ninguna métrica oficial: lee el
    mismo bloque que ya alimenta a la interfaz y produce lo suyo encima. Es lo que
    hace imposible que la pantalla y el motor describan dos mercados distintos.
    """
    sym = str(symbol or "").upper()
    h = hub if isinstance(hub, dict) else {}
    exposure = (h.get("exposure_by_strike") or {}).get("rows") or []
    oi = (h.get("open_interest") or {}).get("by_strike") or []
    im = h.get("interval_map") or {}
    iv = ((h.get("volatility") or {}).get("iv_rank"))

    dex_bias = None
    dex_vals = [_f(r.get("dex")) for r in exposure if isinstance(r, dict)]
    dex_vals = [v for v in dex_vals if v is not None]
    if dex_vals:
        dex_bias = float(sum(dex_vals))

    pressure = gamma_pressure(sym, exposure, spot)
    migration = gamma_migration(sym, im)
    confluence = flow_confluence(sym, net_drift=net_drift, net_flow=net_flow,
                                 dex=dex_bias, qflow=qflow_net, dark_pool=dark_pool_bias)
    dominance = dominant_strikes(sym, exposure, oi, spot)
    persist = persistence(sym, drift_series or price_series)
    structure = structural_state(sym, spot=spot, levels=levels, migration=migration,
                                 price_series=price_series)
    score = structural_score(sym, pressure=pressure, migration=migration,
                             confluence=confluence, dominance=dominance,
                             persistence_block=persist, structure=structure)
    reg = regime(sym, pressure=pressure, confluence=confluence, migration=migration,
                 iv_rank=_f(iv) if not isinstance(iv, dict) else None)

    return {
        "symbol": sym,
        "source_mode": DERIVED,
        "gamma_pressure": pressure,
        "gamma_migration": migration,
        "flow_confluence": confluence,
        "dominant_strikes": dominance,
        "persistence": persist,
        "structural_state": structure,
        "structural_score": score,
        "regime": reg,
        "separation": {
            "direct_provider": sorted(DL.PRIMARY_PROVIDER_METRICS),
            "derived": sorted(DL.DERIVED_METRICS),
            "rule": ("Ningún resultado DERIVED se presenta como dato directo del "
                     "proveedor. Quant Data entrega los datos oficiales; ITM QUANT "
                     "los interpreta."),
        },
        "contract": "ITMQ_INTELLIGENCE_V1",
    }


# ───────────────────────────────────────────────────────────── internos

def _unavailable(symbol: str, metric: str, detail: str) -> Dict[str, Any]:
    LINEAGE.record(metric, symbol, source_mode=UNAVAILABLE, state=NO_PROVIDER_DATA,
                   provider=DL.ITM_QUANT, detail=detail)
    return {"ready": False, "symbol": symbol, "metric": metric,
            "source_mode": UNAVAILABLE, "state": NO_PROVIDER_DATA,
            "detail": detail, "display": DL.NO_DATA_LABEL,
            "strength": None, "sign": 0}


def _record(metric: str, symbol: str, out: Dict[str, Any], rows: Optional[int] = None) -> None:
    LINEAGE.record(metric, symbol, source_mode=DERIVED, state=DATA_OK,
                   provider=DL.ITM_QUANT,
                   normalized_value=out.get("strength"),
                   final_value={"strength": out.get("strength"), "sign": out.get("sign")},
                   derivation=str(out.get("derivation") or ""), rows=rows)


__all__ = ["gamma_pressure", "gamma_migration", "flow_confluence", "persistence",
           "dominant_strikes", "structural_state", "structural_score", "regime",
           "analyze", "BREAK", "CONTAINMENT", "TRANSITION", "UNDEFINED", "MIN_STRENGTH"]
