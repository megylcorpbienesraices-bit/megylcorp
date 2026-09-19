from pathlib import Path
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version

ROOT = Path(__file__).resolve().parents[1]


def _webgpu():
    return (ROOT / 'app/static/webgpu_surface.js').read_text(encoding='utf-8')


def _nextgen():
    return (ROOT / 'app/static/nextgen_terminal.js').read_text(encoding='utf-8')


def test_webgpu_uniform_layout_matches_wgsl_96_bytes():
    js = _webgpu()
    assert 'struct U { mvp: mat4x4<f32>, time: f32, pad: vec3<f32> };' in js
    assert 'const UNIFORM_FLOATS=24;' in js
    assert 'const UNIFORM_BYTES=UNIFORM_FLOATS*4' in js
    assert 'createBuffer({size:UNIFORM_BYTES' in js
    assert 'new Float32Array(UNIFORM_FLOATS)' in js
    assert 'size:80' not in js


def test_webgpu_first_frame_is_validated_before_active_status():
    js = _webgpu()
    for token in (
        "pushErrorScope('validation')",
        'popErrorScope()',
        'onSubmittedWorkDone()',
        'markFrameOk()',
        'GPU · WEBGPU READY',
        'GPU · WEBGPU ACTIVE · FRAME OK',
        'FIRST_FRAME_VALIDATION',
    ):
        assert token in js


def test_webgpu_device_loss_and_uncaptured_error_are_guarded():
    js = _webgpu()
    assert 'device.lost' in js or 'd?.lost' in js
    assert "addEventListener?.('uncapturederror'" in js
    assert "itmq:webgpu-failed" in js
    assert 'DEVICE_LOST' in js
    assert 'UNCAUGHT_GPU_ERROR' in js


def test_webgpu_triangle_winding_is_visible_with_backface_culling():
    js = _webgpu()
    assert 'verts.push(...a0,...a2,...a1,...a1,...a2,...a3)' in js
    assert "cullMode:'back'" in js


def test_runtime_failure_replaces_canvas_before_webgl_fallback():
    js = _nextgen()
    assert 'function replaceSurfaceCanvas()' in js
    assert 'old.cloneNode(false)' in js
    assert 'old.replaceWith(fresh)' in js
    assert 'function activateWebGLFallback' in js
    assert "window.addEventListener('itmq:webgpu-failed'" in js
    assert "WEBGL · GLSL SHADER · FALLBACK ACTIVO" in js
    assert "activateWebGLFallback('WEBGPU_INIT_UNAVAILABLE')" in js


def test_product_version_v1249_without_persistence_reset():
    assert_version_at_least('1.26.2')
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in (ROOT / 'app/config.py').read_text(encoding='utf-8')
