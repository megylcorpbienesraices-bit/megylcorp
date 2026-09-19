"""v1.39.4 · NextGen cleanup contracts."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def test_release_identity_is_1370_and_equity_hub_is_preserved():
    version = text("VERSION.txt").strip()
    marker = json.loads(text(".itm_quant_product.json"))
    assert marker["version"] == version
    html = text("app/templates/dashboard.html")
    main = text("app/main.py")
    service = text("app/service.py")
    assert "EQUITY HUB" in html
    assert 'data-section="equityhub"' in html
    assert '"equity_hub"' in main
    assert "equity_hub_monte_carlo" in main
    assert "_equity_hub_gamma_model_figure" in service
    assert "_monte_carlo_figure" in service


def test_legacy_trace_and_auth_files_are_physically_removed():
    assert not (ROOT / "app/db.py").exists()
    assert not (ROOT / "app" / "static" / "spot_trace_workspace.js").exists()
    visuals = text("app/core/advanced_visuals.py")
    for name in (
        "trace_pro", "trace_fusion", "trace_2d", "surface_3d",
        "surface_3d_filtered", "pressure_map", "trace_levels_table", "exposure_3d",
    ):
        assert f"def {name}(" not in visuals


def test_chart_router_no_longer_builds_legacy_trace_or_primary_surface():
    main = text("app/main.py")
    service = text("app/service.py")
    assert '"trace":{"trace_orderflow","flow_pro"}' in main.replace(" ", "")
    assert '"surface":{"trace_landscape","surface_slice","surface_main_slice"}' in main.replace(" ", "")
    assert "trace_pro(" not in service
    assert "trace_fusion(" not in service
    assert 'result["surface"]' not in service


def test_dashboard_has_11_primary_destinations_and_grouped_subnavigation():
    html = text("app/templates/dashboard.html")
    assert html.count('class="nav-btn') == 11
    assert 'id="flowGroupNav"' in html
    assert 'data-lean-go="netdrift"' in html
    assert 'data-lean-go="prints"' in html
    assert 'id="structureGroupNav"' in html
    for section in ("exposure", "gexmatrix", "positioning", "surface"):
        assert f'data-lean-go="{section}"' in html


def test_dead_trace_controls_are_gone_and_nextgen_controls_remain():
    combined = text("app/templates/dashboard.html") + text("app/static/app.js")
    for dead in (
        "traceMode", "traceFusionView", "traceHeatScale", "traceForwardMinutes",
        "traceDealerFlow", "traceTimeFlow", "traceAggression",
    ):
        assert dead not in combined
    html = text("app/templates/dashboard.html")
    for live in (
        "tracePriceStyle", "traceCandle", "traceTimeWindow", "traceYFrame",
        "traceWindow", "traceTemporalHeatmap", "traceKeyLevels", "traceExpectedMove",
    ):
        assert f'id="{live}"' in html


def test_key_levels_and_expected_move_drive_nextgen_renderer_directly():
    js = text("app/static/nextgen_terminal.js")
    assert "drawExpectedMove" in js
    assert "drawLevels" in js
    assert "traceExpectedMove" in js
    assert "traceKeyLevels" in js
    assert "['traceTemporalHeatmap','traceKeyLevels','traceExpectedMove']" in js


def test_legacy_sqlite_auth_configuration_is_removed():
    cfg = text("app/config.py")
    env = text(".env.example")
    for token in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "DATABASE_PATH"):
        assert token not in cfg
        assert token not in env
    assert "SESSION_SECRET" not in cfg
    assert "ITM_SESSION_HTTPS_ONLY" in env


def test_only_current_release_evidence_is_active_at_root():
    v=text("VERSION.txt").strip()
    assert sorted(p.name for p in ROOT.glob("CHANGELOG_v*.md")) == [f"CHANGELOG_v{v}.md"]
    assert sorted(p.name for p in ROOT.glob("QUANT_ENGINE_AUDIT_v*.md")) == [f"QUANT_ENGINE_AUDIT_v{v}.md"]
    assert sorted(p.name for p in ROOT.glob("RELEASE_MANIFEST_v*.json")) == [f"RELEASE_MANIFEST_v{v}.json"]
    assert sorted(p.name for p in ROOT.glob("VALIDACION_PRE_VPS_v*.md")) == [f"VALIDACION_PRE_VPS_v{v}.md"]
