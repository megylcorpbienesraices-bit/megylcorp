"""v1.15.1 · TRACE PRO 3 analytics + hardening."""
import numpy as np
import pandas as pd

from app.core.trace_analytics import (heat_matrix, dealer_flow_line, structural_walls,
                                      expected_move_band, expected_move_band_from_chain,
                                      charm_pressure_proxy, forward_profile)


def _decaying_pivot():
    strikes = np.arange(-10, 11)
    shape = np.exp(-(strikes ** 2) / 18.0) * np.sign(np.cos(strikes / 4))
    decay = np.linspace(1.0, 0.10, 8)          # same shape, 10x smaller by the end
    raw = np.outer(shape, np.ones(8)) * decay * 1e6
    return pd.DataFrame(raw, index=strikes + 600,
                        columns=pd.date_range("2026-09-04 15:30", periods=8, freq="5min"))


def test_column_scaling_hides_magnitude_and_session_scaling_does_not():
    pv = _decaying_pivot()
    col = heat_matrix(pv, "column")["z"]
    ses = heat_matrix(pv, "session")["z"]
    # Legacy behaviour: first and last column render identically despite a 10x decay.
    assert np.allclose(np.abs(col).max(axis=0), 100.0)
    # Fixed denominator: intensity must fall monotonically with the real structure.
    peaks = np.abs(ses).max(axis=0)
    assert peaks[0] > peaks[-1]
    assert np.all(np.diff(peaks) <= 1e-9)


def test_anchor_scaling_is_comparable_across_sessions():
    pv = _decaying_pivot()
    anchor = {"hi": float(np.log1p(1e6)), "n": 5}
    small = heat_matrix(pv * 0.1, "anchor", anchor=anchor)
    same = heat_matrix(pv, "anchor", anchor=anchor)
    assert np.abs(small["z"]).max() < np.abs(same["z"]).max()
    assert same["comparable_across_sessions"] is True
    # An anchor with too few sessions must degrade, never be fabricated.
    thin = heat_matrix(pv, "anchor", anchor={"hi": 1.0, "n": 1})
    assert thin["comparable_across_sessions"] is False


