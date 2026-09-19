from __future__ import annotations

"""Regresiones de la auditoría cuantitativa v1.42.2.

Cada test de aquí bloquea un defecto CONFIRMADO NUMÉRICAMENTE contra una referencia
externa al motor (fórmula cerrada o identidad exacta), no contra el propio motor.
Un test que sólo compara el motor consigo mismo no habría detectado ninguno de los
tres, porque los tres eran internamente coherentes y aun así estaban mal.
"""

import itertools
import math

import numpy as np
import pytest
from scipy.stats import norm

from app.core.expiry_clock import year_fraction
from app.core.monte_carlo import monte_carlo_level_report
from app.core.precision_engine import black_scholes_price, implied_volatility_from_price
from app.core.engine import dynamic_gamma_flip_snapshot, EngineConfig


# ----------------------------------------------------------------- F1 · Monte Carlo

def _analytic_first_passage_pct(S: float, B: float, r: float, q: float,
                                iv_pct: float, dte_days: float) -> float:
    """P(tocar B antes de T) para un GBM, monitoreo continuo.

    Fórmula cerrada clásica de primer paso. Es la referencia EXTERNA: no usa nada
    del motor salvo el reloj ACT/365, así que un error en el simulador no puede
    esconderse detrás de ella.
    """
    T = year_fraction(dte_days)
    sig = iv_pct / 100.0
    mu = r - q - 0.5 * sig * sig
    b = math.log(B / S)
    s = sig * math.sqrt(T)
    if B >= S:
        return 100.0 * (norm.cdf((-b + mu * T) / s)
                        + math.exp(2 * mu * b / (sig * sig)) * norm.cdf((-b - mu * T) / s))
    return 100.0 * (norm.cdf((b - mu * T) / s)
                    + math.exp(2 * mu * b / (sig * sig)) * norm.cdf((b + mu * T) / s))


@pytest.mark.parametrize("spot,iv,dte,up,down", [
    (100.0, 20.0, 1.0, 101.0, 99.0),
    (600.0, 15.0, 0.5, 604.0, 596.0),
    (100.0, 45.0, 5.0, 110.0, 92.0),
    (50.0, 30.0, 21.0, 56.0, 45.0),
    (420.0, 12.0, 0.2, 421.5, 418.0),
])
def test_touch_probability_matches_continuous_first_passage(spot, iv, dte, up, down):
    """El defecto: con 1 paso/día un 0DTE tenía UN solo tramo, así que el máximo del
    camino era max(spot, terminal) y 'tocar' degeneraba en 'terminar más allá'.
    Medido antes de la corrección: 17.30% publicado frente a 34.40% real.

    La corrección de puente browniano debe reproducir la fórmula continua dentro
    del error de muestreo de Monte Carlo, no dentro de un factor 2.
    """
    r, q = 0.045, 0.012
    rep = monte_carlo_level_report(spot, r, q, iv, dte,
                                   {"call_wall": up, "put_wall": down},
                                   n_sims=200_000, seed=11)
    assert rep["ready"] is True
    assert rep["touch_method"] == "BROWNIAN_BRIDGE_CONTINUOUS"
    for key in ("call_wall", "put_wall"):
        got = rep["levels"][key]["prob_touch_by_expiry_pct"]
        exact = _analytic_first_passage_pct(spot, rep["levels"][key]["level"], r, q, iv, dte)
        assert got == pytest.approx(exact, abs=0.6), f"{key}: {got:.2f}% vs exacto {exact:.2f}%"


def test_touch_probability_is_not_merely_the_finish_probability():
    """La firma exacta del fallo antiguo: para 1 DTE, touch == finish clavado.
    Un nivel cercano debe ser tocado MUCHO más a menudo de lo que se termina más allá.
    """
    rep = monte_carlo_level_report(100.0, 0.045, 0.012, 20.0, 1.0,
                                   {"call_wall": 101.0}, n_sims=120_000, seed=3)
    lvl = rep["levels"]["call_wall"]
    assert lvl["prob_touch_by_expiry_pct"] > 1.6 * lvl["prob_finish_beyond_pct"]


