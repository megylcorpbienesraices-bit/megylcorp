from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]

def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_version_is_v12512():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')


def test_startup_yields_without_synchronous_full_chain_refresh():
    main = text("app/main.py")
    block = main[main.index("async def lifespan"):main.index("app = FastAPI")]
    assert "await asyncio.to_thread(STATE.refresh, False)" not in block
    assert block.count("itmq-initial-quant-hydration") == 1
    assert "TASTYTRADE.start" in block


def test_surface_switch_is_local_first_and_single_rebuild():
    js = text("app/static/nextgen_terminal.js")
    assert "function surfaceCacheMatches" in js
    assert "setPayload(p, field=null)" in js
    assert "if(next===this.field)" in js.replace(" ", "") or "if (next === this.field)" in js
    sync = js[js.index("function syncSurfaceControls"):js.index("async function refreshTrace")]
    assert "surfaceCacheMatches" in sync
    assert "refreshSurface" in sync
    refresh = js[js.index("async function refreshSurface"):]
    assert "AbortController" in refresh
    assert "surfaceRequestSeq" in refresh


def test_cross_section_latest_request_wins_and_warm_requests_are_not_dropped():
    js = text("app/static/app.js")
    assert "pendingSurfaceSlice" in js
    assert "pendingSurfaceMain" in js
    assert "pendingCharts" in js
    assert "flushPendingQuantUI" in js
    assert "surfaceSliceFetchController" in js
    block = js[js.index("async function loadSurfaceSliceFast"):js.index("async function loadCharts")]
    assert "AbortController" in block
    assert "pendingSurfaceSlice=true" in block or "pendingSurfaceSlice = true" in block
    assert "chart_state" in block


def test_surface_backend_cache_and_revision_contract():
    service = text("app/service.py")
    assert "analytics_revision" in service
    assert "surface_payload_cache" in service
    assert "surface_slice_cache" in service
    assert "_invalidate_visual_caches_locked" in service
    assert "cache_key" in service


def test_cross_section_net_oi_title_matches_requested_metric():
    from app.service import _surface_slice_figure
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-09-09 14:30:00"] * 4),
        "strike": [530.0, 530.0, 531.0, 531.0],
        "option_type": ["call", "put", "call", "put"],
        "open_interest": [100, 80, 120, 150],
        "volume": [10, 8, 12, 15],
        "gamma": [0.01, 0.02, 0.015, 0.017],
        "delta": [0.5, -0.5, 0.45, -0.55],
    })
    fig = _surface_slice_figure({"enriched": df, "spot": 530.5}, "Net OI", "Net", "Barras")
    title = str(fig.layout.title.text)
    assert "Net OI" in title
    assert "Gamma" not in title


def test_provider_bus_has_no_fixed_primary_secondary_rank_and_fuses_comparable_prices():
    from app.core.provider_bus import UnifiedProviderBus
    bus = UnifiedProviderBus()
    now = datetime.now(timezone.utc).isoformat()
    bus.ingest(source="ALPACA_SIP", symbol="DIA", event_type="QUOTE", values={"bid": 532.10, "ask": 532.12}, timestamp=now, received_at=now)
    bus.ingest(source="TASTYTRADE", symbol="DIA", event_type="QUOTE", values={"bid": 532.11, "ask": 532.13}, timestamp=now, received_at=now)
    snap = bus.snapshot("DIA")
    assert snap["ready"] is True
    assert set(snap["usable_providers"]) == {"ALPACA_SIP", "TASTYTRADE"}
    assert snap["weighting"] == "QUALITY_DYNAMIC_NOT_FIXED_PROVIDER_RANK"
    assert 532.10 <= snap["consensus_price"] <= 532.13


def test_tastytrade_oauth_and_dxlink_are_read_only_and_secret_safe():
    auth = text("app/providers/tastytrade/auth.py")
    client = text("app/providers/tastytrade/client.py")
    dx = text("app/providers/tastytrade/dxlink.py")
    env = text(".env.example")
    assert "/oauth/token" in auth and "json=body" in auth
    assert '"User-Agent"' in auth
    assert "token[:" not in auth and "access_token)" not in auth
    assert "/api-quote-tokens" in client
    assert '"FEED_SUBSCRIPTION"' in dx and '"COMPACT"' in dx
    assert "TASTYTRADE_REFRESH_TOKEN=" in env
    assert "TASTYTRADE_STREAM_DERIVATIVES=1" in env


def test_dxlink_batches_chain_subscriptions_and_parses_mixed_compact_frames():
    dx = text("app/providers/tastytrade/dxlink.py")
    runtime = text("app/providers/tastytrade/runtime.py")
    assert "def add_symbols" in dx
    assert "self.dxlink.add_symbols(base_items)" in runtime
    assert "self.dxlink.add_symbols(derivative_items)" in runtime
    block = dx[dx.index("async def _consume_feed_data"):dx.index("async def _run")]
    assert "if isinstance(item, str)" in block
    assert "elif isinstance(item, list) and event_type" in block


def test_tastytrade_derivatives_hydrate_off_click_path_with_provider_streamer_symbols():
    runtime = text("app/providers/tastytrade/runtime.py")
    symbols = text("app/providers/tastytrade/symbols.py")
    assert "_hydrate_derivatives" in runtime
    assert "asyncio.create_task" in runtime
    assert "streamer-symbol" in symbols
    assert "derivative_subscription_plan" in symbols
    assert "normalization_required" in symbols
    assert "FUTURE_OPTION" in symbols and "EQUITY_OPTION" in symbols


def test_scanner_and_trace_can_use_quality_aware_consensus_spot_without_cross_instrument_mix():
    service = text("app/service.py")
    assert "PROVIDER_BUS.snapshot(self.symbol)" in service
    assert 'scan_result["provider_consensus"]' in service
    assert 'pulse["provider_consensus"]' in service
    bus = text("app/core/provider_bus.py")
    assert "futures/index exposures require instrument normalization" in bus
