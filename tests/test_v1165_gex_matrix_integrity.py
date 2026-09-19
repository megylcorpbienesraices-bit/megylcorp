from __future__ import annotations
import numpy as np
import pandas as pd

from app.core.institutional_modules import gex_matrix_figure


def _fig(rows, **kw):
    return gex_matrix_figure({"enriched": pd.DataFrame(rows), "spot": 534.2}, exp_count=6, mode="ΔGEX", **kw)


def test_delta_only_uses_common_strike_expiry_cells_real_plotly():
    t0=pd.Timestamp("2026-09-08 08:30:00")
    t1=pd.Timestamp("2026-09-08 11:00:00")
    rows=[
        {"timestamp":t0,"strike":533.,"expiration_date":"2026-09-11","signed_gex_proxy":1_000_000},
        {"timestamp":t0,"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":2_000_000},
        {"timestamp":t1,"strike":533.,"expiration_date":"2026-09-11","signed_gex_proxy":1_000_000},
        {"timestamp":t1,"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":2_000_000},
        # New strike and new expiry must never be previous=0 deltas.
        {"timestamp":t1,"strike":535.,"expiration_date":"2026-09-11","signed_gex_proxy":9_000_000},
        {"timestamp":t1,"strike":534.,"expiration_date":"2026-09-18","signed_gex_proxy":-8_000_000},
    ]
    fig=_fig(rows,baseline="OPEN")
    vals=np.asarray(fig.data[0].customdata,dtype=float)
    finite=vals[np.isfinite(vals)]
    assert finite.size == 2
    assert np.allclose(finite,0.0)
    title=str(fig.layout.title.text)
    assert "2 celdas NUEVAS sin Δ" in title
    assert "1 venc. NUEVO" in title
    # Real Plotly serialization smoke test.
    assert '"type":"heatmap"' in fig.to_json()


def test_categorical_axes_and_spot_overlay_work_in_real_plotly():
    t0=pd.Timestamp("2026-09-08 08:30:00");t1=pd.Timestamp("2026-09-08 10:00:00")
    rows=[]
    for t,v in [(t0,1_000_000),(t1,1_500_000)]:
        rows += [
            {"timestamp":t,"strike":533.,"expiration_date":"2026-09-08","signed_gex_proxy":v},
            {"timestamp":t,"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":-v},
        ]
    fig=_fig(rows)
    assert fig.layout.xaxis.type == "category"
    assert fig.layout.yaxis.type == "category"
    assert len(fig.data) >= 2  # heatmap + categorical-safe spot row scatter
    fig.to_json()


def test_open_baseline_is_only_called_open_when_near_0930_new_york():
    # September: Ecuador 08:30 == New York 09:30.
    rows=[
        {"timestamp":pd.Timestamp("2026-09-08 08:30:00"),"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":1e6},
        {"timestamp":pd.Timestamp("2026-09-08 11:00:00"),"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":2e6},
    ]
    assert "vs apertura" in str(_fig(rows,baseline="OPEN").layout.title.text).lower()
    late=[
        {"timestamp":pd.Timestamp("2026-09-08 09:17:00"),"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":1e6},
        {"timestamp":pd.Timestamp("2026-09-08 11:00:00"),"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":2e6},
    ]
    title=str(_fig(late,baseline="OPEN").layout.title.text).lower()
    assert "primer snapshot" in title
    assert "vs apertura" not in title


def test_previous_baseline_is_explicit():
    rows=[]
    for ts,v in [("2026-09-08 08:30",1e6),("2026-09-08 10:45",1.2e6),("2026-09-08 11:00",1.5e6)]:
        rows.append({"timestamp":pd.Timestamp(ts),"strike":534.,"expiration_date":"2026-09-11","signed_gex_proxy":v})
    title=str(_fig(rows,baseline="PREVIOUS").layout.title.text).lower()
    assert "snapshot previo" in title
    assert "10:45:00" in title


def test_auto_threshold_is_relative_and_does_not_change_data():
    t0=pd.Timestamp("2026-09-08 08:30");t1=pd.Timestamp("2026-09-08 10:00")
    rows=[]
    for i in range(10):
        strike=530.+i
        rows.append({"timestamp":t0,"strike":strike,"expiration_date":"2026-09-11","signed_gex_proxy":0.0})
        rows.append({"timestamp":t1,"strike":strike,"expiration_date":"2026-09-11","signed_gex_proxy":float(i+1)*100_000})
    fig=_fig(rows,text_threshold_m="AUTO")
    text=np.asarray(fig.data[0].text,dtype=object)
    assert 0 < np.count_nonzero(text != "") < text.size
    vals=np.asarray(fig.data[0].customdata,dtype=float)
    assert np.nanmax(vals) == 1.0


def test_missing_expiration_is_unavailable_not_inferred():
    df=pd.DataFrame([{"timestamp":pd.Timestamp("2026-09-08 10:00"),"strike":534.,"dte":0.1,"signed_gex_proxy":1e6}])
    fig=gex_matrix_figure({"enriched":df,"spot":534.},mode="ΔGEX")
    assert "no infiere vencimientos" in str(fig.layout.annotations[0].text).lower()
