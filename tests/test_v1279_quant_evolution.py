"""v1.27.9: quantitative evolution without changing live authority by decree."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_oi_structural_freshness_accepts_friday_on_monday():
    from app.core.oi_freshness import assess_oi_structural_freshness, STRUCTURAL_VALID
    out = assess_oi_structural_freshness(["2026-09-11"], as_of="2026-09-14T10:00:00-04:00")
    assert out["estado"] == STRUCTURAL_VALID
    assert out["expected_session"] == "2026-09-11"
    assert out["would_block_gex_if_promoted"] is False
    assert out["mode"] == "SHADOW_ONLY"


def test_oi_structural_freshness_marks_previous_expected_session_stale():
    from app.core.oi_freshness import assess_oi_structural_freshness, STRUCTURAL_STALE
    out = assess_oi_structural_freshness(["2026-09-11"], as_of="2026-09-15T10:00:00-04:00")
    assert out["estado"] == STRUCTURAL_STALE
    assert out["expected_session"] == "2026-09-14"
    assert out["lag_business_sessions"] == 1
    assert out["would_block_gex_if_promoted"] is True


def test_oi_structural_freshness_never_calls_missing_timestamp_fresh():
    from app.core.oi_freshness import assess_oi_structural_freshness, STRUCTURAL_UNKNOWN
    out = assess_oi_structural_freshness([], as_of="2026-09-15T10:00:00-04:00")
    assert out["estado"] == STRUCTURAL_UNKNOWN
    assert out["would_block_gex_if_promoted"] is True


def test_oi_structural_freshness_is_shadow_not_hard_gate_yet():
    from app.core.oi_freshness import assess_oi_structural_freshness
    out = assess_oi_structural_freshness(["2026-09-01"], as_of="2026-09-15T10:00:00-04:00")
    assert out["mode"] == "SHADOW_ONLY"
    assert "hard gate" in out["note"].lower()


def test_data_quality_exposes_oi_and_greeks_provenance_without_relabeling_probability():
    from app.core.precision_engine import build_data_quality
    now = pd.Timestamp("2026-09-15T14:00:00Z")
    df = pd.DataFrame({
        "strike": [530.0, 531.0], "bid": [1.0, 1.1], "ask": [1.1, 1.2],
        "provider_delta": [.5, .45], "provider_gamma": [.02, .019],
        "fallback_delta": [.49, .44], "fallback_gamma": [.021, .020],
        "greeks_dislocation": [False, False],
    })
    meta = {
        "quality_clock": now, "latest_option_market_timestamp": now - pd.Timedelta(seconds=2),
        "stock_market_timestamp": now - pd.Timedelta(seconds=1), "market_state": "OPEN",
        "matched_snapshots": 2, "contract_definitions": 2, "oi_dates": ["2026-09-11"],
    }
    out = build_data_quality(df, meta, {}, {"count": 1, "mode": "WEEKLY"})
    assert out["oi_structural_freshness"]["estado"] == "STRUCTURAL_STALE"
    assert out["greeks_provenance"]["provider_greeks_coverage_pct"] == 100.0
    assert "no probabilidad" in out["greeks_provenance"]["note"].lower()


def test_candidate_promotes_only_after_oos_shadow_baseline_improvement():
    from app.core.model_governance import evaluate_candidate
    current = {"brier": .20, "log_loss": .60, "ev_net": .10, "drawdown": .10, "profit_factor": 1.20}
    candidate = {"brier": .18, "log_loss": .56, "ev_net": .13, "drawdown": .09, "profit_factor": 1.30}
    decision = evaluate_candidate(current, candidate, samples=200, sessions=12, shadow=True, purge_gap=True)
    assert decision.promotable is True and decision.state == "PROMOTE"


def test_candidate_cannot_promote_for_complexity_without_measured_gain():
    from app.core.model_governance import evaluate_candidate
    current = {"brier": .20, "log_loss": .60, "ev_net": .10, "drawdown": .10}
    candidate = {"brier": .201, "log_loss": .603, "ev_net": .099, "drawdown": .10}
    decision = evaluate_candidate(current, candidate, samples=500, sessions=30, shadow=True, purge_gap=True)
    assert decision.promotable is False
    assert any("no mejora" in r for r in decision.reasons)


def test_candidate_cannot_trade_a_small_score_gain_for_materially_worse_drawdown():
    from app.core.model_governance import evaluate_candidate
    current = {"brier": .20, "ev_net": .10, "drawdown": .10}
    candidate = {"brier": .17, "ev_net": .14, "drawdown": .13}
    decision = evaluate_candidate(current, candidate, samples=300, sessions=20, shadow=True, purge_gap=True)
    assert decision.promotable is False
    assert any("drawdown" in r for r in decision.reasons)


def test_governance_contract_keeps_current_as_only_authority():
    from app.core.model_governance import governance_contract
    c = governance_contract()
    assert c["authority"] == "CURRENT_ONLY"
    assert c["candidate_role"] == "SHADOW_ONLY_UNTIL_PROMOTED"
    assert "OOS" in c["promotion_requires"] and "PURGE_GAP" in c["promotion_requires"]


def test_gex_reconciliation_exposes_structural_and_estimated_dealer_models_separately():
    from app.core.dealer_intelligence import gex_reconciliation
    chain = pd.DataFrame({"contract_symbol": ["A", "B"], "signed_gex_proxy": [2_000_000.0, -500_000.0]})
    out = gex_reconciliation(chain, dealer_gex=750_000.0, dealer_confidence=72.0, inventory_contracts=1)
    assert out["structural_gex"] == pytest.approx(1_500_000.0)
    assert out["estimated_dealer_gex"] == pytest.approx(750_000.0)
    assert out["structural_label"] == "STRUCTURAL GEX PROXY"
    assert out["dealer_label"] == "ESTIMATED DEALER GEX · SHADOW"
    assert out["agreement"] == "ALIGNED"


def test_ui_never_labels_structural_gex_as_observed_dealer_inventory():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "STRUCTURAL GEX PROXY" in html
    assert "no afirma inventario dealer" in html
    assert "ESTIMATED DEALER GEX · SHADOW" in html


def test_gamma_flip_ui_is_explicitly_approximate_while_internal_value_remains_numeric():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert "`≈ ${fmt(s.gamma_flip,1)}`" in js
    assert "`≈ ${fmt(p.gamma_flip,1)}`" in js


def test_observability_severity_distinguishes_critical_data_from_normal_degradation():
    from app.core.obs import degradations, note, reset_degradations
    reset_degradations()
    note("optional.feature", RuntimeError("optional"), severity="OPTIONAL")
    note("chain.input", RuntimeError("stale chain"), severity="CRITICAL_DATA")
    d = degradations()
    assert d["status"] == "CRITICAL"
    assert d["by_severity"]["OPTIONAL"] == 1
    assert d["by_severity"]["CRITICAL_DATA"] == 1
    reset_degradations()


@pytest.mark.parametrize(
    "S,K,T,sigma,kind,r,q",
    [
        (100.0, 50.0, 1 / 365.0, 0.05, "c", 0.0, 0.0),   # deep ITM, near expiry
        (100.0, 150.0, 1 / 365.0, 0.80, "c", 0.0, 0.0),  # deep OTM, high IV
        (100.0, 100.0, 1 / 36500.0, 0.20, "p", 0.01, 0.02),
        (425.5, 440.0, 30 / 365.0, 2.00, "c", 0.00, 0.013),
    ],
)
def test_black_scholes_extremes_remain_finite_and_inside_basic_bounds(S, K, T, sigma, kind, r, q):
    from app.core.precision_engine import black_scholes_greeks_full, black_scholes_price
    price = black_scholes_price(S, K, T, sigma, kind, r, q)
    g = black_scholes_greeks_full(S, K, T, sigma, kind, r, q)
    assert math.isfinite(price) and price >= 0.0
    assert all(math.isfinite(float(g[k])) for k in ("delta", "gamma", "vanna", "charm", "speed"))
    assert g["gamma"] >= 0.0
    if kind == "c":
        assert price <= S * math.exp(-q * T) + 1e-9
    else:
        assert price <= K * math.exp(-r * T) + 1e-9


def test_sophia_digest_receives_oi_and_greeks_provenance_without_gaining_directional_authority():
    from app.core.sophia_core import SophiaRuntime
    state = {
        "symbol": "DIA",
        "scanner": {"direction": "BUY", "evidence_score": 80},
        "data_quality_report": {
            "circuito_frescura": {"publicar_permitido": True},
            "oi_structural_freshness": {"estado": "STRUCTURAL_STALE", "mode": "SHADOW_ONLY"},
            "greeks_provenance": {"input_support_label": "HIGH"},
        },
    }
    d = SophiaRuntime._digest(state, {})
    assert d["oi_structural_freshness"]["estado"] == "STRUCTURAL_STALE"
    assert d["greeks_provenance"]["input_support_label"] == "HIGH"
    assert d["scanner"]["direction"] == "BUY", "Scanner remains the sole directional source"
