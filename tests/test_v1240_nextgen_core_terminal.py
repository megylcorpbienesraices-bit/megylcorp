from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.core.shared_ring import SequenceRing
from app.core.source_arbitration import arbitrate
from app.core.autonomous_sentinel import evaluate as sentinel_evaluate
from app.core.feed_adapters import status as feed_adapter_status, normalize_event
from app.core.nextgen_terminal import build_nextgen_trace_payload
from app.core.operational_readiness import build_operational_readiness
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]


def test_sequence_ring_commit_crc_and_overrun():
    r=SequenceRing(4)
    seqs=[r.write(f"e{i}".encode()) for i in range(6)]
    assert seqs == list(range(6))
    state,payload=r.read(5)
    assert state=="OK" and payload==b"e5"
    state,payload=r.read(0)
    assert state=="OVERRUN" and payload is None
    state,payload=r.read(6)
    assert state=="WAIT" and payload is None


def test_source_arbitration_prefers_fresh_low_latency_feed_and_records_switch():
    out=arbitrate([
        {"name":"A","status":"LIVE","latency_ms":12,"age_ms":20,"gap_rate":0.0,"sequence_ok":True,"cross_source_divergence":0.0001},
        {"name":"B","status":"LIVE","latency_ms":350,"age_ms":2000,"gap_rate":0.12,"sequence_ok":True,"cross_source_divergence":0.004},
    ], previous="B")
    assert out["selected"]=="A"
    assert out["switched"] is True
    assert out["authority"]=="SOURCE_SELECTION_ONLY"
    assert out["ranked"][0]["quality_score"] > out["ranked"][1]["quality_score"]


def test_sentinel_states_never_claim_execution_connection(monkeypatch):
    monkeypatch.setenv("ITM_SENTINEL_MAX_FEED_AGE_MS","1000")
    monkeypatch.setenv("ITM_SENTINEL_MAX_CLOCK_OFFSET_NS","1000")
    green=sentinel_evaluate(feed_age_ms=10,latency_p95_ms=10,clock_offset_ns=10,broker_health="LIVE",model_health=95)
    assert green["state"]=="GREEN" and green["execution_connected"] is False
    halt=sentinel_evaluate(feed_age_ms=5000,latency_p95_ms=10,clock_offset_ns=10,broker_health="LIVE",model_health=95)
    assert halt["state"]=="HALT" and halt["recommended_action"]=="HALT_NEW_ORDERS"
    lock=sentinel_evaluate(feed_age_ms=5000,latency_p95_ms=10,clock_offset_ns=5000,broker_health="LIVE",model_health=95)
    assert lock["state"]=="LOCK"
    assert "EXECUTION_ADAPTER" in lock["recommended_action"]


def test_feed_adapter_catalog_is_truthful_and_normalizes_event(monkeypatch):
    monkeypatch.delenv("ITM_OPRA_DIRECT_ACTIVE",raising=False)
    monkeypatch.delenv("ITM_OPRA_DIRECT_ENDPOINT",raising=False)
    s=feed_adapter_status()
    rows={r["key"]:r for r in s["adapters"]}
    assert rows["alpaca_ws"]["transport"]=="WEBSOCKET/HTTPS"
    # v1.27.1: `opra_direct` y `udp_direct` se RETIRARON del catálogo en v1.27.0.
    # No eran adaptadores implementados, solo placeholders. La intención de estas
    # dos líneas —que nada se declare ACTIVE sin estar configurado— se comprueba
    # ahora para TODOS los adaptadores, no para dos nombrados a mano, en:
    #     tests/test_v1271_provider_truthfulness.py
    assert all(r["configured"] for r in s["adapters"] if r["state"] == "ACTIVE")
    assert "opra_direct" not in rows and "udp_direct" not in rows
    ev=normalize_event(source="TEST",symbol="DIA",event_type="TRADE",event_time="2026-09-08T10:00:00Z",payload={"price":530.0},source_seq=7)
    assert ev.symbol=="DIA" and ev.source_seq==7 and ev.payload["price"]==530.0



def test_trace_payload_carries_native_structure_but_scanner_remains_authority():
    native={"ready":True,"status":"OK","authority":"ITM_QUANT_NATIVE_OPTIONS_STRUCTURE",
            "provider_dependency":"NONE","gamma_flip":530.5,"major_pos_oi":532.0,"major_neg_oi":528.0}
    out=build_nextgen_trace_payload(
        symbol="DIA",gd={"spot":530.0,"source":"TEST","enriched":pd.DataFrame()},
        scanner={"direction":"BUY","edge_state":"ARMED"},market_state={},ticks=pd.DataFrame(),option_events=pd.DataFrame(),
        native_options_structure=native,
    )
    assert out["native_options_structure"]["authority"]=="ITM_QUANT_NATIVE_OPTIONS_STRUCTURE"
    assert out["native_options_structure"]["provider_dependency"]=="NONE"
    assert out["provenance"]["direction_authority"]=="SCANNER_ONLY"
    assert out["decision"]["direction"]=="BUY"

def test_operational_readiness_exposes_new_core_boundaries():
    out=build_operational_readiness(source_health={"fusion_sources":[{"name":"A","status":"LIVE","latency_ms":5,"age_ms":10}]})
    caps=out["capabilities"]
    for key in ("precision_clock","simd_dispatch","source_arbitration","autonomous_sentinel","shared_ring_ipc","feed_adapters"):
        assert key in caps
    assert out["sentinel"]["execution_connected"] is False
    assert out["source_arbitration"]["selected"]=="A"


