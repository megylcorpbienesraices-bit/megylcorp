from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import plotly.graph_objects as go

from app.service import _fig_json
from app.core.binary_protocol import pack_surface_frame
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]


def test_plotly6_binary_arrays_are_decoded_for_native_renderers():
    fig = go.Figure(go.Scatter(x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0])))
    out = _fig_json(fig)
    assert out["data"][0]["x"] == [1.0, 2.0]
    assert out["data"][0]["y"] == [3.0, 4.0]


def test_plotly6_heatmap_shape_is_restored_as_nested_lists():
    fig = go.Figure(go.Heatmap(x=np.array([1, 2, 3]), y=np.array([10, 20]), z=np.arange(6, dtype=np.float32).reshape(2, 3)))
    out = _fig_json(fig)
    z = out["data"][0]["z"]
    assert z == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0]]


def test_frontend_contract_understands_plotly_bdata_and_shape():
    js = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    assert "decodePlotlyBinary" in js
    assert "typeof v.bdata!=='string'" in js
    assert "reshape(flat,dims)" in js


def test_surface_decoder_never_constructs_float32array_at_unaligned_frame_offset():
    js = (ROOT / "app/static/binary_transport.js").read_text(encoding="utf-8")
    assert "decodeSurfaceDataView" in js
    assert "getFloat32(off+i*4,true)" in js
    assert "if(LITTLE_ENDIAN&&(off&3)===0)" in js
    assert "DATAVIEW_COPY" in js
    # Gamma has a 5-byte field name, so payload starts at byte 31 (unaligned).
    raw = pack_surface_frame(field="Gamma", rows=1, cols=2, values=[[1.25, -2.5]], sequence=3)
    assert (26 + len("Gamma")) % 4 != 0
    assert len(raw) == 26 + len("Gamma") + 8


def test_wasm_probe_is_capability_based_and_does_not_blind_import():
    js = (ROOT / "app/static/binary_transport.js").read_text(encoding="utf-8")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "status?.wasm_built" in js
    assert "wasm_built" in main
    assert "state.wasm='FALLBACK'" in js


def test_trace_price_range_uses_structure_when_candles_are_empty():
    js = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    assert "frame==='strikes'" in js and "frame==='structure'" in js
    assert "for(const level of traceArray(this.payload?.levels" in js
    assert "if(spot!=null)prices.push(spot)" in js
    assert "if(!prices.length)return[0,1]" in js
    assert "if(!vis.length)return[0,1]" not in js


def test_version_1243_without_persistence_reset():
    assert_version_at_least('1.26.2')
    config = (ROOT / "app/config.py").read_text(encoding="utf-8")
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in config
    assert "persistent_data" in (ROOT / "app/persistence.py").read_text(encoding="utf-8")


def test_heatmap_axis_uses_strike_y_not_exposure_z():
    js = (ROOT / "app/static/ultra_charts.js").read_text(encoding="utf-8")
    assert "vals.push(...arr(t.y,'trace.y').filter(finite).map(Number))" in js
    assert "vals.push(...arr(t.z,'trace.z').flat().filter(finite).map(Number))" not in js
