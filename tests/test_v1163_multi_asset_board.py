from datetime import datetime, timezone, timedelta
from pathlib import Path
import re

from app.core.multi_asset_board import board_row, build_board, ALLOWED_FIELDS
from app.service import PlatformState


def scanner(symbol='SPY', ev=0.25, active=False, calibrated=True, evidence=70, edge='ACTIONABLE'):
    return {
        'ready':True,'symbol':symbol,'spot':500,'direction':'BUY','scenario_type':'REBOTE',
        'zone':{'low':499,'center':500,'high':501},'target1':503,'target2':506,'invalidation':497,
        'edge_state':edge,'evidence_score':evidence,'contender_gap':7.5,'rr_t1':1.5,
        'edge_gate':{'expected_value_r':ev,'probability_t1_first':0.62,'active':active,
                     'calibration_ready':calibrated,'stage':'ACTIVE' if active else 'CALIBRATED','mode':'EV'},
    }


def test_ev_shadow_is_not_treated_as_active_gate():
    r=board_row('SPY',scanner(active=False,calibrated=True,ev=.42),as_of=datetime.now(timezone.utc),refresh_cadence_seconds=60)
    assert r['model_calibrated'] is True
    assert r['ev_gate_active'] is False
    assert r['ev_shadow_available'] is True
    assert r['gate_bucket']=='CALIBRATED · GATE OFF'


def test_zone_reads_real_scanner_contract():
    r=board_row('SPY',scanner(),as_of=datetime.now(timezone.utc),refresh_cadence_seconds=60)
    assert r['zone_low']==499 and r['zone_high']==501
    assert r['scenario_type']=='REBOTE'


def test_stale_rows_are_outside_operational_counts_and_sort():
    now=datetime.now(timezone.utc)
    fresh=board_row('SPY',scanner('SPY',edge='CAUTION'),as_of=now,now=now,refresh_cadence_seconds=30)
    stale=board_row('TSLA',scanner('TSLA',edge='ACTIONABLE',active=True),as_of=now-timedelta(minutes=10),now=now,refresh_cadence_seconds=30)
    b=build_board([stale,fresh])
    assert b['rows'][0]['symbol']=='SPY'
    assert b['stale_rows']==1
    assert b['counts']['ACTIONABLE']==0
    assert b['counts']['CAUTION']==1


def test_ev_and_evidence_never_share_sort_slot():
    now=datetime.now(timezone.utc)
    ev=board_row('SPY',scanner('SPY',ev=.10,active=True,evidence=50),as_of=now,now=now,refresh_cadence_seconds=60)
    legacy=board_row('DIA',scanner('DIA',ev=.90,active=False,calibrated=True,evidence=99),as_of=now,now=now,refresh_cadence_seconds=60)
    b=build_board([legacy,ev])
    assert b['rows'][0]['symbol']=='SPY'
    assert b['rows'][1]['symbol']=='DIA'


def test_board_whitelist_blocks_invented_strength():
    r=board_row('SPY',scanner(),as_of=datetime.now(timezone.utc))
    r['trend_score']=99
    try:
        build_board([r])
    except ValueError as e:
        assert 'trend_score' in str(e)
    else:
        raise AssertionError('Board accepted an invented field')


def test_ui_keeps_18_navigation_tabs_and_board_inside_operativa():
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    assert len(re.findall(r'data-section="[^"]+"',html))==11  # v1.37: grouped top navigation; child workspaces remain accessible through subnavigation
    assert 'data-section="board"' not in html
    assert 'id="opBoardView"' in html and 'id="opAssetView"' in html


def test_board_refresh_is_staggered_and_dynamic_universe():
    src=Path('app/service.py').read_text(encoding='utf-8')
    assert 'if cfg.get("full",False)' in src
    assert 'refresh_board_next' in src
    assert '_board_scan_symbol' in src
    assert 'OPTION_STREAM.set_universe' not in src[src.index('def _board_scan_symbol'):src.index('@dataclass',src.index('def _board_scan_symbol'))]


def test_public_state_exposes_board():
    src=Path('app/service.py').read_text(encoding='utf-8')
    assert '"board": self.board_state()' in src


def test_no_old_multi_expiry_copy_survives():
    src=Path('app/service.py').read_text(encoding='utf-8')
    assert 'relevante en 0DTE + semana + 2 semanas' not in src
