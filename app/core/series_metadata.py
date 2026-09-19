"""Metadata obligatoria de cada serie publicada por TRACE (v1.42).

LA REGLA
--------
TRACE no es un motor. Es el RENDER de la misma verdad matemática que usan el Scanner,
Dealer y Exposure. Para que eso sea verificable y no una declaración de intenciones,
cada serie viaja con su ficha:

    métrica · unidad · fuente · modelo · marca de tiempo · frescura · confianza ·
    y si es OBSERVADA, DERIVADA o INFERIDA

Esa última distinción es la que más importa, y la que casi nunca se muestra:

    PRICE           source = ALPACA   type = OBSERVED    — alguien lo cotizó
    GEX             source = ITM      type = DERIVED     — cálculo determinista
    HEDGE_PRESSURE  source = ITM      type = INFERRED    — modelo con incertidumbre

Las tres se pintan como líneas en la misma pantalla y parecen tener el mismo estatus
epistemológico. No lo tienen. La interfaz no necesita mostrarlo todo a la vez, pero
el sistema debe saberlo, y el Auditor debe poder exigirlo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

from .units_registry import UNITS

TYPE_OBSERVED = "OBSERVED"
TYPE_DERIVED = "DERIVED"
TYPE_INFERRED = "INFERRED"

_TYPES = (TYPE_OBSERVED, TYPE_DERIVED, TYPE_INFERRED)

FRESH_LIVE = "LIVE"
FRESH_RECENT = "RECIENTE"
FRESH_STALE = "RANCIO"
FRESH_UNKNOWN = "SIN_MARCA"

LIVE_SECONDS = 5.0
RECENT_SECONDS = 60.0


class SeriesContractError(ValueError):
    """Una serie que no puede describirse no debería pintarse."""


@dataclass(frozen=True)
class SeriesMeta:
    metric: str
    unit: Optional[str]
    source: str
    kind: str                       # OBSERVED | DERIVED | INFERRED
    model: Optional[str] = None
    timestamp: Optional[float] = None       # epoch segundos
    confidence: Optional[float] = None      # 0..1
    note: str = ""
    extra: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in _TYPES:
            raise SeriesContractError(
                f"tipo de serie no reconocido: {self.kind!r}. Debe ser uno de {_TYPES}")
        if self.unit is not None and self.unit not in UNITS and not str(self.unit).isupper():
            raise SeriesContractError(
                f"unidad {self.unit!r} no registrada en units_registry ni declarada explícita")
        if self.kind == TYPE_INFERRED and self.confidence is None:
            raise SeriesContractError(
                f"«{self.metric}» es una INFERENCIA y se publica sin confianza. Una "
                "inferencia sin incertidumbre se lee como un hecho, que es exactamente "
                "lo que no es.")
        if self.kind == TYPE_DERIVED and not self.model:
            raise SeriesContractError(
                f"«{self.metric}» es DERIVADA y no declara con qué modelo. Sin eso no se "
                "puede comprobar que TRACE y el Scanner usan la misma Gamma.")

    @property
    def freshness(self) -> str:
        if self.timestamp is None:
            return FRESH_UNKNOWN
        age = time.time() - float(self.timestamp)
        if age <= LIVE_SECONDS:
            return FRESH_LIVE
        if age <= RECENT_SECONDS:
            return FRESH_RECENT
        return FRESH_STALE

    @property
    def age_seconds(self) -> Optional[float]:
        return None if self.timestamp is None else round(time.time() - float(self.timestamp), 2)

    def describe(self) -> Dict[str, Any]:
        return {"metric": self.metric, "unit": self.unit,
                "unit_label": UNITS.get(self.unit or "", {}).get("label"),
                "source": self.source, "type": self.kind, "model": self.model,
                "timestamp": self.timestamp, "age_seconds": self.age_seconds,
                "freshness": self.freshness,
                "confidence": None if self.confidence is None else round(float(self.confidence), 3),
                "note": self.note}


def observed(metric: str, *, source: str, unit: str | None = None,
             timestamp: float | None = None, note: str = "") -> SeriesMeta:
    return SeriesMeta(metric, unit, source, TYPE_OBSERVED, None, timestamp, None, note)


def derived(metric: str, *, model: str, unit: str | None = None, source: str = "ITM",
            timestamp: float | None = None, confidence: float | None = None,
            note: str = "") -> SeriesMeta:
    return SeriesMeta(metric, unit, source, TYPE_DERIVED, model, timestamp, confidence, note)


def inferred(metric: str, *, model: str, confidence: float, unit: str | None = None,
             source: str = "ITM", timestamp: float | None = None, note: str = "") -> SeriesMeta:
    return SeriesMeta(metric, unit, source, TYPE_INFERRED, model, timestamp, confidence, note)


def bundle(metas: Iterable[SeriesMeta]) -> Dict[str, Any]:
    """Ficha conjunta de un panel. Es lo que consume el Auditor."""
    rows = [m.describe() for m in metas or []]
    by_type: Dict[str, int] = {}
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    stale = [r["metric"] for r in rows if r["freshness"] == FRESH_STALE]
    undated = [r["metric"] for r in rows if r["freshness"] == FRESH_UNKNOWN]
    return {
        "series": rows, "count": len(rows), "by_type": by_type,
        "stale": stale, "undated": undated,
        "ready": not stale and not undated,
        "legend": {
            TYPE_OBSERVED: "Alguien lo cotizó o lo reportó. Es un hecho.",
            TYPE_DERIVED: "Cálculo determinista sobre observaciones. Reproducible.",
            TYPE_INFERRED: "Estimación de un modelo con incertidumbre. Lleva confianza.",
        },
        "doctrine": ("Precio, GEX y presión de cobertura se pintan igual y no son lo mismo. "
                     "La serie declara su naturaleza aunque la interfaz no la muestre."),
    }
