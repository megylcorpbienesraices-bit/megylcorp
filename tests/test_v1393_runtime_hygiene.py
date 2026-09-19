"""v1.39.4 · Runtime hygiene and operational-package cleanup."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core import calibration, obs, terminal_metrics
from app import service

ROOT = Path(__file__).resolve().parents[1]


def test_expected_runtime_control_flow_is_not_reported_as_degradation():
    data_lake = (ROOT / "app/core/provider_data_lake.py").read_text(encoding="utf-8")
    ecosystem = (ROOT / "app/core/ecosystem_runtime.py").read_text(encoding="utf-8")
    library = (ROOT / "app/core/provider_library.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "_obs_note('provider_data_lake:280'" not in data_lake
    assert "_obs_note('ecosystem_runtime:129'" not in ecosystem
    assert "_obs_note('provider_library:85'" not in library
    assert "_obs_note('main:1360'" not in main
    assert "except queue.Empty:" in data_lake


def test_missing_optional_metrics_do_not_pollute_degradation_health():
    obs.reset_degradations()
    terminal_metrics.set_gauge("optional_none", None)
    terminal_metrics.set_gauge("optional_empty", "")
    terminal_metrics.set_gauge("optional_bad", "not-a-number")
    assert obs.degradations()["total_degradations"] == 0


def test_missing_resolution_times_are_absence_not_calibration_errors():
    obs.reset_degradations()
    frame = pd.DataFrame(
        {
            "outcome": ["OPEN", "T1", "INVALIDATION", "T2"],
            "t1_minutes": [None, 3.0, None, "4.5"],
            "invalidation_minutes": [None, None, 7.0, None],
        }
    )
    out = calibration._resolution_time_summary(frame)
    assert out["overall"]["n"] == 3
    assert obs.degradations()["total_degradations"] == 0


def test_transient_hot_chain_probe_does_not_create_false_degradation(monkeypatch):
    obs.reset_degradations()
    monkeypatch.setattr(type(service.TASTYTRADE), "configured", property(lambda self: True))
    monkeypatch.setattr(
        service,
        "_tastytrade_own_chain_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("NO_OWN_UNDERLYING_PRICE")),
    )
    expected = pd.DataFrame({"strike": [500.0], "option_type": ["call"]})
    monkeypatch.setattr(service, "fetch_alpaca_options_snapshot", lambda *a, **k: (expected.copy(), {"source": "ALPACA"}))
    frame, meta = service.fetch_asset_options_snapshot("DIA", 10, 7)
    assert len(frame) == 1 and meta["source"] == "ALPACA"
    assert not any(x["site"] == "service:hot_chain_probe" for x in obs.degradations()["sites"])


def test_orderly_shutdown_cancellation_is_not_logged_as_degraded():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    runtime = (ROOT / "app/providers/tastytrade/runtime.py").read_text(encoding="utf-8")
    dxlink = (ROOT / "app/providers/tastytrade/dxlink.py").read_text(encoding="utf-8")
    ecosystem = (ROOT / "app/core/ecosystem_runtime.py").read_text(encoding="utf-8")
    assert "except BaseException" not in main + runtime + dxlink + ecosystem
    assert "main:490" not in main
    assert "dxlink:229" not in dxlink
    assert "ecosystem_runtime:85" not in ecosystem


def test_runtime_artifact_does_not_ship_retired_release_history_or_oneoff_validators():
    for rel in (
        "docs/archive/release-history",
        "docs/archive/release_history",
        "docs/release-history",
        "docs/release_history",
    ):
        assert not (ROOT / rel).exists(), f"historial de release retirado dentro del runtime: {rel}"
    assert not (ROOT / "scripts/validate_v1270.py").exists()
    assert not (ROOT / "scripts/validate_v1272.py").exists()


def test_living_architecture_docs_are_version_neutral():
    expected = {
        "AGGRESSION_TRIGGER.md",
        "CHART_STATE_SESSION_CONTINUITY.md",
        "INSTANT_BOOT.md",
        "LIVE_VALIDATION.md",
        "PERFORMANCE_INTERACTION_CORE.md",
        "SOPHIA_SELF_HOSTED.md",
        "SOURCE_FUSION.md",
        "TRACE_RENDERER.md",
        "UNIFIED_MARKET_INTELLIGENCE.md",
    }
    docs = {p.name for p in (ROOT / "docs").glob("*.md")}
    assert expected <= docs
    assert not any("_v1." in name for name in docs)


def test_chart_cache_busting_tracks_current_app_version():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert '/static/modern_chart_system.js?v={{ app_version }}' in html
    assert '/static/modern_3d_theme.css?v={{ app_version }}' in html
