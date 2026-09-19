"""Estado latente del dealer con incertidumbre explícita (v1.42).

POR QUÉ CAMBIA
--------------
v1.41 publicaba cifras como «Dealer hedge = +$420M» apoyadas en un
`counterparty_share ≈ 70 %` fijo. El número es defendible como escenario. Como
VERDAD no lo es, y presentado sin banda invita a operarlo como si lo fuera.

Nada de lo que hay debajo se observa:

    ¿el cliente abrió o cerró posición?          no se observa
    ¿el dealer era la contraparte?               no se observa
    ¿quién inició el print?                      se infiere (flow_probability)

Tres variables latentes multiplicándose no producen un punto. Producen una
distribución. Esta versión la calcula y la publica:

    Presión de cobertura del dealer
        P10      +$180M
        Mediana  +$410M
        P90      +$710M
        Confianza direccional  78 %
        Calidad del modelo     BUENA

La mediana puede coincidir con el número anterior. La diferencia es que ahora se
ve cuánto de eso es conocimiento y cuánto es suposición.

DETERMINISMO
------------
El muestreo usa semilla fija derivada del contenido. Dos ejecuciones sobre la misma
cinta dan la misma banda: un replay que no reproduce sus propias cifras no sirve
para investigar nada.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Dict

import numpy as np
import pandas as pd

from .contract_spec import multiplier_series
from .flow_probability import classify_frame
from .safe_stats import correlation as safe_correlation

# Prior sobre la fracción de prints en los que el dealer es la contraparte.
# Beta(7, 3) tiene media 0.70 —el valor que v1.41 fijaba— y desviación ~0.14, que
# es aproximadamente la incertidumbre honesta sobre un parámetro que nadie publica.
CP_ALPHA, CP_BETA = 7.0, 3.0

DEFAULT_SIMULATIONS = 2000

QUALITY_GOOD = "GOOD"
QUALITY_FAIR = "FAIR"
QUALITY_POOR = "POOR"


def _f(v: Any, default: float = float("nan")) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _seed_from(events: pd.DataFrame) -> int:
    """Semilla derivada del contenido: mismo input, misma banda, siempre."""
    try:
        key = f"{len(events)}|{events.index.min()}|{events.index.max()}"
        cols = [c for c in ("timestamp", "contract_symbol", "trade_price", "contracts")
                if c in events.columns]
        if cols:
            key += "|" + str(pd.util.hash_pandas_object(events[cols], index=False).sum())
    except Exception:
        key = str(len(events) if events is not None else 0)
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


def opening_probability(events: pd.DataFrame) -> pd.Series:
    """P(la operación ABRE posición | features observables).

    Señales, todas observables y todas imperfectas:
      tamaño/OI alto      → difícil que cierre una posición que no cabe en el OI
      volumen/OI alto     → contrato con actividad nueva concentrada
      OI bajo             → poca posición previa que cerrar
      DTE muy corto       → sube la proporción de cierres y de ejercicio/expiración

    Se acota a [0.15, 0.90]: ni una operación es con certeza apertura ni con certeza
    cierre a partir de la cinta sola. Fijarlo en 0 o 1 sería afirmar lo que no se ve.
    """
    n = len(events)
    idx = getattr(events, "index", None)
    if n == 0:
        return pd.Series(dtype=float, index=idx)

    def col(*names, default=0.0):
        for nm in names:
            if nm in events.columns:
                return pd.to_numeric(events[nm], errors="coerce").fillna(default)
        return pd.Series(float(default), index=idx, dtype=float)

    oi = col("open_interest").clip(lower=0.0)
    vol = col("daily_volume", "volume").clip(lower=0.0)
    size = col("contracts", "size").clip(lower=0.0)
    dte = col("dte", default=5.0).clip(lower=0.0)

    size_oi = size / oi.clip(lower=1.0)
    vol_oi = vol / oi.clip(lower=1.0)
    z = (-0.30
         + 1.80 * np.tanh(size_oi * 25.0)
         + 0.90 * np.tanh(vol_oi * 1.5)
         + 0.60 * np.exp(-oi / 750.0)
         - 0.70 * np.exp(-dte / 1.5))
    p = 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))
    return pd.Series(np.clip(p, 0.15, 0.90), index=idx, dtype=float)


@dataclass(frozen=True)
class DealerState:
    p10: float
    p50: float
    p90: float
    direction_confidence: float
    quality: str
    simulations: int
    events: int
    classified_pct: float
    mean_opening_probability: float
    unit: str = "USD"
    note: str = ""

    def describe(self) -> Dict[str, Any]:
        return {"p10": round(self.p10, 2), "median": round(self.p50, 2), "p90": round(self.p90, 2),
                "direction": "COMPRA" if self.p50 > 0 else ("VENTA" if self.p50 < 0 else "NEUTRO"),
                "direction_confidence": round(self.direction_confidence * 100.0, 1),
                "model_quality": self.quality, "simulations": self.simulations,
                "events": self.events, "classified_pct": round(self.classified_pct, 1),
                "mean_opening_probability": round(self.mean_opening_probability * 100.0, 1),
                "unit": self.unit, "kind": "INFERRED", "note": self.note}


def hedge_pressure_distribution(events: pd.DataFrame, *, symbol: Any = None,
                                simulations: int = DEFAULT_SIMULATIONS,
                                cp_alpha: float = CP_ALPHA,
                                cp_beta: float = CP_BETA) -> Dict[str, Any]:
    """Distribución de la presión de cobertura del dealer, en dólares de subyacente.

    Signo: positivo = necesidad estimada de COMPRAR subyacente.
    Cadena de signos, explícita porque es donde más fácil se cuela un error:
      el cliente compra una call (lado +1) → el dealer queda corto de esa call →
      corto de delta → para neutralizar debe COMPRAR subyacente.
    """
    if events is None or len(events) == 0:
        return {"ready": False, "reason": "sin eventos de flujo"}

    e = events.copy()
    probs = classify_frame(e)
    p_buy = probs["p_buy"].to_numpy(float)
    p_sell = probs["p_sell"].to_numpy(float)
    p_unk = probs["p_unknown"].to_numpy(float)

    def col(*names, default=0.0):
        for nm in names:
            if nm in e.columns:
                return pd.to_numeric(e[nm], errors="coerce").fillna(default).to_numpy(float)
        return np.full(len(e), float(default))

    size = np.clip(col("contracts", "size"), 0.0, None)
    delta = col("model_delta", "calc_delta", "provider_delta", "delta")
    gamma = col("model_gamma", "calc_gamma", "provider_gamma", "gamma")
    spot = np.clip(col("underlying_price"), 0.0, None)
    mult = multiplier_series(e, symbol).to_numpy(float)
    p_open = opening_probability(e).to_numpy(float)

    valid = (size > 0) & np.isfinite(delta) & (spot > 0) & np.isfinite(mult)
    if not valid.any():
        return {"ready": False, "reason": "ningún evento tiene tamaño, delta y spot utilizables"}

    size, delta, gamma, spot, mult = size[valid], delta[valid], gamma[valid], spot[valid], mult[valid]
    p_buy, p_sell, p_unk, p_open = p_buy[valid], p_sell[valid], p_unk[valid], p_open[valid]
    n = int(valid.sum())

    rng = np.random.default_rng(_seed_from(e))
    sims = max(int(simulations), 200)

    # Lado del cliente: +1 compra, -1 venta, 0 sin clasificar. Se muestrea con la
    # distribución del clasificador en vez de con su etiqueta dura.
    u = rng.random((sims, n))
    side = np.where(u < p_buy[None, :], 1.0,
                    np.where(u < (p_buy + p_sell)[None, :], -1.0, 0.0))
    # Apertura: Bernoulli por evento y simulación.
    opens = (rng.random((sims, n)) < p_open[None, :]).astype(float)
    # Contraparte dealer: parámetro SISTÉMICO, un solo valor por simulación. Muestrear
    # uno por evento promediaría la incertidumbre hasta hacerla desaparecer, que es
    # exactamente el error que esta versión corrige.
    cp = rng.beta(cp_alpha, cp_beta, size=(sims, 1))

    hedge_shares = side * size[None, :] * opens * cp * delta[None, :] * mult[None, :]
    hedge_notional = (hedge_shares * spot[None, :]).sum(axis=1)

    p10, p50, p90 = (float(x) for x in np.percentile(hedge_notional, [10.0, 50.0, 90.0]))
    if p50 >= 0:
        direction_conf = float((hedge_notional > 0).mean())
    else:
        direction_conf = float((hedge_notional < 0).mean())

    classified = float(np.mean(1.0 - p_unk)) * 100.0
    # Calidad: cuánta cinta está clasificada, cuántos eventos hay y cuán ancha sale
    # la banda respecto a su propio centro.
    width_ratio = (p90 - p10) / max(abs(p50), 1e-9)
    if classified >= 65.0 and n >= 40 and width_ratio <= 2.5:
        quality = QUALITY_GOOD
    elif classified >= 40.0 and n >= 15:
        quality = QUALITY_FAIR
    else:
        quality = QUALITY_POOR

    gex_shares = side * size[None, :] * opens * cp * gamma[None, :] * mult[None, :]
    dealer_gex = (-gex_shares * (spot[None, :] ** 2) * 0.01).sum(axis=1)
    g10, g50, g90 = (float(x) for x in np.percentile(dealer_gex, [10.0, 50.0, 90.0]))

    state = DealerState(p10, p50, p90, direction_conf, quality, sims, n, classified,
                        float(np.mean(p_open)),
                        note=("Inferencia, no inventario observado. La banda refleje tres "
                              "incógnitas reales: quién inició, si abrió o cerró, y si el "
                              "dealer era la contraparte."))
    out = state.describe()
    out.update({
        "ready": True,
        "hedge_pressure": {"p10": round(p10, 2), "median": round(p50, 2), "p90": round(p90, 2),
                           "unit": "USD nocionales de subyacente",
                           "sign": "positivo = necesidad estimada de COMPRAR subyacente"},
        "dealer_gex": {"p10": round(g10, 2), "median": round(g50, 2), "p90": round(g90, 2),
                       "unit": "GEX_PER_1PCT"},
        "counterparty_prior": {"distribution": f"Beta({cp_alpha:g}, {cp_beta:g})",
                               "mean_pct": round(100.0 * cp_alpha / (cp_alpha + cp_beta), 1),
                               "note": "La participación del dealer como contraparte no se "
                                       "observa; se declara como prior con dispersión."},
        "method": "LATENT_STATE_MONTE_CARLO",
        "deterministic": True,
    })
    return out


def fit_opening_from_oi(trades: pd.DataFrame, next_session_oi: pd.DataFrame) -> Dict[str, Any]:
    """Aprende P(apertura | features) contrastando con el cambio de OI del día siguiente.

    La idea, y su límite: en un contrato con muchas operaciones no se puede saber cuál
    abrió. Pero AGREGADO por contrato y sesión, el cambio de OI sí acota cuántos de
    esos contratos fueron aperturas netas. Esa etiqueta parcial basta para recalibrar
    la pendiente del modelo, no para etiquetar operaciones individuales, y así se
    declara.
    """
    if trades is None or len(trades) == 0 or next_session_oi is None or len(next_session_oi) == 0:
        return {"ready": False, "reason": "faltan operaciones o el OI de la sesión siguiente"}
    need = {"contract_symbol", "contracts"}
    if not need.issubset(set(trades.columns)):
        return {"ready": False, "reason": f"la cinta necesita {sorted(need)}"}
    if not {"contract_symbol", "open_interest_change"}.issubset(set(next_session_oi.columns)):
        return {"ready": False, "reason": "el OI necesita contract_symbol y open_interest_change"}

    vol = (trades.groupby("contract_symbol")["contracts"].sum().rename("volume").reset_index())
    merged = vol.merge(next_session_oi[["contract_symbol", "open_interest_change"]],
                       on="contract_symbol", how="inner")
    merged = merged[merged["volume"] > 0]
    if len(merged) < 25:
        return {"ready": False, "reason": "muestra insuficiente (< 25 contratos con OI posterior)",
                "contracts": int(len(merged))}

    # Fracción de apertura neta implícita por contrato, acotada a [0, 1]: |ΔOI| no
    # puede exceder el volumen y su exceso sólo indica ruido de reporte.
    implied = (merged["open_interest_change"].abs() / merged["volume"]).clip(0.0, 1.0)
    feats = opening_probability(
        trades.groupby("contract_symbol").agg(
            open_interest=("open_interest", "last") if "open_interest" in trades.columns else ("contracts", "size"),
            volume=("contracts", "sum"), contracts=("contracts", "mean"),
            dte=("dte", "last") if "dte" in trades.columns else ("contracts", "size"),
        ).reset_index().set_index("contract_symbol")
    )
    aligned = feats.reindex(merged["contract_symbol"]).to_numpy(float)
    ok = np.isfinite(aligned) & np.isfinite(implied.to_numpy(float))
    if ok.sum() < 25:
        return {"ready": False, "reason": "no hay suficientes contratos alineables"}

    x, y = aligned[ok], implied.to_numpy(float)[ok]
    _corr = safe_correlation(x, y)
    bias = float(np.mean(y - x))
    return {
        "ready": True, "contracts": int(ok.sum()),
        "model_mean": round(float(np.mean(x)), 4),
        "oi_implied_mean": round(float(np.mean(y)), 4),
        "bias": round(bias, 4),
        "correlation": _corr.or_none(),
        "correlation_state": _corr.state,
        "suggested_intercept_shift": round(float(np.clip(bias * 2.0, -1.0, 1.0)), 4),
        "label_quality": "PARTIAL_AGGREGATE",
        "note": ("Etiqueta parcial y agregada por contrato: acota cuántos contratos "
                 "fueron apertura neta, no cuál operación concreta lo fue. Sirve para "
                 "recalibrar la pendiente, no para etiquetar prints."),
    }
