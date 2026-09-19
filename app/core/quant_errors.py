"""Excepciones tipadas y política fail-closed / fail-soft (v1.42).

EL PROBLEMA QUE RESUELVE
------------------------
`except Exception` no está mal por sí mismo: mantiene viva una terminal cuando falla
un widget. El problema es que trata igual dos cosas que no lo son:

  - «no se pudo pintar el sparkline de VIX»  → seguir, nadie opera por eso;
  - «no sé el multiplicador de este contrato» → NO seguir, porque continuar significa
    publicar una exposición en dólares calculada con un tamaño inventado.

La segunda, atrapada por un `except Exception` genérico, no desaparece: se convierte
en un número plausible. Un error visible cuesta una recarga; un número plausible y
falso cuesta una operación.

LA POLÍTICA
-----------
    RUTA CRÍTICA  → FAIL CLOSED   (contrato, quote, precio, IV, unidades, costes)
    WIDGET VISUAL → FAIL SOFT     (gráficos, adornos, texto de contexto)

`critical()` y `soft()` marcan la intención en el punto de uso, y el Auditor puede
contar cuántas veces se degradó cada ruta en lugar de descubrirlo en producción.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator


class QuantError(Exception):
    """Raíz de todo fallo que el motor sabe nombrar.

    Lleva `detail` estructurado porque el Auditor consume estos fallos como datos,
    no como texto, y un mensaje formateado no se puede agregar por categoría.
    """

    code = "QUANT_ERROR"
    critical_path = True

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail: Dict[str, Any] = dict(detail)

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": str(self), "critical": self.critical_path,
                **self.detail}


class ProviderUnavailable(QuantError):
    code = "PROVIDER_UNAVAILABLE"

    def __init__(self, provider: str, reason: str = "", **detail: Any):
        super().__init__(f"Proveedor no disponible: {provider}. {reason}".strip(),
                         provider=provider, reason=reason, **detail)


class StaleData(QuantError):
    code = "STALE_DATA"

    def __init__(self, what: str, age_seconds: float, limit_seconds: float, **detail: Any):
        super().__init__(
            f"{what} tiene {age_seconds:.1f}s de antigüedad; el límite es {limit_seconds:.1f}s",
            what=what, age_seconds=float(age_seconds), limit_seconds=float(limit_seconds), **detail)


class InvalidContract(QuantError):
    code = "INVALID_CONTRACT"

    def __init__(self, contract: str, reason: str = "", **detail: Any):
        super().__init__(f"Contrato inválido {contract!r}: {reason}".strip(),
                         contract=str(contract), reason=reason, **detail)


class InvalidQuote(QuantError):
    code = "INVALID_QUOTE"

    def __init__(self, contract: str, reason: str = "", **detail: Any):
        super().__init__(f"Cotización inválida en {contract!r}: {reason}".strip(),
                         contract=str(contract), reason=reason, **detail)


class PricingFailure(QuantError):
    code = "PRICING_FAILURE"


class IVFailure(QuantError):
    code = "IV_FAILURE"


class SurfaceArbitrage(QuantError):
    code = "SURFACE_ARBITRAGE"

    def __init__(self, kind: str, detail_text: str = "", **detail: Any):
        super().__init__(f"Arbitraje en la superficie ({kind}): {detail_text}".strip(),
                         kind=kind, **detail)


class UnitMismatch(QuantError):
    code = "UNIT_MISMATCH"

    def __init__(self, left: str, right: str, reason: str = "", **detail: Any):
        super().__init__(
            f"Unidades incompatibles: {left} vs {right}. {reason}".strip(),
            left=str(left), right=str(right), reason=reason, **detail)


class CalibrationUnavailable(QuantError):
    code = "CALIBRATION_UNAVAILABLE"


class ExecutionCostUnavailable(QuantError):
    code = "EXECUTION_COST_UNAVAILABLE"


class ModelRiskExceeded(QuantError):
    code = "MODEL_RISK_EXCEEDED"


CRITICAL_ERRORS = (
    ProviderUnavailable, StaleData, InvalidContract, InvalidQuote, PricingFailure,
    IVFailure, SurfaceArbitrage, UnitMismatch, CalibrationUnavailable,
    ExecutionCostUnavailable, ModelRiskExceeded,
)

# Registro en memoria de degradaciones, para que el Auditor no tenga que adivinar.
_DEGRADATIONS: list[Dict[str, Any]] = []
_MAX_DEGRADATIONS = 500


def record_degradation(where: str, exc: BaseException, *, critical: bool) -> None:
    from .obs import note as _obs_note
    entry = {"where": where, "error": type(exc).__name__, "message": str(exc)[:300],
             "critical": bool(critical)}
    _DEGRADATIONS.append(entry)
    if len(_DEGRADATIONS) > _MAX_DEGRADATIONS:
        del _DEGRADATIONS[: len(_DEGRADATIONS) - _MAX_DEGRADATIONS]
    _obs_note(f"quant_errors:{where}", exc, severity="CRITICAL" if critical else "DEGRADED")


def degradations(limit: int = 50) -> list[Dict[str, Any]]:
    return list(_DEGRADATIONS[-int(max(limit, 0)):])


def clear_degradations() -> None:
    _DEGRADATIONS.clear()


@contextmanager
def soft(where: str, default: Any = None) -> Iterator[Dict[str, Any]]:
    """Ruta no crítica: si falla, se registra y se sigue con `default`.

        with soft("trace:sparkline") as box:
            box["value"] = render()
        pintar(box["value"])
    """
    box: Dict[str, Any] = {"value": default, "ok": True, "error": None}
    try:
        yield box
    except QuantError as exc:
        box.update(ok=False, error=exc.as_dict())
        record_degradation(where, exc, critical=False)
    except Exception as exc:  # noqa: BLE001 - frontera deliberada de widget
        box.update(ok=False, error={"code": "UNEXPECTED", "message": str(exc)[:300]})
        record_degradation(where, exc, critical=False)


@contextmanager
def critical(where: str) -> Iterator[None]:
    """Ruta crítica: el fallo se registra y SE PROPAGA.

    Existe para dejar constancia sin ocultar. Si esto envolviera un `except`, sería
    exactamente el problema que el módulo intenta eliminar.
    """
    try:
        yield
    except QuantError as exc:
        record_degradation(where, exc, critical=True)
        raise
    except Exception as exc:  # noqa: BLE001
        record_degradation(where, exc, critical=True)
        raise
