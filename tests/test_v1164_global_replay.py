from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.core.replay import ReplayContext, filter_asof, load_tape, persist_tape, session_coverage


def test_tape_archive_preserves_legitimate_same_prints_when_sequence_differs(tmp_path: Path):
    day = date(2026, 9, 7)
    ticks = pd.DataFrame([
        {"seq": 101, "timestamp": "2026-09-07T10:00:00", "price": 600.0, "size": 100, "exchange": "P", "conditions": [], "tape": "A"},
        {"seq": 102, "timestamp": "2026-09-07T10:00:00", "price": 600.0, "size": 100, "exchange": "P", "conditions": [], "tape": "A"},
    ])
    r1 = persist_tape(tmp_path, "SPY", ticks, day)
    r2 = persist_tape(tmp_path, "SPY", ticks, day)
    out = load_tape(tmp_path, "SPY", day)
    assert r1["dedupe_key"] == "timestamp+seq"
    assert len(out) == 2
    assert set(out["seq"].astype(int)) == {101, 102}
    assert r2["new_rows"] == 0


def test_filter_asof_is_inclusive_and_causal_for_any_event_family():
    x = pd.DataFrame({
        "timestamp": ["2026-09-07T10:41:59", "2026-09-07T10:42:00", "2026-09-07T10:42:01"],
        "kind": ["flow", "large_print", "options_structure"],
    })
    y = filter_asof(x, "2026-09-07T10:42:00")
    assert y["kind"].tolist() == ["flow", "large_print"]
    assert pd.to_datetime(y["timestamp"]).max() == pd.Timestamp("2026-09-07T10:42:00")


def test_replay_context_is_one_global_clock():
    r = ReplayContext.at("2026-09-07", "2026-09-07T10:42:00", "QQQ")
    d = r.describe()
    assert d["mode"] == "REPLAY"
    assert d["date"] == "2026-09-07"
    assert d["symbol"] == "QQQ"
    assert d["causal"] is True
    assert "10:42:00" in d["banner"]


def _write_history(path: Path, symbol: str, day: str, timestamps: list[pd.Timestamp]):
    pd.DataFrame({"timestamp": timestamps}).to_csv(path / f"alpaca_{symbol.lower()}_history_{day}.csv", index=False)


def test_session_coverage_is_temporal_not_snapshot_count(tmp_path: Path):
    day = "2026-09-07"
    # Few points spanning most of the equity session: coverage is about time, not row count.
    wide = list(pd.date_range(f"{day} 09:30:00", f"{day} 15:20:00", periods=25))
    _write_history(tmp_path, "SPY", day, wide)
    r = session_coverage(tmp_path, "SPY", [day])[0]
    assert r["snapshots"] == 25
    assert r["coverage_pct"] >= 80
    assert r["coverage_class"] == "FULL"
    assert r["usable"] is True

    day2 = "2026-09-08"
    dense_short = list(pd.date_range(f"{day2} 09:30:00", f"{day2} 09:42:00", periods=200))
    _write_history(tmp_path, "SPY", day2, dense_short)
    r2 = session_coverage(tmp_path, "SPY", [day2])[0]
    assert r2["snapshots"] == 200
    assert r2["coverage_class"] == "INSUFFICIENT"
    assert r2["usable"] is False


def test_service_replay_applies_no_future_before_external_overlays_and_disables_current_model():
    src = Path("app/service.py").read_text(encoding="utf-8")
    assert 'load_frame(self.symbol,ctx.day,"flow",asof=ctx.asof)' in src
    assert 'load_frame(self.symbol,ctx.day,"large_prints",asof=ctx.asof)' in src
    assert '"native_options_structure":build_native_options_structure(b.get("gd") or {})' in src
    assert "HISTORICAL MODEL SNAPSHOT UNAVAILABLE" in src
    assert '"macro":{"status":"UNAVAILABLE_IN_CAUSAL_REPLAY"' not in src  # built as an explicit object, never current macro
    assert 'macro={"status":"UNAVAILABLE_IN_CAUSAL_REPLAY"' in src


def test_backtest_readiness_is_separated_from_sample_coverage():
    src = Path("app/core/replay.py").read_text(encoding="utf-8")
    assert '"sample_coverage"' in src
    assert '"current_calibration_model"' in src
    assert '"signals_required": min_signals' in src
    assert "calibration_ready" not in src


def test_replay_ui_is_global_not_trace_local_and_stops_live_mutation():
    html = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    js = Path("app/static/app.js").read_text(encoding="utf-8")
    assert 'id="globalReplayBar"' in html
    assert 'id="traceDate"' in html
    assert "Fecha / Replay" not in html
    assert "CONTEXTO TEMPORAL GLOBAL · TODA LA PLATAFORMA" in html
    assert "if(lastState?.replay?.mode&&lastState.replay.mode!=='LIVE')return;" in js
    assert "trace_date=${encodeURIComponent(td)}" not in js
    assert "applyReplaySession" in js
    assert "requestResearchRange" in js


def test_replay_does_not_create_a_nineteenth_navigation_section():
    html = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert html.count('class="nav-btn') == 11  # Replay is global; v1.37 groups related analytical workspaces
    assert 'data-section="replay"' not in html
    assert 'data-section="backtest"' not in html
    assert 'id="section-backtest"' not in html
