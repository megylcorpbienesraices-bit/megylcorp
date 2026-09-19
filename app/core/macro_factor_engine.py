"""Motor de factores macro universal con betas por activo (v1.42).

QUÉ SUSTITUYE
-------------
`macro_dia` lee series de FRED y construye un índice de estrés. Funciona, y su
lectura de tipos, curva y crédito sigue siendo válida. Lo que no escala es la
arquitectura: un solo canal de estrés, pensado para un ecosistema, aplicado igual
a todos los activos. XLF y XLK no reaccionan igual a un movimiento del 2 años.
GLD y NVDA no comparten sensibilidad al dólar. XLE vive del petróleo y SPY no.

La alternativa ingenua sería escribir reglas por activo. Para 2.000 símbolos eso
no se mantiene, y además es exactamente el `if symbol ==` que v1.42 elimina.

LA ALTERNATIVA CORRECTA
-----------------------
Un modelo de factores. Cada activo APRENDE su exposición:

    r_activo = α + β₁·F₁ + β₂·F₂ + … + ε

con factores observables: tipos, curva, dólar, crédito, petróleo, volatilidad,
condiciones financieras, crecimiento, inflación y el factor sectorial. No hace
falta escribir nada por símbolo: la regresión lo descubre.

HONESTIDAD ESTADÍSTICA
----------------------
Una beta estimada con 30 observaciones no es una beta, es ruido con decimales. El
módulo exige un mínimo de muestra, publica el R² y el error estándar de cada beta,
y marca como NO SIGNIFICATIVA toda beta cuyo |t| < 2. Una exposición no significativa
se publica como tal en vez de alimentar una narrativa macro inventada.

La regresión usa ridge suave porque los factores macro están correlacionados entre
sí (tipos y dólar, crédito y volatilidad): con OLS puro, dos factores colineales se
reparten un coeficiente enorme y otro enorme de signo contrario, y ninguno de los
dos significa nada.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd

# Definición de factores. `series` son ids de FRED; `proxy` un ticker cuando el
# factor se observa mejor en mercado que en una serie oficial con retardo.
FACTORS: Dict[str, Dict[str, Any]] = {
    "RATES_LEVEL":   {"label": "Nivel de tipos", "series": "DGS2", "kind": "rate",
                      "transform": "diff", "note": "Cambio del 2 años: el tramo que descuenta a la Fed."},
    "RATES_CURVE":   {"label": "Pendiente de la curva", "series": "T10Y2Y", "kind": "rate",
                      "transform": "diff", "note": "10y−2y: empinamiento frente a aplanamiento."},
    "CREDIT":        {"label": "Crédito", "series": "BAMLH0A0HYM2", "kind": "credit",
                      "transform": "diff", "note": "OAS high yield: el precio del riesgo corporativo."},
    "DOLLAR":        {"label": "Dólar", "series": "DTWEXBGS", "kind": "fx",
                      "transform": "logret", "note": "Índice amplio del dólar."},
    "OIL":           {"label": "Petróleo", "series": "DCOILWTICO", "kind": "commodity",
                      "transform": "logret"},
    "VOLATILITY":    {"label": "Volatilidad", "series": "VIXCLS", "kind": "vol",
                      "transform": "diff"},
    "FIN_CONDITIONS": {"label": "Condiciones financieras", "series": "NFCI", "kind": "macro",
                       "transform": "diff", "note": "Índice de Chicago; sube = más restrictivas."},
    "GROWTH":        {"label": "Crecimiento", "series": "T10YIE", "kind": "macro",
                      "transform": "diff", "proxy": "SPY",
                      "note": "Sin una serie diaria de crecimiento, se usa el mercado amplio "
                              "como aproximación y se declara como tal."},
    "INFLATION":     {"label": "Inflación esperada", "series": "T5YIFR", "kind": "macro",
                      "transform": "diff"},
    "SECTOR":        {"label": "Factor sectorial", "series": None, "kind": "sector",
                      "transform": "logret",
                      "note": "Retorno del ETF sectorial del activo, ortogonalizado frente al mercado."},
}

MIN_OBSERVATIONS = 120          # ~6 meses de sesiones: por debajo, una beta es ruido
SIGNIFICANCE_T = 2.0
RIDGE_LAMBDA = 1e-3

# Mapa sector → ETF representativo. Es metadatos de mercado, no una regla de señal:
# no decide dirección, sólo qué serie sirve de factor sectorial.
SECTOR_ETF = {
    "TECHNOLOGY": "XLK", "INFORMATION TECHNOLOGY": "XLK",
    "FINANCIAL SERVICES": "XLF", "FINANCIALS": "XLF",
    "HEALTHCARE": "XLV", "HEALTH CARE": "XLV",
    "ENERGY": "XLE", "INDUSTRIALS": "XLI",
    "CONSUMER CYCLICAL": "XLY", "CONSUMER DISCRETIONARY": "XLY",
    "CONSUMER DEFENSIVE": "XLP", "CONSUMER STAPLES": "XLP",
    "UTILITIES": "XLU", "REAL ESTATE": "XLRE",
    "BASIC MATERIALS": "XLB", "MATERIALS": "XLB",
    "COMMUNICATION SERVICES": "XLC",
}


def sector_factor_symbol(sector: Any) -> Optional[str]:
    return SECTOR_ETF.get(str(sector or "").strip().upper())


def _transform(series: pd.Series, how: str) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    if how == "logret":
        s = np.log(s.where(s > 0)).diff()
    elif how == "diff":
        s = s.diff()
    return s.replace([np.inf, -np.inf], np.nan)


def build_factor_frame(series_map: Mapping[str, pd.Series]) -> pd.DataFrame:
    """Construye la matriz de factores a partir de series crudas indexadas por fecha."""
    cols: Dict[str, pd.Series] = {}
    for name, spec in FACTORS.items():
        raw = series_map.get(name)
        if raw is None:
            continue
        cols[name] = _transform(pd.Series(raw), str(spec.get("transform") or "diff"))
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index()


@dataclass(frozen=True)
class FactorBeta:
    factor: str
    beta: float
    std_error: float
    t_stat: float
    significant: bool

    def describe(self) -> Dict[str, Any]:
        spec = FACTORS.get(self.factor, {})
        return {"factor": self.factor, "label": spec.get("label", self.factor),
                "beta": round(self.beta, 6), "std_error": round(self.std_error, 6),
                "t_stat": round(self.t_stat, 3), "significant": self.significant,
                "interpretation": (
                    "no significativa: los datos no distinguen esta exposición de cero"
                    if not self.significant else
                    f"cada unidad de {spec.get('label', self.factor)} mueve el activo "
                    f"{self.beta:+.4f} en retorno")}


def fit_asset(returns: pd.Series, factors: pd.DataFrame, *,
              min_observations: int = MIN_OBSERVATIONS,
              ridge: float = RIDGE_LAMBDA) -> Dict[str, Any]:
    """Estima las betas de un activo frente a la matriz de factores.

    Ridge suave, no OLS puro: los factores macro son colineales entre sí y OLS
    responde a esa colinealidad con coeficientes enormes y opuestos que parecen
    exposiciones fuertes y no lo son.
    """
    y_raw = pd.to_numeric(pd.Series(returns), errors="coerce")
    if factors is None or factors.empty:
        return {"ready": False, "reason": "sin matriz de factores"}
    joined = pd.concat([y_raw.rename("__y__"), factors], axis=1).dropna()
    n = len(joined)
    if n < int(min_observations):
        return {"ready": False, "reason": f"muestra insuficiente: {n} < {min_observations} "
                                          "observaciones alineadas. Una beta con menos datos "
                                          "es ruido con decimales.",
                "observations": n}

    y = joined["__y__"].to_numpy(float)
    names = [c for c in joined.columns if c != "__y__"]
    Xr = joined[names].to_numpy(float)

    # Estandarizar para que la penalización ridge trate igual a factores con
    # escalas muy distintas (puntos básicos frente a retornos logarítmicos).
    mu, sd = Xr.mean(axis=0), Xr.std(axis=0, ddof=1)
    sd = np.where(sd > 1e-12, sd, 1.0)
    Xs = (Xr - mu) / sd
    X = np.column_stack([np.ones(n), Xs])

    P = np.eye(X.shape[1]) * float(ridge) * n
    P[0, 0] = 0.0   # el intercepto no se penaliza
    XtX = X.T @ X + P
    try:
        beta_s = np.linalg.solve(XtX, X.T @ y)
    except np.linalg.LinAlgError:
        return {"ready": False, "reason": "el sistema de factores es singular"}

    resid = y - X @ beta_s
    dof = max(n - X.shape[1], 1)
    sigma2 = float(resid @ resid) / dof
    try:
        cov = sigma2 * np.linalg.inv(XtX)
    except np.linalg.LinAlgError:
        cov = np.full((X.shape[1], X.shape[1]), np.nan)
    se_s = np.sqrt(np.clip(np.diag(cov), 0.0, None))

    # Devolver a la escala original: beta_original = beta_estandarizada / sd.
    betas = []
    for i, nm in enumerate(names, start=1):
        b = float(beta_s[i] / sd[i - 1])
        se = float(se_s[i] / sd[i - 1]) if math.isfinite(se_s[i]) else float("nan")
        t = float(b / se) if se and math.isfinite(se) and se > 0 else float("nan")
        betas.append(FactorBeta(nm, b, se, t, bool(math.isfinite(t) and abs(t) >= SIGNIFICANCE_T)))

    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float(resid @ resid) / ss_tot if ss_tot > 0 else float("nan")
    adj_r2 = (1.0 - (1.0 - r2) * (n - 1) / dof) if math.isfinite(r2) else float("nan")

    return {
        "ready": True, "observations": n,
        "alpha": float(beta_s[0]),
        "betas": [b.describe() for b in betas],
        "significant_factors": [b.factor for b in betas if b.significant],
        "r_squared": None if not math.isfinite(r2) else round(r2, 4),
        "adj_r_squared": None if not math.isfinite(adj_r2) else round(adj_r2, 4),
        "residual_vol": round(float(np.sqrt(sigma2)), 6),
        "method": "RIDGE_MULTIFACTOR",
        "note": ("Exposición estimada, no causal. Una beta no significativa (|t| < 2) se "
                 "publica como tal en vez de alimentar una narrativa macro."),
    }


def explain_move(fit: Mapping[str, Any], factor_move: Mapping[str, float]) -> Dict[str, Any]:
    """Descompone un movimiento esperado según las betas SIGNIFICATIVAS.

    Las no significativas se excluyen del cálculo a propósito: sumarlas produciría
    una atribución completa y falsamente precisa a partir de coeficientes que los
    propios datos no distinguen de cero.
    """
    if not fit or not fit.get("ready"):
        return {"ready": False, "reason": (fit or {}).get("reason", "sin ajuste")}
    parts = []
    total = 0.0
    excluded = []
    for b in fit.get("betas", []):
        move = factor_move.get(b["factor"])
        if move is None:
            continue
        if not b.get("significant"):
            excluded.append(b["factor"])
            continue
        contrib = float(b["beta"]) * float(move)
        total += contrib
        parts.append({"factor": b["factor"], "label": b.get("label"),
                      "factor_move": float(move), "beta": b["beta"],
                      "contribution": round(contrib, 6)})
    parts.sort(key=lambda p: abs(p["contribution"]), reverse=True)
    return {"ready": bool(parts), "expected_return": round(total, 6),
            "expected_return_pct": round(total * 100.0, 4),
            "contributions": parts, "excluded_not_significant": excluded,
            "r_squared": fit.get("r_squared"),
            "note": ("Sólo se suman factores con exposición estadísticamente distinguible "
                     "de cero. El resto se declara excluido en vez de aparentar precisión.")}


def compare_assets(fits: Mapping[str, Mapping[str, Any]], factor: str) -> Dict[str, Any]:
    """Ranking de sensibilidad a un factor entre activos ya ajustados.

    Esto es lo que sustituye a escribir reglas por símbolo: la diferencia entre
    XLF y XLK frente a los tipos sale de sus datos, no de una tabla mantenida a mano.
    """
    rows = []
    for symbol, fit in (fits or {}).items():
        if not fit or not fit.get("ready"):
            continue
        for b in fit.get("betas", []):
            if b["factor"] == factor:
                rows.append({"symbol": symbol, "beta": b["beta"], "t_stat": b["t_stat"],
                             "significant": b["significant"], "r_squared": fit.get("r_squared")})
    rows.sort(key=lambda r: (not r["significant"], -abs(r["beta"])))
    return {"ready": bool(rows), "factor": factor,
            "label": FACTORS.get(factor, {}).get("label", factor), "assets": rows}


def factor_contract() -> Dict[str, Any]:
    """Descripción publicable del modelo de factores."""
    return {
        "factors": {k: {kk: vv for kk, vv in v.items() if kk != "proxy"} | (
            {"proxy": v["proxy"]} if v.get("proxy") else {}) for k, v in FACTORS.items()},
        "model": "r_asset = alpha + sum(beta_i * F_i) + epsilon",
        "estimator": "RIDGE_MULTIFACTOR (lambda={:g}, factores estandarizados)".format(RIDGE_LAMBDA),
        "min_observations": MIN_OBSERVATIONS,
        "significance_threshold_t": SIGNIFICANCE_T,
        "sector_map": dict(SECTOR_ETF),
        "doctrine": ("Cada activo aprende su exposición. No hay reglas macro escritas por "
                     "símbolo, y una beta no significativa se publica como no significativa."),
    }
