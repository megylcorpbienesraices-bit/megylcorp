"""v1.55.0 · AUDITORIA MATEMATICA DEL MONTE CARLO.

«Corre sin fallar» no es una certificacion. Un simulador con la deriva mal
puesta corre igual de bien y publica probabilidades equivocadas con seis
decimales de aparente precision.

Lo que se comprueba aqui es que el modelo ES el que dice ser, y se comprueba
contra soluciones CERRADAS, no contra si mismo:

    MODELO        GBM neutral al riesgo: E[S_T] = S0*e^{(r-q)T} y varianza exacta
    DETERMINISMO  mismas entradas -> mismo numero; mercado distinto -> numero distinto
    CONVERGENCIA  el error tipico cae como 1/raiz(N), sin sesgo residual
    EXACTITUD     P(terminar) contra N(d2) y P(tocar) contra la formula de
                  primer paso con reflexion, a 1, 5 y 30 DTE
    SENSIBILIDAD  monotonia correcta en IV, DTE, r y q

La prueba de exactitud del TOQUE es la que importa mas: con un solo paso por
dia, contar trayectorias da ~17 % donde la respuesta es ~34 %. La correccion de
puente browniano se mide aqui contra el valor exacto, no contra la intuicion.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import norm

from app.core.expiry_clock import year_fraction
from app.core.monte_carlo import (deterministic_seed, monte_carlo_level_report,
                                  simulate_gbm_paths)

S0, R, Q, IV_PCT = 500.0, 0.045, 0.012, 20.0
SIG = IV_PCT / 100.0


def _mu() -> float:
    """Deriva del LOG-precio bajo la medida neutral al riesgo."""
    return R - Q - 0.5 * SIG * SIG


def _finish_exact(B: float, T: float, above: bool) -> float:
    d = (math.log(B / S0) - _mu() * T) / (SIG * math.sqrt(T))
    return 100.0 * (1.0 - norm.cdf(d)) if above else 100.0 * norm.cdf(d)


def _touch_exact(B: float, T: float) -> float:
    """P(el maximo/minimo alcanza B) para un browniano con deriva. Formula de
    reflexion de Girsanov, monitoreo CONTINUO."""
    b = math.log(B / S0)
    s = SIG * math.sqrt(T)
    mu = _mu()
    if b > 0:
        return 100.0 * (norm.cdf((-b + mu * T) / s)
                        + math.exp(2 * mu * b / (SIG * SIG)) * norm.cdf((-b - mu * T) / s))
    return 100.0 * (norm.cdf((b - mu * T) / s)
                    + math.exp(2 * mu * b / (SIG * SIG)) * norm.cdf((b + mu * T) / s))


# ── 1. El modelo es el que dice ser ──────────────────────────────────────

def test_el_precio_esperado_es_el_del_activo_sin_arbitraje():
    """E[S_T] = S0 * e^{(r-q)T}. Si la deriva estuviera mal puesta —por ejemplo
    sin el termino -sigma^2/2 del paso de log a nivel— este test lo caza."""
    T = year_fraction(30.0)
    terminal = simulate_gbm_paths(S0, R, Q, SIG, 30.0, n_sims=400_000, seed=1)[:, -1]
    exact = S0 * math.exp((R - Q) * T)
    se = terminal.std(ddof=1) / math.sqrt(len(terminal))
    assert abs(terminal.mean() - exact) < 4 * se


def test_la_varianza_terminal_es_la_lognormal_exacta():
    T = year_fraction(30.0)
    terminal = simulate_gbm_paths(S0, R, Q, SIG, 30.0, n_sims=400_000, seed=3)[:, -1]
    exact = S0 * S0 * math.exp(2 * (R - Q) * T) * (math.exp(SIG * SIG * T) - 1.0)
    assert abs(terminal.var(ddof=1) / exact - 1.0) < 0.02


def test_toda_trayectoria_arranca_en_el_spot():
    paths = simulate_gbm_paths(S0, R, Q, SIG, 5.0, n_sims=500, seed=4)
    assert np.allclose(paths[:, 0], S0)
    assert paths.shape[1] == 6           # 5 dias, 1 paso/dia, mas el origen


# ── 2. Determinismo ──────────────────────────────────────────────────────

def test_el_mismo_mercado_publica_el_mismo_numero():
    """Sin esto, dos refrescos seguidos con el mercado quieto daban
    probabilidades distintas y parecia movimiento."""
    a = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {"call_wall": 510.0})
    b = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {"call_wall": 510.0})
    assert a["seed"] == b["seed"]
    assert a["levels"]["call_wall"] == b["levels"]["call_wall"]
    assert a["terminal_percentiles"] == b["terminal_percentiles"]


def test_si_el_mercado_cambia_la_simulacion_cambia():
    """Lo contrario seria congelar el resultado, que es el defecto opuesto."""
    a = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {"call_wall": 510.0})
    for cambio in (dict(spot=S0 + 0.01), dict(atm_iv_pct=IV_PCT + 0.1),
                   dict(dte_days=30.5), dict(levels={"call_wall": 510.5})):
        kw = dict(spot=S0, r=R, q=Q, atm_iv_pct=IV_PCT, dte_days=30.0,
                  levels={"call_wall": 510.0})
        kw.update(cambio)
        b = monte_carlo_level_report(kw["spot"], kw["r"], kw["q"], kw["atm_iv_pct"],
                                     kw["dte_days"], kw["levels"])
        assert b["seed"] != a["seed"], f"la semilla no reacciono a {cambio}"


def test_la_semilla_explicita_manda_sobre_la_derivada():
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {}, seed=99)
    assert rep["seed"] == 99 and rep["seed_source"] == "EXPLICIT"
    assert monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {})["seed_source"] == "DERIVED_FROM_INPUTS"


def test_la_semilla_derivada_cabe_en_el_generador():
    s = deterministic_seed("QQQ", 500.0, 0.2)
    assert 0 <= s < 2 ** 64
    np.random.default_rng(s)             # no debe lanzar


# ── 3. Exactitud contra solucion cerrada ─────────────────────────────────

@pytest.mark.parametrize("dte", [1.0, 5.0, 30.0])
@pytest.mark.parametrize("mult", [0.98, 0.99, 1.01, 1.02])
def test_probabilidad_de_terminar_mas_alla_coincide_con_n_d2(dte, mult):
    B = S0 * mult
    T = year_fraction(dte)
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, dte, {"x": B}, n_sims=200_000, seed=11)
    got = rep["levels"]["x"]["prob_finish_beyond_pct"]
    exact = _finish_exact(B, T, above=B >= S0)
    se = math.sqrt(max(exact * (100 - exact), 1e-9) / 200_000)
    assert abs(got - exact) < 4 * se, f"sim={got:.4f} exacta={exact:.4f}"


@pytest.mark.parametrize("dte", [1.0, 5.0, 30.0])
@pytest.mark.parametrize("mult", [0.98, 0.99, 1.01, 1.02])
def test_probabilidad_de_tocar_coincide_con_el_primer_paso_continuo(dte, mult):
    """El puente browniano tiene que dar precision de monitoreo CONTINUO con un
    solo paso por dia. Contar trayectorias daria aqui la mitad."""
    B = S0 * mult
    T = year_fraction(dte)
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, dte, {"x": B}, n_sims=200_000, seed=11)
    got = rep["levels"]["x"]["prob_touch_by_expiry_pct"]
    exact = _touch_exact(B, T)
    se = math.sqrt(max(exact * (100 - exact), 1e-9) / 200_000)
    assert abs(got - exact) < 4 * se, f"sim={got:.4f} exacta={exact:.4f}"


def test_contar_trayectorias_subestimaria_el_toque_a_la_mitad():
    """La razon de ser de la correccion, medida y no supuesta."""
    dte, B = 1.0, S0 * 1.01
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, dte, {"x": B}, n_sims=200_000, seed=11)
    ingenuo = rep["levels"]["x"]["prob_finish_beyond_pct"]   # 1 paso: tocar == terminar
    corregido = rep["levels"]["x"]["prob_touch_by_expiry_pct"]
    exacto = _touch_exact(B, year_fraction(dte))
    assert corregido / max(ingenuo, 1e-9) > 1.8
    assert abs(corregido - exacto) < 0.2


def test_los_percentiles_terminales_son_los_cuantiles_lognormales():
    T = year_fraction(30.0)
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {}, n_sims=400_000, seed=5)
    for p, v in rep["terminal_percentiles"].items():
        exact = S0 * math.exp(_mu() * T + SIG * math.sqrt(T) * norm.ppf(p / 100.0))
        assert abs(v / exact - 1.0) < 3e-3, f"p{p}: sim={v:.3f} exacta={exact:.3f}"


def test_tocar_nunca_es_menos_probable_que_terminar_mas_alla():
    """Invariante estructural: para terminar mas alla hay que haber pasado."""
    for dte in (1.0, 5.0, 30.0):
        rep = monte_carlo_level_report(S0, R, Q, IV_PCT, dte,
                                       {f"L{i}": S0 * m for i, m in
                                        enumerate((0.96, 0.99, 1.01, 1.04))},
                                       n_sims=50_000, seed=7)
        for name, st in rep["levels"].items():
            assert st["prob_touch_by_expiry_pct"] >= st["prob_finish_beyond_pct"] - 1e-9


# ── 4. Convergencia ──────────────────────────────────────────────────────

def test_el_error_cae_como_uno_partido_por_raiz_de_n():
    """Si el error no bajara con N, el estimador tendria sesgo y mas
    simulaciones no comprarian nada."""
    B, dte = 505.0, 30.0
    exact = _finish_exact(B, year_fraction(dte), above=True)
    rms = {}
    for n in (1_000, 16_000):
        errs = [monte_carlo_level_report(S0, R, Q, IV_PCT, dte, {"x": B},
                                         n_sims=n, seed=2_000 + k)["levels"]["x"]["prob_finish_beyond_pct"] - exact
                for k in range(24)]
        rms[n] = math.sqrt(sum(e * e for e in errs) / len(errs))
        teorico = math.sqrt(exact * (100 - exact) / n)
        assert 0.6 < rms[n] / teorico < 1.6, f"N={n}: rms={rms[n]:.4f} teorico={teorico:.4f}"
    # 16x mas trayectorias -> ~4x menos error.
    assert 2.5 < rms[1_000] / rms[16_000] < 6.5


def test_el_informe_publica_su_propio_error_tipico():
    """Sin el, dos probabilidades separadas por tres decimas parecen distintas."""
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {"x": 505.0}, n_sims=20_000)
    st = rep["levels"]["x"]
    assert 0.2 < st["prob_touch_stderr_pp"] < 0.5
    assert 0.2 < st["prob_finish_stderr_pp"] < 0.5
    # Cuadruplicar N tiene que reducir el error a la mitad.
    big = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {"x": 505.0}, n_sims=80_000)
    assert big["levels"]["x"]["prob_finish_stderr_pp"] < st["prob_finish_stderr_pp"] * 0.6


# ── 5. Sensibilidad ──────────────────────────────────────────────────────

def _touch(**kw) -> float:
    a = dict(spot=S0, r=R, q=Q, atm_iv_pct=IV_PCT, dte_days=30.0)
    a.update(kw)
    return monte_carlo_level_report(a["spot"], a["r"], a["q"], a["atm_iv_pct"],
                                    a["dte_days"], {"x": 510.0},
                                    n_sims=60_000, seed=42)["levels"]["x"]["prob_touch_by_expiry_pct"]


def test_mas_volatilidad_hace_mas_probable_tocar():
    vals = [_touch(atm_iv_pct=iv) for iv in (10, 15, 20, 30, 45)]
    assert vals == sorted(vals), vals


def test_mas_tiempo_hace_mas_probable_tocar():
    vals = [_touch(dte_days=d) for d in (1, 5, 15, 30, 60)]
    assert vals == sorted(vals), vals


def test_la_deriva_empuja_en_el_sentido_correcto():
    """Con la barrera ARRIBA, subir r acerca; subir el dividendo aleja. Es el
    unico sitio donde se ve si r y q entran con el signo que les toca."""
    sube_r = [_touch(r=x) for x in (0.0, 0.02, 0.045, 0.08)]
    assert sube_r == sorted(sube_r), sube_r
    sube_q = [_touch(q=x) for x in (0.0, 0.02, 0.05)]
    assert sube_q == sorted(sube_q, reverse=True), sube_q


# ── 6. Lo que el modelo NO es ────────────────────────────────────────────

def test_el_informe_declara_sus_supuestos_y_no_alimenta_direccion():
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0, {})
    assert rep["model"] == "GBM_CONSTANT_VOL"
    assert "skew" in rep["model_risk"] and "prediccion" in rep["model_risk"]


def test_entradas_imposibles_no_producen_un_cono_falso():
    for bad in (dict(spot=0.0), dict(spot=float("nan")), dict(dte_days=0.0),
                dict(dte_days=float("-inf"))):
        kw = dict(spot=S0, r=R, q=Q, atm_iv_pct=IV_PCT, dte_days=30.0)
        kw.update(bad)
        rep = monte_carlo_level_report(kw["spot"], kw["r"], kw["q"],
                                       kw["atm_iv_pct"], kw["dte_days"], {})
        assert rep["ready"] is False and rep["reason"] == "INVALID_SPOT_OR_DTE"


def test_un_nivel_imposible_se_descarta_en_vez_de_publicarse():
    rep = monte_carlo_level_report(S0, R, Q, IV_PCT, 30.0,
                                   {"bueno": 510.0, "cero": 0.0, "negativo": -5.0,
                                    "nulo": None, "texto": "x"})
    assert set(rep["levels"]) == {"bueno"}
