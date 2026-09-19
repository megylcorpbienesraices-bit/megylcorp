from pathlib import Path
import ast
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def test_v1246_true_x_scale_contract_is_present():
    js = (ROOT / 'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    html = (ROOT / 'app/templates/dashboard.html').read_text(encoding='utf-8')
    assert 'TRACE TRUE CHART SCALE' in js
    assert 'barSpacingPx' in js and 'rightOffsetBars' in js and 'xShiftPx' in js
    assert 'viewportTimeRange' in js and 'timeAtX' in js and 'anchorX' in js
    py = (ROOT / 'app/core/nextgen_terminal.py').read_text(encoding='utf-8')
    assert 'TRACE_TRUE_CHART_SCALE_92' in py and 'DATA_HISTORY_NOT_STRETCH_TO_FIT' in py
    assert 'Rueda = zoom X' in html
    assert 'TRUE X SCALE' in html


def test_v1246_center_resets_temporal_viewport():
    js = (ROOT / 'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    app = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
    assert 'resetTrace:(follow=true)' in js
    assert 'this.barSpacingPx=this.defaultBarSpacingPx' in js
    assert 'this.xShiftPx=0' in js
    assert 'ITMQNextGen?.resetTrace' in app


def test_v1246_auto_price_focus_does_not_force_all_far_levels_into_y_range():
    js = (ROOT / 'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    assert "if(frame==='structure'||frame==='strikes')" in js
    assert 'baseLo-focusPad' in js and 'baseHi+focusPad' in js
    assert "above.push({lv,p})" in js and "below.push({lv,p})" in js
    assert "top?'↑':'↓'" in js


def test_v1246_backend_timeframe_ohlc_remains_event_time_aligned():
    from app.core.nextgen_terminal import candles_from_ticks
    ts = pd.to_datetime([
        '2026-09-09 09:30:05', '2026-09-09 09:31:20', '2026-09-09 09:32:58',
        '2026-09-09 09:33:02', '2026-09-09 09:35:01'
    ])
    df = pd.DataFrame({
        'timestamp': ts,
        'price': [100, 101, 99, 102, 103],
        'size': [1, 2, 3, 4, 5],
        'signed_volume': [1, -2, 3, 4, 5],
        'seq': range(5),
    })
    bars = candles_from_ticks(df, '3m', 60)
    assert [pd.Timestamp(x['t']).minute for x in bars] == [30, 33]
    assert (bars[0]['o'], bars[0]['h'], bars[0]['l'], bars[0]['c']) == (100, 101, 99, 99)
    assert bars[0]['v'] == 6
    assert (bars[1]['o'], bars[1]['h'], bars[1]['l'], bars[1]['c']) == (102, 103, 102, 103)


def test_v1246_python_syntax():
    for rel in ['app/core/nextgen_terminal.py', 'app/core/trace_live.py', 'app/main.py']:
        ast.parse((ROOT / rel).read_text(encoding='utf-8'))
