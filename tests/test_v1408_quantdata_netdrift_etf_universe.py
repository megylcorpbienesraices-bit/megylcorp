from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core.assets import ASSETS, STATIC_ASSET_SYMBOLS, asset_info, register_provider_etfs, selectable_assets
from app.core.institutional_modules import quantdata_net_drift_figure
from app.core.provider_etf_catalog import _looks_like_etf, _merge
from app.providers.quantdata.runtime import QuantDataRuntime, _net_drift_series

ROOT = Path(__file__).resolve().parents[1]


def _payload():
    return {
        "data": {
            "1778679000000": {
                "netCallPremium": 150000.0,
                "netPutPremium": -80000.0,
                "netCallVolume": 1250,
                "netPutVolume": -620,
                "stockPrice": 185.50,
            },
            "1778679060000": {
                "netCallPremium": -25000.0,
                "netPutPremium": -40000.0,
                "netCallVolume": -100,
                "netPutVolume": -220,
                "stockPrice": 185.75,
            },
        }
    }


def test_quantdata_net_drift_preserves_signed_buckets_and_cumsum():
    out = _net_drift_series(_payload())
    assert out["provider"] == "QUANTDATA"
    rows = out["buckets"]
    assert len(rows) == 2
    assert rows[0]["net_call_premium"] == 150000.0
    assert rows[0]["net_put_premium"] == -80000.0
    assert rows[1]["cum_call_premium"] == 125000.0
    assert rows[1]["cum_put_premium"] == -120000.0
    assert rows[1]["cum_net_premium"] == 5000.0


def test_quantdata_net_drift_rejects_zero_price_placeholder():
    payload = _payload()
    payload["data"]["1778679000000"]["stockPrice"] = 0
    out = _net_drift_series(payload)
    assert out["buckets"][0]["stock_price"] is None


def test_quantdata_net_drift_chart_is_provider_observed_not_native_dex_gex():
    values = {"net_drift_series": _net_drift_series(_payload())}
    fig = quantdata_net_drift_figure(values, "AAPL")
    names = [str(t.name or "") for t in fig.data]
    assert "CALL DRIFT" in names
    assert "PUT DRIFT" in names
    assert "NET DRIFT" in names
    assert "CALL / BUCKET" in names
    assert "PUT / BUCKET" in names
    assert not any("GAMMA DRIFT" in n or "NET DELTA DRIFT" in n for n in names)
    assert fig.layout.meta["provider"] == "QUANTDATA"
    assert fig.layout.meta["native_dex_gex_substitution"] is False
    assert fig.layout.meta["price_zero_rejected"] is True


def test_quantdata_net_drift_chart_does_not_plot_zero_direct_price():
    values = {"net_drift_series": {"buckets": [], "available": False}}
    history = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-09-17T13:30:00Z", "2026-09-17T13:31:00Z", "2026-09-17T13:32:00Z"]),
        "close": [0.0, 519.10, 519.20],
    })
    fig = quantdata_net_drift_figure(values, "DIA", price_history=history)
    price = next(t for t in fig.data if t.name == "DIA")
    assert all(float(v) > 0 for v in price.y)


def test_provider_etf_classifier_covers_names_and_quantdata_asset_management_taxonomy():
    assert _looks_like_etf("SPDR S&P 500 ETF Trust", exchange="ARCA")
    assert _looks_like_etf("Example Income Fund", exchange="NYSEARCA")
    assert _looks_like_etf("Example Leveraged Portfolio", sector="NOT_APPLICABLE", industry="ASSET_MANAGEMENT__LEVERAGED")
    assert not _looks_like_etf("Apple Inc.", sector="TECHNOLOGY", industry="CONSUMER_ELECTRONICS")


def test_provider_etf_merge_is_union_not_intersection():
    rows = _merge(
        {"SPY": {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "alpaca": True, "alpaca_has_options": True}},
        {"QQQ": {"symbol": "QQQ", "name": "Invesco QQQ Trust", "quantdata": True, "quantdata_option_activity": True}},
    )
    by_symbol = {r["symbol"]: r for r in rows}
    assert set(by_symbol) == {"SPY", "QQQ"}
    assert by_symbol["SPY"]["alpaca"] is True
    assert by_symbol["QQQ"]["quantdata"] is True


def test_dynamic_provider_etfs_are_searchable_and_full_only_with_own_alpaca_options(monkeypatch):
    before = set(ASSETS)
    rows = [
        {"symbol": "ZZZA", "name": "Test ETF A", "alpaca": True, "quantdata": True, "alpaca_has_options": True},
        {"symbol": "ZZZB", "name": "Test ETF B", "alpaca": True, "quantdata": True, "alpaca_has_options": False},
    ]
    try:
        register_provider_etfs(rows)
        a = asset_info("ZZZA"); b = asset_info("ZZZB")
        assert a["dynamic"] is True and a["full"] is True and a["selectable"] is True
        assert b["dynamic"] is True and b["full"] is False and b["selectable"] is True
        assert a["ecosystem"]["derivatives"]["equity_options"] == ["ZZZA"]
        visible = {r["symbol"] for r in selectable_assets()}
        assert {"ZZZA", "ZZZB"}.issubset(visible)
    finally:
        for sym in set(ASSETS) - before:
            ASSETS.pop(sym, None)


def test_quantdata_dynamic_etf_target_is_direct(monkeypatch):
    before = set(ASSETS)
    try:
        register_provider_etfs([{"symbol": "ZZZQ", "name": "Test ETF Q", "quantdata": True, "alpaca": True, "alpaca_has_options": True}])
        target = QuantDataRuntime._target("ZZZQ")
        assert target.active_symbol == "ZZZQ"
        assert target.query_ticker == "ZZZQ"
        assert target.direct is True
    finally:
        for sym in set(ASSETS) - before:
            ASSETS.pop(sym, None)


def test_dynamic_etfs_do_not_expand_board_or_history_precompute_loops():
    service = (ROOT / "app/service.py").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'cfg.get("board_enabled",True)' in service
    assert "for a in core_selectable_assets()" in main


def test_symbol_search_keeps_all_provider_etfs_searchable_without_thousands_of_dom_nodes():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    fn = js[js.index("function renderSymbolSearchResults(){"):js.index("function openSymbolSearch()")]
    assert "selectable===false" not in fn
    assert "limit=q?320:160" in fn
    assert "MERCADO DIRECTO · DERIVADOS PARCIALES" in fn
    assert "símbolos adicionales" in fn


def test_nextgen_net_drift_mini_uses_quantdata_call_put_net_names():
    js = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    fn = js[js.index("function compactNetDriftMini"):js.index("function updateDealerMini")]
    assert "CALL DRIFT|PUT DRIFT|NET DRIFT" in fn
    assert "NET DELTA DRIFT|GAMMA DRIFT" not in fn
    assert "provider:'QUANTDATA'" in fn


def test_static_core_universe_is_not_redefined_by_provider_catalog():
    assert {"DIA", "YM", "MYM", "DJX", "XLI", "XLF", "VIX", "VXD"}.issubset(STATIC_ASSET_SYMBOLS)