def test_days_axis_is_in_real_days_not_step_indices():
    """Con DTE fraccionario el índice de paso no es un día: el eje del cono estaba
    mal etiquetado y el último punto no coincidía con el vencimiento."""
    rep = monte_carlo_level_report(100.0, 0.045, 0.012, 20.0, 2.5, {}, n_sims=500, seed=1)
    axis = rep["days_axis"]
    assert axis[0] == 0.0
    assert axis[-1] == pytest.approx(2.5, abs=1e-6)
    assert all(b > a for a, b in zip(axis, axis[1:]))


def test_touch_probability_never_below_finish_probability():
    rep = monte_carlo_level_report(100.0, 0.045, 0.012, 25.0, 10.0,
                                   {"a": 104.0, "b": 96.0, "c": 100.0}, n_sims=40_000, seed=4)
    for lvl in rep["levels"].values():
        assert lvl["prob_touch_by_expiry_pct"] >= lvl["prob_finish_beyond_pct"] - 1e-9


# ------------------------------------------------------------------- F2 · Gamma flip

def _chain(spot: float, only_calls: bool) -> "object":
    import pandas as pd
    rng = np.random.default_rng(1)
    types = ("call",) if only_calls else ("call", "put")
    rows = [{"strike": float(K), "iv": 0.18, "dte": 1.0,
             "open_interest": float(rng.integers(50, 5000)), "option_type": ot,
             "underlying_price": spot, "contract_multiplier": 100.0,
             "model_risk_free_rate": 0.045, "model_dividend_yield": 0.012}
            for K in np.arange(spot * 0.85, spot * 1.15, 1.0) for ot in types]
    return pd.DataFrame(rows)


def test_flip_is_a_true_root_of_net_gex():
    """La deduplicación de mallas es una optimización pura, así que el flip tiene que
    seguir siendo una RAÍZ de verdad de NetGEX(S)=0, no un punto cercano.

    Se comprueba la propiedad, no una constante mágica: un test que fija el número
    se rompe con cualquier cambio legítimo de malla y no dice nada sobre si el
    resultado es correcto.
    """
    from app.core.engine import _net_gex_at_hypothetical_spot
    cfg = EngineConfig(symbol="SPY")
    chain = _chain(600.0, only_calls=False)
    out = dynamic_gamma_flip_snapshot(chain, cfg)
    assert out["crossing"] is True
    assert out["method"] == "DYNAMIC_ROOT"
    assert out["grid_low"] <= out["flip"] <= out["grid_high"]
    gross = float(np.abs([_net_gex_at_hypothetical_spot(chain, 600.0 * m, cfg)
                          for m in (0.9, 1.0, 1.1)]).max())
    assert abs(_net_gex_at_hypothetical_spot(chain, out["flip"], cfg)) < gross * 1e-3


def test_flip_is_deterministic_across_repeated_calls():
    """Mismo snapshot, mismo flip, dígito a dígito. Un flip que baila entre refrescos
    hace que TRACE y Equity Hub muestren niveles distintos del mismo mercado."""
    cfg = EngineConfig(symbol="SPY")
    chain = _chain(600.0, only_calls=False)
    runs = {dynamic_gamma_flip_snapshot(chain, cfg)["flip"] for _ in range(5)}
    assert len(runs) == 1


def test_flip_without_crossing_is_not_reported_as_a_flip():
    """Sin raíz NetGEX(S)=0 el motor debe decirlo, no inventar un flip."""
    out = dynamic_gamma_flip_snapshot(_chain(600.0, only_calls=True), EngineConfig(symbol="SPY"))
    assert out["crossing"] is False
    assert out["method"] == "NEAREST_ZERO_NO_CROSS"


# ---------------------------------------------------------------------- F3 · IV ITM

@pytest.mark.parametrize("S,K,T,sig,ot", [
    (420.0, 300.0, 1.0, 0.08, "call"),
    (100.0, 65.0, 0.25, 0.15, "call"),
    (100.0, 155.0, 0.25, 0.15, "put"),
    (420.0, 273.0, 1.0, 0.15, "call"),
])
def test_deep_itm_iv_is_recovered_through_put_call_parity(S, K, T, sig, ot):
    """Antes devolvía NaN: en un contrato muy ITM el valor temporal se pierde dentro
    del intrínseco y brentq ni siquiera encuentra cambio de signo, así que el
    contrato desaparecía de la cadena. La paridad put-call es exacta, así que
    invertir el gemelo OTM recupera la MISMA sigma sin suponer nada.
    """
    r, q = 0.045, 0.012
    px = black_scholes_price(S, K, T, sig, ot, r, q)
    iv = implied_volatility_from_price(px, S, K, T, ot, r, q)
    assert math.isfinite(iv), "IV no recuperada en contrato deep-ITM"
    assert iv == pytest.approx(sig, abs=1e-4)


