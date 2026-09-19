from __future__ import annotations

import ast
import pathlib
import pytest

from app.core import obs
from app.core.engine import EngineConfig, engine_config_for_asset

ROOT=pathlib.Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def _clean_obs():
    obs.reset_degradations(); yield; obs.reset_degradations()

def test_config_por_activo_declara_procedencia():
    cfg=engine_config_for_asset("DIA")
    assert cfg.inputs_source in {"PER_ASSET","FALLBACK_DEFAULT_CARRY"}
    assert cfg.anchors_source in {"HISTORICAL","NONE","UNAVAILABLE"}

def test_carry_fallback_declared_and_recorded(monkeypatch):
    import app.core.precision_engine as pe
    monkeypatch.setattr(pe,"market_inputs",lambda *a,**k: (_ for _ in ()).throw(RuntimeError("macro cache broken")))
    cfg=engine_config_for_asset("NVDA")
    assert cfg.inputs_source=="FALLBACK_DEFAULT_CARRY"
    sites={x["site"] for x in obs.degradations()["sites"]}
    assert any(x.startswith("engine:asset_carry_fallback") for x in sites)
    assert obs.degradations()["by_severity"]["CRITICAL_MODEL"]>=1
    assert cfg.dividend_yield==EngineConfig().dividend_yield

def test_missing_anchors_are_observable(monkeypatch):
    import app.core.scale_anchors as sa
    monkeypatch.setattr(sa,"load_anchors",lambda *a,**k: (_ for _ in ()).throw(OSError("storage down")))
    cfg=engine_config_for_asset("DIA")
    assert cfg.anchors_source=="UNAVAILABLE"
    assert any(x["site"]=="engine:scale_anchors_unavailable" for x in obs.degradations()["sites"])

def test_unreadable_env_is_not_same_as_missing_credentials(monkeypatch,tmp_path):
    from app.core import alpaca_data
    p=tmp_path/".env"; p.write_text("X=1")
    orig=pathlib.Path.read_text
    def broken(self,*a,**k):
        if self==p: raise PermissionError("denied")
        return orig(self,*a,**k)
    monkeypatch.setattr(pathlib.Path,"read_text",broken)
    assert alpaca_data._parse_env(p)=={}
    assert any(x["site"]=="alpaca_data:env_unreadable" for x in obs.degradations()["sites"])

def test_sql_count_failure_is_observable(tmp_path):
    from app.core import live_validation
    p=tmp_path/"x.sqlite"; p.write_text("not sqlite")
    assert live_validation._sql_count(p,"SELECT COUNT(*) FROM no_table")==0
    assert any(x["site"]=="live_validation:sql_count_failed" for x in obs.degradations()["sites"])

def test_freshness_exception_stays_fail_closed_and_observable():
    from app.core.freshness import publication_allowed
    class Weird(dict):
        def get(self,*a,**k): raise RuntimeError("corrupt report")
    w=Weird(); dict.__setitem__(w,"circuito_frescura",{})
    assert publication_allowed(w) is False
    assert any(x["site"]=="freshness:publication_gate_exception" for x in obs.degradations()["sites"])

def test_health_exposes_degradations(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("ITM_ACCESS_TOKEN","")
    obs.note("test:critical",RuntimeError("boom"),severity="CRITICAL_DATA")
    r=TestClient(app).get("/health")
    assert r.status_code==200
    d=r.json().get("degradations") or {}
    assert d.get("status")=="CRITICAL"
    assert any(x["site"]=="test:critical" for x in d.get("sites",[]))

def test_no_new_silent_default_handlers_in_selected_decision_paths():
    # Focused regression guard: the six previously dangerous fallbacks must remain observable.
    checks={
        "app/core/engine.py":["engine:asset_carry_fallback","engine:scale_anchors_unavailable"],
        "app/core/alpaca_data.py":["alpaca_data:env_unreadable"],
        "app/core/scenario_engine.py":["scenario_engine:probability_model_unreadable"],
        "app/core/freshness.py":["freshness:publication_gate_exception"],
        "app/core/live_validation.py":["live_validation:sql_count_failed"],
        "app/core/dealer_intelligence.py":["dealer_intelligence:recent_events_query_failed"],
    }
    for rel,markers in checks.items():
        src=(ROOT/rel).read_text(encoding="utf-8")
        ast.parse(src)
        for marker in markers:
            assert marker in src, f"observable degradation marker missing: {marker}"