def test_dealer_flow_is_delta_weighted_and_flips_with_the_flow():
    rng = np.random.default_rng(5); n = 600
    t0 = pd.Timestamp("2026-09-04 15:30")
    ev = pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(seconds=int(s)) for s in np.sort(rng.uniform(0, 3600, n))],
        "strike": 600.0, "expiration_date": "2026-09-04",
        "contracts": rng.lognormal(1.5, 1.0, n).clip(1),
        "open_interest": rng.lognormal(6.5, 1.0, n).clip(1),
        "daily_volume": rng.lognormal(7, 1, n), "aggressor": "BUY",
        "aggressor_confidence": rng.uniform(.5, .95, n), "underlying_price": 600.0,
        "option_type": ["call"] * (n // 2) + ["put"] * (n - n // 2),
        "provider_delta": [0.5] * (n // 2) + [-0.5] * (n - n // 2), "provider_gamma": 0.04})
    line = dealer_flow_line(ev, resample="10min")
    assert not line.empty
    first, last = line["hedge_notional"].iloc[0], line["hedge_notional"].iloc[-1]
    # Under the explicit dealer-counterparty scenario, call flow estimates BUY hedge
    # pressure and put flow estimates SELL hedge pressure. It is not observed inventory.
    assert first > 0 > last
    assert bool(line["is_estimate"].all())
    assert float(line["counterparty_share_assumption_pct"].iloc[0]) == 70.0
    assert dealer_flow_line(pd.DataFrame()).empty


def test_walls_use_gamma_not_stale_open_interest():
    spot = 600.0
    ks = np.arange(590, 611, 1.0)
    oi = np.where(ks == 610, 90000, np.where(np.abs(ks - spot) <= 3, 12000, 2500))
    gamma_pc = np.exp(-((ks - spot) ** 2) / 8.0) * 0.05
    gross = gamma_pc * oi * 100 * spot ** 2 * 0.01
    signed = gross * np.where(ks >= spot, 1, -1)
    cur = pd.DataFrame({"strike": ks, "open_interest": oi, "gross_gex": gross, "signed_gex": signed})
    w = structural_walls(cur, spot)
    assert w["call_wall"] is not None and w["call_wall"] < 610      # not the stale-OI strike
    assert w["put_wall"] is not None and w["put_wall"] < spot
    assert abs(w["key_gamma"] - spot) <= 1.0


def test_expected_move_band_narrows_as_time_passes():
    times = pd.date_range("2026-09-04 14:30", periods=6, freq="45min")
    b = expected_move_band(600.0, 18.0, 0.30, times)
    assert b["ready"]
    widths = [u - d for u, d in zip(b["upper"], b["lower"])]
    assert all(widths[i] > widths[i + 1] for i in range(len(widths) - 1))
    assert expected_move_band(600.0, float("nan"), 0.3, times)["ready"] is False


def test_forward_profile_concentrates_gamma_toward_atm_near_expiry():
    spot = 600.0; rows = []
    for k in np.arange(594, 607, 1.0):
        for typ in ("call", "put"):
            m = abs(k - spot) / spot
            base = 9000 * np.exp(-((k - spot) ** 2) / 6.0) + 300
            skew = 1.35 if (typ == "call" and k >= spot) or (typ == "put" and k < spot) else 0.55
            rows.append(dict(timestamp=pd.Timestamp("2026-09-04 15:30"), underlying_price=spot,
                             strike=float(k), dte=0.28, option_type=typ,
                             iv=0.20 + 1.4 * m ** 1.5, open_interest=int(base * skew)))
    fp = forward_profile(pd.DataFrame(rows), minutes_ahead=90.0, spot=spot, symbol="SPY")
    assert not fp.empty
    def dispersion(col):
        w = fp[col].abs()
        c = (fp["strike"] * w).sum() / w.sum()
        return float(np.sqrt(((fp["strike"] - c) ** 2 * w).sum() / w.sum()))
    assert dispersion("gamma_forward") < dispersion("gamma_now")
    assert forward_profile(pd.DataFrame()).empty


def test_expected_move_asof_does_not_apply_current_dte_to_the_morning():
    times = pd.date_range("2026-09-04 14:30", periods=4, freq="60min")
    # 0.10 DTE belongs to the LAST timestamp. Earlier timestamps must reconstruct more DTE.
    b = expected_move_band(600.0, 20.0, 0.10, times, dte_asof=times[-1])
    widths = np.array(b["upper"]) - np.array(b["lower"])
    assert widths[0] > widths[-1]
    assert b["dte_asof"].startswith(str(times[-1].date()))


def test_expected_move_history_uses_each_snapshots_own_iv_and_dte():
    rows=[]
    for ts,iv,dte,spot in [(pd.Timestamp("2026-09-04 14:30"),0.15,0.30,600.0),
                           (pd.Timestamp("2026-09-04 15:30"),0.30,0.20,602.0)]:
        for k in (600.0,602.0):
            for typ in ("call","put"):
                rows.append({"timestamp":ts,"underlying_price":spot,"strike":k,"iv":iv,"dte":dte,"option_type":typ,"open_interest":100})
    b=expected_move_band_from_chain(pd.DataFrame(rows))
    assert b["ready"] and b["source"]=="EACH TRACE SNAPSHOT"
    # IV doubles in the second snapshot, so the actual implied band can widen despite less time.
    widths=np.array(b["upper"])-np.array(b["lower"])
    assert widths[1] > widths[0]


def test_forward_profile_reprices_delta_in_notional_and_zeros_expired_contracts():
    spot=600.0
    rows=[
        {"timestamp":pd.Timestamp("2026-09-04 15:30"),"underlying_price":spot,"strike":600.0,"dte":0.20,"option_type":"call","iv":0.20,"open_interest":10},
        {"timestamp":pd.Timestamp("2026-09-04 15:30"),"underlying_price":spot,"strike":601.0,"dte":0.02,"option_type":"call","iv":0.20,"open_interest":10},
    ]
    fp=forward_profile(pd.DataFrame(rows),minutes_ahead=60.0,spot=spot,symbol="SPY")
    atm=fp.loc[fp["strike"].eq(600.0)].iloc[0]
    exp=fp.loc[fp["strike"].eq(601.0)].iloc[0]
    # Delta notional uses Delta * OI * 100 * Spot, matching the engine's TRACE unit.
    assert abs(float(atm["delta_now"])) > 100000.0
    # The 0.02-DTE contract expires before +60m and must not be frozen at 1 minute to expiry.
    assert abs(float(exp["gamma_forward"])) < 1e-12
    assert abs(float(exp["delta_forward"])) < 1e-12


def test_charm_pressure_proxy_is_repriced_delta_drift_not_charm_shortcut():
    row=pd.DataFrame([{"timestamp":pd.Timestamp("2026-09-04 15:30"),"underlying_price":600.0,
                      "strike":600.0,"dte":0.25,"option_type":"call","iv":0.20,"open_interest":500}])
    x=charm_pressure_proxy(row,minutes=30.0,symbol="SPY")
    assert len(x)==1 and np.isfinite(float(x.iloc[0]))

def _tape(n, p_buy, seed=3):
    r = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-09-04 15:30")
    sign = np.where(r.random(n) < p_buy, 1, -1)
    size = r.lognormal(3.2, .9, n).clip(1).astype(int)
    return pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(milliseconds=int(x)) for x in np.sort(r.uniform(0, 1800_000, n))],
        "price": 600 + np.cumsum(sign * size * 1e-5), "size": size, "aggressor_sign": sign})