def test_every_contract_with_identifiable_vega_inverts():
    """Barrido: si hay vega medible, tiene que haber IV. Sin excepciones."""
    r, q = 0.045, 0.012
    missing = []
    for S, K, T, sig, ot in itertools.product(
            [100.0, 50.0, 420.0], [40.0, 80.0, 95.0, 100.0, 105.0, 130.0, 300.0],
            [1 / 365, 7 / 365, 0.25, 1.0], [0.08, 0.22, 0.75], ["call", "put"]):
        px = black_scholes_price(S, K, T, sig, ot, r, q)
        if px <= 0:
            continue
        d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
        if S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T) < 1e-8:
            continue  # vega nula: IV genuinamente no identificable, NaN es correcto
        if not math.isfinite(implied_volatility_from_price(px, S, K, T, ot, r, q)):
            missing.append((S, K, round(T, 4), sig, ot))
    assert not missing, f"IV perdida en {len(missing)} contratos invertibles: {missing[:5]}"


def test_unidentifiable_iv_still_returns_nan_not_a_fabricated_number():
    """La otra mitad de la regla: cuando NO se puede medir, hay que decirlo."""
    assert math.isnan(implied_volatility_from_price(0.0, 100.0, 100.0, 0.25, "call", 0.045, 0.012))
    assert math.isnan(implied_volatility_from_price(-1.0, 100.0, 100.0, 0.25, "call", 0.045, 0.012))
    # precio por encima de la cota superior de no-arbitraje
    assert math.isnan(implied_volatility_from_price(1e6, 100.0, 100.0, 0.25, "call", 0.045, 0.012))


def test_iv_is_never_fabricated_at_the_bracket_bound():
    """Defecto preexistente: `if f(lo)==0: return lo` publicaba IV = 0.5% siempre que
    el precio fuese indistinguible en coma flotante del precio a volatilidad cero.

    No es un detalle cosmético. Gamma va como 1/sigma, así que una sigma inventada
    de 0.005 infla la gamma del contrato y esa gamma falsa entra en GEX, y de ahí a
    Call Wall, Put Wall y Gamma Flip. Un número inventado contamina toda la
    estructura, y encima lo hace con la misma apariencia que uno medido.

    Regla: sin vega no hay IV, y eso se dice con NaN.
    """
    r, q = 0.045, 0.012
    for S, K, T, ot in [(100.0, 40.0, 0.25, "call"), (100.0, 300.0, 1.0, "put"),
                        (50.0, 100.0, 1.0, "put")]:
        px = black_scholes_price(S, K, T, 0.08, ot, r, q)
        if px <= 0:
            continue
        iv = implied_volatility_from_price(px, S, K, T, ot, r, q)
        assert not (math.isfinite(iv) and iv <= 0.0051), (
            f"IV fabricada en el extremo del bracket: {iv} para S={S} K={K} {ot}")


def test_recovered_iv_always_reprices_the_original_quote():
    """Cierre del círculo: si el motor publica una IV, esa IV tiene que devolver el
    precio del que salió. Vale tanto para la inversión directa como para la vía
    paridad put-call."""
    r, q = 0.045, 0.012
    checked = 0
    for S in (100.0, 420.0):
        for mny in (0.65, 0.85, 1.0, 1.15, 1.45):
            for T in (7 / 365, 0.25, 1.0):
                for sig in (0.15, 0.35):
                    for ot in ("call", "put"):
                        K = S * mny
                        px = black_scholes_price(S, K, T, sig, ot, r, q)
                        if px <= 0:
                            continue
                        iv = implied_volatility_from_price(px, S, K, T, ot, r, q)
                        if not math.isfinite(iv):
                            continue
                        checked += 1
                        back = black_scholes_price(S, K, T, iv, ot, r, q)
                        assert back == pytest.approx(px, abs=max(1e-6, px * 1e-6))
    assert checked > 100, "el barrido no cubrió suficientes contratos"
