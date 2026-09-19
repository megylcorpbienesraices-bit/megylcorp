from __future__ import annotations

"""v1.42.3 · El muro se mide en el strike, no en el spot de ahora.

Estos tests siembran un muro DELIBERADO en la cadena y comprueban que el motor lo
encuentra. Es la única forma honesta de validar un detector: darle una respuesta
conocida y ver si la saca. Un test que sólo compruebe que devuelve "algún número"
habría pasado durante todo el tiempo que los dos muros salían pegados al ATM.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.engine import analyze_gamma_delta, engine_config_for_asset
from app.core.nextgen_terminal import key_levels_report, structure_levels
from app.core.trace_analytics import structural_walls

SPOT = 600.0
CALL_WALL = 612.0
PUT_WALL = 585.0


def _chain(dte: float = 1.0, wall_mult: float = 6.0) -> pd.DataFrame:
    """Cadena con fondo plano, pico ATM moderado y un muro real en 612/585.

    `wall_mult` es múltiplo del OI ATM, que es como se forman los muros de verdad:
    concentración en strikes redondos, no un pico suave alrededor del dinero.
    """
    t0 = datetime(2026, 9, 17, 10, 0, 0)
    atm_oi = 3000.0
    rows = []
    for K in np.arange(564.0, 637.0, 1.0):
        m = np.log(K / SPOT)
        iv = float(np.clip(0.16 + 0.55 * m * m - 0.45 * m, 0.05, 1.5))
        for ot in ("call", "put"):
            oi = 800.0 + atm_oi * np.exp(-((K - SPOT) / (SPOT * 0.010)) ** 2)
            if ot == "call" and abs(K - CALL_WALL) < 1e-9:
                oi = atm_oi * wall_mult
            if ot == "put" and abs(K - PUT_WALL) < 1e-9:
                oi = atm_oi * wall_mult
            rows.append({
                "timestamp": t0, "underlying_symbol": "SPY", "underlying_price": SPOT,
                "strike": float(K), "option_type": ot, "dte": dte,
                "expiration_date": (t0 + timedelta(days=dte)).date().isoformat(),
                "open_interest": float(oi), "volume": float(oi * 0.2), "iv": iv,
                "contract_multiplier": 100.0,
            })
    return pd.DataFrame(rows)


def _gd(dte: float, wall_mult: float = 6.0) -> dict:
    return analyze_gamma_delta(_chain(dte, wall_mult), engine_config_for_asset("SPY", "AUTO"))


@pytest.mark.parametrize("dte", [1.0, 3.0, 7.0, 30.0])
def test_walls_find_the_seeded_concentration_at_every_horizon(dte):
    """El defecto: la gamma al spot es máxima en el dinero, así que a DTE corto el
    argmax se iba al ATM y enterraba el muro. Medido antes: a 1 DTE hacían falta 40x
    el OI del ATM para ver el Call Wall, y el Put Wall no aparecía nunca.
    """
    gd = _gd(dte)
    w = structural_walls(gd["current"], SPOT, enriched=gd["enriched"])
    assert w["call_wall"] == CALL_WALL, f"DTE {dte}: call_wall={w['call_wall']}"
    assert w["put_wall"] == PUT_WALL, f"DTE {dte}: put_wall={w['put_wall']}"
    assert w["method"] == "barrier-gamma-at-strike"


def test_walls_are_not_just_the_atm_strike_reported_twice():
    """La firma exacta del fallo antiguo: los dos muros a menos de 0.25 sigmas del
    spot, uno a cada lado. Eso no es estructura, es el ATM dicho dos veces."""
    gd = _gd(1.0)
    w = structural_walls(gd["current"], SPOT, enriched=gd["enriched"])
    sigma_1d = SPOT * 0.16 * np.sqrt(1.0 / 365.0)
    assert abs(w["call_wall"] - SPOT) / sigma_1d > 1.0
    assert abs(w["put_wall"] - SPOT) / sigma_1d > 1.0


def test_walls_stay_on_their_own_side_of_spot():
    gd = _gd(1.0)
    w = structural_walls(gd["current"], SPOT, enriched=gd["enriched"])
    assert w["call_wall"] > SPOT > w["put_wall"]


def test_sections_do_not_contradict_each_other_on_the_walls():
    """key_levels_report y structure_levels alimentan paneles distintos de la misma
    pantalla. Si divergen, el operador ve dos Call Wall diferentes del mismo mercado.
    """
    gd = _gd(1.0)
    cur = gd["current"]
    kl = key_levels_report(gd, {}, curagg=cur, spot=SPOT, symbol="SPY")
    lv = {l["kind"]: l["price"] for l in structure_levels(gd, {}, curagg=cur, spot=SPOT)
          if l.get("kind") in ("call_wall", "put_wall")}
    assert kl["call_wall"] == lv["call_wall"] == CALL_WALL
    assert kl["put_wall"] == lv["put_wall"] == PUT_WALL


def test_without_the_chain_the_previous_method_still_works_and_declares_itself():
    """Sin cadena no se inventa un muro de barrera: se mantiene el método anterior
    y queda declarado, igual que hace el resto del motor cuando degrada."""
    gd = _gd(1.0)
    w = structural_walls(gd["current"], SPOT)
    assert w["method"].startswith("gamma-weighted")
    assert w["call_wall"] is not None and w["put_wall"] is not None


def test_the_previous_value_stays_published_as_a_diagnostic():
    """Cambiar una definición sin dejar comparar la anterior es pedir fe. El valor
    viejo sigue publicado para poder auditar la diferencia."""
    gd = _gd(1.0)
    w = structural_walls(gd["current"], SPOT, enriched=gd["enriched"])
    assert "call_wall_gamma_at_spot" in w and "put_wall_gamma_at_spot" in w
    # y en esta cadena la diferencia es justo la que motivó el cambio
    assert w["call_wall_gamma_at_spot"] != w["call_wall"]
