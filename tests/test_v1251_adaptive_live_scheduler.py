from pathlib import Path
import ast
from conftest import assert_version_at_least, assert_marker_version_at_least, assert_dashboard_uses_runtime_version

ROOT=Path(__file__).resolve().parents[1]


def test_backend_adaptive_scheduler_transitions_and_intervals():
    from app.core.live_scheduler import AdaptiveLiveScheduler, SchedulerCadence
    s=AdaptiveLiveScheduler(SchedulerCadence(structural_active=5,structural_normal=10,structural_quiet=20,flow_active=10,flow_normal=20,flow_quiet=45,wake=1))
    t=100.0
    # first regular observation with no events is quiet
    assert s.observe({'last_seq':0},{'last_seq':0},market_mode='LIVE',market_state='REGULAR',now=t)=='QUIET'
    assert s.structural_interval()==20
    # burst of SIP activity promotes ACTIVE and is held
    assert s.observe({'last_seq':20,'last_tick':{'price':100.0}},{'last_seq':0},market_mode='LIVE',market_state='REGULAR',now=t+1)=='ACTIVE'
    assert s.structural_interval()==5 and s.flow_interval()==10
    # no immediate flip back to quiet one second later
    assert s.observe({'last_seq':20,'last_tick':{'price':100.0}},{'last_seq':0},market_mode='LIVE',market_state='REGULAR',now=t+2)=='ACTIVE'
    # premarket is explicitly slower
    assert s.observe({'last_seq':20},{'last_seq':0},market_mode='LIVE',market_state='PREMARKET',now=t+3)=='PREMARKET'
    assert s.structural_interval()>=45


def test_backend_scheduler_is_scheduling_only_and_wakes_fast():
    py=(ROOT/'app/core/live_scheduler.py').read_text(encoding='utf-8')
    main=(ROOT/'app/main.py').read_text(encoding='utf-8')
    cfg=(ROOT/'app/config.py').read_text(encoding='utf-8')
    assert 'EVENT_DRIVEN_MULTI_PROVIDER + LONDON_SESSION_AWARE_ADAPTIVE_REFRESH' in py and 'SCHEDULING_ONLY' in py
    assert '"authority": "SCHEDULING_ONLY"' in py
    assert 'PRICE_TICK_FABRIC.health(STATE.symbol)' in main and 'OPTION_FLOW_FABRIC.health(STATE.symbol)' in main
    assert 'LIVE_REFRESH_SCHEDULER.due' in main
    assert '/api/nextgen/live-scheduler' in main
    assert 'ITM_STRUCT_ACTIVE_SECONDS' in cfg and 'ITM_FLOW_ACTIVE_SECONDS' in cfg
    assert 'ADAPTIVE_WAKE_SECONDS' in cfg


def test_client_scheduler_loaded_before_renderers_and_has_adaptive_cadence():
    html=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')
    js=(ROOT/'app/static/live_scheduler.js').read_text(encoding='utf-8')
    assert html.index('/static/live_scheduler.js') < html.index('/static/binary_transport.js')
    for token in ('ACTIVE','NORMAL','QUIET','HIDDEN','noteTicks','noteFlow','cadence','loop','frame'):
        assert token in js
    assert "pulse:{ACTIVE:650,NORMAL:1000,QUIET:1800,HIDDEN:5000}" in js
    assert "trace:{ACTIVE:1200,NORMAL:2200,QUIET:4500,HIDDEN:8000}" in js


def test_tick_hot_path_is_frame_coalesced_and_not_double_ingested():
    ng=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    app=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
    assert 'function queueLiveTicks' in ng
    assert 'NQ.tickQueue.push(...filtered)' in ng
    assert "NQ.tickFrame=requestAnimationFrame" in ng
    assert 'requestHotDraw()' in ng
    assert 'this.hotDrawFrame=requestAnimationFrame' in ng
    # app shell receives the binary event but no longer injects the same ticks into NextGen again
    binary_listener=app.split("window.addEventListener('itmq:binary-ticks'",1)[1].split('});',1)[0]
    assert 'ITMQNextGen?.ingestTicks' not in binary_listener
    assert 'NextGen owns tick ingestion' in binary_listener


def test_trace_and_pulse_use_adaptive_loops_not_old_fixed_hot_intervals():
    ng=(ROOT/'app/static/nextgen_terminal.js').read_text(encoding='utf-8')
    app=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
    assert "ITMQLiveScheduler.loop('nextgen-trace'" in ng
    assert "ITMQLiveScheduler.loop('nextgen-surface'" in ng
    assert "ITMQLiveScheduler.loop('trace-pulse'" in app
    assert 'setInterval(pollTracePulse,900)' not in app
    assert "},1200);" not in ng
    assert 'NQ.miniPending.set(id,spec)' in ng


def test_v1251_version_and_persistence_compatibility():
    assert_version_at_least('1.26.2')
    assert_marker_version_at_least('1.26.2')
    assert_dashboard_uses_runtime_version((ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8"))
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in (ROOT/'app/config.py').read_text(encoding='utf-8')


def test_python_syntax_v1251():
    for rel in ('app/core/live_scheduler.py','app/config.py','app/main.py','app/core/nextgen_terminal.py'):
        ast.parse((ROOT/rel).read_text(encoding='utf-8'))
