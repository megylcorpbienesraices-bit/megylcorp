"""Instrument metadata layer (v1.16.2).

THE ONE RULE THIS FILE ENFORCES
-------------------------------
This layer may describe PHYSICAL properties of an instrument. It may never contain a
signal rule. `multiplier`, `session hours`, `pricing model`, `tick size` and `settlement`
are facts about the contract that exist whether or not anyone trades it. A scanner weight,
a threshold or a chain width is a modelling choice, and making it per-ticker is how a
multi-asset engine quietly becomes thirteen single-asset engines that happen to share a
repository.

So: `if symbol == "DIA"` is forbidden in production formulas. `instrument.multiplier` is
required, because pretending an S&P 500 index option has the same contract size as an ETF
option is not neutrality, it is an error.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
VIX. Standard VIX options are cash-settled (VRO) and should not be priced as ordinary equity
options directly off VIX spot. Their valuation is forward/term-structure sensitive. A proper
implementation needs the VX/VIX forward curve; without it, presenting spot-based Greeks as
institutional would be misleading. VIX therefore remains UNSUPPORTED until that input exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

# Trading time per year, per session type. The stop and every sigma conversion divide by
# this, so getting it wrong silently rescales every risk number for that instrument.
SESSIONS: Dict[str, Dict[str, float]] = {
    "US_EQUITY":   {"minutes_per_day": 390.0,  "days_per_year": 252.0, "label": "09:30-16:00 ET"},
    "US_INDEX":    {"minutes_per_day": 405.0,  "days_per_year": 252.0, "label": "09:30-16:15 ET"},
    "CME_FUTURES": {"minutes_per_day": 1380.0, "days_per_year": 252.0, "label": "casi 23h"},
}

MODEL_EQUITY = "EQUITY_OPTION"      # Black-Scholes on spot, continuous dividend yield
MODEL_INDEX = "INDEX_OPTION"        # BS on spot, cash-settled, European
MODEL_FUTURE = "FUTURE_OPTION"      # Black-76 on the future, not on spot
MODEL_UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str = "ETF"
    multiplier: float = 100.0
    session: str = "US_EQUITY"
    option_model: str = MODEL_EQUITY
    settlement: str = "PHYSICAL"
    exercise: str = "AMERICAN"
    tick_size: float = 0.01
    supported: bool = True
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def minutes_per_year(self) -> float:
        s = SESSIONS.get(self.session, SESSIONS["US_EQUITY"])
        return float(s["minutes_per_day"]) * float(s["days_per_year"])

    @property
    def session_label(self) -> str:
        return str(SESSIONS.get(self.session, SESSIONS["US_EQUITY"])["label"])


_ETF = dict(asset_class="ETF", multiplier=100.0, session="US_EQUITY",
            option_model=MODEL_EQUITY, settlement="PHYSICAL", exercise="AMERICAN")
_EQ = dict(_ETF, asset_class="EQUITY")
_IDX = dict(asset_class="INDEX", multiplier=100.0, session="US_INDEX",
            option_model=MODEL_INDEX, settlement="CASH", exercise="EUROPEAN")
_FUT = dict(asset_class="FUTURE", session="CME_FUTURES", option_model=MODEL_FUTURE,
            settlement="CASH", exercise="AMERICAN")

REGISTRY: Dict[str, Instrument] = {
    **{s: Instrument(s, **_ETF) for s in ("DIA", "SPY", "QQQ", "TQQQ", "IWM", "IBIT", "XLK", "XLF", "XLI", "SMH", "SOXX", "XLC", "XLY", "GLD", "GDX", "VXX")},
    **{s: Instrument(s, **_EQ) for s in ("AAPL", "NVDA", "MSFT", "META", "AMZN", "TSLA")},
    "SPX": Instrument("SPX", **_IDX),
    "NDX": Instrument("NDX", **_IDX),
    "DJX": Instrument("DJX", **_IDX),
    "RUT": Instrument("RUT", **_IDX),
    "ES": Instrument("ES", multiplier=50.0, tick_size=0.25, **_FUT),
    "NQ": Instrument("NQ", multiplier=20.0, tick_size=0.25, **_FUT),
    "YM": Instrument("YM", multiplier=5.0, tick_size=1.0, **_FUT),
    "MES": Instrument("MES", multiplier=5.0, tick_size=0.25, **_FUT),
    "MNQ": Instrument("MNQ", multiplier=2.0, tick_size=0.25, **_FUT),
    "MYM": Instrument("MYM", multiplier=0.5, tick_size=1.0, **_FUT),
    "RTY": Instrument("RTY", multiplier=50.0, tick_size=0.1, **_FUT),
    "M2K": Instrument("M2K", multiplier=5.0, tick_size=0.1, **_FUT),
    "GC": Instrument("GC", multiplier=100.0, tick_size=0.1, **_FUT),
    "MGC": Instrument("MGC", multiplier=10.0, tick_size=0.1, **_FUT),
    "VX": Instrument("VX", asset_class="FUTURE", multiplier=1000.0, session="CME_FUTURES", option_model=MODEL_UNSUPPORTED, settlement="CASH", exercise="EUROPEAN", tick_size=0.05, supported=False, reason="VX price/volume/OI supported; derivative-volatility Greeks require a dedicated term-structure model."),
    "VIX": Instrument("VIX", asset_class="INDEX", multiplier=100.0, session="US_INDEX",
                      option_model=MODEL_UNSUPPORTED, settlement="CASH", exercise="EUROPEAN",
                      supported=False,
                      reason="Las opciones estándar VIX son cash-settled (VRO) y su valoración es sensible a la "
                             "estructura forward. No se tratan como opciones equity sobre VIX spot. "
                             "Requiere curva VX/VIX forward antes de habilitar Greeks institucionales."),
}

DEFAULT = Instrument("UNKNOWN", **_ETF)

# Display-only TRACE grid configuration. This never enters signal/model formulas.
TRACE_GRID_STEPS: Dict[str, float] = {
    "DIA": 0.50, "DJX": 0.50, "YM": 50.0,
    "ES": 5.0, "MES": 5.0, "NQ": 25.0, "MNQ": 25.0, "NDX": 25.0, "SPX": 5.0,
    "GC": 5.0, "MGC": 5.0, "MYM": 50.0, "RTY": 5.0, "M2K": 5.0, "RUT": 5.0, "VX": 0.5,
}

def trace_grid_step(symbol: str, spot: float | None = None) -> float:
    key = str(symbol or "").strip().upper()
    configured = TRACE_GRID_STEPS.get(key)
    if configured is not None:
        return float(configured)
    try:
        s = abs(float(spot or 0.0))
    except Exception:
        s = 0.0
    if s >= 1000.0:
        return 5.0
    if s >= 100.0:
        return 0.50
    if s >= 20.0:
        return 0.25
    return 0.10


def get(symbol: str) -> Instrument:
    """Never raises. An unknown ticker gets US equity-option conventions, which is the
    right default for anything Alpaca will hand us, and says so via symbol='UNKNOWN'."""
    key = str(symbol or "").strip().upper()
    inst = REGISTRY.get(key)
    if inst is not None:
        return inst
    return Instrument(key or "UNKNOWN", **_ETF)


def sigma_horizon(spot: float, iv_pct: float, minutes: float, symbol: str = "DIA") -> float:
    """One-sigma move over `minutes` of TRADING time for this instrument's clock.

    The formula is identical for every instrument: sigma = S * IV * sqrt(t/T). Only the
    clock changes, and that is physics, not preference - applying an equity session of
    390 minutes to a future that trades 1,380 understates its horizon variance by more
    than a factor of two.
    """
    import math
    inst = get(symbol)
    try:
        s = float(spot); iv = float(iv_pct) / 100.0; m = max(float(minutes), 1e-9)
        if not (math.isfinite(s) and math.isfinite(iv)) or s <= 0 or iv <= 0:
            return float("nan")
        return s * iv * math.sqrt(m / inst.minutes_per_year)
    except Exception:
        return float("nan")


def describe(symbol: str) -> Dict[str, Any]:
    i = get(symbol)
    return {"symbol": i.symbol, "asset_class": i.asset_class, "multiplier": i.multiplier,
            "session": i.session, "session_hours": i.session_label,
            "minutes_per_year": round(i.minutes_per_year, 1), "option_model": i.option_model,
            "settlement": i.settlement, "exercise": i.exercise, "tick_size": i.tick_size,
            "supported": i.supported, "reason": i.reason}
