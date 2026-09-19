from pathlib import Path

import numpy as np
import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]

def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _surface_result():
    ts = pd.Timestamp("2026-09-09 10:00:00")
    return {"spot": 530.0, "enriched": pd.DataFrame([
        {"timestamp":ts,"strike":530.0,"option_type":"call","open_interest":100,"volume":40,"signed_gex_proxy":2_000_000,"option_delta_exposure_info":1_000_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.4},
        {"timestamp":ts,"strike":530.0,"option_type":"put","open_interest":70,"volume":55,"signed_gex_proxy":-1_000_000,"option_delta_exposure_info":-500_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.8},
        {"timestamp":ts,"strike":531.0,"option_type":"call","open_interest":50,"volume":20,"signed_gex_proxy":1_500_000,"option_delta_exposure_info":700_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.2},
        {"timestamp":ts,"strike":531.0,"option_type":"put","open_interest":90,"volume":10,"signed_gex_proxy":-2_000_000,"option_delta_exposure_info":-900_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.3},
    ])}


def test_net_drift_breaks_restart_gaps_and_never_connects_them():
    from app.core.institutional_modules import net_drift_pro_figure
    hist = pd.DataFrame([
        {"timestamp":"2026-09-09 08:30:00","spot":530.0,"net_delta":10_000_000,"net_gex":5_000_000,"call_delta":14_000_000,"put_delta":-4_000_000},
        {"timestamp":"2026-09-09 08:30:15","spot":530.1,"net_delta":11_000_000,"net_gex":5_500_000,"call_delta":15_000_000,"put_delta":-4_000_000},
        # Simulate closing the old build and reopening much later.
        {"timestamp":"2026-09-09 09:15:00","spot":529.0,"net_delta":4_000_000,"net_gex":1_000_000,"call_delta":8_000_000,"put_delta":-4_000_000},
    ])
    fig = net_drift_pro_figure({}, pd.DataFrame(), scope="Todas exp.", symbol="DIA", session_frame=hist)
    assert fig.layout.meta["session_continuity"] == "MERGE_DEDUPE_GAP_SEGMENTATION"
    line = fig.data[0]
    assert line.connectgaps is False
    assert any(v is None or pd.isna(v) for v in line.x)
    assert any(v is None or pd.isna(v) for v in line.y)


def test_flow_price_series_also_uses_explicit_gap_breaks():
    from app.core.institutional_modules import flow_unusual_pro_figure
    h = pd.DataFrame([
        {"timestamp":"2026-09-09 08:30:00","underlying_price":530.0},
        {"timestamp":"2026-09-09 08:30:15","underlying_price":530.1},
        {"timestamp":"2026-09-09 09:20:00","underlying_price":528.8},
    ])
    fig = flow_unusual_pro_figure(pd.DataFrame(), h, symbol="DIA")
    price = fig.data[0]
    assert price.connectgaps is False
    assert any(v is None or pd.isna(v) for v in price.x)


def test_session_memory_persists_call_put_decomposition_without_becoming_signal(tmp_path):
    from app.core.session_memory import append_session_metric, session_metric_frame
    ts = pd.Timestamp("2026-09-09 10:00:00")
    gd = {"spot":530.0,"total_signed_gex":2_000_000,"net_delta_exposure":3_000_000,"enriched":pd.DataFrame([
        {"timestamp":ts,"option_type":"call","option_delta_exposure_info":5_000_000,"signed_gex_proxy":4_000_000},
        {"timestamp":ts,"option_type":"put","option_delta_exposure_info":-2_000_000,"signed_gex_proxy":-2_000_000},
    ])}
    append_session_metric(tmp_path,"DIA",gd,{"direction":"SELL"},"ALL")
    out=session_metric_frame(tmp_path,"DIA","ALL")
    assert len(out)==1
    assert out.iloc[0]["call_delta"]==5_000_000
    assert out.iloc[0]["put_delta"]==-2_000_000
    # This file is presentation/history support; Scanner direction is only recorded, never derived here.
    assert "session_metric_frame" in text("app/core/session_memory.py")
    assert "presentation/history support only" in text("app/core/session_memory.py")


