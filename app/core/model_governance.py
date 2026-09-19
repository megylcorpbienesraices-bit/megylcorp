"""CURRENT/CANDIDATE quantitative model governance.

The purpose is to make continuous improvement measurable rather than cosmetic.
A CANDIDATE never changes live Scanner authority merely because it exists.  It
must run in SHADOW/OOS, accumulate enough independent observations, and beat the
CURRENT baseline on predeclared metrics without breaching risk guardrails.

This module is deliberately model-agnostic: Scanner weights, probability
calibration, flow normalisation or any future model can use the same contract.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping
import math

LOWER_IS_BETTER = {"brier", "log_loss", "calibration_error", "drawdown", "mae"}
HIGHER_IS_BETTER = {"ev_net", "profit_factor", "hit_rate", "mfe", "stability"}


@dataclass(frozen=True)
class PromotionPolicy:
    min_oos_samples: int = 120
    min_sessions: int = 8
    min_relative_improvement: float = 0.01
    max_drawdown_worsening: float = 0.05
    require_shadow: bool = True
    require_purge_gap: bool = True


@dataclass(frozen=True)
class PromotionDecision:
    state: str
    promotable: bool
    reasons: tuple[str, ...]
    deltas: Mapping[str, float]
    current: Mapping[str, float]
    candidate: Mapping[str, float]
    samples: int
    sessions: int

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["reasons"] = list(self.reasons)
        out["deltas"] = dict(self.deltas)
        out["current"] = dict(self.current)
        out["candidate"] = dict(self.candidate)
        return out


def _finite(v: Any) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _relative_improvement(name: str, current: float, candidate: float) -> float:
    denom = max(abs(current), 1e-12)
    if name in LOWER_IS_BETTER:
        return (current - candidate) / denom
    return (candidate - current) / denom


def evaluate_candidate(
    current: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    samples: int,
    sessions: int,
    shadow: bool,
    purge_gap: bool,
    policy: PromotionPolicy | None = None,
) -> PromotionDecision:
    """Return a deterministic promotion decision without touching LIVE authority."""
    p = policy or PromotionPolicy()
    reasons: list[str] = []
    if int(samples) < p.min_oos_samples:
        reasons.append(f"muestra OOS insuficiente: {samples} < {p.min_oos_samples}")
    if int(sessions) < p.min_sessions:
        reasons.append(f"sesiones insuficientes: {sessions} < {p.min_sessions}")
    if p.require_shadow and not shadow:
        reasons.append("candidate no fue evaluado en SHADOW")
    if p.require_purge_gap and not purge_gap:
        reasons.append("validación sin purge gap")

    shared = sorted((LOWER_IS_BETTER | HIGHER_IS_BETTER) & set(current) & set(candidate))
    deltas: dict[str, float] = {}
    materially_better = 0
    for name in shared:
        c0, c1 = _finite(current.get(name)), _finite(candidate.get(name))
        if c0 is None or c1 is None:
            continue
        rel = _relative_improvement(name, c0, c1)
        deltas[name] = rel
        if rel >= p.min_relative_improvement:
            materially_better += 1

    if not deltas:
        reasons.append("sin métricas comparables finitas")
    elif materially_better == 0:
        reasons.append("candidate no mejora materialmente ninguna métrica declarada")

    # Drawdown is a hard guardrail: a model cannot buy a tiny scoring gain with a
    # materially worse tail profile.
    dd0, dd1 = _finite(current.get("drawdown")), _finite(candidate.get("drawdown"))
    if dd0 is not None and dd1 is not None:
        base = max(abs(dd0), 1e-12)
        worsening = (dd1 - dd0) / base
        if worsening > p.max_drawdown_worsening:
            reasons.append(f"drawdown empeora {worsening:.1%} > {p.max_drawdown_worsening:.1%}")

    promotable = not reasons
    return PromotionDecision(
        state="PROMOTE" if promotable else "SHADOW" if int(samples) else "COLLECTING",
        promotable=promotable,
        reasons=tuple(reasons),
        deltas=deltas,
        current={k: float(v) for k, v in current.items() if _finite(v) is not None},
        candidate={k: float(v) for k, v in candidate.items() if _finite(v) is not None},
        samples=int(samples),
        sessions=int(sessions),
    )


def governance_contract() -> dict[str, Any]:
    """Public, stable description used by docs/UI without enabling a candidate."""
    return {
        "authority": "CURRENT_ONLY",
        "candidate_role": "SHADOW_ONLY_UNTIL_PROMOTED",
        "promotion_requires": ["OOS", "PURGE_GAP", "MIN_SAMPLE", "BASELINE_COMPARISON", "RISK_GUARDRAILS"],
        "lower_is_better": sorted(LOWER_IS_BETTER),
        "higher_is_better": sorted(HIGHER_IS_BETTER),
        "note": "CANDIDATE no altera Scanner hasta superar CURRENT bajo política predeclarada.",
    }
