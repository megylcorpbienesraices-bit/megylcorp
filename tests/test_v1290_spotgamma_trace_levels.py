from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.nextgen_terminal import structure_levels, hiro_series

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
CONTRACT = (ROOT / "app/core/trace_contract.py").read_text(encoding="utf-8")


def test_release_identity_at_least_v1290():
    assert_version_at_least("1.29.0")
    assert_marker_version_at_least("1.29.0")


# --------------------------------------------------------------- fixtures

def _curagg() -> pd.DataFrame:
    # Strikes deliberately produce a real sign crossing in cumulative signed
    # gamma so Vol Trigger is exercised, not just Call/Put Wall.
    return pd.DataFrame({
        "strike":     [440.0, 441.0, 442.0, 443.0, 444.0, 445.0, 446.0],
        "gross_gex":  [1.0e6, 2.0e6, 3.0e6, 1.0e6, 9.0e6, 4.0e6, 1.5e6],
        "signed_gex": [1.0e6, 2.0e6, 3.0e6, -1.0e6, -9.0e6, 4.0e6, 1.5e6],
    })


def _gd(spot: float = 443.0) -> dict:
    return {"gamma_flip": 443.2, "gamma_center": 443.5, "delta_center": 444.0, "spot": spot}


def _scanner() -> dict:
    return {"zone": {"low": 441.0, "high": 445.0}, "target1": 446.0, "target2": 448.0, "invalidation": 440.0}


# ------------------------------------------------- structure_levels() contract

def test_structure_levels_without_curagg_keeps_legacy_shape():
    """No aggregate/spot supplied -> behaves exactly like before this release."""
    levels = structure_levels(_gd(), _scanner())
    kinds = {lv["kind"] for lv in levels}
    assert kinds == {"flip", "gamma", "delta", "zone", "target", "risk"}
    flip = next(lv for lv in levels if lv["kind"] == "flip")
    assert flip["name"] == "Zero Gamma", "Gamma Flip level must display SpotGamma's own naming"
    assert flip["price"] == pytest.approx(443.2)


def test_structure_levels_adds_spotgamma_walls_when_aggregate_available():
    levels = structure_levels(_gd(), _scanner(), curagg=_curagg(), spot=443.0)
    by_kind = {lv["kind"]: lv for lv in levels}
    for kind, name in (("call_wall", "Call Wall"), ("put_wall", "Put Wall"),
                       ("vol_trigger", "Vol Trigger")):
        assert kind in by_kind, f"missing {kind} level when structural aggregate is available"
        assert by_kind[kind]["name"] == name
        assert isinstance(by_kind[kind]["price"], float)


def test_hedge_wall_is_published_when_it_is_a_level_of_its_own():
    """Con gamma positiva concentrada por debajo del Call Wall, el Hedge Wall es una
    observación distinta y se publica con su etiqueta propia."""
    from app.core.trace_analytics import structural_walls
    # 440 es el más negativo bajo el spot -> Put Wall.
    # 441 es el pico de gamma POSITIVA -> Hedge Wall, distinto de los dos muros.
    # 445 es el mayor sobre el spot -> Call Wall.
    curagg = pd.DataFrame({
        "strike":     [440.0, 441.0, 445.0, 446.0],
        "gross_gex":  [5.0e6, 8.0e6, 3.0e6, 1.0e6],
        "signed_gex": [-5.0e6, 8.0e6, 3.0e6, 1.0e6],
    })
    w = structural_walls(curagg, 443.0)
    assert w["put_wall"] == 440.0 and w["hedge_wall"] == 441.0 and w["call_wall"] == 445.0
    assert w["hedge_wall_coincides_with"] is None
    by_kind = {lv["kind"]: lv for lv in structure_levels(_gd(), _scanner(), curagg=curagg, spot=443.0)}
    assert "hedge_wall" in by_kind
    # Es un proxy propio de ITM y su etiqueta tiene que decirlo, nunca leerse como
    # una métrica nativa de SpotGamma.
    assert "ITM" in by_kind["hedge_wall"]["name"]


