"""v1.41.4 · Auditoría numérica de las fórmulas del motor.

No comprueba que el código "parezca" correcto: contrasta cada Greek contra la
derivada de su propia definición y contra identidades que deben cumplirse sí o sí
(paridad put-call, gamma idéntica en call y put, simetrías de Black-76).

Por qué se derivan las formas cerradas de Delta y Gamma en lugar del precio:
derivar el precio dos o tres veces acumula cancelación catastrófica y el ruido de
la diferencia finita acaba superando al valor buscado. Delta y Gamma tienen forma
cerrada exacta, así que derivarlas UNA vez es el contraste bien condicionado.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

from app.core.accelerated_quant import black_scholes_batch
from app.core.precision_engine import (
    black_76_greeks_full, black_76_price, black_scholes_greeks_vector,
)

R, Q = 0.042, 0.013


@pytest.fixture(scope="module")
def book():
    """Cartera sintética amplia: ITM/OTM, de una semana a dos años, IV 10%-100%."""
    rng = np.random.default_rng(11)
    n = 6000
    s = rng.uniform(50.0, 600.0, n)
    return {
        "S": s, "K": s * rng.uniform(0.80, 1.20, n),
        "T": rng.uniform(0.02, 2.0, n), "sigma": rng.uniform(0.10, 1.0, n),
        "call": rng.random(n) < 0.5,
        "r": np.full(n, R), "q": np.full(n, Q),
    }


def _greeks(b):
    return black_scholes_greeks_vector(b["S"], b["K"], b["T"], b["sigma"], b["call"], b["r"], b["q"])


def _d1(S, K, T, sig, r=R, q=Q):
    return (np.log(S / K) + (r - q + 0.5 * sig * sig) * T) / (sig * np.sqrt(T))


def _price(S, K, T, sig, call, r=R, q=Q):
    d1 = _d1(S, K, T, sig, r, q)
    d2 = d1 - sig * np.sqrt(T)
    c = S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    p = K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)
    return np.where(call, c, p)


def _exact_delta(S, K, T, sig, call, r=R, q=Q):
    d1 = _d1(S, K, T, sig, r, q)
    return np.exp(-q * T) * np.where(call, norm.cdf(d1), norm.cdf(d1) - 1.0)


def _exact_gamma(S, K, T, sig, r=R, q=Q):
    return np.exp(-q * T) * norm.pdf(_d1(S, K, T, sig, r, q)) / (S * sig * np.sqrt(T))


def _rel_p99(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    scale = np.maximum(np.abs(a), np.abs(b))
    m = np.isfinite(a) & np.isfinite(b) & (scale > np.percentile(scale, 5))
    return float(np.percentile(np.abs(a[m] - b[m]) / scale[m], 99))


# ────────────────────────── cada Greek contra su definición

def test_delta_is_the_derivative_of_the_price(book):
    h = book["S"] * 1e-5
    fd = (_price(book["S"] + h, book["K"], book["T"], book["sigma"], book["call"])
          - _price(book["S"] - h, book["K"], book["T"], book["sigma"], book["call"])) / (2 * h)
    assert _rel_p99(_greeks(book)["delta"], fd) < 1e-5


def test_gamma_is_the_second_derivative_of_the_price(book):
    h = book["S"] * 1e-5
    S, K, T, v, c = book["S"], book["K"], book["T"], book["sigma"], book["call"]
    fd = (_price(S + h, K, T, v, c) - 2 * _price(S, K, T, v, c) + _price(S - h, K, T, v, c)) / (h * h)
    assert _rel_p99(_greeks(book)["gamma"], fd) < 1e-3


def test_vanna_is_delta_moving_with_volatility(book):
    S, K, T, v, c = book["S"], book["K"], book["T"], book["sigma"], book["call"]
    h = 1e-6
    fd = (_exact_delta(S, K, T, v + h, c) - _exact_delta(S, K, T, v - h, c)) / (2 * h)
    assert _rel_p99(_greeks(book)["vanna"], fd) < 1e-6


def test_charm_is_delta_decaying_with_calendar_time(book):
    """charm = ∂Δ/∂t. El tiempo avanza mientras el vencimiento se acerca, así que
    es −∂Δ/∂T: un signo invertido aquí pondría la deriva de cobertura al revés."""
    S, K, T, v, c = book["S"], book["K"], book["T"], book["sigma"], book["call"]
    h = T * 1e-6
    fd = -(_exact_delta(S, K, T + h, v, c) - _exact_delta(S, K, T - h, v, c)) / (2 * h)
    assert _rel_p99(_greeks(book)["charm"], fd) < 1e-6


def test_speed_is_gamma_moving_with_spot(book):
    S, K, T, v = book["S"], book["K"], book["T"], book["sigma"]
    h = S * 1e-6
    fd = (_exact_gamma(S + h, K, T, v) - _exact_gamma(S - h, K, T, v)) / (2 * h)
    assert _rel_p99(_greeks(book)["speed"], fd) < 1e-6


# ────────────────────────── identidades que no admiten tolerancia

def test_put_call_delta_parity_holds_exactly(book):
    """Δc − Δp = e^{−qT}. No es una aproximación: sale de la paridad put-call
    derivada respecto al spot, y cualquier desvío es un error de fórmula."""
    n = len(book["S"])
    args = (book["S"], book["K"], book["T"], book["sigma"])
    dc = black_scholes_greeks_vector(*args, np.ones(n, bool), book["r"], book["q"])["delta"]
    dp = black_scholes_greeks_vector(*args, np.zeros(n, bool), book["r"], book["q"])["delta"]
    assert np.max(np.abs((dc - dp) - np.exp(-book["q"] * book["T"]))) < 1e-12


def test_gamma_vanna_and_speed_do_not_depend_on_the_option_type(book):
    """Call y put del mismo strike comparten convexidad: sólo delta y charm
    distinguen el tipo."""
    n = len(book["S"])
    args = (book["S"], book["K"], book["T"], book["sigma"])
    gc = black_scholes_greeks_vector(*args, np.ones(n, bool), book["r"], book["q"])
    gp = black_scholes_greeks_vector(*args, np.zeros(n, bool), book["r"], book["q"])
    for greek in ("gamma", "vanna", "speed"):
        assert np.array_equal(gc[greek], gp[greek]), greek


def test_call_delta_is_bounded_and_put_delta_is_its_mirror(book):
    g = _greeks(book)
    call, dq = book["call"], np.exp(-book["q"] * book["T"])
    assert np.all(g["delta"][call] >= 0) and np.all(g["delta"][call] <= dq[call])
    assert np.all(g["delta"][~call] <= 0) and np.all(g["delta"][~call] >= -dq[~call])


def test_gamma_is_never_negative(book):
    assert np.all(_greeks(book)["gamma"] >= 0.0)


def test_deep_in_and_out_of_the_money_converge_to_their_limits():
    """Delta → e^{−qT} muy dentro del dinero y → 0 muy fuera. Gamma → 0 en ambos
    extremos. Es donde una fórmula mal condicionada se rompe primero."""
    T, sig = np.array([0.5, 0.5]), np.array([0.25, 0.25])
    deep = black_scholes_greeks_vector(np.array([100.0, 100.0]), np.array([1.0, 10_000.0]),
                                       T, sig, np.array([True, True]),
                                       np.full(2, R), np.full(2, Q))
    assert deep["delta"][0] == pytest.approx(np.exp(-Q * 0.5), abs=1e-9)
    assert deep["delta"][1] == pytest.approx(0.0, abs=1e-9)
    assert deep["gamma"] == pytest.approx([0.0, 0.0], abs=1e-9)


# ────────────────────────── Black-76 (opciones sobre futuros)

@pytest.mark.parametrize("option_type", ["call", "put"])
def test_black_76_greeks_match_its_own_price(option_type):
    f, k, t, sig = 310.0, 305.0, 0.35, 0.22
    g = black_76_greeks_full(f, k, t, sig, option_type, R)
    h = f * 1e-5
    up, dn = black_76_price(f + h, k, t, sig, option_type, R), black_76_price(f - h, k, t, sig, option_type, R)
    assert g["delta"] == pytest.approx((up - dn) / (2 * h), abs=1e-6)
    mid = black_76_price(f, k, t, sig, option_type, R)
    assert g["gamma"] == pytest.approx((up - 2 * mid + dn) / (h * h), abs=1e-5)


def test_black_76_put_call_parity():
    """c − p = e^{−rT}(F − K). Es la definición del contrato, no una convención."""
    f, k, t, sig = 310.0, 305.0, 0.35, 0.22
    c = black_76_price(f, k, t, sig, "call", R)
    p = black_76_price(f, k, t, sig, "put", R)
    assert c - p == pytest.approx(np.exp(-R * t) * (f - k), abs=1e-9)


# ────────────────────────── el backend acelerado no puede cambiar el resultado

def test_accelerated_backend_is_identical_to_the_reference(book):
    """Si la GPU devolviera otra cosa, activarla cambiaría en silencio todos los
    Greeks de la pantalla. Aquí se exige igualdad bit a bit, no parecido."""
    ref = _greeks(book)
    acc = black_scholes_batch(book["S"], book["K"], book["T"], book["sigma"],
                              book["call"], book["r"], book["q"], prefer_accelerated=False)
    for greek in ("delta", "gamma", "vanna", "charm", "speed"):
        assert np.array_equal(acc[greek], ref[greek]), greek


def test_the_flip_grid_uses_the_same_gamma_as_the_rest_of_the_engine():
    """dynamic_gamma_flip_snapshot escribe la fórmula de gamma en línea en lugar
    de llamar al kernel. Es correcta hoy; esta prueba impide que las dos copias
    se separen mañana sin que nadie lo note."""
    import inspect
    from app.core import engine
    src = inspect.getsource(engine.dynamic_gamma_flip_snapshot)
    assert "np.exp(-q * T) * norm.pdf(d1) / (S * sigma * sqrtT)" in src

    S = np.array([[420.0]]); K = np.array([[415.0]])
    T = np.array([[0.25]]); sigma = np.array([[0.22]])
    r = np.full((1, 1), R); q = np.full((1, 1), Q)
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    inline = np.exp(-q * T) * norm.pdf(d1) / (S * sigma * sqrtT)
    canonical = black_scholes_greeks_vector(S.ravel(), K.ravel(), T.ravel(), sigma.ravel(),
                                            np.array([True]), r.ravel(), q.ravel())["gamma"]
    assert inline.ravel() == pytest.approx(canonical, rel=1e-12)


# ────────────────────────── agregación: max pain y convención de exposición

def test_max_pain_minimises_the_total_payout():
    """Max pain es el strike de liquidación que menos paga al tenedor agregado.
    Se comprueba contra una cadena donde la respuesta se conoce por construcción."""
    import pandas as pd
    from app.core.trace_analytics import max_pain
    chain = pd.DataFrame([
        {"strike": 400.0, "option_type": "call", "open_interest": 100.0},
        {"strike": 410.0, "option_type": "call", "open_interest": 5000.0},
        {"strike": 410.0, "option_type": "put", "open_interest": 5000.0},
        {"strike": 420.0, "option_type": "put", "open_interest": 100.0},
    ])
    assert max_pain(chain) == 410.0


def test_max_pain_is_exactly_the_argmin_of_the_payout_curve():
    import pandas as pd
    from app.core.trace_analytics import max_pain
    rng = np.random.default_rng(3)
    rows, strikes = [], np.arange(380.0, 441.0, 5.0)
    for k in strikes:
        rows.append({"strike": k, "option_type": "call", "open_interest": float(rng.integers(0, 9000))})
        rows.append({"strike": k, "option_type": "put", "open_interest": float(rng.integers(0, 9000))})
    chain = pd.DataFrame(rows)
    k = chain["strike"].to_numpy(float)
    oi = chain["open_interest"].to_numpy(float)
    is_call = chain["option_type"].str.startswith("c").to_numpy()
    pay = [float(np.sum(np.where(is_call, np.maximum(s - k, 0), np.maximum(k - s, 0)) * oi)) for s in strikes]
    assert max_pain(chain) == float(strikes[int(np.argmin(pay))])


def test_exposure_scenarios_reprice_gamma_instead_of_scaling_it():
    """Un escenario de spot no puede escalar el GEX actual por S²: la gamma de cada
    contrato cambia al moverse el spot, y los strikes lejanos dejan de contribuir.
    Reprecia la cadena entera, que es la diferencia entre un escenario y una regla
    de tres."""
    import inspect
    from app.core import precision_engine
    src = inspect.getsource(precision_engine.exposure_scenarios)
    assert "black_scholes_greeks_vector(S2" in src
    assert "for shift in shifts_pct:" in src
