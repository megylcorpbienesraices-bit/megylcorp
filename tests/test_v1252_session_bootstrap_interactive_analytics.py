from pathlib import Path
import threading

import numpy as np
import pandas as pd
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]


def test_alpaca_session_backfill_uses_ny_rth_and_session_anchored_3m(monkeypatch):
    from app.core import alpaca_data

    rows = [
        {"t":"2026-09-09T13:29:00Z","o":99,"h":99,"l":99,"c":99,"v":1,"n":1,"vw":99},
        {"t":"2026-09-09T13:30:00Z","o":100,"h":101,"l":99.5,"c":100.5,"v":10,"n":2,"vw":100.2},
        {"t":"2026-09-09T13:31:00Z","o":100.5,"h":102,"l":100,"c":101.5,"v":20,"n":3,"vw":101.0},
        {"t":"2026-09-09T13:32:00Z","o":101.5,"h":103,"l":101,"c":102.0,"v":30,"n":4,"vw":102.0},
        {"t":"2026-09-09T13:33:00Z","o":102.0,"h":102.5,"l":101.5,"c":102.2,"v":40,"n":5,"vw":102.1},
    ]
    monkeypatch.setattr(alpaca_data, "_get_json", lambda *a, **k: {"bars": rows})
    settings = alpaca_data.AlpacaSettings("k", "s", stock_feed="sip")
    out = alpaca_data.fetch_stock_session_bars(settings, "DIA", "3m", "2026-09-09")
    assert len(out) == 2
    assert out.iloc[0]["timestamp"].hour == 8 and out.iloc[0]["timestamp"].minute == 30  # Ecuador clock
    assert out.iloc[0]["open"] == 100
    assert out.iloc[0]["high"] == 103
    assert out.iloc[0]["low"] == 99.5
    assert out.iloc[0]["close"] == 102.0
    assert out.iloc[0]["volume"] == 60


def test_platform_trace_session_bootstrap_contract_and_cache(monkeypatch):
    from app import service

    frame = pd.DataFrame([
        {"timestamp":pd.Timestamp("2026-09-09 08:30:00"),"open":530.0,"high":531.0,"low":529.5,"close":530.5,"volume":1000,"trades":12,"vwap":530.3},
        {"timestamp":pd.Timestamp("2026-09-09 08:31:00"),"open":530.5,"high":531.2,"low":530.2,"close":531.0,"volume":1200,"trades":14,"vwap":530.8},
    ])
    calls = {"n": 0}
    def fake_fetch(**kwargs):
        calls["n"] += 1
        return frame.copy()
    monkeypatch.setattr(service.alpaca_data, "fetch_stock_session_bars", fake_fetch)
    obj = service.PlatformState.__new__(service.PlatformState)
    obj.lock = threading.RLock(); obj.symbol = "DIA"; obj.mode = "LIVE"; obj.trace_session_cache = {}
    out = obj.trace_session_bootstrap("1m")
    assert out["ready"] is True
    assert out["timezone"] == "America/New_York"
    assert out["open"] == "09:30:00"
    assert out["handoff"] == "BACKFILL→MERGE→DEDUPE→LIVE_SIP"
    assert len(out["bars"]) == 2
    again = obj.trace_session_bootstrap("1m")
    assert calls["n"] == 1
    assert again["bars"] == out["bars"]


def _surface_result():
    ts = pd.Timestamp("2026-09-09 10:00:00")
    return {"spot": 530.0, "enriched": pd.DataFrame([
        {"timestamp":ts,"strike":530.0,"option_type":"call","open_interest":100,"volume":40,"signed_gex_proxy":2_000_000,"option_delta_exposure_info":1_000_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.4},
        {"timestamp":ts,"strike":530.0,"option_type":"put","open_interest":70,"volume":55,"signed_gex_proxy":-1_000_000,"option_delta_exposure_info":-500_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.8},
        {"timestamp":ts,"strike":531.0,"option_type":"call","open_interest":50,"volume":20,"signed_gex_proxy":1_500_000,"option_delta_exposure_info":700_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.2},
        {"timestamp":ts,"strike":531.0,"option_type":"put","open_interest":90,"volume":10,"signed_gex_proxy":-2_000_000,"option_delta_exposure_info":-900_000,"calc_vanna":.1,"calc_charm":.2,"calc_speed":.3,"activity_ratio":.3},
    ])}


def test_cross_section_net_oi_net_volume_and_true_peak_render():
    from app.service import _surface_slice_figure
    f = _surface_result()
    oi = _surface_slice_figure(f, "Net OI", "Net", "Barras")
    assert list(oi.data[0].y) == [30.0, -40.0]
    assert oi.layout.meta["metric"] == "Net OI"
    vol = _surface_slice_figure(f, "Volumen Neto", "Net", "Picos")
    assert vol.layout.meta["render"] == "Picos"
    # True stems: each strike contributes x,x,None and y=0,value,None.
    assert len(vol.data) == 2
    assert vol.data[0].mode == "markers" and vol.data[1].mode == "lines"
    assert list(vol.data[0].y) == [-15.0, 10.0]
    assert list(vol.data[1].y[:3]) == [0, -15.0, None]


def test_exposure_cube_net_oi_and_net_volume_semantics():
    from app.core.trace_analytics import exposure_cube
    ts = pd.Timestamp("2026-09-09 10:00:00")
    h = pd.DataFrame([
        {"timestamp":ts,"strike":530,"expiration_date":"2026-09-11","option_type":"call","open_interest":100,"volume":40,"underlying_price":530},
        {"timestamp":ts,"strike":530,"expiration_date":"2026-09-11","option_type":"put","open_interest":70,"volume":55,"underlying_price":530},
    ])
    oi = exposure_cube(h, "Net OI", "net", spot=530)
    vol = exposure_cube(h, "Volumen Neto", "net", spot=530)
    assert oi["ready"] and float(np.asarray(oi["z"])[0,0]) == 30.0
    assert vol["ready"] and float(np.asarray(vol["z"])[0,0]) == -15.0
    assert oi["signed"] is True and vol["signed"] is True


def test_v1252_frontend_contract_has_bootstrap_multirender_and_node_inspector():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    app = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    ng = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    ultra = (ROOT / "app/static/ultra_charts.js").read_text(encoding="utf-8")
    for token in ("surfaceSliceMetric", "surfaceSliceRender", "nodeInspector", "Net OI", "Volumen Neto", "Picos"):
        assert token in html
    assert "/api/trace/session-bootstrap" in ng
    assert "setSessionBootstrap" in ng and "mergeSessionCandles" in ng
    assert "BACKFILL" in ng and "new WebSocket" not in ng
    assert "surface_slice_metric=" in app and "surface_slice_render=" in app
    assert "itmq:node-inspect" in ultra and "itmq:node-inspect" in app
    assert "clickPoint" in ultra and "nearestPoints" in ultra


def test_v1252_version_and_persistence_compatibility():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    cfg = (ROOT / "app/config.py").read_text(encoding="utf-8")
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in cfg
