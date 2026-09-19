"""Semántica del interés abierto (v1.42).

TRES COSAS DISTINTAS CON NOMBRES PARECIDOS
------------------------------------------
    OPEN INTEREST      contratos vivos. Es un STOCK, y se publica una vez al día,
                       después del proceso nocturno de la OCC.
    OI CHANGE          diferencia entre dos sesiones. Es un FLUJO, y sólo existe si
                       se tienen las dos fotos.
    VOLUME / OI        actividad del día relativa al stock. No es ni lo uno ni lo otro.

Llamar «cambio de OI» a una tabla que contiene OI y volumen es el error más común y
el más caro: lleva a leer como «se abrieron 12.000 contratos» lo que en realidad es
«se negociaron 12.000 contratos», que puede corresponder a 12.000 aperturas, a 12.000
cierres o a cualquier combinación.

FRESCURA
--------
El OI no es tick-by-tick. A las 11:00 de un martes, el OI disponible es el del cierre
del lunes: tiene entre 18 y 19 horas. Eso no está mal — es lo que hay — pero debe
verse, porque un OI de hace tres sesiones y uno de ayer se leen distinto y parecen
idénticos si no se declara la fecha efectiva.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd

KIND_STOCK = "OPEN_INTEREST"
KIND_FLOW = "OI_CHANGE"
KIND_RATIO = "VOLUME_OVER_OI"

FRESHNESS_CURRENT = "CIERRE_ANTERIOR"     # < 1 sesión de retraso: lo normal
FRESHNESS_STALE = "RETRASADO"             # 2+ sesiones
FRESHNESS_UNKNOWN = "SIN_FECHA"           # el proveedor no dice a qué sesión pertenece


def _as_date(v: Any) -> Optional[date]:
    ts = pd.to_datetime(v, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def _sessions_between(effective: date, asof: date) -> int:
    if effective >= asof:
        return 0
    return int(len(pd.bdate_range(effective, asof)) - 1)


@dataclass(frozen=True)
class OIReading:
    kind: str
    value: Optional[float]
    effective_date: Optional[str]
    source: str
    freshness: str
    sessions_behind: Optional[int]
    note: str = ""

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "effective_date": self.effective_date,
                "source": self.source, "freshness": self.freshness,
                "sessions_behind": self.sessions_behind, "note": self.note}


def read_open_interest(value: Any, *, effective_date: Any, source: str = "ALPACA",
                       asof: Any = None) -> OIReading:
    """Una lectura de OI SIEMPRE lleva su fecha efectiva y su retraso."""
    v = pd.to_numeric(value, errors="coerce")
    v = None if pd.isna(v) else float(v)
    eff = _as_date(effective_date)
    ref = _as_date(asof) or datetime.now(timezone.utc).date()
    if eff is None:
        return OIReading(KIND_STOCK, v, None, source, FRESHNESS_UNKNOWN, None,
                         "El proveedor no declara a qué sesión corresponde este OI. Sin esa "
                         "fecha no se distingue 'estable' de 'viejo'.")
    behind = _sessions_between(eff, ref)
    fresh = FRESHNESS_CURRENT if behind <= 1 else FRESHNESS_STALE
    note = ("OI del cierre anterior, que es lo más reciente que publica la OCC."
            if behind <= 1 else
            f"OI con {behind} sesiones de retraso: lo ocurrido desde entonces no está reflejado.")
    return OIReading(KIND_STOCK, v, eff.isoformat(), source, fresh, behind, note)


def compute_oi_change(today: pd.DataFrame, previous: pd.DataFrame, *,
                      key: str = "contract_symbol", column: str = "open_interest",
                      today_date: Any = None, previous_date: Any = None) -> Dict[str, Any]:
    """Cambio de OI REAL: requiere las dos fotos. Sin ellas, no se inventa.

    Esto es lo que separa un flujo medido de un flujo supuesto. Si falta la sesión
    anterior, la respuesta correcta es «no disponible», no el volumen disfrazado.
    """
    if today is None or previous is None or len(today) == 0 or len(previous) == 0:
        return {"ready": False, "kind": KIND_FLOW,
                "reason": ("Falta una de las dos fotos de OI. El cambio de interés abierto "
                           "es una diferencia entre sesiones: sin las dos, no existe. "
                           "El volumen NO es un sustituto.")}
    if key not in today.columns or key not in previous.columns:
        return {"ready": False, "kind": KIND_FLOW, "reason": f"falta la clave {key}"}

    a = today[[key, column]].copy()
    b = previous[[key, column]].copy()
    a[column] = pd.to_numeric(a[column], errors="coerce")
    b[column] = pd.to_numeric(b[column], errors="coerce")
    merged = a.merge(b, on=key, how="outer", suffixes=("_today", "_prev")).fillna(0.0)
    merged["oi_change"] = merged[f"{column}_today"] - merged[f"{column}_prev"]

    d_today, d_prev = _as_date(today_date), _as_date(previous_date)
    return {
        "ready": True, "kind": KIND_FLOW,
        "contracts": int(len(merged)),
        "total_change": float(merged["oi_change"].sum()),
        "opened": float(merged.loc[merged["oi_change"] > 0, "oi_change"].sum()),
        "closed": float(-merged.loc[merged["oi_change"] < 0, "oi_change"].sum()),
        "rows": merged.to_dict(orient="records"),
        "from_session": None if d_prev is None else d_prev.isoformat(),
        "to_session": None if d_today is None else d_today.isoformat(),
        "note": ("Diferencia medida entre dos cierres. Positivo = posición neta abierta; "
                 "negativo = cerrada. No se infiere de la cinta."),
    }


def volume_over_oi(frame: pd.DataFrame, *, volume_column: str = "volume",
                   oi_column: str = "open_interest") -> Dict[str, Any]:
    """Actividad relativa al stock. NO es apertura y se dice explícitamente."""
    if frame is None or len(frame) == 0:
        return {"ready": False, "kind": KIND_RATIO, "reason": "sin cadena"}
    vol = pd.to_numeric(frame.get(volume_column), errors="coerce").fillna(0.0)
    oi = pd.to_numeric(frame.get(oi_column), errors="coerce").fillna(0.0)
    ratio = vol / oi.clip(lower=1.0)
    hot = int((ratio > 1.0).sum())
    return {
        "ready": True, "kind": KIND_RATIO,
        "median": round(float(ratio.median()), 4),
        "p90": round(float(ratio.quantile(0.9)), 4),
        "contracts_above_1x": hot,
        "note": ("Volumen sobre interés abierto. Mide ACTIVIDAD, no apertura: un ratio > 1 "
                 "significa que se negoció más de lo que había vivo, y eso puede ser todo "
                 "aperturas, todo cierres o cualquier mezcla."),
    }


def describe_contract() -> Dict[str, Any]:
    return {
        "kinds": {
            KIND_STOCK: "Contratos vivos al cierre de una sesión. STOCK, publicación diaria.",
            KIND_FLOW: "Diferencia de OI entre dos cierres. FLUJO, requiere ambas fotos.",
            KIND_RATIO: "Volumen sobre OI. Actividad relativa; no indica apertura.",
        },
        "required_metadata": ["effective_date", "source", "freshness"],
        "doctrine": ("Nunca se llama 'cambio de OI' a una tabla que sólo contiene OI y "
                     "volumen. Si falta la sesión anterior, el cambio es NO DISPONIBLE."),
    }
