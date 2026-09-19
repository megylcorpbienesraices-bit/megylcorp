import math
import numpy as np
import pandas as pd

from app.core.engine import analyze_gamma_delta, engine_config_for_asset
from app.core.precision_engine import black_scholes_greeks_full, black_scholes_greeks_vector, market_inputs
from app.core.regime_engine import classify_regime
from app.service import _demo_history
from app.core.expiry_window import apply_expiry_window


def test_vectorized_greeks_match_scalar_path():
    S=np.array([100.0,101.5,99.2,110.0]);K=np.array([100.0,100.0,102.0,105.0]);T=np.array([1,3,7,14],dtype=float)/365.0
    iv=np.array([.18,.22,.35,.28]);calls=np.array([True,False,True,False]);r=np.array([.045]*4);q=np.array([.012]*4)
    vg=black_scholes_greeks_vector(S,K,T,iv,calls,r,q)
    for i in range(4):
        sg=black_scholes_greeks_full(S[i],K[i],T[i],iv[i],'call' if calls[i] else 'put',r[i],q[i])
        for key in ('delta','gamma','vanna','charm','speed'):
            assert math.isclose(float(vg[key][i]),float(sg[key]),rel_tol=1e-11,abs_tol=1e-12)


def test_market_inputs_cache_keeps_phase2_asset_carry():
    for sym in ('NVDA','MSFT','META','AMZN','TSLA'):
        mi=market_inputs(sym,7)
        assert math.isfinite(mi['risk_free_rate'])
        assert math.isfinite(mi['dividend_yield'])


def test_decorrelated_basis_is_exposed_and_bounded():
    h=_demo_history('DIA')
    w,_=apply_expiry_window(h,'WEEK')
    out=analyze_gamma_delta(w,engine_config_for_asset('DIA'))
    cur=out['current_delta']
    for c in ('mass_pct','intensity_pct','turnover_pct','net_tilt_pct','containment_core','break_core'):
        assert c in cur.columns
        vals=pd.to_numeric(cur[c],errors='coerce').dropna()
        assert not vals.empty
        if c.endswith('_pct'):
            assert vals.between(0,1).all()
        else:
            assert vals.between(0,100).all()


def test_turnover_stabilizer_reduces_tiny_oi_explosion():
    now=pd.Timestamp('2026-09-04 10:00:00')
    rows=[]
    # same volume but OI differs drastically; both call+put so every strike aggregates cleanly
    for strike,oi,vol in [(100.0,2,100),(101.0,100,100),(102.0,1000,100)]:
        for typ in ('call','put'):
            rows.append({'timestamp':now,'underlying_price':101.0,'strike':strike,'dte':3.0,'option_type':typ,
                         'open_interest':oi/2,'volume':vol/2,'iv':.25})
    df=pd.DataFrame(rows)
    out=analyze_gamma_delta(df,engine_config_for_asset('SPY'))
    cur=out['current_delta'].set_index('strike')
    raw=float(cur.loc[100.0,'turnover_raw']); stable=float(cur.loc[100.0,'turnover'])
    assert stable < raw
    assert float(cur.loc[100.0,'turnover_oi_stabilizer']) >= 25.0


def test_net_tilt_noise_floor_neutralizes_without_injecting_mass():
    """v1.14.3 replaces the continuous gross-magnitude gate with a hard noise floor.

    The v1.14.1 contract was `net_tilt_pct` ordered by mass, which is precisely what
    made the feature collinear with open interest again: multiplying the tilt by
    0.30 + 0.70*magnitude(gross) put OI back inside a feature whose whole purpose was
    to be mass-free. The contract is now:
      - strikes below the gross-GEX noise floor are neutralised to 0.5 (no evidence);
      - every strike above it keeps the clean, mass-free tilt;
      - so net_tilt must NOT be strongly rank-correlated with open interest.
    """
    now = pd.Timestamp('2026-09-04 10:00:00')
    rows = []
    rng = np.random.default_rng(11)
    for strike in np.arange(80.0, 121.0, 1.0):
        m = abs(strike - 100.0) / 100.0
        oi = max(1, int(rng.lognormal(6.5 - 9 * m, 1.0)))
        for typ in ('call', 'put'):
            rows.append({'timestamp': now, 'underlying_price': 100.0, 'strike': float(strike),
                         'dte': 7.0, 'option_type': typ,
                         'open_interest': oi if typ == 'call' else int(oi * rng.uniform(0.0, 1.2)),
                         'volume': int(oi * rng.uniform(0.05, 1.5)), 'iv': .25})
    out = analyze_gamma_delta(pd.DataFrame(rows), engine_config_for_asset('SPY'))
    cur = out['current_delta']

    # Strikes in the bottom quartile of gross GEX carry no tilt evidence.
    floor_q = float(cur['net_tilt_floor_quantile'].iloc[0])
    gross_rank = cur['gross_gex'].rank(pct=True)
    quiet = cur[gross_rank < floor_q]
    if len(quiet):
        assert np.allclose(quiet['net_tilt_pct'].to_numpy(float), 0.5)

    # And the feature must stay essentially free of open-interest mass.
    corr = abs(cur['net_tilt_pct'].corr(cur['open_interest'], method='spearman'))
    assert corr < 0.35, f'net_tilt volvió a correlacionar con OI: {corr:.3f}'


def test_regime_confidence_has_no_artificial_floor_when_no_evidence():
    r=classify_regime({'spot':100.0,'gamma_flip':200.0}, {'regime':''}, {'direction':''})
    assert r['confidence'] == 0.0
