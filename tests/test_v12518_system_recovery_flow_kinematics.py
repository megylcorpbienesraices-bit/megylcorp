from __future__ import annotations

from datetime import date
import inspect
from pathlib import Path

import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least


ROOT = Path(__file__).resolve().parents[1]
def test_release_identity_v12518():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')


def test_replay_context_exposes_derived_mode_without_corrupting_replay():
    from app.core.replay import ReplayContext
    assert ReplayContext.live("DIA").mode == "LIVE"
    assert ReplayContext.at("2026-09-10", None, "QQQ").mode == "REVIEW"
    assert ReplayContext.at("2026-09-10", "2026-09-10T10:42:00", "SPY").mode == "REPLAY"
    # It is a derived property, not a dataclass field that would default replay to LIVE.
    assert "mode" not in ReplayContext.__dataclass_fields__


def test_generic_stock_helpers_keep_rth_contract_while_trace_explicitly_requests_full_day():
    from app.core import alpaca_data
    # Generic historical helpers keep the long-standing RTH default so non-TRACE consumers
    # do not silently change semantics. TRACE/CHART presentation requests full_day explicitly.
    assert inspect.signature(alpaca_data.load_cached_stock_session_bars).parameters["session_scope"].default == "rth"
    assert inspect.signature(alpaca_data.fetch_stock_session_bars).parameters["session_scope"].default == "rth"
    service=(ROOT/'app/service.py').read_text(encoding='utf-8')
    assert 'scope=str(session_scope or "full_day").lower()' in service
    assert 'session_scope="full_day"' in service


def test_london_reference_is_dst_aware_and_does_not_claim_synthetic_bars():
    from app.service import PlatformState
    segs = PlatformState._session_reference_segments(date(2026, 9, 10))
    london = next(x for x in segs if x["label"] == "LONDON WINDOW")
    # 08:00 London is 03:00 New York on this date; computed through zoneinfo rather than hard-coded.
    assert "T03:00:00" in london["start_ny"]
    assert london["role"] == "REFERENCE_WINDOW_ONLY"


def test_net_drift_keeps_sparse_real_gamma_delta_snapshots_visible_and_breaks_long_holes():
    from app.core.institutional_modules import net_drift_pro_figure
    sf = pd.DataFrame([
        {"timestamp":"2026-09-10 08:00:00","spot":525.0,"call_delta":10e6,"put_delta":-6e6,"net_delta":4e6,"net_gex":-3e6},
        {"timestamp":"2026-09-10 08:05:00","spot":525.2,"call_delta":11e6,"put_delta":-6.5e6,"net_delta":4.5e6,"net_gex":-2.5e6},
        {"timestamp":"2026-09-10 09:00:00","spot":526.0,"call_delta":13e6,"put_delta":-7e6,"net_delta":6e6,"net_gex":1e6},
    ])
    fig = net_drift_pro_figure({}, pd.DataFrame(), symbol="DIA", session_frame=sf)
    by_name = {str(t.name): t for t in fig.data}
    assert by_name["NET DELTA DRIFT"].mode == "lines+markers"
    assert by_name["GAMMA DRIFT"].mode == "lines+markers"
    assert sum(v is not None and not pd.isna(v) for v in by_name["GAMMA DRIFT"].y) == 3
    assert any(v is None or pd.isna(v) for v in by_name["GAMMA DRIFT"].y)
    assert fig.layout.meta["sparse_snapshots_visible"] is True
    assert fig.layout.meta["drift_observations"]["net_gex"] == 3


