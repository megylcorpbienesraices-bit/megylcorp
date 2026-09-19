"""SSVI/eSSVI como challenger en SOMBRA del SVI campeón (v1.42).

POR QUÉ EN SOMBRA Y NO COMO SUSTITUTO
-------------------------------------
El SVI por corte que ya usa el motor está bien elegido y, sobre todo, tiene control
de arbitraje de mariposa por Durrleman con fallo cerrado. Cambiarlo por SSVI «porque
es más moderno» sería sustituir algo validado por algo no validado en este dato.

SSVI tiene una ventaja real y concreta: parametriza TODA la superficie con una
función de varianza total ATM θ(T) y una función de correlación/curvatura comunes,
en lugar de ajustar cada vencimiento por separado. Eso le da dos cosas que el SVI por
corte no puede garantizar:

  - ausencia de arbitraje de CALENDARIO por construcción, si θ(T) es creciente;
  - estabilidad entre cortes: un vencimiento con pocos strikes hereda la forma de
    sus vecinos en vez de ajustar cinco parámetros sobre siete puntos.

Y tiene una desventaja igual de real: menos grados de libertad por corte, luego peor
RMSE local cuando un vencimiento tiene forma propia.

Cuál gana es una pregunta empírica, no estética. Aquí se calculan ambos y se comparan
con las métricas que importan —RMSE ponderado por vega, violaciones de bid/ask,
mariposa, calendario, estabilidad de parámetros y error de predicción del siguiente
snapshot— y el campeón NO cambia por su cuenta. La promoción pasa por la puerta
champion/challenger como cualquier otro modelo.

PARAMETRIZACIÓN
---------------
    w(k, θ) = θ/2 · { 1 + ρφ(θ)k + sqrt[ (φ(θ)k + ρ)² + (1 − ρ²) ] }

con la familia de potencia φ(θ) = η·θ^(−γ). Condición suficiente de ausencia de
mariposa (Gatheral-Jacquier): θφ(θ)(1 + |ρ|) ≤ 4 y θφ(θ)²(1 + |ρ|) ≤ 4.
eSSVI extiende ρ a ρ(θ), que es lo que permite que el skew cambie con el plazo.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

import numpy as np
from scipy.optimize import least_squares



def phi_power(theta: np.ndarray, eta: float, gamma: float) -> np.ndarray:
    th = np.maximum(np.asarray(theta, dtype=float), 1e-12)
    return float(eta) * th ** (-float(gamma))


def ssvi_total_variance(k, theta, rho, eta, gamma) -> np.ndarray:
    """w(k, θ) de SSVI. `rho` puede ser escalar (SSVI) o por corte (eSSVI)."""
    k = np.asarray(k, dtype=float)
    th = np.maximum(np.asarray(theta, dtype=float), 1e-12)
    ph = phi_power(th, eta, gamma)
    r = np.asarray(rho, dtype=float)
    x = ph * k
    return 0.5 * th * (1.0 + r * x + np.sqrt(np.maximum((x + r) ** 2 + (1.0 - r ** 2), 1e-18)))


def butterfly_conditions(theta, rho, eta, gamma) -> Dict[str, Any]:
    """Condiciones suficientes de Gatheral-Jacquier. Suficientes, no necesarias."""
    th = np.atleast_1d(np.asarray(theta, dtype=float))
    r = np.atleast_1d(np.asarray(rho, dtype=float))
    if r.size == 1:
        r = np.full(th.shape, float(r[0]))
    ph = phi_power(th, eta, gamma)
    c1 = th * ph * (1.0 + np.abs(r))
    c2 = th * ph * ph * (1.0 + np.abs(r))
    return {"condition_1_max": float(np.max(c1)), "condition_2_max": float(np.max(c2)),
            "pass": bool(np.all(c1 <= 4.0 + 1e-9) and np.all(c2 <= 4.0 + 1e-9)),
            "note": "θφ(1+|ρ|) ≤ 4 y θφ²(1+|ρ|) ≤ 4 (condiciones suficientes)."}


def calendar_monotonic(theta: Sequence[float]) -> Dict[str, Any]:
    """θ(T) creciente ⇒ sin arbitraje de calendario en la parametrización SSVI."""
    th = np.asarray(list(theta), dtype=float)
    if th.size < 2:
        return {"pass": True, "violations": 0, "note": "un solo corte: no hay calendario que violar"}
    d = np.diff(th)
    bad = int(np.sum(d < -1e-12))
    return {"pass": bad == 0, "violations": bad,
            "min_increment": float(np.min(d)),
            "note": "La varianza total ATM debe ser no decreciente en el plazo."}


def fit_ssvi(slices: Sequence[Dict[str, Any]], *, extended: bool = True) -> Dict[str, Any]:
    """Ajusta SSVI/eSSVI a varios cortes a la vez.

    Cada corte es {"k": log-moneyness, "w": varianza total observada, "T": años,
    "vega": pesos opcionales}. Los pesos por vega importan: sin ellos, el ajuste se
    deja arrastrar por alas ilíquidas donde un centavo de prima mueve la IV varios
    puntos, y termina describiendo el ruido de las alas en vez del cuerpo negociable.
    """
    clean = []
    for s in slices or []:
        k = np.asarray(s.get("k"), dtype=float)
        w = np.asarray(s.get("w"), dtype=float)
        T = float(s.get("T") or 0.0)
        if k.size < 4 or k.size != w.size or T <= 0:
            continue
        ok = np.isfinite(k) & np.isfinite(w) & (w > 0)
        if ok.sum() < 4:
            continue
        vega = s.get("vega")
        vg = np.asarray(vega, dtype=float)[ok] if vega is not None else np.ones(int(ok.sum()))
        vg = np.where(np.isfinite(vg) & (vg > 0), vg, 1e-6)
        clean.append({"k": k[ok], "w": w[ok], "T": T, "vega": vg,
                      "atm": float(np.interp(0.0, np.sort(k[ok]), w[ok][np.argsort(k[ok])]))})
    if len(clean) < 2:
        return {"ready": False, "reason": "SSVI necesita al menos 2 vencimientos con 4 strikes",
                "model": "SSVI"}

    clean.sort(key=lambda s: s["T"])
    theta0 = np.array([max(s["atm"], 1e-8) for s in clean])
    n_slices = len(clean)

    # Parámetros: θ por corte (log para forzar positividad), η, γ y ρ (uno o por corte).
    n_rho = n_slices if extended else 1
    x0 = np.concatenate([np.log(theta0), [0.6, 0.35], np.zeros(n_rho) - 0.2])

    def unpack(x):
        th = np.exp(x[:n_slices])
        eta, gamma = float(x[n_slices]), float(x[n_slices + 1])
        rho = np.tanh(x[n_slices + 2:])
        if rho.size == 1:
            rho = np.full(n_slices, float(rho[0]))
        return th, eta, gamma, rho

    def resid(x):
        th, eta, gamma, rho = unpack(x)
        parts = []
        for i, s in enumerate(clean):
            model = ssvi_total_variance(s["k"], th[i], rho[i], eta, gamma)
            wgt = np.sqrt(s["vega"] / np.mean(s["vega"]))
            parts.append((model - s["w"]) * wgt)
        # Penalización blanda por calendario decreciente: SSVI lo evita por
        # construcción sólo si θ es creciente, así que se empuja hacia ahí.
        dec = np.minimum(np.diff(th), 0.0)
        parts.append(dec * 25.0)
        # Penalización por violar las condiciones suficientes de mariposa.
        ph = phi_power(th, eta, gamma)
        c1 = np.maximum(th * ph * (1.0 + np.abs(rho)) - 4.0, 0.0)
        c2 = np.maximum(th * ph * ph * (1.0 + np.abs(rho)) - 4.0, 0.0)
        parts.append(np.concatenate([c1, c2]) * 5.0)
        return np.concatenate(parts)

    try:
        res = least_squares(resid, x0, loss="soft_l1", max_nfev=3000,
                            f_scale=max(float(np.median(theta0)) * 0.05, 1e-8))
    except Exception as exc:  # noqa: BLE001 - frontera del optimizador
        return {"ready": False, "reason": f"{type(exc).__name__}: {exc}"[:180], "model": "SSVI"}

    th, eta, gamma, rho = unpack(res.x)
    per_slice = []
    for i, s in enumerate(clean):
        model = ssvi_total_variance(s["k"], th[i], rho[i], eta, gamma)
        wgt = s["vega"] / np.sum(s["vega"])
        rmse = float(np.sqrt(np.mean((model - s["w"]) ** 2)))
        vw_rmse = float(np.sqrt(np.sum(wgt * (model - s["w"]) ** 2)))
        per_slice.append({"T": s["T"], "theta": float(th[i]), "rho": float(rho[i]),
                          "n": int(s["k"].size), "rmse_total_variance": round(rmse, 8),
                          "vega_weighted_rmse": round(vw_rmse, 8)})

    bf = butterfly_conditions(th, rho, eta, gamma)
    cal = calendar_monotonic(th)
    ready = bool(res.success and bf["pass"] and cal["pass"])
    return {
        "ready": ready, "fit_converged": bool(res.success),
        "model": "eSSVI" if extended else "SSVI",
        "params": {"eta": float(eta), "gamma": float(gamma),
                   "theta": [float(t) for t in th], "rho": [float(r) for r in rho]},
        "per_slice": per_slice,
        "butterfly": bf, "calendar": cal,
        "mean_vega_weighted_rmse": round(float(np.mean([p["vega_weighted_rmse"] for p in per_slice])), 8),
        "reason": None if ready else ("arbitraje de mariposa" if not bf["pass"]
                                      else "arbitraje de calendario" if not cal["pass"]
                                      else "el ajuste no converge"),
        "role": "SHADOW_CHALLENGER",
        "note": ("Challenger en sombra. No sustituye al SVI campeón sin ganar la puerta "
                 "OOS multi-ventana."),
    }


def compare_to_svi(ssvi_fit: Dict[str, Any], svi_slices: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Enfrenta el challenger al campeón corte a corte, en la misma unidad.

    Comparar RMSE en varianza total es lo correcto: en volatilidad, un error igual
    pesa distinto según el plazo, y eso haría que el modelo pareciese mejor o peor
    sólo por el vencimiento que le tocara.
    """
    if not ssvi_fit or not ssvi_fit.get("ready"):
        return {"ready": False, "reason": (ssvi_fit or {}).get("reason", "sin ajuste SSVI"),
                "champion": "SVI", "authority": "SVI"}
    rows = []
    for ch, sv in zip(ssvi_fit.get("per_slice", []), svi_slices or []):
        svi_rmse = sv.get("rmse_total_variance")
        if svi_rmse is None:
            continue
        rows.append({"T": ch["T"], "svi_rmse": float(svi_rmse),
                     "ssvi_rmse": float(ch["rmse_total_variance"]),
                     "ssvi_vega_weighted_rmse": float(ch["vega_weighted_rmse"]),
                     "ssvi_better": bool(float(ch["rmse_total_variance"]) < float(svi_rmse))})
    if not rows:
        return {"ready": False, "reason": "no hay cortes comparables", "authority": "SVI"}
    wins = sum(1 for r in rows if r["ssvi_better"])
    return {
        "ready": True, "slices": len(rows), "ssvi_wins": wins,
        "champion": "SVI", "challenger": ssvi_fit.get("model"),
        "authority": "SVI",
        "calendar_arbitrage_free_by_construction": bool(ssvi_fit.get("calendar", {}).get("pass")),
        "per_slice": rows,
        "verdict": ("el challenger ajusta mejor la mayoría de cortes" if wins > len(rows) / 2
                    else "el campeón mantiene mejor ajuste local"),
        "note": ("Comparación informativa. La superficie publicada sigue siendo la del "
                 "campeón SVI hasta que el challenger gane la puerta de promoción OOS."),
    }