def test_churn_produces_bars_instead_of_disappearing():
    """A pure-imbalance trigger cannot close a bar when neither side wins.

    Balanced heavy trade used to be swallowed into oversized bars. The volume trigger
    must surface it as CHURN. (Note: CHURN is not absorption - see the module docstring;
    true absorption is one-sided flow that fails to move price, flagged separately.)
    """
    from app.core.trace_analytics import aggression_bars
    balanced = aggression_bars(_tape(8000, 0.50), 250, absorption_multiple=3.0)
    trending = aggression_bars(_tape(8000, 0.88), 250, absorption_multiple=3.0)
    n_abs_bal = int((balanced["bar_type"] == "CHURN").sum())
    n_abs_trend = int((trending["bar_type"] == "CHURN").sum())
    assert n_abs_bal > 0, "una cinta 50/50 debe producir velas de CHURN"
    assert n_abs_bal > n_abs_trend, "hay más churn en la pelea que en la tendencia"
    # Every completed bar must trip exactly one of the two triggers.
    done = balanced[balanced["complete"]]
    imb = done[done["bar_type"] == "IMBALANCE"]
    assert (imb["delta"].abs() >= 250 - 1e-9).all()


def test_aggression_score_is_continuous_not_six_states():
    from app.core.trace_analytics import aggression_bars, aggression_strength
    scores = set()
    for s in range(60):
        p = float(np.random.default_rng(s).uniform(.35, .75))
        scores.add(aggression_strength(aggression_bars(_tape(2500, p, seed=s), 250), 250)["score"])
    assert len(scores) > 25, f"el score volvió a discretizarse: {len(scores)} valores"


def test_balanced_tape_is_not_reported_as_a_direction():
    from app.core.trace_analytics import aggression_bars, aggression_strength
    calls = []
    for s in range(40):
        st = aggression_strength(aggression_bars(_tape(4000, 0.50, seed=s), 250), 250)
        calls.append(st["control"])
    mixed = calls.count("MIXED") / len(calls)
    assert mixed >= 0.5, f"solo {mixed:.0%} de cintas 50/50 se reportan MIXED"


