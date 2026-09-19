from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding="utf-8")


def test_provider_center_has_retained_providers_only():
    gui=text("setup_local_gui.py")
    assert "ITM QUANT — CENTRO DE PROVEEDORES" in gui
    assert "Quant Data API Key" in gui
    assert "Tastytrade Client Secret" in gui
    assert "Tastytrade Refresh Token" in gui
    assert "principal/secundario/terciario" in gui
    assert "_merge_env" in gui


def test_env_template_has_quantdata_and_tastytrade():
    env=text(".env.example")
    assert "QUANTDATA_ENABLED=1" in env
    assert "QUANTDATA_API_KEY=" in env
    assert "TASTYTRADE_CLIENT_SECRET=" in env
    assert "TASTYTRADE_REFRESH_TOKEN=" in env
    assert "estructura permanecen bajo autoridad matemática nativa" in env


def test_provider_bus_quote_survives_non_price_event_from_same_provider():
    from app.core.provider_bus import UnifiedProviderBus
    bus=UnifiedProviderBus(); now=datetime.now(timezone.utc).isoformat()
    bus.ingest(source="TASTYTRADE",symbol="DIA",event_type="QUOTE",values={"bid":532.10,"ask":532.12},timestamp=now,received_at=now)
    bus.ingest(source="TASTYTRADE",symbol="DIA",event_type="GREEKS",values={"gamma":0.01,"delta":0.52},timestamp=now,received_at=now)
    snap=bus.snapshot("DIA")
    assert snap["ready"] is True and snap["usable_providers"]==["TASTYTRADE"]
    assert 532.10 <= snap["consensus_price"] <= 532.12


def test_feature_bus_fuses_by_semantic_channel_without_provider_rank():
    from app.core.provider_bus import UnifiedFeatureBus
    bus=UnifiedFeatureBus(); now=datetime.now(timezone.utc).isoformat()
    bus.ingest(source="QUANTDATA",symbol="DIA",feature_group="OPTIONS_INTELLIGENCE",values={"directional":{"gamma":{"sign":-1,"confidence":80},"delta":{"sign":-1,"confidence":70},"flow":{"sign":-1,"confidence":75}}},timestamp=now,received_at=now,confidence=80)
    snap=bus.snapshot("DIA")
    assert snap["ready"] is True
    assert snap["weighting"]=="QUALITY_DYNAMIC_BY_OBSERVATION_NOT_PROVIDER_RANK"
    assert snap["channels"]["gamma"]["direction"]=="SELL"
    assert snap["usable_providers"]==["QUANTDATA"]


def test_scanner_source_contains_provider_feature_fusion_contract():
    sc=text("app/core/scenario_engine.py"); service=text("app/service.py")
    assert "provider_features" in sc and "_fuse_signal_channel" in sc and '"provider_feature_fusion"' in sc
    assert "FEATURE_BUS.snapshot(self.symbol)" in service and "provider_features=provider_features" in service


def test_dashboard_exposes_native_structure_not_provider_specific_overlay():
    html=text("app/templates/dashboard.html"); js=text("app/static/app.js")
    assert "ESTRUCTURA NATIVA" in html
    assert 'id="nativeStructureStatus"' in html
    assert "native_options_structure" in js


def test_provider_status_exposes_quantdata_as_corroboration_only():
    main=text("app/main.py")
    assert '@app.get("/api/providers/status")' in main
    assert '"quantdata"' in main and 'OPTIONS_INTELLIGENCE_PROVIDER' in main
    assert 'CORROBORATION_ONLY_NATIVE_MATH_RETAINS_AUTHORITY' in main
