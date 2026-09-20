"""VALIDACIÓN CELDA POR CELDA del Interval Map. Puntos 18, 52, 53 y 54.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ NO BASTA COMPARAR CAPTURAS
═══════════════════════════════════════════════════════════════════════════

«El mapa de QQQ sale casi todo rojo» puede tener dos causas opuestas:

    el proveedor devuelve exposición negativa   →  el rojo es CORRECTO
    la agregación o el renderizador invierten   →  es un BUG

Mirar la pantalla al lado de la web del proveedor no las distingue: las dos
producen una imagen roja. Lo que las distingue son los NÚMEROS.

Este módulo los compara, celda a celda, a lo largo de la cadena:

    RAW del proveedor  →  valor canónico  →  signo  →  intensidad publicada

y exige que el SIGNO se conserve. No arregla ninguna paleta: primero demuestra
qué manda Quant Data.

═══════════════════════════════════════════════════════════════════════════
LO QUE SÍ PUEDE CAMBIAR Y LO QUE NO
═══════════════════════════════════════════════════════════════════════════

La normalización por rango cambia la MAGNITUD a propósito: sustituye cada celda
por el percentil que ocupa. Eso está documentado y es lo que hace el mapa
legible en cualquier activo.

Lo que NO puede cambiar nunca:

    · el SIGNO                positivo no puede salir negativo
    · el ORDEN                si A tiene más exposición que B, se ve más intensa
    · un HUECO                una celda que el proveedor no publicó no puede
                              aparecer como una celda medida

Una celda por debajo del suelo de ruido se atenúa hasta cero. Eso NO es una
inversión: es el suelo, y se cuenta aparte.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

#: Tolerancia relativa al comparar dos magnitudes que deberían ser la misma.
TOLERANCE = 1e-9


def _f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x if math.isfinite(x) else None
    if isinstance(v, str):
        texto = v.strip()
        if not texto:
            return None
        try:
            x = float(texto)
        except ValueError:
            return None
        return x if math.isfinite(x) else None
    return None


def _sign(v: Optional[float]) -> Optional[int]:
    if v is None:
        return None
    return 1 if v > 0 else -1 if v < 0 else 0


def sample_cells(strikes: Sequence[Any], times: Sequence[Any],
                 raw: Sequence[Sequence[Any]],
                 canonical: Sequence[Sequence[Any]],
                 *, limit: int = 50) -> List[Dict[str, Any]]:
    """Hasta `limit` celdas repartidas por la rejilla, con sus dos valores.

    Se reparten en vez de tomarse las primeras: las primeras filas de una
    matriz de exposición suelen ser los strikes más lejanos, donde casi todo es
    cero, y una muestra de ceros no demuestra nada sobre el signo.
    """
    h = len(raw or [])
    w = len(raw[0]) if h and isinstance(raw[0], (list, tuple)) else 0
    if not h or not w:
        return []
    total = h * w
    paso = max(1, total // max(1, limit))
    out: List[Dict[str, Any]] = []
    for idx in range(0, total, paso):
        y, x = divmod(idx, w)
        if y >= len(canonical or []) or x >= len(canonical[y] or []):
            continue
        crudo = _f(raw[y][x])
        canon = _f(canonical[y][x])
        out.append({
            "strike": _f(strikes[y]) if y < len(strikes or []) else None,
            "time": str(times[x]) if x < len(times or []) else None,
            "row": y, "col": x,
            "raw": crudo,
            "raw_missing": raw[y][x] is None,
            "canonical": canon,
            "canonical_missing": canonical[y][x] is None,
            "raw_sign": _sign(crudo),
            "canonical_sign": _sign(canon),
            "expected_color": ("verde" if (crudo or 0) > 0 else
                               "rojo" if (crudo or 0) < 0 else "neutro"),
        })
        if len(out) >= limit:
            break
    return out


def audit_grid(payload: Dict[str, Any], *, limit: int = 50) -> Dict[str, Any]:
    """¿La rejilla publicada representa el MISMO dato que llegó?

    `payload` es la salida de `quant_data_hub.interval_map`, que lleva la matriz
    cruda (`matrix`) y la normalizada (`intensity`) sobre los MISMOS ejes.
    """
    p = payload if isinstance(payload, dict) else {}
    strikes = p.get("strikes") or []
    times = p.get("times") or []
    raw = p.get("matrix") or []
    canon = p.get("intensity") or []

    if not raw or not canon:
        return {"ok": False, "checked": 0, "reason": "la rejilla no publica las dos matrices",
                "symbol": p.get("symbol"), "greek": p.get("greek")}

    if len(raw) != len(canon) or any(len(a or []) != len(b or []) for a, b in zip(raw, canon)):
        return {"ok": False, "checked": 0,
                "reason": "la matriz cruda y la normalizada no tienen la misma forma",
                "symbol": p.get("symbol"), "greek": p.get("greek")}

    celdas = sample_cells(strikes, times, raw, canon, limit=limit)
    invertidas: List[Dict[str, Any]] = []
    huecos_rellenados: List[Dict[str, Any]] = []
    atenuadas = 0
    for c in celdas:
        rs, cs = c["raw_sign"], c["canonical_sign"]
        # Un HUECO tiene que seguir siendo un hueco.
        if c["raw_missing"] != c["canonical_missing"]:
            huecos_rellenados.append(c)
            continue
        if rs is None or cs is None:
            continue
        # Atenuar hasta cero por el suelo de ruido NO es invertir.
        if cs == 0 and rs != 0:
            atenuadas += 1
            continue
        if rs != 0 and cs != 0 and rs != cs:
            invertidas.append(c)

    ok = not invertidas and not huecos_rellenados
    return {
        "ok": ok,
        "symbol": p.get("symbol"), "greek": p.get("greek"),
        "source": p.get("source"), "source_detail": p.get("source_detail"),
        "normalization": p.get("normalization"),
        "checked": len(celdas),
        "cells": celdas,
        "sign_flips": invertidas,
        "sign_flip_count": len(invertidas),
        "missing_became_measured": huecos_rellenados,
        "noise_floor_muted": atenuadas,
        "grid": {"strikes": len(strikes), "times": len(times),
                 "cells": len(strikes) * len(times)},
        "detail": ("el signo se conserva en todas las celdas muestreadas" if ok else
                   f"{len(invertidas)} celda(s) cambian de signo y "
                   f"{len(huecos_rellenados)} hueco(s) se publican como medidos"),
        "rule": ("la normalización puede cambiar la MAGNITUD —es un percentil— "
                 "pero nunca el signo, nunca el orden y nunca un hueco"),
    }


def compare_rows(rows: Sequence[Dict[str, Any]], payload: Dict[str, Any],
                 *, limit: int = 50) -> Dict[str, Any]:
    """Compara filas RAW del proveedor contra la rejilla canónica publicada.

    `rows` son filas con `strike`, `time`/`t`, `call_exposure` y `put_exposure`
    tal y como las entrega el proveedor. Es la comprobación que se ejecuta con
    la API real: demuestra que la celda dibujada en (strike, intervalo) lleva
    el valor que el proveedor publicó para ese mismo par.
    """
    p = payload if isinstance(payload, dict) else {}
    strikes = [(_f(k), i) for i, k in enumerate(p.get("strikes") or [])]
    times = {str(t): i for i, t in enumerate(p.get("times") or [])}
    raw = p.get("matrix") or []
    por_strike = {k: i for k, i in strikes if k is not None}

    filas: List[Dict[str, Any]] = []
    desajustes: List[Dict[str, Any]] = []
    for r in list(rows or [])[:limit]:
        if not isinstance(r, dict):
            continue
        k = _f(r.get("strike"))
        t = str(r.get("time") or r.get("t") or "")
        y = por_strike.get(k)
        x = times.get(t)
        call = _f(r.get("call_exposure") if r.get("call_exposure") is not None else r.get("call"))
        put = _f(r.get("put_exposure") if r.get("put_exposure") is not None else r.get("put"))
        esperado = None if (call is None and put is None) else (call or 0.0) + (put or 0.0)
        publicado = (_f(raw[y][x]) if (y is not None and x is not None
                                       and y < len(raw) and x < len(raw[y] or [])) else None)
        coincide = (esperado is None and publicado is None) or (
            esperado is not None and publicado is not None
            and abs(esperado - publicado) <= max(abs(esperado), 1.0) * TOLERANCE)
        fila = {"strike": k, "time": t, "call_exposure": call, "put_exposure": put,
                "expected": esperado, "published": publicado,
                "found_in_grid": y is not None and x is not None, "match": coincide}
        filas.append(fila)
        if not coincide:
            desajustes.append(fila)

    return {
        "ok": not desajustes and bool(filas),
        "checked": len(filas), "rows": filas,
        "mismatches": desajustes, "mismatch_count": len(desajustes),
        "reduction": ("call_exposure + put_exposure por (strike, intervalo); "
                      "la MISMA reducción para todos los activos"),
        "detail": ("cada celda publicada coincide con el crudo del proveedor"
                   if not desajustes and filas else
                   "sin filas que comparar" if not filas else
                   f"{len(desajustes)} celda(s) no coinciden con el crudo"),
    }
