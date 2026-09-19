from __future__ import annotations

from pathlib import Path

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.sophia_core import SophiaRuntime

ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "app/service.py").read_text(encoding="utf-8")


def test_release_identity_at_least_v1340():
    assert_version_at_least("1.34.0")
    assert_marker_version_at_least("1.34.0")


def _base_state(**klr_over):
    klr = {"zero_gamma": 443.2, "call_wall": 445.0, "put_wall": 440.0, "vol_trigger": 442.0}
    klr.update(klr_over)
    return {
        "active_symbol": "DIA", "spot": 445.05, "scanner": {}, "flow_pro": {},
        "publication_gate": {"publicar_permitido": True}, "key_levels_report": klr,
    }


# ------------------------------------------------------------- parse_watch()

def test_parse_watch_recognizes_all_named_levels():
    runtime = SophiaRuntime()
    cases = {
        "avisame cuando rompa el call wall": "CALL_WALL",
        "avisame cuando el precio baje de put wall": "PUT_WALL",
        "avisame cuando supere el vol trigger": "VOL_TRIGGER",
        "avisame cuando el precio baje de zero gamma": "ZERO_GAMMA",
        "notificame si rompe el gamma flip": "ZERO_GAMMA",
    }
    for text, expected in cases.items():
        rule = runtime.parse_watch(text)
        assert rule is not None, text
        assert rule.kind == "PRICE"
        assert rule.params["level_name"] == expected, text


def test_parse_watch_named_level_relation_directions():
    runtime = SophiaRuntime()
    above = runtime.parse_watch("avisame cuando supere el call wall")
    below = runtime.parse_watch("avisame cuando baje de put wall")
    touch = runtime.parse_watch("avisame cuando toque el call wall")
    assert above.params["relation"] == "ABOVE"
    assert below.params["relation"] == "BELOW"
    assert touch.params["relation"] == "TOUCH"


def test_named_level_check_precedes_generic_gamma_delta_catch_all():
    """'zero gamma'/'gamma flip' contain the word 'gamma' and must resolve to a
    named-level PRICE watch, never fall through to the generic GAMMA_DELTA
    dominance rule."""
    runtime = SophiaRuntime()
    rule = runtime.parse_watch("avisame cuando rompa el gamma flip")
    assert rule.kind == "PRICE"
    assert rule.params["level_name"] == "ZERO_GAMMA"


def test_numeric_level_watch_still_works_unchanged():
    """Regression guard: named-level parsing must not shadow the pre-existing
    explicit-number watch rule."""
    runtime = SophiaRuntime()
    rule = runtime.parse_watch("avisame cuando el precio supere 445")
    assert rule.kind == "PRICE"
    assert rule.params.get("level_name") is None
    assert rule.params["level"] == 445.0
    assert rule.params["relation"] == "ABOVE"


# ----------------------------------------------------------------- evaluate()

def test_named_level_watch_fires_against_current_report_value():
    runtime = SophiaRuntime()
    runtime.parse_watch("avisame cuando rompa el call wall")
    events = runtime.evaluate(_base_state(), {})
    assert len(events) == 1
    assert "Call Wall" in events[0]["message"]


def test_named_level_watch_tracks_the_level_moving_session_to_session():
    """The level is resolved at EVALUATION time, not frozen when the watch was
    created -- a call wall that has since moved must be reflected immediately."""
    runtime = SophiaRuntime()
    runtime.parse_watch("avisame cuando rompa el call wall")
    # Call wall today is above spot: must not fire yet.
    quiet_state = _base_state(call_wall=450.0)
    assert runtime.evaluate(quiet_state, {}) == []
    # Same watch, call wall has since moved down to spot: must fire now.
    moved_state = _base_state(call_wall=445.0)
    events = runtime.evaluate(moved_state, {})
    assert len(events) == 1


def test_named_level_watch_never_raises_when_report_not_yet_published():
    runtime = SophiaRuntime()
    runtime.parse_watch("avisame cuando rompa el call wall")
    state = {"active_symbol": "DIA", "spot": 445.05, "scanner": {}, "flow_pro": {},
            "publication_gate": {"publicar_permitido": True}, "key_levels_report": {}}
    assert runtime.evaluate(state, {}) == []


# ------------------------------------------------------------- service wiring

def test_public_state_publishes_key_levels_report_for_watches():
    assert "key_levels_report" in SERVICE
    assert "key_levels_report(gd" in SERVICE
