"""v1.15.6 · Command Center is a Scanner translator, not a second signal engine."""
from app.service import _command_center


def _result():
    return {
        "spot": 600.0,
        "gamma_flip": 599.0,
        "regime": "POSITIVE GAMMA",
        # Deliberately bearish/mixed context. It must NOT flip the headline.
        "pressure_direction": "SELL",
        "pressure_score": 95.0,
        "delta_pressure_direction": "SELL",
        "delta_pressure_score": 90.0,
    }


def _targets():
    return {"active": {"state": "CONTAINMENT", "confidence": 77}}


def test_scanner_is_the_only_direction_authority_even_when_context_opposes():
    scanner = {
        "ready": True,
        "direction": "BUY",
        "zone": {"low": 599.8, "high": 600.2, "center": 600.0},
        "edge_state": "ACTIONABLE",
        "edge_gate": {"active": False, "calibration_ready": False, "mode": "EVIDENCE THRESHOLD · COLLECTING"},
    }
    cmd = _command_center(_result(), {"regime": "SELL", "confidence": 100},
                          {"regime": "EXPANSION", "expected_move": 2.0}, _targets(), scanner, {})
    assert cmd["bias"] == "BUY"
    assert cmd["direction_source"] == "SCANNER"
    assert cmd["bias_score"] is None
    assert cmd["actionability_source"] == "EVIDENCE THRESHOLD (LEGACY)"


def test_expected_value_label_only_when_gate_is_actually_active():
    scanner = {
        "ready": True, "direction": "SELL", "zone": {"low": 600.5, "high": 600.9},
        "edge_state": "CAUTION",
        "edge_gate": {"active": True, "calibration_ready": True, "probability_t1_first": .61,
                      "expected_value_r": .12, "mode": "EXPECTED VALUE · ACTIVE"},
    }
    cmd = _command_center(_result(), {}, {"regime": "STABLE", "expected_move": 2.0}, _targets(), scanner, {})
    assert cmd["bias"] == "SELL"
    assert cmd["actionability"] == "CAUTION · EXPECTED VALUE"
    assert cmd["probability_t1_first"] == .61
    assert cmd["probability_status"] == "CALIBRATED"


def test_shadow_ev_does_not_masquerade_as_production_ev_gate():
    scanner = {
        "ready": True, "direction": "BUY", "zone": {"low": 599.8, "high": 600.2},
        "edge_state": "ACTIONABLE",
        "edge_gate": {"active": False, "calibration_ready": True, "probability_t1_first": .64,
                      "expected_value_r": .20, "mode": "EXPECTED VALUE · CALIBRATED · SHADOW"},
    }
    cmd = _command_center(_result(), {}, {"regime": "STABLE", "expected_move": 2.0}, _targets(), scanner, {})
    assert cmd["actionability_source"] == "EVIDENCE THRESHOLD (LEGACY)"
    assert cmd["expected_value_r"] == .20  # visible research context, not the production gate


def test_tape_is_timing_only_and_is_exposed_without_changing_scanner():
    scanner = {"ready": True, "direction": "BUY", "zone": {"low": 599.8, "high": 600.2},
               "edge_state": "ACTIONABLE", "edge_gate": {"active": False, "calibration_ready": False}}
    tape = {"confirmation": {"state": "ARMED", "progress_pct": 54, "seconds_remaining": 150}}
    cmd = _command_center(_result(), {}, {"regime": "STABLE", "expected_move": 2.0}, _targets(), scanner, tape)
    assert cmd["bias"] == "BUY"
    assert cmd["tape_state"] == "ARMED"
    assert cmd["tape_progress_pct"] == 54
    assert cmd["tape_role"] == "TIMING_ONLY"


def test_flip_is_normalized_context_not_a_direction_vote():
    scanner = {"ready": True, "direction": "SELL", "zone": {"low": 600.2, "high": 600.6},
               "edge_state": "CAUTION", "edge_gate": {"active": False, "calibration_ready": False}}
    cmd = _command_center(_result(), {}, {"regime": "STABLE", "expected_move": 2.0}, _targets(), scanner, {})
    assert cmd["bias"] == "SELL"  # spot is above flip, but Scanner still owns direction
    assert cmd["flip_distance"] == 1.0
    assert cmd["flip_distance_expected_move"] == .5
    assert "ABOVE FLIP" in cmd["flip_context"]


def test_no_scanner_means_waiting_not_independent_bias():
    cmd = _command_center(_result(), {"regime": "BUY", "confidence": 100},
                          {"regime": "EXPANSION", "expected_move": 2.0}, _targets(), {}, {})
    assert cmd["bias"] == "WAITING"
    assert cmd["actionability"] == "WAITING"
