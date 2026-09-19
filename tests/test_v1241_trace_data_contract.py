from pathlib import Path

from app.core.trace_contract import normalize_nextgen_trace_contract, normalize_trace_pulse_contract
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]


def test_trace_pulse_wrong_rows_shape_fails_soft():
    out = normalize_trace_pulse_contract({"ready": True, "rows": {"strike": 537}}, source="test")
    assert out["ready"] is True
    assert out["rows"] == []
    assert out["contract"]["valid"] is False
    assert out["contract"]["violations"][0]["field"] == "rows"


def test_nextgen_collection_shapes_are_invariant():
    out = normalize_nextgen_trace_contract({
        "ready": True,
        "candles": {"bad": True},
        "option_prints": None,
        "levels": ({"name": "Gamma Flip", "price": 537.0},),
        "profiles": {"ready": True, "rows": {"bad": True}},
        "market_state": {"top_factors": {"bad": True}},
        "model_risk": {"scenarios": "BASE"},
    }, source="test")
    assert out["candles"] == []
    assert out["option_prints"] == []
    assert isinstance(out["levels"], list) and len(out["levels"]) == 1
    assert out["profiles"]["rows"] == []
    assert out["market_state"]["top_factors"] == []
    assert out["model_risk"]["scenarios"] == []
    assert out["contract"]["valid"] is False


def test_frontend_has_single_trace_contract_boundary():
    js = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    app = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    ultra = (ROOT / "app/static/ultra_charts.js").read_text(encoding="utf-8")
    assert "window.ITMQTraceContract" in js
    assert "normalizeTracePayload" in js and "normalizeTracePulse" in js
    assert "ITMQTraceContract.normalizePulse" in app
    assert "(t.y||[]).map(Number)" not in ultra
    assert "arr(t.y,'trace.y').map(Number)" in ultra


def test_version_bumped_without_persistence_reset():
    assert_version_at_least('1.26.2')
    config = (ROOT / "app/config.py").read_text(encoding="utf-8")
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in config
    assert "persistent_data" in (ROOT / "app/persistence.py").read_text(encoding="utf-8")
