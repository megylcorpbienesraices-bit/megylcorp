"""v1.42 · Universal Quantitative Truth Architecture.

CERTIFICACIÓN EN CUATRO NIVELES
-------------------------------
Un test que pasa no demuestra que una fórmula sirva. Cada pieza se certifica así:

    UNIT      la fórmula está implementada como dice estar
    GOLDEN    coincide con una referencia independiente conocida
    PROPERTY  cumple los invariantes matemáticos que DEBE cumplir
    ECONOMIC  distingue lo que dice distinguir en datos

Los cuatro niveles están marcados en el nombre de cada test.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════ P0-2 · ContractSpec

def test_unit_contract_spec_parses_osi_and_resolves_multiplier():
    from app.core.contract_spec import from_row, from_symbol, parse_osi

    osi = parse_osi("AAPL240119C00150000")
    assert osi["underlying"] == "AAPL"
    assert osi["option_type"] == "CALL"
    assert osi["strike"] == 150.0
    assert osi["expiration"].isoformat() == "2024-01-19"

    assert from_symbol("DIA").multiplier == 100.0
    assert from_symbol("YM").multiplier == 5.0
    assert from_symbol("MYM").multiplier == 0.5


def test_property_an_adjusted_contract_never_uses_the_standard_multiplier():
    """Una acción corporativa cambia el entregable, y con él el valor de un punto.

    Un contrato ajustado que entrega 62 acciones más efectivo NO vale 100 × prima.
    Con el ×100 escrito a mano, esa cifra salía plausible y equivocada, que es peor
    que salir vacía.
    """
    from app.core.contract_spec import from_row

    spec = from_row({
        "contract_symbol": "AAPL1240119C00150000",
        "deliverables": [{"type": "equity", "amount": 62}, {"type": "cash", "amount": 173.40}],
    })
    assert spec.adjusted is True
    assert spec.multiplier == 62.0
    assert spec.multiplier_source == "PROVIDER"
    assert spec.notional(2.00, 1) == 124.0          # no 200
    assert "AJUSTADO" in spec.notes


def test_property_notional_is_linear_in_price_and_quantity():
    from app.core.contract_spec import from_symbol

    spec = from_symbol("DIA")
    assert spec.notional(2.0, 10) == pytest.approx(2 * spec.notional(1.0, 10))
    assert spec.notional(2.0, 10) == pytest.approx(10 * spec.notional(2.0, 1))


def test_unit_multiplier_series_matches_row_by_row_resolution():
    from app.core.contract_spec import from_row, multiplier_series

    frame = pd.DataFrame({"contract_symbol": ["YM250321C00420000", "YM250321P00420000"]})
    vec = list(multiplier_series(frame, "YM"))
    scalar = [from_row({"contract_symbol": s}).multiplier for s in frame["contract_symbol"]]
    assert vec == scalar == [5.0, 5.0]


# ══════════════════════════════════════════════ P0-3 · sin ×100 económicos

_MONEY_MODULES = ("flow_intelligence", "dealer_intelligence", "trace_analytics",
                  "option_stream", "nextgen_terminal", "market_state_field",
                  "derivatives_intelligence", "expiry_intelligence", "profile_engine",
                  "institutional_modules")


def test_property_no_hardcoded_contract_multiplier_in_money_paths():
    """El tamaño del contrato sale de `ContractSpec`, nunca de un literal.

    Se detecta el patrón `<algo> * 100` en los módulos que producen cifras
    monetarias. Un `* 100.0` para convertir a porcentaje es otra cosa y vive en
    variables con nombre de porcentaje; el guardia mira los nombres implicados.
    """
    offenders = []
    money_words = ("premium", "notional", "gex", "dex", "exposure", "delta_shares",
                   "hedge", "vanna_exposure", "charm_exposure")
    # Magnitudes adimensionales: un ×100 ahí convierte una fracción en porcentaje,
    # que es otra cosa y es correcta.
    dimensionless = ("score", "pct", "percent", "ratio", "rank", "share", "prob")
    for name in _MONEY_MODULES:
        path = ROOT / "app" / "core" / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = " ".join(
                t.id if isinstance(t, ast.Name) else
                (t.slice.value if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                 and isinstance(t.slice.value, str) else "")
                for t in node.targets
            ).lower()
            if not any(w in targets for w in money_words):
                continue
            if any(w in targets for w in dimensionless):
                continue
            for sub in ast.walk(node.value):
                if (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mult)
                        and isinstance(sub.right, ast.Constant)
                        and isinstance(sub.right.value, (int, float))
                        and float(sub.right.value) == 100.0):
                    offenders.append(f"{name}.py:{node.lineno}: {targets.strip()}")
    assert not offenders, (
        "multiplicador de contrato escrito a mano en una ruta monetaria:\n  "
        + "\n  ".join(offenders)
        + "\n\nUsa contract_spec.multiplier_series(frame, symbol) o row_multiplier(row)."
    )


# ══════════════════════════════════════════════ P0-4 · dispatcher único

def test_property_no_module_bypasses_the_greeks_dispatcher():
    """Sólo `precision_engine` (implementación) y `greeks_service` (puerta) pueden
    tocar Black-Scholes o Black-76 directamente.

    Antes de v1.42, TRACE, Dealer y Market State importaban `black_scholes_greeks_vector`
    por su cuenta. Para un ETF daba lo mismo; para un future option NO, y producía una
    Gamma de TRACE distinta de la del Scanner presentada como el mismo concepto.
    """
    allowed = {"precision_engine.py", "greeks_service.py", "accelerated_quant.py"}
    forbidden = ("black_scholes_greeks_vector", "black_scholes_greeks_full",
                 "black_76_greeks_vector", "black_76_greeks_full")
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name in allowed or "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        for token in forbidden:
            if f"import {token}" in src or f"{token}(" in src:
                offenders.append(f"{path.relative_to(ROOT)} → {token}")
    assert not offenders, (
        "secciones que esquivan el dispatcher de Greeks:\n  " + "\n  ".join(offenders))


def test_economic_the_dispatcher_changes_the_answer_for_a_future_option():
    """La unificación no es cosmética: cambia el número para un futuro.

    Black-76 descuenta el forward y no aplica dividendo. Tratar el precio del futuro
    como si fuera un spot con dividendo desplaza delta y, en el caso de vanna, le
    cambia el SIGNO. Dos secciones no pueden publicar eso como la misma métrica.
    """
    from app.core import greeks_service as G
    from app.core.precision_engine import black_scholes_greeks_vector

    args = (np.array([45000.0]), np.array([45000.0]), np.array([0.05]),
            np.array([0.15]), np.array([True]))
    r, q = np.array([0.045]), np.array([0.0])

    correct = G.greeks_vector("YM", *args, r, q)
    naive = black_scholes_greeks_vector(*args, r, q)

    assert abs(float(correct["delta"][0]) - float(naive["delta"][0])) > 1e-3
    assert np.sign(correct["vanna"][0]) != np.sign(naive["vanna"][0])
    assert G.describe_dispatch("YM")["option_model"] == "FUTURE_OPTION"
    assert G.describe_dispatch("DIA")["option_model"] == "EQUITY_OPTION"


def test_golden_vega_matches_finite_difference_of_price():
    """Nivel GOLDEN: vega contra la derivada numérica del precio, que es su definición."""
    from app.core import greeks_service as G

    S, K, T, sig, r, q = 450.0, 455.0, 0.25, 0.20, 0.045, 0.015
    h = 1e-5
    up = G.price("DIA", S, K, T, sig + h, "call", r, q)
    dn = G.price("DIA", S, K, T, sig - h, "call", r, q)
    numeric = (up - dn) / (2 * h)
    analytic = G.greeks("DIA", S, K, T, sig, "call", r, q)["vega"]
    assert analytic == pytest.approx(numeric, rel=1e-6)


# ══════════════════════════════════════════════ P0-7 · unidades

def test_golden_gex_unit_conversions_are_exact():
    """GEX por 1 % = GEX por $1 × S × 0.01 = gamma cruda × S² × 0.01. Exacto."""
    from app.core import units_registry as U

    spot = 450.0
    per_pct = U.gamma_exposure(1_000_000.0, underlying="DIA", spot=spot)
    assert per_pct.to("GEX_PER_1D").value == pytest.approx(1_000_000.0 / (spot * 0.01))
    assert per_pct.to("GAMMA_RAW").value == pytest.approx(1_000_000.0 / (spot ** 2 * 0.01))
    assert per_pct.to("GEX_PER_1D").to("GEX_PER_1PCT").value == pytest.approx(1_000_000.0)


def test_property_comparability_requires_all_seven_conditions():
    """Antes de enfrentar ITM contra Quant Data hay siete cosas que deben coincidir."""
    from app.core import units_registry as U

    base = dict(underlying="DIA", spot=450.0, timestamp=1_000_000.0)
    a = U.gamma_exposure(5e6, **base)

    assert U.comparable(a, U.gamma_exposure(5e6, **base))["comparable"] is True

    # universo de vencimientos distinto
    v = U.comparable(a, U.gamma_exposure(5e6, expiration_universe="0DTE", **base))
    assert v["comparable"] is False and "vencimientos" in v["blockers"][0]

    # instante distinto
    v = U.comparable(a, U.gamma_exposure(5e6, underlying="DIA", spot=450.0, timestamp=1_000_400.0))
    assert v["comparable"] is False

    # subyacente a otro precio
    v = U.comparable(a, U.gamma_exposure(5e6, underlying="DIA", spot=460.0, timestamp=1_000_000.0))
    assert v["comparable"] is False

    # griegas distintas no se convierten jamás
    with pytest.raises(Exception):
        a.to("DEX_NOTIONAL")


# ══════════════════════════════════════════════ P0-6 · autoridad por métrica

def test_property_gex_is_never_averaged_across_providers():
    from app.core.metric_authority import fusion_allowed, resolve
    from app.core.provider_parity import fuse_values

    assert fusion_allowed("gex") is False
    assert fusion_allowed("underlying_price") is True

    out = fuse_values([{"provider": "ALPACA", "quality_score": 90.0, "gex": 100.0},
                       {"provider": "QUANTDATA", "quality_score": 88.0, "gex": 104.0}], "gex")
    assert out["status"] == "FUSION_FORBIDDEN"
    assert "value" not in out


def test_unit_scanner_direction_fails_closed_without_its_authority():
    """La dirección jamás procede de un proveedor. Si falta ITM, no hay dirección."""
    from app.core.metric_authority import resolve
    from app.core.quant_errors import ProviderUnavailable

    with pytest.raises(ProviderUnavailable):
        resolve("scanner_direction", [{"provider": "QUANTDATA", "value": "BUY"}])


def test_unit_net_drift_belongs_to_quantdata_only():
    from app.core.metric_authority import policy

    p = policy("net_drift")
    assert p.authority == "QUANTDATA"
    assert p.fusion_contract is None
    assert "ITM no tiene un Net Drift" in p.note


# ══════════════════════════════════════════════ P1-8 · IV y riesgo americano

def test_property_iv_without_vega_is_declared_unidentifiable():
    """Con vega ~ 0 un tick de prima mueve la IV varios puntos. Publicarla es ficción."""
    from app.core import iv_quality as Q

    deep = Q.assess(symbol="DIA", S=450, K=250, T=0.002, option_type="call",
                    r=0.04, q=0.015, bid=200.0, ask=200.02)
    assert deep.state in (Q.UNIDENTIFIABLE, Q.INVALID)
    assert deep.usable_for_surface is False


def test_unit_crossed_and_out_of_bounds_quotes_are_invalid():
    from app.core import iv_quality as Q

    crossed = Q.assess(symbol="DIA", S=450, K=450, T=0.05, option_type="call",
                       r=0.04, q=0.015, bid=8.1, ask=7.9)
    assert crossed.state == Q.INVALID and "cruzado" in crossed.reason

    impossible = Q.assess(symbol="DIA", S=450, K=450, T=0.05, option_type="call",
                          r=0.04, q=0.015, mid=500.0)
    assert impossible.state == Q.INVALID and "no arbitraje" in impossible.reason


def test_golden_american_call_without_dividend_equals_european():
    """Resultado clásico: sin dividendo nunca conviene ejercer una call antes."""
    from app.core import american_risk as A

    r = A.assess(symbol="SPY", S=450, K=440, T=0.5, sigma=0.20,
                 option_type="call", r=0.045, q=0.0)
    assert r.screened_out is True
    assert r.premium == 0.0
    assert "nunca conviene ejercer" in r.reason


def test_economic_deep_itm_put_has_material_early_exercise_premium():
    """Donde BSM sí se equivoca de verdad: put ITM con tipo positivo."""
    from app.core import american_risk as A

    r = A.assess(symbol="SPY", S=380, K=450, T=1.0, sigma=0.20,
                 option_type="put", r=0.05, q=0.0)
    assert r.risk == A.RISK_ELEVATED
    assert r.premium > 1.0
    assert r.premium_pct > 5.0
    assert r.american_price > r.european_price     # no arbitraje


# ══════════════════════════════════════════════ P1-10 · flujo probabilístico

def test_unit_sweep_at_ask_is_a_confident_buy_distribution():
    from app.core import flow_probability as F

    p = F.classify(price=8.10, bid=7.90, ask=8.10, quote_age_ms=50,
                   size=40, ask_size=10, prev_trade_price=8.00)
    assert p.buy > 0.85 and p.sell < 0.10
    assert p.buy + p.sell + p.unknown == pytest.approx(1.0)
    assert "SWEEP_LIKE" in p.conditions


def test_property_a_mid_print_with_no_tick_history_carries_no_direction():
    """Un print a mitad de spread no dice quién inició. Repartirlo sería inventar."""
    from app.core import flow_probability as F

    p = F.classify(price=8.00, bid=7.90, ask=8.10, quote_age_ms=40)
    assert p.unknown == pytest.approx(1.0)
    assert p.net_sign == pytest.approx(0.0)


def test_property_corrected_prints_are_never_classified():
    from app.core import flow_probability as F

    p = F.classify(price=8.10, bid=7.90, ask=8.10, quote_age_ms=50, conditions="z")
    assert p.unknown == 1.0
    assert p.method == "NON_CLASSIFIABLE_CONDITION"


def test_economic_ambiguous_tape_does_not_manufacture_conviction():
    """Mil prints ambiguos no deben producir un sesgo direccional agregado."""
    from app.core import flow_probability as F

    rows = [{"contract_symbol": "X", "trade_price": 1.00, "bid": 0.80, "ask": 1.20,
             "quote_age_ms": 40, "premium": 1000.0} for _ in range(1000)]
    agg = F.aggregate(pd.DataFrame(rows))
    assert agg["classified_pct"] < 25.0
    assert abs(agg["net_premium"]) < 0.10 * 1000 * 1000


# ══════════════════════════════════════════════ P1-9 · dealer probabilístico

def _dealer_tape(n=60):
    """Cinta con prints EN EL ASK: hay agresión comprador que clasificar.

    Con prints exactamente a mitad de spread y sin histórico de tick, la banda
    correcta es cero — no hay dirección que inferir — y esa es precisamente la
    propiedad que comprueba `test_property_a_mid_print_with_no_tick_history...`.
    """
    return pd.DataFrame([{
        "timestamp": pd.Timestamp("2026-09-18 10:00") + pd.Timedelta(seconds=i),
        "contract_symbol": f"DIA260116C0045{i % 3}000",
        "trade_price": 2.15, "bid": 2.05, "ask": 2.15, "quote_age_ms": 60,
        "contracts": 25 + i, "bid_size": 20, "ask_size": 18,
        "underlying_price": 450.0, "model_delta": 0.42, "model_gamma": 0.021,
        "open_interest": 900, "daily_volume": 2400, "dte": 4.0,
    } for i in range(n)])


def test_property_dealer_pressure_is_a_band_and_is_deterministic():
    from app.core import dealer_latent_state as D

    a = D.hedge_pressure_distribution(_dealer_tape(), symbol="DIA")
    b = D.hedge_pressure_distribution(_dealer_tape(), symbol="DIA")
    assert a["ready"] and a["hedge_pressure"] == b["hedge_pressure"]
    hp = a["hedge_pressure"]
    assert hp["p10"] < hp["median"] < hp["p90"]
    assert a["kind"] == "INFERRED"
    assert 0.0 <= a["direction_confidence"] <= 100.0


def test_property_counterparty_share_is_a_prior_not_a_constant():
    from app.core import dealer_latent_state as D

    out = D.hedge_pressure_distribution(_dealer_tape(), symbol="DIA")
    prior = out["counterparty_prior"]
    assert prior["mean_pct"] == pytest.approx(70.0, abs=0.1)
    assert "no se observa" in prior["note"]
    # una banda de ancho cero significaría que las incógnitas desaparecieron
    assert out["hedge_pressure"]["p90"] > out["hedge_pressure"]["p10"]


# ══════════════════════════════════════════════ P1-11 · champion/challenger

def _cc_sample(n=1200, seed=7):
    rng = np.random.default_rng(seed)
    g = np.repeat(np.arange(24), n // 24)
    x = rng.normal(size=(n, 2))
    p = 1 / (1 + np.exp(-(0.9 * x[:, 0] + 0.5 * x[:, 1])))
    return x, (rng.random(n) < p).astype(float), g


def test_golden_cox_calibration_slope_is_one_for_a_true_model():
    """La regresión de calibración debe ser LOGÍSTICA.

    Con OLS sobre el logit y una `y` binaria, incluso un modelo perfecto devuelve
    pendiente ~0,2, y el modelo bueno parecería peor calibrado que uno plano.
    """
    from app.core.champion_challenger import calibration_line

    x, y, _ = _cc_sample()
    true_p = 1 / (1 + np.exp(-(0.9 * x[:, 0] + 0.5 * x[:, 1])))
    cal = calibration_line(true_p, y)
    assert cal["slope"] == pytest.approx(1.0, abs=0.15)
    assert abs(cal["intercept"]) < 0.20


def test_economic_a_true_challenger_wins_and_a_flat_one_never_does():
    from app.core import champion_challenger as CC

    x, y, g = _cc_sample()
    champ = CC.Challenger("CHAMPION", lambda f: 1 / (1 + np.exp(-(0.4 * f[:, 0]))), "expert")
    good = CC.Challenger("A", lambda f: 1 / (1 + np.exp(-(0.9 * f[:, 0] + 0.5 * f[:, 1]))), "logistic")
    flat = CC.Challenger("B", lambda f: np.full(len(f), 0.5), "constant")

    r = CC.evaluate(features=x, labels=y, groups=g, champion=champ,
                    challengers=[good, flat], n_windows=4)
    assert r["challengers"]["A"]["promotable"] is True
    assert r["challengers"]["B"]["promotable"] is False
    assert r["promoted"] == ["A"]


def test_property_winning_one_window_is_not_enough_to_promote():
    """Con muchos candidatos, alguno gana una ventana por azar. Se exige mayoría."""
    from app.core import champion_challenger as CC

    x, y, g = _cc_sample()
    champ = CC.Challenger("CHAMPION", lambda f: 1 / (1 + np.exp(-(0.9 * f[:, 0] + 0.5 * f[:, 1]))))
    noisy = CC.Challenger("NOISE", lambda f: np.clip(
        1 / (1 + np.exp(-(0.9 * f[:, 0] + 0.5 * f[:, 1]))) + 0.02 * np.sin(f[:, 0] * 31), 0.01, 0.99))
    r = CC.evaluate(features=x, labels=y, groups=g, champion=champ, challengers=[noisy], n_windows=4)
    res = r["challengers"]["NOISE"]
    assert r["required_window_wins"] >= 3
    assert res["promotable"] is False


# ══════════════════════════════════════════════ P1-13 · factores macro

def test_economic_factor_model_recovers_betas_and_rejects_spurious_ones():
    from app.core import macro_factor_engine as M

    rng = np.random.default_rng(3)
    n = 400
    idx = pd.bdate_range("2024-01-01", periods=n)
    F = M.build_factor_frame({
        "RATES_LEVEL": pd.Series(np.cumsum(rng.normal(0, 0.03, n)), index=idx),
        "CREDIT": pd.Series(np.cumsum(rng.normal(0, 0.02, n)), index=idx),
        "DOLLAR": pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.003, n))), index=idx),
    })
    y = pd.Series(0.9 * F["RATES_LEVEL"].fillna(0) - 0.5 * F["CREDIT"].fillna(0)
                  + rng.normal(0, 0.004, n), index=idx)
    fit = M.fit_asset(y, F)
    assert fit["ready"]
    betas = {b["factor"]: b for b in fit["betas"]}
    assert betas["RATES_LEVEL"]["beta"] == pytest.approx(0.9, abs=0.05)
    assert betas["CREDIT"]["beta"] == pytest.approx(-0.5, abs=0.05)
    assert betas["DOLLAR"]["significant"] is False    # no existe: no se inventa


def test_property_a_short_sample_never_produces_betas():
    from app.core import macro_factor_engine as M

    idx = pd.bdate_range("2024-01-01", periods=40)
    F = M.build_factor_frame({"RATES_LEVEL": pd.Series(np.arange(40, dtype=float), index=idx)})
    out = M.fit_asset(pd.Series(np.random.normal(size=40), index=idx), F)
    assert out["ready"] is False
    assert "insuficiente" in out["reason"]


# ══════════════════════════════════════════════ P1-14 · replay ejecutable

def test_economic_replay_prices_the_spread_and_the_fees():
    """El P&L ejecutable descuenta el spread de entrada y de salida. Siempre."""
    from app.core import option_replay_store as R

    snaps = pd.DataFrame([
        {"timestamp": "2026-09-18T10:00:00", "contract_symbol": "C", "bid": 2.00,
         "ask": 2.20, "mid": 2.10, "quote_timestamp": "2026-09-18T10:00:00"},
        {"timestamp": "2026-09-18T14:00:00", "contract_symbol": "C", "bid": 2.90,
         "ask": 3.10, "mid": 3.00, "quote_timestamp": "2026-09-18T14:00:00"},
    ])
    out = R.replay_trades([{"contract_symbol": "C", "entry_time": "2026-09-18T10:00:00",
                            "exit_time": "2026-09-18T14:00:00", "quantity": 10}], snaps)
    trade = out["results"][0]
    assert trade["entry_price"] == 2.20       # se paga el ask
    assert trade["exit_price"] == 2.90        # se recibe el bid
    assert trade["mid_to_mid_pnl"] == 900.0
    assert trade["net_pnl"] == 684.0
    assert trade["execution_drag"] == 216.0


def test_property_replay_never_uses_a_quote_from_the_future():
    from app.core import option_replay_store as R

    snaps = pd.DataFrame([
        {"timestamp": "2026-09-18T14:00:00", "contract_symbol": "C", "bid": 5.0, "ask": 5.2,
         "quote_timestamp": "2026-09-18T14:00:00"},
    ])
    out = R.replay_trades([{"contract_symbol": "C", "entry_time": "2026-09-18T10:00:00",
                            "exit_time": "2026-09-18T14:00:00"}], snaps)
    assert out["ready"] is False
    assert "hacia adelante" in out["errors"][0]["reason"]


# ══════════════════════════════════════════════ P1-15 · SSVI en sombra

def test_golden_ssvi_recovers_its_own_parameters():
    from app.core import ssvi_shadow as S

    rng = np.random.default_rng(11)
    slices = []
    for T, th in ((0.05, 0.0040), (0.12, 0.0095), (0.30, 0.0230)):
        k = np.linspace(-0.25, 0.25, 15)
        w = S.ssvi_total_variance(k, th, -0.35, 0.7, 0.35) + rng.normal(0, 2e-5, k.size)
        slices.append({"k": k, "w": w, "T": T, "vega": np.exp(-2 * k ** 2)})
    fit = S.fit_ssvi(slices)
    assert fit["ready"]
    assert fit["params"]["eta"] == pytest.approx(0.7, abs=0.08)
    assert fit["params"]["gamma"] == pytest.approx(0.35, abs=0.08)
    assert all(r == pytest.approx(-0.35, abs=0.05) for r in fit["params"]["rho"])
    assert fit["butterfly"]["pass"] and fit["calendar"]["pass"]


def test_property_ssvi_never_takes_authority_from_svi():
    from app.core import ssvi_shadow as S

    fit = {"ready": True, "model": "eSSVI", "per_slice": [
        {"T": 0.1, "rmse_total_variance": 1e-9, "vega_weighted_rmse": 1e-9}],
        "calendar": {"pass": True}}
    out = S.compare_to_svi(fit, [{"rmse_total_variance": 1.0}])
    assert out["ssvi_wins"] == 1
    assert out["authority"] == "SVI"      # gana el ajuste y NO gana la autoridad


# ══════════════════════════════════════════════ P1-16 · escenarios

def test_economic_gbm_understates_touch_probability_versus_the_bootstrap():
    """El hallazgo que justifica los challengers: GBM subestima el toque.

    Casi toda decisión de opciones depende de una probabilidad de TOCAR un nivel,
    no de la varianza del cierre. Con agrupamiento de volatilidad real, remuestrear
    bloques históricos da una probabilidad de toque muy superior a la del GBM.
    """
    from app.core import scenario_challengers as S

    rng = np.random.default_rng(5)
    vol = 0.0012 * (1 + 0.8 * np.abs(np.sin(np.arange(4000) / 120)))
    r = rng.normal(0, vol) - 0.004 * (rng.random(4000) < 0.01)
    out = S.run_all(450.0, sigma_annual=0.18, horizon_years=1 / 252,
                    returns=r, regime_flags=vol > np.median(vol), levels=(455.0,))
    gbm = out["models"]["GBM"]["touch_probability"]["455"]
    boot = out["models"]["BLOCK_BOOTSTRAP"]["touch_probability"]["455"]
    assert out["models"]["BLOCK_BOOTSTRAP"]["ready"] is True
    assert boot > gbm
    assert out["authority"] == "GBM"      # el hallazgo no promueve por sí solo


# ══════════════════════════════════════════════ P2 · semántica

def test_property_oi_change_is_never_inferred_from_volume():
    from app.core import oi_semantics as O

    out = O.compute_oi_change(None, None)
    assert out["ready"] is False
    assert "El volumen NO es un sustituto" in out["reason"]

    today = pd.DataFrame({"contract_symbol": ["A", "B"], "open_interest": [120, 80]})
    prev = pd.DataFrame({"contract_symbol": ["A", "B"], "open_interest": [100, 95]})
    real = O.compute_oi_change(today, prev)
    assert real["opened"] == 20.0 and real["closed"] == 15.0 and real["total_change"] == 5.0


def test_unit_open_interest_always_carries_its_effective_date():
    from app.core import oi_semantics as O

    fresh = O.read_open_interest(1200, effective_date="2026-09-17", asof="2026-09-18")
    assert fresh.freshness == O.FRESHNESS_CURRENT and fresh.sessions_behind == 1

    old = O.read_open_interest(1200, effective_date="2026-09-10", asof="2026-09-18")
    assert old.freshness == O.FRESHNESS_STALE

    undated = O.read_open_interest(1200, effective_date=None)
    assert undated.freshness == O.FRESHNESS_UNKNOWN


def test_property_dark_pool_separates_facts_from_derivations():
    from app.core import dark_pool_taxonomy as D

    ticks = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-18 14:00", periods=400, freq="1s", tz="UTC"),
        "price": 450 + np.concatenate([np.zeros(100), np.linspace(0, 0.5, 300)]),
        "size": np.full(400, 100)})
    prints = [
        {"timestamp": "2026-09-18T14:01:40Z", "exchange": "D", "size": 250000, "price": 450.0},
        {"timestamp": "2026-09-18T14:01:40Z", "exchange": "P", "size": 15000, "price": 450.0},
    ]
    panel = D.build_panel(prints, ticks, adv=5_000_000)
    cats = panel["categories"]
    assert cats[D.CONFIRMED_OFF_EXCHANGE]["evidence"] == "HECHO REPORTADO"
    assert cats[D.LARGE_PRINT]["evidence"] == "HECHO REPORTADO CON UMBRAL ELEGIDO"
    assert cats[D.DERIVED_LIQUIDITY_ZONE]["evidence"] == "DERIVACIÓN DE ITM"
    item = cats[D.CONFIRMED_OFF_EXCHANGE]["items"][0]
    assert item["pct_adv"] == 5.0
    assert item["behaviour"] == "CONTINUACIÓN"
    assert set(item["impact_bps"]) == {"1s", "5s", "30s", "300s"}


def test_property_an_inferred_series_cannot_be_published_without_confidence():
    from app.core import series_metadata as S

    with pytest.raises(S.SeriesContractError):
        S.SeriesMeta("HEDGE_PRESSURE", "DEX_NOTIONAL", "ITM", S.TYPE_INFERRED, "m", 0.0, None)
    with pytest.raises(S.SeriesContractError):
        S.SeriesMeta("GEX", "GEX_PER_1PCT", "ITM", S.TYPE_DERIVED, None, 0.0, 0.9)

    ok = S.bundle([S.observed("PRICE", source="ALPACA"),
                   S.derived("GEX", model="BSM_DISPATCHER", unit="GEX_PER_1PCT")])
    assert ok["by_type"] == {"OBSERVED": 1, "DERIVED": 1}


# ══════════════════════════════════════════════ P3 · auditor

def test_economic_a_crossed_quote_degrades_data_and_the_decision():
    """El Scanner puede estar fresco y estar leyendo una cadena rota."""
    from app.core import model_risk_auditor as A

    chain = pd.DataFrame({
        "contract_symbol": ["DIA260116C00450000", "DIA260116P00450000"],
        "bid": [2.0, 1.8], "ask": [1.0, 2.0],       # la primera está cruzada
        "strike": [450.0, 450.0], "expiration_date": ["2026-01-16"] * 2,
        "open_interest": [100, 80], "timestamp": [pd.Timestamp.now(tz="UTC")] * 2})
    out = A.audit(chain=chain, symbol="DIA", scanner={"input_age_seconds": 5},
                  ev={"costs": {"fee": 0.65}}, flow={"non_causal_quotes": 0,
                                                     "classified_pct": 70.0})
    assert out["headline"]["DATA_QUALITY"] == "DEGRADED"
    assert out["headline"]["DECISION_QUALITY"] != "ACCIONABLE"


def test_property_a_replay_leaking_the_future_is_a_critical_failure():
    from app.core import model_risk_auditor as A

    findings = A.audit_replay({"active": True, "cutoff": "2026-09-18T10:00:00",
                               "latest_event": "2026-09-18T10:05:00"})
    assert findings[0].passed is False
    assert findings[0].severity == A.SEVERITY_CRITICAL
    assert "mirar" in findings[0].detail


def test_unit_ev_without_costs_is_a_critical_decision_failure():
    from app.core import model_risk_auditor as A

    findings = A.audit_decision({"input_age_seconds": 5}, {"expectancy": 12.0}, None, None)
    ev = [f for f in findings if f.area == A.AREA_EV][0]
    assert ev.passed is False and ev.severity == A.SEVERITY_CRITICAL


# ══════════════════════════════════════════════ P0-1/P0-5 · universo e identidad

def test_property_a_symbol_without_a_provider_chain_is_never_promised():
    """La causa raíz de `CADENA_NO_HIDRATADA`: prometer lo que Alpaca no sirve."""
    from app.core import assets as A

    con = {"kind": "ETF", "dynamic": True, "alpaca_has_options": True, "quant_provider": "ALPACA"}
    sin = {"kind": "ETF", "dynamic": True, "alpaca_has_options": False, "quant_provider": "ALPACA"}
    assert A.resolve_quant_provider(con)[0] == "ALPACA"
    assert A.resolve_quant_provider(sin)[0] == "QUANTDATA"


def test_unit_equities_and_etfs_register_through_the_same_path():
    from app.core import assets as A

    n = A.register_provider_assets([
        {"symbol": "ZZEQ", "name": "Test Equity", "asset_class": "EQUITY",
         "exchange": "NASDAQ", "alpaca": True, "options_enabled": True, "sector": "Technology"},
        {"symbol": "ZZET", "name": "Test ETF", "asset_class": "ETF",
         "exchange": "ARCA", "alpaca": True, "options_enabled": False},
    ])
    try:
        assert n == 2
        assert A.ASSETS["ZZEQ"]["kind"] == "ACCION" and A.ASSETS["ZZEQ"]["full"] is True
        assert A.ASSETS["ZZET"]["kind"] == "ETF" and A.ASSETS["ZZET"]["full"] is False
        eco = A.asset_info("ZZEQ")["ecosystem"]
        assert eco["architecture"] == "UNIVERSAL EQUITY · SEPARATE INSTRUMENT MATH"
    finally:
        A.ASSETS.pop("ZZEQ", None)
        A.ASSETS.pop("ZZET", None)


def test_unit_product_identity_is_multi_asset():
    import json

    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    assert marker["scope"] == "MULTI_ASSET"
    assert marker["product_id"] == "com.itmquant.multiasset"
    persistence = (ROOT / "app" / "persistence.py").read_text(encoding="utf-8")
    # el id anterior queda como legado: un marcador v1.41 debe seguir validando
    assert '"com.itmquant.dow.specialized"' in persistence


def test_unit_architecture_endpoint_publishes_the_contracts():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        body = client.get("/api/architecture").json()
    assert body["scope"] == "MULTI_ASSET"
    assert body["pricing_dispatch"]["YM"]["option_model"] == "FUTURE_OPTION"
    assert body["pricing_dispatch"]["DJX"]["option_model"] == "INDEX_OPTION"
    assert body["units"]["comparability_checks"][0] == "same_greek"
    assert "net_drift" in body["metric_authority"]["by_provider"]["QUANTDATA"]["authority"]
