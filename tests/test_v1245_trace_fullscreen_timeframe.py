from pathlib import Path
import ast
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]

def test_v1245_layout_and_structured_lanes_contract():
    js=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    css=(ROOT/'app/static/app.css').read_text(encoding='utf-8')
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    py=(ROOT/'app/core/nextgen_terminal.py').read_text(encoding='utf-8')
    assert 'TRACE_FULL_VIEWPORT_STRUCTURED_LANES_92' in py
    assert 'structured_lanes' in py and 'smart_tick_skipping' in py and 'timeframe_integrity' in py
    assert 'gammaLeft' in js and 'deltaLeft' in js and 'strikeLeft' in js
    assert 'labelEvery' in js and 'pxPerStep' in js
    assert 'xMapTime' in js and 'candleStepPx' in js and 'normalizeTimeframeCandles' in js
    assert 'SIN OHLC OBSERVADO PARA' in js
    assert 'min-height:92vh' in css
    assert 'traceTfIntegrity' in html

def test_v1245_python_candles_are_aligned_to_selected_timeframe():
    from app.core.nextgen_terminal import candles_from_ticks
    ts=pd.to_datetime(['2026-09-09 09:30:05','2026-09-09 09:31:20','2026-09-09 09:32:58','2026-09-09 09:33:02','2026-09-09 09:35:01'])
    df=pd.DataFrame({'timestamp':ts,'price':[100,101,99,102,103],'size':[1]*5,'signed_volume':[1,-1,1,1,1],'seq':range(5)})
    b=candles_from_ticks(df,'3m',60)
    assert [pd.Timestamp(x['t']).minute for x in b]==[30,33]
    assert b[0]['o']==100 and b[0]['h']==101 and b[0]['l']==99 and b[0]['c']==99
    assert b[1]['o']==102 and b[1]['c']==103
    assert all('complete' in x for x in b)

def test_v1245_python_syntax():
    for rel in ['app/core/nextgen_terminal.py','app/core/trace_live.py']:
        ast.parse((ROOT/rel).read_text(encoding='utf-8'))
