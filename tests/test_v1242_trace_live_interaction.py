from pathlib import Path

import pandas as pd

from app.core.engine import EngineConfig, enrich_options
from app.core.trace_contract import normalize_trace_pulse_contract
from app.core.trace_live import build_trace_pulse
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]


def _chain():
    ts = pd.Timestamp("2026-09-08 10:00:00")
    rows = []
    for strike, typ, oi, vol, iv in [
        (99, "call", 1200, 210, .24), (99, "put", 600, 140, .25),
        (100, "call", 2600, 480, .22), (100, "put", 900, 260, .23),
        (101, "call", 1800, 370, .23), (101, "put", 1400, 330, .24),
        (102, "call", 800, 110, .25), (102, "put", 1900, 410, .26),
    ]:
        rows.append({
            "timestamp": ts,
            "underlying_price": 100.0,
            "strike": float(strike),
            "dte": 5.0,
            "option_type": typ,
            "open_interest": float(oi),
            "volume": float(vol),
            "iv": float(iv),
        })
    return enrich_options(pd.DataFrame(rows), EngineConfig(symbol="DIA"))


def test_trace_pulse_exposes_same_strike_gamma_delta_interaction_without_direction_authority():
    enriched = _chain()
    events = pd.DataFrame([
        {
            "timestamp": pd.Timestamp("2026-09-08 10:01:00"),
            "underlying_symbol": "DIA",
            "strike": 100.0,
            "contracts": 75.0,
            "premium": 185000.0,
            "direction_sign": 1,
            "directional_premium": 185000.0,
        }
    ])
    pulse = build_trace_pulse(
        {"enriched": enriched}, "DIA", 101.0, events,
        asof=pd.Timestamp("2026-09-08 10:02:00"), visual_window=5,
    )
    assert pulse["ready"] is True
    interaction = pulse["gamma_delta_interaction"]
    assert interaction["label"] in {"COHERENT", "OPPOSED", "MIXED"}
    assert -100.0 <= interaction["coherence_index"] <= 100.0
    assert 0.0 <= interaction["active_score"] <= 100.0
    assert interaction["active_strike"] in {99.0, 100.0, 101.0, 102.0}
    assert interaction["authority"] == "DESCRIPTIVE_ONLY_SCANNER_REMAINS_AUTHORITY"
    for row in pulse["rows"]:
        assert row["gamma_delta_state"] in {"SAME_SIGN_POS", "SAME_SIGN_NEG", "OPPOSED", "NEUTRAL"}
        assert 0.0 <= row["gamma_delta_joint_score"] <= 100.0


def test_trace_contract_fails_soft_if_interaction_shape_is_wrong():
    out = normalize_trace_pulse_contract({
        "ready": True,
        "rows": [],
        "gamma_delta_interaction": ["bad"],
    }, source="test")
    assert out["gamma_delta_interaction"] == {}
    assert out["contract"]["valid"] is False
    assert any(v["field"] == "gamma_delta_interaction" for v in out["contract"]["violations"])


def test_trace_renderer_has_real_pulse_interpolation_structure_mass_and_flow_halo():
    js = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    for token in (
        "applyProfileUpdate", "profileRowsForRender", "requestProfileFrame",
        "gamma_delta_joint_score", "drawInteractionHUD", "TRACE_PROFILE_CLICK",
        "GRIS=OI", "HALO=OPRA NUEVO", "Motion caps encode actual change",
    ):
        assert token in js
    # Structural OI/official volume must remain a distinct, non-animated layer.
    assert "Structural mass is intentionally static between chain snapshots" in js
    assert "out.gamma_m=lerp" in js and "out.delta_m=lerp" in js
    assert "out.oi=lerp" not in js and "out.volume_snapshot=lerp" not in js


def test_trace_ui_explains_live_motion_without_claiming_new_oi():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    app = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert "CAP cian" in html and "HALO = actividad nueva de opciones observada" in html
    assert "OI y volumen oficial no se animan entre snapshots" in html
    assert "pulse ${gPulse" in app and "pulse ${dPulse" in app
    assert "NEW +${intfmt(opraDelta)}" in app


def test_v1242_version_is_consistent():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in (ROOT / "app/config.py").read_text(encoding="utf-8")
