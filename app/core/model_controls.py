"""ALIMENTAR LOS CONTROLES DEL AUDITOR DESDE LA CADENA REAL.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ EXISTE
═══════════════════════════════════════════════════════════════════════════

El auditor de riesgo de modelo avisaba con dos frases:

    IV          «no se evaluó la identificabilidad de la IV»
    SUPERFICIE  «no hay ajuste de superficie»

Las dos parecían decir «esto no está implementado». Ninguna lo decía.

    · `iv_quality.assess()` y `chain_quality()` existen, completos.
    · `ssvi_shadow.fit_ssvi()` existe, con sus condiciones de mariposa de
      Durrleman y su monotonía de varianza total en calendario.

Lo que faltaba era el cable: `audit()` se llamaba sin `iv_assessments` y sin
`surface`. Un control al que nadie entrega nada no está midiendo, está
esperando, y su aviso manda a buscar una funcionalidad que ya estaba escrita.

Este módulo es ese cable, y se ejecuta DONDE VIVE LA CADENA. Calcularlo en el
auditor obligaría a arrastrar el snapshot entero hasta la capa de presentación.

═══════════════════════════════════════════════════════════════════════════
LO QUE NO HACE
═══════════════════════════════════════════════════════════════════════════

No rebaja ninguna severidad ni tapa ningún aviso. Si la cadena tiene mala IV,
el control lo dirá con más fuerza que antes, porque antes ni miraba.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

#: Mínimos para que un corte de vencimiento entre en el ajuste. Son los de
#: `fit_ssvi`: cuatro strikes por corte y dos cortes. Ajustar una superficie con
#: tres puntos no es ajustar, es unir puntos.
MIN_STRIKES_PER_SLICE = 4
MIN_SLICES = 2

#: Cuántos contratos se evalúan como máximo. La identificabilidad es una
#: propiedad de la cadena, no hace falta recorrer diez mil filas para medirla, y
#: esto vive en el camino de refresco.
MAX_ASSESSMENTS = 600


def _f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _rows(chain: Any) -> List[Dict[str, Any]]:
    """Filas de la cadena, venga como DataFrame o como lista de diccionarios."""
    if chain is None:
        return []
    if hasattr(chain, "to_dict") and hasattr(chain, "empty"):
        if getattr(chain, "empty", True):
            return []
        try:
            return list(chain.to_dict("records"))
        except Exception:
            return []
    if isinstance(chain, (list, tuple)):
        return [r for r in chain if isinstance(r, dict)]
    return []


def _years(row: Dict[str, Any]) -> Optional[float]:
    t = _f(row.get("T")) or _f(row.get("t_years"))
    if t is not None and t > 0:
        return t
    dte = _f(row.get("dte"))
    if dte is None or dte <= 0:
        return None
    return dte / 365.0


#: Último recuento de contratos que no se pudieron evaluar, y por qué. No es
#: estado compartido con significado: es una nota para el diagnóstico.
_NOTA: Dict[str, Any] = {}


def _evaluar_una(assess, *, symbol, S, K, T, tipo, r, q, row):
    """Evalúa un contrato. Devuelve la evaluación, o el motivo del descarte.

    Existe como función aparte para que el `except` tenga un cuerpo con
    contenido: el guardia del proyecto prohíbe los handlers silenciosos, y con
    razón —un `except: continue` es exactamente donde se pierde la causa—.
    """
    try:
        return assess(
            symbol=symbol, S=S, K=K, T=T, option_type=tipo,
            r=float(r), q=float(q),
            mid=_f(row.get("mid")), bid=_f(row.get("bid")), ask=_f(row.get("ask")),
            provider_iv=_f(row.get("iv")) or _f(row.get("provider_iv")),
        )
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def iv_assessments(chain: Any, *, symbol: str, spot: Optional[float] = None,
                   r: float = 0.0, q: float = 0.0,
                   limit: int = MAX_ASSESSMENTS) -> List[Any]:
    """Evalúa contrato a contrato si su IV significa algo.

    Devuelve lista vacía cuando no hay cadena utilizable. Esa lista vacía es
    la que el auditor traduce a N/A —«no hay cadena en este ciclo»— y no a un
    aviso de que la identificabilidad no se evaluó.
    """
    from .iv_quality import assess

    filas = _rows(chain)
    if not filas:
        return []

    S0 = _f(spot)
    if S0 is None:
        for r0 in filas:
            S0 = _f(r0.get("underlying_price")) or _f(r0.get("spot"))
            if S0:
                break
    if not S0 or S0 <= 0:
        return []

    fuera: List[Any] = []
    descartados: List[str] = []
    for row in filas[: max(1, int(limit))]:
        K = _f(row.get("strike"))
        T = _years(row)
        tipo = str(row.get("option_type") or row.get("type") or "").strip()
        if K is None or T is None or not tipo:
            descartados.append("fila sin strike, vencimiento o tipo")
            continue
        v = _evaluar_una(assess, symbol=symbol, S=S0, K=K, T=T, tipo=tipo,
                         r=r, q=q, row=row)
        if isinstance(v, str):
            # Un contrato que no se puede evaluar no puede tumbar la evaluación
            # de los otros seiscientos, pero tampoco desaparece en silencio: se
            # cuenta, y el porcentaje de identificabilidad lo refleja.
            descartados.append(v)
            continue
        fuera.append(v.describe())
    if descartados:
        _NOTA["descartados"] = len(descartados)
        _NOTA["primer_motivo"] = descartados[0][:160]
    return fuera


def surface_slices(chain: Any, *, spot: Optional[float] = None) -> List[Dict[str, Any]]:
    """Cortes {k, w, T, vega} por vencimiento, listos para `fit_ssvi`.

    `k` es log-moneyness y `w` la varianza total observada, w = IV² · T, que es
    la magnitud en la que SSVI está definido. Ajustar sobre IV directamente
    mezclaría vencimientos con escalas distintas.
    """
    filas = _rows(chain)
    if not filas:
        return []

    S0 = _f(spot)
    if S0 is None:
        for r0 in filas:
            S0 = _f(r0.get("underlying_price")) or _f(r0.get("spot"))
            if S0:
                break
    if not S0 or S0 <= 0:
        return []

    por_T: Dict[float, Dict[str, List[float]]] = {}
    for row in filas:
        K = _f(row.get("strike"))
        T = _years(row)
        iv = _f(row.get("iv")) or _f(row.get("provider_iv"))
        if K is None or T is None or iv is None or K <= 0 or iv <= 0:
            continue
        w = iv * iv * T
        if not (w > 0):
            continue
        d = por_T.setdefault(round(T, 6), {"k": [], "w": [], "vega": []})
        d["k"].append(math.log(K / S0))
        d["w"].append(w)
        # El peso por vega evita que el ajuste lo arrastren alas ilíquidas donde
        # un centavo de prima mueve la IV varios puntos.
        v = _f(row.get("vega"))
        d["vega"].append(v if (v is not None and v > 0) else 1.0)

    cortes = []
    for T, d in sorted(por_T.items()):
        if len(d["k"]) < MIN_STRIKES_PER_SLICE:
            continue
        cortes.append({"k": d["k"], "w": d["w"], "T": float(T), "vega": d["vega"]})
    return cortes


def surface_report(chain: Any, *, spot: Optional[float] = None) -> Dict[str, Any]:
    """Ajuste SSVI de la cadena, en la forma que `audit_surface` consume.

    `ready=False` con su motivo cuando la cadena no da para ajustar. El auditor
    lo traduce a N/A con ese motivo, no a «no hay ajuste de superficie», que
    sugería que el ajustador no existía.
    """
    from .ssvi_shadow import fit_ssvi

    cortes = surface_slices(chain, spot=spot)
    if len(cortes) < MIN_SLICES:
        return {"ready": False, "model": "SSVI", "slices": [],
                "reason": (f"la cadena de este ciclo da {len(cortes)} corte(s) con "
                           f"{MIN_STRIKES_PER_SLICE}+ strikes; SSVI necesita "
                           f"{MIN_SLICES}"),
                "slices_available": len(cortes)}
    try:
        fit = fit_ssvi(cortes)
    except Exception as exc:
        return {"ready": False, "model": "SSVI", "slices": [],
                "reason": f"el ajuste falló: {type(exc).__name__}: {exc}"[:180]}
    fit.setdefault("slices_available", len(cortes))
    return fit


def build(chain: Any, *, symbol: str, spot: Optional[float] = None,
          r: float = 0.0, q: float = 0.0) -> Dict[str, Any]:
    """Todo lo que los controles necesitan, en una pasada sobre la cadena."""
    ass = iv_assessments(chain, symbol=symbol, spot=spot, r=r, q=q)
    sur = surface_report(chain, spot=spot)
    return {
        # Como diccionarios: esto viaja por el estado público, que se serializa
        # a JSON, y un `IVAssessment` no es serializable.
        "iv_assessments": ass,
        "iv_count": len(ass),
        "iv_discarded": dict(_NOTA),
        "surface": sur,
        "chain_rows": len(_rows(chain)),
        "symbol": str(symbol or "").upper(),
    }


__all__ = ["build", "iv_assessments", "surface_slices", "surface_report",
           "MIN_STRIKES_PER_SLICE", "MIN_SLICES", "MAX_ASSESSMENTS"]
