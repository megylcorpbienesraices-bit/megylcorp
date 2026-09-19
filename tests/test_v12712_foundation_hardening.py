"""v1.27.12 · mathematical/security foundations hardened before VPS/LIVE.

These tests are behavioural guards for the six approved changes.  They do not claim
alpha or LIVE profitability; they protect the data/model contracts that feed Scanner.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.core import net_guard
from app.core.calibration import probability_calibration
from app.core.expiry_clock import (
    SECONDS_PER_YEAR,
    bs_year_fraction,
    is_regular_session_day,
    trading_minutes_between,
    year_fraction,
)
from app.core.sophia_core import _actionable_side, validate_against_scanner
from app.core.source_arbitration import arbitrate, score_source
from app.core.temporal_truth import EVENT_TTL_MS
from app.core.volatility_surface import (
    forward_price,
    svi_basic_checks,
    svi_full_checks,
)

NY = ZoneInfo("America/New_York")


def _calibration_frame(session_count: int, rows_per_session: int = 36, seed: int = 12712):
    rng=np.random.default_rng(seed)
    sessions=pd.date_range("2026-03-02",periods=session_count,freq="B").strftime("%Y-%m-%d").tolist()
    rows=[]
    for i,day in enumerate(sessions):
        for _ in range(rows_per_session):
            ev=float(rng.uniform(30,98))
            # Strong but not deterministic relation, stationary across sessions.
            p=float(np.clip(.08+.84/(1+np.exp(-(ev-67)/5.2)),.02,.98))
            ok=bool(rng.random()<p)
            rows.append({"horizon":10,"evidence_score":ev,"outcome":"T1" if ok else "INVALIDATION","session_date":day})
    return sessions,pd.DataFrame(rows)


def test_foundation_hardening_survives_current_release_identity():
    from pathlib import Path
    import json
    version=Path("VERSION.txt").read_text().strip()
    meta=json.loads(Path(".itm_quant_product.json").read_text())
    assert meta["version"]==version
    assert meta["scope"]=="MULTI_ASSET"
    assert str(meta["release"]).endswith("_PRE_VPS")


def test_bs_clock_uses_exact_calendar_seconds_near_expiry():
    now=datetime(2026,9,18,15,57,41,326000,tzinfo=NY)
    expiry=datetime(2026,9,18,16,0,0,tzinfo=NY)
    remaining=bs_year_fraction(expiry_at=expiry,now=now)*SECONDS_PER_YEAR
    assert remaining==pytest.approx(138.674,abs=1e-9)
    # No historic 1-minute or 0.01-day expansion for a 9-second DTE.
    assert year_fraction(9/86400.0)*SECONDS_PER_YEAR==pytest.approx(9.0,abs=1e-9)


def test_trading_clock_is_separate_and_holiday_aware():
    # 2026-07-03 is the observed XNYS Independence Day closure (July 4 is Saturday).
    assert not is_regular_session_day(date(2026,7,3))
    assert not is_regular_session_day(date(2021,12,31))  # observed 2022 New Year closure
    a=datetime(2026,7,3,10,0,tzinfo=NY); b=datetime(2026,7,3,15,0,tzinfo=NY)
    assert trading_minutes_between(a,b)==0.0
    # By contrast, the BS clock still counts calendar time across market closures.
    assert bs_year_fraction(expiry_at=b,now=a)*SECONDS_PER_YEAR==pytest.approx(5*3600.0)


def test_svi_counterexample_is_hard_rejected_by_durrleman():
    p={"a":0.0005,"b":0.45,"rho":-0.85,"m":0.02,"sigma":0.02}
    assert svi_basic_checks(p)["pass"] is True
    hard=svi_full_checks(p)
    assert hard["pass"] is False
    assert hard["state"]=="BUTTERFLY_ARBITRAGE"
    assert hard["min_durrleman_g"]==pytest.approx(-2.7097933270,rel=1e-6)
    assert hard["negative_pct"]>20.0


def test_svi_forward_uses_carry_not_spot_proxy():
    f=forward_price(100.0,0.05,0.01,0.5)
    assert f==pytest.approx(100.0*np.exp((.05-.01)*.5))
    assert f>100.0


def test_calibration_gate_sessions_are_shadow_then_provisional():
    sessions19,df19=_calibration_frame(19)
    m19=probability_calibration(df19,sessions19,10,min_sessions=8)
    assert not m19["ready"] and m19["session_stage"]=="SHADOW"
    sessions25,df25=_calibration_frame(25)
    m25=probability_calibration(df25,sessions25,10,min_sessions=8)
    assert not m25["ready"] and m25["session_stage"]=="PROVISIONAL_RESEARCH"


def test_calibration_gate_uses_validation_final_oos_and_session_bootstrap(monkeypatch):
    monkeypatch.setenv("ITM_CALIB_BOOTSTRAP_DRAWS","500")
    sessions,df=_calibration_frame(45,rows_per_session=44)
    m=probability_calibration(df,sessions,10,min_sessions=8)
    assert m["session_stage"]=="ELIGIBLE_FOR_PROMOTION"
    assert m["selected_calibrator"] in {"PLATT","ISOTONIC"}
    assert m["validation_sessions"]>0 and m["test_sessions"]>0 and m["purged_sessions"]>=2
    assert set(m["final_oos_sessions"]).isdisjoint(set(m["split"]["validation"]))
    assert m["bootstrap_draws"]>=500
    assert m["brier_skill_ci_low"] is not None
    # Strong synthetic relationship should earn promotion under the predeclared gate.
    assert m["ready"] is True and m["statistical_promotion_pass"] is True


def test_rate_limiter_reads_do_not_allocate_attacker_keys():
    net_guard.reset_rate_limiter()
    for i in range(10_000):
        assert not net_guard.is_rate_limited(f"198.18.{(i//256)%256}.{i%256}")
    assert net_guard.rate_limiter_stats()["tracked_ips"]==0


def test_rate_limiter_failure_cache_is_bounded(monkeypatch):
    net_guard.reset_rate_limiter(); monkeypatch.setattr(net_guard,"MAX_TRACKED_IPS",128)
    for i in range(500):
        net_guard.record_failure(f"198.19.{(i//256)%256}.{i%256}")
    stats=net_guard.rate_limiter_stats()
    assert stats["tracked_ips"]<=128
    assert stats["global_policy"]=="TELEMETRY_ONLY_NO_GLOBAL_LOCKOUT"


def test_proxy_headers_require_verified_direct_peer(monkeypatch):
    monkeypatch.setenv("ITM_TRUST_PROXY","1")
    monkeypatch.setenv("ITM_TRUSTED_PROXY_IPS","127.0.0.1/32")
    spoof={"client":("203.0.113.9",1),"headers":[(b"x-forwarded-for",b"1.2.3.4")]}
    trusted={"client":("127.0.0.1",1),"headers":[(b"x-forwarded-for",b"198.51.100.4, 198.51.100.5")]}
    assert net_guard.client_ip(spoof)=="203.0.113.9"
    assert net_guard.client_ip(trusted)=="198.51.100.5"


def test_source_arbitration_uses_channel_policy_for_open_interest():
    row={"name":"A","channel":"OPEN_INTEREST","latency_ms":20_000,"age_ms":12*3600*1000,
         "gap_rate":0.0,"sequence_ok":True,"cross_source_divergence":0.0,"status":"LIVE"}
    s=score_source(row)
    assert s["channel_policy"]=="OPEN_INTEREST"
    assert s["quality_label"]!="POOR"
    assert EVENT_TTL_MS["OPEN_INTEREST"]>=36*3600*1000


def test_source_hysteresis_prevents_near_equal_flapping():
    a={"name":"ALPACA","channel":"EQUITY_QUOTE","latency_ms":80,"age_ms":150,"gap_rate":0,"sequence_ok":True,"cross_source_divergence":0,"status":"LIVE"}
    b={"name":"TASTY","channel":"EQUITY_QUOTE","latency_ms":60,"age_ms":120,"gap_rate":0,"sequence_ok":True,"cross_source_divergence":0,"status":"LIVE"}
    out=arbitrate([a,b],previous="ALPACA",switch_margin=10.0,min_dwell_ms=0)
    assert out["candidate"]=="TASTY"
    assert out["selected"]=="ALPACA"
    assert out["switch_reason"]=="HYSTERESIS_RETAIN_PREVIOUS"


def test_sophia_post_llm_governor_blocks_opposite_but_understands_negation():
    state={"scanner":{"direction":"SELL"},"publication_gate":{"publicar_permitido":True}}
    blocked=validate_against_scanner("Yo compraria aqui.",state)
    assert blocked["blocked"] and blocked["reason"]=="SCANNER_DIRECTION_CONFLICT"
    allowed=validate_against_scanner("No compraria aqui; venderia.",state)
    assert not allowed["blocked"] and allowed["detected_recommendation"]=="SELL"
    assert _actionable_side("El contexto es bajista, pero no recomiendo comprar.") is None
