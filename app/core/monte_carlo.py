"""Monte Carlo price-path simulation (SpotGamma-style Equity Hub companion, v1.36).

GBM under risk-neutral drift, using the SAME risk-free rate / dividend yield every
option in this engine is already priced with (precision_engine.market_inputs), and
a single constant volatility equal to today's ATM implied vol -- the same convention
already used for Expected Move in trace_analytics.expected_move_band(). This is a
probability-cone tool, not a forecast: it assumes constant realized vol and
lognormal dynamics, which real markets do not exactly follow (skew, vol clustering,
fat tails). Every output states this; nothing here feeds Scanner direction.

v1.42.2 -- PROBABILIDAD DE TOQUE
--------------------------------
Contar cuantas trayectorias discretas superan un nivel SUBESTIMA el toque: entre dos
puntos simulados el precio pudo cruzar y volver sin dejar rastro. Con 1 paso/dia y un
0DTE eso degeneraba en "tocar == terminar mas alla", con un error medido de ~2x
(17.3% publicado frente a 34.4% real para un nivel a +1% con IV 20% y 1 DTE).

La correccion es el puente browniano: condicionado a sus extremos, el maximo de un
puente tiene distribucion conocida, asi que la probabilidad de que el tramo
[t_i, t_i+1] haya cruzado la barrera b es exactamente

    p_i = exp( -2 * (b - x_i) * (b - x_i+1) / (sigma^2 * dt) )

en log-precios, y P(no tocar) = prod_i (1 - p_i). Es el mismo ajuste que se usa en
valoracion Monte Carlo de barreras discretas, y da precision de monitoreo CONTINUO
sin subir el numero de pasos: el coste de calculo no cambia.
"""
from __future__ import annotations

from typing import Any, Dict
import hashlib
import math
import numpy as np

from .expiry_clock import year_fraction

DEFAULT_SIMS = 20_000
PERCENTILES = (5, 10, 25, 50, 75, 90, 95)


