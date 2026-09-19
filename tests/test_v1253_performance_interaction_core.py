from pathlib import Path
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version

ROOT = Path(__file__).resolve().parents[1]


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_performance_core_is_presentation_only_and_singleton():
    js = text("app/static/performance_core.js")
    assert "PRESENTATION_ONLY" in js
    assert "if(window.ITMQPerformanceCore?.runtime)" in js
    assert "class PerformanceGovernor" in js
    assert "class HotDisplayState" in js
    assert "class DirtyRegistry" in js
    assert "class DisplayEventBus" in js
    assert "class RingQueue" in js
    assert "class FrameScheduler" in js
    assert "class Float32BufferPool" in js
    assert "class VirtualListRecycler" in js
    # v1.27.3: igual que arriba, versión incrustada dentro del JS. El runtime ya
    # lee window.ITMQ_VERSION en vez de fijar un release histórico.
    assert "version:(window.ITMQ_VERSION||'unknown')" in js
    assert "window.ITMQ_VERSION" in js
    assert "/ws/market" not in js


def test_ring_queue_and_time_budget_replace_array_shift_microtask_hot_loop():
    js = text("app/static/performance_core.js")
    assert "this.head=0;this.tail=0;this.length=0" in js
    assert "this.head=(this.head+1)&this.mask" in js
    assert "deadline=start+budget" in js
    assert "while(this.queue.length&&now()<deadline)" in js
    assert "setTimeout(()=>{this.scheduled=false;this.drainSlice();},0)" in js
    assert "queueMicrotask" not in js


def test_governor_is_display_aware_and_has_hysteresis():
    js = text("app/static/performance_core.js")
    assert "this.displayHz=60;this.targetFPS=60" in js
    assert "this.targetFPS=Math.min(this.maxTargetFPS,this.displayHz)" in js
    assert "this.pendingMode" in js
    assert "const dwell=moreSevere" in js
    assert ":3000" in js
    assert "Math.floor(dt/expected)-1" in js
    assert "longtask" in js


def test_existing_binary_transport_is_reused_and_feeds_display_bus():
    js = text("app/static/binary_transport.js")
    assert "/ws/nextgen/ticks-bin" in js
    assert "/ws/nextgen/surface-bin" in js
    assert "/ws/market" not in js
    assert "rt?.ingestTicks" in js
    assert "rt?.ingestSurface" in js
    assert "single_transport:true" in js
    assert "authority:'TRANSPORT_ONLY'" in js


def test_surface_minimal_copy_has_safe_ownership_rules():
    js = text("app/static/binary_transport.js")
    assert "TYPEDARRAY_VIEW" in js
    assert "LITTLE_ENDIAN" in js
    assert "WASM_COPY_SAFE" in js
    assert "new Float32Array(new Float32Array(state.wasmMemory.buffer,ptr,n))" in js


def test_trace_uses_independent_cached_layers_and_hover_only_dirties_interaction():
    js = text("app/static/nextgen_terminal.js")
    assert "this.layerOrder=['base','price','options','flow','interaction']" in js
    assert "ensureLayer(name)" in js
    assert "renderLayer(name,paint)" in js
    assert "composeLayers()" in js
    assert "this.renderLayer('base'" in js
    assert "this.renderLayer('price'" in js
    assert "this.renderLayer('options'" in js
    assert "this.renderLayer('flow'" in js
    assert "this.renderLayer('interaction'" in js
    hover_fragment = "this.markLayers('interaction');this.draw();this.showTip(x,y);"
    assert hover_fragment in js
    assert "rt.coalesce('nextgen-live-ticks',run,0)" in js


def test_adaptive_quality_only_changes_visual_css_and_render_resolution():
    css = text("app/static/app.css")
    js = text("app/static/performance_core.js")
    assert 'data-itm-perf-mode="PROTECT"' in css
    assert "backdrop-filter:none!important" in css
    assert "box-shadow:none!important" in css
    assert "surfaceResolution" in js
    assert "Scanner" in js and "PRESENTATION_ONLY" in js


def test_dashboard_loads_performance_core_before_binary_transport_and_has_diagnostics_toggle():
    html = text("app/templates/dashboard.html")
    assert "PERF · MAX" in html
    assert "/static/performance_core.js?v={{ app_version }}" in html
    assert html.index("performance_core.js") < html.index("binary_transport.js")
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))


def test_version_and_persistence_contract_are_preserved():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    service = text("app/service.py")
    assert_marker_version_at_least('1.26.2')
    assert "NO RESET LIVE/AUDITOR/CALIBRATION/REPLAY" in service
    doc = text("docs/PERFORMANCE_INTERACTION_CORE.md")
    assert "Scanner remains the only directional authority" in doc
    assert "Only redundant browser-side display work can be deferred or coalesced" in doc
