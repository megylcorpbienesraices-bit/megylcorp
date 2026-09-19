"""Taxonomía de liquidez fuera de bolsa (v1.42).

TRES CATEGORÍAS, NO UNA
-----------------------
Agrupar todo bajo «Dark Pool» mezcla cosas con niveles de evidencia muy distintos:

    CONFIRMED_OFF_EXCHANGE   el print viene con condición/venue de fuera de bolsa.
                             Es un HECHO reportado, no una interpretación.
    LARGE_PRINT              operación grande en bolsa. Es un hecho, pero «grande» es
                             un umbral que alguien eligió, y debe declararse.
    DERIVED_LIQUIDITY_ZONE   zona de precio donde se acumuló volumen. Es una
                             DERIVACIÓN nuestra. No hay ningún print «de dark pool»
                             ahí; hay una inferencia sobre dónde hubo interés.

Presentar la tercera junto a la primera, con el mismo color y sin distinción, es lo
que convierte un panel informativo en uno que sugiere certezas que no existen.

DE VISUAL A CUANTITATIVO
------------------------
Cada print se acompaña de lo que permite juzgarlo: nocional, porcentaje del volumen
medio diario, percentil dentro de la distribución intradía esperada, e impacto de
precio a 1 s, 5 s, 30 s y 5 min, con su continuación o reversión. Un print de 200.000
acciones que no mueve el precio y revierte en 30 segundos dice algo muy distinto de
uno que lo mueve y continúa.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd
from .frame_guards import numeric_column

CONFIRMED_OFF_EXCHANGE = "CONFIRMED_OFF_EXCHANGE"
LARGE_PRINT = "LARGE_PRINT"
DERIVED_LIQUIDITY_ZONE = "DERIVED_LIQUIDITY_ZONE"

# En los feeds consolidados de EE. UU., el identificador 'D' (FINRA ADF/TRF) es la
# marca de que el print se ejecutó fuera de bolsa. Las condiciones de venta que
# indican operación negociada también cuentan como confirmación.
OFF_EXCHANGE_VENUES = {"D", "FINRA", "TRF", "ADF"}
OFF_EXCHANGE_CONDITIONS = {"4", "B", "SOLD", "SELLER", "NEXT_DAY", "PRIOR_REFERENCE"}

IMPACT_HORIZONS_S = (1.0, 5.0, 30.0, 300.0)


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _epoch_ns(series: pd.Series) -> np.ndarray:
    """Epoch en nanosegundos, sea cual sea la resolución del índice de entrada."""
    s = pd.to_datetime(series, errors="coerce", utc=True).dt.tz_localize(None)
    return s.to_numpy(dtype="datetime64[ns]").astype("int64")


def classify_print(row: Dict[str, Any], *, adv: Optional[float] = None,
                   large_threshold_shares: float = 10_000.0,
                   large_threshold_notional: float = 1_000_000.0) -> Dict[str, Any]:
    """Clasifica un print y declara POR QUÉ quedó en esa categoría."""
    venue = str(row.get("exchange") or row.get("venue") or "").strip().upper()
    conds = str(row.get("conditions") or "").replace(",", " ").upper().split()
    size = _f(row.get("size") or row.get("shares")) or 0.0
    price = _f(row.get("price")) or 0.0
    notional = size * price

    off = venue in OFF_EXCHANGE_VENUES or any(c in OFF_EXCHANGE_CONDITIONS for c in conds)
    if off:
        category = CONFIRMED_OFF_EXCHANGE
        why = (f"venue {venue or '?'} identificado como fuera de bolsa"
               if venue in OFF_EXCHANGE_VENUES else
               f"condición de venta {conds} propia de operación negociada")
        evidence = "REPORTED_FACT"
    elif size >= large_threshold_shares or notional >= large_threshold_notional:
        category = LARGE_PRINT
        why = (f"{size:,.0f} acciones / ${notional:,.0f} supera el umbral declarado "
               f"({large_threshold_shares:,.0f} acciones o ${large_threshold_notional:,.0f})")
        evidence = "REPORTED_FACT_WITH_CHOSEN_THRESHOLD"
    else:
        return {"category": None, "reason": "print ordinario", "evidence": "REPORTED_FACT"}

    pct_adv = None if not adv or adv <= 0 else round(100.0 * size / adv, 4)
    return {
        "category": category, "reason": why, "evidence": evidence,
        "size": size, "price": price, "notional": round(notional, 2),
        "pct_adv": pct_adv, "venue": venue or None, "conditions": conds,
        "timestamp": row.get("timestamp"),
    }


def price_impact(prints: Sequence[Dict[str, Any]], ticks: pd.DataFrame, *,
                 horizons_s: Sequence[float] = IMPACT_HORIZONS_S) -> List[Dict[str, Any]]:
    """Impacto de precio de cada print, medido contra la cinta del subyacente.

    Se compara con el precio inmediatamente ANTERIOR al print, no con el del propio
    print: el precio de ejecución ya incorpora parte del impacto, y usarlo como
    referencia lo haría desaparecer del cálculo.
    """
    if ticks is None or len(ticks) == 0:
        return [{**p, "impact": None, "reason": "sin cinta del subyacente"} for p in prints or []]
    tk = ticks.copy()
    tk["timestamp"] = pd.to_datetime(tk["timestamp"], errors="coerce", utc=True)
    tk = tk.dropna(subset=["timestamp"]).sort_values("timestamp")
    px = pd.to_numeric(tk.get("price", tk.get("last")), errors="coerce")
    tk = tk.assign(_px=px).dropna(subset=["_px"])
    if tk.empty:
        return [{**p, "impact": None, "reason": "cinta sin precios"} for p in prints or []]

    # Se trabaja en epoch de nanosegundos explícitos. Ni `np.datetime64` sobre un
    # Timestamp con zona (numpy lo acepta a medias y pandas lo rechaza) ni un
    # `.astype("int64")` directo, cuya unidad depende de la resolución del índice:
    # con datetime64[us] devuelve microsegundos y la comparación se va por mil.
    times = _epoch_ns(tk["timestamp"])
    prices = tk["_px"].to_numpy(float)
    out: List[Dict[str, Any]] = []
    for p in prints or []:
        t0 = pd.to_datetime(p.get("timestamp"), errors="coerce", utc=True)
        if pd.isna(t0):
            out.append({**p, "impact": None, "reason": "print sin marca de tiempo"})
            continue
        t0_ns = int(pd.Timestamp(t0).tz_convert("UTC").tz_localize(None).as_unit("ns").value)
        i0 = int(np.searchsorted(times, t0_ns, side="left"))
        if i0 <= 0 or i0 >= len(prices):
            out.append({**p, "impact": None, "reason": "sin precio previo en la cinta"})
            continue
        ref = float(prices[i0 - 1])
        impacts: Dict[str, Any] = {}
        for h in horizons_s:
            j = int(np.searchsorted(times, t0_ns + int(float(h) * 1e9), side="right")) - 1
            if j < i0:
                impacts[f"{h:g}s"] = None
                continue
            impacts[f"{h:g}s"] = round((float(prices[j]) - ref) / max(abs(ref), 1e-9) * 1e4, 2)
        short = impacts.get(f"{horizons_s[0]:g}s")
        long_ = impacts.get(f"{horizons_s[-1]:g}s")
        behaviour = "SIN_DATOS"
        if short is not None and long_ is not None and abs(short) > 1e-9:
            behaviour = "CONTINUACIÓN" if (long_ / short) > 1.0 else (
                "REVERSIÓN" if (long_ / short) < 0.0 else "DESVANECIMIENTO")
        out.append({**p, "reference_price": round(ref, 6), "impact_bps": impacts,
                    "behaviour": behaviour,
                    "note": ("Impacto medido contra el precio previo al print, en puntos "
                             "básicos. Referenciar contra el propio print escondería el "
                             "impacto dentro del precio de ejecución.")})
    return out


def derive_liquidity_zones(ticks: pd.DataFrame, *, bins: int = 40,
                           top: int = 5) -> List[Dict[str, Any]]:
    """Zonas de acumulación de volumen. DERIVACIÓN nuestra, etiquetada como tal."""
    if ticks is None or len(ticks) == 0:
        return []
    px = pd.to_numeric(ticks.get("price", ticks.get("last")), errors="coerce")
    sz = numeric_column(ticks,"size",1.0)
    ok = px.notna() & (px > 0)
    if ok.sum() < 20:
        return []
    px, sz = px[ok], sz[ok]
    hist, edges = np.histogram(px, bins=max(int(bins), 5), weights=sz)
    total = float(hist.sum()) or 1.0
    order = np.argsort(hist)[::-1][: max(int(top), 1)]
    return [{
        "category": DERIVED_LIQUIDITY_ZONE, "evidence": "DERIVED_BY_ITM",
        "price_low": round(float(edges[i]), 4), "price_high": round(float(edges[i + 1]), 4),
        "volume_share_pct": round(100.0 * float(hist[i]) / total, 3),
        "note": ("Zona derivada de la concentración de volumen. NO es un print de dark "
                 "pool: es una inferencia sobre dónde hubo interés."),
    } for i in order if hist[i] > 0]


def build_panel(prints: Iterable[Dict[str, Any]], ticks: pd.DataFrame | None = None, *,
                adv: Optional[float] = None) -> Dict[str, Any]:
    """Panel completo, separado por categoría y con el nivel de evidencia visible."""
    classified = []
    for row in prints or []:
        c = classify_print(row, adv=adv)
        if c.get("category"):
            classified.append(c)
    with_impact = price_impact(classified, ticks) if ticks is not None else classified
    zones = derive_liquidity_zones(ticks) if ticks is not None else []

    buckets: Dict[str, List[Dict[str, Any]]] = {
        CONFIRMED_OFF_EXCHANGE: [], LARGE_PRINT: [], DERIVED_LIQUIDITY_ZONE: list(zones)}
    for row in with_impact:
        buckets.setdefault(row["category"], []).append(row)

    return {
        "ready": True,
        "categories": {
            CONFIRMED_OFF_EXCHANGE: {
                "evidence": "HECHO REPORTADO", "count": len(buckets[CONFIRMED_OFF_EXCHANGE]),
                "items": buckets[CONFIRMED_OFF_EXCHANGE][:100],
                "definition": "Print con venue o condición de fuera de bolsa."},
            LARGE_PRINT: {
                "evidence": "HECHO REPORTADO CON UMBRAL ELEGIDO", "count": len(buckets[LARGE_PRINT]),
                "items": buckets[LARGE_PRINT][:100],
                "definition": "Operación en bolsa por encima de un umbral declarado."},
            DERIVED_LIQUIDITY_ZONE: {
                "evidence": "DERIVACIÓN DE ITM", "count": len(buckets[DERIVED_LIQUIDITY_ZONE]),
                "items": buckets[DERIVED_LIQUIDITY_ZONE],
                "definition": "Zona de acumulación de volumen. No hay prints de dark pool aquí."},
        },
        "impact_horizons_s": list(IMPACT_HORIZONS_S),
        "doctrine": ("Las tres categorías no se mezclan. Un hecho reportado y una derivación "
                     "nuestra no pueden compartir etiqueta ni color."),
    }
