"""Gobierno champion/challenger con evidencia OOS multi-ventana (v1.42).

QUÉ ESTABA BIEN Y QUÉ FALTABA
-----------------------------
`model_governance.evaluate_candidate` ya impedía promover a ciegas: exige muestra
OOS, sesiones mínimas, evaluación en sombra y purge gap, y protege el drawdown.
Lo que faltaba es lo que convierte esa puerta en evidencia estadística y no en una
foto afortunada:

  1. VARIOS challengers a la vez contra un baseline experto declarado, en vez de
     un único «candidate» anónimo.
  2. VARIAS ventanas temporales. Un modelo que gana una semana no ha demostrado
     nada: con 20 challengers, uno gana la semana por azar. Aquí se exige mayoría
     de ventanas, y eso sí distingue señal de suerte.
  3. El juego de métricas completo, incluidas las que un backtest ingenuo omite:
     calibración (pendiente e intercepto), EV después de costes y estabilidad
     entre regímenes.

LA REGLA DE PROMOCIÓN
---------------------
Un challenger sustituye al champion sólo si:

    gana en ≥ `min_window_wins` de N ventanas OOS purgadas,
    y su agregado supera la puerta de `model_governance`,
    y no empeora materialmente el drawdown,
    y su calibración no se degrada (pendiente lejos de 1 = probabilidades que mienten).

Nada de «esta semana ganó, cámbialo».
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

import numpy as np

from .model_governance import PromotionPolicy, evaluate_candidate

EXPERT_BASELINE = "EXPERT_BASELINE_V1"


# ── métricas ────────────────────────────────────────────────────────────────────

def _clip_p(p: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), 1e-6, 1.0 - 1e-6)


def brier(p: Any, y: Any) -> float:
    p = _clip_p(p); y = np.asarray(y, dtype=float)
    return float(np.mean((p - y) ** 2)) if p.size else float("nan")


def log_loss(p: Any, y: Any) -> float:
    p = _clip_p(p); y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))) if p.size else float("nan")


def calibration_line(p: Any, y: Any) -> Dict[str, float]:
    """Calibración de Cox: regresión LOGÍSTICA de `y` sobre el logit de `p`.

        y ~ Bernoulli(sigmoid(a + b·logit(p)))

    Un modelo perfectamente calibrado da a = 0 y b = 1. La tentación es hacer esta
    regresión por mínimos cuadrados, y está mal: con `y` binaria, OLS sobre el logit
    devuelve aproximadamente la pendiente media de la sigmoide (~0,2) incluso para
    un modelo impecable, de modo que el modelo BUENO parecería peor calibrado que
    uno plano. Con logística, b < 1 significa exceso de confianza —sus 0,9 no
    aciertan el 90 % de las veces— y b > 1, falta de confianza.

    Esa distinción decide tamaño de posición, así que aquí bloquea promociones que
    el Brier por sí solo aprobaría.
    """
    p = _clip_p(p); y = np.asarray(y, dtype=float)
    if p.size < 10 or np.std(p) < 1e-9 or len(np.unique(y)) < 2:
        return {"slope": float("nan"), "intercept": float("nan")}
    x = np.log(p / (1.0 - p))
    X = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2)
    for _ in range(60):
        eta = np.clip(X @ beta, -30.0, 30.0)
        mu = 1.0 / (1.0 + np.exp(-eta))
        W = np.maximum(mu * (1.0 - mu), 1e-8)
        z = eta + (y - mu) / W
        A = X.T @ (X * W[:, None]) + 1e-8 * np.eye(2)
        try:
            new_beta = np.linalg.solve(A, X.T @ (W * z))
        except np.linalg.LinAlgError:
            return {"slope": float("nan"), "intercept": float("nan")}
        if np.max(np.abs(new_beta - beta)) < 1e-9:
            beta = new_beta
            break
        beta = new_beta
    if not np.all(np.isfinite(beta)):
        return {"slope": float("nan"), "intercept": float("nan")}
    return {"slope": float(beta[1]), "intercept": float(beta[0])}


def outcome_metrics(pnl: Any, *, costs: Any = 0.0) -> Dict[str, float]:
    """Expectancy, profit factor, EV neto, drawdown y MFE/MAE si están disponibles."""
    r = np.asarray(pnl, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return {k: float("nan") for k in ("expectancy", "profit_factor", "ev_net",
                                          "drawdown", "hit_rate")}
    c = np.asarray(costs, dtype=float)
    net = r - (c if c.shape == r.shape else float(np.mean(c) if c.size else 0.0))
    wins, losses = net[net > 0], net[net < 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    equity = np.cumsum(net)
    peak = np.maximum.accumulate(equity)
    dd = float(np.max(peak - equity)) if equity.size else 0.0
    return {
        "expectancy": float(np.mean(net)),
        "profit_factor": float(gp / gl) if gl > 0 else float("inf") if gp > 0 else float("nan"),
        "ev_net": float(np.mean(net)),
        "drawdown": dd,
        "hit_rate": float(np.mean(net > 0)),
    }


def regime_stability(per_window: Sequence[Mapping[str, Any]], key: str = "brier") -> float:
    """1 − dispersión relativa de la métrica entre ventanas. 1 = estable, 0 = errático."""
    vals = [float(w.get(key)) for w in per_window if w.get(key) is not None
            and math.isfinite(float(w.get(key)))]
    if len(vals) < 2:
        return float("nan")
    mu = float(np.mean(vals))
    if abs(mu) < 1e-12:
        return float("nan")
    return float(max(0.0, 1.0 - float(np.std(vals)) / abs(mu)))


# ── ventanas purgadas ───────────────────────────────────────────────────────────

def purged_windows(groups: Sequence[Any], *, n_windows: int = 4,
                   purge: int = 1) -> list[Dict[str, list]]:
    """Walk-forward por bloques con purga entre entrenamiento y evaluación.

    `groups` son unidades no solapadas —sesiones, típicamente—. La purga existe
    porque una etiqueta construida sobre la sesión siguiente contamina la anterior:
    sin ella, el modelo «predice» algo que ya vio, y el OOS deja de serlo.
    """
    gs = list(dict.fromkeys(groups))
    n = len(gs)
    out: list[Dict[str, list]] = []
    if n < (n_windows + purge + 1):
        return out
    test_size = max(1, (n - purge) // (n_windows + 1))
    for w in range(n_windows):
        test_end = n - w * test_size
        test_start = max(test_end - test_size, 0)
        train_end = max(test_start - purge, 0)
        if train_end < 2 or test_start >= test_end:
            continue
        out.append({"window": n_windows - w,
                    "train": gs[:train_end],
                    "purge": gs[train_end:test_start],
                    "test": gs[test_start:test_end]})
    return list(reversed(out))


# ── evaluación ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Challenger:
    """Un modelo candidato.

    `fit` es opcional. Si se proporciona, el marco lo reajusta con el tramo de
    entrenamiento de CADA ventana y evalúa en el tramo de prueba: eso es un
    walk-forward de verdad. Si no, el modelo se considera ya entrenado fuera y las
    ventanas sólo miden su estabilidad — útil para un baseline experto, insuficiente
    para promocionar un modelo ajustado.
    """

    name: str
    predict: Callable[[Any], np.ndarray]   # features → probabilidad
    family: str = "UNSPECIFIED"
    fit: Optional[Callable[[Any, Any], Callable[[Any], np.ndarray]]] = None
    monotonic_constraints: Dict[str, int] = field(default_factory=dict)
    note: str = ""

    def predictor_for(self, features: Any, labels: Any) -> Callable[[Any], np.ndarray]:
        if self.fit is None:
            return self.predict
        return self.fit(features, labels)


@dataclass(frozen=True)
class WindowResult:
    window: int
    n: int
    champion: Dict[str, float]
    challenger: Dict[str, float]
    challenger_wins: bool
    reason: str


def _score(p: np.ndarray, y: np.ndarray, pnl: Optional[np.ndarray],
           costs: Optional[np.ndarray]) -> Dict[str, float]:
    out = {"brier": brier(p, y), "log_loss": log_loss(p, y)}
    out.update(calibration_line(p, y))
    if pnl is not None:
        out.update(outcome_metrics(pnl, costs=0.0 if costs is None else costs))
    return out


def evaluate(*, features: Any, labels: Any, groups: Sequence[Any],
             champion: Challenger, challengers: Iterable[Challenger],
             pnl: Any = None, costs: Any = None,
             n_windows: int = 4, purge: int = 1,
             min_window_wins: int | None = None,
             policy: PromotionPolicy | None = None) -> Dict[str, Any]:
    """Compara challengers contra el champion en ventanas OOS purgadas."""
    y = np.asarray(labels, dtype=float)
    g = np.asarray(groups)
    if y.size == 0 or g.size != y.size:
        return {"ready": False, "reason": "etiquetas y grupos no alineados"}

    windows = purged_windows(list(g), n_windows=n_windows, purge=purge)
    if not windows:
        return {"ready": False, "reason": f"histórico insuficiente para {n_windows} ventanas purgadas",
                "groups": int(len(set(g.tolist())))}

    required = int(min_window_wins if min_window_wins is not None
                   else math.ceil(0.75 * len(windows)))
    pnl_a = None if pnl is None else np.asarray(pnl, dtype=float)
    cost_a = None if costs is None else np.asarray(costs, dtype=float)

    report: Dict[str, Any] = {"ready": True, "windows": len(windows),
                              "required_window_wins": required,
                              "champion": champion.name, "challengers": {}}

    for ch in challengers:
        per_window: list[Dict[str, Any]] = []
        wins = 0
        for w in windows:
            mask = np.isin(g, w["test"])
            if mask.sum() < 20:
                continue
            train_mask = np.isin(g, w["train"])
            fx = features[mask] if hasattr(features, "__getitem__") else features
            ftr = features[train_mask] if hasattr(features, "__getitem__") else features
            p_champ = np.asarray(champion.predictor_for(ftr, y[train_mask])(fx), dtype=float)
            p_chal = np.asarray(ch.predictor_for(ftr, y[train_mask])(fx), dtype=float)
            yy = y[mask]
            pn = None if pnl_a is None else pnl_a[mask]
            cs = None if cost_a is None else cost_a[mask]
            s_champ = _score(p_champ, yy, pn, cs)
            s_chal = _score(p_chal, yy, pn, cs)
            # Gana la ventana si mejora Brier Y no destroza la calibración.
            better_brier = (math.isfinite(s_chal["brier"]) and math.isfinite(s_champ["brier"])
                            and s_chal["brier"] < s_champ["brier"])
            cal_ok = True
            sc, sp = s_chal.get("slope"), s_champ.get("slope")
            if sc is not None and sp is not None and math.isfinite(sc) and math.isfinite(sp):
                cal_ok = abs(sc - 1.0) <= abs(sp - 1.0) + 0.15
            won = bool(better_brier and cal_ok)
            wins += int(won)
            per_window.append({"window": w["window"], "n": int(mask.sum()),
                               "champion": s_champ, "challenger": s_chal,
                               "challenger_wins": won,
                               "reason": ("mejor Brier con calibración sostenida" if won else
                                          "no mejora Brier" if not better_brier else
                                          "mejora Brier pero degrada la calibración")})

        if not per_window:
            report["challengers"][ch.name] = {"ready": False,
                                              "reason": "ninguna ventana con muestra suficiente"}
            continue

        def _mean(rows, key):
            vals = [float(r.get(key)) for r in rows
                    if r.get(key) is not None and math.isfinite(float(r.get(key)))]
            return float(np.mean(vals)) if vals else float("nan")

        agg_champ = {k: _mean([w["champion"] for w in per_window], k)
                     for k in per_window[0]["champion"]}
        agg_chal = {k: _mean([w["challenger"] for w in per_window], k)
                    for k in per_window[0]["challenger"]}
        agg_champ["stability"] = regime_stability([w["champion"] for w in per_window])
        agg_chal["stability"] = regime_stability([w["challenger"] for w in per_window])

        samples = int(sum(w["n"] for w in per_window))
        decision = evaluate_candidate(agg_champ, agg_chal, samples=samples,
                                      sessions=len(set(g.tolist())), shadow=True,
                                      purge_gap=True, policy=policy)
        enough_windows = wins >= required
        reasons = list(decision.reasons)
        if not enough_windows:
            reasons.append(f"gana {wins}/{len(per_window)} ventanas; se exigen {required}. "
                           "Ganar una ventana no distingue señal de azar.")
        promotable = bool(decision.promotable and enough_windows)

        report["challengers"][ch.name] = {
            "ready": True, "family": ch.family, "note": ch.note,
            "window_wins": wins, "windows_evaluated": len(per_window),
            "promotable": promotable,
            "state": "PROMOTE" if promotable else "SHADOW",
            "reasons": reasons,
            "aggregate": {"champion": agg_champ, "challenger": agg_chal},
            "per_window": per_window,
        }

    promoted = [n for n, r in report["challengers"].items()
                if isinstance(r, dict) and r.get("promotable")]
    report["promoted"] = promoted
    report["authority"] = "CHAMPION_ONLY" if not promoted else "CHAMPION_UNTIL_OPERATOR_PROMOTES"
    report["doctrine"] = ("El champion conserva la autoridad hasta que un challenger gana "
                          "la mayoría de ventanas OOS purgadas Y pasa la puerta de gobierno. "
                          "La promoción nunca es automática dentro de una sesión.")
    return report


def baseline_weights_contract(weights: Mapping[str, float], *, name: str = EXPERT_BASELINE) -> Dict[str, Any]:
    """Declara unos pesos como criterio experto, no como resultado de un ajuste.

    Los pesos de Gamma Structure (0.32 masa, 0.28 intensidad, …) son razonables y
    útiles, pero no salieron de una optimización sobre datos. Llamarlos calibrados
    sería falso; nombrarlos baseline experto los hace auditables y comparables.
    """
    total = float(sum(abs(float(v)) for v in weights.values())) or 1.0
    return {"name": name, "kind": "EXPERT_JUDGEMENT",
            "weights": {k: round(float(v), 6) for k, v in weights.items()},
            "normalized": {k: round(abs(float(v)) / total, 6) for k, v in weights.items()},
            "fitted": False,
            "note": ("Criterio experto declarado. No procede de una optimización sobre datos; "
                     "compite contra challengers ajustados en la puerta OOS multi-ventana.")}
