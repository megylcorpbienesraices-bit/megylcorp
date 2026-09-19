from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]

def text(rel): return (ROOT/rel).read_text(encoding='utf-8')

def test_v1394_lightweight_market_terminals_are_loaded_and_routed_before_generic_canvas():
    html=text('app/templates/dashboard.html')
    app=text('app/static/app.js')
    js=text('app/static/market_line_terminal.js')
    assert 'market_line_terminal.js' in html
    assert html.index('market_line_terminal.js') < html.index('app.js')
    for cid in ('flowProChart','netDriftChart','printsChart'):
        assert cid in js
    assert app.index('ITMQMarketLineTerminals?.render') < app.index('ITMQUltraCharts?.render')
    assert 'PRECIO · TRACE STYLE' in js
    assert 'NET DRIFT · ESTRUCTURA' in js
    assert 'Q-FLOW · ACUMULADO' in js


def test_v1394_generic_canvas_no_longer_owns_financial_line_terminals_or_matrices():
    js=text('app/static/ultra_charts.js')
    native_line=next(line for line in js.splitlines() if line.startswith('const NATIVE=new Set'))
    for retired in ('flowProChart','netDriftChart','printsChart','chainChart','gexMatrixChart'):
        assert retired not in native_line


def test_v1394_chart_state_mismatch_only_blocks_surface():
    app=text('app/static/app.js')
    assert "activeView==='surface'&&bs.surface_metric&&backendKey!==requestedKey" in app
    assert "surface backend/control mismatch" in app


def test_v1394_plotly_hover_formats_do_not_use_unsupported_explicit_plus_flag():
    for rel in ('app/core/institutional_modules.py','app/core/advanced_visuals.py','app/service.py'):
        src=text(rel)
        assert ':+.2f' not in src
        assert ':+,.0f' not in src
        assert ':+,.2f' not in src


def test_v1394_large_prints_expose_renderer_roles_and_no_text_clutter():
    src=text('app/core/large_prints.py')
    assert '"role":"UNDERLYING_PRICE"' in src
    assert '"role":"PRINT_EVENT"' in src
    assert '"role":"PRINT_NOTIONAL"' in src
    assert 'mode="markers+text"' not in src


def test_v1394_chain_and_gex_matrix_publish_every_strike_level():
    service=text('app/service.py')
    inst=text('app/core/institutional_modules.py')
    assert 'tickmode="array",tickvals=list(piv.index),ticktext=strike_labels' in service
    assert 'tickmode="array", tickvals=row_labels, ticktext=row_labels' in inst
    assert 'tickvals=row_labels, ticktext=row_labels' in inst
    assert 'fig.update_xaxes(type="category"' in inst


def test_v1394_structure_exposes_surface_3d_clearly():
    html=text('app/templates/dashboard.html')
    assert '>Surface</span><em class="workspace-3d-badge">3D</em>' in html
    for metric in ('Gamma','GEX','DEX','Charm'):
        assert f'data-quant3d="{metric}"' in html
    # Renderer capability is a structural/runtime contract, not analyst-facing copy.
    # Analyst View intentionally hides engineering implementation details.
    assert '/static/webgpu_surface.js?v={{ app_version }}' in html
    runtime=text('app/static/nextgen_terminal.js')
    assert 'window.ITMQWebGPU?.supported?.()' in runtime
    assert 'activateWebGLFallback' in runtime


def test_v1394_xr_runtime_probe_is_lazy():
    js=text('app/static/institutional_terminal.js')
    assert "state.textContent='BROWSER CHECK'" in js
    assert "b.addEventListener('click',async()=>" in js
    # Browser runtime probing remains available but is not performed before user action.
    click=js.index("b.addEventListener('click',async()=>")
    probe=js.index("navigator.xr.isSessionSupported('immersive-vr')")
    assert probe > click
