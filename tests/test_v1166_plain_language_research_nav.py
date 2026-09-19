from __future__ import annotations
from pathlib import Path
import json
import re

import numpy as np
import pandas as pd

from app.core.plain_language import easy_view, JARGON
from app.core.institutional_research import (
    microstructure_lab, volatility_forecast_lab, cross_asset_lab,
    ml_research_readiness, institutional_research_snapshot,
)

ROOT=Path(__file__).resolve().parents[1]


def _scanner(calibrated=False, active=False, p=None):
    return {
        "ready": True,"direction":"BUY","target1":535.2,"target2":536.1,"invalidation":532.9,
        "zone":{"low":533.8,"center":534.05,"high":534.3},
        "edge_gate":{"calibration_ready":calibrated,"active":active,"probability_t1_first":p,"stage":"CALIBRATED" if calibrated else "COLLECTING"},
    }


def _flat_text(x):
    return json.dumps(x,ensure_ascii=False).lower()


def test_easy_mode_reads_nested_scanner_zone_and_keeps_uncertainty_visible():
    e=easy_view(_scanner(),{"regime":"NEGATIVE","gamma_flip":534.28,"spot":533.9},{"expected_move":2.4,"nearest_expiry":"2026-09-11"},{"state":"ABSORBED"},82,3,{})
    txt=_flat_text(e)
    assert "533.80" in txt and "534.30" in txt
    assert "aún no está validada" in txt or "todavía no" in txt
    assert "absorb" in txt
    assert "alguien está vendiendo" not in txt


def test_easy_mode_calibrated_probability_is_model_estimate_not_observed_hit_rate():
    e=easy_view(_scanner(True,True,.58),{}, {}, {"state":"CONFIRMED"},91,14,{})
    txt=_flat_text(e)
    assert "58%" in txt
    assert "modelo calibrado estima" in txt
    assert "de cada 100" not in txt
    assert "acertaron" not in txt


def test_easy_expected_move_names_actual_expiry_not_assumes_today():
    e=easy_view(_scanner(),{}, {"expected_move":1.9,"nearest_expiry":"2026-09-11"}, {},80,2,{})
    txt=_flat_text(e)
    assert "vencimiento 2026-09-11" in txt
    assert "lo normal para hoy" not in txt


def test_easy_mode_does_not_leak_blacklisted_quant_jargon():
    e=easy_view(_scanner(),{"regime":"POSITIVE","gamma_flip":534.28,"spot":534.4},{"expected_move":2.1,"nearest_expiry":"2026-09-11"},{"state":"CHURN"},75,1,{})
    txt=_flat_text(e)
    for term in JARGON:
        assert term not in txt, term


def test_microprice_and_l1_imbalance_are_observed_not_l2_invented():
    ticks=pd.DataFrame({
        "timestamp":pd.date_range("2026-09-08 10:00",periods=40,freq="s"),
        "price":np.linspace(100,100.1,40),"size":[10]*40,
        "bid":[99.9]*40,"ask":[100.1]*40,"bid_size":[30]*40,"ask_size":[10]*40,
        "signed_volume":[10,-10]*20,
    })
    m=microstructure_lab(ticks)
    assert m["affects_scanner"] is False
    assert abs(m["microprice"]-100.05)<1e-9
    assert abs(m["book_imbalance_l1"]-.5)<1e-9
    assert m["l2_metrics"]["status"] == "WAITING FOR L2"
    assert m["l2_metrics"]["cancellation_pressure"] is None


def test_vol_forecast_is_shadow_and_garch_collects_until_sample_is_large():
    ticks=pd.DataFrame({"timestamp":pd.date_range("2026-09-08 10:00",periods=70,freq="min"),"price":100*np.exp(np.cumsum(np.linspace(-.0002,.0002,70)))})
    v=volatility_forecast_lab("SPY",ticks,{"atm_iv":18.2})
    assert v["affects_scanner"] is False and v["state"] == "SHADOW"
    g=next(x for x in v["models"] if x["name"]=="GARCH(1,1)")
    assert g["status"] == "COLLECTING"


def test_cross_asset_lab_runs_pca_kalman_as_shadow_when_common_history_exists(tmp_path: Path):
    dates=pd.date_range("2026-08-20",periods=12,freq="B")
    for i,d in enumerate(dates):
        for sym,scale in [("DIA",1.0),("XLF",.15)]:
            pd.DataFrame({"timestamp":[pd.Timestamp(d)+pd.Timedelta(hours=15)],"underlying_price":[500+scale*i+0.02*i*i]}).to_csv(tmp_path/f"alpaca_{sym.lower()}_history_{d.date().isoformat()}.csv",index=False)
    x=cross_asset_lab(tmp_path,"DIA")
    assert x["state"] == "SHADOW" and x["affects_scanner"] is False
    assert x["common_sessions"] >= 8
    assert x["pca"]["first_component_explained_pct"] >= 0
    assert x["pair"]["peer"] == "XLF"
    assert np.isfinite(x["pair"]["kalman_beta"])


def test_ml_readiness_is_stricter_and_never_production_by_sample_count_alone():
    low=ml_research_readiness({"sample_size":120,"sessions":8})
    hi=ml_research_readiness({"sample_size":600,"sessions":25})
    assert low["state"] == "COLLECTING"
    assert hi["state"] == "TRAINABLE"
    assert hi["production_enabled"] is False and hi["affects_scanner"] is False


def test_research_snapshot_all_families_are_non_authoritative(tmp_path: Path):
    r=institutional_research_snapshot(tmp_path,"SPY",pd.DataFrame(),{}, {})
    assert r["role"] == "RESEARCH_SHADOW"
    for key in ("vol_forecast","microstructure","cross_asset","ml"):
        assert r[key]["affects_scanner"] is False


def test_navigation_is_15_real_entry_points_with_access_to_merged_content():
    html=(ROOT/"app/templates/dashboard.html").read_text()
    js=(ROOT/"app/static/app.js").read_text()
    navs=re.findall(r'class="nav-btn[^\"]*"[^>]*data-section="([^"]+)"',html)
    assert len(navs)==11  # v1.37: grouped top navigation; child workspaces remain accessible through subnavigation
    assert "scanner" in navs and "chain" in navs and "auditor" in navs and "surface" not in navs and "infrastructure" not in navs
    assert 'data-lean-go="surface"' in html and 'data-lean-go="netdrift"' in html
    for section,control in [("section-scanner","openScannerDetail"),("section-surface","matrixSurfaceView"),("section-infrastructure","auditSourceView")]:
        assert f'id="{section}"' in html
        assert f'id="{control}"' in html
    assert "MERGED_SECTIONS" in js
    assert "renderEasyMode" in js and "renderInstitutionalResearch" in js
    assert "body.easy-ui .full-only" in (ROOT/"app/static/app.css").read_text()
