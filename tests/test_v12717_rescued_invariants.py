from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

def read(rel: str) -> str:
    p = ROOT / rel
    assert p.exists(), f"fichero esperado ausente: {rel}"
    return p.read_text(encoding="utf-8", errors="replace")

ETIQUETAS_SIN_AUTORIDAD = (
    "SCANNER_ONLY", "DESCRIPTIVE_ONLY_SCANNER", "SHADOW_CONTEXT_ONLY",
    "MANUAL_ANALYSIS_ONLY", "PRESENTATION_AND_STARTUP_ONLY", "INTERNAL_CONTEXT_ONLY", "NONE",
)
MODULOS_SIN_AUTORIDAD = (
    "app/core/decision_intelligence.py", "app/core/quant_synthesis.py",
    "app/core/market_state_field.py", "app/core/trace_live.py",
    "app/core/flow_kinematics.py", "app/core/nextgen_terminal.py",
)

def test_only_scanner_has_directional_authority():
    missing=[]
    for rel in MODULOS_SIN_AUTORIDAD:
        txt=read(rel)
        if not any(x in txt for x in ETIQUETAS_SIN_AUTORIDAD):
            missing.append(rel)
    assert not missing, f"capas sin declaración explícita de no-autoridad: {missing}"

def test_human_readable_single_authority_contract_exists():
    sources=(read("app/service.py"), read("app/core/sophia_core.py"))
    assert any(re.search(r"[uú]nica autoridad direccional", s, flags=re.I) for s in sources)

def test_no_hidden_cross_instrument_proxy():
    svc=read("app/service.py")
    assert "proxy oculto" in svc.lower()
    marks=re.findall(r"SAME_INSTRUMENT_[A-Z_]+", svc + read("app/core/flow_intelligence.py"))
    assert marks, "same-instrument backfill contract disappeared"

def test_frontend_does_not_own_directional_engine():
    js="\n".join(read(f"app/static/{f}") for f in ("itm_chart_engine.js","ultra_charts.js","app.js"))
    for name in ("computeScannerDirection","computeGammaFlip","scoreSetup"):
        assert name not in js

def test_current_chart_stack_is_packaged():
    for f in ("itm_chart_engine.js","ultra_charts.js","app.js"):
        p=ROOT/"app/static"/f
        assert p.exists() and p.stat().st_size>1000, f"asset de chart ausente o vacío: {f}"

OPTIONAL_PREPARED_RELEASE_ASSETS = {
    "app/static/vendor/lightweight-charts.standalone.production.js",
    "app/static/vendor/LIGHTWEIGHT_CHARTS_LICENSE",
    "app/static/vendor/lightweight-charts.lock.json",
}

def test_no_test_references_missing_release_asset():
    pat=re.compile(r"['\"](app/(?:static|templates|providers|core)/[\w./-]+)['\"]")
    bad=[]
    for p in sorted((ROOT/"tests").glob("test_*.py")):
        if p.name==__file__.split('/')[-1]:
            continue
        for rel in set(pat.findall(p.read_text(encoding="utf-8",errors="replace"))):
            if not (ROOT/rel).exists() and rel not in OPTIONAL_PREPARED_RELEASE_ASSETS:
                bad.append(f"{p.name} -> {rel}")
    assert not bad, "tests huérfanos:\n"+"\n".join(sorted(bad))

def test_optional_prepared_assets_remain_fail_closed_for_final_artifact():
    verifier=read("scripts/verify_release_artifact.py")
    preparer=read("scripts/prepare_release_assets.py")
    for rel in sorted(OPTIONAL_PREPARED_RELEASE_ASSETS):
        name=pathlib.Path(rel).name
        assert name in verifier, f"artifact verifier dejó de exigir {name}"
        assert name in preparer, f"release preparer dejó de gobernar {name}"

def test_canvas_interaction_cannot_trigger_backend_actions():
    js="\n".join(read(f"app/static/{f}") for f in ("itm_chart_engine.js","ultra_charts.js","app.js","nextgen_terminal.js"))
    for forbidden in ("/api/live_scheduler/trigger","SET_TACTICAL_ALERT"):
        assert forbidden not in js

def test_live_providers_feed_shared_fabrics():
    assert "PRICE_TICK_FABRIC.ingest" in read("app/core/live_price.py")
    assert "OPTION_FLOW_FABRIC.ingest_trade" in read("app/core/option_stream.py")
    tasty=read("app/providers/tastytrade/market_data.py")
    assert "PRICE_TICK_FABRIC.ingest" in tasty and "OPTION_FLOW_FABRIC.ingest_trade" in tasty
    svc=read("app/service.py")
    assert "PRICE_TICK_FABRIC.dataframe" in svc and "OPTION_FLOW_FABRIC" in svc

def test_provider_health_distinguishes_configuration_connection_observation():
    main=read("app/main.py")
    for scope in ("configured_scope","connected_scope","observed_scope"):
        assert scope in main
    assert "QUANTDATA" in main and "active_observed_provider_scope" in main

def test_asset_without_direct_source_fails_loudly():
    svc=read("app/service.py").lower()
    idx=svc.find("proxy oculto")
    assert idx>=0
    assert "raise" in svc[max(0,idx-600):idx+250]

def test_release_manifest_cannot_hide_skips_as_audited():
    import json
    manifests=sorted(ROOT.glob("RELEASE_MANIFEST_v*.json"))
    if not manifests:
        return
    d=json.loads(manifests[-1].read_text(encoding="utf-8"))
    tests=d.get("tests") or {}
    n=int(tests.get("skipped_audited") or tests.get("skipped") or 0)
    if n:
        assert tests.get("skipped_reasons") or tests.get("skip_reasons")