def test_dashboard_contains_nextgen_surface_and_native_structure_contracts():
    html=(ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8")
    required=[
        "Surface 4D","opItmStructuralFlowCard","opItmGexFlow","opItmDexFlow","opItmConvexity",
        "opItmFlowCanvas","nativeStructureStatus","nativeStructureDetail","surfaceItmGexFlow",
        "surfaceStrikeSliceCanvas","surfaceHeatCanvas","surfaceExpiryBarsCanvas","techRender",
    ]
    for token in required:
        assert token in html
    assert 'id="nativeStructureStatus"' in html and 'id="nativeStructureDetail"' in html


def test_frontend_native_renderers_have_structural_hud_diagnostics_and_perf():
    js=(ROOT/"app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    for token in ("drawSurfaceDiagnostics","initRenderTelemetry","ULTRA NATIVE TERMINAL"):
        assert token in js
    assert "native_options_structure" in js
    ultra=(ROOT/"app/static/ultra_charts.js").read_text(encoding="utf-8")
    for cid in ("flowChart","flowProChart","gexMatrixChart","printsChart","netDriftChart","exposureChart"):
        assert cid in ultra


def test_contextual_help_explains_native_structure_and_surface_diagnostics():
    js=(ROOT/"app/static/section_help.js").read_text(encoding="utf-8")
    assert "calcula nativamente en ITM QUANT" in js
    assert "Quant Data aporta corroboración" in js
    assert "corte por strike" in js


def test_webgpu_surface_has_real_wireframe_contours_and_confidence_fog():
    js=(ROOT/"app/static/webgpu_surface.js").read_text(encoding="utf-8")
    for token in ("line-list","wireBuf","contourBuf","marching-squares","confidence fog","terrain + wireframe + contours"):
        assert token in js
    assert "WebGL fallback" in js


def test_rust_core_sources_have_safe_ring_clock_sentinel_arbitration_and_gex_formula():
    rroot=ROOT/"rust/causality_engine/src"
    ring=(rroot/"ring.rs").read_text(encoding="utf-8")
    for token in ("sequence","generation","committed","crc","OVERRUN"):
        assert token.lower() in ring.lower()
    main=(rroot/"main.rs").read_text(encoding="utf-8")
    for token in ("mod ring","mod clock","mod sentinel","mod source_arbitration","mod simd"):
        assert token in main
    assert 'env!("CARGO_PKG_VERSION")' in main
    assert 'release identity follows Cargo package metadata' in main
    simd=(rroot/"simd.rs").read_text(encoding="utf-8")
    assert "gamma*oi*multiplier*spot*spot*0.01" in simd.replace(" ","")
    assert "strike * gamma * spot" not in simd.lower()
    provider=(rroot/"provider.rs").read_text(encoding="utf-8")
    for token in ("AlpacaWebSocketAdapter","DatabentoAdapter","OPRAFeedAdapter","CMEAdapter","UDPDirectAdapter","ReplayAdapter"):
        assert token in provider


def test_solid_shell_is_aggregate_ui_only_and_versioned():
    src=(ROOT/"frontend/solid-shell/src/main.tsx").read_text(encoding="utf-8")
    readme=(ROOT/"frontend/solid-shell/README.md").read_text(encoding="utf-8")
    package=json.loads((ROOT/"frontend/solid-shell/package.json").read_text(encoding="utf-8"))
    current=(ROOT/"VERSION.txt").read_text(encoding="utf-8").strip()
    assert "itmq:aggregate-ui" in src
    assert "tick" not in src.lower() or "tick" in readme.lower()
    assert "never enter solidjs/dom" in readme.lower()
    assert package["version"] == current


def test_version_file_is_1240():
    assert_version_at_least('1.24.0')
def test_runtime_version_boundaries_use_single_current_release_source():
    main=(ROOT/"app/main.py").read_text(encoding="utf-8")
    config=(ROOT/"app/config.py").read_text(encoding="utf-8")
    service=(ROOT/"app/service.py").read_text(encoding="utf-8")
    wasm=(ROOT/"rust/wasm_bridge/Cargo.toml").read_text(encoding="utf-8")
    current=(ROOT/"VERSION.txt").read_text(encoding="utf-8").strip()
    assert "APP_VERSION" in main
    assert "bootstrap_persistence(BASE_DIR, APP_VERSION)" in config
    assert "APP_VERSION" in service
    assert f'version = "{current}"' in wasm
    assert '"version": _release_version()' in main
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in config


def test_solid_shell_is_wired_to_native_aggregate_event_bridge():
    js=(ROOT/"app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    assert "function dispatchAggregateUI" in js
    assert "itmq:aggregate-ui" in js
    assert "updateStateRail(p);dispatchAggregateUI(p);" in js
    src=(ROOT/"frontend/solid-shell/src/main.tsx").read_text(encoding="utf-8")
    assert "itmq:aggregate-ui" in src


def test_v124_design_tokens_are_concrete_and_complete():
    import re
    css=(ROOT/"app/static/app.css").read_text(encoding="utf-8")
    assert not re.search(r"--([\w-]+)\s*:\s*var\(--\1\)", css)
    defs=set(re.findall(r"--(v124-[\w-]+)\s*:", css))
    uses=set(re.findall(r"var\(--(v124-[\w-]+)\)", css))
    assert not (uses-defs), f"undefined v1.24 tokens: {sorted(uses-defs)}"
    for token in ("v124-cyan-soft","v124-violet-soft","v124-title","v124-deep","v124-trace"):
        assert token in defs