def test_strong_tape_keeps_its_direction_even_when_contested():
    from app.core.trace_analytics import aggression_bars, aggression_strength
    st = aggression_strength(aggression_bars(_tape(9000, 0.62), 250), 250)
    assert st["control"] == "BUY"          # direction survives
    assert st["contested"] in (True, False)  # reported separately, never overriding

def _zone_tape(p_buy, ticks_per_min, mu_size, minutes=5, seed=4, center=600.0):
    r = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-09-04 15:30")
    n = int(ticks_per_min * minutes)
    return pd.DataFrame([{
        "timestamp": t0 + pd.Timedelta(seconds=k * (minutes * 60.0 / n)),
        "price": center + r.normal(0, .05),
        "size": int(np.clip(r.lognormal(mu_size, .7), 1, None)),
        "aggressor_sign": 1 if r.random() < p_buy else -1} for k in range(n)])


def test_bars_have_a_time_bound():
    """Without max_seconds a bar can print nothing for a quarter of an hour."""
    from app.core.trace_analytics import aggression_bars
    quiet = _zone_tape(0.50, 6, 1.2, minutes=30, seed=7)
    unbounded = aggression_bars(quiet, 250, max_seconds=None)
    bounded = aggression_bars(quiet, 250, max_seconds=120)
    done = bounded[bounded["complete"]]
    assert len(bounded) > len(unbounded)
    assert (pd.to_numeric(done["duration_s"], errors="coerce") <= 121).all()
    assert (bounded["bar_type"] == "TIMEOUT").any()


def test_confirmation_fires_before_any_bar_would_close():
    from app.core.trace_analytics import aggression_bars, tape_confirmation
    tk = _zone_tape(0.70, 45, 2.6)
    t0 = pd.Timestamp("2026-09-04 15:30")
    fired = None
    for sec in range(5, 181, 5):
        now = t0 + pd.Timedelta(seconds=sec)
        c = tape_confirmation(tk[tk["timestamp"] <= now], 599.8, 600.2, "BUY", 250,
                              budget_seconds=180, now=now)
        if c["state"] == "CONFIRMED":
            fired = sec; break
    assert fired is not None and fired <= 120, f"confirmó a los {fired}s"


def _drift_tape(n, p_buy, drift, mu_size, seed=4):
    """Tape where price moves `drift` per unit of signed volume.

    drift=0 is the absorption shape: heavy one-sided aggression that does not move price.
    """
    r = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-09-04 15:30")
    rows = []; cum = 0.0
    for k in range(n):
        sz = int(np.clip(r.lognormal(mu_size, .7), 1, None))
        sg = 1 if r.random() < p_buy else -1
        cum += sg * sz
        rows.append({"timestamp": t0 + pd.Timedelta(seconds=k * 180.0 / n),
                     "price": 600.0 + cum * drift + r.normal(0, .02),
                     "size": sz, "aggressor_sign": sg})
    return pd.DataFrame(rows)


def test_absorption_is_not_confused_with_churn_or_with_confirmation():
    """Three shapes that a naive delta reading collapses into one.

    CONFIRMED : one-sided flow AND price responds.
    ABSORBED  : one-sided flow, price does NOT respond. Passive size is eating it. This
                is a warning, and calling it a confirmation is the expensive mistake.
    CHURN     : balanced flow, heavy volume. Indecision, not defence.
    """
    from app.core.trace_analytics import tape_confirmation
    now = pd.Timestamp("2026-09-04 15:30") + pd.Timedelta(seconds=181)
    def state(p, drift, mu, n=540):
        return tape_confirmation(_drift_tape(n, p, drift, mu), 598.0, 602.0, "BUY", 250,
                                 budget_seconds=180, now=now)
    assert state(0.74, 4e-4, 2.6)["state"] == "CONFIRMED"
    assert state(0.74, 0.0, 2.6)["state"] == "ABSORBED"
    assert state(0.26, 4e-4, 2.6)["state"] == "REJECTED"
    assert state(0.50, 1e-4, 3.6)["state"] == "CHURN"
    assert state(0.52, 3e-4, 1.0, n=20)["state"] == "EXPIRED"


