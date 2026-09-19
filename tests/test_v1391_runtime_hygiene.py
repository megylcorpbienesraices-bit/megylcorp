"""v1.39.4 · persistence quarantine and runtime warning hygiene."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import warnings

import pandas as pd

from app import persistence
from app.core import alpaca_data, calibration, live_price, option_stream, replay

NY = ZoneInfo("America/New_York")


def test_quarantine_is_first_class_persistent_category(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(persistence, "PERSISTENT_ROOT", tmp_path)
    q = persistence.category_dir("quarantine")
    assert q == tmp_path / "quarantine"
    assert q.is_dir()
    assert persistence._classify("calibration_csv_scanner_history_dia_deadbeef.jsonl") == "quarantine"


def test_calibration_quarantine_works_on_canonical_persistent_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(persistence, "PERSISTENT_ROOT", tmp_path)
    src = tmp_path / "scanner_history_dia_2026-09-14.csv"
    src.write_text(
        "timestamp,direction,evidence_score\n"
        "2026-09-14T13:30:00Z,BUY,72\n"
        "2026-09-14T13:31:00Z,SELL,65,EXTRA\n",
        encoding="utf-8",
    )
    calibration._CALIBRATION_RECOVERY_CACHE.clear()
    frame, meta = calibration._read_scanner_history_csv(src, tmp_path, "DIA")
    assert len(frame) == 1
    assert meta["quarantined_rows"] == 1
    q = Path(meta["quarantine_file"])
    assert q.parent == tmp_path / "quarantine"
    assert q.exists()


def test_alpaca_history_reads_disable_chunked_dtype_inference(tmp_path: Path, monkeypatch):
    p = tmp_path / "history.csv"
    p.write_text("timestamp,a,b\n2026-09-14T13:30:00Z,1,x\n", encoding="utf-8")
    monkeypatch.setattr(alpaca_data, "history_path", lambda *a, **k: p)
    calls = []
    original = pd.read_csv
    def spy(*args, **kwargs):
        calls.append(dict(kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(alpaca_data.pd, "read_csv", spy)
    out = alpaca_data.load_history(symbol="DIA")
    assert not out.empty
    assert calls and calls[-1].get("low_memory") is False


def test_replay_concat_has_no_futurewarning_and_preserves_all_na_schema(tmp_path: Path):
    day = datetime(2026, 9, 14).date()
    first = pd.DataFrame({
        "seq": [1], "timestamp": ["2026-09-14T10:00:00-05:00"], "price": [500.0],
        "size": [10], "bid": [None], "ask": [None], "conditions": [[]],
    })
    second = pd.DataFrame({
        "seq": [2], "timestamp": ["2026-09-14T10:00:01-05:00"], "price": [500.1],
        "size": [12], "bid": [None], "ask": [None], "conditions": [[]],
    })
    replay.persist_tape(tmp_path, "DIA", first, day=day)
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        replay.persist_tape(tmp_path, "DIA", second, day=day)
    stored = pd.read_csv(replay._tape_path(tmp_path, "DIA", day), compression="gzip")
    assert len(stored) == 2
    assert {"bid", "ask"}.issubset(stored.columns)


def test_timeout_diagnostics_respect_market_session():
    london = datetime(2026, 9, 14, 3, 30, tzinfo=NY)
    ny_rth = datetime(2026, 9, 14, 10, 0, tzinfo=NY)
    assert live_price._price_timeout_should_warn("DIA", london) is False
    assert live_price._price_timeout_should_warn("YM", london) is True
    assert option_stream._option_timeout_should_warn("DIA", london) is False
    assert live_price._price_timeout_should_warn("DIA", ny_rth) is True
    assert option_stream._option_timeout_should_warn("DIA", ny_rth) is True