def deterministic_seed(*parts: Any) -> int:
    """Semilla REPRODUCIBLE derivada de las propias entradas.

    v1.55.0 · Hasta aqui produccion llamaba sin `seed`, asi que cada refresco
    sorteaba numeros nuevos. Con 20.000 trayectorias el error tipico de una
    probabilidad cercana al 50 % es ~0,35 pp, de modo que el mismo mercado, sin
    haberse movido un centimo, publicaba 43,8 % y un minuto despues 44,2 %.

    Un operador no tiene forma de distinguir eso de un cambio real, y es lo peor
    que puede hacer una herramienta de probabilidad: moverse sola.

    Derivando la semilla de las entradas, dos ciclos con los MISMOS datos dan el
    MISMO numero, y en cuanto el mercado cambia —spot, IV, DTE o un nivel— la
    semilla cambia con el. No es congelar el resultado: es que solo se mueva
    cuando se mueve el mercado.
    """
    raw = "|".join("" if p is None else f"{p!r}" for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def simulate_gbm_paths(spot: float, r: float, q: float, sigma: float, dte_days: float, *,
                       n_sims: int = DEFAULT_SIMS, steps_per_day: int = 1,
                       seed: int | None = None) -> np.ndarray:
    """Vectorized GBM path simulation.

    Returns an array of shape (n_sims, n_steps+1); paths[:, 0] == spot for every
    row. `steps_per_day=1` is enough for a daily probability cone; raise it only if
    intraday touch resolution is genuinely needed, at proportional compute cost.
    """
    spot = max(float(spot), 1e-9)
    sigma = max(float(sigma), 1e-6)
    dte_days = max(float(dte_days), 1.0 / 1440.0)
    n_steps = max(1, int(math.ceil(dte_days * max(1, int(steps_per_day)))))
    T = year_fraction(dte_days)
    dt = T / n_steps
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((int(n_sims), n_steps))
    drift = (float(r) - float(q) - 0.5 * sigma * sigma) * dt
    diffusion = sigma * math.sqrt(dt) * z
    log_paths = np.cumsum(drift + diffusion, axis=1)
    log_paths = np.concatenate([np.zeros((int(n_sims), 1)), log_paths], axis=1)
    return spot * np.exp(log_paths)


def _bridge_touch_probability(log_paths: np.ndarray, log_level: float, var_step: float,
                              above: bool) -> float:
    """P(tocar el nivel en algun momento hasta expiracion), monitoreo continuo.

    ``log_paths`` son log-precios (n_sims, n_steps+1); ``var_step`` es sigma^2*dt, la
    varianza del incremento logaritmico de un paso. Un tramo cuyo extremo ya esta al
    otro lado de la barrera toca con probabilidad 1; para el resto se aplica la
    formula del puente browniano. Devuelve un porcentaje.
    """
    x0 = log_paths[:, :-1]
    x1 = log_paths[:, 1:]
    if above:
        endpoint_hit = (x0 >= log_level) | (x1 >= log_level)
        d0 = log_level - x0
        d1 = log_level - x1
    else:
        endpoint_hit = (x0 <= log_level) | (x1 <= log_level)
        d0 = x0 - log_level
        d1 = x1 - log_level
    var = max(float(var_step), 1e-300)
    # d0/d1 son > 0 en los tramos que no tocan por extremo; el clip solo protege el
    # signo en los que si tocan, y esos se fuerzan a 1 justo despues.
    exponent = -2.0 * np.maximum(d0, 0.0) * np.maximum(d1, 0.0) / var
    p_cross = np.exp(np.maximum(exponent, -745.0))
    p_cross = np.where(endpoint_hit, 1.0, p_cross)
    p_no_touch = np.prod(1.0 - p_cross, axis=1)
    return float(100.0 * np.mean(1.0 - p_no_touch))


def monte_carlo_level_report(spot: float, r: float, q: float, atm_iv_pct: float, dte_days: float,
                             levels: Dict[str, float | None] | None = None, *,
                             n_sims: int = DEFAULT_SIMS, seed: int | None = None) -> Dict[str, Any]:
    """Probability cone + probability of touching/finishing beyond named structural
    levels by expiration.

    `levels` should come straight from nextgen_terminal.key_levels_report() (e.g.
    {"zero_gamma":..., "call_wall":..., "put_wall":..., "vol_trigger":..., "max_pain":...})
    -- this function computes nothing about WHERE those levels are, only what a GBM
    cone implies about a simulated path reaching them.
    """
    try:
        spot_f = float(spot)
        sigma = max(float(atm_iv_pct), 1e-6) / 100.0
        dte_f = float(dte_days)
        if not (math.isfinite(spot_f) and spot_f > 0 and math.isfinite(dte_f) and dte_f > 0):
            return {"ready": False, "reason": "INVALID_SPOT_OR_DTE"}
        # Sin semilla explicita se DERIVA de las entradas: mismo mercado, mismo
        # numero. Ver `deterministic_seed`.
        resolved_seed = (int(seed) if seed is not None else
                         deterministic_seed(round(spot_f, 6), round(float(r), 8),
                                            round(float(q), 8), round(sigma, 8),
                                            round(dte_f, 8), int(n_sims),
                                            tuple(sorted((str(k), None if v is None else round(float(v), 6))
                                                         for k, v in (levels or {}).items()
                                                         if v is None or isinstance(v, (int, float))))))
        paths = simulate_gbm_paths(spot_f, r, q, sigma, dte_f, n_sims=n_sims, seed=resolved_seed)
    except Exception as exc:
        return {"ready": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}

    terminal = paths[:, -1]
    n_steps = paths.shape[1] - 1
    # Log-precios y varianza por paso: lo que necesita la correccion de puente.
    log_paths = np.log(np.maximum(paths, 1e-300))
    T_years = year_fraction(dte_f)
    var_step = (sigma * sigma) * (T_years / max(n_steps, 1))

    terminal_pct = {int(p): float(np.percentile(terminal, p)) for p in PERCENTILES}
    cone_pct = {int(p): np.percentile(paths, p, axis=0).tolist() for p in PERCENTILES}
    # Eje real en dias, no indices de paso: con steps_per_day>1 o DTE fraccionario el
    # indice de paso no es un dia y el eje del cono quedaba mal etiquetado.
    days_axis = [round(dte_f * i / max(n_steps, 1), 6) for i in range(n_steps + 1)]

    level_stats: Dict[str, Dict[str, Any]] = {}
    for name, lvl in (levels or {}).items():
        try:
            lvl_f = float(lvl) if lvl is not None else None
        except Exception:
            lvl_f = None
        if lvl_f is None or not math.isfinite(lvl_f):
            continue
        if lvl_f <= 0:
            continue
        above = lvl_f >= spot_f
        finish = float(100.0 * np.mean(terminal >= lvl_f if above else terminal <= lvl_f))
        touch = _bridge_touch_probability(log_paths, math.log(lvl_f), var_step, above)
        # Tocar es estrictamente mas debil que terminar mas alla; el puente lo respeta
        # por construccion, pero el suelo deja la invariante explicita y auditable.
        touch = float(min(100.0, max(touch, finish)))
        # Error tipico de la propia simulacion, en puntos porcentuales. Publicarlo
        # es lo que separa «43,8 %» de «43,8 % +/- 0,35»: sin el, una diferencia
        # de tres decimas entre dos niveles parece informacion y es ruido.
        se_finish = math.sqrt(max(finish * (100.0 - finish), 0.0) / max(int(n_sims), 1))
        se_touch = math.sqrt(max(touch * (100.0 - touch), 0.0) / max(int(n_sims), 1))
        level_stats[name] = {
            "level": lvl_f,
            "side": "ABOVE_SPOT" if above else "BELOW_SPOT",
            "prob_finish_beyond_pct": finish,
            "prob_touch_by_expiry_pct": touch,
            "prob_finish_stderr_pp": se_finish,
            "prob_touch_stderr_pp": se_touch,
            "touch_method": "BROWNIAN_BRIDGE_CONTINUOUS",
        }

    return {
        "ready": True, "n_sims": int(n_sims), "n_steps": int(n_steps),
        # Reproducibilidad: con estas mismas entradas sale exactamente esto.
        "seed": int(resolved_seed), "seed_source": "EXPLICIT" if seed is not None else "DERIVED_FROM_INPUTS",
        "deterministic": True,
        "dte_days": dte_f, "atm_iv_pct": float(atm_iv_pct), "spot": spot_f,
        "risk_free_rate": float(r), "dividend_yield": float(q),
        "days_axis": days_axis,
        "terminal_percentiles": terminal_pct,
        "cone_percentiles": cone_pct,
        "levels": level_stats,
        "model": "GBM_CONSTANT_VOL",
        "touch_method": "BROWNIAN_BRIDGE_CONTINUOUS",
        "n_steps_per_day": int(round(n_steps / max(dte_f, 1e-9))),
        "model_risk": ("Simulacion GBM con volatilidad constante = IV ATM observado hoy y deriva "
                      "neutral al riesgo (r-q) ya usada para precificar cada opcion del motor. No "
                      "captura skew, smile ni clustering de volatilidad real. Es un cono de "
                      "probabilidad bajo supuestos simplificados, no una prediccion de precio ni "
                      "una senal de Scanner."),
    }
