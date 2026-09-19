from pathlib import Path
import re
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version

ROOT=Path(__file__).resolve().parents[1]


def _nextgen():
    return (ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')


def test_trace_chart_rect_geometry_method_exists_and_is_not_domrect_alias():
    js=_nextgen()
    assert ' chartRect(){' in js
    assert 'gammaLeft' in js and 'gammaRight' in js
    assert 'deltaLeft' in js and 'deltaRight' in js
    assert 'strikeLeft' in js and 'strikeRight' in js
    method=js[js.index(' chartRect(){'):js.index(' timeRange(', js.index(' chartRect(){'))]
    assert 'getBoundingClientRect' not in method
    assert 'return{' in method


def test_pointer_coordinates_use_domrect_but_layout_uses_chartrect():
    js=_nextgen()
    assert 'this.canvas.getBoundingClientRect()' in js
    assert 'r=this.chartRect()' in js
    assert 'this.clampXShift(this.chartRect())' in js


def test_render_init_is_failure_isolated_so_one_canvas_does_not_block_surface():
    js=_nextgen()
    assert 'function safeRenderInit(name,factory)' in js
    assert "safeRenderInit('TRACE'" in js
    assert "safeRenderInit('OPERATIVA TRACE'" in js
    assert "Promise.resolve().then(initSurface).catch" in js
    assert "console.error('[ITM QUANT RENDER INIT] SURFACE'" in js


def test_product_version_v1248():
    assert_version_at_least('1.26.2')
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))