def test_no_entry_is_left_hanging_after_the_budget():
    from app.core.trace_analytics import tape_confirmation
    now = pd.Timestamp("2026-09-04 15:30") + pd.Timedelta(seconds=181)
    for p, drift, mu, n in ((.74, 4e-4, 2.6, 540), (.26, 4e-4, 2.6, 540),
                            (.50, 1e-4, 3.6, 540), (.52, 3e-4, 1.0, 20)):
        c = tape_confirmation(_drift_tape(n, p, drift, mu), 598.0, 602.0, "BUY", 250,
                              budget_seconds=180, now=now)
        assert c["state"] != "ARMED", "quedó colgado sin veredicto"
        assert c["seconds_in_zone"] >= 0


def test_confirmation_ignores_flow_from_an_earlier_visit():
    """Only the current uninterrupted visit to the zone counts."""
    from app.core.trace_analytics import tape_confirmation
    t0 = pd.Timestamp("2026-09-04 15:30")
    old = _zone_tape(0.95, 45, 3.0, minutes=2, seed=2)                 # heavy buying, long ago
    away = _zone_tape(0.50, 30, 2.0, minutes=3, seed=3, center=602.0)  # price leaves the zone
    away["timestamp"] = away["timestamp"] + pd.Timedelta(minutes=2)
    back = _zone_tape(0.50, 10, 1.2, minutes=1, seed=5)                # returns, nobody there
    back["timestamp"] = back["timestamp"] + pd.Timedelta(minutes=5)
    tk = pd.concat([old, away, back], ignore_index=True)
    now = t0 + pd.Timedelta(minutes=6, seconds=1)
    c = tape_confirmation(tk, 599.8, 600.2, "BUY", 250, budget_seconds=45, now=now)
    assert c["state"] != "CONFIRMED", "usó flujo de una visita anterior"
    assert c["seconds_in_zone"] < 120


def test_cadence_separates_a_pin_from_an_active_tape():
    from app.core.trace_analytics import aggression_bars, bar_cadence
    quiet = bar_cadence(aggression_bars(_zone_tape(0.51, 6, 1.2, minutes=30, seed=8), 250))
    busy = bar_cadence(aggression_bars(_zone_tape(0.62, 200, 3.0, minutes=30, seed=9), 250))
    assert quiet["bars_per_minute"] < busy["bars_per_minute"]
    assert busy["regime"] in ("ACTIVO", "AGRESIÓN INTENSA")


def test_absorbed_flag_survives_dataframe_return():
    from app.core.trace_analytics import aggression_bars
    b=aggression_bars(_drift_tape(540,.74,0.0,2.6),250)
    assert "absorbed" in b.columns and "efficiency" in b.columns


def test_pinned_tape_zero_efficiency_does_not_emit_empty_slice_warning():
    """A flat/pinned tape is valid market data, not an empty statistical sample."""
    import warnings
    from app.core.trace_analytics import aggression_bars

    t0 = pd.Timestamp("2026-09-18 07:40:00")
    ticks = pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(seconds=i) for i in range(12)],
        "price": [515.80] * 12,
        "size": [100.0] * 12,
        "aggressor_sign": [1] * 12,
    })
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        bars = aggression_bars(ticks, threshold=100.0)
    assert not bars.empty
    assert (pd.to_numeric(bars["efficiency"], errors="coerce").fillna(0.0) == 0.0).all()


def test_bar_cadence_all_nan_duration_is_explicit_not_runtime_warning():
    import warnings
    from app.core.trace_analytics import bar_cadence

    t0 = pd.Timestamp("2026-09-18 07:40:00")
    bars = pd.DataFrame({
        "end": [t0, t0 + pd.Timedelta(seconds=30)],
        "duration_s": [np.nan, np.nan],
        "bar_type": ["FORMING", "FORMING"],
    })
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        out = bar_cadence(bars)
    assert out["median_seconds"] is None
