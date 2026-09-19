from pathlib import Path


def test_trace_timeframe_selector_is_visible_and_separate_from_window():
    html=Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'id="traceCandleWrap"' in html
    assert 'VELAS</span>' in html and 'Timeframe</label>' in html
    block=html[html.index('id="traceCandleWrap"'):html.index('id="traceTimeWindowWrap"')]
    for value in ('1m','3m','5m','15m'):
        assert f'value="{value}"' in block
    assert 'value="1m" selected' in block
    assert 'id="traceTimeWindow"' in html
    assert '15m' in html and '30m' in html and '1H' in html and '2H' in html


def test_api_accepts_only_standard_trace_timeframes_and_falls_back_to_1m():
    src=Path("app/main.py").read_text(encoding="utf-8")
    core=Path("app/core/nextgen_terminal.py").read_text(encoding="utf-8")
    assert 'if timeframe not in {"1m", "3m", "5m", "15m"}' in src
    assert 'timeframe = "1m"' in src
    assert 'if tf not in {"1m", "3m", "5m", "15m"}' in core


def test_normal_and_fusion_support_3m_and_15m_causal_ohlc():
    """Legacy Plotly fusion is gone; the single NextGen candle engine owns causal TFs."""
    src=Path("app/core/nextgen_terminal.py").read_text(encoding="utf-8")
    assert 'return {"1m": 1, "3m": 3, "5m": 5, "15m": 15}' in src
    assert 'same origin/closure so a 3m/5m/15m selection cannot visually drift' in src
    assert '"candle_integrity"' in src and '"synthetic_fill": False' in src
    assert 'def candles_from_ticks' in src


def test_timeframe_is_sent_end_to_end_and_live_tick_bucketing_uses_selection():
    js=Path("app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    main=Path("app/main.py").read_text(encoding="utf-8")
    assert "const tf=$('traceCandle')?.value||'1m'" in js
    assert '/api/nextgen/trace?timeframe=${encodeURIComponent(tf)}&tail_minutes=${encodeURIComponent(tail)}&window=${encodeURIComponent(win)}' in js
    assert "const tf=(this.payload?.timeframe||'1m')" in js
    assert 'd.setMinutes(Math.floor(d.getMinutes()/mins)*mins)' in js
    assert 'STATE.nextgen_trace(timeframe=timeframe,tail_minutes=tail_minutes,visual_window=window)' in main


def test_timeframe_does_not_replace_tick_tape_or_visible_window():
    html=Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    app=Path("app/static/app.js").read_text(encoding="utf-8")
    ng=Path("app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    assert 'Tape sigue tick-by-tick' in html
    assert "document.getElementById('traceTimeWindow')" in ng
    assert "$('traceCandle')" in ng
    assert 'window.ITMQNextGen?.ingestTicks?.(ticks)' in app
    assert 'followCount()' in ng
