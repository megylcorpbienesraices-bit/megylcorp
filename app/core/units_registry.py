"""Registro canónico de unidades y semántica de exposición (v1.42).

POR QUÉ
-------
«GEX» no es una magnitud. Son al menos tres magnitudes con el mismo nombre:

    Σ γ·OI·M            → variación de delta en ACCIONES por cada $1
    Σ γ·OI·M·S          → variación de delta en DÓLARES por cada $1
    Σ γ·OI·M·S²·0.01    → variación de delta en DÓLARES por cada 1 %

Para DIA a 450 el tercero es ~4,5 veces el segundo y ~2.025 veces el primero. Quant
Data permite pedir exposición como RAW, PER_ONE_DOLLAR_MOVE o PER_ONE_PERCENT_MOVE,
así que comparar «nuestro GEX» contra «su GEX» sin declarar representación produce
una divergencia del 400 % que no es una divergencia: es una confusión de unidades.

Antes de v1.42 eso se resolvía calculándolo todo igual y confiando. Confiar no es
un control. Aquí cada exposición viaja con su unidad, su convención de signo, su
universo de vencimientos, su precio de subyacente y su marca de tiempo, y comparar
dos exposiciones exige que esas siete cosas coincidan. Si no coinciden, el resultado
no es «conflicto»: es NOT_COMPARABLE, que es una respuesta distinta y honesta.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .quant_errors import UnitMismatch

# ── griegas reconocidas ─────────────────────────────────────────────────────────
GREEK_GAMMA = "GAMMA"
GREEK_DELTA = "DELTA"
GREEK_VANNA = "VANNA"
GREEK_CHARM = "CHARM"
GREEK_VEGA = "VEGA"
GREEK_SPEED = "SPEED"

# ── representaciones ────────────────────────────────────────────────────────────
REP_RAW = "RAW"                              # sin escalar por el precio
REP_PER_ONE_DOLLAR = "PER_ONE_DOLLAR_MOVE"   # por cada $1 de movimiento
REP_PER_ONE_PERCENT = "PER_ONE_PERCENT_MOVE" # por cada 1 % de movimiento
REP_NOTIONAL = "NOTIONAL"                    # nocional en dólares
REP_SHARES = "SHARES"                        # equivalente en acciones
REP_PER_VOL_POINT = "PER_VOL_POINT"          # por 1 punto de volatilidad (1 %)
REP_PER_MINUTE = "PER_MINUTE"                # por minuto de sesión
REP_PER_DAY = "PER_DAY"                      # por día de sesión

# ── unidades canónicas (nombre público de cada combinación) ─────────────────────
UNITS: Dict[str, Dict[str, Any]] = {
    "GAMMA_RAW":            {"greek": GREEK_GAMMA, "rep": REP_RAW,             "dim": "SHARES_PER_DOLLAR",
                             "label": "Δ acciones por $1"},
    "GEX_PER_1D":           {"greek": GREEK_GAMMA, "rep": REP_PER_ONE_DOLLAR,  "dim": "USD_PER_DOLLAR",
                             "label": "USD de delta por $1"},
    "GEX_PER_1PCT":         {"greek": GREEK_GAMMA, "rep": REP_PER_ONE_PERCENT, "dim": "USD_PER_PERCENT",
                             "label": "USD de delta por 1 %"},
    "DEX_SHARES":           {"greek": GREEK_DELTA, "rep": REP_SHARES,          "dim": "SHARES",
                             "label": "acciones equivalentes"},
    "DEX_NOTIONAL":         {"greek": GREEK_DELTA, "rep": REP_NOTIONAL,        "dim": "USD",
                             "label": "USD nocionales"},
    "VANNA_PER_VOL_POINT":  {"greek": GREEK_VANNA, "rep": REP_PER_VOL_POINT,   "dim": "USD_PER_VOLPT",
                             "label": "USD de delta por punto de vol"},
    "CHARM_PER_MINUTE":     {"greek": GREEK_CHARM, "rep": REP_PER_MINUTE,      "dim": "USD_PER_MINUTE",
                             "label": "USD de delta por minuto"},
    "CHARM_PER_DAY":        {"greek": GREEK_CHARM, "rep": REP_PER_DAY,         "dim": "USD_PER_DAY",
                             "label": "USD de delta por día"},
    "VEGA_PER_VOL_POINT":   {"greek": GREEK_VEGA,  "rep": REP_PER_VOL_POINT,   "dim": "USD_PER_VOLPT",
                             "label": "USD por punto de vol"},
    "SPEED_RAW":            {"greek": GREEK_SPEED, "rep": REP_RAW,             "dim": "SHARES_PER_DOLLAR2",
                             "label": "Δ gamma por $1"},
}

# ── convenciones de signo ───────────────────────────────────────────────────────
# Quién está en el lado que se describe. Dos casas pueden publicar el mismo número
# con signo opuesto y ambas tener razón; lo que no se puede es no decirlo.
SIGN_DEALER = "DEALER"        # positivo = el dealer está largo de esa griega
SIGN_CUSTOMER = "CUSTOMER"    # positivo = el cliente está largo
SIGN_CALL_MINUS_PUT = "CALL_MINUS_PUT"   # call positivo, put negativo, sin inferir lado
SIGN_ABSOLUTE = "ABSOLUTE"    # magnitud sin dirección

SIGN_CONVENTIONS = (SIGN_DEALER, SIGN_CUSTOMER, SIGN_CALL_MINUS_PUT, SIGN_ABSOLUTE)

# El opuesto exacto de cada convención, cuando existe. Sirve para normalizar sin
# tener que decidir arbitrariamente cuál es «la buena».
_SIGN_FLIP = {SIGN_DEALER: SIGN_CUSTOMER, SIGN_CUSTOMER: SIGN_DEALER}

DEFAULT_COMPARE_TOLERANCE = 0.05   # 5 % de dispersión relativa
DEFAULT_TIMESTAMP_TOLERANCE_S = 90.0
DEFAULT_SPOT_TOLERANCE_PCT = 0.25  # 0,25 % de diferencia de subyacente


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class ExposureQuantity:
    """Una cifra de exposición con todo lo necesario para saber qué significa.

    Los siete campos que decide `comparable()` no son burocracia: cada uno ha
    producido, en algún terminal real, una comparación falsa entre dos casas.
    """

    value: float
    unit: str
    underlying: str
    spot: Optional[float] = None
    sign_convention: str = SIGN_DEALER
    expiration_universe: str = "ALL"      # ALL | 0DTE | FRONT | <fecha ISO> | ...
    timestamp: Optional[float] = None     # epoch segundos
    source: str = "ITM"
    multiplier_source: str = "REGISTRY"
    notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.unit not in UNITS:
            raise UnitMismatch(str(self.unit), "|".join(sorted(UNITS)),
                               "unidad no registrada")
        if self.sign_convention not in SIGN_CONVENTIONS:
            raise UnitMismatch(str(self.sign_convention), "|".join(SIGN_CONVENTIONS),
                               "convención de signo no registrada")

    @property
    def greek(self) -> str:
        return UNITS[self.unit]["greek"]

    @property
    def representation(self) -> str:
        return UNITS[self.unit]["rep"]

    @property
    def dimension(self) -> str:
        return UNITS[self.unit]["dim"]

    @property
    def label(self) -> str:
        return UNITS[self.unit]["label"]

    def describe(self) -> Dict[str, Any]:
        return {"value": self.value, "unit": self.unit, "greek": self.greek,
                "representation": self.representation, "dimension": self.dimension,
                "label": self.label, "underlying": self.underlying, "spot": self.spot,
                "sign_convention": self.sign_convention,
                "expiration_universe": self.expiration_universe,
                "timestamp": self.timestamp, "source": self.source,
                "multiplier_source": self.multiplier_source, "notes": self.notes}

    # ── conversiones ────────────────────────────────────────────────────────────
    def to(self, unit: str, *, minutes_per_year: float = 98280.0) -> "ExposureQuantity":
        """Convierte a otra unidad de la MISMA griega. Nunca entre griegas.

        Convertir gamma a delta no es una conversión de unidades, es otra magnitud;
        si alguien lo pide es un error de programación y debe verse como tal.
        """
        if unit == self.unit:
            return self
        if unit not in UNITS:
            raise UnitMismatch(str(unit), "|".join(sorted(UNITS)), "unidad no registrada")
        if UNITS[unit]["greek"] != self.greek:
            raise UnitMismatch(self.unit, unit,
                               "no se convierte entre griegas distintas; son magnitudes diferentes")
        factor = _conversion_factor(self.unit, unit, self.spot, minutes_per_year)
        if factor is None:
            raise UnitMismatch(self.unit, unit,
                               "la conversión requiere el precio del subyacente y no está disponible")
        return ExposureQuantity(
            value=self.value * factor, unit=unit, underlying=self.underlying, spot=self.spot,
            sign_convention=self.sign_convention, expiration_universe=self.expiration_universe,
            timestamp=self.timestamp, source=self.source,
            multiplier_source=self.multiplier_source, notes=self.notes, extra=dict(self.extra))

    def with_sign_convention(self, convention: str) -> "ExposureQuantity":
        if convention == self.sign_convention:
            return self
        flip = _SIGN_FLIP.get(self.sign_convention)
        if convention != flip:
            raise UnitMismatch(self.sign_convention, convention,
                               "estas convenciones no son el reflejo exacto una de otra")
        return ExposureQuantity(
            value=-self.value, unit=self.unit, underlying=self.underlying, spot=self.spot,
            sign_convention=convention, expiration_universe=self.expiration_universe,
            timestamp=self.timestamp, source=self.source,
            multiplier_source=self.multiplier_source, notes=self.notes, extra=dict(self.extra))


def _conversion_factor(src: str, dst: str, spot: Optional[float],
                       minutes_per_year: float) -> Optional[float]:
    """Factor multiplicativo src→dst. None si falta un dato imprescindible."""
    s = _f(spot)
    pairs: Dict[Tuple[str, str], Any] = {
        ("GAMMA_RAW", "GEX_PER_1D"):      (lambda: s),
        ("GEX_PER_1D", "GAMMA_RAW"):      (lambda: 1.0 / s if s else None),
        ("GEX_PER_1D", "GEX_PER_1PCT"):   (lambda: s * 0.01 if s else None),
        ("GEX_PER_1PCT", "GEX_PER_1D"):   (lambda: 100.0 / s if s else None),
        ("GAMMA_RAW", "GEX_PER_1PCT"):    (lambda: (s * s * 0.01) if s else None),
        ("GEX_PER_1PCT", "GAMMA_RAW"):    (lambda: 100.0 / (s * s) if s else None),
        ("DEX_SHARES", "DEX_NOTIONAL"):   (lambda: s),
        ("DEX_NOTIONAL", "DEX_SHARES"):   (lambda: 1.0 / s if s else None),
        ("CHARM_PER_MINUTE", "CHARM_PER_DAY"): (lambda: 390.0),
        ("CHARM_PER_DAY", "CHARM_PER_MINUTE"): (lambda: 1.0 / 390.0),
    }
    fn = pairs.get((src, dst))
    if fn is None:
        return None
    try:
        out = fn()
    except ZeroDivisionError:
        return None
    return None if out is None or not math.isfinite(out) else float(out)


def comparable(a: ExposureQuantity, b: ExposureQuantity, *,
               timestamp_tolerance_s: float = DEFAULT_TIMESTAMP_TOLERANCE_S,
               spot_tolerance_pct: float = DEFAULT_SPOT_TOLERANCE_PCT) -> Dict[str, Any]:
    """¿Describen estas dos cifras el mismo fenómeno?

    Devuelve un veredicto, no lanza: el llamador casi siempre quiere mostrar «no
    comparables y por qué» en vez de perder la pantalla. Los siete controles son
    los que el propio informe de arquitectura exige antes de enfrentar ITM contra
    Quant Data: misma griega, misma representación, mismo universo de vencimientos,
    mismo instante, mismo subyacente, mismas unidades y mismo signo.
    """
    blockers: list[str] = []

    if a.underlying != b.underlying:
        blockers.append(f"subyacentes distintos ({a.underlying} vs {b.underlying})")
    if a.greek != b.greek:
        blockers.append(f"griegas distintas ({a.greek} vs {b.greek})")
    if a.expiration_universe != b.expiration_universe:
        blockers.append(f"universo de vencimientos distinto "
                        f"({a.expiration_universe} vs {b.expiration_universe})")

    # Representación y dimensión: si son de la misma griega pero distinta unidad,
    # se intenta llevar `b` a la unidad de `a`. Si no se puede, no son comparables.
    rhs = b
    converted = False
    if a.greek == b.greek and a.unit != b.unit:
        try:
            rhs = b.to(a.unit)
            converted = True
        except UnitMismatch as exc:
            blockers.append(f"unidades no reconciliables ({b.unit} → {a.unit}): {exc.detail.get('reason','')}")

    if a.sign_convention != rhs.sign_convention:
        try:
            rhs = rhs.with_sign_convention(a.sign_convention)
        except UnitMismatch:
            blockers.append(f"convenciones de signo incompatibles "
                            f"({a.sign_convention} vs {b.sign_convention})")

    ta, tb = _f(a.timestamp), _f(b.timestamp)
    age_gap = None
    if ta is not None and tb is not None:
        age_gap = abs(ta - tb)
        if age_gap > float(timestamp_tolerance_s):
            blockers.append(f"instantes separados {age_gap:.0f}s (límite {timestamp_tolerance_s:.0f}s)")

    sa, sb = _f(a.spot), _f(b.spot)
    spot_gap_pct = None
    if sa is not None and sb is not None and sa > 0:
        spot_gap_pct = abs(sa - sb) / sa * 100.0
        if spot_gap_pct > float(spot_tolerance_pct):
            blockers.append(f"precio de subyacente distinto en {spot_gap_pct:.2f} % "
                            f"(límite {spot_tolerance_pct:.2f} %)")

    ok = not blockers
    out: Dict[str, Any] = {
        "comparable": ok,
        "status": "COMPARABLE" if ok else "NOT_COMPARABLE",
        "blockers": blockers,
        "converted": converted,
        "unit": a.unit,
        "timestamp_gap_s": None if age_gap is None else round(age_gap, 1),
        "spot_gap_pct": None if spot_gap_pct is None else round(spot_gap_pct, 3),
        "left": a.describe(),
        "right": (rhs if ok else b).describe(),
    }
    if ok:
        scale = max(abs(a.value), abs(rhs.value), 1e-9)
        out["difference"] = round(a.value - rhs.value, 6)
        out["difference_pct"] = round(abs(a.value - rhs.value) / scale * 100.0, 3)
    return out


def compare_or_raise(a: ExposureQuantity, b: ExposureQuantity, **kw: Any) -> Dict[str, Any]:
    """Versión fail-closed para la ruta crítica."""
    verdict = comparable(a, b, **kw)
    if not verdict["comparable"]:
        raise UnitMismatch(a.unit, b.unit, "; ".join(verdict["blockers"]))
    return verdict


# ── constructores desde el motor ────────────────────────────────────────────────

def gamma_exposure(value: float, *, underlying: str, spot: float,
                   representation: str = REP_PER_ONE_PERCENT, **kw: Any) -> ExposureQuantity:
    unit = {REP_RAW: "GAMMA_RAW", REP_PER_ONE_DOLLAR: "GEX_PER_1D",
            REP_PER_ONE_PERCENT: "GEX_PER_1PCT"}.get(representation)
    if unit is None:
        raise UnitMismatch(str(representation), "RAW|PER_ONE_DOLLAR_MOVE|PER_ONE_PERCENT_MOVE",
                           "representación de gamma no registrada")
    return ExposureQuantity(value=float(value), unit=unit, underlying=str(underlying).upper(),
                            spot=float(spot), **kw)


def delta_exposure(value: float, *, underlying: str, spot: float,
                   representation: str = REP_NOTIONAL, **kw: Any) -> ExposureQuantity:
    unit = {REP_SHARES: "DEX_SHARES", REP_NOTIONAL: "DEX_NOTIONAL"}.get(representation)
    if unit is None:
        raise UnitMismatch(str(representation), "SHARES|NOTIONAL",
                           "representación de delta no registrada")
    return ExposureQuantity(value=float(value), unit=unit, underlying=str(underlying).upper(),
                            spot=float(spot), **kw)


def registry_snapshot() -> Dict[str, Any]:
    """Lo que el Auditor y la documentación publican como contrato de unidades."""
    return {
        "units": {k: dict(v) for k, v in UNITS.items()},
        "sign_conventions": list(SIGN_CONVENTIONS),
        "representations": [REP_RAW, REP_PER_ONE_DOLLAR, REP_PER_ONE_PERCENT, REP_NOTIONAL,
                            REP_SHARES, REP_PER_VOL_POINT, REP_PER_MINUTE, REP_PER_DAY],
        "comparability_checks": ["same_greek", "same_representation", "same_expiration_universe",
                                 "same_timestamp_window", "same_underlying", "same_units",
                                 "same_sign_convention"],
        "default_tolerances": {"value_pct": DEFAULT_COMPARE_TOLERANCE * 100.0,
                               "timestamp_s": DEFAULT_TIMESTAMP_TOLERANCE_S,
                               "spot_pct": DEFAULT_SPOT_TOLERANCE_PCT},
    }
