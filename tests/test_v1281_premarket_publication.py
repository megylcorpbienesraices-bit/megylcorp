from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.core.freshness import evaluate_publication_gate, reset_publication_gate

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/app.css").read_text(encoding="utf-8")
TASTY = (ROOT / "app/providers/tastytrade/market_data.py").read_text(encoding="utf-8")


def _meta(*, state: str, option_ts: str, stock_ts: str, clock: str) -> dict:
    return {
        "symbol": "DIA",
        "market_state": state,
        "quality_clock": pd.Timestamp(clock),
        "latest_option_market_timestamp": pd.Timestamp(option_ts),
        "stock_market_timestamp": pd.Timestamp(stock_ts),
    }


def test_calendar_and_nav_share_one_sticky_shell_without_overlap():
    assert 'id="globalStickyShell"' in HTML
    assert '<div class="global-sticky-shell"' in HTML
    assert '.global-sticky-shell{position:sticky;top:68px' in CSS
    assert '.global-sticky-shell .replay-bar{position:relative;top:auto' in CSS
    assert '.global-sticky-shell .top-section-nav{position:relative;top:auto' in CSS
    # Regression: the two siblings must never independently claim top:68px again.
    assert '.replay-bar{position:sticky;top:68px' not in CSS
    assert '.top-section-nav{position:sticky;top:68px' not in CSS


def test_premarket_accepts_previous_session_option_structure_with_fresh_underlying():
    reset_publication_gate("DIA")
    # Monday 08:00 New York (12:00 UTC): Friday 16:00 option structure is ~64h old,
    # but the premarket underlying is current. Structural GEX/DEX must be publishable.
    now = pd.Timestamp("2026-09-14T12:00:00Z")
    out = evaluate_publication_gate(
        _meta(
            state="PREMARKET",
            option_ts="2026-09-11T20:00:00Z",
            stock_ts="2026-09-14T11:59:58Z",
            clock=now.isoformat(),
        ),
        now=now.timestamp(),
    )
    assert out["publicar_permitido"] is True
    assert out["premarket_structural_options"] is True
    assert out["clock_policy"]["option_market_open"] is False
    assert out["clock_policy"]["underlying_market_open"] is True
    assert out["clock_policy"]["option_mode"] == "STRUCTURAL_SESSION"


def test_same_old_option_timestamp_is_blocked_during_regular_session():
    reset_publication_gate("DIA")
    now = pd.Timestamp("2026-09-14T14:00:00Z")
    out = evaluate_publication_gate(
        _meta(
            state="REGULAR",
            option_ts="2026-09-11T20:00:00Z",
            stock_ts="2026-09-14T13:59:58Z",
            clock=now.isoformat(),
        ),
        now=now.timestamp(),
    )
    assert out["publicar_permitido"] is False
    assert out["cadena_opciones"]["estado"] == "STALE"
    assert out["clock_policy"]["option_market_open"] is True


def test_premarket_still_blocks_if_underlying_itself_is_stale():
    reset_publication_gate("DIA")
    now = pd.Timestamp("2026-09-14T12:00:00Z")
    out = evaluate_publication_gate(
        _meta(
            state="PREMARKET",
            option_ts="2026-09-11T20:00:00Z",
            stock_ts="2026-09-14T11:50:00Z",
            clock=now.isoformat(),
        ),
        now=now.timestamp(),
    )
    assert out["publicar_permitido"] is False
    assert out["subyacente"]["estado"] == "STALE"


def test_tastytrade_structural_meta_exports_real_freshness_clocks():
    # Contract-level and chain-level timestamps must come from observed market events;
    # Summary/OI may not impersonate a fresh quote.
    for token in (
        '"latest_option_market_timestamp":latest_option_ts',
        '"stock_market_timestamp":stock_market_ts',
        '"quality_clock":now.isoformat()',
        '"market_state":market_state',
        '"oi_dates":oi_dates',
        'if str(event_name).upper() in {"QUOTE","TRADE","GREEKS"}',
        'latest_market=max(market_event_times)',
    ):
        assert token in TASTY
    assert '"stock_market_timestamp":now.isoformat()' not in TASTY


def test_tastytrade_ym_chain_meta_is_complete_and_uses_observed_underlying_clock(monkeypatch):
    from app.providers.tastytrade.health import TastytradeHealth
    from app.providers.tastytrade.market_data import TastytradeMarketData, PRICE_TICK_FABRIC

    md = TastytradeMarketData(TastytradeHealth())
    now = datetime.now(timezone.utc)
    local_naive = pd.Timestamp(now).tz_convert("America/Guayaquil").tz_localize(None)
    monkeypatch.setattr(PRICE_TICK_FABRIC, "dataframe", lambda symbol: pd.DataFrame([
        {"timestamp": local_naive, "price": 46000.0}
    ]))

    for i, strike in enumerate((45900.0, 46000.0, 46100.0, 46200.0)):
        base = {
            "component_symbol": "YM", "instrument_type": "FUTURE_OPTION",
            "strike": strike, "dte": 10, "option_type": "call",
            "expiration": "2026-09-25T00:00:00Z", "contract_multiplier": 5.0,
        }

        def pack(event: str, payload: dict) -> dict:
            return {
                "event_type": event, "event_time": now.isoformat(),
                "receive_time": now.isoformat(), "payload": {**base, **payload},
            }

        md._derivative_state.setdefault("YM", {})[f"YM_OPT_{i}"] = {
            "QUOTE": pack("QUOTE", {"bid": 100.0, "ask": 102.0}),
            "GREEKS": pack("GREEKS", {"iv": 0.20, "delta": 0.5, "gamma": 0.0001}),
            "SUMMARY": pack("SUMMARY", {"open_interest": 100, "open_interest_date": "2026-09-11"}),
            "TRADE": pack("TRADE", {"price": 101.0, "day_volume": 5}),
        }

    frame, meta = md.structural_chain_frame("YM", strike_window=1200.0, expiry_days=21)
    assert len(frame) == 4
    assert meta["market_state"] == "FUTURES_SESSION"
    assert meta["latest_option_market_timestamp"]
    assert meta["stock_market_timestamp"]
    assert meta["quality_clock"]
    assert meta["oi_dates"] == ["2026-09-11"]
    assert set(frame["pricing_model"]) == {"BLACK_76"}


def test_build_data_quality_propagates_premarket_structural_publication():
    from app.core.precision_engine import build_data_quality

    reset_publication_gate("DIA")
    now = pd.Timestamp("2026-09-14T12:00:00Z")
    frame = pd.DataFrame({
        "strike": [525.0], "bid": [1.0], "ask": [1.1],
        "calc_delta": [0.5], "calc_gamma": [0.01],
        "fallback_delta": [0.5], "fallback_gamma": [0.01],
    })
    meta = _meta(
        state="PREMARKET",
        option_ts="2026-09-11T20:00:00Z",
        stock_ts="2026-09-14T11:59:58Z",
        clock=now.isoformat(),
    )
    meta.update({"matched_snapshots": 1, "contract_definitions": 1, "symbol": "DIA"})
    dq = build_data_quality(frame, meta, {}, {"count": 1, "mode": "WEEKLY"})
    assert dq["circuito_frescura"]["publicar_permitido"] is True
    assert dq["circuito_frescura"]["premarket_structural_options"] is True
