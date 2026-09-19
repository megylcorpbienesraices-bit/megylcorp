from __future__ import annotations

from pathlib import Path
import threading

import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version

ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_release_identity_and_boot_contract():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    html = text("app/templates/dashboard.html")
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))
    assert "/static/app.js?v={{ app_version }}" in html


def test_boot_ui_does_not_publish_false_zero_expiry_while_loading():
    js = text("app/static/app.js")
    assert "info.loading||info.count===null" in js
    assert "'CARGANDO…'" in js
    assert "'VERIFICANDO…'" in js
    assert "Price, memory and TRACE readiness must never be hidden" in js
    assert "hydrateSelectedAsset(String(lastState.active_symbol||activeSymbol)" in js


def test_backend_primes_price_history_and_publishes_core_before_enrichment():
    main = text("app/main.py")
    service = text("app/service.py")
    assert "def _schedule_trace_bootstrap" in main
    assert "STATE.trace_session_bootstrap" in main
    assert "def adopt_asset_core_worker" in service
    assert "core_ready_callback" in service
    assert "BACKGROUND_ENRICHMENT" in service
    assert "QUANT_CORE_READY" in service


def test_instant_boot_window_hint_roundtrip(tmp_path, monkeypatch):
    from app.core import instant_boot

    monkeypatch.setattr(instant_boot, "_PATH", tmp_path / "instant_boot_hints.json")
    v, src = instant_boot.get_chain_window_hint("DIA", "AUTO", 12.0)
    assert v == 12.0 and src == "LEGACY_FALLBACK"
    instant_boot.save_chain_window_hint("DIA", "AUTO", 18.5)
    v, src = instant_boot.get_chain_window_hint("DIA", "AUTO", 12.0)
    assert abs(v - 18.5) < 1e-9
    assert src == "PERSISTED_SIGMA_HINT"


def test_alpaca_contracts_and_chain_start_concurrently(monkeypatch):
    from app.core import alpaca_data as ad

    monkeypatch.setattr(ad, "load_settings", lambda: ad.AlpacaSettings("k", "s"))
    monkeypatch.setattr(ad, "fetch_stock_snapshot", lambda settings, symbol: {"spot": 100.0, "market_timestamp": "2026-09-10T12:00:00Z"})

    contracts_started = threading.Event()
    chain_started = threading.Event()
    both_saw_peer = {"contracts": False, "chain": False}

    def fake_contracts(settings, spot, strike_window, expiry_days, symbol):
        contracts_started.set()
        both_saw_peer["contracts"] = chain_started.wait(0.8)
        return pd.DataFrame([{
            "symbol": "DIA260911C00100000", "strike_price": "100", "open_interest": "100",
            "expiration_date": "2026-09-11", "type": "call", "open_interest_date": "2026-09-09",
        }])

    def fake_chain(settings, spot, strike_window, expiry_days, symbol):
        chain_started.set()
        both_saw_peer["chain"] = contracts_started.wait(0.8)
        return {"DIA260911C00100000": {
            "impliedVolatility": 0.20,
            "greeks": {"delta": 0.5, "gamma": 0.02},
            "dailyBar": {"v": 25},
            "latestQuote": {"bp": 1.0, "ap": 1.2, "t": "2026-09-10T12:00:00Z"},
            "latestTrade": {"p": 1.1, "t": "2026-09-10T12:00:00Z"},
        }}

    monkeypatch.setattr(ad, "fetch_contracts", fake_contracts)
    monkeypatch.setattr(ad, "fetch_chain", fake_chain)
    monkeypatch.setattr(ad, "recover_iv_and_greeks", lambda **kwargs: {
        "iv": 0.20, "iv_source": "ALPACA", "greeks_source": "ALPACA",
        "calc_delta": 0.5, "calc_gamma": 0.02, "calc_vanna": 0.0, "calc_charm": 0.0,
        "calc_speed": 0.0, "greeks_dislocation": False, "provider_delta_diff": 0.0,
        "provider_gamma_diff_pct": 0.0, "quote_source": "NBBO", "risk_free_rate": 0.04,
        "risk_free_source": "TEST", "dividend_yield": 0.0, "dividend_source": "TEST",
    })

    df, meta = ad.fetch_asset_options_snapshot("DIA", 12.0, 7)
    assert len(df) == 1
    assert both_saw_peer == {"contracts": True, "chain": True}
    assert meta["contracts_chain_parallel"] is True
    assert meta["boot_timing_ms"]["contracts_plus_chain_parallel"] >= 0


def test_warmup_progress_never_rolls_back_phase():
    from app.service import PlatformState

    s = PlatformState(symbol="DIA", symbol_epoch=4)
    s.asset_warmup = {"active": True, "phase": "CHAIN_AND_QUANT", "progress_pct": 35, "symbol": "DIA", "epoch": 4}
    assert s.update_asset_warmup("DIA", 4, phase="PRICE_READY", progress_pct=25)
    assert s.asset_warmup["progress_pct"] == 35
    assert s.asset_warmup["phase"] == "CHAIN_AND_QUANT"


def test_core_publish_becomes_usable_before_final_enrichment():
    from app.service import PlatformState

    main = PlatformState(symbol="DIA", symbol_epoch=7, mode="LIVE")
    main.asset_warmup = {"active": True, "phase": "CHAIN_AND_QUANT", "progress_pct": 35, "symbol": "DIA", "epoch": 7}
    worker = PlatformState(symbol="DIA", symbol_epoch=7, mode="LIVE")
    worker.gamma_delta = {"spot": 530.0, "gamma_center": 531.0}
    worker.scanner = {"ready": True, "direction": "BUY", "evidence_score": 71}
    worker.snapshot = pd.DataFrame({"symbol": ["DIA260911C00530000"], "strike": [530.0]})
    worker.expiry_info = {"count": 2, "expirations": ["2026-09-10", "2026-09-11"]}

    assert main.adopt_asset_core_worker(worker, "DIA", 7) is True
    assert main.gamma_delta["spot"] == 530.0
    assert main.scanner["ready"] is True
    assert main.asset_warmup["active"] is True
    assert main.asset_warmup["phase"] == "BACKGROUND_ENRICHMENT"
    assert main.asset_warmup["core_ready"] is True
    assert main.asset_warmup["progress_pct"] == 82

    worker.research_storage_report = {"research_ready": True}
    assert main.adopt_asset_warm_worker(worker, "DIA", 7) is True
    assert main.asset_warmup["active"] is False
    assert main.asset_warmup["phase"] == "READY"
    assert main.asset_warmup["progress_pct"] == 100
    assert main.research_storage_report["research_ready"] is True
