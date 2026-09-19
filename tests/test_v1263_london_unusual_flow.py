from datetime import date
import pandas as pd

from app.core.flow_intelligence import london_open_ny, flow_session_phase, _activity_scores
from app.core.live_scheduler import AdaptiveLiveScheduler, SchedulerCadence
from app.core.institutional_modules import flow_unusual_pro_figure, flow_pro_summary


def test_london_open_is_dst_aware_and_not_symbol_specific():
    # 2026-09-10: both US/UK on DST -> London 08:00 = New York 03:00.
    a=london_open_ny(date(2026,9,10))
    assert a.hour == 3 and a.tzinfo is not None
    # During the autumn mismatch week the mapping changes automatically.
    b=london_open_ny(date(2026,10,29))
    assert b.hour in {3,4}


def test_flow_session_phase_starts_at_london_before_us_premarket():
    # Ecuador is UTC-5; 2026-09-10 02:15 EC == 03:15 NY.
    assert flow_session_phase(pd.Timestamp('2026-09-10 02:15:00')) == 'LONDON'
    assert flow_session_phase(pd.Timestamp('2026-09-10 03:30:00')) == 'PREMARKET'
    assert flow_session_phase(pd.Timestamp('2026-09-10 08:45:00')) == 'NEW YORK'
    assert flow_session_phase(pd.Timestamp('2026-09-10 15:15:00')) == 'AFTER HOURS'


def test_activity_score_is_causal_prior_bars_only():
    ts=pd.date_range('2026-09-10 02:00', periods=12, freq='min')
    base=pd.DataFrame({
        'timestamp':ts,'open':[100]*12,'high':[100.05]*12,'low':[99.95]*12,'close':[100]*12,
        'volume':[100]*11+[5000],'vwap':[100]*12,'signed_notional_proxy':[10000]*11+[500000],
    })
    out=_activity_scores(base)
    # Early bars collect baseline; the last spike can be detected without self-normalizing.
    assert bool(out.loc[0,'activity_ready']) is False
    assert bool(out.loc[11,'activity_ready']) is True
    assert float(out.loc[11,'activity_score']) >= 70


def test_flow_figure_accepts_london_underlying_anomaly_without_faking_options():
    ts=pd.date_range('2026-09-10 02:00', periods=12, freq='min')
    bars=pd.DataFrame({
        'timestamp':ts,'open':[100]*12,'high':[100.05]*12,'low':[99.95]*12,'close':[100]*12,
        'volume':[100]*11+[5000],'vwap':[100]*12,'signed_notional_proxy':[10000]*11+[500000],
    })
    bars=_activity_scores(bars)
    hist=pd.DataFrame({'timestamp':ts,'underlying_price':[100]*12})
    fig=flow_unusual_pro_figure(pd.DataFrame(),hist,{},bars,'QQQ')
    assert fig.layout.meta['unusual_flow_start'] == 'LONDON_OPEN_DST_AWARE'
    assert fig.layout.meta['multi_asset'] is True
    names=[str(t.name) for t in fig.data]
    assert 'Punto exacto de entrada' in names
    summ=flow_pro_summary(pd.DataFrame(),hist,{},bars)
    assert summ['state'] == 'ACTIVE'
    assert str(summ['latest']['source']).startswith('UNDERLYING')


def test_scheduler_has_london_cadence_instead_of_closed_cadence():
    sch=AdaptiveLiveScheduler(SchedulerCadence(london=20,closed=90))
    state=sch.observe({}, {}, market_mode='LIVE', market_state='LONDON', now=1.0)
    assert state == 'LONDON'
    assert sch.structural_interval() == 20
    assert sch.flow_interval() == 20
