from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.freshness import evaluate_publication_gate

ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "app/service.py").read_text(encoding="utf-8")
EC = ZoneInfo("America/Guayaquil")


def test_release_identity_at_least_v1350():
    assert_version_at_least("1.35.0")
    assert_marker_version_at_least("1.35.0")


# --------------------------------------------------------------- regression

def test_demo_meta_block_publishes_timestamps_the_freshness_gate_can_read():
    """Found while manually running the DEMO server end-to-end: build_data_quality()
    reads meta['latest_option_market_timestamp']/meta['stock_market_timestamp'] via
    age_of(). Before this fix DEMO's meta dict never set either key, so age_of(None)
    -> UNKNOWN forever, latching the publication gate OPEN and blocking every DEMO
    TRACE/GEX/DEX payload permanently. This is a static-source regression guard:
    the functional behavior is covered below by driving evaluate_publication_gate()
    directly against a DEMO-shaped meta dict."""
    demo_block_start = SERVICE.index('"source": "DEMO INTEGRADO"')
    demo_block_end = SERVICE.index("self.mode = \"DEMO\"", demo_block_start)
    block = SERVICE[demo_block_start:demo_block_end]
    assert "latest_option_market_timestamp" in block
    assert "stock_market_timestamp" in block


def test_demo_shaped_meta_with_timestamps_opens_the_publication_gate():
    now_iso = datetime.now(EC).isoformat()
    meta = {
        "symbol": "DIA", "market_state": "DEMO",
        "latest_option_market_timestamp": now_iso,
        "stock_market_timestamp": now_iso,
    }
    gate = evaluate_publication_gate(meta, market_state="DEMO", is_replay=False)
    assert gate["publicar_permitido"] is True, gate
    assert gate["estado"] == "CLOSED"
    assert gate["cadena_opciones"]["estado"] == "FRESH"
    assert gate["subyacente"]["estado"] == "FRESH"


def test_demo_shaped_meta_without_timestamps_is_the_bug_this_fixes():
    """Documents the failure mode this release fixes: a DEMO meta dict missing
    both timestamp keys latches the gate OPEN with an UNKNOWN reading, exactly the
    PUBLICATION_BLOCKED_STALE_DATA behavior observed before the fix."""
    meta = {"symbol": "DIA", "market_state": "DEMO"}
    gate = evaluate_publication_gate(meta, market_state="DEMO", is_replay=False)
    assert gate["publicar_permitido"] is False
    assert gate["cadena_opciones"]["estado"] == "UNKNOWN"
