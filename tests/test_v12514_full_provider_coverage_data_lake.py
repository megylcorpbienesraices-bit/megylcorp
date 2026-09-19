from __future__ import annotations

import json
import time
from pathlib import Path
from conftest import assert_version_at_least, assert_marker_version_at_least


def test_version_and_product_marker_are_12514():
    root = Path(__file__).resolve().parents[1]
    assert_version_at_least('1.26.2')
    marker = json.loads((root / ".itm_quant_product.json").read_text(encoding="utf-8"))
    assert_marker_version_at_least('1.26.2')
def test_data_lake_archives_raw_and_normalized_without_provider_rank():
    from app.core.provider_data_lake import ProviderDataLake
    lake = ProviderDataLake(capacity=10_000)
    lake.archive_raw(source="ALPACA_SIP", symbol="DIA", event_type="QUOTE", payload={"bp": 1, "ap": 2})
    lake.archive_normalized(source="TASTYTRADE", symbol="DIA", event_type="GREEKS", values={"gamma": 0.1})
    lake.archive_catalog(source="OFFICIAL_MACRO", symbol="DIA", catalog_type="DISCOVERY", items={"series": ["DIA_CONTEXT"]})
    deadline = time.time() + 2
    while time.time() < deadline and lake.status()["records_written_this_process"] < 3:
        time.sleep(0.02)
    st = lake.status()
    assert st["architecture"] == "HOT_LIVE + WARM_UNIVERSE + COLD_DATA_LAKE"
    assert st["provider_total_external_library_claimed"] is False
    assert st["providers"]["ALPACA"]["raw_records_seen"] >= 1
    assert st["providers"]["TASTYTRADE"]["normalized_records_seen"] >= 1
    # v1.27.1: TRADESTATION se quitó del registro en v1.27.0 porque
    # `app/providers/tradestation/` nunca llegó a empaquetarse. Listar un proveedor
    # sin implementación produce huecos silenciosos en la cobertura histórica.
    # El invariante fuerte (registro == implementación) vive ahora en:
    #     tests/test_v1271_provider_truthfulness.py
    assert "TRADESTATION" not in st["providers"]


def test_tastytrade_settings_include_background_catalog(monkeypatch):
    # El roster manda sobre TASTYTRADE_ENABLED: sin estar en él, las credenciales
    # no se cargan por mucho que la variable diga que sí. Esta prueba comprueba el
    # catálogo en segundo plano, así que declara el roster que lo incluye.
    import importlib
    import app.core.provider_parity as _P
    monkeypatch.setenv("ITM_OPTIONS_PEERS", "ALPACA,TASTYTRADE,QUANTDATA")
    importlib.reload(_P)
    monkeypatch.setenv("TASTYTRADE_ENABLED", "1")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "refresh-value")
    monkeypatch.setenv("TASTYTRADE_CATALOG_ENABLED", "1")
    monkeypatch.setenv("TASTYTRADE_CATALOG_REFRESH_MINUTES", "45")
    from app.providers.tastytrade.settings import load_settings
    s = load_settings()
    assert s is not None
    assert s.catalog_enabled is True
    assert s.catalog_refresh_minutes == 45
    assert s.user_agent == f"ITM-QUANT/{(Path(__file__).resolve().parents[1]/'VERSION.txt').read_text(encoding='utf-8').strip()}"
    monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
    importlib.reload(_P)


def test_alpaca_cold_library_functions_exist():
    from app.core import alpaca_data
    assert callable(alpaca_data.fetch_contract_catalog)
    assert callable(alpaca_data.fetch_full_option_snapshot_library)


def test_provider_library_is_one_asset_per_cycle_and_non_hot():
    from app.core.provider_library import PROVIDER_LIBRARY
    st = PROVIDER_LIBRARY.status()
    assert st["policy"] == "ONE_ASSET_PER_CYCLE · NEVER_BLOCK_HOT_LIVE"
    assert st["alpaca_catalog_days"] >= st["alpaca_snapshot_days"]


def test_main_exposes_coverage_and_data_lake_endpoints():
    root = Path(__file__).resolve().parents[1]
    src = (root / "app" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/providers/coverage")' in src
    assert '@app.get("/providers/coverage", response_class=HTMLResponse)' in src
    assert '@app.get("/api/providers/data-lake")' in src
    assert '@app.post("/api/providers/library/collect-next")' in src
    assert_marker_version_at_least('1.26.2')


def test_provider_center_surfaces_coverage_auditor_and_cold_settings():
    root = Path(__file__).resolve().parents[1]
    gui = (root / "setup_local_gui.py").read_text(encoding="utf-8")
    assert "AUDITOR DE COBERTURA" in gui
    assert "/providers/coverage" in gui
    assert "PROVIDER_LIBRARY_ENABLED" in gui
    assert "PROVIDER_DATA_LAKE_QUEUE" in gui


def test_tastytrade_catalog_does_not_change_live_subscriptions_by_design():
    root = Path(__file__).resolve().parents[1]
    runtime = (root / "app" / "providers" / "tastytrade" / "runtime.py").read_text(encoding="utf-8")
    block = runtime[runtime.index("async def _catalog_loop"):runtime.index("def status", runtime.index("async def _catalog_loop"))]
    assert "set_subscriptions" not in block
    assert "add_symbols" not in block
    assert "archive_catalog" in block
    assert "equity_option_instruments" in block
    assert "futures_option_instruments" in block


def test_data_lake_never_archives_tastytrade_quote_token_path():
    root = Path(__file__).resolve().parents[1]
    client = (root / "app" / "providers" / "tastytrade" / "client.py").read_text(encoding="utf-8")
    assert "safe_prefixes" in client and "/instruments/" in client and "/option-chains/" in client and "/futures-option-chains/" in client and "/market-data/by-type" in client
    assert '"/api-quote-tokens"' in client
    # The quote-token request exists, but is absent from the archive allow-list.
    allow = client[client.index("safe_prefixes"):client.index("if any", client.index("safe_prefixes"))]
    assert "/api-quote-tokens" not in allow