def test_cross_section_olas_and_true_peaks_share_same_metric_data():
    from app.service import _surface_slice_figure
    f=_surface_result()
    wave=_surface_slice_figure(f,"Net OI","Net","Olas")
    assert wave.data[0].type=="scatter"
    assert wave.data[0].line.shape=="spline"
    assert wave.data[0].connectgaps is False
    assert list(wave.data[0].y)==[30.0,-40.0]
    peaks=_surface_slice_figure(f,"Volumen Neto","Net","Picos")
    # Marker comes first so native/Plotly node inspection resolves the actual peak value, not the zero stem origin.
    assert peaks.data[0].mode=="markers"
    assert list(peaks.data[0].y)==[-15.0,10.0]
    assert peaks.data[1].mode=="lines" and peaks.data[1].connectgaps is False


def test_chart_state_is_authoritative_and_stale_requests_are_discarded():
    js=text("app/static/app.js")
    assert "chartRequestSeq" in js
    assert "stale response discarded" in js
    assert "backend/control mismatch" in js
    assert "chart_state" in js
    assert "surface_main_slice" in js
    assert "surfaceAltChart" in js
    assert "setSurfacePresentation" in js


def test_surface_controls_are_above_graph_and_support_all_presentations():
    html=text("app/templates/dashboard.html")
    controls=html.index('id="surfacePrimaryControls"')
    host=html.index('id="surfaceChart"')
    assert controls < host
    assert html.count('id="surfaceView"')==1
    assert html.count('id="surfaceMetric"')==1
    assert html.count('id="surfaceRenderStyle"')==1
    for token in (">Superficie<",">Barras<",">Líneas<",">Puntos<",">Picos<",">Olas<"):
        assert token in html
    assert 'id="surfaceAltChart"' in html
    for token in ("ALTURA", "COLOR", "NIEBLA", "CONTORNOS", "Q · SHADOW"):
        assert token in html


def test_native_surface_alternative_honors_spline_and_node_inspector():
    js=text("app/static/ultra_charts.js")
    assert "'surfaceAltChart'" in js
    assert "quadraticCurveTo" in js
    assert "t.line?.shape" in js
    assert "surfaceAltChart'].includes(this.id)" in js
    assert "customdata!=null" in js
    assert "Call OI" in js and "Net Vol" in js


def test_surface_presentation_switches_gpu_vs_2d_without_recomputing_scanner():
    js=text("app/static/nextgen_terminal.js")
    assert "function setSurfacePresentation" in js
    assert "NQ.surfacePresentation==='Superficie'" in js
    assert "presentation-disabled" in js
    assert "setSurfacePresentation('Superficie')" in js
    assert "SCANNER" not in js[js.index("function setSurfacePresentation"):js.index("function syncSurfaceFieldTabs")]


def test_projection_overlay_is_separate_from_render_and_scanner_owned():
    html=text("app/templates/dashboard.html")
    app=text("app/static/app.js")
    ng=text("app/static/nextgen_terminal.js")
    assert 'id="premarketRouteCanvas"' in html and 'id="premarketRouteToggle"' in html
    assert 'id="traceRouteProjection"' in html
    assert "drawProjectedRouteCanvas" in app
    assert "OVERLAY VISUAL" in ng
    assert "SCANNER ROUTE" in ng
    assert "this.payload?.decision" in ng
    assert "setRouteProjection" in ng
    # Projection is not one of the data render modes.
    render_block=html[html.index('id="surfaceRenderStyle"'):html.index('id="traceLandscapeLensWrap"')]
    assert "Proyección" not in render_block and "Ruta" not in render_block


def test_v1254_version_and_persistence_compatibility():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in text("app/config.py")
    assert "PRESENTATION_ONLY" in text("app/static/performance_core.js")