def test_hedge_wall_is_not_drawn_twice_when_it_lands_on_the_call_wall():
    """El Hedge Wall es el pico de gamma positiva; en una cadena dominada por calls cae
    en el mismo strike que el Call Wall. Publicarlo igual apilaba dos etiquetas sobre
    la misma línea y las presentaba como dos evidencias independientes.
    """
    from app.core.trace_analytics import structural_walls
    curagg = pd.DataFrame({
        "strike":     [440.0, 441.0, 445.0, 446.0],
        "gross_gex":  [1.0e6, 2.0e6, 9.0e6, 1.0e6],
        "signed_gex": [-1.0e6, -2.0e6, 9.0e6, 1.0e6],
    })
    w = structural_walls(curagg, 443.0)
    assert w["call_wall"] == w["hedge_wall"] == 445.0
    assert w["hedge_wall_coincides_with"] == "call_wall"
    kinds = {lv["kind"] for lv in structure_levels(_gd(), _scanner(), curagg=curagg, spot=443.0)}
    assert "call_wall" in kinds and "hedge_wall" not in kinds


def test_structure_levels_never_raises_on_bad_aggregate():
    """A malformed/empty aggregate degrades to the legacy level set, never crashes."""
    levels = structure_levels(_gd(), _scanner(), curagg=pd.DataFrame(), spot=443.0)
    assert {lv["kind"] for lv in levels} == {"flip", "gamma", "delta", "zone", "target", "risk"}
    levels2 = structure_levels(_gd(), _scanner(), curagg=None, spot=None)
    assert {lv["kind"] for lv in levels2} == {"flip", "gamma", "delta", "zone", "target", "risk"}


# --------------------------------------------------------------- hiro_series()

def test_hiro_series_empty_without_flow_events():
    out = hiro_series(None)
    assert out["ready"] is False
    assert out["points"] == []
    out2 = hiro_series(pd.DataFrame())
    assert out2["ready"] is False


def test_hiro_series_schema_and_disclosure_when_ready():
    events = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-14 09:30", periods=6, freq="1min"),
        "aggressor": ["BUY", "SELL", "BUY", "BUY", "SELL", "BUY"],
        "aggressor_confidence": [0.8] * 6,
        "contracts": [10, 5, 8, 20, 3, 12],
        "delta": [0.5, -0.4, 0.3, 0.6, -0.2, 0.55],
        "option_type": ["call", "put", "call", "call", "put", "call"],
    })
    out = hiro_series(events, timeframe_minutes=1)
    assert out["label"] == "HIRO (ITM)"
    assert out["is_estimate"] is True
    assert "no es inventario" in out["model_risk"].lower() or "inventario de dealer" in out["model_risk"].lower()
    if out["ready"]:
        assert isinstance(out["points"], list)
        for p in out["points"]:
            assert {"t", "hedge_notional", "cumulative", "events"}.issubset(p.keys())


def test_hiro_series_never_raises_on_malformed_events():
    bad = pd.DataFrame({"garbage": [1, 2, 3]})
    out = hiro_series(bad)
    assert out["ready"] is False
    assert out["points"] == []


# ------------------------------------------------------- frontend wiring checks

def test_nextgen_js_has_spotgamma_level_colors():
    assert "call_wall:'#22c55e'" in JS or "call_wall:\"#22c55e\"" in JS
    assert "put_wall:'#ef4444'" in JS or "put_wall:\"#ef4444\"" in JS
    assert "vol_trigger:" in JS
    assert "hedge_wall:" in JS


def test_nextgen_js_has_hiro_panel():
    assert "drawHiro(" in JS
    assert "HIRO (ITM)" in JS
    assert "hiroOn" in JS


def test_trace_contract_validates_hiro_field():
    assert '"hiro"' in CONTRACT or "'hiro'" in CONTRACT
    assert "hiro.points" in CONTRACT
