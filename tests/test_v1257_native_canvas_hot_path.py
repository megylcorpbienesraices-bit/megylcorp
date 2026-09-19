from pathlib import Path
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version
ROOT=Path(__file__).resolve().parents[1]

def text(p):
    return (ROOT/p).read_text(encoding='utf-8')

def test_version_and_cache_bust():
    assert_version_at_least('1.26.2')
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))
    # v1.27.3: la versión venía incrustada en la URL del asset, un patrón que el
    # guardia de asserts de versión no cazaba. El dashboard ya usa {{ app_version }};
    # se comprueba con el helper que tú mismo añadiste.
    assert '/static/ultra_charts.js?v={{ app_version }}' in text('app/templates/dashboard.html')

def test_ultra_canvas_uses_cached_base_and_overlay_hover():
    js=text('app/static/ultra_charts.js')
    assert "this.baseCanvas=document.createElement('canvas')" in js
    assert 'this.baseDirty=true' in js
    assert 'c.drawImage(this.baseCanvas' in js
    assert "if(this.hover)this.drawHover(c)" in js
    assert 'desynchronized:true' in js

def test_gap_segmentation_is_time_aware():
    js=text('app/static/ultra_charts.js')
    assert 'function inferGapMs' in js
    assert "this.xMode==='time'" in js
    assert 'gap_threshold_ms' in js
    assert 'x-prevX>gapMs' in js

def test_visible_range_y_autoscale_and_pixel_width():
    js=text('app/static/ultra_charts.js')
    assert 'updateAxisBounds()' in js
    assert 'x<this.xmin||x>this.xmax' in js
    assert 'function pixelSpacing' in js
    assert 'pxStep=pixelSpacing' in js

def test_trace_ring_buffer_is_circular_and_price_line_breaks_gaps():
    js=text('app/static/nextgen_terminal.js')
    assert 'this.buf=new Array(this.capacity)' in js
    assert 'this.start=(this.start+1)%this.capacity' in js
    assert 'traceGapThresholdMs()' in js
    assert 't-prevT>gapMs' in js
    assert 'desynchronized:true' in js

def test_windows_webgpu_warning_removed():
    js=text('app/static/webgpu_surface.js')
    assert 'requestAdapter()' in js
    assert "powerPreference:'high-performance'" not in js

def test_performance_authority_unchanged():
    js=text('app/static/performance_core.js')
    assert "authority:'PRESENTATION_ONLY'" in js
    assert "version:(window.ITMQ_VERSION||'unknown')" in js
