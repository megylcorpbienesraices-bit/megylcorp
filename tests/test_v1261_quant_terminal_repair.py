from pathlib import Path
import json
from conftest import assert_version_at_least, assert_marker_version_at_least


ROOT=Path(__file__).resolve().parents[1]
def text(rel):
    return (ROOT/rel).read_text(encoding='utf-8')


def test_v1261_release_markers():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')


def test_dow_specialized_navigation_is_visible():
    html=text('app/templates/dashboard.html')
    for label in ['OPERATIVA','EQUITY HUB','PREMARKET','TRACE','SCANNER','FLUJO','DEALER FLOW','ESTRUCTURA','VOLATILIDAD','MACRO','AUDITOR']:
        assert f'>{label}<' in html
    for child in ['Unusual Flow','Net Drift','Large Prints','Cadena','Exposure','GEX Matrix','Positioning','Surface']:
        assert f'>{child}<' in html
    assert html.count('class="nav-btn')==11


def test_tradingview_style_symbol_search_replaces_visible_ecosystem_picker():
    html=text('app/templates/dashboard.html')
    assert 'id="symbolSearchModal"' in html
    assert 'Instrumentos · ETF · futuros · índices' in html
    assert 'Buscar SPY, QQQ, IWM, DIA, YM, DJX…' in html
    assert 'data-symbol-category="Futuros"' in html
    assert 'data-symbol-category="Índices"' in html
    assert 'data-symbol-category="ETFs"' in html
    assert '<select id="quickAssetSelect" class="lean-hidden"' in html
    assert 'for="quickAssetSelect">ECOSISTEMA' not in html


def test_surface_restores_gex_dex_oi_volume_fields():
    html=text('app/templates/dashboard.html')
    core=text('app/core/nextgen_terminal.py')
    service=text('app/service.py')
    for field in ['GEX','DEX','Open Interest','Net OI','Volumen','Volumen Neto','Actividad inusual']:
        assert field in core
    assert 'data-field="GEX"' in html and 'data-field="DEX"' in html
    assert 'm in {"Gamma","GEX"}' in service
    assert 'm in {"Delta","DEX"}' in service


def test_backend_chart_calculation_is_view_gated():
    service=text('app/service.py')
    assert 'view: str = "all"' in service
    for token in ['need_trace','need_flow','need_netdrift','need_exposure','need_gexmatrix','need_positioning','need_volatility','need_surface']:
        assert token in service


def test_trace_has_single_active_render_target_and_polling_is_gated():
    app=text('app/static/app.js')
    ng=text('app/static/nextgen_terminal.js')
    assert 'if(c.trace)' not in app
    assert "const sec=activeSectionId();if(!['section-trace','section-command'].includes(sec))return;" in app
    assert 'window.ITMQNextGen?.ingestTicks?.(ticks)' in app
    assert 'function activeTraceRenderer()' in ng
    assert 'withActiveTrace(r=>r.ingestTicks(batch))' in ng
    assert 'NQ.trace?.ingestTicks(batch);NQ.opTrace?.ingestTicks(batch);' not in ng


def test_surface_gpu_is_lazy_loaded():
    ng=text('app/static/nextgen_terminal.js')
    assert 'Surface GPU is lazy' in ng
    assert "if(activeSection()==='section-surface')Promise.resolve().then(initSurface)" in ng
    assert "else if(sec==='section-surface'){if(!NQ.surface)" in ng


def test_chart_views_do_not_default_to_command_for_non_chart_screens():
    app=text('app/static/app.js')
    assert "return m[sec]||null;" in app
    assert "const activeView=chartViewForSection();if(!activeView)return;" in app
