from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from conftest import assert_marker_version_at_least, assert_version_at_least
from app.core.assets import asset_info
from app.core.engine import engine_config_for_asset
from app.core.historical_session_store import HistoricalSessionStore, trade_date_for_timestamp
from app.core.replay import persist_tape
from app.core.instruments import MODEL_FUTURE
from app.core.precision_engine import (
    black_76_greeks_full,
    black_76_price,
    implied_volatility_black_76,
)

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
JS = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/app.css").read_text(encoding="utf-8")
SERVICE = (ROOT / "app/service.py").read_text(encoding="utf-8")
TASTY = (ROOT / "app/providers/tastytrade/market_data.py").read_text(encoding="utf-8")
SYMBOLS = (ROOT / "app/providers/tastytrade/symbols.py").read_text(encoding="utf-8")


def test_release_identity_at_least_v1280():
    assert_version_at_least("1.28.0")
    assert_marker_version_at_least("1.28.0")


def test_backtest_is_not_a_navigation_or_page_mode_anymore():
    assert 'data-section="backtest"' not in HTML
    assert 'id="section-backtest"' not in HTML
    assert 'id="topBacktestBtn"' not in HTML
    assert "navigateSection('backtest')" not in JS
    assert "CONTEXTO TEMPORAL GLOBAL · TODA LA PLATAFORMA" in HTML
    assert ".lean-replay{display:flex}" in CSS


def test_global_calendar_has_session_playback_controls():
    for token in ('id="replayDateCalendar"', 'id="replayTimeline"', 'id="replayPlayPause"', 'id="replaySpeed"', 'id="replayLiveBtn"'):
        assert token in HTML
    assert "applyReplayClockIndex" in JS
    assert "toggleReplayPlayback" in JS
    assert "asof=${encodeURIComponent(asofISO)}" in JS
    assert "replayClockMarks" in JS


def test_research_range_lives_under_auditor_not_separate_backtest():
    auditor = HTML[HTML.index('id="section-auditor"'):HTML.index('id="section-infrastructure"')]
    assert "RESEARCH &amp; CALIBRATION" in auditor
    assert "RANGO HISTÓRICO · INVESTIGACIÓN" in auditor
    assert 'id="researchRangeBtn"' in auditor  # internal research-range id, not navigation


def test_cme_trade_date_maps_sunday_evening_to_monday():
    ny = ZoneInfo("America/New_York")
    sunday = datetime(2026, 9, 13, 18, 30, tzinfo=ny)
    monday = datetime(2026, 9, 14, 11, 0, tzinfo=ny)
    assert trade_date_for_timestamp("YM", sunday).isoformat() == "2026-09-14"
    assert trade_date_for_timestamp("MYM", sunday).isoformat() == "2026-09-14"
    assert trade_date_for_timestamp("YM", monday).isoformat() == "2026-09-14"


def test_provider_neutral_store_builds_cross_calendar_cme_clock(tmp_path):
    ny = ZoneInfo("America/New_York")
    store = HistoricalSessionStore(tmp_path)
    ticks = pd.DataFrame({
        "timestamp": [
            datetime(2026, 9, 13, 18, 5, tzinfo=ny),
            datetime(2026, 9, 13, 18, 6, tzinfo=ny),
            datetime(2026, 9, 14, 9, 30, tzinfo=ny),
        ],
        "price": [46000.0, 46001.0, 46125.0],
        "size": [1, 2, 1],
        "seq": [1, 2, 3],
        "exchange": ["CBOT"] * 3,
    })
    out = store.append_frame("YM", "price_ticks", ticks, source="TASTYTRADE_DXLINK")
    assert out["sessions"] == ["2026-09-14"]
    assert store.available_sessions("YM") == ["2026-09-14"]
    clock = store.session_clock("YM", "2026-09-14")
    assert clock["ready"] is True
    assert clock["trade_date"] == "2026-09-14"
    assert clock["session_model"] == "CME_FUTURES"
    assert clock["families"]["price_ticks"] == 3
    assert len(clock["marks"]) > 2
    assert str(clock["start"]).startswith("2026-09-13")  # platform-local EC still previous calendar day
    assert str(clock["end"]).startswith("2026-09-14")


def test_black76_put_call_parity_and_iv_roundtrip():
    F, K, T, sigma, r = 46000.0, 46200.0, 21 / 365.0, 0.19, 0.045
    call = black_76_price(F, K, T, sigma, "call", r)
    put = black_76_price(F, K, T, sigma, "put", r)
    assert np.isclose(call - put, np.exp(-r * T) * (F - K), rtol=1e-10, atol=1e-8)
    recovered = implied_volatility_black_76(call, F, K, T, "call", r)
    assert np.isclose(recovered, sigma, rtol=1e-6, atol=1e-7)


