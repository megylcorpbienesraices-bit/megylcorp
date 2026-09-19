"""Tests golden numericos del motor (v1.27.6).

La suite heredada verifica sobre todo *forma*: que los ficheros existan, que el
AST contenga ciertas llamadas, que un endpoint responda. Esto verifica *la
matematica*, que es lo unico que cuesta dinero cuando falla.

Las constantes de referencia NO salen del motor. Se calcularon con una
implementacion independiente de Black-Scholes-Merton basada en ``math.erf``,
incluida aqui como ``_oracle`` para que cualquiera pueda reproducirlas.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from app.core.engine import (
    EngineConfig,
    aggregate_strikes,
    black_scholes_greeks,
    enrich_options,
    gamma_center,
    gamma_flip,
    validate_input,
)
from app.core.precision_engine import (
    black_scholes_greeks_full,
    black_scholes_price,
    implied_volatility_from_price,
)

TOL = 1e-9

# --------------------------------------------------------------- oraculo


def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _oracle(S: float, K: float, T: float, sig: float, r: float, q: float, kind: str) -> dict:
    """Black-Scholes-Merton independiente del motor."""
    root = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / root
    d2 = d1 - root
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    gamma = dq * _n(d1) / (S * sig * math.sqrt(T))
    base = (2.0 * (r - q) * T - d2 * root) / (2.0 * T * root)
    if kind == "c":
        return {"price": S * dq * _N(d1) - K * dr * _N(d2), "delta": dq * _N(d1), "gamma": gamma,
                "vanna": -dq * _n(d1) * d2 / sig, "charm": q * dq * _N(d1) - dq * _n(d1) * base,
                "speed": -gamma / S * (d1 / root + 1.0)}
    return {"price": K * dr * _N(-d2) - S * dq * _N(-d1), "delta": dq * (_N(d1) - 1.0), "gamma": gamma,
            "vanna": -dq * _n(d1) * d2 / sig, "charm": -q * dq * _N(-d1) - dq * _n(d1) * base,
            "speed": -gamma / S * (d1 / root + 1.0)}


BASE = dict(S=100.0, K=100.0, T=0.25, sig=0.20, r=0.05, q=0.02)

# Valores congelados. Si el motor cambia, estos numeros deben cambiar a proposito.
GOLDEN_CALL = {"price": 4.33588561636158, "delta": 0.546996393995195, "gamma": 0.039386343824479,
               "vanna": -0.049232929780599, "charm": -0.087525931681294, "speed": -0.000886192736051}
GOLDEN_PUT = {"price": 3.592417746481487, "delta": -0.448016085197487, "gamma": 0.039386343824479,
              "vanna": -0.049232929780599, "charm": -0.107426181265148, "speed": -0.000886192736051}


# ------------------------------------------------------- greeks base


@pytest.mark.parametrize("kind,esperado", [("c", GOLDEN_CALL), ("p", GOLDEN_PUT)])
def test_greeks_completos_coinciden_con_valores_congelados(kind, esperado):
    got = black_scholes_greeks_full(BASE["S"], BASE["K"], BASE["T"], BASE["sig"],
                                    kind, BASE["r"], BASE["q"])
    for campo in ("delta", "gamma", "vanna", "charm", "speed"):
        assert got[campo] == pytest.approx(esperado[campo], abs=1e-12), campo


@pytest.mark.parametrize("kind", ["c", "p"])
def test_precio_bs_coincide_con_valor_congelado(kind):
    esperado = GOLDEN_CALL if kind == "c" else GOLDEN_PUT
    got = black_scholes_price(BASE["S"], BASE["K"], BASE["T"], BASE["sig"], kind, BASE["r"], BASE["q"])
    assert got == pytest.approx(esperado["price"], abs=1e-12)


def test_los_valores_congelados_los_reproduce_un_oraculo_independiente():
    """Si este test falla, las constantes de arriba estan mal, no el motor."""
    for kind, esperado in (("c", GOLDEN_CALL), ("p", GOLDEN_PUT)):
        ref = _oracle(BASE["S"], BASE["K"], BASE["T"], BASE["sig"], BASE["r"], BASE["q"], kind)
        for campo, valor in esperado.items():
            assert ref[campo] == pytest.approx(valor, abs=1e-12), (kind, campo)


def test_delta_y_gamma_de_dos_caminos_del_motor_concuerdan():
    """engine.black_scholes_greeks y precision_engine no pueden divergir."""
    delta, gamma = black_scholes_greeks(BASE["S"], BASE["K"], BASE["T"], BASE["sig"], "call",
                                        BASE["r"], BASE["q"])
    full = black_scholes_greeks_full(BASE["S"], BASE["K"], BASE["T"], BASE["sig"], "call",
                                     BASE["r"], BASE["q"])
    assert delta == pytest.approx(full["delta"], abs=1e-12)
    assert gamma == pytest.approx(full["gamma"], abs=1e-12)


# --------------------------------------------------- invariantes teoricos


def test_paridad_put_call_se_cumple():
    c = black_scholes_price(BASE["S"], BASE["K"], BASE["T"], BASE["sig"], "c", BASE["r"], BASE["q"])
    p = black_scholes_price(BASE["S"], BASE["K"], BASE["T"], BASE["sig"], "p", BASE["r"], BASE["q"])
    teorico = BASE["S"] * math.exp(-BASE["q"] * BASE["T"]) - BASE["K"] * math.exp(-BASE["r"] * BASE["T"])
    assert (c - p) == pytest.approx(teorico, abs=TOL)


def test_gamma_es_identica_para_call_y_put_mismo_strike():
    call = black_scholes_greeks_full(425.5, 440.0, 7 / 365, 0.185, "c", 0.045, 0.013)
    put = black_scholes_greeks_full(425.5, 440.0, 7 / 365, 0.185, "p", 0.045, 0.013)
    assert call["gamma"] == pytest.approx(put["gamma"], abs=1e-15)


def test_delta_call_menos_delta_put_es_el_factor_de_dividendo():
    T, q = 7 / 365, 0.013
    call = black_scholes_greeks_full(425.5, 440.0, T, 0.185, "c", 0.045, q)
    put = black_scholes_greeks_full(425.5, 440.0, T, 0.185, "p", 0.045, q)
    assert (call["delta"] - put["delta"]) == pytest.approx(math.exp(-q * T), abs=TOL)


def test_gamma_decrece_al_alejarse_del_strike():
    g = [black_scholes_greeks_full(100.0, k, 0.25, 0.2, "c", 0.05, 0.02)["gamma"]
         for k in (100.0, 110.0, 130.0)]
    assert g[0] > g[1] > g[2] > 0.0


def test_iv_invertida_reproduce_la_sigma_original():
    for sigma in (0.08, 0.185, 0.42, 1.10):
        precio = black_scholes_price(425.5, 440.0, 21 / 365, sigma, "c", 0.045, 0.013)
        recuperada = implied_volatility_from_price(precio, 425.5, 440.0, 21 / 365, "c", 0.045, 0.013)
        assert recuperada == pytest.approx(sigma, abs=1e-6), sigma


def test_iv_devuelve_nan_si_el_precio_viola_no_arbitraje():
    # Precio por encima del techo teorico de una call.
    assert math.isnan(implied_volatility_from_price(1e6, 425.5, 440.0, 21 / 365, "c", 0.045, 0.013))
    # Precio negativo.
    assert math.isnan(implied_volatility_from_price(-1.0, 425.5, 440.0, 21 / 365, "c", 0.045, 0.013))


# --------------------------------------------- agregacion GEX / DEX


def _chain() -> pd.DataFrame:
    ts = pd.Timestamp("2026-09-11 15:30:00")
    filas = []
    for strike, oi_c, oi_p in ((420.0, 1200, 300), (425.0, 800, 900), (430.0, 250, 2100)):
        filas.append(dict(timestamp=ts, underlying_price=425.5, strike=strike, dte=7.0,
                          option_type="call", open_interest=oi_c, volume=10, iv=0.185))
        filas.append(dict(timestamp=ts, underlying_price=425.5, strike=strike, dte=7.0,
                          option_type="put", open_interest=oi_p, volume=10, iv=0.185))
    return pd.DataFrame(filas)


def test_gex_por_contrato_coincide_con_la_formula_declarada():
    cfg = EngineConfig(risk_free_rate=0.045, dividend_yield=0.013, symbol="DIA")
    enriquecido = enrich_options(_chain(), cfg)
    for _, fila in enriquecido.iterrows():
        kind = "c" if str(fila["option_type"]).startswith("c") else "p"
        ref = _oracle(425.5, float(fila["strike"]), 7.0 / 365.0, 0.185, 0.045, 0.013, kind)
        assert float(fila["calc_gamma"]) == pytest.approx(ref["gamma"], abs=1e-12)
        assert float(fila["calc_delta"]) == pytest.approx(ref["delta"], abs=1e-12)
        signo = 1.0 if kind == "c" else -1.0
        esperado = signo * ref["gamma"] * float(fila["open_interest"]) * 100.0 * (425.5 ** 2) * 0.01
        assert float(fila["signed_gex_proxy"]) == pytest.approx(esperado, rel=1e-12)


def test_dex_por_contrato_coincide_con_la_formula_declarada():
    cfg = EngineConfig(risk_free_rate=0.045, dividend_yield=0.013, symbol="DIA")
    enriquecido = enrich_options(_chain(), cfg)
    for _, fila in enriquecido.iterrows():
        esperado = float(fila["calc_delta"]) * float(fila["open_interest"]) * 100.0 * 425.5
        assert float(fila["option_delta_exposure_info"]) == pytest.approx(esperado, rel=1e-12)


def test_gross_gex_es_la_suma_de_magnitudes_y_signed_la_suma_con_signo():
    cfg = EngineConfig(risk_free_rate=0.045, dividend_yield=0.013, symbol="DIA")
    agregado = aggregate_strikes(enrich_options(_chain(), cfg))
    assert len(agregado) == 3
    for _, fila in agregado.iterrows():
        assert float(fila["gross_gex"]) >= abs(float(fila["signed_gex"])) - 1e-9
    # El strike con mayor OI de puts debe quedar con GEX neto negativo.
    peor = agregado.loc[agregado["strike"] == 430.0].iloc[0]
    assert float(peor["signed_gex"]) < 0.0


def test_gamma_flip_interpola_linealmente_el_cruce_por_cero():
    snapshot = pd.DataFrame({"strike": [100.0, 110.0], "signed_gex": [200.0, -300.0],
                             "underlying_price": [104.0, 104.0], "gross_gex": [200.0, 300.0]})
    # Cruce exacto: 100 - 200 * (110-100) / (-300-200) = 104.0
    assert gamma_flip(snapshot) == pytest.approx(104.0, abs=1e-12)


def test_gamma_center_es_la_media_ponderada_por_gex_bruto():
    snapshot = pd.DataFrame({"strike": [100.0, 110.0, 120.0], "gross_gex": [1.0, 3.0, 0.0],
                             "signed_gex": [1.0, -3.0, 0.0], "underlying_price": [105.0] * 3})
    assert gamma_center(snapshot) == pytest.approx((100.0 * 1 + 110.0 * 3) / 4.0, abs=1e-12)


# ------------------------------------------------- validacion de entrada


def test_iv_en_porcentaje_en_vez_de_decimal_no_pasa_silenciosamente():
    """Un 18.5 en lugar de 0.185 arruina toda la superficie: debe detectarse."""
    df = _chain()
    df.loc[0, "iv"] = -0.01
    with pytest.raises(ValueError):
        validate_input(df)


def test_open_interest_negativo_es_rechazado():
    df = _chain()
    df.loc[0, "open_interest"] = -5
    with pytest.raises(ValueError):
        validate_input(df)


def test_columna_ausente_es_rechazada_con_el_nombre():
    df = _chain().drop(columns=["iv"])
    with pytest.raises(ValueError, match="iv"):
        validate_input(df)
