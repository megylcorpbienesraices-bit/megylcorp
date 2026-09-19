from __future__ import annotations

from pathlib import Path
import os
import pandas as pd

from app.core.operational_readiness import build_operational_readiness, latency_summary
from app.core.scenario_lab import build_scenario_lab
from app.core.quantum_ready import build_qubo
from app.core.nextgen_terminal import build_quant_surface_payload

ROOT = Path(__file__).resolve().parents[1]


def test_scenario_lab_is_shadow_not_forecast():
    r = build_scenario_lab(symbol="DIA", spot=530.0, atm_iv_pct=18.0, horizon_minutes=90, paths=500)
    assert r["ready"] is True
    assert r["state"] == "SHADOW"
    assert r["authority"] == "NONE"
    assert r["is_forecast"] is False
    assert set(r["iv_scenarios"]) == {"IV_MINUS", "BASE", "IV_PLUS"}
    assert len(r["representative_paths"]) == 25


def test_quantum_ready_uses_classical_fallback_without_qpu(monkeypatch):
    monkeypatch.delenv("ITM_QPU_PROVIDER", raising=False)
    r = build_qubo([
        {"symbol":"DIA","evidence_score":82,"data_quality":95},
        {"symbol":"SPY","evidence_score":70,"data_quality":90},
        {"symbol":"QQQ","evidence_score":76,"data_quality":88},
    ], max_positions=2)
    assert r["ready"] is True
    assert r["qpu_active"] is False
    assert r["backend"] == "CLASSICAL_EXACT_FALLBACK"
    assert r["authority"] == "NONE"
    assert len(r["selected"]) <= 2


def test_readiness_never_claims_fpga_colo_qpu_without_config(monkeypatch):
    for k in ("ITM_FPGA_ENABLED","ITM_FPGA_BRIDGE_URL","ITM_COLOCATION_REGION","ITM_QPU_PROVIDER","ITM_GENAI_SCENARIO_ENABLED","ITM_GENAI_PROVIDER","OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = build_operational_readiness(causality={}, source_health={}, external_markets={})
    caps = r["capabilities"]
    assert caps["fpga_feed_bridge"]["status"] == "READY"
    assert caps["co_location"]["status"] == "READY"
    assert caps["quantum_optimization"]["status"] == "READY"
    assert caps["direct_futures"]["status"] == "UNAVAILABLE"
    assert caps["security_posture"]["status"] == "ACTIVE"
    assert "ACTIVE means observed/configured" in r["truth_policy"]


def test_latency_summary_has_percentiles():
    c={"events":[{"latency_ms":10},{"latency_ms":20},{"latency_ms":30},{"latency_ms":40},{"latency_ms":50}]}
    r=latency_summary(c)
    assert r["count"] == 5
    assert r["p50_ms"] == 30
    assert r["p95_ms"] >= 40


def _surface_gd():
    rows=[]
    ts=pd.Timestamp("2026-09-08T14:00:00Z")
    for expiry,dte in [("2026-09-08",0.20),("2026-09-11",3.20)]:
        for strike in [528.0,529.0,530.0,531.0,532.0]:
            for typ in ["call","put"]:
                rows.append({
                    "timestamp":ts,"expiration_date":expiry,"dte":dte,"strike":strike,"iv":0.18,
                    "open_interest":1000+int(abs(strike-530)*200),"volume":200+int(abs(strike-530)*50),
                    "underlying_price":530.0,"option_type":typ,
                })
    return {"spot":530.0,"enriched":pd.DataFrame(rows)}


def test_surface_has_confidence_and_all_fields():
    r=build_quant_surface_payload(symbol="DIA",gd=_surface_gd(),dealer={},calibration={})
    assert r["ready"] is True
    assert r["version"] == (ROOT/"VERSION.txt").read_text(encoding="utf-8").strip()
    assert r["renderer"] == "ITM_QUANT_WEBGL"
    assert "confidence" in r
    assert len(r["confidence"]) == len(r["y_strikes"])
    assert set(["IV","Gamma","Delta","Vanna","Charm","Speed","Color","GEX","DEX","Hedge","Q"]).issubset(r["fields"])
    assert r["q_is_probability"] is False


def test_dashboard_uses_local_plotly_and_all_in_assets():
    h=(ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "https://cdn.plot.ly" not in h
    assert "/static/vendor/plotly.min.js" in h
    assert "/static/institutional_terminal.js" in h
    assert "surfaceImmersiveBtn" in h
    assert "ADVANCED TECH / SCENARIO / HFT READINESS" in h


def test_native_js_has_linked_strike_and_uncertainty_shader():
    nq=(ROOT/"app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    inst=(ROOT/"app/static/institutional_terminal.js").read_text(encoding="utf-8")
    assert "setExternalStrike" in nq
    assert "UNCERTAINTY" in (ROOT/"app/static/app.css").read_text(encoding="utf-8")
    assert "aConf" in nq and "uncertainty" in nq
    assert "itmq:strike-hover" in inst
    assert "CLASSICAL" in inst or "quantumBackend" in inst


def test_docs_exist():
    for name in [
        "INSTITUTIONAL_VISUAL_TERMINAL.md","HFT_INFRASTRUCTURE_READINESS.md",
        "GENERATIVE_SCENARIO_POLICY.md","QUANTUM_READY_OPTIMIZATION.md","OBSERVABILITY_SECURITY.md",
    ]:
        assert (ROOT/"docs"/name).exists(), name
    assert (ROOT/".github/workflows/ci.yml").exists()
    assert (ROOT/"monitoring/prometheus.yml").exists()