def test_black76_delta_gamma_match_finite_difference():
    F, K, T, sigma, r = 45500.0, 45500.0, 14 / 365.0, 0.22, 0.04
    g = black_76_greeks_full(F, K, T, sigma, "call", r)
    h = 0.25
    up = black_76_price(F + h, K, T, sigma, "call", r)
    dn = black_76_price(F - h, K, T, sigma, "call", r)
    base = black_76_price(F, K, T, sigma, "call", r)
    delta_fd = (up - dn) / (2 * h)
    gamma_fd = (up - 2 * base + dn) / (h * h)
    assert np.isclose(g["delta"], delta_fd, rtol=2e-5, atol=2e-5)
    assert np.isclose(g["gamma"], gamma_fd, rtol=2e-4, atol=2e-7)


def test_future_engine_uses_own_multiplier_and_black76_model():
    ym = engine_config_for_asset("YM")
    mym = engine_config_for_asset("MYM")
    assert ym.contract_multiplier == 5.0
    assert mym.contract_multiplier == 0.5
    assert ym.option_model == MODEL_FUTURE
    assert mym.option_model == MODEL_FUTURE


def test_ym_and_mym_are_full_own_chain_assets_without_dia_proxy():
    for sym in ("YM", "MYM"):
        cfg = asset_info(sym)
        assert cfg["full"] is True
        assert cfg["quant_provider"] == "TASTYTRADE"
        assert sym in set(cfg["ecosystem"]["derivatives"]["future_options"])
    assert 'itype == "FUTURE_OPTION"' not in TASTY
    assert '"FUTURE_OPTION"' in TASTY and "component!=under" in TASTY
    assert "recover_iv_and_greeks" in TASTY and 'provider_name="TASTYTRADE_DXLINK"' in TASTY


def test_date_only_replay_starts_from_archived_clock_not_hidden_precompute_gate():
    assert "SESSION_NOT_PRECOMPUTED_YET" not in SERVICE
    assert "resolved_asof=str(clock.get(\"start\")" in SERVICE
    assert "replay_load_structural_history" in SERVICE
    assert "HistoricalSessionStore" in SERVICE
    assert '"option_chain"' in SERVICE and 'append_quant_state' in SERVICE


def test_live_stale_blocks_actionability_not_historical_navigation():
    assert "LIVE NO ACCIONABLE" not in JS
    assert "freshnessHistoryBtn" not in JS
    assert "publicationBlocked=gate?.publicar_permitido!==true && replayMode==='LIVE'" in JS


def test_persist_tape_does_not_force_cme_local_calendar_date(tmp_path):
    ny = ZoneInfo("America/New_York")
    ticks = pd.DataFrame({
        "timestamp": [datetime(2026, 9, 13, 18, 5, tzinfo=ny)],
        "price": [46000.0], "size": [1.0], "seq": [1], "exchange": ["CBOT"],
    })
    # Legacy mirror is still keyed to the local calendar day for compatibility,
    # but the canonical provider-neutral store must use Monday's CME trade date.
    persist_tape(tmp_path, "YM", ticks, day=datetime(2026, 9, 13).date())
    store = HistoricalSessionStore(tmp_path)
    assert "2026-09-14" in store.available_sessions("YM")
    assert "2026-09-13" not in store.available_sessions("YM")


def test_future_option_selection_accepts_expires_at_and_never_defaults_multiplier_to_100():
    assert 'x.get("expires-at") or x.get("expiration-date")' in SYMBOLS
    assert 'itype=="FUTURE_OPTION"' in TASTY
    assert '_get_instrument(component).multiplier' in TASTY
    assert 'multiplier=100.0' in TASTY  # still valid only for equity/index options


def test_tasty_candles_are_archived_incrementally_for_global_replay():
    assert 'historical_candle_cursor' in SERVICE
    assert 'TASTYTRADE.market_data.candle_frame(self.symbol, "1m")' in SERVICE
    assert '"candles", _candles' in SERVICE
    assert 'dedupe_keys=("timestamp", "period", "source")' in SERVICE


def test_replay_clock_starts_at_first_quant_reconstructible_snapshot(tmp_path):
    ny = ZoneInfo("America/New_York")
    store = HistoricalSessionStore(tmp_path)
    store.append_frame("YM", "candles", pd.DataFrame({
        "timestamp": [datetime(2026, 9, 13, 18, 0, tzinfo=ny), datetime(2026, 9, 13, 18, 1, tzinfo=ny)],
        "period": ["1m", "1m"], "open": [46000, 46001], "high": [46001, 46002],
        "low": [45999, 46000], "close": [46001, 46002], "source": ["DX", "DX"],
    }), source="DX")
    store.append_frame("YM", "option_chain", pd.DataFrame({
        "timestamp": [datetime(2026, 9, 13, 18, 5, tzinfo=ny)],
        "contract_symbol": ["YM_OPT"], "expiration_date": ["2026-10-01"],
        "strike": [46000.0], "option_type": ["call"], "source": ["DX"],
    }), source="DX")
    clock = store.session_clock("YM", "2026-09-14")
    assert clock["quant_ready"] is True
    assert clock["observed_start"] < clock["quant_start"]
    assert clock["start"] == clock["quant_start"]
    assert clock["marks"][0] == clock["quant_start"]
