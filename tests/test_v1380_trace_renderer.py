from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

def text(rel):
    return (ROOT / rel).read_text(encoding='utf-8')

def test_lightweight_price_bridge_is_optional_and_loaded_before_nextgen():
    html = text('app/templates/dashboard.html')
    remote = 'https://unpkg.com/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js'
    local = '/static/vendor/lightweight-charts.standalone.production.js'
    vendor_files = [
        ROOT / 'app/static/vendor/lightweight-charts.standalone.production.js',
        ROOT / 'app/static/vendor/LIGHTWEIGHT_CHARTS_LICENSE',
        ROOT / 'app/static/vendor/lightweight-charts.lock.json',
    ]
    present = [p.is_file() for p in vendor_files]
    assert all(present) or not any(present), 'Lightweight Charts vendor state must be atomic'
    if all(present):
        assert local in html
        assert remote not in html
        renderer_marker = local
    else:
        assert remote in html
        renderer_marker = remote
    assert html.index(renderer_marker) < html.index('/static/lightweight_trace.js') < html.index('/static/nextgen_terminal.js')
    bridge = text('app/static/lightweight_trace.js')
    assert 'native fallback active' in bridge
    assert 'window.ITMQLightweightTrace' in bridge

def test_trace_defaults_are_dex_left_gex_right_and_charm_is_selectable():
    html = text('app/templates/dashboard.html')
    left = html.split('id="traceLeftProfile"',1)[1].split('</select>',1)[0]
    right = html.split('id="traceRightProfile"',1)[1].split('</select>',1)[0]
    heat = html.split('id="traceTemporalHeatmap"',1)[1].split('</select>',1)[0]
    assert '<option value="DEX" selected>' in left
    assert '<option value="GEX" selected>' in right
    assert '<option value="charm">CHARM</option>' in heat

def test_temporal_heatmap_exports_independent_charm_field():
    from app.core.nextgen_terminal import temporal_heatmap_history
    ts = pd.Timestamp('2026-09-14T14:30:00Z')
    rows=[]
    for j in range(12):
        t=ts+pd.Timedelta(minutes=j)
        for strike,typ,ch in [(530.0,'call',0.03),(531.0,'put',-0.02)]:
            rows.append({
                'timestamp':t,'strike':strike,'option_type':typ,
                'signed_gex_proxy':1_000_000 if typ=='call' else -800_000,
                'option_delta_exposure_info':500_000 if typ=='call' else -450_000,
                'open_interest':1000,'option_volume':100,'calc_charm':ch,
                'underlying_price':530.5,
            })
    out=temporal_heatmap_history({'enriched':pd.DataFrame(rows),'spot':530.5},530.5,visual_window=5)
    assert out['ready'] is True
    assert 'charm_intensity' in out and 'charm_m' in out
    assert len(out['charm_intensity']) == len(out['strikes'])
    assert any(abs(v)>0 for row in out['charm_intensity'] for v in row)

def test_obsolete_trace_docs_are_removed_and_current_doc_exists():
    assert (ROOT/'docs/TRACE_RENDERER.md').exists()
    for rel in [
        'docs/CHART_TW_V2_v1.25.20.md',
        'docs/NEXTGEN_CORE_TERMINAL_v1.24.md',
        'docs/LEAN_INTELLIGENT_TERMINAL_v1.26.0.md',
        'docs/SPOTGAMMA_PARITY_ROADMAP.md',
    ]:
        assert not (ROOT/rel).exists()
