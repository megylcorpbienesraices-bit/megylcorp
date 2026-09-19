from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.nextgen_terminal import key_levels_report
from app.core.trace_analytics import max_pain, atm_iv_and_dte, structural_walls, expected_move_band_from_chain
from app.core.premarket_intelligence import _max_pain as premarket_max_pain
from app.core.expiry_clock import year_fraction

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
JS = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")


def test_release_identity_at_least_v1310():
    assert_version_at_least("1.31.0")
    assert_marker_version_at_least("1.31.0")


# --------------------------------------------------------------- fixtures

def _curagg() -> pd.DataFrame:
    return pd.DataFrame({
        "strike":     [440.0, 441.0, 442.0, 443.0, 444.0, 445.0, 446.0],
        "gross_gex":  [1.0e6, 2.0e6, 3.0e6, 1.0e6, 9.0e6, 4.0e6, 1.5e6],
        "signed_gex": [1.0e6, 2.0e6, 3.0e6, -1.0e6, -9.0e6, 4.0e6, 1.5e6],
    })


def _chain() -> pd.DataFrame:
    strikes = [440, 441, 442, 443, 444, 445, 446]
    rows = []
    for k in strikes:
        for ot, oi in (("call", 100), ("put", 120)):
            rows.append(dict(timestamp=pd.Timestamp("2026-09-14 10:00"), strike=k, iv=0.20, dte=5.0,
                             open_interest=oi, option_type=ot))
    return pd.DataFrame(rows)


def _gd() -> dict:
    return {"gamma_flip": 443.2, "gamma_center": 443.5, "spot": 443.5, "enriched": _chain()}


# ------------------------------------------------------- max_pain single source

def test_premarket_and_trace_analytics_share_one_max_pain_implementation():
    """premarket_intelligence._max_pain must delegate, never reimplement."""
    chain = _chain()
    assert premarket_max_pain(chain) == max_pain(chain)
    assert premarket_max_pain is max_pain


# --------------------------------------------------------- key_levels_report()

def test_key_levels_report_matches_structural_walls_exactly():
    """key_levels_report debe DELEGAR en structural_walls, nunca reimplementarlo.

    La comparación usa los mismos insumos que el informe: desde v1.42.3 los muros se
    calculan sobre la cadena (gamma evaluada en S=K), así que pasarle sólo el
    agregado compararía dos cálculos distintos y el test dejaría de medir lo que
    dice medir.
    """
    curagg, spot = _curagg(), 443.5
    gd = _gd()
    walls = structural_walls(curagg, spot, enriched=gd["enriched"])
    report = key_levels_report(gd, {}, curagg=curagg, spot=spot, symbol="DIA")
    assert report["ready"] is True
    assert report["call_wall"] == walls["call_wall"]
    assert report["put_wall"] == walls["put_wall"]
    assert report["vol_trigger"] == walls["volatility_trigger"]
    assert report["hedge_wall"] == walls["hedge_wall"]


def test_key_levels_report_max_pain_matches_shared_function():
    report = key_levels_report(_gd(), {}, curagg=_curagg(), spot=443.5, symbol="DIA")
    assert report["max_pain"] == max_pain(_chain())


def test_key_levels_report_expected_move_uses_same_t_convention_as_engine():
    """Must use year_fraction (same T as every Greek in the engine), not a
    different intraday trading-minutes clock -- those answer a different
    question and mixing them silently misprices the band."""
    report = key_levels_report(_gd(), {}, curagg=_curagg(), spot=443.5, symbol="DIA")
    atm_iv, dte_days = atm_iv_and_dte(_chain(), 443.5)
    expected = 443.5 * (atm_iv / 100.0) * math.sqrt(year_fraction(dte_days))
    assert report["expected_move"] == pytest.approx(expected, rel=1e-9)
    assert report["expected_low"] == pytest.approx(443.5 - expected, rel=1e-9)
    assert report["expected_high"] == pytest.approx(443.5 + expected, rel=1e-9)


def test_key_levels_report_degrades_without_raising():
    report = key_levels_report({}, {}, curagg=None, spot=None, symbol="DIA")
    assert report["ready"] is False
    report2 = key_levels_report({"spot": 443.5}, {}, curagg=pd.DataFrame(), spot=443.5, symbol="DIA")
    assert report2["ready"] is True
    assert report2["call_wall"] is None and report2["max_pain"] is None


# ------------------------------------------------------- frontend wiring checks

def test_dashboard_html_has_key_levels_hud():
    for hud_id in ("traceHudCallWall", "traceHudPutWall", "traceHudVolTrigger",
                  "traceHudMaxPain", "traceHudExpectedMove"):
        assert f'id="{hud_id}"' in HTML


def test_nextgen_js_reads_key_levels_report_into_hud():
    assert "key_levels_report" in JS
    assert "traceHudCallWall" in JS and "traceHudMaxPain" in JS and "traceHudExpectedMove" in JS
