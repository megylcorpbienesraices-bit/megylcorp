"""v1.39.4 · chart visibility and line-terminal contracts."""
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding="utf-8",errors="replace")


def test_after_hours_cash_is_not_strict_red_feed_requirement():
    from app.core.session_expectations import activity_expectation
    # 2026-09-14 21:28 UTC = 17:28 New York: after-hours, sparse DIA prints are normal.
    x=activity_expectation("DIA", datetime(2026,9,14,21,28,tzinfo=timezone.utc))
    assert x["session_phase"] == "AFTER_HOURS"
    assert x["expected_price_live"] is False
    assert x["price_expectation"] == "LIVE_IF_ACTIVE"
    assert x["display_state"] == "STRUCTURAL"


def test_freshness_gate_blocks_actionability_not_chart_visibility():
    main=text("app/main.py"); app=text("app/static/app.js"); css=text("app/static/app.css")
    charts=main[main.index('@app.get("/api/charts")'):main.index('@app.get("/api/charts/surface-slice")')]
    assert '_publication_blocked_payload("CHARTS")' not in charts
    assert 'chart_publication_gate=_publication_gate_snapshot()' in charts
    assert 'payload["context_only"]' in charts
    assert 'async function loadCharts(){if(publicationBlocked)return;' not in app
    assert 'async function loadTablesIfNeeded(){if(publicationBlocked' not in app
    assert 'filter:blur(5px)' not in css
    assert 'NO ACCIONABLE · CONTEXTO VISIBLE' not in css


def test_noncritical_structural_state_has_no_intrusive_popup():
    app=text("app/static/app.js")
    block=app[app.index('function renderFreshnessGate'):app.index('function renderState')]
    assert "publicationCritical=publicationBlocked&&expectedLive" in block
    assert "freshnessGateBanner" in block and ".remove()" in block
    assert "innerHTML" not in block and "ABRIR HISTÓRICO" not in block


def test_unusual_flow_main_chart_is_reference_multipane_terminal():
    from app.core.institutional_modules import flow_unusual_pro_figure
    ts=pd.date_range("2026-09-14 09:30",periods=5,freq="min")
    hist=pd.DataFrame({"timestamp":ts,"underlying_price":[500,500.2,500.1,500.4,500.3]})
    events=pd.DataFrame({
        "timestamp":[ts[1],ts[3]],"premium":[250000,400000],"direction_sign":[1,-1],
        "flow_score":[82,91],"underlying_price":[500.2,500.4],"strike":[501,499],"option_type":["call","put"]
    })
    cur=pd.DataFrame({"strike":[498,499,501,502],"signed_gex":[-2e6,-3e6,4e6,2e6],"gross_gex":[2e6,3e6,4e6,2e6]})
    fig=flow_unusual_pro_figure(events,hist,{"spot":500.3,"current_delta":cur,"gamma_flip":500.0,"gamma_flip_crossing":True},None,"DIA")
    assert fig.layout.meta["renderer_intent"] == "UNUSUAL_FLOW_REFERENCE_TERMINAL"
    assert fig.layout.meta["bar_traces"] is True
    assert fig.layout.meta["panes"] == ("PRICE","AGGRESSOR","TOTAL","NET_FLOW") or list(fig.layout.meta["panes"]) == ["PRICE","AGGRESSOR","TOTAL","NET_FLOW"]
    roles={str((t.meta or {}).get("role","")) for t in fig.data}
    for role in ("UNDERLYING_PRICE","FLOW_EVENT","AGGRESSOR_BAR","TOTAL_BAR","NET_FLOW_BAR","LEVEL_LINE"):
        assert role in roles
    names={str(t.name) for t in fig.data}
    for name in ("CALL WALL","GAMMA FLIP","PUT WALL","Punto exacto de entrada"):
        assert name in names
    assert fig.layout.dragmode == "pan" and fig.layout.hovermode == "x unified"


def test_net_drift_main_chart_is_line_only_and_contains_core_components():
    from app.core.institutional_modules import net_drift_pro_figure
    ts=pd.date_range("2026-09-14 09:30",periods=4,freq="min")
    sf=pd.DataFrame({
        "timestamp":ts,"spot":[500,500.1,500.2,500.3],
        "call_delta":[10e6,11e6,12e6,13e6],"put_delta":[-8e6,-8.4e6,-8.8e6,-9e6],
        "net_delta":[2e6,2.6e6,3.2e6,4e6],"net_gex":[5e6,5.2e6,5.6e6,6e6],
    })
    ev=pd.DataFrame({"timestamp":[ts[1],ts[2]],"premium":[200000,300000],"direction_sign":[1,-1]})
    ph=pd.DataFrame({"timestamp":ts,"underlying_price":[500,500.1,500.2,500.3]})
    fig=net_drift_pro_figure({},ev,symbol="DIA",session_frame=sf,price_history=ph)
    assert fig.layout.meta["renderer_intent"] == "LINE_TERMINAL_TRACE_STYLE"
    assert fig.layout.meta["bar_traces"] is False
    assert all(str(t.type).lower() != "bar" for t in fig.data)
    names={str(t.name) for t in fig.data}
    for name in ("CALL DRIFT","PUT DRIFT","NET DELTA DRIFT","GAMMA DRIFT","Q-FLOW CUM","DIA"):
        assert name in names


def test_requested_charts_remain_wired_and_navigable():
    html=text("app/templates/dashboard.html"); js=text("app/static/ultra_charts.js")
    for cid in ("operativaTraceChart","scannerChart","flowProChart","netDriftChart","macroChart"):
        assert f'id="{cid}"' in html
    assert "'scannerChart','macroChart'" in js
    assert html.count('id="section-exposure"') == 1
    flow=html.split('id="section-flow"',1)[1].split('id="section-netdrift"',1)[0]
    assert 'line-terminal-chart' in flow and 'barras = prima/notional' not in flow
