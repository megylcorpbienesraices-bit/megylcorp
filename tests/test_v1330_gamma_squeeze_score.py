from __future__ import annotations

from pathlib import Path

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.market_state_field import build_market_state_field

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
JS = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
NEXTGEN = (ROOT / "app/core/nextgen_terminal.py").read_text(encoding="utf-8")


def test_release_identity_at_least_v1330():
    assert_version_at_least("1.33.0")
    assert_marker_version_at_least("1.33.0")


SCANNER = {"ready": True, "direction": "BUY", "zone": {"low": 440, "high": 442}}


def _market(**over):
    base = {"spot": 443.0, "regime": "POSITIVE GAMMA", "total_signed_gex": 5e8, "total_gross_gex": 6e8,
            "gamma_flip_crossing": True, "flip_proximity_label": "LOW",
            "flip_velocity_label": "STABLE", "flip_acceleration_label": "STABLE",
            "net_delta_exposure": 1e8}
    base.update(over)
    return base


def test_gamma_squeeze_present_in_market_state_field():
    state = build_market_state_field(SCANNER, _market(), {}, {"regime": "STABLE"}, {}, symbol="DIA")
    sq = state["gamma_squeeze"]
    assert sq["ready"] is True
    assert 0.0 <= sq["score"] <= 100.0
    assert sq["authority"] == "DESCRIPTIVE_ONLY_SCANNER_REMAINS_AUTHORITY"


def test_negative_gamma_fast_flip_scores_higher_than_calm_positive_gamma():
    high = build_market_state_field(SCANNER, _market(
        regime="NEGATIVE GAMMA", total_signed_gex=-5e8,
        flip_proximity_label="VERY HIGH", flip_velocity_label="FAST", flip_acceleration_label="ACCELERATING",
    ), {}, {"regime": "EXPANSION"}, {}, symbol="DIA")["gamma_squeeze"]
    low = build_market_state_field(SCANNER, _market(), {}, {"regime": "STABLE"}, {}, symbol="DIA")["gamma_squeeze"]
    assert high["score"] > low["score"]
    assert high["label"] in {"ACTIVE SQUEEZE RISK", "ELEVADO"}
    assert low["label"] in {"BAJO", "WATCH"}


def test_diagnostic_no_true_root_never_scores_at_full_proximity_weight():
    """A flip level with no genuine NetGEX(S)=0 crossing is a diagnostic fallback,
    not a tradable level -- the squeeze score must not treat it as confidently as
    a real dynamic root, even when its label claims VERY HIGH proximity."""
    with_root = build_market_state_field(SCANNER, _market(
        gamma_flip_crossing=True, flip_proximity_label="VERY HIGH", flip_velocity_label="FAST",
    ), {}, {"regime": "STABLE"}, {}, symbol="DIA")["gamma_squeeze"]
    no_root = build_market_state_field(SCANNER, _market(
        gamma_flip_crossing=False, flip_proximity_label="VERY HIGH", flip_velocity_label="FAST",
    ), {}, {"regime": "STABLE"}, {}, symbol="DIA")["gamma_squeeze"]
    assert no_root["components"]["flip_basis"] == "DIAGNOSTIC_NO_TRUE_ROOT"
    assert with_root["components"]["flip_basis"] == "DYNAMIC_ROOT"
    assert no_root["score"] < with_root["score"]


def test_gamma_squeeze_never_raises_on_missing_fields():
    state = build_market_state_field({}, {}, {}, {}, {}, symbol="DIA")
    sq = state["gamma_squeeze"]
    assert sq["ready"] is True
    assert 0.0 <= sq["score"] <= 100.0


# ------------------------------------------------------- payload/frontend wiring

def test_nextgen_payload_forwards_gamma_squeeze():
    assert '"gamma_squeeze"' in NEXTGEN


def test_dashboard_and_js_render_squeeze_hud():
    assert 'id="traceHudSqueeze"' in HTML
    assert "traceHudSqueeze" in JS and "gamma_squeeze" in JS
