import math

from app.service import PlatformState


def _report():
    s=PlatformState(); s.mode='DEMO'; s.refresh(True)
    return s, s.analyze_premarket(False)


def test_dia_premarket_is_native_and_compact_near_spot():
    _, r=_report()
    assert r['ready'] and r['symbol']=='DIA' and r['native_price_only'] is True
    rows=r['key_strikes']
    assert 6 <= len(rows) <= 8
    spot=float(r['spot'])
    strikes=sorted(float(x['strike']) for x in rows)
    step=min(abs(b-a) for a,b in zip(strikes,strikes[1:]) if abs(b-a)>1e-9)
    assert max(abs(float(x['strike'])-spot) for x in rows) <= 7.1*step


def test_strike_rows_have_numeric_oi_volume_gamma_delta_totals():
    _, r=_report()
    for x in r['key_strikes']:
        for key in ['call_oi','put_oi','total_oi','call_volume','put_volume','total_volume','call_gex','put_gex','net_gex','call_delta','put_delta','net_delta']:
            assert math.isfinite(float(x[key]))
        assert math.isclose(x['call_oi']+x['put_oi'],x['total_oi'],rel_tol=1e-9,abs_tol=1e-6)
        assert math.isclose(x['call_volume']+x['put_volume'],x['total_volume'],rel_tol=1e-9,abs_tol=1e-6)
        assert x['joint_reading']


def test_chain_totals_are_explicit_and_reconcile():
    _, r=_report(); t=r['chain_totals']
    assert t['total_oi'] > 0 and t['total_volume'] > 0
    assert math.isclose(t['call_oi']+t['put_oi'],t['total_oi'],rel_tol=1e-9,abs_tol=1e-6)
    assert math.isclose(t['call_volume']+t['put_volume'],t['total_volume'],rel_tol=1e-9,abs_tol=1e-6)
    assert math.isfinite(float(t['net_gex'])) and math.isfinite(float(t['net_delta']))


def test_pending_related_sources_do_not_fake_confirmation():
    _, r=_report(); c=r['confirmation_summary']
    assert c['usable'] == 0
    assert c['confirmed'] == 0 and c['contradicted'] == 0
    assert c['score'] is None
    assert all(x['status'] in {'SIN DATO','DATO PARCIAL','ESPERANDO'} for x in r['confirmations'])


def test_report_has_one_main_and_at_most_one_alternative_scenario():
    _, r=_report()
    m=r['main_scenario']
    assert m['direction'] in {'COMPRA','VENTA','MIXTO'}
    assert m['zone']['center'] is not None
    assert m['target1'] is not None and m['invalidation'] is not None
    a=r['alternative_scenario']
    assert a is None or isinstance(a,dict)
    assert 'conclusion' in r and r['conclusion']['reading']
