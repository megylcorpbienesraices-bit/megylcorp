"""Challengers del Scenario Lab frente al GBM base (v1.42).

QUÉ SE CONSERVA
---------------
El movimiento browniano geométrico se queda como BASELINE. Es rápido, es el lenguaje
en el que están escritas las bandas del propio mercado de opciones y, para el rango
central de una sesión, describe razonablemente bien lo que pasa.

QUÉ NO DESCRIBE
---------------
Las colas, y el recorrido. Un GBM con σ calibrada acierta la desviación típica y
subestima sistemáticamente:

  - la probabilidad de TOCAR un nivel durante la sesión (el camino importa, no sólo
    el cierre);
  - la frecuencia de días de rango extremo;
  - la asimetría: las caídas del mercado de acciones no son simétricas a las subidas.

Eso importa mucho, porque casi todo lo que decide una operación de opciones es una
probabilidad de toque o una probabilidad de rango, no la varianza del cierre.

LOS CHALLENGERS
---------------
  BLOCK_BOOTSTRAP     remuestrea bloques de retornos históricos contiguos. Conserva
                      el agrupamiento de volatilidad y la asimetría reales sin
                      suponer ninguna distribución.
  REGIME_CONDITIONED  remuestrea sólo días del régimen actual (volatilidad y signo
                      de tendencia parecidos). Menos muestra, más pertinencia.
  JUMP_DIFFUSION      GBM más saltos de Poisson. Recupera las colas que el GBM puro
                      no tiene, a costa de dos parámetros más.

LA COMPARACIÓN
--------------
No se elige por sofisticación. Se evalúa fuera de muestra sobre lo que se usa:
cobertura de los intervalos, probabilidad de toque, rango intradía, asimetría y
error de la distribución realizada. El que gana, gana.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

import numpy as np

DEFAULT_PATHS = 4000
DEFAULT_STEPS = 78          # barras de 5 minutos en una sesión


def _seed(tag: str, *parts: Any) -> int:
    import hashlib
    key = tag + "|" + "|".join(str(p) for p in parts)
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def _paths_to_stats(paths: np.ndarray, spot: float, levels: Sequence[float]) -> Dict[str, Any]:
    """Estadísticos que de verdad se usan: cierre, rango y probabilidad de toque."""
    closes = paths[:, -1]
    highs = paths.max(axis=1)
    lows = paths.min(axis=1)
    out: Dict[str, Any] = {
        "close_p05": float(np.percentile(closes, 5)),
        "close_p50": float(np.percentile(closes, 50)),
        "close_p95": float(np.percentile(closes, 95)),
        "expected_range_pct": float(np.mean(highs - lows) / max(spot, 1e-9) * 100.0),
        "max_range_p95_pct": float(np.percentile(highs - lows, 95) / max(spot, 1e-9) * 100.0),
        "skew": float(((closes - closes.mean()) ** 3).mean() / max(closes.std() ** 3, 1e-18)),
        "kurtosis": float(((closes - closes.mean()) ** 4).mean() / max(closes.std() ** 4, 1e-18)),
    }
    touch = {}
    for lv in levels or ():
        lv = float(lv)
        if lv >= spot:
            touch[f"{lv:g}"] = float(np.mean(highs >= lv))
        else:
            touch[f"{lv:g}"] = float(np.mean(lows <= lv))
    out["touch_probability"] = touch
    return out


def gbm_paths(spot: float, sigma_annual: float, horizon_years: float, *,
              paths: int = DEFAULT_PATHS, steps: int = DEFAULT_STEPS,
              drift: float = 0.0, seed: int | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed if seed is not None else _seed("GBM", spot, sigma_annual, horizon_years))
    dt = max(float(horizon_years), 1e-9) / max(int(steps), 1)
    sig = max(float(sigma_annual), 1e-9)
    shocks = rng.normal((drift - 0.5 * sig * sig) * dt, sig * math.sqrt(dt), size=(paths, steps))
    return float(spot) * np.exp(np.cumsum(shocks, axis=1))


def block_bootstrap_paths(spot: float, returns: Sequence[float], steps: int = DEFAULT_STEPS, *,
                          paths: int = DEFAULT_PATHS, block: int = 12,
                          seed: int | None = None) -> np.ndarray:
    """Remuestreo por bloques: conserva agrupamiento de volatilidad y asimetría.

    El tamaño de bloque es el parámetro que importa. Bloques de 1 destruyen la
    autocorrelación —y con ella el agrupamiento de volatilidad, que es la propiedad
    por la que se hace esto—. Bloques demasiado largos reproducen literalmente el
    pasado y dejan de ser un muestreo.
    """
    r = np.asarray(list(returns), dtype=float)
    r = r[np.isfinite(r)]
    if r.size < max(block * 3, 30):
        raise ValueError(f"histórico insuficiente para bootstrap por bloques: {r.size} retornos")
    rng = np.random.default_rng(seed if seed is not None else _seed("BLOCK", spot, r.size, steps))
    n_blocks = int(math.ceil(steps / block))
    starts = rng.integers(0, r.size - block, size=(paths, n_blocks))
    sampled = np.concatenate([r[s[:, None] + np.arange(block)[None, :]] for s in starts.T], axis=1)
    return float(spot) * np.exp(np.cumsum(sampled[:, :steps], axis=1))


def regime_conditioned_paths(spot: float, returns: Sequence[float], regime_flags: Sequence[bool],
                             steps: int = DEFAULT_STEPS, *, paths: int = DEFAULT_PATHS,
                             block: int = 8, seed: int | None = None) -> np.ndarray:
    """Remuestrea sólo tramos del régimen actual. Menos muestra, más pertinencia."""
    r = np.asarray(list(returns), dtype=float)
    flags = np.asarray(list(regime_flags), dtype=bool)
    if r.size != flags.size:
        raise ValueError("retornos y régimen no alineados")
    idx = np.flatnonzero(flags)
    idx = idx[idx <= r.size - block]
    if idx.size < 20:
        raise ValueError(f"muestra del régimen insuficiente: {idx.size} tramos utilizables")
    rng = np.random.default_rng(seed if seed is not None else _seed("REGIME", spot, idx.size, steps))
    n_blocks = int(math.ceil(steps / block))
    picks = rng.choice(idx, size=(paths, n_blocks))
    sampled = np.concatenate([r[p[:, None] + np.arange(block)[None, :]] for p in picks.T], axis=1)
    return float(spot) * np.exp(np.cumsum(sampled[:, :steps], axis=1))


def jump_diffusion_paths(spot: float, sigma_annual: float, horizon_years: float, *,
                         jump_intensity: float = 12.0, jump_mean: float = -0.004,
                         jump_vol: float = 0.012, paths: int = DEFAULT_PATHS,
                         steps: int = DEFAULT_STEPS, seed: int | None = None) -> np.ndarray:
    """Merton: difusión más saltos de Poisson. `jump_mean` negativo por asimetría."""
    rng = np.random.default_rng(seed if seed is not None else _seed("JUMP", spot, sigma_annual, horizon_years))
    dt = max(float(horizon_years), 1e-9) / max(int(steps), 1)
    sig = max(float(sigma_annual), 1e-9)
    diff = rng.normal(-0.5 * sig * sig * dt, sig * math.sqrt(dt), size=(paths, steps))
    n_j = rng.poisson(max(jump_intensity, 0.0) * dt, size=(paths, steps))
    jumps = rng.normal(jump_mean, jump_vol, size=(paths, steps)) * n_j
    return float(spot) * np.exp(np.cumsum(diff + jumps, axis=1))


@dataclass(frozen=True)
class ScenarioModel:
    name: str
    role: str          # BASELINE | CHALLENGER
    stats: Dict[str, Any]
    ready: bool
    reason: str = ""


def run_all(spot: float, *, sigma_annual: float, horizon_years: float,
            returns: Optional[Sequence[float]] = None,
            regime_flags: Optional[Sequence[bool]] = None,
            levels: Sequence[float] = (), paths: int = DEFAULT_PATHS,
            steps: int = DEFAULT_STEPS) -> Dict[str, Any]:
    """Ejecuta baseline y challengers sobre el mismo horizonte y compara."""
    models: list[ScenarioModel] = []

    base = gbm_paths(spot, sigma_annual, horizon_years, paths=paths, steps=steps)
    models.append(ScenarioModel("GBM", "BASELINE", _paths_to_stats(base, spot, levels), True))

    # `returns or ()` sería ambiguo con un array de numpy: `or` evalúa su verdad y
    # numpy se niega, con razón, a decidir si un vector es verdadero.
    hist = () if returns is None else returns
    regs = () if regime_flags is None else regime_flags
    for name, fn in (
        ("BLOCK_BOOTSTRAP", lambda: block_bootstrap_paths(spot, hist, steps, paths=paths)),
        ("REGIME_CONDITIONED", lambda: regime_conditioned_paths(
            spot, hist, regs, steps, paths=paths)),
        ("JUMP_DIFFUSION", lambda: jump_diffusion_paths(
            spot, sigma_annual, horizon_years, paths=paths, steps=steps)),
    ):
        try:
            p = fn()
        except ValueError as exc:
            models.append(ScenarioModel(name, "CHALLENGER", {}, False, str(exc)))
            continue
        models.append(ScenarioModel(name, "CHALLENGER", _paths_to_stats(p, spot, levels), True))

    ready = [m for m in models if m.ready]
    tails = {m.name: {"skew": round(m.stats["skew"], 4), "kurtosis": round(m.stats["kurtosis"], 3),
                      "expected_range_pct": round(m.stats["expected_range_pct"], 4)}
             for m in ready}
    return {
        "ready": True, "spot": float(spot), "horizon_years": float(horizon_years),
        "paths": int(paths), "steps": int(steps),
        "models": {m.name: {"role": m.role, "ready": m.ready, "reason": m.reason, **m.stats}
                   for m in models},
        "tail_comparison": tails,
        "authority": "GBM",
        "doctrine": ("GBM conserva la autoridad publicada. Los challengers se evalúan contra "
                     "lo realizado —cobertura de intervalos, probabilidad de toque y rango— y "
                     "sólo sustituyen al baseline si ganan fuera de muestra."),
    }


def score_against_realized(model_stats: Dict[str, Any], *, realized_close: float,
                           realized_high: float, realized_low: float,
                           spot: float) -> Dict[str, Any]:
    """Puntúa un modelo contra un día realizado. Se acumula para el juicio OOS."""
    inside = bool(model_stats.get("close_p05", -np.inf) <= realized_close
                  <= model_stats.get("close_p95", np.inf))
    realized_range_pct = (realized_high - realized_low) / max(spot, 1e-9) * 100.0
    exp_range = float(model_stats.get("expected_range_pct") or 0.0)
    return {
        "interval_covered": inside,
        "realized_range_pct": round(realized_range_pct, 4),
        "expected_range_pct": round(exp_range, 4),
        "range_error_pct": round(realized_range_pct - exp_range, 4),
        "abs_range_error_pct": round(abs(realized_range_pct - exp_range), 4),
        "note": ("Una cobertura del 90 % debería cubrir ~90 % de los días. Muy por encima "
                 "significa intervalos inútilmente anchos; muy por debajo, riesgo "
                 "subestimado — y eso último cuesta dinero."),
    }