def test_flow_kinematics_is_observed_time_derivative_context_not_greek_relabeling():
    from app.core.flow_kinematics import build_flow_kinematics
    t0 = pd.Timestamp("2026-09-10 09:30:00")
    ev = pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(seconds=5*i) for i in range(8)],
        "directional_premium": [10_000,15_000,25_000,40_000,60_000,90_000,130_000,180_000],
        "premium": [10_000,15_000,25_000,40_000,60_000,90_000,130_000,180_000],
    })
    px = pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(seconds=5*i) for i in range(8)],
        "price": [525.0 + .01*i for i in range(8)],
    })
    out = build_flow_kinematics(ev, px, symbol="DIA", bucket_seconds=5)
    assert out["ready"] is True
    assert out["authority"] == "SHADOW_CONTEXT_ONLY"
    assert out["semantics"] == "OBSERVED_FLOW_DYNAMICS_NOT_PHYSICAL_INSTITUTIONAL_INERTIA"
    assert out["latest"]["flow_velocity_usd_s"] > 0
    assert out["latest"]["flow_acceleration_usd_s2"] > 0
    assert out["latest"]["flow_jerk_usd_s3"] > 0
    assert out["latest"]["state"] == "FLOW_ACCELERATING_BUY"
    assert "not Delta/Gamma/Speed" in out["disclosure"]


def test_flow_kinematics_api_is_read_only_context():
    from fastapi.testclient import TestClient
    import app.main as main
    r = TestClient(main.app).get("/api/nextgen/flow-kinematics")
    assert r.status_code == 200
    body = r.json()
    assert body["authority"] == "SHADOW_CONTEXT_ONLY"


def test_tactical_ui_dark_from_frame_zero_and_existing_selector_is_reused():
    css=(ROOT/'app/static/app.css').read_text(encoding='utf-8')
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    assert '--terminal-canvas:#0b0e14' in css
    assert 'TACTICAL UI / DARK-FROM-FRAME-0' in css
    assert 'id="quickAssetSelect" class="lean-hidden"' in html
    assert 'id="symbolSearchModal"' in html and 'Instrumentos · ETF · futuros · índices' in html
    assert '/static/app.css?v={{ app_version }}' in html
    assert html.count('id="quickAssetSelect"') == 1


def test_visual_motion_is_presentation_only_bounded_and_accessible():
    js=(ROOT/'app/static/itm_chart_engine.js').read_text(encoding='utf-8')
    css=(ROOT/'app/static/app.css').read_text(encoding='utf-8')
    assert "prefers-reduced-motion: reduce" in js
    assert '@media(prefers-reduced-motion:reduce)' in css
    assert 'LOD VISUAL · DATOS CRUDOS INTACTOS' in js
    assert 'this.ripples.length>24' in js
    assert 'this.seenPrints' in js
    assert "ctx.fillStyle='#0b0e14'" in js
    assert "ctx.fillStyle='#ffffff'" not in js
    assert 'HIDRATANDO HISTÓRICO · LIVE CONTINÚA' in js


def test_ultra_charts_use_visual_only_lod_and_controlled_drift_bloom():
    js=(ROOT/'app/static/ultra_charts.js').read_text(encoding='utf-8')
    assert "this.host.dataset.lod='VISUAL_ONLY'" in js
    assert 'GAMMA DRIFT|NET DELTA DRIFT|CALL DRIFT|PUT DRIFT' in js
    assert 'c.shadowBlur=2+5*rel' in js
    assert "if(k==='yaxis'&&this.hover.y>=f.t&&this.hover.y<=f.b)" in js
    assert "endDrag(){this.dragState=null;this.canvas.style.cursor='none'}" in js


def test_solid_shell_is_low_frequency_ecosystem_control_not_a_second_data_engine():
    src=(ROOT/'frontend/solid-shell/src/main.tsx').read_text(encoding='utf-8')
    bridge=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    appjs=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
    assert 'function EcosystemSelector' in src
    assert "itmq:select-asset" in src and "itmq:select-asset" in appjs
    assert 'assets:Array.isArray(p?.assets)?p.assets:[]' in bridge
    assert '/api/v1/ecosystems' not in src
    assert 'fetch(' not in src  # no parallel feed/API path in Solid control shell


def test_trace_crosshair_is_canvas_native_smoothed_but_reduced_motion_safe():
    js=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert 'this.crossTarget=null;this.crossFrame=0;this.motionReduced=' in js
    assert 'scheduleCrosshair()' in js
    assert "prefers-reduced-motion: reduce" in js
    assert "c.fillRect(r.strikeLeft+3,this.cross.y-10,pw,20)" in js
